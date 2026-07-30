# jump_from_cursor 与源码到输出映射

## 1. 本讲目标

本讲是「双向跳转」系列的第三篇。u7-l1 讲了「点击渲染结果跳回源码」的入口 `jump_from_click`，u7-l2 打开了它的命中检测黑盒 `jump_from_click_in_frame`。本讲讲它的**逆方向**：`jump_from_cursor`——给定源码里的一个光标，找出它在渲染产物（分页文档或 HTML 文档）里的渲染位置。

学完后你应当能够：

- 说清 `jump_from_cursor` 的「只认 Text/MathText 叶子」过滤背后的原因；
- 理解 `JumpInDocument` 这个 sealed trait 如何用关联类型 `type Position` 让一个泛型函数同时服务 `PagedDocument` 和 `HtmlDocument` 两种后端（与 u7-l1 的 `JumpFromDocument` 完全对称）；
- 跟着 `find_in_frame` 看懂「在帧里按 glyph 的 span 反查坐标」、并在 `Group` 上施加**正向**变换；
- 跟着 `find_in_elem` 看懂「沿 DOM 索引路径定位文本/帧」，以及它产出的 `HtmlPosition` 如何用「DOM 索引路径 + frame 内坐标」共同确定一个渲染点；
- 用「数据流方向互逆」一句话对比 `jump_from_click` 与 `jump_from_cursor`。

## 2. 前置知识

本讲默认你已经学完 u7-l1（`Jump` 枚举、`jump_from_click`、`JumpFromDocument` sealed trait、`AsOutput`/`Output` 抽象、`PagedPosition`/`HtmlPosition` 位置类型）和 u7-l2（`jump_from_click_in_frame` 的命中检测）。这里只补充两个新概念，其余承接前两讲。

**Span——连接源码与渲染的「桥」。** Typst 在源码语法树的每个节点上挂一个 `Span`，又在渲染产物（帧里的字形、HTML 的文本/帧节点）上挂同一个 `Span`。于是：

- **正向**（本讲）：源码光标 → 语法树叶子节点的 `Span` → 去渲染产物里搜「谁的 `Span` 等于它」。
- **反向**（u7-l1/l2）：渲染点击点 → 命中的字形/图形的 `Span` → 反查它在源码里的字节偏移。

`Span` 就是这两个方向共用的唯一标识。理解了「`Span` 是桥」，本讲所有代码都只是「在桥的两端来回」。

**字节偏移 vs 光标 vs 字符偏移（codepoint）。** 源码侧用的是**字节偏移**（`usize`，UTF-8 字节）。HTML 文本节点内部用的是**字符偏移**（codepoint，与编码无关，见后文 `HtmlPosition::at_char`）。本讲里 `jump_from_cursor` 的 `cursor: usize` 是字节偏移；`find_in_elem` 只产出 DOM 路径、不产出字符偏移（节点粒度），二者要分清。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/typst-ide/src/jump.rs` | 本讲主战场。`jump_from_cursor` 入口、`JumpInDocument` sealed trait、`find_in_frame`（分页后端）、`find_in_elem`（HTML 后端）全部在这里 |
| `crates/typst-library/src/introspection/position.rs` | `PagedPosition` / `HtmlPosition` / `InnerHtmlPosition` 的定义与构造方法 |
| `crates/typst-html/src/dom.rs` | `HtmlNode` / `HtmlElement` 枚举、`HtmlNode::span()`、`iter_with_dom_indices()`（DOM 索引遍历器） |
| `crates/typst-ide/src/lib.rs` | 顶层导出，确认哪些是公共 API |

先看公共导出，确认边界：

[src/lib.rs:14-15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L14-L15) 这一行 `pub use` 把 `jump_from_cursor` 摆上货架。注意：`JumpInDocument` 这个 trait **没有**被 `pub use` 导出——它是 sealed 的、仅供约束泛型用，外部无法也无需命名它。

---

## 4. 核心概念与源码讲解

### 4.1 jump_from_cursor：公共入口与「只认文本」过滤

#### 4.1.1 概念说明

`jump_from_cursor` 解决的问题是：用户在编辑器里把光标放在源码某个位置（比如一段正文文字上），编辑器想知道「这段文字最终渲染到了画面的哪个点」，好在预览面板里高亮/滚动到那里。

它与 `jump_from_click` 是**方向相反的一对**：

| | 输入 | 输出 |
|---|------|------|
| `jump_from_click` | 渲染产物 + 渲染位置（点击点） | `Option<Jump>`（源码文件 + 字节偏移） |
| `jump_from_cursor` | 渲染产物 + 源码 + 光标 | `Vec<Position>`（渲染位置，可能多个） |

两个关键差异：①方向相反；②一个是 `Option`（一次点击一个落点），一个是 `Vec`（一段源码可能被渲染多次，比如函数被多次调用、内容被复用）。

**为什么只认 `Text` / `MathText` 叶子？** 这是本讲最重要的设计取舍。渲染帧里的字形（glyph）携带的 `span`，指向的是源码里的 `Text`/`MathText` 节点——也就是「肉眼可见的字符内容」。如果你把光标放在 `#rect(...)` 的 `rect` 上，它对应的是一个函数调用节点，渲染产物里并没有「一个字形挂着这个节点的 span」（图形虽然挂了 span，但 `find_in_frame` 的反查逻辑只比对文本字形，见 4.3）。所以「源码光标 → 渲染点」天然只对文本内容有意义。`jump_from_cursor` 用一个 `is_text` 过滤把这个边界显式表达出来：找不到文本叶子就直接返回空。

