# 默认收集器与线程局部 HANDLE

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `epoch::pin()`、`epoch::is_pinned()`、`epoch::default_collector()` 这三个「无需自己建 `Collector`」的便捷函数背后到底发生了什么。
- 解释 `default.rs` 里那个进程级单例 `Collector` 是如何被**惰性初始化**的，以及为什么在 `loom` 模型测试里要走另一条分支。
- 理解「每个线程一个参与者」是如何用 `thread_local` 实现的，以及 `with_handle` 的「回退注册」机制为何能让 `pin()` 在线程退出期间也不会 panic。
- 把本讲的「默认收集器」与上一讲（u4-l13）的「自建 `Collector` / `LocalHandle`」对应起来：默认收集器只是把这两样东西用一个全局单例 + 线程局部存储自动串好了而已。

## 2. 前置知识

本讲建立在你已经掌握以下概念的基础上（来自前序讲义，这里只做一句话复习）：

- **`Collector` 是 `Arc<Global>` 的薄包装**（u4-l13）：`Collector::new()` 创建一个独立的 `Global`，`clone()` 共享同一个 `Global`，所以两个 `Collector` 是否「同一个」要用 `Arc::ptr_eq` 判断。
- **`LocalHandle` 是某线程在某 `Collector` 里的「会员卡」**（u4-l13）：内部就是一根 `*const Local` 裸指针，由 `Collector::register()` 发放，`Drop` 时调用 `Local::release_handle` 归还。
- **`Local` 是真正的「参与者」状态**（u4-l13 / u3-l9）：`guard_count`（pin 层数）与 `handle_count`（会员卡张数）两个计数器，二者皆归 0 才触发 `finalize` 销毁该参与者。
- **`Guard` 是 pin 的 RAII 凭证**（u3-l9）：内部也只是一根指向 `Local` 的指针，drop 即 unpin，且 pin 可重入。
- **`pin()` 这条便捷路径只在 `std` 下可用**（u1-l2）：因为它依赖线程局部存储（TLS）。

如果你对这些还不够熟，建议先回去看 u4-l13 和 u3-l9。本讲要回答的核心问题是：

> 「我明明没有写 `Collector::new()`，也没有写 `collector.register()`，为什么直接调 `epoch::pin()` 就能用？是谁帮我建的收集器、帮我注册的参与者？」

答案就在 `src/default.rs` 这一个不到 100 行的文件里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/default.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs) | 本讲主角。定义进程级单例 `Collector`、线程局部 `HANDLE`、以及 `pin`/`is_pinned`/`default_collector` 三个公开函数。**整个文件只在 `feature = "std"` 下编译。** |
| [src/collector.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs) | `Collector` 与 `LocalHandle` 的公开定义。`default.rs` 直接复用这里的 `Collector::register()` 和 `LocalHandle::pin()` 等 API。 |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | `Local`（参与者）的真正实现：`register`/`pin`/`is_pinned`/`release_handle`/`finalize`。本讲会引用它来解释「注册一次参与者」到底做了什么。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs) | 模块总装车间。`mod default;` 和 `pub use default::{pin, is_pinned, default_collector};` 把这三个函数暴露为 crate 顶层 API。 |

一句话定位：`default.rs` = 「全局单例 `Collector`」+「线程局部 `LocalHandle`」+「三个转发函数」。

---

## 4. 核心概念与源码讲解

### 4.1 全局 COLLECTOR 的惰性初始化

#### 4.1.1 概念说明

在 u4-l13 里，你必须自己写 `let collector = Collector::new();` 才能开始用 epoch 回收。但绝大多数用户根本不关心收集器实例——他们只想 `epoch::pin()` 拿个 `Guard` 用。

所以 crossbeam-epoch 提供了一个**进程级单例收集器**（default collector）：整个进程共享同一个 `Collector`，谁第一次需要它，它就被创建出来；之后所有人都复用这一个。

这里有两个关键设计决策：

