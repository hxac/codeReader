# plumbing 总览：Rayon 的发动机

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **Producer（生产者）**、**Consumer（消费者）**、**bridge（桥接）** 三者各自的角色与分工。
2. 画出一条完整并行迭代链的数据流图：数据从生产者流出，经过层层「适配器包装的消费者」，最终被 Reducer 归并成单个结果。
3. 解释 `ProducerCallback` 这个看似古怪的设计为什么存在——它解决的是 Rust 类型系统的哪个根本限制。

本讲是单元四的第一讲。前面三个单元我们一直在「使用」并行迭代器、阅读单个适配器的源码；从本讲开始，我们进入 `src/iter/plumbing/` 模块——Rayon 官方称之为 internals（内部构件），它是整个并行迭代器体系真正运转的发动机。

## 2. 前置知识

本讲假设你已经掌握以下内容（均来自前面讲义）：

- **两大根 trait 与两条驱动路径**（u2-l1）：`ParallelIterator` 有内部方法 `drive_unindexed`，`IndexedParallelIterator` 追加 `drive` 与 `with_producer`；看一个方法的返回类型就能判断它是惰性适配器还是立即执行的消费者。
- **适配器的结构模式**（u3-l1）：每个适配器是「上游迭代器 + 用户闭包」的小结构体；它的 `drive`/`drive_unindexed` 实现，就是把下游传来的消费者再包一层，然后转发给上游。
- **工作窃取调度**（u1-l1）：`join(a, b)` 把 b 入队后先执行 a，空闲线程可以偷走 b。本讲会看到 bridge 内部正是用 `join_context` 实现递归切分。

此外补充两个本讲要用到的 Rust 语言背景：

- **泛型单态化**：Rayon 的 trait 方法对具体类型泛型，编译期为每种组合生成专门代码，因此「包装一层消费者」通常是零运行时成本的。
- **关联类型与生命周期**：trait 的关联类型必须在 `impl` 头部就能写出一个确定的类型名。这个看似平淡的规则，正是 `ProducerCallback` 存在的根源（见 4.4 节）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md) | 官方设计文档（约 300 行），讲「为什么这样设计」：推/拉两种模式、执行流程、`ProducerCallback` 的来龙去脉。注意它讲的是设计而非用法。 |
| [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) | plumbing 的全部实体：`ProducerCallback`、`Producer`、`UnindexedProducer`、`Consumer`、`Folder`、`Reducer`、`UnindexedConsumer` 七个 trait，加上 `Splitter`/`LengthSplitter` 两个切分策略和 `bridge`/`bridge_unindexed` 两个桥接函数，总共不到 500 行。 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | 两大根 trait 的定义处。本讲只关注其中的内部驱动方法：`drive_unindexed`、`opt_len`、`len`、`drive`、`with_producer`。 |
| [src/iter/zip.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs) | README 中「从推模式切换到拉模式」的真实代码样本：`Zip` 如何用双层回调凑出 `ZipProducer`。 |

## 4. 核心概念与源码讲解

### 4.1 plumbing 总体架构：推模式与拉模式

#### 4.1.1 概念说明

串行迭代器只有一个方向：你调用 `next()`，从迭代器里**拉**（pull）出一个元素。并行迭代器为什么不能就这么简单？官方 README 开篇就点明了挑战：

> 并行迭代器比串行迭代器复杂，因为它们必须**能把自身拆成两半，并在两个半边并行地工作**。

「能拆成两半」这个需求让 Rayon 把参与计算的双方都抽象成了可分裂的对象，于是出现了两种视角：

- **拉模式（Pull mode）—— `Producer` / `UnindexedProducer`**：像普通迭代器一样按需产出下一个元素，但多了一个本事：可以**在产出之前先对半分裂**，两个一半交给不同线程。
- **推模式（Push mode）—— `Consumer` / `UnindexedConsumer`**：方向反过来，元素被**送进来**（`consume` 方法），更像 `for_each`。消费者同样可以分裂，分裂时还会附带产出一个 Reducer 负责合并两半结果。

一个关键的不对称性（README 称之为 variance，很妙）：

| | 索引世界（indexed） | 无索引世界（unindexed） |
| --- | --- | --- |
| 谁能驱动消费者 | 只有索引生产者能驱动 `Consumer` | **任何**生产者都能驱动 `UnindexedConsumer` |
| 谁能当这种消费者 | **所有**迭代器都能以 `Consumer` 模式工作 | 只有部分消费者可以（`for_each`、`reduce` 可以；`collect_into_vec` 不行，因为每个元素落在目标容器的哪个位置很关键） |

对应源码：[src/iter/plumbing/README.md:16-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L16-L31) 解释拉模式两种分裂方式（`split_at` 按下标 vs `split` 近似对半），[src/iter/plumbing/README.md:32-51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L32-L51) 解释推模式两种分裂方式（`split_at` vs `split_off_left`），而 [src/iter/plumbing/README.md:78-81](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L78-L81) 就是上面那张不对称表的原话。

#### 4.1.2 核心流程

README 用下面这条链作为贯穿示例（[src/iter/plumbing/README.md:58-63](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L58-L63)）：

```rust
vec1.par_iter()
    .zip(vec2.par_iter())
    .flat_map(some_function)
    .for_each(some_other_function)
```

