# 列表 / 枚举 / 术语布局

## 1. 本讲目标

本讲聚焦 `typst-layout` 中三类「条目列表」的排版：无序列表（list）、有序枚举（enum）、术语表（terms）。它们在源码里几乎共享同一套机制——**不是各自写一个独立排版器，而是把每个条目当成一个自定义子排版器，复用栈布局 `layout_stack_internal` 来纵向堆叠**。

读完本讲你应当能够：

- 说清楚列表排版**为什么复用栈布局**，以及通过 `StackLayoutChild::CustomLayouter` 这条「自定义子排版器」通道如何接入。
- 描述 `ListLayouter` 的**四步管线**（测量 marker 宽度 → 必要时测量 body 宽度 → 生成每个条目的子排版器 → 交给栈排版）。
- 解释 **tight（紧凑）与 loose（宽松）列表**在 body 处理上的差别：`ParbreakElem::shared()` 的注入，以及它如何把文本「升级」为段落。
- 讲清 **`indent` / `body_indent` 如何与 marker 一起形成悬挂缩进**（hanging indent）。
- 理解 **marker 与 body 的垂直对齐**：默认的基线对齐 `baseline_align` 与用户显式对齐时的 `vertical_align`。
- 知道 **RTL（从右到左）方向**与 **`PdfMarkerTag`（PDF 无障碍标记）**在排版结果中是如何落地。

---

## 2. 前置知识

本讲是专家层讲义，承接 **u6-l5 栈布局 StackLayouter**。在继续之前请确认你已掌握：

- **栈布局（stack）**：沿主轴线性堆放子项的排版器，主轴 / 交叉轴经 `GenericSize` 抽象，支持绝对间距与 `Fr` 分数间距，`finish_region` 用 `ruler` 累积做主轴对齐。这些都在 u6-l5 讲过，本讲直接复用其结论。
- **`layout_fragment` / `layout_frame`**：本讲的条目排版反复调用这两个入口（参见 u1-l3）。`layout_frame` 强制单帧、`layout_fragment` 可跨区域断裂。
- **`Regions` / `Region`（pod）**：排版的通用画布抽象（u2-l2）。本讲会反复「削窄区域宽度再排 body」。
- **`Fragment` / `Frame`**：排版产出物（u2-l3）。列表最终返回 `Fragment`（一个条目可能跨页断裂为多帧）。
- **show 规则与 `multi_layouter`**：列表 / 枚举 / 术语三类元素，都是经由 `rules.rs` 把对应的 `*_RULE` 注册到 `Target::Paged`，再用 `BlockElem::multi_layouter(elem, crate::lists::layout_xxx)` 把排版函数挂进 flow 管线（参见 u7-l1、u7-l2）。

如果你对「`#[comemo::memoize]` 的 `*_impl` 模式」「Tracked/TrackedMut」还不熟，建议先回看 u2-l1，本讲不再展开。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/lists.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs) | 列表 / 枚举的排版主体。定义 `layout_list` / `layout_enum` 两个入口、共享数据结构 `ListLayouter` / `ItemContent`、单条目排版器 `ItemLayouter`，以及四步管线 `layout_items`。 |
| [src/stack.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs) | 栈布局。本讲重点是其「对外可复用」的部分：`StackLayoutChild` 枚举、`layout_stack_internal` 泛型入口、`layout_custom_layouter` 分支。 |
| [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs) | 三类元素的 show 规则注册：`LIST_RULE` / `ENUM_RULE` / `TERMS_RULE`。注意**术语表没有独立的 layouter 函数**，是在规则内联拼装。 |
| typst-library/src/pdf/accessibility.rs（兄弟 crate，仅参考） | `PdfMarkerTag` 元素的定义，及其 `ListItemLabel` / `ListItemBody` / `TermsItemLabel` / `TermsItemBody` 变体，用于给条目内容打上 PDF 无障碍结构标签。 |

> 心智模型一句话：**列表 = 「测量好的 marker + 缩窄后的 body」拼成一个条目帧 → 把所有条目帧交给栈纵向堆叠**。`ListLayouter` 负责「测量与对齐」，`stack` 负责「堆叠与跨页」。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**stack 复用通道**（4.1）、**ListLayouter 四步管线**（4.2）、**lists 入口与 tight/loose + 悬挂缩进**（4.3）、**marker/body 对齐 + RTL + PDF 标记**（4.4）。其中 4.1 对应 `stack` 最小模块，4.2 对应 `ListLayouter` 最小模块，4.3 / 4.4 对应 `lists` 最小模块。

### 4.1 列表如何复用栈布局：`StackLayoutChild::CustomLayouter`

#### 4.1.1 概念说明

为什么要复用栈？因为列表、枚举、术语表在「外形」上就是一条条纵向排列的条目——这正是栈布局（TTB 方向）做的事。但栈的常规子项是 `StackChild`（即 `Content`），而列表的每个条目**不是一段现成的 content，而是「先排 marker、再排 body、再做基线对齐」的一整套工序**，还需要从外层借用 `ListLayouter` 的测量结果（marker 宽度、body 宽度、是否基线对齐、是否 RTL）。

