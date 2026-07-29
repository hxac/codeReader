# 跨文档链接与锚点：create_link_anchors

## 1. 本讲目标

本讲是专家层「内省、跨文档链接、性能与 CLI 集成」的第二篇，承接 [u5-l1 的统一内省器](u5-l1-bundle-introspector.md) 与 [u4-l2 的导出钩子](u4-l2-format-exporters.md)，回答一个核心问题：

> 在 bundle 这种「一次编译产出多个文件」的目标下，一个文档里的 `#link(<标签>)` 是怎样精确跳转到**另一个文档**（甚至是某个 PDF 里的某个元素、某个 asset 文件本身）的？

学完本讲你应当能够：

- 说清「链接目标」的两类来源（`LinkElem` 逻辑链接 + frame 内原始链接），以及 `link_targets()` 为什么要把它们**按文档路径分桶**。
- 说清 `create_link_anchors()` 如何在 HTML 与 paged 两条路径上分别生成锚点：HTML 是**原地注入 DOM `id`**，paged 是**生成命名目的地写入侧车**。
- 解释 `AnchorGenerator` 如何把标签变成「人类可读且无碰撞」的锚点字符串，以及 paged 路径为什么必须先按 `loc_index` 排序再分配。
- 解释为什么文档和 asset 自身也需要一个**空锚点**，以及 `LateLinkResolver::resolve` 如何消费这些锚点得到最终的相对 URI。

## 2. 前置知识

本讲默认你已经掌握前置讲义的以下结论（不再重复证明）：

- **统一内省器**（u5-l1）：bundle 不让各文档独立跑内省，而是用单一 `BundleIntrospector` 让所有文档彼此可见；`path(loc)` 能把任意 `Location` 路由回它所属文档的 `VirtualPath`，这正是跨文档链接「找得到目标文件」的基础。
- **数据模型**（u3-l1）：`BundleDocument::Paged(doc, PagedExtras)` 中 `PagedExtras.anchors: Vec<(Location, EcoString)>` 是一个**侧车**，用来装 `PagedDocument` 自身装不下的跨文档命名锚点；HTML 变体没有这个侧车。
- **导出钩子**（u4-l2）：`pdf_in_bundle` / `svg_in_bundle` / `html_in_bundle` 等钩子比普通导出多收 `link_resolver` 与 `anchors`；`LateLinkResolver` 在导出期把链接 `Location` 解析为相对 URI。

还需要两个通用概念：

- **锚点（anchor）**：链接要跳转到的「目的地标记」。在 HTML 里它是元素的 `id` 属性（URL 片段 `#id`）；在 PDF 里它叫**命名目的地（named destination）**；在 SVG 里也是元素 `id`。跨文档链接 = 「相对路径 + 锚点片段」。
- **Location**：Typst 给每个可定位元素分配的稳定标识。跨文档链接在编译期只持有目标的 `Location`，锚点的「具体字符串」要到编译末尾才分配——这就是本讲要讲的「后置回填」。

一句话直觉：**链接是「我指向谁」，锚点是「那个谁叫什么名字」**。bundle 难就难在「那个谁」可能在另一个文件里，名字要等所有文档都排完版才能统一分配，并且分配结果还要再喂回导出阶段去解析。

## 3. 本讲源码地图

本讲横跨四个文件，分属三个 crate：

| 文件 | 角色 |
| --- | --- |
| `crates/typst-bundle/src/introspect.rs` | 在统一内省器上实现 `link_targets()`（收集目标并分桶）、`set_anchors()`（回填锚点表）、`anchor()`（按 Location 查锚点）。 |
| `crates/typst-bundle/src/link.rs` | bundle 侧的 `create_link_anchors()`（总分派）与 `create_paged_link_anchors()`（paged 命名目的地生成）。 |
| `crates/typst-html/src/link.rs` | HTML 侧的 `typst_html::create_link_anchors()`，遍历 DOM 节点树原地注入 `id`。 |
| `crates/typst-library/src/model/link.rs` | `LinkElem::find_destinations()`（找逻辑链接目标）、`AnchorGenerator`（生成锚点字符串）、`LateLinkResolver`/`ResolvedLink`（消费锚点产出 URI）。 |

另外会少量引用 `crates/typst-layout/src/introspect.rs`（paged 的 `frame_link_targets` 来源）与 `crates/typst-bundle/src/lib.rs`（这些函数在 `bundle_impl` 里的调用顺序）。

## 4. 核心概念与源码讲解

### 4.1 收集链接目标：link_targets 把目标按文档分桶

#### 4.1.1 概念说明

要给「被链接的目标」分配锚点，第一步得先知道**有哪些目标被链接了**、**它们各自在哪个文档里**。

bundle 的难点在于：链接的发起方和目标可能不在同一个文档。比如 `index.html` 里写了 `#link(<glossary>)`，而 `<glossary>` 这个标签挂在 `appendix.html` 的某个标题上。分配锚点时，这个标题在 `appendix.html` 内部，所以**注入 `id` 的动作必须发生在处理 `appendix.html` 的时候**。

