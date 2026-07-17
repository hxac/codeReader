# Trap 处理与回滚

## 1. 本讲目标

本讲是「虚拟内存、TLB 与异常」单元的收尾篇。前两讲（u7-l1、u7-l2）讲清了**异常如何被检测出来**（TLB 缺失、页缺失、中断挂起等）；本讲回答下一个问题：**异常被检测到之后，硬件如何安全地把控制权交给处理程序，并在处理完后无损地返回原执行点？**

学完本讲你应该能够：

- 说出 `trap_type_t` 的全部异常类型，以及每一类分别在哪里、由什么条件产生。
- 解释为什么 Nyuzi 在「指令可能乱序到达写回级」的前提下仍能做到**精确异常（precise exception）**。
- 描述 `writeback_stage` 作为**唯一回滚仲裁点**的工作流程，以及回滚信号如何逐级刷新流水线、改写取指 PC。
- 读懂 `syscall` 指令如何被解码、如何在写回级被捕获并把系统调用号交给软件，再经 `eret` 返回。

## 2. 前置知识

本讲默认你已掌握以下概念（若不熟请先复习对应讲义）：

- **流水线全景与三条执行路径**（u3-l2）：取指 → 解码 → 线程选择 → 操作数 fetch → {访存 / 整数 / 浮点} → 写回。整数路径 1 级、访存路径 2 级、浮点路径 5 级，长度不等导致指令**可能乱序到达写回**。
- **控制寄存器与 trap 状态栈**（u7-l2）：`control_registers` 用 `trap_state[thread][level]` 三层栈保存 trap 现场（`flags_t`、`trap_pc`、`trap_cause` 等），trap 进入时 push、`eret` 返回时 pop。
- **TLB 与页缺失**（u7-l1）：DTLB 缺失产生 `TT_TLB_MISS`；TLB 命中但 `present=0` 产生 `TT_PAGE_FAULT`。
- **`decoded_instruction_t` 结构**（u4-l2）：解码级把 32 位指令填成这个结构体，其中 `has_trap` / `trap_cause` 字段用于把异常「搭便车」随指令一起流向写回。

两个关键术语先约定：

- **异常 / 陷阱（trap）**：本讲中「异常」「陷阱」「trap」三个词混用，统指一切需要打断正常执行流、跳到处理程序的事件——包括真正的错误（页缺失、非法指令）、同步请求（syscall、breakpoint）以及异步事件（中断）。
- **回滚（rollback）**：把某个线程的取指 PC 强制改写到一个新值，并冲刷掉流水线中比回滚点更年轻的指令。分支跳转、缓存缺失、异常**三种事件**最终都通过同一套回滚机制实现控制流转移。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `hardware/core/writeback_stage.sv` | **本讲主角**。写回级：选择三条路径的结果、写回寄存器，并集中处理所有回滚（分支 / 缺失 / 异常），是整个流水线的唯一回滚仲裁点。 |
| `hardware/core/control_registers.sv` | trap 现场栈的存储与 push/pop；`eret` 返回地址的来源；`CR_SYSCALL_INDEX` 等寄存器的读写。 |
| `hardware/core/defines.svh` | `trap_type_t`（异常编号）、`trap_cause_t`（带 dcache/store 标志的异常包）、`branch_type_t`（含 `BRANCH_ERET`）、`OP_SYSCALL` 操作码的定义。 |
| `hardware/core/dcache_data_stage.sv` | 数据访存的**异常检测源头**之一：判定对齐、页缺失、越权、写权限等 fault，并产出 `dd_trap` / `dd_trap_cause`。 |
| `hardware/core/instruction_decode_stage.sv` | 取指/解码阶段的异常检测源头：非法指令、syscall、breakpoint、中断，以及 ifetch 各类 fault，都汇总成 `has_trap`。 |
| `hardware/core/ifetch_tag_stage.sv` | 回滚信号的**最终落点**：根据 `wb_rollback_pc` 改写每线程的取指 PC。 |
| `tests/core/trap/syscall.S`、`tests/core/mmu/data_page_fault_read.S` | 真实的异常测试程序，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 异常类型：trap_type_t 与 trap_cause_t

#### 4.1.1 概念说明

要统一处理异常，首先要有一张「异常编号表」。Nyuzi 用 4 位编码 `trap_type_t` 列出全部 12 种异常。每种异常都对应一个**产生它的检测点**：

- 取指阶段产生的：`TT_TLB_MISS`（ITLB 缺失）、`TT_PAGE_FAULT`（指令页缺失）、`TT_SUPERVISOR_ACCESS`（越权取指）、`TT_UNALIGNED_ACCESS`（取指地址未对齐）、`TT_NOT_EXECUTABLE`（页不可执行）。
- 解码阶段产生的：`TT_ILLEGAL_INSTRUCTION`（非法指令）、`TT_SYSCALL`（系统调用）、`TT_BREAKPOINT`（断点）、`TT_INTERRUPT`（中断）。
- 数据访存阶段产生的：`TT_TLB_MISS`（DTLB 缺失）、`TT_PAGE_FAULT`（数据页缺失）、`TT_SUPERVISOR_ACCESS`（越权访存）、`TT_UNALIGNED_ACCESS`（数据未对齐）、`TT_ILLEGAL_STORE`（写只读页）、`TT_PRIVILEGED_OP`（用户态执行特权操作）。
- 系统级：`TT_RESET`（复位）。

