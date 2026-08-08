# 自定义奖励与样本→训练数据转换

## 1. 本讲目标

本讲承接 [u6-l1 定制化接口总览](u6-l1-customization-overview.md)，把视线聚焦到 RL 闭环里「打分」与「打包」这两段最贴近业务的衔接处。读者学完后应该能够：

- 写出 slime 两种奖励函数（单样本 `custom_rm` 与 batch `group_rm`）的正确签名，并知道框架在哪一行、按什么优先级调用它们。
- 看懂默认的 `convert_samples_to_train_data` 把 `Sample` 列表翻译成训练张量字典的完整逻辑，并能说出每个返回字段会被训练端的哪一步消费。
- 理解 `reward_post_process` 这个钩子在「奖励写回」与「优势估计」之间的精确介入点，以及默认 GRPO 组归一化的数学含义。
- 知道这三个接口与契约测试的对应关系，能用纯 CPU 自检自己的实现。

## 2. 前置知识

在进入源码前，先澄清三个在本讲反复出现的概念：

- **reward（奖励）与 advantage（优势）**：reward 是对一条回答好坏的标量打分，由奖励函数算出；advantage 是 reward 经过归一化/基线扣除后、直接进入策略梯度的信号。两者的分界线正是 `reward_post_process`：它的输出 `rewards` 才是喂给优势估计的值。
- **raw_reward 与 normalized reward**：slime 会同时保留「原始分」和「归一化分」。前者用于日志和指标统计，后者用于训练。`convert_samples_to_train_data` 返回的字典里 `raw_reward` 与 `rewards` 就是这两份。
- **单样本模式 vs 整组模式（`group_rm`）**：GRPO 系算法的优势由「同一 prompt 的 n 条采样的相对位置」决定。如果奖励计算本身需要看到整组（例如要做组内排序、或调用一个按批返回的奖励服务），就必须把打分推迟到整组生成完之后——这就是 `--group-rm` 的 batch 模式。本讲的两个最小化模块都会绕着「单 vs 批」这一对来展开。

