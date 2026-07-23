# 训练策略 DraftTrainStrategy

## 1. 本讲目标

上一篇（u6-l1）我们走完了「装配」：`build_training_run` 把草稿模型、目标头、tokenizer、离线 vocab mapping 等打包成 `ModelBundle`，并最终接出一个可以 `.run()` 的 `TrainingRun`。但装配层只负责「把对象造好、把线接上」，它**并不知道**一个 batch 进来后该怎么算损失。

本讲就来回答这个问题：**草稿模型到底怎么从一个 batch 算出一个 loss？** 这个唯一知道答案的地方，就叫**训练策略（DraftTrainStrategy）**。

学完本讲你将能够：

- 说清 `DraftTrainStrategy` 这个抽象基类定义了哪几个职责，以及为什么训练主循环要刻意保持「无分支（branch-free）」。
- 区分 EAGLE3 / P-EAGLE / DFlash / DSpark / Domino 五种策略在 `required_features`、目标投影、损失计算上的差异。
- 理解 `StepContext` 与 `linear_lambda_base` 如何让损失随训练进度衰减（Domino 专属）。
- 看懂 `checkpoint_state_filter` 为什么每个策略过滤权重的方式都不一样。

本讲全部源码集中在一个文件：[specforge/training/strategies/base.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py)。

## 2. 前置知识

本讲默认你已掌握以下概念（来自前置讲义）：

- **TrainBatch（u5-l4）**：训练面唯一携带张量的契约，含 `tensors: Dict[str, Tensor]`、`metadata: Dict[str, Any]`、`sample_ids`。策略的输入就是它。
- **算法契约 FeatureContract（u4-l1）**：声明每个算法在离线/在线模式下需要哪些张量（`required_tensors`）。本讲的 `required_features` 是它在运行时的「镜像」——契约描述「特征文件该存什么」，策略描述「batch 里该有什么」。
- **装配层（u6-l1）**：`ModelBundle` 通过 `providers.step.bind_runtime(...)` 把构造策略所需的全部参数（`strategy_kwargs`）打包好，策略对象在装配阶段就被实例化。
- **训练主循环（u6-l3 的前置直觉）**：`TrainerCore.train_step` 负责「前向 → 反向 → optimizer 边界」三步。本讲聚焦其中第一步「前向」。

如果你对 EAGLE3 的「特征式草拟」「训练时测试（TTT）」还不熟，建议先回看 u1-l4，它会让你更容易理解为什么 EAGLE3 的 loss 是一串按 TTT 步数衰减的 `plosses`。

> **一句话直觉**：训练主循环像一个「不知算法为何物」的传送带工人，它只懂「拿到 batch → 调策略算 loss → 反向 → 更新」；而真正懂「EAGLE3 要怎么投影目标、Domino 的 loss 该怎么加权」的，是被插在传送带上的策略插件。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
|---|---|---|
| `specforge/training/strategies/base.py` | **本讲主角**。定义抽象基类、5 个具体策略、`StepOutput`/`StepContext`/`linear_lambda_base` | 全部 |
| `specforge/training/controller.py` | 训练主循环。是策略的**唯一调用方**，体现「无分支」 | `train_step`、eval、`save_checkpoint` |
| `specforge/runtime/contracts.py` | 跨平面契约。定义 `TrainBatch` | `TrainBatch.tensors/metadata` |
| `specforge/training/DESIGN.md` | 训练面设计文档，画出策略在调用链中的位置 | 职责划分、mermaid 图 |
| `specforge/algorithms/eagle3/providers.py` | 算法 provider，演示策略是如何被构造出来的 | `build_step` |

## 4. 核心概念与源码讲解

### 4.1 DraftTrainStrategy 抽象：让训练循环「无分支」

#### 4.1.1 概念说明

先抛出本讲最关键的设计判断，它写在文件开头的模块文档字符串里：

> A strategy is the only place that knows how a draft model (EAGLE3 / P-EAGLE / DFlash / Domino) turns a normalized `TrainBatch` into a loss; `TrainerCore` stays branch-free and the strategy owns the target projection.

翻译过来有三层含义：

1. **唯一的知识持有者**：整个训练进程里，只有策略对象知道「拿到一个 batch，怎么前向、怎么投影目标、怎么算损失」。
2. **主循环无分支**：`TrainerCore.train_step` 不写任何 `if algorithm == "eagle3"` 之类的判断，它只是机械地调用 `self.strategy.forward_loss(batch, ctx)`。
3. **策略拥有目标投影**：把目标模型的输出（隐藏状态或 logits）投影成训练所需的监督信号，是策略自己的职责，不外包。

这种「把变化点收敛到一个插件」的做法，是策略模式（Strategy Pattern）的典型应用：**算法在变，循环不变**。这也是为什么 u6-l1 装配层能复用同一套 trainer/backend/loader/checkpoint 「脊椎（spine）」服务所有算法。

#### 4.1.2 核心流程

`DraftTrainStrategy` 对外暴露**四个职责**，正好对应训练生命周期的四个时刻：

