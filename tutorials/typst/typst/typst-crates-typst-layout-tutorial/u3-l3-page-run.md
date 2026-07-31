# 页面运行 layout_page_run：边距与页眉页脚

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `layout_page_run` 在「collect → 并行排版 → finalize」三段式中的位置，以及它为什么是**可并行**的那一步。
- 看懂从样式链（`PageElem` 的各字段）解析出页面尺寸、边距、出血（bleed）、填充色、页码、页眉页脚、前景背景的完整过程。
- 解释「页码到底进 header 还是 footer」的判定规则，以及 `header_ascent` / `footer_descent` 如何缩小页眉页脚的可用区域。
- 理解 `LayoutedPage` 为什么把「边距值」和「是否双面（two_sided）」分两个字段暂存，而把左右互换推迟到 finalize。
- 理解正文用 `layout_flow(FlowMode::Root)` 排版、各 marginal 用 `layout_marginal` 闭包排版的分工。

## 2. 前置知识

本讲假设你已学习以下内容（见前置讲义摘要）：

- **u1-l4 / u3-l1**：文档级排版的全链路 `layout_document → realize → layout_pages → PagedDocument::new`，以及 `layout_document_common` 装配线。
- **u3-l2**：`collect` 把扁平 `Vec<Pair>` 切成 `Item::Run / Tags / Parity`，只有 `Run` 进入并行排版。
- **u2-l1**：「公开薄封装 + `#[comemo::memoize]` 的 `_impl`」模式，以及 `Tracked` / `TrackedMut` 参数的来源。
- **u2-l2 / u2-l3**：`Regions` / `Region` 作为排版画布输入，`Frame` / `Fragment` 作为排版产物。
- **u4-l1**：`layout_flow` 主循环与 `FlowMode`（本讲会用到 `FlowMode::Root`，但不深入其内部）。

如果你还不熟悉「Pair」「StyleChain」「RealizationKind」，建议先回顾 u1-l4 与 u3-l1。本讲**不展开** finalize 的左右互换细节（那是 u3-l4 的内容），但会解释为什么 run 阶段做不了互换、必须暂存标记。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/pages/run.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs) | 本讲主角。定义 `layout_page_run`：解析页面级样式、排版正文与各 marginal、产出 `Vec<LayoutedPage>`。 |
| [src/pages/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs) | `layout_pages` 的三段式：collect → `engine.parallelize` 调用 `layout_page_run` → 循环 `finalize`。本讲只看它如何调用 run。 |
| [src/pages/finalize.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs) | `finalize`：拿到 `LayoutedPage` 和物理页号后，做左右互换、按顺序拼装整页 `Frame`、推进页码计数器。本讲引用它来说明「为什么 run 要暂存 two_sided」。 |
| `crates/typst-library/src/layout/page.rs`（兄弟 crate，本 crate 仅消费） | 定义 `PageElem` 的全部字段（`width`/`height`/`flipped`/`margin`/`bleed`/`numbering`/`number_align`/`header`/`footer`/`header_ascent`/`footer_descent` 等）以及 `Margin<T>` 结构。本讲通过 `styles.get(PageElem::xxx)` / `styles.resolve(...)` 读取它们。 |

> 说明：`PageElem`、`Margin`、`Binding`、`Paper`、`Frame`、`Regions` 等类型都定义在 **typst-library** 中；typst-layout 只是消费与构造它们。这与 u1-l2 提到的「typst-library 提供类型骨架」一致。

---

## 4. 核心概念与源码讲解

### 4.1 layout_page_run 的位置与职责

#### 4.1.1 概念说明

回顾 u3-l2：`layout_pages` 把文档切成三类 `Item`，其中只有 `Item::Run` 是「真正可以并行排版的连续正文片段」。`layout_page_run` 就是处理一个 `Run` 的函数：

> 给定一组 `children`（扁平 `Pair` 列表）和初始样式链，排版出「若干张内容上连续、但还差最后一步的页面」。

这里的关键修饰词是「还差最后一步」。因为页面边距可能取决于**物理页号**（双面打印时左右页的 inside/outout 不同），而物理页号在并行排版阶段是未知的——它要等所有 run 排完、按顺序串行 finalize 时才能确定。所以 run 的产物 `LayoutedPage` 被刻意设计成「**几乎完成、只差页号**」的中间形态（这正是它文档注释的原话）。

#### 4.1.2 核心流程

`layout_page_run_impl` 内部可以分成四步：

```text
1. 建立局部 Engine、Locator
   ├─ Protected::from_raw(introspector)  // 防止在并行任务里误写
   ├─ LocatorLink::new + Locator::link().split()  // 给本 run 的元素分配身份
   └─ Route::extend(route)  // 排版深度跨 memoize 边界累加

2. 解析页面级样式（PageElem 各字段）
   ├─ Styles::root(children, initial)  // 抽出本 run 共享的「主干样式」
   ├─ width / height / flipped → size
   ├─ margin / bleed（含 two_sided 标记）
   ├─ fill / foreground / background
   ├─ numbering / supplement / number_align / binding
   └─ header_ascent / footer_descent

3. 排版正文
   └─ layout_flow(..., FlowMode::Root)  // 产出 Fragment（每帧 = 一页正文 inner）

4. 逐帧排版各 marginal，组装 LayoutedPage
   ├─ header / footer / background / foreground 用 layout_marginal 闭包排版
   └─ 把所有字段塞进 LayoutedPage（margin/bleed 连同 two_sided 标记一起暂存）
```

