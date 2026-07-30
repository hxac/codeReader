# PagedIntrospector：构建查询索引

## 1. 本讲目标

本讲是「页面与文档布局」单元（u3）的收尾。前面四讲（u3-l1～u3-l4）已经把 content 一路排成了最终的 `PagedDocument`：每一页的正文、页眉页脚、左右页边距、显示页码都已组装到位。但 Typst 还有一类功能必须依赖一份「已排版完成」的索引才能工作——`query`、`counter`、`state`、`outline`、`locate`、`label` 查询，以及 PDF 导出时的内部跳转链接。提供这份索引的对象，就是本讲的主角 `PagedIntrospector`。

学完本讲，你应当能够：

- 说清 `PagedIntrospector` 为什么是 `&[Page]` 的**纯派生产物**，以及它由哪几部分数据组成。
- 读懂构造过程 `discover_frame → discover_tag → start_insertion/end_insertion → finalize`，并解释带 `parent` 的 `Group` 如何被「整体插到父元素 start tag 之后」以修正跨帧内省顺序。
- 看懂 `ElementIntrospector` 的四种加速结构（`elems` / `keys` / `locations` / `labels`）与查询缓存 `QueryCache` 各自加速了什么查询。
- 看懂 `Introspector` trait 在分页目标上的实现：`query` / `page` / `position` / `page_numbering` 等方法分别返回什么。
- 解释 `PagedDocument::new` 里 introspector 为何**不参与 `Hash`**，以及这和 comemo 缓存正确性的关系。

## 2. 前置知识

本讲假设你已掌握 u2-l4（Locator/Tag/内省定位）与 u3-l4（页面 finalize）的内容。为承上启下，这里用一句话回顾几个关键概念（不展开，详见对应讲义）：

- **内省（introspection）**：编译期「回头看」已经排好的文档，回答「某个元素在第几页」「第 N 个标题是哪个」「`<label>` 指向哪里」之类的问题。Typst 采用**迭代收敛**：第一遍排版时索引还不完整，靠 comemo 记忆化反复重排，直到索引稳定（见 u2-l1）。
- **Tag**：随内容流进 frame 的「不可见但有序」标记，分 `Tag::Start(elem, flags)` 与 `Tag::End(loc, key, flags)`。只有 `flags.introspectable` 为真的 tag 才会进入索引。
- **Location**：元素 128 位的稳定身份。
- **PagedPosition**：一个 `page`（从 1 开始）加一个页面坐标 `point`，是分页目标里「位置」的具体形态。
- **Group / FrameParent**：`Frame` 树里唯一的非叶子节点是 `Group`（`GroupItem`），它可以携带一个 `parent: Option<FrameParent>`，把一段内容标注为「逻辑上属于某个父元素」。

本讲要回答的新问题是：当所有页都排好、所有 tag 都落进 frame 之后，**如何把这些散落在帧树各处的 tag，扫描、整理成一份支持高效查询的索引**。

## 3. 本讲源码地图

本讲跨两个 crate，主角在 typst-layout，但索引引擎的「内膛」其实在 typst-library。typst-layout 只负责「分页语义」那一层。

| 文件 | 所属 crate | 作用 |
| --- | --- | --- |
| `src/introspect.rs` | typst-layout | 定义 `PagedIntrospector` 与其构造器 `PagedIntrospectorBuilder`，实现 `Introspector` trait 的分页版本 |
| `src/document.rs` | typst-layout | 定义 `PagedDocument`，在 `new` 里调用 `PagedIntrospector::new` 派生索引；`Hash` impl 刻意排除索引 |
| `crates/typst-library/src/introspection/introspector.rs` | typst-library | 定义 `Introspector` trait、与目标无关的索引引擎 `ElementIntrospector<P>` 及其构造器 `ElementIntrospectorBuilder<P>`（含 `discover_tag` / `start_insertion` / `end_insertion` / `finalize` / `visit`） |
| `crates/typst-library/src/introspection/tag.rs` | typst-library | `Tag` 与 `TagFlags` 的定义 |
| `crates/typst-library/src/introspection/position.rs` | typst-library | `DocumentPosition` / `PagedPosition` 的定义 |
| `crates/typst-library/src/layout/frame.rs` | typst-library | `GroupItem`（含 `parent`）与 `FrameParent` 的定义 |

一句话分工：**typst-library 提供「与目标无关」的通用索引引擎，typst-layout 把它特化到「分页」这一种目标上**（位置类型换成 `PagedPosition`、补上页码/编号/补充信息、收集 PDF 链接目标）。

## 4. 核心概念与源码讲解

### 4.1 PagedIntrospector：pages 的纯派生产物

#### 4.1.1 概念说明

`PagedIntrospector` 是 typst-layout 对 `Introspector` trait 的分页实现。它的全部输入就是「已经排好的页序列 `&[Page]`」——不读源码、不依赖引擎、不带任何额外上下文。这一点至关重要：**索引是排版产物的派生物，而不是排版输入**。

它对外暴露两类能力：

1. **结构查询**：`query(selector)`、`query_first`、`query_label` 等，回答「文档里有哪些匹配元素、顺序如何」。
2. **定位查询**：`position(loc)`、`page(loc)`、`page_numbering(loc)` 等，回答「某个 Location 在第几页、什么坐标、页码格式是什么」。

