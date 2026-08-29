# 实现自定义 ParallelIterator

## 1. 本讲目标

本讲是单元九的第一讲，也是整个学习路线的「毕业设计」入口：不再阅读别人写好的适配器，而是**从零实现一个自定义数据源类型的完整 `IndexedParallelIterator`**。

学完后你应该能够：

1. 独立为一个自有类型实现 `ParallelIterator` + `IndexedParallelIterator` 全套方法（`Item`、`drive_unindexed`、`opt_len`、`len`、`drive`、`with_producer`）。
2. 正确实现 `Producer` 的 `split_at` 与 `into_iter`，理解契约中「按值切分」「index ≤ N」「长度由框架记账」三条规则。
3. 手写一个最小 `Consumer`（`split_at` / `into_folder` / `full` 三件套，配 `Folder` 与 `Reducer`），并通过 `drive` 直接驱动它。
4. 为自定义迭代器编写与标准库对照的正确性测试。

本讲的实现标的 `RepeatN<T>`（重复元素生成器）并非凭空虚构——rayon 自己就内置了这个类型（`rayon::iter::repeat_n`），我们会**先精读官方实现，再在独立工程里不看答案重写一遍**，最后用 `std::iter::repeat().take(n)` 做结果对照。这是检验你是否真正理解 plumbing 的最可靠方式。

## 2. 前置知识

本讲直接建立在 u4-l2（Producer 契约）与 u4-l3（Consumer 与驱动流程）之上，先快速复习三条已建立的认知，然后说明本讲的视角转换。

**复习一：plumbing 三角色。** `Producer` 是拉模式一侧的「可切分 IntoIterator」，核心是 `split_at(index)`；`Consumer` 是推模式一侧的「可分裂广义 fold」，配套 `Folder`（叶子任务里串行吃元素）和 `Reducer`（合并两半结果）；`bridge` 是把两者接通的递归引擎。忘记细节时可回看 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md)。

**复习二：驱动两条路。** 索引路径 `drive → bridge → with_producer`（生产者与消费者在精确中点对齐切分）；无索引路径 `drive_unindexed → bridge_unindexed`（凭数据自身能力任意对半分）。任何生产者都能驱动无索引消费者，反之不行。

**复习三：适配器 vs 数据源。** u3-l1 读过的 `map`/`filter` 是适配器——它们自己不生产数据，只包装上游迭代器并把下游消费者再包一层转发上去。本讲要写的是**数据源**（base iterator）：链条的最前端，必须亲手交出一个 `Producer`。适配器是「包装消费者」，数据源是「交出生产者」，两者合起来才是完整链路。

本讲视角转换的口诀：**适配器实现的是「消费者侧的转发」，数据源实现的是「生产者侧的供给」**。zip 这类需要协调多路输入的适配器也会切到生产者侧（u4-l1 讲过的 push→pull 切换），但它最终仍要向链头的数据源要生产者——所以 `with_producer` 是迭代器世界与生产者世界的唯一交换口。

需要的 Rust 基础：关联类型（`type Item`）、带泛型约束的 trait 实现、所有权转移与 `clone` 的成本意识。无需unsafe。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `src/iter/plumbing/mod.rs` | plumbing 全部 trait 的定义处：`Producer`、`Consumer`、`Folder`、`Reducer`、`ProducerCallback`，以及 `bridge` / `bridge_producer_consumer` 引擎。本讲反复查询的「契约文档」 |
| `src/iter/plumbing/README.md` | 官方设计文档：拉/推两模式、`ProducerCallback` 为何存在。本讲主要引用其模式划分 |
| `src/iter/repeat.rs` | **本讲的范本**：`RepeatN` 的迭代器结构体、`RepeatNProducer` 的生产者实现，以及无限版 `Repeat` 的无索引生产者作对照 |
| `src/iter/mod.rs` | 两大根 trait 的正式定义（`drive_unindexed` / `opt_len` / `len` / `drive` / `with_producer` 的签名与文档） |
| `src/iter/for_each.rs` | 最简真实消费者 `ForEachConsumer`，作为手写 Consumer 的参照 |
| `src/iter/test.rs` | 测试技法库：`is_indexed` 静态断言、直接调用 `with_producer` 的观察测试、`count_repeat_n_clones` 的 clone 计数测试 |

## 4. 核心概念与源码讲解

### 4.1 迭代器结构体：从自有类型到 IndexedParallelIterator

#### 4.1.1 概念说明

要给一个类型赋予并行迭代能力，第一步不是写 `Producer`，而是为它实现两个 trait：`ParallelIterator`（必选）与 `IndexedParallelIterator`（可选但强烈推荐）。

这层的职责是**回答三个问题**：

1. 产出什么（`type Item`，必须 `Send`——元素要能跨线程搬运）；
2. 有多长（`len` 与 `opt_len`——能否走索引路径的分水岭）；
3. 怎么被驱动（`drive` / `drive_unindexed`——两者都只是把自身交给 `bridge`）。

注意一个容易混淆的点：`RepeatN` 这个**结构体本身既是数据源又是迭代器**。它不像标准库那样区分集合与迭代器两个类型，rayon 的惯例是「数据源类型直接实现迭代器 trait」，再靠一个 blanket 实现让所有并行迭代器自动获得 `into_par_iter()`。

#### 4.1.2 核心流程

实现「五件套 + 一个关联类型」的清单如下：

