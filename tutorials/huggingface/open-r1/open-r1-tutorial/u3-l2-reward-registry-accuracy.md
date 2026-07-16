# 奖励函数注册表与数学正确性奖励

## 1. 本讲目标

读完本讲，你应当能够：

- 说清 `get_reward_funcs(script_args)` 这一行背后的「字符串名 → 可调用函数」机制：`REWARD_FUNCS_REGISTRY` 是什么、什么时候被构建、为什么里面有三种形态各异的「值」。
- 逐行读懂 `accuracy_reward`：它如何用 `math_verify` 的 `parse` + `LatexExtractionConfig` + `verify` 三件套，把一段含 `\boxed{}` 的模型回答判成 `1.0`（对）或 `0.0`（错），以及在什么情况下会返回 `None`（跳过该样本）。
- 解释 `cosine` / `repetition_penalty` / `soft_overlong_punishment` 等「参数化奖励」为什么采用「工厂函数返回闭包」的写法，以及这些参数是怎么从 `GRPOScriptArguments` 一路流到奖励函数内部的。

本讲是强化学习单元（Unit 3）的第二篇。上一篇 `u3-l1` 讲了 `grpo.py` 的主流程骨架，并把 `get_reward_funcs(script_args)` 当成一个**黑盒**——`reward_funcs = get_reward_funcs(script_args)` 拿到一串可调用函数，再塞进 `GRPOTrainer(reward_funcs=...)`。本讲就专门**拆开这个黑盒**，先讲注册表机制，再精读其中最基础、也最重要的 `accuracy_reward`（数学正确性奖励），最后讲清「参数化奖励」的工厂模式。其余奖励函数（`format` / `tag_count` / `reasoning_steps`、`len` / `cosine` / `repetition`、代码类奖励）的细节分别在 `u3-l3`、`u3-l4`、Unit 5 展开，本讲不重复。

## 2. 前置知识

进入源码前，先建立三个朴素直觉。

**直觉一：奖励函数是「打分函数」，输入是「模型生成的回答」，输出是「一个数字」。**

在 GRPO 里，模型对同一个问题（prompt）采样出 G 个回答（一个「组」），奖励函数给每个回答打分，分数再组内归一化成优势（advantage）去更新策略。所以奖励函数的核心契约是：

\[
\text{reward}(\text{completions}, \ldots) \rightarrow \text{list}[\text{float}]
\]

它一次接收一批回答（`completions`），返回等长的分数列表。分数越高代表这个回答越「好」——「好」的定义完全由你决定：答得对、格式漂亮、推理步骤清晰、长度合适……每一种「好」就是一个奖励函数。open-r1 把这些「好」拆成十几个独立的小函数，让你像点菜一样在 YAML 里挑选组合。

**直觉二：为什么要「字符串名 → 函数」的注册表？**

因为 YAML 配方里只能写文本（`reward_funcs: [accuracy, format]`），而训练器需要的是真正的 Python 函数对象。中间必须有一张「翻译表」，把字符串 `"accuracy"` 翻译成函数 `accuracy_reward`。这张表就是 `REWARD_FUNCS_REGISTRY`。这样做的好处是：配方可读、可版本化、可 diff，而真正的打分逻辑集中在 `rewards.py` 一个文件里。

**直觉三：为什么判数学对错这么麻烦？**

模型回答和标准答案都是「自然语言 + LaTeX 混排」的文本，比如标准答案是 `\frac{63}{400}`，模型可能写成 `\boxed{\dfrac{63}{400}}`、`63/400`、`0.1575`……字面上一个字符都不一样，但数学上完全等价。所以不能做字符串比对，必须把双方都解析成「数学对象」再比较等价性。`math_verify` 库就是干这件事的：`parse` 把文本解析成数学表达式，`verify` 判定两个表达式是否等价。

