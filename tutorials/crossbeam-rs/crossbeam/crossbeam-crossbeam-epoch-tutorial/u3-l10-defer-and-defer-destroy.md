# 延迟执行：defer / defer_unchecked / defer_destroy

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分 `Guard` 上三种延迟执行 API（`defer` / `defer_unchecked` / `defer_destroy`）各自的安全契约与适用场景。
- 说清楚为什么 `defer_unchecked` 要求闭包必须 `move`、为什么它放宽了 `Send` 约束、以及背后的「宽限期后无人再用」推理。
- 把一个延迟闭包从「本地 bag」一路追踪到「全局 queue 被回收」，并解释 `flush()` 在这条路径上的作用。
- 知道 `unprotected()` 假守卫下 `defer` 会立即执行，以及它在构造/析构场景里的价值。
- 自己写出一个用 `defer_destroy` 延迟释放节点的简化版 Treiber 栈。

## 2. 前置知识

本讲承接 [u3-l9 Guard：pin 语义与可重入](u3-l9-guard-and-pin.md)，默认你已经理解：

- **`Guard`** 是「线程已 pin」的轻量凭证，内部只有一根 `*const Local` 裸指针；真正的 pin 状态住在 `Local` 里。
- **`unprotected()`** 返回 `local` 为 null 的假守卫，不真正 pin。
- **宽限期（grace period）**：在第一讲里定义为「全局 epoch 相对垃圾盖戳前进满 2 次」即可安全回收。本讲会反复用到这个结论。

此外需要一点 Rust 基础：闭包的 `move` 语义、`Send` trait、以及 `unsafe` 的含义。

一个直觉性的比喻：defer 就像「寄存一个定时炸弹」。你交给 `Guard` 一个闭包，它**保证**不会立刻引爆，而是等到所有「此刻正 pin 着的线程」都 unpin 之后（即宽限期过去），才由某个线程把它拆掉（执行）。在那之前，闭包捕获的对象一定还活着、还能被别的线程读到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard` 类型本身，三种 `defer*` 与 `flush` 的公开 API 都在这里。 |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 延迟闭包的「存储与回收车间」：线程本地 `Bag`、全局 `Global`（含 queue/epoch）、`Local::defer` / `Local::flush` / `Global::push_bag` / `Global::collect`。 |
| [src/deferred.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs) | `Deferred` 类型：把不定大小的 `FnOnce` 内联或装箱，是 `Bag` 里存放的单元（本讲只顺带提及，深入留给 u3-l11）。 |

阅读顺序建议：先看 `guard.rs` 里三个 `defer*` 的签名与文档注释（建立 API 心智模型），再下沉到 `internal.rs` 看 `Local::defer` 和 `Global::collect`，最后回到 `flush` 把整条链路串起来。

## 4. 核心概念与源码讲解

### 4.1 defer 与 defer_unchecked：延迟执行任意闭包

#### 4.1.1 概念说明

`defer` 系列解决的问题是：**「我现在要把一个对象从数据结构里摘掉，但别的线程可能还握着指向它的旧指针，我不能马上释放它。」** 于是我们注册一个「稍后释放它」的闭包，交给 epoch 系统在确认安全之后再执行。

crossbeam-epoch 提供了两层 API：

- `defer(f)`：**安全**函数。它在类型层面要求 `F: Send + 'static`，编译器帮你把关。
- `defer_unchecked(f)`：**unsafe** 函数。它**不**要求 `F: Send`，把「这个闭包可以安全地被别的线程执行」这一保证交给调用方。

为什么需要 unsafe 版本？因为最常见的用法——延迟释放一个 `Shared<'g, T>`——里，`Shared` 是 `!Send` 的（它带生命周期、带裸指针），Rust 的类型系统证明不了它能跨线程移动。但在「宽限期已过、该对象已无人引用」的前提下，让任意线程去析构它是**实际安全**的。库作者选择相信调用方，于是放宽 `Send`。这正是文档里那句 *"We intentionally didn't require `F: Send`"* 的含义。

