# 分支调用与控制寄存器

## 1. 本讲目标

本讲是 Nyuzi 指令集（ISA）系列的第四讲，专门讲解「程序如何改变执行顺序」和「如何读写处理器内部状态」。学完后你应当能够：

- 说出 Nyuzi 七种分支/调用指令（`branch_type_t`）各自的语义，并理解条件分支为什么不放在解码阶段。
- 理解 `getcr` / `setcr` 的本质是「控制寄存器型访存指令」（`MEM_CONTROL_REG`），知道它为何要走特权检查而非普通缓存。
- 建立 28 个控制寄存器（`control_register_t`）的编号地图，重点掌握线程号、trap 状态、标志位、ASID/页目录、中断、性能计数这几组的用途。
- 看懂一次中断从「挂起」到「精确替换指令」再到「eret 返回」的全过程。

本讲只讲「分支调用」「控制寄存器」「中断相关寄存器」三个最小模块。具体的 trap 回滚流水线细节、虚拟内存/TLB 细节、性能计数剖析流程会在后续 u7（虚拟内存与异常）、u11（调试与性能）中展开。

## 2. 前置知识

在开始前，请确认你已经理解 u2-l1 建立的几个概念：

- **指令格式**：Nyuzi 指令是 32 位定长，按最高几位特征码分为 R / I / M / C / B 五大格式。本讲的分支指令属于 **B 格式**（`instruction[31:28] == 4'b1111`），控制寄存器访问属于 **M 格式**（`MEM_CONTROL_REG`）。
- **没有条件码（flags）寄存器**：Nyuzi 不像 x86/ARM 那样有一条独立的标志寄存器。比较指令（u2-l2 讲过的 `cmpeq_i` 等）直接把 0/1 写回一个普通标量寄存器；条件分支再去「测试这个寄存器是否为 0」。这一点是理解 Nyuzi 控制流的关键。
- **标量寄存器 s31 是返回地址 RA**：`REG_RA = 31`，调用指令 `call` 默认把返回地址写进 s31。
- **流水线总览**（u3-l2）：指令依次经过取指 → 解码 → 线程选择 → 操作数 fetch → 执行（整数/浮点/访存）→ 写回。分支在「整数执行」阶段才被解析。

此外，本讲会用到三个关键源文件，它们在硬件、模拟器、汇编三个层面定义了同一套编码（这是协同仿真的基础，见 u8-l3）：

| 层面 | 文件 | 作用 |
| --- | --- | --- |
| 硬件 | `hardware/core/defines.svh` | 定义 `branch_type_t`、`control_register_t` 等枚举 |
| 模拟器 | `tools/emulator/instruction-set.h` | 用 C 枚举给出同一套数值编码 |
| 汇编 | `tests/asm_macros.h` | 给出 `getcr`/`setcr` 用的 CR 编号宏与 FLAG 位 |

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `hardware/core/defines.svh` | 分支类型 `branch_type_t`、控制寄存器 `control_register_t`、trap 类型等全局定义 |
| `tools/emulator/instruction-set.h` | 模拟器侧的 `branch_type`、`control_register` 枚举（与硬件同构） |
| `hardware/core/instruction_decode_stage.sv` | 把 B 格式/M 格式指令的位段填入 `decoded_instruction_t` |
| `hardware/core/int_execute_stage.sv` | 在整数执行阶段解析分支是否 taken、计算回滚 PC |
| `hardware/core/dcache_data_stage.sv` | 识别 `MEM_CONTROL_REG` 访问、做特权检查并路由到控制寄存器模块 |
| `hardware/core/control_registers.sv` | 控制寄存器的存储、读写逻辑与中断处理 |
| `tools/emulator/processor.c` | 模拟器的分支执行与控制寄存器读写（功能参考实现） |
| `tests/asm_macros.h` | 汇编侧 CR 编号宏、FLAG 位、分支宏 |
| `tests/tools/emulator/recv_host_interrupt.S` | 一个完整的中断处理汇编示例 |
| `software/libs/libos/bare-metal/crt0.S` | 裸机启动里用 `getcr` 读线程号、用 `bnz`/`call`/`b` 控制流的实例 |

## 4. 核心概念与源码讲解

### 4.1 分支与调用指令

#### 4.1.1 概念说明

「分支」解决的问题是：**程序如何不按地址递增的顺序执行**。Nyuzi 用一个 3 位字段 `branch_type_t` 区分七种控制流转移，定义在 [defines.svh:153-162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L153-L162)：

