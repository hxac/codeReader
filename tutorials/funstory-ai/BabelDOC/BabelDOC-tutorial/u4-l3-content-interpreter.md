# u4-l3 内容流解释器：interpreter 与 glyphs

## 1. 本讲目标

在 u4-l2 里我们解决了「PDF 的字节如何被读懂成一个个对象」的问题：字典、数组、名字、字符串、数字……都被解析成了 Python 值。但这些对象本身还不会「动」——它们只是静态的积木。

真正让一页 PDF「画」出来的，是一段叫做 **内容流（content stream）** 的指令序列。它长得像这样：

```text
BT
/F1 12 Tf
1 0 0 1 72 720 Tm
(Hello) Tj
ET
```

这段指令的意思是：开始一个文本对象（`BT`）、把字体设成 `/F1` 12 号（`Tf`）、把文本矩阵挪到 (72,720)（`Tm`）、显示字符串 `Hello`（`Tj`）、结束文本对象（`ET`）。

本讲要回答的核心问题是：**BabelDOC 如何把这一长串操作符，翻译成程序能消化的「语义事件」，并最终计算出每个字形（glyph）落在页面上的精确坐标？**

学完本讲你应该能够：

1. 说清楚 PDF 内容流**操作符（operator）**的分类（图形状态、路径绘制、文本、XObjet），以及「操作数 + 操作符」的栈式执行模型。
2. 看懂 `interpreter.py` 里 `TextContentInterpreter` 如何维护一个**解释器状态机**（图形状态、文本状态、文本对象），并在遇到每个操作符时吐出一个**事件（Event）**——尤其是 `TextRunEvent`、`SetFontEvent`、`ConcatMatrixEvent`、`TextMatrixEvent`。
3. 掌握 `glyphs.py` 如何把一个 `TextRunEvent`（一段文本运行）**逐字形展开**成 `GlyphEvent`，再计算出每个字形的包围盒得到 `PositionedGlyphEvent`，并能解释其中用到的字体度量与坐标矩阵。
4. 理解 `page_api.py` 这层「解释入口」是如何把分词器、解释器、XObjet 递归与字形展开串成一条完整管线的。

本讲是 u4-l2 的直接后继：内容流操作符的**操作数**，正是 u4-l2 讲的那些 PDF 对象（`PdfString`、`PdfName`、数字、数组）。本讲又是 u4-l4（active 运行时与字体后端）的前置——字形的解码、宽度、包围盒，全靠字体后端提供。

---

## 2. 前置知识

### 2.1 你已经知道（来自 u1～u4-l2）

- frontend（解析前端）的总体目标是「PDF → IL」。产品入口是 `parse_prepared_pdf_with_new_parser_to_legacy_ir`，最终用 `ActiveILCreater` 把解析到的事件投影成 IL 实体（见 u4-l1）。
- 一个 **prepared page** 是「页视图 + 对象视角」合并后的解析原料，内容流字节就挂在它身上（见 u4-l1）。
- PDF 文件由若干类对象（字典、数组、名字、字符串、数字、间接引用、流）拼成（见 u4-l2）。本讲里内容流操作符吃掉的，就是这些对象。

### 2.2 本讲需要的新概念

**操作符与操作数（operator / operand）。** 内容流是一条「后缀式」指令流：先压入若干个操作数（数字、字符串、数组），再跟一个操作符（一个关键字，比如 `Tf`、`Tj`、`cm`）。操作符消费紧挨在它前面的操作数。比如 `/F1 12 Tf` 里，`/F1` 和 `12` 是操作数，`Tf`（set font）是操作符。

> 类比：这就像计算器里的「逆波兰表达式」`3 4 +`——先压 3、再压 4、遇到 `+` 就弹出两个数相加。区别只是 PDF 的操作符种类多得多。

**图形状态机（graphics state）。** PDF 渲染是一个有状态的机器。它维护一个「当前变换矩阵 CTM（Current Transformation Matrix）」、线宽、颜色、字体、文本矩阵等等一堆「当前设置」。`q`/`Q` 操作符像括号一样保存/恢复这些设置。解释器必须忠实地维护这套状态，才能正确算出每个字形的坐标。

**事件驱动（event-driven）。** `interpreter.py` 不直接产出最终结果，而是把每条有语义的操作符翻译成一个**事件对象**（`TextRunEvent`、`SetFontEvent`……），丢进一个「水槽」（sink）。下游想干什么（投影成 IL、做调试快照、统计字数）由下游决定。这是「解释」与「消费」的解耦。

**字形（glyph）与字体度量（font metrics）。** 一个「字符」在 PDF 里其实是一个**字形**，用一个整数 CID（Character ID）标识。字体的「度量」告诉我们：这个 CID 多宽（`char_width`）、它的 Unicode 是什么（`unicode_text`）、它在竖排时偏移多少（`char_disp`）、字体下沿多深（`get_descent`）。要把文本画对位置，必须逐字形查这些度量。详见 u4-l4。

**坐标矩阵（matrix）。** PDF 用一个 6 元组 \((a,b,c,d,e,f)\) 表示一个二维仿射变换。它作用在一个点 \((x,y)\) 上的方式是：

\[
\begin{bmatrix} x' \\ y' \\ 1 \end{bmatrix}
=
\begin{bmatrix} a & c & e \\ b & d & f \\ 0 & 0 & 1 \end{bmatrix}
\begin{bmatrix} x \\ y \\ 1 \end{bmatrix},
\quad
\text{即 } x' = ax + cy + e,\ y' = bx + dy + f
\]

一个字形的最终设备坐标，要把「字形局部坐标 → 文本矩阵 Tm → 当前变换矩阵 CTM」一串矩阵乘起来。

> 如果你对 PDF 内容流的规范细节感兴趣，可以配合阅读 [docs/intro-to-pdf-object.md](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/docs/intro-to-pdf-object.md)（u4-l2 也用到过）。

---

## 3. 本讲源码地图

本讲涉及的关键文件，按「从指令到坐标」的数据流顺序排列：

| 文件 | 作用 | 本讲角色 |
|------|------|---------|
| [babeldoc/format/pdf/new_parser/tokenizer.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/tokenizer.py) | 内容流**分词器**：字节 → `PdfOperation`（操作数列表 + 操作符） | 起点：提供被解释的指令 |
| [babeldoc/format/pdf/new_parser/interpreter.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py) | 内容流**解释器**：操作符 → 事件；维护状态机 | **主角之一**：事件模型与状态机 |
| [babeldoc/format/pdf/new_parser/glyphs.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/glyphs.py) | 字形展开：`TextRunEvent` → `GlyphEvent` → `PositionedGlyphEvent` | **主角之二**：逐字形算坐标 |
| [babeldoc/format/pdf/new_parser/page_api.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/page_api.py) | 解释**入口门面**：把分词、解释、XObjet 递归装配到一起 | 装配层 |
| [babeldoc/format/pdf/new_parser/xobject_content_execution.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/xobject_content_execution.py) | 在解释器外层包一层 **XObjet 递归** | 关键：解释入口的真实实现 |
| [babeldoc/format/pdf/new_parser/state.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/state.py) | 解释器状态机（CTM/文本状态/文本对象）与矩阵运算 | 支撑：状态与数学 |
| [babeldoc/format/pdf/new_parser/font_types.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/font_types.py) | `PdfFontLike` 协议：字体度量接口 | 支撑：glyphs 依赖的字体能力 |

