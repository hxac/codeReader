# u4-l3 SymPy 数学等价性判定

## 1. 本讲目标

上一讲（u4-l2）我们解决了「从模型输出里**提取**答案」和「把文本**清洗**成统一写法」两件事。本讲补上评测链路的最后一环：**判分**——把模型给出的答案文本与标准答案文本放在一起，回答「它们在数学上是否等价」。

学完本讲，你应该能够：

1. 解释 `sympy_parser` 如何把一段文本安全地转成 SymPy 符号表达式，包括隐式乘法转换、超长输入保护和八类异常兜底。
2. 理解 `equality_check` 的两级判定策略：先走字符串全等捷径，再用 `simplify(a - b) == 0` 做符号等价判定。
3. 说明 `split_into_parts` 为什么存在、它如何把 `(1, 2)` 这类多元答案拆成可逐项比较的片段，以及它的天真切分会留下哪些盲区。
4. 把以上三者组装成 `grade_answer` 验证器，并用 `run_demos_table` 对它做系统体检，定位一条判错样例失败在 normalize、parse 还是 simplify 环节。

本讲所有函数都**不需要下载模型权重**、不需要 GPU，在 u1-l2 搭好的环境里用纯 CPU 即可运行（注意：`reasoning_from_scratch/ch03.py` 顶部 `import torch`，所以仍需完整安装环境，只是不必加载权重）。

## 2. 前置知识

### 2.1 符号计算系统（CAS）与 SymPy

我们平时用 Python 算 `0.1 + 0.2` 得到 `0.30000000000000004`，这是**浮点近似**。而符号计算系统（Computer Algebra System，CAS）不一样：它把 `1/3` 当作一个精确的有理数对象，把 `x` 当作一个抽象符号，做的是「代数」而不是「近似算术」。

SymPy 是 Python 生态里最常用的符号计算库。本讲要回答的核心问题——「`\frac{\sqrt{8}}{2}` 和 `sqrt(2)` 是不是同一个数」——只有符号系统能精确回答：浮点系统只能说「差值小于某个阈值」，而符号系统能证明差值**恒等于零**。

### 2.2 「解析（parse）」是什么

一段文本如 `"2x + 3x"` 只是字符序列，计算机并不认识。**解析器（parser）**的工作是按语法规则把字符序列翻译成内存中的结构化对象（表达式树）。SymPy 提供 `sympy.parsing.sympy_parser.parse_expr`，它大致做三件事：

1. 把字符串切成 token（词法分析）；
2. 按语法组合成 Python 表达式；
3. 在 SymPy 命名空间里求值，得到 SymPy 对象（如 `sp.Integer(5)`、`sp.Rational(14, 3)`、符号 `x` 等）。

### 2.3 隐式乘法

数学书写里 `2x` 表示 `2×x`、`3(1+2)` 表示 `3×(1+2)`，但 Python 语法不允许省略乘号。SymPy 解析器有一个可选的 `implicit_multiplication_application` 转换，会在解析时自动补上省略的乘号。模型输出的数学文本几乎总是省略乘号的，所以这个转换对本项目必不可少。

### 2.4 符号等价与 simplify

判断两个表达式 \( a \) 与 \( b \) 是否数学等价，经典做法是检验

\[
a \equiv b \iff a - b \equiv 0
\]

即把两者相减，若差能**化简**成常数 0，则等价。SymPy 的 `simplify` 函数会尝试代数化简（约分、合并同类项、根式有理化等）。例如

\[
\frac{\sqrt{8}}{2} - \sqrt{2} = \frac{2\sqrt{2}}{2} - \sqrt{2} = 0
\]

要注意：表达式等价性判定在数学上是不可判定问题（理查森定理），`simplify` 是「尽力而为」——差不是 0 可能只是没化简到位。本项目接受这种近似，换取实现简单。

### 2.5 多元答案

MATH 数据集里有不少答案是坐标、解集等多元素形式，如 `(1, 2)`、`[x, y, z]`。两个元组等价应当是**逐项**等价且**顺序敏感**：`(1, 2)` 与 `(2, 1)` 不等价。这就要求判分器先把整串拆成对齐的片段，再逐项判等——这是 `split_into_parts` 存在的理由。

## 3. 本讲源码地图

| 文件 | 位置 | 作用 |
| --- | --- | --- |
| `reasoning_from_scratch/ch03.py` | L296-L317 | `sympy_parser`：文本 → SymPy 表达式（隐式乘法 + 异常兜底） |
| `reasoning_from_scratch/ch03.py` | L320-L336 | `equality_check`：字符串捷径 + `simplify` 差值为零 |
| `reasoning_from_scratch/ch03.py` | L339-L357 | `split_into_parts`：把元组/列表样式的答案拆成片段 |
| `reasoning_from_scratch/ch03.py` | L360-L381 | `grade_answer`：组装以上三者，输出 True/False |
| `reasoning_from_scratch/ch03.py` | L384-L410 | `run_demos_table`：判分演示驱动器（本讲主实践的载体） |
| `reasoning_from_scratch/ch03.py` | L208-L293 | `normalize_text`：u4-l2 已精读，本讲作为上游输入，只在定位失败时回头查看个别规则 |
| `tests/test_ch03.py` | L211-L343 | 与本讲一一对应的五个测试：`test_sympy_parser`、`test_equality_check`、`test_split_into_parts`、`test_grade_answer`、`test_run_demos_table_pass_fail_labels` |
| `ch03/01_main-chapter-code/ch03_main.ipynb` | — | 第 3 章主 notebook，逐函数演示了本章开发过程（阅读材料） |
| `ch03/02_math500-verifier-scripts/evaluate_math500.py` | L13-L18 | 独立评测脚本，从包里导入这些函数跑完整 MATH-500（下游消费者，下一讲主角） |

