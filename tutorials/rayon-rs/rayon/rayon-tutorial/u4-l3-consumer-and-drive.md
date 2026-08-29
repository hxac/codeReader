# Consumer 与驱动流程：for_each 的完整调用链

## 1. 本讲目标

上一讲（u4-l2）我们读完了拉模式一侧的 Producer；本讲转到推模式一侧的 **Consumer**。学完本讲，你应该能够：

1. 说出 `Consumer` trait 三个关联类型（`Folder` / `Reducer` / `Result`）的分工，并描述一个消费者从 `split_at` 到 `reduce` 的完整生命周期。
2. 区分 `drive` 与 `drive_unindexed` 两条驱动路径：谁走 `bridge`（中点精确切分），谁走 `bridge_unindexed`（按能力切分），以及 `UnindexedConsumer::split_off_left` 为何不需要下标。
3. 把 `par_iter().for_each(...)` 这一行代码从 `ParallelIterator::for_each` 一路追到 plumbing 层的 `bridge` 递归与 `join_context`，指出工作窃取发生在哪一行。
4. 亲手实现一个自定义 `Consumer`（LogConsumer），挂在 `par_iter().map(...).drive(...)` 上，观察任务的切分顺序与线程分布。

## 2. 前置知识

本讲默认你已掌握 u4-l1 的 plumbing 三角色图景，这里做最小回顾并补充两个新概念。

- **Producer（生产者）**：拉模式，「可切分的 `IntoIterator`」，上一讲的主角。
- **Consumer（消费者）**：推模式，官方文档称它是[广义的 fold 操作][fold-doc]——拥有一个「累积状态」，逐个吃进元素，最后产出结果。它的特殊之处在于：像 Producer 一样**可以被切分**，切分后各半独立吃元素，最后用一个 `Reducer` 把两份结果合并。
- **Folder（折叠器）**：Consumer 的「串行形态」。并行世界里负责切分的是 Consumer；切到叶子、不再并行时，Consumer 转换（`into_folder`）成 Folder，由 Folder 在单线程里一个一个吃元素。
- **drive（驱动）**：让并行迭代器「跑起来」的内部方法。用户调用的 `for_each` / `sum` / `collect` 最终都会构造一个 Consumer，然后调用 `drive` 或 `drive_unindexed` 把数据「推」进消费者。
- **join_context**：rayon-core 提供的并行原语，并行执行两个闭包并等待两者结果；与 `join` 的区别是闭包能拿到一个 `FnContext`，从中查询「我是否被别的线程窃取了」。它是 plumbing 切分递归的发动机，本讲 4.4 节展开。

一个直觉类比：Producer 是「可对半掰开的巧克力块」，Consumer 是「可对半克隆的胃」，`bridge` 负责把两者在同一位置掰开、分给不同线程吃、最后用 Reducer 把两份「消化结果」加起来。

[fold-doc]: https://github.com/rayon-rs/rayon/blob/main/src/iter/plumbing/README.md

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/iter/plumbing/mod.rs` | 定义 `Consumer` / `Folder` / `Reducer` / `UnindexedConsumer` 四个 trait，以及 `bridge` / `bridge_unindexed` 两个驱动引擎 |
| `src/iter/for_each.rs` | `for_each` 的实现：最简消费者 `ForEachConsumer`，本讲的主样例 |
| `src/iter/noop.rs` | `NoopReducer` 等无操作占位类型，供 `for_each` / `skip` 等复用 |
| `src/iter/mod.rs` | `ParallelIterator::for_each` 入口，以及 `drive_unindexed` / `drive` / `with_producer` 的 trait 声明 |
| `src/iter/map.rs` | 适配器一側的驱动样板：`Map` 如何包装消费者后转发给上游 |
| `src/slice/mod.rs` | 数据源一側的驱动样板：切片 `Iter` 如何落到 `bridge` |
| `src/iter/collect/consumer.rs` | `CollectConsumer`：一个「真实复杂」的消费者对照组 |
| `rayon-core/src/join/mod.rs` | `join_context` 本体：窃取发生的现场 |

## 4. 核心概念与源码讲解

本讲按规格覆盖三个最小模块并外加一个衔接模块：4.1 Consumer trait 契约、4.2 两条驱动路径（衔接 Producer 一讲）、4.3 for_each 完整调用链、4.4 join_context 驱动引擎。

### 4.1 Consumer trait：可分裂的「广义 fold」

#### 4.1.1 概念说明

`Consumer` 是推模式的核心抽象。它回答的问题是：**「数据来了以后，怎么处理、怎么切分着处理、怎么把各路结果合回来」**。

一个消费者要能参与并行计算，必须具备三种能力，分别由三个关联类型承载：

| 关联类型 | 职责 | 何时登场 |
| --- | --- | --- |
| `Self::Folder` | 串行吃元素的状态机 | 叶子任务里，`into_folder()` 产出它 |
| `Self::Reducer` | 合并左右两半的结果 | 每次 `split_at` 时产出，递归返回时使用 |
| `Self::Result` | 最终结果的类型 | 贯穿全程，必须是 `Send`（要跨线程搬运） |

注意 `Result: Send` 这条约束的含义：既然结果可能由别的线程算出来再送回来，它就必须能安全地跨线程转移。

#### 4.1.2 核心流程

一个 Consumer 的完整生命周期如下：

```text
                        ┌──────────────────────────────┐
                        │  Consumer（尚未开始消费）      │
                        └──────────────┬───────────────┘
                    splitter 说「还要切」│
                                       ▼
              consumer.split_at(mid) ──► (左 Consumer, 右 Consumer, Reducer)
                 │                                    │
        递归处理左半                          递归处理右半（可能被窃取到别的线程）
                 │                                    │
                 └──────────────┬─────────────────────┘
                splitter 说「不切了」（叶子任务）
                                ▼
              consumer.into_folder() ──► Folder
                                ▼
       folder.consume(item) 逐个吃元素（或 consume_iter 批量吃）
                                ▼
              folder.complete() ──► Result（叶子结果）
                                ▼
       Reducer::reduce(left_result, right_result) 逐层向上合并
                                ▼
                        最终的 Result
