# ThreadPoolBuilder：线程池配置

## 1. 本讲目标

学完本讲，你应该能够：

1. 使用 `ThreadPoolBuilder` 的链式配置方法设定线程数、线程名、栈大小等参数，并知道每项配置在源码中落在哪个字段上。
2. 理解「线程名闭包」从 builder 一路传递到 `std::thread::Builder::name` 的完整旅程，并会用 `start_handler` / `exit_handler` 观察线程生命周期。
3. 掌握 `build` 与 `build_global` 的区别：前者返回一个可随 `drop` 终止的用户池句柄，后者替换全局池且恰好成功一次；理解全局池惰性初始化带来的「先到先得」限制。
4. 理解 `spawn_handler` 与 `CustomSpawn` 如何把「如何创建线程」本身变成可定制的插槽，以及为什么 `ThreadSpawn` trait 是 crate 私有的。

## 2. 前置知识

- **链式 Builder 模式**：Rust 中常见的配置写法——结构体持有全部配置项，每个配置方法「吃掉 self、返回新 self」（`fn num_threads(mut self, ...) -> Self`），从而可以像 `.num_threads(2).thread_name(...)` 一样串联。这与返回 `&mut Self` 的 JavaScript 风格不同，好处是配置完成后所有权交出， builder 本身不可再被误用。
- **线程池与 Registry**：第 5 单元讲过，rayon-core 的 `Registry` 是线程注册表，持有每线程的工作窃取队列、睡眠计数器、注入队列等。本讲的 `ThreadPoolBuilder` 就是「生产一个 `Registry` 的配方」。建议先回顾 u5-l3 的三条启动链路。
- **惰性初始化（lazy init）**：全局线程池不是程序启动时就创建的，而是第一次有人用到 rayon 时才创建。`std::sync::Once` 保证同一段初始化代码在多线程下只执行一次。
- **泛型参数做策略插槽**：`ThreadPoolBuilder<S>` 的 `S` 是「如何 spawn 线程」的策略类型，默认是 `DefaultSpawn`（用 `std::thread`），可换成 `CustomSpawn<F>`（用户闭包）。这是「类型层面注入策略」的手法，不需要 dyn trait 对象。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs) | `ThreadPoolBuilder` 结构体定义与全部配置方法（`num_threads`、`thread_name`、`stack_size`、`spawn_handler`、`build`、`build_global` 等），以及 `ThreadPoolBuildError` |
| [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) | `ThreadBuilder`、`ThreadSpawn`/`DefaultSpawn`/`CustomSpawn`、`Registry::new`（配置的真正消费者）、全局池的 `Once` 初始化、`main_loop`（start/exit handler 调用点） |
| [rayon-core/src/thread_pool/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs) | `ThreadPool` 句柄：`build` 如何拿到 registry、`install` 如何把闭包送进池执行 |
| [tests/named-threads.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/named-threads.rs) | 官方线程命名集成测试：`build_global` + 在并行任务里断言线程名 |

另外你会看到旧类型 `Configuration`（[rayon-core/src/lib.rs:199-204](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L199-L204)），它只是 `ThreadPoolBuilder` 的废弃外壳，读旧代码时认识即可，新代码不要用。

## 4. 核心概念与源码讲解

### 4.1 builder 链式配置

#### 4.1.1 概念说明

`ThreadPoolBuilder` 是一份「线程池配方」。它本身不创建任何线程，只是一个装着 9 个配置字段的结构体；真正的创建动作发生在 `build()` / `build_global()` 里，由 `Registry::new` 消费这份配方。

需要理解的三个设计点：

1. **`num_threads` 默认 0 表示「自动」**：0 不是「零线程」，而是一个哨兵值，意思是「按优先级链自动决定」（显式设置 > `RAYON_NUM_THREADS` 环境变量 > 逻辑核数）。
2. **配置项分两类**：普通字段（线程数、栈大小）与装箱闭包字段（线程名、panic/start/exit handler）。后者因为大小编译期未知，必须 `Box` 起来。
3. **`S: ThreadSpawn` 泛型参数**是「spawn 策略」插槽，本模块先记住它默认是 `DefaultSpawn`，细节在 4.4 展开。

#### 4.1.2 核心流程

```text
ThreadPoolBuilder::new()          // 全部字段取默认值（num_threads = 0, spawn_handler = DefaultSpawn）
    .num_threads(2)               // 覆写 num_threads 字段
    .thread_name(|i| ...)         // 装箱闭包进 get_thread_name 字段
    .stack_size(4 * 1024 * 1024)  // 覆写 stack_size 字段
    .build()                      // → Registry::new(配方) → ThreadPool 句柄
    .build_global()               // → init_global_registry(配方) → 全局池（恰好成功一次）
```

线程数的最终裁定优先级链（与 u5-l3 讲过的 `get_num_threads` 一致，这里从 builder 视角再看一眼）：