执行时的构造顺序是**从链尾向链头**的：

1. `for_each` 是立即执行的消费者，它先创建一个 `ForEachConsumer`——最简单的消费者，收到元素就调用 `some_other_function`。
2. `for_each` 拿着这个消费者调用上游 `flat_map` 的 `drive_unindexed`，语义是「把你的元素喂给这个消费者」。
3. `FlatMap` 把 `ForEachConsumer` 包一层变成 `FlatMapConsumer`，继续调用更上游 `zip` 的 `drive_unindexed`。
4. 到了 `zip`，事情变了：**zip 无法作为消费者实现**——它必须让两条迭代器齐步走（lockstep），而一次只能调用一个 `drive`。于是 `Zip` 停止包装消费者，转而创建**生产者** `ZipProducer`（它需要两侧输入都是 `IndexedParallelIterator`，因为要从任意下标处开始配对），然后调用 `bridge` 函数把「生产者侧」与「消费者侧」接通。这正是 README「Switching from push to pull mode」一节（[src/iter/plumbing/README.md:101-129](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L101-L129)）的内容。
5. `bridge` 递归地把两侧不断对半切分（切到多细由切分策略决定），在叶子处把生产者转成串行迭代器**拉**出元素，逐个**推**给消费者的 `Folder`，最后用 Reducer 把各半结果合并回去。

数据流全景图：

```
 用户视角的链:
   vec1.par_iter() ──zip── vec2.par_iter() ──flat_map(f) ──for_each(g)
        │                                              │
        │ (链头到 zip：拉模式)                (链尾到 zip：推模式)
        ▼                                              ▼
 生产者侧(自链头向外包装):                   消费者侧(自链尾向内包装):
   SliceProducer(a)  ─┐                      ForEachConsumer(g)
   SliceProducer(b)  ─┴─> ZipProducer        FlatMapConsumer(包住 ForEach)
                                                    │
                                                    ▼
              ┌──────────── bridge(ZipProducer, FlatMapConsumer) ───────────┐
              │  递归: split_at(mid) 对半切生产者和消费者                    │
              │        └─ join_context(左半, 右半)   ← 工作窃取在此发生     │
              │  叶子: producer.into_iter() 拉元素 → folder.consume() 推元素 │
              │  回溯: reducer.reduce(left_result, right_result)            │
              └─────────────────────────────────────────────────────────────┘
```

一句话总结方向感：**消费者从链尾向链头层层包装（洋葱皮在外侧生长），生产者从链头向链尾层层包装，bridge 在「切换点」把两者焊在一起。**

#### 4.1.3 源码精读

驱动方法的定义在根 trait 上：

- [src/iter/mod.rs:2410-2412](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2410-L2412)——`ParallelIterator::drive_unindexed` 的声明：「让本迭代器开始产出元素并逐个喂给 consumer，途中可能先分裂消费者以制造并行机会」。这是无索引驱动路径的入口。
- [src/iter/mod.rs:3236](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3236)——`IndexedParallelIterator::drive` 的声明：与 `drive_unindexed` 同义，但分裂时会**告知切分下标**。
- [src/iter/mod.rs:3253](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3253)——`with_producer` 的声明：把迭代器转成生产者并交给回调，生产者类型不出现在签名里（原因见 4.4 节）。

`Zip` 的两个驱动入口都直接调用 `bridge`：

- [src/iter/zip.rs:30-35](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L30-L35)——`drive_unindexed` 的实现体只有一行 `bridge(self, consumer)`；[src/iter/zip.rs:37-39](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L37-L39) 的 `opt_len` 返回 `Some(self.len())`，向消费者透露长度以便走快速路径。
- [src/iter/zip.rs:47-52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L47-L52)——索引版 `drive` 同样是 `bridge(self, consumer)`。

这里有个初学者容易困惑的点：`drive_unindexed` 收到的是 `UnindexedConsumer`，为什么能传给要求 `Consumer` 的 `bridge`？因为 `UnindexedConsumer` 是 `Consumer` 的子 trait（见 4.2.3 中 [src/iter/plumbing/mod.rs:208](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L208) 的声明 `pub trait UnindexedConsumer<I>: Consumer<I>`）——「无索引消费者」是一种特殊的「消费者」，这正对应上表里「任何生产者都能驱动无索引消费者」。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍 README 描述的执行顺序，验证「消费者从链尾构造」的结论。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/iter/plumbing/README.md:65-99](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L65-L99)，通读「How iterator execution proceeds」一节。
2. 在纸上写下示例链的四个阶段名：`for_each` → `flat_map` → `zip` → `bridge`。
3. 在源码中找到 `ForEachConsumer` 的定义（提示：在 [src/iter/for_each.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs) 内搜索 `struct ForEachConsumer`，它的 `consume` 方法就是一行闭包调用）。
4. 找到 `FlatMap` 的 `drive_unindexed`（在 [src/iter/flat_map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map.rs) 中），确认它做的事是「包装消费者再转发上游」。

**需要观察的现象**：`ForEachConsumer` 的 `consume` 是否真的只有一行；`FlatMap` 的驱动方法里是否出现了「new 一个包装消费者」的模式。