#### 4.1.3 源码精读

先看入口的「公开薄封装 → memoize 的 `_impl`」结构，这正是 u2-l1 讲过的通用模式：

[layout_page_run 把 Engine 拆成 tracked 参数后转入 _impl（run.rs:57-74）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L57-L74)

```rust
pub fn layout_page_run(engine, children, locator, initial) -> SourceResult<Vec<LayoutedPage>> {
    layout_page_run_impl(
        engine.world, engine.library,
        engine.introspector.into_raw(),
        engine.traced, TrackedMut::reborrow_mut(&mut engine.sink),
        engine.route.track(), children, locator.track(), initial,
    )
}
```

注意 `engine.introspector.into_raw()`：因为 `Engine.introspector` 字段是 `Protected`（不可在并行任务里被改写），这里用 `into_raw()` 取出内部的 `Tracked` 再传进 `_impl`，随后在 `_impl` 里又用 `Protected::from_raw` 重新包回去：

[在 _impl 开头重建局部 Engine（run.rs:90-100）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L90-L100)

```rust
let introspector = Protected::from_raw(introspector);
let link = LocatorLink::new(locator);
let mut locator = Locator::link(&link).split();
let mut engine = Engine { library, world, introspector, traced, sink,
                          route: Route::extend(route) };
```

这段和 u3-l1 的 `layout_document_common` 几乎一样，差别只在于：document 那里是 `Route::extend(route).unnested()`（顶层干净起步），而 run 这里只是 `Route::extend(route)`——因为 run 是 document 的子调用，排版深度应当**累加**进父级链路（参见 u2-l1 关于 `Route::extend` 与 `check_layout_depth` 阈值 72 的说明）。

`layout_pages` 又是如何调用它的？看并行那一行：

[layout_pages 用 parallelize 并行调用 layout_page_run（mod.rs:184-195）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L184-L195)

```rust
let mut runs = engine.parallelize(
    items.iter().filter_map(|item| match item {
        Item::Run(children, initial, locator) =>
            Some((children, initial, locator.relayout())),
        _ => None,
    }),
    |engine, (children, initial, locator)| {
        layout_page_run(engine, children, locator, *initial)
    },
);
```

每个 `Run` 用 `locator.relayout()` 复用身份（u2-l4），交给一个独立的并行任务。注意 `Tags` 和 `Parity` 在这里被 `filter_map` **过滤掉**了——它们依赖串行全局状态，不走并行排版。这也回答了一个常见疑问：**为什么 run 一次能返回多张页？** 因为一个 `Run`（两次分页符之间的连续正文）可能由于内容溢出、浮动体、脚注等在 `layout_flow` 里自然跨页，所以返回的是 `Vec<LayoutedPage>`。

#### 4.1.4 代码实践

**实践目标**：确认 `layout_page_run` 是三段式中「可并行」的那一步。

**操作步骤**：

1. 打开 [src/pages/mod.rs:184-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L184-L195)，确认 `Item::Run` 走 `parallelize`，而 `Item::Tags` / `Item::Parity` 走下方 `for item in &items` 的串行循环。
2. 在 `layout_page_run_impl` 返回前（[run.rs:247](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L247) 附近）临时插入一行：
   ```rust
   eprintln!("[run] produced {} page(s)", layouted.len());
   ```
3. 运行 `cargo test -p typst-layout`（或编译一个含 `#pagebreak()` 与溢出正文的小文档）。

**需要观察的现象**：`[run] produced N page(s)` 可能出现 N > 1（一个 run 产出多页），且多条 `[run]` 日志的相对顺序可能交错（说明并行）。

**预期结果**：确认 run 是并行单元、可一次返回多张页。**注意这只是临时调试日志，验证后请还原源码（本讲禁止修改源码，仅作阅读理解用途）。**

> ⚠️ 本讲要求不修改源码，因此上面这条 eprintln 属于「阅读理解型」建议；如果你不便改源码，可改为纯阅读：跟踪 `layout_flow` 返回的 `fragment.len()` 如何等于 `layouted.len()`（见 4.4.3）。

#### 4.1.5 小练习与答案

**练习 1**：`layout_page_run` 为什么不直接返回最终的 `Page`，而要返回「半成品」`LayoutedPage`？

**参考答案**：因为双面打印时左右页的 inside/outside 边距不同，互换取决于**物理页号**；而物理页号在并行阶段未知（各 run 并发执行、互不感知全局页号），只能等所有 run 排完后在串行 `finalize` 阶段确定。所以 run 把「能确定的」都排好，把「依赖页号的左右互换」连同 `two_sided` 标记暂存，留给 finalize。

**练习 2**：`layout_blank_page`（奇偶补页用）和正常 run 走的是同一条路径吗？

**参考答案**：是。`layout_blank_page` 内部就是 `layout_page_run(engine, &[], locator, initial)`——传入空 `children`，取返回的第一个 `LayoutedPage`（[run.rs:46-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L46-L53)）。空 children 意味着没有正文，但仍会排版页眉页脚/背景等 marginal，正好用于补一张空页。

---

### 4.2 页面尺寸与边距解析（PageElem）

#### 4.2.1 概念说明

