# 平铺 Tiling

## 1. 本讲目标

本讲是绘制系统（第 5 单元）的收尾篇，专门拆解 `Paint::Tiling`——即「重复填充图案」——是如何被翻译成 SVG 的。

学完本讲，你应当能够：

1. 说清楚一个 Typst `Tiling`（重复图案）是如何映射成 SVG `<pattern>` 元素的，以及 `patternUnits` / `patternTransform` / `viewBox` 各属性的含义。
2. 解释 `push_tiling` 为什么要把图案 frame「渲染两次」：第一次用于分配资源并计算去重键，第二次才真正写入输出。
3. 解释为什么 `push_tiling` 的去重键不是 `Tiling` 结构体本身，而是「渲染结果字符串 + 尺寸 + 偏移 + 角度」。
4. 理解 `write_tiling_refs` 里 `href` 引用为何会覆盖被引用 pattern 的 `patternTransform`，以及代码如何用 `ts.pre_concat(tiling.transform())` 来规避这个 SVG 语义陷阱。

本讲承接 u5-l2（填充/描边入口与「源 + 引用」两层去重模型）与 u2-l1（`SVGRenderer` 与 7 个 `Deduplicator`）。如果你还没读过 u5-l2，请先读：本讲反复使用「源定义不带变换、引用只带变换」这套模型，区别在于平铺有一个渐变没有的额外陷阱。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

### 2.1 什么是「平铺 / 重复图案」

如果你给一个矩形填充「平铺」，就像给墙面贴瓷砖：定义一小块图案（一块「瓷砖」），然后让它在整个填充区域内沿网格无限重复，铺满为止。在 Typst 里这样写（这是 typst-library 里 `Tiling` 的官方示例）：

```typ
#let pat = tiling(size: (30pt, 30pt), {
  place(line(start: (0%, 0%), end: (100%, 100%)))
  place(line(start: (0%, 100%), end: (100%, 0%)))
})

#rect(fill: pat, width: 100%, height: 60pt, stroke: 1pt)
```

`size` 是一块瓷砖的大小，花括号里的内容是瓷砖上画的东西（两条对角线）。最终 `rect` 会被这块 30pt×30pt 的瓷砖铺满。

### 2.2 SVG 怎么表示「重复图案」：`<pattern>`

