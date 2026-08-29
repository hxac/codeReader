# ParallelIterator 与 IndexedParallelIterator

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ParallelIterator` 与 `IndexedParallelIterator` 两个根 trait 的接口差异，以及「有索引（已知长度）」到底意味着什么。
2. 理解「长度信息」为什么是并行切分的前提：没有长度就无法 `split_at`，就无法 `zip`、`enumerate`、`rev`，`collect` 也拿不到快速路径。
3. 区分两条驱动路径：`drive_unindexed`（配 `UnindexedConsumer`）与 `drive`（配 `Consumer`），以及 `bridge_unindexed` 与 `bridge` 分别如何完成递归切分。
4. 掌握一个实用的阅读方法：看一个方法的**返回类型**，就能判断它是惰性适配器还是立即执行的消费者。

本讲只讲 trait 层面的「契约」，不深入 `map`/`filter` 的完整实现（那是单元三的内容），也不涉及线程池调度（那是单元五的内容）。

## 2. 前置知识

- **trait 与 trait 方法**：Rust 的 trait 类似其他语言的接口。trait 可以带默认实现的方法；调用 trait 方法前需要该 trait 在作用域内（这就是 `use rayon::prelude::*` 的作用，见上一讲）。
- **惰性与立即执行**：标准库迭代器是惰性的——`map`/`filter` 只是把迭代器包一层，真正的计算发生在 `sum`/`collect`/`for` 循环这些「消费者」上。Rayon 沿用了这套心智模型：**先构造计算图，再一次性执行**。
- **并行为什么需要切分**：把一份数据分给多个线程，前提是能把数据「劈开」。对切片来说在任意下标处劈开是 O(1) 的；但对一个过滤后的流，劈开之前你根本不知道每个元素会留下还是被丢弃——这正是本讲两条驱动路径的分水岭。
- **关联类型**：`type Item: Send;` 这样的写法表示「实现者必须指明产出元素的类型，且该类型必须可跨线程传递（`Send`）」。这是 Rayon 在编译期拦住数据竞争的第一道关卡（回顾 u1-l1）。
- **子 trait（超 trait）**：`trait IndexedParallelIterator: ParallelIterator` 表示「索引迭代器首先必须是一个并行迭代器」，`ParallelIterator` 的所有方法它都继承。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/iter/mod.rs` | 定义 `ParallelIterator`、`IndexedParallelIterator` 两大根 trait，以及 `IntoParallelIterator` 等入口 trait；约 3600 行，是 rayon 上层最核心的文件 |
| `src/iter/plumbing/mod.rs` | 底层「管道」协议：`Producer`、`Consumer`、`Folder`、`Reducer`、`UnindexedProducer`、`UnindexedConsumer`，以及 `bridge` / `bridge_unindexed` 两个驱动引擎 |
| `src/iter/map.rs` | `Map` 适配器：同时实现两个 trait 的范例（保留索引能力） |
| `src/iter/filter.rs` | `Filter` 适配器：只实现 `ParallelIterator` 的范例（丢失索引能力） |

记忆锚点（承接 u1-l4 的代码地图）：`src/iter/` 下「一个适配器一个文件」，而 `mod.rs` 是所有 trait 的总纲；`plumbing` 子模块是 trait 与调度器之间的夹层。

## 4. 核心概念与源码讲解

### 4.1 ParallelIterator trait

#### 4.1.1 概念说明

`ParallelIterator` 是**所有**并行迭代器的根 trait，是标准库 `Iterator` 的并行对应物。它的接口分三类：

1. **惰性适配器（combinator）**：如 `map`、`filter`，返回一个新的迭代器类型，不做任何计算。
2. **立即执行的消费者（consumer）**：如 `for_each`、`sum`、`collect`，触发真正的并行执行并返回结果。
3. **内部驱动方法**：`drive_unindexed` 与 `opt_len`，标注为「Internal method」，用户不应直接调用，它们是实现自定义迭代器的挂点。

