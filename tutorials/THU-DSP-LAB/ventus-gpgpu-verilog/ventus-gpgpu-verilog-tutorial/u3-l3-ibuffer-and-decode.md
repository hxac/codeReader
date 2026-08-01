# 指令缓冲 ibuffer 与译码 decodeUnit

## 1. 本讲目标

上一讲（u3-l2）我们看到：取指单元每个 warp 按 PC 取回 `NUM_FETCH=2` 条 32 位「生」指令，命中的指令才有资格进入后续流水。但 32 位二进制本身硬件无法直接使用——它必须先被「翻译」成一堆控制信号，告诉流水线：这条指令是不是向量指令？操作数从哪里来？做什么运算？结果写到哪里？而且翻译好的指令还要按 warp 暂存起来，等轮到某个 warp 发射时再送出去。

学完本讲，你应当能够：

1. 说清 `decodeUnit` 如何用一张 `casex` 译码表，把 32 位指令「查表」翻译成一个 42 位的打包控制字 `ctrlSignals`，并理解这 42 位里每个字段的含义。
2. 理解 `ibuffer` 为什么是「每个 warp 一个独立 FIFO」的组织方式，以及它如何用深度、满/空、flush 来缓冲已译码指令。
3. 说清 `ibuffer2issue` 与 `slowdown` 如何把「2 条一捆」的缓冲输出拆成「1 条一拍」的发射流，并在多个 warp 之间做轮询仲裁。
4. 能够对照 `define.v` 的指令位模式，亲手「译码」一条 `VADD_VV` 或 `ADDI`，预言它会产出哪些控制信号。

## 2. 前置知识

- **控制信号 vs. 指令二进制**：CPU/GPU 流水线前端的核心动作就是「把指令编码翻译成控制信号」。译码器本质上是一张「输入 = 32 位二进制，输出 = 一组控制位」的查表。
- **位模式（BitPat）与 `casex`**：RISC-V 一类指令往往只关心 opcode、funct3、funct7 等几个字段，其余位「任意取值都行」。Verilog 用 `casex` 配合带 `?`（任意匹配）的 32 位模式来一次性匹配一整类指令。例如 `32'b000000???????????000?????1010111` 中，`?` 表示该位 0/1 都匹配。
- **标量 vs. 向量**：本 GPU 是「标量 + 向量」混合的。标量指令（如 `ADDI`）一次只算一个值、写一个标量寄存器；向量指令（如 `VADD_VV`）一次广播给整个 warp 的所有 lane 并行算、写一个向量寄存器。译码器必须用 `isvec`、`wvd`/`wxd`、`A1_VRS1`/`A1_RS1` 等信号区分二者。
- **FIFO 反压（backpressure）**：当下游来不及消费时，上游必须停下来。本讲里的 `valid`/`ready` 握手、`full`/`empty` 标志都服务于这一点。
- **warp 级并行**：一个 SM 同时驻留 `NUM_WARP` 个 warp，它们共享同一条流水线。因此缓冲、仲裁都要「按 warp」来组织——这是本讲两条主线（ibuffer、ibuffer2issue）的出发点。

## 3. 本讲源码地图

本讲涉及的文件都在 `src/gpgpu_top/sm/pipeline/` 下，外加全局配置 `define.v`：

| 文件 | 作用 |
| --- | --- |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 全局配置。本讲关心其中的**指令位模式宏**（`ADDI`、`VADD_VV` 等 32 位带 `?` 模式）与**功能码/选择码宏**（`FN_ADD`、`A1_VRS1`、`IMM_I`、`CSR_N` 等），它们是译码表的「字典」。 |
| [src/gpgpu_top/sm/pipeline/decodeUnit.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v) | 译码器。一次译 2 条指令，输出两路控制信号给 ibuffer。 |
| [src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v) | 指令缓冲。为每个 warp 维护一个 FIFO，暂存该 warp 已译码的指令。 |
| [src/gpgpu_top/sm/pipeline/ibuffer/slowdown.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/slowdown.v) | 「2 拆 1」拆分器。把 ibuffer 取出的 2 条一捆的指令，逐条送给下游。 |
| [src/gpgpu_top/sm/pipeline/ibuffer2issue.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer2issue.v) | 缓冲→发射的桥。在 `NUM_WARP` 个 warp 的缓冲输出间做轮询仲裁，选出一路送给 issue。 |

数据流向（从取指出口到发射入口）：

```
取指命中 ──► decodeUnit(译2条) ──► ibuffer(每warp一个FIFO)
                                         │ slowdown(2拆1)
                                         ▼
                                   ibuffer2issue(NUM_WARP轮询仲裁) ──► issue(发射)
```

---

## 4. 核心概念与源码讲解

### 4.1 指令编码宏：define.v 中的位模式与功能码

#### 4.1.1 概念说明

译码器要做「查表」，就必须先有「字典」——这就是 `define.v` 里两类宏的职责：

