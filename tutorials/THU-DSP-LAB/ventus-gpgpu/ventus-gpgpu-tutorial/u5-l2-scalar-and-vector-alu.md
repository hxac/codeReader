# 标量 ALU 与向量 ALU

## 1. 本讲目标

上一讲（u5-l1）给出了 SM 后端「双发射 + 8 类执行单元」的全景图，并指出标量指令走 `issueX → out_sALU`、向量指令走 `issueV → out_vALU`。本讲把这两个端口后面挂着的整数运算核彻底拆透。读完本讲你应当能够：

- 说清 `ScalarALU` 这颗**单 lane 整数运算核**支持哪些运算，以及如何用一个 5 位 `func`、配合单个加减法器复用出 ADD/SUB/SLT/AND/OR/XOR/移位/MIN/MAX；
- 说清标量执行单元 `ALUexe` 如何把 `ScalarALU` 包起来，并在同一通路里兼任**分支解析器**（输出 `BranchCtrl` 的 `jump`/`new_pc`）；
- 说清向量执行单元 `vALUv2` 如何用「按 lane 复制 `ScalarALU`」的方式，让一个 warp 内的 32 个线程**同一拍并行**算完一条向量指令，结果如何带上 `mask` 送往写回，以及 SIMT 分支如何借用各 lane 的比较结果。

本讲是 u5-l1 的细化，覆盖三个最小模块：`ScalarALU`、`ALUexe`、`vALUv2`。它们共用同一个 `ScalarALU` 内核——这正是 Ventus「向量化 = 把标量运算铺满 lane」设计哲学的最佳样本。

## 2. 前置知识

阅读本讲前，请确保你已经理解：

- **warp / thread / lane**：一个 warp 由 `num_thread`（默认 32）个线程组成；一条向量寄存器是「整条向量」，按 lane 切分，每个 lane 对应一个线程的标量值（见 u2-l1）。
- **CtrlSigs 控制信号**：译码器把 32 位指令翻成的控制包（见 u4-l2），本讲关键字段有 `alu_fn`（运算码）、`wxd`/`wvd`（写标量/向量寄存器）、`branch`（分支类型）、`isvec`、`reverse`、`simt_stack`、`mask`。
- **双发射**：`issueX` 发标量指令、`issueV` 发向量指令；标量走 `out_sALU`、向量走 `out_vALU`（见 u5-l1）。
- **DecoupledIO / `Queue(pipe=true)`**：本讲大量出现「输入握手 + 1 深流水队列 + 输出握手」的包装手法。
- **关键参数**（`parameters.scala`）：`xLen=32`、`num_thread=32`、`num_lane=num_thread`、`num_sfu=(num_thread>>2).max(1)`。`num_lane` 默认等于 `num_thread`，这决定了 `vALUv2` 走「满宽」分支。