实现上，它把「与目标无关」的绝大部分工作委托给一个泛型引擎 `ElementIntrospector<PagedPosition>`，自己只额外保存四样「分页特有」的数据。

#### 4.1.2 核心流程

构造与查询的整体形状如下：

```text
PagedDocument::new(pages, info)
        │
        │  PagedIntrospector::new(&pages)        // 唯一入口
        ▼
┌─────────────────────────────────────────┐
│ for each (i, page) in pages:            │
│   记录 page.numbering / page.supplement │
│   discover_frame(page.frame, id, to_pos)│  // 扫描帧树，收集 tag
│      to_pos = |pt| PagedPosition{i+1,pt}│
│ builder.finish(...)                      │
└─────────────────────────────────────────┘
        │
        ▼
   PagedIntrospector { elements, frame_link_targets, pages, page_numberings, page_supplements }
        │
        │  之后被 Introspector trait 的方法查询（query/page/position/...）
        ▼
   query(selector) / position(loc) / page_numbering(loc) ...
```

关键点：**`to_pos` 闭包把帧内的局部坐标 `pt` 绑定上「这是第几页」**，于是任意一个 tag 的位置最终都形如 `PagedPosition { page, point }`。页号 `page = i + 1` 来自页在切片中的下标——注意这是**物理页号**（第几张纸），与 `Page.number`（逻辑页号，可被 `counter(page)` 改写）不是一回事（见 u3-l4）。

#### 4.1.3 源码精读

先看 `PagedIntrospector` 的字段定义——五个字段正好对应它的全部职责：

[introspect.rs:22-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L22-L33) 定义了 `PagedIntrospector` 的五个字段：通用引擎 `elements`、链接目标集合 `frame_link_targets`、总页数 `pages`、按物理页号索引的 `page_numberings` 与 `page_supplements`。

```rust
pub struct PagedIntrospector {
    elements: ElementIntrospector<PagedPosition>,
    frame_link_targets: FxHashSet<Location>,
    pages: NonZeroUsize,
    page_numberings: Vec<Option<Numbering>>,   // 下标 = 物理页号 - 1
    page_supplements: Vec<Content>,            // 下标 = 物理页号 - 1
}
```

其中 `elements` 承担几乎全部结构查询；后四个字段是分页特有的「边带信息」。`page_numberings` / `page_supplements` 都是「按物理页号 - 1 取下标」的数组，因为页号在扫描时就是按下标分配的。

再看唯一构造入口 `new`：

[introspect.rs:38-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L38-L58) 标注了 `#[typst_macros::time(name = "introspect pages")]`，遍历每页记录编号与补充信息，并调用 `discover_frame` 扫描帧树，最后 `finish` 组装。

```rust
pub fn new(pages: &[Page]) -> PagedIntrospector {
    let mut builder = PagedIntrospectorBuilder::default();
    let mut page_numberings = Vec::with_capacity(pages.len());
    let mut page_supplements = Vec::with_capacity(pages.len());

    for (i, page) in pages.iter().enumerate() {
        let nr = NonZeroUsize::new(1 + i).unwrap();
        page_numberings.push(page.numbering.clone());
        page_supplements.push(page.supplement.clone());
        builder.discover_frame(&page.frame, Transform::identity(), &mut |point| {
            PagedPosition { page: nr, point }
        });
    }

    builder.finish(/* pages */ , page_numberings, page_supplements)
}
```

这里有两点值得注意：

- **`nr = 1 + i`**：位置里的页号用的是物理页号。这决定了后面 `page_numbering(loc)` 也是按物理页号取值。
- **每页一个新的 `to_pos` 闭包**：闭包捕获了 `nr`，把帧内坐标绑到「这一页」。`Transform::identity()` 是坐标变换的初始值（页面顶层无需额外平移）。

`new` 之上，`PagedIntrospector` 直接暴露了三个取值方法（[`position`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L61-L63)、[`elements`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L66-L68)、[`frame_link_targets`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L72-L74)），其余全部由 `Introspector` trait 的 impl 提供（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：确认「索引 = pages 的纯派生物」这一论断，并看清 `Page` 上有哪些字段会进入索引。

**操作步骤**：

1. 打开 [introspect.rs:38-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L38-L58)，列出 `new` 从每个 `page` 上读取的字段。
2. 对照 [document.rs:82-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L82-L105) 的 `Page` 定义，确认这些字段都来自 `Page` 本身。

**需要观察的现象**：`new` 只用到 `page.frame`、`page.numbering`、`page.supplement` 三项；`Page` 上的 `bleed` / `fill` / `number`（逻辑页号）**完全没有**进入索引。

**预期结果**：你能得出结论——「索引只关心「谁在哪里、页码格式如何」，对出血、背景色、逻辑页号这些导出/计数语义不感兴趣」。其中 `number`（逻辑页号）不进索引尤其合理：页号到 Location 的反查用的是**物理页号**（位置里存的就是物理页号），逻辑页号是 `counter(page)` 的事，由 4.3 的 `page_numbering` 配合编号结果来表达。

#### 4.1.5 小练习与答案

**练习 1**：`PagedIntrospector::new` 标了 `#[typst_macros::time(name = "introspect pages")]`。这个计时名说明「内省」在编译耗时统计里是单独的一项。结合 u2-l1 的迭代收敛，为什么这一步可能被反复执行？

