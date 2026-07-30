# jump_from_click 入口与文档后端抽象

## 1. 本讲目标

Typst 有两种「编译产物」：分页文档 `PagedDocument`（PDF / 预览后端）和 HTML 文档 `HtmlDocument`。typst-ide 提供了一个非常讨喜的功能——**点击渲染结果里的某个字 / 图形 / 链接，直接跳回源码对应位置**。本讲只聚焦这条「点击 → 跳转」链路的**入口层**，讲清三件事：

1. 跳转终点 `Jump` 有哪几类、分别对应什么场景。
2. 公共入口 `jump_from_click` 的签名与参数含义。
3. typst-ide 用一个 **sealed trait**（封闭 trait）`JumpFromDocument` 统一了 `PagedDocument` 与 `HtmlDocument` 两种后端——为什么是 sealed trait，而不是泛型函数或 enum；以及 `PagedDocument` 的 `resolve_position` 如何把一次点击落到对应页面的 `frame` 上。

学完后你应当能：

- 说出 `Jump::File / Url / Position` 三种终点各自的触发场景。
- 读懂 `jump_from_click<D: JumpFromDocument>` 的泛型签名与关联类型 `D::Position` 的含义。
- 解释 Rust「sealed trait 模式」在此处的封闭意图，以及它相对泛型/enum 的优势。
- 描述 `PagedDocument` 从 `PagedPosition` 到 `frame` 命中检测的完整流程。

> 本讲**不**深入 `frame` 内部的命中检测算法（那是 u7-l2），也**不**讲反向的「光标 → 渲染位置」（那是 u7-l3）。本讲把 `jump_from_click_in_frame` 当作一个黑盒「命中检测助手」，只关注入口分发与后端抽象。

## 2. 前置知识

### 2.1 两种输出后端

Typst 编译可以输出两种结构化产物：

- `PagedDocument`（来自 `typst-layout`）：分页文档，由若干 `Page` 组成，每个 `Page` 持有一个 `Frame`（一帧，里面装着文字字形、图形、图片、链接、子分组等渲染图元）。对应 PDF、预览。
- `HtmlDocument`（来自 `typst-html`）：HTML 文档，是一棵 `HtmlNode` 组成的 DOM 树（Element / Text / Tag / Frame）。

### 2.2 位置（Position）是后端相关的

「点击发生在渲染结果的哪里」这件事，两种后端的描述方式完全不同：

| 后端 | 位置类型 | 描述 |
| --- | --- | --- |
| Paged | `PagedPosition` | 第几页（1 起）+ 页面内二维坐标 `Point` |
| HTML | `HtmlPosition` | 一串 DOM 索引路径 `EcoVec<usize>` + 节点内偏移（字符或 frame 内坐标） |

这种「位置类型随后端而变」正是本讲 sealed trait 设计的出发点。

### 2.3 关联类型（associated type）与 sealed trait

- **关联类型**：trait 内部声明 `type Position;`，让「实现这个 trait 的类型」自带一个「配套的位置类型」。调用方写 `position: &D::Position`，编译器就能在静态层面强制「传 `PagedDocument` 就必须配 `PagedPosition`」。
- **sealed trait 模式**：把 trait 的真实方法放进一个**私有模块**里的 supertrait，公开 trait 只是空壳并继承它。因为私有 supertrait 在 crate 外无法被命名/实现，外部就无法为自己的类型实现这个公开 trait——从而把「谁能实现」封闭在 crate 内部。

如果你还不熟悉这两点，本讲 4.3 节会结合真实代码详细展开。

## 3. 本讲源码地图

本讲全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [src/jump.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs) | 双向跳转的全部实现：`Jump`、`jump_from_click`、`JumpFromDocument`（sealed）、`jump_from_click_in_frame`（frame 命中）、`jump_from_cursor`（反向跳转，本讲不展开）。 |