#### 4.1.2 核心流程

```
jump_from_cursor(document, source, cursor)
  │
  ├─ 1. root = LinkedNode::new(source.root())      # 取语法树根并套上位置上下文
  │
  ├─ 2. 找文本叶子：
  │      leaf_at(cursor, Side::Before)  若是 Text/MathText → 用它
  │      否则 leaf_at(cursor, Side::After) 若是 Text/MathText → 用它
  │      否则 → return vec![]   （光标不在文本上）
  │
  ├─ 3. span = node.span()                         # 取该叶子节点的 Span（桥）
  │
  └─ 4. document.find_span(span)                   # 把 Span 交给后端去渲染产物里搜
            → Vec<Position>
```

第 2 步先试 `Side::Before` 再试 `Side::After`，沿用 u2-l1 讲过的规则：在两个 token 的交界点，`Before` 偏向「前一个 token」、`After` 偏向「后一个 token」。对文本来说，这处理了光标正好落在两段文字交界的情况。

注意签名里**没有 `world` 参数**（对比 `jump_from_click(world, document, position)`）。因为本方向只需 `source`（取语法树）和 `document`（搜渲染产物），不需要把 `Span` 翻译回「文件 id + 字节偏移」——那是反方向 `jump_from_click` 才需要的活。

#### 4.1.3 源码精读

函数全貌：[src/jump.rs:343-363](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L343-L363)

```rust
pub fn jump_from_cursor<D: JumpInDocument>(
    document: &D,
    source: &Source,
    cursor: usize,
) -> Vec<D::Position> {
    fn is_text(node: &LinkedNode) -> bool {
        matches!(node.kind(), SyntaxKind::Text | SyntaxKind::MathText)
    }

    let root = LinkedNode::new(source.root());
    let Some(node) = root
        .leaf_at(cursor, Side::Before)
        .filter(is_text)
        .or_else(|| root.leaf_at(cursor, Side::After).filter(is_text))
    else {
        return vec![];
    };

    let span = node.span();
    document.find_span(span)
}
```

要点逐条对照：

- 泛型 `<D: JumpInDocument>` 与返回类型 `Vec<D::Position>`：`D::Position` 是关联类型，传 `PagedDocument` 时编译期就确定为 `PagedPosition`、传 `HtmlDocument` 时确定为 `HtmlPosition`。一个函数体服务两种后端，经单态化零成本——这正是 u7-l1 讲过的 sealed trait 套路，这里换成了它的对称兄弟 `JumpInDocument`。
- 内部 `fn is_text`（[src/jump.rs:348-350](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L348-L350)）就是「只认文本」过滤的实现。
- `.filter(is_text).or_else(...)` 这条链：先按 `Before` 取叶子、若不是文本则改用 `After` 的叶子；两个都不是文本（或都取不到叶子）就走 `let Some(...) else { return vec![] }` 返回空。

#### 4.1.4 代码实践

实践目标：亲手验证「只认文本」过滤与 `Before/After` 的差异。

操作步骤（源码阅读 + 推理型）：

1. 看测试 [src/jump.rs:761-765](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L761-L765) 的源码串 `*Hello* #box[ABC] World`。
2. 数清每个字符的字节索引：`*`(0) `H`(1) `e`(2) `l`(3) `l`(4) `o`(5) `*`(6) ` `(7) `#`(8) `b`(9) `o`(10) `x`(11) `[`(12) `A`(13) `B`(14) `C`(15) `]`(16) …。
3. 测试断言：`test_cursor(s, 12, None)` 与 `test_cursor(s, 14, pos(1, 37.55, 16.58))`。

