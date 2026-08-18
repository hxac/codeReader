# DFlash 配置化实现与三种算法对比

## 1. 本讲目标

学完本讲，你应该能够：

1. **指出 DFlash 是 DSpark 实现的简化配置而非独立模型代码**：在仓库里找到证据——`config/dflash/` 下的配置文件与 `config/dspark/` 逐行几乎相同，且 `trainer_cls` 用的是**同一个** `Qwen3DSparkTrainer`。
2. 说出关闭 DSpark 三个可拆卸部件（markov 头、置信度头、L1 蒸馏）的**三个配置开关**，并追踪每个开关从 config 字段到模型结构、再到损失项的完整传播路径。
3. 用一张表横向对比 DSpark / DFlash / Eagle3 在**提议形态、头结构、损失函数、训练前向次数**上的差异。
4. 根据「是否需要置信度调度、显存预算、追求接受率还是追求训练简单」选出合适的 config，并说明理由。

本讲是第 5 单元收官，也是从「读懂一种算法」过渡到「理解算法族谱与选型」的桥梁。

## 2. 前置知识

本讲不再重复算法内部细节，只搬运前面讲义已建立的结论：

- **配置即代码（u1-l4）**：`config/` 下每个 `.py` 文件被 `load_config` 动态执行，顶层名字（包括 `trainer_cls` 这样的**类对象**）被收集成配置；`--opts "train.lr=3e-4"` 可在命令行按点路径覆盖已有键。配置里能放类，是本讲「换算法 = 换配置」的前提。
- **DSpark 的三个可拆卸部件**：
  - **markov 头（u4-l3）**：块式并行产出的整块 logits 缺少块内 token 级依赖，`VanillaMarkov` 用低秩双线性偏置（查表 `markov_w1` + 投影 `markov_w2`）在冻结 `lm_head` 的 logits 上加一阶修正；
  - **置信度头（u4-l4）**：`AcceptRatePredictor` 以 detach 后的接受率为 BCE 目标，学会预测「这块 token 会被接受几个」，是评估侧置信度调度（提前截断低置信块）的基础；
  - **L1 蒸馏损失（u4-l4）**：最小化草稿/目标分布的 L1 距离，等价于最大化期望接受概率（\( a = 1 - \mathrm{TV}(p,q) \)，\( L1 = 2(1-a) \)）。
- **Eagle3（u5-l1、u5-l2）**：拼接 5 层目标隐状态为输入特征，草稿主干仅 1 层，训练时链式展开 `ttt_length=7` 步（TTT，train-time test），损失是对教师分布的软交叉熵，按 `step_loss_decay=0.8` 几何衰减加权。
- **一个观察角度**：在 DeepSpec 里，「算法」= **模型结构开关 + 损失权重组合**。DSpark 是全量开启，DFlash 是关掉三个部件，Eagle3 则换了一条代码线（不同的 trainer、不同的损失文件）。

还需要一点参数量直觉：Qwen3-4B 的词表大小为 151936。`markov_w1`（`Embedding(151936, 256)`）与 `markov_w2`（`Linear(256, 151936)`）合计约 \( 2 \times 151936 \times 256 \approx 7780 \) 万参数，而且这些参数**可训练**——按 u3-l4 所讲，BF16Optimizer 会为每个可训练参数维护 fp32 主权重与 Adam 动量，优化器状态的放大倍数远大于参数本身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | DSpark 基准配置：三个部件全开 |
| [config/dflash/dflash_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py) | DFlash 配置：与 DSpark 逐行 diff，只改三个开关 |
| [config/eagle3/eagle3_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py) | Eagle3 配置：另一套 model 字段与另一个 trainer |
| [config/dflash/dflash_gemma4_12b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_gemma4_12b.py) | 证明 DFlash 模式跨模型族成立（Gemma4 版） |
| [deepspec/modeling/dspark/qwen3/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py) | `build_draft_config`：alpha/markov_rank 如何变成结构开关 |
| [deepspec/modeling/dspark/markov_head.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py) | `build_markov_head`：rank=0 时返回 None |
| [deepspec/modeling/dspark/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py) | 条件构建 markov/置信度头；forward/采样中的分支 |
| [deepspec/modeling/dspark/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py) | `compute_dspark_loss`：alpha 组合与各分项的跳过逻辑 |
| [deepspec/modeling/eagle3/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py) | `compute_eagle3_loss`：逐步软 CE + 几何衰减 |
| [deepspec/trainer/dspark_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py) | `Qwen3DSparkTrainer`（DSpark 与 DFlash 共用）与 `run_batch` |
| [eval.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py) | `EVALUATORS` 分发表：DFlash 与 DSpark 共享同一评估器 |
| [README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md) | Released Checkpoints 表与三算法定位 |

## 4. 核心概念与源码讲解

### 4.1 DFlash = 关掉 markov / 置信度 / L1 的 DSpark

#### 4.1.1 概念说明

DFlash 是一篇独立论文（arXiv 2602.06036，见 README 的 Supported Algorithms 与致谢部分）提出的**块式草稿模型**：一次草稿前向并行产出一整块 token，块内各 token 相互独立采样，训练用纯交叉熵。

在 DeepSpec 仓库里，「DFlash」**没有自己的 modeling 目录、没有自己的 trainer 类、没有自己的 loss 函数**。它的全部存在形式是 `config/dflash/` 下 4 份配置文件，而每份文件 import 的正是 DSpark 的训练器：

