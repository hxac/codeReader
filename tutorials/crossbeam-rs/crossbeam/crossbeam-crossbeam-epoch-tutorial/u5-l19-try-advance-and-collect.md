# try_advance 与 collect：推进 epoch 并回收垃圾

## 1. 本讲目标

本讲是「epoch 推进与回收主链路」的收尾篇。前两讲我们已经把两块拼图摆好：

- 数据表示层（u5-l17）：`Epoch` 用最低位记 pin、`successor` 每次 `+2`、`wrapping_sub` 算带符号代差。
- 屏障层（u5-l18）：`pin`/`unpin` 如何写本地 epoch、插 `SeqCst` 屏障，以及为何这能保证回收线程不会误判。

本讲把最后一块、也是把整条链路「接通」的一步讲透：**全局 epoch 何时被推进、过期垃圾何时被弹出销毁、参与者退出时如何善后**。学完后你应当能够：

1. 读懂 `Global::try_advance`，并能判断「给定一组参与者的 pin 状态，本轮能否推进」。
2. 读懂 `Global::collect`，解释为什么单次回收量被 `COLLECT_STEPS × MAX_OBJECTS` 封顶。
3. 读懂 `Local::finalize`，画出线程退出时从 `LocalHandle::drop` 到 `Local` 堆内存被释放的完整调用时序。
4. 区分三个容易混淆的名字：`Local::finalize`（inherent，逻辑退会）、`<Local as IsElement>::finalize`（trait，物理回收）、`Global::collect`（驱动回收）。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。回忆 u4-l16 给出的不变量：

> **一次 pin 至多见证一次 epoch 推进。**

这句话是整条回收链路的安全基石，它带来两个直接推论：

- **推进条件**：全局 epoch 想从 \(g\) 前进到 \(g+1\)，前提是「当前所有还 pin 着的参与者，都钉在 \(g\)」。如果有人仍钉在更老的 \(g-1\)，说明他可能还在读上一代的对象，此时推进会让他的临界区「跨代」，回收线程就可能释放他正读的对象。`try_advance` 的工作就是遍历花名册逐个检查这个条件。
- **宽限期**：一个袋（bag）在 \(g\) 被封箱入队，必须等全局前进满 2 步（到 \(g+2\)）才算「过保」。判据是 u4-l16 / u5-l17 讲过的：

\[ \text{global\_epoch.wrapping\_sub}(\text{sealed\_epoch}) \ge 2 \]

为什么是 2 而不是 1？因为封箱时可能正好有线程在 \(g\) 的临界区里（他见证了这次推进），再多等一步就能保证他的 guard 必然 drop。这个「两步窗口」正是 `SealedBag::is_expired` 的判据。

最后回忆三个角色的分工（u4-l16）：

| 字段 | 类型 | 职责 |
|---|---|---|
| `Global::locals` | 侵入式链表 `List<Local>` | 参与者花名册，决定 `try_advance` 能否推进 |
| `Global::epoch` | `CachePadded<AtomicEpoch>` | 全局时钟，给袋盖戳 |
| `Global::queue` | `Queue<SealedBag>` | 存放已封箱、等回收的袋，由 `collect` 弹出 |

本讲就是把这三者串成一条会「自己往前走」的流水线。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|---|---|
| `src/internal.rs` | `Global::try_advance`、`Global::collect`、`Local::finalize`、`<Local as IsElement>::finalize`、`SealedBag::is_expired`、`Bag::Drop`、常量 `COLLECT_STEPS` / `MAX_OBJECTS` |
| `src/sync/queue.rs` | `Queue::try_pop_if` / `pop_if_internal`：基于条件的弹出，是 `collect` 回收袋的引擎 |
| `src/sync/list.rs` | `Entry::delete`（逻辑删除）、`Iter::next`（物理摘除 + 触发 `IsElement::finalize`）、`IterError::Stalled` |
| `src/collector.rs` | `incremental` / `buffering` 测试，是本讲代码实践的对照模板；`LocalHandle::drop` 是 finalize 时序的起点 |
| `src/epoch.rs` | `successor`（`+2`）、`wrapping_sub`（带符号代差），编码基础（u5-l17 已讲透，本讲直接用） |

---

## 4. 核心概念与源码讲解

本讲拆三个最小模块，恰好对应回收链路的三个阶段：**判定能否推进 → 弹出过期袋销毁 → 参与者退会善后**。

### 4.1 try_advance：判定能否推进全局时钟

#### 4.1.1 概念说明

`Global::try_advance` 回答一个问题：**「现在能不能把全局 epoch 往前推一步？」**

它不是无脑 `+1`。回忆前置知识里的推进条件——必须确认所有「还 pin 着」的参与者都钉在当前全局 epoch。只要有一个钉在更老的 epoch，本轮就不能推进。注意：

- **不推进只影响进度（liveness），不影响正确性（safety）**。最坏情况是垃圾迟迟不被回收，绝不会释放正在被读的对象。
- `try_advance` 每次至多推进 **一步**（`successor`，代差 `+1`）。它不会被一次调用推很多步。