需要观察的现象与预期结果：

- 光标 `12` 落在 `[` 上——它是 markup 的方括号，不是 `Text` 叶子，故 `leaf_at(...).filter(is_text)` 与 `After` 分支都失败 → 返回空 `Vec` → `expected = None`。这就是「只认文本」过滤的体现。
- 光标 `14` 落在 `B` 上——`[ABC]` 内部是一个 `Text` 叶子节点 `"ABC"`，`is_text` 命中 → 取它的 `span` 去帧里搜 → 命中 `(37.55pt, 16.58pt)`。
- 思考题：把光标改成 `13`（落在 `A` 上）应得到什么？预期同样是同一个 `Text` 叶子的 `span`，因此渲染点位置与 `14` 完全相同（节点粒度，不分字符）。

> 说明：上述坐标 `37.55 / 16.58` 来自测试里的 `assert_approx_eq`，属于字体度量结果，**待本地验证**你机器上的具体像素值（受字体影响），但「命中同一文本节点」这一结论与具体数值无关。

#### 4.1.5 小练习与答案

**练习 1**：`jump_from_cursor` 为什么返回 `Vec` 而 `jump_from_click` 返回 `Option`？

参考答案：一次点击只对应渲染产物上的一个点，故反查源码只得一个落点（`Option<Jump>`）；而源码里的一个 `Span`（尤其是一段被复用/被多次求值的文本）可能出现在渲染产物的多个位置，所以正查要返回所有命中（`Vec<Position>`）。

**练习 2**：把光标放在 `#rect(width: 10pt)` 的 `rect` 上，`jump_from_cursor` 会返回什么？为什么？

参考答案：返回空 `Vec`。因为 `leaf_at` 得到的叶子不是 `Text`/`MathText`，被 `is_text` 过滤掉。反查管线只在文本字形上挂 `span`、且 `find_in_frame` 也只比对字形 span，所以非文本节点没有可定位的渲染点。

---

### 4.2 JumpInDocument：与 JumpFromDocument 对称的 sealed trait

#### 4.2.1 概念说明

`JumpInDocument` 是 `jump_from_cursor` 的后端抽象，与 u7-l1 的 `JumpFromDocument` 是**镜像对称**的一对 sealed trait：

| | 正向（本讲） | 反向（u7-l1） |
|---|---|---|
| trait | `JumpInDocument` | `JumpFromDocument` |
| 关键方法 | `find_span(span) -> Vec<Position>` | `resolve_position(pos) -> Option<Jump>` |
| 语义 | 源码 `Span` → 渲染位置 | 渲染位置 → 源码 `Jump` |
| 实现者 | `PagedDocument`、`HtmlDocument` | `PagedDocument`、`HtmlDocument` |

二者结构完全同构：都是「公开空壳 trait + 私有 sealed supertrait」两层。公开层让外部能写泛型约束 `D: JumpInDocument`，私有层把真正的 `find_span` 实现藏起来、使外部 crate 无法再为别的类型实现——从而**封闭后端集合**，保留 typst-ide 的演进自由（与 u7-l1 同理）。

#### 4.2.2 核心流程

```
公开 trait JumpInDocument: jump_in_document_sealed::JumpInDocument {}
                          ↑ 只能被本 crate 的 sealed supertrait 实现

mod jump_in_document_sealed {
    trait JumpInDocument {
        type Position;                          // 关联类型：后端↔位置类型配对
        fn find_span(&self, span) -> Vec<Self::Position>;
    }
    impl JumpInDocument for PagedDocument { type Position = PagedPosition; ... }
    impl JumpInDocument for HtmlDocument   { type Position = HtmlPosition;  ... }
}
```

类型系统在这里替我们做静态保证：只要 `D: JumpInDocument`，`D::Position` 就自动是正确的位置类型，调用方拿到 `Vec<D::Position>` 后无需再做运行时分派。

#### 4.2.3 源码精读

公开空壳与两个实现者声明：[src/jump.rs:365-371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L365-L371)

```rust
pub trait JumpInDocument: jump_in_document_sealed::JumpInDocument {}
impl JumpInDocument for PagedDocument {}
impl JumpInDocument for HtmlDocument {}
```

私有 supertrait 及其 `find_span` 签名：[src/jump.rs:385-389](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L385-L389)——注意只有 `find_span` 一个方法、一个关联类型 `Position`，比 `JumpFromDocument` 的 `resolve_position` 更简单（无需 `world` 参数）。

两个后端的 `find_span` 实现就是后续 4.3、4.4 的全部内容。

