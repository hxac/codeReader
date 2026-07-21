# 共享类型与宏定义

## 1. 本讲目标

本讲聚焦于向量数据通路的「公共词典」——三份被几乎所有模块反复 `` `include `` 的头文件：

- `rtl/vector/vstructs.sv`：定义**向量指令在各级流水之间传递时所用的结构体**（`to_vector`、`remapped_v_instr`、`to_vector_exec` 等），以及**整数操作码枚举** `v_int_op_t`。
- `rtl/vector/vmacros.sv`：定义**功能单元（FU）编码**、**存储操作编码**、以及**转发点宏**。
- `rtl/shared/structs.sv`：定义标量侧的结构体（ROB 表项、译码指令等），其中 `is_vector` 标志是「标量核 → 向量数据通路」的分界点。

读完本讲，你应当能够：

- 看懂 `to_vector` / `remapped_v_instr` / `to_vector_exec_info` 等结构体里每一个字段的含义、位宽和它「在流水线哪一级被填充」。
- 理解 `v_int_op_t` 操作码枚举如何把一个 7 位 `microop` 映射成具体运算（如 `VADD = 7'b0000001`），并知道这条 7 位编码最终在 `v_int_alu` 的 `case` 里被译码。
- 掌握 `MEM_FU / FP_FU / INT_FU / FXP_FU` 四种功能单元编码，以及 `vmacros.sv` 里存储寻址模式（unit-strided / strided / indexed）、元素宽度（SZ_8/16/32）和转发点（`EX1`/`EX4_F`）这一整套宏。
- 解释 `vstructs.sv` 为什么用 `DUMMY_VECTOR_LANES` 这种「假常量」而不是直接用 `params.sv` 里的真实参数——以及由此带来的「改 lane 数必须同步改」的隐藏耦合（承接 u1-l3）。

本讲只讲「数据长什么样」，不讲「数据怎么被处理」。结构体和宏是后续阅读 `vrrm`、`vis`、`vex`、`vmu` 全部模块的前提——它们定义了模块之间「接口的物理形状」。

## 2. 前置知识

在阅读本讲前，你需要了解几个 SystemVerilog 的关键语法：

- **`typedef struct packed { ... } 名字;`**：定义一个「紧凑打包」的结构体。`packed` 的含义是：所有字段像一条连续的位向量那样**首尾相接、没有空隙**地排布，因此整个结构体可以当作一个完整的位宽来传递、存储、拼接。本讲的结构体几乎全部是 `packed`，这样它们才能整体塞进一个流水线寄存器或一个 FIFO 槽位。
- **位宽标注 `logic [Hi:Lo]`**：`logic [4:0] dst` 表示一个 5 位的字段（位 4 到位 0）。`logic [31:0]` 就是 32 位。结构体里每个字段的位宽都是手工写死的。
- **`enum`（枚举）**：给一组具名常量赋予固定的二进制编码。`v_int_op_t` 就是一个枚举，每个助记符（如 `VADD`）都对应一个固定的 7 位编码。用枚举而不是裸数字，是为了让代码可读——`microop == VADD` 比 `microop == 7'b0000001` 直观得多。
- **`` `define `` 宏**：SystemVerilog 的文本替换宏。`` `define INT_FU 2'b10 `` 之后，源码里凡写 `` `INT_FU `` 的地方都会被替换成 `2'b10`。`vmacros.sv` 里全是这种宏，作用是「给魔法数字起名字」。
- **`` `include `` 与 `+incdir`**：承接 u1-l2，`` `include "vmacros.sv" `` 会把另一个文件的内容原地展开；为了让编译器找到这些文件，需要在 `vlog` 命令里用 `+incdir` 指定搜索目录。
- **流水级与数据通路**：承接 u1-l1，向量主路是 `vRRM → vIS → vEX`，存储岔路是 `vMU`。本讲定义的各个结构体，正是这四级之间「接口」的具体形状。

> 一个贯穿全讲的关键直觉：**结构体字段 = 流水线上「谁负责填、谁负责用」的契约**。比如 `to_vector` 里的 `dst/src1/src2` 由测试台/标量核填，而 `remapped_v_instr` 里多出来的 `ticket`、`lock` 则是 `vRRM`（寄存器重映射）填进去的——结构体越往后流水级走，字段就越多，因为一路上不断有新的「重命名信息、同步信息」被附加进来。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `rtl/vector/vstructs.sv` | 向量侧所有结构体定义 + `v_int_op_t` 操作码枚举。本讲的主角。 |
| `rtl/vector/vmacros.sv` | 向量侧的宏定义：FU 编码、存储操作编码、转发点宏。 |
| `rtl/shared/structs.sv` | 标量侧结构体定义。其中 `decoded_instr` 的 `is_vector` 字段是标量→向量分流的标志。 |
| `rtl/vector/v_int_alu.sv` | 整数 ALU。它用 `case (microop_i)` 译码，证明 `v_int_op_t` 的编码确实「落到了硬件上」。 |
| `rtl/vector/vex_pipe.sv` | 单 lane 执行流水。它用 `fu_i === `INT_FU` 判断是否走整数路径，证明 FU 宏的真实用法。 |
| `vector_simulator/vector_driver.sv` | 测试台驱动器。它逐字段拼装 `to_vector`，是理解「结构体字段从哪来」的最佳入口。 |
| `rtl/shared/params.sv` | 真实参数所在地。`vstructs.sv` 用 `DUMMY_VECTOR_LANES` 而非 `VECTOR_LANES`，二者必须手工同步。 |