| 编码 | 名称 | 汇编助记符（典型） | 语义 |
| --- | --- | --- | --- |
| `3'b000` | `BRANCH_REGISTER` | `b sX` / `ret` | 跳到寄存器里的地址 |
| `3'b001` | `BRANCH_ZERO` | `bz sX, label` | 若 `sX == 0` 则跳 |
| `3'b010` | `BRANCH_NOT_ZERO` | `bnz sX, label` | 若 `sX != 0` 则跳 |
| `3'b011` | `BRANCH_ALWAYS` | `b label` | 无条件跳 |
| `3'b100` | `BRANCH_CALL_OFFSET` | `call label` | 把返回地址存 s31，跳到 PC 相对偏移 |
| `3'b110` | `BRANCH_CALL_REGISTER` | `call sX` | 把返回地址存 s31，跳到寄存器地址 |
| `3'b111` | `BRANCH_ERET` | `eret` | 从 trap/中断返回（需 supervisor） |

有三个反直觉但重要的点：

1. **条件分支只看最低位**。`bz`/`bnz` 测试的是标量寄存器的 `bit[0]` 是否为 0，而不是「整个寄存器是否为 0」的独立比较单元——但因为比较指令写回的值恰好是 0 或 1，所以效果等价于「测试比较结果」。这套「先 cmp 写 0/1，再 bz/bnz」的组合，替代了传统架构的条件码寄存器。
2. **`call` 的返回地址就是「当前 PC」**。硬件把 `call` 当作一条特殊的 `move ra, pc` 来处理，目的寄存器被强制设为 `REG_RA`（31）。
3. **`eret` 不是普通跳转**。它从「保存的 trap 状态」里恢复 PC、标志位和 subcycle，并要求当前处于 supervisor 模式，否则触发 `TT_PRIVILEGED_OP`。

#### 4.1.2 核心流程

分支指令的生命周期分两段：**解码阶段只做标记，执行阶段才真正解析**。

解码阶段（`instruction_decode_stage`）：

1. 识别 B 格式（`instruction[31:28] == 4'b1111`），从 `instruction[27:25]` 取出 `branch_type`。
2. 根据分支类型决定偏移立即数的位置与长度（见 4.1.3），并把 `decoded.branch` 置 1、填好 `branch_type` 与 `immediate_value`。
3. 若是 `call`，把目的寄存器强制设为 `REG_RA`，并把 ALU 操作改写成 `OP_MOVE`（即「move ra, pc」）。
4. 分支被指派到**整数流水线**（`PIPE_INT_ARITH`）。

执行阶段（`int_execute_stage`）：

1. 读出操作数（条件分支的操作数就是被测试的标量寄存器）。
2. 计算 `branch_taken`：`bz` 看 `operand1[0] == 0`，`bnz` 看 `operand1[0] != 0`，其余类型恒为 taken。
3. 计算**回滚目标 PC** `ix_rollback_pc`：
   - `BRANCH_REGISTER` / `BRANCH_CALL_REGISTER`：目标 = 操作数寄存器的值；
   - `BRANCH_ERET`：目标 = `cr_eret_address[thread]`（保存在 trap 状态里的返回 PC）；
   - 其余（`bz`/`bnz`/`b`/`call offset`）：目标 = 当前指令 PC + 立即数偏移。
4. 若 taken，发出 `ix_rollback_en`，通知取指阶段「从 `ix_rollback_pc` 重新取指」，从而刷新流水线里错误路径上的指令。

为什么不在解码阶段就跳？因为条件分支依赖**寄存器的值**，而寄存器要等到操作数 fetch 之后才读得到；而且 Nyuzi 是多线程乱序执行的，提前猜测分支会大幅增加复杂度，所以采用「执行阶段解析 + 回滚刷新」的简单方案（回滚机制的细节见 u5-l2）。

分支偏移量是**字对齐**的（所有指令 4 字节，所以偏移量在硬件里已经预先 ×4）。设位段为 \(b\)，则：

\[
\text{target\_pc} = \text{PC}_{\text{inst}} + 4 \times \operatorname{signext}(b)
\]

其中条件分支和寄存器分支用 20 位偏移（\(b = \text{inst}[24:5]\)），而无条件 `b` 与 `call offset` 用更长的 25 位偏移（\(b = \text{inst}[24:0]\)），因为函数调用/长跳转通常需要更大的跳转范围。

#### 4.1.3 源码精读

**分支类型定义**（硬件与模拟器数值一致）：

- [defines.svh:153-162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L153-L162) 定义 `branch_type_t` 七个枚举值；[instruction-set.h:107-116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L107-L116) 是模拟器侧同构的 `branch_type`。

**B 格式解码表**（决定每种分支的偏移长度、操作数来源、是否 call）：

- [instruction_decode_stage.sv:215-222](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L215-L222)：可见 `BRANCH_ALWAYS`（`1111_011`）和 `BRANCH_CALL_OFFSET`（`1111_100`）用大偏移 `IMM_24_0`，其余用小偏移 `IMM_24_5`；`call` 类型把 `call` 位置 1。

