# u4-l2 答案提取与文本归一化

## 1. 本讲目标

上一讲（u4-l1）我们搭好了评测的「前三件套」：数据集（MATH-500）、提示模板（`render_prompt`）和模型加载（`load_model_and_tokenizer`）。模型生成的是一段自由文本，而评测需要的是一个可以和标准答案比对的「短答案」。本讲解决评测四步链路「渲染 → 生成 → **提取** → **判分**」中的后两步的前半部分：

学完本讲，你应该能够：

1. 手写一个能处理**嵌套大括号**的 `\boxed{...}` 提取器（`get_last_boxed`），并解释大括号不配平时为什么返回 `None`。
2. 说出 `extract_final_candidate` 的**三级回退策略**：优先取 boxed，其次取全文最后一个数字，最后（视 `fallback` 参数）返回全文；并说出 `number_then_full` 与 `number_only` 两种模式的差异。
3. 逐条跟踪 `normalize_text` 中十几条正则清洗规则（LaTeX 修正、上标转换、分数、千分位等），理解它们**为什么按这个顺序排列**。

这三个函数是纯文本操作，不依赖 GPU，也不需要加载模型——你可以在任何装好项目依赖的机器上把玩它们。

## 2. 前置知识

### 2.1 为什么需要「答案提取」

`render_prompt`（见 [reasoning_from_scratch/ch03.py:413-420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420)）在提示里明确要求模型「把最终结果写在单独一行，格式为 `\boxed{ANSWER}`」。这个约定是上一讲埋下的伏笔：它给模型的自由输出留了一个**机器可解析的锚点**。

但 0.6B 的 base 模型并不总是听话。它可能：

- 压根不写 `\boxed`，直接说 "The answer is 42"；
- 写了 `\boxed` 但大括号没闭合（生成被 `max_new_tokens` 截断时很常见）；
- 把答案写成 LaTeX 形式（`\frac{1}{2}`、`30^\circ`），和标准答案的写法不一致。

所以提取器不能只认 `\boxed`，还需要回退策略；判分前还需要把两边文本「归一化」到同一种书写习惯。本讲的三个函数就是干这两件事的。

### 2.2 你需要熟悉的几个概念

- **LaTeX**：数学排版语言。`\frac{1}{2}` 表示分数 \(\frac{1}{2}\)，`\sqrt{2}` 表示根式 \(\sqrt{2}\)，`^\circ` 表示角度单位（度），`\cdot` 表示乘号。
- **正则表达式**：Python 的 `re` 模块。本讲大量使用 `re.sub`（替换）、`re.match`（从头匹配）、`re.findall`（找所有匹配）。看懂 `\d`（数字）、`\s`（空白）、`(?<=...)`（后行断言）即可。
- **嵌套结构**：`\boxed{\frac{1}{2}}` 里，外层大括号内部还有 `{1}`、`{2}` 两层大括号。要提取最外层内容，不能简单找「第一个 `}`」，必须数大括号的深度——这就是括号配平计数，编译器解析表达式用的也是同一招。

### 2.3 与上一讲的衔接

上一讲我们确认了 `render_prompt` 的 `\boxed{ANSWER}` 输出契约。本讲就是这个契约的「兑现端」：[reasoning_from_scratch/ch03.py:455](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L455) 的 `mini_eval_demo` 和 [reasoning_from_scratch/ch03.py:532-534](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L532-L534) 的 `evaluate_math500_stream` 都在生成之后立刻调用 `extract_final_candidate`。

一个重要的架构事实先摆在这里：**提取（extract）和归一化（normalize）是解耦的两步**。`extract_final_candidate` 不调用 `normalize_text`；归一化发生在判分函数 `grade_answer` 内部（[reasoning_from_scratch/ch03.py:366](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L366)、[L370](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L370)），且对**预测答案和标准答案对称地**做同一套清洗。`grade_answer` 的内部原理（SymPy 符号等价判定）是下一讲 u4-l3 的主题，本讲只把它当作「会调用 `normalize_text` 的黑盒」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 本讲主战场：三个目标函数 + 正则常量表 + 消费方（`grade_answer`、评测循环） |
| [tests/test_ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py) | 三个函数的权威行为样例（输入 → 期望输出的对照表），本讲的实践基准 |
| [reasoning_from_scratch/ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py) | 下游消费者预览：启发式打分把三种 fallback 模式当成三档奖励 |

ch03.py 中与本讲相关的代码块分布：

| 位置 | 内容 |
| --- | --- |
| [L28-L30](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L28-L30) | `RE_NUMBER`：识别「最后一个数字」的正则 |
| [L32-L42](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L32-L42) | `LATEX_FIXES`：十组「坏 LaTeX → 好 LaTeX」替换对 |
| [L44-L49](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L44-L49) | `RE_SPECIAL` 与 `SUPERSCRIPT_MAP`：聊天特殊 token 剥除、上标字符映射表 |
| [L148-L183](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L148-L183) | `get_last_boxed`：嵌套大括号扫描 |
| [L186-L205](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L186-L205) | `extract_final_candidate`：三级回退 |
| [L208-L293](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L208-L293) | `normalize_text`：正则清洗流水线 |

另外，ch04（自一致性投票，[ch04.py:290](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L290)）、ch05（Best-of-N 打分）、ch06（RLVR 奖励，[ch06.py:102](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L102)）都会复用 `extract_final_candidate`——本讲讲透的这一个函数，是全书推理评测管线的公共零件。

## 4. 核心概念与源码讲解

