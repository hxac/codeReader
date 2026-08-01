# 填充/描边入口与去重引用模型

## 1. 本讲目标

本讲是「绘制系统」单元的枢纽。上一篇（u5-l1）解决了「一种纯色如何变成字符串」，本讲解决「一个 `Paint` 如何落到 SVG 的 `fill` / `stroke` 属性上」，以及更重要的——**当一个渐变或平铺图案在同一篇文档里被反复使用时，typst-svg 如何避免把它的完整定义重复写很多遍**。

学完后你应当能够：

1. 读懂 `write_fill` / `write_stroke` 对 `Paint` 三种变体（`Solid` / `Gradient` / `Tiling`）的分发逻辑，以及 `fill-rule`、描边属性如何写出。
2. 说清 `push_gradient` / `push_tiling` 的「**源（source）+ 引用（ref）**」两层去重模型：源不带变换、可被多处复用；引用带变换、用 `href` 指回源。
3. 解释 `ts.is_identity()` 快速路径为什么直接返回源 ID，以及这种设计如何随复用次数增长而大幅节省文件体积。
4. 看懂 `write_gradient_refs` / `write_tiling_refs` 如何把每个引用写成一个近乎空壳、仅带 `gradientTransform` / `patternTransform` 与 `href`（兼容 `xlink:href`）的元素。

## 2. 前置知识

在进入源码前，先约定几个本讲反复用到的概念。

- **`Paint`**：Typst 里「用什么涂」的枚举，三变体——`Solid(Color)`（纯色）、`Gradient(Gradient)`（渐变）、`Tiling(Tiling)`（平铺图案）。本讲的入口函数就是按它三路分发。
- **`FillRule`**：填充规则，决定重叠区域是否被填充，只有 `NonZero` / `EvenOdd` 两种，对应 SVG 的 `fill-rule="nonzero"` / `"evenodd"`。
- **`FixedStroke`**：描边参数集合，含 `paint`（同样是 `Paint`）、`thickness`、`cap`、`join`、`miter_limit`、可选 `dash`。
- **`Transform`**：仿射变换矩阵。本讲里 `ts` 特指「把单位渐变/图案映射到某个具体使用位置」的变换。`ts.is_identity()` 表示无需变换。
- **`<defs>` 与 `url(#id)`**：SVG 的复用机制。可复用资源（渐变、图案、字形、裁剪路径）集中写在 `<defs>` 里并各取一个 `id`；使用处用 `fill="url(#id)"` 引用。本讲要回答的核心问题是：**这个 `id` 指向的到底是「源定义」还是「引用定义」？**
- **`Deduplicator`**：typst-svg 自带的去重容器，u2-l1 已介绍它持有 7 份、各带一个命名空间字符（`g/c/f/r/s/t/p`）。其内部机制（`hash128` 键、`DedupId` 编码）留到 u6-l3 深入，本讲只需把它当成「按键去重、返回稳定 `DedupId`」的黑盒。

> 关于纯色 `Color` 如何序列化成 `#rrggbb` / `oklch(...)` 等字符串，已在 u5-l1 讲透，本讲直接把 `color` 当作「能被写成 SVG 文本」的值来用。

## 3. 本讲源码地图

本讲横跨两个文件，二者都以 `impl SVGRenderer` 为同一个被拼装出来的类型添加方法（延续 u1-l2「状态集中、行为分散」的主题）：

| 文件 | 本讲涉及的关键符号 | 作用 |
| --- | --- | --- |
| `src/paint.rs` | `write_fill`、`push_gradient`、`push_tiling`、`write_gradients`、`write_gradient_refs`、`write_tilings`、`write_tiling_refs`、`GradientRef`、`TilingRef`、`GradientKind` | 填充入口、渐变/平铺的两层去重与引用写出 |
| `src/shape.rs` | `render_shape`、`shape_paint_transform`、`shape_fill_size`、`write_stroke` | 描边入口，以及计算传入 `write_fill` 的 `ts` 与 `aspect_ratio` |
| `src/lib.rs` | `SVGRenderer` 的 7 个字段、`finalize`、`Deduplicator`、`DedupId` | 渲染器状态集中存放去重表，`finalize` 统一写出定义 |
| `src/write.rs` | `SvgUrl`、`SvgIdRef`、`SvgTransform` | 把 `DedupId` / `Transform` 适配成 `url(#…)` / `#…` / `matrix(…)` 文本 |

数据流总览（先见森林）：