`try_advance` 被 `collect` 调用，而 `collect` 又被两条路径触发：`pin` 每 128 次的周期性回收（u5-l18）、以及用户主动 `flush`。所以「推进时钟」这件事是**懒驱动、摊销式**的——没有 pin/collect 就不前进。

#### 4.1.2 核心流程

用伪代码描述 `try_advance` 的控制流：

```
fn try_advance(guard) -> Epoch:
    global_epoch = self.epoch.load(Relaxed)
    fence(SeqCst)                          # 与 pin 里的 SeqCst 屏障配对，保证“公告在读之前”

    for local in self.locals.iter(guard):  # 遍历参与者花名册
        match local:
            Err(Stalled):                  # 并发线程正在改链表，没能遍历完
                return global_epoch        #   本回合放弃推进，把任务让给那个线程
            Ok(local):
                local_epoch = local.epoch.load(Relaxed)
                if local_epoch.is_pinned() and local_epoch.unpinned() != global_epoch:
                    return global_epoch    #   有人钉在老 epoch 的临界区，不能推进

    fence(Acquire)                         # 把“已检查全部参与者”这一事实定序
    new_epoch = global_epoch.successor()   #   +2（原始数据），即代差 +1
    self.epoch.store(new_epoch, Release)   #   发布新 epoch
    return new_epoch
```

两个 early-return 是关键：遇到 `Stalled` 直接返回，遇到「钉在老 epoch 的参与者」也直接返回。只有把花名册**完整**遍历完且无人挡路，才执行 `successor` + `store`。

#### 4.1.3 源码精读

先看 `try_advance` 的整体结构与起始屏障：

[crossbeam-epoch/src/internal.rs:228-288](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L228-L288) — `try_advance` 的文档注释与完整实现。函数标了 `#[cold]`（L236），因为它很少真正推进成功，编译器应把它放到冷路径。

起始处先 `Relaxed` 读全局 epoch，再插 `SeqCst` 屏障：

```rust
let global_epoch = self.epoch.load(Ordering::Relaxed);
atomic::fence(Ordering::SeqCst);
```

这道 `SeqCst` 屏障与 `pin` 里写完本地 epoch 后的 `SeqCst` 屏障（u5-l18）配对。`SeqCst` 屏障在所有线程间构成一个**全局总序**：于是「某线程已发布 pin」与「回收线程读取它的 local_epoch」二者必有明确的先后——要么 pin 先于我们的读（我们看得到它钉在当前 epoch，可推进），要么我们的屏障先于它的 pin（它的后续 `Atomic::load` 必发生在我们推进之后，安全）。这正是 u5-l18 不变量 A「公告在读之前」在回收侧的对偶。

接着是遍历花名册的两条 early-return 分支：

[crossbeam-epoch/src/internal.rs:249-270](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L249-L270) — 遍历 `self.locals.iter(guard)`。

`Stalled` 分支：

```rust
Err(IterError::Stalled) => {
    // A concurrent thread stalled this iteration. ...
    return global_epoch;
}
```

`IterError::Stalled` 的语义见 [crossbeam-epoch/src/sync/list.rs:126-132](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L126-L132)：迭代器在并发修改链表时「打滑」，需要从头重启。`try_advance` 选择直接返回、不重启——因为那个并发线程很可能自己也在 `try_advance`，把推进交给它即可。

「钉在老 epoch」分支：

```rust
Ok(local) => {
    let local_epoch = local.epoch.load(Ordering::Relaxed);
    // If the participant was pinned in a different epoch, we cannot advance...
    if local_epoch.is_pinned() && local_epoch.unpinned() != global_epoch {
        return global_epoch;
    }
    ...
}
```

注意判据是 `is_pinned() && unpinned() != global_epoch`：

- 若参与者**没 pin**（`is_pinned()` 假），无论它的 epoch 字段是什么老值，都不挡路——它没在临界区。
- 若参与者**pin 在当前 global_epoch**，也不挡路。
- 只有「pin 在一个更老的 epoch」才挡路。

`unpinned()` 把 LSB 清零后再比较，对应 u5-l17 的编码约定（持 guard 期间本地 epoch 存「已 pin」奇数；比代号前先抹掉 LSB）。

最后是推进与发布：

[crossbeam-epoch/src/internal.rs:271-288](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L271-L288) — 末尾屏障与推进。

```rust
#[cfg(not(crossbeam_sanitize_thread))]
atomic::fence(Ordering::Acquire);

let new_epoch = global_epoch.successor();
self.epoch.store(new_epoch, Ordering::Release);
new_epoch
```

末尾 `Acquire` 屏障 + `Release` 的 `store`：把「我已确认所有参与者都在当前 epoch」这件事**发布**出去，与后续线程 `pin` 时读全局 epoch 配对。注释（L278-285）还点出一个细节：即使别的线程已经抢先推进过，我们这次 `store` 也只是写入「同一个值」，不会跳步——因为当前线程 pin 在 `global_epoch`，全局 epoch 不可能超前它两步。

