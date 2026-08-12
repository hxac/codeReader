# 讲义：Solver AIE 内核与 L2 基准

> 承接：本讲假设你已学完 [u7-l1 稠密矩阵分解 HLS 内核](u7-l1-solver-hls.md)（solver 的 **PL HLS** 分解内核、统一的「流式入口 + traits + ARCH + 返回码」写法）与 [u6-l2 L2 AIE 内核全景](u6-l2-aie-kernel-catalog.md)（dsp 库 AIE 内核家族、`kernel + traits + utils` 三件式组织、`TT_`/`TP_` 命名、`TP_CASC_LEN` 级联、`*_aie1_*`/`*_aie2_*` 参数集、UUT/REF 双图比对）。本讲不再重复这些基础，而是把它们**搬到 solver 库**，并补上 solver 独有的两块内容：AIE 分解内核的**网格/级联拓扑**，以及 L2/benchmarks 在 **Alveo PL 卡**上做的性能评测。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 solver 库**同时存在两条实现路线**——PL HLS（`include/hw`）与 AIE（`include/aie`）——它们各自的目录、产物与目标硬件。
- 读懂 AIE cholesky 的**二维网格分块拓扑**（`TP_GRID_DIM`/`TP_X`/`TP_Y`）和 qrd 的**修正 Gram-Schmidt 级联**（`TP_CASC_LEN`），以及它们为何要在图级编译里做角色分派。
- 解释 solver AIE 内核沿用的 `kernel.hpp + traits.hpp + utils.hpp + 运行时 .cpp` 四件拆分，以及 L2 层 `qrd_graph` 这类**图包装器**如何把单内核拼成可实例化的 ADF 图。
- 区分 `L2/tests/aie`（功能/精度验证）与 `L2/benchmarks`（Alveo 上的性能评测），并说明后者为何是 **PL HLS** 路线、与 L1 单内核测试在目标上有何根本差别。

## 2. 前置知识

- **PL 与 AIE 两条加速范式**（见 u1-l3）：PL 走 HLS→RTL，跑在 Alveo 数据中心卡与 Zynq/Versal 的可编程逻辑上；AIE 走 ADF 数据流图，跑在 Versal 的 AI Engine 阵列上。
- **HLS 内核写法**（见 u3-l1/u3-l2）：`extern "C"` DUT、`hls::stream`、`#pragma HLS INTERFACE m_axi`、pipeline/unroll/dataflow。
- **AIE 图构成**（见 u6-l2/u6-l3）：`graph`、`kernel::create_object`、`connect`/`dimensions`、window（带地址的缓冲）与 stream（无地址流）、cascade（核间专用级联流）。
- **数值背景**：QR 分解把矩阵 \(A\) 分解为 \(A=QR\)（\(Q\) 正交、\(R\) 上三角）；Cholesky 分解把对称正定矩阵 \(A\) 分解为 \(A=LL^{H}\)（\(L\) 下三角）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [solver/L1/include/aie/cholesky.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp) | AIE Cholesky **内核类定义**：二维网格拓扑常量、cascade/stream 端口类型、`registerKernelClass` 的角色分派。 |
| [solver/L1/include/aie/qrd.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd.hpp) | AIE QRD **内核类定义**：修正 Gram-Schmidt 函数签名，按 `CASC_IN/OUT` 的四个偏特化。 |
| [solver/L1/include/aie/qrd_kernel.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_kernel.hpp) | 更底层的 Gram-Schmidt 流式内核类（Start/Mid/End，用 `caccfloat` 级联累加）。 |
| [solver/L1/include/aie/qrd_traits.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_traits.hpp) | QRD 的 **traits**：编译期合法性检查（数据类型、缓冲区大小、tiling）与级联端口接口结构体。 |
| [solver/L1/include/aie/qrd_utils.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_utils.hpp) | QRD 的 **utils**：含向量 intrinsics 的数值辅助函数（如 `hw_invsqrt`）。 |
| [solver/L1/src/aie/cholesky.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/src/aie/cholesky.cpp) | Cholesky **运行时实现**：含 `::aie::invsqrt`/`::aie::mul` 等 intrinsics 的函数体。 |
| [solver/L2/include/aie/qrd_graph.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/include/aie/qrd_graph.hpp) | L2 层 **图包装器** `qrd_graph`：把内核级联拼成 ADF 图，含防御性 `static_assert`。 |
| [solver/L2/benchmarks/gesvj/](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/kernel_gesvj.cpp) | **PL HLS** 基准：Jacobi SVD（`double`），在 Alveo 上评测吞吐/资源。 |
| [solver/L2/benchmarks/gesvdj/](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvdj/README.md) | **PL HLS** 基准：对称矩阵 Jacobi SVD。 |
| [solver/L2/tests/aie/qrd/](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/tests/aie/qrd/description.json) | AIE QRD 的 **功能测试**：`flow=aie`，UUT/REF 双图比对。 |

