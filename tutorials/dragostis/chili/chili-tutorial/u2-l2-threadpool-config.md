# ThreadPool 生命周期与 Config 配置

## 1. 本讲目标

学完本讲，你应该能够：

- 准确说出 `Config` 中 `thread_count` 与 `heartbeat_interval` 的语义，特别是「实际 worker 数 = 配置值 − 1」这条规则及其背后的原因。
- 理解 `GLOBAL_THREAD_POOL` 如何用 `OnceLock` 实现全局线程池的惰性初始化，以及 `set_global` 为什么只能成功一次、失败时池如何「原样退回」。
- 逐步复述 `Drop` 中的停机顺序——持锁置位 `is_stopping` → `notify_all` / `notify_one` 唤醒 → 逐个 `join` 等待线程退出——并解释每一步为什么不能省、不能换序。
- 独立编写使用自定义 `Config` 的线程池程序，并亲手验证全局池的「一次性」语义。

本讲承接 u1-l3（会用 `Scope::global()` 和 `scope.join()`）与 u2-l1（知道 `join` 有顺序 / 心跳两条路径、心跳标志由专职线程置位）。上一讲我们站在 `Scope` 内部看 `join` 怎么分流；这一讲我们退回到 `Scope` 的「生产者」——`ThreadPool`——看这个对象本身是如何出生、如何被全局共享、如何优雅死去的。

## 2. 前置知识

本讲用到以下 Rust 标准库概念，先用大白话过一遍：

| 概念 | 一句话解释 |
|---|---|
| `OnceLock<T>` | 标准库（1.70 起）自带的「只能写入一次」的线程安全单元格：`set` 只有第一次成功，之后永远返回 `Err(原值)`；`get_or_init` 在第一次读取时才初始化。不需要 `unsafe`，也不需要 `lazy_static` / `once_cell` 这类外部 crate。 |
| `Condvar`（条件变量） | 让线程「睡着等信号」的原语：`wait(lock)` 会释放锁并睡眠，被 `notify_one` / `notify_all` 唤醒后重新加锁返回。经典纪律是：**改共享标志时要持锁，通知要在等待之前发出**，否则可能丢失唤醒。 |
| `Barrier`（栅栏） | 让 N 个线程在同一个点「集合」再一起放行的同步原语，构造时指定参与人数。 |
| `NonZero<usize>` | 「不为 0 的 usize」，类型层面保证 ≥ 1。本讲里它的作用是让 `x - 1` 这个减法不可能下溢。 |
| `Drop` trait | 值离开作用域（或被显式 `drop`）时自动调用的析构函数，Rust 的确定性资源释放机制。 |
| `Arc<T>` | 原子引用计数的共享指针，多个线程可以持有同一份 `Context`。 |
| `available_parallelism()` | 运行时探测当前进程可用的逻辑并行度（近似逻辑 CPU 数），返回 `NonZero<usize>`，某些环境下可能失败（返回 `Err`）。 |

另外回顾两个上一讲已建立的事实，本讲会反复用到：

- `join` 的两条执行路径中，**分支 `b` 永远由发起 `join` 的调用线程在本地执行**（`join_seq` 中先 `b(self)` 后 `a(self)`）。
- 心跳标志（`AtomicBool`）由一个**专职心跳线程**周期性置位，该线程自己不执行任何用户任务。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它占据了 `src/lib.rs` 的后半段：

| 行区间（src/lib.rs） | 内容 | 本讲角色 |
|---|---|---|
| L459-L476 | `Config` 结构体与默认值 | 模块 4.1 主角 |
| L478 | `static GLOBAL_THREAD_POOL: OnceLock<ThreadPool>` | 模块 4.2 主角 |
| L480-L486 | `ThreadPool` 结构体（三个字段） | 生命周期载体 |
| L497-L546 | `ThreadPool::new` / `with_config` | 模块 4.1：出生流程 |
| L548-L606 | `set_global` / `global` / `scope` | 模块 4.2：全局共享 |
| L609-L633 | `Default` 与 `impl Drop` | 模块 4.3：停机流程 |
| L112-L145 | `execute_worker`（worker 主循环） | 模块 4.3：被停机的一方 |
| L147-L189 | `execute_heartbeat`（心跳线程） | 模块 4.3：被停机的另一方 |
| L250-L267 | `Scope::global` 与 `new_from_thread_pool` | 模块 4.2：池与 Scope 的衔接 |
| L643-L654, L716-L747, L807-L813 | 三个使用 `Config` 的测试 | 实践参照 |

## 4. 核心概念与源码讲解

### 4.1 Config 参数：线程数是怎么定下来的

#### 4.1.1 概念说明

`Config` 是 `ThreadPool` 的全部可调参数，只有两个字段：

- **`thread_count: Option<NonZero<usize>>`**——配置「参与计算的总线程数」，**把发起 `join` 的调用线程自己也算在内**。传 `None` 时使用 `std::thread::available_parallelism()` 的探测值。线程池实际会 spawn 的 worker 数是「配置值 − 1」。
- **`heartbeat_interval: Duration`**——「任意一个线程上两次心跳之间的间隔」，默认 100 微秒。注意整个进程只有一个心跳线程，它会把该间隔除以当前心跳数来实现轮询均摊——细节属于 u3-l1，本讲只需要记住这个字段控制「多久尝试分享一次任务」。