## 4. 核心概念与源码讲解

### 4.1 sympy_parser：把文本安全地变成符号表达式

#### 4.1.1 概念说明

`sympy_parser` 是验证器与 SymPy 之间唯一的桥梁。它要解决的问题是：**模型输出是任意的、不可信的文本**——可能是合法数学、可能是 `"??"`、可能是数千字符的垃圾（第 6 章训练早期的模型尤其如此），而 `parse_expr` 遇到非法输入会直接抛异常。如果不在这一层挡住异常，一次评测循环跑 500 道题，任何一条坏输出都会让整个评测崩溃。

它的设计契约是：**永不抛异常、永不卡死**——成功返回 SymPy 对象，失败一律返回 `None`，把「解析失败」这个信息编码进返回值，交给上层统一处理。

#### 4.1.2 核心流程

```
sympy_parser(expr):
    1. 入口护栏：expr 为 None 或长度 > 2000 → 返回 None
    2. parse_expr(expr,
         transformations = standard_transformations        # 括号、指数等标准语法
                         + implicit_multiplication_application,  # "2x" → "2*x"
         evaluate=True)                                    # 解析期就求值，2+3 → 5
    3. 若抛出八类已知异常中的任何一种 → 返回 None
```

「解析期求值」值得注意：`evaluate=True` 让 `2+3` 在解析完成时就变成 `Integer(5)`，而不是保留 `Add(2, 3)` 的未求值形态，后续 `simplify` 的负担更小。

#### 4.1.3 源码精读

先看模块顶部的导入，本项目从 SymPy 借用了什么（[reasoning_from_scratch/ch03.py:11-15](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L11-L15)）：`simplify` 用于等价判定；`spp`（SymPy 官方解析器）用于文本转表达式；`SympifyError`、`PolynomialError`、`TokenError` 是要捕获的三种「远房」异常——前两个来自 SymPy 内部，最后一个来自 Python 标准库 `tokenize`，说明解析失败可能发生在很深的层次。

主体函数（[reasoning_from_scratch/ch03.py:296-317](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L296-L317)）：

```python
def sympy_parser(expr):
    # To avoid crashing on long garbage responses
    # that some badly trained models (chapter 6) may emit
    if expr is None or len(expr) > 2000:
        return None
    try:
        return spp.parse_expr(
            expr,
            transformations=(
                *spp.standard_transformations,
                spp.implicit_multiplication_application,
            ),
            evaluate=True,
        )
    except (SympifyError, SyntaxError, TypeError, AttributeError,
            IndexError, TokenError, ValueError, PolynomialError):
        return None
```

四个关键点：

- **入口护栏**（L298-L300）：`None` 与超长串直接出局。注释明说是防第 6 章「训练不佳的模型」喷出的超长垃圾——解析超长串可能极慢，而垃圾内容反正不可能与正确答案等价，提前返回 `None` 是体检与效率的双重考虑。
- **标准变换 + 隐式乘法**（L304-L310）：`standard_transformations` 处理括号、指数记法等常规语法；追加的 `implicit_multiplication_application` 让 `"2x + 3x"`、`"3(1+2)"` 这类数学惯用写法变得可解析。
- **解析期求值**（L312-L314）：`evaluate=True`，简单常量在解析时就化简（注释里的例子：`2+3 -> 5`）。
- **八类异常兜底**（L315-L317）：`SympifyError`、`SyntaxError`、`TypeError`、`AttributeError`、`IndexError`、`TokenError`、`ValueError`、`PolynomialError`——覆盖了词法错误、语法错误、类型错误等多个失败层次，全部收敛为 `None`。

对应测试（[tests/test_ch03.py:211-241](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L211-L241)）给了 8 个成功用例和 5 个失败用例。成功用例印证隐式乘法的效果：

```python
success_cases = [
    ("2+3", sp.Integer(5)),
    ("14/3", sp.Rational(14, 3)),
    ("sqrt(8)/2", sp.sqrt(8) / 2),
    ("2*x + 3*x", 5*x),
    ("2x + 3x", 5*x),          # 隐式乘法：省略乘号也能解析
    ("3(1+2)", sp.Integer(9)), # 数字后跟括号 = 乘法
    ("x(1+y)", x*(1+y)),
    ("pi*x", sp.pi*x),
]
```

失败用例则覆盖护栏与兜底（[tests/test_ch03.py:232-241](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L232-L241)）：括号不配平的 `"sqrt("`、非法记号 `"??"`、不完整运算符 `"2**"`、`None` 本身、以及 2001 个字符组成的超长串，全部断言返回 `None`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认隐式乘法转换与两类护栏的行为。
2. **操作步骤**：在项目根目录（u1-l2 搭好的环境）运行以下示例代码：

   ```python
   # 示例代码：grader_step1.py
   from reasoning_from_scratch.ch03 import sympy_parser

   print(repr(sympy_parser("2x + 3x")))    # 隐式乘法生效
   print(repr(sympy_parser("3(1+2)")))     # 数字×括号
   print(repr(sympy_parser("sqrt(")))      # 语法错误 → 兜底
   print(repr(sympy_parser("??")))         # 非法记号 → 兜底
   print(repr(sympy_parser(None)))         # 入口护栏
   print(repr(sympy_parser("1" * 2001)))   # 超长护栏
   ```

