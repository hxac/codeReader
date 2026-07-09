# 语义分割后处理：ICNet 着色叠加

## 1. 本讲目标

本讲是「后处理算法」单元的最后一篇，承接 [u5-l2（输出读取与 epmat→ncnn::Mat 转换）](u5-l2-output-read-and-epmat-conversion.md)，把目光从「分类」「检测」转向**语义分割（semantic segmentation）**。

学完本讲你应当能够：

- 说清楚语义分割网络的输出和分类/检测有什么本质不同（逐像素类别图）。
- 读懂 icnet demo 里「逐像素类别读取 → 调色板查色 → 生成彩色掩码 → 与原图加权叠加 → 保存」整条后处理链路。
- 解释 demo 里两个最容易踩坑的细节：加权系数 `0.4 / 0.6` 的视觉效果，以及写入掩码时为什么要把调色板的 R/B 顺序反过来。
- 自己动手用一段纯 CPU 代码复现这条着色叠加流程。

## 2. 前置知识

### 三类视觉任务输出的对比

| 任务 | TPU 输出形态 | 后处理核心动作 |
|---|---|---|
| 分类（classify） | 一个长度为类别数的得分向量 | topk 排序（见 [u6-l1](u6-l1-classify-topk-postprocess.md)） |
| 目标检测（yolo） | 若干行 `[label, prob, x1, y1, x2, y2]` | 解析框 + 画框（见 [u6-l2](u6-l2-yolo-detect-postprocess.md)） |
| **语义分割（icnet）** | **一张与输入同分辨率的「逐像素类别图」** | **逐像素查调色板上色 + 与原图叠加** |

分类只给「一张图一个标签」，检测给「图里几个框」，而**分割给图里每一个像素都判一个类别**。所以分割的输出是一张稠密的二维标签图，后处理的核心不再是排序或解框，而是「把每个像素的类别号翻译成颜色，再画回去」。

### ICNet 与 Cityscapes

ICNet（Image Cascade Network）是一个面向实时场景的语义分割网络，本 demo 的模型在城市街景数据集 **Cityscapes** 上训练，共 **19 类**（road、sidewalk、building、……、bicycle）。这一点直接决定了后处理里调色板有 19 种颜色。

### Linux demo 的输出已经「好用」

回忆 [u5-l2](u5-l2-output-read-and-epmat-conversion.md)：裸机路线下，TPU 输出是 int16 定点、16 通道分组的 **epmat**，要靠 `epmat2nmat` 反量化才能用。而本讲的 icnet 是 **Linux demo**，运行库 `libeeptpu_pub` 已经把输出整理成了友好的 `EEPTPU_RESULT`：一个扁平的 `float` 缓冲区加一个 `shape[4]`。所以本讲的后处理直接 `float* p = (float*)results[0].data` 就能读，不需要手动反量化。这是 Linux 路线比裸机路线省心的地方。

### eepimg 的字节序约定（关键）

[u2-l2](u2-l2-eepimg-library.md) 讲过 eepimg 库：一段图像内存由 `image_bytes{w,h,c,data,layout}` 描述，默认 HWC 交错布局。本讲要反复用到两个事实：

- 用 `EEPIMG_PIXEL_BGR` 加载的图，`data` 里每个像素的字节顺序是 **B, G, R**（byte0=B）。
- `eepimg_save` 默认 `swapRB=true`，存盘前会把 BGR 翻回 RGB。

这两个事实是理解「R/B 反写」的钥匙，第 4.3 节会详细拆解。

## 3. 本讲源码地图

本讲只涉及两个文件，且重点是 `main.cpp` 里的后处理段：

| 文件 | 作用 |
|---|---|
| [sdk/demo/icnet/main.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp) | icnet demo 主程序：参数解析 → EEPTPU 初始化 → 读图/resize/forward → **后处理（着色叠加）** → 存盘。本讲聚焦其后处理段。 |
| [sdk/demo/common/eepimg_v0.2.6/eep_image.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.h) | 图像工具库头文件：`image_bytes` 结构与 `eepimg_make_image_bytes / copy_image / save` 等接口声明。 |

