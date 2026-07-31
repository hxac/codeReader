# 公共 API 与入口函数

## 1. 本讲目标

上一讲（u1-l2）我们建立了一张「模块地图」，知道了 typst-layout 分成六大子系统，且 `lib.rs` 只是一个纯索引。本讲要回答一个更具体的问题：**外部世界（typst 主 crate、typst-pdf、typst-svg、typst-cli）究竟通过哪些函数进入这个排版引擎？**

学完本讲你应当能够：

1. 说出 `lib.rs` 对外暴露的全部公共符号，并按「产物 / 入口 / 注册」三类归类。
2. 区分**文档级入口**（`layout_document` / `layout_document_for_bundle`）与**片段级入口**（`layout_fragment` / `layout_frame`），并知道何时该用哪一个。
3. 理解 `layout_frame` 只是 `layout_fragment` 的「单区域便捷封装」，并明白它依赖的一个关键约定（`Fragment::into_frame` 在多帧时会 panic）。
4. 知道 `register` 这个函数如何把 show 规则挂到 `Target::Paged`，从而把 layout 与 library 这两层粘合起来。

## 2. 前置知识

在进入源码前，先澄清几个本讲反复出现的术语。

- **入口函数（entry function）**：排版引擎对外开放的「大门」。外部代码不直接调用内部的 flow / inline / grid 这些子系统，而是通过入口函数进入。入口函数负责把高层意图（排一篇文档 / 排一段内容）翻译成对子系统的调用。
- **Region（区域）**：一块可供排版的矩形空间。一个 `Region` 只有一块空间；一个 `Regions` 则是一「串」候选空间（含 `backlog` 后续尺寸）。这决定了内容能否「断裂到下一页/列」。
- **Frame（帧）与 Fragment（片段）**：排版结果的基本载体。`Frame` 是单张二维结果；`Fragment` 是一组 `Frame` 的序列。内容排在单区域内 → 单个 Frame；内容可能跨多个区域 → 多个 Frame 的 Fragment。
- **realize（现实化）**：在真正排版前的一步，把用户写的 Content 展开成排版引擎认识的元素序列（如把 `*粗体*` 变成带样式的文本）。上一讲已经提到过，本讲会看到它在入口函数里被显式调用。
- **Engine**：贯穿排版的「上下文大礼包」，字段包括 `world`（访问字体/文件）、`library`（类型与规则）、`introspector`（查询索引）、`traced` / `sink`（诊断收集）、`route`（递归深度控制）。入口函数做的第一件事常常是把 `Engine` 拆开。

> 本讲承接 u1-l1（crate 定位与依赖）和 u1-l2（模块地图），不再重复「typst-layout 是什么」「目录怎么分」，直接聚焦「门在哪里、每扇门通往哪里」。

## 3. 本讲源码地图

本讲涉及的文件极少，但它们是整个 crate 的「门面」：

| 文件 | 作用 |
|------|------|
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs) | 纯索引：16 条 `mod` 声明 + 5 条 `pub use`，定义全部公共 API。 |
| [`src/flow/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs) | 片段级入口 `layout_frame` / `layout_fragment` / `layout_columns`，以及真正干活的 `layout_flow`。 |
| [`src/pages/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs) | 文档级入口 `layout_document` / `layout_document_for_bundle`，以及 `layout_pages`。 |
| [`src/document.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs) | 产物类型 `PagedDocument` / `Page`，以及 `Output` trait 实现（连向 `layout_document`）。 |
| [`src/rules.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs) | `register` 函数：把 show 规则挂到 `Target::Paged`。 |