> **关于 `crossbeam_sanitize_thread` 分支**：ThreadSanitizer 不理解屏障，源码用「把所有 local 收集进 `Vec` 再对每个做 `Acquire` load」来模拟等效效果（L244-274）。这只是为 sanitizer 不报假阳性，正常编译走 `Acquire` 屏障分支。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用「代差」语言预测 `try_advance` 的行为。

**操作步骤**：

1. 读 [crossbeam-epoch/src/collector.rs:190-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L190-L213) 的 `pin_holds_advance` 测试。它起 8 个线程，每个线程循环：pin → 记 `before` → `collect` → 记 `after`，并断言 `after.wrapping_sub(before) <= 2`。
2. 思考：`wrapping_sub` 返回的是「代差」（u5-l17：原始数据 `+2` 对应代差 `+1`）。单线程一次 `collect` 至多推进一步（代差 `+1`）。为什么测试允许 `<= 2`（即至多两步）而不是 `<= 1`？
3. 给出你的解释后，对照下方答案核对。

**需要观察的现象 / 预期结果**：

- `before` 与 `after` 之间隔着一次 `collect`（本线程），但**也隔着其他 7 个线程可能发起的 `collect`**。当前线程虽然在 `collect` 调用期间持有 guard，但在循环开头 `pin` 之前 / 上一轮 guard drop 之后存在「无人 pin」的窗口，别的线程可趁机推进。所以两步是合理上界。核心点不变：**一次 `try_advance` 本身只推一步**，多出来的步数来自并发线程。

> 说明：本测试访问了 `collector.global`（`pub(crate)`），只能作为 crate 内测试存在，外部用户无法直接运行；这里做源码阅读理解即可。

#### 4.1.5 小练习与答案

**Q1**：全局 epoch 为 \(g\)。线程 A 刚 `pin`（本地存 `pinned(g)`）。线程 B 调用 `try_advance`。B 能推进到 `successor(g)` 吗？

**A1**：能。遍历到 A 时，`local_epoch.is_pinned()` 为真，但 `unpinned() == g == global_epoch`，不满足 `unpinned() != global_epoch`，不触发 early-return。所有参与者都在当前 epoch，满足推进条件。

**Q2**：接上，B 推进到 \(g+1\) 后，A 仍未 unpin（仍 `pinned(g)`）。线程 C 此时调用 `try_advance`，能再推进到 \(g+2\) 吗？

**A2**：不能。C 读到 A 的 `is_pinned()` 为真且 `unpinned() == g != g+1`（当前 global），命中 early-return，原样返回 \(g+1\)。这正是「一次 pin 至多见证一次推进」——A 在 \(g\) 的临界区挡住了第二次推进，直到 A `unpin`/`repin`。

**Q3**：遍历遇到 `IterError::Stalled` 时，为什么 `try_advance` 选择直接 `return` 而不重试？

**A3**：`Stalled` 表示有并发线程正在改链表（如物理摘除已删除节点）。重试成本高，且那个并发线程很可能自己也在 `try_advance`；把推进任务交给它即可。放弃推进只损失进度（liveness），不损失正确性（safety）。

---

### 4.2 collect：try_advance 之后增量回收过期袋

#### 4.2.1 概念说明

`Global::collect` 做两件事，严格按顺序：

1. 调一次 `try_advance(guard)`——**先尝试推进时钟**，因为只有推进了，更多袋才会「过保」。
2. 用 `Queue::try_pop_if` 从全局队列里**弹出至多 `COLLECT_STEPS` 个已过期的袋**，逐个 `drop`，触发 `Bag::drop` 执行其中全部延迟闭包。

关键词是**增量**（incremental）。一次 `collect` 不会把队列清空，而是只回收一小批（`COLLECT_STEPS = 8` 个袋）。这样设计的目的是**摊销**：把回收工作打散到很多次 `collect` 里，避免一次 `collect` 卡住太久。代价是：即便队列里堆了上千个袋，一次 `collect` 也只消化 8 个；要全部回收需要多次 `collect`（即多次 `pin`/`flush`）。

#### 4.2.2 核心流程

```
fn collect(guard):
    global_epoch = self.try_advance(guard)         # 1. 先推进时钟
    steps = COLLECT_STEPS                          #    = 8（sanitize 下 usize::MAX）
    for _ in 0..steps:                             # 2. 增量回收
        match self.queue.try_pop_if(
            |sealed_bag| sealed_bag.is_expired(global_epoch),  # 过保判定
            guard,
        ):
            None    => break                       #    队空 或 队首未过保 → 停
            Some(b) => drop(b)                     #    drop 触发 Bag::drop → 执行全部延迟闭包
```

