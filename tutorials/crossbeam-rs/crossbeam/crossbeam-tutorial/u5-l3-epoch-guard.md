# Guard 与 pin 机制

## 1. 本讲目标

本讲承接 u5-l2（`Atomic`/`Shared`/`Owned` 原子指针与标签），进入 crossbeam-epoch 的「使用入口」：读者拿到一个 `Shared<'g, T>` 指针后，那个生命周期 `'g` 到底由谁代表？答案就是本讲的主角 **`Guard`**。

学完本讲你应该能够：

1. 说清 `Guard` 是什么、它为什么是 RAII 凭证、为什么 `drop` 它就等于 unpin。
2. 解释「可重入 pin」——同一个线程连续 `pin()` 多次为何不会重复登记 epoch，靠的是 `guard_count` 计数。
3. 区分 `defer` / `defer_destroy` / `defer_unchecked` 三个延迟销毁 API 的安全边界，并能说明 `defer_unchecked` 为什么是 `unsafe`。
4. 理解 `Deferred` 如何用「函数指针 + 定长缓冲」把任意 `FnOnce()` 类型擦除成定长结构，何时内联、何时装箱。
5. 看懂 `default.rs` 提供的全局收集器与线程局部 `HANDLE`，明白随手一调的 `epoch::pin()` 背后发生了什么。

---

## 2. 前置知识

本讲默认你已经掌握 u5-l1 与 u5-l2 的结论，下面三句话复习关键概念：

- **pin（钉住）**：线程在访问无锁数据结构里的共享对象前，必须先 pin 自己。pin 之后，回收器承诺「现在被摘除的对象不会被立刻销毁」，从而避免读取方还在用、删除方就 free 的 use-after-free。
- **`Shared<'g, T>`**：从 `Atomic` 读出的受保护指针，其生命周期 `'g` 绑定在 `Guard` 上——`Guard` 就是 `'g` 的具象化，从类型层保证读出的引用不活过本次 pin。
- **延迟销毁（deferred destruction）**：删除方不能立即释放节点，而是把「释放动作」塞进一个袋子，等全局 epoch 推进够了再统一执行。

本讲用到但不过度展开的两个底层细节（详见 u5-l5）：

- 每个 pin 中的参与者最多见证一次 epoch 推进，因此一个袋子要等 **两个 epoch 之后** 才能安全销毁，判定式为：

\[ \text{global\_epoch} - \text{bag\_epoch} \geq 2 \quad (\text{按 wrapping 语义}) \]

- `pin()` 真正做的事里含一个 `SeqCst` fence，作用是把「我已登记在 epoch E」这一发布动作与「之后从 `Atomic` 读对象指针」隔离开，防止读被重排到登记之前。

另外，`Guard` 内部持有一个裸指针 `*const Local`，指向「参与者」结构。参与者（`Local`）的完整实现位于 `internal.rs`，本讲只取其中与 `Guard` 直接相关的 `pin`/`unpin`/`defer`/`flush` 几个方法，`Local` 的全貌留到 u5-l4/u5-l5。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crossbeam-epoch/src/guard.rs` | `Guard` 类型本体、`defer`/`defer_destroy`/`defer_unchecked`/`flush`/`repin` 等方法、`unprotected()` 假守卫 |
| `crossbeam-epoch/src/deferred.rs` | `Deferred`：把任意 `FnOnce()` 类型擦除成定长结构（内联或装箱） |
| `crossbeam-epoch/src/default.rs` | 全局默认收集器、线程局部 `HANDLE`、对外 `pin()`/`is_pinned()`/`default_collector()` 入口 |
| `crossbeam-epoch/src/internal.rs` | `Local` 参与者的 `pin`/`unpin`/`defer`/`flush` 实现，`guard_count` 计数字段就在这里；`Bag`/`SealedBag` 延迟队列 |
| `crossbeam-epoch/src/collector.rs` | `LocalHandle::pin()` 一行转发到 `Local::pin()`；含若干可复用的测试 |

---

## 4. 核心概念与源码讲解

### 4.1 Guard：RAII 凭证与可重入 pin

#### 4.1.1 概念说明

`Guard` 是「当前线程处于 pin 状态」的**凭证（witness）**。它的设计遵循 Rust 的 RAII 习惯：

- **获取即 pin**：调用 `pin()` 返回一个 `Guard`，拿到它就意味着线程已被钉住。
- **释放即 unpin**：`Guard` 被 `drop` 时，自动把线程 unpin。
- **可重入**：同一个线程可以同时持有多个 `Guard`（例如嵌套调用 `pin()`）。真正的「登记 epoch + fence」只在第一次 pin 时做一次，最后一个 `Guard` 释放时才真正 unpin。

为什么需要可重入？因为你很可能在已经 pin 的函数里，又调用了一个内部也会 `pin()` 的库函数。若每次都重新做一遍「加载全局 epoch + `SeqCst` fence」，既慢又无意义；用计数把内层 pin 折叠掉，是最自然的做法。

`Guard` 本身极薄——只持有一个指向参与者 `Local` 的裸指针，所有方法都是把活儿转交给 `Local`。

#### 4.1.2 核心流程

pin / unpin 的状态由参与者 `Local` 上的 `guard_count: Cell<usize>` 这个**线程局部**计数器驱动（`Cell` 非 `Sync`，所以它天生只属于一个线程，无需原子操作）：

```
pin():
    guard_count += 1
    若 guard_count 由 0 变 1（首次 pin）：
        读取全局 epoch E
        把「E 的 pinned 形态」写入 self.epoch       # 对外发布：我钉在 E
        执行 SeqCst fence                         # 隔离后续读
        pin_count += 1
        每 128 次 pin 顺手 collect 一次            # 周期性推进 epoch + 回收
    返回 Guard { local: self }

