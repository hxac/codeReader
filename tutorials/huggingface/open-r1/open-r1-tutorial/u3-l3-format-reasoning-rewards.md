# 格式与推理过程奖励

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `format_reward`、`tag_count_reward`、`reasoning_steps_reward`、`code_format_reward` 这四个奖励函数各自奖励「什么行为」、返回值范围是什么。
- 读懂它们背后的正则表达式与字符串计数逻辑，并能预测一段给定模型输出会得到多少分。
- 理解「二值奖励（0/1）」与「部分得分奖励（0~1 连续）」在 RL 训练中的差别，以及为什么二者常常搭配使用。
- 动手调用这四个函数，验证它们对同一段文本的打分差异。

本讲承接 [u3-l2 奖励函数注册表与数学正确性奖励](u3-l2-reward-registry-accuracy.md)：上一讲拆开了 `accuracy_reward` 与注册表机制，本讲继续把注册表里「与输出格式 / 推理结构相关」的四个条目逐个讲透。

## 2. 前置知识

### 2.1 为什么需要「格式奖励」

`accuracy_reward` 只关心「答案对不对」，是**稀疏的二值信号**（1.0 或 0.0）。它有两个问题：

1. **不关心可用性**：模型可能算出了正确答案，却没有用约定的标签包起来，导致下游脚本无法自动抽取答案。
2. **梯度稀疏**：当模型还不会做题时，几乎所有样本都得 0 分，RL 没有差异化信号可供优化。

「格式奖励」就是用来补这两个洞的：它们检查模型输出是否遵循了约定的**结构**，给出一个**几乎总是非零**的密集信号，引导模型先学会「按规矩说话」，再逐步学会「说对」。

### 2.2 `<think>` / `<answer>` 标签约定

open-r1 的蒸馏与强化学习都要求模型把回答分成两段：

```text
<think>
这里写推理过程（思维链）
</think>
<answer>
这里写最终答案
</answer>
```

这个约定来自配方里的 `chat_template` 系统提示词。例如蒸馏配方就明确告诉模型：把回答拆成 Thought 与 Solution 两段，思维过程要分步骤写。参见：

- [recipes/OpenR1-Distill-7B/sft/config_distill.yaml:L9](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/OpenR1-Distill-7B/sft/config_distill.yaml#L9)：系统提示词要求「structure your response into two main sections: Thought and Solution ... `<think>` Thought section `</think>`」。

本讲的四个奖励函数，本质上都在用不同方式检查「模型有没有遵守这套标签约定」。

### 2.3 两个 Python 正则细节（本讲会反复用到）

- `re.DOTALL`：让 `.` 也能匹配换行符 `\n`，于是 `.*?` 可以跨越多行。
- `re.MULTILINE`：让 `^` 和 `$` 在每一行的行首/行尾都能匹配（默认只在整串首尾匹配）。
- `re.match` 只从**字符串开头**匹配；`re.findall` 返回**所有**不重叠的匹配，长度即「命中次数」。

记住这三点，下面的正则就很好读了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/open_r1/rewards.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py) | 全部奖励函数的实现，本讲的四个函数都在这里 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | `GRPOScriptArguments` 定义 `reward_funcs` 默认值与 `code_language` 等参数 |
| [recipes/OpenR1-Distill-7B/sft/config_distill.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/OpenR1-Distill-7B/sft/config_distill.yaml) | 蒸馏配方，其中的 `chat_template` 定义了 `<think>` 两段式约定 |
| [tests/test_rewards.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py) | 这四个函数的单元测试，是理解行为的最佳样本 |

## 4. 核心概念与源码讲解

### 4.1 format_reward：严格的标签结构匹配

#### 4.1.1 概念说明

`format_reward` 是一个**二值（0/1）奖励**：只有当模型输出**完全**符合 `<think>...</think>\n<answer>...</answer>` 结构时才给 1.0，差一个换行都给 0.0。它是最严格的格式裁判。

它的价值在于：一旦模型拿到这 1.0 分，就保证输出能被下游脚本用简单的字符串切分解析出「推理段」和「答案段」。

