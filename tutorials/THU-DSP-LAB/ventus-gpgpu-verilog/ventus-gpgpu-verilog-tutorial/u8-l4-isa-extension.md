# 指令集扩展与二次开发

## 1. 本讲目标

本讲是单元 8（工程实践）的收官篇，也是整本手册的“融会贯通”篇。前面 27 篇讲义把 Ventus GPGPU 从顶层系统一路拆到 lane 级运算核，本讲反过来，把这条数据通路**重新串起来**——通过“新增一条自定义指令”把所学全部用上。

学完后你应当能够：

1. 说出**新增一条向量指令**至少需要改动哪些源码文件、为什么是它们、它们的先后依赖关系。
2. 独立完成「位模式定义 → 译码表 → 执行单元功能实现 → 操作数采集 → 写回 → 测试」这条**全栈贯通**链路。
3. 理解 `decodeUnit` 的 42 位控制字（`ctrlSignals`）每一位域的含义，能够照葫芦画瓢地写出一条新指令的译码项。
4. 设计一个最小测试用例，在仿真框架下验证新指令的正确性。

> 本讲以一个**真实存在但尚未接通**的指令 `VSADD_VV`（有符号饱和向量加）为贯穿示例。它的 32 位编码宏在 [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1062) 中已经定义，但译码表里并没有对应条目——这正是练习“把一条指令接进流水线”的绝佳素材。

## 2. 前置知识

本讲为专家层（advanced），默认你已读完毕 u3-l3（译码）、u4-l1（操作数采集）、u4-l2（向量 ALU）。这里只做最简短的回顾：

- **指令是一条 32 位串**：CPU 取回的 `inst` 是一个 32 bit 向量。译码器要做的，就是看这串里的某些位（opcode、funct3、funct6 等）长什么样，决定它“是什么指令、源操作数从哪来、要不要写回、交给哪个执行单元”。
- **Ventus 用 RV32V 向量语义**：一条向量指令广播给整个 warp，由 `NUM_THREAD` 个 lane 并行执行（SIMT）。
- **控制信号是一张查表**：`decodeUnit` 用 `casex` 把 32 位指令查表成一个 42 位的打包控制字，再切片成 `isvec / fp / alu_fn / sel_alu1 / wvd ...` 等具名信号去驱动后级。
- **`FN_*` 功能码**：决定运算单元“做什么运算”（加、减、与、或……），是 6 位宽的命名空间，被各执行单元复用。

如果你对其中某项不熟，建议先回看对应讲义再继续。

## 3. 本讲源码地图

本讲会动手改动的 5 个关键文件，及其在指令贯通链中的角色：

| 文件 | 角色 | 本讲要改什么 |
|------|------|--------------|
| `src/define/define.v` | 配置与编码总开关 | 新增功能码 `FN_VSADD`（位模式已存在） |
| `src/gpgpu_top/sm/pipeline/decodeUnit.v` | 译码器 | 在 `casex` 表新增 `VSADD_VV` 的译码项 |
| `src/gpgpu_top/sm/pipeline/valu/alu.v` | 单 lane 运算核 | 实现“饱和加”这个新功能 |
| `src/gpgpu_top/sm/pipeline/operand_collector/gen_imm.v` | 立即数生成 | 仅 `_VI` 形式需要；`_VV` 形式可跳过 |
| `src/gpgpu_top/sm/pipeline/pipe.v` | 流水线连线集装箱 | 只读：确认 vALU 写回通路已就绪 |

此外，`testcase/test_gpgpu_axi_top/tc_vecadd/` 是用来跑测试的载体。

> 全程不会改动 `pipe.v` / `issue.v` / `valu.v`：因为 `VSADD_VV` 走的是与 `VADD_VV` **完全相同**的数据通路（向量 ALU），只要译码正确、运算核会算，它就能自动复用现成的取数—发射—执行—写回链路。这正是 Ventus 流水线设计带来的红利。

## 4. 核心概念与源码讲解

### 4.1 全局视角：一条新指令要经过哪几道关卡

#### 4.1.1 概念说明

把 Ventus SM 流水线想象成一条流水作业的车间，一条指令从进厂到出厂要盖 6 个章：

```
取指 → 译码 → 缓冲 → 发射 → 操作数采集 → 执行 → 写回
 icache   decodeUnit   ibuffer   issue   operand_collector  vALU   writeback
```

新增一条指令，本质上就是回答 6 个问题：

