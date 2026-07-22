# 框架补丁总览与图像加载/归一化

## 1. 本讲目标

本讲是「Vitis AI 推理框架补丁」单元的第一篇。在 u4 我们把模型编译成了 DPU 可执行的 `.xmodel`，在 u5 我们把 DPU 硬件部署到了 KV260 板上。但要让板载 C++ 程序真的「读得对图、喂得对数、算得对框」，还差最后一块：**改造 Vitis AI 推理框架本身的源码**，使其与训练侧的数据格式、数值范围、IoU 度量保持一致。

学完本讲你应当能够：

1. 说出 `xview3_yolov8_v3.5.patch` 修改了 Vitis AI 的哪四个源文件，以及每个文件负责解决哪一类「训推不一致」问题。
2. 看懂一段 unified diff（`---`/`+++`/`@@`/`+`/`-`），能从补丁里提取出被改动和新增的代码。
3. 解释为什么 `cv::imread` 必须加 `cv::IMREAD_UNCHANGED` 才能正确读取 SAR 的 TIFF 芯片。
4. 读懂 `image_preprocess` 中 SAR 三通道线性归一化与「转 signed int8」的两步，并说清推理输入为什么用有符号 8 位定点。
5. 画出从「磁盘上的 int16 SAR TIFF」到「喂给 DPU 的 signed int8 张量」的完整数据流。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个本讲会反复用到的概念。

**补丁（patch）与 unified diff。** 补丁是一个纯文本文件，记录「旧代码 → 新代码」的差异，由 `git diff` 或 `git apply` 识别。它不是完整源码，而是「在某文件的第几行删什么、加什么」。一个补丁可以同时改多个文件。本讲的 `xview3_yolov8_v3.5.patch` 就改了 Vitis AI 源码树里的四个文件。读补丁时关注三类行：以 `-` 开头是被删除的旧行，以 `+` 开头是新增的行，以空格开头是未改动的上下文。`@@ -151,7 +151,7 @@` 这种叫 hunk 头，冒号后的两个数字分别是旧、新文件的起始行号和行数。

**Vitis AI 是什么。** Vitis AI 是 Xilinx/AMD 提供的深度学习推理框架，负责把编译好的 `.xmodel` 加载到 DPU 上跑、并做前/后处理。它本身**已经内置了 YOLOv8 的支持**（`yolov8_imp.cpp` 做预处理、`yolov8.cpp` 做后处理、`apply_nms.cpp` 做 NMS），还有一个精度评测用的 demo 框架（`demo_accuracy.hpp`）。本项目的做法不是另起炉灶，而是**在现有实现上打补丁**，把它从「普通自然图像 RGB 检测」改造为「SAR 三通道船舶检测」。

**`cv::imread` 的隐式转换。** OpenCV 的 `cv::imread` 默认会把图像读成 8 位无符号、3 通道 BGR 的 `cv::Mat`——这是为普通照片设计的。如果输入是 16 位的多波段 TIFF（SAR 芯片正是如此），默认读取会**悄悄把 16 位降成 8 位、甚至读出空图**，从而丢失 dB 数值。`cv::IMREAD_UNCHANGED` 标志告诉 OpenCV「按文件原样读，不要改位深、不要改通道数」。

**signed int8 与 DPU。** KV260 上的 DPU（`DPUCZDX8G`）是一个**有符号 8 位定点（signed int8）**加速器：权重和激活都按 `[-128, 127]` 的整数运算。因此喂给 DPU 的输入张量也必须是 signed int8，否则数值对不上、精度直接崩。这一点会在 4.3 详细展开。

**训推一致性暗线。** 回顾 u1-l3 提出的三条贯穿全链的一致性暗线：归一化、IoU、输入尺寸。本讲的 `image_preprocess` 正是「归一化」暗线在 C++ 推理侧的落地，它必须与训练侧 u3-l2 的 `normalize()` 用同一套公式；而 `apply_nms.cpp`（本讲只做总览，细节在 u6-l2）则是「IoU」暗线的落地。

## 3. 本讲源码地图

本讲只涉及两个文件，但其中一个（补丁）会牵出 Vitis AI 的四个目标文件。

