# 标题、列表、枚举与术语

## 1. 本讲目标

本讲继续深入「文档模型」（`src/model/`）子系统，讲清四类**结构化正文元素**：标题 `heading`、项目符号列表 `list`、编号列表 `enum`、术语列表 `terms`。

读完本讲后，你应当能够：

- 逐字段读懂 `HeadingElem`：它的 `level`/`depth`/`offset` 三者关系，`numbering`/`supplement`/`outlined`/`bookmarked` 的语义，并说清 **`numbering` 字段如何经 `Count` 能力驱动计数器、再被 `Refable`/`Outlinable` 复用**——这是本讲的主线。
- 解释 `ListElem` 的 `marker` 机制：内容数组按嵌套深度**循环取用**、或用函数按深度动态生成。
- 对比 `EnumElem` 与 `list` 的根本差异：`enum` 没有「自由 marker」，它的「标记」就是**按 `numbering`/`start`/`reversed`/`full` 计算出来的数字**。
- 理解 `TermsElem` 为何只有 `separator` 而无 marker，以及 `term`/`description` 两个必填字段。
- 认识贯穿三类列表的统一抽象：`ListLike`/`ListItemLike` trait，以及它们共享的 `tight`/`spacing`/`indent`/`children` 配置。

本讲承接 u8-l1（`ParElem` 与文档模型入口）、u3-l3（`#[elem]` 宏与字段标注）、u3-l2（能力 vtable）与 u4-l1（样式链与折叠），把「元素的编号能力如何与内省/引用系统衔接」讲透。

> 一个贯穿全讲的判断法：**本 crate（typst-library）只「定义元素 + 归一化配置数据」，真正把列表排成行、把标题号渲染出来的算法都住在 `typst-layout`，运行期经 `Routines` 回调。** 标题号之所以「对」，靠的不是本 crate 的排版算法，而是 `Count`→计数器→`Refable` 这条数据通路。

## 2. 前置知识

学过 u3-l2 / u3-l3 / u4-l1 的读者可跳读本节。

- **字段标注（u3-l3）**：`#[required]`（必填，存进 struct）、`#[default(x)]`（有默认的可设置字段）、`#[fold]`（沿样式链折叠而非覆盖）、`#[ghost]`（不入 struct，只活样式链）、`#[internal]`（不对用户暴露）、`#[synthesized]`（存 `Option<T>`、由 `Synthesize` 填充、不参与相等比较）、`#[variadic]`（收集剩余位置参数为数组）、`#[positional]`（可选位置参数）。本讲的 `depth`/`parents` 是 `#[fold] #[ghost]`，`numbers` 是 `#[internal] #[synthesized]`。

- **能力（Capability，u3-l2）**：`#[elem(...)]` 括号里列的是元素具备的能力 trait。本讲会频繁遇到：
  - `Locatable` / `Tagged`——参与内省、可被打位置标签（标题与三类列表都有）。
  - `Synthesize`——在 realization 前由编译器调用一次，**回填**元素字段（标题用它算 `numbers`、`supplement`）。
  - `ShowSet`——show 时反向注入样式（标题用它按 `level` 缩放字号、加粗）。
  - `Count`——「我能驱动计数器前进」，是**标题号与计数器的接口**。
  - `Refable` / `Outlinable`——「我能被 `@ref` 引用 / 被列入 `outline`」。
  - `LocalName`——「我在不同语言下有本地化名字」（如 "Chapter"/"Kapitel"）。

- **样式链与折叠（u4-l1）**：`StyleChain` 按「最内层优先」查询；`Fold` 要满足结合律。本讲的 `tight` 是个特例——它由**标记语法**决定（空行分隔即非紧凑），且**不能用 `set` 规则覆盖**（后述）。

- **计数器（预告 u9-l2，本讲只需直觉）**：`Counter` 是「随文档位置变化的、上下文相关的」计数状态。`Counter::of(HeadingElem::ELEM)` 就是「标题专用计数器」。它的前进由 `Count::update()` 描述，它当前值由内省器在收敛循环里算出。

## 3. 本讲源码地图

本讲涉及四个主文件与四个支撑文件：

| 文件 | 作用 | 本讲引用的关键定义 |
|------|------|--------------------|
| [src/model/heading.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs) | 标题元素 | `HeadingElem` 及全部字段、`resolve_level`、`Synthesize`/`ShowSet`/`Count`/`Refable`/`Outlinable`/`LocalName` 实现 |
| [src/model/list.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs) | 项目符号列表 | `ListElem`、`ListItem`、`ListMarker`、`ListLike`/`ListItemLike` trait |
| [src/model/enum.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs) | 编号列表 | `EnumElem`、`EnumItem` |
| [src/model/terms.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs) | 术语列表 | `TermsElem`、`TermItem` |
| [src/model/outline.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs) | 目录 | `Outlinable` trait（标题号被它复用） |
| [src/model/reference.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs) | 引用 | `Refable` trait、`Supplement` 枚举 |
| [src/introspection/counter.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs) | 计数器 | `Count` trait、`CounterUpdate`、`with::<dyn Count>()` 的消费点 |
| [src/text/lang.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs) | 本地化名字 | `LocalName` trait |

> 行号与永久链接基于当前 HEAD `146a5832`。如遇代码改动，用 `git log -p -- <文件>` 校准。

---

## 4. 核心概念与源码讲解

### 4.1 HeadingElem：层级、编号与 outline/refable 衔接

#### 4.1.1 概念说明

标题（`heading`）是文档结构的骨架。它有三个相互关联的需求，每个都对应一类能力：

