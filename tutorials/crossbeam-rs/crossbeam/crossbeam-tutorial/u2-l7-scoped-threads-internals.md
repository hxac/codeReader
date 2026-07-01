# 作用域线程实现内幕

## 1. 本讲目标

在 u1-l4 里我们已经「会用」`crossbeam_utils::thread::scope`：它能 spawn 借用栈上数据的线程，并在作用域结束时自动 join。本讲把镜头从「用法」转向「实现」，回答三个问题：

1. `Scope` 内部靠什么数据结构记账所有 spawn 出去的线程？它与 `WaitGroup` 怎样协作？
2. `std::thread::spawn` 明明要求闭包 `'static`，为什么这里能传入只活到 `'env` 的闭包？这处 `unsafe` 凭什么是安全的？
3. `scope` 返回之前，代码具体走了哪几步，才能向编译器兑现「所有子线程一定已结束」的承诺？子线程 panic 又是如何被收集而不影响其它线程的？

学完后，你应能独立读懂 `thread.rs` 的 `scope`/`spawn` 全流程，并能向别人讲清楚「作用域线程为什么不会 use-after-free」。

## 2. 前置知识

本讲假设你已经掌握以下内容（来自前置讲义）：

- **作用域线程的基本用法（u1-l4）**：`scope` 向编译器承诺「作用域结束前所有 spawn 的线程一定 join」，从而放宽 `std::thread::spawn` 的 `'static` 限制，允许子线程借用外层 `'env` 生命周期的栈数据。`Scope<'env>` 用 `PhantomData<&'env mut &'env ()>` 把 `'env` 钉成不变（invariant）生命周期。
- **WaitGroup（u2-l6）**：引用计数 + 条件变量原语。`new()` 计数初始为 1，`clone` 加 1，`wait`/`drop` 减 1；最后一个把计数减到 0 的引用负责抢锁并 `notify_all`，等待方持锁 `while` 循环检查计数，从而不丢唤醒。
- **Rust 并发基础**：`'static` 约束、`Send`/`Sync` auto trait、`Arc<Mutex<T>>` 的共享可变状态模式、`mem::transmute` 的危险性与用途、unwinding panic 的传播。

术语速查：

| 术语 | 含义 |
| --- | --- |
| `'env` | 用户允许子线程借用的「外部数据」的生命周期 |
| `'scope` | `ScopedJoinHandle` 自身存活期（不超过 `scope` 函数本次调用） |
| flavor（口味） | 本讲不涉及 channel，借用 channel 的讲义里的「flavor」与本讲无关 |
| SharedVec / SharedOption | `Arc<Mutex<Vec<T>>>` / `Arc<Mutex<Option<T>>>` 的别名，见下文 |

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [crossbeam-utils/src/thread.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs) | 作用域线程的全部实现：`scope` 函数、`Scope` 结构、`ScopedThreadBuilder::spawn`、`ScopedJoinHandle`、`AbortOnPanic` 守卫 |
| [crossbeam-utils/src/sync/wait_group.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs) | `WaitGroup` 实现（u2-l6 已精读过内部协议，本讲只看它如何被 `scope` 使用） |

整篇讲义围绕一条主线：**「spawn 注册 → 收尾等待 → 收尾 join → 收集 panic」**。我们把实现拆成三个最小模块分别拆解。

## 4. 核心概念与源码讲解

### 4.1 Scope 结构与共享句柄表

#### 4.1.1 概念说明

`Scope` 本质上是一个**线程句柄注册表**：每 spawn 一个线程，就把它的 `JoinHandle` 登记进表里；作用域收尾时统一把表里「还没被手动 join 的」句柄 join 掉。这个表必须满足两个苛刻要求：

- **跨线程可追加**：子线程还能继续嵌套 spawn，所以表要能被多个线程并发 `push`。
- **手动 join 与自动 join 互不冲突**：用户拿到的 `ScopedJoinHandle` 可以手动 `join()`，但作用域收尾也会尝试 join 同一个线程。二者只能有一个真正去 join，否则会 double-join。

为满足这两点，crossbeam 把表设计成「共享的 `Vec`，且每个元素本身又是一个共享的 `Option`」。

#### 4.1.2 核心流程

```
spawn(thread)
   │
   │  把 JoinHandle 包成 Arc<Mutex<Option<JoinHandle<()>>>>
   ├─→ 同一个 Arc 同时存进两处：
   │     ① Scope.handles（共享 Vec，供收尾自动 join）
   │     ② ScopedJoinHandle.handle（交给用户，供手动 join）
   │
   ▼
谁先调用 .lock().take() 拿走 Some(handle)，谁就拥有 join 权；
另一方 take 到 None，直接跳过。
```

关键直觉：**`Option` 是「抢占标记」**。手动 join 和自动 join 抢的是同一个 `Arc<Mutex<Option<…>>>`，`take()` 把 `Some` 变 `None`，谁先抢到谁 join，另一方看到 `None` 就知道「已被别人接管」。

