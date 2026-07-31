# Locator、Tag 与内省定位

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Location` 是什么，以及为什么它必须在多次排版迭代之间保持稳定。
- 解释 `Locator`/`SplitLocator`/`LocatorLink` 是如何通过「分层哈希」为每个排版实例分配稳定、唯一的身份，并理解 `disambiguator`（消歧计数器）解决重复元素（如重复表头、循环生成的图）的作用。
- 看懂 `Tag`（`Start`/`End`，携带 `TagFlags`）如何作为「普通排版内容」流过 flow 管线，最终被 `distribute` 打进 `Frame` 的 `FrameItem::Tag`。
- 理解 `PagedIntrospector` 是排版完成后从最终 `Frame` 反向构建查询索引的纯派生物，以及 `discover_frame` 如何借助 `group.parent` 修正跨帧元素的内省顺序。
- 理解为何 `pages/collect.rs` 里需要一个看起来很「黑科技」的 `migrate_unterminated_tags`，并用 `show heading: it => pagebreak() + it` 这样的真实例子解释它。

本讲是 Typst 的 query、counter、label、outline、footnote 等一切「内省（introspection）」能力能工作的底层地基。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

**直觉一：排版是一个「猜到收敛」的迭代过程。**

很多排版结果依赖「还没排完的信息」。例如：

- 一个图（figure）的编号「图 3」，取决于它前面共有几个图。
- 一个目录（outline）项的页码，取决于目标标题最终落在第几页。
- 一个脚注要排到本页底部，但本页能放多少正文又取决于脚注占了多少空间。

所以 Typst 会反复排版整个文档：先用「上一轮的旧答案」排一遍，得到新的答案，再用新答案排一遍……直到答案不再变化（收敛）。这意味着**同一个元素会被排版很多次**。为了在多轮之间识别「这是同一个元素」，Typst 给每个可定位元素分配一个稳定身份，这就是 `Location`。

**直觉二：身份必须在「可缓存、可并行」的前提下生成。**

如果用一个全局自增计数器当身份（第 1 个元素是 1，第 2 个是 2……），那么在文档中间插一个元素，后面所有元素的身份都会后移，增量编译就废了；而且全局计数器无法并行。Typst 的做法是：把身份做成**递归的哈希**——一个元素的身份由「它在排版调用树中的路径」决定，每层只负责本层的局部哈希。这样身份既是稳定的，又是局部可计算的，从而支持 comemo 记忆化缓存与并行排版（这些概念在 u2-l1 已建立）。

> 术语提示：
> - **realize（现实化）**：排版前把任意 Content 展开成扁平的 `Vec<Pair>`（见 u1-l4）。
> - **introspect（内省）**：在排版过程中/之后，通过 `Location` 查询元素的位置、编号等信息。
> - **locatable（可定位）**：自带 `Location`、能被 query 找到的元素（heading、figure、footnote 等）。

## 3. 本讲源码地图

本讲涉及的关键文件分两层：typst-layout 层（消费与构造）与 typst-library 层（类型定义）。

| 文件 | 所属 crate | 作用 |
| --- | --- | --- |
| `src/flow/mod.rs` | typst-layout | flow 主入口；在 `layout_fragment_impl` 里把 `Tracked<Locator>` 重新拼成 `SplitLocator` 供子排版使用；定义承载待写标签的 `Work.tags`。 |
| `src/introspect.rs` | typst-layout | `PagedIntrospector`：排版完成后从所有 `Page` 的 frame 反向构建查询索引；`discover_frame` 递归遍历 frame。 |
| `src/pages/collect.rs` | typst-layout | 文档级分页的收集器；定义 `Item::Run/Tags/Parity`，并实现关键的 `migrate_unterminated_tags`。 |
| `src/flow/collect.rs` | typst-layout | flow 收集器；把 realized children 里的 `TagElem` 提取成 `Child::Tag`。 |
| `src/flow/distribute.rs` | typst-layout | 把 `Child::Tag` 排进当前 frame，产出 `FrameItem::Tag`。 |
| `introspection/locator.rs` | typst-library | `Locator`/`SplitLocator`/`LocatorLink` 的定义与分层哈希算法（本讲核心理论在此）。 |
| `introspection/tag.rs` | typst-library | `Tag` 枚举、`TagFlags`、`TagElem` 的定义。 |
| `introspection/location.rs` | typst-library | `Location`（身份）的定义。 |
| `introspection/introspector.rs` | typst-library | `ElementIntrospector` 的 `discover_tag`/`start_insertion`/`end_insertion`/`visit`。 |

> 记忆要点：**类型定义在 typst-library，排版期的「流动」与「消费」在 typst-layout。** 本讲义运行在 typst-layout 仓库内，但理解原理必须穿过 crate 边界去看 typst-library 的定义。

---

## 4. 核心概念与源码讲解

### 4.1 Location 与 Locator：为每个排版实例分配稳定身份

#### 4.1.1 概念说明

`Location` 是一个元素在全文档范围内的**唯一身份**，本质上是一个 128 位哈希。它的设计目标有三个（见 `Locator` 类型级文档）：

1. **跨迭代匹配**：同一个元素在多次排版中应得到同一个 `Location`，否则「上一轮的答案」无法对上「这一轮的元素」。
2. **编辑稳定性**：用户在文档中间改一个字，不应让后面所有元素的身份都变（否则增量编译缓存全失效）。
3. **无长期可变状态**：身份生成应是局部的，以支持并行排版。

`Locator` 就是「身份分配器」。每个排版函数都**接收一个 owned `Locator`**，并按需生成 `Location`。关键设计：`Locator` **故意不实现 `Copy`/`Clone`**，强制你在「需要给多个子元素分配身份」时做出明确选择：

- **`split()`**：要给**多个不同的子内容**分配身份时用。它返回一个 `SplitLocator`，可以反复调用 `next(key)` 产出多个互不相同的子 `Locator`。例如把同一张图排两次会得到**两个不同编号**的图。
- **`relayout()`**：要**把同一份内容排多次**（典型是测量 measure）时用。两次复用同一身份，例如测量一张图和真正放置它时编号相同；通常只有一次结果真正进入文档，其余只用于量尺寸。

#### 4.1.2 核心流程

`Locator` 内部只有两个字段：一个本层局部哈希 `local`，和一个指向「更外层缓存定位器」的引用 `outer`。

身份生成的分层哈希算法（伪代码）：

```
# 每一层只维护本层的局部哈希 local 与对外层的引用 outer
# 真正需要 Location 时，才逐层向外 resolve：

