# controller.cuh 主控制循环

> 单元 6 · 第 1 讲 · 阶段：intermediate
>
> 依赖：本讲建立在「VM 状态与页映射」([U5·L3](u5-l3-vm-state-and-pages.md)) 之上。请确认你已经了解 `state` 结构体、`instruction_index` / `instruction_ring` 这一对游标、以及 `instruction_arrived` / `instruction_finished` 两个信号量的方向与阈值。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 controller warp 在处理**一条指令**时做的「四步」分别是什么：取指（fetch）、建立物理页序（page order）、构造动态信号量（construct semaphores）、通知就绪（notify）。
2. 解释为什么 controller 在处理新指令之前，**必须先 `wait` 上一条指令的 `instruction_finished`**——也就是「环形 buffer 复用槽位」与「相位位（phase bit）」的关系。
3. 读懂 `instruction_ring` 这条环形流水如何在「2 级双缓冲」里把多条指令重叠起来，以及 controller 与其余 19 个 worker warp 怎样被 `instruction_arrived` / `instruction_finished` 串成生产者—消费者。
4. 描述 controller 收尾时如何把剩下的指令槽「排空（drain）」并把每条指令的 timings 写回全局内存。

## 2. 前置知识

用最朴素的话，把本讲要反复用到的几个概念先讲清楚。

- **controller 是 VM 的「大脑」**：在 [U5·L2](u5-l2-kernel-entry-and-warp-specialization.md) 里我们见过，整个 megakernel 内核把 20 个 warp 分成「16 个 consumer + loader + storer + launcher + controller」。controller 是非 consumer warpgroup 里 `warpgroup::warpid()==3` 的那个 warp（全局第 19 号 warp，见 [megakernel.cuh:123-136](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L123-L136) 的 `case 3`）。它不参与实际计算，职责是**逐条**把指令从全局内存搬到共享内存、把每条指令用到的物理页与信号量准备好，然后告诉其余 worker「这条可以跑了」。

- **生产者—消费者节拍**：controller 是「生产者」，loader/storer/launcher/consumer 是「消费者」。生产者准备好一条，就在 `instruction_arrived` 上「敲门」；消费者跑完一条，就在 `instruction_finished` 上「回报」。两个信号量的详细定义见 [U5·L3](u5-l3-vm-state-and-pages.md)，本讲只复习结论：

  | 信号量 | 方向 | init 阈值 | 含义 |
  | --- | --- | --- | --- |
  | `instruction_arrived[ring]` | controller → workers | 1 | 「这条指令准备好了，可以执行」 |
  | `instruction_finished[ring]` | workers → controller | `NUM_WARPS - 1` = 19 | 「所有 19 个 worker 都跑完这条了」 |

- **相位位（phase bit）**：物理槽位是会被反复复用的（`INSTRUCTION_PIPELINE_STAGES = 2`，只有 2 个槽）。光知道「槽 r 准备好了」不够，还得知道「这是第几轮」。因此每次等待都带一个 0/1 翻转的相位位，公式恒为：

  \[
  \text{phase} = \left(\left\lfloor \text{instruction\_index} / \text{INSTRUCTION\_PIPELINE\_STAGES} \right\rfloor\right)\ \&\ 1
  \]

- **kittens 信号量四件套**：`init / wait / arrive / invalidate`。`wait(sem, phase)` 阻塞到「当前相位累计的到达次数达到 init 时设的阈值」；`arrive(sem, count)` 累加到达次数；`invalidate_semaphore(sem)` 把信号量复位以便下一轮复用。内部 mbarriage 机制留给后续讲义，本讲只用高层语义。

- **warp 级并行**：controller 是**整整一个 warp（32 个 lane）**。下文你会看到，取指、建页序、清信号量这些「记账工作」都被拆给不同 lane 并行做；只有「敲门 arrive」「写 timing」这种只能做一次的动作，才用 `laneid() == 0` 单独交给 0 号 lane。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | **本讲主角**：controller warp 的 `main_loop`，四步控制流程、环形推进、收尾 drain 全在这里 |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `state` 结构体、`ring_advance` / `ring_retreat`、`instruction()` / `pid_order()` / `semaphores()` 访问器、timing 事件常量、`record()` |
| [include/controller/instruction_fetch.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh) | Step 1 的实现：`load_instructions` 把一条指令从全局内存搬进共享内存 |
| [include/controller/page_allocator.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh) | Step 2 的「op 分发器」：`page_allocator_op_dispatcher` 调用各 op 的 `release_lid` |
| [include/controller/semaphore_constructor.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh) | Step 3 的「op 分发器」：`semaphore_constructor_op_dispatcher` 调用各 op 的 `init_semaphores` |
| [include/controller/timings_store.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh) | 收尾阶段用的 `store_timings_and_reset`：把 timing 数组用 TMA 写回全局内存并清零 |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | 主内核：在共享内存里**初始化** `instruction_arrived`（阈值 1）与 `instruction_finished`（阈值 19），并把 `warpid()==3` 的 warp 路由进 controller::main_loop |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | 全部尺寸来源：`INSTRUCTION_PIPELINE_STAGES=2`、`INSTRUCTION_WIDTH=32`、`NUM_PAGES=13`、`DYNAMIC_SEMAPHORES=32` |