```
render_shape  ──► write_fill / write_stroke ──► push_gradient / push_tiling
                  （写 fill=/stroke= 属性）        （渲染期：登记进去重表，返回 ID）
                                                            │
                                                            ▼
                              finalize ──► write_gradients   （写「源」定义，无变换）
                                          write_gradient_refs（写「引用」定义，带变换 + href）
                                          write_tilings / write_tiling_refs（同构）
```

关键时间差：**渲染期只登记 + 写引用属性，真正的 `<defs>` 定义推迟到 `finalize` 阶段集中写出**。这个时间差正是两层去重得以成立的前提。

## 4. 核心概念与源码讲解

### 4.1 入口分发：write_fill 与 write_stroke

#### 4.1.1 概念说明

`write_fill` 和 `write_stroke` 是绘制系统的「前台」。它们接收一个已经算好变换 `ts` 和宽高比 `aspect_ratio` 的 `Paint`，把它翻译成 SVG 元素上的 `fill="…"` 或 `stroke="…"` 属性。

二者的结构高度对称：都先对 `Paint` 做三路分发，再补充各自特有的属性（填充补 `fill-rule`，描边补 `thickness`/`linecap`/`linejoin`/`miterlimit`/`dash`）。唯一的差异在于分发后纯色之外的两种 `Paint` 如何处理：

- `Solid` → 直接把颜色字符串写进属性。
- `Gradient` / `Tiling` → 调用 `push_gradient` / `push_tiling` 拿到一个 `DedupId`，再用 `SvgUrl(id)` 包成 `url(#id)` 写进属性。

也就是说，**入口函数本身不关心渐变/平铺的几何细节，它只负责「要一个 ID、写成 URL 引用」**。至于这个 ID 指向源还是引用，由 `push_*` 内部决定（见 4.2）。

#### 4.1.2 核心流程

`write_fill` 的分发表：

| `Paint` 变体 | 写出的属性 | 附带处理 |
| --- | --- | --- |
| `Solid(color)` | `fill="<颜色字符串>"` | 颜色经 `SvgDisplay for Color`（u5-l1） |
| `Gradient(g)` | `fill="url(#<id>)"` | `id = push_gradient(g, aspect_ratio, ts)` |
| `Tiling(t)` | `fill="url(#<id>)"` | `id = push_tiling(t, ts)` |

之后无条件追加 `fill-rule`：`NonZero → "nonzero"`、`EvenOdd → "evenodd"`。

`write_stroke` 的分发与上表同构（`fill` 换成 `stroke`），只是 `Tiling` 分支的 `push_tiling` 不需要 `aspect_ratio`（平铺不涉及角度修正）。分发完毕后再写出描边专属属性：`stroke-width`、`stroke-linecap`（`butt`/`round`/`square`）、`stroke-linejoin`（`miter`/`round`/`bevel`）、`stroke-miterlimit`，以及可选的 `stroke-dashoffset` + `stroke-dasharray`。

伪代码：

```
fn write_fill(svg, fill, fill_rule, aspect_ratio, ts):
    match fill:
        Solid(c)      → svg.fill = c
        Gradient(g)   → svg.fill = url(push_gradient(g, aspect_ratio, ts))
        Tiling(t)     → svg.fill = url(push_tiling(t, ts))
    svg.fill-rule = match fill_rule { NonZero→nonzero, EvenOdd→evenodd }
```

#### 4.1.3 源码精读

先看入口 `write_fill`（位于 paint.rs）：

[src/paint.rs:32-57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L32-L57) —— 按 `Paint` 三变体分发；渐变/平铺走 `push_*` 拿 ID，再用 `SvgUrl(id)` 写成 `url(#…)`；末尾补 `fill-rule`。

```rust
match fill {
    Paint::Solid(color) => { svg.attr("fill", color); }
    Paint::Gradient(gradient) => {
        let id = self.push_gradient(gradient, aspect_ratio, ts);
        svg.attr("fill", SvgUrl(id));
    }
    Paint::Tiling(tiling) => {
        let id = self.push_tiling(tiling, ts);
        svg.attr("fill", SvgUrl(id));
    }
}
match fill_rule {
    FillRule::NonZero => svg.attr("fill-rule", "nonzero"),
    FillRule::EvenOdd => svg.attr("fill-rule", "evenodd"),
};
```

`SvgUrl(id)` 是 write.rs 里的适配器，它把 `DedupId` 格式化成 `url(#<id>)`：[src/write.rs:293-301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L293-L301)。

再看描边入口 `write_stroke`（注意它位于 **shape.rs**，而非 paint.rs——这两个入口被拆在不同文件，却都挂在同一个 `SVGRenderer` 上）：