drop(Guard):
    guard_count -= 1
    若 guard_count 由 1 变 0（最后一个 guard 释放）：
        把 self.epoch 重置为「未钉」                # 解除发布
        若 handle_count 也为 0：销毁参与者          # 见 u5-l4
```

要点：

1. **首尾两端才做重活**：epoch 读写与 fence 只在 `0→1` 与 `1→0` 两个边界发生，中间任意多次嵌套 pin/unpin 只是增减一个 `Cell`，几乎零成本。
2. **fence 的意义**：发布「我钉在 E」必须先于「我从 `Atomic` 读对象」被其它线程观察到，否则回收器可能误判没有人在 E 读，从而提前释放对象。`SeqCst` fence 正是用来钉死这个顺序。
3. **`is_pinned()` 就是查计数**：`guard_count > 0` 即视为已 pin。

#### 4.1.3 源码精读

先看 `Guard` 的定义，只有一个裸指针字段：

[guard.rs:70-72](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L70-L72) —— `Guard` 持有指向参与者 `Local` 的裸指针；若为 `null` 则是「假守卫」（见 4.2）。

`Guard::drop` 把 unpin 完全转交给 `Local::unpin`：

[guard.rs:416-423](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L416-L423) —— 释放 `Guard` 即 unpin，前提是 `local` 非空（假守卫的 `drop` 是 no-op）。

`guard_count` 字段位于 `Local` 结构体内，与 `handle_count`、`pin_count` 并列：

[internal.rs:293-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L293-L318) —— `guard_count: Cell<usize>` 记录「有多少个 `Guard` 在钉住本参与者」；注意它用 `Cell` 而非原子类型，因为参与者是线程私有的。

`is_pinned` 直接读计数：

[internal.rs:372-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L372-L375) —— `guard_count > 0` 即已 pin。

核心的 `Local::pin`，注意 `guard_count == 0` 这条分支只走一次：

[internal.rs:402-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L402-L462) —— 先无条件 `guard_count += 1`；只有当旧值为 0（首次 pin）时才发布 epoch、插 fence、并在每 `PINNINGS_BETWEEN_COLLECT = 128` 次 pin 时触发一次 `collect`。

对应的 `Local::unpin`：

[internal.rs:465-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L465-L479) —— `guard_count -= 1`；只有旧值为 1（最后一个 guard）时才把 epoch 重置为未钉状态，并在没有残留 handle 时销毁参与者。

对外暴露的 `epoch::pin()` 最终就是走到上面这条 `Local::pin`（经 `LocalHandle::pin` 一行转发）：

[collector.rs:82-85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L82-L85) —— `LocalHandle::pin` 解引用裸指针后调用 `Local::pin`。

官方测试 `pin_reentrant` 正好验证了「内层 pin 不改变 pinned 状态、最后一个 guard 释放才 unpin」：

[collector.rs:135-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L135-L151) —— 嵌套两层 `pin()`，内层 `drop` 后 `is_pinned()` 仍为真，外层 `drop` 后才变假。

#### 4.1.4 代码实践

**实践目标**：亲手验证可重入 pin——嵌套 `pin()` 不会让 `is_pinned()` 行为反复跳变。

**操作步骤**（在一个依赖 `crossbeam-epoch` 的小 crate 里，开启 `std` 特性）：

```rust
// 示例代码：验证可重入 pin
use crossbeam_epoch as epoch;