辅助理解（跨 crate 类型，本讲会引用但不深读）：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs) | 第 15 行 `pub use` 把 `Jump`、`jump_from_click` 等摆上货架。 |
| `crates/typst-library/src/introspection/position.rs` | `PagedPosition` / `HtmlPosition` / `InnerHtmlPosition` 的定义。 |
| `crates/typst-layout/src/document.rs` | `PagedDocument`、`Page` 的定义（`Page` 持有 `frame`）。 |
| `crates/typst-library/src/foundations/target.rs` | `AsOutput` trait（`fn as_output(&self) -> &dyn Output`），让 `jump_from_click_in_frame` 能同时接受两种文档。 |

## 4. 核心概念与源码讲解

### 4.1 Jump：跳转目标的三种类型

#### 4.1.1 概念说明

「点击 → 跳转」的终点用 `Jump` 枚举表达。它只有三种互斥的终点，覆盖了用户在渲染结果上可能点击的全部对象：

- `Jump::File(FileId, usize)`：跳到**某个源码文件**的某个字节偏移。最常见——点击正文里的一个字，跳回 `.typ` 源码。
- `Jump::Url(Url)`：跳到一个**外部 URL**。点击超链接（`#link("https://...")`）时触发。
- `Jump::Position(PagedPosition)`：跳到**分页文档的另一处页面坐标**。点击内部跳转链接（如脚注、`#link(location)`）时触发。

注意第三种**只**用 `PagedPosition`，没有 HTML 变体——因为「页面坐标」这个概念只存在于分页后端。

#### 4.1.2 核心流程

把一次点击映射成 `Jump` 的总流程（本讲只展开到「分发到后端」为止）：

```
用户点击渲染结果
      │
      ▼
jump_from_click(world, document, position)   ← 公共入口（4.2）
      │  按 document 的实际类型静态分发
      ▼
<后端>.resolve_position(world, position)     ← sealed trait 方法（4.3 / 4.4）
      │  PagedDocument: 取页 → 委托 frame 命中
      │  HtmlDocument : 遍历 DOM 树
      ▼
jump_from_click_in_frame(...)  或  DOM 内定位   ← u7-l2 详讲
      │
      ▼
Option<Jump>   ← File / Url / Position，或 None
```

#### 4.1.3 源码精读

`Jump` 的定义极其简短：

[jump.rs:L14-L23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L14-L23) — 定义「跳转终点」的三种互斥类型：跳到源码文件偏移、跳到外部 URL、跳到分页文档的页面坐标。

枚举上还有一个**私有**辅助构造器 `Jump::from_span`，它把一个语法 `Span`（源码位置标记）转成 `Jump::File`：

[jump.rs:L25-L31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L25-L31) — `from_span` 先用 `span.id()` 拿到文件 id（detached span 会返回 `None`），再用 `world.range(span)` 查出该 span 在源码里的字节区间，取其 `start` 作为偏移，产出 `Jump::File`。

> 小知识：`Span` 是 typst-syntax 里给每个语法节点分配的轻量「身份证」。一个 span 由 `(id, offset)` 组成，`id` 指向所属文件。`Span` 是 detached（无文件归属）时 `span.id()` 返回 `None`，于是 `from_span` 也返回 `None`——这就是 u7-l2 里图形/图片点击有时返回 `None` 的根源之一。`from_span` 是 `Jump` 的「最小公分母」：凡是命中了一个带 span 的图元（图形、图片），就用它收尾。

`Jump` 与入口函数都通过 lib.rs 第 15 行对外导出：

[lib.rs:L15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L15) — `pub use self::jump::{Jump, jump_from_click, jump_from_click_in_frame, jump_from_cursor};` 把跳转相关的四个公共项摆上货架。

#### 4.1.4 代码实践

**目标**：在真实测试里观察 `Jump` 的三个变体分别何时出现。

**步骤**：

1. 打开 [jump.rs:L474-L809](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L474-L809) 的测试模块，重点看这三处：
   - `test_jump_from_click`（L577-L585）：点击正文文字，期望 `Jump::File(...)`。
   - `test_footnote_links`（L781-L786）：点击脚注链接，期望 `Jump::Position(...)`。
   - `jump_from_click_in_frame` 内部对 `FrameItem::Link(Destination::Url(url), ..)` 会产出 `Jump::Url`（见 [jump.rs:L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L223)）。