后处理用到 eepimg 的几个实现细节在 `eep_image.cpp` 里，讲到时会附带链接。

---

## 4. 核心概念与源码讲解

### 4.1 分割网络的输出形态：逐像素类别图

#### 4.1.1 概念说明

语义分割把「分类」做到了像素级：网络的最后一步通常是对每个空间位置在所有类别上算概率，再取最大值（argmax）。经过 argmax 之后，输出就不再是 `[C, H, W]` 的概率图，而变成 `[1, H, W]` 的**类别索引图**——每个像素一个整数，取值范围是 `0 ~ 类别数-1`。

icnet demo 里，编译器/TPU 已经替我们做完了 argmax，所以后处理拿到的 `results[0]` 就是这样一张「每个像素一个类别号」的图。我们要做的只是把它「读出来、上色」。

#### 4.1.2 核心流程

1. forward 完成，`results[0]` 里是一段 `float` 缓冲区，`shape[1..3] = (C, H, W)`。
2. 把 `data` 当成 `float*`，按行优先（H 外、W 内）逐像素走。
3. 每个像素读一个 float，转成 int 就是该像素的类别号 `readval`。
4. 后续用 `readval` 去调色板查色（见 4.2）。

注意循环只遍历了 `shape[2]`（H）和 `shape[3]`（W），每个位置只读一个 float——这说明 `shape[1]` 必须是 1（已经是 argmax 后的单通道类别图）。这是 demo 与编译器之间的一个隐含契约。

#### 4.1.3 源码精读

先看拿到结果后打印维度、并据此创建掩码图像的两行：

[sdk/demo/icnet/main.cpp:293-297](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L293-L297) —— 打印输出 `chw` 维度，并用输出的 `(W, H)` 创建一张 3 通道的空掩码图 `img_seg`。

再看逐像素读取类别号的循环：

[sdk/demo/icnet/main.cpp:300-306](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L300-L306) —— 把 `results[0].data` 当 `float*`，按 H、W 双层循环，每像素 `readval = (int)(*p_out++)` 即类别号。

> 解读：`p_out` 每读一个像素就 `++`，正好对应行优先的 H×W 排列；`shape[1]`（通道维）未被遍历，故其值应为 1。

#### 4.1.4 代码实践

**实践目标**：确认「输出是单通道类别图」这一契约，并发现一个潜在缺陷。

**操作步骤**：

1. 打开 [main.cpp:302-306](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L302-L306)，确认循环只用了 `shape[2]`、`shape[3]`。
2. 回看 [main.cpp:293](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L293) 的打印格式 `chw %d,%d,%d`，推断 `shape[1]` 在运行时应当打印为 `1`。
3. 思考：若某个像素的 `readval` 超过 18（例如浮点转 int 时出现 19、20），下游 `seg_color[readval]` 会怎样？

**需要观察的现象 / 预期结果**：`shape[1]` 应为 1；而 `seg_color` 只有 19 项（下标 0~18），`readval` 一旦 ≥19 就会越界读到相邻内存，颜色错误甚至崩溃。这是 argmax 输出范围必须与调色板长度严格一致的根源。

> 是否能在本机跑：需要板卡与 TPU 库才能产生真实 `results`，**端到端运行待本地验证**；但「读代码推断 shape[1]=1」与「越界风险」这两点纯靠源码即可确认。

#### 4.1.5 小练习与答案

**练习 1**：循环为什么只遍历 H、W，不遍历 `shape[1]`？
**答案**：因为输出已是 argmax 后的单通道类别图，`shape[1] == 1`，每个空间位置只有一个类别号，无需再沿通道维循环。

**练习 2**：如果模型输出的是未经 argmax 的 `[19, H, W]` 概率图，这段后处理还能直接用吗？
**答案**：不能。那样每个像素有 19 个概率值，`readval = (int)(*p_out++)` 只会取到第 0 个通道的概率再取整，类别号完全错误。需要先在通道维做 argmax，再进入本流程。