```

三个要点：

1. **切分是「消费者自己分裂」**：`split_at(index)` 按值消费 `self`，吐出两个新消费者与一个归约器——与 `Producer::split_at` 完全对称，两侧在同一个 `mid` 上对齐切。
2. **吃元素是 Folder 的事**：`Consumer::into_folder` 完成从「并行协议参与者」到「串行状态机」的角色转换；`Folder::consume` 吃一个元素返回新状态（按值 `self` 进、按值出），`complete` 收尾。
3. **`full()` 是短路开关**：Consumer 与 Folder 都有 `full()`，随时可以被框架查询「是否不想再吃了」（如 `find_any` 找到答案后返回 `true`，全池停工）。这正是 u2-l5 讲过的 `try_reduce` 全局短路的落点。

若用公式描述中点切分下叶子任务的规模：初始长度为 \( N \)，递归深度为 \( d \) 的叶子长度约为 \( N / 2^d \)；切分预算（4.4 节）决定 \( d \) 的上限。

#### 4.1.3 源码精读

先看 trait 定义本体：

- [src/iter/plumbing/mod.rs:111-146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L111-L146)：官方文档注释明确写着「consumer 本质上是一个广义 fold，最终会转换成 Folder；特殊之处在于它可以像 Producer 一样被 `split_at` 切分，切分时额外产出一个 Reducer」。trait 本体只有三个关联类型加三个方法：`split_at(index)` 返回 `(Self, Self, Self::Reducer)`；`into_folder()` 把自己转换成串行折叠器；`full()` 给框架的短路提示。

- [src/iter/plumbing/mod.rs:154-188](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L154-L188)：`Folder` trait。`consume(self, item) -> Self` 是标准的按值折叠签名；`consume_iter` 是**可覆写的批量吃元素默认实现**（循环里逐个 `consume` 并检查 `full()`），如果类型有更高效的一次性写法（比如 `for_each` 直接把闭包交给串行迭代器的 `for_each`）就应当覆写它；`complete(self) -> Self::Result` 收尾产出结果。

- [src/iter/plumbing/mod.rs:197-201](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L197-L201)：`Reducer` 只有一个方法 `reduce(self, left, right) -> Result`。它存在的唯一理由就是「合并被切开的两半」。

再看一个真实世界的复杂消费者作对照（`collect` 的内部实现，下一讲 u4-l4 的主角，这里只看骨架）：

- [src/iter/collect/consumer.rs:85-119](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L85-L119)：`CollectConsumer` 的三个关联类型是 `Folder = CollectResult`、`Reducer = CollectReducer`、`Result = CollectResult`。它的 [split_at（第 90-103 行）](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L90-L103) 做的事是：把「目标 `Vec` 里尚未初始化的内存段」按 `index` 切成前后两段指针，左右两个消费者各自认领一段；`into_folder` 则造出一个 `initialized_len = 0` 的 `CollectResult` 作为折叠器。

- [src/iter/collect/consumer.rs:121-150](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L121-L150)：`Folder` 这边，`consume` 把元素 `write` 进自己认领的内存段并递增 `initialized_len`——这就是「collect 能无锁写同一个 Vec」的全部秘密。`Reducer`（[第 165-185 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L165-L185)）只负责把相邻两段已初始化的记录拼成一条。

对照之下可以看出 `Consumer` 协议的表达力：`ForEachConsumer`（4.3 节）与 `CollectConsumer` 遵守同一份契约，前者丢弃元素，后者把元素写进内存——**框架（bridge 递归）完全不关心消费者内部做什么**。

#### 4.1.4 代码实践

**实践目标**：不动手写代码，先练「读协议」——用真实源码填写 Consumer 契约表。

**操作步骤**：

1. 打开 [src/iter/plumbing/mod.rs:123-146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L123-L146)，抄下 `Consumer` 的三个方法签名。
2. 打开 [src/iter/collect/consumer.rs:85-150](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs#L85-L150)，为 `CollectConsumer` 填写下面这张表：

| 契约成员 | CollectConsumer 中的值 | 一句话说明 |
| --- | --- | --- |
| `type Folder` | `CollectResult<'c, T>` | 记录「写到哪了」的内存段所有者 |
| `type Reducer` | `CollectReducer` | 拼接相邻两段 |
| `type Result` | `CollectResult<'c, T>` | 与 Folder 同型，`complete` 直接返回自身 |
| `split_at(index)` | 切指针段 | `start.0` 与 `start.0.add(index)` 各领一段 |
| `into_folder()` | 造 `initialized_len = 0` 的结果 | 「还没写任何元素」的初始折叠状态 |
| `full()` | 恒 `false` | collect 永不短路 |

3. 再对照 [src/iter/for_each.rs:19-38](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L19-L38) 给 `ForEachConsumer` 填同样一张表，注意它的 `type Folder = Self`（消费者自己就是折叠器）。

**需要观察的现象**：两个消费者的 `Result` 一个是内存段、一个是 `()`；`Reducer` 一个要真正合并、一个是空操作——协议形状相同，语义天差地别。

