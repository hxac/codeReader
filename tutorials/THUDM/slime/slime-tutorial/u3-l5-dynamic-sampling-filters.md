# 动态采样、过采样与过滤

## 1. 本讲目标

在 [u3-l2 默认 rollout 函数 generate_rollout 全流程](u3-l2-default-rollout-flow.md) 里，我们已经看到了「过采样补数 + 动态过滤」如何配合凑齐 `rollout_batch_size` 组样本，但当时只把它当成主循环里的一个黑盒。本讲把这个黑盒打开，专门讲清楚三件事：

1. **为什么要过滤**：DAPO 风格动态采样背后的 GRPO 学习信号原理。
2. **过滤如何与过采样协同**：`over_sampling_batch_size`、`remaining_batch_size`、`target_data_size` 三个量如何决定「丢一组就补采一组」。
3. **过滤器怎么写**：`DynamicFilterOutput` 契约、`should_drop` 裁决逻辑、内置的 `check_reward_nonzero_std`，以及如何写一个自定义动态过滤器并接入 `--dynamic-sampling-filter-path`。

学完后，你应当能够：判断什么样的样本组会被丢弃、解释 `keep_when_insufficient` 为何能避免无限补采、并独立实现一个自定义动态过滤器。

## 2. 前置知识

本讲默认你已经掌握以下内容（前序讲义已建立）：

- **Sample 数据结构（u3-l1）**：知道 `Sample` 的 `response_length`、`reward`、`loss_mask` 字段，以及 `effective_response_length` 属性（可训练 token 数）。
- **默认 rollout 主循环（u3-l2）**：知道 `generate_rollout_async` 用「同步外壳 + 异步内核 + 双层 while 循环 + `asyncio.wait(FIRST_COMPLETED)`」凑数，并听过「动态过滤」这个词。
- **奖励计算（u3-l4）**：知道 `sample.reward` 在奖励计算后被写入，`get_reward_value(args)` 会按 `--reward-key` 取标量或字典里的值。

此外需要一个 RL 直觉：**GRPO（Group Relative Policy Optimization）的优势是「组内相对」算出来的**。对同一个 prompt 的 `n` 条采样，先把它们的奖励做组内归一化：

\[
A_i = \frac{r_i - \bar{r}}{\sigma_r + \varepsilon}, \qquad \bar{r}=\frac{1}{n}\sum_{j} r_j,\quad \sigma_r=\sqrt{\frac{1}{n}\sum_{j}(r_j-\bar{r})^2}
\]

> 名词解释：**优势（advantage）** 告诉模型「这条采样比组内平均好多少/差多少」，是 RL 更新权重的方向信号。slime 里优势的具体实现见 [u6-l4 优势估计器](u3-l5-dynamic-sampling-filters.md)（后续讲义），本讲只需这个直觉。

关键观察：如果一组里 `n` 条采样的奖励**完全相同**（全对或全错），则 \(\sigma_r = 0\)，于是所有 \(A_i = 0\)。这组样本**不产生任何梯度**，喂给训练等于浪费算力。这正是动态过滤要解决的核心问题。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `slime/rollout/filter_hub/base_types.py` | 定义 `DynamicFilterOutput` 契约、`should_drop_dynamic_filter_output` 裁决逻辑、`call_dynamic_filter` 兼容包装、`MetricGatherer` 指标收集 |
| `slime/rollout/filter_hub/dynamic_sampling_filters.py` | 两个内置过滤器：`check_reward_nonzero_std` 与 `check_reward_nonzero_std_with_fallback` |
| `slime/rollout/sglang_rollout.py` | `generate_rollout_async` 主循环，是动态过滤的集成点 |
| `slime/utils/arguments.py` | `--over-sampling-batch-size` 与 `--dynamic-sampling-filter-path` 两个参数的定义与校验 |
| `docs/en/get_started/customization.md` | 第 4 节「Dynamic Sampling Filter」接口契约文档 |

整个 `filter_hub/` 目录只有三个文件，体量很小，适合一次性读透。

## 4. 核心概念与源码讲解

### 4.1 为什么需要动态采样：GRPO 与组内 reward 方差

#### 4.1.1 概念说明

**动态采样（dynamic sampling）** 是 DAPO 论文提出的一种训练数据筛选策略，核心思想是：**只在样本「有学习价值」时才保留它**。

