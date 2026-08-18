# 输出解析工具：utils.py 的 boxed 提取与章节切分

## 1. 本讲目标

学完本讲，你应该能够：

1. **手推** `extract_boxed_answers` 的括号计数算法：给定任意一段含嵌套花括号的 \(\boxed{\cdots}\) 文本，能在纸上逐字符走一遍，写出函数的返回值。
2. **解释** `_normalize_prover_output` 为什么存在、它把哪些写法（如 `* Solution *`、`**Self Evaluation**`）统一成 `## Solution` / `## Self Evaluation`，以及 `extract_solution` / `extract_self_eval` 如何靠这两个标题把模型输出切成两段。
3. **说明** `hash_problem_idx` 如何用 SHA-256 为没有 `problem_idx` 字段的题目生成稳定主键，以及 `read_data` 如何屏蔽 `.json` 与 `.jsonl` 两种格式的差别。
4. **会写**一份不修改任何源码的 `test_utils.py` 单元测试，把以上行为全部固化成断言。

本讲是 u3-l1 的镜像：u3-l1 讲的是「提示词如何**要求**模型输出」，本讲讲的是「代码如何**兑现**这份要求」。两者合起来才是完整的格式契约。

## 2. 前置知识

### 2.1 为什么需要「输出解析器」

大模型的输出是一段自由文本，而流水线需要的是结构化字段：这条证明的正文是什么？模型给自己打了几分？验证器给这条证明打了几分？把这些信息从自由文本里可靠地「抠」出来，就是输出解析器的职责。

本项目的解析策略非常朴素，只有两条：

- **约定标记**：模板（u3-l1）要求所有分数写进 \(\boxed{\cdots}\)、证明输出必须分 `## Solution` 和 `## Self Evaluation` 两个小节。
- **按标记切分**：解析器只认这些标记，标记不在就抛异常，由调用方决定丢弃还是降级。

### 2.2 括号配对计数（深度计数器）

\(\boxed{\frac{1}{2}}\) 的内容里**还有**花括号，所以不能「找到第一个 `}` 就停」。经典做法是用一个整数计数器 \(n\) 表示当前未闭合的 `{` 个数：

- 遇到 `{`：\(n \leftarrow n + 1\)
- 遇到 `}`：\(n \leftarrow n - 1\)；当 \(n < 0\) 时，这个 `}` 正好与最外层那一个配对，截断。

这等价于栈的进出，但只需要一个整数，空间 O(1)、时间 O(文本长度)。

### 2.3 正则表达式基础

本讲会用到这些正则语法（`regex.sub` / `regex.split` 与标准库 `re` 同名函数用法一致）：

| 语法 | 含义 |
| --- | --- |
| `\n` | 换行符 |
| `\s` | 任意空白字符（含空格、换行） |
| `\*` | 字面星号 `*`（`*` 本身是量词，须转义） |
| `\*+` | 一个或多个星号 |
| `(^|\n)` | 文本开头**或**换行符之后（一个分组） |
| `r"..."` | Python 原始字符串，避免反斜杠被 Python 先吃掉 |

注意：`utils.py` 顶部 `import regex` 用的是**第三方库 `regex`**（`pip install regex`），不是内置的 `re`。本讲用到的模式两者行为相同，但复现实验前要先装这个包。

### 2.4 SHA-256 与「稳定主键」

`hashlib.sha256(text.encode()).hexdigest()` 把任意文本映射成 64 个十六进制字符。它有两个关键性质：

- **确定性**：同一文本永远得到同一哈希值——不需要任何中心化的编号分配器。
- **雪崩效应**：文本哪怕改一个空格，哈希值也完全不同。

这两点使它适合当文件名级别的「主键」。

### 2.5 `.json` 与 `.jsonl` 回顾

u1-l2 讲过：`.json` 是整份 JSON 数组，一次 `json.load` 读完；`.jsonl` 每行一个独立 JSON 对象，逐行 `json.loads`。`read_data` 就是这两条读取路径的统一入口。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [inference/utils.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py) | 输出解析工具箱，全文件仅 50 行 | 全部 5 个函数逐一精读 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | 流水线主编排 | 只看「谁在调用 utils」：第 83-92 行的解析兜底、第 171-174 / 359-363 行的主键回退 |
| [inputs/CMO2024.json](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/CMO2024.json) | 竞赛题输入数据 | 综合实践中作为 `read_data` / `hash_problem_idx` 的真实测试数据 |

`utils.py` 的 5 个函数与它们在 `main.py` 中的消费点对照表（先混个脸熟，细节见第 4 节）：

