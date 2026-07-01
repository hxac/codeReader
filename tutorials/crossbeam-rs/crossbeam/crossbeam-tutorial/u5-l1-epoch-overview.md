# epoch 内存回收：问题背景与 API

> 所属单元：u5 crossbeam-epoch：基于 epoch 的内存回收
> 前置讲义：u2-l3 AtomicCell 任意类型原子单元

## 1. 本讲目标

本讲是 crossbeam-epoch 单元的**入门篇**。我们不深入任何算法实现，只回答三个问题：

1. 无锁（lock-free）数据结构在「删除一个节点」时为什么会遇到内存回收难题？
2. 为什么用「epoch（代）」来延迟回收就能安全地解决它？
3. crossbeam-epoch 暴露给使用者的公共 API 长什么样、入口在哪里？

学完后你应当能够：

- 用自己的话讲清楚「读取方正在读、删除方想 free」的竞争场景；
- 解释「一个对象变成垃圾后，至少要等 2 个 epoch 推进才能销毁」这条核心安全判据；
- 画出 `pin` → `Guard` → `Atomic`/`Shared`/`Owned` → `defer` 的 API 关系图，并知道 `pin()`、`is_pinned()`、`default_collector()` 这三个全局入口来自哪个文件。

本讲**只精读** `crossbeam-epoch/src/lib.rs` 与 `crossbeam-epoch/src/default.rs` 两个文件，对 `atomic.rs`、`guard.rs`、`collector.rs`、`internal.rs`、`epoch.rs` 仅做「指路式」引用——它们各自有专门的后续讲义（u5-l2 ~ u5-l5）展开。

## 2. 前置知识

本讲需要你先具备以下认知（若不熟悉，建议先回到对应讲义）：

- **原子操作与 CAS**（来自 u2-l3）：`load`/`store`/`compare_exchange` 的语义。epoch 的指针操作全部建立在 `AtomicPtr` 之上。
- **并发数据结构为什么需要原子指针**：链表、栈、队列等结构在多线程下靠「把节点地址塞进一个原子指针」来共享。
- **RAII**：Rust 靠 `Drop` 在值离开作用域时自动清理资源。本讲的 `Guard` 就是典型的 RAII 句柄。
- **生命周期 `'g`**：与借用检查相关。你会看到 `Shared<'g, T>` 这个带生命周期参数的指针类型。

几个本讲会反复用到的术语，先在这里统一约定：

