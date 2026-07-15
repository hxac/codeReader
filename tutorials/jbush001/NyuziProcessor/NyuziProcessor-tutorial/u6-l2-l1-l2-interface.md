# L1-L2 接口与队列

## 1. 本讲目标

上一讲（u6-l1）我们看清了 L1 数据缓存如何在两拍内判命中、检测缺失。本讲要回答紧接着的下一个问题：**L1 缺失之后，请求去了哪里？数据从哪里回来？回来之后怎么把缓存填上、把被挂起的线程叫醒？**

学完本讲你应该能够：

- 说清一次 L1 缺失如何进入 `l1_load_miss_queue`、经 `l1_l2_interface` 发往 L2，并在 L2 响应回来后更新 L1 标签/数据、唤醒线程。
- 解释 store 队列的缓冲、写合并（write combine）与 load-after-store 旁路（bypass）机制。
- 描述 L2 响应的三级处理流水线，以及为什么「先更标签、后更数据」能避免竞争。
- 理解「每个硬件线程一条 miss 表项、一条 store 表项」的设计如何让 L2 响应靠 `id` 就能精确路由回正确的线程。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **缓存行即向量宽度**：`CACHE_LINE_BYTES = NUM_VECTOR_LANES * 4 = 64` 字节，一行恰好装下一个向量寄存器（u1-l1、u2-l1）。
- **L1D 的 VI/PT 结构**：L1 数据缓存是「虚拟索引 / 物理标签」，缺失时拿到的是物理地址（u6-l1）。
- **多线程隐藏延迟**：每核 4 个硬件线程，一个线程因缺失挂起时，仲裁器会切到其他线程继续发射，流水线不空转（u4-l3）。
- **写回/唤醒位图**：线程被挂起时进入 `thread_blocked` 位图，缺失完成后由唤醒位图清掉对应位（u4-l3）。

需要先认识的几个类型（来自 [defines.svh:347-392](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L347-L392)）：

| 类型 | 含义 |
|------|------|
| `cache_type_t` | `CT_ICACHE` / `CT_DCACHE`，标记请求/响应属于哪个一级缓存 |
| `l1_miss_entry_idx_t` | `$clog2(THREADS_PER_CORE)` 位，实质就是**线程号** |
| `l2req_packet_t` | L2 请求包：core、id、packet_type、cache_type、address、store_mask、data |
| `l2rsp_packet_t` | L2 响应包：status、core、id、packet_type、cache_type、address、data |

请求包类型有 7 种（`L2REQ_LOAD / LOAD_SYNC / STORE / STORE_SYNC / FLUSH / IINVALIDATE / DINVALIDATE`），响应包有 5 种（`L2RSP_LOAD_ACK / STORE_ACK / FLUSH_ACK / IINVALIDATE_ACK / DINVALIDATE_ACK`），见 [defines.svh:354-381](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L354-L381)。

## 3. 本讲源码地图

本讲涉及三个核心文件，全部位于 `hardware/core/`：

| 文件 | 作用 |
|------|------|
| [l1_load_miss_queue.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv) | 跟踪挂起的 L1 取数/取指缺失，合并对同一地址的多次缺失，响应到来时唤醒等待线程 |
| [l1_store_queue.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv) | 缓冲 store 请求，支持写合并与 load 旁路，并承载 flush/invalidate/membar/sync 等缓存控制操作 |
| [l1_l2_interface.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv) | 一级缓存与 L2 之间的「协议适配器」：实例化两个 miss 队列和一个 store 队列，仲裁三类请求发往 L2，用三级流水处理 L2 响应 |

它们的连接关系是：

```
        dcache_data_stage ──(dd_cache_miss/ dd_store_*)──┐
        ifetch_data_stage ──(ifd_cache_miss)──────────┐  │
                                                    ▼  ▼
                              l1_load_miss_queue  l1_store_queue
                              (dcache + icache)        │
                                    │                  │
                                    └──── l1_l2_interface ──── l2i_request ──► L2
                                          ▲                      │
                                          │                  l2_response
                                          └──────────────────────┘
                                  （三级响应流水线 + 唤醒位图）
```

