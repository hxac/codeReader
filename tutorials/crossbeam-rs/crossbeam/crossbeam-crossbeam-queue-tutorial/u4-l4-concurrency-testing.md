# 并发测试方法与可线性化验证

## 1. 本讲目标

本讲是专家层的「测试方法」篇。前面几讲我们一直在读「队列是怎么实现的」，这一讲换一个视角：**怎么验证它是对的**。

并发队列的特殊之处在于——它几乎不可能靠「单线程跑一遍」来验证。并发的 bug 往往只在特定的线程交错（interleaving）下才暴露，概率极低，可能在测试里跑一万次都不出现，却在生产环境一次就炸。因此 crossbeam-queue 的 `tests/` 目录设计了一套**多范式**的测试组合，从不同角度逼近「正确」。

学完本讲你应当能够：

1. 说出并发队列测试为什么难，以及 crossbeam-queue 用哪几类测试范式来应对。
2. 读懂 `tests/array_queue.rs` 与 `tests/seg_queue.rs` 中的 `spsc` / `mpmc` / `ring_buffer` / `drops` / `linearizable` 测试，并能复用它们的骨架写出自己的并发测试。
3. 理解析构计数（drop counting）如何**同时**检测「内存泄漏」与「重复释放」。
4. 建立「可线性化（linearizability）」的直觉，并理解为什么 `linearizable` 测试只是压力测试、真正的穷尽验证要靠 miri / Loom。
5. 掌握 `crossbeam_utils::thread::scope` 的用法，以及 `cfg!(miri)` 规模缩放的工程实践。

## 2. 前置知识

本讲假设你已经学过：

- **u1-l1 ～ u1-l3**：知道 `ArrayQueue`（有界）与 `SegQueue`（无界）的定位、`push` / `pop` / `force_push` 的基本 API，以及如何用 `cargo test` 跑测试。
- **u2-l2 / u2-l3**：`push` 满了返回 `Err`、`force_push` 满了覆盖最旧元素并返回 `Some(old)`、`pop` 空了返回 `None`。
- **u3-l2**：`SegQueue::push` 永远返回 `()`（无界，不会失败）。
- **u4-l1**：原子内存序（`SeqCst` / `Acquire` / `Release` / `Relaxed`）的含义。

下面用通俗语言补三个本讲专属的概念。

### 概念一：为什么并发测试难？

单线程程序的输出由「输入 + 程序」唯一决定。并发程序的输出还由「线程交错的次序」决定，而交错次序是**调度器**在运行时决定的，几乎不可预测。同一个测试跑两次，每次的实际交错可能不同。所以并发测试的核心难题是：**怎样在「无法控制交错」的前提下，依然能断言正确性？** crossbeam-queue 的回答是——不直接断言交错，而是断言那些「无论怎样交错都必须成立」的不变量（invariant）。

### 概念二：什么是可线性化（linearizability）？

「可线性化」是并发数据结构最强、最直观的正确性准则之一。直觉上它说的是：**每个操作虽然在物理上是和别人重叠并发执行的，但总可以找到一个「瞬间点」（linearization point），把它当成是在那一瞬间「原子地」完成的**；并且这些瞬间点的相对顺序，尊重了各操作在真实时间里「调用 → 返回」的先后。

换句话说：可线性化让使用者可以**假装**这个并发队列是一个「每次操作都瞬间完成」的顺序队列。只要这一点成立，我们就能用顺序队列的常识去推理它（FIFO、不丢、不重）。本讲会看到一个用 `.unwrap()` 把「可线性化」编码进断言的压力测试。

### 概念三：作用域线程（scoped thread）

标准库的 `std::thread::spawn` 要求闭包捕获的所有变量都是 `'static` 的（即拥有所有权，不能借用局部变量），因为新线程可能在局部变量被销毁之后还在跑。这让「在测试里临时开几个线程共享一个局部队列 `q`」变得很啰嗦——要么用 `Arc`，要么手动 `join`。

`crossbeam_utils::thread::scope` 提供的「作用域线程」解决了这个问题：在 `scope` 闭包内用 `scope.spawn` 开出的线程**允许借用局部变量**，并且 **`scope` 闭包返回之前会保证所有子线程都已 join 完毕**。于是借用的局部变量绝不会提前失效。本讲几乎每个并发测试都建立在它之上。

## 3. 本讲源码地图

本讲只涉及两个测试文件和一处依赖声明：

| 文件 | 作用 |
| --- | --- |
| [tests/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs) | `ArrayQueue` 的全部集成测试：`smoke` / `len` / `spsc` / `spsc_ring_buffer` / `mpmc` / `mpmc_ring_buffer` / `drops` / `linearizable` / `into_iter`。 |
| [tests/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs) | `SegQueue` 的全部集成测试，结构与 `ArrayQueue` 对称，另有 `stack_overflow`（见 u3-l4）。 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml) 第 42–43 行 | `[dev-dependencies]` 里的 `fastrand = "2"`，仅供 `drops` 测试造随机数用。 |