fn main() {
    assert!(!epoch::is_pinned());
    let g1 = epoch::pin();
    assert!(epoch::is_pinned());
    {
        let _g2 = epoch::pin();      // 内层 pin
        assert!(epoch::is_pinned()); // 仍为真
    }                                // _g2 drop：guard_count 1→... 但还 >0
    assert!(epoch::is_pinned());     // 仍为真（外层 g1 还在）
    drop(g1);                        // guard_count → 0，真正 unpin
    assert!(!epoch::is_pinned());
}
```

**需要观察的现象**：内层 `_g2` 释放后 `is_pinned()` 不变；只有外层 `g1` 释放后才变为 `false`。

**预期结果**：全部断言通过，无 panic。

**待本地验证**：若你的环境无法编译，可改为阅读上文的 `pin_reentrant` 测试（[collector.rs:135-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L135-L151)），其断言与本实践等价。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `guard_count` 用 `Cell<usize>` 而不是 `AtomicUsize`？
**答案**：参与者 `Local` 是线程私有的（每线程一个），不存在跨线程并发访问该字段，因此无需原子操作的可见性/原子性保证；`Cell` 更轻。

**练习 2**：假设某线程先 `pin()` 得到 `g1`，又在同一线程 `pin()` 得到 `g2`，随后 `drop(g1)` 再 `drop(g2)`。期间 `SeqCst` fence 执行了几次？
**答案**：1 次。只有 `g1`（`guard_count` 首次 `0→1`）会发布 epoch 并插 fence；`g2` 只是计数 +1，`drop` 也只是计数 -1，都不触及 fence。

---

### 4.2 defer 系列：延迟销毁与三层 API

#### 4.2.1 概念说明

删除一个无锁数据结构里的节点时，不能立刻 `free`——别的线程可能正持着它的 `Shared` 指针在读。正确做法是把「释放这个节点」这件事**延迟**到所有当前 pin 的线程都 unpin 之后。`Guard` 提供了三个层次递进的方法来做这件事：

| 方法 | 签名要点 | 安全性 | 典型用途 |
|------|----------|--------|----------|
| `defer` | `F: FnOnce()->R, F: Send + 'static` | 安全 | 延迟执行任意「满足 Send + 'static」的闭包 |
| `defer_destroy` | `ptr: Shared<'_, T>` | **unsafe** | 延迟销毁一个 `Shared` 指向的对象（最常用） |
| `defer_unchecked` | `F: FnOnce()->R` | **unsafe** | 最底层；`defer` 与 `defer_destroy` 都基于它 |

三者关系是一条调用链：`defer` 内部直接调 `defer_unchecked`；`defer_destroy` 把 `ptr` 包成 `move || ptr.into_owned()` 再调 `defer_unchecked`。

那么为什么 `defer` 是安全的、而 `defer_unchecked`/`defer_destroy` 是 unsafe？关键在于 **`defer` 多了 `F: Send + 'static` 两个约束**：

- `Send`：延迟函数最终可能由**另一个线程**执行（袋子会被推进全局队列，谁 pin 谁可能去回收），所以闭包捕获的数据必须能跨线程移动。
- `'static`：闭包不能借用栈上数据，因为它可能在很久以后、远在当前栈帧销毁之后才运行。

而 `defer_unchecked` 故意**不要求** `Send`/`'static`，因此调用者必须自己担保这两点（以及「被销毁对象不再被其它线程访问」）。为什么要留这个口子？因为最典型的用例——延迟销毁一个 `Shared`——其闭包捕获的 `Shared<'g, T>` 并**没有实现 `Send`**（它绑定了 `Guard` 的生命周期，是借用语义）。类型系统无法证明「宽限期结束后它已不再被共享，所以让别人执行也安全」，于是把证明责任以 `unsafe` 的形式交给调用者。`defer_destroy` 同理：它要删的 `T` 未必 `Send`。