模块顶部的注释把职责概括得很清楚（[l1_l2_interface.sv:21-43](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L21-L43)）：它把 L2 的协议细节对执行流水线隐藏起来，方便试验不同的协议与互连。

---

## 4. 核心概念与源码讲解

### 4.1 缺失队列 l1_load_miss_queue

#### 4.1.1 概念说明

L1 缺失一次，要等几十甚至上百个周期才能从 L2/内存把整行（64 字节）取回来。这段时间里：

1. 发起缺失的那个线程必须被挂起，否则它会反复重发同一条 load。
2. 但其它线程应当继续跑——这正是多线程 GPU 隐藏内存延迟的方式。
3. 如果**多个线程几乎同时缺失了同一缓存行**（GPU 里相邻线程访问相邻数据时极常见），不应该发多次一模一样的 L2 请求，而应合并成一次请求，等数据回来后**一起唤醒**。

`l1_load_miss_queue` 就是做这三件事的小模块。它的核心数据结构是「每个硬件线程一条表项」。

#### 4.1.2 核心流程

一次缺失从入队到唤醒的生命周期如下：

```
dcache 报 miss ──► 入队：占用「发起线程号」那条表项，记录地址 + waiting_threads={自己}
                        │
            （若已有同地址表项且都非 sync）──► 合并：把本线程位 OR 进 waiting_threads
                        │
                   rr_arbiter 轮询选一条「已入队且未发送」的表项
                        │
                   dequeue_ack 时置 request_sent，组装 L2REQ_LOAD 发往 L2
                        │
                        └──── 等待 ────┐
                                        ▼
              L2RSP_LOAD_ACK 回来，带 id（=线程号）
                                        │
              清掉该表项 valid，并把 waiting_threads 作为 wake_bitmap 输出
                                        │
              wake_bitmap ──► thread_select 清掉 thread_blocked ──► 线程重新可调度
```

关键设计点：**表项索引就是线程号**。因为每个线程最多只有一条未完成的 load 缺失，所以用 `THREADS_PER_CORE` 条表项、以线程号为下标就足够；L2 响应包里的 `id` 字段（`l1_miss_entry_idx_t`）直接当线程号用，无需额外查表（[defines.svh:352](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L352)）。

#### 4.1.3 源码精读

**表项结构**（[l1_load_miss_queue.sv:48-54](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L48-L54)）：

```systemverilog
struct packed {
    logic valid;
    logic request_sent;            // 这条缺失是否已发往 L2
    local_thread_bitmap_t waiting_threads;  // 在等这一行的所有线程
    cache_line_index_t address;
    logic sync;
} pending_entries[`THREADS_PER_CORE];
```

`waiting_threads` 是一个位图，这就是「合并」能成立的根基：多个线程缺失同一地址时，它们共享同一条表项，各自的位都被 OR 进这个位图。

**合并判定**（[l1_load_miss_queue.sv:92-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L92-L95)）：当本周期新缺失的地址与某条已有效表项地址相同，且**双方都不是 sync**，就判定为「碰撞」，走合并分支而非新建表项：

```systemverilog
assign collided_miss_oh[wait_entry] = pending_entries[wait_entry].valid
    && pending_entries[wait_entry].address == cache_miss_addr
    && !pending_entries[wait_entry].sync
    && !cache_miss_sync;
```

> 为什么 sync（LL/SC）不能合并？因为同步访存需要「监视这一缓存行是否被他人写过」，每个发起者必须独立向 L2 登记自己的监视请求，合并会破坏原子语义。详见 u10-l1。

合并动作只是把发起线程的位 OR 进去（[l1_load_miss_queue.sv:141-145](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L141-L145)）：

```systemverilog
if (cache_miss && collided_miss_oh[wait_entry])
    pending_entries[wait_entry].waiting_threads <=
        pending_entries[wait_entry].waiting_threads | miss_thread_oh;
