# flow 组合 compose：浮动体与脚注

## 1. 本讲目标

本讲深入 flow 三段式（configuration → collect → 主循环每区域一次 compose）的最后一段内部：`compose`。学完本讲你应该能够：

- 说清 `compose` 在 flow 布局中的位置与职责——它负责「一个区域（page/region）」的全部组装，重点是 out-of-flow（脱离正文流）内容：浮动体（floats）与脚注（footnotes）。
- 理解「每加入一个 float / footnote 就缩小可用区域并触发整片重排」的机制，以及 `Stop::Relayout` 这条控制流如何在 page/column 两层循环里被拦截。
- 区分 page 级插入（`page_insertions`，影响所有列）与 column 级插入（`column_insertions`，只影响当前列）。
- 理解 `footnote_spill` / `footnote_queue` 为什么必须挂在 `Composer` 上而非 `Work` 上，以及它们如何跨越区域把一条脚注「接力」排完。

本讲是最小模块 **flow/compose、Composer、Insertions** 的精读，承接 u4-l3（distribute）。

## 2. 前置知识

- **flow 三段式骨架**（u4-l1）：`layout_flow` 先 `configuration` 求一次 `Config`，再 `collect` 把 `Pair` 收成 `Child`，最后主循环每个区域调用一次 `compose` 产一帧。
- **Regions / Region**（u2-l2）：排版的「画布」。`regions.size.y` 是当前剩余高度，`regions.base()` 返回 `(size.x, full)`，`regions.may_progress()` 判断换区域能否改善空间。
- **Work**（u4-l1）：跨区域可变状态，含 `children / spill / floats / footnotes / footnote_spill / tags / skips`，靠 `clone()` 做检查点。
- **Stop 控制流**（u4-l1、u4-l3）：`Stop::Finish(bool)`（distribute 产）、`Stop::Relayout(PlacementScope)`（compose 产，由 page/column 检查点吞掉）、`Stop::Error`（外泄）。关键不变量：`layout_flow` 主循环永远看不到 `Stop`。
- **PlacementScope**（u4-l2）：`PlacedChild` 的作用域只有 `Column`（列级）与 `Parent`（页面级）两变体，且 parent 作用域只允许浮动体。

如果对 distribute 如何「逐 child 塞入当前区域」尚不熟悉，建议先回顾 u4-l3，因为本讲的 `float` / `footnotes` 正是 distribute 在排到 `Child::Placed` 或某个 frame 时**回调** `Composer` 的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/flow/compose.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs) | 本讲主角。定义 `compose`、`Composer`、`Insertions`，处理浮动体/脚注的排版与拼装，外加行号（line numbers）。 |
| [src/flow/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs) | `layout_flow` 主循环（调用 `compose`）、`Work` 状态、`Stop`/`FlowMode`/`Config` 定义。 |
| [src/flow/distribute.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs) | 贪心分发；排到 `Child::Placed(float)` 或任意 frame 时回调 `Composer::float` / `Composer::footnotes`。 |
| [src/flow/collect.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/collect.rs) | 把 `PlaceElem` 收成 `PlacedChild`（含 `scope`/`float`/`clearance`/`delta`），并校验「parent 作用域必须 float」。 |

---

## 4. 核心概念与源码讲解

### 4.1 compose 的位置与职责：编排一个区域的全部内容

#### 4.1.1 概念说明

在 u4-l1 我们讲过 `layout_flow` 的主循环每个区域调用一次 `compose`。`compose` 拿到一个 `Regions`（一个 page/region 的可用空间），要产出**一整帧**。这一帧里既有「顺着流的正文」（由 distribute 负责），也可能有「脱离流的浮动体和脚注」（由 `Composer` 自己负责）。

模块顶部的注释把职责说得很清楚：Composer 主要关心 out-of-flow 插入，它通过 page/column 两层循环来处理；每加入一个浮动体就要重跑（因为浮动体吃掉了 distribute 可用的区域）；至于各子区域里顺着流的内容，则交给 [distribute()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L25-L34)。

#### 4.1.2 核心流程

```
layout_flow 主循环（每个区域一次）
        │
        ▼
   compose(engine, work, config, locator, regions)
        │  构造一个 Composer 并调用 .page()
        ▼
   Composer::page()        ── 处理 page 级插入（parent 浮动体），可重排
        │  pod.size.y -= page_insertions.height()
        ▼
   page_contents()         ── 单列：直接 column()；多列：逐列 column() 并横向拼接
        │
        ▼
   column()                ── 处理 column 级插入（column 浮动体 + 脚注），可重排
        │  pod.size.y -= column_insertions.height()
        ▼
   column_contents()       ── 先排 pending 脚注/浮动体，再调 distribute 排正文
        │
        ▼
   distribute(...)         ── 贪心塞 child；遇 float 回调 Composer::float，
                              遇任意 frame 回调 Composer::footnotes
```

关键点：**两层循环各自带一个检查点（checkpoint）**。重排不是从头再来，而是「把 Work 恢复到本层入口的状态，扣掉已加入的插入物高度，再跑一遍」。

#### 4.1.3 源码精读

`compose` 只是把 `Composer` 构造出来再调用 `.page()`，是个纯入口：

[src/flow/compose.rs:35-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L35-L55) —— 构造 `Composer`，初始化两套 `Insertions`（page/column）、`page_base`（记下原始 base 供 parent 浮动体使用），然后交给 `.page(locator, regions)`。

`layout_flow` 主循环里，`compose` 的调用点在：

[src/flow/mod.rs:222-234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L222-L234) —— 每轮 `compose` 产一帧推入 `finished`，未完成则 `regions.next()` 推进高度队列。注意主循环本身**不感知** `compose` 内部的重排：重排完全在 `compose` 内部消化，对外只返回最终那一帧。