`layout_page_run` 第 2 步是从样式链读出 `PageElem` 的各字段，决定「这张页多大、四周留多少白、要不要出血」。几个要点：

- **`width` / `height` 是 `Smart<Length>`**：`Auto` 表示「随内容」（在这一步被解析为无穷大 `Abs::inf()`，使页面在该轴上自动撑开）。
- **`flipped`**：横竖翻转，交换 `size.x` 与 `size.y`。
- **`margin` 是 `Smart<Margin<Smart<Rel<Length>>>>`**：嵌套了两层 `Smart`。外层 `Smart` 决定「用默认边距还是自定义」；内层每条边的 `Smart::custom` 决定「用默认值还是这条边自定义值」。`Margin<T>` 还带一个 `two_sided: Option<bool>`，标记是否按 inside/outout 解释左右边距。
- **`bleed`（出血）**：印刷用的裁切余量，结构与 margin 类似，但默认为 0。

#### 4.2.2 核心流程

```text
width  = PageElem::width.resolve()  否则 inf
height = PageElem::height.resolve() 否则 inf
size   = (width, height)
if flipped: swap(size.x, size.y)

# 用于「相对边距」基准与默认边距计算
min = min(width, height);  若非有限 → A4 宽度（210mm）
default = (2.5/21.0) * min           # ≈ 11.9% × min，约 A4 的 25mm

margin = PageElem::margin.unwrap_or_default()
margin_two_sided = margin.two_sided.unwrap_or(false)
margin.sides 每条边：.custom 取值 否则 default → resolve(styles).relative_to(size)

bleed 同理，但默认每边为 Rel::zero()
```

默认边距公式 `2.5 / 21.0 * min` 值得记住：A4 宽 210mm，`2.5/21 × 210mm = 25mm`，这正是 Typst 默认 25mm 页边距的来源。`min` 取宽高较小者，保证在窄页面（如 A5）下边距也按比例缩小。

#### 4.2.3 源码精读

[页面尺寸：width/height/flipped（run.rs:106-113）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L106-L113)

```rust
let width = styles.resolve(PageElem::width).unwrap_or(Abs::inf());
let height = styles.resolve(PageElem::height).unwrap_or(Abs::inf());
let mut size = Size::new(width, height);
if styles.get(PageElem::flipped) {
    std::mem::swap(&mut size.x, &mut size.y);
}
```

`styles.resolve(...)` 会把 `Smart::Auto` 视作「无法解析」返回 `None`，于是 fallback 到 `Abs::inf()`。注释点明：**当某一轴为无穷大时，页面在该轴上「贴合内容」**——这正是 `#set page(height: auto)` 让页面随内容增长的实现方式。

[默认边距基准 min 与 A4 兜底（run.rs:115-118）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L115-L118)

```rust
let mut min = width.min(height);
if !min.is_finite() {
    min = Paper::A4.width();   // 内容驱动页面时，默认边距仍以 A4 宽为基准
}
```

当 `height` 为 `auto`（无穷）时，`min` 会落到 `width`（有限），不会触发兜底；只有宽高都 auto（极端情况）才用 A4 宽。

[边距解析：default + two_sided + relative_to(size)（run.rs:120-128）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L120-L128)

```rust
let default = Rel::<Length>::from((2.5 / 21.0) * min);
let margin = styles.get(PageElem::margin).unwrap_or_default();
let margin_two_sided = margin.two_sided.unwrap_or(false);
let margin = margin
    .sides
    .map(|side| side.and_then(Smart::custom).unwrap_or(default))  // 每条边：自定义 or 默认
    .resolve(styles)
    .relative_to(size);   // 相对长度（如 10%）按 size 解析
```

注意 `side.and_then(Smart::custom)`：`Smart<Rel>` 有 `Auto` / `Custom` 两态，`and_then(Smart::custom)` 把「显式给定的值」留下、「Auto」变成 `None`，再用默认值兜底。

[出血 bleed：默认 0（run.rs:130-136）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L130-L136)

```rust
let bleed = styles.get(PageElem::bleed);
let bleed_two_sided = bleed.two_sided.unwrap_or(false);
let bleed = bleed.sides
    .map(|side| side.unwrap_or(Rel::zero()))   // 未指定 → 0
    .resolve(styles).relative_to(size);
```

`bleed` 与 `margin` 结构几乎一致，区别仅在默认值（0 而非 default）。这里把 `bleed_two_sided` 也单独记录，原因和 margin 相同——finalize 时按物理页号决定是否左右互换。

#### 4.2.4 代码实践

**实践目标**：手算默认边距，验证 `2.5/21` 公式。

**操作步骤**：

1. 假设一张 A4 纸：`width = 595.28pt`（210mm），`height = 841.89pt`（297mm），`flipped = false`。
2. 计算 `min = width.min(height) = 595.28pt`。
3. 计算 `default = (2.5 / 21.0) × 595.28 ≈ 70.86pt ≈ 25mm`。
4. 若用户未设任何边距，`margin` 四条边全部 fallback 到 `default`。

**需要观察的现象**：默认四边边距相等且约为 25mm。

**预期结果**：`default ≈ 70.86pt`。若 `#set page(flipped: true)`，则 `min` 仍取宽高较小者（翻转后 `size` 已交换，但 `min` 在交换**之后**取，所以对正反面尺寸对称的纸张结果一致；对非正方形且原 height < width 的自定义纸张则需注意 `min` 基于翻转后的 size）。