## 4. 核心概念与源码讲解

### 4.1 向量指令结构体（vstructs.sv）

#### 4.1.1 概念说明

向量数据通路是一条流水线，指令要逐级往下传。每一级需要的「信息」并不一样：

- 入口（测试台或标量核送进来）：只需要「这条指令是什么、操作数是哪几个寄存器、有没有立即数」。
- 经过 `vRRM`（寄存器重映射）之后：还要带上「物理寄存器号、同步用的 ticket、要不要锁住某个寄存器」。
- 到了执行级 `vEX`：操作数已经从寄存器堆读出来了，所以结构体退化成「两个 32 位数据 + 一个立即数 + 一些控制位」。

因此 `vstructs.sv` 定义了**一组结构体**，每个对应流水线的一段。它们不是凭空设计的，而是「数据通路每一段接口形状」的直接写照。

#### 4.1.2 核心流程

把 `vstructs.sv` 里的结构体按流水线顺序排开，信息是「逐级累加、再逐级精简」的：

1. **`to_vector`**：入口契约。测试台/标量核按它填字段，送给向量数据通路。
2. **`remapped_v_instr`**：`vRRM` 重命名后的契约。比 `to_vector` 多了 `*_iszero`（零寄存器优化）、`mask_src`（掩码寄存器号）、`ticket`（同步票据）、`lock`（锁定位）。
3. **`memory_remapped_v_instr`**：存储岔路专用。多了 `last_ticket_src1/src2`（用于和 store 的源操作数同步）。
4. **`to_vector_exec` / `to_vector_exec_info`**：执行级契约。前者装数据，后者装控制（`dst/ticket/fu/microop/vl/head_uop/end_uop`）。
5. **`vector_mem_req` / `vector_mem_resp`**：`vMU` 与缓存/主存之间的请求/响应。

一条整数指令 `vadd` 的「字段旅行」大致是：

```
to_vector  ──vRRM──▶  remapped_v_instr  ──vIS──▶  to_vector_exec + to_vector_exec_info  ──vEX──▶ 写回
                                  │
                                  └─存储指令分叉──▶ memory_remapped_v_instr ──vMU──▶ vector_mem_req
```

#### 4.1.3 源码精读

**入口结构体 `to_vector`** —— 这是测试台拼装、向量数据通路接收的第一份契约：

[vstructs.sv:13-32](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L13-L32) 定义了 `to_vector`。字段逐一看：

| 字段 | 位宽 | 含义 |
|------|------|------|
| `valid` | 1 | 本拍是否是一条有效指令 |
| `dst / src1 / src2` | 各 5 | 目标/源 1/源 2 的**架构**寄存器号（5 位 → 0~31，共 32 个向量寄存器） |
| `data1 / data2` | 各 32 | 标量/立即数旁路数据（寄存器-寄存器运算时一般不用） |
| `reconfigure` | 1 | 是否要求向量核「重配置」（如清空寄存器重映射状态） |
| `immediate` | 32 | 立即数 |
| `fu` | 2 | 功能单元编码（见 4.3，`INT_FU=2'b10` 等） |
| `microop` | 7 | 操作码（见 4.2，`VADD=7'b0000001` 等） |
| `use_mask` | 2 | 掩码使用模式（被掩码门控，详见 u2-l6） |
| `maxvl / vl` | 各 9 | 最大向量长度 / 当前向量长度（位宽推导见下） |

注意 `maxvl/vl` 的位宽写法：

[vstructs.sv:30-31](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L30-L31) 用 `logic [$clog2(32*DUMMY_VECTOR_LANES):0]` 来定义位宽。代入 `DUMMY_VECTOR_LANES=8`：

\[
\text{宽度} = \$clog2(32 \times 8) + 1 = \$clog2(256) + 1 = 8 + 1 = 9 \text{ 位}
\]

之所以要 `+1`（写成 `[...:0]`），是因为 `vl` 既要能表示 0 也要能表示「满长度 256」，\[0, 256\] 共 257 个值需要 9 位。这是一个很容易踩的细节。

