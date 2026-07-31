# Frame 场景树与 render_frame 派发

## 1. 本讲目标

学完本讲，你应该能够：

- 把 Typst 的排版结果 `Frame` 理解成一棵**层级化的场景图（scene graph）**，而不是一张扁平的画。
- 说出 `FrameItem` 的六种变体（`Group` / `Text` / `Shape` / `Image` / `Link` / `Tag`）各代表什么。
- 读懂 [`render_frame`](src/lib.rs#L186-L205) 这个递归函数，并对照它的 `match` 说出每一种元素被交给哪个子模块（`text::render_text` / `shape::render_shape` / `image::render_image` / `render_group`）。
- 解释为什么 `Link` 和 `Tag` 是空分支、为什么 `Text`/`Shape`/`Image` 都要做 `state.pre_translate(*pos)`，而 `Group` 却把 `pos` 单独传进 `render_group`。

本讲是 typst-render 的「分诊台」：[u1-l2](u1-l2-entry-points.md) 讲了画布怎么建、背景怎么填，但**画布里的内容是怎么一笔一笔画上去的**——答案就在 `render_frame` 对场景树的递归遍历里。搞懂这层派发，后面四篇进阶讲义（State 变换、Group 裁剪、形状、图像、文本）才有入口。

## 2. 前置知识

本讲承接 [u1-l2 渲染入口 render 与 RenderOptions](u1-l2-entry-points.md)。那里我们确立了：

- `render(page, opts)` 先建好 `sk::Pixmap` 画布、填好背景，然后在末尾调用 `render_frame(&mut canvas, state, &page.frame)` 开始画内容。
- `State` 是贯穿渲染递归的「随身背包」，其中 `transform` 字段记录了「当前画到哪、被怎么缩放/旋转了」。

本讲需要你先理解几个名词（正文会再展开）：

| 名词 | 直觉解释 |
| --- | --- |
| 场景图（scene graph） | 把一幅画面拆成「树」：根是整页，子节点是段落、图片、形状……子节点又可以嵌套子节点。渲染时从根开始递归往下画。 |
| `Frame` | Typst 排版的最终产物，就是场景图里的「一个节点」，持有自己的尺寸和一组带位置的子元素。 |
| `FrameItem` | `Frame` 里能装的「叶子/分支元素」的枚举类型，共六种。 |
| `pos`（位置） | 每个子元素相对其所在 `Frame` 左上角的偏移量（`Point`）。 |

一句话：**Typst 把「一页」表达成一棵 `Frame` 树，`render_frame` 就是遍历这棵树、按节点类型分派给各个子模块去画的函数。**

## 3. 本讲源码地图

本讲的主角仍然是 typst-render 的 crate 根文件，外加类型定义所在的 typst-library。

| 文件 | 作用 |
| --- | --- |
| `crates/typst-render/src/lib.rs` | 定义 [`render_frame`](src/lib.rs#L186-L205) 与 [`render_group`](src/lib.rs#L208-L262)，是本讲的派发中枢。 |
| `crates/typst-library/src/layout/frame.rs` | 定义 [`Frame`](../typst-library/src/layout/frame.rs#L16-L30)、[`FrameItem`](../typst-library/src/layout/frame.rs#L484-L499)、[`GroupItem`](../typst-library/src/layout/frame.rs#L514-L530)、[`FrameKind`](../typst-library/src/layout/frame.rs#L453-L470) 等场景图数据结构。 |

> 说明：本讲的永久链接分属两个 crate。typst-render 内的文件用 `src/lib.rs`；typst-library 的文件用相对仓库根的 `crates/typst-library/src/layout/frame.rs`。

## 4. 核心概念与源码讲解

### 4.1 Frame：排版结果的层级化场景图

#### 4.1.1 概念说明

很多人会以为排版引擎的输出是一张「大图」或者一段「PDF」。其实都不是。Typst 排版的直接产物是一个叫 `Frame` 的数据结构，它是一棵**树**：

- 一页文档 = 一个根 `Frame`（它的尺寸就是页面大小）。
- 根 `Frame` 里挂着一组子元素（段落、图片、形状……）。
- 其中「子帧」类型的元素（`Group`）自己又是一个 `Frame`，可以继续挂子元素。
- 于是 `Frame` 套 `Frame`，整页内容就长成一棵树。

这跟浏览器里的 DOM、游戏引擎里的场景图是同一类思路：**用树形结构表达「谁包含谁、谁相对谁定位」，渲染时自顶向下递归处理。**

为什么不用扁平的「画一条线、画一个字」指令列表？因为树形结构天然带上了「层级 + 局部坐标系」：每个 `Frame` 都有自己的尺寸和原点，子元素的位置都是**相对父帧左上角**算的，这样裁剪、变换、背景填充都能限定在一棵子树里，互不干扰。

#### 4.1.2 核心流程

一个 `Frame` 的内部构成可以画成：

```
Frame {
    size:   本帧的尺寸（宽高）
    items:  [(pos_a, FrameItem::...),
             (pos_b, FrameItem::...),
             ...]   ← 每个子元素都带一个相对本帧左上角的位置
    kind:   Soft | Hard   ← 软帧/硬帧（影响渐变坐标系，本讲先不深入）
}
```

渲染时，外层调用者（`render` 函数）把根 `Frame` 交给 `render_frame`，后者遍历 `items`，按 `FrameItem` 的种类分别处理。当遇到 `FrameItem::Group` 时，会**递归**地再次进入 `render_frame` 处理那棵子帧——这就是「树形遍历」的递归点。

#### 4.1.3 源码精读

`Frame` 结构体定义在 typst-library 里（注意它是个纯数据结构，与渲染无关，typst-pdf / typst-svg 也共用它）：

[crates/typst-library/src/layout/frame.rs:16-30](../typst-library/src/layout/frame.rs#L16-L30) 定义 `Frame`：它持有 `size`（尺寸）、`baseline`（基线，用于文字对齐）、`items`（子元素列表）、`kind`（软/硬帧）。

```rust
pub struct Frame {
    size: Size,
    baseline: Option<Abs>,
    items: Arc<LazyHash<Vec<(Point, FrameItem)>>>,
    kind: FrameKind,
}
```

关键看 `items` 字段的类型 `Vec<(Point, FrameItem)>`：**每个子元素都和一个 `Point` 配对**，这个 `Point` 就是该元素相对本帧左上角的偏移。这正是后面 `render_frame` 里 `for (pos, item) in frame.items()` 的来源。

注意两个细节：

- `Arc<LazyHash<Vec<...>>>`：`Frame` 是**不可变、共享、可哈希**的。一旦排版完成，`Frame` 就被冻结，多个渲染目标（PNG/SVG/PDF）可以共享同一份。`LazyHash` 表示哈希值按需计算并缓存——这跟 u3-l4 会讲到的 `comemo` 记忆化直接相关。
- `items()` 访问器只是返回这个 `Vec` 的切片迭代器，不做任何加工：

[crates/typst-library/src/layout/frame.rs:145-149](../typst-library/src/layout/frame.rs#L145-L149) `items()` 返回子元素及其位置的迭代器。

```rust
pub fn items(&self) -> std::slice::Iter<'_, (Point, FrameItem)> {
    self.items.iter()
}
```

#### 4.1.4 代码实践

1. **实践目标**：建立「`Frame` 是一棵嵌套树」的直觉。
2. **操作步骤**：
   - 打开 [crates/typst-library/src/layout/frame.rs:16-30](../typst-library/src/layout/frame.rs#L16-L30)，确认 `Frame` 只有四个字段。
   - 在同一文件里搜索 `pub fn push`（或 `pub fn push_frame`），看 `Frame` 是如何在排版阶段往 `items` 里追加子元素的——这能帮你理解这棵树是「自底向上长出来的」。
3. **需要观察的现象**：`push` 系列方法接收一个 `Point`（位置）和一个 `FrameItem`（内容），把它们配对后压入 `items`。
4. **预期结果**：你会看到排版阶段（typst-layout）不断 `push`，最终把一整棵 `Frame` 树装配好，再交给 typst-render 来画。
5. **备注**：typst-render 本身**只读**这棵树、不修改它——只读源码即可，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Frame` 用 `Vec<(Point, FrameItem)>` 而不是把位置直接存进 `FrameItem` 里？

> **参考答案**：位置是「元素相对其父帧的位置」，属于父子关系的信息，而 `FrameItem` 本身描述的是「这个元素是什么」。把位置外置成元组，可以让同一个 `FrameItem`（比如一个形状）在不同位置被复用，也让 `items()` 的返回值天然带坐标，遍历时直接拿到 `pos`。

**练习 2**：`Frame` 的 `items` 字段为什么用 `Arc<LazyHash<...>>` 包装？

> **参考答案**：`Arc` 允许多处共享同一份 `Frame`（排版结果只读、不可变，适合共享）；`LazyHash` 让 `Frame` 可以被哈希（用作缓存键），且哈希值惰性计算、只算一次——这对 typst-render / typst-svg 等多处使用 `comemo` 记忆化至关重要。

---

### 4.2 FrameItem：构成场景的六种元素

#### 4.2.1 概念说明

`FrameItem` 是一个枚举，列出了 `Frame` 里能装的所有「东西」。可以把它理解成场景图的「节点类型表」：

| 变体 | 携带的数据 | 直觉 |
| --- | --- | --- |
| `Group(GroupItem)` | 一个子帧 + 变换 + 可选裁剪 | 「这一片是一个整体，自成一组」——**树枝节点** |
| `Text(TextItem)` | 一串已经塑形好的字形 | 一行/一段文字 |
| `Shape(Shape, Span)` | 几何形状 + 填充/描边 | 矩形、曲线、线条等矢量图形 |
| `Image(Image, Size, Span)` | 图像 + 目标尺寸 | 光栅图（PNG/JPG）、SVG 或嵌入的 PDF |
| `Link(Destination, Size)` | 链接目标 + 尺寸 | 超链接区域（点击跳转） |
| `Tag(Tag)` | 内省元数据 | 给 Typst 查询系统用的标记 |

其中 `Group` 是**唯一能让树继续往下长**的变体（它内部又是一个 `Frame`）；其余五个都是**叶子**。`Link` 和 `Tag` 比较特殊：它们**不带任何可见像素**，在位图渲染里会被直接忽略——原因在 4.4 节展开。

#### 4.2.2 核心流程

`FrameItem` 本身不驱动任何流程，它只是个数据标签。真正的流程发生在 `render_frame` 的 `match` 里：每拿出一个 `FrameItem`，就根据它的变体决定「交给谁处理」。可以把这套分派想象成医院的分诊台：

```
拿出一个 FrameItem
  ├─ Group  →  交给 render_group（递归处理子帧）
  ├─ Text   →  交给 text::render_text
  ├─ Shape  →  交给 shape::render_shape
  ├─ Image  →  交给 image::render_image
  ├─ Link   →  忽略（空分支）
  └─ Tag    →  忽略（空分支）
```

这就是 typst-render 内部 `mod image; mod paint; mod shape; mod text;` 四个子模块存在的根本原因——它们各自负责一类场景元素的绘制。

#### 4.2.3 源码精读

`FrameItem` 的定义非常简洁：

[crates/typst-library/src/layout/frame.rs:484-499](../typst-library/src/layout/frame.rs#L484-L499) 定义六种 `FrameItem` 变体。

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

几个值得注意的点：

- `Shape`、`Image` 都附带一个 `Span`。`Span` 是源码定位信息（类似「这段内容来自 `.typ` 文件的第几行」），用于报错和调试，**不参与渲染**——所以 `render_frame` 里写成 `FrameItem::Shape(shape, _)`，用 `_` 把 `Span` 丢掉。
- `Image(Image, Size, Span)` 里的 `Size` 是图像要被画成的**目标尺寸**（已经过排版缩放），而不是图像的原始像素尺寸。
- `Link(Destination, Size)`：`Destination` 是链接要去的地方（网页 URL、文档内位置等），`Size` 是链接热区的范围。
- `Tag(Tag)`：`Tag` 承载的是 Typst **内省（introspection）** 系统的元数据（如标签、文档结构、查询锚点）。

#### 4.2.4 代码实践

1. **实践目标**：体会「同一种数据结构服务多个渲染目标」。
2. **操作步骤**：
   - 在 typst 仓库里搜索 `FrameItem::Link` 和 `FrameItem::Tag` 的使用点（用 `Grep` 搜 `FrameItem::Link`、`FrameItem::Tag`）。
   - 对比 typst-render（本 crate）和 typst-pdf / typst-svg 对这两种元素的处理差异。
3. **需要观察的现象**：
   - 在 typst-render 里，`Link`/`Tag` 是空分支。
   - 在 typst-pdf / typst-svg 里，`Link` 通常会被写成可点击的链接注解（PDF 的 annotation、SVG 的 `<a>`），`Tag` 可能被写成文档结构/无障碍信息。
4. **预期结果**：你会直观感受到——**位图格式没有「交互」概念**，所以链接和元数据对 PNG 毫无意义；而矢量/结构化格式（PDF/SVG）却能保留它们。
5. **备注**：这是源码阅读型实践，不需要编译运行。

#### 4.2.5 小练习与答案

**练习 1**：`FrameItem::Shape(Shape, Span)` 里的 `Span` 为什么在 `render_frame` 里被绑定成 `_`？

> **参考答案**：`Span` 是源码定位（用于报错、IDE 跳转、debug），不携带任何绘制信息。渲染只需要 `Shape` 本身，所以用 `_` 忽略 `Span`。

**练习 2**：六种 `FrameItem` 里，哪一个是「树枝节点」（能让树继续嵌套）？为什么？

> **参考答案**：只有 `Group(GroupItem)`。因为 `GroupItem` 内部持有一个 `Frame`（`frame: Frame`），而 `Frame` 又可以装新的 `FrameItem`，从而形成 `Frame → Group → Frame → …` 的无限嵌套。其余五个变体都是叶子，不含子 `Frame`。

---

### 4.3 GroupItem：让场景树可以无限嵌套

#### 4.3.1 概念说明

`GroupItem` 是「一组子元素的容器」。它做三件事：

1. **装一棵子帧**（`frame: Frame`）——这是嵌套的来源。
2. **带一个变换**（`transform: Transform`）——可以对整组内容统一做平移、缩放、旋转。
3. **可选地裁剪**（`clip: Option<Curve>`）——用一条曲线把组内超出范围的内容剪掉。

一个典型的例子：一个「带圆角裁剪的图片缩略图」。排版时会把图片放进一个 `GroupItem`，给 `transform` 设上缩放、给 `clip` 设上圆角矩形曲线。渲染时整组先被变换、再被裁剪，最后才画到画布上。

#### 4.3.2 核心流程

`GroupItem` 自身只是数据，处理它的流程在 [`render_group`](src/lib.rs#L208-L262) 里（本讲只看它与派发的关系，裁剪/变换的细节留待 [u2-l2](u2-l2-groups-clipping-masks.md) 展开）：

```
render_group(canvas, state, pos, group):
  1. 把 group.transform 转成 tiny-skia 的 Transform
  2. 用 pos（位置）和 group.transform 组合出新的 state
     （Hard 帧还要额外更新 container_transform、size——u2-l2 详讲）
  3. 若 group.clip 存在：把裁剪曲线转成 Mask，与父遮罩求交
  4. 用更新后的 state 递归调用 render_frame 画 group.frame
```

注意第 4 步：`render_group` 最后会**再次调用 `render_frame`**——这就是树的递归下降点。

#### 4.3.3 源码精读

[crates/typst-library/src/layout/frame.rs:514-530](../typst-library/src/layout/frame.rs#L514-L530) 定义 `GroupItem`：一个带变换和可选裁剪的子帧容器。

```rust
pub struct GroupItem {
    pub frame: Frame,          // 子帧（递归的源头）
    pub transform: Transform,  // 整组变换
    pub clip: Option<Curve>,   // 可选裁剪曲线
    pub label: Option<Label>,  // 标签（用于查询，与渲染无关）
    pub parent: Option<FrameParent>, // 逻辑父节点（文档顺序，与渲染无关）
}
```

本讲只需关注前三个字段：`frame`（要画的子帧）、`transform`（整组变换）、`clip`（裁剪）。`label` 和 `parent` 服务于 Typst 的查询/内省系统，与位图渲染无关。

再看 `render_group` 的开头，注意它是如何**自己**完成位置+变换的组合的：

[crates/typst-render/src/lib.rs:208-223](src/lib.rs#L208-L223) `render_group` 在内部用 `state.pre_translate(pos).pre_concat(sk_transform)` 把「位置」和「组的变换」叠加到 `state` 上。

```rust
fn render_group(canvas: &mut sk::Pixmap, state: State, pos: Point, group: &GroupItem) {
    let sk_transform = to_sk_transform(&group.transform);
    let state = match group.frame.kind() {
        FrameKind::Soft => state.pre_translate(pos).pre_concat(sk_transform),
        FrameKind::Hard => state
            .pre_translate(pos)
            .pre_concat(sk_transform)
            // ...（Hard 帧还要更新 container_transform 与 size）
    };
    // ...（裁剪处理，然后递归 render_frame）
}
```

这一段解释了 4.4 节的一个关键问题：**为什么 `render_frame` 遇到 `Group` 时不在原地 `pre_translate`，而是把 `pos` 原样传给 `render_group`？** 因为 `Group` 除了位置 `pos`，还自带一个 `transform`（甚至 `clip`、`FrameKind`），这些必须**在一起**组合才有意义，所以这件事只能在 `render_group` 内部完成。

`render_group` 的最后一行是把更新后的 state 交给 `render_frame` 继续画子帧：

[crates/typst-render/src/lib.rs:261](src/lib.rs#L261) 递归点：用带遮罩的 state 调用 `render_frame` 画 `group.frame`。

```rust
render_frame(canvas, state.with_mask(mask), &group.frame);
```

#### 4.3.4 代码实践

1. **实践目标**：在源码里「亲眼看到」递归。
2. **操作步骤**：
   - 打开 [src/lib.rs:208-262](src/lib.rs#L208-L262) 的 `render_group`。
   - 找到它最后调用 `render_frame` 的那一行（L261）。
   - 再回到 [render_frame](src/lib.rs#L189-L191) 里调用 `render_group` 的那一行。
3. **需要观察的现象**：`render_frame` 调 `render_group`，`render_group` 又调 `render_frame`——两者构成**相互递归（mutual recursion）**。
4. **预期结果**：你能画出 `render_frame ⇄ render_group` 的调用环，并理解正是这个环让 `Frame` 树可以被遍历到任意深度。
5. **备注**：待本地验证的只是「调用关系」，纯静态阅读即可确认，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`GroupItem` 的 `clip: Option<Curve>` 为什么是 `Option`？

> **参考答案**：不是所有组都需要裁剪。`Option<Curve>` 表示「要么没有裁剪（`None`，整组内容原样画出），要么有一条裁剪曲线（`Some`，超出曲线范围的内容被剪掉）」。把它设计成 `Option` 比用「空曲线」表示「不裁剪」更清晰，也避免了无意义的几何运算。

**练习 2**：为什么 `render_group` 要接收 `pos: Point` 作为独立参数，而不是让调用方先 `pre_translate` 好？

> **参考答案**：因为 `Group` 的位置 `pos` 必须和它自带的 `transform`、`FrameKind`（以及裁剪）**一起**组合到 `state` 上，且 `FrameKind::Hard` 分支还要用 `pos` 去更新 `container_transform`（见 [src/lib.rs:220](src/lib.rs#L220)）。如果在 `render_frame` 里就提前 `pre_translate(pos)`，`render_group` 就拿不到原始 `pos` 去做这些组合，逻辑会出错。所以 `pos` 必须原样传进去。

---

### 4.4 render_frame：遍历与派发的总枢纽

#### 4.4.1 概念说明

[`render_frame`](src/lib.rs#L186-L205) 是本讲的真正主角。它做的事情极其朴素：

> 拿到一个 `Frame`，遍历它的 `items`，对每个 `(pos, item)` 按 `item` 的类型，分派给对应的子模块去画。

它本身**不画任何东西**，只负责「分诊」。这种「中央派发 + 子模块各司其职」的结构让 typst-render 的 5 个文件职责清晰：`lib.rs` 是骨架与派发，`text.rs`/`shape.rs`/`image.rs`/`paint.rs` 各管一摊。

#### 4.4.2 核心流程

`render_frame` 的完整控制流：

```
fn render_frame(canvas, state, frame):
    for (pos, item) in frame.items():        # 遍历本帧所有子元素
        match item:
            Group(group)  → render_group(canvas, state, pos, group)
                            # 注意：state 不预先 pre_translate，
                            # pos 交给 render_group 内部处理
            Text(text)    → text::render_text(canvas, state.pre_translate(pos), text)
            Shape(shape)  → shape::render_shape(canvas, state.pre_translate(pos), shape)
            Image(img,sz) → image::render_image(canvas, state.pre_translate(pos), img, sz)
            Link(_,_)     → {}   # 空：位图没有链接
            Tag(_)        → {}   # 空：元数据不产生像素
```

派发目标一览表：

| `FrameItem` 变体 | 派发目标 | 是否在原地 `pre_translate(*pos)` |
| --- | --- | --- |
| `Group(GroupItem)` | 本文件的 `render_group` | **否**——`pos` 作为参数传入，在 `render_group` 内部组合 |
| `Text(TextItem)` | `text::render_text` | 是 |
| `Shape(Shape, Span)` | `shape::render_shape` | 是 |
| `Image(Image, Size, Span)` | `image::render_image` | 是 |
| `Link(Destination, Size)` | （空分支） | — |
| `Tag(Tag)` | （空分支） | — |

#### 4.4.3 源码精读

[crates/typst-render/src/lib.rs:186-205](src/lib.rs#L186-L205) `render_frame`：遍历 `frame.items()`，按 `FrameItem` 种类派发到子模块。

```rust
fn render_frame(canvas: &mut sk::Pixmap, state: State, frame: &Frame) {
    for (pos, item) in frame.items() {
        match item {
            FrameItem::Group(group) => {
                render_group(canvas, state, *pos, group);
            }
            FrameItem::Text(text) => {
                text::render_text(canvas, state.pre_translate(*pos), text);
            }
            FrameItem::Shape(shape, _) => {
                shape::render_shape(canvas, state.pre_translate(*pos), shape);
            }
            FrameItem::Image(image, size, _) => {
                image::render_image(canvas, state.pre_translate(*pos), image, *size);
            }
            FrameItem::Link(_, _) => {}
            FrameItem::Tag(_) => {}
        }
    }
}
```

逐行解读三个关键设计：

**① 为什么 `Text`/`Shape`/`Image` 都要 `state.pre_translate(*pos)`？**

`pos` 是该元素相对其所在 `Frame` 左上角的偏移。而 `state.transform` 此刻累积的是「从画布原点到本 `Frame` 左上角」的所有祖先变换。每个子模块（比如 `shape::render_shape`）在画的时候，都假设**自己画在原点 (0,0)**——它不知道自己在版面里的位置。所以 `render_frame` 必须先用 `pre_translate(*pos)` 把「本元素的位置」叠加到 `state.transform` 上，子模块才能在正确的坐标落笔。`pre_translate` 是「在已有变换之前再叠加一个平移」，保证局部坐标先平移、再受祖先变换作用，顺序正确。

**② 为什么 `Group` 不在原地 `pre_translate`，而是把 `pos` 传给 `render_group`？**

如 4.3 节所述，`Group` 自带 `transform`/`clip`/`FrameKind`，`pos` 必须和它们一起组合；且 `FrameKind::Hard` 分支要用 `pos` 更新 `container_transform`。所以 `render_frame` 把原始 `state` 和 `pos` 分别交给 `render_group`，由后者统一处理。

**③ 为什么 `Link` 和 `Tag` 是空分支？**

- `Link`：链接是「可点击的跳转」。PNG/JPG 这类**位图只有像素、没有交互**，一个像素不可能「点击跳转」。链接信息只在 PDF（annotation）/ SVG（`<a>`）这类支持交互或结构的格式里才有意义。所以位图渲染直接忽略它。
- `Tag`：`Tag` 承载的是 Typst 内省系统的元数据（标签、查询锚点、文档结构），它**本身不产生任何可见像素**，纯粹是给 Typst 的查询机制和导出器的结构化输出用的。对位图而言没有任何东西可画，于是也是空分支。

这两个空分支并不是「没写完」，而是**位图渲染的固有边界**——理解了这一点，就理解了为什么 typst-render 相比 typst-pdf/typst-svg 是「有损」的（丢掉了链接和结构信息）。

#### 4.4.4 代码实践

1. **实践目标**：亲手把派发表填出来，确认每种 `FrameItem` 去了哪里。
2. **操作步骤**：
   - 打开 [src/lib.rs:186-205](src/lib.rs#L186-L205)。
   - 对 `match` 的六个分支，逐一写出派发目标（见下方「待填写表格」）。
   - 用 `Grep` 在 `crates/typst-render/src/` 下搜索 `pub fn render_text`、`pub fn render_shape`、`pub fn render_image`，确认这些子模块函数真实存在、签名与调用一致。
3. **需要观察的现象**：六个分支里，四个有实际处理（Group/Text/Shape/Image），两个为空（Link/Tag）；四个有处理的分支里，三个在原地 `pre_translate(*pos)`，唯独 `Group` 把 `pos` 单独传走。
4. **预期结果**：你应当能不看答案地复述那张「派发目标 + 是否 pre_translate」表，并解释三个「为什么」。
5. **备注**：源码阅读型实践，不需要编译运行。

**待填写表格**（请自行补全）：

| `FrameItem` 变体 | 派发到哪个函数 | `pre_translate(*pos)`？ | 为什么 |
| --- | --- | --- | --- |
| `Group` | `render_group` | 否 | `pos` 要与 group 自带的 transform/clip/FrameKind 一起组合 |
| `Text` | ？ | ？ | ？ |
| `Shape` | ？ | ？ | ？ |
| `Image` | ？ | ？ | ？ |
| `Link` | ？ | — | ？ |
| `Tag` | ？ | — | ？ |

#### 4.4.5 小练习与答案

**练习 1**：假设把 `render_frame` 里 `FrameItem::Shape` 分支改成 `shape::render_shape(canvas, state, shape)`（去掉 `pre_translate(*pos)`），会出现什么视觉问题？

> **参考答案**：形状会被画到**本帧的原点 (0,0)**，而不是它该在的 `pos` 位置。也就是说，页面上所有形状都会堆到所属 `Frame` 的左上角，布局全乱。`pre_translate(*pos)` 的作用正是把每个元素搬到它该在的位置。

**练习 2**：用一句话解释 `render_frame` 与 `render_group` 为什么是「相互递归」。

> **参考答案**：`render_frame` 在遇到 `Group` 时调用 `render_group`，而 `render_group` 在完成变换与裁剪后又调用 `render_frame` 去画子帧；两者互相调用，共同把 `Frame` 树从根递归遍历到每一个叶子。

**练习 3**：为什么说 typst-render 相比 typst-pdf 是「有损」的？请结合 `FrameItem::Link` 与 `FrameItem::Tag` 说明。

> **参考答案**：因为 `Link`（超链接）和 `Tag`（结构/内省元数据）在位图渲染里被空分支丢弃了。位图（PNG）只保留可见像素，无法表达「可点击跳转」和「文档结构语义」；而 PDF 能把这些写成 annotation 和结构树。所以同一棵 `Frame` 树，typst-render 的输出信息量少于 typst-pdf。

---

## 5. 综合实践

把本讲学的「场景树 + 派发」串起来，完成下面这个贯穿性小任务。

**任务：手工模拟 `render_frame` 对一棵小 `Frame` 树的派发。**

假设有一棵极简的 `Frame` 树（用缩进表示嵌套）：

```
根 Frame（页面）
├─ (pos=(70,50))  Shape：一个矩形
├─ (pos=(70,80))  Text：一行文字 "Hello"
└─ (pos=(0,0))    Group（transform=scale(2,2)，无 clip）
   └─ 子 Frame
      ├─ (pos=(10,10)) Image：一张图
      └─ (pos=(10,40)) Tag：一个查询锚点
```

请完成：

1. **写出派发序列**：按 `render_frame` 的遍历顺序，列出每一步会调用哪个函数。例如第一步是 `shape::render_shape(canvas, state.pre_translate((70,50)), 矩形)`。
2. **标出递归点**：在哪一步发生了 `render_frame → render_group → render_frame` 的相互递归？进入子帧时，`state` 的 `transform` 相比根帧多了哪些叠加？
3. **判断空分支**：树里的 `Tag` 在渲染时会发生什么？为什么？
4. **解释坐标**：为什么 `Image` 的位置是 `(10,10)`，但它在最终画布上的实际位置不仅取决于这 `(10,10)`，还取决于外层 `Group` 的 `transform=scale(2,2)` 和 `Group` 自身的 `pos=(0,0)`？

**参考思路**（建议你先自己写，再对照）：

1. 派发序列：
   - `shape::render_shape(state.pre_translate((70,50)), 矩形)`
   - `text::render_text(state.pre_translate((70,80)), "Hello")`
   - `render_group(state, pos=(0,0), group)` → 内部 `state.pre_translate((0,0)).pre_concat(scale(2,2))` → 递归 `render_frame`：
     - `image::render_image(state'.pre_translate((10,10)), 图)`
     - `Tag` → 空分支，什么都不画。
2. 递归点：处理 `Group` 那一步。进入子帧时 `state.transform` 在根帧变换的基础上，先叠加了 `pre_translate((0,0))`（即 `Group` 的位置），再叠加了 `scale(2,2)`（即 `group.transform`）。
3. `Tag` 走 `FrameItem::Tag(_) => {}` 空分支，不产生任何像素——因为它是内省元数据，与可见画面无关。
4. 因为 `Image` 在子帧里的局部位置 `(10,10)` 先被 `pre_translate` 叠加，再被外层 `Group` 的 `scale(2,2)`（以及 `Group` 的 `pos=(0,0)`）整体作用。所以它在画布上的最终落点大致是 `((0+10)×2, (0+10)×2) = (20,20)` 量级（忽略根帧自身变换）。这正是「树形局部坐标系」的意义：每个节点的位置都是相对父帧的，最终位置由从根到叶的所有变换**连乘**决定。

> 说明：精确的坐标合成涉及 `State` 的变换矩阵细节，那是 [u2-l1 State 状态与坐标变换](u2-l1-state-and-transforms.md) 的主题。本练习只需把握「派发顺序 + 递归点 + 局部坐标被祖先变换叠加」这三点。

## 6. 本讲小结

- Typst 的排版产物是一棵 **`Frame` 场景树**，每个 `Frame` 持有尺寸和一组 `(位置, FrameItem)` 子元素。
- `FrameItem` 有六种变体：`Group`（树枝）、`Text`/`Shape`/`Image`（可见叶子）、`Link`/`Tag`（不可见、不产生像素）。
- `Group(GroupItem)` 是唯一的嵌套来源，自带 `transform` 与可选 `clip`，是场景树能无限往下长的关键。
- [`render_frame`](src/lib.rs#L186-L205) 是派发中枢：遍历 `items`，按 `FrameItem` 种类分派到 `render_group` / `text::render_text` / `shape::render_shape` / `image::render_image`。
- `Text`/`Shape`/`Image` 都在原地 `state.pre_translate(*pos)` 把元素搬到它该在的位置；唯独 `Group` 把 `pos` 传给 `render_group` 内部处理（因为还要叠加组自带变换、处理裁剪与软/硬帧）。
- `Link` 和 `Tag` 是空分支：位图没有交互概念、元数据也不产生像素，所以 typst-render 相比 typst-pdf 是「有损」的。

## 7. 下一步学习建议

本讲只讲了「派发到哪」，没讲「到了之后怎么画」。接下来按渲染递归的自然顺序深入：

1. **[u2-l1 State 状态与坐标变换](u2-l1-state-and-transforms.md)**：`state.transform` 到底是怎么一步步叠加的？`pre_translate`/`pre_concat`/`to_sk_transform` 的矩阵含义是什么？——这是理解本讲里「坐标被祖先变换连乘」的钥匙。
2. **[u2-l2 Group 渲染、裁剪与遮罩](u2-l2-groups-clipping-masks.md)**：把本讲一笔带过的 `render_group` 彻底讲透，包括 `FrameKind::Soft`/`Hard` 的区别、裁剪曲线如何变成 `Mask`。
3. 之后再按 `Shape` → `Image` → `Text` 的顺序，分别进入 `shape.rs` / `image.rs` / `text.rs` 三个子模块的内部。

建议你先把本讲的派发表背熟，再带着「这个子模块函数收到的 `state` 是怎么来的」这个问题去读 u2-l1。