一个重要的工程事实：`ParallelIterator` **不是 dyn 兼容的**，你不能写 `Box<dyn ParallelIterator>`。模块文档明确说了这样做的目的是保持实现简单并允许额外优化——并行计算图因此完全是静态类型、零虚函数调用的。

#### 4.1.2 核心流程

构建与执行两阶段的心智模型：

```text
数据源                     惰性适配器链                      消费者
(0..n).into_par_iter() → .map(f).filter(p) ... → .sum()  ← 这里才"点火"
```

- 每个适配器把上游迭代器包一层，形成编译期完全展开的洋葱结构。
- 消费者方法内部最终调用 `drive_unindexed`（或 indexed 路径的 `drive`），把整个计算图交给 plumbing 层切分执行。
- 很多消费者是用其他消费者实现的默认方法。例如 `try_for_each` 的默认实现就是 `self.map(op).try_reduce(...)` ——先映射成 `Result`，再做可短路的归约。

#### 4.1.3 源码精读

先看根 trait 的定义与唯一的关联类型：

[src/iter/mod.rs:L359-L365](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L359-L365)
`ParallelIterator` 要求实现者是 `Sized + Send`（整个迭代器要能送到别的线程），并定义关联类型 `Item: Send`（产出的元素也要能跨线程）。

最简单的消费者 `for_each`：

[src/iter/mod.rs:L376-L381](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L376-L381)
它返回 `()`（不是新迭代器类型），所以是立即执行的消费者；实现直接转调自由函数 `for_each::for_each(self, &op)`，闭包要求 `Fn + Sync + Send`——`Sync` 是因为多个线程可能同时读这个闭包。

典型的惰性适配器 `map`：

[src/iter/mod.rs:L598-L604](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L598-L604)
返回类型是 `Map<Self, F>`——一个新类型，这就是「惰性」的形式标志。函数体只是 `Map::new(self, map_op)`，一行计算都没有。

对照标准库的关键差异——`fold` 在 rayon 里是惰性的：

[src/iter/mod.rs:L1263](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1263)
`fn fold<...>(self, identity: ID, fold_op: F) -> Fold<Self, ID, F>`——它返回 `Fold` 迭代器，产出「每个并行分段各自折叠出的中间值」，之后还需要再接一个 `sum`/`reduce` 才能得到最终结果。而标准库 `Iterator::fold` 是立即返回累计值的。这是初学者最常踩的语义差异之一（详见 u3-l2）。

消费者如何「套娃」实现：

[src/iter/mod.rs:L469-L479](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L469-L479)
`try_for_each` 的默认实现是 `self.map(op).try_reduce(<()>::default, ok)`——用 `map` + `try_reduce` 两个已有积木拼出来。注意 `R: Try<Output = ()>` 这个约束里的 `Try` 是一个刻意不对公众开放的私有 trait（见 [src/iter/mod.rs:L68-L72](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L68-L72) 的说明），它镜像了尚未稳定的 `std::ops::Try`，为 `Option`/`Result` 提供短路语义。

再看 `collect`：

[src/iter/mod.rs:L2063-L2068](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2063-L2068)
`collect` 把整个迭代器交给目标集合的 `FromParallelIterator::from_par_iter` 实现，由集合决定怎么收（这是 u2-l4 的主题）。

trait 的收尾是两个内部方法（详细讲解见 4.3）：

- [src/iter/mod.rs:L2410-L2412](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2410-L2412)：`drive_unindexed`，唯一一个**没有默认实现**的公开层方法——每个并行迭代器必须自己回答「如何被驱动」。
- [src/iter/mod.rs:L2428-L2430](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2428-L2430)：`opt_len` 默认返回 `None`（长度未知）；若返回 `Some(n)`，则承诺本迭代器只会走 indexed 的 `Consumer` 协议（如 `split_at`），乱实现会导致 panic。它目前被 `collect` 用来触发快速路径，相当于手工模拟「特化（specialization）」。