#### 4.1.4 代码实践

**实践目标**：在脑中把 `compose` 嵌回 flow 主循环，确认「对外一帧、对内可能重排多次」。

**操作步骤**：

1. 打开 [src/flow/mod.rs:224](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L224)，在 `compose(...)` 调用前临时加一行 `eprintln!("[flow] region size before compose = {:?}", regions.size);`。
2. 打开 [src/flow/compose.rs:88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L88)，在 `page()` 的 `loop` 体开头加 `eprintln!("[compose] page loop, page_insertions.height = {:?}", self.page_insertions.height());`。
3. 运行 `cargo test -p typst-layout`（或任意一个触发排版的测试）。

**需要观察的现象**：对**同一个** flow 区域，`[flow]` 只打印一次（主循环每区域调一次），但 `[compose]` 可能打印多次——每次重排 `page_insertions.height()` 会递增（因为又塞进了一个浮动体）。

**预期结果**：一个含浮动体的文档，`[compose]` 的打印次数 > `[flow]`。这直观证明「主循环一帧 = compose 内部零或多次重排」。

> 实验完成后请记得撤销这两行 `eprintln!`，不要修改源码留在仓库里。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `compose` 必须返回单个 `Frame`（`SourceResult<Frame>`），而 `distribute` 返回 `(Frame, Abs)` 元组？

**参考答案**：`compose` 对外代表「一个区域/页的完整产物」，调用方（`layout_flow`）只关心一帧。而 `distribute` 额外返回「正文实际用到的高度」`used_height`，这是给**列平衡（column balancing）**测量用的——`page_contents` 要据此算平均列高（见 4.2 的平衡逻辑）。`compose` 把这个细节吞在内部，对外只给成品帧。

---

### 4.2 Composer 的状态与 page/column 两层循环

#### 4.2.1 概念说明

`Composer` 是 `compose` 的工作状态。它有两组「插入物容器」：`page_insertions`（页面级，跨所有列共享）和 `column_insertions`（列级，每列重置）。两层循环分别用检查点+`Stop::Relayout` 实现重排。

需要特别区分两套「脚注续排」字段：`Work` 上的 `footnotes` / `footnote_spill` 是**跨区域**的接力状态；而 `Composer` 自己的 `footnote_queue` / `footnote_spill` 是**区域内**、必须扛过重排的暂存。4.4 会详述。

#### 4.2.2 核心流程

**page() 循环**（处理 parent 作用域浮动体）：

```
checkpoint = work.clone()
loop {
    pod = regions
    pod.size.y -= page_insertions.height()      // 扣掉已加入的页面级插入
    match page_contents(locator.relayout(), pod) {
        Ok(frame) => break frame
        Err(Relayout(Parent)) => *work = checkpoint.clone()   // 回滚，重跑
        Err(Relayout(Column)) => unreachable!()               // 列级不会冒泡到这里
        Err(Finish(_))       => unreachable!()                // distribute 已在内部消化
        Err(Error(e))        => return Err(e)
    }
}
output = page_insertions.finalize(work, config, output, None)
```

**column() 循环**（处理 column 作用域浮动体 + 脚注）结构几乎一样，但拦截的是 `Relayout(Column)`，并把 `Relayout(Parent)` 放行给上层 `page()`。

#### 4.2.3 源码精读

`Composer` 结构体：

[src/flow/compose.rs:65-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L65-L80) —— 注意 `page_base` 记录原始 base（parent 浮动体用它当画布）；`column` 是当前列号（0-based）；`column_balancing_height` 是列平衡目标；`footnote_spill`/`footnote_queue` 上方的注释（[L74-L77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L74-L77)）点明：它们挂在 Composer 上是为了**扛过重排**，否则恢复 Work 检查点时会丢掉脚注。

`page()` 的检查点循环：

[src/flow/compose.rs:84-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L84-L107) —— `let checkpoint = self.work.clone()`（[L87](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L87)）；循环里先 `pod.size.y -= self.page_insertions.height()`（[L92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L92)）再排版；遇 `Relayout(Parent)` 就 `*self.work = checkpoint.clone()`（[L99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L99)）回滚后重跑。注意 `page_insertions` **不**回滚——它只会单调增长，这正是重排后区域变小的原因。

`page_contents()` 多列拼接（单列时直接走 `column()`）：

[src/flow/compose.rs:110-183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L110-L183) —— 它把 page 的 `column_height` 切成 N 个等高子区域（[L117-L130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L117-L130)），逐列调 `column()` 并按 `columns.dir`（LTR/RTL）横向摆放（[L144-L171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L144-L171)）。**关键**：因为 `column_height = regions.size.y`，而 `regions.size.y` 已经在 `page()` 里被 `page_insertions.height()` 扣过，所以**一个 page 级浮动体会等量压缩所有列的高度**——这是本讲综合实践要追踪的核心现象。

末尾的列平衡（[L173-L180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L173-L180)）：若 `balanced` 且内容已排完，用「各列正文实际用高之和 / 列数」得到目标高度，若比上次记录的更大就存下并 `Err(Stop::Relayout(Parent))` 触发整页重排，直到收敛（每次重排各列被限制在更紧的高度下，用高下降，最终稳定）。

`column()` 与 `column_contents()`：