- `config/dflash/dflash_qwen3_4b.py` 第 3 行：`from deepspec.trainer import Qwen3DSparkTrainer`。

换句话说，DSpark 的实现被设计成**三个部件全部可拆卸**，DFlash 就是「全拆掉」的那个配置点。这是比继承更彻底的复用：算法差异被压进了配置字段。

三个开关分别是：

| 开关 | DSpark 的值 | DFlash 的值 | 拆掉的是什么 |
| --- | --- | --- | --- |
| `markov_rank` | 256 | 0 | markov 头（块内 token 级依赖） |
| `confidence_head_alpha` | 1.0 | 0.0 | 置信度头（AcceptRatePredictor） |
| `ce_loss_alpha` / `l1_loss_alpha` | 0.1 / 0.9 | 1.0 / 0.0 | L1 蒸馏项（损失退化为纯 CE） |

注意第三个开关是**两个**字段联动：只把 `l1_loss_alpha` 置 0 而不把 `ce_loss_alpha` 提到 1.0，总损失会整体缩小一个数量级，梯度尺度随之改变，不再是「DFlash」语义。

#### 4.1.2 核心流程

三个开关各自沿一条独立路径从 config 文件传播到运行时行为：

```text
config/dflash/dflash_qwen3_4b.py
├── model.markov_rank = 0
│     └─ trainer._build_draft_model(model_args=cfg.model)
│          └─ build_draft_config：markov_rank=0 → 不再要求 markov_head_type
│               └─ Qwen3DSparkModel.__init__
│                    └─ build_markov_head(config) 返回 None
│                         ├─ forward：跳过 apply_block_logits（draft_logits 即裸 lm_head 输出）
│                         └─ 采样：sample_draft_block 直接对整块独立采样
│
├── model.confidence_head_alpha = 0.0
│     └─ build_draft_config：enable_confidence_head = (alpha > 0) = False
│          ├─ 模型侧：confidence_head 不构建，confidence_pred 恒为 None
│          └─ 损失侧：has_confidence = (confidence_pred is not None) = False
│                        → BCE 项与置信度指标全部跳过
│
└── model.ce_loss_alpha = 1.0, model.l1_loss_alpha = 0.0
      └─ trainer.run_batch 把三个 alpha 原样传给 compute_dspark_loss
           ├─ l1_loss_alpha=0 → 不计算 L1 项，也不再要求 aligned_target_logits
           └─ _build_loss：总损失 = 1.0 × CE + 0 × L1 + 0 × BCE = 纯位置衰减 CE
```

一个容易忽略的细节：**`loss_decay_gamma=4.0` 在 DFlash 里被保留**。位置衰减 \( e^{-t/\gamma} \) 不是 L1 蒸馏的附属品，而是 CE 自己的加权方式（见 4.2.3），所以 DFlash 的准确描述是「**带槽位指数衰减的纯 CE**」，不是普通 SFT 损失。

另一条对评估很重要的推论：`build_draft_config` 无条件写入 `architectures = ["Qwen3DSparkModel"]`，与开关取值无关。因此 DFlash 训出的 checkpoint 在 `eval.py` 里与 DSpark 走**同一个** `Qwen3DSparkEvaluator`（见 4.3.3），评估代码路径完全共享，只是运行到「采样块 token」时因 `markov_head is None` 走独立采样分支。

#### 4.1.3 源码精读

**证据一：两份配置逐行 diff。** 先看 DSpark 的 model 字段：

[config/dspark/dspark_qwen3_4b.py:L10-L30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L30)
> 定义 DSpark 的全部模型超参：`block_size=7`、`num_draft_layers=5`、`target_layer_ids=[1,9,17,25,33]`、`num_anchors=512`，以及 markov 头（`markov_rank=256`、`markov_head_type='vanilla'`）、置信度头（`confidence_head_alpha=1.0`、`confidence_head_with_markov=True`）和损失权重（`ce_loss_alpha=0.1`、`l1_loss_alpha=0.9`、`loss_decay_gamma=4.0`）。

再看 DFlash 的 model 字段：

