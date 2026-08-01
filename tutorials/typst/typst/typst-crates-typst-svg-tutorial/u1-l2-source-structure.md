# 源码结构与模块职责

## 1. 本讲目标

在上一篇里，我们建立了 typst-svg 的「定位」：它消费 typst-layout 产出的 `Page` / `Frame`，翻译成一段 SVG 字符串。本讲要回答的是**「代码长什么样」**——具体来说：

1. 说出 `src/` 下 7 个 `.rs` 文件各自的核心职责。
2. 理解 `lib.rs` 作为**聚合入口**的组织方式：它用 `mod` 声明挂载子模块，再用 `use` 把各模块的类型拼装到一起。
3. 理解一个关键设计：**同一个结构体 `SVGRenderer` 的方法，被分散在 5 个不同文件里用多个 `impl` 块添加**。
4. 建立一条推荐的源码阅读顺序：**「输出抽象 → 原语 → 高层渲染 → 编排」**。

读完本讲，你应该能拿到任何一个渲染相关的方法名（比如 `render_shape`），迅速判断它住在哪个文件、属于哪一层。

## 2. 前置知识

### 2.1 Rust 的模块系统（速览）

- `mod foo;` 表示「当前 crate 里有一个名为 `foo` 的子模块」，编译器会去找 `foo.rs` 或 `foo/mod.rs`。在 typst-svg 里，`mod image;` 对应 `src/image.rs`。
- 默认情况下，模块里的内容是**私有**的。要让别的模块用到，要么用 `pub` 标记条目，要么用 `pub use` 把它「转出口」。
- `use crate::foo::Bar;` 是**绝对路径**引用，`crate` 代表当前 crate 的根。typst-svg 各文件之间大量用 `use crate::...` 来互相引用。

### 2.2 同一类型可以有多处 `impl`

Rust 允许在**同一个类型**上写多个 `impl` 块，而且这些 `impl` 块可以散落在**不同文件**里——只要该类型本身是在当前 crate 里定义的。typst-svg 正是用这一点把渲染逻辑拆成多个文件的：`SVGRenderer` 这个结构体只在 `lib.rs` 里定义一次，但它的方法被拆到了 `shape.rs`、`text.rs`、`paint.rs`、`image.rs` 里分别实现。

> 这一点是理解整个 crate 架构的钥匙，本讲的第 4.3 节会专门讲。

### 2.3 衔接上一篇

上一篇我们提到 typst-svg 约 2200 行、7 个源码文件，且「各文件均以 `impl SVGRenderer` 为同一个被拼装出来的类型添加方法」。本讲就把这句话拆开讲清楚。

## 3. 本讲源码地图

本讲只读一个入口文件，但会**横跨**它引用的所有文件：

