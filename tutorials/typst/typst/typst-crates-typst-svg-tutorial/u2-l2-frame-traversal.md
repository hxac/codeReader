# Frame 遍历与 Group/Link/Anchor

## 1. 本讲目标

上一讲（u2-l1）我们看懂了 `render_page`：它先画页面背景矩形，再把正文交给 `render_frame`。本讲就从 `render_page` 留下的边界 `render_frame` 切入，回答三个问题：

1. typst-svg 怎样遍历一棵 Frame 树、把六种 `FrameItem` 分发到对应的渲染方法？
2. `render_group` 是如何区分「软 frame」与「硬 frame」的？为什么硬 frame 要重置坐标系？
3. 链接（`<a>`）、锚点（`<g id>`）、裁剪路径（`<clipPath>`）这三类「非可视结构」是如何生成与复用的？

学完后，你应当能够：读懂 Frame 树到 SVG 元素树的映射过程，解释 `State` 在 group 递归中如何流转，并能根据 SVG 输出反推出对应的 Frame 结构。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 Frame 是一棵带坐标的树

Typst 排版的最终产物不是「一串文字」，而是一棵 `Frame` 树。每个 `Frame` 持有一组 `(Point, FrameItem)` 二元组：

- `Point` 是这个条目在**当前 frame 局部坐标系**里的左上角偏移；
- `FrameItem` 是条目本身。

`Frame` 的内部存储就是一个 `Vec<(Point, FrameItem)>`，`items()` 方法返回它的迭代器：

[src/lib.rs:310-324](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L310-L324)（`render_frame` 就是在消费这个迭代器）

> 参考 `Frame` 的定义：[crates/typst-library/src/layout/frame.rs:18-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L18-L30)，`items()` 的签名：[frame.rs:147-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L147-L149)。

SVG 导出器的本质任务，就是把这棵 Frame 树「翻译」成 SVG 的元素树（`<g>`、`<path>`、`<text>`、`<image>`、`<a>`…）。`render_frame` 就是这棵树的遍历器。

### 2.2 State 是「渲染上下文快照」

上一讲已经讲过 `State`：它是一个 `Copy` 的小结构，只携带两个字段——累积变换 `transform` 和当前硬 frame 的尺寸 `size`。`render_frame` 每访问一个条目，都会先用 `state.pre_translate(*pos)` 生成一个**新的** `State`，再把这份快照递交给具体渲染方法。正因为 `State` 是 `Copy`，兄弟节点之间互不污染。

### 2.3 LazySvgElem：按需生成的元素

`render_group` 大量使用 `svg.lazy_elem("g")`。它返回一个 `LazySvgElem`——只有当你显式调用 `.init()` 时，对应的 `<g>` 标签才会真正被写入；如果整个作用域结束都未调用 `.init()`，则什么都不输出。这是 typst-svg 避免「生成空的 `<g></g>` 垃圾元素」的关键机制（详见 u2-l3）。

> 简记：`svg.elem("g")` 立即写一个 `<g>`；`svg.lazy_elem("g")` 先「记账」，需要时才写。

## 3. 本讲源码地图

本讲几乎全部围绕 `src/lib.rs` 中的渲染编排层，外加一处对 `shape.rs` 与 `typst-library` 的引用。

| 源码位置 | 作用 |
|---|---|
| [src/lib.rs:310-324](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L310-L324) `render_frame` | 遍历 Frame，按 `FrameItem` 六变体分发 |
| [src/lib.rs:328-360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L328-L360) `render_group` | 处理软/硬 frame、label、clip，递归进入子 frame |
| [src/lib.rs:363-401](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L363-L401) `render_link` | 生成 `<a>` + 透明 `<rect>`，处理三种 `Destination` |
| [src/lib.rs:404-408](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L404-L408) `render_anchor` | 生成带 `id` 的空 `<g>` 作为锚点 |
| [src/lib.rs:422-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L422-L433) `write_clip_path_defs` | 在 `finalize` 阶段把去重后的裁剪路径写入 `<defs>` |
| [src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419) `finalize` | 编排各类 `<defs>` 的写出顺序（clip 排第二） |
| 外部类型 | `FrameItem` / `FrameKind` / `GroupItem` 定义于 `typst-library/src/layout/frame.rs`；`Destination` / `LateLinkResolver` 定义于 `typst-library/src/model/link.rs` |

## 4. 核心概念与源码讲解

### 4.1 render_frame：FrameItem 分发器

#### 4.1.1 概念说明

`FrameItem` 是 Frame 树的「叶子类型」，共有六种变体：