| 成员 | 所属 trait | 职责 | 有无默认 |
|---|---|---|---|
| `type Item` | `ParallelIterator` | 元素类型，约束 `Send` | 必填 |
| `drive_unindexed` | `ParallelIterator` | 无索引驱动入口 | 必填 |
| `opt_len` | `ParallelIterator` | 「长度已知」提示，喂给 collect 快速路径 | 默认 `None` |
| `len` | `IndexedParallelIterator` | 精确长度 | 必填 |
| `drive` | `IndexedParallelIterator` | 索引驱动入口 | 必填 |
| `with_producer` | `IndexedParallelIterator` | 交出 `Producer` 的唯一出口 | 必填 |

对一个已经实现索引能力的类型，消费端到生产端的完整调用链（承接 u4-l3 的时序）：

```text
sum() / collect() 等消费者方法
  └→ IndexedParallelIterator::drive(self, consumer)
       └→ bridge(self, consumer)                    // plumbing 引擎入口
            ├→ self.len()                           // 框架侧记账总长度
            └→ self.with_producer(Callback)          // 换出生产者
                 └→ callback.callback(RepeatNProducer)
                      └→ bridge_producer_consumer(len, producer, consumer)
                           ├→ 递归：split_at(mid) + join_context   // 工作窃取在此发生
                           ├→ 叶子：producer.into_iter() 喂给 Folder
                           └→ Reducer::reduce 逐层合并
```

关键结论：`drive` / `drive_unindexed` 的实现几乎总是一行 `bridge(self, consumer)`；真正需要动脑的只有 `with_producer`（交什么生产者）和 `len`（多长）。

#### 4.1.3 源码精读

先看范本 `RepeatN` 的两个 trait 实现。

第一层：`ParallelIterator` 的实现。[src/iter/repeat.rs:L155-L171](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L155-L171) 中，`drive_unindexed` 直接调用 `bridge(self, consumer)`——因为 `RepeatN` 有精确长度，无索引消费者也完全可以被索引生产者驱动（「任何生产者都能驱动无索引消费者」）；`opt_len` 覆写为 `Some(self.len())`，让 `collect` 走 u4-l4 讲过的「已知长度精确预分配」快速路径。

第二层：`IndexedParallelIterator` 的实现。[src/iter/repeat.rs:L173-L197](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L173-L197) 中三个方法各司其职：`drive` 同样转发给 `bridge`；`with_producer` 把内部字段解开、组装成 `RepeatNProducer` 后交给回调（`callback.callback(self.inner)`，注意这里是按值移动，零克隆）；`len` 从内部枚举读出计数。这里 `with_producer` 的写法是「结构体直接把生产者字段搬出来」的最简形态——生产者类型甚至就是这个迭代器的私有字段类型。

再对照根 trait 的正式签名。无索引侧入口与长度提示定义于 [src/iter/mod.rs:L2410-L2430](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2410-L2430)：`drive_unindexed` 无默认实现，`opt_len` 默认返回 `None`（文档同时警告：返回 `Some` 就必须只走索引式 `Consumer` 协议）。索引侧三件套 `len` / `drive` / `with_producer` 定义于 [src/iter/mod.rs:L3220-L3253](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3220-L3253)，全部必填、全部无默认——这就是实现一个索引数据源的最低工作量边界。

最后是 `into_par_iter()` 从哪里来。[src/iter/mod.rs:L2433-L2440](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2433-L2440) 是 blanket 实现：任何 `T: ParallelIterator` 自动实现 `IntoParallelIterator`。所以只要 `RepeatN` 实现了迭代器 trait，`repeat_n(7, 5).into_par_iter()` 就自动可用——我们**不需要**手写 `IntoParallelIterator`。这也回扣了 u2-l2 的规则：`par_iter()` 是否可用只看 `&Self: IntoParallelIterator` 是否成立，若想支持 `&repeat_n(...)` 的共享借用迭代，才需要额外写一个引用包装类型。

还有一个测试层面的实用细节：`RepeatN` / `repeat_n` 等符号经 [src/iter/mod.rs:L194](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L194) 从私有模块 `repeat` 公开导出，plumbing 模块则经 [src/iter/mod.rs:L91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L91) 以 `pub mod plumbing` 暴露——**用户代码可以 `use rayon::iter::plumbing::*;` 拿到全部契约 trait 与 `bridge` 函数**，这是本讲所有实践工程的理论基础。

#### 4.1.4 代码实践：用 with_producer 直接观察生产者

`with_producer` 是公开方法，可以在测试里直接调用，亲手摸到平时被 `bridge` 藏起来的生产者。仓库自身的 [src/iter/test.rs:L226-L250](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L226-L250)（`check_indices_after_enumerate_split`）就是这么做的：实现一个 `ProducerCallback`，在回调里对生产者调 `split_at(512)`，再分别迭代左右两半验证下标连续。我们照抄这个技法观察 `repeat_n`。

1. **实践目标**：绕过 `bridge`，直接拿到 `repeat_n(7, 8)` 的生产者并手动切一刀，确认切分语义。
2. **操作步骤**：在任意一个依赖 rayon 的工程里写入以下代码并运行（示例代码，非仓库原有）：

```rust
use rayon::iter::plumbing::{Producer, ProducerCallback};

struct Inspect;

impl ProducerCallback<i32> for Inspect {
    type Output = ();
    fn callback<P>(self, producer: P)
    where
        P: Producer<Item = i32>,
    {
        let (left, right) = producer.split_at(3);
        println!("左半: {:?}", left.into_iter().collect::<Vec<_>>());
        println!("右半: {:?}", right.into_iter().collect::<Vec<_>>());
    }
}

fn main() {
    rayon::iter::repeat_n(7, 8).with_producer(Inspect);
}
```

