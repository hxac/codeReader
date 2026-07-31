# 文本整形 shaping

## 1. 本讲目标

本讲是 Typst 行内（段落）布局单元的第四篇，深入四段管线（collect → prepare → **linebreak** → finalize）中「整形」这一环节的内部实现。

学完后你应当能够：

- 说清楚**整形（shaping）**到底做了什么：从一串字符到一组带字形的 `ShapedGlyph`，中间发生了哪些事。
- 看懂 `ShapedText` / `Glyphs` / `ShapedGlyph` 三个核心数据结构的字段含义，以及它们如何同时服务于「测量宽度」「按需 reshape」「最终生成 Frame」三件事。
- 理解**字体回退**机制：当一个字符在首选字体里缺失（tofu）时，`get_font_and_covers` 如何挑出下一个字体并递归整形。
- 理解**按需重排（substring reshaping）**：为什么段落只在 `prepare` 阶段整形一次，断行时却还能「reshape」出正确的子串字形，以及 `safe-to-break` 标志如何决定是否需要真正重新整形。
- 理解**软连字符（shy, U+00AD）**如何在不占宽度的前提下，在断行后变成可见的 `-`。

## 2. 前置知识

在进入本讲前，你需要先掌握上一篇 u5-l2 建立的认知，并了解几个术语：

- **段落四段管线**：`collect` 把异构 `&[Pair]` 拍平成 `(String, Vec<Segment>, SpanMapper)`，`prepare` 做 BiDi 并整形得到 `Preparation`，`linebreak` 断行，`finalize` 提交成 Frame（见 u5-l1 / u5-l2）。本讲的主角就是 `prepare` 里被调用的整形代码。
- **Segment 与 Item**：`Segment` 是「尚未整形」的原料（一段待整形文本，或一个已就绪的非文本 item）；`Item` 是「整形后」的产物之一（`Item::Text(ShapedText)`、`Item::Absolute`、`Item::Frame` 等）。
- **字形（glyph）**：字体里以字形索引（glyph id）标识的一张矢量图。一个字符不一定对应一个字形（如「fi」合字、阿拉伯文连写），一个字形也可能由多个字符合成。
- **rustybuzz**：HarfBuzz 整形引擎的 Rust 移植。它吃一段文本 + 字体 + 方向/脚本/语言/特性，吐出「每个字形的位置（advance / offset）」。Typst 不自己写字形选择算法，而是把脏活交给 rustybuzz。
- **em 单位**：相对单位，1em = 当前字号。字形的位置信息普遍用 em 存储，渲染时按 \(\text{绝对长度} = \text{em 值} \times \text{字号}\) 换算。这样同一组字形在不同字号下无需重新整形。
- **cluster**：rustybuzz 给每个字形标记的「源文本字节位置」。一个 cluster 可能包含多个字形（如合字），这些字形共享同一段文本范围，是「不可分割的最小单位」。

如果你对「为什么 BiDi 必须在全段文本上做」还有疑问，请先回顾 u5-l2，本讲不再重复。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 |
| --- | --- |
| `src/inline/shaping.rs` | **主角**。定义整形产物 `ShapedText`/`Glyphs`/`ShapedGlyph`，以及入口 `shape_range`、内部 `shape`/`shape_segment`、按需 `reshape`、`hyphen`、字体回退 `get_font_and_covers`、可缓存 `create_shape_plan` 等。 |
| `src/inline/prepare.rs` | 上层调用方。在 `prepare` 中对每个文本 `Segment` 调 `shape_range`，把结果装进 `Preparation.items`；还负责 `add_cjk_latin_spacing`。 |
| `src/inline/line.rs` | 消费侧。断行构造每一行时，遇到被断点切开的文本 item 调 `reshape`；行首/行尾按需追加 `ShapedText::hyphen`。 |
| `src/inline/collect.rs` | 定义 `Item`（整形产物之一）与 `Segment`（整形原料）。 |
| `src/inline/box.rs` | inline 里的 `#box` 排版 `layout_box`，作为「非文本 item 如何产生 `Item::Frame`」的对照样例。 |

一句话串联：`collect` 产出 `Segment` → `prepare` 调 `shape_range` 把文本 `Segment` 整形成 `Item::Text(ShapedText)` → `linebreak`/`line` 按断点 `reshape` 子串并可能加 `hyphen` → `ShapedText::build` 把字形落成 `FrameItem::Text`。

## 4. 核心概念与源码讲解

本讲拆为五个最小模块：

- **4.1** 整形产物：`ShapedText` / `Glyphs` / `ShapedGlyph`
- **4.2** 整形入口：`shape_range` 的分段与 `shape` 主流程（含 rustybuzz plan）
- **4.3** 字体回退：`get_font_and_covers` 与 `SharedShapingContext`
- **4.4** 按需重排：`reshape` 与 `safe-to-break`
- **4.5** 软连字符：`shy` 如何在断行后变成 `hyphen`

### 4.1 整形产物：ShapedText / Glyphs / ShapedGlyph

#### 4.1.1 概念说明

整形的结果不是一个「字符串」，而是一个带丰富元数据的结构 `ShapedText`。它必须同时回答三个问题：

