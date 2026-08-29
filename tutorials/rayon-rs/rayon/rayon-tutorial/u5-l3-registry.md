# Registry：线程注册表

## 1. 本讲目标

本讲是单元五的第三讲。前两讲已经搞清楚了「任务长什么样」（Job 与 JobRef）和「任务完成怎么通知」（Latch 家族），但一直回避了一个问题：**这些任务最终被谁保管、线程从哪里来、什么时候创建？**

答案就是 `Registry`——rayon-core 的线程注册表，也是整个调度体系的"户口管理中心"。学完本讲，你应该能够：

1. 说出全局线程池（global registry）的惰性初始化流程：谁触发、如何保证只初始化一次、失败怎么办。
2. 画出从 `Registry::new` 到工作线程 `main_loop` 的完整启动链路，并说出 `ThreadBuilder`、`DefaultSpawn`、`CustomSpawn` 三者的关系。
3. 准确复述默认线程数的优先级规则：显式 `num_threads()` > `RAYON_NUM_THREADS` > 废弃的 `RAYON_RS_NUM_CPUS` > 逻辑核数，以及 `max_num_threads()` 软上限的来历。
4. 解释 registry 何时被释放：全局池永不释放，用户池随 `ThreadPool::drop` 终止。

## 2. 前置知识

本讲需要以下基础概念，均用通俗语言解释：

- **`Arc<T>`（原子引用计数的共享所有权）**：多个所有者共享同一份 `Registry`，最后一个释放者负责回收。每个工作线程都持有 `Arc<Registry>`，所以只要还有线程活着，registry 就活着。
- **`std::sync::Once` / `call_once`**：标准库提供的"只执行一次"原语。多个线程同时调用 `call_once`，只有一个闭包真正运行，其余阻塞等待。这是全局线程池只初始化一次的保证。
- **`thread_local!`**：线程局部存储。每个线程有自己独立的一份变量。rayon 用它存放"当前线程的 `WorkerThread` 指针"，从而让任意代码都能问一句"我现在是不是池内线程"。
- **crossbeam-deque 三件套**（u5-l1 已接触）：`Worker` 是双端队列的拥有端（本地 push/pop），`Stealer` 是窃取端（别人从另一头偷），`Injector` 是全局注入队列（池外线程投递任务用）。
- **Latch 家族**（u5-l2 已精读）：单向一次性信号。本讲会用到 `LockLatch`（阻塞等待，用于 primed/stopped）和 `OnceLatch`（免循环引用的一次性触发，用于 terminate）。
- **环境变量**：进程启动时由操作系统传入的字符串。`RAYON_NUM_THREADS` 就是 rayon 约定的一个环境变量，用于在不改代码的情况下控制线程数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rayon-core/src/registry.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs) | 本讲主战场：`Registry`、`WorkerThread`、`ThreadBuilder`、全局池初始化、`main_loop` 全部在此 |
| [rayon-core/src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs) | `ThreadPoolBuilder` 及其全部配置项、`get_num_threads` 优先级链、`max_num_threads`、`current_num_threads` |
| [rayon-core/src/thread_pool/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs) | `ThreadPool`——外界握住 `Arc<Registry>` 的公开句柄，`Drop` 时触发终止 |
| [rayon-core/src/sleep/counters.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs) | `THREADS_MAX` 常量的定义处，解释线程数软上限的位数来源 |
| [src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs) | 上层 rayon crate 的再导出清单，用户侧 API 的真实入口 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**全局 registry（惰性初始化）**、**线程启动流程**、**默认线程数规则**。

### 4.1 全局 Registry：线程池的单例与惰性初始化

#### 4.1.1 概念说明

读者在前几讲已经多次看到「全局线程池」这个词：直接调用 `rayon::join`、`rayon::spawn`、`par_iter()` 而不建自定义池时，任务就落在全局池里。它有三个关键性质：

1. **惰性创建**：rayon 不在程序启动时创建线程，而是在**第一次真正需要池的瞬间**才创建。写了一个只导入了 rayon 但从未并行计算的程序，一个工作线程都不会有。
2. **全局唯一**：整个进程只有一个全局池，由 `Once` 保证；二次配置会得到 `GlobalPoolAlreadyInitialized` 错误。这也是 rayon-core 用 `links` 机制禁止多版本共存的原因（u1-l2 已介绍）。
3. **永不销毁**：全局池的 `Arc` 被静态变量攥着永不放手，工作线程活到进程退出。

「查询即初始化」是惰性创建的直接推论：连 `rayon::current_num_threads()` 这样看似无害的只读查询，也会在全局池尚不存在时把它创建出来——因为不创建就没有答案可查。

#### 4.1.2 核心流程

全局池的获取流程（伪代码）：