一句话：**`defer` 用类型约束换安全；`defer_unchecked`/`defer_destroy` 用 unsafe 换灵活性**，因为类型系统证明不了的「宽限期后独占」是运行期不变量。

#### 4.2.2 核心流程

延迟销毁的完整链路（结合 `internal.rs`）：

```
guard.defer_destroy(ptr)
  └─ guard.defer_unchecked(move || ptr.into_owned())      # 包成闭包
       └─ 若 local 为空（假守卫）：立即执行闭包，返回       # unprotected 走这条
          否则 local.defer(Deferred::new(闭包), guard):
             └─ bag.try_push(deferred)                     # 塞进线程局部袋子
                若袋子满（MAX_OBJECTS=64）：
                   global.push_bag(bag, guard)             # 给袋子盖章当前 epoch，并入全局队列
                   重试 try_push
```

几个要点：

1. **袋子是线程局部的**：先攒在 `Local::bag` 里，攒够 64 个（`MAX_OBJECTS`）才一次性盖章入队，摊薄全局队列的同步开销。
2. **盖章 = 记录入队时的全局 epoch**：袋子进入全局队列时被「密封（seal）」成 `SealedBag { epoch, bag }`，回收时据此判断是否已过宽限期。
3. **假守卫（unprotected）短路**：`local` 为 `null` 时，`defer_unchecked` 直接 `drop(f())` 立即执行——因为假守卫根本没有宽限期概念，常用于析构数据结构（此刻无并发）。
4. **何时真正销毁**：要等全局 epoch 推进到满足 `global_epoch - bag_epoch >= 2`，由 `collect` 从队列里 `try_pop_if` 取出并 `drop`。这部分详见 u5-l5，本讲只需知道「不是马上」。

#### 4.2.3 源码精读

安全的 `defer`，注意它的约束与对 `defer_unchecked` 的转发：

[guard.rs:90-98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L90-L98) —— `defer` 要求 `F: Send + 'static`，体内在 `unsafe` 块里调 `defer_unchecked`。

底层的 `defer_unchecked`，分「真守卫入袋 / 假守卫立即执行」两路：

[guard.rs:189-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L189-L200) —— `local` 非空则 `local.defer(...)`，否则 `drop(f())` 立即执行。

`defer_destroy` 把 `Shared` 包成闭包，复用 `defer_unchecked`：

[guard.rs:271-273](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L271-L273) —— `defer_destroy` 即 `defer_unchecked(move || ptr.into_owned())`；`into_owned` 把借用指针转成独占 `Owned` 并 `drop` 释放。

参与者 `Local::defer` 的「入袋，满了就推全局队列」循环：

[internal.rs:382-389](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L382-L389) —— `try_push` 失败（袋子满）时 `push_bag` 把当前袋子盖章入队、换一个空袋，再重试。

袋子的容量与「满了返回 `Err`」的语义：

[internal.rs:64-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L64-L69) 与 [internal.rs:100-108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L100-L108) —— `MAX_OBJECTS` 通常为 64（miri/sanitize 下缩到 4 以更易暴露竞争）；`try_push` 满则原样返回 `Err(deferred)`。

宽限期判定（销毁时机）：

[internal.rs:155-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L155-L162) —— `SealedBag::is_expired`：`global_epoch.wrapping_sub(self.epoch) >= 2` 才允许销毁，对应 §2 的公式。

`flush` 把袋子主动推入全局队列并顺手 `collect`，用于「想尽快执行」的场景：

[guard.rs:295-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L295-L299) —— `flush` 委托 `Local::flush`；假守卫是 no-op。

#### 4.2.4 代码实践

**实践目标**：用 `defer_destroy` 销毁一段共享数据，通过 `Drop` 计数观察「它并非立即释放，而是要等后续 pin 周期推进 epoch 之后才真正销毁」。

**操作步骤**（参考官方 `count_drops` 测试的思路）：