```rust
pub enum FrameItem {
    Group(GroupItem),   // 子 frame（可带变换/裁剪）
    Text(TextItem),     // 一段已塑形的文本
    Shape(Shape, Span), // 几何形状
    Image(Image, Size, Span), // 图像
    Link(Destination, Size),  // 链接
    Tag(Tag),           // 可内省标签（不影响渲染）
}
```

> 定义见 [crates/typst-library/src/layout/frame.rs:486-499](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L486-L499)。

`render_frame` 的职责很纯粹：**遍历 + 分发**。它本身不做任何绘制，只负责把每个条目连同调整后的 `State` 派发给专门的渲染方法。

#### 4.1.2 核心流程

```
对 frame.items() 中的每个 (pos, item):
    1. 生成新快照：state = state.pre_translate(pos)   ← 把条目位置烘焙进变换
    2. 按 item 分发：
        Group(group)  → render_group(svg, state, group)
        Text(text)    → render_text(svg, state, text)
        Shape(shape,_)→ render_shape(svg, state, shape)
        Image(im,sz,_)→ render_image(svg, state, im, sz)
        Link(dest,sz) → render_link(svg, state, dest, sz)
        Tag(_)        → 什么都不做（空分支）
```

关键细节：第 1 步的 `let state = state.pre_translate(*pos);` 是在循环体**内部**用 `let` 遮蔽（shadow）出新的局部变量。由于 `State` 是 `Copy`，每次迭代都从「函数入口的 state」重新派生，兄弟条目之间互不影响——位置不会在同级之间累积。

#### 4.1.3 源码精读

```rust
fn render_frame(&mut self, svg: &mut SvgElem, state: &State, frame: &Frame) {
    for (pos, item) in frame.items() {
        let state = state.pre_translate(*pos);
        match item {
            FrameItem::Group(group) => self.render_group(svg, &state, group),
            FrameItem::Text(text) => self.render_text(svg, &state, text),
            FrameItem::Shape(shape, _) => self.render_shape(svg, &state, shape),
            FrameItem::Image(image, size, _) => {
                self.render_image(svg, &state, image, size);
            }
            FrameItem::Link(dest, size) => self.render_link(svg, &state, dest, *size),
            FrameItem::Tag(_) => {}
        }
    }
}
```

逐行说明（[src/lib.rs:310-324](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L310-L324)）：

- `for (pos, item) in frame.items()`：`items()` 返回 `&(Point, FrameItem)` 的迭代，`pos` 是条目左上角在当前 frame 局部坐标系中的位置。
- `let state = state.pre_translate(*pos)`：调用上一讲讲过的 `pre_translate`，语义是把 `translate(pos)` **垫在内层**（先作用于点）。即变换结果为 `state.transform ∘ translate(pos)`，于是子条目的任意点 `p` 最终被映射到 `state.transform(p + pos)`——这正是「先平移到条目位置，再套用祖先变换」。
- `match item` 的六个分支：`Shape` / `Image` 中的 `_` 是丢弃 `Span`（仅用于错误定位，渲染不需要）；`Tag(_)` 直接空分支，因为标签只服务于内省（introspection），不产生任何像素。
- 注意 `Group` 分支调用的是 `render_group`，它会**递归**回到 `render_frame`（处理 group 内部的子 frame），从而完成整棵树的深度优先遍历。

#### 4.1.4 代码实践

**实践目标**：确认 `render_frame` 的分发是「无状态遍历」——它本身不生成任何 SVG 元素，元素由下游方法生成。

**操作步骤**（源码阅读型实践）：

1. 在 [src/lib.rs:310-324](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L310-L324) 中确认：`render_frame` 内部没有调用任何 `svg.elem(...)` 或 `svg.lazy_elem(...)`——它只在循环里调用 `self.render_*`。
2. 追踪一次 `FrameItem::Shape` 的旅程：`render_frame` → `render_shape`（在 `src/shape.rs`）→ 最终生成 `<path>`。
3. 追踪 `FrameItem::Tag`：它对应空分支 `{}`，确认它不会在 SVG 里留下任何痕迹。

**需要观察的现象**：`render_frame` 既不写元素也不修改 `self`（除经由下游方法），它纯粹是一个调度循环。

**预期结果**：你能画出「`render_frame` 是 Frame 树的中序调度器，真正的 SVG 元素都在它调用的子方法里产生」的结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `let state = state.pre_translate(*pos)` 写在 `for` 循环体内部，而不是循环之前？

**参考答案**：因为它在每次迭代中用 `let` 遮蔽出一个新的局部 `state`。`State` 是 `Copy`，每次迭代都从函数入口的 `state` 重新派生（`入口state + 本条目的pos`），因此兄弟条目之间的位置不会相互累积。若写在循环之前，位置会在兄弟之间叠加，导致坐标错乱。