**偏移立即数（预先 ×4）**：

- [instruction_decode_stage.sv:370-372](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L370-L372)：`IMM_24_5 = $signed({inst[24:5], 2'b00})`、`IMM_24_0 = $signed({inst[24:0], 2'b00})`，末尾补两个 0 就是 ×4。

**分支标记与 call 改写**：

- [instruction_decode_stage.sv:377-380](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L377-L380) 把 `branch_type`、`branch` 标记填入解码结构。
- [instruction_decode_stage.sv:334-344](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L334-L344) 关键两行：`dest_reg = dlut_out.call ? REG_RA : ...`（call 的目的地强制为 s31），`alu_op = OP_MOVE`（call 被当作 move ra, pc）。

**执行阶段解析分支**：

- [int_execute_stage.sv:263-298](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L263-L298)：计算 `branch_taken`，`bz`/`bnz` 分别判 `operand1[0] == 0` 与 `!= 0`，其余恒 taken。
- [int_execute_stage.sv:310-316](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L310-L316)：按分支类型计算 `ix_rollback_pc`（寄存器分支用操作数、eret 用保存的返回地址、其余用 `pc + immediate`）。

**模拟器功能参考**：

- [processor.c:1822-1856](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1822-L1856)：`execute_branch_inst`，注意模拟器里 `pc` 取指后已自增 4，所以偏移要 `- 4` 修正：`offset = signext(bits) * 4 - 4`。

#### 4.1.4 代码实践

**实践目标**：用一组比较 + 条件分支实现「if (a == b) goto L1」，并用模拟器跟踪它的执行。

**操作步骤（源码阅读型 + 待本地运行）**：

1. 打开 [crt0.S:56-66](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L56-L66)，观察真实代码如何组合 `cmpeq_i`（比较写 0/1）→ `bnz`（非零则跳）→ `b`（无条件跳）→ `call`（调用）。
2. 自己写一段最小汇编（**示例代码**，非项目原有）：
   ```asm
   // 假设 s1、s2 已有值
               cmpeq_i s0, s1, s2   ; s0 = (s1 == s2) ? 1 : 0
               bnz s0, equal        ; 若 s0 != 0（即相等）跳到 equal
               move s3, 0           ; 不相等分支
               b done
   equal:      move s3, 1
   done:       ...
   ```
3. 把它编进一个测试程序（参考 u1-l4 的构建方式），用 `run_emulator 程序.hex -v` 运行。

**需要观察的现象**：在 `-v` 跟踪输出里，找到 `cmpeq_i` 那一行，确认它向 `s0` 写入了 0 或 1；再找到 `bnz`，确认它的下一条 PC 要么是 `move s3, 0`（未跳），要么是 `equal` 处的 `move s3, 1`（跳转）。

**预期结果**：跟踪里 `bnz` 之后 PC 的变化取决于 `s0` 的最低位，与你的输入一致。具体输出格式「待本地验证」（取决于模拟器版本与程序编排）。

#### 4.1.5 小练习与答案

**练习 1**：为什么条件分支（`bz`/`bnz`）要等到整数执行阶段才解析，而不是在解码阶段？

> **答案**：因为条件依赖「操作数寄存器的值」，而寄存器读出发生在解码之后的操作数 fetch 阶段；解码阶段还拿不到被测试的值。把解析推迟到执行阶段，可以用「回滚刷新」简单地处理错误路径，避免引入复杂的分支预测。

**练习 2**：`call label` 是如何保存返回地址的？返回地址具体是哪条指令的地址？

> **答案**：解码阶段把 `call` 改写成 `OP_MOVE`，并把目的寄存器强制设为 `REG_RA`（s31），所以效果是「把当前指令的 PC 写入 s31」。返回地址就是这条 `call` 指令自身的 PC（硬件用 `of_instruction.pc`）。由于后续指令从该 PC 之后继续取指，调用者返回时自然落到 `call` 的下一条指令。

**练习 3**：`BRANCH_ALWAYS` 和 `BRANCH_CALL_OFFSET` 为什么用 25 位偏移，而其他分支只用 20 位？

> **答案**：无条件跳转和函数调用通常需要跨越更大的代码距离（比如跳到另一个函数、跨模块调用），所以这两种类型占用 `inst[24:0]` 共 25 位来扩大跳转范围；而条件分支多用于局部 if/else、循环，20 位（`inst[24:5]`）已足够，省下的位段留给被测试的寄存器号 `inst[4:0]`。

---

### 4.2 控制寄存器组

#### 4.2.1 概念说明