2. 运行（**待本地验证**）：

   ```bash
   cargo test -p typst-ide --lib jump_from_click
   ```

**需要观察的现象**：测试通过，说明三类终点都能被正确产生。注意 `test_footnote_links` 里 `pos(1, 10.0, 31.58).map(Jump::Position)` 正是把一个 `PagedPosition` 包进 `Jump::Position`——印证了「内部跳转链接 → `Jump::Position`」。

**预期结果**：三条 `test_*` 全部 pass；你能在断言里一一对应看到 `File` / `Position`（`Url` 的产生点在 frame 命中函数内）。

#### 4.1.5 小练习与答案

**练习 1**：用户在 PDF 里点击 `#link("https://typst.app")` 的文字，最终会得到哪个 `Jump` 变体？为什么不是 `Jump::Position`？

> **答案**：`Jump::Url`。因为该链接的目标是 `Destination::Url`，frame 命中检测在 `FrameItem::Link` 分支里按 `Destination` 分派，URL 直接产出 `Jump::Url`（[jump.rs:L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L223)）。`Jump::Position` 只对应 `Destination::Position` / `Destination::Location` 这类「页内坐标」目标。

**练习 2**：`Jump::Position` 为什么没有 HTML 版本（即不存在 `Jump::HtmlPosition`）？

> **答案**：「页面坐标」是分页后端独有的概念；HTML 后端的跳转终点要么是源码（`Jump::File`）要么是外链（`Jump::Url`），不需要「跳到 DOM 某处」这种终点。因此 `Jump` 只用 `PagedPosition` 表达 `Position` 变体，保持终点集合精简。

---

### 4.2 jump_from_click：公共入口

#### 4.2.1 概念说明

`jump_from_click` 是「点击跳转」对外的唯一入口。它的关键设计是**泛型 + 关联类型**：用类型参数 `D` 表示「文档后端」，用 `D::Position` 表示「该后端对应的位置类型」。这样一份函数签名就能同时服务两种后端，而调用方传不同后端时，位置参数的类型也会被编译器静态约束。

#### 4.2.2 核心流程

入口函数本身**不做任何命中检测**，它只做一件事——把点击转交给「文档自己」去解析：

```
jump_from_click(world, &document, &position)
        │
        │  document.resolve_position(world, position)
        ▼
   <PagedDocument 或 HtmlDocument 的 resolve_position>
        │
        ▼
   Option<Jump>
```

这种「入口只分发、逻辑在各后端」的写法，正是面向后续要支持的后端扩展而做的抽象。

#### 4.2.3 源码精读

入口签名只有三行体：

[jump.rs:L33-L40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L33-L40) — `jump_from_click` 接收 `world`（数据来源）、`document: &D`（任意实现了 `JumpFromDocument` 的文档后端）、`position: &D::Position`（与后端配套的位置），整体直接委托给 `document.resolve_position(world, position)`。

三个参数各司其职：

- `world: &dyn IdeWorld`：典型 IDE 数据契约（见 u1-l2）。这里主要用来通过 `world.source(id)` 把命中图元的 span 解析回源码节点、以及 `world.range(span)` 取偏移。
- `document: &D`：被点击的渲染产物，`D` 受 `JumpFromDocument` 约束。
- `position: &D::Position`：**关联类型**。`D = PagedDocument` 时它就是 `PagedPosition`；`D = HtmlDocument` 时它就是 `HtmlPosition`。编译期保证「文档类型与位置类型匹配」。

注意入口**不接收 `output: Option<impl AsOutput>`**——这点和 tooltip/definition 不同。因为整个渲染文档本身就是「输出」，命中检测所需的所有信息（introspector、frame 树）都已经在 `document` 里了。

#### 4.2.4 代码实践

**目标**：验证「入口只分发」的极简性——它确实只有一行实现。

**步骤**：

1. 阅读 [jump.rs:L34-L40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L34-L40)，确认函数体只有 `document.resolve_position(world, position)` 一句。
2. 阅读 [jump.rs:L532-L543](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L532-L543) 的测试辅助 `test_click`：它先用 `typst::compile(world).output.unwrap()` 编译出 `PagedDocument`，再调 `jump_from_click(world, &doc, &PagedPosition{...})`。注意第三个参数类型 `PagedPosition` 正是 `D::Position` 在 `D = PagedDocument` 时的具体化。