> 一句话定位：`tokenizer.py` 切指令，`interpreter.py` 把指令变成事件，`glyphs.py` 把文本事件变成带坐标的字形，`page_api.py` 把这三者串成一页的完整解释。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应一条数据流：

```text
内容流字节
  │  tokenizer.py（分词）
  ▼
list[PdfOperation]   （操作数 + 操作符）
  │  interpreter.py（解释 + 状态机）
  ▼
list[Event]          （TextRunEvent / SetFontEvent / ConcatMatrixEvent / …）
  │  glyphs.py（逐字形展开）
  ▼
list[PositionedGlyphEvent]   （每个字形的 CID + Unicode + bbox + 矩阵）
```

### 4.1 内容流操作符：PDF 的「绘画指令集」

#### 4.1.1 概念说明

一页 PDF 的内容流，本质上是一段**绘绘画指令序列**。每条指令形如「若干操作数 + 一个操作符」。PDF 规范定义了大约七八十种操作符，BabelDOC 按用途把它们分成几大类：

- **图形状态（graphics state）**：`q`/`Q`（保存/恢复状态）、`cm`（拼接变换矩阵）、`w`/`J`/`j`/`M`/`d`（线宽/线帽/线连接/斜接/虚线）、`rg`/`g`/`k`/`sc`（颜色）、`gs`（扩展图形状态）。
- **路径构造（path construction）**：`m`（移动到）、`l`（直线到）、`c`/`v`/`y`（贝塞尔曲线）、`re`（矩形）、`h`（闭合）。
- **路径绘制（path painting）**：`S`/`s`（描边）、`f`/`f*`（填充）、`B`/`b`（填充并描边）、`n`（结束路径不画）、`W`/`W*`（裁剪）。
- **文本（text）**：`BT`/`ET`（开始/结束文本对象）、`Tf`（设字体）、`Tm`（文本矩阵）、`Td`/`TD`/`T*`（换行定位）、`Tj`/`TJ`/`'`/`"`（显示文本）、`Tc`/`Tw`/`Tz`/`TL`/`Ts`/`Tr`（字符间距/词间距/缩放/行距/基线偏移/渲染模式）。
- **XObjet 与图像**：`Do`（调用一个 Form/Image XObjet）、`INLINE_IMAGE`（内联图像）、`sh`（着色）。

> **为什么要分这么多类？** 因为 PDF 是一种「声明式」的绘图语言：它不写「循环、if」，而是用一连串状态修改 + 绘制指令来描述「这一页长什么样」。理解这些操作符，就理解了 PDF 页面是怎么被「画」出来的。

#### 4.1.2 核心流程

内容流的执行是一个**栈式 + 有状态**的过程：

```text
1. 分词器把字节流切成一条 PdfOperation 序列。
2. 解释器逐条取出 operation：
   a. 把 operation.operands 看作「这条指令的操作数」；
   b. 根据 operation.operator 查表找到对应的处理函数；
   c. 处理函数：
      - 修改解释器状态（CTM、文本状态、当前路径……）；
      - 往 sink 里 emit 一个（或多个）事件；
3. 全部跑完后，sink.events 就是这页的完整事件流。
```

