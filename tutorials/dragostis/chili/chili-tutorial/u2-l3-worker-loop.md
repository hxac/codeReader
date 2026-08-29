# worker 线程与共享上下文

> 隶属单元：u2（进阶：公共 API 与执行模型）· 本讲编号 u2-l3
> 前置讲义：u2-l2（ThreadPool 生命周期与 Config 配置）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Context` / `LockContext` 里每个字段的作用，以及为什么 chili 敢把所有共享状态塞进**一把**互斥锁里。
2. 逐行读懂 `execute_worker` 的主循环：worker 如何取任务、如何执行任务、如何在无事可做时挂起、如何在线程池销毁时退出。
3. 理解 `Mutex + Condvar` 的「等待—通知」模型在本讲涉及的三处使用：worker 等 `job_is_ready`、心跳线程等 `scope_created_from_thread_pool`、`Drop` 停机时的唤醒顺序。
4. 理解 `pop_earliest_shared_job` 与 `BTreeMap` 的真实排序语义（这里有一个和直觉不太一样的源码阅读发现）。
5. 理解 `first_run` + `Barrier` 如何保证「线程池构造函数返回时所有 worker 已就绪」。
6. 通过给 `execute_worker` 加临时打印，亲眼看到任务被多个线程取走。

## 2. 前置知识

### 2.1 互斥锁（Mutex）与临界区

多个线程同时读写同一份数据会产生数据竞争（data race），这是未定义行为。`Mutex<T>` 保证同一时刻只有一个线程能拿到里面 `T` 的访问权：

- `lock()` 获取锁，返回一个守护者（guard）；守护者存活期间持锁，离开作用域被丢弃时自动放锁。
- 如果某个线程持锁时 panic 了，这把锁就进入「毒化」（poisoned）状态，之后其他线程 `lock()` 会返回 `Err`（`PoisonError`）。本讲会看到 chili 对毒化的处理方式。

### 2.2 条件变量（Condvar）：让线程「等到条件成立」

只靠互斥锁，一个没有任务的线程只能忙等（自旋空转烧 CPU）。`Condvar` 配合 `Mutex` 提供了「睡觉等人叫」的机制：

- `condvar.wait(guard)`：**原子地**放掉锁并挂起当前线程；被唤醒后重新拿锁，再把守护者还给你。「放锁 + 睡觉」必须是原子的，否则可能出现「刚放锁、还没睡着，通知就在这一瞬间发生了」的丢失唤醒。
- `notify_one()` / `notify_all()`：唤醒一个 / 所有正在这个条件变量上睡觉的线程。
- 被唤醒后**必须重新检查条件**。条件变量存在「虚假唤醒」（没人 notify 也可能醒），而且唤醒只表示「可能有机会了」，不表示「条件一定成立」。这就是经典的「while 循环里 wait」写法；本讲会看到 chili 用「持锁检查 `is_stopping`」实现同样的目的。

### 2.3 Barrier（屏障）

`Barrier::new(n)` 创建一个需要 `n` 方到齐的汇合点：每个线程调用 `barrier.wait()` 后阻塞，直到第 `n` 方到达，然后**所有参与者同时放行**。适合「大家一起初始化，都好了再继续」的场景。

### 2.4 BTreeMap：有序映射

`BTreeMap<K, V>` 按键 `K` 的排序维护条目，因此 `pop_first()`（弹出最小的键）和 `entry(key)`（按键插入或查找）都是 \( O(\log n) \)。本讲的 `shared_jobs` 就是一个 `BTreeMap`，它的键是 `usize`。

### 2.5 承接上一讲

u2-l2 已经讲过 `ThreadPool::with_config` 的启动时序（先 spawn W 个 worker、`Barrier(W+1)` 等全员就绪、最后 spawn 心跳线程）和 `Drop` 的三步停机。本讲把镜头对准**被 spawn 出来的 worker 线程本身**，以及它与其他线程共享的那本「调度账本」。忘了启动时序的话，建议先回看 u2-l2 的第 4.2 节。

另外 u2-l1 讲过：任务真正跨线程分享只发生在 `Scope::heartbeat()` 里——把本地队列队头的任务放进共享队列并 `notify_one` 唤醒 worker。本讲的 worker 主循环就是那个「被唤醒的一方」。

## 3. 本讲源码地图

本讲几乎全部内容都在 `src/lib.rs`；`src/job.rs` 只需要看两个类型的对外接口（内部机制留到 u3-l2 / u3-l3）。

| 位置 | 内容 | 本讲视角 |
| --- | --- | --- |
| [src/lib.rs:73-80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L73-L80) | `LockContext` 结构 | 被锁保护的共享状态（重点） |
| [src/lib.rs:82-103](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L82-L103) | `new_heartbeat` / `pop_earliest_shared_job` | 账本的两个核心操作 |
| [src/lib.rs:105-110](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L105-L110) | `Context` 结构 | 一把锁 + 两个条件变量 |
| [src/lib.rs:112-145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L112-L145) | `execute_worker` | 本讲主角：worker 主循环 |
| [src/lib.rs:147-189](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L147-L189) | `execute_heartbeat` | 只看它与 `scope_created_from_thread_pool` 的关系，细节在 u3-l1 |
| [src/lib.rs:191-215](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L191-L215) | `ThreadJobQueue` | worker 的任务队列形态 |
| [src/lib.rs:269-282](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L269-L282) | `new_from_worker` / `heartbeat_id` | worker 的 `Scope` 怎么来的、scope 编号是什么 |
| [src/lib.rs:284-333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L333) | `wait_for_sent_job` / `heartbeat` | 共享队列的「第二个消费者」与「唯一生产者」 |
| [src/lib.rs:513-546](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L546) | `with_config` | Barrier 在哪里、为什么是 W+1 |
| [src/lib.rs:615-633](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L633) | `Drop` | 停机时的 `notify_all` 与 join 顺序 |
| [src/lib.rs:643-832](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L643-L832) | `tests` 模块 | 本讲实践用到的测试 |
| [src/job.rs:207-225](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L207-L225) | `JobShared::execute` | worker 拿到任务后调用的东西 |
| [src/job.rs:236-275](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L236-L275) | `JobQueue` | `pop_front` 出队时顺带创建结果通道 |

## 4. 核心概念与源码讲解

### 4.1 Context 与 LockContext 结构：一本被锁保护的调度账本

#### 4.1.1 概念说明

线程池里有多种角色的线程需要协作：

- **调用线程**：执行用户代码，持有 `Scope`，递归调用 `join`，偶尔把任务放进共享队列；
- **worker 线程**：常驻后台，等着从共享队列取任务执行；
- **心跳线程**：专职「敲铃」，周期性把各 scope 的心跳标志置位。

它们需要一个地方交换信息：任务货架（谁分享了任务）、心跳登记表（哪些 scope 还活着）、停机旗（池要销毁了）。chili 的做法是把这一切装进一个 `LockContext`，再用一把 `Mutex` 包起来，配上两个 `Condvar`，合成一个 `Context`，用 `Arc` 克隆给每个线程。

为什么敢用**一把粗粒度锁**？因为这整条共享路径在 chili 里是**冷路径**：心跳默认每 100µs 才可能触发一次任务分享，而且每个 scope 同时至多挂一个任务（后面会看到 `entry` 去重保证这一点）。真正的热路径——`join_seq` 顺序执行、本地 `JobQueue` 的进出队——完全不碰这把锁。冷路径用一把锁换取极简的正确性推理，这是 chili「低开销」设计哲学的一部分：**开销不在锁上，因为锁根本不在快路上**。

#### 4.1.2 核心流程

`Context` 的整体结构可以用下面这张图描述（一个 `ThreadPool` 恰好一个 `Context`，被 `Arc` 共享给所有线程）：

```text
Context（每池一个，Arc 共享）
├── lock: Mutex<LockContext>            ← 一把锁保护以下账本
│   ├── time: u64                        投递时间戳计数器（单调递增）
│   ├── is_stopping: bool                停机标志（Drop 时置位）
│   ├── shared_jobs: BTreeMap<scope_id, (投递时间, JobShared)>
│   │                                    ↑ 跨线程「任务货架」
│   ├── heartbeats: HashMap<登记号, Heartbeat>  心跳登记表
│   └── heartbeat_index: u64             心跳登记号的发号器
├── job_is_ready: Condvar                worker 在此睡觉，等「有活干 / 该停了」
└── scope_created_from_thread_pool: Condvar  心跳线程在此睡觉，等「出现了用户 scope / 该停了」
```

围绕这本账本有三种典型交互：

1. **投递**（调用线程的 `heartbeat()`）：加锁 → 若自己名下没有挂账（`Entry::Vacant`）→ 从本地队列弹出任务放进 `shared_jobs` → `time` 加一 → `job_is_ready.notify_one()` 敲铃 → 放锁。
2. **消费**（worker 或等待中的 scope）：加锁 → `pop_first()` 取走一条 → 放锁 → **锁外**执行任务。
3. **停机**（主线程的 `Drop`）：加锁 → 置 `is_stopping` → 放锁 → `notify_all` 敲铃 → 逐个 join 线程。

#### 4.1.3 源码精读

先看账本本身：

```rust
#[derive(Debug, Default)]
struct LockContext {
    time: u64,
    is_stopping: bool,
    shared_jobs: BTreeMap<usize, (u64, JobShared)>,
    heartbeats: HashMap<u64, Heartbeat>,
    heartbeat_index: u64,
}
```

这是 [src/lib.rs:73-80](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L73-L80)。逐字段说明：

- `time: u64`——全局单调递增的「投递时间戳」。每次有任务进 `shared_jobs` 就取当前值、然后加一（见 4.3.3 的 `heartbeat()`）。
- `is_stopping: bool`——停机标志。`Drop` 置位，worker 醒来后看到它就退出循环。
- `shared_jobs: BTreeMap<usize, (u64, JobShared)>`——**键**是 scope 的编号（`heartbeat_id()` 的返回值），**值**是 `(投递时间, 可跨线程执行的任务)`。这就是「任务货架」。
- `heartbeats: HashMap<u64, Heartbeat>`——所有已登记的心跳。`Heartbeat` 内部持有一个 `Weak<AtomicBool>`（弱引用），scope 销毁后弱引用自动失效，心跳线程借此清理死 scope（细节在 u3-l1）。
- `heartbeat_index: u64`——心跳登记表的发号器，每次登记加一，用 `checked_add` 保证溢出时 panic 而不是回绕。

再看两个核心操作。第一，登记心跳：

```rust
pub fn new_heartbeat(&mut self) -> Arc<AtomicBool> {
    let is_set = Arc::new(AtomicBool::new(true));
    let heartbeat = Heartbeat {
        is_set: Arc::downgrade(&is_set),
        last_heartbeat: Instant::now(),
    };
    let i = self.heartbeat_index;
    self.heartbeats.insert(i, heartbeat);
    self.heartbeat_index = i.checked_add(1).unwrap();
    is_set
}
```

这是 [src/lib.rs:83-96](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L83-L96)。注意两个细节：

1. **新心跳的初值是 `true`**（`AtomicBool::new(true)`）。也就是说，一个刚诞生的 scope 第一次走到心跳路径的 `join` 时，`heartbeat` 标志已经是置位状态，会**立即**尝试分享一次任务——新 scope 不用干等第一个 100µs。
2. 登记表里存的是 `Arc::downgrade` 得到的 `Weak`，而返回给调用者的是强引用 `Arc<AtomicBool>`。scope 持有强引用，scope 死了强引用消失，`Weak` 升级失败，登记项就能被清理——「scope 的生死」天然成为「心跳的生死」。

第二，取任务：

```rust
pub fn pop_earliest_shared_job(&mut self) -> Option<JobShared> {
    self.shared_jobs
        .pop_first()
        .map(|(_, (_, shared_job))| shared_job)
}
```

这是 [src/lib.rs:98-102](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L98-L102)。**这里有一个值得停下来咀嚼的源码阅读发现**：

- 函数名叫 `pop_earliest`（取最早的），很容易让人以为排序依据是值里那个 `(u64, …)` 投递时间。但 `BTreeMap::pop_first()` 弹出的是**键最小**的条目，而这个键是 scope 编号——它来自 `Arc::as_ptr(&self.heartbeat) as usize`，即心跳 `AtomicBool` 在堆上的**指针地址**（见 [src/lib.rs:280-282](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L280-L282)）。指针地址与投递先后毫无关系。
- 所以真实的取货语义是：「按 scope 编号（实际是地址大小）排序，取编号最小的 scope 挂的那个任务」。由于每个 scope 同时至多挂一个任务（`heartbeat()` 里的 `Entry::Vacant` 去重，见 4.3.3），这个排序相当于「在所有有待领任务的 scope 中按地址顺序挑一个」——是一种**确定性的、去重友好的**取货规则，但**不是**严格按投递时间的 FIFO。
- 值里的投递时间 `u64` 在当前 HEAD 中**只被写入、从未被读取**——`pop_first` 的结果把它原样丢弃（`|(_, (_, shared_job))|`）。往前追溯一次重构（提交 617cbd4「Refactored and simplified jobs」）之前的代码，它同样是只写不读，很可能是早期按时间排序设计的遗留字段。读源码时遇到这种「写了没人读」的字段，先别怀疑自己看漏了，用搜索工具确认全仓库确实没有读方——这是一个很典型的「设计演进残迹」。

为什么用 `BTreeMap` 这个数据结构？两个理由：

1. `entry(key)` API 天然实现「每个 scope 至多挂一个任务」的去重（`Entry::Vacant` 分支才插入）；
2. 键有序带来 `pop_first()` 的确定性出队（不管入队顺序如何，出队顺序只由键决定），同时 `wait_for_sent_job` 还能按键 `remove` 撤回自己名下的任务（[src/lib.rs:284-296](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L296)），这些都是 \( O(\log n) \)。

最后看外壳 `Context`：

```rust
struct Context {
    lock: Mutex<LockContext>,
    job_is_ready: Condvar,
    scope_created_from_thread_pool: Condvar,
}
```

这是 [src/lib.rs:105-110](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L105-L110)。一把锁配两个条件变量——**同一把锁可以配多个条件变量**，这样「等任务」和「等 scope 出现」两类睡眠者互不干扰：`job_is_ready.notify_one()` 只叫醒等任务的 worker，不会无谓吵醒心跳线程。两个条件变量的完整分工表见 4.3.2。

#### 4.1.4 代码实践：数一数心跳登记表里都有谁

**实践目标**：亲眼验证「登记表里的心跳数 = worker 数 + 活跃 scope 数」这一账本模型。

**操作步骤**：

1. 在你的本地克隆中打开 `src/lib.rs`，在 `new_heartbeat`（[src/lib.rs:83-96](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L83-L96)）的 `self.heartbeats.insert(i, heartbeat);` 一行之前，临时加一句打印：

   ```rust
   // 示例代码：临时插桩，观察完删除
   eprintln!("[登记] 心跳 #{i} 入表，当前共 {} 个", self.heartbeats.len() + 1);
   ```

2. 运行专门考察多 scope 的测试：

   ```bash
   cargo test concurrent_scopes -- --nocapture --test-threads=1
   ```

   （`concurrent_scopes` 在 [src/lib.rs:807-832](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L807-L832)：`thread_count: 4` 的池——按 u2-l2 讲过的语义，这是 4 个总计算线程、即 3 个 worker——128 个线程各自创建一个短命 scope 并做一次 `join`。）

3. 观察完还原：`git restore src/lib.rs`，再跑一次 `cargo test --lib` 确认全绿。

**需要观察的现象**：开头 3 条是该池 3 个 worker 的登记（在 barrier 之前就发生）；随后 `concurrent_scopes` 阶段登记数此起彼伏地上涨。

**预期结果**：`concurrent_scopes` 阶段的登记计数在 3（worker）到 3+128=131 之间波动——每个新 scope 登记一条、scope 销毁后被心跳线程的 `retain` 清理。具体数值随调度变化，属正常。**待本地验证**（本讲不预填具体输出）。

#### 4.1.5 小练习与答案

**练习 1**：`heartbeats.len()` 什么时候恰好等于传给 `execute_heartbeat` 的 `num_workers`？

**答案**：当池里只有 W 个 worker 自己的心跳、没有任何活跃的用户 scope 时。这正是心跳线程的睡眠条件——没有用户 scope 就没有可分享的任务，心跳线程不必空转（见 [src/lib.rs:153-159](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L153-L159) 的谓词，细节在 u3-l1）。

**练习 2**：如果把 `shared_jobs` 从 `BTreeMap<usize, (u64, JobShared)>` 换成 `Vec<(usize, (u64, JobShared))>`，哪些操作会变慢或变麻烦？

**答案**：(a) `heartbeat()` 里的去重插入——`BTreeMap::entry` 一次 \( O(\log n) \) 完成「查有没有 + 没有 就插」，`Vec` 要先线性扫一遍再 push；(b) `wait_for_sent_job` 里按 scope 编号 `remove` 撤回任务（[src/lib.rs:284-296](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L296)）——`Vec` 需要线性查找加搬移元素，而搬移 `JobShared` 这种含裸指针的类型还要小心；(c) `pop_first` 的确定性出队——`Vec` 弹 `first()` 按的是插入顺序而非键序（不过如前所述，键序本来也不等于时间序）。

**练习 3**：`new_heartbeat` 把新心跳初值设为 `true` 而不是 `false`，对刚创建的 scope 意味着什么？

**答案**：意味着 scope 的第一次心跳路径 `join` 不必等待心跳线程的第一个 100µs 周期，立刻就有机会把任务分享出去。这缩短了「scope 诞生到第一次可能并行」的延迟。

### 4.2 worker 主循环：execute_worker 逐行精读

#### 4.2.1 概念说明

worker 是线程池里的「常驻打工人」。它的一生非常简单，可以概括成一个死循环：

> **取一个共享任务 → 执行它 → 回到 `job_is_ready` 上睡觉等下一个 → 被叫醒或被告知停机 → 重复**。

两个关键认知：

1. **worker 不生产任务，只消费**。生产者是调用线程的 `Scope::heartbeat()`。worker 自己也可能在生产——当它执行一个共享任务时，任务闭包里的递归 `join` 会在这个 worker 自己的 `Scope` 上运行，从而也可能触发新的分享。也就是说，worker 执行任务时「客串」了一把调用线程的角色。
2. **worker 与它的 `Scope` 同生共死**。每个 worker 在线程开始时创建一个自己的 `Scope`（和属于该 worker 的本地 `JobQueue`），贯穿整个线程生命周期反复使用。这与调用线程「每次 `tp.scope()` 新建一个 scope」形成对照。

为什么要这样设计？因为 fork-join 负载里，worker 执行的任务闭包签名是 `FnOnce(&mut Scope) -> T`——闭包需要一个 `Scope` 来继续嵌套 `join`。给每个 worker 配一个常驻 `Scope`，任务在任何线程上执行时都有一致的「继续分叉」环境。

#### 4.2.2 核心流程

`execute_worker`（[src/lib.rs:112-145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L112-L145)）的伪代码：

```text
fn execute_worker(context, barrier):
    first_run ← true
    job_queue ← 空队列（分配在 worker 栈上）
    scope     ← 用 context 和 &mut job_queue 造一个常驻 Scope（顺便登记心跳）

    循环:
        job ← 加锁 { 从共享货架弹出一条 } 放锁     # 锁只覆盖「弹出」这一瞬
        若 job 存在:
            在锁外执行 job（用 worker 自己的 scope）  # 关键：执行不持锁

        若 first_run:                              # 只在第一轮发生
            first_run ← false
            barrier.wait()                          # 与其余 worker 和主线程汇合

        lock ← context.lock 加锁（失败则线程结束）
        若 lock.is_stopping 或 在 job_is_ready 上等待失败:
            跳出循环                                # 停机或锁毒化
