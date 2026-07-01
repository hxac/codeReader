# Stripmining 与 SIMD 指令编码

> 本讲是第 7 单元（RVV 向量/矩阵后端）的第 6 讲。前置讲义 [u7-l2](u7-l2-rvv-decode-dispatch.md) 已经讲过 SystemVerilog 侧 `rvv_backend_decode`/`rvv_backend_dispatch` 如何把向量指令拆成微操作（uop）、检测结构冒险并决定何时发射。本讲我们要回答两个更底层的问题：**到底「一个指令被拆成几个 uop」是由什么决定的？** 以及 **CoralNPU 是从哪里「挤出」编码位来同时装下 64 个向量寄存器、灵活类型编码和这种「一拆多」机制的？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **stripmining（条带挖矿）** 的直觉：它把「数组的并行」折叠成「硬件的并行」，让一次前端派发在 Issue 端变成多次串行发射（典型地 1 派发 → 4 发射）。
- 在 `rvv_backend_define.svh` 与 `rvv_backend_decode_unit_ari.sv` 中定位**决定 uop 数量的两个编码来源**：CSR 里的 `lmul` 与指令里的 `nreg` 立即数字段。
- 解释 overview.md 所说的「**复用 C 扩展编码空间**为 SIMD 寄存器提供 6b 索引」的含义，并诚实指出「文档设计意图（64 寄存器/6b）」与「当前 RTL 实现（32 寄存器/5b）」之间的差异。
- 读懂 `rvv_backend_define.svh` 中与编码、展开、类型相关的关键宏。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**(1)「数组并行」≠「硬件并行」。** 一段 `for (i) c[i] = a[i] + b[i]` 对成百上千个元素做加法，这是「数组并行」。但硬件一个周期只能算固定个数（CoralNPU 向量寄存器一个 VLEN 宽，处理一拍是一组元素）。把「上千个元素」映射到「每拍一组」的过程，就叫 stripmining——像挖矿一样一条一条地把数组切（strip）成硬件吃得下的条带。

**(2) RISC-V 向量寄存器组（register group / LMUL）。** RVV 允许把 `LMUL` 个相邻向量寄存器「绑」成一组，当成一个更宽的逻辑寄存器用。`LMUL=4` 时一条 `vadd` 在体系结构上是对 4 个寄存器宽的数据做加法。CoralNPU 的执行通路每拍只处理 1 个寄存器宽，于是就把这「一条 LMUL=4 的 vadd」**在内部展开成 4 个 uop**，分别打 `v0 / v1 / v2 / v3`。这就是「1 派发 → 4 发射」的物理来源。

**(3) 32 位指令字是「装不下」的。** 标准 RISC-V 32 位字里：7 位 opcode + 3 位 funct3 + rd/rs1/rs2 各 5 位，已经很挤。向量 ISA 还想再塞 6 位 funct6（主操作码）、1 位 vm（掩码）、**6 位**的向量寄存器索引（为了编址 64 个向量寄存器）、以及一个 stripmine 计数字段——直接算会超过 32 位。CoralNPU 的解法是**把 16 位压缩指令（C 扩展）的编码空间「收回」自用**，从而拿到额外的编码预算。

> 术语速查：**uop**（micro-operation，微操作）是后端真正调度的最小单位；**EMUL/LMUL** 是寄存器组倍率；**SEW**（selected element width）是单元素位宽（8/16/32）；**VLEN** 是单个向量寄存器的位宽。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/overview.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md) | 项目顶层设计说明，本讲的「权威定义」来源：C 扩展复用、Stripmining、向量寄存器模型 |
| [doc/microarch/microarch.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/microarch.md) | 微架构概览：标量核「每周期最多派发 4 条」、各类指令延迟 |
| [hdl/verilog/rvv/inc/rvv_backend_define.svh](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh) | RVV 后端的「参数总表」：issue lane 数、uop 队列深度、VLEN、所有编码字段宽度 |
| [hdl/verilog/rvv/inc/rvv_backend_opcode.svh](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_opcode.svh) | 操作码与 `NREG`/`NF` 等条带计数的符号常量 |
| [hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv) | 算术类译码：把 `lmul`/`nreg` 翻译成 `uop_index_max`（即「这条指令要展开成几个 uop」） |
| [hdl/chisel/src/coralnpu/rvv/RvvDecode.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala) | Chisel 前端译码：定义「压缩指令」中间表示、字段切片、`VMV1R/2R/4R/8R` |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 Stripmining 的展开机制**、**4.2 C 扩展编码空间复用与 6b 索引**、**4.3 rvv_backend_define.svh 的关键宏与类型编码**。