`Content` 作为 Typst 的值类型，**不能持有借用 / 生命周期参数**（它最终要被哈希、缓存、跨线程传递）。所以无法把「带 `&ListLayouter` 借用的排版闭包」塞进一个 `Content` 里再交给普通栈。解决方案是 `stack.rs` 暴露一个**更底层的泛型入口** `layout_stack_internal`，它的子项类型 `StackLayoutChild<'a, F>` 多了一个变体 `CustomLayouter(F)`，允许直接传一个**持有环境借用的闭包**作为子项。

#### 4.1.2 核心流程

```text
layout_stack(elem)          // 普通栈：子项都是 StackChild（Content）
   └─ layout_stack_internal(children = [...StackChild], dir, spacing)

layout_list / layout_enum   // 列表：子项是「自定义排版闭包」
   └─ layout_items(...)
        └─ layout_stack_internal(
               children = [ CustomLayouter(|engine, styles, regions| layout_item(...)),
                            CustomLayouter(...), ... ],   // 每个条目一个闭包
               dir = Dir::TTB,                              // 纵向
               spacing = Some(gutter),                      // 条目间距
           )
```

也就是说，**列表不重新发明「纵向堆叠 + 跨页 + Fr 间距」**，而是把这些机制全部委托给栈；列表自己只负责「一个条目内部怎么排」。

#### 4.1.3 源码精读

`StackLayoutChild` 的两个变体（[src/stack.rs:33-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L33-L44)）：`StackChild` 透传普通栈子项，`CustomLayouter(F)` 接受一个自定义排版闭包。注释明确写了这是「为列表等排版器复用栈布局」而设。

`layout_stack_internal` 的主循环对三类子项分别处理（[src/stack.rs:68-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L68-L125)）。`CustomLayouter` 分支（[src/stack.rs:113-120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L113-L120)）：先补上「延迟的条目间距 `deferred`」，再调用 `layouter.layout_custom_layouter(...)`。注意它与普通 `Block` 分支共享 `deferred = spacing` 的逻辑——**条目之间的 gutter（行距）正是栈的 spacing**。

闭包真正被调用、产出帧的地方在 `layout_custom_layouter`（[src/stack.rs:252-268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L252-L268)）：取 `AlignElem::alignment`、调用闭包拿到 `Fragment`、再交给统一的 `layout_fragment` 累积。这里**闭包返回的是 `Fragment`（可能多帧）**，所以一个超长条目能自然跨页，跨页逻辑完全由栈的 `finish_region` 接管。

#### 4.1.4 代码实践

**实践目标**：验证「列表条目 = 栈的自定义子项」这条链路，看清闭包何时被调用、返回的 Fragment 如何被栈消费。

**操作步骤（源码阅读型）**：

