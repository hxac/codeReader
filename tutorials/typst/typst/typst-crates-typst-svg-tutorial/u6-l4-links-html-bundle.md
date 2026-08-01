# 链接、锚点与 HTML/Bundle 集成

## 1. 本讲目标

本讲是第 6 单元的收尾篇，聚焦 typst-svg 的「**对外接口层**」：当 SVG 不再是一个孤立的单页文件，而是要被嵌入 HTML、被打包进多文档 bundle、或被多页拼成一张图时，typst-svg 是如何与之衔接的。学完后你应当能够：

- 说清 `finalize` 在渲染结束后**按固定顺序**写出哪几类 `<defs>` 定义，以及为什么这个顺序里只有一处存在真正的依赖（圆锥子渐变必须在源渐变之后）。
- 把 `render_link` 的 `Destination::Location` 分支与外部的 `LateLinkResolver` 串成一条完整的「延迟解析 → 相对 URI」链路，并能解释 `Local` 与 `Cross` 两种解析结果的差异。
- 对比 `svg_in_bundle` / `svg_in_html` / `svg_merged` 三个集成入口在**坐标系、bleed（出血）、锚点、尺寸单位、链接解析**上的不同取舍。
- 解释为什么 `svg_in_html` 用 `em`（相对字号）而不是 `pt`（绝对点）来标注尺寸。

本讲承接 [u2-l1](./u2-l1-renderer-and-state.md)（SVGRenderer/State）、[u2-l2](./u2-l2-frame-traversal.md)（`render_link`/`render_anchor` 的元素级细节已在那里讲过，本讲只做**承接式回顾**并把视角抬到「集成」层）、[u1-l3](./u1-l3-public-api-and-usage.md)（四个公共 API 的签名），不再重复它们的逐行解读。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **延迟写出 / 去重模型**：typst-svg 在渲染主体时只**登记**资源（字形、裁剪路径、渐变、平铺）并写一个 `url(#id)` 或 `<use href="#id">` 引用；资源的「真身」被推迟到 `finalize` 阶段集中写进 `<defs>`。详见 [u6-l3](./u6-l3-deduplicator.md)（Deduplicator）与 [u4-l2](./u4-l2-glyph-defs.md)（字形定义）。
- **SVG `<defs>` 与 ID 引用**：SVG 允许把可复用元素放进 `<defs>`，再用 `id` 在任意位置引用；**引用是否生效取决于 ID 是否存在，与 `<defs>` 在文档中的出现先后无关**——这一点对本讲很关键。
- **Destination 与 Location**：Typst 里一个链接的目标 `Destination` 有三态（`Url` / `Position` / `Location`）；其中 `Location` 是排版期产生的**抽象位置引用**，要等到导出时才能解析成真实目标。
- **bleed（出血）**：印刷术语，为避免裁切误差而在页面边缘多留的区域；`Page.bleed` 是一组 `Sides`。
- **CSS 中「属性」与「样式」的优先级**：对 SVG/HTML 元素，写在 `style="..."` 里的 CSS 属性优先级**高于**同名的展现属性（presentation attribute，如 `width="..."`）。这一点是理解 `svg_in_html` 双重写尺寸的钥匙。

## 3. 本讲源码地图

本讲几乎全部源码集中在 **`src/lib.rs`** 一个文件（它是 typst-svg 的聚合入口与编排层）：

| 代码点 | 作用 | 本讲用到的视角 |
| --- | --- | --- |
| `finalize` | 渲染收尾，集中写出全部 `<defs>` | 7 个 `write_*` 的固定顺序与依赖 |
| `write_clip_path_defs` | 写裁剪路径定义（`finalize` 中唯一在 lib.rs 里定义的 `write_*`） | 作为「先引用、后定义」的样例 |
| `render_link` | 把一个链接渲染成 `<a><rect/></a>` | 承接 u2-l2，聚焦 `Location` → `LateLinkResolver` 链路 |
| `render_anchor` | 渲染一个带 `id` 的命名锚点 | 它在 bundle/html 入口里被批量调用 |
| `svg_in_bundle` | bundle 一份子的导出入口 | 锚点 + 跨文档链接解析 |
| `svg_in_html` | HTML 内嵌导出入口 | 裸 Frame、`em` 单位、自定义属性 |
| `svg_merged` | 多页纵向拼接导出入口 | 共享一个渲染器与一份 `<defs>` |
| `page_bleed` | 计算含出血的画布尺寸与平移变换 | 三个 `Page` 入口共享的尺寸原语 |
| `svg_header` / `svg_header_with_custom_attrs` | 写 `<svg>` 根元素的属性 | `viewBox`/`width`/`height`/命名空间，含 ≥1pt 兜底 |

此外会少量引用上游 crate：`typst-library/src/model/link.rs` 里的 `Destination` / `LateLinkResolver` / `ResolvedLink` / `into_relative_uri`，以及 `src/paint.rs` 里 `write_gradients` 对圆锥子渐变的登记副作用。

---

## 4. 核心概念与源码讲解

### 4.1 finalize：把所有 `<defs>` 集中写出

#### 4.1.1 概念说明