---

## 4. 核心概念与源码讲解

### 4.1 四步控制流程总览

#### 4.1.1 概念说明

把 controller 想象成一个「指令译码 + 后勤调度」的工位。每来一条指令，它要完成四件事，才能让流水线上的其他 worker 动手：

1. **取指（fetch）**：把这条 32 字的指令从全局内存读到共享内存的指令槽里。
2. **建立物理页序（page order）**：算出本条指令看到的「逻辑页 lid → 物理页 pid」映射表 `pid_order`，写进指令槽。
3. **构造动态信号量（construct semaphores）**：根据本条指令的 opcode，让对应 op 在指令槽里 `init` 它需要的若干个信号量，并返回个数。
4. **通知就绪（notify）**：在 `instruction_arrived[ring]` 上 `arrive`，告诉所有 worker「这条齐活了，开干」。

注意：controller **不是**「取指—执行—写回」那种自己执行指令的角色。它只负责把指令「布置」好，真正的执行（加载/计算/存储）是 loader / consumer / storer 干的。所以 controller 的四步，本质是**为每条指令搭好舞台**。

#### 4.1.2 核心流程

一条指令在 controller 眼里的生命周期（伪代码）：

```
对每一条指令 instruction_index = 0,1,2,...:
    ring = instruction_index % STAGES              # 当前用哪个物理槽

    # —— Step 0（仅当这个槽上一轮被用过）：回收上一条 ——
    if instruction_index >= STAGES:
        wait(instruction_finished[ring], phase_of(index - STAGES))   # 等 worker 跑完上一条
        invalidate 上一条在这个槽里建的信号量
        把上一条的 timings 写回全局内存（CONTROLLER_END）

    record(CONTROLLER_START)

    # —— Step 1：取指 ——
    load_instructions(...)             # 全局内存 → instruction()[0..31]
    record(IFETCH_DONE)

    # —— Step 2：建立物理页序 ——
    根据【上一条】的 opcode 算出本条的 pid_order[0..NUM_PAGES-1]
    record(PAGE_ALLOC_DONE)

    # —— Step 3：构造信号量 ——
    n = 根据本条 opcode 调 init_semaphores，返回信号量个数
    把 n 广播给全 warp，记进 num_semaphores[ring]
    record(SEMS_SETUP)

    # —— Step 4：通知就绪 ——
    arrive(instruction_arrived[ring], 1)
```

这五次 `record(...)`（`CONTROLLER_START / IFETCH_DONE / PAGE_ALLOC_DONE / SEMS_SETUP / CONTROLLER_END`）刚好把四步的耗时打点画出来，是后面做性能分析时的关键。

#### 4.1.3 源码精读

先看整个 `main_loop` 的骨架——一个 `for` 循环，循环变量同时推进「绝对指令号」`instruction_index` 和「环形槽位」`instruction_ring`：

[controller.cuh:24-29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L24-L29) —— **主循环骨架**：`instruction_index` 走 0→num_iters，每轮 `instruction_ring` 用 `ring_advance<STAGES>` 推进一格（在 0/1 之间来回）。

[controller.cuh:14-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L14-L18) —— **函数签名与本地状态**：`num_iters` 是本 worker 要处理的指令总数（来自全局 `g.instructions.rows()`）；`num_semaphores[STAGES]` 是一个**按槽位记录**的数组，记住「这个槽上一轮建了几个动态信号量」，收尾时才知道要 invalidate 几个。

`ring_advance` 的定义在 [util.cuh:57](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L57)：

```cpp
template<int N> __device__ static inline int ring_advance(int ring, int distance=1) { return (ring + distance) % N; }
```

即「在长度为 N 的环上前进 distance 格」。本讲 N = `INSTRUCTION_PIPELINE_STAGES` = 2。

#### 4.1.4 代码实践

**实践：用 grep 数清楚四步对应的 `record` 打点。**

1. 目标：建立「四步 ↔ timing 事件」的直观对应。
2. 步骤：在 `controller.cuh` 里搜索 `kvms.record(`，记下每处出现的行号和它紧跟的事件常量名。
3. 观察现象：你应该看到 5 处 `record`，分别对应 `CONTROLLER_START`(L63)、`IFETCH_DONE`(L71)、`PAGE_ALLOC_DONE`(L102)、`SEMS_SETUP`(L127)、`CONTROLLER_END`(L53 与 L158)。
4. 预期结果：四步在源码里被 5 个打点切成 4 段区间，正好可以算出每步耗时（前提是 `TIMING_RECORD_ENABLED` 打开）。
5. 若你不确定：**待本地验证**（默认 `TIMING_RECORD_ENABLED=false`，见 [config.cuh:46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L46)，需要手动改 true 才会真的写 timing）。

#### 4.1.5 小练习与答案