#### 4.1.2 核心流程

无论哪个版本，闭包都要满足两条不变量：

1. **不能借用栈上的数据**：闭包可能在当前函数返回很久之后才执行，那时栈帧早已销毁。所以**必须 `move`**，把所需数据捕获进闭包自己拥有的存储里。
2. **可以被任意线程执行**：本地 bag 满了之后会被推入全局 queue，任何线程调用 `collect()` 时都可能弹出并 drop 这个 bag，从而在**它自己的线程上**执行你的闭包。所以闭包捕获的所有东西实际都必须能跨线程移动。

`defer_unchecked` 的执行伪代码：

```text
fn defer_unchecked(self, f):
    if self.local 非空:                 # 真 guard
        local.defer(Deferred::new(move || drop(f())), self)
    else:                               # unprotected 假 guard
        drop(f())                       # 立即执行，不延迟
```

注意 `move || drop(f())`：它把原闭包 `f` 的返回值 `R` 直接丢掉。也就是说，无论 `f` 声明返回什么，defer 都只关心它的副作用，返回值不会被保留。

#### 4.1.3 源码精读

安全的 `defer` 只是把调用转发给 `defer_unchecked`，由它承担 unsafe 责任：[src/guard.rs:90-98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L90-L98)。它额外用 `F: Send + 'static` 约束换来了「无需写 `unsafe`」。

核心实现在 `defer_unchecked`：[src/guard.rs:189-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L189-L200)。

```rust
pub unsafe fn defer_unchecked<F, R>(&self, f: F)
where
    F: FnOnce() -> R,
{
    unsafe {
        if let Some(local) = self.local.as_ref() {
            local.defer(Deferred::new(move || drop(f())), self);
        } else {
            drop(f());   // unprotected 路径，4.4 节详解
        }
    }
}
```

- `self.local.as_ref()` 把裸指针转成 `Option<&Local>`；非空说明是真守卫。
- 真 guard 分支把闭包包成 `Deferred`（见 u3-l11），交给 `Local::defer`（4.3 节）。
- 文档里反复强调的 *"ALWAYS use `move`"* 对应 `Deferred::new(move || ...)`——这一层 `move` 是库帮你加的，但**你传进来的闭包本身仍必须 `move` 捕获**，否则 `f` 会借用栈。

#### 4.1.4 代码实践

**目标**：体会 `defer` 与 `defer_unchecked` 的区别，以及「为什么闭包必须 `move`」。

操作步骤：

1. 新建一个 binary，依赖 `crossbeam-epoch`。
2. 写下面这段「延迟打印」示例（**示例代码**，非项目原有）：

```rust
use crossbeam_epoch as epoch;

fn main() {
    let guard = &epoch::pin();

    // (a) 安全版：String 是 Send，编译通过
    let msg1 = String::from("from safe defer");
    guard.defer(move || println!("{}", msg1));

    // (b) unsafe 版：放宽 Send，但闭包仍必须 move
    let msg2 = String::from("from defer_unchecked");
    unsafe {
        guard.defer_unchecked(move || println!("{}", msg2));
    }

    // 主动 flush，让本地 bag 进全局队列并尽快 collect
    guard.flush();
}
```

3. 把 (b) 里的 `move` 删掉再编译，观察报错类型（通常是生命周期/借用错误，因为 `msg2` 会被借用进闭包）。

需要观察的现象 / 预期结果：

- 带 `move` 的版本能编译运行，两条 `println` 都会打印（顺序、时机由 epoch 决定，`flush` 后通常会很快触发）。
- 删掉 `move` 后编译失败，证明「闭包不能借用栈」是硬约束。
- 若运行结果在你本机有出入（例如两条都没打印就退出），属正常——defer 只保证「在宽限期之后、尽量快」，不保证进程退出前一定执行。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `defer_unchecked` 的签名里没有 `F: Send`，但文档仍说「another thread may execute `f`」？这两者矛盾吗？