1. **惰性初始化（lazy init）**：不用就不建，避免给不需要 EBR 的程序引入启动开销。这是「零成本抽象」的体现。
2. **线程安全的初始化**：多线程可能同时「第一次」调用，必须保证 `Collector::new()` 只被执行一次，且所有人拿到同一个引用。

`default.rs` 顶部那段模块注释准确地描述了这套机制（包含单例和每线程参与者两件事）：

```rust
//! For each thread, a participant is lazily initialized on its first use, when the current thread
//! is registered in the default collector.  If initialized, the thread's participant will get
//! destructed on thread exit, which in turn unregisters the thread.
```

#### 4.1.2 核心流程

单例的获取由一个私有函数 `collector()` 完成，它返回 `&'static Collector`：

```
collector()
  ├─ [std 分支] OnceLock<Collector>::get_or_init(Collector::new)
  │     · 首次调用：执行 Collector::new()，写入 static，返回 &Collector
  │     · 后续调用：直接返回已存的 &Collector（全程线程安全）
  └─ [loom 分支] loom::lazy_static! { static ref COLLECTOR: Collector = Collector::new(); }
        · loom 的等价惰性初始化，返回 &COLLECTOR
```

要点：

- `static` 被定义在**函数内部**（function-local static）。这既保证了惰性，又让它对外完全私有——外部只能通过 `collector()` 拿到引用。
- 返回类型是 `&'static Collector`：static 的生命周期就是整个程序，所以引用可以「无限期」地持有。
- `OnceLock::get_or_init` 内部用一次性同步原语保证 `Collector::new()` 恰好执行一次，即使成百上千个线程同时撞上来。

#### 4.1.3 源码精读

[default.rs:L16-L33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L16-L33) 是单例获取的全部逻辑：

```rust
fn collector() -> &'static Collector {
    #[cfg(not(crossbeam_loom))]
    {
        /// The global data for the default garbage collector.
        static COLLECTOR: OnceLock<Collector> = OnceLock::new();
        COLLECTOR.get_or_init(Collector::new)
    }
    // FIXME: loom does not currently provide the equivalent of Lazy:
    // https://github.com/tokio-rs/loom/issues/263
    #[cfg(crossbeam_loom)]
    {
        loom::lazy_static! {
            static ref COLLECTOR: Collector = Collector::new();
        }
        &COLLECTOR
    }
}
```

逐行解读：

- 第 8 行 `use std::sync::OnceLock;`——`OnceLock` 来自 `std::sync`，这正解释了为什么 `default.rs` 只能在 `feature = "std"` 下编译（见 [lib.rs:L184-L187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187) 的 `#[cfg(feature = "std")] mod default;`）。
- `static COLLECTOR: OnceLock<Collector>`：`OnceLock` 是一个「只能写一次的格子」。一开始它是空的；`get_or_init` 会在空时执行闭包 `Collector::new()` 填入，在非空时直接返回借用。整个「判空 + 填充」是原子的。
- 这里用 `OnceLock` 而不是更老的 `lazy_static` / `once_cell::Lazy`，是因为本项目 MSRV 已提升到 1.74（见 [Cargo.toml:L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L10) `rust-version = "1.74"`），而 `OnceLock` 自 Rust 1.70 起就进入标准库，足够用了。
- `loom` 分支：[default.rs:L23-L32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L23-L32) 走 `loom::lazy_static!`。原因是 loom 为了做有界状态模型检验，需要劫持所有「同步原语」和「static」，而它尚未提供 `OnceLock`/`Lazy` 的等价物（注释里挂了上游 issue #263 的 FIXME）。所以这里用条件编译分两路。

注意 `Collector::new()` 真正做了什么，回顾 [collector.rs:L40-L50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L40-L50)：它内部调 `Arc::new(Global::new())`（经 `Default` 实现），返回一个**全新的、独立的** `Global`。也就是说，这个进程级单例拥有自己的一条 locals 链表、一个垃圾队列和一个 epoch 计数器，与任何「自建 `Collector`」互不相干。

#### 4.1.4 代码实践

