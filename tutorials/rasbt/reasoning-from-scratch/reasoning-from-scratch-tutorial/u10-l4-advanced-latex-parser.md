# 高级混合 LaTeX 解析器（bonus/parser.py）

## 1. 本讲目标

第 3 章的简单验证器（`grade_answer`）把一切答案都当作「算术/符号表达式」来归一化，遇到**结构性答案**——区间、并集、矩阵、集合、± 号——就会把结构信息洗掉。本讲解读仓库的加分项（bonus）模块 `reasoning_from_scratch/bonus/parser.py`：一个**模式优先（pattern-first）**的混合 LaTeX 解析器，来自 [issue #133](https://github.com/rasbt/reasoning-from-scratch/issues/133)，由 @labdmitriy 原始实现。

学完本讲你应该能够：

1. 说出 `sympy_parser_hybrid` 的分发阶梯：按语法类型（属于 / ± / 文本 / 货币 / 矩阵 / 并集 / 区间 / 集合 / 元组 / 进制下标 / 裸逗号列表）依次尝试，最后才回退到 LaTeX 后端。
2. 理解 `_preprocess` 中两条关键预处理——**Unicode 上标转 `**`** 与**隐式乘法注入 `\cdot`**——为什么能显著提高解析成功率。
3. 分辨「简单解析器漏判、混合解析器判对」与「两者各打五十大板」的样例类型，并能指出每个分歧命中的 `_parse_*` 分支。
4. 说清混合解析器接入评测后**判分口径的变化**：从符号等价（`simplify(a-b)==0`）退化为「规范形字符串相等」，精度提升来自归一化能力扩展，而非等价判定变强。

## 2. 前置知识

### 2.1 回顾：第 3 章简单验证器的管线

本讲承接 u4-l3。先回忆 [ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) 的判分四件套：

- `normalize_text`（L208-L293）：**破坏性**正则归一化——剥特殊 token、拆 `\text{}`、修 LaTeX、`^`→`**`、**删光所有花括号**、**转小写**；
- `sympy_parser`（L296-L317）：`sympy.parse_expr` + 隐式乘法转换，失败返回 `None`；
- `split_into_parts`（L339-L357）：把括号包裹的逗号串拆成片段；
- `grade_answer`（L360-L381）：逐片段 `equality_check`——先字符串捷径、再 `simplify(gtruth - pred) == 0`。

它把答案一律当表达式：`\{1, 2\}` 归一化后花括号被删变成 `1, 2`，集合语义（无序、去重）随之丢失；`(0,9) \cup (9,36)` 里的 `\cup` 没有任何规则处理，原样残留。

### 2.2 两种解析哲学：归一化优先 vs 模式优先

- **归一化优先**（ch03）：先把文本洗成 SymPy 友好的字符串，再交给符号引擎。规则少、可读、快，但洗不掉的就永远丢了。
- **模式优先**（bonus）：先**识别答案的语法类型**，按类型构造 SymPy 的**结构化对象**，识别不出才回退到通用解析。

「混合（hybrid）」之名即由此而来：正则分发 + SymPy 结构对象 + LaTeX 后端回退，三层混合。

### 2.3 本讲会用到的 SymPy 结构化对象

| 对象 | 含义 | 示例 |
|---|---|---|
| `Interval(a, b, left_open, right_open)` | 区间，端点可开可闭 | `(0,9]` → `Interval.open(0, 9)` 的亲戚 |
| `FiniteSet(*elems)` | 有限集合，**无序** | `\{1,2\}` → `FiniteSet(1, 2)` |
| `Union(*sets)` | 并集 | `(0,9)\cup(9,36)` |
| `Matrix(rows)` | 矩阵 | 2×2 矩阵 |
| `Contains(x, S)` | 属于关系 | `x \in (0,5)` |
| `oo` | 正无穷 | `-\infty` → `-oo` |

关键性质：这些对象的 `str()` 是**规范形**（canonical form）——`FiniteSet(2, 1)` 与 `FiniteSet(1, 2)` 打印出来都是 `{1, 2}`。这让「比较两个答案」可以退化为「比较两个规范形字符串」。

### 2.4 LaTeX 后端与 antlr4 依赖

`bonus/parser.py` 顶层导入了 `sympy.parsing.latex.parse_latex`（LaTeX 后端）。这个 import 本身不会失败；**失败发生在调用时**——若环境缺少解析器运行时（仓库建议 `antlr4-python3-runtime==4.11.*`），调用会抛异常。`parser.py` 的设计是**优雅降级**：所有 `_parse_latex` 调用都包在 `try/except` 里，失败返回 `None`，而纯正则驱动的分支（进制下标、货币、文本）不依赖后端、照常工作。仓库的检测方法是：

```python
backend_ok = bonus.normalize_text_hybrid(r"\frac{1}{2}") == "1/2"
```

后端就绪时返回 `"1/2"`；后端缺失时解析失败、原文透传，返回 `\frac{1}{2}`，即得 `False`。

### 2.5 正则命名分组速览

`bonus/parser.py` 大量使用命名分组 `(?P<name>...)`，之后用 `m.group("name")` 取值，比位置分组可读得多。看到 `RE_PM = r"^(?P<a>.+?)\\pm(?P<b>.+)$"` 时，应立刻读出：「把 `\pm` 左右两部分分别捕获为 a 和 b」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [reasoning_from_scratch/bonus/parser.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py) | 本讲主角：混合解析器全部实现（428 行），文件头 L1-L9 注明来自 issue #133 |
| [reasoning_from_scratch/bonus/\_\_init\_\_.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/__init__.py) | 包出口，只导出 `normalize_text_hybrid` 与 `sympy_parser_hybrid`（L6-L8） |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 对照组：简单验证器 `normalize_text` / `sympy_parser` / `grade_answer` |
| [ch03/03_advanced-parser/evaluate_math500_advanced.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/evaluate_math500_advanced.py) | 评测脚本：`--hybrid_parser` 开关切换两套判分 |
| [ch03/03_advanced-parser/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/README.md) | 官方对比说明：两种解析器的差异与取舍（L23-L45） |
| [ch03/03_advanced-parser/compare_with_current_parser.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/compare_with_current_parser.ipynb) | 对比实验 notebook：GPT-5、Qwen3、边界用例三组实测 |
| [tests/test_ch03_advanced_parser.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03_advanced_parser.py) | 回归测试：把两套解析器的分歧样例锁进 CI |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 `_preprocess` 预处理流水线**——上标转换、隐式乘法、单位与度数剥离；
2. **4.2 结构化分支家族**——`_parse_interval` / `_parse_union` / `_parse_matrix` / 集合与 ± 及其余分支；
3. **4.3 `sympy_parser_hybrid` 主分发器与 `normalize_text_hybrid`**——分发阶梯与认领规则；
4. **4.4 与 ch03 简单验证器的精度对比与评测接入**——分歧样例、判分口径变化。

### 4.1 `_preprocess`：预处理流水线

#### 4.1.1 概念说明

无论走哪条分支，最终都要把一段 LaTeX 文本喂给 SymPy。LaTeX 写法千姿百态：`90^\circ` 与 `90` 是同一个角度、`.5` 与 `0.5` 是同一个数、`2\pi` 与 `2\cdot\pi` 是同一个积。`_preprocess` 是一条**顺序敏感的正则工位流水线**，在解析之前把这些「表面差异」统一抹平。其中两个工位对解析成功率影响最大：

- **Unicode 上标转 `**`**：模型可能输出 `2¹⁰` 这种 Unicode 上标（BPE 分词常把它拆成独立字符），SymPy 的 LaTeX 后端并不认识它们；
- **隐式乘法注入**：LaTeX 允许并列表示乘法（`2\sqrt3`、`3\pi`），但解析器的语法要求显式运算符，注入 `\cdot` 后才能通过。

#### 4.1.2 核心流程

```
_preprocess(text)
  1. strip + 去首尾 $ 数学定界符
  2. 删行内/行间数学定界符 \( \) \[ \]
  3. _convert_superscripts: Unicode 上标 → "**指数"（带底数拼接）
  4. RE_LEADING_DOT: ".5" → "0.5"
  5. 删 \left / \right（定界符强化命令）
  6. _strip_text_mbox_and_exponents: 删 "\text{单位}^指数" → 删 "\text{单位}"
  7. _insert_implicit_multiplication: 补 \sqrt 花括号 + 并列乘法注入 \cdot
  8. 删度数标记 ^\circ、^{\circ}、°
```

注意顺序：先删 `\text{...}^...`（带指数的单位，如 `\text{ cm}^2`）再删裸 `\text{...}`——若顺序颠倒，先删掉 `\text{ cm}` 后残留孤立的 `^2`，反而制造垃圾。

#### 4.1.3 源码精读

**上标转换**与 ch03 的同名逻辑同源，映射表覆盖 `⁰-⁹⁺⁻⁽⁾`（[parser.py:L36-L52](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L36-L52)）。转换函数先做「底数 + 上标」拼接，再兜底转换裸上标（[parser.py:L55-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L55-L69)）：

```python
def _convert_superscripts(text: str) -> str:
    ...
    text = re.sub(
        r"([0-9A-Za-z\)\]\}])([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)",
        lambda m: convert(m.group(2), base=m.group(1)),  # "2¹⁰" -> "2**10"
        text,
    )
    text = convert(text)   # 裸上标（无底数）也转成 ASCII
    return text
```

**隐式乘法注入**是四条正则（[parser.py:L90-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L90-L98)）：

```python
def _insert_implicit_multiplication(text: str) -> str:
    text = re.sub(r"\\sqrt\s*([0-9]+)", r"\\sqrt{\1}", text)  # \sqrt2 -> \sqrt{2}
    text = re.sub(r"(\d)\s*(\\sqrt)", r"\1\\cdot\2", text)    # 2\sqrt -> 2\cdot\sqrt
    text = re.sub(r"([\)\}])\s*(\\sqrt)", r"\1\\cdot\2", text)# )\sqrt -> )\cdot\sqrt
    text = re.sub(r"(\d)\s*(\\pi)", r"\1\\cdot\2", text)      # 2\pi  -> 2\cdot\pi
    ...
```

第一条先把无花括号的 `\sqrt2` 规范成 `\sqrt{2}`，随后两条在「数字或右括号紧贴 `\sqrt`」处插入 `\cdot`，`\pi` 同理。对比 ch03 的 `normalize_text`：它用 `\\sqrt\s+([^\\\s{}]+)`（[ch03.py:L260-L264](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L260-L264)）转换无括号根式，但要求 `\sqrt` 后**有空白**——`11\sqrt2` 这种紧贴写法两条规则都打不中，归一化后原样残留、`sympy_parser` 解析失败。这正是简单解析器漏判 `11\sqrt2`（而混合解析器靠 `\cdot` 注入救回）的根源，4.4 节实测验证。

**主流水线**把各工位串起来（[parser.py:L101-L115](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L101-L115)）：先剥定界符与上标（L102-L104）、补前导零（L105-L107）、删 `\left`/`\right`（L108-L109）、删单位（L110）、注入隐式乘法（L111）、最后删度数标记（L112-L114）。单位剥离的细节在 [parser.py:L80-L87](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L80-L87)：先删 `\text{...}^指数`、再删裸 `\text{...}` / `\mbox{...}`。

#### 4.1.4 代码实践

**实践目标**：直观看到 `_preprocess` 每个工位的输入输出，理解「表面差异抹平」。

**操作步骤**（示例代码，保存为任意 `.py` 文件后 `uv run` 或在装好本包的环境运行）：

```python
# 示例代码：观察 _preprocess 的变换效果
import reasoning_from_scratch.bonus.parser as bp

samples = [
    r"90^\circ",        # 度数标记
    r".5",              # 前导点
    r"2\pi",            # 并列乘法
    r"2\sqrt3",         # 并列乘法 + 无括号根式
    r"2¹⁰",             # Unicode 上标
    r"\left( 3 \right)",# \left \right 强化
    r"5\text{ cm}^2",   # 带指数的单位
]
for s in samples:
    print(f"{s!r:25} -> {bp._preprocess(s)!r}")
```

**需要观察的现象**：度数与单位被整段删除、`.5` 变 `0.5`、`2\pi` 变 `2\cdot\pi`、`2\sqrt3` 变 `2\cdot\sqrt{3}`、`2¹⁰` 变 `2**10`、`\left`/`\right` 消失。

**预期结果**（推导自上述源码，可本地核对）：

| 输入 | `_preprocess` 输出 |
|---|---|
| `90^\circ` | `90` |
| `.5` | `0.5` |
| `2\pi` | `2\cdot\pi` |
| `2\sqrt3` | `2\cdot\sqrt{3}` |
| `2¹⁰` | `2**10` |
| `\left( 3 \right)` | `( 3 )` |
| `5\text{ cm}^2` | `5` |

前 6 项可从正则直接推出；单位项依赖 LaTeX 后端与否无关（剥离在预处理期完成）。若个别输出与预期不符，回到对应正则逐字符对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_insert_implicit_multiplication` 要先把 `\sqrt2` 改写成 `\sqrt{2}`，而不是直接在 `2\sqrt2` 里插 `\cdot` 就完事？

**答案**：插入 `\cdot` 只解决乘号缺失；LaTeX 语法里根号被开方数需要花括号包裹成组。`\sqrt{2}` 是合法的组结构，紧贴写法 `\sqrt2` 即使有了乘号也可能无法被后端语法接受。先规范化结构、再补运算符，两步各司其职（对应 [parser.py:L93-L95](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L93-L95) 的前两条正则）。

**练习 2**：`_strip_text_mbox_and_exponents` 里，如果把「删 `\text{...}^...`」和「删裸 `\text{...}`」两条规则调换顺序，`5\text{ cm}^2` 会得到什么？

**答案**：先删裸 `\text{ cm}` 会剩下 `5^2`，语义从「5 厘米平方」变成「25」，单位剥离反而篡改了数值。原顺序先整段删除「单位 + 指数」再删剩余单位，才得到 `5`。

**练习 3**：ch03 的 `normalize_text` 也做了上标转换（[ch03.py:L246-L251](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L246-L251)）。两边的实现是复制关系吗？

**答案**：逻辑几乎逐行相同（同样的映射表、同样的「底数 + 上标」拼接），`bonus/parser.py` 是从 ch03 的实现演化/复用过来的（细节见 [parser.py:L55-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L55-L69) 对 [ch03.py:L237-L251](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L237-L251)），说明上标转换是两类解析器公认的必备工位。

### 4.2 结构化分支家族：区间、并集、矩阵、集合与 ±

#### 4.2.1 概念说明

这一组 `_parse_*` 函数是混合解析器的核心增量：**按语法类型把文本翻译成 SymPy 结构化对象**。每个分支对应一类 ch03 完全无法表达的结构：

- 区间 `(0,9]`：端点开闭是语义的一部分，字符串归一化无法表达「左开右闭」；
- 并集 `\cup`：ch03 对 `\cup` 没有任何规则，残留在文本里导致解析必败；
- 矩阵 `\begin{pmatrix}...\end{pmatrix}`：ch03 的 `normalize_text` 在最后一步删光花括号，矩阵环境名直接被肢解；
- 集合 `\{1,2\}` 与 ± `x\pm2`：集合无序而元组有序，`x\pm2` 表示**两个**值，都需要专门的对象来承载。

#### 4.2.2 核心流程

各分支的通用套路是「**正则切结构 → 递归调 `_parse_single_expr` 解析叶子 → 构造 SymPy 对象，任一叶子失败则整体返回 None**」：

- **区间**：`RE_INTERVAL_BOUNDS` 捕获「左括号、左端点、右端点、右括号」四元组；端点单独处理（`\infty` → `oo`）；`Interval(a, b, left_open, right_open)` 中圆括号映射开端点。
- **并集**：按 `\cup` 切片，**每个片段都必须是区间**，然后 `Union(*intervals)`。
- **矩阵**：`RE_MATRIX` 匹配 matrix/pmatrix/bmatrix/Bmatrix/vmatrix/Vmatrix 环境；行按 `\\` 切、列按 `&` 切，逐格解析后 `Matrix(rows)`。
- **集合**：`\{...\}` 内按逗号拆元素，`FiniteSet(*elems)`；元素内含 `\pm` 时展开成两个值。
- **±**：把 `a\pm b` 翻译成 `FiniteSet(a+b, a-b)`，即 \[ x \pm y \;\Rightarrow\; \{x+y,\; x-y\} \]

#### 4.2.3 源码精读

**区间**（[parser.py:L160-L175](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L160-L175)）——四段捕获 + 端点辅助函数：

```python
def _parse_interval(text: str):
    text = _strip_left_right(text.strip())
    m = RE_INTERVAL_BOUNDS.match(text)     # ^([\(\[])(.+),(.*)([\)\]])$
    if not m:
        return None
    left_brace, left_val, right_val, right_brace = m.groups()
    a = _parse_interval_endpoint(left_val) # \infty -> oo, 其余走 _parse_single_expr
    b = _parse_interval_endpoint(right_val)
    ...
    return Interval(a, b, left_open=(left_brace == "("), right_open=(right_brace == ")"))
```

端点辅助函数 [parser.py:L151-L157](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L151-L157) 把 `\infty` / `+\infty` 映射为 SymPy 的 `oo`、`-\infty` 映射为 `-oo`；圆括号 → 开端点、方括号 → 闭端点，与数学记法一致。

**并集**（[parser.py:L178-L191](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L178-L191)）——按 `\cup` 切开后**只接受区间片段**：

```python
def _parse_union(text: str):
    parts = RE_UNION.split(text)          # RE_UNION = r"\\cup"
    if len(parts) < 2:
        return None
    intervals = []
    for p in parts:
        iv = _parse_interval(p.strip())   # 注意：只尝试区间，不尝试集合
        if iv is None:
            return None                   # 任一片段失败 -> 整体 None
        intervals.append(iv)
    return Union(*intervals)
```

注意一个真实限制：片段解析**只调 `_parse_interval`**。像 `(1,2) \cup \{5\}` 这种「区间并集合」的混合并集，第二个片段过不了区间正则，整个并集解析失败——4.4 节的实测表里会再次遇到它。

**矩阵**（[parser.py:L194-L218](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L194-L218)）——环境正则覆盖六种矩阵环境（[parser.py:L19-L21](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L19-L21) 中 `[pbvBV]?` 可选前缀，兼得 `matrix` 本体），行按字面 `\\` 分、列按 `&` 分：

```python
def _parse_matrix(text: str):
    m = RE_MATRIX.search(text)            # \\begin{[pbvBV]?matrix}(.*)\\end{...}
    ...
    rows_raw = re.split(r"\\\\", inner)   # LaTeX 换行符是两个反斜杠
    ...
    cols = [c.strip() for c in row.split("&")]
    parsed_cols = []
    for c in cols:
        expr = _parse_single_expr(c)      # 每个格子递归走单表达式解析
        if expr is None:
            return None
        parsed_cols.append(expr)
    ...
    return Matrix(rows)
```

**集合**（[parser.py:L238-L264](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L238-L264)）与 **±**（[parser.py:L267-L278](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L267-L278)）都落到 `FiniteSet`。集合分支还有个细节：元素内含 `\pm` 时（L246-L254）展开为「加」和「减」两个元素分别入集合；顶层 `_parse_plus_minus` 则直接返回 `FiniteSet(a + b, a - b)`。`FiniteSet` 的无序性由 SymPy 保证——`FiniteSet(2, 1)` 与 `FiniteSet(1, 2)` 是同一个对象、`str()` 相同，这正是集合语义。

其余分支一览（同样套路，不再展开）：

| 分支 | 源码 | 语义 |
|---|---|---|
| `_parse_text_answer` | [parser.py:L130-L137](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L130-L137) | `\text{...}` / `\mbox{...}` 整体包裹时**原样**取出内容（保留大小写与括号） |
| `_parse_currency` | [parser.py:L140-L148](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L140-L148) | `\$32,\!348` 剥货币符与 `\,`/`\!` 间距、去逗号 → `float` |
| `_parse_tuple_or_list` | [parser.py:L221-L235](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L221-L235) | 括号包裹、逗号 ≥2 段 → Python 元组（**有序**，区别于集合） |
| `_parse_base_subscript` | [parser.py:L281-L293](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L281-L293) | `52_8` 校验进制合法（2–36 且数位有效）后**原样返回字符串** |
| `_parse_bare_comma_list` | [parser.py:L296-L314](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L296-L314) | `1,-2` 裸逗号列表 → 元组；先用千分位正则把关 |
| `_parse_membership` | [parser.py:L317-L337](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L317-L337) | `x \in (0,5)` → `Contains(x, Interval.open(0, 5))` |

其中 `_parse_base_subscript` 有个值得玩味的细节（L287-L291）：`int(digits, base)` 的返回值**被丢弃**，只用作合法性校验，最终返回原字符串 `"52_8"`——它并不理解「8 进制的 52 等于 10 进制的 42」，只是把进制下标答案当作不透明记号保真传递。

#### 4.2.4 代码实践

**实践目标**：直接调用各分支函数，观察它们产出的 SymPy 对象与规范形字符串。

**操作步骤**（示例代码）：

```python
# 示例代码：逐个分支构造 SymPy 结构化对象
import reasoning_from_scratch.bonus.parser as bp

demos = [
    ("interval",  bp._parse_interval,  r"(0, 9]"),
    ("interval",  bp._parse_interval,  r"[1, \infty)"),
    ("union",     bp._parse_union,     r"(0,9) \cup (9,36)"),
    ("matrix",    bp._parse_matrix,    r"\begin{pmatrix} -1 & 0 \\ 0 & -1 \end{pmatrix}"),
    ("set",       bp._parse_set_braces, r"\{2, 1\}"),
    ("pm",        bp._parse_plus_minus, r"x\pm 2"),
]
for name, fn, s in demos:
    obj = fn(s)
    print(f"{name:9} {s!r:38} -> {obj!r}")
print("集合无序验证:", str(bp._parse_set_braces(r"\{1, 2\}"))
      == str(bp._parse_set_braces(r"\{2, 1\}")))
```

**需要观察的现象**：区间对象带开端点标记；`\infty` 变成 `oo`；并集打印为 `Union(Interval...)`；pmatrix 环境变成 `Matrix([[-1, 0], [0, -1]])`；两组元素顺序不同的集合打印**完全相同**；`x\pm 2` 变成含两个元素的 `FiniteSet`。

**预期结果**（依源码推导；前几项同时被 [tests/test_ch03_advanced_parser.py:L67-L88](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03_advanced_parser.py#L67-L88) 锁定）：

- `(0, 9]` → 左开右闭的 `Interval` 对象（SymPy 对半开区间有专门打印，形如 `Interval(0, 9]`，具体拼写待本地验证）
- `[1, \infty)` → 左闭右开、右端为 `oo` 的区间对象
- `(0,9) \cup (9,36)` → `Union(Interval.open(0, 9), Interval.open(9, 36))`（测试 L72-L75 记录的确切打印）
- 矩阵 → `Matrix([[-1, 0], [0, -1]])`
- `\{2, 1\}` 与 `\{1, 2\}` 的 `str()` 相等 → 打印 `True`
- `x\pm 2` → `{x - 2, x + 2}` 形式的 `FiniteSet`

除第一项的 SymPy 打印拼写属「待本地验证」外，其余均可从源码与测试直接推出。

#### 4.2.5 小练习与答案

**练习 1**：`(0, 9]` 里左端是圆括号、右端是方括号。写出 `_parse_interval` 构造对象时 `left_open` 与 `right_open` 各取什么值，并说明与数学记法的对应。

**答案**：`left_open=(left_brace == "(")` 为 `True`（圆括号 = 开端点 = 不含端点），`right_open=(right_brace == ")")` 为 `False`（方括号 = 闭端点 = 含端点）。即数学记法「圆开方闭」被逐字翻译成布尔参数（[parser.py:L171-L173](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L171-L173)）。

**练习 2**：`\{1, 2\}` 与 `(1, 2)` 在混合解析器里分别变成什么对象？判定 `\{1, 2\}` 与 `\{2, 1\}` 相等、但 `(1, 2)` 与 `(2, 1)` 不相等，依据是什么？

**答案**：前者是 `FiniteSet(1, 2)`（无序、去重），后者是 `tuple(1, 2)`（`_parse_tuple_or_list`，有序）。`FiniteSet` 的元素在 SymPy 内部按规范序存储，两者 `str()` 相同；元组保序，`(2, 1)` 的 `str()` 不同。数学上这正是集合与有序对（坐标）的区别。

**练习 3**：`_parse_union` 为什么在 `(1,2) \cup \{5\}` 上返回 `None`？

**答案**：它把每个 `\cup` 片段**只**交给 `_parse_interval`（[parser.py:L183-L186](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L183-L186)），`\{5\}` 不满足区间正则 `^([\(\[])(.+),(.*)([\)\]])$`（首字符不是 `(`/`[`，且无逗号），于是整体放弃。要支持「区间并集合」需要把片段尝试扩展为「区间或集合」——这是一个自然的二次开发点。

### 4.3 `sympy_parser_hybrid` 主分发器与 `normalize_text_hybrid`

#### 4.3.1 概念说明

`sympy_parser_hybrid` 是总调度：按**从特殊到一般**的固定顺序把文本派给各分支，谁先认领就返回谁的结果。它和 `normalize_text_hybrid` 一起构成对外的两个入口（也是 `bonus/__init__.py` 仅有的两个导出，[\_\_init\_\_.py:L6-L8](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/__init__.py#L6-L8)）。两个设计问题值得带着读：

1. **顺序为什么这样排？** 越特殊、越不容易误判的语法越靠前；最通用的「单表达式」永远垫底。
2. **分支失败后要不要继续下落？** 各分支并不一致——有的「条件式认领」（失败继续尝试后续分支），有的「独占式认领」（触发即返回，失败也不再下落）。

#### 4.3.2 核心流程

分发阶梯（自上而下，✓ 条件式 / ★ 独占式）：

```
sympy_parser_hybrid(answer)
  ├─ 含 \in      → ✓ _parse_membership
  ├─ 含 \pm      → ✓ _parse_plus_minus
  ├─ \text{}/\mbox{} 整体 → ★ _parse_text_answer
  ├─ 以 \$ 开头   → ✓ _parse_currency
  ├─ 含 \begin{ 且含 matrix → ★ _parse_matrix
  ├─ 含 \cup     → ★ _parse_union
  ├─ 含 \infty 或 「括号包裹且带 [/]」 → ✓ _parse_interval
  ├─ \{...\}     → ✓ _parse_set_braces
  ├─ 括号包裹逗号 → ✓ _parse_tuple_or_list
  ├─ 数字_进制   → ✓ _parse_base_subscript
  ├─ 裸逗号列表   → ✓ _parse_bare_comma_list
  ├─ 已是 SymPy 风格（sqrt( / ** 且无反斜杠）→ sympify
  └─ 兜底：_parse_latex → sympify → None
```

一个容易忽略的语义细节：**上下文改变 `(0,9)` 的身份**。顶层的 `(0,9)`（纯圆括号、无 `\infty`、无方括号）不会进区间分支——它落到 `_parse_tuple_or_list` 成为**有序点对**；而在并集片段或 `\in` 右侧，`_parse_interval` 被直接调用，同样的 `(0,9)` 被当作**开区间**。触发条件见 [parser.py:L370-L374](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L370-L374)：`RE_INFTY` 命中，或 `RE_TUPLE_LIKE` 命中**且**文本含 `[` 或 `]`。

`normalize_text_hybrid` 则是「归一化门面」：调分发器拿对象，失败透传原文，成功返回规范形字符串。

#### 4.3.3 源码精读

**分发器主体**（[parser.py:L340-L412](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L340-L412)）。先看条件式认领的典型写法（属于分支，[parser.py:L346-L354](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L346-L354)）：

```python
if r"\in" in text:
    result = _parse_membership(text)
    if result is not None:
        return result          # 失败则继续往下尝试

if r"\pm" in text:
    result = _parse_plus_minus(text)
    if result is not None:
        return result
```

再看独占式认领（并集分支，[parser.py:L367-L368](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L367-L368)）：

```python
if r"\cup" in text:
    return _parse_union(text)   # 无条件返回：失败也返回 None，不再下落
```

矩阵分支（L364-L365）同样独占。读后可以体会出取舍：`\begin{...matrix...}` 与 `\cup` 是**无歧义**的结构标记——出现即表明答案就是这个类型，失败了交给通用回退也没有意义；而「含逗号」「括号包裹」这类触发词是**启发式**的，失败后让位给后续分支才合理。

**两级兜底**（[parser.py:L395-L412](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L395-L412)）：

```python
preprocessed = _preprocess(text)
...
is_sympy_style = RE_SYMPY_STYLE.search(preprocessed) and "\\" not in preprocessed
if is_sympy_style:
    try:
        return sympify(preprocessed)     # 已是 SymPy 语法（sqrt(3)/2、x**2）直接解析
    except Exception:
        pass

try:
    return _parse_latex(preprocessed)    # LaTeX 后端
except Exception:
    try:
        return sympify(preprocessed)     # 最后再试一次 SymPy 语法
    except Exception:
        return None
```

`RE_SYMPY_STYLE`（L34）识别「形如 `sqrt(`/`log(`/`**` 且不含反斜杠」的输入——模型有时直接输出 SymPy 风格文本（如 `3*sqrt(13)`），对它们走 `sympify` 比绕 LaTeX 后端更快更稳。最后的三级链 `sympify → _parse_latex → sympify` 用 `try/except` 串成**优雅降级**：LaTeX 后端缺失（抛 ImportError）或语法不收（抛解析异常）都只是返回 `None`，不炸调用方。

**归一化门面**（[parser.py:L415-L427](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L415-L427)）：

```python
def normalize_text_hybrid(answer: str) -> str:
    if not answer:
        return ""
    expr = sympy_parser_hybrid(answer)
    if expr is None:
        return answer.strip()   # 解析失败：原文透传（退化为字面比对）
    if isinstance(expr, str):
        return expr             # 进制下标等分支本来就返回字符串
    return str(expr)            # SymPy 对象 -> 规范形字符串
```

#### 4.3.4 代码实践

**实践目标**：给一组输入逐个标注「被哪个分支认领」，把分发阶梯从纸面知识变成可观察行为。

**操作步骤**（示例代码）：

```python
# 示例代码：分支认领追踪器（按分发器 L346-L393 的顺序手工复现触发条件）
import reasoning_from_scratch.bonus.parser as bp

def which_branch(text):
    raw = text.strip()
    t = bp._strip_left_right(raw)
    checks = [
        ("membership",  r"\in" in t,                        lambda: bp._parse_membership(t)),
        ("plus_minus",  r"\pm" in t,                        lambda: bp._parse_plus_minus(t)),
        ("text/mbox",   bp.RE_TEXT.match(raw) or bp.RE_MBOX.match(raw),
                                                        lambda: bp._parse_text_answer(raw)),
        ("currency",    raw.startswith(r"\$"),              lambda: bp._parse_currency(raw)),
        ("matrix",      r"\begin{" in raw and "matrix" in raw, lambda: bp._parse_matrix(raw)),
        ("union",       r"\cup" in t,                       lambda: bp._parse_union(t)),
        ("interval",    bp.RE_INFTY.search(t) or (bp.RE_TUPLE_LIKE.match(t)
                         and ("]" in t or "[" in t)),       lambda: bp._parse_interval(t)),
        ("set_braces",  t.startswith(r"\{") and t.endswith(r"\}"),
                                                        lambda: bp._parse_set_braces(t)),
        ("tuple/list",  bp.RE_TUPLE_LIKE.match(t),          lambda: bp._parse_tuple_or_list(t)),
        ("base_sub",    bp._parse_base_subscript(t) is not None,
                                                        lambda: bp._parse_base_subscript(t)),
        ("comma_list",  bp._parse_bare_comma_list(t) is not None,
                                                        lambda: bp._parse_bare_comma_list(t)),
    ]
    for name, trig, fn in checks:
        if trig:
            res = fn()
            if res is not None or name in ("matrix", "union", "text/mbox"):
                return name, res          # 独占分支失败也在此终止
    return "fallback(_preprocess + parse_latex/sympify)", bp.sympy_parser_hybrid(raw)

for s in [r"x \in (0, 5)", r"(0,9) \cup (9,36)", r"52_8", r"\$32,\!348",
          r"\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}", r"(0, 9)", r"1,-2",
          r"\frac{14}{3}", r"3*sqrt(13)"]:
    name, obj = which_branch(s)
    print(f"{s!r:42} -> {name:20} {obj!r}")
```

**需要观察的现象**：每个输入命中且只命中一个分支；`(0, 9)` 走 tuple/list 而不是 interval（印证 4.3.2 的上下文语义）；`3*sqrt(13)` 命中 fallback 里的 sympy 捷径；`52_8` 返回的是字符串而不是 SymPy 对象。

**预期结果**（依分发器源码推导；LaTeX 后端相关项需后端就绪）：

| 输入 | 认领分支 | 输出 |
|---|---|---|
| `x \in (0, 5)` | membership | `Contains(x, Interval.open(0, 5))` |
| `(0,9) \cup (9,36)` | union | `Union(Interval.open(0, 9), Interval.open(9, 36))` |
| `52_8` | base_sub | `"52_8"`（原样字符串） |
| `\$32,\!348` | currency | `32348.0` |
| `\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}` | matrix | `Matrix([[1, 2], [3, 4]])` |
| `(0, 9)` | tuple/list | `(0, 9)` |
| `1,-2` | comma_list | `(1, -2)` |
| `\frac{14}{3}` | fallback | `14/3` |
| `3*sqrt(13)` | fallback（sympy 捷径） | `3*sqrt(13)` |

其中 `\frac{14}{3}` → `14/3`、currency → `32348.0`、`52_8` → `52_8` 均被测试文件直接锁定（[tests/test_ch03_advanced_parser.py:L35-L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03_advanced_parser.py#L35-L44)）。其余「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `\cup` 与 `\begin{matrix}` 分支采用「触发即返回」的独占式认领，而逗号类分支失败后可以继续下落？

**答案**：`\cup`、矩阵环境标记是**无歧义结构标记**——一旦出现，答案的类型就确定了；若连专门的分支都解析失败，说明内容有分支覆盖不到的问题，交给通用回退也只能得到错误结果，不如明确返回 `None` 让 `normalize_text_hybrid` 透传原文。而「含逗号」「括号包裹」是弱启发式，同一个文本完全可能同时像一个列表又像一个别的结构，失败后让后续分支再试能提高整体命中率。

**练习 2**：`normalize_text_hybrid` 在解析失败时返回 `answer.strip()`（原文透传）。这对下游「字符串相等」判分意味着什么？

**答案**：意味着两个**都没解析出来**的答案只有在**逐字符相同**（含空格）时才判对；解析失败不是「报错」而是「退化成最保守的字面比对」。因此预处理抹平的每一种表面差异（空格、`\left`、度数标记……）都直接转化为判分召回率。

**练习 3**：不看代码回答：`52_8` 与 `42` 表达的是同一个数（8 进制 52 = 10 进制 42）。混合解析器会把它们判为相等吗？

**答案**：不会。`_parse_base_subscript` 只做合法性校验后**原样返回字符串** `52_8`（[parser.py:L281-L293](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L281-L293)），`int(digits, base)` 的换算结果被丢弃；`42` 走回退分支成为 `Integer(42)`。两个规范形字符串 `52_8` 与 `42` 不等。（ch03 同样判错：它把 `52_8` 按数字字面量解析成 `528`。）

### 4.4 与 ch03 简单验证器的精度对比与评测接入

#### 4.4.1 概念说明

对比要在**两个独立维度**上看，否则容易得出「混合解析器全面更强」的错误结论：

1. **归一化能力**（能识别多少种答案形态）：混合解析器显著更强——结构化分支 + LaTeX 后端覆盖了区间/并集/矩阵/集合/±/进制/货币，而 ch03 只有字符串洗牌。
2. **等价判定**（两个已解析的答案何时算相等）：混合评测脚本用**规范形字符串相等**，ch03 的 `grade_answer` 用**符号等价** `simplify(a-b)==0`。前者识别 `\frac{14}{3}` 与 `14/3`（都归一为 `14/3`），但会漏判 `0.5` 与 `\frac{1}{2}`（分别打印成 `0.500000000000000` 与 `1/2`）——这类「数值相同、形态不同」恰恰是 `simplify` 的强项。

即：混合方案**扩展了轴 1、弱化了轴 2**，精度净变化取决于答案分布。仓库实测（见 4.4.3）是净正收益，但并非没有反向样例。

#### 4.4.2 核心流程

评测接入的改动极小，精髓在判分三行的替换：

```
ch03 路径（evaluate_math500_stream）:
  extracted ── grade_answer ──> normalize_text × 2 → split → equality_check（符号等价）

hybrid 路径（evaluate_math500_stream_hybrid）:
  extracted ──> pred = normalize_text_hybrid(extracted)
                gold = normalize_text_hybrid(row["answer"])
                is_correct = (pred == gold)        # 规范形字符串相等
```

提取环节（`extract_final_candidate`）**原样复用 ch03**——两套方案只换了「归一化 + 判定」，`\boxed{}` 契约不动。

#### 4.4.3 源码精读

**评测脚本的判分口径**（[evaluate_math500_advanced.py:L115-L118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/evaluate_math500_advanced.py#L115-L118)）：

```python
extracted = extract_final_candidate(gen_text)      # 提取仍用 ch03
pred = bonus.normalize_text_hybrid(extracted)      # 归一化换成混合解析器
gold = bonus.normalize_text_hybrid(row["answer"])
is_correct = pred == gold                          # 字符串相等取代符号等价
```

**开关与后端自检**：`--hybrid_parser` 命令行参数（[evaluate_math500_advanced.py:L74-L80](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/evaluate_math500_advanced.py#L74-L80)）选择走哪套判分；运行前用 `\frac{1}{2}` 自检后端，失败则打印安装建议（L205-L209）：

```python
if args.hybrid_parser:
    backend_ok = bonus.normalize_text_hybrid(r"\frac{1}{2}") == "1/2"
    print("LaTeX backend ready:", backend_ok)
    if not backend_ok:
        print('Suggestion: uv pip install "antlr4-python3-runtime==4.11.*"')
```

**测试锁定的分歧样例**（[tests/test_ch03_advanced_parser.py:L47-L88](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03_advanced_parser.py#L47-L88)）。测试用 `current_parser_output`（ch03 归一化 + 解析，L17-L22）与 `normalize_text_hybrid` 并排断言，其中「带 LaTeX 后端」组（L63-L88 的 `skipif` 装饰器：后端不可用就跳过）记录了六个权威分歧：

| 输入 | ch03 输出 | hybrid 输出 | hybrid 命中分支 |
|---|---|---|---|
| `52_8` | `528` | `52_8` | `_parse_base_subscript` |
| `\text{(C)}` | `c` | `(C)` | `_parse_text_answer` |
| `11,\! 111,\! 111,\! 100` | `(11, 111, 111, 100)` | `11111111100` | 兜底链（`_parse_bare_comma_list` 的千分位正则 L300-L302 拒绝把它当列表，最终由回退解析消化千分位逗号；后端具体行为待本地验证） |
| `11\sqrt2` | `11\sqrt2`（解析失败残留） | `11*sqrt(2)` | 兜底 + `_preprocess` 隐式乘法 |
| `(0,9) \cup (9,36)` | 原文残留 | `Union(Interval.open(0, 9), Interval.open(9, 36))` | `_parse_union → _parse_interval` |
| `x=5` | `x=5`（解析失败残留） | `Eq(x, 5)` | 兜底（LaTeX 后端把 `=` 解析为方程） |

三个 ch03 漏判的机理值得各说一句：

- **`52_8` → 528**：Python 数字字面量允许下划线分隔（PEP 515），`parse_expr` 把 `52_8` tokenize 成一个数字 528——不是 bug，却悄悄改变了进制下标答案的语义。
- **`11\sqrt2` 残留**：ch03 的无括号 `\sqrt` 规则要求后面有空白（[ch03.py:L260-L264](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L260-L264)），紧贴写法打不中，反斜杠残留使解析失败。
- **`\text{(C)}` → `c`**：ch03 归一化时全局转小写且解析 `(c)` 为符号，大小写与括号双重丢失；hybrid 的 `_parse_text_answer` 原样保留——注意这是把双刃剑：`\text{(C)}` 与 `\text{(c)}` 在 ch03 下判等（过度宽容），在 hybrid 下判不等（严格但可能过严）。

**notebook 的三组实测**（[compare_with_current_parser.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/compare_with_current_parser.ipynb)，输出为已保存的执行结果）：

| 实验 | ch03 | hybrid |
|---|---|---|
| 500 题标准答案自检（boxed 包装后回判） | 500/500 | 500/500 |
| GPT-5 答案重判 | 457/500 | 458/500 |
| Qwen3 0.6B reasoning 答案重判 | 277/500 | 282/500 |
| 8 个 LaTeX 边界用例 | 4/8 | 8/8 |

两个解读要点：其一，**标准答案自检双双满分**——因为 gold 自己跟自己比，字面比对天然全对，这说明自检只能验证「不误伤」，验证不了「能判对」；其二，真实模型答案上 hybrid 净提升 +1（GPT-5）与 +5（Qwen3），而**边界用例上翻倍**（4→8）——模型输出越是 LaTeX 结构复杂，混合解析器优势越大。README 对此的总结见 [ch03/03_advanced-parser/README.md:L23-L45](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/README.md#L23-L45)：ch03 简单快、易讲解；hybrid 覆盖广、规则多、依赖 LaTeX 后端。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：构造 10 个复杂答案，让两套判分管线并排跑，输出双列对照表，统计不一致样例数，并为每个不一致样例标注命中的 `_parse_*` 分支。

**操作步骤**（示例代码）：

```python
# 示例代码：compare_parsers.py —— 双解析器判分对照
# 前置：已安装本包与 sympy；建议先 uv pip install "antlr4-python3-runtime==4.11.*"
import warnings
import reasoning_from_scratch.bonus as bonus
import reasoning_from_scratch.ch03 as ch03

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    backend_ok = bonus.normalize_text_hybrid(r"\frac{1}{2}") == "1/2"
print("LaTeX backend ready:", backend_ok)
if not backend_ok:
    print('部分用例将失败，建议: uv pip install "antlr4-python3-runtime==4.11.*"')

def hybrid_grade(pred_text, gt_text):   # 复刻 evaluate_math500_advanced.py L116-L118 的口径
    return bonus.normalize_text_hybrid(pred_text) == bonus.normalize_text_hybrid(gt_text)

# (编号, 标准答案, 模型输出, 预期 hybrid 命中分支)
CASES = [
    ("U1", r"(0,9) \cup (9,36)",   r"(0,9)\cup(9,36)",   "_parse_union -> _parse_interval"),
    ("U2", r"\begin{pmatrix} -1 & 0 \\ 0 & -1 \end{pmatrix}",
           r"\begin{bmatrix}-1&0\\0&-1\end{bmatrix}",    "_parse_matrix"),
    ("U3", r"11\sqrt2",            r"11\cdot\sqrt{2}",   "兜底 + _preprocess 隐式乘法"),
    ("U4", r"x=5",                 r"x = 5",             "兜底（LaTeX 后端 -> Eq）"),
    ("U5", r"\{1, 2\}",            r"\{2, 1\}",          "_parse_set_braces（FiniteSet 无序）"),
    ("U6", r"x \in (0, 5)",        r"x\in(0,5)",         "_parse_membership -> _parse_interval"),
    ("U7", r"[1, \infty)",         r"[1,\infty)",        "_parse_interval（端点 oo）"),
    ("D1", r"\text{(C)}",          r"\text{(c)}",        "_parse_text_answer（方向相反：ch03 过度宽容）"),
    ("F1", r"(1,2) \cup \{5\}",    r"(1,2)\cup\{5\}",    "_parse_union 独占认领后失败（混合并集不支持）"),
    ("F2", r"\$32,\!348",          r"32348",             "_parse_currency（float 与 Integer 打印不同）"),
]

n_disagree = 0
for name, gold, pred, branch in CASES:
    a = ch03.grade_answer(pred, gold)
    b = hybrid_grade(pred, gold)
    mark = ""
    if a != b:
        n_disagree += 1
        mark = "  <-- 不一致"
    print(f"{name}: ch03={str(a):5} hybrid={str(b):5} | 分支: {branch}{mark}")
print(f"\n不一致样例数: {n_disagree} / {len(CASES)}")
```

**需要观察的现象**：U 系列应看到 ch03 全 False、hybrid 全 True；D1 出现**方向相反**的分歧（ch03 True、hybrid False）；F1/F2 两者皆 False——混合解析器也有自己的盲区。

**预期结果**（逐例推导自两侧源码；标注「待本地验证」的项请以实际运行产出为准）：

| 编号 | ch03 | hybrid | 分歧机理（已推导 / 待本地验证） |
|---|---|---|---|
| U1 | False | True | ch03：`\cup` 无规则残留、拆片后解析失败；hybrid：两侧归一为同一 `Union(Interval.open(0, 9), Interval.open(9, 36))`（测试 L72-L75 锁定） |
| U2 | False（待本地验证） | True | ch03：`normalize_text` 删花括号肢解环境名、两侧残留串不同；hybrid：`[pbvBV]?` 兼容 pmatrix/bmatrix，同得 `Matrix([[-1, 0], [0, -1]])` |
| U3 | False | True | ch03：gold `11\sqrt2` 紧贴写法两条 sqrt 规则都打不中、解析失败；hybrid：隐式乘法注入后两侧同为 `11*sqrt(2)`（测试 L71-L72 锁定） |
| U4 | False | True | ch03：`x=5` 是赋值语法、`parse_expr` 失败，空格差异又挡住字符串捷径；hybrid：两侧同得 `Eq(x, 5)`（测试 L77 锁定） |
| U5 | False | True | ch03：花括号删除后 `(1,2)` 成元组相减报 TypeError；hybrid：`FiniteSet` 无序，两侧同打印 `{1, 2}` |
| U6 | False | True | ch03：`\in` 无规则；hybrid：两侧同得 `Contains(x, Interval.open(0, 5))` |
| U7 | False | True | ch03：`\infty` 无规则、空格差异致字符串不等；hybrid：端点映射 `oo`，同得含 `oo` 的区间 |
| D1 | **True** | **False** | ch03：两侧归一化后字符串已相同（`(c)`，即便解析也是同一符号 `c`），字符串捷径判等（过度宽容）；hybrid：`(C)` 与 `(c)` 原样保留、大小写敏感判不等（严格但可能过严） |
| F1 | False | False | hybrid：`\cup` 分支**独占认领**，第二片段 `\{5\}` 过不了区间正则 → 整体 None → 原文透传，空格差异致不等；ch03：同样残留失败 |
| F2 | False | False | hybrid：gold 走 `_parse_currency` 得 `32348.0`，pred 走兜底得 `Integer` 打印 `32348`，`"32348.0" != "32348"`；ch03：gold 归一为 `32, 348`（`\!` 被剥但空格挡住千分位规则），与 `32348` 判不等 |

预期统计：**不一致 8 例**（U1-U7 与 D1），其中 7 例 hybrid 胜、1 例（D1）方向相反；F1、F2 是双方共同的盲区。若本地运行与上表不符（尤其 U2 与 F2 的细节），优先核对 LaTeX 后端是否就绪，再逐分支排查。

#### 4.4.5 小练习与答案

**练习 1**：GPT-5 答案重判上 ch03 与 hybrid 只差 1 题（457 vs 458），能否据此说混合解析器没什么用？

**答案**：不能。差值取决于答案分布：GPT-5 输出的答案大多已是「单表达式」形态（两套解析器都处理得好），结构性答案占比低。边界用例实验（4/8 → 8/8）才暴露了真实差距；越弱的模型（Qwen3 0.6B，277→282）与越 LaTeX 化的输出，hybrid 的相对收益越大。评测结论必须联系答案分布看。

**练习 2**：hybrid 评测把判分换成「规范形字符串相等」。举一个 ch03 判对、hybrid 判错的答案对，并解释原因。

**答案**：gold `\frac{1}{2}`、pred `0.5`。ch03：两侧分别解析为 `Rational(1,2)` 与 `Float(0.5)`，`simplify(1/2 - 0.5) == 0` 成立 → True。hybrid：规范形分别为 `1/2` 与 `0.500000000000000`（SymPy 浮点打印 15 位有效数字），字符串不等 → False（此例的具体打印位数待本地验证，机制成立）。这是「归一化增强、等价判定减弱」的直接体现。

**练习 3**：如果要让 `evaluate_math500_advanced.py` 同时享受「hybrid 的结构归一化」与「ch03 的符号等价」，最小改动是什么？

**答案**：把判分从 `pred == gold` 改为两段式——先用 `sympy_parser_hybrid` 把两侧解析成 SymPy 对象，字符串相等则直接 True（保留快捷路径与结构对象比较），否则对两个对象做 `simplify(a - b) == 0`（沿用 [ch03.py:L320-L336](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L320-L336) 的 `equality_check` 思路，注意元组/矩阵等不能相减的类型需先判同型）。这是自然的二次开发练习。

## 5. 综合实践

**任务：搭建一个 15 分钟内可完成的「双解析器基准」。**

1. **自检层**（复刻 notebook 的 Self-parse check）：调用 `ch03.load_math500_test()` 加载 500 题（首次运行会自动下载数据文件），把每题标准答案包成 `\boxed{...}` 后用 `extract_final_candidate` 提取，分别走 `ch03.grade_answer` 与「hybrid 归一化 + 字符串相等」两套判分，验证两侧都能 500/500——确认环境与后端就绪。
2. **分歧层**：运行 4.4.4 的 `compare_parsers.py`，得到 10 例双列对照表与不一致计数，并为每个不一致样例写出命中的 `_parse_*` 分支（对照 4.3.3 的分发阶梯核对）。
3. **扩展层**：自造 2 个新用例加入表格——要求一个「ch03 错、hybrid 对」（提示：想想 `\infty`、矩阵环境、`\pm`、集合无序性），一个「两者都错」（提示：进制换算、混合并集、float/Integer 打印差异、`0.5` vs `\frac{1}{2}`）。为每个新用例写一句「失败发生在哪一层：触发、分支解析、对象构造，还是字符串比对」。
4. **（可选，需模型与算力）**：在 10 题子集上运行
   `uv run evaluate_math500_advanced.py --dataset_size 10 --hybrid_parser --max_new_tokens 2048`
   与不带 `--hybrid_parser` 的同款各一次，对比两次 jsonl 日志中 `pred_normalized_hybrid`/`correct` 字段与 ch03 版本的判分差异（脚本参数见 [evaluate_math500_advanced.py:L30-L81](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/evaluate_math500_advanced.py#L30-L81)）。

产出物：一张三层的对照表 + 每个分歧的分支标注。完成后你应当能对任意一个判分错误回答「错在哪一层、哪个分支」。

## 6. 本讲小结

- `bonus/parser.py` 是**模式优先**的混合解析器：`sympy_parser_hybrid` 按「属于 → ± → 文本 → 货币 → 矩阵 → 并集 → 区间 → 集合 → 元组 → 进制下标 → 裸逗号列表 → sympify 捷径 → LaTeX 后端 → sympify」的阶梯分发，特殊语法在前、通用回退垫底。
- 分支认领分两种：`membership`/`pm`/区间/集合等**条件式**（失败继续下落），`matrix`/`union`/`text` **独占式**（触发即返回）——无歧义结构标记不值得再回退。
- `_preprocess` 的两大成功关键是 **Unicode 上标转 `**`** 与**隐式乘法注入 `\cdot`**（连带 `\sqrt2` → `\sqrt{2}` 的结构规范化）；单位剥离必须「先带指数、后裸单位」有序执行。
- 结构化分支把答案翻译成 `Interval`/`Union`/`Matrix`/`FiniteSet`/`Contains` 对象，对象 `str()` 的规范形（如 `FiniteSet` 无序）让判分可退化为字符串相等——这是能力也是代价：`0.5` 与 `1/2`、`32348.0` 与 `32348` 这类跨形态等价反而漏判。
- 实测定位了三类样例：**ch03 漏判而 hybrid 判对**（并集/矩阵/紧贴根式/方程/集合无序/进制保真），**方向相反**（`\text{(C)}` 大小写：ch03 过度宽容、hybrid 严格），**两者皆错**（区间并集合的混合并集、进制换算、float 与 Integer 打印差异）。
- 接入评测只改三行：提取仍用 ch03 的 `extract_final_candidate`，归一化换 `normalize_text_hybrid`，判定从 `grade_answer`（符号等价）换成 `pred == gold`（规范形字符串相等）；notebook 实测边界用例 4/8 → 8/8，Qwen3 答案重判 277 → 282。

## 7. 下一步学习建议

- **横向对比评测方法**：下一讲 u10-l5「更多评测方法：MMLU、排行榜与 LLM-as-judge」把判分问题推广到无客观答案的场景（字母匹配、logprob、教师强制、Elo/BT 排名、LLM 裁判），与本讲的「可验证奖励」路线互为补充。
- **回看判分管线的消费方**：`grade_answer` 是第 6 章 `reward_rlvr` 奖励与第 7 章格式奖励的判分内核（参见 u7-l1、u8-l4）。想清楚「换更聪明的解析器 = 换奖励函数的口径」——它会直接改变 RL 训练信号，可以思考：若把 `reward_rlvr` 的判分换成 hybrid，训练动态会更好还是更不稳？
- **动手改造**：完成 4.4.5 练习 3 的两段式判分（规范形捷径 + 符号等价兜底），并把 4.4.4 的对照表扩展成 pytest 参数化用例，仿照 [tests/test_ch03_advanced_parser.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03_advanced_parser.py) 的 `skipif` 模式处理后端可选依赖。
- **延伸阅读**：[ch03/03_advanced-parser/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/README.md) 与 [compare_with_current_parser.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/03_advanced-parser/compare_with_current_parser.ipynb)（官方对比实验），以及 issue #133 的讨论串（源码文件头 [parser.py:L1-L9](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/bonus/parser.py#L1-L9) 附有链接）。