#### 4.1.3 源码精读

先看两个类型别名，它们是理解后续代码的钥匙：

[crossbeam-utils/src/thread.rs:120-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L120-L121) —— 定义 `SharedVec` 与 `SharedOption`：

```rust
type SharedVec<T> = Arc<Mutex<Vec<T>>>;
type SharedOption<T> = Arc<Mutex<Option<T>>>;
```

`SharedVec` 让 `Vec` 可被多线程并发追加；`SharedOption` 让单个值可在多线程间安全地「抢占式取走」。

接着看 `Scope` 结构本身：

[crossbeam-utils/src/thread.rs:206-215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L206-L215) —— `Scope` 的三个字段：

```rust
pub struct Scope<'env> {
    /// The list of the thread join handles.
    handles: SharedVec<SharedOption<thread::JoinHandle<()>>>,

    /// Used to wait until all subscopes all dropped.
    wait_group: WaitGroup,

    /// Borrows data with invariant lifetime `'env`.
    _marker: PhantomData<&'env mut &'env ()>,
}
```

展开 `handles` 的类型，是层层套娃的 `Arc<Mutex<Vec<Arc<Mutex<Option<JoinHandle<()>>>>>>>`，每一层都有职责：

| 层 | 职责 |
| --- | --- |
| 外层 `Arc<Mutex<Vec<…>>>`（`SharedVec`） | 让所有线程都能 `push` 新句柄 |
| `Vec` 内每个元素 `Arc<Mutex<Option<JoinHandle<()>>>>`（`SharedOption`） | 让「手动 join」与「自动 join」共享同一个句柄、靠 `Option` 抢占 |
| `JoinHandle<()>` | 真正的 OS 线程句柄，注意返回类型是 `()` |

**为什么 `JoinHandle` 的返回类型是 `()`？** 这是刻意的**类型擦除**：一个 `Scope` 里 spawn 的线程可能返回不同类型 `T1`、`T2`、`T3`，但 `Vec` 只能装同种类型。解决办法是让被 spawn 的闭包不通过返回值传递结果，而是把结果写进一个**单独的** `SharedOption<T>`，让 `JoinHandle` 恒为 `JoinHandle<()>`。于是 `Vec` 里所有元素同型，而每个线程的真实结果 `T` 各自存在自己的 `result` 字段里（见 4.2.3）。

再看 `_marker`：它借 `PhantomData<&'env mut &'env ()>` 把 `'env` 钉成**不变**生命周期（u1-l4 已讲）。不变性是安全模型的地基——它阻止编译器把 `'env` 协变缩短，从而堵死「子线程借用的数据其实活得比宣称的更短」这类逃逸。

紧接着一行手写的 `unsafe impl`：

[crossbeam-utils/src/thread.rs:217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L217) —— 显式声明 `Scope` 是 `Sync`：

```rust
unsafe impl Sync for Scope<'_> {}
```

为什么需要 `Scope: Sync`？因为用户可以把 `&Scope`（即 spawn 闭包收到的那个参数 `s`）**捕获进又一个被 spawn 的闭包**，从而把 `&Scope` 发送到子线程做嵌套 spawn；而 `&T` 跨线程 `Send` 的前提正是 `T: Sync`。手写这行 `unsafe impl` 把这一保证锁死。其安全性在于：`Scope` 没有任何「裸」的可变共享状态——`handles` 在 `Mutex` 之后、`wait_group` 是内部已用 `Mutex`+`Condvar` 同步的 `Arc<Inner>`，因此共享 `&Scope` 不会引发数据竞争。（`Send` 则不需要手写：`Scope` 各字段天然 `Send`，故自动满足。）

最后看 spawn 时如何把句柄登记进表：

[crossbeam-utils/src/thread.rs:467-472](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L467-L472) —— 同一个 `Arc` 同时塞进共享表与返回给用户的句柄：

```rust
let handle = Arc::new(Mutex::new(Some(handle)));
// ...
// Add the handle to the shared list of join handles.
self.scope.handles.lock().unwrap().push(Arc::clone(&handle));
```

注意是 `Arc::clone(&handle)`：表里存的是克隆，返回给用户的 `ScopedJoinHandle.handle`（见 [crossbeam-utils/src/thread.rs:474-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L474-L479)）拿到的是同一个 `Arc` 的另一份克隆。两边 `.take()` 抢的是同一份 `Option`。

而手动 join 的实现，正是「`take` 抢占」的直接体现：

[crossbeam-utils/src/thread.rs:533-542](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L533-L542) —— `ScopedJoinHandle::join`：

```rust
pub fn join(self) -> thread::Result<T> {
    let handle = self.handle.lock().unwrap().take().unwrap();
    handle
        .join()
        .map(|()| self.result.lock().unwrap().take().unwrap())
}
```

它先把 `JoinHandle` 从共享 `Option` 里 `take()` 出来（此处 `unwrap` 安全：只要还没被自动收尾 join 过，必定是 `Some`），再 `join` 线程，最后从那个**单独的** `result: SharedOption<T>` 里取出真实返回值。注意 `join()` 返回 `Result<(), ...>`（因为闭包返回 `()`），真实结果 `T` 是从 `result` 另取的——这正是上文「类型擦除」的兑现。

#### 4.1.4 代码实践

**实践目标**：在源码里亲手标注「同一个 `Arc` 被两处共享」的关系，确认自动 join 与手动 join 不会重复 join。

**操作步骤**：

1. 打开 [crossbeam-utils/src/thread.rs:467](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L467)，找到 `let handle = Arc::new(Mutex::new(Some(handle)));`。
2. 往下两行看到 `push(Arc::clone(&handle))`（表里那份）。
3. 再往下到 `Ok(ScopedJoinHandle { handle, ... })`（用户那份）。
4. 跳到 [crossbeam-utils/src/thread.rs:536](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L536)（`join` 里的 `take()`）和 [crossbeam-utils/src/thread.rs:184](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L184)（收尾里的 `take()`），确认两处都是「先 `take`、拿到 `None` 就跳过」。

**需要观察的现象**：两处 join 入口都通过 `Option` 互斥，没有任何地方对同一个 `JoinHandle` 调用两次 `join()`。

**预期结果**：你能用一句话说明——「`Arc<Mutex<Option<JoinHandle>>>` 是手动 join 与自动 join 的仲裁者，`take()` 是一次性抢占」。

#### 4.1.5 小练习与答案

**练习 1**：如果 `handles` 不用 `Option` 包裹、直接存 `JoinHandle`，手动 join 之后作用域收尾再 join 会怎样？

> **答案**：会对同一个 `JoinHandle` 调用第二次 `join()`，要么 panic（标准库禁止对已 join 的句柄再 join），要么产生未定义行为。`Option` 的 `take()` 把「是否已被 join」显式建模，避免了 double-join。

**练习 2**：`JoinHandle` 为什么是 `JoinHandle<()>` 而不是 `JoinHandle<T>`？

> **答案**：为了让 `Vec` 能装下返回不同 `T` 的线程句柄（类型擦除）。真实结果 `T` 不走 `JoinHandle` 的返回值，而是被闭包写进一个单独的 `SharedOption<T>`（`ScopedJoinHandle.result`），`join` 之后再从那里取。

---

### 4.2 spawn 的生命周期转换与 AbortOnPanic 守卫

#### 4.2.1 概念说明

这里藏着一处 `unsafe`，也是整个作用域线程「能用」的核心机关。

`std::thread::spawn` 的签名要求闭包 `F: Send + 'static`——它无法证明线程何时结束，所以要求闭包不借用任何栈数据。而作用域线程恰恰要借用 `'env`（短于 `'static`）的数据。二者直接冲突。