**实践目标**：验证「默认收集器是进程级单例」——多次获取拿到的是同一个 `Collector`。

**操作步骤**：

1. 在一个依赖了 `crossbeam-epoch` 的项目里写如下 `main`（示例代码，需自行放入 Cargo 工程）：

   ```rust
   // 示例代码：验证默认收集器的单例性
   use crossbeam_epoch::default_collector;

   fn main() {
       let c1 = default_collector();
       let c2 = default_collector();
       // Collector 实现了 PartialEq，内部用 Arc::ptr_eq 判断是否同一个 Global
       assert!(c1 == c2, "default_collector() 必须每次返回同一个 Collector");
       println!("两次获取的是同一个 Collector: {}", c1 == c2);
   }
   ```

2. `cargo run` 运行。

**需要观察的现象**：断言通过，打印 `true`。

**预期结果**：`default_collector()` 无论调用多少次、在哪个线程，返回的 `&'static Collector` 都指向同一个 `Global`（`Arc::ptr_eq` 为真）。

> 说明：`default_collector()` 的实现就是直接转发 `collector()`，见 4.3 节。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `COLLECTOR` 要定义成函数内的 `static`，而不是模块顶层的 `pub static`？

**参考答案**：函数内 `static` 有两个好处——一是对外完全私有，外部代码无法绕过 `collector()` 直接访问，从而保证「一定是惰性初始化」的不变量；二是符号作用域更小，命名不污染模块空间。`OnceLock` 本身已经线程安全，不需要额外的访问控制。

**练习 2**：如果某程序从不调用 `epoch::pin()`，`Collector::new()` 会被执行吗？

**参考答案**：不会。`OnceLock::get_or_init` 是惰性的，只有真正调用 `collector()`（即 `pin`/`is_pinned`/`default_collector` 其一）才会触发构造。从不使用 EBR 的进程不会付出任何默认收集器的开销。

---

### 4.2 线程局部 HANDLE 与 with_handle 回退注册

#### 4.2.1 概念说明

单例 `Collector` 解决了「全局只有一个收集器」的问题，但 epoch 回收是**按线程**工作的：每个线程必须先 `register()` 拿到一张自己的「会员卡」（`LocalHandle`），才能 `pin()`。如果每次 `pin()` 都要用户手动 `register`，那就太啰嗦了。

`default.rs` 的解法是：给每个线程配一个**线程局部变量** `HANDLE: LocalHandle`。它在线程第一次访问时自动调用 `collector().register()` 注册一次，之后该线程的所有 `pin()` 都复用这一张卡。当线程退出时，`HANDLE` 作为线程局部变量被自动 drop，于是 `LocalHandle::drop` → `Local::release_handle`，该线程的参与者就被注销了。这正对应模块注释里那句「the thread's participant will get destructed on thread exit, which in turn unregisters the thread」。

但这里藏着一个**陷阱**：线程退出时，线程局部变量的析构顺序并不完全可控。如果别的线程局部变量的 `Drop` 实现里调用了 `epoch::pin()`，而那时 `HANDLE` 可能已经被析构了——直接 `.with()` 会 panic。为此 `default.rs` 设计了一个 `with_handle` 辅助函数，带一个**回退分支**：当 TLS 已失效时，临时注册一张新卡来完成任务。这是本讲最精妙的一处。

#### 4.2.2 核心流程

线程局部 `HANDLE` 的声明极其简洁（[default.rs:L35-L38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L35-L38)）：

```rust
thread_local! {
    /// The per-thread participant for the default garbage collector.
    static HANDLE: LocalHandle = collector().register();
}
```

初始化表达式 `collector().register()` 只在线程首次访问 `HANDLE` 时执行一次。注意这里的 `thread_local` 宏并不是 `std::thread_local!` 直接来的——它来自 `crate::primitive::thread_local`（见 [default.rs:L10-L14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L10-L14) 的 import）。在 `loom` 下它指向 `loom::thread_local`（[lib.rs:L90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L90)），在真实环境下指向 `std::thread_local`（[lib.rs:L129-L130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L129-L130)）。这是 u1-l2 / u6-l22 讲过的「`primitive` 抽象层」: 让上层代码对 loom 与真实环境无感。