1. **层级**：标题有嵌套深度（章、节、小节）。Typst 区分两个概念——
   - `depth`：**相对**深度，由标记语法决定（`==` 即 depth=2）。
   - `level`：**绝对**深度，默认 `auto`，由 `offset + depth` 算出。
   - `offset`：把相对深度抬升为绝对深度的偏移量（默认 0）。

2. **编号**：标题可以自动编号。但本 crate **不**自己算「1.2.3」，而是声明一个 `numbering` 配置，让**计数器**去算，再在 `Synthesize` 阶段把结果回填进 `numbers` 字段。

3. **可被引用 / 可入目录**：`@intro` 引用、`#outline()` 目录都要复用「标题的编号、标题的正文」。这由 `Refable` / `Outlinable` 两个能力 trait 暴露。

`HeadingElem` 在 [src/model/heading.rs:76-87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L76-L87) 声明了它的能力清单：

```rust
#[elem(
    since = "forever",
    Locatable, Tagged, Synthesize, Count, ShowSet,
    LocalName, Refable, Outlinable
)]
pub struct HeadingElem { ... }
```

注意它**没有**列 `Construct`——所以 `#[elem]` 宏会生成默认构造函数，按声明顺序收集 `#[required]` 的 `body` 与各可设置字段。括号里**有** `Count`/`Refable`/`Outlinable`/`LocalName`，意味着要为 `Packed<HeadingElem>` 手写这些能力的实现。

#### 4.1.2 核心流程：numbering 如何变成标题号

「`set heading(numbering: "1.")` 之后标题号从哪来」是本讲最值得追的一条链路。把它拆成五步：

1. **配置落地**：`numbering: "1."` 经 `set` 规则成为 `HeadingElem::numbering` 字段上的样式（值是 `Option<Numbering>`，`None` 表示不编号）。
2. **计数器前进**：每个带 `numbering` 的标题在 realization 阶段被询问 `Count::update()`，它返回 `CounterUpdate::Step(level)`。计数器 machinery 遍历匹配元素、套用这些 `Step`，得到「每个位置上的计数器状态」。
3. **回填 numbers**：`Synthesize` 用 `self.counter().display_at(engine, location, styles, numbering, span)` 把「当前标题位置的计数器值」按 `numbering` 模式格式化成字符串，写入内部字段 `numbers`（专供 PDF 书签使用）。
4. **引用复用**：`@ref` 经 `Refable::counter()` / `numbering()` / `supplement()` 拿到**同一个计数器与同一个编号模式**，在引用处重新 `display_at` 出「标题号」。
5. **目录复用**：`outline` 经 `Outlinable` 查询 `level()` / `outlined()` / `body()` / `prefix()`，逐条生成条目。

关键洞察：第 2 步的 `Step(level)` 是**层级感知**的——计数器在某一级前进时会 `truncate`（截断）更深的级。于是「写一个一级标题」会把二级编号清零，这正是「1.1 → 2」的由来。这条截断逻辑在 `CounterState::step`（计数器单元会细讲）。

#### 4.1.3 源码精读

**层级三字段**——`level` 是 `Smart`（可 `auto`），`depth` 默认为 1，`offset` 默认为 0：

[src/model/heading.rs:106-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L106-L132)

```rust
pub level: Smart<NonZeroUsize>,          // auto => offset + depth
...
#[default(NonZeroUsize::ONE)]
pub depth: NonZeroUsize,                  // 相对深度，由 == 语法设置
...
#[default(0)]
pub offset: usize,                        // 抬升偏移
```

`resolve_level` 把「`auto` 的 level」落定为 `offset + depth`：

[src/model/heading.rs:240-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L240-L245)

```rust
pub fn resolve_level(&self, styles: StyleChain) -> NonZeroUsize {
    self.level.get(styles).unwrap_or_else(|| {
        NonZeroUsize::new(self.offset.get(styles) + self.depth.get(styles).get())
            .expect("overflow to 0 on NoneZeroUsize + usize")
    })
}
```

> 因此 `#set heading(offset: 1)` 后，`= Title`（depth=1）的绝对 level 是 2——它的逻辑层级被整体抬升一格。

**编号与回填字段**——`numbering` 是用户配置，`numbers` 是回填结果：

[src/model/heading.rs:144-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L144-L156)

```rust
pub numbering: Option<Numbering>,         // 用户配置：None = 不编号

/// The resolved plain-text numbers.（仅供 PDF 书签使用）
#[internal]
#[synthesized]
pub numbers: EcoString,                   // 编译期回填
```

`#[synthesized]` 的字段：struct 里存 `Option<T>`、不参与元素相等比较、由 `Synthesize` 阶段填入。`#[internal]` 表示不向用户暴露。

**`Synthesize`：把计数器值格式化成 `numbers`**——

[src/model/heading.rs:248-285](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L248-L285)

```rust
impl Synthesize for Packed<HeadingElem> {
    fn synthesize(&mut self, engine: &mut Engine, styles: StyleChain) -> SourceResult<()> {
        // 1. 解析 supplement：auto => 本地化名字（如 "Chapter"）
        let supplement = match self.supplement.get_ref(styles) {
            Smart::Auto => TextElem::packed(Self::local_name_in(styles)),
            ...
        };

        // 2. 若有 numbering 且元素已有 location，回填 numbers
        if let Some((numbering, location)) =
            self.numbering.get_ref(styles).as_ref().zip(self.location())
            && let Ok(numbers) = self.counter().display_at(
                engine, location, styles, numbering, self.span(),
            )
        {
            self.numbers = Some(numbers.plain_text());
        }

        // 3. 把 level 与 supplement 落定为 Custom
        elem.level.set(Smart::Custom(elem.resolve_level(styles)));
        elem.supplement.set(Smart::Custom(...));
        Ok(())
    }
}
```