注意 `try_pop_if` 的条件闭包：它对**队首**元素做 `is_expired` 判定。Michael-Scott 队列是 FIFO，袋按封箱时的 epoch 单调进入；但 epoch 会回绕，所以「过保」仍须逐袋用 `wrapping_sub >= 2` 判定，而非简单比较大小。一旦队首袋未过保，`try_pop_if` 返回 `None`，`collect` 立即 `break`（更老的袋先入队，过保顺序大致与入队一致，可安全停手）。

#### 4.2.3 源码精读

`collect` 主体：

[crossbeam-epoch/src/internal.rs:200-226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L200-L226) — `Global::collect` 的文档注释与实现。同样标了 `#[cold]`（L207），因为 `pin()` 极少真正走到周期性 `collect`，编译器应优化「不调用 collect」的分支。

```rust
pub(crate) fn collect(&self, guard: &Guard) {
    let global_epoch = self.try_advance(guard);

    let steps = if cfg!(crossbeam_sanitize) {
        usize::MAX
    } else {
        Self::COLLECT_STEPS            // = 8
    };

    for _ in 0..steps {
        match self.queue.try_pop_if(
            |sealed_bag: &SealedBag| sealed_bag.is_expired(global_epoch),
            guard,
        ) {
            None => break,
            Some(sealed_bag) => drop(sealed_bag),
        }
    }
}
```

常量定义紧邻：

[crossbeam-epoch/src/internal.rs:177-178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L177-L178) — `const COLLECT_STEPS: usize = 8;`

[crossbeam-epoch/src/internal.rs:64-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L64-L69) — `MAX_OBJECTS`：正常 64，`crossbeam_sanitize`/`miri` 下为 4（让数据竞争更容易暴露）。

> 顺带一提：在 `cfg!(crossbeam_sanitize)` 下 `steps = usize::MAX`，看似会「一直回收到空」。但循环里有 `None => break` 兜底——一旦队首袋未过保或队列空就停。这与 `MAX_OBJECTS` 调小到 4 配合，让 sanitizer 跑得更快又能覆盖回收路径。

回收发生的地方是 `drop(sealed_bag)`，它触发 `Bag::drop`：

[crossbeam-epoch/src/internal.rs:125-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L125-L134) — `Bag::drop` 逐个调用袋内延迟闭包（u3-l11 详述过 `Deferred` 的内联/装箱存储与 `call`）。注意它用 `mem::replace(..., NO_OP)` 把已执行的槽替换成 `NO_OP` 哨兵，保证 panic 安全下每条闭包至多执行一次。

最后看「弹出引擎」`try_pop_if`：

[crossbeam-epoch/src/sync/queue.rs:188-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L188-L202) — `Queue::try_pop_if`：循环调用内部的 `pop_if_internal` 直到成功或观察到队空/不满足条件。

其内部实现：

[crossbeam-epoch/src/sync/queue.rs:148-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L148-L175) — `pop_if_internal`：`Acquire` 读 head → 读 next → 对 next 的数据跑条件闭包 → CAS 推进 head → `guard.defer_destroy(head)` 回收旧 head 节点。

```rust
Some(n) if condition(unsafe { &*n.data.as_ptr() }) => unsafe {
    self.head
        .compare_exchange(head, next, Release, Relaxed, guard)
        .map(|_| { ... guard.defer_destroy(head); Some(n.data.assume_init_read()) })
        .map_err(|_| ())
},
None | Some(_) => Ok(None),     // 队空 或 条件不满足
```

两个要点：① `SealedBag` 要求 `T: Sync`（`SealedBag` 自己 `unsafe impl Sync`，见 internal.rs L153），因为 `is_expired` 会被任意回收线程读；② 弹出的 `SealedBag` 一旦被 `collect` 拿到 `drop`，就由本线程独占执行其闭包，无并发。

#### 4.2.4 代码实践（源码阅读型，对应指定实践任务）

**实践目标**：解释 `incremental` 测试中 `assert!(curr - last <= 1024)` 的来历，算出单次 `collect` 的紧上界。

**操作步骤**：

1. 读 [crossbeam-epoch/src/collector.rs:215-248](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L215-L248) 的 `incremental` 测试。它的循环体是：

   ```rust
   let curr = DESTROYS.load(Ordering::Relaxed);
   assert!(curr - last <= 1024);
   last = curr;
   let guard = &handle.pin();
   collector.global.collect(guard);
   ```

   即：两次读 `DESTROYS` 之间隔着**恰好一次** `collect`（单线程）。

2. 推算单次 `collect` 至多执行多少条延迟闭包。提示：`collect` 至多弹 `COLLECT_STEPS` 个袋，每袋至多装 `MAX_OBJECTS` 条闭包。

**预期结果 / 推算**：

单次 `collect` 的紧上界是

\[
\text{COLLECT\_STEPS} \times \text{MAX\_OBJECTS} = 8 \times 64 = 512
\]