`with_handle` 是把「在 HANDLE 上做事」安全化的关键（[default.rs:L57-L65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L57-L65)）：

```
with_handle(f)
  ├─ HANDLE.try_with(|h| f(h))            ← 正常路径：拿到本线程已注册的 HANDLE，对它跑 f
  │     · Ok(R)  → 返回 R
  │     · Err(_) → TLS 已被销毁（线程退出期间），走回退
  └─ .unwrap_or_else(|_| f(&collector().register()))
        · 回退路径：临时在单例 Collector 上注册一张新卡，对它跑 f
        · 这张临时卡用完（语句结束）即 drop → release_handle → finalize，自我清理
```

为什么回退能正常工作、且不留垃圾？结合 [internal.rs:L337-L357](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L337-L357) 的 `register` 和 [internal.rs:L513-L527](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L513-L527) 的 `release_handle` 可以看清：

- `collector().register()` 创建一个全新 `Local`（`handle_count = 1`、`guard_count = 0`），插入全局链表，返回临时 `LocalHandle`。
- `f(&handle)` 对它 `pin()`（`guard_count` 升到 1 再降回 0），完成 `f`。
- 语句结束时临时 `LocalHandle` 被 drop → `release_handle`：`handle_count` 从 1 降到 0，而此时 `guard_count == 0`，于是触发 `finalize`（[internal.rs:L522-L526](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L522-L526)），把这个「用完即弃」的参与者从链表里摘除并销毁。

所以回退注册是一张**幽灵会员卡**：它出现、完成这一次 `pin`、然后立刻自我注销，不留下长期参与者。这一切对调用者完全透明。

#### 4.2.3 源码精读

`with_handle` 的完整代码（[default.rs:L57-L65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L57-L65)）：

```rust
#[inline]
fn with_handle<F, R>(mut f: F) -> R
where
    F: FnMut(&LocalHandle) -> R,
{
    HANDLE
        .try_with(|h| f(h))
        .unwrap_or_else(|_| f(&collector().register()))
}
```

逐行解读：

- `HANDLE.try_with(|h| f(h))`：`LocalKey::try_with` 返回 `Result<R, AccessError>`。`AccessError` 唯一会在「该线程局部变量的析构器已经运行」时出现——典型场景就是线程退出期间。
- `.unwrap_or_else(|_| f(&collector().register()))`：一旦拿到 `Err`，**不 panic**，而是回退。注意这里 `collector().register()` 是一个临时值（temporary），它返回的 `LocalHandle` 在这一行结束时立即 drop。这正是「幽灵会员卡」自我清理的发生点。
- `f` 是 `FnMut`，所以闭包可以被多次调用（正常路径一次、或回退路径一次，二者只会走其一）。

`register` 的真身在 [internal.rs:L337-L357](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L337-L357)，关键几行：

```rust
pub(crate) fn register(collector: &Collector) -> LocalHandle {
    unsafe {
        let local = Owned::new(Self {
            // ...
            collector: UnsafeCell::new(ManuallyDrop::new(collector.clone())),
            handle_count: Cell::new(1),
            // ...
        })
        .into_shared(unprotected());
        collector.global.locals.insert(local, unprotected());
        LocalHandle { local: local.as_raw() }
    }
}
```

两个细节呼应 u4-l13：一是 `collector.clone()` 把单例 `Collector` 的 `Arc` 引用计数 +1 存进 `Local`（这就是为什么「drop(collector) 后 handle 仍可用」，也对回退路径无害——单例永远活着）；二是新 `Local` 的 `handle_count` 初始为 1，正好对应「这一张临时 `LocalHandle`」。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「TLS 已析构时回退注册」如何让 `pin()` 不 panic。这其实就是复现 crate 自带的 `pin_while_exiting` 测试。

**操作步骤**：