| 文件 | 角色 | 本讲用来做什么 |
| --- | --- | --- |
| [`src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs) | 聚合入口 | 看 `mod` 声明、`use`/`pub use`、`SVGRenderer` 定义与编排方法 |
| [`src/write.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/write.rs) | 输出抽象地基 | 看它为什么是「最底层」、不依赖任何业务模块 |
| [`src/path.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/path.rs) | 路径原语 | 看它只依赖 `write` |
| [`src/shape.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs) | 形状渲染 | 看它的 `impl SVGRenderer` |
| [`src/text.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs) | 文本/字形 | 看它的 `impl SVGRenderer` 与对外类型 `RenderedGlyph` |
| [`src/paint.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs) | 绘制系统（颜色/渐变/平铺） | 看它对外暴露的类型 `GradientRef` 等 |
| [`src/image.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs) | 图像 | 看它的 `impl SVGRenderer` 与对外暴露的 `WebImage` |

> 提示：本讲是「见森林」的概览，每个文件的**实现细节**会在后续对应单元里展开。现在只需要建立「谁是谁、谁依赖谁」的全局图景。

## 4. 核心概念与源码讲解

### 4.1 七个源码文件的职责分工

#### 4.1.1 概念说明

typst-svg 没有把所有渲染代码塞进一个巨型 `lib.rs`，而是按**「关注点」**拆成了 7 个文件。可以这样理解这种拆分：渲染一份 Typst 文档，本质上要回答 5 类问题——

1. **怎么把数据写出来？** → 文本/字节输出抽象（`write.rs`）
2. **怎么画一条线/曲线？** → 矢量路径原语（`path.rs`）
3. **怎么画一个几何形状？** → 形状（`shape.rs`）
4. **怎么画文字？** → 文本与字形（`text.rs`）
5. **怎么填色/渐变/平铺？** → 绘制系统（`paint.rs`），以及**怎么放图片**（`image.rs`）

剩下的 `lib.rs` 负责**编排**：定义渲染器、决定渲染顺序、把上面 5 个模块串成一条流水线。

#### 4.1.2 核心流程：推荐的阅读顺序

如果从零开始读 typst-svg，**不要**先扎进 `lib.rs` 的细节。推荐按「从底层到高层」的顺序读：

```
write.rs   （输出抽象：怎么写 XML 元素、怎么格式化数字）
   ↑
path.rs    （矢量原语：怎么拼一段 SVG path 数据，依赖 write）
   ↑
shape.rs / text.rs / paint.rs / image.rs
            （高层渲染：实现具体的 render_xxx，依赖 path + write + lib 类型）
   ↑
lib.rs     （编排：定义 SVGRenderer，分发 FrameItem，调用上面各层）
```

关键直觉是：**下层不知道上层存在，上层组合下层**。`write.rs` 完全不关心「形状」或「文字」是什么；而 `shape.rs` 既要用 `write.rs` 输出元素，又要用 `path.rs` 生成路径数据。

#### 4.1.3 源码精读：每个文件的职责与代表代码

下表给出每个文件的职责、对外（crate 外或 lib.rs）提供的代表性条目，以及它在哪一行开始 `impl SVGRenderer`（如果有的话）。

| 文件 | 核心职责 | 代表性对外条目 | `impl SVGRenderer` 位置 |
| --- | --- | --- | --- |
| `write.rs` | 基于 `XmlWriter` 的 RAII 元素包装、数字/值格式化 | `SvgElem`、`SvgDisplay`/`SvgWrite` trait、`SvgTransform` 等 | **无**（独立类型） |
| `path.rs` | 用相对坐标生成 SVG path 数据，提取字形轮廓 | `SvgPathBuilder` | **无**（独立类型） |
| `shape.rs` | 把 `Shape` 渲染成 `<path>`，处理描边与几何转路径 | `render_shape`、`convert_curve` | 有：[`shape.rs:11`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L11) |
| `text.rs` | 渲染文本与字形（轮廓/图像字形），写字形定义 | `RenderedGlyph`、`render_text` | 有：[`text.rs:25`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L25) |
| `paint.rs` | 颜色序列化、填充/描边、线性/径向/圆锥渐变、平铺 | `GradientRef`、`SVGSubGradient`、`TilingRef` | 有：[`paint.rs:21`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L21) |
| `image.rs` | 渲染 `<image>`、base64 内嵌、PDF 图转 SVG（hayro） | `WebImage`、`convert_image_scaling` | 有：[`image.rs:18`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L18) |
| `lib.rs` | 聚合入口：公共 API、`SVGRenderer`/`State`、分发与编排 | `svg`/`svg_merged`/`svg_in_bundle`/`svg_in_html` | 有：[`lib.rs:265`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L265) |

注意两点：

- **只有 5 个文件有 `impl SVGRenderer`**：`lib.rs`（编排）加上 `shape`/`text`/`paint`/`image`（四类渲染对象）。
- **`write.rs` 和 `path.rs` 没有 `impl SVGRenderer`**：它们定义的是**独立工具类型**（`SvgElem`、`SvgPathBuilder`），不是渲染器自己的方法。这是「原语层」与「渲染层」的清晰分界。

例如，`text.rs` 里的渲染入口 `render_text` 就是挂载在 `SVGRenderer` 上的，见 [`src/text.rs:29`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L29)：

```rust
impl SVGRenderer<'_> {
    pub(super) fn render_text(&mut self, svg: &mut SvgElem, state: &State, text: &TextItem) {
        // ...
    }
}
```

它用到的 `SvgElem`、`State`、`SVGRenderer` 都不是 `text.rs` 自己定义的，而是从别处 `use` 进来的——这就引出下一节。

#### 4.1.4 代码实践：定位每个 `impl SVGRenderer` 的归属

这是一个源码阅读型实践，目标是让你亲手验证「5 个文件各有 `impl`、2 个没有」。

1. **实践目标**：建立「方法名 → 所在文件」的快速映射直觉。
2. **操作步骤**：
   - 在 `src/` 目录下搜索 `impl SVGRenderer`，记录每次出现的文件名与行号。
   - 再搜索 `render_shape`、`render_text`、`render_image`、`render_frame`、`write_fill`，分别记录它们定义在哪个文件。
3. **需要观察的现象**：搜索 `impl SVGRenderer` 应该恰好命中 5 处（`lib.rs`、`shape.rs`、`text.rs`、`paint.rs`、`image.rs`），而 `path.rs` 与 `write.rs` 不会命中。
4. **预期结果**：
   - `render_shape` → `shape.rs`
   - `render_text` → `text.rs`
   - `render_image` → `image.rs`
   - `render_frame`、`render_page`、`render_group` → `lib.rs`
   - `write_fill`、`write_stroke` → `paint.rs`
5. 若本地未运行搜索，请按上表手动核对（表中的行号已核实）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `write.rs` 和 `path.rs` 不需要 `impl SVGRenderer`？

> **参考答案**：因为它们提供的是**与渲染器无关的通用工具**。`SvgElem` 包装的是 XML 写出能力，`SvgPathBuilder` 拼的是路径字符串——这些能力不属于「渲染器状态」，任何持有 `&mut SvgElem` 的人都能用，所以它们是独立类型，而非渲染器的方法。

**练习 2**：`paint.rs` 是 7 个文件里最大的（包含颜色、填充、描边、三种渐变、平铺）。如果让你进一步拆分它，你会按什么维度拆？

> **参考答案（开放）**：一种自然维度是按「绘制对象类型」拆：颜色序列化（`Color` 的实现）、填充/描边入口、渐变（再细分为线性/径向与圆锥）、平铺。typst-svg 选择把它们留在同一文件，因为它们共享同一套「源 + ref 去重模型」，拆开反而要暴露更多内部类型。这体现了「拆分粒度」的取舍。

---

### 4.2 lib.rs 作为聚合入口：mod 声明与 use 依赖网络

> 这是本讲指定的两个最小模块之一：**lib.rs 的 mod 声明**、**lib.rs 顶部的 use 与 pub use**。

#### 4.2.1 概念说明

一个 crate 的根文件（`lib.rs`）天然是「门面」。typst-svg 让 `lib.rs` 承担三件事：

1. **挂载子模块**：用一串 `mod xxx;` 把 6 个文件接入 crate。
2. **转出口（re-export）**：用 `pub use` 把少数几个需要对外公开的条目（如 `WebImage`）从子模块「提升」到 crate 根，调用方可以直接写 `typst_svg::WebImage`。
3. **导入内部依赖**：用 `use crate::...` 把各子模块的类型拿到 `lib.rs` 作用域，以便定义 `SVGRenderer` 的字段、写出 `finalize` 里的定义。

换句话说，`lib.rs` 既是「目录索引」（`mod`），也是「进口报关」（`use`/`pub use`），还是「总装车间」（定义并编排 `SVGRenderer`）。

#### 4.2.2 核心流程：声明 → 转出 → 导入 → 定义

`lib.rs` 顶部按固定顺序展开：

```
1) mod 声明：把 6 个子模块挂进来
2) pub use：对外转出公开 API（WebImage 等）
3) 外部依赖：use typst_library::... / xmlwriter::...
4) 内部依赖：use crate::paint::... / crate::text::... / crate::write::...
5) 之后才是：公共导出函数、SVGRenderer、State、Deduplicator 等定义
```

这套顺序让你一眼就能看出「这个 crate 由哪些模块组成、对外暴露什么、内部依赖什么」。

#### 4.2.3 源码精读

**(a) mod 声明**——6 行，挂载全部子模块（[`src/lib.rs:3-8`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L3-L8)）：

```rust
mod image;
mod paint;
mod path;
mod shape;
mod text;
mod write;
```

注意：这些 `mod` 没有 `pub`，说明子模块本身对 crate 外不可见——外部用户只能通过 `lib.rs` 选择性 `pub use` 的条目访问。

**(b) pub use**——对外转出口（[`src/lib.rs:11`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L11)）：

```rust
pub use image::{WebImage, convert_image_scaling};
```

这正是上一篇提到的「typst-html 复用 typst-svg 的 `WebImage`/`convert_image_scaling`」的实现机制——它们被 `pub use` 到了 crate 根，因此外部能直接 `use typst_svg::WebImage;`。除此以外，crate 还公开了 `svg`/`svg_merged`/`svg_in_bundle`/`svg_in_html` 四个函数与 `SvgOptions` 结构体（它们就定义在 `lib.rs` 里，天生是 `pub`）。

**(c) 内部 use**——把子模块的类型拿到根作用域（[`src/lib.rs:26-28`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L26-L28)）：

```rust
use crate::paint::{GradientRef, SVGSubGradient, TilingRef};
use crate::text::RenderedGlyph;
use crate::write::{SvgDisplay, SvgElem, SvgTransform, SvgUrl, SvgWrite};
```

这三个 `use` 揭示了 `lib.rs` 与子模块的**反向依赖**：虽然「概念上」`lib.rs` 在最上层编排，但它需要 `paint` 提供的 `GradientRef`/`TilingRef`（用作 `SVGRenderer` 字段类型）、`text` 提供的 `RenderedGlyph`、以及 `write` 提供的全部输出抽象。这些类型随后就出现在 `SVGRenderer` 的字段里（[`src/lib.rs:188-225`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L188-L225)）：

```rust
struct SVGRenderer<'a> {
    // ...
    glyphs: Deduplicator<Option<RenderedGlyph>>,   // 来自 text
    gradients: Deduplicator<(Gradient, Ratio)>,
    gradient_refs: Deduplicator<GradientRef>,      // 来自 paint
    conic_subgradients: Deduplicator<SVGSubGradient>,
    tilings: Deduplicator<Tiling>,
    tiling_refs: Deduplicator<TilingRef>,          // 来自 paint
}
```

这就是「聚合」的具体含义：`SVGRenderer` 把分散在 `text.rs`、`paint.rs` 的类型，作为自己的字段类型，收拢到一个结构体里。

#### 4.2.4 代码实践：追踪一个符号的传播路径

1. **实践目标**：体会 `pub use` 如何让一个定义在子模块里的类型变成 crate 级别的公开 API。
2. **操作步骤**：
   - 在 `src/image.rs` 中找到 `WebImage` 的定义（`struct WebImage ...`）。
   - 回到 `src/lib.rs:11`，确认它被 `pub use image::{WebImage, convert_image_scaling};` 转出。
   - 想象一个下游 crate（如 `typst-html`）写 `use typst_svg::WebImage;`，这条路径之所以成立，正是因为步骤 2 的 `pub use`。
3. **需要观察的现象**：`WebImage` 在 `image.rs` 里定义，但外部不能写 `typst_svg::image::WebImage`（因为 `mod image` 是私有的），只能写 `typst_svg::WebImage`（因为 `pub use` 把它提升到了根）。
4. **预期结果**：理解「私有 `mod` + 选择性 `pub use`」是一种**控制公开 API 表面积**的常用手法——typst-svg 只想把 `WebImage` 和 `convert_image_scaling` 这两个图像工具暴露出去，而不是整个 `image` 模块。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `lib.rs:11` 的 `pub use`，外部还能用 `typst_svg::WebImage` 吗？为什么？

> **参考答案**：不能。`mod image` 是私有的，外部无法穿透到 `typst_svg::image::WebImage`；而 `WebImage` 本身定义在 `image.rs` 里，不经过 `pub use` 提升就不会出现在 crate 根上。删掉这行就等于把 `WebImage` 从公开 API 里移除。

**练习 2**：`lib.rs:26-28` 的三条 `use crate::...` 如果删掉，代码会在哪里报错？

> **参考答案**：会在 `SVGRenderer` 的字段定义处报错（`GradientRef`、`TilingRef`、`RenderedGlyph` 等变成未解析的标识符），以及在 `finalize`/各 `write_xxx` 调用处报错（`SvgElem` 等找不到）。这正说明 `lib.rs` 必须把子模块的类型「进口」进来，才能完成总装。

---

### 4.3 同一结构体的多文件拼装：impl SVGRenderer 的分布

> 这是从「静态结构」过渡到「运行时行为」的关键一节。它解释了为什么 7 个文件能拼成**一个**渲染器。

#### 4.3.1 概念说明

Rust 有一条规则：**只要类型在当前 crate 中定义，就可以在任意多个 `impl` 块里给它添加方法**，这些 `impl` 块甚至可以分布在不同文件——前提是它们都属于同一个 crate。

typst-svg 把这条规则用到了极致：

- `SVGRenderer` 结构体**只在 `lib.rs` 里定义一次**（[`src/lib.rs:188`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L188)）。
- 它的字段（7 个 `Deduplicator`）也只在 `lib.rs` 里出现。
- 但它的**方法**被拆成了 5 个 `impl` 块，分别住在 `lib.rs`、`shape.rs`、`text.rs`、`paint.rs`、`image.rs` 里。

为什么要这么拆？因为渲染器的行为可以按「渲染对象类型」清晰分类：画形状是一组方法、画文字是另一组、填色又是一组。把它们放进各自的主题文件，比挤在一个 `lib.rs` 里可读得多，同时它们又能共享 `SVGRenderer` 的字段（比如每个方法都能读写那 7 个去重器）。

#### 4.3.2 核心流程：一个类型，五处实现

```
            ┌─────────────────────────────────────────┐
            │   struct SVGRenderer { ... }            │  ← 仅在 lib.rs:188 定义一次
            │   （7 个 Deduplicator 字段）            │
            └─────────────────────────────────────────┘
                               │ 被 5 个 impl 块「挂方法」
   ┌───────────────┬───────────┴─────────┬───────────────┬───────────────┐
   ▼               ▼                     ▼               ▼               ▼
