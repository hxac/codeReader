# 自定义新任务：端到端扩展

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 TinyZero 接入一个**全新 RL 任务**需要改动哪「三处」，以及为什么是这三处；
- 理解 `data_source` 这个字符串如何像一根线，把「数据 → 奖励 → 路由」三件套串起来；
- 能够照着现有 countdown 任务，从零写出一个属于自己的数据预处理脚本、规则奖励函数与路由分支；
- 知道哪些代码**完全不用动**（Worker、训练主循环、PPO/GRPO 算法），从而复用整套训练基础设施跑自己的任务。

本讲是「数据与任务定义」「奖励函数」「训练入口」三条线的收口实战，不再展开算法细节（那些在 u5/u6 已讲透）。

## 2. 前置知识

本讲默认你已经读过以下讲义（它们建立了本讲所需的心智模型，这里只做承接、不重复）：

- **u2-l1**：countdown 数据预处理脚本的完整流程——`make_prefix` 把题目翻译成 prompt、parquet 的字段结构（`data_source` / `prompt` / `reward_model.ground_truth`）。
- **u2-l4**：规则奖励函数 `compute_score` 的「提取 → 校验 → 求值 → 分级打分」四步法，以及 `format_score` 作为奖励塑形的作用。
- **u4-l1**：`main_ppo.py` 是总装入口；`RewardManager` 按 `data_source` 路由到 `compute_score`，并把标量分数挂在回答最后一个有效 token 上。

本讲只补充一句最关键的「接线直觉」：**TinyZero 真正属于「业务」的代码只有两类——生成数据的脚本、判分的奖励函数**；其余（训练循环、Worker、算法、Ray 调度）都是 veRL 提供的通用基础设施，对新任务完全透明、不需要改动。

> 术语速查：`data_source`（数据源字符串，写在 parquet 每一行，训练时用来**路由奖励函数**）；`ground_truth`（标准答案，判分参考）；`compute_score`（规则奖励函数，把模型回答映射成标量分数）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `examples/data_preprocess/countdown.py` | 第一件套：数据预处理 | `make_prefix` 怎么造 prompt、`process_fn` 怎么写 parquet 字段 |
| `verl/utils/reward_score/countdown.py` | 第二件套：规则奖励 | `compute_score` 的四步打分结构，作为新任务的模板 |
| `verl/utils/reward_score/multiply.py` | 第二件套（最简版） | 只比较整数答案，是最适合「照抄」的极简奖励 |
| `verl/trainer/main_ppo.py` | 第三件套：奖励路由 | `_select_rm_score_fn` 用 `data_source` 子串匹配接线 |
| `scripts/train_tiny_zero.sh` | 启动脚本 | 怎么把新任务的 parquet 喂进 `main_ppo` |

## 4. 核心概念与源码讲解

### 4.1 三件套总览：`data_source` 是贯穿全流程的契约

#### 4.1.1 概念说明

新增一个 TinyZero 任务，听起来庞大，实际只动三处代码，我们称之为「**数据-奖励-路由**」三件套：

1. **数据预处理脚本**（`examples/data_preprocess/` 下新建一个 `.py`）：生成训练要用的 parquet。
2. **规则奖励函数**（`verl/utils/reward_score/` 下新建一个 `.py`）：定义「什么样的回答得多少分」。
3. **路由分支**（`verl/trainer/main_ppo.py` 的 `_select_rm_score_fn` 加一行）：告诉系统「这种 `data_source` 用哪个奖励函数」。

三件套之间的「胶水」是一个字符串：**`data_source`**。它在数据预处理脚本里被写进 parquet 的每一行，在训练时被 `RewardManager` 读出来，再交给 `_select_rm_score_fn` 做子串匹配。`data_source` 一旦定下来，三处改动就有了共同的命名锚点。

#### 4.1.2 核心流程

一条样本从「题目」到「训练梯度」的全流程，新任务需要介入的只有前三步：

