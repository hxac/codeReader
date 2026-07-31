# Group 渲染、裁剪与遮罩

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `render_group` 处理一个 `GroupItem` 时的「三段式」流程：算变换 → 算遮罩 → 递归进子帧。
- 区分 `FrameKind::Soft` 与 `FrameKind::Hard`，并解释硬帧为何要「重置」`container_transform` 与 `size`。
- 把一条裁剪曲线 `Curve` 跟踪成 `convert_curve → path.transform → Mask` 的完整链路，并讲清「已有父遮罩」与「无父遮罩」两条分支的差别。
- 说明 `with_mask` 如何把遮罩沿递归一路向下传递、逐层求交。
- 解释 `sk::Mask::new` 失败时为何选择直接 `return`（什么都不画）。

本讲承接 u2-l1 建立的 `State` 与坐标变换体系（`pre_concat` 语义为「先 B 后 A」、`AbsExt` 把 `Abs` 转 pt-f32），只聚焦「组（Group）」这一树枝节点如何被渲染。

## 2. 前置知识

在进入本讲前，你需要先掌握（来自前置讲义）：

- **Frame 场景树**：Typst 排版产物是一棵 `Frame` 树；`Group` 是唯一的树枝节点，`Text`/`Shape`/`Image` 是叶子，`Link`/`Tag` 不产生像素（见 u1-l3）。
- **State 背包**：渲染递归中随身携带的不可变状态，派生 `Copy`，含 `transform`、`container_transform`、`mask`、`pixel_per_pt`、`size` 五个字段（见 u2-l1）。
- **pre_concat 语义**：`A.pre_concat(B)` 表示「先 B 后 A」，即数学上的复合 \(A \circ B\)；链式 `pre_*` 中写在前者作用在后、写在后者作用在先（更靠近被绘制的点）。
- **tiny-skia 基本类型**：`Pixmap`（像素画布）、`Path`（矢量路径）、`Transform`（仿射变换）、`Mask`（逐像素遮罩）。

两个本讲会用到的直觉：

- **遮罩（Mask）是一张「逐像素 u8 覆盖图」**：值 `255`（0xFF）表示该像素允许绘制，值 `0` 表示禁止绘制。把它传给 `canvas.fill_path(..., Some(mask))` 时，绘制结果会被遮罩「按像素裁掉」。typst-render 用它来实现组的裁剪（clip）。
- **裁剪是「取交集」**：当多层嵌套的组各自带裁剪时，最终某片叶子能被画出来的区域，是所有祖先裁剪区域的「逐层求交」结果。typst-render 用 `Mask::intersect_path` 来做这个求交。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-render/src/lib.rs` | `render_group`（组渲染中枢）、`State`（含 `container_transform`/`mask`）、`with_mask`/`pre_concat_container`、`render_frame`（派发器） |
| `crates/typst-render/src/shape.rs` | `convert_curve`：把 Typst 的 `Curve` 转成 tiny-skia 的 `Path`，被裁剪逻辑复用 |
| `crates/typst-library/src/layout/frame.rs` | `FrameKind`（软/硬帧）、`GroupItem`（含 `clip: Option<Curve>` 字段）的定义 |

只读地看，本讲的核心就是 `render_group` 这 55 行（含注释）。

## 4. 核心概念与源码讲解

### 4.1 Group 渲染总览：render_group 的三段式

#### 4.1.1 概念说明

`Group` 是场景树里唯一的树枝节点。它自带三样东西（见 `GroupItem` 结构）：

- `frame: Frame` —— 组内部的子框架（可能继续嵌套）；
- `transform: Transform` —— 作用于整组的附加变换；
- `clip: Option<Curve>` —— 可选的裁剪曲线。

`render_group` 的工作就是「带着父级 `State` 走进这个组」：先把组自身的变换叠加进 `state.transform`，再处理可选裁剪、算出新的遮罩，最后递归调用 `render_frame` 把子框架画出来。整个过程可以清晰地切成三段。

#### 4.1.2 核心流程

```
render_group(canvas, state, pos, group):
  ┌─ 第一段：算变换 ─────────────────────────────────┐
  │ sk_transform = to_sk_transform(&group.transform) │
  │ 根据 frame.kind()（Soft/Hard）更新 state：       │
  │   - 叠加 pre_translate(pos) 与 pre_concat(G)     │
  │   - Hard 帧额外重置 container_transform / size   │
  └──────────────────────────────────────────────────┘
  ┌─ 第二段：算遮罩（仅当 group.clip 存在）──────────┐
  │ convert_curve(clip) → Path                       │
  │ Path.transform(state.transform) → 画布坐标的路径 │
  │ 若已有父遮罩：clone + intersect_path（求交）     │
  │ 若无父遮罩：Mask::new + fill_path（新建）        │
  └──────────────────────────────────────────────────┘
  ┌─ 第三段：递归 ───────────────────────────────────┐
  │ render_frame(canvas, state.with_mask(mask),      │
  │              &group.frame)                        │
  └──────────────────────────────────────────────────┘