条延迟闭包。`incremental` 里每条闭包恰好 `fetch_add(1)` 一次，所以单次 `collect` 至多让 `DESTROYS` 增加 512。测试断言却写 `<= 1024`，这是作者刻意取的 **\(2\times\) 余量**：它对紧上界 512 留出一倍裕度，既吸收未来 `COLLECT_STEPS`/`MAX_OBJECTS` 微调，也吸收测量噪声。

> 关键洞察不是「1024」这个数字，而是 **「每次 `collect` 回收量被两个常量的乘积封顶」** 这件事本身。这保证即便队列里积压了成千上万个袋，单次 `collect` 的最坏执行时间也是常数级（摊销），不会一次性卡住线程。

**补充对照**：再看 [crossbeam-epoch/src/collector.rs:250-282](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L250-L282) 的 `buffering` 测试。它先 defer 10 个对象但**不 flush**（垃圾还在线程本地袋里，没入全局队列），然后连续 `collect` 十万次也回收不了它们——`assert!(DESTROYS < COUNT)`。直到 `handle.pin().flush()` 把本地袋 push 进队列，后续 `collect` 才开始回收。这印证了 u3-l10 的结论：**`defer` 只入本地袋，必须 `flush`/满袋 `push_bag` 入队后，`collect` 才看得到**。

#### 4.2.5 小练习与答案

**Q1**：非 sanitizer/miri 配置下，单次 `collect` 至多回收多少条延迟闭包？

**A1**：\(8 \times 64 = 512\) 条（`COLLECT_STEPS × MAX_OBJECTS`）。

**Q2**：`incremental` 为什么断言 `curr - last <= 1024` 而不是紧上界 512？

**A2**：512 是紧上界；1024 是 \(2\times\) 余量，吸收常量未来调整与测量噪声。本质保证是「单次 `collect` 回收量有常数上界」。

**Q3**：`collect` 为什么标 `#[cold]`？`try_pop_if` 的条件闭包起什么作用？

**A3**：`#[cold]` 引导编译器把 `collect` 调用放到冷路径，保护 `pin` 热路径（`pin` 每 128 次才调一次）。条件闭包 `|bag| bag.is_expired(global_epoch)` 让队列**只在队首袋过保时**弹出；队首未过保即返回 `None`，`collect` 随即 `break`。

---

### 4.3 Local::finalize：参与者退会与销毁

#### 4.3.1 概念说明

当一个线程在某 `Collector` 中的所有 guard 和所有 handle 都释放完毕（`guard_count == 0` 且 `handle_count == 0`），它就「退会」了。`Local::finalize`（inherent 方法）负责退会的善后工作，做四件事：

1. 把线程本地袋里**残留**的垃圾封箱入队（不能丢）。
2. 把持有的 `Arc<Global>` 引用取走并 drop（可能让 `Global` 引用计数归零）。
3. 在侵入式链表里把这个 `Local` **逻辑删除**（`entry.delete`，打标记）。
4. 之后的物理回收由**别的线程**的 `Iter` 完成。

注意区分**两个 `finalize`**（u4-l15 已点出，本讲给完整时序）：

| 名字 | 定义位置 | 调用者 | 作用 |
|---|---|---|---|
| `Local::finalize`（inherent） | internal.rs L530 | `unpin` / `release_handle` | 逻辑退会：入队残留袋、读出 `Arc`、打删除标记、drop `Arc` |
| `<Local as IsElement>::finalize`（trait） | internal.rs L583 | `List::Iter::next` 物理摘除时 | 对 `Local` 的堆内存做 `defer_destroy`，宽限期后真正释放 |

#### 4.3.2 核心流程

`Local::finalize(this)` 的伪代码（调用前提：`guard_count==0 && handle_count==0`）：

```
fn finalize(this):                                  # unsafe fn(this: *const Self)
    (*this).handle_count = 1                        # 临时+1（见 4.3.3 解释：防 pin 的 guard 析构时递归）
    {
        guard = &(*this).pin()                      # 重新 pin：写 pinned epoch + SeqCst 屏障
        (*this).global().push_bag(local_bag, guard) # 把残留本地袋封箱入队（push_bag 内有 SeqCst 屏障）
    }                                               # guard 在此 drop → unpin → guard_count 1->0（handle_count 仍 1，不递归）
    (*this).handle_count = 0                        # 恢复 0

    collector = ptr::read((*this).collector)        # 取走 Arc<Global>（ManuallyDrop 不会替我们 drop）
    (*this).entry.delete(unprotected())             # 逻辑删除：fetch_or(1) 打标记
    drop(collector)                                 # Arc -1；若为最后一引用 → Global 析构 → 队列内袋全部执行
```

之后，某个线程的 `try_advance`/`collect` 遍历 `locals` 时，`Iter::next` 看到 `next.tag() == 1`，会物理摘除该节点并调用 `<Local as IsElement>::finalize` → `guard.defer_destroy(Shared::from(local))`，再过一个宽限期，`Local` 的堆内存才被真正释放。

#### 4.3.3 源码精读

先看 `finalize` 全貌：