**Q1**：四步里，哪一步是唯一会让 controller「阻塞等 worker」的？  
**答**：严格说四步本身都不等 worker；等待发生在每轮开头的「Step 0 回收」里（`wait(instruction_finished)`）。四步的终点是 `arrive(instruction_arrived)`，这一步立即返回。所以「等 worker」是下一轮复用同一个槽时才发生的。

**Q2**：为什么 `num_semaphores` 要做成「按槽位记录」的数组，而不是一个普通变量？  
**答**：因为流水线有 2 个槽在飞。收尾（Step 0）要 invalidate 的，是**这个槽上一轮**建的信号量个数，而不是「最近一条」的个数。用数组按 `ring` 索引，才能对上号。

---

### 4.2 instruction_ring 环形 buffer 与双相节拍

#### 4.2.1 概念说明

这是本讲最容易绕晕、也最关键的一块。核心矛盾是：

- 全局内存里可能有成百上千条指令（`num_iters` 很大）；
- 但共享内存里只准备了 `STAGES = 2` 个物理指令槽（`instruction_state_t[2]`）。

所以 controller 必须**反复复用**这 2 个槽。槽 0 先装第 0 条，再装第 2 条，再装第 4 条……槽 1 装第 1、3、5……条。这就是 `instruction_ring = instruction_index % 2` 的来历。

复用带来一个硬约束：**当 controller 想把第 4 条写进槽 0 时，第 0 条必须已经被所有 worker 彻底跑完**——否则 controller 一覆盖 `instructions` / `pid_order` / `semaphores`，还在读旧数据的 worker 就会读到垃圾。这就是「处理新指令前要先 `wait` 上一条的 `instruction_finished`」的根本原因。

而 `instruction_finished[0]` 这个信号量本身也会被反复复用（第 0 条用它、第 2 条用它、第 4 条还用它），于是需要**相位位**来区分「这一信号现在指的是第几轮」。

#### 4.2.2 核心流程

复用同一个槽 `r` 的若干轮，相位位是这样翻转的（以 STAGES=2 为例）：

| `instruction_index` | `instruction_ring` | `phase = (idx/2)&1` | 这条占用槽 ring 的「第几次」 |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 槽 0 第 1 次 |
| 1 | 1 | 0 | 槽 1 第 1 次 |
| 2 | 0 | 1 | 槽 0 第 2 次（相位翻成 1） |
| 3 | 1 | 1 | 槽 1 第 2 次（相位翻成 1） |
| 4 | 0 | 0 | 槽 0 第 3 次（相位翻回 0） |

关键不变式（请记住）：

\[
\text{controller 在处理第 } i \text{ 条时，若 } i \geq \text{STAGES}，
\text{它等待的是第 } (i - \text{STAGES}) \text{ 条的完成信号}
\]

而那条「上一任房客」完成时，敲的是 `instruction_finished[ring]` 的相位：

\[
\text{phase\_to\_wait} = \left\lfloor (i - \text{STAGES}) / \text{STAGES} \right\rfloor\ \&\ 1
\]

整条流水可以用一张「时间轴」理解（W=worker 群，C=controller）：

```
时间 →
C: [布置第0条][布置第1条][wait f0][布置第2条][wait f1][布置第3条][wait f0][布置第4条]...
W:                 [跑第0条 ............][跑第1条 ............][跑第2条 ...]
                                    ↑ 第0条跑完,f0置位 → C 才敢复用槽0装第2条
```

因为布置（C）很快、执行（W）较慢，所以「布置第 i+STAGES 条」必须卡在「第 i 条执行完」之后，否则槽位冲突。

#### 4.2.3 源码精读

「先 wait 上一条」的代码就在每轮开头：

[controller.cuh:33-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L33-L46) —— **Step 0：回收上一条**。只有 `instruction_index >= STAGES` 时才执行（前两条是槽的「首次使用」，没有上一任房客）。其中：

- [controller.cuh:34-35](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L34-L35) 算出「上一任房客」的绝对指令号 `last_slot_instruction_index = index - STAGES`。
- [controller.cuh:37-40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L37-L40) 算相位位并 `wait(instruction_finished[ring], phasebit)`——**这就是「处理新指令前先等上一条」的那一行**。
- [controller.cuh:42-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L42-L46) 用 `laneid < num_semaphores[ring]` 把上一条建的动态信号量**并行 invalidate**（每个 lane 清一个）。

对比一下「消费者」一侧的节拍，能看得更清楚。worker 在 [util.cuh:122-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L140) 封装了 `await_instruction()` / `next_instruction()` 这一对：

- `await_instruction()`：`wait(instruction_arrived[ring], phase)`——等 controller 布置好。
- `next_instruction()`：lane 0 `arrive(instruction_finished[ring])`，然后推进游标。

也就是说：controller 在 `instruction_arrived` 上 arrive / worker 在它上 wait；worker 在 `instruction_finished` 上 arrive / controller 在它上 wait。**两个信号量方向相反，构成完整的一来一回。** controller 没有（也不需要）调 `next_instruction()`，因为它用自家的 `for` 循环 + Step 0 直接管理这两个节拍。

#### 4.2.4 代码实践

**实践：手推一张「指令号 ↔ 槽位 ↔ 相位 ↔ 等谁」的表。**