**参考答案**：不矛盾。`Send` 是**类型系统层面的静态证明**，而「可能被别的线程执行」是**运行时事实**。典型场景里被延迟释放的 `Shared` 是 `!Send`，类型系统证明不了；但在宽限期过后它已无人引用，跨线程析构实际安全。库把这一推理责任交给调用方，于是用 `unsafe` 替代了 `Send` 约束。

**练习 2**：下面这段代码有什么隐患？

```rust
let s = String::from("hi");
unsafe { guard.defer_unchecked(|| println!("{}", s)); }
```

**参考答案**：闭包没有 `move`，按引用捕获了栈上的 `s`。`defer_unchecked` 可能在当前函数返回后才执行闭包，届时 `s` 已被释放，构成悬垂引用（use-after-free）。修正：改成 `move || println!("{}", s)`。

---

### 4.2 defer_destroy：延迟析构对象的标准姿势

#### 4.2.1 概念说明

`defer_destroy` 是三个 API 里**最常用**的一个。它的语义很纯粹：**「把这个 `Shared` 指向的对象，在宽限期过后析构并回收内存。」** 它内部就是一行 `defer_unchecked(move || ptr.into_owned())`——把借来的 `Shared` 升级成拥有所有权的 `Owned`，然后 drop 掉（`Owned::drop` 既运行 `T` 的析构，又归还内存，见 u2-l6）。

它之所以单独存在，是因为「延迟析构一个从 `Atomic` 摘下来的对象」是无锁数据结构里**反复出现**的固定模式，封装成专门方法更醒目、更不容易写错。

#### 4.2.2 核心流程

`defer_destroy` 的安全契约（来自文档 *# Safety*）：

1. **该对象已经不能被其他线程触达**——即它已经从共享数据结构里被原子地摘除（通常是一次成功的 CAS）。
2. 由于析构可能由别的线程执行，对象本身在概念上要能跨线程移动（但因为走 `defer_unchecked`，类型层面不强制 `T: Send`，理由同 4.1）。

执行流程：

```text
defer_destroy(ptr: Shared):
    defer_unchecked(move || ptr.into_owned())   # 宽限期后: Shared -> Owned -> drop
```

为什么这是安全的？因为 `defer_unchecked` 保证闭包在「所有当前 pin 的线程都已 unpin」之后才执行。而任何持有该对象旧指针的线程，必然是在某个 epoch 内 pin 着、并基于那次 load 的结果在访问。等它们全部 unpin，就再无人能解引用这个对象了，此时 `into_owned()` + `drop` 自然安全。

#### 4.2.3 源码精读

`defer_destroy` 的实现只有一行：[src/guard.rs:271-273](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L271-L273)。

```rust
pub unsafe fn defer_destroy<T: ?Sized + Pointable>(&self, ptr: Shared<'_, T>) {
    unsafe { self.defer_unchecked(move || ptr.into_owned()) }
}
```

要点：

- 入参 `ptr: Shared<'_, T>` 的生命周期 `'_` 绑定到当前 guard。`move` 把 `ptr`（一个 `Copy` 的裸指针包装）按值捕获进闭包，之后闭包就独立持有它，不再依赖原 guard。
- `T: ?Sized + Pointable`：支持动态大小类型（包括 `[MaybeUninit<T>]` 数组，见 u2-l4），析构走的是 `Pointable::drop`。
- 调用方负责保证「对象已不可达」——这就是这个函数 `unsafe` 的全部理由。

`Shared::into_owned` 的语义见 [u2-l7](u2-l7-shared-pointer-and-lifetime.md)：它把无所有权的 `Shared` 升级为独占的 `Owned`，drop 时真正释放。

#### 4.2.4 代码实践

**目标**：用一个带 Drop 计数的类型，肉眼观察 `defer_destroy` 在宽限期过后真正触发了析构。

操作步骤（**示例代码**）：

