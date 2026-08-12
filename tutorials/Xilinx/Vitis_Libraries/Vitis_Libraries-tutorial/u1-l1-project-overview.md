# 项目定位与加速库全景

## 1. 本讲目标

本讲是整本学习手册的第一讲，目标是让你在完全不写代码、不装工具的前提下，建立起对 **Vitis_Libraries** 这个项目的全局认知。学完后你应该能够：

- 说出 **Vitis 加速库是什么**、它解决什么问题、对开发者意味着什么价值。
- 准确列举当前仓库里的 **9 个活跃库**，并指出每个库所属的技术领域。
- 区分 **PL（FPGA）** 与 **AIE（AI Engine）** 两条加速路线，并说出它们各自对应的目标硬件。
- 识别哪些库已经 **被废弃**、哪些硬件平台已经 **不再支持**，避免在旧目录上浪费时间。

本讲不涉及任何工具链操作，全部基于阅读真实仓库文件完成，是后续所有讲义的「地图」。

## 2. 前置知识

阅读本讲前，你只需具备下面这些常识即可：

- **什么是开源软件库**：一组打包好、可以直接调用的功能集合，类似你用过的任何第三方库。
- **CPU/GPU/加速卡的区别**：CPU 通用但相对慢，GPU 擅长大规模并行，而本仓库面向的是另一类硬件——**FPGA 与 AI Engine（AIE）**。如果没接触过也没关系，本讲会用通俗语言解释。
- **什么是「硬件加速」**：把原本在 CPU 上跑的计算任务，搬到专门设计的硬件上去做，从而更快、更省电。

下面几个名词会在文中反复出现，先建立一个最简印象（后面会结合源码展开）：

| 名词 | 一句话解释 |
| --- | --- |
| **FPGA** | 一种芯片，内部逻辑可以由开发者用代码「重新连线」配置，也叫 **PL（Programmable Logic，可编程逻辑）**。 |
| **AI Engine（AIE）** | AMD Versal 芯片里专门做高性能向量/矩阵计算的处理器阵列，是另一种加速资源，和 FPGA 逻辑并列。 |
| **Alveo** | AMD 的数据中心加速卡系列（插在服务器里的 PCIe 卡），里面是 FPGA。 |
| **Versal** | AMD 的新一代自适应 SoC，同时包含 FPGA 逻辑（PL）和 AI Engine（AIE）。 |
| **内核（kernel）** | 跑在硬件上的一个计算单元，类似函数，但运行在 FPGA/AIE 上。 |

> 提示：如果你暂时分不清 PL 与 AIE，先记住一句话——**PL 是「可重新连线」的硬件电路，AIE 是「可编程」的向量计算阵列**。第 4.3 节会用源码把它们彻底讲清楚。

## 3. 本讲源码地图

本讲引用的真实仓库文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md) | 顶层 README，定义项目定位、列出废弃库与不再支持的平台。 |
| [dsp/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md) | DSP 库说明，是最能体现「同一功能在 PL 与 AIE 两条路线都实现」的例子。 |
| [vision/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md) | 视觉库说明，明确写出 PL/AIE/PL+AIE 三种内核类型与验证过的开发板。 |
| [utils/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md) | 基础工具库说明，是很多其他库的底层依赖。 |
| [dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json) | 描述 9 个库之间依赖关系的清单。 |
| [platform_map.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json) | 把「逻辑平台名」映射到具体 `.xpfm` 平台文件。 |

此外，本讲还会引用各库自身 README 的开头几行来说明它们的领域定位。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Vitis 加速库的定位与价值**（顶层 README）
- **4.2 库清单：9 个活跃库与 7 个废弃库**（库清单与废弃说明）
- **4.3 PL 与 AIE 两条加速路线及目标硬件**（目标平台）

---

### 4.1 Vitis 加速库的定位与价值

#### 4.1.1 概念说明

[AMD Vitis 统一软件平台](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vitis.html) 提供了一整套 **开源、性能已优化的加速库**。这套库的核心卖点用一句话讲就是：**「尽可能少改代码，甚至零改代码，就能让你现有的应用获得硬件加速」**。

对开发者来说，它的价值有三层：

1. **即插即用**：很多库函数可以直接像普通软件库一样调用，无需自己设计硬件。
2. **作为积木**：你可以把库函数当作「已优化的算法积木」，拼装、修改成自己的加速器。
3. **跨平台可移植**：同一套库可以部署到边缘（嵌入式）、本地数据中心、云端，无需重写加速代码。