[src/flow/compose.rs:190-253](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L190-L253) —— `column()` 入口先把 `column_insertions` 重置（[L192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L192)），处理从上一列/页接力来的 `work.footnote_spill`（[L195-L197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L195-L197)），再进检查点循环。循环里 `pod.size.y -= self.column_insertions.height()`（[L206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L206)），并计算列平衡用的 `balancing_target`（[L208-L214](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L208-L214)：只算浮动体高度，不算脚注）。列循环拦 `Relayout(Column)`，放行其它。出口处把 `footnote_queue` / `footnote_spill` 提交给 `Work`（[L227-L230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L227-L230)），再用 `column_insertions.finalize(...)` 把插入物拼到正文帧上，最后叠上行号。

[src/flow/compose.rs:263-279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L263-L279) —— `column_contents()` 先排 `work.footnotes`（pending 脚注）和 `work.floats`（pending 浮动体），注意这两处都传 `migratable: false`（理由见注释 [L256-L262](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L256-L262)：此处无法处理 `Stop::Finish`，那是 distribute 专属的），最后才调 `distribute(self, regions, balancing_target)` 排正文。

#### 4.2.4 代码实践

**实践目标**：看清 `page()` 与 `column()` 两个检查点循环的对称性，以及谁拦截哪种 `Relayout`。

**操作步骤**：在源码阅读层面完成下表（不必运行）：

| 层级 | 入口函数 | 检查点 | 循环内扣减 | 拦截的 Relayout | 放行的 Relayout |
|------|----------|--------|-----------|-----------------|-----------------|
| 页 | `page()` | `work.clone()` @L87 | `page_insertions.height()` @L92 | `Parent` @L98 | `Column`（此处 `unreachable!`，因为已被下层拦） |
| 列 | `column()` | `work.clone()` @L201 | `column_insertions.height()` @L206 | `Column` @L219 | `Parent` @L222（`err => return err` 放行给 page） |

**需要观察的现象**：`Relayout(Column)` 在 `column()` 被吞，绝不冒泡到 `page()`；`Relayout(Parent)` 反过来穿透 `column()`/`page_contents()`，只在 `page()` 被吞。

**预期结果**：填表后能解释「为什么 `page()` 的 match 里 `Relayout(Column)` 是 `unreachable!()`」——因为 column 循环已经把它拦截并内部重跑了。

#### 4.2.5 小练习与答案

**练习 1**：`page_insertions` 和 `column_insertions` 为什么在重排时**不**随 `work` 一起回滚？

**参考答案**：重排的**目的**就是「因为新加了一个浮动体/脚注，区域变小了，要带着这个变小的区域重排正文」。如果连插入物也回滚，重排就和上一轮完全一样，陷入死循环。所以 `work` 回滚（正文从头排），而 `Insertions` 只增不减（`push_float`/`push_footnote` 只追加），重排时 `height()` 反映了「到目前为止所有已确认的插入物」，正文据此避让。唯一防重复处理的机制是 `skips` 集合（4.3 末尾）。

**练习 2**：列平衡为什么用 `Stop::Relayout(Parent)` 而不是 `Relayout(Column)`？

**参考答案**：列平衡要让**所有列**在同一目标高度下重新测量，单列重排没有意义。`Relayout(Parent)` 触发整页重排，`page_contents()` 重新遍历全部列、把 `column_balancing_height` 传给每一列，使各列在统一高度下收敛到等高。

---

### 4.3 float：缩小区域并触发重排

#### 4.3.1 概念说明

