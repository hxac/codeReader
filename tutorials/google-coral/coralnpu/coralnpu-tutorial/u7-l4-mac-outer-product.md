# MAC 外积乘累加引擎

## 1. 本讲目标

本讲是「RVV 向量/矩阵后端」系列的第四篇，承接 u7-l3（向量寄存器堆、ROB 与退休）。在 u7-l3 里我们看到：派发出去的向量微操作（uop）会被各执行单元（PU）乱序算完，再由 ROB 按序退休、写回向量寄存器堆（VRF）。本讲专门拆解其中**最重、也最能代表 CoralNPU「ML 加速器」身份**的那个执行单元——**MAC 外积乘累加引擎**。

学完本讲，你应当能够：

1. 用自己的话说清「**外积（outer product）**」为什么是一台 ML 加速器的灵魂：它如何用**二维广播**把「算力 / 访存比」拉到最高。
2. 把架构文档里的「**VDOT：4 个 8 位乘 → 1 个 32 位累加**」「**每周期 256 MACs**」这两句口号，**逐条对应到真实 SystemVerilog 源码**上，并指出文档与实际构建的一处差异。
3. 读懂 `rvv_backend_mulmac`（顶层包装/仲裁）与 `rvv_backend_mac_unit`（核心引擎）两级模块，看懂「**4×4 字节外积网格**」如何用同一张乘法阵列同时服务 8/16/32 位三种元素宽度。
4. 解释 MAC 的「累加器」到底住在哪里——它为什么**不是一个独立的寄存器堆**，而是**复用向量寄存器 `vd`（以 `vs3` 的身份读出旧值）**。
5. 推演一次「wide 权重广播 × narrow 输入」在硬件里如何流动，并标注累加器如何被更新、最终如何写回 VRF。

---

## 2. 前置知识

本讲假设你已读过 u7-l1（RVV 后端总览）、u7-l2（RVV 译码与派发）、u7-l3（VRF/ROB/退休）。下面补充几个本讲会用到的术语与直觉：

- **MAC（Multiply-Accumulate，乘累加）**：一次「乘法再加到累加器」的操作 \(a \leftarrow a + b \times c\)。ML 推理（尤其是卷积、全连接）的算力几乎全来自 MAC，所以 NPU 的吞吐通常用「每秒多少 MAC」或「每周期多少 MAC」来衡量。
- **外积（outer product）**：给两个向量 \(\vec{w}\)（长 \(m\)）和 \(\vec{x}\)（长 \(n\)），外积是一个 \(m \times n\) 矩阵，第 \(i,j\) 项是 \(w_i \cdot x_j\)。注意它只要 \(m+n\) 个输入就能产生 \(m \cdot n\) 个乘积——这就是「广播」的威力。
- **量化（quantization）**：把原本 32 位浮点权重/激活压缩成 8 位整数（int8），乘积再用 32 位累加器收集。CoralNPU 的 MAC 引擎就是为 int8 量化算子量身打造的。
- **VDOT**：架构文档里对「4 个 8 位乘法、归约进 1 个 32 位累加器」这一基本积木的称呼（点积式归约）。注意：RTL 里并没有一条叫 `VDOT` 的指令，它由标准的 RVV 乘累加指令族（`vmacc`/`vnmsac`/`vmadd`/`vnmsub` 等）实现，详见 4.5。
- **EEW（Effective Element Width，有效元素位宽）**：一条向量指令当前操作的元素宽度，取 8/16/32 位。一条 256/128 位的向量寄存器，按 EEW 不同被切成不同数量的 lane。
- **wide / narrow（宽轴 / 窄轴）**：架构文档对外积两个输入轴的命名。「wide」通常是卷积权重（一批同时参与），「narrow」是转置移位后的输入（若干 batch，如 MobileNet 的 XY batching）。
- **`edff` / `cdffr`**：项目自定义寄存器原语。`edff` 是「带使能的 D 触发器」（`.e` 使能、`.d` 入、`.q` 出）；`cdffr` 是「带使能与同步清零的 D 触发器」（`.c` 清零，本讲里被 `trap_flush_rvv` 复位流水线用）。