lib.rs         shape.rs              text.rs         paint.rs        image.rs
编排方法       render_shape          render_text     write_fill      render_image
render_page    convert_curve         render_glyph    write_stroke    WebImage
render_frame   write_stroke          write_glyph_    push_gradient
render_group                         defs            write_gradients
render_link                                         （圆锥/平铺…）
finalize
```

在运行时，这 5 个 `impl` 块的方法共同构成了**同一个** `SVGRenderer` 实例的能力。`lib.rs` 的 `render_frame` 在分发 `FrameItem` 时，调用的 `render_shape`/`render_text`/`render_image` 其实是分别定义在 `shape.rs`/`text.rs`/`image.rs` 里的——但对编译器来说，它们都是 `SVGRenderer` 的方法，没有任何区别。

#### 4.3.3 源码精读

**(a) 结构体的唯一定义**——字段集中在 `lib.rs`（[`src/lib.rs:188-225`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L188-L225)）：

```rust
struct SVGRenderer<'a> {
    link_resolver: Option<Tracked<'a, LateLinkResolver<'a>>,
    glyphs: Deduplicator<Option<RenderedGlyph>>,
    clip_paths: Deduplicator<EcoString>,
    gradients: Deduplicator<(Gradient, Ratio)>,
    gradient_refs: Deduplicator<GradientRef>,
    conic_subgradients: Deduplicator<SVGSubGradient>,
    tilings: Deduplicator<Tiling>,
    tiling_refs: Deduplicator<TilingRef>,
}
```

**(b) 编排方法**——也住在 `lib.rs`，以 `impl<'a> SVGRenderer<'a>` 开头（[`src/lib.rs:265`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L265)）。这里定义的是「调度」类方法，例如 `render_frame` 按 `FrameItem` 分发（[`src/lib.rs:310-324`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L310-L324)）：

```rust
fn render_frame(&mut self, svg: &mut SvgElem, state: &State, frame: &Frame) {
    for (pos, item) in frame.items() {
        let state = state.pre_translate(*pos);
        match item {
            FrameItem::Group(group) => self.render_group(svg, &state, group),
            FrameItem::Text(text) => self.render_text(svg, &state, text),
            FrameItem::Shape(shape, _) => self.render_shape(svg, &state, shape),
            FrameItem::Image(image, size, _) => self.render_image(svg, &state, image, size),
            FrameItem::Link(dest, size) => self.render_link(svg, &state, dest, *size),
            FrameItem::Tag(_) => {}
        }
    }
}
```

注意 `self.render_shape`、`self.render_text`、`self.render_image` 这三个调用——它们的方法体并不在 `lib.rs`，而在各自的主题文件里。

**(c) 各主题文件的 `impl` 入口**——每个都用 `impl SVGRenderer<'_> { ... }` 为同一个类型添砖加瓦：