### 4.1 get_last_boxed：嵌套大括号扫描

#### 4.1.1 概念说明

模型输出里可能有多处 `\boxed{...}`（比如先给一个中间结果，最后给终答）。我们想要**最后一个**——终答通常在文本末尾。

这件事难在**嵌套**：`\boxed{\frac{1}{2}}` 的答案是 `\frac{1}{2}`，它的内部还有 `{1}`、`{2}`。如果天真地从第一个 `{` 找到第一个 `}`，就会截出 `\frac{1`，答案直接报废。

解决办法是经典的**括号深度计数**（和编译器检查表达式合法性用的是同一个算法）：遇到 `{` 深度加一，遇到 `}` 深度减一，深度归零时的那个 `}` 才是和外层配对的右括号。

#### 4.1.2 核心流程

```
输入 text
  │
  ├─ rfind(r"\boxed") ──► 找不到？ ──► 返回 None
  │
  ├─ 跳过 \boxed 之后的空白字符
  │
  ├─ 下一个字符必须是 "{" ──► 不是？ ──► 返回 None
  │
  ├─ 从 "{" 之后开始，brace_depth = 1
  │    循环扫描每个字符：
  │      "{" → depth += 1
  │      "}" → depth -= 1
  │      直到 depth == 0 或文本耗尽
  │
  ├─ 扫描结束后 depth ≠ 0（不配平）──► 返回 None
  │
  └─ 返回 (内容起点, 闭合括号位置) 之间的子串
```

时间复杂度是 \(O(n)\)：一次 `rfind` 加一次线性扫描，对几千 token 的生成文本毫无压力。

#### 4.1.3 源码精读

[reasoning_from_scratch/ch03.py:148-163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L148-L163)——定位与「三道准入检查」：

```python
def get_last_boxed(text):
    # Find the last occurrence of "\boxed"
    boxed_start_idx = text.rfind(r"\boxed")
    if boxed_start_idx == -1:
        return None

    # Get position after "\boxed"
    current_idx = boxed_start_idx + len(r"\boxed")

    # Skip any whitespace after "\boxed"
    while current_idx < len(text) and text[current_idx].isspace():
        current_idx += 1

    # Expect an opening brace "{"
    if current_idx >= len(text) or text[current_idx] != "{":
        return None
```

这段做了三件事：① `rfind` 从右向左找**最后一次**出现的 `\boxed`，找不到返回 `None`；② 容忍 `\boxed` 与 `{` 之间的空白（模型可能写成 `\boxed   {42}`）；③ 紧跟的字符必须是 `{`，否则（比如写成了 `\boxed 42`）返回 `None`。

[reasoning_from_scratch/ch03.py:165-183](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L165-L183)——深度计数与提取：

```python
    # Parse the braces with nesting
    current_idx += 1
    brace_depth = 1
    content_start_idx = current_idx

    while current_idx < len(text) and brace_depth > 0:
        char = text[current_idx]
        if char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
        current_idx += 1

    # Account for unbalanced braces
    if brace_depth != 0:
        return None

    # Extract content inside the outermost braces
    return text[content_start_idx:current_idx-1]
```

四个关键点：

- 进入循环前 `brace_depth = 1`，对应已经消费掉的那个外层 `{`。
- 循环条件是 `brace_depth > 0`——深度归零即刻停步，`}` 之后的文本不再关心。
- 循环自然耗尽文本时深度仍大于 0，说明右括号缺失（典型场景：`max_new_tokens` 截断了生成），返回 `None`，**宁缺毋滥**。
- 切片终点是 `current_idx - 1`：循环退出时 `current_idx` 已经越过了配对的 `}`，退一格正好把它排除。

一个值得注意的细节：扫描器**不区分** `\{`（LaTeX 转义的花括号，常用于集合记号）和普通 `{`，两种字符一视同仁地计数。碰巧的是 `\{...\}` 本身也是配平的，所以 `\boxed{\{1,2\}}` 依然能正确取出 `\{1,2\}`——这是「无心插柳」的鲁棒性。

还有个容易忽略的行为：如果文本里有**多个** `\boxed` 而**最后一个**不配平，函数返回 `None`，**不会**回过头去取前一个配平的 `\boxed`。`rfind` 一锤定音。

仓库测试 [tests/test_ch03.py:139-153](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L139-L153) 给出了七个权威样例：

| 输入 | 期望输出 | 考察点 |
| --- | --- | --- |
| `foo \boxed{42}` | `42` | 基本提取 |
| `\boxed{a} bla \boxed{b+c}` | `b+c` | 取最后一个 |
| `noise \boxed   {  x^2 } end` | `'  x^2 '` | 容忍空白；内容原样保留（含首尾空格） |
| `\boxed{outer {inner} ok}` | `outer {inner} ok` | 嵌套配平 |
| `nothing here` | `None` | 无 boxed |
| `\boxed  not_brace` | `None` | 缺 `{` |
| `\boxed{unbalanced` | `None` | 括号不配平 |

#### 4.1.4 代码实践

**实践目标**：亲手复现上表七条样例，确认你理解的扫描行为与仓库测试一致。

**操作步骤**：

