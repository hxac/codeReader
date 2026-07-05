# repin 与 repin_after：避免阻塞 epoch 推进

> 本讲属于第 3 单元「Guard 与延迟回收」，承接 [u3-l9 Guard：pin 语义与可重入](./u3-l9-guard-and-pin.md)。
> 前置认知：`Guard` 只是一根指向 `Local` 的裸指针凭证；真正的 pin 状态（`guard_count`、`handle_count`、`epoch`）住在 `Local` 里；pin 可重入，首个 guard 才真正 pin，最后一个 guard 才真正 unpin。

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「为什么长时间持有同一个 guard 会拖慢（甚至卡死）全局 epoch 推进与垃圾回收」；
- 解释 `Guard::repin` / `Local::repin` 的工作原理，特别是 `guard_count == 1` 这个约束从何而来、为什么不需要 `SeqCst` 屏障；
- 解释 `Guard::repin_after` 如何「临时完全 unpin」去执行一个可能阻塞的长任务，再安全地 re-pin；
- 读懂 `repin_after` 内部用 `ScopeGuard` 实现 panic 安全的设计；
- 能在自己的代码里判断该用 `repin` 还是 `repin_after`，并能写出一个最小可运行实验来观察它们对回收进度的影响。

## 2. 前置知识

在进入正题前，先用三句话复习三个关键事实（细节都在前置讲义里，这里只做唤醒）：

1. **epoch 是怎么编码的。** `Epoch` 内部就是一个整数，最低位（LSB）是「pinned」标志，其余位是单调（回绕）递增的「纪元号」。`successor` 每次让整数 `+2`，相当于纪元号 `+1`；`pinned()` 把最低位置 1，`unpinned()` 把最低位清 0。详见 [src/epoch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs)。

2. **宽限期（grace period）是怎么算的。** 一个垃圾 bag 在入队时会被盖一个「当时全局 epoch」的戳。判定它能否被安全回收的条件是：

   \[
   \text{global\_epoch.wrapping\_sub}(\text{sealed\_epoch}) \ge 2
   \]

   也就是说，全局 epoch 相对盖戳时至少前进了 2 步（`successor` 两次），期间没有任何 participant 还握着指向该垃圾的指针。这正是 [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) 中 `SealedBag::is_expired` 的判据。

3. **全局 epoch 想前进，需要所有「被 pin 的参与者」都钉在当前 epoch。** `Global::try_advance` 会遍历所有 `Local`，只要有一个被 pin 的 participant 的 `local_epoch.unpinned() != global_epoch`，就立即放弃推进。这是本讲一切问题的根源。

如果你对上面任意一条感到陌生，建议先回头读 [u3-l9](./u3-l9-guard-and-pin.md) 和 [u3-l10](./u3-l10-defer-and-defer-destroy.md)。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们各自承担不同角色：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | 公开的 `Guard` 类型，是「线程已 pin」的轻量凭证 | `Guard::repin`、`Guard::repin_after` 两个公开方法，以及 `repin_after` 内部的 `ScopeGuard` |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 私有的 `Local`（参与者）与 `Global`（全局数据） | `Local::repin`、`Local::pin`、`Local::unpin`、`acquire_handle`/`release_handle`、`Global::try_advance` |

另外会顺带引用：

- [src/epoch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs)：`Epoch` 的位编码。
- [src/collector.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs) 中的 `drop_array` 测试：项目里「在循环里调 `guard.repin()`」的活样本。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 4.1 问题背景：长持有旧 epoch 如何卡住 GC；
2. 4.2 `repin`：原地刷新 local epoch；
3. 4.3 `repin_after`：临时 unpin 执行长任务；
4. 4.4 `ScopeGuard`：`repin_after` 的 panic 安全保证。

### 4.1 问题背景：长持有旧 epoch 如何卡住 GC

#### 4.1.1 概念说明

回顾 [u3-l9](./u3-l9-guard-and-pin.md)：当线程第一次 `pin()` 时，它会读取当前全局 epoch，把它（置上 pinned 位）写进自己的 `Local::epoch`，并打一道 `SeqCst` 屏障。**此后只要这个线程还 pin 着，它的 `local_epoch` 就不会再变。**

这本身没问题——pin 的语义就是「我正在安全地读共享数据，请别在我读的时候回收」。但问题出在「pin 得太久」：

