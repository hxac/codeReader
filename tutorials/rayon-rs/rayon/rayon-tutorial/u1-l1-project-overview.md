# 项目全景：Rayon 是什么

> 所属单元：单元一「初识 Rayon」 · 讲义编号 u1-l1 · 难度：入门
> 代码永久链接基于 HEAD `ee0a00bdb1ab039e178a215ad5712fb7fa58e58f`。

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一两句话说清 Rayon 的定位：一个**数据并行（data-parallelism）**库，基于**工作窃取（work stealing）**调度，并保证**无数据竞争（data-race freedom）**。
2. 区分 Rayon 的四类核心 API——并行迭代器、`join`、`scope`、`ThreadPool`——各自的适用场景。
3. 解释 README 中那句 "if your code compiles, it typically does the same thing it did before"（如果代码能编译，行为通常和串行版一致）背后的含义与边界。

本讲不要求你已经读过任何 Rayon 源码；我们只读两个文档文件（`README.md`、`FAQ.md`）和少量入口源码，先建立正确的"心智地图"，再动手跑第一个并行程序。

## 2. 前置知识

本讲用到的概念都在这里用通俗语言解释一遍。已经熟悉的读者可以跳过。

### 2.1 并发与并行

- **并发（concurrency）**：程序"有能力"同时处理多件事，比如一个 Web 服务器同时挂着 1000 个请求。
- **并行（parallelism）**：程序"实际上"同时用多个 CPU 核干活的情形，比如把一个数组的求和拆到 8 个核上同时算。

Rayon 关注的是后者：**把一个本来串行的计算拆到多个核上同时执行**，目标是更快地算完。

### 2.2 Rust 迭代器与闭包

Rust 的串行迭代器是这样的链式写法：

```rust
let sum: i32 = input.iter()      // 创建迭代器（此时什么都没算，惰性的）
    .map(|&i| i * i)             // 装上一个"变换"适配器（还是没算）
    .sum();                      // 消费者：真正开始逐个取元素计算
```

`|&i| i * i` 是闭包（匿名函数）。Rayon 的 API 几乎是这套写法的并行镜像，所以熟悉串行迭代器的人会非常眼熟。

### 2.3 Send 与 Sync（先有个直觉）

- `Send`：一个类型的值**可以安全地搬到另一个线程**去用。
- `Sync`：一个类型的引用**可以安全地被多个线程同时拿**。

`Rc`、`Cell`、`RefCell` 不是 `Send`/`Sync` 的（它们没加锁，跨线程用会出事）；`Arc`、`AtomicUsize`、`Mutex` 是。这两个标记 trait 是 Rust 编译器在**编译期**检查并行安全的抓手，第 4.3 节会看到 Rayon 如何利用它们。

### 2.4 数据竞争与竞态条件（重要区别）

- **数据竞争（data race）**：两个线程同时读写同一块内存、至少一个是写、且没有任何同步。在 Rust 里这是**未定义行为**，可能直接崩溃或产出错乱数据。
- **竞态条件（race condition）**：更广义的"时序不对"，程序逻辑上依赖的事件顺序被打乱，但内存访问本身是同步过的、不是未定义行为。

Rayon 的承诺是消灭前者；后者（比如两个线程都读到旧值再各自写回）仍需要你自己用对原子操作。4.3 节会给出 FAQ 中的经典反例。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `README.md` | 项目门面：定位、第一个示例、四类 API 导览、依赖添加方式 | 本讲主教材 |
| `FAQ.md` | 官方问答：线程数规则、工作窃取原理、非 Send 类型的处理 | 本讲第二教材 |
| `src/lib.rs` | `rayon` crate 的库入口与总导览文档，re-export 各 API | 确认四类 API 的"户口" |
| `rayon-core/src/join/mod.rs` | `join` 原语的实现（属于底层 crate rayon-core） | 看 `join` 的函数签名 |
| `rayon-core/src/scope/mod.rs` | `scope` 的实现 | 看 `scope` 的函数签名 |