### 4.1 Stripmining：一次派发如何变成四次发射

#### 4.1.1 概念说明

overview.md 给出了 CoralNPU 对 stripmining 的官方定义：

> Strip mining is defined as **folding array-based parallelism to fit the available hardware parallelism**.

即「把基于数组的并行**折叠**到硬件能提供的并行上」。它解决两个现实问题：

1. **降低前端派发压力。** 标量核每周期最多派发 4 条指令（见 [microarch.md:7-18](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/microarch.md#L7-L18)）。如果每处理一组向量元素就要派发一条新指令，前端会立刻成为吞吐瓶颈。让一条指令「自带」处理多组的能力，前端压力就下来了。
2. **原生支持 SIMD 寄存器层级的 tiling。** 算子（如卷积）天然需要把数据切块（tile）放进寄存器组，stripmining 让「一条指令 ↔ 一个 tile」直接对应。

overview 用一句话点明了展开比例：

> ... converts a single frontend dispatch event to the command queue into **four serialized issue events** into the SIMD units. For instance a “vadd v0” in Dispatch will produce “vadd v0 : vadd v1 : vadd v2 : vadd v3” at Issue.

完整出处见 [overview.md:63-72](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L63-L72)。注意「four」只是举例（对应 LMUL=4），实际展开数由编码字段决定，可以是 1/2/4/8。

#### 4.1.2 核心流程

一条向量指令从前端到 Issue 的「一拆多」流程：

```
标量核 Dispatch（每周期≤4 条指令）
        │  一条「vadd v0, v1, v2」(LMUL=4)
        ▼
RVV 后端 Decode（rvv_backend_decode_unit_ari.sv）
        │  1. 读 inst_nr / reduced_lmul
        │  2. 计算 uop_index_max = 组倍率-1  (LMUL4 → 3)
        │  3. 生成 uop_index ∈ {0,1,2,3}，每拍目标寄存器号 = vd + uop_index
        ▼
Uop Queue（UQ_DEPTH=16）
        │  NUM_DE_UOP=4：译码每周期最多写 4 个 uop
        ▼
Dispatch（rvv_backend_dispatch）
        │  NUM_DP_UOP=2：每周期最多发射 2 个 uop
        ▼
Issue → 各执行单元（4 个 uop 作为 4 个独立事件串行处理）
        产出：vadd v0 : vadd v1 : vadd v2 : vadd v3
```

展开后的 uop 数量满足（非 widening 指令）：

\[
\text{uop 数} = \text{EMUL}_\text{vd} = \text{LMUL}
\]

对 widening（加宽）指令，目标寄存器组翻倍：

\[
\text{uop 数} = \text{EMUL}_\text{vd} = 2 \times \text{LMUL}
\]

于是「一条指令处理的元素总数」= `uop 数 × (VLEN / SEW)`，但前端只付出了 1 条指令的派发代价——这就是 stripmining 的「折叠」本质。

#### 4.1.3 源码精读

**决定 uop 数的字段声明。** 在算术译码单元里，每个原始编码字段都被切出来，其中 `inst_nr` 就是「条带计数」字段：

[rvv_backend_decode_unit_ari.sv:29-35](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv#L29-L35) —— 声明掩码位、三个寄存器索引、funct3、以及关键的 `inst_nr`：

```systemverilog
logic [`VM_WIDTH-1:0]            inst_vm;   // encoding[25]
logic [`REGFILE_INDEX_WIDTH-1:0] inst_vs2;  // encoding[24:20]
logic [`REGFILE_INDEX_WIDTH-1:0] inst_vs1;  // encoding[19:15]
logic [`FUNCT3_WIDTH-1:0]        inst_funct3; // encoding[14:12]
logic [`REGFILE_INDEX_WIDTH-1:0] inst_vd;   // encoding[11:7]
logic [`NREG_WIDTH-1:0]          inst_nr;   // encoding[17:15]  ← stripmine 计数
```

注意 `inst_nr` 取自 `encoding[17:15]`，也就是 **`vs1` 字段的低 3 位**——这正是「字段复用」的体现：当某指令不需要 `vs1` 寄存器时，这 3 位就被借走当作条带计数。后面会看到，这就是 overview 所说的「flexible type encodings」。

**展开计数的两个来源。** 同一个 `uop_index_max` 变量被两种方式赋值，对应「条带计数」的两套编码：

来源 A——**CSR `lmul` 驱动**（绝大多数算术指令），以 `vadd/vsub/...` 为例：

[rvv_backend_decode_unit_ari.sv:163-168](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv#L163-L168)：

```systemverilog
VSSUB,
VSMUL_VMVNRR: begin
  case(reduced_lmul)
    LMUL2: uop_index_max = 'd1;   // 2 个 uop
    LMUL4: uop_index_max = 'd3;   // 4 个 uop  ← 即 "vadd v0..v3"
    LMUL8: uop_index_max = 'd7;   // 8 个 uop
  endcase
```

`reduced_lmul` 来自 `vtype` CSR（由 `vsetvli` 设定）。LMUL4 → `uop_index_max=3` → uop 编号 0..3 → 命中 overview 的例子。

来源 B——**指令内 `nreg` 立即数驱动**（整组搬运 `vmv<nr>r.v`），以 `VSMUL_VMVNRR`（vm 字段=1 时即 vmv）为例：

[rvv_backend_decode_unit_ari.sv:843-867](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv#L843-L867)：

```systemverilog
case(inst_nr)
  NREG1: begin emul_vd=EMUL1; uop_index_max = 'd0; end  // 1 个 uop
  NREG2: begin emul_vd=EMUL2; uop_index_max = 'd1; end  // 2 个 uop
  NREG4: begin emul_vd=EMUL4; uop_index_max = 'd3; end  // 4 个 uop
  NREG8: begin emul_vd=EMUL8; uop_index_max = 'd7; end  // 8 个 uop
endcase
```

这里的 `NREG1/2/4/8` 是定义在 [rvv_backend_opcode.svh:216-230](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L216-L230) 的常量（与段加载的 `NF1..NF8` 紧挨着）：

```systemverilog
parameter NREG1 = 3'b000;   parameter NREG2 = 3'b001;
parameter NREG4 = 3'b011;   parameter NREG8 = 3'b111;
```

可见 3 位 `nreg` 只编码 4 种合法倍率（1/2/4/8，二进制呈现「低位逐位点亮」的形态）。Chisel 前端有完全对称的译码，见 [RvvDecode.scala:348-351](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L348-L351)，把 `imm5` 的几组取值分别映射成 `VMV1R/VMV2R/VMV4R/VMV8R`。

**展开后的「串行发射」由队列与派发宽度节流。** 译码每周期最多写 `NUM_DE_UOP` 个 uop，派发每周期最多发 `NUM_DP_UOP` 个（见 4.3）。所以一条 LMUL=8 的指令产生 8 个 uop，可能要花几个周期才能全部发射完——这正是 overview 所说的「processed as four discrete events」（离散事件），uop 之间在派发/保留站层面像独立指令一样接受冒险检测与旁路。

#### 4.1.4 代码实践

**实践目标：** 用源码亲手验证「vadd v0 → vadd v0..v3」这条转化链的每一个环节。

**操作步骤：**

1. 打开 [overview.md:63-72](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L63-L72)，确认文档把「four serialized issue events」写成了展开目标。
2. 在 [rvv_backend_decode_unit_ari.sv:163-168](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv#L163-L168) 找到 `LMUL4: uop_index_max = 'd3`，确认 `vadd` 在 `lmul=4` 时展开为 4 个 uop。
3. 在 [rvv_backend_opcode.svh:217-220](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L217-L220) 确认 `NREG4 = 3'b011`，对照 [rvv_backend_decode_unit_ari.sv:855-859](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv#L855-L859) 中 `NREG4 → uop_index_max='d3`，确认 `vmv4r.v` 同样展开 4 个 uop。
4. 填写下面这张「展开映射表」（待本地验证你的填写）：

| 输入 | 计数来源 | `uop_index_max` | 展开后的 uop 序列（目标寄存器） |
| --- | --- | --- | --- |
| `vadd v0`，CSR `lmul=4` | `reduced_lmul` | 3 | v0, v1, v2, v3 |
| `vmv4r.v v0` | `inst_nr=NREG4` | 3 | v0, v1, v2, v3 |
| `vadd v0`，CSR `lmul=2` | ? | ? | ? |
| `vmv8r.v v0` | ? | ? | ? |

**需要观察的现象 / 预期结果：** 你会发现「CSR lmul」与「指令 nreg」是两条独立的展开入口，但最终汇入同一个 `uop_index_max`，下游完全无感——这印证了 overview 说的「stripmine 机制被显式编进指令编码」。

> 待本地验证：上表第 3、4 行的具体取值需你按源码自行填入（提示：分别看 `LMUL2` 分支与 `NREG8` 分支）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 CoralNPU 不直接把执行通路做宽到「一拍处理 4 个寄存器」，而要费劲地展开成 4 个串行 uop？

**参考答案：** 一来「一拍 4×」会让 VRF 读口、ALU 单元、互联位宽都翻 4 倍，面积/功耗代价巨大；二来 stripmining 让前端派发压力降为 1/4，且天然对齐软件的 tiling 模式，性价比远高于无脑加宽。

**练习 2：** 一条 widening 的 `vwmacc`（LMUL=2）会展开成几个 uop？为什么？

**参考答案：** 4 个。widening 指令目标寄存器组是源组的 2 倍，`EMUL_vd = 2×LMUL = 4`。源码里 `VWMACC` 的 `LMUL2 → uop_index_max='d3`（见 [rvv_backend_decode_unit_ari.sv:887-889](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv#L887-L889)）正对应此。

---

### 4.2 C 扩展编码空间复用与 6b 索引

#### 4.2.1 概念说明

overview.md 在描述标量核时有一句关键的话：

> The C extension encoding is **reclaimed** (as per the risc-v specification) to provide the necessary encoding space for the SIMD registers (**6b indices**), and to allow flexible type encodings and instruction compression (stripmining) for the SIMD instruction set.

出处见 [overview.md:19-23](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L19-L23)。这里要分清两件事：

- **「C 扩展」是什么：** 标准 RISC-V 用指令字最低 2 位 `inst[1:0]` 区分长度——`=11` 是 32 位标准指令，`=00/01/10` 是 16 位压缩指令（C 扩展）。CoralNPU 的标量核跑的是 `rv32im` **不压缩**指令集，于是这整个 16 位指令的编码象限就闲置了。overview 说「按 RISC-V 规范把它收回」，指的是把这块闲置空间改作自定义 SIMD 编码。
- **「reclaimed」换来了什么：** 文档列了三样东西——(a) 6b 的 SIMD 寄存器索引（编址 64 个向量寄存器）；(b) 灵活的类型编码（`vtype`：SEW/LMUL 等）；(c) 指令压缩/stripmining（即 4.1 的 `nreg`/`lmul` 计数）。

#### 4.2.2 核心流程：为什么「复用 C 扩展」能换来 6b 索引

关键是**编码预算**（encoding budget）。设想一个「理想向量指令」想同时拥有：

| 字段 | 位数 |
| --- | --- |
| opcode | 7 |
| funct3（minor opcode） | 3 |
| funct6（major opcode） | 6 |
| vm（掩码） | 1 |
| vs2 / vs1 / vd 各 6b（编址 64 个向量寄存器） | 18 |
| **合计** | **35** |

35 > 32，**一个标准 32 位字装不下**。这就是为什么需要「额外的编码空间」。

复用 C 扩展后，CoralNPU 拿到了更多的主操作码空间和更多可定义的「指令格式」，可以把向量指令集**摊到多种格式**上（如 `OPIVV`/`OPIVX`/`OPMVV`…），每种格式只编码它真正需要的寄存器字段，并允许**字段复用**（像 4.1 看到的：不需要 `vs1` 时就把 `vs1` 的几位借给 `nreg`）。这样整体上才腾得出位来「考虑」给向量寄存器配 6 位索引。

**重要提醒：文档是「设计意图」，RTL 是「当前实现」，两者目前不一致。**

- 文档（[overview.md:42-45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L42-L45)）描述的目标是 **64 个向量寄存器、256 位、6b 索引**：
  > `Vector (64) | v0..v63 | 256 bits (eg. int32 x8)`
- 但当前 RTL（见 4.3 的 `NUM_VRF`、`REGFILE_INDEX_WIDTH`）实际构建的是 **32 个向量寄存器、5b 索引**（且默认 `VLEN_128` 即 128 位）。

所以准确的表述是：**「复用 C 扩展」在体系结构层面为 6b/64 寄存器预留了编码预算与机制；当前流片的 RTL 只用到 5b/32 寄存器，第 6 个索引位属于「已规划、暂未启用」。** 读源码时若只看 `define.svh` 会觉得「明明是 5b」，不要被这个表面矛盾困惑——它是 doc 与 RTL 的版本差，而非错误。

#### 4.2.3 源码精读

**字段切片证实「5b 标准 + 字段复用」。** Chisel 前端 `RvvS1DecodeInstructionBase.s1decode_opv` 把去掉低 7 位 opcode 后的 25 位按下式切片：

[RvvDecode.scala:404-411](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L404-L411)：

```scala
val vd   = bits(4, 0)      // 5b 目标寄存器
val mode = bits(7, 5)      // 3b funct3 (OPIVV/...)
val vs1  = bits(12, 8)     // 5b 源1 / imm5  ← 与 SV 侧 inst_nr 共用
val vs2  = bits(17, 13)    // 5b 源2
val f6vm = bits(24, 18)    // 6b funct6 + 1b vm
```

可以看到 vd/vs1/vs2 当前都按 **5 位**切片（对应当前 32 寄存器实现）；而 `vs1` 这 5 位在 `vmv<nr>r` 等指令里又被 `inst_nr`（低 3 位）借走——这就是「flexible type encodings」在位级别的真身。若将来要扩到 6b，正是靠 4.2.2 说的「回收 C 扩展预算」去腾位。

**「压缩指令」中间表示 ≠ C 扩展。** Chisel 侧有个 `RvvCompressedInstruction`，名字容易和「C 扩展」混淆，但它其实是另一回事——是标量核→向量后端之间 FIFO 里用的**内部**压缩格式：把 7 位 opcode 压成 2 位（只区分 LOAD/STORE/ALU 三类），剩下 25 位原样带过：

[RvvDecode.scala:36-48](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L36-L48)：

```scala
class RvvCompressedInstruction(p: Parameters) extends Bundle {
  val opcode = RvvCompressedOpcode()   // 2b: RVVLOAD/RVVSTORE/RVVALU
  val bits   = UInt(25.W)              // inst[31:7] 原样保留
  def originalEncoding(): UInt = Cat(bits, lower7bits) // 还原成 32b
}
```

代码注释明确说了它是**内部格式、不暴露给软件**：

[RvvDecode.scala:441-444](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/rvv/RvvDecode.scala#L441-L444)：

```scala
// This compressed format is only used within some of our
// cores, and never exposed to sw.
```

> 辨析：本讲的「C 扩展复用」是 **ISA 层面**（指令编码空间的分配），而 `RvvCompressedInstruction` 是**微架构层面**（FIFO 压缩省 5 位/条）。两者都叫「compressed」，但层次不同，不要混为一谈。

#### 4.2.4 代码实践

**实践目标：** 把 overview 的一句口号（「C 扩展复用 → 6b 索引」）拆成可验证的编码预算论证。

**操作步骤：**

1. 读 [overview.md:19-23](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L19-L23)，圈出「reclaimed / 6b indices / flexible type encodings / stripmining」四个关键词。
2. 读 [overview.md:42-45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L42-L45)，记录文档声明的「Vector (64), 256 bits」。
3. 读 [rvv_backend_define.svh:57-58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L57-L58) 与 [:162](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L162)（`NUM_VRF 32`、`REGFILE_INDEX_WIDTH 5`），记录 RTL 实际的「32 寄存器 / 5b」。
4. 按 4.2.2 的表格，自己算一遍「理想 6b×3 寄存器」需要多少位，体会「为什么必须回收 C 扩展」。

**预期结果：** 你能写出一段话，同时讲清三件事：(a) 标准 32 位字装不下「6b×3 + funct6 + vm + funct3 + opcode」；(b) 回收 C 扩展提供了多格式与字段复用的预算；(c) 当前 RTL 只兑现了 5b/32，6b 是预留。

> 待本地验证：步骤 4 的位宽求和请自行完成并核对（答案：35 位 > 32 位）。

#### 4.2.5 小练习与答案

**练习 1：** 「复用 C 扩展编码空间」具体指回收 RISC-V 指令字的哪几位所对应的指令象限？

**参考答案：** 指最低 2 位 `inst[1:0]` 为 `00/01/10` 的那部分——标准 RISC-V 中这是 16 位压缩指令（C 扩展）。CoralNPU 不用压缩指令，故该象限被改作自定义 SIMD 编码。

**练习 2：** 一个同学读完 overview 说「CoralNPU 有 64 个 256 位的向量寄存器」。请根据源码纠正他。

**参考答案：** 这是文档**设计意图**。当前 RTL（`NUM_VRF=32`、`REGFILE_INDEX_WIDTH=5`、默认 `VLEN_128`）实际构建 32 个 128 位向量寄存器。64/256 是规划目标，第 6 个索引位由回收的 C 扩展预算预留但尚未启用。

---

### 4.3 rvv_backend_define.svh 的关键宏与类型编码

#### 4.3.1 概念说明

`rvv_backend_define.svh` 是 RVV 后端的「参数总表」。它不实现逻辑，只定义所有宽度与深度常量。读懂它，就拿到了后端所有模块的「尺度」。本模块把与 stripmining/编码相关的宏分四组讲清。

#### 4.3.2 核心流程（四组宏的语义）

| 组别 | 代表宏 | 含义 |
| --- | --- | --- |
| 派发/译码吞吐 | `ISSUE_LANE`、`NUM_DE_INST`、`NUM_DE_UOP`、`NUM_DP_UOP` | 决定 stripmine 展开后 uop 的「流入/流出」速率 |
| uop 展开上限 | `UOP_NUM_ALU`、`UOP_NUM_LSU`、`UOP_INDEX_WIDTH` | 单条指令最多能拆成几个 uop |
| 向量尺度 | `VLEN`、`VLMAX_MAX`、`VTYPE_VSEW_WIDTH`、`VTYPE_VLMUL_WIDTH` | VLEN、SEW、LMUL 的位宽定义 |
| 指令编码字段 | `FUNCT6_WIDTH`、`NFIELD_WIDTH`、`VM_WIDTH`、`REGFILE_INDEX_WIDTH`、`NREG_WIDTH`、`OPCODE_WIDTH` | 32 位指令字里各字段的位宽 |

#### 4.3.3 源码精读

**组 1：派发/译码吞吐。** 标量核每周期最多译 `NUM_DE_INST` 条指令、写 `NUM_DE_UOP` 个 uop、派发 `NUM_DP_UOP` 个 uop（`DISPATCH2` 默认配置）：

[rvv_backend_define.svh:32-40](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L32-L40)：

```systemverilog
`define NUM_DE_INST   3'd2   // 每周期译码 2 条指令
`define NUM_DE_UOP    4      // 每周期写 4 个 uop 进队列
`define NUM_DP_UOP    2      // 每周期派发 2 个 uop
`define NUM_DP_VRF    4      // VRF 读口数
```

> `ISSUE_LANE=4`（[第 4-5 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L4-L5)）指的是**标量核**的 4 发射，别和后端 uop 速率混了。注意 `NUM_DE_UOP=4` 恰好够一条 LMUL=4 指令一次写完 4 个 uop。

**组 2：uop 展开上限。** 这是 stripmining 的「天花板」：

[rvv_backend_define.svh:117-126](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L117-L126)：

```systemverilog
`define UOP_NUM_ALU           8     // 算术指令最多拆 8 个 uop (=LMUL8)
`define UOP_NUM_LSU           32    // LSU 最多拆 32 个 uop (NF×LMUL 段访存)
`define UOP_INDEX_WIDTH       5     // $clog2(32)，uop 编号位宽
```

`UOP_NUM_ALU=8` 正好对应 `NREG8`/`LMUL8`；LSU 能拆到 32，是因为段加载（segment load）的 `nf`（字段数）与 `lmul` 相乘，总数更大。

**组 3：向量尺度。** VLEN 由编译宏选择，并据此推导 VLMAX 等：

[rvv_backend_define.svh:128-156](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L128-L156)（节选）：

```systemverilog
`ifdef VLEN_128
`define VLEN          128       // 当前默认构建
`endif
`define VLMAX_MAX     `VLEN     // VLMAX_max = VLEN*LMUL_max(8)/SEW_min(8) = VLEN
`define VTYPE_VSEW_WIDTH   3    // SEW 3 位
`define VTYPE_VLMUL_WIDTH  3    // LMUL 3 位
```

注释里那行 `VLMAX = VLEN*LMUL/SEW` 正是 4.1.2 公式的依据。`SEW`/`LMUL` 各 3 位（编码 8 种取值），属于 4.2 说的「flexible type encodings」在 CSR 侧的落点。

**组 4：指令编码字段宽度。** 这是「32 位字怎么切」的权威定义：

[rvv_backend_define.svh:158-171](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L158-L171)：

```systemverilog
`define FUNCT6_WIDTH         6    // funct6 主操作码
`define NFIELD_WIDTH         3    // 段加载 nf 字段
`define VM_WIDTH             1    // 掩码位
`define REGFILE_INDEX_WIDTH  5    // 向量寄存器索引（当前 5b → 32 寄存器）
`define UMOP_WIDTH           5
`define NREG_WIDTH           3    // stripmine 计数（NREG1/2/4/8）
`define IMM_WIDTH            5
`define FUNCT3_WIDTH         3
`define OPCODE_WIDTH         7
`define V0_INDEX             5'b00000   // v0 作掩码寄存器的固定索引
```

把这张表和 4.2.3 的 Chisel 字段切片对照看：`REGFILE_INDEX_WIDTH=5`（5b，编址 32 寄存器）就是当前实现；`NREG_WIDTH=3` 就是 4.1 里 `inst_nr` 的宽度；`V0_INDEX` 说明 v0 被固定用作掩码寄存器（掩码语义需要专用寄存器）。

#### 4.3.4 代码实践

**实践目标：** 用 `define.svh` 自己推算「一条 LMUL=8 的算术指令」在后端的资源占用。

**操作步骤：**

1. 由 [rvv_backend_define.svh:118](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L118) `UOP_NUM_ALU=8` 与 [opcode.svh:220](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_opcode.svh#L220) `NREG8=3'b111`，确认 LMUL8 展开为 8 个 uop。
2. 由 [define.svh:36-39](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L36-L39) `NUM_DE_UOP=4`、`NUM_DP_UOP=2`，估算这 8 个 uop 至少需要几个周期才能全部派发出去。
3. 用 `grep` 在仓库里查 `VLEN_128` / `VLEN_256` 的定义点，确认当前默认选哪个。

**预期结果：** 8 个 uop，译码端 1 周期可写满（NUM_DE_UOP=4 需要 2 周期写完 8 个），派发端 NUM_DP_UOP=2 需要 4 周期发完——你能据此体会到「1 派发 → 8 串行发射」的时间开销。

> 待本地验证：步骤 3 的默认 VLEN 取值请用 `grep -rn "VLEN_128\|VLEN_256" hdl rules` 自行确认（提示：当前默认为 128）。

#### 4.3.5 小练习与答案

**练习 1：** `UOP_NUM_LSU=32` 远大于 `UOP_NUM_ALU=8`，为什么？

**参考答案：** 段加载/存储（segment load/store）的 uop 数 ≈ `nf × lmul`，其中 `nf`（`NFIELD`，最大 8）× `lmul`（最大 8）可达 64，取整为上限 32。算术指令没有 `nf` 维度，只需 `lmul`（最大 8）。

**练习 2：** `NUM_DE_UOP` 和 `NUM_DP_UOP` 谁更可能是吞吐瓶颈？为什么？

**参考答案：** 通常是 `NUM_DP_UOP`（=2）更小。译码每周期可写 4 个 uop，但派发每周期只发 2 个，所以当指令展开数较大（如 LMUL=8）时，uop 会在派发端积压，发射成为瓶颈——这也解释了 overview 强调「serialized issue」的原因。

## 5. 综合实践

把三个最小模块串起来，完成一次「从一句文档到一组源码常量」的完整论证。

**任务：** 假设你要向同事解释「为什么 CoralNPU 能用一条 `vadd` 指令处理一整个 tile，而前端只派发了一次」。请产出一份一页纸说明，必须包含：

1. **直觉图：** 画一条「数组（N 个元素）→ stripmine 切条 → 每条 = LMUL 个寄存器 → 每个 uop 处理 VLEN/SEW 个元素」的折叠链。
2. **源码证据：** 引用 [overview.md:63-72](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L63-L72)（定义）、[rvv_backend_decode_unit_ari.sv:163-168](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_decode_unit_ari.sv#L163-L168)（LMUL→uop 数）、[define.svh:117-126](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L117-L126)（上限）三处。
3. **编码预算账：** 用 4.2.2 的位宽表说明「为什么需要回收 C 扩展才有位装下这一切」，并标注 doc（64/6b）与 RTL（32/5b）的差异。
4. **一句结论：** 用一句话点出 stripmining 的本质——「把数组并行折叠成硬件并行，用编码字段显式记录折叠比」。

> 这是「源码阅读型实践」，无需运行仿真；重点是能把文档口号与 RTL 常量一一对应起来。

## 6. 本讲小结

- **Stripmining = 折叠数组并行到硬件并行。** 一条 `vadd v0`（LMUL=4）在 Issue 端变成 `vadd v0..v3` 四个串行 uop，前端只派发一次，大幅降低派发压力并原生支持 tiling。
- **展开数有两个编码入口：** 绝大多数算术指令由 CSR `lmul`（`reduced_lmul`）驱动；整组搬运 `vmv<nr>r.v` 由指令内 `nreg` 立即数（`inst_nr`）驱动；二者最终都汇入同一个 `uop_index_max`。
- **展开后的 uop 数 = 目标寄存器组 EMUL**（非 widening 为 LMUL，widening 为 2×LMUL），上限 `UOP_NUM_ALU=8`、`UOP_NUM_LSU=32`。
- **C 扩展复用是 ISA 层面的编码预算回收**，用来同时容纳 6b 寄存器索引、灵活类型编码（SEW/LMUL）和 stripmine 计数字段；字段复用（`vs1` 低 3 位借给 `nreg`）是其位级体现。
- **doc 与 RTL 存在版本差：** 文档设计意图是 64 个 256 位向量寄存器、6b 索引；当前 RTL 实现是 32 个、`REGFILE_INDEX_WIDTH=5`、默认 `VLEN_128`。第 6 位属「预留未启用」。
- **`define.svh` 是后端尺度总表：** 吞吐（`NUM_DE_UOP`/`NUM_DP_UOP`）、展开上限（`UOP_NUM_*`）、向量尺度（`VLEN`/`VLMAX_MAX`）、编码字段宽度（`FUNCT6/NREG/REGFILE_INDEX/...`）全在此定义。

## 7. 下一步学习建议

- 想看 stripmine 展开后的 uop **如何被冒险检测与发射**，回到 [u7-l2](u7-l2-rvv-decode-dispatch.md) 重读 `rvv_backend_dispatch` 的保留站与结构冒险逻辑，关注它如何把同一条指令的多个 uop 当独立事件调度。
- 想看 stripmine 在**访存侧**的放大形态（段加载 `nf×lmul`），接着读 [u6-l1 LSU](u6-l1-lsu-slots.md) 与 `rvv_backend_decode_unit_lsu.sv`，对照 `UOP_NUM_LSU=32` 的来源。
- 想从**软件视角**感受 stripmining，进入第 10 单元 [u10-l1 RVV intrinsics](u10-l1-rvv-intrinsics.md)，看 C 代码里 `vsetvli` 如何设 `lmul`、循环如何按 `VLMAX` 切条，体会「软件 stripmine ↔ 硬件 stripmine」的呼应。
- 想理解**第 6 个索引位的体系结构代价**，可阅读 RISC-V V 扩展规范中关于「register grouping」与「major opcode space」的章节，对照本讲的编码预算论证。