#### 4.2.4 代码实践

实践目标：体会 sealed trait 如何「封闭集合 + 复用代码」。

操作步骤（源码阅读型）：

1. 打开 [src/jump.rs:367-371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L367-L371)，确认只有 `PagedDocument` 和 `HtmlDocument` 两个实现者。
2. 思考：若 typst 未来新增第三种输出后端（比如某种「幻灯片文档」），需要改动哪些地方？

预期结果：只需在 sealed 模块里加一个 `impl JumpInDocument for SlidesDocument { type Position = ...; fn find_span(...) }`，公开层加一行 `impl JumpInDocument for SlidesDocument {}`，`jump_from_cursor` 函数体**一行都不用改**——泛型 + 关联类型让入口对所有后端通用。这就是 sealed trait 相比「写死两个自由函数」的价值。

#### 4.2.5 小练习与答案

**练习**：为什么 `JumpInDocument` 的 `find_span` 不需要 `world`，而 `JumpFromDocument` 的 `resolve_position` 需要 `world: &dyn IdeWorld`？

参考答案：`find_span` 只在渲染产物**内部**按 `span` 搜坐标，不碰源码、不碰文件系统；`resolve_position` 命中后要把 `Span` 翻译成「文件 id + 字节偏移」，这需要 `world.source(id)` 去取源码并定位（见 u7-l2 里 `world.source(id)`、`source.find(span)` 的用法），所以必须传入 `world`。

---

### 4.3 find_in_frame：在分页文档的帧里反查 span

#### 4.3.1 概念说明

`find_in_frame` 是 `PagedDocument` 后端的搜索核心。分页文档由若干「页」组成，每页一个 `Frame`，`Frame` 里装着一组 `FrameItem`：`Text`（一串带 `span` 的字形）、`Shape`、`Image`、`Group`（子帧 + 变换 + 可选裁剪）、`Link`。

`find_in_frame` 要回答：在某个 `Frame` 里，哪个字形挂的 `span` 等于目标 `span`？命中就返回那个字形在帧坐标系里的 `Point`。

**关键设计：对 `Group` 施加「正向」变换。** u7-l2 的 `jump_from_click` 是把点击点用 `transform.invert()` 从父坐标系**逆向映射**到子帧坐标系后再递归。本讲的 `find_in_frame` 方向相反：先在子帧坐标系里递归找到一个 `point`，再用**正向** `group.transform` 把它从子坐标系映射回父坐标系。两者在数学上互为逆运算。

#### 4.3.2 核心流程

```
find_in_frame(frame, span) -> Option<Point>
  for (pos, item) in frame.items():
    Group(group):
      若 find_in_frame(&group.frame, span) 命中 point：
        return Some(pos + point.transform(group.transform))   # 正向变换 + 组原点
    Text(text):
      for glyph in text.glyphs:
        若 glyph.span.0 == span： return Some(pos)             # 这个字形的左上角
        pos.x += glyph.x_advance.at(text.size)                 # 否则光标右移一个字宽
  None
```

注意两点：

1. **只比对 `Text` 与 `Group`**，不比对 `Shape`/`Image`/`Link` 的 span——这正好与 `jump_from_cursor` 的「只认文本」过滤一致：源码光标只能映射到文本字形。
2. 字形命中后返回的是字形**起点** `pos`（左侧基线参考点），返回前不继续累加字宽。

数学上，正向变换把子帧局部点 \(p_{\text{local}}\) 映射到父帧点：

\[
p_{\text{parent}} = \text{origin}_{\text{group}} + T(p_{\text{local}})
\]

其中 \(T\) 是 `group.transform`。这与 u7-l2 反方向用 \(T^{-1}\) 把父点映回子点恰好互逆。

#### 4.3.3 源码精读

`PagedDocument::find_span`：[src/jump.rs:395-407](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L395-L407)

```rust
fn find_span(&self, span: Span) -> Vec<Self::Position> {
    self.pages()
        .iter()
        .enumerate()
        .filter_map(|(i, page)| {
            find_in_frame(&page.frame, span).map(|point| PagedPosition {
                page: NonZeroUsize::new(i + 1).unwrap(),  // 页码从 1 开始
                point,
            })
        })
        .collect()
}
```

逐页枚举、每页调 `find_in_frame`，命中就包成 `PagedPosition { page, point }`，用 `filter_map` + `collect` 自然收集成 `Vec`（一页一个、跨页多次命中都会被收进来）。

`find_in_frame` 本体：[src/jump.rs:419-438](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L419-L438)

