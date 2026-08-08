# 多样本 fan-out 与轨迹分段训练

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「一次 rollout 执行产出多个可训练 `Sample`」（fan-out / 轨迹分段）这件事为什么会出现、解决什么问题。
- 牢记 fan-out 的硬契约：**同一次 rollout 产出的兄弟样本必须共享同一个 `rollout_id`**，并能解释它如果不满足会破坏什么。
- 跟着源码看清训练侧如何「按 `rollout_id` 把分段聚合成一次 rollout」：步骤切分（step splitter）按 rollout 计数、损失归约（per-rollout-mean reducer）按 rollout 求平均、奖励如何分摊才不会被放大 K 倍。
- 读懂 `TrajectoryManager` 如何把一棵多轮会话树线性化成一组带 `loss_mask` 的训练分段。
- 看懂 `examples/multi_agent` 这个真实示例如何落地 fan-out。

## 2. 前置知识

本讲是专家层（advanced），默认你已掌握前置讲义的内容：

- **u3-l1 Sample 数据结构与生命周期**：`Sample` 的字段（`tokens` / `loss_mask` / `rollout_log_probs` / `reward` / `rollout_id`）、`Status` 状态机，以及 `append_response_tokens` 如何增量写回响应 token。
- **u3-l2 默认 rollout 函数 generate_rollout 全流程**：默认 rollout 的形状是 `list[list[Sample]]`（外层 prompt 组、内层同一 prompt 的多采样副本），以及 `generate_and_rm` / `generate_and_rm_group` 的分层结构。
- **u4-3 数据打包、微批调度与 loss mask**：`build_dp_schedule` 的「先装箱、再分发」调度，以及 `global_batch_size` 的含义。
- **u4-4 RL 损失与优势估计**：`rollout_mask_sums` 作为损失归约分母、GRPO 组归一化、`sum_of_sample_mean` 归约。
- **u7-l1 智能体 RL 路线图与接口选择**：`--custom-generate-function-path` 的契约 `async def custom_generate(args, sample, sampling_params) -> Sample | list[Sample]`，以及 fan-out 时兄弟样本共享 `rollout_id`、总 reward 按 1/K 分摊的原则。

几个关键术语回顾：

- **rollout（一次 rollout 执行）**：对一个 prompt 跑一次采样流程，默认产出一条训练 `Sample`。
- **fan-out（扇出）**：一次 rollout 执行产出**多条**训练 `Sample`，又叫 compact / subagent / 轨迹分段。
- **兄弟样本（sibling samples）**：同一次 fan-out 产出的那几条 `Sample`。
- **`rollout_id`**：标记「这条样本来自第几次 rollout」的编号，默认回退到 `index`（即一执行一样本时 `rollout_id == index`）。

> 一个一句话动机：subagent 分派、上下文压缩（compaction）、多 agent 协作等场景里，一条轨迹天然由多段「模型生成的可训练片段」拼接而成。把它们压成一条 `Sample` 会丢失分段结构、也放不进单条序列；正确做法是切成多条 `Sample` 分别训练——但这必须配合一个不会把「一次 rollout」重复计 K 次的机制。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注 |
| :--- | :--- | :--- |
| `slime/utils/types.py` | `Sample` dataclass 定义 | `rollout_id` 字段语义、`append_response_tokens` 的 `trainable` 区分 |
| `slime/agent/trajectory.py` | 多轮会话树 → 训练分段的线性化器 | `TrajectoryManager` 如何把一棵树切成 `list[Sample]`、奖励如何落到每段 |
| `slime/rollout/sglang_rollout.py` | 默认 rollout 函数 | `generate_and_rm` 如何兼容 `Sample` 与 `list[Sample]` 两种返回 |
| `slime/ray/rollout.py` | rollout→训练数据装配 | `rollout_mask_sums` 按 `rollout_id` 聚合、`_split_train_data_by_dp`、`_validate_rollout_id_annotated` |
| `slime/utils/dp_schedule.py` | 纯 Python 调度表 | 按 `rollout_id` 分组、按 `global_batch_size`（rollout 数）切 step |
| `slime/backends/megatron_utils/cp_utils.py` | 损失归约器 | `get_sum_of_sample_mean` 的 per-rollout token 加权平均 |
| `slime/backends/megatron_utils/loss.py` | RL 损失 | 用 `rollout_mask_sums` 作分母归约 pg_loss |
| `examples/multi_agent/agent_system.py` | 多 agent 真实示例 | `input_rollout_id` + `_emit` 给所有兄弟盖同一个 `rollout_id` |
| `slime/rollout/_fanout_test_helpers.py` | fan-out 测试夹具 | `compact_generate` 是最简 fan-out 范本 |
| `docs/en/get_started/customization.md` | 定制化文档 | fan-out 与 reward/K 分摊的官方约定 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：①fan-out 的硬契约与训练侧如何按 `rollout_id` 聚合（这是地基，先讲）；②`TrajectoryManager` 的轨迹管理；③`multi_agent` 示例。

> 说明：为保证「契约」这条主线最先建立，下面把「fan-out rollout_id 约束」作为 4.1，把「轨迹管理」作为 4.2，把「multi_agent 示例」作为 4.3。

