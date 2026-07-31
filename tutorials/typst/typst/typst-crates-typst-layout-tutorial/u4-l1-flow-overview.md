# flow 布局总览：layout_flow 主循环

## 1. 本讲目标

本讲是「流式（块级）布局」单元（u4）的入口。读完本讲，你应当能够：

1. 说清楚 `layout_flow` 的「每区域 compose 一次」主循环是怎么转起来的，`regions.next()` 在何时发生，循环又靠什么条件终止。
2. 画出 flow 排版的**三段式骨架**：`configuration`（求配置）→ `collect`（收集成 Child）→ 主循环里的 `compose`（每个区域排一帧）。
3. 区分 `FlowMode` 的三种取值（`Root` / `Block` / `Inline`）及其能力差异（谁能承载脚注与行号）。
4. 看懂贯穿区域之间的「待办工作」`Work`（children / spill / floats / footnotes / footnote_spill / tags / skips）是如何被消费与跨区域延续的。
5. 理解 `Stop` 这套三层控制流（`Finish(bool)` / `Relayout` / `Error`）分别由谁产生、由谁吞掉。

本讲只建立**总览与主循环**的认知，把 `collect` 的细节留给 u4-l2，`distribute` 留给 u4-l3，`compose` 的浮动体/脚注留给 u4-l4，`block` 留给 u4-l5，`columns` 留给 u4-l6。

## 2. 前置知识

本讲承接 u2-l2（Regions）与 u2-l3（Frame/Fragment），并用到 u1-l4（端到端流程）与 u2-l1（Engine/comemo）。如果你还记得下面几条，本讲会读得很顺：

- **Regions 是排版的「画布输入」**：`size` 是当前剩余尺寸、`backlog` 是后续候选高度队列、`last` 是可无限重复的末区高度；`next()` 推进队列，`may_break()` / `may_progress()` / `is_full()` 共同防止死循环（详见 u2-l2）。
- **Fragment 是排版的「输出」**：`Fragment` 就是一串 `Frame`；可断裂内容用 `layout_fragment` 拿到多帧，注定单帧用 `layout_frame`（详见 u2-l3）。
- **realize 是排版前的「翻译层」**：把任意 `Content` 展开成扁平的 `Vec<Pair>`（`Pair` = 已知元素 + `StyleChain`），不算几何、不画东西（详见 u1-l4）。
- **入口函数的 comemo 模式**：公开薄封装把 `Engine` 拆成 `Tracked`/`TrackedMut` 参数后，调用带 `#[comemo::memoize]` 的 `*_impl`（详见 u2-l1）。

