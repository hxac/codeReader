# 代码奖励函数与执行脚本模板

## 1. 本讲目标

本讲是「代码奖励与沙箱执行」单元的第一篇。在前面 GRPO 单元里，我们见过的奖励（`accuracy_reward`、`format_reward`、`len_reward`……）都是**纯函数**：输入文本、输出分数，不运行任何外部代码。但当任务是「写一段程序」时，光看文本无法判断对错——**判断程序对错的唯一可靠办法就是把它跑起来，喂测试用例，看输出**。

open-r1 用一组函数把这件事做成了 GRPO 的奖励信号。学完本讲，你应当能够：

1. 说清 `code_reward` 为什么返回**连续的** `success_rate`（通过率），而 `binary_code_reward` 又如何把它**二值化**，以及两者各自适合什么训练阶段。
2. 掌握 `extract_code` 如何用一条正则从模型的 Markdown 输出里抽出代码，并理解「取最后一个代码块」的设计意图。
3. 读懂 `evaluation_script_template` 这段内嵌脚本：它是奖励的真正「判分逻辑」，用 `stdin/stdout` 比对给候选程序打分。你能脱离沙箱，在本地手动渲染并运行它、核对 `success_rate`。

> 前置认知（来自 u3-l2）：GRPO 通过 `get_reward_funcs` 把 YAML 里的字符串名翻译成可调用函数；本讲涉及的 `"code"` 与 `"binary_code"` 就是注册表里的两个条目，由 `partial` 焊入超参。本讲只拆「函数内部怎么打分」，不再重复注册表机制。

## 2. 前置知识

- **奖励函数的签名约定**：open-r1 的奖励函数都形如 `func(completions, **kwargs) -> list[float]`。`completions` 是一个 batch 的模型输出；`**kwargs` 里塞的是从数据集「按列注入」的字段（如 `verification_info`、`solution`）。这一点在 u3-l2 已建立。
- **什么是「可验证的编程题」**：题目给出若干 `test_cases`，每个用例有 `input`（标准输入）和 `output`（期望输出）。候选程序从标准输入读数据、向标准输出写结果。这种「读 stdin、写 stdout」的判题方式来自**竞技编程（competitive programming）**，也是本讲 `evaluation_script_template` 的判分基础。
- **沙箱（sandbox）**：直接在本机跑模型生成的代码很危险（可能删文件、发网络请求）。所以代码奖励把候选程序放进一个**隔离的执行环境**（沙箱）里跑。open-r1 默认用 [E2B](https://e2b.dev) 或 MorphCloud 这类远端沙箱；本讲关注「判分逻辑」，沙箱 Provider 的细节留给 u5-l2。
- **表达式求值即返回值**：E2B / MorphCloud 的沙箱像 Jupyter Notebook 一样，会把脚本**最后一个表达式的值**当作输出捕获。这是理解模板末尾「为什么没有 `print`」的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/open_r1/rewards.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py) | 所有奖励函数的大本营。本讲聚焦其中的 `extract_code`、`code_reward`、`binary_code_reward` 三个函数，以及 `code_reward` 内嵌的 `evaluation_script_template`。 |
| [src/open_r1/utils/code_providers.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py) | 代码执行的 Provider 抽象（`CodeExecutionProvider`、`E2BProvider`、`MorphProvider`、`get_provider`）。本讲只用到「它把渲染好的脚本送进沙箱、再 `float(结果)` 回收奖励」这一层接口。 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | `GRPOScriptArguments` 里和代码奖励有关的超参（`code_provider`、`parallel_code_exec_per_proc` 等），它们经注册表流入 `code_reward`。 |
| [tests/slow/test_code_reward.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py) | 代码奖励的慢测试。它给出「怎么构造 `completions` 与 `verification_info` 喂给 `code_reward`」的最权威用法示例。 |

## 4. 核心概念与源码讲解

本讲按数据流自上而下拆成三个最小模块：