```

**发送与唤醒**：用 `rr_arbiter` 在「已有效且未发送」的表项里轮询选一条（[l1_load_miss_queue.sv:67-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L67-L71)、[96-78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L78-L78)）。唤醒时直接把对应表项的 `waiting_threads` 位图吐出去（[l1_load_miss_queue.sv:85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L85)）：

```systemverilog
assign wake_bitmap = l2_response_valid
    ? pending_entries[l2_response_idx].waiting_threads
    : local_thread_bitmap_t'(0);
```

注意 `l1_l2_interface` 里实例化了**两个** miss 队列，分别服务 D-cache 和 I-cache（[l1_l2_interface.sv:189-229](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L189-L229)）。I-cache 那个的 `cache_miss_sync` 永远接 `'0`——取指没有同步访存。

#### 4.1.4 代码实践

**实践目标**：跟踪一次 L1D 取数缺失在 miss 队列里的流转。

**操作步骤**（源码阅读型）：

1. 打开 [dcache_data_stage.sv:505-512](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L505-L512)，确认 `dd_cache_miss` 在「load 请求 + 未命中 + TLB 命中 + 非 near_miss + 无 fault」时拉高，并把线程号经 `dd_cache_miss_thread_idx` 传出。
2. 跟到 [l1_l2_interface.sv:189-207](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L189-L207)，看 `dd_cache_miss` 接到 `l1_load_miss_queue_dcache` 的 `cache_miss` 入口。
3. 设想线程 1 缺失地址 A：进入 [l1_load_miss_queue.sv:115-129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L115-L129) 的新建分支，`pending_entries[1].waiting_threads = 0b0010`。
4. 再设想线程 2 紧接着也缺失地址 A：这次命中 [92-95 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L92-L95) 的 `collided_miss_oh[1]`，于是 `waiting_threads` 变成 `0b0110`。
5. 当 L2 的 `L2RSP_LOAD_ACK` 带 `id=1` 回来，看 [85 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L85) 输出 `wake_bitmap = 0b0110`，两个线程被同时唤醒。

**需要观察的现象**：两个线程的缺失只产生一次 L2 请求，却换来一次同时唤醒两位的响应。

**预期结果**：你应当能解释「为什么 `id` 能等于线程号」——因为表项是按线程号分槽的，一次合并后槽位归「最先发起的那个线程」所有。

> 说明：本实践为源码阅读型跟踪。若想看真实命中/缺失计数，可用性能计数器（CR 事件 `DCACHE_MISS` 等），但那属于 u11-l2 的内容，此处不展开。

#### 4.1.5 小练习与答案

**练习 1**：如果两个线程缺失的是**不同**地址，会怎样？

**答案**：`collided_miss_oh` 对两条表项都不成立（地址不同），于是各占一条表项（线程号 1、线程号 2），产生两次独立的 L2 请求，各自独立唤醒。由于每核只有 `THREADS_PER_CORE` 条表项，若所有线程同时缺失不同地址，表项恰好够用——这正是「每线程一条表项」容量设计的依据。

**练习 2**：为什么表项数等于线程数，而不是等于「可能并发的缺失数」？

**答案**：因为一个线程一次只能挂起一条未完成的 load（它一旦缺失就被 `thread_blocked` 挂起，不会再发新 load）。所以并发缺失数的上界就是线程数，每线程一条表项既不浪费也不短缺。

---

### 4.2 store 队列与旁路 l1_store_queue

#### 4.2.1 概念说明

写操作比读更麻烦。如果每次 store 都立刻发给 L2，会因为一次只写 4 字节而浪费整行 64 字节的带宽。`l1_store_queue` 用一个「每线程一条」的缓冲表项解决一连串问题：

