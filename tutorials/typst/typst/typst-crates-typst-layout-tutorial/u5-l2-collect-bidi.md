# 文本收集与 BiDi 准备

## 1. 本讲目标

本讲是行内（段落）布局单元的第二篇。在上一篇 u5-l1 中，我们建立了段落排版的**四段管线**心智模型：

```
collect → prepare → linebreak → finalize
```

并知道这四步的类型链是：

```
&[Pair] ──collect──▶ (String, Vec<Segment>, SpanMapper)
        ──prepare──▶ Preparation
        ──linebreak▶ Vec<Line>
        ──finalize─▶ Fragment
```

本讲钻进这条链的**前两段**：`collect` 与 `prepare`。读完本讲你应当能够：

1. 说清楚为什么 Typst 要把一整段（含 `#box`、链接、空格、脚注标记等异构内容）的段落**先压扁成一个纯字符串**，再做后续处理。
2. 看懂 `collect` 如何用一个**替换字符表**（空格、对象替换符、BiDi 控制符）把非文本内容「占位」进字符串，同时把真正的样式/几何信息存进 `Segment` / `Item`。
3. 理解 Unicode 双向算法（BiDi）在 `prepare` 中的角色：为什么 BiDi 必须在「收集完所有文本」之后、在「整形（shaping）」之前进行，以及 `shape_range` 如何在 level 边界切分文本 run。
4. 看懂 `Preparation` 结构里每一字段的作用，以及 `SpanMapper` 如何把字符串里的字节偏移**回映**到源码 `Span`，从而支撑编译诊断与追踪。

本讲不展开 `linebreak`（u5-l3）与 `shaping` 的完整细节（u5-l4），只在 `prepare` 调用处点到为止。

## 2. 前置知识

### 2.1 什么叫「双向文字（BiDi）」

人类书写的方向并不统一：英文、中文从左到右（LTR），阿拉伯文、希伯来文从右到左（RTL）。当一段文字里**同时**出现两种方向时（比如一句阿拉伯语里夹了一个英文单词，或一句中文里夹了英文），每个字符的**视觉位置**不仅取决于它自己，还取决于它**周围的字符**与整段的**基准方向**。

Unicode 定义了一套**双向算法（UAX #9）**来解决「逻辑顺序 → 视觉顺序」的映射。它的核心产物是给每个字符分配一个 **embedding level（嵌套层级）**：

- 偶数 level（0、2、4…）表示该字符处于 LTR 上下文；
- 奇数 level（1、3、5…）表示该字符处于 RTL 上下文。

视觉排版时，连续的同 level 字符构成一个「方向 run」，RTL run 会在显示时整体翻转。**关键点**：一个字符的 level 不能孤立计算，必须拿到**整段文字**才能算。这正是本讲标题里「把所有文本拼成一整段做 BiDi」的根本原因。

### 2.2 文本整形（shaping）是什么

字体里存的不是「字符 → 字形」的一一映射，而是一组规则：相邻字符可能合并成一个连字（ligature，如 `fi`）、可能产生字距微调（kerning）、阿拉伯字母在不同位置（词首/词中/词尾/独立）有不同字形。**整形（shaping）**就是把一串字符 + 字体 + 方向，喂给整形引擎（Typst 用 `rustybuzz`），得到一串带 advance、offset 的**字形（glyph）**。

整形是**有方向性的**：RTL 文本整形的字形顺序与 LTR 不同。所以整形必须发生在 BiDi 算完 level 之后。这一点决定了管线顺序：**先 BiDi，后整形**。

### 2.3 Pair 与 StyleChain

回顾 u1-l4 / u4-l2：`realize` 把任意 Content 展开成扁平的 `Vec<Pair>`，其中 `Pair = (已知的强类型元素, StyleChain)`。`StyleChain` 是「样式链」，记录该元素继承到的所有 `set` 样式。本讲中 `collect` 的输入就是这个 `&[Pair]`，它需要逐个把 Pair 翻译成字符串片段或 `Item`。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `src/inline/` 下：

| 文件 | 作用 |
| --- | --- |
| [src/inline/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs) | 管线调度。`layout_inline_impl` 在此把四步串起来；`Config` / `ConfigBase` 定义于此。 |
| [src/inline/collect.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs) | **本讲主角之一**：`collect` 把 `&[Pair]` 压扁成 `(String, Vec<Segment>, SpanMapper)`。定义 `Item`、`Segment`、`Collector`、`SpanMapper`。 |
| [src/inline/prepare.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs) | **本讲主角之二**：`prepare` 跑 BiDi、调 `shape_range` 整形、装配 `Preparation`。 |
| [src/inline/shaping.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs) | `shape_range` 与 `ShapedText` 在此（详见 u5-l4，本讲只在 `prepare` 调用处引用）。 |