[src/shape.rs:128-173](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L128-L173) —— `stroke.paint` 的三路分发与 `write_fill` 完全对称，随后写出描边专属属性。

```rust
match &stroke.paint {
    Paint::Solid(color) => { svg.attr("stroke", color); }
    Paint::Gradient(gradient) => {
        let id = self.push_gradient(gradient, aspect_ratio, fill_transform);
        svg.attr("stroke", SvgUrl(id));
    }
    Paint::Tiling(tiling) => {
        let id = self.push_tiling(tiling, fill_transform);
        svg.attr("stroke", SvgUrl(id));
    }
}
svg.attr("stroke-width", stroke.thickness.to_pt());
// … linecap / linejoin / miterlimit / dash …
```

那传入 `write_fill` 的 `ts` 与 `aspect_ratio` 从何而来？看 `render_shape` 的调用点：

[src/shape.rs:21-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L21-L28) —— 填充分支：`aspect_ratio` 由 `shape_fill_size(...).aspect_ratio()` 给出，`ts` 由 `shape_paint_transform(...)` 给出（描边分支多传一个 `include_stroke_in_bbox = true`，见 [src/shape.rs:33-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L33-L40)）。

```rust
if let Some(paint) = &shape.fill {
    self.write_fill(
        svg, paint, shape.fill_rule,
        self.shape_fill_size(state, paint, shape).aspect_ratio(),
        self.shape_paint_transform(state, paint, shape, false),
    );
} else {
    svg.attr("fill", "none");
}
```

`shape_paint_transform` 的关键逻辑（`RelativeTo::Self_` 用形状自身包围盒、`RelativeTo::Parent` 用父级 frame 尺寸加 `state.transform` 的逆）已在 u3-l2 详述，这里只需记住：**它产出的 `ts` 几乎总不是单位矩阵**（除非形状恰好 1pt × 1pt 且位于原点），这一点对 4.2 的快速路径讨论至关重要。完整实现见 [src/shape.rs:51-103](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L51-L103)。

#### 4.1.4 代码实践

**实践目标**：用 Typst 源码观察三种 `Paint` 变体在最终 SVG 里分别长什么样。

**操作步骤**（若已安装 typst CLI；未安装则改为阅读型实践，见下方）：

1. 新建 `fill.typ`（下列为 Typst 示例代码，非本 crate 内容）：

   ```typst
   #rect(width: 60pt, height: 40pt, fill: red)
   #rect(width: 60pt, height: 40pt, fill: gradient.linear(red, blue))
   ```

2. 编译为 SVG：`typst compile fill.typ fill.svg`。
3. 在 `fill.svg` 中搜索 `fill=`。

**需要观察的现象**：

- 第一个矩形：`fill="#FF0000"`（或等价 hex），即 `Solid` 走的颜色字符串路径。
- 第二个矩形：`fill="url(#r…)"`——注意 ID 以 `r` 开头，说明它引用的是一个 **GradientRef**（见 4.2），而非源渐变（源以 `f` 开头）。

**阅读型替代**（不装 CLI 时）：直接对照 [src/paint.rs:32-57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L32-L57) 与 `SvgUrl` 的格式化（[src/write.rs:293-301](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L293-L301)），手工推断三种 `Paint` 各自产生的属性文本，并与上述预期对照。

**预期结果**：纯色直接出颜色串；渐变/平铺出 `url(#<id>)`，且 `<id>` 的首字符由 `push_*` 返回的是「源 ID」还是「引用 ID」决定。

#### 4.1.5 小练习与答案

**练习 1**：`write_fill` 里 `fill_rule` 的 `match` 为什么写在 `match fill` 之后、且没有 `else` 分支？

> **答案**：`fill_rule` 对三种 `Paint` 一视同仁，都要写出 `fill-rule` 属性，所以它独立于 `fill` 的分发、无条件执行；`FillRule` 只有 `NonZero`/`EvenOdd` 两个变体，`match` 已穷尽，无需 `else`。

**练习 2**：`write_stroke` 的 `Tiling` 分支调用 `push_tiling(tiling, fill_transform)`，比 `write_fill` 少传一个 `aspect_ratio`。为什么平铺不需要它？

> **答案**：`aspect_ratio` 用于修正**渐变角度**在不同宽高比下的视觉变形（u5-l3 详述）；平铺图案没有「角度」这一概念（其 `angle` 已作为去重键的一部分直接进入 `push_tiling`），因此不需要额外的宽高比修正参数。

---

### 4.2 两层去重模型：源 + 引用

