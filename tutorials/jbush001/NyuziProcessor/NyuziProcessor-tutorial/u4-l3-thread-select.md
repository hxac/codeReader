# 线程选择与记分牌

## 1. 本讲目标

本讲聚焦 Nyuzi 单核流水线的「发射控制中枢」——`thread_select_stage`。学完后你应该能够：

- 说清楚 `thread_select_stage` 在流水线中的位置，以及它「每周期从多个硬件线程里挑一条指令发射」的核心职责。
- 解释每线程指令 FIFO 如何把「解码速率」与「发射速率」解耦。
- 掌握轮询调度（round robin）如何让等内存的线程不阻塞其他线程，从而隐藏延迟。
- 读懂 `scoreboard` 如何用一张 64 位位图跟踪寄存器依赖，并据此阻止 RAW / WAW 冒险。
- 理解「写回结构冒险」：整数、访存、浮点三条长度不同的执行路径为何会在写回级撞车，以及 4 位移位寄存器如何规避它。
- 理解缓存缺失时线程如何被挂起、又如何在 L2/IO 响应回来后被唤醒。

## 2. 前置知识

本讲建立在前面几讲之上，先用通俗语言补齐两个关键概念。

### 2.1 流水线位置回顾

回顾 [u3-l2 单核流水线总览](u3-l2-core-pipeline.md)：指令在单核内依次流经

```
取指标签 → 取指数据 → 解码 → 【线程选择】 → 操作数 fetch → 执行 → 写回
```

本讲的主角就是被方括号标出的「线程选择」级。它从解码级接收已经翻译好的 `decoded_instruction_t`（见 [u4-l2 指令解码](u4-l2-instruction-decode.md)），决定**这一周期把哪一条指令送进操作数 fetch 级**。

### 2.2 什么是数据冒险（hazard）

「冒险」是指前后两条指令因为共享寄存器而必须保持的先后顺序。常见的有三种，记住它们对读懂记分牌至关重要：

- **RAW（Read After Write，写后读）**：`add s0, s1, s2` 之后紧跟 `add s3, s0, s4`。第二条要读 `s0`，但 `s0` 的新值还没算出来，必须等第一条写回。
- **WAW（Write After Write，写后写）**：两条指令都要写同一个寄存器，后写的不能比先写的先落到寄存器堆，否则最终值就错了。
- **WAR（Write After Read，读后写）**：一条指令要先读某寄存器，另一条指令要写它。先读的必须真正读到旧值。

Nyuzi 是**多线程**处理器：默认每核 `THREADS_PER_CORE = 4` 个硬件线程，寄存器按线程分体（见 [defines.svh:L42-L52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L42-L52)）。冒险**只发生在同一线程的前后指令之间**——不同线程用的是各自的寄存器，互不干扰。这一点是后续所有调度逻辑的前提。

### 2.3 位图：一位一线程 / 一位一寄存器

Nyuzi 大量用「位图（bitmap）」表示集合：

- `local_thread_bitmap_t`（[defines.svh:L49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L49)）：宽度等于线程数，第 i 位为 1 表示「线程 i 在集合里」。
- 记分牌内部还会用一张 64 位位图，第 i 位为 1 表示「寄存器 i 正在被某条在飞指令占用、尚待写回」。

理解了「位图 + 按位与（&）做集合求交」，本讲的源码就豁然开朗。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hardware/core/thread_select_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv) | 本讲主角。每线程指令 FIFO、轮询调度、写回冒险移位寄存器、缓存缺失挂起/唤醒。 |
| [hardware/core/scoreboard.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv) | 记分牌。用位图跟踪寄存器依赖，每个线程独占一个实例。 |
| [hardware/core/rr_arbiter.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv) | 轮询仲裁器，从「可发射线程位图」里公平挑一个。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 提供 `decoded_instruction_t`、`pipeline_sel_t`、各类位宽类型。 |
| [tests/core/isa/waw.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/waw.S) | 真实的「写后写」功能测试，本讲综合实践的样本。 |

模块头注释本身就把职责概括得很清楚，建议先读一遍：[thread_select_stage.sv:L21-L32](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L21-L32)。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：**指令 FIFO、轮询调度、记分牌冒险、写回冒险、缓存缺失挂起**。它们共同回答一个问题——「这一周期到底该发射哪条指令，或者干脆别发射」。

### 4.1 指令 FIFO：每线程一个解码指令缓冲

#### 4.1.1 概念说明

解码级每个周期可能为**某一个**线程产出一条 `decoded_instruction_t`。但线程选择级不一定能立刻把它发射出去——可能正在发射别的线程，也可能这条指令正卡在冒险上。如果解码结果无处安放，整条前端就得停摆。

