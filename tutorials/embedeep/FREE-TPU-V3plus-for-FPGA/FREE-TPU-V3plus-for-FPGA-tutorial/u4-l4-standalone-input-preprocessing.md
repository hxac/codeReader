# 裸机输入预处理与硬件输入格式

## 1. 本讲目标

在 u4-l2 / u5-l1 里我们讲清了「写寄存器 → 启动 TPU → 轮询完成」的控制协议，但刻意回避了一个问题：**送进 TPU 的那张输入图，到底是什么样子的字节流？** ARM 不能把一张普通 RGB 图片直接丢给 TPU，TPU 也不吃浮点。本讲就专门拆解 `get_input_data()` 这个函数——它是裸机推理链路上「软件到硬件的最后一道翻译关」。

学完本讲你应该能够：

- 说清 `get_input_data` 的四步流水：双线性 resize → mean/norm/exp 定点化 → RGB→BGR 通道重排 → 32 字节步长打包。
- 推导单个像素被翻译成硬件 int16 定点数的数学公式，并手算一个具体值。
- 解释「每个像素明明只有 3 个通道（6 字节）有效数据，缓冲区却按每像素 32 字节推进」的根本原因（16 通道分组）。
- 自己算出一张 416×416×3 输入最终在 DDR 里占多少字节，并理解它为什么和 `config.h` 里的 `INPUTDATA_SIZE` 完全相等。

## 2. 前置知识

本讲需要以下概念，未曾接触的读者先建立直觉：

- **定点数（fixed-point）**：硬件不擅长浮点运算，常用「一个整数 + 一个公共的 2 的幂次缩放」来近似小数。例如把 `0.5` 表示为 `2048 × 2⁻¹²`，其中 `2048` 是存的整数、`12` 是约定的指数（记作 `exp`）。本讲的 `exp` 就是这个二进制小数点位置。
- **NCHW 与 HWC**：深度学习里张量形状常用 NCHW（批量 N、通道 C、高 H、宽 W）描述；而图像在内存里通常是 HWC 交错存储（`rgbrgbrgb...`，同一像素的各通道紧挨着）。本讲的输入图是 HWC，网络形状是 NCHW，两者要对应清楚。
- **像素序（RGB vs BGR）**：同一个像素，先存 R 还是先存 B，取决于训练时数据集的约定。darknet 等 YOLO 框架常用 BGR；摄像头给的是 RGB，于是中间必须做一次 R↔B 重排。这个概念在 u2-l2 的 eepimg 库里已经出现过。
- **DDR 物理地址即指针**：裸机没有操作系统，ARM 看到的 DDR 物理地址可以直接当指针解引用。`0x39000000` 这样的「魔法地址」就是某段 DDR，赋给一个 `unsigned char*` 即可读写（回顾 u1-l3 的两条 AXI 通路）。
- **通道分组（16-channel grouping）**：TPU 的计算与访存以「16 个通道」为一组并行处理。这一点贯穿输入打包（本讲）和输出读取（u5-l2 的 epmat），是理解所有「32 字节步长」现象的钥匙。

承接前置讲义：u4-l2 讲了 `EEPTPU_SA` 类与寄存器协议，u3-l3 讲了 `eepnet_config[]` 里存的 `mean/norm/exp`，本讲就看这些字段如何被 `get_input_data` 用起来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `sdk/standalone/src/main.cc` | 裸机主程序。本讲的主角 `get_input_data()`（L100–L188）在此；它的两次调用点（case `'1'` 单帧、case `'5'` 实时循环）也在此。 |
| `sdk/standalone/src/resize.h` | 声明两个双线性 resize 入口：`resize_bilinear_c1`（单通道）、`resize_bilinear_c3`（三通道）。 |
| `sdk/standalone/src/resize.cpp` | resize 的实现，源自 ncnn/OpenCV 风格的定点双线性插值，分「水平 → 垂直」两趟。 |
| `sdk/standalone/src/config.h` | TPU 寄存器/地址宏、`NET_SIZE`、`INPUTDATA_SIZE`（打包后的输入字节数）等编译期常量。 |
| `sdk/standalone/src/eeptpu/eeptpu_sa.h` | `EEPTPU_SA` 类与 `st_hwaddr_info{hwaddr, shape[4], exp}` 结构体定义——`get_input_data` 读的 `addr_in.shape`、`addr_in.exp`、`mean`、`norm` 都挂在这个类上。 |

