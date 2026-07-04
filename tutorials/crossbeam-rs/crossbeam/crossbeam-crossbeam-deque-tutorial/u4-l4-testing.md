# 测试体系：smoke、spsc、stampede、mpmc 与 Miri

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `crossbeam-deque` 的 `tests/` 目录是按 **fifo / lifo / injector / steal** 四个维度切分、再按 **smoke → 并发 → 压力** 递进组织的，并能解释为什么这样切。
- 读懂并复用 `busy_retry` 这个辅助函数：它如何把可能返回 `Steal::Retry` 的偷取操作包装成确定性断言。
- 理解 `spsc`、`stampede`、`mpmc`、`stress`、`destructors` 等经典并发测试「套路」各自的验证目标与终止协调方式。
- 掌握三种 **Miri 兼容技巧**：`cfg!(miri)` 缩小规模、`#[cfg_attr(miri, ignore)]` 跳过过慢用例、`option_env!("MIRI_FALLIBLE_WEAK_CAS")` 在弱 CAS 环境跳过。
- 能仿照 `stampede` 的写法，为 `Worker` 编写一个新的「无丢失、无重复」回归测试。

## 2. 前置知识

在进入测试代码之前，请确认你已经理解以下概念（它们在前面几讲已建立）：

- **三种队列角色与三种偷取变体**：`Worker`（单线程私有本地队列）、`Stealer`（由 `Worker::stealer()` 派生、可跨线程共享）、`Injector`（全局 MPMC FIFO）；偷取方法 `steal` / `steal_batch` / `steal_batch_and_pop`。参见 u1-l4。
- **`Steal<T>` 三态**：`Empty`（队列真空）、`Success(T)`（成功）、`Retry`（伪失败，需立刻重试）。`Retry` 是无锁乐观并发算法的伴生物——`compare_exchange` 抢占失败时偷取侧返回 `Retry` 而非真正的错误。参见 u2-l3。
- **FIFO 与 LIFO 的出队顺序**：对同一序列 `push 1,2,3`，`steal`/`pop` 在 FIFO 下得到 `1,2,3`，在 LIFO 下 `pop` 得到 `3,2,1` 而 `steal` 仍得到 `1,2,3`（偷取侧永远从队头取）。参见 u1-l3。
- **`crossbeam_utils::thread::scope`**：crossbeam 提供的受限线程作用域（scoped threads），允许借用栈上数据 `&T` 跨线程使用，无需 `Arc` 或 `'static`。测试大量依赖它来短小地启动一组线程。

> 术语提示：本讲所说的「flavor」指队列的口味 `Fifo` / `Lifo`；「源（source）」指被偷的队列，「目的（dest）」指偷入的目标 `Worker`。

## 3. 本讲源码地图

`crossbeam-deque` 没有内联单测（`src/deque.rs` 中没有任何 `#[cfg(test)]` 模块，可自行用 `Grep` 验证），所有测试都以**集成测试**形式放在 `tests/` 目录，每个文件是一个独立的测试 crate：

| 文件 | 行数 | 维度 | 覆盖范围 |
|------|------|------|----------|
| `tests/fifo.rs` | 342 | FIFO `Worker` | smoke、is_empty、spsc、stampede、stress、no_starvation、destructors |
| `tests/lifo.rs` | 344 | LIFO `Worker` | 与 `fifo.rs` **结构完全镜像**，仅 flavor 与期望顺序不同 |
| `tests/steal.rs` | 225 | 偷取 flavor 组合 | 16 个测试，覆盖 `steal` / `steal_batch` / `steal_batch_and_pop` × 各 flavor 组合 |
| `tests/injector.rs` | 387 | `Injector` 全局队列 | smoke、is_empty、spsc、**mpmc**、stampede、stress、destructors、**stack_overflow** |

此外，测试只依赖一个 `dev-dependency`：

- [Cargo.toml:39-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L39-L40) 声明 `fastrand = "2"`，用于 `stress` 测试里的轻量随机数；而 `crossbeam_utils::thread::scope` 直接来自普通依赖 `crossbeam-utils`，无需额外声明。

> 永久链接基址为仓库 HEAD `6195355`，下文所有链接均指向该 commit。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块。建议按顺序读：先认识两个测试「脚手架」（4.1），再依次看四种被测对象（4.2 ~ 4.4），最后单独拆 Miri 工程技巧（4.5）。

### 4.1 测试脚手架：busy_retry 与 crossbeam_utils::thread::scope

#### 4.1.1 概念说明

无锁偷取操作可能返回 `Steal::Retry`——它表示「这次没偷到，但不是因为空，而是因为和别人抢输了（CAS 失败或 `buffer` 被换掉），请你立刻重试」。在**确定性单元测试**里，我们往往只想断言「能偷到 / 偷完是空」，不想为 `Retry` 写一堆重试循环把代码搞乱。于是测试文件各自定义了一个名为 `busy_retry` 的小工具：把任意一个可能返回 `Retry` 的闭包，循环重试直到它返回**非 `Retry`** 的结果。