这两个测试文件是「姊妹篇」——几乎所有测试名字一一对应，只是 `ArrayQueue` 版多了「满则失败」「`force_push` 覆盖」的分支。对照阅读是理解它们最快的路径。

## 4. 核心概念与源码讲解

### 4.1 测试基础设施：crossbeam-utils::thread::scope 与 cfg!(miri) 规模缩放

#### 4.1.1 概念说明

所有并发测试都建立在两块基础设施之上：

1. **`crossbeam_utils::thread::scope`**：让多个子线程方便地共享同一个局部队列 `&q`，并在作用域结束时自动 join。如果某个子线程 panic，`scope(...)` 会把这个 panic 聚合后以 `Err` 返回——所以测试里常见的 `.unwrap()` 既是在等线程结束，也是在「任何一个子线程断言失败就让整个测试失败」。
2. **`cfg!(miri)` 规模缩放**：miri 是 Rust 官方的「未定义行为检测器」，它能解释执行程序并检查 `unsafe` 是否有 UB（详见 u4-l3 的安全性论证）。代价是**极慢**（比原生慢几十到上百倍）。如果测试规模跟原生一样（比如 10 万次），miri 根本跑不完。所以每个并发测试都用一个 `const` 把规模缩成两档：miri 下很小，原生下很大。

#### 4.1.2 核心流程

一个并发测试的骨架可以这样概括：

```text
1. 定常量 COUNT/CAP：miri 档 vs 原生档（用 cfg!(miri) 选择）
2. 构造队列 q 与必要的计数向量 / 原子计数器
3. scope(|scope| {
       scope.spawn(|_| { 生产者循环：push/force_push，失败就重试 });
       scope.spawn(|_| { 消费者循环：pop，空就重试 });
   }).unwrap();          // ← 等所有线程 join，任何 panic 在此抛出
4. 在主线程校验不变量（如每个值被消费恰好 N 次）
```

#### 4.1.3 源码精读

两个测试文件的第 4 行都导入同一个 `scope`：

- [tests/array_queue.rs:4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L4) 与 [tests/seg_queue.rs:4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L4)：导入 `use crossbeam_utils::thread::scope;`。注意它来自 `crossbeam-utils`，是 `crossbeam-queue` 的 `[dependencies]`（见 [Cargo.toml:40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L40)），不是 `std`。

`cfg!(miri)` 两档规模的典型写法（以 `len` 测试为例）：

- [tests/array_queue.rs:89-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L89-L93)：`const COUNT: usize = if cfg!(miri) { 30 } else { 25_000 };` 以及 `CAP` 同理。这是编译期分支：miri 编译产物里 `COUNT` 就是 30，原生产物里就是 25_000。

`scope` 的典型调用骨架（`spsc` 测试的开头）：

- [tests/array_queue.rs:153-172](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L153-L172)：`scope(|scope| { scope.spawn(...); scope.spawn(...); }).unwrap();`。两个 `spawn` 的闭包都忽略了传入参数（写成 `|_|`，那个参数其实是 `&Scope`），并且都借用着外层的 `&q`——这正是作用域线程带来的便利。

#### 4.1.4 代码实践

**实践目标**：亲手把这两块基础设施跑起来，体会 miri 的「慢」与 scope 的「自动 join」。

**操作步骤**：

1. 在 `crossbeam-queue` 目录下，只跑 `ArrayQueue` 的测试文件：
   ```bash
   cargo test --test array_queue
   ```
2. 挑一个测试单独跑，并开启一些并发压力（多跑几次）：
   ```bash
   cargo test --test array_queue mpmc -- --test-threads=1
   ```
3. （可选，需要 nightly + miri 组件）用 miri 跑同一个测试，观察它如何因为 `cfg!(miri)` 自动缩小规模：
   ```bash
   cargo +nightly miri test --test array_queue mpmc
   ```

**需要观察的现象**：第 1 步很快（毫秒级）；第 3 步即便规模已缩到几十，仍然比第 1 步慢得多（秒级以上），这就是 miri 的代价。

**预期结果**：所有测试通过。若 miri 报告任何 UB，说明 `unsafe` 实现有问题（这正是 miri 的价值——u4-l3 的安全性论证最终要靠它兜底）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `scope(...).unwrap()` 里的 `.unwrap()` 去掉，测试还能不能正确反映「子线程里的断言失败」？

> **答案**：能反映「失败」，但方式不同。`.unwrap()` 的作用是：任一子线程 panic 时，`scope` 返回 `Err(panic_payload)`，`.unwrap()` 会在这里再次 panic，从而让当前测试函数以失败结束。去掉 `.unwrap()` 后，`scope` 返回的 `Result` 被丢弃，子线程的 panic 会被「吞掉」，测试可能误判通过。所以 `.unwrap()` 是把「子线程的断言」与「测试函数的成败」绑定起来的关键。

