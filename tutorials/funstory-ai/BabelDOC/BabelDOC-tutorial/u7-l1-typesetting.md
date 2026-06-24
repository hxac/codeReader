# 排版重排：Typesetting

## 1. 本讲目标

到上一讲为止，PDF 已经被解析成带坐标的中间表示 IL，段落被识别、公式被分离、译文也被 LLM 填了回去（见 u6-l2）。但这里藏着一个根本矛盾：**原文是英文（短），译文是中文（长）**。把更长的译文逐字塞回原文每个字符的 `box` 里，必然装不下、彼此重叠、冲出版心。

本讲讲解 midend 流水线倒数第二个 stage **`Typesetting`**（stage_name=`Typesetting`，权重 4.71）。它的工作就是**把译文重新排版、贴着原版面画回 PDF**——这正是 BabelDOC「保结构翻译」优于单栏重排的关键一环。

学完本讲你应能：

1. 说清「逐字符重排」的整体流程：段落拆成统一的 `TypesettingUnit` → 按 CJK/拉丁规则换行 → `relocate`+`render` 还原成 `PdfCharacter` 写回 IL。
2. 理解 CJK 换行规则三件套：`LINE_BREAK_REGEX`（拉丁词不拆）、标点悬挂（`is_hung_punctuation`）、行末禁则/避尾（`is_cannot_appear_in_line_end_punctuation`）。
3. 看懂字体度量为何要做三级缓存（`FontMapper` 的 `lru_cache` → `get_font` → 单元级 cache）。
4. 说清 `rtree` 空间索引在「段落互相避让」里起的作用，以及它为什么比朴素双重循环快。
5. 解释 `matrix_helper.decompose_ctm` 把 PDF 变换矩阵分解成「平移/旋转/缩放/错切」后，对字号提取与向量图形搬迁为何至关重要。

## 2. 前置知识

本讲建立在 u3-l1（IL 数据模型）、u5-l3（段落识别）、u5-l4（公式与样式）、u6-l2（IL 翻译编排）之上。开始前先约定几个概念：

- **PdfParagraph / PdfParagraphComposition**：IL 的段落是「一串 composition」拼成的。每个 composition 可能是一行（`pdf_line`）、单个字符（`pdf_character`）、同样式的字符组（`pdf_same_style_characters`）、同样式的纯文本（`pdf_same_style_unicode_characters`，译文常用）或一个公式（`pdf_formula`）。详见 u3-l1。
- **box**：每个字符/段落/公式都有一个 `Box(x, y, x2, y2)`，是它在页面上的轴对齐包围盒（AABB），坐标原点在左下角、y 向上。
- **CTM（Current Transformation Matrix，当前变换矩阵）**：PDF 内容流里当前生效的 2D 仿射变换，写成 6 个数 `(a,b,c,d,e,f)`。它把「字体内部坐标系」（通常以 1/1000 字号为单位）映射到「页面点坐标」。一个矩阵同时编码了平移、旋转、缩放、错切，揉在一起。
- **字体度量（font metrics）**：一个字符在给定字号下的「前进宽度」（advance width，画完这个字符后游标右移多少）。CJK 字符通常等宽，拉丁字符不等宽。
- **R-tree**：一种空间索引数据结构，把若干矩形按区域聚合成分支节点，支持「查询与某矩形相交/包含的所有矩形」，期望 \(O(\log n)\)。Python 的 `rtree` 库提供了它。

> 直觉：原文每个字符都有自己的 `box`，但译文变长了，原 `box` 已失效。`Typesetting` 做的就是——拿段落的总 `box`（版面给的可用区域）当画布，把译文按 CJK 规则一行行重新铺进去，必要时缩小字号（`scale`）直到塞得下，再把每个字符的新位置写回 IL，交给 backend 渲染。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `babeldoc/format/pdf/document_il/midend/typesetting.py` | 本讲主角。定义 `TypesettingUnit`（单个可排版单元）与 `Typesetting`（stage 入口），含换行规则、布局循环、缩放求解、rtree 避让。 |
| `babeldoc/format/pdf/document_il/utils/fontmap.py` | `FontMapper`：把原字体映射到目标语言字体，并对 `has_glyph`/`char_lengths` 等字体度量做 `lru_cache`。详见 u7-l2，本讲只取其缓存与度量部分。 |
| `babeldoc/format/pdf/document_il/utils/matrix_helper.py` | CTM 工具箱：`decompose_ctm`（分解）、`compose_ctm`（合成）、`create_translation_and_scale_matrix`、`multiply_matrices`。 |
| `babeldoc/format/pdf/high_level.py` | 主编排。`TRANSLATE_STAGES` 登记 `Typesetting`（权重 4.71），`_do_translate_single` 在翻译后调用它。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**逐字符重排 → CJK 换行规则 → 字体度量缓存 → rtree 空间索引**。

### 4.1 逐字符重排：从段落到 TypesettingUnit

#### 4.1.1 概念说明

「逐字符重排」的核心抽象是 **`TypesettingUnit`（排版单元）**。它把段落 composition 里形形色色的内容统一成一种「可测量、可换行、可重定位、可渲染」的原子：

- 一个**原文字符**（`char`，带原始 `box`）→ 可直接透传（passthrough），位置不动。
- 一个**公式**（`formular`，内含多个字符/曲线/表单）→ 当作不可拆的整体，整体平移缩放。
- 一个**译文 unicode 字符**（`unicode`，由 LLM 产出、没有原始 `box`）→ 需要重新选字体、量宽度、定位。