- 你在 epoch `e` 时 pin，`local_epoch` 锁在 `e`。
- 全局 epoch 被 `try_advance` 推进到 `e+1`。
- 此时 `try_advance` 再想推进到 `e+2`，会遍历到你：`local_epoch.is_pinned() == true` 且 `local_epoch.unpinned() == e != e+1 (global)`，于是**放弃推进**。
- 于是全局 epoch 永远卡在 `e+1`，直到你 unpin。

而宽限期要求「盖戳后全局前进满 2 步」。你一直 pin 着，全局就前进不了第 2 步，于是**你在 pin 期间产生的垃圾永远不会被回收**。这是 EBR（epoch-based reclamation）最典型的「长 pin 拖慢 GC」问题。

> 注意：这只影响「回收进度」，不影响正确性。对象不会被提前释放，只是迟迟不释放。但在长期运行的服务里，这会导致内存占用持续上涨。

#### 4.1.2 核心流程

把上面的因果链画成时序（纪元号用 `e`、`e+1`、`e+2` 表示，省略 pinned 位）：

```
时刻   全局 epoch   你的 local_epoch   你的 guard_count   能否 try_advance？
 T0      e            e (pinned)           1               — (你刚 pin)
 T1      e            e (pinned)           1               能 → 推进到 e+1
 T2      e+1          e (pinned)           1               否！local(e) != global(e+1) → 卡住
 T3      e+1          e (pinned)           1               否（依然卡）
 ...（只要你一直持有这个 guard，全局就停在 e+1）
```

关键不变量来自 `Global::try_advance`：

> 全局 epoch 只能在「所有当前被 pin 的 participant 都钉在当前全局 epoch」时前进。

#### 4.1.3 源码精读

判定逻辑在 `Global::try_advance` 里：

[src/internal.rs:257-264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L257-L264)：遍历每个 `Local`，若发现一个被 pin 且其 epoch 与当前全局 epoch 不一致的 participant，就立即返回、放弃推进。中文说明：**这就是「长 pin 卡住全局推进」的代码出处**——只要你的 `local_epoch` 落后于全局，全局就别想再前进。

[src/epoch.rs:82-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L82-L86)：`successor` 让整数 `+2`，即纪元号 `+1`。中文说明：全局 epoch 每次前进只走「一步」，所以宽限期需要它走「两步」。

[src/internal.rs:157-161](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L157-L161)：`SealedBag::is_expired` 用 `global_epoch.wrapping_sub(self.epoch) >= 2` 判定。中文说明：垃圾必须等到全局前进满 2 步才能回收——而长 pin 正好挡住了这第 2 步。

#### 4.1.4 代码实践

**目标：** 用一个独立的 `Collector`（单线程、无其他人推进 epoch）亲眼看到「一直持有同一个 guard，垃圾迟迟不回收」。

**操作步骤（示例代码，非项目原有代码）：**

```rust
// 示例代码：演示「长持有 guard 卡住回收」
use crossbeam_epoch::{self as epoch, Collector};
use std::sync::atomic::{AtomicUsize, Ordering};

static DROPS: AtomicUsize = AtomicUsize::new(0);

struct Beep;
impl Drop for Beep {
    fn drop(&mut self) { DROPS.fetch_add(1, Ordering::Relaxed); }
}

fn main() {
    let collector = Collector::new();
    let handle = collector.register();
    let guard = handle.pin();                 // 在某个 epoch e 上 pin

    let a = epoch::Atomic::new(Beep);
    // 产生一个垃圾：把旧值换出来并 defer_destroy
    let old = a.swap(epoch::Owned::new(Beep).into_shared(&guard), Ordering::SeqCst, &guard);
    unsafe { guard.defer_destroy(old); }
    guard.flush();                            // 把本地 bag 推入全局队列并尝试 collect

    // 持有同一个 guard 反复 flush，但不 repin、也不 drop guard
    for i in 0..200_000 {
        guard.flush();
        if DROPS.load(Ordering::Relaxed) >= 1 {
            println!("在第 {} 次 flush 时回收了垃圾", i);
            break;
        }
    }
    println!("循环结束时 DROPS = {}", DROPS.load(Ordering::Relaxed));

    drop(guard);                              // 直到这里 unpin，后续才可能回收
    // 观察现象后回收剩余对象，避免泄漏
    unsafe { drop(a.into_owned()); }
}
```

**需要观察的现象：** 循环里 `DROPS` 一直是 0（直到 `drop(guard)` 之前都不回收）。