- **缓冲**：store 先进缓冲，再择机发往 L2，让流水线不必每次写都等 L2。
- **写合并（write combine）**：连续写到**同一缓存行**的多次 store 在缓冲里拼成一整行，只发一次 L2 请求。
- **load 旁路（load-after-store bypass）**：紧跟在 store 后面对同一地址的 load，可以直接从缓冲里读到刚写的值，不必等数据往返 L2。
- **缓存控制**：flush、I/D invalidate、membar 也走这个队列，因为它们同样需要在 L2 排队确认。

#### 4.2.2 核心流程

store 队列的表项同样是「每线程一条」（[l1_store_queue.sv:75-88](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L75-L88)）。一条 store 的处理逻辑：

```
dd_store_en 拉高（带线程号、地址、数据、字节掩码）
        │
   该线程表项空闲？──是──► 新建：写地址/数据/掩码，置 valid
        │ 否
   同地址且可合并？──是──► 按掩码把新数据并入 data，掩码 OR 合并
        │ 否（缓冲满/地址不同）
        │
   置 rollback：挂起本线程（thread_waiting=1），等当前表项发完得到响应
        │
   rr_arbiter 选一条已有效未发送的表项，组装 L2REQ_STORE（带 store_mask）发往 L2
        │
   L2RSP_STORE_ACK 回来 ──► 清表项 / 唤醒线程
```

旁路逻辑独立于发送：每个周期，队列都根据 `dd_store_bypass_addr` 检查「当前 load 是否命中某条挂起的 store」，若命中就把那条 store 的 `data`/`mask` 经 `sq_store_bypass_*` 送回 dcache（[l1_store_queue.sv:319-335](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L319-L335)）。

#### 4.2.3 源码精读

**写合并判定**（[l1_store_queue.sv:122-132](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L122-L132)）：要求地址相同、当前表项不是缓存控制类、双方都非 sync、且尚未发送，才能合并：

```systemverilog
assign can_write_combine = pending_stores[thread_idx].valid
    && pending_stores[thread_idx].address == dd_store_addr
    && !pending_stores[thread_idx].flush
    && !pending_stores[thread_idx].iinvalidate
    && !pending_stores[thread_idx].dinvalidate
    && !dd_store_sync
    && !pending_stores[thread_idx].request_sent
    && !send_this_cycle
    && !dd_flush_en && !dd_iinvalidate_en && !dd_dinvalidate_en;
```

合并写入时，只更新掩码命中字节、并把新掩码 OR 进去（[l1_store_queue.sv:212-221](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L212-L221)）：

```systemverilog
for (int byte_lane = 0; byte_lane < CACHE_LINE_BYTES; byte_lane++)
    if (dd_store_mask[byte_lane])
        pending_stores[thread_idx].data[byte_lane*8+:8] <= dd_store_data[byte_lane*8+:8];

if (can_write_combine)
    pending_stores[thread_idx].mask <= pending_stores[thread_idx].mask | dd_store_mask;
else
    pending_stores[thread_idx].mask <= dd_store_mask;
```

> 注意 `data` 只在「合并」时按掩码部分更新；新建表项时整行都会被赋（未命中字节为 0，靠 `store_mask` 在 L2 侧区分有效字节）。

**回滚（挂起）时机**（[l1_store_queue.sv:149-171](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L149-L171)）：当缓冲满且无法合并时挂起线程；sync store 第一次发起时**总是**挂起（因为它必须等 L2 响应才能拿到成功/失败结果）。membar 则等到所有挂起 store 完成（注释见 [l1_store_queue.sv:25](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L25)）。

**load 旁路**（[l1_store_queue.sv:319-335](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L319-L335)）：这是让「写后立刻读」不付出 L2 往返代价的关键。`sq_store_bypass_data` 直接把挂起 store 的数据送回，`sq_store_bypass_mask` 告诉 dcache「这些字节来自缓冲、可信」。`sq_store_sync_success` 则把 LL/SC 的成功/失败位（来自 `l2rsp_packet_t.status`）送回写回级。