为什么需要这种统一？因为换行算法只关心「这个单元多宽、能不能在这里断行」，不关心它到底是字符还是公式。把三种来源包装成同一个 `TypesettingUnit`，换行循环就只面对一种对象。

#### 4.1.2 核心流程

整个 stage 走「两趟扫描」：

```
typesetting_document(docs)
  ├─ preprocess_document(docs)          # 第一趟：只算最优缩放，不真排版
  │     for 每段:
  │        units = create_typesetting_units(paragraph)
  │        if 全部可透传: optimal_scale = 1.0
  │        else:        optimal_scale = _get_optimal_scale(...)   # 从 1.0 逐级减 scale 直到塞下
  │     取所有 scale 的「众数」→ 把更大的段强制压到众数（统一字号）
  │
  └─ for 每页: render_page(page)
        ├─ 建 rtree 段落索引，上抬重叠段落            # 4.4 模块
        └─ for 每段: render_paragraph(paragraph)
              units = create_typesetting_units(paragraph)
              if 全部可透传: 直接保留原 composition   # 原文段落，不动
              else: retypeset_with_precomputed_scale(...)   # 第二趟：真排版
                     _find_optimal_scale_and_layout(apply_layout=True)
                       └─ _layout_typesetting_units(...)     # 4.2 模块：换行
                             for 每个单元: unit.relocate(x,y,scale)   # 算新 box
                     for 每个单元: unit.render()  → PdfCharacter  写回 composition
```

关键设计是**两趟**。第一趟 `preprocess_document` 只试探「这段译文在不溢出的前提下最大能用多大字号」，把所有段落的最优 scale 汇总成众数、统一全文字号——避免同一文档里有的段落字号大、有的小，观感混乱（见 [typesetting.py:L920-L935](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L920-L935)）。第二趟 `render_paragraph` 才用统一的 scale 真正摆放每个字符。

> 「最优 scale」的求解是逐步逼近的：从 `scale=1.0` 开始尝试布局，塞不下就减——大于 0.6 时每次减 0.05，之后每次减 0.1，直到 `min_scale=0.1`；中途还会尝试把段落 `box` 向下/向右扩展（`get_max_bottom_space`/`get_max_right_space`）借更多空间；全部失败后关闭「英文换行」再试一次（见 [typesetting.py:L941-L1076](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L941-L1076)）。

#### 4.1.3 源码精读

**TypesettingUnit 的三种身份与断言**——构造时强制三选一：

[typesetting.py:L94-L151](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L94-L151) — `__init__` 用 `assert (char is not None) + (formular is not None) + (unicode is not None) == 1` 保证单元只能是「字符 / 公式 / 译文 unicode」之一。`unicode` 分支额外要求 `font_size`、`style`、`xobj_id`，因为译文字符没有原始 box，必须自带度量参数。这些代码用 `assert` 做内部不变量校验。

**把段落 composition 拆成单元**——`create_typesetting_units`：

[typesetting.py:L1458-L1555](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1458-L1555) — 遍历 `pdf_paragraph_composition`，按 composition 的实际类型分别产出单元。对译文常用的 `pdf_same_style_unicode_characters`，它对串里**每个字符**单独调 `self.font_mapper.map(font, char_unicode)` 选字体，再各包成一个 `TypesettingUnit(unicode=...)`。这就是「逐字符」的由来——译文文本被拆到字级，每个字独立量宽、独立定位。末尾的过滤会丢掉映射不到字体的单元（`x.font is not None`）。

**重定位 relocate**——把单元摆到新坐标并缩放：

[typesetting.py:L490-L655](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L490-L655) — `relocate(x, y, scale)` 返回一个**新的** `TypesettingUnit`（不原地改），新 box 由 `Box(x, y, x + width*scale, y + height*scale)` 算出，字号同步乘 `scale`。公式分支更复杂：它遍历公式内每个字符，按相对原公式左下角的偏移 `rel_x/rel_y` 加上 `x_offset/y_offset`（u5-l4 算出的行内公式偏移）再乘 scale，整体平移缩放，最后 `update_formula_data` 重算包围盒。搬迁完通过 `try_resue_cache(self)` 把布尔判定缓存搬运过去，避免重算。

**渲染 render**——把摆好位的单元变回 `PdfCharacter`：

[typesetting.py:L789-L843](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L789-L843) — `render()` 返回 `(chars, curves, forms)`。可透传单元直接 `passthrough()`；译文 unicode 单元则用 `self.font.has_glyph(ord(self.unicode))` 取字形编号当 `pdf_character_id`，用 `self.width` 当前进宽度，组装出一个带新 box 的 `PdfCharacter`。这些字符最终被写回 `paragraph.pdf_paragraph_composition`，交给 backend 画。

**stage 入口与编排位置**：

[high_level.py:L70](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L70) — `TRANSLATE_STAGES` 里 `(Typesetting.stage_name, 4.71)`，紧跟在翻译（46.96）之后、加字体（0.61）之前。

[high_level.py:L1038](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1038) — `Typesetting(translation_config).typesetting_document(docs)` 在 `_do_translate_single` 中被调用，操作的就是那份贯穿全流程的 `docs`（IL Document）。

#### 4.1.4 代码实践

**目标**：动手感受「逐字符量宽 + 摆位」，理解为什么译文必须拆到字级。

**操作步骤**（阅读型 + 可选运行）：