### 4.1 fan-out 的硬契约：兄弟样本必须共享 rollout_id

#### 4.1.1 概念说明

默认情况下，slime 的 rollout 形状是 `list[list[Sample]]`（外层是 prompt 组、内层是同 prompt 的 `n_samples_per_prompt` 份采样），此时**一次 rollout 执行只产出一条训练样本**，所以 `rollout_id` 自然回退到 `index`，二者相等，谁也不需要特别设置。

fan-out 打破了这个「一执行一样本」的假设：一次 rollout 执行产出 K（K 可以是 1，也可以是 3）条训练样本。典型来源：

- **subagent 分派**：主 agent 调起一个子 agent，子 agent 的轨迹段和主 agent 的续写段都值得训练。
- **上下文压缩（compaction）**：一条很长的轨迹被压缩成「压缩前一段 + 压缩后一段」。
- **多 agent 协作**：solver / rewriter / selector 多个角色各自产生可训练片段。

如果不加约束地把这 K 条样本当成 K 次独立 rollout，会出现两类错误：

1. **步骤计数被放大**：训练 step 数 = `rollout 数 // global_batch_size`。把 K 条样本当 K 个 rollout，step 数会被放大 K 倍，`global_batch_size` 的语义（每 step 喂多少 rollout）也被破坏。
2. **损失/奖励被放大**：每条样本都按独立 rollout 求一次平均，K 条样本就把同一份轨迹的梯度信号累计了 K 次。

slime 的解法是一条硬契约：**同一次 fan-out 产出的所有兄弟样本，必须带上同一个 `rollout_id`**。这样训练侧就能用 `rollout_id` 把它们识别为「同一次 rollout」，按一次 rollout 去切 step、去归约损失。

`Sample.rollout_id` 字段的文档把这条契约写得非常直白：

> 默认为 `None`，下游回退到 `index`（默认 rollout 路径一执行一样本，所以 `rollout_id == index`）。把一次 rollout 执行切成多条训练样本的 compact / subagent 路径，应当在每个兄弟上都设同一个 `rollout_id`，让损失聚合在 rollout 内部平均，而不是重复计数。

#### 4.1.2 核心流程

fan-out 数据从「产出」到「被训练」经过四个关卡，每个关卡都依赖共享的 `rollout_id`：

```text
custom_generate 返回 list[Sample]（兄弟样本共享 rollout_id）
        │
        ▼
① generate_and_rm 识别 list[Sample]，对扁平兄弟列表逐样本算 reward
        │
        ▼
② _validate_rollout_id_annotated：在 depth≥2 处校验兄弟 rollout_id 非空且相同
        │
        ▼
③ _convert_samples_to_train_data：按 rollout_id 聚合 loss_mask，预算 rollout_mask_sums
        │
        ▼
④ build_dp_schedule：按 rollout_id 分组，按 global_batch_size 个 rollout 切 step
   （一个 rollout 的所有兄弟样本留在同一个 step）
        │
        ▼
⑤ get_sum_of_sample_mean（per-rollout-mean reducer）：用 rollout_mask_sums 作分母，
   把兄弟样本的贡献聚合成「每 rollout 一次 token 加权平均」
```

奖励分摊是另一条相关约定（见 4.1.3 末尾），但**防止放大的真正地基是 ③④⑤ 这条「按 rollout_id 聚合」的链路**，而不是 reward 怎么分。

#### 4.1.3 源码精读

**（a）`rollout_id` 字段契约**

[slime/utils/types.py:99-106](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L99-L106) —— `Sample.rollout_id` 字段及其文档。注意「compact / subagent 路径应在每个兄弟上都设同一个 `rollout_id`」这句就是契约本身。

**（b）`generate_and_rm` 兼容 `Sample` 与 `list[Sample]`**

custom_generate 既可以返回单条 `Sample`，也可以返回 `list[Sample]`。`generate_and_rm` 用 `isinstance(sample, list)` 分流：fan-out 情况下，对扁平的兄弟列表用 `batched_async_rm` 逐样本算 reward。

[slime/rollout/sglang_rollout.py:268-278](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L268-L278) —— fan-out 返回 `list[Sample]` 时，挑出 `reward is None` 的样本，用 `batched_async_rm` 批量打分，再写回各自的 `sample.reward`。

[slime/rollout/sglang_rollout.py:298-304](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L298-L304) —— `generate_and_rm_group` 的注释说明：`asyncio.gather` 会保留每个任务返回的形状，所以普通 rollout 是 `list[Sample]`、fan-out 是 `list[list[Sample]]`。