```

注意一个关键细节：在 `render_frame` 的派发里（u1-l3 讲过），叶子节点（Text/Shape/Image）是原地 `state.pre_translate(*pos)` 落位，而 `Group` 是把 `pos` **原样**传给 `render_group`。也就是说，组的 `pos` 不会在外面被消费掉，而是由 `render_group` 在第一段里自己 `pre_translate(pos)`。这保证组的变换、裁剪都基于「组在父帧中的位置 + 组自带 transform」共同决定。

#### 4.1.3 源码精读

整函数主体：[crates/typst-render/src/lib.rs:L208-L262](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L208-L262) —— 组渲染的入口，先算变换、再算遮罩、最后递归。

第一段「算变换」骨架：

```rust
let sk_transform = to_sk_transform(&group.transform);
let state = match group.frame.kind() {
    FrameKind::Soft => state.pre_translate(pos).pre_concat(sk_transform),
    FrameKind::Hard => state
        .pre_translate(pos)
        .pre_concat(sk_transform)
        // ... 额外重置 container_transform / size（见 4.2）
};
```

这段把「组在父帧中的位置 `pos`」与「组自带变换 `sk_transform`」依次复合进 `state.transform`。Soft 与 Hard 的差别在 4.2 展开。

第三段「递归」只有一行，但很关键：[crates/typst-render/src/lib.rs:L261](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L261) —— 把（可能更新过的）遮罩通过 `with_mask` 装进 state，递归进入子帧。

#### 4.1.4 代码实践

**实践目标**：在脑中把 `render_group` 的三段式与 `render_frame` 的派发对应起来。

**操作步骤**：

1. 打开 [crates/typst-render/src/lib.rs:L186-L205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L186-L205)（`render_frame`）。
2. 找到 `FrameItem::Group(group)` 分支，确认它调用 `render_group(canvas, state, *pos, group)`——`pos` 被原样传入，没有在 `render_frame` 里 `pre_translate`。
3. 对比 `FrameItem::Shape` 分支：它在 `render_frame` 里就 `state.pre_translate(*pos)` 后才调用 `shape::render_shape`。

**需要观察的现象 / 预期结果**：叶子（Shape/Text/Image）的 `pos` 在派发层就被消费；Group 的 `pos` 延迟到 `render_group` 第一段才消费。这是为了让组的裁剪曲线能在「组的位置 + 组的 transform」共同确定的坐标系里被正确变换到画布像素空间。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `render_group` 必须自己 `pre_translate(pos)`，而不能像叶子那样在 `render_frame` 里提前平移？

**参考答案**：因为组带裁剪。裁剪曲线 `group.clip` 定义在组的局部坐标系里，必须先 `pre_translate(pos).pre_concat(sk_transform)` 确定完整的 `state.transform`，再用这个变换把裁剪路径映射到画布像素空间（见 4.3 的 `path.transform(state.transform)`）。若在派发层就提前平移，会破坏「变换与裁剪共享同一坐标系」的语义，也使得 Soft/Hard 对 `container_transform` 的处理无法统一表达。

**练习 2**：`render_group` 末尾调用的是 `render_frame` 还是 `render_group`？二者关系是什么？

**参考答案**：调用的是 `render_frame`。`render_frame` 负责遍历子帧的 items 并按变体派发；当遇到下一层 `Group` 时再回到 `render_group`。两者相互递归，共同遍历整棵 Frame 树（与 u1-l3 讲过的派发结构一致）。

---

### 4.2 FrameKind：软帧与硬帧，以及 container_transform 的重置

#### 4.2.1 概念说明

`FrameKind` 标注一个帧的「硬度」，定义在库层：

[crates/typst-library/src/layout/frame.rs:L453-L470](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L453-L470) —— 文档说得很直白：它决定「该帧是否被视为其内容的最近父容器」，用途是**给渐变确定坐标参考系**。

- **Soft（默认）**：跟随父容器的尺寸——不改变「容器参考」。
- **Hard**：使用自己的尺寸——它本身成为新的「容器参考」。文档注明 Hard 用于 page、block、box。

`State` 里有两个专门服务于此的字段（见 [crates/typst-render/src/lib.rs:L120-L121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L120-L121) 与 [L126-L127](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L126-L127)）：

- `container_transform`：「层级中第一个（最近的）硬帧」的变换；
- `size`：「层级中最近的硬帧」的尺寸。

它们是给「相对于父容器（`RelativeTo::Parent`）的渐变」用的——具体怎么用，在 u3-l1（渐变）讲。本讲只讲清它们在 `render_group` 里**如何被更新**。

#### 4.2.2 核心流程

核心是一条**不变量**：

> 每当渲染进入一个「硬帧边界」时，`container_transform` 等于当前的 `transform`，`size` 等于该硬帧的尺寸。Soft 帧不动这两个字段，于是它们继续指向「最近的硬帧祖先」。

- **Soft 帧**：只做 `pre_translate(pos).pre_concat(sk_transform)`，`container_transform` 与 `size` 原样继承——即它「借用」父级的容器参考。
- **Hard 帧**：在同样叠加 `transform` 之外，额外把 `container_transform` **重置**为新的 `transform`、把 `size` 重置为本帧尺寸——即它「自立门户」成为新容器。

重置的难点在于：`State` 是不可变更新，`pre_concat_container` 只支持「在现有 `container_transform` 上复合」，并没有「赋值」操作。要把当前值 \(C_0\) 改成目标值 \(T\)，代码用的技巧是「先用 \(C_0\) 的逆抵消自己，再叠上目标」。

#### 4.2.3 源码精读

Soft 分支：[crates/typst-render/src/lib.rs:L211](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L211) —— 只更新 `transform`，`container_transform`/`size` 不变。

Hard 分支：[crates/typst-render/src/lib.rs:L212-L222](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L212-L222) —— 在叠加 `transform` 之外，三次 `pre_concat_container` 完成重置，最后 `with_size`。

辅助方法 `pre_concat_container`：[crates/typst-render/src/lib.rs:L177-L182](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L177-L182) —— 仅对 `container_transform` 做 `pre_concat`。

下面推导 Hard 分支三次 `pre_concat_container` 的净效果。记号约定：复合 \(A \circ B\) 表示「先 B 后 A」（与 `pre_concat` 一致）；`post_concat` 则相反，\(A.\text{post\_concat}(B) = B \circ A\)。设进入该硬帧前：

- \(T_0 = \text{state.transform}\)（父级当前变换）
- \(C_0 = \text{state.container\_transform}\)（旧容器变换）
- \(P = \text{translate}(pos)\)、\(G = \text{sk\_transform}\)（组变换）

注意 Hard 分支里第一个 `pre_concat_container` 的实参引用的是**原始** `state`（`state.transform`、`state.container_transform`），而非链式更新后的值——因为 `State` 不可变，方法返回新值，`state` 这个绑定始终指向入参。三次复合依次为：

1. 实参 \(X = T_0.\text{post\_concat}(C_0^{-1}) = C_0^{-1} \circ T_0\)；

   \[ C_0 \circ X = C_0 \circ C_0^{-1} \circ T_0 = T_0 \]

   这一步用 \(C_0^{-1}\) 把旧容器变换「清零」，结果回到 \(T_0\)。

2. 再 `pre_concat_container(P)`：\(T_0 \circ P\)。
3. 再 `pre_concat_container(G)`：\(T_0 \circ P \circ G\)。

而 `transform` 在前两步 `pre_translate(pos).pre_concat(sk_transform)` 之后也正是 \(T_0 \circ P \circ G\)。于是得到本节开头的不变量：

\[ \text{container\_transform}_{\text{新}} = \text{transform}_{\text{新}} = T_0 \circ P \circ G \]

加上 `with_size(group.frame.size())`，硬帧边界处 `container_transform == transform`、`size == 本帧尺寸`，严格成立。

另外，根页面在 `State::new` 里就被「自举」为硬容器：[crates/typst-render/src/lib.rs:L131-L139](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L131-L139) —— `container_transform: transform`、`size` 取页面尺寸。所以 `render_frame` 派发页面顶层 items 时并不检查页面自身的 `kind`；硬帧判定只在进入子 `Group` 时由 `render_group` 进行。

#### 4.2.4 代码实践

**实践目标**：用一个两层嵌套场景，预测叶子处的 `container_transform` 与 `size`。

**操作步骤**：

1. 设场景：页面（Hard，在 `State::new` 自举）→ 组 A（**Soft**，带裁剪）→ 组 B（**Soft**）→ 一个 Shape。
2. 跟踪从页面到 Shape 的 `render_group` 调用：A、B 都是 Soft，二者都**不**触发 Hard 分支。
3. 因此 `container_transform` 与 `size` 一路不变。

**需要观察的现象 / 预期结果**：在 Shape 处，`state.container_transform` 仍是 `State::new` 设置的页面变换（pt→像素的缩放），`state.size` 仍是页面尺寸——因为路径上没有任何硬帧。若把组 A 改成 Hard（例如它对应一个 `block`），则进入 A 时 `container_transform` 会被重置为 A 的局部→画布变换、`size` 重置为 A 的尺寸，B（Soft）继续沿用 A 的值。

**待本地验证**：可写一份 Typst 源码（`block`/`box` 会产生硬帧，普通容器多为软帧），用调试打印或在 `render_group` 临时加日志确认实际 `FrameKind`，验证上述预测。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Soft 帧需要存在？直接全用 Hard 不行吗？

**参考答案**：Soft 帧让「中间包装层」（如纯粹为了平移/变换而插入的组）不改变渐变的容器参考系。若全是 Hard，每一层包装都会重置 `container_transform`/`size`，那么一个 `RelativeTo::Parent` 的渐变就会把「最近包装层」当父容器，而非用户语义上的 `block`/`box`/页面。Soft = 「我只是搬运工，容器参考请认上面的硬帧」。

**练习 2**：Hard 分支第一个 `pre_concat_container` 的实参里出现了 `state.container_transform.invert().unwrap()`。这里的 `unwrap` 在什么情况下会 panic？

**参考答案**：当 `container_transform` 不可逆（行列式为 0，即退化变换，比如某轴缩放为 0）时 `invert()` 返回 `None`，`unwrap` 会 panic。正常排版中累积变换不会退化，故视为安全；这是一种「前置不变量假设」。

---

### 4.3 Mask 与裁剪曲线：convert_curve → Path → Mask

#### 4.3.1 概念说明

裁剪（clip）的本质是：限定「组内内容只能出现在某条闭合曲线内部」。在位图渲染里，这用一张与画布等大的逐像素 `Mask` 表达——曲线内部像素值为 255（可绘制）、外部为 0（禁止）。

`GroupItem.clip` 是 `Option<Curve>`（见 [crates/typst-library/src/layout/frame.rs:L522](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/frame.rs#L522)）。要把这条 Typst 的矢量曲线变成像素遮罩，需要三步：

1. **`convert_curve`**：`Curve`（Typst 的 Move/Line/Cubic/Close 序列）→ tiny-skia `Path`。这一步在 `shape.rs` 里实现，渲染形状和裁剪共用。
2. **`path.transform(state.transform)`**：把位于「组局部坐标系」的路径，用当前累积变换映射到画布像素坐标系（因为 Mask 是按画布像素组织的）。
3. **建/交 Mask**：根据有没有父遮罩，走两条不同分支。

#### 4.3.2 核心流程

```
if group.clip 存在:
    path = convert_curve(clip_curve)              # Curve → Path（组局部 pt 坐标）
         .and_then(|p| p.transform(state.transform))  # → 画布像素坐标
    if path 存在:
        if 已有父遮罩 mask:
            new = mask.clone()
            new.intersect_path(path, ..., invert=true)   # 与父遮罩求交
        else:
            new = Mask::new(canvas.w, canvas.h)?         # 全画布零遮罩
                 .fill_path(path, ..., invert=true)      # 用裁剪曲线标出可绘制区
        mask = &new