1. 先用 `extract_code` 从模型输出里**抽出代码**；
2. 再把代码塞进 `evaluation_script_template`，**渲染成一段可执行的判分脚本**；
3. 这段脚本在沙箱里跑出 `success_rate`，由 `code_reward` 回收；`binary_code_reward` 再把它**二值化**。

### 4.1 code_reward 与 binary_code_reward 的关系

#### 4.1.1 概念说明

判断一段程序「对不对」可以有两种粒度：

- **连续粒度（partial / 通过率）**：通过 `k` 个测试用例就得 `k/total` 分。一道题 10 个用例对了 7 个 → 0.7 分。这给模型一个**稠密、平滑的梯度**——「更接近正确」能拿到更高分，RL 才有方向可优化。
- **二值粒度（pass/fail）**：只有「几乎全对」才算 1.0，否则 0.0。信号稀疏，但更贴近「这道题到底做没做对」的人类直觉，也常用作最终评估。

open-r1 同时提供两者：`code_reward` 返回连续的 `success_rate`，`binary_code_reward` 在它之上套一个阈值门槛做二值化。两者**共享同一套判分内核**（都跑同一个 `evaluation_script_template`），区别只在最后怎么把分数映射出去。

一个关键约束：两者都**依赖数据集里的 `verification_info` 列**——它携带了测试用例和语言信息。没有它，代码奖励无从打分。

#### 4.1.2 核心流程

```text
binary_code_reward(completions, ...)
        │
        │  先调用 code_reward 拿到连续分
        ▼
   rewards = code_reward(...)            # 形如 [1.0, 0.7, 0.0, None, ...]
        │
        │  逐个过阈值 BINARY_THRESHOLD = 0.99
        ▼
   reward > 0.99 ? 1.0 : 0.0            # [1.0, 0.0, 0.0, None, ...]
        │  （None 原样透传，不二值化）
        ▼
   return output
```

为什么阈值是 `0.99` 而不是 `1.0`？`success_rate = passed / total` 是精确除法。要让它**严格大于** `0.99`，对于常见的几十个测试用例规模，几乎等价于「全部通过」（例如 `total=10` 时只有 `10/10=1.0` 才超过 0.99）。写成 0.99 而非 1.0 更多是一种**防御性写法**，对整数计数并无实质区别。

#### 4.1.3 源码精读

`binary_code_reward` 的全部逻辑就是「调用 `code_reward` + 阈值化」，非常薄：