另一个脚手架是 `crossbeam_utils::thread::scope`。`std::thread::spawn` 要求闭包 `Send + 'static`，这在测试里很笨重（要把每个数据包进 `Arc`、`move` 进线程）。`scope` 提供受限线程作用域：在 `scope` 块结束前所有 spawned 线程保证 join 完毕，因此允许线程借用栈上的 `&T`，测试代码得以写得非常短。

#### 4.1.2 核心流程

`busy_retry` 的控制流很简单：

```text
loop {
    let s = f();          // 调用被包裹的偷取操作
    if !s.is_retry() {    // 只要是 Empty 或 Success 就返回
        return s;
    }
    // 否则（Retry）立刻下一轮，不自旋让步
}
```

`scope` 的典型用法（所有测试共用）：

```text
scope(|scope| {
    scope.spawn(|_| { /* 线程 1：可借用外部 &T */ });
    scope.spawn(|_| { /* 线程 2 */ });
    // 主线程也可以在这里直接做事
}).unwrap();  // 作用域结束前会 join 所有线程
```

#### 4.1.3 源码精读

`steal.rs` 与 `injector.rs` 各自定义了**完全相同**的 `busy_retry`（因为集成测试文件互相独立、不能共享代码）：

- [tests/steal.rs:7-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L7-L14) 用 `is_retry()` 判断并循环重试。注意它接受的是 `impl FnMut() -> Steal<T>`，因此每次调用都会**重新执行**偷取（而不是缓存同一次结果）。
- [tests/injector.rs:13-20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L13-L20) 是同一份实现的拷贝。

`scope` 的导入与使用可参看 `fifo.rs` 的测试头与 `spsc` 测试体：

- [tests/fifo.rs:1-10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L1-L10) 顶部 `use crossbeam_utils::thread::scope;`。
- [tests/fifo.rs:81-99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L81-L99) 在 `scope` 内 spawn 一个消费线程，同时主线程直接 `push`——`scope` 让消费线程可以借用栈上的 `s`（`Stealer`）。

> 为什么 `busy_retry` 在并发场景下也安全？因为 `Retry` 的语义是「请立刻重试」，自旋重试本身就是被允许的语义；唯一风险是无限循环，但在所有使用 `busy_retry` 的测试里，要么队列已被预填充足够任务、要么生产者一定能跟上，故循环必然终止。

#### 4.1.4 代码实践

**目标**：亲手体会 `busy_retry` 如何把可能 `Retry` 的 `Injector::steal` 变成确定性结果。

**步骤**：

1. 在 `tests/` 下新建一个临时文件 `playground.rs`，写入：

   ```rust
   use crossbeam_deque::{Injector, Steal::{self, Success}};

   fn busy_retry<T>(mut f: impl FnMut() -> Steal<T>) -> Steal<T> {
       loop {
           let s = f();
           if !s.is_retry() { return s; }
       }
   }

   #[test]
   fn play() {
       let q = Injector::new();
       q.push(42);
       assert_eq!(busy_retry(|| q.steal()), Success(42));
   }
   ```

2. 运行 `cargo test --test playground`。

**需要观察的现象**：测试稳定通过。如果把 `busy_retry(|| q.steal())` 换成直接 `q.steal()`，断言 `assert_eq!(q.steal(), Success(42))` 在大多数机器上**仍然**通过（单线程几乎不会 `Retry`），这正是 `Retry` 的隐蔽性——只有在并发竞争下它才会频繁出现，所以需要 `busy_retry` 这类工具来保证断言的稳健性。

**预期结果**：测试通过。

#### 4.1.5 小练习与答案

**练习 1**：`busy_retry` 返回 `Steal<T>`，调用方仍然可能拿到 `Empty`。如果调用方只想在「确定能拿到值」时继续，应该怎么写？

**答案**：用 `if let Success(v) = busy_retry(|| q.steal()) { ... }`，对 `Empty` 与 `Retry`（已被吸收）一概不处理；这与 `injector.rs` 里 `spsc`/`mpmc` 测试中 `if let Success(v) = q.steal()` 的写法一致。

**练习 2**：`busy_retry` 在生产环境（非测试）的代码里适合直接用吗？为什么测试里却大量用它？

**答案**：生产环境通常应该用 `Backoff::snooze` 之类的退避策略来重试 `Retry`，避免空转浪费 CPU；测试里追求的是「确定性与代码简洁」，且运行时间短，故直接忙等即可。

### 4.2 fifo.rs / lifo.rs：smoke → spsc → stampede → stress 递进

#### 4.2.1 概念说明

`fifo.rs` 与 `lifo.rs` 是**镜像双胞胎**：两者 7 个测试函数的名字、结构、并发拓扑完全相同，只有三处不同——`new_fifo` vs `new_lifo`、`pop` 期望的出队顺序、个别断言的比较方向。这种「同模板、换 flavor」的写法本身就是一种测试设计：它能最大化地保证 FIFO 与 LIFO 两条实现路径被**同等强度**地覆盖。

测试按强度递进：

