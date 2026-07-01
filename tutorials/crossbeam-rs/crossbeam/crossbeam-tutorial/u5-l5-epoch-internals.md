# internal：全局状态、epoch 推进与垃圾回收

## 1. 本讲目标

本讲是 crossbeam-epoch 单元的「收官篇」。前面三讲（u5-l2 原子指针、u5-l3 Guard/pin、u5-l4 Collector/注册）回答了「指针怎么存」「怎么 pin」「参与者怎么挂上去」，但始终绕开了最核心的一个问题：**删掉的节点，到底什么时候、由谁、按什么规则真正释放？**

读完本讲，你应当能够：

1. 说清 `Epoch` 如何用一个机器字同时编码「代次」与「是否被 pin」，以及 `successor`/`wrapping_sub` 的位运算含义。
2. 画出 `Global`（全局状态）与 `Local`（线程局部参与者）的字段布局，说清垃圾从「线程局部 bag」流向「全局 queue」的数据通路。
3. 复述 `try_advance` 推进全局 epoch 的「全员到齐」协议——为什么必须等所有 pin 中的参与者都登记在当前代，才允许推进。
4. 用一句话解释「**为什么要等 2 个 epoch 而不是 1 个**」——这是整个 epoch 回收安全性的命门。
5. 跟踪一次 `defer → push_bag → try_advance → collect → drop(SealedBag)` 的完整调用链。

## 2. 前置知识

本讲默认你已经掌握前四讲建立的认知，下面只做最简回顾，**不重复展开**：

- **无锁结构为什么需要内存回收**（u5-l1）：删除方想 free 节点时，读取方可能仍持有其指针，立即释放会 use-after-free；epoch 方案 = 标记 → 延迟 → 销毁。
- **Atomic / Shared / Owned**（u5-l2）：`Atomic<T>` 是单机器字原子指针，`Shared<'g,T>` 的生命周期 `'g` 绑定 `Guard`，从类型上禁止读出的指针逃出本次 pin。
- **Guard 与 defer**（u5-l3）：`pin()` 返回 RAII 凭证 `Guard`；`defer` / `defer_destroy` 把销毁闭包塞进「线程局部 bag」，满 64 个才盖章入全局队列，须等 `global_epoch - bag_epoch >= 2` 才销毁。
- **Collector 与参与者注册**（u5-l4）：`Collector` 本质是 `Arc<Global>`；`register()` 把一个 `Local` 节点无锁插入 `Global::locals` 侵入式链表；`LocalHandle::pin` 在 `guard_count` 从 0→1 时「发布 epoch + `SeqCst` fence」，fence 是回收安全的命门。

本讲要做的事，就是把上面散落的拼图——**Global 的全局队列、Local 的局部 bag、epoch 的推进规则、垃圾的销毁判据**——一次性拼成一张完整的运行时图。