**预期结果：** 在独立 `Collector` + 单线程下，循环跑完 `DROPS` 仍为 0。原因正是 4.1.2 的时序：你的 `local_epoch` 锁在 `e`，全局只能前进到 `e+1` 就被你卡住，宽限期（需要 `e+2`）永远到不了。**待本地验证**（具体迭代次数与平台有关，但「循环内 DROPS 保持 0」这一结论是确定的）。

#### 4.1.5 小练习与答案

**练习 1：** 如果把上面例子改成「每个线程只 pin 一小会就 drop guard」，还会卡住吗？为什么？

**参考答案：** 不会。`drop(guard)` 触发 `Local::unpin`，当 `guard_count` 归零时会把 `local_epoch` 重置为 `starting()`（unpinned）。此时该 participant 不再被 pin，`try_advance` 不会再被它阻挡，全局 epoch 可以继续前进到 `e+2`，垃圾就能被回收。

**练习 2：** 为什么「长 pin 卡住 GC」只影响回收进度、不影响正确性？

**参考答案：** 正确性依赖于「宽限期未到不回收」，这条永远不会被违反；卡住只是让宽限期迟迟到不了，对象只是「该回收而没回收」，不会「不该回收而回收」。

---

### 4.2 `repin`：原地刷新 local epoch

#### 4.2.1 概念说明

现在问题清楚了：我们想在「不得不长时间持有 guard」的场景下（比如一个大循环里反复 `load` 同一个 `Atomic`），既不破坏 pin 的安全性，又能让全局 epoch 继续前进、让自己的垃圾被回收。

最朴素的想法是「先 unpin 再 pin」：

```text
drop(guard);   // unpin
let guard = epoch::pin();   // 重新 pin，拿到新的（可能更新的）epoch
```

但这有两个代价：①真正 unpin 会清空 `local_epoch`、并在 `handle_count==0` 时触发 `finalize`（销毁 participant），开销大；②在 unpin 与 re-pin 之间存在一个「完全未保护」的窗口，期间不能安全地解引用任何 `Shared`。

`Guard::repin` 提供了一个更轻量的等价操作：**「在保持 pin 的前提下，把 `local_epoch` 原地刷新到当前全局 epoch」**。它语义上等价于 unpin+pin，但实现上从不真正 unpin——participant 始终保持 pinned 状态，只是把钉住的纪元号挪到了「现在」。

#### 4.2.2 核心流程

`repin` 的核心流程（伪代码）：

```text
Guard::repin(&mut self):
    若是 unprotected guard（local 为 null）→ 直接返回（no-op）
    否则 → Local::repin()

Local::repin(&self):
    guard_count = 当前 guard 数
    若 guard_count != 1 → 什么都不做（见 4.2.3 解释）
    否则：
        old = local_epoch
        new = 全局 epoch 的 pinned 版本
        若 old != new：
            用 Release 顺序把 local_epoch 存为 new
            （注意：之后不再打 SeqCst 屏障）
```

为什么 `guard_count == 1` 才真正刷新？因为 `repin` 的前提是「这是当前线程唯一活跃的 guard」，此时不存在别的代码还依赖旧的 epoch 快照。如果 `guard_count > 1`，说明还有其它 guard（以及由它们派生的 `Shared<'g, _>`）正在使用旧 epoch 的快照，贸然刷新会破坏它们的安全假设——所以干脆不动，降级为 no-op。

#### 4.2.3 源码精读

公开入口 [src/guard.rs:329-333](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L329-L333)：`Guard::repin` 只是把活儿转给 `Local::repin`，并对 unprotected guard 做了 no-op 短路。中文说明：**注意签名是 `&mut self`**——这是本讲的一个关键设计，下面专门讲。

真正干活的是 [src/internal.rs:482-502](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L482-L502)：`Local::repin`。逐句解读：

- 第 484 行读 `guard_count`；第 487 行 `if guard_count == 1` 就是上面说的「唯一 guard」约束。
- 第 488-489 行分别读「自己的 local_epoch」和「全局 epoch 的 pinned 版本」。
- 第 492 行 `if epoch != global_epoch`：若二者已经相同就不用写，省一次原子 store。
- 第 495 行用 **`Release`** 顺序写入新 epoch。注释（493-500 行）解释了为什么这里**不需要**像 `pin()` 那样在 store 之后再补一道 `SeqCst` 屏障——这是本讲最微妙的一处，值得单独看。