1. 阅读自带测试 [default.rs:L76-L101](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L76-L101)：

   ```rust
   #[test]
   fn pin_while_exiting() {
       struct Foo;
       impl Drop for Foo {
           fn drop(&mut self) {
               // Pin after `HANDLE` has been dropped. This must not panic.
               super::pin();
           }
       }
       std::thread_local! { static FOO: Foo = const { Foo }; }

       thread::scope(|scope| {
           scope.spawn(|_| {
               // Initialize `FOO` and then `HANDLE`.
               FOO.with(|_| ());
               super::pin();
               // At thread exit, `HANDLE` gets dropped first and `FOO` second.
           });
       })
       .unwrap();
   }
   ```

2. 在 `crossbeam-epoch` 目录下运行该测试：
   ```
   cargo test --features std pin_while_exiting
   ```

**需要观察的现象**：测试**通过**，没有 panic。子线程退出时，`FOO::drop` 里调用的 `super::pin()` 安全返回。

**预期结果**：测试输出 `test default::tests::pin_while_exiting ... ok`。

**为什么会这样（关键解释）**：在该测试的设置下，子线程先初始化 `FOO`、再首次 `super::pin()`（这才会初始化 `HANDLE`）。Rust 标准库按「后初始化先析构」的顺序销毁线程局部变量，因此线程退出时 **`HANDLE` 先 drop、`FOO` 后 drop**。当 `FOO::drop` 运行 `super::pin()` 时，`HANDLE` 这个 TLS 已经析构，`HANDLE.try_with(...)` 返回 `Err(AccessError)`。此时 `with_handle` 走回退分支 `f(&collector().register())`，临时注册一张新卡完成 pin，于是不 panic。

> 「待本地验证」的细节：线程局部变量的析构顺序在不同平台/标准库实现上并非由语言规范严格保证；该测试依赖 std 当前的「逆初始化序」行为。但无论顺序如何，只要 `HANDLE` 先于 `FOO` 被销毁，回退分支就会兜住——这正是 `with_handle` 设计的稳健之处。

#### 4.2.5 小练习与答案

**练习 1**：如果 `with_handle` 写成 `HANDLE.with(|h| f(h))`（用 `.with` 而非 `.try_with`），`pin_while_exiting` 测试会发生什么？

**参考答案**：`.with` 在 TLS 已析构时会直接 **panic**（它内部就是 `try_with(...).expect(...)`）。那样 `FOO::drop` 里的 `super::pin()` 会让线程在退出期间 panic。`try_with` + `unwrap_or_else` 的回退分支正是为了避免这种情况。

**练习 2**：回退分支里 `f(&collector().register())` 创建的临时 `LocalHandle`，会在什么时候、以什么方式被清理？会不会泄漏一个长期挂着的参与者？

**参考答案**：它在**这一行语句结束时**作为临时值被 drop。`LocalHandle::drop` 调用 `Local::release_handle`（[collector.rs:L100-L107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L100-L107)）；由于此刻 `guard_count == 0` 且 `handle_count` 由 1 降到 0，触发 `finalize`，把这个临时 `Local` 从全局链表删除并销毁。所以不会泄漏——它是一张「用完即注销」的幽灵会员卡。

**练习 3**：为什么 `HANDLE` 用的是 `crate::primitive::thread_local` 而不是直接写 `std::thread_local!`？

**参考答案**：因为同一个 `default.rs` 文件还要在 `loom` 模型测试下编译。`primitive::thread_local` 在 loom 下指向 `loom::thread_local`（被 loom 劫持以做有状态检验），在真实环境下指向 `std::thread_local`。这样上层代码无需为 loom 单独写一份。

---

### 4.3 pin / is_pinned / default_collector 三个公开函数

#### 4.3.1 概念说明

有了单例 `Collector`（4.1）和线程局部 `HANDLE` + `with_handle`（4.2），三个公开函数就是一层薄薄的转发。它们是 `lib.rs` 暴露给用户的全部「默认收集器」API（[lib.rs:L184-L187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187)）：