为什么 worker 数要减一？因为 chili 的执行模型里**调用线程从不闲置**：`join_seq` 中 `b(self)` 先于 `a(self)` 由调用线程亲自执行；`join_heartbeat` 中也是调用线程先执行 `b`，再决定 `a` 是等结果还是收回本地执行。于是在 \( N \) 核机器上，「\( N-1 \) 个 worker + 1 个调用线程」恰好凑满 \( N \) 个计算线程，把 CPU 打满；再多 spawn 一个 worker 反而多一个抢不到活的闲置线程。chili 的卖点是低开销，「一个多余的线程都不养」正是这种抠门的体现。

用公式表示，设配置值（显式给定或探测到的并行度）为 \( C \)，则实际 worker 数：

\[ W = C - 1 \]

参与计算的线程数为 \( W + 1 = C \)（worker 加调用线程），另有 1 个心跳线程（不执行任务、绝大多数时间在睡眠）。

`NonZero<usize>` 在这里不是装饰：代码对配置值直接做 `get() - 1`，如果允许传入 0，`usize` 减一就会下溢（debug 构建下直接 panic）。`NonZero` 把「至少为 1」编码进类型，减法无需任何运行时检查就安全了。

#### 4.1.2 核心流程

`with_config` 的启动流程（伪代码）：

```text
with_config(config):
    C = config.thread_count ?? available_parallelism() ?? 兜底 1（随后减一得 0）
    W = C - 1                          # 实际 worker 数
    barrier = Barrier::new(W + 1)      # +1 = 构造线程自己也参与集合
    context = Arc::new(Context { Mutex<LockContext>, 两个 Condvar })

    对 i in 0..W:
        spawn 线程运行 execute_worker(context, barrier)

    barrier.wait()                     # 阻塞，直到所有 worker 完成首轮循环

    heartbeat = spawn 线程运行 execute_heartbeat(context, config.heartbeat_interval, W)

    return ThreadPool { context, worker_handles: [W 个句柄], heartbeat_handle: Some(heartbeat) }
```

三个值得注意的时序点：

1. **先 spawn worker，等它们全部就绪，最后才 spawn 心跳线程**——`with_config` 返回时，W 个 worker 已进入「等活」状态。
2. `Barrier::new(W + 1)` 中的 `+ 1` 是构造线程自己；worker 在首轮循环末尾到达栅栏（见 4.1.3），构造线程的 `barrier.wait()` 返回意味着「全员到齐、池可用了」。
3. `unwrap_or_default()` 兜底：`available_parallelism()` 探测失败时 worker 数为 0，池退化成「调用线程顺序执行 + 一个空转的心跳线程」，功能仍然完整——结果正确性从不依赖并行度（u1-l1 的「may 并行」语义）。

#### 4.1.3 源码精读

先看 `Config` 的定义与默认值：

> [src/lib.rs:459-467](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L459-L467)
>
> ```rust
> /// `ThreadPool` configuration.
> #[derive(Debug)]
> pub struct Config {
>     /// The number of threads or `None` to use
>     /// `std::thread::available_parallelism`.
>     pub thread_count: Option<NonZero<usize>>,
>     /// The interval between heartbeats on any particular thread.
>     pub heartbeat_interval: Duration,
> }
> ```

两个字段都是公开的，可以直接用结构体字面量构造，也可以配合 `..Default::default()` 只改一个字段。

> [src/lib.rs:469-476](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L469-L476)
>
> ```rust
> impl Default for Config {
>     fn default() -> Self {
>         Self {
>             thread_count: None,
>             heartbeat_interval: Duration::from_micros(100),
>         }
>     }
> }
> ```

默认值：不指定线程数（交给 `available_parallelism`），心跳间隔 100µs。

接着是 `ThreadPool` 结构体本身，三个字段分别是共享上下文、worker 句柄列表和心跳线程句柄：

> [src/lib.rs:480-486](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L480-L486)
>
> ```rust
> /// A thread pool for running fork-join workloads.
> #[derive(Debug)]
> pub struct ThreadPool {
>     context: Arc<Context>,
>     worker_handles: Vec<JoinHandle<()>>,
>     heartbeat_handle: Option<JoinHandle<()>>,
> }
> ```

`heartbeat_handle` 用 `Option` 包裹，是为了让 `Drop`（只拿得到 `&mut self`）能用 `take()` 把句柄 move 出来——这在 4.3 会看到。

`new()` 只是一层转发：

> [src/lib.rs:497-499](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L497-L499)
>
> ```rust
> pub fn new() -> Self {
>     Self::with_config(Config::default())
> }
> ```

真正的构造逻辑在 `with_config`。第一段是线程数的确定——本模块最关键的五行：

> [src/lib.rs:513-519](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L513-L519)
>
> ```rust
> pub fn with_config(config: Config) -> Self {
>     let thread_count = config
>         .thread_count
>         .or_else(|| thread::available_parallelism().ok())
>         .map(|thread_count| thread_count.get() - 1)
>         .unwrap_or_default();
>     let worker_barrier = Arc::new(Barrier::new(thread_count + 1));
> ```

逐行读：

- `config.thread_count.or_else(|| thread::available_parallelism().ok())`——用户给了就用用户的；没给就探测；探测也失败则是 `None`。
- `.map(|thread_count| thread_count.get() - 1)`——**注意：减一同时作用于用户显式指定的值和探测值**。所以 `Config { thread_count: Some(NonZero::new(2).unwrap()) }` 实际只创建 1 个 worker；`Some(1)` 则创建 0 个 worker。
- `.unwrap_or_default()`——全链路失败的兜底，worker 数为 0。
- `Barrier::new(thread_count + 1)`——这里的 `thread_count` 已经是 worker 数 `W`，`+ 1` 是构造线程自己。

第二段 spawn worker 并在栅栏上等它们就绪：