对数学题这类「答案对/错」的 0/1 奖励任务，如果一个 prompt 太简单（模型每次都对）或太难（每次都错），那么这一组采样要么全 1、要么全 0。如前置知识所述，这样的组方差为 0，GRPO 算不出有效优势，等于白跑一趟推理。

slime 把这个策略做成了一个**可插拔的过滤器函数**：每当一组采样（同一个 prompt 的 `n_samples_per_prompt` 条回复）生成完，就调用一次过滤器，由它决定「留还是丢」。这就是 `--dynamic-sampling-filter-path`。

#### 4.1.2 核心流程

一条 prompt 组的命运分两步：

1. **生成**：rollout 引擎对同一条 prompt 采样 `n` 次，得到一组 `Sample`，逐条算奖励。
2. **裁决**：把这一组 `Sample` 交给动态过滤器，它返回「保留 / 丢弃」以及丢弃原因。

判据因任务而异，但 DAPO 默认的判据就是「组内奖励标准差是否大于 0」：

\[
\text{keep} = \left(\sigma_r > 10^{-6}\right)
\]

注意阈值用 `1e-6` 而非严格的 `0`，是为了规避浮点误差导致的「本应丢弃却保留」。

#### 4.1.3 源码精读

参数帮助文本明确点出了 DAPO 与「全对/全错」的动机：

[slime/utils/arguments.py:431-443](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L431-L443) —— `--dynamic-sampling-filter-path` 的 help 写道：「We will do dynamic filter for sampling as in DAPO. e.g. not all correct or all wrong samples.」（我们按 DAPO 的方式做动态过滤，例如丢弃全对或全错的样本）。帮助文本还直接给出两个内置过滤器作为示例。

这个参数的值是一个 **import 路径字符串**（如 `slime.rollout.filter_hub.dynamic_sampling_filters.check_reward_nonzero_std`），运行时由 `load_function` 解析成可调用对象——这是 slime 所有 `--xxx-path` 接口的统一约定（见 u6-l1）。

#### 4.1.4 代码实践

**实践目标**：用直觉验证「方差为 0 的组没有学习信号」。

**操作步骤**：纯纸笔计算，无需运行。

设某 prompt 组有 4 条采样，奖励分别为 `[1, 1, 1, 1]`（全对）。

1. 计算 \(\bar{r}\)。
2. 计算 \(\sigma_r\)。
3. 代入优势公式算每条 \(A_i\)。

**预期结果**：\(\bar{r}=1\)，\(\sigma_r=0\)，每条 \(A_i=0\)。全组零梯度——这正是 `check_reward_nonzero_std` 要丢弃的情况。

#### 4.1.5 小练习与答案

**练习 1**：奖励为 `[1, 0, 1, 0]` 的组会被 `check_reward_nonzero_std` 保留吗？

> **参考答案**：会。方差约为 0.547，远大于 `1e-6`，`keep=True`。这种「有对有错」的组正是 GRPO 最需要的对比信号。

**练习 2**：为什么阈值是 `1e-6` 而不是 `0`？

> **参考答案**：浮点运算（尤其 `torch.tensor.std()`）可能让本应为 0 的方差算成 `1e-8` 量级的极小正数。用 `1e-6` 作阈值能稳定地把这类数值当作 0 处理，避免「本该丢却留」。

---

### 4.2 过采样补数：over_sampling_batch_size 与 remaining_batch_size

#### 4.2.1 概念说明

动态过滤会丢样本，但 rollout 最终必须凑够 `rollout_batch_size` 组（这是训练侧供需公式的硬约束，见 u1-l4）。**丢了就得补**。这就引出两个量：

- **`over_sampling_batch_size`（过采样批量）**：每次「补采」从数据源取多少组 prompt。它决定了补采的**粒度**。
- **`remaining_batch_size`（剩余批量）**：`GenerateState` 维护的一个计数器，记录「已提交、尚未被丢弃」的组数，用来判断是否该再补一轮。

`target_data_size` 就是最终要保留的组数，等于 `rollout_batch_size`。

> 名词解释：**过采样（over-sampling）** = 故意一次多取一些 prompt 组，给过滤器留出「挑选」的余地；**补数** = 丢弃后凑不够时再取一批。

#### 4.2.2 核心流程

`generate_rollout_async` 的核心是一个双层 while 循环，伪代码如下：

