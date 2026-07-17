# 控制寄存器与中断

## 1. 本讲目标

本讲深入 Nyuzi 单核内的「状态中枢」模块 `control_registers`。读者在 u2-l4 已经从指令集层面认识了 `getcr`/`setcr` 与各控制寄存器的含义，本讲则要打开这个模块的黑盒，搞清楚：

- 这些控制寄存器在硬件里到底**以什么结构存储**、按什么粒度（每线程 / 全核）维护。
- 一根外部中断请求线 `interrupt_req` 是如何经过**挂起（pending）→ 屏蔽（mask）→ 使能（enable）→ 确认（ack）** 的时序，最终变成一次精确中断。
- trap（陷阱）发生时，旧的状态如何被**压栈**，`eret` 返回时又如何**出栈**，从而支持**嵌套**。
- supervisor / MMU / ASID 等**线程运行态**如何随线程独立保存，以及 `suspend`/`resume` 如何与顶层 `thread_en` 联动。

学完后，你应当能画出「中断从引脚到指令替换」的完整信号通路，并解释为什么 Nyuzi 的中断是精确的。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个概念。

- **控制寄存器（control register）**：和通用寄存器（s0–s31）不同，它们保存的是处理器的「配置与状态」，例如「当前是不是特权态」「中断有没有打开」「页目录在哪儿」。程序用 `getcr`/`setcr` 读写它们（u2-l4）。
- **精确中断/精确异常（precise interrupt / precise exception）**：从软件角度看，中断恰好发生在两条指令的边界上——中断前的每条指令都已执行完，中断后的每条指令都尚未执行。这在乱序退役的流水线里并不 trivial，是本讲的重点之一。
- **trap（陷阱）**：Nyuzi 把复位、非法指令、缺页、TLB 缺失、syscall、中断等统称为 trap，共用一套保存现场 → 跳处理程序 → `eret` 返回的机制。
- **嵌套（nesting）**：处理 trap 的过程中又发生了 trap。需要多套「现场槽」来保存被打断的状态。
- **电平触发 vs 边沿触发（level vs edge triggered）**：电平触发的中断只要信号为高就一直挂起；边沿触发的中断只在信号「由低变高」的那一瞬间记一次。 Nyuzi 两种都支持，每个中断源可单独配置。

如果你对 `trap_cause_t`、`control_register_t`、流水线分级（取指→解码→…→写回）还不熟，请先看 u2-l4 与 u3-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hardware/core/control_registers.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv) | 本讲主角。存储控制寄存器、实现中断检测与确认、实现 trap 压栈/出栈、维护 supervisor/MMU/ASID 与线程挂起/恢复。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 定义 `control_register_t`（控制寄存器编号）、`trap_type_t`、`trap_cause_t`、`ASID_WIDTH` 等贯穿全局的类型与常量。 |
| [hardware/core/instruction_decode_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv) | 中断落地的「现场」：当 `cr_interrupt_pending` 有效时，把当前指令替换成一条只携带 `TT_INTERRUPT` 的空壳指令。 |
| [hardware/core/writeback_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv) | 统一处理 trap/eret/分支的回滚，把 `wb_trap`、`wb_eret` 及现场信息发回 `control_registers`。 |
| [hardware/core/nyuzi.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv) | 顶层：把各核的 `cr_suspend_thread`/`cr_resume_thread` 聚合成全局 `thread_en`。 |
| [tests/core/trap/io_interrupt.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/io_interrupt.S) / [int_config.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/int_config.S) | 真实中断测试程序，本讲代码实践的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 控制寄存器存储

#### 4.1.1 概念说明

一个核里有多个硬件线程（默认 `THREADS_PER_CORE = 4`），每个线程都有自己的运行状态：它是不是特权态、MMU 开没开、用哪个 ASID、页目录在哪、上一条被打断的指令是什么。因此大部分控制寄存器**不是全局唯一的**，而是**每个线程一份**。

`control_registers` 模块用一个二维数组 `trap_state[thread][level]` 来集中保存这些「会随 trap 进出而切换」的状态，另用几个独立数组保存 ASID、页目录、中断屏蔽等。

注意模块参数：

- [`NUM_INTERRUPTS = 16`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L28-L31)：本核支持 16 个中断源。
- `NUM_PERF_EVENTS = 8`：性能事件数（详见 u11-l2）。

#### 4.1.2 核心流程

存储结构的关键是两个 packed struct。先看「标志位」：

```systemverilog
typedef struct packed {
    logic supervisor_en;
    logic mmu_en;
    logic interrupt_en;
} flags_t;
```

这三 bit 就是 `CR_FLAGS`（编号 4）的全部内容：是否特权态、是否开 MMU、是否**全局**开中断。再看承载一次 trap 现场的结构：