```rust
fn find_in_frame(frame: &Frame, span: Span) -> Option<Point> {
    for &(mut pos, ref item) in frame.items() {
        if let FrameItem::Group(group) = item
            && let Some(point) = find_in_frame(&group.frame, span)
        {
            return Some(pos + point.transform(group.transform));   // 正向变换
        }

        if let FrameItem::Text(text) = item {
            for glyph in &text.glyphs {
                if glyph.span.0 == span {
                    return Some(pos);                              // 字形起点
                }
                pos.x += glyph.x_advance.at(text.size);
            }
        }
    }
    None
}
```

对照 u7-l2 的 `jump_from_click_in_frame`：那段是对 `Group` 做 `transform.invert()` 把点击点映进子帧、然后递归；这里反过来，先递归拿到子帧局部点 `point`，再 `point.transform(group.transform)` 映回父帧并加上组原点 `pos`。一正一逆，正好闭环。

#### 4.3.4 代码实践

实践目标：验证「正向变换」在带 `#rotate` 的内容上仍能正确定位。

操作步骤（推理型，结合测试）：

1. 看测试 [src/jump.rs:773-779](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L773-L779)，源码 `#rotate(90deg, origin: bottom + left, [hello world])`，光标 `-5`（从串末倒数，见 u8-l1 的负光标约定）落在 `[hello world]` 的文本上。
2. 推理渲染过程：`[hello world]` 是文本，被 `rotate(90deg)` 包裹 → 在帧里成为一个带 90° 旋转变换的 `Group`，子帧里是 `Text` 字形。
3. 跟踪 `find_in_frame`：在 `Text` 字形上命中 `span` 得到子帧局部点 `point`（约文本行起点）→ 回到 `Group` 分支执行 `pos + point.transform(group.transform)`，90° 旋转变换把这个局部点转成页面坐标。

需要观察的现象与预期结果：测试断言结果为 `pos(1, 10.0, 16.58)`（第 1 页，约 `(10pt, 16.58pt)`）。关键不是数值，而是「即便文字被旋转，正查仍能算出它在页面上的真实位置」——这完全依赖 `point.transform(group.transform)` 这一行。

> 该坐标为字体度量结果，**待本地验证**你机器上的具体值。

#### 4.3.5 小练习与答案

**练习 1**：`find_in_frame` 为什么不检查 `Shape`/`Image` 的 `span`？

参考答案：因为它的调用方 `jump_from_cursor` 只会在「文本叶子」上触发（`is_text` 过滤），文本节点的 `span` 只会挂在字形上、不会挂在图形/图片上；即使去查也永远命中不了，故省略以保持管线一致（图形 span 的反查属于反方向 `jump_from_click` 的职责）。

**练习 2**：若一个 `Span` 同时出现在第 1 页和第 3 页的文本里，`PagedDocument::find_span` 返回几个 `PagedPosition`？

参考答案：两个。`pages().iter().enumerate().filter_map(...).collect()` 会把每一页的命中都收进 `Vec`，页码分别是 1 和 3。

---

### 4.4 find_in_elem 与 HtmlPosition：沿 DOM 路径定位（HTML 后端）

#### 4.4.1 概念说明

HTML 后端的结构和分页完全不同：`HtmlDocument` 是一棵 DOM 树，节点是 `HtmlNode`（四种：`Tag` 内省标记、`Text` 文本、`Element` 元素、`Frame` 内嵌帧/SVG）。HTML 没有「页码 + 像素坐标」这种位置，它的位置是 `HtmlPosition`——一句话概括：

> **`HtmlPosition` = 「从根走到目标节点的 DOM 索引路径」+「节点内部的精确定位」**

- **DOM 索引路径** `element: EcoVec<usize>`：一组下标，从根元素一路往下选孩子就能走到目标节点。下标由 `iter_with_dom_indices` 给出，它有两个特殊规则——`Tag` 节点不占下标（取前一个节点的下标且不推进游标）、连续的 `Text` 节点共享同一个下标（因为渲染成真实 HTML 后它们会被合并成一个文本节点，无法区分）。
- **节点内部定位** `inner: Option<InnerHtmlPosition>`：要么是 `Frame(Point)`（目标节点是个内嵌帧，给帧内坐标），要么是 `Character(usize)`（目标节点是文本，给字符/codepoint 偏移）。

`find_in_elem` 就是 `HtmlDocument::find_span` 的实现：沿 DOM 树深度优先递归，边走边把经过的 `dom_index` 压进一个 `EcoVec<usize>`（即「当前路径」），一旦某个 `Text` 节点的 span 命中，就用「当前路径」造一个 `HtmlPosition` 返回；若命中 `Frame` 节点，则先用 4.3 的 `find_in_frame` 算出帧内坐标，再造一个带 `in_frame(...)` 的 `HtmlPosition`。