[binary_code_reward 定义与二值化逻辑 · src/open_r1/rewards.py#L485-L508](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L485-L508) —— 第 492–498 行直接把入参透传给 `code_reward`；第 499 行定义阈值；第 502–506 行逐项映射，其中第 506 行 `1.0 if reward > BINARY_THRESHOLD else 0.0` 是二值化的核心。注意第 503–505 行的特判：**`None` 原样保留**，不被转成 0.0。

`None` 的语义至关重要：GRPO 里 `None` 表示「**这个样本跳过、既不奖励也不惩罚**」，与 `accuracy_reward` 解析失败时返回 `None` 的含义一致（见 u3-l2）。所以二值化必须放过 `None`，否则会把「无法判分」误当成「答错」。

`code_reward` 的骨架（判分内核留到 4.3 详讲）：

[code_reward 主流程 · src/open_r1/rewards.py#L511-L592](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L511-L592) —— 它做了四件事：第 569 行用 `extract_code` 从每条输出抽代码；第 570 行取 `verification_info`；第 574–577 行把代码与测试用例**渲染进模板**生成一批脚本；第 586–592 行用 Provider 把脚本送进沙箱执行并回收分数。

第 579–584 行的 `enforce_same_language` 是个可选校验：开启时会检查整个 batch 的 `verification_info` 是否同语言，不同就抛错——用于「纯单语言训练」时尽早暴露数据混入。

最后，注册表里两者都用 `partial` 焊入超参（来自 `GRPOScriptArguments`）：

[注册表中的 "code" 与 "binary_code" · src/open_r1/rewards.py#L663-L680](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L663-L680) —— 可以看到 `provider_type=script_args.code_provider`、`num_parallel=script_args.parallel_code_exec_per_proc` 被固定进闭包，所以在 YAML 里改 `code_provider: e2b` 就能切换沙箱，无需动代码。

#### 4.1.4 代码实践

这是一个**纯推理型**小练习（无需沙箱），用来检验你对「二值化」与 `None` 语义的理解。

1. **实践目标**：在不运行任何沙箱的前提下，准确预测 `binary_code_reward` 的输出。
2. **操作步骤**：假设 `code_reward` 对某 batch 返回了 `rewards = [1.0, 0.7, 0.0, None, 0.995]`，你手算 `binary_code_reward` 应得到的结果列表。
3. **需要观察的现象**：哪些项变成 1.0、哪些变成 0.0、哪一项被原样保留。
4. **预期结果**：`[1.0, 0.0, 0.0, None, 1.0]`。逐项解释：`1.0 > 0.99 → 1.0`；`0.7、0.0` 不大于 0.99 → `0.0`；`None` 透传；`0.995 > 0.99 → 1.0`。
5. 此为纯逻辑推演，结果确定，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：一道题共 10 个测试用例，候选程序通过了 7 个。`code_reward` 与 `binary_code_reward` 分别返回多少？

> **答案**：`code_reward` 返回 `7/10 = 0.7`；`binary_code_reward` 因 `0.7` 不大于 `0.99`，返回 `0.0`。这正是「连续奖励在训练期更有用、二值奖励更严格」的直观体现。

**练习 2**：为什么 `binary_code_reward` 要把 `None` 原样透传，而不是当成 0.0？

> **答案**：`None` 在 GRPO 里的语义是「跳过该样本」，由 `GRPOTrainer` 过滤掉、不参与优势估计。若把它改成 0.0，就等于把「无法判分」（比如测试用例缺失、沙箱出错）误判为「答错」，给模型施加了错误的负梯度。

### 4.2 extract_code：从 Markdown 代码块抽取代码

#### 4.2.1 概念说明

模型在 GRPO 里产出的 `completion` 通常是一段**混合了自然语言、思考标签和代码**的 Markdown 文本，比如：

```text
<think>先读两个整数……</think>
<answer>
```python
a, b = map(int, input().split())
print(a + b)
```
</answer>
```

但 `code_reward` 要把**纯代码**送进沙箱。`extract_code` 就是这个「剥壳」函数：用正则找到所有形如 ```` ```python ... ``` ```` 的代码围栏（fenced code block），取出代码内容。

两个设计决策值得注意：

- **按语言过滤**：正则里带 `language` 参数（默认 `python`），只抓指定语言的围栏。若 `language=None`，直接返回空串——上游据此「跳过判分」。
- **取最后一个**：当输出里有多个代码块时，`extract_code` 取**最后一个**。这契合一种常见习惯——模型先写一段探索性/示例代码，最后才给出正式解答；把「最后一个」当作最终答案。

#### 4.2.2 核心流程

```text
输入：completion（Markdown 文本）、language（默认 "python"）
        │
        │  language is None ?
        ├─ 是 ─→ return ""（跳过判分）
        ▼
   正则 rf"```{language}\n(.*?)```"  +  re.DOTALL
        │  （DOTALL 让 . 匹配换行，跨多行抓代码）
        ▼
   matches = pattern.findall(completion)
        │
        │  有匹配？
        ├─ 有 ─→ return matches[-1]   # 最后一个代码块
        └─ 无 ─→ return ""
```

#### 4.2.3 源码精读

`extract_code` 只有 7 行，但每行都有讲究：

[extract_code 定义 · src/open_r1/rewards.py#L476-L482](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L476-L482)

- 第 477–478 行：`language is None` 时直接 `return ""`。注意 `code_reward` 调用它时用的是默认参数（第 569 行 `extract_code(completion[-1]["content"])`，未传 language），所以默认走 Python 分支。
- 第 479 行：正则 `` rf"```{language}\n(.*?)```" ``。关键点有三：① 以 `` ```{language}\n `` **开头**（要求紧跟语言标记和换行），保证只匹配指定语言的围栏；② `(.*?)` 是**非贪婪**捕获，遇到第一个闭合的 `` ``` `` 就停，避免把多个代码块并成一个；③ `re.DOTALL` 让 `.` 能匹配换行符，否则多行代码只能抓到第一行。
- 第 480–481 行：`findall` 返回所有匹配的代码内容列表，`matches[-1]` 取**最后一个**；无匹配则 `""`。

> ⚠️ 一个易踩的坑：正则要求 `` ```python `` **后面紧跟一个换行**。如果模型输出 `` ```pythonprint(1) `` （语言标记后没有换行），这里不会匹配，会被当成「无代码」。这种格式细节在排错时常被忽略。

#### 4.2.4 代码实践

1. **实践目标**：验证「取最后一个代码块」与「按语言过滤」两条行为。
2. **操作步骤**：在装好 open-r1（`make install`，见 u1-l3）的环境里启动 `python`，执行：
   ```python
   from open_r1.rewards import extract_code
   completion = """思考：先试一种写法。
   ```python
   print("draft")
   ```
   最终答案：
   ```python
   a, b = map(int, input().split())
   print(a + b)
   ```
   """
   print(extract_code(completion))              # 期望取到第二段
   print(extract_code(completion, "javascript")) # 期望返回 ""
   print(extract_code("没有任何代码的纯文本", "python")) # 期望返回 ""
   ```
3. **需要观察的现象**：第一次调用返回的是「最终答案」那段代码（含 `a + b`），而非 `print("draft")`；后两次返回空串。
4. **预期结果**：`'a, b = map(int, input().split())\nprint(a + b)\n'`、`''`、`''`。
5. 若本地未装 open-r1，可把 `extract_code` 的 7 行源码复制进脚本直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：模型输出里同时有 ```` ```python ```` 和 ```` ```javascript ```` 各一块。`extract_code(completion)`（默认参数）会返回哪一段？为什么？

> **答案**：返回 Python 那一块。默认 `language="python"`，正则只匹配 ```` ```python\n...``` ```` 围栏，JavaScript 块被忽略。

**练习 2**：把 `re.DOTALL` 去掉会发生什么？

> **答案**：`.` 不再匹配换行，多行代码只能捕获到第一行（直到第一个 `\n` 之前）。因此对绝大多数真实代码，`extract_code` 会抽到残缺片段，导致后续判分失败。`re.DOTALL` 是这段正则能工作的前提。

### 4.3 evaluation_script_template：在沙箱里跑测试用例

#### 4.3.1 概念说明

这是代码奖励的**真正判分内核**。`code_reward` 自己并不懂怎么判分——它的聪明之处在于：**不直接判分，而是动态拼出一段「判分脚本」，把这段脚本和候选代码一起丢进沙箱执行**，沙箱返回的数字就是奖励。

这段判分脚本就是内嵌在 `code_reward` 函数体里的字符串常量 `evaluation_script_template`。它做三件事：

1. 定义一个 `evaluate_code(code, test_cases)` 函数：把候选 `code` 当成一个独立程序，用 `subprocess.run(["python3", "-c", code], input=case["input"])` 逐个跑测试用例；
2. 把候选代码、测试用例**渲染**进模板（`{code}`、`{test_cases}` 占位符）；
3. 末尾调用 `evaluate_code(...)`，其返回值 `success_rate` 成为脚本「最后一个表达式的值」，被沙箱捕获、被 Provider 用 `float()` 转成奖励。

判分规则是**逐行精确字符串比对**：把候选程序的标准输出（`.strip()` 后）与期望输出按行 `zip` 比较，全部一致才算该用例通过；用例数 `passed / total` 即 `success_rate`。代码里有一行 `TODO` 注释坦白：这只是朴素的逐行比对，没有做真正的 validator（对浮点格式、空白、多解顺序都不宽容）。

#### 4.3.2 核心流程

判分单条样本的执行过程（伪代码）：

```text
对每个 (code, info) in zip(code_snippets, verification_info):
    # 1. 渲染：把 code、test_cases 安全嵌入模板
    script = template.format(
        code        = json.dumps(code),                          # 单层：变成带引号的字符串字面量
        test_cases  = json.dumps(json.dumps(info["test_cases"])) # 双层：先序列化成 JSON 文本，再包成字符串字面量
    )
    # 2. 送沙箱执行 script；模板末行 evaluate_code(...) 的返回值即奖励
    reward = sandbox.run(script)   # Provider 内部 float(返回值)
```

`evaluate_code` 内部对**单个测试用例**的判定：

```text
process = subprocess.run(["python3", "-c", code], input=case["input"], timeout=5)
if process.returncode != 0:   # 程序崩溃/异常 → 该用例不通过，跳过
    continue
逐行 zip 比对 process.stdout.strip() 与 case["output"]（每行也 strip）
全部一致 → passed += 1
最终 success_rate = passed / total
```

**为什么 `test_cases` 要双层 `json.dumps`，而 `code` 只单层？** 这是本讲最精巧的一处：

- `code` 经 `json.dumps(code)` 变成一个**合法的 JSON 字符串字面量**（带引号、转义好换行与引号），直接嵌入 `code_snippet = {code}`，Python 把它当成一个普通字符串对象——既安全又省事。
- `test_cases` 是一个**列表/字典结构**。模板里写的是 `test_cases = json.loads({test_cases})`：它期望 `{test_cases}` 处填入一个**字符串字面量**，再用 `json.loads` 把这个字符串解析回列表。因此要先 `json.dumps` 把列表变成 JSON 文本，**再** `json.dumps` 一次把这个文本包成带引号的字面量。双层序列化保证了「任意内容（含换行、引号、甚至恶意片段）都能被安全地塞进模板并被正确还原」，避免了字符串拼接注入。

#### 4.3.3 源码精读

`evaluation_script_template` 全文（注意末行没有 `print`）：

[evaluation_script_template 内嵌判分脚本 · src/open_r1/rewards.py#L529-L567](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L529-L567)

- 第 533 行定义 `evaluate_code(code, test_cases)`。
- 第 539–545 行是判分的核心：`subprocess.run(["python3", "-c", code], input=case["input"], text=True, capture_output=True, timeout=exec_timeout)`——把候选代码当成独立进程，从标准输入喂入用例的 `input`，超时 5 秒。
- 第 547 行：`if process.returncode != 0: continue`——程序异常退出（抛错、超时被杀）直接判该用例不过。
- 第 552–556 行：逐行精确比对（第 554 行 `zip(output.split('\\n'), case['output'].split('\\n'))`），第 552 行的 `TODO` 注释自承「暂时只做逐行精确匹配，没有正经 validator」。
- 第 560 行：`success_rate = passed / total`。
- 第 563–564 行：两个占位符 `{code}`、`{test_cases}` 的注入点（后者外层套 `json.loads`）。
- 第 566 行：`evaluate_code(code_snippet, test_cases)` 是**最后一条语句、且是表达式**——这正是沙箱能捕获其值的原因。

`code_reward` 如何渲染并送执行：

[渲染与执行 · src/open_r1/rewards.py#L569-L592](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L569-L592) —— 第 574–577 行的列表推导为 batch 里每条样本生成一个完整脚本；第 586–590 行用 `get_provider(...)` 拿到执行 Provider；第 592 行 `execute_scripts(scripts, ["python"] * len(scripts))` 一次性把整批脚本送进沙箱。

回收奖励的一侧在 Provider 里：

[E2BProvider.execute_scripts 回收奖励 · src/open_r1/utils/code_providers.py#L82-L113](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L82-L113) —— 第 101 行 `reward = float(execution.text)` 把沙箱捕获的「末行表达式值」转成浮点奖励；转换失败（第 102–104 行）则记 `None`（与 4.1 里 `None` 的语义呼应）。`execute_scripts` 的抽象接口定义在 [CodeExecutionProvider.execute_scripts · src/open_r1/utils/code_providers.py#L49-L60](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L49-L60)。

> 提醒：`GRPOScriptArguments.code_provider` 的 `choices` 写了 `["e2b", "local", "morph"]`（见 [configs.py#L308-L314](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L308-L314)），但 `get_provider` 目前**只实现了 `"e2b"` 与 `"morph"`**（见 [code_providers.py#L339-L366](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L339-L366)），传 `"local"` 会抛 `ValueError`。也就是说「本地 Provider」是一个尚未接线的预留选项，本讲的「本地执行」实践是**手动复刻**，而非调用这个尚不存在的 Provider。

#### 4.3.4 代码实践（本讲主实践，可脱离沙箱运行）

模板本质就是一段普通 Python（用 `subprocess` 跑候选代码），所以**无需 E2B/Morph，在本地就能完整复现判分过程**。这正是理解代码奖励最快的方式。

1. **实践目标**：手动渲染 `evaluation_script_template`、本地用 `python3` 执行，核对 `success_rate`，从而亲眼看到「候选程序 → 通过率 → 奖励」的整条链路。
2. **操作步骤**：把下面这段脚本存为 `render_and_run.py`（注意：这是**示例代码**，不是 open-r1 源码，但 `extract_code` 与模板均逐字照搬自 `rewards.py`）：
   ```python
   # 示例代码：本地复现 code_reward 的判分内核
   import json, re, subprocess, sys

   # ---- 1) 逐字照搬 src/open_r1/rewards.py#L476-L482 ----
   def extract_code(completion, language="python"):
       pattern = re.compile(rf"```{language}\n(.*?)```", re.DOTALL)
       matches = pattern.findall(completion)
       return matches[-1] if len(matches) >= 1 else ""

   # ---- 2) 逐字照搬 src/open_r1/rewards.py#L529-L567 的模板 ----
   evaluation_script_template = """
       import subprocess
       import json

       def evaluate_code(code, test_cases):
           passed = 0
           total = len(test_cases)
           exec_timeout = 5
           for case in test_cases:
               process = subprocess.run(
                   ["python3", "-c", code],
                   input=case["input"],
                   text=True,
                   capture_output=True,
                   timeout=exec_timeout
               )
               if process.returncode != 0:
                   continue
               output = process.stdout.strip()
               all_correct = True
               for line1, line2 in zip(output.split('\\n'), case['output'].split('\\n')):
                   all_correct = all_correct and line1.strip() == line2.strip()
               if all_correct:
                   passed += 1
           success_rate = (passed / total)
           return success_rate

       code_snippet = {code}
       test_cases = json.loads({test_cases})
       print(evaluate_code(code_snippet, test_cases))   # 本地复现时加 print 以观察返回值
       """

   # ---- 3) 构造样本：模型输出 + verification_info ----
   completion = """<think>读两个整数求和。</think>
   <answer>
   ```python
   a, b = map(int, input().split())
   print(a + b)
   ```
   </answer>"""
   info = {
       "language": "python",
       "test_cases": [
           {"input": "1 2\n", "output": "3\n"},
           {"input": "10 20\n", "output": "30\n"},
           {"input": "5 5\n", "output": "10\n"},
       ],
   }

   # ---- 4) 复刻 rewards.py#L574-L577 的渲染（注意双层 json.dumps）----
   code = extract_code(completion)
   script = evaluation_script_template.format(
       code=json.dumps(code),
       test_cases=json.dumps(json.dumps(info["test_cases"])),
   )

   # ---- 5) 本地执行渲染后的脚本 ----
   result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
   print("success_rate =", result.stdout.strip())   # 期望 1.0
   ```
3. **需要观察的现象**：脚本最后打印 `success_rate = 1.0`，说明候选程序通过了全部 3 个用例。
4. **预期结果**：`success_rate = 1.0`。
5. **延伸观察**（确定性结果）：把候选代码改成 `print(a - b)`（错解），重跑应得到 `success_rate = 0.0`；把第二个用例的期望输出改成 `31`（故意改错），应得到 `success_rate = 0.666...`（2/3）。

> 🔎 为什么示例代码在末行加了 `print`，而原模板没有？因为 E2B/Morph 沙箱像 Notebook 一样会**自动捕获最后一个表达式的值**（Provider 再 `float()` 之），无需 `print`；而本地用 `python3 -c` 直接跑时，表达式值不会打印，必须显式 `print` 才看得见。这是「沙箱语义」与「普通脚本语义」的关键差异。

#### 4.3.5 小练习与答案

**练习 1**：候选程序在某个用例上抛了异常（`returncode != 0`），该用例怎么算？会对 `success_rate` 产生什么影响？

> **答案**：第 547–548 行 `if process.returncode != 0: continue` 直接跳过该用例，它既不计入 `passed`、也不改变 `total`。因此一个会崩的程序通常 `passed=0`、`success_rate=0.0`（分母 `total` 不变）。

**练习 2**：模板里 `test_cases = json.loads({test_cases})`，渲染时传的是 `json.dumps(json.dumps(info["test_cases"]))`。如果只传**一层** `json.dumps` 会怎样？

> **答案**：一层 `json.dumps` 产出的 `[{"input": ...}]` 是一段「裸 JSON 文本」，没有外层引号；嵌进模板后变成 `test_cases = json.loads([{"input": ...}])`，这在 Python 里是**语法错误**（列表字面量不能直接作 `json.loads` 的实参，且其中的双引号未转义也会破坏语法）。双层序列化正是为了让 `{test_cases}` 处填入一个**带引号的字符串字面量**，使 `json.loads` 能正确解析。

**练习 3**：第 552 行的 `TODO` 自承判分「朴素」。举一个「逐行精确比对」会误判的情形。

> **答案**：`zip` 只比到**较短**的那一方为止。若期望输出是 `3\n4\n`，而候选程序只输出 `3\n`，`zip` 只比较第一行 `3==3`，多余的一行 `4` 被忽略，`all_correct` 仍为 `True`，于是**少输出却判通过**。同理，浮点数 `0.10` 与 `0.1`、行序不同的多解，都会被误判——这正是 `TODO` 想要改进的地方。

## 5. 综合实践

把三个模块串起来，做一个「**本地迷你代码奖励管线**」：给定同一道题（求两数和）的**三种候选解**——完全正确、部分错误、直接崩溃——用 `extract_code` + 模板渲染 + 本地执行，得到三条 `success_rate`，再套用 `binary_code_reward` 的阈值逻辑，观察连续奖励与二值奖励的差异。

```python
# 示例代码：综合实践
import json, re, subprocess, sys

def extract_code(completion, language="python"):
    pattern = re.compile(rf"```{language}\n(.*?)```", re.DOTALL)
    matches = pattern.findall(completion)
    return matches[-1] if len(matches) >= 1 else ""

TEMPLATE = open("template.txt").read()  # 把 4.3.4 里的 evaluation_script_template 存成该文件

def run_one(code, test_cases):
    script = TEMPLATE.format(
        code=json.dumps(code),
        test_cases=json.dumps(json.dumps(test_cases)),
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None  # 复刻 Provider 转换失败 → None 的语义

# 三种候选解
solutions = {
    "correct": "a, b = map(int, input().split())\nprint(a + b)",
    "partial": "a, b = map(int, input().split())\nprint(a - b)",   # 全错
    "crash":   "raise RuntimeError('boom')",                        # 直接崩
}
test_cases = [
    {"input": "1 2\n", "output": "3\n"},
    {"input": "4 5\n", "output": "9\n"},
]
# 用 extract_code 模拟「从带围栏的输出里取代码」
completions = {k: f"```python\n{v}\n```" for k, v in solutions.items()}

for name, comp in completions.items():
    sr = run_one(extract_code(comp), test_cases)
    binary = None if sr is None else (1.0 if sr > 0.99 else 0.0)
    print(f"{name:8s} code_reward={sr}  binary_code_reward={binary}")
```

**操作与预期**：

1. 先按 4.3.4 把模板存为 `template.txt`（**末行保留 `print(evaluate_code(...))`**）。
2. 运行上述脚本，预期输出大致为：
   ```text
   correct  code_reward=1.0  binary_code_reward=1.0
   partial  code_reward=0.0  binary_code_reward=0.0
   crash    code_reward=0.0  binary_code_reward=0.0
   ```
3. 把 `partial` 的代码改成「只对一个用例正确」（如对 `1 2` 输出 `3`、对 `4 5` 仍输出 `-1`），重跑应看到 `code_reward=0.5`、`binary_code_reward=0.0`——**这正是连续奖励能区分「半对」、而二值奖励只在几乎全对时给分的核心差异**。
4. 思考：若把 `TEMPLATE` 末行的 `print` 去掉，本地为何就观测不到 `success_rate`？（答：本地普通脚本不会捕获表达式值；只有沙箱/Notebook 才会。）

## 6. 本讲小结

- `code_reward` 返回**连续**的 `success_rate = passed/total`，给 RL 提供稠密梯度；`binary_code_reward` 在其上套 `> 0.99` 阈值做**二值化**，两者共享同一判分内核。
- `None` 在两者中都**原样透传**，语义是「跳过样本、不奖不罚」，不可误当成 0.0。
- `extract_code` 用正则 `` ```{language}\n(.*?)``` ``（`re.DOTALL`、非贪婪）抓代码，取**最后一个**代码块作为最终答案；`language=None` 返回空串以跳过判分。
- `code_reward` 的判分逻辑不在自身，而在动态渲染的 `evaluation_script_template`：把候选代码当独立进程、按 `stdin/stdout` 逐行精确比对，`passed/total` 即奖励。
- **双层 `json.dumps`** 安全地把测试用例嵌入模板（单层用于 `code`），末行表达式靠沙箱的「自动捕获」变成返回值，Provider 用 `float()` 回收。
- 判分目前是**朴素逐行比对**（代码自带 `TODO`），对少输出、浮点格式、多解顺序会误判；这是后续可改进的方向。

## 7. 下一步学习建议

本讲把「判分逻辑」讲透了，但刻意把「脚本怎么送进沙箱」当成黑盒。建议接着：

- **u5-l2 代码执行 Provider 抽象**：拆开 `CodeExecutionProvider` 抽象基类与 `E2BProvider`/`MorphProvider`，看清 `execute_scripts` 背后的**异步并发、信号量限流、超时与重试**是怎么把成千上万个渲染脚本安全跑完的。
- **u5-l3 Router 路由沙箱与限流**：当多机训练同时压向 E2B 时，`RoutedSandbox` 如何用 `/execute_batch` 批量接口与共享 router 规避沙箱限流。
- 若你对**竞技编程判题**更感兴趣，可直接跳到第六单元：u6-l1（IOI 评分）与 u6-l2（Codeforces 评分）会在本讲的「单程序 stdin/stdout 比对」之上，引入**子任务（subtask）、分批评测早停、三种 scoring_mode** 等更接近真实赛制的判分体系。

建议带着本讲的 `render_and_run.py` 实践代码继续：当你能在本地复现 `success_rate`，再去读 Provider 与 Router 时，就能把注意力全部放在「规模化执行」而非「怎么判分」上。