```rust
// 示例代码：观察 defer_destroy 的延迟释放
use std::sync::atomic::{AtomicUsize, Ordering};
use crossbeam_epoch::{self as epoch, Owned};

static DROPS: AtomicUsize = AtomicUsize::new(0);

struct Elem;
impl Drop for Elem {
    fn drop(&mut self) { DROPS.fetch_add(1, Ordering::Relaxed); }
}

fn main() {
    let collector = epoch::default_collector();
    let handle = collector.register();
    const COUNT: usize = 1000;

    unsafe {
        let guard = &handle.pin();
        for _ in 0..COUNT {
            let a = Owned::new(Elem).into_shared(guard);
            guard.defer_destroy(a);   // 入袋，此刻 DROPS 仍是 0
        }
        // 注意：这里不 flush，看看「不推进 epoch」会怎样
    }

    println!("刚 defer 完，DROPS = {}", DROPS.load(Ordering::Relaxed));
    // 通常 << COUNT，甚至为 0：袋子还没入队 / epoch 没推进

    // 不断 pin（pin 内部周期性 collect）直到全部销毁
    while DROPS.load(Ordering::Relaxed) < COUNT {
        let guard = &handle.pin();
        collector.global_collect_hint(&guard); // 见下方说明
    }
    println!("最终 DROPS = {}", DROPS.load(Ordering::Relaxed));
}
```

> 说明：`pin()` 内部每 128 次（`PINNINGS_BETWEEN_COLLECT`）会调一次 `collect`；官方测试里直接用 `collector.global.collect(guard)`（`global` 字段在本仓库为 `pub(crate)`，外部 crate 无法访问）。因此本实践以「反复 pin」替代——足够多 pin 之后 epoch 会推进、过期袋子会被销毁。也可阅读 `count_drops` 测试 [collector.rs:285-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L285-L315) 看官方如何在 crate 内用 `collector.global.collect(guard)` 驱动回收。

**需要观察的现象**：`defer_destroy` 调用结束后 `DROPS` 远小于 `COUNT`（甚至为 0）；随着反复 `pin()`，`DROPS` 逐步爬升直到等于 `COUNT`。

**预期结果**：最终 `DROPS == COUNT`，证明每个对象最终都被销毁——但**不是**在 `defer_destroy` 调用那一刻。

**待本地验证**：上述 `global_collect_hint` 是占位写法；若你只能读源码，请精读 `count_drops` 测试（链接见上），其循环 `while DROPS < COUNT { pin(); collect(); }` 与断言 `DROPS == COUNT` 即为本实践的标准答案。

#### 4.2.5 小练习与答案

**练习 1**：用 `epoch::unprotected()`（假守卫）调用 `defer_destroy`，会发生什么？
**答案**：因为假守卫的 `local` 为 `null`，`defer_unchecked` 走 `drop(f())` 分支，闭包**立即**执行——即对象被立即销毁，没有任何延迟。这正是析构整条数据结构时想要的（此时无并发，无需宽限期）。

**练习 2**：为什么 `defer_destroy` 标 `unsafe`，却并不要求 `T: Send`？
**答案**：因为执行销毁的可能是另一线程，类型上理应要求 `T: Send`；但典型的 `T`（经 `Shared` 持有）类型系统证不出 `Send`，而「宽限期结束后该对象已不被任何线程共享、让别人销毁是安全的」是一条**运行期不变量**，无法在类型层表达，故交由调用者以 `unsafe` 担保。

---

### 4.3 Deferred：类型擦除闭包与全局 pin 入口

#### 4.3.1 概念说明

袋子 `Bag` 要装下「各种不同类型」的延迟闭包（销毁 `i32`、销毁 `Node`、销毁 `Vec`……），但 `Bag` 是一个定长数组 `[Deferred; 64]`，元素类型必须统一。解决办法是**类型擦除**：`Deferred` 把任意 `FnOnce()` 统一成同一个具体类型，不暴露原始闭包类型 `F`。

`Deferred` 的内部结构是手写的「闭包表示」经典套路：

- 一个**函数指针** `call: unsafe fn(*mut u8)`，负责把裸指针还原回真实闭包类型 `F` 再调用；
- 一块**定长缓冲** `data: MaybeUninit<[usize; 3]>`（3 个机器字），用来存放闭包本身（捕获的环境）。

存放策略由闭包 `F` 的大小和对齐决定：

- **小闭包（能塞进 3 个字且对齐不超标）**：直接**内联**进 `data`，零堆分配。这是绝大多数情况——例如一个函数指针 + 一个 `Shared`（胖指针）正好 3 个字。
- **大闭包**：把 `F` `Box` 到堆上，把 `Box<F>` 这个指针塞进 `data`，调用时再 `Box::read` 出来执行。