如果你对「三元组配置」「`TrlParser` 把 YAML 与命令行合并成 `GRPOScriptArguments`」还不熟，请先读 `u1-l4`；对 `grpo.py` 主流程不熟，请先读 `u3-l1`。本讲默认你已经知道 `get_reward_funcs(script_args)` 是在 `grpo.py` 第 7 个阶段被调用的。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/open_r1/rewards.py` | 奖励函数库 + 注册表 `get_reward_funcs`。本讲主角，重点看 `accuracy_reward`（L40–L82）与 `get_reward_funcs`（L646–L706），工厂函数 `get_cosine_scaled_reward`（L205–L282）、`get_repetition_penalty_reward`（L285–L354）作对照。 |
| `src/open_r1/configs.py` | 定义 `GRPOScriptArguments`，其 `reward_funcs` 字段与一组 `cosine_*` / `repetition_*` / `code_*` / `soft_*` 超参字段，是注册表与工厂函数的数据来源。 |
| `src/open_r1/grpo.py` | 调用方：`reward_funcs = get_reward_funcs(script_args)`（L88）后注入 `GRPOTrainer`（L114）。本讲只引用这两行，不重复讲主流程。 |
| `tests/test_rewards.py` | 奖励函数的单元测试，含 `test_get_reward_funcs`（验证注册表返回的函数名）与 `accuracy_reward` 的 1.0 / 0.0 断言，是本讲代码实践的依据。 |
| `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml` | 一份可直接跑的 GRPO 配方，本讲用它讲解 `reward_funcs` 与 `reward_weights` 的配合。 |

## 4. 核心概念与源码讲解

### 4.1 注册表机制：从字符串名到可调用函数

#### 4.1.1 概念说明

上一篇 `u3-l1` 留了一个问题：YAML 里写的是 `reward_funcs: [accuracy, format, tag_count]`（一串字符串），而 `GRPOTrainer(reward_funcs=...)` 要的是真正的 Python 函数。中间的「翻译」就发生在 `get_reward_funcs` 里。这个函数做三件事：

1. 构建一张**注册表（registry）** `REWARD_FUNCS_REGISTRY`——一个 `{字符串名: 函数对象}` 的字典。
2. 用 `script_args.reward_funcs`（YAML 里那串字符串）作为 key，去注册表里逐个取出对应的函数对象。
3. 把取出的函数对象列表返回给调用方。

最关键的设计点是：**这张注册表是「函数内的局部变量」，每次调用 `get_reward_funcs` 都重新构建一次**。这不是随手写的习惯，而是有意为之——因为注册表里某些「值」需要读取 `script_args` 里的超参（比如余弦奖励的上下界），把它做成局部变量、每次用最新的 `script_args` 重建，就能保证超参总是最新的。

#### 4.1.2 核心流程

`get_reward_funcs` 的执行流程：

```
get_reward_funcs(script_args)
  │
  ├─ 1. 构建局部字典 REWARD_FUNCS_REGISTRY：
  │      {
  │        "accuracy":              <直接指向函数对象 accuracy_reward>,
  │        "format":                <直接指向 format_reward>,
  │        "cosine":                <先调用工厂 get_cosine_scaled_reward(...),
  │                                   把 script_args 的超参「焊」进去，
  │                                   得到闭包 cosine_scaled_reward>,
  │        "code":                  <用 partial + update_wrapper 包装 code_reward,
  │                                   既焊入超参又保留 __name__>,
  │        ...
  │      }
  │
  ├─ 2. 按名字取值：
  │      reward_funcs = [REWARD_FUNCS_REGISTRY[func]
  │                      for func in script_args.reward_funcs]
  │
  └─ 3. return reward_funcs   ← 一串可调用函数，长度 == len(reward_funcs 字段)
```

注意第 2 步用的是**列表推导式 + 字典下标**，意味着 YAML 里写的名字必须在注册表里有对应 key，否则会抛 `KeyError`。换句话说，`reward_funcs` 字段的合法取值集合，就等于注册表的 key 集合。

#### 4.1.3 源码精读

注册表在 `get_reward_funcs` 函数内部构建，整张表覆盖了 open-r1 支持的全部奖励：

[src/open_r1/rewards.py:646-706](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L646-L706) —— `get_reward_funcs(script_args)` 的完整定义：先建 `REWARD_FUNCS_REGISTRY` 字典，再 `[REWARD_FUNCS_REGISTRY[func] for func in script_args.reward_funcs]` 按名取值返回。

仔细看这张表，你会发现「值」有**三种形态**，理解这三种形态是读懂注册表的关键：

| 形态 | 例子（注册表 key） | 写法 | 特点 |
| --- | --- | --- | --- |
| **① 直接函数引用** | `accuracy`、`format`、`reasoning_steps`、`length`、`tag_count` | `"accuracy": accuracy_reward` | 不需要任何超参，函数自身就够用。注册表里存的就是函数对象本身。 |
| **② 工厂调用结果** | `cosine`、`repetition_penalty`、`code_format`、`soft_overlong_punishment` | `"cosine": get_cosine_scaled_reward(min_value_wrong=..., ...)` | 「工厂函数」被**当场调用**，返回一个**内层闭包**；超参在调用时被「焊」进闭包，运行时再不需要这些参数。 |
| **③ `partial` + `update_wrapper`** | `code`、`binary_code`、`ioi_code`、`cf_code` | `"code": update_wrapper(partial(code_reward, num_parallel=..., ...), code_reward)` | 用 `functools.partial` 把超参「冻结」进函数，再用 `update_wrapper` 把原函数的元信息（尤其是 `__name__`）拷过来。 |

形态 ① 最简单，比如：

[src/open_r1/rewards.py:648-650](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L648-L650) —— `"accuracy": accuracy_reward` 与 `"format": format_reward`，直接把函数对象塞进字典，因为它们不需要额外超参。

形态 ② 是本讲的次重点（详见 4.3）。以 `cosine` 为例，注意它**不是**存 `get_cosine_scaled_reward` 这个工厂本身，而是存「调用工厂后得到的那个内层函数」：

[src/open_r1/rewards.py:651-657](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L651-L657) —— `get_cosine_scaled_reward(min_value_wrong=script_args.cosine_min_value_wrong, ...)` 被当场调用，把 `script_args` 里的余弦超参捕获进闭包，返回的 `cosine_scaled_reward` 才是注册表里存的值。

形态 ③ 的 `update_wrapper` 看似多余，其实是为了**通过单元测试**。来看 `code` 这一项：

[src/open_r1/rewards.py:663-671](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L663-L671) —— `update_wrapper(partial(code_reward, num_parallel=..., provider_type=..., enforce_same_language=...), code_reward)`：`partial` 冻结超参，`update_wrapper` 把 `code_reward.__name__` 等元信息拷到 partial 对象上。

为什么非得拷 `__name__`？因为 `functools.partial` 对象默认没有可读的 `__name__`，而单元测试 `test_get_reward_funcs` 会逐个断言返回函数的名字：

[tests/test_rewards.py:74-75](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L74-L75) —— `for func_name, func in zip(reward_func_names, reward_funcs): self.assertEqual(func_name, func.__name__)`，要求每个返回函数的 `__name__` 与预期名字（如 `"code_reward"`）严格相等。没有 `update_wrapper`，这一断言就会失败。

注册表构建完毕后，最后一行做按名取值：

[src/open_r1/rewards.py:704-706](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L704-L706) —— `reward_funcs = [REWARD_FUNCS_REGISTRY[func] for func in script_args.reward_funcs]`，再 `return reward_funcs`。返回列表的长度就等于 YAML 里 `reward_funcs` 写了几个名字。

#### 4.1.4 代码实践

**实践目标**：亲手调用 `get_reward_funcs`，确认它能用字符串名取出函数，且返回的函数对象的 `__name__` 符合预期。

**操作步骤**（这是「源码阅读型 + 可运行型」实践，需先按 `u1-l3` 装好开发依赖，并设置 `PYTHONPATH=src`）：

```python
# 文件名：practice_registry.py（示例代码，非项目原有文件）
import sys
sys.path.insert(0, "src")