> **参考答案**：因为内省依赖排版结果，而第一遍排版时索引还不完整（query 可能拿不到全部元素），需要重排。每次重排结束都会重新 `PagedIntrospector::new` 派生新索引，直到收敛。comemo 记忆化保证：只要 `pages` 没变，`new` 直接命中缓存、不会真重跑。

**练习 2**：`pages` 字段类型是 `NonZeroUsize`。若文档为空（`pages` 列表为空），`finish` 会传入什么？为什么类型上仍然合法？

> **参考答案**：见 [introspect.rs:53-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L53-L54)，传入 `NonZeroUsize::new(pages.len()).unwrap_or(NonZeroUsize::ONE)`。空文档时回退到 `1`（至少「有一页」的语义），避免 `NonZeroUsize::new(0)` 返回 `None` 的尴尬，同时 `pages()` 查询对任意 location 仍返回 `Some`。

### 4.2 构建索引：discover_frame 与 start/end_insertion

#### 4.2.1 概念说明

`PagedIntrospectorBuilder` 的核心是一棵帧树的扫描器 `discover_frame`。它要做两件事：

1. **把散落的 tag 收集成有序的 Start/End 事件流**，交给通用引擎 `ElementIntrospectorBuilder`。
2. **处理「逻辑元素跨越多帧」的错位问题**——这是本节（也是本讲）最精妙的地方。

什么叫「跨越多帧错位」？考虑一个脚注：它的**引用标记**在正文里，但它的**内容**被排到页面底部。又或者一个跨页表格的单元格、一个浮动体。这些元素的 tag 可能落在与「逻辑顺序」不一致的物理位置。如果直接按物理出现顺序排进索引，那么「在某元素之前/之后」这类查询就会出错。

Typst 的解法是：在排版阶段（flow/grid 等）把这些「逻辑属于同一父元素、但物理上被挪走了」的内容用一个带 `parent` 的 `Group` 包起来（见 u2-l3 的 `set_parent`）；扫描阶段遇到这种 group 时，**把它的内容暂时 diverted（分流）**，等遇到父元素自己的 start tag 时，再把分流内容「整体插到父元素之后」。

#### 4.2.2 核心流程

`discover_frame` 对帧树做一次深度优先遍历，维护一个累积变换 `ts`（把局部坐标换算到页面坐标）：

```text
discover_frame(frame, ts, to_pos):
  for (pos, item) in frame.items():
    match item:
      Tag(tag)      -> elements.discover_tag(tag, to_pos(pos.transform(ts)))
      Group(group)  -> ts' = ts · translate(pos) · group.transform
                      if group.parent is Some(p):
                          elements.start_insertion()           # 开一个新「分流槽」
                          discover_frame(group.frame, ts', to_pos)  # 子树 tag 进分流槽
                          elements.end_insertion(p.location)   # 分流槽内容挂到父 location
                      else:
                          discover_frame(group.frame, ts', to_pos)  # 正常进当前槽
      Link(dest, _) -> if dest is Location(loc): frame_link_targets.insert(loc)
      Text/Shape/Image -> 忽略
```

`start_insertion` / `end_insertion` 的语义（定义在 typst-library 的构造器里）是：

- `start_insertion()`：把当前正在收集的事件流 `sink` 暂存到栈上，开一个**空的新 sink** 接收后续事件。
- `end_insertion(parent)`：把新 sink 里收到的事件流（即这个 group 子树的所有 tag）整体取出来，按 `parent` 这个 Location 登记到一张 `insertions: MultiMap<Location, Vec<BuilderItem>>` 表里，再恢复栈顶的旧 sink。

换句话说：**带 parent 的 group 的内容不会按物理顺序混入主流，而是被「打包」挂在父 Location 名下**。等 `finalize` 阶段按主流顺序访问到父元素的 Start 事件时，再把这个包「展开」插到父元素正后方。这样，无论内容物理上被挪到哪里，逻辑上它永远是父元素的「紧邻后代」。

#### 4.2.3 源码精读

`discover_frame` 是本讲的「心脏」，定义在 typst-layout：

[introspect.rs:177-207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L177-L207) 遍历 frame 的每个 item，对 Tag 调 `discover_tag`、对 Group 累积变换后递归（带 parent 时用 `start/end_insertion` 分流）、对 Link 收集链接目标。

```rust
fn discover_frame<F>(&mut self, frame: &Frame, ts: Transform, to_pos: &mut F)
where F: FnMut(Point) -> PagedPosition,
{
    for (pos, item) in frame.items() {
        match item {
            FrameItem::Tag(tag) => {
                self.elements.discover_tag(tag, to_pos(pos.transform(ts)));
            }
            FrameItem::Group(group) => {
                let ts = ts
                    .pre_concat(Transform::translate(pos.x, pos.y))
                    .pre_concat(group.transform);

                if let Some(parent) = group.parent {
                    self.elements.start_insertion();
                    self.discover_frame(&group.frame, ts, to_pos);
                    self.elements.end_insertion(parent.location);
                } else {
                    self.discover_frame(&group.frame, ts, to_pos);
                }
            }
            FrameItem::Link(dest, _) => {
                if let Destination::Location(loc) = dest {
                    self.frame_link_targets.insert(*loc);
                }
            }
            FrameItem::Text(..) | FrameItem::Shape(..) | FrameItem::Image(..) => {}
        }
    }
}
```