本讲的主角文件是 [`src/flow/mod.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs)，它把四个兄弟文件串成一条流水线。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
|------|------|--------------|
| `src/flow/mod.rs` | flow 的入口、主循环、`Config`/`Work`/`Stop`/`FlowMode` 定义 | 全讲核心 |
| `src/flow/collect.rs` | 把 `Pair` 预处理成 `Child` 枚举 | 4.1 三段式的第二步 |
| `src/flow/compose.rs` | 排单个区域（含列、浮动体、脚注），吞掉 `Stop` | 4.1 / 4.4 |
| `src/flow/distribute.rs` | 贪心地把 child 填进区域，产生 `Stop::Finish` | 4.4 |
| `src/flow/block.rs` | 单块 / 可断裂多块排版 | 仅引用，详见 u4-l5 |
| `src/pages/run.rs` | 页面正文调用 `layout_flow(FlowMode::Root)` | 4.2 |

> 提示：`Work`、`Stop`、`FlowMode` 这些类型都定义在 `mod.rs` 里，但它们的**消费者**分散在 `compose.rs` / `distribute.rs`。理解 flow 必须把「定义」和「使用」两边对照看。

## 4. 核心概念与源码讲解

### 4.1 flow 的三段式骨架与主循环

#### 4.1.1 概念说明

`flow` 是 typst-layout 里**最通用**的排版原语：给定一份已 realized 的 `children: &[Pair]` 和一组 `Regions`，它产出 `Fragment`（一串 `Frame`）。几乎所有容器型 layouter（grid、stack、math、transforms……）最终都会回调 `layout_fragment` / `layout_frame`，而那两个入口的尽头就是 `layout_flow`。

可以把 `layout_flow` 想成一个「**每区域排一帧、排完即止**」的循环泵。它只做三件事：

1. **configuration**：根据共享样式、`Regions`、列选项、`FlowMode`，算出一份贯穿整个 flow 的 `Config`（列宽、脚注配置、行号配置）。
2. **collect**：把扁平的 `Pair` 预处理成更好操作的 `Child`（详见 u4-l2），并用 bump 分配器安置它们。
3. **主循环**：对每个区域调用一次 `compose`，拿到一帧；用 `work.done()` 判断是否还有活干，没有就停。

注意：`collect` 在进入循环**之前**只跑一次——它产出的 `Vec<Child>` 被存进 `Work`，之后所有区域共用同一份 child 列表，循环里只是不断「啃」它。

#### 4.1.2 核心流程

```text
layout_flow(children, regions, column, mode)
  │
  ├─① configuration(...)  → Config        // 一次性，全局共享
  │
  ├─② collect(...)        → Vec<Child>     // 一次性，bump 分配
  │     Work::new(&children) → work        // 把 child 切片装进 Work
  │
  └─③ loop {                                  // 每个区域转一圈
        compose(engine, &mut work, &config, locator, regions)  → Frame
        finished.push(frame)
        若 work.done() 且 (不沿 y 扩张 或 backlog 已空)  → break
        regions.next()                        // 推进到下一个候选高度
     }
     → Fragment::frames(finished)
```

三个关键不变量：

- `Config` 在整个 flow 内**不变**（除非列平衡触发的内部 relayout，见 u4-l6），所以列宽、脚注 gap 这些只算一次。
- `Work` 是**可变**的跨区域状态：每排完一个区域，`Work` 里还剩多少 child、有没有 `spill`（断裂块的残骸）、有没有排不下的 float/footnote，都如实保留，传给下一个区域。
- 主循环本身**极简**——所有复杂控制流都被 `compose` 封装在内部，主循环看到的永远只是一个「成功的 `Frame`」或「一个 `Err`」。

#### 4.1.3 源码精读

主循环就在 [src/flow/mod.rs:194-237](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L194-L237)：

```rust
pub fn layout_flow<'a>(
    engine: &mut Engine,
    children: &[Pair<'a>],
    locator: &mut SplitLocator<'a>,
    shared: StyleChain<'a>,
    mut regions: Regions,
    column: ColumnOptions,
    mode: FlowMode,
) -> SourceResult<Fragment> {
    // ① Prepare configuration that is shared across the whole flow.
    let config = configuration(shared, regions, column, mode);

    // ② Collect the elements into pre-processed children.
    let bump = Bump::new();
    let children = collect(
        engine, &bump, children, locator.next(&()),
        Size::new(config.columns.width, regions.full),
        regions.expand.x, mode,
    )?;

    let mut work = Work::new(&children);
    let mut finished = vec![];

    // ③ This loop runs once per region produced by the flow layout.
    loop {
        let frame = compose(engine, &mut work, &config, locator.next(&()), regions)?;
        finished.push(frame);

        // Terminate the loop when everything is processed, though draining the
        // backlog if necessary.
        if work.done() && (!regions.expand.y || regions.backlog.is_empty()) {
            break;
        }
        regions.next();
    }

    Ok(Fragment::frames(finished))
}
```

逐行解读：

- [L204 `configuration`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L240-L293)：算 `Config`。注意它把 `regions.full`（整区域高度）当作列布局的纵向基准，而不是 `regions.size.y`（可能已被上游削短）——这与 u2-l2 讲的 `base()` 用 `full` 而非 `size.y` 一脉相承。
- [L208 `Bump::new()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L208-L217)：bump 分配器，给 `Child` 里的大变体（`LineChild`/`SingleChild`/`MultiChild`/`PlacedChild`）装箱，压缩枚举体积。
- [L219 `Work::new(&children)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L322-L332)：把 child 切片存进 `Work`，其余字段（spill/floats/…）初始化为空。
- [L224 `compose(...)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L35-L55)：**核心**。它返回 `SourceResult<Frame>`——注意不是 `FlowResult`！所有 `Stop` 都在 compose 内部消化掉了，主循环拿到的永远是一帧。
- [L229 终止条件](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L229-L231)：`work.done() && (!regions.expand.y || regions.backlog.is_empty())`。这是本讲最值得品味的一行，详见下方「终止条件拆解」。
- [L233 `regions.next()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/regions.rs#L114-L127)：推进队列，从 `backlog` 取下一个高度（取不到就回落到 `last`），只改 `size.y` 和 `full`，**不改宽度**。

**终止条件拆解**（结合 u2-l2 的 Regions 语义）：

| `work.done()` | `regions.expand.y` | `backlog` 是否空 | 行为 |
|---|---|---|---|
| `false` | 任意 | 任意 | 继续：`regions.next()` 后再排一帧 |
| `true` | `false`（收缩贴合内容） | 任意 | **break**：内容排完即止 |
| `true` | `true`（填满指定高度） | 非空 | 继续：即便没内容，也要为 backlog 里剩余的「满高区域」产出空帧 |
| `true` | `true` | 空 | **break**：backlog 耗尽，避免对着 `last` 无限产帧 |

为什么 `expand.y == true` 时要「抽干 backlog」？因为扩张语义承诺「给每个请求的满高区域都产一帧」。如果内容提前排完但 backlog 还有 3 个区域没填，就必须继续转 3 圈、产 3 个（可能为空的）帧，才能对齐外层对区域数的预期。

而为什么不担心无限循环？因为终止条件里加了 `backlog.is_empty()`——一旦只剩可无限重复的 `last`，立刻停。这把「扩张到底」与「不会死循环」调和在一起。

**谁会调用 `layout_flow`？** 全 crate 只有两处：

- [src/flow/mod.rs:161](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L150-L169)：`layout_fragment_impl` 在 realize 之后调用，`mode` 由 `FragmentKind`（`Block`/`Inline`）转换而来。
- [src/pages/run.rs:190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L190-L202)：页面正文用 `FlowMode::Root` 调用，`Regions::repeat(area, area.map(Abs::is_finite))`——页码区是有限高度，所以 `expand.y` 为真、`backlog` 由 `repeat` 填充。

#### 4.1.4 代码实践

**实践目标**：亲眼看见主循环「每区域转一圈」的节奏，验证 `work.done()` 与 `regions.next()` 的配合。

**操作步骤**（源码阅读 + 可选日志）：

1. 打开 [src/flow/mod.rs:223-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L223-L234) 的主循环，确认它只调用 `compose` 一次、push 一帧、判 `done`、`next`。
2. **可选**：临时在 `finished.push(frame);` 之后插一行调试日志（示例代码，非项目原码）：

   ```rust
   // 示例代码：临时调试，用完请删除
   eprintln!(
       "[flow] region {} -> frame {}x{}, done={} expand_y={} backlog_len={}",
       finished.len(), frame.width().to_pt(), frame.height().to_pt(),
       work.done(), regions.expand.y, regions.backlog.len(),
   );
   ```

3. 若要观察输出：从仓库根目录用 typst CLI 编译一个会跨页的文档（例如一段很长的正文），让 `FlowMode::Root` 路径反复进入主循环。

**需要观察的现象**：

- 每行日志对应**一个区域（一页或一列）**，`finished.len()` 单调递增。
- 当内容跨页时，前面的区域 `done=false`，最后一帧 `done=true`。
- 若页面高度有限（`expand.y=true`），即使 `done=true`，只要 `backlog_len>0` 就还会再转几圈。

**预期结果**：日志行数 ≈ 实际页数（根流场景）；`done` 在最后一帧翻为 `true`。如果你没有可运行的 typst 构建，**待本地验证**，退而用下面的纯阅读实践：手动追踪 4.1.5 的例子。

#### 4.1.5 小练习与答案

**练习 1**：假设 `expand.y = false`、`last = None`、`backlog = [H1, H2]`，内容恰好需要 1.5 个区域。写出主循环每一圈的 `size.y`、`work.done()` 与是否 `break`。

**参考答案**：

- 第 1 圈：`size.y = H1`，compose 填满后 `done = false`，不 break，`regions.next()` → `size.y = H2`（backlog 现在剩 `[H2]`…严格说 `next` 取走 `H1` 后 backlog 指向 `H2`，`size.y` 变 `H1` 的下一个）。注意：初始 `size.y` 才是第 1 帧，`next()` 后变第 2 帧。
- 第 2 圈：`size.y = H2`，剩余内容排完 `done = true`，因 `expand.y = false` → `!expand.y` 为真 → break。最终 `Fragment` 含 2 帧。

**练习 2**：如果把终止条件改成只判 `work.done()`（去掉后半段），在 `expand.y = true` 且 backlog 很长时会发生什么？

**参考答案**：内容排完后会立刻 break，导致 backlog 里未被填满的区域拿不到帧——扩张语义被破坏，外层期望的「满高区域数」对不上实际帧数。所以后半段 `!expand.y || backlog.is_empty()` 是为「抽干 backlog」而存在的。

---

### 4.2 FlowMode：根流、块流、行内流

#### 4.2.1 概念说明

`FlowMode` 是 flow 的「**能力档位**」。同样是 `layout_flow`，根据 `mode` 不同，能承载的东西不一样：

- `Root`：根流。子元素是块级的，**额外**能承载脚注（footnote）和行号（line number）。页面正文走的就是这一档。
- `Block`：普通块流。子元素是块级的，但**不**支持脚注/行号（那些只在根流处理）。
- `Inline`：行内流。子元素是行内级的，collect 阶段直接走段落断行管线（`layout_inline`），把整段拍成若干行帧。

`mode` 的影响主要体现在两处：`configuration`（只有 `Root` 才生成 `line_numbers` 配置）和 `collect`（`Root`/`Block` 走 `run_block`，`Inline` 走 `run_inline`）。

#### 4.2.2 核心流程

`FlowMode` 来自 `FragmentKind` 的转换——realize 阶段会判断一段 content 最终是「块级」还是「行内级」：

```text
realize(content)  →  FragmentKind::Block | FragmentKind::Inline
                         │  From<FragmentKind>
                         ▼
                    FlowMode::Block | FlowMode::Inline
                         （页面正文直接用 FlowMode::Root）
```

三档对 collect 与 compose 的影响：

| `FlowMode` | collect 路径 | line_numbers 配置 | 脚注处理 |
|---|---|---|---|
| `Root` | `run_block` | 生成（`Some`） | 允许 |
| `Block` | `run_block` | 不生成（`None`） | 跳过（`config.mode != Root` 时直接返回） |
| `Inline` | `run_inline` | 不生成 | 跳过 |

#### 4.2.3 源码精读

枚举与转换在 [src/flow/mod.rs:172-191](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L172-L191)：

```rust
pub enum FlowMode {
    /// A root flow with block-level elements. Like `FlowMode::Block`, but can
    /// additionally host footnotes and line numbers.
    Root,
    /// A flow whose children are block-level elements.
    Block,
    /// A flow whose children are inline-level elements.
    Inline,
}

impl From<FragmentKind> for FlowMode {
    fn from(value: FragmentKind) -> Self {
        match value {
            FragmentKind::Inline => Self::Inline,
            FragmentKind::Block => Self::Block,
        }
    }
}
```

注意 `From<FragmentKind>` 只产出 `Inline` 或 `Block`——**`Root` 永远不会从 FragmentKind 得到**，它只能由页面正文 [pages/run.rs:201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L190-L202) 显式传入。这是合理的：只有「整页」才够格当根流。

`mode` 真正起作用的两处：

- [configuration L274](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L274-L291)：`(mode == FlowMode::Root).then(|| LineNumberConfig { .. })`——只有根流才有行号配置。
- [collect.rs run() L68-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs#L68-L73)：`Root`/`Block` 走 `run_block`，`Inline` 走 `run_inline`（后者直接调 `crate::inline::layout_inline` 把段落拍成行）。
- [compose.rs footnotes() L398-400](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L398-L400)：`if self.config.mode != FlowMode::Root { return Ok(()); }`——非根流直接拒绝处理脚注。

#### 4.2.4 代码实践

**实践目标**：确认 `Root` 的「特权」由两处代码共同界定。

**操作步骤**：

1. 在 `src/flow/mod.rs` 搜 `FlowMode::Root`，数一共有几处（提示：`configuration` 里一处）。
2. 在 `src/flow/compose.rs` 搜 `FlowMode::Root`，找到脚注提前返回的那行。
3. 解释：为什么一个 `#block(width: 100%, footnotes ...)` 容器里的脚注不会在该 block 内部就近排版？

**需要观察的现象 / 预期结果**：脚注在 compose 层被「非根流直接 return」挡掉，因此容器内 block 的脚注不会被消化，而是随内容流回到根流，最终在页面底部统一排版。这正是「脚注只在根流处理」的体现。

**待本地验证**：可用 Typst 写一个含 `#footnote[...]` 的 `#block` 测试用例，观察脚注出现在页面底部而非 block 内部。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `From<FragmentKind> for FlowMode` 不实现 `Root` 这一分支？

**参考答案**：因为 `Root` 表达的是「我是整页正文」这一拓扑身份，而非某段 content 的块/行内性质。realize 只能判断内容是块级还是行内级（`Block`/`Inline`），无法断言「这是根」，所以 `Root` 必须由页面层显式传入。

**练习 2**：`FlowMode::Inline` 的 flow 在 collect 阶段做了什么和 `Block` 不同的事？

**参考答案**：`Inline` 走 `run_inline`，它**在收集阶段就直接调用 `crate::inline::layout_inline` 把整段拍成行帧**（因为断行不依赖具体区域高度），再把行帧包装成 `Child::Line`；而 `Block` 走 `run_block`，按元素类型（`TagElem`/`VElem`/`ParElem`/`BlockElem`/`PlaceElem` 等）逐个映射成对应 `Child` 变体。

---

### 4.3 Work：跨区域的待办工作状态

#### 4.3.1 概念说明

主循环之所以能「每区域转一圈、最终把内容排光」，靠的是 `Work` 这个**跨区域的可变工作状态**。它就像一份「待办清单 + 几个暂存抽屉」：每排完一个区域，还没处理的内容、断裂块的残骸、排不下的浮动体/脚注，都留在 `Work` 里原样传给下一个区域。

`Work` 的设计要点：

- 它是**按引用持有** child 切片的（`children: &'b [Child<'a>]`），所以跨区域传递是 O(1) 的——只是挪切片头指针（`advance` 把 `&self.children[1..]` 赋回去）。
- `Clone` 是为了支持 compose 内部的「检查点 / 回滚」机制（处理浮动体失败时复原，见 4.4）。
- `done()` 判断「是否还有实质内容要排」——但**故意不算 `tags` 和 `skips`**。

#### 4.3.2 核心流程

`Work` 的七个字段，按用途分组：

```text
Work {
  children:     &[Child]      ← 主待办：未处理的 child 切片，随 advance() 不断缩短
  spill:        Option<MultiSpill>  ← 断裂块的「残骸」，跨区域续排
  floats:       EcoVec<&PlacedChild> ← 上一区域没塞下的浮动体，排队等下个区域
  footnotes:    EcoVec<Packed<FootnoteElem>> ← 待处理的脚注
  footnote_spill: Option<IntoIter<Frame>> ← 一条脚注跨区域的剩余帧
  tags:         EcoVec<&Tag>   ← 待挂到下一帧的内省标签（不参与 done 判定）
  skips:        Rc<FxHashSet<Location>> ← 已处理过的 float/footnote，避免重复排
}
```

`done()` 的判定（[mod.rs:345-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L345-L351)）：

```text
done = children 空 ∧ spill 空 ∧ floats 空 ∧ footnote_spill 空 ∧ footnotes 空
       （注意：不查 tags，也不查 skips）
```

为什么不算 `tags`？因为标签是**被动**的——它不占布局空间，只是「随大流」挂到当前/最后一帧上。当其它实质内容都排完、只剩尾巴标签时，flow 视为「已完成」，这些标签会在 `distribute` 的 `forced` 分支里 `flush_tags()` 进最后一帧。若把 tags 算进 `done()`，就会因为永远有尾巴标签而无法终止。

`skips` 用 `Rc<FxHashSet>` 是因为 `Work` 需要 `Clone`（检查点），而 `Rc` 让克隆廉价；真要改时用 `Rc::make_mut`（[extend_skips L355-359](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L355-L359)）写时复制。

#### 4.3.3 源码精读

`Work` 结构与构造在 [src/flow/mod.rs:300-332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L300-L332)：

```rust
struct Work<'a, 'b> {
    /// Children that we haven't processed yet. This slice shrinks over time.
    children: &'b [Child<'a>],
    /// Leftovers from a breakable block.
    spill: Option<MultiSpill<'a, 'b>>,
    /// Queued floats that didn't fit in previous regions.
    floats: EcoVec<&'b PlacedChild<'a>>,
    /// Queued footnotes that didn't fit in previous regions.
    footnotes: EcoVec<Packed<FootnoteElem>>,
    /// Spilled frames of a footnote that didn't fully fit. Similar to `spill`.
    footnote_spill: Option<std::vec::IntoIter<Frame>>,
    /// Queued tags that will be attached to the next frame.
    tags: EcoVec<&'a Tag>,
    /// Identifies floats and footnotes that can be skipped if visited ...
    skips: Rc<FxHashSet<Location>>,
}
```

两个生命周期（见 [L296-299 注释](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L296-L299)）：`'a` 是 realize 产出 content 的生命周期，`'b` 是 collect 产出 child 的生命周期。`spill`/`floats` 同时绑定这两个生命周期，因为断裂块既引用原始 block 又引用收集后的 `MultiChild`。

消费 `Work` 的主入口是 distribute 的 [`run()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L119-L133)：

```rust
fn run(&mut self) -> FlowResult<()> {
    // First, handle spill of a breakable block.
    if let Some(spill) = self.composer.work.spill.take() {
        self.multi_spill(spill)?;
    }
    // ... process children until no space is left or no children are left.
    while let Some(child) = self.composer.work.head() {
        self.child(child)?;
        self.composer.work.advance();
    }
    Ok(())
}
```

它先取 `spill`（上一区域断裂块的残骸，优先续排），再用 `head()` / `advance()` 逐个啃 `children`——这正是 [Work::head](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L335-L342)（取切片头）与 [Work::advance](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L339-L342)（切片后移一位）的用武之地。

**`spill` 跨区域续排**的一个典型场景：一个可断裂 block（`MultiChild`）在当前区域排不下，[`multi()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L354-L392) 排出第一帧后把剩余部分包成 `MultiSpill` 存进 `work.spill`，并 `return Err(Stop::Finish(false))` 结束本区域；下一个区域 `run()` 开头 `take()` 出这个 spill，调 `multi_spill` 续排，直到该 block 全部排完。这就是「一个 block 跨多个区域断裂」的底层机制（详见 u4-l5）。

#### 4.3.4 代码实践

**实践目标**：追踪 `spill` 如何让一个可断裂块跨越两个区域。

**操作步骤**：

1. 读 [distribute.rs multi() L354-392](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L354-L392)，找到「`spill` 非空 → 存进 `work.spill` → `advance()` → `Finish(false)`」这段。
2. 读 [distribute.rs run() L120-123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L120-L123)，确认下一区域开头会先 `take()` spill。
3. 读 [distribute.rs multi_spill() L395-423](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L395-L423)，看它如何把残骸再排一帧、若仍有剩余则继续存回 `work.spill`。

**需要观察的现象**：`spill` 在区域之间形成一条「接力链」——每个区域消费它一次、若没排完就再扔回去，直到 `MultiSpill::layout` 返回 `spill = None`。

**预期结果**：一个需要 2.5 个区域的可断裂 block，会让主循环多转几圈，且前几圈 `work.spill` 非空、`done()` 为 `false`；最后一圈 `spill` 变 `None`、`done()` 变 `true`。

#### 4.3.5 小练习与答案

**练习 1**：`Work::done()` 为什么不检查 `tags` 字段？

**参考答案**：标签是被动元素，不占布局空间，只是随内容流挂到帧上用于内省。若把它算进 `done()`，则只要还有尾巴标签，flow 就永远「没完成」、无法终止。实际处理是：当实质内容排完（`forced=true`）时，`distribute` 的 `finalize` 会 `flush_tags()` 把剩余标签塞进最后一帧。

**练习 2**：`skips` 为什么用 `Rc<FxHashSet>` 而不是 `FxHashSet`？

**参考答案**：`Work` 需要实现 `Clone`（供 compose 的检查点/回滚用，4.4 会讲）。直接克隆 `FxHashSet` 是 O(n) 的；用 `Rc` 让克隆变成 O(1) 的引用计数加一，真正需要修改时再 `Rc::make_mut` 做写时复制（见 `extend_skips`）。

**练习 3**：`floats` 和 `footnotes` 都是 `EcoVec`，它们在什么情况下会非空地从一个区域传到下一个？

**参考答案**：当某个浮动体/脚注在当前区域「装不下」且 `regions.may_progress()` 为真（换区域有望改善）时，会被 push 进对应队列（见 compose 的 [`float`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L346-L349) / [`footnote`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L535-L541)），随 `Work` 进入下一个区域重试。

---

### 4.4 Stop：三层控制流（Finish / Relayout / Error）

#### 4.4.1 概念说明

排版过程中有大量「非局部跳转」：当前区域装不下了要换区域、塞进一个浮动体后可用空间变小要重排这一区域、遇到致命错误要整体失败。Rust 里用 `?` 和 `Result` 表达这类控制流最自然，于是 flow 定义了 `FlowResult<T> = Result<T, Stop>` 和 `Stop` 枚举。

`Stop` 的三层语义：

- `Stop::Finish(bool)`：**本区域该结束了**。`bool` 是「forced」标志——`true` 表示遇到了显式 `#colbreak()` 强制断列，`false` 表示空间不够被动断开。**只由 distributor 产生，也只由 distributor 自己消费**。
- `Stop::Relayout(PlacementScope)`：**本区域要重排**。塞进一个 float/footnote 后可用空间收缩，需要带着新空间重来一遍。分两个作用域：`Column`（列内重排）和 `Parent`（整页/整容器重排）。**由 compose 产生，由 `column()`/`page()` 的检查点循环消费**。
- `Stop::Error(EcoVec<SourceDiagnostic>)`：**致命错误**，一路冒泡成 `SourceResult` 的 `Err`。

最关键的理解：**主循环永远看不到 `Stop`**。因为 `compose` 的返回类型是 `SourceResult<Frame>`（不是 `FlowResult`），所有 `Stop` 都在 compose 内部被处理干净——要么被 distribute 吞掉（`Finish`），要么被 `column`/`page` 的重排循环吞掉（`Relayout`），要么转成 `Err` 冒泡（`Error`）。这就是主循环能保持极简的根本原因。

#### 4.4.2 核心流程

`Stop` 的产生点与消费点对照：

```text
                      产生者                          消费者
Stop::Finish(b)   ← distribute (空间满/colbreak)   → distribute::run 的 match → finalize(forced)
Stop::Relayout    ← compose   (塞入 float/footnote)→ column() / page() 的检查点循环（restore 后重试）
Stop::Error       ← 任意排版错误                    → 一路 ? 冒泡到 layout_flow 的 SourceResult
```

`Relayout` 的检查点机制（compose 内部）：

```text
page():
  checkpoint = work.clone()          // 进入前存档
  loop {
    pod.size.y -= page_insertions.height()   // 按当前插入物缩可用空间
    match page_contents(...) {
        Ok(frame) => break frame
        Err(Relayout(Parent)) => *work = checkpoint.clone()   // 回滚，带着更小的空间重来
        Err(Error(e)) => return Err(e)
        Err(Finish(_) | Relayout(Column)) => unreachable!()   // 这层不该收到
    }
  }
```

为什么回滚？因为「塞入一个浮动体」是在排版**途中**发生的副作用——它已经改了 `work`（比如把 float 标记进 `skips`、缩小了 region）。若不回滚到检查点直接重排，就会重复计入。所以先 `clone` 一份 `work`，重排前恢复。

#### 4.4.3 源码精读

`Stop` 与 `FlowResult` 在 [src/flow/mod.rs:434-455](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L434-L455)：

```rust
type FlowResult<T> = Result<T, Stop>;

enum Stop {
    /// Indicates that the current subregion should be finished. Can be caused
    /// by a lack of space (`false`) or an explicit column break (`true`).
    Finish(bool),
    /// Indicates that the given scope should be relayouted.
    Relayout(PlacementScope),
    /// A fatal error.
    Error(EcoVec<SourceDiagnostic>),
}
```

**`Finish` 的产生者**（全部在 distribute.rs）：

- [line() L306-308](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L303-L308)：某行排不下且换区域有望改善 → `Finish(false)`。
- [single() L346-348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L344-L348)：不可断裂块排不下 → `Finish(false)`。
- [multi() L385-389](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L383-L389)：可断裂块第一帧排完、剩余存进 spill → `Finish(false)`。
- [break_() L525-534](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L525-L534)：显式 `#colbreak()` 且确有下一个区域可断 → `Finish(true)`。**这是唯一产生 `true` 的地方**。

`Finish` 的消费者是 [distribute() L30-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L30-L37)：

```rust
let forced = match distributor.run() {
    Ok(()) => distributor.composer.work.done(),
    Err(Stop::Finish(forced)) => forced,
    Err(err) => return Err(err),
};
```

`Ok(())` 表示 child 全部塞下了（`forced = work.done()`），`Err(Finish(forced))` 表示中途被迫断开。两种情况都进入 `finalize(region, init, forced)`，`forced` 决定是否 `flush_tags()`（[distribute.rs L545-547](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L545-L547)）。

**`Relayout` 的产生者**（compose.rs）：

- [float() L377](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L366-L377)：成功塞入一个浮动体 → `Err(Stop::Relayout(placed.scope))`。
- [footnote() L579](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L578-L580)：成功排了一条脚注 → `Err(Stop::Relayout(PlacementScope::Column))`。

`Relayout` 的消费者分两层：

- [column() L202-224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L202-L224)：消费 `Relayout(Column)`（回滚 checkpoint 重来）；`Relayout(Parent)` 与 `Error` 经 `err => return err` 上抛。
- [page() L88-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L84-L107)：消费 `Relayout(Parent)`（回滚 checkpoint 重来）；`Finish` 与 `Relayout(Column)` 标 `unreachable!()`，`Error` 上抛。

注意两层都把不该收到的 `Stop` 标成 `unreachable!()`——这是一种**不变量自检**：`Finish` 永远在 distribute 内部解决（不会冒到 column/page），`Relayout(Column)` 不会冒到 page（已被 column 截住），`Relayout(Parent)` 会被 column 透传到 page。这正好印证了「`Stop` 的产生与消费严格分层」。

#### 4.4.4 代码实践

**实践目标**：验证「主循环看不到 Stop」这一论断，并理清 `Finish(true)` 与 `Finish(false)` 的唯一差别。

**操作步骤**：

1. 看 [compose() L35-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L35-L55)，确认它返回 `SourceResult<Frame>`，内部把所有 `Stop` 吞掉。
2. 对照 [page() L96-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L88-L103) 与 [column() L218-219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L216-L223) 的 `unreachable!()`，回答：为什么 page 不可能收到 `Finish`？
3. 在 distribute.rs 里全局搜 `Stop::Finish`，确认 `Finish(true)` 只在 `break_` 出现，其余全是 `Finish(false)`。

**需要观察的现象**：

- `compose` 的签名是「干净的」`SourceResult<Frame>`，脏活全在内部。
- `Finish(true)` 唯一来源是显式 `#colbreak()`；其余所有「断开」都是 `Finish(false)`（空间不足）。

**预期结果**：能用自己的话讲清「distribute 产 `Finish` 自己吞、compose 产 `Relayout` 自己吞、`Error` 才外泄」这条分层规则。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `page()` 和 `column()` 都把 `Stop::Finish(_)` 标成 `unreachable!()`？

**参考答案**：因为 `Finish` 是「本区域该结束了」的信号，而它**唯一**的合法消费者是 `distribute::run` 内部的 `match`——distributor 自己产生、自己吞掉，并把 `forced` 标志传给 `finalize`。column/page 调用的是返回 `FlowResult<(Frame, Abs)>` 的 `column_contents`，而 `column_contents` 调用的 `distribute` 已经把 `Finish` 解决掉了，所以 `Finish` 永远不会冒到 column/page 层。

**练习 2**：`Stop::Relayout(PlacementScope::Column)` 为什么不可能冒到 `page()`？

**参考答案**：因为它会被 `column()` 的 match 截住——`column()` 对 `Relayout(Column)` 做回滚重试，只有 `Relayout(Parent)` 和 `Error` 才走 `err => return err` 上抛。所以 page 收到的 `Relayout` 必然是 `Parent`，于是 page 对 `Relayout(Column)` 标 `unreachable!()` 是安全的。

**练习 3**：`forced`（`Finish` 的 bool）最终影响了什么？

**参考答案**：它流入 [`distribute::finalize`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L544-L558)，决定三选一：`forced` 为真则 `flush_tags()`（把尾巴标签挂上，因为是显式断列、本区域就此定稿）；否则若本区域全是「可迁移项」（纯标签/零尺寸帧）则 `restore(init)` 回到初始（把这些被动项让给下一区域）；否则处理 sticky 检查点回滚。

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「**全链路追踪**」：

**任务**：给定一段 Typst 文档——两段较长的正文，中间夹一个 `#colbreak()`，再夹一个 `place(float: true, top)[图]`——手动模拟 `layout_flow` 的执行。

**要求画出**：

1. **configuration** 产出的 `Config` 关键字段（假设 `FlowMode::Root`、单列、有行号配置）。
2. **collect** 把内容映射成的 `Child` 序列（粗略：`Line…/Placed(浮动)/Break(true)/Line…`）。
3. **主循环**每一圈的：`compose` 返回的 frame 高度、`work.done()` 真假、是否 `regions.next()`、是否 break。
4. 标注 `Stop` 在哪一圈、由谁产生（`Finish(false)` / `Finish(true)` / `Relayout`）、由谁消费。

**参考追踪要点**（供你对照自己的答案）：

- 第一个区域：distribute 啃掉前若干行 → 遇到浮动体 → compose `float()` 成功塞入 → `Relayout(Column)` → `column()` 回滚 checkpoint、缩小可用空间重排 → 直到塞不下更多行 → `Finish(false)` → 本区域定稿。`done=false`，`regions.next()`。
- 中间某区域：遇到 `#colbreak()` 对应的 `Child::Break(weak=false)` → `break_()` 返回 `Finish(true)`（若有后续区域）→ 本区域定稿。
- 浮动体若在某区域装不下且 `may_progress()` → 进 `work.floats` → 随 `Work` 进下一区域重试。
- 最后一圈：所有 child 啃完、`spill/floats/footnotes` 皆空 → `done=true` → 因 `expand.y` 与 backlog 状态决定是否立即 break。

> 这个练习同时用到了 4.1（主循环）、4.2（Root 特权）、4.3（Work 的 spill/floats 接力）、4.4（Stop 的产生与消费）。完成它，你就掌握了 flow 的运行时心智模型。

## 6. 本讲小结

- `layout_flow` 是 typst-layout 最通用的排版原语，采用**三段式骨架**：`configuration`（一次性求 `Config`）→ `collect`（一次性把 `Pair` 收成 `Child`）→ 主循环（每个区域 `compose` 一次）。
- **主循环**靠 `work.done() && (!regions.expand.y || regions.backlog.is_empty())` 终止：内容排完即停，但 `expand.y` 时要「抽干 backlog」为每个满高区域产帧；`regions.next()` 在不终止时推进队列。
- **`FlowMode`** 是能力档位：`Root`（页面正文，额外支持脚注/行号）、`Block`（普通块流）、`Inline`（行内，collect 阶段直接断行）；`Root` 只能由页面层显式传入。
- **`Work`** 是跨区域可变状态：`children` 切片随 `advance()` 缩短，`spill`/`floats`/`footnotes`/`footnote_spill` 让排不下的内容接力到下一区域；`done()` 故意不算 `tags`/`skips`。
- **`Stop`** 是三层控制流：`Finish(bool)`（distribute 产、distribute 吞）、`Relayout(scope)`（compose 产、column/page 检查点吞）、`Error`（外泄）；**主循环永远看不到 Stop**，因为 `compose` 把它全封装了。
- 两个调用点：`layout_fragment_impl`（`Block`/`Inline`）与页面正文 `pages/run.rs`（`Root`）。

## 7. 下一步学习建议

本讲只搭了 flow 的「骨架与主循环」，接下来按依赖顺序深入：

- **u4-l2 flow 收集**：打开 `collect.rs`，看 `Pair` 如何变成 `Child` 的各变体（`LineChild`/`SingleChild`/`MultiChild`/`PlacedChild`），理解 `par_situation` 与 bump 分配器。
- **u4-l3 flow 分发**：深入 `distribute.rs`，看贪心分发、sticky 元素迁移、列平衡测量，以及所有 `Stop::Finish` 的产生细节。
- **u4-l4 flow 组合**：深入 `compose.rs` 的 `Composer`，搞清浮动体/脚注如何触发 `Relayout` 重排、检查点如何回滚。
- **u4-l5 块布局**：读 `block.rs` 的 `layout_single_block` / `layout_multi_block` 与 `MultiSpill`，理解 `spill` 接力的完整实现。
- **u4-l6 列布局**：回到 `configuration` 与 `compose` 的列循环，理解 `balanced` 列如何反复测量（`balancing_target`）使各列等高。

建议阅读顺序：u4-l2 → u4-l3 → u4-l4 → u4-l5 → u4-l6，每篇都回到本讲的主循环图里对号入座。
