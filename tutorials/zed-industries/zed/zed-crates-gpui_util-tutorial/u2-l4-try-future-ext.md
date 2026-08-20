# TryFutureExt：为 Future 手写错误处理适配器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `TryFutureExt` / `TryFutureExtBacktrace` 两个扩展 trait 解决了什么问题：把上一讲 `ResultExt` 的「记录并降级」语义搬到异步世界。
2. 逐行读懂 `LogErrorFuture` 的 `Future` 实现：它如何在 `poll` 返回 `Ready(Err)` 时记录错误、把输出类型改写成 `Option<T>`。
3. 掌握 `unsafe { Pin::new_unchecked(&mut self.get_unchecked_mut().0) }` 这行手工 Pin 投影的原理、安全性前提，以及为什么必须用 `unsafe`。
4. 解释为什么 `Location` 必须在**构造 future 时**（也就是 `.log_err()` 的调用点）捕获，而不能等到 `poll` 里再取。
5. 对比 `TryFutureExt` 与 `TryFutureExtBacktrace` 的适用边界（`Display` vs `Debug`），并了解 `UnwrapFuture` 这个「不可能失败」断言变体。

本讲是第 2 单元中最难的一篇，涉及手写 `Future`、`Pin` 与 `unsafe`。前置两讲（`ResultExt`、`log_error_with_caller`）建立的语义会在这里直接复用。

## 2. 前置知识

### 2.1 Future 与 Poll：异步的「问一句」

Rust 的异步不靠线程切换，而靠状态机。一个 `Future` 本质上只有一个方法：

```rust
// 示例代码：std 中 Future trait 的简化形式
fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output>;
```

- 执行器（executor）反复调用 `poll` 问 future：「好了没？」
- 回答要么是 `Poll::Ready(值)`（完成了），要么是 `Poll::Pending`（还没好，稍后再问）。
- 返回 `Pending` 时，future 应当把 `cx` 里的 **Waker** 登记下来；等条件满足时有人调用 `waker.wake()`，执行器才会再次 `poll`。
- `async fn` / `async` 块会被编译器改写成一个实现了 `Future` 的状态机——你写 `.await` 的地方就是状态机的一次「暂停点」。

### 2.2 Pin 与 !Unpin：为什么有钉住这回事

编译器生成的 async 状态机经常包含**指向自身字段的引用**（自引用结构）。如果这样的结构体在两次 `poll` 之间被移动（move）到别的内存地址，内部指针就会指向旧地址，造成未定义行为。

`Pin<P>` 就是为了表达「这个东西不许再动」：

- `Pin<&mut T>` 是一个「被钉住的 `&mut T`」。
- 大多数类型其实可以随便动（实现 `Unpin`）；async 状态机通常是 `!Unpin`。
- 对 `!Unpin` 的类型，想从 `Pin<&mut T>` 拿到 `&mut T`，只能走 `unsafe` 的 `get_unchecked_mut()`，并**由你出面承诺**不移动它。这是本讲 `unsafe` 的根源，4.2 节展开。

### 2.3 承接前两讲的两个概念

- **记录并降级（u2-l1）**：分离（detach）的后台任务没有调用者可以把 `Result` 还回去，所以约定是「把错误记进日志，把 `Ok(T)` 降级成 `Option<T>`」——`ResultExt::log_err` 返回 `Option<T>` 正是这个语义。本讲把它搬到 future 上。
- **调用点捕获（u2-l2）**：`#[track_caller]` + `Location::caller()` 能拿到**调用处**的文件与行号（编译期常量、`'static`、零运行时开销），最终交给 `log_error_with_caller` 填进 `log::Record` 的 `file` / `line` / `target`。本讲要回答：在异步世界里，这个 Location 应该在什么时机取。

### 2.4 扩展 trait 模式

给已有的类型（这里是所有 `Output = Result<T, E>` 的 future）追加方法，标准做法是：定义一个新 trait，再写一个**全量实现**（blanket impl）：

```rust
// 示例代码：扩展 trait 的骨架
impl<F, T, E> TryFutureExt for F
where
    F: Future<Output = Result<T, E>>,
{ ... }
```

