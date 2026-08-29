# u4-l4 collect 的内部实现：把并行产出的各段安全写进同一个 Vec

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `collect` 快速路径的完整调用链：从 `collect::<Vec<_>>()` 到 `collect_with_consumer`，再到最终的 `set_len`。
2. 理解 `CollectConsumer` 如何在 `reserve` 出来的未初始化容量上按区间直接写入，而全程不使用锁与原子计数。
3. 说出 `CollectResult` 的三重身份（Folder / Consumer::Result / 代理所有者），以及 `CollectReducer` 的「相邻才合并」协议如何与最终断言一起保证正确性。
4. 画出「长度为 N、切分为 4 段」时各段写入位置的示意图，并用 `with_max_len(8)` 强制切分验证输出顺序仍然正确。

本讲是 u2-l4（collect 的使用层面）与 u4-l3（Consumer 契约与 bridge 引擎）的合流点：前两讲分别回答了「结果怎么回去」和「消费者如何被驱动」，本讲拆开 `src/iter/collect/` 目录，看其中最精巧的一个 Consumer 实现。

## 2. 前置知识

### 2.1 Vec 的内存三件套

`Vec<T>` 内部是 `(指针, capacity, len)` 三元组。`capacity - len` 那一段是**已分配但未初始化**的内存：

- 向这段内存写入元素必须用 `ptr::write`（即 `std::ptr::write`），不能用赋值 `*p = x`——赋值会先对旧值调用 `Drop`，而对未初始化内存这么做是未定义行为。
- `vec.set_len(n)` 是 unsafe 函数，它直接把 `len` 改成 `n`，等于向编译器承诺「前 `n` 个元素都已初始化」。本讲的整个模块就是在安全地凑齐这个承诺。

### 2.2 collect 的调用链回顾（u2-l4）

`pi.collect::<Vec<T>>()` 的实际路径是：

```
collect() → Vec::from_par_iter → collect_extended（default + par_extend）
         → Vec::par_extend → 按 opt_len() 分岔：
              Some(len) → special_extend → collect_with_consumer   ← 本讲主线（快速路径）
              None      → ListVecConsumer 收 LinkedList<Vec<T>>，再逐个 append（慢路径）
```

无长度路径（各任务自建 `Vec`，最后拼接）在 u2-l4 已讲过，本讲只专注于**已知长度**的快速路径。

### 2.3 Consumer 契约与 bridge 引擎（u4-l3）

- `Consumer::split_at(index)` 把消费者切成左右两半并返回 `Reducer`；`into_folder` 把自己变成串行折叠器 `Folder`；`Folder::consume` 吃一个元素、`complete` 收尾。
- `bridge_producer_consumer` 的递归骨架：取 `mid = len / 2`，让**生产者与消费者在同一个 mid 上对齐切分**，两半用 `join_context` 并行（工作窃取在此发生），最后 `Reducer::reduce(left, right)` 合并。

记住「对齐切分」这四个字——它是本讲一切顺序保证的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/collect/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs) | collect 家族入口：`collect_into_vec` / `special_extend` / `unzip_into_vecs`，以及核心骨架 `collect_with_consumer`（预分配、终检、`set_len`） |
| [src/iter/collect/consumer.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs) | 三位主角：`CollectConsumer`（按位置写入的消费者）、`CollectResult`（Folder 兼代理所有者）、`CollectReducer`（相邻段合并器） |
| [src/iter/collect/test.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs) | 针对「滥用生产者」的恐慌测试：多写、少写、忘记 complete、反序 reduce、panic 后的 Drop 计数 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | `collect_into_vec` 公开方法、`drive_unindexed` 与 `opt_len` 的契约文档 |
| [src/iter/from_par_iter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs) 与 [src/iter/extend.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs) | `collect` 如何走到本模块：`from_par_iter → par_extend → opt_len 分岔` |
| [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) | bridge 切分引擎与 `LengthSplitter`（复习，段的划分来自这里） |
| [src/lib.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs) | `SendPtr<T>`：允许裸指针跨线程的显式包装类型 |

> 术语说明：规格中的「start/write/finish」对应源码里的三个真实方法名——`CollectConsumer::appender`（确定写入起点）、`Folder::consume`（逐元素写入）、`Folder::complete`（收尾）。

## 4. 核心概念与源码讲解

### 4.1 空间预分配策略

