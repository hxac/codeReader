# 链接与目的地址

## 1. 本讲目标

在 Typst 排版产物里，链接（超链接、目录跳转、脚注回链、跨文档引用）最终都要落到 PDF 的「链接注记（link annotation）」上。本讲聚焦 `typst-pdf` 如何把这些链接翻译成 krilla 能理解的形态。

学完本讲你应当能够：

- 说出 `Destination` 的三种变体（`Url` / `Position` / `Location`）各自走哪条转换路径，最终变成 krilla 的哪种 `Target`。
- 描述 `Destination::Location` 的三级回退顺序：`link_resolver`（跨文档）→ `loc_to_names`（命名目的地址）→ `introspector.position`（实时定位）。
- 解释 `collect_named_destinations()` 为什么要在导出页面之前先把「带标签的标题」预注册成命名目的地址。
- 理解 `pos_to_xyz()` 如何做页号重映射、为什么要给 y 坐标减 10pt。
- 看懂 `bounding_box()` 如何从当前变换状态算出点击矩形，以及 PDF/UA 下多行链接为什么用 quadpoints 合并而非拆成多个注记。

本讲只讲「链接」这一条支线；背景填充、文字、图形等其它 `FrameItem` 的翻译已在 u3 系列讲过，tagged PDF 子系统的完整机制留待 u5 深入。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（这些都在依赖讲义里讲过）：

- **Frame 树与遍历器**（u2-l7）：`handle_frame()` 按 `FrameItem` 六变体分派，其中 `FrameItem::Link(dest, size)` 就是本讲的入口。遍历过程中维护着一条 `FrameContext.states` 变换栈，记录当前累计的 `Transform`。
- **`State` 与变换**（u2-l7）：`fc.state().transform()` 给出「当前绘制点在页面坐标系下的变换」。本讲的链接矩形正是用它算出来的。
- **页号转换**（u2-l6）：当 `PdfOptions.page_ranges` 排除部分页面时，`PageIndexConverter` 负责把「Typst 页索引」重映射为「PDF 页索引」，被排除的页查不到。本讲里 `pos_to_xyz()` 会用到它。
- **`convert()` 编排**（u2-l5）：`collect_named_destinations()` 和 `GlobalContext`（含 `loc_to_names`、`link_resolver`、`page_index_converter`）都是在 `convert()` 里构造的。

几个 PDF 术语先说清楚：

- **目的地址（destination）**：PDF 里描述「跳到哪一页的哪个坐标」的结构。最常见的是 **XYZ destination**，即「跳到第 N 页、坐标 (x, y)、缩放因子 Z」。
- **命名目的地址（named destination）**：给某个目的地址起一个字符串名字，链接通过名字引用它。好处是名字稳定、可跨文档复用。
- **链接注记（link annotation）**：PDF 里一个可点击区域（由矩形或四边形 quadpoints 描述），绑定到一个 target（URL 或目的地址）。
- **quadpoints**：PDF 用 4 个点（一个四边形）精确描述一块点击区域。当链接跨多行时，一个注记可挂多个 quadpoints，比单个包围矩形更精确。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/link.rs` | 链接翻译的全部逻辑：`handle_link()` 入口、`LinkAnnotation`/`LinkAnnotationKind` 数据模型、`bounding_box()` 矩形计算、`pos_to_xyz()` XYZ 目的地计算。 |
| `src/convert.rs` | 提供 `FrameContext`/`GlobalContext`/`PageIndexConverter`，以及 `collect_named_destinations()` 预收集命名目的地址；`FrameItem::Link` 在 `handle_frame()` 里分派到 `handle_link`。 |
| `src/tags/mod.rs` | `add_link_annotations()` 把收集到的注记真正写入页面；`disabled()` 控制 tagged PDF 是否启用。 |
| `crates/typst-library/src/model/link.rs` | 定义上游的 `Destination` 枚举（输入）与 `ResolvedLink`（跨文档链接解析结果）。 |

## 4. 核心概念与源码讲解

### 4.1 链接注记的数据模型：LinkAnnotation 与 LinkAnnotationKind

#### 4.1.1 概念说明

`typst-pdf` 并不是在遍历到链接时就立刻调用 krilla 把注记写进页面。它的做法是：遍历 Frame 树时，先把每个链接「收集」成一个中间结构 `LinkAnnotation`，存进 `FrameContext`；等整页内容画完，再统一交给 `tags::add_link_annotations()` 一次性写出去。

这样做有两个好处：

1. **多行链接可合并**：一段跨行的链接文字，在 Frame 树里会被拆成多个 `FrameItem::Link`，每个只覆盖一行。先把它们收集起来，才能决定是合并成一个带 quadpoints 的注记，还是拆成多个注记。
2. **与 tagged PDF 解耦**：注记需要知道自己在逻辑结构树里挂哪个 `Link` 组，这依赖 tags 子系统的状态。先收集、后发射，把两件事分开。

#### 4.1.2 核心流程

```
遍历到 FrameItem::Link
        │
        ▼
handle_link() 算出 target（URL / 目的地）
        │
        ▼
bounding_box() 算出当前这一行的点击矩形 rect
        │
        ▼
按 group_id 归档进 FrameContext.link_annotations
        │
        （整页画完后）
        ▼