#### 4.1.4 代码实践：给 ParallelIterator 的方法分类

这就是本讲的主实践任务：**数出 `ParallelIterator` 提供的方法并分类，写成一张表格注释**。

1. **实践目标**：建立「看返回类型就知道惰性/立即」的直觉，并亲手盘点一次 API 面。
2. **操作步骤**：
   - 打开 `src/iter/mod.rs`，定位到 `pub trait ParallelIterator`（第 359 行）到 trait 结束（第 2431 行）之间的区域。
   - 在仓库根目录执行（只读命令）：
     ```bash
     grep -n "^    fn " src/iter/mod.rs | awk -F: '$1 >= 359 && $1 <= 2431'
     ```
   - 判断标准：返回类型是 `Map<Self, F>`、`Filter<Self, P>` 这类迭代器新类型的 → **惰性适配器**；返回 `usize`、`bool`、`Option<...>`、`Self::Item`、集合类型的 → **立即执行消费者**；标注 Internal 的 → **内部驱动方法**。
   - 在你 u1-l3 创建的示例工程的 `main.rs` 顶部，把结果整理成一张表格写进注释块。
3. **需要观察的现象**：哪些方法名与标准库 `Iterator` 重名但语义不同（重点看 `fold`）；`try_*` 家族有多少个；`*_any` 后缀的方法（顺序无关，见 u3-l4）有多少个。
4. **预期结果**：按上述区间统计，惰性适配器约 **25** 个（`map`/`map_with`/`map_init`/`cloned`/`copied`/`inspect`/`update`/`filter`/`filter_map`/`flat_map`/`flat_map_iter`/`flatten`/`flatten_iter`/`fold`/`fold_with`/`try_fold`/`try_fold_with`/`chain`/`while_some`/`panic_fuse`/`intersperse`/`take_any`/`skip_any`/`take_any_while`/`skip_any_while`）；立即执行消费者约 **33** 个（for_each 家族 6 个、count/reduce/sum/product 家族 7 个、min/max 家族 6 个、find 家族 7 个、any/all 2 个、collect/unzip/partition 家族 5 个）；另有内部方法 2 个。你的统计与这份参考允许有细微出入（例如是否把 `try_*` 单独归为一类），但数量级应一致。**待本地验证**：具体条数以你机器上 grep 的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ParallelIterator::drive_unindexed` 没有默认实现，而 `opt_len` 有？

**答案**：`opt_len` 的默认值 `None` 对「长度未知」的迭代器是正确语义，大多数实现可以直接用；而「如何把自身喂给一个消费者」是每个迭代器各不相同的本质行为（数据源自己生产、适配器转发给上游），无法给出统一默认值，所以必须由实现者提供——它是这个 trait 唯一的「必须回答的问题」。

**练习 2**：`(0..100).into_par_iter().map(|x| x * 2)` 会执行任何乘法吗？

**答案**：不会。`map` 是惰性适配器，只构造了 `Map<Range<i32>, _>` 类型的值；乘法要等到 `sum()`/`collect()`/`for_each()` 等消费者出现时才发生。

**练习 3**：`try_for_each` 的默认实现为什么可以建立在 `map` + `try_reduce` 之上？

**答案**：`map(op)` 把每个元素变成 `Result`/`Option`，`try_reduce` 在归约时一旦遇到错误值就短路并尽快返回（并行下的「尽快」意味着多个错误竞争，不保证返回哪一个，这正是文档说明的行为）。所以「逐个执行、可能提前停」被拆成了「映射成可短路类型 + 可短路归约」两个可复用的积木。

### 4.2 IndexedParallelIterator trait

#### 4.2.1 概念说明

`IndexedParallelIterator` 是 `ParallelIterator` 的子 trait，表示「支持对数据的随机访问：可以在**任意索引**处切分，并从切分点取数据」。通俗地说就是**长度已知、位置可寻**。

这层额外契约解锁了一批必须知道长度（或必须按下标对齐）的操作：

- `zip` / `zip_eq`：两个序列按下标配对——不知道各自多长就无法配对。
- `enumerate` / `positions`：给元素编号——不知道起点和长度就编不出号。
- `rev` / `skip` /`take` / `step_by`：按下标反向、跳过、截取。
- `len`：直接问长度。
- `with_min_len` / `with_max_len`：控制切分粒度（u3-l3 详讲）。
- `cmp` / `eq` 等逐元素比较：需要两侧同步推进。
- 以及 `collect` 的快速路径（预分配定长缓冲区，u4-l4 详讲）。

关键机制：**索引能力会在链条中丢失，且丢了就找不回来**。`map` 保留索引（每个输入对应一个输出，长度不变）；`filter` 丢失索引（留下多少个元素取决于运行时的谓词结果）。一旦丢失，`enumerate` 等方法就直接从可用方法列表里消失——这不是运行时错误，而是**编译错误**。

#### 4.2.2 核心流程

一个并行迭代器「有索引」需要同时满足三件事，对应 trait 的三个无默认实现的方法：

```text
len()            → 我总共有多少元素（切分的依据）
drive(consumer)  → 我知道如何被"带索引的消费者"驱动
with_producer(cb) → 我能交出一个可 split_at 的 Producer（u4-l2 详讲）
```

切分时长度信息的用处：中点 \( \text{mid} = \lfloor \text{len} / 2 \rfloor \)，生产者和消费者在**同一个 mid** 处对半劈开，两侧元素与两侧消费逻辑严格对位。没有长度就无法保证这种对位——这就是 zip 必须要求索引的根因。

索引能力的传播规则：

```text
IndexedParallelIterator --map/inspect/cloned/...--> 仍是 Indexed（长度不变）
IndexedParallelIterator --filter/filter_map/while_some/...--> 退化为 Parallel（长度不可知）
ParallelIterator       --任何适配器--> 不可能升格为 Indexed（信息无法凭空恢复）
```

#### 4.2.3 源码精读

trait 定义与一条重要的平台限制：

[src/iter/mod.rs:L2442-L2449](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2442-L2449)
文档把「有索引」定义为支持随机访问切分；注释还指出 `u64/i64/u128/i128` 范围未实现此 trait——因为长度可能超出 `usize` 无法表示（64 位平台上 `u64::MAX..` 之类）。

`zip` 的约束写法：

[src/iter/mod.rs:L2584-L2589](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2584-L2589)
`Z: IntoParallelIterator<Iter: IndexedParallelIterator>` ——右侧操作数转成并行迭代器后**必须是有索引的**。因为 zip 的实现要反复在两侧相同下标处 `split_at`，长度未知的一侧无法配合。

三个核心方法：

- [src/iter/mod.rs:L3220](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3220)：`fn len(&self) -> usize;`——精确承诺产出元素个数（假定不 panic）。
- [src/iter/mod.rs:L3236](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3236)：`fn drive<C: Consumer<Self::Item>>(self, consumer: C) -> C::Result;`——与 `drive_unindexed` 平行的驱动入口，消费者是**带索引**的 `Consumer`，切分时会被告知切在哪个下标。
- [src/iter/mod.rs:L3253](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3253)：`fn with_producer<CB: ProducerCallback<Self::Item>>(self, callback: CB) -> CB::Output;`——把自身转换成 Producer 并交给回调。回调模式的存在原因：Producer 的具体类型不进 API，调用方必须「对任意 P 泛型」，这样生产者类型里可以藏引用，rayon 也能不改公共 API 地调整它。

「保留索引」与「丢失索引」的正反两个例子：

保留——`Map` 同时实现两个 trait：

- [src/iter/map.rs:L47-L49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L47-L49)：`ParallelIterator::opt_len` 覆盖为 `self.base.opt_len()`——上游知道长度我就知道。
- [src/iter/map.rs:L52-L68](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L52-L68)：`Map` 在上游是 `IndexedParallelIterator` 时也实现 `IndexedParallelIterator`，`len()` 直接转发 `self.base.len()`——一对一映射长度不变。

丢失——`Filter` 只有 `ParallelIterator` 实现：

- [src/iter/filter.rs:L29](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs#L29)：整个文件里 `Filter` 只有一个 `impl ParallelIterator for Filter<I, P>`，没有 `IndexedParallelIterator` 的实现。
- [src/iter/filter.rs:L89](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs#L89)：它的消费者实现的是 `UnindexedConsumer`——过滤后的元素数在数完之前不可知，只能走「无索引」协议。

#### 4.2.4 代码实践：亲手触发一次「索引丢失」

1. **实践目标**：用编译器验证「`filter` 之后 `enumerate` 不可用」是编译期约束而非运行时行为。
2. **操作步骤**：在 u1-l3 的示例工程 `main.rs` 中写入两段代码（标注为示例代码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       // A：map 之后 enumerate —— 应能编译
       let v: Vec<(usize, i32)> = (0..10)
           .into_par_iter()
           .map(|x| x * x)
           .enumerate()
           .collect();
       println!("{:?}", v);

       // B：filter 之后 enumerate —— 预期编译失败
       let w: Vec<(usize, i32)> = (0..10)
           .into_par_iter()
           .filter(|x| x % 2 == 0)
           .enumerate() // 预期：no method named `enumerate`
           .collect();
       println!("{:?}", w);
   }
   ```

   先 `cargo build` 观察 B 段的报错，再把 B 段注释掉确认 A 段可运行。
