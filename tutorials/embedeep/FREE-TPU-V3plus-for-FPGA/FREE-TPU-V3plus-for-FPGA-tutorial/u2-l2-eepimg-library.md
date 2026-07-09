# eepimg 图像工具库：加载、缩放、绘制

## 1. 本讲目标

在上一篇 u2-l1 里，我们把 SDK 拆成了「编译器 / 运行库 / demo / standalone」四部分，并画出了一条主线：**框架模型 → eeptpu_compiler → `*.pub.bin` → 运行库加载推理**。但有一条贯穿所有 demo 的「暗线」我们还没讲——**图像数据从哪里来、长什么样、又怎么画回成结果图**。

不管是 classify、yolo、icnet 还是 nntpu_test，它们在做推理之前都要先「读一张图、resize 到网络输入尺寸」，推理之后又要「把检测框/类别文字画在原图上、存成结果图」。这些工作在 FREE-TPU V3+ 的 demo 里全部交给一个小而完整的图像工具库 **eepimg** 来完成。

本讲结束后，你应当能够：

- 说清 `image_bytes` 这个核心结构体里每个字段的含义，以及像素在内存里是怎么排布的。
- 区分「像素序（RGB/BGR/GRAY…）」与「布局（HWC/CHW）」这两个容易混淆的概念。
- 看懂并独立调用 `eepimg_load_image`、`eepimg_resize`、`eepimg_free`、`eepimg_draw_box`、`eepimg_draw_text`、`eepimg_save` 这一组接口。
- 理解为什么 demo 里读图和可视化「全都依赖」这个库，并为下一篇 u2-l3 的 EEPTPU API 实战打好图像侧的基础。

## 2. 前置知识

阅读本讲前，你需要先建立以下几个直觉（不熟悉的术语下面会逐一解释）：

- **位图（bitmap）的本质**：一张彩色图在内存里就是一段连续的字节数组。最常见的是每个像素用 3 个字节表示 R、G、B 三个颜色分量（0~255）。所以一张 \(w \times h\) 的 RGB 图，数据量就是 \(w \times h \times 3\) 字节。
- **通道（channel, c）**：颜色分量的个数。灰度图 c=1，真彩 RGB 图 c=3，带透明度的 RGBA 图 c=4。
- **像素序（pixel order）**：同是 3 通道，是「R、G、B」排列还是「B、G、R」排列？这关系到你把数据喂给网络时颜色对不对。darknet/opencv 默认 BGR，很多训练框架默认 RGB。
- **布局（layout）**：是「先排完一个像素再排下一个」（HWC，pixel-interleaved，交错存储），还是「先把整张图的 R 平面排完，再排 G 平面，再排 B 平面」（CHW，planar，平面存储）。深度学习框架常用 CHW，而图像文件、显示器常用 HWC。
- **stb 库**：一个极其流行的「单文件」C 图像库（stb_image 系列），只用 `#include` 一个头文件就能读写 jpg/png/bmp。eepimg 在底层正是调用了 stb 来干脏活。

> 关键直觉：**eepimg = stb（干底层编解码）+ 一层统一封装（统一结构体、统一像素序/布局转换、统一绘制接口）**。它的价值在于让所有 demo 不用各自处理「jpg 怎么读、BGR 怎么转、框怎么画」这些琐碎但容易出错的细节。

承接 u2-l1：上一篇我们说过 demo 会把图像字节直接交给 `tpu->eeptpu_set_input(img.data, ...)`，那个 `img.data` 就是本讲的产物。

## 3. 本讲源码地图

本讲只涉及 `sdk/demo/common/eepimg_v0.2.6/` 这个目录，它是被所有 demo 共享的「公共库」：

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `eep_image.h` | 库的对外头文件：定义 `image_bytes` 结构体、像素序/布局常量、所有函数声明 | ✅ 精读 |
| `eep_image.cpp` | 库的全部实现：加载、resize、crop、绘制、保存等 | ✅ 精读 |
| `stb_image.h` / `stb_image_resize.h` / `stb_image_write.h` | 第三方 stb 库，负责底层 jpg/png/bmp 解码、缩放、编码 | 仅了解其角色 |
| `eep_ascii.h` | 内置的 ASCII 点阵字库数据，`draw_text` 靠它把字符串渲染成像素 | 仅了解其角色 |

为了让讲解不悬空，我们还会引用 demo 里**真实调用**这些函数的代码（classify / yolo），让你看到「库接口 → 实际用法」的对应。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「数据结构 → 颜色/布局常量 → 加载与缩放 → 绘制与保存」的顺序推进。

### 4.1 image_bytes 数据结构

#### 4.1.1 概念说明

eepimg 用一个叫 `image_bytes` 的结构体来表示「一张图」。你可以把它理解成一句话：**「在 `data` 指向的那段内存里，存着一张 `w` 宽、`h` 高、`c` 通道、按 `layout` 布局排列的字节图」**。