- **smoke**：单线程、确定性顺序，验证基本正确性。
- **is_empty**：单线程，验证 `is_empty()` 在各种 push/pop/steal 后的状态。
- **spsc**（single-producer single-consumer）：2 线程，一个生产、一个消费，验证跨线程可见性与顺序。
- **stampede**（蜂拥）：1 个 owner + N 个 stealer 同时抢同一批预填充任务，验证「恰好消费一次」。
- **stress**：随机混合 push/pop/steal/steal_batch，长时间压力测试。
- **no_starvation**：验证多 stealer 都能拿到任务（无饿死），Miri 下跳过。
- **destructors**：验证带 `Drop` 的元素被正确析构、无 double-drop、无泄漏。

#### 4.2.2 核心流程

**stampede** 是本模块最重要的「套路」，几乎所有「无丢失无重复」断言都用它。流程：

```text
1. owner 线程预填充 COUNT 个任务（每个任务带唯一递增 id）
2. 创建共享计数器 remaining = AtomicUsize(COUNT)
3. scope 内：
   a. spawn N 个 stealer 线程：
      while remaining > 0:
          if steal 成功拿到 x:
              断言 x > 上次拿到的 last   // 验证本线程内单调（FIFO）
              remaining -= 1
   b. 主线程（owner）同时 pop：
      while remaining > 0:
          if pop 成功拿到 x:
              断言 x > last
              remaining -= 1
4. scope 结束（所有线程 join）
```

关键点：**用 `remaining` 计数器作为所有线程共同的终止条件**。只要还有任务没被消费，大家就继续抢；`remaining` 归零即所有任务被恰好消费一次（每个任务被某个线程 `fetch_sub(1)` 一次）。

#### 4.2.3 源码精读

先看 `smoke`，理解 FIFO 的基本顺序断言：

- [tests/fifo.rs:12-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L12-L46) 单线程下 `push 3,4,5` 后 `steal` 依次得到 `3,4,5`；`push 6,7,8,9` 后 `pop` 得到 `6`、`steal` 得到 `7`、再 `pop` 得到 `8,9`——注意 owner 的 `pop` 与 stealer 的 `steal` **从同一队列的不同端**取，FIFO 下都从队头方向，故 owner `pop` 取走 `6` 后 stealer 接着取 `7`。
- 对比 [tests/lifo.rs:37-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L37-L46)：同样 `push 6,7,8,9`，但 LIFO 下 `pop` 先取 `9`（后进先出），`steal` 取 `6`（偷取侧仍从队头），再 `pop` 得到 `8,7`。这是 FIFO/LIFO 最直观的差异断言。

再看 `stampede`：

- [tests/fifo.rs:102-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L102-L141) 8 个 stealer + 1 个 owner，预填充 50000（Miri 下 500）个 `Box<i>`，用 `remaining` 计数协调。每个线程维护局部 `last`，断言每次新拿到的值严格大于 `last`——这能同时验证「不重复」（同一个值不会被两个线程都拿到，否则会破坏单调性）与「FIFO 顺序」。
- [tests/lifo.rs:104-143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L104-L143) 镜像版本，唯一显著差异在 [tests/lifo.rs:133-140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L133-L140)：owner 的 `last` 初值设为 `COUNT + 1`，断言改成 `last > *x`（LIFO 下 owner 从 back 端 pop，取到的值**递减**）。

最后看 `stress` 的随机混合：

- [tests/fifo.rs:143-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L143-L200) 主线程用 `fastrand` 随机决定「清空本地队列」还是「push 一个新任务」，每个 stealer 持有自己的 `w2` 接收 `steal_batch` 的结果；用全局 `hits` 计数，最后断言 `hits == COUNT`。这是把 `steal`、`steal_batch`、`steal_batch_and_pop`、`pop` 全部混在一起的「模糊测试（fuzz-like）」。

> 顺带一提：[tests/fifo.rs:202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L202) 的 `no_starvation` 用 `#[cfg_attr(miri, ignore)]` 标注，理由写在注释里——「Miri is too slow」。这类跳过留到 4.5 详讲。

#### 4.2.4 代码实践

**目标**：亲眼对照 FIFO 与 LIFO 两个 `smoke` 测试的断言差异，加深对「pop 方向不同」的理解。

**步骤**：

1. 打开 [tests/fifo.rs:37-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L37-L46) 与 [tests/lifo.rs:37-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L37-L46) 并排阅读。
2. 在纸上对 `push 6,7,8,9` 画出队列状态（`front..back` 游标），标注：
   - FIFO 下 owner 第一次 `pop()` 取走的是哪个槽位？
   - LIFO 下 owner 第一次 `pop()` 取走的是哪个槽位？
   - 两种情况下第一次 `steal()` 取走的又是哪个？

**需要观察的现象**：两种 flavor 下第一次 `steal()` 取走的任务相同（都是 `6` 或对应队头），但第一次 `pop()` 取走的任务不同（FIFO 取 `6`、LIFO 取 `9`）。

**预期结果**：在纸上得到 FIFO pop 序列 `6,8,9`、LIFO pop 序列 `9,8,7`，与源码断言一致。这一步是纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`stampede` 测试里，为什么断言「`x > last`（FIFO）」能同时验证「不重复」？如果出现重复消费，这条断言会怎样？