crossbeam 的解法是：**先用 `Box` 把闭包装箱并标注为 `'env`，再用 `mem::transmute` 把它的生命周期边界从 `'env` 擦成 `'static`，喂给标准库**。这等于在跟编译器说：「相信我，我会在 `'env` 数据失效之前把这个线程 join 掉」。这处 `unsafe` 的正确性，完全依赖收尾阶段的自动 join（4.3）。

但如果收尾阶段本身发生了意外 panic（比如某个 `Mutex` 被毒化、`.lock().unwrap()` panic 了），unwinding 会一路向外逃逸、销毁 `'env` 数据，而此时可能还有子线程没 join 完、仍在访问这些数据——这就是灾难性的 use-after-free。为此 `scope` 安插了一个 `AbortOnPanic` 守卫：**收尾期间一旦发现正在 unwinding，立即 `abort` 整个进程**，宁可崩成渣，也不让任何线程带着悬垂引用继续跑。

#### 4.2.2 核心流程

```
ScopedThreadBuilder::spawn(f)
   │
   ├─ 1. result = SharedOption<T>::default()      // 准备装返回值
   ├─ 2. 造一份子 Scope（克隆同一个 handles Arc、再 clone 一份 WaitGroup）
   ├─ 3. 包一层闭包：
   │        move || {
   │            let scope = <子 Scope>;            // 带 'env 生命周期进闭包
   │            let res = f(&scope);               // 跑用户代码（这里访问 'env 数据）
   │            *result.lock() = Some(res);        // 把结果写进 SharedOption
   │        }                                      // ← 注意：返回 ()，不是 T
   ├─ 4. Box<dyn FnOnce() + Send + 'env>           // 装箱，标 'env
   ├─ 5. unsafe transmute → Box<… + 'static>       // 擦成 'static（unsafe！）
   ├─ 6. self.builder.spawn(closure)               // 交给标准库真正起线程
   └─ 7. 把句柄 Arc 推进共享表 + 包成 ScopedJoinHandle 返回
```

#### 4.2.3 源码精读