1. **它长什么样？** → 在 `define.v` 写 32 位位模式。
2. **它是什么？** → 在 `decodeUnit` 的译码表里登记，告诉硬件“看到这串位就认它”。
3. **它要做什么运算？** → 在执行单元（这里是 `alu.v`）里实现算法。
4. **它的操作数从哪来？** → 由译码出的 `sel_alu1/2` 决定，`operand_collector` 自动照办；若用到立即数还要 `gen_imm` 配合。
5. **结果写到哪？** → 由 `wvd`（写向量寄存器）/`wxd`（写标量寄存器）决定。
6. **它对吗？** → 写测试用例，仿真比对。

其中 1、2、3 是**必改**的；4、5 通常靠译码位的取值自动满足，零代码；6 是验证。

#### 4.1.2 核心流程

新增指令的改动决策树（伪代码）：

```
function 新增指令(指令编码, 运算类型):
    在 define.v 定义位模式宏          # 已有则跳过
    选定一个空闲的 FN_* 功能码        # 6 位空间，需避开已用值
    在 decodeUnit casex 表加一行      # 仿照同类指令（如 VADD_VV）
    if 指令需要新运算:
        在对应执行单元(alu/fpu/sfu)实现
    if 指令含立即数(_VI/_I):
        确认 gen_imm 有对应 IMM 类型
    重新编译仿真，跑测试用例
```

#### 4.1.3 源码精读：先看“别人怎么写”——`VADD_VV`

最稳的学习方法是照抄一条已存在的同类指令。先看 `VADD_VV`（普通向量加）是怎么在三个文件里串起来的。

**① 位模式与功能码**（`define.v`）：

普通加的功能码是 `FN_ADD = 6'd0`（[define.v:471](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L471)），`VADD_VV` 的位模式在 [define.v:717](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L717)：

```
`define VADD_VV            32'b000000???????????000?????1010111
```

解读这 32 位（从高位 [31] 到低位 [0]）：低 7 位 `1010111`(0x57) 是 RISC-V 的 `OP-V` 主操作码；`[14:12]=000` 是 funct3（`.vv` 形式）；`[31:26]=000000` 是 funct6，区分“是哪种向量运算”。中间的 `?` 是“任意匹配”，分别对应 rs1/rs2/rd 寄存器号与 vm 掩码位。

**② 译码项**（`decodeUnit.v`）：

[decodeUnit.v:396](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L396) 把 `VADD_VV` 译成一个 42 位控制字：

```
`VADD_VV: ctrlSignals_0 = {`Y,`N,`N,`B_N,`N,`N,`CSR_N,`Y,`A3_X,`A2_VRS2,`A1_VRS1,`IMM_X,`MEM_X,`FN_ADD,`N,`M_X,`N,`N,`N,`Y,`N,`N,`N,`N,`N,`N,`N};
```

这 27 个字段从 MSB 到 LSB 一一对应 [decodeUnit.v:602-644](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L602-L644) 切片出的控制信号。下表列出了关键位域：

| 位域 | 字段 | VADD_VV 取值 | 含义 |
|------|------|------------|------|
| [41] | `isvec` | `Y` | 是向量指令（决定路由到 vALU） |
| [32] | `reverse` | `Y` | 操作数换序开关（对称运算无影响） |
| [29:28] | `sel_alu2` | `A2_VRS2` | 第二源 = 向量寄存器 rs2 |
| [27:26] | `sel_alu1` | `A1_VRS1` | 第一源 = 向量寄存器 rs1 |
| [25:22] | `sel_imm` | `IMM_X` | 无立即数 |
| [19:14] | `alu_fn` | `FN_ADD` | 运算 = 加 |
| [7] | `wvd` | `Y` | 写向量目的寄存器 |

其余位（`fp/barrier/branch/simt_stack/csr/mul/mem/sfu/tc/...`）全为 `N`，表示这条指令不碰浮点、不是分支、不访存……而这些“全 N”恰恰是让 `issue` 把它路由到 vALU 的关键（见 4.4）。

**③ 运算核**（`alu.v`）：

[alu.v:103-106](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L103-L106) 实现加法，[alu.v:145-147](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L145-L147) 在最终多路选择器里当选中 `FN_ADD` 时输出 `adder_out`。

> 结论：仿照 `VADD_VV`，只需改 3 处——位模式（已有）、译码项（改 `alu_fn`）、运算核（加饱和逻辑）——就能把 `VSADD_VV` 接通。下面逐个最小模块展开。

#### 4.1.4 代码实践（只读热身）

> **实践目标**：验证 `VSADD_VV` 当前确实未被译码。
>
> **操作步骤**：
> 1. 在 `decodeUnit.v` 中搜索 `VSADD_VV`，确认**搜不到**（它没有 casex 条目）。
> 2. 对照 [decodeUnit.v:594](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L594) 的 `default` 分支——未登记的指令都会落到这里，控制字几乎全是 `X`（无关值）、`alu_fn = FN_X(63)`、`isvec = N`。
>
> **预期现象**：`VSADD_VV` 编码若进入译码器，会被当成“未知指令”，`alu_fn` 被设为 `FN_X`、`isvec=N`，进而被 `issue` 当作标量指令发往 sALU，行为未定义。这正是我们要修复的状态。

#### 4.1.5 小练习与答案

- **练习 1**：`VADD_VV`、`VADD_VX`、`VADD_VI` 三者的译码项，哪几个字段不同？为什么？
- **答案**：`sel_alu1` 与 `sel_imm` 不同。`_VV` 第一源是向量寄存器（`A1_VRS1`）、无立即数（`IMM_X`）；`_VX` 第一源是标量寄存器（`A1_RS1`）；`_VI` 第一源是立即数（`A1_IMM`，配 `IMM_V`）。`alu_fn` 三者都是 `FN_ADD`——运算本身不变，差异上推到了操作数采集（见 u4-l1）。
- **练习 2**：为什么 `alu_fn` 是 6 位，而 `alu.v` 的 `op_i` 只有 5 位？
- **答案**：见 4.3，`valu.v` 把 6 位 `alu_fn` 截取低 5 位喂给单 lane 的 `alu`，高位用于在 `valu` 层区分伪指令。

---

### 4.2 最小模块一：define.v —— 位模式与功能码

#### 4.2.1 概念说明

`define.v` 是全项目的“词汇表”。新增指令在此要登记两样东西：

1. **位模式宏**：用 `?` 通配的 32 位串，供译码器 `casex` 匹配。
2. **功能码 `FN_*`**：6 位整数，告诉执行单元做哪种运算。

`VSADD_VV` 的位模式**已经存在**（[define.v:1062](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1062)）：

```
`define VSADD_VV           32'b100001???????????000?????1010111
```