from open_r1.configs import GRPOScriptArguments
from open_r1.rewards import get_reward_funcs

# 构造一个最小的 script_args：只需 dataset_name 与 reward_funcs 两个字段
# 其余 cosine_* / repetition_* 等用 dataclass 默认值即可
args = GRPOScriptArguments(
    dataset_name="dummy",
    reward_funcs=["accuracy", "format"],
)

# 走一遍注册表
reward_funcs = get_reward_funcs(args)

# 观察返回结果
print("数量:", len(reward_funcs))          # 预期 2
print("名字:", [f.__name__ for f in reward_funcs])  # 预期 ['accuracy_reward', 'format_reward']
```

**需要观察的现象**：

1. `len(reward_funcs)` 应等于 2，与 `reward_funcs` 字段长度一致。
2. 两个函数的 `__name__` 分别是 `'accuracy_reward'` 与 `'format_reward'`。
3. 故意把 `reward_funcs` 改成 `["accuracy", "not_a_real_reward"]`，重新运行应抛 `KeyError: 'not_a_real_reward'`——这验证了「合法取值 = 注册表 key 集合」。

**预期结果**：输出 `数量: 2` 与 `名字: ['accuracy_reward', 'format_reward']`。若改动名字触发 `KeyError`，说明注册表的「白名单」约束生效。

> 待本地验证：上述输出依赖 `math_verify` / `latex2sympy2_extended` 等依赖已正确安装。若环境缺包，`import` 阶段就会报错，请先回到 `u1-l3` 完成 `make install`。

#### 4.1.5 小练习与答案

**练习 1**：如果我想新增一个奖励函数 `my_reward`，让它能被 YAML 里 `reward_funcs: [my_reward]` 选中，至少要改哪两个地方？

**参考答案**：① 在 `rewards.py` 里定义函数 `my_reward(completions, **kwargs) -> list[float]`；② 在 `get_reward_funcs` 内的 `REWARD_FUNCS_REGISTRY` 字典里加一行 `"my_reward": my_reward`（或对应的工厂调用）。此外，若希望它出现在文档里，可顺手更新 `GRPOScriptArguments.reward_funcs` 字段的 `metadata["help"]`。

**练习 2**：为什么 `code` / `ioi_code` 等奖励要用 `update_wrapper(partial(...), 原函数)`，而不是直接写 `"code": code_reward`？

**参考答案**：因为它们需要把 `num_parallel`、`provider_type`、`test_batch_size` 等**运行期固定不变的超参**冻结进函数（用 `partial`），但 `partial` 对象没有可读的 `__name__`，会导致 `test_get_reward_funcs` 里的 `func.__name__` 断言失败；`update_wrapper` 把原函数的 `__name__`（及 `__doc__` 等）拷到 partial 上，既焊入了超参，又保留了可被测试断言的名字。

### 4.2 accuracy_reward：用 math_verify 解析 LaTeX 并判分

#### 4.2.1 概念说明

`accuracy_reward` 是 open-r1 里**最基础、用得最多**的奖励函数，衡量「模型回答是否在数学上等价于标准答案」。它的判定**不做字符串比对**，而是借助第三方库 `math_verify` 完成三步：

1. **解析标准答案（gold）**：用 `parse(sol)` 把标准答案文本解析成数学表达式对象；解析不出（返回空列表）则该样本判 `None`（跳过）。
2. **解析模型回答（answer）**：用更严格的 `LatexExtractionConfig` 从模型回答里抽答案——优先抽 `\boxed{}` 里的内容，并对 LaTeX 做规范化（去单位、修畸形算符等），再 `parse`。
3. **比对等价性**：用 `verify(gold_parsed, answer_parsed)` 判定二者数学等价，返回布尔值，转成 `1.0`（对）或 `0.0`（错）。

为什么有「判 `None`」这步？因为不是每道题的标准答案都能被 `parse` 成功（比如纯文字答案、图片题），强行判分会污染训练信号；返回 `None` 让 trl 的 `GRPOTrainer` **跳过**这个样本（不参与优势计算），是更安全的做法。

#### 4.2.2 核心流程

`accuracy_reward` 对一批 `(completion, solution)` 配对逐个判分：

```
accuracy_reward(completions, solution, **kwargs)
  │
  ├─ contents = [c[0]["content"] for c in completions]   # 取每条回答的首条消息文本
  │
  └─ for content, sol in zip(contents, solution):        # 逐对处理
       │
       ├─ gold_parsed = parse(sol, extraction_mode="first_match")
       │
       ├─ if len(gold_parsed) == 0:        # 标准答案解析失败
       │     reward = None                 # 跳过该样本
       │
       ├─ else:
       │     answer_parsed = parse(content, extraction_config=[LatexExtractionConfig(...)])
       │     try:
       │         reward = float(verify(gold_parsed, answer_parsed))  # True→1.0, False→0.0
       │     except Exception:
       │         reward = None             # verify 抛异常也跳过
       │
       └─ rewards.append(reward)