「控制寄存器」（control register, CR）是**除标量/向量寄存器之外的另一组系统状态寄存器**。普通寄存器存的是「数据」，控制寄存器存的是「处理器本身的运行状态」：我是哪个线程？上次 trap 的原因是什么？虚拟内存开没开？哪些中断被允许？

控制寄存器一共有 32 个槽位（5 位索引），目前已分配 0–27 号，完整定义在 [defines.svh:164-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L164-L194)。按用途分组：

| 编号 | 名称 | 用途 |
| --- | --- | --- |
| 0 | `CR_THREAD_ID` | 只读，当前核号 + 线程号 |
| 1 | `CR_TRAP_HANDLER` | trap 处理入口地址 |
| 2 | `CR_TRAP_PC` | 触发 trap 的指令 PC |
| 3 | `CR_TRAP_CAUSE` | trap 原因（类型 + dcache/store 标志） |
| 4 | `CR_FLAGS` | 标志位（中断/MMU/supervisor 使能） |
| 5 | `CR_TRAP_ADDRESS` | 触发访问类 trap 的虚拟地址 |
| 6 | `CR_CYCLE_COUNT` | 只读，周期计数器 |
| 7 | `CR_TLB_MISS_HANDLER` | TLB miss 处理入口 |
| 8 | `CR_SAVED_FLAGS` | 嵌套 trap 时保存的上一级 flags |
| 9 | `CR_CURRENT_ASID` | 当前地址空间标识（8 位） |
| 10 | `CR_PAGE_DIR` | 页目录物理基址 |
| 11/12 | `CR_SCRATCHPAD0/1` | trap handler 用的通用暂存字 |
| 13 | `CR_SUBCYCLE` | scatter/gather 的子周期游标 |
| 14–17 | `CR_INTERRUPT_*` | 中断使能/确认/挂起/触发方式（见 4.3） |
| 18 | `CR_JTAG_DATA` | 片上调试器数据通道 |
| 19 | `CR_SYSCALL_INDEX` | syscall 号 |
| 20/21 | `CR_SUSPEND/RESUME_THREAD` | 挂起/恢复线程位图 |
| 22–27 | `CR_PERF_EVENT_*` | 性能事件选择与计数（见 u11-l2） |

**访问方式**：`getcr`（读）和 `setcr`（写）本质上是 M 格式里的 `MEM_CONTROL_REG` 访存指令——`getcr` 是 load 方向（CR → 标量寄存器），`setcr` 是 store 方向（标量寄存器 → CR）。它们看起来像访存指令，但**根本不碰缓存、不碰内存**，而是被数据缓存阶段识别后直接路由到 `control_registers` 模块。

**两个关键约束**：

1. **需要 supervisor 权限**。用户态程序读写控制寄存器会触发 `TT_PRIVILEGED_OP` trap（eret 也一样）。开机时所有线程默认处于 supervisor 模式（[control_registers.sv:129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L129)），所以裸机程序可以直接用。
2. **读写不能在同一周期同时发生**。`control_registers` 内有断言 `assert(!(dd_creg_write_en && dd_creg_read_en))` 保证这一点。

#### 4.2.2 核心流程

一次 `getcr sX, CR_THREAD_ID` 的完整旅程：

1. **解码**：`creg_index = control_register_t'(instruction[4:0])`，读出寄存器号 0；指令被标记为 `MEM_CONTROL_REG` 型 load（[instruction_decode_stage.sv:423](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L423)）。
2. **数据缓存阶段识别**：`creg_access_req = memory_access && memory_access_type == MEM_CONTROL_REG`（[dcache_data_stage.sv:226-229](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L226-L229)）。
3. **特权检查**：`privileged_op_fault = creg_access_req && !cr_supervisor_en[thread]`（[dcache_data_stage.sv:269-270](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L269-L270)）。
4. **路由**：通过 `dd_creg_read_en` / `dd_creg_index` 把请求送给 `control_registers`（[dcache_data_stage.sv:317-324](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L317-L324)）。
5. **读写执行**：`control_registers` 按 `dd_creg_index` 查读写分支，把结果放到 `cr_creg_read_val`。

几个值得记住的设计：

- **`CR_THREAD_ID` 的编码**：读返回 `{CORE_ID, local_thread_idx}`——高位是核号，低位是核内线程号。单核配置下核号为 0，于是线程 0/1/2/3 分别读到 0/1/2/3（[control_registers.sv:286](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L286)）。这正是 crt0 用来给每个线程算栈地址的依据。
- **`CR_FLAGS` 是 3 位**：`{supervisor_en, mmu_en, interrupt_en}`（[control_registers.sv:90-94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L90-L94)）。汇编侧对应 `FLAG_SUPERVISOR_EN(1<<2)`、`FLAG_MMU_EN(1<<1)`、`FLAG_INTERRUPT_EN(1<<0)`（[asm_macros.h:70-73](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L70-L73)）。
- **trap 状态可嵌套**：每个线程维护 `TRAP_LEVELS = 3` 级 trap 状态（`trap_state[thread][0..2]`），所以最多嵌套 2 层 trap（第 0 级是当前状态）。每次 trap 把状态向高一级压栈，`eret` 再弹回（[control_registers.sv:161-184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L161-L184)）。