#### 4.1.2 核心流程

异常「载荷」不只是 4 位的类型号。`trap_cause_t` 在类型号之外还附带两个标志位，让处理程序一眼看清这次异常发生在哪条路径、是读还是写：

```
trap_cause_t = { dcache, store, trap_type[3:0] }
                  1位    1位       4位
```

- `dcache=1` 表示异常来自**数据访存路径**（dcache_data_stage），`dcache=0` 表示来自取指/解码路径（走整数流水线）。
- `store=1` 表示出错的是一次**写操作**（store），`store=0` 表示读或非访存指令。

这两个标志位在汇编层也能读到：`tests/asm_macros.h` 把它们定义成 `TRAP_CAUSE_DCACHE = 0x20`、`TRAP_CAUSE_STORE = 0x10`，软件读 `CR_TRAP_CAUSE` 后按位检测即可区分「读页缺失」还是「写页缺失」。

#### 4.1.3 源码精读

异常类型表定义在 [hardware/core/defines.svh:197-210](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L197-L210)，共 12 种 `trap_type_t`，注意 `TT_RESET` 是默认值（见下文 4.3）：

```systemverilog
typedef enum logic[3:0] {
    TT_RESET                = 4'd0,
    TT_ILLEGAL_INSTRUCTION  = 4'd1,
    TT_PRIVILEGED_OP        = 4'd2,
    TT_INTERRUPT            = 4'd3,
    TT_SYSCALL              = 4'd4,
    TT_UNALIGNED_ACCESS     = 4'd5,
    TT_PAGE_FAULT           = 4'd6,
    TT_TLB_MISS             = 4'd7,
    TT_ILLEGAL_STORE        = 4'd8,
    TT_SUPERVISOR_ACCESS    = 4'd9,
    TT_NOT_EXECUTABLE       = 4'd10,
    TT_BREAKPOINT           = 4'd11
} trap_type_t;
```

异常载荷结构 `trap_cause_t` 定义在 [hardware/core/defines.svh:239-243](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L239-L243)：

```systemverilog
typedef struct packed {
    logic dcache;
    logic store;
    trap_type_t trap_type;
} trap_cause_t;
```

数据访存路径如何填这两个标志位，见 [hardware/core/dcache_data_stage.sv:545-570](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L545-L570)。注意它把 `dcache` 位恒置 1，并按 `fault_store_flag`（当前指令是否为写）填 `store` 位：

```systemverilog
if (tlb_miss)
    dd_trap_cause <= {1'b1, fault_store_flag, TT_TLB_MISS};
else if (page_fault)
    dd_trap_cause <= {1'b1, fault_store_flag, TT_PAGE_FAULT};
...
else // write fault
    dd_trap_cause <= {2'b11, TT_ILLEGAL_STORE};
```

最后那行 `2'b11` 正好对应「写 + dcache」——这就是 `TT_ILLEGAL_STORE` 永远只可能由写操作触发的原因。

#### 4.1.4 代码实践

**实践目标**：把异常类型表与汇编层的常量对上号。

**操作步骤**：

1. 打开 `hardware/core/defines.svh` 的 `trap_type_t`，记录每个枚举值的数字。
2. 打开 `tests/asm_macros.h:53-68`，对比汇编层的 `TT_*` 宏定义。
3. 打开 `tests/core/mmu/data_page_fault_read.S:25`，看它期望的 cause 是 `TT_PAGE_FAULT | TRAP_CAUSE_DCACHE`。

**需要观察的现象**：三处的数值应当完全一致——硬件枚举、汇编宏、测试期望值用的是同一套编码，这是「硬件 / 模拟器 / 软件 / 测试」四方共享 ISA 编码原则（u2-l1 已建立）的又一体现。

**预期结果**：`TT_PAGE_FAULT = 6`、`TRAP_CAUSE_DCACHE = 0x20`，所以读页缺失时 `CR_TRAP_CAUSE` 的值是 `0x26`；若是写页缺失则还会 OR 上 `0x10` 变成 `0x36`。

#### 4.1.5 小练习与答案

**练习 1**：一次对只读页的 **store** 触发了什么异常？`trap_cause_t` 的三个字段分别是什么？
**答案**：触发 `TT_ILLEGAL_STORE`（值 8），`dcache=1`、`store=1`、`trap_type=TT_ILLEGAL_STORE`，整体打包值为 `2'b11_1000`。注意它不是 `TT_PAGE_FAULT`——页是 present 的，只是不可写。

**练习 2**：用户态程序执行 `eret` 会触发哪种异常？在哪里检测？
**答案**：触发 `TT_PRIVILEGED_OP`。检测点在整数执行级（`int_execute_stage.sv` 的 `privileged_op_fault = eret && !cr_supervisor_en`），随后在写回级被翻译成该 trap 类型（详见 4.3）。

---

### 4.2 精确异常：搭便车 + 单点仲裁

#### 4.2.1 概念说明