#### 4.1.1 概念说明

问题：\( P \) 个任务并行产出 \( N \) 个元素，怎么放进**同一个** `Vec`？

朴素方案是每个任务各自 `Vec::new()` 然后 `push`，结束后按顺序 `append`——这正是 rayon 无长度时的慢路径。它的代价是：\( O(\text{任务数}) \) 次堆分配，外加一遍元素搬运。

快速路径的洞察是：如果总长度 \( N \) 已知（`opt_len()` 返回 `Some(len)`），那么**每个元素在目标 Vec 里的最终位置在切分那一刻就已经确定了**。于是可以：

1. 一次性 `vec.reserve(len)`，把整块内存先要下来；
2. 把区间 \([\text{vec.len()},\ \text{vec.len()} + len)\) 按切分树逐段分给各任务；
3. 每个任务用自己的 `ptr::write` 直接写自己的区间；
4. 全部完成后 `set_len` 一次性「点亮」这些元素。

因为任意两个任务的写入区间**互不重叠**（由切分的区间算术保证），所以不需要 `Mutex`，不需要 `AtomicUsize` 计数，甚至不需要任何跨任务通信——这就是 collect 能做到无锁的原因。

#### 4.1.2 核心流程

```
collect::<Vec<_>>()
  → Vec::from_par_iter                     (from_par_iter.rs)
  → collect_extended: C::default() + par_extend
  → Vec::par_extend
      match par_iter.opt_len()
        Some(len) → special_extend(par_iter, len, vec)      ← 伪特化入口
        None      → ListVecConsumer + append                （慢路径，本讲不讲）

collect_with_consumer(vec, len, scope_fn):
  1. vec.reserve(len)                        # 只涨 capacity，不涨 len
  2. consumer = CollectConsumer::appender(vec, len)
                                            # start = vec.len()，指向未初始化容量起点
  3. result = scope_fn(consumer)             # 内部走 drive → bridge → 切分递归
  4. assert!(result.len() == len)            # 终检：写入总数必须分毫不差
  5. result.release_ownership()              # 元素所有权移交给 Vec
  6. vec.set_len(old_len + len)              # 点亮元素，collect 完成
```

注意 `special_extend` 里调用的是 `pi.drive_unindexed(consumer)`，而 `CollectConsumer` 的 `UnindexedConsumer` 实现是 `unreachable!`——这看起来矛盾，其实这正是本模块最有趣的设计，见 4.1.3 的第 3 点。

#### 4.1.3 源码精读

**入口链路。** `Vec` 的 `from_par_iter` 只是转发给 `collect_extended`，后者「建空集合再 `par_extend`」：