SVG 原生支持平铺，元素名叫 [`<pattern>`](https://developer.mozilla.org/en-US/docs/Web/SVG/Element/pattern)。一个最小例子：

```xml
<defs>
  <pattern id="pat" width="30" height="30" patternUnits="userSpaceOnUse"
           patternTransform="rotate(45)">
    <!-- 瓷砖内容：两条对角线 -->
    <line .../>
    <line .../>
  </pattern>
</defs>
<rect fill="url(#pat)" .../>
```

关键属性：

| 属性 | 含义 |
|------|------|
| `width` / `height` | 一块瓷砖的尺寸 |
| `patternUnits="userSpaceOnUse"` | 瓷砖尺寸用「用户坐标」（pt）衡量，且图案定位在用户坐标系中（而非跟随每个被填充物体的包围盒） |
| `patternTransform` | 对整片瓷砖网格做整体变换（旋转、平移整面墙） |
| `viewBox` | 瓷砖内部使用的坐标系 |

被填充的物体只要写 `fill="url(#pat)"`，浏览器/PDF 渲染器就会自动把这块瓷砖无限重复铺满。typst-svg 的工作，就是把 Typst 的 `Tiling` 翻译成这样一个 `<pattern>`。

### 2.3 回顾：两层去重模型

u5-l2 讲过：为了压缩体积，渐变和平铺都采用「**源（source）+ 引用（ref）**」两层去重。

- **源定义**不带「使用变换」，所以同一个图案无论被多少个形状用到，只存一份。
- **引用**只记录「源 ID + 这次使用的变换」，是一个近乎空壳的元素，靠 `href` 指回源定义。

每个不同形状的包围盒几乎都不同，所以变换几乎总不同，现实中多数情况走「引用」层。本讲会看到：渐变的引用可以只写自己的变换，**但平铺的引用必须把源的变换「拼」进来**——这是本讲最关键的差异。

---

## 3. 本讲源码地图

本讲几乎全部位于 [src/paint.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs)，少量涉及 `lib.rs` 的编排与 `typst-library` 的 `Tiling` 定义。

| 文件 | 关键点 | 作用 |
|------|--------|------|
| `src/paint.rs` | `render_tiling_frame` | 把 frame 渲染成一个**独立的 SVG 字符串**（第一次渲染） |
| `src/paint.rs` | `push_tiling` | 平铺去重入口：决定走「源」还是「源+引用」，并算出 `patternTransform` |
| `src/paint.rs` | `write_tilings` | `finalize` 阶段写出**源** `<pattern>`（第二次渲染） |
| `src/paint.rs` | `write_tiling_refs` | `finalize` 阶段写出**引用** `<pattern>` 空壳 |
| `src/paint.rs` | `TilingRef` | 引用结构体：源 ID + 变换矩阵 |
| `src/paint.rs` | `correct_tiling_pos` | 把单位方形 Ratio 坐标映射到 pattern 绘图坐标 |
| `src/lib.rs` | `finalize` | 决定 `<defs>` 各类定义的写出顺序（关键！） |
| `crates/typst-library/.../tiling.rs` | `Tiling::transform` | 平铺自身变换 = 平移 ∘ 旋转 |

> 阅读顺序建议：先看 `write_fill` 的 `Tiling` 分支（入口），再看 `push_tiling`（去重决策），最后看 `write_tilings` / `write_tiling_refs`（落地写出）。

---

## 4. 核心概念与源码讲解

### 4.1 平铺如何映射到 `<pattern>`：入口与坐标助手

#### 4.1.1 概念说明

`Paint` 有三种变体（`Solid` / `Gradient` / `Tiling`），填充入口 `write_fill` 用一个 `match` 把它们分发出去。平铺这一支的目标很简单：拿到一个能填进 `fill="url(#...)"` 的 ID。和渐变完全对称——调用 `push_tiling` 拿到 `DedupId`，再用 `SvgUrl` 适配器包成 `url(#id)`。

本模块还顺带讲一个共享的小助手 `correct_tiling_pos`：它把「单位方形里的 Ratio 坐标」换算成「pattern 绘图坐标」。它主要被圆锥渐变（把圆锥也实现成一个 `<pattern>`，见 u5-l4）的扇形路径构建所使用，但概念上属于「pattern 空间坐标映射」，所以放在这里一并交代。

#### 4.1.2 核心流程

```
write_fill(svg, Paint::Tiling(tiling), ..., ts)
   │
   ├─ id = self.push_tiling(tiling, ts)      // 去重，拿到 DedupId
   └─ svg.attr("fill", SvgUrl(id))           // 输出 fill="url(#tXXX)"
```

`SvgUrl(id)` 是 [src/write.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs) 里的 newtype 适配器，它把 `DedupId` 格式化成 `url(#...)`。`id` 落到纸面上是一个由「类型字符 + 大写十六进制哈希」组成的字符串（命名空间 `'t'` 表示源平铺，`'p'` 表示平铺引用，详见 u6-l3）。

`correct_tiling_pos(x, y)` 的映射是一条简单的仿射：

\[ r \;\mapsto\; 0.5 \times (r + 0.5)\;\text{pt} \]

即先把 Ratio 坐标整体平移 +0.5，再缩放 0.5 倍（单位 pt）。它把 `[0,1]` 区间的 Ratio「贴」进 pattern 的绘图区域。

#### 4.1.3 源码精读

填充入口的三路分发，平铺这一支在最后：

[src/paint.rs:L40-L52](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L40-L52) —— `write_fill` 按 `Paint` 三变体分发：纯色直接写字符串；渐变调 `push_gradient`；平铺调 `push_tiling`，两者都用 `SvgUrl(id)` 包成 `url(#id)` 写进 `fill`。

坐标助手 `correct_tiling_pos`：

[src/paint.rs:L519-L522](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L519-L522) —— 把单位方形 Ratio 坐标映射到 pattern 绘图坐标：`Point::new(Abs::pt(x+0.5), Abs::pt(y+0.5))` 再乘 0.5。它的实际调用点在圆锥渐变的扇形路径构建处（`write_gradients` 的 `Conic` 分支），例如 [src/paint.rs:L201](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L201) 把圆锥中心、[src/paint.rs:L203-L206](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L203-L206) 把扇形端点换算到 pattern 空间。本讲的平铺 `<pattern>` 写出路径并不直接调用它，但理解它有助于看懂「为何圆锥渐变要复用 pattern 机制」。

`SvgUrl` / `SvgIdRef` / `SvgTransform` 三个适配器都在 [src/write.rs:L292-L311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs#L292-L311)：`SvgUrl` 输出 `url(#id)`，`SvgIdRef` 输出 `#id`（给 `href` 用），`SvgTransform` 挑最短的 `scale`/`translate`/`matrix` 写法。

#### 4.1.4 代码实践

**目标**：亲手把一个平铺编译成 SVG，确认它确实变成 `<pattern>`。

1. 新建 `tiling.typ`，写入本讲 2.1 节那段官方示例。
2. 编译为 SVG（typst-svg 正是 CLI 输出 SVG 时调用的 crate）：

   ```bash
   typst compile tiling.typ tiling.svg
   ```

3. 在 `tiling.svg` 里搜索 `<pattern`。

**需要观察的现象**：你会看到一个 `<defs>` 块，里面有形如 `<pattern id="tXXXX" width="30" height="30" patternUnits="userSpaceOnUse" ...>` 的元素，内部是两条对角线（`<path>` 或 `<line>`）；而那个 `<rect>` 的 `fill` 属性值是 `url(#tXXXX)`。

**预期结果**：平铺被去重存进 `<defs>` 的 `<pattern>`，矩形通过 `fill="url(#...)"` 引用它。若你的 typst 版本输出格式与本讲描述有出入，以本地实际输出为准（**待本地验证**具体缩进与属性顺序）。

#### 4.1.5 小练习与答案

**练习 1**：为什么平铺和渐变都写成 `url(#id)` 而不是把图案内容内联到每个形状里？

**答案**：内联会让同一个图案在被多个形状使用时重复写出 N 份，体积爆炸；写成 `url(#id)` + 集中定义，同一份图案只存一次，形状只带一个短引用。

**练习 2**：`correct_tiling_pos(0.5, 0.5)` 的结果是多少？它对应 pattern 里的哪个位置？

**答案**：\(0.5 \times (0.5 + 0.5) = 0.5\)，即 `(0.5pt, 0.5pt)`——一个 1pt×1pt 区域的正中心。这也是圆锥渐变中心点（默认 `center=(0.5,0.5)`）落在 pattern 里的位置。

---

### 4.2 `push_tiling`：为什么要「渲染两次」，以及为何用字符串做去重键

#### 4.2.1 概念说明

`push_tiling` 是平铺去重的中枢，做两件事：决定这次使用对应「源」还是「源+引用」，并返回对应的 `DedupId`。但它有一个看似古怪的设计——**在去重之前，先把图案 frame 渲染成一段字符串**。这引出本讲最常被问的两个问题：

1. **为什么要渲染两次？**（源码里那条 `Unfortunately due to a limitation of xmlwriter...` 注释）
2. **为什么去重键是「渲染结果字符串」而不是 `Tiling` 结构体本身？**

答案分别藏在「资源预分配」和「`Tiling` 含不稳定的 `Location`」这两点上。

#### 4.2.2 核心流程

```
push_tiling(tiling, ts)
  │
  │  ① 第一次渲染（扔进一个临时 XmlWriter）
  ├─ rendered = self.render_tiling_frame(State::new(tiling_size), tiling.frame())
  │      副作用：把图案内部用到的字形/渐变等资源登记进 self 的各 Deduplicator
  │
  │  ② 用 (尺寸, 偏移, 角度, rendered 字符串) 作为去重键
  ├─ tiling_id = self.tilings.insert_with(key, || tiling.clone())
  │
  │  ③ 若本次使用变换是单位矩阵 → 直接返回源 ID（快速路径）
  ├─ if ts.is_identity() { return tiling_id; }
  │
  │  ④ 否则造一个引用，变换 = ts.pre_concat(tiling.transform())
  └─ self.tiling_refs.insert_with(TilingRef{id: tiling_id, transform}, || ...)
```

**为什么必须第一次渲染（资源预分配）**：看 `finalize` 的写出顺序——

[src/lib.rs:L411-L419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419) —— `finalize` 先写字形定义（`write_glyph_defs`），**最后**才写平铺（`write_tilings`，第 6 步）。

也就是说：**字形的 `<symbol>` 定义在平铺之前就被写出了**。但平铺图案内部可能包含文字（瓷砖上写了字），这些文字依赖字形资源。如果把这些字形的登记推迟到 `write_tilings` 里第二次渲染时才发生，那它们就「来不及」被 `write_glyph_defs` 写出——最终 `<use href="#gXXX">` 会指向一个不存在的 `<symbol>`。

因此第一次渲染（发生在正常渲染阶段，远早于 `finalize`）必须先把图案 frame 跑一遍，**让它在 `self.glyphs` 等表里预登记好所有需要的资源**。等到 `finalize` 写字形定义时，这些字形已经在表里，会被正确写出。

**为什么去重键要用 `rendered` 字符串**：注释写得很直接——「the `Tiling` itself includes `Location`s which aren't stable」。`Tiling` 内部含有 `Locator`/`Location` 这类与语法节点位置绑定的信息，它们在不同编译之间不稳定，不能直接拿来哈希做去重键。真正能代表「这块平铺画出来是什么样」的稳定身份，就是**它实际渲染出的 SVG 字符串**，再配上尺寸、偏移、角度这几个标量。

#### 4.2.3 源码精读

[src/paint.rs:L23-L29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L23-L29) —— `render_tiling_frame`：新建一个**独立的** `XmlWriter`，套一个 `<g>`，调用 `self.render_frame`（同一个渲染器！），最后 `end_document()` 返回字符串。注意它用的是 `self`，所以渲染过程会把字形/渐变登记进渲染器的全局去重表——这正是「分配资源」的含义。

[src/paint.rs:L87-L114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L87-L114) —— `push_tiling` 全文。重点看三段注释：

- [L91-L93](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L91-L93)：「render twice」注释——第一次分配资源、第二次真正渲染。
- [L96-L101](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L96-L101)：去重键是 `(tiling_size, tiling_offset, tiling_angle, rendered.as_str())`，并附注 `Tiling` 含不稳定的 `Location`。
- [L103-L105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L103-L105)：`ts.is_identity()` 快速路径，直接返回源 ID（命名空间 `'t'`）。

`tilings` 字段的定义与去重语义在 [src/lib.rs:L213-L218](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L213-L218)（源平铺，命名空间 `'t'`）和 [src/lib.rs:L219-L224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L219-L224)（平铺引用，命名空间 `'p'`）。`insert_with` 的「按 key 哈希、惰性构造值」语义见 [src/lib.rs:L493-L512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L493-L512)：若哈希已存在则不调用闭包，直接返回既有 ID——这正是第二次渲染时不会重复分配资源的原因。

#### 4.2.4 代码实践（源码阅读型）

**目标**：用阅读 + 推理验证「第一次渲染为预分配资源」这一论断。

1. 阅读 [src/lib.rs:L411-L419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419) 的 `finalize`，记下 7 个 `write_*` 的顺序，确认 `write_glyph_defs` 在 `write_tilings` **之前**。
2. 阅读本讲 4.3 节的 `write_tilings`，确认它内部会调用 `self.render_frame(...)`（第二次渲染），而 `render_frame` 会调用 `render_text` → `render_glyph` → `self.glyphs.insert_with(...)`。
3. **思想实验**：假设删掉 `push_tiling` 里那行 `let rendered = self.render_tiling_frame(...)`（即不做第一次渲染），只在 `write_tilings` 里渲染一次。问：当平铺瓷砖上写了字时，这些字对应的字形 `<symbol>` 还会被写出吗？

**需要观察的现象 / 预期结果**：不会。因为字形的登记发生在 `write_tilings`（第 6 步），而 `write_glyph_defs`（第 1 步）此时已经跑完。字形表里没有这些字形，`<symbol>` 就不会被写出，导致 `<use href="#gXXX">` 悬空。第一次渲染正是为了把这些字形提前登记进表。这印证了注释里「once to allocate all of the resources that it needs」。

> 说明：这是一个「阅读 + 推理」型实践，不需要真的去改源码编译（那样会破坏不变量）。理解调用顺序与 `insert_with` 的惰性语义即可。

#### 4.2.5 小练习与答案

**练习 1**：既然第一次渲染已经产出了完整的 SVG 字符串 `rendered`，为什么 `write_tilings` 还要第二次渲染、而不是直接把 `rendered` 字符串粘进 `<pattern>`？

**答案**：两个原因。（1）`rendered` 来自一个临时的、独立的 `XmlWriter`，是用 `SvgElem` 抽象流式写出的产物；主输出的 `SvgElem` 没有「粘贴整段原始字符串」的原语，流式写法只能再渲染一遍来产生 `<pattern>` 的子节点。（2）更重要的是，第一次渲染的真正目的是**预分配资源**（把字形/渐变登记进全局表），`rendered` 字符串本身只是顺带拿来做去重键；真正的图案内容必须作为 `<pattern>` 的子元素流式写入主输出。

**练习 2**：去重键里为什么除了 `rendered` 还要带 `tiling_size` / `tiling_offset` / `tiling_angle`？光用 `rendered` 不够吗？

**答案**：`rendered` 是图案 frame 的内容，但平铺的**定位**（偏移、角度）和**瓷砖尺寸**决定了它铺出来的整体效果。两个内容相同但旋转角度不同的平铺，`rendered` 字符串可能一样，但视觉效果不同，应当被视作不同的源定义（它们的 `patternTransform` / `width` / `height` 不同）。带上这些标量能更精确地刻画身份，也避免把视觉上不同的平铺错误合并。（注意：实际上 `tiling.transform()` 是在源 `<pattern>` 的 `patternTransform` 上写出的，见 4.3。）

---

### 4.3 源 / 引用两层去重的落地：`write_tilings` / `write_tiling_refs` 与 `patternTransform` 陷阱

#### 4.3.1 概念说明

`push_tiling` 只是把平铺和引用登记进两张表；真正把它们写成 SVG 元素的是 `finalize` 阶段的 `write_tilings`（写源）和 `write_tiling_refs`（写引用）。

这里藏着一个 SVG 语义陷阱，也是本讲的另一核心：**当一个 `<pattern>` 用 `href` 引用另一个 `<pattern>` 时，引用方的 `patternTransform` 会整体覆盖被引用方的 `patternTransform`，而不是叠加。** 代码里 `ts.pre_concat(tiling.transform())` 这一行，正是为了规避这个陷阱。

要理解它，先看 `Tiling` 自身的变换是怎么来的。[crates/typst-library/src/visualize/tiling.rs:L374-L377](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/visualize/tiling.rs#L374-L377)：

```rust
pub fn transform(&self) -> Transform {
    Transform::translate(self.offset().x, self.offset().y)
        .pre_concat(Transform::rotate(self.angle()))
}
```

回忆 u2-l1/`Transform::pre_concat` 的语义：`A.pre_concat(B)` 等于矩阵乘积 \(A \cdot B\)，对点而言 **B 先作用、A 后作用**。所以平铺自身变换是：

\[ T_{\text{tiling}} = T_{\text{offset}} \cdot R_{\text{angle}} \]

即「先绕原点旋转 angle，再平移 offset」。这块变换会被写进**源** `<pattern>` 的 `patternTransform`。

#### 4.3.2 核心流程

**写源** `write_tilings`：

```
对每个源平铺 (id, tiling)：
  size = tiling.size() + tiling.spacing()
  <pattern id  width=size.x  height=size.y
           patternUnits="userSpaceOnUse"
           patternTransform = tiling.transform()        ← 源自带自身变换
           viewBox="0 0 size.x size.y">
      第二次渲染：self.render_frame(pattern, State::new(size), tiling.frame())
  </pattern>
```

**写引用** `write_tiling_refs`：

```
对每个引用 (id, tiling_ref)：
  <pattern id
           patternTransform = tiling_ref.transform      ← ts · T_tiling（拼上了源的变换）
           href      = "#源ID"
           xlink:href = "#源ID" />
```

**陷阱与规避**：SVG 的 pattern 继承语义规定，引用方会继承被引用方的全部属性，**但引用方自己显式声明的属性会覆盖被继承的值**。所以引用方一旦写了 `patternTransform`，源的那份 `patternTransform = T_tiling` 就被丢弃了。若引用方只写 `patternTransform = ts`，瓷砖就丢了「旋转 + 偏移」，铺出来是错的。

正确做法：让引用方的 `patternTransform` 同时包含「源自身变换」和「本次使用变换」。我们要的最终有效变换是先做平铺自身变换、再做使用变换：

\[ T_{\text{有效}} = T_s \cdot T_{\text{tiling}} \]

对应代码正是：

```rust
let transform = ts.pre_concat(tiling.transform());   // = T_s · T_tiling
```

这样就补偿了被覆盖掉的源变换。

**与渐变的对比**：渐变的源定义画在单位方形里、**不带** `gradientTransform`（见 u5-l3），所以渐变引用只需写 `gradientTransform = ts`，无需拼接。平铺则因为源 `<pattern>` **必须**带 `patternTransform = tiling.transform()`（瓷砖需要旋转/偏移），引用才不得不拼接。这是两层模型里「渐变」与「平铺」唯一的实现差异。

#### 4.3.3 源码精读

[src/paint.rs:L341-L367](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L341-L367) —— `write_tilings` 写源 `<pattern>`。注意几处：

- [L347-L349](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L347-L349)：先把 `self.tilings.iter()` 收集成 `Vec<_>`（`.clone()`，但 `Tiling` 是 `Arc`，克隆只是引用计数 +1，很廉价）。**为什么要先收集？** 因为循环体内要调用 `self.render_frame(...)`（可变借用 `self`），而迭代器持有了对 `self.tilings` 的不可变借用——先收集进 Vec 释放不可变借用，才能通过借用检查。这是典型的 Rust 借用检查规避写法。
- [L352-L360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L352-L360)：写出 `width`/`height`/`patternUnits="userSpaceOnUse"`/`patternTransform=tiling.transform()`/`viewBox`。
- [L361-L365](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L361-L365)：**第二次渲染**——`self.render_frame(pattern, &state, tiling.frame())`，把瓷砖内容作为 `<pattern>` 的子元素流式写出。

[src/paint.rs:L370-L385](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L370-L385) —— `write_tiling_refs` 写引用空壳：只有 `patternTransform`、`id`、`href`、`xlink:href` 四个属性，没有子元素。`xlink:href` 与 `href` 同时写是为了兼容新旧 SVG 标准（u5-l2 讲过）。

[src/paint.rs:L107-L113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L107-L113) —— 规避陷阱的关键代码：注释明确解释了「`href` 会覆盖被引用 pattern 的 `patternTransform`，因此源已有的 `patternTransform` 必须被拼接进来」，随后 `let transform = ts.pre_concat(tiling.transform());`。

[src/paint.rs:L388-L398](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L388-L398) —— `TilingRef` 结构体：只有 `id: DedupId`（指向源）和 `transform: Transform`（已拼接好的 \(T_s \cdot T_{\text{tiling}}\)）。它派生了 `Copy/Clone/Hash`，可直接作为 `Deduplicator` 的键与值。

#### 4.3.4 代码实践

**目标**：在真实 SVG 输出里看到「源 pattern 带 `patternTransform`、引用 pattern 带 `href` 且 `patternTransform` 已拼接」。

1. 写一个让平铺旋转、并用在多个不同尺寸形状里的 `.typ` 文件：

   ```typ
   #let pat = tiling(
     size: (20pt, 20pt),
     angle: 30deg,
     place(dx: 5pt, dy: 5pt, circle(radius: 5pt, fill: black)),
   )

   #rect(width: 60pt, height: 40pt, fill: pat)
   #box(width: 30pt, height: 30pt, fill: pat)
   ```

2. 编译：`typst compile demo.typ demo.svg`。
3. 打开 `demo.svg`，分别找命名空间以 `t`（源）和 `p`（引用）开头的 `<pattern>`。

**需要观察的现象**：

- 应有一个 `id="t..."` 的源 `<pattern>`，带 `patternTransform`（对应 30° 旋转），内部有那个小圆。
- 应有 `id="p..."` 的引用 `<pattern>`，**没有子元素**，只有 `patternTransform`（一个比源更复杂的矩阵，因为它把使用变换 `ts` 和源的旋转拼在了一起）和 `href="#t..."`。
- 两个形状的 `fill` 分别指向某个 `url(#p...)` 或 `url(#t...)`。

**预期结果**：引用 pattern 的 `patternTransform` ≠ 单纯的使用变换，而是「使用变换 ∘ 源旋转」的复合矩阵；这正是 `pre_concat` 的效果。具体属性顺序与数值以本地输出为准（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `push_tiling` 里的 `let transform = ts.pre_concat(tiling.transform());` 改成 `let transform = ts;`，平铺的旋转效果会怎样？

**答案**：引用 pattern 的 `patternTransform` 会覆盖源的 `patternTransform`，于是源的 `angle` 旋转丢失，瓷砖不再旋转（或只体现使用变换 `ts` 的部分），视觉上与源定义不一致。这印证了拼接源变换的必要性。

**练习 2**：为什么 `write_tilings` 要先 `.collect::<Vec<_>>()` 再循环，而不能直接 `for (id, tiling) in self.tilings.iter()`？

**答案**：循环体内 `self.render_frame(...)` 需要 `&mut self`，而 `self.tilings.iter()` 持有 `&self.tilings`（属于 `&self`）的不可变借用，两者冲突，编译不过。先收集成 Vec（克隆出 `Tiling`，因其是 `Arc` 所以廉价）释放不可变借用，即可在循环体内可变借用 `self`。

**练习 3**：源平铺用 `'t'` 命名空间，平铺引用用 `'p'`。为什么不共用一个命名空间？

**答案**：源和引用是不同性质的对象（源有子元素和自身变换；引用是空壳、带复合变换），它们的 `DedupId` 哈希来自不同的键。用不同 kind 字符把它们分到不同 ID 命名空间，可以保证一个源 ID 和一个引用 ID 绝不会撞号，`href` 引用时不会歧义。这也是 `Deduplicator::new('t')` / `Deduplicator::new('p')` 分别构造的原因（见 [src/lib.rs:L280-L281](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L280-L281)）。

---

## 5. 综合实践

把本讲的两条主线串起来，完成下面这个「讲解 + 验证」任务。

### 任务

准备一个瓷砖里带文字、且平铺被旋转、并用在两个不同形状里的文档：

```typ
#let pat = tiling(
  size: (40pt, 40pt),
  angle: 20deg,
  align(center + horizon, text(size: 8pt)["Hi"]),
)

#rect(width: 120pt, height: 60pt, fill: pat)
#circle(radius: 25pt, fill: pat)
```

编译：`typst compile final.typ final.svg`，然后完成两份「说明」：

**说明 A：解释「渲染两次」注释。** 结合源码回答：

1. `push_tiling` 里的第一次渲染（[src/paint.rs:L91-L94](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L91-L94)）做了哪两件事？（提示：产出去重键字符串；把瓷砖里 "Hi" 的字形预登记进 `self.glyphs`。）
2. 为什么这第一次渲染必须在 `finalize` 之前发生？参考 [src/lib.rs:L411-L419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L411-L419) 的写出顺序（`write_glyph_defs` 在 `write_tilings` 之前）。
3. 在 `final.svg` 里找到 "Hi" 字形对应的 `<symbol>` 定义（命名空间 `g`），确认它确实被写出了——这正是第一次渲染预分配资源的功劳。

**说明 B：解释 `patternTransform` 覆盖与 `pre_concat` 规避。** 结合源码回答：

1. 源 `<pattern>`（`t...`）的 `patternTransform` 是什么？（应是 `tiling.transform()`，即 20° 旋转。）
2. 引用 `<pattern>`（`p...`）的 `patternTransform` 为什么不是单纯的使用变换 `ts`？引用 [src/paint.rs:L107-L111](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L107-L111) 的注释，说明 SVG pattern 继承语义里的「覆盖」行为。
3. 说明 `ts.pre_concat(tiling.transform())` 在数学上等于 \(T_s \cdot T_{\text{tiling}}\)（使用变换在外、平铺自身变换在内），为何这个顺序是正确的。

### 预期结果

你能用自己的话讲清两件事：（A）第一次渲染 = 分配资源（+ 产出去重键），是为了让瓷砖内部的字形赶在 `write_glyph_defs` 之前登记；（B）`href` 引用会覆盖源的 `patternTransform`，所以引用方必须用 `pre_concat` 把源变换拼进自己的 `patternTransform`，否则瓷砖的旋转/偏移会丢失。SVG 里的实际属性以本地输出为准（**待本地验证**）。

---

## 6. 本讲小结

- 平铺（`Paint::Tiling`）映射为 SVG `<pattern>`：`write_fill` 调 `push_tiling` 拿 `DedupId`，再用 `SvgUrl` 包成 `fill="url(#...)"`。
- `push_tiling` 采用「源 + 引用」两层去重：`ts` 为单位矩阵时直接返回源 ID（命名空间 `'t'`），否则插入一个引用（命名空间 `'p'`）。
- **渲染两次**：第一次（`render_tiling_frame`）在 `finalize` 之前把图案 frame 跑一遍，目的是**预分配资源**（把瓷砖内部的字形/渐变登记进全局去重表）并**产出字符串去重键**；第二次（`write_tilings` 内的 `render_frame`）才把瓷砖内容真正写进主输出的 `<pattern>`。
- 去重键用「渲染结果字符串 + 尺寸 + 偏移 + 角度」，而非 `Tiling` 结构体本身，因为 `Tiling` 含不稳定的 `Location`，不能直接哈希。
- **`patternTransform` 覆盖陷阱**：SVG 中带 `href` 的引用 `<pattern>` 会用自身的 `patternTransform` 整体覆盖被引用方的；代码用 `ts.pre_concat(tiling.transform())`（即 \(T_s \cdot T_{\text{tiling}}\)）把源变换拼进引用，规避这个陷阱。这是平铺与渐变（源不带变换、引用只写 `ts`）的关键实现差异。
- `correct_tiling_pos` 是把单位方形 Ratio 坐标映射到 pattern 绘图坐标的小助手（\(r \mapsto 0.5(r+0.5)\) pt），主要被圆锥渐变（本身也实现为 `<pattern>`）的扇形路径所用。

---

## 7. 下一步学习建议

- 本讲是第 5 单元（绘制系统）的最后一篇。若想彻底看清「源 + 引用」两层去重的底层，请接着学 **u6-l3 去重机制 Deduplicator 与 ID 编码**，那里讲解 `hash128`、`IndexMap`、`DedupId` 的「kind 字符 + 大写十六进制」编码。
- 想了解平铺图案内部那些字形的 `<symbol>`+`<use>` 复用细节，可回顾 **u4-l2 字形定义与符号复用**。
- 想看 `finalize` 如何把全部 `<defs>`（字形/裁剪/渐变/引用/子渐变/平铺/平铺引用）按固定顺序串起来，可回顾 **u6-l4 链接、锚点与 HTML/Bundle 集成**。
- 若你对圆锥渐变如何复用 `<pattern>` + `correct_tiling_pos` 感兴趣，可对照阅读 **u5-l4 圆锥渐变 Conic**——本讲的坐标助手在那里被大量使用。