```rust
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
use std::sync::Arc;
use crossbeam_epoch::{self as epoch, Atomic, Owned};

struct DropCounter(Arc<AtomicUsize>);
impl Drop for DropCounter {
    fn drop(&mut self) { self.0.fetch_add(1, SeqCst); }
}

fn main() {
    let dropped = Arc::new(AtomicUsize::new(0));

    // 旧对象：会在被 swap 替换后 defer_destroy
    let a = Atomic::new(DropCounter(dropped.clone()));
    let guard = &epoch::pin();

    // 用一个新对象换掉旧对象，旧对象从此不可达
    let old = a.swap(Owned::new(DropCounter(dropped.clone())), SeqCst, guard);
    unsafe { guard.defer_destroy(old); }   // 延迟析构旧对象

    println!("after defer_destroy: {}", dropped.load(SeqCst)); // 期望 0（还没回收）

    // 反复 flush + pin，推动 epoch 前进，直到回收发生
    for _ in 0..1000 {
        drop(epoch::pin());
        guard.flush();
        if dropped.load(SeqCst) >= 1 { break; }
    }
    println!("after flush loop: {}", dropped.load(SeqCst)); // 期望 >=1
}
```

需要观察的现象 / 预期结果：

- 第一条打印通常是 `0`：defer 只是登记，闭包尚未执行。
- 循环若干次后计数变为 `1`，说明旧对象的 `Drop` 在宽限期过后被触发。
- 别忘了回收 `a` 里最后那个对象，否则会泄漏（演示里可忽略，进程退出即归还）。
- 不同机器/不同调度下触发所需迭代数不同，**待本地验证**具体轮数。

#### 4.2.5 小练习与答案

**练习 1**：`defer_destroy(ptr)` 之后，能不能继续在当前 guard 下使用 `ptr`（例如 `ptr.deref()`）？

**参考答案**：技术上，只要 guard 还活着、且析构闭包还没真正执行（宽限期未过），`ptr` 指向的内存仍有效，解引用不会立刻崩。但这是**非常糟糕**的用法——你刚声明「此对象已不可达、请择日析构」，转头又去读它，逻辑自相矛盾，且在别的线程推进回收时随时可能失效。正确做法：`defer_destroy` 之后不要再碰 `ptr`。

**练习 2**：为什么 `defer_destroy` 不直接写成 `self.defer(move || drop(ptr.into_owned()))`（用安全的 `defer`）？

**参考答案**：因为 `Shared` 是 `!Send`（带生命周期与裸指针），不满足 `defer` 的 `F: Send + 'static` 约束，编译过不了。这正是库提供 `defer_unchecked` 与 `defer_destroy` 的根本原因——把「宽限期后跨线程析构是安全的」这一推理交给调用方。

---

### 4.3 本地 bag 与全局 queue：defer 的存储与回收路径（含 flush）

#### 4.3.1 概念说明

到这里我们知道闭包被「登记」了，但它到底存哪、什么时候才真正执行？答案是两层缓存：

- **本地 bag**（`Local::bag`）：每个参与者（线程）一个，存放近期 defer 的闭包。用本地缓存是为了**摊薄**「把垃圾推到全局队列」的同步开销。
- **全局 queue**：所有参与者共享的 `SealedBag` 队列。每个 bag 入队时会被**盖戳**（seal）当前全局 epoch，作为日后判断「是否安全回收」的依据。

`flush()` 则是「我等不及了，请尽快回收」的按钮：它把当前本地 bag 推入全局 queue，并主动触发一次 `collect()`。

#### 4.3.2 核心流程

一条延迟闭包从登记到执行的完整旅程：

```text
Guard::defer_unchecked(f)
   └─> Local::defer(Deferred, guard)
         └─> bag.try_push(deferred)?
               ├─ 成功：留在本地 bag，结束（暂不执行）
               └─ 失败(bag 满)：Global::push_bag(bag, guard)  # 满袋入队
                     └─> 重试 try_push，直到塞进新的空 bag
```