**预期结果**：你能不查源码说出「split_at 切的是什么」在两个消费者里分别是「内存段指针」与「什么都没切（无状态）」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Consumer::Result` 要求 `Send`，而 `Folder::Result` 没有这个约束？

**参考答案**：`Result` 会在递归返回时跨线程搬运——右半任务可能被窃取到别的线程执行，其结果必须送回发起 `join_context` 的线程参加 `reduce`，所以必须 `Send`。`Folder` 只在叶子任务的单线程内活动，`consume` 与 `complete` 都不跨线程，因此其 `Result`（通常等于 `Consumer::Result`，由 `Consumer` 那一侧的 `Send` 约束兜底）无需单独声明。

**练习 2**：`Folder::consume_iter` 有默认实现，什么情况下应该覆写它？

**参考答案**：当类型可以绕过「逐元素调用 `consume` + 每次检查 `full()`」的逐项循环、直接批量处理整个迭代器时。典型例子是 4.3 节的 `ForEachConsumer`：它覆写 `consume_iter` 为 `iter.into_iter().for_each(self.op)`，把元素直接灌给用户闭包，省掉逐元素包装的开销。

**练习 3**：`full()` 在 Consumer 和 Folder 上各出现一次，两者语义有何差别？

**参考答案**：语义相同（「不想再吃了」），询问时机不同。`Consumer::full` 在每次**切分递归入口**被 bridge 检查（决定是否连切都省掉，直接 `into_folder().complete()` 拿空结果）；`Folder::full` 在**串行吃元素的过程中**（默认 `consume_iter` 的每次 `consume` 之后）被检查，用于叶子任务内部提前停手。

### 4.2 drive 与 drive_unindexed：两条驱动路径

#### 4.2.1 概念说明

`Consumer` 有了，谁来「驱动」它？答案是迭代器自己身上的两个内部方法：

- **`drive`（indexed 路径）**：定义在 `IndexedParallelIterator` 上。要求消费者是完整形态的 `Consumer`，走 `bridge`——框架从 `len()` 拿到精确长度，取中点 `mid`，让生产者与消费者**在同一个 `mid` 上对齐切分**（u4-l1 讲过的拉推对齐）。
- **`drive_unindexed`（无索引路径）**：定义在 `ParallelIterator` 上。只要求消费者是 `UnindexedConsumer`，通常走 `bridge_unindexed`——没有长度，生产者凭自身能力「随便找个点」切，消费者用 `split_off_left` 无中生有地克隆出左半。

两者的能力不对称（承 u4-l1 的结论）：**任何生产者都能驱动无索引消费者；只有索引生产者才能驱动索引消费者。**

`UnindexedConsumer` 在 `Consumer` 之上追加两个方法，解决的是「没有下标时怎么分裂」：

- `split_off_left(&self) -> Self`：凭空分裂出一个「左」消费者，`self` 之后的角色是「右」。它只借用 `&self` 而不消耗——因为无索引世界里切分点由数据决定，消费者自己往往是无状态的，复制即可。
- `to_reducer(&self) -> Self::Reducer`：单独讨要一个 Reducer。为什么需要它？正因为 `split_off_left` 只拿 `&self`、不消耗 `self`，没法像 `Consumer::split_at` 那样「切分时顺手把 Reducer 一起返回」，所以 Reducer 必须另走一条门。

#### 4.2.2 核心流程

一次 `for_each` 调用中，驱动请求沿迭代器链条**从链尾（消费者注入点）向链头（数据源）传播**，最终在某一层落到 `bridge` / `bridge_unindexed`：

```text
用户代码:  v.par_iter().map(f).filter(p).for_each(op)
                                     │ 构造 ForEachConsumer
                                     ▼
            filter: drive_unindexed(包装后的 consumer)   ── filter 无索引，只能转发
                                     ▼
            map:    drive_unindexed(再包一层 MapConsumer)
                                     ▼
            数据源:  Iter::drive_unindexed(consumer)
                                     ▼
            bridge(self, consumer)   ── 切片有索引，仍然可以走 indexed 引擎！
                                     ▼
            bridge_producer_consumer(...)  ── 4.4 节的递归
```

注意最后一跳的微妙之处：`drive_unindexed` 是「无索引入口」，但切片这个数据源本身实现了 `IndexedParallelIterator`，所以它可以**用索引生产者驱动无索引消费者**（能力不对称允许的方向），照走 `bridge` 的精确中点切分。

#### 4.2.3 源码精读

**trait 声明一側**（都在 `src/iter/mod.rs`）：

- [src/iter/mod.rs:2410-2412](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2410-L2412)：`ParallelIterator::drive_unindexed` 的声明，约束 `C: UnindexedConsumer<Self::Item>`。它紧邻其后的伙伴方法是 [opt_len（第 2428-2430 行）](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2428-L2430)，默认返回 `None`——它告诉消费端「这个迭代器有没有长度可用」，`collect` 据此选择预分配还是分块收集（u2-l4）。

- [src/iter/mod.rs:3236](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3236)：`IndexedParallelIterator::drive` 的声明，约束 `C: Consumer<Self::Item>`（完整消费者）。同一 trait 里的 [len（第 3220 行）](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3220) 与 [with_producer（第 3253 行）](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3253) 共同构成索引三件套（u4-l2 已精读 `with_producer` 的回调机制）。

**无索引消费者契约**：

- [src/iter/plumbing/mod.rs:208-221](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L208-L221)：`UnindexedConsumer` 的文档点名 `for_each` 就是典型产出者（无状态消费者）。`split_off_left` 的注释强调**顺序对 `find_first` 这类方法很重要**——返回的「左」消费者产出的值优先于 `self`（右），u3-l4 讲过的虚区间协议正是靠这个约定维持串行序的。

**数据源一側**（切片，两条路径都到 `bridge`）：

- [src/slice/mod.rs:824-837](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L824-L837)：`Iter<'data, T>` 的 `ParallelIterator` 实现里，`drive_unindexed` 直接 `bridge(self, consumer)`——无索引消费者 + 索引迭代器，合法且最优；`opt_len` 返回 `Some(self.len())`。