注意两个细节：(a) `zip(self.location())`——只有拿到了文档位置（location 由排版阶段回填）才能查计数器，这正是「计数器值是上下文相关的」的体现；(b) 注释指出这里**故意不**在错误时提前返回（见 issue #7428），以保证 show 规则的错误处理一致性。

**`Count`：标题驱动计数器前进**——这是「numbering ↔ 计数器」的接口：

[src/model/heading.rs:311-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L311-L318)

```rust
impl Count for Packed<HeadingElem> {
    fn update(&self) -> Option<CounterUpdate> {
        self.numbering
            .get_ref(StyleChain::default())
            .is_some()                              // 没配 numbering 就不前进
            .then(|| CounterUpdate::Step(self.resolve_level(StyleChain::default())))
    }
}
```

只有配了 `numbering` 的标题才返回 `Step(level)`；否则返回 `None`，计数器对其视而不见。

**计数器侧如何消费 `Count`**——在 [src/introspection/counter.rs:938-959](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L938-L959)，遍历匹配元素时查询能力：

```rust
for elem in introspector.query(selector) {
    ...
    if let Some(update) = match elem.with::<dyn Count>() {
        Some(countable) => countable.update(),       // 标题走这条：Step(level)
        None => Some(CounterUpdate::Step(NonZeroUsize::ONE)),  // 普通可数元素默认 step 1
    } {
        current.update(&mut engine, update)?;        // 套用到计数器状态
    }
    stops.push((current.clone(), page));
}
```

`with::<dyn Count>()` 即能力查询（u3-l2 讲过的 `can`/`with` 机制）：能查到就走元素自己的 `update()`，查不到就默认 `Step(1)`。这就是「`Counter::of(HeadingElem::ELEM)` 会随标题前进」的完整闭环。

**`Refable` 与 `Outlinable`：把计数器借给引用和目录**——

[src/model/heading.rs:320-354](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L320-L354)

```rust
impl Refable for Packed<HeadingElem> {
    fn supplement(&self) -> Content { /* synthesis 后的本地化名 */ }
    fn counter(&self) -> Counter { Counter::of(HeadingElem::ELEM) }  // 关键：标题专用计数器
    fn numbering(&self) -> Option<&Numbering> { self.numbering... }
}

impl Outlinable for Packed<HeadingElem> {
    fn outlined(&self) -> bool { self.outlined.get(...) }   // 是否进目录
    fn level(&self) -> NonZeroUsize { self.resolve_level(...) }
    fn prefix(&self, numbers: Content) -> Content { numbers } // 标题前缀就是编号本身
    fn body(&self) -> Content { self.body.clone() }
}
```

`counter()` 返回 `Counter::of(HeadingElem::ELEM)`——这是**与 `Count` 步进的是同一个计数器**。于是 `@ref`、`#outline()`、`Synthesize` 三者读的都是同一个真相源，编号绝不会不一致。

`Outlinable` 与 `Refable` 的关系定义在 [src/model/outline.rs:451-466](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L451-L466)：`Outlinable: Refable`，即「能进目录」必先「能被引用」。

**`LocalName`：本地化的「标题」名字**——

[src/model/heading.rs:356-358](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L356-L358)

```rust
impl LocalName for Packed<HeadingElem> {
    const KEY: &'static str = "heading";
}
```

