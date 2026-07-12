# 自定义 Loss 与 Loss Scale

> 本讲属于「扩展机制与二次开发」单元（u10），是高级（advanced）讲义。
> 前置讲义：[u5-l1 TrainerFactory 与训练器体系](u5-l1-trainer-factory-and-trainers.md)、[u3-l3 Template 体系与对话格式](u3-l3-template-and-chat-format.md)。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ms-swift 里「损失函数（Loss）」与「损失权重（Loss Scale）」是两件**正交**的事，分别由 `swift/loss/` 与 `swift/loss_scale/` 两个模块负责。
- 通过继承 `BaseLoss` 写出自定义损失，注册进 `loss_map`，并用 `--loss_type` 在训练中启用。
- 读懂 `LossScale.__call__` / `_inner_call` 如何用 `base_strategy`（`default`/`last_round`/`all`）决定「哪些 token 参与训练」。
- 会用 JSON 配置文件（精确匹配 / 正则匹配）自定义按 token 的损失权重，像内置的 `hermes`、`react` 一样控制特定片段的权重。
- 理解 `is_binary_loss_scale` 这个优化开关：当权重只有 0/1 时，框架把 mask 折叠进 `labels`（用 `-100`）并把 `loss_scale` 置空，从而兼容 liger_kernel 等加速路径。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，语言模型训练的损失本质上是一个「逐 token 交叉熵再求平均」的过程。** 对于一个生成的回答序列，模型在每个位置预测下一个 token，与真实 token 算一次交叉熵，最后把所有有效位置的损失加起来除以有效 token 数。`-100` 是 PyTorch 约定的「忽略标记」——标签为 `-100` 的位置不参与损失计算。ms-swift 默认只把 assistant（助手）回答段的 token 当作有效标签，system / user 段被填成 `-100`，所以模型「只学怎么答，不学问什么」。

**第二，但「只在回答上算损失」并不总是够用。** 有时我们希望：

- 给某些关键 token 更高的权重（比如 agent 场景里 `Action:` / `Action Input:` 比寒暄更重要）；
- 让某些 token 完全不算损失（比如空 think 标签 `<think></think>` 不应被学习）；
- 只在最后一轮回答上训练（多轮对话里历史轮次不参与梯度）。

这就是 **Loss Scale**（损失权重）要解决的问题——它给每个 token 打一个 0 到任意正浮点数的权重 `w`，最终损失变成对每个 token 的 `w_i * loss_i` 求和。

**第三，Loss 与 Loss Scale 的分工。**

| 模块 | 目录 | 解决的问题 | 触发方式 |
|---|---|---|---|
| Loss（损失函数） | `swift/loss/` | 「损失**怎么算**」（交叉熵？对比学习？列表排序？） | `--loss_type <名称>` |
| Loss Scale（损失权重） | `swift/loss_scale/` | 「每个 token 的损失**算多重**」（权重 0/1/2.0…） | `--loss_scale <名称>` |