**精确异常**是一个严格的架构约定：当异常发生时，故障指令之前的所有指令都已完整执行（副作用全部生效），故障指令本身及其之后的所有指令都像**从未执行过**一样（没有任何副作用），并且处理程序能拿到一个清晰的「异常 PC」——即故障指令的地址。

这条约定看起来理所当然，在 Nyuzi 里却很棘手，原因写在 [hardware/core/writeback_stage.sv:33-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L33-L39) 的模块注释里：整数路径 1 级、访存 2 级、浮点 5 级，**指令会乱序到达写回级**；而且回滚发生后，比回滚点更早、但还在长流水线（浮点）里的同线程指令，之后仍会抵达写回级。

Nyuzi 用两个设计联手解决它：

1. **异常搭便车（piggyback）**：异常不是当场拉响警报，而是在检测点被打包进指令的 `has_trap` / `trap_cause` 字段，**随指令一起沿流水线前进**，只有当这条指令真正走到写回级时才被「引爆」。
2. **副作用门控**：一条带 `has_trap` 的指令，其全部寄存器读写和访存副作用在更早的阶段就被抑制，所以它一路上「什么都没做」，只把异常运到写回。

#### 4.2.2 核心流程

精确异常的完整生命周期：

```
检测点(has_trap=1, trap_cause=...)
   │  异常随指令流动，副作用沿途被抑制
   ▼
线程选择 → 操作数fetch(不读寄存器) → 执行(不产生有效结果)
   │
   ▼
写回级：wb_trap=1, wb_rollback_en=1
   ├─ 抑制本指令的写回（writeback_en 需要 !wb_rollback_en）
   ├─ 把取指 PC 改写为 trap handler 地址（回滚）
   ├─ 向 control_registers push trap 现场（进特权态、关中断）
   └─ 冲刷掉比本指令更年轻的、还在前级流水线的指令
```

为什么这样就是精确的？因为：

- 故障指令的副作用被门控掉（store 在 dcache 用 `!any_fault` 拦截，寄存器写在写回用 `!wb_rollback_en` 拦截），所以「故障指令及之后」无副作用。
- 比故障指令**更老**的指令（更小的 PC）即使因长流水线晚到写回，也允许正常退休——它们本来就排在前面，退休它们符合「之前的指令都已完成」。
- 比故障指令**更年轻**的指令（已被取指/解码但还没到写回）会被回滚信号冲刷（见 4.3）。

#### 4.2.3 源码精读

**① 解码级把异常搭便车，并抑制副作用。** `has_trap` 的产生见 [hardware/core/instruction_decode_stage.sv:237-241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L237-L241)：

```systemverilog
assign has_trap = (ifd_instruction_valid
    && (dlut_out.illegal || syscall || breakpoint || raise_interrupt))
    || ifd_alignment_fault || ifd_tlb_miss
    || ifd_supervisor_fault
    || ifd_page_fault || ifd_executable_fault;
```

随后，所有源操作数读取都被 `!has_trap` 闸住。例如 [hardware/core/instruction_decode_stage.sv:293-294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L293-L294)：

```systemverilog
assign decoded_instr_nxt.has_scalar1 = dlut_out.scalar1_loc != SCLR1_NONE && !nop
    && !has_trap && !unary_arith;
```

`has_scalar2`、`has_vector1`、`has_vector2`、`has_dest` 等字段同样带 `!has_trap`。于是一条带 trap 的指令既不读也不写任何寄存器，纯粹是一辆「运异常的货车」。

**② 中断的精确性靠「指令替换」。** 当一个挂起且使能的中断要发生时，解码级**不是**在任意时刻打断，而是把当前指令替换成一个只携带 PC 与 `TT_INTERRUPT` 的空壳指令（[hardware/core/instruction_decode_stage.sv:248-249](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L248-L249)），并刻意不在「两段式指令」（IO 访问、同步访存）的中途插入（u7-l2 已详述）。替换后的空壳走整数流水线到写回，副作用天然为零。

**③ 数据访存的副作用在 dcache 当场拦截。** 对一次有 fault 的 store，写使能被 `!any_fault` 关掉，见 [hardware/core/dcache_data_stage.sv:291-293](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L291-L293)：

```systemverilog
assign dd_store_en = cached_store_req && !tlb_miss && !any_fault;
```

`any_fault` 的定义见 [hardware/core/dcache_data_stage.sv:284-285](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L284-L285)，是对齐、越权、页缺失、写权限 fault 的并集。这意味着：**故障访存的副作用在离 CPU 最近的 dcache 就被拦下，绝不会真正写进缓存或内存**，然后 `dd_trap` 才带着 cause 走向写回。

**④ 写回级抑制故障指令的写回。** 三条路径的写回使能都要 `&& !wb_rollback_en`，例如浮点路径 [hardware/core/writeback_stage.sv:364](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L364)、整数路径 [hardware/core/writeback_stage.sv:391](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L391)、访存路径 [hardware/core/writeback_stage.sv:415](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L415)。异常会同时拉起 `wb_rollback_en`，于是写回被一并取消。

#### 4.2.4 代码实践

**实践目标**：验证「故障指令不产生任何写回」这一精确性保证。

**操作步骤**：