3. **需要观察的现象**：前两行输出 SymPy 对象（形如 `5*x`、`9`），后四行全部是 `None`，且**没有抛出任何异常**。
4. **预期结果**：`5*x`、`9`、`None`、`None`、`None`、`None`。前六个行为都有 `tests/test_ch03.py` 的断言背书，可放心对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么对长度超过 2000 的输入直接返回 `None`，而不是交给 `parse_expr` 去失败？

**答案**：源码注释指出这是防第 6 章训练不佳模型喷出的超长垃圾响应。超长字符串解析可能极慢（甚至构造出巨大的表达式树），拖垮评测循环；而垃圾内容无论如何不可能与标准答案等价，提前返回 `None` 让上层判 `False`，既是保护也是捷径。

**练习 2**：`implicit_multiplication_application` 对 `"3(1+2)"` 做了什么？结果是多少？

**答案**：它在解析时补上省略的乘号，把 `3(1+2)` 解释为 `3*(1+2)`；又因 `evaluate=True`，解析结果直接是 `Integer(9)`。`tests/test_ch03.py` L221 断言了这一行为。

**练习 3**：`sympy_parser` 捕获了哪几类异常？为什么要兜底而不是让异常向上传播？

**答案**：`SympifyError`、`SyntaxError`、`TypeError`、`AttributeError`、`IndexError`、`TokenError`、`ValueError`、`PolynomialError` 共八类。因为验证器面对的是模型自由生成的任意文本，解析失败是**常态**而非意外；把失败编码为 `None` 返回值，让 `equality_check` 统一走 `False` 分支，保证 500 题的评测循环永不中断。

---

### 4.2 equality_check：字符串捷径 + 符号差值为零

#### 4.2.1 概念说明

有了表达式解析，判等似乎就是「两边各 parse 一下再比较」。但有两个工程问题：

1. **解析有成本**：字符串完全相同的两侧（如 MATH-500 的标准答案自己判自己）没必要走符号系统。
2. **`==` 在 SymPy 里是结构相等**：`sqrt(8)/2` 和 `sqrt(2)` 的表达式树不同，`a == b` 是 `False`，但数学上相等。必须用「做差化简为零」才能判定**语义**等价。

`equality_check` 用两级策略解决：第一级字符串全等捷径，第二级 `simplify(gtruth - pred) == 0`。

#### 4.2.2 核心流程

```
equality_check(expr_gtruth, expr_pred):
    1. 若两字符串完全相同 → True（捷径，不进符号系统）
    2. 两侧分别 sympy_parser(...)
    3. 若两侧都解析成功：
         尝试返回 simplify(gtruth - pred) == 0
         若这一步抛 SympifyError / TypeError → 落到第 4 步
    4. 返回 False
```

数学依据即第 2.4 节的等价判据：

\[
a \equiv b \iff a - b \equiv 0
\]

#### 4.2.3 源码精读

完整函数（[reasoning_from_scratch/ch03.py:320-336](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L320-L336)）：

```python
def equality_check(expr_gtruth, expr_pred):
    # First, check if the two expressions are exactly the same string
    if expr_gtruth == expr_pred:
        return True

    # Parse both expressions into SymPy objects (returns None if parsing fails)
    gtruth, pred = sympy_parser(expr_gtruth), sympy_parser(expr_pred)

    # If both expressions were parsed successfully, try symbolic comparison
    if gtruth is not None and pred is not None:
        try:
            # If the difference is 0, they are equivalent
            return simplify(gtruth - pred) == 0
        except (SympifyError, TypeError):
            pass

    return False
```

- **字符串捷径**（L321-L323）：两侧字符级相同直接 `True`。注意调用方传入的通常是 `normalize_text` 的产物，所以「normalize 后字符串相同」也命中这条捷径。
- **解析两侧**（L325-L326）：任何一个为 `None` 都进不了符号比较，最终 `False`——parse 失败即判不等。
- **符号比较**（L328-L334）：核心一行 `simplify(gtruth - pred) == 0`。异常捕获只有 `SympifyError, TypeError` 两类，比 `sympy_parser` 的八类窄得多——因为最脏的输入已经在解析层挡掉了，这里主要防「解析成功但无法做减法」的对象（下一小节会看到一个典型例子：元组）。

对应测试（[tests/test_ch03.py:244-255](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L244-L255)）：

```python
cases = [
    ("13/4.", r"(13)/(4)", True),   # 小数点后缀 vs 分数括号形式，符号等价
    ("0.5", r"(1)/(2)", True),      # 有限小数 == 精确分数 1/2
    ("14/3", "15/3", False),        # 数值不同
    ("(14/3, 2/3)", "(14/3, 4/6)", False),  # ← 关键用例，见下文
]
```

注意每个用例都先过 `normalize_text` 再进 `equality_check`（L254），这正是 `grade_answer` 的调用方式。

第 4 个用例是本讲的**转折点**：`(14/3, 2/3)` 与 `(14/3, 4/6)` 数学上逐项等价（`4/6 = 2/3`），但断言结果是 `False`！原因：这两个字符串能被 `parse_expr` 成功解析成元组对象，而**元组不支持减法运算**，`gtruth - pred` 在 `simplify` 之前就抛出 `TypeError`，被 L333 的 `except (SympifyError, TypeError): pass` 吞掉，落到 `return False`。这说明：`equality_check` 单枪匹马处理不了多元答案——这正是下一模块 `split_into_parts` 存在的直接动机。