```systemverilog
typedef struct packed {
    flags_t flags;
    scalar_t scratchpad0;
    scalar_t scratchpad1;
    trap_cause_t trap_cause;
    scalar_t trap_pc;
    scalar_t trap_access_addr;
    subcycle_t trap_subcycle;
    syscall_index_t syscall_index;
} trap_state_t;
```

它把「被打断时的标志位 + 两个通用暂存槽 + 这次 trap 的原因/PC/访存地址/subcycle/syscall 号」打包在一起。最终用一个三维数组集中存放：

```systemverilog
trap_state_t trap_state[`THREADS_PER_CORE][TRAP_LEVELS];   // 每线程 TRAP_LEVELS 套现场
scalar_t page_dir_base[`THREADS_PER_CORE];
logic[NUM_INTERRUPTS - 1:0] interrupt_mask[`THREADS_PER_CORE];
logic[NUM_INTERRUPTS - 1:0] interrupt_pending[`THREADS_PER_CORE];
```

其中 [`TRAP_LEVELS = 3`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L87-L88)：每个线程保留 3 套现场槽，第 0 套是「当前」，所以最多嵌套 2 层（`TRAP_LEVELS - 1`）。其余数组分别按线程保存页目录基址、中断屏蔽字、中断挂起字。

#### 4.1.3 源码精读

控制寄存器的**写**由来自 `dcache_data_stage` 的 `dd_creg_write_en` 触发（因为 `setcr` 是一条访存指令，走访存流水线，最终在数据缓存级把写请求送到这里）。写逻辑是一张 `unique case` 表，按编号分发到对应存储：

[hardware/core/control_registers.sv:192-215](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L192-L215) —— 这段 `case` 把 `CR_FLAGS` 写进 `trap_state[dt_thread_idx][0].flags`，把 `CR_INTERRUPT_ENABLE` 写进 `interrupt_mask[dt_thread_idx]`，把 `CR_CURRENT_ASID` 截低 8 位写进 `cr_current_asid`，等等。注意：**写的下标一律是 `dt_thread_idx`**（即将进入 dcache 数据级的那个线程），说明这些寄存器都是每线程一份。

关键细节：
- [`CR_FLAGS`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L195) 写 `trap_state[...][0].flags`（当前层）；而 [`CR_SAVED_FLAGS`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L196) 写 `trap_state[...][1].flags`（上一层），软件用它手动恢复嵌套现场。
- [`CR_INTERRUPT_ENABLE`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L205) 写的是 **16 位 `interrupt_mask`**（逐源屏蔽），与 `flags.interrupt_en`（1 位全局使能）是两码事——中断要被受理需要两者都满足，详见 4.2。
- [`CR_SUSPEND_THREAD` / `CR_RESUME_THREAD`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L208-L209) 写的是 `TOTAL_THREADS` 位（跨核全局位宽），这一点与其它「每线程」寄存器不同，原因见 4.4。

读逻辑是另一张对称的表：

[hardware/core/control_registers.sv:276-311](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L276-L311) —— `getcr` 走读通路，按编号把对应字段拼成 `scalar_t` 送回 `cr_creg_read_val`，例如 `CR_TRAP_PC` 读 `trap_state[dt_thread_idx][0].trap_pc`，`CR_THREAD_ID` 读 `{CORE_ID, dt_thread_idx}`。未分配的编号落到 `default`，返回 `32'hffffffff`。代码里还有一条断言 [`assert(!(dd_creg_write_en && dd_creg_read_en))`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L151)，保证同周期不会又读又写。

> 类型定义在 [defines.svh 的 `control_register_t`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L165-L194)：编号 0–27 已分配，5 位编号最多可到 32。与之配套的 [`trap_type_t`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L197-L210) 列出全部 12 种 trap 类型（含本讲关注的 `TT_INTERRUPT = 3`）。

#### 4.1.4 代码实践

**目标**：确认「控制寄存器是每线程独立存储」这一结论，并验证读写一致。

**操作步骤**：
1. 阅读 [tests/core/trap/int_config.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/int_config.S)，它写一个值到 `CR_INTERRUPT_TRIGGER` / `CR_INTERRUPT_ENABLE` 再读回，用 `assert_reg` 自校验。
2. 在仓库根目录构建后，用运行脚本在模拟器上跑该测试（u1-l2 介绍过 `run_emulator` 由 CMake 生成）：
   ```bash
   run_emulator int_config
   ```
   （具体目标名以 `tests/core/trap` 的 CMake 注册为准，若不确定可 `make help` 或查阅 `tests/CMakeLists.txt`。）

**观察现象**：模拟器输出 `PASS`，表示写进去的 `0x871e` / `0xdec4` / `0x9a0f` / `0x5f21` 都能原样读回。

**预期结果**：`CR_INTERRUPT_TRIGGER` 与 `CR_INTERRUPT_ENABLE` 的读写完全对称，证明它们就是普通的可读写存储位。

> 若本地未搭建工具链，标记为「待本地验证」；可改为纯源码阅读：对照 4.1.3 的写表与读表，确认每个编号的写目标和读来源是同一字段。

#### 4.1.5 小练习与答案

**练习 1**：`CR_FLAGS` 和 `CR_INTERRUPT_ENABLE` 都和「中断」有关，它们各控制什么？为什么需要两个？

**参考答案**：`CR_FLAGS` 里的 `interrupt_en` 是**全局**使能位（1 bit），是「要不要响应任何中断」的总开关；`CR_INTERRUPT_ENABLE` 是 **16 位逐源屏蔽字** `interrupt_mask`，决定 16 个中断源里哪几个被允许。两者相与才决定某中断是否被受理（见 4.2.3）。分两层是因为：全局位用于「进入临界区临时关中断」，屏蔽字用于「永久选择订阅哪些源」。

**练习 2**：读 `CR_THREAD_ID`（编号 0）得到的是一个怎样的值？为什么高位是核号、低位是线程号？

**参考答案**：读回 `{CORE_ID, dt_thread_idx}`，即高位拼核号、低位拼核内线程号（见 [control_registers.sv:286](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L286)）。因为控制寄存器是**核内**按线程存的（下标 `dt_thread_idx` 只在核内唯一），必须再拼上 `CORE_ID` 才能得到全芯片唯一的线程标识，供软件区分自己跑在哪个核的哪个线程上。

---

### 4.2 中断处理

#### 4.2.1 概念说明

外部中断请求是一组 16 位信号 `interrupt_req[NUM_INTERRUPTS-1:0]`，从顶层送进每个核。问题在于：这些原始信号不能直接去打断流水线，否则会破坏精确性。Nyuzi 的做法分三步：

1. **检测**：在 `control_registers` 内把 `interrupt_req` 加工成「该线程是否有挂起且未屏蔽的中断」`cr_interrupt_pending`。
2. **落地**：在解码级，当某线程有挂起中断且全局使能时，把它正在解码的指令**替换**成一条 trap 指令。
3. **退役**：被替换的指令随整数流水线到达写回级，触发 `wb_trap`，由 `control_registers` 保存现场并跳到处理程序。

此外，软件处理完后要**确认（ack）**中断，否则边沿型中断会一直挂着；还要能配置每个源是**电平**还是**边沿**触发。

#### 4.2.2 核心流程

中断检测的组合逻辑链路可以画成：

```
interrupt_req (16 位, 来自顶层)
        │
        ▼
  interrupt_req_prev ──► interrupt_edge = req & ~prev   （上升沿，逐源）
        │
        ▼
  int_trigger_type (16 位, 全局配置: 1=电平, 0=边沿)
        │
        ├─ 电平位: pending = req
        └─ 边沿位: pending = edge_latched （锁存到 ack 前）
        │
        ▼
  interrupt_pending[thread]              （每线程一份）
        │  & interrupt_mask[thread]      （CR_INTERRUPT_ENABLE, 每线程一份）
        ▼
  cr_interrupt_pending[thread] = |(...)   （归约成 1 位送解码级）
        │  & cr_interrupt_en[thread]      （flags.interrupt_en, 全局使能）
        │  & ~两段式指令进行中
        ▼
  raise_interrupt ──► 解码级把指令替换为 TT_INTERRUPT
```

要点：`cr_interrupt_pending` 已经**与上了屏蔽字**但**还没与全局使能位**；全局使能在解码级再相与。这样设计让「有没有挂起」和「现在能不能响应」解耦——软件可以先 `getcr CR_INTERRUPT_PENDING` 查询挂起情况，即使中断暂时被全局关闭。

确认（ack）的语义：
- **边沿型**：ack 把对应位的 `interrupt_edge_latched` 清掉（`interrupt_edge_latched & ~interrupt_ack`）。
- **电平型**：ack **不清除**任何东西，pending 直接跟随 `req` 电平；软件必须让外设撤掉请求（例如重置定时器），否则中断会立刻再次触发。

#### 4.2.3 源码精读

**(a) 边沿检测**（全局，不分线程）：

[hardware/core/control_registers.sv:221-229](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L221-L229) 把 `interrupt_req` 打一拍得到 `interrupt_req_prev`，再做 `interrupt_edge = interrupt_req & ~interrupt_req_prev`，即逐位提取上升沿。

**(b) 每线程的挂起、ack、输出**：用 `generate` 给每个线程实例化一段逻辑：

[hardware/core/control_registers.sv:231-274](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L231-L274)。逐段看：

- ack 判定 [L238-L242](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L238-L242)：当 `setcr CR_INTERRUPT_ACK` 且写操作的线程号等于本生成块的线程号时，`interrupt_ack` 取出写值的对应位。
- 边沿锁存 [L251-L261](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L251-L261)：`(latched & ~ack) | edge`——锁存上升沿，直到对应位被 ack 清除。
- 电平/边沿选择与挂起归约 [L266-L272](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L266-L272)：

```systemverilog
assign interrupt_pending[thread_idx] = (int_trigger_type & interrupt_req)
    | (~int_trigger_type & interrupt_edge_latched[thread_idx]);
assign cr_interrupt_pending[thread_idx] = |(interrupt_pending[thread_idx]
                                      & interrupt_mask[thread_idx]);
```

`int_trigger_type` 位为 1 的源走电平（直接取 `req`），为 0 的走边沿锁存；最后与屏蔽字相与、归约成一位 `cr_interrupt_pending`。

**(c) 落地到指令替换**（在解码级）：

[hardware/core/instruction_decode_stage.sv:279-281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L279-L281)：

```systemverilog
assign masked_interrupt_flags = cr_interrupt_pending & cr_interrupt_en
    & ~ior_pending & ~dd_load_sync_pending & ~sq_store_sync_pending;
assign raise_interrupt = masked_interrupt_flags[ifd_thread_idx] && !ocd_halt;
```

这里再把「挂起(已含屏蔽)」与「全局使能 `cr_interrupt_en`」相与，并且**屏蔽掉两段式指令进行中**的线程（`ior_pending`/`dd_load_sync_pending`/`sq_store_sync_pending` 分别表示 IO 访问、同步 load、同步 store 的「第一拍已发、第二拍未取」状态）。这正是 u2-l4 提到的「不在成对指令之间打断」的硬件实现。

一旦 `raise_interrupt` 成立，[`has_trap`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L237-L241) 被置位，trap 原因被设成 [`TT_INTERRUPT`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L248-L249)，并且因为 `has_trap` 为真，这条指令的**所有读写副作用都被抑制**（`has_scalar1/has_dest/memory_access` 等都 `& !has_trap`，见 [L293-L330](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L293-L330)）——它变成一条「只携带原 PC 与 `TT_INTERRUPT`」的空壳指令。这就是精确性的来源：中断替换发生在解码边界，原指令什么都没做。

**(d) 退役与跳转**（在写回级）：

被替换的空壳指令走整数流水线（`has_trap` 时 `pipeline_sel = PIPE_INT_ARITH`），到写回级触发 trap 回滚：

[hardware/core/writeback_stage.sv:177-199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L177-L199)：当 `ix_instruction.has_trap` 成立，置 `wb_trap=1`、`wb_rollback_pc = cr_trap_handler`（通用 trap 处理入口），把 `wb_trap_cause`（这里是 `TT_INTERRUPT`）、`wb_trap_pc`（原 PC）等送回 `control_registers` 去保存现场。

#### 4.2.4 代码实践

**目标**：把 4.2.3 画出的「中断从引脚到指令替换」通路，对照一个真实跑中断的程序逐段验证，并解释 `CR_INTERRUPT_ENABLE` / `ACK` / `PENDING` 三者如何配合。

**操作步骤**：
1. 阅读 [tests/core/trap/io_interrupt.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/io_interrupt.S)。这个测试在「主循环不停写串口」的同时让硬件定时器（中断源 1）反复触发中断，曾用于暴露「中断命中 IO 写导致写重复」的硬件 bug。
2. 对照源码标注三件事在程序里的位置：
   - **订阅源**：[`setcr s24, CR_INTERRUPT_ENABLE`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/io_interrupt.S#L65-L66) 写 `2`（即 bit1，开定时器），对应 `interrupt_mask`。
   - **全局开中断**：[`setcr s0, CR_FLAGS`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/io_interrupt.S#L74-L75) 写 `FLAG_SUPERVISOR_EN | FLAG_INTERRUPT_EN`，把 `flags.interrupt_en` 置 1。
   - **查询 + 确认**：处理程序里 [`getcr s24, CR_INTERRUPT_PENDING`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/io_interrupt.S#L36) 查挂起，处理完后 [`setcr s25, CR_INTERRUPT_ACK`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap/io_interrupt.S#L48-L49) 写 `2` 清掉 bit1。
3. 运行（仅 Verilog 仿真 / 模拟器有硬件定时器）：
   ```bash
   run_verilator io_interrupt      # 或 run_emulator io_interrupt
   ```

**观察现象**：输出里夹着 `*`（中断处理程序写的字符）和主循环写的数字序列，且没有任何字符被重复写两次。

**预期结果**：`*` 出现的次数与定时器周期数吻合；主循环字符无重复，证明中断没有打断在 IO 写的两拍中间（4.2.3 的 `~ior_pending` 守卫生效）。

> 该测试依赖硬件定时器（中断源 1），仅在 verilator / emulator 上有意义，标记为「待本地验证」。若环境不具备，可降级为源码阅读：把 4.2.3 的四个代码点 (a)–(d) 与本程序的四个 `setcr`/`getcr` 一一对应起来。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cr_interrupt_pending` 在 `control_registers` 里只与屏蔽字相与，而把全局使能位留到解码级才相与？

**参考答案**：为了让软件「关着中断也能查询挂起状态」。如果把全局使能也并进 `cr_interrupt_pending`，那么一旦关中断，`getcr CR_INTERRUPT_PENDING` 就恒为 0，软件无法知道哪些源在排队。现在的拆分使 `CR_INTERRUPT_PENDING`（见 [control_registers.sv:298-L299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L298-L299)）只反映「挂起且未屏蔽」，与「现在是否允许响应」解耦。

**练习 2**：一个电平触发的中断源，软件在处理程序里只写 `CR_INTERRUPT_ACK` 但不撤掉外设请求，会发生什么？

**参考答案**：`CR_INTERRUPT_ACK` 只清 `interrupt_edge_latched`，而电平源的 pending 直接取 `interrupt_req` 电平（见 [L266-L267](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L266-L267)），ack 对它无效。`eret` 返回后中断使能重新打开，pending 仍为高，会立刻再次触发中断——表现为「中断风暴」。所以电平型中断必须在处理程序里让外设撤请求（io_interrupt 里重置定时器 `REG_TIMER_COUNT` 就是这个目的）。

---

### 4.3 trap 嵌套

#### 4.3.1 概念说明

处理 trap 时往往要先关中断、进特权态，处理完再 `eret` 恢复。但如果在处理 trap 的过程中又触发了新 trap（例如 TLB miss handler 自身又访存导致缺页），就需要**保存被中断的「处理中状态」**，这就是嵌套。Nyuzi 用一个「移位寄存器式」的现场栈来实现：trap 把栈整体上移（push），`eret` 把栈整体下移（pop）。

#### 4.3.2 核心流程

```
发生 trap (wb_trap):
  for level in 0..TRAP_LEVELS-2:
      trap_state[t][level+1] <= trap_state[t][level]      // 整体上移(push)
  trap_state[t][0].trap_cause  <= 本次原因
  trap_state[t][0].trap_pc     <= 被打断的 PC
  trap_state[t][0].flags.interrupt_en <= 0                 // 进 trap 即关中断
  trap_state[t][0].flags.supervisor_en <= 1                // 进 trap 即进特权态
  (若 TT_TLB_MISS) trap_state[t][0].flags.mmu_en <= 0      // 关 MMU 走物理地址

eret 返回 (wb_eret):
  for level in 0..TRAP_LEVELS-2:
      trap_state[t][level] <= trap_state[t][level+1]       // 整体下移(pop), 恢复旧 flags
```

栈深 `TRAP_LEVELS = 3`，第 0 层是「当前」，所以最多嵌套 2 层。push 时第 0 层被新现场覆盖、旧的第 0 层升到第 1 层；pop 时第 1 层降回第 0 层，自动恢复被打断时的 `flags`（包括 `interrupt_en`、`supervisor_en`、`mmu_en`）。

#### 4.3.3 源码精读

push 逻辑在主 `always_ff` 里，由 `wb_trap` 触发：

[hardware/core/control_registers.sv:161-177](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L161-L177)：

```systemverilog
if (wb_trap)
begin
    // Copy trap state
    for (int level = 0; level < TRAP_LEVELS - 1; level++)
        trap_state[wb_rollback_thread_idx][level + 1] <= trap_state[wb_rollback_thread_idx][level];

    trap_state[wb_rollback_thread_idx][0].trap_cause <= wb_trap_cause;
    trap_state[wb_rollback_thread_idx][0].trap_pc <= wb_trap_pc;
    ...
    trap_state[wb_rollback_thread_idx][0].flags.interrupt_en <= 0;   // 进 trap 关中断
    trap_state[wb_rollback_thread_idx][0].flags.supervisor_en <= 1;
    if (wb_trap_cause.trap_type == TT_TLB_MISS)
        trap_state[wb_rollback_thread_idx][0].flags.mmu_en <= 0;
end
```

两个关键设计：
1. **进 trap 自动关中断 + 进特权态**：`interrupt_en<=0` 防止处理过程被新中断打断（除非软件显式再开），`supervisor_en<=1` 让 handler 能访问特权资源。这解释了为什么 `eret` 后中断会自动恢复——因为 push 时只是把旧 `flags` 上移，pop 时原样回来。
2. **TLB miss 特判关 MMU**：TLB miss handler 要去内存里查页表填 TLB，若此时 MMU 还开着，查页表的访存又会 TLB miss，形成死循环。所以 [`TT_TLB_MISS` 时 `mmu_en<=0`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L175-L176)，让 handler 走物理地址查页表（这与 u7-l1 讲的软件管理 TLB 配合）。

pop 逻辑由 `wb_eret` 触发：

[hardware/core/control_registers.sv:179-184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L179-L184) 把栈整体下移一层，第 0 层自动恢复成「上一层保存的 flags」，从而正确回到被中断时的特权级/中断使能/MMU 状态。

注意代码里的两条断言：[`assert(!(wb_trap && wb_eret))`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L156-L157)（同一拍不会既 trap 又 eret）。此外 [`cr_eret_address`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L247) 直接取 `trap_state[t][0].trap_pc`——这正是 `eret` 指令跳回的「被打断的那条指令」地址（u2-l4）。

> 复位时 [`trap_state[t][0].flags.supervisor_en <= 1`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L124-L133)，即复位后天然处于特权态，这与「上电后第一段代码（crt0/bootrom）需要特权」一致。

#### 4.3.4 代码实践

**目标**：用源码阅读方式验证「嵌套靠移位寄存器实现、最多 2 层」，并理解为什么 TLB miss 必须特判。

**操作步骤**：
1. 在 [control_registers.sv:88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L87-L88) 确认 `TRAP_LEVELS = 3`。
2. 在 [L161-L184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L161-L184) 比对 push 和 pop 两个 `for` 循环：push 是 `[level+1] <= [level]`（向上移），pop 是 `[level] <= [level+1]`（向下移）。
3. 思考：如果中断处理程序里再次发生 `TT_TLB_MISS`，第 0 层的 `mmu_en` 会被清 0，而原来第 0 层（即第一次 trap 的现场）被推到第 1 层完整保留；`eret` 两次后能恢复到最初的用户态 `flags`。

**观察现象 / 预期结果**：能口述出「第 0 层=当前、1/2 层=嵌套现场」的对应关系，并解释 `CR_SAVED_FLAGS`（读 `trap_state[t][1].flags`）为什么读到的是「上一层」的 flags。

> 本节为源码阅读型实践，无需运行；如想运行验证嵌套，可参考 `tests/cosimulation/interrupt.S`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么进 trap 时硬件强制 `interrupt_en<=0`，而不是让软件自己关？

**参考答案**：保证 trap 处理的「入口段」不会被新中断打断，避免在还没保存好现场（如还没 push 栈、还没设好 handler）时又来一次 trap 导致状态丢失。硬件强制关中断后，软件在确保现场安全后可以再 `setcr CR_FLAGS` 显式开中断来支持中断嵌套。

**练习 2**：若嵌套深度超过 2 层会怎样？

**参考答案**：栈只有 3 层（第 0/1/2），第 3 次 push 会把原第 1 层（已是「最老」的现场）挤出丢失，导致最深层 `eret` 时无法恢复最初的现场。设计上把最大嵌套定为 `TRAP_LEVELS - 1 = 2`，软件应避免在不可重入的 handler 内再触发更深 trap。

---

### 4.4 线程状态：supervisor / MMU / ASID 与挂起恢复

#### 4.4.1 概念说明

除了「最近一次 trap 的现场」，每个线程还有一组「长期运行态」需要独立维护：

- **supervisor_en / mmu_en**：当前特权级与是否启用虚拟地址翻译（从 `trap_state[t][0].flags` 取，会随 trap 进出自动切换）。
- **ASID**（address space id，8 位）：标识当前线程属于哪个地址空间，让多个线程的 TLB 表项共存而不串扰（u7-l1）。
- **page_dir_base**：当前线程页目录的物理基址。

此外还有一个跨核的「线程使能」机制：软件可以通过写 `CR_SUSPEND_THREAD` / `CR_RESUME_THREAD` 把某个线程挂起或唤醒，这是 libos 做并行调度、程序停机的基础（u9-l2、u1-l4）。

#### 4.4.2 核心流程

```
每线程运行态(本核内):
  cr_supervisor_en[t] = trap_state[t][0].flags.supervisor_en   ──► 送各流水级做权限检查
  cr_mmu_en[t]        = trap_state[t][0].flags.mmu_en           ──► 送 dcache/ifetch 决定是否翻译
  cr_current_asid[t]  = CR_CURRENT_ASID 写入的低 8 位             ──► 送 TLB 参与 hit 判定
  page_dir_base[t]    = CR_PAGE_DIR

线程挂起/恢复(跨核全局):
  软件写 CR_SUSPEND_THREAD (TOTAL_THREADS 位) ──► cr_suspend_thread
  软件写 CR_RESUME_THREAD  (TOTAL_THREADS 位) ──► cr_resume_thread
        │ (顶层 nyuzi 把各核的位图 OR 起来)
        ▼
  thread_en <= (thread_en | resume_mask) & ~suspend_mask   ──► 作为取指/调度的总使能
```

注意位宽差别：运行态是**核内每线程**（`THREADS_PER_CORE` 套），而 suspend/resume 是**全芯片每线程**（`TOTAL_THREADS` 位），因为顶层要把多个核的请求汇成一个全局 `thread_en`。

#### 4.4.3 源码精读

**(a) 运行态输出**：每线程的三个使能直接从第 0 层 flags 连出：

[hardware/core/control_registers.sv:243-247](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L243-L247)：

```systemverilog
assign cr_interrupt_en[thread_idx] = trap_state[thread_idx][0].flags.interrupt_en;
assign cr_supervisor_en[thread_idx] = trap_state[thread_idx][0].flags.supervisor_en;
assign cr_mmu_en[thread_idx] = trap_state[thread_idx][0].flags.mmu_en;
assign cr_eret_subcycle[thread_idx] = trap_state[thread_idx][0].trap_subcycle;
assign cr_eret_address[thread_idx] = trap_state[thread_idx][0].trap_pc;
```

这意味着 `supervisor_en` / `mmu_en` / `interrupt_en` 都会随 4.3 的 trap push/pop 自动切换——`eret` 返回用户态时，`supervisor_en` 自动变 0，无需软件再写 `CR_FLAGS`。`cr_current_asid` 在 [写逻辑 L203](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L203) 截 `ASID_WIDTH`（=8，见 [defines.svh:295](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L295)）位写入，切进程时软件换 ASID 即可（u7-l1）。

**(b) suspend / resume 跨核聚合**：模块输出 `cr_suspend_thread`/`cr_resume_thread` 是 `TOTAL_THREADS` 位：

[hardware/core/control_registers.sv:208-L209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L208-L209) 把写值展开到全芯片位宽。顶层 `nyuzi` 收集所有核的这两组信号：

[hardware/core/nyuzi.sv:76-L94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L76-L94)：

```systemverilog
always @* begin
    thread_suspend_mask = '0;
    thread_resume_mask = '0;
    for (int i = 0; i < `NUM_CORES; i++) begin
        thread_suspend_mask |= core_suspend_thread[i];
        thread_resume_mask |= core_resume_thread[i];
    end
end
always_ff @(posedge clk, posedge reset) begin
    if (reset) thread_en <= 1;
    else thread_en <= (thread_en | thread_resume_mask) & ~thread_suspend_mask;
end
```

即「或上 resume、减去 suspend」得到下一拍的 `thread_en`。复位时 `thread_en <= 1`，只有**线程 0**被使能，其余线程需由软件显式 `CR_RESUME_THREAD` 唤醒——这正是 libos `parallelExecute` 与 crt0 `_start` 的协作基础：线程 0 跑起来后唤醒其它线程（u9-l2、u1-l4）。

**(c) 一个跨核 suspend/resume 的真实用法**：[tests/asm_macros.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h) 给出两个常用宏：

- [`start_all_threads`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L80-L83)：写 `0xffffffff` 到 `CR_RESUME_THREAD`，唤醒全部线程。
- [`halt_all_threads`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L93-L97)：写 `0xffffffff` 到 `CR_SUSPEND_THREAD`，停掉全部线程（这就是程序结束停机的方式，见 u1-l4 的 `CR_SUSPEND_THREAD` 写 -1）。

#### 4.4.4 代码实践

**目标**：验证「复位只使能线程 0、其余需唤醒」，并理解 suspend/resume 如何变成全局停机。

**操作步骤**：
1. 阅读 [nyuzi.sv:88-L93](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L88-L93) 的 `thread_en` 复位值与更新式。
2. 阅读 [asm_macros.h 的 `start_all_threads` / `halt_all_threads`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L80-L97)。
3. 思考停机链路：`halt_all_threads` 写全 1 到 `CR_SUSPEND_THREAD` → 各核 `cr_suspend_thread` 全 1 → 顶层 `thread_suspend_mask` 全 1 → `thread_en` 被清成 0 → 没有任何线程可取指 → 整个模拟器/仿真进入空闲，触发退出（u1-l4）。

**观察现象 / 预期结果**：能解释「为什么多线程程序里线程 0 必须显式唤醒其它线程它们才会跑」——因为复位 `thread_en=1` 只置了 bit0。

> 本节为源码阅读型实践；若要在模拟器观察多线程唤醒，可参考 u9-l2 的 `parallelExecute` 实践（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`cr_supervisor_en` 为什么直接从 `trap_state[t][0].flags` 取，而不是单独设一个寄存器？

**参考答案**：因为特权级天然属于「当前运行态」，而 trap push/pop 已经在维护 flags 栈了。把 `supervisor_en` 放进 flags，进 trap 自动置 1、`eret` 自动恢复，省去单独的状态机和软件负担。如果单独设寄存器，软件就得在每次 trap 进出时手动保存恢复特权级，容易出错。

**练习 2**：为什么 `CR_SUSPEND_THREAD` 用 `TOTAL_THREADS` 位宽而不是 `THREADS_PER_CORE`？

**参考答案**：因为软件（如 libos 调度器）用全局线程号标识线程，可能想挂起别的核上的线程；而 `control_registers` 模块只看到一个核，无法直接寻址其它核的线程。于是把 suspend/resume 做成全芯片位宽、经顶层 `nyuzi` 聚合（OR）后作用到全局 `thread_en`，任一核写出对应bit都能挂起目标线程。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「端到端中断追踪」。

**任务**：用一张图把一次硬件定时器中断的完整生命周期的信号通路画出来，并在每个节点标注对应的源码位置。

建议的图结构（文字版流程图）：

```
[顶层] interrupt_req[1] 拉高 (定时器)
   │
   ▼ (4.2) control_registers.sv L221-272
   interrupt_edge → interrupt_edge_latched → interrupt_pending[t]
   → & interrupt_mask (CR_INTERRUPT_ENABLE=2) → cr_interrupt_pending[t]=1
   │
   ▼ (4.2) instruction_decode_stage.sv L279-281
   cr_interrupt_pending & cr_interrupt_en(全局开) & ~两段式进行中
   → raise_interrupt → has_trap → 指令替换为 TT_INTERRUPT (副作用全抑制)
   │
   ▼ (4.2) writeback_stage.sv L177-199
   空壳指令到写回级 → wb_trap=1, wb_rollback_pc=cr_trap_handler
   │
   ▼ (4.3) control_registers.sv L161-177
   trap_state 栈 push: 关中断(interrupt_en<=0)、进特权态(supervisor_en<=1)
   │
   ▼ (4.4) control_registers.sv L243-245
   cr_supervisor_en 变 1 → handler 可访问特权寄存器
   │
   ▼ handler 执行: getcr CR_INTERRUPT_PENDING → setcr CR_INTERRUPT_ACK(=2) 清边沿锁存
   │
   ▼ eret (writeback_stage.sv L223-227)
   wb_eret=1 → trap_state 栈 pop → 恢复原 flags(interrupt_en/supervisor_en)
   → cr_eret_address(=trap_pc) 作为回滚 PC → 回到被中断的指令继续执行
```

**验证方式**：
1. 逐节点对照本讲引用的源码行号，确认每个箭头的逻辑真实存在。
2. 在每个节点旁注明它属于哪个最小模块（存储 / 中断处理 / trap 嵌套 / 线程状态）。
3. 用一两句话解释「为什么这条通路能保证中断精确」——答案应包含「替换发生在解码边界、原指令副作用被 `has_trap` 抑制、退役时才保存现场并跳转」。

> 若本地有 verilator/emulator 环境，可运行 `io_interrupt` 并用波形（或 `-v` 跟踪）观察一次中断发生时上述信号的变化；否则作为源码阅读型综合练习完成。

## 6. 本讲小结

- `control_registers` 是单核的状态中枢：大部分控制寄存器**按线程独立存储**（数组下标为线程号），用 `trap_state[thread][level]` 三维数组集中管理 trap 现场。
- 中断检测是组合链路：`interrupt_req` → 边沿检测 → 按电平/边沿选择 → 与 `interrupt_mask` 相与 → 归约成 `cr_interrupt_pending`；**屏蔽字在控制寄存器里相与，全局使能位留到解码级相与**，从而关中断时仍可查询挂起。
- 精确中断靠「解码级把指令替换成 `TT_INTERRUPT` 空壳并抑制其全部副作用」，且在两段式指令（IO/sync）进行中不插入中断。
- trap 嵌套用移位寄存器栈：`wb_trap` 整体上移（push，并自动关中断+进特权态，TLB miss 额外关 MMU），`wb_eret` 整体下移（pop，自动恢复 flags），栈深 3、最多嵌套 2 层。
- 线程运行态 `supervisor_en`/`mmu_en`/`interrupt_en` 直接取自第 0 层 flags，随 trap 进出自动切换；`suspend`/`resume` 用全芯片位宽，经顶层 `nyuzi` 聚合成 `thread_en`，复位时只使能线程 0。

## 7. 下一步学习建议

- **u7-l3 Trap 处理与回滚**：本讲聚焦 `control_registers` 内部的存储与中断检测，下一篇将把视角放到 `writeback_stage`，讲清 trap/eret/分支的回滚仲裁、精确异常在乱序退役下的保证，以及 syscall 派发。
- **u11-l2 性能计数器与 profiling**：本讲提到 `CR_PERF_EVENT_SELECT*` / `CR_PERF_EVENT_COUNT*` 与 `perf_event_count`，下一篇会展开性能事件的来源与采样剖析流程。
- **u12-l1 内核启动与陷阱/系统调用**：本讲是硬件侧的中断/trap 机制，内核篇将展示软件侧如何设置 `CR_TRAP_HANDLER`、编写 `trap_entry.S` 与处理 `TT_INTERRUPT`/`TT_SYSCALL`，把硬件机制用起来。
- 继续阅读建议：[hardware/core/core.sv 的 control_registers 实例化](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L379-L388)，确认本模块如何用 `.*` 接入单核；以及 [tests/core/trap/ 目录下的其它 .S 测试](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/trap)（如 `syscall.S`、`eret_non_super.S`），从测试断言反推控制寄存器行为。