#### 4.2.3 源码精读

**CR 编号定义**：

- [defines.svh:164-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L164-L194)：硬件侧 `control_register_t`；[instruction-set.h:118-142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L118-L142) 模拟器侧（注意模拟器只列了它实现的部分，到 `CR_RESUME_THREAD=21` 为止，性能计数相关在硬件里有、模拟器省略）；[asm_macros.h:25-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L25-L51) 汇编侧宏。

**存储结构**：

- [control_registers.sv:90-118](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L90-L118)：`flags_t`、`trap_state_t` 结构，以及按线程分体的 `trap_state`、`page_dir_base`、`interrupt_mask`、`interrupt_pending` 等存储。

**写逻辑**（`setcr` 的落点）：

- [control_registers.sv:192-215](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L192-L215)：`unique case (dd_creg_index)` 把每个 CR 写到对应存储。注意 `CR_SUSPEND_THREAD` / `CR_RESUME_THREAD` 写的是全核线程位图（`TOTAL_THREADS` 位）。

**读逻辑**（`getcr` 的来源）：

- [control_registers.sv:281-310](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L281-L310)：`CR_THREAD_ID` 返回 `{CORE_ID, dt_thread_idx}`；未实现的槽位返回 `32'hffffffff`。

**裸机实例**：

- [crt0.S:44-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L44-L47)：`getcr s0, 0` 读线程号，`shl s0, s0, 14`（每线程 16KiB 栈），再用 `sub_i sp, sp, s0` 算出各自栈顶。
- [crt0.S:88-89](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L88-L89)：`setcr s0, CR_SUSPEND_THREAD` 写 -1 停机（呼应 u1-l4 讲过的停机机制）。

#### 4.2.4 代码实践

**实践目标**：列出全部控制寄存器编号与用途，并验证「在 4 线程配置下不同线程读到不同的线程号」。

**操作步骤**：

1. 对照 [defines.svh:164-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L164-L194) 与 [asm_macros.h:25-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L25-L51)，自己整理一张「编号 → 名称 → 用途」表（本文 4.2.1 已给出参考）。
2. 阅读真实启动代码 [crt0.S:44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L44)，确认它就是用 `getcr s0, 0` 读线程号。
3. 写一段**示例汇编**（非项目原有），让每个线程把自己的线程号打印出来：
   ```asm
   _start:
               getcr s0, CR_CURRENT_THREAD   ; 读自己的线程号
               ; ... 把 s0 通过 printf/UART 打印 ...
               halt_current_thread           ; asm_macros.h 里的停机宏
   ```
4. 按 u1-l4 的方式构建并用 `run_emulator` 运行。

**需要观察的现象**：默认 `THREADS_PER_CORE = 4`，四个硬件线程都会从 `_start` 并发执行。每个线程读 `CR_THREAD_ID` 应得到 0、1、2、3 之一，且互不相同。

**预期结果**：输出里出现 0、1、2、3 四个不同的值（顺序可能交错，因为线程并发）。若开了多核（`NUM_CORES > 1`），高位的核号也会体现出来，例如 1 号核的 0 号线程读到 `0x10`。具体打印格式「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`getcr` / `setcr` 属于哪一种指令格式？为什么它「不走缓存」？

> **答案**：属于 M 格式（访存格式），其 `memory_op_t == MEM_CONTROL_REG`。控制寄存器是处理器内部状态，不在内存地址空间里，所以数据缓存阶段识别到这一类型后，不查 DTLB、不访问缓存行，而是直接把请求路由给 `control_registers` 模块。

**练习 2**：在单核 4 线程配置下，4 个线程各自读 `CR_THREAD_ID` 得到什么？为什么？

> **答案**：分别得到 0、1、2、3。因为读逻辑返回 `{CORE_ID, local_thread_idx}`，单核时 `CORE_ID` 为 0，低 2 位就是核内线程号 0–3。这正是多线程程序「自报家门」、给每个线程分配独立栈/工作量的基础。

**练习 3**：`CR_FLAGS` 和 `CR_SAVED_FLAGS` 有什么关系？

> **答案**：`CR_FLAGS` 是当前线程的第 0 级（当前）flags；`CR_SAVED_FLAGS` 对应第 1 级（嵌套保存）flags。发生 trap 时，当前 flags 被压到更高一级（隐式保存），中断处理返回时由 `eret` 自动恢复——`CR_SAVED_FLAGS` 让软件能在不触发再次 trap 的前提下查看/修改被保存的那一级 flags。