**练习 2**：为什么规模缩放用 `const` + `cfg!(miri)`，而不是运行时 `if`？

> **答案**：`cfg!(miri)` 在**编译期**求值（miri 是用 rustc 的 miri 分支解释执行，本身就是一个特定的编译产物）。用 `const` 让两档规模都成为编译期常量，零运行时开销，且 miri 产物里根本不存在那 25_000 的循环。

---

### 4.2 spsc 与 mpmc 计数校验测试

#### 4.2.1 概念说明

这是最基本的两类「计数校验」测试，名字来自并发文献里的经典模型：

- **SPSC**（Single-Producer Single-Consumer，单生产者单消费者）：只有一个线程生产、一个线程消费。因为生产序唯一且确定，我们可以**直接断言消费序 == 生产序**（第 `i` 个弹出的值必须是 `i`）。这是在验证 FIFO 顺序。
- **MPMC**（Multi-Producer Multi-Consumer，多生产者多消费者）：多个线程同时生产、多个线程同时消费。此时「全局消费顺序」已经无法预测（不同生产者谁先谁后取决于调度），所以**不能**再断言 `pop` 出来的值是升序的。改用一个不变量：**每个值最终被消费的次数，必须恰好等于它被生产的次数**。用一个计数向量 `v` 记录每个值被 `pop` 到的次数，最后逐个核对。

一句话区别：SPSC 验证「顺序对」，MPMC 验证「不丢不重」。

#### 4.2.2 核心流程

SPSC：

```text
生产者线程：for i in 0..COUNT { 重试直到 push(i) 成功 }
消费者线程：for i in 0..COUNT {
                重试直到 pop 到一个值 x；
                assert_eq!(x, i);          // ← 必须严格升序
            }
            assert!(pop().is_none());      // ← 队列被掏空
```

MPMC（核心是计数向量 `v`）：

```text
v = [0; COUNT] 的 AtomicUsize 数组
THREADS 个消费者：每次 pop 到 n，就 v[n].fetch_add(1)
THREADS 个生产者：每个都推 i in 0..COUNT（即每个值被推 THREADS 次）
结束后：对每个 n，断言 v[n] == THREADS   // 每个值被消费 THREADS 次
```

#### 4.2.3 源码精读

`ArrayQueue` 的 SPSC 测试：

- [tests/array_queue.rs:147-173](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L147-L173)：消费者用 `assert_eq!(x, i)` 严格校验 FIFO；生产者用 `while q.push(i).is_err() {}` 在队列满时自旋重试（`ArrayQueue` 满了会 `Err`，所以需要重试循环）。容量只有 3，迫使生产者频繁等待消费者，制造大量并发交错。

`ArrayQueue` 的 MPMC 测试（计数向量的范式）：

- [tests/array_queue.rs:215-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L215-L249)：第 221 行构造 `v = (0..COUNT).map(|_| AtomicUsize::new(0)).collect::<Vec<_>>()`；消费者 `v[n].fetch_add(1, Ordering::SeqCst)`（第 232 行）；最后第 247 行 `assert_eq!(c.load(Ordering::SeqCst), THREADS)`。注意每个生产者都推 `0..COUNT`，所以每个值 `n` 被推了 `THREADS` 次，期望也就被消费 `THREADS` 次。

`SegQueue` 的 MPMC 测试与之几乎逐字相同，唯一区别是 `SegQueue::push` 返回 `()` 不会失败，所以生产者不需要重试循环：

- [tests/seg_queue.rs:128-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L128-L162)：对照 [tests/array_queue.rs:236-243](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L236-L243) 看「`ArrayQueue` 用 `while q.push(i).is_err() {}`」与「`SegQueue` 直接 `q.push(i)`」的差异——这正是有界 vs 无界在测试代码里的直接体现。

> **小贴士**：MPMC 测试里的 `fetch_add` / `load` 都用 `Ordering::SeqCst`。这是因为计数向量 `v` 是测试自己的「事实记录仪」，与被测队列的内存序无关；用最强的 `SeqCst` 让计数本身没有任何歧义，避免「测试工具」本身成为 bug 来源。

#### 4.2.4 代码实践

**实践目标**：体会「MPMC 不能断言顺序，只能断言计数」这件事。

**操作步骤**：

1. 打开 [tests/array_queue.rs:215-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L215-L249) 的 `mpmc` 测试。
2. 把消费者里的 `v[n].fetch_add(1, Ordering::SeqCst)` 暂时替换成 `println!("consumed {n}")`，运行 `cargo test --test array_queue mpmc -- --nocapture`。
3. 观察输出的 `consumed` 序列，你会看到它**不是**升序的（多个生产者交错）。
4. 改回 `fetch_add`，确认测试通过。