1. 目标：把上面的不变式吃透，能预测任意一条指令会等谁。
2. 步骤：假设 `num_iters = 6`，`STAGES = 2`，画一张 6 行的表，列分别是 `index`、`ring`、`phase_of(index)`、`是否进 Step0`、`若进 Step0，等的是哪条的 finished`、`该 finished 的相位`。
3. 需要观察的现象：第 2、3、4、5 条都会进 Step 0；第 2 条等第 0 条（相位 0），第 4 条等第 2 条（相位 1）……
4. 预期结果（参考答案）：

   | index | ring | phase(idx) | 进 Step0? | 等 finished[?] 的相位 |
   | --- | --- | --- | --- | --- |
   | 0 | 0 | 0 | 否 | — |
   | 1 | 1 | 0 | 否 | — |
   | 2 | 0 | 1 | 是 | finished[0], phase = (0/2)&1 = 0 |
   | 3 | 1 | 1 | 是 | finished[1], phase = (1/2)&1 = 0 |
   | 4 | 0 | 0 | 是 | finished[0], phase = (2/2)&1 = 1 |
   | 5 | 1 | 0 | 是 | finished[1], phase = (3/2)&1 = 1 |

5. 注意第 4 行：同样是 `finished[0]`，第 2 条等的是相位 0、第 4 条等的是相位 1——这正是「同一个信号量被复用、靠相位位区分轮次」的体现。

#### 4.2.5 小练习与答案

**Q1**：如果 controller **不**在 Step 0 里 `wait`，直接覆盖槽 0 装第 2 条，会发生什么？  
**答**：第 0 条可能还在被 worker 执行（读 `instruction_state[0]` 里的 `instructions` / `pid_order` / `semaphores`）。controller 覆盖后，worker 会读到第 2 条的数据，行为错乱——典型数据竞争 / 流水线冒险。

**Q2**：为什么阈值是「`instruction_index >= STAGES`」才进 Step 0，而不是 `>= 1`？  
**答**：槽位 r 第一次被使用是在 `index == r`（r < STAGES），此时它之前没被任何指令用过，没有「上一任房客」要回收。要到 `index == r + STAGES` 才会第二次复用槽 r。所以判据是 `>= STAGES`。

---

### 4.3 Step 1 取指 与 Step 2 建立物理页序

#### 4.3.1 概念说明

**Step 1 取指**：一条指令是 32 个 `int`（`INSTRUCTION_WIDTH = 32`，即 128 字节），存在全局内存的 `g.instructions[worker_id][index][0..31]` 里。controller 用一个 warp 的 32 个 lane，**一人搬一个字**，一次性把整条指令读进共享内存槽。第 0 个字 `instruction()[0]` 约定为 **opcode**。

**Step 2 建立物理页序**：这是全讲最绕的一点。`pid_order[NUM_PAGES]` 是一张「逻辑页 lid → 物理页 pid」的映射表。但本条的 `pid_order` **不是由本条指令决定的，而是由【上一条】指令决定的**——因为「哪些物理页现在空出来了」取决于「上一条指令消费完了哪些页」。所以 controller 看的是 `last_instruction_ring`（环上的前一个槽）里那条指令的 opcode，问它「你释放了哪些 lid」，再把这些 lid 映射成 pid，填进本条的 `pid_order`。

> 旁注：page 分配的完整语义（`release_lid` 到底怎么算）属于页分配器专题。本讲只需明白：Step 2 的产出是一张写进当前指令槽的 `pid_order` 表，供本条指令的 worker 用 `pid(lid)` 查询物理页。

#### 4.3.2 核心流程

```
Step 1 取指:
    src = &g.instructions[worker_id][index][0]
    if laneid < INSTRUCTION_WIDTH: instruction()[laneid] = src[laneid]   # 并行搬运

Step 2 页序:
    last_ring = (ring + STAGES - 1) % STAGES          # 环上的「前一个槽」
    if index == 0:                                     # 首条：恒等映射
        if laneid < NUM_PAGES: pid_order()[laneid] = laneid
    else:
        last_opcode = all_instructions[last_ring].instructions[0]
        if laneid < NUM_PAGES:
            lid = dispatch(release_lid, last_opcode, last_instruction, laneid)  # 问上一条：laneid 号逻辑页现在映射到谁？
            pid_order()[laneid] = all_instructions[last_ring].pid_order[lid]
```

注意 `last_instruction_ring = (ring + STAGES - 1) % STAGES`：当 STAGES=2 时，它就是 `1 - ring`（另一个槽）。也就是说，「上一条」永远住在另一个槽里——因为相邻两条指令必然占用不同的槽。

#### 4.3.3 源码精读

**Step 1**：

[controller.cuh:66-72](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L66-L72) —— 调 `load_instructions` 取指，随后 lane 0 打点 `IFETCH_DONE`。

`load_instructions` 的实现见 [instruction_fetch.cuh:10-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L10-L27)。关键两行：