1. **这条文本有多宽？**（供断行测量）
2. **被断点切开后，能不能直接复用已有字形，还是必须重新整形？**（供 reshape）
3. **最终渲染时，每个字形画在哪、用什么字体/颜色/装饰？**（供 `build` 生成 Frame）

为此，`ShapedText` 把「样式链」「方向/语言」「原始文本借用」和「字形序列」都打包在一起。注意 `text` 字段是 `&'a str`——它**借用**整段段落文本的一个切片，而非自己拥有字符串。这一点是「按需 reshape 能廉价复用」的关键（见 4.4）。

字形序列被进一步包成 `Glyphs`，它用写时复制（`Cow`）+ `kept` 区间实现了「裁掉行尾空白字形但不丢弃」的需求：行尾的空格不影响布局宽度（裁掉），但导出 PDF 时仍要保留以保持文本可选取。

单个字形 `ShapedGlyph` 记录了：来自哪个字体、字形 id、advance/offset、字号、所属 cluster 的字节范围、是否 `safe_to_break`、首字符、脚本、是否可两端对齐等。

#### 4.1.2 核心流程

一个 `ShapedText` 的生命周期：

```text
shape() 一次性构造
   │  字段：base, text(借用), dir, lang, region, styles, variant, glyphs
   ▼
[测量阶段] width() / measure() / justifiables() / stretchability() ...
   │  只读 glyphs，不分配
   ▼
[reshape 阶段] reshape() 可能直接切片复用，否则重新 shape
   ▼
[提交阶段] build() 把 glyphs 转成 FrameItem::Text
```

em 到绝对长度的换算贯穿所有测量：

\[
\text{宽度} = \sum_{g \in \text{glyphs}} g.\text{x\_advance} \times g.\text{size}
\]

注意每个字形自带 `size`，因为字体回退可能让同一段文本里不同字形用不同字号（例如合成的上下标）。

#### 4.1.3 源码精读

`ShapedText` 结构定义在 [src/inline/shaping.rs:38-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L38-L56)，关键点是 `text: &'a str` 借用、`styles: StyleChain<'a>` 也借用，使整个结构克隆廉价且可哈希（comemo 缓存友好）。

`Glyphs` 的「裁剪但不丢弃」设计见 [src/inline/shaping.rs:58-129](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L58-L129)。核心是 `kept: Range` 标记未裁剪区间：

- `Deref` 只暴露 `kept` 范围内的字形（绝大多数代码只该看到这些）。
- `all()` 暴露全部字形（含被裁的行尾空白，仅供 PDF 导出用）。
- `trim()` 通过移动 `kept` 端点来裁剪，**不真正删除**底层数据。

单个字形的字段见 [src/inline/shaping.rs:131-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L131-L167)，其中 `range`（cluster 字节范围）与 `safe_to_break` 是 4.4 节 reshape 的依据。

宽度计算极简，见 [src/inline/shaping.rs:468-470](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L468-L470)：把每个字形的 `x_advance` 按各自 `size` 换算成绝对长度求和。

最终提交 `build` 见 [src/inline/shaping.rs:325-464](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L325-L464)。它按 `(font, y_offset, size)` 把字形分组，每组生成一个 `TextItem` 推入 Frame，同时应用两端对齐（`justification_ratio` / `extra_justification`）和行内装饰（下划线等，详见 u5-l5）。

#### 4.1.4 代码实践

**实践目标**：确认「行尾空白字形被裁剪但不删除」这一行为，并理解 `Deref` 与 `all()` 的差异。

**操作步骤**：

1. 打开 [src/inline/shaping.rs:106-120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L106-L120)，阅读 `trim` 与 `all`、`is_fully_empty` 的实现。
2. 在 [src/inline/shaping.rs:122-129](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L122-L129) 的 `Deref` 实现处确认：它返回的是 `&self.inner[self.kept]`，即只暴露未裁剪部分。
3. 追踪 `trim` 的调用点：在 `line.rs` 的 `collect_range` 里（[src/inline/line.rs:326-332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L326-L332)）会 `shaped.glyphs.trim(|glyph| glyph.range.start >= trim.layout)`。
4. 在 `build` 里（[src/inline/shaping.rs:366-397](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L366-L397)）确认：被裁剪字形（`kept` 不含 `i`）的 `x_advance`/`x_offset` 被置零，但仍被遍历并写入 `TextItem`。

**需要观察的现象**：行尾空格在布局上不计宽度（`width()` 只 sum `glyphs` 的 deref，即 kept 部分），但 PDF 里仍能被选中复制——这正是 `Glyphs` 双层设计的目的。

**预期结果**：你能用自己的话解释「为什么 Typst 不直接 `retain` 删掉行尾空格字形，而是用 `kept` 区间遮蔽」。（提示：导出时还要保留。）

> 说明：以上为源码阅读型实践，未实际运行命令。

#### 4.1.5 小练习与答案

**练习 1**：`ShapedText` 为什么要同时存 `base`（在整段中的字节起点）和 `text`（借用切片）两个看起来重叠的信息？

**答案**：`text` 是「这一 run 自己的字符串」，方便整形与渲染；`base` 是「该 run 在整段段落文本中的绝对字节偏移」，用于把字形的 `range` 换算回全局坐标，供断行（按全局字节位置切分）和 `reshape`（按全局 range 切片）使用。二者职责不同。

