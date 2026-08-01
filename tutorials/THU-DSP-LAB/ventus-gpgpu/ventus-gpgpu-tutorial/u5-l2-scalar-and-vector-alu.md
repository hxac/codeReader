# 标量 ALU 与向量 ALU

## 1. 本讲目标

本讲聚焦 Ventus SM 后端里最基础、也最常用的一类执行单元——整数算术逻辑单元（ALU）。读完本讲你应当能够：

- 说清 `ScalarALU` 这个「单 lane 整数运算核」支持哪些运算，以及如何用一个 5 位 `func` 配合加减法器复用出 ADD/SUB/SLT/AND/OR/XOR/移位/MIN/MAX。
- 说清标量执行单元 `ALUexe` 如何把 `ScalarALU` 包起来、并在同一通路里兼任**分支解析器**（输出 `BranchCtrl` 的 `jump`/`new_pc`）。
- 说清向量执行单元 `vALUv2` 如何用「按 lane 复制 `ScalarALU`」的方式让 32 个线程同一拍并行算完一条向量指令，结果如何带上 `mask` 送往写回，以及 SIMT 分支如何借用各 lane 的比较结果。

本讲是 u5-l1 的细化：u5-l1 给出了「8 类执行单元 + 双发射」的全景图，本讲把其中 `ALUexe`（标量 ALU）与 `vALUv2`（向量 ALU）这两个单元彻底拆透。它们共用同一个 `ScalarALU` 内核——这正是 Ventus「向量化 = 把标量运算铺满 lane」设计哲学的最佳样本。

## 2. 前置知识

阅读本讲前，请确保你已经理解：

- **warp / thread / lane**：一个 warp 由 `num_thread`（默认 32）个线程组成；向量寄存器的一条是「整条向量」，按 lane 切分，每个 lane 对应一个线程的标量值（见 u2-l1）。
- **CtrlSigs 控制信号**：译码器把 32 位指令翻成的控制包，关键字段有 `alu_fn`（6 位运算码）、`wxd`/`wvd`（写标量/向量寄存器）、`branch`（分支类型）、`isvec`、`reverse`、`simt_stack`、`mask` 等（见 u4-l2）。
- **双发射**：`issueX` 发标量指令、`issueV` 发向量指令；标量指令走 `out_sALU`，向量指令走 `out_vALU`（见 u5-l1）。
- **DecoupledIO / Queue(pipe=true)**：本讲大量出现「输入握手 + 一级流水队列 + 输出握手」的包装手法。