1. 阅读 `tests/core/mmu/mmu_test_common.h:67-82` 的 `mmu_fault_test` 宏。它在触发 fault 前故意把目标寄存器装上一个已知值 `0x12345678`。
2. 找到第 82 行的断言 `assert_reg s15, 0x12345678  // Ensure dest reg wasn't modified`。
3. 思考：这条 load 指令的目标寄存器是 `s15`，若精确异常不成立（即 fault 指令仍写了 `s15`），这个断言会怎样？

**需要观察的现象**：处理程序读到 `s15` 仍然是 `0x12345678`，说明那条会 fault 的 load 指令**没有写回任何值**。

**预期结果**：测试通过（`PASS`），证明 fault 指令的写回被 `!wb_rollback_en` 正确抑制。若想亲眼运行，可在仓库根目录 `cmake . && make` 后执行（具体命令以本机环境为准，待本地验证）：

```bash
cd tests/core/mmu && python3 runtest.py    # 运行该目录下的 mmu 测试
```

#### 4.2.5 小练习与答案

**练习 1**：为什么异常不在检测点（如 dcache）直接跳到 handler，而要「搭便车」走到写回级？
**答案**：因为指令乱序到达写回。若在 dcache 当场跳转，就无法保证「故障指令之前的指令都已退休」——一条更老但还在浮点流水线里的指令可能还没到写回。搭便车让异常跟随指令按到达顺序在**唯一的写回点**引爆，自然满足「之前都完成、之后都未执行」的精确性。

**练习 2**：模块注释（writeback_stage.sv:38-39）说「回滚信号不会刷新多周期流水线的后级」，这为什么不会破坏精确性？
**答案**：因为那些没被刷新、继续流向写回的「漏网」指令都是比回滚点**更老**的指令（它们更早发射、只是路径更长）。让它们正常退休恰好符合「故障指令之前的指令都已完成」。若刷新它们反而错了。

---

### 4.3 回滚机制：writeback 统一处理与流水线刷新

#### 4.3.1 概念说明

回滚是 Nyuzi 控制流转移的统一底座。分支跳转、缓存缺失、异常这三种看似不同的事件，最终都归结为同一件事：**改写某线程的取指 PC，并冲刷掉更年轻的指令**。

设计上，`writeback_stage` 是**全流水线唯一的回滚仲裁点**。所有回滚请求都在这里汇总、按固定优先级选出**至多一个**去执行。这样做的好处写在 [hardware/core/writeback_stage.sv:26-32](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L26-L32)：避免在「同一周期可能有多个回滚」时去协调冲突。而且回滚信号是**组合逻辑（不寄存）**输出的——因为下一条指令可能是一条 store，必须在它施加副作用之前就把它压住。

#### 4.3.2 核心流程

`writeback_stage` 的回滚仲裁按如下优先级（见 [hardware/core/writeback_stage.sv:163-241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L163-L241)），排在前面的优先：

| 优先级 | 触发条件 | 回滚目标 PC | 是否 trap | 说明 |
|--------|----------|-------------|-----------|------|
| 1 | 整数路径 `has_trap` 或 `privileged_op_fault` | handler（TLB miss 走 `cr_tlb_miss_handler`，其余走 `cr_trap_handler`） | 是 | 解码级 fault、syscall、中断、非法指令等 |
| 2 | 访存路径 `dd_trap` | handler（同上分流） | 是 | 数据访存 fault |
| 3 | 整数路径分支 `ix_rollback_en` | `ix_rollback_pc`（分支目标 / `eret` 返回地址） | 否 | 普通分支；`eret` 另置 `wb_eret=1` |
| 4 | 访存路径 `dd_rollback_en` / `sq_rollback_en` / `ior_rollback_en` | `dd_rollback_pc`（重取本指令） | 否 | 缓存缺失、store 队列满、IO 请求 |

仲裁输出后，回滚信号 `wb_rollback_en / wb_rollback_thread_idx / wb_rollback_pc / wb_rollback_pipeline` 广播给**所有流水级**，每一级各自检查「这次回滚是不是针对我手里的指令」并做出反应：

- **取指级**（`ifetch_tag_stage`）：把该线程的 PC 改成 `wb_rollback_pc`——这是回滚的最终落点。
- **解码级、线程选择级、dcache**：把自己手里属于该线程、且比回滚点年轻的指令标记为「冲刷（squash）」，不再让它前进或施加副作用。

#### 4.3.3 源码精读

**① 仲裁主体。** 整个回滚仲裁是一个 `always_comb` 块，[hardware/core/writeback_stage.sv:163-241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L163-L241)。默认值把 `wb_trap_cause` 设成 `{2'b0, TT_RESET}`——这就是 `TT_RESET` 充当「无异常」默认值的由来。最高优先级是整数路径的异常/特权 fault（[hardware/core/writeback_stage.sv:177-199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L177-L199)）：