#### 4.1.2 核心流程

从「应用想要加速」到「跑在硬件上」，Vitis 库扮演的角色可以画成下面的流程：

```text
你的应用代码 (C/C++/Python/Matlab)
        │
        │  调用 Vitis 加速库 API / kernel
        ▼
   Vitis 加速库（本仓库：9 个活跃库）
        │
        │  生成硬件设计（HLS → RTL / AIE 图）
        ▼
   AMD 硬件（Alveo 数据中心卡 / Versal / Zynq）
```

库本身被分成两大类（这点在顶层 README 里写得很清楚）：

- **通用库**：覆盖数学、线性代数、DSP 等核心计算，适用面广。
- **领域专用库**：针对视觉图像、安全、超声、电机控制等具体场景。

#### 4.1.3 源码精读

顶层 README 开篇就给出了项目定位：

> [README.md:L12-L14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L12-L14) —— 定义整个项目是「开源、性能已优化的加速库集合」，强调「开箱即用、极少甚至零代码改动」。

接着它把库分成「通用」与「领域专用」两类：

> [README.md:L18-L19](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L18-L19) —— 说明通用库覆盖数学/线性代数/DSP，领域专用库覆盖视觉图像处理、安全、超声、电机控制等。

关于「在哪些语言里用」，README 写道：

> [README.md:L39](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L39) —— 支持在 C、C++、Python、Matlab 等常用语言中使用。

关于跨平台可移植性：

> [README.md:L49](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L49) —— 这些库可在所有 AMD 平台上扩展，无缝部署到边缘、本地或云端。

另外，从 2025.2 版本起，项目还引入了一个姊妹仓库：