| utils.py 函数 | 行号 | main.py 中的调用点 | 用途 |
| --- | --- | --- | --- |
| `hash_problem_idx` | L5-L6 | L174、L362 | 无 `problem_idx` 字段时生成证明池文件名主键 |
| `read_data` | L8-L17 | L68、L120 | 读各阶段的 input.jsonl |
| `extract_boxed_answers` | L19-L34 | L88、L128、L307、L330 | 从自评/验证输出中抠出 \(\boxed{\cdots}\) 分数 |
| `_normalize_prover_output` | L36-L42 | （被下面两个函数调用） | 标题写法归一化 |
| `extract_solution` / `extract_self_eval` | L44-L50 | L85-L86 | 把证明输出切成「解答正文」与「自我评价」 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`extract_boxed_answers` —— 括号计数提取 \(\boxed{\cdots}\)

#### 4.1.1 概念说明

u3-l1 的评分契约规定：生成器、验证器、元验证器的最终结论（分数）都必须写进 \(\boxed{\cdots}\)。于是「拿分数」这个需求就统一收敛为「提取文本中所有 \(\boxed{\cdots}\) 的内容」。

这件事的难点在**嵌套**：\(\boxed{\frac{1}{2}}\) 的内容 `\frac{1}{2}` 自带一对花括号，\(\boxed{x^{2}}\) 同理，而且嵌套深度任意。一次性的朴素正则（比如 `boxed\{(.+?)\}`）在 `\frac{1}{2}` 的第一个 `}` 处就会提前截断，得到错误的 `\frac{1`。所以这里改用显式的括号计数扫描。

#### 4.1.2 核心流程

算法分两步：

```text
第一步：text.split('boxed{') 把文本按每个 boxed{ 出现的位置切块，
        丢掉第一块（它位于首个 boxed{ 之前，不含答案），剩下的每一块
        恰好以「某个 boxed 的内容」开头。
第二步：对每一块做深度计数扫描：
        n = 0
        从头逐字符走：
            '{' → n += 1
            '}' → n -= 1；若 n < 0，说明这个 '}' 与开头 boxed{ 的 '{' 配对：
                  若紧随其后是 '%' → 答案取 piece[:i+1]（保留这个 '}'）
                  否则             → 答案取 piece[:i]（不含 '}'）
                  立即 break（后面的内容属于 boxed 之外的正文，与本答案无关）
        若扫到结尾 n 始终 ≥ 0，说明这个 boxed 没有闭合 → 什么都不追加
```

手推一遍。设输入文本为（Python 原始字符串）：

```python
text = r"所以答案是 \boxed{\frac{1}{2}}，完毕"
```

`text.split('boxed{')` 得到两块：`['所以答案是 \\', '\\frac{1}{2}}，完毕']`，取 `[1:]` 后只剩一块 `piece = r'\frac{1}{2}}，完毕'`。对这块逐字符计数：

| i | 字符 | 动作 | n |
| --- | --- | --- | --- |
| 0-4 | `\ f r a c` | 无事 | 0 |
| 5 | `{` | n+1 | 1 |
| 6 | `1` | 无事 | 1 |
| 7 | `}` | n-1 | 0 |
| 8 | `2` | 无事 | 0 |
| 9 | `}` | n-1 → **n=-1 < 0** | -1 |

i=9 触发截断，`piece[10]` 是 `，`不是 `%`，答案取 `piece[:9]`，即 `\frac{1}{2}` ——正好是 boxed 的完整内容，嵌套括号完好无损。

再快速手推两个边界：

- `r"\boxed{x^{2}}"` → piece = `x^{2}}`：i=2 `{`（n=1）、i=4 `}`（n=0）、i=5 `}`（n=-1）→ 答案 `x^{2}`。
- `r"\boxed{x^{2}"`（漏写最外层右括号）→ piece = `x^{2}`：i=2 `{`（n=1）、i=4 `}`（n=0），扫完 n 仍为 0 → **不追加任何答案**。未闭合的 boxed 会被静默丢弃。

#### 4.1.3 源码精读

完整实现（[inference/utils.py:L19-L34](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L19-L34)）——用 `split('boxed{')` 定位、用计数器配对括号，逐 boxed 提取内容：

```python
def extract_boxed_answers(text):
    answers = []
    for piece in text.split('boxed{')[1:]:
        n = 0
        for i in range(len(piece)):
            if piece[i] == '{':
                n += 1
            elif piece[i] == '}':
                n -= 1
                if n < 0:
                    if i + 1 < len(piece) and piece[i + 1] == '%':
                        answers.append(piece[: i + 1])
                    else:
                        answers.append(piece[:i])
                    break
    return answers
```