```rust
#[cfg(feature = "std")]
mod default;
#[cfg(feature = "std")]
pub use crate::default::{default_collector, is_pinned, pin};
```

注意它们都在 `#[cfg(feature = "std")]` 下——这再次印证 u1-l2 的结论：`epoch::pin()` 这条便捷路径**只在 `std` 下存在**；在 `no_std` + `alloc` 环境下，你必须自己建 `Collector`（u4-l13）。

三个函数的分工：

- `pin() -> Guard`：返回一个 pin 凭证。等价于「自建收集器」写法里的 `handle.pin()`。
- `is_pinned() -> bool`：当前线程是否处于 pin 状态（`guard_count > 0`）。
- `default_collector() -> &'static Collector`：拿到那个单例 `Collector` 本身。当你想拿到单例去做 `register()`、或想用 `handle.collector() == default_collector()` 来判断某 handle 是否挂在默认收集器上时用它。

#### 4.3.2 核心流程

```
epoch::pin()           → with_handle(|h| h.pin())         → 走 TLS（或回退）拿到 LocalHandle，再 Local::pin
epoch::is_pinned()     → with_handle(|h| h.is_pinned())   → 同上，再 Local::is_pinned（看 guard_count）
epoch::default_collector() → collector()                  → 直接返回 &'static 单例（不经 TLS！）
```

注意一个容易忽略的**不对称**：`pin` 和 `is_pinned` 都走 `with_handle`（即「需要本线程的参与者」），而 `default_collector()` **不走** `with_handle`——它只是返回单例本身，不涉及「当前线程有没有注册」这件事。所以即使在一个从未 `pin` 过的线程里调 `default_collector()` 也不会触发注册；但调 `pin()` 会（首次会注册本线程参与者）。

#### 4.3.3 源码精读

三个函数的实现都在 [default.rs:L40-L55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L40-L55)，每个都只有一行：

```rust
/// Pins the current thread.
#[inline]
pub fn pin() -> Guard {
    with_handle(|handle| handle.pin())
}

/// Returns `true` if the current thread is pinned.
#[inline]
pub fn is_pinned() -> bool {
    with_handle(|handle| handle.is_pinned())
}

/// Returns the default global collector.
pub fn default_collector() -> &'static Collector {
    collector()
}
```

它们转发到的 `LocalHandle::pin` / `LocalHandle::is_pinned` 定义在 [collector.rs:L80-L98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L80-L98)，二者都只是解引用 `self.local` 这个裸指针，转发到 `Local`：

```rust
pub fn pin(&self) -> Guard { unsafe { (*self.local).pin() } }
pub fn is_pinned(&self) -> bool { unsafe { (*self.local).is_pinned() } }
```

而 `Local::is_pinned` 的判断极其简单（[internal.rs:L371-L375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L371-L375)）：

```rust
pub(crate) fn is_pinned(&self) -> bool {
    self.guard_count.get() > 0
}
```

这呼应 u3-l9：`Guard` 只是「pin 的凭证」，真正记录 pin 状态的是 `Local` 里的 `guard_count`；只要至少还有一个 `Guard` 活着，当前线程就是 pinned。

`Local::pin`（[internal.rs:L401-L462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L401-L462)）的内部细节（写 local epoch + `SeqCst` 屏障 + 每 128 次周期性 collect）属于 u5 单元的范畴，本讲只需知道：`epoch::pin()` 最终会走到这里，并在首个 guard 时真正执行 pin 动作、其余情况只增 `guard_count`（pin 可重入）。

把三个公开函数和「自建收集器」写法对照一下，等价关系非常清晰：

| 默认收集器写法 | 等价的自建收集器写法 |
| --- | --- |
| `epoch::pin()` | `let h = collector.register(); h.pin()` |
| `epoch::is_pinned()` | `h.is_pinned()` |
| `epoch::default_collector()` | 你自己持有的那个 `collector` |

#### 4.3.4 代码实践

**实践目标**：把「默认收集器写法」和「自建收集器写法」放在同一段代码里，确认它们是两套**互不相干**的 `Collector` 实例，但 `pin` 行为一致。