为什么要自己定义这么个结构体？因为 stb 返回的是「裸指针 + 三个独立的 w/h/c 变量」，用起来要到处传 4 个参数；而 demo 里图像要被传来传去（读出来 → resize → 喂网络 → copy 一份 → 画框 → 保存），把它打包成一个结构体后，函数签名干净很多，也更不容易写错维度。

> 这是一种很常见的工程封装思路：**把「描述一块内存的元信息」和「内存本身」绑在一起**，类似 OpenCV 的 `cv::Mat` 或 ncnn 的 `ncnn::Mat`，只是 eepimg 这个版本极简，不搞引用计数、不搞设备迁移。

#### 4.1.2 核心流程

一个 `image_bytes` 的生命周期通常是：

```
[创建/加载]  eepimg_load_image / eepimg_make_image_bytes / eepimg_copy_image
     │              ── 分配 data = calloc(h*w*c) ──
     ▼
[使用]   读 .data / 改 .data / 喂给 set_input / resize / crop / 画框画字
     │
     ▼
[释放]   eepimg_free(im)   ── free(im.data) ──
```

三个要点：

1. `data` 由库内部用 `calloc` 分配，**调用者必须用 `eepimg_free` 释放**，否则内存泄漏。
2. 结构体里的 `w/h/c` 一旦由加载/创建函数设定，后续操作都按它们来寻址。
3. 默认布局是 `EEPIMG_LAYOUT_HWC`（交错存储）。

#### 4.1.3 源码精读

结构体定义在头文件里，非常简短：