#### 4.2.4 代码实践

1. **实践目标**：验证两级策略，并亲眼看到「元组整体判等失败」这个关键行为。
2. **操作步骤**：运行以下示例代码：

   ```python
   # 示例代码：grader_step2.py
   from reasoning_from_scratch.ch03 import equality_check, normalize_text

   # ① 字符串捷径：两侧 normalize 后完全相同
   print(equality_check("abc", "abc"))

   # ② 符号比较：0.5 与 (1)/(2)（normalize 后的 1/2）
   print(equality_check(normalize_text("0.5"), normalize_text(r"\frac{1}{2}")))

   # ③ 符号比较：差非零
   print(equality_check(normalize_text("14/3"), normalize_text("15/3")))

   # ④ 元组整体比较（tests/test_ch03.py L250 的复现）
   print(equality_check(normalize_text("(14/3, 2/3)"),
                        normalize_text("(14/3, 4/6)")))
   ```

3. **需要观察的现象**：输出依次为 `True`、`True`、`False`、`False`。特别关注 ④——明明逐项等价却得 `False`。
4. **预期结果**：四个输出与 `tests/test_ch03.py` L246-L250 的断言一一对应（①是 L322 捷径的直接体现，②对应 `"0.5" vs (1)/(2)` 用例，③④同测试用例）。④的失败为下一模块埋下伏笔。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `simplify(a - b) == 0` 而不是直接 `a == b`？

**答案**：SymPy 的 `==` 是**结构相等**（表达式树逐节点比较），`sqrt(8)/2` 与 `sqrt(2)` 树不同、`==` 为 `False`，但数学上相等。做差再化简为 0 判定的是**语义等价**，能覆盖写法不同但数值相同的情形（`tests/test_ch03.py` L278 的 `\frac{\sqrt{8}}{2}` vs `sqrt(2)` 就是靠它判 `True` 的）。

**练习 2**：`"0.3333333333"` 与 `"1/3"` 为什么判 `False`？这是 bug 吗？

**答案**：`0.3333333333` 被解析成精确的有理数 \(\frac{3333333333}{10^{10}}\)，与 \(\frac{1}{3}\) 的差是一个确定的非零有理数，`simplify` 不会把它化成 0。这不是 bug，而是**精确符号比较、无数值容差**的设计取向：验证器宁可偏严，也不引入「差多小算相等」的阈值（`tests/test_ch03.py` L284 断言该用例为 `False`）。

**练习 3**：字符串捷径为 MATH-500 的哪条回归测试提供了保底？

**答案**：`test_math500_parser_self_answers`（[tests/test_ch03.py:75-85](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L75-L85)）把 500 道题的标准答案自己判自己，断言 500 个全对。字符串全等捷径保证哪怕某题答案无法解析（如纯文字答案），自比对也走第一级返回 `True`，验证器不会「误伤自己」。

---

### 4.3 split_into_parts：多元答案的对齐比较策略

#### 4.3.1 概念说明

4.2 节末尾看到：元组作为整体进 `equality_check` 会因「不可做减法」而失败。解法是把 `(a, b)` 拆成 `["a", "b"]`，让每一项单独走 `equality_check`，再要求**全部项**判等。这就是 `split_into_parts` 的职责——它是多元答案进入符号判等之前的「对齐」工序。

拆分之后，比较语义自然变成：

- **长度对齐**：`(1, 2, 3)` 与 `(1, 2)` 片段数不同 → 直接不等；
- **顺序敏感**：`(1, 2)` 与 `(2, 1)` 逐项比较必有一项不等 → 不等；
- **逐项等价**：`(14/3, 2/3)` 与 `(14/3, 4/6)` 拆开后 `2/3` vs `4/6` 能化简为零 → 相等（整体比较时反而失败！）。

#### 4.3.2 核心流程

```
split_into_parts(text):
    1. text 为空        → 返回 []（空列表）
    2. 形状检测：长度 ≥ 2
         且首字符 ∈ "(["，尾字符 ∈ ")]"
         且中间（去掉首尾后的子串）含 ","
       → 按逗号切开、strip 每一项
       → 若所有项都非空 → 返回拆分结果
         （任何一项为空 → 放弃拆分，返回 [原文本]）
    3. 其他一切情况     → 返回 [原文本]（单元素列表）
```

注意它只做**形状判断**，不认嵌套：切分就是朴素的 `split(",")`，不追踪括号深度（对比：u4-l2 的 `get_last_boxed` 用括号深度计数处理嵌套）。

#### 4.3.3 源码精读

完整函数（[reasoning_from_scratch/ch03.py:339-357](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L339-L357)）：

```python
def split_into_parts(text):
    result = [text]

    if text:
        # Check if text looks like a tuple or list, e.g. "(a, b)" or "[a, b]"
        if (
            len(text) >= 2
            and text[0] in "([" and text[-1] in ")]"
            and "," in text[1:-1]
        ):
            # Split on commas inside brackets and strip whitespace
            items = [p.strip() for p in text[1:-1].split(",")]
            if all(items):
                result = items
    else:
        # If text is empty, return an empty list
        result = []

    return result
```