**待本地验证**：第 4 步在非标准纸张下 `min` 的取值，建议写一个最小 typst 文档 `#set page(width: 100mm, height: 60mm, flipped: true)` 后用 `context page.margin` 观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认边距用 `min(width, height)` 而不是固定值或 `width`？

**参考答案**：用较小者作基准能让边距随纸张大小**按比例**缩放，避免在小纸张（如 A5）上出现「边距占满页面」或在大纸张上「边距过小」的不协调。取较小者还保证窄边方向（通常是宽度）的边距比例合理。

**练习 2**：`margin.sides.map(|side| side.and_then(Smart::custom).unwrap_or(default))` 中，如果用户只写了 `#set page(margin: (left: 0pt))`，其余三边会得到什么？

**参考答案**：left = 0pt，其余 top/right/bottom = `default`。因为 `Margin` 的 `FromValue` 把未指定的边留作 `None`（见 typst-library 的 `Margin::from_value`），`and_then` 把 `None` 透传，`unwrap_or(default)` 再用默认值兜底。这正是「只指定一条边、其余用默认」的行为来源。

---

### 4.3 页码归属与页眉页脚分派

#### 4.3.1 概念说明

页面级的「页码」并不是一个独立放在固定位置的元素，而是一种特殊的 marginal：

- 若 `numbering` 已设置（如 `"1"` 或 `"1 of 1"`），Typst 会构造一个 `CounterDisplayElem`（显示 page 计数器）作为「页码内容」。
- 这个页码内容**根据 `number_align` 的垂直分量**决定进 header 还是 footer：`y = top` 进 header，`y = bottom`（默认）进 footer。
- 但有一个覆盖规则：**如果用户显式提供了与页码归属方向一致的 `header`/`footer`，则页码被忽略**（`header`/`footer` 优先）。

#### 4.3.2 核心流程

```text
numbering_marginal = numbering 存在 ? 构造 CounterDisplayElem(page, numbering, both) : None
    # both = 模式片段数 ≥ 2（如 "1 of 1" 需要总数）或 numbering 是函数

header, footer =
    if number_align.y == Top:
        (header.unwrap_or(numbering_marginal), footer.unwrap_or(None))
    else:  # Bottom（默认）
        (header.unwrap_or(None), footer.unwrap_or(numbering_marginal))
```

即：页码流向由 `number_align.y` 决定；显式 header/footer 优先于自动页码。

#### 4.3.3 源码精读

[构造页码 marginal：CounterDisplayElem（run.rs:157-178）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L157-L178)

```rust
let numbering_marginal = numbering.as_ref().map(|numbering| {
    let both = match numbering {
        Numbering::Pattern(pattern) => pattern.pieces() >= 2,  // 如 "1 / 1" 需要总数
        Numbering::Func(_) => true,
    };
    let mut counter = CounterDisplayElem::new(
        Counter::new(CounterKey::Page),
        Smart::Custom(numbering.clone()),
        both,
    ).pack();
    // 把 number_align 的 X 分量当对齐用，忽略 Y（Y 只用于选 header/footer）
    if let Some(x) = number_align.x() {
        counter = counter.aligned(x.into());
    }
    counter
});
```

注释明确写道：「我们把 Y 对齐解读为选择 header 或 footer，然后在真正对齐数字时忽略它」。所以 `number_align` 的两个分量职责不同：**X 控制水平对齐（左/中/右），Y 控制页码归属（顶/底）**。

[页码进 header 还是 footer：number_align.y（run.rs:180-186）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L180-L186)

```rust
let header = styles.get_ref(PageElem::header);
let footer = styles.get_ref(PageElem::footer);
let (header, footer) = if matches!(number_align.y(), Some(OuterVAlignment::Top)) {
    (header.as_ref().unwrap_or(&numbering_marginal),
     footer.as_ref().unwrap_or(&None))
} else {
    (header.as_ref().unwrap_or(&None),
     footer.as_ref().unwrap_or(&numbering_marginal))
};
```

这里的 `unwrap_or` 实现了「显式优先」：若 `header`（用户显式给的内容）存在就用它，否则才退回 `numbering_marginal`（自动页码）。对照 typst-library 中 `PageElem::number_align` 的文档：「若给了与归属方向匹配的显式 header/footer，则页码被忽略」——正是这几行 `unwrap_or` 的语义。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：回答两个问题——① `number_align.y == Top` 时页码出现在哪里；② `header_ascent` / `footer_descent` 如何缩小 marginal 可用区域。

**操作步骤 / 分析**：

**问题①**：阅读 [run.rs:182-186](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L182-L186)。当 `number_align.y()` 是 `Some(OuterVAlignment::Top)` 时，走 `if` 分支：`header` 取 `header.unwrap_or(&numbering_marginal)`，`footer` 取 `footer.unwrap_or(&None)`。即**页码被放进 header（顶部页眉）**；若 `y == Bottom`（默认），则页码进 footer。

所以：「页码默认在底部（footer）」；设置 `#set page(number-align: top)` 后页码出现在顶部（header）。注意 `number_align` 的默认值是 `Center + Bottom`（见 typst-library `PageElem::number_align` 的 `#[default(... OuterVAlignment::Bottom)]`）。

**问题②**：阅读 [run.rs:141-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L141-L143) 与构建 marginal 尺寸的 [run.rs:226-227](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L226-L227)：