```

这里的「签名」值得专门说一下：函数声明是 `accuracy_reward(completions, solution, **kwargs)`，`solution` 并不是训练器「硬编码」传进来的，而是 trl 的 **GRPOTrainer 列注入机制**——数据集里有一列叫 `solution`，训练器就会把这一列作为同名关键字参数喂给每个奖励函数。这也是为什么各种奖励函数的签名长得不太一样（`accuracy_reward` 要 `solution`、`code_reward` 要 `verification_info`、`ioi_code_reward` 要 `id` 等）——它们各自声明自己需要的数据集列，训练器按列名自动注入。

#### 4.2.3 源码精读

函数整体只做「取文本 → 双边解析 → verify」这一件事：

[src/open_r1/rewards.py:40-82](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L40-L82) —— `accuracy_reward` 完整定义，接收 `completions` 与 `solution`，返回 `list[Optional[float]]`。

第一步，解析标准答案。注意它只给了 `extraction_mode="first_match"`，**没有**给 `extraction_config`，即用 `math_verify` 的默认抽取策略：

[src/open_r1/rewards.py:45-48](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L45-L48) —— `gold_parsed = parse(sol, extraction_mode="first_match")`。

如果标准答案解析不出来（空列表），直接判 `None` 跳过，并打印警告：

[src/open_r1/rewards.py:49](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L49) —— `if len(gold_parsed) != 0:` 是继续判分的前提；否则走到 L76–L79 的 `reward = None` 分支。

第二步，解析模型回答。这里**显式**传了 `LatexExtractionConfig`，并对抽取过程做了细致的规范化配置：

[src/open_r1/rewards.py:51-69](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L51-L69) —— `answer_parsed = parse(content, extraction_config=[LatexExtractionConfig(normalization_config=..., boxed_match_priority=0, try_extract_without_anchor=False)], extraction_mode="first_match")`。

这段配置的含义（由 `latex2sympy2_extended` 与 `math_verify` 提供能力）：

| 配置项 | 取值 | 含义 |
| --- | --- | --- |
| `normalization_config.nits` | `False` | 不把 `pi`、`e` 这类常数做特殊替换 |
| `normalization_config.malformed_operators` | `False` | 不强行修正畸形算符（如 `+-`） |
| `normalization_config.basic_latex` | `True` | 做基础 LaTeX 规范化 |
| `normalization_config.equations` | `True` | 处理等式形式 |
| `normalization_config.boxed` | `"all"` | 抽取所有 `\boxed{}` 内容（注：`accuracy_reward` 用 `"all"`，而 `len_reward`/`cosine` 用 `True`） |
| `normalization_config.units` | `True` | 处理物理单位 |
| `boxed_match_priority` | `0` | **优先**尝试 `\boxed{}` 抽取（数值越小优先级越高） |
| `try_extract_without_anchor` | `False` | 不在没有锚点（如 `=`、`\boxed{}`）时勉强抽取，避免误抽 |

第三步，用 `verify` 判等价性，并转成浮点；异常时返回 `None`：

[src/open_r1/rewards.py:71-75](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L71-L75) —— `reward = float(verify(gold_parsed, answer_parsed))`；`except Exception` 时打印错误并把 `reward` 置 `None`。

注意 `verify` 返回的是布尔值，`float(True)=1.0`、`float(False)=0.0`，这正是单元测试断言的来源。来看测试里两个最经典的用例——一正一反，标准答案都是 `\frac{63}{400}`：

[tests/test_rewards.py:79-91](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L79-L91) —— 正确回答 `\boxed{\frac{63}{400}}` 断言 `rewards[0] == 1.0`；错误回答 `\boxed{\frac{64}{400}}` 断言 `rewards[0] == 0.0`。两个回答字面几乎相同，但 `verify` 能区分 63/400 与 64/400 不等价。

还有一个边界用例值得注意——标准答案里没有 LaTeX（`"6"`），模型回答 `\boxed{3}`，应判 `0.0`（而非 `None`），说明纯数字答案也能被 `parse`/`verify` 处理：

[tests/test_rewards.py:93-98](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L93-L98) —— `solution=["6"]`、`completion=\boxed{3}` 断言 `0.0`，验证非 LaTeX 数字答案同样可判分。

#### 4.2.4 代码实践

**实践目标**：构造一组含 `\boxed{}` 的模型回答与标准答案，验证 `accuracy_reward` 返回 `1.0`（对）或 `0.0`（错），并复现 `None`（跳过）的边界。

**操作步骤**：

```python
# 文件名：practice_accuracy.py（示例代码，非项目原有文件）
import sys
sys.path.insert(0, "src")