**需要观察的现象**：第 2 步打印出的消费顺序杂乱无章；但第 4 步计数校验依然通过——证明「顺序不可预测」与「不丢不重」是两回事，MPMC 测试只对后者负责。

**预期结果**：计数校验通过，每个 `n` 恰好被消费 `THREADS` 次。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SPSC 测试容量选 3 这么小，而不选 1000？

> **答案**：容量越小，生产者越快被填满、被迫等待消费者，制造出的「阻塞—唤醒」交错越多，越容易暴露竞态。容量 1000 时生产者几乎一路畅通，反而减少了关键交错的覆盖。

**练习 2**：`SegQueue` 的 `mpmc` 测试里，如果删掉最后的计数校验循环（`for c in v { assert_eq!(c.load(...), THREADS) }`），测试还能发现「丢数据」的 bug 吗？

> **答案**：基本不能。删掉后测试只是「推完、消费完」就结束，没有断言。即使队列丢了一半数据，消费者也只是在 `pop` 的自旋循环里多等或少等，不会失败。计数校验正是用来抓住「丢」和「重」的，是这套测试的灵魂。

---

### 4.3 ring_buffer 环形缓冲测试：force_push 覆盖语义

#### 4.3.1 概念说明

`force_push` 是 `ArrayQueue` 独有的「环形缓冲」操作：队列满时它**覆盖最旧元素**，并把被覆盖的旧值用 `Some(old)` 返回给调用者（详见 u2-l3）。这里有一个微妙的不变量要验证：**任何一个被推入的值，要么最终被消费者 `pop` 出去，要么被 `force_push` 覆盖时由生产者自己拿到（`Some`）——总之不能凭空消失，也不能被重复「处置」**。

测试的设计因此很巧妙：用**一个值被处置的总次数**来校验。每个值 `n` 的「被处置次数」= 被消费者 `pop` 到的次数 + 被生产者 `force_push` 退回（`Some(n)`）的次数。校验它恰好等于「期望次数」。

另外还有一个协调问题：消费者怎么知道「生产者都干完活了，可以退出了」？答案是用一个原子计数器 `t`（剩余生产者数），生产者每结束一个就 `fetch_sub`，消费者在 `t == 0 && q.is_empty()` 时才退出。

#### 4.3.2 核心流程

SPSC 环形缓冲（1 个生产者）：

```text
t = AtomicUsize::new(1)              // 1 个生产者
v = [0; COUNT]                       // 每个值的处置计数
生产者：for n in 0..COUNT {
            if let Some(old) = q.force_push(n) { v[old].fetch_add(1) }  // 覆盖了 old
        }
        t.fetch_sub(1);              // 我下班了
消费者：loop {
            if t==0 && q.is_empty() { break }
            while let Some(n) = q.pop() { v[n].fetch_add(1) }           // 消费了 n
        }
校验：for c in v { assert_eq!(c.load(), 1) }   // 每个值恰好被处置 1 次
```

为什么是 1 次？因为每个值 `n` 被**一个**生产者推入恰好 1 次，所以它被「消费 or 覆盖」的总次数必须是 1。

#### 4.3.3 源码精读

SPSC 环形缓冲测试：

- [tests/array_queue.rs:175-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L175-L213)：注意第 179 行 `t` 初值为 1（单生产者）；生产者用 `force_push`，拿到 `Some(n)` 时给 `v[n]` 计数（第 200-202 行）；消费者在 `t == 0 && q.is_empty()` 时退出（第 187 行）；最后第 210-212 行断言每个值恰好被处置 1 次。

MPMC 环形缓冲测试（每个值被 `THREADS` 个生产者各推一次）：

- [tests/array_queue.rs:251-294](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L251-L294)：与 SPSC 版结构相同，区别有二：`t` 初值改为 `THREADS`（第 256 行，因为有 `THREADS` 个生产者）；最后断言改为 `v[c] == THREADS`（第 292 行，因为每个值 `n` 被 `THREADS` 个生产者各推一次，期望被处置 `THREADS` 次）。

处置次数的等式可以写成（以 MPMC 为例，对每个值 `n`）：

\[
\#\text{pop}(n) \;+\; \#\text{force\_push 退回}(n) \;=\; \text{THREADS}
\]

这正是第 292 行断言的数学含义。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`force_push` 不丢数据」这一不变量。

**操作步骤**：

1. 阅读 [tests/array_queue.rs:251-294](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L251-L294) 的 `mpmc_ring_buffer`。
2. 运行 `cargo test --test array_queue mpmc_ring_buffer`。
3. 想一个改造实验：把生产者的 `if let Some(n) = q.force_push(i) { v[n].fetch_add(1, ...) }` 故意改成 `let _ = q.force_push(i);`（即丢弃被覆盖的旧值，不计入 `v`），再跑测试。

