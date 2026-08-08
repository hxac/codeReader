# Sample 数据结构与生命周期

## 1. 本讲目标

本讲精读 slime 中**贯穿整个训练闭环**的核心数据载体 `Sample`。学完后你应该能够：

- 说清 `Sample` 的关键字段（`tokens` / `response` / `loss_mask` / `reward` / `rollout_log_probs` / `rollout_id` 等）各自**在哪一阶段被写入、又在哪一阶段被读取**。
- 读懂 `Sample.Status` 五态状态机（`PENDING` / `COMPLETED` / `TRUNCATED` / `ABORTED` / `FAILED`）的转移条件。
- 认识 rollout 函数的产出结构 `RolloutFnTrainOutput` / `RolloutFnEvalOutput`，理解「一次 rollout 到底吐出什么」。
- 手工构造一个 `Sample`，并跟踪它从生成到进入训练张量的全过程。

本讲是整个「数据生成（rollout）层」单元（U3）的地基：后续讲默认 rollout 流程（u3-l2）、数据源（u3-l3）、奖励模型（u3-l4）都会反复回到这里定义的字段。

## 2. 前置知识

在开始前，请确认你已建立以下认知（来自前置讲义）：

- **三大模块闭环**：slime 把 RL 训练切成 rollout（采样）→ data buffer（桥梁）→ training（训练）三段，每轮重复，训后把权重单向同步回 rollout。`Sample` 就是在这条闭环上流动的「快递箱」。
- **train.py 主循环**：`generate / async_train / update_weights` 每轮必做，采样、训练、同步三阶段恰好对应 `Sample` 的三段生命。
- **dataclass 基础**：Python 的 `@dataclass` 是一种自动生成 `__init__`/`__repr__` 的类声明方式；`field(default_factory=list)` 表示「每个实例各自一个新空列表」。本讲的 `Sample` 就是一个 dataclass。

一个关键直觉：在普通训练框架里，一条训练样本通常就是「输入张量 + 标签」。但在 RL 后训练里，一条样本需要携带**远比这多的信息**——它是模型自己刚生成出来的，所以要额外记录「生成时的对数概率」「哪些 token 该算 loss」「这次回答的奖励是多少」「为什么停下来」。`Sample` 就是为了把这些信息统一打包而设计的。

## 3. 本讲源码地图

本讲只涉及两个源码文件，外加一个消费方文件用于说明字段去向：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [`slime/utils/types.py`](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py) | 定义 `Sample` dataclass、`Status` 枚举、`ParamInfo`、`RolloutBatch` 等 | `Sample` 的全部字段、`Status` 状态机、`append_response_tokens` 增量维护方法 |
| [`slime/rollout/base_types.py`](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py) | 定义 rollout 函数的输出容器 | `RolloutFnTrainOutput` / `RolloutFnEvalOutput` / `call_rollout_fn` |
| [`slime/ray/rollout.py`](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py) | rollout 编排层，把 `Sample` 转成训练张量 | `_convert_samples_to_train_data`（说明字段如何流向训练） |
| [`slime/rollout/sglang_rollout.py`](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py) | 默认 rollout 实现 | `generate` 函数如何填充 `Sample` 字段、如何置 `Status` |

## 4. 核心概念与源码讲解

### 4.1 Sample dataclass：贯穿闭环的核心数据载体

#### 4.1.1 概念说明

`Sample` 是 slime 中**唯一**贯穿 rollout 与 training 两大模块的数据结构。一个 `Sample` 实例从数据源（data source）取出时几乎是一张白纸，随后在 rollout 阶段被层层填充：先填 prompt、再生成 response、再算奖励、再打 loss_mask……最终在训练阶段被「展开」成 Megatron 能吃的张量字典。

为什么要用一个大而全的 dataclass，而不是拆成多个小对象？因为一条 RL 样本的各字段必须**长度对齐、语义同源**：`tokens` 是完整序列，`loss_mask` 必须和它的 response 部分等长，`rollout_log_probs` 也必须等长。把它们放进同一个对象，配合一套严格的长度校验，能保证「错位」这种隐蔽 bug 在填充时就被抓出来，而不是等到 loss 算出 NaN 才发现。