[crossbeam-epoch/src/internal.rs:529-569](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L529-L569) — `Local::finalize`。

**第一个关键点：临时 `handle_count = 1`**（L537-541）：

```rust
// Temporarily increment handle count. This is required so that the following call to `pin`
// doesn't call finalize again.
unsafe { (*this).handle_count.set(1); }
```

注释说「为了让随后的 `pin` 不会再次调用 finalize」。准确的机制是：`let guard = &(*this).pin();`（L545）创建了一个临时 `Guard`，它的生命周期到该语句所在的 `unsafe` 块结束（L549）为止。块结束时这个 guard 析构 → `unpin`。`unpin` 把 `guard_count` 由 1 减到 0，然后检查 `if handle_count.get() == 0 { finalize(self) }`（见 [internal.rs:466-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L466-L479) 的 `unpin`）。若此时 `handle_count` 是 0，`unpin` 就会**再次**调用 `finalize(this)`，形成无限递归。预先把它置 1，这次 `unpin` 就不会触发 finalize；之后我们在 L552 把它显式归 0。这是一个精巧的**防递归**手法。

**第二个关键点：读 `collector` 必须在 `entry.delete` 之前**（L555-562）：

```rust
// Take the reference to the `Global` out of this `Local`. Since we're not protected
// by a guard at this time, it's crucial that the reference is read before marking the
// `Local` as deleted.
let collector: Collector = ptr::read((*this).collector.with(|c| &*(*c)));

// Mark this node in the linked list as deleted.
(*this).entry.delete(unprotected());
```

为什么顺序不能反？`entry.delete` 一旦执行（逻辑删除），这个 `Local` 就对其他线程的 `Iter` 可见为「待摘除」；别的线程可能在 `try_advance` 的遍历中把它物理 unlink 并 `defer_destroy` 掉它指向的堆内存。若先 `delete` 再读 `(*this).collector`，读的就是可能已被释放的内存（use-after-free）。所以必须趁节点还「干净」时先把 `Arc<Global>` `ptr::read` 出来。

> `ptr::read` 是因为 `collector` 字段是 `ManuallyDrop<Collector>`（[internal.rs:300](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L300)），`ManuallyDrop` 不会自动 drop，`finalize` 要亲手取走并 drop 它。

**第三个关键点：`drop(collector)` 的连锁反应**（L564-567）：

```rust
// Finally, drop the reference to the global. Note that this might be the last reference
// to the `Global`. If so, the global data will be destroyed and all deferred functions
// in its queue will be executed.
drop(collector);
```

这是 `Collector = Arc<Global>` 引用计数 -1。如果这是最后一根 `Arc`，`Global` 被 drop：它的 `queue`（`Queue<SealedBag>`）随之析构，`Queue::drop` 会把队列里**所有**残留袋 pop 出来 drop——此时已无需关心宽限期（整个 collector 都没了，无人会再 pin），所以全部立即执行。

**调用入口**：`finalize` 由谁触发？两处（u4-l15 已列，这里给链接）：

[crossbeam-epoch/src/internal.rs:464-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L464-L479) — `unpin`：最后一个 guard 释放（`guard_count` 1→0）且 `handle_count == 0` 时调用。

[crossbeam-epoch/src/internal.rs:513-527](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L513-L527) — `release_handle`：最后一张 handle 释放（`handle_count` 1→0）且 `guard_count == 0` 时调用。线程退出走的就是这条：`LocalHandle::drop` → `release_handle`。

**物理回收入口**：trait 版 `finalize`：

[crossbeam-epoch/src/internal.rs:583-585](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L583-L585) — `<Local as IsElement>::finalize`：

```rust
unsafe fn finalize(entry: *const Entry, guard: &Guard) {
    unsafe { guard.defer_destroy(Shared::from(Self::element_of(entry))) }
}
```

它由 [crossbeam-epoch/src/sync/list.rs:262-264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L262-L264) 的 `Iter::next` 在物理 unlink 成功后调用，对这个 `Local` 的堆内存做 `defer_destroy`。而 `entry.delete` 本身只是逻辑标记：

[crossbeam-epoch/src/sync/list.rs:143-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L143-L154) — `Entry::delete`：`self.next.fetch_or(1, Release, guard)`，把 `next` 指针的最低位置 1，表示「逻辑已删除」。

#### 4.3.4 代码实践（源码阅读型，对应指定实践任务）

**实践目标**：画出线程退出时 `finalize` 的完整调用时序。

**操作步骤**：在线程里 `let handle = collector.register();`，然后线程结束（`handle` 离开作用域）。请按顺序画出从 `LocalHandle::drop` 到 `Local` 堆内存被释放的全部步骤，并标注每步发生在「本线程」还是「别的线程」。

**预期结果（完整时序）**：