一个核心直觉先放在这里：**Ventus 的向量整数运算并不发明新的运算电路，而是把同一个标量 `ScalarALU` 例化 32 份**，每份喂一个 lane 的操作数，同拍算完。理解了这一点，本讲就理解了一大半。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ventus/src/pipeline/ALU.scala` | 定义 `ALUOps` 运算码常量与 `ScalarALU` 内核——单 lane 的加减/逻辑/移位/比较/MIN/MAX 运算电路。 |
| `ventus/src/pipeline/execution.scala` | 定义标量执行单元 `ALUexe`（包装 `ScalarALU` + 分支输出）、向量执行单元 `vALUv2`（按 lane 复制 `ScalarALU`）、以及 `vMULv2`/`FPUexe`/`SFUexe`/`vTCexe` 等其他单元。 |
| `ventus/src/pipeline/pipe.scala` | SM 流水线总装：例化 `alu`/`valu` 并把它们连到 `issueX`/`issueV`、`branch_back`、`simt_stack`、写回模块 `wb`。 |
| `ventus/src/pipeline/issue.scala` | 定义 `vExeData`/`sExeData` 执行数据包（`in1/in2/in3/mask/ctrl`）。 |
| `ventus/src/pipeline/operandCollector.scala` | 定义写回控制包 `WriteScalarCtrl`/`WriteVecCtrl`。 |
| `ventus/src/top/parameters.scala` | 关键参数：`num_thread=32`、`num_lane=num_thread`、`xLen=32`、`num_sfu`。 |

## 4. 核心概念与源码讲解

### 4.1 ScalarALU：单 lane 整数运算内核

#### 4.1.1 概念说明

`ScalarALU` 是一个**纯组合**的整数运算电路：给它两个（外加一个保留的第三）32 位操作数和一个 5 位功能码 `func`，它同一拍给出 32 位结果 `out` 和一位比较结果 `cmp_out`。它本身不带状态、不做握手，是最底层的「算术砖块」。

之所以先讲它，是因为它被两处复用：

- `ALUexe` 例化 **1 个** `ScalarALU`，服务标量指令；
- `vALUv2` 例化 **`num_lane` 个** `ScalarALU`，服务向量指令的每个 lane。

所以搞懂 `ScalarALU` 的运算表，标量和向量的运算就都搞懂了。

#### 4.1.2 核心流程

`ScalarALU` 的设计哲学是「**一个加减法器干所有事**」：

1. **加减法**：减法变成「取反加一」。令
   - \( \text{in2\_inv} = \text{isSub} ? \sim\text{in2} : \text{in2} \)
   - \( \text{adder\_out} = \text{in1} + \text{in2\_inv} + \text{isSub} \)

   这样 ADD 与 SUB 共用同一个加法器：ADD 时 `isSub=0` 得 `in1+in2`；SUB 时 `isSub=1` 得 `in1 + (~in2) + 1 = in1 - in2`。

2. **比较**：有符号/无符号小于（SLT/SLTU）复用减法结果的最高位与操作数符号位；相等（SEQ）用 `in1^in2==0` 判定。最终比较结果 `cmp_out` 由 `cmpInverted`（是否取反）与 `cmpEq`（是否相等比较）两个 `func` 位微调，从而用一套电路覆盖 SEQ/SNE/SLT/SLTU/SGE/SGEU 六种比较。

3. **逻辑**：AND/OR/XOR 用三路 `Mux` 选出。

4. **移位**：把左移实现成「反转 → 右移 → 再反转」，于是只造一个右移器。算术右移 SRA 靠把符号位拼进移位输入实现。

5. **MIN/MAX**：用比较器选大小，区分有符号（`asSInt`）/无符号两套。

6. **A1ZERO/A2ZERO**：特殊功能码，直接透传 `in2` 或 `in1`（如伪指令 MV、忽略某操作数的场合）。

最后用一个多路选择把「加法器 / 比较 / 逻辑 / 移位 / MIN-MAX / 透传」按 `func` 选出唯一的 `out`。

#### 4.1.3 源码精读

运算码常量集中在 `ALUOps` 对象里，注意它们是 5 位（`SZ_ALU_FUNC=5`）：

[ventus/src/pipeline/ALU.scala:17-57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L17-L57) — 定义 `FN_ADD/FN_SUB/FN_SL/FN_SR/FN_SRA/FN_AND/FN_OR/FN_XOR/FN_SLT/.../FN_MIN/FN_MAX/FN_A1ZERO/FN_A2ZERO` 等运算码，以及一组用位域判断类别的辅助函数。

几个关键的位域判定函数：

```scala
def isSub(cmd: UInt) = (cmd >= FN_SUB) & (cmd <= FN_SGEU)   // 10~15 都视作减法族
def isCmp(cmd: UInt) = (cmd >= FN_SLT) & (cmd <= FN_SGEU)   // 12~15 是比较族
def cmpUnsigned(cmd) = cmd(1)
def cmpInverted(cmd) = cmd(0)
def cmpEq(cmd) = !cmd(3)
def isMIN(cmd) = (cmd(4,2) === "b100".U)   // 16~19 是 MIN/MAX 族
```

这意味着只要 `func` 落在 `FN_SUB..FN_SGEU`（10~15），`isSub` 即为真，加减法器自动切换到减法模式——这是 ADD/SUB/SLT/SLTU 共用加法器的关键。

`ScalarALU` 的端口：

[ventus/src/pipeline/ALU.scala:61-70](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L61-L70) — `in1/in2/in3` 各 32 位输入、`func` 5 位功能码、`out` 32 位结果、`cmp_out` 1 位比较结果。

> **注意一个细节**：`in3` 在 `ScalarALU` 内部**并不参与任何算术运算**（通读函数体可见它从未被引用）。它在端口里保留，主要是给上层复用——在标量分支路径里 `in3` 被用来承载**分支目标地址**（见 4.2.3），在浮点单元里它才是第三操作数（fmadd 等）。换句话说，`in3` 是个「预留槽」，对纯整数 ALU 运算是无用的。

加减法器与比较的核心几行：

[ventus/src/pipeline/ALU.scala:72-81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L72-L81) — `in2_inv` 配合 `isSub` 把减法归一为加法；`adder_out` 同时供 ADD/SUB/比较使用；`slt` 借减法结果最高位判断小于；`cmp_out` 最终由 `cmpInverted ^ (相等或小于)` 得到。

最终结果选择：

[ventus/src/pipeline/ALU.scala:111-113](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L111-L113) — 优先级为 `A1ZERO`（透传 in2）→ `A2ZERO`（透传 in1）→ `isMIN`（MIN/MAX 结果）→ 其余（加法/比较/逻辑/移位）。

#### 4.1.4 代码实践

**实践目标**：手工执行一次 `ScalarALU` 运算，验证「减法即取反加一」与「比较复用减法」。

**操作步骤**：

1. 打开 [ALU.scala:72-81](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L72-L81)。
2. 取 `func=FN_SUB`、`in1=5`、`in2=3`：因 `isSub(FN_SUB)` 为真，`in2_inv = ~3`，`adder_out = 5 + (~3) + 1 = 2`，最终 `out = adder_out = 2`。
3. 取 `func=FN_SLT`（有符号小于）、`in1=5`、`in2=-3`：`isSub` 为真，`adder_out = 5 + (~(-3)) + 1 = 5-(-3)=8`，最高位为 0；因两数符号位不同且非无符号比较，`slt` 取 `in1` 的符号位 0 → `cmp_out = cmpInverted ^ slt = 0`，即「5 小于 -3」为假。

**需要观察的现象**：加减法、比较是否都由 `adder_out` 一根线推导出来，没有出现第二个加法器或独立比较器。

**预期结果**：上述两例分别得到 `out=2` 与 `cmp_out=0(false)`，与 RISC-V 语义一致。

#### 4.1.5 小练习与答案

**练习 1**：`func=FN_ADD`、`in1=7`、`in2=6` 时 `adder_out` 与 `out` 各是多少？
**答案**：`isSub(FN_ADD)` 为假，`in2_inv=6`，`adder_out=7+6+0=13`，`out=adder_out=13`。

**练习 2**：为什么 `ScalarALU` 里看不到独立的「比较器」，却仍能给出 SLT 结果？
**答案**：因为减法 `in1-in2` 的结果最高位、连同两个操作数的符号位，足以判定有符号/无符号的大小关系（`slt` 的逻辑），所以比较「免费」复用了加减法器。

**练习 3**：`in3` 在 `ScalarALU` 内部参与运算吗？它在本讲后续会被用来做什么？
**答案**：不参与算术运算。在标量分支路径里，`in3` 被用作分支目标地址 `new_pc` 的来源（见 4.2.3）。

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
- `out`：送往写回 `wb.in_x(0)` 的标量结果（`WriteScalarCtrl`）。
- `out2br`：送往 `branch_back.in0` 的分支结果（`BranchCtrl`）。

核心是依据 `ctrl.branch`（2 位分支类型）分流：

| `ctrl.branch` | 含义 | `out`（写回） | `out2br`（分支） |
| --- | --- | --- | --- |
| `B_N`(0) | 非分支 | 若 `wxd` 则有效 | 无效 |
| `B_B`(1) | 条件分支 | 无效 | `jump = cmp_out` |
| `B_J`(2) / `B_R`(3) | 无条件跳转/JALR | 若 `wxd` 则有效（写返回地址） | `jump = true` |

`new_pc` 一律取自 `in3`（译码阶段已把 PC+imm 或 rs1 算好放进 `in3`）。`jump` 对条件分支取 `cmp_out`，对无条件跳转恒为真。

输入 `ready` 同样按分支类型决定：条件分支只看分支队列是否可收，非分支只看写回队列是否可收，跳转两者都要可收。

#### 4.2.3 源码精读

`BranchCtrl` 就是分支信息的载体：

[ventus/src/pipeline/execution.scala:20-31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L20-L31) — `BranchCtrl` 含 `wid`（哪个 warp）、`jump`（是否跳转）、`new_pc`（目标地址）；`ALUexe` 的三个端口 `in`/`out`/`out2br`。

例化 `ScalarALU` 并喂入操作数与低 5 位功能码：

[ventus/src/pipeline/execution.scala:32-45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L32-L45)

> 注意：[execution.scala:36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L36) 传给 `ScalarALU` 的是 `alu_fn(4,0)`——`CtrlSigs.alu_fn` 是 6 位，而 `ScalarALU.func` 只取低 5 位。高位用于在 `ALUOps` 里区分 MUL/MAC 族（见 `isMUL/isMAC` 的位域）或被上层（如乘法单元）另作他用，对纯 ALU 运算取低 5 位即可。

分支信息装配（本小节的核心）：

[ventus/src/pipeline/execution.scala:55-64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L55-L64) — `new_pc := in3`；`jump` 对 `B_B` 取 `alu.io.cmp_out`、对 `B_J/B_R` 取 `true`；两个 `valid` 按 `branch` 类型分流，`B_B` 只发分支、`B_N` 只发写回、`B_J/B_R`（默认分支）两者都发。

也就是说，JAL/JALR 这类「写返回地址 + 跳转」的链接指令，会同时驱动 `out`（写 `rd`=返回地址）和 `out2br`（跳转），二者互以对方的 `ready` 为前提，保证原子地同发。

`pipe.scala` 里的连接实证：

[ventus/src/pipeline/pipe.scala:378-392](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L378-L392) — `issueX.io.out_sALU <> alu.io.in`（标量指令进入）、`alu.io.out2br <> branch_back.io.in0`（分支结果汇总）。

[ventus/src/pipeline/pipe.scala:416](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L416) — `wb.io.in_x(0) <> alu.io.out`，标量 ALU 占用写回模块的 0 号标量输入口。

#### 4.2.4 代码实践

**实践目标**：追踪一条标量 `ADDI x5, x1, 10` 在 `ALUexe` 中的完整通路。

**操作步骤**：

1. 在 [pipe.scala:378](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L378) 确认 `ADDI` 经 `issueX.out_sALU` 进入 `alu.io.in`。
2. 在 [execution.scala:32-45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L32-L45) 确认 `in1=x1`、`in2=10`（立即数）、`func=alu_fn(4,0)=FN_ADD`。
3. 因 `ctrl.branch=B_N`，查 [execution.scala:59-64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L59-L64)：`result.io.enq.valid = in.valid && wxd && ...`（写回有效），`result_br` 无效。
4. 结果 `wb_wxd_rd = x1+10`、`reg_idxw = 5`、`wxd = true`，经 `alu.io.out` 进入 [pipe.scala:416](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L416) 的 `wb.in_x(0)`。

**需要观察的现象**：非分支指令只走 `out`、`out2br` 全程静默；写回包里同时携带了数据、目标寄存器号、warp id。

**预期结果**：`x5 ← x1 + 10` 经 0 号标量写回口送达寄存器堆。若想看波形，可在 `ALUexe` 内临时去掉注释的 `printf`（[ALU.scala:115](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ALU.scala#L115)）后重新 `make verilog` 并仿真——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一条 `JAL x1, offset`（`branch=B_J`、`wxd=true`）经过 `ALUexe`，`out` 与 `out2br` 的 `valid` 分别由什么决定？
**答案**：`B_J` 落入默认分支，`result_br.valid = in.valid && result.io.enq.ready`，`result.valid = in.valid && wxd && result_br.io.enq.ready`。两者互以对方 ready 为前提，从而原子地「写返回地址 + 跳转」。

**练习 2**：条件分支 `BEQ` 的 `jump` 信号从哪里来？
**答案**：译码把 BEQ 的 `alu_fn` 设为 `FN_SEQ`，`ScalarALU` 据此算出 `cmp_out`（两数相等），`ALUexe` 在 `B_B` 分支里把 `jump := alu.io.cmp_out`（[execution.scala:57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L57)）。

---

### 4.3 vALUv2：向量 ALU（按 lane 复制）

#### 4.3.1 概念说明

`vALUv2` 是向量指令的整数执行单元。它的核心思想在本讲开头就点明了：**把 `ScalarALU` 例化 `num_lane` 份**，每份服务一个 lane，于是「一条向量加法」就变成「32 个标量加法同拍并行」。

`vALUv2` 与 `vALUexe`（同文件里的旧版）的关系：`pipe.scala` 实际例化的是 `vALUv2`（见 4.3.3）。`v2` 多了一个能力——当硬件 lane 数 `hardThread`（= `num_lane`）少于软件线程数 `softThread`（= `num_thread`）时，能把一条向量指令拆成多拍「分批发送、分批回收」地算完。默认 `num_lane = num_thread = 32`，二者相等，走的是最简单的单拍分支。

`vALUv2` 还承担一个 SIMT 专属职责：**SIMT 分支指令**（如 `beqv`，`ctrl.simt_stack=true`）经它执行，把「每个 lane 是否满足分支条件」汇成一个 `if_mask` 掩码送给 SIMT stack，用于分支分歧处理（详见 u5-l5）。

#### 4.3.2 核心流程

默认配置（`softThread == hardThread == 32`）下的流程：

1. 输入 `vExeData` 含整条向量的 `in1/in2/in3`（各 32 个 32 位 lane）与 `mask`（32 个 lane 的活动位）。
2. 对每个 lane `x`：把 `in1(x)/in2(x)/in3(x)` 与 `func=alu_fn(4,0)` 喂给第 `x` 个 `ScalarALU`，把它的 `out` 写进结果包 `wb_wvd_rd(x)`。
3. 结果包带上 `wvd_mask := io.in.bits.mask`——**注意运算本身对所有 lane 都执行，mask 只是随结果一起送到写回，由写回阶段决定哪些 lane 真正提交**。
4. 普通向量指令（`simt_stack=false`）从 `out` 送往写回 `wb.in_v(0)`；SIMT 分支指令（`simt_stack=true`）从 `out2simt_stack` 送出 `if_mask`。

几个特殊向量运算的处理：

- `reverse`：交换 `in1`/`in2`（某些指令需要反序操作数）。
- **向量比较**（`FN_SLT/SEQ/...`）：lane 结果取 `cmp_out` 而非 `out`。
- **掩码归约运算**（`FN_VMANDNOT/VMORNOT/VMNAND/VMNOR/VMXNOR`）：复用标量 AND/OR/XOR，对输入取反或对输出取反来实现。
- `FN_VID`：lane 结果直接取 lane 编号 `x`（vid.v 指令）。
- `FN_VMERGE`：按 `mask(x)` 在 `in1`/`in2` 间选择（vmerge 指令）。

当 `softThread != hardThread` 时（分批模式）：用 `maxIter = softThread/hardThread` 次迭代，每批发 `hardThread` 个 lane、回收 `hardThread` 个结果，靠 `sendCS/recvCS` 两个计数器状态机驱动，直到 32 个 lane 全部算完再统一送出。这正是 u5-l1 提到的「chime（分组串行）」机制——默认配置下该分支是死代码，但为面积换吞吐留好了扩展点。

#### 4.3.3 源码精读

`vALUv2` 的例化与参数：

[ventus/src/pipeline/pipe.scala:76-77](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L76-L77) — `val valu = Module(new vALUv2(num_thread, num_lane))`。默认 `num_lane = num_thread = 32`（[parameters.scala:57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/parameters.scala#L57)），故 `softThread == hardThread`。

按 lane 复制 `ScalarALU`——本讲最关键的一行：

[ventus/src/pipeline/execution.scala:502](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L502) — `val alu = VecInit(Seq.fill(hardThread)((Module(new ScalarALU())).io))`，即例化 `hardThread`（默认 32）个 `ScalarALU`，一个 lane 一个。

逐 lane 喂数据、收结果（默认分支）：

[ventus/src/pipeline/execution.scala:507-517](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L507-L517) — 每个 lane `x`：`alu(x).in1 := in1(x)`、`in2 := in2(x)`、`in3 := in3(x)`、`func := alu_fn(4,0)`，并把 `alu(x).out` 写入 `wb_wvd_rd(x)`；`reverse` 时交换 `in1/in2`。

向量比较结果改取 `cmp_out`：

[ventus/src/pipeline/execution.scala:530-535](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L530-L535) — 当 `alu_fn` 为 `SEQ/SNE/SGE/SGEU/SLT/SLTU` 时，lane 结果取 `alu(x).cmp_out`（1 位布尔比较结果）。

结果包携带 mask 并送出（普通向量写回与 SIMT 分支二选一）：

[ventus/src/pipeline/execution.scala:556-564](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L556-L564) — `wvd_mask := io.in.bits.mask`（mask 随结果流向写回）；普通指令 `result.valid := in.valid && wvd && !simt_stack`；SIMT 分支指令把各 lane `cmp_out` 拼成 `if_mask`（取反以契合 SIMT stack 约定）经 `result2simt` 送出。

`io.in.ready` 的二选一：

[ventus/src/pipeline/execution.scala:570](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L570) — `Mux(simt_stack, result2simt.io.enq.ready, result.io.enq.ready)`：SIMT 分支等 SIMT 队列可收，普通向量等写回队列可收。

`pipe.scala` 里的连接实证：

[ventus/src/pipeline/pipe.scala:374](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L374) — `issueV.io.out_vALU <> valu.io.in`（向量指令进入）。

[ventus/src/pipeline/pipe.scala:387](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L387) — `simt_stack.io.if_mask <> valu.io.out2simt_stack`（SIMT 分支掩码送往 SIMT stack）。

[ventus/src/pipeline/pipe.scala:422](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L422) — `wb.io.in_v(0) <> valu.io.out`，向量 ALU 占用写回模块的 0 号向量输入口。

> **分批模式（可选阅读）**：当 `num_lane < num_thread` 时走 [execution.scala:573-737](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L573-L737) 的 `else` 分支，由 `sendCS`/`recvCS` 计数器把一条向量指令拆成 `maxIter` 拍处理。本讲只需了解其存在与意图，不必逐行精读。

#### 4.3.4 代码实践

**实践目标**：追踪一条向量加法 `vadd.vv v3, v1, v2` 在 `vALUv2` 中的数据通路，体会「lane 复制 + 同拍并行 + mask 透传」。

**操作步骤**：

1. 在 [pipe.scala:374](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L374) 确认 `vadd` 经 `issueV.out_vALU` 进入 `valu.io.in`，`in1=v1`、`in2=v2`、`alu_fn=FN_ADD`。
2. 在 [execution.scala:502](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L502) 确认 32 个 `ScalarALU` 同时存在；在 [507-517 行](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L507-L517) 确认每个 lane `x` 都在做 `v1[x] + v2[x]`，结果写入 `wb_wvd_rd(x)`。
3. 在 [556-564 行](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L556-L564) 确认 `wvd_mask := mask`、`simt_stack=false`，于是结果从 `out` 送往 [pipe.scala:422](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L422) 的 `wb.in_v(0)`。
4. 填写下面这张 lane 追踪表（假设 `v1=1,2,...,32`、`v2=10` 全相同、mask 全 1）：

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
**答案**：从 `out2simt_stack` 送出（`out` 此时无效）。`if_mask = ~(VecInit(alu.map(_.cmp_out)).asUInt)`，即把每个 lane 的比较结果按位取反拼接（[562-564 行](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/execution.scala#L562-L564)），供 SIMT stack 判定分支分歧。

**练习 3**：向量运算时 mask 在 `vALUv2` 内部是否屏蔽了不活动 lane 的计算？真正的屏蔽发生在哪里？
**答案**：没有。`vALUv2` 对所有 lane 都执行运算，`mask` 只是随结果包 `wvd_mask` 透传；真正的「不活动 lane 不提交」由下游写回阶段依据 `wvd_mask` 落实。

## 5. 综合实践

把标量与向量两条通路串起来对照。设想一个 warp 同时执行两条指令（双发射）：

- 标量侧：`ADDI x5, x1, 10`
- 向量侧：`vadd.vv v3, v1, v2`（mask 全 1）

请完成以下任务：

1. **入口分流**：在 [pipe.scala:374](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L374) 与 [pipe.scala:378](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L378) 分别确认两条指令由哪个 Issue 端口送入哪个执行单元。
2. **内核复用**：指出两条指令最终都落在同一个 `ScalarALU` 运算核上——标量侧用了 1 份，向量侧用了 32 份，且 `func` 都是 `FN_ADD`。
3. **结果汇总**：填表对比二者输出端口与写回口：

| 指令 | 执行单元 | 结果端口 | 写回口 | 结果形态 | mask |
| --- | --- | --- | --- | --- | --- |
| ADDI | ALUexe | `out` | `wb.in_x(0)` | 单个 32 位 | 无 |
| vadd | vALUv2 | `out` | `wb.in_v(0)` | 32 个 32 位 | wvd_mask 透传 |

4. **分支角色**：把标量侧换成 `BEQ x1, x2, label`，说明 `ALUexe` 此刻切换到「分支解析器」角色——`alu_fn=FN_SEQ`、`cmp_out` 成为 `jump`、`new_pc` 来自 `in3`，结果走 `out2br` 而非 `out`。再把向量侧换成 `beqv`，说明它走 `out2simt_stack`、把各 lane `cmp_out` 汇成 `if_mask` 送给 SIMT stack。
5. **参数伸缩**：若把 [parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) 中的 `num_lane` 改小于 `num_thread`（例如把第 57 行改回注释里的 `2`），重新 `make verilog`，观察 `vALUv2` 是否会改走分批分支、`maxIter` 变为多少——**待本地验证**。

通过这个综合任务，你会直观看到：「标量 ALU」和「向量 ALU」并非两套独立电路，而是**同一个 `ScalarALU` 内核在「1 份」与「32 份」两种例化规模上的复用**，再加上各自适配的分支/掩码出口。

## 6. 本讲小结

- `ScalarALU` 是纯组合的单 lane 整数运算核，用一个加减法器复用出 ADD/SUB/比较/逻辑/移位/MIN/MAX，输出 `out` 与 `cmp_out`；`in3` 在其内部不参与算术运算，是预留槽。
- `ALUexe` 例化 1 个 `ScalarALU` 服务标量指令，并兼任分支解析器：依据 `ctrl.branch` 把 `cmp_out`/`true` 装配成 `BranchCtrl`（`jump`+`new_pc`）经 `out2br` 送往 `branch_back`，普通结果经 `out` 送 `wb.in_x(0)`。
- `vALUv2` 例化 `num_lane`（默认 32）个 `ScalarALU`，一 lane 一份，让一条向量整数指令同拍并行算完；结果带 `wvd_mask` 透传到写回（`wb.in_v(0)`），运算本身不屏蔽 lane。
- `vALUv2` 还服务 SIMT 分支：把各 lane 的 `cmp_out` 汇成 `if_mask` 经 `out2simt_stack` 送给 SIMT stack。
- 当 `num_lane < num_thread` 时，`vALUv2` 走分批分支，靠计数器状态机把一条指令拆成多拍（chime）；默认配置下该分支不启用。
- 标量 ALU 与向量 ALU 的本质区别是**同一个内核的例化份数**——这正是 Ventus「用 RVV 向量指令充当 SIMT」在执行单元层面的直接体现。

## 7. 下一步学习建议

- 下一讲 **u5-l3（浮点、乘法与 SFU 特殊运算单元）** 会继续剖析 `execution.scala` 里的 `FPUexe`/`vMULv2`/`SFUexe`，你会看到它们同样采用「按 lane 复制」思路，只是内核换成浮点 FPU、乘法器、除法器，并引入「多周期」与「单元数少于 lane」的 chime 现象。
- 若想看 `alu_fn` 的取值从何而来，回看 **u4-l2（译码与指令定义）** 的 `IDecode` 译码表与 `CtrlSigs`。
- 若想理解 `if_mask` 送给 SIMT stack 之后如何引发分支分歧与汇合，预习 **u5-l5（SIMT stack 与分支汇合）**。
- 建议顺带浏览 `execution.scala` 中 `vMULexe`/`vALUexe`（旧版）与 `vMULv2`/`vALUv2`（v2）的对照，体会「v2 增加分批能力」的演进。