对比 `pin()` 的屏障：[src/internal.rs:446-448](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L446-L448)：`pin()` 在写完 `local_epoch` 后必须 `SeqCst fence`，目的是阻止「后续从 `Atomic` 的 load」被重排到「写 local_epoch」之前——否则别的线程可能先看到你 load 了对象、却还没看到你 pin 了，从而错误地回收。

而 `repin` 注释（[src/internal.rs:497-499](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L497-L499)）说：这里反着的方向是安全的——「新 epoch 下的内存访问发生在『更新 local_epoch』之前」是允许的，最坏后果只是别的线程晚一点看到你的新 epoch、GC 稍微延迟。所以 `repin` 用一道 `Release` store 就够了，省掉了 `SeqCst fence` 这条相对昂贵的指令。这是一个「 correctness 与 performance 的精细权衡」。

**关于 `&mut self` 约束：** 回到 [src/guard.rs:301-308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L301-L308) 的文档：它要求「调用前后不要持有任何 guard-based 引用」，并用 `&mut self` 在编译期强制。原理是：`Atomic::load(_, &guard)` 返回的 `Shared<'g, T>` 借用了 `&'g guard`；而调用 `guard.repin()` 需要 `&mut guard`，与那个共享借用互斥。于是借用检查器强迫你先把所有 `Shared` drop 掉再 repin。文档里的 doctest（[src/guard.rs:312-328](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L312-L328)）正是用一对 `{ ... }` 作用域把 `p` 限制在 repin 之前。

#### 4.2.4 代码实践

**目标：** 复刻项目里 `drop_array` 测试的写法——在循环里调 `guard.repin()`，让卡住的回收重新跑起来。

**操作步骤：**

1. 先读 [src/collector.rs:345-381](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L345-L381) 的 `drop_array` 测试。注意它用一个 `let mut guard = handle.pin();` 跨整个循环，每轮 `guard.repin(); collector.global.collect(&guard);`。
2. 在自己的实验 crate（依赖 `crossbeam-epoch`）里写一个只能用公开 API 的等价版本：用 `guard.flush()` 替代私有的 `collect`。

```rust
// 示例代码：repin 让卡住的回收恢复
use crossbeam_epoch::{self as epoch, Collector};
use std::sync::atomic::{AtomicUsize, Ordering};

static DROPS: AtomicUsize = AtomicUsize::new(0);
struct Beep;
impl Drop for Beep { fn drop(&mut self) { DROPS.fetch_add(1, Ordering::Relaxed); } }

fn main() {
    let collector = Collector::new();
    let handle = collector.register();
    let mut guard = handle.pin();            // 注意：mut，且长持有

    let a = epoch::Atomic::new(Beep);
    let old = a.swap(epoch::Owned::new(Beep).into_shared(&guard), Ordering::SeqCst, &guard);
    unsafe { guard.defer_destroy(old); }
    guard.flush();

    for i in 0..200_000 {
        guard.repin();                      // ← 关键：原地刷新 local epoch，放行全局推进
        guard.flush();                      //   flush 内部会 collect
        if DROPS.load(Ordering::Relaxed) >= 1 {
            println!("在第 {} 轮回收了垃圾", i);
            break;
        }
    }
    println!("最终 DROPS = {}", DROPS.load(Ordering::Relaxed));
    drop(guard);
    unsafe { drop(a.into_owned()); }
}
```

**需要观察的现象：** 与 4.1.4 对照——这次循环里很快就会出现 `DROPS >= 1`。

**预期结果：** 加了 `guard.repin()` 后，垃圾会在循环内被回收（`DROPS` 变为 1）。把 4.1.4（无 repin）和本例（有 repin）放在一起跑，能直观对比出 repin 的作用。**待本地验证**（具体在第几轮回收与调度有关，但「有 repin 能在循环内回收、无 repin 不能」是确定的）。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `Local::repin` 在 `guard_count > 1` 时直接 no-op，而不是「只刷新一次」？

**参考答案：** `guard_count > 1` 意味着当前线程还有别的活跃 guard，以及由它们派生、仍在使用的 `Shared<'g, _>`。这些 `Shared` 的安全性依赖于「当前这一段 pin 期间 epoch 不变」。若 `repin` 此时刷新了 `local_epoch`，就破坏了那份仍在进行中的读取的安全前提。降级为 no-op 是唯一安全的做法。

**练习 2：** `repin` 写新 epoch 用的是 `Release`，而 `pin` 写完还要补 `SeqCst fence`。请用一句话说明这个差别为什么可以接受。