一个先记住的事实：仓库分为三层——上层 `rayon`（面向使用者的并行迭代器等）、底层 `rayon-core`（线程池与调度内核）、`rayon-demo`（演示程序）。上层通过 `pub use rayon_core::...` 把底层能力重新导出，所以你平时 `use rayon::prelude::*` 一句话就能拿到全部 API。本讲只在入口层看几眼，细节留给后续讲义。

## 4. 核心概念与源码讲解

### 4.1 Rayon 是什么

#### 4.1.1 概念说明

Rayon 是 Rust 的一个**数据并行库**，官方自我介绍有三个关键词：

1. **极轻量（extremely lightweight）**：接入成本低，通常只改一行代码。
2. **串行转并行极其容易**：把 `foo.iter()` 改成 `foo.par_iter()`，剩下的事 Rayon 管。
3. **保证无数据竞争**：这不是"我们测试过没发现问题"式的保证，而是由类型系统在编译期强制出来的（见 4.3）。

名字的来历：工作窃取技术最早来自 90 年代 MIT 的 **Cilk** 项目，"Rayon"（人造丝）是对 Cilk（丝绸）的致敬——FAQ 里明确写了这一段。

为什么需要它？手写多线程程序要自己管线程数、切分数据、汇总结果、避免竞争，代码量大且容易错。Rayon 把这三件事全部包掉，尤其在**任务切分**上采用运行期自适应策略：数据怎么拆、拆多细，取决于当时的机器负载，而不是写死在代码里。

#### 4.1.2 核心流程

Rayon 的底层调度叫**工作窃取**。按 FAQ 的描述，一次 `join(a, b)` 的执行协议是：

```text
线程 W 调用 join(a, b)：
1. 把 b 包装成任务，放进 W 自己的工作队列（对外"广告"：这活儿别人可以拿）
2. W 自己开始执行 a
3. 其他空闲线程可能从 W 的队列里把 b 拿走执行 —— 这叫"窃取"(steal)
4. a 做完后，W 检查 b 是否被偷走：
   - 被偷了 → 等那个线程做完
   - 没被偷 → W 自己把 b 做掉
5. W 自己的队列空了 → 去翻别的线程的队列，尝试偷活儿
```

这个协议的妙处：

- **不拆也有并行**：如果别的线程都忙，W 顺序做完 a、b，开销近似串行，不亏。
- **拆了就赚**：只要有空闲线程，任务天然被"偷"过去，不需要显式的任务分配器。
- **自动负载均衡**：快的工作线程会主动去偷慢的线程的活。

理论上这是 Cilk 系调度器的经典结论（Blumofe–Leiserson 工作窃取定理）：设总工作量为 \( W \)、关键路径长度（span）为 \( S \)、线程数为 \( P \)，则随机工作窃取调度的期望运行时间满足

\[ T_P \;\le\; \frac{W}{P} + O(S) \]

直觉解读：只要你的程序"可拆分的并行度"够大（\( S \) 相对 \( W \) 很小），就能逼近理想加速比 \( W/P \)。这解释了为什么 Rayon 敢说"动态适配最大性能"。

#### 4.1.3 源码精读

定位段落——README 开头一句话给出三个关键词（轻量、易转换、无数据竞争）：

- [README.md:L8-L15](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L8-L15)：Rayon 的自我定位：数据并行库、轻量、易把串行计算转并行、保证无数据竞争；同时给出了背景博客和演讲视频的链接。

著名的 "just change that" 示例，整个库最核心的卖点就是这一行注释：

- [README.md:L26-L33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L26-L33)：`sum_of_squares` 函数——`input.par_iter()`（注释写着 "just change that!"）→ `.map(|&i| i * i)` → `.sum()`。与串行版的唯一区别是把 `iter()` 换成 `par_iter()`。

- [README.md:L35-L45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L35-L45)：这段说明了 API 的层次递进——并行迭代器自动决定怎么切数据；不够用就用 `join`、`scope` 自己造任务；还要更多控制就自建线程池。这就是 4.2 节四类 API 的官方出处。

工作窃取的完整官方描述在 FAQ：