- [instruction_fetch.cuh:16-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L16-L17) 算出源指针：`&g.instructions[worker_id][instruction_index][0]`——**按 worker_id 取本 SM 自己那份指令流**（不同 SM 处理不同的指令序列，见 `get_worker_id()`，[util.cuh:27-30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L27-L30)）。
- [instruction_fetch.cuh:24-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L24-L26) `laneid` 号 lane 搬第 `laneid` 个字。`INSTRUCTION_WIDTH=32` 恰好等于一个 warp 的宽度，所以一个 warp 一拍搬完整条指令。

`instruction()` 访问器见 [util.cuh:83-89](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L83-L89)，它返回 `all_instructions[instruction_ring].instructions`——即「当前槽」的指令数组。

**Step 2**：

[controller.cuh:74-103](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L74-L103) —— 完整的 Step 2。其中：

- [controller.cuh:75-77](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L75-L77) 算 `last_instruction_ring`（另一个槽）。
- [controller.cuh:79-82](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L79-L82) 首条指令走恒等映射 `pid_order()[laneid] = laneid`。
- [controller.cuh:83-98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L83-L98) 非首条：用 `dispatch_op` 按 `last_opcode` 找到对应 op，调它的 `release_lid`（[page_allocator.cuh:13-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L13-L17)），把结果经上一条的 `pid_order` 翻译后写进本条 `pid_order`。

`dispatch_op` 是个模板「op 列表遍历器」，定义在 [util.cuh:32-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L32-L55)：它拿一个 `opcode`，在编译期展开的 op 列表里逐个比 `opcode == op::opcode`，命中就调对应 dispatcher；全不命中就 `trap`（[util.cuh:38](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L38)）——也就是遇到未知 opcode 直接炸内核，这是有意为之的「fail fast」。

#### 4.3.4 代码实践

**实践：阅读 `load_instructions`，确认「每个 lane 搬一个字」。**

1. 目标：验证 Step 1 是 warp 级并行搬运，而非 lane 0 串行搬 32 次。
2. 步骤：打开 [instruction_fetch.cuh:24-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L24-L26)，看条件 `if (laneid < config::INSTRUCTION_WIDTH)`。
3. 观察现象：搬运动作以 lane 为单位，无循环、无串行。
4. 预期结果：你能解释为什么 `INSTRUCTION_WIDTH` 必须恰好 ≤ 32（见 [instruction_fetch.cuh:22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L22) 的 `static_assert`）——因为一个 warp 只有 32 个 lane。
5. 思考（不必运行）：如果把 `INSTRUCTION_WIDTH` 改成 48，会发生什么？→ 一拍搬不完，需要两个 warp 协作或分两拍，现有代码会 assert 失败。

#### 4.3.5 小练习与答案

**Q1**：Step 2 里，`pid_order()` 写的是**当前槽**还是上一条槽？它读的又是哪个槽？  
**答**：写的是当前槽 `all_instructions[instruction_ring].pid_order`（[util.cuh:96-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L96-L101)）；读的是上一条槽 `all_instructions[last_instruction_ring]` 的 `instructions` 和 `pid_order`。一写当前、一读上一条。

**Q2**：为什么首条指令（index==0）要特殊处理成恒等映射？  
**答**：首条之前没有「上一条」可问 `release_lid`。此时所有物理页都空闲且顺序未定，最自然的就是 `pid_order[i] = i`（逻辑页 i 就是物理页 i），让后续指令在此基础上开始轮转。

---

### 4.4 Step 3 构造动态信号量 与 Step 4 通知就绪

#### 4.4.1 概念说明

**Step 3 构造信号量**：每条指令在执行时，可能需要若干个「动态信号量」来表达自己的数据依赖（比如「K 页到了吗」「O 部分和写完了吗」）。这些信号量住在指令槽的 `semaphores[DYNAMIC_SEMAPHORES]` 数组里（`DYNAMIC_SEMAPHORES = 32`，见 [util.cuh:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L17)）。controller 的工作是：根据本条 opcode，调用对应 op 的 `init_semaphores`，让它在这个槽里 `init` 自己要用的信号量，并返回个数 `n`。这个 `n` 会被广播给全 warp，并存进 `num_semaphores[ring]`，供日后回收。

一个特例：**opcode == 0 是 NoOp**（见 [megakernel.cuh:158-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L158-L163) 的 `NoOp`）。NoOp 不需要任何动态信号量，所以直接置 `num_semaphores[ring] = 0`，跳过分发。

**Step 4 通知就绪**：前三步把舞台搭好后，lane 0 在 `instruction_arrived[ring]` 上 `arrive(..., 1)`。因为 `instruction_arrived` 的 init 阈值是 1，这一次 arrive 就能让所有 `wait` 它的 worker 放行——「这条指令准备好了，开干」。

#### 4.4.2 核心流程

```
Step 3:
    opcode = instruction()[0]
    if opcode == 0:                       # NoOp
        num_semaphores[ring] = 0
    else:
        if laneid == 0:
            n = dispatch(init_semaphores, opcode, g, kvms)   # 只让 lane0 跑，避免重复 init
        num_semaphores[ring] = __shfl_sync(0xffffffff, n, 0) # 广播给全 warp
    record(SEMS_SETUP)

Step 4:
    if laneid == 0: arrive(instruction_arrived[ring], 1)      # 敲门，worker 放行
```