**操作步骤**：

```rust
// 示例代码：对照默认收集器与自建收集器
use crossbeam_epoch::{self as epoch, Collector};

fn main() {
    // (A) 默认收集器路径
    let g1 = epoch::pin();
    assert!(epoch::is_pinned(), "持有 guard 期间应当 is_pinned");
    drop(g1);
    assert!(!epoch::is_pinned(), "guard drop 后应当不再 is_pinned");

    // (B) 自建收集器路径
    let mine = Collector::new();
    let h = mine.register();
    let g2 = h.pin();
    assert!(h.is_pinned());

    // (C) 两者不是同一个 Collector
    assert!(h.collector() != epoch::default_collector(),
            "自建的 Collector 与默认单例不应相同");
    drop(g2);
}
```

**需要观察的现象**：所有断言通过。特别地，(C) 处的 `!=` 成立——说明 `default_collector()` 返回的单例与你 `Collector::new()` 出来的实例是两个不同的 `Global`（`Arc::ptr_eq` 为假）。

**预期结果**：程序正常退出，无 panic。

**思考**：如果你在 (A) 里把 `assert!(!epoch::is_pinned())` 放在 `drop(g1)` **之前**，会怎样？参考答案见下。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `default_collector()` 不像 `pin()`/`is_pinned()` 那样走 `with_handle`？

**参考答案**：`default_collector()` 返回的是「单例 `Collector` 本身」，它属于整个进程，与「当前线程有没有注册参与者」无关。`with_handle` 的作用是拿到**本线程**的 `LocalHandle`，对「拿单例」这件事既无必要、还会带来不必要的副作用（首次调用就会触发注册）。所以它直接 `collector()` 返回 `&'static Collector`。

**练习 2**：在一个 `no_std` + `alloc`（关掉 `std`）的环境里，`crossbeam_epoch::pin()` 还能调用吗？

**参考答案**：不能。`pin`/`is_pinned`/`default_collector` 三个函数连同整个 `default` 模块都在 `#[cfg(feature = "std")]` 下导出（见 [lib.rs:L184-L187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187)）。关掉 `std` 后这些符号根本不存在。`no_std` 用户必须走 u4-l13 的 `Collector::new()` + `register()` 自己管理收集器（因为没有了线程局部存储来托管 `LocalHandle`）。

**练习 3**：上一节 4.3.4 末尾的思考题——把 `assert!(!epoch::is_pinned())` 放在 `drop(g1)` 之前会怎样？

**参考答案**：断言会失败（程序 panic）。因为 `g1` 还活着，`guard_count >= 1`，`is_pinned()` 返回 `true`。pin 是可重入的：只要还有任意一个 `Guard` 未 drop，当前线程就算 pinned。

---

## 5. 综合实践

把本讲三块内容（单例、TLS、回退）串起来，做一个「观察默认收集器生命周期」的小任务。

**任务描述**：写一个程序，启动若干个工作线程，每个线程里：

1. 先调 `epoch::default_collector()` 拿到单例并打印一句（这一步**不应**触发本线程注册）。
2. 调 `epoch::is_pinned()`——观察此时返回 `false`（还没 pin 过）。
3. 首次 `epoch::pin()`——这一步才会触发本线程的 `HANDLE` 初始化（即 `collector().register()`）。
4. 在 guard 存活期间再调 `epoch::is_pinned()`——应返回 `true`。
5. 在线程里再 spawn 一个**孙子线程**，让它也 `epoch::pin()`，验证它用的是同一个单例（通过 `default_collector()` 比较）。
6. 让线程退出，观察（通过日志）退出顺序，确认无 panic。

**参考骨架（示例代码）**：