1. 打开 [typesetting.py:L1458-L1555](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1458-L1555)，确认译文串是被 `for char_unicode in ...unicode` 逐字拆分的。
2. 打开 `--debug` 翻译一个 PDF（运行方式见 u1-l2），翻译完成后找到工作目录下的 `typsetting.json`（[high_level.py:L1040-L1044](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1040-L1044) 就是它的落盘点；注意源码里这个文件名是 `typsetting.json`，少了一个字母 e）。
3. 在该 JSON 里定位一个译文段落，观察它的 `pdf_paragraph_composition`：译文被存成 `pdf_same_style_unicode_characters`（整段 unicode 串）或 `pdf_character`（逐字），且每个字符的 `box` 坐标是排版后重新算出的、彼此不再重叠。

**需要观察的现象**：译文字符的 `box.x` 从左到右递增、`box.y` 在同一行内基本一致、换行处 `y` 跳变——这正是 `_layout_typesetting_units` 摆位的痕迹。

**预期结果**：能从 `typsetting.json` 中读出「译文按行重排」的结构。若无法本地运行翻译，标注**待本地验证**，可改为纯阅读：对照 `relocate` 与 `render`，画出「unicode 串 → 单元 → 新 PdfCharacter」的数据流。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `relocate` 要返回一个**新**的 `TypesettingUnit`，而不是原地修改？

> **答案**：因为同一个单元会在「试探不同 scale」时被反复布局（`_find_optimal_scale_and_layout` 从 1.0 逐级下调），每次试探都需要一个干净的起点。返回新对象保证原单元的原始 box/度量不变；同时 `try_resue_cache` 把昂贵的布尔判定缓存搬到新对象，兼顾「不可变」与「不重算」。

**练习 2**：`preprocess_document` 为什么要取所有段落 scale 的**众数**再统一，而不是各自用各自的最优 scale？

> **答案**：为了全文字号统一、观感一致。若每段各用最优 scale，同页会出现大小不一的字号，像排版事故。取众数（出现最多的字号）能把「大多数段落本来就能放下的字号」作为基准，再把偏大的段压下来，既统一又尽量少缩放。

---

### 4.2 CJK 换行规则：LINE_BREAK_REGEX 与标点避头尾

#### 4.2.1 概念说明

CJK（中日韩）和拉丁文字的换行规则根本不同：

- **拉丁文**：词与词之间有空格，词内部（如 `Hello`）**不能**断开。换行只能在空格或连字符处。
- **CJK**：字与字之间没有空格，**任意两个字之间都可以断行**。但同时有「标点禁则」：句号、顿号等不能出现在行首（避头）；左括号、左引号等不能出现在行末（避尾）；某些标点可以「悬挂」到版心右边缘之外（标点悬挂）。

`TypesettingUnit` 用三个布尔属性把这套规则编码进去，`_layout_typesetting_units` 的换行判断就在这三个属性上做决策。

#### 4.2.2 核心流程

换行发生在贪心摆放循环里，对每个单元依次判断「放在当前行会不会溢出」，溢出则换行：

```
对单元 unit（游标 current_x）：
  若 unit 是「悬挂标点」(is_hung_punctuation)            → 即使超出右边缘也不换行（让它挂出去）
  否则若满足以下任一条件 → 换行：
     (A) current_x + unit_width > box.x2                  # 基本溢出
     (B) 英文模式 且 current_x + width + 词尾预留 > box.x2 # 整个拉丁词放不下，提前换行
     (C) unit 是「避尾标点」且 current_x + unit_width*2 > box.x2  # 左括号快到行末，提前换行
  换行：current_x 归零，current_y 下移一个行高（行高 = 本行最高单元 × line_skip）
```

其中：

- `line_skip`：CJK 用 1.50，非 CJK 用 1.30（[typesetting.py:L968](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L968)）。CJK 行距更宽，因为汉字方块视觉更密。
- 条件 (B) 的「词尾预留」由 `_get_width_before_next_break_point` 算：从当前单元往后累加宽度，直到遇到一个 `can_break_line=True` 的单元为止。这保证一整个拉丁词不会被拆成两行（[typesetting.py:L1285-L1298](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1285-L1298)）。
- 「英文模式」（`use_english_line_break`）失败到极限后，会关掉它再试一次（[typesetting.py:L1064-L1073](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1064-L1073)）——即宁可拆词也要塞下。

#### 4.2.3 源码精读

**LINE_BREAK_REGEX：哪些字符「不可在中间断行」**：

[typesetting.py:L31-L87](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L31-L87) — 这个正则枚举了大量「字母/数字/连字符」区间（拉丁、西里尔、希腊、天城体、CJK 之外的各类文字），**匹配它 = 属于一个不可拆的词**。注意它故意把 CJK 统一表意文字排除在外——这正是「拉丁词不能拆、CJK 字字可断」的界线。

**can_break_line：断行能力判定**：

[typesetting.py:L206-L219](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L206-L219) — `calc_can_break_line` 逻辑很简洁：匹配 `LINE_BREAK_REGEX`（即拉丁字母类）就返回 `False`（**不能**在这里断行），其余（含所有 CJK 字）返回 `True`（可以断行）。公式和无 unicode 的单元返回 `True`。

**is_cjk_char：是不是 CJK 字**（用于行距、中英文间距判断）：

[typesetting.py:L221-L297](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L221-L297) — 先用一份全角标点白名单（`（）、。：《》` 等），再用一段覆盖 CJK 符号/平假名/片假名/谚文/汉字的 Unicode 区间正则，最后兜底用 `unicodedata.name` 看是否含 `CJK UNIFIED IDEOGRAPH` 或 `FULLWIDTH` 字样。`Typesetting.__init__` 里 `is_cjk` 则按目标语言（`ZH/JA/KR/CN/HK/TW`）整体判定（[typesetting.py:L849-L863](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L849-L863)）。