\[
\text{显式 } \texttt{num\_threads}(>0) \;>\; \texttt{RAYON\_NUM\_THREADS} \;>\; \texttt{RAYON\_RS\_NUM\_CPUS}(\text{废弃}) \;>\; \text{available\_parallelism()}
\]

且末端还会被 `max_num_threads()` 软限截断。

#### 4.1.3 源码精读

先看结构体全貌——每个字段对应一项可配置能力：

- [rayon-core/src/lib.rs:165-197](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L165-L197)：`ThreadPoolBuilder<S = DefaultSpawn>` 的 9 个字段。注意 `num_threads: usize` 的注释明确写着「If zero will use the RAYON_NUM_THREADS environment variable」——0 是自动哨兵。
- [rayon-core/src/lib.rs:221-235](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L221-L235)：手写 `Default` 实现。注释 `// NB: We can't #[derive(Default)] because S is left ambiguous` 说明为什么不能派生——`S` 是泛型参数，derive 无法为其确定默认类型。

三个最常用的配置方法，注意签名都是 `mut self -> Self`：

- [rayon-core/src/lib.rs:525-528](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L525-L528)：`num_threads` 设置线程数。文档中的「Future compatibility warning」值得读：默认行为未来可能变成动态增减线程，想锁死线程数就显式调用它。
- [rayon-core/src/lib.rs:492-498](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L492-L498)：`thread_name` 接收闭包 `FnMut(usize) -> String`，参数是线程在池内的索引（`0..num_threads`），闭包会被调用 N 次。
- [rayon-core/src/lib.rs:581-584](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L581-L584)：`stack_size` 设置工作线程栈大小（字节）。深递归的并行任务（如 `join` 递归到底的 fib）可能需要调大。

自动线程数的裁定逻辑就在 builder 自己身上：

- [rayon-core/src/lib.rs:454-482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L454-L482)：私有方法 `get_num_threads`。`self.num_threads > 0` 直接返回显式值；否则依次尝试 `RAYON_NUM_THREADS`（合法正整数则采用，`Some(0)` 视为「用默认」）、废弃的 `RAYON_RS_NUM_CPUS`，最后兜底 `available_parallelism()`。这是 u5-l3 优先级链的源头代码。

配置真正被消费的第一站在 `Registry::new`：

- [rayon-core/src/registry.rs:237-245](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L237-L245)：开头第一行就是 `Ord::min(builder.get_num_threads(), crate::max_num_threads())`——线程数软限。`max_num_threads()`（[rayon-core/src/lib.rs:108-111](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L108-L111)）返回睡眠计数器的位上限：64 位平台 \( 2^{16} - 1 = 65535 \)，32 位平台 \( 2^{8} - 1 = 255 \)（定义见 [rayon-core/src/sleep/counters.rs:56-60](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L56-L60) 与 [counters.rs:77](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L77)）。原因在 u5-l5 讲过：睡眠线程数要压进 `AtomicUsize` 的固定位段。

顺带一提两个容易踩的废弃项：`breadth_first()`（[rayon-core/src/lib.rs:613-617](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L613-L617)）已按 RFC #1 废弃，替代品是 `scope_fifo` / `spawn_fifo`（u6-l1、u6-l2 讲过）；整个 `Configuration` 类型（[rayon-core/src/lib.rs:199-204](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L199-L204)）是 `ThreadPoolBuilder` 的废弃外壳。

#### 4.1.4 代码实践

**实践目标**：验证 `num_threads` 是「每个池各自的属性」，以及自动模式对环境变量的响应。

**操作步骤**（在 u1-l3 创建的示例工程中，或新建一个依赖 `rayon` 的 Cargo 工程）：

1. 写入以下程序（示例代码）：

   ```rust
   use rayon::prelude::*;
   use rayon::ThreadPoolBuilder;

   fn main() {
       let tiny = ThreadPoolBuilder::new().num_threads(2).build().unwrap();
       let big = ThreadPoolBuilder::new().num_threads(8).build().unwrap();

       println!("tiny 池内: {}", tiny.install(|| rayon::current_num_threads()));
       println!("big  池内: {}", big.install(|| rayon::current_num_threads()));
       println!("池外(全局): {}", rayon::current_num_threads());
   }
   ```

2. 先直接 `cargo run --release` 运行一次，记录「池外(全局)」的数字（应等于机器逻辑核数）。
3. 再用 `RAYON_NUM_THREADS=3 cargo run --release` 运行，对比三行输出。

**需要观察的现象**：`install` 内外 `current_num_threads()` 返回不同值——`install` 把当前 registry 切换成目标池；显式 `num_threads` 的两个池不受环境变量影响，只有「池外(全局)」那行从核数变成 3。

**预期结果**：两次运行中 tiny/big 两行始终是 2 和 8；全局一行第一次为逻辑核数，第二次为 3。（具体数字与机器有关，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`ThreadPoolBuilder::new().num_threads(0).build()` 会创建零线程的池吗？

