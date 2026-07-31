# 页面导出：PageSettings、出血框与页码标签

## 1. 本讲目标

在上一讲（u2-l5）里，我们把 `convert()` 的 13 步编排拆成了「准备 / 转换 / 收集 / 收尾」四阶段。本讲进入转换阶段的第一个动作：**如何把 Typst 排好版的每一页落成 krilla 的 `PageSettings`，并把它交给 krilla 去开页、绘制内容**。

学完本讲你应该能够：

1. 理解 `PageIndexConverter` 如何在 `page_ranges`（只导出部分页面）场景下做「逻辑页号 → PDF 页号」的重映射。
2. 看懂页面尺寸的最小值约束（3×3 单位），以及出血框（bleed）如何扩充 MediaBox、又如何用 trim box 标记最终裁切区域。
3. 掌握 `PageLabelExt::generate` 如何把 Typst 的 `Numbering` 翻译成 PDF 原生支持的 `PageLabel`，以及哪些情况下不得不「退化为纯前缀字符串」。
4. 把上述三块串成 `convert_pages()` 的完整执行流程，并能用具体数字追踪一个真实页面。

## 2. 前置知识

在阅读本讲前，建议你已经理解 u2-l5 中关于 `convert()` 编排、`GlobalContext`、`SerializeSettings` 的内容。本讲只补充三个本讲特有的概念。

### 2.1 PDF 的「页面边界框（page boundaries）」

PDF 规范为一页定义了多种矩形边界，最常用的有：

- **MediaBox**：页面的物理媒介大小，也就是整张纸的范围（krilla 里由 `PageSettings` 的宽高决定）。
- **BleedBox**：印刷出血区域，通常比最终成品略大，保证裁切后没有白边。
- **TrimBox**：**最终裁切后的成品尺寸**，印刷厂沿这条线裁切。
- CropBox / ArtBox：本讲用不到。

typst-pdf 只关心两个：MediaBox（页面总尺寸）和 TrimBox（成品区域）。当用户设置了非零的 bleed 时，MediaBox 会比正文区域大一圈，而 TrimBox 则标记正文区域的位置，告诉阅读器/印刷厂「真正的内容边界在这里」。

### 2.2 Typst 的页码与编号（Numbering）

Typst 里一页有两个跟「编号」相关的字段（定义在上游 `typst-layout` 的 `Page` 上）：

- `number: u64`：**逻辑页号**，由 `counter(page)` 控制，可以和物理页号不一致（比如封面不计数、从第 3 页开始等）。
- `numbering: Option<Numbering>`：页码的**编号格式**，比如阿拉伯数字 `"1"`、罗马数字 `"i"`、带前缀的 `"A-"` 等。

`Numbering` 是个枚举，本讲只关心它的 `Pattern(NumberingPattern)` 变体，其结构为：

```rust
pub struct NumberingPattern {
    pub pieces: EcoVec<(EcoString, NamedNumeralSystem)>,  // (前缀, 数字系统)
    pub suffix: EcoString,                                // 后缀
    trimmed: bool,
}
```

一个 `NumberingPattern` 由若干「片段」组成，每个片段是「前缀字符串 + 数字系统」对，外加一个可选后缀。例如 `"A-1"` 会被解析成一个片段（前缀 `"A-"`，阿拉伯数字系统）。本讲的难点之一就是：**Typst 的编号表达能力比 PDF 原生页码标签更丰富，导出时要决定哪些能用 PDF 原生格式、哪些只能退化为字符串**。

### 2.3 krilla 的 PageLabel 三要素

krilla（以及 PDF 规范）的 `PageLabel` 只有三样东西：

- **style**：数字系统，如 `Arabic` / `LowerRoman` / `UpperRoman` / `LowerAlpha` / `UpperAlpha`，或 `None`（纯字符串标签）。
- **prefix**：一个字符串前缀。
- **offset**：从哪个数字开始计数（`NonZeroU32`，即不能为 0）。

这三样东西决定了 PDF 阅读器在侧边栏里如何显示这页的页码标签。理解了这个限制，你就理解了 `PageLabelExt` 为什么要做那么多判断。

## 3. 本讲源码地图