```text
[你写的脚本]  造题 → make_prefix → parquet(data_source=xxx, ground_truth=...)
   ↓
[RLHFDataset] 读 parquet → tokenize → 一批张量样本
   ↓
[vLLM rollout] 让模型生成回答 response
   ↓
[RewardManager] 读出 data_source → _select_rm_score_fn 路由 → compute_score 打分   ← 你写的奖励
   ↓
[PPO/GRPO 算法] 分数挂到末位 token → KL → advantage → 更新 Actor/Critic   ← 完全复用，无需改动
```

关键结论：**你只需要让 `compute_score(回答, ground_truth) → 标量分数` 这一步成立，后面的强化学习整套机制原样复用。** 这正是 TinyZero「轻量」的根源——它把「任务」与「训练框架」彻底解耦。

#### 4.1.3 源码精读：路由与奖励的会合点

`RewardManager.__call__` 是数据与奖励函数真正会合的地方，它从每条样本里读出 `data_source` 并调用 `_select_rm_score_fn`：

[verl/trainer/main_ppo.py:74-81](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L74-L81) —— `RewardManager` 取出 `ground_truth` 与 `data_source`，路由到 `compute_score_fn`，再把标量分数挂在第 \(i\) 条样本最后一个有效 response token 位 \(L_{\text{resp}}-1\)。

这五行就是「奖励路由」的全部运行时行为。它告诉了我们三件套的对接方式：`data_source`（你写进 parquet 的字符串）→ `_select_rm_score_fn`（你要加分支的地方）→ `compute_score_fn`（你要实现的打分函数）。本讲后面三节，就是分别讲这三处怎么改。

#### 4.1.4 代码实践

1. **目标**：在纸上画出你自己任务的「数据-奖励-路由」接线图。
2. **步骤**：选定一个最简单的任务（例如「两数求和」），给它定一个 `data_source` 名字；然后列出三件套分别要新建/修改哪个文件。
3. **需要观察的现象**：你会发现自己能不查代码就说出三处改动点。
4. **预期结果**：例如 `data_source='yolo/sum-2_digit'`；新建 `examples/data_preprocess/sum.py`；新建 `verl/utils/reward_score/sum_task.py`；在 `main_ppo.py:24-34` 的 `_select_rm_score_fn` 里加一个 `elif` 分支。

#### 4.1.5 小练习与答案

**练习 1**：如果忘记在 `_select_rm_score_fn` 注册新 `data_source`，训练会怎样？
**答**：`_select_rm_score_fn` 会走到最后的 `else: raise NotImplementedError`（见下文 4.4.3），训练一启动就抛异常，所有奖励都算不出来。

**练习 2**：为什么把任务逻辑做成「三件套」，而不是改训练循环？
**答**：因为不同任务只是「题目」和「判分标准」不同，PPO/GRPO 的梯度更新逻辑是通用的。解耦后，新任务零成本复用整条训练链路，符合开闭原则。

---

### 4.2 第一件套·数据预处理：写 `make_prefix` 生成 parquet

#### 4.2.1 概念说明

数据预处理脚本唯一的职责是：**把你的「题目」翻译成 veRL 期望的 parquet 行**。countdown 脚本里真正承担翻译工作的是 `make_prefix`——它把 `(target, numbers)` 塞进一段自然语言 prompt，并约定模型用 `<think>` / `<answer>` 标签作答。

新任务这一步要回答两个问题：
1. **prompt 怎么写**：包括任务描述、输出格式约定、以及一个**未闭合的 `<think>` 作为续写钩子**（让基座模型接着生成推理过程）。
2. **`ground_truth` 放什么**：判分时需要的标准答案。

#### 4.2.2 核心流程

```text
原始题目 (target, numbers / num1, num2 / ...)
   ↓  make_prefix(dp, template_type)
prompt 字符串（含 <think> 续写钩子）
   ↓  process_fn(example, idx) 组装字段
parquet 行 = {
  data_source, prompt=[{role,content}],
  reward_model={style:'rule', ground_truth},
  extra_info, ...
}
   ↓  to_parquet → train.parquet / test.parquet
```

