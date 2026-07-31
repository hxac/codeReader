# SVGRenderer 渲染器与渲染状态

## 1. 本讲目标

第 1 单元我们已经从外部看清了 typst-svg 的四个导出函数、`SvgOptions` 配置，以及单页导出的七步骨架：

```
page_bleed → new → svg_header → State::new → render_page → finalize → end_document
```

本讲我们跨进渲染器的“内部”，把骨架里的三个核心角色拆开看：

- `SVGRenderer` —— 持有 7 个去重器（`Deduplicator`）的“状态容器”。
- `State` —— 贯穿整棵 Frame 树的渲染上下文（当前变换 + 当前尺寸）。
- `render_page` —— 页面级渲染入口，处理背景填充与出血偏移，再把内容交给 `render_frame`。

学完后你应当能够：

1. 说出 `SVGRenderer` 的 7 个 `Deduplicator` 各自缓存哪一类资源、用哪个字符做 ID 前缀。
2. 解释 `State` 的 `pre_concat` / `pre_translate` / `with_size` / `with_transform` 四个方法的语义，尤其是“前乘（pre-concat）”的坐标含义。
3. 读懂 `render_page` 如何用一个矩形 `Shape` 画出页面背景、为什么要这样画，以及它与 `render_frame` 的调用关系。

> 本讲只覆盖**页面级**的入口与状态抽象；`render_frame` / `render_group` 如何遍历 Frame 里的各类元素，是下一讲（u2-l2）的内容。

## 2. 前置知识

### 2.1 Frame 是什么

Typst 排版的最终产物是一棵 `Frame`（帧）树。一个 `Frame` 里装着若干 `(Point, FrameItem)` 二元组：`Point` 是该元素在帧内的局部坐标，`FrameItem` 是具体内容（文本、形状、图像、链接、子帧组……）。typst-svg 的工作就是把这棵树翻译成 SVG。

### 2.2 变换矩阵 Transform

typst-library 提供的 `Transform` 是一个“缩放-倾斜-平移”仿射变换，共 6 个分量：

\[ M = \begin{bmatrix} s_x & k_x & t_x \\ k_y & s_y & t_y \\ 0 & 0 & 1 \end{bmatrix} \]

对一个点 \(p\)（列向量）作用后得到 \(M \cdot p\)。两个变换的组合遵循矩阵乘法：`a.pre_concat(b)` 得到的是 \(M_a \cdot M_b\)，物理含义是“**先**对点作用 `b`，**再**作用 `a`”。也就是说 `pre_concat` 把传入的变换“垫在更局部（更内层）的位置”。这个语义是理解 `State` 的关键，下文会反复用到。