#### 4.1.2 核心流程

1. 从每个 completion 取出文本内容（`completion[0]["content"]`）。
2. 用一条「从头到尾」的正则去匹配整段文本。
3. 匹配成功 → 1.0；否则 → 0.0。

关键正则（注意每个标签两侧的 `\n`）：

```text
^<think>\n.*?\n</think>\n<answer>\n.*?\n</answer>$
```

含义拆解：

- `^<think>\n`：必须以 `<think>` 加一个换行开头。
- `.*?`：推理内容（非贪婪；配合 `DOTALL` 可跨多行）。
- `\n</think>\n<answer>\n`：`</think>` 和 `<answer>` 之间只能有一个换行。
- `.*?`：答案内容。
- `\n</answer>$`：必须以 `</answer>`（前接换行）结尾。

#### 4.1.3 源码精读

定义与正则匹配：

- [src/open_r1/rewards.py:L85-L90](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L85-L90)：`format_reward` 用 `re.match(pattern, content, re.DOTALL | re.MULTILINE)` 做整段匹配，返回 `[1.0 if match else 0.0 ...]`。

```python
def format_reward(completions, **kwargs):
    pattern = r"^<think>\n.*?\n</think>\n<answer>\n.*?\n</answer>$"
    completion_contents = [completion[0]["content"] for completion in completions]
    matches = [re.match(pattern, content, re.DOTALL | re.MULTILINE) for content in completion_contents]
    return [1.0 if match else 0.0 for match in matches]
```

两点说明：

- 用 `re.match` 已经隐式锚定字符串开头，所以前面的 `^` 是「显式但冗余」；真正起强制作用的是结尾的 `$`——它要求 `</answer>` 后面（基本）不能再有别的字符。
- `re.DOTALL` 让两个 `.*?` 能跨越多行，所以推理段里写多少行都行；这正是测试 `test_format_reward_specific_multiline` 能拿满分的原因。

测试佐证了「严格性」：

- [tests/test_rewards.py:L100-L119](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L100-L119)：正确格式得 1.0；缺标签、顺序颠倒、纯文本都得 0.0。

#### 4.1.4 代码实践

**目标**：直观感受 `format_reward` 的「全有或全无」。

**步骤**：

1. 在已安装 open-r1 的环境里（`PYTHONPATH=src` 或已 `make install`）新建脚本 `try_format.py`：

```python
# 示例代码
from open_r1.rewards import format_reward

samples = [
    # (a) 完全合规
    "<think>\nSome reasoning\n</think>\n<answer>\nThe answer\n</answer>",
    # (b) 缺少结尾换行与 </answer>
    "<think>\nSome reasoning\n</think>\n<answer>\nThe answer",
    # (c) 标签之间多了空行（\n\n 而非 \n）
    "<think>\nSome reasoning\n</think>\n\n<answer>\nThe answer\n</answer>",
]
completions = [[{"content": s}] for s in samples]
print(format_reward(completions))
```

2. 运行 `python try_format.py`。

**需要观察的现象 / 预期结果**：

- (a) → `1.0`； (b) → `0.0`（缺 `\n</answer>`）； (c) → `0.0`（`</think>` 与 `<answer>` 间是两个换行，正则要求恰好一个）。

> 说明：这些是确定性纯函数，上述结果可据正则推出；若你的环境输出不同，请检查是否改动了 `rewards.py`。运行命令本身**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：下面这段输出在 `format_reward` 下得几分？
`"<think>\n第一步\n第二步\n</think>\n<answer>\n9\n</answer>"`
**答案**：`1.0`。推理段虽有两行，但 `re.DOTALL` 让 `.*?` 能跨行匹配，整体结构合规。

**练习 2**：把结尾的 `$` 去掉，会更容易还是更难拿到分？为什么？
**答案**：更容易。去掉 `$` 后正则不再要求 `</answer>` 位于串尾，后面随便接什么字符都能匹配成功，从而放宽了判定。

---

### 4.2 tag_count_reward：部分得分的标签计数