- **形状检测**（L344-L348）：三个条件缺一不可——长度至少 2、首字符是 `(` 或 `[`、尾字符是 `)` 或 `]`、中间有逗号。注意首尾括号**不必配对**：`(1, 2]` 这种畸形写法也会被拆（宽松接受）。
- **切分与 `all(items)` 守卫**（L349-L351）：按逗号切开并 `strip()`；`all(items)` 检查每项都非空——`"(a, )"` 会切出 `["a", ""]`，含空串，于是**放弃拆分**，原样返回 `["(a, )"]`，避免空片段进符号系统。
- **空文本分支**（L353-L355）：空串返回 `[]`，这个空列表会让 `grade_answer` 的非空检查直接判 `False`。
- **默认分支**：任何不像元组的文本（包括 `"(a)"` 这种无逗号括号式）都原样包成单元素列表——普通标量答案走「拆成 1 项再比较」的同一条路，代码因此不必区分标量/元组两种流程。

对应测试（[tests/test_ch03.py:258-271](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L258-L271)）：

```python
cases = [
    ("(a, b)", ["a", "b"]),      # 圆括号元组
    ("[x,y,z]", ["x", "y", "z"]),# 方括号列表，无空格也能拆
    ("( 1 ,  2 )", ["1", "2"]),  # 多余空格被 strip
    ("hello", ["hello"]),        # 普通文本 → 单元素
    ("(a)", ["(a)"]),            # 无逗号 → 不拆
    ("(a, )", ["(a, )"]),        # 尾逗号切出空串 → 放弃拆分
    ("", []),                    # 空文本 → 空列表
]
```

七个用例把三条分支与两个守卫全部钉死。

#### 4.3.4 代码实践

1. **实践目标**：跑通全部官方用例，再探两个官方未覆盖的边界（嵌套、混用括号）。
2. **操作步骤**：运行以下示例代码：

   ```python
   # 示例代码：grader_step3.py
   from reasoning_from_scratch.ch03 import split_into_parts

   cases = ["(a, b)", "[x,y,z]", "( 1 ,  2 )", "hello",
            "(a)", "(a, )", "((1,2), 3)", "(1, 2]"]
   for t in cases:
       print(f"{t!r:14} -> {split_into_parts(t)}")
   ```

3. **需要观察的现象**：前 6 个输出与上表一致；`"((1,2), 3)"` 被天真地切成两份；`"(1, 2]"` 也会被拆分（首尾括号不要求配对）。
4. **预期结果**：前 7 个行为有测试断言背书（嵌套的那个为 `['(1,2)', '3']`，混用括号的为 `['1', '2']`，二者属「按源码逻辑推演」，待本地验证）。嵌套切分不追踪括号深度，是简单验证器的已知盲区，附录的高级解析器（u10-l4）专门补强这一点。

#### 4.3.5 小练习与答案

**练习 1**：`"(a, )"` 为什么最后原样返回、没有被拆成 `["a", ""]`？

**答案**：按逗号切分得到 `["a", ""]`，`all(items)` 检查发现空串（空字符串为假值），于是放弃拆分、把原文本作为单元素列表返回。这防止空片段进入 `sympy_parser`——空串解析必然失败，拆了反而把本可整体处理的文本判死。

**练习 2**：`split_into_parts("(a)")` 和 `split_into_parts("")` 的返回值分别是什么？对 `grade_answer` 意味着什么？

**答案**：前者返回 `["(a)"]`（中间无逗号，不满足形状条件），后者返回 `[]`。空列表使 `grade_answer` 中 `gt_parts and pred_parts` 的非空检查失败，直接判 `False`——这就是「空预测一律不得分」的实现位置。

**练习 3**：为什么 `split_into_parts` 不像 `get_last_boxed` 那样用括号深度计数处理嵌套？

**答案**：两者的输入可靠度不同：`get_last_boxed` 面对的是必须精确到字符的 `\boxed{}` 提取，嵌套大括号是常态；而 `split_into_parts` 服务于 MATH-500 判分，绝大多数答案是单层元组，朴素切分已覆盖主流场景。用最低成本覆盖主流、把长尾交给附录的高级解析器（`reasoning_from_scratch/bonus/parser.py`，u10-l4 讲义），是本项目「先简后繁」的一贯取舍。

---

### 4.4 grade_answer 与 run_demos_table：组装成验证器

#### 4.4.1 概念说明

`grade_answer` 是本讲的收口：把 `normalize_text`（u4-l2）、`split_into_parts`、`equality_check`、`sympy_parser` 串成一条完整判分管线，对外只暴露一个布尔接口。它就是评测四步链路「渲染 → 生成 → 提取 → **判分**」的最后一步，被 `evaluate_math500_stream` 与独立脚本 `evaluate_math500.py` 消费。

`run_demos_table` 则是一个演示驱动器：接收 `(名称, 预测, 真值, 期望结果)` 四元组列表，逐条调用 `grade_answer` 并打印对齐的结果表——它既是第 3 章 notebook 里的教学道具，也是我们做「验证器体检」的趁手工具。

#### 4.4.2 核心流程

```
grade_answer(pred_text, gt_text):          # 注意：预测在前，真值在后
    1. 任一侧为 None → False
    2. gt_parts  = split_into_parts(normalize_text(gt_text))
       pred_parts = split_into_parts(normalize_text(pred_text))   # 两侧对称处理
    3. 两侧 parts 非空 且 数量相等，否则 False
    4. 逐对 zip，all(equality_check(gt, pred)) → 返回
```

整条判分管线的失败传播一览：