**练习 2**：`FrameItem::Tag(_)` 为什么是空分支？

**参考答案**：`Tag` 是供 Typst 内省系统使用的元信息（如查询定位、计数器锚点），不携带任何可视内容。SVG 导出只关心可视输出，所以直接忽略。

---

### 4.2 render_group：软/硬 frame 与裁剪

#### 4.2.1 概念说明

`GroupItem` 是 Frame 树的「内部节点」，它包着一个子 `Frame`，并可携带三种附加信息（[frame.rs:516-530](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L516-L530)）：

- `transform`：施加在子 frame 上的变换；
- `clip: Option<Curve>`：可选的裁剪曲线；
- `label: Option<Label>`：可选的语义标签。

而每个 `Frame` 还有一个 `kind: FrameKind` 字段，取值 `Soft` 或 `Hard`。这是本讲最关键的概念。先看 typst-library 给出的定义（[frame.rs:453-470](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L453-L470)）：

- **Soft（软 frame，默认）**：「跟随父级尺寸」的容器。它**不构成**坐标系边界，不会影响设置在它子元素上的渐变布局。
- **Hard（硬 frame）**：「使用自身尺寸」的容器。它**是**其内容的坐标系参考边界，用于 page、block、box。

一句话直觉：**硬 frame 是一堵「墙」，它确立一个新的坐标系和渐变参照；软 frame 是「透明的纸」，它只是把变换往下传，不改变参照系。**

#### 4.2.2 核心流程

```
render_group(svg, state, group):
    g = svg.lazy_elem("g")                  ← 惰性 <g>，先不输出
    根据 group.frame.kind():
      Soft:  state = state.pre_concat(group.transform)   ← 变换吸收进 state，<g> 仍可能不输出
      Hard:  g.init()                                     ← 强制输出 <g>
             T = state.transform.pre_concat(group.transform)
             若 T 非单位：g 上写 transform = T
             state = state.with_transform(identity)       ← 重置变换！
                       .with_size(group.frame.size())     ← 更新尺寸！
    若有 label：g.init(); 写 data-typst-label
    若有 clip_curve：
        offset = (state.transform.tx, state.transform.ty)   ← 当前变换的平移分量
        id = clip_paths.insert_with((clip_curve, offset), || convert_curve(offset, clip_curve))
        g.init(); 写 clip-path = url(#id)
    render_frame(g, state, group.frame)    ← 递归遍历子 frame
```

#### 4.2.3 源码精读

```rust
fn render_group(&mut self, svg: &mut SvgElem, state: &State, group: &GroupItem) {
    let mut svg = svg.lazy_elem("g");

    let state = match group.frame.kind() {
        FrameKind::Soft => state.pre_concat(group.transform),
        FrameKind::Hard => {
            // Always generate a group for hard frames.
            svg.init();

            let transform = state.transform.pre_concat(group.transform);
            if !transform.is_identity() {
                svg.init().attr("transform", SvgTransform(transform));
            }
            state
                .with_transform(Transform::identity())
                .with_size(group.frame.size())
        }
    };

    if let Some(label) = group.label {
        svg.init().attr("data-typst-label", label.resolve());
    }

    if let Some(clip_curve) = &group.clip {
        let offset = Point::new(state.transform.tx, state.transform.ty);
        let id = self.clip_paths.insert_with((clip_curve, offset), || {
            shape::convert_curve(offset, clip_curve)
        });
        svg.init().attr("clip-path", SvgUrl(id));
    }

    self.render_frame(svg.lazy(), &state, &group.frame);
}
```

逐段说明（[src/lib.rs:328-360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L328-L360)）：

**（a）软 frame 分支**：`state.pre_concat(group.transform)`——把 group 的变换「垫进」state 的累积变换里，仅此而已。`<g>` 标签**尚未**调用 `.init()`，也就是说，一个既无 label 也无 clip 的纯软 frame，最终在 SVG 里**什么都不产生**——它的变换被「摊平」进了后代元素的 `transform` 属性中。这正是「软 frame 是透明纸」的体现。

**（b）硬 frame 分支**（本讲重点）：

1. `svg.init()`：**强制**输出一个 `<g>`（注释 "Always generate a group for hard frames."）。硬 frame 永远对应一个真实的 `<g>` 元素。
2. `let transform = state.transform.pre_concat(group.transform)`：算出「祖先累积变换 ∘ group 自身变换」的**完整变换 T**。
3. 若 `T` 不是单位矩阵，把它作为 `transform` 属性写到这个 `<g>` 上。
4. `state.with_transform(Transform::identity()).with_size(group.frame.size())`：把 state 的变换**重置为单位矩阵**，并把 size 更新为**这个 group 自己的尺寸**。