## 4. 核心概念与源码讲解

### 4.1 solver 的两条实现路线与 AIE 内核总览

#### 4.1.1 概念说明

solver 库最大的认知陷阱是：**同一个数学运算（如 QR 分解、Cholesky 分解、SVD）往往有两套互不相干的实现**。

- **PL HLS 路线**：代码在 `solver/L1/include/hw/` 与 `solver/L2/include/hw/`，被 `solver/L2/benchmarks/` 的基准调用，综合成 RTL，跑在 **Alveo U200/U250/U280** 这类数据中心 FPGA 卡上（详见 u7-l1）。
- **AIE 路线**：代码在 `solver/L1/include/aie/` 与 `solver/L2/include/aie/`，被 `solver/L2/tests/aie/` 的测试驱动，编进 ADF 图，跑在 **Versal（vck190/vek280）** 的 AI Engine 阵列上。

两套实现**不共享源码**，只是数学语义对应。本讲先讲 AIE 路线的内核本体（4.2/4.3/4.4），再讲 PL 路线的 benchmarks（4.5）。读者务必记住：**看到 `include/aie` 是 AIE，看到 `include/hw` 与 `L2/benchmarks` 是 PL**，二者文件名可能同名（都叫 `cholesky`/`qrd`）却属于完全不同的编译流程与硬件。

AIE 路线在 `solver/L1/include/aie/` 下提供的内核家族如下：

| 内核 | 数学功能 | 关键拓扑参数 |
| --- | --- | --- |
| `cholesky` | 对称正定矩阵 Cholesky 分解 \(A=LL^{H}\) | `TP_GRID_DIM`/`TP_X`/`TP_Y`（二维网格） |
| `qrd` | 一般矩阵 QR 分解（修正 Gram-Schmidt） | `TP_CASC_LEN`（级联长度） |
| `qrd_hh` | 基于 Householder 反射的 QR 分解 | 级联 |
| `svd` | 奇异值分解 | 级联 |
| `substitution` | 三角求解（前/回代） | 级联 |

#### 4.1.2 核心流程

每个 AIE 内核在工程里都被组织成「四件一套」——这套约定在 u6-l2 讲 dsp 库时已经建立，solver 完全沿用：

```
solver/L1/include/aie/<name>.hpp          ← 内核类定义（构造函数 + 防御检查 + 函数声明，无 intrinsics）
solver/L1/include/aie/<name>_traits.hpp   ← 编译期属性函数（数据类型/缓冲区/tiling 合法性，无 intrinsics）
solver/L1/include/aie/<name>_utils.hpp    ← 数值辅助函数（可含向量 intrinsics）
solver/L1/src/aie/<name>.cpp              ← 运行时函数体（真正调用 aie intrinsics）
solver/L2/include/aie/<name>_graph.hpp    ← 图包装器（把内核实例化、连线成 ADF 图）
```

为什么要这样拆？cholesky.hpp 顶部注释说得很直接：

> The constructor definition is held in this class because this class must be accessible to graph level aie compilation. The main runtime function is captured elsewhere (cpp) as it contains aie intrinsics … which are not included in aie graph level compilation.

AIE 编译分两阶段：**图级编译**（建图、布局、连线，不能出现 intrinsics）与**核级编译**（为每个核生成微码，才允许 intrinsics）。所以「不能含 intrinsics」的部分（类定义、traits）放头文件参与图级编译；「含 intrinsics」的函数体另放 `.cpp`，只在核级编译时被 `source(...) = "<name>.cpp"` 引入。

#### 4.1.3 源码精读