- [FAQ.md:L17-L36](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L17-L36)：FAQ 用通俗语言完整描述了 `join` 的工作窃取协议（b 入队 → W 执行 a → 其他线程偷 b → a 完成后 W 检查 b 是否被偷），并说明该技术源自 MIT 的 Cilk 项目，Rayon 之名即为致敬。4.1.2 的伪代码就是照这段整理的。

- [FAQ.md:L5-L15](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L5-L15)：默认线程数 = 可用 CPU 数（开启超线程时是逻辑核数而非物理核数）；可用环境变量 `RAYON_NUM_THREADS` 或 `ThreadPoolBuilder::build_global` 修改。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 README 的第一个例子，再手写一个等价的手动分线程版本，直观体会"轻量"到底轻在哪。

**操作步骤**：

1. 新建项目：

   ```bash
   cargo new rayon-first
   cd rayon-first
   ```

2. 在 `Cargo.toml` 中添加依赖（README 推荐写法）：

   ```toml
   [dependencies]
   rayon = "1.12"
   ```

3. 把 `src/main.rs` 改成下面这样。前半段是 README 示例的逐字抄写；后半段 `sum_of_squares_manual` 是**示例代码**（本讲义编写，非项目源码），用标准库 `std::thread::scope` 手动实现同样的功能：

   ```rust
   use rayon::prelude::*;
   use std::thread;

   // ↓↓↓ 逐字抄自 README.md L26-L33 ↓↓↓
   fn sum_of_squares(input: &[i32]) -> i32 {
       input.par_iter() // <-- just change that!
            .map(|&i| i * i)
            .sum()
   }

   // ↓↓↓ 示例代码：手动并行版，功能等价 ↓↓↓
   fn sum_of_squares_manual(input: &[i32]) -> i32 {
       let nthreads = thread::available_parallelism()
           .map(|n| n.get())
           .unwrap_or(1);
       // 把切片尽量均分成 nthreads 段（向上取整，且至少为 1 防止空输入）
       let chunk = input.len().div_ceil(nthreads).max(1);
       let mut sums = vec![0i32; nthreads];
       thread::scope(|s| {
           for (t, part) in input.chunks(chunk).enumerate() {
               let out = &mut sums[t];
               s.spawn(move || {
                   *out = part.iter().map(|&i| i * i).sum();
               });
           }
       }); // scope 结束时隐式 join 所有线程
       sums.iter().sum()
   }

   fn main() {
       // 注意：用 0..1000 是为了结果能放进 i32（更大范围会溢出）
       let input: Vec<i32> = (0..1000).collect();
       let expected: i32 = input.iter().map(|&i| i * i).sum();

       assert_eq!(sum_of_squares(&input), expected);
       assert_eq!(sum_of_squares_manual(&input), expected);
       println!("三个版本结果一致：{}", expected);
   }
   ```

4. 运行：

   ```bash
   cargo run
   ```

**需要观察的现象**：

- 程序正常打印 `三个版本结果一致：332833500`（0 到 999 的平方和）。
- 数一数两个函数的行数与概念负担：Rayon 版 3 行有效代码；手动版要自己决定线程数、自己切分、自己开数组收每段的部分和、自己汇总。README 说的"extremely lightweight"在这里变成具体感受。
- 手动版把切分策略**写死**在编译期（均分 nthreads 段）；而 Rayon 是运行期按负载自适应切分的（本例数据太小看不出差别，大量数据的对比放在第 5 节综合实践）。

**预期结果**：断言全部通过。**待本地验证**：具体打印数值请实际运行确认（应为 332833500）。

#### 4.1.5 小练习与答案

**练习 1**：把示例中的 `input` 换成 `(0..1_000_000)` 的 `Vec<i32>` 会发生什么？为什么？

<details><summary>参考答案</summary>