```rust
let header_ascent = styles.resolve(PageElem::header_ascent).relative_to(margin.top);
let footer_descent = styles.resolve(PageElem::footer_descent).relative_to(margin.bottom);
// ...
let header_size = Size::new(inner.width(), margin.top - header_ascent);
let footer_size = Size::new(inner.width(), margin.bottom - footer_descent);
```

**分析**：`header_ascent` 是「页眉向顶部边距内上移的量」，默认 `30%`（typst-library 中 `#[default(Ratio::new(0.3).into())]`），相对 `margin.top` 解析。`footer_descent` 同理，默认 `30%`，相对 `margin.bottom`。它们的作用是**缩小页眉/页脚的可用排版高度**：

- 页眉的可用高度 = `margin.top - header_ascent`（把页眉往下压，留出顶部 ascent 空隙）。
- 页脚的可用高度 = `margin.bottom - footer_descent`（把页脚往上提，留出底部 descent 空隙）。

**需要观察的现象**：若 `margin.top = 70.86pt`、`header_ascent = 30%`，则 `header_ascent ≈ 21.26pt`，页眉可用高度 = `70.86 − 21.26 ≈ 49.6pt`。增大 `header-ascent` 会进一步压缩页眉可用高度，极端值可能让页眉内容溢出。

**预期结果**：`number_align.y == Top` → 页码进 header；ascent/descent 通过从边距高度中**减去**对应比例来缩小 marginal 可用区域。

#### 4.3.5 小练习与答案

**练习 1**：如果同时设置了 `numbering: "1"` 和 `header: [My Header]`（默认 `number-align: bottom`），页码还会显示吗？

**参考答案**：会显示，但在 **footer**。因为默认 `number_align.y == Bottom`，走 `else` 分支：`footer = footer.unwrap_or(&numbering_marginal)`（用户没给 footer，所以页码进 footer），而 `header` 用用户给的 `My Header`。页码与自定义 header 共存，互不干扰。

**练习 2**：`numbering: "1 / 1"` 里的 `both = true` 起什么作用？

**参考答案**：`pattern.pieces() >= 2` 表示该编号模式需要「总数」（第二片段），于是 `CounterDisplayElem::new(..., both=true)` 会同时查询当前页号与总页数，才能渲染出 `3 / 10` 这样的形式。单片段模式（如 `"1"`）则 `both = false`，只需当前页号。

---

### 4.4 正文排版与 marginal 组装

#### 4.4.1 概念说明

样式解析完成后，第 3、4 步是真正的「排版」：

- **正文**：调用 `layout_flow(..., FlowMode::Root)`。`FlowMode::Root` 表示这是页面根级排版（区别于 `Block` / `Inline`，详见 u4-l1），它会处理分栏、脚注、浮动体等，返回一个 `Fragment`——其中**每一帧对应一页的正文区域（inner）**。
- **marginal**：用一个闭包 `layout_marginal` 把每类 marginal（header/footer/background/foreground）排版进各自的 `Region`。闭包内部调用 `crate::layout_frame`（即 u1-l3 讲过的「单区域便捷封装」）。
- **组装**：对 `fragment` 里的每一帧 inner，分别排版 header/footer/background/foreground，连同已解析的 margin/bleed/two_sided 等字段，塞进一个 `LayoutedPage`。

#### 4.4.2 核心流程

```text
area = size - margin.sum_by_axis()           # 正文可用区域（扣除四边距）
fragment = layout_flow(children, Regions::repeat(area, expand), ColumnOptions{...}, FlowMode::Root)
#   Regions::repeat(area, area.is_finite): 无限重复同尺寸区域 → 自动跨页
#   expand = (area.x 有限, area.y 有限)：内容驱动轴不强制填满

layout_marginal(content, area, align):
    若 content 为 None → None
    否则 crate::layout_frame(aligned(content), Region::new(area, expand=true))

对 fragment 中每个 inner:
    header_size   = (inner.width, margin.top    - header_ascent)
    footer_size   = (inner.width, margin.bottom - footer_descent)
    full_size     = inner.size() + margin.sum_by_axis() + bleed.sum_by_axis()
    LayoutedPage { inner, header, footer, background, foreground,
                   fill, numbering, supplement,
                   margin, margin_two_sided, bleed, bleed_two_sided, binding }
```

#### 4.4.3 源码精读

[正文区域与 layout_flow 调用（run.rs:188-202）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L188-L202)

```rust
let area = size - margin.sum_by_axis();
let fragment = layout_flow(
    &mut engine, children, &mut locator, styles,
    Regions::repeat(area, area.map(Abs::is_finite)),
    ColumnOptions {
        count: styles.get(PageElem::columns),
        balanced: styles.get(ColumnsElem::balanced),
        gutter: styles.get(ColumnsElem::gutter).resolve(styles),
    },
    FlowMode::Root,
)?;
```

几个关键点：

- `area = size - margin.sum_by_axis()`：从整页尺寸里扣掉四边距，得到**正文（inner）可用区域**。注意 bleed 不在这里扣——bleed 是给印刷裁切用的，不算正文空间。
- `Regions::repeat(area, area.map(Abs::is_finite))`：这是 u2-l2 讲的 `Regions` 构造。`repeat` 表示「用同一个 area 无限重复」（`last = area`，无 backlog）。第二参数 `expand` 取 `area` 每轴「是否有限」：若 `height = auto`（inf），则 y 轴 `expand=false`，正文可以自由向下生长而不被强制填满固定高度——这正是 auto 高度页能贴合内容的原因。
- `ColumnOptions`：把 `PageElem::columns`、`ColumnsElem::balanced`、`ColumnsElem::gutter` 传给 flow，由 flow 内部把 `area` 切成多列（详见 u4-l6）。