#### 4.2.1 概念说明

这是本讲的核心。考虑一个常见场景：文档里 10 个不同尺寸的矩形都用同一个线性渐变 `g` 填充。最朴素的写法是把整段 `<linearGradient>` 定义在每个使用处复制一份、各自带上不同的 `gradientTransform`。这样写出来的 SVG 里，同一段渐变定义（含全部 stops、颜色）会出现 10 次，体积爆炸。

typst-svg 的解法是把「**渐变本身**」和「**把它摆到哪、缩放多少**」拆成两层：

- **源（source）**：不带任何使用变换的「纯渐变定义」。键里**不含** `ts`，因此同一渐变无论被用在何处都只存一份。
- **引用（ref）**：只记录「指向哪个源 + 一个变换矩阵」。键是 `(源ID, ts)`，每个不同的使用变换各存一份，但每份极小。

使用处（`fill="url(#…)"`）引用的是「引用」；而「引用」又通过 `href` 指回「源」。最终 SVG 渲染器（浏览器、resvg）解析 `href` 把源定义「代换」进来，再套上引用自带的变换。

体积收益（设源定义大小为 \(S\)，引用大小为 \(R\)，复用次数为 \(N\)）：

\[
\text{朴素复制} = \Theta(N\cdot S),\qquad
\text{源+引用} = \Theta\!\left(S + N\cdot R\right)
\]

由于引用几乎是个空壳（一个 ID + 一个变换），\(R \ll S\)，当 \(N\) 增大时节省非常显著。

#### 4.2.2 核心流程

`push_gradient` 的两步：

1. **第一层（源）**：以 `(gradient, aspect_ratio)` 为键插入 `self.gradients`（命名空间字符 `'f'`），得到源 ID `gradient_id`。注意键里**没有** `ts`。
2. **快速路径**：若 `ts.is_identity()`，直接返回 `gradient_id`——使用处直接引用源，连引用层都不创建（这是文档注释里点名的「file size optimization」）。
3. **第二层（引用）**：否则以 `(gradient_id, ts)` 为键插入 `self.gradient_refs`（命名空间字符 `'r'`），值为 `GradientRef { id: gradient_id, kind, transform: ts }`，返回引用 ID。

`push_tiling` 同构：第一层键是 `(tiling_size, tiling_offset, tiling_angle, rendered_string)`（命名空间 `'t'`），快速路径后第二层插入 `TilingRef`（命名空间 `'p'`）。

> **关键洞察**：使用处拿到的 `DedupId`，其首字符（`f`/`t` 表示源，`r`/`p` 表示引用）直接告诉你它走了哪条路径——`ts` 为单位矩阵走源，否则走引用。

#### 4.2.3 源码精读

`push_gradient` 全文很短，却浓缩了整个模型：

[src/paint.rs:66-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L66-L85) —— 先把 `(gradient, aspect_ratio)` 登记为源（键不含 `ts`）；`ts` 为单位矩阵则直接返回源 ID；否则再登记一个带变换的 `GradientRef` 并返回引用 ID。

```rust
let gradient_id = self
    .gradients
    .insert_with((gradient, aspect_ratio), || (gradient.clone(), aspect_ratio));

if ts.is_identity() {
    return gradient_id;
}

self.gradient_refs
    .insert_with(&(gradient_id, ts), || GradientRef {
        id: gradient_id,
        kind: gradient.into(),
        transform: ts,
    })
```

`push_tiling` 与之对称，只是源键更复杂——它把**渲染出来的 SVG 字符串**也作为键的一部分：

[src/paint.rs:87-114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L87-L114) —— 渲染一次 tiling frame，用 `(size, offset, angle, rendered)` 作源键（因为 `Tiling` 含不稳定的 `Location`，不能直接作键）；同样有 `ts.is_identity()` 快速路径；否则插入 `TilingRef`。

```rust
let rendered = self.render_tiling_frame(&State::new(tiling_size), tiling.frame());
let tiling_id = self.tilings.insert_with(
    (tiling_size, tiling_offset, tiling_angle, rendered.as_str()),
    || tiling.clone(),
);
if ts.is_identity() {
    return tiling_id;
}
let transform = ts.pre_concat(tiling.transform());
let tiling_ref = TilingRef { id: tiling_id, transform };
self.tiling_refs.insert_with(tiling_ref, || tiling_ref)
```

> 这里 `render_tiling_frame` 之所以「渲染两次」、以及 `ts.pre_concat(tiling.transform())` 的微妙之处（被引用 pattern 的 `patternTransform` 会被 `href` 覆盖，需提前拼接），留待 u5-l5 详述。本讲只需理解它的两层结构与 `push_gradient` 同构。