resolve(本层):
    if 没有 outer:
        直接返回 local
    else:
        外层哈希 = resolve(outer)        # 递归到更外层
        返回 hash(local, 外层哈希)        # 层层合并
```

其中 `SplitLocator::next(key)` 在本层负责：

1. 用 `key`（通常是子内容的 `Span`）算一个 key 哈希；
2. 查「同样的 key 出现过几次」——这个次数就是 **disambiguator（消歧计数器）**；
3. 把 `(key, disambiguator, local)` 三者一起哈希，作为子 `Locator` 的 `local`。

用公式表达这一步的合并：

\[
\text{local}_{\text{child}} = \mathrm{hash}\bigl(\,(\text{key},\ \text{disambiguator},\ \text{local}_{\text{parent}})\,\bigr)
\]

`disambiguator` 正是解决「重复元素」的关键：`#for _ in range(5) { figure(..) }` 里 5 个 figure 的源码 `Span` 相同，但 `disambiguator` 分别是 0..4，于是得到 5 个不同身份——这就是「重复表头」「循环生成图」能各自独立编号的原因。

#### 4.1.3 源码精读

先看 `Location` 的定义——它就是一个包装了 `u128` 的新类型：

[crates/typst-library/src/introspection/location.rs:52-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/location.rs#L52-L54) —— `Location` 由一个 128 位哈希构成，`Hash`/`Eq` 都直接基于该哈希，因此可作为缓存键。

再看 `Locator` 本体：

[crates/typst-library/src/introspection/locator.rs:153-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L153-L160) —— 只有 `local`（本层哈希）与 `outer`（外层缓存链接）两个字段；这里没有全局计数器，所以天然可并行。

`split()` 与 `relayout()` 的差别：

[crates/typst-library/src/introspection/locator.rs:187-206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L187-L206) —— `split()` 把 `Locator` 转成可反复 `next()` 的 `SplitLocator`；`relayout()` 则是显式克隆（注释说明：`Locator` 不实现 `Clone` 就是为了让「复用同一身份」这个动作变得显眼）。

`next` 的核心：

[crates/typst-library/src/introspection/locator.rs:263-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L263-L282) —— `next(key)` 先把 key 哈希成 u128，再进 `next_inner`；`next_inner` 用 `disambiguators` 这个 `HashMap<u128, usize>` 记录「同一 key 见过几次」，然后把 `(key, disambiguator, local)` 三元组哈希成本子层的 `local`。注意：**外层信息此刻并未合并进来**，留到 `resolve()` 时按需做。

`resolve()` 的分层合并：

[crates/typst-library/src/introspection/locator.rs:211-223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L211-L223) —— 没有外层就直接返回 `local`；有外层就递归 `outer.resolve()` 再 `hash128(&(self.local, outer))` 合并。这里还埋了一个测量模式分支 `Resolved::Measure`，见 4.4 节。

再看 typst-layout 这侧如何把「可追踪的 locator」重新拼回「可用的 SplitLocator」。在 `layout_fragment_impl`（u2-l1 讲过的薄封装 → memoize 模式）里：

[crates/typst-layout/src/flow/mod.rs:137-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L137-L138) —— 入口收到的 `locator: Tracked<Locator>` 先包成一个 `LocatorLink`，再用 `Locator::link(&link).split()` 重建一个**可变可分**的 `SplitLocator`。这正是 `LocatorLink` 存在的意义：跨过 comemo 的 memoize 边界，按需（而不是立即）访问外层信息，从而不破坏缓存命中。

随后 flow 主循环里两处显式 `split`：

[crates/typst-layout/src/flow/mod.rs:209-217](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L209-L217) —— 给 `collect` 阶段分配一个子 locator（`locator.next(&())`）。

[crates/typst-layout/src/flow/mod.rs:223-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L223-L224) —— 主循环里**每个 region** 都 `locator.next(&())` 一次，意味着同一内容排到不同 region/页时会得到不同身份——这正是「同一图在不同页的实例」能被区分的原因。

#### 4.1.4 代码实践

**实践目标**：亲手追踪 `disambiguator` 如何让「同一 `Span` 的重复元素」获得不同身份。

**操作步骤**（源码阅读型）：

1. 打开 [crates/typst-library/src/introspection/locator.rs:268-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L268-L282)，对照下面的「手工演算」走一遍。
2. 假设有如下 Typst 源码（5 个 figure 来自同一 `Span`）：
   ```typst
   #for _ in range(5) { figure(rect()) }
   ```
3. 想象 flow 收集阶段为这 5 个 figure 依次调用 `split.next(&span)`，其中 `span`（即 key）5 次都相同。

**需要观察的现象 / 预期结果**：

- `disambiguators.entry(key)` 第一次返回 `0`（然后槽位变 1），第二次返回 `1`……第五次返回 `4`。
- 5 次 `hash128(&(key, disambiguator, local))` 因 `disambiguator` 不同而得到 5 个不同的 `local`，进而 resolve 出 5 个不同的 `Location`。
- 结论：它们会被 introspector 当成 5 个不同元素，编号为「图 1..图 5」。

> 本地验证：若你想在运行时确认，可在 `next_inner` 末尾临时加一行 `eprintln!("disamb={disambiguator} key={key:?}");`，编译一个含上述循环的小文档，观察 5 行递增的 `disamb`。改动仅用于调试，实践结束请还原，不要提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Locator` 不实现 `Clone`？

> **参考答案**：为了在类型层面强制开发者在「复用身份（relayout）」与「分裂出新身份（split）」之间做显式选择。如果可以随意 `Clone`，就可能在排两份不同内容时误用同一身份，导致内省把两个元素当成同一个。

**练习 2**：把单个全局自增计数器当 `Location`，会违反 `Locator` 文档列出的哪几条设计目标？

> **参考答案**：三条都违反——跨迭代匹配在「依赖内省生成新内容」时会崩（新内容把后续 ID 全部后移）；编辑稳定性差（中间插入会移动所有后续 ID）；显然不满足「无长期可变状态」，无法并行。

---

### 4.2 Tag：把定位标记打入 Frame

#### 4.2.1 概念说明

有了 `Location` 还不够——introspector 需要在排版**之后**「看见」每个元素排在哪。可排版产物是一棵 `Frame` 树（见 u2-l3），而 `Frame` 里装的是字形、图形、图片这些「可见」的东西。元素身份是「不可见」的元信息，怎么塞进 frame？

答案是 `Tag`。`Tag` 是一种特殊的 `FrameItem`，它不画任何东西，只携带「某个元素从这里开始 / 到这里结束」的标记。realize 阶段会为每个 locatable 元素在内容流里插入一个 `TagElem`（它本身是一个合法的 flow-level 元素），于是 `Tag` 就像普通内容一样**顺着 flow 管线流动**，最终被排进 frame。

`Tag` 有两个变体：

- `Tag::Start(Content, TagFlags)`：元素从这里开始。它**直接持有元素本身**（`Content`），因为 introspector 要能 query 出这个元素的内容。
- `Tag::End(Location, u128, TagFlags)`：元素在这里结束。持有 `Location` 和一个 key 哈希（key 哈希放在 `End` 仅仅是为了让两个变体内存大小更均衡，没有语义原因）。

`TagFlags` 有两个布尔位：

- `introspectable`：是否真的要进 introspector（元素是 locatable、被打了 label、或手动设了 location 才为真）。
- `tagged`：是否是「Tagged」元素。

只有 `introspectable` 的 tag 才会被 introspector 记录；否则只是过路、不影响布局也不进索引。

#### 4.2.2 核心流程

`Tag` 从产生到落入 frame 的完整旅程：

```
realize 阶段
   │  为每个 locatable 元素在 children 里插入 TagElem::packed(Tag::Start(...))
   │  以及对应的 Tag::End(...)
   ▼
flow/collect.rs: Collector
   │  run_block / run_inline 遇到 TagElem → push(Child::Tag(&elem.tag))
   │  （run_inline 还会用 split_prefix_suffix 把段落首尾的 tag 剥出来）
   ▼
flow/distribute.rs: Distributor
   │  child(Child::Tag(tag)) → tag(tag) → work.tags.push(tag)  （暂存）
   │  flush_tags() → items.extend(... Item::Tag) → 放到当前 y 位置
   ▼
flow/distribute.rs 输出阶段
   │  Item::Tag(tag) → output.push(pos, FrameItem::Tag(tag.clone()))
   ▼
Frame 里出现了 FrameItem::Tag  ←  排版产物
```

要点：tag **不占布局空间**，但它在 frame items 序列里的**位置（pos）**就是元素的位置——introspector 正是靠这个 pos 来回答「这个元素在第几页、坐标多少」。

#### 4.2.3 源码精读

先看 `Tag` 与 `TagFlags` 的定义：

[crates/typst-library/src/introspection/tag.rs:11-24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L11-L24) —— `Tag::Start` 持有 `Content` + `TagFlags`；`Tag::End` 持有 `Location` + key 哈希 + `TagFlags`。注释明确：内容必须有 `Location`，否则后续会 panic。

[crates/typst-library/src/introspection/tag.rs:46-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L46-L61) —— `TagFlags` 的 `introspectable`/`tagged` 两个位，以及 `any()` 表示「至少其一为真」。

[crates/typst-library/src/introspection/tag.rs:63-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/tag.rs#L63-L83) —— `TagElem` 是承载 `Tag` 的元素；`packed` 直接产出已 mark_prepared 的内容，跳过准备阶段。`Construct` 实现里 `bail!` 禁止用户手动构造。

再看 typst-layout 这侧，flow 收集器如何把 `TagElem` 转成 `Child::Tag`：

[crates/typst-layout/src/flow/collect.rs:76-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L76-L79) —— 块级收集时，遇到 `TagElem` 就 `push(Child::Tag(&elem.tag))`，其余分支才是真正占空间的元素（间距、段落、块等）。可见 tag 是与普通内容并列的「一等公民」。

行内（段落）场景更讲究：段落首尾可能挂着 tag（整段是一个 locatable 元素，如 `par`），需要先把它们剥出来：

[crates/typst-layout/src/flow/collect.rs:111-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L111-L141) —— `run_inline` 用 `split_prefix_suffix` 把首尾的 `TagElem` 分离，对中间内容做行内排版，再把首部 tag、行、尾部 tag 依次 push 进 output。这保证了段落的 Start/End tag 正好包裹住它的行。

接着看 `Work` 如何暂存待写 tag：

[crates/typst-layout/src/flow/mod.rs:301-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L301-L318) —— `Work` 结构里有一列 `tags: EcoVec<&'a Tag>`（第 313 行），注释说明「将附着到下一个 frame 的待处理标签」。`Work` 是 flow 在多 region 间携带的状态（u4-l1 会详讲），tag 也搭这趟车。

最后看 `Distributor` 如何把 tag 落进 frame：

[crates/typst-layout/src/flow/distribute.rs:157-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L157-L169) —— `tag()` 只是把 tag 暂存进 `work.tags`；`flush_tags()` 在合适的时机把暂存的 tag 转成 `Item::Tag` 放进 items（并清空暂存）。这种「先攒后冲」的设计，是为了让连续多个 tag 落在**同一个 y 位置**。

[crates/typst-layout/src/flow/distribute.rs:610-616](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L610-L616) —— 输出阶段，`Item::Tag(tag)` 被换成 `output.push(pos, FrameItem::Tag(tag.clone()))`，其中 `pos` 的 y 由 `offset + ruler.position(free)` 决定。到这一步，tag 真正成为 frame 的一员。

#### 4.2.4 代码实践

**实践目标**：确认 tag「不占空间但占序列位置」这一性质。

**操作步骤**（源码阅读 + 本地可选验证）：

1. 阅读 [crates/typst-layout/src/flow/distribute.rs:608-616](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L608-L616)，注意 `Item::Tag` 分支**只**调用 `output.push(pos, ...)`，不像 `Item::Abs`/`Item::Fr` 那样改动 `offset`。
2. 推论：连续多个 tag 会被 push 到**同一个 `pos`**，互不挤压后续内容。

**需要观察的现象 / 预期结果**：

- 一个段落同时被 label 和 locatable 命中时，Start tag 与 End tag 都在段落起始/结束的同一坐标，不改变段落高度。
- introspector 后续读取这些 tag 的 pos 时，能正确定位元素。

> 本地验证（可选）：在 `flush_tags` 内临时 `eprintln!("flush {} tags at offset {:?}", tags.len(), offset);`，编译一个含 `#figure(rect()) <fig1>` 的小文档，应看到 flush 了若干 tag 且 offset 不因 tag 而增长。改动仅供调试，结束后还原。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Tag::Start` 持有 `Content`，而 `Tag::End` 不持有？

> **参考答案**：introspector 在 query 时要返回元素本身的内容（如 `query(heading)` 返回 heading 的 Content），所以 Start 必须带上元素。End 只需要标记「这个 Location 的元素到此为止」，用于确定元素在序列里的范围结束位置，带 `Location` 即足够。

**练习 2**：如果一个元素的 `TagFlags.introspectable == false`，它还会进 introspector 吗？

> **参考答案**：不会。`discover_tag` 会检查 `flags.introspectable`（见 4.3.3），只有为真才记录。不过它仍会作为 `FrameItem::Tag` 留在 frame 里——只是不参与查询。

---

### 4.3 PagedIntrospector：从 Frame 反向构建查询索引

#### 4.3.1 概念说明

前面两节解决了「身份」和「把身份写进 frame」。现在反过来：**排版全部结束后**，如何把 frame 里散落的 tag 收集成可被 query 的高效索引？

这就是 `PagedIntrospector` 的工作。关键认知（承接 u1-l4/u3-l5）：introspector 是 `Page` 列表的**纯派生物**，在 `PagedDocument::new` 时一次性构建；它**不参与 `Hash`**（否则每轮迭代哈希都会因 introspector 变化而失效，comemo 缓存就垮了）。它的输入是「已经 finalize 完毕、带有物理页号」的 `&[Page]`，输出是一套可回答下列问题的索引：

- `position(loc)` / `page(loc)`：某元素在第几页、坐标多少。
- `query(selector)`：找出所有满足选择器的元素。
- `query_label(label)` / `query_labelled()`：按 label 查。
- `query_count_before(selector, end)`：某元素之前有几个匹配（counter 的核心）。
- `locator(key, base)`：测量模式下找「最像」的真实元素（见 4.4）。

#### 4.3.2 核心流程

```
PagedIntrospector::new(&[Page])
   │  逐页：记录 numbering/supplement，对 page.frame 调 discover_frame
   ▼
discover_frame(frame, 累积变换 ts, to_pos)
   │  遍历 frame.items()：
   │    Tag(tag)         → discover_tag(tag, pos)         记录 Start/End
   │    Group(group)     → 更新 ts；若 group.parent 存在：
   │                        start_insertion()             暂存当前 sink
   │                        递归 discover_frame(group.frame)
   │                        end_insertion(parent.location) 把这批元素挂到 parent 名下
   │                      若无 parent：直接递归
   │    Link(dest,_)     → 若目标是 Location，记入 frame_link_targets
   │    Text/Shape/Image → 忽略
   ▼
builder.finish() → ElementIntrospector（带 locations/labels/keys 等加速结构）
```

`to_pos` 是个闭包，把 frame 内的局部点 `Point` 包成带页码的 `PagedPosition { page, point }`。

#### 4.3.3 源码精读

`PagedIntrospector::new` 的主循环：

[crates/typst-layout/src/introspect.rs:37-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L37-L58) —— 对每个 page：算出页号 `nr`，收集 `numbering`/`supplement`，调用 `builder.discover_frame(&page.frame, Transform::identity(), ...)`。注意起始变换是单位变换，页内坐标即最终坐标。最后 `builder.finish(...)` 注入页数与每页编号/补语。

`discover_frame` 的核心（tag + group + link 三类处理）：

[crates/typst-layout/src/introspect.rs:177-207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L177-L207) —— 关键是 `FrameItem::Group` 分支：

- 先把 `pos`（group 在父 frame 的位置）与 `group.transform` 累乘进变换 `ts`（`pre_concat`），这样子 frame 内任意点的绝对坐标都能算出来。
- 若 `group.parent` 存在（说明这是一个「跨多帧的逻辑元素」的容器，例如表格单元格、脚注），则 `start_insertion()` → 递归 → `end_insertion(parent.location)`：把递归期间发现的元素**整体记到 parent 名下**，而不是按物理顺序混进主流。
- `FrameItem::Link` 若目标是 `Destination::Location`，把该 location 记入 `frame_link_targets`（用于 PDF 导出时知道哪些位置被链接引用）。

跨到 typst-library 看 `discover_tag` 的记录逻辑：

[crates/typst-library/src/introspection/introspector.rs:513-530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L513-L530) —— `Start` 时检查 `flags.introspectable`，并用 `seen` 去重后 push 一条 `BuilderItem::Start`；`End` 时把 `key → loc` 记入 `keys`（供测量模式 locator 查找），并 push 一条 `BuilderItem::End`。

`start_insertion` / `end_insertion` 这对操作的实现：

[crates/typst-library/src/introspection/introspector.rs:562-574](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L562-L574) —— `start_insertion` 把当前 `sink` 暂存到 `stack` 并换一个空的；`end_insertion(parent)` 把递归期间累积的 sink 内容作为一整批，挂到 `insertions[parent]`。等 `finalize` 时再决定这批元素**插在哪里**。

最终 `visit` 决定插入顺序：

[crates/typst-library/src/introspection/introspector.rs:598-623](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L598-L623) —— 当 `visit` 处理一个 `Start` 时，先把该元素本身写入 `elems`，然后检查 `insertions[loc]`：若有，就把这批「子元素」**紧接着插在它之后**。这正是 u2-l3 提到的「整体插在父元素 start tag 之后」的实现——它纠正了跨多帧元素的内省顺序，使得 `query_count_before` 等「按文档顺序计数」的语义正确。

> 为什么 `PagedIntrospector` 不参与 `Hash`？因为它完全由 `pages` 派生，若让它参与哈希，就会在每次迭代因 introspector 内容变化而改变 `PagedDocument` 的哈希，使依赖它的 comemo 缓存永远 miss。这一点在 u1-l4/u3-l5 已建立，这里只做呼应。

#### 4.3.4 代码实践

**实践目标**：理解 `group.parent` 如何改变元素在内省序列中的位置。

**操作步骤**（源码阅读型）：

1. 对照 [crates/typst-layout/src/introspect.rs:186-198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L186-L198) 与 [crates/typst-library/src/introspection/introspector.rs:562-574](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L562-L574)。
2. 设想一个表格单元格的内容跨了两页（第一页底部 + 第二页顶部）。flow 会用 `FrameParent`/`set_parent` 把这两段内容包进一个带 `parent` 的 group（见 u2-l3 的 Frame 讲解）。

**需要观察的现象 / 预期结果**：

- 没有 parent 机制时：第二页顶部那段单元格内容会**按物理顺序**混进第二页的元素流，于是 `query` 出来的表格单元格顺序会与逻辑顺序不符。
- 有了 `start/end_insertion`：两段内容都被登记到「单元格元素」名下，`visit` 时统一插在该单元格的 start 之后，逻辑顺序得到恢复。

> 本地验证（可选）：在 `end_insertion` 内临时 `eprintln!("insertion under parent={:?}", parent);`，编译一个跨页表格，观察同一 parent 下收集到多条记录。改动仅供调试，结束后还原。

#### 4.3.5 小练习与答案

**练习 1**：`discover_frame` 为什么要维护变换 `ts` 并在进入 group 时 `pre_concat`？

> **参考答案**：group 内的元素坐标是相对 group 自身的，而 introspector 要回答的是「绝对在第几页、坐标多少」。把 group 的位置与自身变换累乘进 `ts`，递归时就能把任意子点的局部坐标换算成页面绝对坐标。

**练习 2**：为什么 `Text`/`Shape`/`Image` 在 `discover_frame` 里被忽略？

> **参考答案**：introspector 只关心「可定位元素」，而可定位性完全由 `Tag` 表达。字形/图形/图片是纯可见内容，不带身份；元素身份信息已经由与之同流的 `Tag` 携带了。

---

### 4.4 migrate_unterminated_tags：分页符与未终止标签的边界处理

#### 4.4.1 概念说明

前三节建立了「身份 → tag → 索引」的主干。但文档级分页引入了一个棘手的边界情形，必须专门处理——这就是 `pages/collect.rs` 里的 `migrate_unterminated_tags`。

问题来自一个极其常见的 show 规则：

```typst
#show heading: it => pagebreak() + it
```

它的语义是「每个标题都从新的一页开始」。realize 展开后，标题的 `Tag::Start` 会排在 `pagebreak()` **之前**（因为 show 规则是 `pagebreak() + it`，`it` 即标题本体在 pagebreak 之后，但标题元素的 start tag 是随 `it` 一起的——而 introspection 关心的「标题起始位置」可能落在 pagebreak 的哪一侧，会直接影响页码归属）。

更一般的问题：当一段内容流里出现「一个 `Tag::Start`，但它的 `Tag::End` 不在同一个 page run 内」——也就是 start tag 在分页符之前，而它对应的可见内容（以及 end tag）在分页符之后。如果听之任之：

- introspector 会把这个元素的 Start 位置记成「分页符之前那一页」，但它实际上下一段可见内容都在「分页符之后那一页」。
- 于是 `heading.page()`（标题在第几页）会返回**错误**的页号，目录页码就错了。

解决办法：**把那些「在分页符之前、且在分页符之前没有匹配 End」的 start tag，迁移到分页符之后。** 这样它们的 Start 位置就落在「真正有内容」的那一页。

> 注意区分两个层面：本节讲的是 **pages/collect.rs**（文档级分页）的迁移；flow 层面（块/列断裂）也有类似的 tag 暂存机制（`Work.tags` 跨 region 续传），但那是 4.2 节的内容。本节聚焦文档级。

#### 4.4.2 核心流程

`collect` 把扁平 children 切成 `Item::Run`（一段连续内容）/`Item::Tags`（纯 tag）/`Item::Parity`（奇偶补页）。在切出一段非分页符的连续内容（Run）前，会调用 `migrate_unterminated_tags`。

`migrate_unterminated_tags` 处理的范围是「分页符两侧的一个窗口」：

```
... [可能存在的尾部 tag 组] | [一串 pagebreak] ...
     ↑ start..mid              ↑ mid..end
```

算法分三步：

1. **确定窗口**：`start` 是「从 mid 往左数连续 tag」的起点；`end` 是「从 mid 往右数连续 pagebreak」的终点。
2. **计算排除集 `excluded`**：在 `start..mid` 这些 tag 里，凡是 `Tag::End(loc)` 出现过的 `loc`，都说明对应的 start 已经终止、**不应**迁移。把这些 loc 收集进 `excluded`。
3. **稳定排序分区**：用一个 key 函数把窗口内每个元素归为三类：
   - `-1`：被排除的 tag（已终止）——留在原处（分页符左侧）；
   - `0`：pagebreak——天然在中间；
   - `1`：要迁移的 tag（未终止的 start）——挪到分页符右侧。

   用**稳定排序**按 key 重排 `children[start..end]`，于是顺序变成：`[已终止 tag(-1)] [pagebreak(0)] [未终止 tag(1)]`。稳定排序保证了同类内部相对顺序不变（这对 introspection 顺序很重要）。

最后返回新的 `end`（重排后「未迁移 tag」之前的边界），让 `collect` 把迁移后的 tag 划进正确的 Run。

#### 4.4.3 源码精读

先看 `collect` 在何处调用迁移，以及 `Item::Tags` 的特殊处理：

[crates/typst-layout/src/pages/collect.rs:68-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L68-L77) —— 找到连续非分页符内容的末尾 `end` 后，立即 `let end = migrate_unterminated_tags(children, end);`。若迁移后 `end == 0`（整段都被搬走了），则 `continue` 跳过。

[crates/typst-layout/src/pages/collect.rs:83-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L83-L101) —— `Item::Tags` 的来历：如果一段内容**全是 tag**，就不应为它单独开一页（因为纯 tag 不影响布局）；这些 tag 会被记成 `Item::Tags`，**插到下一页最开头（甚至在 header 之前）**。注释里还专门排除了「只剩 boundary pagebreak 且仍有 staged 空页」的情况。

`Item` 枚举本身：

[crates/typst-layout/src/pages/collect.rs:8-19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L8-L19) —— `Run`（要并行排版的页内容）、`Tags`（夹在页之间的纯 tag， prepend 到下一页或 append 到末页）、`Parity`（奇偶补页指令，必须串行、在已知物理页号时执行）。

现在看 `migrate_unterminated_tags` 本体：

[crates/typst-layout/src/pages/collect.rs:119-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L119-L127) —— 函数文档注释直接点明了动机：「`show heading: it => pagebreak() + it`」场景——元素在技术上「开始于分页符之前」、但尚无可见内容，我们希望它的位置落在分页符之后。

[crates/typst-layout/src/pages/collect.rs:127-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L127-L143) —— 计算 `start`/`end` 窗口；`excluded` 收集 `start..mid` 范围内所有 `Tag::End(loc)` 的 loc。注意它只看 `Tag::Start(..) => None`（不计入）与 `Tag::End(loc, ..) => Some(loc)`。

[crates/typst-layout/src/pages/collect.rs:144-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L144-L160) —— key 函数把窗口内元素分成 -1/0/1 三类；随后 `children[start..end].sort_by_key(key)` 做**稳定**分区。注释解释了为何用排序而非手写算法：这段不在热路径，稳定排序更不容易出 bug。

[crates/typst-layout/src/pages/collect.rs:162-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L162-L164) —— 计算新的 `end`：从 `start` 起向后数「key == -1」（已终止、留在左侧）的元素个数，得到分界点。

#### 4.4.4 代码实践（本讲核心实践任务）

**实践目标**：用真实例子说清「为什么必须把未终止的 start tag 迁到分页符之后」。

**操作步骤**：

1. 打开 [crates/typst-layout/src/pages/collect.rs:119-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L119-L164)，把函数文档注释里的例子在脑中展开。
2. 设想如下 Typst 源码：
   ```typst
   #show heading: it => pagebreak() + it
   = 标题 A      // 假设它在第 1 页末尾触发 show 规则
   正文……
   ```
   realize 后，内容流大致是（简化）：
   ```
   …(第1页内容)…  [Start(标题A)]  Pagebreak(strong)  [标题A正文]  [End(标题A)] …
   ```
   即：标题 A 的 start tag 出现在 strong pagebreak **之前**，但其本体（及 end tag）在 pagebreak **之后**。
3. 手工模拟 `migrate_unterminated_tags`：
   - `mid` 指向 `[Start(标题A)]` 与 `Pagebreak` 之间。
   - `start..mid` 里只有 `[Start(标题A)]`，没有 `End(标题A)` → `excluded` 为空 → `[Start(标题A)]` 的 key = 1（要迁移）。
   - `mid..end` 是 `[Pagebreak]`，key = 0。
   - 稳定排序后窗口变成：`[Pagebreak(0)] [Start(标题A)(1)]`——start tag 被挪到了 pagebreak 之后。
4. 推论后果：
   - **迁移前**：`Start(标题A)` 在第 1 页 → introspector 认为标题 A 在第 1 页 → 目录页码错。
   - **迁移后**：`Start(标题A)` 在第 2 页（与正文同页）→ `heading.page()` 返回 2 → 目录页码正确。

**需要观察的现象 / 预期结果**：

- 迁移后，标题 A 的 start tag 与它的可见内容落在同一页 run，introspector 给出的页号与肉眼一致。
- 如果 start tag 在分页符之前**已经**有对应的 end tag（即元素其实在第 1 页内完整结束），`excluded` 会包含它，key = -1，留在原处不被迁移——这正是「只迁未终止的」语义。

> 本地验证（可选）：在 `migrate_unterminated_tags` 的 `sort_by_key` 之后临时加 `eprintln!("migrated window start={start} end={end}");`，用上面的源码编译，观察窗口被重排。改动仅供调试，结束后还原，不要提交。

#### 4.4.5 小练习与答案

**练习 1**：如果 `migrate_unterminated_tags` 改用**非稳定**排序，会出什么问题？

> **参考答案**：非稳定排序会打乱同类元素内部的相对顺序。比如多个未终止的 start tag（key 都是 1）原本有确定的文档顺序，非稳定排序后可能乱序，导致 introspector 看到的元素顺序与文档逻辑顺序不一致，进而影响 `query` 结果与 counter 计数。注释明确写了用「*stable* sort」正是为此。

**练习 2**：`LocatorLink::measure` 进入的「测量模式」与本节的「迁移」是同一回事吗？

> **参考答案**：不是。本节迁移是文档级分页时对 tag **物理位置**的修正；而测量模式（`Resolved::Measure`，见 4.1.3 的 locator.rs）解决的是「用户测量了一段内省内容后，如何把测量期分配的身份与真实文档中的身份对上」——`introspector.locator(key, base)` 用 key 哈希在真实元素里找「最像」的那个（见 [introspector.rs:414-421](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L414-L421)）。两者都是「尽力而为的对齐」，但发生在不同阶段、解决不同问题。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「全链路追踪」任务。

**场景**：给定文档

```typst
#show heading: it => pagebreak() + it

= 引言 <intro>
本节介绍背景。

= 方法 <method>
本节介绍方法。
```

**任务**：以「= 引言」这个 heading 为对象，沿下列检查点逐站说明它的身份与位置是如何被建立、修正、消费的。对每一站，给出对应的源码位置与一句话解释。

1. **身份分配**：flow 在 `layout_fragment_impl` 重建 `SplitLocator`，并在主循环为每个 region 调 `locator.next(&())`。heading 的 `Location` 由分层哈希 + disambiguator 决定。参考 [flow/mod.rs:137-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L137-L138) 与 [locator.rs:263-282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L263-L282)。
2. **tag 生成与流动**：realize 为 heading 插入 `Tag::Start`/`Tag::End`；flow/collect 把它们转成 `Child::Tag`，distribute 攒进 `Work.tags` 再 flush 进 frame。参考 [flow/collect.rs:76-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L76-L79)、[flow/distribute.rs:157-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L157-L169)、[flow/distribute.rs:610-616](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L610-L616)。
3. **分页级修正**：因为 show 规则，引言的 start tag 落在 strong pagebreak 前；`migrate_unterminated_tags` 把它迁到 pagebreak 之后，使页号正确。参考 [pages/collect.rs:119-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/collect.rs#L119-L164)。
4. **索引构建**：`PagedIntrospector::new` 遍历各 page 的 frame，`discover_frame` 经 `discover_tag` 记录 heading 的 Start 位置（含修正后的页号）。参考 [introspect.rs:37-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L37-L58) 与 [introspect.rs:177-207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L177-L207)。
5. **查询回答**：下一轮排版里，`query(heading)` 或 `heading.page()` 通过 introspector 拿到正确结果；若用户在 grid 里测量了该 heading，则走 `locator(key, base)` 的测量匹配。

**交付物**：一张包含上述 5 站的流程图（手绘或文字伪代码均可），并在「= 方法」上重复同样追踪，验证 disambiguator 让两个 heading 得到不同身份。

> 待本地验证：流程图中具体的页号（引言在第几页、方法在第几页）取决于实际编译结果；若需确认，可用 `#context [引言在第 #query(heading).first().page() 页]` 这类写法在 Typst 中实测。

## 6. 本讲小结

- `Location` 是元素的 128 位稳定身份；`Locator`/`SplitLocator` 用「分层哈希 + disambiguator」生成它，兼顾跨迭代匹配、编辑稳定性与可并行性，且无全局可变状态。
- `Locator` 故意不实现 `Clone`，强制在 `split()`（不同内容各自身份）与 `relayout()`（同一内容复用身份/测量）之间显式选择；`LocatorLink` 跨 comemo memoize 边界按需取外层信息以保住缓存命中。
- `Tag`（`Start`/`End` + `TagFlags`）是「不可见但有序」的 `FrameItem`：它像普通内容一样流过 realize→collect→distribute，最终 `output.push(pos, FrameItem::Tag(...))` 落进 frame；`pos` 即元素位置，`introspectable` flag 决定是否进索引。
- `Work.tags` 是 flow 在 region 间携带的「待写标签」队列，`flush_tags` 把连续 tag 批量冲到同一 y 位置，保证 tag 不占空间。
- `PagedIntrospector` 是 `&[Page]` 的纯派生物，由 `discover_frame` 递归遍历 frame、经 `discover_tag` 记录 Start/End、并借助 `group.parent` 的 `start/end_insertion` 把跨多帧的逻辑元素整体插到父元素之后，修正内省顺序；它不参与 `Hash`。
- `migrate_unterminated_tags` 处理文档级分页的边界：用稳定排序把「分页符之前、且无匹配 End」的 start tag 迁到分页符之后，否则 `show heading: it => pagebreak() + it` 这类规则的页号归属会出错。

## 7. 下一步学习建议

- **下一讲 u3-l1（文档布局 layout_document 的实现）**：把本讲的 `pages/collect.rs` 放回 `layout_document_common → realize → layout_pages → PagedDocument::new` 全链路，看 collect 产出的 `Item::Run/Tags/Parity` 如何被并行排版与最终化。
- **承接 u3-l2（页面收集）**：深入 `collect` 的 weak/strong pagebreak、staged empty page、parity 补页等其余分支，补全本讲只聚焦的「迁移」一角。
- **测量模式延伸阅读**：若对 `LocatorLink::measure` 与 `introspector.locator(key, base)` 感兴趣，可继续读 [crates/typst-library/src/introspection/locator.rs:284-341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/locator.rs#L284-L341) 的 `next_location` 与 `MeasureIntrospection`，理解「测量的身份如何尽力对上真实身份」。
- **flow 层的 tag 续传**：u4-l1（flow 总览）会讲 `Work` 状态如何在 region 间流转，届时可回头印证本讲的 `Work.tags` 跨 region 行为。