1. 确认已在 u1-l2 搭好的项目环境中（`import reasoning_from_scratch.ch03` 会连带 `import torch`，见 [ch03.py:16](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L16)，所以要在装好依赖的虚拟环境里跑）。
2. 在项目根目录外任何位置新建 `boxed_practice.py`（以下为示例代码，不要改动仓库源码）：

   ```python
   from reasoning_from_scratch.ch03 import get_last_boxed

   cases = [
       (r"foo \boxed{42}", "42"),
       (r"\boxed{a} bla \boxed{b+c}", "b+c"),
       (r"noise \boxed   {  x^2 } end", "  x^2 "),
       (r"\boxed{outer {inner} ok}", "outer {inner} ok"),
       (r"nothing here", None),
       (r"\boxed  not_brace", None),
       (r"\boxed{unbalanced", None),
   ]

   for i, (text, expected) in enumerate(cases, 1):
       got = get_last_boxed(text)
       mark = "PASS" if got == expected else "FAIL"
       print(f"case {i} [{mark}]: {text!r} -> {got!r}")
   ```

3. 运行 `python boxed_practice.py`。

**需要观察的现象**：case 3 的输出带首尾空格（`'  x^2 '`）——`get_last_boxed` 自己不做 strip，清洗职责后移；case 4 能完整取出嵌套内容；case 5/6/7 分别因三种准入失败返回 `None`。

**预期结果**：七行全部 PASS（样例即仓库测试原题）。若 case 7 疑惑，手数一遍深度：外层 `{` 计 1，扫到文本末尾仍是 1，触发 [L179-L180](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L179-L180) 的 `return None`。

#### 4.1.5 小练习与答案

**练习 1**：`\boxed{\frac{\sqrt{3}}{2}}` 会返回什么？先手推再验证。

<details>
<summary>参考答案</summary>

返回 `\frac{\sqrt{3}}{2}`。深度轨迹：外层 `{` 计 1 → `\frac` 后 `{` 计 2 → `\sqrt` 后 `{` 计 3 → `}` 计 2 → `}` 计 1 → `}` 计 0 停止。多层嵌套照样配平。（此为手推结论，可本地运行验证。）
</details>

**练习 2**：构造一个输入，使得文本中**存在**一个完整配平的 `\boxed{...}`，但函数仍返回 `None`。

<details>
<summary>参考答案</summary>

让**最后一个** `\boxed` 不配平即可，例如 `\boxed{1} then \boxed{bad`。`rfind` 定位到最后一个 `\boxed`，其大括号不闭合，返回 `None`——不会回退到前一个。注意此时 `extract_final_candidate` 的数字回退会接手（见 4.2），对这条输入 `RE_NUMBER` 能找到 `1`，最终提取结果是 `"1"`（手推结论，待本地验证）。
</details>

**练习 3**：为什么函数结尾切片是 `text[content_start_idx:current_idx-1]` 而不是 `text[content_start_idx:current_idx]`？

<details>
<summary>参考答案</summary>

循环体内每次处理后 `current_idx` 都自增，退出循环时它指向**配对 `}` 的下一个字符**。减一正好排除 `}` 本身；若不减，提取结果会以 `}` 结尾。注意当循环因「文本耗尽」退出时深度非零、走 `return None` 分支，所以 `-1` 修正只在正常配平时生效，不会切掉有效字符。
</details>

### 4.2 extract_final_candidate：三级回退策略

#### 4.2.1 概念说明

`get_last_boxed` 只覆盖「模型听话」的情况。`extract_final_candidate` 在它外面套了一层**逐级降级的保险丝**：

1. **第一优先**：取最后一个 `\boxed{...}` 的内容（4.1 的 `get_last_boxed`）。
2. **第二优先**：没有 boxed 时，用正则在全文里找数字，取**最后一个**数字（模型习惯把结论放最后）。
3. **第三优先**（可选）：连数字都没有时，返回**全文**——交给下游判分器「死马当活马医」。

第 2、3 级由 `fallback` 参数控制，这也是本讲学习目标里「`number_then_full` 与 `number_only` 两种回退的差异」的所在：

| `fallback` 取值 | 有 boxed | 无 boxed、有数字 | 无 boxed、无数字 |
| --- | --- | --- | --- |
| `"number_then_full"`（默认） | boxed 内容 | 最后一个数字 | **返回全文** |
| `"number_only"` | boxed 内容 | 最后一个数字 | **返回空串 `""`** |
| 其他任何值（含 `None`） | boxed 内容 | **直接返回空串**（不做数字回退） | 空串 |

#### 4.2.2 核心流程

```
输入 text, fallback="number_then_full"
  │
  ├─ text 为空 ──► 返回 ""
  │
  ├─ boxed = get_last_boxed(text.strip())
  │     ├─ 成功 ──► 返回 boxed.strip().strip("$ ")     # 剥掉边缘的 $ 和空格
  │     └─ 失败
  │
  ├─ fallback ∈ {number_then_full, number_only}？
  │     ├─ 否 ──► 返回 ""
  │     └─ 是 ──► m = RE_NUMBER.findall(text)
  │           ├─ m 非空 ──► 返回 m[-1]                  # 最后一个数字
  │           └─ m 为空
  │                 ├─ fallback == number_then_full ──► 返回全文 text
  │                 └─ 否（number_only）──────────────► 返回 ""
```

数字识别正则 `RE_NUMBER` 定义在 [reasoning_from_scratch/ch03.py:28-30](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L28-L30)：

```python
RE_NUMBER = re.compile(
    r"-?(?:\d+/\d+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)
```

它匹配三种形态：分数（`\d+/\d+`，如 `14/3`）、整数（`42`）、小数（`3.14`），可选负号，小数还可带科学计数法后缀（`1.5e-3`）。注意它**不认识** `\frac{1}{2}` 这种 LaTeX 分数——`\frac` 里的 `1` 和 `2` 会被当成两个独立整数，最后取到 `2`。这是回退路径天然的下限，也正是第一优先 boxed 路径存在的意义。