| 文件 | 作用 | 本讲用法 |
|------|------|----------|
| `framework/vitis_ai/README.md` | 说明补丁的安装方式与四项改动的概要 | 给出「四项改动」的总览与归一化代码摘要 |
| `framework/vitis_ai/xview3_yolov8_v3.5.patch` | 针对 Vitis AI 3.5 源码的 unified diff，改四个文件 | 本讲的核心精读对象，逐 hunk 拆解 |

补丁触及的四个 Vitis AI 目标文件（补丁内路径，均在 `src/vai_library/` 下）：

| 目标文件 | 所属模块 | 改动要点 | 本讲/后续讲 |
|----------|----------|----------|-------------|
| `benchmark/include/vitis/ai/demo_accuracy.hpp` | 精度 demo 框架 | `ReadImagesThread` 的 `cv::imread` 加 `IMREAD_UNCHANGED` | **本讲 4.2 精读** |
| `yolov8/src/yolov8_imp.cpp` | YOLOv8 预处理 | `image_preprocess` 改为 SAR 归一化 + signed int8 | **本讲 4.3 精读** |
| `xnnpp/src/apply_nms.cpp` | NMS 后处理 | 新增 `cal_piou2` 与 `PIOU2_NMS` 环境变量 | 本讲总览，**u6-l2 精读** |
| `xnnpp/src/yolov8.cpp` | YOLOv8 后处理解码 | 优化 `yolov8_post_process`、支持 P2 头 | 本讲总览，**u6-l3 精读** |

## 4. 核心概念与源码讲解

### 4.1 patch 四文件总览

#### 4.1.1 概念说明

Vitis AI 框架是为「通用视觉模型 + RGB 自然图像」设计的。把它用到本项目——SAR 三通道（VV/VH/bathymetry）、极小点目标、训练侧用了 PIoU2 损失、网络加了 P2 高分辨率头——会在四个环节出现「框架默认行为 ≠ 我们训练时的行为」的不一致。补丁的作用就是把这四处一一校正回来，使**推理时模型看到的数据与训练时一致**。

这四个文件分别对应四类不一致：

1. **数据格式不一致**：框架默认按普通图像读图，读不了 SAR 的 16 位 TIFF → 改 `demo_accuracy.hpp`。
2. **数值范围不一致**：框架默认做 ImageNet 风格的 resize/归一化，而我们训练时做的是 SAR 线性归一化 → 改 `yolov8_imp.cpp` 的 `image_preprocess`。
3. **IoU 度量不一致**：框架默认 NMS 用普通 IoU/DIoU，而训练框回归用的是 PIoU2 → 改 `apply_nms.cpp`。
4. **网络结构不一致**：框架默认后处理只解 P3/P4/P5 三个尺度的头，而我们加了 P2 头，多一个尺度 → 改 `yolov8.cpp` 的 `yolov8_post_process`。

一句话：**补丁 = 把训练侧的「数据格式、归一化、IoU、网络头」四件事在 C++ 推理侧复刻一遍。**

#### 4.1.2 核心流程

补丁的使用流程（来自 README）：

```
下载 Vitis AI 3.5 源码
   → cd vitis_ai
   → git apply ../framework/vitis_ai/xview3_yolov8_v3.5.patch
   → 用 PetaLinux 交叉编译框架（详见 Vitis AI 文档）
   → 把编译产物传到 KV260 的 /usr/local/
```

补丁内部的逻辑分层：

```
xview3_yolov8_v3.5.patch
├── hunk #1  demo_accuracy.hpp   → 读图（输入端，4.2）
├── hunk #2  apply_nms.cpp        → NMS 度量（输出端，u6-l2）
├── hunk #3  yolov8.cpp           → 后处理解码（输出端，u6-l3）
└── hunk #4  yolov8_imp.cpp       → 预处理归一化（输入端，4.3）
```

注意两个「输入端」改动（#1、#4）决定了**喂进 DPU 之前**数据长什么样；两个「输出端」改动（#2、#3）决定了**DPU 出来之后**怎么解码和去重。本讲聚焦输入端（#1、#4）。

#### 4.1.3 源码精读

README 开头的安装说明，给出 `git apply` 这一步是补丁生效的唯一入口：

[framework/vitis_ai/README.md:5-11](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L5-L11) —— 用 `git apply` 把补丁贴到 Vitis AI 源码，再用 PetaLinux 交叉编译部署到 KV260 的 `/usr/local/`。