四个值得精读的细节：

1. **`[1:]` 的含义**（[L21](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L21)）：`split` 的结果第一块是首个 `boxed{` **之前**的文本。含 0 个 boxed 时整个列表只有一块，`[1:]` 恰好得到空列表，函数返回 `[]`——这是「没有 boxed」的自然兜底。
2. **`break` 的含义**（[L33](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L33)）：找到配对的 `}` 后立刻停止，piece 剩余部分（boxed 之后的正文，可能还含有别的花括号）与本答案无关。
3. **百分号分支**（[L29-L30](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L29-L30)）：若紧随 `}` 之后的是 `%`（即文本形如 `\boxed{50}%`），答案取 `piece[:i+1]`——**保留闭合花括号**，得到 `50}`；而普通情况取 `piece[:i]`，花括号不进答案。注意 `if i + 1 < len(piece)` 这个越界保护：`}` 若是整段最后一个字符，直接走 else 分支。这个「保留 `}`」的设计意图推测与百分比形态的答案有关，具体动机待确认——做实验时把它当**既定行为**固化进测试即可。
4. **分隔符不带反斜杠**：`split('boxed{')` 匹配的是子串 `boxed{`，`\boxed{` 包含它所以正常命中；但文本里若出现 `myboxed{...}` 这类子串也会被误命中——解析器完全不设防，这是自由文本解析的固有脆弱性。

下游消费方式以自评分为例（[inference/main.py:L88](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L88)）——取**最后一个**非空 boxed 并转 float：

```python
self_eval_score = float([s.strip() for s in extract_boxed_answers(self_eval) if s.strip()][-1])
```

两个信息量：`if s.strip()` 过滤掉空 boxed（`\boxed{}` 会产出空串答案）；`[-1]` 取最后一个，呼应模板「Self Evaluation 的**最终**结论写进 boxed」的约定。验证器评分的提取（[inference/main.py:L128](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L128)）也是同一套调用。

#### 4.1.4 代码实践

**实践目标**：不运行代码先预测，再运行验证，确认你真的掌握了括号计数算法。

**操作步骤**：

1. 确认依赖：`pip install regex`（`import utils` 时顶部 `import regex` 会用到）。
2. 在仓库根目录创建 `box_trace.py`（示例代码）：

   ```python
   import sys
   sys.path.insert(0, "inference")
   from utils import extract_boxed_answers

   cases = [
       r"\boxed{\frac{1}{2}}",            # 嵌套一层
       r"\boxed{x^{2}}",                  # 嵌套一层
       r"\boxed{\dfrac{a}{b}} + \boxed{3}",# 两个 boxed
       r"\boxed{50}%",                    # 百分号紧跟 '}'
       r"\boxed{}",                       # 空 boxed
       r"\boxed{x^{2}",                   # 未闭合
       "no box at all",                   # 没有 boxed
       r"myboxed{a}",                     # 不带反斜杠的子串
   ]
   for c in cases:
       print(repr(c), "->", extract_boxed_answers(c))
   ```

3. 先在纸上写出你预测的 8 行输出，再运行 `python box_trace.py` 对答案。

**需要观察的现象**：嵌套括号是否完整保留；两个 boxed 是否都提取且顺序与出现顺序一致；百分号分支的答案是否带着 `}`；未闭合与无 boxed 是否返回空列表。

**预期结果**（由 4.1.2 的算法手推可得）：

| 输入 | 输出 |
| --- | --- |
| `\boxed{\frac{1}{2}}` | `['\\frac{1}{2}']` |
| `\boxed{x^{2}}` | `['x^{2}']` |
| `\boxed{\dfrac{a}{b}} + \boxed{3}` | `['\\dfrac{a}{b}', '3']` |
| `\boxed{50}%` | `['50}']` |
| `\boxed{}` | `['']` |
| `\boxed{x^{2}` | `[]` |
| `no box at all` | `[]` |
| `myboxed{a}` | `['a']`（子串误命中） |

运行环境差异（Python 版本、`regex` 版本）不影响本函数，若本地结果与上表不符，优先检查你是否抄对了转义。具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：手推 `extract_boxed_answers(r"\boxed{\sqrt{\frac{2}{3}}}")` 的返回值。

**答案**：piece = `\sqrt{\frac{2}{3}}}`。扫描：`{`(n=1) → `}`(n=0) → `{`(n=1) → `}`(n=0) → `}`(n=-1，触发截断)，答案为 `\sqrt{\frac{2}{3}}`。返回 `['\\sqrt{\\frac{2}{3}}']`。