#### 4.2.3 源码精读

[reasoning_from_scratch/ch03.py:186-205](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L186-L205)——完整函数只有 20 行：

```python
def extract_final_candidate(text, fallback="number_then_full"):
    # Default return value if nothing matches
    result = ""

    if text:
        # Prefer the last boxed expression if present
        boxed = get_last_boxed(text.strip())
        if boxed:
            result = boxed.strip().strip("$ ")
        # If no boxed expression, try fallback
        elif fallback in ("number_then_full", "number_only"):
            m = RE_NUMBER.findall(text)
            if m:
                # Use last number
                result = m[-1]
            elif fallback == "number_then_full":
                # Else return full text if no number found
                result = text
    return result
```

逐点解读：

- `if text:` 一行完成空串与 `None` 的统一防御，空输入直接返回 `""`（下游 `grade_answer` 对空串会判错，见 [L364](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L364) 的判空）。
- `boxed.strip().strip("$ ")`：先去首尾空白，再剥掉两侧的 `$` 与空格字符（`str.strip` 的参数是**字符集合**）。这处理了行内数学包裹 `$ \boxed{...} $` 的情形。
- `elif fallback in (...)` 是一个**白名单**：传非法值（包括 `None`）不会报错，只是静默跳过所有回退——效果等于「只认 boxed」。第 5 章（u6-l2）正是要利用这个第三档行为。
- `m[-1]`：`findall` 按出现顺序返回，取最后一个，呼应「结论在文末」的先验。

仓库测试 [tests/test_ch03.py:156-175](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L156-L175) 的十条权威样例：

| 输入 | 期望输出 | 走的路径 |
| --- | --- | --- |
| `Steps...\n\boxed{3/4}\nDone.` | `3/4` | boxed |
| `Reasoning...\n\boxed{\frac{3}{4}}` | `\frac{3}{4}` | boxed，LaTeX **原样保留** |
| `Compute...\n\boxed{\sqrt{2}}` | `\sqrt{2}` | boxed |
| `Tuple case...\n\boxed{(1,2)}` | `(1,2)` | boxed，元组整体取出 |
| `Earlier box \boxed{1/3}, later \boxed{2/3}` | `2/3` | boxed，取最后一个 |
| `Noisy \boxed   {  x^2 } trailing text` | `x^2` | boxed，空白容忍 + strip |
| `Nested braces \boxed{outer {inner} ok}` | `outer {inner} ok` | boxed 嵌套 |
| `In math mode: $ \boxed{ \dfrac{14}{3} } $` | `\dfrac{14}{3}` | boxed + `strip("$ ")` |
| `Some steps...\nFinal Answer: 14/3` | `14/3` | 回退：数字（`RE_NUMBER` 认识 `14/3`） |
| `All done. 1 \nFinal 2 answer: ` | `2` | 回退：取**最后**一个数字 |

**下游怎么消费这三档**：第 5 章的 `heuristic_score`（[reasoning_from_scratch/ch05.py:22-39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L22-L39)）把三种模式串成三档奖励——先用 `fallback=None` 探测「有没有规范 boxed」（有则加 `boxed_bonus=2.0`），再用 `"number_only"` 探测「能否至少提取出数字」（加 `extract_bonus=1.0`），最后用 `"number_then_full"` 兜底（`fulltext_bonus=0.0`）。本讲的三级回退在第 5 章会原封不动地变成「奖励的分层尺度」，这是 u6-l2 的预告。

#### 4.2.4 代码实践

**实践目标**：直观对比三种 `fallback` 模式在同一批「不守规矩」文本上的行为差异。

**操作步骤**：

1. 新建 `fallback_practice.py`（示例代码）：

   ```python
   from reasoning_from_scratch.ch03 import extract_final_candidate

   texts = [
       "The answer is 42",       # 无 boxed，有数字
       "I have no idea.",        # 无 boxed，无数字
       "\\boxed{unbalanced",     # boxed 不配平，无数字
       "final 3/4 maybe",        # 无 boxed，有分数
   ]

   modes = ["number_then_full", "number_only", None]
   for t in texts:
       row = [extract_final_candidate(t, fallback=m) for m in modes]
       print(f"{t!r:32} then_full={row[0]!r:22} only={row[1]!r:8} None={row[2]!r}")
   ```

2. 运行脚本，观察三种模式给出的四组结果。

**需要观察的现象**：

- `"I have no idea."` 在 `number_then_full` 下返回**全文**，在 `number_only` 和 `None` 下返回空串。
- `"The answer is 42"` 在三种模式下都返回 `"42"`（数字回退对 `number_only` 依然生效）。
- `\boxed{unbalanced` 在任何模式下都不走 boxed 路径，退回数字/全文回退。

**预期结果**：`then_full` 列依次为 `"42"`、全文、全文、`"3/4"`；`only` 列为 `"42"`、`""`、`""`、`"3/4"`；`None` 列为 `""`、`""`、`""`、`""`（以上为依据源码逻辑的手推结论，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：输入 `"\\boxed{\\frac{1}{2}"`（boxed 不配平），`extract_final_candidate` 默认模式返回什么？

<details>
<summary>参考答案</summary>

`get_last_boxed` 因深度未归零返回 `None`，随即触发数字回退。`RE_NUMBER` 在文本中找到 `1` 和 `2` 两个独立整数（`\frac{1}{2}` 里的两组花括号之间没有 `/`，分数分支不命中），取最后一个，返回 `"2"`。也就是说提取结果从「二分之一」变成了「2」——大概率判错，但不至于空手而归。（手推结论，待本地验证。）
</details>

