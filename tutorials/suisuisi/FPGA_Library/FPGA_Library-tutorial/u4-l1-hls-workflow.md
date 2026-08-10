# HLS 概念与 Vivado HLS 工作流

## 1. 本讲目标

本讲是 **Unit 4（HLS 高层综合与图像处理）** 的入口。前面三个单元我们都在读「手写的 Verilog/VHDL」（HDL），从本讲开始切换到一种完全不同的 FPGA 设计范式——**高层综合（High-Level Synthesis, HLS）**：用 C/C++ 写算法，让工具自动生成 RTL。

本讲**不讲中值滤波算法本身的细节**（那是下一讲 u4-l2 的任务），只解决四个问题：

1. 什么是 HLS？它和手写 HDL 有什么区别？
2. Vivado HLS 的「C 仿真 → 综合 → 协同仿真」三阶段工作流分别做什么？
3. 一个 Vivado HLS 工程由哪些文件组成？工程描述文件 `vivado_hls.app` 写了什么？
4. 本项目里 `MedianFilter.h`（数据类型约定）和 `main_test.c`（测试激励）如何组织？

学完后，你应该能看懂任意一个 Vivado HLS 工程的目录结构，说出顶层函数是谁、测试平台是哪个文件、三个仿真阶段各自的目的。

## 2. 前置知识

阅读本讲前，建议你已经了解（来自 u1-l2）：

- **FPGA**：可现场编程的芯片，内部是大量查找表（LUT）、触发器、BRAM、DSP 等可重构资源。
- **HDL（硬件描述语言）**：Verilog/VHDL，直接描述每个时钟周期电路怎么动，是「逐周期」地把硬件画出来。
- **IP 核**：可复用的硬件模块，可被打包进 Vivado 工程。
- **bitstream / 综合 / 实现**：把 RTL 变成可烧进 FPGA 的比特流的过程。

本讲新增的核心术语：

- **HLS（高层综合）**：把 C/C++ 函数自动翻译成 RTL 的工具流程。
- **顶层函数（top function）**：HLS 把哪一个 C 函数当作「整个硬件模块」来综合。
- **testbench（测试平台）**：含 `main()` 的 C 程序，负责喂数据、调用顶层函数、检查结果；它**不会被综合成硬件**。
- **csim / 综合报告 / cosim（协同仿真）**：HLS 的三个关键阶段，详见 4.1。
- **任意精度数据类型（arbitrary-precision type）**：如 `uint8_t`，让 HLS 知道这个数只用 8 根线，从而节省硬件资源。

一个最关键的直觉差别先记住：

> **手写 HDL = 你亲自安排每个时钟周期；HLS = 你写 C 算法 + 加优化指令（pragma），工具替你安排周期。**

## 3. 本讲源码地图

本讲涉及的关键文件都在 `HLS/2D-median-filter-algorithm-HLS/` 目录下，这是一个用 Vivado HLS 实现的 **2D 中值滤波器**（图像去噪）工程，源自第三方项目（仓库 fork 自 `13hanu/2D-median-filter-algorithm-HLS`，许可证 Apache-2.0）。

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| `README.md` | 项目说明：性能目标、工作流、最终结果 | 是（读目标与结果） |
| `vivado_hls.app` | Vivado HLS 工程描述文件（XML）：声明顶层函数、源文件、测试平台、solution | 是（重点） |
| `MedianFilter.h` | 数据类型与图像尺寸宏定义、函数原型 | 是 |
| `main_test.c` | C 测试平台：读 CSV、调滤波、比对结果 | 是 |
| `MedianFilter.c` | 算法实现（ZeroPad / 滑窗 / 排序） | 否（留给 u4-l2） |
| `noisy.csv` / `clean.csv` | 加噪图像 / 干净图像的像素数据（各 242 行 × 308 列） | 否（当作数据文件） |
| `LICENSE` | Apache-2.0 许可证 | 否 |

> 注意：这个目录是 Vivado HLS GUI 工程「压平」后的快照。真实的 GUI 工程里源文件通常放在以工程名命名的子目录中，所以 `vivado_hls.app` 里会出现形如 `Median_TwoD_FilterWithPragmas/MedianFilter.c` 的相对路径（见 4.2.3）。在当前仓库里这些文件被铺平到了同一层。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先建立 HLS 概念与三阶段工作流（4.1），再依次精读 `vivado_hls.app`（4.2）、`MedianFilter.h`（4.3）、`main_test.c`（4.4）。