**练习 2**：为什么第二步扫描里 `n == 0` 时遇到 `}` 还不输出，非要等 `n < 0`？

**答案**：`n == 0` 表示此刻处于 boxed 内容的「第一层」，但遇到的 `}` 使 n 变成 -1 才说明它对应的是**最外层** `boxed{` 的那个 `{`。若在 n 减到 0 时就停（即第一个令 n=0 的 `}`），嵌套内容如 `\frac{1}{2}` 会在内层 `}` 处被拦腰截断。

**练习 3**：`main.py` L88 用 `[-1]` 取最后一个 boxed 而不是 `[0]`，这与 u3-l1 的哪条模板约定呼应？

**答案**：呼应 proof_generation 模板中「Self Evaluation 小节要给出自我评分，且最终结论以 \(\boxed{\cdots}\) 收尾」的约定——评述性文字里可能先出现别的 boxed（如引用中间结果），只有最后一个才是模型对自身的打分。

### 4.2 模块二：`_normalize_prover_output` / `extract_solution` / `extract_self_eval` —— 标题归一化与章节切分

#### 4.2.1 概念说明

proof_generation 模板（u3-l1）要求生成器把输出分成 `## Solution` 与 `## Self Evaluation` 两个小节。但真实模型时常「手滑」把标题写成别的 Markdown 强调形式：`**Solution**`、`* Solution *`、`*** Self Evaluation ***`……

项目没有用更复杂的解析器，而是两步走：

1. `_normalize_prover_output` 把各种星号写法的标题**统一改写**成标准的 `\n## Solution\n` / `\n## Self Evaluation\n` 形式；
2. `extract_solution` / `extract_self_eval` 只认标准形式，用「标题字符串」当分隔符切分。

标题不在（模型彻底没按契约输出）就抛 `IndexError`，由调用方兜底——这是「宽容归一化 + 严格切分」的组合：小错修复，大错丢弃。

#### 4.2.2 核心流程

```text
_normalize_prover_output(text):
    ① 把 (^|\n)\s*\*+\s*Solution\s*\*+\s*\n     替换为 \n## Solution\n
    ② 把 \n\s*\*+\s*Self Evaluation\s*\*+\s*\n  替换为 \n## Self Evaluation\n
       —— ①② 把任意个数的星号标题统一成 ## 标题
    ③ 把 (^|\n)## Solution\s*\n                  替换为 \n## Solution\n
    ④ 把 \n## Self Evaluation\s*\n               替换为 \n## Self Evaluation\n
       —— ③④ 把已是 ## 的标题也规范化：前面必是换行、标题后直接换行（去尾部空白）
    ⑤ 首尾 strip

extract_solution(text):
    a = 以 "\n## Self Evaluation\n" 切分，取前半（[0]）—— 砍掉自评尾巴
    b = 以 "## Solution\n" 切分 a，取后半（[1]）—— 去掉 Solution 标题本身
    返回 b.strip()                       # 解答正文

extract_self_eval(text):
    以 "\n## Self Evaluation\n" 切分，取后半（[1]）
    返回 .strip()                        # 自评正文；标题不存在则 IndexError
```

一个具体的归一化例子：

```text
输入：  "* Solution *\n设 x=1……\n** Self Evaluation **\n本证明严谨，自评 \\boxed{1}。"
步骤①②："\n## Solution\n设 x=1……\n\n## Self Evaluation\n本证明严谨，自评 \\boxed{1}。"
步骤⑤：  strip 后首部多余的 \n 被去掉
```

注意 ①③ 的替换分组是 `(^|\n)`：文本**开头**的标题匹配 `^`，被替换成 `\n## ...`（凭空多出一个前导换行），最终靠⑤的 `strip()` 收掉。而 ②④ 的 Self Evaluation 模式**只认前面有换行**（`\n` 开头）——因为按契约它永远排在 Solution 之后，不可能位于文本最开头；若模型真的把它放在开头，它将不被归一化，后续切分随之失败，样本被丢弃。

#### 4.2.3 源码精读

归一化函数（[inference/utils.py:L36-L42](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L36-L42)）——四条 `regex.sub` 把两类标题都钉死到统一形态：

```python
def _normalize_prover_output(text):
    text = text.strip()
    text = regex.sub(r"(^|\n)\s*\*+\s*Solution\s*\*+\s*\n", "\n## Solution\n", text)
    text = regex.sub(r"\n\s*\*+\s*Self Evaluation\s*\*+\s*\n", "\n## Self Evaluation\n", text)
    text = regex.sub(r"(^|\n)## Solution\s*\n", "\n## Solution\n", text)
    text = regex.sub(r"\n## Self Evaluation\s*\n", "\n## Self Evaluation\n", text)
    return text.strip()
```