**练习 2**：为什么默认回退取 `m[-1]`（最后一个数字）而不是 `m[0]`（第一个）？

<details>
<summary>参考答案</summary>

数学解答的行文习惯是「推理在前、结论在后」，题干复述里出现的数字往往是中间量，末尾的数字更可能是终答。测试样例 `"All done. 1 \nFinal 2 answer: "` 取 `2` 而非 `1` 印证了这一设计。
</details>

**练习 3**：如果你希望「模型没写 boxed 就直接判错，不做任何挽救」，应该怎么调用？

<details>
<summary>参考答案</summary>

`extract_final_candidate(text, fallback=None)`（或任何不在白名单里的值）。第 5 章 `heuristic_score`（[ch05.py:25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L25)）正是用它做「是否规范作答」的布尔探测。
</details>

### 4.3 normalize_text：正则清洗流水线

#### 4.3.1 概念说明

提取出的答案（`\frac{3}{4}`）和标准答案（`3/4`、`0.75`、`\dfrac{3}{4}`……）可能**数学上相等但写法不同**。`normalize_text` 的任务是把两边拉到同一种「书写方言」，让后续的字符串比较和 SymPy 解析（u4-l3）更容易命中。

它的设计哲学是**流水线式的顺序替换**：一条文本从上到下流过十几个工位，每个工位只干一件小事。顺序不是随便排的——后面的工位依赖前面工位的产出（比如必须先把 `^\circ` 处理掉，之后统一的 `^` → `**` 替换才不会把它污染成 `**circ`）。

#### 4.3.2 核心流程

`normalize_text`（[reasoning_from_scratch/ch03.py:208-293](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L208-L293)）内部按以下顺序执行：

```
输入 text
  ① 空值防御 ──────────────► 返回 ""
  ② 剥除聊天特殊 token（<|...|>）
  ③ 剥除选择题字母前缀（"c. 3" → "3"）
  ④ 剥除角度单位三种写法（^{\circ}、^\circ、°）
  ⑤ 若整体被 \text{...} 包裹则拆包
  ⑥ 剥除行内/行间数学定界符 \( \) \[ \]
  ⑦ LATEX_FIXES 十连替换（\left/\right、间距符、\cdot、·、×、\dfrac→\frac、° …）
  ⑧ Unicode 上标转指数（2² → 2**2），两遍扫描
  ⑨ 剥 \%、$、%（百分号直接删除）
  ⑩ \sqrt{...} 与 \sqrt x → sqrt(...)
  ⑪ \frac{a}{b} 与 \frac a b → (a)/(b)
  ⑫ ^ → **（幂运算符）
  ⑬ 混合数字：`1 1/2` → `1+1/2`
  ⑭ 千分位逗号：`1,234` → `1234`
  ⑮ 删除所有残余 { }、strip、转小写
```

目标是把答案逐步改写成「SymPy 友好」的形态：`sqrt(...)`、`(a)/(b)`、`**` 都是 Python/SymPy 表达式语法。

#### 4.3.3 源码精读

**工位 ②：剥聊天特殊 token**——[L211](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L211)：

```python
    text = RE_SPECIAL.sub("", text).strip()
```

`RE_SPECIAL`（[L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L44)）是 `<\|[^>]+?\|>`，匹配 `<|assistant|>` 这类标记。u2-l6 讲过：分词器的 `decode` 默认**保留**特殊 token，就是为了让这里有机会剥掉它们。

**工位 ③：选择题前缀**——[L213-L217](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L213-L217)：

```python
    match = re.match(r"^[A-Za-z]\s*[.:]\s*(.+)$", text)
    if match:
        text = match.group(1)
```

单个字母 + `.` 或 `:` + 内容，即视为选择题标签剥掉（`c. 3` → `3`）。注意副作用：任何「单字母单词开头」的文本都会被砍头（如 `e.g. 2` 会变成 `g. 2`，且只剥一层）——简单规则的固有盲区，本讲末尾与 u10-l4 再谈。

**工位 ④：角度单位**——[L219-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L219-L222)：

```python
    text = re.sub(r"\^\s*\{\s*\\circ\s*\}", "", text)   # ^{\circ}
    text = re.sub(r"\^\s*\\circ", "", text)             # ^\circ
    text = text.replace("°", "")                        # Unicode degree
```

三种「度」的写法（花括号式、裸式、Unicode 字符）统一删除。`30^{\circ}` → `30`。这一步**必须**排在工位 ⑫（`^` → `**`）之前，否则会先被污染成 `30**{circ}`。

**工位 ⑤⑥：拆包与剥定界符**——[L224-L231](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L224-L231)：

```python
    match = re.match(r"^\\text\{(?P<x>.+?)\}$", text)
    if match:
        text = match.group("x")

    # strip inline/display math wrappers \( \) \[ \]
    text = re.sub(r"\\\(|\\\)|\\\[|\\\]", "", text)
```

`\text{45}` → `45`（仅当**整串**被包裹）；`\( 2/3 \)` → `2/3`。

**工位 ⑦：LATEX_FIXES 十连替换**——[L32-L42](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L32-L42) 与 [L232-L235](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L232-L235)：