**练习 2**：`Glyphs::to_mut` 的文档警告「在可能被借用的字形上调用会带来约 10% 的性能损失」。结合 `Cow`，请解释何时是借用、何时是 owned。

**答案**：`reshape` 复用字形时用 `Glyphs::from_slice`（`Cow::Borrowed`，零拷贝）；初次整形 `shape()` 用 `Glyphs::from_vec`（`Cow::Owned`）。`to_mut` 在 Borrowed 时会触发整段克隆（`Cow::to_mut`），所以 `add_cjk_latin_spacing` 等需要原地改字形的代码注释里特意说明「只在 prepare 阶段调用，此时 Cow 一定是 Owned」。

### 4.2 整形入口：shape_range 的分段与 shape 主流程

#### 4.2.1 概念说明

整形的公开入口是 `shape_range`，但它**不是**把整段文本一次性扔给 rustybuzz。它先按两个维度把文本切成若干 **shape run**（一次整形的最小单位）：

1. **BiDi embedding level**（来自 u5-l2 的 BiDi 分析）：level 变化意味着方向变化，必须分开整形。
2. **脚本（script）**：拉丁、汉字、阿拉伯等不同脚本通常需要不同整形行为。`is_compatible` 判定两个脚本能否合并进同一 run。

每个 run 方向一致、脚本相近，于是可以用单一方向、单一（推断的）脚本调用 rustybuzz。切分后，每个 run 调用私有 `shape()` 完成实际整形。

`shape()` 内部对一组字形做三步后处理：

- `track_and_space`：应用 `text.tracking`（字间距）与 `text.spacing`（空格宽度比例），并把不间断空格统一成普通空格宽度。
- `calculate_adjustability`：计算每个字形的可拉伸/可压缩量（两端对齐用），并做 CJK 标点连续压缩（中文排版要求里的标点挤压）。

#### 4.2.2 核心流程

```text
shape_range(items, text, bidi, range, styles)
  │  遍历 range 内每个字符边界
  │  按 BiDi level 与 script 切 run
  ▼
对每个 run 调 shape(range, level → dir)
  │
  ├─ 构造 ShapingContext（world/size/variant/features/variations/fallback/dir...）
  ├─ shape_segment(...)  ← 真正调 rustybuzz，含字体回退（4.3）
  ├─ track_and_space(...) ← 应用 tracking/spacing
  └─ calculate_adjustability(...) ← 算可伸缩性 + CJK 标点压缩
  │
  ▼
items.push((range, Item::Text(shaped)))
```

切 run 的判据（伪代码）：

```text
for 每个字符边界 i:
    level = bidi.levels[i]
    script = text[i..].首字符脚本      # 用户显式 set script 时记为 Unknown
    if level 变了 或 脚本不兼容:
        收尾上一个 run，开启新 run
```

脚本「兼容」的规则见 `is_compatible`：只要有一方是通用脚本（Unknown/Common/Inherited）就兼容，否则必须相等。这让「拉丁文里夹一个数字」不会被无谓切开，而「拉丁夹汉字」会切开。

#### 4.2.3 源码精读

`shape_range` 的切分循环见 [src/inline/shaping.rs:721-772](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L721-L772)。注意第 752-757 行：当用户显式设置了 `text.script` 时，脚本被记为 `Script::Unknown`，从而**只按 level 切**而不按脚本切——因为脚本已被强制固定，不需要按字符推断切分。

`shape` 主流程见 [src/inline/shaping.rs:785-832](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L785-L832)。它先组装 `ShapingContext`（[src/inline/shaping.rs:835-847](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L835-L847)），再依次调 `shape_segment` → `track_and_space` → `calculate_adjustability`，最后两行是 debug 断言，确保字形 range 落在文本范围内且按方向单调。

`track_and_space` 见 [src/inline/shaping.rs:1270-1295](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1270-L1295)：tracking 加在每个 cluster 的末尾（用 `peek` 判断「下一个字形是否属于同一 cluster」），spacing 只作用于空格字形。

`calculate_adjustability` 见 [src/inline/shaping.rs:1299-1337](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1299-L1337)，其中第二段循环实现了中文/日文排版要求里的「连续标点挤压」（相邻两个 CJK 标点各让出一半宽度）。

rustybuzz 的整形计划（shape plan）被独立缓存：`create_shape_plan` 见 [src/inline/shaping.rs:1217-1233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1217-L1233)，带 `#[comemo::memoize]`。它的注释说得很清楚：plan 只依赖字体、方向、脚本、语言、特性，**与文本无关**，因此可跨不同文本复用——这是整形性能的关键优化之一。

#### 4.2.4 代码实践

**实践目标**：理解 `shape_range` 如何按 BiDi level 与 script 把一段混合文本切成多个 run，并验证 `create_shape_plan` 的可缓存性。

**操作步骤**：