> **关于「文档与实现差异」的提醒（重要）**：`doc/overview.md` 写「Vector 256 bits」「performing 256 MACs per cycle」，但实际构建（FPGA / UVM / VCS / cocotb / Chisel 的 BUILD 文件）**统一选用 `VLEN_128`**，即每向量寄存器 128 位（这一点 u7-l3 已确认）。本讲涉及的「每周期 MAC 数」因此会随 `VLEN` 线性变化：文档引用的是 `VLEN=256` 的数字，**实际芯片是 `VLEN=128`**。我们会在 4.3 给出一个干净公式把两者统一起来，读源码时请以宏定义与 BUILD 为准。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/overview.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md) | 架构愿景：把 MAC 描述为「量化外积乘累加引擎」，提出 wide/narrow 双轴广播、VDOT、每周期 256 MACs、`acc<8><8>` 累加器等概念。 |
| [hdl/verilog/rvv/design/rvv_backend_mulmac.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mulmac.sv) | **顶层包装**：实例化 2 个 MAC 执行单元，并在两个保留站槽位与两个单元之间做仲裁/pop。 |
| [hdl/verilog/rvv/design/rvv_backend_mac_unit.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv) | **核心引擎**：把操作数拆成字节、用 4×4 外积网格做 8×8 乘、按 EEW 做 schoolbook 归约、再做乘累加（加减到 `vs3`），两拍流水。 |
| [hdl/verilog/rvv/design/rvv_backend_mul_unit_mul8.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mul_unit_mul8.sv) | **叶子乘法器**：一个带符号控制的 8×8 → 16 位乘法，是整个引擎里唯一真正「算乘法」的单元，被大量复制。 |
| [hdl/verilog/rvv/design/rvv_backend_mul_unit.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mul_unit.sv) | **纯乘法兄弟单元**：与 MAC 单元共享同一片 `mul8` 叶子与同样的 4×4×4 网格，但**不累加**（只做 `vmul`/`vsmul` 等）。用来对照理解「乘」与「乘累加」的差别。 |
| [hdl/verilog/rvv/inc/rvv_backend_define.svh](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh) | 全局宏：`NUM_MUL=2`、`VLEN`/`VLENW`、`BYTE/HWORD/WORD_WIDTH` 等，是本讲所有「阵列规模」数字的来源。 |
| [hdl/verilog/rvv/inc/rvv_backend_opcode.svh](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_opcode.svh) | 操作码定义：`VMACC`/`VNMSAC`/`VMADD`/`VNMSUB`/`VWMACC` 等，即「VDOT」在 RTL 里的真名。 |
| [hdl/verilog/rvv/design/rvv_backend_dispatch.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_dispatch.sv) | 派发单元：把 `vd` 的旧值以 `vs3_data` 名义送给 MAC——这是「累加器住在 VRF」的关键证据。 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块，按「先愿景、再顶层、再网格、再归约、最后累加」的顺序，逐层把架构口号落到代码。

### 4.1 架构愿景：外积引擎为何能「以少换多」

#### 4.1.1 概念说明

CoralNPU 的架构文档开宗明义：**整个设计的中心部件是一个「量化外积乘累加引擎」（quantized outer product multiply-accumulate engine）**。要理解它，先抓住一个矛盾：

- ML 加速器要的是**算力**（每秒海量乘累加）。
- 但嵌入式芯片的瓶颈往往是**访存带宽**和**面积/功耗**。

如果每个乘法都独占一对输入，那么「算力翻倍」就得「搬数翻倍」，得不偿失。**外积结构**的妙处在于：它用「**二维广播**」让少量输入反复参与大量乘法，从而把「算力 / 访存比」最大化。

具体地，架构文档这样描述两个轴（见 [overview.md:L48-L61](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L48-L61)）：

- 一个轴是**并行广播**的「wide」（典型是卷积权重）；
- 另一个轴是**转置移位**的多 batch 输入「narrow」（如 MobileNet 的 XY batching）。

> 旁注：文档把外积结构称作「**VDOT 操作码的纵向排列**，每个 VDOT 用 4 个 8 位乘法归约进 32 位累加器，每周期完成 256 MACs」。还提到一张累加器表 [overview.md:L42-L45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L42-L45)：`Accumulator acc<8><8> = 8x8x 32 bits`，即一张 8×8、每格 32 位的累加器阵列。这个 `acc<8><8>` 是**架构概念**，在 RTL 里它**并不是一个独立寄存器堆**——4.5 会证明它实际复用了向量寄存器堆。

#### 4.1.2 核心流程

外积广播的数学本质：

设有「wide」权重向量 \(\vec{w}\)（长 \(m\)）与「narrow」输入向量 \(\vec{x}\)（长 \(n\)），外积矩阵 \(\mathbf{P}\) 为：

\[
P_{i,j} = w_i \cdot x_j, \quad i \in [0,m),\ j \in [0,n)
\]

输入字节数为 \(m+n\)，产生的乘积数为 \(m\cdot n\)。当 \(m=n=4\) 时：8 个输入字节 → 16 个乘积。每个 \(w_i\) 被复用 \(n\) 次、每个 \(x_j\) 被复用 \(m\) 次。这就是「以少换多」。

在卷积语境里，最终要的是沿输入通道方向的**点积归约**：

\[
\text{acc}_k \;=\; \text{acc}_k + \sum_{c} w_{k,c}\cdot a_{c}
\]

外积网格一次性算出 \(w_{k,c}\cdot a_{c}\) 的多种组合，再由累加器沿某一方向求和收集——于是「4 个 8 位乘 → 1 个 32 位累加」的 VDOT 积木就自然出现了。

#### 4.1.3 源码精读

文档把 MAC 定位为「central component」，并点明 wide/narrow 两个轴与 256 MACs/周期：

