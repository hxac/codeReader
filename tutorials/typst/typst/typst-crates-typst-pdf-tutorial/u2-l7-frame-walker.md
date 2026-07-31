# Frame 遍历器：handle_frame 与 handle_group

## 1. 本讲目标

本讲深入 `convert.rs` 中**真正干活的两条递归函数**——`handle_frame()` 与 `handle_group()`。它们是整条 PDF 导出链路上「穿线」的核心：把一棵 `Frame` 树自上而下走一遍，沿途把每个节点翻译成对 krilla `Surface` 的绘制调用。

学完后你应当掌握：

- `FrameItem` 的六个变体（`Group` / `Text` / `Shape` / `Image` / `Link` / `Tag`）各自由谁处理、`handle_frame` 如何分派。
- `handle_group` 如何处理「子 frame 的变换 + 可选裁剪路径」，并与 `handle_frame` 形成互递归。
- `State` 三个字段（`transform` / `container_transform` / `container_size`）的含义，以及为什么 `container_*` 只在 **hard frame** 上注册。
- `FrameContext` 的 `states` 栈的 `push` / `pop` 纪律：为什么每个 item 都要 push 一次平移、pop 一次。

本讲承接 [u2-l5](u2-l5-convert-orchestrator.md)（`convert()` 编排），那里已经确认 `handle_frame` 是「内容分派器」、`GlobalContext` 是贯穿全程的状态容器；本讲把镜头拉近到这个分派器的内部齿轮。

## 2. 前置知识

### Typst 的排版产物是一棵 Frame 树

`typst-layout` 排版结束后的产物不是一串「字符 + 坐标」，而是一棵树：

- 每一页是一个根 `Frame`（一个固定尺寸的矩形区域）。
- `Frame` 内部是一组 `(Point, FrameItem)`：`Point` 是该 item 在本 frame 内的左上角位置，`FrameItem` 是具体内容。
- `FrameItem::Group(GroupItem)` 是「带变换、可选裁剪的子 frame」，从而形成递归树。

`FrameItem` 的定义在 typst-library 中（不是 typst-pdf 自己定义的，typst-pdf 只是它的消费者）：

```rust
pub enum FrameItem {
    Group(GroupItem),
    Text(TextItem),
    Shape(Shape, Span),
    Image(Image, Size, Span),
    Link(Destination, Size),
    Tag(Tag),
}
```

完整定义见 [crates/typst-library/src/layout/frame.rs:486-499](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L486-L499)（这部分代码属于 typst-library，不是本 crate，但理解它才能读懂遍历器）。

### Frame 有「软硬」之分

`Frame` 带一个 `kind: FrameKind`，分两种：

- `Soft`（默认）：**不**建立新的坐标系参考，渐变仍以最近的外层 hard frame 为参照。
- `Hard`：用自身尺寸作为坐标系参考，用于 page / block / box。