#### 4.2.1 概念说明

`format_reward` 太严格：模型只要漏掉一个换行就一分不得，RL 训练早期几乎拿不到格式分。`tag_count_reward` 是它的「温和版」——把完整的标签结构拆成**四个独立的小检查**，每通过一个加 `0.25`，最终得分在 `0.0~1.0` 之间连续变化。

这种**部分得分（partial credit）**对训练至关重要：它能给「差一点点」的输出提供梯度，告诉模型「你已经做对了 3/4，再修最后一点就行」。

#### 4.2.2 核心流程

对每段文本统计四个子串各出现了几次，每个恰好出现 1 次就加 0.25：

| 检查的子串 | 含义 | 通过加分 |
| --- | --- | --- |
| `<think>\n` | 推理段开头 | 0.25 |
| `\n</think>\n` | 推理段结尾 + 答案段衔接 | 0.25 |
| `\n<answer>\n` | 答案段开头 | 0.25 |
| `\n</answer>` | 答案段结尾 | 0.25 |

得分公式（\(k\) 为通过的检查数）：

\[
\text{reward} = 0.25 \times k,\quad k \in \{0,1,2,3,4\}
\]

注意：它用的是 `str.count(...)` 统计**精确子串**出现次数，要求「恰好等于 1」。所以标签写了两遍（比如 `<think>\n` 出现两次）反而拿不到这一项的分。

#### 4.2.3 源码精读

内层计数函数与外层调用：

- [src/open_r1/rewards.py:L93-L112](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L93-L112)：`count_tags` 依次检查四个子串，每个 `== 1` 则累加 0.25。

```python
def count_tags(text: str) -> float:
    count = 0.0
    if text.count("<think>\n") == 1:
        count += 0.25
    if text.count("\n</think>\n") == 1:
        count += 0.25
    if text.count("\n<answer>\n") == 1:
        count += 0.25
    if text.count("\n</answer>") == 1:
        count += 0.25
    return count
```

对比 `format_reward`：两者检查的「结构」几乎一致，但 `format_reward` 用一条正则**一次性**判定（全有或全无），`tag_count_reward` 把它拆成四份**独立**判定（可部分通过）。这正是「严格裁判」与「鼓励型教练」的差别。

测试覆盖了每一种「缺一个标签」的情形：

- [tests/test_rewards.py:L414-L448](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L414-L448)：全对 → 1.0；缺任意一个标签 → 0.75；全无 → 0.0。

#### 4.2.4 代码实践

**目标**：对比 `format_reward` 与 `tag_count_reward` 在「差一点点」的输出上的差别。

**步骤**：

1. 运行下面的示例：

```python
# 示例代码
from open_r1.rewards import format_reward, tag_count_reward

samples = [
    "<think>\nSome reasoning\n</think>\n<answer>\nThe answer\n</answer>",   # 完美
    "<think>\nSome reasoning\n</think>\n<answer>\nThe answer",               # 缺结尾 </answer>
    "Some reasoning\n</answer>",                                             # 只剩一个结尾标签
]
completions = [[{"content": s}] for s in samples]
print("format   :", format_reward(completions))
print("tag_count:", tag_count_reward(completions))
```

**需要观察的现象 / 预期结果**：

- `format`：`[1.0, 0.0, 0.0]`
- `tag_count`：`[1.0, 0.75, 0.25]`（第二条少了 `\n</answer>`，第三条仅命中 `\n</answer>`）

差异解读：同样「不完美」的第二条，`format_reward` 给 0，`tag_count_reward` 给 0.75——后者为早期训练提供了可优化的信号。

> 运行命令**待本地验证**；预期值由源码逻辑直接推出。

#### 4.2.5 小练习与答案

**练习 1**：若某输出里 `<think>\n` 出现了 **2 次**（比如模型啰嗦地写了两段 think），这一项得多少分？
**答案**：0。条件是 `== 1`，出现 2 次不满足，该项不加 0.25。这是为了惩罚重复标签。