tags::add_link_annotations() 统一写入 krilla 页面
```

`LinkAnnotation` 的字段正好对应「一个注记需要的全部信息」。

#### 4.1.3 源码精读

数据结构定义在 [src/link.rs:15-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L15-L21)：

```rust
pub(crate) struct LinkAnnotation {
    pub kind: LinkAnnotationKind,  // 这条注记属于 tagged 还是 artifact
    pub alt: Option<String>,       // 无障碍替代文本（屏幕阅读器朗读）
    pub span: Span,                // 源码位置，用于错误定位
    pub rects: Vec<kg::Rect>,      // 点击区域：单元素=矩形，多元素=quadpoints
    pub target: Target,            // 跳转目标（URL 或目的地址）
}
```

`kind` 区分两类注记，见 [src/link.rs:23-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L23-L28)：

```rust
pub(crate) enum LinkAnnotationKind {
    /// 一个位于 `Link` 结构元素内部的链接注记。
    Tagged(AnnotationId),
    /// 一个位于 artifact（装饰性内容）内部的链接注记。
    Artifact,
}
```

- `Tagged(AnnotationId)`：这条注记属于逻辑结构树里某个 `Link` 元素，`AnnotationId` 是 tags 子系统预分配的编号，后续用于把注记和结构元素关联起来（无障碍场景需要）。
- `Artifact`：链接落在装饰性内容（artifact）里，不参与结构树。

`rects` 是一个 `Vec`，长度为 1 时表示单矩形，长度大于 1 时表示跨行的 quadpoints——这个区分在 4.5 节的发射逻辑里会用上。

这些收集好的 `LinkAnnotation` 存在 `FrameContext.link_annotations` 里，它按 `GroupId` 归集（同一个逻辑链接的所有片段共享一个 group），见 [src/convert.rs:226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L226)：

```rust
link_annotations: IndexMap<GroupId, SmallVec<[LinkAnnotation; 1]>, FxBuildHasher>,
```

#### 4.1.4 代码实践

**实践目标**：理解「先收集、后发射」的设计。

**操作步骤**：

1. 打开 [src/convert.rs:168-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L168-L169)，找到 `convert_pages()` 末尾把注记交给 krilla 的两行：

   ```rust
   let link_annotations = fc.link_annotations.into_values().flatten();
   tags::add_link_annotations(gc, &mut page, link_annotations);
   ```

2. 确认 `handle_link()` 全程只是往 `fc` 里 `push_link_annotation`，没有任何直接写页面的调用。

**需要观察的现象**：注记的收集发生在 `handle_frame` 遍历过程中，发射发生在 `surface.finish()` 之后、整个页面收尾阶段。

**预期结果**：你会发现链接注记的「生成」与「写入」在时间上是分离的——这正是为了支持多行合并与结构树关联。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rects` 设计成 `Vec<kg::Rect>` 而不是单个 `kg::Rect`？

> 参考答案：因为一个逻辑链接可能跨多行，每行各算出一个矩形。把这些矩形收集进同一个 `LinkAnnotation`，发射时就能用 quadpoints 把多行精确合并成一个注记；而单行链接的 `rects` 长度为 1，退化为普通矩形注记。

**练习 2**：`LinkAnnotationKind::Tagged` 里的 `AnnotationId` 是在什么时刻被分配的？

> 参考答案：在 4.5 节会看到，当需要新建一条 tagged 注记时，`handle_link` 会调用 `gc.tags.annotations.reserve()` 预占一个 `AnnotationId`，再把它塞进 `LinkAnnotationKind::Tagged`。随后 `add_link_annotations` 写入注记时，用 `page.add_tagged_annotation` 拿到真正的注记标识并调用 `gc.tags.annotations.init` 回填。

---

### 4.2 入口与分派：handle_link() 把 Destination 翻译成 Target

#### 4.2.1 概念说明

`handle_link()` 是本支线的唯一入口，在 `handle_frame()` 遇到 `FrameItem::Link` 时被调用。它接收到的是 Typst 上游的 `Destination`，需要翻译成 krilla 的 `Target`。

先看输入类型 `Destination`（定义在上游 [crates/typst-library/src/model/link.rs:295-302](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L295-L302)）：

```rust
pub enum Destination {
    Url(Url),                  // 外部网址
    Position(PagedPosition),   // 一个已确定的「第几页哪个坐标」
    Location(Location),        // 一个待解析的文档内位置（最常见）
}
```

而 krilla 的 `Target` 有两大类：

- `Target::Action(Action::Link(LinkAction))`：触发一个动作——对链接而言就是「打开一个 URI」。
- `Target::Destination(...)`：跳到一个目的地址，细分为 `Xyz`（精确坐标）和 `Named`（按名字查找）。

`handle_link` 的核心任务就是按 `Destination` 的三种变体，选择正确的 `Target` 形态。其中 `Location` 最复杂，因为它「还没被解析成坐标」，需要一条三级回退链。

#### 4.2.2 核心流程

```
Destination::Url(u)
    └─ Target::Action(LinkAction(u))            ← 直接做成 URI 动作

Destination::Position(p)
    └─ pos_to_xyz(p)  ──失败(None)─▶ 静默跳过（返回 Ok）
        └─成功─▶ Target::Destination(Xyz)

Destination::Location(loc)
    ├─ ① link_resolver.resolve(loc) 是 Cross？
    │      └─ into_relative_uri ─▶ Target::Action(LinkAction)   ← 跨文档链接
    ├─ ② gc.loc_to_names 含 loc？
    │      └─ Target::Destination(Named)                        ← 命名目的地址
    └─ ③ introspector.position(loc) ─▶ pos_to_xyz ─▶ Target::Destination(Xyz)  ← 实时定位
```

`Location` 的三级回退是本模块的重点，每级都对应一种「链接还没解析完」的情形：