几个要点：

- **坐标累积 `ts`**：进入一个 group 时，`ts' = ts · translate(pos) · group.transform`，`pre_concat` 保证子坐标先被 group 自身变换、再被父位移、最后被祖先变换。于是 `pos.transform(ts)` 始终给出**页面坐标系下的绝对点**。
- **Tag 不进 `frame_link_targets`，Link 不进 `elements`**：两条收集线互不干扰。`frame_link_targets`（[introspect.rs:199-202](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L199-L202)）专门服务于 PDF 导出——后者需要知道「文档里有哪些 Location 被链接指向」，以便为它们生成命名锚点。注意只收集 `Destination::Location`，外部 URL 不算。
- **只有带 `parent` 的 group 才分流**：普通 group（绝大多数，比如单纯的 `place`、变换）只是「透明地」递归进去，tag 直接进当前 sink。

`group.parent` 的类型是 `Option<FrameParent>`，`FrameParent` 带一个 `location: Location`：

[frame.rs:555-560](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L555-L560) 定义 `FrameParent { location: Location, inherit: Inherit }`——`location` 是逻辑父元素的 Location，`inherit` 表示子内容是否继承父样式。`discover_frame` 这里只用到了 `parent.location`。

真正的分流机制在 typst-library 的构造器里：

[introspector.rs:562-574](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L562-L574) 用「栈 + 临时 sink」实现分流：`start_insertion` 把当前 `sink` 压栈并清空；`end_insertion` 把新 `sink` 里的事件挂到 `insertions[parent]`，再恢复旧 `sink`。

```rust
pub fn start_insertion(&mut self) {
    self.stack.push(std::mem::take(&mut self.sink));
}

pub fn end_insertion(&mut self, parent: Location) {
    let elems = std::mem::replace(
        &mut self.sink,
        self.stack.pop().expect("insertion to have been started"),
    );
    self.insertions.insert(parent, elems);
}
```

`discover_tag` 本身则很简单——只把 introspectable 的 tag 翻译成 `BuilderItem::Start` / `BuilderItem::End` 推进**当前** sink（在分流区间内，当前 sink 就是那个临时槽）：

[introspector.rs:513-530](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L513-L530) 对 `Tag::Start` 用 `seen` 去重后推 `Start` 事件、对 `Tag::End` 登记 `keys` 并推 `End` 事件；`introspectable` 为假的 tag 一律忽略。

```rust
pub fn discover_tag(&mut self, tag: &Tag, position: P) {
    match tag {
        Tag::Start(elem, flags) => {
            if flags.introspectable {
                let loc = elem.location().unwrap();
                if self.seen.insert(loc) {                       // 同一 Location 只收一次
                    self.sink.push(BuilderItem::Start(elem.clone(), position));
                }
            }
        }
        Tag::End(loc, key, flags) => {
            if flags.introspectable {
                self.keys.insert(*key, *loc);                    // 供 measurement 用
                self.sink.push(BuilderItem::End(*loc));
            }
        }
    }
}
```

注意 `seen.insert(loc)` 的去重：同一个 Location 的 Start 事件只入队一次（重复表头、循环生成元素等场景下，同一逻辑元素可能有多枚 tag）。

那么分流出去的 `insertions` 何时被「展开」？在 `finalize` → `visit` 里：

[introspector.rs:598-631](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L598-L631) 的 `visit` 在处理一个 `Start` 事件时，先写入 `elems`、登记 `locations`/`labels`，**然后立即取出 `insertions[loc]` 递归展开**——这正是「整体插到父元素 start 之后」的落点。

```rust
BuilderItem::Start(elem, pos) => {
    let loc = elem.location().unwrap();
    let idx = elems.len();
    self.locations.insert(loc, idx..idx + 1);     // 先占 [idx, idx+1)
    if let Some(label) = elem.label() {
        self.labels.insert(label, idx);
    }
    elems.push((elem, pos));
    if let Some(insertions) = self.insertions.take(&loc) {  // ← 把分流内容插到此处
        for pair in insertions.flatten() {
            self.visit(elems, pair);
        }
    }
}
BuilderItem::End(loc) => {
    if let Some(entry) = self.locations.get_mut(&loc) {
        entry.end = elems.len();                  // 用最终末尾更新 range.end
    }
}
```

把 4.2.2～4.2.3 串起来就是：带 parent 的 group 内容被分流到 `insertions[parent.location]`；当主流按文档顺序走到父元素的 `Start` 时，`visit` 把这些内容紧贴着父元素展开进 `elems`；父元素的 `End` 再把 `locations[parent].end` 更新为展开后的末尾。于是父元素在 `elems` 里的下标区间 `[start, end)` **完整覆盖了它逻辑上的全部后代**，无论这些后代物理上被挪到了哪一页。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：亲手解释「带 parent 的 Group 遇到 `start_insertion`/`end_insertion` 时发生了什么」，并理解它为什么能修正跨帧内省顺序。

**操作步骤**：