注意：`Local::defer` **只在 bag 满时**把满袋推入全局 queue，**它本身不会 collect**。真正让全局 queue 里的过期垃圾被执行的，只有两条路径：

1. `Guard::flush()` → `Local::flush` → `push_bag`（如有）+ `Global::collect`。
2. `Local::pin` 每隔 `PINNINGS_BETWEEN_COLLECT = 128` 次 pin 触发一次 `collect`（见 u3-l9 与 u5 单元）。

`collect` 的回收判据是宽限期。一个在全局 epoch `g` 入队的 bag，只有当：

\[
\text{expired}(g_{\text{now}},\ g) \;\iff\; (g_{\text{now}} \ominus g) \geq 2
\]

（其中 \(\ominus\) 为 epoch 编码上的回绕减法）成立时，才允许弹出并 drop，从而执行其中的闭包。这正对应第一讲定义的「前进满 2 次」宽限期。

#### 4.3.3 源码精读

**`Bag` 与容量**：[src/internal.rs:64-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L64-L76) 定义 `MAX_OBJECTS = 64`（sanitize/miri 下为 4，更容易暴露竞争），`Bag` 就是一个 `Deferred` 定长数组加 `len`。

**`Local::defer`**：[src/internal.rs:382-389](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L382-L389)。

```rust
pub(crate) unsafe fn defer(&self, mut deferred: Deferred, guard: &Guard) {
    let bag = self.bag.with_mut(|b| unsafe { &mut *b });
    while let Err(d) = unsafe { bag.try_push(deferred) } {
        self.global().push_bag(bag, guard);   // 满了才入队
        deferred = d;
    }
}
```

`try_push` 满了返回 `Err(deferred)`（原物奉还，见 [src/internal.rs:100-108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L100-L108)），于是 `push_bag` 把满袋冲走、换一个空袋，再塞。

**`Global::push_bag`**：[src/internal.rs:191-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L191-L198)。

```rust
pub(crate) fn push_bag(&self, bag: &mut Bag, guard: &Guard) {
    let bag = mem::replace(bag, Bag::new());  // 换新空袋
    atomic::fence(Ordering::SeqCst);          // 关键屏障：先让本线程之前的写对所有线程可见
    let epoch = self.epoch.load(Ordering::Relaxed);
    self.queue.push(bag.seal(epoch), guard);  // 盖当前 epoch 戳入队
}
```

这里的 `SeqCst fence` 很关键：它保证「本线程在 defer 前对这些对象的所有写入」在 bag 公开到全局 queue 之前已对其他线程可见——否则别的线程在回收时可能读到未初始化完成的对象。

**`Guard::flush` → `Local::flush`**：[src/guard.rs:295-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L295-L299) 与 [src/internal.rs:391-399](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L391-L399)。

```rust
pub fn flush(&self) {
    if let Some(local) = unsafe { self.local.as_ref() } {
        local.flush(self);
    }
}
// Local::flush：
pub(crate) fn flush(&self, guard: &Guard) {
    let bag = self.bag.with_mut(|b| unsafe { &mut *b });
    if !bag.is_empty() { self.global().push_bag(bag, guard); }
    self.global().collect(guard);   // 主动回收
}
```

**`Global::collect`**：[src/internal.rs:207-226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L207-L226)。它先 `try_advance` 尝试推进全局 epoch，然后最多弹出 `COLLECT_STEPS = 8` 个已过期 bag（sanitize 下无上限）逐个 `drop`——而 `Bag::drop`（[src/internal.rs:125-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L125-L134)）正是真正调用每个 `Deferred::call()` 的地方。

**过期判据**：[src/internal.rs:155-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L155-L162)，`is_expired` 用 `global_epoch.wrapping_sub(self.epoch) >= 2`，即上面的宽限期公式。

#### 4.3.4 代码实践

**目标**：把 4.2 的实践改成「对比有 `flush` 与无 `flush`」时回收的快慢。