**一个值得注意的不对称**：`find_in_elem` 对文本命中**只产出 DOM 路径**（不带 `Character` 内部细节），即节点粒度；而反方向 `jump_from_click` 的 HTML 分支会用 `at_char(...)` 给出字符粒度。换句话说，正查给「整段文本节点」的位置，反查给「文本里的某个字符」。

#### 4.4.2 核心流程

```
find_in_elem(elem, span, current_position: &mut EcoVec<usize>) -> Vec<HtmlPosition>
  result = []
  for (child, dom_index) in elem.children.iter_with_dom_indices():
    Tag(_)        : 跳过
    Element(e)    : current_position.push(dom_index)
                    result.extend(find_in_elem(e, span, current_position))  # 递归，压/弹路径
                    current_position.pop()
    Text(_, s)    : 若 s == span → return [HtmlPosition::new(current_position.clone())]  # 命中即返回
    Frame(frame)  : 若 find_in_frame(&frame.inner, span) 命中 frame_pos：
                      path = current_position.clone() + [dom_index]
                      return [HtmlPosition::new(path).in_frame(frame_pos)]
  result
```

两个细节：

1. `Element` 分支用 `result.extend(...)` **累加**所有子树的命中；而 `Text`/`Frame` 命中是**立即 `return`**（带的是叶子最精确的答案）。由于一个 `span` 唯一对应一个源码节点，叶子命中即最终解。
2. 路径用「压栈—递归—弹栈」维护，回溯时自动恢复，无需每次克隆——只有真正命中时才 `current_position.clone()`。

#### 4.4.3 源码精读

`HtmlDocument::find_span`：[src/jump.rs:412-415](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L412-L415)——一行委托给 `find_in_elem`，从根元素 `self.root()` 开始，初始路径为空 `EcoVec`。

`find_in_elem` 本体：[src/jump.rs:441-472](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L441-L472)

```rust
fn find_in_elem(
    elem: &HtmlElement,
    span: Span,
    current_position: &mut EcoVec<usize>,
) -> Vec<HtmlPosition> {
    let mut result = Vec::new();

    for (child, dom_index) in elem.children.iter_with_dom_indices() {
        match child {
            HtmlNode::Tag(_) => {}
            HtmlNode::Element(e) => {
                current_position.push(dom_index);
                result.extend(find_in_elem(e, span, current_position));
                current_position.pop();
            }
            HtmlNode::Text(_, node_span) => {
                if *node_span == span {
                    return vec![HtmlPosition::new(current_position.clone())];
                }
            }
            HtmlNode::Frame(frame) => {
                if let Some(frame_pos) = find_in_frame(&frame.inner, span) {
                    let mut position = current_position.clone();
                    position.push(dom_index);
                    return vec![HtmlPosition::new(position).in_frame(frame_pos)];
                }
            }
        }
    }
    result
}
```