```text
任意顶层 rayon 调用（join / spawn / par_iter / current_num_threads）
    └─> 需要拿到全局 Registry
        └─> global_registry()
            ├─ THE_REGISTRY 已初始化？ ──是──> 直接返回 &'static Arc<Registry>
            └─ 否 ──> set_global_registry(default_global_registry)
                 ├─ THE_REGISTRY_SET.call_once：
                 │     ├─ Registry::new(默认 ThreadPoolBuilder)
                 │     ├─ 成功 → 写入 THE_REGISTRY，返回 &'static 引用
                 │     └─ 失败 → result 保持 Err，THE_REGISTRY 仍为 None
                 └─ call_once 已被别人执行过（本调用者来晚了）
                       └─ 重读 THE_REGISTRY：有则返回，无则 expect panic
```

特殊分支：在线程根本不被支持的环境（如 `wasm32-unknown-unknown`），默认创建失败且错误类型为 `Unsupported` 时，回退为「复用当前线程的单线程池」。

#### 4.1.3 源码精读

先看 `Registry` 结构体本身，它就是线程池的全部共享状态：

> [rayon-core/src/registry.rs:126-149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L126-L149) —— `Registry` 的七个字段：每线程信息表 `thread_infos`、睡眠唤醒控制器 `sleep`（u5-l5 专题）、池外注入队列 `injected_jobs`、广播队列组 `broadcasts`（u6-l3 专题）、三个用户回调，以及核心的 `terminate_count` 终止计数器。

`terminate_count` 的注释块值得逐句读：

> [rayon-core/src/registry.rs:135-148](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L135-L148) —— 注释列出了「计数归零意味着池内所有工作完成」的四条保障：全局池有一份永不释放的引用；用户池存活期间持有引用；`install` 注入阻塞任务时 `ThreadPool` 句柄持引用；`join`/`scope` 总是被某个更外层的任务管辖。

接着是两个静态变量——全局单例的物理载体：

> [rayon-core/src/registry.rs:154-155](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L154-L155) —— `static mut THE_REGISTRY: Option<Arc<Registry>>` 存放单例，`THE_REGISTRY_SET: Once` 负责只初始化一次。

这段代码用了 `static mut` 而非现代的 `OnceLock`，安全性完全依赖 `Once` 建立的 happens-before 关系，`SAFETY` 注释里写得明明白白：

> [rayon-core/src/registry.rs:160-170](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L160-L170) —— `global_registry()` 的实现：先尝试 `set_global_registry(default_global_registry)`；若返回 `Err`（说明 `call_once` 已被别人执行过），再在 `or_else` 里安全地重读 `THE_REGISTRY`。注意 `debug_assert!(THE_REGISTRY_SET.is_completed())`——此刻 `call_once` 必然已完成，之后不会再有任何可变访问，所以取共享引用是安全的。

`set_global_registry` 是"只许成功一次"的执行者：

> [rayon-core/src/registry.rs:185-205](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L185-L205) —— `result` 预置为 `GlobalPoolAlreadyInitialized` 错误；只有 `call_once` 真正选中本闭包时才会运行 `registry()` 回调并写入静态变量。于是：第一个调用者拿到初始化结果，迟到的调用者拿到预置错误，再由上层 `global_registry()` 转换成读现有单例。

默认配置与回退逻辑：

> [rayon-core/src/registry.rs:207-226](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L207-L226) —— `default_global_registry()` 用**全新的默认 `ThreadPoolBuilder`** 建池；若错误是"线程不支持"（典型如 wasm）且当前线程不在任何池中，则改用 `num_threads(1).use_current_thread()` 建单线程回退池。这正对应 [rayon-core/src/lib.rs:22-37](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L22-L37) 文档描述的"全局回退模式"。

用户想**主动**配置全局池时走 `build_global`：

> [rayon-core/src/lib.rs:273-277](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L273-L277) —— `build_global` 内部调用 `registry::init_global_registry(self)`，随后 `wait_until_primed()` 等所有工作线程就绪（为基准测试提供稳定起点）。

> [rayon-core/src/registry.rs:174-181](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L174-L181) —— `init_global_registry` 把**用户给定的 builder**（而非默认 builder）塞进同一个 `set_global_registry` 流程。注意它同样受 `Once` 约束：若全局池已被隐式初始化，这里返回 `Err`。

#### 4.1.4 代码实践

**实践目标**：亲眼验证「全局池只初始化一次」与「隐式使用会抢占初始化权」。

**操作步骤**（示例代码，非项目源码）：

1. 新建独立 Cargo 项目 `registry-lab`，`Cargo.toml` 加 `rayon = "1"`。
2. `src/main.rs` 写入：

```rust
use rayon::ThreadPoolBuilder;

fn main() {
    // 第一次 build_global：预期 Ok
    match ThreadPoolBuilder::new().num_threads(4).build_global() {
        Ok(()) => println!("第一次 build_global 成功"),
        Err(e) => println!("第一次 build_global 失败: {e}"),
    }

    // 第二次 build_global：预期 Err(GlobalPoolAlreadyInitialized)
    match ThreadPoolBuilder::new().num_threads(8).build_global() {
        Ok(()) => println!("第二次 build_global 成功"),
        Err(e) => println!("第二次 build_global 失败: {e}"),
    }

    // 线程数仍是第一次的 4，而不是 8
    println!("current_num_threads = {}", rayon::current_num_threads());
}
```