还需要一个术语铺垫：**代次（generation / lap）**。在 crossbeam 里，「epoch」既是全局那个单调递增的代次计数器，也指参与者本地记录的「我 pin 时全局停在第几代」。一次推进（advance）= 全局代次 +1。后文为避免混淆，用「代」表示逻辑代次（0,1,2,…），用「epoch 值」表示它在机器字里的实际存储。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [crossbeam-epoch/src/epoch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs) | `Epoch` / `AtomicEpoch` 的定义与位运算 | LSB=pin 位、`successor` 加 2、`wrapping_sub` 算代差 |
| [crossbeam-epoch/src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | `Global`、`Local`、`Bag`、`SealedBag` 及 `try_advance` / `collect` / `push_bag` | 本讲的绝对主战场，4 个模块都围绕它 |
| [crossbeam-epoch/src/sync/queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs) | Michael-Scott 无锁队列，承载全局 `Queue<SealedBag>` | `try_pop_if` 用条件谓词摘取过期 bag |
| [crossbeam-epoch/src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard` 的 `defer_unchecked` 与 `Drop`（unpin） | 垃圾的入口与 pin 的出口 |

---

## 4. 核心概念与源码讲解

### 4.1 Epoch 的位编码：一个字塞下「代次 + pin 状态」

#### 4.1.1 概念说明

`epoch.rs` 开头一句话点明整个回收安全性的根据：

> If an object became garbage in some epoch, then we can be sure that after two advancements no participant will hold a reference to it.

要把这句话变成代码，需要一个既能表示「当前第几代」、又能表示「这个参与者现在是否被 pin」的值。crossbeam 的做法极其紧凑：**用一个机器字，最低位（LSB）当 pin 标志，其余位当代次**。这样 `pin`/`unpin` 只动 1 个 bit，`advance` 只需把代次 +1，而代次 +1 在「低位被 pin 占用」的布局下等价于「存储值 +2」。

#### 4.1.2 核心流程

设机器字为 `data`，则：

- `data & 1 == 1` → 已 pin；`== 0` → 未 pin
- 逻辑代次 \( g \)（未 pin 时）= `data >> 1`，即存储值 \( = 2g \)
- `pinned()`：`data | 1`（置 pin 位，代次不变）
- `unpinned()`：`data & !1`（清 pin 位）
- `successor()`：`data.wrapping_add(2)`（代次 +1，pin 位保持）

  > 为什么是加 2 而不是加 1？因为代次存在「第 1 位及以上」，`+2 = 0b10` 恰好让第 1 位进位、第 0 位（pin 位）不变。所以「推进一代」=「存储值 +2」。

- 代差：`wrapping_sub(other)` 算「我比 `other` 早/晚几代」，结果以「代」为单位（有符号）。

由于 `successor` 用 `wrapping_add`，代次在机器字回绕后仍正确——只要参与运算的两者代差远小于 \( 2^{63} \)（64 位平台），就不会出错。这也是为什么 epoch 回收在工程上是「实用」的：代次永远不会真正回绕到伤及正确性。

#### 4.1.3 源码精读

`Epoch` 只有一个 `data` 字段，类型按平台选 `u64`（有 64 位原子）或 `usize`：

[crossbeam-epoch/src/epoch.rs:32-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L32-L36) —— 注释直白说明「最低位表 pin，其余位表代次」。

四个位运算方法集中体现了上面的编码：

[crossbeam-epoch/src/epoch.rs:57-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L57-L86) —— `is_pinned`/`pinned`/`unpinned`/`successor`。注意 `successor` 的注释：「The returned epoch will be marked as pinned only if the previous one was also」，正是因为 `wrapping_add(2)` 不动第 0 位。

代差的计算是本讲判据 `>= 2` 的算术基础：

[crossbeam-epoch/src/epoch.rs:49-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L49-L54) —— `self.data.wrapping_sub(rhs.data & !1) as Signed >> 1`。先把 `rhs` 的 pin 位屏蔽掉（防御性，因为被减的通常是「全局 epoch」与「bag 盖章 epoch」，二者本就未 pin），再做有符号右移 1 位，把「存储值之差」换算成「代差」。注释点明结果落在 `isize` 范围内。

#### 4.1.4 代码实践

**实践目标**：用纸笔验证位编码，确认「推进两代」在存储值上等于「+4」。

**操作步骤**：

1. 设 `starting()` 的 `data = 0`（未 pin，代 0）。
2. 手算 `starting().successor().successor()` 的 `data`：应为 \( 0 + 2 + 2 = 4 \)，对应代 2。
3. 手算 `global_epoch = 代2`（data=4）对 `sealed = 代0`（data=0）的 `wrapping_sub`：\( (4 - 0) \gg 1 = 2 \)，满足 `>= 2` → 可销毁。
4. 把 `sealed` 改成代 1（data=2）：\( (4-2) \gg 1 = 1 \)，不满足 `>= 2` → **不可销毁**。

**预期结果**：你能口算出「代 0 的垃圾，要等全局到代 2 才能销毁；全局在代 1 时还不能动它」——这正是「等 2 个 epoch」的位运算来源。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `successor` 用 `wrapping_add(2)` 而不是 `+1`？

> **答**：因为最低位被 pin 标志占用，代次从第 1 位开始存。`+2 = 0b10` 让代次进位而保留 pin 位；若 `+1` 则会把 pin 位翻成 1，破坏「未 pin」语义。

**练习 2**：`wrapping_sub` 末尾的 `>> 1` 若去掉，`is_expired` 里的阈值应该改成多少？

> **答**：去掉右移后结果是「存储值之差」，每代占 2，所以阈值要从 `>= 2` 改成 `>= 4` 才等价。

---

### 4.2 Global 与 Local：全局状态与线程局部参与者

#### 4.2.1 概念说明

epoch 回收是一个「两级缓存」系统：

- **全局侧 `Global`**：所有线程共享，持有三样东西——参与者链表、垃圾队列、全局 epoch。它是「最终事实」所在。
- **线程局部侧 `Local`**：每个注册参与者一个，持有自己的小垃圾袋 `bag`、pin 计数、本地 epoch。它是「缓冲」所在，用来摊薄与全局同步的开销。

为什么要有线程局部 bag？因为如果每次 `defer` 都直接往全局队列 push，全局队列会成为争用热点。crossbeam 的做法是：闭包先进线程局部 bag，**攒满 64 个**才盖一个 epoch 戳、整体塞进全局队列。这样绝大多数 `defer` 只是写线程局部数组，零同步。

#### 4.2.2 核心流程

垃圾的生命周期是一条「漏斗」：

```
defer_unchecked(closure)
        │  写入线程局部 bag
        ▼
   Local::bag  (最多 64 个，零同步)
        │  攒满 → push_bag
        ▼
   Global::queue  (Michael-Scott 无锁队列，盖 epoch 戳)
        │  collect 时 try_pop_if(is_expired)
        ▼
   drop(SealedBag)  → Bag::drop → 逐个调用闭包（真正释放）
```

- `pin()`：`guard_count` 从 0→1 时，把「全局 epoch 标记为 pinned」存进 `Local::epoch`，插 `SeqCst` fence；每 128 次 pin 周期性 `collect`。
- `unpin()`：`guard_count` 从 1→0 时，把 `Local::epoch` 复位为未 pin；若同时 `handle_count==0`（线程退出），执行 `finalize` 把残留 bag 推全局并从链表摘除自己。
- `defer()`：往局部 bag 塞；塞不下就先 `push_bag` 腾空，再塞。

#### 4.2.3 源码精读

`Global` 的三件套一目了然：

[crossbeam-epoch/src/internal.rs:165-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L165-L174) —— `locals`（参与者侵入式链表）、`queue`（`Queue<SealedBag>` 全局垃圾队列）、`epoch`（`CachePadded<AtomicEpoch>`，缓存行隔离防伪共享，呼应 u2-l2）。

`push_bag` 是「局部 → 全局」的关口，关键在盖章与 fence 的顺序：

[crossbeam-epoch/src/internal.rs:191-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L191-L198) —— 先 `mem::replace` 腾空 bag 换个新的，再 `SeqCst` fence，再 `load` 当前全局 epoch，最后 `bag.seal(epoch)` 入队。**用「当前最大 epoch」盖章是最保守的选择**：bag 里可能混有更早 defer 的闭包，盖一个更大的戳只意味着多等一会，绝不危及安全。

`Local` 的字段布局体现「线程私有的全部状态」：

[crossbeam-epoch/src/internal.rs:292-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L292-L318) —— 注意三处要点：① `entry` 必须是首字段且结构体 `#[repr(C)]`（见 [L292](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L292) 注释），这样 `*const Local` 与 `*const Entry` 数值相等，链表才能用侵入式节点；② `bag: UnsafeCell<Bag>` 提供内部可变性；③ `guard_count`/`handle_count`/`pin_count` 都是 `Cell`（线程私有，无需原子）。

`defer` 的「塞不下就冲刷」循环很简洁：

[crossbeam-epoch/src/internal.rs:382-389](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L382-L389) —— `try_push` 返回 `Err(d)` 表示满，于是 `push_bag` 把整袋推全局、换空袋，再重试塞 `d`。

`pin` 的可重入与周期性回收：

[crossbeam-epoch/src/internal.rs:402-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L402-L462) —— `guard_count == 0` 分支（[L409](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L409)）只在「首次 pin」做实质工作：存 pinned 本地 epoch + fence；[L455-L458](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L455-L458) 用 `pin_count % 128 == 0` 触发 `collect`（注意 `collect` 标了 `#[cold]`，编译器会把它放到冷路径）。

`unpin` 在最后一次离开时复位 epoch，并在「无 handle」时自我终结：

[crossbeam-epoch/src/internal.rs:465-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L465-L479) —— `guard_count == 1` 时（即将变 0）`store(Epoch::starting(), Release)`，把 pin 位与代次一起清零；若 `handle_count == 0` 调 `finalize`，确保线程退出时不丢垃圾。

#### 4.2.4 代码实践

**实践目标**：确认「局部 bag 攒满 64 个才入全局」这一缓冲策略。

**操作步骤**：

1. 阅读 [internal.rs:64-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L64-L69)，记下正常构建 `MAX_OBJECTS = 64`、`miri`/`sanitize` 下为 4。
2. 阅读 [internal.rs:612-635](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L612-L635) 的单元测试 `check_bag`：连 push 64 个都 `is_ok`，第 65 个 `is_err`，`drop(bag)` 后 `FLAG == MAX_OBJECTS`（证明 `Bag::drop` 逐个调用了闭包）。

**需要观察的现象**：bag 满之前 `FLAG` 始终为 0（闭包未执行），`drop` 后才等于 64。这正是「延迟执行」的最小验证。

#### 4.2.5 小练习与答案

**练习 1**：`Local` 的 `guard_count` / `handle_count` / `pin_count` 为什么用 `Cell` 而非原子？

> **答**：因为一个 `Local` 始终归唯一一个线程所有（线程局部），不存在并发访问，`Cell` 的「单线程内部可变」语义正合适，且比原子便宜。真正需要跨线程可见的是 `Local::epoch`，它用 `AtomicEpoch`。

**练习 2**：`push_bag` 用「当前全局 epoch」给整袋盖章，若袋里混有更早 defer 的闭包，会出错吗？

> **答**：不会。盖更大的戳只是让这袋「多等几代」才被判定过期，是更保守（更晚释放）的选择，安全性只增不减。回收判据 `global - sealed >= 2` 对「真实产生时间 ≤ 盖章时间」永远成立。

---

### 4.3 try_advance：推进 epoch 的「全员到齐」协议

#### 4.3.1 概念说明

全局 epoch 不是随便就能 +1 的。回忆 u5-l1 的安全判据：「一个 pin 中的参与者最多见证一次 epoch 推进」。要兑现这条，推进必须满足：

> **当前所有处于 pin 状态的参与者，都必须登记在「当前全局代」。**

如果还有一个参与者 pin 在更老的代（说明它可能正持有老代读出来的指针），就**绝不能推进**——否则它会在一次 pin 里见证两次推进，破坏安全前提。`try_advance` 就是这条规则的执行者。

#### 4.3.2 核心流程

```
try_advance(guard):
  1. global_epoch = load(全局 epoch, Relaxed)
  2. fence(SeqCst)               ← 与 pin 里的 fence 配对，建立可见性
  3. 遍历 locals 链表每个参与者 local:
       local_epoch = local.epoch.load(Relaxed)
       if local_epoch.is_pinned() && local_epoch.unpinned() != global_epoch:
           return global_epoch   ← 有人 pin 在老代，放弃推进
  4. fence(Acquire)              ← 把上面所有读「钉住」
  5. new_epoch = global_epoch.successor()
  6. store(全局 epoch, new_epoch, Release)
  7. return new_epoch
```

步骤 2 的 `SeqCst` fence 与 `Local::pin` 里的 `SeqCst` fence 配对（见 u5-l4），作用是：**回收方读到的「参与者 pin 在第几代」一定不早于该参与者之后对数据结构的读**。换句话说，只要 `try_advance` 没看到某参与者 pin 在老代，那它后续推进释放的老代垃圾，该参与者也不可能还在引用——这就是「不 use-after-free」的形式化保证。

步骤 6 的 `store` 即使和别的线程并发也无害：注释解释，因为发起 `try_advance` 的线程自己也 pin 在 `global_epoch`，它会把全局 epoch 顶死在「最多 +1」——所以并发者要么还没推进（仍是旧值，被本线程推进），要么已经推进到同一个 `new_epoch`（覆写相同值）。

#### 4.3.3 源码精读

整段 `try_advance` 是本讲最该精读的代码：

[crossbeam-epoch/src/internal.rs:236-288](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L236-L288) —— 标了 `#[cold]`，因为只在周期性 `collect` 里偶尔调用。

核心判定就一行：

[crossbeam-epoch/src/internal.rs:262-264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L262-L264) —— 「参与者已 pin」**且**「其 unpinned 代次 ≠ 当前全局代」→ `return global_epoch` 放弃推进。换言之，只有「未 pin」或「pin 在当前代」的参与者才不挡路。

遍历期间若被并发线程「卡住」（链表迭代返回 `IterError::Stalled`），[L251-L256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L251-L256) 选择「把活儿让给对方」直接返回——宁可本次不推进，也不冒进。

两道 fence 分别在 [L239](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L239)（`SeqCst`，读参与者前）和 [L276](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L276)（`Acquire`，遍历结束）。注意 ThreadSanitizer 不理解 fence，于是 [L244-L274](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L244-L274) 有一段 `#[cfg(crossbeam_sanitize_thread)]` 的「用一次 `Acquire` load 模拟 fence」的等价实现——这是为了在 tsan 下不产生假阳性告警，呼应 u7-l3 的「可验证性」取舍。

推进动作本身很轻：

[crossbeam-epoch/src/internal.rs:285-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L285-L287) —— `successor()` 造新代，`store(Release)` 发布。

#### 4.3.4 代码实践

**实践目标**：用一个具体场景走查「为什么 A pin 在老代会挡住推进」。

**操作步骤**：

1. 假设全局在代 0。线程 A `pin()` → `Local::epoch` 存「pinned 代 0」。
2. 线程 B `pin()`（也代 0）后调 `collect` → `try_advance`：遍历到 A，`A.epoch.unpinned() == 0 == global_epoch` → 不挡路；B 自己也不挡路 → 推进到代 1。
3. 此时 A 仍 pin 着（没 unpin），其 `Local::epoch` 还是「pinned 代 0」。线程 C 再 `try_advance`：遍历到 A，`A.epoch.unpinned() == 0 != 1(全局)` → **挡路，返回不推进**。
4. 直到 A `unpin()`（`epoch` 复位为未 pin），下一次 `try_advance` 才能推进到代 2。

**需要观察的现象**：只要 A 一直 pin 在代 0，全局 epoch 就被钉在代 1，永远到不了代 2——因此 A 引用的「代 0 垃圾」也永远不会被判定过期（代差始终 < 2）。

#### 4.3.5 小练习与答案

**练习 1**：`try_advance` 里读参与者 `local_epoch` 用 `Relaxed`，安全吗？

> **答**：单看这一次 `Relaxed` load 确实没有同步语义，但前后有 `SeqCst`/`Acquire` fence 兜底：步骤 2 的 `SeqCst` fence 保证「读到的是某个一致时刻的值」，步骤 4 的 `Acquire` fence 保证「遍历中所有读不被重排到 store(新代) 之后」。fence 才是真正的同步点，单次 load 用 `Relaxed` 是性能取舍。

**练习 2**：如果允许「pin 在老代的参与者」存在时仍推进全局 epoch，会破坏哪条不变量？

> **答**：会破坏「一次 pin 最多见证一次推进」。该参与者可能在自己的一次 pin 内看到全局从代 \(g\) 跳到 \(g+2\)，于是它读出的「代 \(g\) 指针」对应的节点可能在代 \(g+1\) 就被释放了——即 use-after-free。「全员到齐」协议正是为守住这条不变量。

---

### 4.4 collect 与 SealedBag：两 epoch 延迟销毁

#### 4.4.1 概念说明

推进了 epoch 还不够，得有人真正去「销毁过期垃圾」，这就是 `collect`。它做两件事：

1. 调 `try_advance`（尽量把全局代往前推，让更多垃圾「变老」）。
2. 从全局队列里摘取「已过期」的 `SealedBag` 并 `drop` 之——`drop` 会触发 `Bag::drop`，逐个调用闭包，**这才是真正的释放**。

判定「过期」的判据就是模块 4.1 的代差：`global_epoch - sealed_epoch >= 2`。这条 `>= 2` 是整个系统正确性的压舱石，下面专门讲清「为什么是 2 不是 1」。

#### 4.4.2 核心流程

```
collect(guard):
  global_epoch = try_advance(guard)        ← 先尝试推进
  steps = COLLECT_STEPS (8)                ← sanitize 下为 usize::MAX（尽量清空）
  repeat steps 次:
      sealed_bag = queue.try_pop_if(|sb| sb.is_expired(global_epoch))
      match sealed_bag:
          None    => break                 ← 队头未过期（FIFO，后面更老才会过期？见下）
          Some(b) => drop(b)               ← Bag::drop → 逐个执行闭包
```

> 关于「队头未过期就 break」：全局队列是 FIFO，**先入队的 bag 盖章更早（代更小）**，所以「更可能先过期」。一旦队头都还没过期，后面的只会更「年轻」，于是直接 break，避免无效遍历。这是用 FIFO 单调性换来的提前退出。

#### 4.4.3 源码精读

`collect` 主体：

[crossbeam-epoch/src/internal.rs:207-226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L207-L226) —— 先 `try_advance`，再循环 `try_pop_if(is_expired)`，`drop(sealed_bag)` 触发销毁。`COLLECT_STEPS = 8` 见 [L178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L178)；`crossbeam_sanitize` 下改成 `usize::MAX`（[L211-L215](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L211-L215)），让单测/排错时尽量一次清空，更容易暴露竞争。

`SealedBag` 与过期判据：

[crossbeam-epoch/src/internal.rs:145-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L145-L162) —— `SealedBag = { epoch, _bag }`；`is_expired` 的注释就是本讲的中心论点：「**A pinned participant can witness at most one epoch advancement. Therefore, any bag that is within one epoch of the current one cannot be destroyed yet.**」判据 `global_epoch.wrapping_sub(self.epoch) >= 2`。

`Bag` 的结构与销毁语义：

[crossbeam-epoch/src/internal.rs:71-114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L71-L114) —— `Bag { deferreds: [Deferred; 64], len }`，`seal(epoch)` 把自己包成 `SealedBag`。

[crossbeam-epoch/src/internal.rs:125-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L125-L134) —— `Bag::drop` 把每个闭包 `mem::replace` 出来逐个 `call()`。**这一刻才是被延迟对象真正被释放的时刻**——之前它一直安稳躺在 bag / 队列里。

`try_pop_if` 来自 Michael-Scott 队列：

[crossbeam-epoch/src/sync/queue.rs:188-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L188-L202) —— 它在摘取队头前先用谓词 `condition(&T)` 判断（[L157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L157) 的 `Some(n) if condition(...)`），不满足就当空返回。摘下后 `guard.defer_destroy(head)`（[L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L168)）——队列自己的节点也用 epoch 回收，颇有「自举」意味。

#### 4.4.4 代码实践

**实践目标**：把「等 2 个 epoch」的论证用一张表固定下来。

**操作步骤**：填下表（设垃圾在代 \(g\) 产生并盖章）：

| 全局当前代 | 代差 `global - g` | 该 bag 是否可销毁 | 理由 |
|-----------|-------------------|------------------|------|
| \(g\)     | 0                 | 否               | 正是产生它的代，可能有参与者正引用 |
| \(g+1\)   | 1                 | 否               | 可能有参与者 pin 在代 \(g\)，见证了 \(g \to g+1\) 这一次推进，仍可能引用 |
| \(g+2\)   | 2                 | **是**           | 任何仍 pin 的参与者必 pin 在 \(\ge g+1\)，不可能还引用代 \(g\) 的垃圾 |

**预期结果**：你能看着表说出「代差 0、1 都不安全，必须 ≥ 2」。

#### 4.4.5 小练习与答案

**练习 1**：为什么是「等 2 代」而不是「等 1 代」？用「见证推进次数」解释。

> **答**：一次 pin 最多见证一次推进。一个 pin 在代 \(g\) 的参与者，能活到全局代 \(g+1\)（见证 \(g \to g+1\)），但因为有它在挡路（`unpinned != global`），全局**到不了** \(g+2\)。所以当全局真的到 \(g+2\) 时，**没有任何**还 pin 着的参与者可能引用代 \(g\) 的垃圾——此时释放才安全。若只等 1 代（在 \(g+1\) 释放），那个 pin 在 \(g\)、活到 \(g+1\) 的参与者可能正拿着代 \(g\) 的指针，会 use-after-free。

**练习 2**：`collect` 里「`try_pop_if` 返回 `None` 就 break」会不会漏掉「队头没过期、队尾却过期」的 bag？

> **答**：不会。队列 FIFO，先入队的 bag 盖章更早（代更小），代差更大，**更先过期**。队头未过期意味着它太「年轻」，队尾只会更年轻，故后续全未过期，break 是安全的提前退出。

---

## 5. 综合实践

本任务把四个模块串起来：**画出 epoch 推进时序图 + 跑一个能观察「延迟销毁」的最小程序**。

### 5.1 画时序图（必做）

参与者 A 在代 0 pin → defer 一袋垃圾 → 参与者 B 推进 epoch → 追踪该垃圾何时被销毁。按下表填写（箭头表示时间推进）：

```
时间   全局epoch   动作                                   代0垃圾状态
────   ─────────   ────                                   ──────────
t0     代0         A.pin() → A.epoch = "pinned 代0"        尚未产生
t1     代0         A defer 闭包 X → 进 A 的局部 bag         X 在局部 bag
t2     代0         A 的 bag 满 → push_bag，盖章"代0"        X 在全局队列(SealedBag@代0)
t3     代1         B.pin()后 collect → try_advance 成功     队列里，代差=1，未过期
                   （A 仍 pinned 代0，本应挡路？见思考题）
t4     代1         A.unpin() → A.epoch 复位未 pin           A 不再挡路
t5     代2         某 pin 后 collect → try_advance → 代2    代差=2，is_expired 为真 → drop(SealedBag)
                                                          → Bag::drop → 执行闭包 X（真正释放）
```

**思考题（关键）**：上表 t3 标注「A 仍 pinned 代0」，按 4.3 的协议 `try_advance` 应当被 A 挡住、推不进代 1。请重新修正时序：要让全局推进到代 1，前提条件是什么？（提示：A 必须先 unpin，或 A 根本没 pin。重画时让 B 在 A unpin 之后再推进，并据此重新定位 t3/t4/t5。）

**最终要回答**：该垃圾（盖章代 0）在全局到达**代 2** 时才被销毁；为何不能在代 1 销毁——因为可能存在「pin 在代 0、并活到代 1」的参与者仍引用它（见 4.4.5 练习 1）。

### 5.2 跑一个观察「延迟销毁」的小程序（可选，待本地验证）

下面示例用**自定义 `Collector`** 隔离观察：线程 A pin 并 `defer_unchecked` 一个会自增计数器的闭包；线程 B 反复 pin/unpin 驱动 `try_advance + collect`，看计数器何时从 0 变 1。这是「源码阅读型 + 运行型」混合实践。

```rust
// 示例代码：依赖 crossbeam_epoch（cargo add crossbeam-epoch）
use crossbeam_epoch::{self as epoch, Collector};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

fn main() {
    let collector = Collector::new();
    let fired = Arc::new(AtomicUsize::new(0));

    // 线程 A：pin → defer 一个闭包 → unpin
    let collector_a = collector.clone();
    let fired_a = fired.clone();
    let a = std::thread::spawn(move || {
        let handle = collector_a.register();
        let guard = handle.pin();
        let f = fired_a.clone();
        // 把“销毁动作”塞进延迟队列
        unsafe {
            guard.defer_unchecked(move || {
                f.fetch_add(1, Ordering::SeqCst);
            });
        }
        drop(guard); // unpin
        drop(handle); // 释放参与者（finalize 会把残留 bag 推全局）
    });
    a.join().unwrap();
    println!("A join 后，闭包执行次数 = {}", fired.load(Ordering::SeqCst));

    // 线程 B：反复 pin/unpin，周期性触发 try_advance + collect
    let collector_b = collector.clone();
    let b = std::thread::spawn(move || {
        for _ in 0..2048 {
            let handle = collector_b.register();
            let guard = handle.pin(); // 每 128 次会 collect 一次
            drop(guard);
            drop(handle);
        }
    });
    b.join().unwrap();
    println!("B 反复 pin/unpin 后，闭包执行次数 = {}", fired.load(Ordering::SeqCst));
}
```

**需要观察的现象**：

1. A join 后，闭包**大概率尚未执行**（计数仍为 0）——因为它刚被推入队列，代差不足 2。
2. B 反复 pin/unpin 一两千次后，计数变为 1——说明 `collect` 终于判定该 bag 过期并执行了闭包。

**预期结果 / 待本地验证**：由于 epoch 推进与 `collect` 的触发时机依赖线程调度，第 1 步「未执行」与第 2 步「最终执行」是可观察的趋势，但**精确在哪一次 pin 触发不可预言**（这正是「延迟销毁，不保证立即」的体现）。若运行结果与此不符，请结合 4.3/4.4 重新分析 `try_advance` 是否被谁的 pin 挡住了。

> 提示：如果你想强制观察「代差 < 2 时不释放」，可在 A 的闭包里再 `pin()` 一次并长时间持有（让 A 持续挡路），此时无论 B 怎么 pin/unpin，计数应迟迟不为 1——直到 A 真正 unpin。

---

## 6. 本讲小结

- **Epoch 位编码**：一个机器字，LSB 当 pin 位、其余当代次；`successor = +2`（代次 +1 不动 pin 位），`wrapping_sub >> 1` 把存储值之差换算成代差——这是判据 `>= 2` 的算术根基。
- **两级缓存**：闭包先进线程局部 `Local::bag`（攒满 64 个零同步），`push_bag` 时盖「当前全局 epoch」戳、`SeqCst` fence 后整体入 `Global::queue`（Michael-Scott 无锁队列）。
- **try_advance 协议**：推进前用 `SeqCst` fence 遍历所有参与者，只要存在「pin 在老代」的参与者就放弃推进；只有「全员都 pin 在当前代（或未 pin）」才 `successor + store(Release)`。这是「一次 pin 最多见证一次推进」的执行保障。
- **两 epoch 延迟销毁**：`collect` 先 `try_advance` 再 `try_pop_if(is_expired)`，过期判据 `global - sealed >= 2`；`drop(SealedBag)` 触发 `Bag::drop` 逐个调闭包，**这才是真正释放的时刻**。
- **为什么是 2 不是 1**：pin 在代 \(g\) 的参与者可活到代 \(g+1\)（见证一次推进）并仍引用代 \(g\) 垃圾，故代 \(g+1\) 释放不安全；只有全局到 \(g+2\)（该参与者要么已 unpin，要么不可能仍 pin 在 \(g\)），才无人能再引用——必须等 2 代。
- **可验证性取舍**：`miri`/`sanitize` 下 `MAX_OBJECTS` 降到 4、`COLLECT_STEPS` 提到 `usize::MAX`、tsan 用 `Acquire` load 模拟 fence，都是为了在排错工具下更容易暴露竞争、不产生假阳性。

## 7. 下一步学习建议

至此 crossbeam-epoch 的五讲（u5-l1 ~ u5-l5）已闭合：从「为何要回收到「指针/Guard/Collector/全局协议/延迟销毁」的完整链路你都走过了。接下来：

1. **立刻验证理解**：进入 [u6 工作窃取队列](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs)，看 `deque.rs` 在 resize 旧 buffer 时如何用 `guard.defer_destroy` 安全回收——这是 epoch 的第一个真实用例。
2. **更复杂的综合应用**：[u7 无锁跳表](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) 里 `mark_tower` 逻辑删除 + `defer_unchecked` 物理释放，是 epoch 与无锁数据结构最深度的结合。
3. **正确性保障**：[u7-l3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml) 会讲 loom/miri/tsan 如何检验本讲这些 `SeqCst` fence 与「全员到齐」协议——建议回头用 `ci/crossbeam-epoch-loom.sh` 跑一遍 epoch 的 loom 模型，把本讲的时序论证交给模型检查器确认。
4. **延伸阅读**：对照 RCU（Read-Copy-Update）的「宽限期（grace period）」概念理解 epoch——本讲的「等 2 代」本质就是一种自动化的、基于代次计数的宽限期检测。