操作步骤：

1. 复用 4.2 的 `DropCounter` 示例。
2. 在 `defer_destroy(old)` 之后，**不**调用 `flush`，而是只做 `for _ in 0..N { drop(epoch::pin()); }`，统计 `dropped` 变为 1 大约需要多大的 `N`。
3. 再改成每次循环里 `drop(epoch::pin()); guard.flush();`，对比所需 `N`。

需要观察的现象 / 预期结果：

- 不 flush 时，回收只能靠 `pin` 每 128 次触发的周期性 `collect`，所需 pin 次数明显更多。
- 加了 flush 后，每次循环都推进 epoch 并 collect，回收发生得快得多。
- 这说明：`flush` 不改变「最终一定回收」的保证，只影响「多快回收」。**待本地验证**两组 N 的具体量级。

#### 4.3.5 小练习与答案

**练习 1**：假设一个线程只调用 `defer`、从不 `flush`，也不再有新的 `pin()`，它登记的闭包会执行吗？

**参考答案**：在本线程内不会主动执行——`Local::defer` 只在 bag 满时入队，且不 collect。若完全没有后续 pin/flush，这条闭包可能长期停留在本地 bag 或全局 queue 里，**理论上甚至可能一直不执行**（文档明示 *"in theory, `f` might never run"*）。要尽快执行，需要别的线程（或本线程）继续 pin/flush 推动 epoch。最终若进程退出或 `Collector` 被销毁，残留 bag 的 `Drop` 会兜底执行。

**练习 2**：`Global::push_bag` 里那行 `atomic::fence(Ordering::SeqCst)` 删掉会怎样？

**参考答案**：在弱内存模型下，本线程对延迟对象的写入可能与「bag 已公开到 queue」重排，导致回收线程读到未完成写入的对象、进而 UB。这个 fence 是「先完成所有写、再公开 bag」顺序的保障，不能删。（屏障与 pin 的更深机制留待 u5-l18。）

---

### 4.4 unprotected guard：defer 立即执行的特殊路径

#### 4.4.1 概念说明

回顾 [u3-l9](u3-l9-guard-and-pin.md)：`epoch::unprotected()` 返回一个 `local` 为 null 的 `&'static Guard`——假守卫。它不真正 pin，专门用于**单线程、无并发**的构造/析构场景。

在假守卫上调用 `defer` 系列时，行为是特殊的：**闭包立即在当前线程执行**，没有任何延迟。这在析构整个数据结构时非常有用——此时已无其他线程并发访问，与其走完整的 epoch 登记流程，不如直接析构。

#### 4.4.2 核心流程

回到 4.1.3 里 `defer_unchecked` 的 `else` 分支：

```text
fn defer_unchecked(self, f):
    if self.local 非空: local.defer(...)      # 正常延迟
    else:              drop(f())              # unprotected: 立即执行
```

`flush` 在假守卫上则是 **no-op**：[src/guard.rs:295-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L295-L299) 里 `self.local.as_ref()` 为 `None`，直接返回。`collector()` 也返回 `None`。

#### 4.4.3 源码精读

`unprotected()` 的构造见 [src/guard.rs:517-528](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L517-L528)：用一个 `GuardWrapper` newtype 加 `unsafe impl Sync` 存进 `static`，其 `local` 为 `core::ptr::null()`。

`defer_unchecked` 的 `else` 分支 `drop(f())` 即立即执行：[src/guard.rs:196-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L196-L198)。

文档里给出的典型用法是 Treiber 栈的 `Drop`：[src/guard.rs:493-512](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L493-L512)——遍历链表，用 `unprotected()` 做 load 与 `into_owned()` 直接析构，而不是去 pin。

#### 4.4.4 代码实践

**目标**：验证假守卫上 `defer` 立即执行、`flush` 为 no-op。

操作步骤（**示例代码**）：