| 输入特征 | 失败环节 | 结果 |
| --- | --- | --- |
| `pred_text` 或 `gt_text` 为 `None` | 入口检查（L363-L364） | `False` |
| 空字符串 → `[]` | `split_into_parts` 空分支 | `False` |
| `"sqrt("` 这类非法片段 | `sympy_parser` 兜底 → `None` | `False` |
| 元组未拆分就进减法 | `equality_check` 的 `except TypeError` | `False`（4.2 节的关键用例） |
| `0.3333333333` vs `1/3` | `simplify` 差非零（无数值容差） | `False` |
| `50%` vs `1/2` | `normalize_text` 把 `%` 直接删除（L254） | `"50"` vs `"1/2"` → `False` |
| `(1, 2, 3)` vs `(1, 2)` | 片段数不等（3 ≠ 2） | `False` |

#### 4.4.3 源码精读

`grade_answer`（[reasoning_from_scratch/ch03.py:360-381](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L360-L381)）：

```python
def grade_answer(pred_text, gt_text):
    result = False  # Default outcome if checks fail

    # Only continue if both inputs are non-empty strings
    if pred_text is not None and gt_text is not None:
        gt_parts = split_into_parts(
            normalize_text(gt_text)
        )  # Break ground truth into comparable parts

        pred_parts = split_into_parts(
            normalize_text(pred_text)
        )  # Break prediction into comparable parts

        # Ensure both sides have same number of valid parts
        if (gt_parts and pred_parts
           and len(gt_parts) == len(pred_parts)):
            result = all(
                equality_check(gt, pred)
                for gt, pred in zip(gt_parts, pred_parts)
            )  # Check each part for mathematical equivalence

    return result  # True only if all checks passed
```

- **默认拒绝**（L361）：`result` 初始为 `False`，任何一步不满足就短路，整条管线「疑罪从无」。
- **normalize 在 split 之前、两侧对称**（L365-L371）：预测与真值走完全相同的清洗与拆分工序，保证比较公平。normalize 剥掉全部大括号（[reasoning_from_scratch/ch03.py:293](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L293)），所以 `2^{10}` 到达 split 时已是 `2**10`，不再干扰逗号检测。
- **非空 + 数量对齐**（L373-L375）：空列表（空文本）或片段数不等直接出局——`("(1, 2, 3)", "(1, 2)")` 判 `False` 就发生在这里。
- **逐项 all 判等**（L376-L379）：`zip` 对齐后逐项 `equality_check`，注意实参顺序是真值在前（与 `grade_answer` 的预测在前恰好相反，源码阅读时别混淆）。

`run_demos_table`（[reasoning_from_scratch/ch03.py:384-410](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L384-L410)）：遍历 `(name, pred, gtruth, expect)` 四元组（L387），调用 `grade_answer(pred, gtruth)`（L388），`status = "PASS" if got == expect else "FAIL"`（L389），随后按列宽对齐打印表格（L392-L406），末尾打印 `Passed x/y` 汇总（L408-L410）。它的价值在于：**期望值由人填写，FAIL 行就是验证器与人类判断不一致的地方**——这正好是找判错样例的仪器。

配套测试有三组，各司其职：

- `test_grade_answer`（[tests/test_ch03.py:274-300](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L274-L300)）：19 个端到端用例，覆盖分数/小数互认（`"0.5"` vs `"1/2"` → `True`）、元组顺序敏感（`"(2, 1)"` vs `"(1, 2)"` → `False`）、长度对齐、千分位（`"1,234/2"` vs `"617"` → `True`）、度数符号（`90^\circ` vs `90` → `True`）、空值与 `None`。
- 模块级 `tests` 列表（[tests/test_ch03.py:303-319](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L303-L319)）：15 条演示用例，与 `test_grade_answer` 的前 15 条一致——notebook 与测试共享同一组样例，本讲主实践直接复用它。
- `test_math500_parser_boxed_constant_mismatch`（[tests/test_ch03.py:103-115](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L103-L115)）：把 500 题的预测全部换成常数 `\boxed{123}`，断言 `correct == 0`——验证器必须一个都不误判，这是防「全对假阳性」的回归锁。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用 `run_demos_table` 跑通自带 15 条演示，再追加至少 10 个自选用例，从中找出至少一个**判错样例**（语义等价却被判 `False`，或反之），并定位它失败在 normalize、parse 还是 simplify 环节。

**操作步骤**：

