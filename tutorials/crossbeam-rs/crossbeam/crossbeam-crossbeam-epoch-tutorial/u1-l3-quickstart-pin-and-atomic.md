# 快速上手：pin 与 Atomic 的最小例子

> 本讲属于「入门：认识 crossbeam-epoch」单元（u1）。在动手之前，请先确认你读过 [u1-l1 项目定位与 epoch-based 内存回收思想](./u1-l1-project-overview.md) 与 [u1-l2 项目结构、构建配置与特性开关](./u1-l2-project-structure-and-build.md)。

## 1. 本讲目标

学完本讲后，你应该能够：

1. 调用默认收集器的 `epoch::pin()` 拿到一个 `Guard`，并解释这个 `Guard` 为什么是「线程已 pin」的凭证。
2. 用 `Atomic::new` 在堆上分配一个对象，用 `Atomic::load` / `Atomic::swap` 读写它，并说明返回值 `Shared<'g, T>` 与传入的 `Guard` 之间的生命周期绑定。
3. 用 `Guard::defer_destroy` 把「旧对象」延迟到宽限期之后再释放，并能指出它的 `unsafe` 边界。
4. 把上面三步串成一个完整的最小闭环：分配 → 读取 → 替换 → 延迟回收，并用一个带 `Drop` 计数的类型验证回收确实发生了。

本讲只讲「怎么用」，几乎不涉及 epoch 内部如何推进——那是第 5 单元的事。

## 2. 前置知识

承接前两讲，你已经知道：

- **EBR（基于纪元的内存回收）** 的核心难题是：无锁数据结构里「逻辑上移除一个对象」和「物理上释放它的内存」必须分开，因为别的线程可能还握着指向它的旧指针。
- 解决办法是引入一个单调（回绕）递增的**全局 epoch**，以及一个 **grace period（宽限期）**：在某个垃圾被打上「当前 epoch」的盖戳之后，全局 epoch 只要再前进满两次，就可以安全回收它。形式化地说，若垃圾盖戳为 \(g\)、当前全局 epoch 为 \(G\)，则当
  \[ G - g \geq 2 \quad (\text{模回绕语义下}) \]
  时该垃圾可回收。这里的「减 2」对应每轮推进 `successor` 都让 epoch 加 2（最低位被复用为 pinned 标志，细节留到 [u5-l17](./u5-l17-epoch-representation.md)）。
- 线程在访问共享对象前要先 **pin**（钉住自己并快照当前 epoch、挂起新垃圾回收），用完再 **unpin**。
- crate 是 `#![no_std]` 的；最方便的 `epoch::pin()` 只在开启 `std` 特性时提供，因为它依赖线程局部存储（见 [u1-l2](./u1-l2-project-structure-and-build.md)）。

本讲用到三个核心类型，先给一句话直觉（细节在第 4 节展开）：

| 类型 | 直觉 | 类比 |
|------|------|------|
| `Atomic<T>` | 一个**共享**的原子指针，指向堆上对象，多线程可同时操作 | `Arc` + 原子 |
| `Owned<T>` | 一个**独占**的堆指针，拥有对象所有权 | `Box<T>` |
| `Shared<'g, T>` | 从 `Atomic` 里 `load` 出来的**借来的**指针，只能在 `Guard` 的生命周期 `'g` 内用 | `&'g T` 但带 tag |