```python
LATEX_FIXES = [  # Latex formatting to be replaced
    (r"\\left\s*", ""),
    (r"\\right\s*", ""),
    (r"\\,|\\!|\\;|\\:", ""),
    (r"\\cdot", "*"),
    (r"\u00B7|\u00D7", "*"),
    (r"\\\^\\circ", ""),
    (r"\\dfrac", r"\\frac"),
    (r"\\tfrac", r"\\frac"),
    (r"°", ""),
]
...
    for pat, rep in LATEX_FIXES:
        text = re.sub(pat, rep, text)
```

逐对说明：删 `\left`/`\right`；删四种 LaTeX 细间距符（`\,` `\!` `\;` `\:`）；`\cdot`、`·`（U+00B7）、`×`（U+00D7）统一为 `*`；删 `\^\circ` 与 `°`（与工位 ④ 有意重叠，属于纵深防御）；`\dfrac`、`\tfrac` 统一为 `\frac`——**这次归一是工位 ⑪ 分数转换能命中的前提**（`\dfrac{14}{3}` 先变 `\frac{14}{3}` 才能匹配后文的 `\frac` 模式）。

**工位 ⑧：Unicode 上标**——[L236-L251](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L236-L251)：

```python
    def convert_superscripts(s, base=None):
        converted = "".join(
            SUPERSCRIPT_MAP[ch] if ch in SUPERSCRIPT_MAP else ch
            for ch in s
        )
        if base is None:
            return converted
        return f"{base}**{converted}"

    text = re.sub(
        r"([0-9A-Za-z\)\]\}])([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)",
        lambda m: convert_superscripts(m.group(2), base=m.group(1)),
        text,
    )
    text = convert_superscripts(text)
```

`SUPERSCRIPT_MAP`（[L45-L49](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L45-L49)）把 `⁰¹²³…` 映射到普通数字、`⁺⁻⁽⁾` 映射到 `+-()`。第一遍正则把「基数 + 上标串」拼成 `基数**上标`（`2²` → `2**2`、`x⁻¹` → `x**-1`）；第二遍兜底转换任何**没有基数**的散落上标字符。

**工位 ⑨⑩：百分号与根式**——[L253-L264](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L253-L264)：

```python
    text = text.replace("\\%", "%").replace("$", "").replace("%", "")
    text = re.sub(
        r"\\sqrt\s*\{([^}]*)\}",
        lambda match: f"sqrt({match.group(1)})",
        text,
    )
    text = re.sub(
        r"\\sqrt\s+([^\\\s{}]+)",
        lambda match: f"sqrt({match.group(1)})",
        text,
    )
```

`\%` 先还原为 `%` 再连同 `$` 一起删除（注意：百分号被**彻底移除**，`50\%` 与 `50` 归一相同——数学题里通常不带单位，可接受）；`\sqrt{2}` 与无花括号的 `\sqrt 2` 都转成 `sqrt(2)`。

**工位 ⑪：分数**——[L266-L276](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L266-L276)：

```python
    text = re.sub(
        r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}",
        lambda match: f"({match.group(1)})/({match.group(2)})",
        text,
    )
    text = re.sub(
        r"\\frac\s+([^\s{}]+)\s+([^\s{}]+)",
        lambda match: f"({match.group(1)})/({match.group(2)})",
        text,
    )
```

花括号式与空格式各一条规则，统一改写成 `(分子)/(分母)`。注意 `[^{}]+` 显式排除嵌套花括号——所以 `\frac{\frac{1}{2}}{3}` 这类**嵌套分数不会被转换**，最后残余花括号在工位 ⑮ 被整体删除，得到 `\frac\frac1/2/3` 这样的残串（会交给 SymPy 解析失败、判错）。这是简单归一器的已知上限，u10-l4 的混合解析器来补。

**工位 ⑫⑬：幂与混合数字**——[L278-L284](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L278-L284)：

```python
    text = text.replace("^", "**")
    text = re.sub(
        r"(?<=\d)\s+(\d+/\d+)",
        lambda match: "+" + match.group(1),
        text,
    )
```

`^` 全局替换为 `**`；随后「数字 空格 分数」被改写为 `数字+分数`——带分数 `1 1/2` → `1+1/2`（数学上 \(1\frac{1}{2} = 1 + \frac{1}{2}\)）。

**工位 ⑭⑮：千分位与收尾**——[L286-L293](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L286-L293)：

```python
    # 1,234 -> 1234
    text = re.sub(
        r"(?<=\d),(?=\d\d\d(\D|$))",
        "",
        text,
    )

    return text.replace("{", "").replace("}", "").strip().lower()
```

千分位规则用后行断言 `(?<=\d)` 和先行断言 `(?=\d\d\d(\D|$))` 保证「逗号前是数字、后面**恰好**三位数字再接非数字」，`1,234` → `1234`，而 `(1, 2)`（带空格的元组）不受影响。最后删除所有残余花括号、去首尾空白、**整体转小写**。

一个值得亲手验证的边界：元组 `(1,234)` **不带空格**时，千分位规则的先行断言同样满足（`234` 后紧跟 `)`），于是被误归一为 `(1234)`——「千分位逗号」和「元组逗号」在无空格书写下天然歧义，简单规则选择偏向前者。这类结构性答案正是 u10-l4 高级解析器的用武之地。

**顺序即正确性**——把整条流水线的依赖关系串起来看：