模式 `\s*\*+\s*Solution\s*\*+\s*` 能吃下 `**Solution**`、`* Solution *`、`** Solution **` 等所有变体：`\*+` 是一串星号，两侧的 `\s*` 容忍空格。第三、四条替换看似重复，实则是在**规范化已有的 `##` 标题**：确保标题前恰好是一个换行、标题后不拖带空白字符——这一步是给切分函数铺路的：切分的正则才能放心地写死 `\n## ...\n`。

切分函数（[inference/utils.py:L44-L50](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L44-L50)）——嵌套的两个 `regex.split`，先砍尾再取身：

```python
def extract_solution(student):
    student = _normalize_prover_output(student)
    return regex.split(r"## Solution\s*\n", regex.split(r"\n## Self Evaluation\s*\n", student)[0])[1].strip()

def extract_self_eval(student):
    student = _normalize_prover_output(student)
    return regex.split(r"\n## Self Evaluation\s*\n", student)[1].strip()
```

读懂 `[0]` 与 `[1]` 的方向感：以分隔符切分后，`[0]` 是分隔符**之前**的部分、`[1]` 是**之后**的部分。`extract_solution` 先按 Self Evaluation 切取 `[0]`（丢掉自评段），再按 Solution 切取 `[1]`（丢掉标题及之前的一切，比如前置的寒暄文字）；`extract_self_eval` 只需一步取 `[1]`。

异常语义：`[1]` 在分隔符不存在时（列表长度为 1）抛 `IndexError: list index out of range`——这就是「契约违反」的信号。调用现场（[inference/main.py:L83-L92](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L83-L92)）用两层 try/except 把异常变成两种处置——**解析失败丢样本，打分失败置 0 分但保留样本**：

```python
try:
    self_eval = extract_self_eval(proof).strip()
    proof = extract_solution(proof).strip()
    try:
        self_eval_score = float([s.strip() for s in extract_boxed_answers(self_eval) if s.strip()][-1])
    except:
        self_eval_score = 0
except:
    continue   # 小节缺失/多余 → 整条样本丢弃
```

对照阅读：外层 `except: continue` 兜住 `IndexError`（小节缺失或多余）；内层 `except: self_eval_score = 0` 兜住 `float()` 失败（比如 4.1.3 的百分号分支产出 `50}` 这种无法转 float 的答案）——两层的宽严程度不同，这正是 u3-l1「提示词格式要求」与「解析器格式约束」互为契约两面的落地现场。

#### 4.2.4 代码实践

**实践目标**：亲眼确认星号标题被归一化、切分结果符合预期、缺节时确实抛 `IndexError`。

**操作步骤**：

1. 在仓库根目录创建 `normalize_demo.py`（示例代码）：

   ```python
   import sys
   sys.path.insert(0, "inference")
   from utils import _normalize_prover_output, extract_solution, extract_self_eval

   outputs = [
       # 三种标题写法 + 完整两节
       "* Solution *\n设 x=1，代入得……\n** Self Evaluation **\n推理无漏洞，自评 \\boxed{1}。",
       "## Solution\n标准写法。\n## Self Evaluation\n自评 \\boxed{0.5}。",
       "*** Solution ***\n第三种写法。\n* Self Evaluation *\n自评 \\boxed{0}。",
   ]
   for out in outputs:
       print("归一化 =>", repr(_normalize_prover_output(out)))
       print("solution =>", repr(extract_solution(out)))
       print("self_eval =>", repr(extract_self_eval(out)))
       print("-" * 40)

   # 缺少 Self Evaluation 小节 → 预期 IndexError
   bad = "## Solution\n只有解答，没有自评。"
   try:
       extract_self_eval(bad)
   except IndexError as e:
       print("按预期抛出 IndexError:", e)
   ```

2. 运行 `python normalize_demo.py`。

**需要观察的现象**：三种输入的「归一化」打印是否都变成了 `\n## Solution\n...\n## Self Evaluation\n...` 的统一形态；`solution`/`self_eval` 两行是否恰好是两节的正文（不带标题）；最后一行是否打印出 IndexError。

**预期结果**：三条输入各自输出 `solution => '设 x=1，代入得……'` / `'标准写法。'` / `'第三种写法。'`（对应各自的正文），`self_eval => '自评 \\boxed{1}。'` 等；末尾打印 `按预期抛出 IndexError: list index out of range`。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`extract_solution` 里为什么要**先**按 Self Evaluation 切再按 Solution 切，反过来行不行？