```systemverilog
if (ix_instruction_valid && (ix_instruction.has_trap
    || ix_privileged_op_fault))
begin
    wb_rollback_en = 1;
    if (ix_instruction.trap_cause.trap_type == TT_TLB_MISS)
        wb_rollback_pc = cr_tlb_miss_handler;   // TLB miss 有独立入口
    else
        wb_rollback_pc = cr_trap_handler;       // 其余异常走通用入口

    wb_rollback_thread_idx = ix_thread_idx;
    wb_rollback_pipeline = PIPE_INT_ARITH;
    wb_trap = 1;
    if (ix_privileged_op_fault)
        wb_trap_cause = {2'b0, TT_PRIVILEGED_OP};   // 特权违规统一翻成此类型
    else
        wb_trap_cause = ix_instruction.trap_cause;  // 沿用解码级打包的 cause
    ...
end
```

两个细节值得注意：第一，**TLB miss 有独立的处理入口** `cr_tlb_miss_handler`（区别于通用 `cr_trap_handler`），因为 TLB miss 极度频繁且需要软件查页表，单独入口省去分发开销；第二，整数执行级检测到的 `eret` 越权（`ix_privileged_op_fault`）在写回级被统一翻译成 `TT_PRIVILEGED_OP`。

**② 访存异常分支。** 数据访存 fault 在第二优先级处理（[hardware/core/writeback_stage.sv:200-215](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L200-L215)）。注意它把 `wb_trap_access_vaddr` 设为 `dd_request_vaddr`——即**引发 fault 的虚拟地址**，软件据此知道是哪个地址出了问题：

```systemverilog
else if (dd_instruction_valid && dd_trap)
begin
    wb_rollback_en = 1'b1;
    if (dd_trap_cause.trap_type == TT_TLB_MISS)
        wb_rollback_pc = cr_tlb_miss_handler;
    else
        wb_rollback_pc = cr_trap_handler;
    wb_rollback_thread_idx = dd_thread_idx;
    wb_rollback_pipeline = PIPE_MEM;
    wb_trap = 1;
    wb_trap_cause = dd_trap_cause;
    wb_trap_pc = dd_instruction.pc;
    wb_trap_access_vaddr = dd_request_vaddr;   // 故障虚拟地址
end
```

**③ 分支与 eret。** 非异常的分支回滚在第三优先级（[hardware/core/writeback_stage.sv:216-230](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L216-L230)）。`eret` 也是一种分支（`BRANCH_ERET`），但它在拉起回滚的同时另置 `wb_eret=1`，并从 `cr_eret_subcycle` 取回子周期状态：

```systemverilog
if (ix_instruction.branch_type == BRANCH_ERET)
begin
    wb_eret = 1;
    wb_rollback_subcycle = cr_eret_subcycle[ix_thread_idx];
end
```

`wb_eret` 送到 `control_registers` 触发 trap 现场栈的 pop（见 4.4）。

**④ 取指级接收回滚，改写 PC。** 回滚信号的最终效果在 [hardware/core/ifetch_tag_stage.sv:155-167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L155-L167)，PC 更新按「复位 > 回滚 > 缺失回退 > 正常 +4」四级优先级：

```systemverilog
if (reset)
    next_program_counter[thread_idx] <= RESET_PC;
else if (wb_rollback_en && wb_rollback_thread_idx == local_thread_idx_t'(thread_idx))
    next_program_counter[thread_idx] <= wb_rollback_pc;   // 回滚盖写
else if ((ifd_cache_miss || ifd_near_miss) && ...)
    next_program_counter[thread_idx] <= next_program_counter[thread_idx] - 4;
else if (selected_thread_oh[thread_idx] && cache_fetch_en)
    next_program_counter[thread_idx] <= next_program_counter[thread_idx] + 4;
```

**⑤ trap 现场的 push。** `wb_trap` 一路送到 `control_registers`，在那里把当前现场压入栈顶并切到特权态（[hardware/core/control_registers.sv:161-177](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L161-L177)）：

```systemverilog
if (wb_trap)
begin
    // 整个栈下移一层（push）
    for (int level = 0; level < TRAP_LEVELS - 1; level++)
        trap_state[wb_rollback_thread_idx][level + 1]
            <= trap_state[wb_rollback_thread_idx][level];

    // 在栈顶记下本次 trap 的全部信息
    trap_state[wb_rollback_thread_idx][0].trap_cause <= wb_trap_cause;
    trap_state[wb_rollback_thread_idx][0].trap_pc <= wb_trap_pc;
    trap_state[wb_rollback_thread_idx][0].trap_access_addr <= wb_trap_access_vaddr;
    trap_state[wb_rollback_thread_idx][0].syscall_index <= wb_syscall_index;
    trap_state[wb_rollback_thread_idx][0].flags.interrupt_en <= 0;  // 进 trap 关中断
    trap_state[wb_rollback_thread_idx][0].flags.supervisor_en <= 1; // 进 trap 进特权态
    if (wb_trap_cause.trap_type == TT_TLB_MISS)
        trap_state[wb_rollback_thread_idx][0].flags.mmu_en <= 0;    // TLB miss 关 MMU
end
```

这里 `TRAP_LEVELS = 3`（[hardware/core/control_registers.sv:88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L88)），所以最多嵌套 2 层 trap。

**⑥ 无 handler 时的保护。** 若触发了 trap 却没有设置 handler 地址，回滚 PC 会是 0，仿真里直接报错退出，见 [hardware/core/writeback_stage.sv:535-547](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L535-L547)。