README 用一个编号列表列出全部四项改动，是本讲最好的导航：

[framework/vitis_ai/README.md:13-32](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L13-L32) —— 四项框架改动总览：①TIFF 加载 ②SAR 归一化 ③PIoU2 NMS ④P2 后处理加速 27×。

补丁本身用 `diff --git` 行声明它修改的目标文件，每个文件对应一段。例如第一段：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:1-4](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L1-L4) —— 补丁第一段声明要改 `demo_accuracy.hpp`，`---`/`+++` 给出旧/新路径，`@@ -151,7 +151,7 @@` 表示从该文件第 151 行起的一段。

四个文件的 `diff --git` 头分别位于补丁的这些位置，可据此快速跳转到对应 hunk：

- `demo_accuracy.hpp`：[patch:1-4](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L1-L4)
- `apply_nms.cpp`：[patch:14-17](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L14-L17)
- `yolov8.cpp`：[patch:115-118](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L115-L118)
- `yolov8_imp.cpp`：[patch:446-449](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L446-L449)

为后续两讲留个伏笔：`apply_nms.cpp` 里新增了环境变量开关 `DEF_ENV_PARAM(PIOU2_NMS, "0")`（[patch:29](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L29)），运行时设 `PIOU2_NMS=1` 即可把 NMS 切换到 PIoU2；`yolov8.cpp` 里加了 `__TIC__(YOLOV8_DECODING)`/`__TOC__` 计时标记（[patch:263](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L263) 与 [patch:384](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L384)），用来度量那个 27× 的解码加速。两者细节留给 u6-l2、u6-l3。

#### 4.1.4 代码实践

**实践目标：** 学会从补丁里快速定位「四个文件分别改了什么」，而不必通读 535 行。

**操作步骤：**

1. 打开 `framework/vitis_ai/xview3_yolov8_v3.5.patch`。
2. 只看每行以 `diff --git` 开头的四行，记下四个目标文件名。
3. 对每个文件，跳到它的 `@@ ... @@` hunk 头，只扫 `+` 行（新增逻辑）。
4. 对照 README 第 13–32 行的编号列表，把「四项改动」与「四个文件」一一对应。

**需要观察的现象：** 你会发现 README 的四项说明与补丁的四个文件是**精确一一对应**的——这不是巧合，而是补丁设计时的刻意分工。

**预期结果：** 得到一张与 4.1.3 中表格一致的「文件 ↔ 改动」映射表。

**待本地验证：** 若你已下载 Vitis AI 3.5 源码，可执行 `git apply --check ../framework/vitis_ai/xview3_yolov8_v3.5.patch`（注意 `--check` 只做干跑检查、不真改文件），确认补丁能干净贴合当前源码版本；若报错通常说明 Vitis AI 版本与 3.5 不符。

#### 4.1.5 小练习与答案

**练习 1：** 补丁为什么写成「打在 Vitis AI 源码上」的 diff，而不是直接提供改好的四个文件？

**参考答案：** 因为 Vitis AI 是第三方大型框架，文件路径、版权头、版本演进都在上游；提供 diff 既能让改动一目了然、便于审查与升级跟踪，又避免把整个框架代码塞进本仓库。`git apply` 还能在版本略有漂移时给出明确的冲突提示。

**练习 2：** 补丁里有两处「输入端」改动和两处「输出端」改动，请各举一个并说明判断依据。

**参考答案：** 输入端：`demo_accuracy.hpp`（读图）、`yolov8_imp.cpp`（预处理），都发生在数据**进入 DPU 之前**；输出端：`yolov8.cpp`（解码 DPU 输出张量）、`apply_nms.cpp`（对解码出的框做 NMS），都发生在数据**离开 DPU 之后**。

---

### 4.2 C++ TIFF 加载（demo_accuracy.hpp）

#### 4.2.1 概念说明

板载推理读的图，就是 u2 用 `generate_xview3.py` 切出来的 SAR 芯片：每个芯片是一个 3 波段、`int16` 的 GeoTIFF，像素值是 dB（VV/VH 约 `[-50, 20]`）和水深米值（bathymetry 约 `[-6000, 2000]`），见 u2-l1、u2-l2。