- [src/slice/mod.rs:839-849](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L839-L849)：同一类型的 `IndexedParallelIterator` 实现里，`drive` 同样是 `bridge(self, consumer)`。对照可见：对切片而言两条驱动路径最终汇合到同一台引擎。

**适配器一側**（`Map`，层层包装转发）：

- [src/iter/map.rs:39-45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L39-L45)：`Map::drive_unindexed` 只做一件事——`MapConsumer::new(consumer, &self.map_op)` 把下游消费者包一层，然后转发给 `self.base`（上游）。数据流过时的变换发生在 `MapConsumer` 的 `Folder::consume` 里（u3-l1 已读）。

- [src/iter/map.rs:58-64](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L58-L64)：`drive` 的实现形状完全一样，只是约束换成 `Consumer`。这就是 u4-l1 说的「消费者自链尾向链头层层包装」在源码里的样子。

#### 4.2.4 代码实践

**实践目标**：用公开的 `opt_len` 方法亲手验证「索引能力在管道中的存亡」，直观区分两条路径的适用范围。

**操作步骤**：

1. 新建（或复用）一个依赖 `rayon` 的 Cargo 项目，加入如下 `main`（示例代码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let a = (0..100).into_par_iter().map(|x| x + 1);
       // map 保留索引能力
       println!("map 之后的 opt_len = {:?}", a.opt_len());

       let b = a.filter(|x| x % 2 == 0);
       // filter 丢失索引能力
       println!("filter 之后的 opt_len = {:?}", b.opt_len());
   }
   ```

2. 用 `cargo run` 运行（`opt_len` 只读不驱动，debug 构建即可）。
3. 尝试在 `b` 上调用 `b.len()`，观察编译错误。

**需要观察的现象**：第一行打印 `Some(100)`，第二行打印 `None`；`b.len()` 无法通过编译——`len` 是 `IndexedParallelIterator` 的方法，而 `filter` 之后迭代器已降级为普通 `ParallelIterator`。

**预期结果**：`Some(100)` 与 `None`。由此可推断：对 `a` 调 `collect` 时消费端能拿到长度走预分配路径（u2-l4），对 `b` 只能走分块收集；对 `a` 可用 `drive`（索引引擎），对 `b` 只能 `drive_unindexed`。

#### 4.2.5 小练习与答案

**练习 1**：`UnindexedConsumer::split_off_left` 为什么拿 `&self` 而 `Consumer::split_at` 拿 `self`？

**参考答案**：索引世界里切分要按 `index` 精确对齐，切完左右职责不同，用移动语义顺理成章，还能顺手把 `Reducer` 一起返回。无索引世界里消费者通常无状态（如 `ForEachConsumer` 只持有一个共享闭包引用），「分裂」就是复制一份，用 `&self` 表达「我不被消耗，右边还是我」；代价是 Reducer 无法随切分返回，只能另设 `to_reducer`。

**练习 2**：切片的 `drive_unindexed` 为什么可以直接调 `bridge` 而不是 `bridge_unindexed`？

**参考答案**：`bridge` 要求 `I: IndexedParallelIterator` 且 `C: Consumer`。切片 `Iter` 两者兼备：它实现了 `IndexedParallelIterator`，而 `UnindexedConsumer` 是 `Consumer` 的子 trait。「索引生产者驱动无索引消费者」是能力不对称中允许的方向，且中点切分对连续内存的切片是最优策略，因此复用同一台引擎。

**练习 3**：`Map` 的 `drive_unindexed` 里为什么是 `self.base.drive_unindexed(consumer1)`，而不是自己直接去 `bridge`？

**参考答案**：`Map` 只是包装器，它不拥有数据，也不知道上游是什么（可能是切片、可能是另一个适配器）。把消费者包好后转发给上游，是适配器唯一的正确动作；最终由链条尽头的**数据源**决定落到哪台引擎（`bridge` 或 `bridge_unindexed`）。这也解释了为什么 `delegate_iterator!` 宏（u3-l1）能为纯转发壳类型自动生成这五个方法。

### 4.3 for_each 实现：最简消费者的完整调用链

#### 4.3.1 概念说明

`for_each` 是理解 Consumer 的最佳标本，因为它把协议裁剪到了最小：

- 结果类型是 `()`——`Reducer` 无事可做，用空操作 `NoopReducer`；
- 永不短路——`full()` 恒 `false`；
- 对元素无累积——消费者无状态，`split_off_left` 就是再克隆一个引用。

于是 `ForEachConsumer` 一个结构体同时扮演三个角色：**Consumer、自己的 Folder、UnindexedConsumer**。读懂它，任何更复杂的消费者都只是在这个骨架上往 `split_at` / `consume` / `reduce` 里加逻辑。

#### 4.3.2 核心流程

`v.par_iter().for_each(op)` 的完整调用链（按执行顺序）：

```text
① ParallelIterator::for_each(op)          src/iter/mod.rs:376     默认方法，转调内部函数
② for_each::for_each(pi, &op)             src/iter/for_each.rs:5  构造 ForEachConsumer { op }
③ pi.drive_unindexed(consumer)            src/iter/for_each.rs:12 注入消费者，驱动开始
④ Map::drive_unindexed                    src/iter/map.rs:39      MapConsumer 包一层，转给上游
   ...（链条上每个适配器同样包装并转发）...