3. **需要观察的现象**：编译器报错大致是 `no method named enumerate found for struct Filter<...>`，并会提示 `enumerate` 存在于 `IndexedParallelIterator` 中但 trait 不在作用域的实现里。**待本地验证**：具体报错文案随编译器版本略有差异。
4. **预期结果**：A 段输出 `[(0, 0), (1, 1), (2, 4), ..., (9, 81)]`；B 段无法通过编译——`Filter` 没实现 `IndexedParallelIterator`，`enumerate`（定义于 [src/iter/mod.rs:L2932](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2932)）对其不存在。这个报错正是「长度信息在类型系统中流动」的直接证据。

#### 4.2.5 小练习与答案

**练习 1**：`a.par_iter().zip(b.par_iter())`（`a`、`b` 是 `Vec<i32>`）与 `a.par_iter().filter(p).zip(b.par_iter())` 哪个能编译？为什么？

**答案**：前者能。切片引用的并行迭代器是 `IndexedParallelIterator`；后者中 `filter` 把左侧降级为普通 `ParallelIterator`，而 `zip` 定义在 `IndexedParallelIterator` 上且要求 `Self` 本身有索引，因此编译失败。

**练习 2**：`par_iter().map(f).len()`（`Vec` 数据源）返回什么？`par_iter().filter(p).len()` 呢？

