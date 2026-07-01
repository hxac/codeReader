# CoralNPU 是什么：三核一体的 ML 加速器

## 1. 本讲目标

本讲是整本《CoralNPU 项目学习手册》的第一篇，面向**完全没有接触过 CoralNPU** 的读者。读完本讲，你应当能够：

- 说清楚 CoralNPU 是什么、解决什么问题、用在什么场景；
- 画出 CoralNPU「三核一体」的整体结构：**标量核（scalar）/ 向量核（vector, SIMD）/ 矩阵引擎（matrix, MAC）**三者如何分工协作；
- 解释 CoralNPU 为什么选择 RV32IMF_Zve32x 这套 RISC-V 指令集，以及「四发射、乱序退休」是什么意思；
- 指出 ITCM/DTCM、Cache、AXI 总线在整个系统里的位置。

本讲只读三份文档，不写代码、不跑仿真。目标只有一个：**先建立一张全景图**，让后续每一篇讲义都有落脚点。

---

## 2. 前置知识

本讲默认你了解下面这些概念。如果你对某一项陌生，下面用一两句话帮你补上。

- **处理器 / CPU（中央处理器）**：从内存里取出指令、解释指令、做计算并写回结果的电路。一条 RISC-V 指令通常是 32 位。
- **指令集架构 ISA（Instruction Set Architecture）**：软件和硬件之间的「合同」，规定了有哪些指令、有哪些寄存器。CoralNPU 用的是 RISC-V 这个开源 ISA。
- **机器学习推理（ML inference）**：把一个已经训练好的模型（比如 MobileNet）跑起来，输入数据、输出预测结果。CoralNPU 专做「推理」，不做「训练」。
- **NPU（Neural Processing Unit，神经网络处理器）**：专门为神经网络计算（尤其是大量的乘加运算）设计的加速器，可以理解为「AI 专用 CPU」。
- **乘加运算 MAC（Multiply-Accumulate）**：神经网络里最常见的操作，`a = a + b * c`。一次 MAC 就是「乘一下再累加」。神经网络里的算力几乎都用 MAC 来衡量。
- **SIMD（Single Instruction Multiple Data，单指令多数据）**：一条指令同时处理多个数据，比如一条指令同时对 8 个数做加法。CoralNPU 里「向量（vector）」和「SIMD」是同一个意思。
- **SoC（System-on-Chip，片上系统）**：把 CPU、加速器、外设、总线等全部塞进一颗芯片。
- **AXI 总线**：一种芯片内部组件之间（以及芯片与外部之间）传输数据的「高速公路」标准，由 ARM 制定，工业界广泛使用。

不需要现在就精通这些概念——它们会在本讲和后续讲义里反复出现，你会越看越熟。

---

## 3. 本讲源码地图

本讲只涉及三份**文档型源码**（不是 RTL 代码，而是项目自带的设计说明文档）。这三份文档是理解整个项目的「总纲」。

| 文件 | 作用 | 在本讲的地位 |
|------|------|--------------|
| [README.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md) | 项目的「门面」：一句话定位 + 核心特性清单 + 快速上手命令 | 给出 CoralNPU 的定位与关键参数 |
| [doc/overview.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md) | 高层架构总览：标量核 / 向量核 / MAC / Cache 各自怎么设计 | 三核一体设计的核心说明 |
| [doc/microarch/microarch.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/microarch/microarch.md) | 微架构：流水线有几个阶段、每类指令要几个周期 | 解释流水线与指令延迟 |

> 提示：本讲会把这三份文档当作「事实来源（source of truth）」。后面所有讲义里关于具体 RTL 实现的细节，最终都可以追溯到这三份文档里的描述。

---

## 4. 核心概念与源码讲解

### 4.1 项目定位：CoralNPU 是一颗开源 NPU IP

#### 4.1.1 概念说明

先回答最基本的问题：**CoralNPU 到底是什么？**

CoralNPU 是 Google Research 设计的一颗**面向 ML 推理的硬件加速器**，它本身是一个**开源 IP**（Intellectual Property，可复用的硬件设计），可以被集成到面向**超低功耗 SoC** 的芯片里，目标设备是**可穿戴设备**：智能耳机（hearables）、AR 眼镜、智能手表等。