[marginal 排版闭包 layout_marginal（run.rs:204-216）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L204-L216)

```rust
let mut layout_marginal = |content: &Option<Content>, area, align| {
    let Some(content) = content else { return Ok(None) };
    let aligned = content.clone().set(AlignElem::alignment, align);
    crate::layout_frame(
        &mut engine, &aligned, locator.next(&content.span()), styles,
        Region::new(area, Axes::splat(true)),
    ).map(Some)
};
```

- 闭包对每类 marginal 复用：content 为 `None` 直接返回 `None`（该 marginal 不存在）。
- `content.set(AlignElem::alignment, align)` 把对齐（如 header 用 `Alignment::BOTTOM`、background 用 `Center+Horizon`）注入内容。
- `crate::layout_frame`（u1-l3）是 `layout_fragment` 的单区域封装，这里用 `Region::new(area, Axes::splat(true))`——`expand = (true, true)` 表示 marginal 内容**强制填满**给定 area（与正文不同，marginal 尺寸是确定的）。

[marginal 尺寸与循环组装（run.rs:219-245）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L219-L245)

```rust
let header = header.clone().map(|h| h.artifact(ArtifactKind::Header));
let footer = footer.clone().map(|f| f.artifact(ArtifactKind::Footer));
let background = background.clone().map(|b| b.artifact(ArtifactKind::Background));

for inner in fragment {
    let header_size = Size::new(inner.width(), margin.top - header_ascent);
    let footer_size = Size::new(inner.width(), margin.bottom - footer_descent);
    let full_size = inner.size() + margin.sum_by_axis() + bleed.sum_by_axis();
    let mid = HAlignment::Center + VAlignment::Horizon;
    layouted.push(LayoutedPage {
        inner, fill: fill.clone(), numbering: numbering.clone(),
        supplement: supplement.clone(),
        header: layout_marginal(&header, header_size, Alignment::BOTTOM)?,
        footer: layout_marginal(&footer, footer_size, Alignment::TOP)?,
        background: layout_marginal(&background, full_size, mid)?,
        foreground: layout_marginal(foreground, full_size, mid)?,
        margin, margin_two_sided, bleed, bleed_two_sided, binding,
    });
}
```

要点：

- **`.artifact(ArtifactKind::Header/Footer/Background)`** 把 marginal 标记为 PDF「artifact」（辅助内容），导出 PDF 时不会被识别为正文（影响可读性与无障碍）。`foreground` 没有 artifact 标记。
- **header_size / footer_size**：宽度取 `inner.width()`（正文宽），高度是「边距 − ascent/descent」（见 4.3.4）。
- **full_size（background/foreground）**：`inner + margin + bleed`，即**整页含出血**的完整尺寸。这与 typst-library 中 `background` 字段文档一致：「background 里的相对长度按含 bleed 的整页尺寸解析」。
- **对齐**：header 用 `Alignment::BOTTOM`（贴底，靠近正文），footer 用 `Alignment::TOP`（贴顶，靠近正文），background/foreground 用 `Center + Horizon`（居中）。这些对齐通过 `layout_marginal` 闭包里的 `set(AlignElem::alignment, align)` 生效。

#### 4.4.4 代码实践

**实践目标**：跟踪「一帧 inner 对应一个 LayoutedPage」，并理解 marginal 尺寸的来源。

**操作步骤**：

1. 阅读 [run.rs:225](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L225) 的 `for inner in fragment`，确认循环次数 = `fragment.len()` = `layout_flow` 产出的页数。
2. 对一个具体数值（A4、默认 25mm 边距、默认 30% ascent）填空：
   - `inner.width()` = 595.28 − 2×70.86 = 453.56pt
   - `header_size = (453.56, 70.86 − 70.86×0.3) = (453.56, 49.60)`pt
   - `footer_size = (453.56, 49.60)`pt
   - 若 bleed 全 0：`full_size = inner.size() + margin.sum + 0`

**需要观察的现象**：header/footer 宽度等于正文宽、高度等于「边距 − 比例」；background/foreground 是整页尺寸。

**预期结果**：每个 inner 帧生成一个完整的 `LayoutedPage`，所有 marginal 都已排版成 `Option<Frame>`，只等 finalize 拼装。

#### 4.4.5 小练习与答案

**练习 1**：为什么 background/foreground 用 `full_size`（含 margin + bleed），而 header/footer 用「正文宽 × (边距 − ascent/descent)」？

**参考答案**：background/foreground 是**整页装饰**（水印、背景图、前景覆盖），需要覆盖包括边距和出血在内的整张纸，所以尺寸 = inner + margin + bleed。header/footer 是**页边距内的条带**，只占顶部/底部边距的一部分（还要再减去 ascent/descent 留出空隙），所以尺寸是「正文宽 × 缩减后的边距高」。

**练习 2**：`for inner in fragment` 这个循环保证了什么不变量？