1. 读 [introspect.rs:186-198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L186-L198)：确认带 `parent` 与不带 `parent` 两条分支的唯一区别就是多了 `start_insertion` / `end_insertion` 这对调用。
2. 读 [introspector.rs:560-574](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L560-L574)：用自己的话写出 `start_insertion` 把 `sink` 怎么了、`end_insertion` 把收集到的事件存到哪里。
3. 读 [introspector.rs:617-622](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L617-L622)：确认分流内容是在父元素 `Start` 之后被展开的。
4. 构造一个心智模型：假设脚注元素 F 的 start tag 在正文流（主流），但其内容被排版器包成一个 `parent = F.location` 的 group 放到页脚。画出 `discover_frame` 走完后 `insertions[F.location]` 的内容，以及 `finalize` 后 F 在 `elems` 里的区间。

**需要观察的现象 / 预期结果**：

- `start_insertion` = 把当前 `sink` 压栈、开空 sink；`end_insertion(parent)` = 把空 sink 里收到的事件（页脚那段内容的 tag）存进 `insertions[parent]`、恢复旧 sink。
- 因此 F 内容的 tag **不会**混在页脚的物理位置上进入 `elems`，而是被「挂起」。
- `finalize` 走到 F 的 `Start` 时（在正文位置），把挂起内容紧贴展开，所以 F 在 `elems` 里的区间 `[start, end)` 同时包含 F 自己和它页脚的全部子元素——逻辑顺序正确，`query(... .before(F))`、`within` 等才不会因「内容物理上在后面」而误判。

**延伸（选做）**：在 `src/flow` 或 `src/grid` 里搜索 `set_parent` 的调用点（例如 grid 单元格、脚注插入），确认「谁会被包成带 parent 的 group」。这就是分流机制的现实触发点。

> 说明：本实践为源码阅读型实践，不要求运行；若想验证，可在 `discover_frame` 的 `end_insertion` 分支前后临时加 `eprintln!` 打印 `parent.location`，编译一个含脚注的小文档观察输出。

#### 4.2.5 小练习与答案

**练习 1**：如果一段内容被包成带 `parent` 的 group，但父元素的 Start tag 因为某种原因**没有**出现在任何帧里（例如被 show 规则整个吞掉），`insertions[parent]` 会怎样？

> **参考答案**：分流内容仍会存进 `insertions[parent]`，但 `finalize` 时主流里永远遇不到该 Location 的 `Start` 事件，`visit` 不会去取它（`insertions.take(&loc)` 只在处理对应 `Start` 时触发）。这些事件最终**不会进入 `elems`**，相当于从索引中消失。这与「父元素不存在于文档」的语义一致。

**练习 2**：`discover_frame` 对 `Text` / `Shape` / `Image` 直接忽略。为什么索引不需要它们？

> **参考答案**：索引面向「元素级」查询（query/counter/label），其基本单位是带 Location 的元素，由 Tag 标记。文字/图形/图片是叶子绘制内容，没有 Location、也不参与 query 选择，所以对索引无价值——它们的信息已经体现在 `PagedPosition.point`（tag 的坐标）里了。

### 4.3 查询引擎：ElementIntrospector 与加速结构

#### 4.3.1 概念说明

`PagedIntrospector` 把绝大多数查询转发给 `ElementIntrospector<PagedPosition>`——一个**与具体目标无关**的泛型索引引擎（`P` 是位置类型，分页时是 `PagedPosition`，HTML 目标会是另一种）。理解了它，就理解了 `query` 一族方法的全部 internals。

`ElementIntrospector` 内部维护四张加速表 + 一个查询缓存，专门回答两类问题：

- **「有哪些元素、顺序如何」**：靠 `elems`（按文档顺序排好的扁平元素表，含后代）+ `locations`（每个元素覆盖自身的下标区间）+ `labels`（label → 下标）。
- **「测量时这个 key 该落到哪个 Location」**：靠 `keys`（key 哈希 → Location 列表），服务于 u2-l4 提到的「内省辅助定位」。

#### 4.3.2 核心流程

`ElementIntrospectorBuilder` 经 `finalize` 产出 `ElementIntrospector`，二者关系如下：

```text
discover_tag / discover_frame  ──►  BuilderItem 流 (sink + insertions)
        │ finalize()
        ▼
   visit() 遍历 BuilderItem：
     Start  -> elems.push((elem,pos)); locations[loc]=idx..idx+1; labels[label]=idx
              再展开 insertions[loc]（递归 visit）
     End    -> locations[loc].end = elems.len()
        │
        ▼
   ElementIntrospector { elems, keys, locations, labels, queries }
```

查询时，`query(selector)` 先用选择子的 128 位哈希查 `QueryCache`；未命中再按选择子类型（`Elem`/`Location`/`Label`/`Or`/`And`/`Before`/`After`/`Within`）分别求解，结果回填缓存。`Before`/`After`/`Within` 这类「有序」查询依赖 `locations` 给出的区间，用**按 `elem_index` 的二分查找**快速定位边界。

#### 4.3.3 源码精读

先看引擎的数据布局：

[introspector.rs:170-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L170-L190) 定义 `ElementIntrospector<P>` 的五块数据：扁平元素表 `elems`、测量用 `keys`、定位区间 `locations`、标签表 `labels`、查询缓存 `queries`。

```rust
pub struct ElementIntrospector<P> {
    elems: Vec<(Content, P)>,                 // 文档顺序的 (元素, 位置)，已含后代
    keys: MultiMap<u128, Location>,           // measurement 辅助定位
    locations: FxHashMap<Location, Range<usize>>,  // loc -> [start,end) 覆盖自身+后代
    labels: MultiMap<Label, usize>,           // label -> elems 下标
    queries: QueryCache,                      // selector 哈希 -> 结果
}
```