**is_hung_punctuation：标点悬挂**：

[typesetting.py:L313-L376](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L313-L376) — 列出句号、逗号、右引号、右括号、破折号、斜杠等。这些标点即使把游标推过 `box.x2` 也不触发换行（见换行条件开头的 `not unit.is_hung_punctuation`），允许它们「挂」在行尾边缘外。

**is_cannot_appear_in_line_end_punctuation：避尾（行末禁则）**：

[typesetting.py:L378-L411](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L378-L411) — 列出左引号、左括号、左书名号等。换行条件 (C)：当这种标点接近行末（`current_x + unit_width*2 > box.x2`）时提前换行，把它挪到下一行行首，避免「左括号孤悬行末」。

**换行主循环**：

[typesetting.py:L1300-L1456](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1300-L1456) — `_layout_typesetting_units` 是本讲最核心的算法。要点：
- 行高用本行单元高度的**众数**（[typesetting.py:L1422-L1425](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1422-L1425)），并取 `max(mode_height*line_skip, max_height*1.05)`——既照顾多数字号又防极高单元贴太近。
- 中英文交界处（`last_unit.is_cjk_char ^ unit.is_cjk_char`，即一中一西）会插入半个空格宽的额外间距（[typesetting.py:L1373-L1398](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1373-L1398)），且用 `mixed_character_blacklist` 排除句号等不该加间距的标点。
- 空格宽度用 CJK 字「你」在目标字号下的前进宽度 × 0.5 来估（[typesetting.py:L1329-L1331](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1329-L1331)）——用真实字形宽度比硬编码常量更准。

#### 4.2.4 代码实践

**目标**：用 typesetting 的思路，手写一个最小 CJK 换行函数，体会 `LINE_BREAK_REGEX` 的作用，并对照说明 `matrix_helper.decompose_ctm` 为何重要（本讲综合实践任务）。

下面是**示例代码**（非项目代码，仅供理解原理），实现一个贪心换行器：

```python
# 示例代码：最小 CJK/拉丁混合换行器
import re

# 简化版 LINE_BREAK_REGEX：字母、数字、连字符算「不可拆词」
LATIN_WORD = re.compile(r"^[A-Za-z0-9\-']+$")

def can_break(ch: str) -> bool:
    """能否在这个字符处断行：CJK/标点可以，拉丁词内部不行。"""
    if ch == " ":
        return True
    return not LATIN_WORD.match(ch)

def is_hung(ch: str) -> bool:
    return ch in "。，、；：！？.),!"

def wrap_line(text: str, budget: float, char_width: float = 1.0) -> list[str]:
    """把 text 按 budget 宽度贪心折行。"""
    lines, cur = [], ""
    cur_w = 0.0
    for ch in text:
        w = char_width  # 简化：等宽
        overflow = (cur_w + w > budget) and not is_hung(ch)  # 悬挂标点不计入溢出
        if overflow and cur:
            lines.append(cur)
            cur, cur_w = ch, w
        else:
            cur += ch
            cur_w += w
    if cur:
        lines.append(cur)
    return lines

if __name__ == "__main__":
    sample = "PDF翻译库 BabelDOC 支持中文与English混排。"
    for ln in wrap_line(sample, budget=12):
        print(ln)
```

**操作步骤**：

1. 把上面示例代码存成 `wrap_demo.py` 运行。
2. 把 `budget` 从 12 调到 8、再调到 20，观察折行点变化。
3. 对照项目里的 [typesetting.py:L1407-L1417](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1407-L1417)，找出示例代码**缺少**了项目里的哪两条规则（提示：英文词整体保留、避尾标点）。

**需要观察的现象**：`budget=8` 时 `BabelDOC` 这个词会被示例代码**拆开**（因为示例没有「词整体保留」），而项目代码靠 `_get_width_before_next_break_point` 不会拆词。

**预期结果**：能口头说明示例代码相比项目实现少处理了「拉丁词不可拆」和「左括号避尾」两条。完整运行结果取决于本地 Python 环境。

> 补充说明 `decompose_ctm` 的重要性（实践任务第二问）：本项目换行循环里量宽用的是字符的 `font_size`，而这个字号在**解析阶段**正是从 CTM 的缩放分量提取的——`decompose_ctm` 把 6 数矩阵 `(a,b,c,d,e,f)` 拆成语义的 `PdfAffineTransform(translation_x/y, rotation, scale_x/y, shear)`，字号、旋转、错切才能分别被读懂。详见本讲末尾「4.5」专节。

#### 4.2.5 小练习与答案

**练习 1**：`LINE_BREAK_REGEX` 为什么**不**包含汉字区间（如 `一-鿿`）？

> **答案**：因为该正则的语义是「匹配 = 不可断行」。汉字字字可断，所以绝不能被它匹配；汉字应落入 `can_break_line=True` 分支。正则只收录拉丁/西里尔/希腊等「词内部不可断」的文字。

**练习 2**：换行条件里有 `not unit.is_hung_punctuation`。如果把这句去掉（即悬挂标点也参与溢出判断），会有什么视觉问题？

> **答案**：句末的句号、逗号会被强制挤到下一行单独成行，或导致整行提前折行，版面右边缘参差不齐。标点悬挂正是为了让这些「轻量」符号略微超出版心、保持正文行的齐整。

---

### 4.3 字体度量缓存：pymupdf.Font 与 lru_cache