两个引用结构体的定义：

[src/paint.rs:400-413](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L400-L413) —— `GradientRef`：存源 ID、`kind`（为何只存种类而不克隆整个渐变？见下）、变换。

[src/paint.rs:388-398](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L388-L398) —— `TilingRef`：只存源 ID 和变换；平铺恒为 `<pattern>`，无需 `kind` 字段。

`GradientRef.kind` 的作用：写出引用时需要知道该用哪种 SVG 元素名（`linearGradient`/`radialGradient`/`pattern`）和哪个变换属性名（`gradientTransform`/`patternTransform`）。与其为此克隆整个 `Gradient`，不如只记一个轻量枚举 `GradientKind`：

[src/paint.rs:430-449](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L430-L449) —— `GradientKind` 三变体，及 `From<&Gradient>` 如何把 `Gradient` 压缩成种类。

这 7 张去重表都挂在 `SVGRenderer` 上（u2-l1 已建立的全景），命名空间字符各异：

[src/lib.rs:272-283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L272-L283) —— 构造时给 7 个 `Deduplicator` 各分配一个字符：`gradients='f'`、`gradient_refs='r'`、`tilings='t'`、`tiling_refs='p'`（另有 `glyphs='g'`、`clip_paths='c'`、`conic_subgradients='s'`）。

```rust
gradients: Deduplicator::new('f'),
gradient_refs: Deduplicator::new('r'),
conic_subgradients: Deduplicator::new('s'),
tilings: Deduplicator::new('t'),
tiling_refs: Deduplicator::new('p'),
```

字段的文档注释也把「源 + 引用」的动机写得很清楚，值得一读：[src/lib.rs:198-224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L198-L224)。

至于「按 key 哈希、惰性构造值」的 `insert_with` 语义，本讲当成黑盒即可（键经 `typst_utils::hash128` 压成 `u128`，命中则不调用构造闭包），完整机制留到 u6-l3：[src/lib.rs:492-512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L492-L512)。

#### 4.2.4 代码实践

**实践目标**：吃透 `push_gradient` 在 `ts.is_identity()` 时的快速路径，并解释为什么现实里这条路径很少被命中。

**操作步骤**：

1. 阅读 [src/paint.rs:66-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L66-L85)，确认：源 ID 在第 72-74 行就拿到了；第 76-78 行的 `if ts.is_identity()` 决定「是否还要再插一个引用」。
2. 回溯 `ts` 的来源：[src/shape.rs:51-103](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L51-L103)。对一个 `RelativeTo::Self_` 的渐变，`ts = scale(size.x, size.y).post_concat(translate(offset))`。
3. 思考：要让 `ts.is_identity()` 为真，需要 `scale` 的两个分量都是 1、且平移为零——即形状包围盒恰好 1pt × 1pt 且位于原点。

**需要观察的现象 / 推理结论**：

- 快速路径成立时，`fill="url(#f…)"`（源，首字符 `f`）。
- 否则 `fill="url(#r…)"`（引用，首字符 `r`）。
- 由于真实形状的包围盒几乎不可能恰好 1pt × 1pt，**绝大多数渐变填充最终都走引用层**。快速路径更多服务于一些特殊场景（例如某些以单位坐标系直接定义、不经形状变换的用法）。

**预期结果**：你能用自己的话讲清「`ts` 是否为单位矩阵」如何决定返回源 ID 还是引用 ID，以及为什么日常 SVG 产物里 `fill="url(#r…)"` 比 `fill="url(#f…)"` 常见得多。（若想验证观察，可用 4.1.4 的 Typst 示例编译后统计 `url(#f` 与 `url(#r` 的出现次数。）

#### 4.2.5 小练习与答案

**练习 1**：`push_gradient` 第一层的键是 `(gradient, aspect_ratio)`，为什么要把 `aspect_ratio` 也纳入键，而不是只拿 `gradient` 作键？

> **答案**：因为「源」要在 `finalize` 阶段被 `write_gradients` 写出，而写线性渐变时需要用 `aspect_ratio` 修正角度（u5-l3）。两个内容相同但 `aspect_ratio` 不同的渐变，其修正后的端点坐标不同，是**不同的源定义**，必须分别存储，所以 `aspect_ratio` 进了键。

**练习 2**：`GradientRef` 为什么存 `kind: GradientKind` 而不是 `gradient: Gradient`？