> [src/lib.rs:527-537](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L527-L537)
>
> ```rust
>     let worker_handles = (0..thread_count)
>         .map(|_| {
>             let context = context.clone();
>             let barrier = worker_barrier.clone();
>             thread::spawn(move || {
>                 execute_worker(context, barrier);
>             })
>         })
>         .collect();
>
>     worker_barrier.wait();
> ```

每个 worker 克隆一份 `Arc<Context>` 和栅栏句柄后启动。worker 首轮循环的结尾会到达栅栏（对应 `execute_worker` 中的 `first_run` 逻辑，见 [src/lib.rs:133-136](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L133-L136)），于是构造线程在 `worker_barrier.wait()` 上等到的是「W 个 worker 全部完成首轮、即将进入等活状态」这一事实——`with_config` 返回即代表池完全可用。

最后一段在返回前 spawn 心跳线程：

> [src/lib.rs:539-545](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L539-L545)
>
> ```rust
>     Self {
>         context: context.clone(),
>         worker_handles,
>         heartbeat_handle: Some(thread::spawn(move || {
>             execute_heartbeat(context, config.heartbeat_interval, thread_count);
>         })),
>     }
> }
> ```

注意心跳线程拿到的第三个参数是 worker 数 `thread_count`（即 `W`），它会被用来判断「当前是否没有额外的用户 Scope」——这在 u3-l1 展开。

库内测试提供了两种 `Config` 用法范式，可以直接参照：

> [src/lib.rs:648-654](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L648-L654)
>
> ```rust
> fn thread_pool_with_one_thread() {
>     let _tp = ThreadPool::with_config(Config {
>         thread_count: Some(NonZero::new(1).unwrap()),
>         ..Default::default()
>     });
> }
> ```

`..Default::default()` 只覆盖 `thread_count`、其余取默认——这是本仓库测试里最常见的写法（`join_wait` 见 [src/lib.rs:718-722](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L718-L722)，`concurrent_scopes` 见 [src/lib.rs:810-813](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L810-L813)）。`thread_pool_with_one_thread` 同时印证了 `Some(1)` → 0 个 worker 也能正常创建与销毁。

#### 4.1.4 代码实践

**实践目标**：验证「worker 数 = 配置值 − 1」以及心跳线程的存在，体会不同 `thread_count` 对耗时的定性影响。

**操作步骤**（以下均为示例代码，写在 chili 仓库之外的一个独立 crate 里，参照 u1-l3 的方式用 `path` 依赖引入 chili）：

1. 先在终端运行 `nproc`（或在程序里打印 `std::thread::available_parallelism()`），记下你机器的逻辑核数 \( N \)。
2. 在纸上推算下表并填空：

   | 配置 | worker 数 \( W \) | 线程池新建的线程总数（\( W + 1 \) 个心跳） |
   |---|---|---|
   | `None`（默认，核数 \( N \)） | ？ | ？ |
   | `Some(NonZero::new(1).unwrap())` | ？ | ？ |
   | `Some(NonZero::new(4).unwrap())` | ？ | ？ |

3. 编写如下程序（**示例代码**，非项目原有代码），对同一个负载用三种配置计时，并在 Linux 上用 `/proc/self/status` 直接数线程：

   ```rust
   use std::{
       num::NonZero,
       thread,
       time::{Duration, Instant},
   };

   use chili::{Config, Scope, ThreadPool};

   fn increment(s: &mut Scope, slice: &mut [u32]) {
       match slice.len() {
           0 => (),
           1 => slice[0] += 1,
           _ => {
               let (head, tail) = slice.split_at_mut(1);
               s.join(|_| head[0] += 1, |s| increment(s, tail));
           }
       }
   }

   // 仅 Linux 可用：读取当前进程的线程总数
   fn linux_thread_count() -> Option<u32> {
       std::fs::read_to_string("/proc/self/status")
           .ok()?
           .lines()
           .find(|l| l.starts_with("Threads:"))
           .and_then(|l| l.split_whitespace().nth(1).parse().ok())
   }

   fn main() {
       let configs: Vec<(&str, Config)> = vec![
           ("Some(1)", Config {
               thread_count: Some(NonZero::new(1).unwrap()),
               ..Default::default()
           }),
           ("Some(4)", Config {
               thread_count: Some(NonZero::new(4).unwrap()),
               ..Default::default()
           }),
           ("None", Config::default()),
       ];

       for (name, cfg_factory) in configs {
           // Config 未实现 Copy，每轮重新构造一份
           let cfg = match name {
               "Some(1)" => Config {
                   thread_count: Some(NonZero::new(1).unwrap()),
                   ..Default::default()
               },
               "Some(4)" => Config {
                   thread_count: Some(NonZero::new(4).unwrap()),
                   ..Default::default()
               },
               _ => Config::default(),
           };
           let _ = cfg_factory;

           let before = linux_thread_count();
           let start = Instant::now();

           let tp = ThreadPool::with_config(cfg);
           let during = linux_thread_count();

           let mut vals = vec![0u32; 1 << 20];
           increment(&mut tp.scope(), &mut vals);
           assert!(vals.iter().all(|&v| v == 1));

           drop(tp); // 显式触发停机（Drop），下一讲之前的预告
           let after = linux_thread_count();

           println!(
               "{name:>8}: 耗时 {:>10?} | 线程数 before={before:?} during={during:?} after={after:?}",
               start.elapsed()
           );
           let _ = thread::current(); // 保持 thread 导入被使用
       }
   }
   ```

   > 注：上面为了绕开 `Config` 不可 `Copy` 的一点小别扭用了 `match` 重建；你也可以简化成三段顺序代码，效果相同。