### 4.1 高层综合（HLS）概念与三阶段工作流

#### 4.1.1 概念说明

**高层综合（High-Level Synthesis, HLS）** 是一种「升维」的设计方式：你不再用 Verilog 一拍一拍地写状态机，而是用 C/C++ 描述算法行为，再由工具（本项目用的是 Xilinx **Vivado HLS**）自动把它综合成寄存器传输级（RTL）Verilog。

为什么要这样做？

- **开发速度快**：图像处理、密码学这类算法，C 代码比等价的 Verilog 短得多、好读得多。
- **算法可先验证**：C 可以直接用普通编译器（gcc）跑，确认逻辑正确后再丢给 HLS。
- **探索空间大**：改一条优化指令（pragma）就能让工具生成结构完全不同的硬件，方便在「快」和「省」之间权衡。

代价是：HLS 生成的 RTL 通常比顶级手工 RTL 略大、略慢，且对 C 代码写法有严格约束（不能有动态内存分配、不能有递归、要能静态确定循环边界等）。

本项目正是 HLS 的典型场景——**2D 中值滤波**：纯数据并行算法，用 C 写非常自然。

#### 4.1.2 核心流程

Vivado HLS 的标准工作流分三个阶段，对应 README 里描述的步骤：

```
        ① C 仿真 (csim)            ② 高层综合 (Synthesis)          ③ 协同仿真 (Co-simulation)
   ┌─────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────┐
   │ 用 gcc 编译 C 代码   │      │ 把顶层函数综合成 RTL      │      │ 用生成的 RTL 跑一遍       │
   │ 跑 testbench，确认   │  ──> │ 产出：RTL Verilog +       │  ──> │ testbench，确认综合后的   │
   │ 算法功能正确         │      │ 时延(Latency)/资源报告    │      │ 行为和 C 仿真一致         │
   └─────────────────────┘      └──────────────────────────┘      └──────────────────────────┘
                                        │
                                        └──> (可选) 导出为 IP，放进 Vivado 块设计
```

- **① C 仿真（csim）**：纯软件层面验证算法对不对。testbench 的 `main()` 被执行，确认「输入加噪图 → 输出干净图」逻辑成立。这一步**不产生任何硬件**，只是保证 C 没写错。
- **② 高层综合（Synthesis）**：HLS 工具读顶层函数，把它翻译成 RTL，并给出两份关键报告：
  - **时延报告（Latency）**：处理一份数据要多少个时钟周期。
  - **资源报告（Utilization）**：用了多少 BRAM、DSP、LUT、FF（占片上资源百分比）。
- **③ 协同仿真（Co-simulation / cosim）**：把第②步生成的 RTL 放进仿真器，**用同一个 testbench 再跑一次**，自动比对 RTL 输出和 C 仿真输出是否一致。这是「综合有没有改变行为」的最后一道关。

三阶段的顺序不可颠倒：功能没验证就去综合没有意义；综合后不 cosim 就不敢相信生成的 RTL。

#### 4.1.3 源码精读：性能目标 vs 实际结果

性能目标写得很清楚，要求在 **3 毫秒内**去噪、占用 **少于 25%** 的可编程逻辑（PL）资源：