1. 在 [src/inline/shaping.rs:746-769](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L746-L769) 标注：`level != prev_level` 与 `!is_compatible(curr_script, prev_script)` 两个触发切 run 的条件。
2. 想象文本 `Hello 世界`（拉丁 + 汉字），手动推断它会被切成几个 run、各自方向与脚本是什么。
3. 在 [src/inline/shaping.rs:1217-1233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1217-L1233) 确认 `create_shape_plan` 被 `#[comemo::memoize]` 标注，参数里**没有**文本本身。
4. （可选，待本地验证）在 `create_shape_plan` 入口临时加一行 `eprintln!("plan for {:?}", font.info())`，编译运行 `cargo test`，观察同一字体在不同文本上是否只打印一次。

**需要观察的现象**：同一字体的 plan 只构造一次，后续命中缓存。

**预期结果**：你能解释「为什么 plan 能与文本解耦」——因为 plan 只描述「这个字体在这种方向/脚本/语言/特性下，该启用哪些 OpenType 查表（lookup）」，与具体要整形哪些字符无关。

> 说明：第 4 步涉及修改源码并运行，属可选；若不便修改源码，完成 1-3 步即可。切勿提交对源码的临时改动。

#### 4.2.5 小练习与答案

**练习 1**：`is_compatible(Latin, Common)` 返回什么？为什么数字（Common 脚本）能和拉丁文挤在同一 run？

**答案**：返回 `true`，因为 `is_generic_script(Common)` 为真。数字、标点等通用脚本字符没有强烈的整形归属，可以跟随相邻的强脚本一起整形，避免无谓切分。

**练习 2**：如果用户写 `#set text(script: "Latn")` 强制脚本，`shape_range` 的切分行为有何变化？

**答案**：见 [src/inline/shaping.rs:752-757](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L752-L757)，`Smart::Custom(_)` 分支把 `curr_script` 恒置为 `Script::Unknown`，于是 `is_compatible` 恒真，**只按 BiDi level 切**，不再按字符脚本切 run。

### 4.3 字体回退：get_font_and_covers 与 SharedShapingContext

#### 4.3.1 概念说明

整形必须有一个具体字体。但首选字体不一定包含所有字符（比如正文是拉丁字体，却出现了汉字）。这时需要**字体回退**：为缺失的字符另选一个字体，把文本切成「首选字体能覆盖的部分」+「需要回退的部分」，分别整形。

Typst 的回退有两层：

1. **样式链里的字体家族列表**：`#set text(font: ("A", "B", "C"))` 给出的有序列表，逐个尝试。
2. **全局 fallback**：当列表耗尽仍找不到字形，且 `text.fallback` 开启（默认开）时，用字体簿（font book）的 `select_fallback` 按字符猜一个字体。

关键机制是 **coverage（覆盖正则）**：一个 `FontFamily` 可以带一个 `covers` 正则，声明「我只负责匹配这些字符」。若某字符不被覆盖，则视为在该家族里「缺失」（即使字形 id 非 0），要继续回退。这让用户能精确指定「 emoji 用 X 字体、汉字用 Y 字体」。

`SharedShapingContext` 是一个把整形所需上下文（world、已用字体、变体、字号、变体轴等）抽象出来的 trait，让 `get_font_and_covers` 这类通用逻辑不必绑定具体 `ShapingContext`，便于复用与测试。

#### 4.3.2 核心流程

`shape_segment` 对一段文本的回退整形：

```text
shape_segment(ctx, base, text, families)
  │
  ├─ get_font_and_covers(ctx, text, families, tofu_cb)
  │     ├─ 遍历 families：book.select → instantiate → 去掉已用(ctx.used)
  │     │     命中则记 covers，跳出
  │     ├─ 都没命中且 fallback 开：book.select_fallback(...)
  │     └─ 仍没有：用首个已用字体 shape_tofus（画 .notdef 占位），返回 None
  │
  ├─ 拿到 (font, covers) 后：
  │     ┌─ 填 rustybuzz buffer（文本/语言/脚本/方向）
  │     ├─ create_shape_plan(font, dir, script, lang, features)  ← 可缓存
  │     └─ rustybuzz::shape_with_plan(...)
  │
  └─ 遍历整形结果的每个 cluster：
        ├─ glyph_id != 0 且被 covers 覆盖 → 正常字形，入栈
        └─ 否则（tofu / 未覆盖）：
              ┌─ 找出连续的 tofu 序列 [k..i]
              ├─ 弹出已入栈的半个 cluster（保证 cluster 完整）
              └─ 递归 shape_segment(剩余 families, 该子串)  ← 换下一个字体再整
```

也就是说，回退是**递归**的：当前字体处理不了的子串，用 `families.clone()` 的剩余部分再调一次 `shape_segment`。`ctx.used` 记录「无覆盖限制且已用过的字体」，避免无限循环或重复选用。

#### 4.3.3 源码精读

`get_font_and_covers` 见 [src/inline/shaping.rs:901-952](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L901-L952)。三段式：

- L916-926：遍历样式链字体家族，`book.select` 选字体、`instantiate` 实例化、`.filter(|font| !ctx.used().contains(font))` 排除已用；命中即记 `covers` 跳出。
- L929-936：列表耗尽且 `fallback` 开启时，调 `book.select_fallback`。
- L939-944：仍无字体时，用 `ctx.used().first()` 调 `shape_tofus` 回调画占位字形，返回 `None`。
- L947-949：**无 covers 限制**的字体一旦用过就 push 进 `used`，防止下次再用（有 covers 的可反复用，因为它只负责特定字符）。