`Deferred` 还标了 `!Send + !Sync`（通过 `PhantomData<*mut ()>`），原因不是它真的不能跨线程（袋子整体 `unsafe impl Send`），而是把它默认标记为不可跨线程、由 `Bag` 在更高层统一担保安全性，避免误用。

本模块的另一条线是 `default.rs` 的**全局 pin 入口**。绝大多数使用者不会自建 `Collector`，而是直接 `epoch::pin()`。这背后是一个**进程级单例收集器** + **每线程一个参与者句柄**：

- `collector()` 用 `OnceLock` 懒初始化唯一的 `Collector`；
- `thread_local! { static HANDLE }` 让每个线程首次访问时自动 `collector().register()` 注册一个参与者；
- `pin()` / `is_pinned()` 经 `with_handle` 拿到本线程的 `LocalHandle` 再转发。

#### 4.3.2 核心流程

`Deferred::new` 的内联/装箱决策：

```
new(f: F):
    size = size_of::<F>(); align = align_of::<F>()
    若 size <= 24 字节 且 align <= [usize;3] 的对齐：    # 内联
        把 f 写入 data 的内存（按 F 布局）
        call = call::<F>（读出 F 再调用）
    否则：                                                # 装箱
        b = Box::new(f)
        把 b 写入 data（按 Box<F> 布局）
        call = call::<F>（读出 Box<F>，(*b)() 调用）

call(self):
    取出函数指针 call，传 data 的裸指针调用               # 还原类型并执行
```

全局 pin 的流程：

```
epoch::pin():
    with_handle(|h| h.pin()):
        HANDLE.try_with(|h| 用本线程的 LocalHandle)
              .unwrap_or_else(|_| collector().register())  # TLS 已析构时兜底新建
        → LocalHandle::pin() → Local::pin() → Guard
```

`with_handle` 用 `try_with`/`unwrap_or_else` 的兜底很关键：线程退出时 `HANDLE` 这个 TLS 会先于该线程上的某些其它 TLS 析构（例如某个对象的 `Drop` 里又调了 `pin()`）。此时 `try_with` 失败，就**临时新建**一个参与者来完成这次 pin，保证 `pin()` 永不 panic、即使在析构中途调用也安全。

#### 4.3.3 源码精读

`Deferred` 的结构与「3 个字」容量：

[deferred.rs:9-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L9-L25) —— `DATA_WORDS = 3`，`Data = [usize; 3]`；`Deferred { call, data, _marker }`，`_marker` 使其 `!Send + !Sync`。

`Deferred::new` 的两条分支（内联 vs 装箱）：

[deferred.rs:44-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L44-L82) —— 先比较 `size`/`align`：小则 `ptr::write` 进 `data` 并生成内联 `call::<F>`（L49-62）；大则 `Box::new(f)` 后把 `Box<F>` 写进 `data`，`call` 里 `Box::read` 再调用（L63-80）。

`Deferred::call` 的「还原类型并执行」：

[deferred.rs:84-89](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L84-L89) —— 取出 `call` 函数指针，以 `data` 的裸指针为参调用；类型还原发生在 `call::<F>` 内部。

`Deferred::NO_OP` 哨兵（袋子初始化与替换用）：

[deferred.rs:34-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L34-L41) —— 一个什么都不做的 `Deferred`，用于 `Bag` 的默认填充与 `Drop` 时的 `mem::replace`。

全局收集器单例：

[default.rs:16-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L16-L33) —— `OnceLock<Collector>` 懒初始化；loom 下用 `loom::lazy_static!` 替代（同一份逻辑可被模型检查）。

线程局部参与者句柄：

[default.rs:35-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L35-L38) —— `HANDLE` 在首次访问时调用 `collector().register()` 注册参与者。

对外入口与兜底逻辑：

[default.rs:41-44](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L41-L44) 与 [default.rs:57-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L57-L65) —— `pin()` 经 `with_handle` 取本线程句柄；`try_with` 失败（TLS 已析构）时兜底 `collector().register()` 临时建一个。

「在线程退出途中也能安全 pin」的回归测试：