#### 4.1.2 核心流程：一个 Sample 的三段生命

可以把 `Sample` 的字段按「生命周期」分成三组，对应闭环的三段：

```text
[数据源取出]   →  [rollout 阶段填充]   →  [训练阶段消费]
                                          │
prompt ─────────► 拼成 prompt_ids 喂 SGLang ─► （不直接进训练张量）
                                   │
                  tokens = prompt + response ─► tokens          ← 打包/loss
                  response_length ───────────► response_lengths  ← 打包
                  loss_mask ─────────────────► loss_masks        ← loss（标记哪些 token 训练）
                  rollout_log_probs ─────────► rollout_log_probs ← off-policy 修正
                  reward ────────────────────► rewards           ← 优势估计
                  status ────────────────────► truncated         ← 指标/过滤
                  rollout_id ────────────────► rollout_ids       ← 多段聚合
```

关键点：**很多字段在 rollout 阶段才被写入，但它们的「读者」在训练阶段**。例如 `rollout_log_probs`（生成时的对数概率）由 SGLang 返回，记录在 `Sample` 上，训练时用它和行为策略做重要性采样比（importance ratio），用于 off-policy 修正。这种「写读分离」正是 `Sample` 存在的意义。

#### 4.1.3 源码精读

**字段定义**　`Sample` 是一个标准 dataclass，注释 `# The sample generated` 点明了它的角色：

[slime/utils/types.py:L93-L119](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L93-L119) —— 这是 `Sample` 的核心字段区，包含 prompt、tokens、response、response_length、reward、loss_mask、rollout_log_probs 等。

下面把最常打交道的字段逐个说明：

| 字段 | 类型 | 默认值 | 写入阶段 | 读取阶段 / 用途 |
|------|------|--------|---------|-----------------|
| `prompt` | `str \| list[dict]` | `""` | 数据源取出 | rollout：拼成 prompt_ids 喂推理引擎 |
| `tokens` | `list[int]` | `[]` | rollout：先存 prompt，再 append response | 训练打包：完整 token 序列（prompt+response） |
| `response` | `str` | `""` | rollout：`append_response_tokens(text=...)` | 评估/日志展示 |
| `response_length` | `int` | `0` | rollout：append 时累加 | 训练打包 / loss_mask 长度校验 |
| `reward` | `float \| dict \| None` | `None` | rollout：奖励模型计算 | 优势估计（advantage） |
| `loss_mask` | `list[int] \| None` | `None` | rollout：append 时按 `trainable` 写 1/0 | loss：标记哪些 response token 参与训练 |
| `rollout_log_probs` | `list[float] \| None` | `None` | rollout：SGLang `meta_info` 返回 | loss / off-policy 修正（重要性比） |
| `rollout_id` | `int \| None` | `None` | rollout / 数据源 | 训练：把一次 rollout 拆成多段时同源聚合 |
| `remove_sample` | `bool` | `False` | 过滤钩子 | 训练：为真则整条 loss_mask 清零 |
| `teacher_log_probs` | `list[float] \| None` | `None` | ref/teacher 前向 | 在线策略蒸馏（OPD） |

**rollout_id 的特殊语义**　这是字段里最需要小心理解的一个。它的注释说明：默认 `None` 时下游回退到 `index`（一次执行=一条训练样本，故 `rollout_id == index`）；而把一次 rollout 执行拆成多条训练样本（compact/subagent 路径）时，**所有兄弟段必须设同一个 `rollout_id`**，这样损失聚合会在一个 rollout 内部平均，而不会把一条 rollout 重复计数：

[slime/utils/types.py:L99-L106](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L99-L106) —— rollout_id 的语义注释。

**奖励取值的小工具**　当奖励是 dict（例如远程 RM 返回多个子奖励）时，用 `get_reward_value` 按 `reward_key` 选其中一个：

[slime/utils/types.py:L246-L247](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L246-L247) —— `get_reward_value(args)` 按是否设了 `reward_key` 决定取标量还是字典项。