会得到错误的值（在 release 下可能静默溢出，debug 下 panic）。平方和 \( \sum_{i=0}^{n-1} i^2 = \frac{(n-1)n(2n-1)}{6} \)，\( n = 10^6 \) 时约 \( 3.3\times 10^{17} \)，远超 `i32` 上限（约 \( 2.1\times 10^9 \)）。这正是 README 提醒的：并行版的**数值结果**应与串行版一致，但类型系统的数据竞争检查管不了算术溢出——那是你的责任。
</details>

**练习 2**：FAQ 说 Rayon 默认开多少线程？这个数字在你的机器上等于什么？

<details><summary>参考答案</summary>

默认等于可用 CPU 数；在开启超线程的机器上是**逻辑核**数而非物理核数（[FAQ.md:L5-L15](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L5-L15)）。在 Linux 上可以用 `nproc` 对照验证。
</details>

**练习 3**：用自己的话说出"窃取（steal）"在这个协议里指什么，谁偷谁？

<details><summary>参考答案</summary>

调用 `join(a, b)` 的线程 W 把 b 放进自己的队列后先去执行 a；此时**其他空闲的工作线程**从 W 的队列里把 b 取走执行，这个动作叫窃取。方向永远是"别的线程偷我的队列里的任务"，而 W 自己取任务用的是队列的另一端（本地快、远程慢，这是后续 u5-l4 的主题）。
</details>

### 4.2 四类核心 API 概览

#### 4.2.1 概念说明

Rayon 的 API 按控制粒度从粗到细排成四级台阶。README 用一段话把它们串了起来，我们先给一张总表：

| API | 典型入口 | 适合场景 | 谁决定任务切分 |
| --- | --- | --- | --- |
| **并行迭代器** | `foo.par_iter()`、`foo.into_par_iter()` | 数据已在容器/迭代器里，按元素流水线处理（map/filter/sum/collect…） | Rayon 运行期自适应 |
| **join** | `rayon::join(\|\| a(), \|\| b())` | 手写分治：把一个任务显式劈成两半并行执行 | 你（劈成两半），内部的再劈交给 Rayon |
| **scope** | `rayon::scope(\|s\| { s.spawn(\|\| ...) })` | 一次派发**任意数量**的任务，且任务能**借用栈上数据** | 你 |
| **ThreadPool** | `ThreadPoolBuilder::new().num_threads(2).build()` | 需要独立或定制的线程池（隔离负载、指定线程数/名字/栈大小） | 不适用（这是池级配置） |

选择的心法：**从上往下试**。能用并行迭代器一行解决就不动别的；需要自己控制"怎么分"时降级到 `join`；需要在任务里借栈上变量或动态产生任务时用 `scope`；要资源隔离或定制线程属性时建自己的 `ThreadPool`。

在这四级之外，rayon-core 还提供 `spawn`（fire-and-forget 丢任务）、`broadcast`（给每个线程广播一个任务副本）等派生 API，它们都建立在同一套线程池之上，留到单元六细讲。

#### 4.2.2 核心流程

面对"我想并行化一段代码"的决策流程：

```text
有一批数据要逐个处理？
├─ 是 → 数据在容器/迭代器里？
│        ├─ 是 → 并行迭代器：iter() 换 par_iter()，完事
│        └─ 否（是自定义分治结构，如树）→ join 递归，或 u3-l6 的 walk_tree
├─ 不是一批数据，是"几件独立的事"？
│        ├─ 恰好两件 → join
│        ├─ 任意多件 / 需要借用栈上数据 → scope + s.spawn
│        └─ 丢出去不等结果 → spawn
└─ 担心和别的负载互相干扰 / 需要特定线程配置 → 自建 ThreadPool，用 install 执行
```

一个容易混淆的点：`join(a, b)` **不是**"开两个新线程"。它把 b 变成可被窃取的任务、当前线程立刻开始做 a——具体几个线程参与，取决于当时池里谁闲着。

#### 4.2.3 源码精读

库入口的文档注释就是官方的 API 分类法：

- [src/lib.rs:L8-L26](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L8-L26)：`rayon` crate 的模块级文档把用法分成两大类——"High-level parallel constructs"（并行迭代器、`par_sort`、`par_extend`）和 "Custom tasks"（`join`、`scope`、`ThreadPoolBuilder`）。4.2.1 的总表就是照这段整理的。