关键词有三个，逐一拆解：

- **NPU / 加速器**：不是通用 CPU，而是专门为神经网络里海量的乘加运算「定制」的电路。
- **基于 RISC-V**：它建立在开源的 RISC-V 指令集之上，而不是 ARM 或 x86。这意味着任何人都可以自由使用、研究、修改它。
- **超低功耗 SoC IP**：它不是一颗你能买到的「成品芯片」，而是一份设计，可以被别的公司「装进」自己的芯片里。

#### 4.1.2 核心流程

从「问题」到「CoralNPU 的回答」，可以串成一条逻辑链：

1. **问题**：可穿戴设备要在很小的电池、很低的功耗下，本地跑机器学习推理（比如语音唤醒、手势识别）。
2. **难点**：通用 CPU 跑这种推理太耗电、太慢；专用 ASIC 又贵、又难复用。
3. **CoralNPU 的回答**：用开源、可复用的 RISC-V IP，针对 ML 数据流做**专门的微架构定制**，做到「低功耗 + 够用 + 可集成」。

一句话总结它的定位：**CoralNPU = 一颗开源的、RISC-V 架构的、为 ML 推理做了深度定制的 NPU IP，目标是塞进可穿戴设备的 SoC 里。**

#### 4.1.3 源码精读

README 开头三句话就把定位说清楚了：

[README.md:3-7](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L3-L7) —— 这段代码（其实是文档）说明：CoralNPU 是面向 ML 推理的硬件加速器，是 Google Research 设计的开源 IP，面向超低功耗 SoC（智能耳机、AR 眼镜、智能手表），并且它是基于 32 位 RISC-V ISA 的 NPU，由三个处理器组件协同工作（matrix / vector / scalar）。

紧接着的「Features」清单是理解整个项目的**参数速查表**：

[README.md:12-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L12-L23) —— 这段列出了 CoralNPU 的核心特性：指令集、地址空间、流水线、发射宽度、SIMD/向量宽度、ITCM/DTCM 容量、AXI 接口等。这张表在本讲后面会被逐条拆开讲。

#### 4.1.4 代码实践

这是一个**纯阅读型实践**，目的是让你亲手把定位刻进脑子。

1. **实践目标**：用自己的话复述 CoralNPU 的定位。
2. **操作步骤**：
   - 打开 [README.md:3-7](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L3-L7)，读三句话。
   - 在这三句话里圈出 4 个关键词：`hardware accelerator for ML inferencing` / `Open Source IP` / `ultra-low-power SoCs` / `RISC-V ISA`。
3. **需要观察的现象**：你会发现这三句话没有出现任何「具体频率」「具体工艺」「具体型号」，而是反复强调「目标场景」和「可复用性」。
4. **预期结果**：你能写出一句不超过 30 字的话，同时覆盖「ML 推理 / 开源 / 可穿戴 / RISC-V」四个要点。
5. **待本地验证**：无（纯文档阅读）。

#### 4.1.5 小练习与答案

**练习 1**：CoralNPU 是「通用 CPU」还是「专用加速器」？请说明依据。

> **参考答案**：是专用加速器（NPU）。依据是 README 第 3 行明确写它是 `hardware accelerator for ML inferencing`，并且 overview.md 第 3-4 行说它的微架构决策「与 ML 加速器的数据流属性对齐（align with the dataplane properties of an ML accelerator）」。

**练习 2**：CoralNPU 是一颗你可以直接买到的「成品芯片」吗？

> **参考答案**：不是。它是开源 IP（Open Source IP），是一份可被别人集成进自己 SoC 的设计，目标平台是「ultra-low-power System-on-Chips」。

---

### 4.2 三核一体：标量 / 向量 / 矩阵如何分工协作

#### 4.2.1 概念说明

CoralNPU 最核心的设计思想是**「三核一体（fused design）」**：它不是「一个处理器干所有事」，而是**三个处理器组件协同**：