> **为什么用 `DUMMY_VECTOR_LANES` 而不是 `VECTOR_LANES`？** 看 [vstructs.sv:6-9](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L6-L9)：这个文件在 `` `ifdef MODEL_TECH`` 下**只 `` `include "vmacros.sv" ``，并不 `` `include "params.sv" ``**。也就是说它编译时根本「看不见」真实参数 `VECTOR_LANES`，只能用一个本地假常量 `DUMMY_VECTOR_LANES = 8` 来推导位宽。`params.sv` 第 87 行的注释「must also change dummy param in vstructs」就是指这个——**改 lane 数时，`vstructs.sv` 第 9 行的 `DUMMY_VECTOR_LANES` 必须手工同步**，否则结构体位宽和实际硬件不一致。这正是 u1-l3 提到的「隐藏耦合陷阱」，它物理上就发生在本讲这个文件里。

**重命名后结构体 `remapped_v_instr`** —— 经过 `vRRM` 后字段变多了：

[vstructs.sv:35-60](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L35-L60) 定义了 `remapped_v_instr`。相比 `to_vector`，新增/变化的有：

- `dst_iszero / src1_iszero / src2_iszero`（各 1 位）：标记该操作数是不是「零寄存器」。零寄存器不需要真正读寄存器堆，给个 0 即可，省去一次读端口冲突。
- `mask_src`（5 位）：掩码来源寄存器号（`to_vector` 里没有，因为掩码是后续才确定的）。
- `ticket`（5 位）：同步票据，`vRRM` 分配，用于在解耦执行里做 acquire-release（详见 u4-l1）。
- `use_mask` 从 2 位变成了 1 位（语义被精简）。
- `lock`（2 位）：锁定位，告诉 `vIS`/`vMU` 这个寄存器在什么条件下要锁住、何时解锁。`lock` 的具体编码（load/store/toeplitz 区分）会在 u2-l3、u3-l1 展开。

**执行级契约 `to_vector_exec` 与 `to_vector_exec_info`** —— 到了执行级，操作数已经被读出来了，结构体分成了「数据」和「控制」两半：

[vstructs.sv:89-96](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L89-L96) 的 `to_vector_exec` 只有 `valid/mask/data1/data2/immediate`——纯粹的运算数据。

[vstructs.sv:97-105](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L97-L105) 的 `to_vector_exec_info` 则是控制：`dst/ticket/fu/microop/vl`，外加 `head_uop/end_uop`——这两个 1 位标志说明当前这个 micro-op 是「硬件循环展开后的第一个」还是「最后一个」（详见 u2-l6）。

**存储请求/响应 `vector_mem_req` / `vector_mem_resp`** —— `vMU` 与缓存之间的接口：

[vstructs.sv:108-114](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L108-L114) 与 [vstructs.sv:117-121](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L117-L121)。注意 `data` 字段宽达 512 位（`DUMMY_REQ_DATA_WIDTH=512`，见 [vstructs.sv:10](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L10)），对应 `params.sv` 里的 `VECTOR_MAX_REQ_WIDTH=512`——一次存储请求可以搬动一整块数据。

> **「打包宽度」小算术**：因为结构体是 `packed`，整条记录就是一个位宽。以 `to_vector` 为例累加各字段：1+5+5+5+32+32+1+32+2+7+2+9+9 = **142 位**。`remapped_v_instr` 累加下来约 **156 位**。这些数字不重要，但它们说明：流水线寄存器、FIFO 槽位的物理宽度，都是由这些结构体决定的。

#### 4.1.4 代码实践

**实践目标**：用真实源码验证「结构体字段是被谁填充的」，把抽象的 `to_vector` 落到具体硬件行为上。

**操作步骤**：

1. 打开 [vector_driver.sv:65-77](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/vector_driver.sv#L65-L77)。这是测试台驱动器逐字段赋值 `to_vector` 的地方——你会看到 `instr_o.dst = destination_list[head]`、`instr_o.microop = microop_list[head]`、`instr_o.fu = fu_list[head]` 等等，每个 `to_vector` 字段都对应一个 `*_list` 数组。
2. 在 `vector_driver.sv` 里向上找这些 `*_list` 是怎么被填充的（通常是 `$readmemb` 从文件加载），确认「字段值来自测试向量文件」。
3. 把它和 [vstructs.sv:13-32](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L13-L32) 的字段定义一一对照，画一张「字段 ← 来源」对照表。

**需要观察的现象**：驱动器里 `instr_o.use_mask = '0`（恒为 0），说明当前测试台默认不注入掩码；而 `dst/src1/src2` 是动态的，逐条指令不同。

**预期结果**：你会得出结论——`to_vector` 的每个字段都是「测试台能直接控制」的，这正是「为什么可以用 CSV 仿真向量程序」的根本原因（详见 u1-l5、u4-l5）。

**待本地验证**：若你有 QuestaSim 环境，可在驱动器里临时把某条指令的 `microop` 改成非法值，观察下游 ALU 是否走到 `default` 分支。

#### 4.1.5 小练习与答案

