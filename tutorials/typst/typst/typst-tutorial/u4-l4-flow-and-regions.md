# 流式布局：flow、regions 与分页

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 **region（区域）** 的「剩余空间」模型：`Regions` 结构的五个字段各管什么、区域如何被内容逐渐消耗、何时判定「满了」、如何切换到下一个区域。
2. 跟踪一个段落块从 `layout_fragment` 进入 flow 主循环、经 collect 预处理、由 distribute 分配进区域、最终产出 `Frame` 的完整路径。
3. 理解 `stack`、`pad` 等容器如何**变换**区域（缩小、分摊、关闭扩展），并通过同一条 flow 机制递归复用。

本讲是第四单元「排版管线」的核心一讲：上一讲（u4-l3）我们看到了页面如何被创建、`layout_document` 如何收集 `Page`；本讲钻进页面的**内部**，看内容是怎样一格一格填进页面剩余空间的。

## 2. 前置知识

- **块级与行内**：块级内容（段落、标题、块）像积木一样从上往下堆叠；行内内容（文字、加粗）在行内从左往右流动。本讲的 flow 负责**块级**方向。
- **`Frame` 与 `Fragment`**（u4-l2、u4-l3 已讲）：`Frame` 是一个绝对定位的二维绘图树（最终排版产物）；`Fragment` 是一组 `Frame`——因为内容可能跨页/跨区域，一个逻辑内容会产出多个帧。
- **`Abs` 与 `Rel`**：`Abs` 是绝对长度（内部单位为 pt）；`Rel<Abs>` 是「相对长度 = 比例 × 基准 + 绝对部分」，需要相对某个基准尺寸解析。
- **pod**：Typst 源码术语，指「给子内容的区域」（pod regions）。容器不会直接把自己的区域交给子内容，而是先按自身规则构造一个缩小的/变换过的 pod。
- **溢出（overflow）**：内容超出区域边界。Typst 的排版永不失败，放不下时宁可溢出也不死循环——这句话的机制正是本讲 4.3 的主题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/layout/regions.rs` | `Region` / `Regions` 的定义：区域的数据模型与推进逻辑（类型在 typst-library，行为在 typst-layout——正是 u1-l3 讲过的架构决策） |
| `crates/typst-layout/src/flow/mod.rs` | flow 模块入口：`layout_frame` / `layout_fragment` / `layout_columns` 与主循环 `layout_flow` |
| `crates/typst-layout/src/flow/collect.rs` | collect 阶段：把 realize 输出的元素对预处理成 `Child` 枚举（段落在此提前断行） |
| `crates/typst-layout/src/flow/compose.rs` | compose 阶段：组装单个页面/区域（列、浮动体、脚注、行号），调用 distribute |
| `crates/typst-layout/src/flow/distribute.rs` | distribute 阶段：把 `Child` 逐个分配进区域，决定「放下还是换页」 |
| `crates/typst-layout/src/flow/block.rs` | `BlockElem` 的可断/不可断布局，以及 pod 构造（`unbreakable_pod` / `breakable_pod`） |
| `crates/typst-layout/src/stack.rs` | `#stack()` 的布局器：一个精简的「自管区域」示范 |
| `crates/typst-layout/src/pad.rs` | `#pad()` 的布局器：区域缩放（shrink/grow）的教科书案例 |
| `crates/typst-layout/src/pages/run.rs` | 页面运行：flow 与页面的衔接点（`Regions::repeat` 的诞生地） |
| `crates/typst-layout/src/rules.rs` | 内置规则注册：`PAD_RULE` / `STACK_RULE` / `COLUMNS_RULE` 如何把容器接入 flow |

数据流总览：

```
pages/run.rs ── Regions::repeat(页面内容区) ──▶ layout_flow (flow/mod.rs)
                                                  │ configuration: 列/脚注/行号配置
                                                  │ collect: Pair 列表 ─▶ Vec<Child>
                                                  │ Work: 待处理 Child + spill/floats/footnotes
                                                  ▼ 每个区域一轮
                                              compose (flow/compose.rs) ── 页/列组装
                                                  ▼
                                              distribute (flow/distribute.rs) ── 逐 Child 协商
                                                  │ Single/Multi Child
                                                  ▼
                                              block.rs ── 构造 pod ──▶ 递归 layout_fragment
                                                  │（MultiLayouter 分支）
                                                  ▼
                                              stack.rs / pad.rs / grid.rs …
```

## 4. 核心概念与源码讲解

### 4.1 模块一：region 生命周期——`Regions` 的结构与推进

#### 4.1.1 概念说明

排版中最本质的约束是：**当前页还剩多少空间**。Typst 把这个概念建模为 region（区域）：一块宽度固定、高度已知（或部分已知）的矩形剩余空间。

为什么需要一整个 `Regions` 而不是一个 `Size`？因为分页决策需要「看得见未来」：

- 放不下当前内容时，要知道**下一个区域**有多高（下一页可能更高）；
- 要判断换区域**是否值得**（如果下一区域和当前一样满，换了也白换）。

于是 `Regions` 不是「一个区域」，而是「一个区域序列的游标」：第一个区域是当前所在的，后面还排着队。

#### 4.1.2 核心流程

一个区域的生命周期：