见 [crates/typst-library/src/layout/frame.rs:453-482](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/frame.rs#L453-L482)。这个「软硬」之分正是本讲 `container_*` 状态只注册在 hard frame 上的根因。

### 变换矩阵的复合顺序

Typst 的 `Transform` 是 2×3 仿射矩阵。关键方法是 `pre_concat`：

```rust
// self.pre_concat(prev) 返回 self ∘ prev，即「先作用 prev，再作用 self」
pub fn pre_concat(self, prev: Self) -> Self { ... }
```

完整定义见 [crates/typst-library/src/layout/transform.rs:333-343](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/transform.rs#L333-L343)。若记点为列向量，复合满足：

\[
\texttt{result}(p) \;=\; \texttt{self}\bigl(\texttt{prev}(p)\bigr)
\]

所以「pre_concat 一个平移」等于「把子内容的局部原点先平移，再套上已有的累计变换」。这一条是理解本讲所有 `push`/`pre_concat`/`pop` 的钥匙。

### 两套栈，不要混淆

本讲的难点在于运行时同时存在**两套独立的栈**：

| 栈 | 归属 | 作用 | 谁来 push/pop |
|---|---|---|---|
| **变换簿记栈** | `FrameContext.states` | 记录 Typst 侧累计变换、容器变换/尺寸 | `handle_frame` / `handle_group` 的 `fc.push()` / `fc.pop()` |
| **krilla 图形状态栈** | `Surface` 内部 | PDF 的实际图形状态（裁剪路径、标记内容序列） | `surface.push_clip_path`/`pop`、`surface.start_tagged`/`end_tagged` |

`handle_frame` **只**操作第一套栈；`handle_group` 两套都操作；`tags::page`/`tags::group` 等 hook 操作第二套栈。把两者分清，源码就不再绕。

## 3. 本讲源码地图

本讲几乎全部集中在**一个文件**：

| 文件 | 角色 |
|---|---|
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) | `State`、`FrameContext`、`handle_frame()`、`handle_group()`、以及调用它们的 `convert_pages()` 全在此 |

需要顺带参照的两个外部知识点：

- `Frame` / `FrameItem` / `GroupItem` / `FrameKind`：定义在 typst-library 的 `layout/frame.rs`（见上一节链接）。
- `tags::page` / `tags::group` / `handle_start` / `handle_end`：定义在 [src/tags/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs)，本讲只把它们当作「可选地在 surface 上插入标记内容」的黑盒 hook，深度留到 [u5-l19](u5-l19-tagged-pdf-overview.md)。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「数据结构 → 分派器 → 子 frame 处理器」的顺序，先讲 `State` 与 `FrameContext` 这两个簿记结构，再讲消费它们的两个函数。

### 4.1 State：变换状态与「容器」概念

#### 4.1.1 概念说明

`State` 回答两个问题：

1. **「我当前的累计变换是什么？」** —— 这样画一个点时，才能把它从 Typst 的局部坐标换算到页面坐标。
2. **「我当前最近的 hard 容器是什么？」** —— 渐变（gradient）和平铺图案（pattern）可以选 `RelativeTo::Parent`，意思是「填满我的父容器」。为了算这种渐变的几何，必须知道「最近的 hard frame 在页面上的变换和尺寸」。

第二个问题就是 `container_transform` / `container_size` 存在的理由。它本质是「一次快照」：每进入一个 hard frame，就把「此刻的累计变换」和「这个 frame 的尺寸」记下来，作为下游渐变的参照系。

#### 4.1.2 核心流程

`State` 有三个字段：

```
transform           : 当前累计变换（局部→页面）
container_transform : 最近 hard frame 的累计变换快照
container_size      : 最近 hard frame 的尺寸
```

关键操作：

- `new(size)`：构造一个干净状态——`transform` 与 `container_transform` 都是单位变换，`container_size` 为给定尺寸。页面的初始 `State` 就这样构造（容器 = 整页）。
- `register_container(size)`：**把当前 `transform` 快照进 `container_transform`，并更新 `container_size`**。这就是「进入 hard frame 时登记容器」。
- `pre_concat(t)`：`transform = transform.pre_concat(t)`，即把一个局部变换（通常是平移）累加进去。`container_*` **不动**。

伪代码：

```
register_container(size):
    container_transform ← transform   # 快照当前累计变换
    container_size      ← size

pre_concat(t):
    transform ← transform ∘ t         # container_* 保持不变
```

注意 `register_container` 与 `pre_concat` 的非对称：前者会改写容器参照系，后者只推进 `transform`。这正是「只有 hard frame 边界才会刷新容器」在数据上的体现。

#### 4.1.3 源码精读

`State` 的定义与三个字段（含原注释，点明 `container_*` 主要是为渐变和图案服务）：

[convert.rs:177-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L177-L185) —— 定义 `State` 结构，`container_transform` 注释为「first hard frame in the hierarchy」，`container_size` 同理。

构造一个干净状态（页面初始态、以及 tiling 图案都会用 `new`）：

[convert.rs:189-195](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L189-L195) —— `State::new`，`container_size` 设为传入尺寸，两个变换均为单位变换。

登记容器（只在 hard frame 调用）：

[convert.rs:197-200](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L197-L200) —— `register_container`：`container_transform = self.transform; container_size = size;`，一行快照、一行尺寸。

累加局部变换：

[convert.rs:202-204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L202-L204) —— `pre_concat`：`self.transform = self.transform.pre_concat(transform)`，不动 `container_*`。

`container_*` 真正被消费的地方在 `paint.rs` 的渐变/图案转换里。例如 `convert_gradient` 在 `RelativeTo::Parent` 时用 `state.container_size()` 作为渐变的填充尺寸：

[paint.rs:207-210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L207-L210) —— `convert_gradient` 中 `RelativeTo::Parent => (state.container_size(), Point::zero())`，直接取容器尺寸当渐变尺寸。

而 `correct_transform` 在 `RelativeTo::Parent` 时「先逆掉当前 transform、再加上 container_transform」，目的是抵消 krilla 里图形自带变换对填充的连带影响：

[paint.rs:411-425](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs#L411-L425)（paint.rs `correct_transform`） —— `state.transform().invert().pre_concat(state.container_transform())`。这两处共同说明：没有 `register_container` 提供的容器快照，`RelativeTo::Parent` 的渐变就无法正确对齐到父容器。

#### 4.1.4 代码实践

**目标**：用源码阅读验证「`register_container` 只刷新容器、`pre_concat` 只推进 transform」这一非对称关系。

**步骤**：

1. 打开 `convert.rs`，对比 `State::register_container`（[L197-200](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L197-L200)）与 `State::pre_concat`（[L202-204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L202-L204)）。
2. 在 `paint.rs` 中用 Grep 搜 `container_transform()` 与 `container_size()`，列出所有调用点。

**需要观察的现象**：`pre_concat` 的函数体里**完全不出现** `container_transform` / `container_size` 这两个词；而 `register_container` 同时改写它们。

**预期结果**：你会确认「推进 transform」与「刷新容器」是两条互不干扰的代码路径——这正是为什么遍历器敢在每个 item 上随便 `pre_concat` 平移，而不必担心污染容器参照系。

#### 4.1.5 小练习与答案

**练习 1**：假如把 `register_container` 删掉（即任何 frame 都不刷新容器），`RelativeTo::Parent` 的渐变会出什么问题？

**答案**：容器的 `container_size` 会永远停留在初始值（整页尺寸），`container_transform` 永远是单位变换。于是 page 内任何 block/box 上的 `RelativeTo::Parent` 渐变都会错误地按整页尺寸铺满，而不是按最近的 page/block/box 铺满；`correct_transform` 算出的补偿矩阵也会错位，渐变将明显偏离用户期望的位置。

**练习 2**：`State::new(size)` 把 `container_size` 设为传入 `size`、`transform`/`container_transform` 都设单位变换。为什么页面初始态这样做是安全的？

**答案**：页面本身就是层级里第一个 hard frame，其局部坐标 == 页面坐标（平移为 0），容器就是它自己。所以单位变换 + 整页尺寸作为初始容器参照是完全正确的。

---

### 4.2 FrameContext：状态栈与页面上下文

#### 4.2.1 概念说明

如果 `State` 是「某一层」的快照，那 `FrameContext` 就是「一层层叠加的栈」外加「单页范围的杂项状态」。它的核心职责是：

1. 维护 `states: Vec<State>` 这条**变换簿记栈**，让递归遍历可以「下去时 push、上来时 pop」。
2. 记录当前页的逻辑页号 `page_idx`（导出 tiling 图案时为 `None`）。
3. 收集本页范围内的**链接注记** `link_annotations`，按 `GroupId` 归集（tagged PDF 把链接与结构分组关联）。

`FrameContext` 的生命周期是「一页」：`convert_pages` 给每一页 `new` 一个新的 `FrameContext`，遍历完一页就把里面的 `link_annotations` 抽出来交给 tags 子系统。

#### 4.2.2 核心流程

变换栈的 push/pop 纪律：

```
push(): states.push(states.last().clone())   # 复制栈顶，压一份副本
pop() : states.pop()                          # 弹掉栈顶
state()    / state_mut(): 访问当前栈顶（最新副本）
```

关键设计：`push` 是**复制栈顶**，不是压空状态。这样每一层都继承了父层的累计变换与容器参照，只需在副本上叠加本层的增量。`pop` 把增量丢弃，自然回到父层状态。这是一套典型的「以复制实现回滚」的栈式作用域。

链接注记按 `GroupId` 归集：

```
push_link_annotation(id, annotation):
    link_annotations[id].push(annotation)      # 同一个 tag group 可能收集多个链接
get_link_annotation(id):
    link_annotations[id].last_mut()            # 取该 group 最新一个（用于追加多行 quadpoints）
```

页面结束后，`convert_pages` 会执行 `fc.link_annotations.into_values().flatten()`，把所有注记摊平交给 `tags::add_link_annotations`。

#### 4.2.3 源码精读

`FrameContext` 的三个字段：

[convert.rs:220-227](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L220-L227) —— `page_idx`、`states: Vec<State>`、`link_annotations`（`IndexMap<GroupId, SmallVec<[LinkAnnotation; 1]>>`）。`SmallVec<[..; 1]>` 暗示「同一个 group 通常只有 1 个链接，偶尔多个」。

构造函数——注意 `states` 初始化为**含一个** `State::new(size)` 的栈：

[convert.rs:230-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L230-L236) —— `FrameContext::new`：`states: vec![State::new(size)]`，这就是「第 0 层」（页面/tiling 的基底）。

变换栈的核心三件套：

[convert.rs:238-252](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L238-L252) —— `push`（`clone` 栈顶再压）、`pop`、`state` / `state_mut`（取栈顶）。`push` 用 `clone()` 实现「继承父层、隔离本层」。

链接注记的写入与读取：

[convert.rs:260-274](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L260-L274) —— `get_link_annotation`（取某 group 最新注记，用于追加矩形）、`push_link_annotation`（新建或追加）。

`convert_pages` 如何在一页结束时抽出席注记并交给 tags：

[convert.rs:168-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L168-L169) —— `let link_annotations = fc.link_annotations.into_values().flatten();` 紧接 `tags::add_link_annotations(gc, &mut page, link_annotations);`。

#### 4.2.4 代码实践

**目标**：理解 `push` 为什么是「复制栈顶」而不是「压入空状态」。

**步骤**：

1. 阅读 `FrameContext::push`（[L238-240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L238-L240)）。
2. 假设把 `push` 改成 `self.states.push(State::new(Size::zero()))`（压一个干净状态）。
3. 推演：此时 `handle_frame` 里 `fc.state_mut().pre_concat(Transform::translate(point.x, point.y))` 之后，`state.transform` 会变成什么？

**需要观察的现象 / 预期结果**：如果 push 的是空状态，`pre_concat` 平移会让 `transform` 从 `identity ∘ translate` 开始——**丢掉了父层的累计变换**。于是所有 item 都会画到页面左上角附近，完全错位。这说明「复制栈顶」是必须的：每一层必须在父层累计变换的基础上叠加，而不是从零开始。**待本地验证**：可在本地临时改写后 `cargo test -p typst-pdf`，预期大量布局相关测试失败。

#### 4.2.5 小练习与答案

**练习 1**：`link_annotations` 为什么用 `IndexMap<GroupId, SmallVec<[LinkAnnotation; 1]>>`，而不是 `Vec<LinkAnnotation>`？

**答案**：tagged PDF 需要把每条链接注记**与某个结构分组（`GroupId`）关联**（见 `tags::add_link_annotations` 里 `LinkAnnotationKind::Tagged(annot_id)` 分支），按 `GroupId` 索引是为了在解析 tag 树时能把注记挂到正确的分组节点上。`SmallVec<[..; 1]>` 则是因为绝大多数分组只有 1 条链接，堆上分配会浪费，所以内联 1 个元素、超过再溢出到堆。

**练习 2**：`FrameContext::page_size()`（[L254-258](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L254-L258)）返回的是 `states.first().container_size`。为什么取「第 0 层」而不是「栈顶」？

**答案**：第 0 层是页面基底，它的 `container_size` 在 `new` 时就被设为「frame 尺寸 + bleed」（见 `convert_pages` 里 `FrameContext::new(page_idx, typst_page.frame.size() + typst_page.bleed.sum_by_axis())`）。栈顶会随 item 的 push/pop 不断变化，不能代表「整页尺寸」；只有第 0 层稳定保存了页面总尺寸。

---

### 4.3 handle_frame()：Frame 树的分派器

#### 4.3.1 概念说明

`handle_frame` 是内容翻译的**总开关**：它接收一个 `Frame`，先登记容器（若 hard）、画背景填充、平移到内容原点，然后**逐个遍历** `frame.items()`，按 `FrameItem` 变体把工作路由给专门的翻译器：

| `FrameItem` 变体 | 路由到 | 说明 |
|---|---|---|
| `Group(g)` | `handle_group` | 递归处理子 frame（本讲 4.4） |
| `Text(t)` | `handle_text`（text.rs） | 文字与字形 |
| `Shape(s, span)` | `handle_shape`（shape.rs） | 几何图形（含本 frame 的背景填充也走这里） |
| `Image(image, size, span)` | `handle_image`（image.rs） | 栅格/SVG/PDF 图像 |
| `Link(dest, size)` | `handle_link`（link.rs） | 链接注记（写进 `fc.link_annotations`） |
| `Tag(Start/End)` | `tags::handle_start` / `tags::handle_end` | tagged PDF 的标记内容（受 `flags.tagged` 门控） |

注意：背景填充**不是** `FrameItem`，它是 frame 自带的 `fill`，由 `handle_frame` 自己构造一个矩形 `Shape` 并复用 `handle_shape` 画出来。

#### 4.3.2 核心流程

`handle_frame(fc, frame, padding, fill, surface, gc)` 的执行骨架（**两次入口 push + 一次每 item push**）：

```
push()                                  # 【入口 push #1】隔离本 frame 的状态改动
if frame.kind().is_hard():
    state.register_container(frame.size())   # 仅 hard frame 刷新容器参照

if let Some(fill) = fill:               # 画背景填充（矩形 Shape）
    handle_shape(... Geometry::Rect(frame.size()+padding).filled(fill) ...
                 ArtifactType::Background)

push()                                  # 【入口 push #2】
state.pre_concat(translate(padding.left, padding.top))   # 平移到内容原点（扣掉 padding）

for (point, item) in frame.items():
    push()                              # 【每 item push】
    state.pre_concat(translate(point.x, point.y))        # 该 item 在 frame 内的位置
    match item { ... 路由到各翻译器 ... }
    pop()                               # 【每 item pop】还原，不影响兄弟 item

pop()                                   # 【入口 pop #2】
pop()                                   # 【入口 pop #1】回到调用方状态
```

三个 push 的分工：

- **入口 push #1**：为本 frame 创建一块「草稿层」，使 `register_container` 与后续所有改动都不会污染调用方（父层）的状态。`handle_frame` 返回后，父层状态完全不变。
- **入口 push #2**：建立「内容原点层」，把 padding 平移隔离在此层；item 都相对这个原点定位。
- **每 item push**：给单个 item 的位置平移一个**作用域**。push→pre_concat→dispatch→pop，保证某个 item 的位置平移**不会泄漏到兄弟 item**——每个 item 都从同一个「内容原点层」出发。

这套 push/pop 纪律与 Frame 树的嵌套结构一一对应，是典型的栈式作用域（scope）实现。

#### 4.3.3 源码精读

`handle_frame` 全貌（约 65 行）：

[convert.rs:327-391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L327-L391) —— 整个函数。

入口 push + 仅 hard frame 登记容器：

[convert.rs:336-340](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L336-L340) —— `fc.push();` 然后 `if frame.kind().is_hard() { fc.state_mut().register_container(frame.size()); }`。**这是「container 只在 hard frame 注册」的直接证据**。

背景填充——构造一个矩形 Shape，复用 `handle_shape`，并标记为 `ArtifactType::Background`（无障碍语境下，背景填充属于「装饰工件」）：

[convert.rs:342-352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L342-L352) —— `Geometry::Rect(frame.size() + padding.sum_by_axis()).filled(fill)` 喂给 `handle_shape`。

内容原点层（第二次 push + padding 平移）：

[convert.rs:354-356](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L354-L356) —— `fc.push();` 紧接 `fc.state_mut().pre_concat(Transform::translate(padding.left, padding.top));`。

逐 item 遍历与分派（每个 item 都 push→pre_concat 该 item 的 `point`→match→pop）：

[convert.rs:358-385](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L358-L385) —— `for (point, item) in frame.items()` 循环，`match item` 把六个变体路由到 `handle_group` / `handle_text` / `handle_shape` / `handle_image` / `handle_link` / `tags::handle_start`·`handle_end`。注意 `Tag` 变体受 `flags.tagged` 门控，未标记的 tag 直接跳过。

收尾两次 pop，与入口两次 push 配对：

[convert.rs:387-388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L387-L388) —— `fc.pop(); fc.pop();`。

`convert_pages` 调用 `handle_frame` 的现场——它被包在 `tags::page` 闭包里，背景填充传的是 `typst_page.fill_or_transparent()`，padding 传的是 `typst_page.bleed`：

[convert.rs:155-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L155-L164) —— `tags::page(gc, &mut surface, |gc, surface| { handle_frame(&mut fc, &typst_page.frame, typst_page.bleed, typst_page.fill_or_transparent(), surface, gc) })`。

#### 4.3.4 代码实践

**目标**：确认「每 item 的位置平移是局部作用域」，即兄弟 item 互不影响。

**步骤**：

1. 阅读 item 循环（[L358-385](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L358-L385)）。
2. 假设 frame 有两个 item：item A 在 `(10, 20)`，item B 在 `(30, 40)`。
3. 手动推演：处理 A 时 push→`pre_concat(translate(10,20))`→画 A→pop；处理 B 时 push→`pre_concat(translate(30,40))`→画 B→pop。

**需要观察的现象**：画 B 时的栈顶 `transform` 等于「内容原点层」的变换（不含 A 的 `(10,20)`），即 B 的坐标基准和 A 完全独立。

**预期结果**：A 画在 `(内容原点 + (10,20))`，B 画在 `(内容原点 + (30,40))`，两者各自独立。若删掉每 item 的 `push`/`pop`，B 会被叠加到 A 之后（`(内容原点 + (10,20) + (30,40))`），位置全部错乱。这验证了「每 item push/pop」是必需的作用域隔离。

#### 4.3.5 小练习与答案

**练习 1**：背景填充为什么用 `frame.size() + padding.sum_by_axis()` 而不是 `frame.size()`？

**答案**：`convert_pages` 传给 `handle_frame` 的 `padding` 是页面出血量 `bleed`，而 `frame.size()` 不含 bleed。背景填充要覆盖到「frame + 四周 bleed」的整个区域（否则出血区域会留白），所以矩形尺寸要加上 `padding.sum_by_axis()`（水平 bleed 之和、垂直 bleed 之和）。注意此时 `state` 还**没有**做 padding 平移（那是第二次 push 之后的事），所以背景矩形从 frame 原点（含 bleed 偏移）开始铺满。

**练习 2**：`Tag(Start/End)` 变体里为什么还要判断 `flags.tagged`？`PdfOptions::tagged` 已经全局控制是否生成 tagged PDF 了，这里不是重复吗？

**答案**：两者粒度不同。全局 `options.tagged` 是「整篇文档要不要 tagging」；而单个 `Tag` 的 `flags.tagged` 表示「这个具体的 introspection tag 是否参与结构化标注」。即便文档开启了 tagging，某些内部 tag（如纯布局用的、非语义的）其 `flags.tagged` 可能为 false，遍历器就跳过它，不向 surface 发射标记内容。两层门控避免把无关 tag 写进结构树。

---

### 4.4 handle_group()：子 frame、变换与裁剪

#### 4.4.1 概念说明

`FrameItem::Group(GroupItem)` 是 Frame 树里**唯一**会产生递归的变体。一个 `GroupItem` 带：

- `frame: Frame` —— 子 frame。
- `transform: Transform` —— 作用于子 frame 的变换（旋转/缩放/平移）。
- `clip: Option<Curve>` —— 可选裁剪曲线。
- `parent: Option<FrameParent>` —— 逻辑父（用于 tagged PDF 的阅读顺序调整，本讲只当 hook 参数）。

`handle_group` 的职责就是：把 group 的 `transform` 累加进变换栈、可选地在 surface 上压一条裁剪路径、然后**递归调用 `handle_frame`** 处理子 frame。`handle_frame` 与 `handle_group` 因此形成**互递归**（mutual recursion），共同走完整棵树。

#### 4.4.2 核心流程

```
handle_group(fc, group, surface, gc):
    push()                                    # 【变换簿记栈 push】
    state.pre_concat(group.transform)         # 累加 group 自带变换

    tags::group(gc, fc, surface, group.parent, |gc, fc, surface| {
        # —— 以下在闭包内 ——
        clip_path = group.clip 经 convert_path 转 krilla Path，再用当前 transform 变换
        if let Some(clip_path):
            surface.push_clip_path(clip_path, NonZero)   # 【krilla 图形状态栈 push】压裁剪

        res = handle_frame(fc, &group.frame,
                           padding=zero, fill=None, surface, gc)   # 递归！

        if clip_path.is_some():
            surface.pop()                               # 【krilla 图形状态栈 pop】弹出裁剪
        res
    })

    pop()                                     # 【变换簿记栈 pop】
```

注意它**同时操作两套栈**：

1. 变换簿记栈：入口 `fc.push()` + `pre_concat(group.transform)`，出口 `fc.pop()`。
2. krilla 图形状态栈：`surface.push_clip_path` / `surface.pop()`（仅当有裁剪）。

裁剪路径有一个细节：`group.clip` 是一条 `Curve`，先在「局部坐标」用 `convert_path` 转成 krilla `Path`，**再用当前累计 `state.transform` 变换到页面坐标**，才能交给 surface（surface 的裁剪是在页面坐标系下生效的）。

#### 4.4.3 源码精读

`handle_group` 全貌（约 38 行）：

[convert.rs:393-430](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L393-L430) —— 整个函数。

入口 push + 累加 group 变换（变换簿记栈）：

[convert.rs:399-400](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L399-L400) —— `fc.push(); fc.state_mut().pre_concat(group.transform);`。

裁剪路径构建——`group.clip` 经 `convert_path` 转 `PathBuilder`，再用 `fc.state().transform.to_krilla()` 把它从局部坐标变换到页面坐标：

[convert.rs:402-415](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L402-L415) —— `tags::group(...)` 闭包内构造 `clip_path`：先 `convert_path(p, &mut builder)`，再 `p.transform(fc.state().transform.to_krilla())`，有则 `surface.push_clip_path(clip_path, FillRule::NonZero)`。

递归调用 `handle_frame`（这里 padding 传 0、fill 传 None——子 frame 的背景由它自己的 `frame.fill` 在 `handle_frame` 内部处理，group 不重复画背景）：

[convert.rs:417-418](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L417-L418) —— `handle_frame(fc, &group.frame, Sides::splat(Abs::zero()), None, surface, gc)`。

裁剪弹出（与 push 配对）：

[convert.rs:420-422](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L420-L422) —— `if clip_path.is_some() { surface.pop(); }`。

出口 pop（变换簿记栈）：

[convert.rs:427](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L427) —— `fc.pop();`。

#### 4.4.4 代码实践

**目标**：理解裁剪路径「先转 Path、再用 transform 变换」两步分离的原因。

**步骤**：

1. 阅读裁剪构建（[L403-411](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L403-L411)）。
2. 思考：为什么不能在 `convert_path` 阶段就直接把 curve 变换好？

**需要观察的现象**：`convert_path` 接收的是 typst 侧 `Curve`（局部坐标的点），输出 krilla `Path`；变换 `p.transform(...)` 发生在**得到 Path 之后**，用的是 `fc.state().transform`（即累加了 group 自身变换之后的页面坐标变换）。

**预期结果**：分两步是因为 `convert_path` 只负责「把曲线离散成 krilla 的线段序列」（纯粹的几何格式转换，不关心坐标系），而坐标变换是 krilla `Path` 自带的能力（`Path::transform`）。把职责分开，`convert_path` 就能保持纯粹、可复用（shape.rs 也复用它）。裁剪最终以**页面坐标**交给 surface，因为 krilla 的裁剪路径在当前图形状态的设备空间生效。**待本地验证**：可在一个带 `clip: rect(..)` + `rotate(..)` 的 group 上导出 PDF，观察裁剪区域是否随旋转正确倾斜。

#### 4.4.5 小练习与答案

**练习 1**：`handle_group` 调用 `handle_frame` 时为什么 `fill` 传 `None`？子 frame 的背景不是也该画吗？

**答案**：`handle_frame` 的 `fill` 参数只在**页面级**才非空——`convert_pages` 把 `typst_page.fill_or_transparent()`（页面背景）传给最外层的 `handle_frame`。`Frame` 结构体本身**没有** `fill` 字段（它只有 `size`/`baseline`/`items`/`kind`），`GroupItem` 也没有背景概念。所以 group 递归时 fill 传 `None`：背景是页面属性，不应在嵌套的 group 里重复绘制。`handle_frame` 内部若 `fill` 为 `None`，就跳过画背景那一段（见 [L342](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L342-L352) 的 `if let Some(fill)`）。

**练习 2**：`handle_group` 里 `fc.push()` 和 `surface.push_clip_path()` 是否必须配对各自的 pop？如果只 pop 了一边会怎样？

**答案**：必须各自配对。`fc.push()`/`fc.pop()` 管变换簿记栈，不配对会导致后续兄弟 item 的坐标基准错乱（栈不平衡）。`surface.push_clip_path()`/`surface.pop()` 管 krilla 图形状态栈，不配对会导致裁剪区域「泄漏」到后续内容——裁剪本应只作用于子 frame，却会一直生效到页尾。源码里用 `if clip_path.is_some() { surface.pop(); }` 严格保证只有 push 过才 pop，两边各自平衡。

---

## 5. 综合实践

把本讲四个最小模块串起来。**目标**：用伪代码画出 `handle_frame` 对一个含「背景填充 + 一段文字 + 一个图形」的 frame 的完整调用顺序，标出每次 push/pop 与 pre_concat 平移的位置；并解释为什么 container 只在 hard frame 上注册。

**任务背景**：假设有这样一个 frame（`kind = Hard`，无 padding，即 `convert_pages` 直接处理的页面级 frame）：

```
frame (Hard, size = W×H, fill = 白色)
  items:
    (point=(0,0),   Shape(线条))     # 一条分割线
    (point=(50,60), Text("Hi"))      # 一段文字
```

注意：背景填充不是 items 里的元素，而是 frame 自带的 `fill`。

**第 1 步：写出调用顺序（含栈层级标注）**

请按下表填写（答案见下）。设入口前 `states` 栈深度为 0：

| 步骤 | 代码动作 | 栈深度 | `transform` 变化 | `container_*` 变化 |
|---|---|---|---|---|
| 1 | `fc.push()`（入口 #1） | 0→1 | 不变（复制栈顶） | 不变 |
| 2 | `is_hard()` 为真 → `register_container(W×H)` | 1 | 不变 | **container_transform←当前transform, container_size←W×H** |
| 3 | 画背景：`handle_shape(Rect(W×H).filled(白), Background)` | 1 | 不变（handle_shape 不改 transform 栈） | 不变 |
| 4 | `fc.push()`（入口 #2） | 1→2 | 不变 | 不变 |
| 5 | `pre_concat(translate(0,0))`（padding=0） | 2 | transform∘translate(0,0)≈不变 | 不变 |
| 6 | item#1：`fc.push()` | 2→3 | 不变 | 不变 |
| 7 | `pre_concat(translate(0,0))`（线条位于(0,0)） | 3 | transform∘translate(0,0) | 不变 |
| 8 | `handle_shape(线条, Layout)` | 3 | 不变（不改 transform 栈） | 不变 |
| 9 | item#1：`fc.pop()` | 3→2 | 还原到步骤 5 后的状态 | 不变 |
| 10 | item#2：`fc.push()` | 2→3 | 不变 | 不变 |
| 11 | `pre_concat(translate(50,60))`（文字位于(50,60)） | 3 | transform∘translate(50,60) | 不变 |
| 12 | `handle_text("Hi")` | 3 | 不变（不改 transform 栈） | 不变 |
| 13 | item#2：`fc.pop()` | 3→2 | 还原到步骤 5 后的状态 | 不变 |
| 14 | `fc.pop()`（出口 #2） | 2→1 | 还原到步骤 2 后 | 不变 |
| 15 | `fc.pop()`（出口 #1） | 1→0 | 还原到入口前 | 还原到入口前 |

**关键观察**：

- 只有**步骤 2**改写了 `container_*`，且仅因为本 frame 是 Hard。全表其余 14 步对 `container_*` 都是「不变」。
- 文字 item 的 `(50,60)` 平移（步骤 11）在步骤 13 被 pop 掉，所以图形 item 的坐标基准不受文字 item 影响——兄弟 item 完全隔离。
- 步骤 2 的 `register_container` 写在入口 push #1（步骤 1）之后、入口 pop #1（步骤 15）之前，所以这个容器刷新的「作用域」是整个子树；当 `handle_frame` 返回（步骤 15 之后），父层状态连同 `container_*` 都回到入口前，不影响父层后续兄弟。

**第 2 步：解释「container 只在 hard frame 注册」**

请在你的笔记里回答以下三点（参考 4.1 与 `FrameKind` 的语义）：

1. 如果把这个 frame 改成 `Soft`，步骤 2 会被跳过，`container_*` 保持上一层的值。这意味着什么？——答：本 frame 不建立新坐标系参照，其内部 `RelativeTo::Parent` 的渐变仍以**更外层的 hard frame**（通常是 page）为参照，这正是 Soft frame 的定义（「follows its parent's size」）。
2. 为什么不在每个 item 上都注册容器？——答：item 不是 frame，没有「尺寸可作为坐标系」的语义；坐标系只在 frame 边界（且仅 hard frame）切换。
3. `register_container` 用「当前 transform」作 `container_transform`，而不是单位变换，为什么？——答：因为渐变的 `correct_transform` 需要把渐变从「当前局部坐标」映射到「容器所在页面坐标」，`container_transform` 必须记录「这个 hard frame 的原点在页面上的位置」，所以是当前累计变换而非单位变换。

**第 3 步（可选，源码阅读型）**：在 `convert.rs` 里用 Grep 搜 `register_container` 的所有调用点，确认全工程**只在** `handle_frame` 的 hard 分支调用它一次。预期结果：唯一调用点即 [L339](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L339)。

> 说明：本实践为源码阅读 + 手动推演型，未要求运行命令；若想实际验证，可在 `handle_frame` 的 push/pop 处临时加日志打印栈深度与 `transform`，导出一份测试 PDF 后观察输出（**待本地验证**）。

## 6. 本讲小结

- `handle_frame` 是 Frame 树的**分派器**：按 `FrameItem` 六变体路由到 `handle_group` / `handle_text` / `handle_shape` / `handle_image` / `handle_link` / tags hook；背景填充由它自己构造矩形 Shape 复用 `handle_shape` 绘制。
- `handle_group` 与 `handle_frame` 构成**互递归**：group 累加自身 `transform`、可选压裁剪路径，再递归 `handle_frame` 处理子 frame，从而走完整棵树。
- `State` 三个字段中，`transform` 是累计变换，`container_transform` / `container_size` 是「最近 hard frame」的快照，专供 `RelativeTo::Parent` 的渐变/图案使用。
- **容器只在 hard frame 注册**（`handle_frame` 里 `if frame.kind().is_hard()` 分支），因为 soft frame 不建立新坐标系参照。
- `FrameContext.states` 是一条「复制栈顶即 push」的变换簿记栈：入口两次 push、每个 item 一次 push/pop、出口两次 pop，保证每层与每个 item 的状态改动互不泄漏。
- 运行时有**两套独立栈**：`FrameContext.states`（变换簿记，Typst 侧）与 krilla `Surface` 内部图形状态栈（裁剪/标记内容）。`handle_frame` 只动前者，`handle_group` 两套都动。

## 7. 下一步学习建议

本讲讲清了「遍历骨架与变换簿记」，但分派出去的各个翻译器细节还没展开。建议接下来按内容类型逐个深入：

- **文字与字体** → [u3-l9 文字与字体：handle_text 与字形适配](u3-l9-text-and-fonts.md)：看 `handle_text` 如何把 `TextItem` 翻译成 krilla 文字绘制。
- **图形与几何** → [u3-l10 图形与几何：handle_shape](u3-l10-shapes-and-geometry.md)：看 `handle_shape`（本讲反复复用）的填充与描边逻辑。
- **链接** → [u4-l14 链接与目的地址](u4-l14-links-and-destinations.md)：看 `handle_link` 如何写入 `fc.link_annotations` 并解析目的地址。
- **渐变与图案** → [u3-l12 渐变与平铺图案](u3-l12-gradients-and-tiling.md)：本讲的 `container_*` 状态在那里被真正消费，建议配合阅读以形成闭环。
- **tagged PDF** → [u5-l19 tagged PDF 概览](u5-l19-tagged-pdf-overview.md)：本讲把 `tags::page` / `tags::group` / `handle_start` / `handle_end` 当作黑盒 hook，其内部机制留到第五单元。

如果想立刻加深对本讲的理解，建议再读一遍 `convert.rs` 的 [L327-L430](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L327-L430)，边读边在纸上面画栈的 push/pop，直到能脱稿复述「一个含背景+文字+图形的 hard frame」的完整调用顺序。