**答案**：前者返回 `Vec` 的长度——`Map` 实现了 `IndexedParallelIterator` 且 `len` 转发上游。后者无法编译——`Filter` 上根本没有 `len` 方法；「过滤后剩几个」只能用 `count()` 这种消费者在运行时数出来。

**练习 3**：为什么 rayon 不让 `filter` 也实现 `IndexedParallelIterator`，哪怕返回一个「估计长度」？

**答案**：indexed 协议的核心承诺是**精确**：`len` 与 `split_at` 的下标必须严格对位，`collect` 据此预分配并直接按下标写入内存（见 `opt_len` 文档中的警告）。过滤后的元素数只能事后得知，任何估计值都会破坏「切分点 = 写入位置」的不变量，所以宁可在类型上禁止，也不留运行时陷阱。

### 4.3 驱动方法族：drive_unindexed 与 drive 两条路径

#### 4.3.1 概念说明

前面反复出现的「点火」，在源码里就是两个驱动方法：

| | `drive_unindexed` | `drive` |
| --- | --- | --- |
| 所属 trait | `ParallelIterator`（全部迭代器） | `IndexedParallelIterator`（有索引的） |
| 消费者参数 | `UnindexedConsumer`（可自由复制、不带下标切分） | `Consumer`（带下标 `split_at`） |
| 桥接引擎 | `bridge_unindexed` | `bridge` |
| 切分方式 | 「请自己看着办劈一半」（`split`） | 「劈在下标 mid 处」（`split_at(mid)`） |
| 适用场景 | filter 后的流、哈希表遍历等长度未知的数据 | 切片、范围等长度已知的数据 |