解决办法是给**每个线程配一个独立的小 FIFO**，把解码产物按线程分门别类地暂存起来。这样「解码速率」和「发射速率」就被解耦了：解码可以连续往里塞，发射端按自己的节奏从各自 FIFO 里取。这就是 `thread_select_stage` 名字里「thread」的由来——它管理的是**多个线程各自的指令流**。

#### 4.1.2 核心流程

```text
解码级 (id_instruction, id_thread_idx, valid)
        │  按 id_thread_idx 分发
        ▼
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ 线程0 FIFO   │ 线程1 FIFO   │ 线程2 FIFO   │ 线程3 FIFO   │
└──────┬───────┴──────┬───────┴──────┬───────┴──────┬───────┘
       │ 各自的队头指令 thread_instr[i]                │
       └──────────────┬───────────────────────────────┘
                      ▼
            交给「轮询调度 + 冒险检查」挑一个发射
```

每个 FIFO 的关键行为：

1. **入队**：当解码级输出有效、且 `id_thread_idx` 等于本线程号时入队。
2. **反压取指**：FIFO 接近满时，向取指级拉低 `ts_fetch_en`，让它别再往解码送指令了。
3. **出队**：当本线程指令的**最后一个 subcycle** 被发射出去时才出队（多 subcycle 指令如 gather load 会在队头停留多个周期）。
4. **冲刷**：发生分支回滚/trap 时，整条 FIFO 清空，丢弃错误路径上的指令。

#### 4.1.3 源码精读

模块用 `generate` 循环为每个线程实例化一组逻辑，FIFO 与记分牌都在这个循环里：[thread_select_stage.sv:L104-L146](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L104-L146)。

入队判定只认「解码有效且线程号匹配」：

```systemverilog
assign enqueue_this_thread = id_instruction_valid
    && id_thread_idx == local_thread_idx_t'(thread_idx);
```
> [thread_select_stage.sv:L116-L117](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L116-L117)　按线程号把解码指令路由进对应 FIFO。

FIFO 容量为 8，并把「几乎满」阈值设为 5（`SIZE - 3`）：

```systemverilog
sync_fifo #(
    .WIDTH($bits(id_instruction)),
    .SIZE(THREAD_FIFO_SIZE),                 // THREAD_FIFO_SIZE = 8
    .ALMOST_FULL_THRESHOLD(THREAD_FIFO_SIZE - 3)
) instruction_fifo(
    .flush_en(rollback_this_thread),         // 回滚时整条冲刷
    .almost_full(ififo_almost_full),
    .enqueue_en(enqueue_this_thread),
    .dequeue_en(issue_last_subcycle[thread_idx]), // 最后一个 subcycle 发射后才出队
    .dequeue_value(thread_instr[thread_idx]),
    .*);
```
> [thread_select_stage.sv:L119-L133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L119-L133)　每线程一个 `sync_fifo`；`THREAD_FIFO_SIZE` 定义在 [L74](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L74)。

反压取指用「几乎满」而非「满」，是因为取指与线程选择之间还隔着好几级流水线，要提前几个周期打招呼：

```systemverilog
assign ts_fetch_en[thread_idx] = !ififo_almost_full && thread_en[thread_idx];
```
> [thread_select_stage.sv:L148-L151](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L148-L151)　FIFO 还有 ≥3 个空位时才允许继续取指，留出流水线级间的缓冲。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解 FIFO 容量与反压阈值的关系。
2. **步骤**：打开 `thread_select_stage.sv`，定位 `THREAD_FIFO_SIZE`（[L74](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L74)）与 `ALMOST_FULL_THRESHOLD`（[L122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L122)）。
3. **观察现象**：阈值是 `8 - 3 = 5`，即 FIFO 里已有 5 条时就反压。
4. **预期结果**：你能用一句话解释「为何不用 `!full` 而用 `!almost_full`」——因为取指标签级、取指数据级、解码级都在线程选择之前，从「停止取指」到「真正不再入队」之间还有几个周期在路上的指令，必须提前留余量。
5. 若想验证余量是否够，可数一下取指到线程选择之间的流水级数，与「3」比较。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FIFO 的出队条件是 `issue_last_subcycle` 而不是「每次发射」？
> **答案**：像 gather/scatter 这类向量访存指令要占用 16 个 subcycle 逐通道执行，会在队头停留多个周期。只有走完最后一个 subcycle，这条指令才算真正发射完毕，此时才能出队让下一条指令露头。

**练习 2**：如果回滚时不清空 FIFO 会怎样？
> **答案**：错误路径上已经解码、但尚未发射的指令会残留在 FIFO 里，线程恢复后会把它们当成有效指令继续执行，导致语义错误。所以回滚必须 `flush_en`（[L124](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L124)）。

---

### 4.2 轮询调度：公平地挑一个可发射线程

#### 4.2.1 概念说明

每核虽然有 4 个线程，但**每个周期只发射一条指令**（只有一条进入操作数 fetch 的发射槽）。于是必须有个仲裁器，从「这一周期哪些线程可以发射」的位图里挑出一个。