`SharedShapingContext` trait 见 [src/inline/shaping.rs:849-869](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L849-L869)，`ShapingContext` 对它的实现见 [src/inline/shaping.rs:871-899](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L871-L899)。它把「world、used、first、variant、fallback、size、variations」抽象成方法，`get_font_and_covers` 因此写成泛型 `<C: SharedShapingContext>`。

递归回退的主体在 `shape_segment`：整段函数见 [src/inline/shaping.rs:955-1157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L955-L1157)。其中填 buffer 与调 rustybuzz 在 [src/inline/shaping.rs:977-1026](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L977-L1026)；逐 cluster 分流（正常 vs tofu 递归）在 [src/inline/shaping.rs:1043-1154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1043-L1154)，递归调用点是 [src/inline/shaping.rs:1150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1150)。

注意 [src/inline/shaping.rs:997](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L997) 设置了 `BufferFlags::REMOVE_DEFAULT_IGNORABLES`：注释解释 HarfBuzz 默认会给「默认可忽略字符」生成零宽字形（GUI 光标用），但 Typst 不需要、且会损害文本提取，于是显式移除。

`shape_tofus` 见 [src/inline/shaping.rs:1236-1267](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1236-L1267)：找不到任何字体时，用 `.notdef` 字形（id=0）按字符宽度占位。

#### 4.3.4 代码实践

**实践目标**：追踪一个含汉字的拉丁文段落如何触发字体回退，并解释 `used` 列表如何防止回退死循环。

**操作步骤**：

1. 假设样式链为 `#set text(font: ("Linux Libertine", "Noto Sans CJK SC"))`，文本是 `Typst 你好`。
2. 在 [src/inline/shaping.rs:916-926](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L916-L926) 推演：第一个 run（`Typst `，拉丁）选中 Linux Libertine；因为它无 covers，会被 push 进 `used`。
3. 推演汉字 run（`你好`）：Linux Libertine 不含汉字 → glyph_id 为 0 或未被覆盖 → 落入 [src/inline/shaping.rs:1110-1151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1110-L1151) 的 tofu 分支 → 递归 `shape_segment` 用剩余 families（含 Noto Sans CJK SC）再整。
4. 在 [src/inline/shaping.rs:921](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L921) 的 `.filter(|font| !ctx.used().contains(font))` 处确认：递归时 Linux Libertine 已在 `used` 里，不会被重复选中。
5. （可选，待本地验证）写一个最小 Typst 文档 `#set text(font: "Linux Libertine"); Typst 你好`，用 `typst compile` 生成 PDF，观察「你好」是否用了回退字体（可用 PDF 阅读器查看嵌入字体列表）。

**需要观察的现象**：汉字用回退字体渲染，而非 `.notdef` 方块。

**预期结果**：你能画出 `Typst 你好` 在 `shape_segment` 里的两次递归调用，以及 `ctx.used` 在每次调用前后的内容。

> 说明：前 4 步为源码阅读型推演，第 5 步为可选的本地编译验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么「带 covers 的字体」不被 push 进 `used`，而「无 covers 的字体」会被 push？

**答案**：见 [src/inline/shaping.rs:947-949](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L947-L949)。带 covers 的字体只负责匹配特定字符（如只管 emoji），对其他字符天然「不覆盖」，可以安全反复使用；无 covers 的字体声称负责一切，一旦用它整形过某子串仍出现 tofu，说明它确实不含这些字，再选也是浪费，故标记为已用避免重复。

**练习 2**：如果 `text.fallback` 设为 `false` 且首选字体缺字，会发生什么？

**答案**：`get_font_and_covers` 的 L929 分支条件 `ctx.fallback()` 为假，跳过全局回退；若样式链里也没有能覆盖的字体，落入 L939-944 用 `shape_tofus` 画 `.notdef` 占位字形。

### 4.4 按需重排：reshape 与 safe-to-break

#### 4.4.1 概念说明

段落只在 `prepare` 阶段整形**一次**，得到一组覆盖整段的 `ShapedText`。但断行会在任意字节位置切开文本——如果切点恰好落在一个「跨字符字形」中间（如合字、阿拉伯连写），直接切字形会得到错误的形状。

rustybuzz 为每个字形标记了 `safe_to_break`：在该字形之前切开，等价于把左右两段分别整形。因此 Typst 的 `reshape` 策略是：

- 若请求的子串两端都落在 `safe_to_break` 的边界上 → **直接切片复用**已有字形，零成本。
- 否则 → 把该子串重新调 `shape` 整形一遍，保证正确。

这就是「按需重排（substring reshaping）」：绝大多数断点都能复用，只有少数危险断点才真正重整。这正是整彞性能的另一关键。

#### 4.4.2 核心流程

```text
reshape(engine, text_range)  # text_range 是全局字节范围
  │
  ├─ slice_safe_to_break(text_range)?
  │     ├─ 找左边界：find_safe_to_break(start) → 字形下标 left
  │     ├─ 找右边界：find_safe_to_break(end)   → 字形下标 right
  │     └─ 返回 &glyphs[left..right]（借用，零拷贝）
  │
  ├─ 命中（两端都 safe）：
  │     返回 ShapedText { glyphs: Glyphs::from_slice(借用切片), ... }   # 不重新整形
  │
  └─ 未命中（某端 unsafe）：
        返回 shape(engine, text_range.start, 子串, ...)   # 重新整形
```