`unicode-bidi` 是外部 crate，`prepare.rs` 顶部 `use unicode_bidi::{BidiInfo, Level as BidiLevel};` 引入它（[src/inline/prepare.rs:4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L4)）。

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

- **4.1 `inline/collect`**：收集阶段，把异构 Pair 压扁成「一个字符串 + segment 列表 + span 映射」。
- **4.2 `unicode-bidi`**：双向算法的概念，以及它在 `prepare` 里如何被消费、如何驱动 run 切分。
- **4.3 `inline/prepare`**：准备阶段，BiDi + 整形装配出 `Preparation`。

### 4.1 inline/collect：把混合内容压扁成一个字符串

#### 4.1.1 概念说明

`collect` 要解决的核心矛盾是：

> 段落里既有**纯文本**（`TextElem`、空格、引号），也有**已经排版好的盒子**（`#box`、行内元素、脚注标记），而后续的 BiDi 与断行算法**只能处理字符串**。

Typst 的选择是：**把整段内容表示成一条「逻辑字符串」**，让 BiDi 和断行在字符串层面工作；而非文本内容（盒子、间距）在字符串里用一个**占位字符**顶替，真正的几何/样式信息另存进 `Segment` / `Item`。这样断行算法看到的永远是一维的字符流，不必关心「这里其实是个图片盒子」。

为什么必须先收集**全部**文本、才能往下走？源码注释一句话点透：

> We can't shape text until we have collected all items because only then we can compute BiDi, and we need to split shape runs at level boundaries.
> （在收集完所有 item 之前无法整形文本，因为只有那时才能算 BiDi，而我们必须在 level 边界处切分 shape run。）

—— [src/inline/collect.rs:99-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L99-L101)

#### 4.1.2 核心流程

`collect` 的签名（[src/inline/collect.rs:125-131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L125-L131)）：

```rust
pub fn collect<'a>(
    children: &[Pair<'a>],
    engine: &mut Engine<'_>,
    locator: &mut SplitLocator<'a>,
    config: &Config,
    region: Size,
) -> SourceResult<(String, Vec<Segment<'a>>, SpanMapper)>
```

它产出三件东西：

1. `full: String` —— 整段的逻辑字符串（含占位符）。
2. `segments: Vec<Segment>` —— 与字符串逐段对齐的「样式/已排版 item」序列。
3. `spans: SpanMapper` —— 把字符串字节偏移回映到源码 `Span`。

主流程是一个**对 children 的单趟扫描**，外加首行/悬挂缩进的预处理：

```
1. 若 first_line_indent 非零 → 在最前面塞一个 Item::Absolute(正值)
2. 若 hanging_indent 非零   → 在最前面塞一个 Item::Absolute(负值)
3. for 每个 (child, styles) in children:
       按元素类型分派（见 4.1.3），追加文本或 item
       记录这段在 full 里占的字节数 → spans.push(len, child.span())
4. 返回 (full, segments, spans)
```

关键是步骤 3 的**统一收尾**：不管这个 child 被翻译成什么，最后都要 `spans.push(len, child.span())`，保证字符串里任何一段都能回溯到源码位置。

#### 4.1.3 源码精读

**(a) 替换字符表**

`collect.rs` 顶部定义了几类常量（[src/inline/collect.rs:20-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L20-L28)）：

```rust
const SPACING_REPLACE: &str = " ";        // 空格 → 半角空格
const OBJ_REPLACE: &str = "\u{FFFC}";     // 对象替换符（U+FFFC）
// Unicode BiDi 控制字符
const LTR_EMBEDDING: &str = "\u{202A}";   // LRE
const RTL_EMBEDDING: &str = "\u{202B}";   // RLE
const POP_EMBEDDING:  &str = "\u{202C}";  // PDF
const LTR_ISOLATE:    &str = "\u{2066}";  // LRI
const POP_ISOLATE:    &str = "\u{2069}";  // PDI
```

- `SPACING_REPLACE`：间距类 item 在字符串里以一个普通空格占位（断行算法能在这里断开）。
- `OBJ_REPLACE`（U+FFFC「对象替换符」）：盒子/图片等「不可分」的行内对象用一个字符顶替，它在 BiDi 里被视为中性、在断行里被视为**不可拆**的整体。
- BiDi 控制符（LRE/RLE/PDF/LRI/PDI）：用于在字符串里**显式声明方向边界**（见下文 TextElem 与 InlineElem 分支）。

每个 `Item` 都实现了 `textual()`，声明自己在字符串里长什么样（[src/inline/collect.rs:72-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L72-L80)）：