---

### 4.2 调色板查表：把类别号变成颜色

#### 4.2.1 概念说明

类别号本身只是一个 0~18 的整数，画出来是一片灰度，人眼看不出谁是谁。解决办法是一张**调色板（lookup table）**：用类别号做下标，查出一个 `(R, G, B)` 三元组。这样「road 永远是绿色、car 永远是蓝色」，肉眼一目了然。

#### 4.2.2 核心流程

1. 定义 `seg_color[19][3]`，第 `i` 行是第 `i` 类的颜色。
2. 对每个像素，用 `readval` 做行下标，取出 `seg_color[readval]` 的三个字节。
3. 把这三个字节写进掩码图 `img_seg` 对应像素（具体顺序见 4.3）。

#### 4.2.3 源码精读

调色板定义（注意每行三个数是 **R, G, B** 顺序，注释里点出了几个代表性类别）：

[sdk/demo/icnet/main.cpp:25-46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L25-L46) —— 19 类、每类 `{R,G,B}`。如 `{0,200,150}` 注释为 road/green（绿色分量最大，整体偏青绿），`{220,20,60}` 为 person/red，`{0,0,142}` 为 car/blue。

与调色板配套的类别名注释（让你知道每个下标对应哪个语义）：

[sdk/demo/icnet/main.cpp:307-313](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L307-L313) —— 0=road、1=sidewalk、…、11=person、13=car、…、18=bicycle，共 19 类，与 Cityscapes 对齐。

查表写入的三行（先看动作，字节序陷阱下一节展开）：

[sdk/demo/icnet/main.cpp:314-317](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L314-L317) —— 用 `seg_color[readval]` 的三个字节写入 `pseg`，然后 `offset += 3` 移到下一像素。

#### 4.2.4 代码实践

**实践目标**：理解调色板与类别号的对应关系，并体会「换数据集要同步改调色板」。

**操作步骤**：

1. 对照 [main.cpp:25-46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L25-L46) 与 [main.cpp:307-313](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L307-L313)，写出第 11 类（person）和第 13 类（car）的 RGB 值。
2. 假设你要把 demo 改成只有 5 类的自定义模型，列出需要修改的地方。

**预期结果**：person = `{220,20,60}`（红），car = `{0,0,142}`（蓝）。改 5 类时需要把 `seg_color` 改成 `[5][3]`，并保证模型 argmax 输出范围是 0~4（即类别数与调色板长度一致）。

#### 4.2.5 小练习与答案

**练习 1**：第一行 `{0,200,150}` 注释是 road/green，但三个数里 200 最大，为什么叫 green？
**答案**：三数顺序是 R=0、G=200、B=150，绿色分量最大、整体呈青绿色，故称 green。这正说明调色板写的是 RGB 顺序。

**练习 2**：如果想让「road」显示成纯红，应该把哪一行改成什么？
**答案**：把第 0 行 `{0,200,150}` 改成 `{255,0,0}`（R=255,G=0,B=0）即可。

---

### 4.3 彩色掩码生成与字节序陷阱（R/B 反写）

#### 4.3.1 概念说明

4.2 把类别号查成了颜色，本节把这些颜色组装成一张完整的彩色掩码图 `img_seg`，好让它能和原图叠加。

这里藏着一个**字节序陷阱**，也是本讲实践题的重点：调色板是用 **RGB** 顺序写的，但写入 `img_seg` 时代码却写成 `seg_color[readval][2]、[1]、[0]`——把 R 和 B 反过来了。要理解为什么，必须追踪整张图在内存里的字节顺序。

#### 4.3.2 核心流程（一次完整的字节序往返）

用「road = {R=0, G=200, B=150}」做例子，追踪颜色从调色板到最终 jpg 的完整旅程：