1. **指令位模式宏**：每条（类）指令用一个 32 位、含 `?` 的常量描述「它长什么样」。`?` 是「不关心」位，配合 `casex` 可以用一个模式匹配一整类指令。
2. **功能码/选择码宏**：译码表的「输出值」。例如运算类型 `FN_ADD`、操作数来源 `A1_VRS1`、立即数类型 `IMM_I`、访存宽度 `MEM_W`、CSR 类型 `CSR_W` 等。它们都是简短的二进制常量，组合起来就构成译码结果。

#### 4.1.2 核心流程

- opcode（`inst[6:0]`）决定大类：`0010011`（OP-IMM，如 `ADDI`）、`1010111`（OPC-V，向量指令）、`1100011`（BRANCH，如 `BNE`）、`0000111`/`0100111`（向量 load/store）等。
- 再用 funct3（`inst[14:12]`）、funct7 等进一步细分具体指令。
- 译码输出由若干「选择码」拼成：选 ALU 操作数 1/2/3 的来源、选立即数类型、选 ALU 功能、选访存宽度、选 CSR 类型等。

#### 4.1.3 源码精读

先看几条典型指令的位模式（注意 `?` 表示任意匹配）：

- `ADDI`（标量 立即数加）opcode=`0010011`，funct3=`000`：

[src/define/define.v:563-563](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L563-L563)

```
`define ADDI  32'b?????????????????000?????0010011
```

- `VADD_VV`（向量-向量加）opcode=`1010111`（OPC-V），funct3=`000`（OPIVV），高 6 位 `000000`：

[src/define/define.v:717-717](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L717-L717)

```
`define VADD_VV  32'b000000???????????000?????1010111
```

对比可见：`ADDI` 高位几乎全是 `?`，而 `VADD_VV` 把 funct7=`000000` 也定死了——因为向量加必须区分于其他 OPC-V 指令。

再看译码输出会用到的一组「选择码」宏（节选）：

[src/define/define.v:421-468](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L421-L468)

```
`define B_N    2'b00   // 非分支；B_B=01 条件分支；B_J=10 无条件跳转；B_R=11 寄存器跳转
`define A1_RS1  2'b01  // ALU 操作数1 来自标量 rs1
`define A1_VRS1 2'b10  // ALU 操作数1 来自向量 vrs1
`define A1_IMM  2'b11  // ALU 操作数1 来自立即数
`define A2_RS2  2'b01  // 操作数2 来自标量 rs2
`define A2_VRS2 2'b10  // 操作数2 来自向量 vrs2
`define A2_IMM  2'b11  // 操作数2 来自立即数
`define CSR_N  2'b00   // 不操作 CSR；CSR_W/S/C = 写/置位/清位
`define IMM_I  4'd0    // I 型立即数；IMM_B=2 分支偏移；IMM_V=6 向量；IMM_Z=7 ...
`define MEM_X  2'b00   // 非访存；MEM_W/H/B = 字/半字/字节
`define M_X    2'b00   // 非访存命令；M_XRD=1 读；M_XWR=2 写
`define FN_ADD 6'd0    // ALU 功能码：加法
`define FN_SUB 6'd10
`define FN_SNE 6'd3    // set-not-equal（比较类）
```

[src/define/define.v:471-474](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L471-L474) 给出 `FN_ADD=6'd0`、`FN_SNE=6'd3` 等。注意：**功能码在不同执行单元的命名空间里会复用**——整数 ALU 的 `FN_ADD=6'd0` 和浮点的 `FN_FADD=6'd0`（[src/define/define.v:513-513](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L513-L513)）数值都是 0，靠 `fp` 位区分走哪条数据通路。这是后续执行单元讲义会反复出现的约定。

#### 4.1.4 代码实践

**实践目标**：学会把一条 32 位二进制「对号入座」到 `define.v` 的位模式。

**操作步骤**：

1. 打开 `src/define/define.v`，搜索 `VADD_VV` 与 `ADDI` 两个模式。
2. 构造一条具体的 `ADDI x1, x0, 5`（rd=x1, rs1=x0, imm=5）的二进制：imm[11:0]=`000000000101`、rs1=`00000`、funct3=`000`、rd=`00001`、opcode=`0010011`，即 `000000000101 00000 000 00001 0010011`。
3. 把它与 `ADDI` 模式逐位对照：所有 `?` 位都「通过」，定死位（funct3=`000`、opcode=`0010011`）必须相等 → 匹配成功。

**需要观察的现象**：模式里的 `?` 越多，能匹配的指令越多；`VADD_VV` 比 `ADDI` 多定死了高 6 位，所以它只匹配向量加这一种。

**预期结果**：你能解释「为什么 opcode 不足以唯一确定指令，还要 funct3/funct7 配合」。

#### 4.1.5 小练习与答案

**练习 1**：`ADDI` 与 `SLTI`（`rd = (rs1 < imm) ? 1 : 0`，有符号）的 opcode 都是 `0010011`，二者靠哪个字段区分？

**答案**：靠 funct3（`inst[14:12]`）。`ADDI` 的 funct3=`000`，`SLTI` 的 funct3=`010`。位模式里这个位是定死的，不是 `?`。

**练习 2**：为什么 `FN_ADD`（6'd0）和 `FN_FADD`（6'd0）数值相同却不会冲突？