1. 在项目根目录创建 `grader_playground.py`（示例代码，放仓库根目录或任意临时目录均可，勿放进 `reasoning_from_scratch/` 包内）：

   ```python
   # 示例代码：grader_playground.py
   from reasoning_from_scratch.ch03 import run_demos_table

   # 第一批：tests/test_ch03.py L303-L319 的自带 15 条（原样复制）
   tests = [
       ("check_1", "3/4", r"\frac{3}{4}", True),
       ("check_2", "(3)/(4)", r"3/4", True),
       ("check_3", r"\frac{\sqrt{8}}{2}", "sqrt(2)", True),
       ("check_4", r"\( \frac{1}{2} + \frac{1}{6} \)", "2/3", True),
       ("check_5", "(1, 2)", r"(1,2)", True),
       ("check_6", "(2, 1)", "(1, 2)", False),
       ("check_7", "(1, 2, 3)", "(1, 2)", False),
       ("check_8", "0.5", "1/2", True),
       ("check_9", "0.3333333333", "1/3", False),
       ("check_10", "1,234/2", "617", True),
       ("check_11", r"\text{2/3}", "2/3", True),
       ("check_12", "50%", "1/2", False),
       ("check_13", r"2\cdot 3/4", "3/2", True),
       ("check_14", r"90^\circ", "90", True),
       ("check_15", r"\left(\frac{3}{4}\right)", "3/4", True),
   ]

   # 第二批：自选追加用例（名称, 预测, 真值, 期望的语义对错）
   tests += [
       ("m01", r"\frac{1}{2}", "0.5", True),        # 分数 vs 小数
       ("m02", r"\sqrt{4}", "2", True),             # 根式可开尽
       ("m03", "(1,2)", "[1, 2]", True),            # 元组 vs 列表
       ("m04", "2^{10}", "1024", True),             # 幂的 LaTeX 记法
       ("m05", "50%", r"\frac{50}{100}", True),     # 百分比语义（暗坑）
       ("m06", "0.3333333333", "1/3", True),        # 无限小数截断（暗坑）
       ("m07", r"1\frac{1}{2}", "1.5", True),       # LaTeX 混合数字（暗坑）
       ("m08", "((1, 2), 3)", "((1,2),3)", True),   # 嵌套元组（暗坑）
       ("m09", "(2, 1)", "(1, 2)", False),          # 顺序敏感，应正确拒绝
       ("m10", "2x+3x", "5x", True),                # 隐式乘法 + 符号合并
       ("m11", r"\pi", "pi", True),                 # 希腊字母（暗坑）
       ("m12", "6/4", "1.5", True),                 # 未约分分数 vs 小数
   ]

   run_demos_table(tests)
   ```

2. 运行 `python grader_playground.py`（或 `uv run python grader_playground.py`）。
3. 在输出表中找出 `Status` 为 `FAIL` 的行——每行都是「人类认为该对/该错，验证器不这么认为」的判错样例。
4. 对任一 FAIL 行做**环节定位**：在 Python 里逐层打印中间产物（示例代码）：

   ```python
   # 示例代码：定位失败环节
   from reasoning_from_scratch.ch03 import normalize_text, sympy_parser, split_into_parts

   pred, gt = "50%", r"\frac{50}{100}"
   p, g = normalize_text(pred), normalize_text(gt)
   print("normalize 后:", repr(p), repr(g))            # ① normalize 环节
   print("split 后:", split_into_parts(p), split_into_parts(g))  # ② split 环节
   print("parse 后:", sympy_parser(p), sympy_parser(g))          # ③ parse 环节
   # ①②③ 都正常，才需要怀疑 ④ simplify（差非零 / 异常兜底）
   ```

**需要观察的现象**：

- 前 15 条 `check_*` 应全部 `PASS`（它们与 `test_grade_answer` 断言通过的用例一致）。
- 追加用例中 `m01`–`m04`、`m09`、`m10`、`m12` 预计 `PASS`（`m01`–`m04` 的依据分别是 check_8 同款符号比较、`evaluate=True` 解析期开根、`split_into_parts` 测试中 `[x,y,z]` 证明方括号可拆、normalize 的 `^`→`**`（L279）加剥大括号（L293）；`m09` 同 check_6；`m10` 依据 `test_sympy_parser` 的 `"2x + 3x"` 用例；`m12` 同 check_8 模式）。
- `m05`、`m06` 预计 `FAIL`：`m05` 中 normalize 把 `%` 直接删除（[reasoning_from_scratch/ch03.py:254](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L254)），`"50"` 与 `"1/2"` 比较必然不等——**失败环节：normalize**（信息丢失而非换算）；`m06` 两个精确有理数差非零——**失败环节：simplify/精确比较**（设计取向，非 bug）。这两个行为分别与 `check_12`、`check_9` 的断言同源，可信度高。
- `m07`、`m08`、`m11` 预计 `FAIL`（按源码逻辑推演，**待本地验证**）：`m07` 的 normalize 混合数字规则（[reasoning_from_scratch/ch03.py:280-284](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L280-L284)）只匹配「数字 + 空格 + 分数」，`1\frac{1}{2}` 清洗后是 `1(1)/(2)`，被隐式乘法解释成 \(1 \times \frac{1}{2} = 0.5\)——**失败环节：normalize**；`m08` 被天真切分成 `["(1, 2)", "3"]` 后，第一项是元组、不可做减法（4.2 节同款 `TypeError` 兜底）——**失败环节：split + parse**；`m11` 的 `\pi` 没有任何 normalize 规则处理，反斜杠进入 parse 即失败——**失败环节：normalize → parse**。

**预期结果**：输出一张 27 行的对齐表格与 `Passed N/27` 汇总；`check_*` 15 条全 `PASS`；追加用例中出现 2–5 个 `FAIL`，其中 `m05`（normalize 丢 `%` 语义）与 `m06`（无容差精确比较）是最稳的两个判错样例。任何一条 `FAIL` 都请用上面的三步打印法确认失败环节，再与本小节的分析对照——**若本地结果与此处推演不符，以本地为准，并回头检查是哪一步假设错了**，这本身就是最好的源码精读练习。

#### 4.4.5 小练习与答案

**练习 1**：`grade_answer` 与 `equality_check` 的参数顺序有什么容易踩的差别？

**答案**：`grade_answer(pred_text, gt_text)` **预测在前**（[reasoning_from_scratch/ch03.py:360](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L360)）；而它内部调用 `equality_check(gt, pred)` 时**真值在前**（[reasoning_from_scratch/ch03.py:320](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L320)）。两个函数恰好相反，写测试或复用时传反了虽然不影响对称比较的结果，但会让阅读者困惑。