**需要观察的现象**：

- `during - before` 是否恰好等于 \( W + 1 \)（worker 数加一个心跳线程）。
- `drop(tp)` 之后 `after` 是否回落到 `before`（这说明 Drop 确实把线程全部 join 干净了——4.3 的伏笔）。
- 链式剥离的负载在 `Some(1)`（0 worker，纯顺序）与 `Some(4)` 之间的耗时差异。

**预期结果**：线程数的增减是确定性的（`Some(1)` 应新增 1 个线程、`Some(4)` 新增 4 个、默认配置新增 \( N \) 个）；耗时的具体数值取决于机器与负载形状，**待本地验证**。定性预期：默认配置明显快于 `Some(1)`；`Some(4)` 在核数足够的机器上介于两者之间。

#### 4.1.5 小练习与答案

**练习 1**：`Config { thread_count: Some(NonZero::new(1).unwrap()), ..Default::default() }` 会创建几个线程？`join` 还能正常工作吗？

**答案**：创建 1 个线程——0 个 worker 加 1 个心跳线程。所有计算（两个分支）都在调用线程上顺序执行，`join` 的结果依然正确，因为 chili 的语义是「may 并行」，结果正确性与是否真的跨线程无关（库内测试 [src/lib.rs:648-654](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L648-L654) 正是这样一个最小验证）。

**练习 2**：为什么 `thread_count` 的类型是 `Option<NonZero<usize>>` 而不是 `Option<usize>`？

**答案**：因为 [src/lib.rs:517](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L517) 对它执行 `thread_count.get() - 1`。如果允许 0，`usize` 的减法会下溢——debug 构建直接 panic、release 构建回绕成超大数进而 spawn 海量线程。`NonZero<usize>` 在类型层面保证值 ≥ 1，把这类错误在编译期排除，减法无需运行时检查。

**练习 3**：`available_parallelism()` 返回 `Err` 时（某些受限环境可能发生），`with_config` 会怎样？

**答案**：走 [src/lib.rs:518](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L518) 的 `unwrap_or_default()`，worker 数为 0。池仍然可用：一切在调用线程顺序执行，心跳线程照常空转。库选择了「优雅降级」而非 panic。

### 4.2 全局线程池与 OnceLock

#### 4.2.1 概念说明

u1-l3 里我们用过 `Scope::global()`——它背后的那个进程级单例线程池就是本模块的主角。设计一个「全局默认池」要回答两个问题：

1. **什么时候创建？** chili 的答案是惰性：第一次有人用 `global()` 时才创建，进程从不为「可能永远不用并行」的程序白白养一池线程。默认配置（`available_parallelism - 1` 个 worker、100µs 心跳）适合大多数场景。
2. **想换配置怎么办？** 答案是 `set_global`：在第一次使用之前，把你自己配置好的池「注册」为全局池。但 `OnceLock` 的语义决定了这件事只有一次机会——之后任何 `set_global` 都会失败，并把你的池**原封不动地退回来**（`Err(self)`），你既可以继续用它（它是个完全正常的池），也可以直接 drop 它触发停机。

「只能设置一次」不是偷懒，而是全局单例的固有约束：如果允许中途替换，正在用旧池的 `Scope` 和新池之间的一致性将无法维护。类似的「全局并发运行时只允许初始化一次」设计在其他并行库中也常见。

还有一个容易忽略的事实：**全局池永远不会被 Drop**。Rust 不会在进程退出时运行 `static` 变量的析构函数，所以 `GLOBAL_THREAD_POOL` 里的池会一直活到进程结束，worker 线程随进程被操作系统统一回收。这也是为什么库内测试 [src/lib.rs:643-646](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L643-L646) 用**局部**线程池来验证 Drop——全局池根本没有「死」的机会。

#### 4.2.2 核心流程

```text
进程启动
  │
  │  （可选，须在任何 global() 之前）
  ├─ tp.set_global()
  │     ├─ 首次：OnceLock 空 → 写入成功，Ok(())
  │     └─ 之后：OnceLock 已占用 → Err(tp 原样退回，池仍可用)
  │
  └─ 任意线程首次调用 Scope::global() / ThreadPool::global()
        └─ get_or_init(ThreadPool::new)
              ├─ OnceLock 已有值 → 直接返回 &'static 引用
              └─ OnceLock 为空   → 用默认 Config 创建池，写入后返回
```

两条铁律：

- **先到先得**：要么 `set_global` 抢在所有 `global()` 之前成功，要么 `global()`（用默认配置）先占坑、你的 `set_global` 必败。
- **并发安全免费获得**：两个线程同时首次调用 `global()`，`get_or_init` 保证只有一个线程执行初始化、另一个阻塞等待后拿到同一个引用，不需要手写任何锁。

#### 4.2.3 源码精读

全局池的载体是一个普通的 `static`：

> [src/lib.rs:478](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L478)
>
> ```rust
> static GLOBAL_THREAD_POOL: OnceLock<ThreadPool> = OnceLock::new();
> ```

`set_global` 消耗 `self`（把池的所有权移交给全局），直接转发 `OnceLock::set`：

> [src/lib.rs:565-567](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L565-L567)
>
> ```rust
> pub fn set_global(self) -> Result<(), Self> {
>     GLOBAL_THREAD_POOL.set(self)
> }
> ```

`OnceLock::set` 的签名恰好是 `Result<(), T>`：成功时值留在单元格内，失败时**把你传入的值原样还给你**——所以 `Err` 分支拿到的不是错误码，而是那个完整的 `ThreadPool`，可以继续 `scope()`、可以 drop。