**答案**：因为它们分属不同执行单元的命名空间，实际硬件靠另一个控制位 `fp` 来选择走整数 ALU 还是浮点通路。译码表里 `FN_ADD` 配 `fp=N`，`FN_FADD` 配 `fp=Y`。

---

### 4.2 decodeUnit：从 32 位指令到 42 位控制字

#### 4.2.1 概念说明

`decodeUnit` 是把「生指令」变成「熟控制信号」的翻译器。它的关键设计有三：

1. **一次译两条**：因为取指宽度 `NUM_FETCH=2`，译码器也同时处理 `inst_0` 和 `inst_1`，输出两路完全对称的控制信号（本模块对两条指令各有一份几乎相同的逻辑）。
2. **查表式译码**：用 `casex (inst_0_i)` 把 32 位指令与 `define.v` 的位模式逐条比对，命中一条就把一个 **42 位的打包控制字 `ctrlSignals`** 整体赋值。
3. **REGEXT 寄存器扩展**：Ventus 支持用前缀指令 `REGEXT`/`REGEXTI` 把寄存器号扩展到 8 位（突破 32 个寄存器的限制）。译码器为每个 warp 维护一个「便签」（scratchPad），记住前缀带来的扩展位，拼到后续指令的寄存器号上。

#### 4.2.2 核心流程

`ctrlSignals` 这 42 位（`[41:0]`）的布局如下（从高位到低位，这是译码表里 `{...}` 拼接的顺序）：

| 位段 | 字段 | 含义 |
| --- | --- | --- |
| [41] | isvec | 是否向量指令 |
| [40] | fp | 是否浮点 |
| [39] | barrier | 屏障指令 |
| [38:37] | branch | 分支类型（B_N/B_B/B_J/B_R） |
| [36] | simt_stack | 是否触发 SIMT 栈 |
| [35] | simt_stack_op | SIMT 栈操作方向 |
| [34:33] | csr | CSR 操作类型（N/W/S/C） |
| [32] | reverse | （乘加类操作数反向等用途） |
| [31:30] | sel_alu3 | ALU 操作数 3 来源（rs3/vrs3/frs3/SD/PC） |
| [29:28] | sel_alu2 | ALU 操作数 2 来源（rs2/vrs2/imm/size） |
| [27:26] | sel_alu1 | ALU 操作数 1 来源（rs1/vrs1/imm/pc） |
| [25:22] | sel_imm | 立即数类型（I/S/B/U/J/V/Z/2/S11/L11） |
| [21:20] | mem_whb | 访存宽度（X/W/H/B） |
| [19:14] | alu_fn | ALU 功能码（6 位） |
| [13] | mul | 是否乘法类 |
| [12:11] | mem_cmd | 访存命令（X/XRD/XWR） |
| [10] | mem_unsigned | 是否无符号 load |
| [9] | fence | fence 指令 |
| [8] | sfu | 是否走 SFU（除法/开方等慢速单元） |
| [7] | wvd | 是否写向量目的寄存器 |
| [6] | readmask | 是否读 mask |
| [5] | writemask | 是否写 mask（本版本恒为 0） |
| [4] | wxd | 是否写标量目的寄存器 |
| [3] | tc | 是否张量核指令 |
| [2] | disable_mask | 是否禁用 mask |
| [1] | custom_signal_0 | 自定义信号（用于 REGEXT 透传） |
| [0] | atomic | 是否原子访存 |

译码流程伪代码：

```
对 inst_0（和 inst_1 对称）:
  casex (inst_0):
    匹配 `ADDI : ctrlSignals_0 = { N,N,N,B_N,...,A2_IMM,A1_RS1,IMM_I,MEM_X,FN_ADD,...,wxd=Y,... }
    匹配 `VADD_VV : ctrlSignals_0 = { Y(isvec),...,A2_VRS2,A1_VRS1,IMM_X,MEM_X,FN_ADD,...,wvd=Y,... }
    ...
    default : ctrlSignals_0 = { 全是 X/无关值 }
  // 把 42 位切片成具名控制信号输出
  isvec_0  = ctrlSignals_0[41]
  alu_fn_0 = ctrlSignals_0[19:14]
  ...
```

#### 4.2.3 源码精读

**（1）模块端口**：译码器接收 2 条指令、PC、wid、flush 信号，输出两路控制信号。见 [src/gpgpu_top/sm/pipeline/decodeUnit.v:17-129](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L17-L129)。

**（2）casex 译码表**：以 `inst_0` 为例，[src/gpgpu_top/sm/pipeline/decodeUnit.v:294-298](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L294-L298) 开始的 `casex (inst_0_i)` 把每条指令映射到一个 42 位常量。表中按 opcode 分了若干「lut」组（lut0=标量整数/分支/CSR/标量load-store/浮点；lut1=OPC-V `1010111`；lut2=`0000111/0100111/0101011` 向量访存；lut3=原子；lut4=`1011011/0001011` SIMT 分支/张量/自定义）。

对照看两条典型指令在表里的译码：

- `ADDI`：标量、立即数加，写标量寄存器：

[src/gpgpu_top/sm/pipeline/decodeUnit.v:324-324](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L324-L324)