核心都在 `ScopedThreadBuilder::spawn`：

[crossbeam-utils/src/thread.rs:424-480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L424-L480)。我们逐段看。

**① 准备装返回值的容器 + 克隆一份子 Scope**：

[crossbeam-utils/src/thread.rs:431-442](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L431-L442)：

```rust
let result = SharedOption::default();                       // 单独的 result 容器（类型擦置的另一半）
// ...
let scope = Scope::<'env> {
    handles: Arc::clone(&self.scope.handles),               // 共享同一个句柄表
    wait_group: self.scope.wait_group.clone(),              // WaitGroup 计数 +1
    _marker: PhantomData,
};
```

子 Scope 克隆了**同一个** `handles` 的 `Arc`（所以本线程后续嵌套 spawn 的句柄会进同一张表），并对 `wait_group` 做了一次 `clone`（计数 +1，表示「又多了一个待收尾的子作用域」）。

**② 包装闭包**：

[crossbeam-utils/src/thread.rs:446-455](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L446-L455)：

```rust
let closure = move || {
    // Make sure the scope is inside the closure with the proper `'env` lifetime.
    let scope: Scope<'env> = scope;
    // Run the closure.
    let res = f(&scope);
    // Store the result if the closure didn't panic.
    *result.lock().unwrap() = Some(res);
};
```

闭包把子 `scope` 以 `'env` 生命周期搬进来，调用用户 `f`（**所有对 `'env` 借用数据的访问都发生在这里**），再把返回值写进 `result`。注意闭包返回 `()`——所以标准库看到的 `JoinHandle` 是 `JoinHandle<()>`。若 `f` panic，`Some(res)` 这行不会执行，`result` 保持 `None`，而 `scope` 会在 unwinding 时被 Drop（从而 WaitGroup 计数 -1），线程以 panic 结束、错误被 `JoinHandle` 捕获。

**③ 那处关键的 transmute**：

[crossbeam-utils/src/thread.rs:458-463](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L458-L463)：

```rust
// Allocate `closure` on the heap and erase the `'env` bound.
let closure: Box<dyn FnOnce() + Send + 'env> = Box::new(closure);
let closure: Box<dyn FnOnce() + Send + 'static> =
    unsafe { mem::transmute(closure) };
// Finally, spawn the closure.
self.builder.spawn(closure)?
```

第一步把闭包装箱为 `Box<dyn FnOnce() + Send + 'env>`——这里编译器仍老老实实记着 `'env`。第二步用 `transmute` 把这个 trait object 的生命周期边界改写成 `'static`，骗过标准库的 `spawn`。**整段实现的正确性就悬在这一行上**：它之所以没变成 UB，是因为 4.3 的收尾流程保证在 `'env` 数据被释放前一定 join 掉这个线程。

**④ AbortOnPanic 守卫**：

[crossbeam-utils/src/thread.rs:150-157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L150-L157) —— 定义在 `scope` 函数内部：

```rust
struct AbortOnPanic;
impl Drop for AbortOnPanic {
    fn drop(&mut self) {
        if thread::panicking() {
            std::process::abort();
        }
    }
}
```

它在 `catch_unwind` 捕获完用户 `f` 的结果之后被创建（[crossbeam-utils/src/thread.rs:171](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L171)），在 join 全部完成、确认无事后被 `mem::forget` 掉（[crossbeam-utils/src/thread.rs:188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L188)）。也就是说，它的「有效期」恰好覆盖「等待子作用域 + join 线程」这段收尾期。一旦这段路径上发生任何 panic，`AbortOnPanic::drop` 探测到 `thread::panicking()` 为真，就 `abort` 进程——把「收尾期 panic 导致线程带着悬垂引用逃逸」这个最危险的窗口，用「直接崩进程」兜底。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `spawn` 的数据流，看清「`'env` 闭包如何被擦成 `'static`、结果如何流回调用者」。

**操作步骤**：

1. 从 [crossbeam-utils/src/thread.rs:446](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L446) 到 `transmute`（[:459](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L459-L460)），画出闭包生命周期标注的变化：`'env` → `'static`。
2. 再从 `ScopedJoinHandle::join`（[:533](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L533-L542)）回看结果：`JoinHandle<()>` 的 `()` 与 `result: SharedOption<T>` 的 `T` 是如何分工的。

**需要观察的现象**：返回值 `T` 在线程内被写进 `result`，在 `join` 后才被读出；`JoinHandle` 全程只搬运 `()`。

**预期结果**：你能解释「为什么 `transmute` 之后标准库仍能正确回收结果」——因为结果根本没走 `JoinHandle` 的返回通道，而是走旁路的 `SharedOption<T>`。

**待本地验证**：若你好奇 panic 路径，可手动在用户闭包里 `panic!`，断点/打印观察 `*result.lock()... = Some(res)` 这行确实没执行（`result` 保持 `None`）。

#### 4.2.5 小练习与答案

**练习 1**：去掉 `AbortOnPanic`（假设收尾期某个 `.lock().unwrap()` 因 Mutex 毒化而 panic），最坏会发生什么？

> **答案**：收尾期的 panic 会沿 `scope` 一路 unwind，导致 `scope` 栈帧上的 `'env` 数据被销毁；而此刻可能还有未被 join 的子线程正在访问这些数据，从而 use-after-free。`AbortOnPanic` 把这种「收尾期 panic」升级为进程 abort，用「全盘崩溃」避免「静默内存损坏」。

