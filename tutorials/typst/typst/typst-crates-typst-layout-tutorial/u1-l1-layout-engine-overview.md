# typst-layout 是什么：Typst 的分页排版引擎

## 1. 本讲目标

本讲是整本 `typst-layout` 学习手册的第一篇。读完本讲，你应当能够：

- 说清楚 `typst-layout` 这个 crate **到底负责什么、不负责什么**（它的边界在哪里）。
- 看懂它的 `Cargo.toml`，并理解其中每一类依赖（字体整形、双向文字、断行、记忆化缓存、几何曲线等）为排版提供了什么能力。
- 看懂 `src/lib.rs` 导出的全部公共符号，知道文档级入口、片段级入口、规则注册和最终产物分别是什么。

本讲**不**带你深入任何排版算法的实现细节——那是后续讲义的任务。本讲只帮你建立一张「全局地图」，让你知道这块代码在整个 Typst 项目里站在什么位置。

## 2. 前置知识

阅读本讲前，你最好已经了解下面这些概念（如果不熟也没关系，我会顺带解释）：

- **Typst 是什么**：一个用 Rust 写的现代、科学排版系统。你写 `.typ` 源文件，它输出 PDF / PNG / SVG 等。
- **crate 与 workspace**：Typst 整个项目是一个 Cargo workspace，由很多个 crate 组成。`typst-layout` 只是其中一个。可以把它理解成「一个独立编译、职责单一的代码包」。
- **Content（内容）**：Typst 在「求值」阶段把源码变成一棵内容树，类型叫 `Content`。它是排版的**输入**。
- **排版（layout）**：把 `Content` 计算成「每个字、每个图具体画在页面的哪个坐标上」的过程。排版的结果是一系列「页面」。
- **分页（paged）**：和「连续滚动 / 无限画布」相对。分页排版会把内容切分到一张张固定大小的页面里，能处理页眉页脚、页码、奇偶页、分页符。

你不需要懂 Rust 的全部高级特性，但要知道：`pub use` 是「公开重新导出某个符号」，`pub fn` 是「公开函数」，`use` 是「引入某个外部库」。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是理解整个 crate 的「入口的两扇门」：

| 文件 | 作用 | 本讲怎么看 |
| --- | --- | --- |
| [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/Cargo.toml) | 声明 crate 名称、描述、依赖。 | 看它依赖了哪些库，反推出排版需要哪些能力。 |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs) | crate 的根模块。声明内部子模块，并用 `pub use` 导出对外 API。 | 看它把哪些函数 / 类型暴露给了外部调用者。 |