这样任何满足条件的 future 自动获得 `.log_err()` 等方法，无需 `impl` 逐个写。gpui_util 没有（也不需要）依赖 `futures` crate 的 `TryFuture`——只凭标准库的 `Future` 就够了（见 [Cargo.toml:7-9](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/Cargo.toml#L7-L9)，全平台依赖只有 `log` 与 `anyhow`）。

## 3. 本讲源码地图

本讲全部源码集中在 crate 的唯一主文件里：

| 文件 | 位置 | 作用 |
| --- | --- | --- |
| `src/lib.rs` | L338-L353 | `trait TryFutureExt`：四个方法的签名 |
| `src/lib.rs` | L355-L368 | `trait TryFutureExtBacktrace`：回溯变体 |
| `src/lib.rs` | L370-L406 | `TryFutureExt` 的全量实现（含 `#[track_caller]` 捕获点） |
| `src/lib.rs` | L408-L431 | `TryFutureExtBacktrace` 的全量实现 |
| `src/lib.rs` | L433-L458 | `LogErrorFuture` 结构体与其 `Future` 实现（本讲核心） |
| `src/lib.rs` | L460-L485 | `LogErrorWithBacktraceFuture`：`Debug` 格式化变体 |
| `src/lib.rs` | L487-L503 | `UnwrapFuture`：错误即 panic 变体 |
| `src/lib.rs` | L290-L336 | `log_error_with_caller` 与 `DebugAsDisplay`（u2-l2 已精读，本讲复用） |
| `crates/gpui/src/executor.rs` | L43-L63 | 下游真实调用链：`TaskExt::detach_and_log_err` |

阅读建议：先读两个 trait 的签名（看它向用户承诺什么），再读 `LogErrorFuture` 的 `poll`（看它如何兑现承诺），最后读 `executor.rs`（看它在真实项目中为什么必须这样设计）。

## 4. 核心概念与源码讲解

### 4.1 从 Result 到 Future：TryFutureExt 与 TryFutureExtBacktrace 两个扩展 trait

#### 4.1.1 概念说明

Zed 代码里大量任务以 `cx.spawn(...)` 或 `cx.background_spawn(...)` 的形式甩到后台运行。这些任务返回 `Task<Result<T, E>>`——注意 `Task` 本身是一个 future，`await` 它才会得到 `Result`。

问题来了：**对一个不 `await`、直接 detach 的任务，错误往哪放？** 没有调用者，`?` 运算符无处传播。`ResultExt` 的答案是「记录并降级」，但它只对 `Result` 生效。`TryFutureExt` 把同样的答案作用在「将来会产出 `Result` 的东西」上：包一层新 future，等它真正完成的那一刻，若产出的是 `Err`，就地记录日志，并把最终输出改写成 `Option<T>`。

为什么拆成两个 trait？与 `ResultExt` 内部用方法级 `where E: Debug` 的取舍同源：`Display` 约束便宜且几乎所有错误都满足（输出单行链式消息）；`Debug` 格式化能让 `anyhow::Error` 打出完整回溯，但不该强迫所有方法都背上这个约束。于是「日常版」和「回溯版」各成一个 trait，边界清晰。

#### 4.1.2 核心流程

```text
用户写下  future.log_err()
   │  （这一行所在的文件:行号 即被捕获的 Location）
   ▼
返回 LogErrorFuture { 内部future, Level::Error, Location }
   │  被 spawn / detach / await
   ▼
执行器反复 poll(LogErrorFuture)
   │  转发给内部 future
   ├── Pending          → 原样返回 Pending（什么都不做）
   ├── Ready(Ok(v))     → Ready(Some(v))
   └── Ready(Err(e))    → log_error_with_caller(Location, e, Level)
                          → Ready(None)
```

#### 4.1.3 源码精读

两个 trait 的定义：

[TryFutureExt 的四个方法签名](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L338-L353)——`log_err` / `warn_on_err` / `unwrap` 三个方法让实现类型自己捕获调用点，`log_tracked_err` 则要求调用方显式传入 `Location`。注意所有方法都有 `where Self: Sized` 约束，这是扩展 trait 的惯用法：把 `Sized` 要求限制在「调用方法」时（方法按值拿走 `self`），而不是「实现 trait」时。

[TryFutureExtBacktrace 的定义与文档注释](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L355-L368)——文档写得很直白：这是 `{:?}` 格式化的伙伴 trait，为 `anyhow::Error` 输出回溯；**除非真的需要回溯，优先用 `TryFutureExt`**。

全量实现（方法体只有一行构造）：

[TryFutureExt for F 的实现](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L370-L406)——约束是 `F: Future<Output = Result<T, E>>` 加 `E: std::fmt::Display`。看 [log_err 的方法体（L375-L382）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L375-L382)：`let location = Location::caller();` 之后立刻 `LogErrorFuture(self, log::Level::Error, *location)`。**Location 在这一刻（构造时）就被拷进包装 future 存起来**，这是 4.4 节的主题。`warn_on_err`（L391-L398）唯一区别是 `Level::Warn`。

[TryFutureExtBacktrace for F 的实现](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L408-L431)——镜像结构，但 impl 块级约束换成 `E: std::fmt::Debug`（L411），产出 `LogErrorWithBacktraceFuture`。

一个容易忽略的细节：两个包装 future 的结构体字段是私有的（[L434](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L434) 的元组字段没有 `pub`），下游只能通过 trait 方法构造它们——保证了「Location 一定来自被认可的调用点」这一不变量。

#### 4.1.4 代码实践

**实践目标**：在 zed 仓库中找到 `TryFutureExt` 的真实使用链，确认它是「生态级」基础设施。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   grep -rn "gpui_util::" crates/*/src --include="*.rs" | grep -E "TryFutureExt" | head -20
   grep -rn "detach_and_log_err" crates --include="*.rs" | head -20
   ```

2. 挑选一个 `detach_and_log_err` 的调用点，向上追 3 层：谁构造了被 detach 的任务？任务的错误类型 `E` 是什么？满足 `Display` 吗？

**需要观察的现象**：`detach_and_log_err` 的调用点非常多（Zed 里大量「发起后台任务后不再关心结果」的场景）；而它们最终都会走进本讲的 `log_tracked_err`（见 4.4）。

**预期结果**：你会看到 `TryFutureExt` 几乎不直接出现在业务代码里——业务代码用的是 `gpui` 在其上包装的 `TaskExt::detach_and_log_err`。工具库 → 框架 → 业务的三层分层清晰可见。

### 4.2 LogErrorFuture：手动实现 Future 与 unsafe Pin 投影

#### 4.2.1 概念说明

`LogErrorFuture<F>` 是一个三字段元组结构体：内部 future、日志级别、调用点位置。它自己实现了 `Future`，输出类型从 `Result<T, E>` **改写**成了 `Option<T>`——这就是「适配器 future」：不改变计算过程，只改变完成时刻对结果的处理。

手写它的难点在 `poll` 的签名：`self: Pin<&mut Self>`。async 状态机是 `!Unpin` 的，所以包装它的 `LogErrorFuture<F>` 也是 `!Unpin` 的，标准库安全的 `get_mut()`（要求 `Self: Unpin`）用不了。要访问内部 future，只能手工做 **Pin 投影**（pin projection）：从「钉住的外层」得到「钉住的内层字段」。生态里常用 `pin-project-lite` 宏生成这段代码，但 gpui_util 坚持零依赖（Cargo.toml 里没有 `futures` / `pin-project-lite`），于是手写——总共也就一行 `unsafe`。

#### 4.2.2 核心流程

`poll` 每次被调用时：

1. 先从钉住的 `self` 上**拷贝**出 `level` 与 `location`（两个字段都是 `Copy`，读取不涉及移动，安全）。
2. 对字段 0（内部 future）做 `unsafe` 投影，得到 `Pin<&mut F>`。
3. 用同样的 `cx` 轮询内部 future：
   - `Pending` → 透传 `Pending`；
   - `Ready(Ok(v))` → 返回 `Ready(Some(v))`；
   - `Ready(Err(e))` → 调 `log_error_with_caller(location, e, level)` 记日志，返回 `Ready(None)`。

日志只会在**某一次 poll 返回 `Ready(Err)` 的那一刻**触发一次——future 完成后不会再被 poll，所以不会重复记录。

#### 4.2.3 源码精读

[LogErrorFuture 的定义（L433-L434）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L433-L434)——`#[must_use]` 元组结构体，三个字段依次是内部 future、`log::Level`、`Location<'static>`。`#[must_use]` 防止你写下 `task.log_err();` 却不 detach / await——future 不被驱动就等于任务被静默取消，日志也永远不会发出。

[Future for LogErrorFuture（L436-L458）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L436-L458)——完整实现。逐行看关键的三行：

```rust
// 引自上方链接 L443-L446
fn poll(self: Pin<&mut Self>, cx: &mut Context) -> Poll<Self::Output> {
    let level = self.1;
    let location = self.2;
    let inner = unsafe { Pin::new_unchecked(&mut self.get_unchecked_mut().0) };
```

- `self.1`、`self.2`：`Pin<P>` 实现了 `Deref`，所以能直接读字段；`Level` 与 `Location` 都是 `Copy`，按值拷出来。**在投影之前先把它们拷走**，让后面的 `unsafe` 区只涉及字段 0，意图清晰。
- `self.get_unchecked_mut()`：`unsafe` 函数，无视钉住保证给出 `&mut LogErrorFuture<F>`。调用它意味着你向编译器承诺：**不会移动这个值，也不会让移动它的引用逃逸**。
- `&mut ....0` 取出内部 future 字段的裸 `&mut F`，再用 `Pin::new_unchecked`（同样是 `unsafe`）重新钉住它。等价写法是 `self.map_unchecked_mut(|s| &mut s.0)`，语义相同。

这套写法的安全性前提（Rust 官方 Pinning 文档所称的「结构化钉住」）在本类型上全部成立：

1. **字段 0 是唯一被钉住的字段**，对它的一切访问都只发生在这一行投影之后（永远裹在 `Pin` 里），从不移动、从不外泄裸 `&mut F`；
2. **其余字段（1、2）不参与钉住**，只做 `Copy` 读取；
3. **`LogErrorFuture` 没有实现 `Drop`**——一旦实现 `Drop`，`drop(&mut self)` 拿到的是未钉住的 `&mut`，会破坏上面的承诺（这正是 `pin-project` 系宏要生成 `Drop` 桩的原因）；
4. 结构体字段私有，外部无法绕过 trait 拿到内部 future。

[Err 分支（L448-L456）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L448-L456)——`Err(error)` 分支调用 [`log_error_with_caller(location, error, level)`](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L290-L321)（u2-l2 精读过的日志引擎：从 `location.file()` 反推 `target`、手工构造 `log::Record`），然后返回 `None`。`Ok` 分支原样包成 `Some`。

#### 4.2.4 代码实践

**实践目标**：亲手实现一个 `PrefixErrFuture`——内部 future 出错时，日志前加上固定前缀（如 `[my-task]`）。这是 `LogErrorFuture` 的最小同构复刻，做完你就独立走过了一遍「手写适配器 future + unsafe 投影」。

**操作步骤**：

1. 在 zed 仓库**之外**新建一个练习 crate（避免改动源码）：

   ```bash
   cargo new prefix_err_demo && cd prefix_err_demo
   cargo add log
   ```

2. 把下面的代码放进 `src/main.rs`（**示例代码**，非 zed 源码；刻意用具名字段而非元组，投影逻辑看得更清楚）：

   ```rust
   use std::{
       future::Future,
       pin::Pin,
       task::{Context, Poll},
   };

   struct PrefixErrFuture<F> {
       prefix: &'static str,
       inner: F,
   }

   // 便捷入口：让任何 Result 输出的 future 都能 .prefix_err("[my-task]")
   trait PrefixErrExt {
       fn prefix_err(self, prefix: &'static str) -> PrefixErrFuture<Self>
       where
           Self: Sized;
   }

   impl<F, T, E> PrefixErrExt for F
   where
       F: Future<Output = Result<T, E>>,
       E: std::fmt::Display,
   {
       fn prefix_err(self, prefix: &'static str) -> PrefixErrFuture<Self> {
           PrefixErrFuture { prefix, inner: self }
       }
   }

   impl<F, T, E> Future for PrefixErrFuture<F>
   where
       F: Future<Output = Result<T, E>>,
       E: std::fmt::Display,
   {
       type Output = Option<T>;

       fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
           // 先拷贝不参与钉住的 Copy 字段（对应源码的 self.1 / self.2）
           let prefix = self.prefix;
           // 结构化钉住：只投影 inner 字段。
           // 安全性前提：本类型不实现 Drop、不移动 inner、不外泄 &mut inner。
           let inner = unsafe { Pin::new_unchecked(&mut self.get_unchecked_mut().inner) };
           match inner.poll(cx) {
               Poll::Ready(output) => Poll::Ready(match output {
                   Ok(value) => Some(value),
                   Err(error) => {
                       log::error!("{prefix}: {error}");
                       None
                   }
               }),
               Poll::Pending => Poll::Pending,
           }
       }
   }

   fn main() {
       // 最小执行器：构造一个什么都不做的 waker，循环 poll。
       // Rust >= 1.85 可以直接用 Waker::noop()。
       use std::task::{RawWaker, RawWakerVTable, Waker};
       fn noop_raw_waker() -> RawWaker {
           fn clone(_: *const ()) -> RawWaker { noop_raw_waker() }
           fn noop(_: *const ()) {}
           static VTABLE: RawWakerVTable = RawWakerVTable::new(clone, noop, noop, noop);
           RawWaker::new(std::ptr::null(), &VTABLE)
       }
       fn block_on<F: Future>(fut: F) -> F::Output {
           // SAFETY: vtable 回调全为空操作，data 为空指针
           let waker = unsafe { Waker::from_raw(noop_raw_waker()) };
           let mut cx = Context::from_waker(&waker);
           let mut fut = std::pin::pin!(fut);
           loop {
               match fut.as_mut().poll(&mut cx) {
                   Poll::Ready(out) => return out,
                   // 演示用忙等：只适合「先 Pending 后 Ready」的轻量 future
                   Poll::Pending => std::thread::yield_now(),
               }
           }
       }

       // Ok 路径
       let ok = block_on(async { Ok::<_, String>(42) }.prefix_err("[my-task]"));
       println!("Ok 路径结果: {ok:?}");

       // Err 路径
       let err = block_on(async { Err::<(), _>("boom".to_string()) }.prefix_err("[my-task]"));
       println!("Err 路径结果: {err:?}");
   }
   ```

3. 运行 `cargo run`。注意 `main` 里没有初始化 logger，`log::error!` 不会有输出——先看两个返回值。

**需要观察的现象**：`Ok 路径结果: Some(42)`，`Err 路径结果: None`。随后把 `main` 换成带 logger 的版本（见第 5 节综合实践，用 `log::set_boxed_logger` 装一个记录器），再运行。

**预期结果**：Err 路径返回 `None` 且日志中出现 `[my-task]: boom`。这验证了「输出改写成 `Option`」与「Err 时记录」两个行为。完整测试断言在综合实践中补齐。

### 4.3 LogErrorWithBacktraceFuture 与 UnwrapFuture：两个变体

#### 4.3.1 概念说明

`LogErrorWithBacktraceFuture` 与 `LogErrorFuture` 逐行同构，唯一差别在 Err 分支用 `DebugAsDisplay(&error)` 包装错误后再交给 `log_error_with_caller`。回看 u2-l2：`log_error_with_caller` 要求 `E: Display` 并用 `{:#}` 格式化；而 `anyhow::Error` 只有 `{:?}`（Debug）才会带出回溯。`DebugAsDisplay` 适配器（[L328-L336](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L328-L336)）在 `Display::fmt` 里转手执行 `{:?}`，让 Debug 输出穿过 Display 约束的函数。于是这个变体 future 的 impl 块级约束从 `E: Display` 换成了 `E: Debug`。

`UnwrapFuture` 是另一种姿态：**断言「这个任务不可能失败」**。Err 直接 `unwrap()` panic——对应同步世界 `Result::unwrap` 的异步版。它不存 `Location`、不记日志，是三个包装里最短的。

#### 4.3.2 核心流程

```text
LogErrorWithBacktraceFuture.poll
   └── Ready(Err(e)) → log_error_with_caller(loc, DebugAsDisplay(&e), level)
                        // Debug 格式化 → anyhow::Error 打出回溯

UnwrapFuture.poll
   └── Ready(result) → Ready(result.unwrap())   // Err 即 panic
```

#### 4.3.3 源码精读

[LogErrorWithBacktraceFuture（L460-L485）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L460-L485)——结构体与 `LogErrorFuture` 完全同形（同样 `#[must_use]`、同样三字段）。impl 约束 `E: std::fmt::Debug`（L466），Err 分支（L477-L480）传 `DebugAsDisplay(&error)`。投影那一行（L473）与 4.2 节逐字相同。

[UnwrapFuture 结构体（L487）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L487)——单字段元组，**没有** `#[must_use]`（对照 L433/L460 两处都有，这是源码里的事实差异）。

[Future for UnwrapFuture（L489-L503）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L489-L503)——`type Output = T`（不是 `Option<T>`！错误路径直接 panic 掉了）。`E: std::fmt::Debug`（L492）来自 `Result::unwrap` 打印错误的需要。L497 的投影与 L499 的 `result.unwrap()` 都一目了然。

一个值得品味的约束细节：`TryFutureExt::unwrap`（[L400-L405](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L400-L405)）处在 `E: Display` 的 impl 块里——构造包装时只查 `Display`；真正 `await`（触发 `Future for UnwrapFuture`）时才查 `Debug`。两层约束各管各的时刻。

#### 4.3.4 代码实践

**实践目标**：用肉眼验证 `Display` 与 `Debug` 两种格式化的差别——这是两个 trait 拆分的全部理由。

**操作步骤**：在 4.2 的练习 crate 里追加（**示例代码**）：

```rust
struct WeirdError;
impl std::fmt::Display for WeirdError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Display 版本")
    }
}
impl std::fmt::Debug for WeirdError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Debug 版本")
    }
}

// 在 main 里调用：
let e = WeirdError;
log::error!("Display 路径: {e}");
log::error!("Debug 路径: {e:?}");
```

装好 logger（综合实践给出记录器实现）后 `cargo run`。

**需要观察的现象**：两条日志分别输出 `Display 版本` 与 `Debug 版本`。再把 `WeirdError` 换成 `anyhow::anyhow!("x")` 的错误并设置 `RUST_BACKTRACE=1`，对比 `{:?}` 多出的回溯段。

**预期结果**：`DebugAsDisplay` 做的事就是「Display 的壳、Debug 的芯」，所以回溯版 future 的日志会多出完整调用栈。

### 4.4 log_tracked_err 与调用点捕获：Location 为什么在构造时取得

#### 4.4.1 概念说明

回顾 u2-l2 的结论：`#[track_caller]` 让 `Location::caller()` 返回**调用点**的编译期常量。现在把这个问题放到异步里：错误日志真正发出的时刻是**某次 poll 返回 `Ready(Err)` 时**——可能在任务构造后很久、由执行器在调度循环深处触发。如果那时才去取位置，得到的会是执行器内部的文件行号，对诊断毫无价值。

所以正确做法是：**在构造包装 future 的那一刻捕获调用点，存进 future，伴随它直到完成**。`log_err` / `warn_on_err` 用 `#[track_caller]` 自动做这件事；`log_tracked_err` 则把口子敞开——让**上层包装函数**把自己捕获到的调用点显式传进来。

为什么需要这个敞开的口子？看真实调用链就明白：`gpui` 的 `TaskExt::detach_and_log_err` 是对 `log_tracked_err` 的再包装。如果它内部直接写 `self.log_err()`，`#[track_caller]` 捕获到的将是 `detach_and_log_err` 函数体内部那一行（executor.rs 的行号），而不是用户调用 `detach_and_log_err` 的那一行。显式传递 `Location` 让调用点信息穿过任意层包装，始终指向用户代码。

#### 4.4.2 核心流程

```text
用户代码:  task.detach_and_log_err(cx)            ← 位置 A（用户文件:行号）
              │ #[track_caller] → Location::caller() = A
              ▼
executor.rs: self.log_tracked_err(*location)      ← 显式传入 A
              │ 构造 LogErrorFuture(task, Error, A)
              ▼
执行器某刻: poll → Ready(Err(e))
              │ log_error_with_caller(A, e, Error)
              ▼
日志:      file = A 的文件, line = A 的行号       ← 指回用户代码
```

#### 4.4.3 源码精读

[log_err 与 log_tracked_err 的对照（L375-L389）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_util/src/lib.rs#L375-L389)——`log_err` 标了 `#[track_caller]`、自己取 `Location::caller()`；`log_tracked_err`（L384-L389）**没有** `#[track_caller]`，`location` 是普通参数，方法体只是把传入的值塞进 `LogErrorFuture`。`warn_on_err`（L391-L398）与回溯版的 `log_err_with_backtrace`（L413-L420）都属前者；`log_tracked_err_with_backtrace`（L422-L430）属后者。四个「自动捕获」方法标 `#[track_caller]`、两个「显式传入」方法不标——源码里这个整齐的对应关系本身就是文档。

下游的真实调用链：

[gpui 的 TaskExt 实现（executor.rs L43-L63）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/executor.rs#L43-L63)——`detach_and_log_err` 自己标注 `#[track_caller]`（L48），在 L50 取 `Location::caller()`（= 用户调用 `detach_and_log_err` 的位置），L52 通过 `.log_tracked_err(*location)` 传入本讲的包装 future，随后 `spawn(...).detach()` 放手运行。`detach_and_log_err_with_backtrace`（L56-L62）走 `log_tracked_err_with_backtrace`，结构完全对称。注意 `TaskExt` 的约束是 `E: Display + Debug`（L46）——它要同时服务两个 trait 的路径。

最后回答标题里的问题：**为什么不能在 poll 里取 Location？** 两个原因。其一，`poll` 的调用者是执行器，就算给 `poll` 标上 `#[track_caller]`，`Location::caller()` 指向的也是执行器调度循环里的一行，而不是业务代码；其二，`.await` 并不像函数调用那样传播 caller 信息，不存在「谁 await 我」的机制可查。存一份构造时的 `Location<'static>`（`Copy`、指向编译期常量、无堆分配）是唯一能把「有意义的调用点」带到完成时刻的方式——而且零运行时开销。

#### 4.4.4 代码实践

**实践目标**：把 4.4.2 的调用链在源码里亲手走一遍，验证「日志指向用户代码」。

**操作步骤**：

1. 打开 [crates/gpui/src/executor.rs L43-L63](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/executor.rs#L43-L63)，确认 `detach_and_log_err` 的 `#[track_caller]` 与 `Location::caller()`。
2. 在 zed 仓库中 grep 一个 `detach_and_log_err(cx)` 的业务调用点（第 4.1 节实践已找过）。
3. 回答：当该任务最终以 `Err` 结束时，日志的 `file:line` 指向哪里？`target`（由 `log_error_with_caller` 从文件路径反推）是什么？

**需要观察的现象 / 预期结果**：`file:line` 指向业务代码里 `detach_and_log_err(cx)` 那一行（不是 executor.rs）；`target` 形如 `业务crate::模块`（推导规则见 u2-l2）。若把答案代入 4.2 节练习 crate——给 `prefix_err` 标 `#[track_caller]` 并把捕获的 `Location` 打进日志——可以实际看到这一行为；本地未运行部分标注：**待本地验证**。

### 4.x.5 小练习与答案

（4.1 练习）

**练习 1**：`TryFutureExt` 的每个方法都写了 `where Self: Sized`，为什么？
**答案**：方法按值拿走 `self`（`fn log_err(self)`），这要求 `Self: Sized`。把约束写在方法级而非 trait 级，可以让 trait 对 `!Sized` 的类型仍然成立，只是不能调用这些方法——这是扩展 trait 的标准写法。

**练习 2**：`Task<Result<T, E>>` 上调用 `.log_err()`，走的是 `ResultExt` 还是 `TryFutureExt`？会不会冲突？
**答案**：走 `TryFutureExt`。方法解析看接收者类型：`Task` 实现了 `Future`（`Output = Result<T,E>`），命中 `TryFutureExt` 的全量实现；`ResultExt` 只实现给 `Result` 本身。两者接收者类型不相交，不会冲突。

**练习 3**：`LogErrorFuture` 的 `Output` 为什么是 `Option<T>` 而不是 `Result<T, E>`？
**答案**：与 `ResultExt::log_err` 同构：分离的后台任务没有调用者可以接收 `Err`，「记录即降级」——错误已经在 poll 完成时记入日志，调用方只需区分「有值 / 无值」。

（4.2 练习）

**练习 1**：为什么 `poll` 里不能写 `self.get_mut().0`？
**答案**：`Pin::get_mut` 要求 `Self: Unpin`。`LogErrorFuture<F>` 的 Unpin 性随字段 0 传播，而 async 状态机 `F` 通常是 `!Unpin`，所以安全通道不可用，只能手工投影。

**练习 2**：`Pin::new_unchecked(&mut self.get_unchecked_mut().0)` 里两个 `unsafe` 各承担什么义务？
**答案**：`get_unchecked_mut()` 承诺「拿到 `&mut Self` 后不移动这个值、不让可移动它的引用逃逸」；`Pin::new_unchecked` 承诺「这个 `&mut F` 从此被钉住，后续只通过 `Pin` 访问」。整体还依赖两个隐含前提：类型不实现 `Drop`，且字段 0 的裸 `&mut` 不外泄——`LogErrorFuture` 两条都满足。

**练习 3**：如果给 `LogErrorFuture` 实现了 `Drop`（比如想在取消时打点），这行投影还安全吗？
**答案**：不安全。`Drop::drop` 收到的是未钉住的 `&mut self`，意味着钉住结构体的字段可能在被钉住期间经由 drop 移动，破坏结构化钉住契约。此时必须改用 `pin-project` / `pin-project-lite` 生成带 `Drop` 桩的投影（这也是那类 crate 存在的主要原因之一）。

（4.3 练习）

**练习 1**：`TryFutureExt::unwrap` 所在的 impl 块要求 `E: Display`，而 `Future for UnwrapFuture` 要求 `E: Debug`，矛盾吗？
**答案**：不矛盾，两个约束作用于不同时刻：调用 `.unwrap()` 构造包装时走 trait impl 块的 `Display`；`await` 触发 `Future for UnwrapFuture` 时才需要 `Debug`（`Result::unwrap` 要用 `Debug` 打印错误）。

**练习 2**：`LogErrorFuture` 和 `LogErrorWithBacktraceFuture` 都标了 `#[must_use]`，`UnwrapFuture` 没标。如果写下 `task.unwrap();`（不 detach 不 await）会发生什么？
**答案**：编译器不警告，future 被立即丢弃，任务取消、永不 poll——既不会得到值，也不会在错误时 panic，等于静默丢失。对照 `LogErrorFuture`：丢弃它同样意味着「日志永远不会发出」，这正是 `#[must_use]` 想拦下的 bug。

（4.4 练习）

**练习 1**：如果把 `#[track_caller]` 标到 `Future::poll` 上并在其中调用 `Location::caller()`，会得到什么？
**答案**：执行器调度循环里调用 `poll` 的那一行（例如 gpui executor 内部），因为 `poll` 的调用者是执行器而不是业务代码——对诊断毫无价值，所以 Location 必须在构造时捕获。

**练习 2**：`detach_and_log_err` 为什么不直接在函数体里调 `self.log_err()`，而要走 `log_tracked_err(*location)`？
**答案**：直接调 `log_err()` 捕获到的是 `detach_and_log_err` 函数体内部那一行（executor.rs 的行号）。先用 `#[track_caller]` 在自己的函数边界拿到用户调用点，再显式传入，日志才能指回用户代码。

**练习 3**：`Location<'static>` 存在 future 里跨多次 poll 存活，有生命周期或开销问题吗？
**答案**：没有。它是对编译期常量的引用，`'static` 生命周期、`Copy` 语义、无堆分配，存储与拷贝都是零成本的（u2-l2 已确认其零运行时开销属性）。

## 5. 综合实践

把 4.2 的练习 crate 补全为一个带测试的小项目，覆盖 `Ok` / `Err` / `Pending` 三条路径与日志断言（**示例代码**，非 zed 源码）。

**任务**：实现 `PrefixErrFuture`（4.2 已完成）＋ `YieldOnce`（先返回一次 `Pending` 再完成的 future）＋ 记录型 logger，并写测试断言「Err 时日志包含前缀」「Pending 被原样透传」「Ok 产出 `Some`」。

**步骤**：

1. 在练习 crate 的 `src/main.rs` 或 `src/lib.rs` 中追加：

   ```rust
   use std::sync::{Mutex, Once};

   static LOGS: Mutex<Vec<String>> = Mutex::new(Vec::new());
   static INIT: Once = Once::new();

   struct Recorder;
   impl log::Log for Recorder {
       fn enabled(&self, _: &log::Metadata) -> bool { true }
       fn log(&self, record: &log::Record) {
           LOGS.lock().unwrap()
               .push(format!("{} {}", record.level(), record.args()));
       }
       fn flush(&self) {}
   }

   fn init_logger() {
       INIT.call_once(|| {
           log::set_boxed_logger(Box::new(Recorder)).expect("logger 只应设置一次");
           log::set_max_level(log::LevelFilter::Info);
       });
   }

   /// 先返回一次 Pending，再产出 value——用来驱动 Pending 路径。
   struct YieldOnce<T> {
       value: Option<T>,
       yielded: bool,
   }

   impl<T: Unpin> Future for YieldOnce<T> {
       type Output = T;
       fn poll(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<T> {
           if !self.yielded {
               self.yielded = true;
               Poll::Pending
           } else {
               Poll::Ready(self.value.take().expect("不应在完成后继续 poll"))
           }
       }
   }

   #[cfg(test)]
   mod tests {
       use super::*;

       #[test]
       fn prefix_err_future_three_paths() {
           init_logger();
           LOGS.lock().unwrap().clear();

           // 路径 1：Ok
           let ok = crate::block_on(async { Ok::<_, String>(7) }.prefix_err("[my-task]"));
           assert_eq!(ok, Some(7));

           // 路径 2 + 3：先 Pending，后 Ready(Err)
           let task = YieldOnce { value: Some(Err::<(), _>("boom".to_string())), yielded: false }
               .prefix_err("[my-task]");
           let err = crate::block_on(task);
           assert_eq!(err, None);

           let logs = LOGS.lock().unwrap();
           assert!(
               logs.iter().any(|m| m.contains("[my-task]") && m.contains("boom")),
               "日志应包含前缀与错误信息，实际: {logs:?}"
           );
           assert_eq!(logs.len(), 1, "只应记录一条错误");
       }
   }
   ```

2. 把 4.2 的 `block_on` / `noop_raw_waker` 从 `main` 提升为 `pub fn block_on`（`tests` 模块要用），`main` 可保留演示输出。
3. 运行 `cargo test`；需要观察 `YieldOnce` 第一次 poll 返回 `Pending` 后，自制 `block_on` 的忙等循环立刻再次 poll 得到 `Ready`。

**预期结果**：测试通过。三条断言分别验证：输出改写（`Some(7)`）、Err 降级（`None`）、日志内容与次数（一条、含 `[my-task]` 与 `boom`）。

**思考题（选做）**：`YieldOnce` 返回 `Pending` 时没有登记 waker，为什么自制 `block_on` 仍能推进？如果换成真实执行器（smol / tokio）会怎样？——答案在 2.1 节的 Waker 契约里：真实执行器在 wake 之前不会再 poll 你，所以「返回 Pending 却不登记 wake」的 future 在真实执行器上会**永远挂起**；本练习的忙等 block_on 恰好掩盖了这一点，这也解释了为什么它只能用于演示。

## 6. 本讲小结

- `TryFutureExt` / `TryFutureExtBacktrace` 用全量实现给所有 `Output = Result<T, E>` 的 future 追加方法，把 `ResultExt` 的「记录并降级」语义搬进异步世界；`Display` / `Debug` 两套约束拆成两个 trait，边界与 u2-l1 完全一致。
- `LogErrorFuture::poll` 在内部 future 返回 `Ready(Err)` 时调用 `log_error_with_caller` 记一条指向**构造时捕获的调用点**的错误日志，并把输出改写为 `Option<T>`；`Pending` 与 `Ready(Ok)` 原样透传。
- 访问 `Pin<&mut Self>` 下的内部 future 依靠手工 Pin 投影 `unsafe { Pin::new_unchecked(&mut self.get_unchecked_mut().0) }`，安全性前提是：字段 0 是唯一被钉住的字段、其余字段只做 `Copy` 读取、类型不实现 `Drop`、内部 `&mut` 不外泄。
- `Location` 必须在构造包装 future 时捕获：poll 的调用者是执行器（拿不到有意义的调用点），且 `.await` 不传播 caller 信息；`log_tracked_err` 敞开显式传入口，让 gpui 的 `detach_and_log_err` 能把用户调用点穿过包装层带进最终日志。
- `LogErrorWithBacktraceFuture` 借 `DebugAsDisplay` 让 `anyhow::Error` 在完成时输出回溯；`UnwrapFuture` 则把 Err 升级为 panic，输出类型直接是 `T`。

## 7. 下一步学习建议

- 下一讲（u2-l5）转向轻量许多的 `defer` 与 `Deferred`：同样是「包装 + Drop」的组合，但不涉及 `unsafe`，可以当作本讲之后的缓冲。
- 想巩固 Pin 投影：阅读 Rust 官方文档的 Pinning 章节（Pinning 与 `Drop`、结构化钉住两节），再对比 `pin-project-lite` 宏生成的代码与本讲手写的这一行，理解宏到底替你挡掉了哪些坑。
- 想看更多手写 Future 的实例：`futures` crate 的 `futures-util` 里满是这类适配器（`Map`、`Then`、`CatchUnwind`），套路与本讲一致；也可以阅读 zed 仓库 `crates/gpui/src/executor.rs` 中 `Task` / `FallibleTask` 的实现，看 `TryFutureExt` 如何被框架层消费。
- 完成本讲综合实践后，建议回到 4.4 的调用链，把 `log_error_with_caller` 的 target 推导（u2-l2）与本题的 file/line 指向串联起来画一张完整的「异步错误日志诞生路径图」。