3. **需要观察的现象**：程序不经过任何线程池、不创建任何任务，直接打印两段内容。
4. **预期结果**：`左半: [7, 7, 7]`，`右半: [7, 7, 7, 7, 7]`。切分点 3 恰好把 8 个元素分成 3 + 5（左闭右开区间 `0..3` 与 `3..8`）。这段行为可由 4.2.3 的 `split_at` 源码直接推得，属确定性输出。

#### 4.1.5 小练习与答案

**练习 1**：如果只实现 `ParallelIterator` 而不实现 `IndexedParallelIterator`，哪些常用方法会失去？

答案：`enumerate`、`zip`、`rev`、`step_by`、`with_min_len` / `with_max_len`、`collect_into_vec` / `unzip_into_vecs` 这些索引专属方法全部不可用（编译期报「method not found」）；`collect` 仍可用但只能走无长度慢路径（各任务分块收集后按序拼接）。这正是 u2-l1 讲过的「索引能力在类型层面传播」——数据源不给，全链皆无。

**练习 2**：`RepeatN::drive_unindexed` 里调用 `bridge(self, consumer)`，为什么不会无限递归？

答案：递归只可能发生在 `drive_unindexed → bridge → ?` 又回到 `drive_unindexed` 的环上。但 `bridge` 的实现（见 4.2.3 引用的 [src/iter/plumbing/mod.rs:L346-L371](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L346-L371)）内部走的是 `self.len()` 与 `self.with_producer(...)`，直接换出生产者进入 `bridge_producer_consumer`，从不回调 `drive_unindexed`。调用链是「消费者世界 → 生产者世界」的单向门。

**练习 3**：删掉 `opt_len` 的覆写（保持默认 `None`），程序行为会变化吗？

答案：结果完全不变，只有性能路径变化。`opt_len` 返回 `None` 时，`collect` 到 `Vec` 走 u4-l4 讲过的无长度路径（`LinkedList` 拼接），而不再是精确预分配直写。它只是「伪特化」开关，不是正确性机制。

### 4.2 Producer 实现：split_at 与 into_iter

#### 4.2.1 概念说明

`Producer` 是「可切分的 `IntoIterator`」：切到底后 `into_iter` 变成普通串行迭代器；切分靠 `split_at(index)` 按值把自身掰成「`0..index`」与「`index..N`」两个独立生产者。数据源的全部并行能力都浓缩在这两个方法里。

对 `RepeatN` 这类「虚拟数据源」（元素按需克隆、不占实际内存）来说，`split_at` 是纯算术：把计数 `n` 分成 `index` 与 `n - index` 两半，唯一成本是左侧需要一个新元素副本（右侧复用被移动的原元素）。对比 u8-l1 的切片生产器（指针运算、零分配零克隆）与 u3-l6 的按值切分（`UnindexedProducer::split`），这里是第三种形态：**克隆计数切分**。

#### 4.2.2 核心流程

`Producer` 契约的三个要点（[src/iter/plumbing/mod.rs:L56-L97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L56-L97)）：

1. **`IntoIter` 必须是 `Iterator + DoubleEndedIterator + ExactSizeIterator`**（[L62](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L62)）。原因：叶子任务可能从任一端消费（`rev` 适配器需要 `next_back`），框架要能信任长度。这就是为什么 `std::iter::RepeatN<T>` 是理想选择，而 `Take<Repeat<T>>` 不行（无限迭代器 `Repeat` 不是 `ExactSizeIterator`）。
2. **长度固定但不可自查询**（trait 文档注释 [L44-L46](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L44-L46)）：`Producer` 的 API 里没有 `len()` 方法，总长由框架从 `IndexedParallelIterator::len()` 记账，切分点 `mid` 由框架计算后传入。你的 `split_at` 只需相信 `index ≤ N` 并用 `assert!` 兜底。
3. **粒度窗口有默认值**：`min_len` 默认 1、`max_len` 默认 `usize::MAX`（[L78-L93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L78-L93)），`with_min_len` / `with_max_len` 的效果由 `MinLen` / `MaxLen` 适配器通过「包装生产器」注入（u3-l3），数据源通常无需覆写。

谁在调用你的 `split_at`：`bridge_producer_consumer` 的递归骨架（详见 4.3.3 的完整引用）在每一层取 `mid = len / 2`，然后让生产者与消费者**在同一个 mid 上对齐切分**，再交给 `join_context` 并行执行两半。你的 `split_at` 被调用的次数大致等于「切分预算内被实际执行的任务分裂数」。

克隆成本的一个漂亮不变量：设最终把 \( n \) 个元素切成 \( f \) 个全消费的片段。每次内部切分裂一次 `element.clone()`（左半新副本、右半复用原值），共 \( f - 1 \) 次；而 `std::iter::RepeatN` 在产出最后一个元素时是把内部元素**移出**而非克隆（每个片段省一次克隆），故片段内克隆共 \( n - f \) 次。合计：

\[ (n - f) + (f - 1) = n - 1 \]

即**全量消费恰好 \( n - 1 \) 次克隆，与切分了多少刀无关**。仓库测试 `count_repeat_n_clones` 正是断言了这一点（见 4.2.3）。

#### 4.2.3 源码精读