问题在于：Vitis AI 的精度 demo 用 `ReadImagesThread` 这个线程逐行读一个「图片路径列表」文件，每行调一次 `cv::imread(line)`。这个**不带标志**的 `cv::imread` 默认会把图像转成 8 位 BGR。对一个 16 位 SAR TIFF，这会导致两种灾难：

- 位深被压缩：`int16` 的 dB 值被截断/重映射到 `uint8`，数值范围彻底失真；
- 甚至直接读空：某些多波段 TIFF 让默认解码器返回空 `cv::Mat`，触发补丁里那句 `if (image.empty())` 的「cannot read image」警告并跳过。

这和 u3-l2 训练侧遇到的是**同一个坑**——训练侧的解决方法是 Python 里加 `cv2.IMREAD_UNCHANGED`；推理侧的解决方法就是这里的 C++ `cv::IMREAD_UNCHANGED`。这正是归一化暗线在「数据加载」这一环的体现。

#### 4.2.2 核心流程

```
test-image-list.txt（每行一个 chip 路径）
        │  getline(fs, line)
        ▼
cv::imread(line, cv::IMREAD_UNCHANGED)   ← 补丁加的标志
        │  按原样读：保留 int16、保留 3 波段
        ▼
cv::Mat image（非空、位深与通道数正确）
        │  若 image.empty() 则告警并跳过
        ▼
送入后续 image_preprocess（4.3）
```

关键：`IMREAD_UNCHANGED` 让 OpenCV「不擅自转换」，从而把磁盘上 `int16` 的真实 dB 值原封不动交给下一步归一化。

#### 4.2.3 源码精读

整个改动只有一行，但它是整条推理数据流的入口。补丁 hunk：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:5-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L5-L13) —— `ReadImagesThread` 的读图循环，`@@ -151,7 +151,7 @@` 指明这是原文件第 151 行附近。

被改的那一行（`-` 旧 / `+` 新）：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:9-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L9-L13) —— 把 `cv::imread(line)` 改成 `cv::imread(line, cv::IMREAD_UNCHANGED)`，紧接着的 `if (image.empty())` 兜底跳过读失败的图。

README 对这一项的说明：

[framework/vitis_ai/README.md:15-15](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L15-L15) —— 明确要求「**每一个** `cv::imread` 调用都要加 `cv::IMREAD_UNCHANGED`」。

注意 README 强调「every」：框架里可能不止一处 `cv::imread`，凡是读 SAR TIFF 的入口都要改，漏一处就会有一路数据悄悄失真——和 u3-l2 训练侧「须覆盖每一处 imread」是同一条教训。

#### 4.2.4 代码实践

**实践目标：** 理解 `cv::IMREAD_UNCHANGED` 到底解决了什么问题，并能用一个小实验复现「不加标志」的失败现象。

**操作步骤：**

1. 阅读补丁 [patch:5-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L5-L13)，确认改动只有「加一个标志」这一处。
2. 阅读下文「示例代码」，它用 Python（OpenCV 与 C++ 行为一致）模拟两种读法。
3. 若本地有一个 `int16` 的 SAR TIFF（或用 numpy 造一个 `int16` 单波段假图存成 TIFF），分别用 `cv2.imread(p)` 与 `cv2.imread(p, cv2.IMREAD_UNCHANGED)` 读取，打印 `dtype` 与像素范围。

**示例代码（非项目原有代码）：**

```python
import cv2, numpy as np
# 假装这是 int16 的 SAR 芯片
arr = np.array([[-50, 0, 20], [-6000, 10, 5]], dtype=np.int16)
cv2.imwrite("/tmp/fake_sar.tif", arr)

a = cv2.imread("/tmp/fake_sar.tif")                     # 默认：可能降位/读空
b = cv2.imread("/tmp/fake_sar.tif", cv2.IMREAD_UNCHANGED)  # 原样
print(a.dtype, a.ravel()[:3])   # 期望观察：uint8 且数值被压缩/错位
print(b.dtype, b.ravel()[:3])   # 期望观察：int16 且保留原始 dB 值
```

**需要观察的现象：** 不加标志时 `dtype` 变成 `uint8`、原始负 dB 值（如 `-50`）丢失或被截断；加 `IMREAD_UNCHANGED` 时保留 `int16` 与原始数值。

**预期结果：** 直观证明「不加标志 → SAR 数值失真」，从而理解补丁这一行的必要性。