**练习 2**：`grade_answer("(1, 2, 3)", "(1, 2)")` 在哪一行返回 `False`？为什么不用逐项比较？

**答案**：在长度对齐检查（L373-L375）处短路：`gt_parts` 有 3 项、`pred_parts` 只有 2 项，`len` 不等直接 `False`，连一次 `equality_check` 都不会执行。多元答案的相等定义要求元素一一对应，长度不同的两个元组没有「部分相等」可言。

**练习 3**：`test_math500_parser_boxed_constant_mismatch` 为什么断言 `correct == 0` 而不是 `== 500`？

**答案**：该测试把 500 题的预测全部换成常数 `\boxed{123}`（[tests/test_ch03.py:111](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L111)）。MATH-500 没有一题的正确答案是 123，所以验证器必须 500 题全判错。这锁死的是**假阳性**方向：如果哪天改动让「随便什么预测都能判对」（比如字符串捷径被误放宽），这条测试会立刻爆红。与 `test_math500_parser_self_answers`（全对）一正一反，把验证器的两个失误方向都钉住。

---

## 5. 综合实践：给验证器做一次「体检报告」

把本讲四个模块串成一个系统性任务——**产出一页验证器体检报告**：

1. **建用例库**（≥15 条，可在 4.4.4 的 27 条基础上扩充）：按四类分组——数值等价（分数/小数/幂/根式）、结构等价（元组/列表/嵌套）、语义陷阱（百分比、截断小数、混合数字、希腊字母）、应拒绝样例（顺序颠倒、长度不等、`None`、空串）。
2. **跑批**：全部过一遍 `run_demos_table`，记录每行 `Expect / Got / Status`。
3. **定位**：对每个 `FAIL` 行用「normalize → split → parse → simplify」四步打印法（4.4.4 步骤 4）标注失败环节，填入下表：

   | 用例 | 期望 | 实得 | 失败环节 | 一句话原因 |
   | --- | --- | --- | --- | --- |
   | `50%` vs `\frac{50}{100}` | True | False（待本地验证） | normalize | `%` 被删除而非换算 |
   | `…` | … | … | … | … |

4. **结论**：统计四类环节的失败占比，回答一个问题——**这个验证器的误判主要来自哪一层？如果只允许你修一处，修哪里性价比最高？**（提示：normalize 是唯一「信息单向流失」的环节，parse/simplify 的失败多数是 normalize 残留的非常规记法导致的。）
5. **延伸对照**（可选）：把几个 normalize 环节失败的样例拿去和附录高级解析器 `reasoning_from_scratch/bonus/parser.py` 的处理思路预想对照（不必读通，u10-l4 会精读），验证你对其设计动机的猜测。

## 6. 本讲小结

- `sympy_parser` 是验证器与 SymPy 之间唯一桥梁：`None`/超长（>2000 字符）双护栏 + 八类异常兜底，把「解析失败」编码为 `None` 返回值，保证评测循环永不中断；隐式乘法转换让 `2x`、`3(1+2)` 这类数学惯用写法可解析。
- `equality_check` 两级判定：字符串全等捷径（自比对保底）在前，`simplify(gtruth - pred) == 0` 的符号差值为零判定在后；元组对象不可做减法，整体比较必失败——这是拆分工序存在的直接原因。
- `split_into_parts` 按形状（首 `([`、尾 `)]`、中间有逗号）把多元答案拆成对齐片段，`all(items)` 守卫拦截空片段；它不感知嵌套，朴素 `split(",")` 是已知盲区。
- `grade_answer` 以「默认拒绝」组装全链路：normalize → split → 非空与长度对齐 → 逐项 all 判等，任何一环不满足即 `False`；预测与真值走完全对称的清洗工序。
- 该验证器是**精确符号比较、无数值容差**的设计取向：`0.3333333333 ≠ 1/3`、`50% ≠ 1/2`（`%` 直接删除）都是「偏严」的表现；一正一反两组 500 题回归测试（自比对全对 / 常数预测全错）锁死了两个失误方向。
- `run_demos_table` 用「人填期望、机器出实得」的方式把判错样例直接暴露在 `FAIL` 行里，是验证器体检的仪器。

## 7. 下一步学习建议

本讲完成了评测四步链路「渲染 → 生成 → 提取 → **判分**」的最后一环。下一讲 **u4-l4 端到端 MATH-500 评测流程**将把四步真正串起来：用 `evaluate_math500_stream` 跑一个真实子集，观察 jsonl 落盘、ETA 进度与准确率统计，并对比流式与批处理（`evaluate_math500_batched.py`）两种评测脚本的差异。

如果你想在判分这条线上走得更深：

- 精读 `tests/test_ch03.py` 的 L75-L115 三条 500 题回归测试，体会「正反两个方向锁死验证器」的测试设计。
- 预习 `reasoning_from_scratch/bonus/parser.py`（u10-l4 的主角）：它用区间/矩阵/集合/± 等语法分支分发替代朴素切分，正是本讲 `split_into_parts` 盲区的系统化解法。
- 思考一个改进实验：把 `equality_check` 里的 `simplify(a - b) == 0` 换成 SymPy 的 `equals()`（随机数值点检验）或先 `together`/`radsimp` 再化简，用第 5 节的体检报告量化召回率变化——注意这是源码之外的实验，请以「复制一份函数再改」的方式进行，不要改动 `reasoning_from_scratch/` 包本身。