**参考答案**：保证「`LayoutedPage` 数量 == 正文页数」。`layout_flow(FlowMode::Root)` 决定了一个 run 产出几页正文（可能因溢出/分栏/脚注而 > 1），每页正文 inner 都会配上各自的 marginal，组装成对应数量的 `LayoutedPage`。这也是 `layout_page_run` 返回 `Vec<LayoutedPage>` 的根本原因。

---

### 4.5 LayoutedPage：为什么暂存两套 margin/bleed

#### 4.5.1 概念说明

`LayoutedPage` 是 run 阶段的产物，它把「一张页所有已确定的信息」打包，唯独**不**做左右互换。看它的字段：

```rust
pub struct LayoutedPage {
    pub inner: Frame,
    pub margin: Sides<Abs>,           // 已解析的边距值
    pub margin_two_sided: bool,       // 是否按 inside/outside 解释
    pub bleed: Sides<Abs>,
    pub bleed_two_sided: bool,
    pub binding: Binding,             // 装订方向（Left/Right）
    pub header: Option<Frame>,
    pub footer: Option<Frame>,
    pub background: Option<Frame>,
    pub foreground: Option<Frame>,
    pub fill: Smart<Option<Paint>>,
    pub numbering: Option<Numbering>,
    pub supplement: Content,
}
```

注意 margin/bleed 各占**两个字段**：值（`Sides<Abs>`）+ 标记（`two_sided: bool`）。这就是讲义标题里「两套 margin/bleed」的含义——不是两份完整的边距数据，而是「值 + 是否双面」这一对字段。

#### 4.5.2 核心流程

```text
run 阶段（并行，不知物理页号）：
    margin = 解析好的对称 Sides<Abs>
    margin_two_sided = 是否需要 inside/outside 互换
    → 暂存进 LayoutedPage（暂不互换）

finalize 阶段（串行，已知物理页号）：
    swap = binding.swap(counter.physical())   # 该页是否需要左右互换
    if margin_two_sided && swap: swap(margin.left, margin.right)
    if bleed_two_sided && swap: swap(bleed.left, bleed.right)
    → 用互换后的 margin 拼装最终 Frame
```

#### 4.5.3 源码精读

[LayoutedPage 结构定义（run.rs:25-43）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L25-L43)

文档注释直白点明：「A mostly finished layout for one page. Needs only knowledge of its exact page number to be finalized into a `Page`. (Because the margins can depend on the page number.)」

[finalize 中按物理页号互换左右（finalize.rs:32-41）](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L32-L41)

```rust
// If two sided, left becomes inside and right becomes outside.
let swap = binding.swap(counter.physical());
if margin_two_sided && swap {
    std::mem::swap(&mut margin.left, &mut margin.right);
}
if bleed_two_sided && swap {
    std::mem::swap(&mut bleed.left, &mut bleed.right);
}
```

finalize 的开头注释也写明：「inside/outside margins require knowledge of the physical page number, which is unknown during parallel layout」。这正是 run 不能自己做互换、必须把 `two_sided` 标记透传给 finalize 的根本原因。

由此可以回答学习目标里的问题——**为什么暂存两套（值 + 标记）**：

1. **值** `margin: Sides<Abs>` 在 run 阶段就能确定（只依赖样式，不依赖页号），所以提前算好。
2. **标记** `margin_two_sided: bool` 告诉 finalize「这套边距需不需要按 inside/outside 互换」。
3. **互换本身**（`swap(left, right)`）依赖物理页号，只能在 finalize 做。
4. 把两者一起存进 `LayoutedPage`，既避免了 finalize 重新解析样式，又把「依赖页号」的操作推迟到唯一拥有页号信息的阶段。

#### 4.5.4 代码实践

**实践目标**：追踪 `margin_two_sided` 从样式到 finalize 的完整流转。

**操作步骤**：

1. 在 run.rs 找到 `margin_two_sided = margin.two_sided.unwrap_or(false)`（[run.rs:123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L123)）。
2. 确认它被写入 `LayoutedPage.margin_two_sided`（[run.rs:240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L240)）。
3. 在 finalize.rs 找到 `if margin_two_sided && swap { swap(...) }`（[finalize.rs:36-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L36-L38)）。
4. 思考：用户写 `#set page(margin: (inside: 20mm, outside: 10mm))` 时，`Margin::from_value` 会把 `two_sided` 设为 `Some(true)`（因为出现了 inside/outside），于是 `margin_two_sided = true`，双数页（左装订）会互换。

**需要观察的现象**：`two_sided` 标记穿透 run → LayoutedPage → finalize，最终在 `binding.swap(physical)` 为真时触发左右互换。

**预期结果**：能画出 `two_sided` 字段的「生命周期」：样式 `Margin.two_sided` → run `LayoutedPage.margin_two_sided` → finalize 里的互换条件。详细的 `binding.swap` / `counter.physical()` 留待 u3-l4。

#### 4.5.5 小练习与答案

**练习 1**：如果直接在 run 阶段就做左右互换，会有什么问题？

**参考答案**：run 是**并行**执行的，各 run 互不感知全局页号；而「第几张物理页」要等所有 run 排完、按 `items` 顺序串行 finalize 累加 `ManualPageCounter` 才能确定。若在 run 里互换，就需要在并行阶段预先知道页号，破坏了并行独立性（这正是 u1-l4 强调的「realize 扁平化样式使各 run 自带上下文、彼此独立」的前提）。