`global` 用 `get_or_init` 实现惰性初始化，传入的是函数指针 `ThreadPool::new`：

> [src/lib.rs:584-586](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L584-L586)
>
> ```rust
> pub fn global() -> &'static ThreadPool {
>     GLOBAL_THREAD_POOL.get_or_init(ThreadPool::new)
> }
> ```

返回 `&'static ThreadPool`——这个引用在进程存活期间永远有效，也永远指向同一个实例。

池与 `Scope` 的衔接点在 `scope()`：

> [src/lib.rs:604-606](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L604-L606)
>
> ```rust
> pub fn scope(&self) -> Scope<'_> {
>     Scope::new_from_thread_pool(self)
> }
> ```

注意签名 `&self` → `Scope<'_>`：返回值的生命周期绑定在池的借用上。虽然内部只是克隆了 `Arc<Context>`（见 [src/lib.rs:254-267](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L254-L267)），但 API 层面借此表达了「Scope 不应活得比池久」的约定——配合 4.3 的 Drop，构成完整的生命周期闭环。`new_from_thread_pool` 还会在锁内注册一个心跳并 `notify_one` 唤醒心跳线程（[src/lib.rs:255-259](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L255-L259)），细节留给 u3-l1。

最后呼应 u1-l3：`Scope::global()` 只有一行——

> [src/lib.rs:250-252](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L250-L252)
>
> ```rust
> pub fn global() -> Scope<'static> {
>     ThreadPool::global().scope()
> }
> ```

`'static` 生命周期来自全局池的 `&'static` 引用——这就是「随手可得的并行作用域」的实现真相。

#### 4.2.4 代码实践

**实践目标**：验证三点——`global()` 返回同一实例、`set_global` 在 `global()` 之后必然失败、失败退回的池可正常使用且 drop 干净。

**操作步骤**：在独立 crate 的 `main.rs` 写入以下**示例代码**：

```rust
use chili::ThreadPool;

fn main() {
    // 1. 先访问全局池：触发惰性初始化（默认 Config）
    let p1 = ThreadPool::global();
    let p2 = ThreadPool::global();

    // 同一个 &'static 实例？
    assert!(std::ptr::eq(p1, p2));
    println!("global() 两次返回同一实例: {}", std::ptr::eq(p1, p2));

    // 2. 此时 OnceLock 已被占用，set_global 必然失败
    let mine = ThreadPool::new();
    let outcome = mine.set_global();
    assert!(outcome.is_err());
    println!("set_global 在 global() 之后调用: 失败（Err 携带原池）");

    // 3. 退回的池完全可用；语句结束时它被 drop，触发优雅停机
    if let Err(returned) = outcome {
        let mut s = returned.scope();
        let (a, b) = s.join(|_| 1u32, |_| 2u32);
        assert_eq!((a, b), (1, 2));
        println!("退回的池仍可执行 join");
    } // <- returned 在此 drop：若停机有死锁，程序会卡在这里

    println!("程序正常结束，说明 Drop 停机没有死锁");
}
```

**需要观察的现象**：三条 `println!` 依次输出；程序在「drop 退回的池」之后正常走到结尾，没有卡死。

**预期结果**：三行输出全部出现、程序退出码为 0。此程序的全部断言都是确定性的（`global()` 先占坑在先是代码写死的顺序），逻辑上必过；若你在本机运行发现卡死，那说明 Drop 有 bug——按 4.3 的分析这不应发生。具体运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：先调用了一次 `Scope::global()`，之后调用 `tp.set_global()` 会发生什么？如何让 `set_global` 成功？

**答案**：返回 `Err(tp)`。因为 `Scope::global()` 内部调用 `ThreadPool::global()`（[src/lib.rs:250-252](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L250-L252)），`get_or_init` 已用默认 `Config` 占据了 `OnceLock`。想让 `set_global` 成功，必须把它安排在进程内**任何** `global()` / `Scope::global()` 调用之前（例如 `main` 的第一行）。

**练习 2**：`GLOBAL_THREAD_POOL` 里那个池的 `Drop` 什么时候运行？

**答案**：永远不运行。Rust 不在进程退出时执行 `static` 的析构，worker 与心跳线程随进程被 OS 回收。正因如此，验证 `Drop` 的库内测试 [src/lib.rs:643-646](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L643-L646) 用的是函数内的局部池——离开作用域即触发 `Drop`。

**练习 3**：`set_global` 返回 `Err` 之后，被退回的那个池还能用吗？语义上它和 `Ok` 时留在全局的池有什么区别？

**答案**：完全能用——`Err` 变体携带的就是那个 `ThreadPool` 本身（[src/lib.rs:565-567](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L565-L567) 只是转发 `OnceLock::set`，失败时值原样归还）。区别仅在于它没有被注册为全局：之后 `ThreadPool::global()` 返回的仍是别人（或默认配置）的池。

### 4.3 Drop 停机流程

#### 4.3.1 概念说明

`ThreadPool` 被 drop 时，它创办的 W 个 worker 和 1 个心跳线程大多正阻塞在各自的 `Condvar::wait` 上呼呼大睡。`Drop` 的任务是把这些线程**全部、干净、不死锁地**送走，并且等到它们真的退出后（`join` 返回）才放行——否则「池没了、线程还在跑」会变成悬空状态。

这里要用到条件变量的两条经典纪律，上一讲的 `heartbeat()` 已经示范过一次（提交任务时 `notify_one`），这次是它的镜像场景「广播停机」：