**需要观察的现象**：测试里调用 `jump_from_click` 时，`document` 与 `position` 的类型是**配对出现**的——传 `&PagedDocument` 就只能传 `&PagedPosition`，传错类型编译不过。

**预期结果**：你能向自己解释清楚「为什么测试里写 `&doc`（PagedDocument）就强制要求 `PagedPosition`」——这正是关联类型在起作用。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `test_click` 改成传一个 `HtmlPosition` 给同一个 `&PagedDocument`，会发生什么？

> **答案**：编译失败。因为 `jump_from_click` 的约束是 `position: &D::Position`，当 `D = PagedDocument` 时 `D::Position = PagedPosition`，传 `&HtmlPosition` 类型不匹配。这就是关联类型提供的静态保证——无需运行时检查。若想测 HTML，要用 `test_click_html`（[jump.rs:L546-L556](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L546-L556)），那里 `D = HtmlDocument`、`position` 为 `HtmlPosition`。

**练习 2**：入口函数为什么不需要 `output` 参数（而 tooltip 的某些分支需要）？

> **答案**：tooltip/definition 只在「偶尔」需要编译产物时才用 `Option<impl AsOutput>`，且 world 本身可能还没渲染。而 `jump_from_click` 的前提就是「用户已经看到了渲染结果并点了它」，所以**整个渲染文档**必然已经存在，直接作为 `document` 参数传入即可，无需再用可选 `output`。

---

### 4.3 JumpFromDocument：用 sealed trait 抽象两种文档后端

#### 4.3.1 概念说明

`jump_from_click` 是泛型函数，泛型参数 `D` 必须实现 `JumpFromDocument`。这个 trait 的真正职责是：**告诉入口函数「我是一个可以被点击定位的文档后端」，并声明我的配套位置类型 `Position` 与定位逻辑 `resolve_position`。**

typst-ide 故意把 `JumpFromDocument` 做成 **sealed（封闭）**：只有 `PagedDocument` 和 `HtmlDocument` 这两个官方后端能实现它，外部 crate 无法为自己的文档类型实现。本节解释「为什么是 sealed trait」，以及 sealed 机制在代码上如何落地。

#### 4.3.2 核心流程：sealed trait 的两层结构

Rust 的 sealed trait 模式典型写法是「公开空壳 trait + 私有 supertrait」：

```
pub trait JumpFromDocument            ← crate 外可见，但空壳（无方法）
    : jump_from_document_sealed::JumpFromDocument   ← 私有 supertrait，真正放方法

mod jump_from_document_sealed {       ← 无 pub，crate 外不可见、不可命名
    pub trait JumpFromDocument {      ← 这才是「真身」
        type Position;
        fn resolve_position(&self, world, position: &Self::Position) -> Option<Jump>;
    }
    impl ... for PagedDocument { ... }
    impl ... for HtmlDocument  { ... }
}

impl JumpFromDocument for PagedDocument {}   ← 空实现：靠私有 supertrait 自动获得方法
impl JumpFromDocument for HtmlDocument  {}
```

为什么这样能「封死」？因为外部要实现公开的 `JumpFromDocument`，就必须先实现它的 supertrait `jump_from_document_sealed::JumpFromDocument`；而 `jump_from_document_sealed` 模块是私有的，外部**无法命名**这个 supertrait，自然也就无法实现它。

#### 4.3.3 源码精读

公开（但被封闭的）trait 声明与两个实现：

[jump.rs:L42-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L42-L48) — 公开 trait `JumpFromDocument` 自身是空壳，方法全在私有 supertrait 里；下方两行只为 `PagedDocument`、`HtmlDocument` 写空实现（`{}`），它们靠私有 supertrait 的 impl 自动获得 `type Position` 与 `resolve_position`。

「真身」所在的私有模块与 trait：