**参考答案：** `repin` 允许「新 epoch 下的访存」重排到「写 local_epoch」之前，最坏只是别的线程晚看到新 epoch、GC 略有延迟，不会破坏「不在宽限期内回收」这条正确性保证；而 `pin` 必须防止「load 对象」跑到「写 local_epoch」之前（否则可能漏掉 pin、导致 use-after-free），所以需要更强的屏障。

---

### 4.3 `repin_after`：临时 unpin 执行长任务

#### 4.3.1 概念说明

`repin` 解决的是「我不阻塞、只是 pin 得久」的场景。但有时你在 pin 期间需要做一件**会阻塞**的事：`sleep`、等 I/O、抢一把 `std::sync::Mutex`、等一个 `Condvar`……这些操作可能挂起任意长时间。

如果你举着 guard 去阻塞，整个 collector 的 epoch 推进会被你一个人卡住（别的线程全在 `try_advance` 里看到你还 pin 在旧 epoch）。正确的姿势是：**临时把 pin 放下，干完这件长活，再重新 pin**。这正是 `Guard::repin_after(f)` 的用途——它保证：

- 调用 `f` 期间线程是 unpin 的（前提：这是唯一活跃 guard），不阻塞全局推进；
- `f` 返回或 panic 后，guard 一定被重新 pin 回来；
- 调用前后不能持有任何 `Shared<'g, _>`（同样由 `&mut self` 强制）。

#### 4.3.2 核心流程

`repin_after` 的整体流程（伪代码）：

```text
Guard::repin_after(&mut self, f):
    若是 unprotected guard → 直接执行 f()，不 pin/unpin，返回结果
    否则：
        local.acquire_handle()   // 先给 handle 计数 +1，防止 unpin 时 finalize 掉 Local
        local.unpin()            // 真正 unpin（guard_count--；若归零则清空 local_epoch）
        构造 ScopeGuard(local)   // 析构时会 re-pin（即使 f panic）
        result = f()             // 期间线程处于 unpin 状态
        // 函数返回时 ScopeGuard 析构 → 重新 pin（pin 后立即 forget 掉返回的 Guard，
        //                                  再 release_handle 把计数 -1 还回去）
        return result
```

和 `repin` 的本质区别：

| 维度 | `repin` | `repin_after` |
| --- | --- | --- |
| 期间是否仍 pin | **始终 pin**，只是挪动纪元号 | **真正 unpin**，期间完全无保护 |
| 是否有「未保护窗口」 | 无 | 有（`f` 执行期间） |
| 适合的任务 | 纯计算、反复读 `Atomic` | 阻塞型任务（sleep / I/O / 锁） |
| 开销 | 一次 `Release` store | 一次完整 unpin + 一次完整 pin（含屏障） |

#### 4.3.3 源码精读

公开方法 [src/guard.rs:366-393](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L366-L393)：`Guard::repin_after`。逐段看：

1. [src/guard.rs:371-381](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L371-L381)：在函数内部定义了一个局部结构体 `ScopeGuard(*const Local)`，它的 `Drop` 负责在 `f` 之后（含 panic）把线程重新 pin 回来。这是「panic 安全」的核心，4.4 节专门讲。

2. [src/guard.rs:383-388](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L383-L388)：先 `acquire_handle()` 再 `unpin()`。**顺序不能反**，原因见下面的「为什么先 acquire_handle」。

3. [src/guard.rs:390](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L390)：在 `unpin` 之后才创建 `ScopeGuard`，保证它在 `f` 之后的析构里 re-pin。

4. [src/guard.rs:392](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L392)：执行用户函数 `f()` 并返回其结果。

**为什么必须先 `acquire_handle` 再 `unpin`？** 看 `Local::unpin` [src/internal.rs:465-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L465-L479)：当 `guard_count` 从 1 减到 0 时，它会清空 `local_epoch`，并且**若 `handle_count == 0` 就调用 `finalize`**——`finalize` 会把这个 `Local` 从全局链表里删掉并销毁（[src/internal.rs:530-569](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L530-L569)）。

设想 `repin_after` 调用前 `handle_count == 1`（这是线程刚 `register` 后的常态）。如果直接 `unpin`：`guard_count` 归零、`handle_count == 0`，于是触发 `finalize`，`Local` 被销毁——可我们一会儿还要 re-pin 它！所以必须先用 `acquire_handle()`（[src/internal.rs:505-510](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L505-L510)）把 `handle_count` 顶到 2，这样 `unpin` 时 `handle_count` 仍 `>= 1`，`finalize` 不会触发，`Local` 得以存活到 re-pin。re-pin 后再用 `release_handle`（[src/internal.rs:514-527](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L514-L527)）把多借的这个 handle 还掉。