回顾 [u6-l3](./u6-l3-deduplicator.md) 与 [u4-l2](./u4-l2-glyph-defs.md) 已建立的认识：typst-svg 采用「**渲染期只登记 + 写引用，收尾期才写真身**」的模型。渲染主体（`render_page` / `render_frame`）遍历 Frame 树时，每遇到一个字形/裁剪路径/渐变/平铺，就往对应的 `Deduplicator` 里塞一条记录、拿到一个 `DedupId`，并在原地写一个引用（`<use href="#id">`、`clip-path="url(#id)"`、`fill="url(#id)"`）。真正的 `<symbol>` / `<clipPath>` / `<linearGradient>` / `<pattern>` 元素一个都没写。

把所有「真身」集中写出的动作，就是 `finalize`。它是渲染器生命周期的最后一步——注意签名是 `fn finalize(mut self, mut svg: SvgElem)`，**按值消费渲染器**，调用完渲染器即被消耗、丢弃。这也呼应了 [u2-l1](./u2-l1-renderer-and-state.md) 的结论：一个 `SVGRenderer` 实例的生命周期恰好对应一次导出。

#### 4.1.2 核心流程

`finalize` 按一个**写死的顺序**调用七个 `write_*` 方法，每个对应一类资源：

```
finalize(self, svg):
    1. write_glyph_defs        ← 字形（<symbol>，见 u4-l2）
    2. write_clip_path_defs    ← 裁剪路径（<clipPath>）
    3. write_gradients         ← 源渐变（<linearGradient>/<radialGradient>/圆锥 <pattern>）
    4. write_gradient_refs     ← 渐变引用（带变换的空壳，href 指向源）
    5. write_subgradients      ← 圆锥子渐变（每段扇形的双停靠 linearGradient）
    6. write_tilings           ← 源平铺（<pattern>）
    7. write_tiling_refs       ← 平铺引用（带变换的空壳）
```

理解这个顺序，要分清两件事：

**(a) 引用正确性不依赖顺序**。`DedupId` 是**内容寻址**的（由资源的哈希决定，见 [u6-l3](./u6-l3-deduplicator.md)），与写出位置无关；主体里早已写好的 `url(#id)` / `<use href>` 只要某个 `<defs>` 里有同 ID 元素就能解析，而 SVG 标准允许 `<defs>` 出现在文档任意位置。所以即便打乱顺序，最终 SVG 仍是合法的、引用仍能命中。

**(b) 但登记侧有一个真实依赖**。`write_gradients` 在写出**圆锥渐变**源 `<pattern>` 时，会顺带把每个扇形的 `SVGSubGradient` **塞进** `self.conic_subgradients` 去重表（这是一个副作用）。因此 `write_subgradients` 必须排在 `write_gradients` **之后**，才能看到全部已登记的子渐变。这是七步里唯一由数据流决定的硬约束；其余各步操作的是互不相同的 `Deduplicator`，相对顺序只是约定与可读性。

> 小贴士：`write_*` 里只有 `write_clip_path_defs` 定义在 `lib.rs`；其余六个定义在 `paint.rs`（渐变/平铺）与 `text.rs`（字形）。但它们都是同一个 `SVGRenderer` 的方法，靠「状态集中、行为分散」的拼装方式汇聚（见 [u1-l2](./u1-l2-source-structure.md)）。

** defs 写在主体之后**：由于 `SvgElem` 的 RAII 机制（[u2-l3](./u2-l3-write-abstraction.md)），`finalize` 创建的 `<defs>` 是根 `<svg>` 的子节点，而主体 `<g>` 在 `render_page` 时就已经作为更早的子节点写出并闭合。所以最终文档形态是：

```xml
<svg ...>
  <g>…主体（含 <use href="#…">、fill="url(#…)" 等引用）…</g>
  <defs>…字形 / 裁剪 / 渐变 / 平铺真身…</defs>
</svg>
```

先引用、后定义，在 SVG 里完全合法。

#### 4.1.3 源码精读

`finalize` 本体极简，就是七行顺序调用：[src/lib.rs:410-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L410-L419) —— 注意 `mut self` 消费渲染器、`mut svg` 借走根元素。

`write_clip_path_defs` 是 `lib.rs` 里唯一可见的 `write_*` 实现，可作为「先引用、后定义」的范本：[src/lib.rs:421-433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L421-L433)

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

- **空表早返回**：若没有任何裁剪路径登记过，直接 `return`，**不会写出空的 `<defs>`**。其余 `write_*` 也遵循同样模式，所以一份没有任何复用资源的简单 SVG，输出里根本不会出现 `<defs>`。
- **按登记顺序遍历**：`clip_paths.iter()` 返回 `(DedupId, &EcoString)`，顺序是**首次插入顺序**（`IndexMap` 保证），而非哈希序——这让输出对同一输入是稳定的、可复现的。

圆锥子渐变的登记副作用（印证 4.1.2(b)）：在 `write_gradients` 内部、写圆锥源 `<pattern>` 的循环里，[src/paint.rs:228-230](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L228-L230) 把每个扇形的 `SVGSubGradient` 插入 `self.conic_subgradients`。因此 `write_subgradients`（[src/paint.rs:287](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L287)）必须在其后运行——这正是 `finalize` 把它排在第 5 步的原因。

#### 4.1.4 代码实践

**实践目标**：确认 `finalize` 七步顺序，并定位那个「唯一的真实依赖」。

**操作步骤**（源码阅读型）：