| 组件 | 名称 | 主要职责 |
|------|------|----------|
| **标量核（scalar）** | Scalar Core | 跑「指挥官」程序：循环控制、地址生成、条件判断；驱动后端 |
| **向量核（vector / SIMD）** | Vector Core | 做并行的向量运算：一条指令处理一批数据 |
| **矩阵引擎（matrix / MAC）** | MAC Outer-Product Engine | 做神经网络里最关键的「乘累加」外积运算，是算力担当 |

关键洞察来自 overview.md 的第一段：**CoralNPU 的设计「从矩阵（matrix）能力出发，再叠加向量（vector）和标量（scalar）能力，形成一个融合设计」**。也就是说，CoralNPU 的「灵魂」是矩阵引擎，其余部分都是为它服务的——这一点和普通 RISC-V CPU 完全相反（普通 CPU 以标量运算为中心）。

#### 4.2.2 核心流程

三者之间的协作关系可以用下面这张「指挥链」来理解：

```
┌─────────────────────────────────────────────────────────────┐
│                    标量核（Scalar Core）                       │
│   跑 run-to-completion 程序：循环、地址生成、控制流            │
│   译码后，把「向量/矩阵指令」塞进命令队列（command queue）     │
└───────────────────────────────┬─────────────────────────────┘
                                 │  向量/矩阵指令（经 FIFO 解耦）
                                 ▼
┌─────────────────────────────────────────────────────────────┐
│              向量核（Vector Core / SIMD）                      │
│   64 个 256 位向量寄存器 v0..v63                              │
│   等依赖关系解除后，再发射到真正的计算单元                     │
└───────────────┬───────────────────────────────┬─────────────┘
                │ ALU/浮点等                     │ MAC 外积引擎
                ▼                                ▼
        向量算术（vadd 等）              矩阵乘累加（vdot 等）
                                        acc<8><8> 累加器，256 MACs/周期
```

要点：

1. **标量核是「指挥官」**，它本身不做重活，只负责把工作「派发」给后端的向量/矩阵计算单元。
2. **标量核和后端之间用 FIFO 解耦**：标量核可以提前把一堆向量指令塞进缓冲区，后端按自己的节奏消化。
3. **矩阵引擎（MAC）是「主力工人」**，神经网络里的卷积、全连接最终都会映射到它身上。

#### 4.2.3 源码精读

**（1）三核一体的设计起点**：

[doc/overview.md:1-6](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L1-L6) —— 这段开宗明义：CoralNPU 是一颗用自定义 SIMD 指令和定制微架构构建的 RISC-V CPU，其微架构决策与 ML 加速器的数据流属性对齐；**设计从矩阵（matrix）能力开始，再叠加向量和标量能力，形成融合设计**。

**（2）标量核：一个简单的 RISC-V 前端，驱动后端命令队列**：

[doc/overview.md:12-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L12-L23) —— 这段说明：标量核是一个简单的 RISC-V 标量前端，驱动 ML+SIMD 后端的命令队列；它用自定义的 rv32im 前端，跑「run-to-completion（一口气跑完，没有 OS、没有中断）」模型所需的最小指令集；它是一台**顺序（in-order）、不投机（no speculation）**的机器。

> 名词解释：
> - **run-to-completion**：程序启动后一口气跑完，不需要操作系统调度，也不依赖中断。CoralNPU 标量核就是这种「死磕到底」的执行模型。
> - **in-order（顺序）**：指令按程序顺序派发。
> - **no speculation（不投机）**：不会像高端 CPU 那样「猜着跑」，省掉了投机执行的硬件开销。

**（3）标量核的分支预测策略**：

[doc/overview.md:25-27](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L25-L27) —— 这段给出一条非常简单（但很巧妙）的分支策略：取指阶段对**向后跳转（backwards）一律当作「跳（taken）」**，对**向前跳转（forward）一律当作「不跳（not-taken）」**；如果执行结果和取指阶段的决定不一致，就付出一个「惩罚周期」。为什么巧妙？因为典型的循环 `for` 就是一条「向后跳转」，预测成「跳」正好命中。

**（4）向量核：FIFO 解耦 + 64 个 256 位寄存器**：

