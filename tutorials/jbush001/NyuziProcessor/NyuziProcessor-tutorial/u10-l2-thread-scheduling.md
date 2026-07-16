# 多线程调度与挂起恢复

## 1. 本讲目标

本讲深入 Nyuzi 单核内部的「线程调度」机制。读完本讲，你应当能够：

- 说清 `thread_select_stage` 如何用**轮询仲裁**在每个周期从多个硬件线程里挑出一个发射，并理解这种细粒度多线程为什么能「隐藏延迟」。
- 区分**两套独立的线程闸门**：软件驱动的 `thread_en`（跨核、持久，由 `CR_SUSPEND_THREAD`/`CR_RESUME_THREAD` 控制）与硬件驱动的 `thread_blocked`（每核、瞬时，由缓存缺失与 L2 响应驱动）。
- 跟踪一次「D-Cache 缺失 → 线程挂起 → L2 回填 → 唤醒位图恢复」的完整链路，并解释这套机制为何能让其他线程继续占用流水线而不空转。

本讲是 u4-l3（线程选择与记分牌）的深化：u4-l3 给出了发射控制的总体骨架，本讲把「轮询调度」「软件 suspend/resume」「硬件挂起唤醒」三件事拆开讲透。

## 2. 前置知识

在进入源码前，先用直觉建立三个概念。

**(1) 细粒度多线程（fine-grained / interleaved multithreading）。**
Nyuzi 每个核有多个硬件线程（默认 4 个，见 `config.svh` 的 `THREADS_PER_CORE 4`）。这些线程**共享同一条流水线**，但每个线程拥有**独立的寄存器组、独立的 PC、独立的指令 FIFO**。每个周期，硬件从「准备好」的线程里挑一个发射一条指令。于是同一时刻流水线上跑着来自不同线程的指令。这样做的好处是：当线程 A 因为等待内存而停顿时，线程 B、C、D 仍然能填充流水线，于是流水线几乎不会因为单个线程的停顿而空泡。

**(2) 两套「停机」语义不要混淆。**
一个线程「不发射」可能有两种完全不同的原因，对应两套独立的位图：

| 概念 | 存储位置 | 位宽 | 触发者 | 寿命 | 复位值 |
|------|----------|------|--------|------|--------|
| `thread_en`（软件闸门） | `nyuzi.sv` 顶层 | `TOTAL_THREADS`（全核） | 软件写 `CR_SUSPEND_THREAD`/`CR_RESUME_THREAD` | 持久（保持到被改写） | `1`（只有线程 0） |
| `thread_blocked`（硬件闸门） | `thread_select_stage.sv` | `THREADS_PER_CORE`（每核） | 缓存/IO 缺失与 L2/IO 响应 | 瞬时（等到响应即清） | `0`（无阻塞） |

一个线程能被发射，必须**同时**通过这两道闸门。这一区分是本讲的核心。

**(3) 位图（bitmap）是调度器的通用语言。**
Nyuzi 用「一位代表一个线程」的位图来表达线程集合：第 `i` 位为 1 表示线程 `i` 在集合中。`local_thread_bitmap_t` 是每核 `THREADS_PER_CORE` 位的类型，`TOTAL_THREADS` 位宽则是全核（`NUM_CORES * THREADS_PER_CORE`）。位图的好处是「按位或 = 合并集合」「按位与 = 求交集」，调度逻辑因此可以写成一行组合逻辑。

> 相关类型定义见 [defines.svh:44-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L44-L49)：`TOTAL_THREADS = THREADS_PER_CORE * NUM_CORES`，`local_thread_idx_t` 是核内线程号，`local_thread_bitmap_t` 是「一位一线程」的核内位图。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `hardware/core/thread_select_stage.sv` | **调度中枢**。维护每线程指令 FIFO、记分牌调用、轮询仲裁、`thread_blocked` 挂起/唤醒位图、写回结构冒险。本讲主角。 |
| `hardware/core/scoreboard.sv` | **依赖追踪**。每线程一个实例，用 64 位位图记录「待写回寄存器」，阻止 RAW/WAW 冒险。 |
| `hardware/core/control_registers.sv` | **软件闸门来源**。处理 `CR_SUSPEND_THREAD`/`CR_RESUME_THREAD` 写入，产生全核宽度的 `cr_suspend_thread`/`cr_resume_thread` 脉冲。 |
| `hardware/core/nyuzi.sv` | **顶层聚合**。把各核的 suspend/resume 脉冲或起来，维护持久的 `thread_en`，再按核切片喂回每个核。 |
| `hardware/core/l1_load_miss_queue.sv`（辅助） | 产生 `wake_bitmap`：L2 响应到达时，唤醒所有在该缓存行上等待的线程。 |
| `hardware/core/l1_l2_interface.sv`（辅助） | 合并 load-miss 与 store-queue 两路唤醒，得到 `l2i_dcache_wake_bitmap`。 |
| `hardware/core/writeback_stage.sv`（辅助） | 产生 `wb_suspend_thread_oh`：把缺失线程号转成独热位图。 |