**append_response_tokens：增量维护的核心方法**　这是理解 `Sample` 生命周期的关键。多轮 agent / partial rollout 场景下，response 是**一段一段拼上去的**，而不是一次性生成。每拼一段，`tokens` / `response_length` / `loss_mask` / `rollout_log_probs` 必须同步增长。该方法用一组校验保证它们始终等长：

[slime/utils/types.py:L253-L314](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L253-L314) —— `append_response_tokens` 的全部逻辑。

它的核心约定（见 docstring）是：**模型生成的 token 传 `trainable=True` 并带 SGLang 的 `meta_info` 与 logp；工具/环境的 token 传 `trainable=False`，它们得到 loss_mask 0、且不带 logp**。这直接对应「loss_mask 标注」的语义。几个关键分支：

- `trainable=True` 且无 logp → 直接报错（可训练 token 必须有 rollout logp）：

[slime/utils/types.py:L276-L277](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L276-L277)

- `trainable=False` 时把 logp 填成 0 占位，并把 loss_mask 追加 0：

[slime/utils/types.py:L278-L281](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L278-L281)

- 实际追加 tokens / response_length / loss_mask：

[slime/utils/types.py:L287-L292](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L287-L292)

- 收尾做长度校验，确保 loss_mask 与 rollout_log_probs 都等于 response_length：

[slime/utils/types.py:L418-L425](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L418-L425) —— `_validate_response_metadata_lengths`。

**字段如何流向训练**　rollout 编排层的 `_convert_samples_to_train_data` 把一列 `Sample` 展开成训练张量字典。注意它**直接读取**上面那些字段：

[slime/ray/rollout.py:L734-L744](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L734-L744) —— 把 `tokens` / `response_lengths` / `rewards` / `status→truncated` / `index` / `rollout_ids` 装进 `train_data`。

其中 `rollout_log_probs` 的去向注释写得很直白——「用于 off-policy 修正」：

[slime/ray/rollout.py:L791-L793](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L791-L793)

这条线索确认了：`rollout_log_probs` 在 rollout 写入、在训练 loss 阶段读取。

#### 4.1.4 代码实践

> **实践目标**：手工构造一个 `Sample`，填入各关键字段，然后逐字段说明它会被哪个阶段读取。

**操作步骤**（这是源码阅读型 + 最小调用型实践，无需 GPU）：

1. 在 slime 仓库根目录确保已 `pip install -e . --no-deps`（见 u1-l3），使 `from slime.utils.types import Sample` 可用。
2. 新建一个临时脚本（示例代码，非项目原有文件）：

```python
# 示例代码：手工构造一个 Sample 并观察字段
from slime.utils.types import Sample

# 1) 从数据源取出的初始形态：只有 prompt
s = Sample(index=0, group_index=0, prompt="1+1=")
print("初始:", s.status)            # Status.PENDING

# 2) 模拟 rollout 阶段：先存 prompt token，再分段 append response
s.tokens = [10, 11, 12]            # 假装 prompt_ids
# 第一段：模型生成 2 个 token（可训练，带 logp）
s.append_response_tokens(
    tokens=[13, 14],
    log_probs=[-0.5, -0.2],
    trainable=True,
    text="2",
)
print("response_length:", s.response_length)   # 2
print("loss_mask:", s.loss_mask)               # [1, 1]
print("rollout_log_probs:", s.rollout_log_probs)  # [-0.5, -0.2]

# 第二段：工具返回的 token（不可训练，loss_mask=0）
s.append_response_tokens(
    tokens=[99],
    trainable=False,              # 不传 log_probs
    text="<tool_result>",
)
print("loss_mask:", s.loss_mask)               # [1, 1, 0]

# 3) 奖励
s.reward = 1.0

# 4) 长度校验（手写错位会抛 ValueError）
s._validate_response_metadata_lengths()
```

3. 运行并观察每个 `print`。

**需要观察的现象**：
- 初始 `status` 是 `PENDING`。
- 第一段 append 后，`loss_mask` 为 `[1, 1]`，`rollout_log_probs` 为 `[-0.5, -0.2]`，二者长度都等于 `response_length=2`。
- 第二段（`trainable=False`）append 后，`loss_mask` 变成 `[1, 1, 0]`——工具 token 不参与 loss，但 logp 被 0 占位以保持等长。