| 阶段 | 像素在内存里的字节序 | byte0 / byte1 / byte2 |
|---|---|---|
| 调色板定义 | RGB | R=0 / G=200 / B=150 |
| 写入 `img_seg`（反写 [2][1][0]） | **BGR** | B=150 / G=200 / R=0 |
| 与 `img_final`（BGR）逐字节叠加 | BGR | 通道对齐，正确混合 |
| `eepimg_save`（swapRB=true）翻回 | RGB（写进 jpg） | R=0 / G=200 / B=150 ✓ |

结论：写掩码时反写 R/B，是为了让 `img_seg` 也变成 **BGR**，从而和 BGR 的原图「逐字节对齐」地叠加；最后存盘时 `eepimg_save` 再统一翻回 RGB。

#### 4.3.3 源码精读

第一步——原图是用 BGR 加载的，这是字节序链路的起点：

[sdk/demo/icnet/main.cpp:250-257](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L250-L257) —— 3 通道输入时用 `EEPIMG_PIXEL_BGR` 加载，故 `img_orig`/`img_resized`/`img_final` 全是 BGR。

第二步——创建空掩码并把颜色按 BGR 写入：

[sdk/demo/icnet/main.cpp:314-317](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L314-L317) —— 写入顺序是 `[2]、[1]、[0]`，即把调色板的 B、G、R 依次放进 byte0、byte1、byte2，结果 `img_seg` 为 BGR。

辅助函数——创建一张清零的 3 通道图：

[sdk/demo/common/eepimg_v0.2.6/eep_image.cpp:640-648](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L640-L648) —— `eepimg_make_image_bytes` 用 `xcalloc` 分配并清零 `h*w*c` 字节，所以掩码里「未写满」的字节天然是 0。

存盘时翻回 RGB 的实现：

[sdk/demo/common/eepimg_v0.2.6/eep_image.cpp:390-420](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L390-L420) —— `eepimg_save` 默认 `swapRB=true`，会先 `eepimg_copy_image` 复制一份再调 `eepimg_rgbgr_image` 把每个像素的 byte0、byte2 互换，再写 jpg/png/bmp。

> 关键链路：[main.cpp:252](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L252)（BGR 加载）→ [main.cpp:314-316](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L314-L316)（反写成 BGR 掩码）→ [eep_image.cpp:658-681](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L658-L681)（`rgbgr_image` 存盘翻回 RGB）。三处字节序处理缺一不可。

#### 4.3.4 代码实践（对应实践题第二问）

**实践目标**：解释为什么写入 `pseg` 时要把 R/B 顺序反过来。

**操作步骤**：

1. 顺着 4.3.2 的表格，把 road `{0,200,150}` 走一遍，确认最终 jpg 里 road 是青绿色（正确）。
2. **反事实推演**：假设删掉反写，直接写 `seg_color[readval][0]、[1]、[2]`（把 R、G、B 原样写进 byte0、byte1、byte2），再让原图（黑像素）叠加、最后 `eepimg_save` 翻一次，推导 road 最终会变成什么颜色。
3. 判断：为什么会错？

**预期结果**：

- 不反写时，`img_seg` 的 byte0 装的是 R(=0)，但它会被当作 B 与原图的 B 通道混合；byte2 装的是 B(=150) 被当作 R。叠加后再被 `eepimg_save` 翻转一次，R、B 被「交叉污染」两次，road 会变成偏黄绿甚至偏红的错误颜色，而非青绿。
- 根因：调色板是 RGB，而下游（加载/叠加）全是 BGR，存盘只做一次 BGR→RGB 翻转。写掩码时反写一次，正是为了抵消这套 BGR 约定，让最终颜色正确。

> 本机可做：这段后处理是纯 CPU 逻辑，可以在 PC 上用伪造的类别图与伪造的 BGR 原图复现（见第 5 节综合实践），**无需 TPU** 即可验证字节序结论。

#### 4.3.5 小练习与答案