[doc/overview.md:34-46](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L34-L46) —— 这段说明：标量前端和后端用一个 FIFO 结构解耦，FIFO 缓存向量指令，**只有当向量寄存器堆里的依赖关系解除后**，才把指令投递到对应的命令队列；向量核支持 8/16/32 位数据宽度。下方的寄存器表列出：64 个向量寄存器 `v0..v63`，每个 256 位（例如可装 8 个 int32）；还有一个累加器 `acc<8><8>`，是 8×8 个 32 位的阵列。

**（5）矩阵引擎：量化外积乘累加，每周期 256 MACs**：

[doc/overview.md:48-61](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L48-L61) —— 这段是整个项目的「算力核心」说明：CoralNPU 的核心组件是一个**量化外积乘累加引擎（quantized outer product MAC engine）**；外积结构提供**二维广播**——一轴是并行广播（「wide」，比如卷积权重），另一轴是若干 batch 的转移输入（「narrow」，比如 MobileNet 的 XY batch）。具体实现是把多个 VDOT 操作码纵向排列，每个 VDOT 用 4 个 8 位乘法归约进 32 位累加器，最终**每周期完成 256 次 MAC**。

> 名词解释：
> - **外积（outer product）**：两个向量的外积产生一个矩阵。CoralNPU 用「wide × narrow」的广播结构，让一份权重同时和一批输入做乘法，最大化「计算量 / 访存量」的比值——这正是 ML 加速器的核心追求。
> - **量化（quantized）**：用低位宽（如 8 位）整数近似表示原本的浮点权重，省电省面积。
> - **VDOT**：一种「点积」操作码，4 个 8 位乘 → 32 位累加。

**（6）Stripmining：一条指令变四条**：

[doc/overview.md:63-72](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L63-L72) —— 这段讲 **stripmining（条带挖掘）**：把「基于数组的并行」折叠成「硬件能提供的并行」。编码里显式内置了一个 stripmine 机制，把**一次前端派发**变成**四次串行的 SIMD 发射**。例子很直观：派发阶段的 `vadd v0`，到了发射阶段会变成 `vadd v0 : vadd v1 : vadd v2 : vadd v3`，当作四个独立事件处理。这是 CoralNPU 用「少的派发压力」换取「多的硬件并行」的关键技巧。

#### 4.2.4 代码实践

这是一个**画图型实践**，帮你把三核关系可视化。

1. **实践目标**：画出标量核 → 命令队列（FIFO）→ 向量核/MAC 的协作关系图。
2. **操作步骤**：
   - 重读 [doc/overview.md:12-72](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L12-L72)。
   - 在纸上（或任意画图工具）画出三个方框：Scalar Core、Vector Core、MAC Engine。
   - 用箭头标出：标量核通过什么（命令队列/FIFO）把指令送给后端；向量核和 MAC 分别产出什么结果。
3. **需要观察的现象**：你会注意到，标量核**不直接**做矩阵乘法，它只是「发号施令」。
4. **预期结果**：得到一张类似本讲 4.2.2 小节那张「指挥链」的图。
5. **待本地验证**：无（纯文档阅读 + 画图）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 overview.md 说 CoralNPU「设计从 matrix 开始」？这和普通 RISC-V CPU 有什么本质区别？

> **参考答案**：因为 CoralNPU 的目标场景是 ML 推理，而 ML 推理的算力瓶颈在大量乘累加（MAC）上，所以矩阵引擎是它的「灵魂」，向量和标量都是为支撑矩阵引擎而叠加的。普通 RISC-V CPU 以标量运算为中心，没有专门的矩阵乘累加引擎。

**练习 2**：标量核和后端之间为什么要有 FIFO？

> **参考答案**：为了**解耦**。标量核可以快速地把多条向量指令塞进 FIFO，不必等后端算完；后端则按自己的节奏（等向量寄存器堆里的依赖关系解除后）再消化指令。这样标量核的派发不会被后端的慢计算拖住。

**练习 3**：`acc<8><8>` 是什么？它有多少个 32 位累加器？

> **参考答案**：它是 MAC 引擎的累加器阵列，组织成 8×8 的结构，所以共有 64 个 32 位累加器（8×8 = 64）。依据见 [doc/overview.md:42-45](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L42-L45)。

