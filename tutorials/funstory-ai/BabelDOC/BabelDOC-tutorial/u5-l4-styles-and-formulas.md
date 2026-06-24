# 公式与样式处理：StylesAndFormulas

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 BabelDOC 在「散字符 → 结构化段落」之后，如何把每个字符判定为**正文**还是**公式**，并把连续的公式字符聚合为一个 `PdfFormula`。
- 理解公式合并、以及如何用 **IoU（交并比）** 把散落的曲线（curve）和表单（form）「吸附」进附近的公式。
- 理解公式相对周围正文的 **x/y 偏移量** 是如何计算的，以及它对最终排版的影响。
- 认识段落**样式（style）**的基准求取与「同样式文本重排」机制，以及颜色 `GraphicState` 的来源。

本讲对应 midend 流水线里的 `Parse Formulas and Styles` 阶段，是上接 `ParagraphFinder`（u5-l3，产出 `pdf_paragraph`）、下接翻译与排版的关键一跳。

## 2. 前置知识

阅读本讲前，请先具备以下认知（来自前面几讲）：

- **三段式与 IL**：BabelDOC 用一棵带坐标的对象树（IL，`il_version_1.Document`）贯穿 frontend→midend→backend。midend 每个阶段都在同一份 `docs` 上原地加工（见 u2-l1、u2-l2）。
- **IL 数据模型**：`Page` 下并列挂着 `pdf_character`、`pdf_paragraph`、`pdf_curve`、`pdf_form`、`page_layout` 等集合；`PdfParagraph` 内部由一串 `PdfParagraphComposition` 组成，每个 composition 是「行 `pdf_line` / 公式 `pdf_formula` / 同样式字符组 …」多选一（见 u3-l1）。
- **段落识别**：`ParagraphFinder` 已经把散字符按版面区域聚成了段落，段落的 `pdf_paragraph_composition` 此刻大多是「一整行文本」的 `pdf_line`（见 u5-l3）。
- **Box 坐标系**：IL 采用左下原点、y 向上的 PDF 坐标系，`Box(x, y, x2, y2)` 表示一个矩形包围盒。

本讲会反复用到两个几何工具（都在 `layout_helper.py` 里）：

- **IoU（这里其实是「交/第一框面积」）**：`calculate_iou_for_boxes(a, b)` 返回的是「a 与 b 的交集面积 ÷ a 自身面积」，是一个**非对称**的「a 被覆盖比例」，而非教科书里的对称交并比。这一点非常关键，后面会反复用到。
- **y 方向重叠率**：`calculate_y_true_iou_for_boxes(a, b)` 返回「y 方向交集高度 ÷ min(两框高度)」，用于判断两个元素是否「在同一行」。