1. 打开 [src/lib.rs:410-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L410-L419)，把七行调用抄成一张表，标注每行操作的是 `SVGRenderer` 的哪个 `Deduplicator` 字段（参考 [src/lib.rs:187-225](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L187-L225) 的字段定义）。
2. 跳到 [src/paint.rs:228-230](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/paint.rs#L228-L230)，确认 `write_gradients` 在写圆锥时会向 `conic_subgradients` 插入数据。

**需要观察的现象**：第 3 步 `write_gradients` 与第 5 步 `write_subgradients` 操作的是**同一个** `conic_subgradients` 表——前者写入、后者读出，故顺序不能颠倒。

**预期结果**：你能用自己的话说明——「除了圆锥子渐变这一处登记侧依赖，其余六步各管各的表，顺序可换；但 typst-svg 仍固定成 `源 → 引用` 的成对顺序（gradients→gradient_refs、tilings→tiling_refs），是为了让人类阅读输出时更清晰」。

#### 4.1.5 小练习与答案

**练习 1**：假如把 `finalize` 里的 `write_subgradients` 挪到 `write_gradients` **之前**，会发生什么？

**参考答案**：`write_subgradients` 会遍历一个**空的**（或残留的）`conic_subgradients` 表，因为填表动作发生在 `write_gradients` 写圆锥源 `<pattern>` 的循环里。结果是有圆锥渐变的文档里，那些扇形子渐变 `<linearGradient>` 不再被写出，圆锥 `<pattern>` 里 `fill="url(#…)"` 指向的 ID 找不到真身——圆锥渐变渲染破损。注意：不会编译报错，因为 ID 引用是字符串级的、运行期才暴露。

**练习 2**：为什么 `finalize` 用 `mut self`（按值消费）而不是 `&mut self`？

**参考答案**：因为收尾写出会**搬空**渲染器的去重表——例如 `write_glyph_defs` 用 `std::mem::take` 取走 `self.glyphs`（见 [u4-l2](./u4-l2-glyph-defs.md) 的 `assert!(self.glyphs.is_empty())` 不变量）。按值消费在类型层面表达「渲染器用完即弃」，避免收尾后被误用；这也与「一个渲染器对应一次导出」的语义一致。

---

### 4.2 render_link 与 render_anchor：与外部世界相连

#### 4.2.1 概念说明

`render_link` 与 `render_anchor` 的**元素级**细节（`<a>` + 透明 `<rect>`、双写 `href`/`xlink:href`、`<g id>` 空锚点）已在 [u2-l2](./u2-l2-frame-traversal.md) 讲透，本节只做一句话回顾，然后把镜头拉到**集成层**：这两者是怎么把一份 SVG 「接」进更大的上下文（bundle / HTML）的。

- **链接（link）**：`FrameItem::Link(dest, size)` 经 `render_link` 变成一个不可见但可点击的矩形 `<a><rect fill="transparent" stroke="none"/></a>`。当 `dest` 是 `Location` 时，目标 URI **不在排版期确定**，要由导出方提供的 `LateLinkResolver` 在导出时解析。
- **锚点（anchor）**：`render_anchor` 产出一个 `<g id="..." transform="translate(x y)">` 空元素，纯粹是一个**带名字的坐标点**。它本身不画东西，而是**等别人来跳**——别的文档或链接解析出 `#id` 片段后，就能精确落到这个点。

二者合起来构成了「**可被外界寻址**」的机制：锚点是「地址」，链接是「投递」。只有 bundle 与 html 两个入口会启用它们。

#### 4.2.2 核心流程

`Destination::Location` 的延迟解析是一条三段链路：

```
Location（排版期抽象位置）
        │
        │  ① resolver.resolve(loc)        ← LateLinkResolver
        ▼
ResolvedLink（Local { anchor } | Cross { from, to, anchor }）
        │
        │  ② link.into_relative_uri()      ← 拼相对 URI
        ▼
相对 URI 字符串（"#anchor" 或 "rel/path#anchor"）
        │
        │  ③ 写到 <a href="...">           ← render_link
        ▼
可点击的 <a>
```

`LateLinkResolver`（定义在上游 `typst-library`）持有两个字段：`base: Option<&VirtualPath>`（当前文档相对路径）与 `introspector`（能查 `Location` 落在哪个文档、哪个锚点）。`resolve` 据此分四象限：

| `base`（来源文档） | `to`（目标所在文档） | 结果 | 含义 |
| --- | --- | --- | --- |
| `None` | `None` | `Local { anchor }` | 单文件导出：目标就在本文档 |
| `Some(from)` | `Some(to)`，`from==to` | `Local { anchor }` | bundle 里目标就在同一文档 |
| `Some(from)` | `Some(to)`，`from≠to` | `Cross { from, to, anchor }` | bundle 里的跨文档链接 |
| `Some` / `None` 或 `None` / `Some` | — | `None`（解析失败） | 链不上的目标 |

`into_relative_uri` 再把 `ResolvedLink` 落成 URI：

- `Local { anchor }` → `#{anchor}`（即使 `anchor` 为空也写 `#`，因为单纯的 `#` 不会触发页面重载，而空 `href` 会）。
- `Cross { from, to, anchor }` → 先算 `to` 相对 `from.parent()` 的路径，做 percent-encoding，再拼 `#anchor`（锚非空时）。

`render_link` 用「`let-let` 链式短路」把三段串起来：任一步失败（渲染器没带解析器、`resolve` 返回 `None`、`into_relative_uri` 出错），`<a>` 就没有 `href`，成为「哑链接」——结构在、点不动。这是**有意的容错**：宁可留个空 `<a>`，也不让整个导出崩溃。

#### 4.2.3 源码精读

`render_link` 全貌：[src/lib.rs:362-401](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L362-L401)。重点看 `Location` 分支的三重 `if let`（[src/lib.rs:383-393](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L383-L393)）——它正是上面三段链路的源码形态：

```rust
Destination::Location(loc) => {
    if let Some(resolver) = self.link_resolver
        && let Some(link) = resolver.resolve(*loc)
        && let Ok(uri) = link.into_relative_uri()
    {
        a.attr("href", &uri);
        a.attr("xlink:href", &uri);
    }
}
```

- `self.link_resolver` 是 `Option<Tracked<LateLinkResolver>>`（[src/lib.rs:190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L190)）。只有 `svg_in_bundle` / `svg_in_html` 创建渲染器时传入 `Some`（见 4.3）；纯 `svg` / `svg_merged` 用 `SVGRenderer::new()`，此字段为 `None`，第一步即短路——所以单页文件里的 `Location` 链接必然是哑链接。

`Destination` 三态定义于上游：[typst-library/src/model/link.rs:295-302](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/link.rs#L295-L302)（`Url` / `Position` / `Location`）。

`LateLinkResolver::resolve` 的四象限逻辑：[typst-library/src/model/link.rs:675-693](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/link.rs#L675-L693)。`ResolvedLink` 枚举（`Local` / `Cross`）：[typst-library/src/model/link.rs:698-715](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/link.rs#L698-L715)。`into_relative_uri`：[typst-library/src/model/link.rs:724-747](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/link.rs#L724-L747)。

`render_anchor` 极短：[src/lib.rs:403-408](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L403-L408)

```rust
fn render_anchor(&mut self, svg: &mut SvgElem, pos: Point, id: &str) {
    svg.elem("g")
        .attr("id", id)
        .attr("transform", SvgTransform(Transform::translate(pos.x, pos.y)));
}
```

关键不在于它做了什么，而在于**谁调用它**——见 4.3.3 里 `svg_in_bundle`（[src/lib.rs:67-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L67-L69)）与 `svg_in_html`（[src/lib.rs:114-116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L114-L116)）渲染完主体后对 `anchors` 列表的循环。锚点与链接是**配对**的：一处用 `render_link` 写 `href="#anchor"`，另一处用 `render_anchor` 写 `<g id="anchor">`，两者靠 `id` 字符串匹配。

#### 4.2.4 代码实践

**实践目标**：在真实输出里看到「哑链接」与「锚点」，并理解它们何时会变「活」。

**操作步骤**（可运行 CLI 实践，待本地验证）：

1. 准备 `links.typ`：

   ```typst
   #link("https://typst.app")[外部链接]
   #link(<mylabel>)[内部跳转]
   = 标题 <mylabel>
   ```

2. 编译为单页 SVG（子命令以本地 `typst --help` 为准）：`typst compile --format svg links.typ links.svg`。
3. 在 `links.svg` 中搜索 `<a`、`xlink:href`、`id="`。

**需要观察的现象 / 预期结果**：

- 外部链接（`Url`）的 `<a>` 同时有 `href` 与 `xlink:href`。
- 内部跳转（`Location`）的 `<a>` 在单页 `svg` 导出下**没有 `href`**（哑链接）——因为 CLI 单页导出用 `SVGRenderer::new()`，不带 `link_resolver`。
- 由于单页 `svg` 不传 `anchors`，输出里**没有** `render_anchor` 生成的 `<g id>`。

> 若本地无 typst CLI，改为**源码阅读型实践**：对比 [src/lib.rs:267-269](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L267-L269)（`new()` 传 `None`）与 [src/lib.rs:60](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L60)（`svg_in_bundle` 传 `Some(link_resolver)`），解释为何同一份 Frame 在两种入口下链接行为不同。

#### 4.2.5 小练习与答案

**练习 1**：`ResolvedLink::Local` 与 `Cross` 在最终 URI 上最直观的差别是什么？

**参考答案**：`Local` 只产出 `#anchor`（无路径，指回本文档的锚点）；`Cross` 会产出 `相对路径#anchor`（指向 bundle 内另一个文档文件，再带锚点片段）。二者的分界正是 `LateLinkResolver` 的 `base` 与 `to` 是否指向同一文档。

**练习 2**：为什么 `render_link` 解析失败时选择「留个空 `<a>`」而不是跳过整个元素？

**参考答案**：为了**不破坏几何**。链接矩形虽然点不动，但它仍占据原本的版面位置；若直接跳过，可能影响读者对文档结构的理解，也让未来在解析器更完善时易于补全。同时哑 `<a>` 不会引发导出崩溃，是「输出鲁棒性优先」的取舍。

---

### 4.3 三种集成入口：bundle / html / merged 的差异

#### 4.3.1 概念说明

typst-svg 有四个导出入口（[u1-l3](./u1-l3-public-api-and-usage.md) 已列签名）。本节聚焦其中三个「非默认」入口——它们都解决「SVG 不是一座孤岛」的问题，但方向不同：

- **`svg_in_bundle`**：把单页渲染成 bundle（多文档打包）里的**一份子**。需要 `link_resolver`（以当前文档为 `base` 解析跨文档链接）与 `anchors`（暴露可被其它文档链接命中的锚点）。
- **`svg_in_html`**：把一个**裸 Frame**（注意不是 `Page`）渲染成适合内嵌进 HTML 的片段。需要 `link_resolver` 与 `anchors`，且尺寸用 `em`。
- **`svg_merged`**：把多页文档纵向拼成**一张** SVG 文件。不需要链接解析、不需要锚点，但要处理多页坐标系与页间留白。

默认的 `svg`（单页文件）是它们的「基准样板」，三者各自的差异都可用「相对 `svg` 改了什么」来描述。

#### 4.3.2 核心流程

先看共同骨架（所有入口都是这五步的变体）：

```
① page_bleed / 取 frame.size()      ← 算画布尺寸（html 无出血）
② SVGRenderer::new() / with_options ← 决定带不带 link_resolver
③ svg_header / with_custom_attrs    ← 写 viewBox/width/height（html 用 em）
④ render_page / render_frame        ← 渲染主体（+ 可选 render_anchor）
⑤ finalize + end_document           ← 收尾
```

差异集中在每一步的取舍。下表是本讲的核心对照：

| 维度 | `svg`（基准） | `svg_in_bundle` | `svg_in_html` | `svg_merged` |
| --- | --- | --- | --- | --- |
| 输入 | 单 `Page` | 单 `Page` | 裸 `Frame` + `text_size` | `PagedDocument` + `gap` |
| `link_resolver` | 无 | **有** | **有** | 无 |
| `anchors` | 无 | **有** | **有** | 无 |
| 处理 bleed | 由 `opts` 控制 | 由 `opts` 控制 | **不处理**（无 `Page`） | 由 `opts` 控制 |
| 渲染主体 | `render_page` | `render_page` + 锚点 | `render_frame` + 锚点 | 循环 `render_page` |
| 尺寸单位 | `pt` | `pt` | **`em`**（+ pt 兜底属性） | `pt` |
| `finalize` 次数 | 1 | 1 | 1 | **1（全文档共享）** |

三个最值得记住的差异：

1. **`svg_in_html` 是唯一不接收 `Page` 的**。它拿到的是裸 `Frame`，因此**没有页面背景填充**（不走 `render_page` 里 `fill_or_white()` 那条路）、**没有出血**概念，直接 `render_frame`。因为 HTML 里 SVG 只是文档流的一个片段，本就不需要「页」。
2. **`svg_in_html` 用 `em` 标注尺寸**。它把 `frame.width() / text_size` 与 `frame.height() / text_size` 写进 `style` 属性：

   \[
   w_{\text{em}} = \frac{w_{\text{pt}}}{s_{\text{text}}}, \qquad h_{\text{em}} = \frac{h_{\text{pt}}}{s_{\text{text}}}
   \]

   这样 SVG 的显示尺寸会**随宿主页面的正文字号一起缩放**，与文字保持视觉比例——这是它单独存在、不复用 `svg` 的根本原因。同时，`svg_header_with_custom_attrs` 仍会写出 `width`/`height` 的 `pt` 属性作为**兜底**：在支持 CSS 的浏览器里 `style` 覆盖属性（em 生效）；在只认属性的旧式 SVG 查看器里 `pt` 兜底。

3. **`svg_merged` 全文档共享一个渲染器**。它在页循环**之前**创建唯一的 `SVGRenderer`，循环里逐页 `render_page`，最后只调用**一次** `finalize`。这意味着所有页的字形/渐变/平铺**共用一份 `<defs>`**——同一字形在 10 页里出现，也只存一个 `<symbol>` 定义。这是合并导出在体积上的关键优势（也意味着 `Deduplicator` 的去重收益随页数增大而放大）。

#### 4.3.3 源码精读

**`svg_in_bundle`**：[src/lib.rs:45-73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L45-L73)。与基准 `svg`（[src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43)）相比只有两处不同：

```rust
let mut renderer = SVGRenderer::with_options(Some(link_resolver));   // ① 带 link_resolver
...
renderer.render_page(&mut svg, &state, ts, page);
for (pos, id) in anchors {                                            // ② 渲染锚点
    renderer.render_anchor(&mut svg, *pos, id);
}
renderer.finalize(svg);
```

`with_options(Some(...))` 的构造见 [src/lib.rs:272-283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L272-L283)：它把 `link_resolver` 存进渲染器，并初始化 7 个 `Deduplicator`。

**`svg_in_html`**：[src/lib.rs:75-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L75-L120)。重点在头部的自定义属性闭包（[src/lib.rs:93-109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L93-L109)）：

```rust
let mut svg = svg_header_with_custom_attrs(&mut xml, frame.size(), |svg| {
    if let Some(id) = id { svg.attr("id", id); }
    svg.attr_with("style", |attr| {
        attr.push_str("overflow: visible; width: ");
        attr.push_num(frame.width() / text_size);
        attr.push_str("em; height: ");
        attr.push_num(frame.height() / text_size);
        attr.push_str("em;");
        if !styles.is_empty() { attr.push_str(" "); attr.push_str(styles); }
    });
});
```

- `overflow: visible` 让 SVG 内容（如突出的描边）不被裁切。
- 之后用 `State::new(frame.size())` + `render_frame`（[src/lib.rs:111-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L111-L112)）渲染——没有页面背景、没有出血平移。

**`svg_merged`**：[src/lib.rs:122-153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L122-L153)。两段式：先算总画布，再逐页渲染。

总画布尺寸（[src/lib.rs:126-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L126-L132)）：宽度取各页最大值，高度为各页高度之和加上页间留白：

\[
H = \sum_{i=1}^{n} h_i + (n-1)\cdot \text{gap}
\]

逐页渲染（[src/lib.rs:138-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L138-L149)）把每页放到正确的纵坐标：

```rust
let mut y = Abs::zero();
for page in document.pages() {
    let (page_size, bleed_ts) = page_bleed(page, opts);
    let state = State::new(page_size);
    renderer.render_page(
        &mut svg, &state,
        Transform::translate(Abs::zero(), y).pre_concat(bleed_ts),
        page,
    );
    y += page_size.y + gap;
}
```

- `Transform::translate(0, y).pre_concat(bleed_ts)`：把「页内出血平移」垫在内层，「纵向累计偏移」放在外层——即先在页局部坐标系里把内容平移到含出血的画布原点，再把整页下移 `y`。
- 循环结束后**只调用一次** `finalize(svg)`（[src/lib.rs:151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L151)）——全文档共用一份 `<defs>`。

#### 4.3.4 代码实践

**实践目标**：对比 `svg` / `svg_in_html` / `svg_merged` 三者如何设置 `viewBox`/`width`/`height`、如何处理 bleed、如何串接 anchors，并解释 `svg_in_html` 为何用 `em`。

**操作步骤**（源码阅读型 + 可选 CLI）：

1. 打开三个入口的源码：[src/lib.rs:30-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L30-L43)（`svg`）、[src/lib.rs:75-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L75-L120)（`svg_in_html`）、[src/lib.rs:122-153](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L122-L153)（`svg_merged`），填下面这张表（答案见「预期结果」）：

   | | `svg` | `svg_in_html` | `svg_merged` |
   | --- | --- | --- | --- |
   | viewBox 来自 | ? | ? | ? |
   | width/height 单位 | ? | ? | ? |
   | 是否处理 bleed | ? | ? | ? |
   | 是否串 anchors | ? | ? | ? |

2. （可选 CLI，待本地验证）用 typst 分别导出单页 SVG 与多页合并 SVG，对比 `<svg>` 根元素的属性差异；若能用程序调用 `svg_in_html`（如通过 `typst` 作为库），观察同一 Frame 在 `pt` 与 `em` 下的根属性。

**需要观察的现象**：`svg_in_html` 的根 `<svg>` 同时有 `width`/`height`（pt 展现属性）与 `style="...width: ..em; height: ..em;"`（CSS）；`svg_merged` 的 `viewBox` 总高度大于任何单页；`svg`/`svg_merged` 输出里没有 `render_anchor` 产生的 `<g id>`。

**预期结果（填表答案）**：

| | `svg` | `svg_in_html` | `svg_merged` |
| --- | --- | --- | --- |
| viewBox 来自 | `page_bleed` 尺寸 | `frame.size()` | 各页累计的总尺寸 |
| width/height 单位 | `pt` | `em`（style）+ `pt`（属性兜底） | `pt` |
| 是否处理 bleed | 是（由 `opts`） | **否**（无 `Page`） | 是（每页各自） |
| 是否串 anchors | 否 | 是 | 否 |

**为何 `svg_in_html` 用 `em` 而非 `pt`**：独立 SVG 文件必须有确定尺寸才能被图片查看器正确显示，故用绝对单位 `pt`；而 HTML 里的 SVG 是文档流的一部分，应当**随正文字号一起缩放**、与文字保持视觉比例。把 `frame` 的 pt 尺寸除以 `text_size` 换算成 `em`（\( w_{\text{em}} = w_{\text{pt}}/s_{\text{text}} \)），SVG 的显示尺寸就锚定到了「多少个字号」而非「多少个点」，浏览器调字号时 SVG 同步放大缩小。保留 `pt` 属性作为兜底，是为了在不识别 CSS `style` 的旧式查看器里仍可显示。

#### 4.3.5 小练习与答案

**练习 1**：`svg_merged` 把 5 页拼成一张 SVG，`finalize` 被调用了几次？为什么这能省体积？

**参考答案**：**1 次**。`SVGRenderer` 在页循环之前创建一次，所有页共享同一个渲染器与同一组 7 个 `Deduplicator`，最后只 `finalize` 一次。于是 5 页里反复出现的同一字形/渐变/平铺只会被登记一次、在共享的 `<defs>` 里存一份定义，主体里各处用 `<use>`/`url(#id)` 引用——页数越多，去重收益越大。

**练习 2**：如果要把一个文档同时导成「HTML 内嵌」和「独立单页文件」，能否复用同一个 `svg()` 调用？为什么？

**参考答案**：不能直接复用。`svg()` 接收 `Page`、用 `pt`、不带 `link_resolver` 与 `anchors`，产出的是孤立单页文件；而 HTML 内嵌需要裸 `Frame`、`em` 单位、`link_resolver`（解析文档内 `Location` 链接）与 `anchors`（暴露锚点）。这正是 typst-svg 提供 `svg_in_html` 这个独立入口的根本原因——两者的「宿主语义」不同。

---

### 4.4 共享原语：page_bleed 与 svg_header_with_custom_attrs

#### 4.4.1 概念说明

三个 `Page` 入口（`svg` / `svg_in_bundle` / `svg_merged`）共享两个小工具：

- **`page_bleed`**：给定一页与选项，返回「含出血的画布尺寸」+「把内容平移到画布原点的变换」。它是 bleed 处理的单一真相源。
- **`svg_header_with_custom_attrs`**：写 `<svg>` 根元素的全部标准属性（`viewBox`/`width`/`height`/命名空间），并预留一个闭包让调用方插自定义属性（`svg_in_html` 用它插 `id`/`style`）。`svg_header` 是它的「无自定义属性」特例。

把这两个原语单独理解，三个入口的代码就只剩「业务差异」了。

#### 4.4.2 核心流程

`page_bleed`：

```
bleed = opts.render_bleed ? page.bleed : Sides::default()
size = page.frame.size() + bleed.sum_by_axis()     ← 画布往外扩
ts   = translate(bleed.left, bleed.top)            ← 内容往右下平移，腾出左/上出血
返回 (size, ts)
```

`svg_header_with_custom_attrs`：

```
size = size.max(splat(1pt))      ← 防御性：clamp 到 ≥1pt（resvg 等解析器处理不了 0 尺寸）
创建 <svg> 元素
write_custom_attrs(&mut svg)     ← 先写自定义属性（svg_in_html 在此插 id/style）
写 viewBox="0 0 w h"
写 width="wpt"、height="hpt"
写 xmlns / xmlns:xlink / xmlns:h5
返回 SvgElem（仍打开）
```

#### 4.4.3 源码精读

`page_bleed`：[src/lib.rs:155-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L155-L160)

```rust
fn page_bleed(page: &Page, opts: &SvgOptions) -> (Size, Transform) {
    let bleed = if opts.render_bleed { page.bleed } else { Sides::default() };
    let size = page.frame.size() + bleed.sum_by_axis();
    let ts = Transform::translate(bleed.left, bleed.top);
    (size, ts)
}
```

- `render_bleed` 为 `false` 时（CLI 默认），`bleed` 是全零 `Sides`，`size` 退化为页面本身尺寸、`ts` 为单位矩阵——这正是单页导出「无出血」的来源。
- `sum_by_axis()` 把四边的出血折算成宽高方向各加的总量（左+右加到宽，上+下加到高）。
- 返回的 `ts` 在 `render_page` 里作为最外层 `<g>` 的 `transform`（见 [u2-l1](./u2-l1-renderer-and-state.md)），把内容从「页面原点」平移到「含出血画布原点」。

`svg_header`（壳）：[src/lib.rs:437-440](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L437-L440) —— 只是用空闭包转发给 `svg_header_with_custom_attrs`。

`svg_header_with_custom_attrs`：[src/lib.rs:442-472](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L442-L472)。三个要点：

- **第 450 行的 clamp**：`size.max(Size::splat(Abs::pt(1.0)))` 把宽高各兜底到至少 1pt。注释解释 resvg 等 SVG 解析器处理不了 0 尺寸的 SVG——这是对下游消费者的防御。
- **自定义属性先于标准属性**（[src/lib.rs:454](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L454)）：先调 `write_custom_attrs`，再写 `viewBox`/`width`/`height`。这让 `svg_in_html` 的 `id`/`style` 排在属性列表前面（更符合人类阅读习惯，也便于 DOM 查找）。
- **三个命名空间**（[src/lib.rs:467-469](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L467-L469)）：默认 SVG、`xlink`（SVG 1.1 旧式引用，`render_link` 双写 `xlink:href` 即用此命名空间）、`h5`（XHTML，用于与 HTML 元素互操作，是 `svg_in_html` 场景的伏笔）。

#### 4.4.4 代码实践

**实践目标**：手算一页含出血的画布尺寸与平移变换，验证 `page_bleed` 的语义。

**操作步骤**（源码阅读 + 手算）：

1. 假设某页 `frame.size() = (200pt, 300pt)`，`page.bleed` 四边均为 `5pt`，`opts.render_bleed = true`。
2. 依 [src/lib.rs:155-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L155-L160) 手算 `size` 与 `ts`。
3. 再算 `opts.render_bleed = false` 时的 `size` 与 `ts`。

**需要观察的现象**：开出血后画布比页面大出一圈（左右各 5pt、上下各 5pt），`ts` 把内容平移 `(5, 5)`；关出血后画布等于页面、`ts` 为单位矩阵。

**预期结果**：

- 开出血：`size = (200+10, 300+10) = (210pt, 310pt)`；`ts = translate(5pt, 5pt)`。
- 关出血：`size = (200pt, 300pt)`；`ts = translate(0, 0)`（单位矩阵）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `svg_header_with_custom_attrs` 要把尺寸 clamp 到至少 1pt？

**参考答案**：因为 0 尺寸的 SVG 会被 resvg（以及很可能其它）SVG 解析器错误处理——可能渲染为空或报错。typst-svg 无法保证上游永远给出正尺寸（例如一个空 frame 的尺寸可能为 0），故在写出根属性时做防御性兜底，保证产物对下游消费者始终合法。

**练习 2**：`svg_header` 和 `svg_header_with_custom_attrs` 为什么是两个函数而不是一个带默认参数的函数？

**参考答案**：Rust 没有「默认参数」语言特性；要复用主体逻辑、又让 `svg`/`svg_merged`/`svg_in_bundle` 这些不需要自定义属性的调用方写得简洁，最自然的做法是把通用版做成 `svg_header_with_custom_attrs`（接收闭包），再提供一个传空闭包的薄壳 `svg_header`。这样三个 `Page` 入口用壳、`svg_in_html` 用完整版，各取所需。

---

## 5. 综合实践

**任务**：画出一次 **bundle 导出**（`svg_in_bundle`）从输入到产出的完整数据流，并把每一步对应到源码行号与产出的 SVG 片段。

要求覆盖以下要点：

1. **入参**：一个 `Page`、`SvgOptions`、一组 `anchors: &[(Point, EcoString)]`、一个 `link_resolver`。
2. 依次标注六步，并写出每步产出的 SVG 片段（伪 XML 即可）：
   - `page_bleed(page, opts)` → 算 `(size, ts)`（参考 [src/lib.rs:155-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L155-L160)）。
   - `SVGRenderer::with_options(Some(link_resolver))` → 创建带解析能力的渲染器（[src/lib.rs:60](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L60)）。
   - `svg_header` → `<svg viewBox="..." width=".." height=".." xmlns...>`（[src/lib.rs:62](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L62)）。
   - `render_page` → `<g transform="..">` 页面背景 + 主体；若主体含 `Destination::Location` 链接，画出它经 `LateLinkResolver` 三步解析后写出 `<a href="...#anchor">` 的过程（[src/lib.rs:383-393](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L383-L393)）。
   - `for (pos, id) in anchors { render_anchor(...) }` → `<g id="..." transform="translate(..)">`（[src/lib.rs:67-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L67-L69)）。
   - `finalize(svg)` → 在主体之后写出 `<defs>...</defs>`（[src/lib.rs:410-419](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs#L410-L419)）。
3. 在图上用箭头标出「链接的 `href="#anchor"`」与「锚点的 `id="anchor"`」是如何靠字符串匹配配对的。
4. 最后写一段说明：若把这次导出从 `svg_in_bundle` 换成 `svg_merged`，上述数据流中**哪些步骤会消失**（`link_resolver`、`anchors`、`render_anchor`、`Location` 解析），**什么会新增**（多页累计画布、页循环、共享一次 `finalize`）。

**预期产物**：一张数据流图（文字版即可）+ 一段对比说明。完成后，你应当能脱稿讲清「一次 bundle 导出里，链接是怎么从抽象 Location 变成可点击 URI 的、锚点又是如何被安放并被外界寻址的」。

---

## 6. 本讲小结

- **`finalize`** 按固定七步顺序（字形 → 裁剪 → 源渐变 → 渐变引用 → 圆锥子渐变 → 源平铺 → 平铺引用）集中写出全部 `<defs>`；其中只有「圆锥子渐变必须在源渐变之后」是真实数据依赖（登记副作用），其余顺序为约定。`<defs>` 写在主体之后，靠 ID 解析仍合法。
- **链接与锚点构成 bundle/html 的寻址机制**：`render_link` 把 `Location` 经 `LateLinkResolver` 三段链路（`resolve` → `into_relative_uri` → 写 `href`）解析为相对 URI，失败则留哑 `<a>`；`render_anchor` 产出带 `id` 的空 `<g>` 作为被链接的地址。二者靠 `id` 字符串配对。
- **`LateLinkResolver`** 据 `base` 与目标所在文档分四象限，产出 `Local`（`#anchor`）或 `Cross`（`相对路径#anchor`）；只有 `svg_in_bundle` / `svg_in_html` 传入它。
- **三个集成入口的差异**：`svg_in_bundle`（带链接解析 + 锚点）、`svg_in_html`（裸 Frame、无出血、`em` 单位 + `pt` 兜底）、`svg_merged`（多页纵向拼接、全文档共享一个渲染器与一份 `<defs>`、无链接/锚点）。
- **`svg_in_html` 用 `em`** 是为了让内嵌 SVG 随宿主页面正文字号缩放、保持视觉比例，同时保留 `pt` 属性兼容不识别 CSS 的查看器。
- **`page_bleed`** 是 bleed 处理的单一真相源（关出血即单位矩阵）；**`svg_header_with_custom_attrs`** 写根属性并把尺寸 clamp 到 ≥1pt 兼容下游解析器，自定义属性先于标准属性写出。

## 7. 下一步学习建议

- 若想补全「去重基础设施」的底层视角，回到 [u6-l3](./u6-l3-deduplicator.md) 看 `Deduplicator` 与 `DedupId` 编码，理解为何 `finalize` 能「按首次插入顺序」稳定输出。
- 若想深入链接解析的上游语义（`Location`、`Introspector`、bundle 的文档树），可阅读 `typst-library/src/model/link.rs` 中 `LateLinkResolver` 与 `EarlyLinkResolver` 的对比，以及 `introspection/locator.rs`。
- 若想横向对比「同一份 Frame 在不同导出格式下的链接处理」，可对照 typst-pdf 的链接注解（PDF annotation）与 typst-html 的 DOM 锚点策略，体会 typst-svg 这套「`<a>` + 透明 `<rect>` + `<g id>`」是 SVG 语义下的合理近似。
- 至此 typst-svg 的 7 个源文件已全部覆盖。建议重读 [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-svg/src/lib.rs) 顶部四个导出函数，确认你能把本讲的「集成层」与第 2 单元的「渲染主链路」在脑海里拼成一张完整的图。