[README.md:L3-L3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/README.md#L3-L3) —— 「denoise the test image in less than 3 milliseconds while consuming less than 25 percent of the available PL resources」。

但 README 结尾的「Results」一节给出的**实际达成**是：

[README.md:L48-L48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/README.md#L48-L48) —— 「denoising in less than **12 milliseconds** with an overall PL resource utilization of approximately **13%**」。

这里有一个**诚实的落差**，值得记住：

| 指标 | 目标 | 实际达成 | 是否达标 |
|------|------|----------|----------|
| 时延 | < 3 ms | < 12 ms | ❌ 未达标（慢了约 4 倍） |
| 资源占比 | < 25% | ≈ 13% | ✌️ 达标（且有富余） |

也就是说，这个工程**资源控制得很好，但速度没达到最初设定的激进目标**。这在 HLS 项目里很常见——pragma 调优、循环展开、流水线化都会在「快」和「省」之间拉扯。README 的「Usage」一节也提到「you may need to adjust the HLS pragmas in the code for optimal performance based on the specific FPGA board」（[README.md:L45-L45](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/README.md#L45-L45)），把进一步优化留给使用者。

> 注：README 里 `Overview`/`Usage` 描述的三步（C 仿真、综合、Co-simulation）正是 4.1.2 的三个阶段，见 [README.md:L41-L43](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/README.md#L41-L43)。

#### 4.1.4 代码实践

**实践目标**：把本项目的性能目标和三阶段工作流用自己的话整理一遍。

**操作步骤**：

1. 打开 [README.md](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/README.md)，找到「目标」与「Results」两处描述。
2. 打开 [main_test.c](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/main_test.c)，确认 testbench 调用了哪个顶层函数。

**需要观察/产出的内容**：

1. 写出本项目的两个性能目标（时延、资源占比）。
2. 写出 README 报告的实际结果，并判断是否达标。
3. 用一段话描述「C 仿真 → 综合 → 协同仿真」三步各自的目的和先后顺序。

**预期结果**：你能得到一张类似 4.1.3 的「目标 vs 实际」对照表，并说清三个阶段不能调换顺序的原因。

> 本实践属「源码阅读型」，无需运行 Vivado HLS；运行 C 仿真的实操放在下一讲 u4-l2。

#### 4.1.5 小练习与答案

**练习 1**：为什么 HLS 的 C 仿真（csim）必须在综合（Synthesis）之前完成？

> **答案**：csim 是用普通 C 编译器验证算法逻辑是否正确。如果 C 本身就算错，综合出来的 RTL 一定也错，且综合耗时长得多。先 csim 能用最低成本把功能 bug 挡在门外。

**练习 2**：README 的目标时延是 3ms，实际是 12ms。在 HLS 里，想缩短时延通常会动哪类东西？

> **答案**：会动**优化指令（pragma/directive）**，比如对循环加 `PIPELINE`（流水线，让多个迭代重叠执行）、`UNROLL`（循环展开，增加并行度）、`ARRAY_PARTITION`（拆分数组让多个端口同时访问）。这些指令会让设计变快，但通常也会增加资源占用——这正是 4.1.3「目标 vs 实际」落差的来源。

---

### 4.2 工程描述文件 vivado_hls.app

#### 4.2.1 概念说明

Vivado HLS 用一个工程文件来记录「这个工程里有哪些文件、顶层函数是谁、用哪个 solution」。这个文件的扩展名是 `.app`，内容其实是 **XML 文本**（不是二进制，尽管扩展名容易让人误解）。

你可以把它类比成 Vivado 普通工程的 `.xpr` 文件、或者 VSCode 的 `.code-workspace`：它本身不是源码，而是一份「工程清单」。打开 Vivado HLS GUI 时，它就靠这份清单把工程还原出来。

#### 4.2.2 核心流程

`.app` 文件的关键信息可以分成四组：

```
┌─────────────────────────────────────────────────────────────┐
│ <project name="..." top="...">   ← ① 工程名 + 顶层函数      │
│   <Simulation> <SimFlow name="csim"/> </Simulation>          │
│                                  ← ② 仿真流程配置            │
│   <files>                       ← ③ 文件清单                 │
│     <file ... tb="1" .../>      ←    tb="1" = 测试平台       │
│     <file ... tb="false" .../>  ←    tb="false" = 设计源/数据│
│   </files>                                                  │
│   <solutions>                   ← ④ 方案列表（每套优化一组） │
│     <solution status="active"/>                            │
│   </solutions>                                              │
│ </project>                                                  │
└─────────────────────────────────────────────────────────────┘
```

其中最重要的概念是 **solution（方案）**：HLS 允许你对同一个设计维护多套不同的优化指令组合，每套叫一个 solution，可以并行比较它们的时延/资源报告。同一时刻只有一个 `status="active"` 的方案在被使用。

#### 4.2.3 源码精读

工程名和顶层函数声明在根元素上——顶层函数就是 `TwoD_MedianFilter`：

[vivado_hls.app:L1-L1](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/vivado_hls.app#L1-L1)

```xml
<project xmlns="com.autoesl.autopilot.project"
         name="Median_TwoD_FilterWithPragmas" top="TwoD_MedianFilter">
```

> 名字空间 `com.autoesl.autopilot` 是 Vivado HLS 的历史遗留（HLS 技术源自 AutoESL 公司，后被 Xilinx 收购），GUI 内部仍叫 AutoPilot。工程名带 `WithPragmas`，暗示这个版本是「加了优化指令」的版本。

仿真流程配置记录了上次跑过 C 仿真：

[vivado_hls.app:L4-L6](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/vivado_hls.app#L4-L6) —— `<SimFlow name="csim" .../>`。

文件清单是本节最关键的部分，它区分了「会被综合的源文件」和「只用于测试的 testbench」：

[vivado_hls.app:L7-L13](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/vivado_hls.app#L7-L13)

逐行解读：

- **第 8 行** `main_test.c`：属性 `tb="1"` 表示它是 **testbench（测试平台）**——含 `main()`，综合时会被排除，只在 csim/cosim 时编译运行。注意它的编译选项 `-Wno-unknown-pragmas`：让普通 C 编译器忽略 `#pragma HLS ...` 这类它不认识的指令，否则 csim 会报大量警告。
- **第 9 行** `MedianFilter.c`：`tb="false"`，这是**真正会被综合成硬件的算法源码**。
- **第 10 行** `MedianFilter.h`：头文件，同样会被综合。
- **第 11–12 行** `clean.csv` / `noisy.csv`：被标记为工程内的数据文件（虽然它们不参与编译，但 GUI 会把它们算作工程成员）。

> 路径细节：`main_test.c` 写成 `../main_test.c`（在工程目录的上一层），而设计文件写成 `Median_TwoD_FilterWithPragmas/MedianFilter.c`（在子目录里）。这正是 4.1.3 提到的「GUI 工程原始布局」的痕迹——这个仓库把文件压平后，这些相对路径不再对得上，重新打开工程时需要修正。

最后是 solution 列表，本工程保留了两套方案：

[vivado_hls.app:L14-L17](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/vivado_hls.app#L14-L17) —— `solution1` 标为 `inactive`、`solution2` 标为 `active`，说明作者迭代过至少两版优化。

#### 4.2.4 代码实践

**实践目标**：学会从 `.app` 文件快速提取「顶层函数 / testbench / 设计源 / 当前 solution」。

**操作步骤**：

1. 打开 [vivado_hls.app](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/vivado_hls.app)。
2. 找到 `top="..."`，写出顶层函数名。
3. 在 `<files>` 里找出唯一一个 `tb="1"` 的文件，写出它的名字和作用。
4. 找出 `status="active"` 的 solution 名字。

**需要观察的现象**：你会确认 testbench（`main_test.c`）和设计源（`MedianFilter.c`）是**两个不同角色**的文件，靠 `tb` 属性区分。

**预期结果**：得到一张「文件 → 角色」对照表：

| 文件 | tb 属性 | 角色 |
|------|---------|------|
| main_test.c | `tb="1"` | 测试平台（不综合） |
| MedianFilter.c | `tb="false"` | 设计源（被综合） |
| MedianFilter.h | `tb="false"` | 设计头文件 |
| clean.csv / noisy.csv | `tb="false"` | 数据文件 |

#### 4.2.5 小练习与答案

**练习 1**：为什么 `main_test.c` 必须标成 `tb="1"`，而不能当作普通设计源？

> **答案**：testbench 含 `main()` 函数和大量 `printf`/文件 IO（`fopen`/`fscanf`）。如果把它当设计源综合，HLS 会报错——硬件不能有操作系统级的文件 IO，也不能有两个入口。标成 `tb="1"` 后，综合时它被排除，只在仿真时编译运行。

**练习 2**：`.app` 里有两个 solution，一个 active 一个 inactive。这种「多 solution」机制对设计者有什么用？

> **答案**：让你对同一份 C 代码尝试多套不同的优化指令（比如 solution1 不加流水线、solution2 全部加流水线），分别综合后**横向对比**它们的时延和资源报告，从而选出最合适的一套，而不必删掉旧的尝试。

**练习 3**：`-Wno-unknown-pragmas` 这个编译选项解决什么问题？

> **答案**：HLS 源码里可能出现 `#pragma HLS ...` 指令。C 仿真用普通 gcc 编译，gcc 不认识这些 `HLS` 指令会报警告甚至错误。加这个选项让 gcc 忽略未知的 pragma，保证 csim 顺利进行。

---

### 4.3 顶层函数与数据类型约定（MedianFilter.h）

#### 4.3.1 概念说明

HLS 的世界里，**头文件里的 typedef 和宏定义不只是给程序员看的——它们直接决定综合出来的硬件长什么样**。这是 HLS 和纯软件 C 最大的区别之一：

- 写 `int`（通常 32 位）→ HLS 会生成 32 位宽的数据通路。
- 写 `uint8_t`（8 位）→ HLS 只用 8 根线，省掉 3/4 的资源。这就是 4.2 提到的「任意精度数据类型」的价值。

类似地，一个固定尺寸的二维数组，在 HLS 里会被推断成一块**存储器（RAM）**：综合时每个 `PIXEL_VAL_T[242][308]` 数组往往对应若干块 BRAM。

`MedianFilter.h` 正是用宏定义固定图像尺寸、用 typedef 规定像素位宽，从而让整个设计在综合时尺寸完全确定。

#### 4.3.2 核心流程

头文件定义了一套贯穿全工程的「尺寸 + 类型」约定：

```
原始图像尺寸 IMAGE_ROWS × IMAGE_COLUMNS (242 × 308)
        │
        │  零填充（每边 +1 行 +1 列）
        ▼
填充后尺寸 PADDED_IMG_ROWS × PADDED_IMG_COLS (244 × 310)
        │
        │  3×3 滑窗扫描
        ▼
滤波后图像 = 原始尺寸 (242 × 308)，每个像素 = 窗口内 9 个值的中位数

类型约定：
  PIXEL_VAL_T        = uint8_t            （每个像素 8 位）
  IMG_ARR_T          = 242×308 的 uint8 二维数组
  PADDED_IMG_ARR_T   = 244×310 的 uint8 二维数组
```

#### 4.3.3 源码精读

图像尺寸用宏固定（这些值会决定综合时数组大小、循环次数）：

[MedianFilter.h:L3-L8](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/MedianFilter.h#L3-L8)

```c
#define IMAGE_ROWS 242
#define IMAGE_COLUMNS 308
#define PADDED_IMG_ROWS (IMAGE_ROWS + 2)
#define PADDED_IMG_COLS (IMAGE_COLUMNS + 2)
#define WindowSize 3
#define Window_Array_Size 9
```

- `PADDED = 原始 + 2`：因为 3×3 窗口在图像边缘会「伸出」一格，所以四周各补 1 行/1 列零（总共 +2 行 +2 列），这样每个原始像素都有完整的 9 邻居。
- `WindowSize 3` / `Window_Array_Size 9`：3×3 窗口共 9 个像素。

像素类型与数组类型别名——**这是影响硬件资源最关键的两行**：

[MedianFilter.h:L10-L12](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/MedianFilter.h#L10-L12)

```c
typedef uint8_t PIXEL_VAL_T;
typedef PIXEL_VAL_T IMG_ARR_T[IMAGE_ROWS][IMAGE_COLUMNS];
typedef PIXEL_VAL_T PADDED_IMG_ARR_T[PADDED_IMG_ROWS][PADDED_IMG_COLS];
```

`uint8_t` 来自第 1 行的 `#include <stdint.h>`，是 8 位无符号整数。灰度图像的像素值范围是 0–255，用 8 位刚好——这正是「任意精度」思想的体现：用 8 位而不是默认的 32 位 `int`，让数据通路窄 4 倍。

顶层函数原型（综合入口）：

[MedianFilter.h:L14-L14](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/MedianFilter.h#L14-L14)

```c
void TwoD_MedianFilter(IMG_ARR_T Image_with_Noise, IMG_ARR_T Filtered_Image);
```

这个签名就是 4.2 里 `top="TwoD_MedianFilter"` 指向的函数。它有两个数组参数：输入 `Image_with_Noise`、输出 `Filtered_Image`。在综合时，每个数组参数通常会变成一组 **AXI4 内存映射接口**或 BRAM 端口——即硬件上「数据从哪里来、到哪里去」的物理连线。

#### 4.3.4 代码实践

**实践目标**：理解「宏尺寸 + typedef」如何同时约束软件和硬件。

**操作步骤**：

1. 打开 [MedianFilter.h](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/MedianFilter.h)。
2. 计算 `IMG_ARR_T` 一个数组有多少个元素、共占多少字节（按 `uint8_t`）。
3. 思考：如果要把像素改成 12 位精度（例如 `uint16_t`），需要改哪一行？对资源会有什么影响？

**需要观察的现象**：图像尺寸 242×308 是写死的；一旦改宏，循环次数、数组大小、综合报告都会跟着变。

**预期结果**：

- 元素数 = 242 × 308 = 74,536 个像素；按 `uint8_t` 占 74,536 字节（≈72.8 KiB）。
- 改成 `uint16_t` 只需把 `typedef uint16_t PIXEL_VAL_T;`，但数据通路宽一倍，BRAM/DSP/LUT 占用都会上升。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `PIXEL_VAL_T` 定义成 `uint8_t` 而不是 `int`？对硬件有什么好处？

> **答案**：灰度像素范围 0–255，8 位足够。用 `int`（32 位）会让所有加法器、比较器、存储数据通路都宽 4 倍，白白浪费 LUT/FF/BRAM。用 `uint8_t` 让综合出的硬件窄、省资源——这正是 README「efficient data type management with arbitrary precision data types」的含义。

**练习 2**：`PADDED_IMG_ROWS` 为什么是 `IMAGE_ROWS + 2` 而不是 `+1`？

> **答案**：3×3 窗口以某像素为中心时，会访问它上、下、左、右各 1 格。处理第一行像素需要它上方那一行，处理最后一行需要下方那一行——上下各补 1 行，总共 +2 行。左右同理 +2 列。这样 242×308 补成 244×310，每个原始像素都有完整 9 邻居。

---

### 4.4 C 测试平台与自校验机制（main_test.c）

#### 4.4.1 概念说明

**testbench（测试平台）** 是 HLS 工程里「不被综合、只用来验证」的程序，它含一个普通的 `main()`。一个好的 testbench 应该做到**自校验（self-checking）**：跑完自动告诉你「通过」还是「失败」，而不是靠人眼盯波形。

本项目就是一个自校验 testbench 的范例，它的巧妙之处在于准备了**两份输入**：

- `noisy.csv`：加了噪声的图像（喂给滤波器）。
- `clean.csv`：原始干净图像（当作「黄金参考答案」）。

滤波后拿结果和 `clean.csv` 逐像素比对——一致就算通过。这样 csim 和 cosim 都能自动判对错。

#### 4.4.2 核心流程

testbench 的执行流程是线性的「导入 → 处理 → 比对」三步：

```
main()
  │
  ├─① CSV_Import(noisy.csv) ──> Image_with_Noise   (读入加噪图)
  ├─① CSV_Import(clean.csv) ──> Clean_Image        (读入干净图，作黄金参考)
  │
  ├─② TwoD_MedianFilter(Image_with_Noise, Filtered_Image)   (调用被测顶层函数)
  │
  └─③ Validate_output(Filtered_Image, Clean_Image)          (逐像素比对，打印通过/失败)
```

数据格式：CSV 每行是一行像素，像素值用逗号分隔（例如 `225,225,225,226,...`），共 242 行，每行 308 个值——正好对应 `IMAGE_ROWS × IMAGE_COLUMNS`。

#### 4.4.3 源码精读

`CSV_Import` 用 `fscanf` 按整数逐个读入像素，存进二维数组：

[main_test.c:L11-L31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/main_test.c#L11-L31)

关键片段：

```c
for (int i = 0; i < IMAGE_ROWS; i++) {
  for (int j = 0; j < IMAGE_COLUMNS; j++) {
    fscanf(Import_Data, "%d,", &temp);
    Image_with_Noise[i][j] = temp;
  }
  fscanf(Import_Data, "\n");   // 每读完一行跳过换行
}
```

> 注意：这是 **testbench 里的代码**，用了 `fopen/fscanf/fclose`。这些文件操作只能出现在 testbench（`tb="1"`）里，绝不能写进会被综合的 `MedianFilter.c`——硬件没有文件系统。

`Validate_output` 实现自校验：逐像素比对实际值与期望值，统计不匹配数：

[main_test.c:L36-L54](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/main_test.c#L36-L54)

```c
if (no_of_mismatches == 0) {
  printf("\n Validation success b/w Actual Vs Expected\n");
} else {
  printf("\n [Error] Validation unsuccessfull, no. of mismatches = %d\n", ...);
}
```

> 注意第 39 行内层循环上有个标签 `Validate_output_label0:`。这种 `xxx_labelN:` 是 Vivado HLS 自动加的**循环锚点标签**——GUI 里给某条循环加优化指令（directive）时，工具就靠这个标签定位循环。它本身对 C 编译无任何影响。`MedianFilter.c` 里也有同类的 `Sort_label2`、`Window_and_Sort_label33`、`Zeros_label9` 等。

`main()` 把三步串起来：

[main_test.c:L57-L66](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/main_test.c#L57-L66)

```c
CSV_Import(Image_with_Noise, "noisy.csv");
CSV_Import(Clean_Image, "clean.csv");
TwoD_MedianFilter(Image_with_Noise, Filtered_Image);   // 唯一被综合的调用
Validate_output(Filtered_Image, Clean_Image);
```

这四行就是整个 csim/cosim 的全部驱动逻辑：读两份数据、跑一次滤波、自动判对错。

> **关于「pragma」的诚实说明**：工程名叫 `WithPragmas`、README 也声称「Employs HLS pragmas」，但通读 `MedianFilter.c` 和 `main_test.c` **找不到任何 `#pragma HLS ...` 语句**（只有循环标签）。这说明这些优化指令很可能是以 **directive 文件**（存在 solution 目录里的 `.tcl`，本仓库未包含）形式通过 GUI 添加的，而不是写死在源码里。这提醒我们：判断一个 HLS 工程的优化，不能只看 `.c`，还要看 solution 的 directive。

#### 4.4.4 代码实践

**实践目标**：读懂自校验 testbench 的数据流，理解 csim 如何自动判对错。

**操作步骤**：

1. 打开 [main_test.c](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/main_test.c)。
2. 跟踪 `main()` 里四行调用，画出 `noisy.csv → Image_with_Noise → Filtered_Image → 与 Clean_Image 比对` 的数据流图。
3. 在 `noisy.csv`（[noisy.csv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HLS/2D-median-filter-algorithm-HLS/noisy.csv)）里挑第一个像素值，对照 `CSV_Import` 的读取逻辑，确认它会被存到 `Image_with_Noise[0][0]`。

**需要观察的现象**：

- testbench 完全靠 `printf` 输出「success / unsuccessfull」判对错，csim 跑完看终端这一行即可。
- 比对的「答案」来自 `clean.csv`，不是写死在代码里的常数。

**预期结果**：你能解释「为什么这个 testbench 是自校验的」——因为它自带黄金参考（clean.csv），跑完自动报告通过与否，无需人工看波形。

> 若本地有 Vivado HLS，可在 GUI 里对该工程点 Run → C Simulation，终端会打印上述比对结果；本讲不要求实跑（实跑放在 u4-l2）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Validate_output` 里用 `clean.csv` 作为期望值，而不是直接判断「像素值在某个范围内」？

> **答案**：因为中值滤波的「正确输出」是确定的——它应该尽可能还原出原始干净图。用 `clean.csv` 当黄金参考是最严格的逐像素正确性判据，比「值在 0–255」这种宽松判断可靠得多。

**练习 2**：如果把 `main()` 里 `TwoD_MedianFilter(...)` 这一行注释掉，csim 会怎样？

> **答案**：`Filtered_Image` 不会被赋值（其内容是未初始化的全局数组，全 0），`Validate_output` 会发现几乎每个像素都和 `Clean_Image` 不匹配，打印 `[Error] Validation unsuccessfull, no. of mismatches = ...`（数量接近全部像素）。这正是自校验的价值——它立刻暴露问题。

**练习 3**：`Validate_output_label0:` 这种标签在 C 语言里是什么？在 HLS 里又有什么额外用途？

> **答案**：在标准 C 里它只是一个普通的循环标签（label），对编译和运行完全无影响。在 Vivado HLS 里，它是 GUI 添加 directive（优化指令）时的**定位锚点**——你给某条循环加 PIPELINE/UNROLL 指令时，工具靠这个标签指名道姓地说「优化这个循环」。

---

## 5. 综合实践

**任务：为这个 HLS 工程写一份一页纸的「工程说明书」。**

把本讲四个模块串起来，整理出一份包含以下小节的文档（纯阅读，不需运行工具）：

1. **工程定位**：用一句话说明这是什么算法、用什么范式实现、来源与许可证。
2. **顶层函数与文件角色**：写出顶层函数名；列出 `vivado_hls.app` 中每个文件的角色（testbench / 设计源 / 数据），并解释靠哪个属性区分。
3. **数据约定**：根据 `MedianFilter.h` 写出图像尺寸、像素位宽、填充后尺寸；并说明「为什么用 `uint8_t`」。
4. **验证机制**：根据 `main_test.c` 画出 `noisy.csv → 滤波 → 与 clean.csv 比对` 的数据流，并说明它是自校验的。
5. **性能与工作流**：列出目标 vs 实际（时延、资源）对照表；写出 csim → 综合 → cosim 三阶段的目的和顺序。
6. **可疑点**：记录你发现的「工程名带 Pragmas 但源码无 `#pragma`」这一现象，并给出你的解释。

**交付物**：一份 Markdown 或手写笔记，覆盖上述 6 点。

**自我检查**：如果你的说明书能让一个没读过本项目的同学，在不打开 Vivado HLS 的情况下就说出「顶层函数是谁、testbench 怎么判对错、性能是否达标」，这份实践就算完成。

## 6. 本讲小结

- **HLS 是「升维」设计范式**：用 C/C++ 写算法，工具（Vivado HLS）自动生成 RTL，开发快、易验证、易探索优化空间，代价是资源和时延通常略逊于顶级手工 RTL。
- **三阶段工作流不可颠倒**：C 仿真（csim，验证功能）→ 高层综合（生成 RTL + 时延/资源报告）→ 协同仿真（cosim，验证 RTL 行为与 C 一致）。
- **性能有诚实落差**：本项目目标 < 3 ms / < 25% 资源，实际达成 ≈ 12 ms / 13%——资源达标，时延未达激进目标，是 pragma 调优「快 vs 省」拉扯的典型结果。
- **`.app` 是 XML 工程清单**：它声明顶层函数（`TwoD_MedianFilter`）、用 `tb="1"/"false"` 区分 testbench 与设计源、用 solution 管理多套优化方案。
- **头文件即硬件约束**：`MedianFilter.h` 里 `uint8_t` 像素类型、固定图像尺寸，直接决定综合后的数据通路宽度和存储大小。
- **testbench 是自校验的**：`main_test.c` 用 `clean.csv` 当黄金参考，逐像素比对并自动打印通过/失败；它含文件 IO，只能作为 testbench、不能被综合。
- **诚实警示**：源码中只有循环标签、没有内联 `#pragma HLS`，所谓「pragmas」很可能以 solution directive 形式存在（本仓库未包含）；判断 HLS 优化要看 directive，不能只看 `.c`。

## 7. 下一步学习建议

本讲只建立了 HLS 的**概念骨架和工程地图**，还没碰算法实现。下一讲 **u4-l2《2D 中值滤波 C 算法实现》** 会逐函数精读 `MedianFilter.c`：

- `ZeroPad` 如何补零；
- `Window_and_Sort` 如何做 3×3 滑窗；
- `Sort` 如何排序取中值（`Window[4]`）；
- 以及如何在本地用 gcc 实跑一次 C 仿真，观察去噪效果。

之后 **u4-l3《HLS 优化与资源/时延权衡》** 会承接本讲提到的「pragma 调优」话题，讲清 `PIPELINE`/`UNROLL`/`ARRAY_PARTITION` 等指令如何把 12 ms 往 3 ms 的目标推。

建议在进入 u4-l2 前，先完成本讲第 5 节的综合实践，确保你已经能在脑海里把「顶层函数 → testbench → 数据流 → 性能目标」这条线串起来。