from open_r1.rewards import accuracy_reward

# 1) 正确回答：63/400 == 63/400
completions = [[{"content": r"\boxed{\frac{63}{400}}"}]]
solution = [r"\frac{63}{400}"]
print("正确回答:", accuracy_reward(completions, solution))   # 预期 [1.0]

# 2) 错误回答：64/400 != 63/400
completions = [[{"content": r"\boxed{\frac{64}{400}}"}]]
print("错误回答:", accuracy_reward(completions, solution))   # 预期 [0.0]

# 3) 等价但写法不同：0.1575 == 63/400（数学等价）
completions = [[{"content": r"\boxed{0.1575}"}]]
print("等价小数:", accuracy_reward(completions, solution))   # 预期 [1.0]（待本地验证）

# 4) 边界：标准答案无法被 parse
completions = [[{"content": r"\boxed{3}"}]]
print("无法解析的金答案:", accuracy_reward(completions, ["无法解析的文字答案"]))  # 预期 [None]
```

**需要观察的现象**：

1. 前两行应分别输出 `[1.0]` 与 `[0.0]`，与 `tests/test_rewards.py` 的断言一致。
2. 第 3 行：`0.1575` 与 `63/400` 数学等价，`verify` 应返回 `True` 得到 `1.0`（这体现了「不做字符串比对」的价值）。
3. 第 4 行：金答案是「无法解析的文字答案」，`parse` 返回空列表，触发 L76–L79 分支，输出 `[None]`。

**预期结果**：`[1.0]`、`[0.0]`、`[1.0]`、`[None]`。其中第 3 行标注「待本地验证」——`math_verify` 对「分数 ↔ 有限小数」的等价判定取决于其版本与规范化配置，若你本地得到 `0.0`，请记录 `math_verify` 版本并对照其文档。

#### 4.2.5 小练习与答案

**练习 1**：为什么标准答案（gold）用默认 `parse`，而模型回答（answer）却要套一个复杂的 `LatexExtractionConfig`？

**参考答案**：标准答案通常来自数据集，格式干净（如 `\frac{63}{400}`），默认 `parse` 就能处理；而模型回答是自由生成的长文本，答案可能藏在 `\boxed{}` 里、带物理单位、有畸形算符，所以需要 `LatexExtractionConfig` 先做「定位 + 规范化」再解析。两者承担的「干净度假设」不同。

**练习 2**：`accuracy_reward` 在哪两种情况下会返回 `None`？返回 `None` 对训练有什么影响？

**参考答案**：① 标准答案 `parse` 出空列表（`len(gold_parsed) == 0`）；② `verify(...)` 抛异常（被 `except Exception` 捕获）。`None` 会被 trl 的 `GRPOTrainer` 视为「跳过该样本」，该样本不参与优势计算，避免用脏数据/不可判分的样本污染训练信号。

### 4.3 参数化奖励与工厂闭包（cosine / repetition / 软超长）

#### 4.3.1 概念说明

注册表里有一类奖励（4.1 表中的「形态 ②」）不能直接写成「输入 completions 就能打分」的简单函数，因为它们的行为**依赖一组超参**——比如「余弦奖励」的上下界、`max_len`，「重复惩罚」的 n-gram 大小、最大罚分，「软超长惩罚」的 `max_completion_len` 等。这些超参来自 `GRPOScriptArguments`（即来自 YAML 配方），不同实验要调不同的值。

如果直接把这些超参写成奖励函数的参数，会破坏统一契约 `reward(completions, **kwargs)`（trl 要求所有奖励函数签名一致，超参只能通过列注入的 `**kwargs` 传，而上面的超参并不来自数据集列）。open-r1 的解法是**工厂函数（factory function）**：

- 定义一个「外层函数」`get_xxx_reward(超参1, 超参2, ...)`，它**返回**一个「内层函数」`xxx_reward(completions, **kwargs)`。
- 内层函数通过**闭包（closure）**捕获外层的超参，运行时只需 `completions`（及列注入的 kwargs）即可打分。
- 在注册表里，写的是 `get_xxx_reward(超参=script_args.xxx)`——**当场调用**外层函数，把超参焊进闭包，返回内层函数存进字典。

这样，超参的来源（`script_args`）和奖励函数的运行期契约（`completions, **kwargs`）就被干净地解耦了。

#### 4.3.2 核心流程

以 `get_cosine_scaled_reward` 为例，工厂的两层结构：

```
get_cosine_scaled_reward(min_value_wrong, max_value_wrong,
                         min_value_correct, max_value_correct, max_len)   ← 外层（工厂）
  │  把超参捕获进闭包
  └─ 返回 cosine_scaled_reward(completions, solution, **kwargs)            ← 内层（真正打分）
       │
       ├─ 对每条 (content, sol)：
       │    ├─ 解析 gold / answer（同 accuracy_reward 的双 parse）
       │    ├─ is_correct = verify(answer, gold)
       │    ├─ progress = len(content) / max_len          # 长度归一化到 [0,1]
       │    ├─ cosine   = cos(progress * π)               # 长度→[-1,1] 的余弦
       │    └─ reward   = min_value + 0.5*(max_value - min_value)*(1 + cosine)
       └─ return rewards