```rust
pub fn textual(&self) -> &str {
    match self {
        Self::Text(shaped) => shaped.text,
        Self::Absolute(_, _) | Self::Fractional(_, _) => SPACING_REPLACE,
        Self::Frame(_) => OBJ_REPLACE,
        Self::Tag(_) => "",
        Self::Skip(s) => s,
    }
}
```

注意 `Tag` 的 `textual()` 是空串——**标签不进字符串、也不占宽度**，它是隐形有序标记（回顾 u2-l4）。`Skip` 则原样吐出它携带的控制符（如 LRI/PDI）。

**(b) 分派循环**

主体循环按元素类型逐个处理（[src/inline/collect.rs:145-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L145-L248)）。下表是「元素 → 字符串/segment」的映射规则：

| 输入元素 | 字符串里写入 | segment 类型 | 说明 |
| --- | --- | --- | --- |
| `SpaceElem` | `" "`（[L148-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L148-L149)） | `Text` | 空格当普通文本 |
| `TextElem` | 大小写转换后的文本（[L150-172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L150-L172)） | `Text` | 若 `dir ≠ config.dir`，前后包 LRE/RLE + PDF |
| `HElem` | ` ` 或 ` `（[L173-184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L173-L184)） | `Item::Absolute` / `Fractional` | 间距，零间距直接 `continue` |
| `LinebreakElem` | `"\n"` 或 `"\u{2028}"`（[L185-189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L185-L189)） | `Text` | 强制换行符 |
| `SmartQuoteElem` | 智能引号（[L190-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L190-L205)） | `Text` | 根据前一个非忽略字符选开/闭引号 |
| `InlineElem` | LRI + … + PDI（[L206-222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L206-L222)） | `Item::Skip` ×2 + 内部 item | 用 isolate 隔离其内部方向 |
| `BoxElem` | ` `（Fr）或 `\u{FFFC}`（[L223-233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L223-L233)） | `Item::Fractional` / `Item::Frame` | **Fr 间距型 box 立即记录、定宽 box 立即排版** |
| `TagElem` | （空） | `Item::Tag`（[L234-235](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L234-L235)） | 隐形标签 |
| 其它 | （忽略） | — | 发一条 warning（[L236-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L236-L244)） |

`#box` 分支尤其值得展开（[src/inline/collect.rs:223-233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L223-L233)）：

```rust
} else if let Some(elem) = child.to_packed::<BoxElem>() {
    let loc = locator.next(&elem.span());
    if let Sizing::Fr(v) = elem.width.get(styles) {
        collector.push_item(Item::Fractional(v, Some((elem, loc, styles))));
    } else {
        let mut frame = layout_and_modify(styles, |styles| {
            layout_box(elem, engine, loc, styles, region)
        })?;
        apply_shift(&engine.world, &mut frame, styles);
        collector.push_item(Item::Frame(frame));
    }
}
```

这里有两个细节：

1. **Fr（分数）宽度的 box 不立即排版**：因为它的实际宽度取决于行内剩余的 Fr 空间分配，只有断行阶段才知道。所以只把 `(元素, locator, styles)` 存进 `Item::Fractional`，延后到 `linebreak`/`finalize` 再排版（回顾 u4-l5 提到的 Fr 高度瓜分思想，行内是宽度瓜分）。
2. **定宽/自动宽度的 box 立即排版成一帧**：调用 `layout_and_modify`（带 `FrameModifiers`，回顾 u6-l7）→ `apply_shift`（处理上下标位移）→ 存成 `Item::Frame`，字符串里只占一个 `\u{FFFC}`。

**(c) 相邻同类合并**

`Collector` 有两条合并规则，目的是**压缩 segment 数量、避免无意义的细分**：

- **相邻同样式文本段合并**（[src/inline/collect.rs:281-289](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L281-L289)）：若新文本段的 `styles` 与上一段 `Text` 相同，只把长度累加进上一段，不新增 segment。
- **相邻弱间距取最大值**（[src/inline/collect.rs:293-300](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L293-L300)）：两个 `weak=true` 的 `Absolute` 间距相邻时，保留较大者——这是段落里多个软空格不会无限累加的关键。

**(d) 首行缩进与悬挂缩进的预处理**

在扫描 children 之前，`collect` 先处理两个段落级缩进（[src/inline/collect.rs:135-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L135-L143)）：

```rust
if !config.first_line_indent.is_zero() {
    collector.push_item(Item::Absolute(config.first_line_indent, false));
    collector.spans.push(1, Span::detached());
}
if !config.hanging_indent.is_zero() {
    collector.push_item(Item::Absolute(-config.hanging_indent, false));
    collector.spans.push(1, Span::detached());
}
```