**练习 2**：`transmute` 把 `Box<dyn FnOnce() + Send + 'env>` 改成 `+ 'static`，为什么不会立刻 UB？

> **答案**：因为 `scope` 的收尾流程保证在 `'env` 数据失效前 join 掉线程，被擦掉的 `'env` 约束实际上由「自动 join」这条运行期不变量承担。`unsafe` 的代价正是「程序员必须维护这条不变量」——这也是 `AbortOnPanic` 如此关键的原因。

---

### 4.3 scope 收尾：等待子作用域 → join 全部线程 → 收集 panic

#### 4.3.1 概念说明

`scope` 函数的收尾，是把「对编译器的承诺」兑现成「运行期事实」的地方。它分三步：

1. **捕获 `f` 的结果**：用 `catch_unwind` 跑用户闭包 `f`，无论它正常返回还是 panic，都先把结果存起来（不立刻传播）。
2. **等待所有子作用域结束**：靠 `WaitGroup` 阻塞到「本 scope 通过 `spawn` 衍生出的全部子 Scope（含嵌套）都已 drop」。这一步保证：再不会有新句柄被 `push` 进共享表，且所有用户闭包 `f` 都已返回、不再执行对 `'env` 数据的访问。
3. **join 剩余线程并收集 panic**：`drain` 共享表，把还没被手动 join 的句柄逐个 join，收集它们的 panic；最后按「`f` 是否 panic / 是否有线程 panic」决定返回 `Ok` 还是 `Err`。

借用安全的论证：第 2 步保证所有 `f` 已返回（不再访问 `'env` 数据），第 3 步 join 进一步等线程彻底退出（包括 drop 掉 `f` 捕获的环境，这是最后一处可能触碰 `'env` 数据的地方）。两步之后，`scope` 才允许返回、才允许外层释放 `'env` 数据。

#### 4.3.2 核心流程

先看 `WaitGroup` 计数如何随 spawn / 收尾变化（设根线程只 spawn 了一个子线程）：

| 时刻 | 事件 | WaitGroup 计数 | 持有者 |
| --- | --- | --- | --- |
| `WaitGroup::new()` | 根创建 `wg` | 1 | `wg` |
| `Scope { wait_group: wg.clone() }` | 根的 scope 持有一份 | 2 | `wg` + `scope.wait_group` |
| `spawn` 子线程 | 子 Scope 再 clone 一份 | 3 | + 子线程的 `scope` |
| 子线程闭包返回、`scope` drop | 子线程那份 drop | 2 | 回到 `wg` + `scope.wait_group` |
| `drop(scope.wait_group)` | 根主动丢掉 scope 那份 | 1 | 只剩 `wg` |
| `wg.wait()` | 根消费 `wg`，计数 1→0 | 0 | 唤醒返回 |

关键点：根持有**两份**引用（`wg` 与 `scope.wait_group`），所以收尾时必须先 `drop(scope.wait_group)` 释放一份，`wg.wait()` 才有可能等到 0。若子线程还没结束（它的 clone 没 drop），`wg.wait()` 会一直阻塞——这正是「等待所有子作用域结束」的实现机制。

#### 4.3.3 源码精读

`scope` 函数全貌：

[crossbeam-utils/src/thread.rs:146-203](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L146-L203)。逐段拆解。

**① 建好 Scope，跑用户 `f`（捕获其 panic）**：

[crossbeam-utils/src/thread.rs:159-171](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L159-L171)：

```rust
let wg = WaitGroup::new();                       // 计数=1
let scope = Scope::<'env> {
    handles: SharedVec::default(),
    wait_group: wg.clone(),                       // 计数=2（scope 持一份）
    _marker: PhantomData,
};
// Execute the scoped function, but catch any panics.
let result = panic::catch_unwind(panic::AssertUnwindSafe(|| f(&scope)));
// If an unwinding panic occurs before all threads are joined
// promote it to an aborting panic to prevent any threads from escaping the scope.
let guard = AbortOnPanic;                         // 守卫上岗
```

注意 `catch_unwind(AssertUnwindSafe(...))`：`f` 借用了 `'env` 数据、并非 `UnwindSafe`，这里用 `AssertUnwindSafe` 显式声明「我接受 unwind 风险」以换取「先把 `f` 的 panic 抓住、稍后再 `resume_unwind`」。`f` 的 panic 不会立刻传播，是因为后面还要先收尾（等子作用域、join 线程）。