注意一个细节：PDF 内容流里，**操作数是先于操作符出现的**。比如 `/F1 12 Tf`，`/F1` 和 `12` 是 `Tf` 的操作数。分词器会把它们攒进 `PdfOperation.operands` 列表，等遇到 `Tf` 才打包成一个 operation。这一点在 `tokenizer.py` 的 `iter_operation_stream` 里看得很清楚（见 [tokenizer.py:168-188](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/tokenizer.py#L168-L188)）：它维护一个 `operands` 列表，遇到普通 token 就 `append`，遇到关键字（`PdfKeyword`）就 `yield` 一个 `PdfOperation(operands, keyword)` 并清空列表。

#### 4.1.3 源码精读

`PdfOperation` 这个数据类只有两个字段，极简但极关键（[tokenizer.py:114-117](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/tokenizer.py#L114-L117)）：

```python
@dataclass(frozen=True)
class PdfOperation:
    operands: list[object]   # 这条指令的操作数（数字 / PdfString / PdfName / list / 字典 …）
    operator: str            # 操作符关键字，如 "Tf"、"Tj"、"cm"、"re"
```

> 中文说明：`operands` 是「喂给操作符的参数」，`operator` 是「要执行的动作」。这正是栈式执行里「先压操作数、再吃操作符」的产物。

`TextContentInterpreter` 在构造时用一个大字典 `operator_to_handler` 把每个操作符映射到一个处理函数（[interpreter.py:240-310](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L240-L310)）。比如：

```python
"cm": self._op_cm,
"Tf": self._op_tf,
"Tm": self._op_tm,
"Tj": self._op_tj,
"TJ": self._op_tj_array,
"q": self._op_q,
"Q": self._op_q_restore,
```

而它支持的**全部**操作符清单写在类常量 `SUPPORTED_OPERATORS` 里（[interpreter.py:155-225](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L155-L225)）。解释器在 `execute` 里会先校验操作符是否在白名单内，不在就抛 `UnsupportedOperatorError`（[interpreter.py:317-325](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L317-L325)）：

```python
def execute(self, operation: PdfOperation) -> None:
    operator = operation.operator
    if operator not in self.SUPPORTED_OPERATORS:
        raise UnsupportedOperatorError(operator)
    self._record_text_object_operation(operation)
    handler = self.operator_to_handler[operator]
    operands = [*self.argstack, *operation.operands]   # 把栈上残留的操作数拼进来
    self.argstack.clear()
    handler(operands)
```

> 中文说明：`execute` 是每条指令的统一入口。它先校验操作符合法性，再把「上一条指令没消费完、留在 `argstack` 里的操作数」和本条操作数拼起来交给处理函数。这个 `argstack` 机制是为了兼容**畸形内容流**（操作数数量不对）——见 `_require_arity` 的注释（[interpreter.py:794-810](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L794-L810)），它模仿 pdfminer 的行为：操作符只消费它需要的尾部操作数，多余的前导操作数留在栈上给下一条指令。

#### 4.1.4 代码实践

**实践目标**：亲手用分词器把一段最小内容流切成 `PdfOperation`，确认「操作数在前、操作符在后」的结构。

**操作步骤**（在仓库根目录用 Python REPL）：

```python
# 示例代码：非项目原有，用于观察分词结果
from babeldoc.format.pdf.new_parser.tokenizer import tokenize_operations

stream = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Hello) Tj ET"
for op in tokenize_operations(stream):
    print(op.operator, "←", op.operands)
```

**需要观察的现象**：输出应该是若干行，每一行先打印操作符，再打印它前面的操作数列表。注意 `Tm` 前面有 6 个数字（矩阵 6 元组），`Tj` 前面是一个 `PdfString`（`raw=b'Hello'`）。

**预期结果**：

```text
BT ← []
Tf ← [PdfName(value='F1'), 12]
Tm ← [1.0, 0.0, 0.0, 1.0, 72.0, 720.0]
Tj ← [PdfString(raw=b'Hello', is_hex=False)]
ET ← []
```

> 这是**源码阅读型实践**，不依赖任何 PDF 文件或 API key，可直接在本地验证。若你的环境里 `tokenize_operations` 的导入路径有差异，以本仓库 `tokenizer.py` 顶部的导出为准。

#### 4.1.5 小练习与答案

**练习 1**：内容流 `[(-100) (World)] TJ` 里，`TJ` 的操作数是什么？`-100` 这个负数在文本显示里起什么作用？

**参考答案**：`TJ` 的操作数是一个**数组**，里面交替存放字符串和数字。`PdfString(b'World')` 是要显示的文本，`-100` 是一个**负的字距调整值**（kerning）：在显示时它会沿书写方向把光标**回退**一段距离，用来微调字间距（详见 4.3 节，负数会被乘以 `dxscale`）。

**练习 2**：为什么 `execute` 里要把 `self.argstack` 拼到操作数前面？如果删掉这步会怎样？

**参考答案**：为了兼容**操作数数量不符**的畸形内容流。PDF 规范要求每个操作符消费固定数量的操作数，但真实世界的 PDF 常有多余或少操作数的情况。`_require_arity` 在操作数过多时会把多余的前导操作数压回 `argstack`，留给下一条指令；如果删掉拼接逻辑，这些「遗留操作数」就会丢失，导致后续指令拿到错误的操作数，进而算错坐标或丢字。

---

### 4.2 解释器事件类型：把操作符翻译成语义事件

#### 4.2.1 概念说明

`interpreter.py` 的设计哲学是**「只解释，不消费」**：它把每个有语义的操作符翻译成一个不可变（`frozen=True`）的事件对象，丢进 `CollectingEventSink`；至于事件拿来干什么（投影成 IL？统计？调试？），由下游决定。

这种「解释器 → 事件 → sink」的三段式，让同一份内容流可以被多种消费者复用，互不干扰。事件就是解释器和消费者之间的**契约**。

本讲最关心的是这几个事件（它们是后续字形展开的输入）：

- **`TextRunEvent`**：一段文本运行。当遇到 `Tj`/`TJ`/`'`/`"` 时发出，携带「要显示的字节段 + 当下完整的文本状态（字体、字号、间距、矩阵……）」。这是字形展开的**唯一输入**。
- **`SetFontEvent`**：遇到 `Tf` 时发出，记录字体名与字号。
- **`ConcatMatrixEvent`**：遇到 `cm` 时发出，记录拼接后的 CTM。
- **`TextMatrixEvent`**：遇到 `Tm` 时发出，记录新的文本矩阵。
- **`BeginTextObjectEvent` / `EndTextObjectEvent`**：遇到 `BT`/`ET` 时发出，标记文本对象的边界。

此外还有路径类（`PathPaintEvent`、`ClipPathEvent`）、图像类（`ImageXObjectEvent`、`InlineImageEvent`）、XObjet 类（`BeginXObjectEvent`/`EndXObjectEvent`）等，服务于非文字内容（它们最终会投影成 IL 的曲线、图、表单）。

#### 4.2.2 核心流程

解释器内部维护一个状态机（`InterpreterState`，见 [state.py:182-215](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/state.py#L182-L215)），它由三部分组成：

```text
InterpreterState
├── graphics_state（图形状态）：ctm 当前变换矩阵、text_clip 文本裁剪标志
├── text_state（文本状态）：font_name, font_size, char_spacing, word_spacing,
│                          horizontal_scaling, leading, rise, render_mode
├── text_object（文本对象）：in_text_object, text_matrix, line_matrix
└── graphics_stack（状态栈）：q 压入、Q 弹出，用来保存/恢复上面三者
```

关键流程（以文本为例）：

```text
BT  → text_object.begin()：进入文本对象，文本矩阵/line_matrix 归一
Tf  → text_state 记录 font_name / font_size；emit SetFontEvent
Tm  → text_object.set_text_matrix()；emit TextMatrixEvent
cm  → graphics_state.ctm = 新矩阵 × 旧 ctm；emit ConcatMatrixEvent
Tj  → 取 PdfString.raw → emit TextRunEvent（带上当前全部状态快照）
ET  → text_object.end()：退出文本对象
```

注意：**事件里携带的是「这一瞬间的状态快照」**，而不是引用。比如 `TextRunEvent` 里直接拷贝了当时的 `text_matrix`、`ctm`、`font_name`、`font_size`……这样下游拿到事件时，哪怕后续状态又变了，这条文本运行的信息也是自洽的。

#### 4.2.3 源码精读

先看核心事件 `TextRunEvent` 的字段（[interpreter.py:22-37](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L22-L37)）：

```python
@dataclass(frozen=True)
class TextRunEvent:
    operator: str                              # "Tj" / "TJ" / "'" / '"'
    segments: tuple[bytes | float, ...]        # 要显示的字节段（bytes）或字距调整（float）
    text_matrix: Matrix                        # 文本矩阵 Tm
    line_matrix: Point                         # 当前行内的逻辑游标 (x, y)
    ctm: Matrix                                # 当前变换矩阵
    font_name: str | None                      # 字体名
    font_size: float                           # 字号
    char_spacing: float                        # 字符间距 Tc
    word_spacing: float                        # 词间距 Tw
    horizontal_scaling: float                  # 水平缩放 Tz
    leading: float                             # 行距 TL
    rise: float                                # 基线抬升 Ts
    render_mode: int                           # 渲染模式 Tr
    xobject_path: tuple[str, ...]              # 是否在某个 Form XObjet 内部
```

> 中文说明：`TextRunEvent` 是一个**自描述的文本运行快照**——它不光告诉你「显示什么」（`segments`），还把「用什么字体、多大、在哪、怎么变形」一次性全带上。有了这一份快照，下游（glyphs）就能独立算出每个字形的坐标，无需回头问解释器。

`segments` 这个字段很巧妙：对于 `Tj` 它就是单个 `bytes`；对于 `TJ`（数组形式）它是 `bytes`（字符串段）和 `float`（字距调整数）的混合元组。这正是 `TJ` 能做精细字距调整的关键。构造它的代码在 `_op_tj` 与 `_op_tj_array`（[interpreter.py:657-672](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L657-L672)）：

```python
def _op_tj(self, operands: list[object]) -> None:
    self._require_arity("Tj", operands, 1)
    self._emit_text_show("Tj", self._require_string(operands[0]).raw)   # 单个 bytes

def _op_tj_array(self, operands: list[object]) -> None:
    self._require_arity("TJ", operands, 1)
    seq = operands[0]
    if not isinstance(seq, list):
        raise ValueError("TJ expects a single array operand.")
    values: list[bytes | float] = []
    for item in seq:
        if isinstance(item, PdfString):
            values.append(item.raw)            # 字符串段保留 bytes
        else:
            values.append(self._require_number(item))   # 数字段转 float
    self._emit_text_show("TJ", values)
```

> 中文说明：`Tj` 取一个字符串；`TJ` 取一个数组，数组里字符串保留为 `bytes`、数字转 `float`。最终都汇入 `_emit_text_show`，由它统一构造 `TextRunEvent` 并 emit。

真正构造并发出 `TextRunEvent` 的地方是 `_emit_text_show`（[interpreter.py:686-721](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L686-L721)）。它先处理「渲染模式 7（仅裁剪）」的特殊情形（只推进游标不画字），否则把当前所有文本状态打包进事件：

```python
self.sink.emit(
    TextRunEvent(
        operator=operator,
        segments=segments,
        text_matrix=self.state.text_object.text_matrix,
        line_matrix=self.state.text_object.line_matrix,
        ctm=self.state.graphics_state.ctm,
        font_name=self.state.text_state.font_name,
        font_size=self.state.text_state.font_size,
        char_spacing=self.state.text_state.char_spacing,
        # …其余文本状态字段…
        xobject_path=self.xobject_path,
    )
)
self._advance_line_matrix(segments)   # 顺手把行内游标推进（见下）
```

> 中文说明：emit 完事件后，还调用 `_advance_line_matrix` 把「行内逻辑游标」`line_matrix` 往前推。这一步至关重要——它保证**同一次 `Tj`/`TJ` 内的多个字符、以及连续的多次文本显示**，光标能正确接续，从而每个字形的 x 坐标递增而不重叠。

`_advance_line_matrix`（[interpreter.py:741-789](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L741-L789)）的逻辑和 4.3 节的 glyphs 几乎一模一样（都是「沿书写方向累加字宽 + 字距」），它复用 `font_resolver` 拿到字体、用 `font.char_width(cid)` 累加 x（或竖排时累加 y）。这也是为什么后面 glyphs 模块能把「游标推进」这件事独立出来再算一遍而不依赖解释器——**同样的数学，算两遍**：解释器算一遍是为了维护状态（让下一条指令拿到正确的 `line_matrix`），glyphs 算一遍是为了产出每个字形的绝对偏移。

再看几个「状态修改型」事件。设字体的 `Tf`（[interpreter.py:602-608](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L602-L608)）：

```python
def _op_tf(self, operands: list[object]) -> None:
    self._require_arity("Tf", operands, 2)
    name = decode_pdf_name(self._require_name(operands[0]))
    size = self._require_number(operands[1])
    self.state.text_state.font_name = name
    self.state.text_state.font_size = size
    self.sink.emit(SetFontEvent(name, size))
```

> 中文说明：`Tf` 把字体名（解码 `#xx` 转义后）和字号写进文本状态，并发出 `SetFontEvent`。

拼接矩阵的 `cm`（[interpreter.py:338-345](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L338-L345)）：

```python
def _op_cm(self, operands: list[object]) -> None:
    self._require_arity("cm", operands, 6)
    matrix = self._matrix_from_operands(operands)
    self.state.graphics_state.ctm = multiply_matrices(matrix, self.state.graphics_state.ctm)
    self.sink.emit(ConcatMatrixEvent(self.state.graphics_state.ctm))
```

> 中文说明：`cm` 把新矩阵**左乘**到当前 CTM 上（`new_ctm = M × old_ctm`），再发出携带拼接后 CTM 的 `ConcatMatrixEvent`。矩阵乘法 `multiply_matrices` 见 [state.py:20-30](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/state.py#L20-L30)。

最后看「水槽」`CollectingEventSink`（[interpreter.py:146-151](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L146-L151)），它朴素到只有一个列表：

```python
class CollectingEventSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)
```

> 中文说明：sink 只负责「收集」。`run()` 跑完所有 operation 后，返回 `self.sink.events`（[interpreter.py:312-315](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L312-L315)），就是这页的完整事件流。生产环境会把 sink 换成真正投影成 IL 的 `ActiveILCreater`（见 u4-l1）。

> **事件类型速查表**（全部定义在 [interpreter.py:22-143](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L22-L143)）：
>
> | 事件 | 触发操作符 | 用途 |
> |------|-----------|------|
> | `TextRunEvent` | `Tj`/`TJ`/`'`/`"` | 一段文本运行（字形展开输入） |
> | `SetFontEvent` | `Tf` | 设字体 |
> | `ConcatMatrixEvent` | `cm` | 拼 CTM |
> | `TextMatrixEvent` | `Tm` | 设文本矩阵 |
> | `Begin/EndTextObjectEvent` | `BT`/`ET` | 文本对象边界 |
> | `Save/RestoreGraphicsStateEvent` | `q`/`Q` | 状态栈 |
> | `PathPaintEvent`/`ClipPathEvent` | `S`/`f`/`B`/`W`… | 路径绘制/裁剪 |
> | `ImageXObjectEvent`/`InlineImageEvent` | `Do`/`BI..EI` | 图像 |
> | `Begin/EndXObjectEvent` | `Do`（Form） | Form XObjet 进出 |
> | `ShadingPaintEvent` | `sh` | 着色 |

#### 4.2.4 代码实践

**实践目标**：用解释器跑一段内容流，把得到的事件按类型分类计数，直观感受「操作符 → 事件」的映射。

**操作步骤**：

```python
# 示例代码：非项目原有，用于观察事件流
from babeldoc.format.pdf.new_parser.interpreter import interpret_operations
from babeldoc.format.pdf.new_parser.tokenizer import tokenize_operations
from collections import Counter

stream = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Hello) Tj 0 -14 Td (World) Tj ET"
operations = tokenize_operations(stream)
events = interpret_operations(operations)

print("事件类型统计：", Counter(type(e).__name__ for e in events))
for e in events:
    print(type(e).__name__, e)
```

**需要观察的现象**：你会看到若干 `BeginTextObjectEvent`、`SetFontEvent`、`TextMatrixEvent`、两个 `TextRunEvent`、`EndTextObjectEvent`。注意两个 `TextRunEvent` 的 `segments` 分别是 `b'Hello'` 和 `b'World'`，且第二个的 `line_matrix`（或文本矩阵）相对第一个发生了位移（因为中间有 `Td`）。

**预期结果**（事件数量大致如此，具体字段值以本地运行为准）：

```text
事件类型统计： Counter({'TextRunEvent': 2, 'BeginTextObjectEvent': 1, 'SetFontEvent': 1, 'TextMatrixEvent': 1, 'EndTextObjectEvent': 1})
```

> 这同样是**源码阅读型实践**，无需 PDF 或 API。若想看 `Td` 如何移动文本矩阵，可对照 `TextObjectState.move_text_position`（[state.py:154-164](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/state.py#L154-L164)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TextRunEvent` 要把 `ctm`、`text_matrix`、`font_size` 等状态**拷贝**进事件，而不是只存一个对 `InterpreterState` 的引用？

**参考答案**：为了**解耦与时序安全**。事件会被收集进列表，供下游事后批量处理；如果只存引用，下游处理时解释器状态早已被后续操作符改写，拿到的就是错误的「当下」状态。把状态快照进事件，保证每个 `TextRunEvent` 都是自洽的、可独立计算的。

**练习 2**：渲染模式 `Tr` 取 7 时（见 `_emit_text_show` 的 `is_clip_only_text` 分支），解释器为什么只调用 `_advance_line_matrix` 而不 emit `TextRunEvent`？

**参考答案**：渲染模式 7 表示「**仅把文本当裁剪路径，不实际显示字形**」。这种文本对最终可见内容没有贡献，所以不发出 `TextRunEvent`（避免下游把它当普通文字投影成 IL 字符）；但仍需推进行内游标，保证后续同一文本对象里的可见文本位置正确。它会被标记为「文本裁剪穿透」供 backend 特殊处理（见 `_op_et` 里 `text_clip_passthrough` 的处理，[interpreter.py:587-600](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/interpreter.py#L587-L600)）。

---

### 4.3 字形展开 glyphs：从文本运行到带坐标的字形

#### 4.3.1 概念说明

`TextRunEvent` 告诉我们「这段字节要在某种字体/字号/矩阵下显示」，但它还**没有把字节拆成一个个字符，也没算出每个字符落在哪**。把 `segments` 里的字节展开成逐字形的几何信息，正是 `glyphs.py` 的职责。

这里有两步：

1. **展开（expand）**：`expand_text_run_events` 把 `TextRunEvent` 沿书写方向「走一遍」，用字体把字节解码成 CID 序列，逐字形累加字宽得到**行内偏移**，产出 `GlyphEvent`（带 CID、Unicode、矩阵、字宽，但**还没有包围盒**）。
2. **定位（position）**：`position_glyph_events` 在每个 `GlyphEvent` 基础上，用字体度量（下沿深度 `get_descent`、竖排位移 `char_disp`）算出字形的**包围盒 bbox**，产出 `PositionedGlyphEvent`。

为什么分两步？因为「算行内偏移」需要字体解码与字宽（轻），而「算包围盒」还需要字体的纵向度量（略重），分开让每一步职责单一、可单独调用。

> **实现现状（重要，需诚实说明）**：经检索，`glyphs.py` 里的 `expand_text_run_events` / `position_glyph_events` 目前**没有被生产主链路直接导入调用**——产品解析路径（`native_parse.py` → `NativeTextRunPositioner`）把同样的「行内游标推进 + 字形几何」数学**内联实现**了一遍，直接产出 sink 专用的 `AWLTChar`（见 [text_positioning.py:24-101](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py#L24-L101)）。因此 `glyphs.py` 在项目里的角色是**字形展开的规范/参考实现**：它用最清晰的数据类（`GlyphEvent`/`PositionedGlyphEvent`）表达了「文本运行如何变成带坐标字形」这件事本身，数学和生产路径完全一致。学懂它，就等于学懂了 `NativeTextRunPositioner` 的核心算法。

#### 4.3.2 核心流程

字形展开的关键，是**沿书写方向「走」一条逻辑游标**。设当前游标为 \((x, y)\)（来自 `line_matrix`），对 `segments` 里的每个元素：

```text
若元素是 float（TJ 的字距调整数 k）：
    x -= k * dxscale        # 横排：回退；竖排则改 y
    标记「下一个字符前要补字符间距」
若元素是 bytes（字符串段）：
    用 font.decode(bytes) 得到 CID 序列
    对每个 cid：
        unicode = font.unicode_text(cid)          # 查 Unicode
        advance = font.char_width(cid) * fontsize * scaling   # 该字宽度
        glyph_offset = (x, y)                      # 记录此刻游标位置
        产出 GlyphEvent（带 glyph_offset、advance、矩阵）
        x += advance                               # 游标前移
        若 cid == 32（空格）且有词间距：x += wordspace
```

其中三个比例因子是 PDF 文本布局的标准换算（来自 PDF 规范）：

\[
\text{scaling} = \frac{\text{Tz}}{100}, \quad
\text{charspace} = \text{Tc} \cdot \text{scaling}, \quad
\text{wordspace} = \text{Tw} \cdot \text{scaling}, \quad
\text{dxscale} = 0.001 \cdot \text{fontsize} \cdot \text{scaling}
\]

> `dxscale` 系数 `0.001` 出现，是因为 PDF 字体度量以 **1/1000** 为单位（1000 点 = 1 个 em）。TJ 里的字距调整数也是 1/1000 单位，所以要乘 `0.001 × fontsize × scaling` 才能换算成设备空间的实际距离。多字节字体（CJK）时 `wordspace` 被强制清零，因为 CJK 没有西式「空格分词」。

每个字形的最终矩阵是把「字形局部偏移」叠加到「Tm × CTM」上：

\[
\text{base} = \text{Tm} \times \text{CTM}, \quad
\text{glyph\_matrix} = \text{translate}(\text{base},\ \text{glyph\_offset})
\]

`translate_existing_matrix` 的效果是把一个平移 \((\text{offset}_x, \text{offset}_y)\) 叠加到矩阵的 e、f 分量上（[state.py:60-70](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/state.py#L60-L70)）。

定位阶段算包围盒时（横排为例），字形的两个角点取自字体度量：

```text
左下角 = (0, descent + rise)
右上角 = (advance, descent + rise + fontsize)
```

再用 `glyph_matrix` 把这两个角点变换到设备空间（`apply_matrix_pt`，[state.py:73-76](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/state.py#L73-L76)），归一化（保证 x0<x1、y0<y1）后即得 `bbox = (x0, y0, x1, y1)`。

#### 4.3.3 源码精读

先看两个产物数据类。`GlyphEvent`（[glyphs.py:13-31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/glyphs.py#L13-L31)）携带字形展开的全部中间信息（CID、Unicode、矩阵、偏移、字宽）：

```python
@dataclass(frozen=True)
class GlyphEvent:
    cid: int                         # 字形 ID
    unicode_text: str                # 对应 Unicode（查不到时回退 "(cid:N)"）
    font_name: str | None
    font_size: float
    xobject_path: tuple[str, ...]
    text_matrix: Matrix
    glyph_matrix: Matrix             # base 平移到本字形偏移后的矩阵
    ctm: Matrix
    glyph_offset: tuple[float, float]   # 行内游标位置 (x, y)
    advance: float                   # 本字宽度（设备空间）
    # …spacing/scaling/rise/render_mode/segment_index/glyph_index…
```

`PositionedGlyphEvent`（[glyphs.py:34-55](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/glyphs.py#L34-L55)）在它基础上加了 **包围盒与方向**：

```python
@dataclass(frozen=True)
class PositionedGlyphEvent:
    # …同 GlyphEvent 的字段…
    bbox: tuple[float, float, float, float]   # 设备空间包围盒 (x0,y0,x1,y1)
    size: float                               # 字号（取宽或高，依方向）
    vertical: bool                            # 是否竖排
```

> 中文说明：`GlyphEvent` 回答「这个字形是哪个 CID、Unicode 是什么、行内偏移多少」，`PositionedGlyphEvent` 进一步回答「它在页面上占据多大一块矩形」。后者是投影成 IL `PdfCharacter.box` 的直接来源（见 u3-l1）。

展开函数 `expand_text_run_events`（[glyphs.py:58-136](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/glyphs.py#L58-L136)）。核心两段——预处理比例因子与遍历 segments：

```python
font = resource_bundle.get_font(event.xobject_path, event.font_name)
if font is None:
    continue                                   # 字体缺失：跳过这段（不丢整个事件流）

scaling = event.horizontal_scaling * 0.01
charspace = event.char_spacing * scaling
wordspace = event.word_spacing * scaling
if font.is_multibyte():
    wordspace = 0                              # CJK 无词间距
dxscale = 0.001 * event.font_size * scaling
x, y = event.line_matrix                      # 行内游标起点
base_matrix = multiply_matrices(event.text_matrix, event.ctm)   # Tm × CTM

for segment_index, segment in enumerate(event.segments):
    if isinstance(segment, float):            # TJ 字距调整
        if vertical:
            y -= segment * dxscale
        else:
            x -= segment * dxscale
        need_charspace = True
        continue

    for cid in font.decode(segment):          # 字节 → CID 序列
        if need_charspace:
            x += charspace                    # 横排（竖排改 y）
        unicode_text = font.unicode_text(cid, f"(cid:{cid})")
        advance = font.char_width(cid) * event.font_size * scaling
        glyph_offset = (x, y)
        glyph_matrix = translate_existing_matrix(base_matrix, glyph_offset)
        glyphs.append(GlyphEvent(cid=cid, unicode_text=unicode_text, …))
        x += advance                          # 游标前移
        if cid == 32 and wordspace:
            x += wordspace                    # 空格额外加词间距
        need_charspace = True
```

> 中文说明：这段就是 4.3.2 节流程图的逐行落地。`resource_bundle.get_font` 沿 `xobject_path` 找到正确的字体（[resources.py:78-111](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/resources.py#L78-L111)）；`font.decode` 把字节解成 CID；`font.char_width` 给字宽。每产出一个字形，游标就前移一个 `advance`，于是同一段文本里的字符 x 坐标自然递增。

定位函数 `position_glyph_events`（[glyphs.py:139-204](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/glyphs.py#L139-L204)）算包围盒。横排分支：

```python
descent = font.get_descent() * glyph.font_size
bbox_lower_left = (0, descent + glyph.rise)
bbox_upper_right = (glyph.advance, descent + glyph.rise + glyph.font_size)
vertical = glyph.glyph_matrix[0] == 0 and glyph.glyph_matrix[3] == 0   # 矩阵推断是否旋转/竖排

x0, y0 = apply_matrix_pt(glyph.glyph_matrix, bbox_lower_left)
x1, y1 = apply_matrix_pt(glyph.glyph_matrix, bbox_upper_right)
if x1 < x0: x0, x1 = x1, x0       # 归一化
if y1 < y0: y0, y1 = y1, y0
size = width if vertical or glyph.glyph_matrix[0] == 0 else height
positioned.append(PositionedGlyphEvent(…, bbox=(x0, y0, x1, y1), size=size, vertical=vertical))
```

> 中文说明：字形在「字形局部空间」是一个宽 `advance`、高 `fontsize` 的矩形，下沿由 `get_descent()` 决定（通常为负，表示低于基线）。把这个矩形的两个角点用 `glyph_matrix` 变换到设备空间，再归一化，就得到页面上的 `bbox`。竖排字体走另一分支，用 `char_disp` 给出竖排位移。`vertical` 字段还通过 `glyph_matrix[0]==0 and [3]==0` 做了一次「矩阵形态」推断，捕捉旋转排版的情形。

字体度量能力来自 `PdfFontLike` 协议（[font_types.py:6-21](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/font_types.py#L6-L21)），它定义了 glyphs 依赖的全部方法：

```python
class PdfFontLike(Protocol):
    def decode(self, data: bytes) -> object: ...           # 字节 → CID 序列
    def unicode_text(self, cid: int, fallback_text: str) -> str: ...   # CID → Unicode
    def is_multibyte(self) -> bool: ...                    # 是否多字节（CJK）
    def is_vertical(self) -> bool: ...                     # 是否竖排
    def char_width(self, cid: int) -> float: ...           # 字宽（1/1000 单位）
    def char_disp(self, cid: int) -> float | tuple: ...    # 竖排位移
    def get_descent(self) -> float: ...                    # 下沿
```

> 中文说明：glyphs 不关心字体是 Type1、TrueType 还是 CIDFont，它只依赖这 7 个方法。具体字体后端如何实现它们，是 u4-l4 的主题。

#### 4.3.4 代码实践

**实践目标**：手工模拟一次 `TJ` 文本运行的展开，理解「字距调整 + 字符」如何在游标上交替作用。

**操作步骤**：阅读 `expand_text_run_events`（[glyphs.py:58-136](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/glyphs.py#L58-L136)），然后**在纸上**走一遍下面这个 `TextRunEvent`（参数为示意值）：

```text
TextRunEvent(
    operator = "TJ",
    segments = (b"AB", -80.0, b"C"),   # 先显示 "AB"，再回退 80 个千分之一单位，再显示 "C"
    line_matrix = (0.0, 0.0),          # 游标起点
    font_size = 10.0,
    horizontal_scaling = 100.0,        # scaling = 1.0
    char_spacing = 0.0, word_spacing = 0.0,
    text_matrix = IDENTITY, ctm = IDENTITY,   # base = IDENTITY
)
```

假设字体把 `b"AB"` 解码成 CID [65, 66]、`b"C"` 解码成 CID [67]，且 `char_width(65)=600`、`char_width(66)=500`、`char_width(67)=700`（均为 1/1000 单位）。

**需要观察的现象 / 预期结果**：计算每个字形的 `glyph_offset` 与 `advance`。

- 字体度量换算：`scaling=1.0`，`dxscale = 0.001 × 10 × 1.0 = 0.01`。
- 字形 A(cid 65)：`offset=(0,0)`，`advance = 600 × 10 × 1.0 / 1000 = 6.0`；之后 `x = 6.0`。

  > 注：`char_width` 返回的是 1/1000 单位的值，公式 `char_width(cid) * font_size * scaling` 在代码里直接相乘；这里按 `600` 当作「千分之一 em 的数值」理解时，实际 advance 应为 `600/1000 × 10 × 1.0 = 6.0`。具体 `char_width` 的返回量纲以 u4-l4 的字体后端为准。
- 字形 B(cid 66)：`offset=(6.0, 0)`，`advance = 500/1000 × 10 = 5.0`；之后 `x = 11.0`。
- 遇到 `-80.0`：`x -= 80 × 0.01 = 0.8`，得 `x = 10.2`（注意：`need_charspace` 置真，但因 `charspace=0` 无额外影响）。
- 字形 C(cid 67)：`offset=(10.2, 0)`，`advance = 700/1000 × 10 = 7.0`。

所以三个字形的 `glyph_offset` 依次是 `(0,0)`、`(6.0,0)`、`(10.2,0)`——C 因为前面的 `-80` 字距调整，比「紧接 B 之后」的 `(11.0,0)` **往左移了 0.8**，这正是 `TJ` 微调字间距的效果。

> 这是**源码阅读 + 手算型实践**，目的是把「游标推进」的数学吃透。若要在机器上验证，可构造一个实现了 `PdfFontLike` 7 个方法的假字体，调用 `expand_text_run_events` 检查返回的 `GlyphEvent.glyph_offset`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `is_multibyte()` 为真时，`wordspace` 要被强制设为 0？

**参考答案**：词间距 `wordsace`（`Tw`）在 PDF 规范里只对**单字节字体的 ASCII 空格（cid 32）**生效，用来在西文里给空格额外加间距。多字节字体（CJK）的「字」本身不是以空格分词，且其 cid 32 通常不表示西文空格，所以规范规定多字节字体忽略 `Tw`。代码里 `if font.is_multibyte(): wordspace = 0` 正是落实这一点。

**练习 2**：`position_glyph_events` 里判断 `vertical` 用了 `glyph.glyph_matrix[0] == 0 and glyph.glyph_matrix[3] == 0`，这判断的是什么？

**参考答案**：它判断字形矩阵的 a、d 分量是否都为 0。一个正常的横排矩阵 a、d 通常非零（代表 x、y 方向的缩放）；当 a 和 d 都为 0 时，说明矩阵把「水平方向的输入」映射到了「垂直方向」，即文字被**旋转了 90°/270°**（常见于竖排或旋转标注）。这是一种用矩阵形态推断排版方向的后备手段，和字体自身的 `is_vertical()` 互为补充。

---

### 4.4 page_api 解释入口：把分词、解释、XObjet 装配成一条管线

#### 4.4.1 概念说明

前面三节分别讲了分词、解释、字形展开。但在真实解析里，一页的内容流往往不是「一条平铺的指令序列」那么简单——它可能通过 `Do` 操作符**调用 Form XObjet**（可复用的子内容流，常见于页眉页脚、水印、旋转文字），而这些 XObjet 又会嵌套。所以「解释一页」需要一个能把 XObjet **递归展开**的装配层。

`page_api.py` 就是这层装配的**门面（facade）**：它对外暴露一个简洁的 `interpret_page_with_resource_bundle(page, resource_bundle)`，内部把「分词 + 解释 + XObjet 递归」串起来，返回这页的完整事件流。

> 为什么要搞一个门面？因为 `page_api.py` 还顺手 re-export 了一组「页面访问」工具（`prepared_pdf_pages`、`read_page_content_bytes`、`tokenize_content_stream` 等），让上层（`native_page_interpreter.py`）只 import 一个模块就能拿到全部页面级 API，降低耦合。这种「一个模块聚合多个子模块导出」的手法在大型项目里很常见。

#### 4.4.2 核心流程

一页的解释流程（从 `native_page_interpreter.process_page` 视角）：

```text
1. build_page_resource_bundle：为这页构造资源包（字体、XObjet 映射），供解释与字形展开查字体。
2. interpret_page_with_resource_bundle(page, resource_bundle)：
   a. tokenize_content_stream(page.content_bytes) → list[PdfOperation]
   b. interpret_operations_with_xobjects(operations, xobject_map, resource_bundle)
      - 内部 new 一个 TextContentInterpreter + CollectingEventSink
      - 给解释器装上 xobject_handler（递归处理 Form XObjet）和 font_resolver
      - interpreter.run(operations) → 事件流
      - 遇到 Do 操作符 → handle_xobject 递归解释子内容流，把子事件夹在 Begin/EndXObjectEvent 之间
   c. 返回 (events, resource_bundle, sidecar)
3. emit_native_text_events_to_legacy_sink：把事件流交给 text_run_positioner（NativeTextRunPositioner），
   产出 AWLTChar，喂给 IL sink（ActiveILCreater）。
```

XObjet 递归的关键设计：子内容流用一个**全新的 `InterpreterState`**，但其 CTM 初始化为 `子 XObjet 的 matrix × 父 CTM`（[xobject_content_execution.py:102-106](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/xobject_content_execution.py#L102-L106)）；同时用 `active_xobject_ids` 防止**递归引用**（XObjet 引用自己）、用 `MAX_XOBJECT_NESTING_DEPTH=64` 防止**过深嵌套**（[xobject_content_execution.py:25](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/xobject_content_execution.py#L25)）。

#### 4.4.3 源码精读

`page_api.py` 本身极薄，几乎全是 re-export（[page_api.py:21-31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/page_api.py#L21-L31)）：

```python
prepared_pdf_pages = _prepared_pdf_pages
read_page_content_bytes = _read_page_content_bytes
read_page_content_streams = _read_page_content_streams
tokenize_content_stream = _tokenize_content_stream


def interpret_page_with_resource_bundle(page, resource_bundle):
    return interpret_prepared_page(page, resource_bundle)
```

> 中文说明：`page_api.py` 把一堆「页面访问」工具聚合导出，并把唯一的解释入口 `interpret_page_with_resource_bundle` 委托给 `interpret_prepared_page`。真正的活儿在 `page_content_execution.py` 和 `xobject_content_execution.py`。

`interpret_prepared_page`（[page_content_execution.py:13-23](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/page_content_execution.py#L13-L23)）只有三行实质代码：

```python
def interpret_prepared_page(page, resource_bundle):
    operations = tokenize_content_stream(page.content_bytes)        # ① 分词
    events, sidecar = interpret_operations_with_xobjects(           # ② 解释（含 XObjet 递归）
        operations,
        page.resource_tree.xobject_map,
        resource_bundle=resource_bundle,
    )
    return events, resource_bundle, sidecar
```

> 中文说明：① 把页面内容流字节分词成操作序列；② 交给带 XObjet 递归能力的解释器，产出事件流和一份 sidecar（记录页面底层操作，供 backend 还原用）。

真正干活的 `interpret_operations_with_xobjects`（[xobject_content_execution.py:37-137](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/xobject_content_execution.py#L37-L137)），它做的装配有三处亮点：

```python
sink = CollectingEventSink()
interpreter = TextContentInterpreter(sink=sink)
interpreter.xobject_path = xobject_path
interpreter.font_resolver = resource_bundle.get_font     # ← 让解释器的 _advance_line_matrix 能查字体

def handle_xobject(name, state):
    details = xobject_map.get(name)
    if details is None:
        return []
    if details.subtype_name == "Image":
        return [ImageXObjectEvent(...)]                  # 图像 XObjet：直接出事件
    if not details.is_form:
        return []
    # Form XObjet：递归解释其内容流
    child_state = InterpreterState()
    child_state.graphics_state.ctm = multiply_matrices(details.matrix, state.graphics_state.ctm)
    child_operations = tokenize_content_stream(details.data)
    child_events, child_sidecar = interpret_operations_with_xobjects(   # ← 递归
        child_operations, details.xobject_map,
        resource_bundle=resource_bundle,
        initial_state=child_state,
        xobject_path=child_path,
        active_xobject_ids=active_xobject_ids | {details_identity},     # ← 防递归
        max_xobject_depth=max_xobject_depth,
    )
    return [BeginXObjectEvent(...), *child_events, EndXObjectEvent(...)]

interpreter.xobject_handler = handle_xobject
return interpreter.run(operations), sidecar
```

> 中文说明：这段把 4.1～4.3 串了起来。它给解释器装上 `font_resolver`（这样 `_advance_line_matrix` 能查字体推进游标）和 `xobject_handler`（这样遇到 `Do` 调用一个 Form XObjet 时，能递归地解释子内容流）。子流的 CTM 是「XObjet 自带 matrix × 父 CTM」，所以 XObjet 里的文字能正确叠加到父页面的坐标系上。最终 `interpreter.run(operations)` 把整页（含所有 XObjet 内的文字）的事件流一次性产出。

最后看上层如何消费——`native_page_interpreter._NativePageInterpreter.process_page`（[native_page_interpreter.py:38-59](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/native_page_interpreter.py#L38-L59)）：

```python
def process_page(self, page):
    resource_bundle = resource_runtime.build_page_resource_bundle(page.resource_tree)
    events, resource_bundle, base_operations = interpret_page_with_resource_bundle(
        page, resource_bundle,
    )
    emit_native_text_events_to_legacy_sink(
        events, resource_bundle, sink,
        xobject_end_operations=base_operations.xobject_end_operations,
        text_run_positioner=text_run_positioner,        # ← NativeTextRunPositioner
    )
    ...
```

> 中文说明：`process_page` 就是 u4-l1 说的「造资源包 → 解释页面 → 喂给 sink」三步。它拿到的 `events` 里就有全部 `TextRunEvent`，再交给 `NativeTextRunPositioner`（4.3 节提到的生产路径字形展开器，产 `AWLTChar`）变成 IL 字符。这条链路与本讲的 glyphs 数学完全等价。

#### 4.4.4 代码实践

**实践目标**：跟踪一条完整调用链，确认「PDF 字节 → PdfOperation → 事件 → 字形」四步各自落在哪个源码文件的哪一行。

**操作步骤**：在仓库根目录执行以下只读检索，把每一步的入口函数与行号填进表格：

```bash
# 示例命令：定位四个阶段的入口
grep -n "def tokenize_operations"      babeldoc/format/pdf/new_parser/tokenizer.py
grep -n "def interpret_operations\b"   babeldoc/format/pdf/new_parser/interpreter.py
grep -n "def expand_text_run_events"   babeldoc/format/pdf/new_parser/glyphs.py
grep -n "def interpret_prepared_page"  babeldoc/format/pdf/new_parser/page_content_execution.py
grep -n "def position_text_run"        babeldoc/format/pdf/new_parser/text_positioning.py
```

**需要观察的现象**：你会得到五个函数的定义行号，分别对应「分词、解释、字形展开（规范实现）、页面解释入口、生产路径字形展开」。

**预期结果**：把它们整理成一张链路表：

| 阶段 | 函数 | 文件 |
|------|------|------|
| ① 分词 | `tokenize_operations` | tokenizer.py |
| ② 解释（操作符→事件） | `interpret_operations` → `TextContentInterpreter.run` | interpreter.py |
| ③ 页面解释入口（含 XObjet 递归） | `interpret_prepared_page` → `interpret_operations_with_xobjects` | page_content_execution.py / xobject_content_execution.py |
| ④ 字形展开（规范实现） | `expand_text_run_events` → `position_glyph_events` | glyphs.py |
| ④' 字形展开（生产路径） | `NativeTextRunPositioner.position_text_run` | text_positioning.py |

> 这是**源码阅读型实践**，目的是把本讲四个模块在代码里的位置钉死，方便日后回查。无需运行翻译。

#### 4.4.5 小练习与答案

**练习 1**：为什么解释 Form XObjet 时要用一个**全新的 `InterpreterState`**，而不是复用父页面的状态？

**参考答案**：因为 Form XObjet 是一个**自包含的绘图单元**，它的内容流假设自己「从原点、单位矩阵开始画」，再由 XObjet 自带的 `Matrix` 把它摆到正确位置。如果复用父状态，父页面累积的 CTM、文本状态会污染子流的坐标系，导致位置全错。所以子流用新状态、CTM 初始化为 `子 Matrix × 父 CTM`，既隔离了内部状态，又正确叠加到了父坐标系。`q`/`Q` 在子流退出后还能恢复父状态。

**练习 2**：`active_xobject_ids` 和 `MAX_XOBJECT_NESTING_DEPTH=64` 分别防什么？

**参考答案**：`active_xobject_ids` 记录当前递归路径上**正在被解释的 XObjet 身份（id）**，遇到已在路径上的就跳过，防止 **XObjet 直接或间接引用自己**造成无限递归（PDF 允许这种结构存在，但渲染时必须打断）。`MAX_XOBJECT_NESTING_DEPTH=64` 是一道**深度上限**保险，防止恶意或畸形 PDF 构造极深的 XObjet 嵌套链把栈打爆。两者都是健壮性保护（呼应 u8-l5 的异常与健壮性主题）。

---

## 5. 综合实践

**任务**：完整跟踪一次 `Tj`/`TJ` 文本操作符「从字节到带坐标字形」的全过程，并产出一份带行号的分析文档。

具体要求：

1. **准备输入**：写一段最小内容流（参考 4.1.4 的 `stream` 变量），其中至少包含一个 `Tf`（设字体）、一个 `Tm`（设文本矩阵）、一个 `Tj` 和一个带字距调整的 `TJ`。
2. **跑分词**：用 `tokenize_operations` 把它切成 `PdfOperation`，记录每条 operation 的 `operator` 与 `operands`。
3. **跑解释**：用 `interpret_operations` 得到事件流，挑出其中的 `SetFontEvent`、`TextMatrixEvent`、`TextRunEvent`，逐个抄下字段值（注意 `TextRunEvent.segments` 里 `bytes` 与 `float` 的混合）。
4. **手算字形展开**：参考 4.3.4 的方法，为 `TJ` 产生的 `TextRunEvent` 手算每个字形的 `glyph_offset` 与 `advance`，画出三个字形的相对位置示意图，标出字距调整带来的回退量。
5. **对照生产路径**：阅读 `NativeTextRunPositioner.position_text_run`（[text_positioning.py:24-101](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/text_positioning.py#L24-L101)），指出它和 `expand_text_run_events`（[glyphs.py:58-136](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/glyphs.py#L58-L136)）在数学上的等价之处（比例因子、游标推进、`base_matrix`），以及它们产物类型的差异（`AWLTChar` vs `GlyphEvent`）。

**预期产出**：一份 Markdown 小报告，包含五个阶段的输入输出示例、一张字形位置草图、一段对「glyphs 规范实现 vs NativeTextRunPositioner 生产实现」的对比说明。

> 这个任务把本讲四个最小模块（操作符、事件、字形展开、解释入口）全部串起来。如果你在本地真实运行了步骤 2～3，把输出贴进报告；若无法运行，明确标注「待本地验证」并继续完成手算与源码对照部分。

---

## 6. 本讲小结

- **内容流是 PDF 的「绘画指令序列」**：每条指令是「若干操作数 + 一个操作符」，按栈式 + 有状态的方式执行。`tokenizer.py` 把字节切成 `PdfOperation`，`operator` 在前、`operands` 在后（操作数先出现）。
- **解释器是「事件翻译机 + 状态机」**：`TextContentInterpreter` 维护 `InterpreterState`（CTM/文本状态/文本对象 + 状态栈），把每个操作符翻译成一个不可变事件丢进 sink。`Tj`/`TJ` 产 `TextRunEvent`，`Tf` 产 `SetFontEvent`，`cm` 产 `ConcatMatrixEvent`，`Tm` 产 `TextMatrixEvent`。
- **`TextRunEvent` 是自描述快照**：它携带显示文本的全部上下文（字体、字号、矩阵、间距），让下游能独立计算每个字形坐标，无需回头问解释器。
- **字形展开 = 沿书写方向走游标**：`expand_text_run_events` 用字体把字节解码成 CID，逐字形累加字宽得到 `glyph_offset`，产 `GlyphEvent`；`position_glyph_events` 再用字体度量算包围盒，产 `PositionedGlyphEvent`。换算核心是 `scaling = Tz/100` 与 `0.001`（1/1000 单位）系数。
- **生产路径用 `NativeTextRunPositioner`**：它内联了同样的几何数学，直接产 `AWLTChar` 喂给 IL sink；`glyphs.py` 则是该算法的规范/参考实现，数学完全一致。
- **`page_api.py` 是解释入口门面**：它把分词、解释、XObjet 递归装配起来；Form XObjet 用全新状态 + `子 Matrix × 父 CTM` 递归解释，并用 `active_xobject_ids` 和深度上限防递归与爆栈。

---

## 7. 下一步学习建议

本讲把「内容流 → 事件 → 字形」讲透了，但字形展开依赖的**字体度量**（`decode`/`char_width`/`is_vertical`/`get_descent`……）还只是一个 `PdfFontLike` 接口——它的具体实现是黑盒。这正是下一讲的主题：

- **u4-l4 active 运行时与字体后端**：精读 `active_parse_runtime`、`active_font_runtime`、`active_direct_font_backend` 等文件，看 BabelDOC 如何从 PDF 字体字典（Type1 / TrueType / CIDFont）构建出实现了 `PdfFontLike` 的运行时字体、如何缓存字体度量、以及它如何兼容 pymupdf 与 pdfminer 两种后端。

补充阅读建议：

- 若想了解字形事件之后如何变成 IL 实体，回看 **u4-l1**（`ActiveILCreater` 的 `project_native_*` 投影）与 **u3-l1**（`PdfCharacter` 的 `box`/`char_unicode`/`pdf_style` 字段）。
- 若对内容流分词细节（如 `TJ` 数组的切分、内联图像 `BI/ID/EI` 的处理）感兴趣，可精读 [tokenizer.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/tokenizer.py) 的 `iter_operation_stream` 与 `_read_inline_image_stream`。
- 若想看「解释器状态机」的完整字段定义（`q`/`Q` 的栈、文本对象的 `move_text_position`/`next_line`），精读 [state.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/new_parser/state.py)。