1. **诞生**：页面流中由 `Regions::repeat(内容区尺寸)` 创建（每个区域同高，无限重复）；容器内部则由父区域变换派生（见模块三）。
2. **消耗**：每放入一个块，`size.y` 减去该块高度；`used.y` 相应累加。区域本身只被减法消耗，不回滚（sticky 场景除外，见 4.3）。
3. **判定**：
   - `is_full()`——是否已满、该换区了；
   - `may_break()`——是否还有下一个区域；
   - `may_progress()`——换下一个区域是否能**改善**空间不足。
4. **切换**：`next()` 从 backlog 或 last 中取下一个高度，重置 `size.y` 与 `full`。

用伪代码表示消耗过程：

```
while 还有待放内容:
    if 内容放得进剩余高度 (fits) 或 换区无进展 (not may_progress):
        放入; size.y -= 内容高度
    else:
        切换区域: next()   # size.y ← backlog 弹出值或 last
```

#### 4.1.3 源码精读

`Regions` 的五个字段——注意 `size` 是**剩余**尺寸，`full` 是**完整**高度：

- [crates/typst-library/src/layout/regions.rs:L42-L55](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/regions.rs#L42-L55)——定义 `Regions`：`size`（第一个区域的剩余尺寸）、`expand`（子内容是否应撑满而非收缩贴合）、`full`（用于相对尺寸解析的完整高度）、`backlog`（后续区域高度列表）、`last`（backlog 耗尽后无限重复的最终高度）。
- [crates/typst-library/src/layout/regions.rs:L59-L67](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/regions.rs#L59-L67)——`repeat` 构造无限重复的同尺寸区域序列：`last = Some(size.y)`、backlog 为空。这正是页面流的模型：内容永远可以「翻页」。
- [crates/typst-library/src/layout/regions.rs:L98-L111](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/regions.rs#L98-L111)——三个判定方法。`is_full` 的实现是 `Abs::zero().fits(self.size.y) && self.may_progress()`：剩余高度已经 ≤ 0（近似），且换区有改善空间。`may_progress` 为 false 的典型情形是「正处于区域顶端」（`size.y == last`），此时连顶端都放不下的内容换页也放不下，只能硬放溢出。
- [crates/typst-library/src/layout/regions.rs:L114-L127](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/regions.rs#L114-L127)——`next()`：优先从 `backlog` 弹出下一个高度，backlog 空则用 `last`，同时把 `full` 也重置为新高度。
- [crates/typst-library/src/layout/abs.rs:L117-L119](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/abs.rs#L117-L119)——`fits` 的语义：`self + EPS >= other`，即「self 不小于 other」（带一个极小容差，避免浮点边界抖动）。所以 `Abs::zero().fits(size.y)` 就是「剩余高度已不多于零」。

页面流中 `Regions` 的诞生地——页面运行把自己的内容区交给 flow：

- [crates/typst-layout/src/pages/run.rs:L187-L202](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/run.rs#L187-L202)——`area = size - margin.sum_by_axis()`（页尺寸减去四边距），然后以 `Regions::repeat(area, ...)` 调用 `layout_flow`，模式为 `FlowMode::Root`（可承载脚注与行号）。每个物理页都会执行一次这里的 flow。

另外 `Regions` 实现了美观的 `Debug`（[crates/typst-library/src/layout/regions.rs:L141-L159](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/regions.rs#L141-L159)），形如 `Regions [453.5x700.2pt, ..]`——实践打点时直接 `{:?}` 打印即可。

#### 4.1.4 代码实践

**实践：手工演算一个区域序列（纸笔实践）**

1. **实践目标**：在不运行代码的情况下，能用 region 模型预测一个简单文档的分配序列，为 4.3 的打点实践建立预期。
2. **操作步骤**：
   - 设想 A4 纵向页面、默认边距：内容区约 453.5pt 宽 × 700.2pt 高（精确值以本地 `Regions` 的 Debug 输出为准，**待本地验证**）。
   - 写一个仅含 8 个 `#block(height: 150pt)[X]` 的文档（每个块高 150pt，块间距记 0）。
   - 在纸上画表格：每行是「放入一个块后的剩余高度」。第一页放入 4 块后剩余 700.2 − 600 = 100.2pt；第 5 块需要 150pt，`fits` 失败。
3. **需要观察的现象**：100.2pt 放不下 150pt 的块——此刻 `may_progress()` 为真（下一页高度 700.2pt ≠ 100.2pt），所以应触发换页；第 5～8 块进入第二页。
4. **预期结果**：两页文档；第二页放 4 块后 work 耗尽，循环结束。编译 `#block(height: 150pt)[X]` × 8 应得到 2 页（**待本地验证**）。
5. 把你的纸面序列留好，综合实践（第 5 节）会用真实打点与之对照。

#### 4.1.5 小练习与答案

**练习 1**：`Regions::repeat` 构造的序列中，`backlog` 与 `last` 分别是什么？为什么页面流必然 `may_break() == true`？

**答案**：`backlog` 为空、`last = Some(size.y)`。因为 `last` 存在，`may_break()`（backlog 非空或 last 存在）恒为真——内容永远可以翻到下一页，这与「文档可以无限长」的直觉一致。

**练习 2**：一个块在区域顶端就放不下（剩余高度 = 完整高度仍不够）。此时 `may_progress()` 返回什么？后果是什么？

**答案**：返回 false（backlog 空且 `last == size.y`，即换页后高度不变）。后果是 distribute 的「放不下」分支不触发换区，内容被硬放进区域、视觉上溢出边界——这是有意设计：溢出优于无限循环。

**练习 3**：为什么 `Regions` 要同时保存 `size`（剩余）和 `full`（完整）两个高度？

**答案**：相对长度（如 `50%` 高的块、`v(20%, weak: true)`）需要相对**完整**区域高度解析而不是已消耗的剩余值，否则同样的 `50%` 在页面顶端和页面底部会得到不同尺寸。`base()` 方法（regions.rs L73-75）正是返回 `Size::new(size.x, full)` 供相对解析使用。

### 4.2 模块二：flow 主循环——collect、Work 与每区域一轮的 compose

#### 4.2.1 概念说明

`layout_flow` 是块级排版的**驱动器**：它手里有一列预处理好的子内容（`Child`）和一个区域游标（`Regions`），任务是把子内容尽可能塞进当前区域、塞不下就产出当前区域的 `Frame` 并推进区域，直到子内容耗尽。

它把工作分成三层，各司其职：

- **collect（收集）**：一次性把 realize 输出的元素对翻译成更易处理的 `Child` 枚举；段落在此**提前断行**（因为断行只依赖宽度，不依赖具体区域高度）。
- **compose（组装）**：负责「一个页面/区域」的完整组装——多列排布、浮动体、脚注、行号都在这一层进进出出。
- **distribute（分配）**：负责把 `Child` 逐个放进区域，是「放下还是换页」的决策现场（模块三的主角）。

`Work` 结构贯穿三层，表示「尚未完成的工作」——主循环每轮结束后检查它是否清空来决定终止。

#### 4.2.2 核心流程

```
layout_flow(children, regions, column, mode):
    config = configuration(styles, regions, column, mode)   # 列/脚注/行号配置
    children = collect(children) → Vec<Child>              # 预处理
    work = Work::new(children)
    finished = []
    loop:                                                    # 每轮 = 一个区域
        frame = compose(work, config, regions)               # 内部调 distribute
        finished.push(frame)
        if work.done() 且 (不需要 y 扩展 或 backlog 已空):
            break
        regions.next()                                       # 换下一个区域
    return Fragment(finished)
```

终止条件里 `regions.expand.y` 的分支：当容器要求沿 y 撑满（比如固定高度的 block），即使内容没了，也必须把 backlog 里的每个区域都产出一个帧。

#### 4.2.3 源码精读

入口三兄弟——单区域、多区域、列模式共用一个实现：

- [crates/typst-layout/src/flow/mod.rs:L43-L52](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/mod.rs#L43-L52)——`layout_frame`：布局进**单个**区域返回单帧，内部转调 `layout_fragment` 后取 `Fragment::into_frame`。
- [crates/typst-layout/src/flow/mod.rs:L57-L82](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/mod.rs#L57-L82)——`layout_fragment`：布局进**多个**区域返回 `Fragment`。它把 Engine 拆解成 tracked 参数、设置单列默认的 `ColumnOptions` 后进入缓存实现 `layout_fragment_impl`。
- [crates/typst-layout/src/flow/mod.rs:L117-L173](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/mod.rs#L117-L173)——`layout_fragment_impl`：先拒绝「向无限尺寸扩展」（L132-137 的两个 bail），再经 `#[comemo::memoize]` 缓存；随后调用 realize 例程取得 `Vec<Pair>`（L155-162，realize 阶段的输出，见 u4-l2），交给 `layout_flow`。

主循环本体：

- [crates/typst-layout/src/flow/mod.rs:L197-L240](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/mod.rs#L197-L240)——`layout_flow` 全貌：`configuration` 建共享配置（列宽按「总宽减去列间距再除以列数」计算，见 L258-259）、`collect` 预处理、`Bump` 分配器装 Child、然后进入 L225-237 的区域循环。
- [crates/typst-layout/src/flow/mod.rs:L309-L327](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/mod.rs#L309-L327)——`Work` 的字段：未处理 children 切片（只收缩不弹出）、可断块的 spill 余料、排队的浮动体与脚注、待挂载 tag。这是三阶段共享的可变状态。

collect 的翻译规则：

- [crates/typst-layout/src/flow/collect.rs:L94-L126](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/collect.rs#L94-L126)——`run_block` 的分派表：`TagElem → Child::Tag`、`VElem → Child::Rel/Fr`（垂直间距）、`ParElem → 提前断行`、`BlockElem → Single/Multi`、`PlaceElem → Placed`、`ColbreakElem → Break`；注意 L112-116——容器内的 `pagebreak` 直接报错并提示改用 `#colbreak()`，因为 flow 的「区域」不一定是页。
- [crates/typst-layout/src/flow/collect.rs:L355-L374](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/collect.rs#L355-L374)——`Child` 枚举全貌：Tag（内省标签）、Rel/Fr（两种间距，携带 weakness 弱度等级）、Line（已断好的一行）、Single（不可断块）、Multi（可断块）、Placed（绝对/浮动放置）、Flush、Break。distribute 对每个变体有一个对应方法。
- [crates/typst-layout/src/flow/collect.rs:L160-L189](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/collect.rs#L160-L189)——`par` 方法：段落在此调用 `inline::layout_par` **提前断行**成一行行 Frame（注释明确说明「断行不依赖具体区域」），前后各补一个 weakness=4 的段间距。

compose 的入口与「缩掉插入物再分发」模式：

- [crates/typst-layout/src/flow/compose.rs:L150-L170](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/compose.rs#L150-L170)——`compose` 函数：构造 `Composer` 并调 `.page(...)`。注释说明了分工：composer 主要负责**流外插入物**（浮动体与脚注），流入 contents 由 distribute 完成。
- [crates/typst-layout/src/flow/compose.rs:L199-L221](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/compose.rs#L199-L221)——`Composer::page`：先 `pod.size.y -= page_insertions.height()`（页级插入物占掉的高度），失败时（`RelayoutStop::Relayout(ParentScope)`）回滚 work 快照重来。这是「插入物改变可用区域 ⇒ 整页重排」的重排循环。
- [crates/typst-layout/src/flow/compose.rs:L224-L254](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/compose.rs#L224-L254)——`page_contents`：单列时直接走 column；多列时把「每页高度重复 count 次」铺成一个 backlog 构造列区域序列（L235-240），列宽用配置中的 `columns.width`。

#### 4.2.4 代码实践

**实践：跟踪一次 `layout_fragment` 的调用栈（源码阅读型实践）**

1. **实践目标**：把 4.2.2 的流程图与真实代码逐行对上号。
2. **操作步骤**：
   - 从 [crates/typst-layout/src/lib.rs:L21](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/lib.rs#L21)（`pub use self::flow::{layout_fragment, layout_frame}`）出发，用编辑器「查找引用」找到 `layout_fragment` 的所有调用点。
   - 依次确认：`pages/run.rs` 谁调用它；`flow/block.rs` 里 Content 分支的递归调用；`stack.rs` 的 `layout_block` 里的调用。
   - 数一数：从 `layout_flow` 出发，有多少条路径能再次回到 `layout_fragment`（答案应不少于 3 条：block 的 Content 体、MultiLayouter 回调内的 stack/pad、列模式）。
3. **需要观察的现象**：排版是一棵**递归树**——每次嵌套容器都经由 `layout_fragment`（或 `layout_frame`）重新进入同一条流水线，只是 regions 越嵌套越小。
4. **预期结果**：你能不假思索地画出「`layout_fragment` → realize → `layout_flow` → compose → distribute → block → `layout_fragment`」的环。

#### 4.2.5 小练习与答案

**练习 1**：为什么段落要在 collect 阶段提前断行，而不是在 distribute 里遇到 `ParElem` 再断？

**答案**：断行只取决于可用**宽度**（区域宽度在同一 flow 里恒定），与剩余高度无关。提前断行后，distribute 拿到的 `Child::Line` 是高度已知的成品帧，可以逐行做 fits 判断与 widow/orphan 分组（`LineChild.need`），把「行级」的分页决策做细。

**练习 2**：`layout_flow` 循环的终止条件为什么在 `work.done()` 之外还要求 `!regions.expand.y || regions.backlog.is_empty()`？

**答案**：`expand.y` 为真时容器承诺「每个区域都要有产出帧」（例如固定高度的块：`height: 150pt` 分到两个区域就要两个 150pt 帧）。此时即使内容已排完，也要把 backlog 耗尽才能停，否则 Fragment 帧数与区域数不匹配。

**练习 3**：`Child::Rel`、`Child::Fr` 里的 `u8` weakness 参数是干什么的？

**答案**：标记间距的「弱度」。weak 间距在区域边界（页顶/页底）或与其他间距相邻时会被折叠或丢弃（distribute 的 `keep_weak_rel_spacing` / `trim_spacing`）。不同来源的间距弱度不同：段内行距 weakness=5、段间距=4、块的 above/below=3、fr 间距=2，数值越大越容易被丢弃。

### 4.3 模块三：分配协商——distribute 如何决定「放下还是换页」

#### 4.3.1 概念说明

distribute 是 flow 的心脏。它的核心问题只有一个：**当前 Child 放得进剩余空间吗？放不下怎么办？**

答案的三个原则：

1. **fits 优先**：高度放得下就直接放。
2. **换页要有意义**：放不下时，只有当 `may_progress()` 为真（下一区域可能更高）才换页。
3. **溢出优于死循环**：换页无意义时硬放，内容溢出边界但流程继续。

「可断块」的分裂与余料（spill）机制：一个 `Multi` 块（比如一个跨页段落块）布局后返回多帧——第一帧留在当前区域，其余帧打包成 `MultiSpill` 存回 `work.spill`，下一轮区域从 spill 继续，而不是重排整个块。

此外还有两类特殊迁移：**sticky**（粘性块，如标题）可以带着自己的检查点一起迁往下一区域，避免「标题孤零零留在页尾」；**migratable**（纯 tag/零尺寸帧）在区域只含它们时会整体迁走，避免产生空页。

#### 4.3.2 核心流程

```
distribute(composer, regions, balancing_target):
    snapshot 初始状态
    run():
        若 work.spill 有余料: multi_spill(spill)      # 续排上一区域的断块
        while work.head() 还有 Child:
            child(head) → 按 Child 变体分派
            work.advance()
        # 三种出口:
        #   Stop::Finish(Soft)  空间不足,本区域结束
        #   Stop::Finish(Forced) 显式 break
        #   Stop::Relayout      浮动体要求重排本区域
    finalize():
        迁移检查(sticky / migratable) → trim 末尾弱间距
        → 统计 fr 总量 → 分配 fr 空间 → 对齐摆放 → 产出 (Frame, 实际用高)
```

fits 判定（含列平衡目标）：

\[
\text{fits}(h) = \bigl(h \le \text{regions.size.y} + \epsilon\bigr) \;\wedge\; \bigl(\text{target} = \varnothing \;\vee\; \text{used}_y \le \text{target}\bigr)
\]

fr 分配在 finalize 中按比例进行：

\[
\text{length}_i = \text{fr}_i \cdot \frac{\text{free}}{\sum_j \text{fr}_j}
\]

#### 4.3.3 源码精读

入口与状态：

- [crates/typst-layout/src/flow/distribute.rs:L53-L78](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L53-L78)——`distribute` 函数：构造 `Distributor`，跑 `run()`，把三种 `Stop` 出口分别翻译成「内容已尽」「软结束」「强制结束」，最后 `finalize` 产出 `(Frame, Abs)`（帧 + 实际使用高度，后者供列平衡用）。
- [crates/typst-layout/src/flow/distribute.rs:L160-L196](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L160-L196)——`run` 主循环与 `child` 分派：先消化 spill 余料，再逐个处理 Child。`child` 方法就是一张 Child 变体 → 处理方法的总表。
- [crates/typst-layout/src/flow/distribute.rs:L213-L216](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L213-L216)——`use_height`：区域消耗的两行真身——`regions.size.y -= amount; used.y += amount`。
- [crates/typst-layout/src/flow/distribute.rs:L334-L341](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L334-L341)——`fits`：剩余高度够，且（若有列平衡目标）已用高度未超目标。

三类「放不下」分支——本模块最值得逐字读的 60 行：

- [crates/typst-layout/src/flow/distribute.rs:L344-L366](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L344-L366)——`line`：单行放不下且换区有进展 → 软结束；更精妙的是 L355-363——widow/orphan 预防：若本行的 `need`（含被分组保护的后续行总高）放不下、但**下一区域**放得下，也提前软结束，把整组行一起带走。
- [crates/typst-layout/src/flow/distribute.rs:L369-L397](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L369-L397)——`single`（不可断块）：先用 `Region::new(self.regions.base(), ...)` 布局（注意给的是**完整**高度基准，让 auto 块可以自由决定高度），再看帧高是否放得下剩余空间。
- [crates/typst-layout/src/flow/distribute.rs:L400-L438](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L400-L438)——`multi`（可断块）：区域已满直接软结束；否则布局后取首帧，若返回了 spill 则存入 `work.spill` 并软结束。L417-425 处理「首帧为空但余料有内容」的孤儿情况——整块推到下一区域。
- [crates/typst-layout/src/flow/distribute.rs:L576-L585](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L576-L585)——`break_`：显式列断（`#colbreak()`），返回 `Finish::Forced`——区别于 Soft：强制结束的帧即使内容为空也要保留。

spill 的续排机制：

- [crates/typst-layout/src/flow/collect.rs:L465-L491](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/collect.rs#L465-L491)——`MultiChild::layout`：调 `layout_full` 得整个 Fragment，首帧之外打包成 `MultiSpill`（携带原 regions 的 full/first 等信息）。
- [crates/typst-layout/src/flow/collect.rs:L564-L620](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/collect.rs#L564-L620)——`MultiSpill::layout`：把新区域的高度并入自己的 backlog 重新构造 pod，调同一个 `layout_full` 后**跳过已产出的帧**（L594-598）。注释坦言这是新旧区域模型的兼容层。

收尾与摆放：

- [crates/typst-layout/src/flow/distribute.rs:L590-L719](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L590-L719)——`finalize`：先处理三类迁移（forced 冲刷 tag / 全 migratable 回滚 / sticky 检查点回滚），再 trim 尾部弱间距、统计 fr、按 `region.expand` 在「区域尺寸」与「实际用尺寸」之间二选一（L651）、逐 item 计算坐标写入输出帧。L694-699 说明：整个区域的基线取自**第一个**有基线的流内帧。
- [crates/typst-layout/src/flow/distribute.rs:L472-L500](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L472-L500)——`frame` 方法中 sticky 检查点的建立：粘性块先存快照，若本区域最终停在它身上，`finalize` 会回滚快照把它（及其后帧）一起移到下一区域——这就是「标题不与正文分离」的实现。

#### 4.3.4 代码实践

**实践：制造一次「溢出」并解释它为何不报错（观察型实践）**

1. **实践目标**：亲眼确认「放不下且换区无进展 ⇒ 硬放溢出」的策略。
2. **操作步骤**：
   - 写文档：`#block(height: 2000pt, breakable: false)[hi]`——一个远高于页面、又不可断的块。
   - 编译：`cargo run -p typst-cli -- compile overflow.typ`。
3. **需要观察的现象**：编译成功、无错误；PDF 中块的内容超出页面边界（可在导出的 PNG 中看清）。
4. **预期结果**：对照源码路径——`single` 方法布局出的帧高 2000pt，`fits` 失败，但该块位于区域顶端，`may_progress()` 为 false，于是不触发 `Finish(Soft)`，帧被硬放进区域。**待本地验证**（用 `typst compile --format png` 看第一页效果更直观）。

#### 4.3.5 小练习与答案

**练习 1**：`Finish::Soft` 与 `Finish::Forced` 的区别是什么？谁消费这个区别？

**答案**：Soft 表示「空间不足导致的区域结束」，Forced 表示「显式 break」。消费点在 `distribute` 函数（L68-70）：Forced 时 `forced = true`，`finalize` 中 forced 分支会冲刷未挂载的 tag 且不做 migratable 回滚（显式断点处即使是纯 tag 区域也保留帧），而 Soft 结尾的纯 migratable 区域会整体迁移到下一区域、避免产出空帧。

**练习 2**：标题（sticky 块）是怎么避免「标题在上一页页尾、正文在下一页」的？

**答案**：distribute 遇到 sticky 帧且此前没有检查点时，先快照（work + items + used）。若区域最终结束在这个 sticky 帧上（其后没有非 sticky 帧），`finalize` 回滚快照，让标题随下一区域重新分配。注释还说明了边界情况：sticky 组在区域顶端时 `stickable` 置 false 禁用迁移，防止无限来回。

**练习 3**：`RelayoutStop::Relayout` 与 `Stop::Finish` 都会中断 distribute，它们分别意味着什么？

**答案**：`Finish` 是正常的区域结束（外层 `layout_flow` 会 `regions.next()` 换区域继续）；`Relayout` 是**同一区域的重来**——浮动体插入后可用空间缩小，需要以更小的 pod 重跑本区域（compose 的 page/column 重排循环捕获它并回滚 work 快照），区域游标不推进。

### 4.4 模块四：容器变换——pod 构造与 stack/pad 的复用

#### 4.4.1 概念说明

容器（block、stack、pad、grid……）自己不发明排版算法，它们做的事本质上只有一件：**变换区域**。把父区域按自己的规则改写成 pod 交给子内容，再把子内容的产出帧「装饰」回来（放大、填充、描边）。

三种典型变换：

1. **block 的尺寸分摊**（`breakable_pod`）：固定高度的块把总高度沿区域序列**切开**，形成新的 first + backlog；auto 高度则原样继承区域序列。
2. **pad 的整体缩放**（`map` + `shrink`）：每个区域的高度都减去上下 padding，宽度减去左右 padding；产出帧再反向放大。
3. **stack 的扩展关闭**：子内容不允许沿堆叠方向 expand（否则第一个子内容就把区域吃满了），stack 自己统一管理主轴。

接线方式值得单独强调：`#pad()`、`#stack()`、`#columns()` 在 realize 阶段被内置规则改写成一个 `BlockElem`，其 body 是 `MultiLayouter` 回调。于是它们天然成为 flow 里的一个可断块，经由 `layout_multi_block` 的回调分支被调用——容器复用 flow 的全部协商机制（spill、sticky、换页），一行都不用重写。

#### 4.4.2 核心流程

以 `#pad(left: 10pt, top: 20pt)[...]` 嵌在一个页面里为例：

```
realize:  PadElem ──PAD_RULE──▶ BlockElem { body: MultiLayouter(layout_pad) }
flow:     collect ─▶ Child::Multi ─▶ layout_multi_block
            └─ breakable_pod: auto 高度 ⇒ 继承页面区域序列
            └─ MultiLayouter 分支 ─▶ layout_pad(elem, ..., pod)
                 ├─ regions.map(shrink): 每个区域高度减 (top+bottom), 宽度减 (left+right)
                 ├─ layout_fragment(body, pod)   # 递归回到 flow!
                 └─ 对每帧 grow: 放大回原尺寸, 内容平移 (left, top)
```

pad 的 `grow` 是 `shrink` 的逆运算，源码注释给出了推导。设目标宽度 \(w\)、内容宽 \(s\)、左右 padding 合计 \(p = l + r\)（其中相对部分为 \(p_{\text{rel}}\)、绝对部分 \(p_{\text{abs}}\)），要求：

\[
w - p_{\text{rel}} \cdot w - p_{\text{abs}} = s
\quad\Longrightarrow\quad
w = \frac{s + p_{\text{abs}}}{1 - p_{\text{rel}}}
\]

#### 4.4.3 源码精读

block 的两个 pod 构造器：

- [crates/typst-layout/src/flow/block.rs:L255-L291](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/block.rs#L255-L291)——`unbreakable_pod`：宽度/高度按 `Sizing`（Auto / Fr / Rel）解析——Auto 取整个区域、Rel 相对基准解析、Fr 等价于 100%（实际份额由 finalize 分配）；再减去 inset；**手动指定尺寸的轴才打开 expand**。
- [crates/typst-layout/src/flow/block.rs:L294-L364](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/block.rs#L294-L364)——`breakable_pod`：`height` 为 Auto/Fr 时**继承**外层区域序列（first/backlog/last 原样传递——可断块直接活在父区域的未来里）；为固定值时调 `distribute` 把总高度切进新区域序列且 `last = None`（总高度有限，不再无限续）。
- [crates/typst-layout/src/flow/block.rs:L374-L419](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/block.rs#L374-L419)——固定高度的分摊 `distribute`：从第一个区域开始，逐个区域「取 min(区域高, 剩余高度)」直到分完；剩余放不下就堆给最后一个区域（注释：会溢出，但别无选择）。

MultiLayouter 的接线与注册：

- [crates/typst-layout/src/flow/block.rs:L184-L188](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/block.rs#L184-L188)——`layout_multi_block` 的 MultiLayouter 分支：合并 pod 与外层的 expand（且仅在尺寸有限时），调用回调——`layout_pad` / `layout_stack` / `layout_columns` / `layout_grid` 都从这里进入。
- [crates/typst-layout/src/rules.rs:L686-L696](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/rules.rs#L686-L696)——三个内置规则：`PAD_RULE`、`COLUMNS_RULE`、`STACK_RULE` 都把元素包装为 `BlockElem::multi_layouter(elem, layout_fn)`。结合 u4-l2 的 NativeRuleMap：容器在 realize 阶段就已经变成了带回调的 BlockElem。

pad——最短小精悍的布局器：

- [crates/typst-layout/src/pad.rs:L11-L36](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pad.rs#L11-L36)——`layout_pad` 全部逻辑：`regions.map(shrink)` 缩区域 → `layout_fragment`（递归）→ 每帧 `grow` 放回。注意 `map` 会同时变换 backlog 与 last 里的每个高度（见 [crates/typst-library/src/layout/regions.rs:L81-L95](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/layout/regions.rs#L81-L95)，宽度对所有区域统一取第一个区域的值）。
- [crates/typst-layout/src/pad.rs:L39-L59](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pad.rs#L39-L59)——`shrink`（单区域）与 `shrink_multiple`（区域序列的 size/full/backlog/last 逐项缩减）。
- [crates/typst-layout/src/pad.rs:L80-L93](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pad.rs#L80-L93)——`grow`：按上面的公式求放大后尺寸，`frame.translate` 把既有内容整体平移进内框。

stack——一个自管区域的迷你 flow：

- [crates/typst-layout/src/stack.rs:L166-L195](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/stack.rs#L166-L195)——`StackLayouter::new`：L178-179 **关闭子内容主轴 expand**（`regions.expand.set(axis, false)`）——子内容不许沿堆叠方向撑满，主轴空间由 stack 统一记账；记下 `initial`（本区域初始尺寸）与 `used`/`fr`。
- [crates/typst-layout/src/stack.rs:L221-L250](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/stack.rs#L221-L250)——`layout_block`：先查 `regions.is_full()` 就 `finish_region()`（与 flow 同款的换区判断）；随后调 `crate::layout_fragment`（再次递归回 flow）并处理 `h`/`v` 的透明透传。
- [crates/typst-layout/src/stack.rs:L271-L300](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/stack.rs#L271-L300)——`layout_fragment`：逐帧记账（主轴累加、交叉轴取最大），**多帧 fragment 的帧间自动触发换区**（L294-296）——子内容跨了区域，stack 也跟着跨。
- [crates/typst-layout/src/stack.rs:L303-L370](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/stack.rs#L303-L370)——`finish_region`：fr 撑满、`Frame::soft` 产出、ruler 对齐（主轴取子对齐的 max）、负方向（RTL/自下而上）支持、`regions.next()` 推进并重置记账。
- [crates/typst-layout/src/stack.rs:L36-L53](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/stack.rs#L36-L53)——`StackLayoutChild::CustomLayouter`：允许列表等其他布局器复用 stack 引擎、为每项注入自定义布局函数（u4-l6 讲列表时会再遇到它）。

#### 4.4.4 代码实践

**实践：观察 padding 如何逐区域缩水（参数观察型实践）**

1. **实践目标**：验证「pad 把 padding 从**每一个**后续区域高度中扣除」，而不是只扣第一段。
2. **操作步骤**：
   - 写一个两页文档：`#pad(top: 50pt, bottom: 50pt)[<会溢出到第二页的长文本>]`。
   - 在 [crates/typst-layout/src/pad.rs:L26](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pad.rs#L26) 的 `let pod = regions.map(...)` 之后临时加一行（示例代码，观察后请还原）：
     ```rust
     eprintln!("[pad] pod = {:?}", pod);
     ```
   - `cargo run -p typst-cli -- compile pad.typ 2>pad-trace.txt`，查看 `pad-trace.txt`。
3. **需要观察的现象**：trace 里 Regions 的首个高度比页面内容区高度少 100pt（上下 padding），且 backlog/last 中每个高度都同样少 100pt。
4. **预期结果**：第二页的文本同样从 padding 之后开始——两页的内框位置一致，说明 `shrink_multiple` 对 last（重复区域）也做了缩放。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`#pad()` 为什么必须同时缩放 backlog 和 last，而不能只缩第一个区域？

**答案**：可断内容跨区域时，每个后续区域同样要为 padding 留出上下空间。若只缩第一个区域，第二页的内容会顶到页面边缘，前后页内框错位。`shrink_multiple` 对 size/full/backlog/last 逐项缩放正为此设计。

**练习 2**：`breakable_pod` 中 `Sizing::Auto` 与 `Sizing::Rel` 的处理路径有何本质区别？

**答案**：Auto 直接**继承**外层区域序列（first = 外层 size.y、backlog/last 原样）——块活在父区域的未来里，可以无限跨区域；固定高度则把总值**切分**进一个新序列且 `last = None`——总高度封顶，切完即止。

**练习 3**：stack 为什么要关闭子内容沿主轴的 expand？

**答案**：expand 表示「子内容应撑满区域」。若第一个子内容就沿主轴撑满整个区域，后续子内容将没有空间、fr 分配也会被破坏。stack 自己记账主轴（`used.main`、`fr`），子内容只负责「收缩贴合（shrink to fit）」，主轴的撑满决策统一收归 `finish_region`。

## 5. 综合实践

**任务：给一篇多次分页的长文档打点，绘出 region 分配序列**

这是本讲的总实践，把四个模块串起来：region 生命周期（看高度如何递减）、flow 主循环（看每页一轮）、distribute 协商（看换页触发点）、容器变换（可选加 pad 对照）。

1. **准备文档** `trace.typ`（用 8 个固定高度块 + 一个会自然跨页的长段落）：

   ```typst
   #for i in range(8) [
     #block(height: 150pt, fill: rgb("#eee"))[块 #i]
   ]
   = 附录
   这是一段足够长的文字，用于触发自然断行与跨页……
   （粘贴足量文本使段落跨到下一页）
   ```

2. **打点**（以下均为示例代码，记录输出后请还原，不要提交）：
   - 在 [crates/typst-layout/src/flow/mod.rs:L226](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/mod.rs#L226) 循环体开头（`compose` 调用前）加：
     ```rust
     eprintln!("[flow] region start: {:?}", regions);
     ```
   - 在 [crates/typst-layout/src/flow/distribute.rs:L400](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L400) `multi` 方法开头加：
     ```rust
     eprintln!("[multi] remaining = {:?}", self.regions.size.y);
     ```
   - 在 [crates/typst-layout/src/flow/distribute.rs:L369](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/flow/distribute.rs#L369) `single` 方法开头加同样一行（把 `[multi]` 换成 `[single]`）。

3. **运行**：`cargo run -p typst-cli -- compile trace.typ 2>trace.txt`，然后通读 `trace.txt`。

4. **整理分配序列**：把 trace 整理成如下形式的表（数值为示意，以本地输出为准）：

   ```
   第 1 页: 700.2pt → 550.2pt → 400.2pt → 250.2pt → 100.2pt(放不下 150pt,换页)
   第 2 页: 700.2pt → 550.2pt → 400.2pt → 250.2pt → (段落逐行消耗…) → 换页
   第 3 页: …
   ```

5. **对照验证**：
   - 与 4.1.4 的纸面演算对照：第 5 个块触发 `[flow] region start` 重新出现（换页）。
   - 找出段落的行级消耗：长段落应表现为大量小步递减（每行一减，来自 `Child::Line`），与 150pt 块的大步递减形成对比。
   - 在 trace 里找出至少一次 `[multi]` 的 spill 链：同一块在下一页开头被 `multi_spill` 续排。
   - 观察内省机制带来的重复：同一页的 region start 可能**成组重复出现**——这是 u4-l1 讲过的多轮迭代排版；统计每组重复次数即可估计实际布局轮数。
6. **预期结果**：一张清晰的「每页高度消耗曲线」；换页点与 `fits + may_progress` 判定吻合；所有数值随文档改动可复现。完整输出**待本地验证**。

## 6. 本讲小结

- **region 是带未来的剩余空间**：`Regions` 用 `size`（剩余）+ `full`（完整基准）+ `backlog`/`last`（后续高度队列）四个字段，把「当前还剩多少」与「换了会怎样」一并建模；页面流用 `Regions::repeat` 造出无限翻页的序列。
- **flow 三层流水线**：collect 预处理（段落提前断行成 `LineChild`）→ compose 组装单页（列/浮动体/脚注/行号）→ distribute 逐 Child 分配；`Work` 结构跨层传递未完成工作。
- **协商三原则**：放得下就放；放不下且 `may_progress()` 才换页；换页无意义就硬放溢出。可断块用 `MultiSpill` 余料续排，sticky 块用检查点迁移，widow/orphan 用 `need` 分组提前换页。
- **容器 = 区域变换**：`breakable_pod` 决定继承还是切分区域序列；pad 用 `map`+`shrink`/`grow` 成对缩放与还原（含逆运算公式）；stack 关闭子内容主轴 expand 并自管记账。
- **一切经 multi_layouter 接线**：`PAD_RULE`/`STACK_RULE`/`COLUMNS_RULE` 把容器变成带回调的 `BlockElem`，于是 stack/pad 天然获得 flow 的全部协商能力，排版形成经 `layout_fragment` 的递归结构。

## 7. 下一步学习建议

本讲补齐了「页面内部」的块级世界，接下来两个方向自然展开：

1. **u4-l5 行内排版**：`Child::Line` 的帧是从哪来的？阅读 `crates/typst-layout/src/inline/` 目录（collect → prepare → shaping → linebreak → finalize），看断行、字形整形与双向算法如何产出一行行 Frame。本讲 4.2 的「段落提前断行」正是它的入口。
2. **u4-l6 网格世界**：`grid.rs` 如何把「轨道协商」建立在本讲的 region 模型之上；`stack.rs` 的 `CustomLayoutChild`（L36-53）如何被列表复用——建议先读 `lists.rs` 再读 `grid.rs`。

巩固建议：把综合实践的 trace 留存，学完 u4-l7（内省）后重跑一次，观察 `query`/`counter` 如何让同一批 region start 成组重复——两讲对照能真正看懂「多轮迭代排版」。