**② 等待所有子作用域结束**：

[crossbeam-utils/src/thread.rs:174-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L174-L175)：

```rust
// Wait until all nested scopes are dropped.
drop(scope.wait_group);
wg.wait();
```

`drop(scope.wait_group)` 释放根 scope 持有的那份引用；`wg.wait()` 阻塞直到计数归零——即所有通过 `spawn`（含嵌套）衍生出的子 Scope 都已 drop，等价于「所有用户闭包 `f` 都已返回」。此刻共享 `handles` 表停止增长。

为何 `wg.wait()` 能等到 0 而不永久阻塞？因为每个子线程的 `scope`（带着 `wait_group` 的 clone）会在其闭包返回时（含 panic 时的 unwinding Drop）被释放，计数终会归零。底层依赖 `WaitGroup::wait` 的不丢唤醒协议（u2-l6）：

[crossbeam-utils/src/sync/wait_group.rs:110-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L110-L131)：

```rust
pub fn wait(self) {
    // 用 ManuallyDrop 取出 inner，避免消费 self 时再次触发 Drop（否则会重复 -1）
    let inner = unsafe {
        let slf = ManuallyDrop::new(self);
        core::ptr::read(&slf.inner)
    };
    if inner.count.fetch_sub(1, Ordering::AcqRel) == 1 {
        // 我是最后一个：抢锁后 notify_all
        drop(inner.lock.lock().unwrap());
        inner.cvar.notify_all();
        return;
    }
    // 否则：持锁 while 循环检查计数，等 notify_all
    let mut guard = inner.lock.lock().unwrap();
    while inner.count.load(Ordering::Acquire) != 0 {
        guard = inner.cvar.wait(guard).unwrap();
    }
}
```

这里有个精妙的 `ManuallyDrop` 技巧：`wait(self)` 按值消费 `self`，若不阻止，函数返回时 `self` 的 `Drop` 会再 `-1`，造成重复递减。用 `ManuallyDrop::new(self)` 包裹后 `core::ptr::read` 把 `inner`「偷」出来而不触发 `Drop`，由 `wait` 自己手动完成那一次 `-1`。不丢唤醒的保证来自注释所述顺序：通知方「先改计数、再抢锁、再 notify」，等待方「持锁时检查计数」——这与 u2-l6 的分析完全一致。

**③ join 剩余线程并收集 panic**：

[crossbeam-utils/src/thread.rs:178-188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L178-L188)：

```rust
let panics: Vec<_> = scope
    .handles
    .lock()
    .unwrap()
    // Filter handles that haven't been joined, join them, and collect errors.
    .drain(..)
    .filter_map(|handle| handle.lock().unwrap().take())  // 已被手动 join 的 → None，跳过
    .filter_map(|handle| handle.join().err())             // join，只保留 panic 错误
    .collect();

mem::forget(guard);                                      // 收尾平安完成，解除 abort 守卫
```

`drain(..)` 把整张表搬空；第一个 `filter_map` 用 `take()` 跳过已被手动 join 的句柄（`Option` 抢占机制）；第二个 `filter_map` 只留 `join().err()`——即 panic 的线程。被手动 join 过的线程不会重复 join，正常的线程 `join().err()` 为 `None` 被过滤。最后 `mem::forget(guard)` 解除 `AbortOnPanic`，表示「收尾平安，无需 abort 兜底」。

**④ 决定返回值**：

[crossbeam-utils/src/thread.rs:193-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L193-L202)：

```rust
match result {
    Err(err) => panic::resume_unwind(err),   // f 自己 panic：原样恢复传播（此时守卫已 forget，不会 abort）
    Ok(res) => {
        if panics.is_empty() {
            Ok(res)                          // 一切正常
        } else {
            Err(Box::new(panics))            // 有子线程 panic：把错误集合返回
        }
    }
}
```

优先级很清晰：`f` 自己 panic 最优先（`resume_unwind` 原样抛出，让调用栈看到原始 panic）；否则若任一子线程 panic，返回 `Err(Box::new(Vec<Box<dyn Any + Send>>))`；都没有才 `Ok`。注意 `resume_unwind` 在 `mem::forget(guard)` **之后**执行，所以 `f` 的 panic 不会被守卫升级成 abort——它只会按正常 unwinding 传播。

至此，`scope` 返回时：所有子线程的 `f` 已返回（第②步）、线程已彻底退出（第③步 join）、panic 已收集（第④步），`'env` 数据方可安全释放。

#### 4.3.4 代码实践

这是本讲的主实践任务（与大纲要求一致）：**解释「为何 `scope` 返回前所有 spawn 的线程一定已结束」，并构造 panic 场景验证 `scope` 返回 `Err` 且不影响其它线程**。

**实践目标**：

1. 用上面的源码分析，写一段因果链说明「返回前线程一定结束」。
2. 写一段示例代码（标记为「示例代码」，非仓库原有），制造一个子线程 panic，观察 `scope` 的返回值与其它线程的执行情况。