#### 4.3.4 代码实践

**实践目标**：跟踪一次数据 `TT_PAGE_FAULT` 从 dcache 到 writeback、再到取指的全链路（本讲核心实践）。

**操作步骤**：

1. 阅读 `tests/core/mmu/data_page_fault_read.S`。它通过 `mmu_fault_test` 宏，在 `0x00002000`（一张 present 位没置的 DTLB 表项）执行 `load_32`，期望触发 `TT_PAGE_FAULT | TRAP_CAUSE_DCACHE`。
2. 用源码追踪这条 load 指令的信号旅程，填下面的链路表：

| 阶段 | 信号 | 取值 | 出处（文件:行） |
|------|------|------|-----------------|
| dcache 检测 fault | `page_fault` | 1（tlb_hit & !present） | `dcache_data_stage.sv:271-273` |
| dcache 组装 cause | `dd_trap_cause` | `{1'b1, 0, TT_PAGE_FAULT}` | `dcache_data_stage.sv:560-561` |
| dcache 拉响 trap | `dd_trap` | 1 | `dcache_data_stage.sv:611` |
| 写回识别访存 trap | `wb_trap` / `wb_rollback_en` | 1 | `writeback_stage.sv:200-215` |
| 写回选 handler | `wb_rollback_pc` | `cr_trap_handler` | `writeback_stage.sv:206-207` |
| 取指改写 PC | `next_program_counter` | `wb_rollback_pc` | `ifetch_tag_stage.sv:159-160` |
| trap 现场入栈 | `trap_state[][0]` | cause/pc/addr 写入 | `control_registers.sv:161-177` |

3. 追踪 `wb_trap_access_vaddr`：它在写回级被赋为 `dd_request_vaddr`，随后存进 `trap_state.trap_access_addr`，软件用 `getcr CR_TRAP_ADDRESS` 读出来，正是 `0x00002000`（测试在第 76-77 行断言 `assert_reg s0, \address`）。

**需要观察的现象**：触发 fault 后，目标寄存器 `s15` 仍是预设的 `0x12345678`（写回被抑制）；处理程序能读到正确的 cause、fault PC、fault 地址；处理完后能返回到 `fault_loc` 之后的正确位置。

**预期结果**：测试输出 `PASS`。完整运行（待本地验证）：

```bash
cd tests/core/mmu && python3 runtest.py
```

#### 4.3.5 小练习与答案

**练习 1**：为什么回滚信号用组合逻辑（`always_comb`）输出，而不是寄存一拍？
**答案**：因为紧随其后的一条指令可能是 store，必须在本周期就把它压住，否则下一周期它的写副作用就已经生效了，破坏精确性。寄存一拍来不及拦截。

**练习 2**：`TT_TLB_MISS` 与其他异常在回滚目标上有何不同？为什么？
**答案**：TLB miss 回滚到 `cr_tlb_miss_handler`，其他异常回滚到通用的 `cr_trap_handler`。因为 TLB miss 极其频繁且处理逻辑固定（软件查页表、插表项、返回重试），给它独立入口省去在通用 handler 里再分发的开销。此外，进 TLB miss 时还会额外关掉 MMU（`flags.mmu_en <= 0`），让 handler 自身可用恒等映射运行。

**练习 3**：如果一个核有 4 个硬件线程，线程 2 触发了 trap，线程 0/1/3 会受影响吗？
**答案**：不会。回滚信号都带 `wb_rollback_thread_idx`，各级都按线程号过滤（如取指级 `wb_rollback_thread_idx == thread_idx` 才改 PC）。其他线程照常执行——这正是多线程隐藏延迟的优势：一个线程陷进 trap handler，流水线仍可被其他线程利用。

---

### 4.4 syscall 派发与 eret 返回

#### 4.4.1 概念说明

syscall（系统调用）是**用户态主动请求内核服务**的同步异常。它和页缺失这类「被动错误」走的是**完全相同**的异常通道——这体现了 RISC 风格的设计哲学：不设专门的 syscall 进入机制，而是把它当作一种 trap，复用整套精确异常与回滚基础设施。

`eret`（exception return）则是 trap 的「出口」指令：它从 trap 现场栈顶弹出上一层状态，把控制权交还给被打断的代码。`eret` 本质上是一条**特权分支**——只有 supervisor 能执行，且它的目标地址是「栈顶保存的 trap PC」。

#### 4.4.2 核心流程

一次 syscall 的完整旅程：

```
用户态执行 syscall N
   │  解码级识别 OP_SYSCALL，打包 {TT_SYSCALL} 并把 N 放进 immediate
   ▼  has_trap=1，随指令走整数流水线（副作用全被抑制）
写回级：
   ├─ wb_trap=1, wb_trap_cause={0,0,TT_SYSCALL}
   ├─ wb_syscall_index = immediate_value = N   （第 243 行）
   └─ 回滚到 cr_trap_handler
control_registers：把 syscall_index 连同 cause/pc 一起压入 trap_state[][0]
   ▼
内核 trap handler 运行：
   ├─ getcr CR_TRAP_CAUSE  → 读到 TT_SYSCALL
   ├─ getcr CR_SYSCALL_INDEX → 读到 N，据此分发到具体系统调用
   └─ 处理完毕后执行 eret
eret：trap 现场栈 pop，PC 回到 trap_pc（syscall 的下一条指令）
```