**预期结果**：两个文件都能找到对应结构，且代码形态与 u3-l1 讲过的适配器骨架一致——这证明 README 的叙述与真实代码一一对应。本实践为源码阅读型，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `zip` 不能实现成一个消费者，而 `map` 可以？

**答案**：`map` 是逐元素独立变换，来一个变一个，天然适合推模式。`zip` 必须让两条数据流**齐步走**——第 k 个左元素配第 k 个右元素；而推模式一次只能驱动一个消费者、一条数据流，无法同时向两侧要数据（除非引入中间线程和 channel，代价太高）。所以 `zip` 必须切换到拉模式：直接按下标把两侧各自切开，从任意下标处开始配对。这也是 `zip` 要求输入实现 `IndexedParallelIterator` 的根本原因。

**练习 2**：`collect_into_vec` 为什么不能作为 `UnindexedConsumer` 工作，`for_each` 却可以？

**答案**：`collect_into_vec` 要把第 i 个元素写进目标 Vec 的第 i 个位置，位置信息至关重要，而无索引消费者分裂时（`split_off_left`）不知道自己处理的数据落在整体流的哪个区间。`for_each` 对每个元素独立执行副作用，不关心顺序与位置，所以两边怎么分都能正确工作。

**练习 3**：判断对错：「所有生产者都能驱动无索引消费者，但只有索引生产者能驱动索引消费者。」

**答案**：对。这正是 README [第 78-81 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L78-L81) 的原文含义。原因：无索引消费者对「会收到多少数据、数据在流中的位置」没有任何要求，任何生产者喂它都行；索引消费者则依赖 `split_at(index)` 的精确语义，只有知道长度的索引生产者才能满足。

### 4.2 Producer 与 Consumer 的契约

#### 4.2.1 概念说明

如果把一次并行计算比作一条流水线，契约双方分别是：

- **Producer（生产者）**：官方定义是「**可分裂的 `IntoIterator`**」。它随时可以变成一个普通串行迭代器按需产出元素；但在那之前，你可以先调 `split_at(index)` 把它掰成「产出前 index 个」和「产出第 index 个起」的两个生产者，各自独立继续分裂或变成迭代器。线程之间的分工就靠这个分裂完成。
- **Consumer（消费者）**：官方定义是「**广义的 fold 操作**」。每个消费者最终会变成一个 `Folder`（串行地吃元素）；特殊之处在于它也能分裂——`split_at(index)` 一次返回**两个消费者加一个 Reducer**：两个消费者各自独立吃数据，吃完后由 Reducer 把两份结果合并成一份。
- **Folder（折叠器）**：消费者的一次性运行形态，`consume(item)` 吃一个、`complete()` 出最终结果。
- **Reducer（归并器）**：分裂的善后者，`reduce(left, right)` 把两半结果合一。

无索引世界还有两个平行变体：`UnindexedProducer`（不知道长度、只能「请你在中间某处分裂」）与 `UnindexedConsumer`（无分裂、可自由复制的消费者，用 `split_off_left` 分裂且不接收下标）。

#### 4.2.2 核心流程

把一个长度为 N 的索引生产者 P 和消费者 C 接通后的完整时序：

1. 询问 C 是否已满（`full()`）——短路检查。
2. 切分策略判断还要不要再切；要切则取中点 \( m = N/2 \)。
3. `P.split_at(m)` → 左生产者（0..m）、右生产者（m..N）。
4. `C.split_at(m)` → 左消费者、右消费者、Reducer。
5. 两半各自递归（通过 `join_context` 并行，空闲线程会偷走右半）。
6. 递归到叶子：`consumer.into_folder()` 把消费者变成 Folder，`producer.fold_with(folder)` 把生产者的元素逐个推进去。
7. `folder.complete()` 得到该半的结果。
8. 回溯途中不断 `reducer.reduce(left, right)`，最终汇成单个结果。

七个 trait 的职责速查表：

| trait | 关键方法 | 一句话职责 |
| --- | --- | --- |
| `Producer` | `split_at(index)`、`into_iter()`、`min_len/max_len` | 可按下标分裂的数据源 |
| `UnindexedProducer` | `split()`、`fold_with()` | 只能近似对半分裂的数据源 |
| `Consumer` | `split_at(index)`、`into_folder()`、`full()` | 可分裂的接收端，分裂时附赠 Reducer |
| `UnindexedConsumer` | `split_off_left()`、`to_reducer()` | 无下标分裂的接收端（要求可自由复制） |
| `Folder` | `consume(item)`、`complete()`、`full()` | 串行吃元素的运行形态 |
| `Reducer` | `reduce(left, right)` | 合并两半结果 |
| `ProducerCallback` | `callback<P>(producer)` | 「拿到生产者后做什么」的回调（见 4.4） |

#### 4.2.3 源码精读

**Producer 契约**——[src/iter/plumbing/mod.rs:56-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L56-L109)：注意它不继承 `IntoIterator`，而是把 `IntoIter` 作为关联类型内联声明（文档注释解释了原因：rust-lang/rust#20671 使 trait 无法在 `IntoIterator` 上叠加 `DoubleEndedIterator + ExactSizeIterator` 约束）。核心几行：

