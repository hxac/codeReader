# Global 全局数据与 Bag 队列

## 1. 本讲目标

上一讲（u4-l15）我们看清了「参与者」`Local`：每个线程在某 `Collector` 里堆分配的一份记录，含 `entry` 链表钩子、`bag` 本地垃圾袋、`guard_count`/`handle_count` 双计数器、`pin_count` 与 `epoch`。本讲把视角从「单个参与者」拉到「整个收集器的全局数据中心」——`Global`。

读完本讲，你应当能够：

1. 说清 `Global` 的三个核心字段 `locals` / `queue` / `epoch` 各自的职责，以及它们如何分工协作完成「延迟回收」。
2. 解释 `SealedBag::is_expired` 为什么用 `wrapping_sub(global, sealed) >= 2` 作为安全判据，并能推导「两步安全窗口」的来源。
3. 读懂 `push_bag` 的三步动作（取出旧袋 → `SeqCst` 屏障 → 用当前全局 epoch 盖戳入队），并解释屏障为什么必不可少。
4. 用文字推演一个多线程场景，判断某个垃圾袋最早在全局 epoch 推进到多少时才会被回收。

本讲刻意把 `try_advance` / `collect` 的推进主链路细节留给 u5 单元，只聚焦「全局数据的形态」与「bag 如何被封箱、判定、入队」。

## 2. 前置知识

本讲承接 u4-l15（`Local` 参与者）与 u3-l11（`Deferred` 与 `Bag`）。这里只做最小回顾，不重复展开：