> ⚠️ 一个易踩的坑：fan-out（返回 `list[Sample]`）**不要**再开 `--group-rm`。`--group-rm` 会把整组奖励计算推迟到 `generate_and_rm_group`，而那里假设 `group` 已经是扁平 `list[Sample]`；和 fan-out 叠用会得到 `list[list[Sample]]`，让 `async_rm` 在 `'list' object has no attribute 'metadata'` 上崩溃。这点在 [tests/test_qwen2.5_0.5B_fanout_short.py:80-87](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_qwen2.5_0.5B_fanout_short.py#L80-L87) 的注释里有明确记录。

**（c）校验器 `_validate_rollout_id_annotated`**

slime 在装配训练数据前会递归遍历 rollout 输出，**只在检测到 compact 形状时才校验**。关键判定是「嵌套深度」：默认形状 `list[list[Sample]]` 的叶子 `list[Sample]` 在 depth=1，跳过校验以保持向后兼容；fan-out 形状 `list[list[list[Sample]]]` 的叶子落在 depth≥2，此时要求叶子内每个兄弟都带 `rollout_id` 且彼此相等。

[slime/ray/rollout.py:901-930](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L901-L930) —— 校验器全文。注意 L918-L927 的两条断言：`rollout_id` 不能缺失、且 `len(set(rids)) == 1`（兄弟必须同值）。

**（d）按 `rollout_id` 预算 `rollout_mask_sums`**

为了让损失归约用对分母，slime 在「样本→训练数据」这一层（能看到一个 step 里每条 rollout 的全部样本）就按 `rollout_id` 把每条 rollout 的可训练 token 数求和，再广播回每条样本。

[slime/ray/rollout.py:762-777](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L762-L777) —— `rollout_mask_sums` 的计算：`rollout_total_mask[rid]` 按 `rollout_id` 累加各样本的 `sum(loss_mask)`，再 `[rollout_total_mask[rid] for rid in rollout_id_list]` 广播回每条样本。注释（L762-L771）点明：first-fit 装箱可能把一个 rollout 的样本拆进不同微批，所以分母必须在 step 级别预算，否则每边都拿到一个残缺的分母。

**（e）按 `rollout_id` 分组、按 rollout 数切 step**

`build_dp_schedule` 的第一步就是按 `rollout_id` 分组，再按 `global_batch_size`（注意单位是 **rollout 数**，不是样本数）切成若干 step。

[slime/utils/dp_schedule.py:127-135](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/dp_schedule.py#L127-L135) —— 用字典 `rollout_id_to_samples` 把同一 `rollout_id` 的样本位置收集到一起，`rollout_ids = list(rollout_id_to_samples.keys())` 得到「不重复的 rollout 列表」，后续 `num_steps = len(rollout_ids) // global_batch_size`。这正是「按 rollout 计数」的实现。

模块顶部 [slime/utils/dp_schedule.py:10-15](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/dp_schedule.py#L10-L15) 的设计说明里有一句关键：「compact / subagent 一次 rollout 可能产出多条训练样本，此时这些样本全部留在同一个 step」。`build_dp_schedule` 的 `global_batch_size` 形参注释 [slime/utils/dp_schedule.py:100-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/dp_schedule.py#L100-L104) 也写明：「每个训练 step 的 rollout 数（不是训练样本数）」。

**（f）per-rollout-mean 损失归约器**

损失归约时，`get_sum_of_sample_mean` 接收上面预算好的 `sample_denoms`（即 `rollout_mask_sums`）作为分母，对一条 rollout 内的所有兄弟样本求一个 token 加权平均。

[slime/backends/megatron_utils/cp_utils.py:54-81](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L54-L81) —— 归约器实现。L67-L68 是「传统 per-sample 均值」（分母是每条样本自己的 `loss_mask.sum()`）；当传入 `sample_denoms` 时，分母换成「整条 rollout 的 mask 总和」。这样把 K 条兄弟样本各自的 `(x*mask).sum()` 求和、再除以整条 rollout 的总 mask，就得到一个**整 rollout 的 token 加权平均**——无论 K 是 1 还是 3，一次 rollout 都只贡献「一次」的量级。

把这个分母接进损失的地方在 [slime/backends/megatron_utils/loss.py:1023-1029](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1023-L1029) 与 [slime/backends/megatron_utils/loss.py:1042-1044](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1042-L1044)：`sum_of_sample_mean` 用 `batch["rollout_mask_sums"]` 作分母，pg_loss 与 ppo_kl 等指标都归约到同一个 per-rollout 均值空间。

> 用一个最简数值感性地理解归约器（CP=1，忽略 advantage 细节）：设一次 rollout 有 K=2 条兄弟样本，mask 计数分别为 \(m_1, m_2\)，每条样本的 token 损失之和为 \(s_1, s_2\)。per-sample 均值会给出 \(s_1/m_1 + s_2/m_2\)（两条各算一次、量级翻倍）；per-rollout 均值给出 \((s_1+s_2)/(m_1+m_2)\)（合并成一次）。共享 `rollout_id` → 预算出正确分母 \(m_1+m_2\) → 归约成后者，避免放大。

**（g）奖励分摊：两种都被接受的约定**

奖励如何落到每段，有两条都「正确」的约定，**前提都是兄弟共享 `rollout_id`**（靠 ④⑤ 把 rollout 计一次）：

1. **reward/K 分摊**（手写 custom_generate 的推荐写法）：把总 reward 平均分到 K 段，每段 `reward/K`，文档原话是「这样同一份 rollout reward 不会被放大」。见 [docs/en/get_started/customization.md:117](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L117)。
2. **每段都带全额 reward**（内置 `TrajectoryManager.get_trajectory` 的做法）：把整条轨迹的 reward 原样写到每一段，靠 per-rollout-mean 归约器把这条 rollout 计一次。见 [slime/agent/trajectory.py:319-322](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L319-L322) 与 L339-L340。

两条约定的差别主要体现在 GRPO 组归一化（advantage）那一层；而「不被放大」的保证来自共享 `rollout_id` + per-rollout-mean 归约，与 reward 怎么分摊是正交的。本讲的综合实践采用约定 1（reward/K），因为它最适合手写 fan-out 骨架。

#### 4.1.4 代码实践：阅读 fan-out 的「全链路固化」测试

slime 专门有一个端到端测试把 fan-out 的完整链路钉死，是理解整套机制最好的入口。

1. **实践目标**：通过阅读 `tests/test_qwen2.5_0.5B_fanout_short.py`，确认 fan-out 从 custom_generate 到 train_one_step 的五道关卡都被覆盖。
2. **操作步骤**：
   - 打开 [tests/test_qwen2.5_0.5B_fanout_short.py:1-40](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_qwen2.5_0.5B_fanout_short.py#L1-L40)，阅读顶部 docstring，它列出了「这个测试到底钉住了什么」。
   - 再打开夹具 [slime/rollout/_fanout_test_helpers.py:42-73](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/_fanout_test_helpers.py#L42-L73) 的 `compact_generate`，这是最简 fan-out 范本。
3. **需要观察的现象**：docstring 里 L12-L21 列出的五步链路（custom_generate 返回共享 rollout_id 的 list[Sample] → 校验通过 → 按 rollout_id 切 step 且 step 数用 `rollout_batch_size * n_samples_per_prompt / global_batch_size` 而非样本数 → 损失用 `rollout_mask_sums` 聚合 → `step_global_batch_size` 分母等于 rollout 数而非样本数）。
4. **预期结果**：你能用自己的话讲清「为什么 fan-out 不会让梯度量级随 K 翻倍」。注意夹具里 `n = 1 + (sample.index % MAX_FANOUT)`（[L62-L71](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/_fanout_test_helpers.py#L62-L71)），`MAX_FANOUT=3`，所以 {1,2,3} 三种 K 在一次 rollout 里都会被覆盖，且 K=1 把「无 fan-out 的兼容路径」也留在 CI 里。
5. 这是一个阅读型实践（需要 4 卡 GPU 才能真跑），重点是读懂断言链，不需要实际执行。

#### 4.1.5 小练习与答案

**练习 1**：如果某次 fan-out 产出 3 条兄弟样本，但你忘了给其中 2 条设 `rollout_id`，会在哪一步被拦下？报什么错？

**参考答案**：会在 `_validate_rollout_id_annotated`（[slime/ray/rollout.py:918-927](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L918-L927)）被断言拦下，提示 `rollout_id is unset on positions [...]`。注意校验只在叶子 `list[Sample]` 落到 depth≥2 且 `len(node) > 1` 时触发；单条样本（K=1）即使没设也不会报错，因为它回退到 `index`。

**练习 2**：为什么 `rollout_mask_sums` 必须在「样本→训练数据」这一层预算，而不能留到微批（micro-batch）级别算？

**参考答案**：因为 first-fit 装箱可能把同一个 rollout 的兄弟样本拆进不同微批（见 [slime/ray/rollout.py:762-771](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L762-L771) 的注释与 [slime/backends/megatron_utils/cp_utils.py:60-66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L60-L66) 的文档）。若在每个微批里临时算分母，每个微批只能看到这条 rollout 的一部分样本，拿到的分母是残缺的；只有在 step 级别（能看到该 rollout 全部兄弟）预算，分母才是整条 rollout 的 mask 总和。

**练习 3**：去掉共享 `rollout_id`（让 K 条样本各走各的 `index`），`build_dp_schedule` 里 `num_steps` 会怎样变化？

**参考答案**：`num_steps = len(rollout_ids) // global_batch_size`（[slime/utils/dp_schedule.py:135](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/dp_schedule.py#L135)）。不共享 `rollout_id` 时，`rollout_ids` 把每条样本当成独立 rollout，数量从「rollout 数」膨胀到「样本数」，`num_steps` 被放大；这正是测试 docstring L14-L16 所防范的「用样本数算 step 数」回归。

### 4.2 轨迹管理：TrajectoryManager 如何把会话树切成训练分段

#### 4.2.1 概念说明

`TrajectoryManager`（`slime/agent/trajectory.py`）服务的是「用一个现成 agent 运行时（如 Claude Code / Codex）跑多轮对话」的场景（详见 u7-l2）。它解决的问题和 4.1 的手写 fan-out 是同一类——一次 rollout 执行产出多条训练样本——只不过这里的「分段」是**自动**从多轮对话的会话树里切出来的。

它的工作可以概括为一句话：**把一个 session（多轮、可能分叉）的会话树，线性化成一组带 `loss_mask` 的 `Sample`，每段都是「根到某叶子」的一条路径上、模型真正生成过的可训练 token。**

几个关键概念：

- **会话树（routing tree）**：一个 session 的消息不是一条线性列表，而是一棵树。模型每一轮的生成是一个「generated」节点；system/user/tool 消息以及「客户端重放但本轮没生成」的 assistant 消息是「routing-only」节点（只用于路由、不训练）。
- **drift（漂移）**：客户端在下一轮 prompt 里重放的对话历史，经过 chat-template 重新渲染或 TITO（text-in-text-out）往返，token id 往往不能和上一轮我们手里持有的 token 逐字节对齐。这种「已持有 token 与新 prompt 的偏差」就是 drift。
- **fork（分叉）**：当 drift 太大、太靠前、没法吸收时，当前累积的样本就闭合，新开一条样本从头累积——这个边界就是 fork。fork 是产生多条 `Sample` 的直接原因。

#### 4.2.2 核心流程

```text
record_turn(sid, turn, prompt_messages, response_message)
   │  逐轮喂入：在会话树里找到挂载点 → 挂上 prompt 消息 → 挂一个 assistant 生成叶子
   ▼
get_trajectory(sid, base_sample, reward)
   │  遍历每条「根→叶子」路径（chain）
   │  对每条 chain：_split_chain_into_builders 按 drift 切成若干 _SampleBuilder（fork 边界）
   │  每个 builder → 一个 Sample（剥掉首轮 prompt，loss_mask/logprob 只覆盖响应区）
   │  把整条轨迹的 reward 写到每个 emitted Sample
   ▼
返回 list[Sample]（每条都是一个可训练分段，共享 base_sample.rollout_id）
```

每个 `_SampleBuilder` 内部用三态分类吸收 drift：

| drift 类型 | 触发条件 | 处理 |
| :--- | :--- | :--- |
| `CLEAN` | drift == 0，新 prompt 是已持有 token 的精确前缀 | 直接续接 prompt 尾部 |
| `REALIGN` | drift 落在最近一段响应区内且较短 | 用新 prompt 覆盖那段漂移区（置 `loss_mask=0`），继续累积 |
| `FORK` | drift 太长 / 太靠前 / 输出太长 | 闭合当前 builder，新开一条（这就是分段边界） |

#### 4.2.3 源码精读

**（a）会话树节点 `MessageNode`**

[slime/agent/trajectory.py:46-82](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L46-L82) —— 节点用 `turn` 是否为 `None` 区分「generated」（本轮真生成的 assistant，`turn` 持有 `TurnRecord`）与「routing-only」（只用于路由）。注意 L80-L82 的 `response_trained` 标志：当多条兄弟叶子路径共享同一段生成响应时，只有第一条路径训练它，其余路径把它重新作为 `loss_mask=0` 上下文——保证每段响应**只被训练一次**。

**（b）drift 三态分类**

[slime/agent/trajectory.py:169-191](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L169-L191) —— `classify_token_drift`：先用 `_common_prefix_len` 算公共前缀，`drift = len(self.tokens) - realign_at`；`drift==0` 走 `CLEAN`；若 drift 落在最近响应区内（`realign_at >= last_response_start_idx`）且输出较短（`len(turn.output_ids) < fork_threshold`）走 `REALIGN`；否则 `FORK`。`fork_threshold` 默认 1024 token（见 L271）。

**（c）按 fork 切分链为多个 builder**

[slime/agent/trajectory.py:456-477](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L456-L477) —— `_split_chain_into_builders`：遍历 chain 里的 generated 节点，能续接就塞进当前 builder，遇到 `FORK` 就新开一个 builder。L469-L470 实现「共享响应只训练一次」：`trained = not asst_node.response_trained; asst_node.response_trained = True`。

**（d）每个 builder 产出一个 `Sample`，盖上共享 `rollout_id`**

[slime/agent/trajectory.py:234-261](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L234-L261) —— `to_sample`：剥掉首轮 prompt（`start = leading_prompt_len`），`loss_mask` 与 `rollout_log_probs` 只覆盖响应区。注意 L251：`rollout_id = base_sample.rollout_id if base_sample.rollout_id is not None else base_sample.index`——**这正是 fan-out 契约在轨迹管理器里的落地**，同一次 session 切出的所有分段都继承同一个 `rollout_id`。

**（e）奖励落到每段 + 消费 session**

[slime/agent/trajectory.py:307-344](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L307-L344) —— `get_trajectory`：遍历每条叶子链（L329-L337），把每段 builder 转成 `Sample`；然后 L339-L340 `for s in samples: s.reward = reward`，即**把整条轨迹的 reward 原样写到每一段**（约定 2，靠 per-rollout-mean 归约计一次）。L342 `self._trees.pop(sid, None)` 说明一次调用就消费掉该 session，再次调用同一 `sid` 返回 `[]`。

> 把 4.1 和 4.2 串起来：`TrajectoryManager` 是「自动 fan-out」——它替你切分段、替你在每段盖上同一个 `rollout_id`、替你写 reward；你手写 custom_generate fan-out 时，这三件事都得自己做。无论哪种路径，下游的 `rollout_mask_sums` 预算 + `build_dp_schedule` 按 rollout 切 step + per-rollout-mean 归约都是同一套。

#### 4.2.4 代码实践：手工模拟一次 fork 切分

1. **实践目标**：用纯 Python 模拟 `TrajectoryManager` 在 drift 下如何把一条多轮链切成两段，直观理解 fork。
2. **操作步骤**（源码阅读型，不依赖 GPU）：
   - 在笔记里画一条 token 序列，表示第一轮：`prompt_1(100) + resp_1(80)`，已持有 180 token。
   - 设想第二轮 prompt 的前 180 token 与已持有**完全一致**（CLEAN），续接 `prompt_2_tail(40) + resp_2(60)`，此时持有 280 token、单个 builder。
   - 再设想第三轮 prompt 在**第 50 个 token 处**就发生 drift（太靠前，远早于 `last_response_start_idx`），按 [L188-191](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L188-L191) 判定为 `FORK`。
3. **需要观察的现象**：第三轮会闭合第一个 builder（产出 Sample A，覆盖前两轮），新开第二个 builder（产出 Sample B，覆盖第三轮）。两条 Sample 共享同一个 `rollout_id`。
4. **预期结果**：你能指出 `last_response_start_idx`（[L208](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L208)）在 REALIGN 判定里的作用——只有 drift 落在「最近一段响应」内部才允许就地修复，否则宁可 fork。
5. 若想真跑，可在 CPU 上 `from slime.agent.trajectory import TrajectoryManager, TurnRecord`，喂 2-3 个 `record_turn` 后调 `get_trajectory`，打印返回的 `list[Sample]` 长度与各自的 `rollout_id`。此为待本地验证项（需要构造合法的 token id 列表）。

#### 4.2.5 小练习与答案

**练习 1**：`TrajectoryManager` 切出的多条 `Sample`，它们的 `rollout_id` 从哪来？为什么这样设计？

**参考答案**：来自 `to_sample` 的 [L251](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L251)，统一取 `base_sample.rollout_id`（回退到 `index`）。因为同一次 session 就是同一次 rollout 执行，切出的所有分段是兄弟样本，必须共享 `rollout_id` 才能被下游当作一次 rollout 聚合。

**练习 2**：`get_trajectory` 为什么把整条轨迹的 reward 原样写到每一段，而不是 reward/K？

**参考答案**：见 [L319-L322](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L319-L322) 文档：每段都带轨迹结果奖励，靠 per-rollout-mean 归约器把这条 rollout 计一次，不会因段数 K 而放大。这与手写 fan-out 的 reward/K 约定是两条都正确的等价路径（见 4.1.3-g）。

### 4.3 multi_agent 示例：一次 rollout 产出多角色样本

#### 4.3.1 概念说明

`examples/multi_agent` 是一个真实可跑的多 agent RL 示例：对一个数学题，并发跑 `num_parallel` 个 solver，再并发跑 rewriter 改写、最后由 selector 选出最佳解。这三类角色（solver / rewriter / selector）的生成轨迹**都值得训练**，于是对**同一个输入 prompt**的一次执行，最终返回 solver+rewriter+selector 一大批 `Sample`。

这是 fan-out 的典型形态：一次 rollout 执行 → 多条训练样本。该示例通过 `--custom-generate-function-path examples.multi_agent.rollout_with_multi_agents.generate_with_multi_agents` 注入（注意：尽管部分文档把它描述成 rollout-function 示例，但 [generate_with_multi_agents 的签名](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/rollout_with_multi_agents.py#L16) `async def ...(args, sample, sampling_params, evaluation=False) -> list[Sample]` 是 custom_generate 的契约）。

它示范了 fan-out 的三个要点：①给所有兄弟盖同一个 `rollout_id`；②用 `batched_async_rm` 整组算奖励；③用一个 `reward_adjustment` 权重（正确加权 / 错误加权）来分配奖励，而不是简单的 reward/K。

#### 4.3.2 核心流程

```text
generate_with_multi_agents(args, sample, sampling_params)
   │  载入 tokenizer、把 MULTI_AGENT_CONFIGS 写到 args、load_function 拿到 run_agent_system
   ▼
run_agent_system(args, sample)
   │  input_rollout_id = sample.index           # 先捕获，避免后续被循环变量遮蔽
   │  并发 solver_worker × num_parallel → solver 样本
   │  batched_async_rm 给 solver 打分
   │  并发 rewrite_worker × num_parallel → rewriter 样本，打分
   │  selector 选最佳解
   │  按最终对错，用 reward_adjustment 给三类角色加权（correct/incorrect_reward_weight）
   │  return _emit(solver + rewriter + selector)  # 给每个样本盖 input_rollout_id
   ▼
返回 list[Sample]（所有兄弟 rollout_id == input_rollout_id）
```

#### 4.3.3 源码精读

**（a）入口装配**

[examples/multi_agent/rollout_with_multi_agents.py:16-33](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/rollout_with_multi_agents.py#L16-L33) —— 入口：载入 tokenizer、把 `MULTI_AGENT_CONFIGS`（含 `num_parallel=5`、奖惩权重）`setattr` 到 `args`，再用 `load_function` 解析 `run_agent_system`，最后 `random.shuffle(samples)` 打乱后返回。注意返回类型是 `list[Sample]`。

**（b）捕获并盖印共享 `rollout_id`（核心）**

[examples/multi_agent/agent_system.py:205-218](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L205-L218) —— 这是整个示例最关键的一段。`input_rollout_id = sample.index` 在函数开头**提前捕获**（注释 L210-L212 说明：因为后面 `sample` 会被 `zip` 循环变量遮蔽）；`_emit` 闭包在**每个返回点**把 `input_rollout_id` 盖到所有样本上。这样无论最终返回 solver、solver+rewriter、还是 solver+rewriter+selector，所有兄弟都共享同一个 `rollout_id`。

**（c）按角色加权分配奖励**

[examples/multi_agent/agent_system.py:230-233](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L230-L233) —— `reward_adjustment`：把一组样本的 reward 统一乘以一个权重。在 [L285-L294](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L285-L294) 里，按 selector 最终是否选中正确解，分别用 `correct_reward_weight=1.2` 或 `incorrect_reward_weight=0.8` 给三类角色加权。这是 reward 分摊的第三种写法（区别于 reward/K 与全额 reward），同样以共享 `rollout_id` 为前提。

**（d）多返回点都用 `_emit` 收口**

[examples/multi_agent/agent_system.py:237](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L237)、[L259](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L259)、[L267](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L267)、[L296](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L296) —— 示例有多个提前返回点（比如 rewriter 全失败就只返回 solver）。**每个返回点都经过 `_emit`**，保证无论走哪条分支、产出几条样本，`rollout_id` 都被正确盖印——这是写 fan-out 时最容易遗漏的地方（漏掉一个 return 点就会让部分兄弟变成「独立 rollout」）。

> 对照旗舰示例 `coding_agent_rl`：它的 [README](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L184-L186) 同样写明「`generate()` 返回 `list[Sample]`，每条根→叶链一个 Sample；每条轨迹的 reward 按 reward/K 分摊到各链，`rollout_id` 共享，使 per-rollout-mean 归约仍把这条轨迹计一次；subagent 分派与自动压缩会增大 K」。它走的正是 4.2 的 adapter + `TrajectoryManager` 路径。

#### 4.3.4 代码实践：阅读 multi_agent 的返回点与盖印

1. **实践目标**：确认 `run_agent_system` 的**每一个** return 都经过 `_emit`，理解「多返回点都要盖印 rollout_id」这一工程要点。
2. **操作步骤**：
   - 打开 [examples/multi_agent/agent_system.py:235-296](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L235-L296)。
   - 列出所有 `return _emit(...)` 出现的行号与各自返回的样本组合（仅 solver / solver+rewriter / 三类全有）。
3. **需要观察的现象**：四个返回点（L237、L259、L267、L296）全部经 `_emit`；没有任何一个 `return` 直接返回裸列表。
4. **预期结果**：你能解释「为什么 `input_rollout_id` 必须在函数开头捕获、而不能在 `_emit` 里现取 `sample.index`」——因为后续 `for sample, reward in zip(...)` 会把 `sample` 这个名字重新绑定为循环变量（[L225](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L225)、[L253](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/agent_system.py#L253)），现取就会拿到错误的对象。注释 L210-L212 专门强调了这一点。
5. 待本地验证：若要真跑，参考 [examples/multi_agent/README.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/README.md) 的 `bash examples/multi_agent/run-qwen3-30B-A3B-multi-agent.sh`（需多卡与数据集）。

#### 4.3.5 小练习与答案

**练习 1**：`multi_agent` 用 `reward_adjustment` 给奖励乘 0.8 或 1.2，而不是 reward/K。这为什么不违背「不被放大」原则？

**参考答案**：「不被放大」靠的是共享 `rollout_id` + per-rollout-mean 归约把这次 rollout 计一次（见 4.1.3-d/e/f），与奖励如何取值是正交的。`reward_adjustment` 只是在「计一次」的前提下调整每条样本的奖励数值（影响 advantage），并不会把整条 rollout 的梯度翻 K 倍。

**练习 2**：把 `run_agent_system` 里的 `input_rollout_id = sample.index` 这一行删掉、并在 `_emit` 里改成 `s.rollout_id = sample.index`，会发生什么？

**参考答案**：由于 `sample` 在 `for sample, reward in zip(...)` 循环里被重新绑定，`_emit`（若在循环后调用）里取到的 `sample.index` 是循环最后一个样本的 index，而非输入样本的 index；而且不同兄弟会被盖上不同的（错误的）`rollout_id`，下游要么在校验器报错，要么把一次 rollout 误计成多次。这正是该行必须提前捕获的原因。

## 5. 综合实践

**任务**：手写一个把一条轨迹拆成 K 段、并把总 reward 按 reward/K 分摊到每段的 `custom_generate` 骨架，确保所有段共享同一个 `rollout_id`。

**背景**：这是 [docs/en/get_started/customization.md:99-117](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L99-L117) 推荐的 fan-out 写法，也是本讲规格指定的实践任务。

**操作步骤**：

1. 阅读文档示例 [docs/en/get_started/customization.md:99-114](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L99-L114) 与夹具 [slime/rollout/_fanout_test_helpers.py:42-73](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/_fanout_test_helpers.py#L42-L73)，作为骨架参考。
2. 在你自己的模块里（注意：模块路径不能含点，否则 `importlib.import_module` 无法解析 `--custom-generate-function-path` 字符串）写出如下骨架（**示例代码**，非项目原有代码）：

   ```python
   import copy
   from slime.utils.types import Sample
   from slime.rollout.sglang_rollout import generate


   async def split_and_share_reward(args, sample: Sample, sampling_params) -> list[Sample]:
       # 1. 先捕获这次 rollout 的 id（避免后续被遮蔽）
       rollout_id = sample.rollout_id if sample.rollout_id is not None else sample.index

       # 2. 跑一次生成，得到一条完整轨迹（这里用默认 generate 占位；
       #    真实场景是你自己的多段切分逻辑，比如按 </think> 或工具调用边界切）
       base = await generate(args, sample, sampling_params)

       # 3. 把轨迹切成 K 段（示例：均分成 K 段）
       K = 3
       seg_len = max(1, base.response_length // K)
       segments = []
       for i in range(K):
           start = i * seg_len
           end = base.response_length if i == K - 1 else (i + 1) * seg_len
           s = copy.copy(base)                       # 浅拷贝，保留 index/group_index/prompt 等
           s.tokens = base.tokens[: len(base.tokens) - base.response_length + end]
           s.response_length = end - start
           s.loss_mask = base.loss_mask[start:end]
           s.rollout_log_probs = base.rollout_log_probs[start:end]
           s.status = Sample.Status.COMPLETED
           s.rollout_id = rollout_id                 # ★ 关键：所有兄弟共享同一 rollout_id
           segments.append(s)

       # 4. reward/K 分摊：总 reward 平均到 K 段
       total_reward = base.reward if base.reward is not None else 0.0
       for s in segments:
           s.reward = total_reward / K

       return segments
   ```

3. 用 `--custom-generate-function-path <你的模块>.split_and_share_reward` 注入（**不要**同时开 `--group-rm`，理由见 4.1.3-b 的坑）。

**需要观察的现象 / 预期结果**：

- 返回的 K 条 `Sample` 的 `rollout_id` 全部相等 → 通过 `_validate_rollout_id_annotated`（[slime/ray/rollout.py:918-927](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L918-L927)）。
- 训练 step 数由 rollout 数（而非样本数）决定：`num_steps = rollout_batch_size * n_samples_per_prompt // global_batch_size`，不会因 K=3 而翻 3 倍。
- `rollout_mask_sums` 把这 K 段的 mask 求和成一个分母，per-rollout-mean 归约把这条 rollout 计一次。
- 如果故意把 `s.rollout_id = rollout_id` 注释掉（让每段走各自 `index`），重新跑，应在训练装配阶段触发校验断言报错——以此反向验证契约。

**待本地验证**：上面骨架里的「均分切段」是教学占位，真实切分逻辑需按你的任务边界实现；段内 `tokens` / `loss_mask` / `rollout_log_probs` 长度必须严格对齐（`loss_mask` 长度 == `response_length`，见 [slime/utils/types.py:418-420](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L418-L420)），否则会在 `_validate_response_metadata_lengths` 报错。完整跑通需多卡 GPU 与数据集。

## 6. 本讲小结

- **fan-out 的硬契约**：一次 rollout 执行产出多条 `Sample` 时，所有兄弟必须共享同一个 `rollout_id`（[types.py:99-106](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L99-L106)），由 `_validate_rollout_id_annotated` 在 compact 形状（depth≥2）下强制校验。
- **不被放大的地基**是「按 `rollout_id` 聚合」这条链路：`rollout_mask_sums` 按 rollout 预算分母 → `build_dp_schedule` 按 rollout 数切 step → `get_sum_of_sample_mean` 用整 rollout 的 mask 作分母求一次 token 加权平均。
- **step 数按 rollout 计数**：`global_batch_size` 单位是 rollout 数不是样本数，fan-out 不会膨胀训练 step。
- **奖励分摊有三条都正确的约定**（前提都是共享 `rollout_id`）：reward/K（手写推荐）、每段全额 reward（`TrajectoryManager` 默认）、按角色加权（`multi_agent`），它们与「计一次」的归约机制正交。
- **`TrajectoryManager` 是自动 fan-out**：把多轮会话树按 drift/fork 自动切成带 `loss_mask` 的训练分段，并在每段盖上同一 `rollout_id`、写好 reward。
- **工程要点**：`rollout_id` 必须在 fan-out 函数开头提前捕获（避免被循环变量遮蔽），且**每个** return 点都要收口盖印（`multi_agent` 的 `_emit`）。

## 7. 下一步学习建议

- **u7-l4 流式、全异步与部分回滚 rollout**：fan-out 让「一次 rollout 产 K 段」，而长尾场景下不同样本耗时差异大；流式 / 全异步 / partial-rollout 是解决「fan-out 后整批被最慢样本拖住」的下一步，建议接着读 `slime/rollout/fully_async_rollout.py` 与 `sglang_streaming_rollout.py`。
- **重读 u4-4 / u6-4**：本讲只讲了「损失如何按 rollout 归约」，但 reward 怎么变成 advantage（GRPO 组归一化）会显著影响 fan-out 的效果；建议结合 `_post_process_rewards`（[slime/ray/rollout.py:685](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L685)）与 `grpo_normalize_by_group_index`（[slime/rollout/_fanout_test_helpers.py:76-115](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/_fanout_test_helpers.py#L76-L115)）理解 fan-out 下 GRPO 归一化为何需要按 `group_index` 重写。
- **通读旗舰示例 `examples/coding_agent_rl`**：它是 fan-out + adapter + `TrajectoryManager` + 沙箱打分的完整生产级落地，读懂 `generate.py` 与 `slime/agent/adapters` 能把本讲的三类机制串成一条真实链路。