3. 再做一个对照实验：把第一次 `build_global` 换成任意一次隐式使用（例如 `rayon::join(|| 1, || 2);`），然后再执行 `num_threads(4)` 的 `build_global`。

**需要观察的现象**：第二次 `build_global` 的错误信息文本；两种实验顺序下最终 `current_num_threads` 的值。

**预期结果**（依据 [rayon-core/src/lib.rs:746-747](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L746-L747) 的错误文案 "The global thread pool has already been initialized."）：第一种顺序输出 4；对照实验中 `build_global` 报已初始化错误，线程数取决于隐式初始化时的默认规则（见 4.3）。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `global_registry()` 里对 `static mut` 的读取不算数据竞争？

**参考答案**：因为读取发生在 `THE_REGISTRY_SET.call_once` 之后（要么本线程就是执行者，要么 `call_once` 已同步返回）。`Once` 保证了写入 happens-before 后续所有读取，且初始化成功后不再有任何可变访问——这正是代码中 `SAFETY` 注释陈述的不变量。

**练习 2**：如果 `Registry::new` 在全局初始化时因线程创建失败而返回 `Err`，随后再次调用 `rayon::join` 会发生什么？

**参考答案**：`call_once` 已经消耗掉（失败的闭包也算执行过），`THE_REGISTRY` 仍是 `None`，于是 `global_registry()` 中 `the_registry.as_ref().ok_or(err)` 仍为 `Err`，最终 `.expect("The global thread pool has not been initialized.")` 引发 panic。失败不会被重试。

**练习 3**：全局池的 `Arc<Registry>` 为什么永远不会归零？

**参考答案**：`THE_REGISTRY` 静态变量持有的那份引用从不释放（[rayon-core/src/registry.rs:138-139](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L138-L139) 注释明言），所以引用计数至少为 1，registry 与其工作线程伴随进程终生。

### 4.2 线程启动流程：从 Registry::new 到 main_loop

#### 4.2.1 概念说明

这一模块回答三个问题：

1. **线程怎么来？** `Registry::new` 为每个下标构造一个 `ThreadBuilder`（打包了名字、栈大小、专属队列、registry 引用、下标），再交给一个"启动器"去真正开线程。启动器有两种：默认的 `DefaultSpawn`（用 `std::thread::Builder`）和用户自定义的 `CustomSpawn`（`spawn_handler` 的回调）。三者关系是：`ThreadSpawn` 是 crate 私有 trait 定义"启动一个线程并让它跑 `run()`"的契约，`DefaultSpawn` 与 `CustomSpawn` 是它的两个实现，`ThreadPoolBuilder<S>` 的泛型参数 `S` 默认就是 `DefaultSpawn`。
2. **线程起来后干什么？** 进入 `main_loop`：注册自己、点亮 primed 锁存器、跑用户 start 回调，然后阻塞在 terminate 锁存器上"等死讯"——期间被 `wait_until` 的循环驱动着干活（u5-l1 已剖析过这个"边等边帮工"循环）。
3. **池怎么散伙？** `terminate_count` 归零时逐线程 set terminate 锁存器，各线程醒来收尾退出。

#### 4.2.2 核心流程

`Registry::new` 的六步：

```text
1. n = min(builder.get_num_threads(), max_num_threads())   // 软上限
2. 为 0..n 各建一对 (job Worker, job Stealer)              // LIFO 默认，breadth_first 时 FIFO
   为 0..n 各建一对 (broadcast Worker, broadcast Stealer)   // 恒为 FIFO
3. 组装 Registry{ thread_infos(装 job Stealer), sleep, injector,
                  broadcasts(装 broadcast Worker), terminate_count = 1 }
   并包进 Arc
4. 安放 Terminator 守卫（中途失败则触发 terminate 收摊）
5. 逐下标构造 ThreadBuilder{ name, stack_size, registry 克隆,
                              job Worker, broadcast Stealer, index }
   交给 spawn handler 启动；index==0 且 use_current_thread 时例外：
   直接征用当前线程（不跑 main_loop）
6. 全部成功 → mem::forget(Terminator)；返回 Arc<Registry>
```

工作线程的一生：

```text
新 OS 线程启动 → ThreadBuilder::run()
  → main_loop:
      1. 栈上构造 WorkerThread 并写入 thread-local
      2. set primed 锁存器（向 registry 报到）
      3. 安放 AbortIfPanic 守卫（内核代码 panic 即 abort）
      4. 若有 start_handler 则回调
      5. wait_until_out_of_work：阻塞在自己的 terminate 锁存器上，
         期间经 wait_until 循环取活/窃取/睡眠（u5-l1、u5-l5）
      6. set stopped 锁存器，回调 exit_handler，线程结束
```