**答案**：不行（至少会引入错误风险）。先切掉 Self Evaluation 尾巴，能保证「Solution 正文」不混入自评内容；若先按 Solution 切，`[1]` 里会带着 Self Evaluation 及其后的一切。此外先砍尾也避免了 Self Evaluation 正文里万一出现 `## Solution` 字样时的二次误切。

**练习 2**：模型输出把 `Self Evaluation` 写在了全文第一行（没有前置换行），会发生什么？

**答案**：归一化模式 ②④ 都要求标题前是 `\n`，位于文本开头的 Self Evaluation 不会被归一化成 `\n## Self Evaluation\n`；随后 `extract_self_eval` 的切分正则同样要求前置 `\n`，切不出 `[1]`，抛 `IndexError`，在 `main.py` L91-92 被 `except: continue` 捕获，该样本被丢弃。

**练习 3**：把 `_normalize_prover_output` 第 38 行的 `\*+` 改成 `\*`（单个星号）会失去哪些兼容性？

**答案**：将不再匹配 `**Solution**`、`*** Solution ***` 等**两个及以上**星号的写法——它们是最常见的 Markdown 加粗/斜体标题形态，模型恰恰最爱这么写。改完后这类样本会在切分时抛 `IndexError` 而被大量丢弃，显著降低可用样本率。（本练习只需推演，不要真去改源码。）

### 4.3 模块三：`hash_problem_idx` 与 `read_data` —— 稳定主键与统一读取

#### 4.3.1 概念说明

证明池（u5-l1 将深入）要为**每道题**维护一个独立文件，路径形如 `{proof_pool_dirname}/{source_name}/{problem_idx}.jsonl`，主键 `problem_idx` 直接成为文件名。`inputs/*.json` 的题目大多自带 `problem_idx` 字段（u1-l2），但流水线并不假设它一定存在——缺失时需要一个**不依赖中心分配、跨轮次稳定**的替补主键。对题面文本做 SHA-256 正好满足：同一道题每轮算出的哈希都相同，证明池文件就能被持续追加；不同题哈希几乎不可能相同。

`read_data` 则是纯粹的便利函数：`.jsonl` 逐行解析、其余（`.json`）整份加载，让 `main.py` 各阶段读输入时不必关心文件格式差异。

#### 4.3.2 核心流程

```text
hash_problem_idx(question):
    return SHA256(question 的 UTF-8 字节).hex()     # 64 个十六进制字符

read_data(path):
    若 path 以 ".jsonl" 结尾：逐行 json.loads，收集为列表
    否则：                    json.load 整份读入（假定是 JSON 数组）
    两种情况都返回 list[dict]
```

主键的确定过程（[inference/main.py:L171-L174](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L171-L174)）——先看字段、缺失才哈希：

```python
if 'problem_idx' in item:
    problem_idx = str(item['problem_idx'])
else:
    problem_idx = hash_problem_idx(item['question'].strip())
```

#### 4.3.3 源码精读

哈希函数（[inference/utils.py:L5-L6](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L5-L6)）——题面文本的 SHA-256 十六进制摘要，一行完成映射：

```python
def hash_problem_idx(question):
    return hashlib.sha256(question.encode()).hexdigest()
```

数据读取（[inference/utils.py:L8-L17](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L8-L17)）——按文件名后缀分发到两种解析路径：

```python
def read_data(path):
    items = []
    if path.endswith(".jsonl"):
        with open(path, "r") as file:
            for line in file:
                item = json.loads(line)
                items.append(item)
    else:
        items = json.load(open(path, "r"))
    return items
```

三个精读要点：

1. **稳定性的边界**：函数内部**不做** `strip`。调用点（main.py L174、L362）传入的是 `item['question'].strip()`——也就是说「同一道题」必须 `strip` 之后逐字符相同才会命中同一个主键；题面差一个尾随空格就会生成两个不同的证明池文件。雪崩效应是稳定主键的另一面。
2. **哈希主键的消费现场**：除生成路径（[inference/main.py:L179](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L179) 用它拼证明池文件名）外，[inference/main.py:L363](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L363) 还用 `assert problem_idx not in problem_idx_dedup` 防御**同一数据文件里出现两道字面相同的题**——届时后一道会触发断言失败，而不是静默共用一个证明池。
3. **`read_data` 的分发只看后缀**：给 `.jsonl` 后缀却传整份数组（或反之）会直接解析报错。它信任调用方遵守 u1-l2 讲过的格式纪律：中间产物一律 JSONL、原始输入用 JSON 数组。

#### 4.3.4 代码实践

**实践目标**：验证哈希的确定性与雪崩效应，并用 `read_data` 读取真实输入数据。