**需要观察的现象**：第 2 步通过；第 3 步**失败**——因为被覆盖的值没被计入 `v`，它们最终的 `v[n]` 会小于 `THREADS`。

**预期结果**：第 3 步会看到形如 `assertion failed: left == right ... left: 3, right: 4` 的错误（具体数值取决于覆盖次数）。这反向证明了「计数」正是用来抓住静默丢失的。

> 注：第 3 步的具体失败数值与调度有关，属正常现象，关键是「测试会失败」这一点。

#### 4.3.5 小练习与答案

**练习 1**：消费者的退出条件为什么必须是 `t == 0 && q.is_empty()` 两个条件「同时」满足，缺一不可？

> **答案**：只看 `t == 0` 不够——生产者虽已全部结束，但队列里可能还有未消费的残留元素，提前退出会漏掉它们（校验失败）。只看 `q.is_empty()` 也不够——生产者可能还在跑，此刻为空只是暂时，消费者退出后生产者又推入的新值就再没人消费了。两者同时成立才表示「没人再生产了，且队列也清空了」。

**练习 2**：`SegQueue` 没有 `force_push`，所以 `tests/seg_queue.rs` 里没有 `ring_buffer` 测试。如果硬要给 `SegQueue` 写一个类似的「环形缓冲」测试，会遇到什么语义障碍？

> **答案**：`SegQueue` 是无界的，`push` 永远不会「满」、永远不会覆盖旧值。所以「被覆盖的旧值」这件事根本不存在，处置次数等式里的第二项恒为 0，测试退化为普通的 mpmc 计数测试，没有新增覆盖面。这也说明 `ring_buffer` 测试是专门为有界 + `force_push` 语义设计的。

---

### 4.4 drops 析构计数测试：验证内存不泄漏、不重复释放

#### 4.4.1 概念说明

`ArrayQueue` 和 `SegQueue` 内部都用 `MaybeUninit<T>` 存值（见 u4-l3），靠手写的 `unsafe` 在 push 时初始化、pop 或 Drop 时释放。这里有两类致命 bug：

- **内存泄漏**：某个值被 pop 走或被 Drop 时，没有真正调用 `T::drop`，析构次数偏少。
- **重复释放**：同一个值被 drop 了两次（use-after-free / double-free），析构次数偏多。

`drops` 测试用一个「自增计数器」类型的元素同时监控这两类 bug：定义一个 `DropCounter`，它的 `Drop::drop` 里对一个全局原子 `DROPS` 做 `fetch_add(1)`。于是 `DROPS` 的最终值就是「真正发生的析构次数」。把它和「理应发生的析构次数」比对——偏少 = 泄漏，偏多 = 重复释放。

#### 4.4.2 核心流程

```text
static DROPS: AtomicUsize = 0
struct DropCounter; impl Drop { DROPS.fetch_add(1); }

每个 run（循环 runs 次，用 fastrand 随机化规模）：
    DROPS.store(0);
    并发：生产者推 steps 个 DropCounter，消费者 pop steps 个
    （ArrayQueue：push 失败时，被退回的 DropCounter 会在表达式结束时
                  被立刻 drop，产生「虚假」计数，要用 fetch_sub(1) 补偿）
    断言 DROPS == steps;                 // 并发期间：推了 steps 个、消费了 steps 个
    再主线程补推 additional 个；          // 留在队列里没消费
    断言 DROPS == steps;                 // 仍应是 steps（additional 还没被 drop）
    drop(q);                             // 整个队列析构，应释放剩余 additional 个
    断言 DROPS == steps + additional;    // 全部析构完毕
```

两个关键校验点：**`drop(q)` 之前** `DROPS == steps`（并发期间恰好析构了被消费的那批），**`drop(q)` 之后** `DROPS == steps + additional`（Drop 实现把残留的也析构了）。

#### 4.4.3 源码精读

`ArrayQueue` 的 `drops` 测试（有补偿版）：

- [tests/array_queue.rs:296-347](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L296-L347)：重点看第 329-335 行的生产者——`while q.push(DropCounter).is_err() { DROPS.fetch_sub(1, Ordering::SeqCst); }`。每次构造 `DropCounter` 都是一个新对象；如果 `push` 失败（队列满），`Err(DropCounter)` 这个临时值在表达式结束时被 drop，`DROPS` 会 `+1`，但这只是「重试造成的虚假析构」，并非「真正进入队列的元素」，所以用 `fetch_sub(1)` 把它扣回去，保持 `DROPS` 只统计「成功入队又被消费」的析构。

`SegQueue` 的 `drops` 测试（无需补偿版）：

- [tests/seg_queue.rs:164-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L164-L213)：生产者（第 197-202 行）是 `for _ in 0..steps { q.push(DropCounter); }`——`SegQueue::push` 返回 `()` 不会失败，所以不存在「虚假析构」，也就不需要 `fetch_sub` 补偿。这是两个 `drops` 测试唯一的实质差异，根源还是「有界会失败 vs 无界不会失败」。