```

关键直觉：余弦奖励在「正确/错误」两类里各用一组 `(min_value, max_value)`，并用 `cos(progress*π)` 把「长度」映射到 `[0,1]` 的插值权重。对**正确**回答，越短奖励越高（鼓励简洁）；对**错误**回答，越长惩罚越轻（容忍长一点的探索）。具体公式（以正确回答为例）：

\[
\text{progress} = \frac{\text{len(content)}}{\text{max\_len}},\quad
\text{cosine} = \cos(\text{progress}\cdot\pi)
\]

\[
\text{reward} = \text{min\_value\_correct} + \tfrac{1}{2}(\text{max\_value\_correct} - \text{min\_value\_correct})(1 + \text{cosine})
\]

当 `progress=0`（最短）时 `cosine=1`，reward 取 `max_value_correct`；当 `progress=1`（达到 `max_len`）时 `cosine=-1`，reward 取 `min_value_correct`。

`get_repetition_penalty_reward` 与 `get_soft_overlong_punishment` 也是同样的工厂闭包结构，只是内层逻辑不同（n-gram 重复率 / 长度分段惩罚），这里不展开实现细节（留给 `u3-l4`），只关注「超参从哪来」。

#### 4.3.3 源码精读

先看工厂的外层签名——它正式声明了所有可调超参，并给了默认值：

[src/open_r1/rewards.py:205-211](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L205-L211) —— `get_cosine_scaled_reward(min_value_wrong=-1.0, max_value_wrong=-0.5, min_value_correct=0.5, max_value_correct=1.0, max_len=1000)` 的外层签名。

内层闭包通过闭包变量访问这些超参（注意内层函数的参数列表里**没有**它们）：

[src/open_r1/rewards.py:212-228](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L212-L228) —— 内层 `cosine_scaled_reward(completions, solution, **kwargs)`，文档注释里列出 `min_value_wrong` 等参数「parametrize」本函数——这些值来自外层闭包，而非函数签名。

插值与 reward 计算的核心两行：

[src/open_r1/rewards.py:266-277](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L266-L277) —— `progress = gen_len / max_len`、`cosine = math.cos(progress * math.pi)`，再据 `is_correct` 选 `(min_value, max_value)`，最后 `reward = min_value + 0.5 * (max_value - min_value) * (1.0 + cosine)`。注意错误回答那一支做了 min/max 互换（L273–L275），使「越短罚得越重」。

那么这些超参的**默认值**和**合法取值**在哪定义？在 `configs.py` 的 `GRPOScriptArguments` 里。先看奖励选择本身与余弦超参：

[src/open_r1/configs.py:234-259](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L234-L259) —— `reward_funcs` 字段默认 `["accuracy", "format", "tag_count"]`，后面跟着 `cosine_min_value_wrong` / `cosine_max_value_wrong` / `cosine_min_value_correct` / `cosine_max_value_correct` / `cosine_max_len` 五个余弦超参，每个都带 `metadata["help"]`。

重复惩罚与代码相关超参：

[src/open_r1/configs.py:260-291](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L260-L291) —— `repetition_n_grams`（默认 3）、`repetition_max_penalty`（默认 -1.0），以及 `code_language`、`code_eval_test_batch_size`、`code_eval_scoring_mode`、`parallel_code_exec_per_proc` 等代码奖励超参。注意 `repetition_max_penalty` 的 help 写明是「Maximum (negative) penalty」，即负数。

软超长惩罚的两个长度超参：

[src/open_r1/configs.py:324-331](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L324-L331) —— `max_completion_len`（默认 16384，单位是「字符」）与 `soft_punish_cache`（默认 4096），对应 `get_soft_overlong_punishment(max_completion_len, soft_punish_cache)` 的两个形参。

最后看「焊超参」这一步——注册表里调用工厂时，实参正是从 `script_args` 取的：

[src/open_r1/rewards.py:658-661](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L658-L661) —— `"repetition_penalty": get_repetition_penalty_reward(ngram_size=script_args.repetition_n_grams, max_penalty=script_args.repetition_max_penalty)`，把 `configs.py` 里的字段灌进工厂。

至此，超参的完整流转链清晰可见：

\[
\underbrace{\text{YAML 配方}}_{\text{cosine\_max\_len: 1000}} \xrightarrow{\text{TrlParser}}
\underbrace{\text{GRPOScriptArguments.cosine\_max\_len}}_{\text{configs.py 字段}} \xrightarrow{\text{注册表调用}}
\underbrace{\text{get\_cosine\_scaled\_reward}(\ldots,\text{max\_len}=\ldots)}_{\text{rewards.py 工厂}} \xrightarrow{\text{闭包}}
\underbrace{\text{cosine\_scaled\_reward}(\text{completions}, \ldots)}_{\text{内层打分函数}}
\]

任何一个超参的改动，都只需改 YAML，无需碰奖励函数代码——这正是工厂闭包 + 注册表带来的可配置性。

#### 4.3.4 代码实践

**实践目标**：验证「改 `script_args` 的超参 → 工厂闭包行为跟着变」，亲手感受超参流转链。

**操作步骤**：

```python
# 文件名：practice_factory.py（示例代码，非项目原有文件）
import sys
sys.path.insert(0, "src")