```rust
pub trait Producer: Send + Sized {
    type Item;
    type IntoIter: Iterator<Item = Self::Item> + DoubleEndedIterator + ExactSizeIterator;
    fn into_iter(self) -> Self::IntoIter;
    fn min_len(&self) -> usize { 1 }        // 默认切到单个元素
    fn max_len(&self) -> usize { usize::MAX }
    fn split_at(self, index: usize) -> (Self, Self);
    fn fold_with<F>(self, folder: F) -> F where F: Folder<Self::Item> {
        folder.consume_iter(self.into_iter())
    }
}
```

其中 [第 97 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L97) 的 `split_at` 是整个契约的心脏：左半产出 `0..index`，右半产出 `index..N`。注意文档特意强调：生产者产出的个数 N **不通过 API 暴露**，由消费者负责记账——这是一个刻意的分工设计。[第 103-108 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L103-L108) 的 `fold_with` 给出了默认实现「变成迭代器然后灌给 folder」，这正是 4.1.2 图中「叶子处拉转推」的那一步。

**UnindexedProducer 契约**——[src/iter/plumbing/mod.rs:231-243](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L231-L243)：`split(self) -> (Self, Option<Self>)`——「请在中间某处分裂；实在分不了就返回 `None`」。适用于长度未知（如 `&str` 的字符）或长度装不进 `usize`（如 32 位平台上的 `Range<u64>`）的场景。

**Consumer 契约**——[src/iter/plumbing/mod.rs:123-146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L123-L146)：三个关联类型 `Folder`/`Reducer`/`Result` 说明了它的一生；[第 137 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L137) 的 `split_at` 一次返回三元组 `(左消费者, 右消费者, Reducer)`；[第 145 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L145) 的 `full()` 是短路钩子（`find_any`、`try_reduce` 全靠它提前喊停）。

**Folder 契约**——[src/iter/plumbing/mod.rs:154-188](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L154-L188)：`consume` 吃一个返回新状态（值语义、链式更新，和 `std::iter::Iterator::fold` 里的累加器一个思路）；[第 169-180 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L169-L180) 的 `consume_iter` 是可覆写的默认实现——循环 `consume` 并在 `full()` 时提前break，覆写它通常是为了更高效的特化版本（例如直接 `extend` 一个切片）。

**Reducer 契约**——[src/iter/plumbing/mod.rs:197-201](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L197-L201)：只有一个方法 `reduce(left, right)`，在每次分裂之后执行。

**UnindexedConsumer 契约**——[src/iter/plumbing/mod.rs:208-221](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L208-L221)：注意 `split_off_left(&self) -> Self` 接收的是**共享引用**、返回一个新副本——这要求实现者是无状态、可自由复制的。文档注释还点出一个语义细节：返回的「左侧」消费者产出的值在 `find_first` 这类方法中优先于 `self`（右侧），左右身份不可互换。

#### 4.2.4 代码实践

**实践目标**：感受「Producer 契约的实现者遍布全仓库」，建立按图索骥的能力。

**操作步骤**（源码检索型实践，本讲义编写时已实际执行过检索，结果可核对）：

1. 在仓库根目录执行（等价的 ripgrep 命令）：

   ```bash
   rg -o "impl(<[^>]*>)?\s+\w*\s*Producer\s+for" src/ --count-matches
   ```

2. 观察命中文件分布，把结果按目录归类：`src/slice/`（切片家族）、`src/iter/`（适配器）、`src/str.rs`、`src/range.rs` 等。

**需要观察的现象**：命中总数；哪些文件实现了 `Producer`（索引）、哪些实现了 `UnindexedProducer`（无索引）。

**预期结果**：共约 **49 处** `Producer`/`UnindexedProducer` 实现，分布在约 **31 个文件**（本讲义编写时实测：`src/slice/mod.rs` 2 处、`src/slice/chunks.rs` 4 处、`src/slice/rchunks.rs` 4 处、`src/str.rs` 7 处全部为 `UnindexedProducer`、`src/iter/zip.rs` 1 处等）。特别验证一个结论：**`src/str.rs` 里全是无索引生产者**——因为 UTF-8 变长编码使字符无法按下标定位，这呼应了 u2-l2 讲过的字符串特例。

#### 4.2.5 小练习与答案

**练习 1**：`Producer::split_at(index)` 要求 `index <= N`，但 N 不能通过任何方法查询。那么 bridge 递归时怎么保证不越界切？

**答案**：长度由**消费者一侧记账**：`bridge` 先取 `par_iter.len()`（`IndexedParallelIterator` 的方法），随后每层递归把 `len` 与 `mid = len/2` 一路传下去，生产者和消费者在同一个 mid 上对齐切分。生产者自己不需要知道总长，它只管「从我这儿切下前 index 个」。

**练习 2**：`UnindexedConsumer::split_off_left` 为什么拿 `&self` 就能分裂，而 `Consumer::split_at` 要拿 `self`（按值）？

**答案**：无索引消费者必须无状态、可自由复制（文档原话 "A stateless consumer can be freely copied"），所以共享引用即可克隆出一份当「左侧」。索引消费者的 `split_at` 按值消费 `self`，是因为它可以把内部状态（如目标 Vec 的区间信息）真正地一分为二，左右各持有一半的所有权。

**练习 3**：`Folder::full()` 与 `Consumer::full()` 两个钩子分别在哪个层面起作用？