**答案**：不会。`0` 是「自动」哨兵：`get_num_threads`（[rayon-core/src/lib.rs:454-482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L454-L482)）在 `num_threads == 0` 时依次查 `RAYON_NUM_THREADS`、`RAYON_RS_NUM_CPUS`，最后取 `available_parallelism()`，实际线程数至少为 1（`unwrap_or(1)` 兜底）。

**练习 2**：为什么 `ThreadPoolBuilder` 不能 `#[derive(Default)]`？

**答案**：见 [rayon-core/src/lib.rs:220-235](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L220-L235) 的注释：derive 会为泛型参数 `S` 生成 `S: Default` 约束并要求推断一个具体默认类型，而 `S` 在这里被刻意留成开放插槽（默认 `DefaultSpawn` 只在 `new()` 中给出），derive 无法表达「S 任意」的默认值。

**练习 3**：在一台 64 核机器上设置 `RAYON_NUM_THREADS=100000`，实际会起多少线程？

**答案**：64 位平台上限是 `max_num_threads()` = \( 2^{16} - 1 = 65535 \)。`Registry::new` 第一行的 `Ord::min` 软限（[rayon-core/src/registry.rs:244](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L244)）会把 100000 截到 65535。限制来源是睡眠计数器只给线程数留了 16 个比特。

### 4.2 线程命名：从闭包到 std::thread 的旅程

#### 4.2.1 概念说明

给线程命名不是装饰：在 `htop`、`top -H`、崩溃报告、调试器里看到的是线程名而不是匿名 `Thread-7`；服务器程序还常用名字区分「池 A 的工人」和「池 B 的工人」。

rayon 的线程名不是一个静态字符串，而是**闭包** `FnMut(usize) -> String`：每个线程创建前，registry 会以该线程的索引调用一次闭包取名字。这样可以用 `format!("rayon-worker-{i}")` 生成带编号的名字。名字随后装进 `ThreadBuilder`，由 spawn 策略转交给 `std::thread::Builder::name`，最终成为操作系统可见的线程名——任务代码里用 `std::thread::current().name()` 就能读到它。

与命名同属「线程生命周期钩子」的还有 `start_handler` / `exit_handler`：分别在worker 线程进入主循环前和退出前调用，参数同样是线程索引。

#### 4.2.2 核心流程

```text
thread_name(|i| format!("rayon-worker-{i}"))     // 用户配置闭包
        │
        ▼
Registry::new 循环中 builder.get_thread_name(index) // 每线程求值一次
        │  装进
        ▼
ThreadBuilder { name, stack_size, worker, stealer, registry, index }
        │  交给
        ▼
DefaultSpawn::spawn(thread)                        // 读 thread.name() / thread.stack_size()
        │  转交
        ▼
std::thread::Builder::new().name(...).stack_size(...).spawn(|| thread.run())
        │
        ▼
main_loop：set_current → 置 primed → start_handler(index)
        │  …… 工作窃取主循环 ……
        ▼
退出前：exit_handler(index)
```

#### 4.2.3 源码精读