#### 4.2.3 源码精读

`make_prefix` 是数据预处理的「翻译核心」——注意第 56 行那条 `NOTE` 注释，它直接点破了「数据与奖励强耦合」这一隐藏约束：

[examples/data_preprocess/countdown.py:53-66](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L53-L66) —— `make_prefix` 用 `f"""..."""` 把 `numbers` 与 `target` 填进指令，提供 `base` 与 `qwen-instruct` 两套外壳不同的模板，并以未闭合的 `<think>` 结尾引导续写。

> 关键提醒（来自代码注释 `# NOTE: also need to change reward_score/countdown.py`）：prompt 里规定用 `<answer> ... </answer>` 标签作答，那么奖励函数就**必须**用同样的标签去抽取答案。改了 prompt 的格式约定却忘了同步改奖励函数，会导致奖励恒为 0、训练完全学不动。

随后 `process_fn` 把 prompt 装进 veRL 期望的字段结构，并写死 `data_source`：

[examples/data_preprocess/countdown.py:94-118](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L94-L118) —— `process_fn` 把 `make_prefix` 的结果包成 `prompt` 字段，并组装 `data_source='countdown'`、`reward_model.ground_truth={'target', 'numbers'}`、`extra_info` 等字段。

其中 `data_source = 'countdown'` 在 [examples/data_preprocess/countdown.py:84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L84) 定义——这就是贯穿三件套的那根线。最后 `to_parquet` 写出两个文件：

[examples/data_preprocess/countdown.py:126-127](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L126-L127) —— 写出 `train.parquet` 与 `test.parquet`，供 `data.train_files` / `data.val_files` 加载。

> 旁注：[examples/data_preprocess/countdown.py:15-51](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L15-L51) 的 `gen_dataset` 描述了样本生成规格，但主流程并未调用它（数据实际来自 [第 88 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L88) 的 `load_dataset('Jiayi-Pan/Countdown-Tasks-3to4')`）。新任务若自带数据源可同样用 `load_dataset`，若数据可程序生成，则可自行写一个 `gen_dataset` 并在 `__main__` 里调用。

#### 4.2.4 代码实践：写一个「两数求和」的 `make_prefix`

1. **目标**：照着 countdown 的 `base` 模板，写一个最简单的求和任务 prompt 生成函数。
2. **操作步骤**：新建 `examples/data_preprocess/sum.py`，把下方「示例代码」（非项目原有代码）粘贴进去。

```python
# 示例代码：examples/data_preprocess/sum.py（非项目原有文件）
import os, argparse
from datasets import Dataset

def make_prefix(dp, template_type='base'):
    num1, num2 = dp['num1'], dp['num2']
    if template_type == 'base':
        prefix = f"""A conversation between User and Assistant. The user asks a question, and the Assistant solves it. The assistant first thinks about the reasoning process in the mind and then provides the user with the answer.
User: What is {num1} + {num2}? Show your work in <think> </think> tags. And return the final answer in <answer> </answer> tags, for example <answer> 42 </answer>.
Assistant: Let me solve this step by step.
<think>"""
    return prefix
```

3. **需要观察的现象**：注意 prompt 同样以**未闭合的 `<think>`** 结尾，且 `num1 + num2` 的真实结果 `num1 + num2` 将作为 `ground_truth`（一个整数）。
4. **预期结果**：调用 `make_prefix({'num1': 13, 'num2': 29})` 得到一段以 `<think>` 结尾、要求模型把最终和填进 `<answer>` 的指令；`ground_truth` 设为 `42`。
5. 待本地验证：完整 `process_fn` 与 `to_parquet` 需仿照 [countdown.py:94-127](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L94-L127) 补齐，最终产出 `train.parquet` / `test.parquet`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prompt 要以**未闭合的 `<think>`** 结尾？
**答**：基座模型（base model）会接着这个 `<think>` 续写，从而自动产出「思维链 + `<answer>` 答案」的格式。这是 R1 Zero 路线用纯 RL 诱导推理的关键 trick（详见 u7-l6）。