注意一个反直觉点：**syscall 的索引号（系统调用号）不是走专用的控制寄存器输入端**，而是搭在指令的立即数字段里，由写回级现取现用。

#### 4.4.3 源码精读

**① syscall 的识别。** 在解码级，`syscall` 被识别为「立即数格式且操作码是 `OP_SYSCALL`」（[hardware/core/instruction_decode_stage.sv:234](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L234)）：

```systemverilog
assign syscall = fmt_i && 6'(ifd_instruction[28:24]) == OP_SYSCALL;
```

它随即被纳入 `has_trap`（4.2 已见），并在 cause 仲裁里产出 `TT_SYSCALL`（[hardware/core/instruction_decode_stage.sv:262-263](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L262-L263)）。系统调用号 N 就放在这条指令的立即数字段里，一路原样带到写回。

**② 写回级取出系统调用号。** 在 [hardware/core/writeback_stage.sv:243](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L243)，系统调用号从整数路径指令的立即数里取出，转成 `wb_syscall_index`：

```systemverilog
assign wb_syscall_index = syscall_index_t'(ix_instruction.immediate_value);
```

`syscall_index_t` 是 15 位（[hardware/core/defines.svh:53](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L53)），所以系统调用号最大到 32767。

**③ 存入 trap 现场并供软件读取。** `wb_syscall_index` 在 push 时被写进 `trap_state[thread][0].syscall_index`（[hardware/core/control_registers.sv:171](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L171)）。内核 handler 用 `getcr CR_SYSCALL_INDEX` 读它（[hardware/core/control_registers.sv:303](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L303)）：

```systemverilog
CR_SYSCALL_INDEX: cr_creg_read_val <= scalar_t'(trap_state[dt_thread_idx][0].syscall_index);
```

**④ eret 的返回。** `eret` 在写回级走分支回滚分支（4.3 已见 `wb_eret=1`）。`wb_eret` 送到 `control_registers` 触发栈的 pop（[hardware/core/control_registers.sv:179-184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L179-L184)）：

```systemverilog
if (wb_eret)
begin
    // 整个栈上移一层（pop），恢复上一层 flags
    for (int level = 0; level < TRAP_LEVELS - 1; level++)
        trap_state[wb_rollback_thread_idx][level]
            <= trap_state[wb_rollback_thread_idx][level + 1];
end
```

pop 之后，第 0 层恢复成上一层（被 trap 打断时）的 `flags_t`——于是中断使能、MMU 使能、特权态都自动还原。eret 的返回地址来自 `cr_eret_address`，它就是栈顶的 `trap_pc`（[hardware/core/control_registers.sv:247](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L247)）。对 syscall 而言，这个 `trap_pc` 是 syscall 指令本身的地址，由内核决定返回到 syscall 之后（通常 handler 会把保存的 PC 加 4 再 eret，或在进入时调整）。

#### 4.4.4 代码实践

**实践目标**：用一个真实测试看清 syscall 的派发与返回。

**操作步骤**：

1. 打开 `tests/core/trap/syscall.S`。它先 `setcr` 设好 `CR_TRAP_HANDLER`，再 `setcr s0, CR_FLAGS` 切到用户态（`s0=0`），然后执行 `syscall 7`。
2. 在 `handle_fault` 里逐条对照断言：
   - `assert_reg s0, TT_SYSCALL`（第 39 行）：cause 是系统调用。
   - `assert_reg s0, FLAG_SUPERVISOR_EN`（第 41 行）：进 trap 后自动切到了特权态。
   - `assert_reg s0, 0`（第 43 行，读 `CR_SAVED_FLAGS`）：被打断时是用户态（flags=0）。
   - `assert_reg s0, 7`（第 45 行，读 `CR_SYSCALL_INDEX`）：系统调用号正确传到了软件。
   - `assert_reg` 检查 `CR_TRAP_PC == fault_loc`（第 47-50 行）：异常 PC 指向 syscall 指令本身。
3. 想清楚为什么测试在用户态下「主动」用 syscall 触发 trap 后，能在 handler 里读到 `FLAG_SUPERVISOR_EN`——这正是 `control_registers` 在 push 时把 `supervisor_en` 置 1 的效果。

**需要观察的现象**：handler 里读到的 `CR_FLAGS` 是 `FLAG_SUPERVISOR_EN`，而 `CR_SAVED_FLAGS` 是 0，证明 trap 进入时**当前层切到特权态、被打断的层原样保存在第 1 层**。

**预期结果**：测试输出 `PASS`。运行方式（待本地验证）：

```bash
cd tests/core/trap && python3 runtest.py
```

该目录的 `runtest.py` 通过 `register_generic_assembly_tests` 把 `syscall.S` 等一批 .S 文件注册成在 `emulator`、`verilator`、`fpga` 三个目标上跑（见 `tests/core/trap/runtest.py:60-72`）。

#### 4.4.5 小练习与答案