终止协议：\[ \text{terminate\_count} = 1 + \#\{\text{存活的异步 spawn 任务}\} \]，`ThreadPool::drop` 调 `terminate()` 使其减一；减到 0 的那次调用负责通知所有线程的 terminate 锁存器。

#### 4.2.3 源码精读

先看定制点 `ThreadBuilder`——它是 `spawn_handler` 回调收到的参数：

> [rayon-core/src/registry.rs:21-52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L21-L52) —— `ThreadBuilder` 私藏六个字段；对外只暴露查询方法 `index()`/`name()`/`stack_size()` 与关键的 `run()`。文档注明 `run()` **不会返回，直到线程池被 drop**——自定义启动器最终必须调用它，线程才算入队服役。

启动器契约与两个实现：

> [rayon-core/src/registry.rs:65-73](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L65-L73) —— crate 私有 trait `ThreadSpawn`：接收 `ThreadBuilder`，负责开线程并让它执行 `ThreadBuilder::run()`。

> [rayon-core/src/registry.rs:84-96](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L84-L96) —— `DefaultSpawn`：把名字与栈大小搬进 `std::thread::Builder` 后 `b.spawn(|| thread.run())`。这是不配置时的默认路径。

> [rayon-core/src/registry.rs:107-124](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L107-L124) —— `CustomSpawn<F>` 只是用户回调 `F: FnMut(ThreadBuilder) -> io::Result<()>` 的包装。而把 `ThreadPoolBuilder<DefaultSpawn>` 换成 `ThreadPoolBuilder<CustomSpawn<F>>` 的正是 [rayon-core/src/lib.rs:429-445](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L429-L445) 的 `spawn_handler` 方法——这就是"泛型参数 S"的切换机制，三个类型的关系全部落在这三段代码里。

`Registry::new` 的关键片段（有删减）：

> [rayon-core/src/registry.rs:243-244](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L243-L244) —— 线程数先与 `max_num_threads()` 取小，注释称之为"软限制"（soft-limit）。

> [rayon-core/src/registry.rs:248-267](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L248-L267) —— 两轮队列创建：job 队列按 `breadth_first` 选 FIFO/LIFO（默认 LIFO，即深度优先，呼应 u5-l1 中"B 入队后先做 A"的行为）；broadcast 队列恒为 FIFO。

> [rayon-core/src/registry.rs:269-281](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L269-L281) —— 组装 `Registry` 并 `Arc::new`；`terminate_count` 初始化为 1。**注意一个易错点**：`thread_infos` 装的是 **job 队列的 Stealer**（供别的线程窃取），而 `broadcasts` 装的是 broadcast 队列的 Worker 端。随后安放 `Terminator` 守卫。

> [rayon-core/src/registry.rs:228-234](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L228-L234) —— `Terminator` 的 `Drop` 会调用 `registry.terminate()`。它的用途在 [rayon-core/src/registry.rs:280-281](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L280-L281) 的注释里："如果提前返回或 panic，确保已启动的线程被终止"——比如开了 3 个线程后第 4 个 spawn 失败，前 3 个必须被叫停收摊。

> [rayon-core/src/registry.rs:283-314](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L283-L314) —— 启动主循环：`workers.zip(broadcast_stealers)` 逐个构造 `ThreadBuilder`（所以 `WorkerThread.stealer` 字段实为 broadcast 队列的窃取端，见 [rayon-core/src/registry.rs:651-652](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L651-L652) 的注释）；`index == 0 && use_current_thread` 分支先检查当前线程不在别的池（否则 `CurrentThreadAlreadyInPool` 错误），然后 `Box::into_raw` 泄漏式征用当前线程、只设 primed 不跑 main_loop；其余下标统一走 `builder.get_spawn_handler().spawn(thread)`。

> [rayon-core/src/registry.rs:316-319](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L316-L319) —— 全部成功后 `mem::forget(t1000)` 撤掉守卫，正常返回 `Arc<Registry>`。

工作线程身份与主循环：

> [rayon-core/src/registry.rs:665-672](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L665-L672) —— `WorkerThread` 分配在工作线程自己的栈上，存进 `thread_local` 的 `Cell<*const WorkerThread>`；用裸指针避免 `RefCell` 开销，安全性靠"线程完全展开前指针有效"保证。

> [rayon-core/src/registry.rs:697-713](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L697-L713) —— `WorkerThread::current()` 返回裸指针（空指针表示非池内线程）；`set_current` 在线程启动时注册自己。这两个函数是上一讲 `join` 里"判断自己在不在池里"的底层支撑。

> [rayon-core/src/registry.rs:913-944](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L913-L944) —— `main_loop` 全文：构造并注册 `WorkerThread` → set primed → 安放 `AbortIfPanic` 守卫（注释言明：工作线程自身 panic 意味着池内状态已坏，直接 abort；**用户代码**的 panic 会被逐任务捕获而不至于此）→ 回调 `start_handler` → `wait_until_out_of_work()` → 撤守卫 → 回调 `exit_handler`。

> [rayon-core/src/registry.rs:819-833](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L819-L833) —— `wait_until_out_of_work`：等的是本线程 `ThreadInfo.terminate` 锁存器；醒来后断言本地队列已空、set stopped 锁存器通知 registry。

每线程档案 `ThreadInfo` 与终止协议：

> [rayon-core/src/registry.rs:613-631](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L613-L631) —— `ThreadInfo` 四件套：`primed`（已就绪，基准测试用）、`stopped`（已退出，测试用）、`terminate`（终止信号）、`stealer`（job 队列窃取端——别的线程 `steal()` 时访问的就是它，见 [rayon-core/src/registry.rs:875-908](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L875-L908)）。

> [rayon-core/src/registry.rs:565-589](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L565-L589) —— `increment_terminate_count` 的文档详细解释了哪些 API 需要动计数：阻塞式的 `join`/`scope` 不需要（外层上下文已持有引用），例外是 `::spawn()`——异步任务自己持有计数并在结束时调用 `terminate()` 平衡。

> [rayon-core/src/registry.rs:594-600](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L594-L600) —— `terminate()`：`fetch_sub` 后若恰好减到 0，逐线程 `OnceLatch::set_and_tickle_one` 点亮 terminate 锁存器并唤醒。

最后是释放时机：外界唯一的公开句柄 `ThreadPool` 只是对 `Arc<Registry>` 的包装：

> [rayon-core/src/thread_pool/mod.rs:46-48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L46-L48) —— `ThreadPool { registry: Arc<Registry> }`，印证 u1-l4 的结论：`Registry` 是 `pub(super)` 的，外界只能经 `ThreadPool` 触达。

> [rayon-core/src/thread_pool/mod.rs:399-403](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/thread_pool/mod.rs#L399-L403) —— `Drop for ThreadPool` 只做一件事：`self.registry.terminate()`。计数归零 → 各线程收到终止信号 → 退出时各自的 `Arc` 克隆释放 → 最后一个 `Arc` 落地时 `Registry` 真正析构。全局池则因静态引用永不走这条路。

#### 4.2.4 代码实践

**实践目标**：让"线程启动与退出流程"变成肉眼可见的事件流。

**操作步骤**（示例代码，非项目源码）：

```rust
use rayon::ThreadPoolBuilder;

fn main() -> Result<(), rayon::ThreadPoolBuildError> {
    let pool = ThreadPoolBuilder::new()
        .num_threads(3)
        .thread_name(|i| format!("my-worker-{i}"))
        .start_handler(|i| println!("[start] 线程 {i}（{:?}）上线",
                                    std::thread::current().id()))
        .exit_handler(|i| println!("[exit ] 线程 {i} 下线"))
        .build()?;

    pool.install(|| {
        println!("install 内 current_num_threads = {}", rayon::current_num_threads());
        println!("install 内线程名 = {:?}",
                 std::thread::current().name().unwrap());
    });

    drop(pool); // 触发 terminate，等待各线程退出
    println!("pool 已销毁");
    Ok(())
}
```

**需要观察的现象**：start 事件的数量与顺序；`install` 内打印的线程名；drop 之后 exit 事件是否在 "pool 已销毁" 之前全部出现。

**预期结果**：3 条 `[start]`（顺序不定，因线程启动是并发的）；`install` 内 `current_num_threads = 3`、线程名形如 `my-worker-0/1/2`；`[exit]` 在池销毁消息前打印完毕——因为工作线程退出前 `ThreadPool::build` 返回的句柄虽已 drop，但退出流程由各线程自身完成。具体打印顺序待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Registry::new` 里 `use_current_thread` 的那个线程不运行 `main_loop`？

**参考答案**：见 [rayon-core/src/registry.rs:299-301](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L299-L301) 注释：征用当前线程时**不跑主循环**，这样 `Registry::new` 才能正常返回（main_loop 不返回）。代价是该线程不会主动参与工作窃取循环，需要 `yield_now`/`scope` 等方式让出时才干活，且 `WorkerThread` 被泄漏。

**练习 2**：`DefaultSpawn` 与 `CustomSpawn` 都实现了 `ThreadSpawn`，但 `ThreadPoolBuilder` 的泛型默认参数是哪个？用户调用 `spawn_handler` 后类型如何变化？

**参考答案**：默认是 `DefaultSpawn`（[rayon-core/src/lib.rs:165](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L165) 的 `ThreadPoolBuilder<S = DefaultSpawn>` 与 [rayon-core/src/lib.rs:231](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L231) 的默认值）。调用 `spawn_handler(f)` 后类型变为 `ThreadPoolBuilder<CustomSpawn<F>>`，其余配置字段被逐一搬到新 builder（不能直接 `..self`，因为泛型参数不同）。

**练习 3**：如果 spawn 到一半失败（比如线程资源耗尽），已经启动的线程会怎样？

**参考答案**：`Registry::new` 提前 `return Err`，栈上的 `Terminator` 守卫随之 `Drop` 并调用 `registry.terminate()`；由于 `terminate_count` 初始为 1 且没有外部异步任务增计，这次减一直接归零，所有已启动线程的 terminate 锁存器被点亮，它们走完 `wait_until_out_of_work` 正常退出，不会泄漏。

### 4.3 默认线程数规则：get_num_threads 的优先级链

#### 4.3.1 概念说明

u1-l2 在使用层面给过一句结论：「显式设置 > `RAYON_NUM_THREADS` > 逻辑核数」。本模块下到源码层，把这条规则补全成完整的优先级链，并解释两个边界：为什么 `num_threads(0)` 是"自动"、以及线程数存在一个与位数相关的软上限。

软上限的数学表达：设指针位宽对应的线程计数位数为 \( b \)（64 位平台 \( b = 16 \)，32 位平台 \( b = 8 \)），则

\[ \text{THREADS\_MAX} = 2^{b} - 1 \]

即 64 位平台最多 65535 个线程、32 位平台最多 255 个。这个限制的来历很有意思：rayon 把"睡眠线程数/非活跃线程数/任务事件计数"三个计数打包进同一个 `AtomicUsize`（u5-l5 的 counters），位数不够就得省着用。

#### 4.3.2 核心流程

`ThreadPoolBuilder::get_num_threads`（[rayon-core/src/lib.rs:454-482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L454-L482)）的裁决流程：

```text
num_threads 字段 > 0 ？
├─ 是 → 直接用它（环境变量完全不参与）
└─ 否（默认 0，表示"自动"）→
    解析 RAYON_NUM_THREADS：
    ├─ 合法且 ≥ 1 → 用它（return）
    ├─ 恰为 0     → 用 default()（available_parallelism）※ 注意：直接 return，
    │               不再看废弃变量
    └─ 未设置/解析失败 → 继续向下
    解析废弃的 RAYON_RS_NUM_CPUS：
    ├─ 合法且 ≥ 1 → 用它
    └─ 未设置/为 0/解析失败 → default()
                                   = available_parallelism()，失败则 1
最后在 Registry::new 入口再夹一次软上限：
n = min(上述结果, max_num_threads())
```

而查询侧 `rayon::current_num_threads()` 的解析规则是：当前是池内线程 → 该池的 `num_threads()`；否则 → 全局池的线程数（必要时触发全局池初始化）。

#### 4.3.3 源码精读

> [rayon-core/src/lib.rs:454-482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L454-L482) —— 优先级链的完整实现。三个细节值得圈出：其一，`default()` 闭包是 `thread::available_parallelism().map(|n| n.get()).unwrap_or(1)`，即逻辑核数、拿不到则 1；其二，`Some(x @ 1..)` 模式只接受正整数，`Some(0)` 单独分支直接返回 default（提前 return，跳过废弃变量）；其三，解析失败的字符串（如 `"abc"`）落入 `_ => {}` 继续走 `RAYON_RS_NUM_CPUS` 的同型检查。

> [rayon-core/src/lib.rs:500-528](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L500-L528) —— `num_threads()` 设置方法的文档：非零值保证池**至多**启动这么多线程；未设置时"目前"基于 `RAYON_NUM_THREADS` 或逻辑核数，且文档带**未来兼容性警告**——默认行为将来可能变为动态增减线程，想锁定数量就该显式调用。文档同时声明 `RAYON_NUM_THREADS` 是废弃变量 `RAYON_RS_NUM_CPUS` 的一比一替代，两者同时设置时前者优先。

> [rayon-core/src/lib.rs:108-111](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L108-L111) —— `max_num_threads()` 直接返回 `sleep::THREADS_MAX`，注释点明受限原因：睡眠计数器的 `AtomicUsize` 里留给线程计数的位就那么多。

> [rayon-core/src/sleep/counters.rs:55-60](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L55-L60) 与 [rayon-core/src/sleep/counters.rs:76-77](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/sleep/counters.rs#L76-L77) —— `THREADS_BITS` 按 `target_pointer_width` 条件编译取 16 或 8；`THREADS_MAX = (1 << THREADS_BITS) - 1`。

> [rayon-core/src/registry.rs:243-244](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L243-L244) —— 软上限真正生效的地方：`Ord::min(builder.get_num_threads(), crate::max_num_threads())`。请求 999999 会被悄悄降到 65535（64 位平台），而不是报错——"soft-limit"的含义。

> [rayon-core/src/lib.rs:113-133](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L113-L133) —— 用户侧 `current_num_threads()` 文档：在池内代码中返回**当前池**的线程数，否则返回全局池的；并行迭代器内部就用它决定切分多少任务。

> [rayon-core/src/registry.rs:337-346](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L337-L346) —— 上述文档的底层实现：读 `WorkerThread::current()`，空指针走全局池，非空走该线程所属的 registry。方法存在的理由（注释）：比 `Registry::current().num_threads()` 少一次 `Arc` 引用计数增减。

> [src/lib.rs:116](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L116) —— 用户实际调用的 `rayon::current_num_threads` 是上层 crate 对 `rayon_core::{current_num_threads, current_thread_index, max_num_threads}` 的再导出（u1-l4 的 12 条再导出之一）。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标**：用环境变量实验逐条验证 `get_num_threads` 的优先级链。

**操作步骤**（示例代码，非项目源码）：

1. 沿用 4.1.4 的 `registry-lab` 项目，把 `main.rs` 换成：

```rust
fn main() {
    // 注意：这一行会触发全局池初始化，环境变量在此刻被读取
    println!("current_num_threads = {}", rayon::current_num_threads());
    println!("available_parallelism = {:?}",
             std::thread::available_parallelism().map(|n| n.get()));
    println!("max_num_threads = {}", rayon::max_num_threads());
}
```

2. 依次用不同环境变量运行（`--release` 与否不影响本实验）：

```bash
cargo run -q                      # 什么都不设
RAYON_NUM_THREADS=2 cargo run -q  # 设为 2
RAYON_NUM_THREADS=0 cargo run -q  # 设为 0
RAYON_NUM_THREADS=abc cargo run -q # 非法值
RAYON_NUM_THREADS=2 RAYON_RS_NUM_CPUS=8 cargo run -q # 两个变量同时设
RAYON_RS_NUM_CPUS=3 cargo run -q  # 只设废弃变量
RAYON_NUM_THREADS=8 cargo run -q  # 大于核数（假设机器 4 核）
```

3. 对照 [rayon-core/src/lib.rs:454-482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/lib.rs#L454-L482) 逐行核对每条输出。
4. 回答一个问题并到源码里找证据：`RAYON_NUM_THREADS` 是在什么时刻被读取的？（提示：环境变量只在 `get_num_threads` 被调用时读取一次，而它只发生在 `Registry::new` 期间——也就是全局池首次使用那一刻。）

**需要观察的现象**：七种运行条件下 `current_num_threads` 的取值；`available_parallelism` 与不设变量时的取值关系；`max_num_threads` 的具体数字。

**预期结果**（按源码逻辑推导，具体数值待本地验证）：

| 运行条件 | current_num_threads |
| --- | --- |
| 什么都不设 | 等于 `available_parallelism`（机器相关） |
| `RAYON_NUM_THREADS=2` | 2 |
| `RAYON_NUM_THREADS=0` | 与什么都不设相同（走 default 分支） |
| `RAYON_NUM_THREADS=abc` | 与什么都不设相同（解析失败下落） |
| 两个变量同时设（2 与 8） | 2（`RAYON_NUM_THREADS` 优先） |
| 只设 `RAYON_RS_NUM_CPUS=3` | 3（废弃变量仍生效） |
| `RAYON_NUM_THREADS=8`（4 核机） | 8（**不会**被核数截断，只会被 `THREADS_MAX` 截断） |

最后一行是本实践最重要的发现：rayon 对环境变量来者不拒，超核数也照单全收——超额线程只是操作系统的调度问题，不是 rayon 的正确性问题。

#### 4.3.5 小练习与答案

**练习 1**：`RAYON_NUM_THREADS=0` 与 `RAYON_NUM_THREADS=abc` 最终效果相同，但源码路径不同。请说出区别。

**参考答案**：`Some(0)` 命中专门分支 `Some(0) => return default()`，**提前返回**，不再检查 `RAYON_RS_NUM_CPUS`；`abc` 解析失败落入 `_ => {}`，**继续向下**检查废弃变量——若此时设置了 `RAYON_RS_NUM_CPUS=3`，则 `=0` 的结果是核数、`=abc` 的结果是 3。这是两者唯一的分叉点。

**练习 2**：在 64 位平台上把 `RAYON_NUM_THREADS` 设成 `100000` 会发生什么？

**参考答案**：`get_num_threads` 返回 100000，随后 `Registry::new` 中 `Ord::min(100000, 65535)` 把它软限到 65535。rayon 会尝试真开 65535 个 OS 线程；多数机器会在中途 spawn 失败，`Terminator` 守卫触发收摊，`Registry::new` 返回 IO 错误；若发生在全局池隐式初始化中，后续调用会 panic（见 4.1.5 练习 2）。不建议实际尝试。

**练习 3**：为什么 `Registry::current_num_threads` 要单独写成方法，而不是复用 `Registry::current().num_threads()`？

**参考答案**：见 [rayon-core/src/registry.rs:334-337](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L334-L337) 的方法文档：`Registry::current()` 会克隆一次 `Arc`（原子引用计数加一减一），而这个查询在并行迭代器切分逻辑里是热路径，直接经 `WorkerThread` 裸指针读可以省掉这次原子操作。

## 5. 综合实践

**任务：写一个「registry 体检程序」，把本讲三个模块串起来。**

要求实现以下功能（示例代码骨架，非项目源码）：

```rust
use rayon::ThreadPoolBuilder;

fn main() -> Result<(), rayon::ThreadPoolBuildError> {
    println!("== 全局池 ==");
    println!("current_num_threads = {}", rayon::current_num_threads());
    println!("max_num_threads = {}", rayon::max_num_threads());

    println!("== 自定义池 ==");
    let pool = ThreadPoolBuilder::new()
        .num_threads(2)
        .thread_name(|i| format!("lab-{i}"))
        .start_handler(|i| println!("[start] {i}"))
        .exit_handler(|i| println!("[exit ] {i}"))
        .build()?;
    pool.install(|| {
        // 池内视角：应打印 2 而不是全局池的线程数
        println!("池内 current_num_threads = {}", rayon::current_num_threads());
        println!("池内线程名 = {:?}", std::thread::current().name());
    });
    drop(pool);

    println!("== 二次配置全局池 ==");
    match ThreadPoolBuilder::new().num_threads(7).build_global() {
        Ok(()) => println!("成功（不该发生）"),
        Err(e) => println!("被拒绝: {e}"),
    }
    Ok(())
}
```

验收要点（全部依据本讲源码推导，具体输出待本地验证）：

1. 第一段打印触发了全局池的惰性初始化——`current_num_threads` 的值遵循 4.3 的优先级链；再用 `RAYON_NUM_THREADS=5 cargo run -q` 跑一遍，该值应变为 5。
2. `install` 闭包里打印的是**自定义池**的 2 与 `lab-*` 线程名（验证 [rayon-core/src/registry.rs:337-346](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L337-L346) 的"池内看本池"规则）。
3. 自定义池不受 `RAYON_NUM_THREADS=5` 影响，仍是 2（显式 `num_threads` 优先于环境变量）。
4. 最后一段必被拒绝（全局池已被第一行初始化），错误文案为 "The global thread pool has already been initialized."。
5. `[exit]` 事件在程序结束前出现，对应 `ThreadPool::drop → terminate → 主循环退出 → exit_handler` 链路。

## 6. 本讲小结

- **全局 Registry 是惰性单例**：`THE_REGISTRY`（`static mut` + `Once`）保证只初始化一次；任何顶层 rayon 调用（甚至只读的 `current_num_threads`）都可能成为触发者；初始化失败不重试，二次配置返回 `GlobalPoolAlreadyInitialized`。
- **启动链路**：`Registry::new` 软限线程数 → 建 job/broadcast 两套队列 → 组装 `Arc<Registry>`（`terminate_count = 1`）→ `Terminator` 守卫防半途而废 → 逐线程构造 `ThreadBuilder` 交启动器；`ThreadSpawn` 是契约，`DefaultSpawn`（`std::thread::Builder`）与 `CustomSpawn`（`spawn_handler` 回调）是两个实现。
- **工作线程的一生**：栈上 `WorkerThread` + thread-local 注册 → set primed → start 回调 → 阻塞在 terminate 锁存器上"边等边干活" → set stopped → exit 回调。内核代码 panic 即 abort，用户代码 panic 被逐任务捕获。
- **释放时机**：全局池永不释放；用户池随 `ThreadPool::drop` 调 `terminate()`，`terminate_count` 归零时逐线程点亮 terminate 锁存器，各线程有序退出后 `Arc` 归零、registry 析构。
- **线程数优先级链**：显式 `num_threads(>0)` > `RAYON_NUM_THREADS`（≥1 有效；0 走默认；非法下落）> 废弃的 `RAYON_RS_NUM_CPUS` > `available_parallelism()`（失败取 1）；最后被 `max_num_threads() = 2^b − 1`（64 位 65535、32 位 255）软限，超核数不截断。
- **两个易混点**：`ThreadInfo.stealer` 是 job 队列窃取端（供偷），`WorkerThread.stealer` 是 broadcast 队列窃取端（自取广播）；`num_threads(0)` 不是"一个线程"而是"自动"。

## 7. 下一步学习建议

Registry 已经把线程组织起来，但本讲刻意绕开了两个问题：**线程找不到任务时如何休眠、新任务到来如何被唤醒**（`sleep` 字段背后是一套精巧的原子计数器协议），以及**窃取循环的完整细节**（`WorkerThread::steal` 的随机起点受害者遍历）。下一讲 u5-l4《工作窃取队列与调度循环》将以 [rayon-core/src/registry.rs:835-908](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L835-L908) 的 `find_work`/`steal` 与 crossbeam-deque 的三方窃取协议为主线补齐前者的一半；u5-l5《睡眠与唤醒协议》再攻下 `sleep` 模块与 `counters.rs` 的不变量（届时 `THREADS_MAX` 的位数分配会再次登场）。建议阅读顺序：先重读本讲的 `inject_or_push`/`inject`（[rayon-core/src/registry.rs:412-442](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/registry.rs#L412-L442)），带着"注入的任务去哪了"的问题进入下一讲。