Nyuzi 选的是**轮询（round robin）**：记住上一轮把发射权给了谁，这一轮就从下一个线程开始找，保证每个就绪线程都能轮到，不会被某个繁忙线程饿死。轮询调度是多线程处理器隐藏内存延迟的关键武器——某个线程因为缓存缺失卡住时，仲裁器自然跳过它，把发射槽让给其他就绪线程，流水线不会空转。

#### 4.2.2 核心流程

```text
每个线程算出一个 can_issue 位（综合 6 个条件，见 4.2.3）
        │
        ▼
   can_issue_thread  (位图，1 = 可发射)
        │
        ▼
   rr_arbiter 轮询挑一位 → grant_oh (独热码)
        │
        ▼
   oh_to_idx 转成线程号 issue_thread_idx
        │
        ▼
   issue_instr = thread_instr[issue_thread_idx]
   下一拍送到操作数 fetch 级
```

「独热码（one-hot）」是指 N 位信号里最多只有一位为 1。仲裁器输出独热的 `grant_oh`，再用 `oh_to_idx` 把它翻译成普通的二进制线程号。

#### 4.2.3 源码精读

「可发射」是把 6 个条件按位与起来的结果，是本模块最核心的一行逻辑：

```systemverilog
assign can_issue_thread[thread_idx] = !ififo_empty
    && (scoreboard_can_issue || current_subcycle[thread_idx] != 0)  // 依赖或子周期
    && thread_en[thread_idx]                                         // 线程被软件使能
    && !rollback_this_thread                                         // 本周期没在回滚
    && !writeback_conflict                                           // 不会撞写回
    && !thread_blocked[thread_idx];                                  // 没因缺失被挂起
```
> [thread_select_stage.sv:L169-L174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L169-L174)　六个条件全部满足，这个线程才进入仲裁。

注意第二个条件里的「或」：记分牌只在**第一个 subcycle** 检查（见 4.3），多 subcycle 指令的后续 subcycle 用 `current_subcycle != 0` 放行，避免被记分牌误拦。

仲裁器实例化非常简洁：