随机化规模（用 `fastrand`，[Cargo.toml:42-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L42-L43) 声明的 dev-dependency）：

- [tests/array_queue.rs:313-320](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L313-L320)：`fastrand::Rng::new()` 配合 `rng.usize(0..steps)` 让每个 run 的 `steps` / `additional` 都随机，从而覆盖「队列空、半满、全满、Drop 时残留不同数量」等多种状态，而不是固定的几个边界。这是用随机化弥补「无法枚举所有交错」的常见手段。

#### 4.4.4 代码实践

**实践目标**：用 `drops` 测试同时抓「泄漏」和「重复释放」。

**操作步骤**：

1. 运行 `cargo test --test array_queue drops` 与 `cargo test --test seg_queue drops`，确认通过。
2. 阅读源码，理解三处断言的用意：[array_queue.rs:343-345](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L343-L345)（`steps`、`drop(q)`、`steps + additional`）。
3. 思考实验（不必改源码）：假设 `SegQueue` 的 `Drop` 实现里漏掉了某个块的回收（即 u3-l4 讲的 Drop 遍历少释放了一块），上面哪个断言会失败？
4. 再思考：假设某个槽被 pop 时错误地 `read` 了两次（重复释放），哪个断言会失败？

**需要观察的现象**：本实践为「源码阅读型」，不改源码、不运行改造版。重点是能**预测**第 3、4 步的失败位置。

**预期结果**：

- 第 3 步（泄漏，少析构）：第二个断言 `DROPS == steps + additional` 失败，实际值**偏小**。
- 第 4 步（重复释放，多析构）：第一个或第二个断言失败，实际值**偏大**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DROPS` 用 `static AtomicUsize` 而不是 `Mutex<usize>`？

> **答案**：`Drop::drop` 会在极高频率下被多个线程同时调用（并发 pop/Drop）。`AtomicUsize::fetch_add` 是无锁的、几纳秒完成；`Mutex` 会序列化所有析构、还可能死锁（若 `T::drop` 内部再触发别的锁）。这里只需「计数」，原子操作正合适。

**练习 2**：`ArrayQueue` 版的 `fetch_sub(1)` 补偿，如果忘了写，`DROPS == steps` 这个断言会偏向哪边？

> **答案**：会**偏大**（失败时 `DROPS > steps`）。因为每次失败的 `push` 重试都会让 `DropCounter` 构造又被 drop，多算了一次。补偿正是为了扣掉这些重试噪声。

---

### 4.5 linearizable 可线性化测试

#### 4.5.1 概念说明

回顾第 2 节的直觉：可线性化让我们「假装」并发队列是个顺序队列。`linearizable` 测试把这个想法编码成了一段压力测试：多个线程同时混合 `push` / `force_push` / `pop`，每个线程在「刚加入一个元素」之后立刻「取走一个元素」，并用 `.unwrap()` 断言「取走这一步必然成功」。

这个「必然成功」之所以成立，依赖的正是队列的**正确性**——如果队列真的提供了可线性化的 FIFO 语义，那么「我刚加了一个元素、全局账面此时至少有我这一个名额外加别人非负的余额，所以我必定能取到一个」这个推理就成立。反之，如果实现有 bug（丢元素、复制元素、破坏 FIFO），这个推理会被打破，`.unwrap()` 就会在压力下 panic。

要强调的是：这是一个**经验性压力测试（stress test）**，不是形式化的可线性化检查。它跑很多次、很多线程，**提高**暴露 bug 的概率，但**不能保证**穷尽所有交错。真正穷尽地验证弱内存模型下的可线性化，要靠 **miri**（多种子）和 **Loom**（枚举所有交错）——这正是 u4-l1 末尾提到的要点。

#### 4.5.2 核心流程

```text
q = ArrayQueue::new(THREADS)          // 容量 = 线程数
THREADS/2 组线程对，每对两种角色：
  角色 A（push/pop）：for COUNT 次 {
        重试直到 push(0) 成功；        // 加入一个
        q.pop().unwrap();              // 取走一个，断言必成功
  }
  角色 B（force_push/pop）：for COUNT 次 {
        if q.force_push(0).is_none() { // 队列没满→确实加入了一个
            q.pop().unwrap();          // 取走一个，断言必成功
        }                              // 队列满了→force_push 覆盖旧值，净增 0，不 pop
  }