from open_r1.configs import GRPOScriptArguments
from open_r1.rewards import get_reward_funcs

# A：用默认 max_len=1000 取出 cosine 奖励
args_A = GRPOScriptArguments(dataset_name="dummy", reward_funcs=["cosine"])
cosine_A = get_reward_funcs(args_A)[0]

# B：把 cosine_max_len 改成 100（更短的归一化基准）
args_B = GRPOScriptArguments(dataset_name="dummy", reward_funcs=["cosine"], cosine_max_len=100)
cosine_B = get_reward_funcs(args_B)[0]

# 同一条 80 字符的回答，在两个 max_len 下 reward 应不同
completions = [[{"content": r"\boxed{\frac{63}{400}}" + " " * (80 - 22)}]]  # 总长约 80
solution = [r"\frac{63}{400}"]

print("max_len=1000:", cosine_A(completions, solution))   # progress=0.08，更接近最短
print("max_len=100 :", cosine_B(completions, solution))   # progress=0.8，接近上限
```

**需要观察的现象**：

1. 两次调用返回的 reward **不相等**——同一个回答、同一个标准答案，只因 `cosine_max_len` 不同，奖励就不同。
2. `max_len=100` 时 `progress≈0.8`（接近 1），正确回答的 reward 更接近 `min_value_correct=0.5`；`max_len=1000` 时 `progress≈0.08`（接近 0），reward 更接近 `max_value_correct=1.0`。
3. 两个函数对象的 `__name__` 都是 `'cosine_scaled_reward'`（来自工厂返回的内层函数名），但它们是**不同的闭包实例**（捕获了不同的 `max_len`）。

**预期结果**：两次输出不同的浮点数，且 `max_len=100` 的 reward **小于** `max_len=1000` 的 reward（因为正确回答在更长基准下被视为「偏短」，奖励更高）。具体数值待本地验证，趋势是确定的。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `cosine_max_len` 从 1000 改成 100，对一条长 500 字符的**正确**回答，reward 会变大还是变小？

**参考答案**：变小。`progress = 500/max_len`：`max_len=1000` 时 progress=0.5，`max_len=100` 时 progress=1.0（被长度饱和）。对正确回答，reward 随 progress 增大而减小（从 `max_value_correct=1.0` 跌向 `min_value_correct=0.5`），所以改小 `max_len` 会让这条偏长回答的 reward 下降。

**练习 2**：`repetition_max_penalty` 的默认值是 `-1.0`。如果有人在 YAML 里误填成正数 `1.0`，会发生什么？

**参考答案**：会在构建注册表、调用 `get_repetition_penalty_reward(max_penalty=1.0)` 时抛 `ValueError`（见 `rewards.py` L295–L296 的 `if max_penalty > 0: raise ValueError(...)`）。这是一种「快速失败」的参数校验，避免把「奖励」误配成「正分」，让重复反而得正收益。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「迷你奖励诊断」任务。

**任务**：假设你要为一个数学 RL 任务挑选奖励组合，候选有 `accuracy`、`format`、`cosine`。请完成：

1. **构造 script_args**：用 `GRPOScriptArguments(dataset_name="dummy", reward_funcs=["accuracy", "format", "cosine"])`，并自定义 `cosine_max_len=200`。
2. **取出函数**：`funcs = get_reward_funcs(args)`，断言 `len(funcs) == 3` 且 `funcs[0].__name__ == "accuracy_reward"`。
3. **造一组对照样本**：两条回答，都「答对」（标准答案 `\frac{63}{400}`），但一条短（`\boxed{\frac{63}{400}}`，约 22 字符）、一条长（后面补空格到 180 字符）。
4. **分别打分**：
   - 用 `funcs[0]`（accuracy）对两条打分，预期都得 `1.0`（都答对）。
   - 用 `funcs[2]`（cosine，max_len=200）对两条打分，预期**短的那条 reward 更高**（正确且更简洁）。
5. **写一句结论**：说明在「都答对」的前提下，`accuracy` 无法区分长短，而 `cosine` 能进一步奖励简洁——这正是把多个奖励组合使用的动机。

**交付物**：一段可运行的 Python 脚本（示例代码），加一段 3–5 句的中文结论。如果你已经读过 `config_demo.yaml` 里的 `reward_funcs` 与 `reward_weights`（见 6.小结的延伸），可以把「组合 + 加权」的思路也写进结论里。

> 提示：本任务不依赖 GPU，全程只调用 `get_reward_funcs` 与各奖励函数，适合在装好开发依赖的 CPU 环境上完成。若 `math_verify` 抽取行为与预期不符，请先记录包版本再排查。

## 6. 本讲小结

- **注册表是局部字典**：`REWARD_FUNCS_REGISTRY` 在 `get_reward_funcs` 函数内部构建，每次调用都用最新的 `script_args` 重建；其 key 集合就是 YAML 里 `reward_funcs` 字段的合法取值集合。
- **三种「值」的形态**：① 直接函数引用（`accuracy`/`format`/…，无需超参）；② 工厂调用结果（`cosine`/`repetition_penalty`/…，把超参焊进闭包）；③ `partial` + `update_wrapper`（`code`/`ioi_code`/…，焊超参的同时保留 `__name__` 以通过单测）。
- **`accuracy_reward` 三步走**：`parse` 金答案（失败则 `None` 跳过）→ `parse` 模型回答（带 `LatexExtractionConfig`，优先抽 `\boxed{}`）→ `verify` 判等价并转 `1.0`/`0.0`；它是「不做字符串比对、只判数学等价」的典型。
- **`None` 是安全阀**：金答案不可解析或 `verify` 抛异常时返回 `None`，让 `GRPOTrainer` 跳过脏样本，避免污染训练信号。
- **参数化奖励走工厂闭包**：`get_xxx_reward(超参)` 返回内层 `xxx_reward(completions, **kwargs)`，超参经 `YAML → GRPOScriptArguments → 注册表调用 → 闭包` 流转，调参只改 YAML。
- **列注入决定签名**：奖励函数签名里的 `solution` / `verification_info` / `id` 等参数，来自 trl `GRPOTrainer` 把数据集列按名注入 `**kwargs` 的机制——这就是各奖励签名「长得不一样」的原因。

## 7. 下一步学习建议

- 下一篇 **`u3-l3` 格式与推理过程奖励** 会展开注册表里 `format_reward` / `tag_count_reward` / `reasoning_steps_reward` / `code_format_reward` 的正则与部分计分逻辑，与本讲的 `accuracy_reward` 互补——一个管「答得对」，一个管「说得规矩」。
- 再下一篇 **`u3-l4` 长度、余弦调度与重复惩罚奖励** 会深入 `len_reward`、`get_cosine_scaled_reward`、`get_repetition_penalty_reward`、`get_soft_overlong_punishment` 的内部公式与边界行为，本讲只点了「超参从哪来」，那里会讲清「reward 怎么算」。
- 想立刻动手验证本讲内容，可运行 `make test`（见 `u8-l2`），`tests/test_rewards.py` 的 `TestGetRewardFuncs` 与 `TestRewards` 已覆盖本讲的全部断言。
- 若你对 trl 的「列注入 `**kwargs`」与 `reward_weights` 加权求和机制想了解更深，建议直接阅读 trl 的 `GRPOTrainer` 源码（open-r1 在此委托，未自行实现）。