回顾 u5-l1：首行缩进是**在整段最前面插一个正值 `Absolute` 间距**（只影响第一行）；悬挂缩进插一个**负值**间距，它在首行把光标往左推，等价于「其它行往右缩」。两者都标 `Span::detached()`（不归属源码），且都占 1 字节（`SPACING_REPLACE`）。

#### 4.1.4 代码实践

> **实践目标**：亲手追踪 `Hello #box[W]!` 这一小段如何被 `collect` 映射成 `(full, segments, spans)`。

**操作步骤（源码阅读型实践）**：

1. 打开 [src/inline/collect.rs:145-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L145-L248) 的分派循环，对照下表逐个 child 推演。假设没有缩进，且所有文本 `styles` 相同。
2. 假设 realize 后 children 大致为：`[Text("Hello"), Space, Box(W), Text("!")]`（实际还会带 StyleChain）。

**预期推演结果（待本地验证）**：

| 步骤 | child | full 变化 | 新增 segment |
| --- | --- | --- | --- |
| 1 | `Text("Hello")` | `"" → "Hello"` | `Text(len=5, s)` |
| 2 | `Space` | `"Hello" → "Hello "` | 与上同 styles，**合并** → `Text(len=6, s)` |
| 3 | `Box(W)`（定宽） | `"Hello " → "Hello \u{FFFC}"` | `Item(Frame{W})` |
| 4 | `Text("!")` | `… → "Hello \u{FFFC}!"` | `Text(len=1, s)` |

最终：

- `full = "Hello \u{FFFC}!"`（注意 `\u{FFFC}` 只占 3 个字节 `0xEF 0xBF 0xBC`，但 `textual_len` 按 UTF-8 字节数算）。
- `segments = [Text(6), Item(Frame), Text(1)]`。
- `spans` 记录每个 child 的 `(字节长度, span)`，于是字符串偏移能反查回源码。

3. **回答 SpanMapper 如何保留源码位置**：阅读 [src/inline/collect.rs:311-338](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L311-L338)。`SpanMapper` 内部是 `Vec<(usize, Span)>`——一组「(该段字节数, 该段源码 Span)」。`span_at(offset)` 用一个游标顺序扫描，命中包含 `offset` 的区间就返回 `(Span, 区间内偏移)`。后续断行/整形若在某个字节处发现问题（如缺字形），就能用 `span_at` 把错误指回源码的精确位置。

**需要观察的现象**：把 `\u{FFFC}` 想象成「不可见地钉在字符串里的一个钉子」——它在 BiDi 与断行里是一个完整的中性字符，但它的真实宽度不在字符串里，而在与之配对的 `Item::Frame` 上。字符串只负责**顺序与可断性**，宽度另算。

#### 4.1.5 小练习与答案

**练习 1**：若把 `Box(W)` 换成 `#box[W, width: 1fr]`（Fr 宽度），`collect` 会写出什么 segment？字符串里该位置是什么字符？

> **答案**：走 [L225-226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L225-L226) 的 Fr 分支，segment 为 `Item::Fractional(fr, Some((elem, loc, styles)))`，**不立即排版**。字符串里该位置写 `SPACING_REPLACE`（一个空格），因为 `Item::Fractional` 的 `textual()` 返回 `SPACING_REPLACE`（[L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L75)）。

**练习 2**：为什么 `TagElem` 不向字符串写入任何字符，却仍要作为一个 `Item::Tag` 存在？

> **答案**：Tag 是「隐形但有序」的内省标记（回顾 u2-l4）。它不占布局空间（`textual()` 为空串、`natural_width()` 为 0），所以不影响字符串的视觉顺序；但它必须**随内容流过断行阶段、落入正确的行与帧**，才能让 `query`/`counter` 在正确位置生效。因此它以 `Item::Tag` 参与 segment 序列，却对字符串「透明」。

---

### 4.2 unicode-bidi：双向算法与 level 切分

#### 4.2.1 概念说明

`unicode-bidi` 是 Rust 生态对 Unicode 双向算法（UAX #9）的实现。Typst 在 `prepare` 里 `use unicode_bidi::{BidiInfo, Level as BidiLevel};`（[src/inline/prepare.rs:4](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L4)），把算 level 的活外包给它。

`BidiInfo::new(text, Some(default_level))` 接收**整段字符串**和**段落基准 level**，输出 `levels: Vec<Level>`——每个字节位置一个 level。这是本模块要理解的核心对象。

为什么要传**基准 level**？因为同一段文字，基准方向不同，视觉结果可能不同。Typst 用 `config.dir` 决定基准（[src/inline/prepare.rs:73-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L73-L76)）：