```
target_data_size = rollout_batch_size          # 要保留的组数
data = []
while len(data) < target_data_size:            # 外层：还没凑够
    while remaining_batch_size < target_data_size:   # 内层：在途组数不足，补采
        samples = data_source(over_sampling_batch_size)   # 取一批 prompt 组
        submit_generate_tasks(samples)         # 异步提交，remaining_batch_size += 这批组数

    done = await asyncio.wait(FIRST_COMPLETED) # 等任意一组生成完成
    for group in done:
        output = call_dynamic_filter(filter, group)
        if should_drop(output, remaining_batch_size, target_data_size):
            remaining_batch_size -= 1          # 丢弃：在途计数 -1，继续等下一组
            continue
        if len(data) < target_data_size:
            data.append(group)                 # 保留：加入结果
```

**再采样的触发条件**就藏在两个 while 里：每当丢弃把 `remaining_batch_size` 压到 `target_data_size` 以下，下一次回到内层 while 时就会再取 `over_sampling_batch_size` 组。这就是「丢一组 → 补一批」的自动化。

#### 4.2.3 源码精读

[slime/rollout/sglang_rollout.py:398-409](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L398-L409) —— `target_data_size = args.rollout_batch_size`（L399）；外层 while 控制总凑数，内层 while 在 `remaining_batch_size < target_data_size` 时调用 `data_source(args.over_sampling_batch_size)` 取数据并 `submit_generate_tasks` 提交。

`remaining_batch_size` 的维护在 `GenerateState`（一个单例）里：

[slime/rollout/sglang_rollout.py:136-149](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L136-L149) —— `submit_generate_tasks` 每提交一批就 `self.remaining_batch_size += len(samples)`。注意 `data_source` 返回的是 `list[list[Sample]]`，外层是 prompt 组，所以 `len(samples)` 是组数。

参数的默认值与校验在 arguments 里：

[slime/utils/arguments.py:418-429](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L418-L429) —— help 说明：若 `--over-sampling-batch-size` 为 `None`，则默认等于 `rollout_batch_size`（即「一次取够、不分批补采」）。

[slime/utils/arguments.py:1921-1927](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1921-L1927) —— 校验逻辑：默认值回填后，强制 `over_sampling_batch_size >= rollout_batch_size`。这是一个关键约束——**过采样批量不能小于目标批量**，否则补采永远赶不上丢弃，最后 L451 的 `assert len(data) == rollout_batch_size` 会失败。

循环结束后的收口：

[slime/rollout/sglang_rollout.py:449-451](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L449-L451) —— 凑够后 `abort` 掉仍在途的请求，并断言 `len(data) == rollout_batch_size`。如果过滤器太严格、又没开 fallback，就可能在有限的推理预算内永远凑不够，最终在这里断言失败。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `remaining_batch_size` 的变化，理解补采何时触发。

**操作步骤**：设 `rollout_batch_size = 8`（即 `target_data_size = 8`）、`over_sampling_batch_size = 8`、`n_samples_per_prompt = 4`，并假设每批 8 组里有 3 组因零方差被丢弃。逐步推演：

1. 初始 `remaining_batch_size = 0`。
2. 内层 while：`0 < 8` 成立，取 8 组，`remaining_batch_size = 8`，提交。
3. 等待完成。假设 8 组陆续返回，其中 3 组被丢弃（`remaining_batch_size` 依次 `-= 1` → 7,6,5），5 组保留加入 `data`。
4. 此时 `len(data) = 5 < 8`，外层 while 继续；内层 while 检查 `remaining_batch_size = 5 < 8` 成立 → 再取 8 组补采，`remaining_batch_size = 13`。
5. 继续直到 `len(data) == 8`。

**预期结果**：你能清楚看到「丢弃 3 组」直接触发了第二轮补采。**待本地验证**：真实异步执行下，丢弃与补采会交错发生，但触发逻辑不变。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `over_sampling_batch_size` 设得远大于 `rollout_batch_size`（比如 10 倍），主循环行为会如何变化？

> **参考答案**：每次补采会一次取很多组，给过滤器更大的挑选空间，凑够目标所需的「补采轮数」更少；但单轮推理峰值更高、缓冲压力更大，且会丢弃更多样本（浪费算力）。这是个吞吐与质量的权衡。

**练习 2**：为什么校验要求 `over_sampling_batch_size >= rollout_batch_size`？

> **参考答案**：若过采样批量小于目标批量，第一轮就取不够目标数，且每丢一组在途计数就 -1，永远凑不到 `target_data_size`，循环无法收敛，最终 `assert` 失败。