**为什么硬 frame 要这么做？** 见下方 4.2.4 的实践任务详解。核心有两点：①把累积变换「落袋」到 `<g>` 的 `transform` 属性上，后代就在 group 的局部坐标系里用单位变换绘制，避免变换在深层嵌套中无限累积、放大浮点误差；②更新 `size`，是因为 `State.size` 的语义是「当前最内层硬 frame 的尺寸」，而渐变/平铺在 `RelativeTo::Parent` 模式下要参照这个尺寸来缩放——硬 frame 是新的参照墙，所以 size 必须更新。

**（c）label 分支**：若有 label，调用 `svg.init()`（确保 `<g>` 已生成），写一个 `data-typst-label="..."` 属性，供外部工具识别语义区域。

**（d）clip 分支**：

- `offset = Point::new(state.transform.tx, state.transform.ty)`：取**当前 state 变换的平移分量**作为偏移。注意此时若是硬 frame，state.transform 已被重置为单位矩阵，所以 `offset = (0, 0)`；若是软 frame，offset 取累积变换的平移。裁剪路径需要被定位到 `<g>` 所在的用户坐标系里，这个 offset 就是定位锚点。
- `self.clip_paths.insert_with((clip_curve, offset), || shape::convert_curve(offset, clip_curve))`：把 `(clip_curve, offset)` 作为去重键（曲线相同但偏移不同 → 视为不同裁剪路径），仅当键不存在时才调用 `convert_curve` 真正生成 SVG path 字符串（惰性求值，详见 u6-l3 的 `Deduplicator`）。返回值 `id` 是一个 `DedupId`。
- `svg.init().attr("clip-path", SvgUrl(id))`：确保 `<g>` 已生成，写 `clip-path="url(#cXXXX...)"`。`SvgUrl(id)` 会格式化成 `url(#<id>)`（见 [src/write.rs:295-301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L295-L301)）。

**（e）递归**：`self.render_frame(svg.lazy(), &state, &group.frame)`——用（可能已重置的）state 递归遍历子 frame。注意传的是 `svg.lazy()`：若前面的 `<g>` 从未 init（纯软 frame 且无 label/clip），这里直接把子内容写到父级，完全没有多余的 `<g>` 包裹层。

#### 4.2.4 代码实践（本讲指定实践任务）

**实践目标**：用自己的话解释「软 frame 与硬 frame 对 `State` 的处理差异」，并说明硬 frame 为何要把 transform 重置为单位矩阵、把 size 更新为 `group.frame.size()`。

**操作步骤**：

1. 打开 [src/lib.rs:331-345](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L331-L345)，对比 `FrameKind::Soft` 与 `FrameKind::Hard` 两个分支对 `state` 的处理。
2. 对照 `FrameKind` 的文档注释 [frame.rs:453-470](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L453-L470)，注意「软 frame 不影响其子元素上设置的渐变布局」这句话。
3. 对照 `State` 字段注释 [src/lib.rs:229-234](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L229-L234)，注意 `size` 的语义是「层级中第一个硬 frame 的尺寸」。

**需要你写出的说明（参考答案）**：

> **软 frame** 只做一件事：`state.pre_concat(group.transform)`，把 group 的变换吸收进累积变换，`size` 保持不变。它**不**调用 `svg.init()`，所以一个无 label、无 clip 的纯软 frame 在 SVG 输出里不产生任何 `<g>` 元素——它的变换被摊平进后代。
>
> **硬 frame** 则做了三件不同的事：
> 1. **强制生成 `<g>`**（`svg.init()`）；
> 2. **把完整累积变换 `T = state.transform ∘ group.transform` 写到 `<g>` 的 `transform` 属性上**；
> 3. **把 `state.transform` 重置为单位矩阵、把 `state.size` 更新为 `group.frame.size()`**。
>
> 之所以要把 transform 重置为单位矩阵，是因为：硬 frame（page / block / box）确立了一个**新的局部坐标系**。把累积变换「落袋」到 `<g transform="T">` 之后，后代元素就在 group 的局部坐标系里以单位变换绘制，由 SVG 渲染器在显示时统一套用 `T`。这样既避免了变换矩阵在深层嵌套中不断相乘而累积浮点误差，也让每一层的坐标计算都基于「自己原点 (0,0)」这一干净前提。
>
> 之所以要更新 `size`，是因为 `State.size` 记录的是「最内层硬 frame 的尺寸」，而 `RelativeTo::Parent` 的渐变/平铺需要参照这个尺寸来缩放（参见 shape.rs 的 `shape_paint_transform`，u3-l2 / u5 讲义）。硬 frame 是新的参照边界，所以必须把自己的尺寸传下去；软 frame 正相反——它被设计成「不影响子元素渐变布局」，所以故意**不**更新 size，让渐变继续参照更外层的那个硬 frame。