plumbing 层为这两条路径分别准备了一组类型（本讲只认脸，细节在单元四）：

- `Producer` / `UnindexedProducer`：可分裂的生产者（数据这一半）。
- `Consumer` / `UnindexedConsumer` / `Folder` / `Reducer`：可分裂的消费者（计算这一半）。

#### 4.3.2 核心流程

**indexed 路径（`bridge`）** 的递归逻辑（[src/iter/plumbing/mod.rs:L393-L434](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L393-L434)）：

```text
helper(len, producer, consumer):
    若 consumer.full()：直接收尾（短路，如 find 已命中）
    若还应该切分（LengthSplitter 同意）：
        mid = len / 2
        (左生产者, 右生产者) = producer.split_at(mid)
        (左消费者, 右消费者, reducer) = consumer.split_at(mid)
        (左结果, 右结果) = join_context(递归左, 递归右)   # 两个闭包进线程池
        返回 reducer.reduce(左结果, 右结果)
    否则：
        producer.fold_with(consumer.into_folder()).complete()  # 顺序消费这一段
```

切分策略由 `LengthSplitter` 决定，它融合两种考虑：

- 自适应的「窃取式切分」：初始期望切分次数设为线程数，一旦任务真的被别的线程偷走就重置（[src/iter/plumbing/mod.rs:L267-L283](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L267-L283)）。
- 长度下界/上界：`with_min_len`/`with_max_len` 生效的地方，最少切分次数为
  \[ \text{min\_splits} = \left\lfloor \frac{\text{len}}{\max(\text{max\_len}, 1)} \right\rfloor \]
  且不会再切到使段长小于 `min`（[src/iter/plumbing/mod.rs:L308-L332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L308-L332)）。

**unindexed 路径（`bridge_unindexed`）** 的差别只在切分那一步（[src/iter/plumbing/mod.rs:L447-L476](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L447-L476)）：

```text
producer.split() 返回 (左, Option<右>)：
    有右半 → 消费者用 split_off_left() 无下标地对半分，join_context 两路递归，reduce 合并
    无右半（数据太少，不必再劈）→ 退化为顺序 fold_with
```

注意 `UnindexedConsumer::split_off_left()` 不接收下标，返回的「左消费者」产出的值在 `find_first` 这类场景中拥有优先权——顺序语义靠「左右」而不是「下标」来维护。

#### 4.3.3 源码精读

驱动入口在两个 trait 里的定义（对照读）：

- [src/iter/mod.rs:L2410-L2412](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2410-L2412)：`drive_unindexed` 收 `UnindexedConsumer`。
- [src/iter/mod.rs:L3236](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3236)：`drive` 收 `Consumer`。

适配器如何实现驱动——`Map` 的两个实现并列在一起看：

- [src/iter/map.rs:L39-L45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L39-L45)：`drive_unindexed` 先把消费者包成 `MapConsumer`（在消费侧补上 map 闭包），再转发给上游——适配器把「变换」塞进消费者，然后委托上游驱动。
- [src/iter/map.rs:L58-L64](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L58-L64)：`drive` 同样的套路，只是走 indexed 协议。这就是 u3-l1 将讲的「委托模式」的雏形。

两个桥接引擎：

