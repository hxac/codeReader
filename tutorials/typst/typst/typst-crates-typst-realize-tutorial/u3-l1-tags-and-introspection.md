# 标签与内省 TagElem

## 1. 本讲目标

本讲聚焦 typst-realize 中「标签（Tag）」这条贯穿整条具现化流水线的暗线。它是内省（introspection）系统能够工作的物理基础。

读完本讲，你应当能够：

- 说清 `Tag`、`TagElem`、`TagFlags` 三个数据结构各自的含义，以及它们与内省系统的关系。
- 解释一条 `Tag::Start` / `Tag::End` 是如何从「给元素加 label」一步步诞生、被 `TagElem` 三明治式夹住、最终被直推进 sink 的。
- 读懂 `finish_grouping` 里最复杂的一段逻辑：`before` / `within` / `after` 三个集合如何在分组裁剪时，把被边界劈开的标签对重新拉回到同一侧，保证 start/end 永远成对。

本讲是 u2-l7（分组生命周期）的下游：u2-l7 讲的是分组「怎么裁、怎么收尾」，本讲专门回答收尾时「标签怎么不被腰斩」。

## 2. 前置知识

在进入本讲前，你需要已经掌握以下概念（来自前置讲义）：

- **具现化（realization）**：把任意 content 树规整成扁平的 well-known 元素清单（u1-l1）。
- **`visit()` 调度流水线**：每个 content 按固定顺序尝试 8 步，任一命中即短路（u1-l3）。本讲关注的是第 1 步——`TagElem` 直推。
- **`prepare()`**：元素的首次准备，分配 location、跑 synthesize、materialize，最后 `mark_prepared`（u2-l4）。本讲关注其中「生成 start/end tag」这一步。
- **`finish_grouping`**：把分组范围 `[start, end)` 内的元素打包成一个分组元素（u2-l7）。本讲关注其中标签边界的调整逻辑。

**什么是内省？** Typst 的很多功能（`query`、`ref`、计数器、交叉引用、PDF 链接标注）都需要在排版完成之后，反过来查询「某个元素最终落在哪一页、哪个坐标」。但排版发生在具现化之后，元素早已被打散重组。为了让元素在排版后仍可被定位，Typst 在具现化阶段为每个「可定位元素」插入一对零宽度的隐形标记——`Tag::Start` 和 `Tag::End`——夹在元素内容的首尾。排版引擎记录这些标记的物理坐标，下一轮内省迭代时，`Introspector` 就能通过 `Location` 反查到元素位置。这就是「内省循环」存在的物理依据，而标签正是这个循环的锚点。

## 3. 本讲源码地图

本讲主要涉及两类文件：标签数据结构的定义（在 typst-library），以及标签在具现化流水线中的流转（在 typst-realize）。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | 标签的全部运行时行为：`visit` 直推、`prepare` 生成、`visit_show_rules` 三明治、`finish_grouping` 边界修复、`tag_set` / `to_tag` 工具函数。 |
| `crates/typst-library/src/introspection/tag.rs` | `Tag` 枚举、`TagFlags`、`TagElem` 三个数据结构的定义。 |
| `crates/typst-library/src/introspection/location.rs` | `LocationKey`——把 `Location` 转成可排序、可做集合键的类型，是边界修复逻辑里「配对」的依据。 |
| `crates/typst-library/src/foundations/content/mod.rs` | `set_location` / `is_prepared` / `mark_prepared` 等 content 生命周期方法，`prepare` 用它们判断与标记。 |

## 4. 核心概念与源码讲解

### 4.1 标签三件套：Tag / TagFlags / TagElem

#### 4.1.1 概念说明

要让一个元素在排版后能被「找到」，需要回答两个独立的问题：

1. **这个元素是谁？** —— 由 `Location`（一个全局唯一的 128 位标识）回答。
2. **这个元素的内容从哪里开始、到哪里结束？** —— 由一对 `Tag::Start` / `Tag::End` 在排版流里标出起止位置。