**和 `repin` 一样的「唯一 guard」约束：** `repin_after` 内部调的 `unpin` 同样只在 `guard_count` 归零时才真正清空 epoch。如果调用时 `guard_count > 1`，`unpin` 只是减计数，epoch 仍保持 pinned，`f` 实际上仍在线程 pin 状态下执行。文档 [src/guard.rs:339-340](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L339-L340) 明确说：「只有当这是当前线程唯一活跃 guard 时，线程才会被真正 unpin」。

**unprotected 分支：** 当 `self.local` 为 null（即 `unprotected()` 假守卫）时，[src/guard.rs:383-388](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L383-L388) 的 `if let` 不进入，`ScopeGuard` 拿到 null 指针、析构时也是 no-op，于是 `f()` 被直接调用、不 pin/unpin——与文档 [src/guard.rs:342-343](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L342-L343) 的描述一致。

#### 4.3.4 代码实践

**目标：** 用 `repin_after` 在长循环里穿插一次阻塞 sleep，验证线程在 sleep 期间不阻塞全局推进。

**操作步骤（示例代码）：**

```rust
// 示例代码：repin_after 在阻塞期间放行 epoch 推进
use crossbeam_epoch::{self as epoch, Collector};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::Duration;

static DROPS: AtomicUsize = AtomicUsize::new(0);
struct Beep;
impl Drop for Beep { fn drop(&mut self) { DROPS.fetch_add(1, Ordering::Relaxed); } }

fn main() {
    let collector = Collector::new();
    let handle = collector.register();
    let mut guard = handle.pin();

    let a = epoch::Atomic::new(Beep);
    let old = a.swap(epoch::Owned::new(Beep).into_shared(&guard), Ordering::SeqCst, &guard);
    unsafe { guard.defer_destroy(old); }
    guard.flush();

    for i in 0..1000 {
        // 在阻塞之前，必须先放下所有 Shared（&mut self 已经在编译期强制）
        guard.repin_after(|| thread::sleep(Duration::from_millis(5))); // sleep 期间线程 unpin
        guard.flush();
        if DROPS.load(Ordering::Relaxed) >= 1 {
            println!("在第 {} 轮（sleep 后）回收了垃圾", i);
            break;
        }
    }
    println!("最终 DROPS = {}", DROPS.load(Ordering::Relaxed));
    drop(guard);
    unsafe { drop(a.into_owned()); }
}
```

**需要观察的现象：** 与 4.2.4（用 `repin`）对照，回收同样能在循环内发生；区别在于 `repin_after` 期间线程真的 unpin 了（可以再用一个旁路线程持续 `pin()`/`drop` 来观察全局 epoch 是否能持续推进，作为进阶验证）。

**预期结果：** 循环内 `DROPS` 能涨到 1。**待本地验证**（sleep 时长与轮数需按机器调整）。

#### 4.3.5 小练习与答案

**练习 1：** 如果在 `repin_after` 的闭包里继续使用闭包外 `load` 出来的 `Shared`，会怎样？

**参考答案：** 编译不过。`repin_after` 是 `&mut self`，而那个 `Shared<'g, _>` 借用了 `&guard`，两者互斥，借用检查器会拒绝。这正是在类型层防止「unpin 期间还解引用受保护指针」。

**练习 2：** `repin_after` 里为什么是 `acquire_handle` + `unpin` + （ScopeGuard 析构）`pin` + `release_handle`，而不是简单的 `unpin` + `pin`？

**参考答案：** 为了避免 `unpin` 在 `guard_count==0 && handle_count==0` 时触发 `finalize` 把 `Local` 销毁。先用 `acquire_handle` 垫高 `handle_count`，`unpin` 就不会 finalize；re-pin 后再用 `release_handle` 还掉垫高的那一个，恢复原计数。

---

### 4.4 `ScopeGuard`：`repin_after` 的 panic 安全

#### 4.4.1 概念说明

`repin_after` 有一个强保证：**无论 `f` 正常返回还是 panic，guard 在调用结束后一定处于 pin 状态。** 这一点至关重要，因为调用方拿到的是 `&mut Guard`，通常假定「只要这个 guard 还在，线程就是 pin 的」——如果 `f` panic 后线程意外留在 unpin 状态，后续使用 guard 的代码就会在无保护下解引用 `Shared`，构成 use-after-free。

