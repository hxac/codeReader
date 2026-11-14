# u6-l3 可验证奖励：verifier 注册表与数学判分

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `VerifierFunction` 抽象基类的契约：输入什么、输出什么、配置怎么注入、同步与异步两条调用路径如何衔接。
2. 理解验证器的「自动发现」机制：为什么只要写一个子类，`build_all_verifiers` 就能枚举它，以及样本如何按数据集名被分发到对应验证器（`apply_verifiable_reward`）。
3. 掌握数学判分的两级流程：boxed/Minerva 答案抽取 + sympy 符号等价与 hendrycks 字符串规范化等价的组合判定。
4. 能独立编写并接入一个自定义验证器（本讲实践：质数判断验证器）。
5. 理解本轮更新引入的 `selected_input` 关键字参数与 `grading_*` 类属性声明：它们是 MILES 统一判分入口（详见 u8-l12）在 `ground_truth_utils` 这一层落地的「消费者迁移契约」——共享选择器只切一次思考边界，任务解析器不再各自二次切分。

## 2. 前置知识

- **RLVR（Reinforcement Learning with Verifiable Rewards）**：对有标准答案的任务（数学、代码、指令遵循），我们不训练奖励模型，而是用一个「验证器」程序直接判定模型输出对不对，输出分数当作奖励信号。这是 GRPO 训练（u6-l1）中奖励的来源之一。
- **ground truth（真值）**：每条训练样本自带的标签字段，可能是标准答案（`"42"`）、约束（IFEval 的指令 id 列表）、测试用例（代码题）或 rubric。验证器拿「模型输出 + 真值」打分。
- **验证器（verifier）**：一个「谓词式」的判分函数：`(prediction, label) → score ∈ [0, 1]`。它不产生梯度，只产生标量奖励。
- **思考模型与 `</think>` 边界**：推理模型的输出通常形如 `<think>推理过程…</think>最终答案`。历史代码里每个验证器各自用 `split("</think>")[-1]` 切掉思考段——本轮更新把这件事统一交给 MILES 判分层的共享选择器（`split_response`），验证器通过 `selected_input=True` 声明「传给我的已经是切好的」。
- **sympy**：Python 的符号数学库。`parse_latex` 能把 LaTeX 字符串解析成符号表达式，`simplify(expr1 - expr2) == 0` 即可判断两个写法不同的答案在数学上是否等价（如 `0.5` 与 `\frac{1}{2}`）。
- **异步与线程池**：RL 训练中判分往往和 GPU 推理并发，验证器因此提供 `async_call`；基类默认实现是把同步 `__call__` 丢进线程池，避免阻塞事件循环。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/ground_truth_utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py) | 验证器家族与注册表：`VerifierFunction` 基类、`VerificationResult`、约 20 个内置验证器、`build_all_verifiers` 自动发现、`apply_verifiable_reward` 分发、`RewardConfig` 奖励组装 |
| [open_instruct/math_utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py) | 数学判分工具：`last_boxed_only_string` boxed 抽取、`normalize_final_answer`/`strip_string` 两套规范化、`is_equiv`（sympy 符号等价）与 `hendrycks_is_equiv`（字符串等价） |
| [open_instruct/answer_utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/answer_utils.py) | 只依赖标准库的共享答案抽取：`extract_final_answer`（本轮新增 `selected_input` 参数） |
| [open_instruct/test_ground_truth_utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_ground_truth_utils.py) | 验证器单测：Puzzle/F1/GSM8K/LMJudge/IFEval 各类打分断言，`RewardConfig` 格式奖励门控测试 |
| [open_instruct/test_math_utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_math_utils.py) | `strip_string` 规范化单测 |
| [open_instruct/grpo.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/grpo.py) | 消费方示例：`main` 里用 `build_all_verifiers` 组装 `RewardConfig`（[L183-L193](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/grpo.py#L183-L193)） |

## 4. 核心概念与源码讲解

### 4.1 VerifierFunction 基类：验证器的统一契约

#### 4.1.1 概念说明

所有验证器共用一个抽象基类 `VerifierFunction`。它定义了三件事：

1. **统一签名**：`__call__(tokenized_prediction, prediction, label, query, rollout_state)`。`prediction` 是解码后的模型输出文本，`label` 是真值，`tokenized_prediction` 是 token id 列表（绝大多数验证器不用它，但按 token 长度打分的验证器需要），`query` 是原始问题（LM 裁判需要），`rollout_state` 携带多轮工具调用的逐轮奖励（u6-l6 会用到）。
2. **统一返回**：`VerificationResult(score, cost, reasoning, diagnostics)`——分数之外还带成本（LLM 裁判的 API 花费）、判分理由和诊断信息（如 sympy 超时计数）。
3. **统一异步路径**：`async_call` 默认把同步 `__call__` 丢进线程池；真正异步的验证器（LM 裁判、代码执行）自己重写它。

配置注入走 `VerifierConfig` 数据类家族：每个验证器类声明自己的 `get_config_class()`，`from_args` 用「字段名反射」从训练参数对象里自动抓取同名字段——这就是为什么给某个验证器加配置项只需要在 config 数据类里加字段。

#### 4.1.2 核心流程

```text
训练脚本 (grpo.py / grpo_fast.py)
    │
    ├─ args（argparse Namespace，含全部训练参数）
    │     │
    │     ▼
    ├─ build_all_verifiers(args)
    │     枚举 VerifierFunction.__subclasses__()
    │     每个子类: get_config_class().from_args(args) → 实例化 → 按 name 注册
    │     → dict[str, VerifierFunction] （key = 数据集名，如 "math" / "gsm8k"）
    │
    └─ RewardConfig(verifier_functions=上述 dict).build() → reward_fn
          训练循环每个 batch 调用 reward_fn
              └─ apply_verifiable_reward(...)   # 见 4.2
```

单个验证器内部的两条调用路径：

```text
同步: verifier(tok, prediction, label, query, rollout_state) → VerificationResult
异步: await verifier.async_call(tok, prediction, label, query, rollout_state,
                                 *, selected_input=False)
        基类默认实现: loop.run_in_executor(None, lambda: self.__call__(...))
        selected_input=True 时才把该关键字透传进 __call__
```

#### 4.1.3 源码精读

返回值结构 [open_instruct/ground_truth_utils.py:L97-L102](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L97-L102)：`score` 必填，`cost`/`reasoning`/`diagnostics` 可选——这让「免费的确实验证器」和「按 token 计费的裁判」共享同一个返回类型。

基类定义 [open_instruct/ground_truth_utils.py:L110-L188](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L110-L188)：

- [L118-L121](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L118-L121)：构造函数保存 `name`（即注册表 key，必须与数据集名对上）、`weight`（多条真值加权时的权重）和 `verifier_config`。
- [L133-L154](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L133-L154)：抽象 `__call__`，注意它的签名**没有** `selected_input`——这是有意为之，见 4.4。
- [L156-L185](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L156-L185)：本轮更新后的 `async_call`，新增关键字参数 `selected_input: bool = False`（[L163-L165](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L163-L165)）。关键实现技巧在 [L182-L184](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L182-L184)：只有当 `selected_input=True` 时才构造 `{"selected_input": True}` 并透传给 `__call__`。这样**没有**声明接受该参数的旧验证器子类在默认路径下完全不受影响，而声明了 `grading_accepts_selected_input = True` 的消费者（见 4.4）可以安全接收它。
- [L187-L188](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L187-L188)：`__repr__` 带类名与注册名，日志排查时能直接看出是哪个验证器打了分。

配置注入 [open_instruct/ground_truth_utils.py:L54-L75](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L54-L75)：`VerifierConfig.from_args(*arg_sources)` 遍历数据类字段名，从每个参数源上 `getattr` 同名属性，后源覆盖前源。基础 `VerifierConfig` 没有任何字段，所以「无配置需求」的验证器传任何 args 都能构造。带字段的子类如 [L78-L86](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L78-L86) 的 `LMJudgeVerifierConfig`（注意这些字段**没有默认值**，意味着 `build_all_verifiers` 要求 args 上必须带齐裁判配置——实践里的一个坑，见 4.2.4）。

#### 4.1.4 代码实践

**实践目标**：用最直接的方式跑通一次验证器调用，确认环境可用。

**操作步骤**：

1. 进入仓库根目录（已 `uv sync`，见 u1-l2）。
2. 运行仓库自带的验证器单测：

   ```bash
   uv run pytest open_instruct/test_ground_truth_utils.py -k "GSM8K or Puzzle" -q
   ```

3. 再在 Python 里手工调用一次（**示例代码**）：

   ```python
   from open_instruct.ground_truth_utils import GSM8KVerifier

   v = GSM8KVerifier()
   print(v([], "Therefore the answer is -3", "-3"))        # score=1.0
   print(v([], "The total cost is $96.00", "96"))           # score=1.0（Decimal 精确比较）
   ```

**需要观察的现象**：测试全绿；两次手工调用的 `score` 分别为 1.0。

**预期结果**：`GSM8KVerifier` 对带符号整数、小数尾零等情况给出正确判定（断言依据见 [test_ground_truth_utils.py:L167-L194](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_ground_truth_utils.py#L167-L194)）。若缺 `nltk` 数据导致 IFEval 相关用例报错，`-k` 过滤已把它们排除；完整文件对本讲非必需。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `VerificationResult` 要有 `cost` 字段，而普通数学验证器从来不用它？

**答案**：注册表里同时存在免费验证器（math/gsm8k）和 LLM 裁判（`LMJudgeVerifier`，按 token 计费）。统一返回类型让上层（`apply_verifiable_reward` 与训练指标）可以用同一套代码聚合分数与成本，`LMJudgeVerifier.get_cost`（[L788-L797](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L788-L797)）按模型单价×token 数计算并写入该字段。

**练习 2**：如果给 `MathVerifier` 加一个配置项 `max_candidates: int`，需要改动哪几处？

**答案**：三处——定义一个含该字段的 `VerifierConfig` 子类（或直接在某个现有 config 上加默认值字段）；在 `MathVerifier` 上重写 `get_config_class()` 返回该子类（参考 `MaxLenVerifier` 的 [L692-L699](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L692-L699)）；在训练入口的 argparse 里加同名参数，`from_args` 的字段名反射会自动把它带进来。

### 4.2 验证器自动发现与 apply_verifiable_reward 分发

#### 4.2.1 概念说明

「注册表」在当前代码里不是一张手写的字典，而是**运行时自动构建**的：`build_all_verifiers` 用 `VerifierFunction.__subclasses__()` 枚举所有已导入的子类，以每个实例的 `name`（小写）为 key 组成映射。数据侧每个样本带一个 `dataset` 字段（数据集名），打分时用这个名字查表——**数据集名就是路由键**。这就是文件顶部注释「Add new verifiers by subclassing VerifierFunction … automatically added」的含义（注意 [L1-L6](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1-L6) 的 docstring 还提到 `REWARD_FN_MAPPING`，这是历史名称，当前实现里该全局变量已不存在，以 `build_all_verifiers` 为准）。

`apply_verifiable_reward` 则是批量分发器：把一个 batch 的（response, ground_truth, dataset）三元组逐条查表、并发调用 `async_call`，再把每个验证器的分数按 `weight × reward_mult` 加权累加回每条 response。

#### 4.2.2 核心流程

`build_all_verifiers` 的流程（[L1239-L1276](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1239-L1276)）：

```text
for subclass in VerifierFunction.__subclasses__():
    if subclass == LMJudgeVerifier: continue          # 裁判单独处理
    config = subclass.get_config_class().from_args(args, streaming_config)
    verifiers[instance.name.lower()] = subclass(config)
    if subclass == CodeVerifier:                       # 额外克隆一个 stdio 版
        verifiers["code_stdio"] = ...
for judge_type in JUDGE_PROMPT_MAP:                    # 每种裁判类型一个实例
    verifiers[f"general-{judge_type}"] = LMJudgeVerifier(...)
if streaming_config.remap_verifier:                    # 可选改名路由
    verifiers[old_name] = verifiers[new_name]
```

`apply_verifiable_reward` 的流程（[L1298-L1361](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1298-L1361)）：

```text
for 每条 response:
    ground_truth_list = [gt] if isinstance(gt, str) else gt   # 一条 response 可配多条真值
    for (gt, ds) in zip(ground_truth_list, dataset_list):
        reward_func = mapping.get(ds.lower())                  # 数据集名 → 验证器
        if None: log warning, skip
        task = reward_func.async_call(tok, prediction, gt, query, rollout_state)
asyncio.gather(*tasks)                                          # 并发判分
for 每个结果:
    weighted = reward_mult × score × reward_func.weight
    response_rewards[i] += weighted
    response_per_func_rewards[i][验证器名] += weighted           # 分函数指标
```

#### 4.2.3 源码精读

- 自动发现主体 [open_instruct/ground_truth_utils.py:L1239-L1276](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1239-L1276)：`__subclasses__()` 只枚举**已被 import** 的子类，`ground_truth_utils` 模块自身定义了全部内置验证器，所以 import 该模块即完成注册。`code_stdio` 的克隆（[L1256-L1261](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1256-L1261)）只是把执行 API 的 URL 后缀从 `/test_program` 换成 `/test_program_stdio`。`remap_verifier`（[L1268-L1274](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1268-L1274)）允许把旧数据集名重定向到新验证器，数据不用重导。
- 分发器 [open_instruct/ground_truth_utils.py:L1323-L1337](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1323-L1337)：查表、找不到只警告不报错（脏数据集名不会炸掉整个 run）；每条 (response, gt, ds) 组合生成一个异步任务并记录元数据。注意本调用点**没有**传 `selected_input`，走默认 `False`——grpo.py / grpo_fast.py 的传统路径保持原行为，`selected_input` 目前只由 MILES 判分层传入（见 4.4.3）。
- 加权汇总 [open_instruct/ground_truth_utils.py:L1345-L1361](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1345-L1361)：`reward_mult` 来自 `RewardConfig.verification_reward`（默认 10.0，见 [L1371](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1371)），`per_func_rewards` 后来被 `RewardConfig.build` 聚成 `objective/<name>_reward`、`objective/<name>_correct_rate` 等训练指标（[L1456-L1463](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1456-L1463)）。
- 消费方接线 [open_instruct/grpo.py:L183-L193](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/grpo.py#L183-L193)：`RewardConfig(..., verifier_functions=build_all_verifiers(args, streaming_config))`，`grpo_fast.py` 同样在 [L2279](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/grpo_fast.py#L2279) 接线。`RewardConfig.build`（[L1380-L1473](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1380-L1473)）返回的 `reward_fn` 还叠加了 R1 格式奖励门控、non-stop 惩罚与多轮奖励聚合（`LastRewardAggregator`/`SumRewardAggregator`，[L1088-L1106](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1088-L1106)），这些属于 u6-l6 的环境话题，本讲只需知道入口。

#### 4.2.4 代码实践

**实践目标**：绕过完整训练入口，直接验证「数据集名 → 验证器 → 分数」的分发链路。

**操作步骤**（**示例代码**，在仓库根目录 `uv run python` 交互或脚本执行）：

```python
import asyncio
from types import SimpleNamespace

from open_instruct.ground_truth_utils import (
    GSM8KVerifier, MathVerifier, RewardConfig, apply_verifiable_reward
)

# 1. 手工构造注册表（不经过 build_all_verifiers，避免依赖完整训练 args）
mapping = {"gsm8k": GSM8KVerifier(), "math": MathVerifier()}

# 2. 直接调分发器：两个样本，dataset 字段决定路由
rewards, per_func = asyncio.run(apply_verifiable_reward(
    reward_fn_mapping=mapping,
    responses=[[1, 2], [3, 4]],                          # tokenized（GSM8K/Math 不读）
    decoded_responses=["The answer is 42", r"\boxed{\frac{1}{2}}"],
    ground_truths=["42", "0.5"],
    datasets=["gsm8k", "math"],
))
print(rewards, per_func)

# 3. 也可以用 RewardConfig 组装完整 reward_fn（mock 掉 infos，模仿单测做法）
reward_fn = RewardConfig(verification_reward=10.0, verifier_functions=mapping).build()
infos = SimpleNamespace(timeouts=[False]*2, tool_errors=[False]*2,
                        tool_outputs=[[], []], tool_calleds=[False, False],
                        rollout_states=None)
scores, metrics = asyncio.run(reward_fn(
    responses=[[], []],
    decoded_responses=["The answer is 42", "wrong"],
    ground_truths=["42", "42"],
    datasets=["gsm8k", "gsm8k"],
    finish_reasons=["stop", "stop"],
    infos=infos,
))
print(scores, metrics)
```

**需要观察的现象**：第一个调用返回 `[10.0, 10.0]`（`reward_mult` 默认 1.0 时是 `[1.0, 1.0]`——注意 `apply_verifiable_reward` 的 `reward_mult` 默认 1.0，而 `RewardConfig.build` 内部传 `verification_reward=10.0`）；`per_func` 里出现 `gsm8k`、`math` 两个 key；第二个调用 `scores` 为 `[100.0, 0.0]`（10.0 × 验证器得分）。

**预期结果**：`\boxed{\frac{1}{2}}` 对 `0.5` 判对（sympy 符号等价，见 4.3）；路由、加权、指标聚合全部按上述数值。完整行为**待本地验证**（以上数值由源码逻辑推得）。

**一个已知的坑**：直接调 `build_all_verifiers(args)` 需要args 携带 `LMJudgeVerifierConfig` 的全部必填字段（`llm_judge_model` 等），否则 `from_args` 构造时抛 `TypeError`——所以手工实验请像上面那样显式构造小映射。

#### 4.2.5 小练习与答案

**练习 1**：某样本的 `dataset` 字段拼错成 `"GSM8K "`（大写+尾随空格），会发生什么？

**答案**：查表用 `ds.lower()`（[L1324](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1324)），大写没问题；但尾随空格不会被去掉，`"gsm8k "` 匹配不到，走 [L1325-L1327](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1325-L1327) 的 warning + skip，该样本验证器奖励为 0。

**练习 2**：想让同一个数学数据集的一部分样本改用严格判分（`StrictMathVerifier`），不改数据文件怎么做？

**答案**：用 `streaming_config.remap_verifier`（格式 `old_name=new_name`，[L1268-L1274](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1268-L1274)）把数据里的名字重定向；或者更直接地，在数据制备阶段（u3-l3）把这部分样本的 `dataset` 字段写成 `strict_math`。

### 4.3 Math/GSM8K 验证器与数学等价判定

#### 4.3.1 概念说明

数学判分要解决两个正交问题：

1. **答案抽取**：模型输出是一大段推理文字，标准答案藏在哪里？`MathVerifier` 依次尝试 `\boxed{}`、Minerva 格式（`Final Answer: The final answer is $...$. I hope it is correct.`）、最后一个 `$...$` 对，最后回退到整段输出。`GSM8KVerifier` 则简单得多——取文本里**最后一个数字**。
2. **等价判定**：抽出来的字符串和真值怎么算「相等」？`0.5` 和 `\frac{1}{2}`、`96.00` 和 `96` 字符串上不等但数学上相等。两级判定：`is_equiv` 用 sympy 做符号等价（强但慢、可能超时），`hendrycks_is_equiv` 用一套字符串规范化做弱等价（快但只覆盖书写差异）。

GSM8K 另有一条独立的精确数值路径：用 `Decimal` 解析后精确比较，避免浮点误差和 `96.0 != 96` 这类假阴性；非数字标签回退到忽略大小写的字符串相等。

#### 4.3.2 核心流程

`MathVerifier.__call__`（[L245-L300](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L245-L300)）：

```text
candidates = []
1. boxed:  last_boxed_only_string(raw) → remove_boxed → 加入
2. Minerva: get_unnormalized_answer → normalize_final_answer → 加入（若非 [invalidanswer]）
3. 仅当前两步全空: 取最后一对 $...$ 之间的内容规范化后加入
4. 仍为空: normalize_final_answer(整段) 和 原始整段 都加入
for ans in candidates:
    if is_equiv(ans, label) or hendrycks_is_equiv(ans, label): return 1.0
return 0.0
```

`is_equiv` 的符号判定（[math_utils.py:L177-L212](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L177-L212)）：

\[ \text{equiv}(x_1, x_2) \iff \operatorname{simplify}\big(\operatorname{parse\_latex}(x_1) - \operatorname{parse\_latex}(x_2)\big) = 0 \]

整个比较包在 5 秒的 `SIGALRM` 超时里，超时计入 `diagnostics["symbolic_timeouts"]`（供 `MathVerifier` 透传到 `VerificationResult.diagnostics`）并返回 False——符号引擎卡死不会拖垮训练循环。

`hendrycks_is_equiv` 则是 `strip_string(both) == ` 后的字符串比较，`strip_string`（[L287-L347](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L287-L347)）做约 15 类规范化：删 `\left`/`\right`、`tfrac→frac`、百分号、单位、多余空格，补全 `\sqrt2 → \sqrt{2}`、`\frac12 → \frac{1}{2}`，甚至把 `0.5` 定死成 `\frac{1}{2}`。

`GSM8KVerifier.__call__`（[L216-L242](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L216-L242)）：

```text
去千分位逗号 → 正则 [-+]?(?:\d*\.\d+|\d+) 找全部数字 → 取最后一个
found, expected = Decimal(extracted), Decimal(label)      # _exact_number
若两者都可解析: 精确比较 Decimal 相等
否则: 忽略大小写的字符串比较
```

#### 4.3.3 源码精读

- boxed 抽取 [open_instruct/math_utils.py:L13-L37](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L13-L37)：`last_boxed_only_string` 从后往前找 `\boxed`，再用大括号计数器找到**配平的**右括号——`\boxed{\frac{1}{2}}` 里嵌套的花括号不会被提前截断。配套的 [remove_boxed L40-L51](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L40-L51) 剥掉外壳；`MathVerifier` 在 [L268-L273](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L268-L273) 捕获其 `AssertionError`（畸形 boxed）并置 None。
- Minerva 抽取 [open_instruct/math_utils.py:L54-L62](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L54-L62)：正则匹配固定话术 `Final Answer: The final answer is(.*?). I hope it is correct.`，找不到返回哨兵 `[invalidanswer]`。
- 弱规范化 [open_instruct/math_utils.py:L65-L158](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L65-L158)：`SUBSTITUTIONS`/`REMOVED_EXPRESSIONS` 两张替换表（删冠词、单位、`\\ldots` 等约 50 项）加上一组正则，是 Minerva 评测（Lewkowycz et al. 2022）的规范化移植。
- 超时守卫 [open_instruct/math_utils.py:L161-L174](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L161-L174)：上下文管理器封装 `signal.alarm`，只在主线程可用（这是验证器同步 `__call__` 依赖线程池执行的原因之一——线程池工作线程里 `signal` 不可用会怎样？`signal.signal` 在非主线程抛 ValueError，被 [L210-L212](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/math_utils.py#L210-L212) 的兜底 `except Exception` 捕获返回 False）。
- GSM8K 数值精确比较 [open_instruct/ground_truth_utils.py:L207-L242](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L207-L242)：`_exact_number` 用 `Decimal` 解析并拒绝非有限值；保留正负号的正则在 [L236](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L236)，对应单测里 `-3`/`+7` 的用例（[test_ground_truth_utils.py:L167-L178](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_ground_truth_utils.py#L167-L178)）。
- 规范化单测 [open_instruct/test_math_utils.py:L8-L24](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_math_utils.py#L8-L24)：9 组 `strip_string` 断言（`\tfrac{3}{4}→\frac{3}{4}`、`0.5→\frac{1}{2}` 等），是理解规范化的最短路径。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「字符串不等但数学等价」被正确判分。

**操作步骤**：

1. 运行规范化单测：

   ```bash
   uv run pytest open_instruct/test_math_utils.py -q
   ```

2. 在 `uv run python` 里做三组对比实验（**示例代码**）：

   ```python
   from open_instruct.math_utils import is_equiv, strip_string, hendrycks_is_equiv
   from open_instruct.ground_truth_utils import MathVerifier, StrictMathVerifier

   print(hendrycks_is_equiv(r"\frac{1}{2}", "0.5"))   # True：strip_string 把 0.5 定死为 \frac{1}{2}
   print(is_equiv("0.5", r"\frac{1}{2}"))             # True：sympy 符号差为 0
   print(is_equiv("0.33", "0.3333"))                  # False：符号差非 0

   v = MathVerifier()
   print(v([], r"The answer is $\frac{1}{2}$ I hope it is correct.", "0.5"))  # 1.0
   strict = StrictMathVerifier()
   print(strict([], r"\boxed{7}", "7"))               # boxed 不属于 Minerva 格式 → 回退整段规范化
   ```

**需要观察的现象**：`strip_string("0.5")` 输出 `\frac{1}{2}`；`MathVerifier` 与 `StrictMathVerifier` 对 boxed 输出的判定差异。

**预期结果**：前三行 True/True/False；`MathVerifier` 判 1.0。`StrictMathVerifier` 那一行的结果请根据 [L303-L330](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L303-L330) 的回退逻辑推导并运行核对——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`is_equiv("1+2", "3")` 返回什么？`hendrycks_is_equiv("1+2", "3")` 呢？

**答案**：`is_equiv` 返回 True（`parse_latex` 后相减化简为 0）；`hendrycks_is_equiv` 返回 False（`strip_string` 只删空格不计算，`1+2 ≠ 3`）。这说明了强弱两级判定互补的必要性。

**练习 2**：`MathVerifier` 为什么把「整段原始输出」也加进候选（[L290-L293](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L290-L293)）？

**答案**：这是最后的兜底。当模型既没写 boxed 也没写 Minerva 话术、也没有 `$...$` 时，如果整段输出本身就是答案（比如数据集的期望答案就是一段短文本），规范化后的整段与真值仍有机会匹配；同时保留未规范化原文，覆盖「规范化反而破坏匹配」的情况。

**练习 3**：`GSM8KVerifier` 为什么用 `Decimal` 而不是 `float` 比较？

**答案**：`float("96.00") == 96.0` 虽然为 True，但很多十进制小数（如 0.1）无法被二进制浮点精确表示，比较会产生假阴性/假阳性；`Decimal` 按十进制语义精确比较，且 `Decimal("96.00") == Decimal("96")` 为 True，正好覆盖「尾随零」用例（[test L180-L189](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_ground_truth_utils.py#L180-L189)）。

### 4.4 selected_input 与 grading_* 声明：MILES 统一判分的消费者契约（本轮更新）

#### 4.4.1 概念说明

这是本轮增量（PR #2250「Unify MILES grading inputs」）落在 `ground_truth_utils` 上的部分，完整机制在 u8-l12 讲解，这里只讲**本文件消费者视角**的迁移契约。

问题背景：思考模型的输出是 `<think>…</think>正文`。历史上每个验证器各自调用 `remove_thinking_section` 或 `extract_final_answer`，用 `split("</think>")[-1]` 这类规则「自己找答案边界」。边界情况（prompt 预填了闭合的 `</think>`、输出中途重开 `<think>`、非思考模式夹带标签）在每个消费者里各处理一遍，容易不一致。

MILES 的解法是**共享选择器切一次**：`open_instruct/miles/rewards/grading.py` 的 `split_response` 一次性把原始输出切成 `ResponseParts`（thinking / delivered response），判分层再把「选定的输入」传给验证器。于是验证器侧需要两件事：

1. **行为开关 `selected_input=True`**：传入的 `prediction` 已经是切好的选定文本，消费者**不得再切思考边界**。谁有能力接收这个开关？在类上声明 `grading_accepts_selected_input = True` 的验证器。注意任务级解析（`<answer>` 标签、`<|assistant|>` 清理、代码块、JSON）**保留不变**——那些是「任务格式」，不是「思考边界」。
2. **声明式输入契约 `grading_*` 类属性**：告诉判分层「我需要什么输入」。本轮 `MaxLenVerifier`/`UpToMaxLenVerifier` 声明了 `grading_objective = "process"`、`grading_scope = "full"`、`grading_uses_tokens = True`——它们按 token 计分，需要**整条轨迹**（含思考）的 token 序列，而不是切出的最终答案文本。

#### 4.4.2 核心流程

调用侧（MILES，[rewards.py:L539-L560](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/rewards.py#L539-L560)）：

```text
policy = 判分策略（由数据声明/registry 解析）
if policy 存在 且 verifier.grading_accepts_selected_input:
    extra["selected_input"] = True        # 传入"已选定"标记
tokens = 整条轨迹 tokens，仅当 policy.scope == "full" 时下发
    （token 计分器要求 full scope，否则只能拿到空列表）
```

验证器侧（本文件）：

```text
async_call(..., selected_input=True)          # 基类把它透传进 __call__
  → remove_thinking_section(prediction, selected_input=True)
       跳过 split("</think>")，但保留 <answer> 标签剥离与 <｜assistant｜> 清理
  → extract_final_answer(prediction, selected_input=True)
       跳过 </think> 后缀匹配，保留 <answer>…</answer> 抽取
```

兼容性设计：基类 `async_call` 只在 `selected_input=True` 时注入该关键字（4.1.3），因此**未声明**的旧验证器在传统路径（`apply_verifiable_reward`，从不传 `selected_input`）行为零变化；MILES 只对声明了 `grading_accepts_selected_input` 的验证器置位（rewards.py L544-L545），双向保护。

#### 4.4.3 源码精读

- `remove_thinking_section` [open_instruct/ground_truth_utils.py:L195-L204](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L195-L204)：`selected_input=True` 时跳过 [L200-L201](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L200-L201) 的 `split("</think>")[-1]`；`<|assistant|>` 清理与 `<answer>` 标签剥离（任务解析）保留。
- `extract_final_answer` [open_instruct/answer_utils.py:L10-L37](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/answer_utils.py#L10-L37)：同样的切分——[L29](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/answer_utils.py#L29) 在 `selected_input=True` 时把 `</think>` 匹配置 None，`<answer>` 抽取优先级不变。docstring（[L16-L17](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/answer_utils.py#L16-L17)）明确了这条契约："With selected_input=True, never interpret thinking boundaries again"。该文件刻意只用标准库，便于判分 worker 不引入训练依赖地复用同一套解析。
- 声明 `grading_accepts_selected_input = True` 并接收参数的消费者（本轮迁移完成的一批）：
  - `IFEvalVerifier`：声明在 [L343](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L343)，`__call__` 签名收参于 [L348-L357](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L348-L357)，透传给 `remove_thinking_section` 于 [L363](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L363)。`IFEvalVerifierOld` 同构（[L393](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L393)、[L409](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L409)）。
  - `F1Verifier`：[L514](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L514) 声明，[L529](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L529) 透传。
  - `PuzzleMatcherVerifier`：[L542](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L542) 声明，[L558](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L558) 透传。
  - `LMJudgeVerifier`：[L749](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L749) 声明，重写的 `async_call` 在 [L799-L812](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L799-L812) 收参并把 `selected_input` 传给 `extract_final_answer`——裁判读到的应当是共享选择器选定的最终回答。
- 声明「输入契约」的 token 计分器：
  - `MaxLenVerifier`：[L670-L672](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L670-L672) 声明 `grading_objective="process"`、`grading_scope="full"`、`grading_uses_tokens=True`；其 `__call__`（[L677-L690](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L677-L690)）用 `len(tokenized_prediction)` 打分——「过程目标、全轨迹、需要 token」三个声明正好对应它的实际输入需求。
  - `UpToMaxLenVerifier`：同样的声明在 [L709-L711](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L709-L711)。
  - MILES 侧消费这些声明：[open_instruct/miles/rewards/grading.py:L179-L186](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/grading.py#L179-L186) 把 `grading_scope`/`grading_objective` 读作策略默认值（objective 缺省 `"answer"`）；[rewards.py:L520-L525](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/rewards.py#L520-L525) 只在 `policy.scope == "full"` 时下发整条轨迹的 token——这就是 token 计分器必须声明 full scope 的原因。
- 传统路径不变式：`apply_verifiable_reward` 的调用点（[L1329-L1335](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1329-L1335)）不传 `selected_input`，grpo.py/grpo_fast.py 的行为与更新前逐位一致。

#### 4.4.4 代码实践

**实践目标**：观察 `selected_input` 两种模式的输出差异，并核对 `grading_*` 声明。

**操作步骤**（**示例代码**，`uv run python`）：

```python
from open_instruct.ground_truth_utils import remove_thinking_section, UpToMaxLenVerifier, VerifierFunction

raw = "<think>草稿: 答案是 42</think>最终答案是 <answer>42</answer>"

# 1. 传统模式：验证器自己切思考边界
print(repr(remove_thinking_section(raw, selected_input=False)))
# 2. 选定输入模式：假定上游已切好，不再二次切分
print(repr(remove_thinking_section(raw, selected_input=True)))

# 3. 观察：若 MILES 已把选定文本传进来（即 raw 本身就是"切好后"的正文，
#    但正文里又合法地引用了 </think> 字样），两种模式的行为差异
already_selected = "正文没有思考段，但引用了标签字面量 </think> 之后的内容"
print(repr(remove_thinking_section(already_selected, selected_input=False)))  # 误切
print(repr(remove_thinking_section(already_selected, selected_input=True)))   # 保留原文

# 4. 查看输入契约声明
print(UpToMaxLenVerifier.grading_objective, UpToMaxLenVerifier.grading_scope,
      UpToMaxLenVerifier.grading_uses_tokens)
print(hasattr(UpToMaxLenVerifier, "grading_accepts_selected_input"))  # False：token 计分器不收选定文本
```

**需要观察的现象**：第 1、2 行输出相同（raw 含 `</think>` 时两种模式都得到 `最终答案是 42`）；第 3 组是关键差异——`selected_input=False` 会把「正文中引用的标签字面量」当成边界误切，`True` 则完整保留；第 4 步打印 `process full True` 和 `False`。

**预期结果**：由 [L195-L204](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L195-L204) 的逻辑直接推得；请运行核对（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`MathVerifier` 为什么**不**声明 `grading_accepts_selected_input`？

**答案**：`MathVerifier` 的候选抽取（`last_boxed_only_string`、最后一个 `$...$` 对、整段回退）本来就要扫**整段原始输出**（思考里出现 boxed 也算数，见 [L264](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L264) 直接用 `prediction`），它不需要「切好的选定输入」。MILES 侧对这类验证器继续传原始输出（其策略由 `grading_objective` 等声明或缺省值推导，见 u8-l12）。

**练习 2**：如果一个验证器声明了 `grading_accepts_selected_input = True` 但 `__call__` 签名里没写 `selected_input` 参数，会发生什么？

**答案**：MILES 置位 `extra["selected_input"] = True` 后，基类 `async_call` 会把它作为关键字参数注入 `self.__call__(..., selected_input=True)`（[L182-L184](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L182-L184)），签名不匹配直接抛 `TypeError`。声明与签名必须成对出现——这正是本轮迁移逐个修改 `IFEvalVerifier` 等子类签名的原因。

**练习 3**：`grading_uses_tokens = True` 的验证器如果把 `grading_scope` 声明成 `"response"`，会出什么问题？

**答案**：MILES 调用侧只在 `policy.scope == "full"` 时下发轨迹 token（[rewards.py:L520-L525](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/rewards.py#L520-L525)），scope 不是 full 时 token 被置空，长度打分全部失真；且「token 计分器要求 full scope」会在 registry 加载校验时被拒绝（u8-l12 的 `validate_registry`）。

## 5. 综合实践

把本讲四个模块串起来：**写一个质数判断验证器，走完「定义 → 注册 → 分发 → 判分 → 契约声明」全链路。**

### 实践目标

实现 `PrimeVerifier`：从模型输出抽取最后一个整数，判断它是否为质数；`label` 取 `"prime"` 或 `"not_prime"`。同时让它符合本轮新增的消费者契约（接受 `selected_input`）。

### 操作步骤（示例代码）

在仓库根目录新建临时脚本运行（不要改动 `open_instruct/` 源码）：

```python
import asyncio
import re
from types import SimpleNamespace

from open_instruct.ground_truth_utils import (
    RewardConfig, VerifierConfig, VerifierFunction, VerificationResult, apply_verifiable_reward,
)


class PrimeVerifier(VerifierFunction):
    """答案应为（或不应为）质数：label ∈ {"prime", "not_prime"}。"""

    grading_accepts_selected_input = True   # 声明：可接收已切分好的选定输入

    def __call__(self, tokenized_prediction, prediction, label,
                 query=None, rollout_state=None, *, selected_input=False):
        # 任务级解析：取最后一个整数（模仿 GSM8KVerifier 的抽取方式）
        numbers = re.findall(r"-?\d+", prediction)
        if not numbers:
            return VerificationResult(score=0.0)
        n = int(numbers[-1])
        is_prime = n > 1 and all(n % d for d in range(2, int(n**0.5) + 1))
        want_prime = str(label).strip().lower() == "prime"
        return VerificationResult(score=float(is_prime == want_prime))


# 1. 五组样例验证打分
cases = [
    ("The answer is 7",  "prime",     1.0),
    ("The answer is 8",  "prime",     0.0),
    (r"\boxed{2}",       "prime",     1.0),
    ("<answer>9</answer>", "not_prime", 1.0),
    ("答案是 42",         "not_prime", 1.0),
]
v = PrimeVerifier(VerifierConfig(), )  # name 默认取构造参数；也可 super().__init__("prime_check")
for pred, label, want in cases:
    got = v([], pred, label).score
    assert got == want, (pred, label, got, want)

# 2. 接入 RewardConfig 并走分发链路（dataset 名 = 注册 key）
mapping = {"prime_check": v}
rewards, per_func = asyncio.run(apply_verifiable_reward(
    reward_fn_mapping=mapping,
    responses=[[9], [9], [9], [9], [9]],
    decoded_responses=[c[0] for c in cases],
    ground_truths=[c[1] for c in cases],
    datasets=["prime_check"] * 5,
))
print(rewards, per_func)

# 3. 验证 selected_input 通路：异步调用并置位（基类会把它透传给 __call__）
result = asyncio.run(v.async_call(
    [], "<think>7 是质数</think>答案就是 7", "prime", selected_input=True))
print(result)   # __call__ 收到 selected_input=True，本验证器不切边界也能抽到最后数字 7 → 1.0
```

### 需要观察的现象

- 五组样例断言全部通过（含负号正则不误伤 `not_prime` 场景）。
- `apply_verifiable_reward` 返回的 `rewards` 全为 1.0（默认 `reward_mult=1.0`），`per_func` 只含 `prime_check` 一个 key。
- 第 3 步 `async_call(..., selected_input=True)` 正常返回 score=1.0——证明声明与签名成对后，基类透传链路可用。

### 预期结果与扩展

- 想让它进入 `build_all_verifiers` 自动注册，只需把类放进某个被 import 的模块并让构造函数调用 `super().__init__("prime_check", weight=1.0)`；然后在数据制备（u3-l3）把样本的 `dataset` 字段写成 `prime_check`。
- 对照检查：`UpToMaxLenVerifier.grading_objective/grading_scope/grading_uses_tokens`（[L709-L711](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L709-L711)）与你的 `grading_accepts_selected_input`——一个是「我要什么输入」的契约，一个是「我能吃选定输入」的能力开关。
- 以上运行结果**待本地验证**；如断言失败，优先检查正则 `-?\d+` 是否把 `not_prime` 里的数字抓走（它出现在 label 而非 prediction，不影响）。

## 6. 本讲小结

- 验证器的统一契约是 `VerifierFunction`：`__call__(tok, prediction, label, query, rollout_state) → VerificationResult(score, cost, reasoning, diagnostics)`，配置经 `VerifierConfig.from_args` 字段名反射注入。
- 注册表是**运行时自动发现**的：`build_all_verifiers` 枚举 `__subclasses__()`，以实例 `name` 为 key；样本按 `dataset` 字段查表，由 `apply_verifiable_reward` 并发分发并按 `weight × reward_mult` 加权汇总。
- 数学判分 = 多策略抽取（boxed → Minerva → 最后 `$...$` → 整段回退）+ 两级等价判定（sympy 符号差为零 / `strip_string` 字符串规范化），GSM8K 用 `Decimal` 精确数值比较。
- 本轮更新（PR #2250）给这套体系接上了 MILES 统一判分入口：`async_call` 新增 `selected_input` 关键字（基类仅在其为 True 时透传），`IFEval/F1/Puzzle/LMJudge` 等消费者声明 `grading_accepts_selected_input` 并在 `selected_input=True` 时**不再二次切分思考边界**（`remove_thinking_section`/`extract_final_answer` 保留任务级 `<answer>` 解析）。
- `MaxLen/UpToMaxLen` 用 `grading_objective/grading_scope/grading_uses_tokens` 声明「过程目标、全轨迹、需要 token」的输入契约，MILES 读取这些声明决定下发整条轨迹 token 还是选定文本。
- 传统 grpo.py / grpo_fast.py 路径完全不传 `selected_input`，行为与更新前一致——新契约是纯增量。

## 7. 下一步学习建议

- **u6-l2（GRPO 损失与优势计算）**：验证器产生的标量奖励如何变成组相对优势并进入策略梯度——奖励消费的下一站。
- **u6-l6（工具调用 RL 环境）**：`rollout_state` 里的逐轮奖励如何被 `LastRewardAggregator`/`SumRewardAggregator` 聚合，`RewardConfig.build` 的完整门控逻辑（格式奖励、non-stop 惩罚）在那里展开。
- **u8-l12（MILES 统一判分输入）**：`split_response`/`ResponseParts` 共享选择器、`GradingPolicy` 的 objective×scope×require_response 组合约束，以及 `_VerifierRegistry` 如何校验本文这些 `grading_*` 声明——本讲 4.4 的完整上游故事。
- **u6-l7 / u6-l8（代码奖励与执行客户端）**：`CodeVerifier` 的外部执行服务、`code_stdio` 克隆与沙箱协议，验证器家族中最重的一支。