如果你对 `Sample` 数据结构、`rm_hub` 的内置奖励分发、或 `load_function` 的路径解析还不熟，建议先读 [u3-l1 Sample 数据结构](u3-l1-sample-data-structure.md)、[u3-l4 奖励模型 rm_hub](u3-l4-reward-model-hub.md) 与 [u6-l1 定制化接口总览](u6-l1-customization-overview.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `slime/rollout/rm_hub/__init__.py` | 奖励分发中枢：`async_rm`（单样本）、`batched_async_rm`（整组）两条入口，以及三级优先级与内置 `rm_type` 分发。 |
| `slime/rollout/sglang_rollout.py` | 默认 rollout 函数，展示 `group_rm` 开关如何决定「逐样本算分」还是「整组算分」的调用时机。 |
| `slime/ray/rollout.py` | `RolloutManager` 所在地：`_post_process_rewards`（奖励后处理）与 `_convert_samples_to_train_data`（样本→训练数据）的默认实现与自定义 hook 挂载点。 |
| `slime/utils/types.py` | `Sample.get_reward_value`，定义如何从 `reward` 字段取数（支持 `reward_key` 选多维奖励中的某一维）。 |
| `tests/plugin_contracts/test_plugin_path_loading_contracts.py` | `custom_rm` 的单样本/批量契约参考实现，是写自定义奖励的最佳模板。 |
| `tests/plugin_contracts/test_plugin_runtime_hook_contracts.py` | `reward_post_process` 与 `convert_samples_to_train_data` 的契约参考实现。 |
| `docs/en/get_started/customization.md` | 全部定制化接口的官方说明，含 `convert_samples_to_train_data` 返回字典的标准字段表。 |

## 4. 核心概念与源码讲解

### 4.1 custom_rm 签名：单样本与 batch 两种奖励函数

#### 4.1.1 概念说明

奖励函数回答的问题是「这条回答该打多少分」。slime 不要求你把奖励逻辑写进框架，而是允许你用一个 import 路径（`--custom-rm-path`）注入自己的函数。它有两种互斥签名：

- **单样本模式**：函数一次处理一条 `Sample`，返回一个 `float`。框架会对一组里的 n 条采样各自调用一次（内部并发）。
- **batch 模式**（`--group-rm`）：函数一次拿到整组 `list[Sample]`，返回等长的 `list[float]`。适合奖励计算必须看全组的场景（如组内排序、按批返回的远程奖励服务）。

这两种模式不是「你二选一写」，而是「同一个 `--custom-rm-path`，框架根据是否开启 `--group-rm` 来决定怎么调你」。这与 [u3-l4](u3-l4-reward-model-hub.md) 讲的内置 `rm_type` 分发是同一套机制——本讲聚焦在「自定义」这条路径上。

#### 4.1.2 核心流程

奖励的分发由两个函数承担，一个面向单样本、一个面向整组：

```text
单样本路径 async_rm(args, sample):
    优先级1  sample.custom_rm_path  (逐样本，来自 eval 数据集配置)
    优先级2  args.custom_rm_path    (命令行 --custom-rm-path)
    优先级3  按 rm_type 内置分发    (math/deepscaler/f1/...)

整组路径 batched_async_rm(args, samples):
    若 args.custom_rm_path is not None:
        → 直接整组调用你的函数 (要求 batch 实现)
    否则:
        → 退化为对每个样本各跑一次 async_rm (gather 并发)
```

两个关键点：第一，自定义路径在整组模式下**不回退**——一旦你设了 `--custom-rm-path` 又开了 `--group-rm`，你的函数就必须实现成 batch 版本，框架不会再把你拆成单样本逐条调。第二，单样本路径的 `custom_rm_path` 优先级最高，是「逐样本覆盖」能力的基础（不同样本可用不同奖励），而整组路径没有逐样本优先级，只能全局设。

调用时机由 rollout 流程控制：`generate_and_rm` 在每条样本生成完、且 `group_rm=False` 时逐样本算分；`generate_and_rm_group` 在整组生成完、且 `group_rm=True` 时整组算分。评估阶段（`eval_rollout`）禁止整组模式。

#### 4.1.3 源码精读

先看单样本分发 `async_rm`。前两个 `if` 就是三级优先级里的前两级——逐样本 `custom_rm_path` 最优先，其次全局 `args.custom_rm_path`：

[slime/rollout/rm_hub/__init__.py:55-64](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L55-L64) 展示了优先级最高的两档：当样本自带 `custom_rm_path` 或全局设了 `--custom-rm-path` 时，用 `load_function` 解析路径并 `await` 调用，**直接返回单个 float**，完全绕过内置 `rm_type`。

若没有自定义路径，才走到按 `rm_type` 内置分发那一大串 `if/elif`（与 [u3-l4](u3-l4-reward-model-hub.md) 一致，此处不重复）。

再看整组分发的 `batched_async_rm`：

[slime/rollout/rm_hub/__init__.py:99-110](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L99-L110) 是本模块的核心：设了 `--custom-rm-path` 时，它把整组 `samples` **原样**传给你的函数，并要求你返回 `list`——这正是 batch 签名的来源；没设时则用 `asyncio.gather` 把每个样本各跑一次 `async_rm`，退化为并发单样本。注意类型注解 `list[Sample] -> list[int | float]`。

那么这两个函数在 rollout 主流程里到底何时被调？看默认 rollout 里的 `generate_and_rm`：

[slime/rollout/sglang_rollout.py:262-287](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L262-L287) 说明了单样本模式的调用：开 `group_rm` 时第 265 行直接 `return` 跳过算分（留给整组阶段）；否则，对 fan-out 成 `list` 的情况用 `batched_async_rm` 批量算、对单个样本用 `async_rm` 逐条算，并把结果写回 `sample.reward`。

整组算分发生在上一层 `generate_and_rm_group`：

[slime/rollout/sglang_rollout.py:327-332](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L327-L332) 在 `asyncio.gather` 收齐整组之后，仅当 `group_rm=True` 且未 abort 时，用 `batched_async_rm(args, group)` 整组算分并写回。这就是「整组算分必须推迟到 group 生成完」的落点。

而评估阶段对整组模式的禁令很直接：

[slime/rollout/sglang_rollout.py:474-475](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L474-L475) 用 `assert not args.group_rm` 表明：评估时奖励逐样本算，不允许整组模式。

最后看开关本身定义在哪里：

[slime/utils/arguments.py:1340](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1340) 注册了 `--group-rm` 这个 `store_true` 参数，默认 `False`。

写自定义奖励时，最省事的参考模板是契约测试里的两个 `reference` 函数：

[tests/plugin_contracts/test_plugin_path_loading_contracts.py:143-148](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_path_loading_contracts.py#L143-L148) 给出了单样本（`reference_single_rm`，收一个 `sample` 返回 `float`）与批量（`reference_batched_rm`，收 `list[Sample]` 返回 `list[float]`）的标准写法，二者签名都带 `**kwargs` 以兼容框架额外透传的参数。

#### 4.1.4 代码实践

**实践目标**：写一个 batch 版本的自定义奖励函数，理解它接收到的 `samples` 与单样本模式的区别。

**操作步骤**（纯 CPU 即可，无需 GPU/推理引擎）：

1. 新建文件 `my_proj/my_rewards.py`，写入下面的 batch 奖励函数（示例代码，非项目原有代码）：

   ```python
   # my_proj/my_rewards.py  —— 示例代码
   from slime.utils.types import Sample


   async def length_normalized_rm(args, samples: list[Sample], **kwargs) -> list[float]:
       """batch 模式奖励：组内按「平均每 token 的奖励」排序后再归一。

       接收的 samples 是同一 prompt 的 n 条采样（一组），
       这是与单样本模式最大的区别：你能同时看到整组。
       """
       # 假设 label 已编码了正确与否，这里仅演示组内相对打分
       raw = []
       for s in samples:
           base = 1.0 if (s.label is not None and str(s.label) in (s.response or "")) else 0.0
           length = max(s.response_length, 1)
           raw.append(base / length)  # 越短且正确得分越高
       return raw
   ```

2. 模拟框架的调用方式，验证签名与返回：

   ```bash
   python -c "
   import asyncio
   from slime.utils.types import Sample
   from my_proj.my_rewards import length_normalized_rm

   async def main():
       group = [
           Sample(index=0, response='42', label='42', response_length=2),
           Sample(index=1, response='The answer is 42.', label='42', response_length=18),
       ]
       rewards = await length_normalized_rm(None, group)
       print(rewards)              # 期望: 两条都含 42，短的得分更高
       assert isinstance(rewards, list) and len(rewards) == 2
   asyncio.run(main())
   "
   ```

3. 想象在真实训练里启用它：`--custom-rm-path my_proj.my_rewards.length_normalized_rm --group-rm`。注意因为开了 `--group-rm`，框架会走 `batched_async_rm` 的「整组直调」分支，把一组样本原样传给你的函数。

**需要观察的现象**：`samples` 的长度等于 `n_samples_per_prompt`（一组内的采样数），而不是 `rollout_batch_size × n_samples_per_prompt`（全批）。即 batch 模式是「逐组」调用，不是「一次性把全批给你」。

**预期结果**：上面脚本打印出长度为 2 的 `list[float]`，且第一条（短回答）得分高于第二条。这与单样本模式（每次只收到一个 `Sample`、看不到同组兄弟）形成鲜明对比。

**待本地验证**：若你的环境里 `slime` 尚未安装，可临时把示例里的 `Sample` 换成一个最小 dataclass（含 `response/response_length/label` 字段）来跑通逻辑，结论一致。

#### 4.1.5 小练习与答案

**练习 1**：如果我设了 `--custom-rm-path foo.bar.rm` 但**没有**开 `--group-rm`，框架会用 `foo.bar.rm` 的哪个签名来调它？

**答案**：单样本签名 `async def rm(args, sample, **kwargs) -> float`。因为没开 `--group-rm`，rollout 走 `generate_and_rm` 的逐样本分支，最终经 `batched_async_rm` 里的 `else` 分支用 `asyncio.gather` 对每条样本各跑一次 `async_rm`，而 `async_rm` 会命中 `args.custom_rm_path`，按单样本方式调用。

**练习 2**：为什么评估阶段（`eval_rollout`）禁止 `--group-rm`？

**答案**：评估是为了得到可复现、逐样本可解释的指标，且评估时通常一组只有一条采样（不需要组内相对打分）；整组模式依赖「同 prompt 多采样」的语义，与评估口径冲突，所以 `eval_rollout` 开头 `assert not args.group_rm`。

---

### 4.2 convert_samples_to_train_data：样本如何变成训练张量

#### 4.2.1 概念说明

奖励写回 `sample.reward` 之后，`Sample` 仍是一个面向「采样/奖励」语义的对象；而 Megatron 训练端需要的是扁平的、按张量组织的字典（tokens、loss_mask、rewards……）。`convert_samples_to_train_data` 就是这两者之间的「翻译层」。

它做三件事：①调用奖励后处理算出 `rewards`/`raw_reward`；②把每个 `Sample` 的字段按训练端期望的 key 拼成字典；③补齐若干「整 rollout 级」的预计算量（如 `rollout_mask_sums`）和可选字段（off-policy 修正、MoE 路由、多模态等）。你可以用 `--custom-convert-samples-to-train-data-path` 整体替换这个翻译过程。

#### 4.2.2 核心流程

```text
_convert_samples_to_train_data(samples):
    若设了 --custom-convert-samples-to-train-data-path:
        → 直接委托给自定义函数，返回 dict，结束
    # 默认实现：
    1. raw_rewards, rewards = _post_process_rewards(samples)   # 见 4.3
    2. 构造必备字段：
         tokens / response_lengths / rewards / raw_reward /
         truncated / sample_indices / rollout_ids
    3. loss_masks：逐样本对齐 response_length，
         remove_sample=True 的样本全置 0
    4. rollout_mask_sums：按 rollout_id 聚合每条 rollout 的可训练 token 数
         （供 pg_loss reducer 当分母，保证跨微批仍正确）
    5. 追加可选字段（按需）：
         rollout_log_probs / rollout_routed_experts /
         metadata / multimodal_train_inputs / teacher_log_probs / ...
    6. return train_data (dict)
```

关键不变量：`loss_mask` 长度必须等于 `response_length`；`rewards` 与 `raw_reward` 长度必须等于 `len(samples)`。`rollout_mask_sums` 是 slime 在「first-fit 装箱可能把一个 rollout 的样本拆进不同微批」这一现实下，为了保证 per-rollout 归约分母正确而做的预计算。

#### 4.2.3 源码精读

先看自定义 hook 的挂载点——在 `RolloutManager.init` 里：

[slime/ray/rollout.py:449-456](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L449-L456) 在初始化时按 `--custom-reward-post-process-path` 与 `--custom-convert-samples-to-train-data-path` 用 `load_function` 解析出两个可调用对象（或 `None`）。这是 4.2 与 4.3 两个 hook 的统一加载点。

然后看默认实现的入口与「自定义优先」短路：

[slime/ray/rollout.py:712-722](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout.py#L712-L722) 中，第 716-717 行表明：一旦设了自定义转换函数，框架直接把 `(args, samples)` 交给它并返回，默认翻译逻辑全部跳过——这意味着自定义实现要**自己负责**调用奖励后处理（否则 rewards/raw_reward 不会有）。否则进入默认流程，先 `_post_process_rewards` 拿到两份奖励，并断言长度对齐。

必备字段的组装：

[slime/ray/rollout.py:734-744](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout.py#L734-L744) 把 `tokens/response_lengths/rewards/raw_reward/truncated/sample_indices/rollout_ids` 一次性拼进字典，其中 `truncated` 由 `Sample.Status.TRUNCATED` 派生，`rollout_ids` 在前面（724-732 行）已为 `None` 的样本补了临时 id 以保证唯一。

loss_mask 的对齐与「剔除样本」处理：

[slime/ray/rollout.py:748-760](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout.py#L748-L760) 逐样本确保 `loss_mask` 长度等于 `response_length`（缺省则全 1），并把 `remove_sample=True` 的样本整段置 0——这是 `--rollout-sample-filter-path`（见 [u6-l1](u6-l1-customization-overview.md)）生效的最终落点：被剔除的样本并不从批次里删除，而是靠 loss_mask 归零退出损失计算。

整 rollout 级的预计算 `rollout_mask_sums`：

[slime/ray/rollout.py:772-777](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout.py#L772-L777) 按 `rollout_id` 累加各样本的可训练 token 数，得到每个 rollout 的总 mask 和，再广播回每条样本。注释解释了原因：first-fit 装箱可能把同一 rollout 的样本拆进不同微批，reducer 需要这个「整 rollout 分母」才能跨微把贡献加回成正确的 token 加权均值。

可选字段按需追加，以 off-policy 修正为例：

[slime/ray/rollout.py:791-793](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout.py#L791-L793) 仅当首条样本带 `rollout_log_probs` 时才把全批的 rollout logp 写进字典，供训练端做重要性采样比（与 [u4-l4](u4-l4-rl-loss-and-advantage.md) 衔接）。`multimodal_train_inputs`、`teacher_log_probs`、`rollout_routed_experts` 同理按需加入。

官方文档把这个返回字典的字段一一列了出来，是写自定义实现时的字段契约：

[docs/en/get_started/customization.md:315-347](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L315-L347) 给出了 `convert_samples_to_train_data` 的签名与返回字典标准字段（必备 + 可选），其中 `round_number`/`rollout_log_probs`/`rollout_routed_experts`/`metadata`/`multimodal_train_inputs`/`teacher_log_probs` 均为可选字段。

契约测试里的最小参考实现最适合当模板：

[tests/plugin_contracts/test_plugin_runtime_hook_contracts.py:60-69](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L60-L69) 的 `reference_convert_samples_to_train_data` 只返回了 7 个必备字段，说明自定义实现只要覆盖必备字段即可通过契约断言。

#### 4.2.4 代码实践

**实践目标**：跟踪一次默认转换，看清 `Sample` 的哪些字段映射进字典。

**操作步骤**（源码阅读型实践）：

1. 打开 `slime/ray/rollout.py` 的 `_convert_samples_to_train_data`（712 行起），对照下表，为每个返回 key 标注它取自 `Sample` 的哪个属性、被训练端哪一步消费：

   | 字典 key | 取自 Sample | 训练端消费点 |
   | :--- | :--- | :--- |
   | `tokens` | `sample.tokens` | 打包成微批、前向输入 |
   | `response_lengths` | `sample.response_length` | loss_mask 对齐、THD cu_seqlens |
   | `rewards` | `_post_process_rewards` 的归一化结果 | 优势估计（advantage）输入 |
   | `raw_reward` | 原始 reward（或 metadata 覆盖） | 日志/指标统计 |
   | `truncated` | `status == TRUNCATED` | 截断样本的处理标记 |
   | `loss_masks` | `sample.loss_mask` | 损失的 per-token 加权 |
   | `rollout_mask_sums` | 按 `rollout_id` 聚合 | pg_loss reducer 的分母 |

2. 阅读契约测试的断言，确认必备字段集合：

   [tests/plugin_contracts/test_plugin_runtime_hook_contracts.py:118-122](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L118-L122) 用集合包含断言 `{"tokens","response_lengths","rewards","raw_reward","truncated","sample_indices","loss_masks"} <= set(train_data)`，这正是自定义实现必须提供的最小字段集。

**需要观察的现象**：默认实现里 `loss_masks` 对 `remove_sample=True` 的样本整段置 0（757-758 行），即「剔除」是靠 mask 实现而非删数据。

**预期结果**：你能画出一张「Sample 字段 → train_data key → 训练端消费」的映射表，并解释为何 `rewards`（归一化）与 `raw_reward`（原始）要分开传。

**待本地验证**：可选地用 4.2.3 提到的 `reference_convert_samples_to_train_data` 在本地喂两条手工构造的 `Sample`，打印返回字典，核对每个 key 的长度与值。

#### 4.2.5 小练习与答案

**练习 1**：如果我提供 `--custom-convert-samples-to-train-data-path`，还需要单独提供 `--custom-reward-post-process-path` 吗？默认转换里的奖励后处理还会跑吗？

**答案**：默认转换里的 `_post_process_rewards` **不会**跑了——因为 716-717 行在自定义函数命中后直接 `return`，跳过了 719 行的 `_post_process_rewards` 调用。所以若你的自定义转换需要归一化后的 `rewards`，必须在函数内部自己调用（或复用默认的组归一化逻辑），否则 `rewards` 字段可能就等于原始 reward。`--custom-reward-post-process-path` 在此场景下不会自动生效（它只在默认转换路径里被调用）。

**练习 2**：`rollout_mask_sums` 为什么要在「样本→训练数据」这一层就预算好，而不是留给训练端算？

**答案**：因为只有在这一层能同时看到一个 rollout 的**全部**样本；训练端的 first-fit 装箱（见 [u4-l3](u4-l3-data-packing-microbatch.md)）可能把同一 rollout 的样本拆进不同微批甚至不同 DP rank，届时单个微批看不到全组，无法算出正确的 per-rollout 分母。所以在打包前预算并按样本广播，是保证归约正确性的必要前置。

---

### 4.3 reward_post_process：优势计算前的奖励后处理

#### 4.3.1 概念说明

奖励后处理是「奖励写回」与「优势估计」之间的一道工序：把 `sample.reward`（可能是多维、未归一化、含偏置的原始分）加工成「可直接进优势估计的标量列表」。默认实现做的是 GRPO 风格的**组内归一化**——扣组均值、可选除以组标准差。你也可以用 `--custom-reward-post-process-path` 替换成任意 shaping（如加 KL 惩罚、按长度缩放、多维加权）。

它的返回是固定二元组 `(raw_rewards, rewards)`：前者保留原始分用于日志，后者是真正喂给优势估计的值。

#### 4.3.2 核心流程

```text
_post_process_rewards(samples):
    若设了 --custom-reward-post-process-path:
        → return custom_func(args, samples)   # 须返回 (raw_rewards, rewards)
    # 默认实现：
    raw_rewards = [sample.get_reward_value(args) for sample in samples]
    若 advantage_estimator 属于 {grpo, gspo, cispo, r++_baseline} 且开启 rewards_normalization:
        rewards = 组内归一化(raw_rewards)
        若属于 {grpo, gspo, cispo} 且开启 grpo_std_normalization:
            rewards = rewards / (std + 1e-6)
        return raw_rewards, rewards.flatten()
    else:
        return raw_rewards, raw_rewards        # 不归一化，二者相同
```

GRPO 组归一化的数学含义（对一个 prompt 的 n 条采样，奖励记为 \(r_1,\dots,r_n\)）：

\[ \tilde r_i = r_i - \bar r, \qquad \bar r = \frac{1}{n}\sum_{j=1}^{n} r_j \]

可选的 std 归一化（仅 `grpo/gspo/cispo` 且 `--grpo-std-normalization`）：

\[ \hat r_i = \frac{\tilde r_i}{\mathrm{std}(\tilde r_1,\dots,\tilde r_n) + \epsilon}, \qquad \epsilon = 10^{-6} \]

扣均值让优势只反映「相对好坏」，除以 std 则稳定梯度尺度。注意默认实现里 std 是**在扣均值之后**算的（即对 \(\tilde r\) 求 std）。

#### 4.3.3 源码精读

入口与自定义短路：

[slime/ray/rollout.py:685-687](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout.py#L685-L687) 设了自定义后处理就直接委托，返回值原样透传（即你的函数须返回 `(raw_rewards, rewards)` 二元组）。

原始奖励的提取，`get_reward_value` 支持多维奖励按 key 选：

[slime/utils/types.py:246-247](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L246-L247) 表明：未设 `reward_key` 时 `reward` 直接当标量；设了则视为 dict 取 `reward[reward_key]`。这就是注释里「remote rm 可能返回多个奖励，用 key 选其中一个」的来源。

默认组归一化的完整逻辑：

[slime/ray/rollout.py:689-710](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout.py#L689-L710) 实现了上面的公式：先把奖励 reshape 成 `(组数, n_samples_per_prompt)`，按最后一维（组内）求均值并扣除；若算法属于 `grpo/gspo/cispo` 且开 `--grpo-std-normalization`，再除以扣均值后的 std。注意 696-700 行对「各组样本数不等」做了兜底 reshape，最后 `flatten().tolist()` 摊平返回。若不满足归一化条件（如非 GRPO 系或没开 normalization），则 `raw_rewards` 与 `rewards` 相同，原样返回。

契约测试里的参考实现最简洁：

[tests/plugin_contracts/test_plugin_runtime_hook_contracts.py:54-57](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L54-L57) 的 `reference_reward_post_process` 把每条 reward 加 1.0 作为「归一化」结果，演示了最简单的 shaping——返回 `(raw_rewards, rewards)` 二元组即可。

#### 4.3.4 代码实践

**实践目标**：用契约机制在纯 CPU 下自检一个自定义奖励后处理函数。

**操作步骤**：

1. 新建 `my_proj/my_postprocess.py`（示例代码）：

   ```python
   # my_proj/my_postprocess.py —— 示例代码
   def clip_reward_post_process(args, samples):
       """把每条 reward 裁剪到 [-1, 1]，并保留原始分。"""
       raw = [sample.reward for sample in samples]
       clipped = [max(-1.0, min(1.0, r)) for r in raw]
       return raw, clipped
   ```

2. 用环境变量把它接入契约测试（参考 [docs/en/get_started/customization.md:509-516](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L509-L516) 介绍的环境变量覆盖机制）：

   ```bash
   SLIME_CONTRACT_CUSTOM_REWARD_POST_PROCESS_PATH=my_proj.my_postprocess.clip_reward_post_process \
   python -m pytest tests/plugin_contracts/test_plugin_runtime_hook_contracts.py -k reward_post_process -v
   ```

   契约测试会做三层断言：①签名匹配（参数为 `args, samples`）；②最小调用后返回结构正确（二元组，两份长度都等于样本数，见 [tests/plugin_contracts/test_plugin_runtime_hook_contracts.py:113-115](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L113-L115)）；③调用点写法稳定（`self.custom_reward_post_process_func(self.args, samples)`）。

**需要观察的现象**：契约测试无需 GPU 即可验证你的函数「形状」正确，但它**不验证语义**（比如不会检查你的裁剪逻辑对不对），只检查返回是二元组且长度对齐。

**预期结果**：测试通过，说明你的后处理函数满足框架调用契约。真实启用时用 `--custom-reward-post-process-path my_proj.my_postprocess.clip_reward_post_process`。

**待本地验证**：若 `pytest` 未安装，可直接 `python tests/plugin_contracts/test_plugin_runtime_hook_contracts.py` 运行（该文件设计为可独立执行），效果等同。

#### 4.3.5 小练习与答案

**练习 1**：默认 `_post_process_rewards` 在 `advantage_estimator=ppo`（带 critic）时，`rewards` 会做组归一化吗？

**答案**：不会。组归一化的条件是 `advantage_estimator in ["grpo","gspo","cispo","reinforce_plus_plus_baseline"]` 且 `--rewards-normalization`（见 691-692 行）。`ppo` 不在这个集合里，所以走最后的 `return raw_rewards, raw_rewards`，`rewards` 等于原始分——因为 PPO 的基线扣除由 critic 经 GAE 完成（见 [u6-l4](u6-l4-advantage-estimators.md)），不需要在奖励层做组归一化。

**练习 2**：我的远程奖励服务对一条回答返回 `{correctness: 0.8, fluency: 0.3}` 两个维度。我想用 correctness 训练，该怎么配置？

**答案**：设 `--reward-key correctness`。这样 `get_reward_value`（246-247 行）会取 `sample.reward["correctness"]` 当作标量奖励进入后处理与训练；`fluency` 维度被忽略。注意此时奖励函数需把多维结果整体写进 `sample.reward`（一个 dict），而不是单个 float。

---

## 5. 综合实践

把本讲三块内容串起来，完成一个「自定义奖励 + 后处理 + 训练数据转换」的最小自洽组合（纯 CPU，无需 GPU）：

1. **写一个 batch 奖励**：实现 `async def rm(args, samples) -> list[float]`，对一组样本返回「正确性 / 响应长度」的比值（参考 4.1.4）。
2. **写一个后处理**：实现 `def post(args, samples) -> (raw, normalized)`，对返回值做 `[-1, 1]` 裁剪（参考 4.3.4）。
3. **写一个转换函数**：实现 `def convert(args, samples) -> dict`，返回 7 个必备字段，并**在自己的函数里调用** 4.3 的后处理（因为你接管了转换，默认后处理不会自动跑——见 4.2.5 练习 1）。
4. **用契约测试自检**：分别用 `SLIME_CONTRACT_CUSTOM_RM_PATH`、`SLIME_CONTRACT_CUSTOM_REWARD_POST_PROCESS_PATH`、`SLIME_CONTRACT_CUSTOM_CONVERT_SAMPLES_TO_TRAIN_DATA_PATH` 三个环境变量跑对应契约测试，全部通过。
5. **画一张数据流图**：标注「`async_rm`/`batched_async_rm` 写 `sample.reward` → `_post_process_rewards` 出 `(raw, rewards)` → `_convert_samples_to_train_data` 拼字典 → 训练端消费」，并指出自定义替换每一层时，前/后层是否仍会自动运行。

完成这个综合实践后，你应当能清楚地回答：在哪一层注入自定义逻辑最省事、接管转换层后还要自己补哪些步骤、以及这三个 hook 各自的「自动链路」边界。

## 6. 本讲小结

- slime 奖励函数有两种互斥签名：单样本 `async def rm(args, sample, **kwargs) -> float` 与 batch `async def rm(args, samples, **kwargs) -> list[float]`，由 `--group-rm` 决定框架用 `async_rm`（逐样本）还是 `batched_async_rm`（整组直调）来调你。
- 整组模式下设了 `--custom-rm-path` **不会回退**为逐样本调用，你的函数必须实现 batch 版本；评估阶段禁止整组模式。
- `convert_samples_to_train_data` 是「Sample 列表 → 训练张量字典」的翻译层，默认实现负责奖励后处理、字段拼装、loss_mask 对齐、`rollout_mask_sums` 预算与可选字段追加；可用 `--custom-convert-samples-to-train-data-path` 整体替换，但替换后默认奖励后处理不再自动跑。
- `reward_post_process` 是奖励写回与优势估计之间的工序，默认做 GRPO 组归一化（扣组均值、可选除以扣均值后的 std），返回固定二元组 `(raw_rewards, rewards)`；可用 `--custom-reward-post-process-path` 替换为任意 shaping。
- 三者都有纯 CPU 契约测试（`tests/plugin_contracts/`），用 `SLIME_CONTRACT_*` 环境变量即可自检自定义实现的签名、返回结构与调用点稳定性，无需 GPU。

## 7. 下一步学习建议

- 下一讲 [u6-l4 优势估计器与 RL 算法选择](u6-l4-advantage-estimators.md) 会接着本讲的「归一化 rewards」往下走，讲它如何进入 `compute_advantages_and_returns` 变成 per-token advantage，建议连贯阅读。
- 若想了解奖励写回之前的生成阶段，回顾 [u6-l2 自定义生成函数](u6-l2-custom-generate-function.md)，看 `custom_generate` 如何与 `custom_rm` 配合构成智能体 RL 的标准组合。
- 想深入「剔除样本靠 loss_mask 归零」这一机制的读者，可结合 [u4-l3 数据打包](u4-l3-data-packing-microbatch.md) 看 loss_mask 在打包与损失阶段的对齐细节。
- 对 off-policy 修正（`rollout_log_probs` 通道）感兴趣的读者，继续看 [u6-l5 自定义损失、TIS 与 off-policy 修正](u6-l5-custom-loss-offpolicy.md)。