**练习 2**：为什么默认的 `reward_funcs` 同时包含 `format` 和 `tag_count`（见 4.2.3 之后的注册表）而不是只用一个？
**答案**：`format` 保证「拿到分就一定可解析」（强约束），`tag_count` 在模型还做不到满分时提供梯度（软塑形）。两者加权叠加，既有底线又有上升空间。

---

### 4.3 reasoning_steps_reward：思维链步骤模式匹配

#### 4.3.1 概念说明

前两个函数只看**外层标签结构**，不关心 `<think>` 里到底写了什么。`reasoning_steps_reward` 则向内看：它检查推理过程是否「分步骤、有条理」——比如有没有 `Step 1:`、有没有编号列表、有没有 `First, / Finally,` 之类的过渡词。

为什么要奖励「分步骤」？因为研究表明，结构化的思维链（chain-of-thought）能显著提升推理类任务的表现；用奖励去鼓励这种写作风格，等于在显式塑造模型的推理习惯。

#### 4.3.2 核心流程

1. 用一条「多选一」的正则去全文搜索步骤标记。
2. 用 `re.findall` 统计命中次数 \(c\)。
3. 得分 = \(\min(1.0,\; c/3)\)。

正则（`|` 表示「或」，命中任意一个分支都算一次）：

```text
(Step \d+:|^\d+\.|\n-|\n\*|First,|Second,|Next,|Finally,)
```

各分支含义：

| 分支 | 匹配示例 |
| --- | --- |
| `Step \d+:` | `Step 1:`、`Step 2:` |
| `^\d+\.` | `1.`（仅字符串开头） |
| `\n-` | 换行后的 `-` 列表项 |
| `\n\*` | 换行后的 `*` 列表项 |
| `First,` / `Second,` / `Next,` / `Finally,` | 过渡词 |

得分曲线：

\[
\text{reward}(c)=\min\!\left(1.0,\;\frac{c}{3}\right)
=
\begin{cases}
0 & c=0\\
\tfrac{1}{3}\approx 0.33 & c=1\\
\tfrac{2}{3}\approx 0.67 & c=2\\
1.0 & c\ge 3
\end{cases}
\]

「魔数 3」写在注释里：鼓励模型写出 3 步及以上的推理；不足 3 步给部分分。

#### 4.3.3 源码精读

函数体与魔数 3：

- [src/open_r1/rewards.py:L115-L129](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L115-L129)：用 `re.findall` 数命中次数，再 `min(1.0, count / 3)`。

```python
pattern = r"(Step \d+:|^\d+\.|\n-|\n\*|First,|Second,|Next,|Finally,)"
completion_contents = [completion[0]["content"] for completion in completions]
matches = [len(re.findall(pattern, content)) for content in completion_contents]
# Magic number 3 to encourage 3 steps and more, otherwise partial reward
return [min(1.0, count / 3) for count in matches]
```

两个**容易踩坑**的细节：

1. 这里**没有**传 `re.MULTILINE`，所以 `^\d+\.` 只在**整个字符串开头**匹配一次，不会匹配每行行首的编号。如果想奖励「每行一个编号」，靠的是 `\n-` / `\n\*`（用换行符前缀而非 `^`）。
2. `re.findall` 在「没有分组」时返回所有完整匹配；一旦给某个分支加了括号 `( )`，`findall` 会改成返回分组内容，计数语义就会变。当前正则最外层只有一个组、内部是「或」，所以返回的是命中字符串列表，`len(...)` 正好是命中次数。

测试覆盖了满分、部分分、零分三类：

- [tests/test_rewards.py:L121-L137](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L121-L137)：3 个 `Step` → 1.0；3 个过渡词 → 1.0；1 个 `Step` → 1/3；2 个过渡词 → 2/3；纯文本 → 0.0。

#### 4.3.4 代码实践

**目标**：观察「步骤标记」而非「标签结构」如何决定得分。

**步骤**：

