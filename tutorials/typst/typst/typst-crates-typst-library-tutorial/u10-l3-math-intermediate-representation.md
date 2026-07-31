# 数学中间表示 IR

> 适用范围：`typst-library` 的 `src/math/ir/` 子模块。
> 前置讲义：u10-l1（EquationElem 与数学模式）、u10-l2（裸数学元素与 MathClass）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Typst 为什么要在 `Content` 和最终 `Frame` 之间专门为数学公式插入一层「中间表示（IR）」，以及这层 IR 由谁构建、交给谁排版。
- 读懂 IR 的核心数据类型族：`RawMathItem`、`MathItem`、`MathComponent`、`MathKind`、`MathProperties`，理解它们各自的职责。
- 描述从一段 `Content` 到一棵 `MathItem` 树的**递归下降处理流水线**（`resolve_equation` → `resolve_into_self` → `resolve_realized` → 各 `resolve_xxx`）。
- 解释 `process.rs` 如何按 TeXBook 规则计算符号间距、如何把 `Vary` 类运算符升格为 `Binary`。
- 解释 `multiline.rs` 如何用对齐点 `&`（与 `&&`）把一行公式切成右对齐/左对齐交替的列。

本讲只讲 IR 的**构建**（typst-library 内）。真正把 `MathItem` 排成 `Frame` 的算法住在 `typst-math`/`typst-layout` 等行为 crate，运行期经 `Routines` 回调——这与 u10-l1、u10-l2 一脉相承。

## 2. 前置知识

在进入 IR 之前，请确认你已经理解以下概念（前序讲义已建立）：

- **Content 与元素系统**（u3-l1/u3-l2）：`Content` 是面向用户、**类型擦除**的运行时对象，一棵 `Content` 树可以表示任意标记与函数调用的产物。数学元素（`FracElem`、`AttachElem`、`LrElem` 等）也都是 `Content`。
- **Routines 与 crate 分离**（u5-l4）：行为 crate 依赖本 crate 的类型，而本 crate 不反向依赖行为 crate；需要回调行为时，通过一张 `Routines` 函数指针表在运行期注入实现。`realize` 就是其中一条例程。
- **MathClass 与数学元素**（u10-l2）：每个数学符号都有一个 `MathClass`（如 `Relation`、`Binary`、`Opening`、`Closing`、`Large`、`Vary`），它同时决定**符号间距**与**上下标位置**。
- **StyleChain**（u4-l1）：样式沿链查询，`Resolve` 把相对值（如 `Em`）解析成绝对值需要吃整条样式链（如字号）。

一个关键直觉：`Content` 树是「给用户和求值器看的」，它** heterogeneous（异构）且类型擦除**——任何元素都可能是任何 `Content`，取字段要做能力查询。而数学排版需要的是一棵**结构化、强类型、已预算好样式、已知每个节点 MathClass** 的树。IR 就是这两种视图之间的翻译层。

## 3. 本讲源码地图

本讲涉及 `src/math/ir/` 下的全部文件，外加两个外围引用：

| 文件 | 行数 | 作用 |
|------|------|------|
| [math/ir/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/mod.rs) | 32 | 子模块入口，定义唯一对外函数 `resolve_equation`。 |
| [math/ir/item.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs) | 1310 | IR 的**数据类型族**：`RawMathItem`、`MathItem`、`MathComponent`、`MathKind`、`MathProperties`，以及各种 `XxxItem` 与伸缩配置 `Stretch`/`StretchInfo`。 |
| [math/ir/resolve.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs) | 1474 | **解析器** `MathResolver`：把 `Content` 递归下降翻译成 `MathItem` 树。 |
| [math/ir/process.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs) | 354 | **间距与分组**：TeXBook 间距规则、`Vary→Binary` 升格、断行/对齐点的归一化。 |
| [math/ir/multiline.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/multiline.rs) | 147 | **多行对齐**：`AlignedRow`、`split_at_align`（按 `&` 切列）、`expand_multiline_fence`（跨行定界符）。 |
| [math/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/mod.rs) | — | `AlignPointElem`（`&` 与 `&&`）定义、间距常量 `THIN`/`MEDIUM`/`THICK`。 |
| [routines.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs) | — | `realize` 例程签名、`RealizationKind::Math`、`Arenas`、`Pair`。 |

## 4. 核心概念与源码讲解

### 4.1 math IR 模块：入口与设计动机

#### 4.1.1 概念说明

数学公式是一种**强结构**的内容：分数有分子分母、根号有被开方数、矩阵有单元格、定界符要伸缩、多行要按 `&` 对齐。如果直接在类型擦除的 `Content` 树上做这些事，排版器会被迫在每一步都做能力查询（`can::<C>()`）、字段提取、样式链查找，既慢又易错。

因此 Typst 在 `Content` 与最终 `Frame` 之间插入了一层**数学中间表示（Math IR）**：

1. typst-library 负责把 `Content` **翻译**成一棵强类型、带预算样式的 `MathItem` 树；
2. 行为 crate（`typst-math`/`typst-layout`）拿到这棵树去**真正排版**成 `Frame`。