```rust
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
use crossbeam_epoch as epoch;

static CALLED: AtomicUsize = AtomicUsize::new(0);

fn main() {
    unsafe {
        let g = epoch::unprotected();
        g.defer(|| { CALLED.fetch_add(1, Relaxed); });   // 期望立即执行
        println!("before flush: {}", CALLED.load(Relaxed)); // 期望 1
        g.flush();                                        // no-op
        println!("after flush: {}", CALLED.load(Relaxed));  // 期望仍 1
        assert!(g.collector().is_none());                 // 假守卫无 collector
    }
}
```

需要观察的现象 / 预期结果：两次打印都是 `1`，证明 defer 当场执行、flush 没有再做任何事。**待本地验证**。

> ⚠️ 注意：在 `unprotected()` 上 load/解引用 `Atomic` 是 `unsafe` 的，前提是「该 Atomic 当前没有其他线程并发修改」。析构时这一前提成立，所以安全。

#### 4.4.5 小练习与答案

**练习 1**：既然 `unprotected()` 上 `defer` 立即执行，那它和直接写 `f()` 有何区别？

**参考答案**：行为上等价（都是立刻在当前线程跑 `f`），但写 `guard.defer(f)` 能让你**用同一套代码**兼顾「并发路径用真 guard 延迟回收」与「单线程析构路径用假 guard 直接回收」。例如数据结构的方法接收 `&Guard`，调用方决定传真 guard 还是 `unprotected()`，方法体不用改。这就是它在析构里好用的原因。

**练习 2**：在假守卫上调用 `defer_destroy(ptr)` 安全吗？需要满足什么条件？

**参考答案**：`defer_destroy` 内部转 `defer_unchecked`，假守卫下会立即 `ptr.into_owned()` 并 drop。这要求此刻**没有其他线程持有或访问 `ptr`**——典型场景就是数据结构正在被整体析构、已无并发访问。条件满足时安全；否则会 use-after-free。

## 5. 综合实践：简化版 Treiber 栈

把本讲的 `defer_destroy`、`defer_unchecked`（move 闭包）和 `flush` 串起来，实现一个经典的 Treiber 栈。这是无锁数据结构里最经典的「CAS + 延迟回收」范本。

**目标**：

- `push`/`pop` 用 CAS 维护无锁链表。
- `pop` 把被摘除的旧 head 用 `defer_destroy` 延迟释放。
- 用 `defer_unchecked` 做一个「延迟打印」的旁路，强调闭包必须 `move`。
- 观察节点 Drop 在宽限期过后被触发。

**参考实现**（**示例代码**，可放入 `examples/treiber.rs` 自行运行）：

```rust
use std::mem::ManuallyDrop;
use std::ptr;
use std::sync::atomic::Ordering::SeqCst;
use crossbeam_epoch::{self as epoch, Atomic, Owned};

struct Node<T> {
    // 用 ManuallyDrop：data 会被 pop 取走，节点析构时不能再 drop 一次
    data: ManuallyDrop<T>,
    next: Atomic<Node<T>>,
}

pub struct TreiberStack<T> {
    head: Atomic<Node<T>>,
}

impl<T> TreiberStack<T> {
    pub fn new() -> Self {
        Self { head: Atomic::null() }
    }

    pub fn push(&self, v: T) {
        let guard = &epoch::pin();
        let mut n = Owned::new(Node {
            data: ManuallyDrop::new(v),
            next: Atomic::null(),
        });
        loop {
            let head = self.head.load(SeqCst, guard);
            n.next.store(head, SeqCst);                       // 链接当前 head
            match self.head.compare_exchange(head, n, SeqCst, SeqCst, guard) {
                Ok(_) => return,                               // 挂载成功
                Err(e) => n = e.new,                           // CAS 失败，回收 n 重试
            }
        }
    }

    pub fn pop(&self) -> Option<T> {
        let guard = &epoch::pin();
        loop {
            let head = self.head.load(SeqCst, guard);
            match unsafe { head.as_ref() } {                   // 安全判空
                Some(h) => {
                    let next = h.next.load(SeqCst, guard);
                    if self.head.compare_exchange(head, next, SeqCst, SeqCst, guard).is_ok() {
                        // head 已被原子摘除，从此不可达
                        // 先把 data 取走（不 drop，因为 defer_destroy 之后节点会被整体析构）
                        let data = unsafe { ptr::read(ManuallyDrop::as_ptr(&h.data)) };
                        // 延迟析构节点本身：等所有还看着旧 head 的线程 unpin
                        unsafe { guard.defer_destroy(head); }
                        return Some(data);
                    }
                }
                None => return None,
            }
        }
    }
}

fn main() {
    let s = TreiberStack::new();
    for i in 0..1000 { s.push(i); }

    // 旁路演示：defer_unchecked 做延迟打印，闭包必须 move
    let guard = &epoch::pin();
    let bye = String::from("stack is being drained");
    unsafe { guard.defer_unchecked(move || println!("{}", bye)); }

    while s.pop().is_some() {}
    guard.flush();   // 推动 epoch，尽快回收被摘除的节点与上面的打印闭包

    // 反复 pin+flush 一会儿，让宽限期过去、延迟闭包执行
    for _ in 0..1000 {
        drop(epoch::pin());
        guard.flush();
    }
}
```