**答案**：任务 id 严格递增地入队，每个线程局部维护已见最大值 `last`。若同一任务被两个线程消费，则至少有一个线程会再次拿到一个 ≤ 自己 `last` 的值（因为该值之前已被本线程或被单调性约束下的前序值覆盖），`x > last` 断言失败。加上 `remaining` 用 `fetch_sub` 原子递减恰好 `COUNT` 次后归零，三者共同保证「恰好一次」。

**练习 2**：`stampede` 用 `remaining.load(SeqCst) > 0` 作为循环条件，为什么这里必须用 `SeqCst`？

**答案**：`remaining` 是所有线程共享的终止信号，既要与 `fetch_sub(SeqCst)` 配对保证计数正确，又要保证「归零」这一状态变化对所有线程可见且不与偷取操作的内存序产生意外重排。`SeqCst` 提供最强的全局顺序保证，在测试里宁可牺牲一点性能也要换正确性。

### 4.3 steal.rs：flavor 组合矩阵与批量偷取断言

#### 4.3.1 概念说明

`steal.rs` 是一份**纯组合矩阵测试**。它不测并发，只测「在 16 种 flavor 组合下，偷取（含批量）到底从哪一端取、按什么顺序进入目的队列」。这对应 u2-l4 讲过的不变式：**被偷批次在目的队列里的消费相对顺序只由源 flavor 决定，与目的 flavor 无关**。

矩阵分三个方法 × 多种源/目的组合：

- `steal`：3 个测试（源为 fifo / lifo / injector）。
- `steal_batch`：6 个测试（源 fifo/lifo/injector × 目的 fifo/lifo，去掉重复语义后是 fifo-fifo、lifo-lifo、fifo-lifo、lifo-fifo、injector-fifo、injector-lifo）。
- `steal_batch_and_pop`：6 个测试，组合同上，但会直接弹出一个任务返回。

#### 4.3.2 核心流程

以「源 push `1,2,3,4`，偷一半（2 个）到目的队列」为例，对照不同源/目的 flavor：

| 源 flavor | 目的 flavor | `steal_batch` 后目的 `pop` 序列 |
|-----------|-------------|--------------------------------|
| FIFO | FIFO | `1, 2` |
| LIFO | LIFO | `2, 1` |
| FIFO | LIFO | `1, 2` |
| LIFO | FIFO | `2, 1` |

关键观察：**目的 pop 序列只取决于源 flavor**——源是 FIFO 得 `1,2`，源是 LIFO 得 `2,1`；目的 flavor（决定 pop 从哪端）只影响「从目的队列哪一端取」，但取出的相对顺序不变。这正是 u2-l4 「相对顺序由源 flavor 决定」不变式的直接体现。

`steal_batch_and_pop` 多了一步：从偷到的批次里**额外弹出 1 个**作为返回值，剩余进入目的队列。例如源 FIFO push `1..=6`，`steal_batch_and_pop` 偷 3 个（一半 `ceil(6/2)=3`），返回 `Success(1)`，剩余 `2,3` 进目的队列。

#### 4.3.3 源码精读

- **单任务 `steal`**：[tests/steal.rs:16-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L16-L27)（FIFO）、[tests/steal.rs:29-40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L29-L40)（LIFO）、[tests/steal.rs:42-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L42-L52)（Injector）。注意三者都断言 `steal` 序列为 `1,2,3`——**偷取侧永远从队头取，与源是 FIFO 还是 LIFO 无关**。Injector 版用 `busy_retry` 包裹，因为 Injector 的 steal 可能 `Retry`。
- **`steal_batch` 四种 Worker 组合**：[tests/steal.rs:54-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L54-L67)（fifo→fifo，断言 `1,2`）、[tests/steal.rs:69-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L69-L82)（lifo→lifo，断言 `2,1`）、[tests/steal.rs:84-97](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L84-L97)（fifo→lifo，断言 `1,2`）、[tests/steal.rs:99-112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L99-L112)（lifo→fifo，断言 `2,1`）。这四条正是上表四行的直接断言。
- **`steal_batch_and_pop` 四种 Worker 组合**：以 [tests/steal.rs:140-153](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L140-L153)（fifo→fifo）为例，push `1..=6`，断言 `steal_batch_and_pop` 返回 `Success(1)`，目的队列 `pop` 得到 `2,3`；而 [tests/steal.rs:155-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L155-L168)（lifo→lifo）返回 `Success(3)`、目的 pop 得到 `2,1`——返回的是「最新偷到」的那个。
- **Injector 作为源**：[tests/steal.rs:114-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L114-L125) 与 [tests/steal.rs:200-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L200-L211)，均用 `busy_retry` 包裹，断言 Injector 作为 FIFO 源时进入目的队列的顺序是 `1,2,...`。

#### 4.3.4 代码实践

**目标**：通过修改入队数量，验证「偷一半」策略。

**步骤**：

1. 复制 [tests/steal.rs:54-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L54-L67) 的 `steal_batch_fifo_fifo` 到一个临时测试文件。
2. 把入队数量从 `1..=4` 改成 `1..=10`。
3. 在 `steal_batch(&w2)` 之后，循环 `while let Some(v) = w2.pop()` 收集所有偷到的值并打印。