| 依赖 | 原因 |
| --- | --- |
| ④（去 `^\circ`）必须先于 ⑫（`^`→`**`） | 否则 `30^{\circ}` 先变 `30**{circ}`，角度规则失配 |
| ⑦（`\dfrac`→`\frac`）必须先于 ⑪（分数转换） | 分数规则的 pattern 只认 `\frac` |
| ⑪⑩（分数、根式）必须先于 ⑮（删花括号） | 它们的 pattern 依赖 `{}` 定位分子分母与根号内容 |
| ⑫（`^`→`**`）必须先于 ⑬（混合数字） | 混合数字在幂记号已统一后才好处理（例如 `x^2 1/2` 场景） |

仓库测试 [tests/test_ch03.py:178-208](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L178-L208) 的十三条权威样例（节选）：

| 输入 | 期望输出 | 命中的工位 |
| --- | --- | --- |
| `"  3/4  "` | `3/4` | ⑮ strip |
| `"$2/3$"` | `2/3` | ⑨ 删 `$` |
| `r"\( 2/3 \)"` | `2/3` | ⑥ 剥定界符 |
| `r"\left(1,\,2\right)"` | `(1,2)` | ⑦ 删 `\left` `\right` 和 `\,` |
| `r"\frac{3}{4}"` | `(3)/(4)` | ⑪ |
| `r"\dfrac{14}{3}"` | `(14)/(3)` | ⑦ 再 ⑪（顺序依赖！） |
| `"c. 3"` / `"b: 2"` | `3` / `2` | ③ |
| `r"\sqrt{2}"` | `sqrt(2)` | ⑩ |
| `r"{x}"` / `r"{ (1, 2) }"` | `x` / `(1, 2)` | ⑮ 删花括号 |

#### 4.3.4 代码实践

**实践目标**：把流水线当「黑盒显微镜」用——喂入一组覆盖各工位的文本，观察逐条归一结果；再用一个**自复制的实验**体会「顺序即正确性」。

**操作步骤**：

1. 新建 `normalize_practice.py`（示例代码，勿改仓库源码）：

   ```python
   import re
   from reasoning_from_scratch.ch03 import normalize_text

   cases = [
       r"30^{\circ}",          # 角度
       "30°",                   # Unicode 角度
       r"\dfrac{14}{3}",       # dfrac 分数
       r"2 \cdot 3",           # cdot 乘号
       "2²",                    # Unicode 上标
       "2^{10}",                # LaTeX 幂
       "1,234",                 # 千分位
       "1 1/2",                 # 混合数字
       r"\left( 5 \right)",    # left/right
       "The answer is 42",      # 普通文本（观察 lower()）
   ]
   for t in cases:
       print(f"{t!r:28} -> {normalize_text(t)!r}")
   ```

2. 运行并记录输出。
3. **顺序实验**（不改源码，在脚本里复制规则做对照）：把 `text.replace("^", "**")` 先执行、再执行 `re.sub(r"\^\s*\{\s*\\circ\s*\}", "", text)`，输入 `30^{\circ}`，观察结果与官方顺序的差异。

**需要观察的现象**：

- `2^{10}` 的输出应为 `2**10`（工位 ⑫ 转 `**`，工位 ⑮ 删花括号）。
- `"The answer is 42"` 输出应为全小写 `"the answer is 42"`（工位 ⑮ 的 `lower()`）。
- 顺序实验中，官方顺序得到 `30`，颠倒顺序后 `^\circ` 规则再也匹配不上（`^` 已变成 `**`），残留 `30**{circ}` 再被删花括号成 `30**circ`——彻底坏掉。

**预期结果**：上表各行的具体输出，除 `30^{\circ}`→`30`、`30°`→`30`、`2²`→`2**2`、`1,234`→`1234`、`1 1/2`→`1+1/2`、`2 \cdot 3`→`2*3`、`\left( 5 \right)`→`(5)` 可由源码规则直接推出外，其余以本地实际运行输出为准（本讲未在编写环境执行，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`normalize_text(r"\frac{1}{2}")` 输出 `(1)/(2)` 而不是 `1/2`。为什么要给分子分母**都**加括号？

<details>
<summary>参考答案</summary>

为了让字符串结果作为 SymPy/Python 表达式时保持正确的运算优先级。分子分母可能是复合表达式：`\frac{1+1}{2}` 若不加括号会变成 `1+1/2`，数值从 \(1\) 变成 \(1.5\)。统一 `(a)/(b)` 对任意嵌套都安全。
</details>

**练习 2**：输入 `"(1,234)"`（无空格元组）会被归一化成什么？这暴露了什么问题？

<details>
<summary>参考答案</summary>

千分位规则 `(?<=\d),(?=\d\d\d(\D|$))` 命中（逗号后是 `234` 再接 `)`），输出 `(1234)`——元组被误并为一个大数。这暴露了「千分位逗号」与「元组分隔逗号」在无空格书写下的天然歧义，简单正则无法两全；结构化答案是 u10-l4 混合解析器（`bonus/parser.py`）要解决的问题。（依据正则的手推结论，待本地验证。）
</details>

**练习 3**：`normalize_text` 为什么在最后做 `.lower()`？这会引入什么风险？

<details>
<summary>参考答案</summary>

让 `X` 与 `x`、`A` 与 `a` 这类仅大小写不同的变量书写归一，提升字符串层面的命中率。风险是数学上 `X` 和 `x` 未必是同一个变量（某些题目会同时使用两者），大小写合并可能把两个不同变量判成相等——在「宽容误判对」与「漏判对」之间，实现选择了前者。
</details>

## 5. 综合实践

把三个函数串成一张完整的「输入 → 提取 → 归一」对照表，这是本讲的收官实践，也是本讲规格中指定的任务。