---

### 4.3 DynamicFilterOutput 契约与 should_drop 裁决逻辑

#### 4.3.1 概念说明

动态过滤器是一个函数，签名固定为：

```python
def filter_function(args, samples: list[Sample], **kwargs) -> DynamicFilterOutput
```

它接收一组样本（同一个 prompt 的所有采样），返回一个 `DynamicFilterOutput`。这个 dataclass 有三个字段：

- **`keep: bool`**：是否保留这一组。这是唯一影响去留的字段。
- **`reason: str | None`**：丢弃原因，仅用于日志/指标，不影响逻辑（默认 `None`）。
- **`keep_when_insufficient: bool`**：「候选不足时是否破例保留」。默认 `False`。设为 `True` 时，当在途样本已经少到「再丢就要触发新一轮补采」时，即使 `keep=False` 也保留这组，避免无限补采。

第三个字段是本讲的精髓，下面单独讲。

> 小贴士：`docs/en/get_started/customization.md` 第 4 节列出的 `DynamicFilterOutput` 只写了 `keep` 和 `reason` 两个字段，**比源码少一个 `keep_when_insufficient`**。以 `base_types.py` 源码为准——文档略滞后。

#### 4.3.2 核心流程

「一组样本到底丢不丢」由 `should_drop_dynamic_filter_output` 统一裁决，决策树如下：

```
若 keep == True                         → 不丢（保留）
否则若 keep_when_insufficient 且 remaining_batch_size <= target_data_size
                                        → 不丢（破例保留，避免再补采一轮）
否则                                     → 丢
```

第二条分支是 `keep_when_insufficient` 的全部意义：它把「宁可保留一个不理想的组」当作「避免触发昂贵的新一轮过采样」的逃生阀。

此外还有一个兼容层 `call_dynamic_filter`：如果过滤器返回的是旧式的 `bool`（而不是 `DynamicFilterOutput`），它会自动包装成 `DynamicFilterOutput(keep=bool)`。这意味着**老插件无需改造**也能继续用。

#### 4.3.3 源码精读

`DynamicFilterOutput` 的定义与字段注释：