`find_safe_to_break` 用二分查找定位「最靠近请求位置、且 safe_to_break 为真」的字形边界，并处理 RTL（右边界/左边界互换）与 `\n` 等边界特例。

#### 4.4.3 源码精读

`reshape` 见 [src/inline/shaping.rs:546-572](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L546-L572)。关键三分支：

- L548：先试 `slice_safe_to_break`。
- L549-560：命中则用 `Glyphs::from_slice`（`Cow::Borrowed`，零拷贝）复用字形，debug 模式下还断言字形 range 与子串一致（[src/inline/shaping.rs:1346-1355](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1346-L1355) 的 `assert_all_glyphs_in_range`）。
- L561-571：未命中则调 `shape` 重新整形。

`slice_safe_to_break` 见 [src/inline/shaping.rs:642-651](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L642-L651)：RTL 时交换 start/end，再用 `find_safe_to_break` 找左右字形下标。

`find_safe_to_break` 见 [src/inline/shaping.rs:655-710](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L655-L710)。要点：

- L660-664：处理文本首尾的边界特例。
- L667-670：用 `binary_search_by` 按 `range.start` 二分查找目标 text_index（RTL 时反转比较）。
- L674-692：`Err` 分支专门处理「在 `\n` 前切开」的特例（`\n` 无字形但 safe）。
- L709：最终返回 `glyphs[idx].safe_to_break.then_some(...)`，unsafe 时返回 `None`，促成上层重新整形。

**消费侧**：断行构造每一行时，`collect_range` 判断 item 是否被断点切开（`split`），若是则调 `shaped.reshape(engine, sliced)`，见 [src/inline/line.rs:300-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L300-L323)。未被切开（`split == false`）的整段 item 直接复用，连 reshape 都不调。

#### 4.4.4 代码实践

**实践目标**：说明同一 run 在不同断点被 reshape 时如何复用已整形结果，以及 `safe_to_break` 如何决定复用 vs 重整。这是本讲的核心实践任务。

**操作步骤**：

1. 打开 [src/inline/shaping.rs:546-572](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L546-L572) 的 `reshape`，确认它先试切片、失败才 `shape`。
2. 打开消费侧 [src/inline/line.rs:300-323](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L300-L323)，确认 `split` 的判定：`subrange.start < sliced.start || sliced.end < subrange.end`，即断点落在 item 内部时才需要 reshape；整段复用时 `split == false`。
3. 在 [src/inline/shaping.rs:655-710](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L655-L710) 的 `find_safe_to_break` 末尾确认：返回值取决于 `glyphs[idx].safe_to_break`，而该标志来自 rustybuzz 的 `info.unsafe_to_break()` 取反（见 [src/inline/shaping.rs:1100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1100)）。
4. 构造一个心智实验：英文单词 `flutter`（含 `fl` 合字的字体下，`fl` 是一个字形、`safe_to_break=false`）。
   - 若断点在 `flu` / `tter`（切在合字内部）→ `slice_safe_to_break` 返回 `None` → 重新 `shape`。
   - 若断点在 `flutter` 之后的空格 → 整段复用，连 reshape 都不触发。
5. （可选，待本地验证）用一个支持 `liga` 的字体排版一段很长的 `flutter flutter ...`，开启 `#set par(justify: true)`，观察是否会出现因合字被切断而触发的重整（可在 `reshape` 的 else 分支临时加 `eprintln!` 统计）。

**需要观察的现象**：绝大多数断点走「切片复用」分支，只有切在合字/连写内部的少数断点走「重新 shape」分支。

**预期结果**：你能用一句话回答「为什么段落只整形一次却能在任意位置断行而不出错」——因为 `safe_to_break` 标志让Typst知道哪些切点可以安全复用字形、哪些必须重整。

> 说明：前 4 步为源码阅读型推演；第 5 步需修改源码并本地运行，可选，勿提交临时改动。

#### 4.4.5 小练习与答案

**练习 1**：`reshape` 命中切片复用分支时，返回的 `ShapedText.glyphs` 是 owned 还是 borrowed？为什么这样设计？

**答案**：borrowed（`Glyphs::from_slice` → `Cow::Borrowed`）。因为复用的字形本就属于 `prepare` 阶段那个长期存活的 `ShapedText`，借用它既零拷贝又无需克隆；只有重新 `shape` 时才是 owned（`from_vec`）。这也是 `Glyphs::to_mut` 文档警告「borrowed 上调用会触发克隆」的由来。

**练习 2**：`find_safe_to_break` 的 `Err` 分支里专门检测了 `\n`（[src/inline/shaping.rs:686-691](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L686-L691)）。为什么需要这个特例？

**答案**：`\n` 在 Typst 里不产生字形（被 `REMOVE_DEFAULT_IGNORABLES` 或跳过），二分查找会得到 `Err`；但在 `\n` 处断行显然是安全的，所以专门检测「前一个字形的 range 末端正好是 text_index 且该处是 `\n`」并允许复用，避免无谓重整。