⑤ Iter::drive_unindexed                   src/slice/mod.rs:827    数据源收尾：bridge(self, consumer)
⑥ bridge                                  src/iter/plumbing/mod.rs:346
   → with_producer(Callback)              把自己转成 Producer 后回调（u4-l2 的回调机制）
⑦ bridge_producer_consumer               src/iter/plumbing/mod.rs:385
   → helper 递归：切分 / join_context / reduce（4.4 节展开）
⑧ 叶子任务：producer.fold_with(consumer.into_folder()).complete()
                                           生产者转串行迭代器喂给 Folder —— 拉转推的接合点
```

用户写的一行 `for_each`，真正「干活」的只有第 ⑧ 步里 `Folder::consume` 调用的 `(self.op)(item)`；其余全是为「把哪些元素分给哪个线程」而设的脚手架。

#### 4.3.3 源码精读

- [src/iter/mod.rs:376-381](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L376-L381)：`ParallelIterator::for_each` 是 trait 上的默认方法，函数体只有一行 `for_each::for_each(self, &op)`。注意闭包约束是 `Fn(Self::Item) + Sync + Send`——`Sync` 的原因马上在消费者结构体里显现。

- [src/iter/for_each.rs:5-13](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L5-L13)：内部函数。构造 `ForEachConsumer { op }` 后立即 `pi.drive_unindexed(consumer)`——**消费者的构造点与注入点**，整条驱动链的起点。

- [src/iter/for_each.rs:15-38](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L15-L38)：`ForEachConsumer` 结构体只有一个字段 `op: &'f F`——共享引用，这正是要求 `F: Sync` 的原因：消费者会被复制到多个线程，所有副本共用同一个闭包。`Consumer` 实现里：`type Folder = Self`（自己就是折叠器）、`type Reducer = NoopReducer`、`type Result = ()`；最妙的是 [split_at（第 27-29 行）](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L27-L29) 的写法——`(self.split_off_left(), self, NoopReducer)`：索引版的切分**借道**无索引版的 `split_off_left` 实现，反正切在哪都无所谓（元素独立、结果为 `()`）。

- [src/iter/for_each.rs:40-64](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L40-L64)：`Folder` 实现。`consume` 调 `(self.op)(item)` 后原样返回 `self`（无累积状态）；[consume_iter（第 51-57 行）](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L51-L57) 覆写为 `iter.into_iter().for_each(self.op)`，绕过逐元素包装——这正是 4.1 节练习 2 提到的批量优化实例；`complete` 直接 `()`。

- [src/iter/for_each.rs:66-77](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L66-L77)：`UnindexedConsumer` 实现。`split_off_left` 重新包一个 `ForEachConsumer { op: self.op }`——克隆引用即克隆消费者；`to_reducer` 返回 `NoopReducer`。