```python
# 示例代码
from open_r1.rewards import reasoning_steps_reward

samples = [
    "Step 1: 分析题意\nStep 2: 列式\nStep 3: 求解",  # 3 个 Step
    "First, 我分析。Finally, 给出答案。",            # 2 个过渡词
    "这是一段没有明显步骤标记的话。",                 # 0 个
]
completions = [[{"content": s}] for s in samples]
print(reasoning_steps_reward(completions))
```

**需要观察的现象 / 预期结果**：

- `[1.0, 0.666..., 0.0]`。

> 注意：这段实践故意**不**加 `<think>/<answer>` 标签，用以说明 `reasoning_steps_reward` 只关心推理文本本身、与外层标签无关。运行命令**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：文本 `"1. 第一\n2. 第二"` 在 `reasoning_steps_reward` 下得几分？
**答案**：约 `0.333`（1/3）。因为没有 `re.MULTILINE`，`^\d+\.` 只匹配字符串最开头的 `1.`，命中 1 次；`\n` 后的 `2.` 不被 `^\d+\.` 命中（也不匹配其他分支）。所以 count=1。

**练习 2**：如果把正则里 `Step \d+:` 误改成 `(Step) \d+:`，得分会怎样变化？
**答案**：会出 bug。`re.findall` 遇到组会只返回组内容（`"Step"`），多个分支混用组会导致返回结构不一致，`len(...)` 不再等于真实命中次数。应保持「整组用 `( )`、内部用 `(?: )` 非捕获组」的写法。

---

### 4.4 code_format_reward：代码回答的专用格式奖励

#### 4.4.1 概念说明

当代码题（而非数学题）时，「答案段」不是一行数字，而是一段代码。`code_format_reward` 是 `format_reward` 的**代码版**：它要求 `<answer>` 里必须含一个**指定语言**的围栏代码块（如 ` ```python ... ``` `），其余结构同 `format_reward`。

它是**工厂函数** `get_code_format_reward(language="python")` 返回的闭包，语言可配置，且支持「混合语言训练」（不同样本用不同语言）。

#### 4.4.2 核心流程

1. `get_code_format_reward(language)` 记住目标语言，返回内层函数 `code_format_reward`。
2. 内层函数优先用数据集里的 `language` 列（`kwargs["language"]`），没有才回退到工厂传入的默认语言。
3. 用一条正则检查：`<think>` 段 + `<answer>` 段，且答案段里含 ` ```<语言> ... ``` ` 代码块。
4. 命中 → 1.0；否则 → 0.0。

关键正则（`{sample_language}` 会被替换成如 `python`）：

```text
^<think>\n.*?\n</think>\n<answer>\n.*?```{sample_language}.*?```.*?\n</answer>$
```

#### 4.4.3 源码精读

工厂与闭包：

- [src/open_r1/rewards.py:L595-L617](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L595-L617)：`get_code_format_reward` 返回 `code_format_reward`，后者按样本读取语言并用正则判定。

```python
def code_format_reward(completions, **kwargs):
    # 如果数据集里有 language 列就用它，否则用默认语言（支持混合语言训练）
    languages = kwargs["language"] if "language" in kwargs else [language] * len(completions)
    completion_contents = [completion[0]["content"] for completion in completions]
    matches = [
        re.match(
            rf"^<think>\n.*?\n</think>\n<answer>\n.*?```{sample_language}.*?```.*?\n</answer>$",
            content, re.DOTALL | re.MULTILINE,
        )
        for content, sample_language in zip(completion_contents, languages)
    ]
    return [1.0 if match else 0.0 for match in matches]
```

要点：

- 正则里 `.*?```{sample_language}.*?```.*?` 用了三段非贪婪 `.*?`，允许代码块前后有说明文字，但代码围栏的开头语言标识必须**精确**等于 `sample_language`（如写成 ``` ```py ``` 而非 ``` ```python ``` 就拿不到分）。
- 与 `format_reward` 一样用 `re.DOTALL | re.MULTILINE`，且仍是二值 0/1。
- 「优先读 `kwargs["language"]`」是为了让一个 batch 里同时有 Python、C++ 等不同语言的样本（混合语言训练），每种样本按自己的语言判定。

