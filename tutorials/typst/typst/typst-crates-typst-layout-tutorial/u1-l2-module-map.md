# 目录结构与模块地图

## 1. 本讲目标

上一篇（u1-l1）我们建立了 typst-layout 的定位：它是 Typst 的分页排版引擎，把已经 realized 的 `Content` 排成 `PagedDocument`。本讲我们要打开它的源码目录，回答三个问题：

1. 这个 crate 的源码由哪些文件、哪些目录组成？
2. 这些文件如何被组织成「子系统」？每个子系统负责什么？
3. 子系统之间是怎么互相调用的——尤其是哪些 layouter 会「回调」`layout_fragment` / `layout_frame`？

学完后你应当能够：

- 看着 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs) 的 `mod` 声明说出每个模块的大致职责；
- 把任意一个 `.rs` 文件归入对应子系统；
- 画出一张「哪些 layouter 会回调 fragment/frame」的调用关系草图；
- 理解 `Page` / `PagedDocument` 作为最终产物在整个地图中的位置。

## 2. 前置知识

阅读本讲前，请确认你已经理解上一篇讲义（u1-l1）的几个结论：

- **typst-layout 是流水线中段**：上游 typst-realize 产出 `Content`，本 crate 把它排成 `PagedDocument`，再交给 typst-pdf / typst-svg 导出。它既不读源码也不写 PDF。
- **lib.rs 只导出 7 个公共符号**：`Page`、`PagedDocument`、`layout_document`、`layout_document_for_bundle`、`layout_fragment`、`layout_frame`、`PagedIntrospector`、`register`。
- **入口函数普遍采用「公开函数 + `#[comemo::memoize]` 的 `_impl` 函数」模式**。

本讲还会用到两个 Rust 语法概念，初学者可能不熟：