> [README.md:L21](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L21) —— 引入 [Vitis IP Libraries](https://github.com/Xilinx/Vitis_IP_Libraries)，作为 Vitis Libraries 的扩展，提供可同时被 Vivado IP 流程和 Vitis 库流程使用的多领域 IP。本手册后续仍以本仓库（Vitis_Libraries）为主。

#### 4.1.4 代码实践

**实践目标**：用阅读确认「项目是什么、面向谁、用什么语言」。

**操作步骤**：

1. 打开顶层 [README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md)。
2. 阅读第 12–20 行（项目定位与库分类）。
3. 跳到第 37–45 行（「Use in Familiar Programming Languages」一节）。
4. 阅读第 47–55 行（「Scalable and Flexible」一节）。

**需要观察的现象**：

- README 没有让你先安装任何工具，而是在反复强调「**少改代码、即插即用、跨平台**」三个卖点。
- 它把库分成 **通用** 和 **领域专用** 两类。

**预期结果**：你能用自己的话写出下面三句填空：

- Vitis 加速库是 ________ 的开源库集合。
- 它支持的语言至少包括 ________。
- 它强调的三点是：即插即用、________、跨平台。

（参考：性能已优化；C/C++/Python/Matlab；作为积木可定制。）

#### 4.1.5 小练习与答案

**练习 1**：顶层 README 为什么要强调「minimal to zero code changes（极少甚至零代码改动）」？这对它的目标用户意味着什么？

> **答案**：目标用户是 **应用开发者**，而非硬件工程师。强调零改动，意味着用户不必学习 FPGA/AIE 底层设计，只需像调用普通软件库那样调用 API，就能获得硬件加速。这大幅降低了硬件加速的门槛。

**练习 2**：从 README 看，Vitis 库除了「即插即用」之外，还能被高级用户怎么使用？

> **答案**：高级用户可以把库函数当作 **已优化的算法积木**，拼装、定制或作为参考来设计自己的加速器（见 README「Scalable and Flexible」一节，[L55](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L55)）。

---

### 4.2 库清单：9 个活跃库与 7 个废弃库

#### 4.2.1 概念说明

本仓库是一个 **单体多库集合（monorepo）**：一个 Git 仓库里并排放着多个独立的库。理解这一点很重要——你不会「整体使用」Vitis_Libraries，而是 **挑选其中一两个库** 来用。

当前仓库根目录下有 **9 个活跃库**。同时，从 2025.2 版本起，有 **7 个旧的 PL 库不再维护**。搞清楚「谁还活着、谁已废弃」是后续不迷路的前提。

#### 4.2.2 核心流程

仓库的实际目录结构是这样的（用 `ls -d */` 即可看到）：

```text
Xilinx-Vitis_Libraries/
├── blas/          线性代数（BLAS）
├── data_mover/    DDR↔AIE 数据搬运
├── dsp/           数字信号处理
├── motor_control/ 电机控制
├── security/      密码学
├── solver/        矩阵分解与求解器
├── ultrasound/    超声波束合成
├── utils/         流式与存储访问基础工具
├── vision/        计算机视觉（基于 OpenCV）
└── Vitis_Libraries-tutorial/   （本讲义所在目录，非库）
```

这 9 个库并不是彼此孤立的，它们之间有依赖关系，记录在 [dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json) 里。把这张依赖图画出来，是后面理解「为什么有些库要先学」的关键：

```text
utils（根，无依赖）
  ├── data_mover ── 依赖 utils
  │       └── dsp ── 依赖 utils + data_mover
  │              └── solver ── 依赖 utils + dsp
  ├── security ──── 依赖 utils
  ├── blas ─────── （独立）
  ├── motor_control ─（独立）
  ├── ultrasound ───（独立）
  └── vision ──────（独立）
```

> 一句话记忆：**utils 是地基，data_mover 和 security 直接建在 utils 上，dsp 建在 data_mover 上，solver 又建在 dsp 上。** 这条链（utils ← data_mover ← dsp ← solver）正是本手册领域库讲解顺序的依据。

#### 4.2.3 源码精读

**(1) 7 个废弃库的明确清单**——顶层 README 写得很清楚：

> [README.md:L22-L29](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L22-L29) —— 从 2025.2 起，下列 **PL 库** 不再维护：`codec`、`data_analytics`、`data_compression`、`graph`、`hpc`、`quantitative_finance`、`sparse`。

并且特别强调了一个重要边界：

> [README.md:L31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L31) —— **AI Engine（AIE）的库不受影响**，废弃的只是 PL 库。所以本手册重点放在仍活跃的 PL+AIE 库上。

**(2) 9 个活跃库各自的领域**——下面引用各库 README 开头的自述（注意：这些路径已逐一核对存在）：

| 库 | 领域 | 证据（README 自述） |
| --- | --- | --- |
| **utils** | 流式与存储访问基础工具 | [utils/README.md:L3-L6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L3-L6)：访问 DDR/HBM/URAM 内存，做数据分发、收集、重排、插入、丢弃。 |
| **data_mover** | DDR↔AIE 数据搬运 | [data_mover/README.md:L3-L4](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/README.md#L3-L4)：在 DDR 与 AIE 之间高效搬运数据。 |
| **dsp** | 数字信号处理 | [dsp/README.md:L3-L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md#L3-L7)：L1 层 HLS 实现 FFT（FPGA），L2 层 AIE 实现 DDS/FFT/FIR/GeMM/Widget。 |
| **solver** | 矩阵分解与求解器 | [solver/README.md:L1](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/README.md#L1)：Vitis Solver 库（Cholesky/LU/QR/SVD 等分解）。 |
| **blas** | 线性代数（BLAS） | [blas/README.md:L7-L9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L7-L9)：加速线性代数函数，提供三级加速。 |
| **vision** | 计算机视觉（基于 OpenCV） | [vision/README.md:L3](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L3)：100+ 内核，基于 OpenCV，面向 FPGA/AIE/SoC。 |
| **security** | 密码学 | [security/README.md:L3-L6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/README.md#L3-L6)：对称/非对称密码、密码模式、MAC、哈希函数。 |
| **motor_control** | 电机控制 | [motor_control/README.md:L3](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/README.md#L3)：FOC、SVPWM_DUTY、PWM_GEN、QEI 四个算法级 API。 |
| **ultrasound** | 超声波束合成 | [ultrasound/README.md:L4-L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/README.md#L4-L7)：超声图像处理工具箱，L1/L2/L3 三层，L3 是完整 Beamformer。 |

**(3) 库之间的依赖关系**——直接看 [dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json)，节选关键几条：

> [dependency.json:L1-L18](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L1-L18) —— `data_mover` 依赖 `utils`；`dsp` 依赖 `utils` 和 `data_mover`。

> [dependency.json:L29-L35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L29-L35) —— `solver` 依赖 `utils` 和 `dsp`。

> [dependency.json:L23-L28](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L23-L28) —— `security` 依赖 `utils`。

> [dependency.json:L12-L21](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L12-L21) 与 [L36-L47](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L36-L47) —— `motor_control`、`ultrasound`、`utils`、`vision` 依赖为空（`dependsOn: []`），其中 `utils` 自身是根依赖。

#### 4.2.4 代码实践

**实践目标**：亲手确认 9 个活跃库的存在，并依据 `dependency.json` 画出依赖图。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   ls -d */
   ```
   你应当看到 9 个库目录加上 `Vitis_Libraries-tutorial/`。
2. 打开 [dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json)，逐条读 `lib` 和 `dependsOn`。
3. 在纸上（或文本里）画出依赖树，确认 `utils` 在最底层。

**需要观察的现象**：

- 目录数正好是 9 个活跃库（不含教程目录）。
- `dependency.json` 里没有任何库依赖 `blas`、`vision`、`motor_control`、`ultrasound`——它们是「叶子」或「独立」库。

**预期结果**：你画出的依赖链应包含这条主干：`solver → dsp → data_mover → utils`，以及 `security → utils`。

**待本地验证**：若你执行 `ls -d */` 的输出与本讲列出的 9 个库不符，请检查你 checkout 的版本（本讲基于 HEAD `629b2c979`）。废弃库目录在某些旧分支里仍可能存在。

#### 4.2.5 小练习与答案

**练习 1**：下列哪个库 **不属于** 9 个活跃库？`codec`、`solver`、`vision`、`ultrasound`。

> **答案**：`codec`。它在 [README.md:L22-L29](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L22-L29) 的废弃清单里。

**练习 2**：如果我要用 `solver` 库，根据 `dependency.json`，我至少还需要引入哪些库？

> **答案**：`solver` 直接依赖 `utils` 和 `dsp`；而 `dsp` 又依赖 `utils` 和 `data_mover`。因此除 `solver` 外，至少还要 `utils`、`dsp`、`data_mover`（`utils` 被重复依赖，只引入一次）。详见 [dependency.json:L29-L35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L29-L35) 与 [L13-L18](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L13-L18)。

**练习 3**：废弃清单里说「AI Engine 库不受影响」，这句话和「7 个废弃库都是 PL 库」是什么关系？

> **答案**：二者互为印证。被废弃的 7 个库（codec 等）都是 **PL（FPGA）** 路线的库；而 **AIE** 路线的库继续维护。这说明项目未来重心在向 AIE 倾斜。（见 [README.md:L31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L31)）

---

### 4.3 PL 与 AIE 两条加速路线及目标硬件

#### 4.3.1 概念说明

这是本讲最重要的概念。AMD 的加速硬件里有 **两种性质完全不同** 的计算资源，Vitis 库因此分成两条路线：

- **PL（Programmable Logic，可编程逻辑）路线**：也就是 **FPGA**。你用 C/C++（通过 HLS 高层综合）或 HDL 描述一个电路，工具把它「编译」成芯片里真实的硬件连线。优点是极度灵活、能做任意位宽与时序的定制电路；代表硬件是 **Alveo 数据中心卡** 和 **Zynq/Zynq UltraScale+** 嵌入式芯片。
- **AIE（AI Engine）路线**：AMD **Versal** 芯片里专门的向量/矩阵处理器阵列。你用 C/C++（遵循 AIE 编程模型）写代码，它运行在固定的处理器阵列上，擅长高吞吐的定点/浮点向量化计算。代表硬件是 **Versal** 系列开发板。

> 直觉区别：**PL 像「为你的算法专门焊一块电路」，AIE 像「在一排可编程的 DSP 处理器上跑你的算法」**。前者面积/时序可极致优化，后者开发更像写软件、吞吐高。

一个库可以只走 PL、只走 AIE，或两者都有。识别某个内核走哪条路线，是阅读本仓库源码的基本功。

#### 4.3.2 核心流程

两条路线从「源码」到「硬件」的流程不同：

```text
PL 路线：  C/C++/HDL 内核 ──HLS──▶ RTL ──打包──▶ XO ──链接──▶ xclbin ──▶ FPGA
AIE 路线： C/C++ 内核 ──组成──▶ ADF 图 ──aiecompiler──▶ AIE 可执行 ──▶ Versal AIE 阵列
```

（`xclbin`、`ADF 图`、`XO` 等术语会在第 5 单元「系统构建」详细讲，这里只需知道「两条路最终产物不同」。）

目标硬件一览（结合 README 与 `platform_map.json`）：

| 路线 | 硬件家族 | 具体型号/板卡 | 状态 |
| --- | --- | --- | --- |
| **PL** | Alveo（数据中心卡） | U200、U250、U280 | **2025.2 起不再支持** |
| **PL** | Alveo（数据中心卡） | U50、U50LV、U55C | 推荐替代 |
| **PL** | Zynq / Zynq UltraScale+（嵌入式） | zcu102、zcu104 | 支持 |
| **AIE** | Versal | VCK190（`vck190` / `vck190_dfx`） | 支持 |
| **AIE-ML** | Versal | VEK280（`vek280`）、VEK385（`vek385`） | 支持 |

> 名词补充：**AIE-ML** 是 AIE 的「机器学习增强」变体，向量/矩阵能力更强，vision 库的 AIE-ML 函数主要在 VEK280 上验证。

#### 4.3.3 源码精读

**(1) 三种内核类型的最权威定义**——vision README 给出了最清晰的分类：

> [vision/README.md:L57-L59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L57-L59) —— 内核分三类：**PL [HLS/RTL]**（面向 FPGA，用 C/C++/HDL 写）、**AIE**（面向 AI Engine）、**PL+AIE**（同时面向两者）。

**(2) PL 与 AIE 在同一库中共存**——dsp 库是最好的例子，它同一个 DSP 功能在两条路线都有实现：

> [dsp/README.md:L5-L6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md#L5-L6) —— L1 层用 **HLS C++** 实现 FFT，加速 Xilinx **FPGA**；L2 层用 **AIE C++ 图** 实现 DDS、FFT、FIR、GeMM、Widget。

这条信息非常关键：**FFT 这个功能，PL 路线有（L1 HLS），AIE 路线也有（L2 AIE 图）**。后续第 6 单元会专门讲 DSP 库如何同时支撑两条路线。

**(3) 验证过的具体开发板**——vision README 明确列出：

> [vision/README.md:L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L7) —— 库已在 zcu102、zcu104、vck190、U50、U200 上验证；**AIE-ML 函数在 VEK280 上验证**。

**(4) Alveo 旧平台不再支持**——顶层 README：

> [README.md:L35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L35) —— 从 2025.2 起，U200、U250、U280 不再支持；可改用 U50、U50LV、U55C 实现性能相近的 PL 设计。

**(5) 平台名到平台文件的映射**——`platform_map.json`：

> [platform_map.json:L1-L6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6) —— 把逻辑名映射到 `.xpfm`：`vck190` → `xilinx_vck190_base_202610_1`、`vck190_dfx` → `xilinx_vck190_base_dfx_202610_1`、`vek280` → `xilinx_vek280_base_202610_1`、`vek385` → `vek385_base`。

这意味着构建时你只需写 `PLATFORM=vck190`，构建脚本就能通过这张表找到具体的平台文件（详见 u1-l2「单仓库结构与跨库配置」）。

#### 4.3.4 代码实践

**实践目标**：通过阅读，给「PL 路线」和「AIE 路线」各找至少一个真实证据。

**操作步骤**：

1. 打开 [dsp/README.md:L5-L6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md#L5-L6)，圈出体现 PL（FPGA + HLS）和 AIE（AIE C++ graph）的句子。
2. 打开 [vision/README.md:L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L7)，把开发板分成「PL 类」（zcu102/zcu104/U50/U200）和「AIE 类」（vck190/VEK280）。
3. 打开 [platform_map.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json)，确认四个 Versal 平台名。

**需要观察的现象**：

- 同一个 README（dsp）里同时出现 **FPGA/HLS** 和 **AIE graph**，证明两条路线并存。
- `platform_map.json` 里 **只有 Versal 平台**（vck190/vek280/vek385），没有 Alveo——因为 Alveo 平台走的是另一套安装方式（见 u2-l1）。

**预期结果**：你能复述下面这条结论——**「PL 跑在 FPGA 上（Alveo/Zynq），用 HLS；AIE 跑在 Versal 的 AI Engine 阵列上，用 ADF 图。FFT 在 dsp 库里两条路线都有实现。」**

#### 4.3.5 小练习与答案

**练习 1**：Alveo 的 U250 属于哪条加速路线？2025.2 之后还能用吗？

> **答案**：U250 是 **Alveo 数据中心卡**，属于 **PL（FPGA）** 路线。从 2025.2 起不再被支持（[README.md:L35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L35)），可改用 U50/U50LV/U55C。

**练习 2**：AIE-ML 函数主要在哪块板子上验证？它和普通 AIE 是什么关系？

> **答案**：在 **VEK280** 上验证（[vision/README.md:L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L7)）。AIE-ML 是 AIE 的机器学习增强变体，向量化/矩阵计算能力更强，属于 AIE 路线的升级版硬件。

**练习 3**：为什么 `platform_map.json` 里只有 vck190/vek280/vek385，而没有 U50？

> **答案**：`platform_map.json` 集中管理 **Versal（AIE）** 平台的逻辑名→`.xpfm` 映射；Alveo（如 U50）属于 PCIe 数据中心卡，其平台文件通过 `PLATFORM_REPO_PATHS` 等方式单独安装与查找（见 [utils/README.md:L50-L58](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L50-L58)），不在本表内。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「加速库全景表」的制作任务。

**任务**：通读顶层 [README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md) 与 [dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json)，为 **9 个活跃库** 各填一行，按「领域角色 / 加速路线 / 依赖」三个维度分类，最终产出一张表 + 一张依赖图。

**步骤**：

1. 用 `ls -d */` 列出 9 个库。
2. 逐个打开库的 README 开头，确定它属于哪类领域（建议分四类：**通用基础设施**、**数值计算**、**信号/图像处理**、**垂直领域**）。
3. 查 README 判断它走 **PL**、**AIE** 还是 **两者都有**。
4. 查 `dependency.json` 填它的依赖。
5. 产出一张表（参考下表，请自行补全）。

**参考分类维度**（请你根据源码自行判定，下表给出一种合理答案供核对）：

| 库 | 领域角色 | 加速路线 | 依赖 |
| --- | --- | --- | --- |
| utils | 通用基础设施 | PL（+ 为主机/其他库服务） | 无（根） |
| data_mover | 通用基础设施 | PL（桥接 DDR↔AIE） | utils |
| dsp | 信号处理 | PL + AIE | utils, data_mover |
| solver | 数值计算 | PL + AIE | utils, dsp |
| blas | 数值计算 | PL | 无 |
| vision | 图像处理 | PL + AIE + AIE-ML | 无 |
| security | 垂直领域（密码学） | PL | utils |
| motor_control | 垂直领域（电机） | PL | 无 |
| ultrasound | 垂直领域（超声） | PL | 无 |

> 说明：上表中「加速路线」一列是基于各库 README 的概括性判断，精确到「某个具体内核走哪条路线」需要进入各库 L1/L2 目录核实——这正是后续单元要做的。

**交付物**：

- 一张 9 行的全景表（领域 + 路线 + 依赖）。
- 一张依赖树图（主干为 `solver → dsp → data_mover → utils`，旁支 `security → utils`）。
- 一句话结论：**「utils 是地基，dsp/solver 是数值计算主干且同时支持 PL 与 AIE，vision 是覆盖最广（PL/AIE/AIE-ML）的领域库。」**

## 6. 本讲小结

- **Vitis_Libraries** 是 AMD Vitis 平台的开源加速库集合，主打「极少甚至零代码改动即可获得硬件加速」，支持 C/C++/Python/Matlab。
- 仓库是一个 **单体多库集合**，当前有 **9 个活跃库**：blas、data_mover、dsp、motor_control、security、solver、ultrasound、utils、vision。
- 从 2025.2 起，**7 个旧 PL 库被废弃**（codec、data_analytics、data_compression、graph、hpc、quantitative_finance、sparse），**AIE 库不受影响**。
- 库之间有明确依赖（[dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json)），主干链是 **utils ← data_mover ← dsp ← solver**，`utils` 是多数库的根依赖。
- 加速有 **两条路线**：**PL（FPGA，用 HLS）** 跑在 Alveo/Zynq 上；**AIE（用 ADF 图）** 跑在 Versal（VCK190/VEK280/VEK385）上，AIE-ML 主要在 VEK280 验证。
- Alveo 旧平台 **U200/U250/U280 已不再支持**，改用 **U50/U50LV/U55C**；`platform_map.json` 维护 Versal 平台名到 `.xpfm` 的映射。

## 7. 下一步学习建议

本讲只建立了「地图」，还没进入任何目录。建议下一步：

- **紧接学习 u1-l2《单仓库结构与跨库配置》**：深入看 `library.json`、`dependency.json`、`platform_map.json`、`Jenkinsfile` 是如何把 9 个库组织起来、如何被 CI 识别的。
- **再学 u1-l3《L1/L2/L3 设计哲学与 PL/AIE 两种范式》**：理解贯穿所有库的 **L1/L2/L3 三层抽象**（原语→内核→应用），这是后续每一篇领域库讲义都会用到的通用心智模型。
- 在进入第 2 单元「环境搭建与首次运行」之前，**不需要安装任何工具**——你可以先纯阅读完成 u1 的三篇讲义。
