# VM 状态 state 与 instruction_state_t

> 本讲对应手册单元 U5·L3，承接 [U5·L2]（内核入口与 warp 角色特化）。上一讲你看到了 `mk` 内核如何把 20 个 warp 分成「16 个 consumer + loader/storer/launcher/controller 四个服务 warp」，并把一大块共享内存切给它们共享。但有一个关键问题被略过了：**这些 warp 怎么「看到同一条指令」？controller 把指令准备好之后，又是怎么告诉其余 19 个 warp「可以开始干了」的？** 答案都藏在 [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) 里的两个结构体——`instruction_state_t`（共享内存里「一条在飞指令」的全部状态）和 `state`（每个 warp 手里那份指向这些共享状态的「VM 运行态」引用）。本讲就把它们逐字段拆开。

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出 `instruction_state_t` 的字段排布，解释为什么它要 `__align__(128)`、为什么 `pid_order` 后面要补一段 `padding` 凑到 32 的倍数。
2. 说清 `state` 里 `instruction_index` 与 `instruction_ring` 的区别，并用「绝对指令号 / 环形槽位」推演出一组 (index, ring, phase) 的取值表。
3. 解释 `pid(int lid)` 如何把**逻辑页号 lid** 映射成**物理页号 pid**，以及为什么这个映射要先在 `await_instruction()` 里缓存成 `pid_order_shared_addr`。
4. 描述 `await_instruction()` / `next_instruction()` 这一对方法如何构成每条指令的「节拍」，并把 controller 与其余 worker 用 `instruction_arrived` / `instruction_finished` 两个信号量串成一条流水。
5. 手动追踪一条指令从「controller 通知就绪」到「所有 worker 报告完成」的完整信号量流转。

## 2. 前置知识

- **环形缓冲（ring buffer）与双缓冲（double buffering）**：GPU 内核里常见一种技巧——准备两份等大的缓冲「槽位」，生产者写 A、消费者读 B，下一拍互换。这样访存与计算可以重叠。本讲的指令流水就是「2 级双缓冲」：`INSTRUCTION_PIPELINE_STAGES = 2`（见 [U5·L1]）。
- **相位位（phase bit）**：当一份物理缓冲被反复复用时，光靠「它准备好了吗」不够，还得知道「这是第几轮」。最简单的办法是让等待方带一个会在 0/1 之间翻转的「相位位」一起等。本讲会反复用到这个公式：`(instruction_index / INSTRUCTION_PIPELINE_STAGES) & 1`。
- **共享内存地址空间转换 `__cvta_generic_to_shared`**：CUDA 里 shared memory 有自己独立的地址空间。要把一个「通用指针」转成「shared 地址」（一个 32 位整数），用内建函数 `__cvta_generic_to_shared`。转成整数地址后，就能用 `lds`（load shared）这类按字节偏移直接寻址的快速指令读取。本讲 `pid()` 就依赖这套。
- **kittens 信号量 `kittens::semaphore`**：本仓库依赖的 ThunderKittens 提供的异步信号量，核心四个动作是 `init / wait / arrive / invalidate`。它内部基于 GPU 的 mbarriage 机制，`wait(sem, phase)` 会阻塞到「当前相位累计的到达次数达到初始化时设的阈值」。**本讲只用它的高层语义**，内部相位翻转的细节留给 [U7·L2]（动态信号量与相位位双缓冲）。
- **warp 内同步**：`__syncwarp()` 让一个 warp 内 32 个 lane 对齐；`kittens::laneid()` 返回 lane 编号（0–31）、`kittens::warpid()` 返回 warp 编号。本讲多处「只有 lane 0 去到达信号量」就是靠 `laneid() == 0` 控制的。
- **逻辑页 lid / 物理页 pid**：仓库在 [util.cuh:8-9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L8-L9) 开头就写明了约定——`lid` 是逻辑页号（「某个 op 想要的第几页」，按语义命名，如「输入页 0」「权重页 1」），`pid` 是物理页号（共享内存里 `pages[]` 数组的真实下标）。一个 op 不会直接知道自己的数据落在哪个物理页，必须通过 `pid(lid)` 查表。

> 如果你忘了 `INSTRUCTION_PIPELINE_STAGES`、`NUM_PAGES`、`DYNAMIC_SEMAPHORES` 这些常量的值，请先回看 [U5·L1] 第 4 节。本讲会直接引用它们：`INSTRUCTION_PIPELINE_STAGES = 2`、`INSTRUCTION_PIPELINE_STAGES_BITS = 1`、`NUM_PAGES = 13`、`DYNAMIC_SEMAPHORES = 32`、`NUM_WARPS = 20`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `instruction_state_t` / `page` / `state` 的定义 | **本讲主角**，逐字段精读 |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | `default_config` 全部参数 | 提供 `NUM_PAGES` / `INSTRUCTION_WIDTH` / `DYNAMIC_SEMAPHORES` 等尺寸来源 |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | VM 主内核 `mk` | 在共享内存里**实例化** `instruction_state_t`、`pages`、各信号量，并构造 `state` |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | controller warp 主循环 | 在 `instruction_arrived` 上 arrive、在 `instruction_finished` 上 wait，是流水线的「生产者」一端 |
| [include/controller/page_allocator.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh) | 页分配器（独立变体） | 展示了如何把 `pid_order` 写进 `instruction_state_t`，以及 arrive 的等价写法 |
| [include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) | NoOp op | 最小的 `pid(lid)` / `wait_page_ready` / `finish_page` 调用样例 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**(A) `instruction_state_t` 布局**——共享内存里「一条在飞指令」长什么样；**(B) `state` 字段、环形推进与 `pid()` 映射**——每个 warp 手里的运行态引用、index/ring 的关系、逻辑页到物理页的查表；**(C) `await_instruction` / `next_instruction` 的节拍与信号量流转**——指令如何被「领出来」又「交回去」。建议按 A → B → C 顺序读：A 解释数据放哪，B 解释怎么找到当前那条，C 解释谁在什么时刻动它。