| 术语 | 含义 |
|------|------|
| 无锁数据结构（lock-free） | 多线程读写不靠互斥锁，而靠原子指令（如 CAS）协调 |
| 内存回收（memory reclamation） | 决定「何时安全地释放一个被多个线程可能还引用着的对象」 |
| epoch（代） | 一个单调递增（回绕）的全局整数，用来给「垃圾」打时间戳 |
| pin（固定） | 线程声明「我现在正在读结构，请暂时不要回收我可能看到的对象」 |
| 参与者（participant） | 注册到收集器、可以 pin/unpin 的执行体（通常对应一个线程） |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到它做什么 |
|------|------|------------------|
| [crossbeam-epoch/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs) | crate 根：模块文档（问题背景 + epoch 思想）、`#![no_std]`、子模块声明、公共 API 的 `pub use` 重导出 | **核心精读**：问题陈述、epoch 原理、对外类型清单 |
| [crossbeam-epoch/src/default.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs) | 默认全局垃圾收集器：惰性初始化的全局 `Collector`、每线程参与者的 thread-local、`pin`/`is_pinned`/`default_collector` 三个入口 | **核心精读**：全局入口函数 |
| [crossbeam-epoch/src/epoch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs) | `Epoch` 类型的定义（最低位作 pinned 标记、`successor` 推进） | 辅助引用：理解「推进一代」的物理含义 |
| [crossbeam-epoch/src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | `Global`/`Local`、`Bag`/`SealedBag`、`try_advance`/`collect`/`pin` 的实现 | 辅助引用：只看「2 epoch 判据」与结构轮廓，细节留待 u5-l5 |
| [crossbeam-epoch/src/guard.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs) | `Guard`（pin 的 RAII 凭证）、`defer`/`defer_destroy`/`unprotected` | 辅助引用：API 表面 |
| [crossbeam-epoch/src/collector.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs) | `Collector`（收集器句柄）、`LocalHandle`（参与者句柄） | 辅助引用：API 表面 |
| [crossbeam-epoch/src/atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs) | `Atomic<T>`/`Owned<T>`/`Shared<'g,T>` 三种指针 | 辅助引用：API 表面 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应三个递进的问题：**有什么问题 → epoch 怎么解决 → 我该怎么用**。

### 4.1 模块一：问题背景——删除与并发读取的竞争

#### 4.1.1 概念说明

设想一个无锁链表。线程 A 正在遍历它，手里拿着指向节点 N 的指针；与此同时，线程 B 用 CAS 把节点 N 从链表里「摘除」了。摘除本身只是改了前驱节点的 `next` 指针——N 这个堆对象还好好地在那里。问题是：**B 现在能 `free(N)` 吗？**

不能。因为 A 手里还攥着指向 N 的指针，可能下一行就要解引用它。如果 B 这时释放了 N，A 就会读到已释放内存（use-after-free），这是未定义行为。

这就是 crossbeam-epoch 在模块文档里第一段就点出的核心痛点：

> An interesting problem concurrent collections deal with comes from the remove operation. Suppose that a thread removes an element from a lock-free map, while another thread is reading that same element at the same time. The first thread must wait until the second thread stops reading the element. Only then it is safe to destruct it.

在**有 GC 的语言**（Java/Go 等）里，这件事几乎不成问题：只要还有任何引用，GC 就不会回收；当最后一个引用消失，GC 自然释放。所以这些语言里写无锁结构相对省心。

但在 **Rust（以及 C/C++）** 里没有全局 GC，内存释放是程序员手动/自动（RAII）管理的。于是无锁数据结构必须**自己发明一套「安全回收」机制**，用来判断「一个被摘除的节点，到底什么时候才没有别的线程还在读它」。这类机制统称 **memory reclamation（内存回收）**，主流方案有三类：

- **引用计数**（如 `Arc`）：每个节点带计数，但原子增减计数开销大，且回收时机不确定。
- **危险指针 Hazard Pointer**：每个读者登记「我正在看这个地址」，删除方回收前扫一遍登记表。
- **基于 epoch / RCU 的延迟回收**：不精确追踪单个节点，而是用「时间代」粗粒度地保证「过了足够久，旧读者一定都退场了」。crossbeam-epoch 选的就是这条路。

> 关键词 `rcu`、`lock-free`、`garbage` 正是 `crossbeam-epoch` 在 `Cargo.toml` 里给自己打的关键字（见 [crossbeam-epoch/Cargo.toml:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L15)）。

#### 4.1.2 核心流程

把上面的竞争抽象成一张时序，体会「为什么删除方不能立即释放」：

```
线程 A (读者)                      线程 B (删除者)
─────────────                      ─────────────
1. load 得到指针 p -> 节点 N
                                   2. CAS：把 N 从链表摘除（改前驱的 next）
                                   3. 想释放 N…… ❌ 此时不能 free！
4. 解引用 *p（读 N 的数据）          ── 仍在使用 N
5. 读完了，丢弃 p
                                   6. ✅ 现在释放 N 才安全
```

难点在于：步骤 3 和步骤 4 之间，B 完全无法知道 A 还在不在用 N。无锁结构没有锁，B 也没有办法「等 A 读完」——那等于退化成加锁。

epoch 方案的关键洞察是：**B 不需要精确知道 A 何时读完，只需要保证「在 A 可能还在读的整个时间窗口之外」才释放。** 下一个模块就讲这个窗口怎么界定。

#### 4.1.3 源码精读

这段问题陈述写在 crate 的模块文档里，是理解整个 crate 的「开场白」：

[crossbeam-epoch/src/lib.rs:1-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L1-L21)

```rust
//! Epoch-based memory reclamation.
//!
//! An interesting problem concurrent collections deal with comes from the remove operation.
//! Suppose that a thread removes an element from a lock-free map, while another thread is reading
//! that same element at the same time. The first thread must wait until the second thread stops
//! reading the element. Only then it is safe to destruct it.
//!
//! Programming languages that come with garbage collectors solve this problem trivially. The
//! garbage collector will destruct the removed element when no thread can hold a reference to it
//! anymore.
```

要点：

- 第 1 行的标题就是全 crate 的定位：**Epoch-based memory reclamation**（基于代/纪元的内存回收）。
- 第 3-6 行：用「一边删除、一边读取」的例子点出问题。
- 第 8-10 行：点明 GC 语言「平凡地」解决了它——这正是后续实践题要你深入思考的对照点。

README 里也用一句话给了 epoch 一个面向用户的定位（注意它标注了 `alloc`，意味着需要堆分配）：

[crossbeam-epoch 在 README 中的归类:30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/README.md#L30) ——「Memory management / `epoch`, an epoch-based garbage collector. <sup>(alloc)</sup>」

#### 4.1.4 代码实践

**实践目标**：理解「GC 语言为什么让无锁结构更省心」这件事，并把它写下来。

**操作步骤**：

1. 打开 [crossbeam-epoch/src/lib.rs:1-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L1-L21)，反复读第 3-10 行。
2. 用一段话（3-5 句中文）回答：**为什么在 GC 语言里写无锁数据结构更简单？** 提示：思考「释放时机的判定责任在谁身上」——GC 语言里这个责任由运行时承担，Rust 里则要由数据结构作者自己承担。
3. 在仓库根目录执行：

   ```bash
   cargo doc -p crossbeam-epoch --no-deps --open
   ```

   这会只编译 `crossbeam-epoch` 自己的文档并在浏览器打开。

**需要观察的现象**：

- 文档页面顶部应能看到与本模块文档一致的「Epoch-based memory reclamation」说明。
- 左侧目录里能看到 `Atomic`、`Shared`、`Owned`、`Guard`、`Collector`、`pin`、`is_pinned`、`default_collector`、`unprotected` 等条目——这就是本 crate 的全部公共表面。

**预期结果**：你能列出 epoch crate 公开的核心类型清单，并口头复述「删除方需等读取方停手才能析构」这一约束。命令能否成功打开浏览器取决于本地环境；若 `--open` 无效，可改用 `cargo doc -p crossbeam-epoch --no-deps` 后手动打开 `target/doc/crossbeam_epoch/index.html`。**命令的实际运行结果待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：在「删除与读取竞争」的时序里，为什么线程 B 不能在摘除节点 N 后立刻 `drop` 它？

> **参考答案**：因为线程 A 可能正持有指向 N 的指针并即将解引用。立即释放会导致 A 读到已释放内存（use-after-free）。摘除操作只改了链表的前驱指针，并不代表「没有人还在读 N」。

**练习 2**：把 epoch 的内存回收与「给链表整体加一把 `Mutex`」对比，各有什么主要代价？

> **参考答案**：加 `Mutex` 能让删除方持有锁时确信没有并发读者，简单但牺牲了无锁的并发度（临界区串行化、可能阻塞、有上下文切换开销）。epoch 保持无锁，读路径几乎零同步开销，但代价是需要延迟回收、需要 pin/unpin 协议、实现复杂度高。epoch 用「回收稍晚一点」换「读路径不阻塞」。

---

### 4.2 模块二：epoch 回收原理——标记 → 延迟 → 销毁

#### 4.2.1 概念说明

epoch 方案的核心思想是：**不逐个追踪「谁在读哪个节点」，而是给整个系统打一个全局时间戳（epoch，代），用一个粗粒度的规则保证安全。**

可以把它想象成一家博物馆的参观管理：

- 全场有一个不断递增的「展期编号」（全局 epoch）。
- 任何想看展品（读数据结构节点）的人，进门前先**登记当前展期编号**并领取一个参观牌（这就是 `pin`，参观牌就是 `Guard`）。
- 有人要撤走一件展品（删除节点）时，他不立刻搬走，而是把展品贴上「撤于第 K 展期」的标签，扔进**仓库**（垃圾袋 `Bag`）。
- 只有当「全场所有还挂着参观牌的人，登记的展期编号都新到一定程度」时，仓库里贴着旧标签的展品才被真正销毁。

关键的安全直觉是：**一个参观者从登记到离场，最多只能见证一次展期推进。** 因此，只要一件垃圾「贴标签时的展期」与「当前展期」相差达到 2，就绝不可能还有参观者拿着旧牌子在摸它——可以安全销毁了。

#### 4.2.2 核心流程

把 epoch 回收写成三个阶段：

```
① 标记 (mark)
   删除方把对象从结构里摘除后，连同「当前全局 epoch」一起塞进垃圾袋 (Bag)。
   此时对象不再被结构引用，但可能还被某个 pin 中的线程读着。

② 延迟 (delay)
   垃圾袋留在「线程局部」或「全局队列」里，静静等待。
   每次有线程 pin，都会偶尔尝试 try_advance（推进全局 epoch）+ collect（回收）。

③ 销毁 (destroy)
   回收时检查每个垃圾袋：若 global_epoch - 该袋 epoch >= 2，则销毁其中所有对象。
```

「为什么是 2 而不是 1」是这个机制最精妙之处，源码里有专门注释解释，下面会引用。用数学语言表述这条判据：

\[ \text{可安全销毁} \iff \text{global\_epoch} \ominus \text{bag\_epoch} \geq 2 \]

其中 \(\ominus\) 表示带回绕的差（`wrapping_sub`）。「至少差 2」的证明依赖于上一节的直觉：**一个 pin 中的参与者最多见证一次 epoch 推进**，因此差距达到 2 意味着「打标签那一刻在场的所有读者，此刻必然早已离场」。

全局 epoch 的推进也有一条严格规则，写在 `epoch.rs` 文档里：

> A pinned participant may advance the global epoch only if all currently pinned participants have been pinned in the current epoch.

也就是说：**只有当所有当前 pin 的人登记的都是「当前这一代」时，才允许推进到下一代。** 这保证了「推进」不会把任何还在旧代里读的人甩下——它最多滞后一代。

#### 4.2.3 源码精读

epoch 思想的总纲写在 `lib.rs` 模块文档的第 12-20 行：

[crossbeam-epoch/src/lib.rs:12-20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L12-L20)

```rust
//! This crate implements a basic memory reclamation mechanism, which is based on epochs. When an
//! element gets removed from a concurrent collection, it is inserted into a pile of garbage and
//! marked with the current epoch. Every time a thread accesses a collection, it checks the current
//! epoch, attempts to increment it, and destructs some garbage that became so old that no thread
//! can be referencing it anymore.
```

「mark → delay → destroy」三步与这段描述一一对应：marked with the current epoch（标记）→ inserted into a pile of garbage（延迟）→ destructs some garbage that became so old（销毁）。

「至少差 2 才安全」这条判据，是整个机制的命门，它有两处权威出处。第一处在 `epoch.rs` 模块文档结尾：

[crossbeam-epoch/src/epoch.rs:1-9](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L1-L9)

```rust
//! If an object became garbage in some epoch, then we can be sure that after two advancements no
//! participant will hold a reference to it. That is the crux of safe memory reclamation.
```

第二处是这条判据的**实际代码**——`SealedBag::is_expired`（`internal.rs`，细节留待 u5-l5，这里只看判据本身）：

[crossbeam-epoch/src/internal.rs:155-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L155-L162)

```rust
impl SealedBag {
    /// Checks if it is safe to drop the bag w.r.t. the given global epoch.
    fn is_expired(&self, global_epoch: Epoch) -> bool {
        // A pinned participant can witness at most one epoch advancement. Therefore, any bag that
        // is within one epoch of the current one cannot be destroyed yet.
        global_epoch.wrapping_sub(self.epoch) >= 2
    }
}
```

`>= 2` 就是数学判据的落地。注释里那句「A pinned participant can witness at most one epoch advancement」正是 4.2.1 那条「最多见证一次推进」直觉的官方表述，也是「为什么是 2」的证明依据。

> 说明：`Bag`/`SealedBag`、`Global`/`Local`、`try_advance`/`collect` 的完整实现是 u5-l5 的主题，本讲只借用这条判据来佐证原理，不展开。

#### 4.2.4 代码实践

**实践目标**：把 epoch 回收的三阶段与真实代码一一对应起来。

**操作步骤**：

1. 阅读上面的 `SealedBag::is_expired` 与 `epoch.rs` 文档。
2. 做一道**纸笔推演**（不需要运行）：假设全局 epoch 从 0 开始，某对象在第 2 代被标记为垃圾（`bag_epoch = 2`）。请填写下表，判断每一代它「能否被销毁」：

   | 当前 global_epoch | `wrapping_sub` 结果 | `is_expired`（能销毁？） |
   |---|---|---|
   | 2 | 0 | ? |
   | 3 | 1 | ? |
   | 4 | 2 | ? |

3. 对照 `epoch.rs` 里 `successor` 的定义，确认「推进一代」在数值上是什么操作：

   [crossbeam-epoch/src/epoch.rs:78-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L78-L86)

   ```rust
   pub(crate) fn successor(self) -> Self {
       Self {
           data: self.data.wrapping_add(2),
       }
   }
   ```

**需要观察的现象**：注意 `successor` 是 `wrapping_add(2)` 而不是 `+1`——因为 `Epoch` 的最低位被复用为「是否 pin」的标记（见 4.3 节会提到），真正的「代」步长是 2。

**预期结果**：表格答案依次为「不能 / 不能 / 能」。你应该能解释：在第 2、3 代时这个对象可能还被某个见证过推进的读者摸着；到第 4 代（差 2），所有曾在它被标记时在场的读者都已离场，可安全销毁。**这道推演无需运行，可自行核对。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `is_expired` 用 `>= 2` 而不是 `>= 1`？

> **参考答案**：因为一个 pin 中的参与者最多只能见证一次 epoch 推进。差距仅为 1 时，仍可能有「在第 K 代 pin、还没 unpin」的读者持有该对象；只有差距达到 2，才能保证打标签时在场的所有读者都已离场。差 1 不够，差 2 是最紧的安全界。

**练习 2**：`successor` 用 `wrapping_add(2)`。如果把最低位「是否 pin」的标记拿掉、改用 `+1`，会带来什么后果？

> **参考答案**：当前设计把「代编号」和「是否 pin」压进同一个整数（低位 1 位表 pin，高位表代），故每推进一代数值增加 2，`is_expired` 的 `>= 2` 与之配套。若强行改成 `+1`，要么得另开一个字段存 pin 标志（增加状态、增加同步开销），要么判据含义会错乱。这是「一个原子字同时编码两类信息」的紧凑设计。

---

### 4.3 模块三：公共 API——pin / default_collector / is_pinned 与重导出

#### 4.3.1 概念说明

理解了原理，接下来看「使用者实际要碰哪些 API」。crossbeam-epoch 的设计哲学是：**对绝大多数使用者，回收是全自动的，你只需要在读写数据结构前调用 `pin()`。**

它的公共 API 可以分成三层，由「最常用」到「最底层」：

| 层 | 入口 | 你什么时候用 |
|----|------|--------------|
| 便捷层（最常用） | `pin()` / `is_pinned()` / `default_collector()` | 99% 的场景：直接 `epoch::pin()` 拿一个 `Guard` |
| 句柄层 | `Collector` / `LocalHandle` | 想自建一个独立收集器（例如每个数据结构实例一个）时 |
| 指针层 | `Atomic<T>` / `Owned<T>` / `Shared<'g,T>` | 实际编写无锁数据结构时，用它们存放/读取堆节点 |

几种核心类型的关系如下（理解这张图就抓住了 API 主干）：

```
                       pin()  ───────►  Guard  ('g 生命周期凭证)
                        ▲                  │
                        │                  │  load/swap/compare_exchange 都要 &'g Guard
   Atomic<T>  ◄────────┼──────────────────┘
   (堆对象的原子指针)    │
        │              │ store/swap 可接受 Owned 或 Shared
        ▼              │
   Owned<T> ──into_shared(&Guard)──►  Shared<'g, T>  (epoch 保护下的借用指针)
   (独占, 像 Box)                       │
                                        └─ defer_destroy(shared) 延迟释放
```

几个要点：

- **`Guard` 是 pin 的 RAII 凭证**：拿到 `Guard` 表示「当前线程已 pin」；`Guard` 被 drop 时自动 unpin。`Shared<'g, T>` 的生命周期 `'g` 绑定在 `Guard` 上——这从类型层面保证了「你读出来的指针，不会活过这次 pin」。
- **`Atomic<T>` 是数据结构里实际存放的字段**：它内部就是一个 `AtomicPtr<()>`，所有读写都要附带一个 `&Guard`（见 [crossbeam-epoch/src/atomic.rs:340-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L340-L358) 的 `load` 签名）。
- **`Owned` 像 `Box`，`Shared` 像借用**：`Owned::new(x)` 独占一个堆对象；`Owned::into_shared(&guard)` 把它变成可被结构共享的 `Shared`（见 [crossbeam-epoch/src/atomic.rs:1063-1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1063-L1065)）。
- **`defer`/`defer_destroy` 把释放推迟到安全之后**：这是「标记 → 延迟」阶段使用者直接接触的 API（详见 u5-l3）。

#### 4.3.2 核心流程

一次典型的「读写无锁结构」会话：

```
1. let guard = &epoch::pin();        // 进入临界「代」，拿到 Guard
2. let p: Shared = a.load(SeqCst, guard);  // 读出受保护的指针
3. unsafe { p.as_ref() } 读取数据    // 安全：只要 guard 还在，p 指向的对象不会被回收
4. （若要删除）guard.defer_destroy(old);   // 把旧对象交给 epoch 延迟回收
5. guard 离开作用域 → Drop → unpin   // 退出「代」
```

而 `pin()` 本身做了三件事（细节在 u5-l4/u5-l5）：把当前线程登记的局部 epoch 更新为全局 epoch 并打上 pin 标记、插一道内存栅栏保证之后的读不会重排到 pin 之前、偶尔触发一次 `collect` 推进 epoch 并回收过期垃圾。

#### 4.3.3 源码精读

**对外类型清单——`lib.rs` 的 `pub use` 重导出。**

整个 crate 真正「公开」的类型，全部集中在 `lib.rs` 末尾的两处 `pub use`：

[crossbeam-epoch/src/lib.rs:170-177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L170-L177)

```rust
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
pub use crate::{
    atomic::{
        Atomic, CompareExchangeError, CompareExchangeValue, Owned, Pointable, Pointer, Shared,
    },
    collector::{Collector, LocalHandle},
    guard::{Guard, unprotected},
};
```

注意那个门控 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]`——epoch 的核心 API 既需要堆分配（`alloc`），也需要原子指针指令（`target_has_atomic = "ptr"`）。这呼应了 README 里 epoch 标注的 `<sup>(alloc)</sup>`，也呼应了 u1-l2 讲过的「特性层层传递」。

而 `pin` / `is_pinned` / `default_collector` 这三个便捷入口需要 `std`（因为它们依赖 thread-local 与全局 `OnceLock`），所以单独在另一处门控里导出：

[crossbeam-epoch/src/lib.rs:184-187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187)

```rust
#[cfg(feature = "std")]
mod default;
#[cfg(feature = "std")]
pub use crate::default::{default_collector, is_pinned, pin};
```

也就是说：**关掉 `std`、只开 `alloc` 时，`pin()` 不可用**（没有全局收集器与 thread-local），但你仍可以用 `Collector::new()` 自建收集器、用 `LocalHandle::pin()` 手动 pin。这是「同一份源码、分级能力」在 epoch 上的体现。

> 补充：`#![no_std]` 写在 [crossbeam-epoch/src/lib.rs:51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L51)，与 u1-l2/u1-l3 讲过的门面纪律一致。

**三个便捷入口——`default.rs`。**

全局收集器是惰性初始化的 `OnceLock`（loom 下用 `lazy_static!`）：

[crossbeam-epoch/src/default.rs:16-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L16-L38)

```rust
fn collector() -> &'static Collector {
    #[cfg(not(crossbeam_loom))]
    {
        static COLLECTOR: OnceLock<Collector> = OnceLock::new();
        COLLECTOR.get_or_init(Collector::new)
    }
    // ... loom 分支略
}

thread_local! {
    static HANDLE: LocalHandle = collector().register();
}
```

这里有两个关键设计：

- **全局唯一的默认收集器**：`collector()` 返回一个 `&'static Collector`，所有线程共享。`default_collector()` 就是它的公开 getter（见 [crossbeam-epoch/src/default.rs:52-55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L52-L55)）。
- **每线程一个参与者**：`HANDLE` 是 thread-local，在线程首次访问时调用 `collector().register()` 注册一个 `LocalHandle`，线程退出时随 thread-local 析构而注销（见文件顶部注释 [crossbeam-epoch/src/default.rs:1-5](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L1-L5)）。

于是 `pin()` 的实现极其简短——委托给本线程的 `HANDLE`：

[crossbeam-epoch/src/default.rs:40-55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L40-L55)

```rust
#[inline]
pub fn pin() -> Guard {
    with_handle(|handle| handle.pin())
}

#[inline]
pub fn is_pinned() -> bool {
    with_handle(|handle| handle.is_pinned())
}

pub fn default_collector() -> &'static Collector {
    collector()
}
```

`with_handle` 是个耐人寻味的小函数，它妥善处理了「线程正在退出、thread-local 已析构」的边界情况：

[crossbeam-epoch/src/default.rs:57-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L57-L65)

```rust
#[inline]
fn with_handle<F, R>(mut f: F) -> R
where
    F: FnMut(&LocalHandle) -> R,
{
    HANDLE
        .try_with(|h| f(h))
        .unwrap_or_else(|_| f(&collector().register()))
}
```

`HANDLE.try_with(...)` 在 thread-local 访问出错（比如线程退出过程中 `HANDLE` 已被销毁）时返回 `Err`，此时 `.unwrap_or_else` 会**临时再注册一个新参与者**来完成这次 `pin`，保证 `pin()` 永不 panic。这个边界情形在文件末尾的测试 `pin_while_exiting`（[crossbeam-epoch/src/default.rs:77-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L77-L100)）里有覆盖——它在另一个 thread-local 的 `Drop` 里调用 `pin()`，验证不会 panic。

**`pin()` 最终落到哪里？** `LocalHandle::pin` 调用 `Local::pin`（在 `internal.rs`，u5-l4 详讲）。本讲只看一眼它的轮廓，确认「pin 会记录局部 epoch 并插栅栏」：

[crossbeam-epoch/src/internal.rs:401-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L401-L462) （节选关键行）

```rust
pub(crate) fn pin(&self) -> Guard {
    let guard = Guard { local: self };
    let guard_count = self.guard_count.get();
    self.guard_count.set(guard_count.checked_add(1).unwrap());

    if guard_count == 0 {
        let global_epoch = self.global().epoch.load(Ordering::Relaxed);
        let new_epoch = global_epoch.pinned();
        // ... store new_epoch + SeqCst fence ...
        // ... 每 PINNINGS_BETWEEN_COLLECT(=128) 次触发一次 collect ...
    }
    guard
}
```

注意 `guard_count`：**pin 是可重入的**，嵌套 `pin()` 只会增加计数，真正「进入代」只在 `guard_count == 0` 时发生一次；对应地 `Guard::drop` 减计数，归零才 unpin（见 [crossbeam-epoch/src/guard.rs:416-423](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L416-L423)）。这与模块文档里「Pinning is reentrant」的承诺一致。

#### 4.3.4 代码实践

**实践目标**：亲手跑通「`pin` → `Atomic::load` → `defer_destroy`」的最小闭环，并体会 `Guard` 的 RAII 语义。

**操作步骤**：

1. 在仓库外新建一个临时小 crate（或用 `examples` 目录），加入依赖：

   ```toml
   [dependencies]
   crossbeam-epoch = { path = "crossbeam-epoch" }   # 若在 workspace 内
   ```

2. 写下面这段**示例代码**（基于 `guard.rs` 文档示例改写，标注为示例）：

   ```rust
   // 示例代码：演示 pin / Atomic::load / defer_destroy 的最小闭环
   use crossbeam_epoch::{self as epoch, Atomic, Owned};
   use std::sync::atomic::Ordering::SeqCst;

   fn main() {
       // 一个堆上的字符串，放在 Atomic 里（数据结构里常见的字段类型）
       let a = Atomic::new("hello");

       {
           // 1. pin 当前线程，拿到 Guard
           let guard = &epoch::pin();
           println!("pinned? {}", epoch::is_pinned()); // 期望 true

           // 2. 原子地把 "hello" 换成 "world"，换出的旧值 p 指向 "hello"
           let p = a.swap(Owned::new("world").into_shared(guard), SeqCst, guard);

           // 3. "hello" 此刻已从结构中消失，但可能还有别的线程在读 → 不能立即释放
           //    用 defer_destroy 把它的释放交给 epoch 延迟回收
           if !p.is_null() {
               unsafe { guard.defer_destroy(p); }
           }
           // guard 在块末 drop → unpin
       }
       println!("pinned? {}", epoch::is_pinned()); // 期望 false

       // 4. 收尾：取回 a 里最后的对象，避免泄漏（单线程演示用）
       unsafe { drop(a.into_owned()); }
   }
   ```

3. 编译运行：

   ```bash
   cargo run
   ```

**需要观察的现象**：

- 第一次 `is_pinned()` 应为 `true`（`guard` 仍存活），第二次应为 `false`（`guard` 已 drop）。这验证了 `Guard` 的 RAII / 可重入计数语义。
- 程序正常退出、无 use-after-free 报错。

**预期结果**：输出大致为

```
pinned? true
pinned? false
```

「`"hello"` 何时被真正释放」在这段单线程代码里并不确定（epoch 只保证「之后某时刻」），因此**不要**在 `defer_destroy` 后立刻断言它已被释放——延迟回收的时机正是 u5-l3/u5-l5 的主题。**完整运行结果待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Atomic::load` 的签名里必须带一个 `&Guard` 参数，而 `Atomic::new` 不需要？

> **参考答案**：`Atomic::new` 只是分配并构造，不涉及「并发读取已存在节点」，不需要 pin 保护。`Atomic::load` 要把指针交给调用者去解引用，必须保证「读出来的对象在解引用期间不会被回收」，这正是 pin 的职责——`&'g Guard` 同时把生命周期 `'g` 绑定到返回的 `Shared<'g, T>` 上，从类型上保证读出的指针不会逃出这次 pin。注意源码里 `load` 其实用 `_` 忽略了该参数的运行期值（见 [atomic.rs:356-358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L356-L358)），它的真正作用是**借用检查与生命周期约束**。

**练习 2**：关闭 `std` 特性、只开 `alloc` 时，`epoch::pin()` 还能用吗？为什么？那想 pin 该怎么办？

> **参考答案**：不能。`pin`/`is_pinned`/`default_collector` 在 `#[cfg(feature = "std")]` 下才导出（见 [lib.rs:184-187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187)），因为它们依赖 thread-local `HANDLE` 与全局 `OnceLock`，这些是 `std` 设施。在 `alloc`-only 环境下，应自建收集器：`let c = Collector::new(); let h = c.register(); let guard = h.pin();`——即走 `Collector`/`LocalHandle` 句柄层。

**练习 3**：`with_handle` 里 `HANDLE.try_with(...)` 失败时为什么用 `.unwrap_or_else(|_| f(&collector().register()))` 而不是直接 panic？

> **参考答案**：thread-local 访问可能在线程退出阶段失败（`HANDLE` 已被析构，比如别的 thread-local 的 `Drop` 里又调了 `pin()`）。直接 panic 会在析构期间引发 abort 或混乱。临时重新注册一个参与者来服务这次 `pin`，既保证 `pin()` 永不 panic，又保证析构期的 pin 仍然安全。`default.rs` 的 `pin_while_exiting` 测试正是验证这一边界。

---

## 5. 综合实践

把三个模块串起来，完成一个「**读源码 + 画关系 + 跑示例**」的小任务。

**任务**：为 crossbeam-epoch 的公共 API 制作一张「**入口速查表**」，并验证你对它的理解。

**步骤**：

1. **读门面**：打开 [crossbeam-epoch/src/lib.rs:170-187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L170-L187)，把所有 `pub use` 出来的条目分到下表四列里：

   | 分类 | 条目 | 来源文件 | 需要的特性 |
   |------|------|----------|------------|
   | 便捷入口（全局函数） | ? | default.rs | std |
   | 收集器句柄 | ? | collector.rs | alloc + 原子 |
   | pin 凭证 | ? | guard.rs | alloc + 原子 |
   | 原子指针族 | ? | atomic.rs | alloc + 原子 |

2. **追一次调用链**：从 `epoch::pin()`（[default.rs:42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/default.rs#L42)）出发，顺着 `with_handle` → `LocalHandle::pin`（[collector.rs:83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L83)）→ `Local::pin`（[internal.rs:403](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L403)）追到「真正记录 epoch + 插栅栏」的地方。把这条链画成一张图。

3. **跑通示例**：完成 4.3.4 的示例代码，确认 `is_pinned()` 的两次输出符合预期，并对照 4.2 的三阶段，口头说明代码里 `defer_destroy` 对应「标记 → 延迟」中的哪一步、「销毁」由谁、在何时触发（提示：由后续某次 `pin` 周期触发的 `collect`，见 [internal.rs:454-458](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L454-L458)）。

**预期结果**：你能不看源码，向同事讲清楚「调用 `epoch::pin()` 之后，系统为我做了哪几件事，我拿到的 `Guard` 又能在哪些 API 里用」，并理解「关掉 `std` 时改用 `Collector` 句柄层」这一设计取舍。

## 6. 本讲小结

- crossbeam-epoch 解决的是无锁数据结构里「**删除方想 free、读取方还在读**」的内存回收难题——这是 Rust（无 GC）写无锁结构必须自己迈过的坎。
- 它采用 **epoch（代）延迟回收**：对象被摘除后贴上「当前代」标签进垃圾袋；当全局 epoch 与之相差 **≥ 2** 时才销毁。这条 `>= 2` 判据的依据是「一个 pin 中的参与者最多见证一次 epoch 推进」。
- 全局 epoch 只能在「所有当前 pin 的参与者都登记在当前代」时才推进，从而保证回收安全。
- 公共 API 分三层：便捷层 `pin()`/`is_pinned()`/`default_collector()`（需 `std`）、句柄层 `Collector`/`LocalHandle`、指针层 `Atomic`/`Owned`/`Shared`（需 `alloc` + 原子）。
- `pin()` 返回的 `Guard` 是 pin 的 RAII 凭证，**可重入**（计数），`drop` 时 unpin；`Shared<'g, T>` 的生命周期 `'g` 绑定在 `Guard` 上，从类型上约束「读出的指针不逃出本次 pin」。
- `pin()` 永不 panic：`with_handle` 在 thread-local 不可用时临时重新注册参与者兜底。

## 7. 下一步学习建议

本讲只建立了 epoch 的「全景地图」。后续讲义会逐层下钻：

- **u5-l2 Atomic/Shared/Owned 原子指针与标签**：深入 `atomic.rs`，搞清楚 `AtomicPtr<()>` 如何用低地址位打包 tag、`Owned` 与 `Shared` 的所有权语义、`compare_exchange` 如何同时比较指针与标签。
- **u5-l3 Guard 与 pin 机制**：深入 `guard.rs` + `deferred.rs`，看清 `defer`/`defer_destroy`/`defer_unchecked` 的延迟执行语义，以及 `Deferred` 如何做类型擦除的闭包内联/装箱。
- **u5-l4 Collector 与参与者注册**：深入 `collector.rs` + `internal.rs` 上半部分，看 `Collector`/`LocalHandle`、参与者如何注册进全局链表。
- **u5-l5 internal：全局状态、epoch 推进与垃圾回收**：深入 `internal.rs` 与 `epoch.rs`，彻底搞懂 `Global`/`Local`、`try_advance`、`collect`、`Bag`/`SealedBag` 的完整协议——本讲埋下的「2 epoch」「successor 步长 2」「PINNINGS_BETWEEN_COLLECT=128」等伏笔都在那里回收。

建议在进入 u5-l2 前，先确保你能背出本讲的「入口速查表」与三阶段（标记 → 延迟 → 销毁）模型。