注意 Step 3 的一个细节：`init_semaphores` **只在 lane 0 执行**（[controller.cuh:110](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L110)），因为 init 信号量是「一次性」动作，不能 32 个 lane 各 init 一遍。但 `n`（个数）需要被全 warp 知道，于是用 `__shfl_sync` 从 lane 0 广播出去（[controller.cuh:119-123](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L119-L123)）。

#### 4.4.3 源码精读

[controller.cuh:105-131](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L105-L131) —— 完整的 Step 3 + Step 4。逐段：

- [controller.cuh:106-108](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L106-L108) NoOp 快路径：`num_semaphores[ring] = 0`。
- [controller.cuh:110-117](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L110-L117) lane 0 跑 `dispatch_op<semaphore_constructor_op_dispatcher>`，命中后调 `op::controller::init_semaphores(g, kvms)`（[semaphore_constructor.cuh:13-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L13-L18)）。注意 [semaphore_constructor.cuh:16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L16) 紧跟一句 `fence.proxy.async.shared::cta`——保证刚 init 的信号量对异步代理（mbarriage）可见。
- [controller.cuh:119-123](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L119-L123) `__shfl_sync(0xffffffff, n, 0)` 把 lane 0 的 `n` 广播到所有 lane。
- [controller.cuh:126-131](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L126-L131) lane 0 打点 `SEMS_SETUP`，然后 `arrive(instruction_arrived[ring], 1)`——**Step 4，本条的「布置」到此结束**。

`instruction_arrived` 的 init 阈值 1 来自主内核 [megakernel.cuh:82-86](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L82-L86)（`init_semaphore(instruction_arrived[i], 1)`）；`instruction_finished` 阈值 19 = `NUM_WARPS - 1`（[megakernel.cuh:84-85](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L84-L85)）。这正好对上：controller arrive 1 次就让 worker 放行；19 个 worker（16 consumer + loader + storer + launcher，controller 自己不算）各 arrive 1 次累计 19 次让 controller 放行。

#### 4.4.4 代码实践

**实践：找一个真实 op 的 `init_semaphores`，看它返回什么。**

1. 目标：把「dispatch → init_semaphores → 返回个数」这条链落到一个真实 op 上。
2. 步骤：在 `demos/low-latency-llama/` 下搜索 `init_semaphores`，例如 [matvec_pipeline.cuh:104-113](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L104-L113) 里的 `pipeline::init_semaphores`。
3. 观察现象：你会看到它调用若干次 `init_semaphore(...)`，并 `return` 一个数字（信号量个数）。
4. 预期结果：这个返回值就是 controller 存进 `num_semaphores[ring]` 的那个 `n`，也是 Step 0 回收时要 invalidate 的信号量个数。
5. 若不确定该 op 的个数含义：**待本地验证**（不同 op 返回值不同，以源码为准）。

#### 4.4.5 小练习与答案

**Q1**：为什么 `init_semaphores` 只在 lane 0 跑，而 Step 0 的 `invalidate` 却是 `laneid < n` 并行跑？  
**答**：`init` 是「建」信号量，只能建一次，所以单 lane 做；`invalidate` 是「逐个清」信号量，每个信号量互相独立，正好可以一个 lane 清一个、并行做。`n` 就是用来界定「有几个 lane 要参与」。

**Q2**：`__shfl_sync(0xffffffff, n, 0)` 里的 `0xffffffff` 和最后那个 `0` 各是什么意思？  
**答**：`0xffffffff` 是「参与 shuffle 的 lane 掩码」，这里表示全 32 个 lane 都参与；最后的 `0` 是「源 lane 编号」，即从 lane 0 取值广播给所有人。

---

### 4.5 结尾收尾：排空（drain）与 timings 回写

#### 4.5.1 概念说明

主 `for` 循环跑完 `num_iters` 条指令后，**流水线并没有真的空**：最后 `STAGES` 条指令（本例是最后 2 条）刚刚被 controller「布置」出去，worker 可能还在跑，对应的动态信号量也还没 invalidate、timings 也还没写回。所以 controller 在循环之后还有一段**收尾（drain）循环**，把剩余在飞的槽一个个排空。

「排空一个槽」做三件事，和主循环开头的 Step 0 完全对称：

1. `wait(instruction_finished[ring], phase)`——等 worker 跑完这个槽里的指令；
2. invalidate 这个槽的动态信号量；
3. 把这个槽的 timings 用 TMA 写回全局内存并清零（`store_timings_and_reset`）。

timings 的回写只在 `TIMING_RECORD_ENABLED` 时才有意义（默认 false），但它体现了 controller 的另一项职责：**记录自己每一步的耗时**，供 host 端做性能剖析。

#### 4.5.2 核心流程

```
# 主循环结束后
for i in 0 .. STAGES-1:
    index = num_iters - STAGES + i                  # 还在飞的指令号
    if index < 0: continue                          # 指令总数 < STAGES 时跳过
    ring = index % STAGES
    phase = (index / STAGES) & 1
    wait(instruction_finished[ring], phase)
    if laneid < num_semaphores[ring]: invalidate(all_instructions[ring].semaphores[laneid])
    lane0: record(CONTROLLER_END)
    store_timings_and_reset(&all_instructions[ring].timings[0], index, g)
```