上层 crate 如何把底层 rayon-core 的能力重新导出，一眼看全：

- [src/lib.rs:L107-L118](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L107-L118)：一连串 `pub use rayon_core::...`——`ThreadPool`/`ThreadPoolBuilder` 在 L109-L111，`scope` 家族在 L113，`join`/`join_context` 在 L117，`spawn` 在 L118。这就是"你 `use rayon::prelude::*` 能用到这一切"的物理来源。

`join` 的真实签名（注意约束全是 `Send`，4.3 节会回来细讲）：

- [rayon-core/src/join/mod.rs:L93-L99](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L93-L99)：`pub fn join<A, B, RA, RB>(oper_a: A, oper_b: B) -> (RA, RB)`，要求两个闭包 `FnOnce() + Send`、两个返回值 `Send`，**同时返回两个结果** `((RA, RB))`——这是它和 fire-and-forget 的 `spawn` 最大的区别。

`scope` 的签名：

- [rayon-core/src/scope/mod.rs:L277-L281](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L277-L281)：`pub fn scope<'scope, OP, R>(op: OP) -> R`，闭包拿到一个 `&Scope<'scope>`，往上面 `spawn` 的任务可以借用 `'scope` 生命周期内的栈上数据；`scope` 返回前保证所有派生任务已完成。

#### 4.2.4 代码实践

**实践目标**：在同一项目里用一次 `join`，并通过线程编号观察工作窃取是**运行期**决策。

**操作步骤**：

1. 在 4.1.4 的项目里新建 `src/bin/join_probe.rs`（`cargo new` 的项目支持 `src/bin/` 下的多个二进制）：

   ```rust
   // 示例代码（本讲义编写）
   use rayon::current_thread_index;

   fn main() {
       let (a, b) = rayon::join(
           || {
               println!("a 在线程 {:?} 上执行", current_thread_index());
               10
           },
           || {
               println!("b 在线程 {:?} 上执行", current_thread_index());
               20
           },
       );
       println!("join 返回 ({a}, {b})");
   }
   ```

2. 运行若干次：

   ```bash
   cargo run --bin join_probe
   cargo run --bin join_probe
   cargo run --bin join_probe
   ```

**需要观察的现象**：

- `current_thread_index()` 返回 `Option<usize>`：工作线程返回 `Some(编号)`，普通线程（比如 main 线程）返回 `None`。
- `a` 通常打印 `None`——因为调用 `join` 的线程自己会先执行 `a`（正是 4.1.2 协议的第 2 步）；`b` 有时在 `Some(i)`（被某个工作线程偷走），有时也在调用线程上执行（没人偷时调用线程自己收尾）。
- 多次运行的线程分布**可能不同**：切分决策是运行期根据负载做的，不是固定的。

**预期结果**：一定打印 `join 返回 (10, 20)`（返回值顺序与传入顺序一致，与谁先执行无关）；线程编号分布**待本地验证**，不同机器/负载下观察到的组合可能不同。

#### 4.2.5 小练习与答案

**练习 1**：想统计 `HashMap` 里所有值的总和，选哪级 API？想实现并行快速排序呢？

<details><summary>参考答案</summary>

统计求和：并行迭代器，`map.par_iter().map(|(_, v)| v).sum::<i32>()` 一行。并行快排：典型的分治结构，先 `join` 递归切两半；不过若是排 `&mut [T]`，直接用现成的 `par_sort`（[src/lib.rs:L19](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L19)）更好，u8-l2 会精读它。
</details>

**练习 2**：`join` 和 `spawn` 都是"把闭包交给线程池"，核心区别是什么？

<details><summary>参考答案</summary>

`join` **同步**等待两个闭包都完成并**返回它们的结果**（签名返回 `(RA, RB)`，见 [rayon-core/src/join/mod.rs:L93-L99](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L93-L99)）；`spawn` 是 fire-and-forget，不返回值、不等待（因此闭包必须是 `'static` 的，u6-l2 细讲）。需要结果就用 `join`/`scope`，只想丢活儿就用 `spawn`。
</details>