- [src/iter/plumbing/mod.rs:L346-L371](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L346-L371)：`bridge` ——先 `par_iter.len()` 拿长度，再经 `with_producer` 换出 Producer，最后进入 `bridge_producer_consumer`。内部定义的 `Callback` 结构就是 `ProducerCallback` 的一个实现样例。
- [src/iter/plumbing/mod.rs:L385-L390](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L385-L390)：`bridge_producer_consumer` 用 `producer.min_len()/max_len()` 和长度构造 `LengthSplitter`，然后进入递归 `helper`（完整递归体见 4.3.2 引用的 L393-L434）。

plumbing 侧的四个关键 trait 签名（认脸即可）：

- [src/iter/plumbing/mod.rs:L56-L97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L56-L97)：`Producer` ——「可分裂的 IntoIterator」，核心是 `fn split_at(self, index: usize) -> (Self, Self)`，转成迭代器后就不能再并行切分；`min_len`/`max_len` 是粒度旋钮。
- [src/iter/plumbing/mod.rs:L123-L146](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L123-L146)：`Consumer` ——「广义的 fold」，`split_at(index)` 同时产出两个消费者和一个 `Reducer`，`full()` 支持短路。
- [src/iter/plumbing/mod.rs:L208-L221](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L208-L221)：`UnindexedConsumer` ——无下标版本，`split_off_left()` 任意点对半分。
- [src/iter/plumbing/mod.rs:L231-L243](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L231-L243)：`UnindexedProducer` ——`split(self) -> (Self, Option<Self>)`，返回 `None` 表示「不值得再劈」。

#### 4.3.4 代码实践：追踪一条真实调用链

1. **实践目标**：验证「消费者方法 → 驱动方法 → 桥接引擎」这条链路真实存在，练一次源码级 grep。
2. **操作步骤**（全部只读）：
   - 打开 `src/iter/for_each.rs`，找到自由函数 `for_each`，确认它调用的是 `par_iter.drive_unindexed(ForEachConsumer)`。
   - 打开 `src/iter/sum.rs`，看 `sum()` 走的是 `drive_unindexed` 还是 `drive`。
   - 在 `src/` 下执行 `grep -rn "bridge_unindexed(" src/iter | head -20` 与 `grep -rn "bridge(" src/iter | head -20`，观察哪些适配器的驱动实现直接调用了桥接引擎（数据源类迭代器大多如此，例如 `src/iter/plumbing/mod.rs` 文档注释里说的用法）。
3. **需要观察的现象**：适配器类（map/filter/...）的 `drive*` 几乎都在「包装消费者后转发上游」；而桥接引擎 `bridge`/`bridge_unindexed` 出现在数据源或需要落地的位置。
4. **预期结果**：你会在 `for_each.rs` 中看到 `drive_unindexed` 调用（因为 for_each 的消费者是无索引的），并看到若干文件的驱动实现直接调 `bridge(...)`/`bridge_unindexed(...)`。**待本地验证**：grep 的具体命中行数以本地输出为准，本实践不修改任何文件。

#### 4.3.5 小练习与答案

**练习 1**：`drive` 和 `drive_unindexed` 的消费者参数类型有何不同？这个差异如何对应「是否知道长度」？

**答案**：`drive` 接 `Consumer`，其 `split_at(index)` 需要被告知切分下标；`drive_unindexed` 接 `UnindexedConsumer`，其 `split_off_left()` 不需要下标。知道长度的数据可以精确对位切分，不知道的只能「任意对半分」。

**练习 2**：`bridge` 的第一行为什么是 `let len = par_iter.len();`？把它去掉行不行？

**答案**：不行。后续 `bridge_producer_consumer` 需要用 `len` 构造 `LengthSplitter`（决定切分次数与下限）并在递归中计算 `mid = len / 2`，还要把 `len` 传给回调以供消费者分配空间。长度正是 indexed 路径相对 unindexed 路径多出来的信息。

**练习 3**：`Splitter::try_split` 在任务被窃取（`stolen == true`）时做了什么？为什么？