两者正交：你可以用默认的交叉熵 Loss 配上 `hermes` 的 Loss Scale，也可以用自定义对比学习 Loss 配上默认 Loss Scale。本讲就把这两件事分别讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [swift/loss/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss/base.py) | `BaseLoss` 抽象基类，定义自定义损失的统一契约 |
| [swift/loss/mapping.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss/mapping.py) | `loss_map` 注册表，把字符串名映射到损失类 |
| [swift/loss/causal_lm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss/causal_lm.py) | `CustomCrossEntropyLoss`，自定义损失的官方范例 |
| [swift/loss_scale/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py) | `LossScale` 基类、`ConfigLossScale`（JSON 配置式）、`ConcatLossScale`（链式组合） |
| [swift/loss_scale/mapping.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/mapping.py) | `loss_scale_map` 注册表与工厂函数 `get_loss_scale`（支持 `+` 链式语法） |
| [swift/loss_scale/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/utils.py) | `calculate_loss_scale`，按关键词/正则切分回答并赋权的核心算法 |
| [swift/loss_scale/agent.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/agent.py) / [swift/loss_scale/other.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/other.py) | 内置的 `HermesLossScale` / `REACTLossScale` / `IgnoreEmptyThinkLossScale` 等子类 |
| [swift/loss_scale/config/*.json](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/config/hermes.json) | 内置的权重配置文件（hermes/react/qwen/agentflan…） |
| [swift/trainers/seq2seq_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py) | `compute_loss`，把 `loss_scale` 张量真正乘到逐 token 损失上的地方 |
| [swift/trainers/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/utils.py) | `per_token_loss_func`，逐 token 交叉熵工具函数 |
| [swift/trainers/mixin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py) | `loss_type` 如何被注入 trainer 的 `compute_loss_func` |
| [swift/template/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py) | `compute_sft_loss`、`loss_scale` 属性、`_encode_context_list`（binary 折叠） |

---

## 4. 核心概念与源码讲解

### 4.1 自定义 Loss：BaseLoss 与 loss_map 注册

#### 4.1.1 概念说明

「Loss（损失函数）」回答的是**损失怎么算**。ms-swift 的默认损失就是标准的「逐 token 交叉熵求平均」（见后文 `per_token_loss_func` + `compute_sft_loss`）。但当任务变了——比如做句向量 embedding 训练需要对比学习损失（contrastive），做重排序需要 listwise 损失——默认交叉熵就不够用了。

ms-swift 把「可替换的损失」抽象成 `BaseLoss`：它是一个**可调用对象**（实现 `__call__`），接收模型输出和标签，返回一个标量张量。所有自定义损失都继承它，注册进 `loss_map`，再用命令行 `--loss_type <名称>` 切换。这与 callbacks/optimizers/metrics 一样，遵循项目统一的「基类 + 注册表 + CLI 开关」三件套范式（见 u1-l3）。

> 注意边界：根据 [Architecture.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Architecture.md) 的说明，自定义 Loss **目前只支持 sft / pretrain / reranker / embedding 任务**，RLHF 系列算法（DPO/GRPO 等）的损失由各自的 trainer 内置，不走这套机制。

#### 4.1.2 核心流程

自定义损失从「命令行」到「梯度」的链路如下：

```text
--loss_type my_loss
      │
      ▼
SftArguments.loss_type  (字段定义在 trainers/arguments.py)
      │  打包进 trainer
      ▼
SwiftMixin.get_train_kwargs_kwargs(): if args.loss_type: res['compute_loss_func'] = loss_map[args.loss_type](args, self)
      │  实例化你的 Loss 对象（持有 args 与 trainer）
      ▼
Seq2SeqTrainer.compute_loss(): pop 出 compute_loss_func
      │
      ▼
loss = compute_loss_func(outputs, labels, num_items_in_batch, loss_scale, trainer)
      │  你写的 __call__ 在这里执行
      ▼
返回标量 loss → 反向传播
```

关键点：`compute_loss_func` 是被**注入到每个 batch 的 inputs 字典**里，再在 `compute_loss` 中取出调用的——这是一种「把策略对象随数据流传递」的简洁做法。

#### 4.1.3 源码精读

**① 契约：`BaseLoss` 抽象基类**

[swift/loss/base.py:10-53](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss/base.py#L10-L53) 定义了所有自定义损失的契约。构造函数持有 `args` 和 `trainer` 两个上下文对象，并自动探测当前是否运行在 Megatron 后端：

```python
class BaseLoss(ABC):
    def __init__(self, args: 'TrainingArguments', trainer: 'Trainer'):
        self.args = args
        self.trainer = trainer
        mro_class_names = [cls.__name__ for cls in trainer.__class__.__mro__]
        self.is_megatron = 'BaseMegatronTrainer' in mro_class_names

    @abstractmethod
    def __call__(self, outputs, labels, *, num_items_in_batch=None, loss_scale=None, **kwargs) -> torch.Tensor:
        ...
```

要点：

- `args` / `trainer` 让损失函数能访问模型、训练状态和所有超参（比如你要拿 `trainer.model` 做点特殊计算）。
- `is_megatron` 这个标志提示你：同一份自定义损失代码可能同时被普通 swift 和 Megatron-SWIFT 调用，必要时分支处理。
- `__call__` 的签名是**强约束**：必须接受 `outputs`（模型输出）、`labels`（标签）、关键字参数 `num_items_in_batch`（本 batch 有效 token 数，用于求平均）和 `loss_scale`（逐 token 权重张量，详见 4.2）。返回值必须是一个**标量张量**。

**② 注册表：`loss_map`**

[swift/loss/mapping.py:6-16](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss/mapping.py#L6-L16) 是一张「名字 → 类」的字典，文件顶部还有一句注释 `# Add your loss scale here`（原文如此，意为鼓励你在此登记自己的损失）：

```python
loss_map = {
    'cross_entropy': CustomCrossEntropyLoss,   # examples
    'cosine_similarity': CosineSimilarityLoss, # embedding
    'contrastive': ContrastiveLoss,
    'online_contrastive': OnlineContrastiveLoss,
    'infonce': InfonceLoss,
    'pointwise_reranker': PointwiseRerankerLoss,  # reranker
    'listwise_reranker': ListwiseRerankerLoss,
}
```

要加自己的损失，只需在此追加一行 `'my_loss': MyLoss`。注意 `cross_entropy` 这一项被标注为 `# examples`——它就是用来给用户当模板的，与默认损失等价。

**③ 范例：`CustomCrossEntropyLoss`**

[swift/loss/causal_lm.py:5-12](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss/causal_lm.py#L5-L12) 是官方推荐参考的最简实现：

```python
class CustomCrossEntropyLoss(BaseLoss):
    def __call__(self, outputs, labels, *, num_items_in_batch=None, loss_scale=None, **kwargs):
        from swift.trainers import per_token_loss_func
        token_loss = per_token_loss_func(outputs, labels)
        if num_items_in_batch is None:
            num_items_in_batch = (labels[:, 1:] != -100).sum()
        return token_loss.sum() / num_items_in_batch
```

它先调 `per_token_loss_func` 拿到**每个 token 的损失向量**（不是标量），再求和除以有效 token 数得到标量。`per_token_loss_func` 定义在 [swift/trainers/utils.py:174-189](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/utils.py#L174-L189)，核心是把 logits 上转到 float、标签左移一位对齐「预测下一个 token」、用 `ignore_index=-100` 的逐元素交叉熵（`reduction='none'`）：

```python
def per_token_loss_func(outputs, labels, enable_dft_loss: bool = False, **kwargs):
    logits = outputs.logits.float()
    labels = torch.roll(labels, shifts=-1, dims=-1).view(-1)  # 左移一位
    logits = logits.view(-1, logits.shape[-1])
    labels = labels.to(logits.device)
    loss = F.cross_entropy(logits, labels, ignore_index=-100, reduction='none')
    ...
    return loss
```

> **重要提醒**：`CustomCrossEntropyLoss` 是一个**最简范例**，它**故意没有处理 `loss_scale` 参数**。这意味着如果你同时用了 `--loss_type cross_entropy` 和 `--loss_scale hermes`，hermes 的逐 token 权重**不会**被这个损失应用。如果你希望自定义损失也能响应 loss_scale，必须自己把 `loss_scale` 乘上去（参考 4.2.3 里默认损失的做法）。这是初学者最容易踩的坑。

**④ 注入点：trainer 如何拿到 `compute_loss_func`**

字段定义在 [swift/trainers/arguments.py:165](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/arguments.py#L165)：

```python
loss_type: Optional[str] = field(default=None, metadata={'help': f'loss_func choices: {list(loss_map.keys())}'})
```

实例化与注入在 [swift/trainers/mixin.py:1052-1053](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1052-L1053)：

```python
if args.loss_type is not None:
    res['compute_loss_func'] = loss_map[args.loss_type](args, self)
```

这里 `loss_map[args.loss_type](args, self)` 就是「按名字查表 + 实例化」一步完成，实例被塞进返回字典，最终随 trainer 配置走。

#### 4.1.4 代码实践

**实践目标**：实现一个「带标签平滑的交叉熵」自定义损失，注册到 `loss_map`，并用 `--loss_type` 启用，观察它确实接管了损失计算。

**操作步骤**：

1. 在源码目录外（或临时 patch）新建一个 Python 文件 `my_loss.py`，内容如下（**示例代码，非项目原有文件**）：

   ```python
   # my_loss.py —— 示例代码
   import torch
   import torch.nn.functional as F
   from swift.loss import BaseLoss, loss_map

   class LabelSmoothingLoss(BaseLoss):
       """带标签平滑的逐 token 交叉熵。"""
       def __init__(self, args, trainer, eps: float = 0.1):
           super().__init__(args, trainer)
           self.eps = eps

       def __call__(self, outputs, labels, *, num_items_in_batch=None, loss_scale=None, **kwargs):
           logits = outputs.logits.float()
           labels = torch.roll(labels, shifts=-1, dims=-1).view(-1)
           logits = logits.view(-1, logits.shape[-1])
           labels = labels.to(logits.device)
           # 标准 CE（逐 token）
           nll = F.cross_entropy(logits, labels, ignore_index=-100, reduction='none')
           # 平滑项：对非 -100 位置，加上均匀分布的负对数似然
           smooth = -F.log_softmax(logits, dim=-1).mean(dim=-1)
           mask = (labels != -100).float()
           token_loss = (1 - self.eps) * nll + self.eps * smooth
           token_loss = token_loss * mask
           if num_items_in_batch is None:
               num_items_in_batch = mask.sum()
           return token_loss.sum() / num_items_in_batch

   # 注册到 loss_map
   loss_map['label_smoothing'] = LabelSmoothingLoss
   ```

2. 把这个文件用 `--external_plugins my_loss.py` 传给 swift（external_plugins 会在训练前 import 该文件，从而触发注册；详见 u4-l4）：

   ```bash
   swift sft --model Qwen/Qwen3-4B --dataset swift/self-cognition#50 \
       --external_plugins my_loss.py --loss_type label_smoothing \
       --max_steps 5 --output_dir output/test-loss
   ```

**需要观察的现象**：

- 训练日志正常打印 `train_loss`，说明你的损失被调用了。
- 相比 `--loss_type cross_entropy`（不平滑），`label_smoothing` 的损失值整体会**偏高一点点**（因为多了一项均匀分布的 `eps * smooth` 正则）。

**预期结果**：能正常跑完 5 步且无 `KeyError`，证明 `loss_map['label_smoothing']` 注册成功。若提示 `loss_type` 不在合法值里，说明 external_plugins 没被正确 import，请检查路径。

> 待本地验证：标签平滑后验证集准确率的变化因任务而异，本实践只验证「自定义损失能接管计算」这一行为。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaseLoss.__call__` 要同时给 `args` 和 `trainer`？只用 `trainer` 不就够了吗？

**参考答案**：`trainer` 确实能拿到大部分状态，但 `args`（`TrainingArguments`）是**纯配置快照**，访问开销小且语义清晰（例如 `args.loss_type`、`args.num_labels`）；直接暴露 `args` 让损失函数能方便地读取超参，而不必穿透 trainer 内部属性。这是一种「显式依赖」的设计。

**练习 2**：`CustomCrossEntropyLoss` 里 `num_items_in_batch` 为 `None` 时，分母用 `(labels[:, 1:] != -100).sum()`，为什么要对 labels 做切片 `[:, 1:]`？

**参考答案**：因为「预测下一个 token」，损失是在位置 `i` 用 logits 预测 `i+1` 的标签，标签需要左移一位对齐。于是最后一个位置没有对应的「下一个 token」，有效预测位置数等于 `labels` 长度减 1，用 `[:, 1:]`（或等价地砍掉第一个位置）来计数，保证分子分母口径一致。

---

### 4.2 LossScale：按 token 控制损失权重的 base_strategy

#### 4.2.1 概念说明

LossScale 回答的是**每个 token 的损失算多重**。它工作的层次比 Loss 更「靠前」——发生在 **Template 编码阶段**（u3-l3），而不是前向计算阶段。

回顾 u3-l3：Template 把一段对话切成多个「上下文段」（context），每段都有一个 `ContextType`（RESPONSE 表示回答、SUFFIX 表示结尾、OTHER 表示 system/外壳/分隔符）。`LossScale` 就是对每个段打一个权重 `w`：

- `w = 0`：这一段不参与训练（对应标签填 `-100`）；
- `w = 1`：正常参与训练；
- `w = 2.0`（或任意正浮点）：这一段的每个 token 损失乘以 2.0，相当于「重点学」。

「哪些段参与训练」由 **base_strategy（基础策略）** 这个总开关决定，它有三个值，定义在 [swift/loss_scale/base.py:9](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L9)：

| base_strategy | 含义 | 典型场景 |
|---|---|---|
| `default` | 只在所有 assistant 回答段上训练（权重 1） | 标准 SFT（默认值） |
| `last_round` | 只在**最后一轮** assistant 回答上训练 | 多轮对话、RLHF（GRPO/DPO 默认） |
| `all` | 所有 token 都训练（含 system/user） | 预训练（continue pretrain） |

而「参与训练的段内部，每个子片段权重是多少」则由子类的 `get_loss_scale` 决定（base 类返回全 1，`ConfigLossScale` 用 JSON 切分赋权，见 4.3）。

#### 4.2.2 核心流程

一段对话从 messages 到「带权重的 token 序列」，经历 `LossScale.__call__` 这个总入口，内部对每个段调用 `_inner_call`：

```text
context_list = [system段, user段, assistant段1, user段, assistant段2, ...]
context_types = [OTHER,      OTHER,   RESPONSE,    OTHER, RESPONSE,    ]
                                    └── is_assistant=True
                                         │
                          LossScale.__call__ 遍历每一段
                                         │
                ┌────────────────────────┴────────────────────────┐
                ▼                                                  ▼
      _inner_call(段, 类型, query, loss, loss_scale, is_last_round)
                │
                │  判定：这一段要不要训练？训练的话权重是多少？
                ▼
   if 数据里显式 loss=True  → 训练（用 get_loss_scale 取权重）
   if 数据里显式 loss=False → 不训练（权重 0）
   if loss=None（未指定）   → 交给 base_strategy 决定：
        all                          → 训练
        default + is_assistant       → 训练
        last_round + is_assistant + is_last_round → 训练
        其他                          → 不训练（权重 0）
                │
                ▼
   每个段返回 (new_context_list, weight_list)
```

注意一个层次关系：**base_strategy 决定「这一段进不进训练」这个 0/1 大开关；`get_loss_scale` 决定「进了训练的段内部，子片段的具体权重」**。后者只有在前者判定为「要训练」时才会被调用。

#### 4.2.3 源码精读

**① 属性与构造：`is_binary`**

[swift/loss_scale/base.py:32-46](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L32-L46)。`LossScale` 有个类属性 `is_binary = True`，表示「权重只有 0 和 1」。这个标志很重要——它决定了编码时是把 mask 折叠进 labels（binary，快）还是单独保留一个 float 权重张量（non-binary，慢但灵活）：

```python
class LossScale:
    is_binary = True

    def __init__(self, base_strategy='default'):
        if base_strategy not in ALL_BASE_STRATEGY:
            raise ValueError(...)
        self.base_strategy = base_strategy
```

**② 子类钩子：`get_loss_scale`**

[swift/loss_scale/base.py:48-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L48-L64)。base 类的实现是「整段不切分、权重全 1」，子类（如 4.3 的 `ConfigLossScale`）覆盖它来切分并赋权：

```python
def get_loss_scale(self, context, **kwargs) -> Tuple[List[str], List[float]]:
    return [context], [1.]
```

返回值是一个二元组：**切分后的片段列表 + 与之一一对应的权重列表**。比如对 `["学习", "好", "数学"]` 返回 `(["学习","好","数学"], [1.0, 0.5, 2.0])` 表示「数学」权重加倍。

**③ 核心调度：`__call__` 与 `_inner_call`**

`__call__` 在 [swift/loss_scale/base.py:66-112](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L66-L112) 遍历所有段，对 RESPONSE 段额外抽取该轮的 `query` 和数据里可能显式标注的 `loss` / `loss_scale` 字段，再交给 `_inner_call`。真正的策略判定在 [swift/loss_scale/base.py:114-131](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L114-L131)：

```python
def _inner_call(self, context, context_type, query, loss, loss_scale, is_last_round):
    ...
    is_assistant = context_type in {ContextType.RESPONSE, ContextType.SUFFIX}
    if loss or loss is None and (self.base_strategy == 'all' or
                                 (self.base_strategy == 'default' and is_assistant) or
                                 (self.base_strategy == 'last_round' and is_assistant and is_last_round)):
        if loss_scale is None:
            new_context, loss_scale = self.get_loss_scale(context, query=query)
        else:
            new_context, loss_scale = [context], [loss_scale]
    else:
        new_context, loss_scale = [context], [0.]
    return new_context, loss_scale
```

这段是整个 LossScale 的「大脑」，值得逐字符理解。注意 Python 运算符优先级：`and` 比 `or` 紧，所以条件等价于：

\[ \text{train} = \text{loss} \;\lor\; \bigl(\text{loss is None} \;\land\; \text{strategy\_match}\bigr) \]

三种情况：

- 数据里 `loss=True`（u4-l4 提到的数据级 loss 字段）→ 恒为真，**强制训练**这一段；
- 数据里 `loss=False` → 整体为假，**强制不训练**（权重 0）；
- 数据里 `loss=None`（最常见的默认）→ 交给 `strategy_match`，即 base_strategy 与 `is_assistant`、`is_last_round` 的组合判定。

`strategy_match` 的三种策略对应前面表格的语义。判定为「训练」后，若数据没显式给 `loss_scale`，就调子类 `get_loss_scale(context, query=query)` 取权重；若显式给了，直接用。

**④ binary 折叠：`is_binary_loss_scale` 与 `_encode_context_list`**

[swift/loss_scale/base.py:133-136](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L133-L136) 暴露 `is_binary_loss_scale` 属性。编码时 [swift/template/base.py:1087-1110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1087-L1110) 据此分流：

```python
def _encode_context_list(self, context_list, labels_list, loss_scale_list=None):
    is_binary_loss_scale = self.is_binary_loss_scale
    if is_binary_loss_scale is None:
        is_binary_loss_scale = self.loss_scale.is_binary_loss_scale
    ...
    for i, (context, loss_weight) in enumerate(zip(context_list, loss_scale_list)):
        ...
        if loss_scale_list[i] > 0.0:
            # 写入真实 label（参与训练）
        if not is_binary_loss_scale:
            loss_scale.extend([loss_weight] * len(token_list))   # 保留 float 权重
    if is_binary_loss_scale:
        loss_scale = None     # 折叠：mask 已进 labels(-100)，不再单独存权重
    return input_ids, labels, loss_scale
```

这就是 binary 优化的本质：**当权重只有 0/1，框架用 `labels` 里的 `-100` 表示权重 0、用真实标签表示权重 1，于是不需要额外的 `loss_scale` 张量**。这不仅省内存，更重要的是让默认 SFT 能走 liger_kernel、flash 注意力等只认 `labels` 的高速路径。只有当权重出现 2.0 这样的非二值（比如 hermes）时，才会保留一个独立的 `loss_scale` 张量并在损失计算时乘上去。

**⑤ 权重真正生效：`compute_loss` 里乘上去**

binary 折叠意味着：只要权重非二值，`inputs['loss_scale']` 就是一个浮点张量，它最终在 [swift/trainers/seq2seq_trainer.py:167-169](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/seq2seq_trainer.py#L167-L169) 被乘到逐 token 损失上：

```python
if loss_scale is not None:
    loss_scale = torch.roll(loss_scale, shifts=-1, dims=-1).view(-1)  # 同样左移对齐
    outputs.loss = outputs.loss * loss_scale
```

注意这里的 `torch.roll(..., shifts=-1, ...)`——权重张量也要左移一位，与「预测下一个 token」的标签对齐。这一行就是「hermes 给 tool_call 段 2.0 权重」最终落到梯度上的那一步。

#### 4.2.4 代码实践

**实践目标**：直观对比三种 base_strategy 对 labels 的影响，理解「只在回答上训练」是如何实现的。

**操作步骤**（纯源码阅读型，无需 GPU）：

1. 写一个调试脚本 `debug_strategy.py`（**示例代码**）：

   ```python
   # debug_strategy.py —— 示例代码
   from swift import get_processor, get_template

   data = {"messages": [
       {"role": "system", "content": "你是一个有用的助手。"},
       {"role": "user", "content": "1+1=?"},
       {"role": "assistant", "content": "等于2。"},
       {"role": "user", "content": "那2+2呢？"},
       {"role": "assistant", "content": "等于4。"},
   ]}

   tokenizer = get_processor('Qwen/Qwen3-0.6B')
   for strategy in ['default', 'last_round', 'all']:
       template = get_template(tokenizer, loss_scale=strategy)
       template.set_mode('train')
       inputs = template.encode(data)
       labels = inputs['labels']
       n_train = sum(1 for t in labels if t != -100)
       print(f'strategy={strategy:11s} 训练token数={n_train}')
       print('  解码参与训练的部分:', template.safe_decode([t for t in labels if t != -100]))
   ```

2. 运行 `python debug_strategy.py`。

**需要观察的现象**：

- `default`：只有两轮 assistant 回答（「等于2。」「等于4。」）的 token 不为 -100，system 和 user 全是 -100。
- `last_round`：只有**最后一轮**「等于4。」参与训练，第一轮「等于2。」也被置为 -100。
- `all`：包括 system「你是一个有用的助手。」、user「1+1=?」在内的全部 token 都参与训练。

**预期结果**：三者的「训练 token 数」依次递增（`last_round` < `default` < `all`），且解码出的文本正好对应各自策略描述的范围。这就验证了 `_inner_call` 的策略判定。

#### 4.2.5 小练习与答案

**练习 1**：`base_strategy='last_round'` 时，框架怎么知道哪一轮是「最后一轮」？

**参考答案**：`__call__` 里调了 `get_last_user_round(messages)` 算出最后一个 user 消息的索引 `last_user_round`，然后对每段用 `is_last_round = 2 * i >= last_user_round` 判断（`i` 是当前轮次计数，每个 RESPONSE 段会让 `i` 自增）。只有满足 `is_last_round` 的 assistant 段才在 `last_round` 策略下被训练。

**练习 2**：为什么 RLHF（DPO/GRPO）默认把 `loss_scale` 设成 `last_round`，而预训练设成 `all`？

**参考答案**：RLHF 的训练样本通常是「prompt + 模型生成的 completion」，我们只想对 completion（即最后一轮回答）算偏好/策略梯度，prompt 部分不应产生损失，所以用 `last_round`。预训练则是无条件地学习整段文本的续写概率，每一个 token 都是预测目标，所以用 `all`。详见 [swift/arguments/rlhf_args.py:300-313](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L300-L313) 与 [swift/arguments/pretrain_args.py:10](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/pretrain_args.py#L10)。

---

### 4.3 JSON 配置式 loss_scale：精确匹配与正则匹配

#### 4.3.1 概念说明

base 类的 `get_loss_scale` 对整段返回权重 1，没有「段内差异化」。但 agent / 工具调用场景里，回答是高度结构化的，比如 ReAct 格式：

```text
Thought: 我需要查天气
Action: get_weather
Action Input: {"city": "北京"}
Observation: 晴，25度
Final Answer: 北京今天晴，25度。
```

我们通常希望 `Action:` / `Action Input:` 这种**决定工具调用的关键片段**权重更高（模型必须学会正确调用工具），而 `Observation:`（环境返回，模型本就会抄）权重为 0，`Thought:` 权重正常。

ms-swift 提供 `ConfigLossScale` 基类，让你**用一个 JSON 文件**描述这种「按关键词或正则切分回答并赋权」的规则，不必写 Python 代码。它支持两种匹配模式：

- **精确字符串匹配**：用关键词把回答切成「关键词 + 关键词之后到下一个关键词之前的内容」两段，分别赋两个权重。代表是 `react.json` / `qwen.json`。
- **正则表达式匹配**：用正则匹配回答里的某段文本，整段赋一个权重。代表是 `hermes.json` / `ignore_empty_think.json`。

> 区分依据是 JSON 里 value 列表的**长度**：长度为 2 → 精确匹配模式；长度为 1 → 正则模式（见 4.3.3 的源码）。

内置的子类只需两行代码声明用哪个 JSON 文件，例如 [swift/loss_scale/agent.py:27-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/agent.py#L27-L28)：

```python
class HermesLossScale(ConfigLossScale):
    loss_scale_config = 'hermes.json'
```

#### 4.3.2 核心流程

JSON 配置式 loss_scale 的执行分两步：**加载 JSON → 切分赋权**。

```text
ConfigLossScale.__init__
   │  读取 swift/loss_scale/config/<loss_scale_config>.json
   ▼
self.loss_scale_map = {...}   # 内存里的规则字典
   │
   │  训练时，对每个 RESPONSE 段（已经过 base_strategy 判定为"要训练"）
   ▼
ConfigLossScale.get_loss_scale(context, query=query)
   │
   ▼
calculate_loss_scale(query, context, response_loss_scale_map, query_loss_scale_map?)
   │
   ├─ 若有 query_loss_scale_map 且 query 命中 → 整段回答赋单一权重（提前返回）
   │
   ├─ value 长度==2 的 key 当作「精确分隔符」 → split_str_parts_by(response, delimiters)
   │     把回答切成 [{key, content}, ...]，每个 key+content 赋 [w_key, w_content]
   │
   └─ value 长度==1 的 key 当作「正则」 → split_str_parts_by(response, regex, regex_mode=True)
         匹配到的片段赋 [w]
   ▼
返回 (切分后的片段列表, 一一对应的权重列表)
```

#### 4.3.3 源码精读

**① JSON 配置长什么样**

先看两个真实配置。`react.json` 是**精确匹配**（每个 value 长度为 2）：

```json
{
    "Action:": [2.0, 2.0],
    "Action Input:": [2.0, 2.0],
    "Thought:": [1.0, 1.0],
    "Final Answer:": [1.0, 1.0],
    "Observation:": [2.0, 0.0]
}
```

含义：用 `Action:` / `Thought:` 这些关键词把回答切片；`[w_key, w_content]` 里第一个权重给关键词本身，第二个给「该关键词之后、到下一个关键词之前」的内容。所以 `Observation:` 本身权重 2.0、其后的观察内容权重 0.0（不学抄写观察）。

`hermes.json` 是**正则匹配**（每个 value 长度为 1）：

```json
{
    "<tool_call>.+?</tool_call>": [2.0]
}
```

含义：用正则 `<tool_call>.+?</tool_call>` 匹配回答里的工具调用块，整块赋权重 2.0（其余未匹配部分权重 1.0）。

`ignore_empty_think.json` 用一组正则把「空的思考标签」权重设为 0，避免模型学会输出无意义的空 `<think></think>`：

```json
{
    "^<think>\\s*</think>\\s*": [0.0],
    "^</think>\\s*": [0.0]
}
```

**② ConfigLossScale：加载 JSON + 覆盖 get_loss_scale**

[swift/loss_scale/base.py:139-199](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L139-L199)。构造时按 `loss_scale_config` 路径读 JSON；`get_loss_scale` 把字符串 context 交给 `calculate_loss_scale`：

```python
class ConfigLossScale(LossScale):
    is_binary = None              # 由 loss_scale_map 内容动态判定
    loss_scale_config = None      # 子类覆盖此属性指定 JSON 文件名

    def __init__(self, base_strategy='default'):
        super().__init__(base_strategy)
        self.loss_scale_map = None
        if self.loss_scale_config is not None:
            path = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(path, 'config', self.loss_scale_config)
            with open(config_path, 'r', encoding='utf-8') as json_file:
                self.loss_scale_map = json.load(json_file)

    @property
    def is_binary_loss_scale(self):
        if self.is_binary is not None:
            return self.is_binary
        if self.loss_scale_map is None:
            return True
        return all(scale in {0.0, 1.0} for lst in self.loss_scale_map.values() for scale in lst)

    def get_loss_scale(self, context, *, query=None, **kwargs):
        if isinstance(context, str):
            return calculate_loss_scale(query, context, self.loss_scale_map)
        return super().get_loss_scale(context)
```

注意 `is_binary_loss_scale` 的动态判定：若 JSON 里所有权重都 ∈ {0, 1}（比如某些只做 mask 的配置），它仍走 binary 快速路径；一旦出现 2.0、3.0 就走 non-binary 保留权重张量。子类也可以硬编码，比如 `AgentFlanLossScale` 直接声明 `is_binary = False`（[swift/loss_scale/agent.py:9-11](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/agent.py#L9-L11)），因为它含 3.0 权重且结构是嵌套的 `response`/`query` 两层。

**③ 核心算法：`calculate_loss_scale`**

[swift/loss_scale/utils.py:7-59](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/utils.py#L7-L59) 是切分赋权的核心。关键分支：

```python
# 先处理 query 级别整段加权（agentflan 用）
if query_loss_scale_map is not None:
    for key in query_loss_scale_map.keys():
        if key in query:
            ...
            return [response], [float(loss_scale_value)]   # 命中即整段单一权重

# value 长度==2 → 精确分隔符
delimiters = [k for k, v in response_loss_scale_map.items() if len(v) == 2]
if delimiters:
    agent_parts = split_str_parts_by(response, delimiters)
else:
    # value 长度==1 → 正则
    regex_delimiters = [k for k, v in response_loss_scale_map.items() if len(v) == 1]
    agent_parts = split_str_parts_by(response, regex_delimiters, regex_mode=True)

weights = []
agent_content = []
for c in agent_parts:
    if c['key'] in response_loss_scale_map:
        loss_scale = response_loss_scale_map[c['key']]
        if len(loss_scale) == 1:                    # 正则：整段一个权重
            weights += loss_scale
            agent_content.append(c['content'])
        else:                                        # 精确：key 与 content 分别赋权
            weights += loss_scale
            agent_content += [c['key'], c['content']]
    else:
        weights.append(1.)                          # 未匹配的片段权重 1
        agent_content.append(c['content'])
return agent_content, weights
```

两个关键设计：

- **长度 2 vs 长度 1** 决定模式：`len(v) == 2` 走精确分隔、`len(v) == 1` 走正则。所以同一份 JSON 不能混用两种模式（`split_str_parts_by` 一次只用一种）。
- **未匹配片段权重默认 1.0**：正则 `hermes` 只对 `<tool_call>...</tool_call>` 赋 2.0，回答里其他正常文本仍是 1.0，不会被误伤。

**④ 链式组合：`ConcatLossScale` 与 `+` 语法**

有时需要组合多个 loss_scale，比如「先按 hermes 给 tool_call 加权，再用 ignore_empty_think 屏蔽空 think」。[swift/loss_scale/base.py:202-236](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/base.py#L202-L236) 的 `ConcatLossScale` 把多个 loss_scale 串起来，**前一个的输出片段喂给后一个，权重相乘**：

```python
def get_loss_scale(self, context, **kwargs):
    contexts = [context]
    weights = [1.0]
    for ls in self.loss_scales:
        new_contexts, new_weights = [], []
        for c, w in zip(contexts, weights):
            sub_contexts, sub_weights = ls.get_loss_scale(c, **kwargs)
            for sc, sw in zip(sub_contexts, sub_weights):
                new_contexts.append(sc)
                new_weights.append(w * sw)   # 权重相乘
        contexts, weights = new_contexts, new_weights
    return contexts, weights
```

工厂函数 `get_loss_scale` 解析 `+` 语法，见 [swift/loss_scale/mapping.py:44-57](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/mapping.py#L44-L57)：用 `+` 切分字符串，第一段若在 `ALL_BASE_STRATEGY` 里就当作 base_strategy，剩余作为子 loss_scale 名链式组合：

```python
parts = loss_scale.split('+')
if parts[0] in ALL_BASE_STRATEGY:
    base_strategy = parts[0]
    ls_names = parts[1:] or ['base']
else:
    base_strategy = 'default'
    ls_names = parts
if len(ls_names) == 1:
    return loss_scale_map[ls_names[0]](base_strategy)
sub_loss_scales = [loss_scale_map[name]('default') for name in ls_names]
return ConcatLossScale(sub_loss_scales, base_strategy)
```

所以 `--loss_scale last_round+hermes+ignore_empty_think` 是合法的，含义是「base 策略 last_round，再依次套用 hermes 和 ignore_empty_think，权重相乘」。注册表本身在 [swift/loss_scale/mapping.py:7-16](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/mapping.py#L7-L16)。

**⑤ 混合思考模型的自动追加**

一个实用的内置行为：当检测到混合思考（hybrid thinking）模型时，框架会自动给 `loss_scale` 追加 `+ignore_empty_think`，见 [swift/arguments/base_args/template_args.py:168-175](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/template_args.py#L168-L175)。这就是为什么用 Qwen3 这类模型训练时，即使你不显式指定，空 think 也不会被学进去。

#### 4.3.4 代码实践

**实践目标**：用 `hermes` loss_scale 编码一条带 tool_call 的对话，亲眼看到 `<tool_call>...</tool_call>` 段的权重是 2.0、其余是 1.0，并理解 binary 折叠为何在这里被关闭。

**操作步骤**（官方 Architecture.md 提供的调试范式，纯 CPU 可跑）：

1. 写调试脚本 `debug_hermes.py`（**示例代码，改自 Architecture.md**）：

   ```python
   # debug_hermes.py —— 示例代码
   from swift import get_processor, get_template

   data = {"messages": [
       {"role": "user", "content": "今天几号？"},
       {"role": "assistant", "content": (
           "<think>\n我可以调用 get_date 函数获取日期。\n</think>\n"
           '<tool_call>\n{"name": "get_date", "arguments": {}}\n</tool_call>'
       )}
   ]}

   template = get_template(get_processor('Qwen/Qwen3-8B'), loss_scale='hermes')
   template.set_mode('train')
   inputs = template.encode(data)

   print('参与训练的标签解码:', template.safe_decode(inputs['labels']))
   print('loss_scale 张量:', inputs['loss_scale'])
   print('是否为二值(应为False, 因含2.0):', template.loss_scale.is_binary_loss_scale)
   ```

2. 运行 `python debug_hermes.py`。

**需要观察的现象**：

- `inputs['loss_scale']` 不为 `None`（因为 hermes 含 2.0 权重，是非二值的），且其长度与 `labels` 的有效 token 数一致。
- 对应 `<tool_call>...</tool_call>` 文本片段的 token 位置，权重值为 `2.0`；其余回答片段权重为 `1.0`；system/user 段权重为 `0.0`。
- `is_binary_loss_scale` 输出 `False`。

**预期结果**：你会看到一个与 labels 等长的浮点列表，在 tool_call 对应区间是 2.0，验证了 hermes.json 的正则赋权规则确实生效。

> 待本地验证：具体哪些 token id 落在 2.0 区间取决于 tokenizer 对 `<tool_call>` 等字符串的切分，可用 `template.safe_decode` 配合逐 token 打印对照确认。

#### 4.3.5 小练习与答案

**练习 1**：如果我想让模型**完全不学**回答里的 `<think>...</think>` 思考过程（不是只屏蔽空 think，而是全部思考内容），该怎么写 JSON？

**参考答案**：用一个正则匹配整段 think 并赋权重 0，例如：

```json
{
    "<think>.+?</think>": [0.0]
}
```

注册成 `ConfigLossScale` 子类后用 `--loss_scale your_name`。注意正则要非贪婪（`.+?`）并覆盖闭合标签，否则会把后续内容也吃进去。

**练习 2**：`react.json` 里 `"Observation:": [2.0, 0.0]`，为什么第二个值是 0.0？

**参考答案**：`Observation:` 之后的内容是环境/工具返回的结果文本，模型在推理时是**接收**它而非**生成**它，学会逐字抄写观察没有意义（甚至有害，因为真实环境的返回千变万化）。所以把「Observation 之后的内容」权重设为 0，让模型不在这些文本上产生损失；而 `Observation:` 这个关键词本身权重 2.0，保留对「该出现观察了」这一结构信号的学习。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个**「自定义损失 + JSON 权重」联合实验**，并验证它们正交协作。

**任务背景**：你想训练一个 agent 模型，既要用自己写的「焦点加权交叉熵」（对高置信错误预测额外惩罚），又想让 `Action:` / `Action Input:` 这类关键调用片段权重更高。

**步骤**：

1. **写自定义损失**（`focus_loss.py`，**示例代码**）：继承 `BaseLoss`，在标准逐 token CE 基础上，对「模型置信度高（softmax 概率大）却预测错」的 token 施加额外倍数惩罚；并且**正确处理传入的 `loss_scale` 参数**（把它乘到逐 token 损失上，避免 4.1.3 提到的坑）：

   ```python
   # focus_loss.py —— 示例代码
   import torch
   import torch.nn.functional as F
   from swift.loss import BaseLoss, loss_map

   class FocusLoss(BaseLoss):
       def __init__(self, args, trainer, focus_ratio: float = 2.0):
           super().__init__(args, trainer)
           self.focus_ratio = focus_ratio

       def __call__(self, outputs, labels, *, num_items_in_batch=None, loss_scale=None, **kwargs):
           logits = outputs.logits.float()
           labels = torch.roll(labels, shifts=-1, dims=-1).view(-1)
           logits = logits.view(-1, logits.shape[-1])
           labels = labels.to(logits.device)
           nll = F.cross_entropy(logits, labels, ignore_index=-100, reduction='none')
           probs = torch.softmax(logits, dim=-1)
           confident_wrong = (probs.max(dim=-1).values > 0.9) & (labels != -100)
           nll = torch.where(confident_wrong, nll * self.focus_ratio, nll)
           if loss_scale is not None:                       # ★ 关键：手动应用 loss_scale
               loss_scale = torch.roll(loss_scale, shifts=-1, dims=-1).view(-1)
               nll = nll * loss_scale
           mask = (labels != -100).float()
           if num_items_in_batch is None:
               num_items_in_batch = mask.sum()
           return (nll * mask).sum() / num_items_in_batch

   loss_map['focus'] = FocusLoss
   ```

2. **准备一个 ReAct 格式的数据集**（或直接用内置支持 react 的 agent 数据），训练命令同时指定两者：

   ```bash
   swift sft --model Qwen/Qwen3-1.7B \
       --dataset <你的react格式数据> \
       --external_plugins focus_loss.py \
       --loss_type focus \
       --loss_scale react \
       --max_steps 20 --output_dir output/focus-react
   ```

3. **验证正交性**：分别跑三组对照（各 20 步），记录 train_loss 与可训练 token 数：

   - 组 A：`--loss_type focus` 单独（无 `--loss_scale react`）
   - 组 B：`--loss_scale react` 单独（默认损失）
   - 组 C：两者同时（上面的命令）

**需要观察的现象**：

- 组 A 与组 B 的 train_loss 数量级不同（不同的损失定义/权重），但都能正常收敛。
- 组 C 的损失行为是两者的叠加：既体现了 focus 的「高置信错预测加倍」，又体现了 react 的「Action 段权重 2.0」。
- 三组的**可训练 token 数相同**（因为 base_strategy 都是 default，决定「训不训」的是 strategy，与 loss_type 无关）——这证明 Loss 和 Loss Scale 确实正交。

**预期结果**：三组都跑通，组 C 的损失曲线与组 A、B 都不重合但趋势合理，说明自定义损失正确读取了 `loss_scale` 参数并与之协作。

> 待本地验证：focus_ratio 与 react 权重叠加后的具体损失数值取决于数据，本实践重点是验证「两个机制能同时启用且互不破坏」。

---

## 6. 本讲小结

- ms-swift 把「损失函数」与「损失权重」拆成两个正交模块：`swift/loss/` 管**损失怎么算**（`--loss_type`），`swift/loss_scale/` 管**每个 token 算多重**（`--loss_scale`）。
- 自定义损失继承 `BaseLoss`、实现 `__call__` 返回标量张量、登记进 `loss_map`，即可用 `--loss_type` 启用；官方范例 `CustomCrossEntropyLoss` 是最简模板，但它**故意不处理 `loss_scale`**，自定义损失若要响应权重需自己乘上去。
- `LossScale` 用 `base_strategy`（`default`/`last_round`/`all`）这个总开关决定「哪些段参与训练」，子类 `get_loss_scale` 决定「段内子片段的具体权重」；数据级的 `loss`/`loss_scale` 字段优先级最高，能覆盖策略。
- **binary 折叠**是关键优化：权重只有 0/1 时，框架把 mask 折进 `labels`（用 `-100`）并丢弃 `loss_scale` 张量，兼容 liger_kernel 等高速路径；出现 2.0 等非二值才保留浮点权重张量，并在 `compute_loss` 里左移对齐后乘到逐 token 损失上。
- JSON 配置式 loss_scale 通过 `ConfigLossScale` + `calculate_loss_scale` 实现，**value 长度 2 = 精确分隔符切分、长度 1 = 正则匹配**；`get_loss_scale` 工厂支持 `+` 链式语法（如 `last_round+hermes+ignore_empty_think`），经 `ConcatLossScale` 把多个策略串联、权重相乘。
- 混合思考模型会自动追加 `ignore_empty_think`，避免学到空 think——这是 LossScale 机制在产品层的实际落地。

## 7. 下一步学习建议

- **继续 u10 单元**：[u10-l2 Callbacks、Optimizers 与 Metrics 扩展](u10-l2-callbacks-optimizers-metrics.md) 讲同范式的另三个扩展点，学会一个就会全部；`--optimizer`（如 lorap/galore）和 `--eval_metric` 与本讲的 `--loss_type` 是平行结构。
- **深入权重生效路径**：若你想彻底搞清「权重张量如何穿过 data_collator 到达 compute_loss」，建议结合 [u3-l3 Template 体系与对话格式](u3-l3-template-and-chat-format.md) 重读 `swift/template/base.py` 的 `_encode_context_list` 与 `data_collator`，对照本讲 4.2.3 的 ④⑤。
- **强化学习里的损失权重**：GRPO/DPO 默认用 `last_round` 策略（见 [swift/arguments/rlhf_args.py:300-313](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L300-L313)），学完 [u7-l2 GRPO 算法核心](u7-l2-grpo-algorithm-core.md) 后可以回头体会「RL 里 loss_scale 只控制 token 是否参与训练、不控制权重」这一约束（见 Architecture.md 的 Loss Scale 章节）。
- **想做 agent 训练**：直接读 [swift/loss_scale/config/](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/loss_scale/config/react.json) 下的 react/qwen/agentflan 配置与 [docs/source_en/Instruction/Agent-support.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/Agent-support.md) 的 loss_scale 用法，按本讲 4.3 的模式写自己的 JSON 即可。