**操作步骤**：

1. 先写下你的因果解释（参考下方「预期结果」）。
2. 新建一个依赖 `crossbeam-utils` 的小 crate（或在仓库内 `cargo doc` 后照其文档示例本地试跑），写入下面的示例代码并 `cargo run`：

```rust
// 示例代码（非仓库原有）：观察 scope 对子线程 panic 的收集行为
use crossbeam_utils::thread;

fn main() {
    let result = thread::scope(|s| {
        s.spawn(|_| { println!("线程 A 正常运行"); 1 });
        s.spawn(|_| { println!("线程 B 正常运行"); 2 });
        s.spawn(|_| {
            println!("线程 C 即将 panic");
            panic!("C 故意 panic");
        });
        "scope 闭包正常返回"   // f 本身不 panic
    });

    match result {
        Ok(v)  => println!("未捕获任何 panic：{v}"),
        Err(e) => println!("scope 返回 Err，收到 {} 个线程 panic", e.len()),
    }
}
```

**需要观察的现象**：

- 终端会先打印「线程 A/B/C …」（A、B 不受 C 的 panic 影响，照常跑完）。
- 由于 C panic，`scope` 返回 `Err`，打印「scope 返回 Err，收到 1 个线程 panic」。
- A、B 的输出证明：**一个线程 panic 不会中断同一 scope 内其它线程**——因为每个线程的 `f` 独立运行，panic 只在收尾时被收集。

**预期结果（因果链）**：

> `scope` 返回前一定没有子线程在运行用户代码，因为：(1) `wg.wait()` 阻塞到所有子 Scope drop，而子 Scope 在其闭包（含 `f`）返回时才 drop，故 `wg.wait()` 返回 ⟺ 所有 `f` 已返回；(2) 随后 `drain(..)` + `join()` 逐个 join 剩余句柄，等线程彻底退出（含 drop 掉 `f` 的捕获环境）。两步走完，`'env` 数据才允许被释放，因此不会 use-after-free。

**待本地验证**：如果你修改示例让 `f` 本身也 `panic!`（而不是子线程），应观察到 `scope` 调用处直接 panic（`resume_unwind`），而非返回 `Err`——对应源码 `match result { Err(err) => panic::resume_unwind(err), … }` 分支。

#### 4.3.5 小练习与答案

**练习 1**：`scope` 收尾为何要先 `drop(scope.wait_group)` 再 `wg.wait()`？只调 `wg.wait()` 会怎样？

> **答案**：根线程持有 `wg` 与 `scope.wait_group` 两份引用。若不先 `drop(scope.wait_group)`，这份引用要等到 `scope` 整体 drop（即 `scope` 函数返回时）才释放，计数永远到不了 0，`wg.wait()` 会永久死锁。先 `drop(scope.wait_group)` 把根的两份引用之一释放掉，`wg.wait()` 才能在所有子 Scope drop 后等到 0。

**练习 2**：子线程 panic 时，`wg.wait()` 会一直阻塞吗？为什么？

> **答案**：不会。子线程 panic 会 unwind，unwinding 会运行其局部 `scope` 的 `Drop`，从而把那份 `wait_group` clone 释放掉（计数 -1）。因此即便子线程 panic，计数照样归零，`wg.wait()` 正常返回；panic 随后在第 ③ 步的 `join().err()` 里被收集进 `panics`。

**练习 3**：`match result` 里为什么是 `resume_unwind` 而不是直接 `unwrap`/`panic!`？

> **答案**：`resume_unwind(err)` 会带着**原始的 panic 信息与调用栈**继续 unwinding，对上层观察者而言就像 `f` 原地 panic 一样，不丢失任何上下文；而重新 `panic!` 会丢掉原始 payload。注意它在 `mem::forget(guard)` 之后执行，所以不会被 `AbortOnPanic` 升级成 abort。

---

## 5. 综合实践

把本讲的三个模块串起来，做一个「嵌套 spawn + 多线程 panic + WaitGroup 计数追踪」的综合练习。

**任务**：在一个 `scope` 里 spawn 一个「调度线程」，调度线程内部再用它收到的 `&Scope` 嵌套 spawn 两个工作线程（其中一个是 panic 线程）。对照源码，回答三个问题。

**示例代码（非仓库原有）**：

```rust
// 示例代码：嵌套 spawn + panic，综合观察 scope 的收尾行为
use crossbeam_utils::thread;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

fn main() {
    let done = Arc::new(AtomicUsize::new(0));

    let result = thread::scope(|s| {
        let done = Arc::clone(&done);
        // 调度线程：内部再嵌套 spawn
        s.spawn(move |s| {
            s.spawn(|_| { println!("嵌套工作线程 1 正常"); });      // 正常
            s.spawn(|_| { panic!("嵌套工作线程 2 故意 panic"); }); // panic
            done.fetch_add(1, Ordering::SeqCst);
        });
        "scope 闭包完成"
    });

    println!("调度线程完成计数 = {}", done.load(Ordering::SeqCst));
    match &result {
        Ok(_)  => println!("scope Ok"),
        Err(e) => println!("scope Err，收集到 {} 个 panic", e.len()),
    }
}
```