[jump.rs:L50-L68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L50-L68) — 私有模块 `jump_from_document_sealed` 里定义真正的 trait：带关联类型 `type Position;` 与唯一方法 `resolve_position`。模块没有 `pub`，外部 crate 既看不到也命名不了它，这就是「封口」的关键。

两个后端的 `resolve_position` 实现分别见 4.4 节。

#### 4.3.4 为什么是 sealed trait，而不是泛型函数或 enum？

这是本讲的核心设计问题，逐条对比：

**（A）为什么不直接用 enum 统一两种文档？**

设想把两种文档塞进一个 enum：

```rust
// 假想的（非项目真实）方案 —— 仅为对比
pub enum AnyDocument { Paged(PagedDocument), Html(HtmlDocument) }
```

问题在于**位置类型不同**。`PagedPosition` 是「页号 + 坐标」，`HtmlPosition` 是「DOM 路径 + 偏移」，两者结构毫无共性。如果用 enum，入口签名就只能退化为：

```rust
fn jump_from_click(world, doc: &AnyDocument, position: &DocumentPosition) -> Option<Jump>
//                                                                ^^^^^^^^^^^^^^^^^
//                  必须用一个 union 类型 DocumentPosition，并在内部 match
```

这样会**丢失静态类型配对**——编译器无法阻止你把 `PagedDocument` 和 `HtmlPosition` 错配，只能在运行时 `match` 检查。而关联类型 `type Position` 把「文档类型 ↔ 位置类型」的绑定交给类型系统，传错直接编译失败（见 4.2.5 练习 1）。所以 enum 方案在类型安全上劣于 trait + 关联类型。

**（B）为什么不直接开放 trait（允许外部实现）？**

把 `JumpFromDocument` 设成普通开放 trait 技术上可行，但 typst-ide 选择封闭它，理由有三：

1. **表达「封闭集合」的意图**：官方只支持这两种后端。sealed 是在类型层面对「这就是全部」的承诺。
2. **保留演进自由**：日后若要给 `resolve_position` 增加方法或调整签名，由于没有外部实现者，这不是破坏性变更（breaking change）。
3. **与反向跳转对称**：`jump_from_cursor` 用了完全平行的 `JumpInDocument` sealed trait（[jump.rs:L367-L416](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L367-L416)），两个方向用同一套抽象风格，降低阅读成本。

**（C）为什么不用两个独立的自由函数（`jump_from_click_paged` / `jump_from_click_html`）？**

那样会膨胀公共 API（导出两个几乎同形的函数），并迫使调用方自己按后端类型分支选择。sealed trait + 泛型入口给出**一个**干净的 `jump_from_click`，由编译器单态化（monomorphization）为两份高效代码，兼顾「API 简洁」与「零成本抽象」。

> 小结：**关联类型**解决了「位置类型随后端而变且需静态配对」；**sealed** 解决了「封闭集合、保留演进自由」；**泛型入口**解决了「单一 API + 单态化零成本」。三者合起来，正是这个设计的精妙之处。

#### 4.3.5 代码实践

**目标**：亲手验证 sealed 的封闭性。

**步骤**：

1. 在 [jump.rs:L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L44) 确认公开 trait 的 supertrait bound 指向私有模块 `jump_from_document_sealed`。
2. 在 [jump.rs:L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L50) 确认 `mod jump_from_document_sealed` 没有 `pub`。
3. 思维实验（**待本地验证**，可作为本 crate 之外的小练习）：如果在一个**外部** crate 里写 `impl JumpFromDocument for MyDoc {}`，编译器会报缺少 supertrait 实现，而该 supertrait 又不可命名——因此无法编译通过。

**需要观察的现象**：trait 的「方法」与公开空壳在物理上分离（方法在私有模块、空壳在公有层）。

**预期结果**：你能向他人讲清「外部 crate 无法实现 `JumpFromDocument`」的根因是私有 supertrait 不可命名，而不是某个运行时检查。

#### 4.3.6 小练习与答案

**练习 1**：如果 typst 团队未来想给 `JumpFromDocument` 增加一个 `fn page_count(&self) -> Option<usize>` 方法，加在公开 trait 还是私有 supertrait 里更合适？为什么？