实现这个保证的手段是一个经典的 RAII 小工具：函数内部定义的 `ScopeGuard`，它持有 `local` 指针，在析构（包括 panic 展开时）时执行 re-pin。

#### 4.4.2 核心流程

```text
struct ScopeGuard(*const Local)
impl Drop for ScopeGuard:
    若 local 非 null：
        let g = local.pin()       // 重新 pin：guard_count++，写新 epoch + 屏障
        mem::forget(g)            // 故意丢掉返回的 Guard，不让它的 Drop 再 unpin
        Local::release_handle(local)  // 把之前 acquire_handle 借的那个 handle 还掉
```

要点：

- `pin()` 会做完整的 pin 动作（[src/internal.rs:402-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L402-L462)），包括 `guard_count+1`、写 `local_epoch`、`SeqCst` 屏障。这正是 `repin` 省掉的那道屏障——`repin_after` 必须补上，因为它经历了一次真正的 unpin。
- `mem::forget(g)`：`pin()` 返回的 `Guard` 若正常 drop 会触发 `unpin`，把刚 pin 的又给撤了。这里只要它的「副作用」（计数与屏障），不要它的析构，所以 `forget` 掉。
- `release_handle`：把 `repin_after` 开头 `acquire_handle` 多借的那个 handle 还回去，计数回到调用前的状态。

#### 4.4.3 源码精读

[src/guard.rs:370-381](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L370-L381) 注释一行道破意图：「Ensure the Guard is re-pinned even if the function panics」。析构函数里：

- [src/guard.rs:375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L375)：`mem::forget(local.pin())`——调用 `Local::pin` 后立刻 forget 返回值。
- [src/guard.rs:376-378](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L376-L378)：再 `Local::release_handle(local)` 还掉 handle。

由于 `_guard`（[src/guard.rs:390](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L390)）是一个普通局部变量，无论 `f()`（[src/guard.rs:392](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L392)）是正常返回还是 panic 展开，`_guard` 都会被析构，re-pin 都会发生。这就是 panic 安全的实现所在。

#### 4.4.4 代码实践

**目标：** 验证「`f` panic 后 guard 仍处于 pin 状态」。

**操作步骤（示例代码）：**

```rust
// 示例代码：验证 repin_after 的 panic 安全
use crossbeam_epoch as epoch;

fn main() {
    let mut guard = epoch::pin();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        guard.repin_after(|| panic!("boom inside f"));
    }));
    assert!(result.is_err(), "f 应当 panic");
    // 关键断言：调用之后线程依然 pin 着
    assert!(epoch::is_pinned(), "panic 后 guard 仍应处于 pin 状态");
    println!("panic 安全验证通过：repin_after 之后 is_pinned() == true");
    drop(guard);
}
```

**需要观察的现象：** 即便 `f` panic，最后一行 `is_pinned()` 仍为 `true`。

**预期结果：** 打印「panic 安全验证通过」，断言全部成立。**待本地验证**（逻辑直接来自 `ScopeGuard` 的析构必然执行，可信度高）。

#### 4.4.5 小练习与答案

**练习 1：** 如果把 `mem::forget(local.pin())` 改成 `let _ = local.pin();`（不 forget），会发生什么？

**参考答案：** `let _ = local.pin();` 会让返回的 `Guard` 在该语句结束时立即 drop，触发 `unpin`，把刚做的 pin 撤销——于是 re-pin 失效，线程会留在 unpin 状态。所以必须 `forget` 掉这个 `Guard`，只保留它的 pin 副作用。

**练习 2：** 为什么 re-pin 走的是完整的 `Local::pin`（含 `SeqCst` 屏障），而 4.2 的 `Local::repin` 却可以省掉这道屏障？

**参考答案：** `repin_after` 经历了一次真正的 unpin（`local_epoch` 被清空成 `starting()`），re-pin 等于「从零开始一次新 pin」，必须像首次 pin 一样写 epoch + `SeqCst` 屏障，防止后续 load 被重排到 pin 之前；而 `repin` 全程保持 pinned，不存在「从 unpinned 重新进入」的窗口，允许更宽松的排序（见 4.2.3）。

---

## 5. 综合实践

把本讲四块内容串起来，完成下面这个综合任务。

**场景：** 你要写一个「周期性扫描一个共享计数器、偶尔小睡、并在摘除旧值时延迟回收」的循环。要求：