本讲涉及的真实源码文件如下：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-pdf/src/convert.rs` | `convert_pages()` 负责逐页建页、算尺寸/出血/标签、调起 `handle_frame`；`PageIndexConverter` 负责页号过滤与重映射 |
| `crates/typst-pdf/src/page.rs` | `PageLabelExt` trait，把 Typst `Numbering` 翻译成 krilla `PageLabel` |
| `crates/typst-layout/src/document.rs` | `Page` 结构体定义（`frame` / `bleed` / `numbering` / `number` / `fill_or_transparent`）——理解输入长什么样 |
| `crates/typst-library/src/layout/page.rs` | `PageRanges::includes_page_index`——`page_ranges` 判断某页是否导出的依据 |
| `crates/typst-library/src/model/numbering.rs` | `NumberingPattern` 与 `Numbering` 的定义——`PageLabelExt` 调用的 `apply` 在这里 |

## 4. 核心概念与源码讲解

### 4.1 页号过滤与重映射：PageIndexConverter

#### 4.1.1 概念说明

当用户用 `pdf(..., page_ranges: "1, 3-5")` 只导出文档的一部分页面时，会立刻出现一个问题：**Typst 文档里第 1 页是第 1 页，但导出的 PDF 里「第 3 页」会变成 PDF 的「第 2 页」**——因为被跳过的页面不留空位，后面的页面要往前挪。

typst-pdf 用一个专门的 `PageIndexConverter` 来管理这个映射。它在 `convert()` 一开始就被构造好，存进 `GlobalContext`，此后所有需要「Typst 页号 ↔ PDF 页号」转换的地方都问它要。它的职责有三：

1. 判断某页是否被导出（`pdf_page_index` 返回 `None` 即跳过）。
2. 给出某页导出后在 PDF 里的实际索引（重映射）。
3. 记录「是否有页面被跳过」这一全局事实（`has_skipped_pages`），供页码标签逻辑使用。

#### 4.1.2 核心流程

`PageIndexConverter::new` 遍历文档每一页，做一遍预处理：

- 对第 `i` 页，若 `page_ranges` 存在且**不包含**该页，就把它计入 `skipped_pages`，**不写入映射表**。
- 否则，把它映射到 `i - skipped_pages`，即「前面跳过了多少页，就把当前页往前挪多少」。

`pdf_page_index(i)` 则只是查表：能查到就是 PDF 页号，查不到（`None`）说明这页不导出。

用伪代码描述：

```
PageIndexConverter::new(document, options):
    for i in 0..页数:
        if page_ranges 存在 且 不含第 i 页:
            skipped_pages += 1        # 这页不导出，但累计跳过数
        else:
            page_indices[i] = i - skipped_pages   # 保留页：PDF 页号 = 原页号 - 跳过数