> **答案**：加在私有 supertrait（`jump_from_document_sealed::JumpFromDocument`）里。因为这是真实方法，公开 trait 只是空壳。又因为 trait 被 sealed，没有外部实现者，新增方法不会破坏下游编译——这正是 sealed 保留「演进自由」的价值所在。

**练习 2**：`JumpFromDocument` 与 `JumpInDocument`（反向跳转用的 sealed trait）在结构上高度对称。说出它们各自唯一的关联方法名。

> **答案**：`JumpFromDocument` 的方法是 `resolve_position`（点击位置 → Jump 终点）；`JumpInDocument` 的方法是 `find_span`（源码 span → 渲染位置列表）。前者是「渲染→源码」，后者是「源码→渲染」，方向相反（详见 u7-l3）。

---

### 4.4 resolve_position：从位置定位到渲染元素

#### 4.4.1 概念说明

`resolve_position` 是 sealed trait 的唯一方法，职责是「**给定一次点击的位置，算出该点击落在哪个源码元素上（或哪个链接上），返回 `Jump`**」。两种后端实现差异很大：

- `PagedDocument`：极简——取出对应页的 `frame`，把点击坐标交给 frame 命中检测。
- `HtmlDocument`：较繁——沿 DOM 索引路径逐层下钻到目标节点，再按节点是 Text / Frame 分别处理。

本节重点讲 `PagedDocument` 的实现（这也是实践任务要求描述的流程），并简述 HTML 实现作对比。

#### 4.4.2 核心流程（PagedDocument）

`PagedDocument::resolve_position` 的数据流：

```
输入: position = PagedPosition { page: NonZeroUsize(1起), point: Point }

  1. position.page.get() - 1        // 1 基页号 → 0 基下标（NonZero 保证 ≥1，减法安全）
            │
            ▼
  2. self.pages().get(idx)          // 在 &[Page] 里按下标取页；越界 → None
            │  page: &Page
            ▼
  3. click = position.point         // 取出页面内点击坐标（左上角为原点）
            │
            ▼
  4. jump_from_click_in_frame(world, self, &page.frame, click)
            │  // self 作为 impl AsOutput（PagedDocument: Output）
            │  // 在 frame 树里做命中检测（u7-l2 详讲）
            ▼
输出: Option<Jump>  // File / Url / Position，或 None
```

页号转换有一个小巧但关键的数学保证。`page` 是 `NonZeroUsize`，其取值恒满足：

\[
\text{page} \geq 1 \quad\Rightarrow\quad \text{page} - 1 \geq 0
\]

因此 `position.page.get() - 1` 永远不会下溢（underflow），1 基到 0 基的转换是安全的；下标越界则由 `.get()` 返回 `None` 兜底。

#### 4.4.3 源码精读

`PagedDocument` 的 `resolve_position` 只有四行：

[jump.rs:L70-L82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L70-L82) — `type Position = PagedPosition`；实现里先把 1 基页号减 1 转下标，用 `.get()` 安全取页（越界返回 `None`），取出点击坐标 `point`，最后把 `self`（`PagedDocument`，实现了 `Output`/`AsOutput`）与该页的 `&page.frame` 一起交给 `jump_from_click_in_frame` 做命中检测。

几个要点：