**练习 1**：`to_vector` 里有 `data1/data2` 又有 `src1/src2`，它们为什么不重复？
**答案**：`src1/src2` 是「寄存器号」（告诉硬件去 VRF 哪个位置取数），`data1/data2` 是「直接旁路的数据」（标量核送来的立即数或已就绪标量值）。寄存器-寄存器运算只用 `src1/src2`，立即数运算用 `immediate` 或 `data*`。

**练习 2**：为什么 `remapped_v_instr` 比 `to_vector` 多了 `ticket` 和 `lock`，却把 `use_mask` 从 2 位缩成了 1 位？
**答案**：`ticket/lock` 是 `vRRM` 在重命名阶段才产生的「同步与锁信息」，入口阶段还没有；而 `use_mask` 经过 `vRRM` 后语义被确定下来（要么用要么不用），不再需要入口那种「2 位选择模式」的冗余。

---

### 4.2 操作码枚举（v_int_op_t）

#### 4.2.1 概念说明

一条向量整数指令（如 `vadd`、`vmul`）具体要做什么运算，靠 `microop` 这 7 位字段来区分。为了让源码可读，`vstructs.sv` 把所有合法的 7 位编码收进一个枚举类型 `v_int_op_t`，每个助记符对应一个固定编码。

这是个典型的「软件汇编助记符 ↔ 硬件二进制编码」的对照表，和 RISC-V 标量指令集的 funct 字段是同一类东西。

#### 4.2.2 核心流程

`v_int_op_t` 的使用链路是：

1. 测试台/`sim_generator.py` 把一条助记符（如 `VADD`）翻译成 7 位编码，塞进 `to_vector.microop`。
2. 这 7 位随结构体一路传到执行级 `vex_pipe`。
3. 在整数 ALU `v_int_alu` 里，一个 `case (microop_i)` 用**同样的二进制编码**选择对应的运算电路。

也就是说：**枚举里的编码值，必须和 ALU 的 `case` 分支值一一对应**。这是本枚举最硬的约束。

#### 4.2.3 源码精读

**枚举定义**：

[vstructs.sv:124-155](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L124-L155) 定义了 `v_int_op_t`，类型为 `enum logic [7-1:0]`，即 7 位。摘录前几项：

```
VADD    = 7'b0000001,   // 加法
VADDI   = 7'b0000010,   // 加立即数
VADDW   = 7'b0000011,   // 加法（word，低 16 位）
...
VMUL    = 7'b0000111,
VMULH   = 7'b0001000,
...
VDIV    = 7'b0001100,
...
```

特别地，[vstructs.sv:125](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L125) 明确 **`VADD = 7'b0000001`**——这就是本讲实践任务要找的「VADD 的 7 位 microop 值」。

> 注意编码没有从 `7'b0000000` 开始（`VADD` 是 `0000001`）。`0000000` 留作「非法/空」编码，便于断言（见 u4-l6）和 `case` 的 `default` 分支识别。

**编码在硬件里被消费**：这 7 位并不是只存在于头文件里，它真的被译码。看整数 ALU：

[v_int_alu.sv:116-121](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L116-L121) 用 `case (microop_i)` 译码，第一个分支 `7'b0000001` 注释写着 `// VADD`，执行 `data_a + data_b`。把这个二进制值和枚举里的 `VADD = 7'b0000001` 对照——**完全一致**。这就是「枚举编码必须和 ALU case 对齐」的实证。

#### 4.2.4 代码实践

**实践目标**：验证「枚举编码 ↔ ALU case」的一致性，建立对操作码链路的信心。

**操作步骤**：

1. 在 [vstructs.sv:124-155](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L124-L155) 里任选 3 个枚举项（如 `VADD`、`VMUL`、`VDIV`），记下它们的 7 位编码。
2. 打开 [v_int_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv)，搜索这 3 个编码值，确认它们各自对应的 `case` 分支做了正确的运算。
3. 顺便统计：`v_int_op_t` 枚举里一共有多少项？`v_int_alu` 的 `case` 里实际处理了多少项？有没有「枚举里有、ALU 里没实现」的操作？（这关系到 u4-l6 的操作码合法性断言。）

**需要观察的现象**：枚举项数与 ALU `case` 分支数是否相等；若有差异，记录差异项。

**预期结果**：基本运算（加/减/与/或/异或/移位/比较）都应有对应 `case` 分支；复杂运算（乘除）可能在 ALU 的另一段（MUL/DIV 子模块，见 u2-l8）处理，但 `microop` 编码仍然由本枚举统一定义。

**待本地验证**：若想确认某个操作码的真实行为，可在仿真里构造一条只含该操作的 CSV，跑通后看 `results.log` 与波形。

#### 4.2.5 小练习与答案

**练习 1**：`v_int_op_t` 是 7 位，理论上有 128 个编码。但枚举里只列了 30 个左右。多出来的编码空间有什么用？
**答案**：留给「非法编码」和「未来扩展」。未占用的编码（如 `0000000` 或枚举末尾的空位）可以被断言识别为非法 microop 并报错，也可用于将来新增指令而不破坏已有编码。

