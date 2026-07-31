# 文字与字体：handle_text 与字形适配

## 1. 本讲目标

本讲精读 `src/text.rs`——它是 `typst-pdf` 里负责「把一段排好版的文字翻译成 PDF 绘制」的翻译器。读完本讲你应该能够：

1. 说清楚 `handle_text()` 把一个 Typst `TextItem` 落成 krilla 绘制调用的完整四步流程（标签 → 字体 → 填充/描边 → 绘制）。
2. 区分「字体转换」的两层缓存：`#[comemo::memoize]` 的进程级前向缓存，与 `GlobalContext` 里 `fonts_forward` / `fonts_backward` 的单文档双向缓存，并解释各自的存在理由。
3. 掌握 `PdfGlyph` 如何借助 `TransparentWrapper` + `#[repr(transparent)]` **零拷贝**地把 Typst 的 `Glyph` 切片适配成实现了 `krilla::text::Glyph` trait 的切片。
4. 认识 `location()` 如何把 Typst 的源码 `Span` 透传给 krilla，使得后续字体/字形校验错误能精确定位回 Typst 源码。

本讲承接 [u2-l7 Frame 遍历器](u2-l7-frame-walker.md)（`handle_frame` 按 `FrameItem` 分派，其中 `FrameItem::Text` 路由到 `handle_text`）与 [u2-l8 类型转换工具集](u2-l8-conversion-utilities.md)（`TransformExt`、`AbsExt`、`display_font` 等本讲会直接用到）。

## 2. 前置知识

- **`TextItem`（一段排好版的文字）**：Typst 排版产出的 `Frame` 树里，文字以 `TextItem` 形式出现。它是一段「已经完成字形塑造（shaping）」的文字：同一个 `TextItem` 内所有字形共用同一字体、同一字号、同一填充色。它内部存着一个 `Vec<Glyph>`，注意字形数 ≠ 字符数（连字 `fi` 会让字形数少于字符数）。
- **`Glyph`（单个字形）**：字形是字体里一个具体图形的索引，包含：在字体里的 `id`、水平/垂直推进量（`x_advance`/`y_advance`）、水平/垂直偏移（`x_offset`/`y_offset`）、它在原文里的字符区间 `range`、以及它对应的源码位置 `span`。其中推进量与偏移都用 `Em`（相对于字号的相对单位）表示。
- **`Em`（em 单位）**：1 em = 当前字号。一个推进量写成 `Em` 意味着「这是字号的多少倍」，要乘上实际字号 `size` 才得到 PDF 点数。例如 `Em(0.5)` 在 12pt 字号下就是 6pt。
- **变量字体（variable font）与 variations**：OpenType 变量字体有一组「坐标轴」（如字重 `wght`、字宽 `wdth`），每个轴取一个数值。`FontInstance` 已经把坐标固化（instantiate），其 `variations()` 给出最终的 `(Tag, 值)` 列表，PDF 导出时需要把这些坐标告诉 krilla，让底层还原出正确的字形。
- **`comemo::memoize`**：Typst 自研的纯函数记忆化（memoization）宏。给一个纯函数加上它后，相同入参的调用结果会被**进程级全局缓存**——不限于某一次导出，而是在整个进程生命周期内复用。
- **`bytemuck::TransparentWrapper` + `#[repr(transparent)]`**：Rust 里两个让「新类型 `struct Wrapper(Inner)` 与 `Inner` 内存布局完全相同」的机制。有了它，`&[Inner]` 与 `&[Wrapper]` 可以互相「重解释」（reinterpret），整个过程不复制任何元素、只是一次指针层面的转换。

> 如果你对 krilla 还不熟悉，只需记住：krilla 是真正「拼装 PDF 字节」的底层库，它定义了自己的 `krilla::text::Font`、`krilla::text::Glyph` trait 与 `Surface::draw_glyphs`。本讲的全部工作，就是把 Typst 侧的 `FontInstance` / `Glyph` / `TextItem`「喂」成 krilla 认识的形状。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/text.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs) | **本讲主角**，仅 149 行。定义 `handle_text`、字体转换 `convert_font` / `build_font`、以及零拷贝适配器 `PdfGlyph`。 |
| [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) | 持有 `GlobalContext`（含 `fonts_forward` / `fonts_backward` 双向字体缓存）、`FrameContext`（变换状态栈 `state()`），并在 `handle_frame` 里把 `FrameItem::Text` 分派给 `handle_text`；字体校验错误也在此用反向映射还原。 |
| [src/tags/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs) | 提供 `tags::text()` 钩子，在绘制文字前穿插发射 tagged PDF 的 `Span` 标记内容，并返回 `TagHandle` 自动收尾。 |
| [src/paint.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/paint.rs) | `convert_fill` / `convert_stroke` 把 Typst 的填充/描边翻译成 krilla 的绘制状态，`handle_text` 直接调用。 |
| [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs) | 提供 `AbsExt::to_f32`（字号换算）、`TransformExt`（变换压栈）、`display_font`（错误信息里的字体名）。 |