**答案**：`Consumer::full()` 在**任务/分裂层面**起作用——bridge 每层递归入口都会先问一次，已满就不再分裂、直接收尾（见 4.3.3 的 helper 代码）；`Folder::full()` 在**元素层面**起作用——`consume_iter` 的默认循环每吃一个元素检查一次，满了立刻停止拉取后续元素。两者配合实现了 `find_any`/`try_reduce` 的全管道短路。

### 4.3 bridge：把生产者与消费者接通

#### 4.3.1 概念说明

`bridge` 是 plumbing 的「总装车间」。前面我们看到：消费者从链尾包过来、生产者从链头包过来，`bridge` 负责把两者接在一起并驱动整个递归切分过程。它回答三个问题：

1. **何时停止切分？**——由切分策略 `Splitter`/`LengthSplitter` 决定。
2. **怎么并行？**——用 `join_context` 把左右两半递归派发出去（工作窃取在此发生）。
3. **叶子上干什么？**——生产者变串行迭代器，元素逐个喂给 Folder，最后 Reducer 逐层合并。

切分策略采用的是 **thief-splitting（窃取自适应）**：初始切分预算设为线程数 P；每切一刀预算减半；一旦发现某半是被别的线程**偷走**执行的（`join_context` 的 `migrated` 标志），说明当前并行度不够用，预算重置回 P。这样任务数的期望约为不小于 \( P \) 的最小 2 的幂，即 \( 2^{\lceil \log_2 P \rceil} \)——既保证每线程至少有一份活干，又不会切出成千上万个小任务。

#### 4.3.2 核心流程

`bridge` 的递归逻辑用伪代码表达（省略类型）：

```
bridge(par_iter, consumer):
    len = par_iter.len()
    return par_iter.with_producer(回调: |producer| ->
        bridge_producer_consumer(len, producer, consumer))

bridge_producer_consumer(len, producer, consumer):
    splitter = LengthSplitter(producer.min_len(), producer.max_len(), len)
    return helper(len, stolen=false, splitter, producer, consumer)

helper(len, migrated, splitter, producer, consumer):
    if consumer.full():                      # 短路：已有答案
        return consumer.into_folder().complete()
    if splitter.try_split(len, migrated):    # 还该继续切
        mid = len / 2
        (p_left, p_right) = producer.split_at(mid)
        (c_left, c_right, reducer) = consumer.split_at(mid)
        (r_left, r_right) = join_context(    # 左右并行，可被窃取
            |_| helper(mid, ctx.migrated(), splitter, p_left, c_left),
            |_| helper(len-mid, ctx.migrated(), splitter, p_right, c_right))
        return reducer.reduce(r_left, r_right)
    else:                                    # 叶子：拉转推
        return producer.fold_with(consumer.into_folder()).complete()
```

`bridge_unindexed` 的结构相同，差别只有两处：切分判断不带长度（只用 `Splitter`）；生产者的 `split()` 可能返回 `None`（数据已不可再分），此时直接落入串行叶子。

#### 4.3.3 源码精读

**bridge 本体**——[src/iter/plumbing/mod.rs:346-371](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L346-L371)：先取 `len`，然后 `par_iter.with_producer(Callback { len, consumer })`。函数体内部定义的 `struct Callback`（[第 354-357 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L354-L357)）实现了 `ProducerCallback`，在回调里转调 `bridge_producer_consumer`——这是「回调拿生产者」的最小真实样本。

**递归核心**——[src/iter/plumbing/mod.rs:385-435](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L385-L435)：

- [第 390 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L390) 用生产者的 `min_len`/`max_len` 与 `len` 构造 `LengthSplitter`——`with_min_len`/`with_max_len`（u3-l3 讲过的粒度控制）正是在这里生效。
- [第 404-405 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L404-L405) 每层递归先做 `consumer.full()` 短路检查。
- [第 406-409 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L406-L409) 在同一个 `mid` 上同时切分生产者与消费者——**两边必须对齐**，否则左半数据会被喂给右半消费者。
- [第 410-429 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L410-L429) 用 `join_context` 递归两半，`context.migrated()` 把「这半是否被窃取」传给下一层，供切分预算参考。
- [第 430 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L430) Reducer 合并；[第 432 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L432) 串行叶子：`producer.fold_with(consumer.into_folder()).complete()`。

**Splitter（窃取自适应预算）**——[src/iter/plumbing/mod.rs:251-284](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L251-L284)：`new()` 把预算初始化为 `current_num_threads()`（[第 260-264 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L260-L264)）；`try_split`（[第 267-283 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L267-L283)）的三分支：被窃取→预算重置为 max(线程数, 剩余/2) 并放行；有预算→预算减半并放行；没预算→拒绝切分。结构体上的文档注释也说明了「由于总是除以二，实际份数是 `next_power_of_two()`」。

**LengthSplitter（带长度约束）**——[src/iter/plumbing/mod.rs:289-333](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L289-L333)：`new` 里 `min_splits = len / max`（[第 318 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L318)，注释举例：len 12345 / max 100 = 123 → 实际 128 份）；`try_split`（[第 329-332 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L329-L332)）的判据是 `len / 2 >= self.min && inner.try_split(stolen)`——**min 是硬下界，max 只保证至少切够次数**，这与 u3-l3 的结论互相印证。