[config/dflash/dflash_qwen3_4b.py:L10-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py#L10-L28)
> 与上面逐行相同的骨架（`block_size`、`num_draft_layers`、`target_layer_ids`、`mask_token_id`、`num_anchors` 全部一致），只把三处改成关闭态：`markov_rank=0`（并整体删掉 `markov_head_type` 键）、`confidence_head_alpha=0.0`（删掉 `confidence_head_with_markov`）、`ce_loss_alpha=1.0, l1_loss_alpha=0.0`（注释明确写着 "CE-only loss"），`loss_decay_gamma=4.0` 保留。

两份文件的 `train` 字典则**完全一致**（同一个 `trainer_cls=Qwen3DSparkTrainer`、同样的 lr 与 batch 设置），见 [config/dflash/dflash_qwen3_4b.py:L30-L43](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py#L30-L43)。同一模式也复制到了 Gemma4：[config/dflash/dflash_gemma4_12b.py:L19-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_gemma4_12b.py#L19-L28)，三个开关的写法一模一样。

**证据二：开关 1（markov）——rank=0 时头根本不构建。**

[deepspec/modeling/dspark/qwen3/config.py:L30-L35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L30-L35)
> `build_draft_config` 校验 `markov_rank >= 0`，并且**只有** `markov_rank > 0` 时才断言 `markov_head_type` 必须存在。这解释了为什么 DFlash 配置可以安全地删掉 `markov_head_type` 键。

[deepspec/modeling/dspark/markov_head.py:L287-L298](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L287-L298)
> `build_markov_head` 在 `markov_rank == 0` 时直接返回 `None`，否则按 `markov_head_type` 分派到 `VanillaMarkov` / `GatedMarkovHead` / `RNNHead`。

[deepspec/modeling/dspark/qwen3/modeling.py:L488-L493](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L488-L493)
> forward 中只有 `markov_head is not None` 才调用 `apply_block_logits` 修正整块 logits。DFlash 的 `draft_logits` 因此就是冻结 `lm_head` 的裸输出。

[deepspec/modeling/dspark/qwen3/modeling.py:L326-L333](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L326-L333)
> 评估侧提议采样：`markov_head is None` 时退化为 `sample_tokens(base_logits, temperature)`——整块 token **相互独立**采样；DSpark 则走 `sample_block_tokens` 逐槽位「修正→采样→推进前驱」（u4-l3）。

**证据三：开关 2（置信度头）——alpha 同时控制结构与损失。**

[deepspec/modeling/dspark/qwen3/config.py:L22-L29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L22-L29)
> `enable_confidence_head = confidence_head_alpha > 0.0`——损失权重兼做结构开关；alpha=0 时也不要求 `confidence_head_with_markov` 键，所以 DFlash 又可以少写一个键。

[deepspec/modeling/dspark/qwen3/modeling.py:L251-L267](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L251-L267)
> `self.markov_head = build_markov_head(config)` 之后，置信度头仅在 `enable_confidence_head` 时构建：输入维度为 `hidden_size`（DSpark 因 `confidence_head_with_markov=True` 还要拼上 `markov_rank` 维的 markov 嵌入）。DFlash 下 `self.confidence_head` 保持 `None`。

[deepspec/modeling/dspark/qwen3/modeling.py:L504-L516](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L504-L516)
> forward 输出 `confidence_pred`：`confidence_head is None` 时恒为 `None`，DSpark 则按是否复用 markov 嵌入拼特征后过 `AcceptRatePredictor`。

[deepspec/modeling/dspark/loss.py:L146-L163](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L146-L163)
> 损失侧以 `has_confidence = outputs.confidence_pred is not None` 判断：DFlash 下 BCE 项与 `confidence_abs_error` 等全部指标直接跳过（保持 0）。

**证据四：开关 3（损失权重）——alpha 传参链与纯 CE 退化。**

[deepspec/trainer/dspark_trainer.py:L25-L39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L25-L39)
> `Qwen3DSparkTrainer.run_batch`（DSpark 与 DFlash 共用同一个类！）把 `self.args.model` 里的 `loss_decay_gamma`、`ce_loss_alpha`、`l1_loss_alpha`、`confidence_head_alpha` 原样传给 `compute_dspark_loss`。算法差异全部来自这几个 config 字段。

[deepspec/modeling/dspark/loss.py:L121-L132](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L121-L132)
> `l1_loss_alpha <= 0` 时不再断言 `aligned_target_logits` 存在，L1 分子分母置 0——DFlash 下即使 forward 仍产出对齐教师 logits（只要 batch 带 `target_last_hidden_states` 就会算，见 [modeling.py:L447-L465](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L447-L465)），损失也完全不使用它。

[deepspec/modeling/dspark/loss.py:L237-L252](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L237-L252)
> 总损失的加权组合：\( \mathcal{L} = \alpha_{ce}\,\overline{CE} + \alpha_{l1}\,\overline{L1} + \alpha_{conf}\,\overline{BCE} \)，再乘 `world_size` 抵消 FSDP 梯度平均（u3-l2、u4-l4）。代入 DFlash 的 (1.0, 0.0, 0.0) 即得纯 CE；代入 DSpark 的 (0.1, 0.9, 1.0) 即「蒸馏为纲」的组合。

[deepspec/modeling/dspark/loss.py:L25-L37](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L25-L37)
> `_build_loss_weight_mask` 把 `eval_mask` 乘上槽位衰减 \( e^{-t/\gamma} \)。CE 与 L1 共用这份权重，所以 DFlash 保留 `loss_decay_gamma=4.0` 意味着它的 CE 仍是「块首重、块尾轻」的加权 CE。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「DFlash 与 DSpark 的配置差异恰好只有三个开关」，并验证配置层面对开关的合法性约束。

**操作步骤**（本实践不需要 GPU）：

1. 在仓库根目录执行逐行 diff：

   ```bash
   git diff --no-index config/dspark/dspark_qwen3_4b.py config/dflash/dflash_qwen3_4b.py
   ```

   预期输出只包含：`exp_name` 一行、markov 两行（删 `markov_rank=256`/`markov_head_type` 换成 `markov_rank=0`）、置信度两行、损失两行，以及注释差异。`train`、`logging`、`data`、`finalize_cfg` 应零差异。

2. 用 `load_config` 以程序方式对比三份配置的 model 字段（**示例代码**，需在仓库根目录、已 `pip install -r requirements.txt` 的环境中运行，因为执行配置文件会 import `deepspec.trainer`，进而 import torch）：

   ```python
   # compare_configs.py —— 示例代码
   from deepspec.utils.config import load_config

   paths = {
       "dspark": "config/dspark/dspark_qwen3_4b.py",
       "dflash": "config/dflash/dflash_qwen3_4b.py",
       "eagle3": "config/eagle3/eagle3_qwen3_4b.py",
   }
   cfgs = {name: load_config(path) for name, path in paths.items()}

   keys = ("markov_rank", "confidence_head_alpha", "ce_loss_alpha",
           "l1_loss_alpha", "loss_decay_gamma")
   for k in keys:
       row = {n: cfgs[n]["model"].get(k, "<无此键>") for n in paths}
       print(f"{k:24s} {row}")
   ```

3. 做一个反向实验：把 `dflash_qwen3_4b.py` 复制为 `/tmp/my_dflash.py`，仅把 `markov_rank=0` 改成 `markov_rank=256`（不补 `markov_head_type`），再 `load_config("/tmp/my_dflash.py")` 不会报错（它只执行文件），但如果继续走训练入口，`build_draft_config` 的断言会拦下。

**需要观察的现象**：

- diff 的输出行数非常少（约 10 行以内），`trainer_cls` 一行都没出现——两份配置用的是同一个类。
- 脚本打印中，`dflash` 行显示 `markov_rank=0`、`confidence_head_alpha=0.0`、`ce_loss_alpha=1.0`、`l1_loss_alpha=0.0`，`loss_decay_gamma` 与 dspark 相同；`eagle3` 行对这五个键多数显示「<无此键>」。

**预期结果**：第 3 步若真的启动训练，会在 `build_draft_config` 处抛出 `AssertionError: markov_head_type must be provided when markov_rank > 0.`（对应 [config.py:L32-L35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L32-L35) 的断言）。断言触发的完整启动过程**待本地验证**（本实践只需走到 diff 与 load_config 即可完成）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 DFlash 配置的 `confidence_head_alpha` 改成 0.5、其余不动，模型结构会发生什么变化？

**答案**：`enable_confidence_head = 0.5 > 0` 变为 True，但此时 [config.py:L25-L29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L25-L29) 会断言 `confidence_head_with_markov` 必须提供——而 DFlash 配置里没有这个键，训练在启动时失败。要真正打开置信度头，需要同时补 `confidence_head_with_markov` 键；且若设为 True，还会因 `markov_rank=0` 触发 [modeling.py:L259-L260](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L259-L260) 的 `assert self.markov_head is not None` 失败。三个开关并非完全独立，配置组合受断言网格约束。

**练习 2**：DFlash 关掉了 L1 蒸馏，为什么 `aligned_target_logits` 这个张量仍然会被算出来？这是浪费吗？

**答案**：`aligned_target_logits` 的计算条件是 batch 提供 `target_last_hidden_states`（[modeling.py:L447-L465](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L447-L465)），而 target cache 协议总会存最终隐状态（u2-l4），所以它仍在 `DSparkForwardOutput` 里。它复用冻结的 `lm_head`，无梯度、无额外参数，代价是一次矩阵乘；同时 `accept_rate@k` 等训练指标仍靠它计算。所以不是纯粹浪费，但确实说明「DFlash 的 forward 输出合同 = DSpark 的输出合同」，拆卸只发生在损失与采样侧。

**练习 3**：DFlash 的 block_size=7 与 Eagle3 的 ttt_length=7 都对应「一次提议 7 个 token」，两者在**训练时**的前向次数有何区别？

**答案**：DFlash/DSpark 一次 `run_batch` 只做**一次**草稿前向，块内 7 个监督位并行产出（u4-l1 的非因果块掩码）；Eagle3 在 `compute_eagle3_loss` 里循环 `ttt_length` 次链式前向（见 4.2.3），每次吃上一步的隐状态与预测 token。同样是 7 个监督位，前者是空间并行，后者是时间展开。

### 4.2 三算法损失与结构对比

#### 4.2.1 概念说明

比较三种算法需要一个统一坐标系。对草稿模型而言，最本质的三个维度是：

1. **提议形态**：推理时草稿怎么产出候选 token——块式并行（一次 forward 一整块）还是链式逐 token（一步一步走）。
2. **头结构**：`lm_head` 之外还有哪些可训练部件——markov 头恢复块内依赖，置信度头预测接受率；Eagle3 什么都不加，因为它链式生成天然就是 token 级自回归。
3. **损失**：监督信号是什么——硬标签 CE、软标签蒸馏，还是多任务组合。

DSpark 与 DFlash 共享提议形态与主干（5 层、双源 K/V），差异全在 2、3 两维；Eagle3 三个维度都不同。**损失函数是三者差异最集中的地方**，也是训练目标「像不像目标模型分布」的直接表达。

#### 4.2.2 核心流程

三种损失的数学形式（加权方式见 4.1.3 的 `_build_loss` 与 4.2.3 的 `compute_eagle3_loss`）：

**DSpark（默认权重 0.1 / 0.9 / 1.0）**：

\[
\mathcal{L}_{\text{DSpark}}
= 0.1 \cdot \overline{CE}_{w} + 0.9 \cdot \overline{L1}_{w} + 1.0 \cdot \overline{BCE}_{w},
\qquad w_t = m_t \cdot e^{-t/\gamma},\ \gamma = 4
\]

其中 \( \overline{CE}_{w} \)、\( \overline{L1}_{w} \)、\( \overline{BCE}_{w} \) 都是按 \( w_t \) 加权、分母跨 rank all_reduce 后的全局均值（u4-l4）。

**DFlash（1.0 / 0.0 / 0.0）**：

\[
\mathcal{L}_{\text{DFlash}} = \overline{CE}_{w}
\]

纯加权 CE。注意 \( w_t \) 的衰减仍在，且分母的跨 rank 归一化机制不变——损失系统里只有「项的取舍」变了，归一化框架原样保留。

**Eagle3（软 CE + 几何衰减）**：

\[
\mathcal{L}_{\text{Eagle3}}
= \sum_{s=0}^{ttt-1} \beta^{s} \cdot \ell_s,
\qquad \beta = 0.8,\ ttt = 7
\]

其中 \( \ell_s \) 是第 \( s \) 个链步上草稿分布对**教师分布**（目标模型 next-token 概率，经 `_build_padded_next_token_target_probs` 展开）的软交叉熵，按 `local_mean` 方式归一（每序列等权）。注意两个衰减的区别：DSpark/DFlash 的 \( e^{-t/\gamma} \) 衰减在**块内槽位**维度（同一个 block 的 7 个位置），Eagle3 的 \( \beta^s \) 衰减在**链步**维度（第 1 步预测到第 7 步预测）——两者都在表达「越靠后的预测越难、权重越低」。

推理形态对比：

| | DSpark | DFlash | Eagle3 |
| --- | --- | --- | --- |
| 提议形态 | 块式并行（1 次草稿 forward 出 7 token） | 块式并行（同左） | 链式逐 token（7 次逐步采样） |
| 块内依赖 | markov 头一阶修正 | 无（独立采样） | 天然自回归 |
| 草稿层数 | 5 | 5 | 1 |
| 置信度头 | 有 | 无 | 无 |

#### 4.2.3 源码精读

**DSpark/DFlash 共用的加权组合：**

[deepspec/modeling/dspark/loss.py:L248-L252](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L248-L252)
> 三个归一化分项按 alpha 线性加权后乘 `world_size`。把 DSpark 的 (0.1, 0.9, 1.0) 与 DFlash 的 (1.0, 0.0, 0.0) 分别代入这一行，就是两种算法的完整损失定义——同一行代码，两组配置。

**Eagle3 的逐步软 CE 与链式衰减：**

[deepspec/modeling/eagle3/loss.py:L402-L443](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L402-L443)
> `compute_eagle3_loss` 的核心循环：每个链步用 `FusedLogSoftmaxLoss.apply` 对教师分布算软交叉熵（Triton 融合前后向省显存，u5-l2），`step_weight = step_loss_decay ** step_idx` 即 \( 0.8^s \) 权重，逐步累加；`current_input_ids` 每步右移一位，实现「吃自己上一步预测」的 teacher-forcing 链。循环体内一次 `model(...)` 就是一次草稿前向——7 步链共 7 次前向。

[deepspec/modeling/eagle3/loss.py:L354-L360](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L354-L360)
> 函数签名只接受 `ttt_length` 与 `step_loss_decay` 两个超参——Eagle3 的损失没有任何 alpha 组合，天然是单一软蒸馏目标，与 DSpark 的多 alpha 空间形成对照。

**三份 config 的 model 字段并排：**

[config/eagle3/eagle3_qwen3_4b.py:L10-L16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L10-L16)
> Eagle3 的 model 字段：`target_layer_ids=[1,9,17,25,33]`（必须恰好 5 层，u5-l1 的协议闸门）、`ttt_length=7`、`step_loss_decay=0.8`、`draft_num_hidden_layers=1`。没有 block/mask/anchor/markov/confidence 任何字段——字段集合本身就是算法身份的声明。

对照 [config/dspark/dspark_qwen3_4b.py:L10-L30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L30)（4.1.3 已精读）可以看出：DSpark 与 Eagle3 的 model 字段几乎不相交，唯一共享的是目标模型身份与 `target_layer_ids`。

**train 字段的隐性差异：**

[config/eagle3/eagle3_qwen3_4b.py:L18-L31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L18-L31)
> Eagle3 用 `Qwen3Eagle3Trainer`，且 `torch_compile=False`；而 [config/dspark/dspark_qwen3_4b.py:L43-L44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L43-L44) 中 DSpark/DFlash 为 `torch_compile=True`。另注意 Eagle3 的 `seed = 0`（[L8](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L8)），DSpark/DFlash 为 42。复现实验时这些「配置外围」差异同样要记录。

#### 4.2.4 代码实践

**实践目标**：用数值代入验证「同一行加权代码、两组配置产生两种损失」，并算一算 Eagle3 的衰减权重分布。

**操作步骤**（**示例代码**，CPU 即可）：

1. 构造假分项，直接调用 `_build_loss`：

   ```python
   # loss_ablation.py —— 示例代码（未启动分布式，world_size 按 1 传）
   import torch
   from deepspec.modeling.dspark.loss import _build_loss

   terms = {  # 假设全局统计已归一前的分子/分母
       "ce_loss_num": torch.tensor(2.0),  "ce_loss_den": torch.tensor(1.0),
       "l1_loss_num": torch.tensor(0.8),  "l1_loss_den": torch.tensor(1.0),
       "confidence_loss_num": torch.tensor(0.5), "confidence_loss_den": torch.tensor(1.0),
   }
   for name, (a_ce, a_l1, a_conf) in {
       "DSpark  (0.1/0.9/1.0)": (0.1, 0.9, 1.0),
       "DFlash (1.0/0.0/0.0)": (1.0, 0.0, 0.0),
   }.items():
       loss = _build_loss(
           loss_terms=terms, global_denominators=terms,
           ce_loss_alpha=a_ce, l1_loss_alpha=a_l1,
           confidence_head_alpha=a_conf, has_confidence=True, world_size=1,
       )
       print(f"{name}: {loss.item():.4f}")
   ```

2. 手算 Eagle3 的链步权重并验证快速衰减：

   ```python
   print([round(0.8 ** s, 4) for s in range(7)])  # 第 7 步权重只有 0.2621
   ```

**需要观察的现象**：

- DSpark 行输出约 \( 0.1 \times 2.0 + 0.9 \times 0.8 + 1.0 \times 0.5 = 1.42 \)；
- DFlash 行输出约 2.0——注意纯 CE 的损失数值**反而更大**，因为它丢掉了数值更小的蒸馏项，这不是「更难训练」的证据，只说明损失尺度不可跨算法比较；
- Eagle3 权重序列 \([1.0, 0.8, 0.64, 0.512, 0.4096, 0.3277, 0.2621]\)。

**预期结果**：以上数值均可手工复核。运行第 1 步需能 import deepspec（依赖 torch）；`_build_loss` 是模块内私有函数，若 import 路径有变以实际文件为准。完整可运行性**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：三个算法的损失里，哪个直接优化「期望接受长度」的代理量？哪个只优化对数似然？

**答案**：DSpark 的 L1 项直接最小化 \( L1 = 2(1-a) \)（\( a \) 为逐 token 接受概率，u4-l4），是最贴近投机解码收益的代理；Eagle3 的软 CE 让草稿分布逼近教师分布，间接提高接受概率，但目标本身是分布匹配；DFlash 的硬标签 CE 只优化真值 token 的对数似然，与接受率的关系最间接。

**练习 2**：同样产出 7 个监督位，DSpark 一次前向、Eagle3 七次前向。训练显存上各有什么代价？

**答案**：DSpark/DFlash 需要在显存里同时持有整块的 `draft_logits` 与 `aligned_target_logits`（形状约为 \( B \times 512 \times 7 \times V \)，\( V \approx 15 \) 万，u4-l2），softmax 中间量也大；Eagle3 每步激活小，但链式循环保留了 7 步的计算图与逐步 KV cache（`DynamicCache`），单层主干使参数侧开销最小。两者显存压力的来源不同：前者是「宽」（一次算很多位置），后者是「长」（图更深）。

**练习 3**：DFlash 与 DSpark 用同一个 trainer 类、同一个损失函数文件，那么 TensorBoard 里两者的指标集合有何不同？

**答案**：DSpark 会多出 `l1_loss`、`confidence_loss`、`confidence_abs_error`、`confidence_bias`、`confidence_cumprod_bias` 等指标（它们的记录被 `if l1_loss_den > 0` / `if has_confidence` 守卫，见 [loss.py:L300-L323](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L300-L323)）；DFlash 只有 `ce_loss`、`accept_rate@k`、`tau_probabilistic` 等公共项。指标集合是判断「实际跑了哪种算法」的旁证。

### 4.3 config 选择：按需挑选与影响推断

#### 4.3.1 概念说明

README 的 Released Checkpoints 表给出了三算法 × 四目标模型共 12 个 checkpoint，每个都对应 `config/` 下同名配置的直接产物。选 config 时真正要问的三个问题：

1. **要不要置信度调度？** 只有 DSpark 训练 `AcceptRatePredictor`，评估时才能用置信度阈值提前截断低置信块（u6-l5 详述）。要玩 confidence-scheduled speculative decoding，只能选 DSpark。
2. **显存与训练预算？** DFlash 比 DSpark 少约 7780 万可训练参数（markov 头）加一个小 MLP（置信度头），按 u3-l4 的 BF16Optimizer 机制，优化器状态的节省是参数节省的数倍；Eagle3 主干只有 1 层，可训练参数最少。
3. **追求接受率还是追求简单？** L1 蒸馏 + markov 头 + 置信度头是三个「锦上添花」部件：DSpark 全开收益最高但超参最多（三个 alpha、gamma、rank）；DFlash 目标简单、梯度行为接近常规 SFT，适合作为基线或二次开发的起点。

#### 4.3.2 核心流程

选型决策（伪代码）：

```text
if 需要 confidence-scheduled 投机解码 or 追求最高接受率:
    选 DSpark   # 三部件全开；超参最多，调参成本最高
elif 想以块式草稿为基线做二次开发 or 可训练参数预算紧张:
    选 DFlash   # 与 DSpark 共用代码线，随时可改配置升级回 DSpark
elif 草稿侧显存极紧 or 想要链式逐 token 提议（与 verify 更同构）:
    选 Eagle3   # 单层草稿；训练 7 次链式前向，torch_compile=False
```

挑选后按目标模型定位文件：`config/<算法>/<算法>_<模型族>_<规模>.py`，如 `dspark_gemma4_12b.py`、`eagle3_qwen3_8b.py`；再用 `--opts` 注入 `data.target_cache_path` 等运行期路径（u1-l2）。

#### 4.3.3 源码精读

**发布 checkpoint 与配置一一对应：**

[README.md:L53-L62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L53-L62)
> Released Checkpoints 表：每行一个算法（Eagle3 / DFlash / DSpark），每列一个目标模型。每个 checkpoint 都是 `config/` 下对应配置的直接输出——例如 `deepseek-ai/dflash_qwen3_4b_block7` 对应 `config/dflash/dflash_qwen3_4b.py`（`exp_name = "dflash_block7_qwen3_4b"`）。名称里的 `block7`/`ttt7` 分别标记块大小与链长。

[README.md:L67-L69](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L67-L69)
> Supported Algorithms 一节列出三篇论文的 arXiv 编号；Acknowledgements 进一步说明 DFlash 的设计来自 z-lab/dflash 仓库、Eagle3 代码改编自 SpecForge——这也解释了为什么 Eagle3 在本仓库是一条独立代码线而 DFlash 不是。

**评估侧的分发：DFlash 与 DSpark 无缝共享：**

[eval.py:L10-L15](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L15)
> `EVALUATORS` 只有四个键（两个模型族 × 两种算法）。DFlash 的 checkpoint `architectures` 仍是 `"Qwen3DSparkModel"`（由 [config.py:L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L38) 无条件写入，与开关无关），所以评估 DFlash 时实例化的就是 `Qwen3DSparkEvaluator`；运行差异由模型内部的 `markov_head is None` / `confidence_head is None` 分支自然消化。

**配置外围差异清单（容易被忽略但影响复现）：**

| 字段 | dspark_qwen3_4b | dflash_qwen3_4b | eagle3_qwen3_4b |
| --- | --- | --- | --- |
| `trainer_cls` | `Qwen3DSparkTrainer` | `Qwen3DSparkTrainer`（同一个类） | `Qwen3Eagle3Trainer` |
| `train.torch_compile` | True | True | False |
| `seed` | 42 | 42 | 0 |
| `data.chat_template` | "qwen" | "qwen" | "qwen" |
| 其余 train/logging/data | 相同 | 相同 | 相同 |

见 [config/dspark/dspark_qwen3_4b.py:L32-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L32-L45)、[config/dflash/dflash_qwen3_4b.py:L30-L43](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py#L30-L43)、[config/eagle3/eagle3_qwen3_4b.py:L18-L31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L18-L31)。另注意 Gemma4 侧的 DSpark/DFlash 配置 `torch_compile=False`（[dflash_gemma4_12b.py:L43](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_gemma4_12b.py#L43)），`torch_compile` 是按「算法 × 模型族」组合调过的，不是全局默认值。

#### 4.3.4 代码实践

**实践目标**：为「从三种算法中选一个」建立一份可复用的选型依据表。

**操作步骤**：

1. 阅读 [README.md:L53-L69](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L53-L69)，把 12 个 checkpoint 名称拆解成「算法_模型_提议长度」三元组，确认每个都能在 `config/` 找到同名文件。
2. 针对下面三个场景各选一个 config 并写一句理由：
   - 场景 A：你复现论文的 DSpark 评测，想先跑一个「结构相同但损失最简单」的对照基线；
   - 场景 B：你只有单机 8 卡，想在 Qwen3-4B 上把草稿侧显存压到最低；
   - 场景 C：你想研究「草稿自知之明」（低置信时少提议）这一方向。
3. 把你的选择与 4.3.2 的决策伪代码互相校验。

**需要观察的现象 / 预期结果**：场景 A 应选 `dflash_qwen3_4b.py`（同主干、同 trainer，只有损失与两个头不同，是最干净的消融基线）；场景 B 倾向 `eagle3_qwen3_4b.py`（单层草稿参数最少，但注意训练是 7 次链式前向，墙钟时间不一定占优）；场景 C 只能选 `dspark_*.py`（唯一带置信度头的算法）。若你的答案与此不符，检查是否漏看了某个结构开关。

#### 4.3.5 小练习与答案

**练习 1**：评估脚本 `eval.py` 加载 `deepseek-ai/dflash_qwen3_4b_block7` 时，`EVALUATORS` 会命中哪个键？为什么不需要为 DFlash 单独注册评估器？

**答案**：命中 `"Qwen3DSparkModel"`。因为 `build_draft_config` 深拷贝目标 config 后**无条件**写 `architectures = ["Qwen3DSparkModel"]`（[config.py:L37-L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L37-L38)），DFlash 的开关只影响模型内部哪些子模块为 None，不影响架构名。评估器内所有与 markov/置信度相关的逻辑都有 `is None` 分支兜底。

**练习 2**：为什么说「DFlash 是理解 DSpark 各部件贡献的天然消融实验」？

**答案**：两者共用同一个 trainer、同一个模型类、同一个损失函数与同一份 `train` 配置（学习率、batch、epoch 全同），差异被压缩到三个配置开关。因此 README 表中 DFlash 与 DSpark 同列 checkpoint 的评测差距，可以近似归因于「markov 头 + 置信度头 + L1 蒸馏」这组部件的净贡献（当然严格归因还需逐个开关地拆，见练习 3）。

**练习 3**：设计一个三配置消融序列，把 DSpark 相对 DFlash 的增益拆解到单个部件。

**答案**：以 `dspark_qwen3_4b.py` 为基线做三份派生配置：① `markov_rank=0`（只关 markov，注意同时保持 `confidence_head_with_markov` 语义成立——设为 False）；② `l1_loss_alpha=0, ce_loss_alpha=1.0`（只关蒸馏，保留置信度头——但置信度头的 BCE 目标 `accept_rate_3d` 依赖 `aligned_target_logits`，它由 forward 产出，不受影响）；③ `confidence_head_alpha=0.0`（只关置信度头）。各自训练后对比 `accept_len` 与 `verify_rate`。数据与缓存可完全复用，因为三个算法读的是同一种 target cache 格式（u2-l4）——这也是 DeepSpec 三算法共享同一数据流水线的直接好处。

## 5. 综合实践

**任务：产出一份「三 config 逐字段对比表 + 影响推断报告」。**

1. **逐字段对比**：以 `dspark_qwen3_4b.py`、`dflash_qwen3_4b.py`、`eagle3_qwen3_4b.py` 为对象，把 `model` 与 `train` 两个字典的所有键列成一张表（键为行，三个算法为列；某算法无此键则标「—」）。可以手工 diff，也可以用 4.1.4 的 `load_config` 脚本自动提取。重点标出：三个开关、`trainer_cls`、`torch_compile`、`seed`、Eagle3 独有的 `ttt_length`/`step_loss_decay`/`draft_num_hidden_layers`、DSpark 系独有的 `block_size`/`mask_token_id`/`num_anchors`。

2. **影响推断**：对下面每个差异，写 2–3 句推断并注明依据（源码行号或前置讲义结论）：

   - `markov_rank=256 → 0` 对**显存**（约 7780 万可训练参数 + BF16Optimizer 的 fp32 主权重与 Adam 动量，u3-l4）、**训练稳定性**（少一个偏置头的多任务交互）、**接受率**（块内独立采样丢失 token 级依赖，链式条件下分布更不准）的影响；
   - `l1_loss_alpha=0.9 → 0` 对**训练稳定性**（硬标签 CE 有唯一真值锚定，梯度更「常规」）与**接受率**（不再直接最小化分布距离 \( L1 = 2(1-a) \)，u4-l4）的影响；
   - `confidence_head_alpha=1.0 → 0` 对**功能面**（评估侧失去置信度调度/提前截断能力，u6-l5）的影响；
   - `num_draft_layers=5 ↔ draft_num_hidden_layers=1` 加上前向次数（1 次 vs 7 次）对**显存来源**（「宽」vs「长」，练习 4.2.5-2）的影响；
   - `torch_compile` 与 `seed` 差异对**复现实验**的注意点。

3. **交叉验证**：拿 README 表中 `Qwen/Qwen3-4B` 一列的三个 checkpoint 名称，回填到你表格的最后一行作为「该配置的公开产物」，确认名称中的 `block7`/`ttt7` 与配置里的 `block_size=7`/`ttt_length=7` 对应。

预期成果是一页 Markdown 表格加推断备注，可直接作为你自己实验的选型记录。所有结论只需读源码即可得出，无需 GPU；涉及实际训练对比的推断标注「待本地验证」。

## 6. 本讲小结

- **DFlash 不是独立代码**：它是 `config/dflash/` 下的配置变体，与 DSpark 共用 `Qwen3DSparkTrainer`、同一个模型类与同一个损失函数；`deepspec/modeling/` 下没有 dflash 目录。
- **三个开关**：`markov_rank=0` 让 `build_markov_head` 返回 None（结构与采样分支同时退化）；`confidence_head_alpha=0` 通过 `enable_confidence_head = alpha > 0` 同时拆掉头与损失项；`ce_loss_alpha=1.0, l1_loss_alpha=0.0` 把加权组合退化为纯 CE——但**位置衰减 `loss_decay_gamma=4.0` 保留**。
- **开关有断言网格保护**：`markov_head_type` 只在 rank>0 时必需、`confidence_head_with_markov` 只在 alpha>0 时必需，且 `confidence_head_with_markov=True` 要求 markov 头存在——非法组合在启动时快速失败。
- **损失对比**：DSpark = \( 0.1\,CE_w + 0.9\,L1_w + 1.0\,BCE_w \)；DFlash = \( CE_w \)（同一行加权代码的退化）；Eagle3 = \( \sum_s 0.8^s \ell_s \)（逐步软 CE，链式 7 次前向）。两种衰减维度不同：块内槽位 vs 链步。
- **评估共享**：DFlash checkpoint 的 `architectures` 仍是 `Qwen3DSparkModel`，与 DSpark 命中同一个 Evaluator，差异由模型内 `is None` 分支消化。
- **选型逻辑**：要置信度调度/最高接受率选 DSpark；要块式基线与最少可训练参数的块式路线选 DFlash；草稿侧显存极紧或要链式提议选 Eagle3。

## 7. 下一步学习建议

本讲结束了训练侧与算法对比（第 4、5 单元）。下一讲进入**第 6 单元评估系统**，建议从 [u6-l1 评估框架：eval.py 入口与 BaseEvaluator 骨架](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py) 开始，重点带着本讲的两个问题去读：

1. `Qwen3DSparkEvaluator` 在 `markov_head is None`、`confidence_head is None` 时的提议与统计路径长什么样（本讲的 DFlash 结论将在评估侧得到验证）；
2. README 表中三算法的 `accept_len`/`verify_rate` 差距如何被度量出来——这将把你在本讲写下的「影响推断」变成可检验的数字。

继续阅读的源码顺序建议：`eval.py` → `deepspec/eval/base_evaluator.py` 的 `generate_decoding_sample` → `verify_draft_tokens`。