**sync 响应的特殊处理**（[l1_store_queue.sv:291-301](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L291-L301)）：普通 store 收到 ACK 就清表项；但 sync store 收到 ACK 后**保留表项**（置 `response_received`、记录 `sync_success`），直到线程醒来重发同一指令取走结果才清掉——这对应 dcache_data_stage 里「sync load 第二次重发」的逻辑（[dcache_data_stage.sv:517-540](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L517-L540)）。

#### 4.2.4 代码实践

**实践目标**：理解写合并如何把多次 store 压成一次 L2 请求。

**操作步骤**（源码阅读型 + 思想实验）：

1. 假设线程 0 连续执行 4 条标量 `store_32`，分别写同一缓存行的 4 个不同字（4 字节）。
2. 跟踪第 1 条：`can_write_combine` 为假（表项无效），走新建分支（[245-263 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L245-L263)），`mask` 初值为该字的 4 字节。
3. 跟踪第 2~4 条：表项有效且地址相同、未发送，`can_write_combine` 为真，于是 `data` 按掩码部分更新、`mask` 不断 OR（[218-221 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L218-L221)），`mask` 最终覆盖整行的 16 个字。
4. 直到仲裁器选中这条表项，**一次** `L2REQ_STORE` 带着满掩码发往 L2（[l1_l2_interface.sv:432-457](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L432-L457)）。

**需要观察的现象**：4 次写只产生 1 次 L2 store 请求，且 `store_mask` 指明全行有效。

**预期结果**：你能说清 `can_write_combine` 每一个 `&&` 条件为何必须存在——例如「未发送」(`!request_sent`) 是因为已发给 L2 的行不能再追加字节。

#### 4.2.5 小练习与答案

**练习 1**：为什么 sync store 第一次发起时即使缓冲有空位也要挂起线程？