# 最终：render_frame(canvas, state.with_mask(mask), ...)
```

两条分支的本质：

- **无父遮罩**：`Mask::new` 建一张全画布、全 0 的遮罩，`fill_path` 用裁剪曲线在它上面「画出」可绘制区域——这是**从零建立**遮罩。
- **已有父遮罩**：`clone` 父遮罩，`intersect_path` 把它「再缩小」到与当前裁剪曲线的交集——这是**求交**。逐层求交后，叶子能被画出的区域 = 所有祖先裁剪区域的交集。

两条分支都给 `fill_path`/`intersect_path` 传 `invert = true`，使「裁剪曲线的内部」成为遮罩中的可绘制区域（被保留）、外部被剔除——这与 Typst 裁剪「保留曲线内部、裁掉外部」的语义一致。

#### 4.3.3 源码精读

裁剪块整体：[crates/typst-render/src/lib.rs:L227-L259](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L227-L259)。

曲线转路径用的是 `shape::convert_curve`：[crates/typst-render/src/shape.rs:L87-L113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L87-L113) —— 遍历 `CurveItem`（Move/Line/Cubic/Close），逐一翻译成 `PathBuilder` 的对应调用：

```rust
pub fn convert_curve(curve: &Curve) -> Option<sk::Path> {
    let mut builder = sk::PathBuilder::new();
    for elem in &curve.0 {
        match elem {
            CurveItem::Move(p)   => builder.move_to(p.x.to_f32(), p.y.to_f32()),
            CurveItem::Line(p)   => builder.line_to(p.x.to_f32(), p.y.to_f32()),
            CurveItem::Cubic(p1, p2, p3) => builder.cubic_to(/* ... */),
            CurveItem::Close     => builder.close(),
        }
    }
    builder.finish()
}
```

注意它输出的是组局部 pt 坐标。随后 `.and_then(|path| path.transform(state.transform))`（[lib.rs:L228-L229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L228-L229)）把它变换到画布像素空间；若变换退化，`Path::transform` 返回 `None`，整个 `if let` 不成立，等价于「没有裁剪」。

「已有父遮罩」分支：[crates/typst-render/src/lib.rs:L231-L239](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L231-L239) —— `clone` 父遮罩后 `intersect_path` 求交。

「无父遮罩」分支：[crates/typst-render/src/lib.rs:L239-L256](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L239-L256) —— 新建全画布遮罩后 `fill_path`。其中 `Mask::new` 失败的处理：[lib.rs:L242-L246](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L242-L246)。

```rust
let pxw = canvas.width();
let pxh = canvas.height();
let Some(mut mask) = sk::Mask::new(pxw, pxh) else {
    // Fails if clipping rect is empty. In that case we just
    // clip everything by returning.
    return;
};
mask.fill_path(&path, sk::FillRule::default(), true, sk::Transform::default());
```

这里 `pxw`/`pxh` 取自**画布**尺寸（不是裁剪曲线包围盒）。按 tiny-skia 语义，`Mask::new(w, h)` 在 `w` 或 `h` 为 0、或字节数溢出时返回 `None`。由于 `render()` 用 `round().max(1.0)` 保证画布至少 1×1 像素（见 u1-l2），正常运行中这条 `else` 几乎不会触发，属防御性代码。

`Mask::new` 失败时为何 `return`（即「什么都不画」）？因为这一分支的职责是**建立**裁剪遮罩。建不出来时只有两种合理选择：「全画」或「全不画」。全画意味着放弃裁剪，会把本应被裁掉的内容泄漏出来（视觉错误，甚至可能暴露版式外内容）；全不画（`return`）则把整组裁剪干净——对「裁剪区域退化/为空」而言，这是安全、保守的失败方式。代码注释把它概括为 "clip everything by returning"。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：讲清裁剪两条分支的差别，以及 `Mask::new` 失败时 `return` 的原因。

**操作步骤**：

1. 打开 [crates/typst-render/src/lib.rs:L231-L256](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L231-L256)。
2. 对比两条分支：
   - 有父遮罩（`if let Some(mask) = mask`）：`let mut mask = mask.clone(); mask.intersect_path(&path, ...);`
   - 无父遮罩（`else`）：`Mask::new(pxw, pxh)?` → `mask.fill_path(&path, ...);`
3. 阅读注释 [L243-L245](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L243-L245)。

**需要观察的现象 / 预期结果**（即「解释二者差别」）：

| 维度 | 有父遮罩分支 | 无父遮罩分支 |
| --- | --- | --- |
| 起点 | `mask.clone()`（继承父遮罩已有的可绘制区） | `Mask::new(pxw, pxh)`（全画布、全 0 的空白遮罩） |
| 操作 | `intersect_path`：取父可绘制区 ∩ 当前裁剪曲线 | `fill_path`：直接用裁剪曲线画出可绘制区 |
| 语义 | **求交**（在已有约束上再收紧） | **建立**（从零定义可绘制区） |
| 失败处理 | `intersect_path` 无 `Option` 返回，无需判空 | `Mask::new` 返回 `Option`，失败则 `return` |

差别根源：父遮罩是否已经存在。若存在，必须在其基础上**继续求交**才能保留祖先的裁剪；若不存在，才需要先 `Mask::new` 建底图。这正是「裁剪逐层求交」在代码里的直接体现。

**「为什么 `Mask::new` 失败时直接 `return`」**：见 4.3.3 末尾——失败时选择「全不画」而非「全画」，是为了避免泄漏本应被裁掉的内容，是安全保守的失败方式。

**待本地验证**：可在 `render_group` 临时为「无父遮罩」分支加一行日志，编译一个带 `block(clip: true)[...]` 的 Typst 文档到 PNG，确认顶层 clip 走的是「无父遮罩」分支、嵌套 clip 走的是「求交」分支（运行验证属可选项）。

#### 4.3.5 小练习与答案

**练习 1**：裁剪曲线为什么要先 `path.transform(state.transform)` 再做 `fill_path`？

**参考答案**：`Mask` 是按**画布像素**组织的（尺寸取自 `canvas.width()/height()`），而裁剪曲线 `group.clip` 定义在**组的局部 pt 坐标系**里。`state.transform` 正是「组局部 → 画布像素」的累积变换（含 `pixel_per_pt` 缩放、`pos` 平移、组 transform、以及所有祖先的变换）。不先变换，遮罩与画布像素就对不上号，裁剪会错位。

**练习 2**：如果 `convert_curve` 返回 `None`（比如曲线没有任何有效段），会发生什么？

**参考答案**：外层 `if let Some(path) = ...` 不成立，整个裁剪块被跳过，`mask` 保持为 `state.mask`（父遮罩）。效果等同于「该组没有裁剪」——这是合理的退化：无效裁剪曲线不应阻断渲染。

**练习 3**：`fill_path` 与 `intersect_path` 都传了 `invert = true`。如果误写成 `false`，裁剪外观会怎样？

**参考答案**：可绘制区会变成「曲线**外部**」，即曲线内部被裁掉、外部被保留——与 Typst 裁剪语义相反。两条分支必须用同一个 `invert` 取值，才能保证「建立」与「求交」得到一致的可绘制区定义。

---

### 4.4 with_mask：遮罩如何沿递归向下传递

#### 4.4.1 概念说明

计算出新的遮罩后，要让它对组内**所有**后代生效——包括多层嵌套的子组与叶子。这一职责由 `State::with_mask` 与「不可变 State + Copy」共同完成：把遮罩塞进 state，state 随递归一路向下；每遇到带裁剪的子组，就在继承来的遮罩上再求交，得到更窄的遮罩继续下传。

#### 4.4.2 核心流程

```
render_group 算出 mask（可能 = 父遮罩，可能 = 新遮罩）
  → render_frame(canvas, state.with_mask(mask), &group.frame)
      → 遍历子 items，state（含 mask）原样传给每个子节点：
          - 叶子（Shape/Text/Image）：把 state.mask 透传给 tiny-skia 绘制调用
          - 子 Group：进入 render_group，若它也有 clip，
                      就在继承来的 mask 上 clone+intersect，再 with_mask 下传