**bridge_unindexed**——[src/iter/plumbing/mod.rs:438-476](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L438-L476)：注意 [第 462-463 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L462-L463) 的写法 `(consumer.to_reducer(), consumer.split_off_left(), consumer)`——先借共享引用造出 Reducer 和左侧副本，再把 `consumer` 本尊当作右侧使用，一行完成三分身；[第 471 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L471) 处理 `split()` 返回 `None`（不可再分）时直接串行收尾。

#### 4.3.4 代码实践

**实践目标**：用一段可运行的程序，亲眼看到 LengthSplitter 决定的「每段元素数上限」。

**操作步骤**：

1. 在 u1-l3 创建的示例工程（或新建一个依赖 rayon 的工程）中写入以下 **示例代码**（改编自 `with_max_len` 的官方文档示例，见 [src/iter/mod.rs:3189-3204](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3189-L3204)）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       // 每个并行任务内部用 fold 数一数自己吃了多少个元素
       let max = (0..1_000_000)
           .into_par_iter()
           .with_max_len(1234)
           .fold(|| 0, |acc, _| acc + 1) // 统计本段元素个数
           .max()
           .unwrap();

       println!("最长的一段包含 {max} 个元素");
       assert!(max <= 1234);
   }
   ```

2. 用 `cargo run --release` 运行。

**需要观察的现象**：打印出的「最长一段」的元素数；尝试把 `with_max_len(1234)` 改成 `with_max_len(1)`、`with_min_len(100_000)` 再运行对比。

**预期结果**：最长一段不超过 1234 个元素。按 [src/iter/plumbing/mod.rs:317](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L317) 注释的算法，1_000_000 / 1234 ≈ 810 → 实际约 1024 段（1024 是不小于 810 的最小 2 的幂），每段约 977 个元素，自然满足上限。注意**切分份数是「约 2 的幂」**，所以不必恰好等于 810。具体数值受线程数与窃取情况影响，若在本地观察到轻微差异属正常现象（待本地验证具体数值）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `helper` 递归中的 `join_context` 换成先顺序执行左闭包再执行右闭包，程序结果会变吗？性能会怎样？

**答案**：结果不变——两半消费的是不相交的元素区间，合并只依赖 Reducer 的结合性，与执行顺序无关。但性能会坍缩成串行：`join_context` 的价值在于右半入队后可被空闲线程偷走，换成顺序执行就没有任何并行度了，Rayon 也就退化成一个普通串行库。

**练习 2**：8 线程机器上，一个很大长度、未设置 `with_min_len`/`with_max_len` 的迭代器，大约会被切成几段？

**答案**：约 \( 2^{\lceil \log_2 8 \rceil} = 8 \) 段起步。预算从 8 开始逐层减半（8→4→2→1→0），共切约 3 刀得 8 段。若执行中发生窃取，预算重置回 8，切分还会继续加深；所以它是自适应的下限而非定值。

**练习 3**：`bridge` 与 `bridge_unindexed` 的消费者类型约束不同（`C: Consumer<I::Item>` vs `C: UnindexedConsumer<P::Item>`），为什么无索引版必须额外要求 `UnindexedConsumer`？

**答案**：无索引生产者给不出切分下标，只能「近似对半」地分裂；这时消费者若还坚持按下标语义分裂（`split_at`），两边无法对齐。`UnindexedConsumer::split_off_left` 不承诺任何区间位置，恰好匹配「分出来的两半各自处理任意数量数据」的现实。

### 4.4 ProducerCallback：为什么需要回调

#### 4.4.1 概念说明

最后回答本讲第三个目标问题：`with_producer` 为什么长成回调的样子，而不是直接返回一个生产者？

理想世界里我们想这样写（README 中的假想代码）：

```rust
base_iter.with_producer(|base_producer| { /* 拿到生产者做点事 */ });
```

比如 `map` 适配器：拿到上游的生产者，包一层变成 `MapProducer`，再传给自己的调用方。麻烦在于**类型**。如果用闭包实现，trait 签名必须写出生产者的类型，只能引入关联类型：

```rust
pub trait IndexedParallelIterator: ParallelIterator {
    type Producer;
    fn with_producer<CB, R>(self, callback: CB) -> R
        where CB: FnOnce(Self::Producer) -> R;
    ...
}
```

而 `MapProducer` 里需要持有 `&self.map_op` 这样的引用（让分裂后的多个生产者共享闭包），这个引用的生命周期 `'f` 指向 `with_producer` **函数体的内部**——它无法出现在 `impl` 头部的关联类型里，因为每次调用的 `'f` 都不一样。这正是 Rust 著名的「 lending trait / Iterable 」难题。解决方案有二：

1. 等关联类型构造器（ATC，RFC 1598）稳定——等不到；
2. 用一个**专门的回调 trait**，让 `callback` 方法对**所有**生产者 `P` 泛型：

```rust
pub trait ProducerCallback<T> {
    type Output;
    fn callback<P>(self, producer: P) -> Self::Output
        where P: Producer<Item = T>;
}
```

签名 `fn with_producer<CB: ProducerCallback<Self::Item>>(self, callback: CB) -> CB::Output` **从头到尾不需要命名生产者类型**——生产者只作为泛型参数 `P` 出现在方法内部，生命周期难题凭空消失。代价是失去闭包语法糖：每个实现都要手写一个「捕获了所需变量的结构体 + 纯样板式的 trait 实现」。README 的原话是「OK, a bit tedious, but it works!」

#### 4.4.2 核心流程

以 `a.zip(b)` 为例，回调链是这样传递的（真实代码见 4.4.3）：

```
调用方 ──callback──▶ Zip::with_producer
                        │ 捕获: b（整个右侧迭代器）+ 调用方的 callback
                        ▼
                 CallbackA ──callback(a_producer)──▶ a 的生产者到手
                        │ 捕获: a_producer + 调用方的 callback
                        ▼
                 CallbackB ──callback(b_producer)──▶ b 的生产者到手
                        │ 组装: ZipProducer { a, b }
                        ▼
                 调用方的 callback.callback(ZipProducer)