为了把「最终产物」讲清楚，我们还会顺带看一眼 [`src/document.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs) 里 `PagedDocument` / `Page` 的定义——它们就是排版的「最终输出」。

> 提示：本讲给出的所有永久链接都指向固定 commit `146a5832`，即使仓库日后改动，你点开的也还是这篇讲义写作时的版本。

## 4. 核心概念与源码讲解

### 4.1 typst-layout 的定位：它是「排版引擎」，不是「解析器」也不是「求值器」

#### 4.1.1 概念说明

很多人第一次接触 Typst 会以为「Typst = 一个把 `.typ` 文件变成 PDF 的程序」。这话对，但太笼统。真正发生的事情是一条长长的流水线，而 `typst-layout` 只占其中一段。

可以把整条流水线粗略分成三步：

1. **解析（parsing）**：`typst-syntax` 把源码文本变成语法树（AST）。
2. **求值 / 现实化（evaluation / realization）**：`typst-eval`、`typst-realize` 等把语法树求值成 `Content`，并应用 `show` / `set` 规则，得到「待排版的内容」。
3. **排版（layout）**：**这就是 `typst-layout` 做的事**——把上一步的 `Content` 计算成一张张具体坐标确定的页面。

`typst-layout` 在 [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/Cargo.toml) 里给自己的一句话描述就是：

> `description = "Typst's layout engine."`

它的**输入**是已经 realized 的 `Content`，**输出**是一个 `PagedDocument`（由若干 `Page` 组成，每个 `Page` 里装着一个 `Frame`，`Frame` 里就是每个文字 / 图形的精确位置）。

#### 4.1.2 核心流程

用一个最简流程表示 `typst-layout` 在整个系统中的位置：

```
.typ 源码
   │  (typst-syntax 解析)
   ▼
语法树 AST
   │  (typst-eval 求值 + typst-realize 应用 show 规则)
   ▼
Content（内容树）  ◄── typst-layout 的输入
   │  (typst-layout 排版)
   ▼
PagedDocument { pages: [Page { frame, ... }, ...] }  ◄── typst-layout 的输出
   │  (typst-pdf / typst-svg / typst-render 导出)
   ▼
PDF / PNG / SVG 文件
```

注意两点：

- `typst-layout` **不读 `.typ` 源文件**，它只接收上游已经算好的 `Content`。
- `typst-layout` **也不写 PDF**，它只产出 `PagedDocument`；真正导出成 PDF / 图片是 `typst-pdf` / `typst-render` / `typst-svg` 等下游 crate 的事。

#### 4.1.3 源码精读

`typst-layout` 的入口 `layout_document` 收到的就是一个 `Content`，签名见 [`pages/mod.rs:33-37`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L33-L37)：

```rust
pub fn layout_document(
    engine: &mut Engine,
    content: &Content,
    styles: StyleChain,
) -> SourceResult<PagedDocument> {
```

可以看到：输入是 `content`（内容）和 `styles`（样式链），输出是 `PagedDocument`。`engine` 是贯穿 Typst 各阶段的「上下文 / 引擎」，后面 4.2 会细讲。

而最终产物 `PagedDocument` 长什么样，见 [`document.rs:16-21`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L16-L21)：

```rust
pub struct PagedDocument {
    pages: EcoVec<Page>,
    info: DocumentInfo,
    introspector: Arc<PagedIntrospector>,
}
```

它就是「一串 `Page` + 一份文档元信息（`DocumentInfo`）+ 一个用于查询的 `introspector`」。这正是导出 crate（`typst-pdf` 等）所需要的东西。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，帮助你亲手确认「typst-layout 处于流水线的哪一段」。

1. **目标**：确认 `typst-layout` 不负责解析、也不负责导出，只负责排版。
2. **步骤**：
   - 打开本 crate 的 [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/Cargo.toml)，确认它的依赖里**没有**任何 PDF 库（如 `krilla`、`pdf-writer`），也没有「读 `.typ` 文件」相关的库。
   - 打开仓库根的 [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/Cargo.toml)，看 workspace 成员列表（第 2 行 `members = ["crates/*", ...]`），注意存在 `typst-syntax`、`typst-eval`、`typst-realize`、`typst-pdf`、`typst-svg`、`typst-render` 等并列的 crate。
3. **观察现象**：`typst-layout` 的依赖里既没有「解析」也没有「PDF 导出」的库，但依赖了 `typst-library`（提供 `Content`、`Engine`、`Frame` 等公共类型）。
4. **预期结果**：你可以画出一句话结论——「`typst-layout` 只夹在求值与导出之间，职责单一」。

#### 4.1.5 小练习与答案

**练习 1**：假如有人想给 Typst 增加一种全新的输出格式（比如 EPUB），需不需要改 `typst-layout`？

> **参考答案**：不需要。`typst-layout` 只产出 `PagedDocument` 这种与具体格式无关的结构，新增导出格式属于下游导出 crate 的职责。

**练习 2**：`layout_document` 的返回类型是 `PagedDocument` 还是 `PDF` 字节？

> **参考答案**：是 `PagedDocument`（见 `pages/mod.rs:37`）。生成 PDF 字节是 `typst-pdf` 的工作。

---

### 4.2 关键依赖：它们各自为排版提供了什么能力

#### 4.2.1 概念说明

`typst-layout` 把很多「专业且困难」的活儿交给了成熟的开源库。看懂 `Cargo.toml` 的依赖列表，就能反推出这个 crate 具备哪些排版能力。本节聚焦四个最具代表性的依赖：

- **`typst-library`**：Typst 的「公共类型大仓库」，提供 `Content`、`Engine`、`Frame`、`Locator`、`StyleChain`、各种元素（`HeadingElem`、`TableElem` 等）。`typst-layout` 和它**不是竞争关系**，而是「消费者」关系——`typst-layout` 用 `typst-library` 定义的类型来工作。
- **`comemo`**：一个记忆化（memoization）库。它让「相同输入的排版函数只算一次、结果缓存复用」，是 Typst 能做增量排版 / 并行排版的关键。
- **`rustybuzz`**：文本**整形（shaping）**库（HarfBuzz 的 Rust 移植）。它把「一串字符 + 一个字体」转换成「一串带精确位置的字形」，负责连字（ligature）、字距（kerning）、OpenType 特性等。
- **`icu_segmenter`**：Unicode 的**断句 / 断行分段器**，告诉排版引擎「这一行可以在哪里断开」。

其余依赖（如 `unicode-bidi`、`kurbo`、`hypher`、`bumpalo`）我会在表中简要说明，它们的具体用法会在后续讲义展开。

#### 4.2.2 核心流程

排版一段文字时，这些库大致按下面的顺序发挥作用：

```
文本字符串
   │  unicode-bidi：分析双向文字（左↔右混排），确定每个字符的显示方向与层级
   ▼
带方向信息的文本
   │  icu_segmenter：找出所有「合法的断行点」
   ▼
候选断点
   │  typst-layout 自己：用断行算法挑出「最好看」的断法（见 linebreak 讲义）
   ▼
每一行的字符区间
   │  rustybuzz：对每一行做 shaping，得到精确的字形与 advance
   ▼
带坐标的字形 → 写入 Frame
```

而**贯穿全程**的 `comemo`，负责把上面每一步的函数调用结果缓存起来：只要输入（内容、样式、字体、区域尺寸）没变，就直接返回缓存，避免重复计算。`typst-library` 则提供了上面所有步骤共享的类型骨架。

#### 4.2.3 源码精读

先看 [`Cargo.toml:15-43`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/Cargo.toml#L15-L43) 里的 `[dependencies]` 段，节选关键几行：

```toml
typst-library = { workspace = true }
comemo = { workspace = true }
icu_segmenter = { workspace = true }
rustybuzz = { workspace = true }
unicode-bidi = { workspace = true }
kurbo = { workspace = true }
hypher = { workspace = true }
bumpalo = { workspace = true }
```

> `workspace = true` 表示版本号由仓库根 `Cargo.toml` 的 `[workspace.dependencies]` 统一管理，本 crate 不单独指定版本。

接着，用搜索确认这些库**确实被代码用到**（而不是声明了却没用）：

- `rustybuzz` 用于文本整形，见 [`src/inline/shaping.rs:8`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L8)：`use rustybuzz::{BufferFlags, Feature, ShapePlan, UnicodeBuffer};`。数学公式整形也会用到它，见 [`src/math/shaping.rs:3`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/math/shaping.rs#L3)。
- `icu_segmenter` 用于找断行点，见 [`src/inline/linebreak.rs:8-9`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/linebreak.rs#L8-L9)：`use icu_segmenter::{LineBreakOptions, LineSegmenter, LineSegmenterBorrowed};`。
- `unicode_bidi` 用于双向文字分析，见 [`src/inline/prepare.rs:4`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L4)：`use unicode_bidi::{BidiInfo, Level as BidiLevel};`。
- `kurbo` 用于几何曲线（下划线、装饰、图形），见 [`src/inline/deco.rs:1`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/deco.rs#L1) 与 [`src/shapes.rs:3`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/shapes.rs#L3)。
- `comemo` 的用法见 [`src/pages/mod.rs:51`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L51)：在 `layout_document_impl` 上标注 `#[comemo::memoize]`，使该函数的结果被缓存。

> `#[comemo::memoize]` 标注的函数必须接收「可追踪（tracked）」的参数，这解释了为什么 `layout_document_impl` 的参数列表（`pages/mod.rs:53-62`）那么长——每个参数都要从 `Engine` 里拆出来变成 `Tracked<...>` 形式。这一点会在第 u2-l1 讲「Engine 与 comemo」中专门讲解，本讲只需建立印象。

下面这张表把主要依赖和它们提供的能力汇总起来，方便查阅：

| 依赖 | 提供的能力 | 在 crate 内的使用位置 |
| --- | --- | --- |
| `typst-library` | 公共类型骨架：`Content`、`Engine`、`Frame`、`Locator`、`StyleChain`、各种 `Elem` | 几乎每个 `.rs` 文件的 `use typst_library::...` |
| `comemo` | 函数结果记忆化（缓存 + 增量 + 并行安全） | 各 `*_impl` 函数上的 `#[comemo::memoize]` |
| `rustybuzz` | 文本整形（字形、advance、kerning、OpenType） | `inline/shaping.rs`、`math/shaping.rs` |
| `icu_segmenter` | Unicode 断行 / 分段，找合法断点 | `inline/linebreak.rs` |
| `unicode-bidi` | 双向文字（BiDi）分析 | `inline/prepare.rs`、`inline/shaping.rs` |
| `hypher` | 英语等语言的连字符（hyphenation） | 断行相关流程 |
| `kurbo` | 贝塞尔曲线等 2D 几何 | `shapes.rs`、`inline/deco.rs` |
| `bumpalo` | 区域内存分配器（bump arena），减少临时对象开销 | `flow/collect.rs`、`flow/mod.rs` |
| `ttf-parser` | 解析字体文件（字形轮廓、MATH 表） | shaping / math |
| `unicode-math-class` | 判断字符的数学类别 | `math/` |
| `ecow` / `smallvec` | 轻量容器（`EcoVec`、`SmallVec`） | `document.rs` 的 `EcoVec<Page>` 等 |

#### 4.2.4 代码实践

这是一个**动手核对型实践**，目标是让你亲手验证「依赖 = 能力」这张表，而不是只听我讲。

1. **目标**：为每个关键依赖找到至少一处真实 `use` 语句，确认它确实在 crate 内被使用。
2. **步骤**：
   - 在仓库里（`crates/typst-layout/src` 目录下）搜索 `rustybuzz`，记录它出现在哪几个文件、用了哪些类型。
   - 同样搜索 `icu_segmenter`、`unicode_bidi`、`kurbo`、`bumpalo`。
   - 你可以使用命令（在 `crates/typst-layout` 目录下）：
     ```bash
     grep -rn "use rustybuzz" src
     grep -rn "use icu_segmenter" src
     grep -rn "use kurbo" src
     ```
3. **观察现象**：每个库都至少有一个 `use` 语句，且出现在与其能力对应的模块里（shaping 库出现在 shaping 文件、断行库出现在 linebreak 文件）。
4. **预期结果**：你能填出一张「依赖 → 文件 → 能力」的小表，和本节给出的表互相印证。
5. 若你所在环境无法运行 `grep`，标注「待本地验证」，但你可以直接点开上面给出的源码永久链接人工核对。

#### 4.2.5 小练习与答案

**练习 1**：`rustybuzz` 和 `icu_segmenter` 一个负责「整形」一个负责「断行」。请说出它们谁先执行、谁后执行，为什么。

> **参考答案**：通常先由 `icu_segmenter`（配合 typst-layout 自己的断行算法）决定每一行**包含哪些字符**（即在哪断开），再由 `rustybuzz` 对「这一行的字符」做整形得到精确字形。先定内容、再定坐标。

**练习 2**：为什么 `layout_document_impl` 上要加 `#[comemo::memoize]`？

> **参考答案**：排版很昂贵，而 Typst 支持「内容只改了一点点就只重排受影响的部分」。`comemo::memoize` 让函数在「输入完全相同」时直接返回缓存结果，是实现增量排版的基石。

**练习 3**：`typst-layout` 依赖 `typst-library`，反过来 `typst-library` 依赖 `typst-layout` 吗？

> **参考答案**：不依赖（否则会形成循环依赖）。`typst-library` 提供公共类型与元素定义，`typst-layout` 消费它们来完成排版。两者的衔接通过「show 规则注册」（见 4.3）和 trait（如 `Output`）完成。

---

### 4.3 公共 API 与入口函数

#### 4.3.1 概念说明

一个 crate 对外暴露什么，完全由它的 `lib.rs` 里的 `pub use` 决定。`typst-layout` 的 `lib.rs` 非常干净——只导出 7 个符号，按用途分三类：

1. **最终产物**：`Page`、`PagedDocument`。
2. **入口函数**：
   - 文档级：`layout_document`、`layout_document_for_bundle`。
   - 片段级：`layout_fragment`、`layout_frame`。
3. **支撑**：`PagedIntrospector`（查询索引）、`register`（注册 show 规则）。

「文档级」入口把**一整个文档**排成 `PagedDocument`；「片段级」入口只把**一小段内容**排成一个 `Fragment`（一序列 `Frame`），常被表格、数学公式、栈这类「容器型」layouter 在内部递归调用。

#### 4.3.2 核心流程

调用关系大致如下：

```
外部调用者（typst 主流程 / typst-cli 编译）
   │
   ├── layout_document ──────────────► PagedDocument
   │        （整篇文档排版，含分页、页眉页脚、introspector）
   │
   ├── layout_document_for_bundle ───► PagedDocument
   │        （给「字体 bundle 编译」等特殊场景用，locator 来源不同）
   │
   └── 容器型 layouter（grid / math / stack / transforms …）在内部递归调用：
            layout_fragment ─────────► Fragment（多区域）
                 └─ layout_frame ────► Frame（单区域，便捷封装）

   PagedDocument 构造时 ──► PagedIntrospector::new（自动生成查询索引）
   Library 构建时 ────────► register(...)（把 show 规则挂到 Target::Paged）
```

其中 `layout_frame` 是 `layout_fragment` 的「单区域便捷封装」：当你确信内容只会排进一个区域时，用它更省事。

#### 4.3.3 源码精读

先看 [`src/lib.rs:20-24`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs#L20-L24)，这是全部公共导出：

```rust
pub use self::document::{Page, PagedDocument};
pub use self::flow::{layout_fragment, layout_frame};
pub use self::introspect::PagedIntrospector;
pub use self::pages::{layout_document, layout_document_for_bundle};
pub use self::rules::register;
```

再逐个看入口函数的真实定义。

**片段级入口 `layout_frame` 与 `layout_fragment`**，见 [`src/flow/mod.rs:42-62`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L62)：

```rust
pub fn layout_frame(
    engine: &mut Engine,
    content: &Content,
    locator: Locator,
    styles: StyleChain,
    region: Region,
) -> SourceResult<Frame> {
    layout_fragment(engine, content, locator, styles, region.into())
        .map(Fragment::into_frame)
}

/// Lays out content into multiple regions.
/// When laying out into just one region, prefer [`layout_frame`].
pub fn layout_fragment(
    engine: &mut Engine,
    content: &Content,
    locator: Locator,
    styles: StyleChain,
    regions: Regions,
) -> SourceResult<Fragment> {
```

注意 `layout_frame` 的函数体：它就是把单个 `Region` 转成 `Regions`，调用 `layout_fragment`，再用 `Fragment::into_frame` 取出唯一一个 `Frame`。注释也明说「只排一个区域时优先用 `layout_frame`」。

**文档级入口 `layout_document`**，见 [`src/pages/mod.rs:33-48`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L33-L48)：

```rust
pub fn layout_document(
    engine: &mut Engine,
    content: &Content,
    styles: StyleChain,
) -> SourceResult<PagedDocument> {
    layout_document_impl(
        engine.world,
        engine.library,
        engine.introspector.into_raw(),
        engine.traced,
        TrackedMut::reborrow_mut(&mut engine.sink),
        engine.route.track(),
        content,
        styles,
    )
}
```

这里又看到 4.2 提到的模式：公开函数把 `Engine` 拆成若干 `Tracked` / `TrackedMut` 参数，再交给带 `#[comemo::memoize]` 的 `layout_document_impl`。另一个文档级入口 `layout_document_for_bundle` 定义在 [`src/pages/mod.rs:78`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L78)，两者的差别会在 u3-l1 讲义里专门讨论（主要是 `Locator` 来源不同）。

**规则注册入口 `register`**，见 [`src/rules.rs:39-40`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L39-L40)：

```rust
/// Register show rules for the [paged target](Target::Paged).
pub fn register(rules: &mut NativeRuleMap) {
    use Target::Paged;
```

它把大量的 `*_RULE`（每个都是一条「把某元素渲染成什么样」的 show 规则）注册到 `NativeRuleMap` 的 `Target::Paged` 目标下。比如 [`rules.rs:43-44`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L43-L44) 就注册了 `STRONG_RULE` 和 `EMPH_RULE`（粗体、强调）。这些规则把「语义元素」转成「排版动作」，是 `typst-layout` 与 `typst-library` 的关键粘合层。

**最终产物** `PagedDocument` 与 `Page` 已在 4.1.3 给出（`document.rs:16-21` 与 `document.rs:83-105`）。其中 [`document.rs:57-61`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L57-L61) 还让 `PagedDocument` 实现了 `Document` trait，并通过 `Output` trait（[`document.rs:63-79`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L63-L79)）的 `create` 方法反向调回 `crate::layout_document`——这构成了「外部通过统一 trait 触发排版」的闭环。

#### 4.3.4 代码实践

这是本讲的主实践，对应任务说明里要求的两件事。

1. **目标**：列出本 crate 导出的全部公共符号及一句话用途；并说明每个关键依赖提供的能力。
2. **步骤 A（公共符号）**：
   - 打开 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs)，逐行读 4 条 `pub use`。
   - 对每个符号，点进它定义处的链接，确认它的类型 / 签名。
   - 填写下面这张表（参考答案见「预期结果」）：

     | 符号 | 定义位置 | 一句话用途 |
     | --- | --- | --- |
     | `PagedDocument` | `document.rs:17` | ? |
     | `Page` | `document.rs:83` | ? |
     | `layout_document` | `pages/mod.rs:33` | ? |
     | `layout_document_for_bundle` | `pages/mod.rs:78` | ? |
     | `layout_fragment` | `flow/mod.rs:56` | ? |
     | `layout_frame` | `flow/mod.rs:42` | ? |
     | `PagedIntrospector` | `introspect.rs:22` | ? |
     | `register` | `rules.rs:39` | ? |

3. **步骤 B（依赖能力）**：复用 4.2.4 的搜索结果，为 `typst-library`、`comemo`、`rustybuzz`、`icu_segmenter` 各写一句话能力说明。
4. **观察现象**：`lib.rs` 只导出 7 个符号；其余一切（`flow`、`inline`、`grid`、`math` 等子模块）都是内部 `mod`，对外不可见。
5. **预期结果（参考答案）**：

   | 符号 | 一句话用途 |
   | --- | --- |
   | `PagedDocument` | 排版的最终产物：一串页面 + 文档信息 + 查询索引。 |
   | `Page` | 单个成品页面，内含 `Frame`、出血量、填充、页码等。 |
   | `layout_document` | 文档级排版入口，把 `Content` 排成 `PagedDocument`。 |
   | `layout_document_for_bundle` | 给 bundle 编译场景用的文档级入口（locator 来源不同）。 |
   | `layout_fragment` | 片段级排版入口，把内容排进多个区域，返回 `Fragment`。 |
   | `layout_frame` | `layout_fragment` 的单区域便捷封装，直接返回一个 `Frame`。 |
   | `PagedIntrospector` | 基于 `PagedDocument` 构建的查询索引，支持 query / counter / 定位。 |
   | `register` | 把 paged 目标下的所有 show 规则注册进 `NativeRuleMap`。 |

   | 依赖 | 能力 |
   | --- | --- |
   | `typst-library` | 提供排版所需的全部公共类型骨架。 |
   | `comemo` | 记忆化缓存，支撑增量与并行排版。 |
   | `rustybuzz` | 文本整形（字形与位置）。 |
   | `icu_segmenter` | Unicode 断行点检测。 |

6. 本实践为源码阅读型，不涉及运行命令，因此不存在「运行失败」；若你尝试用 `cargo doc --open` 查看 `typst-layout` 的文档来交叉验证这 7 个符号，属可选项，结果应与上表一致（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lib.rs` 里 `flow`、`inline`、`grid`、`math` 这些模块是 `mod`（私有），而不是 `pub mod`？

> **参考答案**：因为它们是实现细节。crate 对外只承诺 7 个公共符号的接口稳定，内部如何拆模块、如何排版，可以随时重构而不破坏调用者。这是良好的封装。

**练习 2**：`layout_frame` 和 `layout_fragment` 应该在什么时候分别使用？

> **参考答案**：当你确信内容只排进一个区域（例如排一个不可断裂的内联 box、一个图标）时，用 `layout_frame` 更方便，它直接给你一个 `Frame`；当内容可能跨多个区域（例如一段会自动换行的文字、一个会分页的块）时，必须用 `layout_fragment` 拿到 `Fragment`（一序列 `Frame`）。源码注释（`flow/mod.rs:55`）也明示了这一点。

**练习 3**：`register` 函数注册的规则挂在哪个 `Target` 下？这意味着什么？

> **参考答案**：挂在 `Target::Paged` 下（`rules.rs:40` 的 `use Target::Paged;`）。这意味着这些 show 规则只在「分页排版」目标下生效；如果 Typst 未来有其他目标（例如已有的 HTML 目标），可以挂不同的规则集合。

## 5. 综合实践

把本讲学到的三件事——**定位、依赖、公共 API**——串成一个小任务：动手画一张「`typst-layout` 全局名片」。

1. **任务**：用你顺手的工具（纸笔、Markdown、绘图软件都行）产出一张图 / 一段文字，必须包含：
   - 一条**流水线**：`源码 → 解析 → 求值/realize → Content →【typst-layout 排版】→ PagedDocument → 导出 → PDF`，并标注「typst-layout 只负责【】这一段」。
   - 一张**依赖表**：至少列出 `typst-library`、`comemo`、`rustybuzz`、`icu_segmenter`、`unicode-bidi`、`kurbo` 各自的能力。
   - 一张**公共 API 表**：列出 7 个导出符号及其用途。
2. **验证**：拿你画的名片去对照 [`src/lib.rs:20-24`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs#L20-L24) 和 [`Cargo.toml`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/Cargo.toml)，确保没有多写、也没有漏写。
3. **延伸思考（可选）**：在名片上用箭头标出 `layout_frame` → `layout_fragment` 的调用关系，以及 `Output::create` → `layout_document` 的反向回调（`document.rs:72-78`）。

完成这张名片后，你就拥有了一个稳固的「全局心智模型」，后续深入任何子系统时都能随时回到这张地图定位。

## 6. 本讲小结

- `typst-layout` 是 Typst 的**分页排版引擎**，输入是已 realized 的 `Content`，输出是 `PagedDocument`（若干 `Page` / `Frame`）；它**不**负责解析源码，也**不**负责生成 PDF。
- 它的职责边界可以从 `Cargo.toml` 看出：既无解析库也无 PDF 库，但依赖 `typst-library` 提供公共类型。
- 关键依赖各司其职：`rustybuzz` 做整形、`icu_segmenter` 找断行点、`unicode-bidi` 处理双向文字、`kurbo` 做几何、`comemo` 做记忆化缓存、`typst-library` 提供类型骨架。
- `lib.rs` 只导出 7 个符号：产物 `Page` / `PagedDocument`，入口 `layout_document` / `layout_document_for_bundle` / `layout_fragment` / `layout_frame`，以及支撑的 `PagedIntrospector` 与 `register`。
- `layout_frame` 是 `layout_fragment` 的单区域便捷封装；`register` 把 show 规则挂到 `Target::Paged`，是 layout 与 library 的粘合层。
- 公开函数普遍采用「拆 `Engine` 成 `Tracked` 参数 + 调用 `#[comemo::memoize]` 的 `_impl` 函数」的模式，这是为了支持缓存与增量排版（细节留待 u2-l1）。

## 7. 下一步学习建议

本讲只是「站在门口看地图」。接下来建议：

1. **下一讲 u1-l2《目录结构与模块地图》**：打开 `lib.rs` 里那些私有 `mod`（`flow`、`inline`、`grid`、`math`、`pages`…），建立「七大子系统」心智模型，知道每个 `.rs` 文件大致负责什么。
2. **再后续 u1-l3、u1-l4**：分别细看公共 API 的调用点和端到端的 `Content → PagedDocument` 流程。
3. **如果想提前感受排版的「产物」**：直接读 [`src/document.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs) 全文——它是本 crate 里最短、最易懂的文件之一，能帮你巩固「Page / PagedDocument 长什么样」。
4. **关于 comemo 模式**：本讲多次提到「`_impl` + `#[comemo::memoize]`」，如果你好奇为什么参数要拆成 `Tracked`，可以先跳到 u2-l1，但建议按顺序读完第一层（u1）再进入。