**待本地验证：** 具体读取行为取决于本地 OpenCV 版本与 TIFF 编解码器，若手头没有真实 SAR TIFF，可只用上面的 `int16` 假图验证「位深保留」这一核心点。

#### 4.2.5 小练习与答案

**练习 1：** 假如忘了加 `IMREAD_UNCHANGED`，程序会立刻崩溃吗？为什么这个问题很「隐蔽」？

**参考答案：** 通常不会立刻崩溃。`cv::imread` 会返回一个看似正常的 `cv::Mat`（或空矩阵走 `continue` 跳过），程序继续跑，只是像素数值已失真。结果是「能出预测、但精度莫名下降」，这种静默错误最难排查，所以 README 才强调每一处 `imread` 都不能漏。

**练习 2：** 这一行的修复，和 u3-l2 训练侧的哪一处改动是「同一件事」？

**参考答案：** 和 u3-l2 里给训练侧 `cv2.imread` 加 `IMREAD_UNCHANGED` 完全对应——两侧读的都是同一批 `int16` SAR TIFF，必须用同一种读法，才能保证训练和推理看到的像素值一致（归一化暗线的加载环节）。

---

### 4.3 image_preprocess 归一化与 signed int8（yolov8_imp.cpp）

#### 4.3.1 概念说明

图读进来之后、喂给 DPU 之前，要做「预处理」。Vitis AI 默认的 YOLOv8 预处理是「resize + letterbox 灰边填充 +（隐式的）ImageNet 归一化」——又是为自然图像设计的。本项目的预处理要换成两件事：

**第一件：SAR 三通道线性归一化。** 这就是 u3-l2 训练侧 `normalize()` 的 C++ 版本，公式完全相同——把每个波段按各自的最小值和量程线性映射到 `[0, 1]`，再 clip。为什么必须一致？因为模型在训练时学到的权重，对应的就是「归一化到 `[0,1]`」的输入分布；推理若用别的归一化（比如 ImageNet mean/std），等于给模型喂了一分布外的输入，精度立刻崩。这是归一化暗线的核心环节。

**第二件：转 signed int8。** 归一化到 `[0,1]` 后，还要转成 DPU 需要的有符号 8 位定点 `[-128, 127]`。原因是 DPU（`DPUCZDX8G`）是 signed int8 加速器；u4 量化时确定的输入量化参数，也是按 signed int8 来标定的。所以推理输入张量必须是 `CV_8S`。

一个自然的疑问：u3-l2 训练侧归一化到 `uint8 [0,255]`，这里却归一化到 signed int8 `[-128,127]`，两侧不一致吗？其实**归一化到 `[0,1]` 这一步是完全一致的**，差异只在最后一步的「整数表示」：

- 训练侧：`[0,1] → uint8 [0,255]`（为了零改动复用 Ultralytics 的 Mosaic/HSV/翻转等默认增强，这些增强都假设 `[0,255]`）；
- 推理侧：`[0,1] → signed int8 [-128,127]`（DPU 原生要求）。

而 `signed = unsigned − 128`，二者只是平移了 128 的同一组整数。量化器（u4）在标定输入 scale 时已经把这种表示纳入考量，所以两侧通过「共同的 `[0,1]` 归一化 + 量化 scale」保持一致。

还有一处重要简化：补丁**删掉了默认的 resize 与 letterbox 填充**，把 `scale=1.0`、`left=0`、`top=0` 写死。原因很简单——芯片在 u2-l2 切片时就已经是 `800×800`（`imgsz`），等于模型输入尺寸，运行时无需再缩放、无需灰边。

#### 4.3.2 核心流程

```
cv::Mat input_image            ← 来自 4.2 的 imread（int16、3 波段）
   │
   ├─ 若 4 通道：RGBA→RGB；否则原样
   ▼
cv::subtract(image, Scalar(-6000,-50,-50))   每波段减最小值
   ▼
cv::divide(image,  Scalar(8000,70,70))        每波段除以量程 → 落到 ~[0,1]
   ▼
cv::min(image, 1.0) / cv::max(image, 0.0)     clip 到 [0,1]
   ▼
cv::cvtColor(..., COLOR_RGB2BGR)              通道顺序调整为 DPU 期望的 BGR
   ▼
image.convertTo(output, CV_8S, 255, -128)     [0,1] → signed int8 [-128,127]
   ▼
scale=1.0, left=0, top=0                      不缩放、不填充（芯片已是 800×800）
   ▼
setInputImageBGR(images)                      送入 DPU 任务
```