#### 4.2.5 小练习与答案

**练习 1**：一个「既无 label、也无 clip 的软 frame group」最终会在 SVG 中产生什么？

**参考答案**：什么都不产生。它的 `transform` 被 `pre_concat` 吸收进 state，`<g>` 从未被 `.init()`，作用域结束时 `LazySvgElem::drop` 发现 `initialized == false`，不写闭合标签。子内容通过 `svg.lazy()` 直接挂到父级。

**练习 2**：clip 去重的键为什么是 `(clip_curve, offset)` 而不只是 `clip_curve`？

**参考答案**：因为同一条曲线在不同偏移（即出现在画面不同位置）会产生不同的 SVG path 数据（`convert_curve(offset, ...)` 把 offset 加到每个点上）。两条几何相同但位置不同的裁剪路径是**不同**的 `<clipPath>`，必须各自分配 ID；若只用 `clip_curve` 作键，会把不同位置的裁剪错误地合并成同一个，导致裁剪区域错位。

---

### 4.3 render_link 与 Destination 三态

#### 4.3.1 概念说明

`FrameItem::Link(Destination, Size)` 表示一个矩形可点击区域。`Destination` 是链接目标，有三种变体（[link.rs:295-302](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/link.rs#L295-L302)）：

- `Url(Url)`：外部 URL，如 `https://...`。
- `Position(PagedPosition)`：同一文档内某一页的某一点。
- `Location(Location)`：一个尚未解析的文档内位置，需要通过 `LateLinkResolver` 解析成具体目标（可能解析为本文档锚点，也可能跨文档）。

SVG 用 `<a>` 元素承载超链接。typst-svg 的做法是：`<a>` 内部塞一个**透明的 `<rect>`**，用 rect 的宽高定义点击区域，链接目标写在 `<a>` 的 `href` 上。

#### 4.3.2 核心流程

```
render_link(svg, state, dest, size):
    a = svg.elem("a")                       ← 立即创建 <a>
    若 state.transform 非单位：a 写 transform = state.transform
    match dest:
      Url(url):      a 写 href=url, xlink:href=url
      Position(_):   空分支（TODO，暂不支持同页跳转）
      Location(loc): 若有 link_resolver：
                        解析 loc → ResolvedLink → 相对 URI
                        成功则 a 写 href=uri, xlink:href=uri
    a 内部创建 <rect>：width/height = size（pt），fill=transparent，stroke=none
```

#### 4.3.3 源码精读

```rust
fn render_link(&mut self, svg: &mut SvgElem, state: &State, dest: &Destination, size: Size) {
    let mut a = svg.elem("a");
    if !state.transform.is_identity() {
        a.attr("transform", SvgTransform(state.transform));
    }

    match dest {
        Destination::Url(url) => {
            a.attr("href", url.as_str());
            a.attr("xlink:href", url.as_str());
        }
        Destination::Position(_) => {
            // TODO: Links on the same page could be supported.
        }
        Destination::Location(loc) => {
            // TODO: Location links on the same page could also be supported
            // outside of HTML.
            if let Some(resolver) = self.link_resolver
                && let Some(link) = resolver.resolve(*loc)
                && let Ok(uri) = link.into_relative_uri()
            {
                a.attr("href", &uri);
                a.attr("xlink:href", &uri);
            }
        }
    }

    a.elem("rect")
        .attr("width", size.x.to_pt())
        .attr("height", size.y.to_pt())
        .attr("fill", "transparent")
        .attr("stroke", "none");
}
```

逐段说明（[src/lib.rs:363-401](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L363-L401)）：

- `svg.elem("a")`：注意是 `elem`（立即创建），不是 `lazy_elem`——每个链接都会产生一个真实的 `<a>`。
- `a.attr("transform", SvgTransform(state.transform))`：把当前累积变换写到 `<a>` 上，使链接矩形落在正确位置/尺寸。
- **`Url` 分支**：直接把 URL 同时写到 `href` 和 `xlink:href`。两个都写是为了**兼容性**——`href` 是 SVG 2 的标准，`xlink:href` 是 SVG 1.1 的旧写法，部分老旧渲染器只认后者。
- **`Position` 分支**：空分支，带 TODO。同一文档内的页内跳转在纯 SVG 导出中暂未实现。
- **`Location` 分支**（关键）：这是「延迟解析」链路。`Location` 是排版阶段产生的抽象位置引用，在导出 SVG 时还不一定知道它最终指向哪。代码用 `let-let` 链式短路：
  - `self.link_resolver`：渲染器是否携带了解析器？只有 `svg_in_html` / `svg_in_bundle`（见 u1-l3、u6-l4）会传入；纯 `svg` 导出时它是 `None`。
  - `resolver.resolve(*loc)`：把 `Location` 解析成 `ResolvedLink`（`Local` 本文档锚点 或 `Cross` 跨文档，见 [link.rs:675-693](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/link.rs#L675-L693)）。
  - `link.into_relative_uri()`：把 `ResolvedLink` 转成相对 URI（可能带 `#anchor` 片段，见 [link.rs:717-722](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/link.rs#L717-L722)）。
  - 三步任一失败，`<a>` 就没有 `href`——成为一个「哑链接」（结构在，但点不动）。这是有意的容错：解析失败时宁可留个空 `<a>`，也不让整个导出崩溃。
- 最后的 `<a><rect/></a>`：rect 的尺寸就是链接区域 `size`（单位 pt），`fill="transparent"` + `stroke="none"` 保证它**不可见但可点击**。rect 的左上角默认在 `<a>` 局部坐标系原点，而 `<a>` 的 transform 已把它搬到正确位置。

#### 4.3.4 代码实践

**实践目标**：理解「链接 = 透明矩形 + `<a>` 的 href」，并验证三种 `Destination` 的不同处理。

**操作步骤**（可运行的 CLI 实践，待本地验证）：

1. 准备一个 Typst 源文件 `links.typ`：

   ```typst
   #link("https://typst.app")[外部链接]
   #link(<mylabel>)[内部跳转]

   = 标题 <mylabel>
   ```

2. 编译为 SVG：`typst compile --format svg links.typ links.svg`（具体子命令以本地 `typst --help` 为准）。
3. 打开 `links.svg`，搜索 `<a`。

**需要观察的现象 / 预期结果**：

- 第一个链接（`Url`）的 `<a>` 上应有 `href="https://typst.app"` 和 `xlink:href="..."` 两个属性。
- 每个 `<a>` 内部都有一个 `<rect fill="transparent" stroke="none">`。
- 第二个链接（`Location`）在纯 `svg` 导出下，`<a>` 上**可能没有** `href`——因为 CLI 的单页导出不传 `link_resolver`，`Location` 无法解析（取决于 typst-cli 当前是否为单页 svg 传入解析器；若无效则表现为哑链接）。

> 若本地没有 typst CLI，可改为**源码阅读型实践**：在 [src/lib.rs:383-393](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L383-L393) 确认 `Location` 分支的三重 `if let` 条件，并解释为什么任何一个条件不满足都会导致哑链接。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Url` 分支要同时写 `href` 和 `xlink:href`？

**参考答案**：为了兼容不同 SVG 渲染器。`href` 是 SVG 2 的标准属性，`xlink:href` 是 SVG 1.1 的旧命名空间属性。部分老旧或嵌入式渲染器只识别 `xlink:href`，两个都写可保证链接在尽可能多的环境里可点击。

**练习 2**：在纯 `svg`（单页文件）导出中，一个 `Destination::Location` 链接会变成什么？

**参考答案**：会变成一个「哑链接」——`<a>` 元素结构存在、内部有透明 `<rect>`，但 `<a>` 上没有 `href`。因为 `svg()` 入口创建的 `SVGRenderer` 的 `link_resolver` 为 `None`（见 [src/lib.rs:267-269](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L267-L269)），三重 `if let` 第一步就短路，无法解析。

---

### 4.4 render_anchor 与 write_clip_path_defs

本节把两个「收尾型」模块放在一起讲：锚点（渲染期生成）和裁剪路径定义（finalize 期统一写出）。它们共同点是——都是为「被别处引用」而存在的辅助结构。

#### 4.4.1 概念说明

**锚点（anchor）**：一个带 `id` 的命名坐标点。它本身不画任何东西，但其它文档或链接可以用 `#id` 片段跳转到它。在 bundle / HTML 导出中，typst-svg 会把传入的 `anchors: &[(Point, EcoString)]` 逐个渲染成锚点。

**裁剪路径定义（clip path defs）**：前面 4.2 看到 `render_group` 在遇到 clip 时只是**登记**了一条裁剪路径（拿到一个 `DedupId`），并在 `<g>` 上写 `clip-path="url(#id)"`。真正的 `<clipPath>` 元素要到 `finalize` 阶段才集中写进 `<defs>`——这就是「先引用、后定义」的去重模型。

#### 4.4.2 核心流程

`render_anchor`：

```
render_anchor(svg, pos, id):
    svg.elem("g")               ← 立即创建空 <g>
        .attr("id", id)
        .attr("transform", translate(pos.x, pos.y))
```

`write_clip_path_defs`（在 finalize 中调用）：

```
若 clip_paths 为空：直接返回（不写空 <defs>）
否则：
    defs = svg.elem("defs")
    对每个 (id, path) in clip_paths.iter():
        <clipPath id="id"><path d="path"/></clipPath>
```

#### 4.4.3 源码精读

`render_anchor`（[src/lib.rs:404-408](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L404-L408)）：

```rust
fn render_anchor(&mut self, svg: &mut SvgElem, pos: Point, id: &str) {
    svg.elem("g")
        .attr("id", id)
        .attr("transform", SvgTransform(Transform::translate(pos.x, pos.y)));
}
```

- 生成一个 `<g id="..." transform="translate(x y)">`，**无子元素**。它纯粹是一个带名字的坐标点。
- 谁会调用它？看 [src/lib.rs:67-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L67-L69)（`svg_in_bundle`）和 [src/lib.rs:114-116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L114-L116)（`svg_in_html`）：在渲染完页面后，对传入的 `anchors` 列表逐个调用 `render_anchor`。这样其它文档的 `Location` 解析成相对 URI 后，其 `#anchor` 片段就能精确跳到这里。

`write_clip_path_defs`（[src/lib.rs:422-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L422-L433)）：

```rust
fn write_clip_path_defs(&self, svg: &mut SvgElem) {
    if self.clip_paths.is_empty() {
        return;
    }

    let mut defs = svg.elem("defs");
    for (id, path) in self.clip_paths.iter() {
        defs.elem("clipPath").attr("id", id).with(|svg| {
            svg.elem("path").attr("d", path);
        });
    }
}
```

- **空集合提前返回**：若没有任何裁剪路径，连 `<defs>` 都不写，避免输出无用的空标签。
- 遍历 `clip_paths.iter()`（`Deduplicator::iter` 同时给出 `DedupId` 和值），为每条生成 `<clipPath id="cXXX"><path d="..."/></clipPath>`。这里的 `id` 与 4.2 中 `render_group` 写到 `clip-path="url(#id)"` 的是**同一个** `DedupId`，从而把「引用」与「定义」对接起来。
- `<clipPath>` 默认 `clipPathUnits="userSpaceOnUse"`，所以 path 数据（由 `convert_curve(offset, ...)` 生成，已含偏移）在引用它的 `<g>` 的用户坐标系里解释。

它在 `finalize` 中排第二位（[src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419)）：`write_glyph_defs` → **`write_clip_path_defs`** → `write_gradients` → ……

#### 4.4.4 代码实践

**实践目标**：把「渲染期引用 → finalize 期定义」这条去重链路走通。

**操作步骤**（源码阅读型实践）：

1. 在 [src/lib.rs:351-357](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L351-L357) 找到 `render_group` 写 `clip-path` 属性的代码，记下它用的是 `SvgUrl(id)`。
2. 跳到 [src/write.rs:295-301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L295-L301)，确认 `SvgUrl(id)` 输出成 `url(#<id>)`。
3. 跳到 [src/lib.rs:422-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L422-L433)，确认 `<clipPath>` 的 `id` 与引用处用的是同一个 `DedupId`。

**需要观察的现象**：引用 (`clip-path="url(#cXXX)"`) 与定义 (`<clipPath id="cXXX">`) 通过 `DedupId` 精确匹配；定义集中放在 `<defs>` 里，渲染期只留引用。

**预期结果**：你能解释「为什么 `render_group` 遇到 clip 时不直接内联写 `<clipPath>`，而是先去重拿 ID、把定义推迟到 finalize」——答案是：同一条裁剪曲线可能被多个 group 复用，集中去重 + 集中定义能避免重复输出，显著减小文件体积。

#### 4.4.5 小练习与答案

**练习 1**：`render_anchor` 生成的 `<g>` 为什么是空的？

**参考答案**：锚点的作用是提供一个**可被 `#id` 引用的命名坐标点**，本身不需要任何可视内容。它的位置由 `transform="translate(pos)"` 表达。SVG 渲染器在跳转到 `#id` 时会定位到这个 `<g>` 的位置，内容为空既不影响定位，也不污染画面。

**练习 2**：`write_clip_path_defs` 为什么要在开头检查 `is_empty()` 并提前返回？

**参考答案**：为了不输出无用的空 `<defs></defs>`。大多数页面根本没有裁剪 group，提前返回能让 SVG 更干净、体积更小。这是一种「按需生成」的体积优化，和 `LazySvgElem` 的理念一致。

---

## 5. 综合实践

把本讲的四块知识（frame 遍历、软/硬 frame、clip、link/anchor）串起来。

**任务**：用一段带裁剪与链接的 Typst 源码，生成 SVG，然后**反向追踪**输出里的结构到 `render_frame` 的分发路径。

**操作步骤**（可运行，待本地验证）：

1. 准备 `demo.typ`：

   ```typst
   #block(
     clip: true,
     width: 100pt,
     height: 60pt,
     fill: red,
   )[#link("https://typst.app")[Go]]
   ```

2. 编译：`typst compile --format svg demo.typ demo.svg`（子命令以本地为准）。
3. 打开 `demo.svg`，按顺序寻找并解释：

   | 你看到的 SVG 结构 | 对应的 typst-svg 代码路径 |
   |---|---|
   | 外层若干嵌套的 `<g transform="...">` | 硬 frame group：`render_group` 的 Hard 分支，[src/lib.rs:333-344](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L333-L344) |
   | 某个 `<g>` 上的 `clip-path="url(#cXXX)"` | `render_group` 的 clip 分支，[src/lib.rs:351-357](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L351-L357) |
   | `<defs>` 里的 `<clipPath id="cXXX"><path d="..."/></clipPath>` | `write_clip_path_defs`，[src/lib.rs:422-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L422-L433) |
   | `<a href="..." xlink:href="..."><rect fill="transparent".../></a>` | `render_link`，[src/lib.rs:363-401](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L363-L401) |
   | 红色矩形 | `render_frame` → `FrameItem::Shape` → `render_shape` |

4. **画一棵 Frame 树**：根据 SVG 里 `<g>` 的嵌套层次，反推出原始 Frame 树的形态，标注每个节点的 `FrameKind`（提示：page 是硬 frame；`block(clip:true)` 通常也会引入硬 frame 边界与 clip）。

> 若本地无法运行 typst，可把第 3 步改为纯源码追踪：从 `svg()`（[src/lib.rs:32-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L32-L43)）→ `render_page` → `render_frame` → 各 `render_*`，手工画出调用栈，并预测每种 `FrameItem` 会落到哪个 SVG 元素。

## 6. 本讲小结

- `render_frame` 是 Frame 树的**无状态调度器**：遍历 `(Point, FrameItem)`，对每个条目先用 `state.pre_translate(pos)` 生成新快照，再按六种 `FrameItem` 分发；`Tag` 被忽略。
- `render_group` 按 `FrameKind` 分流：**软 frame** 只把变换吸收进 state、不产生 `<g>`；**硬 frame** 强制生成 `<g>`、把完整累积变换写到 `transform` 属性、并把 state 的 transform 重置为单位矩阵、size 更新为自身尺寸。
- 硬 frame 重置坐标系是为了「落袋为安」、避免深层浮点误差累积；更新 size 是为了让 `RelativeTo::Parent` 的渐变参照新的边界（软 frame 故意不更新，以避免影响子元素渐变布局）。
- clip 采用「渲染期引用 + finalize 期定义」的去重模型：`render_group` 用 `(curve, offset)` 作键登记、拿 `DedupId` 并写 `clip-path="url(#id)"`；`write_clip_path_defs` 在 finalize 时集中写 `<clipPath>`。
- `render_link` 生成 `<a>` + 透明 `<rect>`；`Url` 直接写 `href`/`xlink:href`，`Position` 暂未实现，`Location` 需 `link_resolver` 三步解析、否则成哑链接。
- `render_anchor` 生成带 `id` 的空 `<g>` 作命名锚点，仅供 `svg_in_bundle` / `svg_in_html` 使用。

## 7. 下一步学习建议

- **向下深入形状**：本讲的 `render_frame` 把 `FrameItem::Shape` 交给了 `render_shape`。下一讲 u3-l1（path.rs）与 u3-l2（shape.rs）会讲清楚一条 `Shape` 如何变成 `<path>` 的 `d` 属性，以及 `convert_curve`（本讲 clip 用到它）的内部机制。
- **向旁看文本与字形**：`FrameItem::Text` → `render_text` 的细节在 u4-l1，字形去重与 `<symbol>`/`<use>` 复用模型在 u4-l2。
- **回顾去重地基**：本讲反复出现的 `DedupId`、`Deduplicator::insert_with` 是 typst-svg 体积优化的核心，其哈希编码与惰性求值机制集中在 u6-l3 详解。
- **扩展到集成模式**：`render_anchor` 与 `Location` 解析只在 `svg_in_bundle` / `svg_in_html` 中生效，这两种集成入口与 `svg_merged` 的差异在 u6-l4 统一对比。