**预期结果**：脚本能完整跑完且不抛异常，证明各字段长度始终对齐。

**待本地验证**：`pip install` 在你的环境是否成功、具体 token id 的打印值取决于本地是否真的能 import slime；如果暂无环境，可只读 `append_response_tokens` 源码推断上述输出。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 agent 回合里，模型生成的 token 没有传 `log_probs` 就把 `trainable` 设成 `True`，会发生什么？

> **参考答案**：`append_response_tokens` 会在 [types.py:L276-L277](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L276-L277) 抛出 `ValueError("trainable response tokens require rollout log probabilities.")`。因为可训练 token 必须有行为策略 logp，否则训练阶段无法做重要性采样。

**练习 2**：`effective_response_length` 属性和 `response_length` 字段有什么区别？什么时候相等？

> **参考答案**：`effective_response_length` 在 [types.py:L249-L251](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L249-L251) 定义为「有 loss_mask 时取 `sum(loss_mask)`，否则取 `response_length`」。当 loss_mask 全是 1 时二者相等；当存在工具/环境 token（loss_mask 含 0）时，`effective_response_length` 才反映「真正参与训练的 token 数」。

### 4.2 Sample.Status 状态机

#### 4.2.1 概念说明

`Sample` 不只是数据的集合，它还携带一个 `status` 字段，记录这条样本「当前处于什么状态」。这是 rollout 阶段一个轻量级的状态机：一条样本从 `PENDING`（待生成）出发，生成完成后进入某个**终止态**。终止态决定了这条样本后续如何被处理——是正常进训练、还是被丢弃、还是放回缓冲区下轮续。

状态机之所以重要，是因为 slime 要处理**部分生成（partial rollout）**与**中断（abort）**：一个样本可能因为超时只生成了一半，slime 不能直接丢，而要标记成可续传的状态下轮接着做。`Status` 就是这套机制的标记位。

#### 4.2.2 核心流程：五态转移

`Status` 是定义在 `Sample` 内部的 `Enum`，五个取值：

```text
PENDING ──生成完成──► COMPLETED   (finish_reason.type == "stop"，正常停止)
        ├─生成完成──► TRUNCATED   (finish_reason.type == "length"，达到 max_new_tokens)
        ├─超时/取消──► ABORTED     (finish_reason.type == "abort"，被外部 abort)
        └─可恢复失败─► FAILED      (工具调用/外部 API/解析错误，可重试)
```

注意转移不是任意的：`generate` 函数开头会断言样本必须处于 `PENDING` 或 `ABORTED` 才能继续生成（`ABORTED` 可被续传），而 `generate_and_rm` 会在遇到 `COMPLETED`/`TRUNCATED` 时直接跳过生成。

终止态大多由推理引擎返回的 `finish_reason.type` 决定，这套映射写在 `_apply_meta_info` 里。

#### 4.2.3 源码精读

**枚举定义**　五个状态及各自的中文注释：

[slime/utils/types.py:L130-L138](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L130-L138) —— `Status` 枚举，含 `FAILED` 的区别说明（可恢复、可能含部分输出、可重试）。

**由 finish_reason 驱动的转移**　`_apply_meta_info` 的末尾用 `match/case` 把引擎返回的停止原因映射成终止态：

[slime/utils/types.py:L410-L416](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L410-L416) —— `length→TRUNCATED`、`abort→ABORTED`、`stop→COMPLETED`。

**generate 入口的前置断言**　默认 rollout 的 `generate` 函数要求样本必须处于可生成状态：

[slime/rollout/sglang_rollout.py:L160-L162](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L160-L162) —— 只允许 `PENDING` 或 `ABORTED`。

一个边界情况：当 `max_new_tokens == 0`（不生成任何新 token），样本直接被置为 `TRUNCATED`：

[slime/rollout/sglang_rollout.py:L169-L171](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L169-L171)

**generate_and_rm 的跳过逻辑**　已经完成的样本不必再生成，直接返回；这支撑了 partial rollout「下轮续做」：

[slime/rollout/sglang_rollout.py:L234-L238](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L234-L238)