> **答案**：写出引用时只需知道用哪种 SVG 元素名和变换属性名（线性/径向用 `gradientTransform`，圆锥用 `patternTransform`），这些由 `GradientKind` 三变体即可决定；克隆整个 `Gradient` 既要拷数据又用不上其几何细节，纯属浪费。

**练习 3**：`push_tiling` 的源键为何要用「渲染出来的字符串」而不是 `Tiling` 本身？

> **答案**：`Tiling` 内部含 `Location`（指向文档中某位置），其哈希/相等性不稳定，无法可靠作键。改用「实际渲染出的 SVG 文本」作键，既规避了 `Location` 的不稳定性，又天然把「视觉上相同」的平铺归为一类。

---

### 4.3 引用定义的写出：write_gradient_refs / write_tiling_refs

#### 4.3.1 概念说明

4.2 解决了「渲染期如何登记」。本小节解决「`finalize` 阶段如何把引用写成 SVG」。

每个 `GradientRef` / `TilingRef` 会被写成一个**近乎空壳**的元素：自身不带任何 stops 或几何，只有三样东西——

1. 一个变换属性（`gradientTransform` 或 `patternTransform`），承载这个使用位置特有的变换；
2. 一个 `id`，供使用处的 `url(#…)` 引用；
3. 一个 `href`（外加 `xlink:href`），指回源定义。

这样，SVG 渲染器在解析时会把源定义「代换」到引用元素的位置，再套上引用自带的变换，视觉上等价于「带变换的完整渐变」，但文件里只存了源 + 空壳。

为什么要同时写 `href` 和 `xlink:href`？`xlink:href` 是旧规范（SVG 1.1）的写法，`href` 是新规范（SVG 2）的简写。为兼容老旧渲染器，typst-svg 两个都写。

#### 4.3.2 核心流程

`write_gradient_refs` 对每个引用：

1. 据 `kind` 选 `(元素名, 变换属性名)`：`Linear → ("linearGradient","gradientTransform")`、`Radial → ("radialGradient","gradientTransform")`、`Conic → ("pattern","patternTransform")`。
2. 写出 `<元素 变换属性="…" id="r…" href="#f…" xlink:href="#f…">`（无子元素）。

`write_tiling_refs` 更简单——平铺恒为 `<pattern>`，无需 `kind` 分支：每个引用写成 `<pattern patternTransform="…" id="p…" href="#t…" xlink:href="#t…">`。

这两个函数都在 `finalize` 里被调用，且**在源定义之后**写出（先有源、后有引用，符合阅读顺序）。

#### 4.3.3 源码精读

`write_gradient_refs`：

[src/paint.rs:318-338](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L318-L338) —— 据 `kind` 选元素名与变换属性名，写出一个仅含 `transform / id / href / xlink:href` 的空壳元素。

```rust
for (id, gradient_ref) in self.gradient_refs.iter() {
    let (elem_name, transform_name) = match gradient_ref.kind {
        GradientKind::Linear => ("linearGradient", "gradientTransform"),
        GradientKind::Radial => ("radialGradient", "gradientTransform"),
        GradientKind::Conic  => ("pattern", "patternTransform"),
    };
    defs.elem(elem_name)
        .attr(transform_name, SvgTransform(gradient_ref.transform))
        .attr("id", id)
        .attr("href", SvgIdRef(gradient_ref.id))
        .attr("xlink:href", SvgIdRef(gradient_ref.id));
}
```

`write_tiling_refs` 是它的平铺版（无 `kind` 分支）：

[src/paint.rs:370-385](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L370-L385) —— 每个引用写成一个带 `patternTransform / id / href / xlink:href` 的空 `<pattern>`。

这里出现两个适配器：

- `SvgIdRef(id)` 把 `DedupId` 格式化成 `#<id>`（注意是 `#`，不是 `url(#…)`）——用于 `href` 的值：[src/write.rs:303-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L303-L311)。
- `SvgTransform(ts)` 把 `Transform` 格式化成最短形式（纯缩放→`scale(...)`、纯平移→`translate(...)`、否则→`matrix(...)`）——用于变换属性的值：[src/write.rs:253-290](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L253-L290)。

> 对比 `SvgUrl`（4.1.3，产出 `url(#…)`，用于 `fill`/`stroke`）与 `SvgIdRef`（产出 `#…`，用于 `href`）——同一个 `DedupId`，因所处属性不同而用不同适配器。

`finalize` 把所有定义按固定顺序写出，注意源与引用的先后：

[src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419) —— 依次写 `glyph_defs → clip_path_defs → gradients → gradient_refs → subgradients → tilings → tiling_refs`。