**练习 1**：系统调用号为什么放在指令立即数里，而不是放进某个通用寄存器再用专门指令传？
**答案**：为了复用现有的立即数解码通路与 `wb_syscall_index` 的现成提取逻辑（`ix_instruction.immediate_value`），不必增加新的硬件路径。代价是系统调用号宽度受限于立即数字段（15 位），但对实际需求足够。

**练习 2**：用户态直接执行 `eret` 会怎样？
**答案**：整数执行级检测到 `eret && !cr_supervisor_en`，拉起 `ix_privileged_op_fault`；到写回级被翻译成 `TT_PRIVILEGED_OP` trap（不会真正弹出栈或返回）。`tests/core/trap/eret_non_super.S` 正是验证这一点。

**练习 3**：`syscall` 和中断都走「解码级替换/打包 + 写回引爆」的同一条路，它们在 cause 仲裁上有何区别？
**答案**：中断（`raise_interrupt`）在 cause 仲裁里优先级**最高**（`instruction_decode_stage.sv:248-249`），且只在指令边界、非两段式指令中段发生；syscall 是普通指令，只要解码出 `OP_SYSCALL` 就置 `has_trap`，优先级低于各种 fault。两者最终都成为 `has_trap=1` 的指令搭便车到写回。

---

## 5. 综合实践

把本讲四条主线串起来，做一个「**手工模拟一次 syscall 的完整生命周期**」的源码阅读任务。请按顺序回答并给出对应的源码行号：

1. **解码**：用户程序执行 `syscall 5`。在 `instruction_decode_stage.sv` 找到识别它的那一行，以及把它转成 `TT_SYSCALL` 并塞进 `has_trap` 的那一行。
2. **搭载**：这条指令的寄存器读/写在解码级被哪些 `!has_trap` 闸住？结果它一路上读了几个寄存器、写了几个寄存器？
3. **引爆**：它到达写回级后，触发 [writeback_stage.sv:177-199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L177-L199) 的哪一段？`wb_trap`、`wb_rollback_en`、`wb_rollback_pc`、`wb_syscall_index` 各是什么值？
4. **入栈**：在 `control_registers.sv` 找到 push 的代码，列出哪些字段被写进 `trap_state[thread][0]`、`flags` 三个位各变成什么。
5. **派发**：内核 handler 用哪两条 `getcr` 分别读到「这是 syscall」和「调用号是 5」？
6. **返回**：handler 执行 `eret`。在写回级找到 `wb_eret=1` 的设置，在 `control_registers.sv` 找到 pop 的代码，说明 `flags` 如何恢复、PC 如何回到原执行点。

完成后，你应当能用一句话说清：**syscall 不过是一种「由软件主动触发的精确异常」，它完全复用了为错误异常搭建的检测—搭载—引爆—入栈—回滚—返回 这条高速公路。** 这正是 Nyuzi 异常机制设计的核心洞察。

## 6. 本讲小结

- Nyuzi 用 4 位的 `trap_type_t` 列出 12 种异常，再用 `trap_cause_t` 额外携带 `dcache`、`store` 两个标志位，让软件能区分异常来自取指还是访存、是读还是写。
- **精确异常**靠两件事保证：异常在检测点「搭便车」随指令流动、副作用沿途被 `!has_trap` / `!any_fault` / `!wb_rollback_en` 三道闸门抑制；最终在唯一的写回级引爆。
- `writeback_stage` 是**全流水线唯一的回滚仲裁点**，按「整数异常 > 访存异常 > 分支/eret > 缺失/IO」四级优先级选出至多一个回滚，组合输出，回滚信号广播给所有级，最终在 `ifetch_tag_stage` 改写 PC。
- trap 进入时 `control_registers` 把现场 push 进 3 层栈（关中断、进特权态，TLB miss 还关 MMU）；`eret` 是特权分支，触发 pop 恢复 flags、回到 `trap_pc`。
- **syscall 完全复用异常通道**：解码识别 `OP_SYSCALL` → 搭 `TT_SYSCALL` 便车 → 写回取出立即数作 `wb_syscall_index` → 入栈 → 内核读 `CR_SYSCALL_INDEX` 派发 → `eret` 返回。

## 7. 下一步学习建议

本讲把硬件侧的异常机制讲完了。接下来的自然走向是**软件侧如何使用这套机制**：

- **u12-l1 内核启动与陷阱/系统调用**：阅读 `software/kernel/trap_entry.S` 与 `syscall.c`，看真实的内核 trap 入口如何保存现场、读 `CR_TRAP_CAUSE` 分发、处理完 syscall 后 `eret` 返回。那里会把本讲的硬件契约落到 C/汇编代码上。
- **u10-l1 同步内存操作 LL/SC**：本讲提到了 `MEM_SYNC` 访存与「两段式指令不打断中断」，想深入原子操作与内存序的读者可继续。
- **u11-l1 片上调试器与 JTAG**：调试器通过指令注入读写寄存器/内存，注入指令的副作用与回滚处理同样依赖本讲的写回机制（`wb_inst_injected` 信号）。

建议读者把本讲与 u7-l1（TLB）、u7-l2（控制寄存器/中断）连起来读，三者构成 Nyuzi「虚拟内存与异常」的完整图景。