---

## 4. 核心概念与源码讲解

### 4.1 轮询调度与发射控制

#### 4.1.1 概念说明

每个周期，`thread_select_stage` 要回答一个问题：「这一拍该让哪个线程发射下一条指令？」

它的做法是**轮询（round-robin）**：把所有「本周期可以发射」的线程放进一个集合，用一个公平仲裁器挑出其中一个；下一周期优先考虑这次没被选中的线程，从而让所有线程均匀地轮转。这就是细粒度多线程的核心调度策略。

为什么轮询能隐藏延迟？因为「调度」与「执行」是解耦的：线程 A 若因为数据依赖或缓存缺失暂时发不出，仲裁器**根本不会把它放进候选集合**，于是天然地跳过 A、选中 B，不需要任何「流水线冲刷」或「停顿气泡」。线程越多，单个线程的停顿越容易被其他线程的工作填满，吞吐就越接近满载。

注意：Nyuzi 是**按线程轮转、不按指令乱序**。同一时刻只发射一个线程的一条指令，但「这一条」可能是一条向量指令（16 通道并行），所以宽度上仍是 SIMD。

#### 4.1.2 核心流程

每个线程维护一个**指令 FIFO**（`sync_fifo`，深度 8）。解码级把指令塞进对应线程的 FIFO；本级从 FIFO 头部读出候选指令。一个线程「本周期可发射」需要同时满足六个条件：

\[
\text{can\_issue}_i = \neg\,\text{FIFO\_empty}_i \;\land\; (\text{scoreboard\_can\_issue}_i \lor \text{subcycle}_i \neq 0)
\;\land\; \text{thread\_en}_i \;\land\; \neg\,\text{rollback}_i \;\land\; \neg\,\text{wb\_conflict}_i \;\land\; \neg\,\text{blocked}_i
\]

- `FIFO 非空`：确实有解码好的指令等着。
- `记分牌放行 或 subcycle≠0`：没有未解决的数据依赖；或正处在 scatter/gather 的中间子周期（中途不能停，否则要重新逐通道）。
- `thread_en`：软件没有挂起它（4.2 节）。
- `非回滚`：本线程本周期没有被回滚信号命中。
- `无写回结构冲突`：不会和另一条不同长度路径的指令在同一周期撞到写回级。
- `非阻塞`：没有在等缓存/IO 响应（4.3 节）。

把所有满足条件的线程拼成位图 `can_issue_thread`，送进 `rr_arbiter`：

```
can_issue_thread 位图 ──► rr_arbiter（轮询，update_lru=1）──► grant_oh（独热，唯一被选中的线程）
                                                              │
                                                              ▼
                                                   oh_to_idx ──► issue_thread_idx
                                                              │
                                                              ▼
                                          ts_instruction / ts_thread_idx / ts_subcycle  → operand_fetch
```

仲裁器输出**独热码** `grant_oh`：任意周期至多一个线程被选中。`oh_to_idx` 把独热码翻译成线程号，本级据此从对应 FIFO 取出指令送到操作数 fetch 级。

#### 4.1.3 源码精读

仲裁器实例化在 [thread_select_stage.sv:237-245](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L237-L245)：`rr_arbiter` 的 `request` 接 `can_issue_thread`，`update_lru` 恒为 1（保证公平轮转、不会饿死某个线程），输出独热的 `thread_issue_oh`，再由 `oh_to_idx` 转成线程号。

`can_issue_thread` 的六条件见 [thread_select_stage.sv:169-174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L169-L174)。注意第二个条件里 `current_subcycle != 0` 的特例：一条 scatter/gather 指令要发 16 次，记分牌只跟踪到「寄存器」粒度，若中途停掉会让该寄存器一直「忙」、把自己卡死，所以子周期中途**绕过记分牌检查**强行继续发射（代码注释在 [thread_select_stage.sv:164-168](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L164-L168)）。