```rust
let default_level = match config.dir {
    Dir::RTL => BidiLevel::rtl(),   // 基准 level = 1（奇数，RTL）
    _ => BidiLevel::ltr(),          // 基准 level = 0（偶数，LTR）
};
```

#### 4.2.2 核心流程

BiDi 在 Typst 里的角色可以概括为一句话：**给字符串里每个字节打一个方向标签，供后续整形按方向切 run。**

```
1. 算 default_level（来自 config.dir）
2. BidiInfo::new(text, default_level) → 得到每字节 level
3. is_bidi = 是否存在与基准方向相反的 level
4. 把 levels 交给 shape_range，按 level 边界切分文本 run
```

第 3 步有个重要优化（[src/inline/prepare.rs:79-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L79-L82)）：

```rust
let is_bidi = bidi
    .levels
    .iter()
    .any(|level| level.is_ltr() != default_level.is_ltr());
```

如果**所有** level 的方向都和基准一致（即整段其实是单向的，比如纯英文或纯中文），那就 `is_bidi = false`，最终 `Preparation.bidi` 被存成 `None`（[src/inline/prepare.rs:116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L116)）。绝大多数段落走这条快路径，省掉携带 `BidiInfo` 的开销。

#### 4.2.3 源码精读

BiDi 真正「发挥作用」的地方，是 `shape_range` 按 level 切 run 的循环（[src/inline/shaping.rs:721-772](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L721-L772)）。精简后：

```rust
for i in range.clone() {
    if !text.is_char_boundary(i) { continue; }
    let level = bidi.levels[i];
    let curr_script = /* 当前字符的 script */;
    if level != prev_level || !is_compatible(curr_script, prev_script) {
        if cursor < i { process(cursor..i, prev_level); }  // 整形上一段
        cursor = i;
        prev_level = level;
        prev_script = curr_script;
    } else if is_generic_script(prev_script) {
        prev_script = curr_script;
    }
}
process(cursor..range.end, prev_level);  // 整形最后一段
```

其中 `process` 对每一段调用 `shape(...)`，并把该段的 `dir` 直接由 level 推出（[src/inline/shaping.rs:732-737](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L732-L737)）：

```rust
let mut process = |range: Range, level: BidiLevel| {
    let dir = if level.is_ltr() { Dir::LTR } else { Dir::RTL };
    let shaped = shape(engine, range.start, &text[range.clone()], styles, dir, lang, region);
    items.push((range, Item::Text(shaped)));
};
```

要点：

- **level 边界即 run 边界**：level 一变，就结束当前 run、整形它、开新 run。这保证每个 `ShapedText` 内部方向一致。
- **script 边界也是 run 边界**：即使 level 没变，文字系统（script，如 Latin vs CJK）变了也要另开 run——因为不同 script 通常用不同字体、不同整形规则。`is_compatible` 判定（[L780-782](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L780-L782)）允许「通用 script（Common/Inherited/Unknown）」与任意 script 共存。
- **dir 由 level 决定**：偶数 level → LTR，奇数 → RTL。整形器拿到正确方向，才能正确摆放字形顺序。

> 小贴士：`collect` 里 `TextElem` 的 `dir ≠ config.dir` 时包的 LRE/RLE/PDF（[L152-172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L152-L172)），就是给 BiDi 算法「递信号」——这些控制符会让 `unicode-bidi` 在该子区间抬升或压低 level，从而正确切 run。`InlineElem` 用的 LRI/PDI（isolate）则更强：把内部完全从周围方向里隔离出来。

#### 4.2.4 代码实践

> **实践目标**：理解「为什么一句 RTL 段落里夹的英文单词仍按 LTR 显示」。

**操作步骤（源码阅读型实践）**：

1. 想象一个 RTL 段落（`#set text(dir: rtl)`）里写了 `مرحبا Hello 世界`（阿拉伯词 + 英文 + 中文）。
2. 在 [src/inline/prepare.rs:73-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L73-L82) 追踪：`default_level = 1`（RTL）。`BidiInfo::new` 会给阿拉伯字符 level 1，给 `Hello` 这些拉丁字母 level 2（偶数，LTR，因为强 LTR 字符在 RTL 上下文里被抬到 2）。
3. 在 [shape_range 循环](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L746-L769) 追踪：level 从 1 变 2 时切开，`Hello` 这一段用 `dir = LTR` 整形，因此它在视觉上保持左到右的字母顺序。

**需要观察的现象**：`is_bidi` 此时为 `true`（存在 level 2，其 `is_ltr() != default_level.is_ltr()`），所以 `Preparation.bidi = Some(...)`，BiDi 信息被保留。而纯中文段落里夹英文（基准 LTR）时，英文 level 仍为 0，`is_bidi` 可能为 `false`，走快路径。