#### 4.3.1 概念说明

「逐字符重排」意味着对**每一个译文字符**都要查一次「这个字在目标字体里有没有字形、前进宽度是多少」。一篇论文动辄上万字，每个字又要在多个候选字号（scale 试探）下反复量宽——如果每次都走真实的字体度量计算，开销会爆炸。

BabelDOC 的解法是**三级缓存**：

1. **字体度量级**（`FontMapper`）：把 `pymupdf.Font.has_glyph` 和 `char_lengths` 用 `functools.lru_cache` 包起来，`(字符, 字号) → 宽度` 永久记忆。
2. **字体查找级**（`create_typesetting_units`）：`@cache` 的 `get_font(font_id, xobj_id)` 记住「字体 id → PdfFont」。
3. **单元级**（`TypesettingUnit`）：每个单元把 `width/height/box` 和一堆布尔判定算一次后存进 `*_cache` 字段，`relocate` 出新单元时用 `try_resue_cache` 搬运。

#### 4.3.2 核心流程

```
译文字符 c、字号 sz
  → TypesettingUnit.width
      → box = calculate_box()                          # 算包围盒
            → font.char_lengths(c, sz)[0]              # 真实度量
                  ↑ lru_cache 命中？命中直接返回，不调底层
      → width_cache 缓存
  → relocate 产生新单元时 try_resue_cache 搬运 width_cache（同字符宽度不变）
```

关键点：**字符的前进宽度只跟「字符 + 字号」有关，与位置无关**。所以无论 `relocate` 把它摆到哪里，宽度都不变——这正是 `try_resue_cache` 能安全搬运 `width_cache` 的依据。

#### 4.3.3 源码精读

**FontMapper 对 pymupdf.Font 方法的 lru_cache 包装**：

[fontmap.py:L66-L82](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L66-L82) — 对每个加载的 `pymupdf.Font`，用 `functools.lru_cache(maxsize=10240, typed=True)` 重新绑定 `has_glyph` 和 `char_lengths`：

```python
pymupdf_font.has_glyph = functools.lru_cache(maxsize=10240, typed=True)(pymupdf_font.has_glyph)
pymupdf_font.char_lengths = functools.lru_cache(maxsize=10240, typed=True)(pymupdf_font.char_lengths)
```

`typed=True` 表示参数类型不同（int vs float 字号）也算不同 key，避免类型混淆；`maxsize=10240` 容纳上万字符组合。

**has_char / map_in_type / map 的缓存**：

[fontmap.py:L114-L126](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L114-L126) — `has_char`（遍历所有字体看某字是否有字形）也加了 `lru_cache`。`map` 本身负责「按 bold/italic/serif 选最合适的目标字体」，是译文每个字都要走一遍的 hot path。

**get_font 的 @cache**：

[typesetting.py:L1467-L1473](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1467-L1473) — `create_typesetting_units` 内部的局部函数 `get_font` 用 `@cache`（即 `functools.cache`，无上限），按 `(font_id, xobj_id)` 记住结果，避免对每个字都查一遍 `fonts[...]` 字典与 xobj 分支。

**TypesettingUnit 的单元级缓存字段**：

[typesetting.py:L117-L127](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L117-L127) — 一长串 `*_cache` 字段：`box_cache`、`width_cache`、`height_cache`、`can_break_line_cache`、`is_cjk_char_cache`、`is_space_cache`、`is_hung_punctuation_cache`、`is_cannot_appear_in_line_end_punctuation_cache`、`can_passthrough_cache`、`mixed_character_blacklist_cache`。每个 `@property`（如 `width`）首次访问时调 `calc_*` 填缓存，之后直接返回。

**try_resue_cache 搬运缓存**：

[typesetting.py:L153-L177](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L153-L177) — `relocate` 产生新单元后调用它，把旧单元的布尔判定缓存（`is_cjk_char`、`can_break_line`、`is_hung_punctuation` 等）复制到新单元。注意它搬运的是只依赖字符本身的布尔量，而 `box_cache` 这类依赖具体位置的量不搬——因为「这个字是不是 CJK」「能不能断行」与位置无关，但 box 会随 `relocate` 变化。

**宽度计算的真实度量来源**：

[typesetting.py:L455-L459](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L455-L459) — 对译文 unicode 单元，`calculate_box` 调 `self.font.char_lengths(self.unicode, self.font_size)[0]` 取前进宽度，正是上面被 `lru_cache` 包装的那个方法。

#### 4.3.4 代码实践

**目标**：验证「同字符 + 同字号」的度量只算一次。

**操作步骤**（阅读型）：