**status 如何影响训练**　终止态最终流进训练张量的 `truncated` 字段（`TRUNCATED→1`，其余→0），用于指标统计与（在需要时）奖励修正：

[slime/ray/rollout.py:L741](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L741) —— `"truncated": [1 if sample.status == Sample.Status.TRUNCATED else 0 ...]`。

值得强调：训练阶段**只区分 `TRUNCATED` 与否**，并不会因为 `COMPLETED`/`ABORTED` 在张量层面区别对待（`ABORTED` 样本通常在 rollout 阶段就被放回缓冲或丢弃，见 u3-l2）。

#### 4.2.4 代码实践

> **实践目标**：通过修改 `finish_reason`，观察 `status` 的转移。

**操作步骤**：

1. 复用 4.1.4 的脚本，构造一个 `Sample`。
2. 直接调用内部方法模拟引擎返回（示例代码）：

```python
# 示例代码：模拟 finish_reason 驱动状态转移
from slime.utils.types import Sample

class _Args:  # 最小 args 占位
    pass

s = Sample(index=0, prompt="hi")
s.tokens = [1, 2, 3]
s.response_length = 0

# 模拟 SGLang 返回 type=="stop"
s._apply_meta_info(_Args(), {"finish_reason": {"type": "stop"}}, new_token_count=0, update_terminal_info=True)
print(s.status)   # Status.COMPLETED

s2 = Sample(index=1, prompt="hi")
s2._apply_meta_info(_Args(), {"finish_reason": {"type": "length"}}, new_token_count=0, update_terminal_info=True)
print(s2.status)  # Status.TRUNCATED
```

3. 把 `"stop"` 换成 `"length"`、`"abort"`，分别观察 `status`。

**需要观察的现象**：`stop→COMPLETED`、`length→TRUNCATED`、`abort→ABORTED`。

**预期结果**：三个 print 分别打出对应的枚举值。

**待本地验证**：`_apply_meta_info` 的精确参数在不同版本可能微调；若签名不符，请以当前源码 [types.py:L316](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L316) 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate` 只允许从 `PENDING` 或 `ABORTED` 进入，而不允许从 `COMPLETED` 进入？

> **参考答案**：`COMPLETED` 表示样本已正常结束，再生成会重复回答、破坏 token 与 loss_mask 的对齐。而 `ABORTED` 是「半途中断」，slime 设计成可续传（partial rollout），所以允许带着已生成的部分再次进入 `generate`。

**练习 2**：`ABORTED` 与 `FAILED` 都表示「没正常完成」，它们的区别是什么？

> **参考答案**：见 [types.py:L135-L138](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L135-L138) 的注释。`ABORTED` 通常来自超时/主动取消（引擎返回 `finish_reason.type=="abort"`）；`FAILED` 指可恢复的非致命错误（工具调用失败、外部 API 报错、解析错误），可能仍含部分有效输出，可被重试或优雅处理。前者偏向「资源问题」，后者偏向「逻辑问题」。

### 4.3 RolloutFn 输出结构：RolloutFnTrainOutput / RolloutFnEvalOutput

#### 4.3.1 概念说明

前面两节讲的是「单条样本」。但 rollout 函数（如默认的 `generate_rollout`）一次要处理一大批样本，并最终把结果交还给主循环。这个「整批结果」需要一个统一的外包装——这就是 `base_types.py` 里的两个 dataclass：

- `RolloutFnTrainOutput`：训练用 rollout 的产出，核心是 `samples: list[list[Sample]]`。
- `RolloutFnEvalOutput`：评估用 rollout 的产出，核心是 `data: dict[str, dict[str, Any]]`。

为什么训练输出是「列表的列表」？外层 list 是「每个 prompt 一组」，内层 list 是「一个 prompt 的多条采样」（对应 `n_samples_per_prompt`）。这是 GRPO 类算法的基础——同一 prompt 采样多条，组内做相对比较。

#### 4.3.2 核心流程：rollout 函数返回什么

```text
rollout_fn(args, rollout_id, data_source, evaluation=?) 
        │
        ├── evaluation=False ─► RolloutFnTrainOutput(samples=list[list[Sample]], metrics=...)
        │
        └── evaluation=True  ─► RolloutFnEvalOutput(data={数据集名: {rewards, truncated, samples}}, metrics=...)