**练习 1**：如果把 [main.cpp:252](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L252) 改成 `EEPIMG_PIXEL_RGB` 加载，`pseg` 的写入顺序要不要改？
**答案**：要改成 `[0]、[1]、[2]`（不反写）。因为那样 `img_final` 变成 RGB，`img_seg` 也应是 RGB 才能逐字节对齐叠加；而 `eepimg_save` 的 swapRB 仍会处理存盘。

**练习 2**：如果调用 `eepimg_save(..., img_final, false)`（关掉 swapRB），结果会怎样？
**答案**：jpg 会把 BGR 当作 RGB 直接写，红蓝互换——road 会偏红/粉，car 会偏红而非蓝。

---

### 4.4 掩码与原图加权叠加保存

#### 4.4.1 概念说明

纯彩色掩码虽然类别清晰，但丢失了原始场景信息（看不出这条路、这栋楼长什么样）。常用做法是 **alpha 叠加（alpha blending）**：把掩码当作一层半透明彩色膜，盖在原图之上。这样既能看到「哪片像素属于哪类」，又能保留场景上下文。

#### 4.4.2 核心流程与数学

对每个字节（每个通道）做线性加权：

\[
y_i = (1-\alpha)\,x_i + \alpha\,s_i
\]

其中 \(x_i\) 是原图该字节，\(s_i\) 是掩码该字节，\(\alpha\) 是掩码权重。demo 取 \(\alpha = 0.6\)，即：

\[
\text{img\_final}[i] = \text{orig}[i]\times 0.4 + \text{seg}[i]\times 0.6
\]

流程：

1. `eepimg_copy_image(img_resized)` 复制一份 BGR 原图作为 `img_final`（写时复制，不污染原图）。
2. 逐字节加权混合 `img_final` 与 `img_seg`。
3. `eepimg_save` 存盘。

#### 4.4.3 源码精读

复制原图 + 逐字节叠加：

[sdk/demo/icnet/main.cpp:322-326](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L322-L326) —— 循环上界是 `img_final.c*h*w`，对每个字节做 `*0.4 + *0.6`。

> 两个隐含点：
> - **算术类型**：`unsigned char * 0.4` 会提升为 `double`，相加后再截断回 `unsigned char`（向下取整），所以不会整型溢出，但会有轻微截断误差。
> - **尺寸耦合**：循环用 `img_final` 的尺寸做上界、却按下标 `i` 同时访问 `img_seg`，这隐含要求 **两者 W×H×3 完全相等**。`img_final` 来自 `img_resized`（网络输入分辨率 `input_shape[3]×input_shape[2]`），`img_seg` 来自输出 `shape[3]×shape[2]`。因此本 demo 假设 **ICNet 输出空间分辨率 == 网络输入分辨率**；若二者不同，这个逐字节叠加会越界访问 `img_seg`，是潜在缺陷（待本地验证你的 bin 是否满足该假设）。

存盘与释放：

[sdk/demo/icnet/main.cpp:328-332](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L328-L332) —— `eepimg_save` 存成 `./icnet_result.jpg`（默认 swapRB 翻回 RGB），随后 `eepimg_free` 释放 `img_final` 与 `img_seg`，避免内存泄漏。

复制函数实现（确认是深拷贝）：

[sdk/demo/common/eepimg_v0.2.6/eep_image.cpp:650-656](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L650-L656) —— `eepimg_copy_image` 新分配一段同样大小的内存并 `memcpy`，所以改 `img_final` 不会影响 `img_resized`。

#### 4.4.4 代码实践（对应实践题第一问）

**实践目标**：体会 `0.4 / 0.6` 这组系数对可视化效果的影响。

**操作步骤**：