**答案**：sync store（`store_sync`，即 SC）必须等 L2 返回成功/失败结果，线程才能拿到写进目标寄存器的 0/1。所以它天然是一次「请求—等响应」的往返，第一次发起就挂起、等 `STORE_ACK` 回来再唤醒重发取结果（[l1_store_queue.sv:162-163](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_store_queue.sv#L162-L163)）。详见 u10-l1。

**练习 2**：load 旁路为什么不直接读 L1 数据 SRAM？

**答案**：因为刚 store 的数据可能还停在 store 队列里、尚未写进 L1（甚至尚未发往 L2）。若去读 L1 SRAM 会读到旧值。旁路用 `sq_store_bypass_mask` 标出「哪些字节以缓冲为准」，让 dcache 用缓冲值覆盖 SRAM 值，保证写后读的正确性。

---

### 4.3 响应分发与唤醒 l1_l2_interface 的响应流水线

#### 4.3.1 概念说明

`l1_l2_interface` 是一级缓存与 L2 之间的「翻译官」：对上（执行流水线）它隐藏 L2 协议，对下（L2）它只讲统一的请求/响应包。它做两类工作：

- **请求方向**：把三个来源（D-cache 缺失、I-cache 缺失、store 队列）的请求仲裁成一条 `l2i_request` 发往 L2。
- **响应方向**：把 L2 回来的 `l2_response` 分发到正确的缓存（I/D），更新标签与数据，并向 `thread_select` 发出唤醒位图。

响应方向被刻意做成**三级流水线**，目的是规避标签与数据更新之间的竞争。

#### 4.3.2 核心流程

**请求仲裁**（[l1_l2_interface.sv:398-458](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L398-L458)）：固定优先级 D-cache > I-cache > store 队列，每周期最多发一条（断言 [382 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L382) `$onehot0({..._ack})`）。注释特别说明 `l2i_request_valid` **不**组合依赖于 `l2_ready`，以免与 L2 仲裁器形成组合环路（[l1_l2_interface.sv:41-42](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L41-L42)）。

**响应三级流水线**（注释见 [l1_l2_interface.sv:32-39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L32-L39)）：

```
L2 响应到达
   │
   ├─ 第 1 级：把响应地址的 set 送给 L1D 标签 SRAM 做 snoop（标签读有 1 拍延迟）
   │           同时算 LRU fill 使能（LOAD_ACK 且 core 匹配）
   │
   ├─ 第 2 级：检查 snoop 命中——若该物理地址已在 L1D 某 way，就更新那一 way；
   │           否则填到 LRU 选中的 way。对 load 响应更新 I/D 标签。
   │           判断 ack_for_me（core 号匹配），把响应按类型路由给
   │           dcache/icache/store 三个队列之一。
   │
   └─ 第 3 级：更新 L1D/L1I 数据 SRAM（比标签晚一拍）
```

为什么要「先标签、后数据」？因为执行流水线在一次 load 访问中也是**先查标签、再读数据**（u6-l1 的两拍结构）。若回填时数据先于标签更新，会出现「标签还无效但数据已新」或反之的中间态，被并发访问的 load 错读到。让两者按同一顺序、相差一拍更新，就保证了任何时刻流水线看到的标签与数据是一致的。

**唤醒汇总**（[l1_l2_interface.sv:209](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L209)）：

```systemverilog
assign l2i_dcache_wake_bitmap = dcache_miss_wake_bitmap | sq_wake_bitmap;
```

D-cache 侧的唤醒来自 miss 队列与 store 队列两路，OR 在一起送给 `thread_select`；I-cache 侧唤醒直接来自 icache miss 队列（[228 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L228)）。断言 [253 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L253) 保证这两路不会同一周期同时拉高。

#### 4.3.3 源码精读

**第 2 级：snoop 命中与 fill way 选择**（[l1_l2_interface.sv:271-295](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L271-L295)）。snoop 用一个 generate 块对每 way 比较物理 tag：

```systemverilog
assign snoop_hit_way_oh[way_idx] = dt_snoop_tag[way_idx] == dcache_tag_stage2
    && dt_snoop_valid[way_idx];
```

fill way 的选择体现了两个用途（注释 [285-288 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L285-L288)）：若 snoop 命中，更新已在缓存的那一 way（处理 store 写回和**同义词 synonym**——两个虚拟地址映射到同一物理地址）；否则填到 LRU way。

```systemverilog
if (|snoop_hit_way_oh)
    dupdate_way_idx = snoop_hit_way_idx; // 更新已有行（写更新/同义词）
else
    dupdate_way_idx = dt_fill_lru;       // 填新行
```

**第 2 级：响应路由**（[l1_l2_interface.sv:334-345](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L334-L345)）。同一个响应按 `cache_type` 与 `packet_type` 被分拣到三个队列的 `l2_response_valid` 信号，三者共用 `id` 作下标：

```systemverilog
assign icache_l2_response_valid = ack_for_me && response_stage2.cache_type == CT_ICACHE;
assign dcache_l2_response_valid = ack_for_me
    && response_stage2.packet_type == L2RSP_LOAD_ACK
    && response_stage2.cache_type == CT_DCACHE;
assign storebuf_l2_response_valid = ack_for_me
    && (response_stage2.packet_type == L2RSP_STORE_ACK
     || response_stage2.packet_type == L2RSP_FLUSH_ACK
     || response_stage2.packet_type == L2RSP_IINVALIDATE_ACK
     || response_stage2.packet_type == L2RSP_DINVALIDATE_ACK);
```

注意 `ack_for_me = response_stage2_valid && response_stage2.core == CORE_ID`（[305 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L305)）：L2 的响应是**广播**给所有核的（用于 snoop 一致性），每个核的 `l1_l2_interface` 只对 `core == 自己` 的响应作 ACK 处理，但 snoop 检查对所有响应都做。

**第 3 级：数据更新**（[l1_l2_interface.sv:351-392](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L351-L392)）：`l2i_ddata_update_en` 比 `dcache_update_en`（标签更新）晚一拍寄存输出，正落实了「先标签后数据」。数据最终写进 dcache 的 `l1d_data` SRAM（[dcache_data_stage.sv:486-489](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L486-L489)）。

**唤醒如何真正解阻塞**：`l2i_dcache_wake_bitmap` 进入 `thread_select_stage`，参与 `thread_blocked` 的清零（[thread_select_stage.sv:293-294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L293-L294)）：

```systemverilog
thread_blocked <= (thread_blocked | wb_suspend_thread_oh)
    & ~(l2i_dcache_wake_bitmap | ior_wake_bitmap);
```

即：缺失时由写回级把线程位置进 `thread_blocked`，响应回来时由唤醒位图把该位清掉，线程重新进入轮询调度——闭环完成。

#### 4.3.4 代码实践

**实践目标**：把「L2 响应 → 更新 L1 → 唤醒线程」这条链路在源码里走通。

**操作步骤**（源码阅读型）：

1. 设想 L2 回来一个 `L2RSP_LOAD_ACK`，`cache_type=CT_DCACHE`，`core=CORE_ID`，`id=1`，地址为物理行 A。
2. 第 1 级：[l1_l2_interface.sv:237-241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L237-L241) 拉起 `l2i_snoop_en` 与 `l2i_dcache_lru_fill_en`，把 set 号送给 L1D 标签 SRAM 做 snoop。
3. 第 2 级：[311-317 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L311-L317) 因 `L2RSP_LOAD_ACK + CT_DCACHE + ack_for_me` 置 `dcache_update_en`，更新标签（有效、填 tag）；[335-336 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L335-L336) 置 `dcache_l2_response_valid` 通知 dcache miss 队列。
4. 第 3 级：[386 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L386) `l2i_ddata_update_en` 晚一拍拉高，数据写入 `l1d_data`。
5. miss 队列收到 `dcache_l2_response_valid` + `id=1`，输出 `wake_bitmap`（[l1_load_miss_queue.sv:85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L85)），经 [209 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L209) 汇成 `l2i_dcache_wake_bitmap`，最终在 [thread_select_stage.sv:293-294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L293-L294) 清掉 `thread_blocked[1]`。

**需要观察的现象**：标签在第 2 级更新、数据在第 3 级更新，恰好差一拍；唤醒位图与标签/数据更新在同一些周期里产生。

**预期结果**：你能画出「响应到达 → 第 2 级更标签+发唤醒 → 第 3 级更数据」的时序，并解释线程在被唤醒重发 load 时，标签和数据都已就绪。

#### 4.3.5 小练习与答案

**练习 1**：L2 响应是广播给所有核的，单核配置下 `ack_for_me` 还有意义吗？

**答案**：有意义。snoop 检查对所有响应都做（用于消除同义词 synonym、以及在多核下作一致性监听），但只有 `core == CORE_ID` 的响应才被认为是「给我的 ACK」，才会去清 miss/store 队列表项、唤醒线程。单核下所有响应的 core 都等于自己，`ack_for_me` 恒真，但逻辑分支不变。

**练习 2**：如果把第 3 级（数据更新）删掉、改成和标签同周期更新，会出什么问题？

**答案**：会出现标签—数据竞争。执行流水线的 load 是「先查标签、次拍读数据」。若回填时标签与数据同拍更新，则在更新那一拍，一个并发的 load 可能看到「新标签、旧数据」或反之，错读值。错开一拍让两者顺序一致，才保证一致性（这正是模块注释 [32-39 行](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L32-L39)强调的点）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次**端到端的 L1D 取数缺失追踪**。这是一个源码阅读型综合任务。

**场景**：线程 0 执行一条 `load_32`，目标地址对应的物理缓存行不在 L1D。

**任务**：按下面 8 个检查点，在源码中找到对应的行，填出每一步的关键信号与文件位置。

| # | 阶段 | 你要定位的代码 | 关键信号 / 现象 |
|---|------|----------------|-----------------|
| 1 | 缺失检测 | [dcache_data_stage.sv:505-512](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L505-L512) | `dd_cache_miss` 拉高，带线程号 0 |
| 2 | 挂起线程 | 写回级 → `thread_select_stage.sv:293` | 线程 0 进入 `thread_blocked` |
| 3 | 入 miss 队列 | [l1_load_miss_queue.sv:115-129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L115-L129) | `pending_entries[0]` 置 valid，`waiting_threads=0b0001` |
| 4 | 仲裁发送 | [l1_l2_interface.sv:410-420](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L410-L420) | 组装 `L2REQ_LOAD`，`id=0`，发往 L2 |
| 5 | 响应到达 | [l1_l2_interface.sv:237-244](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L237-L244) | `L2RSP_LOAD_ACK`，第 1 级 snoop |
| 6 | 更标签 + 路由 | [l1_l2_interface.sv:311-336](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L311-L336) | 第 2 级：更 L1D 标签，置 `dcache_l2_response_valid` |
| 7 | 更数据 | [l1_l2_interface.sv:386](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_l2_interface.sv#L386) | 第 3 级：`l2i_ddata_update_en` 晚一拍写 `l1d_data` |
| 8 | 唤醒 | [l1_load_miss_queue.sv:85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l1_load_miss_queue.sv#L85) → [thread_select_stage.sv:294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/thread_select_stage.sv#L294) | `wake_bitmap=0b0001` 清 `thread_blocked[0]` |

**进阶变体**：把场景改成「线程 0 和线程 1 缺失同一行」，重走第 3、4、8 步，验证合并后只发一次请求、一次唤醒两位（`waiting_threads=0b0011`）。

**验收标准**：你能不看答案，用自己的话讲清「为什么 `id` 等于线程号」「为什么标签比数据早一拍更新」「为什么两个线程缺失同一行只发一次请求」这三件事。

## 6. 本讲小结

- `l1_l2_interface` 是一级缓存与 L2 之间的协议适配层，对上隐藏 L2 协议，实例化了两个 `l1_load_miss_queue`（D/I）和一个 `l1_store_queue`。
- **miss 队列**用「每线程一条表项 + `waiting_threads` 位图」实现缺失合并：多线程缺失同一地址只发一次 L2 请求，响应到来时一次唤醒所有等待线程；sync 访存不参与合并。
- **store 队列**缓冲写操作，支持同地址写合并、load-after-store 旁路，并承载 flush/invalidate/membar/sync 等缓存控制；sync store 靠「保留表项等线程取结果」实现两段式语义。
- **响应三级流水线**：第 1 级 snoop 标签、第 2 级判命中/选 way/更标签/路由响应、第 3 级更数据；「先标签后数据」差一拍，规避与并发 load 的竞争。
- 响应靠 `core == CORE_ID` 判断是否属于本核，靠 `id`(=线程号) 路由回正确的 miss/store 表项。
- 唤醒位图 `l2i_dcache_wake_bitmap = dcache_miss_wake_bitmap | sq_wake_bitmap` 送入 `thread_select`，清掉 `thread_blocked`，让被挂起的线程重新可调度，完成「缺失—挂起—回填—唤醒」闭环。

## 7. 下一步学习建议

- **向下看 L2**：本讲的 `l2i_request`/`l2_response` 是 L2 的输入输出。下一讲 **u6-l3（L2 缓存四阶段流水线）** 会讲解 L2 如何仲裁多核请求、判定命中、缺失填充与脏行写回，与本讲无缝衔接。
- **横向看一致性**：本讲提到的 snoop、`L2RSP_*INVALIDATE_ACK`、同义词处理，在多核场景下才是主角，可结合 **u10-l3（多核与 L2 仲裁）** 一起读。
- **回看同步语义**：store 队列对 sync 的两段式处理、load 的「第一次当缺失」逻辑，是理解 **u10-l1（LL/SC 与 membar）** 的硬件基础，建议两讲对照阅读。
- **延伸到性能观测**：想实际数出缺失次数，可阅读 **u11-l2（性能计数器与 profiling）**，用 CR 性能事件选择寄存器统计 `DCACHE_MISS` 等事件。