```

`call_rollout_fn` 是一个兼容层：它调用真正的 rollout 函数，并保证返回值一定是这两个类型之一（老版本可能直接返回裸 list/dict，由它自动包装）。

#### 4.3.3 源码精读

**两个输出容器**　非常简洁：

[slime/rollout/base_types.py:L7-L16](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py#L7-L16) —— `RolloutFnTrainOutput(samples, metrics)` 与 `RolloutFnEvalOutput(data, metrics)`。

注意 `metrics` 默认 `None`，两个字段都可携带额外的统计指标。

**兼容包装函数**　`call_rollout_fn` 负责把「老式返回」自动包成正确类型：

[slime/rollout/base_types.py:L19-L26](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py#L19-L26) —— 若返回值不是这两个类型，按 `evaluation` 标志自动包成 `RolloutFnEvalOutput` 或 `RolloutFnTrainOutput`。

**真实生产者**　默认 rollout 函数 `generate_rollout` 就是按这套契约返回的：

[slime/rollout/sglang_rollout.py:L618-L635](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L618-L635) —— 函数签名声明返回 `RolloutFnTrainOutput | RolloutFnEvalOutput`，按 `evaluation` 分流。

评估路径的产出结构则是一个以数据集名为 key 的字典，里面含 `rewards` / `truncated` / `samples`：

[slime/rollout/sglang_rollout.py:L609-L615](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L609-L615) —— eval 路径返回的 dict 结构，正好填进 `RolloutFnEvalOutput.data`。

#### 4.3.4 代码实践

> **实践目标**：手工构造两种输出，理解 `list[list[Sample]]` 的两层含义。

**操作步骤**：

1. 编写如下示例代码（非项目原有）：

```python
# 示例代码：构造 RolloutFnTrainOutput
from slime.utils.types import Sample
from slime.rollout.base_types import RolloutFnTrainOutput, RolloutFnEvalOutput, call_rollout_fn

# 模拟 2 个 prompt，每个采样 2 条
group0 = [Sample(index=0, reward=1.0, response="ans_a"),
          Sample(index=1, reward=0.0, response="ans_b")]
group1 = [Sample(index=2, reward=0.5, response="ans_c"),
          Sample(index=3, reward=0.5, response="ans_d")]
out = RolloutFnTrainOutput(samples=[group0, group1])
print("prompt 组数:", len(out.samples))   # 2
print("第一组采样数:", len(out.samples[0]))  # 2

# 演示 call_rollout_fn 的兼容包装：返回裸 list[list[Sample]] 也能被包好
def fake_fn(*a, evaluation, **k):
    return [[Sample(index=0, reward=1.0)]]   # 老式裸返回

wrapped = call_rollout_fn(fake_fn, evaluation=False)
print(type(wrapped).__name__)   # RolloutFnTrainOutput
```

2. 运行，确认 `call_rollout_fn` 把裸 list 自动包成了 `RolloutFnTrainOutput`。

**需要观察的现象**：外层 list 长度 = prompt 组数，内层 list 长度 = 每组采样数；`call_rollout_fn` 对老式返回做了兼容。

**预期结果**：打印 `2 / 2 / RolloutFnTrainOutput`。

**待本地验证**：是否能 `import slime` 取决于环境（见 u1-l3）；若无法运行，请对照 [base_types.py:L19-L26](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py#L19-L26) 推断 `call_rollout_fn` 的包装行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RolloutFnTrainOutput.samples` 是 `list[list[Sample]]` 而不是扁平的 `list[Sample]`？

> **参考答案**：因为 GRPO 等算法需要对「同一 prompt 的多条采样」做组内相对比较（如组内奖励均值/标准差做优势归一化）。两层结构天然保留「哪几条属于同一 prompt」的分组信息。下游 `_convert_samples_to_train_data` 在打平时会保留 `rollout_id` 来维持这种同源关系（见 4.1.3）。

**练习 2**：如果一个自定义 rollout 函数返回了一个裸的 `list[Sample]`，主循环还能用吗？

