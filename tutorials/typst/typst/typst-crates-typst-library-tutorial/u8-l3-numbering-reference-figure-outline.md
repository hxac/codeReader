# 编号、引用、图表与目录

## 1. 本讲目标

本讲是「文档模型」单元的收口篇，把 u8-l1（Document/ParElem）与 u8-l2（Heading/List/Enum/Terms）建立的「元素 + 归一化配置」主线，延伸到四个彼此咬合的概念：

- **numbering**：把一串数字变成可见文字的规则；
- **ref（引用）**：通过 label 跨处引用某个元素，并自动生成「图 1」「第 3 节」这样的文字；
- **figure（图表）**：把内容包成一个「可编号、可引用、可进目录」的对象；
- **outline（目录）**：把文档里所有可编号对象收集成一张列表。

学完本讲，你应当能够：

1. 读懂 `NumberingPattern` 如何把 `"1.a)"` 这样的字符串解析成「前缀 + 计数符号 + 后缀」，并理解多级编号的重复规则；
2. 解释 `RefElem` 为何必须依赖被引用元素的 `Refable` 能力，以及 `supplement`（补充词）如何被解析；
3. 看懂 `FigureElem` 如何通过 `kind` 自动分轨计数，以及它如何同时实现 `Count`/`Refable`/`Outlinable` 三种能力；
4. 理解 `OutlineElem` 如何用 `target` 选择器收集元素、用 `Outlinable` 能力渲染条目，以及自动缩进为何依赖内省（introspection）。

> 贯穿全讲的主线（与 u8-l2 一致）：**本 crate 只「定义元素 + 归一化配置数据」**，真正的「查询元素位置、按计数器格式化、排版目录条目」等行为都住在行为 crate，运行期经 `Routines` 回调。计数器本身的内部机制（收敛循环、`Counter::display_at`）将在第 9 单元（内省）深入，本讲只把它当作一个「给定位置返回格式化文字」的黑盒来用。

## 2. 前置知识

本讲默认你已掌握以下概念（它们在前置讲义中讲过）：

- **元素与能力（u3-l2/u3-l3）**：元素是类型擦除的 `Content`，能力（capability）用 `with::<dyn Cap>()` 查询并调用；`Packed<E>` 是受检还原具体类型的包装。
- **样式系统（u4）**：`StyleChain` 链式查询、`Smart<T>`（`Auto`/`Custom`）表达「智能默认」、`#[synthesized]` 字段在 `Synthesize` 阶段被回填。
- **Count 能力与计数器（u8-l2）**：标题号通过 `Count::update()` 返回 `CounterUpdate::Step(level)` 驱动计数器，`Synthesize` 阶段用 `counter.display_at` 把值格式化回填。本讲的 figure 走完全相同的机制。
- **`Locatable`/`Tagged`（u3-l2、u9 预告）**：带 `location` 的元素可被 `query` 检索，`label` 是其检索键。

如果你对这些还生疏，建议先回看 u8-l2 的「标题号如何对」一节——本讲的 figure 编号、ref 显示号、outline 前缀号，**读的是同一套计数器真相源**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/model/numbering.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs) | `numbering()` 函数、`Numbering` 枚举、`NumberingPattern` 的解析与应用 |
| [src/model/reference.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs) | `RefElem` 引用元素、`Refable` 能力 trait、`Supplement` 补充词 |
| [src/model/figure.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs) | `FigureElem` 图表、`FigureCaption` 题注、`Figurable` 自动识别 trait |
| [src/model/outline.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs) | `OutlineElem` 目录、`OutlineEntry` 条目、`Outlinable` 能力 trait |
| [src/introspection/counter.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs) | `Count` trait、`CounterKey` 枚举（本讲只引用，第 9 单元精读） |