```

再把它放进 u2-l2 讲过的启动时序里，看 `Barrier` 那一刻前后发生了什么（W 为 worker 数）：

```text
主线程 with_config                     W 个 worker 线程
──────────────────                    ─────────────────
spawn worker ×W ─────────────────────→ 各自：造 job_queue + Scope（登记心跳）
                                       首轮循环：加锁弹出（此刻货架必为空 → None）
                                       barrier.wait()（睡眠等待汇合）
Barrier(W+1).wait() ──────────────────┼—— 全员到齐，同时放行 ——
spawn 心跳线程
返回 ThreadPool（用户拿到池）           worker：加锁 → is_stopping? 否 → 在 job_is_ready 上睡着
```

一个容易忽略的事实：worker 的**首次弹出发生在 barrier 之前**，但此刻用户还没拿到 `ThreadPool`，不可能创建任何 scope，`shared_jobs` 必然是空的，所以首轮弹出恒为 `None`——这次弹出只是让循环体保持统一形状，不做任何实事。

#### 4.2.3 源码精读

现在逐段读真代码。函数签名与初始化：

```rust
fn execute_worker(context: Arc<Context>, barrier: Arc<Barrier>) -> Option<()> {
    let mut first_run = true;

    let mut job_queue = JobQueue::default();
    let mut scope = Scope::new_from_worker(context.clone(), &mut job_queue);
```

这是 [src/lib.rs:112-116](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L112-L116)。三个要点：

- `job_queue` 是 worker **栈上**的本地队列，整个线程生命周期存在；`Scope::new_from_worker` 拿的是它的可变借用。
- `new_from_worker`（[src/lib.rs:269-278](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L269-L278)）会加锁调用 `new_heartbeat` 登记 worker 自己的心跳——所以 worker 的 `Scope` 与用户 scope 一样参与心跳调度。
- `Scope` 内部用 `ThreadJobQueue` 枚举区分两种队列来源（[src/lib.rs:191-215](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L191-L215)）：worker 的 `Worker(&mut JobQueue)` 借用线程栈上那个常驻队列，用户调用线程的 `Current(JobQueue)` 则把队列直接放在 `Scope` 里。两者通过 `Deref`/`DerefMut` 对外呈现统一的 `JobQueue` 接口，`Scope` 的其余代码完全不感知差异。

循环第一步，取任务：

```rust
    loop {
        let job = {
            let mut lock = context.lock.lock().unwrap();
            lock.pop_earliest_shared_job()
        };
```

这是 [src/lib.rs:118-122](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L118-L122)。注意花括号的用法：守护者 `lock` 的生命周期被限定在这个块里，**块结束锁就放了**。锁只保护「从 `BTreeMap` 弹出一条」这个瞬间动作。

第二步，执行：

```rust
        if let Some(job) = job {
            // SAFETY:
            // Any `Job` that was shared between threads is waited upon before
            // the `JobStack` exits scope.
            unsafe {
                job.execute(&mut scope);
            }
        }
```

这是 [src/lib.rs:124-131](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L124-L131)。**执行在锁外**——这是本函数最重要的性能决策：如果持锁执行，一个耗时任务会把所有其他 worker、所有想投递任务的调用线程、心跳线程统统堵在锁上，并行就退化成串行。SAFETY 注释说的是 unsafe 契约：任务里borrow的 `JobStack`（闭包的家）在发起线程那边要等结果、活得比这次执行久，所以这里的裸指针解引用是安全的（完整论证在 u4-l1）。`job.execute` 消费 `JobShared`，一路调到 job.rs 里的 harness，把闭包取出来、用 `catch_unwind` 包着运行、结果通过一次性通道发回（`JobShared::execute` 在 [src/job.rs:214-225](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L214-L225)；通道本身在出队时由 `JobQueue::pop_front` 创建，见 [src/job.rs:255-274](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/job.rs#L255-L274)——本讲只需知道接口，内部三态状态机在 u3-l2）。

第三步，首轮 barrier：

```rust
        if first_run {
            first_run = false;
            barrier.wait();
        };
```

这是 [src/lib.rs:133-136](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L133-L136)。只发生一次。`Barrier::new(thread_count + 1)` 的 `+1` 是主线程（[src/lib.rs:519](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L519)），它在 [src/lib.rs:537](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L537) 等待。汇合点保证的不变量是：**`with_config` 返回时，W 个 worker 都已完成初始化（队列建好、心跳登记、跑完第一轮空循环）**。这样用户拿到池的那一刻，任何 `join` 分享出的任务都一定有「已就绪的工人」能接——不存在「池已返回但 worker 还在建设中」的窗口。心跳线程在 barrier 之后才 spawn（[src/lib.rs:542-544](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L542-L544)），所以它启动时 `heartbeats.len()` 已经是 W，正好等于传给它的 `num_workers`。

第四步，等待或退出：

```rust
        let lock = context.lock.lock().ok()?;
        if lock.is_stopping || context.job_is_ready.wait(lock).is_err() {
            break;
        }
    }

    Some(())
```

这是 [src/lib.rs:138-145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L138-L145)。三个退出/继续条件：

1. `context.lock.lock().ok()?`——加锁失败说明锁被毒化（某处持锁 panic），`?` 让函数返回 `None`，worker 线程就此结束。
2. `lock.is_stopping`——**先持锁查停机旗，再去睡**。这一步的顺序是杜绝「睡死」的关键，4.3 节展开。
3. `context.job_is_ready.wait(lock).is_err()`——睡觉直到被 notify；醒来后 `wait` 返回的守护者交还锁，条件不满足（毒化）就退出，否则回到循环顶部重新弹任务。

把这些串起来，注意一个结构事实：**一轮循环最多执行一个任务**。worker 被唤醒 → 弹一个 → 执行 → 立刻回去睡。它醒来后并不会先把货架清空再睡。那如果货架里还剩任务、所有 worker 都在睡，谁来推进？答案是两类「兜底人」：持续心跳的活跃 scope 会不断投递并 `notify_one`，以及正在 `wait_for_sent_job` 里等结果的调用线程自己也会去货架取货（见 4.3.3）。完整的推进论证是下一讲（u2-l4）的主菜，这里先按下不表。

#### 4.2.4 代码实践：给 execute_worker 加打印，看任务被谁取走

这是本讲的主实践——通过临时插桩，亲眼看到「投递—领取」的配对过程。

**实践目标**：观察共享任务由哪个线程投递、被哪个 worker 取走，体会调度的非确定性。

**操作步骤**：

1. 在本地克隆的 `src/lib.rs` 里做两处临时修改（都是纯加打印，不碰任何 unsafe 块，不会触发本仓库的 deny lint）。

   修改一：把 `execute_worker` 里取任务的块（[src/lib.rs:119-122](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L119-L122)）替换为下面这样（内联 `pop_first`，顺便拿到 scope 编号和投递时间）：

   ```rust
   // 示例代码：临时插桩，观察完还原
   let job = {
       let mut lock = context.lock.lock().unwrap();
       lock.shared_jobs.pop_first().map(|(scope_id, (time, job))| {
           eprintln!("[领取] worker {:?} 取走 scope {scope_id} 的任务（time {time}）", thread::current().id());
           job
       })
   };
   ```

   修改二：在 `Scope::heartbeat`（[src/lib.rs:318-333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333)）的 `lock.time += 1;` 之后加一行：

   ```rust
   // 示例代码：临时插桩，观察完还原
   eprintln!("[投递] scope {} 在 time {time} 挂出任务", self.heartbeat_id());
   ```

2. 跑跨线程压力最大的测试（每 1µs 一次心跳、每次 `join` 都查心跳、2 线程配置，见 [src/lib.rs:716-747](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L716-L747)）：

   ```bash
   cargo test join_wait -- --nocapture --test-threads=1
   ```

   `--test-threads=1` 避免其他测试的全局池输出混进来；`--nocapture` 让 `eprintln!` 直接上屏。

3. 再跑一次对照（默认 64 次才查一次心跳的池）：

   ```bash
   cargo test join_long -- --nocapture --test-threads=1
   ```

4. 观察完还原并验证：

   ```bash
   git restore src/lib.rs
   cargo test --lib
   ```

**需要观察的现象**：

- `[投递]` 与 `[领取]` 交错出现；`[领取]` 行里的 `worker ThreadId(...)` 与投递线程不同——这就是任务真的跨了线程。
- 同一个 `scope` 编号反复出现（同一个递归 scope 多次分享），但**同一个编号不会同时挂两个任务**（去重保证）。
- 多跑几次，`[领取]` 行的先后顺序、参与者的 ThreadId 都会变——调度是非确定的。
- 对照组 `join_long` 的 `[投递]`/`[领取]` 行数明显更少（降频检查 + 链式切分形状不适合分享，呼应 u2-l1 的结论）。
- 一个微妙现象：偶尔会看到同一个 scope 连续两次 `[投递]` 而中间没有对应的 `[领取]`——那是发起线程在 `wait_for_sent_job` 里把没人领的任务**自己收回了**（那条路径不经过你的打印），这正是下一讲的伏笔。

**预期结果**：测试全部通过（打印不影响语义），日志呈上述交错形态。具体行数、ThreadId 数值依机器与调度而定，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么「弹出」必须持锁，而「执行」绝不持锁？

**答案**：`shared_jobs` 是多线程共享的 `BTreeMap`，弹出（读改写）必须在临界区内，否则数据竞争。执行则可能耗时任意长（闭包是任意用户代码），若持锁执行，其他 worker 取不了任务、调用线程投递不了任务、心跳线程登记不了心跳，整个池退化成单线程串行。所以锁的边界被刻意压缩到「弹出的一瞬」。

**练习 2**：`Barrier::new(thread_count + 1)` 里的 `+1` 是谁？如果去掉 `+1`、也不让主线程 `wait`，会失去什么保证？

**答案**：`+1` 是调用 `with_config` 的主线程（它在线程池侧 [src/lib.rs:537](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L537) 参与汇合）。去掉后，`with_config` 可能在 worker 尚未完成初始化（还没登记心跳、还没进入等待状态）时就返回。此后 spawn 心跳线程时 `heartbeats.len()` 可能仍小于 `num_workers`，心跳线程的睡眠谓词（`heartbeats.len() == num_workers`）会被「未满员」误触发而提前进入轮询，初始化语义就从「汇合点保证」退化成「碰运气」。

**练习 3**：worker 一轮循环执行几个任务？执行完立刻做什么？

**答案**：最多一个。执行完（首轮还要过 barrier）就重新加锁，检查 `is_stopping` 后在 `job_is_ready` 上 `wait` 睡去，直到下一次 notify 把它叫醒（[src/lib.rs:138-141](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L138-L141)）。

### 4.3 Condvar 等待与通知：没有丢失唤醒的关闭协议

#### 4.3.1 概念说明

条件变量问题的本质是：**睡觉的线程和叫醒它的线程必须对「在什么状态下睡、在什么状态下叫」达成一致**，否则会出现两种病：

- **丢失唤醒**：通知发出时目标还没睡下（通知不排队、不留存），目标随后睡下，永远没人再叫它——死等。
- **睡死**：被唤醒后不重新检查条件就继续干活，条件其实已经变了。

标准解法是把「条件相关的状态」放进锁里：**改状态必须持锁，睡之前必须持锁检查条件**。这样，要么先改状态后通知——睡眠者要么还没睡（拿着锁看到了新状态，干脆不睡），要么已睡着（被 notify 叫醒后重新拿锁检查）；两种情况都不会漏。

chili 全库只有两个条件变量、三个睡眠方、三个通知方，规模小到可以完整列成一张表，这正是精读小型并发库的乐趣。

#### 4.3.2 核心流程

| 条件变量 | 谁在上面睡 | 睡眠条件（谓词） | 谁来 notify | 何时 notify |
| --- | --- | --- | --- | --- |
| `job_is_ready` | worker（`execute_worker`，[L139](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L139)） | 持锁看 `is_stopping`；为假则睡等任务 | ① `Scope::heartbeat()`（[L328](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L328)） | 每挂出一个任务 `notify_one`（一个任务只需一个工人） |
| | | | ② `ThreadPool::drop`（[L622](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L622)） | 停机时 `notify_all`（要叫醒所有工人看旗） |
| `scope_created_from_thread_pool` | 心跳线程（`execute_heartbeat` 的 `wait_while`，[L153-L159](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L153-L159)） | `heartbeats.len() == num_workers && !is_stopping`（没有用户 scope 就睡） | ① `Scope::new_from_thread_pool`（[L256-L259](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L256-L259)） | 每次有新用户 scope 诞生登记后 `notify_one` |
| | | | ② `ThreadPool::drop`（[L623](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L623)） | 停机时 `notify_one` |

停机时序（主线程视角）：

```text
Drop:
  1. 加锁 → 置 is_stopping = true → 放锁          # 状态变更在锁内
  2. job_is_ready.notify_all()                    # 叫醒所有睡觉的 worker
  3. scope_created_from_thread_pool.notify_one()  # 叫醒睡觉的心跳线程
  4. 逐个 join worker（等它们真正退出）
  5. join 心跳线程
```

worker 视角的对应面：醒来（或本来就没睡）→ 拿锁 → 看到 `is_stopping` 为真 → `break` 退出循环 → 线程函数返回 → `join` 在主线程那边得以完成。**先拿锁看旗、再决定睡不睡**这一顺序，使得「置旗」和「查看」在同一把锁的仲裁下有序化，从根上杜绝了「worker 刚要睡、旗刚立好、notify 已经过去」的窗口。

#### 4.3.3 源码精读

**生产者一侧：`heartbeat()` 何时敲铃**。

```rust
#[cold]
fn heartbeat(&mut self) {
    let mut lock = self.context.lock.lock().unwrap();

    let time = lock.time;
    if let Entry::Vacant(e) = lock.shared_jobs.entry(self.heartbeat_id()) {
        if let Some(job) = self.job_queue.pop_front() {
            e.insert((time, job));

            lock.time += 1;
            self.context.job_is_ready.notify_one();
        }
    }

    self.heartbeat.store(false, Ordering::Relaxed);
}
```

这是 [src/lib.rs:318-333](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L318-L333)（u2-l1 已逐行讲过它的调用条件，这里只看通知语义）。要点：**先插货、后敲铃**，而且整个「查重 → 出队 → 入账 → 计数 → 敲铃」都在持锁状态下完成。`Entry::Vacant` 保证同一 scope 名下没有旧账时才挂新任务——这就是 4.1.3 反复引用的「每 scope 至多一个在飞任务」不变量。用 `notify_one` 而非 `notify_all`：一个新任务只需要一个工人，全员广播是浪费。`#[cold]` 提示编译器这条路径不常走（呼应 4.1.1 的「冷路径」论断）。

**消费者一侧：worker 的「先查旗再睡」**（[src/lib.rs:138-141](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L138-L141)，前面已读）配合的是 `Drop` 的置旗（[src/lib.rs:615-633](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L633)）：

```rust
impl Drop for ThreadPool {
    fn drop(&mut self) {
        self.context
            .lock
            .lock()
            .expect("locking failed")
            .is_stopping = true;
        self.context.job_is_ready.notify_all();
        self.context.scope_created_from_thread_pool.notify_one();

        for handle in self.worker_handles.drain(..) {
            handle.join().unwrap();
        }

        if let Some(handle) = self.heartbeat_handle.take() {
            handle.join().unwrap();
        }
    }
}
```

（[src/lib.rs:615-633](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L633)，u2-l2 讲过三步序，本讲的视角是它如何与 worker 的等待闭环。）第一条语句在一个表达式里完成「加锁 → 置位 → 语句结束放锁」；随后的两个 notify 都在**锁外**——`Condvar` 允许无锁 notify，可能产生「没人睡的白敲」，但绝不产生丢失唤醒，因为决定 worker 命运的是它自己持锁读到的 `is_stopping`，而不是「有没有收到这声 notify」。换句话说：**notify 负责叫醒，锁内的旗负责判决**，两者职责分离后各自都允许「多余」而不允许「缺失」。join 顺序也有讲究：先 join 完所有 worker、最后 join 心跳线程（[src/lib.rs:625-631](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L625-L631)）——worker 退出可能引发连锁的 scope 销毁与心跳清理，让心跳线程最后谢幕更稳妥。

**同一个货架的第二个消费者：`wait_for_sent_job`**。

```rust
while receiver.is_empty() {
    let job = {
        let mut lock = self.context.lock.lock().unwrap();
        lock.pop_earliest_shared_job()
    };

    if let Some(job) = job {
        // SAFETY: ...
        unsafe {
            job.execute(self);
        }
    } else {
        break;
    }
}

Some(receiver.recv())
```

这是 [src/lib.rs:297-315](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L297-L315)（完整函数含开头，见 [src/lib.rs:284-316](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L316)）。发起线程把自己 fork 出去的任务送走后，就在这个循环里等结果：**不干等**，而是反复从同一个共享货架 `pop_earliest_shared_job` 取别人的任务来执行（帮忙消化），直到自己任务的结果到达（`receiver` 不再 `is_empty`）或货架空了就去 `recv()` 上停车。这段代码是 4.2.3 结尾那个问题（「worker 只弹一个就睡，谁保证推进」）的一半答案：**等待者自己也是消费者**。注意它与 worker 取任务的形状几乎逐字相同——锁内弹、锁外 `unsafe { job.execute(...) }`——两处共享同一套安全契约。另一半答案（被送走任务的结果如何到达 `receiver`）属于 u2-l4。

**心跳线程的睡眠**只看一眼它与 `scope_created_from_thread_pool` 的关系：

```rust
let mut lock = context
    .scope_created_from_thread_pool
    .wait_while(context.lock.lock().ok()?, |l| {
        l.heartbeats.len() == num_workers && !l.is_stopping
    })
    .ok()?;
```

这是 [src/lib.rs:153-159](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L153-L159)。`wait_while` 是「while 循环里 wait」的语法糖：谓词为真就继续睡。谓词的含义是「只有 W 个 worker 心跳、没有用户 scope、也没停机」——没事可做就睡，有 scope 诞生（`new_from_thread_pool` 登记+notify，[src/lib.rs:254-267](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L254-L267)）或停机时被叫醒。它与 worker 等任务的模式同构：谓词状态（`heartbeats.len()`、`is_stopping`）都在锁内变更与检查。心跳线程醒来后干什么（`retain` 清理死心跳、间隔均摊、置位标志）是 u3-l1 的全部内容。

#### 4.3.4 代码实践：观察停机时 notify_all 唤醒所有 worker

**实践目标**：验证 `Drop` 的 `notify_all` 确实把每个睡着的 worker 都叫醒并让它们有序退出。

**操作步骤**：

1. 在本地克隆中做两处临时打印。

   在 `Drop` 的 `notify_all()` 之后（[src/lib.rs:622](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L622) 之后一行）：

   ```rust
   // 示例代码：临时插桩，观察完还原
   eprintln!("[池] 已置停机旗并广播唤醒");
   ```

   在 `execute_worker` 的循环结束后、`Some(())` 之前（[src/lib.rs:142-144](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L142-L144)）：

   ```rust
   // 示例代码：临时插桩，观察完还原
   eprintln!("[退出] worker {:?} 结束", thread::current().id());
   ```

2. 跑最简单的池生命周期测试（[src/lib.rs:643-646](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L643-L646)，创建默认池并在测试结束时 Drop）：

   ```bash
   cargo test thread_pool_stops -- --nocapture --test-threads=1
   ```

3. 换单 worker 配置再跑一次（[src/lib.rs:649-654](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L649-L654)）：

   ```bash
   cargo test thread_pool_with_one_thread -- --nocapture --test-threads=1
   ```

4. 还原：`git restore src/lib.rs`。

**需要观察的现象**：`[池]` 行先出现，随后是若干条 `[退出]` 行（条数 = worker 数，默认池为本机核数减一，单 worker 配置恰好 1 条），`[退出]` 行之间的先后次序可能每次不同。

**预期结果**：两条测试均通过；退出行数量与 worker 数吻合。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：假设把 `Drop` 改成「先 `notify_all`、再（不持锁）写 `is_stopping = true`」，最坏会发生什么？

**答案**：出现丢失唤醒窗口——某 worker 刚拿到锁、查完 `is_stopping`（还是 false）、还没来得及调用 `wait` 睡下；此刻主线程 notify_all（没人在睡，白敲）、然后置旗。worker 随后睡下，再也等不到通知，主线程的 `join` 永远等不回来——死锁。正确顺序是**持锁置旗**（如真实代码 [src/lib.rs:617-621](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L617-L621)）：worker 的「查旗」也在锁内，两者被全序化，要么旗先立好（worker 查到 true 直接退出），要么 notify 落在 worker 已睡之后（被叫醒重查）。

**练习 2**：`heartbeat()` 挂出一个任务用 `notify_one`，`Drop` 用 `notify_all`，为什么不对称？

**答案**：一个新任务只需要一个工人来领，`notify_one` 足矣，全员广播只会制造一场「抢椅子」的空转。停机则不同：目标是让**每一个**睡着的 worker 都醒来看到旗并退出，`notify_one` 只叫一个，剩下的会睡死，`join` 永远完不成。

**练习 3**：`context.job_is_ready.wait(lock).is_err()` 返回 `Err` 意味着什么？worker 的处理合理吗？

**答案**：意味着 `job_is_ready` 关联的互斥锁在某个持锁者 panic 后被毒化，`wait` 拿不回健康的锁。worker 选择 `break` 退出循环、线程结束。这合理：锁毒化说明共享状态可能处于受损的中间态，继续消费任务风险大于收益；而 `Drop` 侧的 `handle.join().unwrap()` 会因线程正常返回而不报错（worker 是以返回 `Some(())` 收场的，panic 的是当初持锁的别人）。注意 worker 对毒化的态度是「宁可少干活也不崩溃」——它把毒化当作停机信号的一种。

## 5. 综合实践

把本讲三个模块串成一个完整的观察实验：**给「投递 → 领取 → 停机」三个环节同时插桩，还原一次跨线程调度的全时序**。

1. 在本地克隆中合并本讲出现过的三处临时打印（都是示例代码，观察完还原）：
   - `Scope::heartbeat`：挂出任务时打印 `[投递]` + scope 编号 + time（4.2.4 修改二）；
   - `execute_worker` 取任务处：内联 `pop_first` 打印 `[领取]` + worker 线程 id + scope 编号 + time（4.2.4 修改一）；
   - `Drop` 与 worker 退出处：打印 `[池]` 与 `[退出]`（4.3.4）。
2. 运行：

   ```bash
   cargo test join_wait -- --nocapture --test-threads=1
   cargo test concurrent_scopes -- --nocapture --test-threads=1
   ```

3. 把 `join_wait` 的日志整理成一张两列表（左边 `[投递]` 按出现顺序，右边对应 `[领取]`），然后回答三个问题：
   - 有几个**不同的** worker 线程参与领取？
   - 同一个 scope 的两次投递之间，是否必然出现一次对该 scope 的领取？若不是，缺的那次被谁、在哪条代码路径上消化了（提示：[src/lib.rs:284-296](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L284-L296) 与 [src/lib.rs:297-315](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L297-L315) 两条路径都不经过你的打印）？
   - `[池]` 行之后还有 `[领取]` 行吗？为什么（提示：`is_stopping` 置位后货架即使非空，worker 醒来看到旗也直接退出）？
4. `concurrent_scopes`（3 个 worker、128 个短命 scope）的日志里，`[投递]` 多不多？结合「每个 scope 只做一次 `join`、闭包瞬间完成、新心跳初值为 true」解释你的观察。
5. 收尾：`git restore src/lib.rs`，`cargo test --lib && cargo test --doc` 确认全绿、工作区干净。

预期：所有测试通过；`join_wait` 能看到跨线程领取，`concurrent_scopes` 的投递行为与你的解释自洽。具体日志**待本地验证**。

## 6. 本讲小结

- `Context` = `Mutex<LockContext>` + 两个 `Condvar`；`LockContext` 是所有线程共享的「调度账本」：`time` 时间戳、`is_stopping` 停机旗、`shared_jobs` 任务货架、`heartbeats` 心跳登记表、`heartbeat_index` 发号器。共享路径是冷路径，一把粗锁换来极简的正确性推理，热路径完全不碰它。
- `shared_jobs` 是以 scope 编号（心跳 `Arc` 的指针地址）为键的 `BTreeMap`：`entry` 去重保证每个 scope 至多挂一个任务，`pop_first` 按键序确定性出队；值里的投递时间 `u64` 只写不读，是设计演进的残迹——`pop_earliest` 的「最早」实际指键最小，不是时间最早。
- `execute_worker` 的循环是「弹一个 → 执行 → 睡」：锁只覆盖弹出的一瞬，执行绝不持锁；worker 的 `Scope` 与栈上 `JobQueue` 常驻线程一生；一轮至多执行一个任务。
- 首轮循环末尾的 `barrier.wait()` 与主线程的 `Barrier(W+1)` 汇合，保证 `with_config` 返回时全部 worker 就绪、心跳线程启动时登记数恰为 W。
- 防丢唤醒的通用配方在这里体现为：**状态在锁内变更（置 `is_stopping`）、条件在锁内检查（worker 持锁查旗再睡）、notify 只负责叫醒**；`heartbeat()` 挂一个任务 `notify_one`，`Drop` 停机 `notify_all`，先 join worker 后 join 心跳线程。
- worker 不是共享货架唯一的消费者：`wait_for_sent_job` 里等结果的发起线程同样在弹出并执行别人的任务——「等待即帮忙」，这是下一讲的入口。

## 7. 下一步学习建议

下一讲 **u2-l4「任务共享与结果等待的完整链路」**将把本讲的每个角色连成一台完整的机器：`heartbeat()` 挂出任务 → worker（或等待者）领取 → `wait_for_sent_job` 忙等中消化他人任务 → 结果经 job.rs 的一次性通道回到发起线程。本讲 4.2.3 结尾和 4.3.3 留下的两个悬念（「谁保证货架不积压」「`receiver` 的结果怎么到达」）都在那里收口。

再往后的 **u3-l1** 会深入本讲只是路过一眼的 `execute_heartbeat`（[src/lib.rs:147-189](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L147-L189)）：`Weak` 引用的自动清理、心跳间隔在线程间均摊、以及 `wait_while` 谓词的完整语义。建议预习时先自己读一遍这个函数，带着「`retain` 为什么安全」「`checked_div` 除以 0 怎么办」两个问题进下一单元。