每线程的指令 FIFO 用 `generate` 实例化，见 [thread_select_stage.sv:119-133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L119-L133)。FIFO 把「解码速率」和「发射速率」解耦：解码级可以连续塞几条，发射级按调度节奏取。当 FIFO 接近满（`ALMOST_FULL_THRESHOLD = SIZE - 3`）时，本级把对应线程的取指使能拉低，提前几拍反压取指，见 `ts_fetch_en` 的赋值 [thread_select_stage.sv:151](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L151)。

> 这里也体现了两套闸门的叠加：取指使能同时要求 `!ififo_almost_full && thread_en[thread_idx]`——硬件反压与软件使能缺一不可。

写回结构冒险由一个 4 位移位寄存器 `writeback_allocate` 预约未来写回槽，避免整数（1 级）、访存（多级）、浮点（5 级）三条不同长度路径的指令同周期撞到写回级，见 [thread_select_stage.sv:212-232](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L212-L232)；与之配套的每线程 `writeback_conflict` 见 [thread_select_stage.sv:153-162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L153-L162)。

记分牌本身的工作方式：`scoreboard_can_issue = (scoreboard_regs & dep_bitmap) == 0`，即「当前指令的源/目的寄存器没有一个是待写回状态」才放行，见 [scoreboard.sv:152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L152)；位图的更新是「清掉回滚/写回位、置上本指令目的位」，见 [scoreboard.sv:147-151](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L147-L151)。`dep_bitmap` 把源寄存器与目的寄存器都标出来（[scoreboard.sv:96-119](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L96-L119)），所以它同时挡 RAW 与 WAW；而 WAR 由「读早于写」的流水线结构天然规避。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用「记分牌 + 轮询」解释一段有 RAW 依赖的指令如何不浪费周期。

**步骤**：

1. 设线程 0 连续发射两条指令：`add_i s0, s1, s2`（写 s0）与 `add_i s3, s0, s4`（读 s0）。第一条发射后，`scoreboard.sv` 把 s0 对应位置 1。
2. 阅读 [scoreboard.sv:152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L152) 与 [thread_select_stage.sv:169-174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L169-L174)：确认第二条指令的 `scoreboard_can_issue` 为 0，于是线程 0 这一拍不进 `can_issue_thread`。
3. 阅读 `rr_arbiter` 的接线：线程 0 被排除后，仲裁器转而把 `grant_oh` 给线程 1（若它就绪）。