其中最关键的是 `locations`：每个元素的下标是一个**区间** `[start, end)`，`start` 是它自己，`end` 是它「最右后代之后」。这正是 4.2 里「父元素区间覆盖全部后代」的结果，也是 `Within` / `Before` / `After` 能正确判断「谁在谁里面/之前/之后」的依据。

`Introspector` trait 定义了所有目标必须实现的查询接口（`#[comemo::track]` 使其可被记忆化追踪，见 u2-l1）：

[introspector.rs:29-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L29-L89) 定义 `Introspector` trait：`query`/`query_first`/`query_unique`/`query_label`/`query_labelled`/`query_count_before`/`label_count`/`locator`/`pages`/`page`/`position`/`page_numbering`/`page_supplement`/`anchor`/`document`/`path`。

`PagedIntrospector` 对它的实现可以分成三组：

**第一组——纯转发 `elements`**（结构查询，[introspect.rs:78-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L78-L108)）：

```rust
fn query(&self, selector: &Selector) -> EcoVec<Content> { self.elements.query(selector) }
fn query_first(&self, selector: &Selector) -> Option<Content> { self.elements.query_first(selector) }
fn query_label(&self, label: Label) -> StrResult<&Content> { self.elements.query_label(label) }
fn query_count_before(&self, selector: &Selector, end: Location) -> usize { self.elements.query_count_before(selector, end) }
fn locator(&self, key: u128, base: Location) -> Option<Location> { self.elements.locator(key, base) }
// ...
```

这些方法 `PagedIntrospector` 一行都不写逻辑，直接交给通用引擎。

**第二组——定位查询，结合位置类型**（[introspect.rs:114-130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L114-L130)）：

```rust
fn page(&self, location: Location) -> Option<NonZeroUsize> {
    self.elements.position(location).map(|pos| pos.page)         // 取 PagedPosition.page
}
fn position(&self, location: Location) -> Option<DocumentPosition> {
    self.elements.position(location).copied().map(DocumentPosition::Paged)
}
fn page_numbering(&self, location: Location) -> Option<&Numbering> {
    let page = self.page(location)?;
    self.page_numberings.get(page.get() - 1)?.as_ref()            // 按物理页号取
}
fn page_supplement(&self, location: Location) -> Option<&Content> {
    let page = self.page(location)?;
    self.page_supplements.get(page.get() - 1)
}
```

这里 `page.get() - 1` 把物理页号换算成数组下标——呼应 4.1 里「`nr = 1 + i`」。`page_numbering` 返回的是该页的编号**格式**（如 `"1"` / `"i"` / `"A"`），而非编号值；编号值由 `Numbering::apply` 在用到时计算。

**第三组——分页目标「不支持」的能力**（[introspect.rs:132-142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L132-L142)）：

```rust
fn anchor(&self, _: Location) -> Option<&EcoString> { None }
fn document(&self, _: Location) -> Option<Location> { None }
fn path(&self, _: Location) -> Option<&VirtualPath> { None }
```

`anchor`（HTML 锚点）、`document` / `path`（bundle 多文档场景的归属与路径）都是 HTML 目标或 bundle 编译才需要的，纯分页文档一律返回 `None`。trait 注释 [introspector.rs:24-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L24-L27) 明确允许这种「按目标特性返回 `None`」的做法。

最后看 `query` 的核心——缓存与 `Before` 选择子的实现：

[introspector.rs:194-206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L194-L206) 用选择子哈希先查 `QueryCache`，命中则直接返回。

```rust
pub fn query(&self, selector: &Selector) -> EcoVec<Content> {
    let hash = typst_utils::hash128(selector);
    if let Some(output) = self.queries.get(hash) {
        return output;                       // 缓存命中
    }
    let output = match selector { /* 按 8 种选择子类型求解 */ };
    self.queries.insert(hash, output.clone());
    output
}
```

[introspector.rs:246-259](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L246-L259) 是 `Selector::Before` 的实现：先 `query(selector)` 拿全集，再用 `binary_search` 找到 `end` 的位置切分。

```rust
Selector::Before { selector, end, inclusive } => {
    let mut list = self.query(selector);
    if let Some(end) = self.query_first(end) {
        let split = match self.binary_search(&list, &end) {
            Ok(i) => i + *inclusive as usize,   // end 本身在列表里：按 inclusive 决定
            Err(i) => i,                         // 不在：插入点即切分
        };
        list = list[..split].into();
    }
    list
}
```

这里的 `binary_search` 按 `elem_index`（即元素在 `elems` 里的下标）比较，而 `elem_index` 来自 `locations[loc].start`。**正因为 4.2 把跨帧元素的区间理顺了，这里的二分才能给出符合逻辑顺序的结果**——4.2 与 4.3 在此处闭环。

> 补充：`QueryCache` 是 `RwLock<FxHashMap<u128, EcoVec<Content>>>`（[introspector.rs:686-697](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L686-L697)）。用读写锁是因为查询阶段可能并发读、且缓存是「派生中再派生」的可变状态；它不参与 4.4 的 Hash（见下）。

#### 4.3.4 代码实践

**实践目标**：验证 `locations` 的「区间覆盖后代」语义，并理解它如何让 `Within` 选择子工作。