> 源码定义见 [crates/typst-library/src/layout/transform.rs:L242-L251](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/transform.rs#L242-L251)（结构体）与 [crates/typst-library/src/layout/transform.rs:L333-L343](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/transform.rs#L333-L343)（`pre_concat` 实现）。

### 2.3 SVG 的 `<defs>` 与“定义一次、多处引用”

SVG 允许把可复用的资源（字形、裁剪路径、渐变、图案……）放进 `<defs>` 里定义一次，赋予一个 `id`，然后在正文里用 `<use href="#id">` 或 `fill="url(#id)"` 反复引用。typst-svg 把这套机制用到了极致，`SVGRenderer` 里那 7 个去重器，本质上就是在为 `<defs>` 收集“去重后的资源清单”。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它会向外引用两个上游类型：

| 文件 / 位置 | 作用 |
| --- | --- |
| `src/lib.rs` L188-L225 | `SVGRenderer` 结构体定义，含 7 个 `Deduplicator` 字段 |
| `src/lib.rs` L265-L307 | `new` / `with_options` 构造器、`render_page` 方法 |
| `src/lib.rs` L227-L263 | `State` 结构体及其四个方法 |
| `src/lib.rs` L478-L523 | `Deduplicator<T>` 去重容器（本讲只了解它的角色，细节留到 u6-l3） |
| `crates/typst-library/.../transform.rs` | `Transform` 与 `pre_concat` 的定义 |
| `crates/typst-layout/.../document.rs` | `Page` 结构体与 `fill_or_white` 方法 |

回忆 u1-l2 的结论：`SVGRenderer` 这个类型只在 `lib.rs` 里**定义一次**，但它的方法**分散**在 shape/text/paint/image 等多个文件的 `impl SVGRenderer` 块里——“状态集中、行为分散”。本讲关注的 `render_page`、`render_frame`、`render_group`、`finalize` 都属于 `lib.rs` 自己的编排层方法。

## 4. 核心概念与源码讲解

### 4.1 SVGRenderer：状态集中、行为分散的渲染器

#### 4.1.1 概念说明

`SVGRenderer` 是整个 SVG 导出过程的“工作台”。它本身**不直接输出任何字节**（输出由 `SvgElem` + `XmlWriter` 负责，那是 u2-l3 的主题），它只负责两件事：

1. **持有去重后的可复用资源**（字形、裁剪路径、渐变、图案），等 `finalize` 阶段统一写进 `<defs>`。
2. **编排渲染流程**：页面 → 帧 → 各类元素，逐层调用 `render_shape` / `render_text` / `render_image` 等方法。

为什么要集中持有这些资源？因为同一份文档里同一个字形、同一种渐变会被用到成百上千次。如果每次出现都原样写一遍，SVG 文件会膨胀得无法使用。typst-svg 的策略是：渲染正文时只记录“我需要某个字形/渐变”，由去重器分配一个稳定 ID，正文里写引用；最后 `finalize` 把真正去重后的资源定义一次性写进 `<defs>`。这是 typst-svg 体积优化的核心思路。

#### 4.1.2 核心流程

`SVGRenderer` 的生命周期很短，恰好对应一次导出：

```
SVGRenderer::new() / with_options()    ← 创建空工作台，7 个去重器全空
        │
        ▼
render_page / render_frame / ...       ← 边渲染正文，边把资源塞进去重器
        │
        ▼
finalize(self, svg)                    ← 把 7 个去重器的内容依次写进 <defs>
        │
        ▼
（renderer 被 consume，丢弃）
```

注意 `finalize` 拿的是 `self`（按值），所以渲染器用完即弃，**一个 `SVGRenderer` 只服务一次导出**。这也意味着每次调用 `svg` / `svg_in_html` 等公开函数都会新建一个渲染器（见 u1-l3 的七步骨架里 `SVGRenderer::new()` 那一步）。

#### 4.1.3 源码精读

先看结构体本身：

[src/lib.rs:L188-L225](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L188-L225) —— `SVGRenderer<'a>` 的全部字段。除 `link_resolver` 之外，剩下 **7 个全是 `Deduplicator`**：

```rust
struct SVGRenderer<'a> {
    link_resolver: Option<Tracked<'a, LateLinkResolver<'a>>>,
    glyphs: Deduplicator<Option<RenderedGlyph>>,           // 'g'
    clip_paths: Deduplicator<EcoString>,                   // 'c'
    gradients: Deduplicator<(Gradient, Ratio)>,            // 'f'
    gradient_refs: Deduplicator<GradientRef>,              // 'r'
    conic_subgradients: Deduplicator<SVGSubGradient>,      // 's'
    tilings: Deduplicator<Tiling>,                         // 't'
    tiling_refs: Deduplicator<TilingRef>,                  // 'p'
}
```

每个字段缓存一类资源，归纳如下：

| 字段 | ID 前缀字符 | 缓存内容 | 用途 |
| --- | --- | --- | --- |
| `glyphs` | `g` | `Option<RenderedGlyph>` | 去重后的字形（轮廓或图像字形） |
| `clip_paths` | `c` | `EcoString` | 裁剪路径的 path 数据字符串 |
| `gradients` | `f` | `(Gradient, Ratio)` | **不带变换**的“源”渐变；`Ratio` 是用于修正角度的纵横比 |
| `gradient_refs` | `r` | `GradientRef` | 带 `gradientTransform` 的渐变引用，用 `href` 指向某个源渐变 |
| `conic_subgradients` | `s` | `SVGSubGradient` | 组成圆锥渐变的每一段小渐变 |
| `tilings` | `t` | `Tiling` | **不带变换**的“源”平铺图案 |
| `tiling_refs` | `p` | `TilingRef` | 带 `patternTransform` 的平铺引用 |

这里有一个贯穿后续 u5 单元的关键设计——**“源 + 引用”两层去重**：渐变和平铺各自用了两个去重器（`gradients`/`gradient_refs`、`tilings`/`tiling_refs`）。源去重器只存“纯定义、不含变换”，引用去重器只存“变换 + 指向源的 href”。这样同一个渐变即使用 100 种不同的变换出现，源定义也只写 1 份，另附 100 条极短的引用。本讲你只需记住“7 个去重器各管一摊、用不同字符区分命名空间”即可。

再看构造器：

[src/lib.rs:L265-L283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L265-L283) —— `new` 与 `with_options`：

```rust
fn new() -> Self {
    Self::with_options(None)
}

fn with_options(link_resolver: Option<Tracked<'a, LateLinkResolver<'a>>>) -> Self {
    SVGRenderer {
        link_resolver,
        glyphs: Deduplicator::new('g'),
        clip_paths: Deduplicator::new('c'),
        gradients: Deduplicator::new('f'),
        gradient_refs: Deduplicator::new('r'),
        conic_subgradients: Deduplicator::new('s'),
        tilings: Deduplicator::new('t'),
        tiling_refs: Deduplicator::new('p'),
    }
}
```

要点：

- `new()` 只是 `with_options(None)` 的简写——**不带链接解析器**，用于 `svg` / `svg_merged`（单页文件、合并多页）。
- `with_options(Some(link_resolver))` 才挂上链接解析器，用于 `svg_in_bundle` / `svg_in_html`，因为这两种宿主需要解析跨文档链接（`Destination::Location`）。
- 7 个 `Deduplicator::new(char)` 的字符就是上表里的前缀字符，最终会出现在 `<defs>` 里各资源的 `id` 开头（如 `g1A2B…`、`c3F4…`）。`DedupId` 的具体编码留到 u6-l3 讲。

> 注意：`SvgOptions`（pretty / render_bleed）并**不**存进 `SVGRenderer`，它只在公开函数 `svg` 等的局部用来决定 `page_bleed` 和 `xml_options`。渲染器自身只关心“要不要解析链接”这一件事。

#### 4.1.4 代码实践

**实践目标**：把“7 个去重器 + 不同前缀字符”这条结论落实到源码里。

**操作步骤**：

1. 打开 [src/lib.rs:L272-L283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L272-L283)，把每个字段与它 `Deduplicator::new('…')` 里的字符一一对应。
2. 接着跳到 [src/lib.rs:L529-L551](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L529-L551) 的 `impl SvgDisplay for DedupId`，看 `kind` 字符是如何被 `f.push_char(kind)` 写到 ID 最前面的。

**需要观察的现象**：ID 字符串由“1 个 kind 字符 + 大写十六进制哈希”组成，前导零会被 `trim_start_matches('0')` 砍掉。

**预期结果**：你会确认 7 个命名空间分别以 `g/c/f/r/s/t/p` 开头，互不冲突。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `link_resolver` 不是 `Deduplicator`，而是 `Option<Tracked<…>>`？

**参考答案**：`link_resolver` 不是“可复用的 SVG 资源”，而是一个外部能力（用来在渲染时把 `Destination::Location` 解析成 URI）。它只在 `svg_in_bundle` / `svg_in_html` 这类需要跨文档链接的宿主里存在，所以用 `Option` 表达“有或没有”，而不是去重容器。

**练习 2**：`gradients` 和 `gradient_refs` 为什么是两个分开的去重器，而不是合在一起？

**参考答案**：因为它们去重的“键”不同。源渐变不含变换，可以被任意多个引用复用；引用本身只记录变换 + 指向源的 `href`。把两者分开，才能做到“源定义只写一次，引用各自去重”，这是体积优化的关键。细节在 u5-l2。

---

### 4.2 State：贯穿渲染的变换与尺寸上下文

#### 4.2.1 概念说明

`State` 是一个 `#[derive(Copy, Clone)]` 的小结构体，只有两个字段：

```rust
struct State {
    transform: Transform,  // 当前元素相对页面根的累积变换
    size: Size,             // “当前层级里第一个硬帧（hard frame）的尺寸”
}
```

它的作用是**在递归遍历 Frame 树时，把“从页面根到当前元素”的坐标上下文一路带下去**。每深入一层子帧，就用 `pre_concat` / `pre_translate` 在变换上“叠加”这一层的局部位置；遇到硬帧就更新 `size`。因为 `State` 是 `Copy`，每层递归拿到的都是一份独立的快照，子层级对 `State` 的修改不会污染兄弟节点。

`size` 字段的注释写的是“The size of the first hard frame in the hierarchy”。它主要服务于渐变和平铺的 `RelativeTo::Self_`（相对元素自身尺寸）计算——这类计算需要知道“当前所在的硬帧有多大”。本讲你只需记住：`transform` 描述“我在哪、怎么转”，`size` 描述“我所在的那个硬帧多大”。

#### 4.2.2 核心流程

`State` 的四个方法可以分成两组：

- **叠加变换（不改变参照系）**：`pre_translate`、`pre_concat` —— 在当前变换基础上，往“更局部”的方向再垫一个变换。
- **替换字段**：`with_size`、`with_transform` —— 直接覆盖某个字段（用于硬帧重置坐标系）。

“前乘”的方向是这里的重点。设当前 `state.transform = A`，调用 `state.pre_concat(B)` 后得到的新变换是 \(A \cdot B\)。对一个点 \(p\) 作用时是 \(A \cdot B \cdot p\)，即**先用 B 变换，再用 A 变换**。换句话说，传入的 `B` 被放在了“离点更近”的内层。

用伪代码描述递归过程中的 `State` 流转：

```
进入一个位于 pos 的子元素时：
    new_state = state.pre_translate(pos)
    # 等价于 new_state.transform = state.transform · Translate(pos)
    # 点先被 Translate(pos) 搬到子元素的局部原点，再受父级变换作用
```

这就是为什么遍历 `Frame` 时，每个 `FrameItem` 的局部坐标 `pos` 能被正确地累加进全局变换——每一层都把自己的平移“垫”到内层，最终点会被从最深的局部坐标系一路映射到页面根坐标。

#### 4.2.3 源码精读

[src/lib.rs:L227-L263](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L227-L263) —— `State` 的全部实现：

```rust
#[derive(Copy, Clone)]
struct State {
    transform: Transform,
    size: Size,
}

impl State {
    fn new(size: Size) -> Self {
        Self { size, transform: Transform::identity() }
    }

    fn pre_translate(self, pos: Point) -> Self {
        self.pre_concat(Transform::translate(pos.x, pos.y))
    }

    fn pre_concat(self, transform: Transform) -> Self {
        Self {
            transform: self.transform.pre_concat(transform),
            ..self
        }
    }

    fn with_size(self, size: Size) -> Self {
        Self { size, ..self }
    }

    fn with_transform(self, transform: Transform) -> Self {
        Self { transform, ..self }
    }
}
```

逐条说明：

- `new(size)`：页面/帧的顶层 `State`，变换为单位矩阵，尺寸为画布尺寸。在七步骨架里对应 `State::new(size)` 这一步。
- `pre_translate(pos)`：是 `pre_concat(Translate(pos))` 的便捷写法。下文 `render_frame` 遍历每个元素时，就是用它把元素的局部坐标 `pos` 叠加进变换。
- `pre_concat(t)`：核心方法，做 `self.transform.pre_concat(t)`，即 \(M_{\text{self}} \cdot M_t\)。`..self` 保留 `size` 不变。
- `with_size(size)`：直接替换 `size`，用于进入硬帧时把参照尺寸切换成硬帧尺寸（详见 u2-l2 的 `render_group`）。
- `with_transform(transform)`：直接替换整个 `transform`，用于硬帧把坐标系“重置”为单位矩阵（详见 u2-l2）。

可以看到，所有方法都返回一个**新的 `State`**（因为 `Copy`，按值传递即可），从不原地修改——这让递归渲染的每一层都有独立、不可变的上下文快照。

#### 4.2.4 代码实践

**实践目标**：亲手算一次 `pre_concat` 的方向，确认“传入的变换在内层”。

**操作步骤**：

1. 设页面根 `state.transform = Translate(10, 20)`（记为 A）。
2. 现在进入一个位于 `pos = (5, 7)` 的子元素，调用 `state.pre_translate(pos)`，即 `pre_concat(Translate(5, 7))`（记为 B）。
3. 用矩阵乘法手算：结果变换 \(A \cdot B\) 对一个局部点 \(p = (1, 1)\) 的作用。

**需要观察的现象**：点 \(p\) 应当**先**被 B 平移到 \((6, 8)\)，**再**被 A 平移到 \((16, 28)\)。

**预期结果**：最终位置 \((16, 28)\)。这验证了 `pre_concat` 把传入变换放在内层（先作用），父级变换放在外层（后作用），与 SVG 里 `<g transform="A"><g transform="B">…</g></g>` 的嵌套语义一致——越内层的 `<g>` 越先作用于点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `State` 要标记 `#[derive(Copy, Clone)]`？如果改成可变引用 `&mut State` 会有什么问题？

**参考答案**：渲染是递归遍历一棵树，同一层可能有多个兄弟子帧。`Copy` 让每个递归分支拿到独立的上下文快照，某个分支深入时对 `State` 的叠加不会影响兄弟分支。若用 `&mut State`，则进入子帧叠加的变换会在返回后“漏”到兄弟节点，坐标全乱。

**练习 2**：`pre_translate(pos)` 与直接写 `state.transform = state.transform.post_concat(Translate(pos))` 效果一样吗？

**参考答案**：不一样。`pre_translate` 用的是 `pre_concat`，把平移垫在内层（点先平移再受父级变换）；`post_concat` 则是 `next.pre_concat(self)`，会把平移放到外层。遍历 Frame 时我们要的是“子元素局部坐标 → 父级坐标”的方向，所以必须用 `pre_concat`/`pre_translate`。

---

### 4.3 render_page：页面级渲染入口与背景处理

#### 4.3.1 概念说明

`render_page` 是七步骨架的第 5 步，它做**页面级**的三件事，然后把正文交给 `render_frame`：

1. **套一层变换 `<g>`**：把 `page_bleed` 算出来的出血平移 `ts` 应用到整页内容。
2. **画页面背景**：用一个大矩形填满画布（默认白色）。
3. **渲染页面正文帧**：调用 `render_frame`。

为什么要单独有个 `render_page`，而不是直接 `render_frame(&page.frame)`？因为页面层有两个帧级别没有的概念：**出血（bleed）**与**背景填充**。`Page` 结构体里 `frame`（页面内容帧）和 `bleed`（出血量）是分开的两个字段，背景颜色 `fill` 也存在 `Page` 上而不是 `Frame` 上。`render_page` 的职责就是把这些页面级属性“翻译”成一个矩形背景 + 一个平移变换，再降级成普通的帧渲染。

#### 4.3.2 核心流程

```
render_page(svg, state, ts, page):
  ┌─ 1. lazy_elem("g")：惰性创建一个 <g> 包装
  │     若 ts 非单位变换 → init() 并写 transform=ts
  │     （若 ts 是单位变换，则连 <g> 都不创建，子元素直接挂到父节点下）
  │
  ├─ 2. 若 page 有背景填充（fill_or_white，SVG 默认白色）：
  │     shape = Geometry::Rect(frame.size + bleed.sum_by_axis()).filled(fill)
  │     state' = state.pre_translate(-bleed.left, -bleed.top)
  │     render_shape(svg, state', &shape)     ← 复用统一的形状渲染管线
  │
  └─ 3. render_frame(svg, state, &page.frame) ← 正文内容，下一讲详讲
```

第 1 步用到的 `lazy_elem` 是一个“惰性元素”：只有调用 `init()` 时才真正在输出里 `<g>` 开始标签；若从头到尾没 `init()`，就什么标签都不产生。`svg.lazy()` 则返回“当前所在的那个元素”（已 init 就是 `<g>`，没 init 就是父 `<svg>`）。这套机制能避免生成无意义的空 `<g>`，细节属于 write.rs（u2-l3），本讲先当作“按需创建的 `<g>`”来理解。

#### 4.3.3 源码精读

[src/lib.rs:L285-L307](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L285-L307) —— `render_page` 全文：

```rust
fn render_page(
    &mut self,
    svg: &mut SvgElem,
    state: &State,
    ts: Transform,
    page: &Page,
) {
    let mut svg = svg.lazy_elem("g");
    if !ts.is_identity() {
        svg.init().attr("transform", SvgTransform(ts));
    }

    if let Some(fill) = page.fill_or_white() {
        let shape =
            Geometry::Rect(page.frame.size() + page.bleed.sum_by_axis()).filled(fill);
        let state =
            &state.pre_translate(Point { x: -page.bleed.left, y: -page.bleed.top });
        self.render_shape(svg.lazy(), state, &shape);
    }

    self.render_frame(svg.lazy(), state, &page.frame);
}
```

逐段拆解：

**① 包装 `<g>` 与出血平移**

`ts` 来自公开函数里 `page_bleed` 的返回值，是 `Transform::translate(bleed.left, bleed.top)`（见 [src/lib.rs:L155-L160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L155-L160)）。它的作用是把“内容帧”从画布左上角往右下平移一段，正好让出血区暴露在内容四周。当 `render_bleed` 关闭时 `bleed` 为零，`ts` 退化为单位变换，`ts.is_identity()` 为真，于是 `<g>` 根本不会被创建——这是命令行导出（CLI 里 `render_bleed` 硬编码为 false）的常态。

**② 背景矩形为什么用 `Geometry::Rect(...).filled(fill)`**

这是本讲的核心问题。`page.fill_or_white()`（定义在 [crates/typst-layout/src/document.rs:L118-L120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/document.rs#L118-L120)）对 SVG 目标会返回 `Some(白色)` 当作默认背景：

```rust
pub fn fill_or_white(&self) -> Option<Paint> {
    self.fill.clone().unwrap_or_else(|| Some(Color::WHITE.into()))
}
```

typst-svg **没有**为“页面背景”写一段专门的 `<rect>` 输出代码，而是构造了一个矩形 `Shape`：`Geometry::Rect(尺寸).filled(填充)`，然后丢给**统一的** `render_shape` 管线。这样做有两个好处：

- **复用**：矩形也是 `Shape` 的一种，背景填充和普通矩形走完全相同的颜色/渐变序列化路径（write_fill 等），避免重复实现一套“画矩形”的逻辑。
- **统一出血处理**：矩形的尺寸是 `page.frame.size() + page.bleed.sum_by_axis()`，即“内容帧 + 四周出血”，这样背景能铺满含出血的整张画布，而不只是内容区。

**③ 背景矩形的定位：`pre_translate(-bleed.left, -bleed.top)`**

这个负号平移容易让人迷惑，但它和第 ① 步的 `<g transform=ts>` 是配对的。逻辑是：

- 背景 `<path>` 渲染在 `<g>` 内部，而 `<g>` 已经带了 `translate(+bleed.left, +bleed.top)`。
- 我们希望背景矩形的**左上角**落在画布原点 `(0,0)`。所以在 `<g>` 的坐标系里，要把矩形原点设到 `(-bleed.left, -bleed.top)`，经 `<g>` 的 `+bleed` 平移后正好抵消，回到 `(0,0)`。
- 矩形宽高 = `frame.size + bleed.sum`，于是它从 `(0,0)` 铺到 `(frame.size + bleed.sum)`，正好覆盖整张含出血的画布。

当 `render_bleed` 关闭（bleed 全零）时，`pre_translate(0,0)` 是恒等的，矩形尺寸就是 `frame.size`，定位在 `(0,0)`——最常见、最简单的情形。

**④ 降级到 `render_frame`**

[src/lib.rs:L309-L324](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L309-L324) 的 `render_frame` 负责遍历 `page.frame.items()`，按 `FrameItem` 的六种变体（Group/Text/Shape/Image/Link/Tag）分发到 `render_group` / `render_text` / `render_shape` / `render_image` / `render_link`。注意 `render_page` 调用 `render_frame` 时传的是**原始的 `state`**（变换仍是单位矩阵、尺寸是画布尺寸），因为页面内容帧的元素都用各自相对于帧的局部坐标，会在 `render_frame` 内部逐个 `pre_translate(pos)` 累加。`render_frame` 与 `render_group` 的细节是下一讲 u2-l2 的主题。

#### 4.3.4 代码实践

**实践目标**：用 typst CLI 实际导出一份 SVG，验证“背景是一个矩形 `<path>`”以及“无出血时没有包装 `<g>`”。

**操作步骤**：

1. 新建一个最小文档 `bg.typ`：

   ```typ
   #set page(width: 100pt, height: 100pt)
   Hello
   ```
2. 用 CLI 导出 SVG（命令行下 `render_bleed` 恒为 false，所以 bleed 为零）：

   ```bash
   typst compile bg.typ bg.svg
   ```
3. 打开 `bg.svg`，定位 `<svg>` 根节点下的前几个元素。

**需要观察的现象**：

- 在正文（`<g>`、字形 `<use>` 等）之前，应能找到一个 `fill="#ffffff"`（白色）的 `<path>`，其路径数据对应一个 `100pt × 100pt` 的矩形。
- 因为 bleed 为零、`ts` 为单位变换，`render_page` 不会生成包装用的 `<g transform=...>`；背景 `<path>` 直接挂在 `<svg>` 下。

**预期结果**：背景矩形 `<path>` 的尺寸恰好等于页面尺寸（`100pt × 100pt`），填充为白色；且其外层没有多余的 `<g>` 包装。若改成程序化调用并打开 `render_bleed`，则会观察到：画布变大、出现 `<g transform="translate(bleed.left, bleed.top)">`、背景矩形铺满含出血的整张画布。

> 若本地没有 typst CLI，本实践可作为“源码阅读型实践”完成：对照 [src/lib.rs:L298-L304](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L298-L304) 推演 bleed 全零时背景矩形的尺寸与定位，结论应是“尺寸 = frame.size，定位 = 原点，无包装 `<g>`”。

#### 4.3.5 小练习与答案

**练习 1**：`render_page` 为什么要单独接收一个 `ts: Transform` 参数，而不是把它编码进 `state` 传进来？

**参考答案**：因为 `ts` 是**页面级**的包装变换，需要写到一个**包裹所有内容（含背景）的 `<g>`** 上；而 `state` 描述的是“渲染当前元素时用的累积变换”，二者用途不同。把 `ts` 单独拎出来，才能在 `<g transform=ts>` 这一层统一套住背景矩形和正文帧。

**练习 2**：如果 `page.fill_or_white()` 返回 `None`（即不要背景），`render_page` 会输出什么？

**参考答案**：跳过背景矩形那一段，只生成（可能的）包装 `<g>` 和 `render_frame` 的正文内容。SVG 的该区域就是透明的。不过对 SVG 目标而言，`fill_or_white` 在 `fill` 为 `None`/`Auto` 时会回落到白色，所以实践中几乎总会画出白色背景。

**练习 3**：在 `render_bleed = false` 时，第 ① 步的 `lazy_elem("g")` 实际上有没有产生 `<g>` 标签？为什么这能省体积？

**参考答案**：没有。因为 `ts.is_identity()` 为真，代码从不调用 `svg.init()`，`LazySvgElem` 在 `Drop` 时发现未初始化就不会写结束标签，于是 `<g>` 完全不出现，后续元素直接挂在父 `<svg>` 下。这避免了每个页面都套一层无意义的空 `<g transform="translate(0,0)">`，对体积和可读性都有好处。

## 5. 综合实践

把本讲三个模块串起来，做一次“端到端追踪”：

**任务**：以 `svg(page, opts)` 这一次公开调用为起点，画一张从 `SVGRenderer` 创建到背景矩形落盘的**完整时序图**，并回答三个问题。

**操作步骤**：

1. 从 [src/lib.rs:L32-L43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L32-L43)（`svg` 函数）出发，依次标注：`page_bleed` → `SVGRenderer::new()` → `svg_header` → `State::new(size)` → `render_page` → `finalize` → `end_document`。
2. 在 `render_page` 内部，标出 `lazy_elem("g")`、背景 `Geometry::Rect(...)`、`render_frame` 三步。
3. 回答：
   - `SVGRenderer::new()` 创建出的 7 个去重器此刻是空的还是有内容的？为什么？
   - 进入 `render_page` 时 `state.transform` 是什么？背景矩形的 `state` 又被 `pre_translate` 改成了什么？
   - 背景矩形渲染时调用的 `render_shape`，会不会把某些资源（比如渐变填充）塞进 `SVGRenderer` 的去重器？这些资源最终在哪里被写出？

**预期结果**：

- 7 个去重器创建时**全空**（`IndexMap::default()`），随渲染过程逐步填充。
- 进入 `render_page` 时 `state.transform` 是单位矩阵（来自 `State::new`）；背景矩形的 `state` 被 `pre_translate(-bleed.left, -bleed.top)` 叠加（bleed 全零时仍是单位矩阵）。
- 是的，若背景用的是渐变填充，`render_shape` → `write_fill` 会把源渐变塞进 `gradients` 去重器、把引用塞进 `gradient_refs`；这些资源最终在 `finalize` 阶段被 `write_gradients` / `write_gradient_refs` 写进 `<defs>`。这条链路把本讲的“渲染器状态”与第 5 单元的“绘制系统”连接了起来。

## 6. 本讲小结

- `SVGRenderer` 是“状态集中、行为分散”的工作台：结构体只在 `lib.rs` 定义一次，持有 `link_resolver` + **7 个 `Deduplicator`**（`g/c/f/r/s/t/p` 七个命名空间），方法分散在各文件的 `impl SVGRenderer` 块里。
- 7 个去重器分别缓存字形、裁剪路径、源渐变、渐变引用、圆锥子渐变、源平铺、平铺引用；其中渐变和平铺采用“源 + 引用”两层去重来压缩体积。
- `new()` 是 `with_options(None)` 的简写；只有 `svg_in_bundle` / `svg_in_html` 才通过 `with_options(Some(link_resolver))` 挂上链接解析器。`SvgOptions` 不存进渲染器。
- `State` 是 `Copy` 的渲染上下文，含 `transform`（累积变换）与 `size`（当前硬帧尺寸）；`pre_concat`/`pre_translate` 把传入变换垫在“内层”（先作用于点），`with_size`/`with_transform` 直接替换字段。
- `render_page` 做三件事：惰性创建带 `ts`（出血平移）的 `<g>`、用一个矩形 `Shape` 画背景（复用 `render_shape` 管线，尺寸含出血、用 `pre_translate(-bleed)` 定位到画布原点）、再把正文交给 `render_frame`。
- 页面背景用 `Geometry::Rect(...).filled(fill)` 而非专用 `<rect>` 代码，是为了复用统一的形状渲染管线，避免重复实现；`fill_or_white` 让 SVG 默认得到白色背景。

## 7. 下一步学习建议

本讲止步于 `render_page` → `render_frame` 的调用边界。下一讲 **u2-l2《Frame 遍历与 Group/Link/Anchor》** 会深入 `render_frame`，看它如何按 `FrameItem` 的六种变体分发，以及 `render_group` 如何处理软帧/硬帧（正是在那里会用上 `State::with_transform` 和 `with_size` 重置坐标系）。

如果你想先换个角度巩固本讲，建议带着这两个问题重读源码：

- 在 [src/lib.rs:L328-L360](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L328-L360) 的 `render_group` 里，`FrameKind::Hard` 分支为什么调用 `state.with_transform(Transform::identity()).with_size(group.frame.size())`？这与本讲的 `State` 有什么关系？
- `Deduplicator` 的 `insert_with` 为何用 `hash128(&key)` 而非直接存 key？答案在 u6-l3，但你可以在 [src/lib.rs:L493-L512](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L493-L512) 先做一次预读。