因此收集阶段产出的不是「一锅端的位置列表」，而是一张**按目标文档路径分桶**的映射：

\[
\text{targets}: \text{VirtualPath} \;\mapsto\; \{\,\text{Location}_1,\ \text{Location}_2,\ \dots\,\}
\]

这样后续 `create_link_anchors` 处理某个文档时，只要查自己路径对应的那一桶即可。

#### 4.1.2 核心流程

`link_targets()` 合并**两类**链接来源，再逐个分桶：

1. **逻辑链接**：用户用 `#link(<标签>)` 或 `#link(location)` 写出的 `LinkElem`。这些元素本身进了内省器，用 `LinkElem::find_destinations()` 查出来。
2. **frame 内原始链接**：排版阶段直接写进 `Frame` 的 `FrameItem::Link`，例如参考文献回链、脚注引用、目录条目等**不经过 `LinkElem`** 的链接（见 `crates/typst-library/src/model/link.rs` 中 `DirectLinkElem` 的注释）。这些由各子文档（paged/html）的内省器在 `discover_frame` 时收集到 `frame_link_targets`。

伪代码：

```
for target in (LinkElem 链接目标 ∪ 所有子文档的 frame_link_targets):
    path = introspector.path(target)      # 这个目标在哪个文档里？
    if path 是 None: continue             # 目标不在任何文档内（例如顶层元数据），跳过
    targets[path].insert(target)
return targets
```

#### 4.1.3 源码精读

入口 `BundleIntrospector::link_targets` 定义在 [crates/typst-bundle/src/introspect.rs:L49-L61](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L49-L61)，中文说明：合并两类链接目标，用 `self.path(target)` 把每个目标路由到它所在文档的路径，按路径分桶到 `FxHashMap<&VirtualPath, FxHashSet<Location>>`。

其中「逻辑链接」由 `LinkElem::find_destinations(self)` 产出，注意它传入的 `self` 就是**整个 bundle 的统一内省器**——因此这一次查询就能把所有文档里的 `LinkElem` 都捞出来（因为 `BundleIntrospector::query` 合并了全部子文档的元素）。该方法定义在 [crates/typst-library/src/model/link.rs:L211-L222](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L211-L222)，中文说明：查询所有 `LinkElem`，用 `resolve_late` 把每个链接的目的地解析成 `Location`，只保留内部位置链接（URL 链接被丢弃，因为它们不需要锚点）。

「frame 原始链接」来自各子文档的 `frame_link_targets()`：

```rust
self.children
    .iter()
    .flat_map(|(_, child, _)| child.frame_link_targets())
    .copied()
```

`ChildIntrospector::frame_link_targets` 见 [crates/typst-bundle/src/introspect.rs:L184-L189](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L184-L189)，中文说明：转发到 paged 或 html 子内省器已收集好的目标集合。