注意 `if (instruction_index < 0) continue;`：当指令总数本身少于 STAGES（比如只有 1 条指令）时，`num_iters - STAGES + i` 会算出负数，这些「不存在的上一任」要跳过。

#### 4.5.3 源码精读

[controller.cuh:134-164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L134-L164) —— 完整 drain 循环。关键点：

- [controller.cuh:135-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L135-L140) 遍历剩余 STAGES 个槽，跳过负索引。
- [controller.cuh:145-147](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L145-L147) 算 `ring` 和 `phase`，`wait(instruction_finished[ring], phase)`。
- [controller.cuh:149-152](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L149-L152) 并行 invalidate。
- [controller.cuh:157-163](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L157-L163) lane 0 打点 `CONTROLLER_END`，然后 `store_timings_and_reset`。

`store_timings_and_reset` 见 [timings_store.cuh:25-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L25-L46)：它先（lane 0）调 `store_timings`，用 `cp.async.bulk`（TMA）把整段 timing 数组从 shared 拷到 global（[timings_store.cuh:14-22](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L14-L22)），再把这段内存清零以便下轮复用（[timings_store.cuh:40-45](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L40-L45)）。

补充：主循环开头的 Step 0 里也有一处 timings 回写（[controller.cuh:52-59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L52-L59)），它和 drain 里那处是**同一个动作的两种触发时机**：Step 0 是「复用槽时顺手回收上一条的 timing」，drain 是「全部结束后回收最后几条的 timing」。两者合起来保证每条指令的 timing 都被写回且仅写回一次。

`record()` 本身定义在 [util.cuh:190-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L190-L196)：它用 `clock64()` 减去 `start_clock` 得到相对 ticks，写进 `timing()[event_id]`。timing 事件常量表见 [util.cuh:215-219](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L219)。

#### 4.5.4 代码实践

**实践：核对「两条 timings 回写路径」覆盖了所有指令。**

1. 目标：确认每条指令的 timing 恰好被写回一次，没有遗漏或重复。
2. 步骤：
   - 设 `num_iters = 5`，`STAGES = 2`。
   - 主循环 Step 0 在 `index >= STAGES` 时回收 `index - STAGES`，即回收第 0、1、2 条的 timing（分别在第 2、3、4 条的开头）。
   - drain 循环回收 `num_iters - STAGES + i` = `3, 4` 两条。
3. 观察现象：Step 0 回收 {0,1,2}，drain 回收 {3,4}，并集 = {0,1,2,3,4}，无重复。
4. 预期结果：5 条指令的 timing 各被写回一次，证毕。
5. 若你想真正看到 timing 数据：需把 [config.cuh:46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L46) 的 `TIMING_RECORD_ENABLED` 改为 true 后重新编译运行，**待本地验证**。

#### 4.5.5 小练习与答案

**Q1**：drain 循环里为什么 `store_timings_and_reset` 没有像 Step 0 那样包在 `if constexpr (TIMING_RECORD_ENABLED)` 里？  
**答**：实际上 Step 0 的 timing 回写包在 `if constexpr` 里（[controller.cuh:52-59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L52-L59)）；drain 里 [controller.cuh:161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L161) 没有外层 `if constexpr`，但 `store_timings_and_reset` 内部的「清零」逻辑无论是否记录 timing 都会把槽清零，便于下一轮（若内核被复用）。注意源码注释 [controller.cuh:160](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L160) 也写了 "technically don't need to reset, whatevs?"——这是作者承认收尾清零并非严格必需。

**Q2**：如果 `num_iters = 1`（只有一条指令），drain 循环会发生什么？  
**答**：`i=0` 时 `index = 1 - 2 + 0 = -1 < 0`，被 `continue` 跳过；`i=1` 时 `index = 0`，正常处理：`wait(instruction_finished[0], phase=0)`，invalidate，回写 timing。即唯一一条指令在 drain 阶段被排空，没有遗漏。

---

## 5. 综合实践

把本讲的三块内容（四步流程、环形双相流水、收尾 drain）串起来，完成下面这个**纸上推演任务**。

**任务背景**：假设 `num_iters = 4`，`STAGES = 2`，`NUM_WARPS = 20`，所有 worker 执行一条指令恰好耗时 T，controller 布置一条指令耗时 t（且 t ≪ T）。

**任务 A：画 controller 处理一条指令的四步时序图。**

针对「第 0 条指令」（首条，index=0），画出如下时序（横轴为时间，单位 t/T）：

```
controller (ring=0, phase=0):
  |─ CONTROLLER_START
  |─ Step1 取指 ───────────── IFETCH_DONE
  |─ Step2 页序(恒等映射) ─── PAGE_ALLOC_DONE
  |─ Step3 init_semaphores ── SEMS_SETUP
  |─ Step4 arrive(instruction_arrived[0]) ──▶ worker 放行
  [controller 立即进入 index=1 的布置，不等 worker]
workers (ring=0):
                         [接到 arrived[0] 后] ─── 执行第0条(耗时T) ─── arrive(instruction_finished[0])×19
```

