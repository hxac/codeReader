# Guard：pin 语义与可重入

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `Guard` 到底「是什么」——它只是一个指向线程参与者 `Local` 的裸指针，是「当前线程已经 pin」的凭证，而不是 pin 这件事本身。
- 解释 `Guard::drop` 为什么能自动 unpin，以及它如何与 `Local` 里的 `guard_count` 计数配合。
- 理解 pin 的**可重入性**：在一个线程里连续创建多个 `Guard`，只会真正 pin 一次，多个 guard 共享这一次 pin。
- 掌握 `epoch::unprotected()` 这个「假守卫」的内部构造（`local` 指针为 null）以及它带来的行为差异（`defer` 立即执行、`flush` 变 no-op、`collector()` 返回 `None`），并知道它在构造/析构数据结构时的典型用途。

本讲不展开 epoch 如何推进、`SeqCst` 内存屏障的细节与垃圾回收主链路（那是 u5 单元的主题），只把目光聚焦在「`Guard` 这个类型本身」。

## 2. 前置知识

本讲建立在 u1-l3（快速上手）和 u2-l7（`Shared` 与生命周期 `'g`）之上。回顾两个关键结论：

- **默认收集器**：进程级单例 `Collector` + 每线程一个 `LocalHandle`。调用 `epoch::pin()` 会经由线程局部 `LocalHandle` 拿到一个 `Guard`。
- **`Shared<'g, T>` 的生命周期 `'g`**：`Atomic::load(&Guard)` 的返回值借用自该 `Guard`，因此 `Guard` 必须比所有从它派生的 `Shared` 活得更久。换句话说，`Guard` 是「我正在安全读取共享数据」这段**时间区间**的类型化证据。

此外需要一点 Rust 直觉：