---

### 4.1 instruction_state_t：单条在飞指令的共享内存布局

#### 4.1.1 概念说明

megakernel 虚拟机一次要执行成百上千条指令（每个 SM 的队列长度，见 [U4·L2]）。如果每条指令都「从头到尾跑完再开始下一条」，访存和计算就无法重叠，延迟会很差。于是 VM 采用**指令流水**：同时让若干条指令「在飞」（in-flight）——controller 在准备第 N+2 条时，consumer 可能还在算第 N 条。

「同时在飞」就意味着共享内存里必须为**每条在飞的指令**各自保留一份「这条指令的全部状态」。这份状态由 `instruction_state_t` 描述，它是一个模板结构体（参数是 `config`）。VM 一共开 `INSTRUCTION_PIPELINE_STAGES`（= 2）份这样的拷贝，构成一个 2 槽的环形缓冲：第 0 条指令用槽 0，第 1 条用槽 1，第 2 条**复用**槽 0，第 3 条复用槽 1……

注意区分两个层次：

- **`instruction_state_t`**：描述「一条指令」的状态，是**数据布局**。
- **`state`**（4.2 讲）：描述「一个 warp 当前站在哪条指令上」，是一组**引用 + 游标**，它指向若干个 `instruction_state_t`。

#### 4.1.2 核心流程

`instruction_state_t` 的字段按以下顺序紧密排列（全部位于共享内存，128 字节对齐）：

```
instruction_state_t<config>          // 一条在飞指令的全部状态
├─ instructions[32]   // 32 个 int = 128 字节，指令本身（opcode + 参数）
├─ timings[128]       // 128 个 int，本条指令的计时槽（TEVENT_*，见 util.cuh 末尾）
├─ pid_order[13]      // 逻辑页→物理页的映射表（NUM_PAGES=13 个 int）
├─ padding[19]        // 把 pid_order 凑成 32 的倍数（13+19=32）
├─ semaphores[32]     // 本条指令动态申请的信号量（DYNAMIC_SEMAPHORES=32）
└─ scratch[1024]      // 临时 scratch 区（SCRATCH_BYTES=4096 → 1024 个 int）
```

关键设计点：

1. **`instructions` / `timings` 是定长数组**，宽度来自 config（`INSTRUCTION_WIDTH` / `TIMING_WIDTH`）。定长是为了让取指（[U6·L2]）和计时回写（[U6·L3]）能用固定的 stride 寻址。
2. **`pid_order` 是本讲的核心之一**：它是一张「逻辑页号 → 物理页号」的查找表，由 controller 在准备指令时写好（见 4.2.3）。注意它**属于某一条具体指令**，而不是全局的——因为每条指令可能复用不同的物理页组合。
3. **`padding` 把 `pid_order` 补到 32 的倍数**。源码注释写得很直白："Round up to multiple of 32"。这有两层好处：(a) 让 `pid_order[0..NUM_PAGES-1]` 能被一个 32 lane 的 warp 用一条向量化 `lds` 整体读出（13 < 32，多读 19 个不影响正确性）；(b) 保证后面的 `semaphores` 字段落在干净的、对齐的地址上，减少 bank conflict。
4. **`semaphores` 是「按指令」动态分配的信号量池**。每条指令按自己 op 的需要申请若干个信号量（[U6·L3] / [U7·L2]），最多 32 个。它们和指令绑定，指令结束时会被 `invalidate` 回收。
5. **`scratch` 是临时工作区**，每条指令独享 4 KiB。`state::zero_scratch<>()` 用向量化指令把它清零（4.2 会看到）。

#### 4.1.3 源码精读

结构体定义只有几行，但每个字段都对应上面流程里的一个角色：

[include/util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19) 定义 `instruction_state_t`：开头两行注释交代了 `pid`(物理页)/`lid`(逻辑页) 的约定；`__align__(128)` 强制每个 `instruction_state_t` 起始地址按 128 字节对齐，配合 `padding` 让整块「每指令状态」落在干净的缓存行/TMA 友好边界上。`instructions`、`timings`、`pid_order`、`semaphores`、`scratch` 依次排列，`padding` 用一个编译期表达式 `((NUM_PAGES + 31) & ~31) - NUM_PAGES` 算出（13 → 32，需补 19）。

那么「2 份拷贝」在哪里分配？在主内核里：