- **延迟闭包 `Deferred`**：一个定长信封，用「函数指针 + 3 个 `usize` 的内联缓冲」手写实现类型擦除的 `FnOnce()`；小闭包内联、大闭包装箱。见 [src/deferred.rs:18-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/deferred.rs#L18-L25)。
- **本地垃圾袋 `Bag`**：就是 `[Deferred; MAX_OBJECTS]` 加一个 `len`。`try_push` 满了就把 `Deferred` 原样 `Err` 还回；`Bag::drop` 时统一执行所有闭包。见 [src/internal.rs:71-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L71-L76)。
- **`Local::defer` 的「满则入队」循环**：往本地袋塞闭包，若 `try_push` 返回 `Err`（袋满），就调用 `Global::push_bag` 把整袋推入全局队列、换一个空袋再塞。见 [src/internal.rs:382-389](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L382-L389)。
- **`Collector` 是 `Arc<Global>` 的薄包装**：所有线程共享同一份 `Global`。见 [src/collector.rs:23-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L23-L26)。

一个直觉：本地袋是「攒着的垃圾」，全局队列是「等宽限期到了再扔的垃圾」。本讲回答两个问题——**全局队列长什么样**，以及**袋从本地搬到全局时被盖了什么戳、何时才算安全可扔**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `src/internal.rs` | 全局数据与参与者的实现 | `Global` 结构、`push_bag`、`collect`、`SealedBag::is_expired`、`Bag::seal` |
| `src/deferred.rs` | 延迟闭包 `Deferred` | 仅回顾其类型擦除表示（u3-l11 已详述） |
| `src/epoch.rs` | epoch 的编码与原子封装 | `wrapping_sub` / `successor` / `pinned` / `unpinned` |
| `src/collector.rs` | 公开类型 `Collector`/`LocalHandle` | 确认 `Collector = Arc<Global>` |
| `src/sync/queue.rs` | Michael-Scott 无锁队列 | `Queue::push` / `try_pop_if`（存放与弹出 `SealedBag`） |

## 4. 核心概念与源码讲解

### 4.1 Global：一个收集器的全局数据中心

#### 4.1.1 概念说明

每个 `Collector` 内部都有一份 `Global`，被 `Arc` 共享给所有注册线程。`Global` 是整个 EBR（epoch-based reclamation）协调的中枢，只持三样东西：

- **`locals`**：一张无锁侵入式链表，挂着所有已注册的 `Local` 参与者。推进 epoch 时要遍历它，看每个参与者钉在哪个 epoch。
- **`queue`**：一个无锁 FIFO 队列，存放「已封箱、等待宽限期」的垃圾袋 `SealedBag`。
- **`epoch`**：全局纪元，一个原子整数。它是整个系统的「时钟」。

三者职责一句话：**`locals` 决定时钟能不能往前走 → `epoch` 给每个垃圾袋盖戳 → `queue` 存放盖好戳、等回收的袋**。

#### 4.1.2 核心流程

从产生垃圾到回收的完整通路（伪代码）：

```text
线程产生垃圾闭包
   └─> Local::defer  ──塞进──>  Local.bag（本地袋，容量 MAX_OBJECTS=64）
                                   │
                                   │ 袋满 / flush() / finalize()
                                   ▼
                              Global::push_bag(bag)
                                   │  1) 取出旧袋，换入空袋
                                   │  2) atomic::fence(SeqCst)
                                   │  3) 读当前全局 epoch，给袋盖戳
                                   │  4) queue.push(SealedBag{ epoch, bag })
                                   ▼
                              Global::queue（全局袋队列）
                                   │
                                   │ 某线程 collect()：try_advance 推进 epoch 后
                                   │ 用 is_expired(global) 逐个试探队首袋
                                   ▼
                   is_expired 为真  ──>  弹出并 drop(SealedBag)
                                   │       └─ Bag::drop 执行全部延迟闭包
                                   ▼
                              垃圾真正析构（宽限期已过）
```

注意：`Global` 把「延迟多久」这件事完全交给 epoch 差值来判定，队列本身**不计时、不轮询**——它只是一个被动的 FIFO 容器，何时弹出由回收线程调用 `collect` 时当场计算。

#### 4.1.3 源码精读

先确认 `Collector` 与 `Global` 的归属关系。`Collector` 只是把 `Arc<Global>` 包了一层，因此同一个 `Collector` 的所有句柄共享同一份 `Global`：

[src/collector.rs:23-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L23-L26) —— `Collector` 内部就是 `global: Arc<Global>`，`new()` 会构造一个新的 `Global`。

`Global` 的结构定义只有三个字段：

[src/internal.rs:164-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L164-L174) —— `Global` 持有 `locals: List<Local>`、`queue: Queue<SealedBag>`、`epoch: CachePadded<AtomicEpoch>`。注意 `epoch` 被 `CachePadded` 包裹以避免多线程读写时的伪共享（false sharing）。

`Global::new` 把三者初始化为空：链表空、队列空、epoch 为 `Epoch::starting()`（即代数 0）：

[src/internal.rs:181-188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L181-L188) —— 构造一个全新的 `Global`，epoch 从 `starting()` 起步。

模块顶部的注释也讲清了这套「本地攒、满则入队、宽限期后回收」的设计动机：

[src/internal.rs:19-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L19-L36) —— 解释了为何要把对象先放进线程局部 bag（摊薄同步开销），袋满才盖戳入全局队列；全局队列不能被直接访问，只能通过 `defer()`（入袋）和 `collect()`（回收）交互。

#### 4.1.4 代码实践

**实践目标**：在源码里亲手定位 `Global` 的三个字段，并追踪谁在读写它们。

**操作步骤**：

1. 打开 [src/internal.rs:164-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L164-L174)，确认 `Global` 的三个字段。
2. 用编辑器搜索 `self.locals`、`self.queue`、`self.epoch`，分别在哪些方法里出现。
3. 记录：`locals` 在 `try_advance` 里被遍历；`queue` 在 `push_bag`（入队）和 `collect`（出队）里出现；`epoch` 在 `push_bag`（读，盖戳）、`try_advance`（读+写，推进）、`Local::pin`（读，快照）里出现。

**需要观察的现象**：`epoch` 字段被读得最频繁（pin、push_bag、try_advance 都读），但只有 `try_advance` 一处会写它（推进）。`locals` 只有遍历、没有直接「写入」——因为插入/删除由侵入式链表 `List` 自己管理。

**预期结果**：三个字段各有清晰分工——`locals` 是「参与者花名册」、`queue` 是「垃圾袋仓库」、`epoch` 是「全局时钟」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Global` 不直接维护一个「待回收对象」的扁平列表，而要用 `List<Local>` + `Queue<SealedBag>` 两层结构？

**参考答案**：`List<Local>` 是「参与者花名册」，回答的是「现在有哪些线程、它们钉在哪个 epoch」——这是推进全局时钟的依据；`Queue<SealedBag>` 是「已封箱垃圾袋」，回答的是「哪些垃圾袋可以扔了」。两者数据语义不同：前者按线程切分、随注册/注销动态增减；后者按「袋」切分、随宽限期批量消失。混在一起会让推进逻辑与回收逻辑耦合。

**练习 2**：`Global.epoch` 用 `CachePadded<AtomicEpoch>` 包裹，`Local.epoch` 也用 `CachePadded`（见 u4-l15）。为什么都要包？

**参考答案**：`epoch` 是跨线程热字段——回收线程会频繁读所有 `Local.epoch`，参与者线程频繁写自己的 `Local.epoch`，还会频繁读写 `Global.epoch`。`CachePadded` 把它填充到独立缓存行，避免多个线程在同一缓存行上「乒乓」失效（伪共享），这是无锁热点字段的标配处理。

---

### 4.2 SealedBag：给垃圾袋盖一个 epoch 戳

#### 4.2.1 概念说明

`Bag` 本身只是「一堆延迟闭包」，它**不知道**自己什么时候安全可执行。要让它能被安全回收，必须在入全局队列时给它盖一个「时间戳」——当时的全局 epoch。盖了戳的袋就是 `SealedBag`：

\[ \text{SealedBag} = \{\ \text{epoch}: \text{当时全局 epoch},\ \ \text{bag}: \text{袋内闭包}\ \} \]

回收线程拿到当前全局 epoch 后，只需比较两个 epoch 的差距，就能判断袋里的闭包是否安全执行。判据是 `SealedBag::is_expired`：

\[ \text{is\_expired}(g_{\text{now}}, e_{\text{sealed}}) \iff g_{\text{now}} - e_{\text{sealed}} \geq 2 \]

也就是「全局 epoch 相对盖戳时已经前进了**至少两步**」。这就是 EBR 所谓的「两步安全窗口」（grace period）。

#### 4.2.2 核心流程：为什么是「两步」

关键不变量（来自 `is_expired` 的源码注释）：**一次 pin 最多只能见证一次 epoch 推进**。

推导如下：

1. 设对象在全局 epoch 为 \(S\) 时成为垃圾（袋在 \(S\) 盖戳）。
2. 某参与者可能在 \(S\) 被 pin，此刻它正拿着指向该对象的指针。
3. 全局 epoch 能从 \(S\) 推进到 \(S+1\)，前提是「当前所有被 pin 的参与者都钉在 \(S\)」（`try_advance` 的检查，见 u5 单元）。所以推进到 \(S+1\) 时，那个参与者**仍可能**钉在 \(S\)、仍可能拿着指针——它只能见证这一次推进。
4. 全局要从 \(S+1\) 再推进到 \(S+2\)，前提变成「所有被 pin 的参与者都钉在 \(S+1\)」。这意味着原来钉在 \(S\) 的参与者必须已经 **re-pin 或 unpin**——即离开了 \(S\) 那个临界区，不再持有旧指针。
5. 因此当全局达到 \(S+2\) 时，没有任何参与者还能持有 \(S\) 时代的指针，袋里闭包执行（析构对象）是安全的。

所以「两步」不是拍脑袋的常数，而是由 `try_advance` 的推进条件 + 「一次 pin 最多见证一次推进」共同决定的下界。少一步（只前进到 \(S+1\)）就不安全。

**距离怎么算**：epoch 的内部编码是「最低位作 pinned 标志，其余位作代数」（详见 u5-l17）。`wrapping_sub` 先抹掉右操作数的 pinned 位、再做带符号减法、最后算术右移一位（除以 2），得到的就是「代数差」：

[src/epoch.rs:45-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L45-L54) —— `wrapping_sub` 计算「self 比 rhs 领先多少代」，用带符号整数表达，能正确处理回绕。

`successor` 每次给代数加 1（data 加 2，因为最低位是 pin 标志）：

[src/epoch.rs:78-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L78-L86) —— 推进一代就是 `data.wrapping_add(2)`，且保留原来的 pinned 位。

#### 4.2.3 源码精读

`SealedBag` 的定义极简——一个 epoch 加一个 bag：

[src/internal.rs:145-150](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L145-L150) —— `SealedBag { epoch: Epoch, _bag: Bag }`。

它手动实现了 `Sync`，理由写得很清楚——只有 `is_expired` 会跨线程读它，而 `is_expired` **只看 epoch 字段**，不碰 `_bag`：

[src/internal.rs:152-153](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L152-L153) —— `unsafe impl Sync for SealedBag`，因为共享访问仅限 `is_expired` 读 epoch。

核心判定：

[src/internal.rs:155-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L155-L162) —— `is_expired` 返回 `global_epoch.wrapping_sub(self.epoch) >= 2`。注释点明「一次 pin 最多见证一次推进，因此与当前 epoch 相差一代以内的袋还不能销毁」。

盖戳的动作在 `Bag::seal`，它消费 `self`、附上 epoch、产出 `SealedBag`：

[src/internal.rs:110-113](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L110-L113) —— `Bag::seal(self, epoch)` 把袋和 epoch 组成 `SealedBag`。

epoch.rs 顶部的模块注释也直接点出了「两步」的安全性根源：

[src/epoch.rs:6-8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L6-L8) —— 「若对象在某 epoch 成为垃圾，那么经过两次推进后，必无参与者还持有它的引用」。

#### 4.2.4 代码实践

**实践目标**：手算几个 `wrapping_sub` 值，验证你对 `>= 2` 判据的理解。

**约定记号**：把 epoch 的「代数」\(g\) 定义为 `data / 2`。全局 epoch 恒为 unpinned，所以 data 是偶数 \(0, 2, 4, 6, \ldots\)，对应代数 \(g = 0, 1, 2, 3, \ldots\)。`wrapping_sub(global, sealed)` 在代数层面就是 \(g_{\text{now}} - g_{\text{sealed}}\)。

**操作步骤**：填下面这张表（先用纸笔算，再对照答案）。

| 盖戳代数 \(g_{\text{sealed}}\) | 当前全局代数 \(g_{\text{now}}\) | `wrapping_sub` | `is_expired`？ |
|---|---|---|---|
| 2 | 2 | 0 | 否 |
| 2 | 3 | 1 | ? |
| 2 | 4 | 2 | ? |
| 2 | 5 | 3 | ? |

**预期结果**：

| 盖戳代数 | 当前全局代数 | `wrapping_sub` | `is_expired`？ |
|---|---|---|---|
| 2 | 2 | 0 | 否（差 0） |
| 2 | 3 | 1 | **否**（差 1，还差一步） |
| 2 | 4 | 2 | **是**（差 2，达到下界） |
| 2 | 5 | 3 | 是（差 3，更安全） |

**对照源码验证**：把上表第一行的 data 值代入 [src/epoch.rs:45-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L45-L54)：sealed 代数 2（data=4）、global 代数 4（data=8），`(8 - (4 & !1)) >> 1 = (8-4) >> 1 = 2`，与代数差一致。

> 说明：本表为「源码阅读型」推演，结论可直接由 `wrapping_sub` 定义得出，无需运行；若要在机器上验证，可参考 u5-l17 的 `Epoch` 单测思路自行构造（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`is_expired` 用的是 `global_epoch.wrapping_sub(self.epoch)`，注意第二个参数是 `self.epoch`（盖戳时的全局 epoch，恒 unpinned）。如果某处误把一个 **pinned** 的 epoch 当作 sealed 戳存进去，`wrapping_sub` 还正确吗？

**参考答案**：仍然正确。`wrapping_sub` 内部对右操作数做了 `rhs.data & !1`，先抹掉了 pinned 位再相减（见 [src/epoch.rs:49-53](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L49-L53) 的注释）。这是有意的健壮设计——无论右操作数带不带 pinned 位，结果一致。当然，实际代码里 sealed 戳来自 `Global.epoch.load()`，恒为 unpinned，这层冗余只是防御。

**练习 2**：为什么阈值是 `>= 2` 而不是 `> 2`（即差 2 时到底算不算安全）？

**参考答案**：差 2 时**安全**，所以用 `>= 2` 把差 2 包含进来。按 4.2.2 的推导，全局达到 \(S+2\) 时所有 \(S\) 时代的 pin 都已失效，此时回收恰好安全；若改成 `> 2`（即要求差 3），就会让袋多等一代才回收，徒增内存占用而不增加安全性。

---

### 4.3 push_bag：fence + 盖戳 + 入队

#### 4.3.1 概念说明

`Global::push_bag` 是「本地袋 → 全局队列」的唯一入口。它做三件事：

1. **取出旧袋**：用 `mem::replace` 把调用方传入的本地袋换成一个新的空袋（调用方拿到空袋继续用）。
2. **全屏障**：执行 `atomic::fence(Ordering::SeqCst)`。
3. **盖戳入队**：读当前全局 epoch，用 `Bag::seal(epoch)` 封箱，再 `queue.push` 追加到队尾。

其中第 2 步（`SeqCst` 屏障）是正确性的关键，**不能省**。

#### 4.3.2 核心流程

为什么屏障必须夹在「取出袋」和「盖戳」之间？考虑这个序列：

```text
线程 A：把对象 O 写入数据结构（Release store）
        把「析构 O」的闭包塞进本地袋
        ... 袋满 ...
        push_bag：
            取出旧袋
            fence(SeqCst)        <-- 关键
            读全局 epoch = S，盖戳
            queue.push
```

回收线程稍后看到「戳为 \(S\) 的袋」，会等到全局 epoch 到 \(S+2\) 才执行「析构 O」。但「析构 O」要安全，前提是 A 当初「把 O 写入数据结构」的写入对回收线程**可见**——否则回收线程可能在 O 还没真正发布时就把它析构了。

`fence(SeqCst)` 在这里起两个作用：

- 它与 `Local::pin` 里 pin 后的 `SeqCst` 屏障配对，构成跨线程的 happens-before 关系（详见 u5-l18）。直观地说：**先确保「产生垃圾之前的所有写入」全局可见，再去读 epoch 盖戳**。
- 保证「盖戳用的 epoch」不会被重排到「产生垃圾的写入」之前——否则可能盖了一个更早的戳，导致回收线程误判宽限期已过。

一句话总结 `push_bag` 的不变量：**戳所记录的 epoch，必须不早于「袋内闭包所操作数据被发布」的时刻**。屏障就是这条不变量的执行手段。

#### 4.3.3 源码精读

`push_bag` 全文很短：

[src/internal.rs:190-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L190-L198) —— 先 `mem::replace` 换出旧袋并 `Bag::new()` 装回空袋；`atomic::fence(SeqCst)`；`self.epoch.load(Relaxed)` 读当前全局 epoch（注意读 epoch 用 `Relaxed` 即可，因为屏障已经保证了所需的可见性顺序）；`bag.seal(epoch)` 封箱；`self.queue.push(...)` 入队。

注意一个细节：`self.epoch` 这里的 `self` 是 `&Global`，所以读的是**全局 epoch**，不是参与者的 local epoch。袋盖的是「入队那一刻的全局时钟」，这与 4.2 的安全判据完全对应。

**谁会调用 `push_bag`**？三处：

1. `Local::defer` 的「满则入队」循环——袋满时换袋：

   [src/internal.rs:382-389](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L382-L389) —— `while let Err(d) = bag.try_push(deferred)` 循环里调用 `self.global().push_bag(bag, guard)`。

2. `Local::flush`——主动把未满的袋也推入队列并立即 `collect`：

   [src/internal.rs:391-399](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L391-L399) —— 只要袋非空就 `push_bag`，然后 `collect`。

3. `Local::finalize`——线程退会时，把残留垃圾袋推入队列再销毁 `Local`：

   [src/internal.rs:542-549](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L542-L549) —— 临时 pin 后 `push_bag` 把本地袋入队。

**入队的容器**是 Michael-Scott 无锁队列，`push` 把 `SealedBag` 装进新节点 CAS 链到队尾：

[src/sync/queue.rs:99-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L99-L116) —— `Queue::push` 循环读取 tail 快照，用 `push_internal` 尝试 CAS 链接新节点，失败则重试（无锁细节留待 u6-l21）。

**出队回收**发生在 `Global::collect`，它先 `try_advance` 拿到最新全局 epoch，再用 `try_pop_if` 弹出最多 `COLLECT_STEPS`（8）个过期袋：

[src/internal.rs:200-226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L200-L226) —— `collect` 用 `sealed_bag.is_expired(global_epoch)` 作弹出条件，弹出的 `SealedBag` 被 `drop`，进而触发 `Bag::drop` 执行全部延迟闭包。

`try_pop_if` 接受一个条件闭包，在队首节点上判定是否满足条件才弹出（这就是 `SealedBag` 必须 `Sync` 的原因——条件闭包会跨线程读 `SealedBag`）：

[src/sync/queue.rs:188-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L188-L202) —— `try_pop_if` 循环调用 `pop_if_internal(&condition)`，命中条件才 CAS 弹出。

> 关于 `try_advance` 如何遍历 `locals` 判定能否推进、`collect` 的增量步进细节，本讲点到为止，u5-l19 会完整拆解。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：用文字推演一个双线程场景，亲手算出「袋最早何时能被回收」，把 `Global` 三字段、`is_expired`、`push_bag` 三者串起来。

**场景**（对应本讲指定的实践任务）：

- 全局 epoch 起始为代数 0。
- 假设经过若干次推进，**线程 A 在全局 epoch = 代数 2 时 pin**，并产生垃圾。
- A 在全局仍为代数 2 时触发 `push_bag`（例如袋满或 `flush`），于是这只袋的盖戳 epoch \(S = 2\)。
- 与此同时，**线程 B 持续 pin / collect**，不断尝试推进全局 epoch。

**操作步骤（按代数逐步推演）**：

1. A 在代数 2 pin：A 的 local epoch = 代数 2（pinned）。A 产生垃圾，袋被盖戳 \(S=2\) 入队。
2. B 调用 `collect` → `try_advance`：当前全局代数 2，遍历 `locals`，发现 A 钉在代数 2 == 全局 2，**不阻挡**。推进成功：全局 → 代数 3。
3. B 再次 `try_advance`：当前全局代数 3，A 仍钉在代数 2 ≠ 3，**A 阻挡**。全局**卡在代数 3**。
4. **A unpin**：A 的 local epoch 归零（unpinned），不再阻挡。
5. B 再次 `try_advance`：所有参与者要么 unpinned、要么钉在当前代数 3，**不阻挡**。推进成功：全局 → 代数 4。
6. `collect` 用 `is_expired(全局代数 4, 戳 S=2)` 判定：\(4 - 2 = 2 \geq 2\) → **过期，可回收**。弹出该 `SealedBag` 并 `drop`，执行袋内全部延迟闭包。

**结论**：

- A 的袋最早在**全局 epoch 达到代数 4** 时才会被 `is_expired` 判为可回收（即盖戳代数 +2）。
- 由于 A 一直钉在代数 2，全局 epoch 在 A unpin 前最多只能到代数 3（被 A 挡住第二步），所以**只要 A 还持有 pin，这只袋就不可能被回收**——这正好印证了 u3-l12 讲过的「长持有 guard 会拖慢 GC」。

**「两步」安全窗口的来源**（呼应 4.2.2）：一次 pin 最多见证一次推进。A 钉在代数 2，能见证 2→3 这一次推进；但它**不可能**见证 3→4（那要求 A 已 re-pin 到 3 或 unpin）。所以当代数到 4 时，A 必已离开代数 2 的临界区，不再持有旧指针——此时析构袋内对象是安全的。

**需要观察的现象（若你在本地复现）**：用一个带 `Drop` 计数的类型（参考 [src/collector.rs:285-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L285-L315) 的 `count_drops` 测试），让 A 长时间持有 guard 不放，会发现 `DROPS` 计数迟迟不增长；A 一旦 unpin（或 `repin`），下一次 `collect` 后计数才跳升。此现象**待本地验证**，但可由上述推演预测。

#### 4.3.5 小练习与答案

**练习 1**：`push_bag` 里读 epoch 用的是 `Ordering::Relaxed`（[src/internal.rs:196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L196)），为什么不需要更强的 Ordering？

**参考答案**：因为紧邻的上一行 `atomic::fence(SeqCst)` 已经建立了所需的跨线程可见性与顺序保证。fence 之前的所有写入（产生垃圾、把对象发布到数据结构）对读 epoch 之后的代码（以及未来的回收线程）都可见且有序。读 epoch 本身只是「盖个戳」，不需要再额外同步，`Relaxed` 足矣。这正是屏障的「集中同步」用法：用一次重操作换取前后普通操作的轻量。

**练习 2**：如果某线程调用 `guard.flush()`，袋被 `push_bag` 入队了，但全局 epoch 此后**一直没推进**（比如没有其他线程在 pin/collect）。这只袋会被回收吗？

**参考答案**：不会。`is_expired` 需要 `global - sealed >= 2`，若全局 epoch 不推进，差值永远不够。`flush` 只负责把袋推入队列，**不负责**推进时钟——推进由 `try_advance`（在 `collect`/周期性 pin 里触发）完成。所以「入队」与「回收」是两个独立事件，中间隔着宽限期。这也解释了为什么 `flush` 只是「加速回收」而不是「保证立即回收」（见 u1-l3 的结论）。

**练习 3**：`push_bag` 用 `mem::replace(bag, Bag::new())` 把传入袋换成空袋。为什么不直接 `&mut Bag` 让调用方自己清空？

**参考答案**：`mem::replace` 一次性完成「搬走旧袋 + 装回空袋」，调用方（`Local::defer` / `flush` / `finalize`）拿回的指针立刻指向一个可继续 `try_push` 的空袋，逻辑简单且无空窗期。若让调用方自己清空，需要在「取出旧袋」和「放回空袋」之间处理失败路径（比如 `queue.push` 万一重试），容易出错。`replace` 把这件事封装成原子语义，是惯用写法。

---

## 5. 综合实践

把本讲三块内容（`Global` 结构、`is_expired` 判据、`push_bag` 时机）串成一个小任务。

**任务**：阅读 [src/collector.rs:250-282](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L250-L282) 的 `buffering` 测试，回答下列问题，并画一张「时间 vs 代数」的推演图。

测试骨架回顾：

- 主线程 pin 一个 guard，循环 `defer_unchecked` 推入 10 个析构闭包，**不 flush**。
- 然后另开循环反复 `collector.global.collect(&handle.pin())`。
- 断言：循环很多次后 `DESTROYS < COUNT`（即 10 个对象**还没全回收**）。
- 接着 `handle.pin().flush()`。
- 再循环 `collect`，直到 `DESTROYS == COUNT`。

**需要回答 / 推演**：

1. 为什么第一阶段反复 `collect` 后，`DESTROYS` 仍 `< 10`？提示：主线程那只长期 guard 一直钉在某个代数，阻挡了 epoch 的第二次推进；且垃圾还没 `flush` 入队。
2. `flush()` 做了什么，让回收得以发生？提示：`flush` 调用 `push_bag`（盖戳入队）+ `collect`（推进 + 试探 `is_expired`）。
3. 画出主线程 local epoch 与全局 epoch 的代数变化：起始都为 0；主线程 pin 后 local = 0(pinned)；某次 `collect` 的 `try_advance` 把全局推进到代数 1（主线程钉在 0 == 全局 0 时不阻挡，但推进到 1 后主线程钉在 0 ≠ 1，卡住）；`flush` 前袋尚未入队；`flush` 时袋盖戳 \(S=\) 当时代数（待确认，可能是 1）；后续 `collect` 推进到 \(S+2\) 时回收。

**预期产出**：

- 一段中文解释：`flush` 之前袋还在 `Local.bag` 里没进 `queue`，`collect` 根本看不到它；即使入队了，长 pin 也会让全局 epoch 卡在「戳 +1」处，差值到不了 2。
- 一张推演表（代数、主线程是否 pinned、袋是否入队、`is_expired` 结果）。

> 说明：第 3 问里 `flush` 那一刻的全局代数取决于运行时调度，**待本地验证**；但无论盖戳代数 \(S\) 是多少，回收一定发生在全局到 \(S+2\) 之后——这是本讲的核心结论。

## 6. 本讲小结

- `Global` 是每个 `Collector` 的全局数据中心，只持三个字段：`locals`（参与者链表，决定时钟能否推进）、`queue`（已封箱垃圾袋队列）、`epoch`（全局时钟）。
- `SealedBag = Epoch + Bag`：袋入全局队列前会被盖一个「当时全局 epoch」的戳；回收时用 `is_expired(global) = global.wrapping_sub(sealed) >= 2` 判定。
- 「两步」安全窗口的根源是不变量「一次 pin 最多见证一次 epoch 推进」：全局前进两步后，盖戳时所有 pin 都已离开旧临界区，析构才安全。
- `push_bag` 三步走：`mem::replace` 取出旧袋换空袋 → `atomic::fence(SeqCst)`（保证「产生垃圾的写入」在盖戳前全局可见）→ 读全局 epoch 盖戳 → `queue.push`。
- 袋真正被回收发生在 `collect`：`try_advance` 推进 epoch 后，用 `try_pop_if` 弹出最多 8 个 `is_expired` 的袋并 `drop`（触发 `Bag::drop` 执行闭包）。
- 长持有 guard 会阻挡 epoch 第二次推进，从而拖慢回收——这正是 `repin`/`flush` 存在的理由（呼应 u3-l12）。

## 7. 下一步学习建议

本讲把「全局数据形态」与「bag 封箱/判定/入队」讲清了，但故意把两件事留给了后面：

- **`try_advance` 如何遍历 `locals`、`collect` 的增量步进、`finalize` 的销毁时序** → 这是 u5 单元的主线，建议紧接着读 **u5-l19（try_advance 与 collect）**，它会补全「时钟到底怎么推进、袋怎么被逐个弹出」。
- **epoch 的位编码细节、pin/unpin 的内存屏障（含 x86 hack）** → 见 **u5-l17（Epoch 表示）** 与 **u5-l18（pin/unpin 与内存屏障）**。读完你就能严格解释 `push_bag` 里那个 `SeqCst` fence 与 `Local::pin` 里的 fence 是如何配对构成 happens-before 的。
- **无锁队列本身的实现** → 本讲的 `queue.push` / `try_pop_if` 只当黑盒用了，想知道 CAS 链接、tail 滞后、helping 等细节，见 **u6-l21（Michael-Scott 队列）**。