无最终断言——bug 体现为运行中的 panic
```

每个线程都是「净增 0」（先加后减），所以队列容量始终在 `0..=THREADS` 之间波动；`force_push` 的分支让测试同时覆盖「正常加入」「满时覆盖」两条路径。

#### 4.5.3 源码精读

- [tests/array_queue.rs:349-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L349-L375)：第 354 行 `ArrayQueue::new(THREADS)` 把容量设成线程数，刻意让队列容易填满，从而频繁触发 `force_push` 的覆盖路径（角色 B 的 `is_none()` 为假的分支）。第 357-372 行循环 `THREADS / 2` 次，每次 `spawn` 两个线程（角色 A 与角色 B）。两处 `.unwrap()`（第 361、368 行）就是「可线性化不变量」的化身。

> **关于「为什么 `.unwrap()` 不会误伤」**：直觉上，每个 `.unwrap()` 之前，调用它的线程都刚完成了一次「净增一个元素」的写入（`push` 成功，或 `force_push` 返回 `None`）。在一个**正确的**可线性化 FIFO 队列里，这一刻全局「已成功写入但尚未被取走」的元素数至少为 1（自己刚写的那个，加上别的线程非负的余额），所以本次 `pop` 必能取到一个。这套推理只有在队列**破坏了可线性化或 FIFO** 时才会失效——而这正是测试想抓的 bug。完整的严格证明涉及 force_push 覆盖带来的精细账目，超出了「测试阅读」的范围；对实现正确性的最终担保仍由 miri/Loom 给出。

#### 4.5.4 代码实践

**实践目标**：体会「`.unwrap()` 即断言」，并理解压力测试的局限性。

**操作步骤**：

1. 运行 `linearizable` 测试多次：
   ```bash
   for i in $(seq 1 20); do cargo test --test array_queue linearizable; done
   ```
2. 观察它从不 panic（在正确实现下）。
3. 阅读源码，确认它「没有任何末尾断言」——所有断言都以 `.unwrap()` 的形式内嵌在并发循环里。
4. 思考：如果要把它升级成「真正的」可线性化检查，应该怎么做？

**需要观察的现象**：20 次运行全部通过；测试函数体内确实没有显式的 `assert_eq!`。

**预期结果**：全部通过。第 4 步的思考结论应是「引入一个记录所有操作调用/返回历史的模型，再用线性化检查器（如 Loom 的模型、或 Lincheck 风格的工具）枚举交错去比对」——这超出了本测试的范围。

> 注：本测试**没有** `cfg!(miri)` 缩放——这是一个疏忽或有意为之的取舍，留给读者在练习中讨论。

#### 4.5.5 小练习与答案

**练习 1**：`linearizable` 测试没有 `cfg!(miri)` 来缩小 `COUNT`（[第 351 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L351) 是 `if cfg!(miri) { 100 } else { 25_000 }`，其实它**有**缩放——请重新核对）。请说明给这类压力测试加 miri 缩放的意义。

> **答案**：（更正题干）该测试**确实**在第 351 行用了 `const COUNT: usize = if cfg!(miri) { 100 } else { 25_000 };` 做了缩放。意义在于：miri 下跑 25_000 次嵌套并发循环会极慢甚至超时；缩到 100 次既能保留「混合 push/force_push/pop」的覆盖，又让 miri 在可接受时间内跑完，从而让 miri 也能为这个测试做 UB 检查。

**练习 2**：为什么这个测试叫 `linearizable`，却「只是」一个压力测试，而不是形式化的线性化检查？

> **答案**：因为它没有显式构造「操作历史」、也没有运行线性化检查算法去验证「存在合法的顺序化」。它只是把「可线性化成立时才必然为真」的性质（`pop` 必成功）编码成 `.unwrap()`，靠高频运行去**概率性**地撞出 bug。要形式化验证，需要记录每次 `push`/`pop` 的调用与返回时刻，再用检查器在所有可能的线性化序里找合法解——这是 Loom/模型检查的工作，超出了集成测试的范畴。

---

## 5. 综合实践

把本讲的几条主线串起来，写一个**全新的** MPMC 测试，综合运用「scope 并发骨架 + 计数向量 + cfg!(miri) 缩放」。

**任务**：参照 [tests/array_queue.rs:215-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L215-L249) 的 `mpmc` 测试，写一个 `mpmc_unique` 测试：

- **4 个生产者**，每个生产者推入 **1000 个唯一 id**（让 id 在所有生产者之间也唯一，例如生产者 `p` 推 `p * 1000 .. p * 1000 + 1000`，共 4000 个互不相同的 id）。
- **4 个消费者**，每个消费者循环 `pop`，用 `AtomicUsize` 计数向量 `v[id]` 记录每个 id 被消费的次数。
- 消费者总数 = 4000，每个消费者线程消费 1000 个后退出。
- 测试结束后断言：**每个 id 恰好被消费 1 次**（`v[id] == 1`）。
- 加上 `cfg!(miri)` 缩放：miri 下生产者数/每者 id 数都缩小（例如 2 个生产者、各 50 个 id）。

**参考骨架**（示例代码，待本地验证）：

```rust
#[test]
fn mpmc_unique() {
    const PRODUCERS: usize = 4;
    const PER_PRODUCER: usize = if cfg!(miri) { 50 } else { 1_000 };
    const TOTAL: usize = PRODUCERS * PER_PRODUCER;

    let q = ArrayQueue::<usize>::new(8); // 有界，制造背压；也可换 SegQueue
    let v = (0..TOTAL).map(|_| AtomicUsize::new(0)).collect::<Vec<_>>();

    scope(|scope| {
        // 4 个消费者
        for _ in 0..PRODUCERS {
            scope.spawn(|_| {
                for _ in 0..PER_PRODUCER {
                    let id = loop {
                        if let Some(x) = q.pop() { break x; }
                    };
                    v[id].fetch_add(1, Ordering::SeqCst);
                }
            });
        }
        // 4 个生产者，每个推自己那段唯一 id
        for p in 0..PRODUCERS {
            scope.spawn(|_| {
                let base = p * PER_PRODUCER;
                for id in base..base + PER_PRODUCER {
                    while q.push(id).is_err() {} // ArrayQueue 满了重试；SegQueue 改为 q.push(id)
                }
            });
        }
    }).unwrap();

    for c in v {
        assert_eq!(c.load(Ordering::SeqCst), 1); // 每个 id 恰好被消费一次
    }
}
```

**操作步骤**：

1. 把上面的测试加入 `tests/array_queue.rs`（或新建一个测试文件并 `use crossbeam_queue::ArrayQueue; use crossbeam_utils::thread::scope;`）。
2. 运行 `cargo test --test array_queue mpmc_unique`。
3. 把 `ArrayQueue` 换成 `SegQueue`（注意 `push` 不返回 `Result`，要去掉 `while ... is_err()`），再跑一次，对照两者。
4. 用 `cargo +nightly miri test --test array_queue mpmc_unique` 跑 miri 版（规模已自动缩小）。

**需要观察的现象与预期结果**：第 2、3 步均通过，每个 id 恰好被消费 1 次；第 4 步 miri 不报告任何 UB。如果改成「每个 id 被推两次」（破坏唯一性），断言会以 `left: 2, right: 1` 失败——这正是计数向量抓「重复」的能力。

> 待本地验证：第 4 步是否需要先安装 miri 组件（`rustup +nightly component add miri`）。

## 6. 本讲小结

- crossbeam-queue 用**多范式**测试组合逼近并发正确性：`spsc` 验顺序、`mpmc`/`ring_buffer` 验不丢不重、`drops` 验析构、`linearizable` 验可线性化。
- 所有并发测试都建在 `crossbeam_utils::thread::scope` 上——它让子线程借用局部队列、自动 join，并把子线程的 panic 聚合给 `.unwrap()`。
- **SPSC** 可直接断言消费序 == 生产序；**MPMC** 无法断言顺序，改用计数向量断言「每个值被消费的次数 == 它被生产的次数」。
- **ring_buffer** 测试用「处置次数 = 被 pop 次数 + 被 force_push 覆盖退回次数」的等式，专验 `force_push` 不静默丢数据；用原子计数器 `t` 协调生产者退出与消费者终止。
- **drops** 测试用一个 `Drop` 时自增的全局原子 `DROPS`，**同时**抓泄漏（析构偏少）与重复释放（析构偏多）；`ArrayQueue` 版还需对失败的 `push` 重试做 `fetch_sub` 补偿。
- **linearizable** 是压力测试而非形式化检查——它把「可线性化成立时才必然为真」的性质编码成 `.unwrap()`，靠高频运行概率性撞 bug；穷尽验证留给 miri/Loom。
- `cfg!(miri)` 用编译期 `const` 把测试规模缩成「miri 小档 / 原生大档」，让 miri 也能在有限时间内核查 `unsafe`。

## 7. 下一步学习建议

- **接着学 u4-l5（no_std / alloc）**：本讲的测试都依赖 `std`（`std::sync::atomic`、`thread`），下一讲会讲 crate 如何在 `no_std + alloc` 下工作，是「环境」维度的最后一篇。
- **动手把测试范式迁移到 Loom**：如果想体验「真正的」可线性化/穷尽交错验证，可以在一个独立的小项目里用 `loom` crate 重写 `mpmc` 测试，对比「随机压力」与「枚举交错」的差异（u4-l1 末尾提到了 Loom）。
- **重读 u4-l3 的 unsafe 论证**：现在你已经看到 `drops` 测试如何为那些 `unsafe` 提供「运行时兜底」，再回去看 `assume_init_drop` / `MaybeUninit::read` 的 SAFETY 论证，会有更具体的体感。
- **阅读 crossbeam-utils 的 `scope` 实现**：本讲只把 `scope` 当工具用；想深入「作用域线程如何安全地借用局部变量」，可以去读 `crossbeam-utils/src/thread.rs`。