- [rayon-core/src/registry.rs:283-291](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L283-L291)：`Registry::new` 的建线程循环里为每个索引组装一个 `ThreadBuilder`，其中 `name: builder.get_thread_name(index)` 就是用户闭包的求值点。除名字外，它还打包了该线程专属的 `worker`/`stealer` 队列半边和指向 registry 的 `Arc` 引用。
- [rayon-core/src/registry.rs:22-52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L22-L52)：`ThreadBuilder` 的公开访问器 `index()` / `name()` / `stack_size()`，以及关键的 `run(self)`——它调用 `main_loop` 且**不返回直到池被 drop**。这就是 spawn 策略必须在新线程里调用的入口。
- [rayon-core/src/registry.rs:84-96](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L84-L96)：`DefaultSpawn::spawn` 把 `ThreadBuilder` 翻译成 `std::thread::Builder` 调用：`name` 与 `stack_size` 若存在则设置，然后 `b.spawn(|| thread.run())`。注意闭包捕获的是整个 `thread`（按值），把所有权搬进新线程。
- [rayon-core/src/registry.rs:913-944](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L913-L944)：`main_loop` 开头 `Latch::set(&...primed)` 通知「本线程就绪」（4.3 会用到这个信号），随后 [929-931 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L929-L931) 调用 `start_handler`，主循环 `wait_until_out_of_work()` 结束后在 [939-942 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L939-L942) 调用 `exit_handler`。两个 handler 都包在 `registry.catch_unwind` 里，handler 自己 panic 不会带崩工作线程。
- [rayon-core/src/lib.rs:634-640](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L634-L640) 与 [lib.rs:653-659](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L653-L659)：`start_handler` / `exit_handler` 的配置方法，闭包约束 `Fn(usize) + Send + Sync + 'static`——`Sync` 是因为同一个闭包可能被多个线程同时调用。
- [tests/named-threads.rs:6-25](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/named-threads.rs#L6-L25)：官方验收测试。它用 `build_global` 配置名字 `hello-name-test-{i}`，然后让 10000 个并行任务各自读 `std::thread::current().name()` 收集进 `HashSet`，断言所有名字都以 `hello-name-test-` 开头。这就是「名字确实传到了任务执行线程」的端到端证据。

#### 4.2.4 代码实践

**实践目标**：亲眼确认线程名出现在操作系统层面，并用 `start_handler` 观察线程启动时序。

**操作步骤**：

1. 在 rayon 仓库根目录运行官方测试：

   ```bash
   cargo test --test named-threads
   ```

2. 在自己的示例工程写入（示例代码）：

   ```rust
   use rayon::prelude::*;
   use rayon::ThreadPoolBuilder;

   fn main() {
       let pool = ThreadPoolBuilder::new()
           .num_threads(2)
           .thread_name(|i| format!("rayon-worker-{i}"))
           .start_handler(|i| println!("线程 {i}（{:?}）启动", std::thread::current().name()))
           .exit_handler(|i| println!("线程 {i} 退出"))
           .build()
           .unwrap();

       let sum: i64 = pool.install(|| (0..1_000_000).into_par_iter().sum());
       println!("sum = {sum}");
       drop(pool); // 池被 drop，工作线程退出 → 触发 exit_handler
   }
   ```

3. `cargo run --release` 运行；再在 Linux 上运行后另开终端执行 `top -H -p $(pgrep -f 你的程序名)`，观察线程列表里的名字。

**需要观察的现象**：启动日志里出现两行 `线程 0`、`线程 1`，名字分别是 `Some("rayon-worker-0")`、`Some("rayon-worker-1")`；`drop(pool)` 之后打印两行「线程退出」；`top -H` 中能看到名为 `rayon-worker-*` 的线程。

**预期结果**：顺序上 start_handler 的两行必然先于「线程退出」两行，但线程 0 与线程 1 谁先打印不确定（两个线程并发启动）。若 `drop(pool)` 被省略，程序结束时池随进程回收，「线程退出」可能来不及打印。

#### 4.2.5 小练习与答案

**练习 1**：`thread_name` 闭包总共会被调用几次？在哪个阶段？

**答案**：每个线程一次，共 `num_threads` 次，发生在 `Registry::new` 的建线程循环里（[rayon-core/src/registry.rs:285](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L285) 组装 `ThreadBuilder` 时），早于任何任务执行。

**练习 2**：`use_current_thread(true)` 的池里，索引 0 的线程会调用 start_handler / exit_handler 吗？

**答案**：不会。[rayon-core/src/lib.rs:530-537](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L530-L537) 文档明确说明：当前线程不受 rayon 管理，两个 handler 都不为其运行。源码依据是 [registry.rs:293-309](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L293-L309)——`use_current_thread` 分支直接 `set_current` 后 `continue`，根本不进入 `main_loop`，而两个 handler 只在 `main_loop` 里调用。

**练习 3**：为什么 `start_handler` 的闭包约束需要 `Sync`，而 `thread_name` 只需要 `FnMut`？

**答案**：`thread_name` 在 `Registry::new` 里被**串行**调用 N 次（建线程循环是顺序的），`&mut` 独占访问即可，所以 `FnMut` 够用（[rayon-core/src/lib.rs:492-498](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L492-L498)）。`start_handler` 被存进 `Registry`，由**多个工作线程同时**调用（[rayon-core/src/registry.rs:929-931](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L929-L931)），多个 `&` 共享引用并发存在，故必须 `Fn + Sync`。

### 4.3 全局池初始化：build_global 的时机与限制

#### 4.3.1 概念说明

rayon 有两类池：

| | `build()` | `build_global()` |
| --- | --- | --- |
| 返回值 | `ThreadPool` 句柄 | `()`（只有 `Ok`/`Err`） |
| 生命周期 | 句柄 drop 时池有序终止 | 池永不终止，随进程退出 |
| 可调用次数 | 任意多次，每池一份配置 | **恰好成功一次** |
| 影响范围 | 只影响经该句柄派发的任务 | 所有未指定池的顶层调用（`rayon::join`、`par_iter` 等） |

`build_global` 的两个典型场景写在它的文档里：**改默认配置**（比如限制全局池线程数），或**基准测试前预热**（第一次迭代就有就绪的线程）。

它的核心限制是「先到先得」：全局池是惰性单例，**任何**顶层 rayon 调用（哪怕只读的 `rayon::current_num_threads()`）都可能抢先触发默认配置的初始化；一旦触发，后来的 `build_global` 只能拿到 `GlobalPoolAlreadyInitialized` 错误。所以想定制全局池，必须抢在 main 里第一行、任何 rayon 调用之前执行。

#### 4.3.2 核心流程

```text
build_global(builder)
    │
    ▼
init_global_registry(builder)
    │
    ▼
set_global_registry(|| Registry::new(builder))
    │  THE_REGISTRY_SET.call_once(...)
    ├─ 第一次：Registry::new 成功 → 写入 THE_REGISTRY → 返回 Ok
    ├─ 第一次但失败：错误经 result 带出（Once 已消耗，不再重试）
    └─ 第二次进入：call_once 直接返回，result 保持初始值
                    → Err(GlobalPoolAlreadyInitialized)
    │ 成功后
    ▼
registry.wait_until_primed()   // 等每个线程的 primed 锁存器置位
    │
    ▼
Ok(())  —— 从此全局池配置不可更改
```

#### 4.3.3 源码精读

- [rayon-core/src/lib.rs:252-254](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L252-L254)：`build()` 只有一行——转调 `ThreadPool::build(self)`；后者（[rayon-core/src/thread_pool/mod.rs:58-66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L58-L66)）调 `Registry::new(builder)` 并把 `Arc<Registry>` 包成 `ThreadPool` 句柄。
- [rayon-core/src/lib.rs:273-278](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L273-L278)：`build_global()` 两步——`init_global_registry(self)?` 拿全局 registry，再 `wait_until_primed()`。文档写明：初始化恰好发生一次，第二次调用返回错误，`Ok` 表示「这是全局池的首次初始化」。
- [rayon-core/src/registry.rs:154-155](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L154-L155)：全局单例的存储——`static mut THE_REGISTRY: Option<Arc<Registry>>` 加一个 `Once`。这是 u5-l3 讲过的「惰性单例」骨架。
- [rayon-core/src/registry.rs:185-205](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L185-L205)：`set_global_registry` 是限制的实现者。注意一个精巧细节：`result` 的**初始值**就是 `GlobalPoolAlreadyInitialized` 错误，只有 `call_once` 里的闭包真正执行并成功时才被覆写为 `Ok`。于是「第二次调用时 `call_once` 不再执行闭包，result 原样返回初始错误」——用一个初始值优雅地覆盖了「已初始化」分支。
- [rayon-core/src/registry.rs:207-226](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L207-L226)：`default_global_registry`——隐式初始化用**默认配置**（`ThreadPoolBuilder::new()`，即自动线程数、无名、默认栈）。末尾还有一个 wasm 兜底：若环境不支持线程（`io::ErrorKind::Unsupported`），退回 `num_threads(1).use_current_thread()` 的单线程「池」，这就是 lib.rs 顶部文档所说「WebAssembly 无线程回退」的落点。注意**显式** `ThreadPoolBuilder` 方法不享受这个兜底，错误原样上报。
- [rayon-core/src/registry.rs:388-392](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L388-L392)：`wait_until_primed` 逐个等待每个线程的 `primed` 锁存器。置位点在 `main_loop` 开头（[registry.rs:921](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L921)）——线程完成 `set_current` 注册后才置位。所以 `build_global` 返回时，所有工作线程已注册完毕、随时可接活；这就是「基准测试预热」收益的来源。
- [rayon-core/src/lib.rs:141-146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L141-L146)：`ThreadPoolBuildError` 的三种错误类型——`GlobalPoolAlreadyInitialized`（本模块主角）、`CurrentThreadAlreadyInPool`（`use_current_thread` 时当前线程已在别的池里，见 [registry.rs:293-298](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L293-L298)）、`IOError`（spawn 线程失败，见 [registry.rs:311-313](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L311-L313)）。

#### 4.3.4 代码实践

**实践目标**：亲手触发「先到先得」限制，理解为什么 `build_global` 必须放在 main 的最前面。

**操作步骤**：

1. 在示例工程写入（示例代码）：

   ```rust
   use rayon::ThreadPoolBuilder;

   fn main() {
       // ① 先查询一次线程数——这会触发全局池的隐式初始化
       println!("提前查询: {}", rayon::current_num_threads());

       // ② 再试图定制全局池
       let result = ThreadPoolBuilder::new()
           .num_threads(2)
           .build_global();
       println!("build_global 结果: {:?}", result);
   }
   ```

2. `cargo run --release` 运行，读第二行输出。
3. 把 ① 那行 `println!` 注释掉，再次运行，并在池里真正跑一个任务（例如 `rayon::join(|| 1, || 2);`）确认 2 线程池生效：`println!("{}", rayon::current_num_threads())`。
4. 回到 rayon 仓库，运行 `cargo test --test named-threads`，思考：这个测试也调用了 `build_global`，为什么不怕和别的测试冲突？

**需要观察的现象**：第 2 步中 `build_global` 返回 `Err(GlobalPoolAlreadyInitialized)`（错误信息为 "The global thread pool has already been initialized."）；第 3 步中同样的调用返回 `Ok(())`，随后 `current_num_threads()` 变为 2。

**预期结果**：与上述一致。第 4 步的答案：每个集成测试文件编译为**独立的可执行文件**（Cargo 的集成测试机制），`named-threads` 与其它测试文件不在同一进程里，各自的 `build_global` 互不干扰；而同一文件内的第二个 `build_global` 就会失败。

#### 4.3.5 小练习与答案

**练习 1**：`build_global` 失败后重试一次会成功吗？

**答案**：不会。失败有两种：若失败原因是「已初始化」，`Once` 已消耗，重试只会拿到同样的错误；若失败原因是首次初始化本身出错（如 `IOError`），`set_global_registry` 的注释也说明 `call_once` 里的闭包不会再次执行——初始化机会只有一次，失败后全局池将永远无法通过 `build_global` 建立（u5-l3 提过「初始化失败不重试」）。

**练习 2**：`build_global()` 返回 `Ok(())` 之后，全局池什么时候销毁？

**答案**：永不销毁。全局 registry 有一个永不释放的引用计数（[rayon-core/src/registry.rs:135-148](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L135-L148) 的注释："if this is the global registry, there is a ref-count that never gets released"），工作线程随进程退出而消失。这也解释了 [rayon-core/src/lib.rs:348-350](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L348-L350) 的警告：在 `std::thread::scope` 里给全局池做 spawn_handler 会因池不终止而卡死 scope。

**练习 3**：`build()` 和 `build_global()` 能用同一份 builder 配置调用两次，分别建两个池吗？

**答案**：能。`build()` 每次都走 `Registry::new` 创建全新 registry，互不影响；但 `build_global()` 消费的是调用顺序——两次 `build_global`（或一次隐式初始化 + 一次 `build_global`）中只有第一次生效。builder 方法都是 `self -> Self`，链式调用后 `build` 与 `build_global` 都拿走所有权，同一实例只能二选一，但可以用两个独立的 builder 链。

### 4.4 spawn_handler 与 CustomSpawn：换掉「如何创建线程」

#### 4.4.1 概念说明

默认情况下，rayon 用 `std::thread::Builder::spawn` 创建工作线程。但有些场景需要接管线程创建：把 rayon 工人挂到自己的线程框架（如某种 actor 运行时）、在 scoped 线程里借栈数据（`build_scoped`）、或给线程加统一的前置初始化（设置线程亲和性、打开 tracing）。

`spawn_handler` 把这个决策点开放出来：你给一个闭包 `FnMut(ThreadBuilder) -> io::Result<()>`，rayon 把**每根线程的完整启动材料**（名字、栈大小、队列半边、registry 引用、索引）打包成 `ThreadBuilder` 交给你，由你负责「在一根独立线程里调用 `thread.run()`」。`run` 是唯一入口——它进入工作窃取主循环，直到池终止。

类型层面的机制：`ThreadPoolBuilder<S>` 的 `S` 默认是 `DefaultSpawn`；调用 `spawn_handler(f)` 后整个 builder 的类型变成 `ThreadPoolBuilder<CustomSpawn<F>>`。`ThreadSpawn` 是 **crate 私有 trait**（`pub(crate)`），外部无法命名或实现它——定制只能经由闭包这一条受控通道，同时 `#[expect(unnameable_types)]` 允许 `DefaultSpawn`/`CustomSpawn` 出现在公开签名里而不进入稳定 API 文档。

#### 4.4.2 核心流程

```text
ThreadPoolBuilder::new()
    .spawn_handler(|thread| {            // thread: ThreadBuilder
        std::thread::spawn(|| thread.run());  // 你来决定怎么开线程
        Ok(())
    })                                    // 类型变为 ThreadPoolBuilder<CustomSpawn<F>>
    .build()
        │
        ▼
Registry::new 循环里：builder.get_spawn_handler().spawn(thread)   // registry.rs:311
        │ 闭包接手 ThreadBuilder
        ▼
闭包内必须（直接或间接）在新线程调用 thread.run() → main_loop
```

契约要点：闭包返回 `io::Result<()>`——`Err` 会让 `Registry::new` 以 `IOError` 失败（[registry.rs:311-313](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L311-L313)）；若闭包「收下线程却从不调用 `run()`」，`build` 本身可能成功（spawn 都返回了 Ok），但没有任何线程进入主循环，后续任务派发将无人认领。

#### 4.4.3 源码精读

- [rayon-core/src/registry.rs:69-73](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L69-L73)：`ThreadSpawn` trait 全文——只有一个方法 `spawn(&mut self, thread: ThreadBuilder) -> io::Result<()>`。注释言明它 crate 私有的理由：「we don't actually want to expose these details in the API」。
- [rayon-core/src/registry.rs:82-96](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L82-L96)：`DefaultSpawn` 的实现，即「翻译器」——把 `ThreadBuilder` 的 `name()`/`stack_size()` 翻成 `std::thread::Builder` 的链式调用，再 `spawn(|| thread.run())`。可以把它当作自定义 handler 的参考范本。
- [rayon-core/src/registry.rs:103-124](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L103-L124)：`CustomSpawn<F>` 只是闭包的新类型包装，`spawn` 就是 `(self.0)(thread)` 一行转发。
- [rayon-core/src/lib.rs:429-445](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L429-L445)：`spawn_handler` 方法本体。注意 [435 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L435) 那行被注释掉的 `// ..self`：因为返回类型从 `ThreadPoolBuilder<S>` 变成了 `ThreadPoolBuilder<CustomSpawn<F>>`，Rust 的结构体更新语法要求基表达式与目标同类型，用不了，只能把其余 8 个字段逐一搬过去。这是「泛型参数变化的 builder」的一个真实代价。
- [rayon-core/src/lib.rs:342-394](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L342-L394)：`spawn_handler` 的文档给了三个渐进示例——最小版（`std::thread::spawn(|| thread.run())`）、完全复刻默认行为版、以及 `std::thread::scope` scoped 版。第三个示例就是 `build_scoped` 的内核。
- [rayon-core/src/lib.rs:318-339](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L318-L339)：`build_scoped`——用 `std::thread::scope` 包住建池过程，使每根工作线程的闭包能借用 `scope` 外的栈数据（示例：把 `pool_data` 塞进 scoped thread-local）。它内部就是「自动生成一个 `spawn_scoped` 版的 spawn_handler 再 `build`」，是 4.4 定制能力的官方应用范例。
- [rayon-core/src/registry.rs:311-313](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L311-L313)：`Registry::new` 中 handler 的调用点与错误传播——`if let Err(e) = builder.get_spawn_handler().spawn(thread)` 把 `io::Error` 包成 `ThreadPoolBuildError::IOError` 返回（同时 `Terminator` 守卫会终止已启动的线程，见 [registry.rs:280-281](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L280-L281)）。

#### 4.4.4 代码实践

**实践目标**：体验「接管线程创建」，并观察违反契约（不调用 `run()`）的后果。

**操作步骤**：

1. 在示例工程写入正常版（示例代码）：

   ```rust
   use rayon::ThreadPoolBuilder;

   fn main() -> Result<(), rayon::ThreadPoolBuildError> {
       let pool = ThreadPoolBuilder::new()
           .num_threads(2)
           .spawn_handler(|thread| {
               println!("接管线程 {} 的创建", thread.index());
               let mut b = std::thread::Builder::new();
               if let Some(name) = thread.name() {
                   b = b.name(name.to_owned());
               }
               if let Some(size) = thread.stack_size() {
                   b = b.stack_size(size);
               }
               b.spawn(|| thread.run())?;
               Ok(())
           })
           .thread_name(|i| format!("custom-{i}"))
           .build()?;

       let n = pool.install(|| (0..1000).sum::<i64>());
       println!("sum = {n}");
       Ok(())
   }
   ```

   `cargo run --release` 运行，确认输出两行「接管线程 0/1 的创建」和正确的 sum。

2. 做反例实验：把闭包里的 `b.spawn(|| thread.run())?;` 整行换成 `Ok(())`（即收下 `ThreadBuilder` 但不启动线程），再运行。

**需要观察的现象**：第 1 步一切正常，且 handler 的 `thread.name()` 能读到 `thread_name` 闭包产生的 `custom-0`/`custom-1`；第 2 步 `build` 仍然成功返回（spawn 都返回了 Ok），但 `install` 永远不返回——没有线程进入 `main_loop`，任务注入后无人认领，程序卡死（可用 Ctrl-C 结束）。

**预期结果**：如上。反例的结论可直接从源码推出：`install → registry.in_worker` 对池外调用者会注入任务并等待锁存器（u6 系列讲过的阻塞等待路径），而锁存器只有 `main_loop` 里的线程才会置位。此现象待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ThreadSpawn` trait 不做成公开的，让用户实现它？

**答案**：见 [rayon-core/src/registry.rs:65-68](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L65-L68) 的注释——rayon 不想把内部细节暴露进稳定 API。做法是 trait 私有（`pub(crate)`），但把 `spawn_handler` 做成只收闭包的公开方法，闭包能力已覆盖定制需求，还避免了用户实现 `ThreadSpawn` 时依赖 `ThreadBuilder` 内部队列等细节的兼容性负担。

**练习 2**：`spawn_handler` 里 `b.spawn(|| thread.run())?` 的 `?` 把什么错误传到哪里？

**答案**：把 `std::thread::Builder::spawn` 的 `io::Result` 错误（如资源不足）从闭包传出，成为 `Registry::new` 里 [registry.rs:311-313](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L311-L313) 的 `ThreadPoolBuildError::IOError`，`build()` 以 `Err` 返回给调用方，已启动的线程由 `Terminator` 守卫终止，不会泄漏。

**练习 3**：想在每根工作线程里访问一个主线程创建的 `Vec<i32>`（非 `'static` 借用），本讲的哪条路径可行？

**答案**：`build_scoped`。它（[rayon-core/src/lib.rs:318-339](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L318-L339)）用 `std::thread::scope` 保证所有工作线程在 scope 结束前 join，线程闭包因此能借用 `pool_data`；直接 `build()` 的线程是 `'static` 的（默认 spawn_handler 用 `std::thread::spawn`），借不到栈数据。文档示例用 scoped thread-local（`scoped_tls`）就是标准用法。

## 5. 综合实践

**任务**：实现本讲规格要求的目标——构建一个 2 线程、线程名为 `rayon-worker-<i>` 的自定义池，并仿照官方 `tests/named-threads.rs` 做端到端验证。

**步骤**：

1. 新建（或复用 u1-l3 的）Cargo 工程，`Cargo.toml` 中添加 `rayon = "1"`。
2. 写入主程序（示例代码）：

   ```rust
   use rayon::prelude::*;
   use rayon::ThreadPoolBuilder;
   use std::collections::HashSet;

   fn main() {
       let pool = ThreadPoolBuilder::new()
           .num_threads(2)
           .thread_name(|i| format!("rayon-worker-{i}"))
           .build()
           .expect("建池失败");

       // 仿照 tests/named-threads.rs：并行任务里收集自己的线程名
       let names: HashSet<String> = pool.install(|| {
           (0..10_000)
               .into_par_iter()
               .map(|_| std::thread::current().name().unwrap().to_owned())
               .collect()
       });

       println!("观察到的线程名: {:?}", names);

       // 验证一：只有两根线程，名字都带前缀
       assert_eq!(names.len(), 2, "应恰有 2 根命名线程");
       assert!(names.iter().all(|n| n.starts_with("rayon-worker-")));
       // 验证二：索引 0 和 1 都出现过
       assert!(names.contains("rayon-worker-0") && names.contains("rayon-worker-1"));

       println!("全部断言通过");
   }
   ```

3. `cargo run --release` 运行。
4. 强化：把 `assert_eq!(names.len(), 2)` 改成 `3` 再运行一次，确认断言按预期失败（证明计数不是碰巧）。
5. 对照仓库官方测试交叉验证：在 rayon 仓库根目录运行 `cargo test --test named-threads`，比较它与你的程序在「收集名字的载体」（`HashSet<String>`）与「断言方式」（前缀检查）上的异同。

**预期结果**：程序打印形如 `{"rayon-worker-0", "rayon-worker-1"}` 的集合（顺序不定）与「全部断言通过」；第 4 步触发 panic 并报告 `assertion failed: left == right`（2 ≠ 3）；第 5 步官方测试通过。`names.len() == 2` 之所以稳定成立，是因为 10000 个采样远大于线程数，两根线程必然都被调度到。

## 6. 本讲小结

- `ThreadPoolBuilder` 是纯配置对象：9 个字段 + 链式 setter（`mut self -> Self`），真正的创建在 `build()` / `build_global()` 里由 `Registry::new` 消费；`num_threads = 0` 是「自动」哨兵，最终线程数还会被 `max_num_threads()`（64 位平台 65535）软限。
- 线程名是 `FnMut(usize) -> String` 闭包，在 `Registry::new` 建线程循环中逐线程求值，经 `ThreadBuilder` → `DefaultSpawn` → `std::thread::Builder::name` 抵达操作系统，任务内用 `std::thread::current().name()` 可读回；`start_handler`/`exit_handler` 在 `main_loop` 首尾被并发调用，故需 `Fn + Sync`。
- `build` 可任意多次、句柄 drop 即池终止；`build_global` 恰好成功一次且永不终止。全局池惰性初始化「先到先得」——任何顶层 rayon 调用都可能用默认配置抢先初始化，定制必须放在 main 最前面，否则得到 `GlobalPoolAlreadyInitialized`。
- `spawn_handler` 通过把 builder 的泛型参数从 `DefaultSpawn` 换成 `CustomSpawn<F>` 来开放线程创建权；契约是「在新线程里调用 `ThreadBuilder::run()`」，返回 `Err` 则建池失败（`IOError`），收下线程却不 `run()` 则池建成但永远无人干活。`build_scoped` 是该能力的官方组合应用。
- `ThreadSpawn` 是 crate 私有 trait，外部只能经闭包定制；`spawn_handler` 的实现因类型参数变化无法使用 `..self` 更新语法，只能逐字段搬运。

## 7. 下一步学习建议

本讲你已经能把一个池「建出来、配置好、起名字」。下一讲 **u7-l2（install 与多线程池协作）** 将回答「建多个池之后怎么用」：`ThreadPool::install` 如何把闭包送进指定池并同步等待、不同池之间任务为何互不窃取、以及跨池等待的经典死锁形态（`tests/cross-pool.rs`）。建议提前浏览 [rayon-core/src/thread_pool/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs) 中 `install` 的文档注释（尤其是那个 `one one two two` 的顺序示例）。如果想巩固本讲，可以再读一遍 `Registry::new`（[rayon-core/src/registry.rs:237-320](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L237-L320)），对照 u5-l4 的工作窃取内容，弄清 `ThreadBuilder` 里 `worker`/`stealer` 两个队列半边各自的用途。