### 4.5 软连字符：shy 如何在断行后变成 hyphen

#### 4.5.1 概念说明

软连字符 U+00AD（`shy`）是一个「平时不可见、断行时变成 `-`」的特殊字符。它的难点在于：

- 不应占据任何宽度（不可见）。
- 只有当断行恰好落在它之后时，才在行尾画一个真正的连字符 `-`。
- 断行算法（u5-l3）需要把它识别为合法断点。

Typst 的做法是：`shy` 在整形阶段**不产生字形**（或产生零宽字形），但它是一个合法的断点候选；当断行决定在某处断开、且该断点是 `Hyphen` 类型（见 u5-l3 的 `Breakpoint::Hyphen`）时，`line.rs` 会调 `ShapedText::hyphen(..., soft=true)` 现场构造一个只含 `-` 字形的 `ShapedText`，追加到行尾。

注意区分两种连字符来源：

- **soft hyphen**（来自文本里的 `shy` 或连字符断词）：`soft=true`，`c` 是 `\u{AD}`。
- **hard hyphen**（文本里本就有的 `-` 被断在行尾，需在下一行行首重复）：`soft=false`，`c` 是 `-`。

#### 4.5.2 核心流程

```text
linebreak 决定在某行末尾断开，断点类型 = Dash::Soft（落在 shy/hyphenation 处）
  │
  ▼
line() 构造该行：
  ├─ 收集 items（含对被切开文本的 reshape）
  ├─ if 行尾是 Dash::Soft：
  │     base = items.trailing_text()      # 取行尾那个 ShapedText 作为样式模板
  │     hyphen = ShapedText::hyphen(engine, fallback, base, trim.shaping, soft=true)
  │     items.push(Item::Text(hyphen), END_HYPHEN)
  └─ ...
```

`ShapedText::hyphen` 的逻辑：沿用 `base` 的样式链与变体，遍历字体家族找一个能画出 `-`（glyph index for `'-'`）的字体，构造一个单字形的 `ShapedText`。`fallback` 参数控制是否启用全局字体回退（找不到 `-` 时）。

#### 4.5.3 源码精读

`SHY` / `HYPHEN` 常量见 [src/inline/shaping.rs:28-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L28-L31)。

`ShapedText::hyphen` 见 [src/inline/shaping.rs:583-638](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L583-L638)。要点：

- L592-596：按 `fallback` 决定是否准备一个 `select_fallback` 闭包。
- L597-601：遍历 `base` 的字体家族（带 covers 过滤，要求能匹配 `-`），再接 fallback，串成一个迭代器。
- L603-637：找第一个能 `ttf.glyph_index('-')` 且有水平 advance 的字体，构造单个 `ShapedGlyph`，其 `c` 与 `text` 由 `soft` 决定（soft → `\u{AD}` / `"\u{ad}"`，否则 → `'-'` / `"-"`，见 L612）。

**消费侧**有两处：

- 行首重复连字符（hard dash 续行）：见 [src/inline/line.rs:158-167](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L158-L167)，`soft=false`。
- 行尾软连字符：见 [src/inline/line.rs:171-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L171-L178)，`soft=true`。

`soft` 标志的作用是让最终生成的字形在文本提取时映射回正确的字符：行尾因 `shy` 产生的连字符应映射为 `\u{AD}`（软连字符），而非普通 `-`，以保留语义。

#### 4.5.4 代码实践

**实践目标**：追踪一个含软连字符的单词在断行后如何多出一个 `-` 字形。

**操作步骤**：

1. 设想文本 `super­califragilistic`（中间是 U+00AD 软连字符，写在 `super` 与 `califragilistic` 之间），排版在窄列里，被迫在软连字符处断行。
2. 在 [src/inline/shaping.rs:583-638](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L583-L638) 确认：`hyphen` 用 `base.styles`（行尾文本的样式）选字体，保证连字符字体与正文字体一致。
3. 在 [src/inline/line.rs:171-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L171-L178) 确认：仅当 `dash == Some(Dash::Soft)` 才追加行尾连字符。
4. 对照 [src/inline/shaping.rs:612](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L612)：`soft=true` 时 `c = SHY`、`text = SHY_STR`，所以导出文本里这个 `-` 的码位是 U+00AD。
5. （可选，待本地验证）写文档 `#set par(justify: true); #box(width: 60pt)[super­califragilistic]`，编译后观察第一行行尾是否出现 `-`，且复制该 `-` 粘贴到纯文本编辑器看是否为软连字符。

**需要观察的现象**：窄列下 `super-` 换行到 `califragilistic`，行尾的 `-` 来自 `ShapedText::hyphen`。

**预期结果**：你能解释「软连字符平时不可见、断行后才画 `-`」这一行为在源码里的三处落点：断点识别（u5-l3 的 `Dash::Soft`）、`hyphen` 构造（本节）、`soft` 标志决定码位。