- [src/iter/from_par_iter.rs:L13-L22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L13-L22)——`collect_extended`：`C::default()` 后调用 `par_extend`，这是所有「默认建空再扩展」型集合的统一实现。
- [src/iter/from_par_iter.rs:L24-L35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/from_par_iter.rs#L24-L35)——`impl FromParallelIterator<T> for Vec<T>`，一行转调 `collect_extended`。

**opt_len 分岔。** `Vec` 的 `par_extend` 是快速/慢速两条路径的分岔口：

- [src/iter/extend.rs:L567-L596](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/extend.rs#L567-L596)——注释里直说「等 Rust 有了特化（specialization），这里就能不靠 `opt_len` 直达索引路径；在那之前，`special_extend()` 靠 `opt_len()` 的承诺伪造一个无索引模式」。`None` 分支则用 `ListVecConsumer` 收集 `LinkedList<Vec<T>>` 再 `append`。

**核心骨架 collect_with_consumer。** 整个模块的中枢只有 40 行：

- [src/iter/collect/mod.rs:75-L114](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L75-L114)——逐行看：
  - L81 `vec.reserve(len)`：预分配，只增加 capacity；
  - L84 `scope_fn(CollectConsumer::appender(vec, len))`：造出消费者交给回调（回调里就是 `pi.drive(consumer)`）；
  - L99-L103 `assert!(actual_writes == len)`：终检，多写少写都会在这里恐慌；
  - L107 `result.release_ownership()`：让 `CollectResult` 放弃元素所有权（否则会被 Drop 双重释放）；
  - L109-L113 `vec.set_len(new_len)`：unsafe 点亮整段元素。

**appender：起点是 vec.len() 而不是 0。**

- [src/iter/collect/consumer.rs:13-L24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L13-L24)——`let start = vec.len();` 断言剩余容量足够后，用 `vec.as_mut_ptr().add(start)` 构造消费者。collect 是**追加**语义：Vec 里已有的元素绝不会被碰，这就是 `collect_into_vec` 能复用调用方缓冲区的原因。注释还特别说明指针必须直接从 `Vec` 取（而不是经 `Deref` 当切片用），以保留整块分配的 provenance。

**三个值得停下想一想的点：**

1. **为什么 reserve 了还要 assert 容量？** L17 的 `assert!(vec.capacity() - start >= len)` 是双重保险：`reserve` 在前，契约检查在后，把 unsafe 代码的前提条件钉死在眼前。
2. **`collect_into_vec` 公开方法**只是清空 + 转发：[src/iter/mod.rs:2514-L2534](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2514-L2534)，文档明说「执行前总是先 clear，跨调用复用缓冲区可能更快」。
3. **`special_extend` 为什么「骗」类型系统？** [src/iter/collect/mod.rs:23-L40](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L23-L40) 的注释承认这是「faking a bit of specialization」：类型上它接受任何 `ParallelIterator`，但调用方已用 `opt_len()` 确认过长度，实际只会传索引迭代器进来。契约写在 [src/iter/mod.rs:2414-L2430](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2414-L2430)：**`opt_len()` 返回 `Some` 的迭代器承诺只用索引方法（`split_at` 等）驱动消费者**。为什么运行期真的不会踩到 `unreachable!`？因为索引数据源的 `drive_unindexed` 底层就是 `bridge`（例如 [src/range.rs:145-L150](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L145-L150)，`Range` 的 `drive_unindexed` 直接调 `bridge(iter, consumer)`），走的是只用 `Consumer::split_at` 的索引路径。

#### 4.1.4 代码实践

**实践：观察预分配与缓冲区复用。**

1. 实践目标：亲眼确认 `reserve` 只涨 capacity 不涨 len，且 `collect_into_vec` 跨调用复用同一块缓冲区。
2. 操作步骤：在独立 Cargo 项目（依赖 `rayon = "1"`）中运行以下程序（示例代码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let mut buf: Vec<u64> = Vec::new();
       for round in 1..=3u64 {
           (0..10u64)
               .into_par_iter()
               .map(|i| i * round)
               .collect_into_vec(&mut buf);
           println!(
               "round {}: len = {}, capacity = {}",
               round,
               buf.len(),
               buf.capacity()
           );
       }
   }
   ```

3. 需要观察的现象：第一轮之后 `capacity >= 10`；第二、三轮 `capacity` 不再增长（缓冲区被复用，没有新的堆分配）；`len` 每轮都是 10（每次 collect 前先 clear）。
4. 预期结果：三轮输出中 capacity 至多在第一轮跳变一次。若在两轮之间打印 `buf.as_ptr()`，还能看到指针不变（同一块内存）。分配行为细节「待本地验证」（Debug/Release 与分配器策略可能影响首次扩容幅度）。

#### 4.1.5 小练习与答案

**练习 1**：`appender` 里为什么用 `vec.as_mut_ptr().add(start)`，而不是 `vec.as_mut_ptr()`？

答案：collect 是追加语义，`start = vec.len()`。从 `start` 开始写入才能落在未初始化容量区，从 0 开始会覆盖已有元素。

**练习 2**：`special_extend` 的参数类型为什么不直接写 `I: IndexedParallelIterator`？

答案：因为调用方 `par_extend` 面向所有 `IntoParallelIterator`，而 Rust 没有特化机制，无法「当它是索引迭代器时选用更快的实现」。只能在运行期用 `opt_len()` 判断，类型上就得放宽到 `ParallelIterator`，代价是 `CollectConsumer` 必须顺手实现一个永远不会被真正使用的 `UnindexedConsumer`。

**练习 3**：如果某个生产者谎报长度（`opt_len` 返回 `Some(5)` 却产出 7 个元素），会在哪一步、以什么方式失败？

答案：在第 7 个元素进入 `Folder::consume` 时触发 `too many values pushed to consumer` 断言（见 4.2.3 的 consume 源码 L125-L128）；若产出不足 5 个，则躲过 consume 断言，最终被 `collect_with_consumer` 的 `expected 5 total writes` 终检抓住。两种失败都有对应测试（见 4.3.3）。

### 4.2 CollectConsumer

#### 4.2.1 概念说明

`CollectConsumer` 不拥有数据，它只拥有一样东西：**一段写入权** \([start,\ start+len)\)。它是对「按位置写入」这一需求的 plumbing 化表达：

- 切分（`split_at`）= 把写入权对半分；
- 折叠（`into_folder` + `consume`）= 拿着写入权逐个 `ptr::write`；
- 结果（`Consumer::Result`）= 一个记录「我写到了哪里」的 `CollectResult`。

它的 `Folder` 不是别的类型，正是 `CollectResult` 本身——写入过程和结果状态是同一个结构体，这是本实现格外紧凑的原因。

#### 4.2.2 核心流程

```
Consumer::split_at(index):
    left  = CollectConsumer{start: start,          len: index}
    right = CollectConsumer{start: start + index,  len: len - index}
    返回 (left, right, CollectReducer)

Folder::consume(item):                 # item 写入自己区间的下一个空位
    assert!(initialized_len < total_len)
    ptr::write(start + initialized_len, item)
    initialized_len += 1

Folder::complete():  原样返回自己（校验推迟到终检）
```

段的边界完全由 bridge 决定：[src/iter/plumbing/mod.rs:L404-L430](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L404-L430) 中 `mid = len / 2` 处生产者与消费者**同 mid 对齐切分**，随后 `join_context` 并行、`reducer.reduce(left, right)` 合并。所以第 \( i \) 段消费者拿到的区间，恰好是第 \( i \) 段生产者产出的元素应当去的位置——元素不需要「找座位」，出生时座位就定了。

段数与段长：bridge 的切分树把长度逐层对半，段长约为 \( N / 2^d \)（\( d \) 为深度）。`LengthSplitter` 决定何时停刀：[src/iter/plumbing/mod.rs:297-L332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L297-L332) 里 `min_splits = len / max(max_len, 1)`（L318），即 `with_max_len(m)` 保证至少切到 \( \lceil N/m \rceil \) 段；注释（L314-L317）还指出实际段数会取整到 2 的幂，且自适应算法（线程数、窃取）可能切得更细，但不会小于 `with_min_len`。

**无锁论证**：任意两个不同任务的写入区间，要么不相交，要么根本是同一段（`split_at` 按值消费 self、区间严格二分，不允许重叠）。写同一地址的竞争在结构上不存在，于是不需要 `Mutex`；连「总共写了多少」都不用原子计数——各段的 `initialized_len` 是纯线程本地的，最后由 `Reducer` 归并。

#### 4.2.3 源码精读

**结构体：一个指针加一个长度。**

- [src/iter/collect/consumer.rs:6-L11](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L6-L11)——`start: SendPtr<T>`、`len: usize`、`marker: PhantomData<&'c mut T>`。注释直接说「为什么不是切片」要去看 `CollectResult` 的解释（见 4.3.3）。`PhantomData<&'c mut T>` 让借用检查器理解：从创建消费者到结果归还，`vec` 一直被可变借用着。
- [src/lib.rs:120-L134](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L120-L134)——`SendPtr<T>`：裸指针的 `!Send`/`!Sync` 只是 lint 不是安全问题，rayon 用这个显式类型把指针名正言顺地送过线程边界，解引用仍需 unsafe。

**split_at：写入权二分。**

- [src/iter/collect/consumer.rs:90-L103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L90-L103)——先 `assert!(index <= len)` 钉死 unsafe 前提，再返回 `new(start, index)` 与 `new(start + index, len - index)`。纯指针算术，无任何运行期数据移动。

**into_folder 与 consume：写入主循环。**

- [src/iter/collect/consumer.rs:105-L114](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L105-L114)——`into_folder` 造出 `initialized_len: 0` 的 `CollectResult`：写入进度从零开始记账。
- [src/iter/collect/consumer.rs:121-L139](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L121-L139)——`consume` 的两行核心：断言 `initialized_len < total_len`（多写即恐慌），然后 `self.start.0.add(self.initialized_len).write(item)`。注释点明为什么用 `write` 而不用赋值：**避免对未初始化的 T 做 drop**。
- [src/iter/collect/consumer.rs:141-L149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L141-L149)——`complete` 原样返回自己，注释说明本地不校验写满，把校验统一推迟到 `Collect` 的终检（见 4.3）。`full()` 恒为 `false`：collect 永不短路。

**「假装无索引」的防御性实现。**

- [src/iter/collect/consumer.rs:152-L161](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L152-L161)——为满足 `special_extend` 的类型要求而实现 `UnindexedConsumer`，`split_off_left` 直接 `unreachable!("CollectConsumer must be indexed!")`。这是一道运行期防线：一旦有代码违反 `opt_len` 的契约，立刻暴露而不是静默写坏内存。官方 plumbing 文档也解释了根本原因：[src/iter/plumbing/README.md:44-L51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L44-L51)——无索引消费者不知道元素落在流的哪个位置，而 `collect_into_vec` 恰恰**位置即一切**。

#### 4.2.4 代码实践

**实践：画出 N 切分为 4 段时的写入位置示意图（本讲核心实践之一）。**

1. 实践目标：把「区间二分 = 写入位置分配」变成肌肉记忆。
2. 操作步骤：以 \( N = 32 \)、`with_max_len(8)` 为例（此时最少需要 \( 32/8 = 4 \) 段），手绘下面两张图（切分树 + 内存条）。注意实际段数可能因线程数/窃取而更多，但每段不超过 max_len 窗口的语义不变。

   ```
   切分树（bridge 递归，每层 mid = len/2）：

   [0, 32)                     ← 第一次切分：mid = 16
   ├── [0, 16)                 ← 第二次切分：mid = 8
   │   ├── [0, 8)    → CollectConsumer{ start:  0, len: 8 }  段 0
   │   └── [8, 16)   → CollectConsumer{ start:  8, len: 8 }  段 1
   └── [16, 32)                ← 第二次切分：mid = 24
       ├── [16, 24)  → CollectConsumer{ start: 16, len: 8 }  段 2
       └── [24, 32)  → CollectConsumer{ start: 24, len: 8 }  段 3

   Vec 缓冲区（reserve(32) 之后的未初始化容量）：

   下标   0            8            16           24           32
         ├─────────────┼─────────────┼─────────────┼─────────────┤
         │   段 0 写入  │   段 1 写入  │   段 2 写入  │   段 3 写入  │
         └─────────────┴─────────────┴─────────────┴─────────────┘
                  ▲ 每段各自 ptr::write，区间互不重叠 → 无锁
   ```

3. 需要观察的现象：四个区间的起点 `0, 8, 16, 24` 全部来自 `split_at` 的指针加法（`start` 与 `start + index`），与任务实际执行顺序无关。
4. 预期结果：合并阶段沿树自底向上：先 `reduce(段0, 段1)` 得 \([0,16)\)，再 `reduce(段2, 段3)` 得 \([16,32)\)，最后合并为覆盖 \([0,32)\)、`initialized_len == 32` 的单个 `CollectResult`。

#### 4.2.5 小练习与答案

**练习 1**：`Folder::consume` 里为什么不能写 `*self.start.0.add(self.initialized_len) = item`？

答案：赋值运算符会先 drop 目标上的旧值，而目标是未初始化内存，这是未定义行为。必须用 `ptr::write`（源码注释 L130-L131 原话是「we avoid assignment here so we do not drop an uninitialized T」）。

**练习 2**：`CollectConsumer::full()` 永远返回 `false`，这意味着什么？

答案：collect 没有「够了就停」的语义，元素来多少收多少。对照 u2-l5/u3-l4 讲过的 `try_reduce`：那里用共享 `AtomicBool` 加 `full()` 钩子实现全局短路，而 collect 的目标就是把所有元素按位置收齐，短路反而破坏正确性。

**练习 3**：bridge 中生产者和消费者如果不在同一个 `mid` 上切分会发生什么？

答案：段消费者分到的区间与段生产者产出的元素个数不匹配——要么 consume 断言「too many values」恐慌，要么写不满导致终检 assert 失败。代码中 [src/iter/plumbing/mod.rs:L407-L409](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L407-L409) 连续两行 `split_at(mid)` 就是把这一致性写死在同一处。

### 4.3 结果拼接

#### 4.3.1 概念说明

`CollectResult` 有三重身份：

1. **Folder**：写入过程中，它就是那个逐元素吃进的折叠器（4.2 已讲）；
2. **`Consumer::Result`**：每段任务完成后，它是该段的「成绩单」——「我从 `start` 起写了 `initialized_len` 个元素」；
3. **代理所有者**：它拥有已写入元素的所有权。如果一切顺利，`release_ownership` 把所有权移交给 Vec；如果中途 panic 或丢弃，它的 `Drop` 会只 drop 已初始化的那部分，**保证不泄漏**。

`CollectReducer` 的协议极其克制：**只合并相邻且按左右顺序的两段**。左段的末尾恰好是右段的起点，才把两段拼成一段；否则直接丢弃右段——总长度因此变短，最终被 `collect_with_consumer` 的断言抓住。于是「正确性校验」被压缩成了一个减法：整个系统只需要在最后问一句「总写入数是否等于 len」。

#### 4.3.2 核心流程

```
CollectReducer::reduce(left, right):
    if left.start + left.initialized_len == right.start:      # 相邻且左前右后
        left.total_len       += right.total_len
        left.initialized_len += right.release_ownership()     # 所有权转移，right 清零
        return left
    else:
        丢弃 right（其 Drop 会释放它已写的元素）
        # 总长度变短 → 终检 assert 恐慌

终检与装配（collect_with_consumer）:
    assert!(result.len() == len)       # actual_writes == len
    result.release_ownership()         # CollectResult 不再拥有元素
    vec.set_len(old + len)             # Vec 接管，元素对外界可见
```

为什么「只比总长」就够？因为 `reduce` 只接受相邻合并，最终的那个 `CollectResult` 必然是从 `start` 开始的**连续**区间；连续且长度恰为 \( len \)，就必然恰好覆盖 \([start,\ start+len)\)，一个位置不多、一个位置不少。mod.rs L92-L98 的注释还给出更深的依据（parametricity）：消费者无法从 `drive` 中逃逸，所以断言时刻所有写入都已发生。

panic 安全：任何一段在写入中途 panic，那个 `CollectResult` 被 unwind 时走 `Drop`，只 drop `initialized_len` 前缀；Vec 自己的 `len` 还没被改，不会重复 drop。测试用 `DropCounter` 逐个计数验证了这一点。

顺序保证：reduce 的调用点固定为 `reduce(left_result, right_result)`（bridge L430），配合对齐切分，段与段天然相邻——**无论任务完成的先后顺序如何**（工作窃取会打乱完成顺序），每个元素的位置只取决于它所在段的区间分配。这就是「并行 collect 的结果顺序与串行一致」的全部秘密。

#### 4.3.3 源码精读

**CollectResult：连续区间的记账本。**

- [src/iter/collect/consumer.rs:38-L54](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L38-L54)——字段三件套：`start`（起点指针）、`total_len`（本段总容量）、`initialized_len`（已写长度）。文档注释点明它是「proxy owner」：drop 时会释放元素，除非先 `release_ownership`。L44-L47 的注释解释了为什么用指针+长度而不是切片：**保留整块分配的 provenance，才能在 Reducer 里做相邻判断与合并**。`invariant_lifetime`（L51-L53）是生命周期型变技巧，保证数据只能从 consumer 流向 result。`#[must_use]`（L42）提醒调用者不能随手丢掉它——丢了等于丢掉整段已写元素。
- [src/iter/collect/consumer.rs:56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L56)——`unsafe impl Send`：跨线程归并的前提。

**所有权交接与 Drop 兜底。**

- [src/iter/collect/consumer.rs:58-L70](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L58-L70)——`release_ownership` 把 `initialized_len` 清零并返回原值：清零后 `Drop` 就不会再碰这些元素，所有权安全地移交给 Vec 或左段。
- [src/iter/collect/consumer.rs:72-L83](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L72-L83)——`Drop for CollectResult`：`drop_in_place(slice_from_raw_parts_mut(start, initialized_len))`，只释放已初始化前缀。这是 panic/短缺场景下的防泄漏安全网。

**CollectReducer：相邻才合并。**

- [src/iter/collect/consumer.rs:163-L185](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L163-L185)——`left_end = left.start + left.initialized_len`，与 `right.start` 比较相等才合并；不等则注释明说「现在就 drop 右段，最终总长短缺会被正确性断言发现」。合并动作是 `total_len` 与 `initialized_len` 各加右段的账，右段经 `release_ownership` 清零后随 `Drop` 无痛销毁。

**终检与 set_len。**

- [src/iter/collect/mod.rs:86-L113](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L86-L113)——L86-L98 的注释交代了 unwind 场景：部分 `CollectResult` 若在中途被丢掉而未归并，就会表现为总长短缺。随后 `assert!`、`release_ownership`、`set_len` 三步完成装配。

**「滥用生产者」测试：把每一种错法都写成用例。** [src/iter/collect/test.rs:4-L6](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs#L4-L6) 开头明说这些测试针对的是故意乱驱动 collect consumer 的生产者：

- [src/iter/collect/test.rs:19-L30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs#L19-L30)——承诺 2 个却写 3 个：consume 断言恐慌 `too many values`。
- [src/iter/collect/test.rs:34-L44](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs#L34-L44)——承诺 5 个只写 2 个：终检恐慌 `expected 5 total writes, but got 2`。
- [src/iter/collect/test.rs:186-L201](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs#L186-L201)——故意把 reduce 参数反着调（`reduce(right, left)`）：段不相邻，右段（原左段）被丢弃，总长 2 ≠ 4，终检抓住。这条测试直接证明了「相邻才合并 + 总长终检」这对组合的拦截能力。
- [src/iter/collect/test.rs:276-L299](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs#L276-L299)——少产出且统计 Drop：配合 [src/iter/collect/test.rs:303-L334](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/test.rs#L303-L334) 的 `DropCounter`，断言「created == dropped」且 `v.is_empty()`——panic 后既不泄漏、Vec 也没有暴露任何未初始化元素。

#### 4.3.4 代码实践

**实践：用 `with_max_len(8)` 强制切分，打乱完成顺序，验证输出顺序（本讲核心实践之二）。**

1. 实践目标：证明「写入位置由区间决定、与完成顺序无关」。
2. 操作步骤（示例代码）：

   ```rust
   use rayon::prelude::*;
   use std::thread::sleep;
   use std::time::Duration;

   fn main() {
       let n = 32u64;
       let got: Vec<u64> = (0..n)
           .into_par_iter()
           .with_max_len(8) // 至少切成 32/8 = 4 段，每段不超过 8
           .map(|i| {
               // 让奇数元素睡 5ms：各元素完成顺序与下标顺序脱钩
               if i % 2 == 1 {
                   sleep(Duration::from_millis(5));
               }
               i * i
           })
           .collect();

       let expect: Vec<u64> = (0..n).map(|i| i * i).collect();
       assert_eq!(got, expect);
       println!("ok: 前 8 个元素 = {:?}", &got[..8]);
   }
   ```

3. 需要观察的现象：由于奇数位睡眠，各段内部的元素完成顺序被打乱（可用 `eprintln!` 在 map 里打印 `(i, 线程id)` 观察，注意打印本身也会改变时序）；不同运行的线程分配可能不同。
4. 预期结果：`assert_eq!` 恒成立——即使元素完成顺序乱掉，`got` 仍严格等于 \( 0^2, 1^2, \dots, 31^2 \) 按序排列。若想进一步看到「多段并行」的痕迹，可在 map 闭包里打印 `rayon::current_thread_index()`，观察同一段的元素倾向落在同一线程（待本地验证：具体线程分布取决于机器核数与窃取时机）。

#### 4.3.5 小练习与答案

**练习 1**：把 `CollectReducer::reduce` 的实参顺序反过来调用（先 right 后 left）会发生什么？哪条测试覆盖了这种情况？

答案：右段与左段不再满足「左段末尾 == 右段起点」的相邻条件（除非长度为零），右段被丢弃、总长度短缺，最终在 `collect_with_consumer` 的终检处恐慌。覆盖测试是 `reducer_does_not_preserve_order`（test.rs L186-L201），期望恐慌信息 `expected 4 total writes, but got 2`。

**练习 2**：`release_ownership` 为什么要特意把 `initialized_len` 清成 0，而不是只返回长度？

答案：`CollectResult` 随后会被 `Drop`。清零后 `Drop` 里的 `drop_in_place` 长度为 0，等于什么都不释放；否则元素会被 `CollectResult` 和 `Vec`（或左段）各 drop 一次，造成双重释放。

**练习 3**：为什么最终校验只需要比较一个总长度，而不必逐段核对位置？

答案：`reduce` 只接受「相邻且左前右后」的合并，因此最终 `CollectResult` 必然是从 `start` 起的连续区间；又因为 `consume` 断言禁止多写，连续区间长度恰为 \( len \) 时必然恰好覆盖目标区间。再加上消费者无法从 `drive` 中逃逸（mod.rs L92-L98 注释所述的 parametricity 论证），断言时刻所有写入都已发生，一个减法就足够。

## 5. 综合实践

把本讲三块知识串成一条完整的链路。任务分四步：

1. **调用链跟踪（笔头作业）**：针对 `(0..1000).into_par_iter().map(|i| i + 1).collect::<Vec<_>>()`，从 `collect` 开始写出依次经过的每个函数及其所在文件，直到 `set_len`。参考答案（可先自己写再对照）：
   `Vec::from_par_iter`（from_par_iter.rs:24）→ `collect_extended`（from_par_iter.rs:14）→ `Vec::par_extend`（extend.rs:573）→ `opt_len() == Some(1000)` → `special_extend`（collect/mod.rs:34）→ `collect_with_consumer`（collect/mod.rs:75，`reserve(1000)`）→ `CollectConsumer::appender`（consumer.rs:15）→ `pi.drive_unindexed(consumer)` → `Map` 转发到 `Range` 的 `drive_unindexed` = `bridge`（range.rs:149）→ `bridge_producer_consumer` 递归切分（plumbing/mod.rs:385）→ 各段 `CollectConsumer::split_at` / `into_folder` / `consume` / `complete`（consumer.rs:90/105/124/141）→ `CollectReducer::reduce` 逐层合并（consumer.rs:167）→ 终检 assert + `release_ownership` + `set_len`（collect/mod.rs:99-113）。
2. **画图**：完成 4.2.4 的 4 段示意图，并在图上标出 `release_ownership` 发生的三个合并点。
3. **运行验证**：完成 4.3.4 的 `with_max_len(8)` 程序，确认乱序完成后结果仍有序。
4. **路径对比（思考 + 验证）**：把管道改成 `(0..1000).into_par_iter().filter(|i| i % 2 == 0).collect::<Vec<_>>()`。根据 u2-l1 的结论（`filter` 丢失索引）预判：`opt_len()` 变为 `None`，collect 退回 `LinkedList<Vec<T>>` 慢路径（extend.rs:586-593）。验证方法：阅读该分支源码确认其结构与预判一致；运行结果（得到 500 个偶数）两条路径完全一致——这正说明快速路径是一种纯性能优化，不改变语义。

## 6. 本讲小结

- **预分配**：已知长度时，`collect_with_consumer` 一次性 `reserve(len)`，把追加区间的写入权按切分树分配给各任务，元素落点在切分那一刻即确定，最后 `set_len` 一次点亮。
- **CollectConsumer**：不拥有数据、只拥有区间 `[start, start+len)` 的写入权；`Folder` 就是 `CollectResult`，`consume` 即 `ptr::write` 加一；区间互不重叠是全程无锁、无原子计数的结构化原因。
- **伪特化**：`special_extend` 靠 `opt_len()` 的运行期约定把索引迭代器骗过类型系统，`UnindexedConsumer` 实现中的 `unreachable!` 是违反契约时的运行期防线。
- **CollectReducer**：只合并相邻且左前右后的段，把「位置正确性」压缩成终检里一个总长度断言；`CollectResult` 的 `Drop`/`release_ownership` 构成 panic 安全与所有权交接的闭环。
- **顺序无关性**：reduce 固定左前右后、切分对齐 mid，元素位置只取决于区间分配，与任务完成顺序（工作窃取）无关。

## 7. 下一步学习建议

- 下一讲 **u4-l5（par_bridge）**：看任意 std 迭代器如何被桥接进并行世界，那里会出现本讲的反面案例——顺序迭代器无法提供区间信息，只能用锁串行取元素，正好反衬 collect 快速路径为何依赖「已知长度」。
- 若想巩固 unsafe 与所有权功力，建议重读 [src/iter/collect/consumer.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs) 中每一段 `// SAFETY:` 注释，并对照 test.rs 的滥用测试逐条映射到防线。
- 进入单元五（rayon-core）前，可以回头看 [src/iter/collect/mod.rs:45-L65](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/mod.rs#L45-L65) 的 `unzip_into_vecs`：两个 `collect_with_consumer` 嵌套的写法是理解「消费者分裂出副本」的一次热身。