1. **改共享标志必须持锁**。`is_stopping = true` 是在持有 `context.lock` 的情况下写入的。如果不持锁，可能出现这样的交错：worker 检查标志（未置位）→ 决定进入 `wait` → dropper 置位标志 → `notify_all`（此刻还没有人在等！）→ worker 才真正睡下。信号被丢掉，worker 永远不会醒。
2. **先唤醒，后等待**。必须先 `notify` 把阻塞中的线程叫醒，它们才能回到循环顶部看到 `is_stopping` 并退出，之后的 `join` 才有返回的一天；顺序颠倒等价于对着空房间喊话然后永远等门开。

另外注意唤醒的目标分两组，用两个不同的 `Condvar`：

- **所有 worker** 睡在 `job_is_ready` 上（平时只有任务提交才唤醒一个），停机时用 `notify_all` 全部叫醒。
- **唯一的心跳线程**睡在 `scope_created_from_thread_pool` 上，一个 `notify_one` 就够。

`join` 的顺序是先 worker、后心跳线程：worker 可能还在执行最后一个共享任务，必须先等它们；心跳线程只负责翻转原子标志，最后收尾。

#### 4.3.2 核心流程

dropper 一侧（伪代码）：

```text
drop(pool):
    加锁 context.lock:
        LockContext.is_stopping = true          # 持锁改标志
    job_is_ready.notify_all()                   # 唤醒全部 worker
    scope_created_from_thread_pool.notify_one() # 唤醒心跳线程

    对 worker_handles 中每个句柄: join()        # 先等 worker 退出
    heartbeat_handle.take() 后 join()           # 再等心跳线程退出
    # 全部 join 返回后，Arc<Context> 引用计数归零，共享上下文释放
```

worker 一侧的配合（`execute_worker` 主循环的收尾判断）：

```text
loop:
    取并执行一个共享任务（若有）
    首轮: barrier.wait()
    加锁
    若 is_stopping 或 wait(job_is_ready) 失败:
        break  # 退出线程
```

心跳线程一侧的配合：

```text
loop:
    在 scope_created_from_thread_pool 上 wait_while(没有新心跳 且 未停机)
    若 is_stopping: break
    ... 否则翻转到期的心跳标志，睡眠一小段
```

三方时序：

```text
dropper                     worker(s)                      heartbeat 线程
  │                           │（阻塞在 wait）                 │（阻塞在 wait_while）
  ├─ lock + is_stopping=true  │                              │
  ├─ notify_all ──────────────┼─► 醒来，回到循环顶部            │
  ├─ notify_one ──────────────┼──────────────────────────────┼─► 醒来，看到 is_stopping
  │                           ├─ 再捞一次共享任务并执行(若有)     ├─ break，线程退出
  ├─ join workers ◄───────────┼─ 看到 is_stopping，break        │
  ├─ join heartbeat ◄─────────┼───────────────────────────────┘
  └─ Drop 返回，池彻底死亡
```

#### 4.3.3 源码精读

`Drop` 全文只有十几行，但每一行都有讲究：

> [src/lib.rs:615-633](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L615-L633)
>
> ```rust
> impl Drop for ThreadPool {
>     fn drop(&mut self) {
>         self.context
>             .lock
>             .lock()
>             .expect("locking failed")
>             .is_stopping = true;
>         self.context.job_is_ready.notify_all();
>         self.context.scope_created_from_thread_pool.notify_one();
>
>         for handle in self.worker_handles.drain(..) {
>             handle.join().unwrap();
>         }
>
>         if let Some(handle) = self.heartbeat_handle.take() {
>             handle.join().unwrap();
>         }
>     }
> }
> ```

逐段解读：

- **L617-L621**：在持有 `context.lock` 的前提下把 `LockContext.is_stopping` 置为 `true`（锁在语句结束时释放）。`.expect("locking failed")` 意味着若互斥锁已中毒（某线程持锁时 panic），Drop 自己会 panic——实际很难发生，因为 chili 从不持锁执行用户任务。
- **L622**：`job_is_ready.notify_all()` 唤醒**所有**睡在这个条件变量上的 worker。对比上一讲 `heartbeat()` 提交任务时的 [src/lib.rs:328](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L328) 用的是 `notify_one`——来一个活叫一个人，全员退休才敲锣打鼓。
- **L623**：`scope_created_from_thread_pool.notify_one()` 唤醒睡在另一个条件变量上的**唯一**心跳线程。
- **L625-L627**：`drain(..)` 把 worker 句柄逐个移出 Vec 并 `join().unwrap()`——阻塞直到每个 worker 线程真正退出。`unwrap` 表示若某个 worker 线程自身 panic 过，Drop 也会 panic（极端情况下 double panic 直接 abort）；正常路径不会发生，因为用户任务的 panic 会被任务的执行框架捕获后经通道传回发起线程（u4-l2 详述），不会炸掉 worker。
- **L629-L631**：`heartbeat_handle.take()` 把 `Option` 里的句柄 move 出来再 `join`——这就是该字段用 `Option` 包裹的原因：`drop` 只拿得到 `&mut self`，不能直接移出字段，而 `Vec` 用 `drain` 达到同样效果。

再看被停机一方的代码。worker 主循环的收尾判断：

> [src/lib.rs:138-141](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L138-L141)
>
> ```rust
> let lock = context.lock.lock().ok()?;
> if lock.is_stopping || context.job_is_ready.wait(lock).is_err() {
>     break;
> }
> ```

两个细节：