**操作步骤**：

1. 在仓库根目录创建 `hash_check.py`（示例代码）：

   ```python
   import sys
   sys.path.insert(0, "inference")
   from utils import hash_problem_idx, read_data

   items = read_data("inputs/CMO2024.json")
   print("CMO2024 题目数 =", len(items))
   print("首题字段 =", sorted(items[0].keys()))

   q = items[0]["question"]
   h1, h2 = hash_problem_idx(q.strip()), hash_problem_idx(q.strip())
   print("两次哈希相同 =", h1 == h2, "| 长度 =", len(h1))
   print("加一个空格后相同 =", hash_problem_idx(q.strip() + " ") == h1)
   ```

2. 运行 `python hash_check.py`。

**需要观察的现象**：题目数与字段列表（应含 `question`，且 inputs 自带 `problem_idx`，此时哈希只是替补）；两次哈希是否完全一致、长度是否为 64；题面追加一个空格后哈希是否彻底改变。

**预期结果**：`两次哈希相同 = True | 长度 = 64`；`加一个空格后相同 = False`。CMO2024 的具体题目数可在 u1-l2 的实践中核对过——应与之相同。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接用 `question` 文本当文件名，而要先哈希？

**答案**：题面可能极长（几百上千字符）、含 `/`、换行、中文等字符，直接当文件名会超路径长度限制或产生非法路径；SHA-256 把任意输入压成定长 64 个十六进制字符，安全可作文件名，且保留一一对应关系（碰撞概率可忽略）。

**练习 2**：`read_data("outputs/IMO2025.jsonl")` 与 `read_data("inputs/IMO2025.json")` 走的是同一条分支吗？返回类型有何共性？

**答案**：前者以 `.jsonl` 结尾走逐行分支，后者走 `json.load` 分支；但两者最终都返回 `list[dict]`，调用方因此可以用同一套 `for item in items` 遍历，这就是该函数存在的意义。

**练习 3**：若两道不同的题哈希后前 8 个字符相同（后 56 个不同），`main.py` L363 的断言会触发吗？证明池会冲突吗？

**答案**：都不会。断言比较的是完整 64 字符哈希（不相等则不触发），证明池文件名也由完整哈希决定，前缀相同不造成任何冲突。只有整串哈希完全相同（概率约 \(2^{-256}\)，实际可忽略）才会出问题。

## 5. 综合实践

把三个模块的验证合并成一份正式的单元测试文件 `test_utils.py`——这就是本讲的 `practice_task`。**不修改任何源码**，只把 4.1-4.3 中推演过的行为固化成断言。

**实践目标**：写一份可重复运行的回归测试，覆盖嵌套括号、百分号分支、星号标题归一化、哈希一致性、缺节异常五类行为。

**操作步骤**：

1. 确认依赖：`pip install regex`（`utils.py` 顶部 `import regex`）。
2. 在仓库根目录创建 `test_utils.py`（示例代码）：

   ```python
   """utils.py 的单元测试（不修改源码）。在仓库根目录运行：python test_utils.py"""
   import sys
   import unittest

   sys.path.insert(0, "inference")
   from utils import (
       extract_boxed_answers, extract_solution, extract_self_eval,
       hash_problem_idx, read_data,
   )


   class TestExtractBoxedAnswers(unittest.TestCase):
       def test_nested_braces(self):
           self.assertEqual(extract_boxed_answers(r"\boxed{\frac{1}{2}}"), [r"\frac{1}{2}"])
           self.assertEqual(extract_boxed_answers(r"\boxed{x^{2}}"), ["x^{2}"])

       def test_percent_suffix_branch(self):
           # '}' 后紧跟 '%'：答案保留闭合花括号
           self.assertEqual(extract_boxed_answers(r"\boxed{50}%"), ["50}"])
           self.assertEqual(extract_boxed_answers(r"\boxed{50}%，即 50 percent"), ["50}"])

       def test_multiple_boxed_in_order(self):
           self.assertEqual(extract_boxed_answers(r"\boxed{a} 中转 \boxed{b}"), ["a", "b"])

       def test_no_boxed_or_unclosed(self):
           self.assertEqual(extract_boxed_answers("no box here"), [])
           self.assertEqual(extract_boxed_answers(r"\boxed{x^{2}"), [])


   class TestSectionSplit(unittest.TestCase):
       OUTPUT = (
           "* Solution *\n"
           "设 x=1，代入原方程得……\n"
           "** Self Evaluation **\n"
           "推理无漏洞，自评 \\boxed{1}。"
       )

       def test_star_headers_normalized(self):
           self.assertEqual(extract_solution(self.OUTPUT), "设 x=1，代入原方程得……")
           self.assertEqual(extract_self_eval(self.OUTPUT), "推理无漏洞，自评 \\boxed{1}。")

       def test_missing_self_eval_raises_index_error(self):
           with self.assertRaises(IndexError):
               extract_self_eval("## Solution\n只有解答，没有自评。")


   class TestHashAndRead(unittest.TestCase):
       def test_hash_deterministic(self):
           q = "求所有正整数 n 使得 n^2 + 1 整除 n! 。"
           self.assertEqual(hash_problem_idx(q), hash_problem_idx(q))          # 同题多次哈希一致
           self.assertNotEqual(hash_problem_idx(q), hash_problem_idx(q + " "))  # 雪崩效应
           self.assertEqual(len(hash_problem_idx(q)), 64)                      # 十六进制定长

       def test_read_real_inputs(self):
           items = read_data("inputs/CMO2024.json")
           self.assertGreater(len(items), 0)
           self.assertIn("question", items[0])


   if __name__ == "__main__":
       unittest.main(verbosity=2)
   ```

   说明：`test_star_headers_normalized` 断言「星号标题被归一化后，两节正文各归各位」——Solution 正文不含标题与自评段，Self Evaluation 正文从标题后一直取到文末（其中的 `\boxed{1}` 原样保留）。