**范本生产者**：[src/iter/repeat.rs:L200-L241](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L200-L241)。`RepeatNProducer` 是个两态枚举：`Repeats(T, NonZeroUsize)` 或 `Empty`——空情形不持有元素（构造 `repeat_n(x, 0)` 时 `x` 立即被丢弃）。`Producer` 实现中，`into_iter`（[L210-L219](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L210-L219)）把非空态转成 `Either::Left(iter::repeat_n(element, count))`、空态转成 `Either::Right(iter::empty())`——注释说明了为什么空态必须绕道：`std::iter::RepeatN` 没有值在手就无法表达「空」，而 `Empty` 变体恰恰不带值。`Either` 是 rayon 再导出的 `either` crate 枚举（[src/iter/mod.rs:L82](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L82)），两个分支都满足迭代器约束，故组合类型自动满足。

`split_at`（[L221-L240](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L221-L240)）是本模块的核心：先 `assert!(index <= count.get())` 兜底契约，再用 `NonZeroUsize::new` 同时检查左右两半是否非零——**只有两半都非零才付一次 `element.clone()`**；`index == 0` 时返回 `(Empty, 原)`、`index == count` 时返回 `(原, Empty)`，均零克隆。`NonZeroUsize` 编码在这里是刚需而非炫技：它静态保证 `Repeats` 分支的计数恒 ≥ 1，四种 `(Some, Some)` / `(Some, None)` / `(None, Some)` / `(None, None)` 组合恰好穷举切分的全部形态（最后一组 `unreachable!`，因为 count 非零时 index 与 count − index 不可能同时为零）。

**调用方**：引擎如何在mid上对齐切两侧。[src/iter/plumbing/mod.rs:L393-L434](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L393-L434) 的 `helper` 递归：第 407 行 `let mid = len / 2`，第 408–409 行紧接着 `producer.split_at(mid)` 与 `consumer.split_at(mid)`——**同一个 mid、两次调用**，这就是「对齐切分」的全部含义；第 410 行 `join_context` 并行两半（工作窃取在此发生）；叶子分支（第 432 行）`producer.fold_with(consumer.into_folder()).complete()` 完成拉转推。而 `bridge` 本体（[L346-L371](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L346-L371)）只做两件事：记下 `par_iter.len()`，再经 `with_producer` 换出生产者。`fold_with` 的默认实现（[L103-L108](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L103-L108)）就是 `folder.consume_iter(self.into_iter())`——你的 `IntoIter` 在这里被消费。

**对照：无限版 `Repeat` 走无索引一侧**。[src/iter/repeat.rs:L80-L104](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L80-L104) 的 `RepeatProducer` 实现 `UnindexedProducer`：`split` 无限二分（每次克隆一份元素），`fold_with` 直接 `folder.consume_iter(iter::repeat(self.element))`——没有长度、没有切分点，靠 `full()` 短路或外层 `take` / `zip` 截停。同一个文件里「有长度」与「无长度」两种生产者并排摆放，是理解两者差异的最佳对照标本。

**克隆计数的实证测试**：[src/iter/test.rs:L2187-L2249](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L2187-L2249) 的 `count_repeat_n_clones` 用自定义 `Counter`（`Clone` 与 `Drop` 各自增计数）精确断言：`repeat_n(Counter, 100).count()` 之后恰好 99 次克隆、100 次丢弃——与上面的 \( n - 1 \) 不变量严丝合缝，且**不依赖线程数**（测试机器切分几刀都一样）。测试后半段还验证了 `split_at` 的零克隆边界优化：`repeat_n(Counter, 100).step_by(99)` 只发生 2 次克隆（一次切分、一次产出首个元素，注释 L2234-L2235 解释了尾部省略）。

#### 4.2.4 代码实践：三刀四段验证 split_at 契约

模仿 u4-l2 介绍过的 `tests/producer_split_at.rs`「三刀四段」思想，在客户端代码里验证 `repeat_n` 生产者的切分正确性：任意切几刀后**按序拼接必须等于原序列**。

1. **实践目标**：亲手验证 `split_at` 的「左闭右开、拼接还原」契约。
2. **操作步骤**（示例代码）：

```rust
use rayon::iter::plumbing::{Producer, ProducerCallback};

struct SplitCheck;

impl ProducerCallback<i32> for SplitCheck {
    type Output = ();
    fn callback<P>(self, producer: P)
    where
        P: Producer<Item = i32>,
    {
        let (a, b) = producer.split_at(4);    // a: 0..4, b: 4..8
        let (b1, b2) = b.split_at(2);         // b1: 4..6, b2: 6..8（相对于 b）
        let (a1, a2) = a.split_at(1);         // a1: 0..1, a2: 1..4（相对于 a）
        let pieces = [a1, a2, b1, b2];
        let total: Vec<i32> = pieces
            .iter()
            .flat_map(|p| p.clone().into_iter().collect::<Vec<_>>())
            .collect();
        assert_eq!(total, vec![7; 8]);
        println!("四段长度: {:?}", pieces.iter().map(|p| p.clone().into_iter().count()).collect::<Vec<_>>());
    }
}

fn main() {
    rayon::iter::repeat_n(7, 8).with_producer(SplitCheck);
}
```