一个核心直觉先放在这里：**Ventus 的向量整数运算并不发明新的运算电路，而是把同一个标量 `ScalarALU` 例化 32 份**，每份喂一个 lane 的操作数，同拍算完。理解了这一点，本讲就理解了一大半。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ventus/src/pipeline/ALU.scala` | 定义 `ALUOps` 运算码常量与 `ScalarALU` 内核——单 lane 的加减/逻辑/移位/比较/MIN/MAX 运算电路。本讲最底层的硬件原语。 |
| `ventus/src/pipeline/execution.scala` | 定义标量执行单元 `ALUexe`（包装 `ScalarALU` + 分支输出）与向量执行单元 `vALUv2`（按 lane 复制 `ScalarALU`）。本讲核心。 |
| `ventus/src/pipeline/pipe.scala` | SM 流水线总装：例化 `alu`/`valu` 并连到 `issueX`/`issueV`、`branch_back`、`simt_stack`、写回模块 `wb`。 |
| `ventus/src/pipeline/issue.scala` | 定义执行数据包 `vExeData`/`sExeData`（`in1/in2/in3/mask/ctrl`），及 `Issue` 的分发优先级链。 |
| `ventus/src/pipeline/operandCollector.scala` | 定义写回控制包 `WriteScalarCtrl`/`WriteVecCtrl`。 |
| `ventus/src/pipeline/SIMT_STACK.scala` | 定义 `vec_alu_bus`（`if_mask`），即 `vALUv2` 给 SIMT 栈的旁路输出类型。 |
| `ventus/src/top/parameters.scala` | 关键参数 `num_thread`/`num_lane`/`xLen`/`num_sfu`。 |

## 4. 核心概念与源码讲解

### 4.1 ScalarALU：单 lane 整数运算内核

#### 4.1.1 概念说明

`ScalarALU` 是一个**纯组合**的整数运算电路：给它两个（外加一个预留的第三）32 位操作数和一个 5 位功能码 `func`，它同一拍给出 32 位结果 `out` 和一位比较结果 `cmp_out`。它本身不带状态、不做握手，是最底层的「算术砖块」。

之所以先讲它，是因为它被两处复用：

- `ALUexe` 例化 **1 个** `ScalarALU`，服务标量指令；
- `vALUv2` 例化 **`num_lane`（默认 32）个** `ScalarALU`，服务向量指令的每个 lane。

所以搞懂 `ScalarALU` 的运算表，标量和向量的运算就都搞懂了。

#### 4.1.2 核心流程

`ScalarALU` 的设计哲学是「**一个加减法器干所有事**」——核心硬件只有一个加法器、一个桶形移位器、一组逻辑门，靠 `func` 多路选择把同一批中间量拼成不同运算的结果。

1. **加减法**：减法变成「取反加一」。令
   - \( \text{in2\_inv} = \text{isSub} ? \sim\text{in2} : \text{in2} \)
   - \( \text{adder\_out} = \text{in1} + \text{in2\_inv} + \text{isSub} \)

   ADD 时 `isSub=0` 得 `in1+in2`；SUB 时 `isSub=1` 得 \( \text{in1}+(\sim\text{in2})+1=\text{in1}-\text{in2} \)。

2. **比较**：有/无符号小于（SLT/SLTU）复用减法结果的最高位与操作数符号位；相等（SEQ）用 `in1^in2==0` 判定。最终比较结果 `cmp_out` 由 `cmpInverted`（是否取反）、`cmpEq`（是否相等比较）等 `func` 位微调，从而用一套电路覆盖 SEQ/SNE/SLT/SLTU/SGE/SGEU。

3. **逻辑**：AND/OR/XOR 用三路 `Mux` 选出。

4. **移位**：左移实现成「反转 → 右移 → 再反转」，于是只造一个右移桶形移位器；算术右移 SRA 靠把符号位拼进移位输入实现。

5. **MIN/MAX**：用比较器选大小，区分有符号（`asSInt`）/无符号两套。

6. **A1ZERO/A2ZERO**：特殊功能码，直接透传 `in2` 或 `in1`（用于忽略某个操作数、或 MV 类搬运语义）。

最后用一个多路选择把「加法器 / 比较 / 逻辑 / 移位 / MIN-MAX / 透传」按 `func` 选出唯一的 `out`。

#### 4.1.3 源码精读

运算码常量集中在 `ALUOps` 对象里，它们是 5 位（`SZ_ALU_FUNC=5`）：

[ventus/src/pipeline/ALU.scala:17-57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L17-L57) — 定义 `FN_ADD/FN_SUB/FN_SL/FN_SR/FN_SRA/FN_AND/FN_OR/FN_XOR/FN_SLT/.../FN_MIN/FN_MAX/FN_A1ZERO/FN_A2ZERO` 等运算码，以及一组用位域判断类别的辅助函数 `isSub/isCmp/cmpUnsigned/cmpInverted/cmpEq/isMIN`。

几个关键的位域判定函数（摘自上段）：

```scala
def isSub(cmd: UInt) = (cmd >= FN_SUB) & (cmd <= FN_SGEU)   // 10~15 视作减法族
def isCmp(cmd: UInt) = (cmd >= FN_SLT) & (cmd <= FN_SGEU)   // 12~15 是比较族
def cmpUnsigned(cmd) = cmd(1)
def cmpInverted(cmd) = cmd(0)
def cmpEq(cmd) = !cmd(3)
def isMIN(cmd) = (cmd(4,2) === "b100".U)                    // 16~19 是 MIN/MAX 族
```

这意味着只要 `func` 落在 `FN_SUB..FN_SGEU`（10~15），`isSub` 即为真，加减法器自动切到减法模式——这是 ADD/SUB/SLT/SRA 共用加法器的关键。

`ScalarALU` 的端口：

[ventus/src/pipeline/ALU.scala:61-70](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L61-L70) — `in1/in2/in3` 各 32 位输入、`func` 5 位功能码、`out` 32 位结果、`cmp_out` 1 位比较结果。

> **一个值得注意的细节**：`in3` 在 `ScalarALU` 内部**并不参与任何算术运算**（通读函数体，`io.in3` 从未被引用）。它在端口里保留，是给上层复用的预留槽——在标量分支路径里 `in3` 承载**分支目标地址**（见 4.2.3），在浮点单元里它才是第三操作数（fmadd 等）。对纯整数 ALU 运算，`in3` 无用。

加减法器与比较的核心几行：

[ventus/src/pipeline/ALU.scala:72-81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L72-L81) — `in2_inv` 配合 `isSub` 把减法归一为加法；`adder_out` 同时供 ADD/SUB/比较使用；`slt` 借减法结果最高位判断小于；`cmp_out` 最终由 `cmpInverted ^ (相等或小于)` 得到。

最终结果选择：

[ventus/src/pipeline/ALU.scala:111-113](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L111-L113) — 优先级为 `A1ZERO`（透传 in2）→ `A2ZERO`（透传 in1）→ `isMIN`（MIN/MAX 结果）→ 其余（加法/比较/逻辑/移位）。

#### 4.1.4 代码实践

**实践目标**：手工执行一次 `ScalarALU` 运算，验证「减法即取反加一」与「比较复用减法」。

**操作步骤**：

1. 打开 [ALU.scala:72-81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L72-L81)。
2. 取 `func=FN_SUB`、`in1=5`、`in2=3`：因 `isSub(FN_SUB)` 为真，`in2_inv = ~3 = 0xFFFFFFFC`，`adder_out = 5 + 0xFFFFFFFC + 1 = 2`，最终 `out = adder_out = 2`。
3. 取 `func=FN_SLT`（有符号小于）、`in1=5`、`in2=3`：`isSub` 为真，`adder_out = 5-3 = 2`，最高位为 0；两数同号（最高位都 0）时 `slt = adder_out(31) = 0`，`cmpInverted(12)=0`，故 `cmp_out = 0`（5 不小于 3，正确）。
4. 取 `func=FN_A2ZERO`（9）：查 [ALU.scala:111-113](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L111-L113)，`out = in1 = 5`，第二操作数被「置零」，相当于把 `in1` 直通输出。

**需要观察的现象**：加减法、比较是否都由 `adder_out` 一根线推导出来，没有出现第二个加法器或独立比较器。

**预期结果**：上述三例分别得到 `out=2`、`cmp_out=0`、`out=5`，与 RISC-V 语义一致。

> 本实践为源码阅读型手算推导，无需运行仿真即可完成。

#### 4.1.5 小练习与答案

**练习 1**：`func=FN_ADD`、`in1=7`、`in2=6` 时 `adder_out` 与 `out` 各是多少？
**答案**：`isSub(FN_ADD)` 为假，`in2_inv=6`，`adder_out=7+6+0=13`，`out=adder_out=13`。

**练习 2**：为什么 `ScalarALU` 里看不到独立的「比较器」，却仍能给出 SLT 结果？
**答案**：因为减法 `in1-in2` 的结果最高位、连同两个操作数的符号位，足以判定有符号/无符号的大小关系（`slt` 的逻辑），所以比较「免费」复用了加减法器。

**练习 3**：`in3` 在 `ScalarALU` 内部参与运算吗？它在本讲后续会被用来做什么？
**答案**：不参与算术运算（函数体未引用 `io.in3`）。在标量分支路径里，`in3` 被用作分支目标地址 `new_pc` 的来源（见 4.2.3）。

---

### 4.2 ALUexe：标量执行单元 + 分支解析器

#### 4.2.1 概念说明

`ALUexe` 是标量指令真正进入执行流水的那一级。它做两件事：

1. 把 `ScalarALU` 的结果包装成 `WriteScalarCtrl`（写标量寄存器控制包），经一个深度 1 的流水队列送往写回模块；
2. **兼任分支解析器**：对分支/跳转指令，把「是否跳转 `jump`」与「跳转目标 `new_pc`」包装成 `BranchCtrl`，送往 `branch_back` 汇总后回传给取指的 warp 调度器。

把分支解析放在标量 ALU 里是个很自然的选择：条件分支（如 RV32I 的 BEQ）本质是一次比较——而 `ScalarALU` 已经能算 `cmp_out`。于是 BEQ 的 `alu_fn` 取 `FN_SEQ`，`cmp_out` 就是「两数相等」，直接当作「是否跳转」。

#### 4.2.2 核心流程

`ALUexe` 的端口有三组握手：

- `in`：来自 `issueX.io.out_sALU` 的标量指令（`sExeData`：`in1/in2/in3 + ctrl`）。
- `out`：送往写回 `wb.io.in_x(0)` 的标量结果（`WriteScalarCtrl`）。
- `out2br`：送往 `branch_back.io.in0` 的分支结果（`BranchCtrl`）。

核心是依据 `ctrl.branch`（2 位分支类型，译码定义于 `DecodeUnit`：`B_N=0/B_B=1/B_J=2/B_R=3`）分流：

| `ctrl.branch` | 含义 | `out`（写回） | `out2br`（分支） |
| --- | --- | --- | --- |
| `B_N`(0) | 非分支 | 若 `wxd` 则有效 | 无效 |
| `B_B`(1) | 条件分支 | 无效 | `jump = cmp_out` |
| `B_J`(2) / `B_R`(3) | 无条件跳转/JALR | 若 `wxd` 则有效（写返回地址） | `jump = true` |

`new_pc` 一律取自 `in3`（译码阶段已把 PC+imm 或 rs1 算好放进 `in3`）。`jump` 对条件分支取 `cmp_out`，对无条件跳转恒为真。输入 `ready` 同样按分支类型决定：条件分支只看分支队列是否可收，非分支只看写回队列是否可收。

#### 4.2.3 源码精读

`BranchCtrl` 与 `ALUexe` 端口：

[ventus/src/pipeline/execution.scala:20-31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L20-L31) — `BranchCtrl` 含 `wid`（哪个 warp）、`jump`（是否跳转）、`new_pc`（目标地址）；`ALUexe` 的三个端口 `in`/`out`/`out2br`。

例化 `ScalarALU` 并喂入操作数与功能码：

[ventus/src/pipeline/execution.scala:32-45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L32-L45) — `alu.func := io.in.bits.ctrl.alu_fn(4,0)`，把运算码接到 ALU；结果 `wb_wxd_rd := alu.io.out`，`reg_idxw`/`wxd`/`warp_id` 从 `ctrl` 透传。

> **一个细节**：`CtrlSigs.alu_fn` 是 **6 位**（[scoreboard.scala:37](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L37) 定义为 `UInt(6.W)`），而 `ScalarALU.func` 只取 `alu_fn(4,0)` 低 5 位。第 5 位（最高位）用于区分 MUL/MAC 族（见 `isMUL/isMAC` 的位域判断）或被乘法等单元另作他用，对纯 ALU 运算取低 5 位即可。

分支信息装配（本小节的核心）：

[ventus/src/pipeline/execution.scala:50-64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L50-L64) — `io.in.ready` 用 `MuxLookup(ctrl.branch, ...)`：`B_B` 等分支队列可收、`B_N` 等写回队列可收；`new_pc := in3`；`jump` 对 `B_B` 取 `alu.io.cmp_out`、对 `B_J/B_R` 取 `true`；两个 `valid` 按 `branch` 类型分流——`B_B` 只发分支、`B_N` 只发写回。

`pipe.scala` 里的连接实证：

[ventus/src/pipeline/pipe.scala:378-379](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L378-L379) — `issueX.io.out_sALU <> alu.io.in`（标量指令进入）；同时 `issueV.io.out_sALU.ready := false.B` 禁用向量侧的标量端口。

[ventus/src/pipeline/pipe.scala:392](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L392) — `alu.io.out2br <> branch_back.io.in0`，分支结果汇入 `Branch_back`。

[ventus/src/pipeline/pipe.scala:416](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L416) — `wb.io.in_x(0) <> alu.io.out`，标量 ALU 占用写回模块的 0 号标量输入口。

> 旁证 `Branch_back` 的角色：[ventus/src/pipeline/writeback.scala:18-29](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L18-L29) 用一个 2 路 `Arbiter` 把 `in0`（来自 ALUexe 的标量分支）与 `in1`（来自 SIMT 栈的向量分支汇合）仲裁成一路 `out`，再回 `warp_scheduler` 改 PC。

#### 4.2.4 代码实践

**实践目标**：追踪一条标量 `ADDI x5, x1, 10` 在 `ALUexe` 中的完整通路。

**操作步骤**：

1. 在 [pipe.scala:378](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L378) 确认 `ADDI` 经 `issueX.out_sALU` 进入 `alu.io.in`。
2. 在 [execution.scala:32-45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L32-L45) 确认 `in1=x1`、`in2=10`（立即数）、`func=alu_fn(4,0)=FN_ADD`。
3. 因 `ctrl.branch=B_N`，查 [execution.scala:59-64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L59-L64)：`result.io.enq.valid = in.valid && wxd && ...`（写回有效），`result_br` 无效。
4. 结果 `wb_wxd_rd = x1+10`、`reg_idxw = 5`、`wxd = true`，经 `alu.io.out` 进入 [pipe.scala:416](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L416) 的 `wb.in_x(0)`。

**需要观察的现象**：非分支指令只走 `out`、`out2br` 全程静默；写回包里同时携带了数据、目标寄存器号、warp id。

**预期结果**：`x5 ← x1 + 10` 经 0 号标量写回口送达寄存器堆。若想看波形，可在 `ScalarALU` 内启用被注释的 `printf`（[ALU.scala:115](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L115)）后重新 `make verilog` 并仿真——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一条 `JAL x1, offset`（`branch=B_J`、`wxd=true`）经过 `ALUexe`，`out` 与 `out2br` 哪个会有效？
**答案**：`B_J` 落入默认分支，`result_br.valid` 拉高（必跳），同时 `result.valid` 在 `wxd` 时也拉高（写返回地址）。二者互以对方 `ready` 为前提，从而原子地「写返回地址 + 跳转」。

**练习 2**：条件分支 `BEQ` 的 `jump` 信号从哪里来？
**答案**：译码把 BEQ 的 `alu_fn` 设为 `FN_SEQ`，`ScalarALU` 据此算出 `cmp_out`（两数相等），`ALUexe` 在 `B_B` 分支里 `jump := alu.io.cmp_out`（[execution.scala:57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L57)）。

**练习 3**：`new_pc` 为什么取 `in3` 而不是 `alu.io.out`？
**答案**：跳转目标地址是「地址」而非「运算结果」，译码/取操作数阶段已算好放进 `in3`；`alu.io.out` 是算术/逻辑结果，语义不同。目标地址走 `in3` 让 ALU 运算通路专心做比较判定（`cmp_out`），职责分离。

---

### 4.3 vALUv2：向量 ALU（按 lane 复制）

#### 4.3.1 概念说明

`vALUv2` 是向量指令的整数执行单元。它的核心思想在本讲开头就点明了：**把 `ScalarALU` 例化 `hardThread`（= `num_lane`，默认 32）份**，每份服务一个 lane，于是「一条向量加法」就变成「32 个标量加法同拍并行」。这是 u5-l1「lane 复制」直觉的落点。

`vALUv2` 有两个参数：`softThread`（软件视角的向量长度，默认 `num_thread=32`）和 `hardThread`（实际铺设的物理 lane 数，默认 `num_lane=num_thread=32`）。

- 当 `softThread == hardThread`（默认）：一拍算完全部 32 个 lane，吞吐最高，本讲主讲这一支；
- 当 `softThread > hardThread`：物理 lane 不够，需分 `softThread/hardThread` 拍串行处理（即「chime」分段），与 u5-l1 讲过的 SFU「单元数少于 lane 导致变长」是同一思想，详见 u5-l3。

`vALUv2` 还承担一个 SIMT 专属职责：**SIMT 分支指令**（如 `beqv`，`ctrl.simt_stack=true`）经它执行，把「每个 lane 是否满足分支条件」汇成一个 `if_mask` 掩码送给 SIMT stack，用于分支分歧处理（详见 u5-l5）。

#### 4.3.2 核心流程

默认配置（`softThread == hardThread == 32`）下的流程：

```text
io.in(vExeData)          # in1/in2/in3 均为 Vec(32), mask 为 Vec(32,Bool)
   │  for x in 0..31:
   │    alu(x).in1 := in1(x);  alu(x).in2 := in2(x)
   │    alu(x).func := ctrl.alu_fn(4,0)
   ▼