**练习 2**：为什么用 `enum` 而不是直接在源码里到处写 `7'b0000001`？
**答案**：可读性与可维护性。`microop == VADD` 一眼就懂，而 `7'b0000001` 是魔法数字。更重要的是，`enum` 把「编码—助记符」的绑定集中在一处，改编码只需改枚举，不必满代码库搜索替换。

---

### 4.3 FU 编码、存储操作与转发点宏（vmacros.sv）

#### 4.3.1 概念说明

如果说 `vstructs.sv` 定义了「数据的形状」，那么 `vmacros.sv` 定义的是「形状里那些控制位的取值约定」。它是一袋子 `` `define `` 宏，给三类东西起名字：

1. **功能单元（FU）编码**：一条指令要走哪个执行单元（存储/浮点/整数/定点）。
2. **存储操作编码**：存储指令的寻址模式、元素宽度、归约操作等。
3. **转发点宏**：执行流水里转发可以「从哪一级」引出，以及是否插入寄存器（flopped）。

这些宏本身只是一堆数字的别名，但它们被 `params.sv`、`vex_pipe`、`vmu` 等模块反复引用，是「跨模块共享的取值约定」。

#### 4.3.2 核心流程

宏的生命周期：

1. `vmacros.sv` 定义宏。
2. 任何需要用它的文件，在仿真时通过 `` `include "vmacros.sv" ``（或在 `params.sv`/`vstructs.sv` 里被间接 include）把它展开。
3. 模块里写 `` `INT_FU ``、`` `EX4_F `` 等宏名，编译期替换成对应的二进制常量。

因此宏是「编译期文本替换」，不占运行时资源，但要求所有模块对同一个名字有一致的取值认知——这正是把它集中放一份的原因。

#### 4.3.3 源码精读

**功能单元编码**：

[vmacros.sv:7-10](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L7-L10) 定义了 4 种 FU，恰好用满 2 位：

| 宏 | 编码 | 含义 |
|------|------|------|
| `MEM_FU` | `2'b00` | 存储指令（load/store/prefetch），走 `vMU` 岔路 |
| `FP_FU` | `2'b01` | 浮点（当前 ALU 是占位，见 u2-l7） |
| `INT_FU` | `2'b10` | 整数，走 `vEX` 的 `v_int_alu` |
| `FXP_FU` | `2'b11` | 定点（当前 `VECTOR_FXP_ALU=0`，未实现） |

这 2 位就是 `to_vector.fu` / `remapped_v_instr.fu` / `to_vector_exec_info.fu` 字段的取值。它在执行级被用来路由指令：[vex_pipe.sv:124](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L124) 写着 `valid_int_ex1 = valid_i ? (fu_i === `INT_FU) : 1'b0`——只有 `fu` 等于 `` `INT_FU ``（`2'b10`）的指令才会激活整数 ALU。这是 FU 宏真实生效的铁证。

**存储操作编码**：存储指令的 `microop` 字段（7 位）被进一步切分成几个子字段，由若干位段宏定义边界：

[vmacros.sv:13-27](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L13-L27) 定义了存储指令内部的位段：

- `LD_BIT=6`（[vmacros.sv:13](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L13)）：最高位，区分 load(1)/store(0)。
- `MEM_OP_RANGE_HI=5 / LO=4`（[vmacros.sv:15-16](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L15-L16)）：位 [5:4]，寻址模式。
  - `OP_UNIT_STRIDED=2'b00`、`OP_STRIDED=2'b10`、`OP_INDEXED=2'b11`（[vmacros.sv:21-23](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L21-L23)）——连续步长、带 stride、带索引三种寻址（详见 u3-l2）。
- `MEM_SZ_RANGE_HI=3 / LO=2`（[vmacros.sv:18-19](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L18-L19)）：位 [3:2]，元素宽度。
  - `SZ_8=1`、`SZ_16=2`、`SZ_32=0`（[vmacros.sv:25-27](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L25-L27)）——8/16/32 位元素（注意取值不是递增的，`SZ_32=0`）。
- 归约操作码 `RDC_ADD/RDC_AND/RDC_OR/RDC_XOR`（[vmacros.sv:29-33](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L29-L33)）：归约指令（跨 lane 求和/与/或/异或）的具体运算，配合 u2-l8 的归约树。

> 注意一个易错点：[vmacros.sv:32-33](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L32-L33) 里 `RDC_OR` 和 `RDC_XOR` **都被定义成了 `2'b11`**——这看起来像是源码里的一处笔误（OR 和 XOR 用了相同编码）。阅读时要留意：宏定义并非总是完美无缺，遇到可疑处应回到使用它的模块去核对真实语义，而不是盲信宏名。这类细节正是后续 SVA 断言（u4-l6）要守卫的对象。

**转发点宏**：