- MAC 是设计中心，外积提供二维广播以最大化算力/访存比：[overview.md:L48-L56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L48-L56)（中文要点：中心部件是量化外积乘累加引擎；外积用二维广播最大化「可交付算力相对访存」的比例；一轴是并行广播的 wide=卷积权重，另一轴是转置移位的多 batch 输入 narrow=如 MobileNet XY batching）。
- VDOT 积木与 256 MACs/周期：[overview.md:L59-L61](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L59-L61)（中文要点：外积结构是多个 VDOT 的纵向排列，每个 VDOT 用 4 个 8 位乘法归约进 32 位累加器，每周期 256 MACs）。
- L1D 双 bank 中的「一半带宽」专门喂给外积引擎，可见其对带宽的胃口：[overview.md:L86-L91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L86-L91)。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：用文档自己的话建立「外积 = 二维广播」的直觉，并记下三个待核对的口号。
2. **步骤**：打开 [overview.md:L48-L61](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L48-L61)，把这三句话抄下来并各用一句中文解释：①「outer-product engine provides two-dimensional broadcast structures」；②「4x 8bit multiplies reduced into 32 bit accumulators」；③「performing 256 MACs per cycle」。
3. **观察**：注意 ②里的「4 个 8 位乘 → 1 个 32 位累加」——先记住这个「4」，4.3 会用源码告诉你这「4」来自 4×4 外积网格里的一个 4。
4. **预期结果**：你得到一张「口号 → 待验证代码位置」的清单。本讲后续模块会逐一兑现它。
5. 「待本地验证」项：无（纯阅读）。

#### 4.1.5 小练习与答案

- **Q1**：为什么外积比「每个乘法独占一对输入」更省访存？
  - **答**：外积把 \(m+n\) 个输入广播成 \(m\cdot n\) 个乘积，输入复用率高；算力提升时搬数量只按线性增长而非平方增长，算力/访存比更高。
- **Q2**：`acc<8><8>` 字面意思是多大？
  - **答**：8×8 个格子、每格 32 位 = 64 个 32 位累加器 = 2048 位。（注：这是架构概念，4.5 会说明 RTL 里它落在 VRF 上。）

---

### 4.2 顶层包装 rvv_backend_mulmac：双实例与仲裁

#### 4.2.1 概念说明

架构上「每周期 256 MACs」不是靠一个巨型乘法器一次算完，而是靠**多个并行的 MAC 执行单元**。RTL 里这个「多实例 + 调度」的职责由 `rvv_backend_mulmac` 担任——它是 MUL/MAC 类指令的**顶层包装**：声明了 `NUM_MUL` 个执行单元，并在「上游保留站（MUL_RS）的两个槽」与「两个 MAC 单元」之间做仲裁，同时负责 pop 保留站。

#### 4.2.2 核心流程