**练习 2**：`ground_truth` 必须是字典吗？
**答**：不一定。countdown 用 `{'target', 'numbers'}` 是因为它要校验「用了哪些数」；multiply / 求和这类只比最终数值的任务，`ground_truth` 直接用一个整数即可（multiply 奖励里就是 `int(answer) == int(ground_truth)`）。

---

### 4.3 第二件套·规则奖励：实现 `compute_score`

#### 4.3.1 概念说明

`compute_score(solution_str, ground_truth) → float` 是规则奖励的统一签名。它做一件事：**把模型生成的一段文本，按规则打成一个标量分数**。countdown 的实现较复杂（要校验用的数字、安全求值等式）；但对「只比较最终数值」的任务（乘法、求和），奖励可以极简——multiply 的 `compute_score` 只做整数比较，是最值得照抄的模板。

#### 4.3.2 核心流程

countdown 版（复杂，四步）：

```text
solution_str
  → extract_solution    抽出最后一个 <answer>...</answer> 内容
  → validate_equation   校验等式只用题目给定数字、各用一次
  → evaluate_equation   用受限 eval 安全求值
  → 分级打分            None→0；格式对但错→format_score(0.1)；结果正确→score(1.0)
```

multiply / 求和版（极简，两步）：

```text
solution_str
  → extract_solution    抽出 <answer> 内整数
  → 分级打分            None→0；int 相等→score(1.0)；否则→format_score(0.1)
```

分级打分是奖励塑形（reward shaping）：`format_score` 给「格式对但答错」的小分，提供稠密的起步信号；但它**不能太高**，否则模型会「只刷格式不解题」（reward hacking，详见 u2-l4、u7-l6）。

#### 4.3.3 源码精读

countdown 的 `compute_score` 四步法，注意它如何用 `format_score=0.1` 与 `score=1.0` 做分级：

[verl/utils/reward_score/countdown.py:59-111](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59-L111) —— `compute_score` 依次调用 `extract_solution`（无答案→返回 0）、`validate_equation`（数字不合规→`format_score`）、`evaluate_equation`（用 `abs(result-target)<1e-5` 判等，正确→`score`，否则→`format_score`）。

其中三步的具体实现：
- [extract_solution:7-25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L7-L25)：先按 `"Assistant:"` 切掉 prompt 部分，再用正则 `<answer>(.*?)</answer>` 取最后一个答案。
- [validate_equation:28-41](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L28-L41)：从等式抠出所有数字，排序后与题目给定数字比较，防「偷数字」。
- [evaluate_equation:44-56](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L44-L56)：用正则白名单 `^[\d+\-*/().\s]+$` 过滤，再 `eval(..., {"__builtins__": None}, {})` 受限求值，保证安全。

而 multiply 的 `compute_score` 只需比较整数，是最简模板：