## 4. 核心概念与源码讲解

`get_input_data` 把一张「摄像头分辨率（如 1280×720）的 HWC-RGB888 图」翻译成「网络输入分辨率（如 416×416）、int16 定点、BGR 序、16 通道分组」的硬件张量，并直接写到 TPU 的输入内存区（`hwbase1`）。整个过程可拆成四个最小模块。

### 4.1 双线性 resize：把任意分辨率拉到网络输入尺寸

#### 4.1.1 概念说明

网络输入分辨率是编译时就定死的（yolov4-tiny 是 416×416），但摄像头给的是 1280×720。两者不一致就必须 resize。本工程用**双线性插值（bilinear interpolation）**：目标图的每个像素，先映射回源图坐标（通常是带小数的位置），再由它周围最近的 4 个源像素按距离加权求和。

双线性 = 水平方向线性插值 + 垂直方向线性插值，两次一维插值的组合。

#### 4.1.2 核心流程

设缩放比 `scale_x = srcw / dstw`、`scale_y = srch / dsth`，对目标像素 `(dx, dy)`：

1. 用「半像素中心（half-pixel）」约定把目标中心映射回源坐标：
   \[ f_x = (dx + 0.5)\cdot scale_x - 0.5 \]
   取 `sx = floor(f_x)`，小数部分 `fx = f_x - sx`。
2. 水平权重（定点，放大 \(2^{11}=2048\) 倍）：`a0 = (1-fx)*2048`，`a1 = fx*2048`。
3. 对源行 `sx` 和 `sx+1` 各做一次水平插值，得到两条中间行 `rows0`、`rows1`。
4. 垂直方向同理：用 `fy`、`b0`、`b1` 把 `rows0`、`rows1` 合并成最终像素。
5. 边界处理：当 `sx < 0` 或 `sx >= srcw-1` 时，把 `sx` 钳到合法范围、令 `fx=0` 或 `1`，避免越界访问。

#### 4.1.3 源码精读

`get_input_data` 里只调用一行 resize，按通道数二选一：

[resize 调用 — sdk/standalone/src/main.cc:115-122](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L115-L122) —— 注意它把缩放结果写到一个**固定 DDR 地址** `0x39000000`（`inbuf_resized`），而不是 `malloc` 出来的堆内存；这是个中间缓冲区，resize 完后立刻被下一步的打包循环消费。

真正的实现在 `resize.cpp`，三通道版本 `resize_bilinear_c3` 的内部用「半像素映射 + 定点系数」预先算好每一列/每一行的偏移和权重：