```
┌─────────────────────────────────────────────────────────────┐
│                   DraftTrainStrategy                        │
├──────────────────┬──────────────────────────────────────────┤
│ required_features│ 声明：我这个算法的 batch 必须带哪些张量  │  ← 静态声明
├──────────────────┼──────────────────────────────────────────┤
│ validate_batch() │ 校验：进来的 batch 够不够数              │  ← 前向前守卫
├──────────────────┼──────────────────────────────────────────┤
│ forward_loss()   │ 计算：batch → loss + 指标（抽象方法）    │  ← 每 micro-step 调用
├──────────────────┼──────────────────────────────────────────┤
│ checkpoint_      │ 过滤：存盘时只保留草稿自己的权重         │  ← 存检查点时调用
│  state_filter()  │                                          │
├──────────────────┼──────────────────────────────────────────┤
│ trainable_module │ 暴露：优化器实际要优化的那个 nn.Module   │  ← 评估/包裹时调用
└──────────────────┴──────────────────────────────────────────┘
```

主循环对策略的调用时机（详见 4.1.3 与 DESIGN.md）：

```text
每个 micro-batch:
    1. ctx = StepContext(global_step, total_steps)
    2. out = strategy.forward_loss(batch, ctx)     # 策略算 loss
    3. backend.backward(out.loss / accum)           # 反向
    4. if 到 optimizer 边界: backend.step(); global_step += 1

存检查点时:
    5. draft_state = strategy.checkpoint_state_filter(backend.state_dict()['model'])

评估时:
    6. module = strategy.trainable_module(); module.eval()
    7. 用同一 forward_loss 跑评估 batch
```

注意第 7 点：**评估也复用 `forward_loss`**，所以策略的损失计算必须同时能用于训练和评测——这意味着策略不能在 forward 里偷偷做「只在训练时该做的事」（比如 dropout 由 module 自己管，策略不掺和）。

#### 4.1.3 源码精读

先看两个贯穿全讲的**值对象**。它们都是 `frozen=True`（不可变），刻意保持轻量：

[specforge/training/strategies/base.py:29-36](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L29-L36) 定义 `StepOutput`：每个 micro-step 的产物，包含 `loss`、`metrics`、可选的 `ratio_metrics`（分子/分母成对存放，便于跨 rank 归约）。它被设计成「通用容器」，所以无论是 EAGLE3 那种「按 TTT 步数返回一串 plosses」的策略，还是 DFlash 那种「只返回一个标量 loss」的策略，都能共用同一条 trainer 循环。

[specforge/training/strategies/base.py:39-46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L39-L46) 定义 `StepContext`：把「训练进行到哪了」这个状态传进 `forward_loss`，只有两个字段 `global_step` 与 `total_steps`。文档字符串说得很明白——**大多数策略忽略它**，只有损失随训练进度变化的算法（目前只有 Domino）才会读它。

接着看抽象基类本体：

[specforge/training/strategies/base.py:63-86](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L63-L86) 定义 `DraftTrainStrategy(abc.ABC)`，这是本讲的「合同」。逐行看四个职责：

- **类属性** `name: str` 与 `required_features: set`：不是实例字段，而是每个子类直接覆盖的「声明」。`required_features` 是一串张量名（如 `"hidden_state"`），它既是 `validate_batch` 的依据，也对应 u4-l1 离线契约里的 `required_tensors`。
- **`trainable_module()`（抽象）**：返回「优化器真正要更新的那个 `nn.Module`」。controller 在评估前用它切到 `eval()` 模式（见下文）。
- **`validate_batch()`（具体实现）**：守卫。把 `required_features` 里不在 `batch.tensors` 中的项挑出来，缺了就直接抛 `ValueError`，并把「缺什么、有什么」都打印出来，便于排错。这是「fail-fast」思想——错误的 batch 绝不混进前向。
- **`forward_loss()`（抽象）**：本合同的核心，子类必须实现。返回 `StepOutput`。
- **`checkpoint_state_filter()`（具体默认实现）**：默认 `return state_dict`，即「不过滤」。但实际每个子类都覆盖了它，因为不同草稿架构存盘时要剔除的东西不同（详见 4.2.3）。

现在验证「主循环确实无分支」。看 controller 是怎么调用策略的：

[specforge/training/controller.py:320-331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L320-L331) 是 `TrainerCore.train_step`。注意第 323 行 `out: StepOutput = self.strategy.forward_loss(batch, ctx)`——它对 `self.strategy` 是哪种算法**一无所知**，只调一个方法。紧接着把 loss 除以 `accumulation_steps`、调 `backend.backward`、在边界处 `backend.step`。整段代码里没有任何 `if strategy.name == ...`，这就是「branch-free」的字面证据。

再看评估路径同样复用策略，进一步印证「forward_loss 必须训练/评估通用」：

[specforge/training/controller.py:676-687](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L676-L687) 中，评估先用 `self.core.strategy.trainable_module()` 拿到模块切 `eval()`，然后用「训练进度的真实 `StepContext`」调用同一个 `forward_loss`。注释特意点出：这样做是为了让「依赖进度的损失（如 Domino 的 `lambda_base`）不会被当成第 0 步来评估」。

最后看检查点路径如何调用 `checkpoint_state_filter`：

[specforge/training/controller.py:711-725](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L711-L725) 中，rank0 会把 `backend.state_dict()['model']`（完整模型状态）交给 `self.core.strategy.checkpoint_state_filter(...)` 过滤，只把「草稿权重」写进共享 payload 里的 `draft_state_dict`。也就是说，**存什么由策略说了算**，存盘机制本身与算法无关。

`specforge/training/DESIGN.md` 用一句话总结了这套分工，可直接作为本小节的判词：