**需要观察的现象**：打印出的值是 `1,2,3,4,5`（共 5 个，即 `ceil(10/2)=5`，符合 u2-l4 讲的「偷约一半」策略，且受 `MAX_BATCH=32` 上限约束在此例不触发）。

**预期结果**：偷到的序列长度为 5，值为 `1..=5`。待本地验证具体打印输出。

#### 4.3.5 小练习与答案

**练习 1**：`steal_batch_lifo_fifo`（[tests/steal.rs:99-112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/steal.rs#L99-L112)）源是 LIFO、目的是 FIFO，断言目的 pop 得到 `2,1`。请解释为什么目的虽然是 FIFO（先进先出）却得到「倒序」。

**答案**：因为偷取时源是 LIFO，按 u2-l4 的不变式，被偷批次进入目的队列时的相对顺序由源 flavor 决定：LIFO 源会把「最新入队的」先写入目的，因此进入目的队列的写入顺序是 `2,1`（2 先写、1 后写）。目的 FIFO 从队头消费，于是先 pop 出最早写入的 `2`，再 pop 出 `1`，得到 `2,1`。

**练习 2**：为什么 `steal_batch` 对 Worker 源不需要 `busy_retry`，而对 Injector 源却需要？

**答案**：`steal_batch` 内部用单个 CAS 认领一整段，要么成功要么失败返回 `Retry`。Worker 源在测试中是单线程调用（无竞争），CAS 几乎必成；Injector 源即便单线程，其底层 Block 链表在跨 block 边界、block 安装等环节仍可能返回 `Retry`，故用 `busy_retry` 吸收以稳定断言。

### 4.4 injector.rs：mpmc 并发计数、destructors 与 stack_overflow

#### 4.4.1 概念说明

`injector.rs` 是覆盖 `Injector`（全局 MPMC FIFO）的测试文件，结构与 `fifo.rs`/`lifo.rs` 类似，但有两个独有的重量级测试：

- **mpmc**（multi-producer multi-consumer）：多个线程同时 `push`、多个线程同时 `steal`，验证 `Injector` 作为 MPMC 队列的「每条消息恰好被消费一次」。
- **stack_overflow**：一个针对 0.8.6 修复（GHSA 安全公告相关、大对象栈溢出）的回归测试。

`destructors` 测试则验证带 `Drop` 的元素在并发偷取 + 队列析构时被正确回收，无泄漏、无 double-drop。

#### 4.4.2 核心流程

**mpmc** 的拓扑（这是 MPMC 队列的经典验证套路）：

```text
COUNT = 25000（Miri 下 500），THREADS = 4
共享：Injector q，以及计数数组 v: Vec<AtomicUsize>（长度 COUNT，初值 0）

scope 内 spawn 两批线程：
  批次 1（4 个生产者）：每个线程 push 0..COUNT（注意每个生产者都 push 同样的 0..COUNT）
  批次 2（4 个消费者）：每个线程循环 steal COUNT 次：
      loop { if let Success(n) = q.steal() { v[n] += 1; break; } }

主断言：for 每个 n in 0..COUNT: v[n] == THREADS
```

含义：值 `n` 被 4 个生产者各 push 一次（共 4 份），应被恰好消费 4 次，所以 `v[n]` 必须等于 `THREADS=4`。这同时验证了「无丢失」（每份都被消费）与「无重复」（每份只被消费一次，否则某个 `v[n]` 会 > 4、另一个会 < 4）。

**destructors** 的关键在于用一个把自身 id 推入共享 `Vec` 的 `Drop` 实现来追踪析构：

\[ \text{已析构数} + \text{剩余未消费数} = \text{总数 COUNT} \]

测试先验证并发偷取阶段析构数 == `COUNT - rem`，再 `drop(w)`（或 `drop(q)`）后验证剩余 `rem` 个也全部析构，且析构出的 id 集合恰好无缺漏。

#### 4.4.3 源码精读

- **mpmc**：[tests/injector.rs:88-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L88-L125)。生产者在 [L96-L103](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L96-L103)、消费者在 [L105-L118](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L105-L118)、最终断言在 [L122-L124](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L122-L124)。注意消费者用 `if let Success(n) = q.steal()` 配 `loop` 重试，并在 Miri 下插入 `std::hint::spin_loop()` 让出（见 [L113-L114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L113-L114)）。
- **destructors**：[tests/injector.rs:291-369](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L291-L369)。`Elem` 结构与 `Drop` 实现在 [L297-L303](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L297-L303)；并发偷取后先断言 [L354-L358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L354-L358)（已析构 == `COUNT - rem`），`drop(q)` 后再断言 [L362-L368](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L362-L368)（剩余 `rem` 个全部析构且 id 连续无缺）。
- **stack_overflow**：[tests/injector.rs:372-386](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L372-L386)。注释解释了动机——若 `Block` 在栈上创建，`BigStruct`（32KB）乘以 slot 数组会撑爆线程栈，故 `Block` 必须直接在堆上分配。这是 0.8.6（见 CHANGELOG 与 u1-l1）针对大对象栈溢出修复的回归测试。
- **spsc**：[tests/injector.rs:59-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L59-L86)，与 `fifo.rs` 的 spsc 同构，但规模更大（`COUNT = 100_000`），且消费端用 `loop` + `Success` 匹配而非 `busy_retry`，并配 Miri 下的 `spin_loop`（[L73-L74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L73-L74)）。

#### 4.4.4 代码实践

**目标**：理解 `mpmc` 测试为什么用「每个生产者都 push 同样的 0..COUNT」这种看似奇怪的设计。

**步骤**：

1. 阅读 [tests/injector.rs:88-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L88-L125)。
2. 回答：如果改成「4 个生产者各 push 互不相同的四分之一区间」，断言应如何改写？此时还能用 `v[n] == THREADS` 吗？

**需要观察的现象/思考**：当前设计里值 `n` 被 push 了 `THREADS` 次，故期望被消费 `THREADS` 次，`v[n] == THREADS`。若改成各 push 不相交区间，则每个值只被 push 一次、应被消费一次，断言应改为 `v[n] == 1` 对所有 `n`。

**预期结果**：能说清「`v[n]` 的期望值 == 值 `n` 被生产者 push 的总次数」这一不变式。这是纯阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：`destructors` 测试在 `drop(q)` 之前先断言「已析构数 == `COUNT - rem`」，其中 `rem = remaining.load(SeqCst)` 且 `assert!(rem > 0)`（[tests/injector.rs:351-353](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L351-L353)）。为什么断言 `rem > 0`（即故意留下没消费完的任务）？

**答案**：故意留下 `rem > 0` 个任务不消费，是为了验证**队列析构时**（`drop(q)`）会把这批残留任务也正确析构。如果全部消费完（`rem == 0`），就无法测试析构路径对「未消费任务」的回收。`drop(q)` 后的断言（[L362-L368](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L362-L368)）正好检查这 `rem` 个全部析构、id 连续，覆盖了 `Injector::Drop` 的正确性。

**练习 2**：`mpmc` 用 `v[n].fetch_add(1, SeqCst)` 计数，为什么这里用 `fetch_add` 而不是普通 `+= 1`？

**答案**：多个消费者线程会并发地对**同一个** `v[n]` 计数（值 `n` 被 4 个生产者各 push 一次、4 个消费者争抢），普通 `+= 1` 是「读-改-写」非原子操作会丢失更新，导致某些 `v[n] < THREADS`。`fetch_add(1, SeqCst)` 保证每次递增都是原子的，计数精确。

### 4.5 Miri 工程实践：cfg!(miri)、option_env! 与 cfg_attr

#### 4.5.1 概念说明

[Miri](https://github.com/rust-lang/miri) 是 Rust 的**未定义行为（UB）检测器**，它解释执行 MIR，能发现数据竞争、未初始化内存、越界等大量问题。`crossbeam-deque` 这种「用 `ptr::write_volatile` 制造技术性数据竞争」（见 u4-l1）的无锁代码尤其需要 Miri 来兜底。

但 Miri 极慢（比原生慢 100~1000 倍），所以测试必须为 Miri 单独「减负」。本 crate 用了三种技巧：

1. **`cfg!(miri)` 运行时缩小规模**：把 `COUNT`、`THREADS`、`STEPS` 从几万降到几百。
2. **`#[cfg_attr(miri, ignore)]`**：对无论如何都太慢的用例（如 `no_starvation`）在 Miri 下直接跳过。
3. **`option_env!("MIRI_FALLIBLE_WEAK_CAS")`**：在某些 Miri 配置下（弱 CAS 可失败），跳过会触发问题的用例（`stress`）。

此外还有一个微妙技巧：在 Miri 下往忙等循环里插 `std::hint::spin_loop()`，让 Miri 的调度器有机会切换线程，避免某些自旋场景下的活锁。

#### 4.5.2 核心流程

**技巧 1：`cfg!(miri)` 缩小规模**（最常见）：

```rust
const STEPS: usize = if cfg!(miri) { 500 } else { 50_000 };
```

`cfg!(miri)` 是**运行期求值的宏**（返回 `bool`），编译器仍会编译两个分支、但优化掉未取分支，运行时得到正确值。这让同一段测试代码在普通模式跑大规模、在 Miri 跑小规模。

**技巧 2：`#[cfg_attr(miri, ignore)]`**：

```rust
#[cfg_attr(miri, ignore)] // Miri is too slow
#[test]
fn no_starvation() { ... }
```

`cfg_attr` 是**属性级条件展开**：当 `miri` cfg 开启时，等价于加了 `#[ignore]`，测试默认跳过（需 `--ignored` 显式运行）。

**技巧 3：`option_env!("MIRI_FALLIBLE_WEAK_CAS")`**：

```rust
if option_env!("MIRI_FALLIBLE_WEAK_CAS").is_some() {
    return; // see ci/miri.sh
}
```

读取编译期环境变量；Miri 的 CI 脚本 `ci/miri.sh`（注释指向它）在某些目标下会设置该变量来模拟「弱 CAS 可能虚假失败」的语义，此时跳过 `stress`（它对 CAS 失败模式敏感）。

#### 4.5.3 源码精读

- **`cfg!(miri)` 缩规模**：几乎每个并发测试都有，如 [tests/fifo.rs:76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L76)（spsc 的 `STEPS`）、[tests/fifo.rs:104-105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L104-L105)（stampede 的 `THREADS/COUNT`）、[tests/injector.rs:61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L61)（spsc 的 `COUNT`）、[tests/injector.rs:90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L90)（mpmc 的 `COUNT`）、[tests/injector.rs:293-295](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L293-L295)（destructors 同时缩三个常量）。
- **`#[cfg_attr(miri, ignore)]`**：[tests/fifo.rs:202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L202)、[tests/lifo.rs:204](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L204)、[tests/injector.rs:231](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L231)，均用于 `no_starvation`。
- **`option_env!` 跳过**：[tests/injector.rs:173-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L173-L175)，`stress` 开头的早返回。
- **Miri 下插 `spin_loop`**：[tests/lifo.rs:89-91](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L89-L91)、[tests/injector.rs:73-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L73-L74) 与 [tests/injector.rs:113-114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L113-L114)。LIFO 的 `spsc` 在 Miri 下尤其需要它——LIFO 末元素 pop 的 CAS 竞争（见 u2-l2）在 Miri 的协作式调度下可能更易活锁。

> 小知识：`cfg!(miri)`（宏，运行期 `bool`）与 `#[cfg(miri)]`（属性，编译期开关）是同一 cfg 开关的两种用法。Miri 运行测试时会由 `cargo miri test` 自动注入这个 cfg，无需手动设置。

#### 4.5.4 代码实践

**目标**：体验 Miri 如何发现普通测试发现不了的问题，并观察「缩规模」的效果。

**步骤**：

1. 确保有 nightly 工具链与 Miri 组件：`rustup toolchain install nightly`、`rustup +nightly component add miri`。
2. 用 Miri 跑某个文件里的 `spsc`：`cargo +nightly miri test --test fifo spsc`（`--test fifo` 指定集成测试二进制，`spsc` 是测试名过滤；注意 `spsc` 在 fifo/lifo/injector 三个文件都有，可换 `--test injector` 等）。
3. 观察输出中 Miri 是否报告任何 UB；同时留意测试规模——`cfg!(miri)` 让它只跑 500 步而非 50000 步。

**需要观察的现象**：Miri 解释执行下测试明显变慢，但 `spsc` 因缩到 500 步仍可在合理时间内通过；不报告数据竞争（说明 volatile 读写的可见性确实由 fence/原子索引正确建立，印证 u4-l1）。

**预期结果**：测试在 Miri 下通过、无 UB 报告。若本地未安装 nightly/Miri，则标注「待本地验证」。

> 如果时间允许，可以试跑未被 `#[cfg_attr(miri, ignore)]` 的并发用例（如 `stampede`），体会 Miri 对并发测试的巨大开销，从而理解为何 `no_starvation` 要被跳过。

#### 4.5.5 小练习与答案

**练习 1**：`cfg!(miri) { 500 } else { 50_000 }` 与直接写 `const COUNT: usize = 50_000;` 相比，多出的开销是什么？为什么可接受？

**答案**：`cfg!(miri)` 是运行期宏，编译器需保留两个分支的常量求值（虽都被优化为常量），有极小的编译期开销，但**运行期零开销**（分支被常量折叠）。对于测试代码这点编译开销完全可以接受，换来的是「同一份代码两种规模」的可维护性，避免维护两套测试。

**练习 2**：`option_env!("MIRI_FALLIBLE_WEAK_CAS")` 与 `cfg!(miri)` 都能影响 Miri 下的行为，它们的本质区别是什么？

**答案**：`cfg!(miri)` 检测的是「是否运行在 Miri 下」这一个布尔事实；`option_env!("MIRI_FALLIBLE_WEAK_CAS")` 检测的是「是否设置了某个具体的环境变量」，它**只在特定 Miri 配置**（模拟弱 CAS 的目标）下才被设置。前者是「Miri 与否」的粗粒度开关，后者是「Miri 的某种具体行为模式」的细粒度开关，故 `stress` 用后者做更精确的跳过判断。

## 5. 综合实践

把本讲学的「stampede 套路 + AtomicUsize 恰好一次计数」综合起来，为 `Worker` 写一个回归测试。这也是本讲的指定实践任务。

**任务**：在 LIFO `Worker` 中 `push 1..=100`，开 4 个偷取线程 + 主线程 `pop`，用 `AtomicUsize`（数组形式）计数所有被取走的任务，最终断言这 100 个任务**各被取走恰好一次**（无丢失、无重复）。

**参考实现**（可放入 `tests/` 下新文件，如 `tests/regression.rs`）：

```rust
// 示例代码：仿照 fifo.rs / lifo.rs 的 stampede 写法
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
use crossbeam_deque::{Steal::Success, Worker};
use crossbeam_utils::thread::scope;

#[test]
fn regression_lifo_no_loss_no_dup() {
    const COUNT: usize = 100;
    const STEALERS: usize = 4;

    let w = Worker::new_lifo();
    for i in 1..=COUNT {
        w.push(i);
    }

    // seen[i] 记录值 (i+1) 被取走的次数；期望最终每个都是 1
    let seen: Vec<AtomicUsize> = (0..COUNT).map(|_| AtomicUsize::new(0)).collect();
    let remaining = AtomicUsize::new(COUNT);
    let seen = &seen;            // 借用给所有线程
    let remaining = &remaining;

    scope(|scope| {
        // 4 个偷取线程
        for _ in 0..STEALERS {
            let s = w.stealer(); // 每个 stealer 共享同一底层 Inner
            scope.spawn(move |_| {
                while remaining.load(SeqCst) > 0 {
                    if let Success(v) = s.steal() {
                        seen[v - 1].fetch_add(1, SeqCst);
                        remaining.fetch_sub(1, SeqCst);
                    }
                }
            });
        }

        // 主线程（owner）同时 pop
        while remaining.load(SeqCst) > 0 {
            if let Some(v) = w.pop() {
                seen[v - 1].fetch_add(1, SeqCst);
                remaining.fetch_sub(1, SeqCst);
            }
        }
    }).unwrap();

    // 断言：100 个任务各被取走恰好一次
    for i in 0..COUNT {
        assert_eq!(seen[i].load(SeqCst), 1, "值 {} 被取走次数不为 1", i + 1);
    }
}
```

**操作步骤**：

1. 把上述代码保存为 `tests/regression.rs`。
2. 运行 `cargo test --test regression`。
3. 多跑几次（`for i in $(seq 1 50); do cargo test --test regression || break; done`）以增加触发并发竞争的概率。

**需要观察的现象**：

- 每次运行都应通过——`remaining` 用 `fetch_sub` 恰好递减 100 次归零，保证无丢失；`seen[v-1]` 用 `fetch_add` 计数，最终每个都是 1，保证无重复。
- 如果实现有 bug（比如某个任务被消费两次、另一个丢失），断言会精确指出哪个值出错。

**预期结果**：测试稳定通过。若偶尔想验证「断言真的有效」，可故意把 `fetch_sub` 注释掉一个（**仅作实验，不要提交**），观察 `remaining` 永不归零导致线程无法退出（测试挂起）——反向印证计数协调的必要性。

> 这与 `fifo.rs` 的 `stampede`（[tests/fifo.rs:102-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/fifo.rs#L102-L141)）的区别在于：stampede 用「`last` 单调性 + `remaining` 计数」间接证明无重复，而本实践用「`seen` 数组逐值计数」直接、显式地证明每个值恰好一次，更适合作为回归测试。

## 6. 本讲小结

- `tests/` 目录按 **fifo / lifo / injector / steal** 四维切分，每个文件是独立集成测试 crate；`fifo.rs` 与 `lifo.rs` 是镜像双胞胎，保证两条 flavor 路径被同等覆盖。
- 测试按强度 **smoke → spsc → stampede → stress → destructors** 递进，从单线程确定性逐步走向多线程压力与析构正确性。
- `busy_retry` 是把可能返回 `Steal::Retry` 的偷取包装成确定性断言的小工具；`crossbeam_utils::thread::scope` 让测试能借用栈数据短小地启动线程。
- **stampede** 套路（预填充 + `remaining` 计数协调 + 单调性/逐值计数断言）是验证「无丢失无重复」的标准范式，MPMC 队列则用 **mpmc** 测试的 `v[n] == THREADS` 断言。
- `steal.rs` 用 16 个测试构成的 flavor 组合矩阵，精确锁死「批量偷取的相对顺序由源 flavor 决定、与目的 flavor 无关」这一不变式。
- Miri 兼容靠三招：`cfg!(miri)` 缩规模、`#[cfg_attr(miri, ignore)]` 跳过慢用例、`option_env!("MIRI_FALLIBLE_WEAK_CAS")` 按需跳过；外加 Miri 下插 `spin_loop` 防活锁。

## 7. 下一步学习建议

- **横向对照实现**：本讲的 `steal.rs` flavor 矩阵断言，正好对应 u2-l4 讲的批量偷取不变式；带着这些断言重读 `src/deque.rs` 中 `steal_batch*` 的拷贝与反转逻辑，会有「断言即不变式」的顿悟。
- **跑一遍 Miri**：按 4.5.4 的步骤在本机用 Miri 跑一次并发测试，亲眼确认无 UB 报告——这是对 u4-l1「volatile hack 的可见性靠 fence 保证」最有力的实证。
- **下一讲 u4-l5**：把本讲学到的 `scope` + 计数协调 + `find_task` 回退链（u1-l4）合起来，动手搭一个最小的多线程 work-stealing 调度器，这是整个手册的收官实战。
- **延伸阅读**：可以阅读 crossbeam-epoch 的测试，对比它是如何用类似套路验证基于 epoch 的内存回收正确性的；也可关注 `ci/miri.sh`（仓库根目录）了解 CI 如何编排 Miri 运行与 `MIRI_FALLIBLE_WEAK_CAS` 的设置时机。