**请回答（结合源码）**：

1. 嵌套工作线程 1、2 的句柄进了**哪一张** `handles` 表？依据 [crossbeam-utils/src/thread.rs:439](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L439)（`Arc::clone(&self.scope.handles)`）说明。
2. 嵌套工作线程 2 的 panic 会被收集进根 `scope` 的返回值吗？为什么？提示：它的 `JoinHandle` 在共享表里，根收尾 `drain(..)` 会 join 到它。
3. 若把调度线程本身也改成 `panic!`，`done` 的计数还会是 1 吗？结合 panic 的 unwinding 顺序与 `WaitGroup::drop` 推断。

**参考答案**：

1. 进的是**根 scope 的同一张**表。因为子 Scope 克隆的是根 `handles` 的同一个 `Arc`（[:439](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L439)），所有层级的 spawn 都往同一张 `Vec` 里 `push`，根收尾时 `drain(..)` 能一次性拿到全部句柄。
2. 会。嵌套工作线程 2 的句柄在共享表里，根收尾 `drain` 后会 join 它，`join().err()` 返回其 panic，被收进 `panics`，故 `scope` 返回 `Err`。
3. 仍是 1。`done.fetch_add(1, …)` 在调度线程的 panic **之前**执行，计数已加 1；panic 发生在其后的 `s.spawn(...)` 调用或闭包返回阶段，但 `fetch_add` 已落地不会回滚。（若把 `fetch_add` 放到 panic 之后，则计数为 0——这正说明执行顺序决定可见结果。）

> 说明：嵌套工作线程 2 的 panic 是否一定被根 `scope` 收集，取决于调度线程是否在根收尾 `drain` 之前已完成 `s.spawn`——而 `wg.wait()` 恰好保证了「所有子 Scope 已 drop、即所有 spawn 已完成」，所以 drain 时表里必然已含其句柄。这正是第②步「等待子作用域」与第③步「drain」必须严格先后执行的原因。

## 6. 本讲小结

- `Scope` 是一张**线程句柄注册表**：`handles: Arc<Mutex<Vec<Arc<Mutex<Option<JoinHandle<()>>>>>>>`，靠 `Option` 的 `take()` 仲裁「手动 join vs 自动 join」谁真正接管，避免 double-join；`JoinHandle<()>` 的 `()` 是类型擦除，真实结果 `T` 走旁路 `SharedOption<T>`。
- spawn 的核心机关是一处 `unsafe { mem::transmute }`：把 `Box<dyn FnOnce() + Send + 'env>` 擦成 `+ 'static` 喂给标准库，安全性完全由「收尾自动 join」这条运行期不变量承担。
- `AbortOnPanic` 守卫覆盖整个收尾期，一旦收尾路径上发生 panic 就 `abort` 进程，杜绝「收尾期 unwind 导致子线程带着悬垂引用逃逸」的 use-after-free。
- 收尾严格三步走：`catch_unwind` 抓 `f` → `drop(scope.wait_group)` + `wg.wait()` 等所有子 Scope drop（保证 `f` 全返回、句柄表停止增长）→ `drain` + `join` 收集 panic。
- `WaitGroup` 的计数模型：根持两份引用（`wg` 与 `scope.wait_group`），每个 spawn 的子 Scope 持一份并在闭包返回（含 panic unwinding）时释放；`wg.wait()` 靠 u2-l6 的「改计数→抢锁→notify」与「持锁检查计数」协议不丢唤醒。
- 返回优先级：`f` 自身 panic 最先（`resume_unwind` 原样传播）；否则有子线程 panic 返回 `Err`，全正常返回 `Ok`。

## 7. 下一步学习建议

- 本讲结束了对 `crossbeam-utils` 全部并发原语的源码精读。下一单元（u3）将进入规模最大的 `crossbeam-channel`：建议从 [u3-l1 channel 总览与基本使用](u3-l1-channel-overview.md) 开始，先掌握 bounded/unbounded/rendezvous 三类通道的用法，再进入 flavors 架构。
- 若你想立刻检验对作用域线程的理解，可对照阅读标准库 `std::thread::scope`（Rust 1.63 起内置，文档注释里标注 crossbeam 的 `scope` 已 soft-deprecated）：两者的安全模型同源，但标准库实现更精简，比较阅读能加深对「`'env` 不变性 + 自动 join」这套范式的体会。
- 想继续深挖并发正确性验证，可跳到 [u7-l3 测试、loom 与并发正确性](u7-l3-testing-concurrency-correctness.md)，看 crossbeam 如何用 loom/miri/tsan 为这类含 `unsafe` 的并发原语兜底。