[vmacros.sv:38-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L38-L44) 定义了执行流水里转发点可选的位置：

| 宏 | 值 | 含义 |
|------|------|------|
| `EX1` | 1 | 第 1 执行级引出（不插寄存器） |
| `EX2` | 2 | 第 2 级 |
| `EX2_F` | 20 | 第 2 级，**flopped**（插一级寄存器） |
| `EX3` | 3 | 第 3 级 |
| `EX3_F` | 30 | 第 3 级，flopped |
| `EX4` | 4 | 第 4 级 |
| `EX4_F` | 40 | 第 4 级，flopped |

文件头注释 `// _F stands for flopped / // non-flopped hurt freq`（[vmacros.sv:36-37](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L36-L37)）已经点明了设计权衡：**不插寄存器（non-flopped）的转发更早可用、解除冒险更快，但组合逻辑路径更长，会拖低主频；插了寄存器（flopped）则相反**。

这两个宏被 `params.sv` 引用：[params.sv:96-97](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L96-L97) 设 `VECTOR_FWD_POINT_A = `EX1`、`VECTOR_FWD_POINT_B = `EX4_F`——即「一个早转发（冒险解除快）+ 一个晚转发（频率友好）」的组合。完整影响见 u4-l2。

#### 4.3.4 代码实践

**实践目标**：亲手组装一条存储指令的 `microop`，体会「位段宏」如何拼出一个完整的操作码。

**操作步骤**：

1. 假设要构造一条 **load、unit-strided、32 位元素** 的向量存储指令。根据上面的位段定义：
   - 位 6（`LD_BIT`）= 1（load）
   - 位 [5:4]（`MEM_OP`）= `OP_UNIT_STRIDED = 2'b00`
   - 位 [3:2]（`MEM_SZ`）= `SZ_32 = 2'b00`（注意是 0）
   - 剩余低位 [1:0] 暂视为 0。
2. 把这 7 位拼起来：`{1'b1, 2'b00, 2'b00, 2'b00} = 7'b1000000`。
3. 打开 `vmu_ld_eng.sv`（u3-l2 会精读），搜索它如何用 `microop[LD_BIT]`、`microop[MEM_OP_RANGE_HI:LO]` 这些宏来拆解字段，验证你的拼装是否正确。

**需要观察的现象**：load/store 引擎是否正是用 `LD_BIT`、`MEM_OP_RANGE`、`MEM_SZ_RANGE` 这些宏来切片 `microop` 的。

**预期结果**：你会看到「一个 7 位 microop 被三个位段宏切成四段语义」的清晰结构，这正是宏存在的价值——避免到处写魔法位号。