3. **需要观察的现象**：断言通过；四段长度打印出来。
4. **预期结果**：四段长度为 `[1, 3, 2, 2]`（对应区间 0..1、1..4、4..6、6..8），拼接后恰为 8 个 7。子生产器的 `split_at` 下标是**相对自己的**，这正是「两个子生产者可独立继续分裂」契约的体现。确定性输出，可本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`split_at` 里的 `assert!(index <= count)` 什么时候会触发？谁来保证不触发？

答案：正常执行中永不触发。切分点由引擎的 `helper` 用 `mid = len / 2` 计算且 `len` 来自 `IndexedParallelIterator::len()` 的记账，恒满足 `0 ≤ mid ≤ len`。`assert` 是防止其它库实现错误地直接调用 `split_at` 时的防线（debug 与 release 均生效，因为显式 `assert!` 不受 `debug_assertions` 控制）。

**练习 2**：为什么 `split_at` 里左半用 `element.clone()`、右半用移动的 `element`，而不是两次 `clone`？

答案：`split_at` 按值消费 `self`，原元素必须有个去处——把它移给右半是最自然的选择，于是只有左半需要新副本。每次分裂恰好一次克隆，这是 \( n - 1 \) 不变量的前提之一；若两次 `clone`，总次数会变为 \( 2(f-1) + (n-f) = n + f - 2 \)，随切分数增长。

**练习 3**：`RepeatNProducer` 的 `Empty` 变体带来了哪两个具体收益？

答案：其一，边界切分零克隆：`index == 0` 或 `index == count` 时不再需要为空的一半克隆元素（见 4.2.3 对 `split_at` 四种组合的分析）；其二，`repeat_n(x, 0)` 在构造时就丢弃 `x`（`repeat_n` 函数 [src/iter/repeat.rs:L125-L131](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L125-L131) 在 `n == 0` 时直接构造 `Empty`），空迭代器克隆也不复制元素。`count_repeat_n_clones` 测试的前几行（L2215–L2224）专门验证了这两点。

### 4.3 Consumer 实现：split_at / into_folder / full 三件套

#### 4.3.1 概念说明

先澄清一个边界：**纯数据源不需要写 Consumer**。`RepeatN` 只交生产者；消费者由 `sum`、`collect`、`for_each` 这些操作各自携带。那为什么本讲要手写一个？两个理由：

1. 学习目标是「全链路」——只有亲手写过 `Consumer` 的 `split_at` / `into_folder` / `full` 与配套的 `Folder::consume` / `complete`、`Reducer::reduce`，才能真正理解 u4-l3 讲过的生命周期时序。
2. 实用价值：`drive(consumer)` 是公开 API，自定义消费者是「不走适配器语法、直接以推模式扩展 rayon」的正规出口（`bridge` 的文档明确说它常被用作 `drive` / `drive_unindexed` 的定义，见 [src/iter/plumbing/mod.rs:L335-L345](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L335-L345)）。

`Consumer` 的本质是「可分裂的广义 fold」：分裂时产出两个子消费者加一个 `Reducer`；每个子消费者经 `into_folder` 转成串行折叠器 `Folder`；`Folder` 逐元素 `consume`，结束时 `complete` 出结果；最后 `Reducer` 把左右结果合并。`full()` 是短路钩子，供 `find_any` / `try_reduce` / `panic_fuse` 提前喊停（u2-l5、u3-l4）。

#### 4.3.2 核心流程

一个 `Consumer` 在一次完整执行中的生命周期时序：

```text
引擎 helper(len, producer, consumer)
  ├→ consumer.full()？ —— 是：直接 into_folder().complete() 返回（短路）
  ├→ 还要切分：
  │    (left_c, right_c, reducer) = consumer.split_at(mid)
  │    join_context( helper(左半), helper(右半) )      // 两半各自递归
  │    reducer.reduce(左结果, 右结果)                    // 逐层向上合并
  └→ 不再切分（叶子）：
       folder = consumer.into_folder()
       folder = folder.consume(x1).consume(x2)...        // 或 consume_iter 批量
       result = folder.complete()
```

写一个最小有状态消费者的检查清单：

| 组件 | 必须实现 | 关键决策 |
|---|---|---|
| `Consumer` | `split_at` / `into_folder` / `full` | 分裂时状态如何初始化（多数无状态可直接复制） |
| `Folder` | `consume` / `complete` / `full` | 累积状态放哪、`complete` 如何收尾 |
| `Reducer` | `reduce` | 左右结果如何合并（必须与串行语义一致） |
| `UnindexedConsumer`（可选） | `split_off_left` / `to_reducer` | 想被 `drive_unindexed` 驱动时才需要 |

无索引一侧的分裂方式（[src/iter/plumbing/mod.rs:L208-L221](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L208-L221)）不带下标：`split_off_left` 从 `&self` 克隆出左半，`self` 继续当右半——因此无索引消费者必须「可随意复制且不依赖位置」；这也解释了为什么 `collect`（位置敏感）不能走这条路（plumbing README L44-L51 的论述）。

#### 4.3.3 源码精读

**契约定义**。`Consumer` trait 全文见 [src/iter/plumbing/mod.rs:L123-L146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L123-L146)：三个关联类型（`Folder` / `Reducer` / `Result`，其中 `Result: Send`——结果要能跨线程送到合并点），三个方法（`split_at(index)` 返回左右消费者加归并器、`into_folder` 转串行折叠、`full` 短路提示）。`Folder` 见 [L154-L188](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L154-L188)：`consume` 按值吃self返回新状态（函数式风格，状态转移即返回值）、可选的 `consume_iter` 批量优化（默认逐个 `consume` 并周期性查 `full`，可特化为直接 `iter.for_each`）、`complete` 收尾。`Reducer` 只有一个方法 `reduce(left, right)`，[L197-L201](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L197-L201)。