`Composer::float` 由 [distribute 的 `placed()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L488-L512) 在遇到 `Child::Placed` 且 `float == true` 时回调。它做的事：

1. 算出当前作用域的**剩余空间** `remaining`；
2. 把浮动体排版成一帧；
3. 若放得下，按对齐推进 `top` 或 `bottom` 插入区，记录 skip，并返回 `Err(Stop::Relayout(scope))` **主动要求重排**；
4. 若放不下且换区域能改善，就排进 `work.floats` 队列留给下一区域。

注意第 3 步：**放得下也要重排**。因为浮动体一旦占位，distribute 可用的区域就变小了，已经排过的正文需要带着新尺寸重来。

#### 4.3.2 核心流程

```
float(placed, regions, clearance, migratable):
  若已 skip → 返回 Ok
  若 floats 队列非空 → 追加进队列，返回 Ok（保序）
  base = scope==Column ? regions.base() : page_base
  frame = placed.layout(engine, base)
  remaining = scope==Column ? regions.size.y           // 精确
             : Σ(后续各列 size.y) / columns.count      // 近似
  need = frame.height() + clearance
  若 !remaining.fits(need) && may_progress → 排进 work.floats，返回 Ok（留给下个区域）
  处理浮动体内部的脚注（footnotes）
  选对齐 align_y（默认按中点是否过半决定 top/bottom）
  area = scope==Column ? column_insertions : page_insertions
  area.push_float(placed, frame, align_y); area.skips.push(loc)
  return Err(Stop::Relayout(scope))                    // 触发重排
```

`remaining` 的 parent 作用域计算是个**近似**：它假设剩余空间均匀分摊到后续各列。这解释了为什么注释说 page 放置「only an approximation」。

#### 4.3.3 源码精读

[float() 全貌](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L298-L378)：

- [L306-L309](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L306-L309)：已处理过的浮动体直接跳过（靠 `skipped`，见后）。
- [L313-L316](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L313-L316)：**保序**——若已有排队浮动体，新来的也排队，避免插队打乱顺序。
- [L319-L339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L319-L339)：`base` 与 `remaining` 的双分支。Column 精确用 `regions.size.y`；Parent 用后续列高度之和除以列数。
- [L346-L349](https://github.com/typst/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L346-L349)：放不下且 `may_progress()` 才排队；否则继续往下（硬塞），避免在末区域死循环。
- [L356-L364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L356-L364)：默认对齐的「中点过半」启发式——若浮动体按正文流排会落在页面上半部，就 top 对齐，否则 bottom。
- [L367-L374](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L367-L374)：选对插入区并 `push_float`，记录 skip。
- [L377](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L377)：`Err(Stop::Relayout(placed.scope))`——放得下也要求重排。

distribute 的回调点：

[src/flow/distribute.rs:488-512](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L488-L512) —— 遇浮动体时，先把「末尾弱间距」临时退还（[L495-L496](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L495-L496)，因为浮动体可能造成断点，弱间距会折叠），调 `composer.float(..., migratable=true)`，再把弱间距加回。`migratable=true` 表示此处能处理 `Stop::Finish`（浮动体内部脚注不满足不变量时可迁移）。

`push_float` 与防重复：

[src/flow/compose.rs:679-697](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L679-L697) —— 按 `align_y` 分到 `top_floats` 或 `bottom_floats`，累加 `top_size`/`bottom_size`（含 clearance）。`skips` 记录 location；[skipped()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L606-L612) 合并查 `work.skips`、`page_insertions.skips`、`column_insertions.skips`，重排时已处理的浮动体不会二次入账。

#### 4.3.4 代码实践

**实践目标**：定量追踪一个 page 级浮动体加入后，各列可用高度如何变化、为什么必须重排。

**操作步骤**（源码阅读 + 手算）：

设定：页面 region `size = (400pt, 700pt)`，`expand.y = true`，`columns.count = 2`，`gutter` 忽略。文档正文中间出现一个 `#place(float: true, scope: parent, bottom)[ … ]`，浮动体排版后帧高 `f = 100pt`，正文目前排在第 0 列。

1. **第一轮**：`page()` 入口 `page_insertions.height() = 0`，故 `pod.size.y = 700pt`；`page_contents` 切成两列，每列 `column_height = 700pt`。
2. distribute 排第 0 列正文，遇到该浮动体 → 回调 `float()`：
   - `scope = Parent`，`base = page_base`，`remaining = (700 + 700) / 2 = 700pt`（近似，[L331-L338](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L331-L338)）。
   - `need = 100pt`，放得下 → `push_float` 进 `page_insertions`（bottom），`page_insertions.height()` 变为 `100pt`。
   - 返回 `Err(Stop::Relayout(Parent))`。
3. 该错误穿透 `column()`（`err => return err`）、`page_contents`，被 `page()` 拦截：`*self.work = checkpoint.clone()` 回滚正文，**进入第二轮循环**。
4. **第二轮**：`pod.size.y = 700 - page_insertions.height() = 700 - 100 = 600pt`；两列各 `column_height = 600pt`。distribute 在第 0 列以 **600pt** 重排正文。遇到同一浮动体时 `skipped(loc)` 为真 → 跳过，不再二次入账。

**需要观察的现象 / 预期结果**：把上述变化填入下表：

| 轮次 | `page_insertions.height()` | page `pod.size.y` | 每列 `column_height` |
|------|---------------------------|-------------------|----------------------|
| 第 1 轮（重排前） | 0 | 700pt | 700pt |
| 第 2 轮（重排后） | 100 | 600pt | 600pt |

可见一个 parent 浮动体让**所有列**等量减少 `f = 100pt`——因为 `page_insertions.height()` 是在切列**之前**从 page pod 扣掉的。这正是「page 级插入影响所有列」的几何根源。

> 待本地验证：若把 `columns.count` 改成 1，结论应保持「page pod 减 100pt、唯一列减 100pt」；若浮动体 `scope: column`，则只有当前列 `column_insertions.height()` 增长、其它列不变（见 4.2 column 循环）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 parent 作用域的 `remaining` 要除以 `columns.count`？

**参考答案**：page 浮动体最终会从 page pod 里扣除 `page_insertions.height()`，而这个 pod 被均分给所有列。浮动体占的 `f` 高度平摊到每列就是 `f`（每列都少 `f`），所以判断「放得下吗」时，用「后续列总剩余 / 列数」近似单列视角下的可用空间，与最终切列后的效果一致。它是近似，因为在多列尚未排完时无法确知每列实际剩多少。

**练习 2**：`float()` 在「放不下」时为什么额外要求 `regions.may_progress()` 才排队？

**参考答案**：若已在最后一个区域、换区域无法改善空间（`may_progress() == false`），排队留给「下一区域」毫无意义——没有下一区域了，会变成死循环（每轮都排队、每轮都重排）。所以在末区域改为「硬塞」（不排队、继续往下走），保证主循环必然收敛。这与 u4-l3 distribute 的防死循环守卫同源。

---

### 4.4 footnotes 与脚注不变量

#### 4.4.1 概念说明

脚注（footnote）的引用标记在正文里，条目（entry）排在页面底部。有一条强约束——**脚注不变量（footnote invariant）**：标记和条目必须在同一页。当条目第一行都放不下时，Typst 有两种应对：把**含标记的正文帧迁移到下一区域**（迁移，保持不变量），或把脚注排队到下一页（仅在迁移不可行时）。

`footnotes` 负责在一帧里**搜集**所有脚注引用；`footnote` 负责排单个条目。它们只在 `FlowMode::Root` 下生效（[mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L174-L177) 说明只有 Root 支持脚注）。

#### 4.4.2 核心流程

```
footnotes(regions, frame, flow_need, breakable, migratable):   // 由 distribute.frame / single 调用
  若 mode != Root → Ok
  notes = work.tags 里的 FootnoteElem + find_in_frame(frame) 里的 FootnoteElem（带 y 坐标）
  若空 → Ok
  relayout = false
  migratable = migratable && !breakable              // 不可断帧才允许迁移
  for (y, elem) in notes:
      flow_need = breakable ? y : flow_need          // 可断帧用标记的 y；不可断用整帧高
      match footnote(elem, regions, flow_need, migratable):
          Ok(()) => {}
          Err(Relayout(_)) => relayout = true        // 先把更多脚注排完再统一重排
          err => return err                          // Finish(迁移) / Error 直接上抛
      migratable = false                             // 只有第一条脚注允许迁移
  relayout ? Err(Relayout(Column)) : Ok
```

`footnote` 单条：排版条目 → 若第一帧为空但存在非空帧（一条都没排下）且可迁移且 `may_progress` → `Err(Stop::Finish(false))` 请求迁移；否则排队或正式入账并 `Err(Relayout(Column))`。

#### 4.4.3 源码精读

`footnotes` 入口与搜集：

[src/flow/compose.rs:389-453](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L389-L453) —— [L404-L409](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L404-L409) 同时从 `work.tags`（尚未 flush 的标签）和帧内（[find_in_frame_impl](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L994-L1011) 累加 y 坐标）搜集脚注。[L420](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L420) 关键：只有 `!breakable` 的帧才允许迁移（原子帧无法在内部断开，只能整帧搬走）。循环里 `relayout` 累积，最后统一返回一个 `Relayout(Column)`（[L448-L450](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L448-L450)）——避免第一条脚注就打断后续脚注的处理。

`footnote` 单条与迁移判定：

[src/flow/compose.rs:456-580](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L456-L580)：

- [L479-L486](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L479-L486)：本列还没有脚注时，先排版分隔线（separator）并预留 `clearance + 分隔线高`。
- [L488-L494](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L488-L494)：构造脚注条目的 pod（`expand.y = false`，扣掉 `flow_need + separator_need + gap`），调 [layout_footnote](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L636-L661) 排版（注意它把每帧 `set_parent(FrameParent::new(loc, Inherit::No))`，供内省把条目挂到引用处，见 u2-l3/u2-l4）。
- [L535-L542](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L535-L542)：**迁移判定**。第一帧空、但存在非空帧 = 一行都没排下。若可迁移且 `may_progress` → `Err(Stop::Finish(false))`（请求把含标记的帧迁到下一区域）；否则若 `may_progress || !flow_need.is_zero()` → 排队。注释 [L514-L534](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L514-L534) 详述了 `flow_need` 在此处为何关键：浮动体把 `regions` 原样传给脚注、自身占位未计入，但脚注会带非零 `flow_need`，使排队在后续页（`flow_need` 归零、脚注独占页面）能正确触发 `may_progress == false` 而被真正排出，避免死循环。
- [L544-L558](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L544-L558)：正式入账——存分隔线、存第一帧、扣 `note_need`、若还有剩余帧则存进 `self.footnote_spill`（跨区域续排，见 4.5）。
- [L561-L576](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L561-L576)：递归处理条目内的嵌套脚注，吞掉它们的 `Relayout`（马上就要统一重排了）。
- [L579](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L579)：排了脚注就要 `Err(Relayout(Column))`。

distribute 侧的调用点：

[src/flow/distribute.rs:463-469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L463-L469) —— 排每个正文帧后立刻 `composer.footnotes(...)`，且必须**先于** sticky 快照的重置（注释 [L456-L462](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L456-L462)：若非 sticky 帧的脚注放不下，帧连同前置 sticky 块要一起迁移，先重置 sticky 会把它们孤立在本区域）。`migratable: true`（distribute 能处理 `Stop::Finish`）。

#### 4.4.4 代码实践

**实践目标**：用脚注不变量解释「为什么不可断块里的脚注会触发整块迁移」。

**操作步骤**：阅读 [src/flow/compose.rs:420](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L420) 与 [distribute.rs:328-351](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L328-L351)（`single` 处理不可断块）。

设想：一个不可断块（`single`，`breakable=false`）里含脚注引用，块本身排下了，但脚注条目一行都排不下。

1. distribute `single()` 排出块帧，调 `frame(...)` → `composer.footnotes(regions, frame, frame.height(), breakable=false, migratable=true)`。
2. `footnotes` 里 `migratable = true && !breakable = true && !false = true`，即允许迁移。
3. `footnote` 发现第一帧空、有非空帧、`migratable && may_progress` → 返回 `Err(Stop::Finish(false))`。
4. 该 `Finish(false)` 一路冒泡到 distribute，被 [distribute() 入口](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/distribute.rs#L30-L34) 转成 `forced`，最终让这个块**迁移到下一区域**重排（那里脚注有空间）。

**需要观察的现象 / 预期结果**：写出迁移前后——重排前块在第 N 区域、脚注放不下；迁移后块整体出现在第 N+1 区域、脚注条目跟在第 N+1 区域底部。标记与条目同页，不变量成立。

**待本地验证**：写一个 `#block(breakable: false)[正文 #footnote[很长的脚注…]]`，把页面高度调到刚好让脚注排不下，观察输出中块是否整体移到下一页。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `footnotes` 里只有**第一条**脚注允许触发迁移（`migratable` 在循环中只置 false）？

**参考答案**：一次迁移把含标记的正文帧搬到下一区域后，该区域会重新变空、脚注重排。若允许多条脚注各自触发迁移，会出现「迁一条 → 重排 → 又一条迁 → 再重排」的连锁，且第二条脚注的标记未必在迁移的那一帧里。限定首条脚注迁移一次，既保住「最先排不下的那条」的不变量，又避免连锁抖动；其余脚注若也排不下，走排队路径到下一页。

**练习 2**：脚注条目的帧为什么要 `set_parent(..., Inherit::No)`？

**参考答案**：脚注条目逻辑上属于其引用处（标记所在元素），`FrameParent` 把条目帧挂到引用的 `Location` 下，使内省器（u2-l4、u3-l5）能通过 `group.parent` 把条目整体插入到引用元素之后、修正跨帧顺序。`Inherit::No` 表示条目内容**不**继承父级（引用处）的样式——脚注用自己的样式排版。详见 u2-l3 对 `FrameParent`/`Inherit` 的讲解。

---

### 4.5 footnote_spill / footnote_queue：跨区域续排

#### 4.5.1 概念说明

一条脚注条目可能很长，跨多个区域才排得完；多个脚注也可能在当前区域排不下、要留到下一区域。续排状态分两套：

- **`Composer.footnote_spill`**（`Option<IntoIter<Frame>>`）：当前区域内，某条脚注剩余的若干帧。挂在 Composer 上是为了**扛过重排**——重排会恢复 `Work` 检查点，若 spill 在 Work 上就会被抹掉，丢失脚注。
- **`Composer.footnote_queue`**（`Vec<Packed<FootnoteElem>>`）：当前区域内因「已有 spill/queue 在前」而保序排队的脚注，同理挂 Composer 上扛重排。

区域结束时（`column()` 出口），这两套暂存被**提交**给 `Work.footnotes` / `Work.footnote_spill`，由 `Work` 携带到下一区域；下一区域入口（`column()` 开头）再消费它们。

#### 4.5.2 核心流程

```
区域内（可能多次重排）：
  footnote() 排出多帧 → 第一帧入 column_insertions，其余存 Composer.footnote_spill
  footnote() 遇到已有 spill/queue → 排进 Composer.footnote_queue（保序）
  （重排时：Work 回滚，但 Composer.spill/queue 保留 ✓）

column() 出口（区域定稿）：
  work.footnotes.extend(Composer.footnote_queue.drain())      // 提交队列
  work.footnote_spill = Composer.footnote_spill.take()         // 提交续帧

下一区域 column() 入口：
  若 work.footnote_spill 存在 → footnote_spill() 方法：
      排一条新分隔线 + 取下一帧入 column_insertions，剩余继续存 spill
  column_contents() 还会处理 work.footnotes（之前排队的脚注）
```

#### 4.5.3 源码精读

`footnote` 里产生 spill/queue：

[src/flow/compose.rs:471-475](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L471-L475) —— 若 `Composer.footnote_spill.is_some() || !footnote_queue.is_empty()`，新脚注直接进 `footnote_queue`（保序，不插队）。[L556-L558](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L556-L558)：正式入账后，若还有剩余帧，存进 `self.footnote_spill`。

`column()` 出口提交：

[src/flow/compose.rs:227-L230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L227-L230) —— `work.footnotes.extend(self.footnote_queue.drain(..))`、`work.footnote_spill = self.footnote_spill.take()`。把 Composer 暂存移交 Work，供下一区域续排。

`column()` 入口消费 spill：

[src/flow/compose.rs:195-L197](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L195-L197) —— 取 `work.footnote_spill`，调 [footnote_spill()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L583-L604) 方法。该方法（[L590-L601](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L590-L601)）会**重新排一条分隔线**（脚注在新区域续排，需要新的分隔线），取下一帧入 `column_insertions`，剩余仍存 `self.footnote_spill`（若还有）。注意入口读的是 `work.footnote_spill`，方法内写的是 `self.footnote_spill`（Composer 的）——后者在本列后续若又触发重排会保留，出口再提交回 Work。

`column_contents()` 消费队列：

[src/flow/compose.rs:269-L271](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L269-L271) —— `for note in take(work.footnotes) { self.footnote(note, ..., migratable=false) }`，把上一区域排队、本区域才有空间排的脚注正式排掉。

`Work` 上的对应字段（跨区域载体）：

[src/flow/mod.rs:308-L311](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L308-L311) —— `footnotes: EcoVec<Packed<FootnoteElem>>`（排队脚注）与 `footnote_spill: Option<IntoIter<Frame>>`（续帧）。注意 [Work::done()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/mod.rs#L345-L351) 把这两者都算进「未完成」——只要有脚注没排完，flow 就不会终止。

#### 4.5.4 代码实践

**实践目标**：追踪一条跨两区域的脚注如何被「接力」排完。

**操作步骤**：设想一条很长的脚注，在区域 A 只排得下第 1 帧，第 2 帧须到区域 B。

1. 区域 A，`footnote()` 排出 `[frame1, frame2]`：`frame1` 入 `column_insertions`，`self.footnote_spill = Some(iter over [frame2])`，返回 `Relayout(Column)`。
2. `column()` 重排（可能多轮），`self.footnote_spill` 始终保留（不在 Work 检查点里）。
3. 区域 A 定稿，`column()` 出口：`work.footnote_spill = Some(iter over [frame2])`。
4. 区域 B，`column()` 入口：取 `work.footnote_spill`，调 `footnote_spill()` → 排**新分隔线** + `frame2` 入 `column_insertions`，`iter` 空 → `self.footnote_spill = None`。
5. 区域 B 定稿，无残留，`Work::done()` 最终为真，flow 终止。

**需要观察的现象 / 预期结果**：写出每一步 `Composer.footnote_spill` 与 `Work.footnote_spill` 的取值变化，确认「区域内靠 Composer 抗重排、区域间靠 Work 接力」的分工。

| 时刻 | `Composer.footnote_spill` | `Work.footnote_spill` |
|------|---------------------------|-----------------------|
| 区域 A 排出脚注后 | `Some([frame2])` | `None` |
| 区域 A 重排中 | `Some([frame2])`（保留） | `None` |
| 区域 A 出口提交后 | `None`（take 走） | `Some([frame2])` |
| 区域 B 入口消费后 | `None` | `None`（已消费） |

> 待本地验证：构造一条跨页脚注，确认两页底部都出现分隔线、且脚注文字首尾相接。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `Composer.footnote_spill` 不能直接用 `Work.footnote_spill`？

**参考答案**：重排时 `column()` 执行 `*self.work = checkpoint.clone()`，会把 `Work` 恢复到列入口的状态——若 spill 存在 Work 上，就会被抹掉，相当于「这条脚注的剩余帧丢了」。把 spill 放在 Composer（不属于 Work、不随检查点回滚）上，重排后依然在；等本列真正定稿、不再重排了，再在出口提交给 Work 携带到下一区域。注释 [compose.rs:74-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L74-L77) 正是此意。浮动体则相反——重排时会重新遍历到、重新入账，所以可以直接用 `work.floats`。

**练习 2**：续排时为什么要重新排一条分隔线（separator），而不是复用上一区域的？

**参考答案**：分隔线属于「本区域脚注区的顶部装饰」，每个有脚注的区域各自需要一条。续排的脚注出现在新区域底部，是新区域的脚注区，理应有自己的分隔线。上一区域的分隔线已经随那一帧输出，物理上不在本区域，无法「复用」。

---

### 4.6 Insertions：可加的插入物与 finalize 拼装

#### 4.6.1 概念说明

`Insertions` 是一个**只增不减**的累加容器，收集一个区域（page 或 column）里的所有 out-of-flow 产物：顶部浮动体、底部浮动体、脚注分隔线、脚注条目。它提供 `height()`（占去多少正文空间）、`float_height()`（仅浮动体高度，列平衡用）和 `finalize()`（把插入物拼到正文帧上得到最终帧）。

#### 4.6.2 核心流程

`finalize` 的拼装顺序（自上而下）：

```
output 高度 = inner 高度 + height()（top + bottom + footnote）
顶部浮动体 ── 从 y=0 起向下堆
inner 正文  ── 推到 y = top_size
底部浮动体 ── 从 (底部基准 - bottom_size) 起向下堆
脚注分隔线 + 脚注条目 ── 从 (output 底部 - footnote_size) 起向下堆
```

注释 [L768-L775](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L768-L775) 说明：Typst 把浮动体放在脚注**之上**（与 LaTeX 不同），是经过 0.12.0-rc1 用户反馈后采用的直观顺序。

#### 4.6.3 源码精读

`Insertions` 结构：

[src/flow/compose.rs:664-L675](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L664-L675) —— `top_floats` / `bottom_floats` 各是 `(PlacedChild, Frame)` 列表；`footnotes` 是帧列表；`footnote_separator` 单独一项；`top_size` / `bottom_size` / `footnote_size` 累计高度；`width` 记最宽插入物（对齐用）；`skips` 防重复。

`height()` / `float_height()`：

[src/flow/compose.rs:716-L724](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L716-L724) —— `height() = top_size + bottom_size + footnote_size`（正文可用空间要减去它）；`float_height()` 不含脚注，专供列平衡（脚注不该影响列高平衡的测量）。

`finalize()`：

[src/flow/compose.rs:728-L803](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L728-L803)：

- [L735-L743](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L735-L743)：先 `work.extend_skips`，若无任何插入物直接返回 inner（零成本）。
- [L745-L766](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L745-L766)：建更大尺寸的 `Frame::soft(size)`，铺顶部浮动体，把 inner 推到 `y = top_size`（并修正 baseline = `top_size + inner.baseline()`）。
- [L777-L786](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L777-L786)：底部浮动体从 `column_height.unwrap_or(size.y - footnote_size) - bottom_size` 起堆。
- [L788-L800](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L788-L800)：分隔线 + 脚注条目从底部起堆，每条之间加 `gap`。

页面级 `page()` 的 finalize 传 `column_height: None`（[L106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L106)），列级 `column()` 传 `self.column_balancing_height`（[L233-L238](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L233-L238)），影响底部浮动体的对齐基准。

#### 4.6.4 代码实践

**实践目标**：用 `finalize` 的拼装顺序解释「浮动体在脚注之上」。

**操作步骤**：阅读 [src/flow/compose.rs:745-L800](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L745-L800)，按代码顺序列出 `output` 帧从上到下的内容层。

**预期结果**：顺序为 ① 顶部浮动体 → ② inner 正文 → ③ 底部浮动体 → ④ 脚注分隔线 → ⑤ 脚注条目。即底部区域里，浮动体在脚注之上（与 LaTeX 默认相反）。

**待本地验证**：写一个同时含 `#place(float: true, bottom)[F]` 和 `#footnote[N]` 的页面，渲染后观察页面底部：浮动体 `F` 在脚注 `N` 之上。

#### 4.6.5 小练习与答案

**练习 1**：`finalize` 为什么要用 `Frame::soft(size)` 而不是 `Frame::hard(size)`？

**参考答案**：`soft` 帧的尺寸「跟随父级」、`hard` 帧「自身尺寸固定」（u2-l3）。compose 产出的帧还要被外层（页面 finalize、或上层块的 frame 容器）进一步缩放/对齐，用 `soft` 表示「我的尺寸可被外层重写」，避免在内嵌场景下硬撑尺寸。而页面整页容器（u3-l4 finalize）才用 `hard` 锁定物理尺寸。

**练习 2**：`float_height()` 与 `height()` 的区别为什么对列平衡很重要？

**参考答案**：列平衡（4.2）的目标是让各列**正文+浮动体**等高，脚注是「页面级共享」的边带内容、不该参与列高比较。`column()` 在算 `balancing_target` 时用 `h - float_height()`（[L209-L211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs#L209-L211)），只把浮动体高度算进列的「已用高度」，脚注高度排除在外，否则脚注多的列会被误判为更高、破坏平衡。

---

## 5. 综合实践

把本讲四块知识（两层循环、float 重排、脚注不变量、spill/queue 续排）串起来，完成下面这个**端到端追踪**任务。

**场景**：双栏页面（`columns.count = 2`），`page region = (400pt, 700pt)`。正文流依次为：若干段落 → `#place(float: true, scope: parent, bottom)[浮动体 F，高 80pt]` → 一段含 `#footnote[脚注 N，很长，跨两列才排得完]` 的正文。

**任务**：按时间顺序写出 compose 内部发生的事，重点回答：

1. **F 如何触发重排**：distribute 排到 F 时回调哪个方法？F 进入 `page_insertions` 还是 `column_insertions`？返回什么 `Stop`？被哪一层循环拦截？拦截后 `pod.size.y` 从多少变成多少？两列各自的 `column_height` 变成多少？
2. **脚注 N 的不变量**：脚注在区域 A 第 0 列排出第 1 帧、剩余第 2 帧。`Composer.footnote_spill` 何时被设、何时被提交给 `Work`？区域 A 第 1 列（或下一区域）入口如何续排？续排时为什么多了一条分隔线？
3. **最终拼装**：区域 A 定稿时 `Insertions::finalize` 把哪些层按什么顺序拼到输出帧上？

**参考解答要点**：

1. F 走 `Composer::float`（parent scope）→ 进 `page_insertions`（bottom）→ 返回 `Relayout(Parent)` → 穿透 `column()`/`page_contents`，被 `page()` 拦截 → 回滚 Work、`page_insertions.height()` 从 0 → 80 → 第二轮 `pod.size.y = 700-80 = 620pt` → 两列各 `column_height = 620pt`。
2. `footnote` 排出 `[f1, f2]`：f1 入 `column_insertions`、`self.footnote_spill = Some([f2])`、返回 `Relayout(Column)` 被 `column()` 拦截重排（spill 保留）；`column()` 出口提交 `work.footnote_spill = Some([f2])`；下一列/区域入口 `footnote_spill()` 排新分隔线 + f2。新分隔线是因为脚注在新区域的脚注区续排。
3. 顺序：顶部浮动体（无）→ inner 正文 → 底部浮动体（F）→ 脚注分隔线 → 脚注条目（f1，区域 B 再续 f2）。

> 这是源码阅读型实践，无需运行；若要验证，可在 `compose.rs` 的 `page()`/`column()` 循环与 `footnote()`/`footnote_spill()` 入口加 `eprintln!` 打印 `page_insertions.height()`、`column_insertions.height()`、`self.footnote_spill.is_some()`，渲染上述文档对照输出。

## 6. 本讲小结

- `compose` 是 flow 三段式的区域级组装器：对外每区域产一帧，对内通过 page/column 两层「检查点 + `Stop::Relayout`」循环消化所有重排，主循环对此无感知。
- **page 级**插入（`page_insertions`，parent 浮动体）在切列**之前**从 page pod 扣高度，因此等量压缩**所有列**；**column 级**插入（`column_insertions`，column 浮动体 + 脚注）只压缩当前列。`Relayout(Column)` 在 `column()` 被吞，`Relayout(Parent)` 穿透到 `page()` 被吞。
- `float()` 放得下也返回 `Relayout`（要带着变小的区域重排正文）；放不下且 `may_progress` 才排队，否则硬塞以防死循环。`skips` 防止重排时重复入账。
- 脚注遵循**不变量**（标记与条目同页）：不可断块的首条脚注排不下时返回 `Stop::Finish(false)` 触发整块迁移；可迁移性只给第一条脚注、且要求 `!breakable`。
- 跨区域续排靠两套状态：区域内 `Composer.footnote_spill/queue`（扛重排，因不属 Work 不被检查点抹掉），区域间 `Work.footnote_spill/footnotes`（接力到下一区域）。`Work::done()` 把它们计入「未完成」。
- `Insertions::finalize` 按 顶部浮动体 → 正文 → 底部浮动体 → 分隔线 → 脚注 顺序拼装（浮动体在脚注之上），用 `Frame::soft` 以便外层重写尺寸；`float_height()` 专为列平衡排除脚注高度。

## 7. 下一步学习建议

- **u4-l5（块布局 block）**：本讲多次提到 distribute 的 `single`/`multi` 与 `spill`，下一讲专门讲 `MultiSpill` 如何在区域间断开可断裂块——它与脚注的 spill 是平行的「跨区域续排」机制，对照阅读会很有收获。
- **u4-l6（列布局 columns 与列平衡）**：本讲的 `column_balancing_height`、`balancing_target`、`float_height()` 都是列平衡的零件，下一讲会把「反复 `Relayout(Parent)` 收敛到等高」的完整测量机制讲透。
- **回看 u3-l3 / u3-l4（页面 run 与 finalize）**：`compose` 产出的帧最终被页面 `finalize` 用 `Frame::hard` 整页容器按 background→header→inner→footer→foreground 拼装；理解 compose 的 `soft` 帧如何被外层 `hard` 帧收纳，能串起「正文帧 → 列帧 → 页帧」的完整产出链。
- **延伸阅读**：[src/flow/compose.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/flow/compose.rs) 末尾的 `layout_line_numbers` / `layout_line_number`（行号）本讲未展开，建议作为补充阅读，理解 `ParLineMarker` 计数器如何挂在列帧两侧。