1. 用一个独立的 `Collector`（单线程即可），pin 一个长持有的 `mut guard`；
2. 在循环里反复 `load` 一个 `Atomic<Beep>`，每隔 `N` 次（例如 128）调用 `guard.repin()` 让 GC 跟上；
3. 每隔 `M` 次（例如 1024）调用 `guard.repin_after(|| thread::sleep(...))` 模拟一次阻塞 I/O，期间不阻塞 epoch；
4. 在某次迭代里 `swap` 出旧值并 `defer_destroy`，用 `DROPS` 计数验证：加了 repin/repin_after 后，旧值能在循环内被回收。

**验收要点（文字描述即可，不必真的提交代码）：**

- 解释为什么必须把 `let p = a.load(...)` 放在 `{ }` 作用域里，紧接其后再调 `repin`/`repin_after`（提示：`&mut self` 与 `Shared<'g, _>` 的借用冲突）；
- 用一句话说明：若去掉所有 `repin`/`repin_after`，只保留 `guard.flush()`，`DROPS` 在循环内会不会涨，为什么；
- 指出 `repin` 与 `repin_after` 在你这个循环里分别解决的子问题（一个不阻塞但 pin 得久；一个要阻塞睡眠）。

**参考思路：** 本质上是把 4.2.4 与 4.3.4 的两段示例合并进同一个循环，并对照 4.1.4 的「无 repin」基线。可参考项目里 [src/collector.rs:376-379](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L376-L379) 的 `drop_array` 测试——它正是「长持有 `mut guard` + 循环里 `repin` + collect」的官方写法。

## 6. 本讲小结

- **长持有 guard 会卡住 GC：** `try_advance` 要求所有被 pin 的 participant 都钉在当前全局 epoch；你的 `local_epoch` 落后，全局就前进不了第 2 步，宽限期到不了，垃圾不回收。这只影响进度、不影响正确性。
- **`Guard::repin` / `Local::repin` 是「原地刷新」：** 仅当 `guard_count == 1`（唯一活跃 guard）时，把 `local_epoch` 用 `Release` 更新到当前全局 epoch；全程保持 pinned，无未保护窗口，也省掉了 `SeqCst` 屏障（因为允许新 epoch 的访存重排到 store 之前）。
- **`&mut self` 是编译期安全闸：** 它与 `Shared<'g, _>` 对 `&guard` 的借用互斥，强制调用方在 repin 前放下所有受保护指针。
- **`Guard::repin_after` 是「临时真 unpin」：** 用 `acquire_handle` + `unpin` 放下 pin（且不误触发 `finalize`），执行完阻塞任务后由 `ScopeGuard` 析构 re-pin，适合 sleep / I/O / 锁等阻塞场景。
- **panic 安全靠 RAII：** 函数内定义的 `ScopeGuard` 在析构里 `mem::forget(local.pin())` + `release_handle`，无论 `f` 正常返回还是 panic 都保证线程被重新 pin。
- **二者都遵循「唯一 guard」前提：** 当 `guard_count > 1` 时，`repin` no-op、`repin_after` 的 `unpin` 也只减计数不清 epoch，因为还有别的 guard 依赖当前 epoch 快照。

## 7. 下一步学习建议

本讲把「Guard 这一侧」的 repin 机制讲完了，但留下了几个更深的问题留给后续单元：

- **`pin`/`unpin` 的屏障细节与 x86 hack：** 我们反复提到「`pin` 写完 epoch 后必须 `SeqCst` 屏障」，以及 x86 上用 `compare_exchange` 代替 `mfence` 的技巧。这属于 [u5-l18 pin/unpin 与内存屏障](./u5-l18-pin-unpin-memory-barriers.md)（高级篇）。
- **epoch 推进与回收主链路：** `try_advance` 如何遍历 `Local`、`collect` 如何用 `try_pop_if` 回收过期 bag、`finalize` 如何销毁 participant，详见 [u5-l19 try_advance 与 collect](./u5-l19-try-advance-and-collect.md)。
- **`Local` 的字段与计数：** 想彻底搞懂 `guard_count`/`handle_count`/`pin_count` 的来龙去脉，可读 [u4-l15 Local：参与者结构](./u4-l15-local-participant.md)。

在进入第 5 单元之前，建议先做一遍本讲「综合实践」，确保你能用公开 API 复现「无 repin 卡住、有 repin 恢复」这一对照实验——它会把本讲所有概念钉死在肌肉记忆里。