```
[本线程] 线程退出
  └─ LocalHandle::drop                         (collector.rs L100-107)
       └─ Local::release_handle(self.local)    (internal.rs L513-527)
            └─ guard_count==0 && handle_count==1 → Local::finalize(this)   (internal.rs L530)
                 ├─ handle_count = 1                       # 防递归
                 ├─ guard = self.pin()                     # 重新 pin
                 ├─ global().push_bag(self.bag, guard)     # 残留袋封箱入队（SeqCst 屏障 + 盖戳）
                 ├─ (guard drop → unpin → guard_count 1->0；handle_count 仍 1，不递归)
                 ├─ handle_count = 0
                 ├─ collector = ptr::read(self.collector)  # 取走 Arc<Global>
                 ├─ self.entry.delete(unprotected())       # 逻辑删除：next.fetch_or(1)
                 └─ drop(collector)                        # Arc -1（若为末引用 → Global 析构 → 队列清空执行）

[别的线程，某次 try_advance/collect 遍历 locals]
  └─ Iter::next 看到 self.curr.next.tag()==1            (list.rs L245)
       └─ CAS 物理 unlink 成功
            └─ <Local as IsElement>::finalize(entry, guard)   (internal.rs L583)
                 └─ guard.defer_destroy(Shared::from(local))  # 入本地袋

[宽限期后，别处 collect]
  └─ 该 defer_destroy 闭包执行 → Local 堆内存真正被释放
```

**需要观察的现象**：

- 退会方的 `finalize` 只做「逻辑删除 + drop `Arc`」，**不**亲自释放 `Local` 堆内存。
- `Local` 堆内存的释放要等**别的线程**遍历链表时物理摘除 + 一个宽限期。这意味着：即使线程已退出，它的 `Local` 节点会在链表里「逻辑删除态」逗留一会儿。
- 若 drop 的 `Arc` 是末引用（所有线程都退会了），`Global` 立即析构，`Queue::drop` 会把残留袋全部立即执行（此时无人 pin，无需宽限期）。

> 说明：以上为源码静态推演，行为正确性已由 `count_drops` / `stress` 等测试覆盖；具体内存释放时刻依赖运行时调度，属「待本地验证」的观测项。

#### 4.3.5 小练习与答案

**Q1**：`finalize` 为什么在调用 `pin()` 之前先把 `handle_count` 临时置 1？

**A1**：`pin()` 返回的 guard 在所在块结束时析构，触发 `unpin`；`unpin` 在 `guard_count` 1→0 时会检查 `handle_count`，若为 0 就会再次调用 `finalize(this)`，造成无限递归。预先置 1 让这次 `unpin` 不触发 finalize，之后再显式归 0。

**Q2**：`finalize` 里「`ptr::read(collector)`」与「`entry.delete()`」的顺序能否对调？为什么？

**A2**：不能。`entry.delete` 后该 `Local` 对别线程的 `Iter` 可见为「待摘除」，可能被物理 unlink 并 `defer_destroy` 其堆内存。若先 `delete` 再读 `collector`，会读到已释放内存（use-after-free）。必须趁节点还干净时先 `ptr::read` 出 `Arc`。

**Q3**：`Local::finalize`（inherent）与 `<Local as IsElement>::finalize`（trait）职责有何不同？

**A3**：inherent 版是「逻辑退会」：由退会方在 `unpin`/`release_handle` 双零时主动调用，负责入队残留袋、读出 `Arc`、打删除标记、drop `Arc`。trait 版是「物理回收」：由任意线程的 `Iter::next` 在物理摘除已标记节点时调用，对 `Local` 堆内存 `defer_destroy`，宽限期后真正释放。

---

## 5. 综合实践

**目标**：用公开 API 跑通一次完整的「分配 → defer → flush → 周期 collect 回收 → 线程退出 finalize」闭环，把本讲三个模块串起来。

> 公开 API 边界提醒：`Collector::global`、`Global::collect`、`Global::epoch` 都是 `pub(crate)`，**外部 crate 无法**手动调用 `collect` 或读取全局 epoch。外部用户只能依赖 `pin()` 内部每 128 次的周期性 `collect`（u5-l18）或主动 `flush()` 来驱动回收。下面给出**公开 API 可运行版**与**crate 内测试版**两条路径。

### 路径 A：公开 API 可运行版（外部 crate）

```rust
// 示例代码（非项目原有代码）：依赖 crossbeam-epoch 的 std feature
use crossbeam_epoch as epoch;
use std::sync::atomic::{AtomicUsize, Ordering};

static DROPS: AtomicUsize = AtomicUsize::new(0);

struct Elem(#[allow(dead_code)] i32);
impl Drop for Elem {
    fn drop(&mut self) { DROPS.fetch_add(1, Ordering::Relaxed); }
}

fn main() {
    const COUNT: usize = 10_000;
    // 1. 分配 + defer：对象进入线程本地袋
    for _ in 0..COUNT {
        let guard = &epoch::pin();                       // pin 给出 guard
        let a = epoch::Owned::new(Elem(7)).into_shared(guard);
        unsafe { guard.defer_destroy(a); }               // 契约：对象已从数据结构摘除、不可达
    }

    // 2. flush：把本地袋封箱入队（push_bag 的 SeqCst 屏障 + 盖戳）
    epoch::pin().flush();

    // 3. 驱动回收：反复 pin 触发周期 collect（每 128 次 pin 一次 collect → try_advance + 弹 8 袋）
    while DROPS.load(Ordering::Relaxed) < COUNT {
        let _g = epoch::pin();                           // drop _g 即 unpin
    }
    assert_eq!(DROPS.load(Ordering::Relaxed), COUNT);    // 待本地验证

    // 4. 线程退出：线程局部 handle 析构 → release_handle → Local::finalize
    //    （残留袋被 push_bag 入队，Local 被逻辑删除，最终由别线程物理回收）
}
```