```

举例：文档有 5 页，`page_ranges: "1, 3-5"`（导出第 1、3、4、5 页，跳过第 2 页，索引分别是 0、2、3、4）。预处理结果：

| Typst 页索引 i | 是否含于 ranges | skipped_pages（累计） | 写入映射 |
| --- | --- | --- | --- |
| 0 | 是 | 0 | 0 → 0 |
| 1 | **否（跳过）** | 1 | （不写入） |
| 2 | 是 | 1 | 2 → 1 |
| 3 | 是 | 1 | 3 → 2 |
| 4 | 是 | 1 | 4 → 3 |

最终 `page_indices = {0:0, 2:1, 3:2, 4:3}`，`skipped_pages = 1`。也就是说，Typst 的第 2 页（索引 1）消失，第 3 页（索引 2）在 PDF 里变成第 2 页（索引 1）。

#### 4.1.3 源码精读

`PageIndexConverter` 的结构只有两个字段，一个映射表、一个计数器：

[convert.rs:887-890](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L887-L890) —— `page_indices` 存「保留页」的 Typst 索引 → PDF 索引映射，`skipped_pages` 累计跳过数。

构造函数的关键是第 898-902 行那段判断：

[convert.rs:893-910](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L893-L910) —— 注意 `options.page_ranges.as_ref().is_some_and(...)`：只有当用户**显式设置了** `page_ranges` 时才会过滤；默认 `None` 时所有页都进入映射、`skipped_pages` 始终为 0。第 905 行 `page_indices.insert(i, i - skipped_pages)` 正是「往前挪」的重映射。

判断「是否包含某页」的底层逻辑在 `typst-library` 的 `PageRanges` 里：

[page.rs:786-794](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L786-L794) —— `includes_page_index` 把 0 基索引转成 1 基页号，再对每个 `PageRange`（`RangeInclusive<Option<NonZeroUsize>>`）判断。四个分支对应：闭区间 `(start..=end)`、`start..` 到末尾、开头到 `..=end`、以及 `(None, None)` 表示「全部页」。注意第 787 行 `page + 1`，因为 ranges 是 **1 基**页号、而内部索引是 **0 基**。

`pdf_page_index` 与 `has_skipped_pages` 都是一行实现：

[convert.rs:912-919](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L912-L919) —— `pdf_page_index` 查表，`None` 表示该页不导出；`has_skipped_pages` 只要跳过过至少一页就返回 `true`。

#### 4.1.4 代码实践

**实践目标**：用具体数字验证 `PageIndexConverter` 的重映射逻辑。

**操作步骤**：

1. 假想一个 6 页文档，导出范围是 `"2, 4-6"`（即跳过第 1、3 页，0 基索引为 0 和 2）。
2. 按照 `PageIndexConverter::new` 的逻辑，手算每页的映射与 `skipped_pages`。
3. 用下面这张表对照你的结果。

| i（0 基） | 含于 ranges？ | skipped_pages 累计 | page_indices |
| --- | --- | --- | --- |
| 0 | 否（第 1 页跳过） | 1 | — |
| 1 | 是（第 2 页） | 1 | 1 → 0 |
| 2 | 否（第 3 页跳过） | 2 | — |
| 3 | 是（第 4 页） | 2 | 3 → 1 |
| 4 | 是（第 5 页） | 2 | 4 → 2 |
| 5 | 是（第 6 页） | 2 | 5 → 3 |

**预期结果**：`page_indices = {1:0, 3:1, 4:2, 5:3}`，`skipped_pages = 2`，`has_skipped_pages()` 返回 `true`。Typst 的第 4 页（索引 3）变成 PDF 的第 2 页（索引 1）。

**需要观察的现象**：跳过的页只是「不写入映射」而非「占位」，所以保留页的 PDF 索引会被连续压缩，不存在空洞。

#### 4.1.5 小练习与答案

**练习 1**：如果 `page_ranges` 为 `None`（默认），一个 10 页文档的 `page_indices` 是什么？`has_skipped_pages()` 返回什么？

> **答案**：`page_indices = {0:0, 1:1, 2:2, ..., 9:9}`（恒等映射），`skipped_pages = 0`，`has_skipped_pages()` 返回 `false`。因为第 898 行的 `is_some_and` 在 `None` 时直接短路为 `false`，永远不会进入跳过分支。

**练习 2**：为什么 `skipped_pages` 要用一个可变计数器边遍历边累加，而不是导出结束后一次性算？

> **答案**：因为重映射值 `i - skipped_pages` 依赖的是「**到第 i 页为止**」跳过的页数，而不是全局总跳过数。边累加边写入，才能让每一保留页都正确前移它之前被跳过的那么多格。这也是为什么 `skipped_pages` 在 `else` 分支里被使用、在 `if` 分支里被自增。

---

### 4.2 页面尺寸、最小约束与出血框（trim box）

#### 4.2.1 概念说明

知道哪些页要导出、它们的新页号之后，`convert_pages()` 要为每一页构造 krilla 的 `PageSettings`——也就是 PDF 的 MediaBox 及各类边界框。这里有两件需要处理的事：

1. **最小尺寸约束**：PDF 1.4–1.7 规定页面最小 3×3 单位；PDF 2.0 没有硬性规定，但 krilla 和多数阅读器无法处理零尺寸页面。typst-pdf 用 `.max(3.0)` 兜底。
2. **出血框（bleed）**：当用户设置了非零 bleed（印刷出血）时，页面要在四周各扩出 bleed 的量，正文内容居中；同时设置 trim box 标记真正的成品边界。

输入来自上游 `typst-layout` 的 `Page` 结构体，关键事实是：**`frame` 的尺寸不含 bleed，bleed 是四周要额外附加的量**。

#### 4.2.2 核心流程

设 frame 宽高为 \(W_{\text{frame}}, H_{\text{frame}}\)，四向 bleed 为 \(b_l, b_r, b_t, b_b\)。则：

页面总尺寸（MediaBox）：

\[
W_{\text{page}} = \max(3.0,\ W_{\text{frame}} + b_l + b_r)
\]

\[
H_{\text{page}} = \max(3.0,\ H_{\text{frame}} + b_t + b_b)
\]

当 bleed 不全为零时，trim box（成品区）在「左上为原点」坐标系里（krilla 的 `Rect::from_ltrb(left, top, right, bottom)`）：

\[
\text{TrimBox} = (b_l,\ b_t,\ b_l + W_{\text{frame}},\ b_t + H_{\text{frame}})
\]

直观地说：MediaBox 是「frame 四周再各加一圈 bleed」的大矩形；trim box 是「从左上 bleed 处开始、宽高正好等于 frame」的内嵌矩形。内容（`handle_frame`）会被平移到 \((b_l, b_t)\) 的位置，正好落在 trim box 内。

#### 4.2.3 源码精读

先看输入。上游 `Page` 把 frame、bleed、页码、编号等都摆在明面上，且明确说明 bleed **不**包含在 frame 内：

[document.rs:83-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L83-L105) —— 注意第 86-88 行 `bleed` 字段的注释：「The bleed amount to be added on each side of the page. The bleed is not included in frame.」第 104 行 `number: u64` 是逻辑页号，第 99 行 `numbering` 是编号格式。

`convert_pages()` 里的尺寸计算是本模块的核心。这一段同时处理了「加上四向 bleed」「`max(3.0)` 兜底」「对非法尺寸报内部错误」三件事：

[convert.rs:107-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L107-L116) —— 宽 = `frame.width + bleed.left + bleed.right`，高 = `frame.height + bleed.top + bleed.bottom`，各自 `.to_f32().max(3.0)`。`expect_internal` 把「理论上不会发生的尺寸错误」转成内部断言，`.at(Span::detached())?` 则给它挂一个（ detached 的）span 以满足 Typst 错误体系的要求。

接着是 trim box。**只有 bleed 非零时才设置**——避免给一个和 MediaBox 完全相同的 trim box（无意义的冗余）：

[convert.rs:118-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L118-L125) —— `Rect::from_ltrb(left=bleed.left, top=bleed.top, right=bleed.left+frame.width, bottom=bleed.top+frame.height)`。`Sides::is_zero()` 在四向全为 0 时返回 `true`，此时跳过 trim box。

> 补充：bleed 还会作为 `padding` 传给 `handle_frame`（见 4.4 节），让正文内容平移到 \((b_l, b_t)\) 处，从而正确落在 trim box 内部。背景填充也会扩展为 `frame.size + bleed.sum_by_axis()`，铺满整个 MediaBox。

#### 4.2.4 代码实践

**实践目标**：手算一个带出血框的页面的 `PageSettings`。

**操作步骤**：设一页 A4 纵向，frame 尺寸为 \(595 \times 842\) pt（A4），bleed 四向均为 3pt。

1. 按 4.2.2 的公式算出 MediaBox 宽高。
2. 算出 trim box 的四个坐标。

**预期结果**：

- MediaBox 宽 = \(595 + 3 + 3 = 601\) pt，高 = \(842 + 3 + 3 = 848\) pt（均 > 3，`max(3.0)` 不生效）。
- `PageSettings::from_wh(601.0, 848.0)`。
- bleed 非零，故设置 trim box = `Rect::from_ltrb(3.0, 3.0, 3.0+595=598.0, 3.0+842=845.0)`。

**需要观察的现象**：trim box 的左上角正好是 `(bleed.left, bleed.top)`，宽高正好等于 frame；正文绘制时会被平移到 `(3, 3)`，从而四周各留 3pt 出血。

#### 4.2.5 小练习与答案

**练习 1**：如果某页 frame 宽为 0（比如一个极其异常的空页），`convert_pages` 会得到多大的 MediaBox？为什么不会 panic？

> **答案**：MediaBox 宽 = `max(3.0, 0 + bleed_left + bleed_right)`。若 bleed 也为 0，则为 `max(3.0, 0.0) = 3.0`，所以页面被钳制为最小 3 单位，不会出现零尺寸页；这正是 PDF 规范与 krilla 对最小尺寸要求的兜底。`expect_internal` 只在 `PageSettings::from_wh` 本身因非法值返回 `Err` 时才触发，`max(3.0)` 已确保不会进入那个分支。

**练习 2**：为什么 trim box 用 `from_ltrb`（左、上、右、下）而不是 `from_xywh`（左、上、宽、高）？

> **答案**：因为右、下两个值正好是 `bleed.left + frame.width` 和 `bleed.top + frame.height`，用 `from_ltrb` 能直接、直观地把「左上原点 + frame 宽高」表达成一个矩形，语义上和 PDF 的 trim box（一个矩形边界）完全对应。两者等价，`from_ltrb` 这里更贴合「成品边界」的直观含义。

---

### 4.3 页码标签：PageLabelExt::generate / arabic

#### 4.3.1 概念说明

页面的 MediaBox 和 bleed 处理完之后，`convert_pages()` 还要为页面设置**页码标签**（PageLabel），它决定 PDF 阅读器侧边栏显示的页码文字（例如 `1, 2, 3` 或 `i, ii, iii` 或 `A-1, A-2`）。

这件事的难点在于：**Typst 的 `NumberingPattern` 比 PDF 原生页码标签更强大**。PDF 的 `PageLabel` 只有「style + prefix + offset」三要素，而 Typst 允许后缀、多个片段、复杂的数字系统。所以 `PageLabelExt::generate` 的核心策略是：

> **能复用 PDF 原生格式就复用（style + offset + prefix）；复用不了就把整段编号渲染成一个字符串塞进 prefix，style 置空。**

这就是代码里反复出现的「common style optimization（公共样式优化）」。

#### 4.3.2 核心流程

`generate(numbering, number)` 的决策流程：

```
1. 只处理 Numbering::Pattern(pat)；其它（如函数式 Numbering）返回 None。
2. 取第一个片段 (prefix, system)。
3. 若没有后缀 且 数字系统是 PDF 原生支持的：
       style = 对应的 PDF NumberingStyle   # 走"优化"路径
   否则：
       style = None                          # 退化路径