辅助理解 Typst 侧的数据结构定义（位于 `typst-library`）：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-library/src/text/item.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/item.rs) | 定义 `TextItem` 与 `Glyph` 两个结构体，是 `handle_text` 的输入。 |
| [crates/typst-library/src/text/font/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs) | 定义 `FontInstance`（含 `variations()`，并通过 `Deref` 复用底层 `Font` 的 `data()` / `index()`）。 |

本讲只精读 `src/text.rs`（149 行），其余文件仅作交叉佐证。

## 4. 核心概念与源码讲解

### 4.1 全景：`handle_text` 的四步流程

#### 4.1.1 概念说明

`handle_text` 是 `handle_frame` 在遇到 `FrameItem::Text` 时调用的翻译器（见 [u2-l7](u2-l7-frame-walker.md)）。它的输入是一个已经塑造好的 `TextItem`，输出是对 krilla `Surface` 的一组绘制调用。

要理解它做了什么，先看输入。Typst 侧的 `TextItem` 长这样：

[crates/typst-library/src/text/item.rs:11-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/item.rs#L11-L31) —— `TextItem` 的字段：`font`（共用字体）、`size`（字号）、`fill`（填充色）、`stroke`（可选描边）、`lang` / `region`（语言区域）、`text`（纯文本）、`glyphs`（字形列表）。注意一句注释点明「字形数可能与字符数不同，例如连字」。

而单个 `Glyph` 的内部结构是本讲第 4.3 节适配器的核心：

[crates/typst-library/src/text/item.rs:92-110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/item.rs#L92-L110) —— `Glyph` 字段：`id: u16`（字体里的字形索引）、`x_advance` / `x_offset` / `y_advance` / `y_offset`（都是 `Em`）、`range: Range<u16>`（在原文里的字符区间）、`span: (Span, u16)`（源码位置）。这些字段将在 `PdfGlyph` 里被逐个翻译。

#### 4.1.2 核心流程

`handle_text` 把一个 `TextItem` 落成绘制，严格分四步：

```
handle_text(fc, t: &TextItem, surface, gc):
  1. 发射标签：  handle = tags::text(gc, fc, surface, t)
       → 若开启 tagged PDF，在 surface 上开始一段 Span 标记内容（含语言）
       → 返回 TagHandle，drop 时自动 end_tagged()
  2. 转字体：   font  = convert_font(gc, t.font.clone())?        # 走两层缓存
  3. 转绘制状态：
       fill   = paint::convert_fill(t.fill, ...)
       stroke = t.stroke ? paint::convert_stroke(stroke, ...) : None
  4. 绘制：
       surface.push_transform(state.transform)                    # 压入累计变换
       surface.set_fill(fill); surface.set_stroke(stroke)
       surface.draw_glyphs(原点, glyphs, font, text, size, false) # krilla 真正写字
       # 离开作用域 → defer 弹出变换 / TagHandle 结束标记
```

四步的顺序不是任意的：**标签必须最先发射**（这样标记内容才能包裹住整段文字），**字体必须先于绘制转换好**（`draw_glyphs` 要用它），而**变换压栈与弹栈必须严格配对**（否则会污染后续兄弟元素的坐标系，见 [u2-l7](u2-l7-frame-walker.md) 的状态栈）。

#### 4.1.3 源码精读

[src/text.rs:17-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L17-L60) —— `handle_text` 全文。`#[typst_macros::time(name = "handle text")]` 给它打上计时标记（便于 profiling，见 [u2-l5](u2-l5-convert-orchestrator.md)）。逐行对应上面四步。

第一步，发射 tagged PDF 标签，并取出一个「临时借用 surface」的句柄：

[src/text.rs:24-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L24-L25) —— `let mut handle = tags::text(...); let surface = handle.surface();`。`tags::text` 会决定是否真正开启标记内容（见 4.1 节末与第 4.4 节），返回的 `TagHandle` 在函数末尾 `drop` 时自动调用 `surface.end_tagged()` 收尾。

第二、三步，转换字体与绘制状态：

[src/text.rs:27-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L27-L41) —— `convert_font` 拿到 krilla 字体；`paint::convert_fill` 把填充色（用 `NonZero` 填充规则、允许透明）翻译成 krilla 填充；描边是可选的，`t.stroke.as_ref()` 为 `Some` 时才转。这两段把 Typst 的颜色/描边对象交给 krilla，细节留到 [u3-l11 纯色与色彩空间](u3-l11-solid-paint-and-color-spaces.md)。

第四步，把字形切片零拷贝重解释后绘制：

[src/text.rs:44-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L44-L57) —— 第 44 行 `TransparentWrapper::wrap_slice(t.glyphs.as_slice())` 把 `&[Glyph]` 零拷贝重解释成 `&[PdfGlyph]`；第 46 行 `push_transform` 把当前累计变换压栈（来自 [u2-l7](u2-l7-frame-walker.md) 的 `fc.state().transform()`，经 `TransformExt::to_krilla` 翻译）；第 47 行 `defer(surface, |s| s.pop())` 保证函数返回时弹出变换（RAII 配对）；最后 `draw_glyphs(原点, glyphs, font, text, size, false)` 让 krilla 真正写出这段文字。

> 关于 `tags::text`：它先检查是否禁用（用户关掉 `tagged` 或正处于 tiling 内）、或父级是 artifact，若是则返回一个 `started: false` 的 `TagHandle`（drop 时什么都不做），否则才真正 `surface.start_tagged(...)`。详见 [src/tags/mod.rs:199-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L199-L228) 与 [src/tags/mod.rs:147-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/tags/mod.rs#L147-L149) 的 `disabled()`。tagged PDF 子系统本身会在 [u5-l19](u5-l19-tagged-pdf-overview.md) 详讲。

#### 4.1.4 代码实践

实践类型：源码阅读。

1. 打开 [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs)，定位到 `handle_frame` 里的 `match item`，找到 `FrameItem::Text(t) => handle_text(...)` 这一行，确认每个 item 在调用前都做了一次 `fc.push()` + `pre_concat(平移到该 item 的 point)`，调用后又 `fc.pop()`。
2. 思考：`handle_text` 第 46 行 `push_transform` 与第 47 行 `defer(..., pop)` 为什么必须成对？如果只 push 不 pop 会怎样？

**预期结果**：你会看到 [src/convert.rs:364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L364) 这一行处于「push 子状态 → 平移到 item 起点 → match 分派 → pop」的循环里（[src/convert.rs:358-385](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L358-L385)）。若 `handle_text` 内部只 push 不 pop，文字自身的平移会泄漏给紧随其后的兄弟元素（如下一个图形），导致它们被多偏移一次。`defer` 用 RAII 保证即便中途 `?` 提前返回，变换也一定被弹出。

#### 4.1.5 小练习与答案

**练习**：为什么 `handle_text` 先调用 `tags::text`（发射标签），再去 `convert_font` / `convert_fill`？能不能把顺序反过来？

**参考答案**：不能。tagged PDF 的标记内容（marked content）必须**包裹住**它要标注的可绘制内容——即 `start_tagged` 必须在绘制调用之前、`end_tagged` 必须在之后。`TagHandle` 通过 RAII（`Drop` 时 `end_tagged`）保证「先开后闭」。如果先做字体/填充转换再开标签，标签的开启就被推迟，无法完整包裹 `draw_glyphs`，结构树里这段文字就会缺失或错位。

---

### 4.2 字体转换的两层缓存：`convert_font` 与 `build_font`

#### 4.2.1 概念说明

把一个 Typst `FontInstance` 转成 krilla 能用的 `krilla::text::Font`，需要：取出字体原始字节、字体在集合里的索引、以及变量字体的坐标轴，再交给 krilla 解析。这是一笔不小的开销（要解析 OpenType 表）。而一个文档里同一种字体+同一组坐标会被成千上万个 `TextItem` 反复使用，因此必须缓存。

`typst-pdf` 用了**两层**缓存，分工不同：

- **第一层：`#[comemo::memoize]`（进程级、前向）**。`build_font` 被这个宏包裹，相同 `FontInstance` 入参的结果在整个进程内复用，跨多次导出/编译都有效。
- **第二层：`GlobalContext` 的 `fonts_forward` / `fonts_backward`（单文档、双向）**。`fonts_forward` 是 `FontInstance → krilla::text::Font`，`fonts_backward` 是反方向。其中**反向映射是 comemo 提供不了的**——comemo 只能「正向查」，无法从一个 krilla 字体反查回 Typst 字体。

反向映射为什么重要？因为 krilla 在序列化/校验时报错时，手里只有它自己的 `krilla::text::Font`，要给用户一个可读的错误信息（比如「处理 `Noto Sans` 字体失败」），就必须把这个 krilla 字体反查回 Typst 的 `FontInstance`，再取出家族名。

#### 4.2.2 核心流程

```
convert_font(gc, typst_font):
  if fonts_forward 命中 typst_font:        # 单文档前向缓存
      return 缓存的 krilla 字体
  else:
      font = build_font(typst_font)         # ← 第一层：comemo 进程级缓存
      fonts_forward.insert(typst_font, font) # 同时写两张表
      fonts_backward.insert(font, typst_font)
      return font

build_font(typst_font):            # #[comemo::memoize]
  data  = typst_font.data().clone()        # 字节（经 Deref 到底层 Font）
  index = typst_font.index()               # 集合索引（经 Deref）
  variations = [(krilla Tag, f32 值), ...]  # 把 Typst 坐标轴翻译成 krilla
  krilla::text::Font::new_variable(data, index, variations)
      → Some(font) | None(→ bail "failed to process font `…`")
```

`FontInstance` 并不直接持有 `data()` / `index()`，而是通过 `Deref` 复用底层 `Font` 的方法：

[crates/typst-library/src/text/font/mod.rs:295-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L295-L301) —— `impl Deref for FontInstance { type Target = Font; ... }`，因此 `typst_font.data()` 实际是底层 `Font::data()`（[font/mod.rs:85-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L85-L93)），`typst_font.index()` 同理。`variations()` 则是 `FontInstance` 自己的方法（[font/mod.rs:196-199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L196-L199)）。

#### 4.2.3 源码精读

[src/text.rs:62-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L62-L76) —— `convert_font`：先查 `gc.fonts_forward`，命中就返回克隆；未命中则调 `build_font`，再把结果**同时**写入 `fonts_forward` 和 `fonts_backward` 两张表。注意 `FontInstance` 与 `krilla::text::Font` 都被标注为「cheap to clone」（底层是 `Arc`），所以这里的 `.clone()` 只是增加引用计数，代价很低。

[src/text.rs:78-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L78-L104) —— `build_font`，顶上挂着 `#[comemo::memoize]`：

- 第 80-82 行：把字体字节包成 `Arc<dyn AsRef<[u8]> + Send + Sync>`——这是 krilla `new_variable` 要求的「可被多线程共享的字节源」形态。
- 第 83-88 行：遍历 `typst_font.variations().0`（一组 `(Tag, AxisValue)`），把 Typst 的 `Tag`（`tag.to_bytes()`）转成 krilla 的 `krilla::text::Tag::new(...)`，把 `AxisValue` 取出底层数值（`value.0`），拼成 krilla 期望的 `&[(Tag, f32)]`。
- 第 90-103 行：调用 `krilla::text::Font::new_variable(data, index, variations)`。解析成功返回 `Some(f)`；失败（`None`）则 `bail!` 抛出带 `display_font` 的错误信息（`` failed to process font `家族名` ``，见 [src/util.rs:200-206](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/util.rs#L200-L206)）。

`GlobalContext` 里这两张表的声明与初始化：

[src/convert.rs:278-301](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L278-L301) —— `GlobalContext` 结构体，注释明确写道「Cache the conversion between krilla and Typst fonts (forward and backward)」。两张表在 `GlobalContext::new()` 里初始化为空（[src/convert.rs:313-314](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L313-L314)），随一次 `convert()` 调用而生灭。

反向映射在错误诊断里的实际用法（详见 [u5-l18](u5-l18-error-and-validation-mapping.md)）：

[src/convert.rs:442-449](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L442-L449) —— `KrillaError::Font(f, err)` 分支用 `display_font(gc.fonts_backward.get(&f))` 把 krilla 字体 `f` 反查回 Typst 字体，拼出可读的家族名。同样的反查还出现在「字形缺失」([src/convert.rs:600-606](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L600-L606)) 与「字体许可受限」([src/convert.rs:641-644](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L641-L644)) 两类错误里——这正是 `fonts_backward` 必须存在的根本原因。

#### 4.2.4 代码实践

实践类型：源码阅读 + 推理。

1. 在 [src/text.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs) 确认 `build_font` 上方有 `#[comemo::memoize]`，并阅读 `convert_font` 把结果写入两张表的逻辑。
2. 在 [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) 搜索 `fonts_backward`，数一共有几处 `.get(&...)` 调用，它们都出现在什么类型的错误分支里。
3. 思考：既然 `build_font` 已经被 comemo 进程级缓存，`convert_font` 里为什么还要再维护一张 `fonts_forward`？

**预期结果**：`fonts_backward.get` 出现在至少三处字体相关错误（`Font`、`ContainsNotDefGlyph`、`RestrictedLicense`）。关于 `fonts_forward` 的「看似冗余」：① 它与 `fonts_backward` 是成对维护的，既然必须为反向映射存一份，前向缓存顺手也存，逻辑更统一；② 单文档的 `FxHashMap` 查询在热路径上可能比 comemo 的全局 interning 查询更直接。但**真正不可替代的是 `fonts_backward`**——comemo 只能正向查，无法支撑错误反查。`待本地验证`：可在本地用 `cargo expand` 查看 `#[comemo::memoize]` 展开后的缓存结构，确认它确实只提供正向查询。

#### 4.2.5 小练习与答案

**练习 1**：`build_font` 用 `#[comemo::memoize]`，而 `convert_font` 用 `GlobalContext` 的 HashMap。这两层缓存的生命周期和作用域有什么不同？

**参考答案**：comemo 缓存是**进程级、跨文档**的——只要进程还活着，同一个 `FontInstance` 转出的 krilla 字体就被全局复用，哪怕导出多个文档也不重复解析字体字节。`GlobalContext` 的两张表是**单次 `convert()` 调用、单文档**的——`GlobalContext::new()` 时建空，导出结束随 `gc` 一起销毁。

**练习 2**：如果删掉 `fonts_backward` 这张表（只保留 comemo 与 `fonts_forward`），哪个功能会直接坏掉？

**参考答案**：面向用户的**字体错误诊断**会失去可读性。krilla 报错时手里只有 `krilla::text::Font`，没有这张反向表就无法还原出 Typst 的 `FontInstance`，错误信息就只能写「a font」而说不出具体是哪个家族（见 `display_font` 的 `None` 分支返回 `"a font"`）。`fonts_backward` 是把 krilla 错误「翻译回 Typst 世界」的桥梁。

---

### 4.3 零拷贝字形适配：`PdfGlyph` 与 `krilla::text::Glyph`

#### 4.3.1 概念说明

krilla 绘制文字时，要求传入一个「实现了 `krilla::text::Glyph` trait 的字形切片」。但 Typst 自己的 `Glyph` 并没有、也不应该实现 krilla 的 trait（两个独立 crate，且 Typst 不该依赖 krilla）。怎么办？

最朴素的办法是：把 `&[Glyph]` 逐个克隆成一个新类型 `PdfGlyph` 的 `Vec`，再传给 krilla。但文字是 PDF 里**最常见**的内容，每段文字都要克隆整个字形向量，开销不可忽视。

`text.rs` 的做法更聪明：定义一个新类型 `PdfGlyph(Glyph)`，用 `#[repr(transparent)]` 保证它和 `Glyph` **内存布局完全相同**，再借助 `bytemuck::TransparentWrapper` 提供的 `wrap_slice`，把 `&[Glyph]` **零拷贝**地重解释成 `&[PdfGlyph]`。整个过程不复制任何一个字形，只是一次指针层面的类型转换。

要理解这种「零拷贝」为什么合法，关键是 `#[repr(transparent)]` 的保证：单字段新类型 `struct PdfGlyph(Glyph)` 在内存里与 `Glyph` 逐字节相同，因此 `&[Glyph]` 与 `&[PdfGlyph]` 在内存里也是同一段数据，安全地互相重解释。

#### 4.3.2 核心流程

`krilla::text::Glyph` trait 要求实现一组方法，`PdfGlyph` 把它们逐一委托给内部的 Typst `Glyph`（即 `self.0`）：

```
PdfGlyph(Glyph)   # #[repr(transparent)] + TransparentWrapper

impl krilla::text::Glyph for PdfGlyph:
  glyph_id()   -> GlyphId            # self.0.id (u16) → GlyphId::new(id as u32)
  text_range() -> Range<usize>       # self.0.range (Range<u16>) → Range<usize>
  x_advance(size) -> f32             # self.0.x_advance.get() * size   (Em → pt)
  x_offset(size) -> f32             # self.0.x_offset.get() * size
  y_advance(size) -> f32             # self.0.y_advance.get() * size
  y_offset(size)  -> f32             # self.0.y_offset.get() * size
  location() -> Option<Location>     # self.0.span.0.into_raw()  (Span → krilla Location)

零拷贝适配入口：
  TransparentWrapper::wrap_slice(t.glyphs.as_slice())  # &[Glyph] → &[PdfGlyph]，无复制
```

四个 advance/offset 方法的共同点是：Typst 用 `Em`（相对字号）存推进量，krilla 要的是绝对 `f32`（PDF 点），所以都要「乘以 `size`」。代码里特意绕开 `Em::at(size)`，直接 `.get() * size`——注释说明 `Em::at` 内含一次「结果是否有限」的昂贵检查，这里用裸乘法以换取热路径性能。

#### 4.3.3 源码精读

[src/text.rs:106-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L106-L108) —— `PdfGlyph` 定义：`#[derive(Debug, TransparentWrapper)]` + `#[repr(transparent)]` 的单字段结构体 `struct PdfGlyph(Glyph)`。`repr(transparent)` 是零拷贝合法性的根基。

[src/text.rs:110-148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L110-L148) —— `impl krilla::text::Glyph for PdfGlyph`，逐方法委托：

- [src/text.rs:111-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L111-L114) —— `glyph_id`：`GlyphId::new(self.0.id as u32)`，把 Typst 的 `u16` 字形索引转成 krilla 的 `GlyphId`（`#[inline(always)]` 强制内联）。
- [src/text.rs:116-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L116-L119) —— `text_range`：把 `Range<u16>` 转成 `Range<usize>`，给 krilla 用来切原文（例如生成「可复制文本」与 `ToUnicode`）。
- [src/text.rs:121-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L121-L143) —— 四个 advance/offset：都是 `self.0.x_advance.get() as f32 * size` 的模式，绕开 `Em::at` 的有限性检查。注意 `size` 由 krilla 在调用时传入（即 `TextItem::size` 换算后的点数），所以 `Em → pt` 的乘法发生在 krilla 一侧按需进行。
- [src/text.rs:145-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L145-L147) —— `location`：`Some(self.0.span.0.into_raw())`。`span` 是 `(Span, u16)`，`.0` 取出 `Span`，`.into_raw()` 转成 krilla 的 `Location`。这让 krilla 在后续报错时能把错误挂到 krilla `Location` 上，再由 `convert.rs` 的 `to_span()` 还原回 Typst `Span`（见第 4.4 节）。

零拷贝适配的入口：

[src/text.rs:44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L44) —— `let glyphs: &[PdfGlyph] = TransparentWrapper::wrap_slice(t.glyphs.as_slice());`。一行就把 Typst 的 `&[Glyph]` 重解释为 `&[PdfGlyph]`，没有分配、没有逐元素克隆。这是 `#[repr(transparent)]` + `TransparentWrapper` 带来的直接收益。

#### 4.3.4 代码实践

实践类型：源码阅读 + 推理。

1. 在 [src/text.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs) 找到 `PdfGlyph` 定义，确认它带有 `#[repr(transparent)]` 与 `#[derive(..., TransparentWrapper)]`。
2. 对照 [crates/typst-library/src/text/item.rs:92-110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/item.rs#L92-L110) 的 `Glyph` 字段，逐一核对 `PdfGlyph` 各方法读取的是哪个字段（`id` / `range` / `x_advance` / ... / `span`）。
3. 思考：如果去掉 `#[repr(transparent)]`，`TransparentWrapper::wrap_slice` 还能安全使用吗？

**预期结果**：字段一一对应：`glyph_id↔id`、`text_range↔range`、四个 advance/offset↔对应 `Em` 字段、`location↔span.0`。关于 `#[repr(transparent)]`：去掉后 `PdfGlyph` 的布局不再保证与 `Glyph` 相同（编译器可能加 padding 或重排），`wrap_slice` 的指针重解释就会变成未定义行为（UB）——这正是该属性不可省略的原因。`待本地验证`：可尝试构造一个不带 `repr(transparent)` 的对照新类型，用 `size_of`/`memoffset` 观察其布局是否仍与内部类型一致。

#### 4.3.5 小练习与答案

**练习 1**：`PdfGlyph` 用 `TransparentWrapper` + `#[repr(transparent)]` 相比「把 `&[Glyph]` 逐个克隆进 `Vec<PdfGlyph>`」带来了什么好处？

**参考答案**：**零分配、零拷贝**。由于布局完全相同，`wrap_slice` 只是一次指针层面的类型重解释，不分配新内存、不复制任何一个字形，时间复杂度 O(1)。文字是 PDF 里最高频的内容，避免每段文字都克隆整个字形向量，对导出性能意义重大。代价是必须严格遵守 `#[repr(transparent)]` 的布局约定，且 `PdfGlyph` 只能是单字段新类型。

**练习 2**：四个 advance/offset 方法为什么写成 `.get() as f32 * size`，而不是 `self.0.x_advance.at(size) as f32`？

**参考答案**：注释写明 `Em::at` 内部有一次「检查结果是否为有限值（finite）」的昂贵校验。文字推进量的计算处于极热路径（每个字形都要算），绕开这次校验、直接取 `Em` 的底层值再乘以 `size`，可以省掉每个字形一次的冗余检查，换取性能；安全性由「推进量本就应当有限」这一上游不变量保证。

---

### 4.4 落地绘制与位置信息：`draw_glyphs`、变换压栈与 `location()`

#### 4.4.1 概念说明

前面三节把「标签、字体、字形适配」都备齐了，本节看它们如何汇成最后一次 krilla 调用，以及一个容易被忽略却很关键的细节——**位置信息（location）**。

krilla 的 `Surface::draw_glyphs` 是真正把文字写进 PDF 内容流的入口。它需要：一个绘制原点、一个字形切片（实现 `krilla::text::Glyph`）、字体、原文文本、字号。但文字在页面上的**实际位置**不是 `draw_glyphs` 决定的，而是由 `surface` 当前的图形状态（变换矩阵）决定——这与 [u2-l7](u2-l7-frame-walker.md) 讲的「变换状态栈」直接对接。所以 `handle_text` 在绘制前必须 `push_transform` 把累计变换压进 krilla 的图形状态栈，绘制后必须 `pop` 弹出。

`location()` 的意义则在于**错误可追溯**：krilla 在做字体/字形校验（如 PDF/A、PDF/UA）时，若发现某段文字有问题（比如用了不允许的私有码位、字形缺失），它能通过每个字形携带的 `Location` 把错误定位到 krilla 内部的某个点，再由 `convert.rs` 的 `to_span()` 翻译回 Typst 的 `Span`，从而在源码里精确标红。

#### 4.4.2 核心流程

```
绘制前的状态准备（handle_text 第四步）：
  surface.push_transform( fc.state().transform().to_krilla() )   # Typst 变换 → krilla 图形状态栈
  defer(surface, |s| s.pop())                                    # RAII：函数返回时弹栈
  surface.set_fill(Some(fill))                                   # 填充色（来自 convert_fill）
  surface.set_stroke(stroke)                                     # 描边（可选）
  surface.draw_glyphs( 原点(0,0), glyphs, font, text, size, false )

位置信息链路（跨函数）：
  PdfGlyph.location() = self.0.span.0.into_raw()   # Typst Span → krilla Location（绘制时随字形透传）
        ↓ krilla 校验报错时带上 Location
  convert.rs::to_span(loc)                         # krilla Location → Typst Span（错误映射时还原）
        ↓
  SourceDiagnostic(span, ...)                      # 带源码 span 的 Typst 诊断
```

注意原点传的是 `(0.0, 0.0)`：文字的真实位置完全交给当前变换矩阵，`draw_glyphs` 只在「已经平移好的局部坐标系」里从原点开始排版。

#### 4.4.3 源码精读

[src/text.rs:46-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L46-L57) —— 绘制落地的核心六行：`push_transform`（压入由 `FrameContext::state()` 给出、经 `TransformExt::to_krilla` 翻译的累计变换）→ `defer` 注册弹栈 → `set_fill` / `set_stroke` 设绘制状态 → `draw_glyphs(原点, glyphs, font, text, size, false)`。`size.to_f32()` 用的是 [u2-l8](u2-l8-conversion-utilities.md) 讲过的 `AbsExt::to_f32`（`to_pt() as f32`）。

`fc.state()` 返回的是 `FrameContext` 状态栈的栈顶（[src/convert.rs:246-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L246-L248)），它记录了「从页面原点到当前文字起点的累计平移/变换」。这与 [u2-l7](u2-l7-frame-walker.md) 里讲的「每遇到一个 item 就 `push` + `pre_concat(平移)`、处理完 `pop`」一脉相承。

位置信息的透传与还原：

[src/text.rs:145-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L145-L147) —— `PdfGlyph::location()` 把 `Span` 转成 krilla `Location`。注意 `span` 字段是 `(Span, u16)` 元组，`.0` 取 `Span`，`.into_raw()` 转成 krilla 认识的 `Location` 原始值。

这条 `Location` 在 krilla 校验报错时被带回，`convert.rs` 再用 `to_span(*loc)`（如 [src/convert.rs:601](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L601) 在 `ContainsNotDefGlyph` 分支）把它还原成 Typst `Span`，于是最终错误能指到具体源码位置。这构成了「Typst Span → krilla Location（绘制时透传）→ krilla 报错 → Typst Span（错误映射时还原）」的完整闭环（错误映射的细节见 [u5-l18](u5-l18-error-and-validation-mapping.md)）。

#### 4.4.4 代码实践

实践类型：源码阅读 + 推理。

1. 在 [src/text.rs:50-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L50-L57) 确认 `draw_glyphs` 的第一个参数是 `Point::from_xy(0.0, 0.0)`（原点），并思考为什么文字位置不由这个参数决定。
2. 在 [src/convert.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs) 搜索 `to_span(`，观察它出现在哪些校验错误分支里，确认这些分支都把 krilla 的 `Location` 还原成了 Typst `Span`。
3. 思考：如果 `PdfGlyph::location()` 永远返回 `None`，会发生什么？

**预期结果**：`to_span` 出现在多个 `ValidationError` 分支（如 `ContainsNotDefGlyph`、`NoCodepointMapping`、`InvalidCodepointMapping` 等）。若 `location()` 恒返回 `None`，krilla 校验错误就不会携带位置信息，`to_span` 拿到的 `loc` 为 `None`，错误就只能在文档级（而非具体源码行）报告——读者会看到「PDF 里某处文字有问题」，但无法定位到源码。`location()` 正是让字形级错误能精确标红的关键。`待本地验证`：可在本地构造一个 PDF/UA 导出、故意使用私有码位字符，观察报错是否指向源码具体位置。

#### 4.4.5 小练习与答案

**练习**：`draw_glyphs` 的原点为什么传 `(0.0, 0.0)`？文字在页面上的真实位置由谁决定？

**参考答案**：因为文字的真实位置已经被「当前变换矩阵」编码了。`handle_frame` 在分派每个 item 前都 `pre_concat(平移到 item 起点)`（[src/convert.rs:359-360](https://github.com/typst/typst/blob/146a58329a30f6cf38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L359-L360)），`handle_text` 又把这个累计变换 `push_transform` 压进 krilla 图形状态栈。所以 `draw_glyphs` 只需在「已经平移好的局部坐标系」里从原点开始排版即可，无需再传绝对坐标。这也让文字与图形/图像共用同一套变换机制（见 [u2-l7](u2-l7-frame-walker.md)）。

---

## 5. 综合实践

本任务把本讲四块内容（四步流程、字体两层缓存、零拷贝适配、位置信息）串起来，全部为**源码阅读型实践**（无需运行）。

### 任务一：画出 `handle_text` 的数据流图

以 [src/text.rs:17-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L17-L60) 为准，画一张从 `TextItem` 到 `surface.draw_glyphs` 的数据流图（箭头图或伪代码均可），标注：

- 每一步的输入与输出（`tags::text` → `TagHandle`；`convert_font` → krilla 字体；`convert_fill`/`convert_stroke` → 绘制状态；`wrap_slice` → `&[PdfGlyph]`）。
- 哪些 Typst 值被「翻译」成了 krilla 值（变换、字号、填充、字体、字形），各自用到了哪个模块（`util`、`paint`、`text` 自身）。

### 任务二：解释两层字体缓存的分工

写一段 150 字左右的说明，要点：

- `#[comemo::memoize]` 缓存的作用域（进程级、跨文档）与方向（仅前向）。
- `GlobalContext` 的 `fonts_forward` / `fonts_backward` 的作用域（单文档）与方向（双向）。
- 为什么 `fonts_backward` 不可替代（用 [src/convert.rs:442-449](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L442-L449) 的错误反查作为论据）。

### 任务三：跟踪 `Span` 的往返旅程

从 [src/text.rs:145-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/text.rs#L145-L147) 出发，描述一个 Typst `Span` 是如何「随字形透传给 krilla → krilla 校验报错时带回 → `to_span` 还原成 `Span`」完成往返的，并说明若没有这一机制，字形级错误（如私有码位）会退化成什么样的报错。

**完成标志**：你能不看源码，复述出 `handle_text` 的四步、两层字体缓存各自的存在理由、`PdfGlyph` 零拷贝适配的原理与必备属性，以及 `location()` 在错误追溯中的作用。

## 6. 本讲小结

- `handle_text` 把一个 `TextItem` 落成 krilla 绘制，严格四步：`tags::text` 发射标签 → `convert_font` 转字体 → `convert_fill`/`convert_stroke` 转绘制状态 → `push_transform` + `draw_glyphs` 绘制；其中标签必须最先发射以包裹可绘制内容，变换压栈与弹栈必须 RAII 配对。
- 字体转换有两层缓存：`build_font` 上的 `#[comemo::memoize]` 提供进程级、跨文档的前向缓存（避免反复解析字体字节）；`GlobalContext` 的 `fonts_forward` / `fonts_backward` 提供单文档双向缓存，其中反向映射是 comemo 给不了的，专门用于把 krilla 字体错误反查回 Typst 字体、拼出可读家族名。
- `build_font` 把 Typst 的字体字节、集合索引、变量坐标轴（`Tag` + 数值）交给 `krilla::text::Font::new_variable`；解析失败时用 `display_font` 报出 `` failed to process font `…` ``。
- `PdfGlyph` 用 `#[repr(transparent)]` + `bytemuck::TransparentWrapper` 把 `&[Glyph]` 零拷贝重解释成 `&[PdfGlyph]`，使其满足 `krilla::text::Glyph` trait 而不复制任何字形；各方法把 `id`/`range`/四个 `Em` 推进量/`span` 逐一委托并换算（`Em → pt` 绕开 `Em::at` 的有限性检查以提速）。
- `PdfGlyph::location()` 把 `Span` 透传为 krilla `Location`，配合 `convert.rs::to_span` 构成「Span → Location → 报错 → Span」闭环，使字形级校验错误能精确定位回 Typst 源码。

## 7. 下一步学习建议

本讲把「文字如何变成 PDF 绘制」讲清楚了，接下来可以从几个方向继续：

- **[u3-l10 图形与几何](u3-l10-shapes-and-geometry.md)**：看与 `handle_text` 并列的 `handle_shape` 如何把 `Geometry` 落成 krilla 路径，理解 `FrameItem` 的另一个主要分派分支。
- **[u3-l11 纯色与色彩空间](u3-l11-solid-paint-and-color-spaces.md)**：深入本讲调用的 `convert_fill` / `convert_stroke`，看 Typst 颜色如何映射到 krilla 的填充/描边与色彩空间。
- **[u3-l13 图像](u3-l13-images-raster-svg-pdf.md)**：对比另一种高频内容（图像）的翻译器 `handle_image`，体会 `FrameItem` 各分派的共性（变换压栈、标签钩子）与差异。
- **[u5-l18 错误处理与校验结果映射](u5-l18-error-and-validation-mapping.md)**：看本讲的 `fonts_backward` 反查与 `location()`/`to_span` 如何汇成最终的、带源码 span 与 hint 的 `SourceDiagnostic`。
- 若想了解 tagged PDF 标签是如何在 `tags::text` 里穿插发射的，可跳到 **[u5-l19 tagged PDF 概览](u5-l19-tagged-pdf-overview.md)**。