```systemverilog
rr_arbiter #(.NUM_REQUESTERS(`THREADS_PER_CORE)) thread_select_arbiter(
    .request(can_issue_thread),
    .update_lru(1'b1),
    .grant_oh(thread_issue_oh),
    .*);
```
> [thread_select_stage.sv:L237-L241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L237-L241)　`update_lru=1` 表示「获得授权的线程这一轮用过之后，要排到队尾，等其他线程都轮过再来」。

仲裁器内部用双重循环判断「在优先级最高的请求者中，谁该被授权」：[rr_arbiter.sv:L41-L60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L41-L60)。优先级指针每轮「向左旋转」一位，实现轮询：

```systemverilog
assign priority_oh_nxt = {grant_oh[NUM_REQUESTERS - 2:0], grant_oh[NUM_REQUESTERS - 1]};
```
> [rr_arbiter.sv:L62-L64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/rr_arbiter.sv#L62-L64)　把刚被授权的那一位旋转到最低优先级，下一轮从它的下一个线程开始找。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把 `can_issue_thread` 的 6 个条件对号入座到「等什么」。
2. **步骤**：对照 [L169-L174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L169-L174)，填写下表。
3. **观察现象 / 预期结果**：

   | 条件 | 含义 | 对应「等待什么」 |
   |------|------|------------------|
   | `!ififo_empty` | FIFO 里有指令 | 等取指/解码（空了说明还没取到） |
   | `scoreboard_can_issue` | 无寄存器依赖 | 等 **RAW/WAW** 依赖 |
   | `thread_en` | 软件使能了该线程 | （被软件主动停用） |
   | `!rollback_this_thread` | 本周期没在回滚 | （正在刷新，暂停一拍） |
   | `!writeback_conflict` | 不会撞写回 | 等**写回结构冒险**（见 4.4） |
   | `!thread_blocked` | 没被挂起 | 等**缓存缺失/IO 响应**（见 4.5） |

4. 只要有任何一个条件不满足，该线程位就是 0，仲裁器自然跳过它。

#### 4.2.5 小练习与答案

**练习 1**：4 个线程里只有线程 2 因缓存缺失被挂起，其余 3 个都就绪。这一周期会发射谁？
> **答案**：线程 2 的 `can_issue` 位为 0（`thread_blocked` 为真），仲裁器只在 {0,1,3} 三位里轮询，挑出优先级最高的那一个发射。线程 2 不会拖慢其他人。

**练习 2**：`update_lru` 如果固定接 0 会发生什么？
> **答案**：优先级指针不再旋转，仲裁器会一直偏向同一个低编号就绪线程，高编号线程可能被**饿死**。轮询公平性依赖 `update_lru=1`。

---

### 4.3 记分牌冒险：用位图跟踪寄存器依赖

#### 4.3.1 概念说明

「记分牌（scoreboard）」是一种经典的冒险检测机制。它的直觉很简单：

> 给每个寄存器配一个「忙碌」标志。一条指令发射时，把它**目的寄存器**标成忙碌；等它把结果写回寄存器堆，标志清掉。下一条指令发射前，先看自己要读、要写的寄存器有没有正忙碌的——有就等。

这样 RAW 和 WAW 就被自然挡住：RAW 是「我要读的源寄存器正被别人写」，WAW 是「我要写的目的寄存器正被别人写」。两种情况都会在位图里表现为「位图求交非零」。

需要特别说明 **WAR**：Nyuzi 的记分牌**不显式跟踪读**（源寄存器不置位），WAR 由流水线结构天然规避——因为源操作数在「操作数 fetch」级（紧跟发射之后的那一拍）就读走了，远早于任何后继指令的写回。所以「先读的必然先读到旧值」，无需额外检测。

记分牌是**每线程独占一个**实例（不同线程寄存器互不相关），实例化在 4.1 的 generate 循环里：[thread_select_stage.sv:L141-L146](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L141-L146)。

#### 4.3.2 核心流程

记分牌内部维护一张位图 `scoreboard_regs`，核心是三组按位运算：

```text
检查: can_issue = (scoreboard_regs AND dep_bitmap) == 0
                ^若任一相关寄存器正忙碌，结果非零 → 不能发射

置位: set_bitmap  = dest_bitmap AND will_issue    // 本周期若发射，把目的置忙碌
清位: clear_bitmap = rollback_bitmap OR writeback_bitmap  // 写回或回滚时清忙碌

更新: scoreboard_regs_next = (scoreboard_regs AND NOT clear_bitmap) OR set_bitmap
```

其中：

- `dep_bitmap`：下一条指令**所有相关寄存器**（目的 + 各源）对应的位。
- `dest_bitmap`：仅目的寄存器对应的位。
- `writeback_bitmap`：本周期写回完成的那个寄存器。
- `rollback_bitmap`：回滚时要清掉的那些寄存器（见下文）。

#### 4.3.3 源码精读

位图宽度 = 寄存器数 × 2，因为标量和向量各占 32 个槽：[scoreboard.sv:L49-L59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L49-L59)。用 6 位扩展编号 `ext_register_idx_t`：0–31 是标量，32–63 是向量。

`dep_bitmap` 把指令的源和目的全部置位：

```systemverilog
dep_bitmap = 0;
if (next_instruction.has_dest)
    dep_bitmap[{next_instruction.dest_vector, next_instruction.dest_reg}] = 1;
if (next_instruction.has_scalar1)
    dep_bitmap[{1'b0, next_instruction.scalar_sel1}] = 1;
if (next_instruction.has_scalar2)
    dep_bitmap[{1'b0, next_instruction.scalar_sel2}] = 1;
if (next_instruction.has_vector1)
    dep_bitmap[{1'b1, next_instruction.vector_sel1}] = 1;
if (next_instruction.has_vector2)
    dep_bitmap[{1'b1, next_instruction.vector_sel2}] = 1;
```
> [scoreboard.sv:L96-L119](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L96-L119)　最高位 1 表示向量、0 表示标量，巧妙地把 32+32 个寄存器压进一张 64 位位图。

最终的检查、置位、清位、更新四行是记分牌的全部精髓：

```systemverilog
assign clear_bitmap = rollback_bitmap | writeback_bitmap;
assign set_bitmap   = dest_bitmap & {SCOREBOARD_ENTRIES{will_issue}};
assign scoreboard_regs_nxt = (scoreboard_regs & ~clear_bitmap) | set_bitmap;
assign scoreboard_can_issue = (scoreboard_regs & dep_bitmap) == 0;
```
> [scoreboard.sv:L147-L152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L147-L152)　`can_issue` 是位图求交——只要相关寄存器有一个正忙碌，就不能发射。

**回滚时为什么不直接清空整张位图？** 注释解释得很清楚（[scoreboard.sv:L72-L78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L72-L78)）：分支或 trap 回滚时，**比回滚点更早 issued、且仍在飞**的指令（尤其是浮点流水线里那些不会触发 trap 的指令）应该继续退休，它们的目的寄存器仍要标记为忙碌。只有**晚于回滚点**、要被冲刷的指令，其目的位才该清掉。代码用一个深度为 `ROLLBACK_STAGES = 4` 的移位寄存器 `has_writeback` / `writeback_reg` 记录最近几条带目的的指令，回滚时只清那些「年龄小于回滚点」的位：[scoreboard.sv:L79-L94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L79-L94) 与 [L154-L176](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L154-L176)。

`ROLLBACK_STAGES` 取 4，对应「操作数 fetch → dcache_tag → dcache_data → 写回」这条最长非浮点路径（浮点不产生 trap，故不计入），见 [scoreboard.sv:L51-L56](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L51-L56)。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：亲手用位图推演一次 RAW 阻塞。
2. **步骤**：假设线程 0 顺序发射两条指令
   - `add_i s3, s1, s2`（写 s3）
   - `add_i s5, s3, s4`（读 s3，写 s5）
3. **推演**：
   - 第 1 条发射：`set_bitmap` 第 3 位置 1（s3 忙碌）。
   - 第 2 条的 `dep_bitmap`：s3（源）+ s5（目的）→ 第 3、5 位置 1。
   - `can_issue = (scoreboard_regs & dep_bitmap) == 0`：第 3 位相交为 1 → **不能发射**。
   - 若干周期后第 1 条写回 s3：`writeback_bitmap` 第 3 位置 1 → `clear_bitmap` 把它清掉 → 第 2 条的 `can_issue` 变 1 → 可发射。
4. **预期结果**：第 2 条指令确实被推迟到第 1 条写回之后才发射。这正是 [tests/core/isa/waw.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/waw.S) 这类测试要验证的行为。

#### 4.3.5 小练习与答案

**练习 1**：记分牌为什么是「每线程一个」而不是全核共享？
> **答案**：冒险只存在于同一线程的前后指令之间，不同线程用各自独立的寄存器堆分体。每线程一个记分牌，既正确（不会误判跨线程依赖），又简单（无需在位图里再编码线程号）。

**练习 2**：`scoreboard_can_issue` 用 `(A & B) == 0` 判断，等价的逻辑含义是什么？
> **答案**：「下一条指令的相关寄存器集合 `B`，与当前忙碌寄存器集合 `A`，没有交集」。只要有交集就说明它依赖的某个寄存器还没就绪。

**练习 3**：为什么 `can_issue_thread` 里要写成 `scoreboard_can_issue || current_subcycle != 0`？
> **答案**：记分牌只在第一个 subcycle 检查（[L164-L168 注释](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L164-L168)）。多 subcycle 指令（如 gather load）从第 2 个 subcycle 起靠 `current_subcycle != 0` 放行，否则会被自己上一拍的目的位卡死。

---

### 4.4 写回冒险：跨不同长度流水线的结构冒险

#### 4.4.1 概念说明

操作数 fetch 之后，指令分三条路走（见 [u3-l2](u3-l2-core-pipeline.md)）：整数（最短）、访存（居中）、浮点（最长）。三条路的**长度不同**，最终又都汇合到同一个写回级。这就带来一个「结构冒险（structural hazard）」：

> 两条在**不同周期**发射的指令，可能因为各自路径长短不一，**在同一个周期到达写回级**，挤同一根写回槽。

例如一条慢吞吞的浮点指令和一条后发射但很快的整数指令，可能在同一拍撞在写回级。解决办法是：发射时就「预约」未来几拍写回槽的使用，若新指令会撞上已预约的槽，就推迟发射。

注意：这种检查对**所有指令**都做，哪怕它不写寄存器（比如 store 指令）。因为写回级除了写寄存器堆，还要处理异常、回滚等副作用，两条指令不能同时挤进写回级。

#### 4.4.2 核心流程

模块用一个 `WRITEBACK_ALLOC_STAGES = 4` 位的移位寄存器 `writeback_allocate` 当作「未来写回槽预约表」：

```text
writeback_allocate[ i ] = 1  表示「i+1 个周期后将有一條指令到达写回级」
每过一周期，所有位向低位移动一格（bit[i] → bit[i-1]），最低位代表「下下拍到达」
```

- 位宽为何是 4？模块注释说它是「最长与最短执行流水线的差」：[thread_select_stage.sv:L76-L77](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L76-L77)。浮点最长、整数最短，二者到写回的延迟跨度正是 4，所以 4 位就能覆盖所有可能的「追尾」情况。

发射时的预约规则（从代码直接读出）：

| 当前发射的指令类型 | 预约的位 |
|----|----|
| `PIPE_FLOAT_ARITH`（浮点，最长） | bit[3]（最远） |
| `PIPE_MEM`（访存） | bit[0]（最近） |
| `PIPE_INT_ARITH`（整数，最短） | 不占位 |

检查时的规则（看新指令会不会撞上已预约的槽）：

| 想发射的指令类型 | 检查的位 |
|----|----|
| `PIPE_INT_ARITH` | bit[0] |
| `PIPE_MEM` | bit[1] |
| `PIPE_FLOAT_ARITH` | 不检查 |

整数「只查不占」、浮点「只占不查」并非疏漏，而是由「最长 / 最短」决定的对称性：整数最短，它是别人可能追尾的对象；浮点最长，没有任何更长的路径会从后面追上它。预约位每周期向低位漂移，正好模拟「这条指令离写回越来越近」。

> **说明**：以上 bit 编号与「占/查」关系完全来自源码（见 4.4.3）。具体的绝对周期数取决于实际流水线级数，本讲以代码可读出的预约规则为准；想得到精确到周期的时序图，**待本地用波形或 trace 验证**。

#### 4.4.3 源码精读

预约表的更新：每拍整体右移一位，新发射的浮点填 bit[3]、访存填 bit[0]：

```systemverilog
writeback_allocate_nxt = {1'b0, writeback_allocate[WRITEBACK_ALLOC_STAGES - 1:1]};
if (|thread_issue_oh) begin
    unique case (issue_instr.pipeline_sel)
        PIPE_FLOAT_ARITH: writeback_allocate_nxt[3] = 1'b1;
        PIPE_MEM:         writeback_allocate_nxt[0] = 1'b1;
        default: ;   // PIPE_INT_ARITH 不占位
    endcase
end
```
> [thread_select_stage.sv:L220-L232](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L220-L232)　`{1'b0, writeback_allocate[3:1]}` 即整体右移一位。

每个线程检查自己队头指令是否撞写回：

```systemverilog
unique case (thread_instr[thread_idx].pipeline_sel)
    PIPE_INT_ARITH: writeback_conflict = writeback_allocate[0];
    PIPE_MEM:       writeback_conflict = writeback_allocate[1];
    default:        writeback_conflict = 0;   // PIPE_FLOAT_ARITH 不检查
endcase
```
> [thread_select_stage.sv:L153-L162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L153-L162)　`writeback_conflict` 随后作为 `can_issue_thread` 的一项（见 4.2.3）。

`pipeline_sel_t` 三值枚举定义在 [defines.svh:L233-L237](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L233-L237)，由解码级填入每条指令（[u4-l2](u4-l2-instruction-decode.md)）。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：用真实测试理解「短指令紧跟长指令、同写一个寄存器」的写回冲突。
2. **步骤**：打开 [tests/core/isa/waw.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/waw.S)，阅读其头部注释与核心两行：

   ```assembly
   mull_i s3, s1, s2     # 长延迟（整数乘法走浮点乘法器，见 u2-l2）
   add_i  s3, s3, s4     # 短延迟，且与上一条 WAW（都写 s3）+ RAW（读 s3）
   ```
   > [waw.S:L46-L48](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/waw.S#L46-L48)

3. **观察现象**：测试先用 8 个 `nop` 清空流水线（[L37-L44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/waw.S#L37-L44)），确保冲突只来自这两条指令本身。
4. **预期结果**：`add_i` 必须等 `mull_i` 写回 s3 之后才能发射（记分牌挡住 RAW/WAW），最终 s3 = 17×19 + 9 = 332... 实际测试用 `cmpeq_i s6, s3, s5`（s5 预置为 14，见 [L34](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/waw.S#L34)）来判定，再据此调用 `pass_test` / `fail_test`。
5. 这个测试同时验证了记分牌（4.3）和写回冒险（4.4）两套机制。

#### 4.4.5 小练习与答案

**练习 1**：为什么 store 指令不写寄存器，却仍要参与写回冲突检查？
> **答案**：写回级不只写寄存器堆，还要处理 store 的异常（如页错误）、回滚等副作用（见 [L217-L219 注释](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L217-L219)）。两条指令不能同时挤进写回级，所以无论是否写寄存器都要预约。

**练习 2**：浮点指令「只占 bit[3]、却不检查任何位」，会不会和别的指令撞写回？
> **答案**：不会。浮点是最长路径，它占的 bit[3] 每拍向低位漂移；当后续某条整数/访存指令即将发射、可能与之撞写回时，那条指令会检查到漂移下来的对应位（整数查 bit[0]、访存查 bit[1]）而被推迟。所以「被推迟的总是较短的后来者」。

---

### 4.5 缓存缺失挂起与唤醒：让等内存不阻塞流水线

#### 4.5.1 概念说明

当某线程的 load/store 在 L1 数据缓存未命中，要等 L2 甚至主存回填——这可能几十上百个周期。如果让整条流水线干等，4 个线程全得陪着停。Nyuzi 的做法是：**把缺失的线程「挂起」（blocked），仲裁器跳过它，让其他线程继续用发射槽**；等数据回来了再「唤醒」它。这就是多线程隐藏延迟的最直接体现。

挂起/唤醒用一张位图 `thread_blocked` 管理：一位一线程，置 1 表示该线程正因缺失等待。

#### 4.5.2 核心流程

```text
L1 缺失 (writeback_stage 检测)
   │  下一拍拉高 wb_suspend_thread_oh 的对应位
   ▼
thread_blocked |= wb_suspend_thread_oh        ← 线程被挂起
   │  can_issue_thread 里 !thread_blocked 为 0 → 仲裁器跳过它
   ▼
（等待 L2/IO 回填，几十~上百周期，期间其他线程照常发射）
   │  回填完成，l2i_dcache_wake_bitmap 或 ior_wake_bitmap 对应位拉高
   ▼
thread_blocked &= ~(wake_bitmap)              ← 线程被唤醒，重新参与调度
```

#### 4.5.3 源码精读

`can_issue_thread` 里的 `!thread_blocked`（[L174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L174)）就是挂起线程被「摘出」调度的地方。

挂起与唤醒的位图运算只有一行，但几条 `assert` 把它的时序约束讲得非常透彻：

```systemverilog
// 不要挂起一个没在运行的线程
assert((wb_suspend_thread_oh & thread_blocked) == 0);
// 不要唤醒一个没被挂起（或本拍正被挂起）的线程
assert(((l2i_dcache_wake_bitmap | ior_wake_bitmap) & ~(thread_blocked | wb_suspend_thread_oh)) == 0);
...
thread_blocked <= (thread_blocked | wb_suspend_thread_oh)
    & ~(l2i_dcache_wake_bitmap | ior_wake_bitmap);
```
> [thread_select_stage.sv:L293-L294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L293-L294)（约束见 [L271-L283](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L271-L283)）　「先或上挂起、再清掉唤醒」，唤醒写在表达式后面，故同周期同时出现时**唤醒优先**。

注释专门解释了「同周期既挂起又唤醒」的边界情况（[L287-L294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L287-L294)）：写回级在缺失发生一拍后才拉高 suspend；若这个地址恰好已在被回填、本拍数据就绪，suspend 与 wake 会同时出现——此时让 wake 赢，因为数据已可用、线程不必回滚。表达式里 wake 排在后面（`& ~wake`），正合此意。

两类唤醒源的区别：`l2i_dcache_wake_bitmap` 来自 L2 回填 D-Cache（见 [u6-l2](u6-l2-l1-l2-interface.md)），`ior_wake_bitmap` 来自 IO 请求队列对 MMIO 外设访问的完成（见 [u6-l4](u6-l4-axi-io-bus.md)）。两者用「或」合并，但断言保证它们**不会同周期同时拉高**（[L271](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L271)）。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：跟踪一次 D-Cache 缺失从挂起到唤醒的完整位图流转。
2. **步骤**：在 [thread_select_stage.sv:L256-L299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L256-L299) 里定位 `thread_blocked` 的更新式，再结合输入端口 `wb_suspend_thread_oh`、`l2i_dcache_wake_bitmap`、`ior_wake_bitmap`（[L66-L69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L66-L69)）理清数据来源。
3. **观察现象**：被挂起的线程其 `thread_blocked=1`，于是 `can_issue` 位为 0；但它**并不从 FIFO 删除**，FIFO 队头指令原样保留。
4. **预期结果**：唤醒后 `thread_blocked` 清 0，该线程重新参与轮询，从原先卡住的那条指令继续发射——对软件而言仿佛只是「等了一会儿」。
5. 结合 4.2 可得出结论：**等内存期间流水线并不空转，而是去服务其他就绪线程**。

#### 4.5.5 小练习与答案

**练习 1**：为什么挂起用位图、而不是给每个线程加一个状态机寄存器？
> **答案**：位图可以用一行位运算同时完成「挂起、唤醒、与调度条件按位与」，简洁且利于时序；同时天然配合 `rr_arbiter` 的请求位图输入。

**练习 2**：4 个线程同时缓存缺失，流水线会怎样？
> **答案**：4 位 `thread_blocked` 全置 1，`can_issue_thread` 全 0，仲裁器无人可授权，`ts_instruction_valid` 为 0（[L285](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L285)），流水线这拍真正空射，等任一唤醒源到达。

---

## 5. 综合实践

把本讲的调度、记分牌、写回冒险、缓存缺失串起来，完成下面这个贯穿性任务。

### 任务：运行 waw.S 并构造一个 RAW 用例

**背景**：[tests/core/isa/waw.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/waw.S) 是项目自带、专门验证「写后写 + 读后写」冒险的功能测试，覆盖了本讲的记分牌与写回冒险机制。

**步骤**：

1. **环境**：按 [u1-l2](u1-l2-build-and-run.md) 完成构建，确保 `bin/nyuzi_emulator`（或 verilator 产物）已生成。
2. **跑通现有测试**（若已装好工具链）：

   ```bash
   cd tests/core/isa
   ./runtest.py --target emulator waw
   ```

   预期：测试输出 `PASS`（自校验通过）。若工具链未就绪，**待本地验证**。
3. **源码侧分析（必做，不依赖运行）**：对照本讲 4.3.4，用位图推演 `waw.S` 里 `mull_i s3,...` 与 `add_i s3, s3,...` 这一对指令：
   - `mull_i` 发射后，s3 在记分牌里被置忙碌；同时因 `mull_i` 走浮点乘法路径（长延迟），`writeback_allocate` 的 bit[3] 被预约。
   - 紧随的 `add_i` 既 RAW（读 s3）又 WAW（写 s3），且是整数短路径——记分牌的 `can_issue` 因 s3 忙碌而为 0，必须等 `mull_i` 写回。
   - 写回冒险检查在此例中也会参与（整数 add 查 bit[0]），但记分牌通常会先把它挡住。两套机制叠加保证结果正确。
4. **构造你自己的 RAW 用例**：仿照 `waw.S` 的结构，在 `tests/work` 下新建一个汇编小片段（**示例代码，非项目原有文件**）：

   ```assembly
   # 示例代码：构造 RAW（不写回冲突版）
   #include "asm_macros.h"
           .globl _start
   _start:
           move   s1, 6
           move   s2, 7
           move   s4, 100        # 预期结果
           nop; nop; nop; nop; nop; nop; nop; nop   # 清空流水线
           add_i  s3, s1, s2     # s3 = 13
           add_i  s5, s3, s4     # RAW：读 s3 → s5 = 113
           nop; nop; nop; nop; nop; nop; nop; nop
           cmpeq_i s6, s5, 113   # 不存在 cmpeq_i 立即数形式时改用寄存器比较
           bnz    s6, 1f
           call   fail_test
   1:      call   pass_test
   ```

   > 注：上面 `cmpeq_i s6, s5, 113` 仅为示意，Nyuzi 比较指令的合法操作数请以 [tools/emulator/instruction-set.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h) 与 [u2-l2](u2-l2-arithmetic-instructions.md) 为准；落地时需把立即数先 `move` 进寄存器再比较。**待本地按合法汇编改写后验证**。
5. **现象观察**：用 `runtest.py --debug` 或模拟器 `-v` 跟踪，关注第二条 `add_i` 的发射时刻——它应明显晚于第一条，二者之间隔着第一条的写回周期。若开启 4 线程，你能看到等待期间仲裁器把发射槽让给其他线程（综合实践里单线程则表现为空射）。
6. **预期结果**：自校验通过（`pass_test`），证明 RAW 依赖被正确处理，最终 s5 = 113。

### 反思题

- 如果把 `THREADS_PER_CORE` 设为 1（单线程），本讲的「等内存时切换其他线程」还能隐藏延迟吗？（答：不能，单线程下缺失即空射，这正是多线程的核心价值。）
- 记分牌和写回冒险检查是「串联」进 `can_issue` 的，一个线程被任一机制挡住都不发射。试想：如果只保留记分牌、去掉写回冲突检查，哪类程序会出错？（提示：浮点与整数/访存撞写回。）

## 6. 本讲小结

- `thread_select_stage` 是单核的发射控制中枢：每个周期从多个硬件线程里**挑一条**指令送入操作数 fetch。
- 每个线程挂一个**指令 FIFO**，把解码与发射解耦；接近满时反压取指，回滚时整体冲刷。
- **轮询仲裁器** `rr_arbiter` 在「可发射线程位图」上公平挑选，让被挂起的线程不拖慢其他人——这是多线程隐藏延迟的关键。
- **记分牌**用一张 64 位位图（32 标量 + 32 向量）标记「待写回」寄存器，通过 `can_issue = (busy & deps) == 0` 阻止 RAW / WAW；WAR 由「读早于写」的流水线结构天然规避；回滚时只清「比回滚点年轻」的位。
- **写回结构冒险**用 4 位移位寄存器 `writeback_allocate` 预约未来写回槽，浮点占最远位、访存占最近位、整数不占，避免不同长度路径的指令在写回级撞车。
- **缓存缺失挂起/唤醒**用 `thread_blocked` 位图把缺失线程摘出调度，等 L2/IO 响应回来再唤醒，把长延迟变成「服务其他线程」的机会。

## 7. 下一步学习建议

本讲讲清了「谁发射、何时发射」。接下来：

- **进入执行路径**：先看 [u5-l1 操作数 fetch 与寄存器文件](u5-l1-operand-fetch.md)，了解本讲发射出的指令如何读源操作数、生成掩码、处理向量子周期。
- **整数执行**：[u5-l2 整数执行单元](u5-l2-integer-execute.md) 讲分支解析与回滚——本讲记分牌里提到的「回滚」就来自这里。
- **缓存细节**：想深究「线程为何被挂起」的源头，可直接读 [u6-l1 L1 数据缓存](u6-l1-l1-dcache.md) 与 [u6-l2 L1-L2 接口](u6-l2-l1-l2-interface.md)，看 `wb_suspend_thread_oh` 与 `l2i_dcache_wake_bitmap` 是如何产生的。
- **进阶**：[u10-l1 同步内存操作 LL/SC 与 membar](u10-l1-sync-load-store.md) 与 [u10-l2 多线程调度与挂起恢复](u10-l2-thread-scheduling.md) 会把本讲的同步访存与挂起/唤醒机制放到并发与多核场景下再深挖一层。