3. 运行 `python test_utils.py`，应看到 8 个测试逐条 `ok`。
4. 全部通过后，再做一次**故意破坏实验**：把 `test_missing_self_eval_raises_index_error` 里的输入换成 `"\n## Self Evaluation\n只有自评。"`（有自评无解答），给 `extract_solution` 加一条同样的 `assertRaises(IndexError)` 断言，验证「缺 Solution 小节」同样抛 IndexError。

**需要观察的现象**：8 个用例全部通过；补做的破坏实验中 `extract_solution` 也抛出 `IndexError: list index out of range`。

**预期结果**：与 4.1.4、4.2.4、4.3.4 的推演一致；`read_real_inputs` 读取的是真实仓库数据，题目数大于 0。完整运行输出**待本地验证**（依赖 `regex` 包已安装、在仓库根目录执行）。

## 6. 本讲小结

- `extract_boxed_answers` 用「`split('boxed{')` 定位 + 深度计数器配对括号」提取任意嵌套的 \(\boxed{\cdots}\) 内容；`}` 后紧跟 `%` 是特殊分支，会把闭合花括号保留在答案里（设计意图待确认）；未闭合与空 boxed 分别表现为漏提取与空串，靠下游 `if s.strip()` 过滤。
- `_normalize_prover_output` 用四条正则替换把 `* Solution *`、`**Self Evaluation**` 等星号标题统一成 `\n## Solution\n` / `\n## Self Evaluation\n`，是「宽容归一化」；`extract_solution` / `extract_self_eval` 随后按标题切分（先砍 Self Evaluation 尾巴再取 Solution 正文），标题缺失直接抛 `IndexError`，是「严格切分」。
- 异常的处置权在调用方：`main.py` L83-92 外层 `except: continue` 丢弃格式不合格样本，内层 `except: self_eval_score = 0` 只把分数降级为 0 并保留样本——两层宽严不同。
- `hash_problem_idx` 以题面文本的 SHA-256 作为无 `problem_idx` 字段时的替补主键，确定性保证证明池文件跨轮次可续写；但函数内部不 `strip`，主键稳定性以「调用点先 `strip`、字面完全一致」为前提。
- `read_data` 按后缀在「逐行 jsonl」与「整份 JSON 数组」两条读取路径间分发，统一返回 `list[dict]`，呼应 u1-l2 的格式纪律。
- 本讲的解析行为全部可以（也应该）用不改动源码的 `test_utils.py` 固化成回归测试。

## 7. 下一步学习建议

下一讲进入 u4 单元，看这些解析函数的**第一个真正消费者**：

- 优先读 [u4-l2 讲义（prepare_proof_verification 解析）](u4-l2-prepare-proof-verification.md)：它把本讲的 `extract_self_eval` → `extract_solution` → `extract_boxed_answers` 串成一条流水线，并演示 `finish_reason` 过滤与模板渲染如何衔接。
- 若想先建立全局观，可先读 u4-l1（main.py 的轮次编排），再回头读 u4-l2。
- 延伸阅读源码：`main.py` 的 [L128](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L128)、[L307](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L307)、[L330](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L330) 三处 `extract_boxed_answers` 调用，观察「同一个解析函数服务三种角色（自评/验证/元验证）」的复用方式。