[specforge/training/DESIGN.md:24-27](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/DESIGN.md#L24-L27) 写道：`TrainerCore` 负责无分支的单步训练与累积边界；`DraftTrainStrategy` 负责「模型相关的校验、前向/损失、目标投影、检查点过滤」；`FSDPTrainingBackend` 负责包裹/反向/optimizer 步/分布式梯度范数/完整训练状态。三者各司其职。

#### 4.1.4 代码实践

**实践目标**：亲手验证「主循环无分支」——证明换一个算法，`train_step` 一行都不用改。

**操作步骤**（源码阅读型，不需 GPU）：

1. 打开 [specforge/training/controller.py:320-331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L320-L331) 的 `train_step`。
2. 在整个 `specforge/training/controller.py` 中搜索 `strategy.name` 或 `isinstance(self.strategy`，观察出现次数与位置。
3. 对比 [specforge/training/strategies/base.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py) 里五个策略子类，确认它们都只是「实现同一组方法」，而非「改主循环」。

**需要观察的现象**：

- `train_step` 中调用策略的方式是统一的 `self.strategy.forward_loss(batch, ctx)`，没有任何按算法名分支的代码。
- controller 里出现的 `self.strategy.xxx` 只有 `forward_loss`、`trainable_module`、`checkpoint_state_filter` 这几个基类定义的方法（外加个别对 `getattr(self.strategy, "ploss_decay", 1.0)` 的「鸭子类型」读取，用于指标归约，不算分支）。

**预期结果**：你会确认「新增一个算法，主循环零修改」——这正是策略模式的价值。如果你搜到的分支只是「读属性」而非「调不同方法」，那也属于无分支范畴。

> 待本地验证：若你方便运行，可在 Python 里 `from specforge.training.strategies.base import DFlashTrainStrategy, Eagle3TrainStrategy`，用 `inspect.getsource(TrainerCore.train_step)` 打印源码，肉眼确认无 `if` 分支。

#### 4.1.5 小练习与答案

**练习 1**：`validate_batch` 是抽象方法还是具体方法？为什么不把它也设成抽象的，强制每个子类各自实现？

**参考答案**：它是**具体方法**（在基类里有默认实现，见 [base.py:71-77](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L71-L77)）。因为校验逻辑对所有算法都一样——「检查 `required_features` 是否都在 `batch.tensors` 里」——只是「要检查哪些名字」不同。而「要检查的名字」已经由子类通过覆盖类属性 `required_features` 表达了。把通用算法下沉到基类、把变化数据留给子类，是避免重复代码的标准做法。

**练习 2**：`checkpoint_state_filter` 在基类里给了默认实现 `return state_dict`，但所有子类都覆盖了它。这种「默认实现 + 全员覆盖」的设计有什么好处？

**参考答案**：默认实现让「不关心存盘细节的占位实现」也能跑通（比如临时写个测试用策略），降低实现门槛；而真实生产策略全部覆盖，是因为每种草稿架构「哪些权重算草稿权重、哪些该剔除」各不相同（如 EAGLE3 要剔除冻结的 embedding，P-EAGLE 却要保留 embedding）。默认实现是「安全垫」，覆盖是「正确性」。

---

### 4.2 各策略的 forward_loss 与目标投影

#### 4.2.1 概念说明

五种策略虽然共用一条脊椎，但它们的 `forward_loss` 差异很大。理解这些差异，关键在于抓住三个维度：

1. **需要哪些输入张量（`required_features`）**：决定了离线特征文件要存什么、在线要捕获什么。
2. **目标（target）怎么处理**：草稿模型的监督信号来自目标模型，但目标模型的输出可能是「完整 logits」「剪枝 logits」或「最后一层隐藏状态」。策略要负责把这些形态统一的监督信号「投影」成可比对的分布。
3. **loss 形态**：是单个标量（DFlash/Domino/DSpark），还是一串按 TTT 步数排列的 loss（EAGLE3）。

下表是五种策略的速查对比（本表数据全部来自 [base.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py) 的源码）：

| 策略 | `name` | `required_features` | 目标投影 | loss 形态 | 读 `StepContext`？ |
|---|---|---|---|---|---|
| Eagle3 | `eagle3` | input_ids, attention_mask, loss_mask, **hidden_state**, **target** | 在线直用；离线用冻结 `target_head` 把隐藏状态投影成 logits | 多个 `plosses` 按几何衰减求和 | 否 |
| PEagle | `peagle` | input_ids, attention_mask, loss_mask, **hidden_state**, **target** | 同 EAGLE3（`_prepare_eagle_target`） | 单标量 | 否 |
| DFlash | `dflash` | input_ids, **hidden_states**, loss_mask | **无**——用硬标签，不需要目标分布 | 单标量 | 否 |
| DSpark | `dspark` | input_ids, hidden_states, loss_mask, **target_last_hidden_states** | 用目标最后一层隐藏状态做额外监督 | 单标量 | 否 |
| Domino | `domino` | input_ids, hidden_states, loss_mask | **无**（同 DFlash 的特征 schema） | 单标量（但混入衰减的 `lambda_base`） | **是** |

注意两个容易混淆的命名细节：

- EAGLE3 / P-EAGLE 的特征名叫 **`hidden_state`（单数）**，而 DFlash / Domino / DSpark 叫 **`hidden_states`（复数）**。这是各自算法约定的 schema 名，并非笔误，文档字符串专门提醒过。
- EAGLE3 的 `target` 是「目标模型的输出」（logits 或隐藏状态），而 DSpark 的 `target_last_hidden_states` 是「目标模型最后一层隐藏状态」——名字不同，语义也不同。

#### 4.2.2 核心流程

**EAGLE3 的目标投影**最值得展开，因为它涉及「在线 vs 离线」两条数据模式的差异（承接 u2-l1）。投影逻辑统一放在一个模块级函数里：

[specforge/training/strategies/base.py:89-115](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L89-L115) 是 `_prepare_eagle_target`，它的分支如下：

```text
读取 batch.metadata["target_repr"]:
├─ "hidden_state"（离线模式）:
│      ① target_head.preprocess(input_ids, target, loss_mask)  # 做位移对齐
│      ② target = target_head(target)                           # 把隐藏状态投影成全词表 logits
│      返回 (input_ids, target, loss_mask)，均已 .to(device)
└─ 其它（"logits" / "pruned_logits"，在线模式）:
       在线捕获阶段已经把 logits 位移对齐好、可能已剪枝
       直接 .to(device) 原样返回，不再过 target_head
```

为什么会有这种分叉？因为在线捕获时，特征是 SGLang 推理服务「当场」算出来的 logits，位移和投影都在捕获侧做完了；而离线模式下，`.ckpt` 文件里存的是目标模型的**最后一层隐藏状态**（更省空间），所以要把位移对齐和「隐藏状态 → logits」的投影在训练时由冻结的 `target_head` 重做一遍。一句话：**在线投影在捕获侧，离线投影在训练侧**，两条路殊途同归到「一份可比对的目标分布」。

**EAGLE3 的 loss 计算**还涉及 TTT（训练时测试，承接 u1-l4）：

[specforge/training/strategies/base.py:285-286](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L285-L286) 显示 EAGLE3 的最终 loss 是一串 `plosses` 的**几何加权和**：

\[ \text{loss} = \sum_{i=0}^{N-1} \text{ploss\_decay}^{\,i} \cdot \text{plosses}[i] \]

其中 \(N\) 是 TTT 步数（草稿连走的步数），权重 \(\text{ploss\_decay}^{\,i}\)（默认 `0.8`）让越靠后的步 loss 权重越低。直觉是：越往后，草稿吃的是自己上一步的输出（误差累积越大），所以衰减它的权重，让训练更关注前面几步的准确度。

**DFlash / Domino / DSpark 的 loss** 则简单得多——它们各自调底层模型一次，直接拿到 `(loss, accuracy, model_metrics)` 三元组，loss 是单标量。区别在于：

- DFlash：用硬的真实 token 标签，不需要目标分布，也不做 vocab 映射。
- Domino：在模型调用时多传一个随训练衰减的 `lambda_base`（见 4.3）。
- DSpark：在模型调用时多传 `target_last_hidden_states`，并从模型返回里捞出一批额外的指标（`ce_loss`/`l1_loss`/`confidence_loss` 等）和 `ratio_metrics`。

#### 4.2.3 源码精读

先看 EAGLE3 的 `required_features` 与构造参数：

[specforge/training/strategies/base.py:118-149](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L118-L149) 定义 `Eagle3TrainStrategy`。`required_features` 含 5 项（含 `hidden_state` 单数与 `target`）；构造函数接收草稿模型 `eagle3_model`、可选的冻结 `target_head`、TTT 衰减系数 `ploss_decay=0.8`，以及两个 compact-teacher 相关参数。若开启 `compact_teacher`，构造时立即调 `_validate_compact_teacher()` 做前置校验。

接着是 EAGLE3 的 `forward_loss` 全貌（本讲最长的一段）：

[specforge/training/strategies/base.py:231-298](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L231-L298)。关键步骤：

1. 第 234 行 `self.validate_batch(batch)`：守卫。
2. 第 237 行 `target_repr = batch.metadata.get("target_repr")`：从 batch 元数据读出「目标是什么形态」，决定走哪条投影路径。
3. 第 240-266 行的 `if self.compact_teacher / else`：compact-teacher 是「分块把冻结目标头流式喂给前向」的省显存路径（仅离线、仅 `hidden_state`）；普通路径走 `self._prepare_target(...)`，它内部就是 4.2.2 讲的 `_prepare_eagle_target`。
4. 第 268-284 行：把投影好的 target、隐藏状态、attention/loss mask 一起喂给 `self.eagle3_model(...)`，返回**七元组**——除 `plosses` 外，还有 `acceptance_rates`、`acces`、`acc_corrects`、`acc_denoms`、`metric_losses`、`metric_loss_denoms`，这些都是按 TTT 步数排列的「接受率」相关诊断量（呼应 u1-l4 的「最大化接受率」目标）。
5. 第 285-286 行：几何加权求和得 loss（公式见 4.2.2）。
6. 第 287-298 行：把所有诊断量 `.detach()` 后塞进 `StepOutput.metrics`。

相比之下，DFlash 的 `forward_loss` 短得多：

[specforge/training/strategies/base.py:430-444](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L430-L444)。它只需要 `input_ids`、`hidden_states`（复数）、`loss_mask` 三个张量，直接调 `self.dflash_model(...)` 拿到 `(loss, accuracy, model_metrics)`，loss 是单标量。注意它的 `required_features` [base.py:419](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L419) **没有 `target`、没有 `attention_mask`**——这是 DFlash「用硬标签、块并行」特性的直接体现。

再看 DSpark，它是「DFlash + 目标隐藏状态监督」：

[specforge/training/strategies/base.py:456-487](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L456-L487)。`required_features` 在 DFlash 基础上 [base.py:460-465](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L460-L465) 多了 `target_last_hidden_states`；前向时把这个张量也喂给模型，并从返回里捞出 `ce_loss`、`l1_loss`、`confidence_loss`、`confidence_abs_error` 等额外指标和 `ratio_metrics`（后者用 `StepOutput.ratio_metrics` 成对存放分子分母，便于跨 rank 归约）。

P-EAGLE 的特别之处在于它是「字段名翻译边界」：

[specforge/training/strategies/base.py:348-396](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L348-L396)。它的 `required_features` 与 EAGLE3 完全相同（都用 `hidden_state` 单数），但底层 `OnlinePEagleModel.forward` 要的是 `hidden_states`（复数）。策略就是这两套命名之间的「显式翻译点」。此外 P-EAGLE 当前是 batch-size=1（COD 模式），第 363-368 行会从 `attention_mask` 推导出 `lengths`，避免把 padding 位置当成文档内容。最后第 378-394 行严格校验模型必须返回「标量 loss」，并额外合成一个 `accuracy = full_acc_sum / full_acc_total` 指标。

最后看本小节要重点对比的 **`checkpoint_state_filter` 差异**。四种「块并行家族」（DFlash/Domino/DSpark）写法几乎一致：

[specforge/training/strategies/base.py:446-453](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L446-L453)（DFlash 版）：只保留键名里含 `draft_model.` 的权重，并把前缀 `draft_model.` 剥掉。注释点明：所有可训练参数都在 `draft_model.` 下，而目标 embedding/head 是另一个模块，不作为草稿权重存盘。Domino（[base.py:572-579](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L572-L579)）与 DSpark（[base.py:507-512](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L507-L512)）写法相同。

但 **EAGLE3 与众不同**：

[specforge/training/strategies/base.py:300-313](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L300-L313) 的 EAGLE3 版多了一步：先检查「从目标模型复制过来的 embedding 是否真的被冻结了」（第 304-308 行，靠查**活模块**的 `requires_grad`，因为存盘张量已 detach、不携带梯度信息）；只有当 embedding 确实冻结时，才在过滤时把它剔除（第 312 行的 `not (embed_frozen and "embed" in k.lower())`）。这保证「冻结的 embedding 不重复存盘，但可训练的 embedding 一定存下来」。

而 **P-EAGLE 又是另一种**：

[specforge/training/strategies/base.py:398-406](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L398-L406) 的 P-EAGLE 版**不做任何 embedding 剔除**，保留 `draft_model.` 下全部权重。注释解释：P-EAGLE 会训练自己的 token embedding 和 `mask_hidden` 参数，所以必须完整存盘，否则 resume/export 会丢权重。

至于「策略对象是怎么被构造出来的」，可在 provider 里看一眼：

[specforge/algorithms/eagle3/providers.py:45-52](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L45-L52) 的 `build_step` 是 EAGLE3 算法的 `step` provider 端口（承接 u4-l3），它把装配阶段算好的 `wrapped_model`、`target_head` 和一组 `**options`（即 `strategy_kwargs`）传给 `Eagle3TrainStrategy`。这条链路正好补全了 u6-l1 提到的「`providers.step.bind_runtime` → `strategy_kwargs` → 策略构造」的最后一环。

#### 4.2.4 代码实践

**实践目标**：完成规格要求的对比——找出 `Eagle3TrainStrategy` 与 `DFlashTrainStrategy` 在三个维度上的差异，并用源码行号佐证。

**操作步骤**（源码阅读型，不需 GPU）：

1. 打开 [base.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py)。
2. 分别定位两个类的 `required_features`、`forward_loss`、`checkpoint_state_filter`。
3. 填写下表（答案见下方）。

| 维度 | Eagle3 | DFlash |
|---|---|---|
| `required_features` | ? | ? |
| target 处理方式 | ? | ? |
| `checkpoint_state_filter` | ? | ? |

**需要观察的现象**：

- 二者所需张量集合大小不同（5 项 vs 3 项）。
- EAGLE3 有一条「目标投影」分支，DFlash 没有。
- EAGLE3 的存盘过滤有「冻结 embedding 剔除」逻辑，DFlash 没有。

**预期结果**（参考答案）：

| 维度 | Eagle3 | DFlash |
|---|---|---|
| `required_features` | `{input_ids, attention_mask, loss_mask, hidden_state, target}`（5 项，含 `target` 与单数 `hidden_state`） | `{input_ids, hidden_states, loss_mask}`（3 项，复数 `hidden_states`，**无 target、无 attention_mask**） |
| target 处理 | 通过 `_prepare_eagle_target` 做目标投影：在线直用 logits，离线用冻结 `target_head` 把隐藏状态投影成全词表 logits | **不做目标投影**——用硬的真实 token 标签，不需要目标分布，也不做 vocab 映射 |
| `checkpoint_state_filter` | 只留 `draft_model.` 权重、剥前缀，**且**当 embedding 被冻结时额外剔除它（保留可训练 embedding） | 只留 `draft_model.` 权重、剥前缀，**无** embedding 特殊处理 |

**深入一步**：用一句话解释为什么 DFlash 不需要 `target`——因为 DFlash 的监督信号是「真实的下一个 token id」（硬标签），直接从 `input_ids` 与 `loss_mask` 就能算交叉熵，不需要目标模型再给一份概率分布；而 EAGLE3 要让草稿去**逼近目标模型的输出分布**（软标签），所以必须有 `target`。

> 待本地验证：以上对比纯基于源码阅读，若你想跑通，可参照 u2-l1 的示例配置分别准备 EAGLE3 与 DFlash 的离线特征，观察二者 `.ckpt` 里张量名的差异（`hidden_state` vs `hidden_states`、是否有 `target`）。

#### 4.2.5 小练习与答案

**练习 1**：DSpark 的 `required_features` 比 DFlash 多了 `target_last_hidden_states`，这对它的离线特征文件意味着什么？

**参考答案**：意味着 DSpark 的离线 `.ckpt` 除了 DFlash 那些张量外，**必须额外存储目标模型最后一层的隐藏状态**（见 u5-l3 的离线 schema 表：DSpark 额外存 `target_last_hidden_states`）。这份额外张量在训练时被喂给草稿模型做监督，所以 DSpark 被描述为「DFlash + 目标隐藏状态监督」。如果离线特征缺了它，`validate_batch` 会在第一个 step 直接抛 `ValueError`。

**练习 2**：为什么 EAGLE3 的 loss 是一串 `plosses` 的加权和，而 DFlash 只有一个标量 loss？

**参考答案**：因为 EAGLE3 有「训练时测试（TTT）」机制——草稿在一次前向里连走多步（默认 7 步，承接 u1-l4），每一步都产生一个 loss，故 `plosses` 是一个长度等于 TTT 步数的列表；用几何衰减权重（`ploss_decay**i`）加权求和，是为了降低「越往后、误差累积越大」的那些步的影响。DFlash 是「块并行、单次前向」，只产生一个 loss，自然就是单标量。这也是 `StepOutput` 要设计成通用容器的原因：它得同时装下「列表型」和「标量型」两种 loss。

**练习 3**：P-EAGLE 的 `checkpoint_state_filter` 为什么不像 EAGLE3 那样剔除 embedding？

**参考答案**：因为 EAGLE3 默认把从目标模型复制来的 embedding **冻结**（不训练），所以存盘时可以剔除以省空间；而 P-EAGLE **会训练自己的 token embedding 和 `mask_hidden` 参数**（见 [base.py:398-406](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L398-L406) 的注释），这些是可训练权重，必须完整存盘，否则 resume 或 export 会丢失已学到的 embedding。这正体现了「存什么由策略的算法语义决定」。

---

### 4.3 StepContext 与 lambda 衰减

#### 4.3.1 概念说明

前面两个小节看到，四个策略都不读 `ctx` 参数——`forward_loss` 收到 `StepContext` 却忽略它。唯一真正用上它的是 **Domino**，因为 Domino 的损失里混入了一个随训练进度衰减的权重 `lambda_base`。

**为什么要衰减？** Domino 的总损失是「最终目标损失」与「某种基础损失（base loss）」的加权混合。训练初期，base loss 提供额外的引导信号，帮助模型快速进入合理的参数空间；随着训练推进，模型越来越接近目标，应该逐步放弃 base loss、只优化最终目标，避免 base loss 的偏差干扰最终收敛。`lambda_base` 就是这个「基础损失的权重」，它从 `lambda_start`（默认 1.0）线性衰减到 0。

`StepContext` 就是把「现在训练到第几步、总共多少步」这个全局进度，从 controller 传进策略的载体。没有它，策略就无法知道当前进度，衰减就无从谈起。

#### 4.3.2 核心流程

衰减函数 `linear_lambda_base` 的数学定义（见 [base.py:49-60](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L49-L60)）：

设总步数为 \(T\)，衰减占比为 \(\rho\)（`decay_ratio`，默认 0.5），起始权重为 \(\lambda_0\)（`lambda_start`，默认 1.0）。先算「衰减窗口长度」：

\[ D = \max(1,\; \lfloor T \cdot \rho \rfloor) \]

再算当前进度比例（不超过 1）：

\[ p = \min\!\left(\frac{\text{global\_step}}{D},\; 1\right) \]

最终权重（截断到 \([0,1]\)）：

\[ \lambda_{\text{base}} = \mathrm{clip}\!\left(\lambda_0 \cdot (1 - p),\; 0,\; 1\right) \]

直观行为：

- 训练开始（`global_step=0`）：\(p=0\)，\(\lambda_{\text{base}}=\lambda_0=1.0\)，base loss 全权重参与。
- 走到 `global_step = D`（即 \(T\cdot\rho\) 处）：\(p=1\)，\(\lambda_{\text{base}}=0\)，base loss 完全关闭，之后一直保持 0。
- 也就是说，**在前 \(\rho\) 比例的步数里线性退火，之后纯目标损失**。

举个数例：\(T=1000,\ \rho=0.5,\ \lambda_0=1.0\)，则 \(D=500\)：

| global_step | \(p\) | \(\lambda_{\text{base}}\) |
|---|---|---|
| 0 | 0.0 | 1.0 |
| 125 | 0.25 | 0.75 |
| 250 | 0.5 | 0.5 |
| 500 | 1.0 | 0.0 |
| 800 | 1.0 | 0.0 |

**没有训练步数上限时的兜底**：如果 `ctx` 为 `None` 或 `total_steps` 未设置（≤0），`_lambda_base` 直接返回 0.0——即「不知道总共训多久，就关闭 base loss，只用最终目标损失」。这是个安全的退化。

#### 4.3.3 源码精读

衰减函数本体：

[specforge/training/strategies/base.py:49-60](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L49-L60) 的 `linear_lambda_base`，与 4.3.2 的公式逐行对应。注意 `decay_steps = max(1, int(total_steps * decay_ratio))` 里的 `max(1, ...)` 防止 `total_steps` 过小或 `decay_ratio` 为 0 时出现除零。

Domino 如何取用 `ctx`：

[specforge/training/strategies/base.py:543-549](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L543-L549) 的 `_lambda_base(ctx)` 是衰减的「门卫」：若 `ctx is None` 或 `not ctx.total_steps`，返回 0.0；否则才调 `linear_lambda_base(ctx.global_step, ctx.total_steps, self.lambda_start, self.decay_ratio)`。这里 `self.lambda_start` 与 `self.decay_ratio` 来自构造函数 [base.py:526-535](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L526-L535)，默认 1.0 与 0.5。

Domino 的 `forward_loss`：

[specforge/training/strategies/base.py:551-570](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L551-L570)。流程：第 557 行先 `lambda_base = self._lambda_base(ctx)` 算出当前权重；第 558-563 行把它作为参数传给 `self.domino_model(...)`（具体怎么混进 loss 是底层模型的事，策略只负责把「当前进度对应的权重」喂进去）；第 566-569 行还把 `lambda_base` 本身作为一个标量指标塞进 `metrics`，便于在 tracker 里观察退火曲线。注意第 566 行 `metrics.setdefault(...)`——如果模型自己已上报了 `lambda_base`，就不覆盖。

`StepContext` 是从哪里造出来的：

[specforge/training/controller.py:576-581](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L576-L581) 显示，controller 在每个 micro-batch 调 `train_step` 时，都会现场构造 `StepContext(global_step=self.global_step, total_steps=self.total_steps)` 传进去。而 `self.global_step` 只在 optimizer 边界才自增（承接 u6-l3 / u6-l4 的边界语义）。所以同一个 optimizer 边界内的若干 micro-batch 共享同一个 `global_step`，`lambda_base` 在这段区间内是稳定的。

评估时的 `StepContext`：

[specforge/training/controller.py:681](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L681) 评估也用「训练进度的真实 `StepContext`」，这正是为了避免把 Domino 评估成「第 0 步、`lambda_base=1`」的失真状态。

#### 4.3.4 代码实践

**实践目标**：亲手推演 `lambda_base` 随训练步数的变化曲线，理解退火窗口。

**操作步骤**（纯计算型，不需 GPU）：

1. 假设一个 Domino 训练：`total_steps=2000`，`lambda_start=1.0`，`decay_ratio=0.5`。
2. 用 4.3.2 的公式，手算 `global_step` 取 0、250、500、750、1000、1500 时的 `lambda_base`。
3. 把结果画成一张「步数 → 权重」的折线图（纸笔即可）。

**需要观察的现象**：

- 权重在哪一步降到 0（即退火窗口的右端点）。
- 退火窗口占总训练的比例是否等于 `decay_ratio`。
- 降到 0 之后，再大的步数权重都恒为 0。

**预期结果**：\(D = \lfloor 2000 \times 0.5 \rfloor = 1000\)。

| global_step | \(p=\min(\text{step}/1000,1)\) | \(\lambda_{\text{base}}=1-p\) |
|---|---|---|
| 0 | 0.000 | 1.000 |
| 250 | 0.250 | 0.750 |
| 500 | 0.500 | 0.500 |
| 750 | 0.750 | 0.250 |
| 1000 | 1.000 | 0.000 |
| 1500 | 1.000 | 0.000 |

退火窗口右端点在第 1000 步（恰为 `total_steps × decay_ratio`），之后权重恒为 0。曲线是一条从 1 线性降到 0、之后归零的折线。

> 待本地验证：若你方便运行 Python，可 `from specforge.training.strategies.base import linear_lambda_base` 后对一系列步数调用它，验证上表。注意它要求传入真实的 `total_steps`（>0）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `decay_ratio` 调大到 0.8（其余不变），退火行为会如何变化？这对训练意味着什么？

**参考答案**：`decay_ratio=0.8` 时 \(D=\lfloor T\times0.8\rfloor\)，退火窗口拉长到总训练的 80%，base loss 会「慢慢」退场、参与更久。这意味着 base loss 的引导作用持续更长，模型更久地受其约束；适合 base loss 质量高、希望长期借助它稳定训练的场景，但也意味着更晚才完全切换到纯目标损失。反之 `decay_ratio` 调小则 base loss 退场更快。

**练习 2**：为什么 `_lambda_base` 在 `ctx is None` 或没有 `total_steps` 时返回 0 而不是 `lambda_start`？

**参考答案**：返回 0 是「安全退化」——当不知道总共要训多久时，无法定义合理的退火窗口，与其盲目给 base loss 一个固定权重（可能干扰最终收敛），不如直接关闭它、只用最终目标损失。`lambda_start` 只在「有明确训练总步数」时才有意义，因为衰减是相对于总步数定义的。这也呼应 u9-l2 将讲的 `max_steps`/`total_steps` horizon 语义：没有 horizon 就没有衰减。

---

## 5. 综合实践

把本讲三个模块串起来，设计一个「为假想的新算法 `mydraft` 画一张策略实现蓝图」的任务。它会逼你把 `required_features`、`forward_loss`、`checkpoint_state_filter`、`StepContext` 四件事都想清楚（这也是 u10-l2「新增训练算法」的预演）。

**任务**：假设 `mydraft` 的算法语义如下，请基于本讲学到的抽象，给出它的策略实现方案（纸笔设计，不写真实代码）：

- 它是**特征式草拟**（吃目标隐藏状态，像 EAGLE3），但**只有单步前向**（无 TTT，像 DFlash 那样出单标量 loss）。
- 它需要目标模型的**最后一层隐藏状态**作为监督（像 DSpark）。
- 它希望训练前 30% 的步数里有一个辅助损失权重从 0.5 线性衰减到 0（像 Domino）。
- 它的可训练参数全在 `draft_model.` 前缀下，且它的 embedding **是可训练的**。

**请回答**：

1. 它的 `required_features` 应该包含哪些张量？（提示：结合 EAGLE3 的 `hidden_state`/`target` 与 DSpark 的 `target_last_hidden_states`）
2. 它的 `forward_loss` 签名要不要读 `ctx`？为什么？若要，它应该复用本讲哪个衰减函数？
3. 它的 `checkpoint_state_filter` 应该照抄 DFlash 版还是 EAGLE3 版？为什么？（注意它的 embedding 是可训练的）
4. 它需要不需要像 EAGLE3 那样做 `_prepare_eagle_target` 投影？为什么？

**参考思路**：

1. 作为特征式草拟，至少需要 `input_ids`、`attention_mask`、`loss_mask`、`hidden_state`（输入特征）；因为要逼近目标分布，需要 `target`；又因额外要目标最后一层隐藏状态监督，还需要 `target_last_hidden_states`。
2. **要读 `ctx`**——因为有随进度衰减的辅助损失权重。可直接复用 `linear_lambda_base(global_step, total_steps, lambda_start=0.5, decay_ratio=0.3)`（30% 窗口）。
3. 照抄 **DFlash 版**（只留 `draft_model.` 权重、剥前缀，不做 embedding 特殊处理）。因为它的 embedding 是**可训练**的，必须存盘——EAGLE3 版那套「冻结时剔除 embedding」的逻辑不适用（EAGLE3 剔除的前提是 embedding 被冻结）。
4. 需要。因为它是特征式草拟、要逼近目标分布，且离线模式下 `target` 可能是隐藏状态形态，需要冻结 `target_head` 把它投影成 logits（若 `target_repr=="hidden_state"`）。可复用 `_prepare_eagle_target`。

这个练习把本讲的三个最小模块（抽象职责、各策略 forward_loss 差异、StepContext 衰减）全部用上，并直接通向 u10-l2 的「新增训练算法」。

## 6. 本讲小结

- **策略是唯一的知识持有者**：`DraftTrainStrategy` 是整个训练进程里唯一知道「batch → loss 怎么算」的地方；`TrainerCore.train_step` 刻意保持无分支，只统一调 `strategy.forward_loss(batch, ctx)`。
- **四个职责对应四个时刻**：`required_features`（声明）→ `validate_batch`（前向守卫）→ `forward_loss`（每步算损失）→ `checkpoint_state_filter`（存盘过滤），外加 `trainable_module`（暴露可训练模块，评估/包裹时用）。
- **五策略差异在三维度**：`required_features`（EAGLE3/PEagle 5 项含 `hidden_state`/`target`；DFlash/Domino 3 项用复数 `hidden_states`；DSpark 多 `target_last_hidden_states`）；目标投影（EAGLE3 家族有，DFlash 家族无）；loss 形态（EAGLE3 是 TTT 多步几何加权和，其余是单标量）。
- **目标投影的在线/离线分叉**：在线捕获已把 logits 投影好，策略直用；离线存的是隐藏状态，策略用冻结 `target_head` 重做位移+投影（`_prepare_eagle_target`）。
- **checkpoint 过滤各不相同**：DFlash/Domino/DSpark 只剥 `draft_model.` 前缀；EAGLE3 额外剔除冻结的 embedding；P-EAGLE 因训练自己的 embedding 而**完整保留**全部权重。
- **StepContext 只服务 Domino**：`linear_lambda_base` 在前 `decay_ratio` 比例步数里把 `lambda_base` 从 `lambda_start` 线性衰减到 0，之后纯目标损失；无 `total_steps` 时安全退化为 0。

## 7. 下一步学习建议

本讲讲清了「策略算 loss」这一步。建议接着沿训练主循环往下走：

- **u6-l3 Trainer 与 TrainerController**：看 `forward_loss` 返回的 `StepOutput` 如何被 `train_step` 接住、loss 如何除以 `accumulation_steps`、`optimizer_stepped` 这个「单一权威边界信号」如何驱动 `global_step` 自增与 durable ack。本讲的 `StepContext` 正是由 controller 构造并下发的。
- **u6-l4 FSDP 后端与梯度累积**：看 `forward_loss` 之后的 `backend.backward` / `backend.step` 如何与 `no_sync` 配合，理解「非边界 micro-batch 不做梯度归约」。
- **u6-l5 损失与核心算子**：深入 EAGLE3 的 `plosses` 背后到底用了什么损失（`LogSoftmaxLoss`、LK loss、compact teacher 分块投影），把本讲「几何加权和」里的每一项 `plosses[i]` 算清楚。
- **u10-l2 新增一个训练算法**：把本讲综合实践的「`mydraft` 蓝图」真正落地——声明 `AlgorithmSpec` 契约、实现 provider 端口、写一个 `DraftTrainStrategy` 子类、注册进 builtin registry。本讲是它的直接前置。