---

### 4.3 流水线与指令执行模型

#### 4.3.1 概念说明

理解了「三核一体」之后，接下来看**标量核自己怎么跑指令**——这就是**流水线（pipeline）**。

流水线是处理器的「生产流水线」：把一条指令的执行拆成几个阶段，每个阶段用独立的硬件，这样多条指令可以像「流水」一样重叠执行，提高吞吐率。

CoralNPU 标量核的关键特性（来自 README）：

- **指令集**：`rv32imf_zve32x_zicsr_zifencei_zbb`（这是 RISC-V 的一组扩展，本讲只需知道它包含整数 M、浮点 F、向量 Zve32x 等）。
- **派发宽度**：标量四发射（Four-way scalar）、向量双发射（two-way vector）——即每个周期最多派发 4 条标量指令或 2 条向量指令。
- **派发 / 退休模型**：**顺序派发（in-order dispatch）、乱序退休（out-of-order retire）**。

#### 4.3.2 核心流程

microarch.md 把标量核的流水线描述为「能每周期派发最多 4 条指令的顺序流水线」，并明确列出三个阶段：

1. **取指（Instruction fetch）**：从内存取出指令，放进指令缓冲（instruction buffer）。
2. **译码 / 派发（Decode/Dispatch）**：对指令缓冲里前 4 条指令译码；互锁（interlock）和记分板（scoreboard）逻辑判断本周期哪些能派发；能派发的转发给各自的执行单元。
3. **执行 / 写回（Execute/Writeback）**：执行单元从寄存器堆读操作数并计算；结果可以在同一周期写回寄存器堆。

> 名词解释：
> - **互锁 interlock**：当一条指令需要的资源/数据还没准备好时，硬件让它「等一等」，避免出错。
> - **记分板 scoreboard**：一张记录「哪个寄存器的值正在被生产/谁在等它」的表，用来判断指令之间有没有数据冒险（dependency）。
> - **顺序派发、乱序退休**：派发时严格按程序顺序；但「完成/提交」（retire/退休）可以不按顺序——只要结果正确，先算完的可以先退休。这是 CoralNPU「in-order dispatch, out-of-order retire」的含义。

**指令延迟**也是流水线理解的关键：不同指令执行时间不同，microarch.md 给了一张表。

#### 4.3.3 源码精读

**（1）指令集与发射/退休特性**：

[README.md:15-19](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L15-L19) —— 这几行列出：完整指令集是 `rv32imf_zve32x_zicsr_zifencei_zbb`；处理器是「Four-stage processor, in-order dispatch, out-of-order retire」；发射宽度是「Four-way scalar, two-way vector dispatch」；SIMD 128 位、向量 256 位（future）。

> 关于「几级流水线」的诚实说明：README 把它概括为「Four-stage processor（四级）」，而 microarch.md 详细描述为「三个阶段（fetch / decode-dispatch / execute-writeback）」。两份文档在「级数」措辞上略有出入。本讲以 microarch.md 的**三阶段**作为详细模型来讲解（因为它把 execute 和 writeback 合并描述为一个阶段）。你在后续阅读 RTL 时，可能会看到更细的划分（比如把 writeback 单独算一级），届时以具体实现为准。

**（2）三阶段流水线描述**：