```
`ADDI : ctrlSignals_0 = {`N,`N,`N,`B_N,`N,`N,`CSR_N,`N,`A3_X,`A2_IMM,`A1_RS1,`IMM_I,`MEM_X,`FN_ADD,`N,`M_X,`N,`N,`N,`N,`N,`N,`Y,`N,`N,`N,`N};
```

读出关键位：`isvec=N`、`sel_alu2=A2_IMM`（操作数 2 取立即数）、`sel_alu1=A1_RS1`（操作数 1 取标量 rs1）、`sel_imm=IMM_I`、`alu_fn=FN_ADD`、`wxd=Y`（写标量目的）。其余全 `N`：不访存、不分支、不浮点。

- `VADD_VV`：向量-向量加，写向量寄存器：

[src/gpgpu_top/sm/pipeline/decodeUnit.v:396-396](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L396-L396)

```
`VADD_VV : ctrlSignals_0 = {`Y,`N,`N,`B_N,`N,`N,`CSR_N,`Y,`A3_X,`A2_VRS2,`A1_VRS1,`IMM_X,`MEM_X,`FN_ADD,`N,`M_X,`N,`N,`N,`Y,`N,`N,`N,`N,`N,`N,`N};
```

读出关键位：`isvec=Y`（向量）、`sel_alu2=A2_VRS2`（操作数 2 取向量 vrs2）、`sel_alu1=A1_VRS1`（操作数 1 取向量 vrs1）、`alu_fn=FN_ADD`、`wvd=Y`（写向量目的）。对比 `ADDI` 就能体会标量/向量在「操作数来源」和「写哪个寄存器堆」上的差异。`default` 分支见 [src/gpgpu_top/sm/pipeline/decodeUnit.v:947-947](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L947-L947)，未匹配指令输出全 `X`（无害）。

**（3）42 位切片成具名信号**：以 `inst_1` 为例，[src/gpgpu_top/sm/pipeline/decodeUnit.v:955-992](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L955-L992) 用一串 `assign` 把 `ctrlSignals_1` 的各 bit 切出来：

```
assign control_Signals_fp_1_o     = ctrlSignals_1[40];
assign control_Signals_isvec_1_o  = ctrlSignals_1[41];
assign control_Signals_alu_fn_1_o = ctrlSignals_1[19:14];
assign control_Signals_sel_alu1_1_o = ctrlSignals_1[27:26];
assign control_Signals_wvd_1_o    = ctrlSignals_1[7];
assign control_Signals_wxd_1_o    = ctrlSignals_1[4];
...
```

另外两个直接由指令位产生的信号值得注意：PC 的传递——`inst_0` 的 PC 就是当前 `pc_i`，`inst_1` 的 PC 是 `pc_i + 4`（同一取指块内第二条往后偏移 4 字节），见 [src/gpgpu_top/sm/pipeline/decodeUnit.v:953-953](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L953-L953)；浮点舍入模式 `rm` 直接取 `inst[14:12]`，见 [src/gpgpu_top/sm/pipeline/decodeUnit.v:154-155](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L154-L155)。

**（4）REGEXT 寄存器扩展**：当一条指令的 opcode=`0001011` 且 funct3=`010`/`011` 时，它是 `REGEXT`/`REGEXTI` 前缀指令，本身不产生运算，只携带「后续指令寄存器号的扩展前缀」和「立即数高位」。译码器先识别它：

[src/gpgpu_top/sm/pipeline/decodeUnit.v:189-201](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L189-L201)

```
regextInfo_0_isExt  = ((inst_0_i[6:0]==7'b0001011)&(inst_0_i[14:12]==3'b010)) ? 1'b1: 1'b0;
regextInfo_0_isExtI = ((inst_0_i[6:0]==7'b0001011)&(inst_0_i[14:12]==3'b011)) ? 1'b1: 1'b0;
// 从 REGEXT 指令的各个字段里取出 rd/rs1/rs2/rs3 的 3 位前缀
regextInfo_0_regprefix = {inst_0_i[22:20], inst_0_i[25:23], inst_0_i[28:26], inst_0_i[31:29]};
```

然后把这些前缀**按 warp 存进 `scratchPads_*`**（每个 warp 一份），供同一 warp 后续指令使用，并在 flush 该 warp 时清空：

[src/gpgpu_top/sm/pipeline/decodeUnit.v:243-290](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L243-L290)

最后，普通指令的寄存器号 = `{前缀, inst[19:15]}`，即把前缀拼到 5 位寄存器号前面，得到 8 位（`REGEXT_WIDTH+REGIDX_WIDTH = 3+5 = 8`）的扩展寄存器号：

[src/gpgpu_top/sm/pipeline/decodeUnit.v:985-988](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L985-L988)

```
assign control_Signals_reg_idx1_1_o = {regextInfo_0_regprefix[8:6], inst_1_i[19:15]};
assign control_Signals_reg_idx2_1_o = {regextInfo_0_regprefix[5:3], inst_1_i[24:20]};
...
```

#### 4.2.4 代码实践

**实践目标**：亲手「当一次译码器」，预言 `VADD_VV` 与 `ADDI` 的译码结果并和源码对账。

**操作步骤**：

1. 打开 `decodeUnit.v` 第 396 行（`VADD_VV`）和第 324 行（`ADDI`）。
2. 按 4.2.2 的字段表，把那串 27 个宏逐个对应到位段，填出下表：

| 字段 | VADD_VV | ADDI |
| --- | --- | --- |
| isvec[41] | Y | N |
| fp[40] | N | N |
| sel_alu2[29:28] | A2_VRS2 | A2_IMM |
| sel_alu1[27:26] | A1_VRS1 | A1_RS1 |
| sel_imm[25:22] | IMM_X | IMM_I |
| alu_fn[19:14] | FN_ADD | FN_ADD |
| wvd[7]（写向量） | Y | N |
| wxd[4]（写标量） | N | Y |

3. 解释差异：同为「加法」（`alu_fn` 都是 `FN_ADD`），`VADD_VV` 两个源都来自向量寄存器、结果写向量寄存器；`ADDI` 操作数 2 是立即数、操作数 1 是标量寄存器、结果写标量寄存器。

**需要观察的现象**：决定一条指令「长什么样」的是位模式（输入侧）；决定它「怎么执行」的是这 42 位控制字（输出侧）。同一个 `FN_ADD` 可以服务完全不同的数据通路。

**预期结果**：你能在不看答案的情况下，说出任意一条表中指令会拉高哪些控制位。**（本实践为源码阅读型，无需运行仿真。）**

#### 4.2.5 小练习与答案

**练习 1**：`BNE`（条件分支）的译码里 `branch=B_B`、`alu_fn=FN_SNE`、`sel_alu1=A1_RS1`、`sel_alu2=A2_RS2`（见 [src/gpgpu_top/sm/pipeline/decodeUnit.v:297-297](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L297-L297)）。请解释这组信号的含义。

**答案**：`B_B` 表示这是条件分支；ALU 被复用来做「比较」——`FN_SNE`（set-not-equal）比较 rs1 与 rs2，若不等则分支成立；两个操作数都来自标量寄存器（`A1_RS1`/`A2_RS2`）。可见分支判定也走 ALU。

**练习 2**：`REGEXT` 前缀指令的「运算」是什么？为什么需要按 warp 存 `scratchPads`？

**答案**：它本身不运算，只是携带后续指令的寄存器号前缀和立即数高位。因为多个 warp 交替译码，前缀必须「跟着各自的 warp 走」，所以每个 warp 维护独立的一份 `scratchPads_regPrefix`/`immHigh`，且在 flush 该 warp 时清空，避免串扰。

---

### 4.3 ibuffer：按 warp 组织的指令缓冲

#### 4.3.1 概念说明

`ibuffer`（instruction buffer）是「译码之后、发射之前」的蓄水池。它要解决两个矛盾：

1. **速率矛盾**：译码一次产出 2 条，而发射（issue）一次只吃 1 条；取指/译码有时还会因 icache miss 而停顿。需要一个 FIFO 把已经译好的指令先攒着。
2. **多 warp 资源隔离**：一个 SM 同时跑 `NUM_WARP` 个 warp，每个 warp 的指令进度各不相同。如果把所有 warp 的指令塞进一个 FIFO，就会乱套。因此 ibuffer 的设计是——**每个 warp 一个独立 FIFO**，各存各的。

#### 4.3.2 核心流程

- **入口**：来自 `decodeUnit` 的 2 条一捆的控制信号，连同 `wid`。根据 `wid` 把这一捆写进对应 warp 的 FIFO。`ibuffer_in_ready_o = ~full[wid]`——只要目标 warp 的 FIFO 没满就收。
- **存储**：用 `generate` 为 `NUM_WARP` 个 warp 各例化一个 `stream_fifo_hasflush_true`（支持 flush 的流式 FIFO），数据宽度 = `BUFFER_WIDTH(159) × NUM_FETCH(2)`，深度 `SIZE_IBUFFER=2`（每个表项装 2 条指令，共可缓存约 4 条/warp）。另外还有一个并行的窄 FIFO 存 `control_mask`。
- **flush**：当 `ibuffer_flush_wid_valid_i` 有效，按 `ibuffer_flush_wid_i` 把该 warp 的 FIFO 清空（`flush = 1 << wid`），用于分支冲刷等场景。
- **出口**：每个 warp 的 FIFO 输出先经过一个 `slowdown` 模块（见 4.4），把 2 条一捆拆成 1 条一拍，再交给 `ibuffer2issue`。
- **反压**：`ibuffer_ready_o = ~full`（按 warp），反馈给译码器/取指；出口的 ready 由下游 `ibuffer2issue` 与 `warp_sche` 共同决定。

容量估算：单 warp 可缓存指令数 ≈ `SIZE_IBUFFER × NUM_FETCH = 2 × 2 = 4` 条。

#### 4.3.3 源码精读

**（1）模块参数与端口**：`BUFFER_WIDTH=159`（单条控制信号的打包位宽）、`SIZE_IBUFFER=2`、`NUM_FETCH=2`：

[src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v:19-23](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v#L19-L23)

输入是把译码器的全部控制信号按 `NUM_FETCH` 份「打捆」进来的（`ibuffer_in_control_Signals_*_i` 每个都是 `NUM_FETCH × 单信号位宽`），输出则按 `NUM_WARP` 份展开（`ibuffer_warps_control_Signals_*_o`）。

**（2）入口打捆**：用 `generate` 把 `NUM_FETCH=2` 份信号拼成 `control_signals`（`NUM_FETCH×BUFFER_WIDTH` 位）：

[src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v:168-226](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v#L168-L226)

这段就是把 4.2 讲的那些控制字段（inst、wid、fp、branch、alu_fn、reg_idx*、wvd、pc、imm_ext、rm……）逐位拼接成一个大宽度的「指令包裹」。

**（3）写/读/flush 控制**：

[src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v:165-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v#L165-L166) 入口握手：

```
assign ibuffer_in_ready_o = ~full[ibuffer_in_control_Signals_wid_0_i]; // 就看目标warp的FIFO满没满
assign io_in_fire = ~full[...] && ibuffer_in_valid_i;
```

[src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v:234-236](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v#L234-L236) 三个 one-hot 控制：

```
assign wr_en = io_in_fire ? (8'b1 << wid) : 'b0;     // 写使能：指向当前wid的那一个FIFO
assign rd_en = ibuffer2issue_io_in_ready_i & warp_sche_io_warp_ready_i;  // 下游就绪才读
assign flush = ibuffer_flush_wid_valid_i ? (1'b1 << ibuffer_flush_wid_i) : 'b0; // 冲刷指定warp
```

**（4）每 warp 一个 FIFO**：用 `generate for` 例化 `NUM_WARP` 个 `stream_fifo_hasflush_true`，数据宽度 `BUFFER_WIDTH*NUM_FETCH`、深度 `SIZE_IBUFFER`：

[src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v:238-259](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v#L238-L259)

这是「按 warp 隔离」的核心——`full[j]`/`empty[j]` 是第 j 个 warp 的 FIFO 状态，彼此独立。

**（5）出口 slowdown 与解包**：每个 warp 的 FIFO 输出送进一个 `slowdown`（4.4 详解），再由 [src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v:308-357](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v#L308-L357) 把 `BUFFER_WIDTH` 位宽的包裹按固定偏移「拆」回成一个个具名控制信号（`inst/wid/fp/branch/alu_fn/...`），按 `NUM_WARP` 份输出给 `ibuffer2issue`。

#### 4.3.4 代码实践

**实践目标**：理解 ibuffer 的容量与反压时序。

**操作步骤**：

1. 在 `ibuffer.v` 顶部找到 `BUFFER_WIDTH`、`SIZE_IBUFFER`、`NUM_FETCH` 三个参数（第 20–22 行）。
2. 计算：单个 warp 的 FIFO 最多缓存几条指令？（`SIZE_IBUFFER × NUM_FETCH`）
3. 跟踪反压链路：当某 warp 的 FIFO `full[w]==1` 时，`ibuffer_in_ready_o` 是什么？它如何反压译码器？（提示：译码器会停止向该 wid 写入；又因「ibuffer 满时伪装 miss」，最终会反压到取指——见 u3-l1。）
4. 跟踪 flush 链路：当一个 warp 发生分支跳转、需要丢弃已取的错误路径指令时，`flush` 信号如何 one-hot 选中该 warp 的 FIFO 并清空。

**需要观察的现象**：不同 warp 的 `full/empty` 是独立的；一个 warp 满不影响另一个 warp 接收。

**预期结果**：你能画出「译码 → ibuffer（NUM_WARP 个 FIFO）→ slowdown → ibuffer2issue」的反压与 flush 信号图。**（源码阅读型实践；具体周期级行为待本地用 Verdi 观察波形验证。）**

#### 4.3.5 小练习与答案

**练习 1**：为什么 ibuffer 不做成「所有 warp 共享一个大 FIFO」？

**答案**：因为各 warp 的指令必须按各自的 PC 顺序、各自的发射资格分别管理。共享 FIFO 会导致不同 warp 的指令互相穿插、顺序无法保证，也无法对单个 warp 做 flush。按 warp 分体是最自然的选择。

**练习 2**：`SIZE_IBUFFER=2` 意味着每个 warp 的 FIFO 只有 2 个表项，但说「可缓存约 4 条指令」，为什么？

**答案**：因为每个表项装的是「一次取指的 2 条指令」（`NUM_FETCH=2`），所以深度 2 × 每项 2 条 ≈ 4 条/warp。

---

### 4.4 ibuffer2issue 与 slowdown：缓冲到发射的衔接

#### 4.4.1 概念说明

ibuffer 出口有两道「关卡」要过：

1. **slowdown（2 拆 1）**：ibuffer 每个 warp 的 FIFO 一次吐出的是「2 条一捆」的包裹，而下游发射（issue）每个时钟只取 1 条。`slowdown` 负责把一捆 2 条逐条送出。
2. **ibuffer2issue（多 warp 仲裁）**：`NUM_WARP` 个 warp 的 slowdown 出口都可能有有效指令，但 issue 每拍只能收一个 warp 的一条。`ibuffer2issue` 用**轮询仲裁器**（round-robin）在这些 warp 里公平地选一个。

二者合起来，就把「NUM_WARP × 每warp 2 条一捆」的并行缓冲，收敛成「每拍 1 条」的发射流。

#### 4.4.2 核心流程

slowdown 内部逻辑（伪代码）：

```
用 mask_reg 记录当前包裹里还有哪几条没送出（位宽 = NUM_FETCH）
当输入握手(slowdown_in_fire)：把新包裹存入 control_reg，mask_reg <= 输入 mask
当输出握手(slowdown_out_fire)：用固定优先级仲裁选最低有效位 ptr，
                                 送出 control_reg[ptr]，并清除 mask_reg[ptr]
当 mask_reg == 0：本捆送完，准备接下一捆
```

ibuffer2issue 内部逻辑（伪代码）：

```
grant = round_robin_arb(req = 各warp的in_valid)   // 选中一个warp（one-hot）
grant_bin = one2bin(grant)                         // 转成二进制编号
out = 各warp输入中第 grant_bin 路的控制信号        // 多路选择
仅当 grant!=0 且输出握手成功(out_fire) 时才真正送出
```

#### 4.4.3 源码精读

**（1）slowdown 的 2 拆 1**：用 `fixed_pri_arb` 在 `mask_reg`（当前包裹内待送出的槽位）里选最低位，`one2bin` 转成指针 `ptr`，输出 `control_reg[ptr]`：

[src/gpgpu_top/sm/pipeline/ibuffer/slowdown.v:43-84](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/slowdown.v#L43-L84)

关键几行：

```
assign slowdown_out_control_valid_o = (mask_reg != 'b0);            // 还有未送的槽位就有效
assign slowdown_out_control_signals_o = control_reg[BUFFER_WIDTH*ptr +: BUFFER_WIDTH]; // 选第ptr条
...
else if(slowdown_out_fire && slowdown_out_grant_i)
    mask_reg <= mask_next;   // 清掉刚送出的那位
```

`mask_next = mask_reg & ~(1 << ptr)` 即「送出一个就摘掉一位」，直到本捆清空。

**（2）ibuffer2issue 的轮询仲裁**：

[src/gpgpu_top/sm/pipeline/ibuffer2issue.v:149-169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer2issue.v#L149-L169)

```
round_robin_arb #(.ARB_WIDTH(`NUM_WARP)) U_round_robin_arb (
    .req(ibuffer2issue_in_valid_i),  // NUM_WARP 个 warp 的请求
    .grant(grant)                    // one-hot 选中一个
);
one2bin #(.ONE_WIDTH(`NUM_WARP), .BIN_WIDTH(`DEPTH_WARP)) U_one2bin (
    .oh(grant), .bin(grant_bin)      // 转成二进制，用作多路选择地址
);
```

**（3）多路选择输出**：把选中路（`grant_bin`）的控制信号送出，且只在真正握手时有效（否则输出 0）：

[src/gpgpu_top/sm/pipeline/ibuffer2issue.v:278-324](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer2issue.v#L278-L324)

```
assign ibuffer2issue_warps_control_Signals_inst_o =
    (grant!=8'h00 && ibuffer2issue_out_fire) ? arbiter_warps_control_Signals_inst[grant_bin*`INSTLEN+:`INSTLEN] : 0;
...
assign ibuffer2issue_out_valid_o = (| ibuffer2issue_in_valid_i) & ibuffer2issue_out_ready_i;
```

注意：所有控制信号都以 `grant_bin` 为索引从 `NUM_WARP` 路输入里「挑」出一路，这就是多 warp 到单发射的「漏斗」。

#### 4.4.4 代码实践

**实践目标**：理解「2 拆 1」与「多 warp 仲裁」如何叠加，把并行缓冲收敛成单发射流。

**操作步骤**：

1. 在 `slowdown.v` 中找到 `mask_reg` 的更新逻辑（第 75–81 行），跟踪一个含 2 条有效指令的包裹：第 1 拍送出 `ptr=0` 那条并清 `mask_reg[0]`，第 2 拍送出 `ptr=1` 那条，第 3 拍 `mask_reg==0` 才接收下一捆。
2. 在 `ibuffer2issue.v` 中找到 `round_robin_arb`（第 149 行）。假设 4 个 warp 同时有有效指令，观察 `grant` 如何在周期之间「轮转」——上一拍被选中的 warp 下一拍优先级降低，避免某个 warp 饿死。
3. 把两步串起来：某 warp 的某条指令，从 ibuffer FIFO 出队 → slowdown 拆分 → ibuffer2issue 选中 → 进入 issue，画成时序。

**需要观察的现象**：每个时钟周期，最终只有 1 个 warp 的 1 条指令被送往 issue（`ibuffer2issue_out_valid_o` 与下游 `out_ready` 握手成功时）。

**预期结果**：你能解释「为什么 `NUM_FETCH=2` 取指并不等于每周期发射 2 条」——因为中间有 slowdown 的 2 拆 1 和单口仲裁。**（轮询仲裁的精确轮转顺序待本地用 Verdi 观察确认。）**

#### 4.4.5 小练习与答案

**练习 1**：`slowdown` 里用 `fixed_pri_arb`（固定优先级），而 `ibuffer2issue` 里用 `round_robin_arb`（轮询）。为什么一个固定、一个轮询？

**答案**：`slowdown` 处理的是**同一个包裹内的 2 条指令**，必须按地址顺序先送低序号再送高序号（否则会乱序），所以用固定优先级（永远先选最低位）。`ibuffer2issue` 处理的是**平等的多个 warp**，要保证公平、避免饿死，所以用轮询，让优先级在周期间轮转。

**练习 2**：如果 `NUM_WARP=4` 且 4 个 warp 的 ibuffer 都非空、下游一直 ready，那么连续 4 拍 `grant` 会如何变化？

**答案**：轮询仲裁器会让 `grant` 在 4 个 warp 间轮转，每拍选中一个不同的 warp，4 拍恰好各被选中一次（具体起始 warp 取决于上一次状态）。这就是「公平调度」。

---

## 5. 综合实践

把本讲四条线索串起来，完成一次「纸上全流程译码 + 缓冲跟踪」：

1. **给定一段汇编**（假设属于同一个 warp）：
   - `ADDI x1, x0, 5`（标量立即数加）
   - `VADD_VV v3, v1, v2`（向量-向量加）
2. **译码**：对照 `decodeUnit.v` 的 casex 表（[src/gpgpu_top/sm/pipeline/decodeUnit.v:324-324](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L324-L324) 与 [src/gpgpu_top/sm/pipeline/decodeUnit.v:396-396](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L396-L396)），分别写出两条指令的 `isvec / sel_alu1 / sel_alu2 / alu_fn / wvd / wxd` 取值。
3. **进缓冲**：这两条指令作为「一捆 2 条」被写入该 warp 的 ibuffer FIFO。说明此时 `wr_en` 是哪个 one-hot 位、`ibuffer_in_ready_o` 由什么决定（[src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v:165-166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/ibuffer.v#L165-L166)）。
4. **出缓冲**：该 warp 的 FIFO 出口经 `slowdown` 把这 2 条拆成 2 拍（[src/gpgpu_top/sm/pipeline/ibuffer/slowdown.v:43-84](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer/slowdown.v#L43-L84)），再由 `ibuffer2issue` 选中该 warp（[src/gpgpu_top/sm/pipeline/ibuffer2issue.v:149-169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/ibuffer2issue.v#L149-L169)），逐条送往 issue。
5. **自检**：在 issue 入口处，你应当看到先一条「标量、操作数2=立即数、写标量」的指令，再一条「向量、两源都是向量、写向量」的指令——和你第 2 步预言一致。

> 提示：若想用仿真验证，可参考 u1-l4 的 `make run-vcs-4w4t`，在 Verdi 中把 `decodeUnit` 的 `control_Signals_*_o` 与 `ibuffer2issue` 的 `ibuffer2issue_warps_control_Signals_*_o` 加入波形，对照某条 `VADD_VV` 观察其从译码到发射的逐拍传播。

## 6. 本讲小结

- `decodeUnit` 用一张 `casex` 译码表，把 32 位指令「查表」成一个 42 位的打包控制字 `ctrlSignals`，再切片成 `isvec/fp/alu_fn/sel_alu*/sel_imm/wvd/wxd/...` 等具名控制信号；一次译 2 条（`inst_0`/`inst_1`）。
- 译码输出由 `define.v` 的两类宏支撑：指令**位模式**（输入匹配）与**功能码/选择码**（输出取值）；同一个 `FN_ADD` 数值可被整数 ALU 与浮点通路复用，靠 `fp` 位区分。
- `REGEXT`/`REGEXTI` 前缀指令通过每 warp 的 `scratchPads` 把寄存器号扩展到 8 位（`{前缀, 5位寄存器号}`），是 Ventus 突破 32 寄存器限制的自定义机制。
- `ibuffer` 是「每 warp 一个 FIFO」的指令缓冲（深度 `SIZE_IBUFFER=2`、每项 `NUM_FETCH=2` 条），靠 `~full[wid]` 反压译码、靠 one-hot `flush` 清空指定 warp。
- `slowdown` 把「2 条一捆」拆成「1 条一拍」（固定优先级，保序）；`ibuffer2issue` 在 `NUM_WARP` 个 warp 间做轮询仲裁，公平地选出一路送 issue。二者合起来把并行缓冲收敛成单发射流。

## 7. 下一步学习建议

- 译码与缓冲产出的控制信号，下一站是**发射与记分板**。建议进入 **u3-l4（issue 与 scoreboard）**，看 `issue` 如何依据这些控制信号选路、`scoreboard` 如何用它们检测数据冒险。
- 想深入理解 `sel_alu1/2/3`、`reg_idx1/2/3/w` 如何真正变成操作数，可预习 **u4-l1（operand_collector 与寄存器堆）**——本讲的「选择码」在那里被用来决定从标量/向量寄存器堆的哪个 bank、哪个地址取数。
- 想了解 `simt_stack`、`branch`、`csr` 等控制位如何驱动 SIMT 分支与 CSR 操作，可后续阅读 **u5-l2（CSR 与分支）** 与 **u5-l3（SIMT 栈与分支发散）**。