[eep_image.h:20-35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.h#L20-L35) —— 定义 `st_image_bytes` 并 `typedef` 为 `image_bytes`：`w/h/c` 是整数维度，`data` 是 `unsigned char*` 指向像素字节，`layout` 记录是 HWC 还是 CHW。构造函数把所有字段清零、`layout` 默认置为 HWC。

注意 `data` 是 `unsigned char*`（每字节 8 位），所以一个像素的一个分量占 1 字节，取值范围 0~255。这正是 8 位图的标准表示。

真正给 `data` 分配内存的地方在实现文件里：

[eep_image.cpp:640-648](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L640-L648) —— `eepimg_make_image_bytes(w,h,c)`：填充维度，再用 `xcalloc(h*w*c, 1)` 分配并清零。所需字节数就是：

\[
\text{bytes} = w \times h \times c
\]

[eep_image.cpp:621-628](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L621-L628) —— `xcalloc` 是对标准 `calloc` 的封装：分配失败直接 `exit`（崩溃式错误处理，适合 demo 这种小程序），并额外 `memset` 清零。

[eep_image.cpp:650-656](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L650-L656) —— `eepimg_copy_image(p)`：先按值拷贝结构体（拿到 w/h/c/data 指针），**再单独 `calloc` 一块新内存并 `memcpy`**。这一步至关重要：copy 之后得到的是一块**独立**的内存，改它不会影响原图。后面你会看到 `draw_box`/`draw_text` 都先 copy 再画，正是为了不破坏原始输入。

[eep_image.cpp:326-333](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L326-L333) —— `eepimg_free(im)`：只要 `data` 非空就 `free`。

[eep_image.cpp:335-340](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L335-L340) —— `eepimg_empty(im)`：w/h/c 任一为 0 或 data 为 NULL，就认为「空」（加载失败的哨兵值）。demo 里到处用它来判断「读图有没有成功」，比如 classify 的 [main.cpp:106](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L106-L109)。

> ⚠️ 一个易错点：`eepimg_free` 接收的是**按值传递**的结构体，函数内把形参的 `im.data` 置 NULL，但**调用者手里的那个变量的 data 指针并不会被置空**。所以 free 之后不要再访问原结构体的 data——这是 C 风格「谁分配谁负责」的惯例。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手算清楚一张图要占多少字节，并理解 copy 的独立性。

**步骤**：

1. 打开 [eep_image.cpp:640-648](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L640-L648)，确认 `make_image_bytes` 分配大小是 `h*w*c`。
2. 假设有一张 classify 网络常用的输入 \(224 \times 224 \times 3\)，手算：

\[
224 \times 224 \times 3 = 150528 \text{ 字节} \approx 147 \text{ KB}
\]

3. 再读 [eep_image.cpp:650-656](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L650-L656) 的 `eepimg_copy_image`，回答：copy 后原图 `p.data` 和副本 `copy.data` 是不是同一块内存？

**需要观察的现象 / 预期结果**：

- 大小确实是 \(w \cdot h \cdot c\)。
- `copy_image` 里第 653 行新 `calloc` 了一块内存，所以原图与副本**互相独立**；这正是后面「画框前先 copy」安全的原因。

#### 4.1.5 小练习与答案

**练习 1**：一张 \(416 \times 416\) 的灰度图（yolo 有时用单通道），用 `eepimg_make_image_bytes` 分配，占多少字节？

**参考答案**：\(416 \times 416 \times 1 = 173056\) 字节 ≈ 169 KB。

**练习 2**：`eepimg_free(im)` 之后，访问 `im.data` 会发生什么？为什么 demo 里 free 完之后通常不再用这个变量？

**参考答案**：data 指向的堆内存已被释放，访问它属于「释放后使用（use-after-free）」，是未定义行为，可能读到垃圾或崩溃。demo 约定 free 即「这张图的生命周期结束」，所以之后不再引用。

---

### 4.2 像素序与布局常量

#### 4.2.1 概念说明

这是本讲最容易踩坑的两个概念，先彻底分清：

- **像素序（pixel order）**：回答「一个像素内的几个字节，谁在前谁在后」。`EEPIMG_PIXEL_RGB` 表示排成 R,G,B；`EEPIMG_PIXEL_BGR` 表示排成 B,G,R；`EEPIMG_PIXEL_GRAY` 表示只有 1 个灰度字节；`EEPIMG_PIXEL_RGBA/BGRA` 表示 4 通道带透明度。
- **布局（layout）**：回答「整张图的字节，是按像素交错排，还是按通道平面排」。`EEPIMG_LAYOUT_HWC`（默认）：`RGBRGBRGB…`，一个像素的几个分量紧挨着；`EEPIMG_LAYOUT_CHW`：先把所有 R 排完，再排所有 G，再排所有 B。

这两件事是**正交**的：你可以有「BGR + HWC」「RGB + CHW」等各种组合。eepimg 在加载时允许你同时指定像素序和布局。

> 为什么必须搞清这两个？因为**网络训练时用哪种，推理时就得喂哪种**。比如一个用 darknet 训练的 yolo，预处理期望 BGR；如果你喂了 RGB，颜色通道错位，模型会「看见」一张红蓝对调的图，结果直接崩坏。eepimg 的价值之一就是让你用一个常量参数声明清楚，库内部替你做转换。

#### 4.2.2 核心流程

像素序/布局的转换发生在**加载阶段**和**显式转换函数**里：

```
读文件(stb 给的是 RGB/灰度/RGBA)
   │
   ├── 按你指定的 pixel_order：原地重排成 BGR / 转 BGRA / 灰度转RGB …
   │
   └── (可选) 按 layout：HWC 保持原样；CHW 则把交错转成平面排列
```

关键转换函数 `eepimg_rgbgr_image` 做的是「3 通道下 R 与 B 互换」（RGB↔BGR），它是实现各种像素序切换的底层积木。

#### 4.2.3 源码精读

常量定义集中在头文件顶部：

[eep_image.h:10-15](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.h#L10-L15) —— 6 个像素序常量：`DFT(0)` 默认、`RGB(1)`、`BGR(2)`、`GRAY(3)`、`RGBA(4)`、`BGRA(5)`。

[eep_image.h:17-18](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.h#L17-L18) —— 2 个布局常量：`HWC(0)`、`CHW(1)`。

最核心的转换积木 R↔B 互换：

[eep_image.cpp:658-681](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L658-L681) —— `eepimg_rgbgr_image`：对 3 通道图，遍历每个像素，把第 0 字节和第 2 字节交换（即 R 和 B 互换），G 在中间不动；对 4 通道同理（跳过 alpha）。这就是「RGB 转 BGR」或反过来的全部秘密——一个字节交换。

> 顺带一提，这个函数在 `eepimg_save` 里也被复用：保存时默认 `swapRB=true`，会把内存里的图先 R↔B 翻一下再写文件。这一点 4.4 节会细讲，是「为什么保存出来的图颜色正常」的关键。

布局转换 HWC→CHW：

[eep_image.cpp:683-703](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L683-L703) —— `eepimg_hwc2chw`：外层循环遍历每个通道 c，把目标指针定位到「该通道的平面起点」`(c*h*w)`，源指针定位到「HWC 里该通道的起点」（每个像素偏移 c），然后逐像素搬运。直观地说，它把：

```
HWC:  R0 G0 B0  R1 G1 B1  R2 G2 B2 ...
CHW:  R0 R1 R2 ...   G0 G1 G2 ...   B0 B1 B2 ...
```

同样的逻辑也在带 layout 参数的 `eepimg_load_image` 里出现：

[eep_image.cpp:203-233](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L203-L233) —— 先按 pixel_order 加载成 HWC，若要 CHW 则再做一次上述平面重排，并把 `layout` 字段标记为 `EEPIMG_LAYOUT_CHW`。**注意：它返回的是一块新内存，调用者同样要 free。**

实际 demo 里的真实用法——classify 根据网络输入通道数选择像素序：

[sdk/demo/classify/main.cpp:97-105](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L97-L105) —— `input_shape[1]`（即通道数）为 3 时用 `EEPIMG_PIXEL_BGR`，为 1 时用 `EEPIMG_PIXEL_GRAY`。注释里还贴心提示：如果用 darknet 训练的模型可以用 RGB（这里默认走 BGR）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：彻底看清 RGB↔BGR 在字节层面到底动了什么。

**步骤**：

1. 打开 [eep_image.cpp:93-102](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L93-L102)：当源图是 3 通道（stb 给的 RGB）、你要求 `EEPIMG_PIXEL_BGR` 时，它先 `memcpy` 原样拷贝，再调 `eepimg_rgbgr_image` 翻转 R/B。
2. 再对照 [eep_image.cpp:658-670](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L658-L670) 的交换循环。

**需要观察的现象 / 预期结果**：

- 对一个像素 `pdst[0]=R, pdst[1]=G, pdst[2]=B`，翻转后变成 `pdst[0]=B, pdst[1]=G, pdst[2]=R`，中间的 G 完全不动。所以 BGR 只是「RGB 的 0 号和 2 号字节对调」，颜色信息一字节不丢，只是顺序变了。

#### 4.2.5 小练习与答案

**练习 1**：`EEPIMG_PIXEL_RGB` 和 `EEPIMG_PIXEL_BGR` 的数据，谁大？通道数相同吗？

**参考答案**：一样大，都是 3 通道，字节数都是 \(w \cdot h \cdot 3\)。区别**仅在于每个像素内 R/B 的排列顺序**，与大小、通道数无关。

**练习 2**：如果网络要 CHW 输入，但你只调用了「不带 layout 参数」的 `eepimg_load_image`，结果会怎样？怎么修正？

**参考答案**：不带 layout 参数的版本默认返回 HWC（[eep_image.cpp:203-206](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L203-L206)），直接喂给要 CHW 的网络会导致通道错位、结果错误。修正：要么调用带 `layout` 参数的重载并传 `EEPIMG_LAYOUT_CHW`，要么加载后再用 `eepimg_hwc2chw` 转一次（参考 nntpu_test 的 [main.cpp:582](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/nntpu_test/main.cpp#L582-L589)）。

---

### 4.3 加载、缩放与释放

#### 4.3.1 概念说明

这是 eepimg 用得最多的一组接口，也是「读图喂网络」主链路的核心。一句话概括：**把磁盘上任意分辨率的 jpg/png/bmp 读进来，缩放成网络要的固定输入尺寸，再交出 `data` 指针。**

为什么 demo 里「resize」这一步几乎必不可少？因为：

- 磁盘上的照片分辨率千奇百怪（1080p、4K…）。
- 而 TPU 网络的输入尺寸是**固定的**（编译时就定死，存在 `tpu->input_shape` 里，比如 224×224 或 416×416）。
- 所以必须把任意输入图缩放到 `input_shape` 指定的大小，才能喂进网络。

eepimg 提供了一个重载家族 `eepimg_load_image`，从「最简」到「最全」覆盖了不同需求：

| 重载 | 参数 | 适合场景 |
| --- | --- | --- |
| 1 | `(filename, pixel_order)` | 只读、按像素序加载 |
| 2 | `(filename, pixel_order, layout)` | 读 + 指定 HWC/CHW |
| 3 | `(filename, pixel_order, resize_w, resize_h, crop_scale)` | 读 + 中心裁剪 + 缩放一步到位 |

外加独立函数 `eepimg_resize`（单独缩放）、`eepimg_crop`（单独裁剪）、`eepimg_crop_resize`（对已有 image_bytes 做裁剪+缩放）。

#### 4.3.2 核心流程

最常用的「读 + 缩放」两步走（classify/yolo 都这么用）：

```
img_orig = eepimg_load_image(path, EEPIMG_PIXEL_BGR);   // 1. 原图加载
if (eepimg_empty(img_orig)) 报错退出;
img = eepimg_resize(img_orig, input_w, input_h);         // 2. 缩放到网络输入
tpu->eeptpu_set_input(img.data, img.c, img.h, img.w);    // 3. 喂网络
eepimg_free(img);                                        // 4. 用完即释放
eepimg_free(img_orig);                                   //    原图也要释放（画完框之后）
```

注意一个**资源管理要点**：加载和 resize 都会 `calloc` 新内存，所以 `img_orig` 和 `img` 是两块独立的内存，**两个都要分别 free**。demo 里常见 bug 就是漏 free 导致内存泄漏。

#### 4.3.3 源码精读

最基础的加载函数（带 pixel_order）：

[eep_image.cpp:48-201](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L48-L201) —— `eepimg_load_image(filename, pixel_order)`。核心逻辑：

1. 第 52-54 行：按像素序决定 stb 的 `desired_channels`（要灰度就传 `STBI_grey`），调用 `stbi_load` 真正解码文件，拿到 w/h/c 和 `data`。
2. 第 55-62 行：解码失败（文件不存在/格式不支持）时打印原因并返回一个「空 image_bytes」（w=h=c=0）作为哨兵。
3. 第 67-196 行：一个大的 `if/else` 阶梯，按「源图实际通道数 c」×「你要求的 pixel_order」做各种转换。例如：
   - 源是灰度（c==1）却要求 RGB：把每个灰度值复制 3 份成 RGB（[L80-L92](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L80-L92)）。
   - 源是 3 通道、要求 BGR：拷贝后 R↔B 翻转（[L93-L102](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L93-L102)）。
   - 源是 4 通道 RGBA、要求 RGB：按 alpha 做白背景混合 \(\text{out}=(1-\alpha)\cdot 255+\alpha\cdot \text{src}\)（[L141-L154](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L141-L154)）。
4. 第 199 行：`free(data)` 释放 stb 的临时缓冲，返回新结构体。

最全的「读+裁剪+缩放」重载：

[eep_image.cpp:235-274](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L235-L274) —— `eepimg_load_image(filename, pixel_order, resize_w, resize_h, crop_scale)`。流程：先按 pixel_order 加载原图 → 若 `crop_scale!=1` 则在中心裁一个正方形（短边×crop_scale）→ 再 resize 到目标尺寸；若不裁剪则直接 resize。返回最终图，并沿途 free 掉中间临时图。nntpu_test 用的就是这个一站式版本（[main.cpp:558](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/nntpu_test/main.cpp#L558-L566)）。

独立的 resize：

[eep_image.cpp:342-351](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L342-L351) —— `eepimg_resize(im, w, h)`：如果尺寸已经一致就 copy 一份返回（保持「返回新内存」的契约）；否则 `make_image_bytes` 新建目标，调用 stb 的 `stbir_resize_uint8`（双线性插值）做缩放。它**不释放输入 im**，调用者要自己管。

裁剪：

[eep_image.cpp:360-388](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L360-L388) —— `eepimg_crop(im, x0, y0, w, h)`：从 `(x0,y0)` 起取 `w×h` 子图。注意它对越界做了**裁剪式修正**：若 `x0+w` 超出图宽，就把 w 缩小到 `im.w-x0`（[L367-L370](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L367-L370)），保证不越界读。这块逐行 `memcpy` 每一行。

实际 demo 的两步读图（classify）：

[sdk/demo/classify/main.cpp:97-114](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L97-L114) —— 完整呈现「按通道数选像素序 → empty 检查 → resize 到 `input_shape[3]×input_shape[2]`（即 w×h）→ set_input → free 缩放图」的标准链路。`input_shape` 是个 4 元素数组（NCHW），`[1]`=通道、`[2]`=高、`[3]`=宽。

> 说明：`input_shape` 的维度顺序是 **NCHW**（batch/通道/高/宽），所以宽在下标 3、高在下标 2。eepimg 的 `resize(im, w, h)` 参数顺序是「宽、高」，因此传入 `(input_shape[3], input_shape[2])` 正好对应「宽、高」。这种「数组下标 vs 函数参数顺序」的对照是初学者最容易看错的地方，务必留意。

#### 4.3.4 代码实践（源码阅读型 + 伪代码）

**目标**：把 classify 的读图链路翻译成可独立理解的伪代码，并验证资源释放是否成对。

**步骤**：

1. 通读 [classify/main.cpp:92-122](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L92-L122) 的 `eeptpu_write_input`。
2. 数一数：函数里 `calloc`（隐藏在 load/resize 里）了几块内存？分别在哪里 free？有没有泄漏？
3. 写出等价伪代码（示例代码，非项目原有）：

```cpp
// 示例代码：读图喂网络的典型两步链路
image_bytes img_orig = eepimg_load_image(path, EEPIMG_PIXEL_BGR); // 第 1 块内存
if (eepimg_empty(img_orig)) return img_orig;                       // 读失败哨兵

image_bytes img = eepimg_resize(img_orig, in_w, in_h);             // 第 2 块内存
tpu->eeptpu_set_input(img.data, img.c, img.h, img.w, 0);
eepimg_free(img);                                                  // 释放第 2 块
// 注意：img_orig 在这里还不释放，因为它要返回给上层「画结果图」用
return img_orig;                                                   // 调用者最终在 main 里 free 它
```

**需要观察的现象 / 预期结果**：

- `img_orig` 和 `img` 是两块独立内存，函数内 free 掉了 `img`，`img_orig` 交还调用者（在 main 里 free，见 [classify/main.cpp:159](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L154-L159)）。**没有泄漏**。
- 错误路径（set_input 失败）也要记得 free `img_orig` 再返回（[L115-L119](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L115-L119)）——否则失败时泄漏。

#### 4.3.5 小练习与答案

**练习 1**：`eepimg_resize` 在「目标尺寸 == 原尺寸」时为什么返回 `eepimg_copy_image(im)` 而不是直接返回 `im`？

**参考答案**：为了保持「返回的永远是一块**新分配、可独立 free** 的内存」这一契约（[eep_image.cpp:342-344](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L342-L344)）。如果直接返回 `im`，调用者 free 返回值时就会把原图也释放掉，造成二次释放。

**练习 2**：假设网络输入是 416×416×3（yolo），磁盘上的照片是 1920×1080。请描述数据从磁盘到 `set_input` 经历了哪几次内存分配、每次多大。

**参考答案**：① `stbi_load` 临时解码 \(1920\cdot1080\cdot3\approx6.2\text{MB}\)（函数内 free）；② `eepimg_load_image` 拷出原图同大小 ≈6.2MB（即 `img_orig`）；③ `eepimg_resize` 新建 \(416\cdot416\cdot3\approx507\text{KB}\)（即 `img`）。三块中 ① 立即释放，②③ 由调用者管理。

---

### 4.4 绘制与保存

#### 4.4.1 概念说明

推理拿到结果后，最直观的展示方式是「把检测框、类别文字画在原图上，存成一张结果图」。eepimg 提供了三个可视化接口：

- `eepimg_draw_box`：在图上画一个矩形框（指定两个对角点 + BGR 颜色 + 线宽）。
- `eepimg_draw_text`：在指定坐标写一串 ASCII 文字（用内置点阵字库 `eep_ascii.h`）。
- `eepimg_save`：把 image_bytes 写成 jpg/png/bmp 文件。

这三个函数共同的设计哲学：**都不修改输入图，而是先 `eepimg_copy_image` 复制一份，在副本上画，再返回副本。** 这样原图始终干净，你可以反复在原图基础上画不同的东西。

> 嵌入式环境通常没有 freetype 之类的字体引擎，eepimg 因此自带了一个精简的 ASCII 点阵字库（`eep_ascii.h` 里的 `eepascii[]` 数组），只支持可见 ASCII 字符（0x20 起步），不支持中文。这是「够用就好」的工程取舍。

#### 4.4.2 核心流程

yolo demo 的画框画字保存链路（典型可视化流程）：

```
image = eepimg_copy_image(img);                       // 1. 复制原图，得到可改的副本
for (每个检测框 obj):
    image = eepimg_draw_text(image, x, y, "dog 98.0%"); // 2a. 画类别文字（返回新副本）
    image = eepimg_draw_box(image, x1,y1,x2,y2, 0,255,0); // 2b. 画绿框（返回新副本）
eepimg_save("./objdet.jpg", image);                   // 3. 保存结果
eepimg_free(image);                                   // 4. 释放
```

⚠️ 关键点：`draw_text`/`draw_box` **返回的是新副本**，所以每画一次都要用 `image = draw_xxx(image, ...)` 重新接收返回值，并且旧的 `image` 指针会被「悬空」（其内存无人释放 → 泄漏）。yolo 实际代码就是这么写的（见下方源码精读），这是一个**潜在内存泄漏点**，初学者要意识到。

#### 4.4.3 源码精读

画框：

[eep_image.cpp:422-485](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L422-L485) —— `eepimg_draw_box(img, x1,y1,x2,y2, b,g,r, linewidth)`：

- 第 424 行：先 `copy_image`，在副本上画。
- 第 427-439 行：对坐标做**越界裁剪**（负数归 0、超界归 w-1/h-1），并保证 x1≤x2、y1≤y2。这让你传任意坐标都不会越界崩溃。
- 第 441-442 行：算 `row_bytes = w*c`（一行字节数）；若图是单通道，颜色取 `(b+g+r)/3` 当灰度。
- 第 447-482 行：画矩形的四条边——两条水平边（沿 x 方向填 `b,g,r`）和两条垂直边（沿 y 方向，按 `row_bytes` 跨行）。`linewidth` 控制线宽，通过把同一条边画 n 遍（每次偏移 1 像素）实现加粗。
- 颜色参数顺序是 **b, g, r**，与 BGR 像素序一致——所以 demo 里画绿框传 `0, 255, 0`（[yolo/main.cpp:262-265](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L262-L265)）。

画文字：

[eep_image.cpp:554-605](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L554-L605) —— `eepimg_draw_text(im, x, y, text)`：

- 第 561 行：根据图像高度 `im.h` 自动选一个合适的字号（`get_suitable_char_height`，字高 ≈ 图高的 5%，见 [L487-L519](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L487-L519)）——所以图越大字越大。
- 第 565 行：在 `eepascii[]` 字库里定位到当前字号的字符偏移表。
- 第 572-601 行：逐字符渲染。每个字符从字库读出 `char_w × char_h` 的点阵（`char_data`），按灰度值拷贝到目标图的 `(gx, gy)` 位置，并把灰度复制到所有通道（所以文字在彩色图上呈白/灰色）。`gx += cw` 让下一个字符接在右边。
- 同样先 `copy_image`（第 570 行），返回新副本。

保存：

[eep_image.cpp:390-420](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L390-L420) —— `eepimg_save(filename, im, swapRB=true)`：

- 第 393-394 行：用 `strrchr` 找文件名最后的 `.`，没扩展名直接返回 -1。
- 第 396-402 行：**关键**——默认 `swapRB=true`，会先 copy 一份并 `eepimg_rgbgr_image` 翻转 R/B。为什么？因为 demo 内部图通常是 **BGR**，但 jpg/png 文件标准是 **RGB**，存盘前要翻回去，否则存出来的图红蓝对调。
- 第 404-415 行：按扩展名分发到 stb 的三个写函数：`.bmp`→`stbi_write_bmp`、`.png`→`stbi_write_png`、`.jpg`→`stbi_write_jpg`（质量 85）。
- 第 418 行：若做了 swapRB 的副本则 free 它。

yolo demo 的真实可视化主循环：

[sdk/demo/yolo/main.cpp:249-269](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L249-L269) —— `draw_objects`：先 copy 原图，再对每个检测目标先 `draw_text`（写 `类别名 + 置信度%`）再 `draw_box`（绿框 `0,255,0`，线宽 1），每次都用 `image = ...` 重新接收返回值。最终结果在 [main.cpp:496](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L496-L498) 处用 `eepimg_save("./objdet.jpg", final_image)` 存盘。

#### 4.4.4 代码实践（伪代码，承接本讲总任务）

**目标**：基于 `eep_image.h` 的接口，写一段伪代码完成本讲指定的实践——**加载一张 jpg、按指定宽高 resize、画一个绿色矩形框、保存为 result.jpg**。

**操作步骤**（示例代码，非项目原有，不能直接编译，仅演示 API 用法）：

```cpp
// 示例代码：eepimg 典型可视化伪代码
#include "eep_image.h"

int main() {
    // 1. 加载一张 jpg，按 BGR 像素序（demo 惯例）
    image_bytes im = eepimg_load_image((char*)"input.jpg", EEPIMG_PIXEL_BGR);
    if (eepimg_empty(im)) { printf("load fail\n"); return -1; }

    // 2. resize 到指定宽高（比如 416x416）
    image_bytes resized = eepimg_resize(im, 416, 416);
    eepimg_free(im);                 // 原图用完释放

    // 3. 在中心画一个绿色矩形框：颜色按 BGR 顺序 (b=0, g=255, r=0)
    //    draw_box 返回新副本，用 resized 接收
    resized = eepimg_draw_box(resized, 100, 100, 300, 300, 0, 255, 0, 2);

    // 4. 保存为 result.jpg（save 默认 swapRB=true，会把 BGR 翻回 RGB 存盘）
    int ret = eepimg_save("./result.jpg", resized);
    printf("save ret = %d\n", ret);

    // 5. 释放
    eepimg_free(resized);
    return 0;
}
```

**需要观察的现象 / 预期结果**：

- 加载成功时 `im.data` 非空、`im.c==3`（jpg 是彩色）。
- `resize` 后 `resized.w==416 && resized.h==416`。
- `draw_box` 返回值与传入的 `resized` **不是同一块内存**（旧的会泄漏，这里为简化没处理；正式代码应避免这种「覆盖式」调用，或对旧指针做 free）。
- `eepimg_save` 返回非 0（stb 写成功返回 1）；生成的 `result.jpg` 颜色正常（因为 save 默认翻了 R/B），能看到一个绿色框。

> 待本地验证：以上为伪代码，实际能否运行取决于编译环境（需要把 `eep_image.cpp` + 三个 stb 头 + `eep_ascii.h` 一起编进来）。建议在 Linux demo 的交叉编译环境（见 u2-l3 的 compile.sh）里搭一个最小测试程序验证。

#### 4.4.5 小练习与答案

**练习 1**：如果不传第 3 个参数，`eepimg_save` 默认 `swapRB=true`。假设你已经在内存里持有的是一张 RGB 图（不是 BGR），直接调用 `eepimg_save("./out.jpg", im)`，存出来的图颜色对吗？怎么修？

**参考答案**：不对。内存里是 RGB，save 又默认翻一次 R/B，存出来变成 BGR（红蓝对调）。修正：显式传 `eepimg_save("./out.jpg", im, false)` 关闭翻转（见 [eep_image.cpp:390](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L390-L402)）。

**练习 2**：`eepimg_draw_text` 写中文字符串会怎样？为什么？

**参考答案**：无法正确显示。`eep_ascii.h` 只内置了可见 ASCII 字符（从 0x20 起）的点阵（见 [eep_image.cpp:574](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L574) `text[i]-0x20` 的寻址），中文是多字节编码，下标会越界或乱码。要显示中文需换用带 ttf 字体引擎的方案。

**练习 3**：yolo 的 `draw_objects` 里每次 `image = eepimg_draw_box(image, ...)` 都覆盖了旧 `image` 指针。这会造成什么问题？在一个 Object 数组有 N 个目标时，大约泄漏多少块内存？

**参考答案**：`draw_box`/`draw_text` 内部会 `copy_image` 新分配一块内存，旧的那块没人 free，造成泄漏。每个目标画 2 次（text + box），N 个目标约泄漏 \(2N\) 块（再加上最开始 copy 的 1 块和 text 之后的副本链）。对一次性跑一张图的 demo 影响不大，但若在循环里实时跑（如摄像头流）会持续泄漏——这是阅读 demo 时要意识到的「demo 简化」之处。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「迷你可视化流水线」的**源码阅读 + 伪代码设计**任务（无需上板，重在理解数据流）。

**背景**：你要给一张 jpg 做一个最简单的处理——读进来，缩成 256×256，在正中央画一个红字标签 `"EEP"` 并画一个红框，存成 png。请结合本讲学到的接口完成下面三件事。

**任务 A：画「红色」框和红字**

eepimg 的颜色参数顺序是 **b, g, r**。红色对应 RGB(255,0,0)，请写出调用 `eepimg_draw_box` 时 `b,g,r` 三个实参应该填什么。

**任务 B：补全伪代码**（示例代码，非项目原有）

```cpp
// 示例代码
image_bytes im = eepimg_load_image((char*)"in.jpg", EEPIMG_PIXEL_BGR);
image_bytes r  = eepimg_resize(im, 256, 256);
eepimg_free(im);
r = eepimg_draw_text(r, /*x=*/____, /*y=*/____, (char*)"EEP");   // 在中央
r = eepimg_draw_box(r, 80, 80, 176, 176, /*b=*/____, /*g=*/____, /*r=*/____, 2);
int ret = eepimg_save("./out.png", r);   // 注意：png 同样支持
eepimg_free(r);
```

请填空：文字坐标想放在「中央偏上」应该填什么（提示：图宽 256，字宽可用 `eepimg_get_text_width` 估算，但这里可直接给一个合理坐标如 (108, 20)）？红框的 b,g,r 填什么？保存成 png 时 `swapRB` 仍是默认 true，会不会有问题？

**任务 C：解释一处「坑」**

上述伪代码里 `r = eepimg_draw_text(r, ...)` 和 `r = eepimg_draw_box(r, ...)` 连续覆盖 `r`，参考 [eep_image.cpp:570](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L570-L604) 和 [eep_image.cpp:424](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/common/eepimg_v0.2.6/eep_image.cpp#L422-L485)，说明丢失了哪些指针、为什么 demo 可以容忍、而实时视频流里不能容忍。

**参考答案**：

- 任务 A：红色 RGB(255,0,0)，eepimg 参数是 (b,g,r)，故填 **b=0, g=0, r=255**。
- 任务 B：文字坐标 (108, 20)（大致居中偏上，可接受任何合理值）；红框 `b=0, g=0, r=255`；png 保存时 `swapRB=true` 仍会把 BGR 翻成 RGB，**没有问题**（save 按扩展名 `.png` 走 `stbi_write_png`，颜色处理对 bmp/png/jpg 一致）。
- 任务 C：每次 draw 都 `copy_image` 新分配一块，旧 `r` 指针被覆盖后无人 free，泄漏 2 块（text 之前那块 + box 之前那块）。demo 只跑一次就退出，进程结束 OS 回收内存，故可容忍；实时视频流是长循环，泄漏会不断累积直到 OOM，必须改成「在固定缓冲上原地画」或「及时 free 旧指针」。

## 6. 本讲小结

- `image_bytes` 是 eepimg 的核心结构体：`w/h/c/data/layout` 五个字段描述一块 `unsigned char` 像素内存，`data` 由库 `calloc` 分配，**必须用 `eepimg_free` 成对释放**。
- 「像素序」（RGB/BGR/GRAY/RGBA/BGRA）和「布局」（HWC/CHW）是两个**正交**概念：前者管一个像素内分量顺序，后者管整图是交错还是平面排列；网络要什么就喂什么，否则颜色/通道错位。
- 读图主链路：`eepimg_load_image`（按像素序加载，内部调 stb）→ `eepimg_resize`（缩放到网络 `input_shape`）→ 喂 `set_input`；`input_shape` 是 NCHW，故传给 resize 的是 `(input_shape[3], input_shape[2])` 即 (宽, 高)。
- 可视化三件套 `draw_box/draw_text/save` 都遵循「先 copy 再改、返回新副本」的设计；颜色参数顺序是 **b,g,r**；`save` 默认 `swapRB=true` 把内部 BGR 翻成 RGB 存盘，这是结果图颜色正常的关键。
- 底层脏活（jpg/png/bmp 解码、双线性缩放、字库渲染）分别由 stb 系列头文件和 `eep_ascii.h` 承担，eepimg 是它们之上的一层统一封装。
- 阅读要点：注意「返回新副本」带来的内存管理（覆盖式调用会泄漏，demo 可容忍、实时流不可容忍），以及坐标越界由库内部裁剪修正。

## 7. 下一步学习建议

本讲把「图像侧」的输入输出打通了：你已知道一张图怎么被读成 `image_bytes`、缩放成网络输入、又怎么把结果画回去存盘。下一篇 **u2-l3《EEPTPU 运行库 API 与 classify demo》** 会把中间被我们「略过」的那一步补上——`tpu->eeptpu_set_input(img.data, ...)` 之后，`forward` 是怎么驱动的、结果 `EEPTPU_RESULT` 怎么读出来。

建议接着做：

1. 读 **u2-l3**，重点对照 [classify/main.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp) 的 `main` 函数，看 `input_shape`、`EEPTPU_RESULT` 这些结构和本讲的 `image_bytes` 是如何衔接的。
2. 顺带翻一眼 [yolo/main.cpp:249-269](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L249-L269) 的 `draw_objects`，它会成为 u6-l2（目标检测后处理）的主菜，本讲已为它打好了「画框画字」的基础。
3. 如果你对底层编解码好奇，可以单独去看 `stb_image.h`（开源、注释极好），理解 jpg 解码原理；但这对使用 eepimg 并非必需。

---