归一化的逐波段数学（与 u3-l2 完全一致）：

\[
\text{norm}_c \;=\; \mathrm{clip}_{[0,1]}\!\left(\frac{x_c - \text{min}_c}{\text{max}_c - \text{min}_c}\right)
\]

三个波段的参数（注意 `cv::Scalar` 按「通道 0、1、2」顺序作用）：

| 通道（内存顺序） | 物理波段 | min | max | 量程 (max−min) |
|------------------|----------|-----|-----|----------------|
| 0 | bathymetry | −6000 | 2000 | 8000 |
| 1 | VV/VH | −50 | 20 | 70 |
| 2 | VV/VH | −50 | 20 | 70 |

于是 `Scalar(-6000,-50,-50)` 是各波段的 min，`Scalar(8000,70,70)` 是各波段的量程。这与 u3-l2 训练侧 `min_values=[-6000,-50,-50]`、除以 `[8000,70,70]` 的逻辑一一对应。

> 通道顺序说明：`cv::Scalar` 的三个值按内存通道顺序（通道 0、1、2）逐通道作用。本补丁里通道 0 按 bathymetry 的量程（±6000/8000）处理，说明读入 `cv::Mat` 的通道 0 对应 bathymetry——这与 u3-l2 指出的「OpenCV 读取多波段 TIFF 时的内存通道顺序」须保持一致；其后 `RGB2BGR` 再做一次通道交换，使最终送入 DPU 的 BGR 顺序与量化模型期望吻合。精确的「磁盘波段 ↔ 内存通道」对应关系需结合本地 OpenCV 版本验证，但**训推两侧必须用同一套通道约定**这一点是硬要求。

signed int8 转换的数学（`convertTo` 的语义是 `out = round(in*alpha + beta)`，这里 `alpha=255, beta=-128`）：

\[
q \;=\; \mathrm{round}(\text{norm}\times 255) - 128, \qquad q\in[-128,\ 127]
\]

#### 4.3.3 源码精读

`image_preprocess` 整段被重写。先看函数签名的变化——补丁删掉了 `height/width` 两个参数（因为不再 resize）：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:459-463](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L459-L463) —— 新签名只剩 `scale/left/top` 三个输出参数，原有的 `height/width` 被移除。

被删除的旧逻辑（默认的 resize + letterbox），注意这些是 `-` 行：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:466-501](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L466-L501) —— 旧代码按 `width/height` 算缩放系数、`cv::resize`、再用 `cv::copyMakeBorder` 填 114 灰边；补丁把这一整段删掉。

新增的 SAR 归一化四步（`+` 行），这是本模块的「心脏」：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:485-488](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L485-L488) —— `subtract` 减最小值、`divide` 除量程、`min(1.0)`/`max(0.0)` clip 到 `[0,1]`，四行合起来就是 u3-l2 的 `normalize()`。

通道顺序调整与 signed int8 转换：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:494-497](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L494-L497) —— 先 `RGB2BGR` 调通道顺序，再 `convertTo(output_image, CV_8S, 255, -128)` 把 `[0,1]` 映射到 signed int8 `[-128,127]`。

写死「不缩放、不填充」：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:502-504](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L502-L504) —— `scale=1.0; left=0; top=0;`，因为芯片在 u2 已切成 `800×800`，运行时无需任何几何变换。

调用点相应从 `setInputImageRGB` 改为 `setInputImageBGR`（因为前面做了 `RGB2BGR`）：

[framework/vitis_ai/xview3_yolov8_v3.5.patch:519-534](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L519-L534) —— `run()` 批量预处理循环里改调新签名的 `image_preprocess`，并把 `setInputImageRGB` 换成 `setInputImageBGR`。

README 给出的归一化代码摘要（与补丁一致，但有一处**小瑕疵**需要留意）：

[framework/vitis_ai/README.md:16-30](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L16-L30) —— README 摘录的归一化片段。