对比 `VADD_VV`，唯一差别是 funct6 `[31:26]`：`100001`（饱和加）vs `000000`（普通加）。这就是 RISC-V 向量指令的编码惯例——同 funct3、不同 funct6 表示同类运算的不同变体。

但它的功能码 `FN_VSADD` 还没定义。我们需要从 6 位（0~63）空间里挑一个空闲值。

#### 4.2.2 核心流程：选一个空闲的 FN 值

`FN_*` 的 6 位空间被多个执行单元**复用**：整数 ALU 用一段、浮点用一段（靠 `fp` 位区分）、SFU/TC 各用一段。挑选新值要同时满足两个约束：

1. 在整数 ALU 命名空间里未被占用（避开 [define.v:470-498](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L470-L498) 已有的 `FN_ADD`…`FN_NMSUB`、伪指令 `FN_V*` 等）。
2. 其**低 5 位**在单 lane `alu.v` 的 localparam 空间里也未占用（因为 6 位会截成 5 位，见 4.3）。

查询已知占用：`0-19`（算术/逻辑/比较/min-max）、`20-27`（乘法族，但被路由到 vmul，不进 alu）、`28/29`（原子 SWAP/AMOADD，进 LSU）、`30`（`FN_VLS12`，见 [define.v:501](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L501)）。因此 **`6'd31`** 是一个干净可用的值（已确认 `6'd31` 在 `define.v` 中无任何引用，`alu.v` 的 localparam 最大用到 `5'd27`）。

#### 4.2.3 源码精读与改动

在 `define.v` 的操作类型区（紧挨 [define.v:498](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L498) 的 `FN_NMSUB` 之后）新增一行。**示例代码（非项目原有）**：

```verilog
// 自定义：有符号饱和向量加，低5位=31 在 alu 中空闲
`define FN_VSADD   6'd31
```

> 提醒：选 `6'd31` 而非随意值，是因为其低 5 位 `5'd31` 在 `alu.v` 的运算核中完全空闲；若误选了与乘法族（`20-27`）低位撞车的值，会导致 alu 行为串扰。

#### 4.2.4 代码实践

> **实践目标**：确认你选的 FN 值真的空闲。
>
> **操作步骤**：
> 1. 在 `define.v` 里全局搜索 `6'd31`，确认无命中。
> 2. 在 `alu.v` 里核对 localparam（[alu.v:31-58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L31-L58)），确认没有 `5'd31`。
>
> **预期结果**：两处均无冲突，`FN_VSADD=6'd31` 安全可用。