---

### 4.3 中断相关控制寄存器

#### 4.3.1 概念说明

中断（interrupt）是「处理器之外的事件」打断当前执行流的一种 trap。Nyuzi 支持最多 16 个中断源（`NUM_INTERRUPTS` 参数），每个核通过四条控制寄存器管理它们（定义见 [defines.svh:180-183](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L180-L183)）：

| 编号 | 名称 | 方向 | 作用 |
| --- | --- | --- | --- |
| 14 | `CR_INTERRUPT_ENABLE` | 读/写 | 16 位中断**屏蔽码**，某 bit 为 1 表示该中断被允许 |
| 15 | `CR_INTERRUPT_ACK` | 写 | 写 1 的 bit 用来**确认（清除）**对应中断 |
| 16 | `CR_INTERRUPT_PENDING` | 只读 | 当前**挂起**（已触发且未被屏蔽）的中断位图 |
| 17 | `CR_INTERRUPT_TRIGGER` | 读/写 | 每个 bit 选择该中断是**电平触发**(1)还是**边沿触发**(0) |

它们和 `CR_FLAGS` 的 `interrupt_en` 位配合：只有「全局中断使能位 `interrupt_en` 为 1」**且**「某中断在屏蔽码里为 1」**且**「该中断挂起」时，才会真正打断处理器。

一个核心设计是**精确中断（precise interrupt）**：从软件角度看，中断恰好发生在两条指令的边界上——中断前的所有指令都已执行完，中断后的指令都还没执行。Nyuzi 通过「在解码阶段把被中断的指令替换成一条 trap 指令」来实现这一点（详见 4.3.2）。

#### 4.3.2 核心流程

一次中断从外部信号到处理完毕的全过程：

1. **外部请求**：外设通过 `interrupt_req[15:0]` 向某核发出中断信号。
2. **挂起计算**：`control_registers` 区分触发方式（[control_registers.sv:266-267](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L266-L267)）：电平触发的中断直接看 `interrupt_req` 电平；边沿触发的中断先锁存上升沿到 `interrupt_edge_latched`，再由 ACK 清除。
3. **汇总输出**：`cr_interrupt_pending[thread] = |(interrupt_pending[thread] & interrupt_mask[thread])`（[control_registers.sv:271-272](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L271-L272)），即「有任意一个被允许且挂起的中断」。
4. **精确替换**：解码阶段判定 `raise_interrupt = (cr_interrupt_pending & cr_interrupt_en)[thread]`（[instruction_decode_stage.sv:279-281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L279-L281)），若成立，把当前指令标记为 `TT_INTERRUPT` trap（`has_trap=1`），其副作用被屏蔽。
5. **进入 trap**：写回阶段捕获 trap，保存 PC/flags/subcycle 到 `trap_state[thread][0]`，置 supervisor、关中断，PC 跳到 `CR_TRAP_HANDLER`。
6. **处理与确认**：handler 读 `CR_INTERRUPT_PENDING` 判断是哪个中断，干完活后写 `CR_INTERRUPT_ACK` 清除它。
7. **返回**：`eret` 恢复保存的 PC/flags/subcycle，回到被打断的位置继续执行。

值得注意的细节：解码阶段会刻意**避免在「需要成对发射」的指令中间插入中断**——例如 IO 访问、同步访存（LL/SC）的第一条指令已经改了内部状态，若在两条之间中断会破坏原子性。所以 [instruction_decode_stage.sv:279-280](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L279-L280) 用 `~ior_pending & ~dd_load_sync_pending & ~sq_store_sync_pending` 把这些情况排除掉。

#### 4.3.3 源码精读

**中断相关 CR 与外设寄存器**：

- [defines.svh:180-183](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L180-L183)：四个中断 CR。
- [asm_macros.h:76-78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L76-L78)：`REG_HOST_INTERRUPT = 0xffff0018` 等设备寄存器（向这个 MMIO 地址写值可触发宿主中断，是模拟器/测试用的中断注入点）。

**中断生成逻辑**：

- [control_registers.sv:231-274](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L231-L274)：每个线程一份边沿锁存、pending 计算、`cr_interrupt_en`/`cr_supervisor_en`/`cr_mmu_en` 输出。
- [control_registers.sv:243-247](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L243-L247)：把 flags 暴露给流水线（中断是否真正生效取决于 `cr_interrupt_en`）。

**精确中断的替换机制**：

- [instruction_decode_stage.sv:279-281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L279-L281)：`raise_interrupt` 的判定，注意排除了成对指令中间的情况。
- [instruction_decode_stage.sv:248-249](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L248-L249)：`raise_interrupt` 时把 trap 原因设成 `TT_INTERRUPT`。