请补全「第 2 条指令」（index=2，首次复用槽 0）的时序图，要求显式画出：
- 开头的 Step 0：`wait(instruction_finished[0], phase=0)`——这一格**必须**落在「第 0 条的 19 次 finished arrive」之后；
- invalidate 槽 0 的 `num_semaphores[0]` 个信号量；
- 四步布置；
- Step 4 `arrive(instruction_arrived[0], phase=1)`。

**任务 B：解释为什么处理新指令前要先 wait 上一条的 instruction_finished。**

用你画的第 2 条时序图回答以下三点：
1. 如果**去掉**这个 wait，controller 会在第 0 条执行到一半时覆盖 `all_instructions[0]` 的哪些字段？（提示：`instructions`、`pid_order`、`semaphores`、`timings`、`scratch`，结构定义见 [util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19)）。
2. 为什么 worker 在 `instruction_finished` 上 arrive 的次数必须是 **19** 而不是 20？（提示：controller 自己不调 `next_instruction`。）
3. 第 2 条 wait 的相位为什么是 0，而第 4 条（若 `num_iters=5`）wait 同一个 `instruction_finished[0]` 的相位却是 1？（提示：`(index/STAGES)&1`。）

**参考答案要点**：
- (1) 覆盖会让还在读槽 0 的 worker 读到第 2 条的指令/页表/信号量，是典型的读—写冒险；这正是双缓冲 + 相位位要解决的核心问题。
- (2) `instruction_finished` init 阈值 = `NUM_WARPS - 1` = 19（[megakernel.cuh:84-85](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L84-L85)）。20 个 warp 里 controller 不参与「完成上报」，其余 19 个各 arrive 一次，累计 19 次正好让 controller 的 wait 放行。
- (3) 因为 `instruction_finished[0]` 被反复复用：第 0 条完成时它在相位 0 翻转，第 2 条完成时在相位 1 翻转。controller 等第 0 条用相位 `(0/2)&1=0`，等第 2 条用相位 `(2/2)&1=1`。相位位是区分「同一个信号量第几轮」的唯一手段。

> 说明：以上为源码阅读型推演，**不需要 GPU 也能完成**。若想在真机上验证时序，可开启 `TIMING_RECORD_ENABLED` 并读取写回的 `CONTROLLER_START`/`CONTROLLER_END` 等事件 ticks，画出实测时序——这部分**待本地验证**。

## 6. 本讲小结

- controller 是整个 VM 的「大脑」：它**不执行**指令，而是为每条指令**搭舞台**——四步：取指 → 建物理页序 → 构造动态信号量 → 通知就绪（[controller.cuh:24-131](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L24-L131)）。
- 共享内存只有 `STAGES=2` 个指令槽，靠 `instruction_ring = index % 2` 反复复用；复用前必须 `wait(instruction_finished[ring], phase)` 确保上一任房客已跑完（[controller.cuh:33-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L33-L46)）。
- 「双相节拍」：`instruction_arrived`（controller→workers，阈值 1）和 `instruction_finished`（workers→controller，阈值 19）方向相反，配合相位位 `(index/STAGES)&1` 区分轮次，把 controller 与 19 个 worker 串成生产者—消费者流水。
- Step 2 的 `pid_order` 由**上一条**指令的 opcode 决定（`release_lid`），首条走恒等映射；Step 3 的 `init_semaphores` 按 opcode 分发，NoOp（opcode 0）直接置 0。
- controller 是一个 warp，记账工作（搬指令、填页表、清信号量）被拆给各 lane 并行，只有 arrive / record / init 这类一次性动作交给 lane 0。
- 主循环结束后还有 drain 循环，把最后 `STAGES` 条在飞的指令排空（wait + invalidate + 回写 timing），与主循环开头的 Step 0 共同保证每条指令的 timing 恰好写回一次。

## 7. 下一步学习建议

- **下一讲（建议）**：精读 controller 的三个子组件——`load_instructions`（[instruction_fetch.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh)）、`page_allocator`（[page_allocator.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh)）、`semaphore_constructor`（[semaphore_constructor.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh)）。注意这三个文件里各有一个「独立变体」的 `*_loop`（如 `instruction_fetch_loop` / `page_allocator_loop` / `semaphore_constructor_loop`），它们是把 controller 拆成多个 warp 的另一种实现思路，对比着读能更懂本讲「单 warp 内联四步」的设计取舍。
- **延伸阅读**：
  - `op` 的 controller 钩子：在任意一个 demo（如 [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh)）里看 `struct controller { init_semaphores / release_lid }`，理解 Step 2 / Step 3 的另一端。
  - timing 体系：[util.cuh:215-246](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L246) 的全部 `TEVENT_*` 常量，以及 host 端如何读取 `g.timings`。
  - 信号量内部机制（mbarriage、相位翻转的底层）预留给后续「动态信号量与相位位双缓冲」专题。