[水平坐标与权重预算 — sdk/standalone/src/resize.cpp:215-239](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/resize.cpp#L215-L239) —— 注意三通道版的 `xofs[dx] = sx*3`（按**字节**偏移，因为源是 HWC 交错，每像素 3 字节）。

两趟插值的内层循环（先水平得到 `rows0/rows1`，再垂直合并）在：

[水平+垂直两趟插值 — sdk/standalone/src/resize.cpp:277-362](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/resize.cpp#L277-L362) —— 它还有一个「行复用」优化（`prev_sy1`）：相邻目标行若落在同样的两条源行上，就不重复做水平插值。

对外暴露的两个薄包装把 `srcstride`/`stride` 补齐为默认的紧凑步长：

[对外入口包装 — sdk/standalone/src/resize.cpp:367-375](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/resize.cpp#L367-L375) —— `c3` 版传 `srcw*3` 和 `w*3`（每行字节数），`c1` 版传 `srcw` 和 `w`。

> 术语小贴士：`INTER_RESIZE_COEF_BITS = 11` 表示插值系数放大到 \(2^{11}=2048\) 的定点；`SATURATE_CAST_SHORT` 是带四舍五入并钳到 `short` 范围的安全取整。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解半像素约定如何避免 resize 后整体平移半像素。
2. **步骤**：在草稿纸上对 2× 缩小（`srcw=8 → w=4`）手算 `dx=0` 时的 `f_x`：`(0+0.5)*2 - 0.5 = 0.5`，于是 `sx=0, fx=0.5`，即在源像素 0、1 正中间取值——这正是半像素约定的效果。
3. **观察**：若改用 `fx = dx*scale_x`（不加 0.5、不减 0.5），`dx=0` 会得到 `sx=0, fx=0`，整张图会偏向左上角。
4. **预期结果**：你能口头解释「半像素中心让缩放后的图像不发生整体位移」。
5. 待本地验证（可选）：把 `resize_bilinear_c3` 的 `(dx + 0.5)` 临时改成 `dx` 重新编译上板，理论上看到的检测框会整体偏移——但这属于破坏性实验，仅供理解，不要提交。

#### 4.1.5 小练习与答案

- **练习 1**：`resize_bilinear_c3` 的内层循环里 `rows1p[0..2]` 三个值分别代表什么？
  - **答案**：目标像素 `(dx, dy+1)` 所在源行的、同一个 `dx` 处的 B/G/R 三个通道的水平插值结果（三通道版一次处理一个像素的 3 个通道）。
- **练习 2**：当目标像素映射到 `sx = srcw-1`（最右列）时，源像素 `sx+1` 不存在，代码如何处理？
  - **答案**：在 [L221-230](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/resize.cpp#L221-L230) 把 `sx` 钳到 `srcw-2` 并令 `fx=1`，于是它只在 `sx`、`sx+1 = srcw-1` 这两个合法像素间插值，不会越界。

### 4.2 mean/norm/exp 定点化：把像素翻译成硬件 int16

#### 4.2.1 概念说明

resize 之后得到的是 `0~255` 的 `unsigned char`，但 TPU 要的是 **int16 定点数**。这步做三件事：

- **减均值（mean）**：对每个通道减去一个训练时统计的均值，把数据居中。
- **归一化（norm）**：乘以一个缩放系数，典型值 `1/255`，把数值压到 `0~1` 附近。
- **定点化（×2^exp）**：再乘以 \(2^{\text{exp}}\)，把浮点变成硬件能直接算的整数。

这三个系数都不是写死的，而是**从 `eepnet_config[]` 里读出来的**（回顾 u3-l3：mean/norm 以 IEEE754 float 存在数组末尾，`exp` 存在输入张量的 `st_hwaddr_info.exp` 字段里）。也就是说，它们和编译时 `setting.ini` 里 `--mean/--norm` 完全一致，保证软件预处理与模型期望对齐。

#### 4.2.2 核心流程

对单通道单像素，变换公式为：

\[ y = \mathrm{round}\bigl((x - \text{mean}) \cdot \text{norm} \cdot 2^{\text{exp}}\bigr) \]

得到整数 `y` 后，按 **int16 小端序**写入两个字节：低字节 `y & 0xff`，高字节 `(y >> 8) & 0xff`。

对 yolov4-tiny 的典型取值 `mean=0`、`norm=1/255`、`exp=12`：缩放系数为 \(\text{norm}\cdot 2^{\text{exp}} = \frac{1}{255}\cdot 4096 \approx 16.06\)。像素 `x` 的取值范围 `0~255` 对应 `y` 的范围 `0~4096`，远在 int16（最大 32767）之内，不会溢出。

#### 4.2.3 源码精读

`get_input_data` 开头把网络形状和系数从 `EEPTPU_SA` 对象里取出来：

[读取 shape/exp/mean/norm — sdk/standalone/src/main.cc:100-107](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L100-L107) —— `shape[1..3]` 分别是 C/H/W（NCHW 约定，`shape[0]` 是 N=1），`exp` 是定点指数，`mean`/`norm` 是 `vector<float>`。

定点缩放系数在这里一次性算好（`pow(2, net_in_exp)` 等价于 `1 << net_in_exp`）：

[打包准备：尺寸、清零、mul_val — sdk/standalone/src/main.cc:126-130](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L126-L130) —— `mul2[k] = norm[k]*mul_val` 把 norm 和定点合并成一个系数，循环里只做一次乘法。

`st_hwaddr_info` 结构体本身定义在类头里，`shape[4]` 是 NCHW、`exp` 是定点指数：

[st_hwaddr_info 结构体 — sdk/standalone/src/eeptpu/eeptpu_sa.h:30-35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L30-L35)；类成员 `addr_in`、`mean`、`norm` 在 [eeptpu_sa.h:87-89](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L87-L89)。

三通道情形里每个通道的变换（以 R 为例）：

```c
us = round( ((float)*psrc++ - mean[0]) * mul2[0]);   // 公式 (x-mean)*norm*2^exp
pdst[4] = us & 0xff;                                  // int16 低字节
pdst[5] = (us >> 8) & 0xff;                           // int16 高字节
```

> 术语小贴士：`round()` 是数学四舍五入（`math.h`），结果赋给 `short us`；当 `(x-mean)*norm*2^exp` 落在 int16 范围内时这是精确的，超出范围则按 C 标准是未定义行为，故编译时务必保证 `exp` 与 norm/mean 的组合不会让 `y` 越界。

#### 4.2.4 代码实践（手算型）

1. **目标**：亲手把一个像素值翻译成硬件 int16，验证对公式的理解。
2. **步骤**：取 yolov4-tiny 参数 `mean=0`、`norm=1/255`、`exp=12`，对像素 `x=128` 手算 `y`。
3. **计算**：\(y = \mathrm{round}((128-0)\cdot\frac{1}{255}\cdot 4096)=\mathrm{round}(2056.0)=2056=0\mathrm{x}0808\)。
4. **预期结果**：写入的两个字节是 `[0x08, 0x08]`（低字节在前）。
5. **延伸**：再算 `x=255`，应得 `round(255/255*4096)=4096=0x1000`，字节 `[0x00, 0x10]`——刚好是 exp=12 的满量程。

#### 4.2.5 小练习与答案

- **练习 1**：如果把 `exp` 从 12 改成 8（INT8 风格），`x=255` 时 `y` 是多少？会溢出吗？
  - **答案**：\(y=\mathrm{round}(255\cdot\frac{1}{255}\cdot 256)=256\)，远小于 32767，不会溢出；同时可见 `exp` 越小，定点数的分辨率越粗。
- **练习 2**：为什么要在软件里乘 `2^exp`，而不是直接把浮点 `(x-mean)*norm` 喂给 TPU？
  - **答案**：TPU 的计算单元和存储格式都是 int16 定点，它不接收浮点；`exp` 是软件与硬件之间约定的二进制小数点位置，乘上 `2^exp` 就是把数值搬到这个定点格式里。

### 4.3 RGB→BGR 通道重排：匹配模型的通道顺序

#### 4.3.1 概念说明

摄像头经 `RGB565toRGB888` 转换后给出的是 **RGB** 序（`rgbrgbrgb...`）。但 YOLO 这类模型在 darknet 里通常按 **BGR** 喂数据训练（或者说，编译出的 bin 期望 BGR）。所以打包时除了定点化，还要顺手把 R 与 B 的位置对调。

这步和 u2-l2 讲的 `eepimg_rgbgr_image`（R↔B 互换）是同一件事，只是这里它和定点化、分组打包耦合在同一个循环里完成，而不是单独一步。

#### 4.3.2 核心流程

源像素 3 字节依次是 `R, G, B`。目标要按 BGR 顺序写入 16 通道组的「通道 0、1、2」位置（下一节解释为何是这个布局）：

| 源字节 | 通道含义 | 写入目标位置（int16 两字节） |
| --- | --- | --- |
| `psrc[0]` | R | 通道 2 → `pdst[4], pdst[5]` |
| `psrc[1]` | G | 通道 1 → `pdst[2], pdst[3]` |
| `psrc[2]` | B | 通道 0 → `pdst[0], pdst[1]` |

也就是说：源里第 1 个字节（R）并没有写进目标的第 1、2 字节，而是跳到了第 5、6 字节——这正是「RGB 输入、BGR 输出」的体现。

#### 4.3.3 源码精读

工程里其实留了两套写法，靠 `#if 0 / #else` 切换。当前启用的是 `#else` 分支（注释明确写着 `src data is rgbrgbrgb....; dst use bgr`）：

[RGB→BGR 重排 + 定点化（启用分支） — sdk/standalone/src/main.cc:155-168](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L155-L168) —— 注意三次写入的目标偏移分别是 `pdst[4..5]`、`pdst[2..3]`、`pdst[0..1]`，正好把 R/G/B 重排成了 B/G/R。

被 `#if 0` 屏蔽的另一套写法假设「源也是 BGR」，于是按顺序连写 `pdst[0..5]` 再 `pdst += 26`（见 [L139-154](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L139-L154)）。两套写法都让指针最终落在 32 字节边界上（`6+26=32`，或直接 `+= 32`），区别只在通道顺序。

> 设计小贴士：把「重排」和「定点化」融在一个循环里，是为了对 `inbuf_resized` 只读一遍、对输出缓冲只写一遍，省一次内存来回——在裸机这种内存带宽敏感的环境里很划算。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：验证启用分支确实做了 R↔B 对调。
2. **步骤**：对照 [L155-168](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L155-L168)，列出 `*psrc`（第一个读出的源字节）被写到了哪个偏移。
3. **观察**：第一个读出的源字节（R）写到 `pdst[4..5]`，第三个读出的源字节（B）写到 `pdst[0..1]`，顺序确实反了。
4. **预期结果**：你能解释「如果模型其实是用 RGB 训练的，就该切回 `#if 1` 那套顺序写法，否则检测会对红/蓝目标系统性失准」。
5. 待本地验证：换网络时若发现「红色目标被识别成蓝色类别」，首先怀疑这里通道顺序与模型不匹配。

#### 4.3.5 小练习与答案

- **练习 1**：若模型其实期望 RGB，但忘了切换分支（仍用启用分支的 BGR 写法），会发生什么？
  - **答案**：R 和 B 通道被互换，TPU 看到的「红」其实是图里的「蓝」。对颜色敏感的类别（如交通灯、车辆）会出现系统性误检或置信度下降。
- **练习 2**：为什么 `mean` 和 `norm` 也是长度为 3 的 `vector<float>`，而不是单个标量？
  - **答案**：因为不同通道可以有不同的均值/归一化（例如按 ImageNet 的 R/G/B 各自均值），`mul2[0..2]` 分别对应 R/G/B 三个通道各自的系数。

### 4.4 32 字节步长打包：16 通道分组格式

#### 4.4.1 概念说明

这是本讲最核心、也最容易让人困惑的一点：一个像素明明只有 3 个通道、6 字节有效数据，输出缓冲为什么按**每像素 32 字节**推进、且总大小是 `net_in_w*net_in_h*32`？

根因是 **TPU 的 16 通道分组**：硬件的存储与计算通路一次并行处理 **16 个通道，每通道 2 字节（int16）**，一个完整分组就是 \(16 \times 2 = 32\) 字节。于是每个空间位置（每个像素）都必须占满一个 32 字节的「槽位」，槽位内的前若干通道放真实数据，剩下的通道填 0。这个格式和 u5-l2 将讲的输出 epmat 是同一套规则。

- 3 通道图：用掉槽位里的通道 0/1/2（共 6 字节），通道 3~15（26 字节）填 0。
- 1 通道灰度图：用掉通道 0（2 字节），通道 1~15（30 字节）填 0。

#### 4.4.2 核心流程

打包前先 `memset(inbuf, 0, *inlen)` 把整块缓冲清零（这样 padding 字节天然为 0），然后逐像素写入真实通道、并把指针推进一个 32 字节槽位：

```
总字节数 = net_in_w * net_in_h * 32          # 不论 C=3 还是 C=1
每个像素：写 C 个 int16(2C 字节) → 跳到下一个 32 字节槽位
```

单通道灰度的步长是「写 2 字节 + `pdst += 30`」，三通道是「在槽位前 6 字节散写 + `pdst += 32`」，两者都精确落在 32 字节边界。

#### 4.4.3 源码精读

缓冲总长和清零：

[打包总尺寸与清零 — sdk/standalone/src/main.cc:126-128](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L126-L128) —— `*inlen = net_in_w*net_in_h*32` 与通道数无关，正是 16 通道分组的结果。

三通道：在槽位 `[0..5]` 写完 BGR 后整槽跳过：

[三通道：写 6 字节后 pdst += 32 — sdk/standalone/src/main.cc:155-168](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L155-L168)（同 4.3.3 链接，重点看末尾 `pdst += 32`）。

单通道灰度：写 2 字节后 `pdst += 30`：

[单通道：写 2 字节后 pdst += 30 — sdk/standalone/src/main.cc:172-184](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L172-L184) —— `2 + 30 = 32`，同样是 16 通道槽位。

打包结果直接写进 TPU 输入内存区——两次调用都把 `inbuf` 传成 `eepsa.hwbase1`：

[调用点 case '1' — sdk/standalone/src/main.cc:376](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L376)、[调用点 case '5' 实时循环 — sdk/standalone/src/main.cc:561](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L561)。也就是说，`get_input_data` 一返回，数据就已经躺在 `BASEADDR1` 指向的输入段里了，紧接着的 `tpu_forward()`（见 u5-l1）只需写寄存器启动即可，不用再搬运。

这块缓冲的字节数正好对应 `config.h` 里的常量：

[INPUTDATA_SIZE 常量 — sdk/standalone/src/config.h:46-47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L46-L47) —— `INPUTDATA_SIZE = 5537792 = 416×416×32`，它就是 SD 卡上 `eepinput.mem` 的大小，也是 `case '4'` 里 `file_read("eepinput.mem", eepinput_addr, INPUTDATA_SIZE)` 一次读入的字节数（见 u8-l2）。

> 设计取舍：用 32 字节/像素换取的是**对齐、流式、零 gather-scatter** 的访存——TPU 可以一笔 32 字节突发读把一个空间位置的所有（已对齐到 16 通道的）数据拿走，硬件代价远小于「紧凑存 6 字节再让硬件拼凑」。内存用得多一点，但延迟和带宽友好，这在边缘推理里是更优解。

#### 4.4.4 代码实践（本讲主实践）

1. **目标**：推导「6 字节有效数据却 `pdst += 32`」的原因，并算出一张 416×416×3 输入的打包字节数。
2. **操作步骤**：
   - 阅读 [main.cc:155-168](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L155-L168)，确认三次写入只动了 `pdst[0..5]` 共 6 字节，随后 `pdst += 32`。
   - 回顾 [main.cc:127](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L127) 的 `memset`，理解 `pdst[6..31]` 这 26 字节为何是 0。
   - 用 `net_in_w*net_in_h*32` 计算 416×416×3 的总字节。
3. **需要观察的现象**：每个像素在缓冲里占一个 32 字节槽位，其中前 6 字节是 BGR 三个 int16，后 26 字节是 0 填充。
4. **预期结果**：
   - **为何 `pdst += 32`**：TPU 按 **16 通道 × 2 字节 = 32 字节** 分组访存。3 通道图只用掉 16 个通道槽里的 3 个（6 字节），其余 13 个（26 字节）必须存在且为 0，所以指针必须跳到下一个 32 字节槽位的起点。若只前进 6 字节，下一个像素的数据会挤进本像素的「通道 3」位置，TPU 就会把它们当成额外通道参与计算，结果全错。
   - **总字节**：\(416 \times 416 \times 32 = 173056 \times 32 = 5{,}537{,}792\) 字节，与 `config.h` 的 `INPUTDATA_SIZE = 5537792` 分毫不差。
5. 待本地验证：若你把网络换成输入不是 416×416（例如 224×224 的分类网），必须同步改 `config.h` 里的 `INPUTDATA_SIZE` 为 `224*224*32 = 1{,}611{,}264`，否则 `file_read` 读 `eepinput.mem` 时长度不匹配。

#### 4.4.5 小练习与答案

- **练习 1**：一张 416×416 的**单通道灰度**网络，打包后占多少字节？和三通道比如何？
  - **答案**：仍然是 \(416\times416\times32 = 5{,}537{,}792\) 字节——因为步长按「每像素一个 32 字节槽位」算，与通道数无关；灰度只是把更多字节（30 字节）用来填 0。
- **练习 2**：能否把 3 通道图紧凑存成每像素 6 字节（`pdst += 6`）来省内存？为什么工程不这么做？
  - **答案**：不能。TPU 的计算与访存以 16 通道（32 字节）为最小并行单元；紧凑存储会让相邻像素的通道错位、且地址不按 32 字节对齐，硬件要么报错要么把 padding 当真实通道算。牺牲一部分 DDR 换取对齐与流式访存，是 TPU 这类 dataflow 架构的通用取舍。

## 5. 综合实践

**任务：追踪一个像素从摄像头到 TPU 输入槽位的完整旅程，并定位它在 DDR 里的字节偏移。**

设网络为 yolov4-tiny（输入 416×416×3，mean=0、norm=1/255、exp=12），摄像头图为 1280×720 RGB888。请完成：

1. **resize 追踪**：目标像素 `(dx, dy) = (100, 100)`，按 `scale_x = 1280/416`、`scale_y = 720/416`，用半像素公式算出它对应的源坐标 `f_x`、`f_y`（不必算最终插值，只要算到 `sx/fx`）。
2. **定点化手算**：设该源像素经双线性插值后 R=200, G=100, B=50，算出三个通道写入的 int16 值（参考 4.2.4 的方法）。
3. **字节偏移定位**：该目标像素在打包缓冲里的 32 字节槽位起始地址偏移是多少？它的 B 通道 int16 写在槽位内的哪两个字节？（提示：槽位起点 = `(dy*416 + dx)*32`，B 在 `pdst[0..1]`。）
4. **通道重排复核**：确认 R 写在槽位的 `[4..5]`、G 在 `[2..3]`、B 在 `[0..1]`，与 [L155-168](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L155-L168) 一致。

**参考答案要点**：

1. `scale_x ≈ 3.077`、`scale_y ≈ 1.731`；`f_x = (100.5)*3.077 - 0.5 ≈ 308.7`，故 `sx=308, fx≈0.7`；`f_y = (100.5)*1.731 - 0.5 ≈ 173.4`，故 `sy=173, fy≈0.4`。
2. R：`round(200/255*4096) = round(3212.5) = 3212 = 0x0C8C`；G：`round(100/255*4096) = round(1606.3) = 1606 = 0x0646`；B：`round(50/255*4096) = round(803.1) = 803 = 0x0323`。
3. 槽位起点偏移 = `(100*416 + 100)*32 = 41700*32 = 1{,}334{,}400` 字节；B 的 int16（`0x0323`）写在槽位内偏移 `[0..1]`，即绝对偏移 `1{,}334{,}400` 与 `1{,}334{,}401`，内容为 `[0x23, 0x03]`（小端）。
4. R(`0x0C8C`) 在槽位 `[4..5]` = `[0x8C, 0x0C]`；G(`0x0646`) 在 `[2..3]` = `[0x46, 0x06]`；B(`0x0323`) 在 `[0..1]` = `[0x23, 0x03]`。槽位 `[6..31]` 为 0。

完成本任务后，你就把本讲的四个最小模块（resize、定点化、RGB→BGR、32 字节打包）串成了一条完整的数据流。

## 6. 本讲小结

- `get_input_data` 是软件到硬件的「翻译关」，分四步：**双线性 resize → mean/norm/exp 定点化 → RGB→BGR 重排 → 32 字节步长打包**。
- **resize** 用 ncnn 风格的定点双线性插值（半像素约定、系数放大 \(2^{11}\)），把摄像头分辨率拉到网络输入尺寸，结果写到固定地址 `0x39000000`。
- **定点化** 公式是 \(y=\mathrm{round}((x-\text{mean})\cdot\text{norm}\cdot 2^{\text{exp}})\)，系数全部来自 `eepnet_config[]`，保证与编译期 `--mean/--norm` 一致；结果按 int16 小端序写入。
- **RGB→BGR 重排** 把摄像头 RGB 调成模型期望的 BGR，靠 `pdst[4..5]/[2..3]/[0..1]` 的散写实现，和定点化共用一个循环。
- **32 字节步长** 源于 TPU 的 **16 通道 × 2 字节** 分组访存：每个空间位置独占一个 32 字节槽位，真实通道之外的字节由 `memset` 预清零。
- 一张 416×416×3 输入打包后恰好 \(416\times416\times32 = 5{,}537{,}792\) 字节，等于 `config.h` 的 `INPUTDATA_SIZE`，也等于 SD 卡上 `eepinput.mem` 的大小。

## 7. 下一步学习建议

- **u5-l1（tpu_forward 寄存器时序）**：本讲产出的输入数据躺在 `hwbase1`，下一讲就看 `tpu_forward()` 如何把 `hwbase1` 写进 `BASEADDR1_REG` 并启动 TPU。两者无缝衔接。
- **u5-l2（输出读取与 epmat→ncnn::Mat 转换）**：输出的 epmat 复用了本讲的「16 通道分组、32 字节步长、exp 定点」格式，只是方向反过来（从 int16 反量化回 float），强烈建议对照阅读 `epmat2nmat`。
- **u8-l2（SD 卡加载）**：本讲提到的 `INPUTDATA_SIZE`、`eepinput.mem` 在那里会被 `file_read` 读入；若你要换网络分辨率，u8-l2 是配套要改的另一处。
- **延伸阅读**：想理解 mean/norm/exp 是怎么进入 `eepnet_config[]` 的，回看 u3-l1 的编译参数与 u3-l3 的配置数组解析；想理解 16 通道分组的底层访存原因，可结合 u4-l3 的 `round_up` 对齐宏与 u1-l4 的两条 AXI 数据通路一起读。
