# pyparsing 基础与 VhdlConstruct 框架

## 1. 本讲目标

本讲是「VHDL 源码解析」单元的第一讲。我们将暂时离开 `TbGenerator` 主流程，钻进解析层最底部的 [VhdlParse.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py)，搞清楚一个根本问题：

> 工具是凭什么把一段纯文本的 VHDL 代码，变成程序里可以查询的「实体名、端口、generic、注释」的？

学完本讲你应当能够：

1. 说出 pyparsing 的 `Word` / `Literal` / `Combine` / `Forward` / `Group` / `scanString` / `setResultsName` 等基本构件分别做什么。
2. 看懂 `VhdlParse.py` 顶部那一堆 `PP_*` 常量是如何「从单个字符一路搭到任意表达式」的。
3. 理解 `VhdlConstruct` 基类用「`PP_DEFINITION`（文法）+ `_Parse`（取字段）」两段式统一描述所有 VHDL 构件的设计模式。
4. 理解 `PrToStr` 为什么是这套模式不可或缺的「胶水」，它如何把解析结果重新拼回字符串。

本讲只讲**框架与地基**；具体的 `entity` / `generic` / `port` 文法和 `VhdlFile` 的读取主流程留给下一讲 u3-l2。

## 2. 前置知识

### 2.1 什么是「解析（parsing）」

一段 VHDL 源码在磁盘上只是普通文本。要让程序理解它，需要做两件事：

- **词法/语法分析**：按 VHDL 的语法规则，把字符流切成有结构的片段（比如「这是一个 entity，它有两个 generic、三个 port」）。
- **建立模型**：把这些片段存成 Python 对象（如 `self.name`、`self.ports`），供后续 `TbGenerator` 生成 testbench 时查询。

`VhdlParse.py` 同时完成这两件事：用 pyparsing 做分析，用一系列 `Vhdl*` 类做建模。

### 2.2 pyparsing 速览