> 说明：前 4 步为源码阅读型；第 5 步为可选本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ShapedText::hyphen` 要用 `base`（行尾文本）的样式链，而不是随便挑一个字体画 `-`？

**答案**：保证连字符与所在行的正文字体、字号、变体（粗细/斜体）完全一致，视觉上协调；也保证颜色（fill）一致。见 [src/inline/shaping.rs:597-608](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L597-L608)，字体从 `families(base.styles)` 取，size 从 `base.styles.resolve(TextElem::size)` 取。

**练习 2**：`soft=true` 与 `soft=false` 分别对应哪种场景？`c`/`text` 字段为何要随之改变？

**答案**：`soft=true` 对应「软连字符断行」（行尾因 `shy` 或连字符断词产生），`c=SHY`；`soft=false` 对应「硬连字符续行」（行尾本就有 `-`，下一行行首要重复一个），`c=HYPHEN`。改变 `c`/`text` 是为了让 PDF 文本提取把该字形映射回正确的码位（U+00AD vs U+002D），保留源文本语义。见 [src/inline/shaping.rs:612](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L612)。

## 5. 综合实践

把本讲五个模块串起来，完成下面这个**端到端追踪任务**。

**任务**：给定样式链 `#set text(font: ("Linux Libertine", "Noto Sans CJK SC"))`、`#set par(justify: true)`，文本为：

```
Typst 是 flutter 一种标记语言
```

请按下面的顺序，在源码里标注每一步发生的位置与产物：

1. **分段**（4.2）：`shape_range` 按 BiDi level 与 script 把这段文本切成哪些 run？分别在 [src/inline/shaping.rs:746-769](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L746-L769) 标注切点。
2. **回退**（4.3）：汉字 `是` 在 Linux Libertine 里缺字，追踪 [src/inline/shaping.rs:1110-1151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1110-L1151) 的 tofu 分支如何递归用 Noto Sans CJK SC 重新整形。
3. **缓存**（4.2）：拉丁 run 与汉字 run 各自调 `create_shape_plan`，确认 [src/inline/shaping.rs:1217-1233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L1217-L1233) 因字体不同而得到两个不同 plan。
4. **reshape**（4.4）：断行决定在 `flutter` 内部切开（假设字体启用了 `fl` 合字），追踪 [src/inline/line.rs:316-319](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L316-L319) 调 `reshape` → [src/inline/shaping.rs:546-572](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L546-L572) 命中或未命中 `slice_safe_to_break`，并解释为何此处会走重新整形分支。
5. **hyphen**（4.5）：若 `flutter` 内部还有一个软连字符断点，追踪 [src/inline/line.rs:171-178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/line.rs#L171-L178) 如何在行尾追加 `ShapedText::hyphen`。

**产出**：一张表格，列出「步骤 / 发生函数（带永久链接） / 输入 / 输出 / 关键判定」。完成后，你应当能向别人讲清楚：一段混合脚本的文本，从字符串到最终可渲染的字形序列，整形层到底做了哪些事、哪些被缓存复用、哪些必须重算。

> 说明：本任务为源码阅读型，不要求运行；若想验证，可在上述函数加临时 `eprintln!` 后 `cargo test`，但请勿提交改动。

## 6. 本讲小结

- 整形把字符变成带位置/字体的 **`ShapedGlyph`** 序列，包成 **`ShapedText`**（借整段文本切片，克隆廉价、可哈希）；`Glyphs` 用 `Cow` + `kept` 区间实现「行尾空白裁剪但不删除」。
- **`shape_range`** 按 BiDi level 与 script 把文本切成若干 run，每个 run 调 **`shape`** 经 `shape_segment`（rustybuzz）→ `track_and_space` → `calculate_adjustability` 三步完成整形；rustybuzz 的 **shape plan 与文本无关**，被 `create_shape_plan` 用 comemo 缓存复用。
- **字体回退**由 `get_font_and_covers` + `SharedShapingContext` 驱动：遍历样式链字体家族、按 covers 判定覆盖、耗尽后走全局 fallback；`shape_segment` 对 tofu 子串**递归**用剩余家族再整，`used` 列表防止死循环。
- **按需 reshape** 是性能关键：`reshape` 优先用 `slice_safe_to_break` 直接**借用切片复用**已整形字形，只有断点落在 `safe_to_break=false` 的边界（如合字内部）才重新 `shape`。
- **软连字符** `shy` 平时不产生可见字形；断行落在它处时，`line.rs` 调 `ShapedText::hyphen` 现场构造单字形 `-` 追加到行尾，`soft` 标志决定导出码位是 U+00AD 还是 U+002D。

## 7. 下一步学习建议

- 本讲的整形产物 `ShapedText` 如何被组装成单行、如何对齐、如何挂装饰（下划线/删除线），见 **u5-l5（行构建与装饰：line/deco/finalize）**。
- 整形提供的 `justifiables` / `stretchability` / `shrinkability` 如何被两端对齐算法消费，可回看 **u5-l3（换行算法 linebreak）** 的成本模型与本讲 [src/inline/shaping.rs:513-540](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L513-L540) 的对应方法。
- 想了解 `ShapedText::build` 产出的 `FrameItem::Text` 如何最终进入页面 Frame 与 PDF，可继续阅读 **u2-l3（Frame 与 Fragment）** 与导出层（typst-pdf / krilla）相关文档。
- 对 BiDi 如何决定每个 run 的方向仍有疑问，请回顾 **u5-l2（文本收集与 BiDi 准备）**——本讲的 `shape_range` 直接消费其 `bidi.levels`。