[default.rs:77-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L77-L100) —— `pin_while_exiting` 让某 TLS 的 `Drop` 在 `HANDLE` 析构之后再调 `pin()`，验证不 panic（正是 `with_handle` 兜底的功劳）。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `deferred.rs` 的两个测试，确认内联/装箱决策，并估算常见闭包走哪条路径。

**操作步骤**：

1. 阅读 `on_stack` 测试 [deferred.rs:103-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L103-L116)：闭包捕获 `a = [0usize; 1]`（1 个字）+ 一个 `&Cell` 引用，整体 ≤ 3 字 → **内联**。
2. 阅读 `on_heap` 测试 [deferred.rs:118-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L118-L131)：闭包捕获 `a = [0usize; 10]`（10 个字）> 3 字 → **装箱**。
3. 运行这两个测试：

```bash
cargo test -p crossbeam-epoch --lib deferred::tests
```

**需要观察的现象**：两个测试都通过；无论是内联还是装箱，`d.call()` 之后 `fired` 都变为 `true`，说明两条路径行为一致。

**预期结果**：`on_stack`、`on_heap`、`string`、`boxed_slice_i32`、`long_slice_usize` 全部通过。

**待本地验证**：若无法运行，直接对照 `Deferred::new`（[deferred.rs:44-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L44-L82)）手工判断：`string`（一个 `String`，3 字：ptr/len/cap）正好内联；`long_slice_usize`（`[usize;5]`，5 字）超出 3 字，装箱。

#### 4.3.5 小练习与答案

**练习 1**：一个捕获单个 `Shared<'g, T>` 的销毁闭包（即 `defer_destroy` 内部生成的 `move || ptr.into_owned()`），会内联还是装箱？
**答案**：内联。`Shared` 是单个机器字的指针（带标签，详见 u5-l2），闭包只捕获它一个字段，远小于 3 字，命中内联分支。这正是 `DATA_WORDS = 3`「能放下一个函数指针 + 一个胖指针」的设计初衷，使最常见的销毁路径零堆分配。

**练习 2**：为什么 `epoch::pin()` 在「线程正在退出、TLS 句柄已被析构」时仍能安全返回一个 `Guard`，而不是 panic？
**答案**：`with_handle` 用 `HANDLE.try_with(...).unwrap_or_else(|_| collector().register())`，当 TLS 访问失败时临时新建一个参与者来完成 pin（见 [default.rs:57-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L57-L65)）。这保证了 `pin()` 永不 panic，对「在析构链里调用 pin」的场景至关重要。

---

## 5. 综合实践

把本讲三个模块串起来，实现一个**极简的「延迟释放」演示**，完整体现「pin 拿凭证 → defer_destroy 入袋 → 不立即释放 → 推进 epoch 后才释放」全链路。

**任务**：基于 `default_collector()`，构造一个带 `Drop` 计数的类型，在 pin 期间用 `defer_destroy` 销毁若干实例，观察销毁发生在「何时」。

```rust
// 示例代码：综合实践
use std::sync::atomic::{AtomicUsize, Ordering};
use crossbeam_epoch::{self as epoch, Owned};

static DROPS: AtomicUsize = AtomicUsize::new(0);

struct Node(usize); // 带编号，便于想象
impl Drop for Node {
    fn drop(&mut self) {
        DROPS.fetch_add(1, Ordering::Relaxed);
    }
}

fn main() {
    let collector = epoch::default_collector();
    let handle = collector.register();
    const N: usize = 500;

    // 1) pin 拿到 Guard（4.1）：首次 pin 会发布 epoch + SeqCst fence
    // 2) defer_destroy 入袋（4.2）：闭包经 Deferred 内联存放（4.3）
    unsafe {
        let guard = &handle.pin();
        for i in 0..N {
            let p = Owned::new(Node(i)).into_shared(guard);
            guard.defer_destroy(p);
        }
        println!("入袋完成，已销毁数量 = {}", DROPS.load(Ordering::Relaxed));
        // 预期：0 或很小（袋子可能还没满、epoch 还没推进）
    }

    // 3) 反复 pin 驱动 epoch 推进 + 周期 collect，直到宽限期满足
    //    （pin 每 128 次内部会 collect 一次）
    let mut rounds = 0;
    while DROPS.load(Ordering::Relaxed) < N {
        let _g = &handle.pin();
        rounds += 1;
    }
    println!("全部销毁，共用了约 {} 次 pin 周期", rounds);
    println!("最终 DROPS = {}", DROPS.load(Ordering::Relaxed));
}
```