`HtmlPosition` 的定义与构造方法（跨 crate，位于 introspection 模块）：[crates/typst-library/src/introspection/position.rs:97-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/position.rs#L97-L103)

```rust
pub struct HtmlPosition {
    element: EcoVec<usize>,                 // DOM 索引路径
    inner: Option<InnerHtmlPosition>,       // 节点内部定位
}
```

两个构造器正是 `find_in_elem` 用到的：`HtmlPosition::new(element)`（[position.rs:116-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/position.rs#L116-L118)）只给路径；`.in_frame(point)`（[position.rs:138-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/position.rs#L138-L143)）在路径基础上叠加 `InnerHtmlPosition::Frame(point)`。内部枚举 `InnerHtmlPosition` 只有 `Frame(Point)` 与 `Character(usize)` 两变体（[position.rs:160-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/position.rs#L160-L167)）。

DOM 索引遍历器 `iter_with_dom_indices`（关键：`Tag` 不推进、连续 `Text` 合并同一下标）：[crates/typst-html/src/dom.rs:161-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L161-L180)。`HtmlNode::span()` 的取值规则（`Tag` 给 detached、其余给各自 span）：[crates/typst-html/src/dom.rs:119-126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L119-L126)。

#### 4.4.4 代码实践

实践目标：把「DOM 索引路径 + frame 内坐标」拆开理解 `HtmlPosition`。

操作步骤（源码阅读型，结合 u7-l1 的 HTML 测试）：

1. 看 u7-l1 出现过的 HTML 帧测试 [src/jump.rs:713-721](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L713-L721)：源码 `A math formula:\n\n#html.frame($a x + b = 0$)`，点击位置 `HtmlPosition::new(eco_vec![1, 1]).in_frame(point(27.0, 5.0))`。
2. 这个 `HtmlPosition` 的两部分分别是什么含义？
   - `element = [1, 1]`：从根出发，先取 DOM 下标 1 的孩子（通常是 `<body>`），再取其下标 1 的孩子——就是 `#html.frame(...)` 渲染出的那个 `<svg>`/`Frame` 节点。
   - `inner = Frame(point(27.0, 5.0))`：在那个内嵌帧内部、坐标 `(27pt, 5pt)` 处（正好落在数学公式里 `b` 的字形上）。
3. 反过来用本讲的 `find_in_elem`：若把光标放在源码的 `b` 上（`MathText` 叶子，`is_text` 命中），`find_in_elem` 会沿 DOM 走到那个 `Frame` 节点、调 `find_in_frame` 算出帧内坐标 `frame_pos`，然后产出 `HtmlPosition::new([1, 1]).in_frame(frame_pos)`——正是上面那个点击位置的同构形态。

需要观察的现象与预期结果：正向（cursor→output）与反向（click→source）在 `Frame` 节点上产出的 `HtmlPosition` 形态一致：都是「DOM 路径 `[1,1]` + 帧内点」。这说明两方向对内嵌帧的处理是对称的。

#### 4.4.5 小练习与答案

**练习 1**：`find_in_elem` 对 `Text` 命中只产出 `HtmlPosition::new(path)`（无 `inner`），而反方向 `jump_from_click` 的 HTML 文本分支会产出 `.at_char(i)`。这个不对称有什么实际影响？

参考答案：正查（光标→预览）只能把预览高亮到「整段文本节点」级别，无法精确到某个字符；反查（点击→源码）则能精确到字符偏移（见 u7-l1 HTML 分支用 `prefix_len` 与 `at_char` 处理 figcaption 的例子）。这是 best-effort 的取舍：正查的输入「光标」本就映射到一个文本节点，节点粒度已足够实用。

**练习 2**：为什么 `iter_with_dom_indices` 要把连续 `Text` 节点合并成同一个下标？

参考答案：因为 Typst 内部可能把一段文字拆成多个相邻 `Text` 节点（例如 figure caption 里 supplement/counter/separator 各成一个无 span 文本节点），但渲染成真实 HTML 后它们会被浏览器合并成单个文本节点，外部程序无法区分。用同一个 DOM 下标能让 `HtmlPosition` 与「外部观察到的 DOM」一一对应（详见 [src/jump.rs:108-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L108-L127) 的注释与补偿逻辑）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「数据流方向互逆」的完整对比。

**任务**：对同一段源码 `$a + b$`，分别在脑中走一遍 `jump_from_click` 与 `jump_from_cursor`，把两边的输入、输出与中间桥梁填进下表，并解释 `HtmlPosition` 的两部分各自对应流程中的哪一步。

参考步骤：

1. **反方向 `jump_from_click`（复习 u7-l1/l2）**
   - 输入：`PagedDocument`（或 `HtmlDocument`）+ 一个渲染点击点，例如 [src/jump.rs:596-598](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L596-L598) 的 `point(28.0, 14.0)`。
   - 流程：`resolve_position` → `jump_from_click_in_frame` → 在数学字形上命中 → 取 `glyph.span` → 用 `world.source(id)` + `source.find(span)` 反查到字节偏移。
   - 输出：`Some(Jump::File(main_id, 5))`（光标落在源码第 5 字节，即 `b`）。
   - 桥梁：`glyph.span`（渲染侧）→ 同一个 `Span` → 源码节点。

2. **正方向 `jump_from_cursor`（本讲）**
   - 输入：`PagedDocument` + `source` + 光标 `-3`（解析为第 5 字节，落在 `b` 上，见 [src/jump.rs:768-770](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L768-L770) 与负光标约定）。
   - 流程：`leaf_at` 取到 `MathText` 叶子（`is_text` 命中）→ `node.span()` → `PagedDocument::find_span` → `find_in_frame` 在数学字形上命中同一个 `span` → 返回字形起点坐标。
   - 输出：`vec![PagedPosition { page: 1, point: (27.51pt, 16.83pt) }]`。
   - 桥梁：源码节点 → 同一个 `Span` → `glyph.span`（渲染侧）。

3. **填表对比**

   | 维度 | jump_from_click | jump_from_cursor |
   |------|-----------------|------------------|
   | 方向 | 渲染点 → 源码 | 源码 → 渲染点 |
   | 入参 | `(world, document, position)` | `(document, source, cursor)` |
   | 出参 | `Option<Jump>`（单值） | `Vec<Position>`（多值） |
   | 节点过滤 | Text/Shape/Image/Group/Link 都查 | 只查 Text/MathText 叶子 |
   | 是否需要 world | 是（span→字节偏移要取源码） | 否（只在产物内搜坐标） |
   | Group 变换 | 用 `transform.invert()` 把点击点映进子帧 | 用 `transform` 把子帧点映回父帧 |
   | 桥梁 | 渲染 `glyph.span` ↔ 源码 `Span` | 源码 `Span` ↔ 渲染 `glyph.span` |

4. **`HtmlPosition` 的两部分归属**（若后端是 `HtmlDocument`）：
   - `element`（DOM 索引路径）：对应 `find_in_elem` 里 `current_position` 这条「压栈—递归—弹栈」走过的路径——告诉你 span 命中在 DOM 树的哪个节点。
   - `inner`（`Frame(point)` 或 `Character(i)`）：`Frame` 时由 `find_in_frame` 算出的帧内坐标（对应 4.3 的字形起点）；`Character` 仅反方向 `jump_from_click` 会填（正查不填）。
   - 两者合起来：先沿 `element` 走到那个节点，再用 `inner` 在节点内部精确定位——这就是「DOM 路径 + frame 内坐标共同定位一个源码 span 渲染位置」的全貌。

> 提示：若想实际运行，可参照测试里的 `test_cursor` 助手（[src/jump.rs:558-575](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L558-L575)）在 `crates/typst-ide` 下用 `cargo test test_jump_from_cursor` 验证断言；具体像素坐标受字体影响，**待本地验证**。

## 6. 本讲小结

- `jump_from_cursor` 是 `jump_from_click` 的**逆方向**：源码光标 → 渲染位置，返回 `Vec<Position>`（一段源码可能渲染多次）。
- 它用 `is_text` 过滤**只认 `Text`/`MathText` 叶子**，因为渲染字形只挂这些节点的 `span`，且 `find_in_frame`/`find_in_elem` 也只比对文本字形——非文本节点无可定位的渲染点。
- `JumpInDocument` 是与 `JumpFromDocument` 对称的 sealed trait，用关联类型 `type Position` 让一个泛型入口同时服务 `PagedDocument`（`PagedPosition`）和 `HtmlDocument`（`HtmlPosition`）两种后端，零成本且封闭可演进。
- `find_in_frame`（分页后端）逐页遍历帧、按 `glyph.span.0 == span` 命中字形起点；对 `Group` 施加**正向** `group.transform` 把子帧点映回父帧，与反方向的 `invert()` 互逆。
- `find_in_elem`（HTML 后端）沿 DOM 深度优先递归，用「压栈—弹栈」维护当前路径，文本命中即返回 `HtmlPosition::new(path)`、帧命中则叠加 `in_frame(frame_pos)`。
- `HtmlPosition` = DOM 索引路径 `element` + 节点内部定位 `inner`（`Frame(Point)` 或 `Character(usize)`）；正查只填路径（节点粒度），反查才填字符粒度。
- 正方向无需 `world`（只在产物内搜坐标），反方向需要 `world`（要把 `Span` 翻译回文件 id + 字节偏移）。

## 7. 下一步学习建议

- **测试体系**：本讲多次引用 `test_cursor`、负光标约定与 `pos(...)` 断言助手，这些是 u8-l1「测试体系与断言扩展」的内容。建议接着学 u8-l1，亲手仿写一个 `jump_from_cursor` 的新测试用例（例如验证一段被复用两次的文本返回两个 `PagedPosition`）。
- **集成落地**：u8-l2「集成实践与架构取舍」会讲如何把 `jump_from_click`/`jump_from_cursor` 接进一个真实 LSP——包括需要缓存哪一类编译产物（`output`）、`IdeWorld` 要实现哪些方法。本讲已经铺垫了「正方向无 `world`、反方向要 `output`/`world`」的差异，正好在 u8-l2 汇总成一张集成清单。
- **继续读源码**：重读 [src/jump.rs:343-472](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L343-L472) 这一段，把它和 u7-l2 的 `jump_from_click_in_frame`（[src/jump.rs:209-331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L209-L331)）对照阅读，体会「同一份 `Frame`/`HtmlNode` 数据结构，正反两个方向各自怎么遍历」。