32 × ScalarALU  (lane 复制，同拍并行)
   │  alu(x).out  → wb_wvd_rd(x)      # 向量结果逐 lane 写回
   │  alu(x).cmp_out → if_mask(x)     # 仅 SIMT 分支用
   ▼
分两路出口：
   ├── out (WriteVecCtrl)        → 写回向量寄存器（普通向量运算）
   └── out2simt_stack (if_mask)  → SIMT 栈（仅 simt_stack=1 的分支）
```

除纯运算外，`vALUv2` 还在 per-lane 循环里处理几类 RVV/SIMT 特殊语义：

- **`reverse`**：某些向量指令（如 `subr`）需要交换 `in1/in2`，由 `ctrl.reverse` 在每个 lane 内换序；
- **向量比较**（`FN_SLT/SEQ/...`）：lane 结果取 `cmp_out`（1 位布尔）而非 `out`；
- **掩码归约伪指令**（`FN_VMANDNOT/VMORNOT/VMNAND/VMNOR/VMXNOR`）：复用标量 AND/OR/XOR，对输入取反或输出取反实现；
- **`FN_VID`**：lane 结果直接取 lane 编号 `x`（`vid.v`，给每个 thread 自己的编号）；
- **`FN_VMERGE`**：按 `mask(x)` 在 `in1`/`in2` 间逐 lane 选择（`vmerge`）。

最关键的 SIMT 出口：当 `ctrl.simt_stack=1`（`beqv`），结果走 `out2simt_stack`，`if_mask` 取每 lane `cmp_out` 的按位取反：

\[
\text{if\_mask} = \mathord{\sim}\big(\text{VecInit}(\text{alu.map}(\_.\text{cmp\_out})).\text{asUInt}\big)
\]

这个掩码告诉 SIMT 栈「哪些 lane 条件成立」，用于压栈/出栈。

> **关于 mask**：`vALUv2` 对**所有 lane 都执行运算**，`mask` 只是随结果包 `wvd_mask` 透传到写回；真正的「不活动 lane 不提交」由下游写回阶段依据 `wvd_mask` 落实（见 u4-l4）。

#### 4.3.3 源码精读

`vALUv2` 的例化与参数：

[ventus/src/pipeline/pipe.scala:76-77](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L76-L77) — `val valu = Module(new vALUv2(num_thread, num_lane))`。默认 `num_lane = num_thread = 32`（[parameters.scala:57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L57)），故 `softThread == hardThread`。

按 lane 复制 `ScalarALU`——本讲最关键的一行：

[ventus/src/pipeline/execution.scala:502](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L502) — `val alu = VecInit(Seq.fill(hardThread)((Module(new ScalarALU())).io))`，即例化 `hardThread`（默认 32）个 `ScalarALU`，一个 lane 一个。这一行就是「lane 复制」的字面体现。

逐 lane 喂数据、收结果（满宽分支）：

[ventus/src/pipeline/execution.scala:507-517](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L507-L517) — 每个 lane `x`：`alu(x).in1 := in1(x)`、`in2 := in2(x)`、`in3 := in3(x)`、`func := alu_fn(4,0)`，并把 `alu(x).out` 写入 `wb_wvd_rd(x)`；`reverse` 时交换该 lane 的 `in1/in2`。**这就是向量加法的全部：每个 lane 各跑一颗标量 ALU。**

向量比较结果改取 `cmp_out`：

[ventus/src/pipeline/execution.scala:530-535](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L530-L535) — 当 `alu_fn` 为 `SEQ/SNE/SGE/SGEU/SLT/SLTU` 时，lane 结果取 `alu(x).cmp_out`（1 位比较结果）。

`FN_VID` / `FN_VMERGE`：

[ventus/src/pipeline/execution.scala:536-542](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L536-L542) — `FN_VID` 把 lane 编号 `x` 当结果；`FN_VMERGE` 按 `mask(x)` 在 `in1(x)/in2(x)` 间选，并把该 lane 的 `wvd_mask` 强制为 `true`。

结果包携带 mask 并送出（普通向量写回与 SIMT 分支二选一）：

[ventus/src/pipeline/execution.scala:556-564](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L556-L564) — 公共字段（`warp_id/reg_idxw/wvd/wvd_mask`）从 `ctrl` 透传，`wvd_mask := io.in.bits.mask`；普通指令 `result.valid := in.valid && wvd && !simt_stack`；SIMT 分支指令把各 lane `cmp_out` 拼成 `if_mask`（取反以契合 SIMT stack 约定）经 `result2simt` 送出。

`io.in.ready` 的二选一：

[ventus/src/pipeline/execution.scala:570](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L570) — `Mux(simt_stack, result2simt.io.enq.ready, result.io.enq.ready)`：SIMT 分支等 SIMT 队列可收，普通向量等写回队列可收。

`pipe.scala` 里的连接实证：

[ventus/src/pipeline/pipe.scala:374](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L374) — `issueV.io.out_vALU <> valu.io.in`（向量指令进入）。

[ventus/src/pipeline/pipe.scala:387](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L387) — `simt_stack.io.if_mask <> valu.io.out2simt_stack`（SIMT 分支掩码送往 SIMT stack）。

[ventus/src/pipeline/pipe.scala:422](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L422) — `wb.io.in_v(0) <> valu.io.out`，向量 ALU 占用写回模块的 0 号向量输入口。

`vec_alu_bus`（`if_mask` 的类型）：

[ventus/src/pipeline/SIMT_STACK.scala:114-117](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/SIMT_STACK.scala#L114-L117) — `if_mask` 为 `UInt(num_thread.W)`，每 bit 对应一个 lane 的条件成立情况。

> **分批模式（可选阅读）**：当 `num_lane < num_thread` 时走 [execution.scala:573-737](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L573-L737) 的 `else` 分支，由 `sendCS`/`recvCS` 计数器状态机把一条向量指令拆成 `maxIter = softThread/hardThread` 拍处理。默认配置下该分支不启用，但它是「面积换吞吐」的参数化伸缩能力。

#### 4.3.4 代码实践

**实践目标**：追踪一条向量加法 `vadd.vv v3, v1, v2` 在 `vALUv2` 中的数据通路，体会「lane 复制 + 同拍并行 + mask 透传」。

**操作步骤**：

1. 在 [pipe.scala:374](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L374) 确认 `vadd` 经 `issueV.out_vALU` 进入 `valu.io.in`，`in1=v1`、`in2=v2`、`alu_fn=FN_ADD`、`simt_stack=false`、`mask` 全 1。
2. 在 [execution.scala:502](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L502) 确认 32 个 `ScalarALU` 同时存在；在 [507-517 行](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L507-L517) 确认每个 lane `x` 都在做 `v1[x] + v2[x]`，结果写入 `wb_wvd_rd(x)`。
3. 在 [556-564 行](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L556-L564) 确认 `wvd_mask := mask`、`simt_stack=false`，于是结果从 `out` 送往 [pipe.scala:422](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L422) 的 `wb.in_v(0)`。
4. 填写下面这张 lane 追踪表（假设 `v1=1,2,...,32`、`v2` 全为 10、mask 全 1）：

| lane x | in1(x) | in2(x) | func | alu(x).out | wb_wvd_rd(x) |
| --- | --- | --- | --- | --- | --- |
| 0 | 1 | 10 | FN_ADD | 11 | 11 |
| 1 | 2 | 10 | FN_ADD | 12 | 12 |
| ... | ... | ... | ... | ... | ... |
| 31 | 32 | 10 | FN_ADD | 42 | 42 |

**需要观察的现象**：32 个 lane 在**同一拍**内全部完成加法（组合电路，无迭代）；mask 不影响运算本身，只随结果透传到写回。

**预期结果**：`v3[i] = v1[i] + v2[i]` 对全部 32 个 lane 同时成立，结果经 0 号向量写回口送出，活动掩码由写回阶段据 `wvd_mask` 落实。如需在仿真中观测，可在 `sim-verilator` 下跑 `vecadd` 用例并 `--dump-mem` 核对——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：默认 `num_lane=num_thread=32` 时，`vALUv2` 走哪个分支？为什么一条向量加法只需一拍？
**答案**：走 `softThread == hardThread` 分支（[507 行起](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L507)）。因为 32 个 `ScalarALU` 是纯组合的并行电路，32 个 lane 同一拍各自算完，无需分批迭代。

**练习 2**：一条 SIMT 分支 `beqv`（`simt_stack=true`）经过 `vALUv2`，结果从哪个端口送出？`if_mask` 是怎么算出来的？
**答案**：从 `out2simt_stack` 送出（`out` 此时被 `!simt_stack` 屏蔽）。`if_mask = ~(VecInit(alu.map(_.cmp_out)).asUInt)`，即把每个 lane 的比较结果按位取反拼接（[562-563 行](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L562-L563)），供 SIMT stack 判定分支分歧。

**练习 3**：向量运算时 mask 在 `vALUv2` 内部是否屏蔽了不活动 lane 的计算？真正的屏蔽发生在哪里？
**答案**：没有。`vALUv2` 对所有 lane 都执行运算，`mask` 只是随结果包 `wvd_mask` 透传；真正的「不活动 lane 不提交」由下游写回阶段依据 `wvd_mask` 落实。

---

## 5. 综合实践

把标量与向量两条通路串起来对照。设想一个 warp 同时执行两条指令（双发射）：

- 标量侧：`ADDI x5, x1, 10`
- 向量侧：`vadd.vv v3, v1, v2`（mask 全 1）

请完成以下任务：

1. **入口分流**：在 [pipe.scala:374](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L374) 与 [pipe.scala:378](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L378) 分别确认两条指令由哪个 Issue 端口送入哪个执行单元。
2. **内核复用**：指出两条指令最终都落在同一个 `ScalarALU` 运算核上——标量侧用了 1 份、向量侧用了 32 份，且 `func` 都是 `FN_ADD`。
3. **结果汇总**：填表对比二者输出端口与写回口：

| 指令 | 执行单元 | 结果端口 | 写回口 | 结果形态 | mask |
| --- | --- | --- | --- | --- | --- |
| ADDI | ALUexe | `out` | `wb.in_x(0)` | 单个 32 位 | 无 |
| vadd | vALUv2 | `out` | `wb.in_v(0)` | 32 个 32 位 | wvd_mask 透传 |

4. **分支角色**：把标量侧换成 `BEQ x1, x2, label`，说明 `ALUexe` 此刻切换到「分支解析器」角色——`alu_fn=FN_SEQ`、`cmp_out` 成为 `jump`、`new_pc` 来自 `in3`，结果走 `out2br` 而非 `out`。再把向量侧换成 `beqv`，说明它走 `out2simt_stack`、把各 lane `cmp_out` 汇成 `if_mask` 送给 SIMT stack。
5. **参数伸缩**：若把 [parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) 中的 `num_lane`（第 57 行）改小于 `num_thread`（如注释里的 `2`），重新 `make verilog`，观察 `vALUv2` 是否改走分批分支、`maxIter` 变为多少——**待本地验证**。

通过这个综合任务，你会直观看到：「标量 ALU」和「向量 ALU」并非两套独立电路，而是**同一个 `ScalarALU` 内核在「1 份」与「32 份」两种例化规模上的复用**，再加上各自适配的分支/掩码出口。

## 6. 本讲小结

- `ScalarALU` 是纯组合的单 lane 整数运算核，用一个加减法器复用出 ADD/SUB/比较/逻辑/移位/MIN/MAX，输出 `out` 与 `cmp_out`；`in3` 在其内部不参与算术运算，是给上层（如分支目标地址）的预留槽。
- `ALUexe` 例化 1 个 `ScalarALU` 服务标量指令，并兼任分支解析器：依据 `ctrl.branch`（`B_N/B_B/B_J/B_R`）把 `cmp_out`/`true` 装配成 `BranchCtrl`（`jump`+`new_pc`）经 `out2br` 送往 `branch_back`，普通结果经 `out` 送 `wb.in_x(0)`。
- `vALUv2` 例化 `hardThread`（默认 `num_lane=32`）个 `ScalarALU`，一 lane 一份，让一条向量整数指令同拍并行算完；结果带 `wvd_mask` 透传到写回（`wb.in_v(0)`），运算本身不屏蔽 lane。
- `vALUv2` 还服务 SIMT 分支：把各 lane 的 `cmp_out` 汇成 `if_mask` 经 `out2simt_stack` 送给 SIMT stack。
- 当 `num_lane < num_thread` 时，`vALUv2` 走分批分支，靠 `sendCS/recvCS` 计数器状态机把一条指令拆成多拍（chime）；默认配置下该分支不启用。
- 标量 ALU 与向量 ALU 的本质区别是**同一个内核的例化份数**——这正是 Ventus「用 RVV 向量指令充当 SIMT」在执行单元层面的直接体现。

## 7. 下一步学习建议

- 下一讲 **u5-l3（浮点、乘法与 SFU 特殊运算单元）** 会继续剖析 `execution.scala` 里的 `FPUexe`/`vMULv2`/`SFUexe`，你会看到它们同样采用「按 lane 复制」思路，只是内核换成浮点 FPU、乘法器、除法器，并正式引入「多周期」与「单元数少于 lane」的 chime 现象。
- 若想看 `alu_fn` 的取值从何而来，回看 **u4-l2（译码与指令定义）** 的 `IDecode` 译码表与 `CtrlSigs`。
- 若想理解 `if_mask` 送给 SIMT stack 之后如何引发分支分歧与汇合，预习 **u5-l5（SIMT stack 与分支汇合）**。
- 建议顺带浏览 `execution.scala` 中 `vALUexe`（旧版）与 `vALUv2`（v2）的对照，体会「v2 增加分批（chime）能力」的演进。