[pyparsing](https://github.com/pyparsing/pyparsing) 是一个纯 Python 的「解析器组合子（parser combinator）」库。它的核心思想是：**用小积木拼出大文法**。每个积木就是一个 Python 对象，能匹配某种文本；把积木用 `+`、`|` 组合起来，就得到更复杂的积木。

本讲会反复用到下面这些积木，先混个眼熟（本讲源码精读里会逐个对照真实代码再讲一遍）：

| 积木 | 作用 |
|------|------|
| `pp.Literal("use")` | 精确匹配字符串 `use` |
| `pp.CaselessKeyword("to")` | 大小写不敏感地匹配关键字 `to`（且要求词边界） |
| `pp.Word(pp.alphanums+"_")` | 匹配由「字母/数字/下划线」组成的词 |
| `pp.Regex(r"\s")` | 用正则匹配 |
| `pp.Optional(x)` | `x` 出现 0 次或 1 次 |
| `pp.OneOrMore(x)` | `x` 出现 1 次或多次 |
| `a + b` | 先匹配 `a`，再匹配 `b`（顺序连接） |
| `a \| b` | 匹配 `a` 或 `b`（按 `MatchFirst` 取第一个成功的） |
| `~x` | 「否定前瞻」：要求当前位置**不**匹配 `x`，本身不消费字符 |
| `pp.Combine(expr)` | 把 `expr` 匹配到的多段文本合并成一个字符串 |
| `pp.Group(expr)` | 把 `expr` 的匹配结果打包成一个子结果（嵌套结构） |
| `pp.Forward()` | 先声明一个「占位」文法，之后再填充，用于**递归文法** |
| `expr("name")` 或 `expr.setResultsName("name")` | 给匹配结果起个名字，事后用 `parts.get("name")` 取出 |
| `expr.parseString(s)` | 从字符串开头尝试完整解析 |
| `expr.scanString(s)` | 在整段文本里**扫描**所有匹配处，逐个给出 `(tokens, start, end)` |

> 小提示：如果你用 `pip show pyparsing` 看到版本较新，会注意到 `pp.lineStart`（小写）被标记为过时、推荐改用 `pp.LineStart()`（大写）。本仓库两种写法都出现了，下面讲到具体行时会指出，行为上是一致的。

## 3. 本讲源码地图

本讲只涉及一个文件，但它是整个解析层的心脏：

| 文件 | 角色 | 本讲关注 |
|------|------|----------|
| [VhdlParse.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py) | 基于 pyparsing 的 VHDL 解析模块 | 顶部 `PP_*` 构件、`PrToStr`、`VhdlConstruct` 基类 |

整个文件大致分成四层（从下往上读更舒服）：

1. **`PP_*` 基础构件**（第 10–31 行）：搭好「字符 → 表达式」的积木箱。
2. **`PrToStr`**（第 34–41 行）：把解析结果还原成字符串的工具函数。
3. **`VhdlConstruct` 基类**（第 43–65 行）：定义「文法 + 取字段」的统一契约。
4. **`Vhdl*` 子类与 `VhdlFile`**（第 67 行起）：每个子类描述一种 VHDL 构件，`VhdlFile` 把它们串起来读整个文件。第 4 层是 u3-l2 的主战场，本讲只用其中最简单的两个子类（`VhdlCommentLine`、`VhdlUseStatement`）当例子。

## 4. 核心概念与源码讲解

### 4.1 PP_* 基础构件：从单个字符到任意表达式

#### 4.1.1 概念说明

VHDL 文本里既有结构清晰的小词（标识符、整数、关键字），也有形状自由的「表达式」（比如 generic 的默认值 `:= 16#FFFF#`、数组范围 `(0 to 7)`、嵌套括号 `(others => '0')`）。`VhdlParse.py` 顶部把这两类需求分成两块来准备：

- **「词法」层积木**：`PP_IDENTIFIER`、`PP_INTEGER`、`PP_DIRECTION` 等，匹配规则固定的小词。
- **「表达式」层积木**：`PP_UNQUOTED_EXPR`、`PP_BRACED_EXPR`、`PP_EXPRESSION`，匹配任意一坨文本（遇到关键字、分号、括号、注释就停），并能递归处理嵌套括号。

#### 4.1.2 核心流程

表达式层的构造是一个**自底向上的递归定义**，可以画成这样：

```
单个字符 PP_ANYCHAR
        │  （反复取字符，但遇到关键字/分号/括号/注释 就停）
        ▼
PP_UNQUOTED_EXPR  ──┐
                    │  （二者交替，可嵌套）
PP_BRACED_EXPR  ────┘   ← “( … )”，里面的 … 又可以是 PP_UNQUOTED_EXPR / PP_BRACED_EXPR
        │
        ▼
   PP_EXPRESSION  （Group + Combine，把一坨文本打包成单个结果）
```

关键技巧有两个：

1. **否定前瞻 `~` 用来「踩刹车」**：每吃一个字符前，先确认下一个位置不是关键字、分号、括号或注释开头。这样表达式不会越界吞掉后续结构。
2. **`Forward()` 用来「表达递归」**：括号里可以再有括号，文法需要引用自身，于是先占位、后填充。

#### 4.1.3 源码精读

先看「表达式」层。第一段先准备了一批底层小件：

[VhdlParse.py:10-13](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L10-L13) —— 定义关键字集合、空白与「任意单字符」。

> 这段代码做了：把 `to / downto / entity / port / generic / end / is` 这些 VHDL 关键字做成「先到先得」的匹配器 `PP_KEYWORDS`；`PP_SPACE` 匹配一个空白字符；`PP_ANYCHAR` 则是「一个空白，或一个非空白字符（并把非空白字符记到结果名 `ac` 下）」。

```python
kw = ["to", "downto", "entity", "port", "generic", "end", "is"]
PP_KEYWORDS = pp.MatchFirst(kw)
PP_SPACE = pp.Regex("\s")
PP_ANYCHAR = PP_SPACE | (pp.Regex("[^\s]").setResultsName("ac", listAllMatches=True))
```

接着定义了四个「边界哨兵」与最核心的 `PP_UNQUOTED_EXPR`：

[VhdlParse.py:15-18](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L15-L18) —— 定义括号、分号、注释开头三种「边界」，再用否定前瞻组合出「未加引号的表达式」。

> 这段代码做了：`PP_BRACES`/`PP_ENDOFLINE`/`PP_COMMENTSTART` 分别标记 `(`/`)`、`;`、`--` 三类边界；`PP_UNQUOTED_EXPR` 反复吃 `PP_ANYCHAR`，但每吃一个字符前都用 `~` 确认**不**踩到关键字、分号、括号、注释开头——这就是上一节说的「踩刹车」。最外层用 `Combine(...)` 把多个字符合并成一个字符串，结果命名为 `ue`。

```python
PP_BRACES = pp.Literal("(") | pp.Literal(")")
PP_ENDOFLINE = pp.Literal(";")("eol")
PP_COMMENTSTART = pp.Literal("--")
PP_UNQUOTED_EXPR = pp.Combine(
    pp.OneOrMore(~PP_KEYWORDS + ~PP_ENDOFLINE + ~PP_BRACES + ~PP_COMMENTSTART + PP_ANYCHAR)
).setResultsName("ue", listAllMatches=True)
```

再看递归的括号表达式：

[VhdlParse.py:19-22](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L19-L22) —— 用 `Forward()` 实现「括号里可以再套括号」的递归文法。

> 这段代码做了：先用 `pp.Forward()` 给 `PP_BRACED_EXPR` 占位；然后定义 `PP_BRACE_PAIR` 为「`(` + 若干个(嵌套括号 \| 普通表达式 \| 关键字) + `)`」；最后用 `<<` 把占位符填充为 `PP_BRACE_PAIR`，于是括号表达式就能自我引用、支持任意深度嵌套。`PP_EXPRESSION` 则把「普通表达式或括号表达式」反复连接，用 `Group + Combine` 打包成单个结果。

```python
PP_BRACED_EXPR = pp.Forward().setResultsName("be", listAllMatches=True)
PP_BRACE_PAIR = pp.Literal("(") + pp.OneOrMore(PP_BRACED_EXPR|PP_UNQUOTED_EXPR|PP_KEYWORDS) + pp.Literal(")")
PP_BRACED_EXPR << PP_BRACE_PAIR
PP_EXPRESSION = pp.Group(pp.Combine(pp.OneOrMore(PP_UNQUOTED_EXPR|PP_BRACED_EXPR)))
```

> 用数学化的方式描述：设 \(U\) 为普通表达式、\(B\) 为括号表达式，则
> \[ B = \texttt{(}\; (B \mid U \mid K)^{*}\; \texttt{)},\qquad E = (U \mid B)^{+} \]
> 其中 \(K\) 是关键字集合。这是一个典型的**递归文法**，`Forward()` 就是用来表达这种自引用的。

接着看「词法」层，这一块简单许多：

[VhdlParse.py:24-31](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L24-L31) —— 定义标识符、整数、注释、值、范围方向、端口方向等小词法积木。

> 这段代码做了：重新声明一遍 `kw` 和 `PP_KEYWORDS`（与第 10–11 行内容完全相同，最终生效的是这里这一份）；`PP_IDENTIFIER` 匹配「字母/数字/下划线」组成的词（VHDL 标识符）；`PP_INTEGER` 匹配纯数字；`PP_COMMENT` 匹配 `--` 加本行剩余内容，并把正文记到 `text`；`PP_VALUE` 用正则宽松地匹配一串合法字符；`PP_RANGEDIR` 匹配范围方向 `to`/`downto`；`PP_DIRECTION` 匹配端口方向 `in`/`out`/`inout`/`buffer`。注意这里用 `CaselessKeyword`，大小写不敏感且自带词边界。

```python
kw = ["to", "downto", "entity", "port", "generic", "end", "is"]
PP_KEYWORDS = pp.MatchFirst(kw)
PP_IDENTIFIER = pp.Word(pp.alphanums+"_")
PP_INTEGER = pp.Word(pp.nums)
PP_COMMENT = pp.Group(pp.Literal("--") + pp.restOfLine("text"))
PP_VALUE = pp.Regex(r"[a-zA-Z0-9\"'_#]*")
PP_RANGEDIR = (pp.CaselessKeyword("to")|pp.CaselessKeyword("downto"))
PP_DIRECTION = (pp.CaselessKeyword("in")|pp.CaselessKeyword("out")|pp.CaselessKeyword("inout")|pp.CaselessKeyword("buffer"))
```

> 小观察：第 10–11 行和第 24–25 行把 `kw` 与 `PP_KEYWORDS` 写了两遍且内容一致，第二遍覆盖第一遍。这是源码里的小冗余，理解时以第 25 行为准即可。

至此「积木箱」就备齐了。后续所有 `Vhdl*` 子类的文法，都是用这些 `PP_*` 积木拼出来的。

#### 4.1.4 代码实践

**实践目标**：亲手感受「否定前瞻」如何给表达式踩刹车。

**操作步骤**（新建一个临时脚本 `play_expr.py`，**不要**放进仓库，跑完即删）：

```python
# 示例代码：体会 PP_UNQUOTED_EXPR 的边界行为
import pyparsing as pp

kw = ["to", "downto", "entity", "port", "generic", "end", "is"]
PP_KEYWORDS = pp.MatchFirst(kw)
PP_SPACE = pp.Regex("\s")
PP_ANYCHAR = PP_SPACE | (pp.Regex("[^\s]").setResultsName("ac", listAllMatches=True))
PP_BRACES = pp.Literal("(") | pp.Literal(")")
PP_ENDOFLINE = pp.Literal(";")("eol")
PP_COMMENTSTART = pp.Literal("--")
PP_UNQUOTED_EXPR = pp.Combine(
    pp.OneOrMore(~PP_KEYWORDS + ~PP_ENDOFLINE + ~PP_BRACES + ~PP_COMMENTSTART + PP_ANYCHAR)
).setResultsName("ue", listAllMatches=True)

# 1) 普通文本：应整段吃下
print(PP_UNQUOTED_EXPR.parseString("std_logic_vector"))   # 预期: std_logic_vector

# 2) 遇到分号停下：只取分号前的部分
print(PP_UNQUOTED_EXPR.parseString("Clk_i : in"))          # 预期: Clk_i  （注意后面的 ":" 也会被吃，因为不在哨兵里）
```

**需要观察的现象**：第 2 条里 `PP_UNQUOTED_EXPR` 会在哪里停下？它并没有把 `:` 列为哨兵，所以会一直吃到行尾或下一个哨兵。这说明「哨兵集合」决定了表达式的边界精度——这也是真实文法里 `VhdlPortDeclaration` 要显式写 `":"` 和 `PP_DIRECTION` 来精确定位的原因。

**预期结果**：第 1 条返回 `['std_logic_vector']`；第 2 条会吃掉 `Clk_i : in` 中的大部分内容（因为没有对应的哨兵），印证「哨兵决定边界」。

**若无法确定运行结果**：可标注「待本地验证」，重点理解 `~X` 的含义即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `PP_BRACES` 从 `PP_UNQUOTED_EXPR` 的否定前瞻里删掉，解析 `a := (1+2)` 时会发生什么？

**参考答案**：`PP_UNQUOTED_EXPR` 不再在 `(` 前停下，会连括号一起吞下去，导致 `PP_BRACED_EXPR` 拿不到独立的括号片段、嵌套结构被破坏。这正是为什么括号必须作为哨兵。

**练习 2**：`PP_KEYWORDS` 用的是 `pp.MatchFirst([...])` 而不是 `pp.Each([...])`。两者在「多个关键字都能匹配」时的行为有何区别？

**参考答案**：`MatchFirst` 按列表顺序逐个尝试、返回第一个成功者；`Each` 要求所有分支都匹配。解析关键字集合需要的是「或」语义，所以用 `MatchFirst`（或等价的 `|` 链）。

---

### 4.2 PrToStr：把解析结果还原成字符串

#### 4.2.1 概念说明

pyparsing 解析一段文本后，给出的是一个 `ParseResults` 对象——它可能是字符串、也可能是嵌套的 `ParseResults`（因为文法里有 `Group`）。但很多时候我们又需要把它**重新变回一段纯文本**，原因有二：

1. **嵌套构造**：父文法（如 `entity`）解析出来的子片段（如某个 `port`）是个 `ParseResults`，而子构件的构造函数更希望拿到一段干净的文本去自己重新解析。
2. **回填输出**：有时要把解析到的内容再写回 VHDL 文本（如 `__str__`）。

`PrToStr` 就是这个「`ParseResults` → 字符串」的转换器。

#### 4.2.2 核心流程

`PrToStr` 是一个**递归**函数：

```
PrToStr(ParseResults):
  对结果里的每个元素 r：
    如果 r 是字符串      → 去空白后收集
    否则（r 还是 ParseResults）→ 递归调用 PrToStr(r)
  用空格把所有片段 join 起来
```

#### 4.2.3 源码精读

[VhdlParse.py:34-41](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L34-L41) —— 递归地把 `ParseResults` 拼回字符串。

> 这段代码做了：遍历 `ParseResults` 里的每个元素；字符串元素就 `strip()` 后收进列表；非字符串元素（即嵌套的 `ParseResults`）就递归处理；最后用单个空格把所有片段连起来。注意它会在每个片段之间插入空格，因此还原出的文本可能与原文在空格上有细微差异（这也是 `VhdlConstruct.__str__` 后来要做 `( ` → `(` 这类清理的原因）。

```python
def PrToStr(pr : pp.ParseResults):
    strings = []
    for r in pr:
        if type(r) is str:
            strings.append(r.strip())
        else:
            strings.append(PrToStr(r))
    return " ".join(strings)
```

#### 4.2.4 代码实践

**实践目标**：直观看到 `PrToStr` 的「拼接 + 插空格」行为，理解为什么需要后续清理。

**操作步骤**（接上一节的临时脚本）：

```python
# 示例代码：观察 PrToStr 的拼接效果
from VhdlParse import PrToStr, PP_EXPRESSION   # 需在仓库根目录运行，便于 import

res = PP_EXPRESSION.parseString("(0 to 7)")
print(repr(PrToStr(res)))   # 预期: "( 0 to 7 )" 之类，片段间多了空格
```

**需要观察的现象**：还原后的字符串里，括号和数字之间是否多了空格？

**预期结果**：大概率为 `'( 0 to 7 )'`（片段之间被插入空格）。这正好解释了 `VhdlConstruct.__str__` 里 `.replace("( ", "(").replace(" )", ")")` 的存在意义。

**若无法确定运行结果**：「待本地验证」（不同 pyparsing 版本在 `Combine`/`Group` 上的默认行为略有差异）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PrToStr` 必须是递归的？

**参考答案**：因为 `ParseResults` 是树状结构——`Group` 会把一段结果嵌套成子 `ParseResults`。只有递归才能把任意深度的嵌套展平成一维字符串。

**练习 2**：`PrToStr` 用 `" ".join(...)` 拼接，会引入多余的空格。这套设计为什么能容忍这点？

**参考答案**：因为下游（`VhdlConstruct.__str__`）会做括号附近的空格清理；而且工具最终输出的是**重新生成**的 testbench，并非逐字回放原文，少量空格差异不影响生成结果。

---

### 4.3 VhdlConstruct 基类：PP_DEFINITION + _Parse 的统一模式

#### 4.3.1 概念说明

`Vhdl*` 子类要描述的构件五花八门——`use` 语句、注释行、范围、类型、generic、port、entity……但它们都遵循同一个套路：

> **每个构件 = 一段文法（怎么认）+ 一段取字段逻辑（认出来之后把哪些片段存成属性）。**

`VhdlConstruct` 把这个共性抽成基类，子类只需要填两个东西：

- 类属性 `PP_DEFINITION`：本构件的 pyparsing 文法。
- 方法 `_Parse(self, parts)`：从解析结果 `parts` 里取出字段，挂到 `self` 上。

这就把「文法」和「建模」解耦，子类写得极其简短（下一讲的 `VhdlGenericDeclaration`、`VhdlPortDeclaration` 都是十几行搞定）。

#### 4.3.2 核心流程

一个 `VhdlConstruct` 子类对象的生命周期如下：

```
              ┌─────────────────────────────┐
              │  类属性 PP_DEFINITION (文法) │
              └──────────────┬──────────────┘
                             │
   构造: VhdlXxx(code)       ▼
   ┌──────────────────────────────────────────────────┐
   │ __init__:                                        │
   │   1. code 若不是 str，用 PrToStr 转成 str         │
   │   2. strip() 后存为 self.code                     │
   │   3. PP_DEFINITION.parseString(code) → parts     │
   │   4. _Parse(parts)  ← 子类实现，把 parts 拆成属性 │
   └──────────────────────────────────────────────────┘
                             │
              类方法 PP():   │   __str__():
              return Group(PP_DEFINITION)   return self.code 清理后
                  ▲                                        ▲
                  │                                        │
        供父文法嵌套引用                          供输出/回填文本
```

两个对外「门面」：

- `PP()` 类方法：把本构件文法包成 `Group`，方便**被父文法嵌套引用**（比如 `entity` 文法里引用 `VhdlGenericDeclaration.PP()`）。
- `__str__`：把 `self.code` 略做清理后返回，让对象能**变回字符串**。

#### 4.3.3 源码精读

[VhdlParse.py:43-65](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L43-L65) —— `VhdlConstruct` 基类全貌。

> 这段代码做了：声明类属性 `PP_DEFINITION = None`（留待子类覆盖）。`__init__` 接受 `code`（字符串或 `ParseResults`）：若不是字符串就调 `PrToStr` 归一化，`strip()` 后存为 `self.code`，再用 `self.PP_DEFINITION.parseString(code)` 解析，把结果交给 `_Parse`。`_Parse` 是抽象方法，子类必须实现，否则抛 `NotImplementedError`。`__str__` 返回 `self.code` 并清理括号附近的空格。类方法 `PP()` 返回 `pp.Group(cls.PP_DEFINITION)`，用于把本构件嵌入父文法时保持结果成组。

```python
class VhdlConstruct:

    PP_DEFINITION = None

    def __init__(self, code):
        if type(code) is not str:
            code = PrToStr(code)
        code = code.strip()
        self.code = code
        try:
            self._Parse(self.PP_DEFINITION.parseString(code))
        except:
            raise

    def _Parse(self, parts : pp.ParseResults):
        raise NotImplementedError()

    def __str__(self):
        return self.code.replace("( ", "(").replace(" )", ")").strip()

    @classmethod
    def PP(cls):
        return pp.Group(cls.PP_DEFINITION)
```

几处值得细看的点：

- **第 48–49 行的 `PrToStr` 分支**：正是 4.2 节那把「胶水」的用武之地——父文法把子片段以 `ParseResults` 形式喂进来时，这里把它转回字符串。
- **第 52–55 行的 `try ... except: raise`**：目前是「捕获后原样重新抛出」，等同于不做处理，属保留的脚手架代码；理解时可以把它当作直接调用 `_Parse(...)`。
- **第 63–65 行的 `PP()`**：`Group` 保证本构件在父文法里匹配到的若干 token **不会散落**，而是作为一个命名子结果整体出现，便于父构件用 `parts.get("xxx")` 取走。

为了看清这套模式有多省事，看两个最简单的子类：

[VhdlParse.py:67-72](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L67-L72) —— `VhdlCommentLine`：文法是「行首 + 注释」，`_Parse` 只取 `text`。

> 这段代码做了：`PP_DEFINITION` = 行首跟着 `PP_COMMENT`（即 `--` 加本行剩余）；`_Parse` 把注释正文存到 `self.comment`。

```python
class VhdlCommentLine(VhdlConstruct):
    PP_DEFINITION = pp.lineStart + PP_COMMENT
    def _Parse(self, parts : pp.ParseResults):
        self.comment = parts[0].get("text")
```

[VhdlParse.py:74-80](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L74-L80) —— `VhdlUseStatement`：文法精确描述 `use 库.元素.对象`，`_Parse` 取三段名字。

> 这段代码做了：`PP_DEFINITION` = 行首（不留空白）+ `use` + 标识符(命名 `library`) + `.` + 标识符(`element`) + `.` + 标识符(`object`)；`_Parse` 把三段分别存为 `self.library`/`self.element`/`self.object`。这是「文法定义 + 字段提取」协作的范本，也是本讲综合实践的参照对象。

```python
class VhdlUseStatement(VhdlConstruct):
    PP_DEFINITION = pp.LineStart().leaveWhitespace() + pp.Literal("use") + PP_IDENTIFIER("library") + pp.Literal(".") + PP_IDENTIFIER("element") + pp.Literal(".") + PP_IDENTIFIER("object")
    def _Parse(self, parts : pp.ParseResults):
        self.library = parts.get("library")
        self.element = parts.get("element")
        self.object = parts.get("object")
```

> 注意对比：`VhdlCommentLine` 用小写 `pp.lineStart`，`VhdlUseStatement` 用 `pp.LineStart().leaveWhitespace()`。后者调用 `leaveWhitespace()` 表示「不要跳过行首空白」，要求 `use` 必须顶格；前者沿用默认的空白跳过行为。这两种写法在新版 pyparsing 里前者已被建议改写为大写形式。

#### 4.3.4 代码实践

**实践目标**：亲手实现一个识别 `use lib.elem.obj;` 的迷你解析器，体会「`PP_DEFINITION` 定义文法、`_Parse` 提取字段」的协作，并把它与真实 `VhdlUseStatement` 对照。

**操作步骤**：

1. 在仓库根目录启动 Python（便于 `from VhdlParse import ...`）：

```bash
cd <仓库根目录>
python
```

2. 在交互环境里运行下面这段「示例代码」（先不继承 `VhdlConstruct`，纯用 pyparsing，体会 `setResultsName` 与 `parts.get` 的配合）：

```python
# 示例代码：迷你 use 语句解析器（不依赖 VhdlConstruct）
import pyparsing as pp

PP_IDENTIFIER = pp.Word(pp.alphanums + "_")

# 1) 定义文法：use 库.元素.对象  （每段都起个名字）
use_grammar = (pp.Literal("use")
               + PP_IDENTIFIER.setResultsName("library")
               + pp.Literal(".")
               + PP_IDENTIFIER.setResultsName("element")
               + pp.Literal(".")
               + PP_IDENTIFIER.setResultsName("object"))

# 2) 解析：parseString 返回 ParseResults
parts = use_grammar.parseString("use ieee.numeric_std.all")

# 3) 取字段：靠 setResultsName 起的名字
print(parts.get("library"))   # 预期: ieee
print(parts.get("element"))   # 预期: numeric_std
print(parts.get("object"))    # 预期: all
```

3. 接着，把上面的迷你版「升级」成 `VhdlConstruct` 风格，与真实的 `VhdlUseStatement` 对比：

```python
# 示例代码：用 VhdlConstruct 模式重写
from VhdlParse import VhdlConstruct, PP_IDENTIFIER

class MyUse(VhdlConstruct):
    PP_DEFINITION = (pp.Literal("use")
                     + PP_IDENTIFIER("library")
                     + pp.Literal(".")
                     + PP_IDENTIFIER("element")
                     + pp.Literal(".")
                     + PP_IDENTIFIER("object"))
    def _Parse(self, parts):
        self.library = parts.get("library")
        self.element = parts.get("element")
        self.object  = parts.get("object")

u = MyUse("use ieee.numeric_std.all")
print(u.library, u.element, u.object)   # 预期: ieee numeric_std all
print(str(u))                            # 预期: use ieee.numeric_std.all
```

**需要观察的现象**：

- 第 2 步里 `parts.get("library")` 之所以能取出 `ieee`，是因为文法里给那段标识符用了 `setResultsName("library")`（或简写 `PP_IDENTIFIER("library")`）。这就是「文法里起名 → `_Parse` 里按名取值」的协作。
- 第 3 步里 `MyUse` 与真实 `VhdlUseStatement` 几乎一模一样，只是真实版多了 `pp.LineStart().leaveWhitespace()`（要求顶格）。你应当体会到：**基类已经包好了「解析 + 调用 `_Parse` + 存 `self.code` + `__str__`」，子类只管填文法和取字段。**

**预期结果**：

- 第 2 步打印 `ieee` / `numeric_std` / `all`。
- 第 3 步 `u.library` 等三个属性正确填充，`str(u)` 回到原文。

**若无法确定运行结果**：若本机未装 pyparsing，可标注「待本地验证」；也可直接阅读真实 `VhdlUseStatement`（第 74–80 行）作为参考答案。

#### 4.3.5 小练习与答案

**练习 1**：`VhdlConstruct.__init__` 里 `if type(code) is not str: code = PrToStr(code)` 这一行的意义是什么？删掉它会怎样？

**参考答案**：它兼容「`code` 是父文法解析出的 `ParseResults`」的情形，先把它转回字符串再统一解析。删掉后，传入 `ParseResults` 时 `code.strip()` 会因 `ParseResults` 没有 `strip` 方法而报错——这正是 `PrToStr` 作为「胶水」的必要性。

**练习 2**：`PP()` 类方法为什么要把 `PP_DEFINITION` 包进 `pp.Group`？以 `entity` 文法引用 `VhdlGenericDeclaration.PP()` 为例说明。

**参考答案**：父文法（`VhdlEntityDeclaration`）里会重复出现多个子构件（多个 generic）。若不加 `Group`，每个 generic 的多个 token 会和兄弟 generic 的 token 混在同一个扁平结果里，难以区分。`Group` 让每个 generic 的 token 成为一个独立的子结果，父构件才能用 `parts.get("generics")` 拿到「一个 generic 列表」，再逐个喂给 `VhdlGenericDeclaration(...)` 重建对象（下一讲 u3-l2 会详讲这条链路）。

**练习 3**：`_Parse` 默认抛 `NotImplementedError`。这种「基类声明、子类必须覆盖」的手法相比直接写抽象基类（`abc.ABC`）有什么取舍？

**参考答案**：这是「按约定」的轻量抽象，不引入 `abc` 依赖、也不阻止实例化基类本身（基类的 `PP_DEFINITION` 还是 `None`，构造时会立刻报错）。好处是简单直接；代价是错误要到运行时构造时才暴露，而非类定义时。本仓库整体偏轻量，选了这种手法。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**贯通任务**：

> **目标**：不依赖 `VhdlParse.py` 现成的 `VhdlUseStatement`，自己用 pyparsing + `VhdlConstruct` 模式，写一个能从一段 VHDL 文本里**扫描出所有 `use` 语句**的小工具，并打印每条的「库.元素.对象」。

建议步骤：

1. **准备积木**：复刻本讲 4.1 讲过的 `PP_IDENTIFIER`（`pp.Word(pp.alphanums+"_")`）。
2. **写文法**：参照 4.3.4 的 `MyUse`，定义 `PP_DEFINITION`，用 `setResultsName`（或括号简写）给三段标识符命名。
3. **写 `_Parse`**：把三段名字存到 `self.library`/`self.element`/`self.object`。
4. **用 `scanString` 扫描**：模仿 `VhdlFile`（下一讲 u3-l2 会细讲）的做法——`for t, s, e in MyUse.PP().scanString(code):` 逐个取出匹配区间 `[s:e]`，再 `MyUse(code[s:e])` 重建对象。
5. **喂真实文本**：把 `example/simpleTb/psi_common_async_fifo.vhd` 的内容读进 `code`，运行你的扫描器，打印结果。

验收标准：

- 你的输出应与直接 `from VhdlParse import VhdlUseStatement` 后用 `scanString` 得到的「库.元素.对象」三元组**完全一致**。
- 能解释：为什么必须用 `scanString`（扫描多处）而不是 `parseString`（只从头解析一次）？

> 提示：`parseString` 要求整段文本从头到尾都符合文法，而 VHDL 文件里 `use` 语句只是其中若干行——必须用 `scanString` 在全文里「捡出」所有匹配处。这正是 `VhdlFile` 第 185 行 `VhdlUseStatement.PP().scanString(code)` 的用意。

## 6. 本讲小结

- `VhdlParse.py` 顶部把 pyparsing 积木分成**词法层**（`PP_IDENTIFIER`/`PP_INTEGER`/`PP_COMMENT`/`PP_RANGEDIR`/`PP_DIRECTION` 等）和**表达式层**（用否定前瞻 `~` 踩刹车、用 `Forward()` 表达递归括号的 `PP_UNQUOTED_EXPR`/`PP_BRACED_EXPR`/`PP_EXPRESSION`）。
- 表达式的「边界」完全由否定前瞻里的哨兵集合（关键字、分号、括号、注释开头）决定；少一个哨兵，表达式就会越界吞字符。
- `PrToStr` 是把树状的 `ParseResults` 递归展平回字符串的「胶水」，是 `VhdlConstruct` 接受 `ParseResults` 入参、以及 `__str__` 回填文本的基础。
- `VhdlConstruct` 用「类属性 `PP_DEFINITION`（文法）+ 方法 `_Parse(parts)`（取字段）」统一描述所有 VHDL 构件；`__init__` 负责解析并调 `_Parse`，`PP()` 负责把文法打包给父文法嵌套引用，`__str__` 负责还原文本。
- 文法里用 `setResultsName`（或括号简写 `expr("name")`）给片段起名，`_Parse` 里再用 `parts.get("name")` 按名取值——这是「定义」与「提取」的协作纽带。
- 真实的 `VhdlCommentLine`、`VhdlUseStatement` 都是十几行的子类，印证了这套基类的省事程度。

## 7. 下一步学习建议

本讲只搭好了**框架与地基**（`PP_*` 积木、`PrToStr`、`VhdlConstruct`）。下一步请进入 **u3-l2「解析 entity/generic/port 与 VhdlFile 读取」**，那里会：

- 用 `VhdlType`/`VhdlRange` 讲怎么解析类型与范围（`to`/`downto`）；
- 用 `VhdlGenericDeclaration`/`VhdlPortDeclaration` 讲怎么解析带默认值和注释的声明；
- 用 `VhdlEntityDeclaration` 讲怎么把 generic/port 嵌进 entity；
- 用 `VhdlFile` 讲怎么用 `scanString` 把 entity、`use` 语句、注释行从整份文件里读出来，最终交还给 u4 的 `DutInfo`。

建议在读 u3-l2 前，先把本讲 4.3.4 的 `MyUse` 跑通，确保你能在「文法 → 解析 → 取字段」这条链路上自如地动手，再去看更复杂的 `entity` 文法会轻松很多。可继续精读的源码位置：[VhdlParse.py:82-121](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L82-L121)（`VhdlRange`/`VhdlType` 等）与 [VhdlParse.py:168-191](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L168-L191)（`VhdlFile`）。