- **`mod xxx;` 声明**：告诉编译器「去 `xxx.rs` 或 `xxx/mod.rs` 找这个模块的实现」。它是整个源码目录的「索引页」。
- **`pub use`**：把某个模块内部的符号「重新导出」到 crate 根，让外部使用者能直接 `use typst_layout::Page` 这样引用，而不必关心它实际定义在哪个文件里。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 |
| --- | --- |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs) | crate 根：16 条 `mod` 声明 + 5 条 `pub use`，是整个源码的索引 |
| [`src/document.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs) | 产物定义：`PagedDocument` 与 `Page` |

此外，本讲会从「目录视角」扫过 `src/pages/`、`src/flow/`、`src/inline/`、`src/grid/`、`src/math/` 五个子目录以及若干顶层支撑文件。这些文件的内部实现是后续讲义的主题，本讲只讲它们「属于哪个子系统、做什么」。

## 4. 核心概念与源码讲解

### 4.1 lib.rs 的 mod 声明：源码目录的索引

#### 4.1.1 概念说明

一个 Rust crate 的根文件（`lib.rs` 或 `main.rs`）通常有两类语句构成它的「骨架」：

- `mod xxx;` —— 声明子模块，指明源码物理上拆成了哪些文件；
- `pub use ...` —— 决定哪些符号对外可见。

typst-layout 的 `lib.rs` 极其精简：它本身**不含任何排版逻辑**，只负责把 16 个子模块串起来，并导出 7 个公共 API。所以读这个文件，等于在读「整个 crate 的目录索引」。

#### 4.1.2 核心流程

```
lib.rs
 ├── 16 条 mod 声明  →  指向 16 个模块（文件或目录）
 └── 5 条 pub use    →  导出 7 个公共符号（见 u1-l1）
```

也就是说，`lib.rs` 是一张「目录表」。理解 typst-layout 的第一步，就是把这张目录表读懂。

#### 4.1.3 源码精读

[`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs#L3-L18) 的全部 `mod` 声明如下：

```rust
mod document;
mod flow;
mod grid;
mod image;
mod inline;
mod introspect;
mod lists;
mod math;
mod modifiers;
mod pad;
mod pages;
mod repeat;
mod rules;
mod shapes;
mod stack;
mod transforms;
```

紧接着是 5 条 [`pub use`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs#L20-L24)，它们把内部模块的符号重新导出到 crate 根：

```rust
pub use self::document::{Page, PagedDocument};
pub use self::flow::{layout_fragment, layout_frame};
pub use self::introspect::PagedIntrospector;
pub use self::pages::{layout_document, layout_document_for_bundle};
pub use self::rules::register;
```

这里有一个关键观察：**被 `pub use` 导出的模块只有 4 个**（document / flow / introspect / pages / rules），但 `mod` 声明却有 16 个。这意味着另外 11 个模块（grid、math、inline、stack、lists、shapes、transforms、image、pad、repeat、modifiers）**对外是不可见的**——它们不直接被外部 crate 调用，而是通过 `rules.rs` 注册成 show 规则后被间接驱动。这一点在 u1-l1 已经提到：`register` 是 layout 与 library 的「粘合层」。

#### 4.1.4 代码实践

1. **实践目标**：把 `lib.rs` 的 16 个 `mod` 与磁盘上的实际文件一一对应。
2. **操作步骤**：在项目根目录执行 `ls src/` 和 `ls src/flow src/pages src/inline src/grid src/math`，对照上面的 `mod` 列表。
3. **需要观察的现象**：`mod flow;` 对应的是一个目录 `src/flow/`（里面有 `mod.rs`），而 `mod image;` 对应的是单个文件 `src/image.rs`。
4. **预期结果**：你会发现 typst-layout 的 16 个模块中，5 个是目录（含子模块）、11 个是单文件。
5. **结论**：Rust 模块「同名时优先取 `xxx/mod.rs` 或 `xxx.rs`」——这正是后续子系统地图的基础。

#### 4.1.5 小练习与答案

**练习 1**：`lib.rs` 里没有 `pub use self::grid::...`，那 grid 模块是如何被调用的？

> **参考答案**：grid 不直接暴露给外部，而是由 `rules.rs` 的 `register` 把 `GRID_RULE` / `TABLE_RULE` 注册到 `NativeRuleMap` 的 `Target::Paged` 上；当排版遇到 `grid` / `table` 元素时，show 规则会驱动 `src/grid/mod.rs` 里的 `layout_grid` / `layout_table`。

**练习 2**：为什么 `lib.rs` 顶部没有任何业务代码，只有 `mod` 和 `pub use`？

> **参考答案**：因为它是「索引页」，职责是组织模块、控制对外可见性，把实现分散到各子模块，便于维护和阅读。

---

### 4.2 七大子系统心智模型

#### 4.2.1 概念说明

光有 16 个文件的清单还不够，我们需要把它们归类成**心智模型**——也就是脑子里能拎得清的几个「大块」。typst-layout 的源码自然地分成「六大核心子系统 + 一层支撑模块」：

- **document**：产物层，定义排版结果。
- **pages**：文档/页面层，把 content 切成若干页并排版。
- **flow**：块级流，处理段落、块、列、脚注、浮动体。
- **inline**：行内段落，处理文本整形、BiDi、断行。
- **grid**：网格与表格。
- **math**：数学公式。
- **支撑层**：stack、lists、shapes、transforms、image、pad、repeat、modifiers、introspect、rules——它们要么被核心子系统复用，要么是横切关注点。

#### 4.2.2 核心流程

下图给出「子系统 → 文件」的归类（数字代表该子系统的入口函数所在行号，供后续精读）：

```
document (产物)        ── document.rs
pages   (文档/页面)    ── pages/{mod, collect, run, finalize}.rs
flow    (块级流)        ── flow/{mod, block, collect, compose, distribute}.rs
inline  (行内段落)      ── inline/{mod, box, collect, prepare, linebreak,
                             shaping, line, deco, finalize}.rs
grid    (网格/表格)     ── grid/{mod, layouter, lines, repeated, rowspans}.rs
math    (数学公式)      ── math/{mod, fragment/, accent, cancel, fenced,
                             fraction, line, radical, run, scripts,
                             shaping, table, text}.rs
─────────────────────────────────────────────────────────────────
支撑层：stack lists shapes transforms image pad repeat modifiers
        introspect rules
```

#### 4.2.3 源码精读

下面这张表把每一个 `.rs` 文件归入子系统，并给出它的职责与入口。你可以把它当作整本手册的「速查表」。

| 子系统 | 文件 | 职责（一句话） | 入口函数 |
| --- | --- | --- | --- |
| document | [`document.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs) | 定义最终产物 `PagedDocument` 与 `Page` | `PagedDocument::new` |
| pages | [`pages/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs) | 文档级入口，realize 后切分 page run | `layout_document` |
| pages | `pages/collect.rs` | 把扁平 children 切成 page run / 标签 / 奇偶页 | `collect` |
| pages | `pages/run.rs` | 排版单页：边距、页眉页脚、正文 | `layout_page_run` |
| pages | `pages/finalize.rs` | 按物理页号组装最终 `Page` | `finalize` |
| flow | [`flow/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs) | 片段级入口 + 块级流主循环 | `layout_fragment` |
| flow | `flow/collect.rs` | 把 Pair 预处理成更易处理的 `Child` | `collect` |
| flow | `flow/distribute.rs` | 把 child 逐个填入当前 region | `distribute` |
| flow | `flow/compose.rs` | 处理浮动体、脚注等 out-of-flow 插入 | `compose` |
| flow | `flow/block.rs` | 单块 / 可断裂多块布局 | `layout_single_block` / `layout_multi_block` |
| inline | [`inline/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs) | 段落排版四段管线 | `layout_par` |
| inline | `inline/collect.rs` | 文本收集成字符串 + segments | `collect` |
| inline | `inline/prepare.rs` | BiDi 分析与预处理 | `prepare` |
| inline | `inline/linebreak.rs` | 断行（simple / optimized） | `linebreak` |
| inline | `inline/shaping.rs` | 文本整形（rustybuzz） | `shape_range` |
| inline | `inline/line.rs` | 单行构建与对齐 | `line` / `commit` |
| inline | `inline/deco.rs` | 下划线/删除线等装饰 | `decorate` |
| inline | `inline/finalize.rs` | 行组装成段落 frame | `finalize` |
| inline | `inline/box.rs` | 行内盒 `#box` | `layout_box` |
| grid | [`grid/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs) | 网格/表格布局入口 | `layout_grid` / `layout_table` / `layout_cell` |
| grid | `grid/layouter.rs` | `GridLayouter` 主排布器 | `GridLayouter::layout` |
| grid | `grid/rowspans.rs` | 跨行单元格 | `Rowspan` / `UnbreakableRowGroup` |
| grid | `grid/repeated.rs` | 重复表头状态机 | （内部） |
| grid | `grid/lines.rs` | hline/vline 线段生成 | `generate_line_segments` |
| math | [`math/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs) | 公式排版入口（行内/块级） | `layout_equation_inline` / `layout_equation_block` |
| math | `math/fragment/mod.rs` | `MathFragment` 等片段类型 | （内部） |
| math | `math/fragment/glyph.rs` | 单字片段 | （内部） |
| math | `math/{fraction,radical,scripts,accent,fenced,cancel,table,line,run,text,shaping}.rs` | 各类数学结构 | 对应 `layout_*` |
| 支撑 | [`stack.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs) | 主/交叉轴通用栈，被 lists 复用 | `layout_stack` |
| 支撑 | [`lists.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs) | 列表/枚举/术语 | `layout_list` / `layout_enum` / `layout_terms` |
| 支撑 | [`shapes.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs) | line/rect/circle/polygon/curve | `layout_line` 等 |
| 支撑 | [`transforms.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs) | move/rotate/scale/skew | `layout_move` 等 |
| 支撑 | [`image.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/image.rs) | 图片 | `layout_image` |
| 支撑 | [`pad.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pad.rs) | 内边距 | `layout_pad` |
| 支撑 | [`repeat.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/repeat.rs) | 重复内容 | `layout_repeat` |
| 支撑 | [`modifiers.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/modifiers.rs) | `FrameModifiers`（hide/link）横切关注点 | `FrameModifiers::get_in` |
| 支撑 | [`introspect.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs) | `PagedIntrospector` 查询索引 | `PagedIntrospector::new` |
| 支撑 | [`rules.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs) | show 规则注册 | `register` |

> 提示：表中「入口函数」大多是后续讲义的主角，本讲只需知道它们存在、在哪个文件。

#### 4.2.4 代码实践

1. **实践目标**：把子目录的内部文件也归入子系统。
2. **操作步骤**：分别打开 `flow/mod.rs`、`pages/mod.rs`、`inline/mod.rs`、`grid/mod.rs`、`math/mod.rs` 的开头几行，看它们各自的 `mod` 声明。例如 [`flow/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L3-L6) 只有 4 条：
   ```rust
   mod block;
   mod collect;
   mod compose;
   mod distribute;
   ```
3. **需要观察的现象**：每个子系统的 `mod.rs` 既是该子系统的「内部索引」，也定义了对外的入口函数。
4. **预期结果**：你能不查表，说出 `flow/compose.rs`、`inline/shaping.rs`、`grid/repeated.rs` 分别属于哪个子系统。

#### 4.2.5 小练习与答案

**练习 1**：`inline/mod.rs` 顶部有这样两行，它们是什么意思？
```rust
#[path = "box.rs"]
mod box_;
```

> **参考答案**：这是「重命名 + 指定路径」的模块声明。`box` 是 Rust 关键字，不能直接当模块名，所以用 `box_` 作为模块名，并用 `#[path = "box.rs"]` 让编译器去 `box.rs` 取实现。

**练习 2**：`modifiers.rs` 为什么被称作「横切关注点（cross-cutting concern）」？

> **参考答案**：因为 `hide` / `link` 这类样式不引入任何布局结构，却必须被多个手动管理样式的 layouter（flow、inline、math）统一应用，否则会出现「部分子内容没有被隐藏/链接」的错误。它「横跨」多个子系统，所以单独成模块。参见 [`modifiers.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/modifiers.rs#L5-L19) 的文档注释。

---

### 4.3 document 模块：排版产物 PagedDocument 与 Page

#### 4.3.1 概念说明

所有排版工作的终点，就是 [`document.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs) 里的两个结构体：

- `PagedDocument`：一份完整文档，由若干 `Page` 加上文档信息和内省器组成；
- `Page`：单页，核心是一个 `Frame`（二维画面），外加 bleed（出血）、fill（背景）、numbering（页码）等导出时需要的元数据。

理解这两个结构体的字段，就理解了「typst-layout 最终产出什么」。

#### 4.3.2 核心流程

```
PagedDocument::new(pages, info)
   ├── 存储pages (EcoVec<Page>)
   ├── 存储info   (DocumentInfo)
   └── 立即构建 PagedIntrospector 并用 Arc 包起来
```

`Page` 本身只是数据容器，它由 `pages/finalize.rs` 的 `finalize` 填好字段后产出（这是 u3 单元的主题）。

#### 4.3.3 源码精读

[`PagedDocument`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L16-L21) 的三个字段：

```rust
pub struct PagedDocument {
    pages: EcoVec<Page>,
    info: DocumentInfo,
    introspector: Arc<PagedIntrospector>,
}
```

[`PagedDocument::new`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L27-L30) 在构造时就构建内省器：

```rust
pub fn new(pages: EcoVec<Page>, info: DocumentInfo) -> Self {
    let introspector = PagedIntrospector::new(&pages);
    Self { pages, info, introspector: Arc::new(introspector) }
}
```

注意注释强调「Internally builds the introspector」——内省器是 `pages` 的**纯派生物**。这一点也体现在 [`Hash`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L48-L55) 实现里：它**只哈希 `pages` 和 `info`，不哈希 introspector**，因为 introspector 完全由这两者决定。这是 comemo 缓存能正确工作的前提。

[`Page`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L82-L105) 的字段揭示了「一页」包含什么：

```rust
pub struct Page {
    pub frame: Frame,                 // 二维画面，真正的排版内容
    pub bleed: Sides<Abs>,            // 出血，导出时在四周附加
    pub fill: Smart<Option<Paint>>,   // 背景填充
    pub numbering: Option<Numbering>, // 页码
    pub supplement: Content,          // 页面 supplement（用于 "page" 文案）
    pub number: u64,                  // 逻辑页号（counter(page) 控制）
}
```

其中 `frame: Frame` 才是排版的「画面」，其余字段都是导出器（PDF/SVG/光栅）需要的边带信息。

最后，`PagedDocument` 实现了 [`Output`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L63-L79) trait，它的 `target()` 返回 `Target::Paged`，`create()` 则回调 `crate::layout_document`。这正是 typst-library 通过统一 `Output` 抽象触发本 crate 的地方。

#### 4.3.4 代码实践

1. **实践目标**：确认「introspector 不参与 Hash」对缓存的含义。
2. **操作步骤**：阅读 [`Hash` 实现](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L48-L55)及其上方注释。
3. **需要观察的现象**：`hash` 方法体里只出现 `self.pages` 和 `self.info`，没有 `self.introspector`。
4. **预期结果**：理解「两个 `PagedDocument` 只要 pages/info 相同，Hash 就相同」——这让 comemo 可以在 pages 没变时复用缓存，即使 introspector 内部结构变化也无妨。
5. **待本地验证**：若你想直观感受，可在 `PagedDocument::new` 临时加一条 `eprintln!("built introspector over {} pages", pages.len());`，编译后用 typst CLI 编译任意文档，观察输出。注意：本实践涉及临时改动源码，仅用于本地学习，完成后请还原。

#### 4.3.5 小练习与答案

**练习 1**：`Page::number` 是「物理页号」还是「逻辑页号」？两者何时不一致？

> **参考答案**：字段注释写明它是「逻辑页号」，由 `counter(page)` 控制，因而可能与物理页号（在 `pages` 数组里的下标 +1）不一致。典型例子：用 `set page numbering: "1"` 配合 `counter(page).update(...)` 跳号，或用 `pagebreak(weak: true)` 产生空白页时，物理页存在但逻辑页号可能被跳过。物理页号的最终敲定在 `pages/finalize.rs`（见 u3-l4）。

**练习 2**：为什么 `Page::fill` 用 `Smart<Option<Paint>>` 而不是直接 `Option<Paint>`？

> **参考答案**：需要区分三种状态——`Auto`（PDF 透明 / 光栅与 SVG 白底）、`None`（明确透明）、`Some(color)`（指定颜色）。`Smart` 多出来的「Auto」档位让导出器能按目标格式自行决定，见 [`fill_or_transparent`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L107-L113) 与 [`fill_or_white`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L115-L120)。

---

### 4.4 容器型布局器与「回调 layout_fragment」现象

#### 4.4.1 概念说明

typst-layout 有一个非常重要的递归现象：**很多 layouter 在排版自己的子内容时，会回调 `crate::layout_fragment` 或 `crate::layout_frame`**。

为什么？因为「把任意 content 排版成 frame/fragment」是一个已经实现好的、通用能力（它正是 `flow` 模块提供的入口）。所以当一个容器（grid 单元格、math 公式、stack 子项、变换元素、pad、repeat）需要排子内容时，它不必自己重新实现一遍排版，而是直接调用这两个入口，把子内容的排版「委托」给 flow。

这就是为什么 [`flow/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51) 的 `layout_frame` / `layout_fragment` 既是「片段级公共 API」，又是「内部布局器的复用基石」。

#### 4.4.2 核心流程

```
layout_document (pages)
        │ 排版每页正文时调用
        ▼
   layout_flow  ──────┐ 排版段落/块时调用
        │             │
        ▼             │
   layout_par (inline)│ 行内盒 box 时可回调
        │             │
        ▼             │
   ┌──── 容器型布局器都「回调」flow 的入口 ────┐
   │  grid/layout_cell  → 排单元格内容          │
   │  math/mod.rs       → 排公式里的 realized 子内容 │
   │  stack.rs          → 排栈子项（或用自定义 layouter）│
   │  transforms.rs     → 先排再变换             │
   │  pad.rs            → 先排再加边距            │
   │  repeat.rs         → 排一个 piece 再重复铺满   │
   │  shapes.rs         → 部分几何需排子内容        │
   │  inline/box.rs     → 行内盒排子内容           │
   └────────────────────────────────────────────┘
```

注意：`layout_flow` 既是「主块级管线」，也会被 `pages/run.rs` 调用来排页面正文——所以它同时处于「被 pages 调用」和「被容器回调」两条边上。

#### 4.4.3 源码精读

最直观的例子是 [`repeat.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/repeat.rs#L12-L20)：它先排出一个「碎片」，再重复铺满区域：

```rust
pub fn layout_repeat(...) -> SourceResult<Frame> {
    let pod = Region::new(region.size, Axes::new(false, false));
    let piece = crate::layout_frame(engine, &elem.body, locator, styles, pod)?;
    // ... 用 piece 重复填满 region
}
```

这里 `crate::layout_frame` 就是回调。类似地：

- [`grid/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L30-L50) 的 `layout_cell` 在排每个单元格时回调 fragment，并且有一段著名的 HACK 注释——它**手动为 table/grid 单元格生成 tag**：
  ```rust
  // HACK: manually generate tags for table and grid cells. Ideally table and
  // grid cells could just be marked as locatable, but the tags are somehow
  // considered significant for layouting.
  ```
  这说明 grid 不是简单地委托排版，还要为内省（query/counter）做特殊处理。
- [`math/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L51-L52) 的 `layout_equation_inline`（行内）和 [`layout_equation_block`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/mod.rs#L106-L108)（块级）在遇到已 realized 的子内容时也会回调。
- [`pad.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pad.rs#L11-L17)、[`transforms.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/transforms.rs#L13-L20)、[`stack.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L13-L20) 同样遵循「先排子内容（回调 fragment/frame），再做几何变换」的模式。

用 grep 在 `src/` 下搜索 `crate::layout_fragment|crate::layout_frame`，会命中 14 个文件，正好印证这张调用图。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「回调现象」覆盖了哪些 layouter。
2. **操作步骤**：在 crate 根目录执行下面的搜索（使用 ripgrep）：
   ```bash
   rg -n "crate::layout_fragment|crate::layout_frame" src
   ```
3. **需要观察的现象**：输出会命中 `pages/run.rs`、`flow/{block,collect,compose}.rs`、`grid/mod.rs`、`math/mod.rs`、`stack.rs`、`lists.rs`、`shapes.rs`、`transforms.rs`、`pad.rs`、`repeat.rs`、`inline/box.rs` 等文件。
4. **预期结果**：你会发现「回调」几乎是所有容器型 layouter 的通用手法；`flow` 自己也会回调自己（递归排版嵌套块）。
5. **结论**：`layout_fragment` / `layout_frame` 是 typst-layout 内部的「通用排版原语」，理解了 flow，就理解了大半 crate。

#### 4.4.5 小练习与答案

**练习 1**：`layout_frame` 与 `layout_fragment` 有什么区别？repeat 为什么用前者？

> **参考答案**：[`layout_frame`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51) 是 `layout_fragment` 的「单区域便捷封装」——它把单个 `Region` 包成 `Regions`，调用 `layout_fragment` 后再 `.map(Fragment::into_frame)` 取出唯一一个 frame。repeat 只需排一个固定尺寸的碎片，不需要多区域回退队列，所以用 `layout_frame` 更直接。

**练习 2**：grid 的 `layout_cell` 为什么要「手动生成 tag」？

> **参考答案**：因为 table/grid 单元格的标签对布局阶段的内省（如 query、counter）有意义，但它们目前不能简单地标为 locatable（注释说「tags 被认为对 layout 是显著的」）。因此 grid 在排版单元格外，手动调用 `generate_tags` 注入标签，并用 `FrameParent` 维护 group 层级，保证跨多 region 时内省顺序正确。详见 [`layout_cell`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L30-L50) 的 HACK 注释。

---

### 4.5 三层主链路速览：pages → flow → inline

#### 4.5.1 概念说明

最后，把六大子系统中**贯穿整个排版主链路的三层**串起来看，建立「从文档到字母」的纵深认知：

- **pages**：文档级。决定「分成几页、每页多大、页眉页脚是什么」。
- **flow**：块级。决定一页之内「段落、块、列、脚注、浮动体怎么堆叠与断行到下一页」。
- **inline**：行内级。决定一段文字「怎么整形、怎么双向排版、在哪里断行、怎么对齐」。

这三层是**层层嵌套**的：pages 排正文时调 flow；flow 排到段落时调 inline；inline 排到行内盒/链接时又会回调 flow。grid 和 math 则是「容器型布局器」，挂在 flow 这一层。

#### 4.5.2 核心流程

```
Content
  └─ pages: layout_document
       └─ pages/run.rs: layout_page_run   (排一页：边距 + 页眉页脚 + 正文)
            └─ flow: layout_flow           (块级堆叠、列、脚注、浮动)
                 ├─ inline: layout_par      (段落：collect→prepare→linebreak→finalize)
                 ├─ grid: layout_grid/table (表格，回调 layout_cell)
                 └─ math: layout_equation_* (公式)
```

这条链路对应后续多个单元：pages 在 u3、flow 在 u4、inline 在 u5、grid/math 在 u6。本讲你只需记住它们的**相对层级**和**文件归属**。

#### 4.5.3 源码精读

三层入口的签名能反映它们的「粒度」：

- 文档级 [`layout_document`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L33-L48)：入参是 `Content` + `StyleChain`，**没有 region**——因为页面尺寸由 content 内部的 `page` 配置决定，不由调用方给定。返回 `PagedDocument`。
- 片段级 [`layout_fragment`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L56-L60)：入参带 `Regions`（多个候选区域），返回 `Fragment`（一序列 frame）。
- 段落级 `layout_par`（在 [`inline/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L44-L52)）：入参是 `Size` + `expand` + `ParSituation`，返回 `Fragment`。

注意粒度递减：pages 不需要 region（自己定义页面），flow 需要 Regions（多区域回退），inline 只需要单个 Size（一行宽度）。

#### 4.5.4 代码实践

1. **实践目标**：通过入口签名体会「粒度递减」。
2. **操作步骤**：依次打开上面三个链接，只看函数签名（参数与返回类型），不看函数体。
3. **需要观察的现象**：从 `layout_document`（无 region）→ `layout_fragment`（`Regions`）→ `layout_par`（`Size`），输入越来越「窄」。
4. **预期结果**：你能用自己的话解释「为什么越往底层，输入尺寸越简单」——因为上层已经把多区域、分页等复杂度处理掉了。
5. **待本地验证**：可对照 u1-l4 的端到端流程，确认这三层确实是嵌套调用关系。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `layout_document` 不接收 `Regions` 参数，而 `layout_fragment` 必须接收？

> **参考答案**：文档级的「区域」就是页面，而页面尺寸/边距来自 content 内部的 `page` 元素与样式链，所以 `layout_document` 自己从样式里解析页面配置，不需要外部传 region。而 `layout_fragment` 是通用片段排版，它可能被任意容器（grid 单元、stack 子项）调用，可用尺寸完全由调用方决定，所以必须接收 `Regions`。

**练习 2**：grid 和 math 处于主链路的哪一层？

> **参考答案**：它们是「块级层（flow）的容器型布局器」——flow 在分发块内容时，遇到 table/grid/equation 就分别调用 `layout_grid` / `layout_table` / `layout_equation_*`；这些 layouter 在排自己的子内容时又会回调 `layout_fragment`，形成递归。因此可以把它们看作 flow 这一层「横向」挂载的专用排布器。

---

## 5. 综合实践

**任务**：绘制一张 typst-layout 的「模块调用关系草图」，并完成文件归类。

要求：

1. **画调用图**：以 `layout_document`、`layout_flow`、`layout_par`、`layout_fragment`、`layout_frame` 为节点，画出它们之间「谁调用谁」的边；再画出 grid、math、stack、transforms、pad、repeat 这 6 个容器型 layouter 「回调 `layout_fragment` / `layout_frame`」的边。可以用纸笔或任意画图工具，标注每条边的调用文件名。

2. **验证调用图**：运行
   ```bash
   rg -n "crate::layout_fragment|crate::layout_frame" src
   ```
   把命中的文件名与你画的「回调」节点对照，修正草图。

3. **归类文件**：把下列 16 个模块名（`document`、`flow`、`grid`、`image`、`inline`、`introspect`、`lists`、`math`、`modifiers`、`pad`、`pages`、`repeat`、`rules`、`shapes`、`stack`、`transforms`）逐一归入「六大子系统 + 支撑层」，并标注哪些是目录型模块（含子 `.rs`）、哪些是单文件。

4. **标注产物位置**：在图上用特殊标记标出 `PagedDocument` / `Page` 在哪里产生（提示：`PagedDocument::new` 在 `document.rs`，`Page` 由 `pages/finalize.rs` 产出）。

**预期产出**：一张你自己画的、能解释「typst-layout 源码如何组织、子系统如何互相调用」的关系图。这张图将是后续 u2–u7 所有讲义的导航底图。

> 如果无法运行 `rg`，也可改用编辑器全局搜索 `crate::layout_fragment` 与 `crate::layout_frame`，效果相同。

## 6. 本讲小结

- typst-layout 的 `lib.rs` 是纯索引：16 条 `mod` + 5 条 `pub use`，本身不含业务代码。
- 源码自然分成 **六大核心子系统**（document / pages / flow / inline / grid / math）和一层**支撑模块**（stack / lists / shapes / transforms / image / pad / repeat / modifiers / introspect / rules）。
- `document.rs` 定义最终产物 `PagedDocument`（pages + info + introspector）与 `Page`（frame + bleed + fill + numbering ...）；introspector 是纯派生物，不参与 Hash。
- 绝大多数容器型 layouter（grid、math、stack、transforms、pad、repeat、shapes、inline/box）都通过**回调 `layout_fragment` / `layout_frame`** 来排子内容——flow 是整个 crate 的「通用排版原语」。
- 主排版链路是三层嵌套：**pages → flow → inline**，粒度递减（文档无 region → 片段多 region → 段落单 Size）。
- 只有 4 个模块（document / flow / introspect / pages / rules）被 `pub use` 对外暴露，其余 11 个通过 `rules.rs` 的 `register` 间接驱动。

## 7. 下一步学习建议

本讲建立了「地图」。接下来的学习路径建议：

1. **u2（排版的通用原语）**：在进入任何具体子系统前，先掌握贯穿全 crate 的原语——`Engine` 与 comemo 记忆化、`Regions`、`Frame` / `Fragment`、`Locator` / `Tag`。这是读懂 pages/flow/inline 内部实现的前提。
2. **u3（页面与文档布局）**：深入 `pages/` 四个文件，理解 collect → run → finalize 的完整页面排版过程，以及 `PagedIntrospector` 如何在 `PagedDocument::new` 时构建。
3. 如果你想先验证本讲的地图，可以挑一个简单 Typst 文档（几段文字 + 一个表格 + 一条公式），对照调用图猜测它依次经过了哪些模块，再在 u3–u6 中逐一印证。

> 阅读源码时，建议始终把 `lib.rs` 的 `mod` 列表和本讲的「子系统归类表」放在手边，随时定位文件归属。