#### 4.2.5 小练习与答案

- **练习**：如果把 `FN_VSADD` 误设成 `6'd20`（=`FN_MUL` 的值），会发生什么？
- **答案**：译码虽对，但 `issue` 并不单看 `alu_fn` 决定路由——它看的是控制字里的 `mul` 位（[decodeUnit.v:621](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L621)）。只要译码项里 `mul=N`，它仍会进 vALU；但 `alu.v` 内部会把 `op_i=20` 当成乘法相关运算（虽然 alu 其实不实现乘法，会得到错误结果）。这正说明：**功能码的数值必须与运算核里对该值的实现一致**，否则“名实不符”。

---

### 4.3 最小模块二：decodeUnit —— 把指令译成控制字

#### 4.3.1 概念说明

`decodeUnit` 是一张巨型 `casex` 查找表（[decodeUnit.v:296](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L296) 起）。它一次译 2 条指令（`inst_0`/`inst_1`），把 32 位指令映射成 42 位 `ctrlSignals`，再由 [decodeUnit.v:602-644](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L602-L644) 切片输出。新增指令就是在这张表里加一行，告诉硬件“遇到这串位，按这套控制信号办”。

#### 4.3.2 核心流程

译码项的模板（以 `inst_0` 为例，`inst_1` 完全对称）：

```
`指令名: ctrlSignals_0 = { isvec, fp, barrier, branch[2], simt_stack, simt_stack_op,
                           csr[2], reverse, sel_alu3[2], sel_alu2[2], sel_alu1[2],
                           sel_imm[4], mem_whb[2], alu_fn[6], mul, mem_cmd[2],
                           mem_unsigned, fence, sfu, wvd, readmask, writemask,
                           wxd, tc, disable_mask, custom_signal_0, atomic };
```

> 注：`writemask` 恒为 `1'b0`（见 [decodeUnit.v:629](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L629)），位 [5] 实为保留位，填 `N` 即可。

#### 4.3.3 源码精读与改动

照搬 `VADD_VV`（[decodeUnit.v:396](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L396)）的模板，只把 `alu_fn` 从 `FN_ADD` 改成 `FN_VSADD`。**示例代码（非项目原有）**，插在 `VADD_VV` 那一行之后：