**需要观察的现象 / 预期结果**：线程 0 在等 s0 写回的那几拍里，发射槽被线程 1/2/3 接管，**流水线不会因为 RAW 而插入气泡**。当 s0 的写回到达（`writeback_en` 清掉 s0 的忙位，见 [scoreboard.sv:138-145](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/scoreboard.sv#L138-L145)），线程 0 重新进入候选集合，轮到自己时即发射第二条指令。这就是「隐藏延迟」的体现。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `rr_arbiter` 的 `update_lru` 改成 0（不更新最近使用），会出现什么问题？
**参考答案**：仲裁器会一直优先同一个固定线程（例如线程 0），只要它持续就绪，其他线程会被饿死，失去「轮询公平性」。`update_lru=1` 让刚被选中的线程排到队尾，是公平轮转的关键。

**练习 2**：为什么 `can_issue_thread` 的第二个条件要写成「`scoreboard_can_issue || subcycle != 0`」这种「或」，而不是直接「`scoreboard_can_issue`」？
**参考答案**：scatter/gather 一条指令要重发 16 个子周期，每次都写同一个目标寄存器。若每个子周期都查记分牌，第一条子周期置上的「忙位」会立刻把自己挡住，导致指令永远发不完。因此子周期中途（`subcycle != 0`）绕过记分牌，只在第 0 个子周期检查一次。

---

### 4.2 软件驱动的 suspend/resume 与 thread_en 聚合

#### 4.2.1 概念说明

`thread_en` 是**软件层面**的线程生死开关。它回答的不是「这一拍能不能发射」，而是「这个线程到底该不该存在」。

复位的瞬间，只有线程 0 被使能（`thread_en <= 1`，二进制 `0001`）。裸机程序从 `crt0.S` 的 `_start` 启动，由线程 0 执行。当线程 0 想要并行计算时，它写控制寄存器 `CR_RESUME_THREAD`（编号 21）来唤醒线程 1~3；当所有工作完成、一个线程无事可做时，它写 `CR_SUSPEND_THREAD`（编号 20）把自己挂起。程序结束的标志正是「所有线程都把自己挂起、`thread_en` 归零」。

这与 4.3 节的 `thread_blocked` 截然不同：`thread_en` 是**持久的**（一旦写入就保持到下次改写），且是**跨核全局**的（位宽 `TOTAL_THREADS`）；而 `thread_blocked` 是瞬时的、每核局部的。

`thread_en` 不能直接由某个核自己维护，因为 `CR_SUSPEND_THREAD` 写入的值是**全核位图**（软件可能一次唤醒别的核的线程，尽管通常不会）。所以设计成：每个核的 `control_registers` 产生自己看到的 suspend/resume 脉冲，顶层 `nyuzi.sv` 把所有核的脉冲「或」起来，统一更新一个全局 `thread_en`，再按核切片下发。

#### 4.2.2 核心流程

```
                软件写 CR_SUSPEND_THREAD / CR_RESUME_THREAD
                              │（值是 TOTAL_THREADS 位宽的全核位图）
                              ▼
        control_registers（每核一个）
          cr_suspend_thread[NUM_CORES][TOTAL_THREADS]   ← 每周期脉冲，默认 0
          cr_resume_thread [NUM_CORES][TOTAL_THREADS]
                              │
                              ▼  顶层 nyuzi.sv 聚合
        thread_suspend_mask = ∨(各核 cr_suspend_thread)
        thread_resume_mask  = ∨(各核 cr_resume_thread)
                              │
                              ▼  持久寄存器更新（resume 先或，suspend 后清）
        thread_en <= ( thread_en | thread_resume_mask ) & ~thread_suspend_mask
                              │
                              ▼  按核切片喂回每个核的 thread_select_stage
        core[k].thread_en = thread_en[ k*TPC +: TPC ]
```

两个关键点：

1. **脉冲而非电平**。`cr_suspend_thread`/`cr_resume_thread` 在 `control_registers` 里每个周期开头被清零（[control_registers.sv:189-190](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L189-L190)），只有在软件写控制寄存器的那一拍才为非零。所以「挂起/恢复」是一个一次性事件，`thread_en` 才是记住结果的状态。
2. **resume 先于 suspend**。更新公式里 resume（或）写在 suspend（清）之前，所以同一周期里若同一位既被 resume 又被 suspend，resume 胜出。

#### 4.2.3 源码精读

控制寄存器的写入译码见 [control_registers.sv:192-215](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L192-L215)。其中两条关键：

- `CR_SUSPEND_THREAD`：把写入值的低 `TOTAL_THREADS` 位赋给 `cr_suspend_thread`，见 [control_registers.sv:208](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L208)。
- `CR_RESUME_THREAD`：同理赋给 `cr_resume_thread`，见 [control_registers.sv:209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L209)。

两者的输出声明是全核 `TOTAL_THREADS` 宽度，见 [control_registers.sv:44-45](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L44-L45)。每周期开头的清零见 [control_registers.sv:189-190](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L189-L190)。

顶层 `nyuzi.sv` 的聚合逻辑见 [nyuzi.sv:77-94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L77-L94)：组合逻辑把各核的 `core_suspend_thread[i]` / `core_resume_thread[i]` 或成全局掩码；时序逻辑里复位时 `thread_en <= 1`（仅线程 0），否则按公式更新。注意 `thread_en` 声明为全核宽度 `TOTAL_THREADS`，见 [nyuzi.sv:41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L41)。

下发到每个核用的是位切片：`.thread_en(thread_en[core_idx * THREADS_PER_CORE +: THREADS_PER_CORE])`，见 [nyuzi.sv:127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L127)。于是在核内部，`thread_select_stage` 看到的 `thread_en` 只是本核那 `THREADS_PER_CORE` 位（`local_thread_bitmap_t`），与 `thread_blocked` 同位宽，两者在 `can_issue_thread` 里按位「与」配合。

控制寄存器编号定义在 [defines.svh:186-187](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L186-L187)：`CR_SUSPEND_THREAD = 5'd20`、`CR_RESUME_THREAD = 5'd21`。

#### 4.2.4 代码实践（源码阅读型）

**目标**：跟踪 `parallel_execute` 唤醒其余硬件线程时 `thread_en` 如何从 `0001` 变成 `1111`。

**步骤**：

1. 回顾 u9-l2：裸机版 `parallel_execute` 调用 `start_all_threads`，后者写 `CR_RESUME_THREAD`，值为「唤醒线程 1~3」的位图（即 `0b1110` 或 `0b1111`，取决于是否含自身）。
2. 沿信号链追踪这 32 位写入值：`dd_creg_write_val` → [control_registers.sv:209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L209) 置 `cr_resume_thread` → [nyuzi.sv:83](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L83) 或入 `thread_resume_mask` → [nyuzi.sv:93](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L93) 更新 `thread_en`。
3. 复位后 `thread_en = 0001`。若 `thread_resume_mask = 1110`，则新值 `(0001 | 1110) & ~0000 = 1111`。

**需要观察的现象 / 预期结果**：更新后 4 个线程全部进入 `thread_en`，于是 4 条线程的 `crt0._start` 都开始执行（各自按线程号取不同栈、汇入 `worker_thread`）。这正是 u9-l2 描述的并行骨架在硬件层的落脚点。

> 待本地验证：若你已按 u1-l2 构建出 `nyuzi_vsim`/`nyuzi_emulator`，可用 `-v` 跟踪一个调用 `parallelExecute` 的程序，观察线程 1~3 的 PC 从复位向量开始跳动的时刻，与软件写 `CR_RESUME_THREAD` 的时刻吻合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cr_suspend_thread`/`cr_resume_thread` 要每周期清零，而 `thread_en` 不清零？
**参考答案**：前者是「事件」——软件只在某一拍写控制寄存器，那一拍产生脉冲即可；后者是「状态」——必须跨周期记住「哪些线程活着」。若把脉冲做成长期电平，就会反复挂起/恢复，逻辑出错。

**练习 2**：复位时 `thread_en <= 1`，为何不是 `<= '1`（全 1）？
**参考答案**：设计上只有线程 0 从复位向量取指并执行 `crt0._start`，其余线程处于挂起态，由软件在需要并行时显式唤醒。这保证启动过程是确定性的、单线程的，避免多个线程同时跑启动代码造成竞争。

---

### 4.3 硬件驱动的缓存缺失挂起与唤醒位图

#### 4.3.1 概念说明

`thread_blocked` 是**硬件层面**的瞬时闸门，专门处理「这个线程正在等一个长延迟操作」。最典型的场景是 D-Cache 缺失：一条 `load_32` 没命中 L1，要等 L2（甚至主存）把整行取回，可能几十上百拍。如果不做任何处理，这个线程会卡在流水线里反复重发同一条 load，白白占用发射槽，还可能堵住记分牌。

Nyuzi 的解法是**把缺失线程「摘出调度」**：

1. 缺失被检测到时，把该线程在 `thread_blocked` 里置 1，于是它退出 `can_issue_thread`，仲裁器再也不会选它。
2. 同时把它的 PC 回滚到这条 load（让它将来重发同一指令）。
3. L2 把数据回填后，产生一个**唤醒位图** `l2i_dcache_wake_bitmap`，在 `thread_blocked` 里清掉对应位，线程重新进入候选集合，下一拍重发 load——这次命中。

关键在于：缺失线程被摘出后，流水线**并没有停**，仲裁器立刻把发射槽让给其他就绪线程。这就是「缓存缺失挂起/唤醒」与「细粒度多线程」联手的威力——长延迟被其他线程的工作填满。

唤醒用位图（而非单个线程号）有一个额外好处：**多个线程缺失同一缓存行时，可以合并成一次 L2 请求，回填后一次性唤醒所有等待者**。

#### 4.3.2 核心流程

整条「缺失 → 挂起 → 回填 → 唤醒」链路跨了好几个模块，但都收敛到 `thread_blocked` 这一个寄存器：

```
【挂起侧】
dcache_data_stage 检测到缺失 ──► dd_suspend_thread=1（dd_thread_idx 指明是哪个线程）
                                         │
writeback_stage 把线程号转独热 ──► wb_suspend_thread_oh = thread_dd_oh   （writeback_stage.sv:266-267）
                                         │
thread_select_stage：
  thread_blocked <= (thread_blocked | wb_suspend_thread_oh) & ~(wake)   （thread_select_stage.sv:293-294）
  （同时 PC 回滚，让该线程将来重发这条 load）

【唤醒侧】
L2 回填响应到达 ──► l1_load_miss_queue.wake_bitmap = pending_entries[响应项].waiting_threads
                                         │（等待位图：所有在该行上缺失的线程）
l1_l2_interface 合并 load-miss 与 store 两路：
  l2i_dcache_wake_bitmap = dcache_miss_wake_bitmap | sq_wake_bitmap       （l1_l2_interface.sv:209）
                                         │
thread_select_stage：在同一个 thread_blocked 更新式里把唤醒位清掉
```

一个精妙的细节：**挂起与唤醒可能发生在同一周期**。注释（[thread_select_stage.sv:287-292](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L287-L292)）说明：写回级的 suspend 信号比缺失晚一拍，而此刻这个缺失可能已经被另一个对同一地址的在途请求满足了、即将回填。这种情况下 suspend 和 wake 同时出现。代码刻意把 wake 写在表达式末尾的取反里，使 **wake 胜出**——既然数据已经在手，就不必再挂起、回滚。

#### 4.3.3 源码精读

`thread_blocked` 的状态更新是本节的核心一行，见 [thread_select_stage.sv:293-294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L293-L294)：

```systemverilog
thread_blocked <= (thread_blocked | wb_suspend_thread_oh)
    & ~(l2i_dcache_wake_bitmap | ior_wake_bitmap);
```

读法：本周期被挂起的线程「或」进阻塞集合，被唤醒的线程（来自 D-Cache 回填 `l2i_dcache_wake_bitmap` 或 IO 响应 `ior_wake_bitmap`）「清」出集合。复位时 `thread_blocked <= '0`（见 [thread_select_stage.sv:262](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L262)）。

挂起信号的来源：写回级把「是否需要挂起」与线程号独热拼起来，见 [writeback_stage.sv:266-267](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L266-L267)。三种情况会触发挂起：D-Cache 缺失（`dd_suspend_thread`）、store 队列回滚（`sq_rollback_en`）、IO 请求回滚（`ior_rollback_en`）。注意断言 `$onehot0(wb_suspend_thread_oh)`（[thread_select_stage.sv:283](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L283)）：每周期至多挂起一个线程。

唤醒信号的来源：`l1_load_miss_queue` 在 L2 响应到达时，输出该响应项里记录的 `waiting_threads` 位图作为 `wake_bitmap`，见 [l1_load_miss_queue.sv:85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L85)。每条 miss 队列项都有一个 `waiting_threads` 字段（[l1_load_miss_queue.sv:48-54](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L48-L54)），后续线程对同一行的缺失会被**合并**进来而不是新建请求（这是 u6-l2 讲过的 collided miss）。`l1_l2_interface` 再把 load-miss 唤醒与 store 队列唤醒或起来，见 [l1_l2_interface.sv:209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L209)。IO 路径有独立的 `ior_wake_bitmap`（来自 `io_request_queue`）。

四条断言把这套机制的**不变量**写得很清楚，是理解设计意图的最佳入口，见 [thread_select_stage.sv:271-283](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L271-L283)：

- `(l2i_dcache_wake_bitmap & ior_wake_bitmap) == 0`：L1 回填唤醒与 IO 响应唤醒不会同周期发生。
- `(wb_suspend_thread_oh & thread_blocked) == 0`：不会重复挂起一个已经阻塞的线程。
- `((l2i_dcache_wake_bitmap | ior_wake_bitmap) & ~(thread_blocked | wb_suspend_thread_oh)) == 0`：唤醒只能针对「已阻塞或本周即将阻塞」的线程——不会无中生有地唤醒。
- `(thread_issue_oh & thread_blocked) == 0`：阻塞线程绝不会被发射。

> 仿真构建时 `SIMULATION` 宏还会启用一个每线程状态枚举（`TS_WAIT_ICACHE / TS_WAIT_DCACHE / TS_WAIT_RAW / TS_WAIT_WRITEBACK_CONFLICT / TS_READY`），见 [thread_select_stage.sv:90-99](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L90-L99) 与 [thread_select_stage.sv:190-208](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L190-L208)，专供可视化工具用，把「线程为什么不发射」归一成一个状态。它在可综合 RTL 中不存在。

#### 4.3.4 代码实践（源码阅读型，对应本讲指定实践任务）

**目标**：分析一个线程因 D-Cache 缺失被挂起后，L2 响应如何通过 `wake_bitmap` 恢复该线程，并说明这种机制如何让其他线程继续利用流水线。

**步骤**：

1. **找到挂起点**。在 [writeback_stage.sv:266-267](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L266-L267) 确认：当 `dd_suspend_thread`（D-Cache 缺失）为真时，`wb_suspend_thread_oh` 取当前在 dcache_data 级的线程号 `dd_thread_idx` 的独热。注意这是**一个周期后**才发出的（见 [thread_select_stage.sv:287-292](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L287-L292) 的注释）。
2. **找到阻塞位如何置位**。在 [thread_select_stage.sv:293-294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L293-L294) 看 `thread_blocked` 的更新式：`wb_suspend_thread_oh` 那一位被或进去，于是该线程退出 `can_issue_thread`（[thread_select_stage.sv:169-174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L169-L174)），仲裁器不再选它。
3. **跟踪唤醒位的产生**。L2 回填该行时，`l1_load_miss_queue` 用响应项的 `waiting_threads` 字段输出 `wake_bitmap`（[l1_load_miss_queue.sv:85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L85)），经 `l1_l2_interface` 合并成 `l2i_dcache_wake_bitmap`（[l1_l2_interface.sv:209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L209)）。
4. **确认唤醒清阻塞**。回到 `thread_blocked` 更新式：`~(l2i_dcache_wake_bitmap | ior_wake_bitmap)` 把该线程的阻塞位清掉，线程重新进入候选集合，下一拍按回滚后的 PC 重发那条 load（此时命中）。

**需要观察的现象 / 预期结果**：在缺失线程被阻塞的那段时间里，阅读 `rr_arbiter` 的 `request`——`can_issue_thread` 里该线程位为 0，仲裁器自动把发射槽让给其他就绪线程。**因此 L2 取数据的几十拍里，流水线仍被其他线程的指令填满，吞吐不塌陷。** 这正是细粒度多线程隐藏内存延迟的本质。

> 待本地验证：若已构建 `nyuzi_vsim`，可对一个故意制造 D-Cache 缺失的小程序用 `+trace` 跑协同仿真（u8-l3），在 trace 里会看到：缺失线程的指令发射暂停若干拍，而其他线程的指令穿插其间，直到回填后缺失线程的那条 load 重新出现并命中。

#### 4.3.5 小练习与答案

**练习 1**：为什么唤醒信号用「位图」而不是「单个线程号 + 有效位」？
**参考答案**：因为多个线程可能同时缺失在同一缓存行上（collided miss）。miss 队列把它们合并成一次 L2 请求，并用 `waiting_threads` 位图记录所有等待者；回填时一次性唤醒全部，既省 L2 带宽，又只需一条唤醒通路。单个线程号无法表达「多个等待者」。

**练习 2**：断言 `(wb_suspend_thread_oh & thread_blocked) == 0` 想保证什么？什么设计保证了它成立？
**参考答案**：它保证「不会重复挂起一个已经阻塞的线程」。之所以成立，是因为一个线程一旦被挂起就被踢出 `can_issue_thread`，于是它不会再前进、不会再到达 dcache_data 级、不会再产生新的缺失与 suspend 信号——直到被唤醒。即「挂起 ⇒ 不再发射 ⇒ 不会再触发挂起」形成自洽。

**练习 3**：suspend 和 wake 同周期出现时代码为何让 wake 胜出？若让 suspend 胜出会怎样？
**参考答案**：同周期出现意味着数据其实已经就绪（只是 suspend 信号晚了一拍才到）。wake 胜出则线程立刻解除阻塞、按回滚重发 load 并命中，行为正确。若 suspend 胜出，线程会被错误地挂起、又要等一次（多余的）唤醒，造成无谓延迟甚至与 miss 队列状态不一致。

---

## 5. 综合实践

**任务**：用一张时序图把「两套闸门 + 轮询调度」串起来，解释 4 个线程跑同一段循环时各自的「不发射」原因各是什么。

设定：单核、`THREADS_PER_CORE=4`，复位后 `thread_en=0001`。线程 0 在 `crt0` 里写 `CR_RESUME_THREAD` 唤醒线程 1~3，随后 4 个线程一起进入 `parallelExecute` 的任务循环。

请按下面的框架完成：

1. **软件唤醒阶段**。标注 `thread_en` 从 `0001` → `1111` 的那一拍（依据 4.2 节的更新公式与 [nyuzi.sv:93](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L93)）。此时 `thread_blocked` 仍为 `0000`。
2. **稳态并行阶段**。假设某周期四个线程的状态分别是：
   - 线程 0：就绪（`READY`）；
   - 线程 1：上条指令的源寄存器未写回（`TS_WAIT_RAW`）；
   - 线程 2：D-Cache 缺失，等 L2（`TS_WAIT_DCACHE`，`thread_blocked` 第 2 位为 1）；
   - 线程 3：就绪（`READY`）。
   
   写出该周期的 `can_issue_thread` 位图（提示：线程 1、2 为 0，线程 0、3 为 1），并指出 `rr_arbiter` 会在 0 和 3 之间轮转选择。
3. **缺失恢复阶段**。L2 回填线程 2 等的那一行，画出 `l2i_dcache_wake_bitmap` 第 2 位为 1 的一拍，`thread_blocked` 第 2 位被清，线程 2 下一拍重发 load 并命中、重新进入候选集合。
4. **归纳**：用一句话分别说明 `thread_en`、`thread_blocked`、记分牌三者各自挡掉了哪一类「不发射」——分别是「软件不让它活」「硬件在等数据」「数据依赖未就绪」。

> 这张图不需要你运行硬件，只需对照本讲引用的源码行号标注信号取值即可。完成后，你应当能一眼看出任意周期某个线程不发射的根因落在三道闸门中的哪一道。

## 6. 本讲小结

- Nyuzi 用**细粒度多线程**隐藏延迟：每个周期由 `rr_arbiter` 在「可发射线程位图」上轮询挑一个线程发射，某个线程停顿时仲裁器自然跳过它，不产生流水线气泡。
- 一个线程能被发射需同时通过**三道闸门**：软件 `thread_en`、硬件 `!thread_blocked`、记分牌 `scoreboard_can_issue`（外加 FIFO 非空、无回滚、无写回结构冲突），合起来就是 [thread_select_stage.sv:169-174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L169-L174) 的 `can_issue_thread`。
- `thread_en` 是**软件驱动、跨核全局、持久**的闸门：软件写 `CR_SUSPEND_THREAD`/`CR_RESUME_THREAD`（[control_registers.sv:208-209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L208-L209)）产生脉冲，顶层 `nyuzi.sv` 聚合并按 `thread_en <= (thread_en | resume) & ~suspend` 维护（[nyuzi.sv:88-94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L88-L94)），复位初值仅线程 0。
- `thread_blocked` 是**硬件驱动、每核局部、瞬时**的闸门：D-Cache/IO 缺失经 `wb_suspend_thread_oh` 置位，L2/IO 响应经 `l2i_dcache_wake_bitmap`/`ior_wake_bitmap` 清位，核心是一行更新式（[thread_select_stage.sv:293-294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L293-L294)）。
- 唤醒用**位图**而非线程号，使多线程对同一缓存行的 collided miss 合并成一次 L2 请求、回填时一次唤醒所有等待者（[l1_load_miss_queue.sv:85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L85)）。
- suspend 与 wake 同周期出现时，表达式顺序让 **wake 胜出**（[thread_select_stage.sv:287-294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L287-L294)）；四条断言（[thread_select_stage.sv:271-283](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L271-L283)）固化了这套机制的不变量。

## 7. 下一步学习建议

- **u10-l3（多核与 L2 仲裁）**：本讲的 `thread_en` 已是 `TOTAL_THREADS` 全核位宽，下一讲把视角拉到核间——多个核如何共享同一个 L2、`l2_cache_arb_stage` 如何仲裁各核请求，以及多核配置的构建与测试约束。
- **回顾 u10-l1（LL/SC 与 membar）**：那套同步访存的「两遍协议」与本讲的 `thread_blocked` 挂起/唤醒同构（都是「挂起等响应、响应唤醒重发」），对照阅读能加深对统一机制的理解。
- **回顾 u4-l3 与 u7-l2**：本讲是 u4-l3 发射骨架的深化、并复用了 u7-l2 控制寄存器的中断与 trap 现场；若对记分牌回滚或 trap 嵌套仍有疑问，可回查这两讲。
- **建议继续阅读的源码**：`hardware/core/rr_arbiter.sv`（轮询仲裁器的纯组合实现）、`hardware/core/io_request_queue.sv`（IO 路径独立的 `ior_wake_bitmap` 产生）、`hardware/core/sync_fifo.sv`（指令 FIFO 的反压机制）。