此外会顺带引用一处**外部调用点**：[`crates/typst/src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs)，那里调用了 `register`，能说明「门面」是如何被外部使用的。

## 4. 核心概念与源码讲解

### 4.1 公共导出全景：lib.rs 的对外门面

#### 4.1.1 概念说明

`lib.rs` 本身**不含任何排版逻辑**——它既不计算尺寸，也不生成 Frame。它唯一的职责是「目录索引 + 对外交接口」。整个文件的「对外输出」部分只有 5 条 `pub use` 语句，把它们读一遍，就等于看完了这个 crate 的全部公共 API。

这 8 个公共符号可以分成三类：

1. **产物类型（Product）**：`Page`、`PagedDocument` —— 排版完成后交出去的东西。
2. **入口函数（Entry）**：文档级 `layout_document` / `layout_document_for_bundle`；片段级 `layout_fragment` / `layout_frame`。
3. **粘合层（Glue）**：`register`（把 show 规则挂到 library）、`PagedIntrospector`（产物附带的查询索引类型）。

> 注意：`flow/mod.rs` 里还有一个 `pub fn layout_columns`，但它**没有**在 `lib.rs` 里被 `pub use`，所以不算 crate 对外 API。它只在本 crate 内部被 `COLUMNS_RULE` 调用（见 4.4）。这是一个容易混淆的点——`pub` 不等于「对外导出」。

#### 4.1.2 核心流程

`lib.rs` 的对外门面遵循一个贯穿全 crate 的设计模式，可以记作「**公开函数 → 记忆化实现**」：

```
外部调用者
   │  调用 pub fn（参数是高层、好用的 &mut Engine / Content / Regions）
   ▼
pub fn（薄封装）
   │  把 Engine 拆成多个 Tracked/TrackedMut 参数
   ▼
#[comemo::memoize] fn *_impl（真正干活、可缓存）
   │  realize → 调用子系统（flow / pages）
   ▼
Frame / Fragment / PagedDocument
```

为什么要拆？因为带 `#[comemo::memoize]` 的函数要求参数是「可追踪、可哈希」的，这样才能缓存结果、支持增量排版与并行排版。这个模式将在 u2-l1 专门讲解；本讲你只需要记住：**每扇大门背后都先做一次「拆 Engine → 进缓存层」的转接**。

#### 4.1.3 源码精读

门面的全部内容：

```rust
pub use self::document::{Page, PagedDocument};
pub use self::flow::{layout_fragment, layout_frame};
pub use self::introspect::PagedIntrospector;
pub use self::pages::{layout_document, layout_document_for_bundle};
pub use self::rules::register;
```

这段位于 [src/lib.rs:L20-L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs#L20-L24)，它逐行说明了每个符号来自哪个子模块：

- 第 20 行：产物 `Page` / `PagedDocument` 来自 `document` 模块；
- 第 21 行：片段级入口 `layout_fragment` / `layout_frame` 来自 `flow` 模块；
- 第 22 行：`PagedIntrospector` 来自 `introspect` 模块（u3-l5 详讲）；
- 第 23 行：文档级入口来自 `pages` 模块；
- 第 24 行：`register` 来自 `rules` 模块。

注意对比：`flow` 模块里虽然也有 `layout_columns` 和 `layout_flow`，但只有前两个被导出；其余子系统的函数（`grid::layout_grid`、`math`、`stack` 等）一个都没导出——它们是通过 `register` 间接驱动的（见 4.4）。

#### 4.1.4 代码实践

**实践目标**：亲手核对「8 个符号」与它们的定义位置。

**操作步骤**：

1. 打开 `src/lib.rs`，数 `pub use` 语句导出的符号总数，确认是 8 个。
2. 对每个符号，用编辑器「跳转到定义」，记录它定义在哪个文件的哪一行。例如 `layout_document` → `src/pages/mod.rs`。
3. 把结果填入一张三列表：`符号 | 定义文件 | 类别（产物/入口/粘合）`。

**需要观察的现象**：你会发现自己跳转到的全是「公开薄封装函数」，真正干活的 `*_impl` 在它们正下方。

**预期结果**：8 个符号全部能跳转到定义；其中 5 个是函数入口，2 个是产物类型，1 个是 introspector 类型，外加 `register`。`layout_columns` 不会出现在任何一次跳转里——它不在门面中。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `grid::layout_grid`、`stack::layout_stack` 这些函数没有出现在 `lib.rs` 的 `pub use` 里，但 typst 依然能排版表格和栈？

**参考答案**：因为它们是被 `rules.rs` 里的 show 规则（如 `GRID_RULE`、`STACK_RULE`）以函数指针形式挂载到 `BlockElem::multi_layouter` 上的。引擎在排版时通过 show 规则回调这些函数，而不需要它们出现在 crate 的公共 API 中。也就是说，「对外门面」和「实际驱动排版」是两条不同的路径。

**练习 2**：如果一个函数在某个模块里标了 `pub`，它就一定是 crate 对外 API 吗？

**参考答案**：不一定。`pub` 只表示「对该 crate 内其它模块可见」；只有被 `lib.rs` 用 `pub use` 重新导出的符号，才是真正的对外 API。`flow::layout_columns` 就是反例——它是 `pub`，但没被 `pub use`。

---

### 4.2 文档级入口：layout_document 与 layout_document_for_bundle

#### 4.2.1 概念说明

文档级入口负责把「一整篇文档」排成 `PagedDocument`（若干 `Page`）。它有两个变体：

- `layout_document`：常规编译用，从「根 locator」开始排版。
- `layout_document_for_bundle`：用于 **bundle 编译**（把某些内容预编译进字体包/资源包的场景），从外部传入的 `Locator` 开始。

两者最显著的共同点是：**它们不接受 `Regions` 参数**。因为页面尺寸完全由内容与样式链里的 `page` 配置决定（纸张大小、边距等），入口函数自己从样式里解析，而不由调用方指定画布。这一点和片段级入口（必须传 `Regions`）形成鲜明对比。

#### 4.2.2 核心流程

`layout_document` 的内部链路：

```
pub fn layout_document
   │  拆 Engine → 调用 memoize 的 layout_document_impl
   ▼
layout_document_impl
   │  生成 Locator::root()（根定位器）
   ▼
layout_document_common   ← 两者共享的实现
   │  1. 把外部样式标记为 outside
   │  2. 填充 DocumentInfo（标题、作者、语言等）
   │  3. routines.realize(RealizationKind::Document)
   │  4. layout_pages：切分 page run → 并行排版 → finalize 组装
   ▼
PagedDocument::new(pages, info)   ← 顺便构建 introspector
```

`layout_document_for_bundle` 与之几乎相同，唯一区别在 locator 的来源（见 4.2.4）。

#### 4.2.3 源码精读

公开的薄封装，注意它如何把 `&mut Engine` 拆开：

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

见 [src/pages/mod.rs:L33-L48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L33-L48)。注意它**没有** `regions` 参数，确认了「文档级入口的画布来自样式而非调用方」。

真正干活且带缓存的是 `layout_document_impl`，它在 [src/pages/mod.rs:L51-L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L51-L74)，里面用 `Locator::root()` 构造根定位器后，转交给 `layout_document_common`：

```rust
#[comemo::memoize]
fn layout_document_impl(/* 6 个 Tracked 参数 + content + styles */) {
    layout_document_common(/* …, */ Locator::root(), styles)
}
```

`layout_document_common`（[src/pages/mod.rs:L128-L172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L128-L172)）是两个入口共享的核心，它做了几件关键的事：

- `styles.to_map().outside()`：把外部样式标记为「页面级有效」（outside），这样页眉页脚等才用得上；
- `info.populate(styles)` / `info.populate_locale(styles)`：填充文档元信息（标题、作者、语言/locale）；
- `routines.realize(RealizationKind::Document { info })`：执行文档级现实化；
- `layout_pages(...)`：切分、并行排版、组装成 `EcoVec<Page>`；
- 最后 `PagedDocument::new(pages, info)`，在构造时顺便构建 introspector。

#### 4.2.4 代码实践

**实践目标**：看清两个文档级入口在 locator 处理上的唯一差异。

**操作步骤**：

1. 打开 [src/pages/mod.rs:L100-L123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L100-L123)（`layout_document_for_bundle_impl`）。
2. 对比 [src/pages/mod.rs:L51-L74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L51-L74)（`layout_document_impl`）。
3. 观察两者传给 `layout_document_common` 的 `locator` 实参分别是什么。

**需要观察的现象**：`layout_document_impl` 传的是 `Locator::root()`（一个全新的根定位器）；而 `layout_document_for_bundle_impl` 先用 `LocatorLink::new(locator)` 包装**外部传入**的 locator，再传 `Locator::link(&link)`。

**预期结果**：你会写出一句话总结——「常规编译从文档根开始定位；bundle 编译把内容嵌入到一个已存在的 locator 上下文里，所以定位器由外部给定」。如果你无法确定 bundle 编译在仓库里的具体触发场景，可标注「待本地验证：在 typst 主 crate 中搜索 `layout_document_for_bundle` 的调用点确认其用途」。

#### 4.2.5 小练习与答案

**练习 1**：`layout_document` 为什么不接收 `Regions` 参数，而 `layout_fragment` 必须接收？

**参考答案**：文档级排版的「画布」就是页面本身，页面尺寸由样式链中的 `page` 配置（纸张大小、边距）决定，入口函数会在 `layout_page_run` 里自行解析，所以不需要调用方传入。而片段级排版是把任意内容排进「调用方指定的任意空间」（一个 grid 单元格、一个块的内边距内……），这个空间的尺寸只有调用方知道，所以必须由调用方通过 `Regions` 提供。

**练习 2**：`PagedDocument::new` 除了保存 `pages` 和 `info`，还多做了一件什么事？

**参考答案**：它还在内部构建了 `PagedIntrospector`（见 `document.rs` 的 `new`）。这意味着 introspector 是 pages 的**纯派生产物**——一旦页面排完，查询索引就自动生成，外部无需单独调用。这一点在 u3-l5 会深入讲解。

---

### 4.3 片段级入口：layout_fragment、layout_frame、layout_columns

#### 4.3.1 概念说明

片段级入口负责把「一段任意内容」排进「调用方给定的区域」。这是 typst-layout 里**被回调得最多**的入口：grid 排单元格、stack 排子项、block 排 body、transform 排变换体……都靠它。

它有三个函数，层级关系一目了然：

- `layout_fragment`：**最通用**。接收 `Regions`（可能多个候选区域），返回 `Fragment`（可能多帧）。内容可以跨区域断裂。
- `layout_frame`：**单区域便捷封装**。接收单个 `Region`，返回单个 `Frame`。内容假定放进一个区域就够。
- `layout_columns`：**列排版专用**，未对外导出。在通用片段排版基础上增加列配置（列数、间距、是否平衡）。

一句话决策规则：**「这段内容会不会跨页/跨列断裂？」** 会 → `layout_fragment`；不会（或调用方只给了一块确定空间）→ `layout_frame`。

#### 4.3.2 核心流程

`layout_frame` 的实现极其简洁，正好暴露了它与 `layout_fragment` 的关系：

```
pub fn layout_frame(region: Region)
   │  region.into()   ← 单 Region 转成无 backlog 的 Regions
   ▼
layout_fragment(regions)  → Fragment
   │  .map(Fragment::into_frame)
   ▼
Frame   ← 断言 Fragment 里恰好只有一帧
```

`layout_fragment` 自己则走「拆 Engine → memoize 的 `layout_fragment_impl` → realize → `layout_flow`」的链路：

```
pub fn layout_fragment
   │  拆 Engine，构造默认 ColumnOptions（单列、不平衡、零间距）
   ▼
layout_fragment_impl  (#[comemo::memoize])
   │  1. 校验：不能向无限尺寸 expand
   │  2. LocatorLink → 派生子 locator
   │  3. check_layout_depth（防递归过深）
   │  4. realize(RealizationKind::Fragment) 决定 Block/Inline
   ▼
layout_flow  ← 真正的流式排版（u4 详讲）
```

#### 4.3.3 源码精读

先看 `layout_frame` 的全部实现，它只有一行核心逻辑：

```rust
pub fn layout_frame(
    engine: &mut Engine, content: &Content, locator: Locator,
    styles: StyleChain, region: Region,
) -> SourceResult<Frame> {
    layout_fragment(engine, content, locator, styles, region.into())
        .map(Fragment::into_frame)
}
```

见 [src/flow/mod.rs:L42-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L42-L51)。两处关键：

1. `region.into()`：把单个 `Region` 转成 `Regions`。看 [regions.rs:L22-L32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L22-L32)，转换时会生成一个 **`backlog` 为空**的 `Regions`——也就是说「后续没有更多候选区域」。
2. `.map(Fragment::into_frame)`：`into_frame` 的契约是「**当且仅当 Fragment 里恰好一帧时**取出该帧，否则 panic」：

```rust
pub fn into_frame(self) -> Frame {
    assert_eq!(self.0.len(), 1, "expected exactly one frame");
    self.0.into_iter().next().unwrap()
}
```

见 [fragment.rs:L34-L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/fragment.rs#L34-L37)。这正是「`layout_frame` 只能处理单区域内容」的根本约束：因为传入的 `Regions` 没有 backlog，flow 只会产出一帧，`into_frame` 才不会 panic。反过来说，**如果你明知内容会断裂成多帧，就绝不能图省事用 `layout_frame`**——它会触发上述断言。

再看 `layout_fragment` 如何拆 Engine 并构造默认列配置：

```rust
pub fn layout_fragment(/* &mut Engine 等 */) -> SourceResult<Fragment> {
    layout_fragment_impl(
        engine.world, engine.library, engine.introspector.into_raw(),
        engine.traced, TrackedMut::reborrow_mut(&mut engine.sink),
        engine.route.track(), content, locator.track(), styles, regions,
        ColumnOptions { count: NonZeroUsize::ONE, balanced: false, gutter: Rel::zero() },
    )
}
```

见 [src/flow/mod.rs:L56-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L56-L80)。注意末尾的 `ColumnOptions` 默认值：`count = 1`、`balanced = false`、`gutter = 0`——即「普通片段排版默认就是单列、不平衡」。`layout_columns` 之所以单独存在，就是为了让 `ColumnsElem` 传入真实的 `count` / `gutter` / `balanced`（见 [src/flow/mod.rs:L87-L111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L87-L111)）。

最后，memoize 的 `layout_fragment_impl`（[src/flow/mod.rs:L114-L170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L114-L170)）里有两处值得记住的细节：

- 开头两处 `bail!`：向无限宽度/高度 `expand` 是非法的（无法把内容拉伸到无穷大）；
- `engine.route.check_layout_depth()`：排版可能递归（block 里套 block），这里限制递归深度，防止栈溢出。

#### 4.3.4 代码实践

**实践目标**：用真实调用点验证「何时用 `layout_frame`、何时用 `layout_fragment`」。

**操作步骤**：

1. 在 `src/` 下搜索 `crate::layout_frame` 与 `crate::layout_fragment` 的所有调用点。
2. 对下面几对形成对比的调用，阅读上下文并判断「内容是否可能断裂」：
   - `src/flow/block.rs` 里**同一个函数** `layout_single_block`（[L43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L43) 用 `layout_frame`）和 `layout_multi_block`（[L147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L147) 用 `layout_fragment`）。
   - `src/grid/mod.rs:55` 排单元格用 `layout_fragment`（单元格内容会随表格跨页）。
   - `src/inline/box.rs:39` 排 box 的 body 用 `layout_frame`（box 是行内不可断的）。
3. 写一句话归纳你看到的规律。

**需要观察的现象**：`single_block`（不可断裂块）和 `box`、`transforms`、`repeat`、`math` 编号这类「注定单帧」的场景，统一用 `layout_frame`；而 `multi_block`（可断裂块）、`grid` 单元格、`stack`、`pad` 这类「可能跨区域」的场景，统一用 `layout_fragment`。

**预期结果**：你能写出这样一条规则——**「内容会被放进一个确定的、不可延伸的空间（单区域）→ `layout_frame`；内容可能溢出到后续区域 → `layout_fragment`」**。这正好对应 `into_frame` 的「恰好一帧」断言。

> 若想进一步验证断言的存在感，可在本地临时把某处 `layout_fragment`（多区域）改成 `layout_frame`（单区域）后 `cargo build`，阅读编译期/逻辑上的矛盾（注意：这只是思考实验，请勿提交修改）。

#### 4.3.5 小练习与答案

**练习 1**：假设你正在写一个新的 layouter，要把用户传入的 Content 排进一个「宽度固定、高度无限」的单列容器，且你**确信**它不会跨页。应该用哪个入口？如果改天需求变成「这段内容可能很长，要能流到下一页」呢？

**参考答案**：确信不跨页 → 用 `layout_frame`（传一个 `Region`，拿一个 `Frame`，简单直接）。可能跨页 → 必须用 `layout_fragment`（传 `Regions`，拿 `Fragment`），否则内容溢出时 `into_frame` 会因「多帧」而 panic。

**练习 2**：`layout_frame` 为什么不会因为 `into_frame` 的「多帧断言」而崩溃？

**参考答案**：因为 `layout_frame` 内部用 `region.into()` 构造的 `Regions` 的 `backlog` 为空（见 `From<Region> for Regions`），flow 排版只会产出唯一一帧，正好满足 `into_frame` 的断言。换句话说，「单区域输入」从结构上保证了「单帧输出」。

---

### 4.4 register：show 规则与 Target::Paged 的粘合层

#### 4.4.1 概念说明

前面三节讲的都是「排版入口」，而 `register` 是一个**不同性质**的对外符号：它不排版，而是**注册 show 规则**。

回顾 u1-l2 的结论：grid、math、stack、lists 等子系统没有出现在 `lib.rs` 的门面里，那它们怎么被驱动？答案就是 `register`——它把一大批 `*_RULE`（类型为 `ShowFn<某元素>` 的函数指针）挂到一张 `NativeRuleMap` 上，目标是 `Target::Paged`。当引擎对一个 paged 文档做 realize 时，遇到这些元素就会查这张表、调用对应的 show 函数，从而**间接**驱动各子系统的 layouter。

`Target::Paged` 表示「分页排版目标」（对应 PDF/打印这种分页输出）。typst 还可以有别的 target（如 typst-html 的 `Target::Html`），每个 target 各注册一套规则。`register` 就是 typst-layout 对 paged target 的规则贡献。

#### 4.4.2 核心流程

```
外部（typst 主 crate）构建 Library
   │  let mut rules = NativeRuleMap::new();
   │  typst_layout::register(&mut rules);   ← 本 crate 贡献 paged 规则
   ▼
register 逐条 rules.register(Paged, XXX_RULE)
   │  按类别：Model / Text / Layout / Visualize / Math / PDF
   ▼
之后排版时，realize 遇到对应元素 → 查表 → 调用 ShowFn
   │  ShowFn 往往把元素包装成 BlockElem::multi_layouter(.., 某layouter)
   ▼
间接驱动 grid/stack/lists/math 等子系统
```

关键洞察：**门面里没有 `layout_grid`，但 `register` 通过 `GRID_RULE` 把它挂进了规则表**。`register` 因此是 layout 与 library 之间真正的「粘合层」。

#### 4.4.3 源码精读

`register` 的签名与分类一目了然：

```rust
pub fn register(rules: &mut NativeRuleMap) {
    use Target::Paged;
    // Model.
    rules.register(Paged, STRONG_RULE);
    rules.register(Paged, EMPH_RULE);
    // …
    // Layout.
    rules.register(Paged, COLUMNS_RULE);
    rules.register(Paged, STACK_RULE);
    rules.register(Paged, GRID_RULE);
    // …
}
```

见 [src/rules.rs:L39-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L39-L112)。所有规则都注册到 `Target::Paged`，并按领域分组：Model（标题/图/脚注/目录…）、Text（上下标/下划线/原文…）、Layout（对齐/边距/列/栈/网格/变换…）、Visualize（图片/线条/形状…）、Math（公式）、PDF（附件/构件标记）。

每条 `*_RULE` 是一个 `ShowFn<E>`——接收元素、引擎、样式，返回 `Content`。它们大致分两种风格：

**风格一：把语义元素转成样式变更**（不直接排版）。例如强调：

```rust
const EMPH_RULE: ShowFn<EmphElem> =
    |elem, _, _| Ok(elem.body.clone().set(TextElem::emph, ItalicToggle(true)));
```

见 [src/rules.rs:L121-L122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L121-L122)，`emph` 只是给 body 打开「斜体」样式，把它变回普通文本流，并不调用 layouter。

**风格二：把元素挂上一个 layouter**（驱动子系统）。例如列、栈、网格：

```rust
const COLUMNS_RULE: ShowFn<ColumnsElem> = |elem, _, _| {
    Ok(BlockElem::multi_layouter(elem.clone(), crate::flow::layout_columns).pack())
};
const GRID_RULE: ShowFn<GridElem> = |elem, _, _| {
    Ok(BlockElem::multi_layouter(elem.clone(), crate::grid::layout_grid).pack())
};
```

见 [src/rules.rs:L678-L688](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L678-L688)。这里就回答了 4.1 的悬念：`layout_columns`、`layout_grid`、`layout_stack` 正是通过 `BlockElem::multi_layouter` 以函数指针形式被挂载，从而在排版时被回调——所以它们无需出现在 crate 的公共门面里。

最后看**外部调用点**，理解 `register` 何时被调用：

```rust
static ROUTINES: LazyLock<Routines> = LazyLock::new(|| Routines {
    rules: || {
        let mut rules = NativeRuleMap::new();
        typst_layout::register(&mut rules);
        typst_html::register(&mut rules);
        rules
    },
```

见 [crates/typst/src/lib.rs:L311-L317](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L311-L317)。typst 主 crate 在构建全局 `Routines` 时，先建一张空规则表，再让 typst-layout 和 typst-html **各自往同一张表里追加**自己的规则。这解释了 `register` 为什么签名是 `&mut NativeRuleMap`——它是在「借用并填充」一张共享表，而不是新建一张。

#### 4.4.4 代码实践

**实践目标**：量化 `register` 注册了多少规则，并理解两种 show 规则风格。

**操作步骤**：

1. 打开 [src/rules.rs:L39-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L39-L112)。
2. 按 6 个分类（Model / Text / Layout / Visualize / Math / PDF）统计 `rules.register` 调用的条数。
3. 挑选 `STRONG_RULE`（[L114-L119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L114-L119)）与 `COLUMNS_RULE`（[L678-L680](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L678-L680)），判断各属哪种风格。

**需要观察的现象**：你能数出每个分类的规则数量（例如 Model 类最多，Layout 类次之）；`STRONG_RULE`/`EMPH_RULE` 属「样式变更型」，`COLUMNS_RULE`/`STACK_RULE`/`GRID_RULE` 属「挂 layouter 型」。

**预期结果**：写出一句话——「`register` 把 N 条规则挂到 `Target::Paged`，其中一部分（如 STRONG/EMPH）只改样式，另一部分（如 COLUMNS/STACK/GRID）通过 `multi_layouter` 把真正的 layouter 接进来」。具体的 N 与各分类计数请以你本地统计为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `register` 接收 `&mut NativeRuleMap`，而不是返回一个新的 `NativeRuleMap`？

**参考答案**：因为 `NativeRuleMap` 是**跨 target 共享**的同一张表。typst 主 crate 先建一张表，再让 typst-layout、typst-html 等各自往这张表里追加规则。如果 `register` 返回新表，就会覆盖掉别的 target 已注册的规则。所以它必须以「借用并填充」的方式注入。

**练习 2**：门面里没有 `layout_grid`，用户写的 `#grid()` 为什么还是能排版出表格？

**参考答案**：因为 `register` 把 `GRID_RULE` 注册到了 paged 规则表；`GRID_RULE` 内部用 `BlockElem::multi_layouter(elem, crate::grid::layout_grid)` 把 grid 元素与 `layout_grid` 函数绑在一起。排版时 realize 遇到 grid 元素 → 查表命中 `GRID_RULE` → 回调 `layout_grid`。所以「公共门面」与「实际驱动的 layouter」是两条路径，`register` 是它们的汇合点。

---

## 5. 综合实践

把本讲的三类公共 API 串起来，完成下面这个「门面审计」任务。

**任务背景**：假设你要给团队写一份《typst-layout 公共 API 速查表》，需要保证每条 API 都有「定义处 + 至少一处真实调用点 + 一句话选用建议」。

**操作步骤**：

1. **盘点门面**：从 [src/lib.rs:L20-L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lib.rs#L20-L24) 列出全部对外符号，按「产物 / 入口 / 粘合」分类。
2. **定位定义**：对每个符号跳转到定义处，记录 `文件:行号`。
3. **找调用点**：
   - `layout_document` 的调用点在 [`src/document.rs:L77`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L77)（`Output::create`）；
   - `layout_frame` 找一处单区域场景（如 [`src/flow/block.rs:L43`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/block.rs#L43)）；
   - `layout_fragment` 找一处多区域场景（如 [`src/grid/mod.rs:L55`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs#L55)）；
   - `register` 的调用点在 [`crates/typst/src/lib.rs:L314`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst/src/lib.rs#L314)；
   - `Page` / `PagedDocument` 的消费点在 typst-svg / typst-render / typst-cli（如 [`crates/typst-cli/src/compile.rs:L19`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L19) 的 `use typst_layout::{Page, PagedDocument};`）。
4. **写选用建议**：重点写清楚 `layout_frame` 与 `layout_fragment` 的抉择——结合 `Fragment::into_frame` 的「恰好一帧」断言说明：**单区域、内容不断裂 → `layout_frame`；多区域、内容可能断裂 → `layout_fragment`**。
5. **画一张调用关系草图**：标出 `Output::create → layout_document → layout_pages`，以及 `各 layouter → layout_frame/layout_fragment → layout_flow` 这两条进入引擎的路径。

**预期产出**：一张三列表（符号 / 定义 / 调用点）+ 一段 `layout_frame` vs `layout_fragment` 的选用说明 + 一张进入引擎的路径草图。这是后续阅读 flow（u4）与 pages（u3）时的「导航手册」。

> 如果某些外部调用点（如 bundle 编译、svg 消费 Page 的细节）你无法在本讲范围内完全确认，请在速查表里标注「待本地验证」，不要臆测。

## 6. 本讲小结

- `lib.rs` 是纯索引，通过 5 条 `pub use` 对外暴露全部公共 API：产物（`Page`、`PagedDocument`）、文档级入口（`layout_document`、`layout_document_for_bundle`）、片段级入口（`layout_fragment`、`layout_frame`）、粘合层（`register`、`PagedIntrospector`）。
- **文档级入口不接收 `Regions`**——页面画布由样式链里的 `page` 配置决定；它走 `realize(Document) → layout_pages → PagedDocument::new`。
- **片段级入口必须接收 `Regions`**；`layout_frame` 只是 `layout_fragment` 的单区域便捷封装：内部用 `region.into()` 构造无 backlog 的 `Regions`，再用 `Fragment::into_frame` 取出唯一一帧。
- 「何时用谁」的根本判据是 `Fragment::into_frame` 的「恰好一帧」断言：**内容可能跨区域断裂 → 必须用 `layout_fragment`；注定单帧 → 用 `layout_frame`**。
- 所有入口都遵循「公开薄封装 → `#[comemo::memoize]` 的 `*_impl`」模式，先把 `&mut Engine` 拆成可追踪参数再进缓存层。
- `register` 是 layout 与 library 的真正粘合层：它把 `*_RULE` 挂到 `Target::Paged`，其中「挂 layouter 型」规则（如 `GRID_RULE`、`COLUMNS_RULE`）通过 `BlockElem::multi_layouter` 把未导出的 layouter 接进排版流程——这解释了为什么门面里没有 `layout_grid`。

## 7. 下一步学习建议

本讲只看了「门面」，还没真正进入任何子系统。建议按以下顺序继续：

1. **u2-l1（Engine 与 comemo）**：本讲反复出现的「拆 Engine → memoize」模式将在那里系统讲解，理解 `Tracked` / `TrackedMut` / `Route` / `Sink` 之后，你会看懂入口函数为什么要那样拆参数。
2. **u2-l2（Regions）与 u2-l3（Frame / Fragment）**：本讲提到的 `Region` / `Regions` / `Fragment::into_frame` 的细节会在那里展开，是理解「内容如何填入区域、结果如何承载」的基础。
3. **u3-l1（layout_document 实现）**：深入 `layout_document_common` 与 `layout_pages` 的三段式（collect → 并行 → finalize），把本讲 4.2 的链路补全。
4. 想提前感受「show 规则如何驱动 layouter」，可先扫读 [`src/rules.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs) 中 `STACK_RULE` / `GRID_RULE` / `LIST_RULE` 三条规则，它们分别对应 u6 将详讲的 stack / grid / lists 子系统。