1. **跨文档（bundle 导出）**：`pdf_in_bundle()` 时会传入 `link_resolver`。若它把该 location 解析成 `ResolvedLink::Cross`（指向另一个文档），就把 `from`/`to`/`anchor` 转成相对 URI（见 [crates/typst-library/src/model/link.rs:724-748](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/link.rs#L724-L748) 的 `into_relative_uri`），做成一个打开 URI 的动作。源码注释也说明：krilla 暂不支持 Go-To Remote / Launch 动作，所以统一降级成 Link 动作。
2. **命名目的地址**：`loc_to_names` 是一张 `Location → NamedDestination` 表，由 4.3 节的 `collect_named_destinations()` 预先填好。命中即用按名字跳转。注释特别强调：「已注册的命名目的地址一定不会指向被排除的页」，所以这条路径对 `page_ranges` 是安全的。
3. **实时定位兜底**：若前两级都不命中，就向 introspector 查询该 location 在排版结果里的实际 `PagedPosition`，再走和 `Position` 一样的 `pos_to_xyz`。注释里留了 TODO，说这种情况目前静默跳过，未来可能改为报错或告警。

#### 4.2.3 源码精读

入口签名与三路分派在 [src/link.rs:30-75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L30-L75)：

```rust
pub(crate) fn handle_link(
    fc: &mut FrameContext,
    gc: &mut GlobalContext,
    dest: &Destination,
    size: Size,
) -> SourceResult<()> {
    let target = match dest {
        Destination::Url(u) =>
            Target::Action(Action::Link(LinkAction::new(u.to_string()))),
        Destination::Position(p) => {
            let Some(dest) = pos_to_xyz(&gc.page_index_converter, *p) else {
                return Ok(());   // 页被排除，静默跳过
            };
            Target::Destination(krilla::destination::Destination::Xyz(dest))
        }
        Destination::Location(loc) => {
            // ① 跨文档
            if let Some(resolver) = gc.link_resolver
                && let Some(kind @ ResolvedLink::Cross { .. }) = resolver.resolve(*loc)
                && let Ok(uri) = kind.into_relative_uri()
            {
                Target::Action(Action::Link(LinkAction::new(uri.into())))
            // ② 命名目的地址
            } else if let Some(nd) = gc.loc_to_names.get(loc) {
                Target::Destination(krilla::destination::Destination::Named(nd.clone()))
            // ③ 实时定位兜底
            } else {
                let pos = gc.document
                    .introspector()
                    .position(*loc)
                    .unwrap_or(PagedPosition::ORIGIN);
                let Some(dest) = pos_to_xyz(&gc.page_index_converter, pos) else {
                    return Ok(());
                };
                Target::Destination(krilla::destination::Destination::Xyz(dest))
            }
        }
    };
    // ……（后续计算 rect、收集注记，见 4.5）
```

注意两点细节：

- `Position` 和兜底的 `Location` 都调用 `pos_to_xyz`，且都对其返回 `None`（页被排除）的情况 `return Ok(())`——也就是**静默丢弃**指向被排除页的链接，而不是报错。
- `Destination::Url` 完全不经过目的地址系统，直接成为 URI 动作。这也意味着 URL 链接天然不受 `page_ranges` 影响。

`handle_link` 的调用点在 `handle_frame` 的分派里，见 [src/convert.rs:371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L371)：

```rust
FrameItem::Link(dest, size) => handle_link(fc, gc, dest, *size)?,
```

传入的 `size` 就是这一段链接文字的本地尺寸，4.5 节用它算矩形。

#### 4.2.4 代码实践

**实践目标**：追踪一个指向文档内标题的链接（`Destination::Location`），写出它的回退顺序。

**操作步骤**：

1. 打开 `handle_link` 的 `Location` 分支，确认三级 `if/else if/else`。
2. 回答：在「单文档独立导出（`pdf()`）」场景下，`gc.link_resolver` 是什么值？

**需要观察的现象**：单文档导出时 `link_resolver` 为 `None`（u1-l2 已讲过），所以第一级 `if let Some(resolver)` 直接短路为假，必然跳到第二级 `loc_to_names`。

**预期结果**：对一个指向带标签标题的链接，因为标题已被预注册进 `loc_to_names`（4.3 节），第二级命中，target 为 `Target::Destination(Named(...))`。只有当目标 location 既不是跨文档、又没被预注册时，才会落到第三级实时定位。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Destination::Position` 和 `Location` 的兜底分支都要对 `pos_to_xyz` 返回 `None` 做 `return Ok(())`，而不是报错？

> 参考答案：当用户用 `page_ranges` 只导出部分页面时，链接目标可能落在被排除的页上。此时 `pos_to_xyz` 因 `pdf_page_index` 查不到而返回 `None`。链接无法指向不存在的 PDF 页，与其报错中断整个导出，不如静默丢弃这条链接。命名目的地址路径则被设计成「预收集时就只收录被导出页」，所以天然不会遇到这个问题。

**练习 2**：如果一条 `Destination::Url` 指向的网址里含空格或中文，`handle_link` 会做什么特殊处理吗？

> 参考答案：不会。`handle_link` 直接 `u.to_string()` 塞进 `LinkAction`，不做任何编码。这是因为上游 `Url` 类型在构造时已经保证了合法性，编码责任不在 typst-pdf 这一层。

---

### 4.3 命名目的地址的预收集：collect_named_destinations()

#### 4.3.1 概念说明

`Destination::Location` 的第二级回退依赖一张表 `gc.loc_to_names`。这张表把「文档内的某个 Location」映射到「一个命名目的地址」。问题是：这张表从哪来？它必须在导出页面**之前**就准备好，否则 `handle_link` 查不到。

答案是 `collect_named_destinations()`——它在 `convert()` 里排在 `convert_pages()` 之前执行（见 [src/convert.rs:68-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L68-L73)），专门做两件事：

1. 找出所有「值得被命名」的位置（显式锚点 anchors + 带标签的标题），给每个起一个唯一名字。
2. 在 krilla `Document` 上注册这些命名目的地址，同时把 `Location → NamedDestination` 记进 `loc_to_names`。

为什么标题要被预注册？因为目录、`#link(<heading>)`、脚注回链等都可能指向某个标题 location，提前把标题做成命名目的地址，链接就能稳定按名字跳转，而不必每次都现算坐标。

#### 4.3.2 核心流程

```
候选 = anchors（外部传入的锚点）
        ∪ 带标签的标题（HeadingElem.location() + .label()）
            │
            ▼  按 name 去重（seen 集合）
        每个 (loc, name)
            │
            ▼  introspector.position(loc) ─▶ pos_to_xyz
        若 pos_to_xyz 返回 Some（即所在页被导出）
            │
            ▼
        document.register_named_destination(NamedDestination(name, xyz))
        locs_to_names.insert(loc, named)
            │
            ▼
        否则（页被排除）：跳过，不注册
```

去重很关键：多个标题可能解析出同一个名字（比如 `<<intro>>` 标签重复，或 anchor 与标题重名）。代码用一个 `seen` 集合，对每个名字只保留第一次出现的，避免 krilla 报「重复命名目的地址」。

#### 4.3.3 源码精读

完整函数在 [src/convert.rs:845-885](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L845-L885)：

```rust
fn collect_named_destinations(
    document: &mut Document,
    typst_document: &PagedDocument,
    anchors: &[(Location, EcoString)],
    pic: &PageIndexConverter,
) -> FxHashMap<Location, NamedDestination> {
    let mut locs_to_names = FxHashMap::default();

    // 去重集合：同一个名字只收第一次出现的位置
    let mut seen = FxHashSet::default();
    let headings = typst_document.introspector().query(&HeadingElem::ELEM.select());
    let matches = anchors
        .iter()
        .map(|(loc, anchor)| (*loc, anchor.to_string()))
        .chain(
            headings
                .iter()
                .filter_map(|elem| elem.location().zip(elem.label()))
                .map(|(loc, label)| (loc, label.resolve().to_string())),
        )
        .filter(|(_, name)| seen.insert(name.clone()));   // 去重

    for (loc, name) in matches {
        // 只有当该位置所在页被导出时才注册
        let pos = typst_document
            .introspector()
            .position(loc)
            .unwrap_or(PagedPosition::ORIGIN);
        if let Some(dest) = crate::link::pos_to_xyz(pic, pos) {
            let named = NamedDestination::new(name, dest);
            document.register_named_destination(named.clone()).unwrap();
            locs_to_names.insert(loc, named);
        }
    }

    locs_to_names
}
```

几个要点：

- **候选来源**：`anchors` 参数（由 `pdf()` / `pdf_in_bundle()` 从外部传入，见 u1-l2）拼上「带 `label` 的标题」。`anchors` 优先排在 `chain` 前面，所以同名的 anchor 会赢过标题。
- **去重**：`.filter(|(_, name)| seen.insert(name.clone()))` 是惯用法——`seen.insert` 返回 `true` 表示首次插入，于是同名的后续候选被过滤掉。
- **页排除检查**：`pos_to_xyz(pic, pos)` 在所在页被 `page_ranges` 排除时会返回 `None`（见 4.4 节），于是整条候选被静默跳过。这正是 4.2 节注释所说「已注册的命名目的地址一定不会指向被排除页」的保证来源。
- **`unwrap` 安全性**：注释解释 `register_named_destination` 返回 `None` 只会发生在「重名」时，而上面已用 `seen` 去重，所以这里 `unwrap` 不会触发。

返回的 `locs_to_names` 随后被塞进 `GlobalContext.loc_to_names`（见 [src/convert.rs:77-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L77-L84) 与 [src/convert.rs:297](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L297)），供 `handle_link` 第二级回退查询。

#### 4.3.4 代码实践

**实践目标**：理解候选来源与去重顺序。

**操作步骤**：

1. 阅读 `collect_named_destinations`，列出候选的两个来源。
2. 假设文档里有两个标题都带标签 `<<intro>>`，追踪它们分别走到哪一步。

**需要观察的现象**：`chain` 把 anchors 排在前、标题排在后；`seen` 去重保留首次。

**预期结果**：两个同名 `<<intro>>` 标题中，只有第一个（在 `headings` 迭代顺序里先出现的）会进入 `matches`，第二个被 `seen.insert` 返回 `false` 过滤。因此 `loc_to_names` 里这个名字只会指向第一个标题。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `collect_named_destinations` 要在 `convert_pages` 之前执行？

> 参考答案：因为 `convert_pages` 遍历页面时会调用 `handle_link`，而 `handle_link` 的第二级回退要查 `gc.loc_to_names`。这张表必须先于页面导出构造好。同时命名目的地址需要先在 krilla `Document` 上注册（`register_named_destination`），后续链接才能按名字引用。

**练习 2**：如果一个带标签的标题恰好落在被 `page_ranges` 排除的页上，会发生什么？

> 参考答案：该标题仍会进入候选 `matches`，但在 `pos_to_xyz(pic, pos)` 时因页被排除返回 `None`，于是既不会 `register_named_destination`，也不会插入 `loc_to_names`。指向它的链接在 `handle_link` 第二级查不到，会落到第三级实时定位，再因同样原因返回 `Ok(())` 静默丢弃。

---

### 4.4 位置到 XYZ 目的地：pos_to_xyz() 与页号映射

#### 4.4.1 概念说明

`pos_to_xyz()` 是 `Position` 链接和 `Location` 兜底链接共同的核心：把 Typst 的 `PagedPosition`（第几页 + 坐标点）翻译成 krilla 的 `XyzDestination`（PDF 页索引 + 坐标点）。它也被 `collect_named_destinations()` 复用。

它做两件事：

1. **页号重映射**：Typst 内部用的是「文档页号」（1 基），PDF 里需要的是「PDF 页索引」（0 基，且可能因 `page_ranges` 跳过而重排）。
2. **y 坐标基线偏移修正**：把目标 y 坐标向上抬一点（减 10pt），让跳转后目标文字能完整显示在视口里。

#### 4.4.2 核心流程

```
输入 PagedPosition { page, point }
        │
        ▼
pdf_page_index(page.get() - 1)     // 1基→0基，并跳过被排除页
        │
   None? ──是──▶ 返回 None（调用方静默丢弃）
        │否
        ▼
adjusted = (point.x, (point.y - 10pt).max(0))
        │
        ▼
XyzDestination::new(page_index, adjusted.to_krilla())
```

关于 10pt 偏移，源码文档注释（[src/link.rs:194-200](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L194-L200)）给出了原因：诸如脚注回链这类链接，其 position 落在文字的**基线**上。如果直接跳到基线坐标，目标文字会刚好在视口顶端之上、看不见。把 y 减去约一行字的高度（10pt），相当于「把视口往上抬一点」，让目标行完整露出。

#### 4.4.3 源码精读

完整函数在 [src/link.rs:201-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L201-L209)：

```rust
pub(crate) fn pos_to_xyz(
    pic: &PageIndexConverter,
    pos: PagedPosition,
) -> Option<XyzDestination> {
    let page_index = pic.pdf_page_index(pos.page.get() - 1)?;
    let adjusted =
        Point::new(pos.point.x, (pos.point.y - Abs::pt(10.0)).max(Abs::zero()));
    Some(XyzDestination::new(page_index, adjusted.to_krilla()))
}
```

逐行解读：

- `pos.page.get() - 1`：`PagedPosition.page` 是 `NonZeroUsize`（1 基页号），`.get()` 得到 `usize`，减 1 转成 0 基页索引交给 `PageIndexConverter`。
- `pic.pdf_page_index(...)?`：查 `PageIndexConverter`（见 [src/convert.rs:917-919](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L917-L919)）。若该页被 `page_ranges` 排除，返回 `None`，`?` 提前返回 `None`。
- `(pos.point.y - Abs::pt(10.0)).max(Abs::zero())`：减 10pt 后用 `.max(0)` 兜底，避免页顶附近的链接算出负 y。

`PageIndexConverter` 的构造逻辑在 [src/convert.rs:893-910](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L893-L910)，它在 `convert()` 启动时一次性遍历全部页，把保留页的索引压缩成连续的 PDF 页号：

```rust
for i in 0..document.pages().len() {
    if options.page_ranges.as_ref()
        .is_some_and(|ranges| !ranges.includes_page_index(i))
    {
        skipped_pages += 1;          // 被排除：只累计跳过数
    } else {
        page_indices.insert(i, i - skipped_pages);  // 保留：映射到紧凑页号
    }
}
```

可见 PDF 页号是「去掉被排除页后重排」的连续编号，不会留空洞。`pos_to_xyz` 正是通过它把 Typst 页号对齐到这份重排后的页号。

#### 4.4.4 代码实践

**实践目标**：理解 10pt 偏移的动机与页号重映射。

**操作步骤**：

1. 阅读函数文档注释（[src/link.rs:194-200](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L194-L200)），它举了「脚注回链」的例子。
2. 假设一份 5 页文档，用 `page_ranges` 只导出第 1、3、5 页。在心里推演 `PageIndexConverter` 构造出的 `page_indices` 表。

**需要观察的现象**：

- 第 0 页（Typst 第 1 页）→ PDF 第 0 页；
- 第 2 页（Typst 第 3 页）→ PDF 第 1 页；
- 第 4 页（Typst 第 5 页）→ PDF 第 2 页。
- 第 1、3 页被排除，查 `pdf_page_index` 返回 `None`。

**预期结果**：一条指向「Typst 第 3 页顶部某坐标」的 `Position` 链接，`pos_to_xyz` 算出的 `page_index` 是 1（不是 2），y 坐标比实际 position 低 10pt。导出的 PDF 里点这条链接会跳到第 2 页（人类计数），且目标行完整显示。

#### 4.4.5 小练习与答案

**练习 1**：如果把 10pt 偏移去掉（直接用 `pos.point.y`），用户会观察到什么？

> 参考答案：脚注回链、目录跳转这类「position 落在基线上」的链接，点击后目标文字会贴在阅读器视口的最顶端甚至被顶出去一点，用户需要往上滚动才能看到完整的目标行。10pt 偏移就是为了避免这种「跳到了但看不见」的体验。

**练习 2**：`pos_to_xyz` 里的 `.max(Abs::zero())` 何时真正起作用？

> 参考答案：当目标 position 的 y 坐标本身就小于 10pt（即在页面最顶端）时，`y - 10pt` 会变成负数。PDF 坐标里负 y 仍在页面外，`.max(0)` 把它钳到 0（页顶），避免出现「跳到页面上方」的无效目的地。

---

### 4.5 链接矩形与多行注记发射：bounding_box()、quadpoints 与 tagged 门控

#### 4.5.1 概念说明

到目前为止我们只解决了「跳到哪」（target），还没解决「点哪里」（点击区域）。`bounding_box()` 负责算出当前这一段链接文字在页面坐标系下的矩形。算完之后，`handle_link` 还要决定如何收集它——这是本支线里和 tagged PDF 耦合最深的部分。

涉及三件事：

1. **矩形计算**：链接在 Frame 树里是「平移到某个点后、占用一个 `size`」，但累计变换可能还有旋转/缩放。`bounding_box` 取这个矩形的四个角，逐个用当前变换变到页面坐标，再取外包矩形。
2. **tagged 门控**：链接是「内容」还是「装饰（artifact）」？tagged PDF 开启时，链接需要挂到结构树的 `Link` 元素下；tagged 关闭或在 tiling 内部时，链接只是普通 artifact。
3. **多行合并 vs 拆分**：一段跨行链接会被收集成同一 `LinkAnnotation` 的多个 rect。是合并成一个带 quadpoints 的注记，还是拆成多个矩形注记？这取决于是否针对 PDF/UA。

#### 4.5.2 核心流程

`handle_link` 在算出 target 后，进入注记收集阶段，整体流程如下（[src/link.rs:77-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L77-L165)）：

```
rect = bounding_box(fc, size)            // 算这一行的点击矩形

if tags::disabled(gc):                   // tagged 关闭 或 在 tiling 内
    ├─ 若 in_tiling 且开启 PDF/UA ─▶ 报错（tiling 内不允许链接）
    └─ 否则：作为 Artifact 注记收集
else (tagged 开启):
    (group_id, link) = parent_link()     // 找到所属 Link 结构元素
    alt = link.alt                        // 无障碍替代文本
    if parent_artifact().is_some():       // 链接落在 artifact 内容里
        ├─ 若开启 PDF/UA ─▶ 报错（artifact 内不允许链接）
        └─ 否则：作为 Artifact 注记收集
    else:
        join = 是否针对 PDF/UA
        if join 且同 group 已有注记:
            把 rect 追加进已有注记.rects   ← quadpoints 合并
        else:
            预占 annot_id，新建 Tagged 注记
```

#### 4.5.3 源码精读

**矩形计算** —— [src/link.rs:169-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L169-L192)：

```rust
fn bounding_box(fc: &FrameContext, size: Size) -> kg::Rect {
    let pos = Point::zero();
    // 取局部矩形（原点起、宽高为 size）的四个角
    let points = [
        pos + Point::with_y(size.y),
        pos + size.to_point(),
        pos + Point::with_x(size.x),
        pos,
    ];

    let mut min_x = f32::INFINITY;
    let mut min_y = f32::INFINITY;
    let mut max_x = f32::NEG_INFINITY;
    let mut max_y = f32::NEG_INFINITY;

    for point in points {
        // 用当前累计变换把角点变到页面坐标系
        let p = point.transform(fc.state().transform()).to_krilla();
        min_x = min_x.min(p.x);
        min_y = min_y.min(p.y);
        max_x = max_x.max(p.x);
        max_y = max_y.max(p.y);
    }

    kg::Rect::from_ltrb(min_x, min_y, max_x, max_y).unwrap()
}
```

注意这里没有用「`pos + size`」直接构造一个轴对齐矩形，而是老老实实取四个角、逐个变换、再取外包。这是因为 `fc.state().transform()` 可能含旋转/缩放，变换后矩形不再是轴对齐的，必须取变换后角点的外包围盒。`fc.state().transform()` 正是 u2-l7 讲的那条 `FrameContext.states` 变换栈栈顶。

**tagged 门控与收集** —— 先看 `tags::disabled` 的定义（[src/tags/mod.rs:147-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L147-L149)）：

```rust
pub fn disabled(gc: &GlobalContext) -> bool {
    !gc.options.tagged || gc.tags.in_tiling
}
```

即「用户关闭了 tagged」或「当前正处在一个 tiling（平铺图案）内部」。后者是因为 tiling 内部不生成结构标签（u3-l12 已讲）。

当 disabled 为真时，链接走 artifact 分支，但在「tiling 内 + PDF/UA」组合下会直接报错（[src/link.rs:79-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L79-L104)）：

```rust
if tags::disabled(gc) {
    if gc.tags.in_tiling
        && let Some(accessibility) = gc.options.accessibility_validator()
    {
        let validator = accessibility.as_str();
        bail!(
            Span::detached(),
            "{validator} error: PDF artifacts may not contain links";
            hint: "a link was used within a tiling";
            hint: "references, citations, and footnotes are also considered links in PDF";
        );
    }
    fc.push_link_annotation(GroupId::INVALID, LinkAnnotation {
        kind: LinkAnnotationKind::Artifact, ...
    });
    return Ok(());
}
```

`accessibility_validator()` 仅在导出标准含 PDF/UA 时返回 `Some`（u1-l3）。也就是说，PDF/UA 模式下，平铺图案里出现链接是直接禁止的——这也呼应了 Typst 里「引用、引文、脚注都算链接」的事实。

tagged 开启时，先用 `parent_link()` 找到当前逻辑位置最近的 `Link` 结构元素及其 group（[src/link.rs:106-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L106-L109)），取其 `alt` 文本。接着是关键的多行合并决策（[src/link.rs:135-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L135-L163)）：

```rust
// quadpoints 在大多数阅读器里支持不佳。仅当针对 PDF/UA 时，
// 才把多个 quadpoints 合进一个注记；否则生成多个注记，
// 以免阅读器回退到包围盒矩形——那个矩形会横跨与链接无关的区域。
let join_annotations = gc.options.accessibility_validator().is_some();
match fc.get_link_annotation(group_id) {
    Some(annotation) if join_annotations => annotation.rects.push(rect),  // 合并
    _ => {
        let annot_id = gc.tags.annotations.reserve();
        fc.push_link_annotation(group_id, LinkAnnotation {
            kind: LinkAnnotationKind::Tagged(annot_id),
            alt, span: link.span(), rects: vec![rect], target,
        });
        let group = gc.tags.tree.groups.get_mut(group_id);
        group.push_annotation(annot_id);
    }
}
```

这段注释值得一读：它解释了一个工程权衡。跨行链接若用单一矩形（包围盒），会覆盖整段文字宽，把无关内容也变得可点击。PDF 提供 quadpoints 来精确描述多个区域，但「多数 PDF 阅读器对 quadpoints 支持不好」。于是策略是：

- **PDF/UA 模式**：无障碍场景要求注记精确，必须用 quadpoints 合并（`annotation.rects.push(rect)`）。
- **非 PDF/UA**：宁可拆成多个独立注记，让阅读器按各自的矩形点击，避免回退到不精确的包围盒。

**发射阶段** —— 整页画完后，`convert_pages` 调用 `tags::add_link_annotations` 把收集到的注记写入 krilla 页面（[src/tags/mod.rs:152-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L152-L175)）：

```rust
for a in annotations {
    let link_annotation = if let [rect] = a.rects.as_slice() {
        krilla::annotation::LinkAnnotation::new(*rect, a.target)        // 单矩形
    } else {
        let quads = a.rects.iter().map(|r| kg::Quadrilateral::from(*r)).collect();
        krilla::annotation::LinkAnnotation::new_with_quad_points(quads, a.target) // quadpoints
    };

    let annotation = krilla::annotation::Annotation::new_link(link_annotation, a.alt)
        .with_location(Some(a.span.into_raw()));

    if let LinkAnnotationKind::Tagged(annot_id) = a.kind {
        let identifier = page.add_tagged_annotation(annotation);
        gc.tags.annotations.init(annot_id, identifier);   // 回填真实注记标识
    } else {
        page.add_annotation(annotation);
    }
}
```

这正是 4.1 节埋的伏笔：`rects` 长度为 1 走普通矩形注记，否则把每个 `Rect` 转成 `Quadrilateral` 用 quadpoints；`Tagged` 注记用 `add_tagged_annotation` 绑定到结构树，`Artifact` 用 `add_annotation` 直接挂上。

#### 4.5.4 代码实践

**实践目标**：弄清多行链接在 PDF/UA 与非 PDF/UA 下的两种发射形态。

**操作步骤**：

1. 在源码里读 [src/link.rs:135-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L135-L145) 的注释，理解「包围盒横跨整段」的问题。
2. 假设一段链接文字跨两行，产生两个 `FrameItem::Link`、两个 rect，且二者属于同一个 `group_id`。

**需要观察的现象**：

- 非 PDF/UA（`accessibility_validator()` 为 `None`）：`join_annotations` 为假，每次都走 `_ =>` 分支新建注记，最终页面里有两个独立的矩形链接注记。
- PDF/UA：`join_annotations` 为真，第二次进入时 `get_link_annotation(group_id)` 命中，`rects.push(rect)` 合并，最终只有一个带两个 quadpoints 的注记。

**预期结果**：在 PDF 阅读器里，两种情况下点击区域都精确覆盖两行链接文字；区别在于 PDF 内部注记数量与是否使用 quadpoints，这会影响阅读器兼容性与无障碍工具的识别。

#### 4.5.5 小练习与答案

**练习 1**：`bounding_box` 为什么要把矩形的四个角分别变换再取外包，而不是直接 `Rect::new(pos, size)` 后再变换？

> 参考答案：当前变换 `fc.state().transform()` 可能含旋转或非均匀缩放，变换后矩形的四条边不再轴对齐，无法再用单个 `Rect` 精确表示。正确做法是先把四个角点各自变换到页面坐标，再取它们的轴对齐外包围盒（min/max）。这样无论旋转多大都能得到一个覆盖真实变换后区域的矩形。

**练习 2**：为什么 PDF/UA 模式坚持用 quadpoints 合并，而普通模式反而拆成多个注记？

> 参考答案：普通模式下「多数阅读器对 quadpoints 支持不好」，若合并成一个 quadpoints 注记，阅读器可能回退到注记的包围盒矩形——这个包围盒会横跨两行之间的无关文字，造成误点。拆成多个独立矩形注记能避开这个问题。而 PDF/UA 场景优先满足无障碍语义（一个逻辑链接对应一个注记、且需精确区域），所以必须用 quadpoints 合并。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，追踪一条「文档内链接指向带标签标题」的完整生命周期。

假设 Typst 文档如下：

```typst
= 引言 <intro>

更多细节见 #link(<intro>)[回到引言]。
```

请按顺序回答并写出每一步对应的关键源码位置：

1. **预收集阶段**：`= 引言 <intro>` 这个带标签标题，在 `convert()` 的哪个阶段被处理？它如何变成一条命名目的地址？写出 `collect_named_destinations` 里负责「带标签标题」的候选来源代码片段，并说明 `loc_to_names` 最终多出的那条记录的 key 和 value 分别是什么。

2. **链接遍历阶段**：`#link(<intro>)[回到引言]` 排版后会变成一个 `FrameItem::Link(dest, size)`，其中 `dest` 是哪种 `Destination` 变体？`handle_link` 进入哪个 `match` 分支？

3. **target 解析**：写出该分支的三级回退顺序。在本场景（单文档 `pdf()` 导出、标题已被预注册）下，哪一级命中？最终 `target` 是 `Target::Action` 还是 `Target::Destination`？子类型是 `Named` 还是 `Xyz`？

4. **矩形与发射**：链接文字「回到引言」的点击矩形由谁计算？它依赖 `FrameContext` 的哪个状态？若该链接恰好不跨行、且导出标准为默认 PDF 1.7（无 PDF/UA），`add_link_annotations` 会走 `new` 还是 `new_with_quad_points` 分支？

5. **页号修正**：若整份文档设置了 `page_ranges` 只导出包含「引言」标题的那一页，`pos_to_xyz` 在预收集阶段对标题 position 算出的 `page_index` 是多少（0 基）？y 坐标相对原始 position 变化了多少？

**参考答案要点**：

1. 预收集发生在 `collect_named_destinations`（[src/convert.rs:845-885](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L845-L885)），早于 `convert_pages`。候选来源是 `headings.iter().filter_map(|elem| elem.location().zip(elem.label()))`。新记录的 key 是标题的 `Location`，value 是 `NamedDestination("intro", XyzDestination{...})`。

2. `dest` 是 `Destination::Location(loc)`（`<intro>` 是一个 location，编译期未解析成坐标）。进入 `Location(loc) =>` 分支。

3. 顺序：① `link_resolver`（单文档为 `None`，短路）→ ② `loc_to_names.get(loc)`（命中，因为标题已预注册）→ ③ introspector 兜底（用不到）。本场景第二级命中，`target = Target::Destination(Named(...))`。

4. 矩形由 `bounding_box(fc, size)`（[src/link.rs:169-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs#L169-L192)）计算，依赖 `fc.state().transform()`。不跨行 → `rects` 长度 1 → 默认 PDF 1.7 无 PDF/UA → `add_link_annotations` 走 `LinkAnnotation::new(*rect, target)`（单矩形分支）。

5. `page_ranges` 只导出一页时，`PageIndexConverter` 把这页映射为 PDF 第 0 页，故 `page_index = 0`。y 坐标相对原始 position 减少了 10pt（基线偏移修正）。

## 6. 本讲小结

- `handle_link()` 是链接支线的唯一入口，按 `Destination` 三变体分派：`Url`→URI 动作，`Position`→`pos_to_xyz`→XYZ 目的地，`Location`→三级回退。
- `Destination::Location` 的回退顺序是 `link_resolver`（跨文档，仅 bundle 导出）→ `loc_to_names`（命名目的地址）→ `introspector.position`（实时定位兜底）；任何一级 `pos_to_xyz` 返回 `None` 都静默丢弃。
- `collect_named_destinations()` 在导出页之前预收集「显式锚点 + 带标签标题」，按名字去重，并通过 `pos_to_xyz` 过滤掉落在被排除页上的候选，从而保证命名目的地址永远指向被导出页。
- `pos_to_xyz()` 做两件事：用 `PageIndexConverter` 把 1 基文档页号重映射为 0 基 PDF 页号；给 y 减 10pt 做基线偏移修正（并 `.max(0)` 兜底），让脚注回链等目标文字完整显示。
- `bounding_box()` 取局部矩形四角、逐个用 `fc.state().transform()` 变到页面坐标再取外包，正确处理旋转/缩放后的点击区域。
- 多行链接在 PDF/UA 下用 quadpoints 合并成一个注记，非 PDF/UA 下拆成多个矩形注记，以避开阅读器对 quadpoints 支持不佳的问题；tiling 内或 artifact 内出现链接在 PDF/UA 模式下会被直接报错。
- 链接注记「先收集进 `FrameContext.link_annotations`、整页画完后再由 `add_link_annotations` 统一发射」，是为了支持多行合并与结构树关联。

## 7. 下一步学习建议

- **书签大纲**（u4-l15）：`build_outline()` 同样依赖 `pos_to_xyz()` 把标题变成目的地址，可作为本讲的延伸练习——对比「链接用命名目的地址」与「大纲直接用 XYZ 目的地」的差异。
- **元数据与时间戳**（u4-l16）：继续 u4 单元的文档级特性，了解 `GlobalContext` 的其它用途。
- **tagged PDF 子系统**（u5-l19 起）：本讲多次出现 `parent_link()`、`parent_artifact()`、`annotations.reserve()/init()`、`GroupId`、`AnnotationId` 等，它们都属于 tagged PDF 的逻辑结构树。若想真正弄懂「链接如何挂到 `Link` 结构元素下」，请进入 u5 系列系统学习 tags 的 build/tree/resolve 三段式。
- 阅读建议：先重读 [src/link.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/link.rs) 全文（仅约 210 行），它已把本讲所有模块串在一起；再带着本讲的理解去看 `convert.rs` 里 `collect_named_destinations` 与 `convert_pages` 的收尾两行，确认「收集—发射」的衔接。