> **读源码时要警惕：** README 这段摘录里夹了四行 `top = round(dh - 0.1)` 之类（README 第 22–25 行），它们其实是**被删除的旧 letterbox 代码的残留**，并不出现在真实补丁的新逻辑里——真实补丁对应位置是 `scale=1.0; left=0; top=0;`（[patch:502-504](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L502-L504)）。当 README 摘要与补丁冲突时，**以补丁为准**。这也是为什么本讲反复让你直接读 `.patch` 而不是只看 README。

#### 4.3.4 代码实践

**实践目标：** 画出从「原始 SAR TIFF」到「signed int8 输入张量」的完整数据流，并用数值验证归一化与转换的正确性。

**操作步骤：**

1. 阅读补丁 [patch:485-497](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/xview3_yolov8_v3.5.patch#L485-L497)，按顺序列出 `subtract → divide → min → max → cvtColor → convertTo` 六步。
2. 用下文「示例代码」对一个已知 SAR 像素值手工跑一遍，验证落点。
3. 画出数据流图（输入 `int16` → `[0,1]` float → signed int8）。

**示例代码（非项目原有代码，用 numpy 复现 C++ 的四步归一化 + signed 转换）：**

```python
import numpy as np
# 某像素三通道 (bathymetry, VV, VH) 的原始 int16 值
x = np.array([-3000.0, -15.0, -8.0])

# 1) subtract min
x = x - np.array([-6000.0, -50.0, -50.0])
# 2) divide 量程
x = x / np.array([8000.0, 70.0, 70.0])
# 3) clip [0,1]
x = np.clip(x, 0.0, 1.0)
print("归一化 [0,1]:", x)           # 期望约 [0.375, 0.5, 0.6]

# 4) convertTo(CV_8S, alpha=255, beta=-128)  → signed int8
q = np.round(x * 255 - 128).astype(np.int8)
print("signed int8:", q)            # 期望约 [-32, 0, 25]
```

**需要观察的现象：** `x=-3000`（bathymetry）落在 `(−3000+6000)/8000 = 0.375`，再转 int8 得 `round(0.375*255)−128 ≈ −32`；`x=-15`（VV）落在 `35/70 = 0.5`，int8 得 `0`。可手算核对。

**预期结果：** 输出一组 `[-128,127]` 内的 signed int8 值，且与「先转 uint8 再减 128」结果一致——印证 `signed = unsigned − 128`。

**待本地验证：** 上面是纯数值复现，不依赖硬件；若要验证 `convertTo` 的 OpenCV 真实行为，可在装了 OpenCV 的环境里用 C++ 跑同一组数对比。

#### 4.3.5 小练习与答案

**练习 1：** 为什么预处理最后要 `convertTo(output_image, CV_8S, 255, -128)` 而不是保留 float 或用 `CV_8U`？

**参考答案：** DPU（`DPUCZDX8G`）是 signed int8 定点加速器，u4 量化时输入张量的 scale 也是按 signed int8 标定的，所以必须喂 `CV_8S`。float 会被框架拒绝或隐式转换；`CV_8U`（无符号）与 DPU 的有符号约定不符，会引入 128 的系统性偏移。

**练习 2：** 补丁为什么能把 `scale/left/top` 直接写死为 `1.0/0/0`，删掉整个 resize+letterbox？

**参考答案：** 因为芯片在 u2-l2 切片时已经按 `imgsz=800` 切成 `800×800`，正好等于模型输入尺寸，运行时不存在「图大小 ≠ 模型输入」的情况，也就不需要缩放和灰边填充。写死这些值还能让后处理坐标还原（u6-l3 里去掉了 `scales[k]`/`left_padding` 的除法）保持一致。

**练习 3：** 训练侧归一化到 `uint8 [0,255]`，推理侧归一化到 signed int8 `[-128,127]`，这会破坏训推一致性吗？

**参考答案：** 不会。因为两侧**归一化到 `[0,1]` 的公式完全相同**（同一组 min/量程），差异只在最后的整数表示，而 `signed = unsigned − 128` 只是平移。量化器在 u4 标定输入 scale 时已把这种表示纳入考虑，所以真正的一致性由「共同的 `[0,1]` 归一化 + 量化 scale」保证，而不是靠两端用同一种整数类型。

---

## 5. 综合实践

**任务：画出从「磁盘 SAR TIFF」到「DPU 输入张量」的完整数据流，并标注每一处「训推一致性」检查点。**

要求：

1. 在一张图（文本框图即可）里串起本讲两个模块：`ReadImagesThread`（4.2）→ `image_preprocess`（4.3）→ `setInputImageBGR` → DPU。每个箭头标注数据的「类型 + 数值范围」，例如：
   - 磁盘 → `imread(IMREAD_UNCHANGED)`：`int16`，VV/VH ∈ `[-50,20]`、bathymetry ∈ `[-6000,2000]`；
   - `subtract/divide/min/max`：float，∈ `[0,1]`；
   - `convertTo(CV_8S,255,-128)`：signed int8，∈ `[-128,127]`。
2. 在图上用 ★ 标出三个**一致性检查点**，并各写一句「若不一致会怎样」：
   - ★ 加载：`IMREAD_UNCHANGED` 必须与训练侧一致，否则 dB 值失真；
   - ★ 归一化：min/量程必须与 u3-l2 的 `normalize()` 一致，否则输入分布偏移、精度崩；
   - ★ dtype：必须 signed int8 与 DPU/量化约定一致，否则数值系统性偏移。
3. 用 4.3.4 的示例代码，对一个手选的 `(bathymetry, VV, VH)` 像素手算到 signed int8，把结果填进数据流图的最后一格。
4. 反思：README 第 22–25 行那段 `round(dh-0.1)` 残留代码，如果有人照着 README（而不是补丁）去实现预处理，会引入什么 bug？

**预期产出：** 一张数据流图 + 三处一致性标注 + 一个手算样例 + 一段关于「README 与补丁不一致」的反思。这个练习把本讲的两个最小模块（TIFF 加载、归一化）与「训推一致性」主线拧成了一股绳，也为 u6-l2（PIoU2 NMS）和 u6-l3（P2 后处理）这两个「输出端」改动铺好了「输入端」的对称认知。

## 6. 本讲小结

- Vitis AI 框架本身已支持 YOLOv8，本项目的做法是**用一个补丁改其四个源文件**，把「RGB 自然图像检测」改造成「SAR 三通道船舶检测」。
- 四个文件对应四类训推不一致：`demo_accuracy.hpp`（数据格式）、`yolov8_imp.cpp`（数值归一化）、`apply_nms.cpp`（IoU 度量，u6-l2 详讲）、`yolov8.cpp`（网络结构/P2，u6-l3 详讲）。
- 读 SAR 的 16 位 TIFF 必须给 `cv::imread` 加 `cv::IMREAD_UNCHANGED`，否则位深被压缩、数值失真甚至读空——与 u3-l2 训练侧同一教训。
- `image_preprocess` 用 `subtract/divide/min/max` 四步把 SAR 三通道线性归一化到 `[0,1]`，公式与 u3-l2 的 `normalize()` 完全一致（min=`(-6000,-50,-50)`、量程=`(8000,70,70)`）。
- 归一化后再 `convertTo(CV_8S, 255, -128)` 转 **signed int8** `[-128,127]`，因为 DPU 是有符号 8 位定点加速器；这与训练侧的 uint8 只差一个 −128 平移，真正的训推一致性由共同的 `[0,1]` 归一化保证。
- 补丁删掉了默认的 resize+letterbox（`scale=1.0/left=0/top=0`），因为芯片在 u2 已切成 `800×800`；当 README 摘要与补丁冲突时以补丁为准。

## 7. 下一步学习建议

本讲搞定了「输入端」的两个文件（加载 + 预处理）。接下来沿数据流走向「输出端」：

- **u6-l2 PIoU2 NMS 后处理：** 精读 `apply_nms.cpp` 里的 `cal_piou2()` 与 `PIOU2_NMS` 环境变量，看它如何把 u3-l3 训练侧的 PIoU2 度量复刻到 C++ NMS，并理解运行时开关 `PIOU2_NMS=1` 的切换机制。
- **u6-l3 YOLOv8 后处理优化与 P2 架构：** 精读 `yolov8.cpp` 的 `yolov8_post_process`，看它如何处理新增的 P2 高分辨率头、为何能把解码加速约 27×，以及它与 u8 HLS 解码核的分工。

建议在进入 u6-l2 前，先回顾 u3-l3（PIoU2 数学推导）和 u3-l5（NMS 与坐标变换），这样看到 C++ 版 `cal_piou2` 时能立刻与 Python 版 `compute_piou` 对账，体会「训推一致」在代码层面的具体落地。