1. 当前 \(\alpha=0.6\)：掩码占 60%、原图占 40%。预想效果是「彩色分类区域很鲜艳，但原图场景还能隐约看见」，适合快速辨认类别边界。
2. 在 [main.cpp:325](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L325) 上做三个对比实验（改系数后重新编译运行）：
   - 改成 `*0.6 + *0.4`（\(\alpha=0.4\)）：原图主导，掩码变成一层淡彩。
   - 改成 `*0.0 + *1.0`（\(\alpha=1.0\)）：纯掩码，类别最清楚但完全失去场景。
   - 改成 `*1.0 + *0.0`（\(\alpha=0.0\)）：纯原图，等于没做分割。

**需要观察的现象 / 预期结果**：\(\alpha\) 越大，掩码越鲜艳、场景越淡；\(\alpha\) 越小，越像一张普通照片。`0.4/0.6` 是一个偏向「突出分割结果」的折中。端到端运行需板卡，**待本地验证**；但系数效果的定性判断无需上板即可得出。

#### 4.4.5 小练习与答案

**练习 1**：把 `0.6` 改成 `1.0`，输出会是什么？
**答案**：`img_final[i] = img_seg[i]`，输出就是纯彩色分割图，看不到任何原图场景。

**练习 2**：为什么循环用 `img_final.c*h*w` 作上界，而不是 `img_seg` 的尺寸？这样安全吗？
**答案**：代码隐含假设输出分辨率 == 输入分辨率，使二者字节数相同。若假设不成立，会越界访问 `img_seg`，不安全——这是阅读时应留意的耦合点。

---

## 5. 综合实践

**任务**：用一段**独立、纯 CPU** 的 C++ 代码，绕开 TPU，把本讲四个模块串起来——给定一张伪造的 `[1,1,4,4]` 类别图（float 数组）和一张伪造的 4×4 BGR 原图，产出加权叠加后的结果，并打印每个像素的 RGB 值，验证你的字节序理解是否正确。

**操作步骤**：

1. 定义一个长度为 16 的 `float class_map[16]`，模拟 argmax 输出（取值只用 0 和 13，分别代表 road 和 car）。
2. 用本讲的 `seg_color`（取前两行即可：road `{0,200,150}`、car `{0,0,142}`）。
3. 按 4.3 的规则把类别图上色成 BGR 的 `img_seg`。
4. 取一张全 0 的 BGR「原图」（这样叠加结果就等于 `img_seg*0.6`，便于核对）。
5. 按 4.4 做 `orig*0.4 + seg*0.6` 加权。
6. 打印结果前 2 个像素的三个字节，手算验证。

**参考答案（示例代码，可独立编译）**：

```cpp
// 示例代码：纯 CPU 复现 icnet 着色叠加，不依赖 TPU/eepimg
#include <cstdio>

// 调色板（RGB 顺序），只取本练习用到的两类
static unsigned char seg_color[2][3] = {
    {  0, 200, 150 },  // 0: road (RGB)
    {  0,   0, 142 },  // 13: car (RGB)
};

int main() {
    const int H = 4, W = 4;
    // 伪造的逐像素类别图（argmax 结果）
    float class_map[H*W] = {
        0, 0, 13, 13,
        0, 0, 13, 13,
        0, 0,  0, 13,
        0, 0,  0,  0,
    };
    unsigned char img_seg[H*W*3];   // 掩码（BGR）
    unsigned char img_orig[H*W*3];  // 原图（BGR），全 0
    for (int i = 0; i < H*W*3; i++) img_orig[i] = 0;

    // 模块 4.2 + 4.3：查表并按 BGR 写入（反写 [2][1][0]）
    for (int p = 0; p < H*W; p++) {
        int r = (int)class_map[p];        // 类别号（这里只用 0/13，需保证 seg_color 有对应行）
        // 注意：真实 demo 里类别号与调色板行号一一对应；
        // 这里为简化，把 13 映射到 seg_color 的第 1 行做演示。
        int row = (r == 0) ? 0 : 1;
        img_seg[p*3 + 0] = seg_color[row][2];  // B
        img_seg[p*3 + 1] = seg_color[row][1];  // G
        img_seg[p*3 + 2] = seg_color[row][0];  // R
    }

    // 模块 4.4：加权叠加 orig*0.4 + seg*0.6
    unsigned char img_final[H*W*3];
    for (int i = 0; i < H*W*3; i++)
        img_final[i] = (unsigned char)(img_orig[i]*0.4 + img_seg[i]*0.6);

    // 打印前 2 像素（存盘前是 BGR；存盘时 swapRB 会翻成 RGB）
    printf("pixel0 (road): BGR = %d,%d,%d\n", img_final[0], img_final[1], img_final[2]);
    printf("pixel2 (car) : BGR = %d,%d,%d\n", img_final[6], img_final[7], img_final[8]);
    return 0;
}
```