**操作步骤**：在实验工程里 `cargo add crossbeam-epoch`，运行上述程序；调整 `COUNT` 与第 3 步的 pin 次数，观察 `DROPS` 增长曲线。

**需要观察的现象 / 预期结果**（待本地验证）：

- 第 3 步 `DROPS` 不是一次性跳到 `COUNT`，而是**一批一批**增长——这正是「每次 collect 至多回收 8×64=512 个」的体现（4.2）。
- 线程退出后程序正常结束，无报错、无 leak（用 `cargo run` + 可选的泄漏工具观察）。

### 路径 B：crate 内测试版（精确观测）

若想精确观测「单次 collect 回收多少」「全局 epoch 推进到几」，必须像项目自带测试那样在 crate 内部写测试（能访问 `collector.global.collect` 与 `collector.global.epoch`）。直接对照 [crossbeam-epoch/src/collector.rs:284-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L284-L315) 的 `count_drops` 测试：它 defer `COUNT` 个带 Drop 计数的对象，`flush`，然后在 `while DROPS < COUNT` 循环里反复 `pin` + `collector.global.collect(guard)`。把它当作模板，自行加日志打印每次 collect 前后的 `collector.global.epoch.load(Relaxed)` 与 `DROPS`，即可看到 4.1/4.2/4.3 三段如何协作。

---

## 6. 本讲小结

- `Global::try_advance` 遍历 `locals` 花名册，只有「所有 pin 着的参与者都钉在当前 epoch」且遍历未被打断（无 `Stalled`）时，才用 `successor` + `Release` store 推进一步；否则原样返回。起始的 `SeqCst` 屏障与 `pin` 的 `SeqCst` 屏障配对，是「公告在读之前」的安全关键。
- `Global::collect` 先 `try_advance` 推进时钟，再用 `try_pop_if`（条件 = `is_expired`，即 `wrapping_sub >= 2`）弹出至多 `COLLECT_STEPS = 8` 个过保袋并 drop，触发 `Bag::drop` 执行延迟闭包。单次回收量被 \(8 \times 64 = 512\) 封顶（`incremental` 断言取 \(2\times\) 余量 1024）。
- `Local::finalize`（inherent）由 `unpin`/`release_handle` 在双计数归零时触发：临时 `handle_count=1` 防 `pin` 的 guard 析构递归，push 残留袋，`ptr::read` 出 `Arc` 后才 `entry.delete`，最后 drop `Arc`（可能连带销毁整个 `Global`）。
- `<Local as IsElement>::finalize`（trait）是「物理回收」，由别的线程 `Iter::next` 物理摘除已标记节点时调用 `defer_destroy`，宽限期后释放 `Local` 堆内存。
- `finalize` 中「先读 `collector` 再 `delete`」的顺序是 use-after-free 防线，不可颠倒。
- 整条链路是懒驱动、摊销式的：`pin` 每 128 次 / 用户 `flush` 才推进一次时钟并回收一小批；不 pin 不前进。

## 7. 下一步学习建议

到这里，EBR 的「数据表示 → pin/unpin 屏障 → 推进与回收 → 退会善后」主链路已全部打通。建议接着：

1. **u6-l20（无锁侵入式链表）**：本讲多次出现 `IterError::Stalled`、`entry.delete`、`Iter::next` 物理 unlink——它们的全貌正是 `sync/list.rs`。读完它你才能彻底理解 `try_advance` 遍历花名册时为何会被「打滑」、被标记删除的节点如何被安全回收。
2. **u6-l21（Michael-Scott 队列）**：本讲的 `try_pop_if` 就是建立在这个队列之上的条件弹出。读它能理解「tail 滞后」「helping」「pop 时 defer_destroy 旧 head」的细节。
3. **u6-l23（测试、基准与示例）**：用 `cargo bench` 实测 `pin`/`defer`/`flush` 的开销，并在 sanitizer 下重跑，验证本讲描述的回收时序在并发压测下依然正确。
4. 若想验证理解，可尝试回答：**「为什么 `try_advance` 的起始 `SeqCst` 屏障若改成 `Acquire`，在 ARM/POWER 上可能 use-after-free？」**（提示：结合 u5-l18 的不变量 A 与 store-load 重排。）