[verl/utils/reward_score/multiply.py:27-58](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/multiply.py#L27-L58) —— `compute_score` 抽出 `<answer>` 内整数，与 `ground_truth` 直接 `int == int` 比较，相等给 `score`、否则给 `format_score`。

> 洞察：因为求和任务也是「只比最终整数」，它的奖励逻辑与 multiply **完全相同**——这意味着你甚至可以直接复用 `multiply.compute_score`，连第二件套都不用新写（见 4.3.5 练习）。但为了教学完整，下面仍给出独立实现。

#### 4.3.4 代码实践：写求和任务的 `compute_score`

1. **目标**：仿照 multiply 写一个极简的求和奖励，并准备三条测试输入。
2. **操作步骤**：新建 `verl/utils/reward_score/sum_task.py`（命名为 `sum_task` 而非 `sum`，避免覆盖 Python 内建 `sum()`），粘贴下方「示例代码」。

```python
# 示例代码：verl/utils/reward_score/sum_task.py（非项目原有文件）
import re, random

def extract_solution(solution_str):
    if "Assistant:" in solution_str:
        solution_str = solution_str.split("Assistant:", 1)[1]
    else:
        return None
    answer_pattern = r'<answer>(.*?)</answer>'
    matches = list(re.finditer(answer_pattern, solution_str))
    if matches:
        return matches[-1].group(1).strip()
    return None

def compute_score(solution_str, ground_truth, format_score=0.1, score=1.):
    answer = extract_solution(solution_str)
    if answer is None:
        return 0
    try:
        if int(answer) == int(ground_truth):
            return score
        return format_score
    except ValueError:
        return format_score
```

3. **需要观察的现象 / 预期结果**：用三条输入验证——
   - 完全正确：`compute_score("...Assistant: ...<answer> 42 </answer>", 42)` → `1.0`
   - 格式对但错：`compute_score("...Assistant: ...<answer> 41 </answer>", 42)` → `0.1`
   - 无 `<answer>` 标签：`compute_score("...Assistant: I think it is 42", 42)` → `0`
4. 待本地验证：可用 `python -c` 直接 import 调用，打印三种情况的返回值确认分级。

#### 4.3.5 小练习与答案

**练习 1**：求和任务其实可以完全不写新奖励，怎么做？
**答**：在路由里直接 `elif "sum" in data_source: return multiply.compute_score`。因为 multiply 的 `compute_score` 已经是「抽 `<answer>` 整数 → 与 `ground_truth` 比大小」，与求和的需求一字不差。这是复用现有奖励的捷径。

**练习 2**：`compute_score` 里的 `do_print = random.randint(1, 64) == 1`（[countdown.py:73](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L73)）有什么用？
**答**：以约 1/64 概率随机抽样打印一条样本，用于训练时低成本观察「模型实际生成什么、奖励函数判成什么样」，是一种轻量调试手段，不影响分数。

---

### 4.4 第三件套·奖励路由：在 `_select_rm_score_fn` 注册

#### 4.4.1 概念说明

写好了奖励函数，系统还不知道「我的 `data_source` 该用哪个函数」。接线点就是 `_select_rm_score_fn`——它用 `data_source` 做子串匹配，返回对应的 `compute_score` 函数对象。新任务要做的，就是加一个 `elif` 分支并 `import` 你的奖励模块。

#### 4.4.2 核心流程

```text
data_source 字符串（来自 parquet 每一行）
  ↓
_select_rm_score_fn(data_source)
  ↓  按顺序做子串匹配 if/elif
  → 返回某个 compute_score 函数对象
  ↓
RewardManager 用它打分
（未命中任何分支 → raise NotImplementedError，训练启动即报错）
```

#### 4.4.3 源码精读

整个路由逻辑只有十行，是三件套里改动量最小的：

[verl/trainer/main_ppo.py:24-34](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L24-L34) —— `_select_rm_score_fn` 用 `in` 做子串匹配：`'openai/gsm8k'` 精确匹配、`"multiply" in data_source` 与 `"countdown" in data_source` 子串匹配，最后 `else: raise NotImplementedError` 兜底。

注意两个设计要点：

1. **子串匹配而非精确匹配**：`"multiply" in data_source` 让 `yolo/multiply-3_digit` 与 `yolo/arithmetic-3_digit` 都命中同一条分支（见 [第 29-30 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L29-L30)）。这意味着你的 `data_source` 名字只要**包含约定子串**即可路由成功，命名灵活。
2. **import 在文件顶部**：所有奖励模块在 [第 20 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L20) 统一导入：`from verl.utils.reward_score import gsm8k, math, multiply, countdown`。新任务要在这里加上你的模块。

#### 4.4.4 代码实践：给求和任务加路由分支

1. **目标**：让 `data_source` 含 `'sum'` 的样本路由到你写的 `sum_task.compute_score`。
2. **操作步骤**（两处改动，均在 `verl/trainer/main_ppo.py`）：
   - 第 20 行 import 处追加 `sum_task`：

     ```python
     from verl.utils.reward_score import gsm8k, math, multiply, countdown, sum_task
     ```

   - 在 [第 32 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L32) 的 `elif "countdown" in data_source:` 之后插入：

     ```python
     elif "sum" in data_source:
         return sum_task.compute_score
     ```
3. **需要观察的现象 / 预期结果**：传 `_select_rm_score_fn('yolo/sum-2_digit')` 返回 `<function compute_score>`；传一个未登记的 `'foobar'` 则抛 `NotImplementedError`。
4. 待本地验证：可在终端 `python -c "from verl.trainer.main_ppo import _select_rm_score_fn; print(_select_rm_score_fn('yolo/sum-2_digit'))"` 验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么把分支写成 `"sum" in data_source` 而非 `data_source == 'sum'`？
**答**：子串匹配更宽松、更易扩展，可兼容 `yolo/sum-2_digit`、`sum/3_digit` 等不同前缀的变体，不必为每种命名各写一条分支。

**练习 2**：若两个分支的子串互为包含（例如同时有 `"multi"` 和 `"multiply"`），会有什么隐患？
**答**：`if/elif` 按顺序短路，先匹配到的分支生效，后者永远命中不到。所以命名子串应尽量**互不重叠**，且把更特殊的分支放前面。

---

## 5. 综合实践：从零跑通「两数求和」任务

本实践把三件套串起来，目标是端到端跑通一个全新任务，验证你已掌握「数据-奖励-路由」全链路。

### 5.1 实践目标

新增 `data_source = 'yolo/sum-2_digit'` 的求和任务，让 TinyZero 用 RL 学会做加法，并复用全部训练基础设施。

### 5.2 操作步骤

**第 1 步：数据预处理**（第一件套）—— 新建 `examples/data_preprocess/sum.py`，仿照 [countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py) 的 `__main__` 结构：
- 自行生成或加载 `(num1, num2)` 数据；
- 用 4.2.4 的 `make_prefix` 造 prompt，以未闭合 `<think>` 结尾；
- `process_fn` 写字段：`data_source='yolo/sum-2_digit'`、`reward_model={'style':'rule', 'ground_truth': num1+num2}`（整数即可）；
- `to_parquet` 产出 `~/data/sum/train.parquet` 与 `test.parquet`。
- 运行：`python ./examples/data_preprocess/sum.py --local_dir ~/data/sum`（仿 [README 第 44 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L44)）。

**第 2 步：规则奖励**（第二件套）—— 新建 `verl/utils/reward_score/sum_task.py`，用 4.3.4 的 `compute_score`；或直接在路由里复用 `multiply.compute_score`（见 4.3.5 练习 1）。

**第 3 步：注册路由**（第三件套）—— 按 4.4.4 改 `verl/trainer/main_ppo.py` 两处（顶部 import + `_select_rm_score_fn` 加 `elif "sum"` 分支）。

**第 4 步：启动训练**—— 复用现成的 [scripts/train_tiny_zero.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh)，只改数据路径与实验名，其余参数原样复用：

```bash
# 示例命令（非项目原有脚本内容）
export N_GPUS=1
export BASE_MODEL={你的 Qwen2.5 模型路径}
export DATA_DIR=~/data/sum
export ROLLOUT_TP_SIZE=1
export EXPERIMENT_NAME=sum-qwen2.5-0.5b
export VLLM_ATTENTION_BACKEND=XFORMERS
bash ./scripts/train_tiny_zero.sh
```

> 注意：`train_tiny_zero.sh` 内部就是一条 `python3 -m verl.trainer.main_ppo` 加一组 Hydra 覆盖（见 [scripts/train_tiny_zero.sh:1-31](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L1-L31)），其中 `data.train_files=$DATA_DIR/train.parquet`、`data.val_files=$DATA_DIR/test.parquet` 会自动指向你的新数据，无需改脚本主体。

### 5.3 最小可跑的参数清单

| 配置项 | 取值 / 来源 | 作用 |
| --- | --- | --- |
| `data.train_files` / `data.val_files` | `$DATA_DIR/{train,test}.parquet` | 指向新任务数据（第 1 步产出） |
| `actor_rollout_ref.model.path` | `$BASE_MODEL` | Qwen2.5 基座（如 0.5B / 3B） |
| `data.max_prompt_length` / `max_response_length` | 256 / 1024（脚本默认） | 序列长度；求和可适当调小 |
| `algorithm.kl_ctrl.kl_coef` | 0.001 | KL 惩罚缰绳（防远离基座，详见 u5-l4） |
| `trainer.experiment_name` | `sum-...` | wandb 实验名 |
| `trainer.n_gpus_per_node` | `$N_GPUS` | GPU 数 |

### 5.4 需要观察的现象与预期结果

1. 训练**能正常启动**（不抛 `NotImplementedError`）——证明三件套接线正确。
2. 控制台出现 `do_print` 抽样打印，能看到模型逐渐学会输出 `<answer> 正确和 </answer>`。
3. wandb 上 `critic/score`（任务分）随训练上升；`response_length` 可能随之变化。
4. 待本地验证：实际 reward 曲线与是否涌现推理，受基座模型规模、学习率、`kl_coef` 等影响（参见 u7-l6 的调参解读）。

> 关键自检：若训练一启动就报 `NotImplementedError`，几乎一定是第 3 步路由没接对，或第 1 步 parquet 的 `data_source` 写错；若能启动但奖励恒为 0，多半是 prompt 的 `<answer>` 约定与奖励抽取标签不一致（回顾 4.2.3 的 `NOTE`）。

## 6. 本讲小结

- 新增 TinyZero 任务只需「**数据-奖励-路由**」三件套：一个数据预处理脚本、一个规则奖励函数、一行路由分支。
- `data_source` 是贯穿三件套的字符串契约：写进 parquet → 被 `RewardManager` 读出 → 经 `_select_rm_score_fn` 子串匹配路由到 `compute_score`。
- 第一件套 `make_prefix` 要保证 prompt 格式约定与奖励抽取标签一致（代码 `NOTE` 已警告），并以未闭合 `<think>` 引导续写。
- 第二件套 `compute_score` 按任务复杂度可选：countdown 需四步（提取/校验/求值/打分），只比数值的任务可极简到整数比较，甚至直接复用 `multiply.compute_score`。
- 第三件套 `_select_rm_score_fn` 改动最小：顶部 import + 一个 `elif "xxx" in data_source` 分支，未命中即 `NotImplementedError`。
- Worker、训练主循环、PPO/GRPO 算法、Ray 调度**全部原样复用**——这正是 TinyZero 把「任务」与「训练框架」解耦的价值。

## 7. 下一步学习建议

- 若想理解「分数如何驱动梯度」，继续读 **u5-l1（KL 惩罚与优势函数）**，看清 `compute_score` 产出的标量如何变成 token 级 reward 与 advantage。
- 若想理解「训练为什么能涌现推理」，读 **u7-l6（R1 Zero 的 Aha 现象与调参）**，结合 `format_score`、`kl_coef` 诊断你新任务的训练曲线。
- 若想给新任务接上「神经网络奖励模型」而非规则奖励，读 **u7-l4（Megatron 后端与 RewardModelWorker）**，了解 model-based RM 如何与 rule-based 奖励并存。
- 建议把本讲跑通的「两数求和」当作沙盒，逐步加大难度（限制可用数字、要求多步运算），亲手观察 R1 Zero 现象。