[doc/microarch/microarch.md:5-18](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/microarch/microarch.md#L5-L18) —— 这段是流水线的权威描述：CoralNPU 基础处理器是「顺序（in-order）」流水线，每周期最多派发 4 条指令；并逐条解释了取指、译码/派发、执行/写回三个阶段，还点明「有些执行单元需要多个周期」。

**（3）各类指令的延迟表**：

[doc/microarch/microarch.md:22-31](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/microarch/microarch.md#L22-L31) —— 这张表给出每种执行单元的延迟：ALU 1 周期、CSR 1 周期、BRU（分支）1 周期、MLU（乘法）2 周期、DVU（除法）**可变**周期、LSU（访存）**2 周期以上**。

为了方便记忆，把这张表转成中文版：

| 指令类型 | 延迟（周期） | 含义 |
|----------|------------|------|
| ALU（算术逻辑） | 1 | add、sub、xor 等基本运算 |
| CSR（状态寄存器） | 1 | 读写 CSR 的指令 |
| BRU（分支跳转） | 1 | bge、jal、ebreak 等控制流 |
| MLU（乘法） | 2 | mul、mulh 等 |
| DVU（除法） | 可变 | div、rem 等（除法慢，周期数不固定） |
| LSU（访存） | 2+ | lw、sw 等（取决于是否命中） |

#### 4.3.4 代码实践

这是一个**追踪型实践**，帮你理解「乱序退休」。

1. **实践目标**：理解为什么「顺序派发、乱序退休」是合理的。
2. **操作步骤**：
   - 假设程序里有两条指令先后派发：第一条是 `div`（DVU，可变延迟，假设要 20 周期），第二条是紧跟其后的 `add`（ALU，1 周期）。这两条指令之间**没有数据依赖**。
   - 对照 [doc/microarch/microarch.md:22-31](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/microarch/microarch.md#L22-L31) 的延迟表。
3. **需要观察的现象**：`add` 1 周期就算完了，但 `div` 还在算。
4. **预期结果**：你能解释——因为两条指令无依赖，`add` 完全可以先于 `div`「退休」（提交结果），这就是「乱序退休」带来的吞吐提升。如果要求严格顺序退休，`add` 就得傻等 `div` 算完，白白浪费周期。
5. **待本地验证**：无（思维实验，基于文档延迟表推理）。

#### 4.3.5 小练习与答案

**练习 1**：CoralNPU 标量核是「投机执行（speculation）」的吗？

> **参考答案**：不是。overview.md 第 23 行明确说标量核是「in order machine with no speculation」。它只有非常简单的分支预测（向后跳 taken、向前跳 not-taken），不会像高端 CPU 那样投机地乱跑指令。

**练习 2**：标量核每个周期最多能派发几条标量指令？几条向量指令？

> **参考答案**：每周期最多 4 条标量指令（Four-way scalar）、2 条向量指令（two-way vector）。依据是 [README.md:18](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L17-L19)。

**练习 3**：为什么 DVU（除法）的延迟标成「可变（Variable）」而不是一个固定数？

> **参考答案**：因为除法常用迭代算法实现，所需周期数取决于操作数的具体数值（比如被除数/除数的大小、是否需要对齐等），所以无法用一个固定周期数描述。详见后续讲义对 Dvu/IDiv 的讲解。

---

### 4.4 内存与总线：ITCM / DTCM、Cache 与 AXI

#### 4.4.1 概念说明

处理器要干活，必须有「放指令的地方」和「放数据的地方」。CoralNPU 的内存子系统有四块：

1. **ITCM（Instruction TCM）**：紧耦合指令存储，8 KB。
2. **DTCM（Data TCM）**：紧耦合数据存储，32 KB。
3. **L1I Cache / L1D Cache**：一级指令/数据缓存（访问 ITCM/DTCM 之外的共享 SRAM 时用）。
4. **AXI4 总线接口**：对外既是 manager（主动发起传输）又是 subordinate（被外部 CPU 配置）。

> 名词解释：
> - **TCM（Tightly-Coupled Memory，紧耦合存储）**：和处理器核心挨得特别近的 SRAM，**单周期**就能访问。它和 Cache 的区别是：TCM 的内容是「确定放好的」（程序员/链接器决定），不会像 Cache 那样被动态替换、不会 miss，所以延迟可预测，非常适合嵌入式实时系统。
> - **Cache（缓存）**：自动缓存「最近用过的」数据，访问外部内存时能加速，但可能 miss（命中失败），延迟不可预测。
> - **AXI manager / subordinate**：AXI 协议里，主动发起读写的一方叫 manager（旧称 master），被动响应的一方叫 subordinate（旧称 slave）。

#### 4.4.2 核心流程

CoralNPU 的内存层次可以这样理解：

```
              ┌──────────── 内核（Core）────────────┐
              │   取指 ←── ITCM (8KB, 单周期)        │
              │   访存 ←── DTCM (32KB, 单周期)       │
              │            │                        │
              │            └──（miss 时）→ L1 Cache  │
              └────────────────┬────────────────────┘
                               │ AXI4（manager / subordinate 双角色）
                               ▼
                  外部内存 / 外部 CPU 配置端口
```

设计取舍的核心思想是：**标量核要的指令/数据尽量进 TCM（快、可预测）；Cache 只是 TCM 之外的补充层，且要尽量小，以免拖慢后端计算流水线**。overview.md 里有一句很关键的话：L1 Cache 和标量前端「对后端计算流水线来说是开销（overhead），理想情况下应尽可能小」。

#### 4.4.3 源码精读

**（1）ITCM / DTCM 容量与单周期特性**：

[README.md:20-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L20-L23) —— 这几行列出：8 KB ITCM（指令紧耦合存储）、32 KB DTCM（数据紧耦合存储），两者都是**单周期延迟的 SRAM，比 Cache 更高效**；并且提供 AXI4 总线接口，同时充当 manager 和 subordinate，既能访问外部内存，也能被外部 CPU 配置。

**（2）Cache 的定位与容量**：

[doc/overview.md:74-91](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L74-L91) —— 这段详细描述 Cache：Cache 是核心和第一级共享 SRAM 之间的单层缓存；L1 Cache 和标量前端「对后端计算流水线是开销，应尽可能小」。具体参数：
- **L1I Cache**：8 KB（256 位块 × 256 槽），4 路组相联。
- **L1D Cache**：16 KB（SIMD 256 位），4 路组相联，**双 bank 架构**（每个 bank 8 KB，类似 L1I），支持一定程度的「下一行预取」；在嵌入式场景下，当只有一个外部存储端口时，L1D Cache 还能把一半的内存带宽让给 ML 外积引擎。还支持「按行 / 全表刷写」，刷写时内核会 stall（停顿）直到完成，以简化契约。

> 这一段透出一个重要的工程哲学：**Cache 在 CoralNPU 里是「配角」**，它的存在主要是为了补 TCM 不够用的场景，并且设计上尽量不给主力计算（MAC）添堵。

#### 4.4.4 代码实践

这是一个**对照型实践**，帮你记住内存层次。

1. **实践目标**：把 TCM 和 Cache 的参数填进一张表，并解释各自的定位。
2. **操作步骤**：
   - 打开 [README.md:20-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L20-L23) 和 [doc/overview.md:74-91](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L74-L91)。
   - 自制一张表，列：`存储 / 容量 / 延迟特点 / 定位`。
3. **需要观察的现象**：你会看到 TCM 强调「单周期、可预测」，Cache 强调「尽量小、是开销」。
4. **预期结果**：得到类似下表的结论。

   | 存储 | 容量 | 延迟特点 | 定位 |
   |------|------|---------|------|
   | ITCM | 8 KB | 单周期 SRAM | 放指令，可预测 |
   | DTCM | 32 KB | 单周期 SRAM | 放数据，可预测 |
   | L1I Cache | 8 KB / 4 路 | 可能 miss | 补充层，尽量小 |
   | L1D Cache | 16 KB / 双 bank / 4 路 | 可能 miss | 补充层，兼顾 ML 带宽 |

5. **待本地验证**：无（纯文档对照）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CoralNPU 用 TCM 而不是只用 Cache？

> **参考答案**：TCM 是单周期、不会 miss 的 SRAM，延迟可预测，比 Cache 更高效（README 第 22 行原话）。对于嵌入式实时、run-to-completion 的执行模型，可预测的单周期访问比 Cache 的「可能很快、可能 miss」更合适，程序员/链接器可以把关键代码/数据显式放进 TCM。

**练习 2**：CoralNPU 的 AXI 接口同时是 manager 和 subordinate，分别意味着什么？

> **参考答案**：作为 **manager**，CoralNPU 可以主动发起 AXI 传输，访问外部内存；作为 **subordinate**，外部 CPU 可以通过 AXI 来配置 CoralNPU（比如写它的控制寄存器、加载程序）。依据见 [README.md:23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L20-L23)。

**练习 3**：L1D Cache 的「双 bank」设计有什么好处？

> **参考答案**：双 bank（每 bank 8 KB）带来两个好处：一是支持一定程度的「下一行预取」，提升命中率；二是当只有一个外部存储端口时，L1D Cache 能把一半的内存带宽让给 ML 外积引擎（MAC），缓解 MAC 的访存压力。依据见 [doc/overview.md:82-91](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L74-L91)。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿任务**（这就是本讲规格里指定的实践任务）：

**任务：画出 CoralNPU 的全景关系图，并用一句话写出它和普通 RISC-V CPU 的关键区别。**

操作步骤：

1. **重读两份核心文档**：[README.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md) 和 [doc/overview.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md)。
2. **画一张图**，必须包含以下元素，并标注它们之间的关系：
   - 三大处理器组件：**scalar / vector (SIMD) / matrix (MAC)**；
   - 内存：**ITCM（8KB）/ DTCM（32KB）**，以及 **L1I/L1D Cache**；
   - 总线：**AXI4**（标出 manager / subordinate 双角色）；
   - 标量核到后端的 **FIFO 命令队列** 解耦关系；
   - MAC 引擎的 **wide / narrow** 广播输入与 **acc<8><8>** 累加器、**256 MACs/周期**。
3. **写一句话**：CoralNPU 和普通 RISC-V CPU 的关键区别是什么？

**预期成果**：

- 一张标注清晰的全景图（手绘或工具均可）。
- 一句区别说明，参考答案：**CoralNPU 是以矩阵乘累加（MAC）引擎为「灵魂」的融合设计（标量核只是驱动后端的指挥官），而普通 RISC-V CPU 以标量运算为中心、没有专用的矩阵/向量后端。**

> 这个综合实践不需要运行任何命令，但它产出的那张图，会成为你阅读后续所有讲义时反复回看「定位」的地图，请认真画。

---

## 6. 本讲小结

- **CoralNPU 的定位**：Google Research 设计的开源 NPU IP，面向 ML 推理，目标是塞进超低功耗可穿戴 SoC，基于 32 位 RISC-V（`rv32imf_zve32x_zicsr_zifencei_zbb`）。
- **三核一体**：标量核（指挥官，run-to-completion、不投机）、向量核（64 个 256 位寄存器，FIFO 解耦）、矩阵 MAC 引擎（外积乘累加，256 MACs/周期）协同；设计从 matrix 出发，再叠加 vector 和 scalar。
- **流水线**：顺序派发、乱序退休；每周期最多 4 条标量 / 2 条向量派发；分支策略为「向后 taken、向前 not-taken」。
- **指令延迟**：ALU/CSR/BRU 1 周期，MLU 2 周期，DVU 可变，LSU 2 周期以上。
- **内存与总线**：ITCM 8KB / DTCM 32KB 单周期 SRAM 为主角，L1I 8KB / L1D 16KB 双 bank Cache 为配角；AXI4 同时作 manager 和 subordinate。
- **关键区别**：CoralNPU 以矩阵 MAC 引擎为核心，而普通 RISC-V CPU 以标量运算为核心。

---

## 7. 下一步学习建议

本讲建立了全景图，但**还没有真正碰过代码**。建议按这个顺序继续：

1. **下一讲 u1-l2《仓库目录结构总览》**：先学会在 995 个文件里找到北——分清哪些是 Chisel 源码、哪些是 SystemVerilog、哪些是软件/验证，建立代码地图。
2. **再下一讲 u1-l3《Bazel 构建系统与快速上手》**：动手用 Bazel 跑通第一个仿真和二进制，获得「我成功跑起来过」的正反馈。
3. **之后进入单元 2**：学工具链、链接脚本，亲手写一个 CoralNPU 的 C++ 程序并在 Verilator 仿真器上运行。
4. **想提前感受「三核一体」如何落到代码**：可以提前扫一眼 [doc/overview.md:48-61](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/overview.md#L48-L61)（MAC 段），但具体 RTL（`rvv_backend_mulmac.sv` 等）建议留到单元 7 再精读。

记住：**本讲的所有结论，都来自 README、overview.md、microarch.md 三份文档**。后续讲义会带你逐层验证这些结论是如何用真实的 Chisel / SystemVerilog 代码实现的。