**完成后请回答**（对照源码自检）：

1. 第一次 `handle.pin()` 与循环里的 `_g = &handle.pin()`，分别处于 `guard_count` 的哪种边界？哪一次才真正写 epoch + fence？
   *提示：见 [internal.rs:402-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L402-L462)；循环里每次 `_g` 都让计数经历 0→1→0，因此每次都会发布/解除。*
2. 为什么「入袋完成」时 `DROPS` 往往远小于 `N`？
   *提示：袋子未满（< 64）就不会入全局队列，更不会推进 epoch；见 [internal.rs:382-389](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L382-L389) 与 [internal.rs:155-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L155-L162)。*
3. 把第 1 步的 `defer_destroy` 换成在 `epoch::unprotected()` 上调用，行为会怎样变化？
   *提示：假守卫 `local` 为 null，闭包立即执行，见 [guard.rs:189-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L189-L200)。*

**待本地验证**：受 `global` 字段可见性限制，本实践用「反复 pin」代替显式 `collect`；如需更精确控制，请在 `crossbeam-epoch` crate 内部参考 `count_drops` 测试 [collector.rs:285-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L285-L315) 用 `collector.global.collect(guard)` 驱动。

---

## 6. 本讲小结

- **`Guard` 是 pin 状态的 RAII 凭证**：唯一字段是指向参与者 `Local` 的裸指针；`Drop` 即 unpin；`local` 为 `null` 时是「假守卫」，`drop` 与 `defer` 都退化为 no-op/立即执行。
- **可重入 pin 由 `guard_count: Cell<usize>` 驱动**：真正的「发布 epoch + `SeqCst` fence」只在 `0→1` 边界做一次，`1→0` 边界才真正 unpin；中间嵌套 pin/unpin 只是增减一个线程局部的 `Cell`。
- **`defer` 系列是一条调用链**：安全的 `defer`（要求 `Send + 'static`）调 `defer_unchecked`；`defer_destroy` 把 `Shared` 包成 `move || ptr.into_owned()` 也调 `defer_unchecked`。后两者 unsafe，是因为「宽限期后对象已独占、可由他线程销毁」是运行期不变量，类型系统证不出。
- **延迟销毁不是立即发生**：闭包先进线程局部袋子（满 64 个才盖章入全局队列），要等 `global_epoch - bag_epoch >= 2` 才被 `collect` 销毁。
- **`Deferred` 用类型擦除统一闭包**：「函数指针 + 3 字缓冲」，小闭包内联、大闭包装箱；最常见的「销毁一个 `Shared`」闭包正好内联，零堆分配。
- **全局 `epoch::pin()` 走单例收集器 + 线程局部句柄**：`OnceLock<Collector>` 懒初始化，`thread_local HANDLE` 自动注册参与者，`with_handle` 的 `try_with` 兜底保证「即便在线程析构途中调用 `pin()` 也永不 panic」。

---

## 7. 下一步学习建议

本讲只用到 `Local` 的 `pin`/`unpin`/`defer`/`flush` 几个方法，但还没回答「参与者 `Local` 是怎么挂进全局链表的」「`handle_count` 与 `LocalHandle` 的注册/析构协议」「`pin` 时的 fence 为何能保证回收安全」。这些正是下一讲 **u5-l4「Collector 与参与者注册」** 的主题，你将看到 `Collector`（`Arc<Global>`）与 `LocalHandle` 的完整生命周期，以及 `register` 如何把 `Local` 插入全局参与者链表。

之后 **u5-l5「internal：全局状态、epoch 推进与垃圾回收」** 会补齐本讲有意留下的回收侧：`Global` 的全局队列、`try_advance` 如何判定「所有 pin 者都已离开旧 epoch」、`Bag`/`SealedBag` 的两 epoch 延迟销毁为何要等 2 而非 1。建议把本讲的「defer 入袋」与 u5-l5 的「collect 出队」对照阅读，形成闭环。

若想立刻看到 `Guard`+`defer_destroy` 在真实无锁结构里的用法，可提前翻阅 **u7-l1/u7-l2（无锁跳表）**，那里会大量出现 `guard.defer_unchecked` 释放跳表节点——届时你会更深刻地理解本讲强调的「`defer_unchecked` 为何 unsafe」。