`Tag` 就是这对起止标记本身；`TagFlags` 描述这个标记附带的两类元信息（是否可内省、是否 tagged）；`TagElem` 则是把 `Tag` 包装成「content」，这样它就能像普通元素一样在 `visit()` 流水线里流动、最终被直推进 sink。

#### 4.1.2 核心流程

一个可定位元素的标签生命周期：

```text
元素带 label 或 is_locatable()
        │
        ▼
   prepare() 分配 Location
        │
        ▼
   生成 (Tag::Start(elem, flags), Tag::End(loc, key, flags))
        │
        ▼
   TagElem::packed(Tag::Start) ──┐
   visit(元素内容)               ├── visit_show_rules 三明治夹住
   TagElem::packed(Tag::End)  ──┘
        │
        ▼
   两个 TagElem 被 visit() 直推进 sink
        │
        ▼
   finish_grouping 保证这对不被分组边界劈开
        │
        ▼
   排版引擎记录 tag 坐标 → 下一轮内省可定位
```

关键不变量：**`Tag::Start` 与其配对的 `Tag::End` 共享同一个 `Location`**（同一个元素的位置）。这正是后面边界修复逻辑赖以「配对」的依据。

#### 4.1.3 源码精读

`Tag` 是一个两变体枚举。`Start` 携带元素本体与标志，`End` 携带 location、key 哈希与标志：

[crates/typst-library/src/introspection/tag.rs:L12-L24](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L12-L24) —— 定义起止两种标记。注意注释说明：key 哈希放在 `End` 而非 `Start`，纯粹是为了让两个变体大小均衡、压缩 `Tag` 的内存占用，没有语义原因。

`Tag::location()` 是配对的枢纽：无论 Start 还是 End，都能取出同一个 `Location`：

[crates/typst-library/src/introspection/tag.rs:L27-L34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L27-L34) —— `Start` 从内部元素取 location（注释强调内容**必须**有 location，否则 panic），`End` 直接返回自身携带的 location。两边得到同一个值。

`TagFlags` 只有两个布尔位：

[crates/typst-library/src/introspection/tag.rs:L46-L61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L46-L61) —— `introspectable` 表示该元素会被插入 `Introspector`（即可被 `query`），`tagged` 表示元素实现了 `Tagged`。`any()` 在两者任一为真时返回 true，`prepare` 用它决定要不要分配 location。

`TagElem` 把 `Tag` 包成 content。它的 `packed` 构造器顺便 `mark_prepared`，让 `TagElem` 自身不再被 prepare（避免标签又生成标签的死循环）：