1. 读 [fontmap.py:L66-L82](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/fontmap.py#L66-L82)，确认 `char_lengths` 被 `lru_cache` 覆盖。
2. 读 [typesetting.py:L455-L459](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L455-L459) 与 [typesetting.py:L153-L177](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L153-L177)，画出一次「译文字符 c 在 scale=1.0 和 scale=0.9 两次试探」的度量调用路径。
3. 思考：scale 变化时 `font_size` 也变（`font_size*scale`），所以 `char_lengths(c, font_size*scale)` 的 cache key 不同——两次试探各算一次；但 `is_cjk_char` 与 scale 无关，所以 `try_resue_cache` 搬运它能在两次试探间省掉一次 Unicode 判定。

**需要观察的现象**（心算）：对一个 100 字的段落、6 次 scale 试探，`char_lengths` 最多被算 `100×6=600` 次（不同字号各一次），而 `is_cjk_char` 只算 100 次（搬运 5 次）。没有缓存时两者都是 600 次。

**预期结果**：能说清「位置/字号相关的量（width）随 scale 重算，字符本身固有的量（is_cjk）只算一次」这条分工。无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`lru_cache(typed=True)` 里的 `typed=True` 去掉会有什么风险？

> **答案**：`typed=True` 区分参数类型，使得 `char_lengths("你", 12)` 和 `char_lengths("你", 12.0)` 视为不同 key。若去掉，当字号有时传 int 有时传 float 时可能命中错误缓存（虽然数值相同一般结果也一样，但 `typed=True` 更稳妥地避免边界问题）。项目选择保守开启。

**练习 2**：为什么 `try_resue_cache` 搬运 `is_cjk_char_cache` 却**不**搬运 `box_cache`？

> **答案**：`is_cjk_char` 只取决于字符本身，与位置无关，搬迁安全；`box` 在 `relocate` 后会变成新坐标，旧 `box_cache` 已失效，必须重算。缓存搬运遵循「只搬与位置无关的量」。

---

### 4.4 rtree 空间索引：段落避让查询

#### 4.4.1 概念说明

译文普遍比原文长，重排后段落会**变高**（行数变多）。变高的段落可能顶到它**下方**的另一个段落，造成重叠。`render_page` 在正式排版前要做一次「段落避让」：检查每个段落正下方一小条区域里有没有别的段落，有就把当前段落的下边界（`box.y`）整体上抬，腾出间距。

朴素做法是双重循环：对每个段落，遍历所有其他段落看是否落在它下方。一篇页面几十上百段时这是 \(O(n^2)\)。BabelDOC 用 **rtree**（R-tree 空间索引）把「找落在某矩形内的段落」降为近似 \(O(\log n)\)。

> 数学上，朴素法对 \(n\) 个段落做 \(n\) 次「与查询矩形相交」检测，每次扫 \(n\) 个候选，总复杂度 \(\Theta(n^2)\)。R-tree 把矩形按空间聚合成分支节点，查询时沿树枝剪枝，期望复杂度 \(\Theta(\log n)\)。

#### 4.4.2 核心流程

```
render_page(page):
  1. 建索引：para_index = rtree.index.Index()
     对每个有效段落 i：para_index.insert(i, box_to_tuple(para.box))
  2. 避让：对每个段落 p_upper
       check_area = p_upper.box 正下方一条 required_gap 高的窄条
       candidate_ids = para_index.intersection(check_area)   # rtree 查询，候选少
       对候选再精确判断水平是否重叠 → conflicting_paras
       若有冲突：把 p_upper.box.y 上抬到 max(下方段落底) + required_gap
  3. 排版：render_paragraph(...)
```

注意：rtree 只负责「快速拿到候选」，精确的几何判断（水平是否重叠）仍由后续条件完成。`required_gap` 按段落高度分档：矮段（<36pt）用 0.5，高段用 3（见 [typesetting.py:L1176-L1178](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1176-L1178)）。

#### 4.4.3 源码精读

**rtree 索引的导入与建库**：

[typesetting.py:L12](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L12) — `from rtree import index`，引入 R-tree 实现。

[typesetting.py:L1158-L1170](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1158-L1170) — 建内存索引 `para_index = index.Index()`，把每个有效段落（box 非空且四坐标非 None）按 `para_map[i] = para` + `para_index.insert(i, box_to_tuple(para.box))` 注册。`box_to_tuple` 把 `Box` 转 `(x,y,x2,y2)`（[layout_helper.py:L115-L119](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L115-L119)）。

**避让查询与上抬**：

[typesetting.py:L1172-L1211](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1172-L1211) — 对每个 `p_upper`：
- 算 `required_gap`（`0.5 if para_height < 36 else 3`）。
- 构造 `check_area`：与 `p_upper` 同 x 范围、在它正下方 `required_gap` 高的一条窄区域。
- `candidate_ids = list(para_index.intersection(box_to_tuple(check_area)))` —— **这就是 rtree 加速点**：只返回 box 与 `check_area` 相交的段落 id，通常很少。
- 对候选（排除自身）用水平条件 `p_lower.box.x2 < p_upper.box.x or p_lower.box.x > p_upper.box.x2` 过滤掉左右不重叠的，剩下的才是真正会被挤压的 `conflicting_paras`。
- 有冲突则把 `p_upper.box.y` 上抬到 `max_y2 + required_gap`（但不超自身顶 `box.y2`）。

**对比：扩展空间的朴素扫描**：

[typesetting.py:L1582-L1652](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1582-L1652) — `get_max_right_space` / `get_max_bottom_space` 用于「scale 实在不够时借空间」，它们**没有**用 rtree，而是朴素遍历 `page.pdf_paragraph/pdf_character/pdf_figure`。这说明 rtree 只用在「页内段落两两避让」这个高频场景；低频的借空间查询仍用朴素法，体现了「按场景选工具」的取舍。

#### 4.4.4 代码实践

**目标**：理解 rtree 把 \(O(n^2)\) 避让降到近 \(O(n\log n)\)。

**操作步骤**（阅读型 + 可选运行）：

1. 读 [typesetting.py:L1158-L1211](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1158-L1211)，找到 `para_index.intersection(...)` 这一行——它替代了「遍历所有段落」的内层循环。
2. 把它和 [typesetting.py:L1596-L1608](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1596-L1608)（`get_max_right_space` 的朴素 for 循环）对比，体会「索引查询 vs 全量扫描」。
3. 可选：写一小段**示例代码**，用 `rtree` 库插入 1000 个随机矩形，再分别用「rtree 查询」和「双重循环」统计与某矩形相交的数量，对比耗时。

**需要观察的现象**：段落数 \(n\) 增大时，朴素法耗时近似线性翻倍（\(n^2\)），rtree 法耗时几乎平缓。

**预期结果**：能说清「rtree 的 `intersection` 返回的是候选 id 列表，把内层 O(n) 扫描变成 O(log n) 索引查找」。可选项的运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么拿到 rtree 候选后，还要再用 `p_lower.box.x2 < p_upper.box.x ...` 做一次水平判断？rtree 不是已经返回「相交」的了吗？

> **答案**：rtree 的 `intersection(check_area)` 返回的是 bbox 与 `check_area` **相交**的段落，但「相交」包括了仅在 y 方向接近、x 方向并不重叠（位于左右两侧）的段落。`check_area` 与 `p_upper` 同 x 范围，所以还需用水平条件剔除那些 x 不重叠的段落，才是真正会被挤压的「正下方」段落。

**练习 2**：`get_max_bottom_space` 为什么不用 rtree？

> **答案**：它是「scale 试探失败时的兜底借空间」，调用频率远低于「每页段落避让」；且它要综合段落、散字符、图形三类元素，建一个覆盖三类的索引收益不大。工程上对低频路径用朴素扫描、对高频路径用 rtree，是合理的性能取舍。

---

### 4.5 矩阵分解为何重要：decompose_ctm 与字号、向量图形搬迁

#### 4.5.1 概念说明

实践任务特别要求说明 `matrix_helper.decompose_ctm` 为何重要。它虽不是 `typesetting.py` 直接调用的函数（typesetting 只 import 了 `create_translation_and_scale_matrix`），但它是排版能正确工作的两个前提——**字号提取**与**带旋转向量图形保真搬迁**——的几何根基，且其产物存在 IL 里被排版与渲染共同消费。

PDF 的 CTM 是 6 个数 `(a,b,c,d,e,f)`，把平移、旋转、缩放、错切**揉**在一个矩阵里，对人和对算法都不直观。

#### 4.5.2 核心流程

`decompose_ctm` 用类 QR 分解把它拆成语义明确的 `PdfAffineTransform(translation_x, translation_y, rotation, scale_x, scale_y, shear)`：

\[ \begin{bmatrix} a & c & e \\ b & d & f \\ 0 & 0 & 1 \end{bmatrix} \Rightarrow (\text{平移}, \text{旋转}, \text{缩放}, \text{错切}) \]

分解逻辑（[matrix_helper.py:L22-L122](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/matrix_helper.py#L22-L122)）可概括为：

- `translation = (e, f)`（直接取）。
- `scale_x = hypot(a, b)`（第一列长度）。
- 把第二列正交化去掉错切分量，得 `shear` 与 `scale_y = hypot(...) `。
- `rotation = atan2(b, a)`（第一列方向角）。
- 用行列式符号判定是否含镜像反射（翻转 `scale_y`/`shear` 符号）。

#### 4.5.3 源码精读

**decompose_ctm：CTM → 语义变换**：

[matrix_helper.py:L22-L122](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/matrix_helper.py#L22-L122) — 上面流程的完整实现，含退化（`sx < eps`）与反射处理兜底。它在解析前端被调用，例如 [il_creater_active.py:L86](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L86) 导入、[il_creater_active.py:L1509](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/frontend/il_creater_active.py#L1509) 在构建 `PdfForm` 时调用，产物 `PdfAffineTransform` 存进 IL。

它重要在哪？两点：

1. **scale_x / scale_y 直接给出字号**：解析阶段正是从 CTM 的缩放分量得到每个字符的 `font_size`（`PdfStyle.font_size`），而排版阶段 4.3 模块读的、用来量宽的字号就是它。没有分解，字号就埋在矩阵里取不出，`char_lengths(c, font_size)` 无从谈起。
2. **rotation / shear 决定向量图形如何搬迁**：公式里常含曲线（`PdfCurve`）和表单（`PdfForm`），它们可能本身带旋转/错切。排版在 `relocate` 公式时**不直接改写原 CTM**，而是另外构造一个只含「平移+缩放」的 `relocation_transform`，叠加在原 CTM 之上（[typesetting.py:L718-L725](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L718-L725)，矩阵由 [matrix_helper.py:L224-L245](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/matrix_helper.py#L224-L245) 的 `create_translation_and_scale_matrix` 生成）。正因为原 CTM 的旋转/错切不能丢，才必须用「附加变换」而非「覆盖变换」。

**compose_ctm：可逆的逆运算**：

[matrix_helper.py:L125-L169](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/matrix_helper.py#L125-L169) — `compose_ctm` 是 `decompose_ctm` 的逆运算，支持「分解 → 改分量 → 合成」的往返，让「只调缩放、不动旋转」这类精细操作成为可能。

#### 4.5.4 代码实践

**目标**：动手验证「分解后能单独读出字号、单独调整缩放」。

**操作步骤**（阅读 + 可选运行）：

1. 读 [matrix_helper.py:L22-L122](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/matrix_helper.py#L22-L122)，对照注释确认 `scale_x`、`scale_y`、`rotation`、`shear` 各自怎么算。
2. 可选运行下面**示例代码**（非项目代码）：

```python
# 示例代码：用 decompose_ctm 把一个「缩放12 + 旋转30°」的 CTM 拆开
from babeldoc.format.pdf.document_il.utils.matrix_helper import decompose_ctm, compose_ctm
import math
theta = math.radians(30); sz = 12.0
# 旋转θ×缩放sz 的 CTM: (sz*cosθ, sz*sinθ, -sz*sinθ, sz*cosθ, 100, 200)
ctm = (sz*math.cos(theta), sz*math.sin(theta), -sz*math.sin(theta), sz*math.cos(theta), 100, 200)
aff = decompose_ctm(ctm)
print("scale_x≈", round(aff.scale_x, 3), " rotation(°)≈", round(math.degrees(aff.rotation), 1))
```

**需要观察的现象**：打印出的 `scale_x` 应约等于 12（即字号），`rotation` 应约等于 30°——证明字号与旋转被正确地从矩阵里分离出来。

**预期结果**：能说清「字号来自 CTM 缩放分量、旋转是独立分量」。运行结果**待本地验证**（需项目环境可 import）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `relocate` 公式时用「附加 `relocation_transform`」而不是直接覆盖原 CTM？

> **答案**：原 CTM 里可能含旋转/错切（由 `decompose_ctm` 刻画并被保留在 IL 中）。覆盖会丢失这些分量，导致公式里的曲线/表单变形；附加一个只含平移+缩放的变换矩阵，叠加在原 CTM 之上，既搬迁了位置又不破坏原始几何。

**练习 2**：若一个字符的 CTM 没有旋转、纯缩放 `(12,0,0,12,e,f)`，`decompose_ctm` 会得到什么？

> **答案**：`scale_x=scale_y=12`、`rotation=0`、`shear=0`、`translation=(e,f)`。这正是最常见的「字号 12、无旋转」情形，字号能被干净地读出。

---

## 5. 综合实践

把四个模块串起来，做一个「最小排版器」阅读任务：

1. **入口追踪**：从 [high_level.py:L1038](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L1038) 的 `Typesetting(...).typesetting_document(docs)` 出发，依次进入 `preprocess_document` → `_get_optimal_scale` → `_find_optimal_scale_and_layout` → `_layout_typesetting_units`，画出完整调用栈。
2. **规则对照**：在 `_layout_typesetting_units` 的换行条件（[typesetting.py:L1407-L1417](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1407-L1417)）旁，标注它分别用了 4.2 模块的哪条规则（基本溢出 / 英文词保留 / 避尾 / 悬挂）。
3. **缓存盘点**：在调用栈上标出 4.3 模块的三级缓存各出现在哪一行（`char_lengths` 的 lru_cache、`get_font` 的 @cache、`try_resue_cache`）。
4. **索引定位**：指出 4.4 模块的 rtree 查询出现在 `render_page` 的哪一步，并说明它发生在「正式逐段排版」**之前**（先避让、后排版）。
5. **写一段总结**（不超过 150 字）：解释「为什么译文变长后，BabelDOC 仍能让结果贴着原版面」——应涵盖「按段落 box 当画布、逐字符量宽、CJK 规则换行、不够就缩字号、rtree 避让邻段」。

> 进阶（可选）：把 4.2.4 的示例换行器扩展，加上「拉丁词不可拆」和「避尾标点提前换行」两条规则，使输出行为接近项目实现。

## 6. 本讲小结

- `Typesetting` 把译文**逐字符**重排回原版面：段落拆成统一的 `TypesettingUnit`（字符/公式/译文 unicode 三选一），在段落 `box` 内按规则摆放，`relocate` 算新坐标、`render` 还原成 `PdfCharacter` 写回 IL。
- 采用**两趟**设计：第一趟 `preprocess_document` 只求各段最优 scale 并取众数统一字号，第二趟 `render_paragraph` 才真正排版。
- CJK 换行靠三个布尔属性：`LINE_BREAK_REGEX` 划出「不可拆的拉丁词」（CJK 字字可断）、`is_hung_punctuation` 允许标点悬挂、`is_cannot_appear_in_line_end_punctuation` 实现行末禁则（避尾）。
- 字体度量做了**三级缓存**：`FontMapper` 对 `has_glyph`/`char_lengths` 的 `lru_cache`、`get_font` 的 `@cache`、`TypesettingUnit` 的单元级 `*_cache`（经 `try_resue_cache` 在 relocate 时搬运与位置无关的量）。
- `rtree` 空间索引把页内「段落互相避让」的查询从 \(O(n^2)\) 降到近 \(O(n\log n)\)；低频的借空间查询仍用朴素扫描。
- `matrix_helper.decompose_ctm` 把 CTM 分解成「平移/旋转/缩放/错切」，是字号提取与保真搬迁带旋转向量图形的前提；排版用「附加 `relocation_transform`」而非覆盖原 CTM。

## 7. 下一步学习建议

- **u7-l2 字体映射：FontMapper**：本讲多次用到 `FontMapper.map` 与字体度量，下一讲将完整讲解它如何按 `bold/italic/serif` 与字符可用性（`has_char`）在 `normal/script/fallback/base` 四类字体间选字。
- **u7-l3 PDF 生成后端：PDFCreater**：排版写回的 `PdfCharacter` 最终如何变成 mono/dual PDF，包括字体子集化与 `save_pdf_with_timeout`。
- **回看 u5-l4 公式与样式**：本讲 `relocate` 公式时用到的 `x_offset/y_offset/x_advance` 正是 u5-l4 算出的，可对照理解行内公式为何要单独算偏移。
- **源码延伸**：想深入换行可读 [typesetting.py:L1300-L1456](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/typesetting.py#L1300-L1456) 的完整主循环；想深入几何可读 [matrix_helper.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/matrix_helper.py) 的分解/合成往返。
