# thread::scope 作用域线程原理

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「为什么需要作用域线程（scoped threads）」：它解决了 `std::thread::spawn` 只能借用 `'static` 数据的痛点。
- 把 [`thread::scope`](#) 的执行流程拆成五段：捕获 `f` 的 panic → 等 WaitGroup → join 残留线程 → 忘记守卫 → 决定返回值，并能解释每一段为何这么排。
- 解释 `AbortOnPanic` 守卫在哪个时间窗口被创建、又在何处被 `mem::forget`，以及它如何防止「线程逃逸出作用域」。
- 理解 panic 汇总机制：多个子线程 panic 时，错误如何被收集成 `Vec<Box<dyn Any + Send>>` 并以 `Err` 返回。
- 用真实源码验证上述结论，而不是凭印象。

本讲只讲 `scope()` 函数本身的原理与 panic 处理；`Scope::spawn` / `ScopedThreadBuilder` / `ScopedJoinHandle` 的细节（闭包装箱、生命周期擦除、返回值取回）留到下一讲 u4-l2。

## 2. 前置知识

### 2.1 `'static` 生命周期与借用检查器的冲突

`std::thread::spawn` 的签名要求闭包 `F: Send + 'static`。`'static` 在这里不是「活到程序结束」，而是「不借用任何非 `'static` 的数据」。原因是借用检查器无法证明新线程何时结束，所以只能保守地要求它不依赖任何可能比它先死的数据。

于是，当你想让一个线程借用栈上的局部变量时，编译器会报 [E0597](https://doc.rust-lang.org/stable/error_codes/E0597.html) `borrowed value does not live long enough`。本讲的模块文档专门用一段 `compile_fail` 示例展示了这个失败，见 [src/thread.rs:23-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L23-L68)。

作用域线程的核心承诺就一句话：**我保证在作用域结束前 join 掉所有子线程**。有了这个承诺，借用检查器就允许子线程借用栈上变量，因为变量的生命周期被「作用域结束前一定 join」这把锁保护住了。参见 [src/thread.rs:70-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L70-L79)。

### 2.2 panic 的两种策略：unwind 与 abort

Rust 默认 panic = unwind（栈回溯），可以通过 `panic = "abort"` 改为直接终止进程。本讲的 `AbortOnPanic` 守卫正是处理「unwind 过程中可能出问题」的场景，所以你需要知道：

- `std::panic::catch_unwind`：把闭包里的 panic 捕获成 `Err(Box<dyn Any + Send>)`，阻止其继续向上 unwinding。
- `std::panic::resume_unwind`：把捕获到的 panic 原样重新抛出（保持 panic 来源的回溯信息，比直接 `panic!` 更好）。
- `std::thread::panicking()`：当前线程是否正在 unwinding（用在 `Drop` 里判断「我是否在 panic 清理过程中」）。

### 2.3 WaitGroup（来自 u3-l2）

`scope()` 用 `WaitGroup` 来「等所有嵌套作用域结束」。回顾 u3-l2 的关键结论：

- `WaitGroup::new()` 把内部计数初始化为 `1`。
- `clone()` 让计数 `+1`，`drop()` / `wait()` 让计数 `−1`。
- 计数归零时唤醒所有等待者。

本讲会把这套计数机制用在「追踪所有子线程的内部 Scope 是否都已销毁」上。如果你对 WaitGroup 的「先 `fetch_sub`、再 `lock`、最后 `notify_all`」防丢失唤醒顺序还不熟，建议先复习 u3-l2。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
| --- | --- | --- |
| [src/thread.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs) | 作用域线程的全部实现 | `scope()` 函数主体（L146-203）、`Scope` 结构（L206-217）、类型别名（L120-121）、模块文档 |
| [src/sync/wait_group.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs) | 引用计数同步原语 | `new`/`Default`（L61-71）、`wait`（L110-131）、`Drop`（L134-142）、`Clone`（L144-151） |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs) | crate 根 | `pub mod thread;` 的门控条件（L98-L100） |
| [tests/thread.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs) | 集成测试 | `counter` / `counter_panic` / `panic_twice` / `panic_many` 等真实断言 |

模块门控值得先记一句：`thread` 模块需要 `feature = "std"` 且 `not(crossbeam_loom)`，见 [src/lib.rs:98-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L98-L100)。也就是说，在 `no_std` 和 loom 模型测试下都没有 `thread::scope`。

## 4. 核心概念与源码讲解

### 4.1 scope() 函数主体与执行流程

#### 4.1.1 概念说明

`thread::scope` 是 `crossbeam-utils` 提供的「作用域线程」入口。它的签名是：

```rust
pub fn scope<'env, F, R>(f: F) -> thread::Result<R>
where
    F: FnOnce(&Scope<'env>) -> R,
```

它接受一个闭包 `f`，给 `f` 传一个 `&Scope<'env>`，`f` 通过这个 `Scope` 来 `spawn` 子线程。`'env` 是一个**不变（invariant）生命周期**（由 `Scope` 内部的 `PhantomData<&'env mut &'env ()>` 实现，见 [src/thread.rs:214](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L214)），它代表「作用域外那些被子线程借用的数据的生命周期」。

`scope()` 对外承诺两件事：

1. **安全承诺**：在 `scope()` 返回之前，所有通过这个 `Scope` spawn 的线程都已经被 join（即不会再用到任何 `'env` 借用）。
2. **返回值约定**：若 `f` 与所有子线程都成功，返回 `Ok(f 的返回值)`；若任一子线程 panic，返回 `Err(Box<Vec<panic 载荷>>)`；若 `f` 自己 panic，则 `scope()` 自身也会 panic（把 `f` 的 panic 原样抛出）。

> 提示：自 Rust 1.63 起，标准库提供了更高效的 [`std::thread::scope`](https://doc.rust-lang.org/std/thread/fn.scope.html)。本 crate 的版本被标注为 **soft-deprecated**（见 [src/thread.rs:131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L131)），但实现思路依然是学习作用域线程的绝佳教材。

#### 4.1.2 核心流程

`scope()` 的执行可以拆成五段。理解这五段的顺序是本讲的核心：

```
┌─ 1. 准备：建 WaitGroup，建根 Scope（持有 wg 的克隆）
│
├─ 2. catch_unwind(f(&scope))        ← 用 AssertUnwindSafe 调用 f，捕获它的 panic
│       （子线程在此期间被 spawn 出去，借用 'env 数据）
│
├─ 3. 创建 AbortOnPanic 守卫          ← 进入「危险清理窗口」
│       drop(scope.wait_group)       ← 根 Scope 退出计数
│       wg.wait()                    ← 阻塞直到所有子 Scope 都被销毁（子线程跑完闭包）
│
├─ 4. join 所有未被手动 join 的线程    ← 收集 panic 错误
│       mem::forget(guard)           ← 离开危险窗口，守卫不再生效
│
└─ 5. match result：
        Err(f 的 panic) → resume_unwind        ← f panic 了，重新抛出（此时线程已全部安全 join）
        Ok(res) + 无子 panic → Ok(res)
        Ok(res) + 有子 panic → Err(Box::new(panics))
```

最关键的设计是：**先 `wg.wait()` 确认所有子线程的闭包都已执行完毕，再去 join 它们的 OS 句柄**。这两步不能合并，原因见 4.3。

#### 4.1.3 源码精读

`scope()` 的完整主体在 [src/thread.rs:146-203](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L146-L203)。下面按段拆开看。

**准备阶段**——建 WaitGroup 与根 Scope：

```rust
let wg = WaitGroup::new();                       // 计数 = 1（wg 自身）
let scope = Scope::<'env> {
    handles: SharedVec::default(),               // Arc<Mutex<Vec<...>>>
    wait_group: wg.clone(),                      // 计数 = 2（scope.wait_group）
    _marker: PhantomData,
};
```

> 见 [src/thread.rs:159-164](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L159-L164)。类型别名 `SharedVec<T> = Arc<Mutex<Vec<T>>>`、`SharedOption<T> = Arc<Mutex<Option<T>>>` 定义在 [src/thread.rs:120-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L120-L121)。

**调用 `f`**——用 `AssertUnwindSafe` 包一层再 `catch_unwind`：

```rust
// Execute the scoped function, but catch any panics.
let result = panic::catch_unwind(panic::AssertUnwindSafe(|| f(&scope)));
```

见 [src/thread.rs:167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L167)。`AssertUnwindSafe` 是必须的：`f` 借用了 `&scope`，而 `&Scope` 没有自动的 `RefUnwindSafe`。这里我们「断言」即使发生 unwind 也是安全的——这个断言的正当性正是由后面的 `AbortOnPanic` 守卫兜底的。

**等待所有嵌套作用域结束**——这是 WaitGroup 在本讲的用法：

```rust
// Wait until all nested scopes are dropped.
drop(scope.wait_group);
wg.wait();
```

见 [src/thread.rs:173-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L173-L175)。为什么这两行能等所有子线程跑完？关键在于 `Scope::spawn`（准确说在 `ScopedThreadBuilder::spawn`）会克隆一份 `wait_group` 进子线程：

```rust
// A clone of the scope that will be moved into the new thread.
let scope = Scope::<'env> {
    handles: Arc::clone(&self.scope.handles),
    wait_group: self.scope.wait_group.clone(),   // 计数 +1
    _marker: PhantomData,
};
```

见 [src/thread.rs:437-442](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L437-L442)（这部分细节属于 u4-l2，这里只需注意 `wait_group.clone()`）。这个内部 `scope` 是子线程闭包里的局部变量，闭包正常返回或 panic unwind 时都会被 drop，从而 drop 掉它持有的 `wait_group`，让计数 `−1`。

**用一个具体计数追踪串起来**（假设 spawn 了 1 个还在运行的子线程）：

| 时刻 | 事件 | `WaitGroup` 内部计数 |
| --- | --- | --- |
| `new()` | 初始化 | 1（`wg`） |
| `wg.clone()` 进根 Scope | clone | 2（`wg` + `scope.wait_group`） |
| 子线程 spawn | clone | 3（多了子线程的内部 Scope 持有的那份） |
| `drop(scope.wait_group)` | 根 Scope 退出 | 2 |
| `wg.wait()` 进入 | `fetch_sub` → 计数 1，旧值 2 ≠ 1，不是最后一个 → 进 `while count != 0` 等待 | 1 |
| 子线程闭包结束，内部 Scope 被 drop | `Drop::fetch_sub` → 旧值 1，是最后一个 → `notify_all` | 0 |
| `wg.wait()` 被唤醒 | `count == 0`，退出循环返回 | 0 |

> 计数归零的判定逻辑见 [src/sync/wait_group.rs:117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L117)（`fetch_sub(1, AcqRel) == 1` 表示自己是最后一个）与 [src/sync/wait_group.rs:127-130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/wait_group.rs#L127-L130)（等待循环）。

于是 `wg.wait()` 返回时，可以确信：**所有子线程的闭包都已经执行完毕**（无论正常返回还是 panic）。这是后续 join 阶段能安全进行的前提。

#### 4.1.4 代码实践

**实践目标**：亲手验证作用域线程能借用栈上变量，并跑通仓库自带的并发计数测试。

**操作步骤**：

1. 在 `crossbeam-utils` 仓库根目录写一个临时 binary（或直接看测试）。为了不修改源码，我们直接运行现成测试。
2. 运行 `counter` 测试，它正是「多个子线程并发 `fetch_add`，主线程在 scope 返回后读取」：

```bash
cd crossbeam-utils
cargo test --features std counter -- --nocapture
```

对应测试源码在 [tests/thread.rs:33-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L33-L46)，关键片段：

```rust
let counter = AtomicUsize::new(0);
thread::scope(|scope| {
    for _ in 0..THREADS {
        scope.spawn(|_| {
            counter.fetch_add(1, Ordering::Relaxed);
        });
    }
})
.unwrap();
assert_eq!(THREADS, counter.load(Ordering::Relaxed));
```

3. 自己写一个最小例子，验证它能借用栈上 `Vec`（不放进仓库，放你自己的 scratch 项目）：

```rust
// 示例代码：放在你自己的 binary crate 里运行
use crossbeam_utils::thread;

fn main() {
    let data = vec![10, 20, 30];          // 栈上的局部变量
    thread::scope(|s| {
        for x in &data {                   // 借用栈上 Vec，编译通过
            s.spawn(move |_| println!("{x}"));
        }
    }).unwrap();
    // scope 返回后，data 仍然可用
    println!("after scope: {:?}", data);
}
```

**需要观察的现象**：

- 步骤 2 的测试通过，`counter == THREADS == 10`。
- 步骤 3 中，闭包 `move |_|` 借用了 `&data` 的元素却无需 `'static`，这正是作用域线程的威力；`scope` 返回后 `data` 仍可读。

**预期结果**：测试通过、示例正常打印。若你不确定命令在本机的输出，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把步骤 3 的 `thread::scope` 换成 `std::thread::spawn`，会发生什么编译错误？为什么？

**参考答案**：会报 E0597 `data does not live long enough`。因为 `std::thread::spawn` 要求闭包 `Send + 'static`，而闭包借用了栈上的 `&data`，借用检查器无法证明新线程会在 `data` 被释放前结束。`thread::scope` 通过「scope 结束前一定 join」的承诺解除了这个顾虑。

**练习 2**：为什么 `scope()` 要把 `wg.clone()` 存进根 `Scope`，而不是直接用 `wg` 本身？

**参考答案**：因为根 `Scope` 的 `wait_group` 字段会被 `Scope::spawn` 进一步 clone 给每个子线程（建立计数关联），而 `wg` 本身要留在 `scope()` 函数栈上用于最后调用 `wg.wait()`。`drop(scope.wait_group)` 这一步显式让根 Scope 那份引用退出计数，确保 `wg.wait()` 时只剩 `wg` 自己作为「主线程份额」，从而 `wait` 能正确等到所有子 Scope 销毁。

---

### 4.2 AbortOnPanic 守卫与防线程逃逸

#### 4.2.1 概念说明

`scope()` 把「子线程借用 `'env` 数据」的安全承诺建立在一个前提上：**在 `scope()` 栈帧被销毁前，所有子线程都已 join**。但是，如果 `scope()` 函数自身在清理过程中发生 panic 并开始 unwind，会发生什么？

危险在于：unwind 会拆毁 `scope()` 的栈帧，连带释放栈上被借用的数据；可此时可能还有子线程没 join，仍然在访问那些数据——这就破坏了安全承诺，属于**未定义行为**（use-after-free 一类）。

`catch_unwind` 已经捕获了 `f` 自己的 panic（不会从这里漏出去），所以这个风险窗口只存在于 `catch_unwind` 之后、join 完成之前那段「清理代码」。`AbortOnPanic` 守卫就是用来覆盖这个窗口的：一旦在这段窗口里发生任何 unwind，就强制 `std::process::abort()`，宁可整个进程崩溃，也不允许栈帧被拆掉时还有线程存活。

#### 4.2.2 核心流程

`AbortOnPanic` 是定义在 `scope()` 函数内部的局部结构体，只实现了一个 `Drop`：

```
        ┌─ let guard = AbortOnPanic;     ← 进入危险窗口（守卫生效）
        │     drop(scope.wait_group);
        │     wg.wait();
        │     join loop（可能触发 Mutex 中毒 → panic）
        │
        └─ mem::forget(guard);           ← 退出危险窗口（守卫不再生效）

        match result {
            Err(err) => resume_unwind(err);   ← 此时线程已全部 join，安全
            ...
        }
```

守卫的 `Drop::drop` 只做一件事：`if thread::panicking() { abort() }`。也就是说，只要在守卫生效期间发生了 unwind（`thread::panicking()` 为真），析构时就 abort。`mem::forget(guard)` 则是「任务安全完成」的信号——之后即使 `resume_unwind(f 的 panic)` 再抛出 panic，也不会触发 abort，因为此时所有子线程早已 join 完毕。

#### 4.2.3 源码精读

守卫的定义在 `scope()` 内部，见 [src/thread.rs:150-157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L150-L157)：

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

守卫的「上岗」与「下岗」分别在这两行：

```rust
// If an unwinding panic occurs before all threads are joined
// promote it to an aborting panic to prevent any threads from escaping the scope.
let guard = AbortOnPanic;          // 上岗
// ... wg.wait() 与 join loop 都在守卫保护下 ...
mem::forget(guard);                // 下岗
```

> 见 [src/thread.rs:169-188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L169-L188)。

那么，危险窗口里到底什么东西可能 panic？主要是 join 循环里那些 `.lock().unwrap()`：

```rust
let panics: Vec<_> = scope
    .handles
    .lock()                        // ← Mutex 中毒会 panic
    .unwrap()
    .drain(..)
    .filter_map(|handle| handle.lock().unwrap().take())   // ← 同上
    .filter_map(|handle| handle.join().err())
    .collect();
```

> 见 [src/thread.rs:178-186](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L178-L186)。

`Mutex::lock().unwrap()` 在 Mutex **中毒**（持有锁时 panic）时会返回 `Err`，`.unwrap()` 把它变成 panic。虽然这里的内部 Mutex 不太容易被污染，但作为防御性编程，`AbortOnPanic` 兜住了所有可能的 unwind，保证「栈帧拆除时绝无线程存活」这条不变量。

最后看返回值逻辑（守卫下岗之后），见 [src/thread.rs:193-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L193-L202)：

```rust
match result {
    Err(err) => panic::resume_unwind(err),   // f 自己 panic → 重新抛出（线程已 join，安全）
    Ok(res) => {
        if panics.is_empty() {
            Ok(res)                          // 全部成功
        } else {
            Err(Box::new(panics))            // 有子线程 panic → 汇总成 Err
        }
    }
}
```

注意 `resume_unwind` 也会让当前线程进入 panicking 状态，但它发生在 `mem::forget(guard)` 之后，所以不会触发 abort——这正是「先 forget、再 resume_unwind」的原因。

#### 4.2.4 代码实践

**实践目标**：通过阅读真实测试，理解 panic 在作用域线程里的传播路径，并推理 `panic = abort` 下的行为差异。

**操作步骤**：

1. 阅读 `counter_panic` 测试，[tests/thread.rs:68-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L68-L86)。它先 spawn 一个会 panic 的线程，再 spawn 一批正常计数线程，最后断言 `result.is_err()` 且计数仍正确：

```rust
let result = thread::scope(|scope| {
    scope.spawn(|_| { panic!("..."); });
    sleep(Duration::from_millis(100));
    for _ in 0..THREADS {
        scope.spawn(|_| { counter.fetch_add(1, Ordering::Relaxed); });
    }
});
assert_eq!(THREADS, counter.load(Ordering::Relaxed));
assert!(result.is_err());
```

2. 运行该测试：

```bash
cargo test --features std counter_panic -- --nocapture
```

3. **推理练习**：假设把 `Cargo.toml` 里加上 `[profile.test] panic = "abort"`（或用 `RUSTFLAGS="-C panic=abort"`），重新思考——
   - `catch_unwind` 还能捕获 `f` 的 panic 吗？
   - `AbortOnPanic` 还有没有意义？

**需要观察的现象**：

- 步骤 2 测试通过：单个子线程 panic 不会让其他子线程少计数，且 `scope()` 返回 `Err`。
- 步骤 3 是源码阅读型推理，不需要真的改 `panic=abort`（改动较大且影响全局）。

**预期结果**：

- `counter_panic` 通过。
- 推理答案见下方小练习 2。

#### 4.2.5 小练习与答案

**练习 1**：`AbortOnPanic` 为什么定义成 `scope()` 内部的局部结构体，而不是 crate 级的 `pub struct`？

**参考答案**：因为它只在 `scope()` 的清理窗口里使用，没有外部消费者；定义在函数内部可以避免污染 crate 的命名空间，也表达了「这是一个一次性的局部守卫」的意图。它的全部行为就是一个 `Drop`，没有任何字段。

**练习 2**：在 `panic = "abort"` 配置下，`AbortOnPanic` 守卫是否还有实际作用？

**参考答案**：基本没有。`panic = "abort"` 下，任何 panic 都会立即终止进程，`catch_unwind` 本身就捕不到 panic（标准库文档明确指出），`scope()` 的 `match result` 也走不到 `resume_unwind` 那条路。换句话说，`AbortOnPanic` 是为默认的 `panic = "unwind"` 模式设计的兜底；在 abort 模式下，操作系统级别的 abort 已经天然保证了「不会有线程逃逸」。`scope()` 的文档注释（[src/thread.rs:128-129](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L128-L129)）也提到「if panics are implemented by aborting the process, no error is returned」，正是这个意思。

---

### 4.3 自动 join 与 panic 汇总

#### 4.3.1 概念说明

`scope()` 有两种 join：

- **手动 join**：用户在 `f` 里调用 `ScopedJoinHandle::join`，把某个线程的结果取走。被手动 join 过的线程，它的 `JoinHandle` 已经从 `Option` 里被 `take()` 走，不再参与后续清理。
- **自动 join**：`scope()` 在末尾把所有「还没被手动 join」的线程统一 join 一遍。

之所以要自动 join，是因为「scope 结束前必须 join 所有线程」是安全承诺，不能依赖用户记得手动 join 每一个。`Scope` 内部用一个共享列表 `handles` 记录所有 spawn 出去的线程句柄，`scope()` 末尾遍历这个列表，跳过已被取走的，join 其余的，并把其中 panic 的错误收集起来。

panic 汇总的返回类型值得记住：`thread::Result<R>` = `Result<R, Box<dyn Any + Send + 'static>>`。当有子线程 panic 时，返回的是 `Err(Box::new(Vec<Box<dyn Any + Send + 'static>>))`——即把多个 panic 载荷装进一个 `Vec` 再装箱。测试里用 `downcast_ref::<Vec<Box<dyn Any + Send + 'static>>>()` 把它取回来。

#### 4.3.2 核心流程

`Scope` 的关键字段有三个（见 [src/thread.rs:206-215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L206-L215)）：

```rust
pub struct Scope<'env> {
    handles: SharedVec<SharedOption<thread::JoinHandle<()>>>,  // 所有线程的 OS 句柄
    wait_group: WaitGroup,                                      // 追踪子 Scope 是否都销毁
    _marker: PhantomData<&'env mut &'env ()>,                  // 不变生命周期
}
```

注意 `handles` 的类型：`Arc<Mutex<Vec<Arc<Mutex<Option<JoinHandle>>>>>>`。两层 `Arc<Mutex>`：

- 外层 `Arc<Mutex<Vec<...>>>`：多个线程（包括子线程做嵌套 spawn 时）共同往列表里 push。
- 内层 `Arc<Mutex<Option<JoinHandle>>>`：让「手动 join」和「自动 join」能通过 `take()` 互斥地取走句柄，避免重复 join。

自动 join 的流程：

```
1. wg.wait() 已返回 → 所有子线程闭包已结束（但 OS 句柄还没回收）
2. 锁住 handles 列表，drain 出全部条目
3. 对每个条目：take() 取出 Option 里的 JoinHandle（已被手动 join 过的这里是 None，跳过）
4. 对取出的 JoinHandle 调 .join()，若 .err() 是 Some（线程 panic）就收集
5. 把收集到的 Vec<Box<dyn Any>> 装箱成 Err 返回
```

#### 4.3.3 源码精读

自动 join 的循环在 [src/thread.rs:178-186](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L178-L186)：

```rust
let panics: Vec<_> = scope
    .handles
    .lock().unwrap()
    // Filter handles that haven't been joined, join them, and collect errors.
    .drain(..)                                         // 取走列表所有权，腾空原 Vec
    .filter_map(|handle| handle.lock().unwrap().take())// 取出 Option<JoinHandle>，None 被滤掉
    .filter_map(|handle| handle.join().err())          // join；只有 panic 的留下错误
    .collect();
```

两个 `filter_map` 的配合很巧妙：

- 第一个 `filter_map(... take())`：对每个内层 `Arc<Mutex<Option<JoinHandle>>>` 加锁并 `take()`。若返回 `Some(handle)` 说明这个线程还没被 join（保留）；若 `None` 说明已被手动 join（丢弃）。
- 第二个 `filter_map(handle.join().err())`：`JoinHandle::join` 返回 `thread::Result<()>`，`.err()` 在线程 panic 时给出 `Some(Box<dyn Any>)`，正常结束时给 `None`（丢弃）。所以最终 `panics` 只含 panic 载荷。

随后，根据 `panics` 是否为空决定返回值（见 [src/thread.rs:195-201](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L195-L201)）：空则 `Ok(res)`，非空则 `Err(Box::new(panics))`。

**为什么必须先 `wg.wait()` 再 join？** 因为 `JoinHandle::join` 本身就会阻塞到线程结束，似乎可以省掉 WaitGroup。但 `wg.wait()` 等待的事件是「子线程闭包里的内部 `Scope` 已销毁」，这一步保证了**嵌套 spawn 的所有孙线程也都已经登记进 `handles` 列表**。如果直接 join 而不等 WaitGroup，可能出现「父线程闭包结束了、但它刚 spawn 的孙线程还没来得及把句柄 push 进列表」的窗口，导致孙线程漏 join，安全承诺被打破。所以 `wg.wait()` 不是冗余，而是**建立「列表已完整」这一不变量**的栅栏。

#### 4.3.4 代码实践

**实践目标**：观察多线程 panic 的汇总返回值，并学会 downcast 取回 panic 载荷。

**操作步骤**：

1. 运行仓库自带的 `panic_twice` 测试，它断言两个 panic 都被收集：

```bash
cargo test --features std panic_twice -- --nocapture
```

2. 阅读它的断言写法，[tests/thread.rs:88-110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/thread.rs#L88-L110)：

```rust
let result = thread::scope(|scope| {
    scope.spawn(|_| { sleep(Duration::from_millis(500)); panic!("thread #1"); });
    scope.spawn(|_| { panic!("thread #2"); });
});
let err = result.unwrap_err();
let vec = err.downcast_ref::<Vec<Box<dyn Any + Send + 'static>>>().unwrap();
assert_eq!(2, vec.len());
```

3. 自己写一个最小示例（放你的 scratch 项目），spawn 3 个都 panic 的线程，打印 panic 数量：

```rust
// 示例代码
use crossbeam_utils::thread;
use std::any::Any;

fn main() {
    let result = thread::scope(|s| {
        s.spawn(|_| panic!("boom-1"));
        s.spawn(|_| panic!("boom-2"));
        s.spawn(|_| panic!("boom-3"));
    });
    let err = result.unwrap_err();
    let vec = err.downcast_ref::<Vec<Box<dyn Any + Send>>>().unwrap();
    println!("collected {} panics", vec.len());   // 预期 3
}
```

**需要观察的现象**：

- `panic_twice` 通过，`vec.len() == 2`。
- 步骤 3 打印 `collected 3 panics`。

**预期结果**：上述断言成立。若在本机运行结果不一致，记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `handles` 的内层是 `Arc<Mutex<Option<JoinHandle>>>` 而不是直接 `JoinHandle`？

**参考答案**：因为同一个句柄可能被「手动 join」或「自动 join」两处取用，需要互斥地表达「是否已被取走」。`Option` 表示这个状态（`Some` = 未 join，`None` = 已被 take），`Mutex` 保证并发安全，`Arc` 让 `ScopedJoinHandle`（用户持有）和 `handles` 列表（`Scope` 持有）共享同一个句柄。`take()` 一次性取出并标记为 `None`，确保不会重复 join。

**练习 2**：如果某个子线程既被用户手动 `join()` 过，又被自动 join 扫到，会发生什么？

**参考答案**：什么也不会重复发生。手动 `join` 内部已经 `take().unwrap()` 把 `Option` 取成了 `None`（见 [src/thread.rs:536](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L536)），所以自动 join 循环里的第一个 `filter_map(... take())` 会拿到 `None` 从而把这个条目滤掉，不会对同一个线程 join 两次。

---

## 5. 综合实践

**任务**：用 `thread::scope` 并发处理一个栈上 `Vec` 的多个分片，每个线程借用对应分片求局部和，主线程在 `scope` 返回后汇总，体会栈上借用如何通过编译。

**目标**：把本讲三个最小模块串起来——你会用到「栈上借用通过编译（4.1）」、「scope 返回即所有线程已 join（4.1/4.3）」、「即使某个分片线程 panic 也能拿到 `Err` 汇总（4.3）」。

**示例代码**（放你自己的 binary crate 运行）：

```rust
// 示例代码：栈上 Vec 分片并行求和
use crossbeam_utils::thread;

fn main() {
    let data: Vec<i32> = (1..=100).collect();   // 栈上的局部 Vec
    let n = 4;
    let chunk = (data.len() + n - 1) / n;

    // 每个 Scope::spawn 闭包都要 FnOnce(&Scope) -> T，这里 T = i32
    let result: thread::Result<Vec<i32>> = thread::scope(|scope| {
        let mut partials = Vec::new();
        for slice in data.chunks(chunk) {        // 借用栈上 data 的分片
            let handle = scope.spawn(move |_| {
                // 子线程持有 &slice 的 move 闭包，借用栈上数据
                slice.iter().copied().sum::<i32>()
            });
            partials.push(handle);
        }
        // 逐个手动 join 取回局部和
        partials.into_iter().map(|h| h.join().unwrap()).collect()
    });

    let partials = result.unwrap();
    let total: i32 = partials.iter().sum();
    println!("partials = {:?}, total = {}", partials, total);
    assert_eq!(total, (1..=100).sum::<i32>());
}
```

**操作步骤**：

1. 新建一个 binary crate，加入依赖 `crossbeam-utils = { version = "0.8", features = ["std"] }`。
2. 把上面的示例代码粘进 `main.rs`，`cargo run`。
3. **观察现象**：
   - 编译通过——尽管每个子线程都借用了栈上的 `data` 分片。
   - 打印的 `total == 5050`。
   - `scope` 返回后 `data` 仍然存活可用（可在 `scope(...)` 调用之后再 `println!("{:?}", &data[..5])` 验证）。
4. **进阶**：故意把其中一个分片的闭包改成 `panic!("boom")`，把 `result.unwrap()` 改成匹配 `Err`，downcast 出 `Vec<Box<dyn Any + Send>>`，验证 panic 汇总路径（对应 4.3）。

**预期结果**：正常版打印 `total = 5050`；进阶版捕获到至少 1 个 panic。

**若无法确定运行结果**：示例代码中 `chunks` 的借用顺序、`handle.join()` 的返回类型在不同工具链版本下应当稳定，但若你在本机遇到编译问题，请记为「待本地验证」并把报错对照 4.1.3 的签名检查 `T: Send + 'env` 约束是否满足。

## 6. 本讲小结

- `thread::scope` 通过「scope 结束前一定 join 所有子线程」的承诺，让借用检查器允许子线程借用栈上的非 `'static` 数据——这是作用域线程存在的根本理由。
- `scope()` 的执行五段：`catch_unwind(f)` → 创建 `AbortOnPanic` 守卫 → `drop(scope.wait_group)` + `wg.wait()` → join 残留线程 → `mem::forget(guard)` 后决定返回值。
- `WaitGroup` 在这里的角色是「追踪所有子 Scope 是否都已销毁」：子线程闭包结束时 drop 掉它持有的 `wait_group` 克隆，使计数归零；`wg.wait()` 返回即意味着所有子线程闭包已结束。
- `AbortOnPanic` 守卫覆盖「子线程已 spawn 但尚未全部 join」的危险窗口，窗口内任何 unwind 都被升级为 `abort()`，确保栈帧拆除时没有线程存活。
- 自动 join 用两层 `Arc<Mutex<Option<JoinHandle>>>` 让「手动 join」与「自动 join」互斥取用句柄；多个子线程的 panic 被收集成 `Vec<Box<dyn Any + Send>>` 并以 `Err` 返回。
- 本 crate 的 `thread::scope` 自 Rust 1.63 起被 soft-deprecated，推荐改用 `std::thread::scope`，但其实现仍是学习作用域线程原理的优秀范例。

## 7. 下一步学习建议

- 下一讲 **u4-l2「Scope、ScopedThreadBuilder 与 ScopedJoinHandle」** 会钻进本讲刻意略过的细节：`Scope::spawn` 如何把闭包 `Box<dyn FnOnce() + Send + 'env>` 用 `mem::transmute` 擦除生命周期成 `'static` 以便交给 `std::thread::spawn`、`ScopedJoinHandle::join` 如何用 `SharedOption` 取回返回值、以及嵌套 spawn 的写法。
- 在阅读 u4-l2 前，建议回顾本讲 [src/thread.rs:424-480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/thread.rs#L424-L480) 的 `ScopedThreadBuilder::spawn`，重点看 `wait_group.clone()` 与闭包装箱两段。
- 想对比工业级实现，可直接读标准库 [`std::thread::scope`](https://doc.rust-lang.org/src/std/thread/scoped.rs.html) 的源码，观察它如何用一组原子计数器替代本讲的 `WaitGroup + Arc<Mutex<Vec<...>>>`，从而做到「更高效」。
- 如果你对 panic 安全（panic safety）还感兴趣，可顺带阅读 [src/sync/parker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs)（u3-l1）中 `Mutex` 中毒的处理，与本讲的 `AbortOnPanic` 形成对照。