1. 打开 [src/lists.rs:275-303](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L275-L303)，找到 `cells` 的构造：每个条目都被包成 `StackLayoutChild::CustomLayouter(|engine, styles, regions| layout_item(...))`。
2. 追踪这个闭包：进入 [src/stack.rs:113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L113) 的 `CustomLayouter` 分支 → `layout_custom_layouter`（[src/stack.rs:253](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/stack.rs#L253)）→ 闭包体 `layout_item`（[src/lists.rs:374](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L374)）。
3. 可选（待本地验证）：在 `layout_custom_layouter` 的 `let fragment = layouter(...)?;` 之后临时插入一条 `eprintln!("list item frame count: {}", fragment.len());`，用 typst CLI 编译一个含多行条目的文档，观察每个条目产出的帧数（单行条目应为 1，跨页条目 >1）。

**需要观察的现象**：列表条目之间的垂直间距由 `layout_stack_internal` 的第三个参数 `spacing = Some(layouter.gutter.into())`（[src/lists.rs:296](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L296)）决定，而不是条目内部产生。

**预期结果**：列表的「纵向堆叠、条目间距、跨页断裂」全部复用栈；列表代码里不出现任何「遍历条目逐个 push 帧」的循环。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `layout_stack_internal` 改回普通 `layout_stack`（子项只能是 `Content`），列表排版会丢失什么能力？

> **答案**：丢失「在条目排版时借用 `&ListLayouter`（marker 宽度、body 宽度、对齐参数）」的能力，因为 `Content` 不能持有借用。基线对齐、悬挂缩进、marker 等宽对齐都依赖这些借用值，无法塞进纯 `Content` 子项。

**练习 2**：为什么条目间距用栈的 `spacing` 而不是在 `layout_item` 内部加一段顶部空白？

> **答案**：栈的 spacing 有「首项前不补、末项后不补、连续 spacing 折叠」等成熟语义（见 u6-l5 的 `deferred` 机制），且天然配合 `Fr` 分数间距与跨页。在条目内部手加空白会破坏这些语义、还会与栈的 ruler 对齐冲突。

---

### 4.2 ListLayouter 的四步排版管线

#### 4.2.1 概念说明

`ListLayouter` 是「**整个列表共享**的排版数据」：条目间距 `gutter`、列表缩进 `indent`、marker 与 body 间距 `body_indent`、是否基线对齐 `baseline_align`、是否 RTL `is_rtl`，以及两个**测量出来**的字段——所有 marker 中的最大宽度 `marker_width`（让 marker 们横向对齐）、必要时测出的 body 宽度 `body_width`（让 body 们在 `width: auto` 时也能横向对齐）。

`layout_items` 把「排版一个列表」拆成注释里写明的**四步**：先测 marker 宽，必要时测 body 宽，再把每个条目包成子排版器，最后交给栈。前两步是「**测量**」，用 `relayout()` 复用同一组 locator，确保测量和正式排版用的是同一批 `Location`——这是内省（query/counter）正确的前提。

#### 4.2.2 核心流程

```text
layout_items(layouter, items, ...):
  1. locator.split() → 给每个条目预分配 (marker_locator, body_locator)，全程复用
  2. marker_width  = measure_markers(...)      // 取所有 marker 帧宽度的 max，封顶 available_width
  3. 若「宽度无限」或「expand.x = false」(即 width:auto / 无法撑满):
        body_width = Some(measure_bodies(...)) // 取所有 body 宽度的 max，封顶 (available_width - marker_width)
  4. 把每个 item 包成 StackLayoutChild::CustomLayouter(→ layout_item)
     → layout_stack_internal(dir=TTB, spacing=gutter)
```

「测量」之所以单独成步，是因为 `marker_width` / `body_width` 是**跨条目对齐**的依据：要让第 1 项的 `•` 和第 10 项的 `10.` 左对齐，必须先知道最宽的 marker 占多宽，再让所有 marker 都按这个宽度排（`layout_marker` 用 `Regions` 的 `expand.x = true` 把 marker 强制成等宽，见 [src/lists.rs:386-392](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L386-L392)）。

#### 4.2.3 源码精读

`ListLayouter` 结构体定义（[src/lists.rs:179-201](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L179-L201)），其中 `marker_width` / `body_width` 注释标明「待后续计算」。`ListLayouter::new`（[src/lists.rs:206-230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L206-L230)）把 `indent` / `body_indent` 在构造时就 `resolve(styles)`。

四步总入口 `layout_items` 及其步骤注释（[src/lists.rs:233-304](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L233-L304)）。关键三处：

- locator 预分配并复用（[src/lists.rs:257-265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L257-L265)）：测量与正式排版用「同一对 locator」，靠 `marker_locator.relayout()` / `body_locator.relayout()` 在两处复刻身份（参见 u2-l4 关于 relayout 的说明）。
- 测 marker（[src/lists.rs:267-268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L267-L268)）：无条件执行。
- 测 body 的**条件**（[src/lists.rs:270-273](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L270-L273)）：`regions.size.x` 无限 或 `!regions.expand.x`（即列表无法撑满到页宽，典型是 `width: auto` 的 block）。注释（[src/lists.rs:339-342](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L339-L342)）解释：这两种情况下列表无法靠「撑满页宽」来对齐，于是改用「收缩到最宽条目」让条目彼此对齐。

`measure_markers`（[src/lists.rs:307-337](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L307-L337)）：逐条用 `crate::layout_frame` 排 marker，取宽度 `set_max`，最后 `.min(available_width)` 封顶（超宽 marker 不挤爆页面）。`measure_bodies`（[src/lists.rs:343-370](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L343-L370)）类似，封顶为 `available_width - marker_width`（marker 优先占其所需，body 拿剩余）。

#### 4.2.4 代码实践

**实践目标**：理解「为什么测 body 是有条件的」，以及测量值如何回流到单条目排版。

**操作步骤**：

1. 读 [src/lists.rs:270-273](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L270-L273) 的判定，写下两种触发 `body_width = Some(...)` 的场景：① block / page 的 `width: auto`（区域宽度无限）；② `expand.x` 为 false（列表不撑满）。
2. 追踪 `body_width` 如何被消费：进入 `ItemLayouter::new`（[src/lists.rs:440-457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L440-L457)）。当 `body_width` 为 `Some` 时，body 区域宽度被强制设为该值且 `expand.x = true`（撑满到对齐宽度）；为 `None` 时则从区域宽度里减去 `total_body_indent`。
3. 对照 [src/lists.rs:451-457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L451-L457) 写出 body 可用宽度的两种计算式。

**需要观察的现象**：在「能撑满页宽」的常规场景下 `body_width = None`，body 直接拿「页宽 − 缩进」；在 `width: auto` 下 `body_width = Some(最宽 body)`，所有 body 被拉齐到同一宽度。

**预期结果（待本地验证）**：可编写两份 Typst 文档对比——一份把列表放进默认（撑满）block，一份放进 `block(width: auto)[...]`——观察条目 body 的右边界是否分别「贴页宽」与「贴最宽条目」。

#### 4.2.5 小练习与答案

**练习 1**：`measure_markers` 用 `layout_frame`（而非 `layout_fragment`）排 marker，为什么？

> **答案**：marker（如 `•` 或 `1.`）是不可断裂的单帧内容，`layout_frame` 的「恰好一帧」契约正合适；且测量只关心宽度，`layout_frame` 更轻量（参见 u1-l3、u2-l3）。

**练习 2**：测量时用了 `marker_locator.relayout()`，正式排版又用同一个 `marker_locator.relayout()`。如果两次用了**不同**的 locator，会发生什么？

> **答案**：内省会把这个 marker 当成两个不同元素（两个 `Location`），导致 `query` / counter 重复计数或顺序错乱。复用同一 locator 并在两次都 `relayout()` 是为了「测量帧与正式帧共用身份」。这是 u2-l4 讲过的 locator 稳定性要求。

**练习 3**：`measure_bodies` 的封顶值是 `available_width - marker_width`，而不是 `available_width`，为什么？

> **答案**：marker 在前、body 在后，两者横向并排。marker 已先分得 `marker_width`，body 只能拿「剩余 = available − marker」，否则 marker + body 会超出页宽（参见 [src/lists.rs:364-369](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L364-L369) 注释）。

---

### 4.3 lists 入口：tight/loose 段落化与悬挂缩进

> 本模块对应本讲**核心实践任务**。

#### 4.3.1 概念说明

`layout_list` 与 `layout_enum` 是两个并列入口，结构几乎一致，差别只在「marker 怎么算」（list 用用户给的 `marker`，enum 用 `numbering` 把序号格式化）。两者都要处理三件事：

1. **tight / loose**：Typst 列表默认是 loose（宽松），条目 body 是**段落**（可自动折行、段间距生效）；`tight: true` 时是紧凑列表，body 视作**行内序列**（更接近「一行一项」）。两者的关键区别，就是是否给 body 末尾追加一个 `ParbreakElem`。
2. **序号 / marker 生成**：enum 还要处理 `start`、`reversed`、`full`（完整层级编号）、`parents`（父级编号栈）。
3. **挂 PDF 无障碍标记**：把 marker / body 包进 `PdfMarkerTag::ListItemLabel` / `ListItemBody`，供导出 tagged PDF 时建立 `L/Lbl/LBody` 结构树。

至于**术语表（terms）**——需要特别说明：**`typst-layout` 里没有 `layout_terms` 函数**。术语表不是通过 `multi_layouter` 挂一个 layouter，而是在 `rules.rs` 的 `TERMS_RULE` 里**内联**拼装成一个 `StackElem`（普通栈），再用 `separator`、`hanging_indent`、`pad` 等手段成形。这点和 list / enum 不同，详见 4.3.3 末尾。

#### 4.3.2 核心流程

```text
layout_list(elem, ...):
  读配置: indent, body_indent, tight, gutter(tight? leading : spacing), is_rtl, depth
  解析 marker, 算 baseline_align
  for each child:
      body = child.body.clone()
      if !tight: body += ParbreakElem::shared()   // ← tight/loose 的根本差别
      body = body.set(ListElem::depth, Depth(1))   // 嵌套深度 +1
      item = { marker: PdfMarkerTag::ListItemLabel(marker),
               body:   PdfMarkerTag::ListItemBody(body) }
  ListLayouter::new(gutter, indent, body_indent, baseline_align, is_rtl, ...)
  layout_items(...)   // → 4.2 的四步管线
```

**悬挂缩进（hanging indent）** 的形成，本质是「**body 在一个被缩窄的区域里排版，再把整块 body 平移到 marker 右侧**」：

```text
区域原始宽度 W
total_body_indent = indent + marker_width + body_indent
body 排版区域宽度 = W - total_body_indent        // body 被缩窄
body 帧左上角 x  = total_body_indent             // body 整体右移到 marker 之后
marker 帧左上角 x = indent                        // marker 顶在 indent 处
```

因为 body 的**所有行**（首行与续行）都在同一个缩窄区域里折行，再整体右移，所以续行会与首行的文字左对齐、而不是退回到 marker 那一列——这就是悬挂缩进。

#### 4.3.3 源码精读

`layout_list` 主体（[src/lists.rs:17-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L17-L74)）。注意 gutter 的推导（[src/lists.rs:29-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L29-L31)）：用户没给 `spacing` 时，tight 用段落 `leading`（行距，较紧），loose 用段落 `spacing`（段距，较松）。

**tight/loose 的核心差别**（[src/lists.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L48-L52)）：

```rust
let mut body = item.body.clone();
if !tight {
    body += ParbreakElem::shared();
}
```

注释 [src/lists.rs:48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L48) 写明「宽（loose）列表里的文本应总是变成段落」。`ParbreakElem` 是段落分隔符：body 末尾追加它，等价于「这块内容以一个段落结束」，从而让 realize 把 body 当作完整段落处理（应用首行缩进、段间距、两端对齐等段落级规则）。tight 列表不追加，body 就只是一串行内内容。

`layout_enum` 的对应逻辑完全一致（[src/lists.rs:141-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L141-L144)）。enum 额外的编号逻辑在 [src/lists.rs:95-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L95-L156)：`full` 模式把 `parents` 栈压入序号生成全路径编号（如 `1.2.3`）；非 full 的 `Pattern` 用 `apply_kth`、`Func` 用 `apply`；并给编号 `set(TextElem::overhang, false)`（[src/lists.rs:138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L138)）以避免末对齐时标点悬挂导致间距抖动。

**悬挂缩进的落点**在 `ItemLayouter::new`（[src/lists.rs:440-470](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L440-L470)）：

- `total_body_indent = indent + marker_width + body_indent`（[src/lists.rs:448](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L448)）。
- body 区域宽度减去 `total_body_indent`（[src/lists.rs:456](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L456)）。
- `body_offset.x = total_body_indent`（[src/lists.rs:467](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L467)），`marker_offset.x = indent`（[src/lists.rs:468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L468)）。

> **关于术语表（terms）**：`TERMS_RULE`（[src/rules.rs:162-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L162-L203)）没有调用任何 `crate::lists::layout_terms`（该函数不存在）。它直接在规则内：用 `unpad`（一个负 `hanging_indent` 的 `HElem`）把 term 标签往左推、用 `separator` 分隔 term 与 description、给 description 末尾按 tight/loose 追加 `ParbreakElem`（[src/rules.rs:186-188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L186-L188)）、包进 `PdfMarkerTag::TermsItemLabel` / `TermsItemBody`、最后组装成 `StackElem` 并 `.padded(padding)`。也就是说**术语表的「列对齐」靠的是 `padded` + 负 `hanging_indent` 的纯样式手段，而不是测量 marker 宽度**——这是它与 list/enum 在实现路线上的根本区别。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：亲手讲清两个问题——(a) tight 与 loose 在 body 处理上的差别；(b) `body_indent` 如何与 marker 共同形成悬挂缩进。

**操作步骤**：

1. **看 tight/loose 差别**。打开 [src/lists.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L48-L52)。记录：loose（默认）时 `body += ParbreakElem::shared()`，tight 时不加。
   - 追问：`ParbreakElem::shared()` 是什么？它是一个**共享单例**段落分隔符（零成本的 `&'static` content）。它的作用是「在这个位置结束当前段落」。body 末尾有它，意味着 body 会被 realize 当作「一个完整段落」处理。
   - 对照 gutter 推导（[src/lists.rs:29-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L29-L31)）：tight 用 `leading`（行距）、loose 用 `spacing`（段距）。所以 tight 列表条目之间更紧密、loose 更宽松，且 loose 的 body 会应用段落级排版（如首行缩进）。
2. **看悬挂缩进形成**。打开 [src/lists.rs:448](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L448) 与 [src/lists.rs:451-457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L451-L457)、[src/lists.rs:467-468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L467-L468)。按上文公式写下：
   - body 排版宽度 = `W − (indent + marker_width + body_indent)`
   - body 帧位置 x = `indent + marker_width + body_indent`
   - marker 帧位置 x = `indent`
3. **在 `finish` 里确认拼装**（[src/lists.rs:584-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L584-L632)）：每帧宽度 = `body_offset.x + body_frame.width()`（[src/lists.rs:590](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L590)），marker 只放进「第一个非空帧」（[src/lists.rs:619-624](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L619-L624)）。即：**只有条目的第一帧带 marker，后续帧（跨页续排）只有右移后的 body**——这正是悬挂缩进在跨页时仍保持的样子。

**需要观察的现象 / 预期结果（待本地验证）**：可写两段 Typst：

```typst
// loose（默认）：body 是段落
- 第一项，这是一段足够长的文字，用来观察折行后续行是否与首行文字左对齐（悬挂缩进）。
- 第二项。

// tight：body 是行内序列
#set list(tight: true)
- 紧凑项一
- 紧凑项二
```

预期：loose 列表里长条目折行后，续行缩进到 marker 右侧（与首行文字对齐），形成悬挂缩进；条目间距较大。tight 列表条目紧凑、间距小、body 不走段落规则。

#### 4.3.5 小练习与答案

**练习 1**：为什么 loose 列表要主动追加 `ParbreakElem`，而不是让 body 自然成为段落？

> **答案**：列表条目的 body 是任意 `Content`，可能是纯行内序列、也可能是多个 block。追加 `ParbreakElem::shared()` 强制「以段落收尾」，保证无论 body 内部结构如何，整块都被段落排版规则覆盖（首行缩进、对齐、折行）。tight 列表刻意不加，保持行内紧凑感。注释 [src/lists.rs:48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L48) 与 [src/lists.rs:140-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L140-L144) 都写了 "Text in wide lists/enums shall always turn into paragraphs"。

**练习 2**：列表元素的 `indent` 与 `body_indent` 分别影响什么？

> **答案**：`indent` 是整个列表相对父容器左边界的外缩进（marker 顶在这里，[src/lists.rs:468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L468)）；`body_indent` 是 marker 与 body 之间的额外间隙（[src/lists.rs:448](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L448) 中 `total_body_indent = indent + marker_width + body_indent`）。两者共同决定 body 的起始 x。

**练习 3**：术语表为什么没有像 list/enum 那样测量 marker 宽度？

> **答案**：术语表的 term 与 description 在**同一行**（被 `separator` 分隔），靠 `padded(padding)` + 负 `hanging_indent` 的 `HElem`（`unpad`）来实现「term 左凸、description 缩进」的版式，是纯样式手段，不需要几何测量。而 list/enum 的 marker 与 body 是**左右两列**关系，必须测出 marker 最大宽度才能让两列纵向对齐。

---

### 4.4 marker / body 的垂直对齐、RTL 与 PDF 无障碍标记

#### 4.4.1 概念说明

单条目内部（`layout_item` / `ItemLayouter`）要解决三个问题：

1. **垂直对齐**：marker 与 body 在垂直方向如何对齐？默认是**基线对齐**（marker 的基线与 body 第一行基线齐平），让 `•` 或 `1.` 坐在文字基线上；当用户显式设置了 `marker_align.y`（或 enum 的 `number_align.y`）时，改为「按指定高度重新排 marker」的 `vertical_align`。判定开关就是 `baseline_align = marker_align.y().is_none()`（[src/lists.rs:39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L39)）。
2. **RTL**：当文本方向是 `Dir::RTL` 时，marker 与 body 的左右关系要镜像。代码用 `is_rtl` 标志在 `finish` 里翻转 x 坐标。
3. **PDF 无障碍标记**：`PdfMarkerTag` 是一个 `Tagged` 元素（兄弟 crate typst-library 定义），它本身**不产生可见内容**，只把内层 content 打上结构标签，导出 tagged PDF 时用于构建无障碍结构树（`L`/`Lbl`/`LBody`）。列表用 `ListItemLabel` / `ListItemBody`，术语表用 `TermsItemLabel` / `TermsItemBody`。

#### 4.4.2 核心流程

```text
layout_item(item, list, ...):
  ItemLayouter::new(...)               // 算 total_body_indent、缩窄 body 区域、定 offsets
  marker = layout_marker(marker_width × base.y, expand=(true,false))   // 强制等宽 marker
  body   = layout_body(body_regions)    // 排 body（可多帧，即跨页）
  if baseline_align:  baseline_align(marker, body)   // 移 marker 或重排 body
  else:               vertical_align(marker, body)   // 按高度重排 marker
  finish(marker, body)                  // 拼装：marker 只入第一非空帧，body 右移
```

`finish` 里对每一帧（含跨页续排帧）都做：算总宽高、构造容器帧、按 `is_rtl` 决定 marker 与 body 的 x、仅在第 `first_frame` 帧放 marker。

#### 4.4.3 源码精读

`layout_item` 主流程（[src/lists.rs:374-403](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L374-L403)）：marker 用 `Region::new(Axes::new(marker_width, base.y), Axes::new(true, false))`（[src/lists.rs:386-392](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L386-L392)）——宽度强制撑满到 `marker_width`（等宽对齐）、高度不限、纵向不 expand。

`ItemLayouter` 结构体（[src/lists.rs:409-436](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L409-L436)）持有 `first_frame`（第一个非空帧下标）、`body_regions`、`body_offset`、`marker_offset`。

**基线对齐 `baseline_align`**（[src/lists.rs:511-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L511-L555)）：算 marker 与 body 第一帧的基线差 `diff = first.baseline() - marker.baseline()`。
- `diff >= 0`：marker 基线在 body 之上 → 把 marker 下移 `diff`（`marker_offset.y = diff`，不动 body）。
- `diff < 0`：marker 基线在 body 之下 → body 要下移，但下移会改变排版，于是**用更小的高度重排 body**（`regions.size.y += diff`，因 diff 为负即缩小，[src/lists.rs:547-549](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L547-L549)），再把结果下移 `-diff`（[src/lists.rs:551](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L551)）。注释 [src/lists.rs:542-546](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L542-L546) 坦承这是有限次迭代的近似。

**显式对齐 `vertical_align`**（[src/lists.rs:560-580](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L560-L580)）：取 body 第一帧高度（与 marker 高度取 max）作为 marker 的新高度，`expand=(true,true)` 重排 marker，让它按用户指定的 `align.y` 在该高度内垂直对齐（典型：让 `1.` 顶部对齐多行 body）。

**`finish` 拼装**（[src/lists.rs:584-632](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L584-L632)）：
- RTL 翻转（[src/lists.rs:602-625](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L602-L625)）：`is_rtl` 时 body 贴到帧左边界（`body_pos.x = 0`，[src/lists.rs:615](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L615)），marker 放到右侧（`marker_pos.x = width - (marker_width + marker_offset.x)`，[src/lists.rs:622](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L622)）。注释 [src/lists.rs:602-614](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L602-L614) 用数学推导说明「body 在 RTL 下向左扩展，故其左角须贴左边界」。
- marker 只入第一非空帧（[src/lists.rs:619-624](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L619-L624)）；`first_frame` 由 `should_skip_first_frame` 判定（[src/lists.rs:637-650](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L637-L650)）——若首帧只有 tag（强制分页产生的空帧），则 marker 改放到第 1 帧。

**`PdfMarkerTag`**（兄弟 crate，仅参考 [typst-library/src/pdf/accessibility.rs:312-371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/pdf/accessibility.rs#L312-L371)）：它是个 `Tagged` 元素，`kind` 标明结构角色（`ListItemLabel`/`ListItemBody`/`TermsItemLabel`/`TermsItemBody` 等），`body` 是真实 content。在 lists.rs 里通过 `PdfMarkerTag::ListItemLabel(marker)`（[src/lists.rs:56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L56)）/ `ListItemBody(body)`（[src/lists.rs:57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L57)）把条目两半包起来。它不影响几何（排版时仍按内层 content 测量），只在导出阶段被消费。

#### 4.4.4 代码实践

**实践目标**：区分两种垂直对齐模式，并验证 marker 只出现在第一帧。

**操作步骤**：

1. 读 [src/lists.rs:39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L39) 与 [src/lists.rs:396-400](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L396-L400)：`baseline_align` 为真走 `baseline_align`，否则走 `vertical_align`。enum 的开关在 [src/lists.rs:109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L109)（`number_align.y().is_none()`）。
2. 构造对比场景（待本地验证）：
   - 默认列表（不设 `marker_align`）：`baseline_align = true`，`•` 与文字基线齐平。
   - `#set list(marker-align: top)`：`marker_align.y() = Some(Top)` → `baseline_align = false` → 走 `vertical_align`，marker 被按 body 首帧高度重排、顶部对齐。
3. 追踪跨页：读 [src/lists.rs:618-624](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L618-L624)。确认条件 `if i == self.first_frame`——只有第一非空帧 `push_frame(marker_pos, marker)`，其余帧只放 body。

**需要观察的现象**：一个跨页的长条目，第一页条目顶部有 marker，第二页续排部分**没有** marker（只有右移后的 body 文字），且续行仍保持悬挂缩进。

**预期结果**：与日常所见 Typst PDF 列表跨页行为一致——marker 不重复出现。

#### 4.4.5 小练习与答案

**练习 1**：`baseline_align` 中 `diff < 0` 的分支为什么要**重排 body**而不是简单下移 marker？

> **答案**：`diff < 0` 表示 marker 基线在 body 之下（marker 比首行文字「低」）。若直接下移 marker 会把它推出条目顶部、与上方内容重叠；正确做法是把 body 下移。但 body 下移后总高度变大、可能需要重新折行，所以用「缩小 body 高度重排 + 下移结果」来近似对齐（[src/lists.rs:536-552](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L536-L552)）。注释也说明这是有限次迭代的近似，无法保证完美。

**练习 2**：`PdfMarkerTag::ListItemLabel(marker)` 会改变 marker 的排版宽度吗？

> **答案**：不会。`PdfMarkerTag` 是 `Tagged` 元素，排版时按其内层 `body`（即 marker content）测量几何，标签信息只在导出 tagged PDF 时用于结构树。所以 `measure_markers` 测出的宽度就是 marker 本身的宽度。

**练习 3**：RTL 列表里，为什么 `body_pos.x` 要被设成 `0` 而不是沿用 `body_offset.x`？

> **答案**：RTL 下条目向左扩展，帧的总宽 = `body_offset.x + body_frame.width()`，body 应贴在帧的**左**边界（向左扩展），故 `body_pos.x = 0`；marker 则被推到帧右侧。注释 [src/lists.rs:602-614](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L602-L614) 给出了等价数学推导：`width - (body_frame.width() + body_offset.x)` 在 body 贴左时化简为 0。

---

## 5. 综合实践

把本讲四条主线串起来，完成一次「**带注释的列表排版追踪**」。

**任务**：给定下面这段 Typst 源（一个含嵌套、含长 body、含 tight 设置的列表），手动追踪它在 `typst-layout` 内的完整调用链，并写出每一步的关键决策。

```typst
#set list(indent: 10pt, body-indent: 8pt)
- 顶层条目 A，这段文字较长，足以折行，请观察续行是否悬挂缩进到 body 起点。
  - 嵌套条目（depth +1）
- 顶层条目 B
#set list(tight: true)
- 紧凑项 X
- 紧凑项 Y
```

**要求画出 / 写出**：

1. **入口分派**：`LIST_RULE`（[src/rules.rs:124-141](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L124-L141)）如何把元素经 `BlockElem::multi_layouter` 挂到 `layout_list`；tight 时还在前面插了一个弱 `VElem`（[src/rules.rs:131-138](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/rules.rs#L131-L138)）。
2. **tight/loose 判定**：第一个列表 loose → body 加 `ParbreakElem`、gutter 取 `spacing`；第二个列表 tight → 不加、gutter 取 `leading`（[src/lists.rs:29-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L29-L31)、[src/lists.rs:48-52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L48-L52)）。
3. **嵌套深度**：外层条目的 body 用 `body.set(ListElem::depth, Depth(1))`（[src/lists.rs:53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L53)），嵌套子列表的 marker 解析会读到 `depth=1` 从而选不同层级 marker（[src/lists.rs:34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L34)、[src/lists.rs:40-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L40-L44)）。
4. **悬挂缩进数值**：代入 `indent=10pt`、`body_indent=8pt`、设测得 `marker_width=6pt`，写出 `total_body_indent = 24pt`，body 排版宽度 = `W − 24pt`，body 帧 x = `24pt`，marker 帧 x = `10pt`（[src/lists.rs:448](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L448)、[src/lists.rs:467-468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L467-L468)）。
5. **栈复用**：所有条目经 `layout_stack_internal(dir=TTB, spacing=gutter)` 纵向堆叠（[src/lists.rs:293-303](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/lists.rs#L293-L303)）；条目间距、跨页全部由栈承担。

**预期产出**：一张标注了「测量 → 缩窄 body → 基线对齐 → 拼装单条目帧 → 栈堆叠」五个阶段的流程草图，以及上述 5 项的具体数值 / 代码位置。若本地有 typst CLI，可编译该文档对照 PDF 截图验证悬挂缩进与 tight/loose 间距差异（待本地验证）。

---

## 6. 本讲小结

- **列表 = 复用栈布局**：list / enum 把每个条目包成 `StackLayoutChild::CustomLayouter` 闭包，经 `layout_stack_internal(dir=TTB)` 纵向堆叠；条目间距、跨页、Fr 间距全部由栈承担，列表自己只管「单条目内部」。
- **四步管线**：`layout_items` 先测 marker 宽（无条件）、必要时测 body 宽（仅 `width:auto` / 不撑满时）、再把每条目包成子排版器、最后交给栈；测量与排版复用同一组 locator 以保证内省正确。
- **tight / loose 的根本差别**：loose 给 body 追加 `ParbreakElem::shared()` 使其成为段落（应用段落级排版、gutter 用 `spacing`）；tight 不加（行内序列、gutter 用 `leading`）。
- **悬挂缩进来自「缩窄 + 右移」**：body 在 `W − (indent + marker_width + body_indent)` 的窄区域里折行，再整体平移到 `total_body_indent` 处；续行因此与首行文字左对齐，marker 只出现在第一非空帧。
- **两种垂直对齐**：默认 `baseline_align`（marker 与首行基线齐平，必要时重排 body 近似对齐）；用户设了 `marker-align.y` 时走 `vertical_align`（按 body 首帧高度重排 marker）。
- **术语表是例外**：没有 `layout_terms` 函数，`TERMS_RULE` 在规则内用 `padded` + 负 `hanging_indent` 的纯样式手段拼成 `StackElem`；`PdfMarkerTag`（`ListItemLabel/Body`、`TermsItemLabel/Body`）只服务于 tagged PDF 无障碍结构，不影响几何。

---

## 7. 下一步学习建议

- **u7-l1 / u7-l2（规则注册）**：本讲多次提到 `LIST_RULE` / `ENUM_RULE` / `TERMS_RULE` 与 `BlockElem::multi_layouter`，下一单元会系统讲 `register` 如何把 `*_RULE` 挂到 `Target::Paged`，以及 `multi_layouter` / `single_layouter` 的挂载差别——这是列表接入 flow 管线的真正入口。
- **回看 u6-l5（栈布局）**：若你对 `finish_region` 的 ruler 对齐、Fr 分配仍有疑问，建议重读；本讲的「条目堆叠」完全建立在其上。
- **延伸阅读**：对照 [src/grid/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/grid/mod.rs) 中表格单元格的 `PdfMarkerTag` / tag 注入（u6-l1），理解「无障碍标记 + 内省 tag」在不同 layouter 中的统一处理思路。
- **动手验证**：本讲多处标注「待本地验证」，建议安装 typst CLI，用第 5 节的文档实测 tight/loose、悬挂缩进、RTL、marker-align 的效果，把源码结论与实际渲染对应起来。