[crates/typst-library/src/introspection/tag.rs:L63-L83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/tag.rs#L63-L83) —— `TagElem` 是 internal 元素，不能由用户构造（`Construct` 直接 bail）。

`LocationKey` 是边界修复逻辑里「集合元素」的类型——它给 `Location` 套上了 `Ord`，从而能放进 `ListSet` 做快速查重：

[crates/typst-library/src/introspection/location.rs:L152-L159](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/location.rs#L152-L159) —— 注释解释了为什么 `Location` 本身不实现 `Ord`（避免误用语义无意义的排序），而要用单独的 `LocationKey`。

#### 4.1.4 代码实践

**实践目标**：确认 `Tag::Start` 与 `Tag::End` 共享同一个 `Location`。

**操作步骤**：

1. 在 `crates/typst-library/src/introspection/tag.rs` 的 `Tag::location()` 里临时加一行日志，例如在函数开头插入 `eprintln!("tag location for {:?}", self);`。
2. 编译 typst（`cargo build --release -p typst-cli`，或按仓库根目录说明构建）。
3. 编译一份带 label 的文档：
   ```typst
   #heading<myhead>[标题]
   ```
4. 观察输出。

**需要观察的现象**：每个可定位元素应输出**两条** `tag location` 日志（Start 与 End），且两者的 `Location(...)` 数值相同。

**预期结果**：每个 locatable 元素产生成对、location 相同的两条日志。具体 location 数值与命中次数**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Tag::End` 不存元素本体，只存 `Location` 和 key 哈希？

**参考答案**：节省内存。`End` 标记的任务只是在排版流里标出「元素到这里为止」并让内省能反查 location，不需要元素的字段；而 `Start` 存元素本体是为了在内省迭代中重建元素状态。把沉重的元素本体只放在一个变体里，让 `Tag` 整体更小。

**练习 2**：`TagElem::packed` 为什么要 `mark_prepared`？

**参考答案**：`TagElem` 自身也是一个 content，会流过 `visit()`。如果不标记 prepared，它会被 `visit_show_rules` 当成普通元素处理，可能再为它生成 start/end tag，形成无限递归。标记 prepared 让它在流水线里被当作「已完成准备」直接放行。

---

### 4.2 visit() 中的 TagElem 直推

#### 4.2.1 概念说明

回顾 `visit()` 的 8 步调度（u1-l3）：TagElem 直推、kind 规则、show 规则、序列、样式、分组、过滤、兜底 push。**TagElem 被放在第 1 步、优先于一切**，这意味着：标签永远不参与 show 规则、不参与分组判定，它被原样、按到达顺序推进 sink。

这个设计是深思熟虑的：标签是排版后定位的锚点，必须保持与元素内容的相对顺序，不能被任何规则改写或吞并。

#### 4.2.2 核心流程

```text
visit(content):
  if content is TagElem:     ← 第 1 步，最高优先级
      sink.push((content, styles))
      return                  ← 立即短路，跳过后续 7 步
  ...（后续 kind/show/grouping/filter 等）
```

一个关键推论：**因为 TagElem 在分组判定之前就被短路推出，分组调度器 `visit_grouping_rules` 永远「看不见」TagElem**。于是标签不会以 Interrupt 的身份打断分组，而是无声地落在 sink 里、夹在分组元素之间。这恰恰是为什么后面 `finish_grouping` 必须单独处理标签边界——标签绕过了分组调度，却仍落在分组将要裁剪的 sink 区间内。

> 补充：从分组规则的 `effect` 看，`TagElem` 不匹配任何规则的 Trigger/Inner/Neutral，对全部规则都是 Interrupt。正因为它在调度器之前就被直推，这个 Interrupt 判定才不会误伤分组。

#### 4.2.3 源码精读

[crates/typst-realize/src/lib.rs:L247-L251](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L247-L251) —— `visit()` 开头的 TagElem 直推。注释「Tags can always simply be pushed」点明了标签的不透明性：它携带自己的样式链，直接落进 sink，不经过任何转换。

标签的样式链来自直推时的第二个参数 `styles`。在 4.3 节会看到，这个 `styles` 就是 `visit_show_rules` 调用 `visit(TagElem)` 时传入的外层样式链。

#### 4.2.4 代码实践

**实践目标**：统计一次具现化里 TagElem 被直推了多少次，验证「每个可定位元素 = 2 个 tag（Start + End）」。

**操作步骤**：

1. 在 `lib.rs` 的 TagElem 直推分支（L248 附近）加入计数日志：
   ```rust
   if content.is::<TagElem>() {
       eprintln!("[visit] TagElem pushed: {:?}", content.to_packed::<TagElem>().unwrap().tag);
       s.sink.push((content, styles));
       return Ok(());
   }
   ```
2. 编译后运行：
   ```typst
   = 标题一
   = 标题二<two>
   ```
3. 观察日志条数。

**需要观察的现象**：两个标题（默认都是 locatable）应各产生一对 Start/End。

**预期结果**：约 4 条 TagElem 日志（2 个标题 × 2 条/标题）。`heading<two>` 因为带 label，标签会进入内省集合；具体条数取决于哪些元素被判定为 locatable，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 TagElem 直推这一步移到 `visit_grouping_rules` 之后会发生什么？

**参考答案**：TagElem 会先撞上分组调度。由于它对任何规则都是 Interrupt，它会迫使正在进行的分组（如 PAR）立即 finish，把段落切碎；同时它还可能被 `visit_filter_rules` 之前的某些逻辑误处理。标签与内容的相对顺序和配对都会被破坏，内省定位将失效。所以直推必须排在最前。

---

### 4.3 prepare() 中 start/end tag 的生成与配对

#### 4.3.1 概念说明

标签不是凭空产生的，它诞生于 `prepare()`——元素的首次准备（u2-l4）。`prepare` 做四件事：分配 location、应用内置 show-set、跑 synthesize、materialize。本讲关注其中两件与标签直接相关的：**何时分配 location**，以及**何时生成 start/end 对**。

生成出来的一对 tag，并不由 `prepare` 自己推进 sink，而是返回给调用方 `visit_show_rules`，由后者用「三明治」结构夹住元素内容再分别直推。

#### 4.3.2 核心流程

```text
visit_show_rules(content):
    verdict → 得到 prepared 标志
    if !prepared:
        tags = prepare(content)        ← 返回 Option<(Tag, Tag)>
    ...
    # 三明治结构：
    if let Some(start) = tags.start:
        visit(TagElem(start))          ← 直推 Start（命中 4.2 的分支）
    visit_styled(元素内容)              ← 内容（可能触发分组）
    if let Some(end) = tags.end:
        visit(TagElem(end))            ← 直推 End
```

`prepare` 内部决定 location 与 tag 的逻辑：

```text
prepare(elem):
    flags = TagFlags {
        introspectable: elem.is_locatable() || elem 有 label || elem 已有 location,
        tagged: elem.is_tagged(),
    }
    if elem 还没 location 且 flags.any():
        elem.set_location(locator.next_location(...))   ← 分配唯一 location
    ...（show-set、synthesize、materialize）...
    tags = elem.location().map(|loc| (
        Tag::Start(elem.clone(), flags),
        Tag::End(loc, key, flags),
    ))
    elem.mark_prepared()
    return tags
```

两个要点：

- **并非所有元素都分配 location**。只有 `flags.any()`（可内省或 tagged）的元素才分配，普通文本等不分配，因此不产生 tag。这是性能优化（`next_location` 有开销）。
- **tag 的生成晚于 synthesize 与 materialize**，这样 `Tag::Start` 里克隆的元素包含了合成字段（如 figure.kind、编号），内省查询时字段齐全。

#### 4.3.3 源码精读

`prepare` 计算 `TagFlags` 并按需分配 location：

[crates/typst-realize/src/lib.rs:L546-L556](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L546-L556) —— `introspectable` 三选一：元素本身 locatable、带 label、或来自 query 已有 location。注释指出，来自 query 的元素可能「未 prepared 但已有 location」，所以这里用 `location().is_none()` 守卫，避免重复分配。`is_locatable` / `is_tagged` 来自元素的 vtable：

[crates/typst-library/src/foundations/content/element.rs:L88-L102](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/element.rs#L88-L102) —— 两者都是元素类型在 vtable 里登记的静态属性。

`set_location` 把 location 写进 content 的 meta：

[crates/typst-library/src/foundations/content/mod.rs:L142-L145](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/content/mod.rs#L142-L145)

在 synthesize 与 materialize 之后、`mark_prepared` 之前，生成 tag 对：

[crates/typst-realize/src/lib.rs:L575-L588](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L575-L588) —— 注意顺序：tag 生成在 synthesize/materialize 之后（带齐合成字段）、`mark_prepared` 之前（注释解释：这样被 query 的元素仍能让 show-set 规则在其上生效）。`mark_prepared` 把 lifecycle 位集的位 0 置 1，保证 prepare 只跑一次。

`visit_show_rules` 用返回的 tag 做三明治。先看 start tag 的直推：

[crates/typst-realize/src/lib.rs:L411-L415](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L411-L415) —— `tags.unzip()` 把 `Option<(Tag,Tag)>` 拆成两个 `Option<Tag>`。有 start 就包成 `TagElem::packed` 再 `visit`（命中 4.2 的直推分支）。

随后是「外层样式降低、show 深度检查、`visit_styled`」的中间层（这部分属 u2-l2/u2-l5），最后是 end tag 的直推：

[crates/typst-realize/src/lib.rs:L427-L430](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L427-L430) —— 内容访问完毕，直推 end tag。这样 Start、内容、End 三者在 sink 里严格相邻（除非内容内部又触发了分组收尾，这正是下一节要解决的问题）。

#### 4.3.4 代码实践

**实践目标**：观察 `prepare` 为带 label 的元素生成的 tag 对，以及三明治顺序。

**操作步骤**：

1. 在 `lib.rs` 的 `prepare` 返回前（L588 之前）加日志：
   ```rust
   if let Some((start, end)) = &tags {
       eprintln!("[prepare] generated tag pair for {:?} | start loc={:?} end loc={:?}",
           elem.elem().name(), start.location(), end.location());
   }
   ```
2. 在 `visit_show_rules` 的 start 直推处（L413）与 end 直推处（L428）各加一条 `eprintln!("[show] push start/end tag")`。
3. 编译运行：
   ```typst
   第一段文字 #box[盒子]<box1> 后续文字。
   ```
4. 观察三条日志的先后顺序。

**需要观察的现象**：对 `<box1>` 这个带 label 的 box，应依次出现 `prepare 生成 tag 对` → `push start tag` → （box 内容）→ `push end tag`，且 start/end 的 location 相同。

**预期结果**：顺序为 prepare → start → end，location 配对一致。普通文字不产生 tag 日志。具体 location 值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 tag 的生成必须晚于 synthesize？

**参考答案**：`Tag::Start` 会克隆元素本体供内省使用。若在 synthesize 之前克隆，合成字段（如 figure 的 kind、编号、caption 关联）尚未写回，内省迭代里 `query` 到的元素就会缺字段，`figure.where(kind: ...)` 之类的筛选与计数器都会出错。

**练习 2**：一段普通正文 `你好世界` 里的文字会产生 tag 吗？

**参考答案**：不会。`TextElem` 既不 locatable 也不 tagged、无 label、无 location，`flags.any()` 为 false，不分配 location，也就不生成 tag。只有显式可定位的元素（heading、figure、带 label 的元素等）才产生。

---

### 4.4 finish_grouping 中 tag 的跨边界纳入逻辑

#### 4.4.1 概念说明

这是本讲最精巧的部分，也是 u2-l7 留下的「尾巴」。

问题来源：当 `finish_grouping` 把 sink 里 `[start, end)` 这段元素打包成**一个**分组元素（比如 `ParElem`）时，它会调用 `Grouped::end()` 把这段截断、truncate 掉，然后 `visit` 新生成的分组元素。问题是：**如果一对 start/end tag 正好横跨这个截断边界——Start 在段内、End 在段外，或反之——这对就会被劈开**。劈开的 tag 对会让内省彻底失效（Start 记录了起点却找不到配对的 End）。

`finish_grouping` 的对策分两种，由规则的 `tags` 字段决定：

- **`tags: true`（只有 `PAR` 和 `TEXTUAL`）**：分组「关心」标签。运行 before/within/after 三集合逻辑，把横跨边界的 tag 对**拉回同一侧**，保证成对。
- **`tags: false`（`CITES`、`LIST`、`ENUM`、`TERMS`）**：分组「不关心」标签。把段内的 TagElem **剥离出来**、暂存，待分组打包完成后**重新 visit**，让标签重新落位、夹住整个分组元素（而不是被吞进某个列表项的 body）。

#### 4.4.2 核心流程

`rule.tags` 为真时的边界修复算法：

```text
1. 先按 effect 裁掉尾部非 Trigger 元素，确定初始 [start, end)。
2. 收集三个 location-key 集合：
     before  = start 之前紧邻的连续 tag 段的 location 集合
     within  = [start, end) 内所有 tag 的 location 集合
     after   = end 之后紧邻的连续 tag 段的 location 集合
3. 向左扩展 start：对 start 之前紧邻的每个 tag，
     若其 location ∈ within ∪ after，则把它纳入（start 左移）。
   —— 含义：这个 Start 的配对 End 在段内或段后，应一起留在分组里。
4. 向右扩展 end：对 end 之后紧邻的每个 tag，
     若其 location ∈ within ∪ before，则把它纳入（end 右移）。
   —— 含义：这个 End 的配对 Start 在段内或段前，应一起留在分组里。
```

直观理解：**只要一对 tag 有一只在分组范围内（含紧邻边界），就把另一只也拽进来**，绝不让它们被截断分离。三集合用 location-key 配对，因为 Start/End 共享同一 location。

`rule.tags` 为假时的剥离算法（一个就地读/写指针压缩）：

```text
k = start
for i in start..end:
    if sink[i] 是 TagElem:
        tags.push(sink[i])        ← 暂存，不保留在段内
        continue
    sink[k] = sink[i]; k += 1     ← 非 tag 元素左移压实
truncate(k)
... 执行 rule.finish（此时段内已无 tag）...
for (content, styles) in tags.chain(tail):
    visit(content, styles)         ← 把剥离的 tag 重新喂回流，夹住整个分组
```

#### 4.4.3 源码精读

先看函数开头：裁掉尾部非 Trigger 元素，得到初始 `end`：

[crates/typst-realize/src/lib.rs:L905-L907](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L905-L907) —— `trim_end_matches` 把尾部不是 Trigger 的元素（包括可能落在此处的 TagElem、SpaceElem 等）裁掉。

整个边界修复被 `if rule.tags` 守卫：

[crates/typst-realize/src/lib.rs:L915-L949](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L915-L949) —— 只有 `tags: true` 的规则进入这段。注释三句话概括了三种情况：段内/起边界开的 tag 若 End 在末边界则纳入；段内/末边界闭的 tag 若 Start 在起边界则纳入；夹在配对 Start/End 之间的 tag 也纳入。

PAR 专属的小优化：在算集合前，先把紧随末尾的 `SpaceElem` 抽掉（因为它们一定会被空格折叠干掉，提前移除可避免它们干扰 tag 边界判断）：

[crates/typst-realize/src/lib.rs:L922-L924](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L922-L924)

三集合的构造。注意 `before` 与 `after` 用 `map_while(to_tag)`——只取**紧邻**边界的连续 tag 段，遇到第一个非 tag 就停；`within` 用 `filter_map`——收集段内**所有** tag：

[crates/typst-realize/src/lib.rs:L927-L930](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L927-L930) —— `before` 反向遍历 `..start`，`after` 正向遍历 `end..`，两者都 `map_while`；`within` 对 `start..end` 用 `filter_map` 全收。

向左扩展 `start`（把配对在段内/段后的 Start 拽进来）：

[crates/typst-realize/src/lib.rs:L932-L939](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L932-L939) —— 逆序遍历 `..start`，遇到第一个非 TagElem 即 `break`（只处理紧邻的连续 tag 段）。命中 `within` 或 `after` 就把 `start` 左移到该 tag。

向右扩展 `end`（把配对在段内/段前的 End 拽进来）：

[crates/typst-realize/src/lib.rs:L942-L948](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L942-L948) —— 对称地，正序遍历 `end..`，命中 `within` 或 `before` 就把 `end` 右移。

随后 `tail` 暂存 `end..` 的元素并 truncate。接着是 `!rule.tags` 的剥离压实：

[crates/typst-realize/src/lib.rs:L954-L970](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L954-L970) —— 读指针 `i` 扫描，写指针 `k` 压实；TagElem 进 `tags` 暂存，非 tag 元素左移。这是就地算法，不分配新 sink。

最后执行 `rule.finish` 打包，再把暂存的 tags 与 tail 重新 visit：

[crates/typst-realize/src/lib.rs:L972-L978](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L972-L978) —— 对 `tags: false` 的规则，重新 visit 让标签夹住整个分组元素（如整个 `ListElem`），而不是埋进某个 item 的 body。

两个工具函数。`tag_set` 把一段 tag 收集成 `ListSet<LocationKey>`：

[crates/typst-realize/src/lib.rs:L985-L994](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L985-L994) —— 用 bump arena 分配。`ListSet` 在元素较少时线性查、超过阈值排序后二分查，兼顾小集合的常数开销与大集合的查找速度。

`to_tag` 把 `Pair` 转成可选的 `TagElem` 引用，供 `map_while` / `filter_map` 使用：

[crates/typst-realize/src/lib.rs:L997-L999](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L997-L999)

最后确认哪些规则关心标签：`TEXTUAL` 与 `PAR` 为 `tags: true`，三条列表规则与 `CITES` 为 `tags: false`：

[crates/typst-realize/src/lib.rs:L1018-L1020](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1018-L1020)（TEXTUAL）与 [L1044-L1046](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1044-L1046)（PAR）—— 两者 `tags: true`；[L1074-L1076](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1074-L1076)（CITES）与 [L1103-L1107](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1103-L1107)（list_like）—— `tags: false`。

#### 4.4.4 代码实践

**实践目标**：追踪 before/within/after 三个集合，以及 start/end 在边界修复前后的变化。

**操作步骤**：

1. 在 `lib.rs` 的 `finish_grouping` 里，三集合构造后（L930 之后、`if rule.tags` 块内）插入日志：
   ```rust
   eprintln!("[finish_grouping] rule={:p} start={start} end={end} \
       before={:?} within={:?} after={:?}",
       rule,
       before.iter().collect::<Vec<_>>(),
       within.iter().collect::<Vec<_>>(),
       after.iter().collect::<Vec<_>>());
   ```
   （`ListSet` 是否实现了直接 `Debug` 取决于其内部；若不便打印，可在 `tag_set` 里追加日志记录收集到的 location-key。）
2. 在两个扩展循环之后，再打印一次最终的 `start` / `end`：
   ```rust
   eprintln!("[finish_grouping] adjusted → start={start} end={end}");
   ```
3. 在 `!rule.tags` 的剥离分支（L956）里，打印被剥离的 tag 数量。
4. 编译运行一份混合内容，刻意制造分组边界与标签相邻的场景：
   ```typst
   #figure<fig>[
     一张图
   ]

   - 第一项
   - 第二项<item2>
   - 第三项
   ```
5. 观察日志。

**需要观察的现象**：

- 对 PAR 分组，当段落收尾时若边界附近有 tag，会看到 `adjusted → start/end` 与初始值不同（被拉宽）。
- 对 LIST 分组（`tags: false`），会看到剥离分支记录了被移出的 tag 数量，随后这些 tag 重新 visit、夹住整个 `ListElem`。

**预期结果**：`<item2>` 的 tag 不会被吞进第二项的 body，而是被剥离后重新落在整个列表之外，从而仍能被 `query` 正确定位。具体的 start/end 数值与集合内容**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `before` / `after` 用 `map_while`（只取紧邻边界的连续 tag 段），而 `within` 用 `filter_map`（全收）？

**参考答案**：扩展循环只能修复「紧贴边界」的 tag 对——如果边界与 tag 之间隔着普通元素，那对 tag 已经被元素自然隔开，不存在「被截断」的问题，无需也无力修复。所以 `before`/`after` 只看紧邻的连续 tag 段、遇到非 tag 即停。而 `within` 需要知道段内**所有** tag 的 location，才能判断边界外的 tag 是否有配对在段内，故用 `filter_map` 全量收集。

**练习 2**：列表规则 `tags: false` 的剥离会把标签「重新 visit」，这与 `tags: true` 的「拉入分组」有何本质区别？

**参考答案**：`tags: true`（PAR/TEXTUAL）希望标签留在分组**内部**、随 `ParElem.body` 一起排版（因为段落内的 inline 元素本就需要被 tag 夹住定位）。`tags: false`（列表/引用）则相反：标签若留在段内会被压实进某个列表项的 body，导致定位错乱；所以剥离后重新 visit，让标签落在整个分组元素之外、夹住整个 `ListElem`，使列表作为一个整体被定位。

**练习 3**：三集合用 `LocationKey` 而非 `Location` 做元素，除了排序需求，还有什么好处？

**参考答案**：`Location` 故意不实现 `Ord`（防止误用语义无意义的排序比较两个位置），无法放进需要排序的 `ListSet`。`LocationKey` 是对 `Location` 的薄封装、实现了 `Ord`/`Hash`，既满足 `ListSet` 的需求，又在类型层面把「可排序的位置键」与「语义位置」区分开，避免滥用。

## 5. 综合实践

把本讲三块知识串起来：完整追踪一个带 label 的块级元素（figure）在「两个段落之间」时的标签流转。

**任务**：

1. 准备文档：
   ```typst
   第一段：这里有一些文字。

   #figure<fig>[
     图的内容
   ]

   第二段：图后面的文字。

   见 @fig。
   ```
   （`@fig` 是交叉引用，会触发内省循环，让标签真正被使用。）
2. 综合打开三处日志：
   - `prepare`（4.3.4）：看 figure 的 tag 对何时生成。
   - `visit` 的 TagElem 直推（4.2.4）：看 Start/End 何时落进 sink。
   - `finish_grouping`（4.4.4）：看第一段 PAR 收尾时，figure 的 Start/End 如何落在 PAR 分组的 `after` 集合里、不被错误纳入段落。
3. 画出 sink 的演化示意：从 `[TagStart_fig, figure, TagEnd_fig]` 在两段之间的布局，标注每个 tag 最终落在哪个分组元素之外。

**需要观察与解释**：

- figure 是块级元素，对 PAR 是 Interrupt，所以它**不会被收进任何段落**；它的 tag 对作为独立元素直推、夹住 figure 自身，跨越段落边界而不被腰斩。
- `@fig` 交叉引用能正确解析，反证标签对完整存活。

**预期结果**：交叉引用 `@fig` 正常显示编号；日志显示 figure 的 tag 对始终成对、未被任何 PAR 分组吞并。具体编号与日志细节**待本地验证**。

## 6. 本讲小结

- `Tag`（Start/End）、`TagFlags`（introspectable/tagged）、`TagElem` 三件套共同构成内省的物理锚点；Start 与配对 End **共享同一个 Location**，这是后续配对修复的依据。
- `visit()` 把 `TagElem` 放在调度第 1 步**直推进 sink**，使标签绕过 show 规则与分组调度，既不被改写也不打断分组——但也因此埋下了「标签落在分组裁剪区间内」的隐患。
- 标签诞生于 `prepare()`：仅给 `flags.any()` 的可定位元素分配 location，在 synthesize/materialize 之后、`mark_prepared` 之前生成一对 tag；`visit_show_rules` 再用三明治结构把它们夹住元素内容。
- `finish_grouping` 用 `rule.tags` 二分处理标签：`tags: true`（PAR/TEXTUAL）跑 before/within/after 三集合逻辑，把横跨边界的 tag 对拉回同一侧；`tags: false`（列表/引用）剥离段内 tag、打包后重新 visit，让标签夹住整个分组元素。
- 贯穿全讲的两个不变量：**(a) TagElem 不参与分组判定；(b) Start/End 永远成对、不被分组边界劈开**。

## 7. 下一步学习建议

- **u3-l2（过滤规则与边界元素）**：继续看 `visit` 流水线里的过滤步骤，理解哪些元素会被静默丢弃，与标签的「绝不丢弃」形成对照。
- **u3-l5（与 layout/html/bundle/math 的集成）**：标签被推进 sink 之后，是排版引擎（`typst-layout`）消费它们、记录坐标并喂回 `Introspector` 的。阅读该讲可以看清 tag 离开 realize 之后的完整生命周期。
- **源码延伸**：阅读 `crates/typst-library/src/introspection/mod.rs` 中 `Introspector` 如何用 `Tag` 的坐标建立 location→position 映射，把本讲的「生成 tag」与「消费 tag」两端接上。