**仓库里最简的真实消费者**：`ForEachConsumer`，[src/iter/for_each.rs:L5-L38](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L5-L38)。入口函数 `for_each`（L5-L13）构造消费者后调 `pi.drive_unindexed(consumer)`——这就是 u4-l3 追踪过的完整调用链起点。它的 `Consumer` 实现全是「模板答案」：`type Result = ()`、`split_at` 直接 `(self.split_off_left(), self, NoopReducer)`、`into_folder` 返回自身、`full` 恒 `false`。对应的 `Folder` 实现（[L40-L64](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L40-L64)）里 `consume` 就是执行用户闭包（L46-L49），并特化了 `consume_iter` 用标准库 `for_each` 灌入（L51-L57）；`complete` 返回单元值（L59）。最后的 `UnindexedConsumer` 实现（[L66-L77](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs#L66-L77)）里 `split_off_left` 只是复制 `op` 引用。注意它持的是 `&'f F` 共享引用——多个分裂副本共享同一个闭包，这正是「以共享引用供多任务调用的闭包须 `Sync`」约束（u3-l1）的来源。

**引擎视角的完整闭环**：把 [src/iter/plumbing/mod.rs:L404-L433](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L404-L433) 与本模块的时序图逐行对照——`consumer.full()` 短路检查（L404）、`consumer.split_at(mid)` 与生产者对齐（L409）、`join_context` 并行（L410-L429）、`reducer.reduce`（L430）、叶子的 `into_folder → fold_with → complete`（L432）。你在 4.3.4 写的每一个方法，都能在这 30 行里找到被调用的确切位置。

#### 4.3.4 代码实践：手写 CountConsumer 并用 drive 驱动

`ForEachConsumer` 的 `Result = ()` 看不出 `Reducer` 的作用，我们写一个**带结果**的：数元素个数（功能等价于 `.count()`，但手写以展示完整协议）。

1. **实践目标**：实现一个最小的有状态消费者，分别经索引路径（`drive`）与无索引路径（`drive_unindexed`）驱动，验证两条路结果一致。
2. **操作步骤**（示例代码）：

```rust
use rayon::iter::plumbing::{Consumer, Folder, Reducer, UnindexedConsumer};
use rayon::prelude::*;

struct Count;                       // 消费者本体：无状态，可无限分裂
struct CountFolder { count: u64 }   // 折叠器：累积状态
struct CountReducer;                // 归并器

impl Consumer<i64> for Count {
    type Folder = CountFolder;
    type Reducer = CountReducer;
    type Result = u64;

    fn split_at(self, _index: usize) -> (Self, Self, Self::Reducer) {
        (Count, Count, CountReducer) // 无内部状态：直接复制
    }

    fn into_folder(self) -> Self::Folder {
        CountFolder { count: 0 }
    }

    fn full(&self) -> bool {
        false // 我们永不提前喊停
    }
}

impl Folder<i64> for CountFolder {
    type Result = u64;

    fn consume(mut self, _item: i64) -> Self {
        self.count += 1;
        self
    }

    fn complete(self) -> u64 {
        self.count
    }

    fn full(&self) -> bool {
        false
    }
}

impl Reducer<u64> for CountReducer {
    fn reduce(self, left: u64, right: u64) -> u64 {
        left + right // 左右两半的计数相加
    }
}

impl UnindexedConsumer<i64> for Count {
    fn split_off_left(&self) -> Self {
        Count
    }

    fn to_reducer(&self) -> Self::Reducer {
        CountReducer
    }
}

fn main() {
    // 索引路径：drive（走 bridge，生产者与消费者按精确中点对齐切分）
    let n = (0..1_000).into_par_iter().map(|i| i as i64).drive(Count);
    assert_eq!(n, 1000);

    // 无索引路径：drive_unindexed（走 bridge_unindexed，任意对半）
    let m = [1i64, 2, 3].par_iter().copied().drive_unindexed(Count);
    assert_eq!(m, 3);

    println!("count = {n}, {m}");
}
```

3. **需要观察的现象**：两个断言均通过；无论线程池几个线程、切分多少刀，结果恒定。
4. **预期结果**：打印 `count = 1000, 3`。计数与切分方式无关是因为 `Reducer::reduce` 用加法合并、`Folder` 从 0 起步——这正是 u3-l2 讲过的「可结合 + 单位元」要求的最小体现。确定性输出，可本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`ForEachConsumer::split_at` 为什么可以直接写成 `(self.split_off_left(), self, NoopReducer)`？

答案：它没有内部状态（只持一个 `&F` 共享引用）也不依赖元素位置，所以「按 index 分裂」与「任意分裂」没有区别，借用无索引侧的 `split_off_left`（复制引用）实现即可，`NoopReducer` 对 `()` 结果也无事可做。一旦消费者有位置语义（如 `collect` 要知道每段写入目标区间），就必须真正使用 `index` 参数（u4-l4 的 `CollectConsumer` 按区间分裂）。

**练习 2**：把 `Count` 改成 `Max`（求最大值）需要动哪几处？

答案：四处。`Folder` 状态改为 `Option<i64>`；`consume` 改为 `self.max = self.max.map_or(Some(item), |m| Some(m.max(item)))` 之类的比较；`complete` 返回该 `Option`；`Reducer::reduce` 改为取两者较大（`None` 视为负无穷，即「空段不参与」）。`split_at` / `full` 不变。注意 `Result = Option<i64>` 仍满足 `Send`。

**练习 3**：`Consumer::full` 返回 `true` 会发生什么？哪些内置功能依赖它？

答案：引擎 `helper` 在每层递归开头检查（L404），为 `true` 时直接 `into_folder().complete()` 返回、不再切分也不再喂元素——注意它**不会中断正在进行的叶子任务**，只是停止派发新工作。依赖它的内置功能：`find_any` / `any` / `all`（共享 `AtomicBool` 置位后各处 `full` 变真，u3-l4）、`try_reduce` 家族与 `panic_fuse`（u2-l5）。自定义消费者若想支持被这些操作「包住」，也需要正确实现 `full`。

## 5. 综合实践

现在不看答案，从零实现完整的 `RepeatN<T>`，并用标准库做对照测试。这是本讲的毕业任务，覆盖全部三个模块：迭代器结构体（4.1）、Producer（4.2）、以及让 map/sum/collect 可用的全链路验证。

### 5.1 工程搭建

```bash
cargo new repeatn-lab
cd repeatn-lab
```

在 `Cargo.toml` 加入依赖（沿用 u1-l3 创建的示例工程亦可，或改用 `rayon = { path = "本地仓库路径" }` 指向本仓库）：

```toml
[dependencies]
rayon = "1"
```

### 5.2 完整实现

写入 `src/main.rs`（示例代码，与本仓库 `src/iter/repeat.rs` 同构但简化：不用 `Either` + `NonZeroUsize` 枚举，直接存 `element + count` 两个字段）：

```rust
use rayon::iter::plumbing::{
    bridge, Consumer, Folder, Producer, ProducerCallback, Reducer, UnindexedConsumer,
};
use rayon::prelude::*;

/// 重复元素生成器：产出 `element` 的 `n` 份克隆
pub struct RepeatN<T> {
    element: T,
    count: usize,
}

pub fn repeat_n<T: Clone + Send>(element: T, n: usize) -> RepeatN<T> {
    RepeatN { element, count: n }
}

// ── 模块一：迭代器结构体 ───────────────────────────────────────
// into_par_iter() 由 blanket impl 自动获得，无需手写
impl<T: Clone + Send> ParallelIterator for RepeatN<T> {
    type Item = T;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        bridge(self, consumer) // 有精确长度，借用 indexed 桥即可
    }

    fn opt_len(&self) -> Option<usize> {
        Some(self.len()) // 打开 collect 的已知长度快速路径
    }
}

impl<T: Clone + Send> IndexedParallelIterator for RepeatN<T> {
    fn drive<C>(self, consumer: C) -> C::Result
    where
        C: Consumer<Self::Item>,
    {
        bridge(self, consumer)
    }

    fn with_producer<CB>(self, callback: CB) -> CB::Output
    where
        CB: ProducerCallback<Self::Item>,
    {
        // 按值搬出字段组装生产者，零克隆
        callback.callback(RepeatNProducer {
            element: self.element,
            count: self.count,
        })
    }

    fn len(&self) -> usize {
        self.count
    }
}

// ── 模块二：Producer ──────────────────────────────────────────
struct RepeatNProducer<T> {
    element: T,
    count: usize,
}

impl<T: Clone + Send> Producer for RepeatNProducer<T> {
    type Item = T;
    // std 的 RepeatN 本身就是 DoubleEnded + ExactSize 迭代器，直接复用
    type IntoIter = std::iter::RepeatN<T>;

    fn into_iter(self) -> Self::IntoIter {
        std::iter::repeat_n(self.element, self.count)
    }

    fn split_at(self, index: usize) -> (Self, Self) {
        assert!(index <= self.count);
        (
            RepeatNProducer {
                element: self.element.clone(), // 左半付一次克隆
                count: index,
            },
            RepeatNProducer {
                element: self.element, // 右半复用被移动的原值
                count: self.count - index,
            },
        )
    }
}

fn main() {
    let v: Vec<i32> = repeat_n(7, 5).into_par_iter().collect();
    println!("collect: {v:?}");

    let sum: i64 = repeat_n(3, 1000).into_par_iter().map(|x| x as i64).sum();
    println!("sum: {sum}");
}
```

### 5.3 对照测试

在同文件末尾追加（模仿 [src/iter/test.rs:L16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L16) 的 `is_indexed` 技法与 [src/iter/test.rs:L2152-L2155](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L2152-L2155) 的 `check_repeat_take` 断言）：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn is_indexed<I: IndexedParallelIterator>(_: &I) {}

    #[test]
    fn indexed_statically() {
        is_indexed(&repeat_n(4, 8)); // 编译期即验证索引能力
    }

    #[test]
    fn matches_std_repeat_take() {
        // 任务要求的主对照：与 std::iter::repeat().take(n) 结果一致
        for n in [0usize, 1, 2, 7, 100, 10_000] {
            let par: Vec<i32> = repeat_n(7, n).into_par_iter().collect();
            let seq: Vec<i32> = std::iter::repeat(7).take(n).collect();
            assert_eq!(par, seq, "n = {n}");
        }
    }

    #[test]
    fn sum_and_map() {
        let par: i64 = repeat_n(3, 1000).into_par_iter().map(|x| x as i64).sum();
        let seq: i64 = std::iter::repeat(3).take(1000).map(|x| x as i64).sum();
        assert_eq!(par, seq);
    }

    #[test]
    fn indexed_benefits() {
        // enumerate / rev / zip / with_max_len 只有索引迭代器可用
        let e: Vec<(usize, i32)> = repeat_n(5, 4).into_par_iter().enumerate().collect();
        assert_eq!(e, vec![(0, 5), (1, 5), (2, 5), (3, 5)]);

        let r: Vec<i32> = repeat_n(5, 4).into_par_iter().rev().collect();
        assert_eq!(r, vec![5, 5, 5, 5]);

        let z: Vec<(i32, i32)> = repeat_n(9, 3).into_par_iter().zip(1..4).collect();
        assert_eq!(z, vec![(9, 1), (9, 2), (9, 3)]);

        let s: i32 = repeat_n(1, 1024).into_par_iter().with_max_len(64).sum();
        assert_eq!(s, 1024);
    }
}
```

### 5.4 运行与观察

```bash
cargo run            # 预期输出：collect: [7, 7, 7, 7, 7] 与 sum: 3000
cargo test           # 四个测试全部通过
cargo test -- --nocapture
```

预期结果（均确定性，可本地验证）：

1. `main` 打印 `collect: [7, 7, 7, 7, 7]` 和 `sum: 3000`。
2. `matches_std_repeat_take` 在含 `n = 0`（空序列是切分实现的经典边角）在内的六种规模下与标准库逐一相等。
3. `indexed_benefits` 证明索引能力真实生效：`enumerate`、`rev`、`zip`、`with_max_len` 编译通过且结果正确——这四个方法在只实现 `ParallelIterator` 时连编译都不能通过。

### 5.5 进阶实验（选做）

1. **克隆计数**：仿照 [src/iter/test.rs:L2187-L2249](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/test.rs#L2187-L2249) 定义 `Clone`/`Drop` 各自增全局 `AtomicUsize` 的 `Counter` 类型，统计你的 `repeat_n(Counter, 100).into_par_iter().count()` 的克隆次数。我们的简化版会略高于 rayon 的 99 次（边界切分 `index == 0` / `index == count` 时也会克隆左半），思考差额来源后，再尝试用 `Option` 字段或枚举把边界克隆也省掉，向官方实现对齐。
2. **双端验证**：给测试加上 `.rev().sum()` 与 `.sum()` 相等的断言，确认 `IntoIter` 的 `DoubleEndedIterator` 约束不是白写的。
3. **粒度观察**：用 `with_max_len(1)` 与 `with_max_len(10_000)` 分别跑 `repeat_n(0u64, 1_000_000).sum()` 并计时，直观感受 u3-l3 讲过的切分开销与任务粒度取舍。

## 6. 本讲小结

- 自定义数据源的三层结构：**迭代器结构体**（`Item` + `drive_unindexed`/`opt_len` + `len`/`drive`/`with_producer` 五件套，`into_par_iter` 由 blanket impl 免费获得）→ **Producer**（`into_iter` + `split_at`，长度由框架从 `len()` 记账）→ 引擎 `bridge` 负责切分、并行与合并，数据源代码里没有一行线程操作。
- `with_producer` 是迭代器世界与生产者世界的唯一交换口，`drive`/`drive_unindexed` 几乎总是一行 `bridge(self, consumer)`；`opt_len` 只影响 collect 快慢、不影响正确性。
- `Producer` 契约三规则：`IntoIter` 必须双端 + 精确长度；`split_at(index)` 按值切分、下标相对自身、恒有 `index ≤ N`；克隆成本存在不变量——全量消费 \( n \) 个元素恰好 \( n - 1 \) 次克隆，与切分刀数无关（rayon 用 `Empty` + `NonZeroUsize` 进一步省掉边界克隆）。
- `Consumer` 三件套（`split_at` / `into_folder` / `full`）配 `Folder`（`consume`/`complete`）与 `Reducer`（`reduce`）构成推模式协议；纯数据源不需要写消费者，但 `drive(my_consumer)` 是直接以推模式扩展 rayon 的正规出口。
- 正确性验证的标准姿势：与 `std::iter` 对照（`repeat_n(x, n)` ≡ `repeat(x).take(n)`）、用 `is_indexed` 做编译期断言、务必覆盖 `n = 0` 边角、用 `rev` 验证双端能力。

## 7. 下一步学习建议

本讲实现了**有索引**的自定义数据源；下一讲 u9-l2《自定义 Producer 与 Split 扩展》转向无索引一侧：为链表、树这类「没有长度、不能按下标切分」的结构实现 `UnindexedProducer::split` 的按值切分，学习 `rayon::iter::split` 函数与 `Split` 结构体（u3-l6 已铺垫）以及 `tests/producer_split_at.rs` 的契约测试。建议在进入下一讲前：

1. 把 5.5 的克隆计数进阶实验做完——它会逼你逐行重读 [src/iter/repeat.rs:L221-L240](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/repeat.rs#L221-L240) 的 `split_at`。
2. 重读 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md) 的「What on earth is ProducerCallback?」一节，此时你已亲手写过 callback，应能完全看懂当年 closure 方案卡在哪个生命周期上。
3. 对照 [src/iter/for_each.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/for_each.rs) 与你手写的 `Count` 消费者，再看一眼 [src/iter/collect/consumer.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/collect/consumer.rs) 里位置敏感的 `CollectConsumer`，体会「无状态可任意分裂 / 有状态须按区间分裂」两个极端之间的谱系。