```rust
self.write_glyph_defs(&mut svg);
self.write_clip_path_defs(&mut svg);
self.write_gradients(&mut svg);       // 源渐变（无变换）
self.write_gradient_refs(&mut svg);   // 引用（带变换 + href）
self.write_subgradients(&mut svg);    // 圆锥渐变的子渐变（u5-l4）
self.write_tilings(&mut svg);         // 源平铺
self.write_tiling_refs(&mut svg);     // 引用（带变换 + href）
```

`write_gradients`（源）写出线性/径向渐变时**只写几何参数、不写 `gradientTransform`**——这正是「源不带变换」的体现（圆锥分支用 `pattern` 近似，细节见 u5-l4）：[src/paint.rs:117-284](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L117-L284)。

#### 4.3.4 代码实践

**实践目标**：验证 `write_gradient_refs` 产出的引用元素结构，并理解 `href` 与 `xlink:href` 的双写。

**操作步骤**：

1. 用 4.1.4 的 Typst 示例编译（两个不同尺寸的矩形用**同一个**渐变对象 `g`）：

   ```typst
   #let g = gradient.linear(red, blue)
   #rect(width: 60pt, height: 40pt, fill: g)
   #rect(width: 120pt, height: 80pt, fill: g)
   ```

2. 在产物 SVG 中搜索 `linearGradient`。

**需要观察的现象**：

- 应当只有 **1 个**含 `<stop>` 子元素的「源」`<linearGradient id="f…">`（两个矩形宽高比都是 1.5，源键 `(g, 1.5)` 命中去重）。
- 应当有 **2 个**近乎空壳的「引用」`<linearGradient id="r…" gradientTransform="…" href="#f…" xlink:href="#f…">`（两个矩形尺寸不同 → `ts` 不同 → 两个不同引用）。
- 使用处的两个矩形分别是 `fill="url(#r…第一个)"` 与 `fill="url(#r…第二个)"`。

**预期结果**：你亲眼看到「1 个源 + 2 个引用」的结构，印证 4.2.1 的体积公式——若改用朴素复制，本该出现 2 份完整定义。

> 若未装 CLI：直接对照 [src/paint.rs:318-338](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L318-L338) 与 [src/paint.rs:117-284](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L117-L284)，推演「源无 `gradientTransform`、引用有 `gradientTransform` 且 `href` 指向源」的对照关系即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么引用元素要同时写 `href` 和 `xlink:href`？

> **答案**：`href` 是 SVG 2 的简写，`xlink:href` 是 SVG 1.1 的旧写法。同时写两者可兼容只认其中一种的渲染器（浏览器多认 `href`，部分老旧工具只认 `xlink:href`）。

**练习 2**：`write_gradient_refs` 里圆锥渐变（`GradientKind::Conic`）为什么用 `("pattern", "patternTransform")` 而不是 `("linearGradient", "gradientTransform")`？

> **答案**：SVG 原生不支持圆锥渐变，typst-svg 用 `<pattern>` + 360 段扇形来近似（u5-l4）。所以圆锥「源」本身就是一个 `pattern`，其引用自然也用 `pattern`/`patternTransform`，以保持元素类型一致、使 `href` 代换生效。

**练习 3**：`SvgIdRef(id)` 输出 `#f…`，而 `SvgUrl(id)` 输出 `url(#f…)`。它们分别用在什么属性上？为什么不能统一？

> **答案**：`SvgUrl` 用于 `fill`/`stroke` 属性（SVG 规定这两者的值用 `url(#id)` 引用 paint server）；`SvgIdRef` 用于 `href`/`xlink:href` 属性（这两者的值是「直接的目标 ID」，即 `#id`）。两者分属不同 SVG 属性语法，不能互换。

## 5. 综合实践

把本讲三条主线（入口分发 → 两层去重 → 引用写出）串起来，手工模拟一次完整的「同渐变、两尺寸」导出。

**任务**：给定两个矩形都用同一个 `RelativeTo::Self_` 的线性渐变 `g` 填充——矩形 A 为 60pt × 40pt、矩形 B 为 120pt × 80pt，二者均位于原点。推演 typst-svg 的完整输出结构。

**步骤**：

1. **算 `aspect_ratio` 与 `ts`**（参考 [src/shape.rs:51-125](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L51-L125)）：
   - A：`aspect_ratio = 60/40 = 1.5`；`ts_A = scale(60,40) . post_concat(translate(0,0))`，非单位。
   - B：`aspect_ratio = 120/80 = 1.5`；`ts_B = scale(120,80)`，非单位，且与 `ts_A` 不同。