它在注册表里是「工厂调用」而非直接函数引用：

- [src/open_r1/rewards.py:L697](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L697)：注册表里 `"code_format": get_code_format_reward(language=script_args.code_language)`，语言取自配置。
- [src/open_r1/configs.py:L268-L275](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L268-L275)：`code_language` 默认 `"python"`，可选 `["python", "javascript", "r", "java", "bash", "cpp"]`。

测试覆盖了正确语言、错误语言、缺语言标识、多代码块等：

- [tests/test_rewards.py:L485-L564](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L485-L564)：正确 Python 格式 → 1.0；缺标签 / 缺代码块 / 错语言 / 缺语言标识 / 标签乱序 → 0.0；`<think>` 与 `<answer>` 里各有一个代码块也能匹配（非贪婪正确止于第一个 `\n</think>`）。

#### 4.4.4 代码实践

**目标**：验证「语言标识必须精确」与「混合语言」行为。

**步骤**：

```python
# 示例代码
from open_r1.rewards import get_code_format_reward

py_fn = get_code_format_reward(language="python")
js_fn = get_code_format_reward(language="javascript")

ok_py = "<think>\n分析\n</think>\n<answer>\n```python\nprint(1)\n```\n</answer>"
ok_js = "<think>\n分析\n</think>\n<answer>\n```javascript\nconsole.log(1)\n```\n</answer>"
no_lang = "<think>\n分析\n</think>\n<answer>\n```\nprint(1)\n```\n</answer>"

print("py_fn :", py_fn([[{"content": ok_py}], [{"content": ok_js}], [{"content": no_lang}]]))
# 混合语言：用 kwargs 里的 language 列，逐样本判定
print("mixed :", py_fn([[{"content": ok_py}], [{"content": ok_js}]], language=["python", "javascript"]))
```

**需要观察的现象 / 预期结果**：

- `py_fn` → `[1.0, 0.0, 0.0]`（只有 Python 块合规；JS 块语言不匹配；无语言标识不合规）。
- `mixed` → `[1.0, 1.0]`（第一个样本按 `python` 判、第二个按 `javascript` 判，都合规——这就是混合语言训练的用法）。

> 运行命令**待本地验证**；预期值据正则推出。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `code_format_reward` 要「优先用 `kwargs["language"]` 而非工厂默认语言」？
**答案**：为了支持混合语言训练。一个 batch 可能既有 Python 题也有 C++ 题，若都用同一个默认语言判定，另一种语言的样本永远拿不到格式分，等于被错误惩罚。

**练习 2**：代码块写成 ` ```python3 `（多了个 `3`）还能拿分吗？
**答案**：不能。正则要求围栏开头恰好是 ` ```python `（即 `sample_language` 紧跟），`python3` 不等于 `python`，匹配失败得 0.0。

---

## 5. 综合实践

把本讲四个函数串起来，体会「同一段输出在不同奖励下得分截然不同」——这正是 GRPO 用多个奖励加权求和的动机。

**任务**：构造三段风格不同的模型输出，分别过 `format_reward`、`tag_count_reward`、`reasoning_steps_reward`，再讨论若再加上 `code_format_reward` 会怎样。

```python
# 示例代码
from open_r1.rewards import format_reward, tag_count_reward, reasoning_steps_reward

completions = [
    # 1. 完美格式 + 3 步推理
    [{"content": "<think>\nStep 1: 分析题意\nStep 2: 列式计算\nStep 3: 得出结论\n</think>\n<answer>\n42\n</answer>"}],
    # 2. 结构几乎对，但缺结尾 </answer>，且有 2 步
    [{"content": "<think>\nStep 1: 分析\nStep 2: 计算\n</think>\n<answer>\n42"}],
    # 3. 完全没有标签，但用了过渡词
    [{"content": "First, 我来分析题意。Finally, 答案是 42。"}],
]

print("format_reward        :", format_reward(completions))
print("tag_count_reward     :", tag_count_reward(completions))
print("reasoning_steps_reward:", reasoning_steps_reward(completions))
```