```rust
use crossbeam_epoch as epoch;
use crossbeam_utils::thread;

fn main() {
    let dc = epoch::default_collector(); // 进程单例

    thread::scope(|scope| {
        for id in 0..3 {
            scope.spawn(move |_| {
                // (1) 拿单例 —— 不触发注册
                assert!(epoch::default_collector() == dc);
                // (2) 还没 pin
                assert!(!epoch::is_pinned());
                // (3) 首次 pin —— 此刻才 register 本线程参与者
                let g = epoch::pin();
                // (4) pin 中
                assert!(epoch::is_pinned());
                println!("thread {} pinned on the default collector", id);
                drop(g);
                assert!(!epoch::is_pinned());
            });
        }
    }).unwrap();
}
```

**需要观察与解释**：

1. 每个工作线程的 `epoch::default_collector() == dc` 都成立——单例全局唯一（4.1）。
2. 首次 `pin()` 之前 `is_pinned()` 为 `false`，证明 `default_collector()` 本身不会触发注册（4.3 的不对称性）。
3. 各线程互不干扰地 pin/unpin——因为每个线程有自己的 `HANDLE`（4.2 的线程局部性）。
4. 线程退出无 panic——即便线程局部变量析构顺序不可控，`with_handle` 的回退分支兜底（4.2）。

> 如果你给某个线程局部变量实现了 `Drop` 并在 `Drop` 里调 `epoch::pin()`（模仿 `pin_while_exiting`），可以进一步观察到回退分支被实际触发。该部分行为属于「待本地验证」——具体是否触发取决于 std 对该线程局部变量的析构时序。

## 6. 本讲小结

- `default.rs` 提供了「无需自建 `Collector`」的便捷路径：一个**进程级单例 `Collector`** + 一个**线程局部 `LocalHandle`（`HANDLE`）** + 三个转发函数 `pin`/`is_pinned`/`default_collector`。
- 单例由 `collector()` 惰性初始化：`std` 下用 `OnceLock::get_or_init(Collector::new)`，`loom` 下用 `loom::lazy_static!`（因 loom 缺少 `Lazy` 等价物）。返回 `&'static Collector`。
- 每个线程的参与者在**首次访问 `HANDLE`** 时由 `collector().register()` 自动注册，线程退出时随 `HANDLE` 的析构自动注销。
- `with_handle` 用 `try_with` + `unwrap_or_else` 实现**回退注册**：当 TLS 已析构（线程退出期间）时，临时注册一张「幽灵会员卡」完成任务再立刻 `finalize` 清理，从而保证 `epoch::pin()` 在任何时点都不会 panic——这正是 `pin_while_exiting` 测试不 panic 的根因。
- 三个公开函数中，`pin`/`is_pinned` 走 `with_handle`（依赖本线程参与者），`default_collector()` 不走（只返回单例本身），这一不对称值得留意。
- 全部便捷 API 都在 `#[cfg(feature = "std")]` 下；`no_std` + `alloc` 用户必须走 u4-l13 的自建 `Collector` 路径。

## 7. 下一步学习建议

本讲把「默认收集器」的对外接口和单例/TLS 机制讲清楚了，但有几处刻意留白，建议按顺序继续：

1. **`Local` 的字段与注册/计数细节** → 下一讲 **u4-l15「Local：参与者结构与注册/计数」**。本讲多次提到的 `guard_count`/`handle_count`/`entry`/`bag`/`epoch` 等字段会逐一展开，并讲清 `#[repr(C)]` 的侵入式布局。
2. **epoch 推进与回收主链路** → **u5 单元**。本讲里「`pin()` 走到 `Local::pin`」之后发生的「写 local epoch + `SeqCst` 屏障 + 周期性 collect」属于 u5-l18；「单例 `Collector` 的 `Global` 如何 `try_advance`/`collect`」属于 u5-l19。
3. **想看「自建收集器」如何手动复刻本讲的便捷性** → 回看 u4-l13，并尝试用 `Collector::new()` + 自己包装的 `thread_local` 复刻一个「私有默认收集器」，作为综合练习。
4. **想理解 loom 分支为何处处不同** → u6-l22「no_std / loom / 可移植性抽象层」会系统讲解 `primitive` 抽象层和 `loom::lazy_static!`/`loom::thread_local` 的来龙去脉。