[slime/rollout/filter_hub/base_types.py:5-11](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/filter_hub/base_types.py#L5-L11) —— 三个字段。注释把 `keep_when_insufficient` 的用途说得很清楚：「Keep a rejected group when dropping it would leave too few candidates to fill the rollout batch. This avoids launching another oversampling round.」

裁决函数：

[slime/rollout/filter_hub/base_types.py:14-24](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/filter_hub/base_types.py#L14-L24) —— 注意第二个判断用的是 `<=`（小于等于），不是 `<`。当 `remaining_batch_size` 刚好等于 `target_data_size` 时，破例保留就生效，把丢弃挡在「触发补采」的门槛之外。

兼容包装：

[slime/rollout/filter_hub/base_types.py:27-37](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/filter_hub/base_types.py#L27-L37) —— `fn is None` 时直接返回 `keep=True`（即不配置过滤器 = 不过滤）；返回值不是 `DynamicFilterOutput` 时按 bool 包装。

主循环里的调用：

[slime/rollout/sglang_rollout.py:426-434](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L426-L434) —— 先 `call_dynamic_filter`，再 `should_drop_dynamic_filter_output` 判定；判定为丢则记指标、`remaining_batch_size -= 1`、`continue`。

#### 4.3.4 代码实践

**实践目标**：实现本讲要求的自定义动态过滤器——「一组样本的平均可训练响应长度低于阈值时丢弃」，并验证 `should_drop` 在边界条件下的行为。这是一个**可在本地完整运行**的单元级实践（仅需 slime 可 import + torch）。

**操作步骤**：

1. 新建一个 Python 文件（例如 `my_filter.py`），写入以下示例代码：

```python
# 示例代码：自定义动态过滤器
from slime.rollout.filter_hub.base_types import DynamicFilterOutput


def check_min_response_length(args, samples, *, min_length=16, **kwargs):
    """丢弃平均可训练响应长度过低的样本组。

    与 check_reward_nonzero_std 同签名：(args, samples, **kwargs) -> DynamicFilterOutput。
    使用 effective_response_length（基于 loss_mask，仅统计模型可训练 token），
    而不是包含工具/环境注入 token 的 response_length。
    """
    avg_len = sum(s.effective_response_length for s in samples) / len(samples)
    keep = avg_len >= min_length
    return DynamicFilterOutput(
        keep=keep,
        reason=None if keep else f"too_short_{int(avg_len)}",
    )
```

2. 在同一文件或 REPL 里构造假样本并验证：

```python
# 示例代码：验证过滤器与 should_drop 的边界行为
from slime.utils.types import Sample
from slime.rollout.filter_hub.base_types import should_drop_dynamic_filter_output

# 构造一组响应很短的样本（loss_mask 为 None 时，effective_response_length == response_length）
short_group = [Sample(response_length=5) for _ in range(4)]
out = check_min_response_length(None, short_group, min_length=16)
print("keep:", out.keep, "reason:", out.reason)   # 预期 keep=False, reason=too_short_5

# 候选充足（remaining > target）→ 正常丢弃
print("drop when sufficient:",
      should_drop_dynamic_filter_output(out, remaining_batch_size=64, target_data_size=32))  # 预期 True

# 手动开启 keep_when_insufficient，且候选紧张（remaining <= target）→ 破例保留
out.keep_when_insufficient = True
print("keep when insufficient:",
      should_drop_dynamic_filter_output(out, remaining_batch_size=20, target_data_size=32))  # 预期 False
```

**需要观察的现象**：
- 第一组样本平均长度 5 < 16，`keep=False`，`reason="too_short_5"`。
- 候选充足时 `should_drop` 返回 `True`（丢弃）。
- 开启 `keep_when_insufficient` 且 `remaining_batch_size(20) <= target_data_size(32)` 时，`should_drop` 返回 `False`（破例保留）。

**预期结果**：三处输出分别约为 `False / too_short_5`、`True`、`False`。这验证了 `keep` 控制去留、`keep_when_insufficient` 控制边界逃生。

#### 4.3.5 小练习与答案

**练习 1**：如果你的过滤器返回的是一个普通 `bool` 而不是 `DynamicFilterOutput`，会怎样？

> **参考答案**：`call_dynamic_filter` 会把它包装成 `DynamicFilterOutput(keep=bool)`。插件能继续工作，但失去了 `reason`（无法记录丢弃原因）和 `keep_when_insufficient`（无法避免无限补采）的能力。新插件建议直接返回 `DynamicFilterOutput`。

**练习 2**：为什么 `should_drop` 第二个判断用 `<=` 而不是 `<`？

> **参考答案**：内层 while 的补采触发条件是 `remaining_batch_size < target_data_size`（严格小于）。裁决用 `<=` 是把「等于目标」也视为「候选紧张」，从而在恰好踩到补采门槛前就破例保留，二者边界对齐，避免「差一格」就触发一轮昂贵补采。

---

### 4.4 内置过滤器 check_reward_nonzero_std 与 with_fallback

#### 4.4.1 概念说明

`filter_hub/` 内置了两个现成的过滤器，差别只在 `keep_when_insufficient`：

- **`check_reward_nonzero_std`**：严格的 DAPO 过滤。组内奖励标准差为 0 就丢，`keep_when_insufficient=False`（默认）。会**主动触发**补采轮次。
- **`check_reward_nonzero_std_with_fallback`**：在上一者的基础上把 `keep_when_insufficient` 设为 `True`。**优先**保留非零方差组，但在候选不足时**接受**零方差组，避免无限补采。

选择哪一个，本质是「数据质量」与「补采算力开销」的权衡。

#### 4.4.2 核心流程

两个过滤器的判据完全相同（都调 `check_reward_nonzero_std` 算核心结果），只是 `with_fallback` 多翻一个开关：

| 场景 | `check_reward_nonzero_std` | `_with_fallback` |
| --- | --- | --- |
| 组内有方差（有对有错） | 保留 | 保留 |
| 组内零方差 + 候选充足 | 丢弃，触发补采 | 丢弃，触发补采 |
| 组内零方差 + 候选紧张 | 丢弃（可能反复补采） | **破例保留**，停止补采 |

#### 4.4.3 源码精读

核心判据：

[slime/rollout/filter_hub/dynamic_sampling_filters.py:9-15](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/filter_hub/dynamic_sampling_filters.py#L9-L15) —— 用 `sample.get_reward_value(args)` 取每条奖励（兼容 `--reward-key` 的字典奖励），转 `float64` 张量算 `std()`，阈值 `1e-6`。丢弃原因形如 `zero_std_1.0`（全对）或 `zero_std_0.0`（全错），把奖励值 `round(_, 1)` 拼进去方便日志区分难度。

fallback 版本：

[slime/rollout/filter_hub/dynamic_sampling_filters.py:18-23](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/filter_hub/dynamic_sampling_filters.py#L18-L23) —— 先复用 `check_reward_nonzero_std` 算出 `output`，再把 `output.keep_when_insufficient = True`。docstring 直白：「Prefer non-zero-std groups without triggering another sampling round.」

丢弃原因会进指标：

[slime/rollout/sglang_rollout.py:432](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L432) —— 丢弃时调 `metric_gatherer.on_dynamic_filter_drop(reason=...)`。

[slime/rollout/filter_hub/base_types.py:40-53](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/filter_hub/base_types.py#L40-L53) —— `MetricGatherer` 用 `defaultdict` 按 reason 计数，最终 `collect()` 输出形如 `{"rollout/dynamic_filter/drop_zero_std_1.0": 17}` 的指标，随 `RolloutFnTrainOutput.metrics` 返回，可在训练日志/可观测面板里看到「被丢的全对组数」「被丢的全错组数」。

#### 4.4.4 代码实践

**实践目标**：对比两个内置过滤器在「候选紧张」时的行为差异。

**操作步骤**：在 REPL（slime 可 import）执行：

```python
# 示例代码：对比 plain 与 with_fallback
from slime.utils.types import Sample
from slime.rollout.filter_hub.dynamic_sampling_filters import (
    check_reward_nonzero_std,
    check_reward_nonzero_std_with_fallback,
)
from slime.rollout.filter_hub.base_types import should_drop_dynamic_filter_output

class Args:  # 极简 args，满足 get_reward_value(args) 不需要 reward_key
    reward_key = None

# 全错组：方差为 0
group = [Sample(reward=0.0) for _ in range(4)]
args = Args()

plain = check_reward_nonzero_std(args, group)             # keep_when_insufficient=False
fb = check_reward_nonzero_std_with_fallback(args, group)  # keep_when_insufficient=True

# 候选紧张：remaining(10) <= target(32)
print("plain drop:", should_drop_dynamic_filter_output(plain, remaining_batch_size=10, target_data_size=32))   # True（丢）
print("fallback drop:", should_drop_dynamic_filter_output(fb, remaining_batch_size=10, target_data_size=32))   # False（破例留）
```

**预期结果**：`plain drop` 为 `True`，`fallback drop` 为 `False`。同样的零方差组，在候选紧张时 plain 会丢弃（可能触发补采），fallback 会保留。

> 说明：`get_reward_value(args)` 在 `args.reward_key` 为假值时直接返回 `self.reward`（见 [slime/utils/types.py:246-247](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L246-L247)），所以用一个只含 `reward_key=None` 的假 `Args` 即可。

#### 4.4.5 小练习与答案

**练习 1**：如果你用 `check_reward_nonzero_std`（plain）却忘了把 `over_sampling_batch_size` 设得比 `rollout_batch_size` 大，会发生什么？

> **参考答案**：因为默认 `over_sampling_batch_size == rollout_batch_size`，第一轮就只取目标数那么多的组。若其中多组零方差被丢，候选立即紧张，却又没有过采样余量补采，很可能凑不够 `rollout_batch_size`，最终在 L451 的 `assert` 失败。要么调大过采样批量，要么改用 `_with_fallback`。

**练习 2**：丢弃原因 `zero_std_1.0` 和 `zero_std_0.0` 分别对应什么情况？为什么把它们分开统计有用？

> **参考答案**：`zero_std_1.0` = 全对（题目太简单），`zero_std_0.0` = 全错（题目太难）。分开统计能让你在指标面板上看出「丢的主要是简单题还是难题」，进而调整数据集难度分布或课程学习策略。

---

## 5. 综合实践

把 4.3.4 写的长度过滤器真正接入 slime 训练流程，并观察丢弃指标。这是一个**端到端**的实践，需要能跑起 slime 的 GPU 环境；若暂无环境，可只完成第 1、2 步并在本地用假数据自测（如 4.3.4），其余标注「待本地验证」。

**实践目标**：写一个可被 `--dynamic-sampling-filter-path` 加载的过滤器文件，启动一次小规模 rollout，并通过 `MetricGatherer` 指标确认过滤器真的在工作。

**操作步骤**：

1. 把 4.3.4 的 `check_min_response_length` 保存为一个**可被 import 的模块**，例如放到 `slime_plugins/filters/my_filters.py`，函数必须是顶层定义（`load_function` 靠 import 路径定位）。注意：默认参数 `min_length=16` 在命令行无法覆盖，若要可配置，可从 `args` 上读一个自定义字段（需要自行加参数），或直接在函数里写死阈值。

2. 复制一份 `scripts/run-qwen3-4B.sh`（见 u1-l4），在参数数组里加入：

   ```bash
   --over-sampling-batch-size 64 \      # 必须大于 rollout-batch-size，给挑选留余地
   --dynamic-sampling-filter-path slime_plugins.filters.my_filters.check_min_response_length
   ```

3. 用小模型（如 Qwen3-0.6B）和小 `rollout-batch-size`（如 8）跑一次，降低门槛。

4. 训练启动后，观察日志/指标里是否出现形如 `rollout/dynamic_filter/drop_too_short_5` 的计数键。

**需要观察的现象**：
- rollout 阶段日志不再只产出 `Rollout generation` 进度条，还会有丢弃计数。
- 指标里出现 `rollout/dynamic_filter/drop_too_short_*`，数字 = 被丢的组数。
- 由于开启了 fallback 的等价逻辑（本过滤器 `keep_when_insufficient` 默认 `False`），若阈值设太激进，可能出现凑不够 `rollout_batch_size` 而 assert 失败——这时把 `keep_when_insufficient` 在返回前设为 `True`，或调大 `over-sampling-batch-size`。

**预期结果**：过滤器被正确加载并生效，平均响应长度过短的组被丢弃并计入指标。**待本地验证**：具体丢弃比例取决于模型与数据集。

**思考延伸**：把本讲的长度过滤器与 4.4 的奖励方差过滤器「合二为一」（既要求有奖励方差、又要求平均长度达标），你会怎么组合？提示：在同一个函数里算两个判据，用 `and` 合成 `keep`，并把首个未通过的条件写进 `reason`。

## 6. 本讲小结

- **动态采样过滤的动机**：GRPO 的优势是组内相对算出的，组内奖励方差为 0（全对/全错）时无梯度信号，DAPO 风格过滤把这些组丢掉。
- **三个量协作**：`target_data_size`（要保留的组数）= `rollout_batch_size`；`over_sampling_batch_size`（补采粒度，须 ≥ 目标）控制每次取多少；`remaining_batch_size`（已提交未丢的组数）低于目标时触发新一轮补采。
- **`DynamicFilterOutput` 三字段**：`keep` 决定去留，`reason` 只用于日志/指标，`keep_when_insufficient` 在候选紧张时破例保留以避免无限补采。
- **`should_drop` 裁决**：`keep=True` 不丢；否则若 `keep_when_insufficient` 且 `remaining_batch_size <= target_data_size` 也不丢；其余丢弃。边界用 `<=` 与补采门槛对齐。
- **两个内置过滤器**：`check_reward_nonzero_std`（严格，会触发补采）与 `_with_fallback`（候选紧张时接受零方差组）。丢弃原因经 `MetricGatherer` 汇成 `rollout/dynamic_filter/drop_*` 指标。
- **可插拔**：过滤器是 `--dynamic-sampling-filter-path` 指向的 import 路径，签名 `(args, samples, **kwargs) -> DynamicFilterOutput`，旧式 bool 返回值也能由 `call_dynamic_filter` 自动兼容。

## 7. 下一步学习建议

- 想看动态过滤「之外」的样本筛选机制，对比学习 **buffer filter**（`--buffer-filter-path`，训练前对缓冲区过滤，见 u3-l3 的 `pop_first`）与 **sample filter / all-samples process**（`sglang_rollout.py` 末尾 L459-466），理解它们各自作用在数据流的哪一段。
- 想理解「reward 怎么变成 advantage」的完整数学，进入 [u6-l4 优势估计器与 RL 算法选择](u6-l4-advantage-estimators.md)（GRPO/GAE/RLOO 等的 `compute_advantages_and_returns`）。
- 想系统掌握所有 `--xxx-path` 可注入接口的层级关系，进入 u6-l1「定制化接口总览」，把动态过滤放进 21+ 个 hook 的全景里。
- 继续阅读 `slime/rollout/filter_hub/` 全部三个文件（本讲已覆盖），并对照 `docs/en/get_started/customization.md` 第 4、5 节（dynamic filter 与 buffer filter）巩固接口契约。