- `lock.is_stopping` 的检查排在 `wait` **之前**——被 `notify_all` 唤醒的 worker 回到循环顶部（先顺手捞一次剩余共享任务并执行，见 [src/lib.rs:118-131](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L118-L131)），一查标志为真就 `break`，绝不会再陷进下一次 `wait`。这保证了停机信号不会被随后的等待吞掉。
- 两种退出途径用 `||` 并列：看到 `is_stopping` 主动退出；或 `wait` 返回 `Err`（互斥锁中毒）时被动退出（`lock().ok()?` 同理，拿不到锁就返回 `None` 结束线程）。

心跳线程一侧：

> [src/lib.rs:152-163](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L152-L163)
>
> ```rust
> loop {
>     let interval_between_workers = {
>         let mut lock = context
>             .scope_created_from_thread_pool
>             .wait_while(context.lock.lock().ok()?, |l| {
>                 l.heartbeats.len() == num_workers && !l.is_stopping
>             })
>             .ok()?;
>
>         if lock.is_stopping {
>             break;
>         }
> ```

`wait_while` 的谓词是「没有新注册的心跳且未停机」——Drop 置位 `is_stopping` 并 `notify_one` 后，谓词变假、`wait_while` 返回，紧接着的 `if lock.is_stopping { break; }` 让心跳线程退出。三条线程（dropper、worker、心跳）就是通过「同一个 `LockContext.is_stopping` 标志 + 两个条件变量」完成了这次三方握手。

#### 4.3.4 代码实践

**实践目标**：从进程外部「数线程」，直接观察 Drop 把 worker 与心跳线程全部 join 干净。

**操作步骤**（Linux 专用；其他平台缺少 `/proc`，此实践**待本地验证 / 不适用**，可退化为「程序不卡死即通过」）。在独立 crate 的 `main.rs` 写入以下**示例代码**：

```rust
use std::{num::NonZero, time::Duration};

use chili::{Config, Scope, ThreadPool};

fn increment(s: &mut Scope, slice: &mut [u32]) {
    match slice.len() {
        0 => (),
        1 => slice[0] += 1,
        _ => {
            let (head, tail) = slice.split_at_mut(1);
            s.join(|_| head[0] += 1, |s| increment(s, tail));
        }
    }
}

fn linux_thread_count() -> usize {
    std::fs::read_to_string("/proc/self/status")
        .unwrap()
        .lines()
        .find(|l| l.starts_with("Threads:"))
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap()
}

fn main() {
    let before = linux_thread_count();

    {
        // thread_count = Some(3) → worker 数 W = 2，另有 1 个心跳线程
        let tp = ThreadPool::with_config(Config {
            thread_count: Some(NonZero::new(3).unwrap()),
            heartbeat_interval: Duration::from_micros(100),
        });

        let mut vals = [0u32; 4_096];
        increment(&mut tp.scope(), &mut vals);
        assert_eq!(vals, [1; 4_096]);

        let during = linux_thread_count();
        println!("before={before} during={during} （预期差值 = 3）");
    } // <- tp 在块尾被 drop：is_stopping → notify → join

    let after = linux_thread_count();
    println!("after={after}（预期回落到 before）");
    assert_eq!(before, after, "Drop 之后线程数应回到基线");
}
```

**需要观察的现象**：`during - before` 等于 3（2 个 worker + 1 个心跳）；块结束后 `after` 与 `before` 相等；程序带着断言正常退出。

**预期结果**：断言通过。线程计数的增减是确定性的；唯一的不确定因素是运行时自带的辅助线程（如某些 allocator 的后台线程），若你的环境有这类干扰，差值可能不为 3，但「drop 后回落到基线」应稳定成立。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果 `Drop` 只置位 `is_stopping` 而忘记调用两个 `notify`，会发生什么？

**答案**：睡在 `job_is_ready.wait` 和 `scope_created_from_thread_pool.wait_while` 里的线程永远不会醒——它们醒来后才有机会检查 `is_stopping`。于是 `Drop` 里的 `handle.join()` 永不返回，dropping 线程死锁，程序卡死在池离开作用域的地方。

**练习 2**：为什么必须「先 notify、后 join」，反过来写不行吗？

**答案**：不行。`join` 等待线程退出，而线程退出依赖它们先被唤醒、回到循环顶部看到 `is_stopping`。先 `join` 后 `notify` 的执行顺序意味着在所有线程还睡着时就开始等待它们退出——等价于上一题的死锁，`notify` 永远没机会执行。

**练习 3**：worker 循环里 `lock.is_stopping || context.job_is_ready.wait(lock).is_err()` 把「查标志」放在「等待」之前，这个顺序有什么意义？

**答案**：它保证每次回到循环顶部都先看停机标志再决定是否继续睡。配合 dropper 侧「持锁改标志、持锁后 notify」的纪律，两种交错都安全——worker 要么在 dropper 拿锁前已进入 `wait`（会被 notify 唤醒），要么在 dropper 释放锁后加锁、一查标志就退出。标志检查在前使得唤醒信号不会被一次新的 `wait` 无限期吞没。

## 5. 综合实践

把本讲三个模块串成一个完整任务：**编写一个测试，用指定 `Config` 创建线程池并执行若干 `join`，然后连续两次调用 `set_global`，验证第二次返回 `Err` 且退回的池仍然可用。**

### 5.1 环境准备

在 chili 仓库**同级**目录创建一个独立测试 crate（不要改动 chili 仓库本身）：

```bash
cd ..                              # 与 dragostis-chili 同级
cargo new chili-pool-lab
cd chili-pool-lab
```

编辑 `Cargo.toml`（路径按你的实际目录调整）：

```toml
[package]
name = "chili-pool-lab"
version = "0.1.0"
edition = "2021"

[dependencies]
chili = { path = "../dragostis-chili" }
```

### 5.2 编写测试