- **`self.pages()`**：`PagedDocument` 内部存了 `pages: EcoVec<Page>`，`pages()` 返回 `&[Page]`。每个 `Page` 都持有一个 `frame: Frame`（见 `typst-layout/src/document.rs`）。
- **`self` 作 `impl AsOutput`**：`jump_from_click_in_frame` 的第二参数是 `impl AsOutput`（[jump.rs:L209-L214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L209-L214)）。`AsOutput` 是 `fn as_output(&self) -> &dyn Output` 的 trait，`PagedDocument` 实现了 `Output`，于是 `self` 可直接传入。命中检测内部用 `output.as_output().introspector()` 把 `Destination::Location` 这类「具名定位」解析成页面坐标（[jump.rs:L225-L230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L225-L230)）。
- **`PagedPosition` 的结构**：

  [`position.rs:L59-L66`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/position.rs#L59-L66) — `PagedPosition { page: NonZeroUsize, point: Point }`，`page` 从 1 开始计数，`point` 为页面左上角原点下的二维坐标。

作为对比，`HtmlDocument::resolve_position` 要复杂得多：

[jump.rs:L84-L205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L84-L205) — `type Position = HtmlPosition`；实现沿 `position.element()` 的 DOM 索引路径逐层下钻（用 `iter_with_dom_indices` 匹配 dom_index），到叶子节点后按 Text（带字符偏移修正，处理 figure caption 那种「多个 spanless 文本节点拼接」的情况）或 Frame（再委托 `jump_from_click_in_frame`）分别产出 `Jump::File`。这段是 HTML 后端专有逻辑，本讲只作对照，不展开。

#### 4.4.4 代码实践

**目标**：用真实测试追踪一次点击「从 `PagedPosition` 到 frame」的完整流程（实践任务的核心）。

**步骤**：

1. 定位测试辅助 `test_click`：

   [jump.rs:L532-L543](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L532-L543) — `test_click` 把一段 Typst 源码编译成 `PagedDocument`，再用 `PagedPosition { page: NonZeroUsize::ONE, point: click }`（即第 1 页、给定坐标）调用 `jump_from_click`。

2. 追踪一个具体用例，例如 `test_jump_from_click`：

   [jump.rs:L577-L585](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L577-L585) — 源码 `"*Hello* #box[ABC] World"`，点击 `(72.0, 10.0)` 期望落到源码偏移 `20`（即 `World` 的 `W`）。

3. **画出该次调用的流程**（建议手写在笔记上）：

   ```
   jump_from_click(world, &doc, &PagedPosition{page:1, point:(72,10)})
     → doc.resolve_position(world, position)
        page = pages().get(1-1 = 0)         // 取第 1 页
        click = (72, 10)
        jump_from_click_in_frame(world, doc, &pages[0].frame, (72,10))
          → 在 frame 内命中 "W" 这个字形
          → Jump::File(main_id, 20)
   ```

4. 运行验证（**待本地验证**）：

   ```bash
   cargo test -p typst-ide --lib test_jump_from_click
   ```

**需要观察的现象**：`page.get() - 1` 把 1 基页号变 0 基下标；`self` 被当作 `impl AsOutput` 透传给 frame 命中函数；最终终点是 `Jump::File`。

**预期结果**：测试通过；你能用自己的话复述「页号减一 → 取页 → 取坐标 → 委托 frame 命中」这四步。

#### 4.4.5 小练习与答案

**练习 1**：若用户在只有 1 页的文档里点击了 `PagedPosition { page: 3, point: ... }`，`resolve_position` 会返回什么？为什么不会 panic？

> **答案**：返回 `None`。因为 `pages().get(3 - 1)` 即 `get(2)` 在长度为 1 的切片里越界，返回 `None`，`?` 提前返回。减法 `3 - 1 = 2` 本身因 `NonZeroUsize` 保证不会下溢；越界由 `.get()` 安全兜底，所以不会 panic。

**练习 2**：`PagedDocument::resolve_position` 把 `self` 直接传给 `jump_from_click_in_frame(world, self, &page.frame, click)`，这里 `self` 满足的是哪个 trait bound？为什么 `PagedDocument` 能满足它？

> **答案**：满足 `impl AsOutput`。`AsOutput` 定义为 `fn as_output(&self) -> &dyn Output`（`foundations/target.rs`），并对任意 `&T where T: Output` 做了 blanket impl。因为 `PagedDocument` 实现了 `Output`，所以 `&self`（即 `&PagedDocument`）自动满足 `AsOutput`，可直接传入。命中函数内部再通过 `output.as_output().introspector()` 取内省器来解析 `Location` 类链接。

**练习 3**：HTML 后端的 `resolve_position` 在叶子节点是 `HtmlNode::Frame` 时，复用了 Paged 后端的哪段逻辑？

> **答案**：复用了 `jump_from_click_in_frame`（[jump.rs:L182-L186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L182-L186)）。当 DOM 路径定位到的节点是一个内嵌 frame（如 `#html.frame($...$)` 里的数学公式），HTML 后端用 `InnerHtmlPosition::Frame(point)` 携带的 frame 内坐标，调用同一个 frame 命中检测函数。这说明 frame 命中检测是两种后端共享的公共能力（u7-l2 详讲）。

## 5. 综合实践

把本讲四个模块串起来，完成下面这份「点击跳转入口层」分析笔记：

**任务背景**：假设你要向团队介绍「点击 PDF 里的一个字为什么会跳回源码」，但只允许讲到「入口与后端抽象」这一层，frame 内部算法留到下次。

**请完成**：

1. **API 对照表**：列出 `jump_from_click` 的三个参数及其类型，并标注其中哪个是关联类型、它随 `D` 如何变化。
2. **Jump 终点表**：用表格列出 `Jump::File / Url / Position` 各自的触发场景与承载信息，并各举一个 Typst 源码示例（提示：`#link("https://...")`、`#footnote[...]`、普通正文文字）。
3. **流程图**：画出 `PagedDocument::resolve_position` 从 `PagedPosition` 到 `jump_from_click_in_frame` 的四步数据流，标出「1 基→0 基」「`self` 作 `AsOutput`」两处关键点。
4. **设计论证**（实践任务核心）：用 200 字左右论证「为什么 typst-ide 用 sealed trait 统一两种后端，而不是 enum 或两个自由函数」。要求至少提到「关联类型提供静态配对」「sealed 封闭集合保留演进自由」「泛型入口单态化零成本」三点。
5. **验证**：运行 `cargo test -p typst-ide --lib test_jump_from_click` 与 `test_footnote_links`（**待本地验证**），把断言里的 `cursor(...)` / `pos(...).map(Jump::Position)` 与你表中的 `File` / `Position` 终点一一对应。

完成本任务后，你就掌握了「点击跳转」入口层的全部设计；frame 内部如何命中字形/图形/链接，留给 u7-l2。

## 6. 本讲小结

- `Jump` 是跳转终点的枚举，只有三类：`File(FileId, usize)`（跳回源码）、`Url(Url)`（外链）、`Position(PagedPosition)`（页内坐标，仅分页后端）。
- 公共入口 `jump_from_click<D: JumpFromDocument>` 本身只有一行——把点击委托给 `document.resolve_position(world, position)`，是纯粹的「分发层」。
- 泛型参数 `D` 带关联类型 `type Position`，把「文档类型 ↔ 位置类型」的配对交给类型系统静态保证（传 `PagedDocument` 就必须配 `PagedPosition`）。
- `JumpFromDocument` 用 **sealed trait** 封闭：公开空壳 + 私有 supertrait，使外部 crate 无法实现，只保留 `PagedDocument`、`HtmlDocument` 两个官方后端。
- sealed（而非 enum/自由函数）的取舍：关联类型解决静态配对、sealed 保留演进自由、泛型入口单态化零成本——三者缺一不可。
- `PagedDocument::resolve_position` 四步走：页号减一（1 基→0 基）→ `.get()` 取页 → 取 `point` 坐标 → 委托 `jump_from_click_in_frame(world, self, &page.frame, click)`，其中 `self` 因 `PagedDocument: Output` 而满足 `impl AsOutput`。

## 7. 下一步学习建议

- **u7-l2（frame 内点击命中检测）**：本讲把 `jump_from_click_in_frame` 当作黑盒。下一讲进入它的内部，看它如何逆序遍历 frame、命中文字字形（含字符前/后半判断）、图形 fill/stroke、图片、以及带 transform/clip 的 group。
- **u7-l3（jump_from_cursor 与源码→渲染映射）**：与本讲方向相反——从源码光标反查渲染位置，复用本讲的 `JumpInDocument` sealed trait（`find_span`）。
- **拓展阅读**：对照 `crates/typst-library/src/introspection/position.rs` 全文，理解 `DocumentPosition` / `PagedPosition` / `HtmlPosition` / `InnerHtmlPosition` 这组位置类型的整体设计；以及 `crates/typst-library/src/foundations/target.rs` 里 `AsOutput` / `Output` / `Target` 如何统一「编译目标」抽象。