`rvv_backend_mulmac` 的数据流（对应其端口与注释 [rvv_backend_mulmac.sv:L1-L10](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mulmac.sv#L1-L10)）：

```text
MUL 保留站 (MUL_RS)
   │  uop_valid_rs2ex[0..1]   mac_uop_rs2ex[0..1]   (MUL_RS_t，含 vs1/vs2/vs3 三操作数)
   ▼
┌─────────────────────────────────────────────┐
│ rvv_backend_mulmac                          │
│   ① mac_ready[i] = ~res_valid | res_ready  │  ← 某单元空闲（无待消费结果）才可接新 uop
│   ② 仲裁 case(mac_ready)：把 2 个 RS 槽     │
│      路由到 2 个 MAC 单元，处理单侧反压      │
│   ③ pop[i] = 选中并送出时拉高，弹 RS        │
└─────────────────────────────────────────────┘
   │  res_valid_ex2rob[0..1]  res_ex2rob[0..1]  (PU2ROB_t)
   ▼
ROB（乱序写回，再按序退休 —— 见 u7-l3）
```

关键点：当只有一个单元就绪（`mac_ready` 只有一位置 1）时，仲裁把 RS 的**槽 0** 路由到那个空闲单元，槽 1 暂不 pop——保证不丢指令、不乱序发射。

#### 4.2.3 源码精读

- 文件头注释点明职责：实例化 MUL/MAC、做 uop0/1 到 MUL 或 MAC 的仲裁、pop `MUL_RS`：[rvv_backend_mulmac.sv:L1-L10](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mulmac.sv#L1-L10)。
- `NUM_MUL` 决定实例数（=2）：[rvv_backend_define.svh:L64-L68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L64-L68)（中文要点：`NUM_MUL=2`，即两个 MUL/MAC 执行单元）。
- 就绪握手：只有「没有待写回结果」或「ROB 能收下结果」时，单元才接收新 uop：[rvv_backend_mulmac.sv:L56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mulmac.sv#L56)（`assign mac_ready = ~res_valid_ex2rob | res_ready_rob2ex;`）。
- 三种仲裁情形：两单元都就绪时两槽齐发；只单元 1 就绪时把槽 0 送给单元 1、槽 1 不动；只单元 0 就绪时同理：[rvv_backend_mulmac.sv:L58-L93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mulmac.sv#L58-L93)。
- 用 `generate for` 实例化 `NUM_MUL` 个 `rvv_backend_mac_unit`：[rvv_backend_mulmac.sv:L100-L118](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mulmac.sv#L100-L118)（中文要点：每个 `u_mac` 接收仲裁后的 `mac_valid/mac_uop`，输出 `res_valid_ex2rob/res_ex2rob` 给 ROB）。
- 这个 wrapper 本身在顶层 `rvv_backend` 被实例化为 `u_mulmac`：[rvv_backend.sv:L896-L897](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend.sv#L896-L897)。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：确认「双实例 + 仲裁」结构，并理解反压时槽 0 优先。
2. **步骤**：读 [rvv_backend_mulmac.sv:L58-L93](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mulmac.sv#L58-L93) 的 `case(mac_ready)`。
3. **观察**：在 `2'b01`（只有单元 0 就绪）分支里，`mac_valid[0]=uop_valid_rs2ex[0]`、`mac_valid[1]=0`、`pop[0]=uop_valid_rs2ex[0]`、`pop[1]=0`——即槽 0 进单元 0，槽 1 既不送也不弹。
4. **预期结果**：你能解释「为什么反压时只动槽 0」——因为 ROB 按序入队，槽 0 是更老的指令，必须先走，不能让槽 1 越过它。
5. 「待本地验证」项：无。

#### 4.2.5 小练习与答案

- **Q1**：`NUM_MUL` 是几？改成 1 会少多少算力？
  - **答**：`NUM_MUL=2`。改成 1 则 MAC 吞吐减半（每周期 MAC 数 ×0.5）。
- **Q2**：`mac_ready` 的公式里 `~res_valid_ex2rob` 这一项目的是什么？
  - **答**：若该单元本拍没有「等待写回 ROB」的结果，说明它空了，可以接新 uop；这是「单拍流水、结果未取走就反压」的标准握手。

---

### 4.3 字节级外积网格：4×4 tile 与「每周期 MAC 数」

#### 4.3.1 概念说明

`rvv_backend_mac_unit` 是真正的「引擎」。它的核心思想极其简洁：**不管你给的是 8/16/32 位元素，我都先把它拆成字节，扔进一张「4×4 的 8×8 乘法网格」**。这张网格的几何结构，正是 4.1 说的**外积**。

之所以敢「一律拆字节」，是因为任何宽度的整数乘法都能用 8×8 乘法做「schoolbook（竖式）」分解；而把网格排成 4×4，恰好让「4 个 src0 字节 × 4 个 src1 字节」构成一个完整外积 tile。

#### 4.3.2 核心流程

引擎内部「拆字节 → 外积网格 → 寄存一拍」的过程：

```text
src2(vs2, wide/权重) ─┐
                      ├─ 按字节拆分 → mac8_in0[z*4 + x]   (x=0..3, 每 tile 取 4 个字节)
src1(vs1, narrow/输入)├─ 按字节拆分 → mac8_in1[z*4 + y]   (y=0..3, 每 tile 取 4 个字节)
                      ▼
            ┌──────── 4×4 外积 tile (每 tile 16 个 mul8) ────────┐
            │  P[x][y] = mac8_in0[z*4+x] · mac8_in1[z*4+y]        │
            │  共 VLENW 个 tile（VLENW = VLEN/32）                  │
            └────────────────────┬─────────────────────────────────┘
                                 ▼  edff 寄存一拍 (mac8_out_d1)
                       按 EEW 归约（4.4）→ 乘累加（4.5）
```

每个 tile 内，{4 个 src0 字节} ⊗ {4 个 src1 字节} = 16 个乘积——一个标准外积。三个循环变量 `z/x/y` 中，`z` 选 tile，`x` 走 src0（wide）轴，`y` 走 src1（narrow）轴。

**每周期 8×8 乘法总数**（把架构口号「256 MACs」算清楚）：

\[
\text{mul8/周期} \;=\; \text{NUM\_MUL} \times \text{VLENW} \times 4 \times 4
\]

代入 `NUM_MUL=2`、`VLENW = VLEN/32`（见 [rvv_backend_define.svh:L142-L144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L142-L144)）：

\[
\text{mul8/周期} \;=\; 2 \times \frac{\text{VLEN}}{32} \times 16 \;=\; \text{VLEN}
\]

也就是说，**每周期 8×8 乘法数恰好等于 `VLEN`**：

| 配置 | VLEN | 每周期 8×8 乘法数 | 与文档对照 |
| --- | --- | --- | --- |
| 文档引用 | 256 | **256** | 即 overview 的「256 MACs/周期」 |
| 实际构建 | 128 | **128** | FPGA/UVM/VCS/cocotb/Chisel 全部 `-DVLEN_128` |

> 因此「256 MACs/周期」是 `VLEN=256` 时的数字；**真实 CoralNPU（`VLEN_128`）是 128 个 int8×int8/周期**。这是 4.1 提到的文档/实现差异的量化兑现。

#### 4.3.3 源码精读

- 操作数结构体解包：从 `MUL_RS_t` 取出 `vs1/vs2/vs3`（累加源）、`funct6/funct3`、`vs2_eew`（EEW）、`vxrm`（舍入模式）、`rob_entry`：[rvv_backend_mac_unit.sv:L149-L161](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L149-L161)（中文要点：`mac_uop_vs3_data` 就是累加源，4.5 会看到它 = 旧 `vd`）。
- 「按字节拆」：把 `src2/src1` 拆成 `VLENB` 个字节，并带上逐字节的符号位：[rvv_backend_mac_unit.sv:L646-L654](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L646-L654)。
- **外积网格（本讲核心）**：三重 `generate for`，`z` 遍历 `VLENW` 个 tile，`x/y` 各遍历 4，实例化 `mul8` 计算 `mac8_out[z*16+y*4+x] = mac8_in0[z*4+x] · mac8_in1[z*4+y]`：[rvv_backend_mac_unit.sv:L687-L713](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L687-L713)（中文要点：`x` 是 src0(wide) 轴、`y` 是 src1(narrow) 轴；同一 `z*4+x` 的字节被广播给 4 个 `y`，同一 `z*4+y` 的字节被广播给 4 个 `x`——这就是二维广播）。
- 叶子乘法器 `mul8`：带符号控制的 8×8 → 16 位乘法，先按 `is_signed` 做符号扩展再相乘：[rvv_backend_mul_unit_mul8.sv:L21-L24](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mul_unit_mul8.sv#L21-L24)（`res = {{8{src0_sgn}},src0} * {{8{src1_sgn}},src1};`）。
- 流水使能 `mac8_en`：按 EEW 选择性地让部分乘法器锁存（EEW8 只用对角线那几个，省功耗）：[rvv_backend_mac_unit.sv:L657-L683](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L657-L683)。
- 宏定义依据：`VLENW=VLEN/WORD_WIDTH`、`WORD_WIDTH=32`、`BYTE_WIDTH=8`：[rvv_backend_define.svh:L98-L100](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L98-L100) 与 [rvv_backend_define.svh:L142-L144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L142-L144)。

#### 4.3.4 代码实践（源码阅读型 —— 本讲主推实践）

1. **目标**：亲手验证 4.3.2 的公式，并标注「wide 字节如何广播给 narrow」。
2. **步骤**：
   - 打开 [rvv_backend_mac_unit.sv:L687-L713](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L687-L713)。
   - 固定 `z=0`，画出该 tile 的 4×4 表格：行用 `x=0..3`（对应 `mac8_in0[0..3]`，即 wide/权重的 4 个字节），列用 `y=0..3`（对应 `mac8_in1[0..3]`，即 narrow/输入的 4 个字节），格子填 `mul8(x,y)`。
   - 数一遍单个 tile 有几个 `mul8` 实例（应为 16），再确认整个单元有 `VLENW × 16` 个。
3. **观察**：同一行（固定 `x`）的 4 个乘积共用同一个 wide 字节；同一列（固定 `y`）的 4 个乘积共用同一个 narrow 字节。这就是「wide 乘在行上广播、narrow 乘在列上广播」。
4. **预期结果**：你得到一张 4×4 外积表，并口算出 `VLEN_128` 下每单元 64 个 mul8、双单元 128 个/周期；`VLEN_256` 下 256 个/周期。
5. 「待本地验证」项：若想看波形，可跑 `tests/uvm` 或 cocotb 的乘法用例对照（见第 5 节综合实践），但纯阅读即可完成本实践。

#### 4.3.5 小练习与答案

- **Q1**：公式 `mul8/周期 = VLEN` 是怎么来的？
  - **答**：`NUM_MUL(2) × VLENW(VLEN/32) × 4 × 4 = 2 × VLEN/32 × 16 = VLEN`。
- **Q2**：为什么 `x` 和 `y` 的循环上界是 `WORD_WIDTH/BYTE_WIDTH = 4`？
  - **答**：一个 tile 对应一个 32 位「字」宽的窗口，32 位 = 4 字节，所以 wide 轴和 narrow 轴各取 4 字节，构成 4×4 外积。

---

### 4.4 一网多用：EEW8/16/32 的 schoolbook 归约

#### 4.4.1 概念说明

4.3 的 4×4 网格产生了 16 个字节级乘积。但一条向量指令要的可能是 8 位、16 位或 32 位元素的结果。`mac_unit` 没有为每种宽度各造一套乘法器，而是**用同一张 16 乘积的网格，按 EEW 用不同方式「挑+加」**——这本质上是中小学竖式乘法（schoolbook multiplication）的硬件化：

- **EEW8**：4 个独立元素，每个元素只取网格的**对角线**乘积（1 个）。
- **EEW16**：2 个独立元素，每个元素取 **2×2 = 4** 个部分积相加。
- **EEW32**：1 个元素，取**全部 4×4 = 16** 个部分积相加。

#### 4.4.2 核心流程

把 32 位元素 \(A=a_3 a_2 a_1 a_0\)、\(B=b_3 b_2 b_1 b_0\)（每段 8 位）的乘积展开（schoolbook）：

\[
A \cdot B \;=\; \sum_{i,j} a_i \cdot b_j \cdot 2^{8(i+j)}
\]

这正是网格里 16 个 \(a_i b_j\) 按权重 \(2^{8(i+j)}\) 求和。EEW16 只用到 \(i,j \in \{0,1\}\) 的 4 项；EEW8 只用到 \(i=j\) 的对角项（逐元素乘）。

归约后的「全精度乘积」再送进 4.5 的乘累加步骤。

```text
16 个字节乘积 mac8_out_d1[16]
        │
        ├── EEW8  : 取对角线 [0,5,10,15]      → 4 个 16 位乘积（逐元素）
        ├── EEW16 : 每 2×2 块相加              → 2 个 32 位乘积
        └── EEW32 : 全部 16 项按权相加         → 1 个 64 位乘积
        ▼
   乘累加（加/减到 vs3）→ 按 EEW 选结果打包
```

#### 4.4.3 源码精读

- **EEW8**：取对角线 `mac8_out_d1[i*16+j*5]`（`j*5 = j*4+j` 即 `x=y=j`），逐元素乘：[rvv_backend_mac_unit.sv:L754-L757](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L754-L757)。
- **EEW16**：把交叉项 `mac8_out_d1[i*16+j*10+4]` 与 `+1` 先相加（带符号扩展），再与 `+5`、`+0` 按权组合成 32 位乘积：[rvv_backend_mac_unit.sv:L816-L823](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L816-L823)（中文要点：`j*10+{0,1,4,5}` 恰是一个 2×2 子块的 4 个字节对）。
- **EEW32**：把 tile 内 16 个 `mac8_out_d1[i*16+0..15]` 全部按权相加（带符号扩展与移位），得到一个 64 位乘积：[rvv_backend_mac_unit.sv:L890-L903](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L890-L903)（中文要点：`mac_rslt_part16/32/48_eew32` 是按字节权重组的中间部分积，最后拼成 64 位）。
- 三选一按 `is_vmac/is_widen/is_vsmul` 选最终结果：[rvv_backend_mac_unit.sv:L801-L810](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L801-L810)。
- 兄弟单元 `rvv_backend_mul_unit` 用**同样的 4×4×4 网格**做纯乘法（无累加），可对照阅读：[rvv_backend_mul_unit.sv:L389-L409](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mul_unit.sv#L389-L409)。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：验证「同一张 16 乘积网格服务三种 EEW」。
2. **步骤**：在 [rvv_backend_mac_unit.sv:L890-L903](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L890-L903)（EEW32）里数一数用到了 `mac8_out_d1[i*16+?]` 的几个下标；再在 EEW16（L816-L823）和 EEW8（L754-L757）里分别数。
3. **观察**：EEW32 用满 16 个、EEW16 用 4 个（2×2）、EEW8 用 1 个（对角线）——完美对应 4.4.2 的 schoolbook 划分。
4. **预期结果**：你能说清「为什么一套乘法器阵列就够了」——不同 EEW 只是**读取/相加的字节组合不同**，硬件是同一批 `mul8`。
5. 「待本地验证」项：无。

#### 4.4.5 小练习与答案

- **Q1**：EEW8 为什么只取对角线 `x=y` 的乘积？
  - **答**：EEW8 是逐元素乘 `vd[k] = vs1[k]·vs2[k]`，只有同下标的字节相乘才合法，对应外积网格的对角线。
- **Q2**：EEW32 用到几个字节乘积？为什么？
  - **答**：16 个。因为两个 32 位数各拆成 4 字节，schoolbook 展开共有 4×4=16 个字节对的部分积。

---

### 4.5 乘累加语义：vmac 加减、vs3 与「累加器」的真相

#### 4.5.1 概念说明

到这里，网格产出的是「乘积」。MAC 还差最后一步：**累加**。架构文档把这一步描述为「reduced into 32 bit accumulators」，并把累加器画成 `acc<8><8>`。

但读 RTL 会发现一个关键事实：**CoralNPU 没有为 MAC 单独建一个累加器寄存器堆**。所谓「累加器」，就是**向量寄存器 `vd` 本身**。MAC 指令做的是标准的 RISC-V V 扩展**读-改-写**语义：

\[
\text{vd} \;\leftarrow\; (\text{vs1} \cdot \text{vs2}) \;\diamond\; \text{vd}_{\text{old}}
\]

其中 \(\diamond\) 是「加」（`vmacc`/`vmadd`）或「减」（`vnmsac`/`vnmsub`）。引擎在派发时把**旧的 `vd` 值以 `vs3` 的名义**读出来送进 MAC（见下方源码铁证），算完再写回 `vd`。于是「累加器」就分布在 VRF 里——`acc<8><8>` 是它的架构画像，而非独立硬件。

这也回答了 4.1 的悬念：文档说「VDOT」，RTL 里它的真名是 `VMACC`/`VNMSAC`/`VMADD`/`VNMSUB`（及其 widening 变体 `VWMACC` 等）这一族标准乘累加指令。

#### 4.5.2 核心流程

乘累加的最后一步（对每个 lane）：

```text
乘积 P = schoolbook(vs1, vs2)            # 来自 4.4
旧累加值 A = vs3 (= vd_old, 来自 VRF)     # 派发时读出
结果 R = mac_mul_reverse ? (A - P) : (A + P)
vd ← R                                    # 经 ROB 按序退休写回 VRF（u7-l3）
```

对应 RVV 语义：

| 指令 | 语义 | `mac_mul_reverse` | `is_vmac` |
| --- | --- | --- | --- |
| `vmacc` | vd = vs1·vs2 + vd | 0（加） | 1 |
| `vnmsac` | vd = -(vs1·vs2) + vd | 1（减） | 1 |
| `vmadd` | vd = vs1·vd + vs2（角色互换） | 0 | 1 |
| `vnmsub` | vd = -(vs1·vd) + vs2 | 1 | 1 |
| `vmul` | vd = vs1·vs2（不累加） | — | 0 |

#### 4.5.3 源码精读

- **铁证：派发把旧 `vd` 当作 `vs3` 送给 MAC**：[rvv_backend_dispatch.sv:L410-L411](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L410-L411)（`assign rs_dp2mul[i].vs3_data = uop_operand[i].vd;`——中文要点：MAC 的「累加源 vs3」就是 `vd` 的旧值，从 VRF 读出）。
- `VMACC` 的操作数选择（vv 形态）：`mac_src2=vs2, mac_src1=vs1, mac_addsrc=vs3, mac_mul_reverse=0, is_vmac=1`：[rvv_backend_mac_unit.sv:L169-L180](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L169-L180)。
- `VNMSAC` 把 `mac_mul_reverse=1`（改成减）：[rvv_backend_mac_unit.sv:L181-L192](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L181-L192)。
- `VMADD`/`VNMSUB` 角色互换（`mac_src2=vs3=vd`，`mac_addsrc=vs2`）：[rvv_backend_mac_unit.sv:L193-L216](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L193-L216)。
- 乘累加计算（EEW8 为例）：`vmac_mul_add = addsrc + 乘积`、`vmac_mul_sub = addsrc - 乘积`，由 `mac_mul_reverse` 选：[rvv_backend_mac_unit.sv:L782-L788](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L782-L788)。
- widen 形态累加（EEW8 widen）：把 `addsrc` 复制到高位再相加，支持 `vwmacc` 类指令：[rvv_backend_mac_unit.sv:L749](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L749)（`mac_addsrc_widen_d1 = {2{mac_addsrc_d1}};`）与 [rvv_backend_mac_unit.sv:L790-L796](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L790-L796)。
- 按 `is_vmac/is_widen/is_vsmul` 四选一打包结果 `w_data`，并按 EEW 选最终宽度：[rvv_backend_mac_unit.sv:L975-L990](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L975-L990)。
- 这些指令的操作码定义（「VDOT」的真名）：[rvv_backend_opcode.svh:L94-L97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L94-L97)（`VMADD/VNMSUB/VMACC/VNMSAC`）与 widening 族 [rvv_backend_opcode.svh:L109-L112](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L109-L112)（`VWMACCU/VWMACC/VWMACCUS/VWMACCSU`）。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：用源码自证「累加器 = 旧 vd」，并跟踪一次 `vmacc` 的完整数据通路。
2. **步骤**：
   - 在 [rvv_backend_dispatch.sv:L410-L411](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L410-L411) 确认 `vs3_data = vd`。
   - 跟踪 `vmacc.vv`（[rvv_backend_mac_unit.sv:L169-L180](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L169-L180)）：`mac_addsrc=vs3` → [L782-L788](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L782-L788) 里 `vmac_mul_add = vs3 + 乘积` → 经 [L801-L810](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L801-L810) 选为 `mac_rslt_eew8_d1` → 打包成 `w_data`（[L975-L990](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L975-L990)）→ 回 ROB → 退休写回 `vd`。
3. **观察**：整条链路上**没有一个叫 acc 的独立存储**；累加值始终以 `vs3`/`vd` 的身份住在 VRF。
4. **预期结果**：你能画出 `vd_new = vd_old + vs1·vs2` 的完整硬件通路，并标注每段代码的行号。
5. 「待本地验证」项：无。

#### 4.5.5 小练习与答案

- **Q1**：架构文档的 `acc<8><8>` 在 RTL 里对应什么？
  - **答**：没有独立累加器堆；累加由向量寄存器 `vd` 承担，旧值以 `vs3` 读入、新值写回 `vd`。`acc<8><8>` 是其架构概念画像。
- **Q2**：`vmacc` 与 `vnmsac` 在 RTL 里的唯一关键差别是哪个信号？
  - **答**：`mac_mul_reverse`（`vmacc`=0 做加，`vnmsac`=1 做减）。

---

## 5. 综合实践：推演一次「wide 权重 × narrow 输入」并标注累加器

把本讲五个模块串起来，完成下面这个贯穿性任务。

**背景**：假设软件下发一条 `vmacc.vv`，EEW=8，把 4 个 int8 权重（wide）与 4 个 int8 输入（narrow）相乘并累加进 `vd` 的 4 个 int32 lane（实际跨多次 uop/stripmine 完成，这里只看「一个 tile、一拍」的微观切片）。

**任务**：

1. **画外积表**：取一个 tile（`z=0`），在 4×4 表格里填出 16 个 `mul8` 计算的是「权重字节 \(w_x\) × 输入字节 \(a_y\)」。标注 wide 在行广播、narrow 在列广播。（依据 [rvv_backend_mac_unit.sv:L687-L713](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L687-L713)）
2. **挑对角线**：EEW8 下，引擎只取对角线 \(w_x·a_x\)（\(x=0..3\)）作为 4 个逐元素乘积。（依据 [L754-L757](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L754-L757)）
3. **加累加器**：每个 lane 执行 \(vd_x \leftarrow vd_x + w_x·a_x\)，其中 \(vd_x\) 的旧值来自 `vs3`（= 旧 vd）。（依据 [dispatch.sv:L410-L411](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_dispatch.sv#L410-L411) 与 [mac_unit.sv:L782-L788](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mac_unit.sv#L782-L788)）
4. **数算力**：填表——本拍本单元算了几次 8×8 乘？整芯片（`NUM_MUL=2`）`VLEN_128` 下一拍几次？若 `VLEN_256` 呢？（答案：64 / 128 / 256）
5. **写回**：结果经 ROB 按序退休写回 `vd`（回顾 u7-l3）。

**预期产出**：一张 4×4 外积表 + 一条带行号的 `vmacc` 数据通路图 + 一份「实际构建 128 MACs/周期」的算力小结。

> **想跑波形的读者（可选，待本地验证）**：可参照 `tests/uvm`（UVM 平台）或 `tests/cocotb`（cocotb）里针对乘法/乘累加的回归用例，构造一个 `vmacc.vv` 激励，在波形里观察 `mac8_out`、`mac8_out_d1`、`vmac_mul_add_*` 信号，对照你画的表格逐拍核对。具体用例路径与运行方式见 u11-l2（VCS/UVM）与 u11-l3（cocotb 回归）。

---

## 6. 本讲小结

- CoralNPU 的 MAC 是一台**量化外积乘累加引擎**：用 wide/narrow 两轴的**二维广播**，以少量输入产生大量乘积，最大化算力/访存比。
- 顶层 `rvv_backend_mulmac` 实例化 **`NUM_MUL=2`** 个 MAC 单元，并在两个保留站槽与两个单元间做**带反压的仲裁**（反压时槽 0 优先）。
- 引擎核心是一张 **4×4 字节外积网格**（每 tile 16 个 `mul8`），`x` 走 wide/权重轴、`y` 走 narrow/输入轴，构成一次完整外积。
- **每周期 8×8 乘法数 = `VLEN`**：文档「256 MACs/周期」对应 `VLEN=256`；**实际构建 `VLEN_128` 为 128/周期**。
- 同一张 16 乘积网格**一网多用**：EEW8 取对角线（逐元素）、EEW16 取 2×2、EEW32 取全部 4×4——本质是 schoolbook 竖式乘法的硬件化。
- 「累加器」**不是独立寄存器堆**：MAC 做标准 RVV 读-改-写，旧 `vd` 以 `vs3` 读入，乘积经 `vmac_mul_add/sub` 加减后写回 `vd`；架构上的 `acc<8><8>` 是其概念画像，RTL 里由 VRF 承担。

---

## 7. 下一步学习建议

- **横向对照「纯乘」与「乘累加」**：读 [rvv_backend_mul_unit.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_mul_unit.sv)，看它如何用同一片 `mul8` 网格做 `vmul`/`vsmul`（含定点舍入与饱和 `vxsat`），巩固「一张阵列服务多种指令」的设计。
- **向上接软件**：进入 u10-l1（RVV intrinsics 向量编程）与 u10-l2（litert-micro 算子内核），看 `conv`/`fully_connected` 算子如何把权重/激活组织成 wide/narrow，喂给本讲的 MAC 引擎。
- **向深究编码与展开**：u7-l6（Stripmining 与 SIMD 指令编码）会解释「一次前端派发如何展成 4 次串行发射」填满 MAC 网格，与本讲的「每周期算力」首尾相接。
- **验证侧**：u11-l2（VCS/UVM）与 u11-l3（cocotb 回归）提供给 MAC 引擎施加定向+随机激励、用 scoreboard 比对的完整方法，是验证你对本讲理解是否正确的最佳手段。