**练习 3**：为什么说 `scope` 比 `spawn` "更安全地灵活"？

<details><summary>参考答案</summary>

`spawn` 的闭包必须是 `'static`（ Owned 数据），不能借用局部变量；而 `scope` 借生命周期参数 `'scope` 保证：作用域返回前所有任务结束，因此任务闭包可以安全地**借用栈上数据**（[rayon-core/src/scope/mod.rs:L277-L281](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/scope/mod.rs#L277-L281)）。等待语义由类型系统兜底，不需要手写 join 计数。
</details>

### 4.3 数据竞争自由保证

#### 4.3.1 概念说明

README 的承诺原文是：Rayon 的所有 API 都保证 **data-race freedom**，"if your code compiles, it typically does the same thing it did before"。

拆成两半理解：

1. **"无数据竞争"是编译期保证**。机制：Rayon 的每个并行入口（`join`、`scope::spawn`、迭代器的闭包…）都要求闭包与产出 `Send`。`Send` 意味着闭包捕获的所有数据都能安全跨线程。你若捕获了 `Rc`、`RefCell` 或同时 `&mut` 同一变量，编译直接失败——错误发生在你写代码时，而不是凌晨三点的生产环境。
2. **"通常行为一致"有边界**。保证的是**结果一致**，不是**副作用顺序一致**。若闭包里有副作用（往 channel 发消息、写文件、打印），这些副作用的**先后顺序**在并行版里不保证。另外一些并行版方法（如 `find_any`）有意牺牲顺序换性能。

还要记住 2.4 节的区分：无数据竞争 ≠ 无竞态条件。FAQ 里那个"两个线程都先 load 后 store、各加一次只生效一次"的计数器，没有任何数据竞争，但结果照样错。类型系统帮你挡住未定义行为；**逻辑正确性**（原子操作选得对不对）仍是你的责任。

#### 4.3.2 核心流程

类型系统如何把"竞争"变成编译错误，以 `join` 为例：

```text
你写 rayon::join(|| ..., || ...)
        │
        ▼
签名要求：A: FnOnce() -> RA + Send，B 同理，RA/RB: Send
（rayon-core/src/join/mod.rs L93-L99）
        │
        ▼
编译器检查每个闭包捕获的所有变量的类型是否都 Send
        │
        ├── 都 Send            → 编译通过 → 运行期无数据竞争（该保证由类型系统背书）
        ├── 捕获了 Rc/RefCell  → E0277：`Rc<...> cannot be sent between threads safely`
        └── 两个闭包都 &mut x  → 借用检查失败：可变借用冲突
```

所以那句口号的准确翻译是：**Rayon 把最常见的整类并行 bug 从运行期挪到了编译期**。你付出的代价是偶尔要和借用检查器搏斗——FAQ 花了很长篇幅教你如何优雅地赢得这场搏斗（把 `Rc` 换 `Arc`、`Cell` 换原子类型、`RefCell` 换锁）。

#### 4.3.3 源码精读

承诺的原文：

- [README.md:L47-L63](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md#L47-L63)：**No data races** 一节：所有 API 保证数据竞争自由，"一般排除了大多数并行 bug（但不是全部）"；并明确给出边界——有副作用的迭代器副作用顺序可能不同。

FAQ 中的经典不可编译反例（本讲实践会亲手复现）：

- [FAQ.md:L38-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L38-L51)：`increment_all` 试图让两个闭包同时处理**同一个** `&mut [i32]`，编译失败；FAQ 给出替代方案——`Rc` 换 `Arc`、`Cell` 换 `AtomicUsize`、`RefCell` 换 `RwLock`/`Mutex`。

- [FAQ.md:L53-L63](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L53-L63)：非线程安全类型的替换对照表。

- [FAQ.md:L74-L100](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L74-L100)：**竞态条件（非数据竞争）反例**——把 `Cell` 的 get/set 换成 `AtomicUsize` 的 `load`/`store` 后，两个线程可能都读到 X、都写回 X+1，加两次只生效一次；FAQ 用双线程时序图演示，并指出正确做法是用 `fetch_add` 这类复合原子操作（[FAQ.md:L108-L111](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L108-L111)）。

- [FAQ.md:L185-L192](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L185-L192)：回答"Rust 不是应该让我不用想这些吗"——只要避免内部可变性（`Cell`/`RefCell`/原子类型/锁），类型系统确实让你完全不用想原子性；但当你**有意**让线程交错（如并行搜索共享当前最优解）时，就得认真对待，FAQ 给了 `Arc<AtomicUsize>` + `fetch_min` 的范式。

#### 4.3.4 代码实践

**实践目标**：亲眼看到一次"数据竞争被编译器拦截"，再修复它。

**操作步骤**：

1. 在项目里新建 `src/bin/race_fail.rs`，先原样抄 FAQ 的反例：

   ```rust
   // 示例代码：先抄 FAQ.md L46-L51 的反例
   fn process(s: &mut [i32]) {
       for v in s.iter_mut() {
           *v += 1;
       }
   }

   fn increment_all(slice: &mut [i32]) {
       rayon::join(|| process(slice), || process(slice));
   }

   fn main() {
       let mut v = vec![1, 2, 3];
       increment_all(&mut v);
       println!("{v:?}");
   }
   ```

2. 编译：

   ```bash
   cargo build --bin race_fail
   ```

3. 观察编译错误后，把 `increment_all` 改成先切分再并行（**示例代码**）：

   ```rust
   fn increment_all(slice: &mut [i32]) {
       let (a, b) = slice.split_at_mut(slice.len() / 2);
       rayon::join(|| process(a), || process(b));
   }
   ```

4. 再次 `cargo build --bin race_fail && cargo run --bin race_fail`。

**需要观察的现象**：

- 第 2 步：编译失败。两个闭包都要用 `slice`（一个 `&mut [i32]`，不可 `Copy`），第一个闭包把它移走后第二个就无权使用——典型报错是 "use of moved value / cannot borrow ... as mutable more than once" 一类的借用/move 错误（具体错误码以本地 rustc 输出为准）。**注意：这个错误是好事**——若没有它，两个线程同时 `+1` 同一片内存就是未定义行为。
- 第 4 步：编译通过，打印 `[2, 2, 4]`（前半 `[1,2]` 与后半 `[3]` 各被加一次）。

**预期结果**：如上。**待本地验证**：具体错误信息文本因 rustc 版本而异。

#### 4.3.5 小练习与答案

**练习 1**：下面这段代码用 Rayon 会编译失败吗？为什么？

```rust
let counter = std::rc::Rc::new(std::cell::Cell::new(0));
rayon::join(|| counter.set(1), || counter.set(2));
```

<details><summary>参考答案</summary>

会。`Rc` 是引用计数的非原子实现，不是 `Send`；闭包捕获了 `Rc<...>` 就不满足 `join` 的 `Send` 约束，编译器报 E0277。这正是 4.3.2 流程图的中间分支。修法：换成 `Arc<AtomicUsize>`（FAQ L53-L63 的替换表）。
</details>

**练习 2**：把 `Cell` 换成 `AtomicUsize` 后就绝对安全了吗？

<details><summary>参考答案</summary>

没有数据竞争了（不再有未定义行为），但可能仍有**竞态条件**：先 `load` 再 `store` 两步之间别的线程可能插入，导致更新丢失（FAQ.md:L74-L100 的时序图）。计数应使用 `fetch_add`，比较交换用 CAS 类操作（FAQ.md:L108-L111）。"编译通过"挡住的是前者，不是后者。
</details>

**练习 3**：`input.par_iter().map(|&i| i * i).sum()` 的结果和串行版一定一样吗？把 `map` 换成 `for_each(...)` 且闭包里 `println!` 呢？

<details><summary>参考答案</summary>

`sum` 一样：数值结果与串行对应物一致（README.md:L47-L63）。`for_each` + 打印：每个元素都会被处理且各打印一次，但**打印顺序**不保证与串行一致——这正是 README 说的"副作用可能以不同顺序发生"。顺序敏感的副作用需要用保序的机制（如 collect 后再串行打印，或 u3-l4 讲的顺序保证操作）。
</details>

## 5. 综合实践

**任务**：把 4.1.4 的对比实验升级成一次"准基准测试"，感受并行收益与线程数的关系。

在 `src/main.rs`（示例代码）：

```rust
use rayon::prelude::*;
use std::time::Instant;

fn main() {
    // 换 i64 避免溢出；一百万个元素足以让并行版本显出差距
    let input: Vec<i64> = (0..1_000_000).collect();
    let expected: i64 = input.iter().map(|&i| i * i).sum();

    let t0 = Instant::now();
    let seq: i64 = input.iter().map(|&i| i * i).sum();
    let t1 = Instant::now();
    let par: i64 = input.par_iter().map(|&i| i * i).sum();
    let t2 = Instant::now();

    assert_eq!(seq, expected);
    assert_eq!(par, expected);
    println!("串行 {:?}  并行 {:?}  结果 {}", t1 - t0, t2 - t1, par);
}
```

步骤与观察点：

1. `cargo run --release`（务必 `--release`，debug 模式优化关闭，对比失真）。记录串行/并行耗时比，与机器逻辑核数对照。
2. 再分别用 `RAYON_NUM_THREADS=1 cargo run --release` 和 `RAYON_NUM_THREADS=2 cargo run --release` 运行，观察并行耗时随线程数的变化（该环境变量的作用见 [FAQ.md:L11-L15](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/FAQ.md#L11-L15)）。
3. 预期：结果三者一致；`RAYON_NUM_THREADS=1` 时并行版耗时接近串行版（退化为单线程）；线程数从 1 到核数加速明显，超过核数后收益趋平。具体数字**待本地验证**。
4. 思考题（为 u9-l3 埋伏笔）：本例每元素只做一次乘法，任务太细。试试给 `.sum()` 前加 `.with_min_len(1024)` 会怎样？（该方法属于 `IndexedParallelIterator`，u3-l3 详述。）

## 6. 本讲小结

- Rayon 是 Rust 的**数据并行**库：把 `iter()` 改成 `par_iter()` 一行完成并行化，由**工作窃取**调度在运行期自适应切分任务（源自 MIT Cilk 项目）。
- 工作窃取协议：调用 `join(a,b)` 的线程把 b 入队后先做 a，空闲线程偷 b；由此自动获得负载均衡，理论期望时间满足 \( T_P \le W/P + O(S) \)。
- 四类核心 API 按控制粒度递增：**并行迭代器**（Rayon 决定切分）→ **join**（两半分治）→ **scope**（任意任务 + 借用栈上数据）→ **ThreadPool**（自建池）。
- "能编译就没有数据竞争"的机制是所有并行入口都要求闭包与返回值 `Send`，把整类并行 bug 从运行期挪到编译期；但**竞态条件**与**副作用顺序**不在保证范围内。
- 默认线程数等于可用 CPU（逻辑核）数，可用 `RAYON_NUM_THREADS` 或 `ThreadPoolBuilder::build_global` 修改。
- 本讲实践：跑通 README 的 `sum_of_squares`，手写 `std::thread::scope` 等价版对比代码量，并复现了 FAQ 的编译期反竞争拦截。

## 7. 下一步学习建议

下一讲 **u1-l2《构建、测试与运行 demo》** 将把本仓库克隆下来本地构建：理解 workspace 三 crate（`rayon` / `rayon-core` / `rayon-demo`）的组织、跑通测试与 `rayon-demo` 演示。

继续阅读源码的建议路径：

1. 通读 [README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/README.md) 全文（本讲只精读了三分之一）。
2. 浏览库入口文档 [src/lib.rs:L1-L79](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L1-L79)，这是官方最好的 API 总览。
3. 有兴趣可读 README 引用的背景博客 *Rayon: Data Parallelism in Rust*（Nicholas Matsakis），理解设计动机。