**预期结果与验证**：

- pixel0 是 road，原图为 0，故结果 = `seg*0.6 = (150*0.6, 200*0.6, 0)` ≈ `(90, 120, 0)`（BGR）。
- pixel2 是 car，结果 = `(142*0.6, 0, 0)` ≈ `(85, 0, 0)`（BGR）。
- 把 BGR 翻回 RGB（即存盘效果）：road → `(0,120,90)` 青绿 ✓，car → `(0,0,85)` 蓝 ✓。颜色语义正确，说明你对 R/B 反写的理解到位。

> 这段代码不调用 TPU 也不依赖 eepimg，可在任意装了 g++ 的 PC 上 `g++ demo.cpp -o demo && ./demo` 直接跑通，用来验证字节序与加权逻辑。端到端的 icnet demo 运行仍**待本地（板卡）验证**。

## 6. 本讲小结

- 语义分割的输出是**逐像素类别图**（已 argmax），后处理本质是「查调色板上色 + 与原图叠加」，与分类的 topk、检测的解框完全不同。
- icnet demo 用 `float* p_out` 逐像素读类别号 `readval`，再去 `seg_color[19][3]` 调色板查 RGB 颜色，组装出彩色掩码 `img_seg`。
- **字节序是最大坑点**：调色板是 RGB，但原图按 BGR 加载、按字节叠加，所以写掩码时必须反写 `[2][1][0]` 让 `img_seg` 也变 BGR，存盘时 `eepimg_save(swapRB=true)` 再统一翻回 RGB。
- 叠加用线性加权 `orig*0.4 + seg*0.6`：掩码权重 0.6 让分类区域鲜艳、场景淡化，便于辨认类别边界。
- 两个隐含契约值得留意：① argmax 输出范围必须 ≤ 调色板长度（否则 `seg_color[readval]` 越界）；② 输出空间分辨率必须 == 输入分辨率（否则逐字节叠加越界）。
- Linux 路线下 `results[0].data` 已是友好的 float 缓冲区，无需像裸机那样做 epmat 反量化——这是本讲后处理如此简短的原因。

## 7. 下一步学习建议

本讲结束了 U6「后处理算法」单元（分类 topk → 检测解框 → yolo3 软件层 → 分割着色）。接下来建议：

- **进入 U7 进阶 demo**：阅读 [u7-l1（multi_bins_test 多网络多核）](u7-l1-multi-bins-multicore.md) 与 [u7-l2（nntpu_test 多输入/npy/pack）](u7-l2-nntpu-multi-input-npy.md)，看同一套 EEPTPU API 如何在一个进程里跑多个网络、处理多输入。
- **对比裸机分割**：本讲是 Linux 路线，输出已是 float。可设想「若把 icnet 搬到裸机」，输出会变成 epmat，需要先用 [u5-l2](u5-l2-output-read-and-epmat-conversion.md) 的 `epmat2nmat` 反量化成 `ncnn::Mat`，再套本讲的调色板逻辑——这是把两条路线打通的好练习。
- **源码延伸阅读**：把 [main.cpp:289-333](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/icnet/main.cpp#L289-L333) 的整段后处理与 [eep_image.cpp 的 save/rgbgr_image](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L390-L420) 对照读一遍，亲手在纸上走一次「RGB→BGR→叠加→RGB」的字节序往返，就能彻底吃透本讲。