- [src/iter/noop.rs:55-59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/noop.rs#L55-L59)：`NoopReducer` 的全部实现——`reduce` 什么都不做。同文件上方（[第 3-53 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/noop.rs#L3-L53)）还有一个 `NoopConsumer`：吃掉所有元素但什么都不做，u3-l3 讲过 `skip` 用它消费「被跳过的段」以保留副作用与 panic 传播。

顺带一提：plumbing 模块本身是公开的（[src/iter/mod.rs:91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L91) 声明为 `pub mod plumbing`），`drive` 也是 `IndexedParallelIterator` 的公开方法——文档虽标注「内部方法，你不应直接调用」，但正因如此，我们才能在下面的实践中从外部实现并注入自定义消费者。

#### 4.3.4 代码实践

**实践目标**：手写一个 `LogConsumer`，只在 `into_folder`（fold 开始）与 `complete`（finish 结束）打印日志，接到 `par_iter().map(...).drive(...)` 上，观察叶子任务的调用顺序与线程分布。这是本讲的主实践。

**操作步骤**：

1. 新建一个 Cargo 项目，`Cargo.toml` 加 `rayon = "1.12"`。
2. 写入如下 `src/main.rs`（示例代码——这不是 rayon 仓库里的文件，是你自己的实验工程）：

   ```rust
   use rayon::iter::plumbing::{Consumer, Folder, Reducer};
   use rayon::prelude::*;

   /// 最简「计数消费者」：数吃了多少个元素，并在 fold/finish 时打日志。
   struct LogConsumer {
       depth: u32, // 切分深度，用来分辨不同叶子
       count: usize,
   }

   struct LogReducer;

   impl Consumer<i32> for LogConsumer {
       type Folder = Self;          // 消费者自己就是折叠器（学 ForEachConsumer）
       type Reducer = LogReducer;
       type Result = usize;

       fn split_at(self, index: usize) -> (Self, Self, LogReducer) {
           // 本实践不打日志，保持「只在 fold 与 finish 打印」；
           // 切分后两个新消费者都从头计数
           let _ = index;
           (
               LogConsumer { depth: self.depth + 1, count: 0 },
               LogConsumer { depth: self.depth + 1, count: 0 },
               LogReducer,
           )
       }

       fn into_folder(self) -> Self {
           println!(
               "[fold   开始] depth={} 线程={:?}",
               self.depth,
               rayon::current_thread_index()
           );
           self
       }

       fn full(&self) -> bool {
           false
       }
   }

   impl Folder<i32> for LogConsumer {
       type Result = usize;

       fn consume(mut self, _item: i32) -> Self {
           self.count += 1;
           self
       }

       fn complete(self) -> usize {
           println!(
               "[finish 结束] depth={} 线程={:?} 吃了 {} 个元素",
               self.depth,
               rayon::current_thread_index(),
               self.count
           );
           self.count
       }

       fn full(&self) -> bool {
           false
       }
   }

   impl Reducer<usize> for LogReducer {
       fn reduce(self, left: usize, right: usize) -> usize {
           left + right // 左右叶子的计数相加
       }
   }

   fn main() {
       // map 保持索引能力，所以可以调用 IndexedParallelIterator::drive
       let total = (0..1024)
           .into_par_iter()
           .map(|x| x * 2)
           .with_max_len(128) // 强制至少 1024/128 = 8 刀，便于观察
           .drive(LogConsumer { depth: 0, count: 0 });

       println!("total = {total}");
       assert_eq!(total, 1024);
   }
   ```

3. 用 `cargo run --release` 运行；再分别用 `RAYON_NUM_THREADS=1 cargo run --release` 与 `RAYON_NUM_THREADS=2 cargo run --release` 各跑一次。
4. 把三次输出的 `[fold 开始]` / `[finish 结束]` 行按线程归组统计。

**需要观察的现象**：

- 每个叶子任务恰好出现一对 `fold 开始` → `finish 结束`，且每对内 depth 相同、线程相同；`finish` 报告的元素数之和等于 1024。
- 默认线程数下，不同叶子对的 `线程=` 值不同（分布在多个工作线程上）；`RAYON_NUM_THREADS=1` 时所有叶子对都落在同一线程（编号通常是 `Some(0)`），输出严格一条线。
- 多线程时 `fold 开始` / `finish 结束` 的打印顺序在多次运行间可能不同——切分递归与窃取是非确定性的。

**预期结果**：`total = 1024`，断言通过；三组日志呈现上述线程分布差异。若你在机器上观察到的线程分布与此描述不一致，以待本地验证的实际输出为准（线程编号与调度取决于运行环境）。

**说明**：为什么接 `.drive(...)` 而不是 `.for_each(...)`？`for_each` 内部硬编码构造 `ForEachConsumer`，不接受外部消费者；`drive` 是公开的注入点，正好把我们的 `LogConsumer` 送进同一台引擎。你的 `LogConsumer` 走的递归、切分、`join_context` 路径与 `for_each` 完全一致。

#### 4.3.5 小练习与答案

**练习 1**：`ForEachConsumer` 的 `type Result = ()`，那并行度对结果还有意义吗？`Reducer` 是不是多余的？

**参考答案**：对**结果**而言确实无事可做，`NoopReducer` 就是这个「多余性」的诚实表达。但 Consumer 协议要求每个 `split_at` 都必须返回一个 Reducer（bridge 递归统一调用 `reducer.reduce(left, right)`），所以协议形状不能少；`NoopReducer` 把「这里没有合并语义」编码为类型，`skip` 等场景复用它避免重复造轮子。

**练习 2**：如果把 `for_each` 的闭包约束从 `Fn(T) + Sync` 放宽为只要求 `Send`，会发生什么？

**参考答案**：编译失败或协议无法成立。消费者会被 `split_off_left` 克隆成多份并发跑在多个线程上，所有副本共享同一个 `&F`——共享引用跨线程并发调用要求 `F: Sync`。只 `Send` 不 `Sync` 的闭包（如捕获 `&mut` 或 `Cell` 的）无法被多线程同时调用。这也是 u1-l1 讲的「数据竞争拦在编译期」的一个具体落点。

**练习 3**：动手改造：给 4.3.4 的 `LogConsumer::split_at` 与 `LogReducer::reduce` 也加上打印（打印切分点 `index` 与合并发生的线程），运行后日志条数之间应满足什么关系？

**参考答案**：设叶子任务数为 \( L \)，则 `split_at` 打印 \( L - 1 \) 次（二叉切分树的内节点数 = 叶子数 − 1），`reduce` 也打印 \( L - 1 \) 次（每个内节点恰好合并一次），`fold 开始` / `finish 结束` 各 \( L \) 次。以 4.3.4 的参数为例若切出 8 个叶子，则 split 与 reduce 各 7 次、fold/finish 各 8 次（自适应切分可能产生更多叶子，但两组等式恒成立）。

### 4.4 join_context 驱动：bridge 递归的发动机

#### 4.4.1 概念说明

前面反复出现的 `bridge_producer_consumer` 就是 Consumer 生命周期的**执行引擎**：它把「切分预算」`Splitter` / `LengthSplitter`（u3-l3、u4-l1 已介绍）与并行原语 `join_context` 组装成一台递归机：

1. 问消费者 `full()`——想停就立刻 `into_folder().complete()` 返回（短路出口）。
2. 问切分器 `try_split`——还要切就取中点 `mid`，**生产者与消费者在同一个 `mid` 上切**。
3. 用 `join_context` 并行执行左右两半的递归——工作窃取在这一步发生。
4. 用 `Reducer` 合并两半结果，向上返回。

`join_context` 是 rayon-core 的原语（单元五 u5-l1 才深入 internals，这里只看与驱动相关的行为）：它把第二个闭包打包成任务压入本地队列，然后**先执行第一个闭包**；如果别的线程空闲，就会把队列里的第二个任务偷走。闭包参数 `FnContext::migrated()` 能告诉你「我是否运行在与发起线程不同的线程上」——bridge 递归正是用它来在「被窃取」时重置切分预算（u3-l3 讲过的自适应策略）。

无索引版的 `bridge_unindexed` 结构相同，差异集中在两处：生产者用 `split()`（返回 `Option`，分不动时退化为串行）；消费者用 `to_reducer()` + `split_off_left()` 而非 `split_at(mid)`——4.2 节讲过的「Reducer 另走一条门」的原因在这里现出全貌。

#### 4.4.2 核心流程

`bridge_producer_consumer` 内部 `helper` 递归的伪代码：

```text
helper(len, migrated, splitter, producer, consumer):
    if consumer.full():                        # ① 短路出口
        return consumer.into_folder().complete()
    if splitter.try_split(len, migrated):      # ② 还要切
        mid = len / 2
        (p_l, p_r) = producer.split_at(mid)    #    生产者在 mid 切
        (c_l, c_r, reducer) = consumer.split_at(mid)  # 消费者对齐切
        (r_l, r_r) = join_context(             # ③ 并行两半（窃取点）
            |ctx| helper(mid,     ctx.migrated(), splitter, p_l, c_l),
            |ctx| helper(len-mid, ctx.migrated(), splitter, p_r, c_r),
        )
        return reducer.reduce(r_l, r_r)        # ④ 合并
    else:                                      # ⑤ 叶子任务
        return producer.fold_with(consumer.into_folder()).complete()
```

第 ⑤ 步是拉模式与推模式的**接合点**：生产者转成串行迭代器（拉），`Folder::consume` / 覆写的 `consume_iter` 把元素一个个推下去——「拉转推」。

#### 4.4.3 源码精读

- [src/iter/plumbing/mod.rs:346-371](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L346-L371)：`bridge` 入口。先用 `par_iter.len()` 记下长度，再经 `with_producer(Callback { len, consumer })` 把自己转换成生产者（u4-l2 读过的回调机制），回调里调 `bridge_producer_consumer(self.len, producer, self.consumer)`。文档注释明说：实现自己的并行迭代器时，`drive` / `drive_unindexed` 的函数体通常就是一句 `bridge(...)`。

- [src/iter/plumbing/mod.rs:385-435](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L385-L435)：`bridge_producer_consumer` 与它的 `helper` 递归，本讲最核心的一段。入口先用生产者的 `min_len` / `max_len` 与 `len` 造 `LengthSplitter`（第 390 行）；递归体里依次是：`consumer.full()` 短路（第 404-405 行）、`splitter.try_split` 裁决（第 406 行）、同一 `mid` 上 `producer.split_at` 与 `consumer.split_at`（第 407-409 行）、`join_context` 并行两半并把 `context.migrated()` 传下去（第 410-429 行）、`reducer.reduce`（第 430 行）；叶子分支（第 432 行）即 4.4.2 伪代码第 ⑤ 步。

- [src/iter/plumbing/mod.rs:438-476](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L438-L476)：`bridge_unindexed` 及其递归。对照索引版的两处差异都在 [第 460-469 行](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L460-L469)：`producer.split()` 返回 `(left, Some(right))` 或 `(producer, None)`（分不动就整段串行，第 471 行）；消费者这边 `(consumer.to_reducer(), consumer.split_off_left(), consumer)`——元组第三个位置直接放原 `consumer` 当右半，正是「split_off_left 借用后 self 继续当右半」的用法现场。

- [src/iter/plumbing/mod.rs:250-284](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L250-L284)：`Splitter` 的窃取自适应——`try_split(stolen)` 在 `stolen == true` 时把预算重置回线程数（第 270-274 行）。`stolen` 从哪来？就是 `join_context` 闭包里那句 `context.migrated()`。

- [rayon-core/src/join/mod.rs:115-121](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L115-L121)：`join_context` 的公开签名，文档说明 `migrated()` 为真表示「闭包运行在与调用者不同的线程上——第二个任务被偷走了，或调用本来就不在池内」。

- [rayon-core/src/join/mod.rs:132-149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/rayon-core/src/join/mod.rs#L132-L149)：窃取现场——第二个闭包被打包成 `StackJob`（栈上分配的任务，单元五细讲）压入本地队列（`worker_thread.push`），**然后**才执行第一个闭包，注释写着「希望这期间 b 被偷走」。plumbing 层的每一层递归在这里都会创建一对可能被窃取的任务。

#### 4.4.4 代码实践

**实践目标**：绕开迭代器，直接使用 `join_context`，亲眼看到「第二个闭包被窃取」这件事，建立 bridge 递归与调度行为之间的直觉。

**操作步骤**：

1. 在实验工程的 `src/main.rs` 里加入（示例代码）：

   ```rust
   use rayon::join_context;

   fn fib(n: u32) -> u32 {
       if n <= 1 {
           return n;
       }
       let (a, b) = join_context(
           |_| fib(n - 1), // 第一个闭包：本地执行
           |ctx| {
               if ctx.migrated() {
                   println!("fib({n}) 的第二个分支被窃取，线程={:?}",
                            rayon::current_thread_index());
               }
               fib(n - 2)
           },
       );
       a + b
   }
   ```

   并在 `main` 里调用 `println!("fib(35) = {}", fib(35));`（先做足够多的顺序工作或直接用较大的 n 预热线程池；线程池在首次使用时创建，最初几个任务来不及被窃取是正常现象）。
2. `cargo run --release` 运行若干次，统计「被窃取」打印出现的次数。
3. 对照 `RAYON_NUM_THREADS=1 cargo run --release` 再跑：窃取打印应当完全不出现（只有一个线程，无处可偷）。

**需要观察的现象**：默认线程数下，多数运行会打出若干条「被窃取」，且线程编号与主线程不同；单线程环境下一条都没有；窃取次数在多次运行间波动。

**预期结果**：如上。若默认环境下始终看不到窃取打印，可把 n 调大（如 40）增加任务量——具体阈值随机器而异，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：bridge 递归里 `consumer.split_at(mid)` 与 `producer.split_at(mid)` 的 `mid` 必须相同吗？为什么？

**参考答案**：必须相同。生产者按 `mid` 切的是「数据的哪些元素」，消费者按 `mid` 切的是「怎么处理这些元素」；一旦错位，左消费者会收到右生产器的元素（或反之），轻则结果错乱，重则像 `CollectConsumer` 那样写错内存段导致断言失败或未定义行为。bridge 是全仓库唯一保证两者对齐的地方。

**练习 2**：`bridge_unindexed` 的叶子分支（`producer.split()` 返回 `None`）与索引版叶子分支有何语义差别？

**参考答案**：索引版叶子意味着「切分预算用完或已到 min_len，剩余元素串行处理」；无索引版返回 `None` 意味着「生产者自己说分不动了」（如字符串切分必须落在 UTF-8 字符边界、树切到叶子节点），此时无论预算还剩多少都只能整段串行。一个是策略性停止，一个是能力性停止。

**练习 3**：如果 `helper` 递归的第一步不检查 `consumer.full()` 直接进入切分判断，哪些功能会坏？

**参考答案**：全局短路类功能失效。`find_any` / `try_reduce` / `panic_fuse` 依赖 `full()` 让「已经不需要再算的子树」在递归入口就被丢弃、立即返回占位结果（`into_folder().complete()` 借出单位元）。没有这个检查，即使答案已找到，剩余任务仍会完整执行所有切分与计算，短路就退化成了「白算完再扔」。

## 5. 综合实践

**任务：把 LogConsumer 升级成「调度观察仪」，量化切分与线程分布。**

在 4.3.4 的 LogConsumer 基础上完成三步：

1. **补全日志**：给 `split_at`（打印切分点 `index` 与当前线程）、`LogReducer::reduce`（打印合并发生的线程）也加上打印；给 `into_folder` / `complete` 的日志加上一个用 `AtomicUsize` 生成的全局叶子编号，这样日志行能按「叶子对」配对。
2. **矩阵实验**：对 `(0..1_000_000).into_par_iter().map(...)` 分别在 `with_max_len` 取 `{默认, 10_000, 1_000, 100}` 与 `RAYON_NUM_THREADS` 取 `{1, 2, 4}`（按机器核数酌情增减）的组合下运行，统计每次运行的：叶子任务数、`split_at` 次数、`reduce` 次数、不同线程上完成的叶子数分布，整理成表。
3. **验证三个等式**：`split_at 次数 = reduce 次数 = 叶子数 − 1`（前提：无 `full()` 短路、无 panic）；并回答：固定 `with_max_len(100)`、把线程数从 1 提到 4，叶子数变化大还是线程分布变化大？结合 4.4 的 `LengthSplitter::new`（`min_splits = len / max`，再与线程数取较大者）解释原因。

**预期结果**：表格呈现「`with_max_len` 越小叶子越多、线程越多分布越散」的趋势；三组等式在每个组合下都成立（自适应切分会让叶子数 ≥ 预算的 `next_power_of_two`，但等式不受影响）；第 3 问的答案是叶子数主要由 `min_splits = len / max_len` 决定、线程数只在与它取 `max` 时才抬高分子数——这正解释了 u3-l3 的结论「粒度控制影响性能、不影响结果」。若某组数据与预期不符，先检查是否混入了 `--release` 缺失导致的 debug 开销干扰，再以待本地验证的实测为准。

## 6. 本讲小结

- `Consumer` 是「可分裂的广义 fold」：三个关联类型 `Folder`（串行吃元素）、`Reducer`（合并两半）、`Result`（必须 `Send`）构成契约；生命周期为 `split_at → into_folder → consume/consume_iter → complete → reduce`。
- 驱动有两条路径：`drive`（indexed，`bridge` 精确中点切分）与 `drive_unindexed`（`bridge_unindexed` 按能力切分）；任何生产者都能驱动无索引消费者，反之不行。`UnindexedConsumer` 用 `split_off_left(&self)` 无中生有地克隆左半、用 `to_reducer` 单独讨要 Reducer。
- 驱动请求沿适配器链**从链尾向链头传播**：适配器只负责包装消费者并转发（`Map::drive_unindexed` 两行），最终由数据源落到 `bridge`；切片即使从无索引入口进来也复用索引引擎。
- `for_each` 是最简消费者标本：`ForEachConsumer` 一个结构体三用（Consumer / 自身 Folder / UnindexedConsumer），`split_at` 借道 `split_off_left`，Reducer 是空操作 `NoopReducer`。
- `bridge_producer_consumer` 的递归是执行引擎：`full` 短路 → `Splitter` 裁决 → 生产者与消费者在同一 `mid` 对齐切分 → `join_context` 并行两半（窃取点，`migrated()` 反馈给 Splitter 重置预算）→ `reduce` 合并；叶子处 `fold_with(into_folder())` 完成拉转推。
- plumbing 模块与 `drive` 都是公开 API，因此可以从外部实现并注入自定义 Consumer——本讲的 LogConsumer 与 rayon 内置消费者走的是完全相同的机器。

## 7. 下一步学习建议

- **下一讲 u4-l4（collect 的内部实现）**：本讲只瞥了 `CollectConsumer` 的骨架，下一讲完整拆解空间预分配、段写入协议与 `CollectReducer` 的拼接规则，并回答「collect 为何无锁」。
- **横向对照**：带着本讲的契约表去读 `src/iter/reduce.rs` 与 `src/iter/sum.rs`，看「有真实语义的 Reducer」如何写；再读 `src/iter/find/mod.rs` 里 `full() == true` 的消费者，把 4.4.5 练习 3 的短路机制看实。
- **向内核进发（单元五）**：4.4 节停在 `join_context` 门口。u5-l1 从 rayon-core 的 `join` 开始，读 `StackJob`、`SpinLatch` 与 `Registry`，把「任务入队 → 先做本地 → 空闲线程窃取」这条链在内核源码里走通。
- **动手预告**：u9-l1 将要求从零实现自定义 `ParallelIterator`，本讲的 LogConsumer 是其 Consumer 一侧的直接预习材料。