`KEY` 是翻译表里的查找键。`LocalName` trait 本身在 [src/text/lang.rs:615-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/lang.rs#L615-L632)，它从样式链读 `TextElem::lang`/`region`，先查「语言+地区」包，再退到「仅语言」，最后退到英语（`localized_str` 是 `#[comemo::memoize]` 的，见 u12-l2）。这就是 `supplement: auto` 时标题补语（"Chapter"/"Kapitel"）的来源。

**`ShowSet`：按层级缩放字号**——

[src/model/heading.rs:288-308](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L288-L308) 在 show 时按 `level` 给 1.4 / 1.2 / 1.0 倍字号、加粗、上下间距，并把 `BlockElem::sticky` 设为 `true`（防止标题成为「孤行」留在页底）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`numbering` 字段 → `Count::Step` → 计数器 → `Refable` 复用」这条链路。

**操作步骤**：

1. 在仓库根写一个最小 Typst 文件 `demo.typ`：
   ```typ
   #set heading(numbering: "1.a)")
   = 引言 <intro>
   这里是一级标题。
   == 背景
   二级标题会被截断重置吗？看编号。
   = 方法
   再次一级，编号应是 2。
   引用 @intro 会显示：#ref(<intro>)。
   ```
2. 用 Typst 编译并查看输出（CLI：`typst compile demo.typ`；或本地 IDE 预览）。
3. 在源码侧打开 [src/model/heading.rs:311-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L311-L318)，在 `Count::update` 的返回处加一行临时日志（仅用于学习，**不要提交**）：`eprintln!("heading step at level {:?}", self.resolve_level(StyleChain::default()));`。

**需要观察的现象**：

- 一级「引言」编号 `1`，其下「背景」编号 `1.a`；紧接着的「方法」编号 `2`（说明计数器在回到一级时截断了 `a` 子级）。
- `ref(<intro>)` 显示的编号与标题自身的编号一致（证明二者共用同一计数器）。
- 若把 `#set heading(numbering: ...)` 删掉，标题不再有编号，`ref` 会报「cannot reference heading without numbering」。

**预期结果**：编号呈现 `1`、`1.a`、`2` 的层级截断行为，引用号与标题号一致。若你无法本地编译，可改为「源码阅读型」：在 [src/model/reference.rs:300-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L300-L323) 跟踪 `RefElem` 如何调用 `refable.counter()` 与 `numbering()`，确认它取的就是 `Counter::of(HeadingElem::ELEM)`。

> 待本地验证：第 3 步加日志的具体输出行数取决于编译遍数（收敛循环会多轮），属正常现象（u9-l3 会讲）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HeadingElem` 同时需要 `depth` 和 `level` 两个字段，而不是只留一个？

> **答案**：`depth` 是**相对**深度，由标记语法 `==`/`===` 决定，表达「这条标题在作者书写层次里的位置」；`level` 是**绝对**深度，可被 `offset` 抬升，用于把整组标题在逻辑层级上整体平移（如把模板里的 `=` 当作二级）。`resolve_level` 用 `offset + depth` 把相对落成绝对，让两者各司其职。

**练习 2**：如果把一个标题的 `outlined` 设为 `false`，它还会被 `@ref` 引用到吗？为什么？

> **答案**：会。`outlined` 只控制是否进入 `outline` 目录（`Outlinable::outlined()`），与 `Refable` 无关。引用能力由 `Refable` 提供，二者独立——`Outlinable: Refable` 是「目录能力依赖引用能力」，不是反过来。

---

### 4.2 ListElem：项目符号与 marker 机制

#### 4.2.1 概念说明

`list`（项目符号列表）把一组条目竖向排列，每条前面放一个**标记**（marker）。它的核心机制是 **marker 如何随嵌套深度变化**：

- 可以传**单个内容**（所有层级用同一个标记）。
- 可以传**内容数组**（按深度循环取用，超长则回绕）。
- 可以传**函数**（输入深度，输出任意内容）。

这正是 `ListMarker` 枚举的两个变体 `Content(Vec<Content>)` 与 `Func(Func)` 要表达的。

`ListElem` 在 [src/model/list.rs:43-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L43-L44) 声明，带 `scope`（因为它有子元素 `list.item`），能力是 `Locatable, Tagged`。注意它**没有** `Synthesize`/`Count`/`Refable`——列表不参与编号与引用。

#### 4.2.2 核心流程

`marker` 从用户输入到「画在条目前」的流程：

1. 用户写 `#set list(marker: ([•], [--]))` 或 `- 条目`。
2. `cast!` 把输入归一化为 `ListMarker`：单个 `Content` → `Content(vec![v])`；`Array` → `Content(...)`（空数组报错）；`Func` → `Func(v)`。
3. 排版时对每个条目，按其**嵌套深度**调 `ListMarker::resolve(engine, styles, depth)`：
   - `Content` 变体：`list.get(depth % list.len())`，即**取模循环**。
   - `Func` 变体：`func.call(engine, ctx, [depth])`，由函数决定。
4. `depth` 本身是 `#[fold] #[ghost]` 字段——嵌套时内层 list 把 `Depth(+1)` 折叠进样式链，marker 据此选下一档。

#### 4.2.3 源码精读

**marker 默认值是三个符号的数组**（按深度循环）：

[src/model/list.rs:87-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L87-L95)

```rust
#[default(ListMarker::Content(vec![
    TextElem::packed('\u{2022}'), // Bullet
    TextElem::packed('\u{2023}'), // Triangular Bullet
    TextElem::packed('\u{2013}'), // En-dash
]))]
pub marker: ListMarker,
```

所以默认的嵌套列表会依次显示 `•`、`‣`、`–`，第四层回到 `•`。

**嵌套深度是折叠幽灵字段**：

[src/model/list.rs:152-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L152-L156)

```rust
/// The nesting depth.
#[internal]
#[fold]
#[ghost]
pub depth: Depth,
```

`Depth(pub usize)`（定义在 [src/foundations/styles.rs:967](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L967)）是 newtype。`#[fold]` 让多层 `Depth` 沿样式链相加（默认 fold 对数字是累加），`#[ghost]` 让它不入 struct 只活样式链，`#[internal]` 使其对用户不可见——它是「给布局算法看的内部信号」。

**`ListMarker::resolve`：取模循环 or 函数调用**：

[src/model/list.rs:186-202](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L186-L202)

```rust
pub fn resolve(
    &self, engine: &mut Engine, styles: StyleChain, depth: usize,
) -> SourceResult<Content> {
    Ok(match self {
        Self::Content(list) => {
            list.get(depth % list.len()).cloned().unwrap_or_default()  // 循环
        }
        Self::Func(func) => func
            .call(engine, Context::new(None, Some(styles)).track(), [depth])?
            .display(),
    })
}
```

`depth % list.len()` 是循环取用的关键。`unwrap_or_default()` 是双保险（`cast!` 已保证数组非空，见下）。

**`cast!`：把杂糅输入归一化为 `ListMarker`**：

[src/model/list.rs:204-222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L204-L222)

```rust
cast! {
    ListMarker,
    self => match self { ... },                 // 序列化：单元素数组降级为单个值
    v: Content => Self::Content(vec![v]),       // 单内容
    array: Array => {
        if array.is_empty() { bail!("array must contain at least one marker"); }
        Self::Content(array.into_iter().map(Value::display).collect())
    },
    v: Func => Self::Func(v),                   // 函数
}
```

空数组被显式拒绝——这保证 `resolve` 里 `list.len()` 恒 ≥ 1，故 `%` 不会除零。

**子元素 `ListItem`**：只有 `body`，且 `cast!` 允许裸 `Content` 自动转成 item：

[src/model/list.rs:166-176](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L166-L176)

```rust
#[elem(name = "item", ...)]
pub struct ListItem {
    #[required]
    pub body: Content,
}

cast! {
    ListItem,
    v: Content => v.unpack::<Self>().unwrap_or_else(Self::new)  // 裸内容 => item
}
```

`#[scope]` + `#[elem] type ListItem;`（[src/model/list.rs:159-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L159-L163)）把它挂成 `list.item`（u3-l4 讲过 `#[scope]` 与子函数）。

#### 4.2.4 代码实践

**实践目标**：观察 marker 在嵌套深度下的循环与函数两种模式。

**操作步骤**：

1. 写 `demo.typ`：
   ```typ
   #set list(marker: ([•], [◦], [–]))
   - 一层
     - 二层
       - 三层
         - 四层（应回到 •）
   
   #set list(marker: n => numbering("i.", n + 1))
   - 顶层（函数 marker，深度 0）
     - 嵌套（深度 1）
   ```
2. 编译查看。
3. 对照 [src/model/list.rs:194-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L194-L195)，确认四层 marker 因 `depth % 3 == 0` 回到首个符号。

**需要观察的现象**：前四层依次 `• ◦ – •`；函数 marker 模式下，条目前显示 `ii.`、`iii.`（深度 0→`ii`，深度 1→`iii`）。

**预期结果**：内容数组模式按取模循环；函数模式按深度动态生成。若无法编译，改为阅读 `ListMarker::resolve` 追踪：传 `marker: ([•], [◦], [–])`、`depth=3` 时 `list.get(3 % 3)=list.get(0)=•`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `depth` 用 `#[fold]` 而不是普通 `#[default]`？

> **答案**：嵌套列表会让「深度」累加——外层 `Depth(0)` + 内层贡献 `Depth(+1)` + 再内层 `Depth(+1)` …… 这是「多层样式相加」的折叠语义，而非「最内层覆盖外层」的覆盖语义。`#[fold]`（对 `usize` 默认求和）正是为此。

**练习 2**：`marker: ([])`（空数组）会发生什么？

> **答案**：报错 `array must contain at least one marker`。`cast!` 在 [src/model/list.rs:216-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L216-L218) 显式拦截空数组，避免 `resolve` 中 `depth % list.len()` 触发除零。

---

### 4.3 EnumElem：编号枚举

#### 4.3.1 概念说明

`enum`（编号列表）与 `list` 在「竖向排列条目」上同构，但**关键区别在于标记**：`enum` 的「标记」不是一个自由的内容/函数，而是**一个按规则计算出来的数字**。控制这个数字的字段是：

- `numbering: Numbering`——编号模式（模式串如 `"1."`、`"a)"`，或函数）。默认 `"1."`。
- `start: Smart<u64>`——起始编号（`auto` 则接续上一级或从 1 开始）。
- `reversed: bool`——是否倒序编号。
- `full: bool`——是否显示「父级 + 自身」的完整编号（如 `1.a`）。
- `number_align: Alignment`——数字的对齐（默认 `end`，即靠尾对齐让数字向右生长）。

此外，`EnumItem` 比 `ListItem` 多一个 `number: Smart<u64>` 字段——允许**单条手动指定编号**（如 `5. 第五步`）。

> 与 `list` 的根本差异：`list` 的 marker 是「装饰」，`enum` 的 marker 是「数据」（一个有语义的序号）。所以 `enum` 没有叫 `marker` 的字段，而是把编号生成参数化。

#### 4.3.2 核心流程

`enum` 编号的生成流程（本 crate 只定义配置，真正算数字在 `typst-layout`）：

1. 用户配置 `numbering`/`start`/`reversed`/`full`，或用 `+`/`数字.` 语法。
2. 嵌套时，父级的编号通过 `parents: SmallVec<[u64;4]>` 这个 `#[fold] #[ghost]` 字段向上累积（供 `full` 模式拼接成 `1.a`）。
3. 单条可用 `EnumItem::number` 手动覆盖序号。
4. `typst-layout` 在排版时综合 `start`、累计偏移、`reversed`、`parents`、单条 `number`，套用 `numbering.apply(...)` 渲染出序号字符串。
5. `number_align` 决定序号在条目左侧的水平锚点。

#### 4.3.3 源码精读

**编号模式与控制字段**：

[src/model/enum.rs:92-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L92-L112)

```rust
#[default(Numbering::Pattern(NumberingPattern::from_str("1.").unwrap()))]
pub numbering: Numbering,

...
pub start: Smart<u64>,        // auto = 接续
...
#[default(false)]
pub full: bool,               // 是否显示父级编号
...
#[default(false)]
pub reversed: bool,
```

`Numbering` 枚举（[src/model/numbering.rs:99-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L99-L104)）有 `Pattern(NumberingPattern)` 与 `Func(Func)` 两变体——与 `ListMarker` 的两变体结构同构，只是语义不同（`Numbering` 吃「一串数字」产内容，`ListMarker` 吃「深度」产内容）。模式串里的多个计数符号按级应用到嵌套 enum（`"1.a)"` 一级用阿拉伯、二级用字母）。

**父级编号累积字段**：

[src/model/enum.rs:215-219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L215-L219)

```rust
/// The numbers of parent items.
#[internal]
#[fold]
#[ghost]
pub parents: SmallVec<[u64; 4]>,
```

与 `list` 的 `depth` 同为折叠幽灵字段，但这里累积的是**父级序号数组**（`SmallVec<[u64;4]>` 栈上容纳 4 级，避免常见深度的堆分配）。`full: true` 时用它拼出完整路径。

**单条手动编号**：

[src/model/enum.rs:229-238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L229-L238)

```rust
#[elem(name = "item", ...)]
pub struct EnumItem {
    #[positional]
    pub number: Smart<u64>,     // 手动指定；auto = 自动
    #[required]
    pub body: Content,
}
```

`#[positional]` 表示可选位置参数。`5. 第五步` 这种语法会把 `5` 填进 `number`。注意它是 `Smart<u64>`——`auto` 时退回自动编号。

**统一骨架 `ListLike`**：

[src/model/enum.rs:245-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L245-L251) 与 [src/model/enum.rs:253-258](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L253-L258) 让 `EnumElem`/`EnumItem` 实现 `ListLike`/`ListItemLike`（详见 4.5）。

#### 4.3.4 代码实践

**实践目标**：对比 `numbering`/`start`/`reversed`/`full` 对序号的影响。

**操作步骤**：

1. 写 `demo.typ`：
   ```typ
   #set enum(numbering: "1.a)", full: true)
   + 准备
     + 烧水
     + 加料
   + 开吃
   
   #set enum(reversed: true, start: 3)
   + 咖啡
   + 茶
   + 牛奶
   ```
2. 编译查看。
3. 在源码侧打开 [src/model/enum.rs:111-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L111-L112) 确认默认 `numbering` 是 `"1."`。

**需要观察的现象**：

- `full: true` 时外层显示 `1`、`2`，内层显示 `1.a`、`1.b`（父级序号被 `parents` 累积并拼接）。
- `reversed: true, start: 3` 时三条编号为 `5`、`4`、`3`（倒序，从 start 起算）。

**预期结果**：见上。本 crate 不实现「数字怎么算」——那个算法在 `typst-layout` 的列表布局里。若想从源码验证行为，可在 `typst-layout` 中搜索 `EnumItem`/`parents` 的消费点（属行为 crate，本讲不展开）。

> 待本地验证：`reversed + start` 的确切数字取决于布局侧实现；以实际编译输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`enum` 为什么没有像 `list` 那样的 `marker` 字段？

> **答案**：`enum` 的「标记」语义上是一个**序号**，由 `numbering`/`start`/`reversed`/`full` 与嵌套结构共同计算得出，不是任意的装饰内容。把它建模成「可计算的数据」而非「自由内容」，才能支持 `full`（拼父级）、`reversed`（倒序）、`number_align`（数字对齐）等序号特有行为。

**练习 2**：`EnumItem::number` 是 `Smart<u64>`。`auto` 和显式数字分别意味着什么？

> **答案**：`auto` 表示「由布局侧按 `start`/累计偏移/`reversed` 自动算」；显式数字（如 `5.`）表示「本条手动指定序号」，布局侧以此覆盖自动值。`#[positional]` 让它可作为位置参数传入。

---

### 4.4 TermsElem：术语列表

#### 4.4.1 概念说明

`terms`（术语列表）用于「词条 + 描述」对，典型场景是词汇表、定义表。它**没有 marker**，取而代之的是：

- `separator: Content`——词条与描述之间的分隔符（默认是一个 0.6em 的弱水平间距 `HElem`）。
- `term` / `description`——`TermItem` 的两个**必填**字段。
- `hanging_indent`——描述的悬挂缩进（默认 2em），让多行描述在视觉上对齐词条之后。

它继承自 `list`/`enum` 的通用配置（`tight`/`indent`/`spacing`/`children`），但「标记位」被「词条 + 分隔符」取代。

#### 4.4.2 核心流程

1. 用户写 `/ 词条: 描述` 或 `#terms((term:[...], description:[...]), ...)`。
2. `TermItem` 持 `term` 与 `description` 两段内容。
3. 排版时把「词条 + separator + 描述」排成一行，描述超出宽度则换行并按 `hanging_indent` 缩进。
4. `within: bool`（`#[ghost]`）标记「当前正处在某个 terms 内部」，供布局侧判断。

#### 4.4.3 源码精读

**分隔符默认是弱水平间距**：

[src/model/terms.rs:50-62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L50-L62)

```rust
/// The separator between the item and the description.
#[default(HElem::new(Em::new(0.6).into()).with_weak(true).pack())]
pub separator: Content,
```

`HElem` 是水平间距元素（u6 讲过），`weak: true` 表示在行首/行尾时自动消失。用户可改为任意内容，如 `#set terms(separator: [: ])`。

**悬挂缩进**：

[src/model/terms.rs:68-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L68-L77)

```rust
/// The hanging indent of the description.
#[default(Em::new(2.0).into())]
pub hanging_indent: Length,
```

这是「描述换行时的缩进」，独立于整条 `indent`。

**`within` 幽灵字段**：

[src/model/terms.rs:101-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L101-L104)

```rust
/// Whether we are currently within a term list.
#[internal]
#[ghost]
pub within: bool,
```

注意它**只有 `#[ghost]` 没有 `#[fold]`**——是个布尔开关而非累加值，仅给布局侧读取。

**`TermItem`：两个必填字段**：

[src/model/terms.rs:114-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L114-L123)

```rust
#[elem(name = "item", ...)]
pub struct TermItem {
    #[required]
    pub term: Content,
    #[required]
    pub description: Content,
}
```

`cast!`（[src/model/terms.rs:125-128](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L125-L128)）要求传入的是 term item 或数组，无法像 `ListItem`/`EnumItem` 那样从裸 `Content` 自动转——因为词条必须有「词 + 描述」两段。

**`ListItemLike` 把样式同时套到 term 与 description**：

[src/model/terms.rs:138-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L138-L144)

```rust
impl ListItemLike for TermItem {
    fn styled(mut item: Packed<Self>, styles: Styles) -> Packed<Self> {
        item.term.style_in_place(styles.clone());
        item.description.style_in_place(styles);
        item
    }
}
```

对比 `ListItem::styled`（[src/model/list.rs:247-252](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L247-L252)）只套 `body` 一个字段——term item 有两段内容，故各套一次。

#### 4.4.4 代码实践

**实践目标**：体验 `separator` 与 `hanging_indent` 的视觉效果。

**操作步骤**：

1. 写 `demo.typ`：
   ```typ
   / Ligature: 两个相邻字形合并为一个。
   / Kerning:   两个相邻字母之间的间距调整，会随字体变化。
   
   #set terms(separator: [ → ], hanging-indent: 0pt)
   / 短词: 描述。
   / 另一词: 这个 terms 没有悬挂缩进。
   ```
2. 编译查看。

**需要观察的现象**：第一组中 `Kerning` 的描述换行后会缩进对齐到词条之后（默认 2em）；第二组把分隔符换成 `→` 且关闭悬挂缩进。

**预期结果**：见上。可在 [src/model/terms.rs:61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L61) 确认默认 separator 是 `HElem(0.6em, weak)`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TermItem` 不能像 `ListItem` 那样从裸 `Content` 自动转换？

> **答案**：`ListItem` 只有一段 `body`，裸内容天然就是它；`TermItem` 需要 `term` 与 `description` 两段，单段内容无法无歧义地拆分，故 `cast!` 在 [src/model/terms.rs:125-128](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L125-L128) 要求传入 term item 或数组。

**练习 2**：`within` 与 `list` 的 `depth` 都是 `#[ghost]`，为何前者无 `#[fold]` 而后者有？

> **答案**：`depth` 是「累加的层数」，嵌套越深值越大，需要 fold 求和；`within` 是「是否处于 terms 内」的布尔开关，只需存在性而非累加，故仅 `#[ghost]`。

---

### 4.5 三类列表的共同骨架：ListLike / ListItemLike 与 marker 对比

#### 4.5.1 概念说明

`list`/`enum`/`terms` 三者结构高度相似——都有 `tight`、`indent`、`spacing`、`children`（`#[variadic]`）、专属标记语法、子元素 `*.item`。本 crate 用两个 trait 把共性抽出来：

- `ListLike`：描述「一种列表由哪种条目组成、如何由条目列表构造」。
- `ListItemLike`：描述「如何把样式套到条目上」。

这让 `typst-layout` 侧可以用同一套布局算法处理三类列表，只在「标记如何生成」上分叉。

#### 4.5.2 核心流程

三类列表的共性字段与差异点：

| 维度 | `list` | `enum` | `terms` |
|------|--------|--------|---------|
| 标记来源 | `marker`（内容数组循环 / 函数） | 编号（`numbering`/`start`/`reversed`/`full` 计算） | 无标记，用 `separator` 分隔 term/description |
| 嵌套深度字段 | `depth: Depth`（`#[fold] #[ghost]`） | `parents: SmallVec<[u64;4]>`（`#[fold] #[ghost]`） | `within: bool`（`#[ghost]`，无 fold） |
| 条目字段 | `body` | `number?` + `body` | `term` + `description` |
| `tight` | ✓（默认 true） | ✓（默认 true） | ✓（默认 true） |
| `spacing: Smart<Length>` | ✓（auto → tight 用 leading，wide 用 spacing） | ✓（同左） | ✓（同左） |
| 专属标记语法 | `- ` | `+` / `数字.` | `/ 词: 描` |

> 关于 `tight`：它在标记模式下由「条目间是否有空行」决定（紧贴=`true`，空行分隔=`false`），且**不能用 `set` 规则覆盖**——因为这是作者书写意图的一部分，强行覆盖会破坏语义。

#### 4.5.3 源码精读

**`ListLike` trait**：

[src/model/list.rs:224-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L224-L231)

```rust
pub trait ListLike: NativeElement {
    type Item: ListItemLike;
    fn create(children: Vec<Packed<Self::Item>>, tight: bool) -> Self;
}
```

关联类型 `Item` 绑定「这种列表的条目类型」，`create` 由「条目数组 + 紧凑标志」构造列表。三者的实现都极简（[src/model/list.rs:239-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L239-L245)、[src/model/enum.rs:245-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L245-L251)、[src/model/terms.rs:130-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L130-L136)）：都是 `Self::new(children).with_tight(tight)`。

**`ListItemLike` trait**：

[src/model/list.rs:233-237](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L233-L237)

```rust
pub trait ListItemLike: NativeElement {
    fn styled(item: Packed<Self>, styles: Styles) -> Packed<Self>;
}
```

把外层 `set` 的样式注入条目内容。`ListItem`/`EnumItem` 套到 `body`，`TermItem` 套到 `term` + `description`。

**注册总装**——三类列表与标题都在 [src/model/mod.rs:63-68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/mod.rs#L63-L68) 经 `define_elem` 注册到 `Model` 分类：

```rust
global.define_elem::<ListElem>();
global.define_elem::<EnumElem>();
global.define_elem::<TermsElem>();
...
global.define_elem::<HeadingElem>();
```

#### 4.5.4 代码实践

**实践目标**：用 `ListLike` 的统一视角，把三类列表的 marker 行为横向对照。

**操作步骤**：

1. 写一个 `demo.typ`，把三种列表并排写出，并各做一层嵌套：
   ```typ
   - bullet 一层
     - bullet 二层
   
   + enum 一层
     + enum 二层
   
   / term: desc 一层
     / 子词: 这其实是另一个 terms
   ```
2. 编译查看三种「标记」的形态。
3. 打开源码，对照本节表格逐一核对：`list` 的标记来自 [src/model/list.rs:186-202](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/list.rs#L186-L202) 的 `resolve`；`enum` 的标记配置在 [src/model/enum.rs:92-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/enum.rs#L92-L147)；`terms` 无标记、用 [src/model/terms.rs:61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/terms.rs#L61) 的 separator。

**需要观察的现象**：`list` 的嵌套 marker 变形（•→‣）；`enum` 的嵌套编号取决于 `numbering`（多计数符号则按级）；`terms` 嵌套仍是 term/description 对，靠 `hanging_indent` 表达层级。

**预期结果**：三类列表共用 `tight`/`spacing`/`children` 行为，差异只在「标记生成」。

#### 4.5.5 小练习与答案

**练习 1**：`ListLike::create` 为何签名里要带 `tight`？

> **答案**：`tight` 由标记语法决定、不可被 `set` 覆盖，因此它必须在「构造元素」时就定下来，而不能像普通字段那样后补。`create` 在解析标记（把相邻条目归并成一个列表）时就把紧凑性烤进元素实例。

**练习 2**：为什么 `TermItem::styled` 要 `styles.clone()` 而 `ListItem::styled` 不用？

> **答案**：`TermItem` 有 `term` 与 `description` 两段内容，要把同一组样式分别 `style_in_place` 到两处，`style_in_place` 消费 `Styles`，故第一次调用后需 clone 给第二次。`ListItem` 只有一个 `body`，调用一次即可。

---

## 5. 综合实践

设计一个小文档，把本讲四类元素与「编号 → 计数器 → 引用/目录」主线串起来。

**任务**：写一个 `report.typ`，要求：

1. 用 `#set heading(numbering: "1.1")` 开启标题编号，写出至少两级标题（一级 + 二级），其中一条二级标题用 `outlined: false` 隐藏。
2. 用 `#outline()` 生成目录，观察被隐藏的标题是否出现（应不出现）。
3. 在正文中用 `@label` 引用一个标题，确认引用号与标题自身编号一致。
4. 用 `list`（自定义 marker 数组）、`enum`（`numbering: "a)"`、`start: 3`）、`terms`（自定义 `separator`）各写一段。
5. 追踪源码：在 [src/model/heading.rs:311-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L311-L318) 的 `Count::update`、[src/model/heading.rs:320-354](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L320-L354) 的 `Refable`/`Outlinable`、[src/introspection/counter.rs:951-956](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L951-L956) 的 `with::<dyn Count>()` 三处之间画一条箭头，标注数据如何流动。

**验收**：

- 目录正确反映层级与 `outlined`；引用号与标题号一致；三类列表按预期呈现。
- 你能用一句话说清：「标题号为什么对」——因为 `numbering` 驱动 `Count` 步进计数器，`Refable`/`Outlinable`/`Synthesize` 三者读的是同一个 `Counter::of(HeadingElem::ELEM)`。

> 待本地验证：编号的精确呈现依赖 Typst 工具链版本；以实际 `typst compile` 输出为准。

## 6. 本讲小结

- `HeadingElem` 用 `level`/`depth`/`offset` 表达层级，`level` 默认 `auto` 时由 `offset + depth` 落定（`resolve_level`）。
- 标题号来自一条数据链：`numbering` 字段 → `Count::update()` 返回 `Step(level)` → 计数器前进 → `Synthesize` 用 `counter.display_at` 把值回填进 `numbers` → `Refable`/`Outlinable` 复用同一计数器。
- `list` 的 `marker` 是 `ListMarker`（内容数组按深度取模循环，或函数按深度生成），`depth` 是 `#[fold] #[ghost]` 累加字段。
- `enum` 没有「自由 marker」，它的标记是按 `numbering`/`start`/`reversed`/`full` 计算的序号；`parents` 累积父级序号供 `full` 拼接；单条可用 `EnumItem::number` 手动指定。
- `terms` 用 `separator` 分隔 `term`/`description`，无 marker；`within` 是布尔幽灵开关。
- 三类列表共享 `ListLike`/`ListItemLike` 抽象与 `tight`/`spacing`/`children` 配置；`tight` 由标记语法决定、不可被 `set` 覆盖。
- `LocalName` 通过 `KEY` 查翻译表，提供标题补语的本地化名字（"Chapter"/"Kapitel"…）；`Outlinable: Refable` 表明「能进目录」必先「能被引用」。

## 7. 下一步学习建议

- **u8-l3 编号、引用、图表与目录**：将深入 `numbering` 函数与 `NumberingPattern` 的解析（`1.a)` 如何拆成计数符号）、`RefElem` 的完整引用流程、`FigureElem`/`OutlineElem` 的实现。本讲只触及 `Refable`/`Outlinable` 接口，下一讲讲消费端。
- **u9-l1 Location、Locator、Tag 与 query/locate/here**：本讲反复出现的 `location`、`with::<dyn Count>()`、内省查询，其底层机制在「内省与上下文」单元。
- **u9-l2 Counter、State 与 Metadata**：本讲把 `Counter::of(HeadingElem::ELEM)`、`CounterUpdate::Step` 当黑盒用了，下一讲拆开计数器的 `get`/`display`/`step`/`at` 与多级数组语义。
- 建议继续阅读：[src/model/heading.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs) 全文（尤其 `Synthesize`/`ShowSet`）、[src/introspection/counter.rs:920-962](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L920-L962)（计数器如何遍历可数元素）。