**待本地验证**：拼出的 `7'b1000000` 是否与 `sim_generator.py` 为 unit-strided vld 生成的编码一致，需在 u4-l5 里对照生成器输出验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SZ_32=0` 而不是 3？（8/16/32 看起来更自然的顺序是 1/2/3）
**答案**：这是设计选择。可能因为 32 位是「默认/最常用」宽度，赋 0 便于用「全 0 判默认」简化逻辑；也可能是历史编码习惯。无论如何，它提醒我们「宏的取值不能想当然，必须查定义」。

**练习 2**：转发点 `EX4_F` 的值是 `40`，但流水线只有 4 级，`40` 这个数字本身有物理意义吗？
**答案**：没有流水级意义，它只是个「与 `EX4=4` 不冲突、又能区分 flopped 变体」的编码占位。代码用 `==` 比较宏名，关心的是「等于哪个宏」，而不是数值大小。把 flopped 变体设成 `40` 而非 `5`，是为了让人一眼看出它属于 `EX4` 家族。

---

### 4.4 标量侧结构体与标量→向量边界（structs.sv）

#### 4.4.1 概念说明

`structs.sv` 是**标量核**那一侧的结构体定义：取指包、ROB 表项、译码指令、重命名指令、计分板表项……这些描述的都是那个「尚未公开的双发射超标量标量核」。

本仓库仿真时绕过了标量核（由测试台直接喂已译码的向量指令），所以这些结构体在本仓库**大多不直接参与向量仿真**。但其中有一个字段是理解「标量核如何把指令交给向量数据通路」的关键：`decoded_instr.is_vector`。它是标量侧与向量侧的**分界标志**，承接 u1-l1 讲的「指令分流」。

#### 4.4.2 核心流程

标量核里指令的生命周期（设计意图，本仓库未实际跑通）：

1. 取指得到 `fetched_packet`（PC + 32 位指令 + 分支信息）。
2. 译码成 `decoded_instr`，其中 `is_vector` 标志置位表示这是一条向量指令。
3. 标量核在发射阶段检查 `is_vector`：若为真，把指令送进向量指令队列，最终转化成 `to_vector`（即本讲 4.1 的入口结构体）交给向量数据通路。
4. 非向量指令留在标量侧，进入 ROB（`rob_entry`）、重命名（`renamed_instr`）、计分板（`scoreboard_entry`）等结构。

#### 4.4.3 源码精读

**译码指令结构体**：

[structs.sv:79-94](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/structs.sv#L79-L94) 定义了 `decoded_instr`。注意它和向量侧 `to_vector` 的对比：

- 标量侧用 `source1/source2/source3/destination`（各 6 位，6 位是因为标量有更多寄存器），向量侧用 `src1/src2/dst`（各 5 位，对应 32 个向量寄存器）。
- 标量侧 `functional_unit` 是 2 位（和向量侧 `fu` 一样宽，语义也类似）。
- 关键的 `is_vector`（[structs.sv:92](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/structs.sv#L92)）：1 位标志，正是「这条指令要不要送去向量数据通路」的分流开关。

**ROB 表项**（作为标量侧结构的代表）：

[structs.sv:13-27](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/structs.sv#L13-L27) 定义了 `rob_entry`，含 `valid/pending/flushed`、逻辑/物理寄存器号、异常信息等——这是超标量核乱序执行的簿记，本仓库仿真不使用，但能帮你理解「向量侧为什么需要自己的一套 `ticket` 同步」（因为标量侧的 ROB 重排序机制不延伸到向量侧，向量侧用 ticket 替代，详见 u4-l1）。

> **字段宽度的一个对照小结**：标量寄存器号 6 位（`structs.sv` 里 `source1 [5:0]`），向量寄存器号 5 位（`vstructs.sv` 里 `src1 [4:0]`）；标量 ticket 在 `writeback_toARF` 里是 3 位（[structs.sv:57](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/structs.sv#L57)），向量 ticket 在 `remapped_v_instr` 里是 5 位（[vstructs.sv:50](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L50)）。两侧各自独立设计，位宽不同——这也是标量与向量解耦的体现。

#### 4.4.4 代码实践

**实践目标**：定位标量→向量的分流点，建立「两套结构体如何对接」的整体观。

**操作步骤**：

1. 在 [structs.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/structs.sv) 里找到 `decoded_instr` 的 `is_vector` 字段，思考「标量核在发射阶段会怎么用它」。
2. 回到 [vstructs.sv:13-32](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L13-L32) 的 `to_vector`，列出：`decoded_instr` 的哪些字段会「翻译」成 `to_vector` 的哪些字段？（例如 `decoded_instr.functional_unit` → `to_vector.fu`，`decoded_instr.microoperation` → `to_vector.microop`，但位宽要从 5 位/标量语义调整到 7 位/向量语义。）
3. 因为标量核 RTL 未公开，这个翻译逻辑（`decoded_instr` → `to_vector`）在**本仓库里看不到**，它发生在「尚未释出的标量核发射单元」里。明确标注这一点，不要假设它存在于某个文件。

**需要观察的现象**：两侧字段名/位宽的差异。

**预期结果**：你会理解「为什么本仓库用 CSV + 驱动器直接拼 `to_vector`」——因为跳过了标量核，`decoded_instr → to_vector` 这一步由 `sim_generator.py` + `vector_driver.sv` 用软件方式代替了（详见 u1-l5、u4-l5）。

**待本地验证**：无（本仓库不含标量核 RTL，此步为源码阅读型分析）。

#### 4.4.5 小练习与答案

**练习 1**：`decoded_instr` 里有 `functional_unit`（2 位），`to_vector` 里也有 `fu`（2 位）。它们的取值含义相同吗？
**答案**：语义上都是「功能单元选择」，且都用了 `vmacros.sv` 的 FU 编码（`MEM_FU/INT_FU/...`）。但标量侧还会用 `functional_unit` 区分标量自身的 ALU/乘法/访存单元，而向量侧的 `fu` 只在 `MEM/FP/INT/FXP` 四种里选。它们共享同一套宏，但「可选范围」因核而异。

**练习 2**：为什么本仓库仿真不需要 `rob_entry`、`scoreboard_entry` 这些标量结构体？
**答案**：因为仿真绕过了标量核，由测试台直接喂入已译码的向量指令。ROB、标量计分板都是标量核乱序执行的簿记结构，不参与纯向量仿真。它们存在于 `structs.sv` 是为了「将来标量核释出后能完整对接」。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次「完整的指令解码还原」。

**任务**：给定一条汇编语义的向量指令 `vadd v2, v0, v1`（把向量寄存器 v0 和 v1 逐元素相加，结果写回 v2，向量长度假设 `vl = 100`，`maxvl = 256`，不使用掩码），写出它在 **`to_vector`** 结构体里每个字段的取值，并回答两个延伸问题。

**步骤**：

1. **字段填充**（参考 [vstructs.sv:13-32](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L13-L32)）：

   | 字段 | 取值 | 说明 |
   |------|------|------|
   | `valid` | `1'b1` | 是有效指令 |
   | `dst` | `5'd2` | 目标 v2 |
   | `src1` | `5'd0` | 源 v0 |
   | `src2` | `5'd1` | 源 v1 |
   | `data1 / data2` | `32'h0` | 寄存器-寄存器运算，旁路数据不用 |
   | `reconfigure` | `1'b0` | 非重配置 |
   | `immediate` | `32'h0` | VADD 不用立即数 |
   | `fu` | `` `INT_FU `` = `2'b10` | 整数运算（参考 [vmacros.sv:9](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L9)） |
   | `microop` | `VADD` = `7'b0000001` | （参考 [vstructs.sv:125](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L125)） |
   | `use_mask` | `2'b00` | 不使用掩码 |
   | `maxvl` | `9'd256` | 最大向量长度 |
   | `vl` | `9'd100` | 当前向量长度 |