**完整汇编示例**：

- [recv_host_interrupt.S:24-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/tools/emulator/recv_host_interrupt.S#L24-L57)：一个 `.macro receive_interrupt`，完整演示了「设 trap handler → 开中断 → 等待 → 进 handler 读 PENDING/ACK → 检查 TRAP_CAUSE/TRAP_PC → 返回」的流程，是理解中断控制寄存器用法的最佳阅读材料。

#### 4.3.4 代码实践

**实践目标**：读懂一个真实的中断处理流程，把控制寄存器的「纸面编号」和「实际行为」对上号。

**操作步骤（源码阅读型）**：

1. 打开 [recv_host_interrupt.S:24-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/tools/emulator/recv_host_interrupt.S#L24-L57)。
2. 逐行标注每条 `setcr`/`getcr` 用到的 CR 编号，对应回 4.3.1 的表：
   - L27 `setcr s0, CR_TRAP_HANDLER`：先装好 trap 入口；
   - L31 `setcr s0, CR_FLAGS`：写 flags 同时开 supervisor + 开中断；
   - L38 `getcr s25, CR_INTERRUPT_PENDING`：进 handler 后查是谁中断了我；
   - L39 `setcr s25, CR_INTERRUPT_ACK`：用同一个位图确认（清除）中断；
   - L42 `getcr s0, CR_TRAP_CAUSE`：核对原因确实是 `TT_INTERRUPT`；
   - L51 `getcr s0, CR_TRAP_PC`：核对返回地址是被打断的那条 `b int_addr`。
3. （可选，待本地运行）按 u15-l1 的测试框架运行该测试，观察它在 `run_emulator` 下通过。

**需要观察的现象**：在中断注入后，handler 里读到的 `CR_TRAP_CAUSE` 应为 `TT_INTERRUPT`(3)，`CR_TRAP_PC` 应指向「等待中断」的那条 `b int_addr` 自循环指令——这正是「精确中断」的证据：返回后会重新执行那条等待指令。

**预期结果**：测试输出 `PASS`。「待本地验证」运行环境是否已装好工具链。

#### 4.3.5 小练习与答案

**练习 1**：Nyuzi 是如何做到「精确中断」的？

> **答案**：在解码阶段，如果发现当前线程有挂起且被允许的中断，就把这条指令标记成 `TT_INTERRUPT` trap（`has_trap=1`），其原本的副作用被屏蔽。这样中断就发生在「这条指令之前」的边界上：之前的指令都已写回，这条及之后的指令都没生效。trap 在写回阶段被捕获并保存精确的 PC。

**练习 2**：`CR_INTERRUPT_PENDING`、`CR_INTERRUPT_ENABLE`、`CR_INTERRUPT_ACK` 三者是什么关系？

> **答案**：`ENABLE` 是软件写的中断屏蔽码（想收哪些中断）；`PENDING` 是硬件维护的「已触发且未被屏蔽」位图（谁正在敲门）；`ACK` 是软件的清除动作（处理完后告诉硬件「这个我处理完了」）。一个中断只有同时「在 ENABLE 里为 1」且「在 PENDING 里为 1」才会真正打断处理器；处理完写 ACK 把它从挂起集合里清掉。

**练习 3**：为什么在 IO 访问或 LL/SC 同步访存的「第一条指令」刚发射后，解码阶段要暂时不派发中断？

> **答案**：这类指令需要**成对发射两次**——第一次排队/上锁并改变内部状态，第二次取回结果。如果在两次之间插入中断，中断处理完返回后状态已被打乱（例如锁已释放或请求已丢失），会破坏原子性或丢请求。所以解码阶段在检测到 `ior_pending` / `dd_load_sync_pending` / `sq_store_sync_pending` 时抑制中断，等到成对指令完成后才允许中断发生。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「**线程号读取 + 条件分支 + 中断计数**」的小任务。

**任务描述**：写一个裸机汇编小程序（**示例代码**，非项目原有），完成下面三件事，并解释每一步用到的是本讲的哪个机制：

1. **读线程号**：用 `getcr s0, CR_CURRENT_THREAD` 读出当前线程号，用 `bnz` 判断「是不是 0 号线程」，只让 0 号线程继续往下做初始化（这正是 [crt0.S:44-56](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L44-L56) 的做法）。
2. **开一个中断并统计**：参考 [recv_host_interrupt.S:24-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/tools/emulator/recv_host_interrupt.S#L24-L39)，设置 `CR_TRAP_HANDLER`、用 `CR_FLAGS` 开中断、用 `CR_INTERRUPT_ENABLE` 解屏蔽某一路中断；在 handler 里用一个标量寄存器做计数器，每进一次 handler 加 1，然后 `CR_INTERRUPT_ACK` 清中断、`eret` 返回。
3. **用条件分支汇报**：当计数器达到 N 时，用 `cmpeq_i` + `bnz` 跳出等待循环，打印「收到 N 次中断」后用 `setcr s0, CR_SUSPEND_THREAD` 停机。

**完成后，请回答**：

- 第 1 步体现了「分支调用」模块的哪条指令？为什么 `bnz` 之前不需要单独比较「s0 == 0」？
  > 因为读出来的线程号本身就是 0 或非零，`bnz` 直接测试 `s0[0] != 0`（非零线程号的最低位通常为 1）。但更稳妥的写法是先 `cmpeq_i` 再 `bnz`，就像 crt0 里 `bnz s0, do_main` 依赖 s0 恰好为线程号——线程 0 时 s0=0 不跳，非 0 时跳。
- 第 2 步用到了「中断相关寄存器」里的哪几个 CR？`eret` 的返回地址是从哪里恢复的？
  > 用到 `CR_TRAP_HANDLER`、`CR_FLAGS`、`CR_INTERRUPT_ENABLE`、`CR_INTERRUPT_ACK`。`eret` 的返回地址来自保存的 trap 状态 `trap_state[thread][0].trap_pc`（即 `CR_TRAP_PC` 自动保存的值），由 `cr_eret_address` 提供给执行阶段。
- 整个过程里，哪些指令需要 supervisor 权限？用户态程序能不能直接做？
  > `setcr`/`getcr`（所有 CR 访问）和 `eret` 都需要 supervisor。用户态程序直接执行会触发 `TT_PRIVILEGED_OP`，必须通过 syscall 进入内核（见 u12）才能间接操作这些状态。

**提示**：如果你在本地搭好了工具链（u1-l2），可以参照 u15-l1 的测试框架把这个小程序包成一个自校验测试；否则按本讲的「源码阅读型实践」逐行对照 crt0.S 与 recv_host_interrupt.S 即可。

## 6. 本讲小结

- Nyuzi 用 3 位 `branch_type_t` 表达七种控制流转移：条件分支 `bz`/`bnz` 只测试标量寄存器最低位，`b`/寄存器分支是无条件跳转，`call` 借助「move ra, pc」保存返回地址，`eret` 从 trap 返回。
- 分支在**整数执行阶段**才解析（因为要读操作数），taken 时通过回滚机制刷新取指；偏移量在硬件里已预先 ×4，条件分支用 20 位偏移，`b`/`call offset` 用 25 位。
- 控制寄存器是「处理器状态」的集合，用 5 位索引编址（0–27），由 `getcr`/`setcr`（即 `MEM_CONTROL_REG` 型访存指令）读写；它不走缓存、需 supervisor 权限，由 `control_registers` 模块存储。
- `CR_THREAD_ID` 返回 `{CORE_ID, local_thread_idx}`，是裸机多线程「自报线程号」的基础；`CR_FLAGS` 的三位控制中断/MMU/supervisor 使能。
- 中断由 `CR_INTERRUPT_ENABLE/ACK/PENDING/TRIGGER` 四个寄存器配合 `CR_FLAGS.interrupt_en` 管理；Nyuzi 用「解码阶段替换指令为 trap」实现精确中断，并刻意避免在成对指令中间打断。
- trap 状态支持 `TRAP_LEVELS=3` 级嵌套，trap 时压栈、`eret` 时弹栈，使中断/异常可以被嵌套处理。

## 7. 下一步学习建议

本讲建立的是「分支与控制状态」的静态地图。接下来建议：

- **想看分支回滚如何驱动流水线刷新** → 进入 u5（执行单元），尤其是 u5-l2「整数执行单元」，它会展示 `ix_rollback_en`/`ix_rollback_pc` 如何回到取指阶段。
- **想深入 trap 的产生与回滚机制** → 进入 u7-l2「控制寄存器与中断」和 u7-l3「Trap 处理与回滚」，它们从写回阶段的角度完整讲解 `wb_trap`/`wb_eret` 的时序。
- **想看控制寄存器在内核里如何被使用** → 进入 u12（内核与虚拟内存系统），`trap_entry.S`、`start.S` 里大量使用 `getcr`/`setcr` 操作 `CR_FLAGS`、`CR_TRAP_PC`、`CR_PAGE_DIR` 等，是本讲知识的「整机应用」。
- **想了解性能计数寄存器（22–27）如何被剖析工具使用** → 进入 u11-l2「性能计数器与 profiling」。

建议同步动手：把本讲 4.1.4 的比较 + 条件分支示例，与 4.2.4 的读线程号示例，在模拟器 `-v` 模式下跟踪一遍，把「纸面编码」和「真实执行轨迹」一一对应起来。