[include/megakernel.cuh:23-24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L23-L24) 用 `__shared__ ... instruction_state_t<config> instruction_state[config::INSTRUCTION_PIPELINE_STAGES];` 在**静态共享内存**里开了一个长度为 2 的数组。这就是环形缓冲的两个槽位。它属于「静态共享内存」，所以会被 [config.cuh:34-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L34-L37) 的 `STATIC_SHARED_MEMORY` 公式计入——那个公式里的 `SCRATCH_BYTES + (INSTRUCTION_WIDTH + TIMING_WIDTH)*4 + DYNAMIC_SEMAPHORES*8` 正是单个 `instruction_state_t` 的尺寸再乘以 `INSTRUCTION_PIPELINE_STAGES`（精确的字节核算见 [U5·L1]）。

字段尺寸的来源都在 config 里：

| 字段 | 尺寸来源（config） | 值 |
| --- | --- | --- |
| `instructions` | `INSTRUCTION_WIDTH` ([config.cuh:14-15](.../config.cuh#L14-L15)) | 32 个 int |
| `timings` | `TIMING_WIDTH` ([config.cuh:18-19](.../config.cuh#L18-L19)) | 128 个 int |
| `pid_order` / `padding` | `NUM_PAGES` ([config.cuh:43-44](.../config.cuh#L43-L44)) | 13 + 19 = 32 个 int |
| `semaphores` | `DYNAMIC_SEMAPHORES` ([config.cuh:22](.../config.cuh#L22)) | 32 个 |
| `scratch` | `SCRATCH_BYTES` ([config.cuh:33](.../config.cuh#L33)) | 4096 字节 = 1024 个 int |

> （上面表格里的省略链接为排版，完整永久链接见本节正文已给出的 config.cuh 行号引用。）

#### 4.1.4 代码实践

**实践目标**：亲手验证 `padding` 的补齐逻辑，理解「凑到 32 的倍数」对向量化读取的意义。

1. **操作步骤**：
   - 打开 [util.cuh:15-16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L15-L16)，阅读 `padding[((NUM_PAGES + 31) & ~31) - NUM_PAGES]` 这个表达式。
   - 代入 `NUM_PAGES = 13`：`(13 + 31) & ~31 = 44 & 0xFFFFFFE0 = 32`，所以 padding 长度 = `32 - 13 = 19`。
   - 假想把 `NUM_PAGES` 改成 20（仅作思考，**不要真改源码**）：`(20+31)&~31 = 51 & ~31 = 32`，padding = `32-20 = 12`。改成 33：`(33+31)&~31 = 64`，padding = `64-33 = 31`。
2. **需要观察的现象**：无论 `NUM_PAGES` 取多少，`pid_order + padding` 合起来永远是 32 的整数倍。
3. **预期结果**：这正是让一个 32-lane 的 warp 能用**一条**向量化 `lds` 把整张 `pid_order` 表读进寄存器的前提（哪怕只前 13 个有效）。它同时保证 `semaphores` 字段从 32 对齐的地址开始。
4. 「待本地验证」：若你想在编译期确认 padding 长度，可以在 `instruction_state_t` 里临时加一行 `static_assert(((config::NUM_PAGES + 31) & ~31) == config::NUM_PAGES + /*padding size*/, "padding math");`（属于示例代码，仅供练习，勿提交）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `instruction_state_t` 要开 2 份而不是 1 份或 4 份？

> **参考答案**：份数 = `INSTRUCTION_PIPELINE_STAGES` = 2，即「指令流水深度」。开 1 份意味着同一时刻只能有一条指令在飞，访存与计算无法重叠，失去流水意义；开更多份（如 4）能容纳更多在飞指令，但每份都要占静态共享内存（约 5 KiB/份），会挤占留给 `pages` 的动态共享内存、减少 `NUM_PAGES`。2 份是在「流水收益」与「页数成本」之间选的平衡点。

**练习 2**：`pid_order` 为什么放在 `instruction_state_t` 里（每指令一份），而不是做成全局唯一的表？

> **参考答案**：因为每条指令的页映射可以不同。不同 op 释放/申请物理页的顺序不同（见 [U6·L2] 的 `release_lid`），controller 每准备一条指令都要重新算一遍 `pid_order` 并写进**这一条指令**的槽位。若做成全局表，多条在飞指令会互相覆盖。

---

### 4.2 state 字段、环形推进与 pid() 映射

#### 4.2.1 概念说明

`instruction_state_t` 解决了「数据放哪」，但每个 warp 在执行时还需要知道一个动态信息：**「我现在站在第几条指令上？这条指令的数据在哪个槽位？」**。这部分「游标 + 指针集合」就是 `state<config>`。

`state` 有两个特点值得先强调：

1. **它几乎只装「引用」，不装「数据」**。它通过引用 (`&`) 指向 `megakernel.cuh` 里实际分配的那些共享内存对象。所以构造一个 `state` 不会复制数据，只是给每个 warp 一份「带游标的视图」。
2. **游标是「每 warp 一份」的寄存器态**。`instruction_index`、`instruction_ring` 是普通 `int` 成员，每个 warp 各自维护、各自推进。它们是 VM 真正的「程序计数器」。

#### 4.2.2 核心流程

`state` 的核心游标是这一对：

```
int instruction_index;   // 绝对指令号：0,1,2,3,... 单调递增，到 num_iters-1
int instruction_ring;    // 环形槽位：= instruction_index % INSTRUCTION_PIPELINE_STAGES
```

二者的关系是「绝对号」与「槽位号」：

\[
\text{instruction\_ring} = \text{instruction\_index} \bmod \text{INSTRUCTION\_PIPELINE\_STAGES}
\]

环形推进由一个内联工具函数完成：

[include/util.cuh:57-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L57-L58) 定义 `ring_advance<N>` 与 `ring_retreat<N>`。`ring_advance<N>(ring, d) = (ring + d) % N`，即向前走 d 步并在 N 处回绕。`ring_retreat` 用 `ring + 16*N - d` 再 `% N` 的写法，是为了避免 `ring - d` 出现负数（取模对负数的行为在 C++ 里不直观），`16*N` 是一个足够大的、保证非负的偏移。

由此可以列出前几条指令的取值表（这是本讲最重要的对照表，后面 4.3 会反复用）：

| instruction_index | instruction_ring | phase = (index / 2) & 1 | 含义 |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 槽 0 第 1 次使用 |
| 1 | 1 | 0 | 槽 1 第 1 次使用 |
| 2 | 0 | 1 | 槽 0 第 2 次使用（相位翻转） |
| 3 | 1 | 1 | 槽 1 第 2 次使用（相位翻转） |
| 4 | 0 | 0 | 槽 0 第 3 次使用（相位翻回 0） |

注意「相位 phase」这一列——同一物理槽（如 ring=0）每被复用一次，phase 就翻转一次。这正是 4.3 里信号量等待时带的「轮次标记」。

`state` 里还有一条「页映射」热线：把逻辑页 lid 翻译成物理页 pid。流程是：

```
controller 在准备指令时，把 pid_order[0..NUM_PAGES-1] 写进 instruction_state_t.ring
        ↓ （worker 端）
await_instruction()：缓存 pid_order[0] 的 shared 地址 → pid_order_shared_addr
        ↓
pid(lid)：从 pid_order_shared_addr + lid*4 处做一次 lds → 得到物理页号
        ↓
用 pid 去索引 pages[pid] / page_finished[pid]
```

#### 4.2.3 源码精读

先看 `state` 的「头部」——引用与游标：

[include/util.cuh:73-81](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L73-L81) 声明了三组引用和一个游标对：`all_instructions`（指向那 2 个 `instruction_state_t` 槽）；`instruction_arrived`、`instruction_finished`（各 2 个 handoff 信号量，4.3 详述）；以及游标 `instruction_index`、`instruction_ring`。第 81 行还有一个 `reg_pid_order[config::NUM_PAGES]`——这是一块「寄存器侧的 pid_order 副本」声明，但经全仓库检索（grep `reg_pid_order`）目前**仅在声明处出现、未被主路径使用**，可理解为「预留的快速访问缓冲」，当前热路径走的是 `pid_order_shared_addr`（见 4.3）。

接下来是一组**访问器**，它们都通过 `instruction_ring` 索引到「当前这条指令」的字段：

[include/util.cuh:83-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L83-L101) 中 `instruction()` / `timing()` / `pid_order()` 都返回 `all_instructions[instruction_ring].<字段>`。这就是「当前指令」语义的来源——同一个名字（`instruction()`）在不同时刻返回的是不同槽位的数据，完全由 `instruction_ring` 决定。注意它们返回的是**数组引用**（`int (&)[...]`），调用方拿到后可直接当数组用。

`scratch()` 和 `semaphores()` 同理（[util.cuh:102-121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L102-L121)）；其中 [util.cuh:106-112](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L106-L112) 的 `zero_scratch<num_bytes>()` 把 scratch 区解释成一个 `sv_fl`（shared vector float）并用 `kittens::warp::zero` 一次性清零，再 `warp::sync()`——这是「每条指令开始前清空临时区」的向量化写法。

现在看本模块的重点——**逻辑页→物理页映射**。分两步：缓存地址、按地址读取。

[include/util.cuh:150-154](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L150-L154) 是 `pid(int lid)`：用 `kittens::move<int>::lds(ret, pid_order_shared_addr + lid * sizeof(int))` 从 shared 内存里按字节偏移读出一个 int。`sizeof(int) = 4`，所以 `lid * 4` 正好跳到 `pid_order[lid]`。返回值就是该逻辑页对应的物理页号。**这一行就是 lid→pid 的全部实现——一次 shared load。**

那 `pid_order` 表是谁写的？是 controller。看一个最直接的写入样例：

[include/controller/controller.cuh:79-82](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L79-L82) 在第一条指令（`instruction_index == 0`）时，controller warp 用「lane i 写 pid_order[i] = i」建立**恒等映射**——逻辑页 i 就是物理页 i。后续指令则按上一条指令的 opcode 决定如何复用页序（[controller.cuh:83-99](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L83-L99)，详见 [U6·L2]）。无论哪种情况，写入的目标都是 `kvms.pid_order()`，即 `all_instructions[instruction_ring].pid_order`——**当前这条指令**的表。

最后看一个「消费 pid」的真实 op 样例，NoOp：

[include/noop.cuh:24-32](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh#L24-L32) 的 loader 角色：`if (laneid < NUM_PAGES) { auto pid = s.pid(laneid); ... }`。这里「逻辑页 lid = laneid」（即第 i 个 lane 负责第 i 个逻辑页），调用 `s.pid(laneid)` 翻成物理页 pid，然后对该物理页做 `wait_page_ready` / `finish_page`（页的生命周期管理，见 [U7·L1]）。这就是 `pid()` 在实战中的典型用法。

#### 4.2.4 代码实践

**实践目标**：确认「当前指令」语义完全由 `instruction_ring` 决定，并理解 `pid()` 为何要依赖 `await_instruction` 先缓存地址。

1. **操作步骤**：
   - 通读 [util.cuh:83-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L83-L101)，注意 `instruction()` / `pid_order()` 等访问器**没有任何参数**，它们隐式依赖成员 `instruction_ring`。
   - 再看 [util.cuh:122-127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L127) 的 `await_instruction()`，它除了 wait，还顺手把 `pid_order_shared_addr` 设成 `&(pid_order()[0])` 的 shared 地址。
   - 对照 [util.cuh:150-154](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L150-L154) 的 `pid()`：它**只读 `pid_order_shared_addr`**，不重新调用 `pid_order()`。
2. **需要观察的现象**：`pid()` 用的地址是「某一次 await 时拍下的快照」，而不是每次现算。
3. **预期结果**（请用一段话解释）：

   `await_instruction()` 一返回，意味着 controller 已经把**本条指令**的 `pid_order` 写完了（见 4.3 的信号量保证）。此刻拍下 `pid_order[0]` 的 shared 地址存进 `pid_order_shared_addr`，之后这一整条指令里所有的 `pid(lid)` 调用都复用这个基址，只需做 `基址 + lid*4` 的一次 `lds`——**省掉了每次都重新走 `all_instructions[instruction_ring].pid_order` 的指针解引用与 `__cvta_generic_to_shared` 转换**。这是热路径优化：一个 op 可能对多个页各调一次 `pid()`（如 NoOp 对 13 个页都调），把地址计算从「每页一次」摊成「每指令一次」。

   **不变式**：`pid_order_shared_addr` 只在「await 之后、next 之前」有效。`next_instruction()` 会推进 `instruction_ring`，使旧的 `pid_order()` 指向别的槽位——所以下一次必须重新 `await_instruction()` 刷新地址。这正好和 worker 主循环 `await → 干活 → next` 的结构吻合（见 4.3.3 的 MAKE_WORKER）。

4. 「待本地验证」：你可以构想一个反例——若把 `pid_order_shared_addr = ...` 这一行从 `await_instruction` 里删掉、改在 `pid()` 内部每次现算，逻辑上仍正确，但每次 `pid()` 都会多一次地址转换。本实践只做源码阅读，不实际改源码。

#### 4.2.5 小练习与答案

**练习 1**：当 `instruction_index = 5`、`INSTRUCTION_PIPELINE_STAGES = 2` 时，`instruction_ring` 和 phase 各是多少？此刻 `instruction()` 返回的是哪个槽位的数据？

> **参考答案**：`ring = 5 % 2 = 1`；`phase = (5/2) & 1 = 2 & 1 = 0`。`instruction()` 返回 `all_instructions[1].instructions`，即槽位 1 的数据。

**练习 2**：`reg_pid_order`（[util.cuh:81](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L81)）当前被用到了吗？如果你来设计，它适合做什么？

> **参考答案**：经全仓库检索未被使用，目前是预留字段。设计上它适合做「把 `pid_order` 整张表一次性 lds 进寄存器」的缓存，从而让 `pid(lid)` 变成纯寄存器查表（比每次 `lds` 更快）。当前实现选择了另一种优化（缓存 shared 地址、按需 lds），二者是同一目标的不同方案。

**练习 3**：为什么 `pid()` 用 `lid * sizeof(int)` 而不是 `lid` 作为偏移？

> **参考答案**：因为 `pid_order_shared_addr` 是按**字节**寻址的 shared 地址，`pid_order` 元素类型是 `int`（4 字节）。要跳过 `lid` 个 int，就要偏移 `lid * 4` 字节。`sizeof(int)` 写法比硬编码 4 更可移植、可读。

---

### 4.3 await_instruction / next_instruction：节拍与信号量流转

#### 4.3.1 概念说明

有了「数据放哪」（4.1）和「当前指令游标」（4.2），还缺最后一块：**节拍**。controller 是唯一「知道指令内容」的 warp（它负责取指、建页序、构造信号量），其余 19 个 worker warp（16 consumer + loader + storer + launcher）只负责执行。于是必须有一套握手机制：

- controller 准备好一条指令后，**通知**所有 worker：「槽位 `ring` 的第 N 轮数据就绪了」。
- worker 看到「就绪」才开始读这条指令、执行 op。
- worker 执行完，**回报**：「槽位 `ring` 的第 N 轮我用完了」。
- controller 在两条之后要**复用**同一个槽位时，先等所有 worker 都「用完」了它，才敢覆盖。

这套握手由两个 per-槽信号量承担：

| 信号量 | 方向 | 阈值（init count） | 含义 |
| --- | --- | --- | --- |
| `instruction_arrived[ring]` | controller → workers | 1 | 「这条指令准备好了」 |
| `instruction_finished[ring]` | workers → controller | `NUM_WARPS - 1` = 19 | 「所有 worker 都跑完这条了」 |

`state` 把这两个节拍方法封装成一对：`await_instruction()`（领指令）和 `next_instruction()`（交还指令）。每个 worker 的主循环就是 `await → 执行 → next` 的无限重复。

#### 4.3.2 核心流程

一条指令在 worker 视角的生命周期：

```
await_instruction():
   wait(instruction_arrived[ring], phase = (index/STAGES)&1)   # 等 controller 通知
   pid_order_shared_addr = &(pid_order()[0]) 的 shared 地址     # 顺手缓存页表基址
        ↓
   ……执行 op（读 instruction()、调 pid()、读写 pages）……
        ↓
next_instruction():
   __syncwarp()                                                  # warp 内对齐
   if (laneid == 0) arrive(instruction_finished[ring])           # lane0 报告完成
   instruction_index++                                           # 推进游标
   instruction_ring = ring_advance<STAGES>(instruction_ring)     # 推进环
```

两条信号量的「阈值/到达次数」是这样对上的（关键！）：

- `instruction_arrived` 初始化成 1（[megakernel.cuh:83](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L83)），表示「等 1 次到达」。controller 每准备完一条就 `arrive(..., 1)` 一次（[controller.cuh:130](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L130)），正好 1 次 → 所有 wait 的 worker 放行。
- `instruction_finished` 初始化成 `NUM_WARPS - 1` = 19（[megakernel.cuh:84-85](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L84-L85)），表示「等 19 次到达」。**controller 自己不调用 `next_instruction`**（它有自己的循环），其余 19 个 warp（16 consumer + loader + storer + launcher）各调一次、各 arrive 一次 → 累计 19 次 → controller 的 wait 放行。

相位位为什么是 `(index / STAGES) & 1`？因为同一个物理槽每 2 条指令复用一次（STAGES=2），需要用 0/1 区分「这是第几轮复用」。结合 4.2.2 的取值表：index 0,1 → phase 0；index 2,3 → phase 1；index 4,5 → phase 0……同一槽位（如 ring 0 在 index 0、2、4）每复用一次相位翻转一次，于是「准备第 0 轮」与「消费第 0 轮」的双方都用 phase 0，下一轮都用 phase 1，天然对齐，无需显式重置信号量。

#### 4.3.3 源码精读

先看 worker 端的两个节拍方法：

[include/util.cuh:122-127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L127) 的 `await_instruction()`：第一行 `kittens::wait(instruction_arrived[instruction_ring], (instruction_index / config::INSTRUCTION_PIPELINE_STAGES) & 1)` 阻塞到 controller 通知本槽本轮就绪；第二行把当前指令 `pid_order[0]` 的 shared 地址缓存进 `pid_order_shared_addr`（4.2.4 已解释为何要在此刻缓存）。

[include/util.cuh:128-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L128-L140) 的 `next_instruction()`：先 `__syncwarp()` 确保 warp 内所有 lane 都不再读写本指令的共享数据；再由 `laneid == 0` 的 lane 调用 `kittens::arrive(instruction_finished[instruction_ring])`（默认到达计数 1）；最后 `instruction_index++` 并用 `ring_advance<STAGES>` 推进 `instruction_ring`。注意 [util.cuh:131-134](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L131-L134) 有一段 `#ifdef MK_DEBUG` 的 printf，是调试时打印「哪个 thread 在哪个 ring 上到达 finished」，非调试编译时为空。

这套 `await → 执行 → next` 被一个宏固化进每个 worker 的主循环。看 `MAKE_WORKER` 展开后的骨架（`MK_DEBUG` 部分略）：

[include/util.cuh:278-291](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L278-L291) 的 for 循环头是 `for (mks.instruction_index = 0, mks.instruction_ring = 0; mks.instruction_index < num_iters; mks.next_instruction())`，循环体里第一件事就是 `mks.await_instruction();`。所以每个 worker（loader/storer/consumer/launcher）的主循环都严格遵循 `await → record → dispatch_op 派发执行 op → record → next` 的节拍。（这个宏与 `dispatch_op` 的完整展开是 [U5·L4] 的主题，这里只需看到它对 await/next 的调用顺序。）

再看 controller 端的「对称」用法——它 arrive 在 arrived、wait 在 finished：

[include/controller/controller.cuh:130](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L130) controller 在四步流程（取指 / 页分配 / 信号量构造）走完后，`arrive(kvms.instruction_arrived[kvms.instruction_ring], 1)`，把「就绪」信号发给所有 worker。

[include/controller/controller.cuh:33-40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L33-L40) controller 在准备**新**指令、且 `instruction_index >= INSTRUCTION_PIPELINE_STAGES`（即从第 3 条指令起，要复用槽位）时，先 `wait(kvms.instruction_finished[kvms.instruction_ring], phasebit)`——等所有 worker 把这个槽位上一轮用完。`phasebit` 用的是「上一轮占用的指令号」算出来的相位（`last_slot_instruction_index = index - STAGES`），与 worker 当初 arrive 时所处的相位一致。

信号量的初始化（阈值）在主内核里，必须和上面的「到达次数」对得上：

[include/megakernel.cuh:82-86](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L82-L86) `instruction_arrived[i]` 初始化为 1（等 controller 的 1 次 arrive），`instruction_finished[i]` 初始化为 `config::NUM_WARPS - 1` = 19（等 19 个 worker 各 1 次 arrive）。这两个数字是整条流水线能自洽运转的「账本」。

> 补充：[include/controller/page_allocator.cuh:69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L69) 里有一个**独立的** `page_allocator_loop`，它也 `arrive(instruction_arrived[ring], 1)`——这是页分配逻辑的一个独立变体（把页分配单独抽成一个循环），与 `controller.cuh` 主循环里的等价 arrive 写法对照着看，能加深理解。megakernel 实际走的是 `controller::main_loop`（见 [megakernel.cuh:134](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L134) 的 switch case 3）。

#### 4.3.4 代码实践（核心实践：信号量流转追踪）

**实践目标**：追踪一条具体指令（以 index = 2 为例，它**复用** ring 0）从「就绪」到「完成」的完整信号量流转，验证 arrived / finished 的阈值与到达次数自洽。

1. **操作步骤**：
   - 设定场景：worker 正要处理第 2 条指令（`instruction_index = 2`）。查 4.2.2 取值表得 `ring = 0`、`phase = 1`。
   - **第 0 步（controller 侧，更早发生）**：controller 之前在准备 index=2 时，先 `wait(instruction_finished[0], phasebit)`（[controller.cuh:40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L40)）——确保 ring 0 的**上一轮**（index=0）已被所有 worker 用完。这里 `last_slot = 2 - 2 = 0`，`phasebit = (0/2)&1 = 0`，正好对上 index=0 那轮 worker arrive 时的相位。
   - **第 1 步（controller 侧）**：controller 写好 `pid_order`、构造好信号量，然后 `arrive(instruction_arrived[0], 1)`（[controller.cuh:130](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L130)）——把 arrived[0] 在 phase 1 这一轮的计数推到阈值 1。
   - **第 2 步（worker 侧）**：每个 worker 的 `await_instruction()` 里 `wait(instruction_arrived[0], phase=1)`（[util.cuh:122-124](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L124)）放行，随后缓存 `pid_order_shared_addr`。
   - **第 3 步（worker 侧）**：worker 执行 op（如 NoOp 的 loader 调 `pid()`、`finish_page()`）。
   - **第 4 步（worker 侧）**：每个 worker 的 `next_instruction()` 里 lane 0 `arrive(instruction_finished[0])`（[util.cuh:135](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L135)）。19 个 worker 各 arrive 1 次 → 累计 19 次，达到 finished[0] 的阈值 19。
   - **第 5 步（controller 侧，两条之后）**：controller 准备 index=4（又轮到 ring 0）时，会 `wait(instruction_finished[0], phasebit=1)`——等这一轮（index=2）被用完，才允许覆盖 ring 0。
2. **需要观察的现象**：arrived 的「1 次 arrive（controller）」与「19 个 worker 各 wait」匹配；finished 的「19 个 worker 各 arrive」与「controller 1 次 wait」匹配。两边账本完全对得上。
3. **预期结果**：你能画出一条「controller arrive(arrived) → 19×worker wait(arrived) → 19×worker arrive(finished) → controller wait(finished)」的因果链，且每个环节的计数都不悬空。这就是 megakernel 指令流水能无锁自洽运转的根本原因。
4. 「待本地验证」：若开启 `MK_DEBUG` 重新编译并运行（见 [U1·L3] 的编译流程），[util.cuh:132-134](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L132-L134) 的 printf 会在每个 worker 到达 finished 时打印 `(thread, ring)`，可用于核对「19 个 worker 是否都在同一个 ring 上 arrive」。无 GPU 环境下此项标注为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `instruction_finished` 的 init 值误设成 `NUM_WARPS`（20）而不是 `NUM_WARPS - 1`（19），会发生什么？

> **参考答案**：阈值变成 20，但实际只有 19 个 worker 会 arrive（controller 不调 `next_instruction`）。于是 `instruction_finished[ring]` 的计数永远差 1 达不到阈值，controller 的 `wait`（[controller.cuh:40](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L40)）会**永久阻塞**，整个 VM 死锁。这正是 init 值必须精确等于「真正会 arrive 的 warp 数」的原因。

**练习 2**：为什么 worker 在 `next_instruction()` 里只让 `laneid == 0` 去 arrive，而不是 32 个 lane 都 arrive？

> **参考答案**：信号量的「到达」是按**信号**计数的，不是按线程。一个 warp 只应贡献 **1 次**到达（代表「这个 warp 用完了这条指令」）。若 32 个 lane 都 arrive，一个 warp 就贡献 32 次，计数会远超阈值，破坏账本。因此用 `laneid == 0` 代表整个 warp 发出一次到达，再用 `__syncwarp()` 保证发出前 warp 内所有 lane 已完成对本指令共享数据的读写。

**练习 3**：`await_instruction()` 里 wait 的相位用的是 `(instruction_index / STAGES) & 1`，controller 端 wait finished 用的相位是 `(last_slot_instruction_index / STAGES) & 1`，二者为何都用「除以 STAGES 再取最低位」？

> **参考答案**：因为同一个物理槽位每 `STAGES`（=2）条指令复用一次。`index / STAGES` 算出「这是第几轮复用」，`& 1` 取其奇偶作为相位位。worker 用当前 index、controller 用「上一轮占用的 index」（即 `index - STAGES`），两者算出的相位正好对应**同一次占用**，于是 wait 与 arrive 的相位一致，握手成立。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「源码阅读型」追踪任务，画出一张完整的「一条指令的共享内存生命周期图」。

**任务**：选定一条普通指令（设 `instruction_index = 2`，复用 `instruction_ring = 0`、`phase = 1`），在一张图上同时标注：

1. **数据层**：在这条指令的执行窗口内，`instruction_state_t` 的哪些字段被谁读写——controller 写 `instructions` / `pid_order` / `semaphores`（[controller.cuh:67-99](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L67-L99)），worker 读 `instructions`（经 `instruction()`）、读 `pid_order`（经 `pid_order_shared_addr` + `pid()`）、读写 `scratch`。
2. **游标层**：worker 的 `instruction_index` / `instruction_ring` 如何从 (2, 0) 经 `next_instruction()` 推进到 (3, 1)（[util.cuh:128-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L128-L140)）。
3. **同步层**：`instruction_arrived[0]` 与 `instruction_finished[0]` 在 phase 1 这一轮的 arrive/wait 顺序（参考 4.3.4 的六步追踪）。
4. **页映射层**：标出 `await_instruction` 把 `pid_order_shared_addr` 拍成快照的时刻，以及之后 `pid(lid)` 如何用这个快照把 lid 翻成 pid，再交给 [U7·L1] 的 `wait_page_ready` / `finish_page` 管理物理页。

**交付物**（纸笔即可）：一张时间轴图，横轴是「controller 准备 → worker await → worker 执行 → worker next → controller 复用」，纵轴分「数据 / 游标 / arrived / finished / pid_order_shared_addr」五行，把上面 4 个层次的关键事件填进对应格子。完成后，你应当能用一句话回答：**「为什么 worker 在 `await` 之后、`next` 之前可以放心地反复调用 `pid()`？」**（答：因为 `await` 既保证了 controller 已写好 `pid_order`，又把它的 shared 地址缓存成了整个指令窗口内不变的 `pid_order_shared_addr`。）

## 6. 本讲小结

- `instruction_state_t`（[util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19)）是共享内存里「一条在飞指令」的全部状态：`instructions` / `timings` / `pid_order`(+padding) / `semaphores` / `scratch`；VM 开 `INSTRUCTION_PIPELINE_STAGES=2` 份构成 2 槽环形缓冲（[megakernel.cuh:23-24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L23-L24)）。
- `state`（[util.cuh:73-212](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L73-L212)）是每个 warp 的「运行态视图」：几乎全是引用 + 游标。游标 `instruction_index`（绝对号）与 `instruction_ring`（= index % 2）共同定位「当前指令」；所有访问器都隐式经 `instruction_ring` 索引。
- `pid(int lid)`（[util.cuh:150-154](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L150-L154)）通过一次 shared `lds` 把逻辑页 lid 翻译成物理页 pid，基址来自 `await_instruction` 缓存的 `pid_order_shared_addr`——这是把「每页一次地址计算」摊成「每指令一次」的热路径优化。
- `await_instruction()`（[util.cuh:122-127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L122-L127)）等 controller 的 `instruction_arrived` 通知并缓存页表基址；`next_instruction()`（[util.cuh:128-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L128-L140)）由 lane 0 arrive `instruction_finished` 并推进游标。
- 两个 handoff 信号量的阈值是流水自洽的「账本」：`instruction_arrived` 阈值 1（controller arrive 1 次），`instruction_finished` 阈值 `NUM_WARPS-1=19`（19 个 worker 各 arrive 1 次）；二者初始化在 [megakernel.cuh:82-86](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L82-L86)。
- 相位位 `(index / STAGES) & 1` 让同一物理槽每复用一次相位翻转一次，使「生产者 arrive」与「消费者 wait」无需显式重置信号量即可对齐轮次（细节深入的相位翻转机制见 [U7·L2]）。

## 7. 下一步学习建议

本讲把 VM 的「数据布局 + 游标 + 节拍」讲清了，但有几条线是刻意留白的，建议按顺序往下读：

1. **[U5·L4]（MAKE_WORKER 宏与 dispatch_op 派发）**：本讲多次提到 worker 主循环是 `await → dispatch_op → next`，那个循环正是 `MAKE_WORKER` 宏展开的结果。下一讲会把这个宏逐行展开，并讲清 `dispatch_op` 如何按 opcode 把指令派发给具体 op 的子结构。
2. **[U6·L1]（controller 主循环）**：本讲只展示了 controller 的 arrive/wait 片段，完整的「取指 / 页分配 / 信号量构造 / 通知」四步流程是下一单元的主题，能补全「controller 凭什么能 arrive arrived」的前置条件。
3. **[U7·L1]（共享内存 page 生命周期）**：本讲的 `pid()` 只是 lid→pid 的查表；真正管理物理页「何时被生产、何时被消费完、何时可释放」的是 `wait_page_ready` / `finish_page` 与 `page_finished` 相位信号量，那是 page 同步原语的专题。
4. **[U7·L2]（动态信号量与相位位双缓冲）**：本讲对 kittens 信号量只用到了高层语义，相位位「内部如何翻转、invalidate 如何回收槽位」等细节，留到这一讲系统讲解。

读完以上四讲，你就能从「一条指令在共享内存里的样子」一路打通到「整个 VM 如何无锁自洽地跑完一个 SM 队列」。