- **RAII**：资源获取即初始化。`Guard` 的价值就在于「它一旦被 drop，就自动执行 unpin」，你不需要手动调用某个 `unpin()`。
- **裸指针 `*const T` 默认 `!Send + !Sync`**：这条规则后面会解释为什么 `Guard` 不能跨线程移动，以及为什么造一个全局的 `unprotected()` 假守卫需要绕一点弯路。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`src/guard.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard` 类型与 `unprotected()` 函数的定义 | `Guard` 结构、`Drop`、`defer_unchecked` 的 null 分支、`unprotected()` 的构造 |
| [`src/internal.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 线程参与者 `Local` 与全局 `Global` 的实现 | `Local` 字段、`pin`/`unpin`/`is_pinned`/`guard_count` 的计数逻辑 |
| [`src/default.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs) | 默认收集器与线程局部 `HANDLE` | `epoch::pin()` / `epoch::is_pinned()` 如何路由到 `LocalHandle` |
| [`src/collector.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs) | `Collector` 与 `LocalHandle` 的公开 API | `LocalHandle::pin/is_pinned` 与可重入测试 `pin_reentrant` |

## 4. 核心概念与源码讲解

### 4.1 Guard：一根指向 Local 的指针

#### 4.1.1 概念说明

很多初学者第一次看 `epoch::pin()` 的返回值时，会以为 `Guard` 内部装着「epoch 编号」「屏障状态」「时间戳」之类的东西。其实都不是。`Guard` 的全部状态只有**一个裸指针**，指向当前线程的参与者 `Local`。

可以这样建立直觉：

- `Local` 是「真正干活的员工」——它持有 local epoch、计数器、本地垃圾袋等所有状态。
- `Guard` 是一张「工牌」——它本身不存什么信息，只记录「我属于哪个 `Local`」。工牌存在的意义有两条：
  1. **RAII 凭证**：工牌 drop 时，前台（`Local`）会被告知「我走人了」，从而可能执行 unpin。
  2. **生命周期锚点**：前面 u2-l7 讲过，`Shared<'g, T>` 的 `'g` 就是借用自 `&'g Guard`。工牌是生命周期那一端的具体物。

为什么不让 `Guard` 直接持有 epoch 信息？因为可重入 pin（见 4.2）要求「多个 guard 共享一次实际 pin」。如果每个 guard 各存一份 epoch，计数与状态就会四分五裂。把状态统一收口在 `Local` 里，guard 只做轻量凭证，是最干净的设计。

#### 4.1.2 核心流程

一次 `epoch::pin()` 的调用链：

```text
epoch::pin()                                 // src/default.rs
  └─ with_handle(|h| h.pin())                // 取线程局部 LocalHandle
       └─ LocalHandle::pin()                 // src/collector.rs
            └─ unsafe { (*self.local).pin() }   // 转发到 Local::pin
                 └─ Local::pin(&self) -> Guard  // src/internal.rs
                      ├─ guard_count += 1
                      ├─ 若 guard_count 从 0 变 1（首个 guard）：
                      │     ├─ 写入 pinned 的 local epoch
                      │     ├─ SeqCst 内存屏障（u5 详解）
                      │     └─ 每 128 次 pin 触发一次 collect
                      └─ 返回 Guard { local: self }
```

`Guard::drop` 的反向链路：

```text
Guard::drop(&mut self)
  └─ if self.local 非空：
       └─ local.unpin()
            ├─ guard_count -= 1
            └─ 若 guard_count 从 1 变 0（最后一个 guard）：
                  ├─ 把 local epoch 清回 starting
                  └─ 若 handle_count 也为 0：finalize（销毁参与者）
```

注意两条链路里反复出现的 `guard_count`——它是可重入性的核心，下一节详述。

#### 4.1.3 源码精读

`Guard` 的结构定义极简，只有一个字段 [src/guard.rs:70-72](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L70-L72)：

```rust
pub struct Guard {
    pub(crate) local: *const Local,
}
```

这行中文说明：`Guard` 就是一根 `*const Local` 裸指针，指向所属线程的参与者；此外没有任何数据。

`Drop` 实现也只做一件事——把指针解引用成 `&Local` 后调用 `unpin` [src/guard.rs:416-423](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L416-L423)：

```rust
impl Drop for Guard {
    #[inline]
    fn drop(&mut self) {
        if let Some(local) = unsafe { self.local.as_ref() } {
            local.unpin();
        }
    }
}
```

这里的 `if let Some(local) = self.local.as_ref()` 是一个关键守卫：当 `self.local` 是 null 指针时（即 `unprotected()` 假守卫，见 4.3），`as_ref()` 返回 `None`，`drop` 直接什么都不做。这就是为什么丢弃一个假守卫是安全的 no-op。

真正承担状态的是 `Local`，看它的字段 [src/internal.rs:292-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L292-L318)：

```rust
#[repr(C)] // Note: `entry` must be the first field
pub(crate) struct Local {
    entry: Entry,                                   // 侵入式链表节点
    collector: UnsafeCell<ManuallyDrop<Collector>>, // 所属收集器
    pub(crate) bag: UnsafeCell<Bag>,                // 本地垃圾袋
    guard_count: Cell<usize>,                       // 当前活跃 guard 数（pin 深度）
    handle_count: Cell<usize>,                      // 活跃 handle 数
    pin_count: Cell<Wrapping<usize>>,               // 历史 pin 总次数
    epoch: CachePadded<AtomicEpoch>,                // local epoch
}
```

中文说明：与「pin 状态」直接相关的是 `guard_count`、`handle_count`、`pin_count` 三个 `Cell`（单线程内的可变单元），以及 `epoch`。`guard_count` 记录「当前有几个 `Guard` 还活着」，`handle_count` 记录「当前有几个 `LocalHandle` 还活着」。注意 `entry` 被标为 `#[repr(C)]` 且必须是首字段——这是为了侵入式链表能通过 `Local *` ↔ `Entry *` 的裸指针互转（u4-l15、u6-l20 详述），本讲只需知道这个布局约定。

`Local::pin` 的关键片段（先只看与计数有关的部分）[src/internal.rs:402-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L402-L462)：

```rust
pub(crate) fn pin(&self) -> Guard {
    let guard = Guard { local: self };

    let guard_count = self.guard_count.get();
    self.guard_count.set(guard_count.checked_add(1).unwrap());

    if guard_count == 0 {
        // 只有「首个 guard」才真正写入 pinned epoch 并执行屏障
        // ……（epoch 写入 + SeqCst fence，u5 详解）……
        // 每 PINNINGS_BETWEEN_COLLECT(=128) 次 pin 触发一次 collect
    }

    guard
}
```

中文说明：`pin()` 先无条件把 `guard_count` 加 1，但**只有当加 1 之前的旧值是 0**（也就是从「没有 guard」变成「有 1 个 guard」）时，才执行真正的 pin 工作。否则只是把计数加 1、返回一个新的 guard。这正是可重入的实现基础。

对应的 `unpin` [src/internal.rs:465-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L465-L479)：

```rust
pub(crate) fn unpin(&self) {
    let guard_count = self.guard_count.get();
    self.guard_count.set(guard_count - 1);

    if guard_count == 1 {
        self.epoch.store(Epoch::starting(), Ordering::Release);
        if self.handle_count.get() == 0 {
            unsafe { Self::finalize(self); }
        }
    }
}
```

中文说明：`unpin` 无条件把计数减 1，但**只有当减之前的旧值是 1**（即最后一个 guard 退出）时，才把 local epoch 清回 `starting()`（真正 unpin）。`guard_count` 与 `handle_count` 共同决定 `finalize` 时机，这一点在 4.2 的练习里会用到，完整销毁流程留到 u4-l15 / u5-l19。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`Guard` 只是个轻量凭证，真正的 pin 状态在 `Local`，由 `guard_count` 记录深度」。

**操作步骤**：

1. 新建一个二进制 crate，把本仓库作为路径依赖加入：

   ```toml
   # 在你的实验项目 Cargo.toml 里
   [dependencies]
   crossbeam-epoch = { path = "../crossbeam-epoch" }
   ```

2. 写一个最小程序，从默认收集器拿一个 `Guard`，然后用 `is_pinned()` 观察 pin 状态在 guard 前后的变化：

   ```rust
   use crossbeam_epoch as epoch;

   fn main() {
       println!("pin 前  is_pinned = {}", epoch::is_pinned()); // 期望 false
       {
           let _g = epoch::pin();
           println!("持有时 is_pinned = {}", epoch::is_pinned()); // 期望 true
       } // _g 在这里 drop
       println!("drop 后 is_pinned = {}", epoch::is_pinned()); // 期望 false
   }
   ```

**需要观察的现象**：三行输出依次为 `false`、`true`、`false`。

**预期结果**：`_g` 离开作用域被 drop 时触发 `Guard::drop → Local::unpin`，因为这是唯一一个 guard（`guard_count` 从 1 回到 0），local epoch 被清回 starting，于是 `is_pinned()` 重新变回 false。若输出与预期不符，请确认依赖是否启用了 `std`（默认开启），因为 `pin`/`is_pinned` 仅在 `std` 下导出（见 u1-l2）。

> 说明：本实践依赖默认收集器，需要 `std` 特性。若你在 `no_std` + `alloc` 环境，应改用自建 `Collector`（见 u4-l13），实践思路相同。

#### 4.1.5 小练习与答案

**练习 1**：`Guard` 的 `Send`/`Sync` 是怎样的？为什么这样设计？

**参考答案**：`Guard` 含 `*const Local` 裸指针，裸指针默认 `!Send + !Sync`，因此 `Guard` 不可跨线程发送或共享。这是有意为之——`Local` 是线程私有的参与者，`Guard` 必须留在创建它的那个线程上，否则 `Drop` 时在错误的线程调用 `unpin` 会破坏计数与 epoch 不变量。

**练习 2**：`Guard::drop` 里为什么要写 `if let Some(local) = self.local.as_ref()`，而不是直接 `unsafe { (*self.local).unpin() }`？

**参考答案**：因为存在 `local` 为 null 的「假守卫」（`unprotected()`，见 4.3）。直接解引用 null 是未定义行为；用 `as_ref()` 在 null 时返回 `None`，让假守卫的 drop 安全地成为 no-op。

---

### 4.2 可重入 pin 与 is_pinned

#### 4.2.1 概念说明

「可重入（reentrant）」是指：在**同一个线程**里，即使当前已经持有一个 `Guard`，你仍然可以再次调用 `epoch::pin()` 拿到第二个 `Guard`，而不会死锁、不会 panic，也不会真正执行第二次 pin 工作。

这在实际代码里非常常见。比如你写了一个无锁数据结构的操作函数，它内部 `epoch::pin()`；而调用方在持有一个 guard 的循环里反复调用这个函数——每次调用都会再 pin 一次。如果 pin 不可重入，这种嵌套就会出问题。

crossbeam-epoch 的设计是：**实际 pin 只发生一次（首个 guard 创建时），后续每个 guard 只是把 `guard_count` 加 1**；相应地，**实际 unpin 只在最后一个 guard drop 时发生**。中间任意数量的 guard 创建/销毁都不会触碰 local epoch。

`is_pinned()` 就是读 `guard_count` 是否大于 0 [src/internal.rs:372-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L372-L375)：

```rust
pub(crate) fn is_pinned(&self) -> bool {
    self.guard_count.get() > 0
}
```

中文说明：只要还有任意一个 `Guard` 活着（`guard_count > 0`），线程就算「pin 着」。

#### 4.2.2 核心流程

两个嵌套 guard 的生命周期与计数变化：

```text
guard_count = 0
  g1 = pin()        →  guard_count: 0 → 1   （真正 pin：写 epoch + 屏障）
  g2 = pin()        →  guard_count: 1 → 2   （只 +1，不碰 epoch）
  drop(g2)          →  guard_count: 2 → 1   （只 -1，不 unpin）
  drop(g1)          →  guard_count: 1 → 0   （真正 unpin：清 epoch）
```

整个过程中 `is_pinned()` 从 g1 创建那一刻起就一直是 true，直到 g1（最后一个）被 drop 才变 false。

#### 4.2.3 源码精读

官方文档用一个 doctest 直观展示了可重入 [src/guard.rs:52-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L52-L67)：

```rust
/// # Multiple guards
///
/// Pinning is reentrant and it is perfectly legal to create multiple guards. In that case, the
/// thread will actually be pinned only when the first guard is created and unpinned when the last
/// one is dropped:
///
/// ```
/// let guard1 = epoch::pin();
/// let guard2 = epoch::pin();
/// assert!(epoch::is_pinned());
/// drop(guard1);
/// assert!(epoch::is_pinned());
/// drop(guard2);
/// assert!(!epoch::is_pinned());
/// ```
```

中文说明：两个 guard 中间 drop 掉 `guard1`，`is_pinned()` 仍是 true，因为 `guard2` 还活着；只有 `guard2` 也 drop 后才变 false。

更结构化的可重入测试在 `collector.rs`，使用自建 `Collector` 而非默认收集器 [src/collector.rs:134-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L134-L151)：

```rust
#[test]
fn pin_reentrant() {
    let collector = Collector::new();
    let handle = collector.register();
    drop(collector);

    assert!(!handle.is_pinned());
    {
        let _guard = &handle.pin();
        assert!(handle.is_pinned());
        {
            let _guard = &handle.pin();   // 第二层 guard
            assert!(handle.is_pinned());
        }                                  // 第二层 drop，仍 pinned
        assert!(handle.is_pinned());
    }                                      // 第一层 drop，才 unpinned
    assert!(!handle.is_pinned());
}
```

中文说明：这个测试用 `LocalHandle::pin`（`collector.rs:83-85`）逐层进入与退出，验证 `is_pinned()` 的真值随「最后一个 guard」是否存活而翻转，而不是随某个具体 guard。

回顾 4.1.3 的 `Local::pin` 与 `Local::unpin`：`pin` 用 `if guard_count == 0` 守住「真正 pin」分支，`unpin` 用 `if guard_count == 1` 守住「真正 unpin」分支。这两个守卫共同保证了「嵌套深度为 N 时只 pin 一次」。计数本身用的是 `Cell<usize>`——因为 `Local` 是线程私有的，不需要原子操作，普通 `Cell` 就够，开销极小。

#### 4.2.4 代码实践

**实践目标**：亲手验证 pin 的可重入，并体会 `is_pinned()` 只在「最后一个 guard」被 drop 时翻转。

**操作步骤**：

```rust
use crossbeam_epoch as epoch;

fn main() {
    // 没有任何 guard
    println!("初始        is_pinned = {}", epoch::is_pinned());

    let guard1 = epoch::pin();
    println!("pin #1 后   is_pinned = {}", epoch::is_pinned());

    let guard2 = epoch::pin();
    println!("pin #2 后   is_pinned = {}", epoch::is_pinned());

    drop(guard1);
    println!("drop g1 后  is_pinned = {}", epoch::is_pinned()); // 仍 true

    drop(guard2);
    println!("drop g2 后  is_pinned = {}", epoch::is_pinned()); // 变 false
}
```

**需要观察的现象**：输出应该形如

```text
初始        is_pinned = false
pin #1 后   is_pinned = true
pin #2 后   is_pinned = true
drop g1 后  is_pinned = true
drop g2 后  is_pinned = false
```

**预期结果**：`g1` 提前 drop 不会让线程 unpin，因为 `g2` 仍持有引用计数；只有两个都 drop，`guard_count` 归零才真正 unpin。这与上面的理论流程完全一致。

> 进阶：在 `drop(guard1)` 与 `drop(guard2)` 之间，再 `epoch::pin()` 拿第三个 guard 并立刻 drop，观察 `is_pinned()` 依旧为 true——这能进一步验证「计数」而非「某个特定 guard」决定了 pin 状态。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Local` 里 `guard_count` 用 `Cell<usize>` 而不是 `AtomicUsize`？

**参考答案**：因为每个 `Local` 是某一线程私有的参与者，`Guard` 不可跨线程移动（见 4.1.5），所以 `guard_count` 的所有读写都发生在同一线程内，没有数据竞争。`Cell` 比 `AtomicUsize` 更轻量（无原子指令开销），足够安全。

**练习 2**：在 4.2.4 的程序里，如果把 `drop(guard1)` 和 `drop(guard2)` 的顺序交换（先 drop g2 再 drop g1），`is_pinned()` 的最终结果会变吗？

**参考答案**：不会变。无论哪个 guard 先 drop，`guard_count` 都是从 2 减到 1 再减到 0；只有最后一次减到 0 才触发真正 unpin。可重入 pin 与 guard 的 drop 顺序无关。

---

### 4.3 unprotected()：假守卫

#### 4.3.1 概念说明

有些场景下，我们其实**不需要**真的 pin 当前线程，但又必须给 `Atomic::load` 之类的方法传一个 `&Guard`（因为 API 签名强制要求）。典型例子：

- **构造数据结构时**：对象刚刚 `Owned::new` 出来，只有当前线程能看到，绝无并发访问，pin 纯属浪费。
- **析构（`Drop`）时**：整个数据结构正在销毁，没有其他线程可能再访问它的内部 `Atomic`，此时 pin 不仅浪费，还可能**拖慢全局 GC**（因为 pin 会卡住 epoch 推进）。

`epoch::unprotected()` 就是为这些场景准备的「假守卫」。它返回一个 `&'static Guard`，但其 `local` 字段是 **null 指针**。它在类型上是个合法的 `&Guard`，能让 `load` 编译通过，但它不对应任何真实的 `Local`，因此：

- **不 pin 线程**：它根本没碰 `Local`，自然没增加 `guard_count`。
- **`defer` / `defer_unchecked` / `defer_destroy` 立即执行**：因为查不到 `Local` 来缓存闭包，干脆当场调用。
- **`flush` 是 no-op**。
- **`collector()` 返回 `None`**。

> ⚠️ `unprotected()` 是 `unsafe fn`。它的安全契约是：**用这个 guard 从 `Atomic` load/解引用时，该 `Atomic` 没有被其他线程并发修改**。一旦违反，就是数据竞争甚至 use-after-free。

#### 4.3.2 核心流程

假守卫的「null 分支」在多个 API 里被统一处理。以 `defer_unchecked` 为例：

```text
guard.defer_unchecked(f)
  └─ if let Some(local) = self.local.as_ref():   // 真 guard
       └─ local.defer(Deferred::new(f), self)     // 入本地垃圾袋，延迟执行
     else:                                        // 假 guard (local == null)
       └─ drop(f())                               // 立即执行 f
```

也就是说，假守卫把「延迟回收」降级成了「当场执行」。这在单线程析构里正是我们想要的：此时没有别的线程，延迟没有意义，直接 drop 即可。

#### 4.3.3 源码精读

`unprotected` 的定义 [src/guard.rs:517-528](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L517-L528)：

```rust
#[inline]
pub unsafe fn unprotected() -> &'static Guard {
    // An unprotected guard is just a `Guard` with its field `local` set to null.
    // We make a newtype over `Guard` because `Guard` isn't `Sync`, so can't be directly stored in
    // a `static`
    struct GuardWrapper(Guard);
    unsafe impl Sync for GuardWrapper {}
    static UNPROTECTED: GuardWrapper = GuardWrapper(Guard {
        local: core::ptr::null(),
    });
    &UNPROTECTED.0
}
```

中文说明：假守卫就是一个 `local` 为 null 的 `Guard`，存放在一个 `static` 里，返回它的 `&'static` 引用。注意两层绕弯：

1. **`Guard` 不是 `Sync`**（见 4.1.5，裸指针的原因），不能直接放进 `static`。
2. 于是用 newtype `GuardWrapper(Guard)`，给它手动 `unsafe impl Sync`——这是合法的，因为这个 guard 的 `local` 是 null，对它的任何操作（`drop`、`defer` 等）都会走 null 分支，不会真的访问任何 `Local`，多线程共享它没有危险。

`defer_unchecked` 的 null 分支 [src/guard.rs:189-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L189-L200)：

```rust
pub unsafe fn defer_unchecked<F, R>(&self, f: F)
where
    F: FnOnce() -> R,
{
    unsafe {
        if let Some(local) = self.local.as_ref() {
            local.defer(Deferred::new(move || drop(f())), self);
        } else {
            drop(f());     // 假守卫：立即执行
        }
    }
}
```

中文说明：`self.local.as_ref()` 为 `None`（假守卫）时，直接 `drop(f())` 当场执行闭包，而不是把它塞进垃圾袋延迟。`flush` 与 `collector` 用的是同一种 null 守卫模式 [src/guard.rs:295-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L295-L299)、[src/guard.rs:411-413](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L411-L413)：`flush` 在 null 时直接返回（no-op），`collector` 在 null 时返回 `None`。

官方文档给出最典型的用途——在 Treiber 栈的 `Drop` 里用假守卫遍历并释放整条链 [src/guard.rs:479-513](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L479-L513)：

```rust
/// struct Stack<T> { head: Atomic<Node<T>> }
/// struct Node<T> { data: ManuallyDrop<T>, next: Atomic<Node<T>> }
impl<T> Drop for Stack<T> {
    fn drop(&mut self) {
        unsafe {
            // Unprotected load.
            let mut node = self.head.load(Relaxed, epoch::unprotected());
            while let Some(n) = node.as_ref() {
                let next = n.next.load(Relaxed, epoch::unprotected());
                let mut o = node.into_owned();   // 取回所有权
                ManuallyDrop::drop(&mut o.data); // 析构数据
                drop(o);                          // 释放节点
                node = next;
            }
        }
    }
}
```

中文说明：`Stack::drop` 执行时，外部已经没有引用这个栈了（否则不会走到它的 `Drop`），所以内部 `Atomic` 不存在并发修改，用 `Relaxed` + `unprotected()` 就足够安全。如果这里改用真 `pin()`，不但多此一举，还会在析构期间卡住 epoch 推进、影响其他线程的 GC。

> 小注：`unprotected()` 文档里提到「可以通过 `unprotected().clone()` 造更多假守卫」。但当前源码中 `Guard` 并未实现 `Clone`/`Copy`（已核对），所以实践中**直接再次调用 `epoch::unprotected()`** 或**复用同一个 `&'static Guard` 引用**即可，不必依赖 `.clone()`。

#### 4.3.4 代码实践

**实践目标**：写一个单线程 Treiber 栈，在它的 `Drop` 实现里用 `epoch::unprotected()` 安全地遍历并释放整条链，体会「假守卫让 `defer` 立即执行、避免 pin 拖慢 GC」。

**操作步骤**：

```rust
use crossbeam_epoch::{self as epoch, Atomic, Owned};
use std::mem::ManuallyDrop;
use std::sync::atomic::Ordering::Relaxed;

struct Node<T> {
    data: ManuallyDrop<T>,
    next: Atomic<Node<T>>,
}

struct TreiberStack<T> {
    head: Atomic<Node<T>>,
}

impl<T> TreiberStack<T> {
    fn new() -> Self {
        TreiberStack { head: Atomic::null() }
    }

    fn push(&self, val: T) {
        let guard = &epoch::pin();
        let mut new = Owned::new(Node {
            data: ManuallyDrop::new(val),
            next: Atomic::null(),
        });
        loop {
            let cur = self.head.load(Relaxed, guard);
            new.next.store(cur, Relaxed);
            match self.head.compare_exchange(cur, new, Relaxed, Relaxed, guard) {
                Ok(_) => return,
                Err(e) => new = e.new,
            }
        }
    }
}

impl<T> Drop for TreiberStack<T> {
    fn drop(&mut self) {
        unsafe {
            // 析构时不存在并发，用假守卫 + Relaxed 即可
            let mut node = self.head.load(Relaxed, epoch::unprotected());
            while let Some(n) = node.as_ref() {
                let next = n.next.load(Relaxed, epoch::unprotected());
                let mut o = node.into_owned();        // 当场取得所有权并释放
                ManuallyDrop::drop(&mut o.data);
                drop(o);
                node = next;
            }
        }
    }
}

fn main() {
    let s = TreiberStack::new();
    for i in 0..1_000 {
        s.push(i);
    }
    // s 离开作用域，Drop 用 unprotected() 逐节点释放
    println!("栈析构完成");
}
```

**需要观察的现象**：程序正常退出、无 panic、无内存错误（若用 miri 跑 `cargo +nightly miri run` 应通过）。注意 `push` 用了真 `epoch::pin()`（多线程可能并发），而 `Drop` 用了 `epoch::unprotected()`（单线程、无并发）。

**预期结果**：`Drop` 中每次 `node.into_owned()` 当场析构并释放节点，整条链被完整回收；因为没用真 pin，析构过程不会卡住默认收集器的 epoch 推进。

> 说明：上述 `push` 用 `Relaxed` 仅为简化示例；若栈会被多线程并发访问，`compare_exchange` 的成功 ordering 至少应为 `AcqRel`、`load` 应为 `Acquire`，否则在弱内存模型下会有数据竞争（详见 u2-l7 关于 Ordering 的讨论）。本实践聚焦 `Drop` 里的 `unprotected()`，故 `push` 简化处理。

#### 4.3.5 小练习与答案

**练习 1**：`unprotected()` 为什么必须返回 `&'static Guard`，而不是 `Guard`（按值）？

**参考答案**：因为很多 API（如 `Atomic::load`）要求 `&'g Guard`，并把返回的 `Shared<'g, T>` 的生命周期绑定到这个借用上。如果 `unprotected()` 按值返回 `Guard`，调用者写出 `a.load(Relaxed, &epoch::unprotected())` 时，临时 `Guard` 在语句末尾就被 drop，借用生命周期过短，编译会失败（其实假守卫的 drop 是 no-op，但借用检查器不知道）。返回 `&'static Guard` 让借用拥有 `'static` 生命周期，任意长的 `Shared` 都能挂上去。

**练习 2**：为什么 `GuardWrapper` 要 `unsafe impl Sync`？这个 `unsafe` 安全吗？

**参考答案**：`Guard` 含裸指针，默认 `!Sync`，无法放进 `static`（`static` 要求类型 `Sync`）。`GuardWrapper` 通过 newtype 绕过这一点，并手动声明 `Sync`。这是安全的，因为这个 guard 的 `local` 恒为 null，所有方法（`drop`、`defer`、`flush`、`collector`）都会走 null 分支，绝不会真正访问任何 `Local`，多线程共享它不会引发数据竞争。

**练习 3**：在假守卫上调用 `guard.defer(move || println!("hi"))`，会发生什么？和真守卫有何区别？

**参考答案**：闭包会**立即执行**（`defer_unchecked` 走 null 分支 `drop(f())`），打印 `hi`。而真守卫会先把闭包塞进本地垃圾袋，等宽限期（全局 epoch 前进 ≥ 2）过后才由某个线程执行，可能延迟很久。这正是假守卫在单线程析构场景下的优势：没有并发，延迟没有意义，立即执行最简单。

---

## 5. 综合实践

把本讲三块内容（Guard 凭证、可重入 pin、假守卫）串起来，完成下面这个小任务。

**任务**：实现一个带 Drop 计数的 Treiber 栈，验证两件事——

1. **可重入 pin 不影响 unpin 时机**：在持有 guard 的循环里反复调用一个内部会再次 `epoch::pin()` 的函数，确认线程只在最外层 guard 存活期间被 pin。
2. **析构时用 `unprotected()` 能正确回收全部节点**：用一个带 `Drop` 计数的元素类型，在栈析构后断言 drop 次数等于 push 次数。

参考骨架（需自行补全并在本地运行）：

```rust
use crossbeam_epoch::{self as epoch, Atomic, Owned};
use std::mem::ManuallyDrop;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
use std::sync::atomic::Ordering;

static DROPS: AtomicUsize = AtomicUsize::new(0);

struct Counted(i32);
impl Drop for Counted { fn drop(&mut self) { DROPS.fetch_add(1, Relaxed); } }

struct Node<T> { data: ManuallyDrop<T>, next: Atomic<Node<T>> }
struct Stack<T> { head: Atomic<Node<T>> }

// 1) push 内部会 pin；外层也 pin，验证可重入
// 2) Drop 用 unprotected() 遍历释放，验证回收
//    析构时调用 DROPS.load() 应等于 push 次数

fn main() {
    let s = Stack { head: Atomic::null() };
    let n = 128;

    {
        let outer = &epoch::pin();              // 外层 guard
        assert!(epoch::is_pinned());
        for i in 0..n {
            // 假设 push(&s, Counted(i)) 内部又会 epoch::pin() 一次（可重入）
            // ... 你的 push 实现 ...
            let _ = outer;                      // 占位，提示外层 guard 仍存活
        }
        assert!(epoch::is_pinned());            // 仍 pinned
    }                                            // 外层 drop，至此才 unpin
    assert!(!epoch::is_pinned());

    drop(s);                                     // Drop 用 unprotected() 回收
    assert_eq!(DROPS.load(Relaxed), n);          // 全部节点已 drop
}
```

**验收点**：

- 第二个断言（`!is_pinned()`）成立，证明外层 guard 是最后一个，可重入的内部 pin 没有让线程「卡在 pin 状态」。
- 最后一个断言（`DROPS == n`）成立，证明 `unprotected()` 在析构时确实逐节点调用了 `into_owned()` 并触发了 `Counted::drop`。
- 若开启 miri 运行应无 UB 报告。

## 6. 本讲小结

- `Guard` 的全部状态是一根 `*const Local` 裸指针，它是「线程已 pin」的轻量凭证，不保存 epoch 等信息；真正的 pin 状态住在 `Local` 里。
- `Guard::drop` 把指针解引用为 `&Local` 后调用 `unpin`；若 `local` 为 null（假守卫），则 drop 是 no-op。
- pin 是**可重入**的：首个 guard 触发真正 pin（写 local epoch + 屏障），后续 guard 只增加 `guard_count`；最后一个 guard 才触发真正 unpin。`is_pinned()` 等价于 `guard_count > 0`。
- `Local` 的 `guard_count` 用 `Cell` 而非 `AtomicUsize`，因为 `Guard` 不可跨线程移动、计数只在单线程内变化。
- `epoch::unprotected()` 返回 `&'static Guard`，其 `local` 为 null：不 pin 线程、`defer` 立即执行、`flush` 是 no-op、`collector()` 返回 `None`，专用于无并发的构造/析构场景。
- 假守卫存进 `static` 需要 `GuardWrapper` + `unsafe impl Sync`，这之所以安全是因为它恒走 null 分支、不触碰任何 `Local`。

## 7. 下一步学习建议

本讲把 `Guard` 当作「黑盒凭证」用，但没有回答两个深层问题：

1. **`Local::pin` 那段被略过的「写 local epoch + SeqCst 屏障」到底防的是什么？** 这关乎为何 pin 能让后续 `Atomic::load` 安全地解引用共享对象。→ 进阶阅读 u5-l17（Epoch 表示）与 u5-l18（pin/unpin 与内存屏障）。
2. **`guard_count` 归零后，`handle_count` 也归零时会发生什么？** 这涉及 `finalize` 把 `Local` 从全局链表摘除、把残余垃圾袋入队的完整销毁流程。→ 阅读 u4-l15（Local 参与者）与 u5-l19（try_advance 与 collect）。

此外，本讲的 `unprotected()` 会反复出现：`Local::register`、`finalize` 等内部函数都用它来在「确定无并发」时跳过 pin。当你阅读 u4（Collector 与 Global/Local 结构）和 u5（回收主链路）时，可以回头留意这些用法，它们都是「假守卫」思想在内部代码里的应用。

> 建议继续按顺序学习 u3-l10（defer / defer_destroy）与 u3-l11（Deferred），它们承接本讲的「`defer` 把闭包塞进本地垃圾袋」机制，讲清楚闭包到底如何被存储与最终调用。