`Guard` 则是 pin 的凭证：拿到它你才能合法地 `load` 一个 `Atomic`、才能安全解引用 `Shared`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/default.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs) | 「默认收集器」：一个进程级单例 `Collector` + 每线程一个 `LocalHandle`，对外暴露 `pin()` / `is_pinned()` / `default_collector()`。 |
| [src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard` 类型本体：`defer` / `defer_unchecked` / `defer_destroy` / `flush` / `repin`，以及 `Drop` 时的 unpin。 |
| [src/atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs) | `Atomic` / `Owned` / `Shared` 三类指针与 `load` / `swap` / `compare_exchange` 等操作。 |
| [src/collector.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs) | `Collector` 与 `LocalHandle` 的公开 API；本讲主要参考其中的测试 `count_drops` 来设计验证实验。 |
| [examples/sanitize.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs) | 一个 16 线程压测示例，展示了 `pin` + `swap` + `defer_destroy` + `flush` 的真实用法。 |

> 说明：第 4 节里的「最小模块」只覆盖 `pin()`、`load/swap`、`defer_destroy` 三块；`Guard` 的 `repin`、`unprotected`、`Collector` 自建等内容留给 u3、u4 单元。

## 4. 核心概念与源码讲解

### 4.1 默认收集器与 `pin()`：拿到你的 Guard

#### 4.1.1 概念说明

绝大多数用户不需要自己建收集器。crossbeam-epoch 提供了一个**进程级单例**的「默认收集器」，并给每个线程懒初始化一个参与者句柄 `LocalHandle`。你只要调用一个函数：

```rust
let guard = &epoch::pin();
```

就完成了两件事：

1. 把当前线程**注册**为这个默认收集器的参与者（首次调用时才真正注册）。
2. 把当前线程 **pin** 住，返回一个 `Guard` 作为凭证。`Guard` 被 drop 时自动 **unpin**。

为什么返回值经常写成 `&epoch::pin()`？因为 `pin()` 返回的是 owned 的 `Guard`，临时借用一下能让「把 guard 传给 `load`」写起来更顺手（这只是风格，不是必须）。

#### 4.1.2 核心流程

`epoch::pin()` 的调用链很短：

```
epoch::pin()                         // 用户调用
  └─ with_handle(|h| h.pin())        // 取出线程局部的 LocalHandle
       └─ LocalHandle::pin()         // collector.rs
            └─ Local::pin()          // internal.rs：真正写 epoch + 屏障
```

关键点在最后一跳 `Local::pin`：它在「第一个活跃 guard」时把当前全局 epoch 快照并标记为 pinned，再插一道内存屏障；之后每隔 `PINNINGS_BETWEEN_COLLECT`（值为 128）次 pin 会顺带 `collect` 一次垃圾。这道屏障是 EBR 安全性的核心，本讲只需知道「它存在」，细节留到 [u5-l18](./u5-l18-pin-unpin-memory-barriers.md)。

#### 4.1.3 源码精读

默认收集器用一个 `OnceLock` 懒初始化，保证整个进程只有一个：

[src/default.rs:16-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L16-L33) —— `collector()` 用 `OnceLock::get_or_init(Collector::new)` 拿到全局单例；在 loom 模型测试分支下改用 `loom::lazy_static!`。

每个线程通过 `thread_local!` 持有自己的参与者句柄，首次访问时自动注册：

[src/default.rs:35-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L35-L38) —— `HANDLE: LocalHandle = collector().register()`，注册发生在句柄第一次被用到时。

对外暴露的三个函数都委托给 `with_handle`：

[src/default.rs:41-55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L41-L55) —— `pin()` / `is_pinned()` / `default_collector()`。

`with_handle` 有一个值得注意的兜底逻辑——它解释了为什么「即使在线程退出过程中句柄已析构，`pin()` 也不会 panic」：

[src/default.rs:57-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L57-L65) —— 先尝试用线程局部 `HANDLE`；若 `HANDLE` 已被析构（`try_with` 失败），就临时 `collector().register()` 再注册一个句柄来用。这正是 [src/default.rs:77-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L77-L100) 测试 `pin_while_exiting` 想要保证的不变量。

最后看 `Guard` 的结构和它的 `Drop`：

[src/guard.rs:70-72](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L70-L72) —— `Guard` 内部只有一个裸指针 `local: *const Local`，指向当前线程的参与者。

[src/guard.rs:416-423](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L416-L423) —— `Drop for Guard` 调用 `local.unpin()`。也就是说，guard 离开作用域 = 线程 unpin，这是 RAII 风格。

> 补充：pin 是**可重入**的。同一个线程连续多次 `epoch::pin()` 不会重复插屏障——只有「第一个」guard 真正 pin，只有「最后一个」guard drop 时才真正 unpin（见 [src/guard.rs:51-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L51-L67) 的文档示例与 [u3-l9](./u3-l9-guard-and-pin.md)）。

#### 4.1.4 代码实践

**实践目标**：用肉眼确认「guard 存在时线程处于 pinned，drop 后 unpinned」。

**操作步骤**（示例代码，非项目原有）：

```rust
// 示例代码：验证 pin / is_pinned 的对应关系
use crossbeam_epoch as epoch;

fn main() {
    println!("before pin: is_pinned = {}", epoch::is_pinned());
    {
        let _g1 = epoch::pin();
        println!("after  pin: is_pinned = {}", epoch::is_pinned());
        {
            let _g2 = epoch::pin(); // 可重入，不会改变 pinned 状态的「真伪」
            println!("two guards: is_pinned = {}", epoch::is_pinned());
        }
        println!("one  dropped: is_pinned = {}", epoch::is_pinned());
    }
    println!("all  dropped: is_pinned = {}", epoch::is_pinned());
}
```

**需要观察的现象**：依次输出 `false` → `true` → `true` → `true` → `false`。

**预期结果**：与 [src/guard.rs:51-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L51-L67) 文档示例的断言完全一致。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `let _g1 = epoch::pin();` 改成 `let _ = epoch::pin();`（用 `_` 而不是 `_g1`），`is_pinned()` 还会是 `true` 吗？

**答案**：不会。`let _ = ...;` 会**立即**把右值 drop 掉，guard 一创建就析构，线程马上 unpinned。必须用 `let _g1 =`（或带 `&`）把 guard 绑定到一个有名字的、有作用域的变量上，它才能活到代码块结束。这是 Rust 初学者常踩的坑。

**练习 2**：`epoch::pin()` 为什么必须在线程退出时仍然安全？提示：联想「某个对象的 `Drop` 里又调用了 `pin()`」的场景。

**答案**：见 [src/default.rs:57-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L57-L65) 与 `pin_while_exiting` 测试：线程退出时线程局部 `HANDLE` 可能已经析构，`with_handle` 会兜底重新 `register()` 一个临时句柄，从而避免 panic。

---

### 4.2 Atomic 指针的 `load` / `swap`：读写受保护对象

#### 4.2.1 概念说明

`Atomic<T>` 是一个可以被多线程共享的原子指针，内部就是一个 `AtomicPtr<()>`（外加利用对齐低位存 tag，本讲先忽略 tag）：

```rust
pub struct Atomic<T: ?Sized + Pointable> {
    data: AtomicPtr<()>,
    _marker: PhantomData<*mut T>,
}
```

它有三个常用入口：

- `Atomic::new(value)`：在堆上分配 `value`，返回指向它的 `Atomic`（类似 `Box` + 原子）。
- `a.load(order, &guard)`：原子地**读出**当前指针，返回 `Shared<'g, T>`。注意它**必须**传一个 `&Guard`——这正是「先 pin 再读」的强制要求。
- `a.swap(new, order, &guard)`：原子地**替换**为新指针（可以是 `Owned` 或 `Shared`），返回**旧的** `Shared<'g, T>`。

返回的 `Shared<'g, T>` 带一个生命周期 `'g`，它和传入的 `guard` 绑定：`Shared` 只在 guard 还活着时才允许解引用。编译器会帮你守住这条线（`load` 的签名把 `'g` 关联到了 `&'g Guard`）。

#### 4.2.2 核心流程

一次典型的「读—改—回收」：

```
Atomic::new(v)            // 分配堆对象，得到 Atomic
  │
guard = &epoch::pin()     // pin
  │
p = a.load(ord, guard)    // 读出 Shared<'g>，绑定到 guard 的 'g
  │
old = a.swap(new, ord, guard)  // 原子替换，拿到旧值 Shared<'g>
  │
// 至此 p / old 都只能在 guard 活着时用
```

要解引用 `Shared`，需要 `unsafe`：`p.deref()` 或 `p.as_ref()`。文档特别强调：如果读写两侧只用 `Relaxed`，会构成**数据竞争**（写线程分配并初始化对象与读线程读对象之间没有同步），正确做法是用 `Release` 存 / `Acquire` 读，或更保守的 `SeqCst`。

#### 4.2.3 源码精读

`Atomic<T>` 的结构与构造：

[src/atomic.rs:274-277](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L274-L277) —— `Atomic { data: AtomicPtr<()>, _marker: PhantomData<*mut T> }`。

[src/atomic.rs:293-295](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L293-L295) —— `Atomic::new(init)` 委托给 `Self::init`，后者用 `Owned::init` 分配堆对象。

`load` 把内部 `AtomicPtr` 的值包成 `Shared`，并把生命周期 `'g` 与传入的 guard 绑定：

[src/atomic.rs:356-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L356-L358) —— `pub fn load<'g>(&self, order: Ordering, _: &'g Guard) -> Shared<'g, T>`。注意那个 `_: &'g Guard` 参数本身在函数体里没被用到（名字是 `_`），它的唯一作用是**把返回值的生命周期 `'g` 和 guard 绑死**，让编译器替你防止「guard drop 之后还用 `Shared`」。

`swap` 是原子交换，返回旧值：

[src/atomic.rs:424-426](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L424-L426) —— `pub fn swap<'g, P: Pointer<T>>(&self, new: P, order: Ordering, _: &'g Guard) -> Shared<'g, T>`，内部 `self.data.swap(new.into_ptr(), order)`。`new` 既可以是 `Owned` 也可以是 `Shared`（都实现了 sealed trait `Pointer`）。

解引用要 `unsafe`，且文档明示 `Relaxed` 会引入数据竞争：

[src/atomic.rs:1330-1332](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1330-L1332) —— `Shared::deref(&self) -> &'g T`。

[src/atomic.rs:1407-1414](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1407-L1414) —— `Shared::as_ref(&self) -> Option<&'g T>`，对 null 指针返回 `None`，是更安全的判空解引用入口。

`examples/sanitize.rs` 给了一个真实的多线程用法，单次循环体里同时用到了 `swap`、`load`、`defer_destroy`、`flush`：

[src/examples/sanitize.rs:27-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/examples/sanitize.rs#L27-L43) —— 每次循环先 `handle.pin()` 拿 guard、`guard.flush()`；然后随机地要么 `a.swap(...)` 替换并 `defer_destroy(p)` 旧值、要么 `a.load(...)` 读出并 `fetch_add`。这是一个非常好的「完整闭环」参考。

#### 4.2.4 代码实践

**实践目标**：单线程下完成「分配 → pin → load → 解引用 → swap → 拿到旧值」的链路，并确认读到的值正确。

**操作步骤**（示例代码，改自 [src/guard.rs:30-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L30-L49) 的文档示例）：

```rust
// 示例代码
use crossbeam_epoch::{self as epoch, Atomic, Owned};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    // 1. 在堆上分配一个数字
    let a = Atomic::new(777);

    // 2. pin 当前线程
    let guard = &epoch::pin();

    // 3. load 出 Shared 并解引用
    let p = a.load(SeqCst, guard);
    unsafe {
        assert_eq!(p.as_ref(), Some(&777));
    }

    // 4. swap 替换为新值，拿回旧值 old
    let old = a.swap(Owned::new(999), SeqCst, guard);
    unsafe {
        assert_eq!(old.as_ref(), Some(&777)); // 旧值仍是 777
        assert_eq!(a.load(SeqCst, guard).as_ref(), Some(&999)); // 新值已生效
    }

    // 5. 重要：Atomic 没有 Drop，当前还存放在 a 里的对象（999）需要手动回收，否则泄漏。
    //    这里直接把所有权取回来 drop 掉（单线程、无人持有引用，故安全）。
    unsafe { drop(a.into_owned()); }
}
```

**需要观察的现象**：三个 `assert_eq!` 全部通过，程序正常结束、无泄漏告警。

**预期结果**：与 `atomic.rs` 中 `load` / `swap` 的文档示例行为一致。运行结果待本地验证（可用 `cargo run` 配合 `RUSTFLAGS="--cfg crossbeam_sanitize_thread"` 或 miri 进一步检查，但本例单线程足够）。

> 注意第 5 步：`Atomic` 故意**没有**实现 `Drop`（否则会和 EBR 的延迟回收语义冲突）。因此 `a` 里当前指向的对象如果不再被任何线程引用，你要么 `defer_destroy` 它，要么 `a.into_owned()` 取回所有权后 `drop`。本讲后面 4.3 节展示前一种（延迟回收）做法。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load` 的签名里那个 `&'g Guard` 参数名字是 `_`？删掉它行不行？

**答案**：它在函数体里确实没被用到，但它的**生命周期 `'g`** 出现在返回类型 `Shared<'g, T>` 中，从而把「`Shared` 可用的时长」和「guard 存活的时长」绑死。删掉它，编译器就无法阻止你「drop guard 之后还继续用 `Shared`」，安全保证就破了。所以它看似多余，实则是生命周期的锚点。

**练习 2**：把上面的 `SeqCst` 全部换成 `Relaxed`，单线程下结果会变吗？多线程下呢？

**答案**：单线程下结果通常不变（没有其他线程参与重排）。但在「线程 A `store` 新对象、线程 B `load` 并解引用」的多线程场景里，`Relaxed` 不提供「分配+初始化」与「读取」之间的同步，会构成**数据竞争**（读到未初始化内存），必须用 `Release` 存 / `Acquire` 读（或 `SeqCst`）。这正是 [src/atomic.rs:1304-1314](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1304-L1314) 文档反复强调的点。

---

### 4.3 `defer_destroy`：延迟释放旧对象

#### 4.3.1 概念说明

当 `swap`（或 `compare_exchange`）把一个对象从数据结构里「踢出来」后，这个旧对象**不能立刻 drop**——别的线程可能正 pin 在旧 epoch、还握着指向它的 `Shared`。正确做法是把它**延迟**到「所有当前 pinned 的线程都 unpin 之后」再释放。`Guard::defer_destroy` 就是干这个的：

```rust
pub unsafe fn defer_destroy<T: ?Sized + Pointable>(&self, ptr: Shared<'_, T>) {
    unsafe { self.defer_unchecked(move || ptr.into_owned()) }
}
```

它把「把 `Shared` 转成 `Owned` 再 drop」这件事注册成一个延迟闭包。等宽限期过后（全局 epoch 相对盖戳前进了至少 2），这个闭包才会被某个线程在 `collect` 时执行，对象才真正被释放。

`defer_destroy` 是 `unsafe` 的，因为它假设**调用方保证：此刻起，没有其他线程还会再访问 `ptr` 指向的对象**（即对象已经从数据结构里逻辑移除了）。如果违反，可能在还有人读的时候就把内存释放了，造成 use-after-free。

#### 4.3.2 核心流程

延迟回收的完整数据流（细节散在 u3/u4/u5，这里只给轮廓）：

```
guard.defer_destroy(ptr)
  └─ Local::defer(Deferred::new(move || ptr.into_owned()), guard)
       └─ 先塞进线程局部 bag
          ├─ bag 没满：就地缓存
          └─ bag 满了：push_bag 推入全局队列（盖上当前 epoch）
                       └─ 之后某次 collect：epoch 已前进 ≥ 2 → 弹出并执行闭包 → 对象 drop
```

`guard.flush()` 的作用是「别等 bag 满了，现在就把本地 bag 推入全局队列，并立刻尝试 `collect` 一次」，让你想尽快回收时能加速。注意：在 `unprotected()` 假守卫下调用 `defer_destroy` 会**立刻**执行闭包（因为根本没 pin，没有宽限期可言）——这在单线程析构数据结构时很有用，留到 [u3-l9](./u3-l9-guard-and-pin.md) 详谈。

#### 4.3.3 源码精读

`defer_destroy` 的实现极简，一行委托：

[src/guard.rs:271-273](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L271-L273) —— `unsafe fn defer_destroy<T: ?Sized + Pointable>(&self, ptr: Shared<'_, T>)`，内部 `self.defer_unchecked(move || ptr.into_owned())`。

它依赖的 `defer_unchecked` 决定「真延迟」还是「立刻执行」：

[src/guard.rs:189-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L189-L200) —— 若 `self.local` 非空（真 guard），走 `local.defer(...)` 进入 bag；若为空（`unprotected()` 假守卫），直接 `drop(f())` 立即执行。

`flush` 把本地袋推入全局并尝试回收：

[src/guard.rs:295-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L295-L299) —— `Guard::flush` 调用 `local.flush(self)`，其内部 `push_bag` + `collect`（见 [src/internal.rs:391-399](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L391-L399)）。

安全契约写在文档里，值得逐字读：

[src/guard.rs:218-224](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L218-L224) —— 「The object must not be reachable by other threads anymore」（对象必须已不被其他线程触达）。

要验证「回收真的发生了」，最权威的参考是 `collector.rs` 里的 `count_drops` 测试：它用一个 `Drop` 时自增全局计数器的类型，循环 `defer_destroy`，然后不断 `pin` + `collect` 直到计数达标：

[src/collector.rs:284-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L284-L315) —— `count_drops` 测试。注意它用**显式 Collector** 并直接调用 `collector.global.collect(guard)`（`global` 是 `pub(crate)`，普通用户调不到）。对默认收集器，我们靠「pin 每 128 次自动 collect」来推进——见 [src/internal.rs:454-458](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L454-L458)。

#### 4.3.4 代码实践

**实践目标**：用默认收集器完成「swap 出旧值 → `defer_destroy` → flush → 循环 pin 推进 epoch → 旧值被回收」，并用一个 `Drop` 计数类型证明回收真的发生了。

**操作步骤**（示例代码，思路对照 [src/collector.rs:284-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L284-L315) 的 `count_drops` 测试）：

```rust
// 示例代码
use crossbeam_epoch::{self as epoch, Atomic, Owned};
use std::sync::atomic::{AtomicUsize, Ordering};

// 一个 Drop 时自增全局计数器的类型，用来“观察”回收何时发生
static DROPS: AtomicUsize = AtomicUsize::new(0);

struct Tracked(i32);
impl Drop for Tracked {
    fn drop(&mut self) {
        DROPS.fetch_add(1, Ordering::Relaxed);
    }
}

fn main() {
    let a = Atomic::new(Tracked(1));

    {
        let guard = &epoch::pin();
        // swap：用新对象替换旧对象，拿到旧值 old
        let old = a.swap(Owned::new(Tracked(2)), Ordering::SeqCst, guard);
        // 关键：旧对象延迟释放（此刻起我们承诺不再访问 old）
        unsafe { guard.defer_destroy(old); }
        // 主动 flush，把本地 bag 推入全局队列
        guard.flush();
        println!("after defer_destroy+flush: DROPS = {}", DROPS.load(Ordering::Relaxed));
    } // guard drop -> unpin，线程让出 epoch 推进的阻力

    // 循环 pin 推进全局 epoch 并触发周期性 collect（每 128 次 pin 一次）
    // epoch 相对盖戳前进 ≥ 2 后，旧对象的闭包才会被执行 -> Tracked(1).drop()
    let mut tries = 0;
    while DROPS.load(Ordering::Relaxed) == 0 {
        let _g = epoch::pin();   // 创建即 pin，离开循环体 drop 即 unpin
        tries += 1;
        if tries > 100_000 {
            println!("尚未观察到回收，可能需要更长时间或更多线程参与推进");
            break;
        }
    }
    println!("observed DROPS = {} after {} pins", DROPS.load(Ordering::Relaxed), tries);

    // 收尾：当前 a 里还存放着 Tracked(2)，Atomic 没有 Drop，需要手动取回并释放。
    unsafe { drop(a.into_owned()); }
    println!("final DROPS = {}", DROPS.load(Ordering::Relaxed));
}
```

**需要观察的现象**：

1. `defer_destroy + flush` 之后，`DROPS` **仍是 0**——因为旧对象还没到宽限期，闭包尚未执行。
2. 经过若干轮 pin/unpin 之后（默认收集器约每 128 次 pin 自动 collect 一次），`DROPS` 变成 `1`——旧对象 `Tracked(1)` 被回收。
3. 最后 `a.into_owned()` 取回 `Tracked(2)` 并 drop，`DROPS` 变成 `2`。

**预期结果**：`DROPS` 从 `0` → `1`（延迟回收触发）→ `2`（收尾 drop）。具体的 `tries` 数量取决于调度，通常在几百到几千之间。运行结果待本地验证。

> 若在单线程下迟迟观察不到回收：这是正常的——epoch 推进需要线程「unpin」来让出阻力。本例的循环每次迭代都让 guard 离开作用域（unpin），所以能推进。如果想更确定地快速回收，可以改用显式 `Collector` 并在每个 guard 内调用其 `collect`（但 `global` 字段非公开，普通用户无法直接调；这是为什么 `count_drops` 测试能写、而库用户通常依赖自动 collect）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `defer_destroy` 是 `unsafe` 的，而 `defer`（要求 `F: Send + 'static`）是 safe 的？

**答案**：`defer_destroy(ptr)` 的安全性依赖一个**类型系统无法证明**的事实：「从此刻起没有其他线程还会访问 `ptr`」。这通常要求 `ptr` 已经从数据结构里被原子地移除（比如 `swap` 拿走旧值之后）。编译器无法知道你是否真的移除了它，所以交给调用方用 `unsafe` 承诺。而 `Guard::defer` 只要求闭包本身 `Send + 'static`、不触碰任何 `Shared`，其安全性可被类型系统静态证明，故是 safe 的（见 [src/guard.rs:90-98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L90-L98) 与 [src/guard.rs:189-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L189-L200) 的文档）。

**练习 2**：把示例里的 `guard.flush();` 删掉，最终 `DROPS` 还会变成 1 吗？

**答案**：最终结果一样会变成 1，只是可能更晚。`flush` 的作用是「不等本地 bag 满，现在就推入全局队列并尝试 collect」，相当于加速。不 flush 时，旧对象会先待在线程局部 bag 里，直到 bag 满（或下次 flush、或线程退出时 finalize）才入全局队列。所以「不 flush」只是延迟更久，不影响「最终会被回收」这一保证。

**练习 3**：如果在上面的 `defer_destroy(old)` 之后、guard 还活着的时候，另一个线程 `load` 到了 `old` 并解引用，会发生什么？

**答案**：这是**正确**的使用场景之一——`old` 被踢出数据结构、但只要还有线程的 `Shared` 指向它且对应 guard 活着，EBR 就**不会**回收它（因为那个线程 pin 在某个 epoch，全局 epoch 还没前进满两次）。`defer_destroy` 注册的闭包只有在宽限期过后才执行，所以并发的 `load+deref` 是安全的。这正是 EBR 的全部意义：让「逻辑移除」和「物理释放」安全地错开。

## 5. 综合实践

把本讲三块内容串起来，实现一个**单线程的、带 Drop 追踪的「替换式计数器」**，完整走一遍 EBR 的使用闭环：

**任务**：

1. 定义一个 `Tracked(u64)` 类型，`Drop` 时把自身的值累加进一个全局 `AtomicU64`（命名为 `RECLAIMED_SUM`），用于观察「哪些对象、何时被回收」。
2. 用 `Atomic::new(Tracked(10))` 建一个堆上对象。
3. 连续做 3 次 `swap`，依次换成 `Tracked(20)`、`Tracked(30)`、`Tracked(40)`；每次 swap 后对旧值 `defer_destroy` 并 `flush`。
4. 循环 `epoch::pin()` 直到 `RECLAIMED_SUM` 不再增长（或达到上限次数）。
5. 最后 `a.into_owned()` 取回当前对象并 drop。
6. 打印最终 `RECLAIMED_SUM`，验证它等于 \(10 + 20 + 30 = 60\)（被回收的三个旧值之和，当前值 40 由最后 `into_owned` 单独 drop，不进入「延迟回收」统计，或你也可把它算进另一个计数器）。

**验收要点**：

- 你能解释为什么「三个旧值并非立刻被回收，而是在循环 pin 若干次后才集中或陆续出现」。
- 你能指出代码里每一处 `unsafe` 的依据（`defer_destroy` 的「不再被其他线程触达」承诺；`into_owned` 的「无人持有引用」承诺）。
- 你能说出 `flush` 在哪一步起加速作用、删掉它对最终结果的（无）影响。

**提示**：整体骨架就是把 4.3.4 的示例从「1 次 swap」扩成「3 次 swap」并把 `DROPS` 计数换成「值求和」。结果待本地验证。

## 6. 本讲小结

- `epoch::pin()` 来自默认收集器（进程级单例 + 线程局部句柄），返回的 `Guard` 是「线程已 pin」的凭证，drop 时自动 unpin；pin 可重入。
- `Atomic<T>` 是共享原子指针；`load` / `swap` 都强制要求传 `&Guard`，返回的 `Shared<'g, T>` 的生命周期 `'g` 与 guard 绑定，由编译器保证「drop guard 后不再使用 `Shared`」。
- 解引用 `Shared` 需要 `unsafe`，且多线程下必须用 `Release`/`Acquire`（或 `SeqCst`）避免数据竞争；`Relaxed` 不安全。
- `Guard::defer_destroy(shared)` 把旧对象注册成「宽限期后再释放」的延迟闭包；它是 `unsafe` 的，契约是「对象已不再被其他线程触达」。
- `Atomic` 没有 `Drop`，当前仍存放在其中的对象要么 `defer_destroy`、要么 `into_owned()` 回收，否则泄漏。
- `flush` 能加速回收（立刻把本地 bag 推入全局并尝试 collect），但不影响「最终会被回收」的保证；用带 `Drop` 计数的类型可肉眼验证回收确实发生。

## 7. 下一步学习建议

本讲你只用了「默认收集器」和最浅的 `pin`。接下来建议：

1. **[u3-l9 Guard：pin 语义与可重入](./u3-l9-guard-and-pin.md)**：深入 `Guard` 的可重入机制、`unprotected()` 假守卫的用途（构造/析构场景），把本讲里「为什么 pin 可重入」「为什么析构时可以用 unprotected」讲透。
2. **[u3-l10 延迟执行：defer / defer_unchecked / defer_destroy](./u3-l10-defer-and-defer-destroy.md)**：把本讲一笔带过的 `defer` vs `defer_unchecked` 的安全权衡、本地 bag → 全局队列 → collect 的数据流彻底展开。
3. **[u4-l13 Collector 与 LocalHandle：自建收集器](./u4-l13-collector-and-local-handle.md)**：本讲的 `epoch::pin()` 是默认收集器的便捷封装；当你需要多个独立收集器或隔离的回收节奏时，就该直接用 `Collector` 了。
4. **[u5-l18 pin/unpin 与内存屏障](./u5-l18-pin-unpin-memory-barriers.md)**：本讲刻意回避的「pin 时那道 `SeqCst` 屏障为什么必须有」将在那里得到严格回答。

> 阅读源码时，推荐按本讲的「源码地图」顺序：先 `default.rs` 看便捷入口，再 `guard.rs` 看 Guard 的 RAII 与延迟 API，最后 `atomic.rs` 看三类指针。`examples/sanitize.rs` 是把这三者用在多线程压测里的最佳范例。