```

两层回调各等一个生产者：先从 `a` 拿到 `a_producer`，再向 `b` 要 `b_producer`，两个都到手后拼成 `ZipProducer` 交给最初的调用方。`bridge` 内部那个匿名 `Callback`（4.3.3 已见）拿到 `ZipProducer` 后就开始 `bridge_producer_consumer`。

#### 4.4.3 源码精读

**问题陈述**——[src/iter/plumbing/README.md:142-249](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L142-L249)：完整推演了「理想闭包签名 → 必须加关联类型 `Producer` → `'f` 生命周期无处安放（[第 229-239 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L229-L239) 的 `// wait, what is this 'f?`）→ ATC 或回调 trait 两条路」的全过程。

**最终形态**——[src/iter/plumbing/README.md:253-265](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L253-L265)：`ProducerCallback` 的定义与「签名永不需要命名生产者类型」的说明。

**真实 trait**——[src/iter/plumbing/mod.rs:17-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L17-L30)：与 README 一致，`callback<P>` 对所有 `P: Producer<Item = T>` 泛型。配合 [src/iter/mod.rs:3253](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3253) 的 `with_producer` 签名使用。

**样板的真实模样**——[src/iter/zip.rs:58-65](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L58-L65)：`Zip::with_producer` 一行转出 `CallbackA`；[src/iter/zip.rs:67-88](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L67-L88)：`CallbackA` 捕获 `b` 和上层回调，等 `a` 的生产者到位后再向 `b` 发起 `with_producer(CallbackB)`；[src/iter/zip.rs:90-111](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L90-L111)：`CallbackB` 捕获 `a_producer`，等 `b` 的生产者到位后组装 [ZipProducer](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L117-L119) 并触发最外层回调。两个 `struct Callback*` 内嵌在函数体里的写法，正是 README「手动闭包」建议的落地。

另外值得一提：回调方案还有个附赠好处（README [第 193-205 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L193-L205)）——`with_producer` 按值拿走 `self`，可以把迭代器拥有的资源（例如 `&mut` 切片）的所有权直接转移进生产者，或在执行期间创建临时资源而不必归还。

#### 4.4.4 代码实践

**实践目标**：通过手动「人肉展开」一条回调链，确认你真的理解了回调的方向与捕获内容。

**操作步骤**（源码阅读型实践）：

1. 阅读 [src/iter/zip.rs:58-111](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L58-L111)，然后合上电脑，在纸上回答三个问题：
   - `CallbackA` 的两个字段分别捕获了什么？
   - `CallbackB` 的 `callback` 被调用时，参数 `b_producer` 是谁生产出来的？
   - 最终 `ZipProducer` 是在哪一行、由哪两个字段拼装的？
2. 再看一个更简单的样本：[src/iter/plumbing/mod.rs:354-370](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L354-L370) 中 `bridge` 内联定义的 `Callback`，对比它只有一个 `consumer` 字段的极简形态。

**需要观察的现象**：两个 Callback 结构体都是「字段 = 需要从创建作用域捕获的变量」；`impl ProducerCallback` 的函数体就是「假想闭包的函数体」。

**预期结果**：纸上答案依次是——`CallbackA { callback（上层回调）, b（右侧迭代器） }`；`b_producer` 由 `self.b.with_producer`（即 `b` 自己的 `with_producer` 实现）产出；在 [src/iter/zip.rs:106-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L106-L109) 由 `a: self.a_producer, b: b_producer` 拼装。全部对上即通过。

#### 4.4.5 小练习与答案

**练习 1**：如果 Rust 明天稳定了 ATC（关联类型构造器），`ProducerCallback` 还需要吗？

**答案**：理论上不需要——可以让 `IndexedParallelIterator` 拥有类似 `type Producer<'a>` 的泛型关联类型，`with_producer` 就能直接用 `FnOnce` 闭包，语法糖回归。但重写整个生态的收益很低、破坏面很大，Rayon 一直维持回调方案。这也提示我们：这是一个**语言能力缺口下的工程妥协**，而非 Rayon 独有的怪癖。

**练习 2**：`with_producer` 为什么按值拿 `self`（`fn with_producer(self, ...)`）而不是 `&self`？