> 术语提示：项目里把配置项与 CLI 参数写成 `formular`（少一个 a），如 `--formular-font-pattern`、`TranslationConfig.formular_font_pattern`；而类名/方法名/实体名用 `formula`，如 `is_formulas_font`、`PdfFormula`。这是历史遗留拼写，阅读源码时请注意对应。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`babeldoc/format/pdf/document_il/midend/styles_and_formulas.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py) | 本讲主角。`StylesAndFormulas` 类，负责公式识别、合并、偏移、曲线/表单归并、段落样式处理。 |
| [`babeldoc/format/pdf/document_il/utils/formular_helper.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py) | 公式判定的「规则库」：字体名三段式 pattern、字符级判定（`is_formulas_start_char` / `is_formulas_middle_char`）、页面公式字体 id 收集、公式 box 重算。 |
| [`babeldoc/format/pdf/document_il/utils/spatial_analyzer.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/spatial_analyzer.py) | 空间关系分析：判断曲线/表单是否「落在公式内」的 `is_element_contained_in_formula`。 |
| [`babeldoc/format/pdf/document_il/utils/layout_helper.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py) | 几何与样式工具：`calculate_iou_for_boxes`、`calculate_y_true_iou_for_boxes`、`is_same_style`、`is_bullet_point` 等。 |
| [`babeldoc/format/pdf/document_il/utils/style_helper.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/style_helper.py) | 颜色与样式工厂：`create_pdf_style`、以及一组预设颜色 `GraphicState`（RED/ORANGE/BLACK…）。 |
| [`babeldoc/format/pdf/document_il/il_version_1.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py) | IL 数据模型：`PdfFormula`、`PdfParagraphComposition`、`PdfStyle`、`Box`、`PdfCharacter` 等定义。 |
| [`babeldoc/format/pdf/high_level.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 流水线编排：本阶段的调用点与 `--debug` 快照落盘位置。 |

## 4. 核心概念与源码讲解

### 4.1 公式字符识别

#### 4.1.1 概念说明

科研论文 PDF 里，正文和公式在视觉上混在一起，但它们的「翻译命运」完全不同：

- **正文**要送进翻译引擎翻译；
- **公式**不翻译，整体当作一个占位符保留（译文里以 `{v1}` 等占位符出现，见 u6-l2）。

所以 midend 必须在「段落已经成型」之后，把段落里的字符重新**二分类**：哪些是公式字符，哪些是正文字符；然后把连续的公式字符聚成一个 `PdfFormula`，连续的正文字符聚成一个 `PdfLine`。

BabelDOC 判断一个字符「像不像公式」，综合了三类信号：

1. **字体名**：公式常用专门字体（如 `Cambria Math`、`STIXTwoMath-Regular`、Computer Modern 系列 `CMMI12`）。字体名能匹配一组 pattern，就认定是公式字体。
2. **字符属性**：`(cid:...)`、数学符号（Unicode 类别 `Sm`）、修饰符（`Mn`/`Sk`）、希腊字母、数字、`[]•`、逗号等，都被视作公式字符。
3. **字体能力**：如果当前字体**根本渲染不出**这个字符（`font_mapper.has_char` 为假），也倾向于是公式专用字符。

#### 4.1.2 核心流程

`StylesAndFormulas.process_page` 是单页的总调度，对一个页面按固定顺序串起一系列子步骤（公式识别、逗号拆分、合并、偏移、归并、样式）。本节聚焦其中的 **公式字符识别** 部分：

```
process_page(page)
  └─ process_page_formulas(page)         # 本节主角：字符二分类 → 聚合
       ├─ collect_page_formula_font_ids(page, formular_font_pattern)
       │     ⇒ (page级公式字体id集合, {xobj_id: 该xobj的公式字体id集合})
       └─ 对每个段落 paragraph：
            对每个 composition（通常是一行 pdf_line）：
              _classify_characters_in_composition(...)   # 逐字符打 is_formula 标签
              _group_classified_characters(...)          # 相同标签的连续字符聚成一组
                ⇒ create_composition(...)               # 公式组 → PdfFormula，正文组 → PdfLine
            paragraph.pdf_paragraph_composition = 新组合列表
```

逐字符分类是一个**带状态机**的过程：维护 `in_formula_state`（当前是否在公式里），因为「逗号不能作为公式开头，但可以出现在公式中间」——这需要看上一个字符的状态。同时还会识别**角标（corner mark）**（上下标），其字号明显小于前后正文。

#### 4.1.3 源码精读

**① 页面公式字体 id 收集**：先扫描页面上所有字体，用 `is_formulas_font` 判定哪些是公式字体，得到它们的 `font_id` 集合。XObject（嵌入子图）里的字体单独维护，允许「覆盖/移除」页面级判断。

[`formular_helper.py:68-107`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L68-L107) 中，页面级集合先复制给每个 XObject，再在 XObject 内部追加/移除：

```python
current_xobj_fonts = page_formula_font_ids.copy()
...
if is_formulas_font(font.name, formular_font_pattern):
    current_xobj_fonts.add(font.font_id)
else:
    current_xobj_fonts.discard(font.font_id)  # 显式非公式字体则移除
```

**② 字体名三段式 pattern**：`is_formulas_font` 是一个 `@functools.cache` 缓存的纯函数，对字体名依次尝试三组正则——精确公式字体（如各类 `*Math*`）、已知非公式字体（如 `Arial.*`、`TimesNewRoman.*`）、宽泛 pattern（如 `CM[^RB]`、`.*Math`）。优先级：精确命中→是公式；否则已知非公式命中→不是公式；否则宽泛命中→是公式。

[`formular_helper.py:110-309`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L110-L309) 关键判定逻辑（节选）：

```python
if re.match(precise_formula_font_pattern, font):
    return True
elif re.match(pattern_text, font):     # 已知非公式字体
    return False
elif re.match(broad_formula_font_pattern, font):  # 宽泛 pattern
    return True
return False
```

注意 `--formular-font-pattern`（即 `formular_font_pattern`）若提供，会**替换**默认的宽泛 pattern（[`formular_helper.py:267-268`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L267-L268)），这是用户自定义公式字体识别的入口。字体名若以 `BASE64:` 前缀存储，还会先 base64 解码再匹配（[`formular_helper.py:291-297`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L291-L297)）。

**③ 字符级判定**：`is_formulas_start_char` / `is_formulas_middle_char` 给出「这个 unicode 字符像不像公式字符」。

[`formular_helper.py:16-51`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L16-L51)：`(cid:...)` 直接为真；字体渲染不出的字符为真；Unicode 类别落在 `Mn/Sk/Sm/Zl/Zp/Zs/Co` 或希腊字母区间为真；正则 `[0-9\[\]•]` 为真。`is_formulas_middle_char` 在此基础上额外把**逗号**也算作公式字符（[`formular_helper.py:54-65`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L54-L65)）。

**④ 逐字符状态机分类**：这是 `_classify_characters_in_composition` 的核心，综合「字体 pattern、字符 pattern、字体能力、角标、垂直排版、视觉框与实际框错位」等多重信号给每个字符打 `is_formula` 标签。

[`styles_and_formulas.py:391-523`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L391-L523)，判定的关键或条件（节选）：

```python
is_formula = (
    (char.formula_layout_id                          # 版面已判为公式区域
     or (is_formulas_start_char(...) and not in_formula_state)
     or (is_formulas_middle_char(...) and in_formula_state))
    or char.pdf_style.font_id in formula_font_ids    # 公式字体
    or char.vertical                                 # 垂直排版
    or (char.char_unicode is None and in_formula_state)   # dummy 空格
    or (char.box.x > char.visual_bbox.box.x2 or ...)      # 视觉框与实际框错位
)
is_formula = is_formula or is_corner_mark            # 角标也算公式
if char.char_unicode == " ":
    is_formula = in_formula_state                    # 空格沿用上一个状态
```

其中 `formula_layout_id` 是字符属性（[`il_version_1.py:697-702`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L697-L702)），若上游版面分析已把该字符划进 `formula` 区域，则直接判定为公式。角标（corner mark）的判定基于「字号明显小于前后正文」（阈值 0.79），见 [`styles_and_formulas.py:471-503`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L471-L503)。

**⑤ 连续同标签字符聚合**：`_group_classified_characters` 把相邻、同标签的字符聚成一组，交给 `create_composition` 生成对应的 IL 节点。

[`styles_and_formulas.py:933-948`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L933-L948)：公式组构造 `PdfFormula`，正文组构造 `PdfLine`。

```python
def create_composition(self, chars, is_formula, line_index, is_corner_mark=False):
    if is_formula:
        formula = PdfFormula(pdf_character=chars, line_id=line_index)
        formula.is_corner_mark = is_corner_mark
        self.update_formula_data(formula)
        return PdfParagraphComposition(pdf_formula=formula)
    else:
        new_line = PdfLine(pdf_character=chars)
        self.update_line_data(new_line)
        return PdfParagraphComposition(pdf_line=new_line)
```

整个段落处理入口在 [`styles_and_formulas.py:568-619`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L568-L619) 的 `process_page_formulas`，它先取本段（或本 xobj）的公式字体 id 集合，再逐 composition 分类、重组。

#### 4.1.4 代码实践

**实践目标**：亲手验证「公式字体识别」与「字符级判定」两条规则的真实输出，体会三段式 pattern 的优先级。

**操作步骤**：

1. 在仓库根目录用已安装的 BabelDOC 环境运行下面这段「示例代码」（独立脚本，不依赖真实 PDF）。

```python
# 示例代码：验证公式字体识别规则
# 文件名：demo_formula_font.py（自行创建，可放在任意可 import 到 babeldoc 的目录）
from babeldoc.format.pdf.document_il.utils.formular_helper import (
    is_formulas_font,
    is_formulas_start_char,
)

# 1) 字体名三段式 pattern：精确 > 已知非公式 > 宽泛
samples = [
    "Cambria Math",        # 精确 *Math* → 公式
    "STIXTwoMath-Regular", # 精确 → 公式
    "CMMI12",              # 宽泛 CM[^RB] → 公式
    "ArialMT",             # 已知非公式 Arial.* → 不是公式
    "Helvetica",           # 都不命中 → 不是公式
]
for name in samples:
    print(f"{name:24s} => is_formulas_font = {is_formulas_font(name, None)}")

# 2) 字符级判定（用 cid 和数字这类不依赖 font_mapper 的分支）
class _FakeFontMapper:
    def has_char(self, ch):  # 简化：永远说“能渲染”，从而走到 unicode 类别分支
        return True

class _FakeCfg:
    formular_char_pattern = None

fm = _FakeFontMapper()
cfg = _FakeCfg()
for ch in ["∑", "α", "5", "[", "•", "a", ",", " "]:
    print(f"char={ch!r:6} is_start={is_formulas_start_char(ch, fm, cfg)}")
```

2. （可选）观察真实分类结果：用 `--debug` 跑一次翻译，在工作目录里找到 `styles_and_formulas.json` 快照（落盘点见下方 4.2.3），对照某一段落的 `pdf_paragraph_composition`，区分哪些是 `pdfFormula`、哪些是 `pdfLine`。

**需要观察的现象**：

- 字体名部分：`Cambria Math` / `STIXTwoMath-Regular` / `CMMI12` 应为 `True`，`ArialMT` / `Helvetica` 应为 `False`。
- 字符部分：`∑ α 5 [ •` 这类应为 `True`（数学符号/希腊字母/数字/括号/项目符号），`a` 与普通空格应为 `False`（注意 `is_formulas_start_char` 对纯空格返回 `False`，因为 `char != " "` 这一支不成立）。

**预期结果**：脚本输出与上述判断一致。若你的 `is_formulas_font` 因缓存命中返回了上一次结果，属于 `@functools.cache` 正常行为。

> 待本地验证：若你的环境里 `unicodedata.category("∑")` 等返回值与预期不符，以你本地实际输出为准（`∑` 属 `Sm`，应为 True）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_formulas_middle_char` 把逗号也算作公式字符，而 `is_formulas_start_char` 不算？请结合状态机说明。

**参考答案**：逗号单独出现时往往是正文标点，不能作为「公式段开头」；但当状态机已经处于公式中（`in_formula_state=True`），逗号大概率属于公式内部的列举（如向量 `(1,2,3)`），所以只在「中间」时才认。这避免了把正文里的普通逗号误判为公式。

**练习 2**：`--formular-font-pattern` 提供后，会替换三组 pattern 中的哪一组？其余两组是否还生效？

**参考答案**：只替换**宽泛 pattern** `broad_formula_font_pattern`（[`formular_helper.py:267-268`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L267-L268)）。精确 pattern 与已知非公式 pattern（`pattern_text`）依然内置且先生效。因此即使用户给了宽泛 pattern，`Cambria Math` 这类精确命中仍优先返回 True，`ArialMT` 这类已知非公式仍优先返回 False。

---

### 4.2 公式合并与 IoU 归并

#### 4.2.1 概念说明

字符级识别后，同一段落里可能出现「很多个碎片公式」——比如角标被单独切出、或公式中间夹了个正文逗号被拆开。同时，公式在 PDF 里往往由**字符 + 矢量曲线（curve）+ 表单（form）**共同绘制（例如分数线、根号、大括号是 curve）。本阶段要解决两个问题：

1. **合并相邻公式**：把本该是一个整体的碎片公式缝合成一个 `PdfFormula`。
2. **把曲线/表单归并进公式**：让公式「吃掉」自己内部的 curve/form，这样翻译与排版时这些矢量元素跟随公式一起保留，不会单独翻译或丢失。

这里的核心工具是 **IoU（交并比）**——但要注意，BabelDOC 的 `calculate_iou_for_boxes(a, b)` 计算的是「a、b 交集 ÷ a 面积」，衡量的是 **a 被覆盖的比例**，是非对称的。

#### 4.2.2 核心流程

**合并公式**（`merge_overlapping_formulas`）在同一段落内反复扫描相邻公式对，满足任一条件即合并，直到无可合并：

- 同一行（`line_id` 相同），且 x 轴重叠 + y 轴有交集；**或**
- 同一行，且 x 轴相邻 + y 方向 IoU > 0.5；**或**
- 两公式所有非空字符的 `formula_layout_id` 完全相同（同属一个版面公式块）；**或**
- 任一方向的 IoU > 0.8。

**归并 curve/form**（`collect_contained_elements`）采用「两阶段指派」：

```
阶段1 _collect_element_formula_candidates：
  对每条 curve / 每个 form，找所有「能装下它」的公式作为候选，
  候选用 (公式下标, 分数, 匹配类型) 记录：
    - 精确匹配 iou_exact（容差 0，IoU 即得分，更高）
    - 容差匹配 iou_tolerant（容差 2.0，按距离折算成 0.5~0.9 的分）

阶段2 _resolve_assignment_conflicts：
  对每个 curve/form，按「精确优先、再按分数」选出唯一最佳公式，
  把它从页面级 curve/form 列表移除，挂到该公式的 pdf_curve/pdf_form 上。
```

判定「能装下」的核心是 `is_element_contained_in_formula`：把公式 box 向外扩 2.0 容差，再算「元素被覆盖比例」，≥ 0.95 即认定「被装在里面」。

#### 4.2.3 源码精读

**① 合并相邻公式**：[`styles_and_formulas.py:1064-1154`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L1064-L1154)。注意 `while merged` 循环——每合并一对就从头重新扫，因为合并后 box 变了、可能产生新的可合并对。

```python
should_merge = same_line and (
    (j == i + 1 and (x重叠且y相交) or (x相邻且y_iou>0.5))
    or self._have_same_layout_ids(formula1, formula2, page)
    or calculate_iou_for_boxes(formula1.box, formula2.box) > 0.8
    or calculate_iou_for_boxes(formula2.box, formula1.box) > 0.8
)
```

合并动作把两公式的字符拼起来、继承 `line_id`、重算 box（`merge_formulas`，[`styles_and_formulas.py:1010-1021`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L1010-L1021)）。由于 IoU 非对称，第 4 个条件需要正反两个方向各算一次。

**② IoU 工具的真实定义**：[`layout_helper.py:566-586`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L566-L586)。

```python
def calculate_iou_for_boxes(box1, box2):
    ...
    intersection_area = (x_right - x_left) * (y_top - y_bottom)
    first_box_area = (box1.x2 - box1.x) * (box1.y2 - box1.y)
    return intersection_area / first_box_area   # 交集 / 第一框面积（非对称！）
```

> 这是一个「以讹传讹」的命名：它不是标准 IoU。在归并场景里调用方都把「元素 box」放在第一参数，于是得到的恰好是「元素被公式覆盖的比例」，物理意义正确。

**③ 「被装在里面」判定**：[`spatial_analyzer.py:20-50`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/spatial_analyzer.py#L20-L50) 的 `is_element_contained_in_formula`。

```python
expanded_formula_box = Box(
    x=formula_box.x - tolerance, y=formula_box.y - tolerance,
    x2=formula_box.x2 + tolerance, y2=formula_box.y2 + tolerance)  # tolerance=2.0
iou = calculate_iou_for_boxes(element_box, expanded_formula_box)   # 元素被覆盖比例
return iou >= containment_threshold  # 0.95
```

也就是说：**「元素至少 95% 的面积落在（外扩 2 个单位的）公式框内」就算被归并**。这解释了为什么一条画在公式内部的分数线 curve 会被吸进公式——它几乎 100% 在公式框里。

**④ 两阶段指派**：候选收集 [`styles_and_formulas.py:159-255`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L159-L255)，冲突裁决 [`styles_and_formulas.py:257-323`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L257-L323)。容差匹配把「距离」折成分数：`score = 0.5 + 0.4 * (1 - distance/100)`，确保它永远低于精确匹配的 IoU 分（精确匹配分即 IoU，接近 1）。裁决时 `_get_best_candidate` 按 `(精确优先级, -分数)` 排序取最优。最后在 [`styles_and_formulas.py:345-360`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L345-L360) 把命中的 curve/form 从 `page.pdf_curve` / `page.pdf_form` 移除，挂到 `formula.pdf_curve` / `formula.pdf_form`。

**⑤ 入口与开关**：`collect_contained_elements` 在 `process_page` 里仅在「非 OCR workaround」时调用（[`styles_and_formulas.py:372-373`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L372-L373)），因为 OCR workaround 模式下版面结构不可靠。

#### 4.2.4 代码实践

**实践目标**：用真实函数验证「曲线为何被归入公式」，亲手体会 0.95 阈值与 2.0 容差。

**操作步骤**：运行下面这段「示例代码」（仅依赖 IL 的 `Box` 与 `spatial_analyzer`，无需 PDF）。

```python
# 示例代码：验证 curve/form 是否被归并入公式
from babeldoc.format.pdf.document_il.il_version_1 import Box
from babeldoc.format.pdf.document_il.utils.spatial_analyzer import (
    is_element_contained_in_formula,
)

formula_box = Box(x=100, y=200, x2=300, y2=240)        # 一个公式框（宽 200，高 40）
curve_inside  = Box(x=110, y=205, x2=290, y2=235)      # 几乎完全在公式内（分数线）
curve_partial = Box(x=250, y=205, x2=400, y2=235)      # 一半在公式内
curve_outside = Box(x=400, y=200, x2=500, y2=240)      # 完全在公式外

for name, cb in [("inside", curve_inside), ("partial", curve_partial), ("outside", curve_outside)]:
    print(f"{name:8s} contained={is_element_contained_in_formula(cb, formula_box)}")
```

**需要观察的现象**：`inside` 为 `True`（≥95% 覆盖）；`outside` 为 `False`；`partial` 取决于精确覆盖比例——大约只覆盖了一半宽度，预期为 `False`。

**预期结果**：`inside=True, outside=False, partial=False`（partial 的覆盖比例约 50%，远低于 0.95）。这正是「曲线必须几乎整体落在公式内才会被吸附」的体现。

#### 4.2.5 小练习与答案

**练习 1**：`calculate_iou_for_boxes(a, b)` 与标准 IoU 有何不同？在 `merge_overlapping_formulas` 里为什么第 4 个条件要正反各算一次？

**参考答案**：标准 IoU 是「交集 / 并集」，对称；而这里 `calculate_iou_for_boxes(a, b)` 是「交集 / a 面积」，非对称，衡量 a 被覆盖的比例。由于非对称，`iou(f1, f2)` 与 `iou(f2, f1)` 一般不等（一个小框几乎全在大框里时，小框作 a 得分接近 1，大框作 a 得分很小）。所以判断「两框高度重叠」需要两个方向都算，任一超过 0.8 即认定重叠。

**练习 2**：容差匹配（`iou_tolerant`）的分数被刻意压在 0.5~0.9，为什么？

**参考答案**：为了让「精确匹配（零容差）」永远排在容差匹配之前（见 `_get_best_candidate` 的 `(priority, -score)` 排序）。零容差能命中说明元素和公式几乎严丝合缝，置信度最高；带 2.0 容差的匹配是「差不多在附近」，应作为次优先。把容差分数上限定在 0.9（< 精确匹配常见的 ~1.0），就能在两者并存时稳定选出精确那个。

---

### 4.3 公式偏移计算

#### 4.3.1 概念说明

很多论文里，行内公式（inline formula）并不是和正文严格底对齐的——比如 `\sum` 的底部会比正文基线低一些，根号顶端会高出正文。译文替换正文后，如果不记录公式相对正文的**原始偏移**，排版时公式就会跳到错误的位置。

所以本阶段为每个公式计算两个量：

- **x_offset**：公式相对其左侧正文末字符的横向偏移（公式左边界 − 左侧文字右边界）。
- **y_offset**：公式相对同行正文的纵向偏移（公式底 − 同行文字底）。

这些偏移写入 `PdfFormula.x_offset` / `y_offset`（[`il_version_1.py:1015-1028`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L1015-L1028)），供下游 `Typesetting`（u7-l1）排版时还原公式位置。可用 `--skip-formula-offset-calculation` 跳过。

#### 4.3.2 核心流程

对每个段落里的每个公式 composition：

1. 向左找最近的「同行正文字符」：用 `calculate_y_true_iou_for_boxes(公式框, 字符框) > 0.6` 判断同行，取第一个命中的作为 `left_char`。
2. 向右找最近的同行正文字符 `right_char`（同样阈值）。
3. 若左右都有，保留 y 重叠率更高的那一个（避免把跨行的字符误当邻居）。
4. **x_offset** = 公式框.x − left_char.box.x2；再做钳制：绝对值 < 0.1 归零、> 10 归零、< −5 归零。
5. **y_offset** = 公式框.y − 邻居字符.box.y（优先左邻居，否则右邻居，否则 0）；绝对值 < 0.1 归零。

#### 4.3.3 源码精读

**① 偏移主流程**：[`styles_and_formulas.py:807-903`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L807-L903) 的 `process_page_offsets`。查找左邻居的关键片段：

```python
for j in range(i - 1, -1, -1):
    comp = paragraph.pdf_paragraph_composition[j]
    if comp.pdf_line:
        for char in reversed(comp.pdf_line.pdf_character):
            if not char.pdf_character_id:
                continue
            left_iou = calculate_y_true_iou_for_boxes(formula.box, char.box)
            if left_iou > 0.6:
                left_char = char
                break
        break
```

x 偏移与钳制（节选）：

```python
if left_char:
    formula.x_offset = formula.box.x - left_char.box.x2
else:
    formula.x_offset = 0
if abs(formula.x_offset) < 0.1: formula.x_offset = 0
if formula.x_offset > 10: formula.x_offset = 0
if formula.x_offset < -5: formula.x_offset = 0
```

钳制规则的直觉：太小的偏移（< 0.1）视为噪声归零；正向偏移过大（> 10）说明这个「邻居」其实隔得很远、可能不是真邻居，归零避免误导；负向偏移过大（< −5，即公式显著跑到左侧文字之前）也归零。y 偏移只对 < 0.1 归零。

**② 同行判定工具**：[`layout_helper.py:618-647`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L618-L647) 的 `calculate_y_true_iou_for_boxes`，返回「y 交集高度 ÷ min(两框高度)」，比单纯比 y 坐标更鲁棒（容忍字号差异）。

**③ 公式 box 重算与偏移初始化**：`update_formula_data`（[`formular_helper.py:312-335`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/formular_helper.py#L312-L335)）每次都会用公式内所有字符的 `visual_bbox` 重新求 box，并把未设置的 `x_offset/y_offset/x_advance` 初始化为 0。`process_page` 在偏移计算前后各调用一次 `update_all_formula_data`（[`styles_and_formulas.py:64-68`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L64-L68)），保证 box 始终是最新的。

> 顺序细节：`process_page` 里偏移会被计算**两次**（[`styles_and_formulas.py:368-381`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L368-L381)）——一次在合并后、一次在归并 curve/form 与样式处理后。第二次是因为前面的步骤可能改变了段落 composition 列表，需要重新定位邻居。

#### 4.3.4 代码实践

**实践目标**：理解「同行判定」对偏移计算的影响，验证 y 重叠率阈值的作用。

**操作步骤**：

1. 用 `--debug` 翻译一个含行内公式的 PDF，找到 `styles_and_formulas.json`。
2. 在该 JSON 里定位一个 `pdfFormula` 节点，查看其 `xOffset` / `yOffset` 属性。
3. 对照源码，确认该公式左侧最近的正文字符 box，手工算 `公式.x − 左字.x2` 是否与 `xOffset` 一致。

**需要观察的现象**：行内公式（如 `E = mc²`）的 `xOffset` 通常为 0 或很小的正值（紧贴左侧文字）；独立公式块（独占一行、左右无同行正文）的 `xOffset` / `yOffset` 多为 0（找不到邻居）。

**预期结果**：手工复算值与 JSON 中 `xOffset` 一致（或因钳制规则被归零）。若该公式无左邻居，则 `xOffset=0`。

> 待本地验证：不同 PDF 的公式排版差异较大，具体数值以你本地 `styles_and_formulas.json` 为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `x_offset` 正向大于 10 要归零？举一个会被误判的场景。

**参考答案**：若左侧最近字符其实属于「上一段末尾」或「远处的另一个词」，算出的 x 差会很大（>10），这显然不是真正的紧邻关系。把它归零可避免下游排版按一个虚假的大间距把公式推开。典型场景：公式独占一行，但 composition 列表里它前面恰好残留了一个属于上一行的尾巴字符。

**练习 2**：`--skip-formula-offset-calculation` 跳过偏移后，下游排版会怎样？

**参考答案**：所有公式的 `x_offset/y_offset` 保持 `update_formula_data` 初始化的 0，排版时公式会按其自身 box 的绝对坐标落位，不再相对左邻居微调。对绝大多数「公式与正文底对齐良好」的 PDF 影响很小；但对存在基线错位的行内公式，可能出现轻微的竖直错位。该开关主要服务于某些偏移计算会引入问题的特殊 PDF（如 u5-l1 提到的 OCR workaround 场景）。

---

### 4.4 段落样式与配色

#### 4.4.1 概念说明

公式之外，段落里的正文也有「样式」——字体、字号、颜色（`GraphicState`，本质是一段透传给 PDF 的逐字符绘图指令，如 `0.8 g 0.8 G` 表示灰色）。本阶段（`process_page_styles`）做两件事：

1. **求段落基准样式** `base_style`：把段落里所有非公式字符的样式做「交集」——字段都相同就保留，不同就置 `None`，最后落到段落 `pdf_style` 上。这个基准样式下游排版会用到（如决定默认字体）。
2. **按样式重新分组**：把段落里相邻、样式相同的正文字符重新聚成 `PdfSameStyleCharacters` 节点，便于翻译与渲染时按统一样式批量处理。

「配色」则来自 `style_helper.py`：它预先定义了一组颜色 `GraphicState`（红橙黄绿蓝……），以及工厂 `create_pdf_style(r, g, b)`，用于在渲染水印、高亮等场景构造带颜色的样式。注意 `GraphicState` 的真正载体是字符串 `passthrough_per_char_instruction`，即一段 PDF 颜色操作符。

#### 4.4.2 核心流程

```
process_page_styles(page):
  对每个段落 paragraph：
    base_style = _calculate_base_style(paragraph)   # 非公式字符样式的“交集”
    paragraph.pdf_style = base_style
    遍历 composition：
      公式 → 原样保留（先冲刷掉当前累积的 same-style 组）
      正文行 → 逐字符：与 current_style 相同则累积，不同则另起一组
    末尾冲刷最后一组
    paragraph.pdf_paragraph_composition = 新组合列表
```

样式「相同」用 `is_same_style` 判定：`font_id` 相同、`font_size` 差 < 0.02、`GraphicState` 的 `passthrough_per_char_instruction` 相同。

#### 4.4.3 源码精读

**① 基准样式求交集**：[`styles_and_formulas.py:710-736`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L710-L736) 的 `_calculate_base_style`。先把所有非公式字符样式收集，再用 `_merge_styles` 两两求交：

```python
base_style = styles[0]
for style in styles[1:]:
    base_style = self._merge_styles(base_style, style)
```

**② 样式合并（交集）**：[`styles_and_formulas.py:747-781`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L747-L781)。`font_id` 不同→`None`；`font_size` 差 ≥ 0.02→`None`；`GraphicState` 指令不同→`None`。若某项为 `None`，再用众数（`_get_mode_value`）兜底（[`styles_and_formulas.py:738-745`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L738-L745)）。

**③ 样式相同判定**：[`layout_helper.py:344-353`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L344-L353) 的 `is_same_style`。

```python
return (style1.font_id == style2.font_id
        and math.fabs(style1.font_size - style2.font_size) < 0.02
        and is_same_graphic_state(style1.graphic_state, style2.graphic_state))
```

**④ 同样式重排**：[`styles_and_formulas.py:650-708`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L650-L708) 的 `process_page_styles`，用 `_create_same_style_composition`（[`styles_and_formulas.py:783-805`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L783-L805)）把同样式字符包成 `PdfSameStyleCharacters`。

**⑤ 配色工厂与预设**：[`style_helper.py:4-25`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/style_helper.py#L4-L25) 的 `create_pdf_style` 把 RGB(0-255) 归一化到 0~1，拼成 `r g b rg` 指令字符串；预设颜色如 [`style_helper.py:28-94`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/style_helper.py#L28-L94) 的 `RED`/`GRAY80`/`BLACK` 等，都是直接写死的 PDF 颜色操作符。`PdfStyle` 与 `GraphicState` 的结构见 [`il_version_1.py:584-609`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L584-L609) 与 [`il_version_1.py:54-63`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L54-L63)。

> 顺带一提：`process_page` 在样式处理前还有 `process_comma_formulas`（按逗号拆分含逗号的复杂公式，[`styles_and_formulas.py:1185-1223`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L1185-L1223)）、`process_translatable_formulas`（把「纯数字/数字加逗号」这类其实可翻译的公式降级为普通文本行，[`styles_and_formulas.py:621-648`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L621-L648)）、以及（当 `--remove-non-formula-lines` 时）清理正文区里非公式的装饰线（[`styles_and_formulas.py:1225-1276`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/styles_and_formulas.py#L1225-L1276)）等收尾步骤，共同把段落整理成「公式 + 同样式文本块」的干净结构，交给翻译阶段。

#### 4.4.4 代码实践

**实践目标**：用真实的 `create_pdf_style` 与 `is_same_style` 验证「颜色样式」的构造与比较。

**操作步骤**：运行下面这段「示例代码」。

```python
# 示例代码：验证样式构造与同样式判定
from babeldoc.format.pdf.document_il.utils.style_helper import (
    create_pdf_style, RED, BLACK,
)
from babeldoc.format.pdf.document_il.utils.layout_helper import is_same_style

red_style  = create_pdf_style(255, 0, 0)         # 红色
same_red   = create_pdf_style(255, 0, 0)         # 同样的红
black_style = create_pdf_style(0, 0, 0)          # 黑色

print("red == same_red ?", is_same_style(red_style, same_red))   # 预期 True
print("red == black   ?", is_same_style(red_style, black_style)) # 预期 False
print("RED instruction:", RED.passthrough_per_char_instruction[:30], "...")
```

**需要观察的现象**：`create_pdf_style(255,0,0)` 生成的 `passthrough_per_char_instruction` 形如 `1.0000000000 0.0000000000 0.0000000000 rg`；两个相同颜色生成的样式 `is_same_style` 为 True；颜色不同则为 False。

**预期结果**：`True / False`，且 `RED` 的指令与 `create_pdf_style(255,0,0)` 生成的指令一致（均表示纯红）。

#### 4.4.5 小练习与答案

**练习 1**：`_calculate_base_style` 为什么要做「交集」而不是「取第一个」？

**参考答案**：段落里可能混有多种样式（如部分加粗、部分正常）。取交集意味着「只有当所有字符都同意某项属性时，才把它作为段落基准」，否则该项置 `None`（再用众数兜底）。这样得到的基准样式最能代表段落的「共同底色」，避免被个别特殊字符带偏。下游排版用它作为默认，再对各 composition 的局部差异单独处理。

**练习 2**：`GraphicState` 的颜色是直接存 RGB 数值吗？

**参考答案**：不是。它存的是一段字符串 `passthrough_per_char_instruction`，即原汁原味的 PDF 颜色操作符（如 `0.8 g 0.8 G` 表示灰色填充与描边、`1 0 0 rg` 表示红色填充）。`create_pdf_style` 的工作就是把 RGB 归一化后拼成这种指令字符串。这种「透传指令」设计让 BabelDOC 不必自己实现 PDF 颜色模型，直接把指令写回 PDF 即可。

---

## 5. 综合实践

把四个模块串起来，做一个**端到端的公式识别小追踪**：

1. 准备一份含行内公式与独立公式块的英文论文 PDF（也可用仓库自带的 `examples/ci/test.pdf`）。
2. 用以下命令翻译并开启 debug（语言与服务按你本地配置替换）：

   ```bash
   babeldoc --openai --openai-api-key <KEY> \
            --files examples/ci/test.pdf \
            --formular-font-pattern ".*MyMathFont.*" \
            --debug --output ./out
   ```

3. 在工作目录找到 `styles_and_formulas.json`（落盘点为 [`high_level.py:980-984`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L980-L984)），完成下面四件事：
   - **识别**：任选一段，统计它的 `pdf_paragraph_composition` 里有几个 `pdfFormula`、几个 `pdfLine`，验证 4.1 的字符二分类。
   - **合并**：找一个含角标的公式，确认它没被拆成多个 `pdfFormula`（4.2 的合并生效）。
   - **归并**：找一个 `pdfFormula`，检查它的 `pdfCurve` / `pdfForm` 子节点是否非空——若有，说明这些曲线被 4.2 的 IoU 归并吸附进来了；用本讲的示例代码思路，手工验证其 box 是否 ≥95% 落在公式 box 内。
   - **偏移与样式**：查看该公式的 `xOffset`/`yOffset`，以及所在段落的 `pdfStyle`，对照 4.3、4.4 解释其取值。
4. 重新跑一次，加上 `--skip-formula-offset-calculation`，对比同一公式的 `xOffset`/`yOffset` 是否都变成 0，验证 4.3 的开关效果。

> 待本地验证：`examples/ci/test.pdf` 是否含足够的行内公式取决于其内容；若公式很少，可换一份真实的论文 PDF 进行观察。

## 6. 本讲小结

- `StylesAndFormulas`（stage_name = `Parse Formulas and Styles`，权重仅 1.66，是 midend 里较轻的阶段）承接 `ParagraphFinder`，把段落里的字符**二分类**为公式/正文，是「公式不翻译」这一核心策略的落地。
- **公式字符识别**综合三类信号：字体名三段式 pattern（精确 > 已知非公式 > 宽泛，`--formular-font-pattern` 只替换宽泛组）、字符 Unicode 属性（`Sm`/希腊/数字/`(cid:)` 等）、字体能力与角标状态机。
- **公式合并**靠 `while merged` 反复扫描相邻公式对，用同 `line_id` + 多种空间/IoU 条件缝合碎片；**curve/form 归并**靠两阶段指派，核心是 `is_element_contained_in_formula` 的「元素 ≥95% 落在（外扩 2.0 的）公式框内」判定。
- 关键几何工具 `calculate_iou_for_boxes(a,b)` **非对称**（交集÷a 面积），衡量「a 被覆盖比例」，调用方都把元素放第一参数。
- **公式偏移** `x_offset/y_offset` 通过找同行正文邻居（y 重叠率 > 0.6）计算，并经多重钳制归零，供下游 `Typesetting` 还原行内公式位置；`--skip-formula-offset-calculation` 可关闭。
- **段落样式**求非公式字符样式的交集作为基准 `pdf_style`，并按相同样式重排为 `PdfSameStyleCharacters`；颜色来自 `style_helper.py` 的 `GraphicState` 透传指令。

## 7. 下一步学习建议

- 本阶段产出的 `PdfFormula` 会在翻译阶段被替换为占位符（`{v1}`），建议接着学习 **u6-l2「IL 翻译编排：占位符、批处理与线程池」**，看公式占位符如何构造与还原。
- 公式的 `x_offset/y_offset` 与 box 会被 **u7-l1「排版重排：Typesetting」** 消费，用于把译文贴回原版面，可对照阅读理解偏移量的真正用途。
- 若对版面驱动的字符归属感兴趣，可回顾 **u5-l2「版面分析」**——本阶段大量用到的 `formula_layout_id` 正是版面分析阶段写入的。
- 想了解 `PdfSameStyleCharacters` 等组合节点如何被翻译成 `PdfSameStyleUnicodeCharacters`（译文侧的富文本），可继续学习 **u6-l2** 与 **u3-l1** 的数据模型对照。