**预期结果**：英文单词的字母顺序不被翻转，但它在行内的**整体位置**仍受 RTL 基准方向支配（从右往左排）。这正是 BiDi「局部方向、全局排布」的效果。

> 说明：具体的 level 数值由 `unicode-bidi` 按 UAX #9 规则计算，上面 level 2 的说法是典型情形，**精确数值待本地用 `unicode-bidi` 直接调用验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BidiInfo::new` 必须传「整段字符串」，而不能逐句算 BiDi 再拼接？

> **答案**：因为一个字符的 level 取决于它在段落里的上下文（前后字符的方向、基准方向、显式嵌入/isolate 控制符）。逐句算会丢失跨句的方向影响，导致相邻句子的边界 run 被错误切分。这是 UAX #9 的「段落级」要求，也是 `collect` 必须先压扁全段的根因。

**练习 2**：`is_bidi` 为 `false` 时，`Preparation.bidi` 存成 `None`。这会影响到 `shape_range` 的切 run 吗？

> **答案**：不会影响正确性。`shape_range` 始终读 `bidi.levels`，即使全是基准 level，它也照常按 level（全相同）+ script 边界切 run，只是不会因 level 变化而切。`None` 只是 `Preparation` 不再**持有** `BidiInfo`（省内存），而 `prepare` 内部算 level 时用的是局部 `bidi` 变量（[L78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L78)），切 run 在那之前已完成。

---

### 4.3 inline/prepare：BiDi 分析与 Preparation 装配

#### 4.3.1 概念说明

`prepare` 是 collect 与 linebreak 之间的「装配车间」。它接收 collect 的产物 `(text, segments, spans)`，做四件事：

1. 跑 BiDi（4.2 已讲）。
2. 遍历 `segments`，把 `Segment::Text` 用 `shape_range` 整形、把 `Segment::Item` 原样收进 items，**并给每个 item 标注它在字符串里的字节区间 `(Range, Item)`**。
3. 建「字节偏移 → item 下标」的反查表 `indices`。
4. （可选）加 CJK-Latin 间距。

产物是 `Preparation`——一个「文本已整形、item 已就位、可按区间切片」的结构，让后续 `linebreak` 不必每行从头排版。

#### 4.3.2 核心流程

```
prepare(text, segments, spans, config):
  1. default_level = config.dir → rtl/ltr level
  2. bidi = BidiInfo::new(text, default_level)
  3. is_bidi = levels 里是否有与基准相反方向
  4. cursor = 0
     for segment in segments:
         range = cursor .. cursor + segment.textual_len()
         match segment:
           Text(_, styles) → shape_range(items, ..., bidi, range, styles)  # 可能切成多个 Text item
           Item(item)      → items.push((range, item))                     # 原样收纳
         cursor = range.end
  5. indices: 字节偏移 → item 下标
  6. 若 cjk_latin_spacing → add_cjk_latin_spacing(items)
  7. 返回 Preparation { text, config, bidi: is_bidi?bidi:None, items, indices, spans }
```

注意第 4 步：一个 `Segment::Text` 经过 `shape_range` 后**可能变成多个 `Item::Text`**（因为 level/script 边界会切 run）；而一个 `Segment::Item` 永远是 1:1 收纳。所以 `items` 比 `segments` 更细。

#### 4.3.3 源码精读

**(a) Preparation 结构**

`Preparation` 各字段（[src/inline/prepare.rs:14-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L14-L30)）：

```rust
pub struct Preparation<'a> {
    pub text: &'a str,          // 整段逻辑字符串（collect 产出的 full）
    pub config: &'a Config,     // 段落配置（u5-l1）
    pub bidi: Option<BidiInfo<'a>>,  // BiDi 信息，单向段落为 None
    pub items: Vec<(Range, Item<'a>)>, // 已整形文本 + 已收纳 item，带字节区间
    pub indices: Vec<usize>,    // 字节偏移 → items 下标，加速查找
    pub spans: SpanMapper,      // 字节偏移 → 源码 Span
}
```