2. **矩形 A 的 `push_gradient`**（[src/paint.rs:66-85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L66-L85)）：
   - 第一层：`gradients` 表里没有 `(g, 1.5)`，插入 → 得源 ID `f…α`。
   - `ts_A` 非单位 → 第二层：`gradient_refs` 插入 `(f…α, ts_A)` → 得引用 ID `r…β`。
   - `write_fill` 写出 `fill="url(#r…β)"`。
3. **矩形 B 的 `push_gradient`**：
   - 第一层：`(g, 1.5)` **命中** → 复用源 ID `f…α`（关键：源只存这一份）。
   - `ts_B` 非单位且与 `ts_A` 不同 → 第二层：插入新的 `(f…α, ts_B)` → 得新引用 ID `r…γ`。
   - `write_fill` 写出 `fill="url(#r…γ)"`。
4. **`finalize`**（[src/lib.rs:411-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419)）：
   - `write_gradients` 写出 **1 个**源 `<linearGradient id="f…α" …><stop/>…</linearGradient>`（无 `gradientTransform`）。
   - `write_gradient_refs` 写出 **2 个**空壳：`<linearGradient id="r…β" gradientTransform="scale(60,40)" href="#f…α" xlink:href="#f…α"/>` 与 `<linearGradient id="r…γ" gradientTransform="scale(120,80)" href="#f…α" xlink:href="#f…α"/>`。

**验证**：用 typst CLI 编译上述文档，grep `linearGradient`，确认「1 源 + 2 引用」、引用的 `href` 都指向同一个源 ID。（具体哈希值不可预测，以 `f`/`r` 前缀与计数为准；若本地无法编译，标注「待本地验证」并以上述结构推演为结论。）

**反思**：若改用朴素复制，两个矩形会各自带一份完整的 stops 定义——本讲的模型用 \(S + 2R\) 取代了 \(2S\)，复用越多越划算。

## 6. 本讲小结

- `write_fill` / `write_stroke` 是绘制系统的前台，按 `Paint` 三变体（`Solid`/`Gradient`/`Tiling`）分发：纯色直接写字符串，渐变/平铺走 `push_*` 拿 `DedupId` 并以 `SvgUrl(id)` 写成 `url(#…)`。
- 核心是「**源 + 引用**」两层去重：源不带变换、键不含 `ts`，故同一渐变/平铺无论用多少次只存一份；引用只记「源 ID + 变换」，用 `href` 指回源。
- `ts.is_identity()` 是快速路径：成立时直接返回源 ID（`f`/`t`），否则返回引用 ID（`r`/`p`）；现实里因形状包围盒极少恰好 1pt × 1pt，绝大多数走引用层。
- `write_gradient_refs` / `write_tiling_refs` 在 `finalize` 阶段把每个引用写成空壳元素，仅含 `gradientTransform`/`patternTransform`、`id`、`href` + `xlink:href`；`GradientKind` 决定元素名与变换属性名。
- 渲染期「登记 + 写引用属性」、`finalize` 期「集中写定义」的时间差，是两层去重得以成立的前提；体积收益为 \(\Theta(S + N\cdot R)\) 对 \(\Theta(N\cdot S)\)。
- `SvgUrl`（`url(#…)`，用于 fill/stroke）与 `SvgIdRef`（`#…`，用于 href）是同一 `DedupId` 在不同 SVG 属性语法下的两种适配。

## 7. 下一步学习建议

本讲把「入口 + 去重骨架」讲清楚了，但刻意避开了两块细节：

- **源定义到底长什么样**？线性渐变的角度如何经宽高比修正变成 `x1/y1/x2/y2`，径向渐变的 `cx/cy/r/fx/fy` 如何映射，`anti_alias` 中间停靠点 workaround 解决什么——这是下一讲 **u5-l3《线性与径向渐变实现》** 的主题，建议紧接着读 `write_gradients` 的 Linear/Radial 分支。
- **圆锥渐变与平铺的 specialties**：圆锥为何用 360 段扇形 `<pattern>` 近似（**u5-l4**），平铺的「渲染两次」与 `patternTransform` 被 `href` 覆盖的微妙处理（**u5-l5**）。
- 想深入了解去重容器本身（`hash128` 键、`DedupId` 的大写十六进制编码、为何不直接存 key），请移步 **u6-l3《去重机制 Deduplicator 与 ID 编码》**。

建议按 u5-l3 → u5-l4 → u5-l5 的顺序读完渐变/平铺细节后，再跳到 u6-l3 看底层容器。