需要观察的现象 / 预期结果：

- 程序不崩溃、不 double-free：`ManuallyDrop` + `ptr::read` 取走 data，`defer_destroy` 只析构节点壳与释放内存，二者职责分离。
- `bye` 的打印在某次 flush 后出现（时机不定）。
- 多线程压测（把 `push`/`pop` 放到多个线程里并发）能稳定跑过——这正是 `defer_destroy` 的价值：在无锁并发下安全回收。

若想严格验证无泄漏、无 double-free，可给 `Node` 加 `Drop` 自增一个全局计数器，在程序末尾断言「push 次数 == pop 取出的节点数 == Drop 次数」。这一步**待本地验证**。

## 6. 本讲小结

- `defer`（安全，要求 `F: Send + 'static`）与 `defer_unchecked`（unsafe，放宽 `Send`）都是延迟执行闭包；后者存在的意义是让 `!Send` 的 `Shared` 也能被延迟析构。
- `defer_destroy(ptr)` 是最常用 API，内部即 `defer_unchecked(move || ptr.into_owned())`；unsafe 责任是「对象已不可达」。
- 所有 `defer*` 的闭包**必须 `move`**，且实际需能跨线程执行（闭包可能由别的线程在回收时调用）。
- 延迟闭包先入**本地 bag**，bag 满才入**全局 queue**（盖当前 epoch 戳）；`Local::defer` 本身不 collect。
- 真正触发回收的是 `flush()`（push_bag + collect）与 `pin()` 每 128 次的周期性 collect；过期判据是 `global_epoch - sealed_epoch >= 2`（宽限期）。
- `unprotected()` 假守卫下 `defer` 立即执行、`flush` 为 no-op，专用于无并发的构造/析构。

## 7. 下一步学习建议

- 想了解 `Deferred` 如何把任意 `FnOnce` 塞进固定大小的 `Bag` 槽位（内联 vs 装箱），继续 [u3-l11 Deferred：内联或装箱的 FnOnce](u3-l11-deferred-fnonce-storage.md)。
- 想搞懂 `flush`/`collect` 背后的 epoch 推进、内存屏障与 x86 hack，进入 u5 单元：先读 [u5-l17 Epoch 表示](u5-l17-epoch-representation.md)，再读 [u5-l18 pin/unpin 与内存屏障](u5-l18-pin-unpin-memory-barriers.md)、[u5-l19 try_advance 与 collect](u5-l19-try-advance-and-collect.md)。
- 想看 `defer_destroy` 在真实无锁数据结构里的运用，直接读 [src/sync/queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs) 的 Michael-Scott 队列（对应 u6-l21）。