`items` 是核心：每个元素是 `(字节区间, Item)`。`Item::Text` 里的 `ShapedText` 自身也记了 `base`（该 run 在全文的起始偏移）和 `text`（该 run 的子串），见 [src/inline/shaping.rs:39-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/shaping.rs#L39-L56)：

```rust
pub struct ShapedText<'a> {
    pub base: usize,          // 该 run 在全文的起始字节
    pub text: &'a str,        // 该 run 的子串
    pub dir: Dir,             // 该 run 的方向（来自 BiDi level）
    pub lang: Lang,
    pub styles: StyleChain<'a>,
    pub glyphs: Glyphs<'a>,   // 整形后的字形
    ...
}
```

**(b) prepare 主体**

关键循环（[src/inline/prepare.rs:88-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L88-L101)）：

```rust
for segment in segments {
    let len = segment.textual_len();
    let end = cursor + len;
    let range = cursor..end;
    match segment {
        Segment::Text(_, styles) => {
            shape_range(&mut items, engine, text, &bidi, range, styles);
        }
        Segment::Item(item) => items.push((range, item)),
    }
    cursor = end;
}
```

`cursor` 在全文上单调推进，保证每个 item 的 `Range` 不重叠地覆盖整段字符串——这是 `indices` 与后续 `slice` 能正确工作的不变量。

**(c) indices 反查表**

[src/inline/prepare.rs:104-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L104-L107)：

```rust
let mut indices = Vec::with_capacity(text.len());
for (i, (range, _)) in items.iter().enumerate() {
    indices.extend(range.clone().map(|_| i));
}
```

即「每个字节偏移都写上它所属 item 的下标」。于是给定任意字节偏移，`indices[offset]` 直接 O(1) 给出 item 下标——`Preparation::get` 就靠它（[src/inline/prepare.rs:34-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L34-L38)）。这是个空间换时间的表（长度等于字节数），但让断行阶段的频繁查找变快。

**(d) Preparation::slice：按行区间取 item**

断行算出某行的字节区间 `[start, end)` 后，`slice()` 返回与该区间相交的 items（[src/inline/prepare.rs:41-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L41-L59)）。这把「字符串上的行」翻译成「item 序列」，是 linebreak 与 finalize 之间的桥梁（详见 u5-l3/u5-l5）。

**(e) CJK-Latin 间距**

若 `config.cjk_latin_spacing` 为真，`add_cjk_latin_spacing` 在 Han（中日韩）字符与西文字母之间注入 1/4 em 的额外间距（[src/inline/prepare.rs:109-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L109-L111) 调用 [L126-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L126-L175) 实现）。它在整形后的 `ShapedGlyph` 上直接改 `x_advance`/`x_offset`，并登记 `shrinkability`（可压缩量），供两端对齐时回缩。这是中文排版「中英文混排留隙」规范的实现（注释引用了 *Requirements for Chinese Text Layout* §3.2.2）。

#### 4.3.4 代码实践

> **实践目标**：把 `Preparation` 的三张「映射表」串起来，看清字节偏移如何同时映射到 item、Span 与字形。

**操作步骤（源码阅读型实践）**：

1. 阅读 [src/inline/prepare.rs:113-121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs#L113-L121) 的 `Ok(Preparation { ... })`，确认五张「表」：`text`（字符串本身）、`items`（带区间的 item）、`indices`（字节→item 下标）、`spans`（字节→Span）、`bidi`（可选）。
2. 画一条字节轴，标出 collect 的产物 `"Hello \u{FFFC}!"` 的字节区间：
   - `"Hello "` 占 6 字节 → `Text` item（经 shape_range 整形为一个 LTR run）。
   - `"\u{FFFC}"` 占 3 字节 → `Item::Frame`。
   - `"!"` 占 1 字节 → `Text` item。
3. 在这条轴上验证三张表的一致性：对偏移 `6`（即 `\u{FFFC}` 首字节），`indices[6]` 应指向 Frame item；`spans.span_at(6)` 应返回该 `#box` 的源码 Span。

**需要观察的现象**：同一字节偏移，经 `indices` 得到「排版对象」，经 `spans` 得到「源码位置」，经 `bidi.levels` 得到「方向」。`Preparation` 把这三者对齐到同一条字节轴上，是后续断行能高效工作的前提。

**预期结果**：你应当能用一句话说清——**`Preparation` 是以「全文字节偏移」为主键的一张连接表**，把字符串、整形结果、方向、源码位置四类信息钉在一起。若要本地验证，可在 `prepare` 返回前临时 `eprintln!("items={:?}", p.items.len())` 观察一个含 `#box` 的段落 items 数量是否符合预期（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Segment::Text` 在 prepare 里可能「一个变多个」item，而 `Segment::Item` 永远 1:1？

> **答案**：`Segment::Text` 内部可能跨越 BiDi level 边界或 script 边界，`shape_range` 会据此把它切成多个 `Item::Text`（每个方向、script 一致）。而 `Segment::Item`（间距、盒子、tag 等）在收集时已是不可分原子，没有内部方向结构，所以 1:1 收进 `items`。

**练习 2**：`Preparation::get(offset)` 用 `indices` 做 O(1) 查找。如果改成在 `items` 上线性扫描每个 `Range`，复杂度会怎样？为什么 Typst 愿意花 `text.len()` 的空间建 `indices`？

> **答案**：线性扫描是 O(items 数)，而断行阶段会对**每个候选断点**反复查 item，总开销可能接近 O(断点数 × items 数)，对长段落很贵。`indices` 用「每字节一个 usize」的空间把查找降到 O(1)，对频繁断行的段落排版是划算的权衡。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个**追踪型综合任务**。

**任务**：给定段落源码

```typst
#set text(dir: rtl)
这是一段中文 Hello 混排 #box[盒] 文字
```

请按 `collect → prepare` 的顺序，手动推演并填写下表（**待本地验证**你的推演）：

1. **collect 阶段**：写出 `full` 字符串（用 `\u{FFFC}` 表示盒子占位），列出 `segments` 序列（标注哪些相邻文本段会合并）。
2. **BiDi 判定**：基准 level 是多少？`Hello` 这一段会得到什么方向的 level？`is_bidi` 是 true 还是 false？
3. **prepare 阶段**：`items` 会有几个元素？`#box[盒]` 对应哪个 item、占多少字节？`indices` 在该区间填什么下标？
4. **回溯**：若 `Hello` 里缺了某个字形触发警告，Typst 如何经 `SpanMapper` 把警告指回源码第几行？

**参考思路**：

- `full` 形如 `"这是一段中文 Hello 混排 \u{FFFC} 文字"`（中文与英文、空格的边界处理可结合 `push_text` 的合并规则推演）。
- 基准 `default_level = 1`（rtl）。`Hello` 的拉丁字母会被抬到偶数 level（LTR 方向），故 `is_bidi = true`。
- `#box[盒]` 走 [BoxElem 定宽分支](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs#L227-L232)，立即排成一帧，对应 `Item::Frame`，字符串里占 3 字节（`\u{FFFC}`），`indices` 在这 3 个字节上填该 Frame 的下标。
- 字形缺失时，整形阶段知道缺失发生在 `ShapedText.base + 字形 cluster 偏移` 处的字节，经 `spans.span_at(该字节)` 得到 `Span`，再由 `Span` 解析回源码行列。

完成后再阅读 [layout_inline_impl 的 collect/prepare 两行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/mod.rs#L167-L171)，确认你推演的产物正是这两行所交接的。

## 6. 本讲小结

- `collect` 把异构 `&[Pair]` **压扁成一条逻辑字符串**，非文本内容用占位符顶替：间距→空格 ` `、盒子/图片→对象替换符 `\u{FFFC}`、标签→空串；真正的样式与几何存进 `Segment` / `Item`。
- **必须先收集全段文本**才能算 BiDi，因为一个字符的方向 level 取决于整段上下文——这是「拼成一整段字符串」的根本原因。
- `Item::Frame`（定宽 box）在 collect 阶段就**立即排版**，而 `Item::Fractional`（Fr 宽 box）**延后**到断行阶段，体现「顺序归 collect、宽度归断行」的分工。
- `unicode-bidi` 给每个字节打 level（偶数 LTR / 奇数 RTL），`shape_range` 在 **level 与 script 边界**切分文本 run，使每个 `ShapedText` 方向、字体一致；单向段落经 `is_bidi` 优化后不携带 `BidiInfo`。
- `prepare` 把 `segments` 装配成 `items: Vec<(Range, Item)>`，并建 `indices`（字节→item 下标，O(1) 查找）与沿用 `SpanMapper`（字节→源码 Span），形成以**字节偏移为主键**的连接表。
- `SpanMapper` 让字符串上的任何字节都能回溯到源码 `Span`，是编译诊断与追踪（`traced`）能精确定位段落内部问题的基石。

## 7. 下一步学习建议

本讲把 `Preparation` 交到了断行阶段手上。下一篇 **u5-l3 换行算法 linebreak** 将讲解 Typst 如何在 `Preparation` 上选择断点：simple（first-fit）与 optimized（Knuth-Plass 风格）两种策略、ICU segmenter 确定可断点、以及 cost/badness/penalty 如何衡量一行的好坏。

如果你对整形细节（`ShapedText` 怎么来、字体回退、连字、`safe_to_break` 如何让断行复用整形结果）更感兴趣，可以平行阅读 **u5-l4 文本整形 shaping**，它会展开本讲里只点到 `shape_range`/`shape` 的部分。

建议同时打开下面两个文件对照阅读，巩固本讲：

- [src/inline/collect.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/collect.rs) —— 重读分派循环与替换字符表。
- [src/inline/prepare.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/inline/prepare.rs) —— 重读 `prepare` 主体与 `Preparation` 的三个查询方法 `get` / `slice` / `span_at`。