```verilog
`VSADD_VV: ctrlSignals_0 = {`Y,`N,`N,`B_N,`N,`N,`CSR_N,`Y,`A3_X,`A2_VRS2,`A1_VRS1,`IMM_X,`MEM_X,`FN_VSADD,`N,`M_X,`N,`N,`N,`Y,`N,`N,`N,`N,`N,`N,`N};
```

逐字段的“为什么这么填”：

- `isvec=Y`：向量指令，让 `issue` 把它送往 vALU。
- `fp=N, sfu=N, mul=N, tc=N, mem=N(M_X), csr=CSR_N, barrier=N, branch=B_N`：全清零，确保**只有** isvec 这一条路由条件命中（见 4.4）。
- `sel_alu1=A1_VRS1, sel_alu2=A2_VRS2`：两个源都来自向量寄存器（`.vv` 形式），由 `operand_collector` 自动读取。
- `sel_imm=IMM_X`：无立即数（`_VV` 形式不需要）。
- `alu_fn=FN_VSADD`：交给运算核做饱和加。
- `wvd=Y, wxd=N`：结果写回向量目的寄存器。
- `reverse=Y`：与 `VADD_VV` 保持一致（对称运算无副作用）。

不要忘了在 `inst_1` 的 `casex` 表里（约 [decodeUnit.v:751](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L751) 附近的 `VADD_VV` 之后）加一行完全相同的 `ctrlSignals_1`，否则取指双发时第二条会漏译。

#### 4.3.4 代码实践

> **实践目标**：理解译码项的位域，能独立推断一条未知指令的控制字。
>
> **操作步骤**：
> 1. 打开 [decodeUnit.v:602-644](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L602-L644)，对照上文位域表，数清楚 `VSADD_VV` 译码项 27 个字段分别落在哪几位。
> 2. 思考：若要做 `VSADD_VX`（向量 + 标量寄存器饱和加），应把哪个字段从 `A1_VRS1` 改成 `A1_RS1`？
>
> **预期结果**：能指出 `sel_alu1` 对应位域 [27:26]，`_VX` 形式需改为 `A1_RS1`（标量广播，见 u4-l1）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `mem_whb=MEM_X`、`mem_cmd=M_X`？
- **答案**：`VSADD` 是纯运算、不访存。`mem` 相关位清零后，[decodeUnit.v:622](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L622) 的 `control_Signals_mem_o = |mem_cmd` 为 0，`issue` 不会把它当访存指令送往 LSU。
- **练习 2**：`default` 分支（[decodeUnit.v:594](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L594)）里 `alu_fn=FN_X`、`isvec=N`，意味着未登记指令会被怎么处理？
- **答案**：被当作标量非向量指令（`isvec=N`），在 `issue` 的优先级链里一路落到最末的 sALU（标量 ALU），`alu_fn=FN_X(63)` 对应未定义运算——这就是为什么“忘了写译码项”会让指令静默地行为错乱。

---

### 4.4 最小模块三：路由与执行 —— issue 自动派发，alu 实现运算

#### 4.4.1 概念说明

本模块说明一个关键事实：**新增 `VSADD_VV` 不需要改 `issue.v` 和 `valu.v`**。只要译码位填对，现成的路由与 lane 阵列会自动接纳它。真正要动算法的只有单 lane 运算核 `alu.v`。

#### 4.4.2 核心流程

`issue` 是纯组合的优先级路由器（详见 u3-l4）。其分支顺序（[issue.v:538-628](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L538-L628)）为：TC → SFU → vFPU → CSR → MUL → LSU → **isvec（含 simt_stack 判定）→ vALU** → barrier → 默认 sALU。

`VSADD_VV` 的控制字里 `tc/sfu/fp/csr/mul/mem/barrier` 全为 0，`isvec=1`，且 `simt_stack=0`，于是命中 [issue.v:585-598](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L585-L598) 的 `else` 分支，直送 vALU——与 `VADD_VV` 走同一条路。

进入 `valu.v` 后，`generate for` 把同一个 `alu` 内核复制 `NUM_THREAD` 份（[valu.v:86-99](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L86-L99)），每 lane 把 6 位 `alu_fn` 截低 5 位喂给 `alu`（[valu.v:162](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L162)）。`FN_VSADD=6'd31` 截取后为 `5'd31`，在 `alu` 的 localparam 空间里未被任何运算占用——我们要做的就是在 `alu.v` 里为 `op_i==5'd31` 添加饱和加的实现。

#### 4.4.3 源码精读与改动

先看现有 `alu` 如何产出加法结果。[alu.v:103-106](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L103-L106) 已经算好了 `adder_out = in1 + in2`（对非减法 `isSub=0` 时即原值相加）；[alu.v:158-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L158-L160) 是最终输出多路选择器。饱和加只需在这两处之间加一段溢出检测，并把新 case 接入 `out_o`。

**有符号饱和加的原理**：设 `sum = a + b`。当 a、b 同号但 `sum` 与它们异号时，发生溢出；此时按 a 的符号钳位到 `INT_MAX(0x7fffffff)` 或 `INT_MIN(0x80000000)`。用公式表达溢出判定：

\[
\text{overflow} = (a_{31} = b_{31}) \land (s_{31} \ne a_{31})
\]

**示例代码（非项目原有）**，添加在 `alu.v` 的 `minmaxout`/`out` 计算之后：

```verilog
// —— 自定义：有符号饱和加（op_i == 5'd31）——
localparam FN_VSADD_ALU = 5'd31;
wire vsadd_overflow = (in1_i[31] == in2_i[31]) && (adder_out[31] != in1_i[31]);
wire [`XLEN-1:0] vsadd_sat   = in1_i[31] ? 32'h8000_0000 : 32'h7fff_ffff;
wire [`XLEN-1:0] vsadd_result= vsadd_overflow ? vsadd_sat : adder_out;
```

然后把 `out_o` 的选择链最前面补一个新分支（[alu.v:158-160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/alu.v#L158-L160) 原文为 `out_o = op_i==FN_A1ZERO ? ... : ...`）：

```verilog
assign out_o = (op_i == FN_VSADD_ALU) ? vsadd_result :
               (op_i == FN_A1ZERO)     ? in2_i       :
               (op_i == FN_A2ZERO)     ? in1_i       :
               (isMIN ? minmaxout : out);
```

> 说明：`alu.v` 里 `isSub`、`adder_out` 等信号对 `op_i=31` 仍然有效（`isSub=0`，`adder_out` 就是普通和），所以饱和逻辑可以直接复用现成的加法器，零额外面积用于求和。这与 u4-l2 讲到的“alu 资源共享”哲学一致。

至于 `valu.v`：因为 `op_i` 取自 `ctrl_alu_fn_i[4:0]`，`FN_VSADD=6'd31`→`5'd31` 自动对齐，**无需改动**。`aluexe.v`（标量通路，[aluexe.v:70-78](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/aluexe.v#L70-L78)）也无需改动——向量饱和加不走标量通路。

#### 4.4.4 代码实践

> **实践目标**：验证饱和加算法的正确性（脱离仿真器，先用纸笔）。
>
> **操作步骤**：用上面公式手算三组 32 位有符号数：
> 1. `0x4000_0000 + 0x4000_0000`（两正数相加溢出）
> 2. `0x8000_0000 + 0xFFFF_FFFF`（负数 + 负数溢出）
> 3. `0x0000_0003 + 0x0000_0004`（正常，不溢出）
>
> **预期结果**：
> 1. 同号、`sum=0x8000_0000`（变负）→ 溢出 → 钳位 `0x7fff_ffff`。
> 2. 同号、`sum=0x7fff_ffff`（变正）→ 溢出 → 钩位 `0x8000_0000`。
> 3. 不溢出 → `0x0000_0007`。

#### 4.4.5 小练习与答案

- **练习 1**：为什么说“新增 `VSADD_VV` 不用改 `issue.v`”？
- **答案**：`issue` 的路由只看控制字里的分类位（`isvec/mul/fp/...`），不看具体 `alu_fn`。`VSADD_VV` 与 `VADD_VV` 的这些分类位完全相同（都 isvec、都无 mul/fp/...），因此命中同一条 `isvec→vALU` 分支（[issue.v:585-590](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L585-L590)）。
- **练习 2**：若要新增的是 `VDIV`（向量除法），路由会有何不同？
- **答案**：除法是高延迟运算，要走 SFU。译码项要把 `sfu` 位置 `Y`、`alu_fn` 用 SFU 命名空间的 `FN_DIV`，`issue` 便会优先于 isvec 把它送往 SFU（见 u4-l5）。

---

### 4.5 最小模块四：operand_collector 与 gen_imm —— 取数与立即数

#### 4.5.1 概念说明

`operand_collector` 负责为发射出的指令凑齐源操作数（详见 u4-l1）。它完全由译码出的 `sel_alu1/2/3` 与 `sel_imm` 驱动——**这些字段我们在 4.3 已经填好**，所以采集器会自动按“向量寄存器 rs1、rs2”去读 VGPR，无需额外改动。

唯一需要留意的是**立即数**：`_VV` 形式无立即数（`sel_imm=IMM_X`），故不碰 `gen_imm`；但若做 `VSADD_VI`（向量 + 立即数饱和加），则需 `sel_imm=IMM_V`，由 `gen_imm` 把指令位拼成 32 位立即数。

#### 4.5.2 核心流程

`gen_imm` 是一张按 `{sel_i, inst[31]}` 索引的选择表（[gen_imm.v:62-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/gen_imm.v#L62-L95)）。其中 `IMM_V` 分支（[gen_imm.v:79](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/gen_imm.v#L79)）会结合 `REGEXTI` 前缀扩展出的高位 `imm_ext_i`，把 5 位 simm 拼成更宽的立即数并符号扩展。

#### 4.5.3 源码精读

对本讲的 `VSADD_VV`：`sel_imm=IMM_X(4'd0)`，`gen_imm` 走默认/无关分支，立即数输出不会被使用；两个源操作数由 `operand_collector` 经 bank 仲裁从 VGPR 读出（流程见 u4-l1 的沙漏结构）。因此 **`gen_imm.v` 无需修改**。

若扩展为 `VSADD_VI`：只需在译码项把 `sel_alu1=A1_IMM`、`sel_imm=IMM_V`，`gen_imm` 的 `IMM_V` 分支已现成可用——**仍无需改 `gen_imm`**，除非你发明了全新的立即数编码格式。

#### 4.5.4 代码实践

> **实践目标**：确认 `_VV` 形式确实不需要立即数通路。
>
> **操作步骤**：对照 `VSADD_VV` 译码项的 `sel_imm=IMM_X`，在 [gen_imm.v:63](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/gen_imm.v#L63) 的 `casex` 里找 `IMM_X(=4'd0)` 是否有专门分支。
>
> **预期结果**：`IMM_X` 无专门分支，落入默认/不命中，立即数输出对 `VSADD_VV` 无意义，符合“VV 形式不取立即数”。

#### 4.5.5 小练习与答案

- **练习**：`VSADD_VV` 的两个源操作数分别经哪条路径进入 lane 的 `alu`？
- **答案**：经 `operand_arbiter`（按 bank 仲裁）→ `collector_unit`（汇集 src1/src2）→ 以 `in1_i/in2_i` 进入每 lane 的 `alu`（[valu.v:92-94](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L92-L94)）。详见 u4-l1。

---

### 4.6 最小模块五：写回与测试 —— 结果落袋与仿真验证

#### 4.6.1 概念说明

vALU 算完结果后，由 `wvd=Y` 触发写回向量寄存器堆。`pipe.v` 把 vALU 的输出挂在向量写回总线的 `[0]` 槽位（[pipe.v:886-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L886-L891)），与 `tensor/mul/sfu/lsu/fpu` 并列。因为 `VSADD_VV` 复用 vALU，写回通路**零改动**。

验证则需要一个能产生 `VSADD_VV` 指令的 kernel。

#### 4.6.2 核心流程

测试框架（详见 u8-l1）的运行链：`tc.v` 的 `init_mem` 用 `force` 把 `.data`/`.metadata` 灌进 `axi_ram` → `drv_gpu` 经 AXI4-Lite 触发 workgroup 派发 → GPU 执行 → `exe_finish` 轮询完成 → `print_result` 比对黄金参考。其中 [tc.v:83-103](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L83-L103) 是 `test_main` 主流程。

生成含 `VSADD_VV` 的 kernel 有两条路：

- **完整路（推荐但需工具链）**：用 Ventus 的 LLVM 后端编译一段含 `vsadd` 内联汇编的 C/CL 程序，产出新的 `object.vmem`/`vecadd_0.data`/`vecadd_0.metadata`。
- **手搓路（无需工具链，适合学习）**：直接修改某个 `.data` 文件，把其中一条 `VADD_VV` 的机器码（funct6=`000000`）改成 `VSADD_VV`（funct6=`100001`），即把指令字的高 6 位由 `000000` 改为 `100001`，再相应调整 `print_result` 的黄金参考为饱和结果。

#### 4.6.3 源码精读

- 仿真入口与 CASE 选择见 [tc.v:60-78](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/tc.v#L60-L78)（`init_test_file` 按 `CASE_*` 宏选 softdata 子目录）。
- Makefile 目标见 [Makefile:12-22](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/Makefile#L12-L22)，如 `make run-vcs-8w4t`（对应 `NUM_THREAD=4`，须与 `define.v` 一致，见 u1-l4）。

#### 4.6.4 代码实践

> **实践目标**：用 tc_vecadd 框架验证 `VSADD_VV`（待本地验证）。
>
> **操作步骤**：
> 1. 完成 4.2~4.4 的三处源码改动（`define.v` / `decodeUnit.v` / `alu.v`）。
> 2. 用 objdump 查看 `softdata/8w4t/object.dump`，定位一条 `vadd.vv` 指令及其机器码；在对应的 `object.vmem` 中把该字的高 6 位改成 `100001`（即把 `0x0xxxxxxx` 的 `[31:26]` 置为 `100001`），使其变成 `vsadd.vv`。
> 3. 编辑 `tc.v` 的 `print_result`，把对应输出的黄金参考改为手算的饱和加结果（参考 4.4.4 的三组样例）。
> 4. 确认 `define.v` 的 `NUM_THREAD=4`，执行 `cd testcase/test_gpgpu_axi_top/tc_vecadd && make run-vcs-8w4t`。
> 5. 查看 `simv.log` 的 `PASSED`/`FAILED`，必要时 `make verdi` 打开波形，在 `u_dut.sm[0].pipe...alu` 信号上观察 `op_i==31` 与 `vsadd_result`。
>
> **需要观察的现象**：`op_i` 出现 `5'd31`；溢出样例 lane 的 `alu_out` 被钳位到 `0x7fff_ffff` 或 `0x8000_0000`。
>
> **预期结果**：`simv.log` 打印 `PASSED`。
>
> **若无法本地运行**（缺 VCS 或 Ventus LLVM）：明确标注「待本地验证」，转而做“源码阅读型验证”——在 `alu.v` 用 `$display` 打印 `op_i` 与 `out_o`，逻辑检视 4.4.4 的三组样例是否自洽。

#### 4.6.5 小练习与答案

- **练习 1**：为什么改 `.vmem` 后必须同步改 `print_result` 的黄金参考？
- **答案**：`print_result`（详见 u8-l1）是把硬件写回结果与硬编码参考逐字比对来决定 `PASSED`/`FAILED`。指令换成饱和加后，正确结果变了，参考必须同步更新，否则即使硬件算对也会判 `FAILED`。
- **练习 2**：若仿真卡死、不报 `PASSED` 也不报 `FAILED`，最可能漏改了什么？
- **答案**：多半是漏加了 `inst_1` 的译码项（4.3.3 提醒的双发对称条目）。当 `VSADD_VV` 落在双发的第二条槽位时，未登记会让 `wvd=N`、结果不写回，warp 可能卡在等待。

---

## 5. 综合实践：从零接通一条自定义指令

把本讲内容串起来，完成下面这个端到端任务：

> **任务**：为 Ventus 新增 `VSADD_VV`（有符号饱和向量加），并在 tc_vecadd 框架下验证。

执行清单（按依赖顺序）：

1. **【define.v】** 新增 `` `define FN_VSADD 6'd31 ``（4.2）。
2. **【decodeUnit.v】** 在 `inst_0` 与 `inst_1` 两张 `casex` 表里，照 `VADD_VV` 各加一行 `VSADD_VV` 译码项，`alu_fn=FN_VSADD`（4.3）。
3. **【alu.v】** 加 `vsadd` 溢出检测与钳位逻辑，并在 `out_o` 选择链最前插入 `op_i==5'd31` 分支（4.4）。
4. **【检查免改项】** 确认 `issue.v`/`valu.v`/`gen_imm.v`/`pipe.v` 均无需改动，并说出理由（4.4、4.5、4.6）。
5. **【测试】** 用 4.6.4 的“手搓路”或“工具链路”产出含 `vsadd.vv` 的 kernel，跑 `make run-vcs-8w4t`，看到 `PASSED`。

完成后，尝试把同样的 5 步套用到 `VSADDU_VV`（无符号饱和加，位模式见 [define.v:1064](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1064)）：思考无符号饱和的溢出判定（进位位 `carry_out`）与钳位值（`0xffffffff`）应如何写。这会逼你真正理解 4.4 的算法，而非照抄。

> 全程请用一份独立的 `git diff` 记录你的改动；正式提交前务必在至少两组 `NUM_THREAD`（如 4 和 8）下都跑通，因为 lane 数变化会暴露你在 `generate for` 边界上的笔误。

## 6. 本讲小结

- 新增一条向量指令的最小改动集是 **3 个文件**：`define.v`（位模式 + FN）、`decodeUnit.v`（译码项）、执行单元（如 `alu.v` 的算法）；取数、发射、写回通常靠译码位自动满足。
- `decodeUnit` 的 42 位控制字是核心：`isvec/mul/fp/sfu/...` 决定**路由**，`sel_alu1/2/sel_imm` 决定**取数**，`alu_fn` 决定**运算**，`wvd/wxd` 决定**写回**。
- `issue` 是按控制位分类的优先级路由器，不看具体 `alu_fn`；所以同类指令（同为向量算术）能复用同一条 `isvec→vALU` 通路。
- `alu_fn` 是 6 位、`alu` 的 `op_i` 是 5 位（截低 5 位），新选 FN 值必须**两个空间都不撞车**。
- `VSADD_VV` 是个真实案例：它的编码宏早已存在于 `define.v`，却因缺译码项而“名存实亡”——接通它正是把所学串起来的最佳练习。
- 验证离不开 tc_vecadd 框架：`.data`/`.metadata` 经 `init_mem` 灌入 RAM，`PASSED`/`FAILED` 由 `print_result` 比对黄金参考决定，改了指令就要同步改参考。

## 7. 下一步学习建议

- **横向扩展**：用本讲的 5 步法，尝试新增一条**浮点**指令（改 `fpu` 子单元，注意 `fp` 位与舍入模式 `rm`，回顾 u4-l4）或一条 **SFU** 指令（高延迟，注意 scoreboard 忙位覆盖，回顾 u4-l5），体会不同执行单元在路由与延迟上的差异。
- **纵向深入**：若你的指令需要**新的立即数编码格式**，去读 `gen_imm.v` 的 `casex` 表（[gen_imm.v:62](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/gen_imm.v#L62)）与 `REGEXT/REGEXTI` 前缀扩展机制（`decodeUnit` 的 `scratchPads`，回顾 u3-l3）。
- **配套软件**：RTL 改完只是半程——要让编译器真正生成新指令，需修改 Ventus 的 **LLVM 后端**（仓库外的 `ventus-gpgpu-compiler` 项目）。建议阅读其指令定义表，理解软硬件协同的另一半。
- **回归架构**：重读 u3-l1 的 `pipe.v` 全景图，现在你应该能指着图上每一格说出“新增指令时这格要不要改、为什么”——这才是真正的融会贯通。