> **参考答案**：能。`call_rollout_fn`（[base_types.py:L22-L24](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py#L22-L24)）会检测返回值类型，若不是 `RolloutFnTrainOutput`/`RolloutFnEvalOutput`，则按 `evaluation` 标志自动包成对应类型。这是为兼容旧版保留的兜底。不过新代码建议显式返回正确的容器类型。

## 5. 综合实践

把本讲三块知识串起来：**跟踪一条 Sample 从诞生到进入训练张量的完整轨迹**。

任务：

1. **构造**：写一个函数 `make_one_sample()`，返回一个 `Sample`，它模拟一次「模型生成 + 工具调用 + 模型再生成」的两段式 agent 回合：
   - prompt 段（3 个 token）。
   - 第一段 response：模型生成 2 个 token，`trainable=True`，logp 自定。
   - 第二段 response：工具返回 1 个 token，`trainable=False`。
   - 第三段 response：模型再生成 2 个 token，`trainable=True`，logp 自定。
   - 最后 `reward=1.0`，并手动把 `status` 设成 `COMPLETED`。
2. **自检**：调用 `_validate_response_metadata_lengths()`，确认不抛异常；打印 `loss_mask`，确认形状是 `[1,1,0,1,1]`。
3. **解释去向**：写一段说明，逐字段指出它会被哪个阶段读取——
   - `tokens` → 训练打包（完整序列）。
   - `loss_mask` → loss 阶段（区分可训练 token）。
   - `rollout_log_probs` → loss 阶段的 off-policy 修正。
   - `reward` → 优势估计。
   - `status` → 训练张量的 `truncated` 字段 / 指标。
4. **状态机**：在脚本里再构造一个样本，调用 `_apply_meta_info` 传入 `{"finish_reason": {"type": "length"}}`，确认它变成 `TRUNCATED`。

**验收标准**：脚本能跑通、长度校验通过、字段去向说明正确。如果你无法运行（无环境），则改为「阅读 [types.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py) 与 [rollout.py 的 _convert_samples_to_train_data](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L712-L818) 源码并填写去向表」，这一阅读型实践同样达成目标。

## 6. 本讲小结

- `Sample` 是 slime 中**唯一**贯穿 rollout 与 training 的数据载体，它把「输入序列 + 生成轨迹 + 奖励 + 终止原因」打包成单一对象，并用严格的长度校验保证各字段对齐。
- 关键字段分三段生命：prompt 类字段在数据源/rollout 写入、生成类字段（`tokens`/`response_length`/`loss_mask`/`rollout_log_probs`）在 rollout 的 `append_response_tokens` 写入、它们在训练 loss / 打包阶段被读取；`reward` 由奖励模型写入、在优势估计读取。
- `loss_mask` 的 1/0 区分「模型生成的 token」与「工具/环境注入的 token」，这是多轮 agent 训练正确性的根基。
- `rollout_id` 在「一次执行拆多段」时保证兄弟段同源聚合，避免重复计数。
- `Sample.Status` 是五态状态机（`PENDING→COMPLETED/TRUNCATED/ABORTED/FAILED`），多由 `finish_reason.type` 驱动，支撑 partial rollout 续传与中断处理。
- rollout 函数的产出统一封装为 `RolloutFnTrainOutput`（训练，`list[list[Sample]]`）或 `RolloutFnEvalOutput`（评估，dict），由 `call_rollout_fn` 做兼容包装。

## 7. 下一步学习建议

下一讲 **u3-l2「默认 rollout 函数 generate_rollout 全流程」** 会把本讲的静态字段「动起来」：你会看到 `generate_rollout` 如何批量调用 `generate_and_rm`，如何用 `Status` 处理 abort 与 partial，以及如何把结果装进 `RolloutFnTrainOutput`。建议在进入 u3-l2 前，先回看本讲 4.1.3 的「字段流向训练」表，因为 u3-l2 的很多逻辑就是在「填充」这些字段。

此外推荐顺手阅读：
- [slime/rollout/base_types.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py)（本讲已读，再确认 `call_rollout_fn`）。
- [slime/rollout/data_source.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py)（u3-l3 会详讲，先了解 `Sample` 是从哪里被取出的）。