2. **延伸问题 A**：这条指令到了 `vRRM` 之后，`to_vector` 会被转成 `remapped_v_instr`（参考 [vstructs.sv:35-60](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L35-L60)）。新结构体里多出来的 `ticket`、`lock`、`*_iszero` 字段，分别由谁、依据什么填入？（提示：`ticket/lock` 由 `vRRM` 的寄存器重映射与同步机制填，`*_iszero` 由「该寄存器号是否为 0」判定。）

3. **延伸问题 B**：这条指令的 `microop = 7'b0000001` 最终在哪里被消费？请给出文件与行号。（答案：[v_int_alu.sv:116-121](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L116-L121) 的 `case` 第一个分支，执行 `data_a + data_b`。）

**预期结果**：完成上表后，你应当能说清「一条 vadd 从入口字段、到操作码枚举、到 FU 宏、再到 ALU 译码」的完整闭环——这正是本讲三个最小模块（向量指令结构体、操作码枚举、FU/存储操作宏）协同工作的全貌。

## 6. 本讲小结

- `vstructs.sv` 定义了向量数据通路各级之间的「接口形状」：`to_vector`（入口）→ `remapped_v_instr`（重命名后，多出 `ticket/lock/*_iszero`）→ `to_vector_exec(_info)`（执行级，数据与控制分离）→ `vector_mem_req/resp`（存储接口），字段随流水级「逐级累加再精简」。
- `v_int_op_t` 把 30 余条整数助记符绑定到固定 7 位编码，`VADD = 7'b0000001`；这些编码必须与 `v_int_alu` 的 `case` 分支一一对应，本讲用源码实证了这一点。
- `vmacros.sv` 给控制位的取值起名字：FU 编码（`MEM/FP/INT/FXP` = `00/01/10/11`）、存储位段（`LD_BIT`、寻址模式、元素宽度）、转发点（`EX1..EX4` 及 `_F` flopped 变体）；`vex_pipe` 用 `` `INT_FU `` 路由、`params.sv` 用 `` `EX1``/`` `EX4_F`` 配转发点，都是宏真实生效的地方。
- `to_vector.fu`、`to_vector.microop` 等字段的取值直接来自这一整套宏与枚举——结构体定义「形状」，宏与枚举定义「形状里允许填什么值」。
- `vstructs.sv` 用 `DUMMY_VECTOR_LANES`（本地常量）而非 `VECTOR_LANES`，因为它不 `` `include "params.sv" ``；改 lane 数时必须手工同步 `DUMMY_VECTOR_LANES`，这是发生在本文件里的隐藏耦合（承接 u1-l3）。
- `structs.sv` 是标量侧结构体，本仓库仿真大多不用，但其 `decoded_instr.is_vector` 是标量→向量的分流标志；标量核未公开，`decoded_instr → to_vector` 的翻译由测试台/生成器软件代替。

## 7. 下一步学习建议

本讲把「数据的形状」讲清楚了，接下来就该看「数据怎么流动」。建议：

1. **进入单元二 u2-l1（vector_top 顶层数据通路）**：看 `to_vector` 如何进入 `vector_top`，并在 `vRRM → vIS → vEX → vMU` 四级之间用本讲的结构体互联。你会第一次看到这些结构体被「实例化」成真实的连线。
2. **重点对照 u2-l3（vRRM 寄存器重映射）**：观察 `to_vector` 是如何被翻译成 `remapped_v_instr` 的——`ticket` 和 `lock` 字段到底由谁、在何时填入，这是 u4-l1「acquire-release 解耦执行」的前置。
3. **带着本讲的字段表去读 u2-l7（vEX 与 vex_pipe）**：验证 `to_vector_exec_info` 里的 `fu/microop/head_uop/end_uop` 如何驱动单 lane 流水与 FU 路由，把本讲的 `` `INT_FU `` 路由（`vex_pipe.sv:124`）放进完整执行路径理解。
4. **若对操作码验证感兴趣**，可跳读 u4-l6（SVA 断言）：看断言如何利用 `v_int_op_t` 枚举和 FU 编码做「非法 microop」「X 值」检查，把本讲「枚举编码必须合法」的约束变成可执行的验证。