**操作步骤**：

1. 读 [introspector.rs:274-335](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L274-L335) 的 `Selector::Within` 分支：它对每个祖先用 `loc_range` 拿到 `[start, end)`，再在 `list` 里二分定位属于该区间的元素。
2. 回到 [introspector.rs:598-631](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L598-L631) 的 `visit`，确认 `locations[loc]` 的 `end` 是在 `End` 事件时更新为「展开所有后代之后的末尾」。

**需要观察的现象 / 预期结果**：一个包含若干后代的父元素，其 `locations` 区间长度 = 1（自身）+ 后代数；`Within(ancestor)` 正是靠判断元素的 `elem_index` 是否落在某祖先的 `[start, end)` 内来筛选。如果 4.2 的分流机制不存在，跨页父元素的后代会落在区间之外，`Within` 就会漏选。

#### 4.3.5 小练习与答案

**练习 1**：`query_count_before(selector, end)` 是 `query(selector).before(end).len()` 的「优化版」（见 trait 注释 [introspector.rs:45-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L45-L47)）。看 [introspector.rs:394-405](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/introspector.rs#L394-L405)，它「优化」在哪里？

> **参考答案**：它复用了一次 `query(selector)` 的结果（且该结果本身被 `QueryCache` 缓存），然后只做一次 `binary_search` 得到计数，避免了再构造一个截断后的 `EcoVec`。counter / state 这类「只关心个数」的查询因此非常廉价。

**练习 2**：`page_numbering(loc)` 用的是 `page(loc)` 返回的物理页号。如果一个文档用 `set page(numbering: "i")` 让前几页显示罗马数字，`page_numbering` 会怎么表现？

> **参考答案**：它按 location 所在的**物理**页号去 `page_numberings` 数组取该页的编号格式 `"i"`（或 `None` 表示无页码）。返回的是格式对象，调用方再用它渲染具体编号。逻辑页号（`counter(page)` 的值）不在这里——那是 counter 子系统的事。

### 4.4 为何 introspector 不参与 Hash

#### 4.4.1 概念说明

`PagedDocument` 的 `Hash` 实现里**故意跳过了 introspector**。这不是疏忽，而是精心设计——它直接关系到 comemo 缓存的正确性与效率。

#### 4.4.2 核心流程

```text
PagedDocument { pages, info, introspector(Arc) }
        │  Hash::hash
        ▼
   只 hash pages + info；introspector 跳过
   理由：introspector 完全由 pages 派生，hash 它是冗余
```

#### 4.4.3 源码精读

[document.rs:48-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L48-L55) 的 `Hash` impl 只哈希 `pages` 与 `info`，注释点明原因。

```rust
impl Hash for PagedDocument {
    fn hash<H: Hasher>(&self, state: &mut H) {
        // The introspector is fully derived from the pages. Thus, there is
        // no need to hash it.
        self.pages.hash(state);
        self.info.hash(state);
    }
}
```

这一决定有三层理由：

1. **正确性上冗余**：introspector 的全部输入是 `pages`（的 `frame`/`numbering`/`supplement`）。两个 `pages`+`info` 相同的文档，introspector 必然相同；反之 introspector 不同 ⇒ pages 必然不同。所以 hash pages 已经足以区分，再加 introspector 不增加区分力。
2. **效率上必要**：introspector 里有 `QueryCache`（`RwLock<HashMap>`）这种「派生过程中累积的可变缓存」，对它做 Hash 既贵又无意义。
3. **架构上自洽**：`PagedDocument` 把 introspector 存为 `Arc<PagedIntrospector>`（[document.rs:20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L20)），克隆 `PagedDocument` 只是增加引用计数、共享同一份索引（[document.rs:27-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L27-L30)）。索引是「便宜可共享的派生物」，而非「需要参与身份的实质数据」。

这一点和 u2-l1 的 comemo 记忆化、u2-l4「introspector 不参与 Hash」一脉相承：**派生物不应进入缓存键**，否则既浪费又可能因为内部可变状态破坏哈希一致性。

#### 4.4.4 代码实践

**实践目标**：把「introspector 不参与 Hash」和「comemo 缓存命中」串起来看。

**操作步骤**：

1. 读 [document.rs:27-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L27-L30) 的 `PagedDocument::new`，确认 introspector 在此处由 `pages` 当场派生。
2. 结合 u2-l1：`layout_document` 是 `#[comemo::memoize]` 的，其缓存键由入参（content + styles + 可追踪的 engine 字段）哈希而成，结果 `PagedDocument` 的哈希又用于上层增量缓存。

**需要观察的现象 / 预期结果**：你会看到一条完整链条——「pages 决定 introspector」⇒「hash pages 即等价于 hash 整个文档身份」⇒「相同输入的二次排版直接命中 comemo 缓存，连 introspector 都不必重建」。如果误把 introspector 纳入 Hash，这条链条会被 `QueryCache` 的内部状态打乱。

> 待本地验证：若想实测，可在 `PagedIntrospector::new` 顶部临时加 `eprintln!("building introspector")`，对同一文档连续编译两次（开启增量），观察第二次是否不再打印——命中缓存时 `new` 不会被调用。

#### 4.4.5 小练习与答案

**练习**：`PagedDocument` 还实现了 `Output` trait（[document.rs:63-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L63-L79)），其中 `introspector()` 返回 `&dyn Introspector`，`target()` 返回 `Target::Paged`，`create` 委托给 `crate::layout_document`。这说明 `PagedDocument` 在 Typst 的「目标无关产物」抽象里扮演什么角色？

> **参考答案**：它是 `Target::Paged`（分页）这一目标的 `Output` 实现——`introspector()` 让上层（query/counter 等）能用统一的 `&dyn Introspector` 接口访问索引，`create()` 让「把 content 变成产物」的入口统一指向 `layout_document`。也就是说，`PagedDocument` + `PagedIntrospector` 一起，构成了「分页目标」对外暴露的全部产物语义。

## 5. 综合实践

把本讲三块内容（派生构造、分流修正、查询引擎）串成一个端到端的追踪任务。

**场景**：一个含若干标题、一个带 `<label>` 的图、以及若干脚注的小文档。编译后，`query(heading)`、`query(<fig>)`、脚注的 `locate` 都要正确工作。

**任务**：

1. **构造侧**：从 [introspect.rs:44-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/introspect.rs#L44-L51) 出发，说明每页的 `frame` 如何被 `discover_frame` 扫描；标题与图的 tag 走「主流」，脚注内容的 tag 因 `parent` 走「分流」。
2. **分流侧**：画出脚注 F 在 `insertions[F.location]` 的暂存，以及 `finalize` 时它被展开到 F 的 `Start` 之后的 `elems` 顺序。说明 F 的 `locations` 区间为何能覆盖页脚的全部脚注子元素。
3. **查询侧**：追踪一次 `query(heading)`——它命中/未命中 `QueryCache` 的路径、`elems` 如何被 `Selector::Elem` 过滤；再追踪一次 `query(<fig>.before(<sometext>))`——它如何用 `binary_search` + `elem_index` 切分列表。
4. **缓存侧**：解释为何第二次编译（pages 未变）时，`PagedIntrospector::new` 不会真重跑，整个索引直接由 comemo 缓存复用。

**交付物**：一张包含「帧树 → BuilderItem 流（含分流）→ elems + locations + labels → query 命中 QueryCache」四列的流程草图，并在分流那一列标注 `start_insertion` / `end_insertion` 的位置。

> 这是一个源码阅读 + 推理型综合实践，不需要运行项目；若要验证推理，可在 `discover_tag`、`start_insertion`、`end_insertion`、`visit` 四处临时加 `eprintln!`，编译上述小文档并对照输出。

## 6. 本讲小结

- `PagedIntrospector` 是 `&[Page]` 的**纯派生产物**：构造只读 `page.frame` / `page.numbering` / `page.supplement`，不读源码、不依赖引擎。
- 它把绝大部分查询转发给与目标无关的泛型引擎 `ElementIntrospector<PagedPosition>`，自己只额外保存 `frame_link_targets`（PDF 链接目标）、`pages`、`page_numberings`、`page_supplements` 四样分页特有数据。
- 构造核心是 `discover_frame`：扫描帧树，把 tag 收集成 Start/End 事件，并累积变换 `ts` 把局部坐标换算成 `PagedPosition`。
- 带 `parent` 的 `Group` 触发 `start_insertion`/`end_insertion`：其内容被分流到 `insertions[parent.location]`，在 `finalize` 的 `visit` 里**紧贴父元素 Start 之后展开**，从而修正跨帧元素的逻辑顺序，使父元素的 `locations` 区间完整覆盖其所有后代。
- `ElementIntrospector` 靠 `elems`（有序扁平表）+ `locations`（元素覆盖后代的区间）+ `labels` + `keys` + `QueryCache` 支撑 `query` / `Before` / `After` / `Within` 等查询；有序查询依赖按 `elem_index` 的二分查找，因此依赖 4.2 的区间正确性。
- `PagedDocument::new` 里 introspector 被 `Arc` 共享、且**不参与 `Hash`**——因为它是 pages 的派生物，hash pages 已等价、且其内部 `QueryCache` 不适合哈希；这是 comemo 缓存正确与高效的前提。

## 7. 下一步学习建议

本讲完成了「页面与文档布局」单元（u3）。至此你已经看完了从 `layout_document` 到最终 `PagedDocument` + 索引的**文档级**全链路。接下来的学习建议：

- **进入块级流（u4 单元）**：本讲多次提到「带 parent 的 group 由 flow/grid 排版器注入」。要真正看清 `set_parent` 在哪里被调用、脚注/浮动体如何触发重排，请进入 [u4-l1 flow 布局总览](u4-l1-flow-overview.md) 与 [u4-l4 flow 组合 compose：浮动体与脚注](u4-l4-flow-compose.md)。
- **补全内省的另一半**：本讲聚焦「索引如何构建与查询」。索引如何驱动 `counter` / `state` 收敛，以及 `Locator::measure` 如何用 `Introspector::locator`（本讲的 `keys` 表）完成测量定位，可阅读 typst-library 的 `introspection/counter.rs` / `locate.rs`（u2-l4 已铺垫）。
- **对照 HTML 目标**：`ElementIntrospector<P>` 是泛型的，`Introspector` trait 里返回 `None` 的 `anchor`/`document`/`path` 正是为 HTML/bundle 目标准备的。若日后阅读 typst 的 HTML 排版器，可对照本讲理解「同一引擎、不同位置类型与不同 trait impl」的复用方式。