**答案**：因为生产者要**拥有**数据的访问权才能安全分裂与转移。例如持有 `&mut` 切片的迭代器，必须把所有权移进生产者，分裂时才能把左右两半的可变借用分给两个子生产者。按借用返回生产者将无法表达这种所有权转移（详见 README [第 193-205 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L193-L205) 的论述）。

**练习 3**：`bridge` 里的 `Callback` 结构体为什么必须定义在函数体内部，而不是放在模块顶层？

**答案**：它需要捕获泛型参数 `C`（具体的消费者类型），且只服务于 `bridge` 这一个调用点。定义在函数体内可以让泛型约束（`impl<C, I> ProducerCallback<I> for Callback<C> where C: Consumer<I>`）紧贴使用处，不污染模块命名空间——这是 Rust 中「函数内定义带泛型的私有辅助类型」的惯用法，zip.rs 的 `CallbackA`/`CallbackB` 同理。

## 5. 综合实践

把本讲三个模块串成一个任务：**亲手绘制并标注一张 plumbing 数据流图**。

**任务描述**：

1. **画图**：用你喜欢的工具（纸笔、draw.io、mermaid 均可）画出下面这条链的完整数据流图：

   ```rust
   v1.par_iter()
     .zip(v2.par_iter())
     .map(|(a, b)| a + b)
     .sum::<i32>()
   ```

   图上必须体现：消费者侧从 `SumConsumer`（链尾）开始、经 `MapConsumer` 包装、传到 `Zip`；生产者侧从两个切片生产者开始、组装成 `ZipProducer`、再包成 `MapProducer`；`bridge` 在中间递归切分，叶子处拉转推，回溯处 Reducer 合并。

2. **对照标注**：打开 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md)，把它的小节与 [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) 中的实体一一对应，标到图的相应位置。参考映射表：

   | README 小节 | mod.rs 中的实体（行号为当前 HEAD 实测） |
   | --- | --- |
   | The challenge——拉模式（L16-L31） | `Producer`（L56）、`UnindexedProducer`（L231） |
   | The challenge——推模式（L32-L51） | `Consumer`（L123）、`UnindexedConsumer`（L208） |
   | How iterator execution proceeds（L53-L99） | `drive_unindexed`（iter/mod.rs L2410） |
   | Switching from push to pull mode（L101-L129） | `drive`（iter/mod.rs L3236）、zip.rs L30-L52 |
   | The base case（L131-L136） | `bridge_producer_consumer` 叶子分支（L432） |
   | What on earth is ProducerCallback（L138 起） | `ProducerCallback`（L17）、`with_producer`（iter/mod.rs L3253） |

3. **验证**（可选加分项）：把 4.3.4 的 `with_max_len` 程序抄进工程跑一遍，把你观察到的「最长一段元素数」标注在图中 bridge 的切分策略旁边，作为 LengthSplitter（mod.rs L289-L333）行为的实证。

**检查标准**：图上能回答这三个问题即算完成——数据在哪一步从「拉」变成「推」？消费者和生产者各自朝哪个方向包装？`ProducerCallback` 在图中出现在哪个环节？

## 6. 本讲小结

- Rayon 把并行计算双方都抽象成**可分裂**的对象：拉模式的 `Producer`（可分裂的 `IntoIterator`）与推模式的 `Consumer`（可分裂的广义 fold），后者分裂时附赠 `Reducer` 负责合并。
- **消费者从链尾向链头层层包装，生产者从链头向外层层包装**；遇到 `zip` 这类需要协调多路输入的适配器时切换方向，由 `bridge` 把两侧焊在一起。
- `bridge`/`bridge_producer_consumer` 的递归骨架是：`full()` 短路检查 → 切分策略裁决 → 同一个 `mid` 上对齐切分生产者与消费者 → `join_context` 并行两半（工作窃取在此发生）→ Reducer 逐层合并；叶子处 `into_iter()` 拉出元素、`consume()` 推给 Folder。
- 切分份数由 **thief-splitting** 自适应决定：预算从线程数起步逐刀减半、被窃取时重置，期望份数约为不小于线程数的最小 2 的幂；`LengthSplitter` 在此之上叠加 `with_min_len` 的硬下界与 `with_max_len` 的最少切分次数。
- `ProducerCallback` 的存在是为了绕开「生产者类型携带指向 `with_producer` 函数体内部的引用生命周期、因而无法写成关联类型」这一语言限制；代价是每个实现都要手写「捕获结构体 + 样板 impl」。

## 7. 下一步学习建议

本建立好了 plumbing 的整体图景，接下来三讲分别从三个方向深入：

1. **u4-l2（Producer：可分裂的生产者）**：动手为自定义类型实现 `Producer` 的 `split_at`/`len`，并用 `tests/producer_split_at.rs` 的测试宏验证契约。
2. **u4-l3（Consumer 与驱动流程）**：追踪 `for_each` 从 `ParallelIterator::for_each` 到 plumbing 的完整调用链，并手写一个带日志的 Consumer。
3. **u4-l4（collect 的内部实现）**：看 `CollectConsumer` 如何预分配目标 Vec 并按段写入——那是 `Consumer::split_at` 区间记账思想最精彩的应用。

建议同时把 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md) 完整重读一遍——第一遍读它是在本讲之前的话，现在带着源码细节再读，会有完全不同的收获。