新建 `tests/pool.rs`（**示例代码**）：

```rust
use std::{num::NonZero, time::Duration};

use chili::{Config, Scope, ThreadPool};

// 与库内 join_long 同款的链式剥离负载
fn increment(s: &mut Scope, slice: &mut [u32]) {
    match slice.len() {
        0 => (),
        1 => slice[0] += 1,
        _ => {
            let (head, tail) = slice.split_at_mut(1);
            s.join(|_| head[0] += 1, |s| increment(s, tail));
        }
    }
}

#[test]
fn custom_pool_and_set_global_only_once() {
    // ── 模块 4.1：自定义 Config 创建线程池并执行若干 join ──
    // thread_count = Some(2) → 实际 worker 数 = 1；心跳间隔 1µs
    // （这正是库内 join_wait 测试采用的配置，见 src/lib.rs:718-722）
    let tp = ThreadPool::with_config(Config {
        thread_count: Some(NonZero::new(2).unwrap()),
        heartbeat_interval: Duration::from_micros(1),
    });

    let mut vals = [0; 1_024];
    increment(&mut tp.scope(), &mut vals);
    assert_eq!(vals, [1; 1_024]);

    // ── 模块 4.2：第一次 set_global，应成功 ──
    tp.set_global().unwrap();

    // ── 第二次 set_global，应返回 Err，且 Err 里就是第二个池本身 ──
    let tp2 = ThreadPool::with_config(Config {
        thread_count: Some(NonZero::new(1).unwrap()), // worker 数 = 0
        heartbeat_interval: Duration::from_micros(100),
    });

    let returned = tp2.set_global().unwrap_err();

    // ── 模块 4.3：退回的池完全可用；还能继续跑 join ──
    let mut vals2 = [0; 16];
    increment(&mut returned.scope(), &mut vals2);
    assert_eq!(vals2, [1; 16]);
    // returned 在测试结束时 drop：若停机死锁，本测试会卡死超时

    // ── 全局池仍是第一个池，可继续使用 ──
    let mut vals3 = [0; 512];
    increment(&mut ThreadPool::global().scope(), &mut vals3);
    assert_eq!(vals3, [1; 512]);
}
```

运行：

```bash
cargo test
```

### 5.3 预期结果与观察点

- 测试输出 `test custom_pool_and_set_global_only_once ... ok`，1 passed。
- `unwrap_err()` 那一行能拿到值，本身就证明第二次 `set_global` 走的是 `Err` 分支；`returned` 还能执行 `join` 并得到正确结果，证明「返回原池」不是一句空话。
- 测试能正常退出（不超时），说明 `returned` 与各池的 `Drop` 停机流程没有死锁。
- 两个易踩的坑：
  1. `OnceLock` 是**进程级**的——同一个测试二进制里只能有**一个**测试碰全局池（`set_global` 或 `global()`）。想加更多测试，请只让它们使用局部 `ThreadPool`。
  2. 若把本测试搬进 chili 仓库自己的测试模块里跑，注意仓库的其他测试进程内是否已初始化全局池；独立 crate 则没有这个顾虑（每个 `cargo test` 都是全新进程）。

具体输出文本以本地为准，**待本地验证**。

## 6. 本讲小结

- `Config` 只有两个参数：`thread_count` 表示「参与计算的总线程数（含调用线程）」，实际 worker 数 \( W = C - 1 \)，减一是为了和「永不闲置的调用线程」恰好凑满 \( C \) 个计算线程；`NonZero<usize>` 在类型层面保证减法不下溢。`heartbeat_interval` 默认 100µs，控制任务分享的节奏。
- `with_config` 的启动时序：spawn W 个 worker → 在 `Barrier(W+1)` 上等全员就绪 → 最后 spawn 心跳线程；`available_parallelism` 失败时优雅降级为 0 个 worker。
- `GLOBAL_THREAD_POOL` 用 `OnceLock` 实现惰性初始化与「只能设置一次」：`global()` 走 `get_or_init`，`set_global` 失败时通过 `Err(self)` 把池原样退回；全局池永远不会被 Drop，static 的析构不随进程退出运行。
- `Drop` 的停机三步曲严格有序：**持锁**置位 `is_stopping` → `notify_all` 唤醒所有 worker、`notify_one` 唤醒心跳线程 → 先 join 全部 worker、再 join 心跳线程；worker 侧「先查标志再等待」与之配合，杜绝丢失唤醒与死锁。
- `tp.scope()` 的签名（`&self` → `Scope<'_>`）在 API 层面约定了 Scope 不活得比池久，与 Drop 一起构成完整的生命周期闭环。

## 7. 下一步学习建议

下一讲 **u2-l3（worker 线程与共享上下文）** 将钻进本讲反复擦肩而过的 `execute_worker` 主循环与 `Context` / `LockContext` 结构，弄清 worker 被唤醒之后如何用 `BTreeMap` 按投递时间公平地取任务、`Mutex + Condvar` 的等待-通知模型全貌，以及首轮 `Barrier` 同步的细节。之后再进入 u3-l1 深挖本讲留给伏笔的两处：`scope()` 注册心跳时为何要 `notify_one`、心跳线程的 `wait_while` 谓词为什么恰好是 `heartbeats.len() == num_workers`。建议阅读顺序：[src/lib.rs:112-145](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L112-L145)（`execute_worker`）→ [src/lib.rs:105-110](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L105-L110)（`Context`）→ [src/lib.rs:73-103](https://github.com/dragostis/chili/blob/6c49338d25f1656ba1cdd0b9b21479cd4207652f/src/lib.rs#L73-L103)（`LockContext`）。