这层 IR 的生命周期很短——它只为一次公式的排版而存在，并且其内部引用（子 `Content`、`StyleChain` 等）借住在调用方提供的 **arena** 上。入口函数 `resolve_equation` 的文档注释说得明白：「返回的 `MathItem` 与传入的 arenas **具有相同的生命周期**」。

#### 4.1.2 核心流程

整条 IR 构建链路只有一步对外可见：

```text
EquationElem.body (Content)
        │
        ▼  resolve_equation()
   MathResolver::new(engine, locator, arenas)
        │
        ▼  resolve_into_item(&body, styles)
   一棵 MathItem<'a> 树（'a 绑定 arenas）
        │
        ▼  交给 layout routine 排版
        Frame
```

#### 4.1.3 源码精读

入口函数 `resolve_equation` 极其精简，只做「建解析器 → 解析 body」两件事：

[math/ir/mod.rs:19-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/mod.rs#L19-L32) —— 接收一个 `Packed<EquationElem>`，返回与 arenas 同寿的 `MathItem`。注意第 22 行的 `#[typst_macros::time(name = "math ir creation")]`：这条属性给 IR 构建挂了一个计时探针，编译器的性能面板里能看到「math ir creation」这一项耗时。

子模块声明与导出在文件顶部，`pub use self::item::*` 把整个数据类型族对外暴露，而 `multiline::AlignedRow` 单独再导出一次：

[math/ir/mod.rs:1-17](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/mod.rs#L1-L17) —— 注意 `resolve::MathResolver` 是 `use self::resolve::MathResolver`（私有引入），说明解析器本身不对外暴露，外部只能通过 `resolve_equation` 这一个函数进入。

#### 4.1.4 代码实践

**实践目标**：确认 IR 构建在整个数学排版链路里的位置与计时点。

**操作步骤**：

1. 阅读 [math/ir/mod.rs:19-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/mod.rs#L19-L32)，确认 `resolve_equation` 的输入是 `&Packed<EquationElem>`、输出是 `SourceResult<MathItem<'a>>`。
2. 用 `Grep` 在本 crate 内搜索 `resolve_equation` 的调用方（注意：真正的调用在 `typst-layout`，本 crate 内搜不到调用点，这恰好印证了「IR 在此构建、在别处消费」的分工）。
3. 搜索 `time(name =` 看看 typst-library 还给哪些步骤挂了计时探针。

**需要观察的现象**：本 crate 内**找不到** `resolve_equation` 的调用方，因为它由行为 crate 经布局路径回调。

**预期结果**：你会确认「构建 IR」与「排版 IR」是两个 crate 的职责，本讲只覆盖前者。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `resolve_equation` 要把 `Arenas` 作为参数传进来，而不是在函数内部 new 一个？

**参考答案**：因为返回的 `MathItem<'a>` 借用了 arenas 里的 `Content`/`Styles`/`StyleChain`（`'a` 就是 arenas 的生命周期，见 4.3.3 的 `store`/`store_styles`）。如果函数内部 new 一个局部 arena 并返回，局部 arena 一离开作用域就被销毁，返回值里的引用会立即悬垂。所以 arena 必须由**调用方**拥有并保持存活，直到排版阶段用完这棵 IR 树。

---

### 4.2 MathItem：IR 的数据类型族

#### 4.2.1 概念说明

IR 的数据类型由一组互相配合的类型构成，理解它们的分工是读懂后续解析器的前提：

- **`RawMathItem<'a>`**：解析器**工作缓冲区**里的条目。除了真正的数学项，还包含两种「只在解析期存在」的标记——`Linebreak`（换行）和 `Align`（对齐点 `&`）。它们在归一化结束后会被消费掉，不进入最终成品。
- **`MathItem<'a>`**：**顶层** IR 节点，只有四个变体。
- **`MathComponent<'a>`**：「带属性和样式的可布局项」，三字段 `kind + props + styles`，是绝大多数节点的真正载体。
- **`MathKind<'a>`**：约 20 个**具体种类**（分数、根号、定界符、矩阵、上下标……），递归或较大的变体用 `Box` 包裹。
- **`MathProperties`**：所有 component 共享的 `Copy` 属性（class、size、cramped、lspace/rspace、limits 等）。

#### 4.2.2 核心流程

类型之间的包装关系如下（`create` 工厂方法负责把具体项组装成 `MathComponent` 再包成 `MathItem`）：

```text
RawMathItem = Item(MathItem) | Linebreak | Align     // 工作缓冲（临时）
MathItem    = Component(MathComponent) | Spacing | Space | Tag
MathComponent { kind: MathKind, props: MathProperties, styles: StyleChain }
MathKind    = Group | Multiline | Radical | Fenced | Fraction
            | SkewedFraction | Table | Scripts | Accent | Cancel | Line
            | Primes | Text | Number | Glyph | Box | Mathml | External
```

每个 `XxxItem`（如 `FractionItem`、`FencedItem`）都是一个普通 struct，配一个 `XxxItem::create(...)` 工厂方法。工厂方法做的事高度统一：构造 `MathKind::Xxx(...)`，配上 `MathProperties`，包成 `MathComponent`，再 `.into()` 成 `MathItem`。

#### 4.2.3 源码精读

先看工作缓冲区的条目类型。`RawMathItem` 把「真数学项」和「解析期标记」放在一起，方便解析器一边遍历一边插入间距：

[math/ir/item.rs:24-34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L24-L34) —— `Item` 包装真正的 `MathItem`；`Linebreak`/`Align` 仅解析期存在。`into_item()`（第 55-60 行）在归一化收尾时会把 `Linebreak`/`Align` 滤掉（返回 `None`）。

顶层 `MathItem` 只有四个变体：

[math/ir/item.rs:63-75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L63-L75) —— `Component` 承载所有「有结构的可布局项」；`Spacing(Length, Abs, bool)` 记录显式间距、**创建时的字号**与是否「弱间距」（弱间距会在 4.4 讲到的预处理里被取较大者合并）；`Space` 是普通空格；`Tag` 是内省标签（不参与布局）。

`MathComponent` 与 `MathKind` 是 IR 的骨架：

[math/ir/item.rs:367-420](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L367-L420) —— `MathComponent` 三字段 `kind + props + styles`；`MathKind` 的注释第 380-381 行说明「递归或较大的变体用 `Box`」，这是为了避免大枚举每个节点都占用 `Box` 指针的体积。注意 `Scripts`（上下标）有 **6 个槽位**：`top`/`bottom`（limits）、`top_left`/`bottom_left`（前置上下标）、`top_right`/`bottom_right`（常规上下标）——对应 u10-l2 讲过的 `AttachElem` 的六向附件。

公共属性 `MathProperties` 是「类型擦除后保留的排版线索」：

[math/ir/item.rs:422-446](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L422-L446) —— 重点字段：`class: Option<MathClass>`（`None` 表示「未显式设定，用 `Normal` 兜底」，见第 473-475 行的 `class()`）；`lspace`/`rspace: Option<Em>`（左右间距，由 4.4 的 `spacing()` 写入）；`align_form_infix`（多行对齐时是否把间距移到右对齐列，见 4.4.3）；`cramped`（挤压样式，影响上下标位置）；`limits`（附件放置策略，u10-l2 讲过）。

一个值得记住的设计约定：**有些项会从 base 继承 class**。看 `ScriptsItem::create`：

[math/ir/item.rs:717-743](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L717-L743) —— 第 731 行用 `MathProperties::new(styles, base.raw_class(), Span::detached())`，即「带上下标的项」对外表现为 base 的类。`AccentItem`、`CancelItem`、`LineItem`（第 774、820、853 行）同理。这保证了 `x^2` 这种项在间距计算时仍被当作字母（`Alphabetic`），而不是某种无名项。

最后看一个最小工厂 `MathItem::wrap`，它在「只剩一个项」时避免无意义的 Group 包装：

[math/ir/item.rs:84-95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L84-L95) —— 若 `items.len() == 1` 直接返回那一项，否则才包成 `GroupItem`。这条「单元素不打包」的规则让 IR 树保持精简。

#### 4.2.4 代码实践

**实践目标**：建立「元素 → IR 种类」的映射直觉。

**操作步骤**：

1. 打开 [math/ir/item.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs)，对照 `MathKind` 的 18 个变体，为每个变体写出一个对应的 Typst 源码例子（如 `Fraction ← $a/b$`、`Radical ← $root(x, 3)$`、`Table ← $mat(...)$`、`Scripts ← $x^2$`、`Fenced ← $lr(a, b)$`）。
2. 找到 `MathProperties::new`（第 450-463 行）与 `MathProperties::default`（第 468-470 行），说明二者的唯一区别（提示：`class` 参数）。
3. 在 `GlyphItem::create`（第 952-974 行）处确认：单个字符的字形，其 `class` 与 `limits` 是怎么由 `default_math_class(c)` 与 `Limits::for_char_with_class` 决定的。

**需要观察的现象**：`new` 接受显式 `class`、`default` 不接受（传 `None`）。

**预期结果**：你会看到「需要继承/指定 class 的项用 `new`，普通项用 `default`」这条简单规律。

#### 4.2.5 小练习与答案

**练习 1**：`RawMathItem` 里的 `Linebreak` 和 `Align` 为什么不直接做进 `MathItem`？

**参考答案**：因为它们是**结构标记**而非可布局内容，只在解析与分组阶段有意义。最终交给排版的 IR 树里，换行已经被吸收进 `MultilineItem` 的多行结构、对齐点已经被吸收进 `AlignedRow` 的多列结构（见 4.4）。把它们放在 `RawMathItem` 这个「工作缓冲类型」里，可以让解析器用统一的 `Vec<RawMathItem>` 一边遍历一边插间距、切列，最后再统一滤掉。

**练习 2**：为什么 `MathKind` 的大变体（`Radical`/`Fenced`/`Fraction`/`Table` 等）要 `Box` 包装，而 `Text`/`Number`/`External`/`Mathml` 不包？

**参考答案**：`Box` 是为了控制枚举体积。不含 `MathItem` 子树的小变体（如 `Number{text}`、`Primes{count}`）本身很小，直接内联；而含递归子树或字段众多的变体若直接内联，会让整个 `MathKind` 枚举膨胀到每个节点都占用很大栈空间。`Box` 把它们放到堆上，枚举本身只持一个指针。文件第 380-381 行的注释明说了这一动机。

---

### 4.3 resolve：Content → MathItem 的递归下降解析器

#### 4.3.1 概念说明

`MathResolver` 是 IR 的构建器，它把一棵异构、类型擦除的 `Content` 树，递归下降地翻译成一棵强类型的 `MathItem` 树。翻译分两个阶段：

1. **realize（实现）**：调用 `Routines` 里的 `realize(RealizationKind::Math, ...)`，把任意 `Content`（含 show 规则产物、函数调用等）**展平**成一个由「typst-library 认识的叶子元素 + 样式链」组成的扁平列表 `Vec<Pair>`。这一步在行为 crate 完成。
2. **resolve（解析）**：对每个叶子元素，用一个大的分派函数 `resolve_realized` 把它翻译成对应的 `MathItem`，递归处理其子内容。

这套两段式设计与 u5-l4 讲过的 crate 分离机制完全一致：realize 是行为，经 routine 回调；resolve 是本 crate 内的纯翻译。

#### 4.3.2 核心流程

```text
resolve_into_item(elem, styles)
  │
  ├─ resolve_into_items(elem, styles)        // 记录起始下标 start
  │     │
  │     └─ resolve_into_self(content, styles)
  │           │
  │           ├─ (routines.realize)(Math, ...) → Vec<Pair>   // 行为 crate 展平
  │           │
  │           └─ for (elem, styles) in pairs:
  │                 resolve_realized(elem, ctx, styles)      // 大分派
  │                   ├─ to_packed::<FracElem>()  → resolve_frac
  │                   ├─ to_packed::<AttachElem>()→ resolve_attach
  │                   ├─ to_packed::<LrElem>()    → resolve_lr
  │                   ├─ to_packed::<TextElem>()  → resolve_text
  │                   ├─ ...（约 30 种）
  │                   └─ else → ExternalItem::create         // 未知元素外包
  │
  └─ process_group(items[start..], ...) → Multiline | Flat(Group)  // 归一化收尾
```

每个 `resolve_xxx` 内部都会：先为子内容**计算派生样式**（如分数的分子用 `style_for_numerator`、根号用 `style_cramped`），再 `ctx.resolve_into_item(child, child_styles)` 递归，最后用 `XxxItem::create(...)` 组装。

#### 4.3.3 源码精读

先看解析器结构体本身。`MathResolver` 持有 engine、locator、arenas 与一个工作缓冲 `items`：

[math/ir/resolve.rs:38-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L38-L63) —— 注意第 47 行 `items: Vec<RawMathItem<'a>>` 就是「工作缓冲区」，所有解析产物先推进这里，最后再归一化。

关键方法 `resolve_into_item`：解析完后做收尾，决定最终是一个项、一个 Group，还是一个 Multiline：

[math/ir/resolve.rs:106-124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L106-L124) —— 第 116-118 行：若解析后缓冲里**恰好一个真项**（且不是孤立的 Linebreak/Align），直接 `pop` 返回它，避免多余包装；否则（第 120-123 行）交给 `process_group` 决定 `Multiline`（有换行/对齐）还是 `Flat`（普通序列，包成 Group）。

realize 调用在 `resolve_into_self` 里——这是「Content → 扁平叶子列表」的咽喉：

[math/ir/resolve.rs:127-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L127-L146) —— 第 132 行 `(self.engine.library.routines.realize)(RealizationKind::Math, ...)` 正是 u5-l4 讲过的「经函数指针打破循环依赖」的写法；返回 `Vec<Pair>`（`Pair = (&Content, StyleChain)`，见 [routines.rs:195-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L195-L196)）；随后第 141-143 行对每个 pair 调 `resolve_realized`。

`resolve_realized` 是核心分派表，用一长串 `else if let Some(elem) = elem.to_packed::<XxxElem>()` 把叶子元素导向各自的 `resolve_xxx`：

[math/ir/resolve.rs:149-235](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L149-L235) —— 注意几个要点：
- `SpaceElem` → `MathItem::Space`（第 157-158 行）；`AlignPointElem` → `RawMathItem::Align`（第 171-172 行，即 `&`）；`LinebreakElem` → `RawMathItem::Linebreak`（第 179-180 行）。这三种是把源码里的空白/对齐/换行直接翻译成工作缓冲标记。
- 末尾的 `else` 分支（第 230-233 行）：任何 typst-library 不认识的元素都走 `ExternalItem::create`，意思是「我自己排不了，把它整体外包给布局 routine 单独排版」。这就是为什么数学环境里嵌入任意 `Content`（如图片、表格）也能工作。

以分数为例看「派生样式 + 递归 + 组装」三段式：

[math/ir/resolve.rs:724-771](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L724-L771) —— `resolve_vertical_frac_like`：第 732-733 行先用 `style_for_numerator`/`style_for_denominator`（u10-l1 讲过的字号缩放）算出子样式；第 736、738 行分别递归解析分子分母（注意第 739-747 行把多个分母用逗号拼接成序列再解析）；第 750 行用 `FractionItem::create` 组装。若是二项式（`binom`，第 752-768 行），还会再套一层 `FencedItem`（伸缩的圆括号）。这条函数同时服务于 `frac` 和 `binom`，是「数据归一化」的典型——把两种用户元素映射到同一棵 IR 子树（差异仅在是否套定界符）。

关于生命周期与 arena：解析器需要把临时构造的 `Content`/`Styles`/`StyleChain`「延长寿命」到 `'a`，靠的是一组 `store_*` 方法：

[math/ir/resolve.rs:65-92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L65-L92) —— `store_styles` 用 `arenas.styles.alloc`（typed_arena）、`store_chain` 用 `arenas.bump.alloc`（bumpalo）、`store` 用 `arenas.content.alloc`。`Arenas` 的三个字段正是为此而设（见 [routines.rs:185-193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L185-L193)）。bumpalo 的特点是一次性批量回收，非常适合「为一棵 IR 树临时分配、用完整体丢弃」的场景。

#### 4.3.4 代码实践

**实践目标**：跟踪一条具体调用链，验证「两段式 + 递归下降」模型。

**操作步骤**：

1. 从 [math/ir/resolve.rs:107](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L107) 的 `resolve_into_item` 出发，写出 `$a + b$` 的解析路径：`resolve_into_self` → realize 把 `a + b` 展平为 `[TextElem("a"), SymbolElem("+"), TextElem("b")]`（大致） → 对每个调 `resolve_realized`。
2. 在 `resolve_realized`（第 150-235 行）里找到 `TextElem` 与 `SymbolElem` 各自走哪个分支（`resolve_text` 与 `resolve_symbol`）。
3. 阅读 `resolve_symbol`（第 330-358 行）：它对符号串按 **grapheme cluster** 切分，每个 cluster 调 `to_style` 做数学样式归一化后生成一个 `GlyphItem`；若是 `Large` 类且 `Display` 尺寸，还会预设一个竖直伸缩（第 350-353 行）。

**需要观察的现象**：`+` 不是 `Binary` 类的运算符元素，而是一个**普通符号**，它的 `MathClass` 来自 `default_math_class('+')`（在 `GlyphItem::create` 里）。

**预期结果**：你会理解「间距」并不是在 resolve 阶段计算的——resolve 只产出带 `class` 的项，真正的间距在 4.4 的 `preprocess` 里按类查表写入。

#### 4.3.5 小练习与答案

**练习 1**：`resolve_into_item` 在什么情况下返回 `MultilineItem`？什么情况下返回单个 `MathItem`？

**参考答案**：看 [resolve.rs:120-123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L120-L123)：解析完缓冲后调 `process_group(...)`。若缓冲里**没有换行、也没有（在 split 模式下的）对齐点**，返回 `Flat`，包成 Group（或单个项）；若有换行或对齐点，返回 `Multiline(rows)`，建成 `MultilineItem`。另外第 116-118 行有个快路径：若缓冲里恰好一个真项，直接返回它，不走 `process_group`。

**练习 2**：为什么 `resolve_realized` 的分派顺序里，`AlignPointElem`/`LinebreakElem`/`SpaceElem` 用 `elem.is::<T>()` 而 `FracElem` 等用 `elem.to_packed::<T>()`？

**参考答案**：`AlignPointElem`、`LinebreakElem`、`SpaceElem` 是**零字段标记元素**（如 `AlignPointElem` 是 `pub struct AlignPointElem {}`），不携带需要读取的 `Packed<T>` 数据，只需判断「是不是它」即可，所以用 `is::<T>()`。其余元素有字段（如 `FracElem` 的 `num`/`denom`），需要拿到 `&Packed<T>` 才能读字段，所以用 `to_packed::<T>()`。

---

### 4.4 process 与 multiline：间距、断行与对齐点的归一化

#### 4.4.1 概念说明

resolve 阶段产出的 `RawMathItem` 流**还没有间距**——符号之间是紧挨着的。把它们排成漂亮的公式，需要做三件归一化：

1. **算间距**：按 TeXBook 的 MathClass 配对规则，在相邻项之间插入 `THIN`/`MEDIUM`/`THICK` 间距（或直接写到项的 `lspace`/`rspace`）。
2. **升格运算符**：`Vary` 类的运算符（如 `+`、`-`）在前有操作数、且前面不是另一个运算符/比较符时，升格为 `Binary`，从而获得正确的两侧间距。
3. **切分多行**：按换行（`Linebreak`）分行、按对齐点（`Align`，即 `&`）分列，并保证各行列数对齐。

这三件事由 `process.rs` 与 `multiline.rs` 协作完成。`&` 与 `&&` 在源码层都是 `AlignPointElem`（见 [math/mod.rs:113-115](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/mod.rs#L113-L115) 的文档注释「`&`, `&&`」），在 IR 层都表示为 `RawMathItem::Align`，由 `split_at_align` 统一处理——多写一个 `&` 就多切出一个（可能为空的）对齐列。

#### 4.4.2 核心流程

`process_group` 是归一化的总入口，它根据「是否有换行/对齐点」分流：

```text
process_group(items, closing, pad, split)
  │
  ├─ preprocess(items, closing)          // 算间距 + Vary→Binary + 清理收尾
  │     产出 Preprocessed { items, had_linebreaks, has_align, linebreaks }
  │
  ├─ if linebreaks>0 或 (split 且 has_align):
  │     按 Linebreak 切行 → 每行 split_at_align → AlignedRow
  │     若 pad：补齐各行列数
  │     → GroupResult::Multiline(rows)
  │
  └─ else:
        滤掉残余 Align 标记 → GroupResult::Flat(items)
```

`preprocess` 内部的间距规则用一张「`(左项.rclass, 右项.lclass) → 设置哪一侧的间距`」的查表实现。三个常量（[math/mod.rs:36-38](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/mod.rs#L36-L38)）：

| 常量 | 值 | 用途 |
|------|----|------|
| `THIN` | \(1/6\,\text{Em}\approx 0.167\,\text{em} \) | 标点之后、大运算符周围 |
| `MEDIUM` | \(2/9\,\text{Em}\approx 0.222\,\text{em} \) | 二元运算符两侧 |
| `THICK` | \(5/18\,\text{Em}\approx 0.278\,\text{em} \) | 关系符两侧 |

#### 4.4.3 源码精读

先看 `process_group` 的分流逻辑：

[math/ir/process.rs:29-75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L29-L75) —— 第 40 行先 `preprocess`；第 41 行判断 `linebreaks > 0 || (split && preprocessed.has_align)` 决定走多行还是扁平。走多行时（第 42-63 行）：在流末尾追加一个哨兵 `Linebreak`（第 46 行），然后每逢 `Linebreak` 就把累积的 `row` 交给 `split_at_align` 切列；若 `pad`（第 56-61 行），统计最大列数 `ncols` 并把每行补齐。

`preprocess` 是间距规则的心脏。它遍历每个项，分情况处理，并在两个「非无知（non-ignorant）」项之间调 `spacing()` 插间距：

[math/ir/process.rs:135-274](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L135-L274) —— 重点几处：
- `Space`（第 157-162 行）：只有当前面已有非空项（`last.is_some()`）时才暂存为一个待用空格，否则丢弃（行首空格无意义）。
- 显式 `Spacing`（第 165-189 行）：一旦出现显式间距，**取消**自动间距（`last = None; space = None`）；若是**弱间距**（`weak=true`），则与前一相邻弱间距取较大者合并（第 169-185 行），这正是 `#h(1em, weak: true)` 的语义来源。
- `Vary→Binary` 升格（第 218-229 行）：这是 u10-l2 提到过的规则落地——当当前项是 `Vary` 类、且前一非空项是 `Normal`/`Alphabetic`/`Closing`/`Fence` 时，把它改判为 `Binary`，从而在 `spacing()` 里拿到 `MEDIUM` 两侧间距。
- 收尾（第 247-266 行）：若 `closing`（后面跟闭合定界符）且最后一项是标点，给它加 `THIN` 右间距（TeXBook 闭标点规则）；去掉末尾悬挂的弱间距；去掉末尾多余的 `Linebreak`。

`spacing()` 是 MathClass 配对的查表函数，直接对应 TeXBook p.170 的间距表：

[math/ir/process.rs:277-319](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L277-L319) —— 以 `(l.rclass(), r.lclass())` 为键 match。例如 `(Relation, _)` 给左项设 `THICK` 右间距（除非右项也是 `Relation` 或在 script 尺寸）；`(_, Binary)` 给右项设 `MEDIUM` 左间距；`(_, Punctuation)` 不加间距（标点前无间距）。注意第 284 行的 `script` 闭包：在 script/scriptscript 尺寸下**关闭大部分间距**，因为小字号下加间距会很难看。注意一个精妙处：第 286 行用的是 `l.rclass()`/`r.lclass()`（左右**有效**类），而 `FencedItem` 的 `rclass`/`lclass` 会返回 `Opening`/`Closing`（[item.rs:122-146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L122-L146)），所以「`(a)b`」里 `)` 后面会按 `Closing` 类算间距，而不是按括号内内容的类。

再看多行对齐 `split_at_align`。它把一行的项按 `&` 切成若干列，并对落在「左对齐列起点」的关系/二元符做特殊标记：

[math/ir/multiline.rs:113-147](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/multiline.rs#L113-L147) —— 每逢 `Align`（第 122-125 行）就 `push` 一个新空列；对紧跟对齐点的非无知项，若 `cols.len()` 为偶数（即进入了「(右,左)配对」中的左对齐列）且是 `Relation`/`Binary`，就把它的 `align_form_infix` 置 true（第 130-138 行）。源码顶部的注释（[process.rs:127-134](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L127-L134)）明说这条策略**与 amsmath 不同**：Typst 选择把不同列之间的间距移到右对齐列。`&` 与 `&&` 在此处并无特殊分支——它们都只是多产生一个 `Align` 标记，多切一列。

`AlignedRow` 是「一行 = 若干列」的容器，`pad_to` 用空 Group 补齐列数：

[math/ir/multiline.rs:9-45](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/multiline.rs#L9-L45) —— 第 30-34 行 `pad_to` 用 `MathItem::wrap(Vec::new(), styles)`（空 Group）补齐，保证所有行列数一致，这是多行对齐排版的前提。

最后看一个进阶场景：**跨行定界符**。当 `$ lr( ... ) $` 内部含换行时（如多行公式外层套了大括号），`resolve_lr` 会调 `expand_multiline_fence` 把一对定界符「摊开」到每个单元格上，并让所有段共享同一个伸缩目标高度：

[math/ir/multiline.rs:56-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/multiline.rs#L56-L108) —— 第 75 行用 `SharedFenceSizing::new(bodies, styles)` 把所有单元格的 body 收拢进一个共享对象；第 91-100 行为每个单元格建一个 `FencedItem`，body 用 `FencedBody::shared(body_idx, sizing.clone())`（[item.rs:1086-1105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L1086-L1105)）。`SharedFenceSizing` 内部用 `Rc` 共享、用 `Cell<Option<Abs>>` 缓存算出的目标高度（[item.rs:1062-1082](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L1062-L1082)），这样伸缩定界符时所有段共用一个高度，外观才连贯。`resolve_lr` 里调用它的地方在 [resolve.rs:971-975](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L971-L975)。

#### 4.4.4 代码实践

**实践目标**：手工模拟 `preprocess` 与 `split_at_align`，预测 IR 结构。

**操作步骤**：

1. 取公式 `$a = b + c$`。按 [process.rs:218-229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L218-L229) 判断：`+` 是 `Vary` 类，前一项 `b` 是 `Alphabetic`，故 `+` 升格为 `Binary`。
2. 按 [process.rs:286-316](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L286-L316) 的 `spacing()` 表推间距：
   - `a`(Alphabetic) 与 `=`(Relation) 之间：`(_, Relation)` → 给 `=` 设 `THICK` 左间距；
   - `=`(Relation) 与 `b`(Alphabetic) 之间：`(Relation, _)` → 给 `=` 设 `THICK` 右间距；
   - `b` 与 `+`(Binary) 之间：`(_, Binary)` → 给 `+` 设 `MEDIUM` 左间距；
   - `+`(Binary) 与 `c` 之间：`(Binary, _)` → 给 `+` 设 `MEDIUM` 右间距。
3. 取多行公式（用 Typst 语法）：
   ```typst
   $ a &= b + c  \
     &+ d $
   ```
   按 [multiline.rs:113-147](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/multiline.rs#L113-L147) 的 `split_at_align`：第一行 `[a, &, =, b, +, c]` 被切成两列 `[a]` 与 `[= b + c]`；第二行切成 `[]`（空）与 `[+ d]`。第二行首项 `+`（升格后 Binary）落在左对齐列起点、`cols.len()` 为偶数，故 `align_form_infix = true`。

**需要观察的现象**：两行的第二列分别是 `= b + c` 与 `+ d`，它们会在最终排版里按 `&` 对齐到同一竖直位置（`=` 与 `+` 对齐）。

**预期结果**：你应当能说出每对相邻项之间被插入了哪种间距，以及多行公式切成了几行几列。若想验证，可在本地用 `typst compile` 生成 PDF 肉眼对照对齐效果（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `$a +$`（末尾是 `+`）和 `$a$` 在 script 尺寸下间距表现不同？

**参考答案**：`spacing()` 里的 `script` 闭包（[process.rs:284](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L284)）判断当前项是否处于 `MathSize::Script` 或更小；若是，则**跳过** `MEDIUM`/`THICK` 等间距的设置（例如第 302-303 行 `if !script(r)`）。所以上下标里的小公式间距更紧凑。`THIN`（标点后）同样受此影响（第 290 行 `if !script(l)`）。

**练习 2**：`process_group` 的 `pad` 参数为 `true` 时会发生什么？谁会用到它？

**参考答案**：`pad=true` 时（[process.rs:56-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L56-L61)），统计所有行的最大列数 `ncols`，把每行用空 Group `pad_to(ncols)` 补齐。多行公式（`EquationElem` 的 body 经 `resolve_into_item` 收尾，第 120 行传 `pad=true`）需要它，因为各行列数可能不同，必须补齐才能整体对齐排版。而 `resolve_lr` 调 `process_group` 时传 `pad=false`（[resolve.rs:971](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L971)），因为它会用 `expand_multiline_fence` 自行处理单元格。

**练习 3**：`expand_multiline_fence` 为什么要用 `SharedFenceSizing` 而不是给每个单元格独立计算定界符高度？

**参考答案**：跨行定界符的左右括号要**共享同一个目标高度**才能视觉连贯（一个括号跨多行）。`SharedFenceSizing`（[item.rs:1051-1082](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L1051-L1082)）用 `Rc` 让所有段共享一份 body 列表、用 `Cell` 缓存一次计算的高度（`try_get_or_update`，第 1070-1081 行），避免每段重复计算且保证各段定界符伸缩到同一基准。

## 5. 综合实践

把本讲四条主线（IR 入口、数据类型、resolve 流水线、process/multiline 归一化）串起来，完成下面这个「**从 Typst 源码到 IR 树**」的端到端追踪任务。

**任务**：给定如下 Typst 公式（多行、含对齐、含跨行定界符、含分数）：

```typst
$ lr( x &= frac(1, 2) + a \
        &  + b ) $
```

请按下列步骤产出一份「IR 结构报告」：

1. **入口定位**。从 [math/ir/mod.rs:23-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/mod.rs#L23-L32) 的 `resolve_equation` 进入，确认它解析的是 `EquationElem.body`。
2. **realize 展平**。说明 [resolve.rs:132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L132) 的 `realize(Math, ...)` 会把这段 body 展平成哪些叶子元素（预期包含 `LrElem`，其 body 内含 `TextElem(x)`、`AlignPointElem`、`FracElem`、`SymbolElem(+)`、`TextElem(a/b)`、`LinebreakElem`）。
3. **resolve 分派**。在 [resolve.rs:149-235](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L149-L235) 中找到 `LrElem` 走 `resolve_lr`、`FracElem` 走 `resolve_frac`，并说明 `resolve_frac` 内部会递归调 `resolve_into_item` 解析分子分母，最终产出 `FractionItem`。
4. **resolve_lr 的多行处理**。读 [resolve.rs:852-984](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L852-L984)：因为 body 含换行，`process_group` 返回 `Multiline(rows)`，于是走 [resolve.rs:972-975](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L972-L975) 的 `expand_multiline_fence`，把一对 `(` `)` 摊到每个单元格、共享一个 `SharedFenceSizing`。
5. **间距与升格**。指出 `+` 在第一行（前为 `FractionItem`）和第二行（行首）分别是否升格为 `Binary`，依据 [process.rs:218-229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/process.rs#L218-L229)。
6. **手绘 IR 树**。画出最终的 `MathItem` 树草图：顶层是 `Fenced`（`open='('`, `close=')'`），其 body 是一个「共享 sizing」结构，内部是多行多列的 `AlignedRow`，其中第一行第二列含一个 `Fraction` 项。

**交付物**：一份 Markdown，含 (a) 调用链文字描述、(b) 手绘的 IR 树（用缩进列表表示）、(c) 对 `+` 升格与否的判断与理由。

**验证**：在本地仓库执行 `cargo build -p typst-library` 确认无编译错误（仅阅读型实践，不修改源码）。若本地装了 typst CLI，可 `typst compile` 上述公式生成 PDF，肉眼确认 `=` 与 `+` 对齐、定界符跨行连贯（待本地验证）。

## 6. 本讲小结

- Typst 在 `Content` 与 `Frame` 之间为数学公式专门插入了一层 **Math IR**，由 typst-library 的 `src/math/ir/` 构建，交给行为 crate 排版；入口是 [mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/mod.rs) 的 `resolve_equation`，返回值的生命周期绑定调用方提供的 `Arenas`。
- IR 数据类型族为 `RawMathItem`（工作缓冲，含临时 `Linebreak`/`Align` 标记）→ `MathItem`（顶层四变体）→ `MathComponent{kind, props, styles}` → `MathKind`（约 18 个具体种类）；`Scripts`/`Accent`/`Cancel`/`Line` 等会从 base **继承 class**。
- 解析是**两段式 + 递归下降**：先经 `routines.realize(Math, ...)` 把 `Content` 展平成 `Vec<Pair>`，再用 `resolve_realized` 的大分派表把每个叶子元素翻译成 `MathItem`；临时 `Content`/`Styles`/`StyleChain` 借住在 `Arenas`（typed_arena + bumpalo）以延长生命期。
- `process.rs` 实现 TeXBook 间距规则：`preprocess` 在相邻非无知项间按 `(rclass, lclass)` 查表写入 `THIN`/`MEDIUM`/`THICK`，并把 `Vary` 类运算符在前有操作数时升格为 `Binary`；script 尺寸下关闭大部分间距。
- `multiline.rs` 处理多行对齐：`&`（与 `&&`）都降为 `RawMathItem::Align`，由 `split_at_align` 把一行切成右/左对齐交替的列（`AlignedRow`），并把左对齐列起点的关系/二元符标 `align_form_infix`；`expand_multiline_fence` 用 `SharedFenceSizing` 让跨行定界符共享一个伸缩高度。
- 贯穿全讲的结论与 u10-l1/u10-l2 一致：**本 crate 只定义并构建 IR，真正排成 `Frame` 的算法住在行为 crate，经 `Routines` 回调**。

## 7. 下一步学习建议

- **向上看（IR 如何被消费）**：本讲止于 `MathItem` 的构建。要看完后半程，需到 `typst-math`/`typst-layout` 行为 crate 阅读它们如何遍历 `MathItem` 树、调用 rustybuzz 塑形、组装 `Frame`。建议从 `Routines::layout_frame`（[routines.rs:92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L92)）的实现端切入。
- **横向看（realize 的另一面）**：`RealizationKind` 还有 `Document`/`Fragment`/`Par`/`Bundle`（[routines.rs:154-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L154-L169)）。对照本讲的 `Math` 分支，理解同一套 realize 机制如何服务不同输出目标。
- **向深看（伸缩与变量字体）**：`Stretch`/`StretchInfo`（[item.rs:1124-1301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/item.rs#L1124-L1301)）与 u7-l2 的变量字体轴（`wght`/`wdth` 等）有联动——大运算符的竖直伸缩最终要落到字体装配（glyph assembly）上，可结合 u7-l2 的 `FontVariations::resolve` 一起读。
- **实践建议**：挑一个本讲没展开的 `resolve_xxx`（如 `resolve_attach` 的附件合并 [resolve.rs:406-580](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L406-L580)，它用栈分配链表合并嵌套附件），自己画一遍数据流，巩固「派生样式 + 递归 + 组装」的三段式读码方法。