以 paged 为例，这些目标是在构建内省器遍历 `Frame` 时收集的，见 [crates/typst-layout/src/introspect.rs:L199-L203](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-layout/src/introspect.rs#L199-L203)，中文说明：`discover_frame` 遇到 `FrameItem::Link(Destination::Location(loc), _)` 时，把目标 `loc` 插入 `frame_link_targets` 集合（字段定义在同文件 [L25-L26](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-layout/src/introspect.rs#L25-L26)）。

> 关键点：`link_targets` 必须同时覆盖两类来源。只查 `LinkElem` 会漏掉目录、参考文献等 frame 级原始链接；只查 frame 会漏掉 HTML 中纯逻辑的 `LinkElem`（HTML 的 frame 里不一定有对应 `FrameItem::Link`）。

#### 4.1.4 代码实践

**实践目标**：用源码确认「两类来源」与「分桶」这两件事。

**操作步骤**：

1. 打开 [crates/typst-bundle/src/introspect.rs:L49-L61](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L49-L61)，把 `for` 循环体里 `.chain(...)` 前后两段分别圈出来，标注「来自 LinkElem」「来自 frame」。
2. 跟进 `self.path(target)`（[L148-L165](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L148-L165)），确认：对文档**内部**的元素，`path` 走 `children[index]` 分支返回该文档路径；对**文档/asset 自身**的 `Location`，走 `get_by_loc` 分支也返回路径。因此桶的 key 既能是文档也能是 asset 的路径。

**需要观察的现象**：`path()` 返回 `None` 时 `link_targets` 用 `continue` 跳过该目标——即「目标不在任何文件内」的链接不会被分配锚点（它会在导出期被 `LateLinkResolver` 判为坏链）。

**预期结果**：你能用一句话说出「`link_targets` 的输出形状是 `文档路径 → 目标 Location 集合`，且同时包含 LinkElem 与 frame 两类目标」。运行结果与具体项目无关，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `link_targets` 用 `FxHashSet<Location>`（集合）而不是 `Vec`（列表）去装每个文档的目标？

> **答案**：同一个目标可能被多条链接指向（例如多个地方都 `#link(<glossary>)`），分配锚点时只需为它生成**一个**锚点。用集合天然去重，避免给同一位置重复注入多个 `id`。

**练习 2**：如果某个 `#link` 指向的目标是一个挂在 bundle 顶层的、不在任何 `#document` 内的元素（比如纯元数据），`link_targets` 会怎么处理？

> **答案**：`self.path(target)` 对这种顶层元素返回 `None`（它不在任何子文档里），循环里 `let Some(path) = ... else { continue }` 直接跳过，不会进入任何桶。后续它也不会得到锚点，导出期 `LateLinkResolver::resolve` 会返回 `None`，链接无法解析。

---

### 4.2 分派锚点生成：create_link_anchors 的 HTML/paged 分支

#### 4.2.1 概念说明

收集完分桶的目标后，下一步是「为每个目标生成一个锚点字符串」。但 HTML 和 paged（PDF/SVG）的锚点机制**完全不同**：

- **HTML**：锚点就是 DOM 元素的 `id` 属性。要给目标「装上 id」，必须**改写已经构建好的 HTML 节点树**——所以 HTML 路径会**原地修改** `HtmlDocument`。
- **paged**：锚点是 PDF 的「命名目的地」/ SVG 的元素 id。`PagedDocument` 是不可变共享的（见 u3-l1 的 `Arc` 共享），不能原地改；于是把锚点写进 `PagedExtras.anchors` 这个**侧车**，留给导出钩子在编码时读取。

`create_link_anchors()` 就是这个「按格式分派」的总入口。它返回一张全局表 `Location → 锚点字符串`（每个锚点都**局部于其所在文件**），供内省器的 `anchor()` 查询。

#### 4.2.2 核心流程

```
对 items 并行(par_iter_mut)处理每一个 Item::Document:
    取出本文档那一桶 targets（没有就空集）
    if Html(doc):   调 typst_html::create_link_anchors(doc, targets)  # 原地改 DOM，返回 loc→id
    if Paged(doc, options): options.anchors = create_paged_link_anchors(doc, targets)  # 写侧车
    把 (loc, 锚点) 对收集进全局 anchors 表
最后：给每个 asset / document 自身的 Location 插入一个空字符串锚点
返回全局 anchors 表
```

注意它取的是 `&mut [Item]`——因为要原地改 HTML DOM 和 paged 侧车。

#### 4.2.3 源码精读

总分派函数见 [crates/typst-bundle/src/link.rs:L20-L63](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L20-L63)，中文说明：用 rayon 并行遍历 `items`，对每个 `Item::Document` 按 `BundleDocument` 分派——HTML 调 `typst_html::create_link_anchors` 原地注入 id，paged 调 `create_paged_link_anchors` 把结果写进 `options.anchors` 侧车——最后把所有 `(Location, 锚点)` 扁平化成一张全局表。

核心分派片段（[L28-L47](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L28-L47)）：

```rust
let Item::Document(path, doc, _) = item else {
    return Either::Right([].iter().cloned());   // Tag/Asset 在此不处理
};
let targets = targets.get(path).unwrap_or(&empty);
match doc {
    BundleDocument::Html(doc) => Either::Left(
        typst_html::create_link_anchors(doc.as_mut(), &targets.iter().copied().collect())
            .into_iter(),
    ),
    BundleDocument::Paged(doc, options) => {
        options.anchors = create_paged_link_anchors(doc, targets);
        Either::Right(options.anchors.iter().cloned())
    }
}
```

中文要点：

- `Item` 是 `bundle_impl` 里编译后的产物枚举，定义在 [crates/typst-bundle/src/lib.rs:L229-L233](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L229-L233)（`Tag` / `Asset(path, bytes, loc)` / `Document(path, doc, loc)`）。这里只对 `Document` 分派锚点，因为只有文档内部有「可被链接的元素」。
- HTML 分支返回的是 `HashMap` 的迭代（`loc → id` 对），paged 分支返回的是 `Vec<(loc, anchor)>` 的克隆。两者都被 `flat_map_iter().collect()` 统一拍平进同一张 `FxHashMap<Location, EcoString>`。
- paged 分支里 `options` 就是 `PagedExtras`，其 `anchors` 字段类型为 `Vec<(Location, EcoString)>`（见 [crates/typst-bundle/src/lib.rs:L113](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L113)），在 `compile_document` 阶段先初始化为空（[L318](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L318)），到此处才被填上命名目的地。

这三步在 `bundle_impl` 里的调用顺序见 [crates/typst-bundle/src/lib.rs:L197-L200](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L197-L200)：先建统一内省器 → 算 `link_targets` → `create_link_anchors` 生成并就地写入 → `set_anchors` 把全局表回填进内省器。这个顺序是 4.5 节锚点能被消费的前提。

#### 4.2.4 代码实践

**实践目标**：确认两条分支的「返回形态」差异，以及它们如何汇成一张表。

**操作步骤**：

1. 在 [crates/typst-bundle/src/link.rs:L33-L46](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L33-L46) 里，分别标注 HTML 分支返回 `Either::Left(...)`、paged 分支返回 `Either::Right(...)`。
2. 回答：`Either` 在这里起什么作用？为什么需要它？

**需要观察的现象 / 预期结果**：`flat_map_iter` 的闭包必须返回**同一种**迭代器类型。HTML 返回的是 `HashMap` 迭代器（`(Location, EcoString)`），paged 返回的是 `slice` 迭代器（`&(Location, EcoString)` clone 后也是 `(Location, EcoString)`）——两者是不同的具体迭代器类型，用 `Either::Left/Right` 把它们统一成同一个 `Either<Iter, Iter>` 类型，才能满足闭包返回类型一致。运行无关，无需本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 HTML 分支用 `doc.as_mut()`（可变借用），而 paged 分支的 `doc` 不需要可变？

> **答案**：HTML 的锚点是 DOM `id`，必须**原地改写** `HtmlDocument` 的节点树才能注入，所以需要 `&mut`。paged 的 `PagedDocument` 是不可变共享的，锚点不写进文档本身，而是写到外挂的 `PagedExtras.anchors` 侧车，因此 `doc` 只需不可变引用。

**练习 2**：如果 bundle 里同时有一个 `a.html` 和一个 `a.pdf`，它们的锚点会互相干扰吗？

> **答案**：不会。`create_link_anchors` 为每个文档独立调用各自的生成函数，返回的锚点字符串都**局部于本文件**。即便是同名标签，`a.html` 和 `a.pdf` 各自的 `AnchorGenerator` 也是独立实例，互不影响。跨文档跳转时由 `LateLinkResolver` 再加上「相对路径」前缀来区分文件。

---

### 4.3 HTML 锚点：typst_html::create_link_anchors 原地注入 DOM id

#### 4.3.1 概念说明

HTML 路径的锚点生成由兄弟 crate `typst-html` 完成（`typst-bundle` 只是调用方）。它的核心思想是**遍历已经构建好的 DOM 节点树**，一旦遇到「位置在目标集合里」的节点，就给它分配一个 `id`。

复杂之处在于：一个链接目标在 DOM 里可能落到不同形态的节点上。`LinkElem` 的文档（[crates/typst-library/src/model/link.rs:L56-L78](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L56-L78)）列出了四种情况：

- 落到**单个元素**（如 `<h2>`）→ 直接给该元素加 `id`。
- 落到**单个文本节点** → 用 `<span>` 包裹文本，给 span 加 `id`。
- 落到**多个节点** → 给第一个节点加 `id`。
- 落到**没有节点**（空目标）→ 生成一个空 `<span>` 当锚点。

#### 4.3.2 核心流程

`typst_html::create_link_anchors` 用一个 `Work` 队列配合深度优先 `traverse`：

```
traverse(节点列表):
    for 每个节点:
        if 是开始标签 且 其 location ∈ targets:  入队 (loc, label)   # 标记"这个元素需要 id"
        if 是结束标签 且 仍在队列里:              该元素没产出任何节点 → 插入空 <span> 并分配 id
        if 是 Element 且 队列非空:                drain → 给当前元素分配 id，再递归它的子节点
        if 是 Text   且 队列非空:                drain → 用 <span> 包裹文本并分配 id
        if 是 Frame  且 队列非空:                drain → 给 SVG 元素分配 id
返回 loc → id 映射
```

「队列 + drain」机制处理的是：一个开始标签入队后，要等到遇到它**产生的第一个实际节点**才能把 id 挂上去（因为 id 只能挂在元素/文本上，不能挂在纯标签上）。

#### 4.3.3 源码精读

入口在 [crates/typst-html/src/link.rs:L26-L45](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/link.rs#L26-L45)，中文说明：若目标集为空直接返回（短路优化），否则用文档自己的内省器构造 `AnchorGenerator`，从根节点的 children 开始 `traverse`，原地改写 DOM 并返回 `loc → id` 映射。

`traverse` 的节点分派见 [crates/typst-html/src/link.rs:L48-L117](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/link.rs#L48-L117)，中文说明：按节点类型（开始标签/结束标签/Element/Text/Frame）决定是「入队」「插入空 span」还是「drain 分配 id」，对应上面四种 DOM 形态。

`id` 字符串本身由 `AnchorGenerator` 生成（4.4 节详述）。注意 HTML 这里调用的是 `generator.assign(element, label)`（[L197-L205](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/link.rs#L197-L205)），中文说明：若元素已有 `id` 属性就复用，否则调 `identify(label)` 生成一个新的并 `push_front` 到属性列表最前。

> 设计要点：HTML 锚点是 DOM 的一部分，所以生成动作**就地改变**了文档——这也是 u4-l2 提到 `export_html` 只接收根元素、且 HTML 内省器「非纯派生」的根因。

#### 4.3.4 代码实践

**实践目标**：亲手在 bundle 里造两个互相 `#link` 的 HTML 文档，观察生成的 DOM `id` 与跨文档 `href`。

**操作步骤**：

1. 准备一个 bundle 源文件 `main.typ`（示例代码）：

   ```typ
   // 示例代码
   #document("index.html")[
     = 首页 <home>
     前往 #link(<list>)[列表页]。
   ]

   #document("list.html")[
     = 列表 <list>
     返回 #link(<home>)[首页]。
   ]
   ```

2. 用开启了 bundle 特性的 typst 编译（构建与命令细节见 [u5-l4](u5-l4-cli-integration-and-e2e.md)，典型形如 `typst compile --features bundle main.typ`，**待本地验证**确切标志）。
3. 打开生成的 `index.html` 与 `list.html`，查看 `<h1>` 与 `<a>`。

**需要观察的现象**（依据源码推断，**待本地验证**）：

- `index.html` 里首页标题应得到 `id="home"`（标签 `home` 是合法 CSS id 且全 bundle 唯一，故直接复用，见 4.4 节 `identify`）。
- `index.html` 里那个链接的 `href` 应形如 `list.html#list`（跨文档：相对路径 `list.html` + 锚点片段 `#list`，见 4.5 节 `into_relative_uri`）。
- `list.html` 对称地有 `id="list"` 与 `href="index.html#home"`。

**预期结果**：你能指出 `id` 来自 `AnchorGenerator::identify`，`href` 的相对路径部分来自 `LateLinkResolver::resolve` 判定 `Cross`、片段部分来自锚点字符串。若本地无 bundle 构建，则改为纯源码阅读实践：对照 [crates/typst-library/src/model/link.rs:L104-L146](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L104-L146)（`Links in bundle export` 一节）逐条核对上述四种 DOM 形态的描述。

#### 4.3.5 小练习与答案

**练习 1**：如果某个被链接的目标在 DOM 里**什么节点都没产生**（比如一个空标题），HTML 锚点生成会怎么做？

> **答案**：`traverse` 在遇到该元素的**结束标签**时，发现它还在 `Work` 队列里（说明遍历期间没遇到任何实际节点来 drain 它），就调用 `remove` 插入一个空 `<span>`，并给这个 span 分配 `id`，作为跳转落点（见 [crates/typst-html/src/link.rs:L71-L78](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/link.rs#L71-L78)）。

**练习 2**：为什么 `typst_html::create_link_anchors` 在 `targets.is_empty()` 时直接 `return`（[L30-L33](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-html/src/link.rs#L30-L33)）？

> **答案**：没有目标就不需要任何 id，直接返回空映射，避免无谓地遍历整棵 DOM 树——这是对「这个文档没有被任何链接指向」常见情况的性能短路。

---

### 4.4 paged 锚点：create_paged_link_anchors、命名目的地与 AnchorGenerator

#### 4.4.1 概念说明

paged 路径（PDF/SVG）不原地改文档，而是把「目标 Location → 锚点字符串」的对收集进 `PagedExtras.anchors` 侧车，留待 `pdf_in_bundle`/`svg_in_bundle` 编码时读取并写成 PDF 命名目的地或 SVG `id`。

锚点字符串由 `AnchorGenerator` 统一生成，规则在 [crates/typst-library/src/model/link.rs:L80-L103](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L80-L103)（HTML 文档里描述，paged 复用同一套）：

- 优先**复用标签**：若元素有标签且标签是合法的 CSS id + URL 片段（仅字母数字/`-`/`_`，且不以数字或 `-` 开头，见 `can_use_label_as_id`），就用标签文本当锚点。
  - 标签在文档内**唯一** → 直接用，如 `<glossary>` → `glossary`。
  - 标签**重复** → 加 `-N` 后缀消歧，如两处 `<mylabel>` → `mylabel-1`、`mylabel-2`。
- 没有可用标签 → 生成 `loc-N`（`N` 递增）。

#### 4.4.2 核心流程

```
create_paged_link_anchors(doc, targets):
    elements = doc.introspector().elements()
    generator = AnchorGenerator::new(doc 的内省器)
    把 targets 从集合转成 Vec，并按 elements.loc_index(loc) 升序排序
    for target in 排序后的 targets:
        elem = elements.get_by_loc(target)        # 该位置对应的元素
        anchor = generator.identify(elem.label())  # 生成锚点字符串
        anchors.push((target, anchor))
    return anchors   # 写入 PagedExtras.anchors
```

**为什么必须先排序**：`AnchorGenerator` 是**有状态**的——`loc_counter` 和 `label_counter` 会随每次 `identify` 递增，消歧后缀 `-N` 的具体取值**取决于处理顺序**。而 `targets` 来自 `FxHashSet`，其迭代顺序在每个进程里是**随机**的（带随机种子）。如果不排序：

1. 同一个重复标签的 `-1`/`-2` 分配可能在不同运行里**互换**，导致锚点字符串不稳定。
2. 这会破坏**可复现构建**，也会让 `comemo` 记忆化失效（输入看似相同、输出却不同）。

按 `loc_index`（即文档内的稳定阅读顺序）排序，保证了「相同的源每次都产出完全相同的锚点字符串」。

#### 4.4.3 源码精读

`create_paged_link_anchors` 定义在 [crates/typst-bundle/src/link.rs:L69-L88](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L69-L88)，中文说明：把目标集合转成向量后**按 `loc_index` 排序**，再依次用 `AnchorGenerator::identify` 依据元素标签生成锚点字符串，收集成 `(Location, 锚点)` 列表。

`AnchorGenerator::identify` 见 [crates/typst-library/src/model/link.rs:L537-L554](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L537-L554)，中文说明：有合法标签则复用（唯一即直接用，重复则消歧），否则生成 `loc-N`。

判断标签能否复用为 id 的规则见 `can_use_label_as_id` [crates/typst-library/src/model/link.rs:L562-L566](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L562-L566)，消歧循环见 `disambiguate` [L570-L586](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L570-L586)，中文说明：用 `-{counter}` 后缀消歧，并通过 `introspector.label_count` 检查避免与已存在的 `<label>` 碰撞（比如生成的 `mylabel-2` 恰好是用户已有的标签，就继续递增）。

#### 4.4.4 代码实践

**实践目标**：理解「按 `loc_index` 排序」的必要性——这是本讲的实践重点之一。

**操作步骤**：

1. 阅读 [crates/typst-bundle/src/link.rs:L69-L88](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L69-L88)，找到第 78 行的 `targets.sort_by_key(|loc| elements.loc_index(loc));`。
2. 假设把这一行**删掉**，设想一个 bundle：`paper.pdf` 里有两处都挂着 `<sec>` 标签的元素，且都被别处链接。
3. 对照 [crates/typst-library/src/model/link.rs:L544-L549](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L544-L549)（重复标签走 `label_counter` 消歧），分析两次独立编译可能出现的结果。

**需要观察的现象 / 预期结果**（源码推导，无需运行）：删除排序后，因为 `FxHashSet` 迭代顺序随机，第一次编译可能把文档内**靠前**的 `<sec>` 分配为 `sec-1`、靠后的为 `sec-2`；第二次编译可能反过来。锚点字符串不稳定 → 跨文档链接的 `#sec-1` 在两次构建里指向不同元素 → 可复现性被破坏。所以排序是「正确性 + 可复现性 + 记忆化友好」的三重保障。

#### 4.4.5 小练习与答案

**练习 1**：`identify` 生成 `loc-N` 时，`N` 是从哪里来的？为什么它也需要排序保证？

> **答案**：`N` 来自 `AnchorGenerator` 的 `loc_counter` 字段，每次给无标签目标生成 id 时 `loc_counter += 1`（[L552-L553](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L552-L553)）。由于它随处理顺序递增，不排序就会让同一个目标在不同运行里拿到不同的 `N`，同样破坏稳定性。

**练习 2**：为什么用 `elements.get_by_loc(&target)` 而不是直接对每个 target 调 `identify`？那个 `if let Some(elem)` 的 `None` 分支何时触发？

> **答案**：`identify` 需要元素的**标签**（`elem.label()`）来决定是否复用标签为 id，所以必须先取出元素。`get_by_loc` 返回 `None` 的情况是：这个 target 位置在本文档内省器里**找不到对应元素**（例如它是文档自身的 Location，由 4.5 节的空锚点机制单独处理），此时跳过不分配，避免给不存在的元素造锚点。

---

### 4.5 让文档与资产自身可链接：空锚点与 LateLinkResolver 的消费

#### 4.5.1 概念说明

前面四节处理的是「文档内部元素」的锚点。但 bundle 还允许**直接链接到一个文档或 asset 整体**（把标签挂在 `#document(...)` 或 `#asset(...)` 上，见 [crates/typst-library/src/model/link.rs:L104-L110](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L104-L110)）。这类目标的 `Location` 不在任何一个文档的元素树里，所以 4.3/4.4 的遍历覆盖不到它们。

解决办法是在 `create_link_anchors` 末尾，为**每个 asset 和 document 自身的 Location** 插入一个**空字符串锚点** `EcoString::new()`。空锚点的语义是「链接到整个文件，不带片段」。它保证：任何被链接到的 Location，在内省器的 `anchor()` 查询里**都有结果**（空字符串也是 `Some`，而不是 `None`）。

#### 4.5.2 核心流程

锚点的「生产—存储—消费」完整链路：

```
生产：create_link_anchors → 全局表 anchors: Location → 锚点字符串
存储：introspector.set_anchors(anchors)   # 回填进 BundleIntrospector
查询：introspector.anchor(loc) → Option<&EcoString>   # 有则 Some(锚点)，无则 None
消费：LateLinkResolver::resolve(loc):
        from = 当前文档路径(base)
        to   = introspector.path(loc)      # 目标在哪个文件
        anchor = introspector.anchor(loc)?  # 取锚点（None 则链接失败）
        match (from, to):
          (None, None)            → Local { anchor }              # 单文件导出
          (Some, Some) 且 from==to → Local { anchor }              # 文档内跳转
          (Some, Some) 且 from≠to → Cross { from, to, anchor }    # 跨文档跳转
          其他                    → None（坏链）
      into_relative_uri:
        Local → "#{anchor}"            （空锚点也是 "#"，避免空 href 触发刷新）
        Cross → "相对路径" 或 "相对路径#anchor"（空锚点时不写末尾 #）
```

空锚点让「链接到整个文档」在 `into_relative_uri` 里产出**干净的相对路径**（不带 `#` 片段）。

#### 4.5.3 源码精读

空锚点循环见 [crates/typst-bundle/src/link.rs:L51-L60](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L51-L60)，中文说明：遍历所有 item，跳过 `Tag`，为每个 `Asset` 和 `Document` 自身的 `Location` 插入空字符串锚点，使它们整体可被链接。

回填与查询：`set_anchors` 见 [crates/typst-bundle/src/introspect.rs:L65-L67](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L65-L67)（把全局表存入 `self.anchors` 字段），`anchor()` 查询见 [crates/typst-bundle/src/introspect.rs:L132-L134](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/introspect.rs#L132-L134)（按 Location 取锚点字符串）。

消费端 `LateLinkResolver::resolve` 见 [crates/typst-library/src/model/link.rs:L675-L693](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L675-L693)，中文说明：用 `base`（当前文档路径）与 `introspector.path(loc)`（目标路径）判定 `Local`/`Cross`，锚点取自 `introspector.anchor(loc)`——若为 `None` 直接返回 `None` 表示链接无法解析。

`ResolvedLink` 的两种形态见 [crates/typst-library/src/model/link.rs:L698-L715](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L698-L715)，把解析结果转成相对 URI 见 `into_relative_uri` [L724-L748](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L724-L748)，中文说明：`Local` 输出 `#锚点`（空锚点也保留 `#`，注释说明 `#` 不会触发页面刷新而空 `href` 会）；`Cross` 用 `to.relative_from(from 的父目录)` 算相对路径并 percent-encode，空锚点时不写末尾 `#`。

> 串起来看：`bundle_impl` 在 [L197-L200](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/lib.rs#L197-L200) 把 `create_link_anchors` 的结果通过 `set_anchors` 灌进内省器；导出阶段每个文档构造 `LateLinkResolver::new(Some(本文档路径), &introspector)`（见 u4-l1），在编码每个链接时调 `resolve` → `into_relative_uri` 得到最终写入 PDF/SVG/HTML 的相对 URI。锚点表是连接「编译末尾」与「导出期」的那座桥。

#### 4.5.4 代码实践

**实践目标**：验证「空锚点」如何让链接到整个文档产出干净路径。

**操作步骤**：

1. 阅读 [crates/typst-bundle/src/link.rs:L51-L60](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-bundle/src/link.rs#L51-L60)，确认 asset/document 自身拿到的是 `EcoString::new()`（空串）。
2. 跟进 [crates/typst-library/src/model/link.rs:L724-L748](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L724-L748)，分别代入两种情况：
   - 链接到一个**元素**（锚点非空，如 `glossary`）→ `Cross` → `appendix.html#glossary`。
   - 链接到**整个文档**（锚点为空）→ `Cross` → `appendix.html`（无 `#`）。

**需要观察的现象 / 预期结果**（源码推导，无需运行）：你能解释「为什么空锚点对应没有 `#` 的纯路径」——因为 `into_relative_uri` 在 `anchor.is_empty()` 时走 `encoded`（不拼接 `#`），注释明说「Don't write a trailing `#` if linking to a full document」。

**如果无法本地运行**：这本身就是一个完整的源码阅读型实践，按上面两步代入即可得出结论。

#### 4.5.5 小练习与答案

**练习 1**：如果删掉 4.5 节那个「空锚点循环」，链接到一个文档整体（标签挂在 `#document` 上）会发生什么？

> **答案**：该文档自身的 `Location` 不会出现在 `anchors` 表里，于是 `introspector.anchor(loc)` 返回 `None`，`LateLinkResolver::resolve` 里的 `self.introspector.anchor(location)?` 提前返回 `None`，链接无法解析、成为坏链。所以空锚点不是装饰，而是「让文档/asset 整体可链接」的必要条件。

**练习 2**：`ResolvedLink::Local { anchor: "" }`（空锚点的文档内链接）经过 `into_relative_uri` 会得到什么？为什么保留 `#` 而不是空字符串？

> **答案**：得到 `"#"`（[L728](https://github.com/typst/typst/blob/9a1d84e9450c66adf9d2cb0068f4e0160e4ba34a/crates/typst-library/src/model/link.rs#L728)）。源码注释说明：空的 `href` 会触发浏览器重新加载当前页面，而 `#` 不会，所以即使是「链接到本文档整体」也保留 `#` 以避免副作用。

---

## 5. 综合实践

把本讲五个模块串起来，完成下面这个端到端任务（融合本讲实践重点）。

**任务**：构造一个最小 bundle，内含两个互相 `#link` 的 HTML 文档与一个被链接的 asset，然后**用源码解释每一条链接从「写出」到「变成 URI」的完整旅程**。

**示例代码**：

```typ
// 示例代码：main.typ
#document("index.html")[
  = 首页 <home>
  前往 #link(<list>)[列表页]，
  或下载 #link(<data>)[数据文件]。
]

#document("list.html")[
  = 列表 <list>
  返回 #link(<home>)[首页]。
]

#asset("data.json", read("data.json")) <data>
```

（其中 `data.json` 需你自行准备一个本地文件；若没有，可改为 `#asset("data.json", json.encode({}))` 并置于 `#context` 内。）

**操作步骤**：

1. 用开启了 bundle 特性的 typst 编译到目录（命令细节见 [u5-l4](u5-l4-cli-integration-and-e2e.md)，**待本地验证**确切标志与产物路径）。
2. 逐条追踪这三个链接，对每一条填出下表（先凭源码推断，再与本地产物对照）：

   | 链接 | 目标类型 | 走哪条生成分支 | 锚点字符串 | 最终 URI |
   | --- | --- | --- | --- | --- |
   | index 里 `#link(<list>)` | list.html 内元素 | HTML 注入 id | `list` | `list.html#list` |
   | list 里 `#link(<home>)` | index.html 内元素 | HTML 注入 id | `home` | `index.html#home` |
   | index 里 `#link(<data>)` | asset 整体 | 空锚点 | `""`（空） | `data.json`（无 `#`） |

3. 打开生成的 `index.html`，确认 `<h1>` 上有 `id`、`<a>` 的 `href` 是相对路径。

**需要观察的现象**：三个链接分别命中「HTML 元素锚点」「HTML 元素锚点」「asset 空锚点」三条路径；前两个带片段、第三个是纯路径。

**预期结果**：你能对照源码说清——目标是 4.1 节 `link_targets` 收集的（含 LinkElem 与 frame 两类）；分派在 4.2 节 `create_link_anchors`；HTML 的 `id` 由 4.3 节 `typst_html::create_link_anchors` 注入、asset 的空锚点由 4.5 节循环补上；锚点字符串规则在 4.4 节 `AnchorGenerator`；最终 URI 由 4.5 节 `LateLinkResolver::resolve` + `into_relative_uri` 产出。若本地无 bundle 构建，则改为纯源码追踪：按上表把「源码位置—行为」逐格写清楚即可。

## 6. 本讲小结

- **两类链接目标**：`link_targets()` 合并 `LinkElem::find_destinations`（逻辑链接）与各子文档的 `frame_link_targets`（frame 内原始链接），再用 `path()` 按**目标文档路径分桶**，供后续逐文档处理。
- **总分派**：`create_link_anchors` 并行遍历 `items`，对每个 `Item::Document` 按格式分派——HTML 走原地 DOM 改写，paged 走 `PagedExtras.anchors` 侧车——最后汇成一张全局 `Location → 锚点` 表。
- **HTML 锚点 = DOM id**：`typst_html::create_link_anchors` 用「队列 + drain」遍历节点树，根据目标落到元素/文本/多节点/空节点四种形态，分别加 `id`、包 `<span>` 或插空 span。
- **paged 锚点 = 命名目的地（侧车）**：`create_paged_link_anchors` **先按 `loc_index` 排序**再用 `AnchorGenerator::identify` 生成锚点；排序是可复现性与记忆化正确性的保障。
- **AnchorGenerator 规则**：优先复用合法且唯一的标签，重复标签加 `-N` 消歧，无标签则生成 `loc-N`。
- **空锚点**：asset 与 document 自身的 Location 各得一个空字符串锚点，使「链接到整个文件」成立；`LateLinkResolver::resolve` 据此产出 `Local`/`Cross`，`into_relative_uri` 把空锚点输出为不带 `#` 的纯相对路径。

## 7. 下一步学习建议

- 接下来读 [u5-l3 并行与记忆化](u5-l3-parallelism-and-memoization.md)：本讲的 `par_iter_mut`（锚点生成并行）与各 `export_*` 的 `#[comemo::memoize]` 正是那一篇的核心素材，结合本讲的「排序保证稳定锚点」会更理解记忆化对确定性的依赖。
- 然后做 [u5-l4 CLI 集成与端到端实践](u5-l4-cli-integration-and-e2e.md)：把本讲的「写 .typ → 编译 → 看产物」真正跑通，并对照源码走完 `export_bundle` 到 `write_virtual_fs` 落盘的完整链路。
- 若想深挖锚点字符串规则，可继续精读 `crates/typst-library/src/model/link.rs` 中 `AnchorGenerator`、`can_use_label_as_id`、`disambiguate` 三者，以及 `LinkElem` 文档里 `Links in HTML export` 与 `Links in bundle export` 两节。