**练习 2**：`bleed_two_sided` 和 `margin_two_sided` 是不是一定相等？

**参考答案**：不一定。它们分别来自 `PageElem::bleed.two_sided` 和 `PageElem::margin.two_sided`（[run.rs:123 与 131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L130-L131)），用户可以单独设置其中一个为双面。所以 finalize 里对 margin 和 bleed 的互换是**两个独立判断**（[finalize.rs:36-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L36-L41)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**源码阅读 + 手动推演**任务（不修改源码）。

**场景**：用户写了一个最小文档：

```typst
#set page(
  paper: "a4",
  margin: (top: 40pt, bottom: 30pt, inside: 25mm, outside: 20mm),
  header: [My Header],
  footer: align(right)[Page],
  numbering: "1",
  number-align: bottom,
  header-ascent: 50%,
)
#lorem(60)
```

**任务**：

1. **尺寸与边距**（模块 4.2）：写出 `size`、`min`、`default`、解析后的 `margin` 四边值，并判断 `margin_two_sided` 为 true 还是 false（提示：出现了 `inside/outside`）。
2. **页码归属**（模块 4.3）：根据 `number-align: bottom` 判断页码（`"1"`）进 header 还是 footer；再判断它与用户给的 `header: [My Header]` / `footer: [...]` 如何共存（谁覆盖谁？）。
3. **正文与 marginal**（模块 4.4）：写出 `area`（正文区域）、`header_size`（含 `header-ascent: 50%` 的缩减）、`footer_size`、`full_size` 的表达式。
4. **LayoutedPage 暂存**（模块 4.5）：说明 run 阶段这张页的 `LayoutedPage.margin_two_sided` 值，以及它在 finalize 阶段会怎样被 `binding.swap(physical)` 影响。

**参考答案要点**：

1. `size` 为 A4 尺寸（约 595.28 × 841.89 pt，未 flipped）；`min ≈ 595.28pt`；`default ≈ 70.86pt`（但用户显式给了所有边，所以 default 不生效）；`margin = {top:40pt, bottom:30pt, left(inside):25mm, right(outside):20mm}`；因含 inside/outside，`margin_two_sided = true`。
2. `number-align: bottom` → 页码进 footer。但用户**显式**给了 `footer`，根据「显式优先」规则（[run.rs:185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L185) 的 `footer.unwrap_or(&numbering_marginal)`），footer 用用户的 `[Page]`，**页码被忽略**。header 用用户的 `[My Header]`。
3. `area = size − margin.sum_by_axis`；`header_size = (inner.width, 40pt − 40pt×0.5) = (inner.width, 20pt)`；`footer_size = (inner.width, 30pt − 30pt×0.3) = (inner.width, 21pt)`（footer-descent 默认 30%）；`full_size = inner + margin + bleed`（bleed 默认 0）。
4. `margin_two_sided = true`；finalize 中 `swap = binding.swap(physical)`，左装订（LTR 默认 `Binding::Left`）下双数物理页会 `swap(margin.left, margin.right)`，使 inside/outout 正确朝向。

> 注：第 3 步中 footer-descent 取默认 30%（用户未设），如需精确值请以本地样式链解析为准。

## 6. 本讲小结

- `layout_page_run` 是 `layout_pages` 三段式中**可并行**的那一步，每个 `Item::Run` 一个并行任务，返回「半成品」`Vec<LayoutedPage>`。
- 页面级样式从 `PageElem` 各字段解析：`width/height` 为 `Auto` 时 fallback 到无穷（贴合内容），`flipped` 交换宽高，默认边距 `2.5/21 × min(width,height)`（A4 ≈ 25mm）。
- 页码是特殊的 marginal：`number_align.y == Top` 进 header，`Bottom`（默认）进 footer；但**用户显式的 header/footer 优先**于自动页码。
- `header_ascent` / `footer_descent`（默认各 30%）从顶部/底部边距中**减去**对应比例，缩小页眉页脚的可用排版高度。
- 正文用 `layout_flow(FlowMode::Root)` 排进 `Regions::repeat(area, ...)`，每帧 inner 对应一页；header/footer/background/foreground 用 `layout_marginal` 闭包（内部 `crate::layout_frame`）分别排版。
- `LayoutedPage` 把 margin/bleed 的「值 + two_sided 标记」一起暂存，把依赖物理页号的左右互换推迟到串行 finalize（u3-l4），从而保住并行独立性。

## 7. 下一步学习建议

- **u3-l4（页面最终化 finalize）**：紧接本讲，详细讲解 `binding.swap(counter.physical())` 如何决定左右互换、`ManualPageCounter` 的 `logical()`/`physical()` 区别，以及 marginal 按何种顺序压入整页 `Frame`（顺序影响内省与计数器解析）。
- **u4-l1（flow 布局总览）**：本讲把正文交给 `layout_flow(FlowMode::Root)` 后就停下了；下一层应深入 flow 的「每区域 compose 一次」主循环、`Work` 状态与 `Stop` 控制流。
- **u4-l6（列布局 columns）**：本讲传给 flow 的 `ColumnOptions { count, balanced, gutter }` 如何在 flow 内部把 `area` 切成多列、balanced 列如何反复测量平衡，是自然的后续。
- **复习 u2-l2**：若对 `Regions::repeat` 的 `last`/`expand`/`may_break` 语义仍有疑问，回头巩固 Regions 五要素。