**预期结果**（据源码推出，运行**待本地验证**）：

| 输出 | format | tag_count | reasoning_steps | 解读 |
| --- | --- | --- | --- | --- |
| 1 完美 | 1.0 | 1.0 | 1.0 | 结构与推理都满分 |
| 2 缺尾标签 | 0.0 | 0.75 | 0.667 | format 全扣，tag_count 给部分分，推理仍被认可 |
| 3 无标签 | 0.0 | 0.0 | 0.667 | 只有推理内容被打分，格式完全失分 |

**思考题**：

1. 如果 GRPO 用 `reward_weights` 把三个奖励等权相加，输出 2 的总分（0 + 0.75 + 0.667 ≈ 1.42）高于输出 3（0 + 0 + 0.667 ≈ 0.67）。这说明训练会优先**先把结构补全**（输出 2 → 输出 1），再优化推理质量——你能从这个排序看出「格式奖励作为塑形信号」的作用吗？
2. 若把输出 1 的 `<answer>` 内容换成 ` ```python\nprint(42)\n``` `，再过 `get_code_format_reward(language="python")`，它能否同时拿到 `format_reward` 和 `code_format_reward` 的分？请据两个正则分别判断。

> 提示：思考题 2 中 `code_format_reward` 的正则与 `format_reward` 的正则在 `<answer>` 段要求不同——后者只要 `.*?`，前者额外要求含 ` ```python...``` `。两套正则并不互斥，但满足条件不同，需分别验证。

## 6. 本讲小结

- `format_reward` 是**二值严格裁判**：用一条 `^...$` 正则要求输出从头到尾完全符合 `<think>\n...\n</think>\n<answer>\n...\n</answer>`，差一个换行即 0 分。
- `tag_count_reward` 是它的**温和版**：把结构拆成四个子串检查，每个 0.25，给出 0~1 的部分得分，为训练早期提供梯度。
- `reasoning_steps_reward` **向内看**：用多选一正则数步骤标记（`Step \d+:`、过渡词等），魔数 3 决定 `min(1.0, count/3)`，鼓励 3 步及以上推理；注意它**没有** `re.MULTILINE`，`^\d+\.` 只匹配串首。
- `code_format_reward` 是 `format_reward` 的**代码版**：由工厂 `get_code_format_reward(language)` 产出，要求 `<answer>` 内含指定语言的围栏代码块，并通过 `kwargs["language"]` 支持混合语言训练。
- 这四个函数中，`format` 与 `tag_count` 是 `GRPOScriptArguments` 的**默认奖励**（`reward_funcs` 默认 `["accuracy", "format", "tag_count"]`），可见「格式塑形」是 open-r1 GRPO 的标配。
- 四者全是**纯函数 + 正则/计数**，无模型、无沙箱，可在 CPU 上直接调用与测试。

## 7. 下一步学习建议

- 想看「正确性 + 长度」类奖励，继续阅读 [u3-l4 长度、余弦调度与重复惩罚奖励](u3-l4-length-cosine-repetition.md)，那里讲 `len_reward`、`get_cosine_scaled_reward` 等如何与 `accuracy_reward` 协同。
- 想了解代码**执行**类奖励（真正跑测试用例打分，而非只看格式），阅读 [u5-l1 代码奖励函数与执行脚本模板](u5-l1-code-reward-template.md)，对比「格式奖励（静态正则）」与「执行奖励（动态运行）」的差别。
- 想动手扩展，可仿照 `tests/test_rewards.py` 的写法，为本讲某个函数补一个边界用例（例如「标签出现两次」），并在 [u8-l2 测试体系与代码质量](u8-l2-tests-and-quality.md) 学到如何用 `make test` 跑通它。
- 建议回头精读 [src/open_r1/rewards.py:L646-L706](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L646-L706) 的 `get_reward_funcs` 注册表，把本讲四个条目（`format` / `tag_count` / `reasoning_steps` / `code_format`）与上一讲的 `accuracy` 放在一起，建立完整的「奖励函数全景图」。