**实践目标**：用至少 8 条精心挑选的测试文本，一次性观察三个函数的协作；并构造一条让 `get_last_boxed` 因大括号不配平返回 `None` 的样例，追踪它如何在 `extract_final_candidate` 中触发回退。

**操作步骤**：

1. 新建 `pipeline_practice.py`（示例代码）：

   ```python
   from reasoning_from_scratch.ch03 import (
       get_last_boxed, extract_final_candidate, normalize_text,
   )

   texts = [
       # —— 有 boxed ——
       r"So we get \boxed{\frac{3}{4}} as the final answer.",   # 嵌套括号
       r"The result is $\boxed{ 2/5 }$, done.",                 # 数学定界符包裹
       r"\boxed{30^{\circ}}",                                    # 带角度
       r"\boxed{(1, 2)}",                                        # 元组
       # —— 无 boxed，靠回退 ——
       "The answer is 42",                                       # 纯文本答案
       "Final Answer: 3.14",                                     # 小数回退
       "I have no idea.",                                        # 无数字
       # —— boxed 存在但不配平 ——
       r"Truncated: \boxed{\frac{1}{2}",                         # 不配平 → None
   ]

   for t in texts:
       boxed = get_last_boxed(t)
       extracted = extract_final_candidate(t)
       normalized = normalize_text(extracted)
       print(f"raw      : {t!r}")
       print(f"boxed    : {boxed!r}")
       print(f"extracted: {extracted!r}")
       print(f"normalized: {normalized!r}")
       print("-" * 60)
   ```

2. 运行脚本，把输出整理成一张四列对照表（原文本 / boxed / 提取结果 / 归一结果）。
3. 重点核对第八条：`get_last_boxed` 返回 `None` 后，`extract_final_candidate` 走了哪条回退、拿到了什么。

**需要观察的现象**：

- 第一条：嵌套大括号被完整取出，提取结果是 `\frac{3}{4}`（LaTeX 原样），归一后才变成 `(3)/(4)`——再次印证「提取与归一解耦」。
- 第五条：`The answer is 42` 无 boxed，数字回退给出 `42`，归一后为 `42`。
- 第六条：`RE_NUMBER` 的小数分支命中，提取 `3.14`。
- 第七条：默认模式下提取结果是**全文**，归一后为小写长串——判分大概率失败，但流程不会崩溃。
- 第八条：boxed 为 `None`；数字回退在 `\frac{1}{2}` 的花括号里找到 `1` 和 `2`，取最后一个，提取结果为 `2`（手推结论，待本地验证）。

**预期结果**：一张 8 行对照表；其中前四行的提取结果可直接对照 4.2.3 的测试样例表验证正确性，后四行的回退路径按 4.2.2 的流程图逐一解释。若某行结果与你的手推不符，回到对应工位的正则逐字符比对——这本身就是最好的正则阅读训练。

**扩展（可选）**：把表格中的「归一结果」再送进 `grade_answer(pred, "0.75")`（u4-l3 的黑盒预演），看 `(3)/(4)` 这类写法能否被判对，提前感受符号等价判定的威力。

## 6. 本讲小结

- `get_last_boxed` 用**括号深度计数**从最后一个 `\boxed` 中提取嵌套内容；`\boxed` 缺失、缺 `{`、括号不配平三种情况一律返回 `None`，且**不会**回退到前一个配平的 `\boxed`。
- `extract_final_candidate` 是三级回退保险丝：boxed 优先 → 全文**最后一个**数字（`RE_NUMBER` 认识分数/整数/小数/科学计数法）→ 视 `fallback` 返回全文（`number_then_full`）、空串（`number_only`）或彻底不回退（`None` 等白名单外取值）。
- **提取与归一化是解耦的**：`extract_final_candidate` 保留 LaTeX 原文；`normalize_text` 由 `grade_answer` 在判分时对预测与标准答案**对称**调用。
- `normalize_text` 是一条**顺序敏感**的十五工位正则流水线：去特殊 token → 去角度单位 → 拆 `\text` → 剥定界符 → LATEX_FIXES → 上标转 `**` → 去 `%`/`$` → `sqrt()` 化 → 分数 `(a)/(b)` 化 → `**` 幂 → 带分数 → 千分位 → 删残余花括号 + 转小写。
- 流水线的每一处顺序都有原因：`^\circ` 必须先于 `^`→`**`，`\dfrac`→`\frac` 必须先于分数规则，所有依赖花括号的规则必须先于最终删花括号。
- 简单正则有已知盲区（嵌套分数、无空格元组 `(1,234)`、单字母开头的文本被砍头），u10-l4 的混合解析器将针对性补强。

## 7. 下一步学习建议

下一讲 **u4-l3 SymPy 数学等价性判定** 将顺着本讲的流水线继续向下：`normalize_text` 的输出会被送进 `sympy_parser`（隐式乘法转换、超长表达式保护）解析成 SymPy 对象，再由 `equality_check` 用「字符串捷径 + `simplify(a-b) == 0` 符号差值判定」比较等价性，多元答案则由 `split_into_parts` 拆分对齐。建议先阅读 [reasoning_from_scratch/ch03.py:296-381](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L296-L381)，并思考一个问题：为什么 `grade_answer` 需要「字符串相等」和「符号等价」**两道**检查？带着它进入下一讲。

再往后，u4-l4 会把本讲的提取器接进 `evaluate_math500_stream` 的端到端评测循环；第 5、6 章则会反复复用 `extract_final_candidate` 的三级回退（分别用作投票候选和奖励分档）。