这四个 model 文件在 [src/model/mod.rs:53-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/mod.rs#L53-L80) 的 `define()` 中统一注册：`FigureElem`/`OutlineElem`/`RefElem` 经 `define_elem`，`numbering` 经 `define_func`，全部归入 `Category::Model`。

---

## 4. 核心概念与源码讲解

### 4.1 numbering 函数与 NumberingPattern

#### 4.1.1 概念说明

「编号」要解决的问题是：给定一串非负整数（比如标题的层级编号 `[1, 2, 1]`），如何把它显示成人能读的文字？答案有两类：

1. **模式串（pattern）**：用一个字符串模板描述格式，如 `"1.a)"` 表示「阿拉伯数字 + 点 + 小写字母 + 右括号」。
2. **函数（function）**：任意一个接收数字、返回 content 的函数，如 `(..nums) => nums.pos().map(str).join(".")`。

Typst 用 `Numbering` 枚举统一这两种形态，使 `set heading(numbering: ...)` 这类 API 既能接受字符串也能接受函数，互不区分。

#### 4.1.2 核心流程

`numbering("1.a)", 1, 2)` 的执行流程：

1. `cast!` 把字符串 `"1.a)"` 经 `FromStr` 解析成 `NumberingPattern { pieces, suffix, trimmed }`。
2. `numbering()` 函数把参数转发给 [`Numbering::apply`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L106-L121)。
3. 对 `Pattern` 变体，调用 [`NumberingPattern::apply`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L170-L182)，内部走 `apply_with`。
4. `apply_with` 用「前缀 + 计数符号」逐段格式化，多余的数字重复最后一段，末尾补 suffix。
5. 对 `Func` 变体，直接把数字作为参数调用该函数。

`NumberingPattern` 的解析（核心在 `FromStr`）可概括为下面这段伪代码：

```
输入: "1.a)"
扫描每个字符 c:
  若 c 是某个计数符号(1/a/A/i/I/α/...) 的简写:
    记录 prefix = c 之前的所有字符
    pieces.push((prefix, 该符号对应的数字系统))
    标记已处理到 c 之后
循环结束:
  suffix = 剩余未处理字符
若 pieces 为空 → 报错 "invalid numbering pattern"
```

#### 4.1.3 源码精读

`Numbering` 是一个二选一枚举，把「模式」与「函数」统一为同一个参数类型：

[numbering.rs:97-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L97-L104) —— `Numbering` 枚举，区分 `Pattern` 与 `Func`。

```rust
pub enum Numbering {
    Pattern(NumberingPattern),
    Func(Func),
}
```

它的 `cast!`（[numbering.rs:138-146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L138-L146)）同时接受字符串和函数：字符串走 `NumberingPattern` 分支（会触发 `parse()`），函数走 `Func` 分支。这就是 `numbering: "1."` 和 `numbering: (..n) => ...` 都合法的根源。

`NumberingPattern` 的数据结构只有三块：

[numbering.rs:157-162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L157-L162) —— `pieces` 是「(前缀, 数字系统)」的有序列表，`suffix` 是末尾后缀，`trimmed` 控制是否去掉首段前缀与末尾后缀（供 ref 调用 `.trimmed()` 用）。

```rust
pub struct NumberingPattern {
    pub pieces: EcoVec<(EcoString, NamedNumeralSystem)>,
    pub suffix: EcoString,
    trimmed: bool,
}
```

解析逻辑在 `FromStr` 里，逐字符扫描：

[numbering.rs:292-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L292-L318) —— 把 `"1.a)"` 切成 `pieces = [("", Arabic), (".", LowerAlpha)]`、`suffix = ")"`。

```rust
for (i, c) in pattern.char_indices() {
    let Some(kind) = NamedNumeralSystem::from_shorthand(c...) else { continue; };
    let prefix = pattern[handled..i].into();
    pieces.push((prefix, kind));
    handled = c.len_utf8() + i;
}
let suffix = pattern[handled..].into();
```

真正的「格式化」在 `apply_with`，分两段循环：

[numbering.rs:188-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L188-L220) —— 第一段循环按 `pieces` 与数字一一对应格式化；第二段循环对「超出 piece 数量的多余数字」重复最后一段（`pieces.last().cycle()`），这正是文档里「more numbers than counting symbols → last symbol with its prefix is repeated」的实现。

```rust
// 第一段：pieces 与 numbers 一一对应
for (i, ((prefix, system), &n)) in self.pieces.iter().zip(&mut numbers).enumerate() {
    if i > 0 || !self.trimmed { fmt.push_str(prefix); }
    write!(fmt, "{}", apply_system(*system, n)?).unwrap();
}
// 第二段：多余数字重复最后一段
for ((prefix, system), &n) in self.pieces.last().into_iter().cycle().zip(numbers) {
    if prefix.is_empty() { fmt.push_str(&self.suffix); } else { fmt.push_str(prefix); }
    write!(fmt, "{}", apply_system(*system, n)?).unwrap();
}
if !self.trimmed { fmt.push_str(&self.suffix); }
```

> 术语：`NamedNumeralSystem` 来自 `codex` crate，代表一种数字系统（Arabic 阿拉伯数字、LowerAlpha 小写字母、Roman 罗马数字等）。`apply_system` 把数字交给它表示；当某个系统无法表示该数（如罗马数字表示 0）时，`apply_system_with_fallback` 会回退到阿拉伯数字并发出一条「将来会变成硬错误」的警告（[numbering.rs:275-290](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L275-L290)）。

另外有个 `apply_kth`（[numbering.rs:223-246](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L223-L246)），它只格式化模式的第 k 段——计数器在「只显示某一级」时会用到。

#### 4.1.4 代码实践

**实践目标**：亲手追踪 `numbering("1.a)", 1, 2)` 的解析与格式化，验证你理解的 `pieces`/`suffix` 结构。

**操作步骤**（源码阅读型）：

1. 打开 [numbering.rs:292-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L292-L318)，按 `from_str("1.a)")` 手动模拟：逐字符判定哪些是 `from_shorthand` 命中的计数符号，写出每一步的 `prefix` 与 `pieces.push`。
2. 再打开 [numbering.rs:188-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L188-L220)，带入 `numbers = [1, 2]`，逐行写出 `fmt` 的累积过程。

**需要观察的现象 / 预期结果**：

- 解析得到 `pieces = [("", Arabic), (".", LowerAlpha)]`，`suffix = ")"`。
- 格式化 `[1, 2]`：第一段循环输出 `"1"` + `".b"`，第二段循环因无多余数字而不执行，末尾补 `")"`，最终 `1.b)`。
- 若改写成 `numbering("1.a)", 1, 2, 3)`（数字多于 piece），第二段循环会用最后一段 `(".", LowerAlpha)` 重复处理多余的 `3`，输出 `1.b.c)`（待本地验证：可在一个最小 typst 文件里写 `#numbering("1.a)", 1, 2, 3)` 编译查看）。

> 提示：若想真的跑起来，可在仓库根目录用 `cargo run --bin typst -- compile input.typ` 编译一个含上述调用的文件；若环境不便，本实践以源码追踪为准，标注「待本地验证」的部分不假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：模式 `"I – 1"` 会被解析成什么样的 `pieces` 和 `suffix`？对 `numbers = [12, 2]` 会输出什么？

**答案**：扫描命中两个符号：`I`（大写罗马）与 `1`（阿拉伯）。`pieces = [("I", Roman/大写), (" – ", Arabic)]`，`suffix = ""`（`" – 1"` 中 `1` 之后无字符）。对 `[12, 2]` 输出 `XII – 2`（罗马数字 12 即 XII）。

**练习 2**：为什么 `Numbering` 要设计成枚举而不是只支持字符串？

**答案**：为了让用户能用任意函数自定义编号（如 unary 计数 `let unary(.., last) = "|" * last`）。枚举让 `set heading(numbering: ...)` 的参数类型统一为 `Numbering`，下游代码（计数器显示）无需关心它是模式还是函数，统一调 `apply` 即可。

---

### 4.2 RefElem：引用如何依赖 Refable 能力

#### 4.2.1 概念说明

`@intro` 这样的引用语法，会被解析成一个 `RefElem`。它要做的事是：拿到一个 label，找到文档里挂这个 label 的元素，然后生成一段「补充词 + 编号」的文字（如「Section 1」「图 3」），并把它变成指向原元素的链接。

这里有个关键约束：**并非任何元素都能被引用**。只有实现了 `Refable` 能力的元素才行——因为它必须能提供「补充词」「计数器」「编号规则」三样东西。`HeadingElem`、`FigureElem`、`EquationElem`、`FootnoteElem` 都实现了 `Refable`；一个普通的 `rect` 就没有，直接引用会报错。

`RefElem` 还支持两种形态（`form` 字段）：

- `Normal`（默认）：生成文字引用（「Section 1」）。
- `Page`：生成页码引用（「第 3 页」），此时不要求元素是 `Refable`，只要所在页面有编号。

#### 4.2.2 核心流程

`@intro`（Normal 形态）的渲染流程：

1. **Synthesize 阶段**：用 `QueryLabelIntrospection` 按 label 查找元素，把结果回填到 `element` 字段（[reference.rs:201-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L201-L244)）。同时构造一个 `CiteElem`（用于参考文献场景）。
2. **Realize 阶段**（[reference.rs:226-325](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L226-L325)）：
   - 若 `form == Page`：取元素 location，查页码编号与页面补充词，交给 `realize_reference`。
   - 若 label 在参考文献里：转成 `CiteElem` 输出。
   - 若是脚注：转成脚注引用。
   - **否则（普通元素）**：`elem.with::<dyn Refable>()` 查能力；拿到 `Refable` 后取其 `counter()`、`numbering()`、`supplement()`，交给 `realize_reference`。
3. **`realize_reference`**（[reference.rs:328-361](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L328-L361)）：`counter.display_at(loc)` 得到编号文字，拼上补充词，包进 `DirectLinkElem`（带超链接与无障碍 alt 文本）。

#### 4.2.3 源码精读

`RefElem` 的元素声明列出了它的能力：

[reference.rs:138-199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L138-L199) —— `#[elem(... Locatable, Tagged, Synthesize)]`，字段有 `target: Label`（必填）、`supplement`、`form`，以及两个 `#[synthesized]` 字段 `citation` 和 `element`。

关键的能力查询发生在 `realize` 里，这是「引用为何依赖 Refable」的直接体现：

[reference.rs:284-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L284-L313) —— 找到元素后，先查 `dyn Refable` 能力；没有就报错（若它只是 `Figurable` 还会给出「放进 figure 里」的提示）。拿到能力后，取 `numbering()`，若没有编号则报「cannot reference ... without numbering」。

```rust
let refable = elem
    .with::<dyn Refable>()
    .ok_or_else(|| { /* cannot reference ... */ })?;
let numbering = refable
    .numbering()
    .ok_or_else(|| { /* cannot reference ... without numbering */ })?;
```

`Refable` 能力 trait 本身只规定三个方法——这正是「可被引用」的契约：

[reference.rs:425-436](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L425-L436) —— 补充词、计数器、编号规则。

```rust
pub trait Refable {
    fn supplement(&self) -> Content;
    fn counter(&self) -> Counter;
    fn numbering(&self) -> Option<&Numbering>;
}
```

`supplement`（补充词）的解析是本讲的另一个重点。它是一个 `Smart<Option<Supplement>>`，三种状态各有含义，在 `realize_reference` 里分派：

[reference.rs:341-345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L341-L345) —— `Auto` 用元素自带的补充词（如 figure 的「Figure」/「图」），`Custom(None)` 表示不要补充词，`Custom(Some)` 用用户指定的（可以是 content，也可以是接收元素作参数的函数）。

```rust
let supplement = match reference.supplement.get_ref(styles) {
    Smart::Auto => supplement,                              // 用元素自己的
    Smart::Custom(None) => Content::empty(),                // 不要
    Smart::Custom(Some(supplement)) => supplement.resolve(engine, styles, [elem])?,  // 用户给定
};
```

`Supplement::resolve`（[reference.rs:388-403](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L388-L403)）对 `Content` 直接克隆，对 `Func` 则把元素作参数调用——这就是 `supplement: it => [Chapter]` 这类动态补充词的工作方式。

最后，生成的文字被包进 `DirectLinkElem` 并带上 alt 文本（`supplement + numbering` 的纯文本），用于无障碍：

[reference.rs:347-361](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L347-L361) —— 拼接 supplement + 不间断空格 + numbers，再 `DirectLinkElem::new(loc, content, Some(alt))`。

#### 4.2.4 代码实践

**实践目标**：验证「引用依赖被引用元素的 Refable 能力与 supplement」。

**操作步骤**（源码阅读型）：

1. 在 [reference.rs:284-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L284-L313) 找到 `with::<dyn Refable>()` 与 `refable.numbering()` 两处 `ok_or_else`，记录它们各自抛出的错误信息。
2. 在 [figure.rs:447-466](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L447-L466) 阅读 `impl Refable for Packed<FigureElem>`，确认 figure 是如何提供 `supplement()`/`counter()`/`numbering()` 三样东西的。
3. 在 [heading.rs:320-336](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/heading.rs#L320-L336) 对比 heading 的 `Refable` 实现，注意它的 `supplement()` 默认返回空（标题通常不需要「Section」前缀）。

**需要观察的现象 / 预期结果**：

- 一个带 label 但**未设置 numbering** 的标题，被 `@` 引用时会触发「cannot reference heading without numbering」，并给出 `#set heading(numbering: "1.")` 的提示（见 [reference.rs:299-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L299-L313)）。
- `@fig[Appendix]` 这种方括号语法会把 `[Appendix]` 设为 `supplement`（`Custom(Some(Content))`），覆盖 figure 自带的「Figure」。
- 对一个 `Figurable` 但非 `Refable` 的元素（如裸 `image`）直接 `@`，会得到「cannot reference image directly, try putting it into a figure」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RefElem` 自己不实现 `Refable`，而是去查被引用元素的 `Refable`？

**答案**：`RefElem` 是「引用」这个动作，它本身没有编号；编号信息属于被引用的对象（标题有标题号、图有图号）。把 `Refable` 实现在具体元素上，让 `RefElem` 通过能力查询去「借用」这些信息，职责分明——同一套 `RefElem` 逻辑可复用于所有可引用元素。

**练习 2**：`form: "page"` 的引用为何不要求元素是 `Refable`？

**答案**：页码引用只需要元素有 `location`（`Locatable`），然后查「该 location 所在页」的页码计数器即可（见 [reference.rs:237-260](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L237-L260)）。它不关心元素自己有没有编号，所以任何带 location 的元素都能被页码引用。

---

### 4.3 FigureElem：可编号对象的容器

#### 4.3.1 概念说明

`figure` 把一段内容（图片、表格、代码等）包装成一个**统一的对象**，让它自动获得三样东西：编号、可被 `@` 引用、可进 `outline` 列表。它的精妙之处在于「自动分轨计数」——图片图和表格图各自独立编号，互不干扰。

这个「分轨」靠 `kind` 字段实现：每个 `kind` 对应一个独立的计数器轨道。`kind` 可以是元素函数（`image`/`table`/`raw`）或自定义字符串（`"atom"`）。当 `kind` 设为 `auto` 时，figure 会去 body 里找第一个实现了 `Figurable` 能力的元素来推断 kind，找不到就默认是 `image`。

`FigureElem` 是本讲里**能力最密集**的元素，它一次性实现了 `Count`、`Refable`、`Outlinable` 三种能力——这分别决定了「它如何被计数」「它如何被引用」「它如何进目录」。

#### 4.3.2 核心流程

一个 `#figure(image(...), caption: [...]) <fig1>` 的处理：

1. **Synthesize**（[figure.rs:343-424](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L343-L424)）：
   - 推断 `kind`：`auto` 时用 `query_first_naive` 在 body 里找 `dyn Figurable` 元素，找到用其函数，否则默认 `image`。
   - 解析 `supplement`：`auto` 时取 kind 对应元素在本语言的 `local_name`（如英文 image → "Figure"，中文 → 「图」）。
   - 构造计数器：`Counter::new(CounterKey::Selector(figure.where(kind => kind)))`——**按 kind 分轨**。
   - 把 kind/supplement/numbering/counter/location 回填进 caption。
2. **Count**（[figure.rs:437-445](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L437-L445)）：若有编号，返回 `CounterUpdate::Step(1)`，让对应 kind 的计数器 +1。
3. **题注渲染**（`FigureCaption::realize`，[figure.rs:599-626](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L599-L626)）：`supplement + 编号 + 分隔符 + body`。分隔符按语言自适应（英文 `": "`、中文全角空格、法文 `". – "`、俄文 `". "`）。

#### 4.3.3 源码精读

`FigureElem` 的能力列表一目了然：

[figure.rs:120-131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L120-L131) —— `#[elem(... Locatable, Tagged, Synthesize, Count, ShowSet, Refable, Outlinable)]`，一元素身兼数职。

按 kind 分轨计数的计数器构造是 figure 的核心设计：

[figure.rs:399-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L399-L402) —— `CounterKey::Selector` 用一个 `where` 选择器作 key，意味着「所有 kind 相同的 figure 共享一个计数器」，不同 kind 各走各的轨道。

```rust
let counter = Counter::new(CounterKey::Selector(
    select_where!(FigureElem, kind => kind.clone()),
));
```

> 术语：`CounterKey`（[counter.rs:527-536](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L527-L536)）是计数器的「键」，三种变体：`Page`（页码）、`Selector`（按选择器分轨，figure 用它）、`Str`（手动计数器）。这正是 `counter(figure.where(kind: table))` 能精确控制表格计数器的根源。

`Count` 能力的实现极其简洁——和 u8-l2 讲过的标题完全同构：

[figure.rs:437-445](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L437-L445) —— 有编号才步进。

```rust
impl Count for Packed<FigureElem> {
    fn update(&self) -> Option<CounterUpdate> {
        self.numbering().is_some().then(|| CounterUpdate::Step(NonZeroUsize::ONE))
    }
}
```

`Refable` 实现把 figure 的三样信息交出去——注意 `counter()` 优先用 synthesize 阶段算好的 `self.counter`，兜底才用 `Counter::of(FigureElem::ELEM)`：

[figure.rs:447-466](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L447-L466)。

`Outlinable` 实现决定 figure 能否进「图表目录」：只有 `outlined && (有题注 || 有编号)` 才算可被收录；它的 `body()` 返回题注正文，`prefix()` 给编号加上补充词：

[figure.rs:468-491](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L468-L491)。

题注的「补充词 + 编号 + 分隔符 + 正文」拼接在 `FigureCaption::realize`：

[figure.rs:604-623](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L604-L623) —— 编号同样来自 `counter.display_at(location)`，分隔符 `resolve_separator` 会按语言取默认值。

```rust
let numbers = counter.display_at(engine, *location, styles, numbering, self.span())?;
realized = supplement + numbers + self.resolve_separator(styles) + realized;
```

语言相关的默认分隔符在 `local_separator_in`，是个 `match Lang`：

[figure.rs:639-649](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L639-L649) —— 这正是「同一段 Typst 代码，换 `lang` 就换题注分隔符」的实现，呼应 u7-l3 讲过的 `LocalName` 机制。

#### 4.3.4 代码实践

**实践目标**：理解 figure 如何按 kind 自动分轨计数，以及它如何复用 `Count`/`Refable`/`Outlinable` 三能力。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [figure.rs:354-360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L354-L360)，确认 `kind` 的 `auto` 推断逻辑：`query_first_naive(&Selector::can::<dyn Figurable>())`——在 body 里找第一个 `Figurable` 元素。
2. 阅读 [figure.rs:399-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L399-L402)，理解为何「图片图与表格图编号互不影响」：它们的 `CounterKey::Selector` 里的 kind 不同，是两条独立轨道。
3. （可选运行）写一个最小 typst 文档：
   ```typ
   #figure(rect[Hi], caption: [A], kind: "atom", supplement: [Atom]) <a>
   #figure(table(columns: 2)[1][2], caption: [B]) <t>
   #figure(image("x.png"), caption: [C]) <i>
   @a / @t / @i
   ```
   预期：三个 figure 因 kind 不同（`"atom"` / `table` / `image`）各自从 1 编号；引用输出类似 `Atom 1 / Table 1 / Figure 1`（待本地验证，需有可用图片或用占位内容）。

**需要观察的现象 / 预期结果**：同一 kind 的多个 figure 编号递增；不同 kind 互不干扰；引用文字由「supplement + 编号」组成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FigureElem` 的 `counter()` 用 `CounterKey::Selector(figure.where(kind => ...))` 而不是直接 `Counter::of(FigureElem::ELEM)`？

**答案**：后者会让所有 figure 共用一个计数器，图片和表格就会混着编号。用 `where(kind => ...)` 选择器作 key，每种 kind 是独立轨道，从而实现「按类别分别计数」。

**练习 2**：`Figurable` trait（[figure.rs:683-686](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L683-L686)）是个空 trait（标记 trait），它的作用是什么？

**答案**：它只用来「标记一个元素可被 figure 自动识别为某种 kind」。synthesize 阶段用 `Selector::can::<dyn Figurable>()` 在 body 里找这种元素，找到就把它的函数当作 kind。`image`/`table`/`raw` 都标记了 `Figurable`，所以放进 figure 能被自动分类。

---

### 4.4 OutlineElem：目录与可编号对象的组织

#### 4.4.1 概念说明

`outline()` 生成一张列表，把文档里所有「可被收录（Outlinable）」的元素收集起来，每条显示「编号/标题 + 引导线 + 页码」。默认 target 是所有标题（`HeadingElem`），但可以改成 `figure.where(kind: image)` 来生成「图片目录」，或 `figure.where(kind: table)` 生成「表格目录」。

`OutlineElem` 自身不实现 `Outlinable`（目录不收录自己），而是**消费**其他元素的 `Outlinable` 能力。它通过 `target` 选择器用内省（`query`）找出所有目标元素，再对每个元素查 `dyn Outlinable` 来取层级、前缀、正文。

本模块还定义了 `Outlinable` 能力 trait——它继承自 `Refable`（`Outlinable: Refable`），因为「能进目录」必然「能被引用」，前者是后者的超集。

#### 4.4.2 核心流程

`#outline()` 的处理：

1. **ShowSet**（[outline.rs:397-409](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L397-L409)）：把目录自己的标题设为「不编号、不收录」，关闭两端对齐，并把自身经 ghost `parent` 字段传给条目。
2. **收集条目**（`realize_iter`，[outline.rs:299-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L299-L318)）：`QueryIntrospection(target)` 查出所有目标元素，对每个查 `dyn Outlinable`，取 `level()` 与 `outlined()`，按 `depth` 过滤，生成 `OutlineEntry`。
3. **可选建树**（`build_tree`，[outline.rs:338-395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L338-L395)）：把扁平列表按层级嵌套成树，处理「跳级」与「被 depth 过滤的祖先」。
4. **渲染条目**（`OutlineEntry` 的 `indented`/`prefix`/`inner`，[outline.rs:524-692](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L524-L692)）：`prefix()` 用元素计数器格式化编号，`inner()` 拼正文 + 引导线 + 页码，`indented()` 负责缩进对齐。

#### 4.4.3 源码精读

`OutlineElem` 的字段很精简——`title`、`target`、`depth`、`indent`：

[outline.rs:150-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L150-L244) —— `target` 默认是 `HeadingElem::ELEM.select()`，是个 `LocatableSelector`（仅接受可定位的选择器）。

条目收集的核心是 `realize_iter`，它体现了「outline 消费 Outlinable 能力」：

[outline.rs:306-317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L306-L317) —— `engine.introspect(QueryIntrospection(target))` 拿到所有目标元素，逐个 `with::<dyn Outlinable>()`，取 level 与 outlined 决定是否收录。

```rust
let elems = engine.introspect(QueryIntrospection(self.target.get_cloned(styles).0, span));
elems.into_iter().map(move |elem| {
    let Some(outlinable) = elem.with::<dyn Outlinable>() else {
        bail!(self.span(), "cannot outline {}", elem.func().name());
    };
    let level = outlinable.level();
    let include = outlinable.outlined() && level <= depth;
    ...
})
```

`Outlinable` 能力 trait 在本文件定义，它继承 `Refable`：

[outline.rs:451-466](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L451-L466) —— `outlined()`（是否收录）、`level()`（层级，默认 1）、`prefix()`（编号前缀）、`body()`（正文）。

```rust
pub trait Outlinable: Refable {
    fn outlined(&self) -> bool;
    fn level(&self) -> NonZeroUsize { NonZeroUsize::ONE }
    fn prefix(&self, numbers: Content) -> Content;
    fn body(&self) -> Content;
}
```

条目的编号前缀同样靠计数器，与 ref 共用同一真相源：

[outline.rs:636-651](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L636-L651) —— `OutlineEntry::prefix` 取元素的 `outlinable.counter()` 与 `numbering()`，调 `display_at(loc)` 格式化，再交给 `outlinable.prefix(numbers)` 拼补充词。

```rust
let numbers = outlinable.counter().display_at(engine, loc, styles, numbering, span)?;
Ok(Some(outlinable.prefix(numbers)))
```

> **关键呼应**：标题号、`@ref` 引用号、`#outline()` 目录前缀号，三者都调「同一个计数器」的 `display_at`，所以它们**永不冲突**——这是 u8-l2 已点明、本讲再次验证的设计。

最有意思的是**自动缩进**（`indent: auto`）。它要让「第 N+1 层条目的正文」与「第 N 层条目的编号」对齐，这需要知道每层编号的宽度——而宽度要排版后才能量出。Typst 的做法是：每个条目在渲染时把自己量得的「编号宽度」存成一个 `PrefixInfo` 元素插入文档，然后通过内省把同 outline 的所有 `PrefixInfo` 收集起来取各层最大值：

[outline.rs:800-822](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L800-L822) —— `compute_auto_indents` 内省查询所有 `PrefixInfo`（用 outline 的 location 作 key 过滤），`determine_prefix_widths` 取每层最大宽度，据此算 base 缩进与 hanging 缩进。

这正是「自动缩进依赖内省、依赖收敛」的根源——第一遍渲染时 `PrefixInfo` 还不全，必须等收敛循环（第 9 单元）多跑几遍才能稳定。条目宽度测量本身经 `routines.layout_frame` 回调到排版 crate（[outline.rs:779-796](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L779-L796)），再次印证「本 crate 只定义与归一化，排版在别处」。

#### 4.4.4 代码实践

**实践目标**：理解 outline 如何用 `target` 选择器与 `Outlinable` 能力收集条目，以及自动缩进为何依赖内省。

**操作步骤**（源码阅读型）：

1. 阅读 [outline.rs:186-187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L186-L187)，确认 `target` 默认值是 `HeadingElem::ELEM.select()`。
2. 阅读 [outline.rs:306-317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L306-L317)，追踪「查询 target → 查 Outlinable 能力 → 取 level/outlined → 过滤 depth」的链路。
3. 阅读 [outline.rs:800-822](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L800-L822) 与 [outline.rs:841-858](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L841-L858)，理解 `PrefixInfo` 如何作为内省的数据载体。

**需要观察的现象 / 预期结果**：

- 改 `target` 为 `figure.where(kind: image)` 即可生成「图片目录」，因为 figure 实现了 `Outlinable`。
- 一个 `outlined: false` 的标题不会出现在目录里（见 `include = outlinable.outlined() && level <= depth`）。
- 自动缩进在首次编译可能跳动，多次编译后稳定——这是收敛循环在起作用（待本地验证：观察 `#outline(indent: auto)` 在含多级长编号标题时的对齐）。

#### 4.4.5 小练习与答案

**练习 1**：`Outlinable` 为什么要继承 `Refable`（`trait Outlinable: Refable`）？

**答案**：进目录需要「编号」（来自 `Refable::numbering/counter`）和「补充词」（来自 `Refable::supplement`），外加目录独有的「层级/正文/是否收录」。继承 `Refable` 让这些共用方法直接复用，并保证「能进目录的元素一定能被引用」这一不变式。

**练习 2**：为什么 outline 的自动缩进（`indent: auto`）需要内省，而固定缩进（`indent: 2em`）不需要？

**答案**：固定缩进只按层级乘一个常量，纯算术即可。自动缩进要让正文与上层编号对齐，必须知道「每层编号渲染后的实际宽度」，而宽度只有排版后才能测得，因此要把测量结果（`PrefixInfo`）写入文档再内省回收——这天然依赖多轮收敛。

---

## 5. 综合实践

设计一个最小 typst 文档，把本讲四个概念串起来，**并用源码知识解释你看到的现象**。

**文档草稿**（存为 `figure-outline-demo.typ`）：

```typ
#set page(numbering: "1")
#set heading(numbering: "1.1.")

// 1. numbering 模式：多级编号
#outline(
  title: [目录],
  indent: auto,
)

= 引言 <intro>
参见 @glacier 与 @data。

#figure(
  image("glacier.jpg", width: 70%),
  caption: [一条冰川。],
) <glacier>

#figure(
  table(columns: 2, [t],[1],[y],[2]),
  caption: [实验数据。],
) <data>

#outline(
  title: [图目录],
  target: figure.where(kind: image),
)
```

**请你完成**：

1. **追踪编号来源**：在 [figure.rs:399-402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L399-L402) 与 [outline.rs:636-651](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/outline.rs#L636-L651) 之间画一条数据流：figure 的 kind 如何决定它进哪个计数器轨道，这个轨道的值如何同时出现在「题注编号」「`@glacier` 引用」「图目录前缀」三处。
2. **解释现象**：为什么 `@glacier` 显示「Figure 1」而 `@data` 显示「Table 1」？（提示：figure 按 kind 分轨，supplement 由 kind 的 `local_name` 决定，见 [figure.rs:362-381](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/figure.rs#L362-L381)。）
3. **验证依赖**：删掉 `#set heading(numbering: "1.1.")`，重新编译，观察 `@intro` 是否报错，并用 [reference.rs:299-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L299-L313) 解释原因。
4. **观察收敛**：把 `indent: auto` 改成 `indent: 2em` 再改回，观察自动缩进下目录条目的对齐是否需要多次编译才稳定（这是第 9 单元收敛循环的直观预演）。

> 若本地无 `glacier.jpg`，可用 `rect(width: 70%, height: 3cm)` 等占位内容替代 `image(...)`，但那样 figure 的 kind 会变成默认 `image`（因为没有 `Figurable` 元素可识别）——这本身就是一个值得记录的观察。

## 6. 本讲小结

- **numbering** 用 `Numbering` 枚举统一「模式串」与「函数」两种形态；`NumberingPattern` 经 `FromStr` 解析成 `pieces`（前缀+数字系统）与 `suffix`，多余数字重复最后一段，`trimmed` 用于去掉首尾装饰供 ref 复用。
- **RefElem** 通过 label 查到元素后，**必须**查到 `dyn Refable` 能力才能生成文字引用——`Refable` 提供 supplement/counter/numbering 三样契约；`supplement` 是 `Smart<Option<Supplement>>`，三态分别表示「用元素自带的/不要/用户给定」。
- **FigureElem** 是能力最密集的元素，同时实现 `Count`/`Refable`/`Outlinable`；它用 `CounterKey::Selector(figure.where(kind => ...))` **按 kind 分轨计数**，使图片、表格、自定义类型各自独立编号；supplement 在 `auto` 时取 kind 的 `local_name`，分隔符按语言自适应。
- **OutlineElem** 用 `target` 选择器经内省收集元素，对每个查 `dyn Outlinable`（继承自 `Refable`）取层级与正文；编号前缀同样调计数器 `display_at`，与 ref、题注共用同一真相源。
- **三号同源**：标题号、引用号、目录前缀号都调同一计数器的 `display_at`，故永不冲突。
- **自动缩进依赖内省**：outline 的 `indent: auto` 用 `PrefixInfo` 把每层编号宽度写入文档再内省回收，必须靠收敛循环才能稳定——这是本 crate「只定义与归一化、行为在别处经 Routines 回调」主线的又一次印证。

## 7. 下一步学习建议

本讲反复把「计数器」「内省（query/introspect）」「收敛」当作黑盒使用，下一单元（第 9 单元「内省与上下文」）正是打开这些黑盒：

- **u9-l1** 会讲 `Location`/`Tag`/`Locator` 与 `query`/`locate`/`here`/`Context`——本讲里 `QueryLabelIntrospection`、`QueryIntrospection`、`PageNumberingIntrospection` 的底层基础。
- **u9-l2** 会精读 `Counter` 与 `State`——本讲里 `counter.display_at`、`CounterKey::Selector`、`Count::update` 的完整实现。
- **u9-l3** 会讲 `Introspector` 与收敛循环——解释本讲「自动缩进为何要多遍编译才稳定」「延迟错误如何过滤」。

建议继续阅读的源码：

- [src/introspection/counter.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs)：`Counter::display_at` 的实现，看它如何按 location 定位到正确的计数值。
- [src/model/cite.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/cite.rs) 与 [src/model/bibliography.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/bibliography.rs)：本讲里 ref 转 citation 的那个分支（`to_citation`）的下游。