4. 若 style 为 None（退化）：
       prefix 字符串 = pat.apply(None, [number])   # 把整个编号渲染成字符串
   否则（优化）：
       prefix = 仅当用户前缀非空时保留
5. offset = style 存在 且 number 能转成 NonZeroU32 时才设置
6. 返回 PageLabel::new(style, prefix, offset)
```

PDF 原生支持的数字系统对应关系（仅当无后缀时）：

| Typst `NamedNumeralSystem` | PDF `NumberingStyle` | 额外条件 |
| --- | --- | --- |
| Arabic | Arabic | 无 |
| LowerRoman | LowerRoman | 无 |
| UpperRoman | UpperRoman | 无 |
| LowerLatin | LowerAlpha | `number <= 26` |
| UpperLatin | UpperAlpha | `number <= 26` |

拉丁字母之所以有 `number <= 26` 的限制：拉丁字母只有 26 个，PDF 原生 alpha 编号在超过 26 时会变成 `AA, BB...`（与 Typst 的 `aa, ab...` 不一致），所以一旦超过就**不使用**原生 style，退化为整串渲染。

#### 4.3.3 源码精读

`PageLabelExt` trait 定义了两个方法，一个通用、一个快捷：

[page.rs:6-15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/page.rs#L6-L15) —— `generate` 把任意 `Numbering` 翻成 `PageLabel`，`arabic` 则是「直接给一个阿拉伯数字标签」的快捷方式（用于跳过页时的回退，见 4.3.4）。

`generate` 的完整实现：

[page.rs:18-53](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/page.rs#L18-L53)

逐段解读：

- 第 19-21 行：只处理 `Numbering::Pattern`，函数式 `Numbering::Func` 直接返回 `None`（PDF 无法表达任意闭包）。
- 第 23 行：取 `pieces.first()`，只看**第一个**片段；`?` 表示多片段模式（如 `"1/1"`）直接放弃样式优化走 `None`。
- 第 27-40 行：核心的「能否用原生 style」判断。`pat.suffix.is_empty()` 是前置条件——一旦有后缀，PDF 无处安放，只能整体退化为字符串。

  [page.rs:30-37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/page.rs#L30-L37) —— 拉丁字母带 `number <= 26` 守卫，`_ => None` 兜住所有「不支持的系统」。

- 第 45-49 行：prefix 的两套来源。退化路径（`style.is_none()`）调用 `pat.apply(None, &[number])` 把整段编号渲染成字符串作为 prefix；优化路径只在用户前缀非空时保留前缀。

  [page.rs:45-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/page.rs#L45-L49) —— 注意 `pat.apply` 的第一个参数 `None` 表示「不带 engine/warning context」；它在 `typst-library` 的 `NumberingPattern` 上定义：

  [numbering.rs:170-173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/numbering.rs#L170-L173)

- 第 51 行：`offset = style.and(number.try_into().ok().and_then(NonZeroU32::new))`——只有走优化路径（`style` 是 `Some`）且 `number` 能转成非零 u32 时才设 offset；否则为 `None`。退化路径下 offset 必为 `None`（编号已整体塞进 prefix）。

`arabic` 是给「跳过页」回退用的快捷方法，直接构造一个阿拉伯标签：

[page.rs:55-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/page.rs#L55-L61) —— style 固定 `Arabic`、prefix 为 `None`、offset 为 `number`（若能转成非零 u32）。

#### 4.3.4 代码实践

**实践目标**：追踪几种典型 `Numbering` 在 `generate` 里走哪条分支、最终产生什么 `PageLabel`。

**操作步骤**：对下面四个例子，分别判断 `style`、`prefix`、`offset` 三要素，并说出 PDF 侧边栏会显示什么。

1. `"1"`（阿拉伯数字，无前缀无后缀），页号 `number = 5`。
2. `"i"`（小写罗马，无后缀），`number = 3`。
3. `"A-"`（大写拉丁带前缀……实际解析：前缀 `""` + UpperLatin 系统），`number = 2`。
4. `"1."`（阿拉伯数字**带后缀** `"."`），`number = 4`。

**预期结果**：

| 例子 | suffix 是否空 | style | prefix | offset | 侧边栏显示（number） |
| --- | --- | --- | --- | --- | --- |
| `"1"` | 是 | Arabic | None | 5 | `5` |
| `"i"` | 是 | LowerRoman | None | 3 | `iii` |
| `"A"`（UpperLatin, n=2） | 是 | UpperAlpha | None | 2 | `B` |
| `"1."` | **否** | None | `"4."`（整串渲染） | None | `4.` |

**需要观察的现象**：

- 前三个走「优化路径」，PDF 原生 style 生效，阅读器会按 PDF 规则重新生成页码（罗马 `iii`、拉丁 `B`）。
- 第四个因为带后缀 `"."`，PDF 无法表达，退化为「把整段 `4.` 当成 prefix 字符串」，style 与 offset 都为 `None`。这种标签在不同页之间是纯字符串、不会自动递增。

**待本地验证**：上述「侧边栏显示」取决于具体 PDF 阅读器对 PageLabel 的渲染。建议本地用 Typst 生成一份带这四种编号的多页文档，导出 PDF 后在阅读器（如 SumatraPDF / Acrobat）侧边栏对照确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么拉丁字母系统要加 `number <= 26` 的守卫，而罗马数字不用？

> **答案**：因为 PDF 规范的 alpha 编号（`/a`、`/A`）在超过 26 之后的行为与 Typst 不一致——PDF 会产生 `aa, bb...`（重复字母）或类似模式，而 Typst 的拉丁编号是 `aa, ab, ac...`。为避免 27 页之后页码出错，typst-pdf 在超过 26 时放弃原生 style、退化为整串渲染，由 Typst 自己算出正确的 `aa, ab...` 塞进 prefix。罗马数字则没有这个跨页不一致问题，故不加守卫。

**练习 2**：`generate` 在 `number = 0` 时会怎样？

> **答案**：即便走优化路径（style 是 `Some`），第 51 行的 `NonZeroU32::new(0)` 返回 `None`，于是 offset 为 `None`。最终 `PageLabel::new(Some(Arabic), prefix, None)`——style 仍在但无 offset。实际场景中 Typst 页号从 1 起，`number = 0` 极罕见；这里的设计保证 offset 永远不会是非法的 0。

---

### 4.4 convert_pages() 全流程串联

#### 4.4.1 概念说明

前面三块（页号过滤、尺寸/bleed、页码标签）都是 `convert_pages()` 内部的步骤。这一节我们把它们串起来，看清这个函数作为「逐页导出循环」的完整面貌，以及它如何把每一页的内容绘制交给 `handle_frame`（下一讲 u2-l7 的主题）。

#### 4.4.2 核心流程

`convert_pages` 对文档的每一页执行：

```
for (i, typst_page) in document.pages().enumerate():
    1. 若 PageIndexConverter 说该页不导出 (pdf_page_index(i).is_none()) -> continue
    2. 构造 PageSettings：
       a. 宽高 = frame + 四向 bleed，再 max(3.0)
       b. bleed 非零 -> 设 trim box
       c. 生成 PageLabel（优先用 numbering；否则在"跳过过页"时用 arabic 回退）
    3. document.start_page_with(settings) 开页，拿到 surface
    4. 构造 FrameContext（page_idx + 页面总尺寸含 bleed）
    5. tags::page(...) 包裹 -> handle_frame(...) 绘制本页 Frame 树
    6. surface.finish() + 把本页收集到的链接注记交给 tags