- 四件拆分的动机写在每个内核头文件顶部，例如 [cholesky.hpp:19-29](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp#L19-L29)：这段注释解释了「类定义留头文件、运行时函数留 cpp」的根因是 AIE 两阶段编译。
- 命名约定 [cholesky.hpp:31-34](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp#L31-L34)：`TT_` 为类型形参前缀、`TP_` 为非类型（整型/布尔）形参前缀，全库统一。
- 内核家族清单见目录 [solver/L1/include/aie/](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp)：每个内核都成对出现 `*.hpp` + `*_traits.hpp` + `*_utils.hpp`。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认「同名不同路」的二分结构。
2. **操作步骤**：
   - 在仓库根目录执行 `ls solver/L1/include/aie/` 与 `ls solver/L1/include/hw/`，对比两边的文件名。
   - 再执行 `ls solver/L2/tests/aie/` 与 `ls solver/L2/benchmarks/`。
3. **观察现象**：你会看到 `aie/` 下有 `cholesky.hpp`/`qrd.hpp` 等，`hw/` 下也有同名的 PL HLS 头文件；`tests/aie/` 下有 `qrd`/`cholesky` 等子目录，`benchmarks/` 下则是 `gesvj`/`gesvdj`/`gtsv`。
4. **预期结果**：同名内核分属两条路线，`benchmarks` 只覆盖 PL 路线，AIE 路线的性能/精度验证在 `tests/aie`。
5. 待本地验证（纯目录浏览，无需工具链）。

#### 4.1.5 小练习与答案

- **练习 1**：solver 的 AIE 内核与 PL HLS 内核分别放在哪两个目录？答案：`solver/L1/include/aie/`（AIE）与 `solver/L1/include/hw/`（PL HLS）。
- **练习 2**：为什么运行时函数体要单独放 `.cpp` 而不写在类定义里？答案：因为它含 AIE intrinsics，而图级编译不允许 intrinsics；只有核级编译才引入 `.cpp`（见 u6-l2 的两阶段编译）。

---

### 4.2 AIE cholesky 内核：二维网格分块拓扑

#### 4.2.1 概念说明

Cholesky 分解是**串行**算法——每个对角元 \(l_{kk}\) 依赖它左上方所有已算出的元素，下一列又依赖 \(l_{kk}\)。要在 AIE 阵列上并行化，solver 采用**分块（block）Cholesky**：把大矩阵切成 \(\text{GRID\_DIM}\times\text{GRID\_DIM}\) 个块，每个块交给一个 AIE 核（一个内核实例），用核间 cascade/stream 流水传递中间结果。

每个核在网格里有一个坐标 \((\text{TP\_X}, \text{TP\_Y})\)，根据坐标扮演不同角色：对角块（diagonal kernel，做分解并广播因子）、下三角块（lower kernel，用对角因子更新自己）。`TP_GRID_DIM` 决定网格边长。

#### 4.2.2 核心流程

cholesky 类在**编译期**用一连串 `constexpr` 算出本核负责的对角线范围，然后在 `registerKernelClass()` 里按坐标分派不同的入口函数：

```
若 本核是孤立单核 → cholesky_main（单核完成全部分解）
否则 若 TP_X == TP_Y          → 对角核：topLeft / middle / botRight
     若 TP_X <  TP_Y          → 下三角核：leftEdge / botLeft / botEdge / nonEdge
```

`registerKernelClass()` 是 AIE 图级编译的「登记窗口」——它用 `REGISTER_FUNCTION` 告诉编译器：**这个类实例真正要跑的是哪个成员函数**。同一个 C++ 类，靠模板参数（坐标）在不同实例上登记不同的运行函数，这是 AIE 内核常见的「一类多角」模式。

#### 4.2.3 源码精读

- 类模板与网格参数 [cholesky.hpp:54-65](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp#L54-L65)：`TP_GRID_DIM`/`TP_X`/`TP_Y`/`TP_DIAG_INV` 等模板形参定义了本核在网格中的位置与行为。
- 编译期拓扑常量 [cholesky.hpp:66-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp#L66-L87)：`kKernelDim = TP_DIM/TP_GRID_DIM` 算出每块尺寸，`kNumStages`/`kStageStartKernel` 等算出本核要执行的阶段范围——全部 `constexpr`，零运行时代价。
- cascade/stream 端口类型随硬件切换 [cholesky.hpp:92-102](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp#L92-L102)：用 `__STREAMS_PER_TILE__` 宏区分 AIE（用 `input_cascade` 专用级联流）与 AIE-ML（用普通 `input_stream`），注释明确「只支持 AIE 与 AIE-ML」。
- 角色分派 [cholesky.hpp:116-140](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp#L116-L140)：`registerKernelClass()` 内一串 `if/else` 按 `TP_X`/`TP_Y` 选 `REGISTER_FUNCTION(...)`，同时 `REGISTER_PARAMETER` 登记两个乒乓缓冲（`diagColBuffer`/`diagRowBuffer`）。

#### 4.2.4 代码实践

1. **实践目标**：理解「同一类、不同角色」的分派逻辑。
2. **操作步骤**：阅读 [cholesky.hpp:116-140](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/cholesky.hpp#L116-L140)，画一张 \(3\times3\) 网格（`TP_GRID_DIM=3`），把每个格子 \((\text{TP\_X},\text{TP\_Y})\) 标上它会被分派到哪个函数（`diagKernel_topLeft`/`diagKernel_middle`/`diagKernel_botRight`/`lowerKernel_*`）。
3. **观察现象**：对角线上的三个格子分别拿到 topLeft/middle/botRight；对角线下方的格子拿到 lower 系列函数。
4. **预期结果**：你能看出「对角核产出因子 → 下三角核消费因子」的数据依赖方向。
5. 待本地验证（纯源码阅读型实践）。

#### 4.2.5 小练习与答案

- **练习 1**：`TP_X == TP_Y` 表示核在网格的什么位置？答案：在对角线上，因此它是 diagonal kernel（负责该块的 Cholesky 分解并广播因子）。
- **练习 2**：`registerKernelClass()` 的作用是什么？答案：在图级编译时登记本类实例真正执行的成员函数（`REGISTER_FUNCTION`）与运行时参数（`REGISTER_PARAMETER`），让同一 C++ 类在不同坐标实例上跑不同函数。

---

### 4.3 AIE qrd 内核：修正 Gram-Schmidt 级联

#### 4.3.1 概念说明

QR 分解把 \(A\in\mathbb{C}^{m\times n}\) 分解为 \(A=QR\)。solver 的 AIE QRD 用**修正 Gram-Schmidt（MGS）**算法：逐列计算 \(Q\) 的列向量 \(q_j\)，每算一列都要从该列里减去它与之前所有 \(q\) 的投影。MGS 比经典 Gram-Schmidt 数值更稳定。

`TP_CASC_LEN`（级联长度）是 QRD 的核心并行旋钮：把 \(A\) 的**列**分布到 `TP_CASC_LEN` 个串联的核上，每个核只负责一段列。数据沿 cascade 流水线逐级累加——这正对应 u6-l2 讲过的 `TP_CASC_LEN` 机制。

#### 4.3.2 核心流程

QRD 的级联拓扑用两个布尔模板参数 `TP_CASC_IN`/`TP_CASC_OUT` 描述每个核在流水线里的位置：

```
TP_CASC_IN = FALSE, TP_CASC_OUT = FALSE  → 唯一核（CASC_LEN=1，无级联）
TP_CASC_IN = FALSE, TP_CASC_OUT = TRUE   → 流水线第一级（源头）
TP_CASC_IN = TRUE,  TP_CASC_OUT = TRUE   → 中间级
TP_CASC_IN = TRUE,  TP_CASC_OUT = FALSE  → 末级（汇点）
```

不同位置的核，其 `qrd_main` 的**函数签名不同**（有的带 `input_cascade`、有的带 `output_cascade`），所以 `qrd.hpp` 用四个偏特化分别给出签名。这正是 AIE 内核「按级联位置裁剪端口」的典型写法。

#### 4.3.3 源码精读

- `qrd_kernel` 基类与 MGS 函数声明 [qrd.hpp:54-91](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd.hpp#L54-L91)：`qrd_mgs`/`qrd_mgs_first_kernel`/`qrd_mgs_casc` 三个函数分别处理一般核、首核、级联核；`QrdCascData[TP_DIM_ROWS]` 是级联中间数据缓冲。
- 默认 `qrd` 类（登记 `qrd_main`） [qrd.hpp:93-135](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd.hpp#L93-L135)：`registerKernelClass()` 登记 `qrd_main`，输入窗口 `inWindowA`、输出 `outWindowQ`/`outWindowR`。
- 四个偏特化给出不同级联位置的签名，例如中间级 [qrd.hpp:137-185](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd.hpp#L137-L185) 同时带 `input_cascade` 与 `output_cascade`；末级 [qrd.hpp:187-234](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd.hpp#L187-L234) 只有 `input_cascade`。
- 更底层的流式 Gram-Schmidt 内核 [qrd_kernel.hpp:25-105](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_kernel.hpp#L25-L105)：`GramSchmidtKernelComplexFloat_Start/_Mid/_End` 三件套用 `input_stream_cfloat`/`output_stream_caccfloat` 表达级联，`_Mid` 用 `caccfloat`（级联累加专用复数累加类型）逐级累加内积。
- 真正的 intrinsics 在运行时文件，例如 cholesky 的 [cholesky.cpp:54-85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/src/aie/cholesky.cpp#L54-L85)：`cholesky_main` 里用 `::aie::invsqrt`（对角倒数平方根）与 `::aie::mul`（向量乘）做块更新，证实了「头文件无 intrinsics、cpp 才有」的拆分。

#### 4.3.4 代码实践

1. **实践目标**：把级联位置与端口签名对应起来。
2. **操作步骤**：阅读 [qrd.hpp:137-234](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd.hpp#L137-L234)，列出 `CASC_IN_TRUE+CASC_OUT_TRUE` 与 `CASC_IN_TRUE+CASC_OUT_FALSE` 两个偏特化的 `qrd_main` 形参差异。
3. **观察现象**：前者同时有 `inCascade*` 与 `outCascade*`，后者只有 `inCascade*`。
4. **预期结果**：你能解释「中间级收上一级、发下一级；末级只收不发」。
5. 待本地验证（源码阅读型实践）。

#### 4.3.5 小练习与答案

- **练习 1**：`TP_CASC_LEN` 调大（如 1→4）对设计意味着什么？答案：把 \(A\) 的列分摊到更多串联核上，单核缓冲更小、可支持更大矩阵或更多帧（`TP_NUM_FRAMES`），代价是占更多 AIE 核。
- **练习 2**：为什么 `qrd_main` 在不同偏特化里签名不一样？答案：级联位置决定端口——首核无入级联、末核无出级联、中间级两者皆有，签名必须随之变化。

---

### 4.4 traits / utils / L2 图包装器与 AIE 测试

#### 4.4.1 概念说明

4.1 讲了「四件一套」的目录约定，本节深入其中三件：**traits**、**utils** 与 **L2 图包装器**。

- **traits**（`*_traits.hpp`）：封装「与 intrinsics 相关、但本身不是 intrinsics」的属性——数据类型是否合法、缓冲区是否装得下、tiling 是否支持。它必须能参与图级编译，所以**不含任何向量类型或 intrinsics**。traits 里的检查函数几乎都是 `constexpr`，配合 `static_assert` 把参数误用前移到编译瞬间（与 u3-l2 讲过的静态断言护栏一脉相承）。
- **utils**（`*_utils.hpp`）：数值辅助函数，**可以含向量 intrinsics**（如开平方倒数 `hw_invsqrt`）。它在核级编译时被引入。
- **L2 图包装器**（`L2/include/aie/*_graph.hpp`）：把 L1 的内核类**实例化、连级联、接端口**，包成一个继承自 `graph` 的类。用户和测试一般直接用 L2 图包装器，而不是裸 L1 内核类。

#### 4.4.2 核心流程

以 QRD 为例，`qrd_graph` 在构造函数里做四件事：

1. 用 `get_qrd_loads_of_kernels()` 算出每个级联核应承担的列数（均匀分配）。
2. 用递归元函数 `create_casc_kernel_recur_qrd` 逐个 `kernel::create_object`，按位置自动选 `CASC_IN/OUT` 组合（首、中、末）。
3. 对每个核设 `source(...) = "qrd.cpp"`（引入运行时实现）、`runtime<ratio> = 0.9`（占用率上限）、`connect`/`dimensions`（连线与窗口大小）、`single_buffer`（单缓冲约束）。
4. 一组 `static_assert` 在图构造期做合法性检查。

AIE 功能测试则放在 `solver/L2/tests/aie/<name>/`，沿 u6-l2 讲过的套路：`flow=aie`、按 `*_aie1_*`（vck190）/`*_aie2_*`（vek280）挑参数集、用 UUT（待测图）与 REF（参考图）双图比对、设 `DIFF_TOLERANCE` 误差门限判 PASS/FAIL，分 x86sim（功能）与 aiesim（周期精确）两档。

#### 4.4.3 源码精读

- traits 里的硬件常量与类型检查 [qrd_traits.hpp:38-52](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_traits.hpp#L38-L52)：`kMaxReadInBytes = __MAX_READ_WRITE__/8` 是单次向量读写字节数，`fnCheckDataType` 只放行 `float`/`cfloat`。
- 缓冲区容量检查 [qrd_traits.hpp:54-61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_traits.hpp#L54-L61)：`fnCheckBufferSize` 确保 `TP_DIM_COLS*TP_DIM_ROWS*TP_NUM_FRAMES*sizeof(TT_DATA)` 不超过单片数据内存 `__DATA_MEM_BYTES__`。
- 级联端口接口结构体 [qrd_traits.hpp:133-149](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_traits.hpp#L133-L149)：`T_inputIF`/`T_outputIF` 用 `std::conditional` 在 `CASC_IN/OUT_FALSE` 时退化为空类型 `no_port`，从而优雅地「按需裁剪端口」。
- utils 里含 intrinsics 的数值函数 [qrd_utils.hpp:53-60](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_utils.hpp#L53-L60)：`hw_invsqrt` 先用 `::aie::invsqrt`（约 12 位硬件近似）再做一步牛顿迭代 \(y_1 = y_0(1.5 - 0.5\,x\,y_0^{2})\) 提升到 float 全精度。
- L2 图包装器的级联创建 [qrd_graph.hpp:47-105](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/include/aie/qrd_graph.hpp#L47-L105)：递归元函数 `create_casc_kernel_recur_qrd` 按 `cascPos` 自动选择首/中/末的 `CASC_IN/OUT` 组合并 `kernel::create_object`。
- 图类与防御性断言 [qrd_graph.hpp:136-184](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/include/aie/qrd_graph.hpp#L136-L184)：`qrd_graph` 公开 `inA`/`outQ`/`outR` 端口数组与 `m_kernels`，构造期一串 `static_assert`（数据类型、列/行必须是向量字节数的整数倍且不小于最小值）把误用挡在编译期。
- 构造函数内的连线 [qrd_graph.hpp:214-249](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/include/aie/qrd_graph.hpp#L214-L249)：`source(m_kernels[i]) = "qrd.cpp"` 引入运行时实现，`runtime<ratio> = 0.9` 设占用率，`connect`/`dimensions`/`single_buffer` 完成窗口接线。
- AIE 测试的流程与平台 [solver/L2/tests/aie/qrd/description.json:1-40](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/tests/aie/qrd/description.json#L1-L40)：`flow=aie`、`platform_allowlist` 为 `vck190`+`vek280`，`param_set` 按平台分别匹配 `*_aie1_*`/`*_aie2_*`。
- 测试参数集命名举例：[multi_params.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/tests/aie/qrd/multi_params.json) 里如 `test_5_1_float_32_32_1_1_..._aie1_hw_checkin`，编码了数据类型（`float`/`cfloat`）、矩阵行列、级联长度、目标平台与测试套件。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一个 AIE QRD 测试从参数到图实例化的路径。
2. **操作步骤**：
   - 读 [solver/L2/tests/aie/qrd/test.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/tests/aie/qrd/test.cpp)，找到 `xf::solver::aie::testcase::test_graph qrdTestHarness;` 这一行。
   - 再读 `test.hpp`（同目录），看 `test_graph` 内部如何同时实例化 UUT（`qrd_graph`）与 REF 参考图并把它们的输出接到比对逻辑。
3. **观察现象**：测试图把 UUT 与 REF 封装在一起，二者吃同一输入、输出被外部比对。
4. **预期结果**：你能在 `multi_params.json` 里挑一个 `*_aie1_*` 参数，说出它会被 vck190 的 CI 选中。
5. 待本地验证（源码阅读型实践，无需工具链）。

#### 4.4.5 小练习与答案

- **练习 1**：traits 文件为什么不能含 intrinsics？答案：它要参与图级编译，而图级编译不引入 intrinsics；intrinsics 留给 utils 与运行时 `.cpp` 在核级编译时才引入。
- **练习 2**：`runtime<ratio> = 0.9` 是什么意思？答案：给该核设定的运行时占用率上限（名义值，0.9 表示不超过核处理能力的 90%），供编译器做布局与吞吐校验。
- **练习 3**：`T_inputIF` 用 `std::conditional` 把 `inCascade` 退化为 `no_port` 的好处是什么？答案：让「有/无入级联」两类核共用同一套接口结构体定义，靠模板参数自动裁剪端口，避免为每种级联位置各写一套。

---

### 4.5 L2/benchmarks：在 Alveo 上评测 PL 分解内核

#### 4.5.1 概念说明

`solver/L2/benchmarks/` 下的 `gesvj`、`gesvdj`、`gtsv` 是 **PL HLS 基准**，不是 AIE。它们评测的是 solver 的 **PL 路线**（`include/hw`）内核——用 `double` 双精度、`#pragma HLS INTERFACE m_axi` 的标准 HLS DUT，在 **Alveo U200/U250/U280** 这类数据中心 FPGA 卡上跑出真实的延迟、频率与资源占用。solver 顶层 README 写得很明确：

> In `L2/benchmarks`, Kernels are built into xclbins targeting Alveo U200/U250.

> ⚠️ **一个容易踩的坑**：`gesvj` 的 [description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/description.json) 里 `platform_allowlist` 写的是 `vck190`、Makefile 默认也是 `vck190`，但它的 README、连接配置（`conn_u200/u250/u280/u50.cfg`）和性能表都指向 **Alveo U250 的 PL 区**。原因是 vck190（Versal）同样含 PL 可编程逻辑，CI 允许在它的 PL 区上跑这个 HLS 内核；但内核本身是纯 PL HLS，历史主验证平台是 Alveo U250。**判断路线看源码（`hw/` + HLS pragma），不要只看 allowlist。**

#### 4.5.2 核心流程

一个 benchmark 目录的典型构成：

```
gesvj/
├── kernel_gesvj.cpp   ← PL HLS 内核 DUT（extern "C" + m_axi pragma，调 xf::solver::gesvj）
├── test_gesvj.cpp     ← 主机程序（xcl2 + OpenCL，量执行时间）
├── Makefile / utils.mk← 构建：v++ -c → -l → --package，产出 xclbin 与 host exe
├── description.json   ← flow=system 元数据（容器、CU、DDR 绑定、平台白名单）
├── conn_u200/u250/u280/u50.cfg  ← 各 Alveo 卡的 sp= 端口-存储连接
└── README.md          ← 构建/运行命令 + 性能与资源表
```

构建与运行（以 gesvj 为例）走标准的 L2 Vitis 流程（见 u5-l1）：`v++ -c` 把 `kernel_gesvj.cpp` 编成 XO，`v++ -l` 按 `conn_*.cfg` 连成 xclbin，主机 `test_gesvj.exe` 加载 xclbin、配参数（`-M`/`-N`/`-runs`/`-seed`）、量出 Kernel/DDR 读写各自的耗时。

#### 4.5.3 源码精读

- 内核 DUT 是标准 HLS 写法 [kernel_gesvj.cpp:30-53](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/kernel_gesvj.cpp#L30-L53)：`extern "C" void kernel_gesvj_0` 带 4 个 `m_axi` 端口（`dataA`/`sigma`/`dataU`/`dataV`）与 `s_axilite` 控制端口，调用 `xf::solver::gesvj<double, MAXM, MAXN, MCU, NCU>(...)`。`double` 类型与 `MCU`/`NCU`（矩阵计算单元并行度，对应 README 里的 Unroll factor）是 PL 路线的标志。
- 它 `#include "hw/xf_solver_L2.hpp"`（`hw/` 前缀 = PL HLS），gesvdj 同理 [kernel_gesvdj.cpp:18-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvdj/kernel_gesvdj.cpp#L18-L31)——证实 benchmarks 全是 PL 路线。
- 元数据声明容器与 CU 绑定 [gesvj/description.json:49-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/description.json#L49-L87)：`containers` 里声明 `kernel_gesvj`、1 个 CU（`kernel_gesvj_0_1`）、各参数绑到 `DDR[0]`（即 bank0），频率 200MHz。`flow=system` 表示这是 L2 系统流程（见 u5-l3）。
- 连接配置把端口绑到 DDR bank [conn_u250.cfg:1-5](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/conn_u250.cfg#L1-L5)：`sp=kernel_gesvj_0_1.dataA:bank0` 等四行，正是 u5-l1 讲过的 `sp=` 存储端口绑定（也是主机 `group_id` 的源头）。
- Makefile 默认平台与目标 [gesvj/Makefile:53-61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/Makefile#L53-L61)：`TARGET ?= hw_emu`、`PLATFORM := vck190`，并显式报错拒绝 `sw_emu`（2025.1 起移除）。
- 构建 XO 与链接 xclbin 的规则 [gesvj/Makefile:154-163](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/Makefile#L154-L163)：`$(VPP) -c ... -o $@ $^` 编 XO，`$(VPP) -l ...` 链 xclbin，与 u5-l1 三段流程一致。
- 性能与资源表 [gesvj/README.md:73-103](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/README.md#L73-L103)：README 记录「validated on Xilinx Alveo U250」、512×512/unroll16 时频率约 249MHz、kernel time 约 4686.5ms，并把资源拆成 P（平台静态区）与 K（内核动态区）两部分；精度用 LAPACK 的 `dgesvd`/`dgesvj` 做参考（见 [gesvj/README.md:74](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/README.md#L74)）。

#### 4.5.4 代码实践

1. **实践目标**：跑通（或读懂）一个 solver PL 基准，并说清它与 L1 单内核测试的差别。
2. **操作步骤**：
   - 读 [gesvj/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/README.md) 的四步流程；若有 Alveo 卡或愿意等 hw_emu，可执行：
     ```bash
     cd solver/L2/benchmarks/gesvj
     source /opt/xilinx/Vitis/2022.2/settings64.sh
     source /opt/xilinx/xrt/setenv.sh
     export PLATFORM=xilinx_u250_xdma_201830_2/xilinx_u250_xdma_201830_2.xpfm
     export TARGET=hw        # 或 hw_emu
     make run                # 耗时可能数小时（hw）
     ./build_dir.hw.xilinx_u250_xdma_201830_2/test_gesvj.exe \
        -xclbin build_dir.hw.xilinx_u250_xdma_201830_2/kernel_gesvj.xclbin \
        -runs 1 -M 512 -N 512 -seed 12
     ```
   - 无硬件时退化为源码阅读：对照 `kernel_gesvj.cpp`、`conn_u250.cfg`、`description.json`，说明数据从主机 → DDR bank0 → 内核 → DDR bank0 → 主机的往返路径。
3. **观察现象**：主机打印 `Kernel Execution time`、`Write/Read DDR Execution time`，给出端到端延迟分解。
4. **预期结果**：能看到（或从 README 读到）512×512 的 kernel time 在秒级、频率约 250MHz；这是 L1 csim 给不出的真实硬件数字。
5. 待本地验证（hw 构建需 Alveo 卡与数小时；hw_emu 可在 x86 上跑但慢）。

#### 4.5.5 小练习与答案

- **练习 1**：gesvj benchmark 与 L1 单内核测试（如 u7-l1 的 cholesky csim）在**目标**上的根本差别是什么？答案：L1 csim 是纯软件功能仿真、秒级、只验对错；benchmark 把内核编成 xclbin 在 Alveo PL 上实跑（或 hw_emu），量真实延迟/频率/资源，耗时数小时，且带完整主机程序与多 CU 并行（`MCU`/`NCU`）。
- **练习 2**：为什么说 gesvj 是 PL 路线而非 AIE？给出两个源码证据。答案：① 内核 `#include "hw/xf_solver_L2.hpp"`（`hw/` 前缀）；② 用 `extern "C"` + `#pragma HLS INTERFACE m_axi` 的 HLS DUT、`double` 类型，完全没有 ADF 图/`kernel::create_object`。
- **练习 3**：README 资源表里的 P 与 K 分别指什么？答案：P = 平台静态区（FPGA 卡固有的）资源占用，K = 内核动态区（用户内核实际使用的）资源占用，分开统计便于看内核本身的成本。

---

## 5. 综合实践

把本讲的两条路线串起来做一次「同题对照」：

1. 选 **QR 分解**这个 solver 同时有 PL 与 AIE 实现的运算。
2. **AIE 侧**：阅读 [qrd_graph.hpp:136-249](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/include/aie/qrd_graph.hpp#L136-L249) 与 [solver/L2/tests/aie/qrd/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/tests/aie/qrd/description.json)，写一段话说明：它的数据类型只支持什么？用哪个模板参数控制并行核数？目标平台是哪两块板？
3. **PL 侧**：阅读 [gesvj](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/README.md) 与 [gesvdj](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvdj/README.md) 的 README，记录：数据类型、主验证板、频率与 kernel time。
4. **产出**：画一张对照表，列出「QR/SVD 在 AIE 路线 vs PL 路线」在 *数据类型 / 并行旋钮 / 目标硬件 / 验证目录* 四个维度的差异，并据此判断：一个需要在 Versal 上做单精度复数 QR 的项目该选哪条路线？一个需要双精度高吞吐 SVD 的 Alveo 项目又该选哪条？

预期结论（待本地验证）：单精度复数、数据流友好 → AIE `qrd_graph`（vck190/vek280）；双精度、需 URAM 大容量、跑在数据中心卡 → PL `gesvj`/`gesvdj`（Alveo U250）。

## 6. 本讲小结

- solver 库对同一运算（Cholesky/QR/SVD）常同时维护 **PL HLS**（`include/hw`）与 **AIE**（`include/aie`）两套实现，文件名可能相同但属于不同编译流程与硬件，判断路线要看源码而非只看平台白名单。
- AIE cholesky 用**二维网格分块拓扑**（`TP_GRID_DIM`/`TP_X`/`TP_Y`），靠 `registerKernelClass()` 按坐标分派对角核/下三角核的不同入口函数。
- AIE qrd 用**修正 Gram-Schmidt + 级联**（`TP_CASC_LEN`），按 `CASC_IN/OUT` 四种组合偏特化出不同端口签名。
- AIE 内核沿用 `kernel.hpp + traits.hpp + utils.hpp + 运行时 .cpp` 四件拆分：前三个不含 intrinsics、参与图级编译，`.cpp` 含 intrinsics、由核级编译经 `source(...)` 引入。
- L2 图包装器（如 `qrd_graph`）负责实例化内核、连级联、接端口、做防御性 `static_assert`，是用户与测试的入口。
- `L2/benchmarks`（gesvj/gesvdj/gtsv）是 **PL HLS** 基准，在 Alveo 上量真实性能；它和 L1 单内核测试（csim，纯功能）在目标、耗时、产出上根本不同。

## 7. 下一步学习建议

- 想深入 AIE 图的运行时控制与 PL↔AIE 边界，继续学 [u13-l1 ADF 图、窗口/流与 PL↔AIE 边界](u13-l1-adf-graph-boundary.md)。
- 想系统对比「功能测试 vs 性能评测」的方法论，继续学 [u14-l3 基准评测与对标 CPU/参考](u14-l3-benchmarking.md)。
- 建议回到源码：把 [solver/L1/include/aie/svd.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/svd.hpp) 与 [solver/L1/include/aie/substitution.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/substitution.hpp) 当练习，用本讲学到的「四件拆分 + 级联/网格拓扑」框架自行分析它们的组织方式。