**答案**：它把剩余期望切分次数重置为 `max(current_num_threads(), splits / 2)` 并返回 true（[src/iter/plumbing/mod.rs:L267-L283](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L267-L283)）。任务被偷说明当前机器还有空闲线程想吃活，那就继续多切一些喂它们——这是工作窃取调度「自适应负载均衡」在迭代器层的具体体现。

## 5. 综合实践

把本讲三个模块串成一个任务：**给你的示例工程写一份「API 面审计报告」**。

1. 完成 4.1.4 的分类表格（惰性适配器 / 立即执行消费者 / 内部方法三列，含方法名与行号）。
2. 在表格下方补一列「是否保留索引」：对每个惰性适配器，打开 `src/iter/` 下对应文件（文件名与方法名基本一致，如 `map.rs`、`filter.rs`），检查该适配器结构体有没有 `impl IndexedParallelIterator`。有则标「保留」，没有则标「丢失」。
3. 写一个演示程序验证至少三条结论（示例代码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       // 1) map 保留索引：enumerate 可用
       let a: Vec<_> = (0..8).into_par_iter().map(|x| x + 1).enumerate().collect();

       // 2) 长度已知：len 直接可问
       let n = (0..8).into_par_iter().map(|x| x * 2).len();

       // 3) 顺序无关的 take_any 属于无索引世界，但它在 ParallelIterator 上
       let b: Vec<_> = (0..8).into_par_iter().filter(|x| x % 2 == 0).take_any(2).collect();

       println!("a={:?}\nn={}\nb={:?}", a, n, b);
   }
   ```

4. 观察要点：`a` 的编号是否严格对应；`n` 是否等于 8；`b` 的两个元素来自 `{0,2,4,6}` 但**多次运行顺序/选取可能不同**（`take_any` 是顺序无关操作，u3-l4 详讲）。
5. 预期结果：报告表格约 60 行方法清单；三个断言成立；`b` 的输出顺序不保证稳定——这正是「无索引世界牺牲顺序换取并行度」的直观体验。**待本地验证**：`take_any` 的具体输出取决于线程调度。

## 6. 本讲小结

- `ParallelIterator` 是所有并行迭代器的根 trait，唯一关联类型 `Item: Send`；它不是 dyn 兼容的，计算图完全静态展开。
- 判断方法性质看返回类型：返回新迭代器类型（`Map<Self, F>` 等）的是**惰性适配器**，返回具体值/集合的是**立即执行消费者**；`drive_unindexed` 和 `opt_len` 是内部驱动方法。
- `IndexedParallelIterator: ParallelIterator` 追加「长度已知、可按下标切分」的契约，核心三件套是 `len` / `drive` / `with_producer`；它解锁 `zip`、`enumerate`、`rev`、`with_min_len` 等一批方法。
- 索引能力会被 `filter` 等适配器**在类型层面丢失**且无法恢复——`Filter` 只实现 `ParallelIterator`，其消费者是 `UnindexedConsumer`。
- 两条驱动路径：`drive_unindexed` + `UnindexedConsumer` + `bridge_unindexed`（任意对半分） vs `drive` + `Consumer` + `bridge`（在 \( \text{mid} = \lfloor \text{len}/2 \rfloor \) 处精确切分）。
- 切分策略由 `Splitter` / `LengthSplitter` 决定：初始按线程数期望切分，任务被窃取时重置——自适应负载均衡从类型契约一路落到这两个小结构体上。

## 7. 下一步学习建议

- 下一讲（u2-l2 数据源）将把本讲的 trait 落到具体类型上：切片、范围、字符串、`Option`/`Result` 如何各自实现 `into_par_iter`，哪些是 indexed、哪些不是。
- 想先看「消费者如何手写」的读者，可以提前浏览 `src/iter/for_each.rs`（最简消费者）和 `src/iter/plumbing/README.md`（官方总览），为单元四的 plumbing 深潜做准备。
- 对「索引丢失后怎么办」感兴趣的读者，可以关注后续 u3-l4 的 `find_any`/`take_any` 家族——它们就是在无索引世界里用「放弃顺序」换回并行度的设计。