```

关键性质：

- **遮罩只增不减（单调收紧）**：每层只能在父遮罩基础上求交，使可绘制区越来越小，永不放大。
- **无裁剪即透传**：若某组没有 `clip`，`mask` 保持为 `state.mask`，`with_mask(mask)` 等于原样把父遮罩下传。

#### 4.4.3 源码精读

`with_mask` 实现：[crates/typst-render/src/lib.rs:L167-L169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L167-L169)：

```rust
/// Sets the current mask.
/// If no mask is provided, the parent mask is used.
fn with_mask(self, mask: Option<&'a sk::Mask>) -> State<'a> {
    State { mask: mask.or(self.mask), ..self }
}
```

`mask.or(self.mask)`：传入 `Some` 就用新遮罩，传入 `None` 就沿用父遮罩。在 `render_group` 末尾的调用 [L261](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L261)，`mask` 要么是父遮罩（无 clip 时）、要么是新算出的 `&storage`（有 clip 时），都是 `Some`，所以这里实际是「替换为新遮罩」；`or` 的回退分支主要服务于其他调用点（如字形子帧传入 `None` 以表示「沿用父遮罩」，详见 u2-l6/u3-l3）。

`mask` 字段的类型是 `Option<&'a sk::Mask>`（[L123](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/lib.rs#L123)）——一个**借用**，而非拥有。这就是为什么裁剪块里需要一个 `let storage;` 局部变量来「拥有」新遮罩：`storage` 活到 `render_group` 函数结束，从而保证 `mask = Some(&storage)` 这个引用在递归调用 `render_frame` 期间始终有效。生命周期 `'a` 把「state 里借用的 mask」与「持有该 mask 的栈帧」绑定起来。

叶子如何消费遮罩：以形状为例，[crates/typst-render/src/shape.rs:L52](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-render/src/shape.rs#L52) —— `canvas.fill_path(&path, &paint, rule, ts, state.mask)`，把 `state.mask` 作为 tiny-skia 的裁剪遮罩传入；文本与图像同理（详见 u2-l4/u2-l5/u3-l3）。

#### 4.4.4 代码实践

**实践目标**：跟踪两层嵌套裁剪，预测叶子处生效的遮罩。

**操作步骤**：

1. 设场景：页面 → 组 A（clip = 曲线 \(c_A\)）→ 组 B（clip = 曲线 \(c_B\)）→ 一个 Shape。页面层 `state.mask = None`（`State::default`）。
2. 进入组 A：无父遮罩 → 走「无父遮罩」分支，`storage_A` = `Mask::new` + `fill_path(c_A)`；`mask = Some(&storage_A)`。
3. `render_frame` 带着 `state.with_mask(Some(&storage_A))` 进入组 A 的子帧，继而进入组 B。
4. 进入组 B：**有**父遮罩（`storage_A`）→ 走「有父遮罩」分支，`storage_B` = `storage_A.clone()` + `intersect_path(c_B)`；`mask = Some(&storage_B)`。
5. 最终 Shape 处的 `state.mask = Some(&storage_B)`，可绘制区 = \(c_A \cap c_B\)。

**需要观察的现象 / 预期结果**：叶子可绘制区是两层裁剪曲线在画布像素空间的交集；这正是 `Mask::intersect_path` 逐层求交的累计结果。若组 B 没有 clip，则其 `mask` 保持 `storage_A`，叶子可绘制区仍是 \(c_A\)。

**待本地验证**：可构造一个 Typst 文档，外层与内层都用 `block(clip: true)`（或 `circle`/`rect` 作 mask），编译成 PNG 肉眼确认「交集区域」可见、其余被裁掉。

#### 4.4.5 小练习与答案

**练习 1**：`render_group` 里 `let storage;` 为什么必须显式声明，而不能直接 `let storage = ...;`？

**参考答案**：因为 `storage` 的赋值发生在 `if let` 的两个不同分支里（有父遮罩分支和无父遮罩分支各算出一个 `Mask`）。把声明与赋值分离，让两个分支都能为同一个 `storage` 赋值，从而在 `if let` 之外用统一的 `mask = Some(&storage)` 拿到引用。`storage` 的生命周期延续到函数末尾，覆盖了递归调用 `render_frame` 的整段时间，保证借用有效。

**练习 2**：遮罩字段为什么用 `Option<&'a sk::Mask>`（借用）而不是 `Option<sk::Mask>`（拥有）？

**参考答案**：为了避免每次递归都深拷贝整张画布大小的遮罩。`State` 派生 `Copy`，若字段是拥有的 `Mask`，每次 `state.pre_translate(...)` 这样的不可变更新都会克隆整张遮罩（可能数百万像素），代价极高。用借用，则 `Copy` 只复制一个指针；只有真正需要修改遮罩时（即遇到 clip），才在「有父遮罩」分支里显式 `mask.clone()` 一次。这是一种「写时复制」式的省拷贝策略。

---

## 5. 综合实践

**任务**：把本讲三件事——`render_group` 三段式、Soft/Hard 对 `container_transform` 的更新、遮罩逐层求交——串起来分析一个三层场景，写出叶子处的 `container_transform`、`size`、`mask`。

**场景**：

```
页面（page.frame，由 render() 经 State::new 自举为硬容器）
  └─ Group G1（FrameKind::Hard，transform=t1，clip=c1）
       └─ Group G2（FrameKind::Soft，transform=t2，clip=c2）
            └─ Shape S（一个纯色矩形）
```

**要求**：

1. **变换与容器**：跟踪进入 G1、G2 后的 `state.transform`、`state.container_transform`、`state.size`。指出在哪一步 `container_transform` 被重置、为什么 G2 不重置。
2. **遮罩**：写出 S 处 `state.mask` 对应的可绘制区（用 \(c_1\)、\(c_2\) 表达），并说明 G1、G2 分别走了 4.3 的哪条分支。
3. **可选运行验证**：写一份 Typst 文档，用嵌套的 `block(clip: true)`（或用 `circle`/`rect` 作为可视参考）生成类似结构，编译为 PNG，肉眼确认：矩形 S 只在 \(c_1 \cap c_2\) 区域可见。

**预期结论（自测用）**：

1. 进入 G1（Hard）：`transform` 叠加 `translate(pos_G1) ∘ t1`；`container_transform` 被 4.2 的三次 `pre_concat_container` 重置为新的 `transform`；`size` 重置为 G1 帧尺寸。进入 G2（Soft）：`transform` 继续叠加 `translate(pos_G2) ∘ t2`；`container_transform`、`size` **不变**（仍指向 G1）。所以 S 处的容器参考是 G1，而非页面。
2. G1 无父遮罩（页面层 `mask=None`）→ 走「无父遮罩」分支（`Mask::new` + `fill_path(c1)`）。G2 有父遮罩 → 走「有父遮罩」分支（`clone` + `intersect_path(c2)`）。S 处可绘制区 = \(c_1 \cap c_2\)。
3. 运行验证属可选项；若无 typst CLI 环境，标注「待本地验证」即可。

## 6. 本讲小结

- `render_group` 是处理树枝节点 `Group` 的中枢，按「算变换 → 算遮罩 → 递归」三段执行；组的 `pos` 由它自己在第一段 `pre_translate`，而非在 `render_frame` 派发层提前消费。
- `FrameKind::Soft` 只更新 `transform`；`FrameKind::Hard` 额外把 `container_transform` 重置为新 `transform`、把 `size` 重置为本帧尺寸，维持「硬帧边界处 container_transform == transform」的不变量。重置靠「用逆抵消旧值再叠目标」实现。
- 裁剪链路是 `convert_curve`（Curve→Path）→ `path.transform(state.transform)`（→画布像素）→ `Mask`；无父遮罩时 `Mask::new`+`fill_path` **建立**，有父遮罩时 `clone`+`intersect_path` **求交**。
- `Mask::new` 失败时 `return`（全不画）是安全保守的失败方式，避免泄漏本应被裁掉的内容。
- 遮罩经 `with_mask` 装入 state、随递归向下传递，逐层求交、单调收紧；`Option<&Mask>` 借用 + `let storage` 的生命周期管理，让深递归中只有真正遇到 clip 时才克隆一次遮罩。
- `container_transform`/`size` 的**消费方**（`RelativeTo::Parent` 渐变采样）留待 u3-l1 展开。

## 7. 下一步学习建议

- **形状与描边（u2-l4）**：`convert_curve` 在本讲只作「裁剪曲线→路径」用，下一讲会看到它如何服务于 `render_shape` 的填充与描边，以及 `FillRule`、描边参数的映射。
- **图像渲染（u2-l5）**：图像同样会把 `state.mask` 透传到 `draw_pixmap`，可对照本讲的遮罩传递理解图像如何被组裁剪。
- **渐变填充（u3-l1）**：这是 `container_transform`/`size` 的真正消费者，读过后你会彻底理解 Hard 帧重置容器的用意。
- **字形光栅化与混合（u3-l3）**：`write_bitmap` 在「有遮罩」时会先渲染到带 1px padding 的临时画布再合成，正是本讲遮罩机制的延续，建议作为进阶阅读。