- 形状：[`src/shape.rs:11`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L11)
- 文本：[`src/text.rs:25`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/text.rs#L25)
- 绘制：[`src/paint.rs:21`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L21)
- 图像：[`src/image.rs:18`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/image.rs#L18)

这些文件为了能在 `impl SVGRenderer` 里使用结构体名，都需要先把 `SVGRenderer`、`State` 从 `lib.rs` `use` 进来，例如 [`src/shape.rs:1-3`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L1-L3)：

```rust
use crate::path::SvgPathBuilder;
use crate::write::{SvgElem, SvgTransform, SvgUrl, SvgWrite};
use crate::{SVGRenderer, State};
```

> 一个细节：方法可见性用 `pub(super)`（如 [`shape.rs:13`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L13) 的 `pub(super) fn render_shape`）。`super` 指向 `lib.rs`（crate 根），所以这些方法对 `lib.rs` 可见、但对 crate 外不可见——它们是渲染器**内部**的协作方法，不构成公开 API。这与「公开的只有 4 个 `svg*` 函数 + `SvgOptions` + `WebImage`」完全一致。

#### 4.3.4 代码实践：阅读一条跨文件的调用链

1. **实践目标**：亲眼看到「`lib.rs` 的方法调用了别处文件定义的同类型方法」。
2. **操作步骤**：
   - 打开 [`src/lib.rs:310-324`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L310-L324) 的 `render_frame`，找到 `self.render_shape(...)` 这一行。
   - 跳转到 [`src/shape.rs:13`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/shape.rs#L13) 的 `render_shape` 定义。
   - 在 `render_shape` 里找到它对 `self.write_fill(...)` 的调用，再跳转到 [`src/paint.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs) 中的 `write_fill`。
3. **需要观察的现象**：这一条调用链 `render_frame → render_shape → write_fill` 横跨了 `lib.rs`、`shape.rs`、`paint.rs` 三个文件，但调用者与被调用者**都是同一个 `SVGRenderer` 实例的方法**（都是 `self.xxx`）。
4. **预期结果**：理解「多文件 `impl` 拼装」在运行时和单一 `impl` 块毫无区别——它纯粹是一种**代码组织**手段。
5. 待本地验证：用 `cargo doc` 或 IDE 的「Go to Definition」确认上述跳转路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SVGRenderer` 的字段必须集中在 `lib.rs`，而方法可以分散？

> **参考答案**：Rust 要求一个结构体的**定义**只能有一处（字段不能「散装」），但 `impl` 块可以任意多。因此 typst-svg 把「唯一的状态（7 个去重器）」锁在 `lib.rs`，把「可以分类的行为」拆到各主题文件。这是一种「状态集中、行为分散」的组织方式。

**练习 2**：各主题文件里方法都用 `pub(super)` 而非 `pub`。如果改成 `pub`，会带来什么问题？

> **参考答案**：`pub` 方法会成为 `SVGRenderer` 的公开 API 的一部分。但 `SVGRenderer` 本身是**私有**的（`struct SVGRenderer` 没有加 `pub`），所以即便方法写 `pub`，外部也无法拿到 `SVGRenderer` 类型来调用——并不会真的「泄漏」API，但会显得语义混乱、给阅读者错误暗示。`pub(super)` 准确表达了「仅供 crate 根（lib.rs）调度使用」的意图。

---

## 5. 综合实践：画出 typst-svg 的源码依赖关系图

这是本讲的总练习，把前面三节串起来。

### 5.1 实践目标

亲手绘制两张图，固化对 typst-svg 架构的理解：

1. **文件级 `use` 依赖图**（谁 `use` 谁）。
2. **`impl SVGRenderer` 拼装图**（哪些文件为同一个类型贡献方法）。

### 5.2 操作步骤

**第一步：收集依赖边。** 在 `src/` 下搜索每个文件里的 `use crate::...`，按下表整理（已给出核实结果，供你对照）：

| 文件 | `use crate::...` 指向的模块 | 解读 |
| --- | --- | --- |
| `write.rs` | `lib.rs`（`crate::DedupId`） | 最底层，几乎不依赖业务 |
| `path.rs` | `write.rs`（`SvgFormatter, SvgWrite`） | 原语层，只依赖输出抽象 |
| `shape.rs` | `path.rs`、`write.rs`、`lib.rs`（`SVGRenderer, State`） | 高层渲染 |
| `text.rs` | `path.rs`、`write.rs`、`lib.rs`（`DedupId, SVGRenderer, State`） | 高层渲染 |
| `paint.rs` | `path.rs`、`write.rs`、`lib.rs`（`DedupId, SVGRenderer, State`） | 高层渲染 |
| `image.rs` | `write.rs`、`lib.rs`（`SVGRenderer, State`） | 高层渲染（不需 path） |
| `lib.rs` | `paint.rs`、`text.rs`、`write.rs` | 反向依赖：用各模块的类型定义 `SVGRenderer` 字段 |

**第二步：画依赖图。** 用箭头表示「A `use` B」。你会得到一个**分层 + 回边**的结构：

```
        lib.rs  ◄──────────────── lib.rs（反向依赖：定义字段时用 paint/text/write 的类型）
          │ ▲                          │
          │ │  (lib.rs 用 shape::convert_curve)
          │ │                          ▼
   ┌──────┴┴┴───────┐          write.rs  ◄── path.rs
   ▼     ▼   ▼  ▼    │           ▲ ▲          ▲
shape  text paint image          │ │          │
  │      │    │    │             │ │          │
  └──────┴────┴────┴──► write.rs ┘ └──────────┘
            （shape/text/paint 都 use path + write；image 只 use write）
```

要点：

- `write.rs` 是**共同地基**，被 5 个文件依赖。
- `path.rs` 只被 `shape/text/paint` 依赖（图像不需要路径）。
- `lib.rs` 与子模块之间存在**双向**关系：子模块 `use crate::{SVGRenderer, State}`（向上要类型），`lib.rs` 又 `use crate::paint::...`（向下要字段类型）。这不是循环依赖，而是「类型定义」与「方法实现」的天然协作。

**第三步：画 `impl SVGRenderer` 拼装图。** 在依赖图上标注：只有 `lib.rs`、`shape.rs`、`text.rs`、`paint.rs`、`image.rs` 这 5 个文件里有 `impl SVGRenderer` 块；`write.rs`、`path.rs` 没有。在图上把这 5 个节点高亮，旁边写上它们各自贡献的代表方法（编排 / 形状 / 文本 / 绘制 / 图像）。

### 5.3 需要观察的现象

- 依赖图呈「中间宽、两头窄」：地基 `write.rs` 在下，编排 `lib.rs` 在上，四类高层渲染横在中间。
- `impl` 拼装图与依赖图**高度重合**：凡是实现了具体渲染对象（形状/文本/绘制/图像）的文件，都既 `use` 了底层、又为 `SVGRenderer` 贡献方法。

### 5.4 预期结果

你应该得到两张清晰的手绘或文本图，并能用一句话解释：「typst-svg 用 `write`+`path` 当地基，让 `shape/text/paint/image` 四类渲染各自为同一个 `SVGRenderer` 添方法，最后由 `lib.rs` 统一定义状态并编排调度。」这是后续每一篇讲义的认知底座。

## 6. 本讲小结

- typst-svg 共 7 个源码文件，按关注点拆分：`write`（输出抽象）、`path`（路径原语）、`shape`/`text`/`paint`/`image`（四类渲染对象）、`lib`（编排）。
- 推荐的阅读顺序是「**输出抽象 → 原语 → 高层渲染 → 编排**」，即 `write → path → shape/text/paint/image → lib`。
- `lib.rs` 是聚合入口：`mod` 声明挂载 6 个私有子模块，`pub use` 只对外暴露 `WebImage`/`convert_image_scaling` 等少量条目，`use crate::...` 把子模块类型拿来定义 `SVGRenderer` 字段。
- `SVGRenderer` 结构体**只在 `lib.rs` 定义一次**，但它的方法通过 5 个分散在 `lib/shape/text/paint/image` 的 `impl` 块拼装而成——「状态集中、行为分散」。
- `write.rs` 和 `path.rs` 没有 `impl SVGRenderer`，因为它们是**独立工具类型**，这是「原语层」与「渲染层」的分界。
- 各主题文件的方法用 `pub(super)` 可见性，说明它们是渲染器**内部协作**，不构成 crate 的公开 API。

## 7. 下一步学习建议

本讲建立的是「静态地图」。接下来应该进入**动态的渲染主链路**，看这张地图如何运转：

- 下一篇（u2-l1）将深入 `SVGRenderer` 与 `State`，看 `render_page` 如何处理页面背景与 bleed。
- 之后（u2-l2）会展开 `render_frame` 如何遍历 `FrameItem`、`render_group` 如何处理软/硬 frame 与裁剪。
- 再之后（u2-l3）专门讲 `write.rs` 这个「输出抽象地基」的内部设计（`SvgElem` 的 RAII、`LazySvgElem`、两个 trait）。

阅读建议：在进入下一篇之前，先把本讲的「依赖关系图」和「`impl` 拼装图」画一遍并留存——后续每读一个文件，都可以回来对照它在图中的位置。