```

第 1 步是 4.1 的 `PageIndexConverter`；第 2 步是 4.2 的尺寸/bleed + 4.3 的页码标签；第 5 步把内容绘制委托给 `handle_frame`（bleed 作为 padding 传入）。

#### 4.4.3 源码精读

完整的 `convert_pages` 函数体：

[convert.rs:97-173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L97-L173)

几个值得单独指出的点：

- **第 99-102 行（跳过判断）**：循环一开始就用 `pdf_page_index(i).is_none()` 决定是否 `continue`。这一行同时完成了「过滤」与「重映射前的存在性检查」。

  [convert.rs:98-102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L98-L102)

- **第 127-145 行（页码标签的三级回退）**：这是一段 `or_else` 链，理解它的优先级很重要。第一优先级是「该页自己定义的 `numbering`」，调用 `PageLabel::generate`；只有当这一步返回 `None`（页面没有 numbering）**且** `has_skipped_pages()` 为真时，才用 `PageLabel::arabic((i + 1) as u64)` 回退——用 1 基的真实页号 `i+1` 作标签。

  [convert.rs:127-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L127-L145) —— 第 131-142 行的注释解释了回退的意图：当一些页被排除时，PDF 物理页号会与 Typst 真实页号不一致；既然无法让它们一致，至少用标签标出「这是原 Typst 文档的第几页」。

- **第 150-153 行（FrameContext 的尺寸）**：`FrameContext::new` 接收的 size 是 `frame.size() + bleed.sum_by_axis()`，即含 bleed 的总尺寸；它作为初始 `State::container_size`，后续渐变/图案（u3-l12）会用到。

  [convert.rs:150-153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L150-L153)

- **第 155-164 行（绘制委托）**：`tags::page` 是一个 hook（在 tagged PDF 子系统里包装页面层标记），它的回调里调用 `handle_frame`，把 `typst_page.frame`、`typst_page.bleed`（作为 padding）、`typst_page.fill_or_transparent()`（背景）传进去。`handle_frame` 是下一讲的主题。

  [convert.rs:155-164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L155-L164)

#### 4.4.4 代码实践

**实践目标**：用伪代码画出 `convert_pages` 对单页的处理顺序，并解释每一步若被跳过的后果。

**操作步骤**：阅读 [convert.rs:97-173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-pdf/src/convert.rs#L97-L173)，为下面每个阶段写一句话注释，说明「若跳过会导致什么」：

1. `pdf_page_index(i).is_none()` 的 `continue`
2. `PageSettings` 的 `max(3.0)`
3. `with_trim_box`
4. `with_page_label`
5. `tags::page + handle_frame`
6. `add_link_annotations`

**预期结果（参考答案）**：

1. 跳过过滤 → 被排除的页仍会进入 PDF，违反 `page_ranges`。
2. 跳过最小尺寸兜底 → 零尺寸或极小页可能让 krilla/阅读器崩溃或拒绝处理。
3. 跳过 trim box → bleed 非零时阅读器无法知道成品边界，印刷裁切失去依据（但页面仍可显示）。
4. 跳过页码标签 → PDF 侧边栏只能显示物理页号 1,2,3…，丢失罗马数字/前缀/「跳过页」的真实页号信息。
5. 跳过 `handle_frame` → 该页内容为空白（只剩 MediaBox）。
6. 跳过 `add_link_annotations` → 本页的超链接注记丢失（链接不可点）。

#### 4.4.5 小练习与答案

**练习 1**：为什么页码标签回退用的是 `(i + 1) as u64`（真实 1 基页号），而不是重映射后的 PDF 页号？

> **答案**：因为回退的目的（见 convert.rs:131-142 的注释）正是「**指出这是原 Typst 文档的第几页**」。重映射后的 PDF 页号是压缩过的连续号（如 1,2,3），无法体现被跳过的页；而 `i+1` 是原文档里的真实位置。例如导出 `page_ranges: "3"` 时，唯一的 PDF 第 1 页标签会显示 `3`，提示读者「这其实是原文档的第 3 页」。

**练习 2**：`has_skipped_pages()` 为 `false` 时（默认全量导出），一个没有设置 `numbering` 的页面，会得到 `PageLabel` 吗？

> **答案**：不会。第 127 行 `numbering.as_ref().and_then(...)` 在 `numbering` 为 `None` 时返回 `None`；随后 `or_else` 里的 `has_skipped_pages()` 在全量导出时为 `false`，`.then(...)` 返回 `None`。整个 `if let Some(label)` 不成立，于是**不调用** `with_page_label`。这与「正常导出时 PDF 物理页号即页码」的预期一致——不需要额外标签。

---

## 5. 综合实践

把本讲三块知识串成一个完整任务。

**任务**：一份 4 页 Typst 文档，页面尺寸为 A4（\(595 \times 842\) pt），第 2 页设置了 3pt 四向出血（bleed），第 2、4 页用罗马数字编号（`numbering: "i"`），其余页不设编号。现以 `page_ranges: "2, 4"` 导出（只保留第 2、4 页）。请回答：

1. **页号重映射**：写出 `PageIndexConverter` 的 `page_indices` 与 `skipped_pages`。
2. **第 2 页的 MediaBox 与 trim box**：列出具体数值（注意该页有 bleed）。
3. **第 2 页的 PageLabel**：说明 `generate` 走哪条分支、style/prefix/offset 各是什么、侧边栏显示什么。
4. **第 4 页的 PageLabel**：第 4 页无 numbering，但有页面被跳过——它的标签是什么？

**参考解答**：

1. 文档索引 0,1,2,3 对应第 1,2,3,4 页。`page_ranges: "2,4"` 保留索引 1 和 3，跳过索引 0 和 2。预处理：i=0 跳过（skipped=1）；i=1 保留 → 1−1=0；i=2 跳过（skipped=2）；i=3 保留 → 3−2=1。故 `page_indices = {1:0, 3:1}`，`skipped_pages = 2`，`has_skipped_pages() = true`。

2. 第 2 页 frame \(595 \times 842\)，四向 bleed 各 3：
   - MediaBox = `from_wh(595+3+3=601, 842+3+3=848)` = `from_wh(601.0, 848.0)`。
   - trim box = `from_ltrb(3.0, 3.0, 598.0, 845.0)`。

3. 第 2 页 `numbering = "i"`（LowerRoman，无前缀无后缀），`number` 为其逻辑页号。`generate`：suffix 空、系统是 LowerRoman → 走优化路径，`style = LowerRoman`、prefix = `None`、offset = `number`。侧边栏显示对应罗马数字（如逻辑页号为 2 则显示 `ii`）。

4. 第 4 页无 `numbering`，但 `has_skipped_pages()` 为 `true`，于是回退到 `PageLabel::arabic((3 + 1) as u64) = PageLabel::arabic(4)`：style=Arabic、prefix=None、offset=4，侧边栏显示 `4`——提示读者「这是原 Typst 文档的第 4 页」（它在 PDF 里其实是第 2 页）。

> **待本地验证**：建议本地构造该文档并用 Typst 导出，用 `pdfinfo` 或阅读器确认 MediaBox/TrimBox 与 PageLabel 实际写入的值。可借助 `qpdf --qdf` 把 PDF 展开成可读文本，检查 `/MediaBox`、`/TrimBox` 与 `/PageLabels` 树。

## 6. 本讲小结

- `PageIndexConverter` 在 `convert()` 启动时一次性预处理出「Typst 页索引 → PDF 页索引」的映射，边遍历边累计跳过数，从而把保留页连续压缩到 PDF 中，不留空洞。
- 页面 MediaBox = `frame 尺寸 + 四向 bleed`，并用 `.max(3.0)` 兜底 PDF 的最小尺寸约束；bleed 非零时另设 trim box（左上为 `(bleed.left, bleed.top)`、宽高等于 frame）标记成品边界。
- `PageLabelExt::generate` 的核心策略是「能用 PDF 原生 style 就用（style+offset+prefix），否则把整段编号渲染成字符串塞进 prefix」；拉丁字母超过 26、带后缀、多片段、函数式编号等都会触发退化。
- 当页面没有 `numbering` 但有页面被跳过时，`convert_pages` 用 `PageLabel::arabic(真实1基页号)` 回退，让阅读器仍能看到原页号。
- `convert_pages()` 把上述三块串成一个逐页循环，过滤 → 构造 PageSettings（尺寸/bleed/标签）→ 开页 → `handle_frame` 绘制内容 → 收尾链接注记。

## 7. 下一步学习建议

本讲只讲清了「页面外壳」——MediaBox、trim box、页码标签。页面里的**内容**（文字、图形、图像、链接）是如何被逐个绘制到 surface 上的，是下一讲 **u2-l7「Frame 遍历器：handle_frame 与 handle_group」** 的主题。建议：

- 先读 `handle_frame`（convert.rs:327-391）与 `handle_group`（convert.rs:393-430），重点理解 `FrameContext` 的状态栈 `push/pop` 与 `State::register_container`。
- 结合本讲提到的「bleed 作为 padding 平移内容」这一点，去 `handle_frame` 第 355-356 行确认内容是如何被 `Transform::translate(padding.left, padding.top)` 推到 trim box 内的。
- 之后进入 u2-l8「类型转换工具集」，再进入 u3 各内容翻译器。
