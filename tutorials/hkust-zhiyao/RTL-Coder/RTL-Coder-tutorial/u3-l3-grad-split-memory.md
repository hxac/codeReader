# 梯度切分显存优化 mle_scoring_grad_split.py

## 1. 本讲目标

在 u3-l1 里我们读懂了 `CompareTrainer.compute_loss` 的「域损失 + 比较损失」乘性合成，在 u3-l2 里把比较损失的数学（候选归一化、`diff`/`rw_diff` 差分矩阵、0.2/0.3 边距、`aval` 掩码）推到了底。但这两讲都绕开了一个工程痛点：**评分训练每个样本要同时前向 N 条候选序列，显存压力远大于标准 SFT**。本讲就来解决它——读 `train/mle_scoring_grad_split.py` 的**梯度切分（gradient splitting）**方案。

学完本讲，你应当能够：

1. 说清 `mle_scoring.py`（基线）为何吃显存：默认 `training_step` 让 `compute_loss` **一次性前向整批候选**，所有候选的激活值同时驻留。
2. 看懂 `mle_scoring_grad_split.py` 覆写的 `training_step` 如何把「一次多候选前向」拆成「表征梯度 + 逐候选前向」**两阶段**。
3. 解释 **`represent_grad` 表征梯度**：把 `prod` 设成叶子张量、对代理损失 `inter_loss` 反传，得到一个紧凑的「每个候选应当朝哪个方向调整」的向量。
4. 理解阶段二的**逐候选加权反向** `represent_grad[i] * loss` 与 `accelerator.backward`，以及为什么这能让 `per_device_train_batch_size=1` 也能跑评分任务。
5. 画出基线与梯度切分两种方案的**单步显存峰值差异示意图**，定量说明后者为何更省显存。

> **一个必须先说的工程现实**：仓库里 shipped 的 `mle_scoring_grad_split.py` 的 `training_step` **并不能直接运行**——它调用的 `self.get_sftscore`、裸名 `device`、`amp` 在文件里都没有定义（详见 4.1.4）。本讲的任务是讲清这套**梯度切分的设计意图与机制**（这是真正有教学价值的部分），并对着真实源码逐行标注哪些是已实现、哪些是缺失的。请把它当作「带瑕疵的原型 / 设计草图」来读，而不是可直接 `torchrun` 的成品。

## 2. 前置知识

- **评分训练的损失结构**：`loss = (compare_weight * comp_loss + 1) * domain_loss`，域损失只在最后一个候选（参考答案，`Score=1`）上做 SFT，比较损失跨所有候选做带边距排序。u3-l1 已讲透。
- **比较损失的数学**：`get_comp_loss` 把每个候选的「归一化对数似然」softmax 成预测相对得分；`compare_loss` 用 `diff`/`rw_diff` 差分矩阵 + `aval` 掩码（真实差>0.2 且预测差<0.3）做成对 hinge 排序。u3-l2 已推到底，本讲直接引用其结论。
- **多候选 batch 的形状**：collator 把一条指令的 N 个候选拍平成 `(batch×cand, L)`，`compute_loss` 用 `idxs` 反推 `batch_size`、用 `view` 折回 `(batch, cand, L, V)`。默认 `per_device_train_batch_size=1`、候选数 4 时，前向的序列数 = 4。
- **训练步的显存来源**：前向时中间激活值（attention/MLP 各层输出）必须留存到反向时才能算梯度，这是「训练比推理费显存」的根本原因；`gradient_checkpointing`（u2-l6/u3-l1 提到 `model.gradient_checkpointing_enable()`）用重算换显存，但每层激活仍与「同时前向的序列数」成正比。
- **PyTorch 自动微分里的「叶子张量」**：对一个 `requires_grad_()` 的新张量做运算再 `.backward()`，梯度会落在这个张量的 `.grad` 上；这是本讲「表征梯度」技巧的力学基础。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train/mle_scoring_grad_split.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py) | 本讲主角：在 `CompareTrainer` 上**额外覆写 `training_step`**（L285–L344），实现梯度切分；`compute_loss`/`get_comp_loss`/`compare_loss` 与数据管线和 `mle_scoring.py` 完全相同 |
| [train/mle_scoring.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py) | 基线方案：`CompareTrainer` **没有**自定义 `training_step`，沿用 HuggingFace `Trainer` 默认实现——`compute_loss`（L219–L256）一次性前向整批候选 |

关键代码点分布（行号以当前 HEAD `b284707` 为准）：

- **两文件共享、本讲不重复展开**：`compute_loss`（grad_split L250–L284 ≡ scoring L219–L256）、`get_comp_loss`（grad_split L208–L239）、`compare_loss`（grad_split L241–L248）、`ScoreDataset`/`DataCollatorForSupervisedDataset`。它们逐字相同，结论直接复用 u3-l1/u3-l2。
- **本讲唯一新增、逐行精读**：`mle_scoring_grad_split.py` 的 `training_step`（L285–L344）。
- **训练入口差异**：grad_split 的 `train()` 在 L379 调用 `make_supervised_data_module` 时**漏传 `training_args`**（基线 scoring L290 有传）——这是 shipped 代码的又一处瑕疵（见 4.1.4）。

## 4. 核心概念与源码讲解

按「痛点→总设计→阶段一→阶段二→显存含义」的顺序，拆四个最小模块。

### 4.1 痛点与覆写 training_step 的两阶段总设计

#### 4.1.1 概念说明：基线为什么吃显存

HuggingFace `Trainer` 的默认 `training_step` 做的事很简单：拿一个 batch，调 `compute_loss(model, inputs)` 算出 loss，再 `loss.backward()`。问题出在「这个 batch 有多大」。

评分训练里，`per_device_train_batch_size=1` 并不意味着只前向 1 条序列——collator 已经把「1 条指令的 4 个候选」拍平进了 `input_ids` 的第 0 维。所以在基线 `mle_scoring.py` 里，`compute_loss` 第一行就是：

[train/mle_scoring.py:223](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L223) —— `model(input_ids=inputs['input_ids'], ...)` 一次性把 `batch×cand`（默认 1×4=4）条序列全送进模型。

```python
outputs = model(input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'])
```

这一次前向要同时为 **4 条序列**留存中间激活值，等 `loss.backward()` 时再用。序列越长、模型越深，这 4 份激活占的显存越大——这就是评分训练「比标准 SFT 吃显存」的根源（标准 SFT 同样 batch 只前向 1 条序列）。

> 换算一下：若单条序列的前向激活需要 \(A\) 字节，基线单步的激活峰值约 \(4A\)（4 条候选同时驻留）。`gradient_checkpointing` 能把每层的常数压小，但「同时 4 条」这个倍数仍在。

#### 4.1.2 核心流程：把一次多候选前向拆成两阶段

梯度切分的核心思想：**候选之间的「比较」信息，其实可以预先浓缩成一个很小的向量；真正费显存的是逐候选的前向-反向，而那是可以串行做的。**

于是 `training_step` 把单步训练拆成两阶段：

1. **阶段一（表征梯度，represent_grad）**：用一个轻量打分函数得到每个候选的「表征得分」`prod`（长度 = 候选数，默认 4），把它设成叶子张量，构造一个只依赖 `prod` 的代理损失 `inter_loss` 并反传。反传后 `prod.grad` 就是「表征梯度」——一个 (4,) 的向量，编码「每个候选的得分该朝哪个方向调」。这一步**不把模型参数纳入反传图**，只为拿到这 4 个标量权重。
2. **阶段二（逐候选前向-反向）**：循环遍历每个候选，**一次只前向 1 条序列**，算该候选的 `loss`，乘以 `represent_grad[i]` 作为权重，立刻 `backward()` 反传到模型参数；反传完释放激活，再处理下一个候选。

净效果：阶段二任意时刻只持有 **1 条序列**的激活，峰值从 \(4A\) 降到约 \(A\)。代价是阶段一额外做一次（设计上应当是 `no_grad` 的）打分前向，以及梯度从「精确耦合」变成「一阶解耦近似」（见 4.3.5）。

#### 4.1.3 源码精读：training_step 的骨架

先看整个 `training_step` 的骨架与两阶段的分界：

[train/mle_scoring_grad_split.py:285-321](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L285-L321) —— 头部 `_prepare_inputs` 后 `deepcopy` 出两份输入（阶段一、阶段二各用一份，避免互相污染）；`cand_num = inputs['input_ids'].shape[0]` 把「拍平后的第 0 维」当作候选数（隐含 `per_device_train_batch_size=1`，所以 4 条 = 4 个候选）；随后是阶段一的打分 + 表征梯度（4.2 详读）。

```python
def training_step(self, model, inputs):
    model.train()
    inputs = self._prepare_inputs(inputs)
    inputs_1 = copy.deepcopy(inputs)   # 阶段一用
    inputs_2 = copy.deepcopy(inputs)   # 阶段二用
    cand_num = inputs['input_ids'].shape[0]   # batch=1 时即候选数
    prod = self.get_sftscore(model=model, inputs=inputs_1)   # 阶段一打分
    ...
    prod.requires_grad_()
    inter_loss = self.args.compare_weight * self.compare_loss(-prod, inputs_score) \
                 + prod[torch.argmax(inputs_score[0], dim=0)]
    inter_loss.backward()
    represent_grad = prod.grad          # 表征梯度（阶段一产物）
```

[train/mle_scoring_grad_split.py:323-344](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L323-L344) —— 阶段二：逐候选取出单候选输入，前向算 `loss`，乘以 `represent_grad[i]` 加权后**立即** `accelerator.backward(loss)`，循环结束后返回 `print_loss`（详见 4.3）。

```python
for i in range(cand_num):
    input = {k: inputs_2[k][0][i] if k in ('idxs','scores') else inputs_2[k][i]
             for k in ('input_ids','attention_mask','labels','idxs','scores')}  # 单候选
    with self.compute_loss_context_manager():
        loss = represent_grad[i] * self.compute_loss(model, input)
    ...
    self.accelerator.backward(loss)     # 立即反传，释放本候选激活
return print_loss.detach() / self.args.gradient_accumulation_steps
```

> 上面阶段二我为了可读把构造 `input` 的五行 `input[...]=...` 压成了一行字典推导；**真实代码**是逐字段赋值（见 [train/mle_scoring_grad_split.py:324-329](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L324-L329)），语义相同。

#### 4.1.4 工程现实：shipped 代码缺失的符号（重要）

在继续读细节前，必须如实说明：**这段 `training_step` 以 shipped 形态无法运行**。逐项核对：

| 行 | 引用 | 状态 | 后果 |
| --- | --- | --- | --- |
| [L309](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L309) | `self.get_sftscore(...)` | **未定义**：全仓库只有这一处调用，没有任何 `def get_sftscore` | 运行到阶段一即 `AttributeError` |
| [L312-313](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L312-L313) | 裸名 `device` | **未定义**：文件无 `device = ...`，也无相关 import | `NameError` |
| [L339](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L339) | `amp.scale_loss(...)` | **未导入**：文件无 `import amp`/`from apex import amp` | `if self.use_apex` 分支命中才报错（默认 `use_apex=False`，属潜在隐患） |
| [L379](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L379) | `make_supervised_data_module(tokenizer=..., data_args=...)` | **漏传 `training_args`**：函数签名（L198）要求 3 个位置参数，基线 [scoring L290](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L290) 有传 | 启动即 `TypeError` |

此外，阶段二把 `input['idxs']` 设成标量 `inputs_2['idxs'][0][i]`（[L328](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L328)），而 `compute_loss` 第一行就做 `inputs['idxs'][:, 0]`（[L254](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L254)），对 0 维张量切片会 `IndexError`——即阶段二的 `compute_loss` 复用也需要修整 `idxs` 形状才能真正跑通。

**结论**：这套梯度切分是**设计意图清晰、但 shipped 实现不完整的原型**。本讲下面把它的机制讲透（这才是学习价值所在），并明确标注哪些是「已实现」、哪些是「需补全」。若你要真正跑它，至少需要：补一个 `get_sftscore`（见 4.2.3 的推测）、定义 `device`、给 `make_supervised_data_module` 补上 `training_args`、并修阶段二的 `idxs` 形状。

> 与之对照，`mle_scoring.py`（基线）是**可直接运行**的：它没有自定义 `training_step`，没有这些缺失符号，README 给出的 `torchrun` 命令针对的就是它。这也解释了为什么 README 的训练章节主推 `mle_scoring.py`，而把 `mle_scoring_grad_split.py` 作为「显存不足时的备选」。

#### 4.1.5 小练习与答案

1. **问**：基线 `mle_scoring.py` 用默认 `training_step`，为什么单步显存大约是标准 SFT 的 4 倍（同等 `per_device_train_batch_size`）？
   **答**：因为 collator 把 1 条指令的 4 个候选拍平进了 `input_ids` 第 0 维，`compute_loss` 一次性前向这 4 条序列，4 份激活同时驻留；标准 SFT 同样 batch 只前向 1 条。激活峰值近似正比于同时前向的序列数。

2. **问**：`cand_num = inputs['input_ids'].shape[0]` 把第 0 维当候选数，这个假设什么时候成立？
   **答**：当 `per_device_train_batch_size=1` 时成立——此时第 0 维恰是「1 条指令的候选数」。若 batch>1，第 0 维 = `batch×cand`，`cand_num` 会被高估，阶段二的逐候选切分也就错位。这正是该方案绑定 `per_device_train_batch_size=1` 的原因（见 4.4）。

3. **问**：阶段二「一次只前向 1 条候选并立即反传」为什么能省显存？省的是哪一部分？
   **答**：省的是「为反传而留存的中间激活」。任意时刻只有当前这 1 条候选的激活在场，上一条已随其 `backward()` 释放，下一条还没开始。激活峰值从「4 条同时」降到「1 条」。

### 4.2 阶段一：表征梯度 represent_grad

#### 4.2.1 概念说明

阶段一的目标是用很小的代价，得到一个「每个候选应当如何调整」的方向信号，存进一个长度为候选数的向量 `represent_grad`。关键技巧是**表征梯度（representation gradient）**：不直接对模型参数反传比较损失，而是先给每个候选打一个「表征得分」`prod`，把 `prod` 当作代理变量，对「关于 `prod` 的损失」求梯度。

为什么要绕这一层？因为比较损失的本质是「跨候选的排序」——它天然耦合所有候选。如果直接对模型参数反传，就必须把所有候选的激活同时留在反传图里（回到基线的显存困境）。而如果我们能把「跨候选的排序方向」预先浓缩成几个标量（表征梯度），再用这些标量去逐候选指导训练，就能把「耦合的反传」拆成「独立的逐候选反传」。

`prod.requires_grad_()` 是这套技巧的「开关」：把 `prod`（本是个普通计算结果）登记成叶子张量，于是 `inter_loss.backward()` 会把梯度沉淀到 `prod.grad`，而**不会**继续往模型参数回传——模型参数在阶段一拿不到梯度，这正是我们想要的（阶段一只为取 `represent_grad`）。

#### 4.2.2 核心流程

1. 用一个打分函数得到 `prod`（长度 = `cand_num`，默认 4），第 \(i\) 个元素代表模型对第 \(i\) 个候选的「表征得分」（候选越好，值的方向越正；确切符号取决于打分函数，见 4.2.3）。
2. `prod.requires_grad_()`：把 `prod` 登记为需要梯度的叶子张量。
3. 构造**只依赖 `prod` 的代理损失** `inter_loss`，由两部分组成：
   - 比较项 `compare_weight * compare_loss(-prod, inputs_score)`：把排序目标写成 `prod` 的函数，复用 u3-l2 的 `compare_loss`。
   - 锚点项 `prod[argmax(inputs_score[0])]`：取真实分最高（参考答案）那个候选的 `prod`，加进损失——给参考候选一个正方向的基础信号，类似基线「域损失锚定参考答案」的角色。
4. `inter_loss.backward()`：梯度沉淀进 `prod.grad`，记为 `represent_grad`。
5. 模型参数在阶段一**不更新、不持有比较损失的反传图**。

#### 4.2.3 源码精读

阶段一的关键几行：

[train/mle_scoring_grad_split.py:308-320](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L308-L320) —— `get_sftscore`（缺失，见下）产出 `prod`；`inputs_score` 取真实分并整理成 `(1, cand_num)`；`prod.requires_grad_()` 把 `prod` 设为叶子；`inter_loss` = 比较项 + 锚点项；`backward` 后取 `prod.grad` 为 `represent_grad`。

```python
cand_num = inputs['input_ids'].shape[0]
prod = self.get_sftscore(model=model, inputs=inputs_1)         # 缺失：见 4.1.4
inputs_score = inputs['scores'][0].reshape(-1, cand_num)      # (1, cand_num)
prod = prod.to(device)                                        # device 未定义：见 4.1.4
prod.requires_grad_()                                         # 登记为叶子张量
inter_loss = self.args.compare_weight * self.compare_loss(-prod, inputs_score) \
             + prod[torch.argmax(inputs_score[0], dim=0)]     # 比较项 + 锚点项
inter_loss.backward()
represent_grad = prod.grad                                    # 表征梯度 (cand_num,)
```

**关于缺失的 `get_sftscore` 的合理推测**。它在语义上对应 u3-l2 里 `get_comp_loss` 内部那条「逐候选算归一化对数似然」的管线：对每个候选算 token 级 NLL、取负除以长度，得到 `prod`。要真正省显存，`get_sftscore` 几乎必然要**在 `torch.no_grad()` 下**运行——这样它虽然前向了所有候选，却不留存激活、不建反传图，只产出 4 个标量 `prod`。若它不在 `no_grad` 下，阶段一就会和基线一样持有全部激活，整套省显存的设计就落空了。**这是基于设计意图的推断，shipped 代码无从证实**（函数本体缺失），但这是理解该方案为何成立的必要前提。

> 锚点项 `prod[argmax(inputs_score[0])]` 的作用：`argmax` 选出真实分最高的候选（参考答案），该项对 `inter_loss` 贡献了 `prod[参考]`，反传时给 `represent_grad[参考]` 直接加 1。这保证参考候选在阶段二总能拿到一份正的训练权重——与基线「域损失保底写对参考答案」的精神一致。

#### 4.2.4 代码实践

**目标**：用最小例子亲眼看到「把一个张量设成叶子、对代理损失反传、梯度沉淀到 `.grad`」这套力学，理解 `represent_grad` 是怎么来的。本实践**不需要模型、不需要 GPU**，纯 PyTorch 张量运算。

**步骤**：

1. 构造一个长度 4 的 `prod`（模拟 4 个候选的表征得分），`requires_grad_()` 登记为叶子。
2. 写一个形如 `inter_loss = w * compare_like(-prod) + prod[best]` 的代理损失（`compare_like` 用一个简单的「把最高分候选拉高」的项代替真实的 `compare_loss`，便于手算）。
3. `inter_loss.backward()`，打印 `prod.grad`。

```python
# 示例代码：观察「叶子张量 + 代理损失 → represent_grad」的力学
# 运行：python explore_represent_grad.py   （仅需 torch，CPU 即可）
import torch

prod = torch.tensor([-2.0, -2.3, -3.1, -1.2])   # 4 个候选的表征得分（第 4 个最看好）
prod.requires_grad_()                            # 关键：登记为叶子张量
rw = torch.tensor([0.40, 0.35, 0.00, 1.00])      # 真实分，第 4 个是参考答案
best = torch.argmax(rw).item()                   # = 3

# 代理损失：比较项（简化版，把参考候选的 -prod 往小压↔prod 往大抬）+ 锚点项 prod[best]
comp_term = (-prod - rw.float()).pow(2).sum()    # 简化：让 -prod 逼近 rw（仅作演示）
inter_loss = 1.0 * comp_term + prod[best]
inter_loss.backward()
print("represent_grad =", prod.grad.tolist())   # 期望第 4 位含来自锚点的 +1
```

**需要观察的现象**：`prod.grad` 是长度 4 的向量；第 `best=3` 位的梯度比其余多出一个常数贡献（来自 `prod[best]` 对自身的导数 1）。

**预期结果**：手算 `comp_term` 对 `prod[i]` 的导数为 `2*(-prod[i]-rw[i])*(-1) = 2*(prod[i]+rw[i])`，锚点项只在 `i=best` 处贡献 `+1`。所以 `represent_grad[3] = 2*(prod[3]+rw[3]) + 1`，明显大于其余位。运行脚本可逐位复现——这就是阶段一产出的「每个候选该朝哪调」的方向向量。

> 这是「力学的最小复现」，**不是**真实 `compare_loss` 的复现（真实项见 u3-l2）。目的是让你抓住 `requires_grad_()` → `backward()` → `.grad` 这条链。

#### 4.2.5 小练习与答案

1. **问**：为什么 `prod.requires_grad_()` 是整套省显存设计的关键开关？
   **答**：它把 `prod` 登记成叶子张量，使 `inter_loss.backward()` 把梯度沉淀到 `prod.grad` 后**停下**，不继续往模型参数回传。这样模型参数在阶段一不进入反传图，阶段一才能（配合 `no_grad` 的 `get_sftscore`）不持有大块激活。

2. **问**：`represent_grad` 的长度为什么是 `cand_num`（4），而不是模型参数的数量？
   **答**：因为它是对「表征得分 `prod`」求的梯度，`prod` 长度 = 候选数。它是一个极紧凑的方向信号（4 个标量），把跨候选的排序耦合浓缩进来；模型参数的梯度留到阶段二逐候选再算。

3. **问**：锚点项 `prod[argmax(inputs_score[0])]` 若去掉，会有什么影响？
   **答**：参考候选（真实分最高）将失去那份 `+1` 的基础正权重，其训练信号完全取决于比较项——当排序已接近满足时比较项梯度可能很小，参考候选可能「学不动」。锚点项保证参考答案始终有正方向信号，对应基线域损失的「保底」作用。

### 4.3 阶段二：逐候选 compute_loss 加权反向

#### 4.3.1 概念说明

拿到 `represent_grad`（长度 4 的方向向量）后，阶段二做「真正的训练」：**逐候选、串行地**前向-反向。每个候选 \(i\) 贡献的参数梯度被设为

\[
\frac{\partial \mathcal{L}}{\partial \theta}\Big|_{\text{候选 }i}
\;\approx\;
\text{represent\_grad}_i \cdot \frac{\partial\, \text{loss}_i}{\partial \theta}
\]

其中 `loss_i = compute_loss(model, 候选i)` 是把**单个候选**送进（与基线完全相同的）`compute_loss` 得到的损失。整步训练对模型参数的总梯度近似为各候选之和：

\[
\frac{\partial \mathcal{L}}{\partial \theta}
\;\approx\;
\sum_{i=0}^{\text{cand\_num}-1} \text{represent\_grad}_i \cdot \frac{\partial\, \text{loss}_i}{\partial \theta}
\]

这是一阶解耦近似：候选间的排序耦合已经被 `represent_grad`（阶段一算好的几个标量）吸收，阶段二只需独立地把每个候选的损失按权重反传。因为每个候选反传完立即释放激活，再开始下一个，**任意时刻只持有 1 条候选的激活**——省显存就来自这里。

#### 4.3.2 核心流程

1. 循环 `for i in range(cand_num)`，每次取出**第 i 个候选**的 `input_ids / attention_mask / labels / idxs / scores`（单候选切片）。
2. 调 `compute_loss(model, input)` 算该候选的损失（复用基线同款 `compute_loss`）。
3. 加权：`loss = represent_grad[i] * compute_loss(...)`。
4. 立即 `self.accelerator.backward(loss)` 把该候选的参数梯度累加进模型的 `.grad`（注意是累加，不是覆盖——每个候选的梯度加在一起才得到上面那个和式）。
5. 循环结束，返回 `print_loss.detach() / gradient_accumulation_steps` 作为日志用的标量。

#### 4.3.3 源码精读

阶段二的逐候选循环与立即反传：

[train/mle_scoring_grad_split.py:323-344](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L323-L344) —— 逐候选切单候选输入；`compute_loss` 在 `compute_loss_context_manager()` 下调用（保持与 Trainer 一致的上下文，如自动混合精度等）；`loss = represent_grad[i] * self.compute_loss(...)` 加权；`accelerator.backward(loss)` **立即**反传该候选、释放其激活后进入下一轮。

```python
for i in range(cand_num):
    input = {}
    input['input_ids'] = inputs_2['input_ids'][i]            # 单候选 (1, L) 或 (L,)
    input['attention_mask'] = inputs_2['attention_mask'][i]
    input['labels'] = inputs_2['labels'][i]
    input['idxs'] = inputs_2['idxs'][0][i]                   # 注意：标量化，compute_loss 需适配
    input['scores'] = inputs_2['scores'][0][i]
    with self.compute_loss_context_manager():
        loss = represent_grad[i] * self.compute_loss(model, input)   # 加权
    ...
    self.accelerator.backward(loss)                          # 立即反传，释放本候选激活
return print_loss.detach() / self.args.gradient_accumulation_steps
```

> 这里复用的 `compute_loss` 与基线 [train/mle_scoring.py:219-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L219-L256) **逐字相同**。当它只收到 1 个候选时，比较损失 `comp_loss` 会退化：`prod_normalized` 退化为单元素 `[1.0]`，`compare_loss` 的差分矩阵全 0、`aval` 全 False，`comp_loss=0`，于是 `loss = (cw*0+1)*domain_loss = domain_loss`。也就是说，**单候选时 `compute_loss` 实际只剩该候选的域损失**——比较信息已经全由 `represent_grad` 承载。这与 4.2 的设计自洽。（但如 4.1.4 所述，`idxs` 标量化会让 `compute_loss` 第一行的 `idxs[:,0]` 报错，需先把 `idxs` 整成 `(1,1)` 之类才能真跑。）

#### 4.3.4 代码实践

**目标**：用一个极小的可训练模型，**真正跑一遍**「逐候选前向-加权-立即反传」的循环，验证每个候选的梯度是**累加**进 `.grad` 的，且任意时刻只有 1 条候选参与。**CPU 即可，不需要 GPU**。

**步骤**：

1. 用 `torch.nn.Linear` 搭一个 1 层「假模型」（输入维度 = 序列长，输出 = 1 个 logit，足够演示梯度流）。
2. 造 4 个「候选」的假输入与一个假 `represent_grad = [1.0, 0.5, -0.5, 1.0]`。
3. 模仿阶段二：逐候选前向 → `loss = represent_grad[i]*loss_i` → `backward()`，每次循环后打印 `model.weight.grad` 的范数，观察它**递增**（累加）。

```python
# 示例代码：逐候选加权反传，观察梯度累加
# 运行：python explore_grad_split_step.py   （仅需 torch，CPU 即可）
import torch

torch.manual_seed(0)
L = 8
model = torch.nn.Linear(L, 1)         # 极小「假模型」
cand = 4
represent_grad = torch.tensor([1.0, 0.5, -0.5, 1.0])   # 假表征梯度（阶段一产物）

xs = [torch.randn(1, L) for _ in range(cand)]          # 4 个候选的假输入

for i in range(cand):
    out = model(xs[i])                                  # 只前向第 i 个候选
    loss_i = out.sum()                                  # 该候选的「损失」（演示用）
    loss = represent_grad[i] * loss_i                   # 加权
    loss.backward()                                     # 立即反传 → 累加进 model.grad
    print(f"候选{i}: rg={represent_grad[i]:+.1f}, "
          f"累计 |grad|={model.weight.grad.norm().item():.4f}")

print("最终 grad =", model.weight.grad.view(-1).tolist())
# 期望 ≈ sum_i represent_grad[i] * xs[i]，逐候选累加而成
```

**需要观察的现象**：`|grad|` 随循环**单调累加**（不是每轮重置）；最终 `model.weight.grad` 等于 `Σ_i represent_grad[i] · xs[i]`——正是 4.3.1 那个和式。

**预期结果**：手算 `∂loss_i/∂W = represent_grad[i]·xs[i]`，逐轮累加；脚本末尾打印的 `grad` 与 `sum(rg[i]*xs[i])` 逐位吻合。这就证明了阶段二的「逐候选加权反传」等价于把一个加权和式拆成串行步骤——而每步只动了 1 个候选。

> 把每个候选换成「一条完整序列过 Transformer」、把 `loss_i` 换成真实 `compute_loss`，就是 shipped `training_step` 阶段二的全貌；显存层面唯一不同的是：这里演示的是「梯度累加」，真实场景下「立即反传释放激活」才是省显存的关键（见 4.4）。

#### 4.3.5 小练习与答案

1. **问**：阶段二的 `accelerator.backward(loss)` 是「累加」还是「覆盖」模型梯度？为什么必须是累加？
   **答**：是累加（PyTorch 默认 `.backward()` 把梯度加到 `.grad` 上）。必须累加，因为总梯度是各候选之和 `Σ_i represent_grad[i]·∂loss_i/∂θ`；逐候选反传只有累加起来才等价于一次完整反传。

2. **问**：梯度切分得到的总梯度 `Σ_i represent_grad[i]·∂loss_i/∂θ`，与基线 `(cw*comp_loss+1)*domain_loss` 的精确梯度相同吗？
   **答**：不完全相同，是一阶解耦近似。基线里比较损失 `comp_loss` 依赖**所有候选**的 `prod`（经 softmax 耦合），其精确梯度含跨候选项；梯度切分把这份耦合预先压成几个标量 `represent_grad`（阶段一、对 `prod` 求导、且 `prod` 已与参数解耦），再逐候选独立反传。它牺牲了一点梯度精确性，换来「无需同时持有全部候选激活」的大幅显存下降。

3. **问**：阶段二为什么必须「前向一个、立即 backward」，而不能「先前向 4 个、再一起 backward」？
   **答**：因为「先前向 4 个」会把 4 条候选的激活同时留存在反传图里，回到基线的显存峰值。「立即 backward」让每条候选的激活在反传后立刻释放，使任意时刻只持有 1 条——这正是省显存的全部诀窍。

### 4.4 per_device_train_batch_size=1 的显存含义与峰值对比

#### 4.4.1 概念说明

前面三节讲了「怎么拆」，这一节定量回答「为什么拆完就能用 `per_device_train_batch_size=1` 训练评分任务」。关键在于区分两种「批量」：

- **数据批量 \(B\)**（`per_device_train_batch_size`）：一次训练步处理几条「指令」。评分训练默认 \(B=1\)。
- **候选批量 \(C\)**：每条指令配几个候选代码。样例数据里 \(C=4\)。

在基线方案里，一次前向的序列数 = \(B \times C\)。\(B=1,C=4\) 时就是 4 条序列同时前向。所谓「`per_device_train_batch_size=1` 也能训练评分任务」，不是说它只前向 1 条序列，而是说**梯度切分把「同时前向的序列数」从 \(B \times C\) 压到 1**——于是即便候选数 \(C\) 较大，单步激活峰值也只相当于 1 条序列。

#### 4.4.2 核心流程（显存峰值模型）

设单条序列（长度 \(L\)）的前向激活峰值约为 \(A\)（含 attention/MLP 各层，已开 `gradient_checkpointing` 后的常数）。模型权重与优化器状态是固定开销 \(W\)（与批量无关，由 DeepSpeed ZeRO-2 切分，见 u3-l4）。则：

- **基线 `mle_scoring.py`**（默认 `training_step`，一次性前向 \(B \times C\) 条）：
  \[
  \text{峰值}_{\text{baseline}} \approx W + (B\cdot C)\cdot A
  \]
  \(B=1,C=4\) 时为 \(W + 4A\)。比较损失的逐候选 Python 循环不额外占激活，但 4 条候选的激活同时在场。

- **梯度切分 `mle_scoring_grad_split.py`**：
  - 阶段一：`get_sftscore`（按设计在 `no_grad` 下）前向 \(B \times C\) 条，但**不留存激活**，瞬时峰值 \(\approx (B\cdot C)\cdot A\) 但用完即释放，且无反传图；
  - 阶段二：任意时刻只前向-反传 **1 条**，峰值 \(\approx A\)。
  \[
  \text{峰值}_{\text{grad\_split}} \approx W + A
  \]

**省显存倍数**（就激活部分）：
\[
\frac{\text{激活峰值}_{\text{baseline}}}{\text{激活峰值}_{\text{grad\_split}}}
= \frac{B\cdot C}{1} = B\cdot C \;\;(\text{默认 }1\times4=4)
\]

即默认配置下，激活峰值降到约 1/4。这正是 README 把 `mle_scoring_grad_split.py` 列为「显存不足时的备选」、并允许更激进配置的原因。

> 注意 \(W\)（权重 + 优化器状态）通常远大于单条激活 \(A\)，所以「降到 1/4」指的是**激活部分**；总显存的下降幅度取决于 \(W\) 与 \(A\) 的比例。对大模型 + 长序列（\(A\) 大）的场景，激活占比高，收益显著。\(W\) 的优化是 DeepSpeed ZeRO-2 的职责（u3-l4），与本讲的激活优化正交。

#### 4.4.3 源码精读

`train()` 里开启梯度检查点，与基线一致（这是激活优化的另一层，与本讲正交但叠加生效）：

[train/mle_scoring_grad_split.py:350-354](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L350-L354) —— 以 `fp16` 加载模型后立刻 `model.gradient_checkpointing_enable()`，与基线 [scoring L266](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L266) 完全相同。

```python
model = transformers.AutoModelForCausalLM.from_pretrained(
    model_args.model_name_or_path, torch_dtype=torch.float16)
model.gradient_checkpointing_enable()
```

阶段二「立即反传」的那一行就是省显存的落点：

[train/mle_scoring_grad_split.py:342](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L342) —— `self.accelerator.backward(loss)` 在每个候选的循环内、紧跟其前向之后调用，使该候选的激活在反传后即可被回收，下一候选前向前峰值回落。

```python
self.accelerator.backward(loss)   # 每个候选各自反传，激活用完即释
```

#### 4.4.4 代码实践

**目标**：用一个最小模拟脚本，定量算出基线与梯度切分在给定 \((B, C, L)\) 下的**激活峰值**与**省显存倍数**，并据此画出峰值差异示意图。**不需要 GPU**，只是把 4.4.2 的模型编进代码，方便你代入真实参数确认。

```python
# 示例代码：基线 vs 梯度切分的激活峰值模拟
# 运行：python explore_grad_split_memory.py   （纯 Python，无需任何第三方库）
def peak_activations(strategy, B, C, hidden_bytes):
    """返回 (单步激活峰值, 峰值时刻同时在场的序列数)。
    hidden_bytes = 单条序列长度 L 下，一层激活的字节数（含各层累加）。
    """
    if strategy == "baseline":               # 一次性前向 B*C 条
        return (B * C) * hidden_bytes, B * C
    if strategy == "grad_split":
        # 阶段一 no_grad 前向不留存；阶段二任意时刻 1 条
        return 1 * hidden_bytes, 1

# 代入默认评分训练配置
B, C = 1, 4
hidden_bytes = 1_000_000_000   # 假设单条序列激活约 1GB（仅示意，真实值依模型/长度而定）

base_peak, base_n = peak_activations("baseline", B, C, hidden_bytes)
split_peak, split_n = peak_activations("grad_split", B, C, hidden_bytes)

print(f"基线    : 激活峰值 ≈ {base_peak/1e9:.2f} GB  (同时在场 {base_n} 条)")
print(f"梯度切分: 激活峰值 ≈ {split_peak/1e9:.2f} GB  (同时在场 {split_n} 条)")
print(f"激活峰值降到约 1/{base_n//split_n}")
```

**操作步骤**：

1. 把脚本存为项目根目录的 `explore_grad_split_memory.py`（**仅供本地观察，不要提交、不要放进 `train/`**）。
2. 运行 `python explore_grad_split_memory.py`，确认输出「基线 4.00 GB / 梯度切分 1.00 GB / 降到约 1/4」。
3. 把 `C` 改成 8（更多候选）、`B` 改成 2，重跑，观察倍数如何随 \(B\cdot C\) 增长。
4. 据此**手绘一张显存时间轴示意图**（见下方），标出两种方案的单步峰值。

**需要观察的现象**：

- 基线：峰值是一个高台（整个 `compute_loss`+`backward` 期间都维持 \(B\cdot C\) 条激活）。
- 梯度切分：阶段一是一个**窄而高**的 `no_grad` 前向峰（不留存，瞬时）、阶段二是 \(C\) 个**矮而窄**的逐候选峰（每个峰值只有 1 条）。

**单步显存峰值差异示意图（时间轴，纵向 = 激活占用）**：

```
激活占用
  ^
  |  ┌───┐                     ← 基线峰值 W+4A：4 条候选激活同时驻留
  |  │   │                        （整个 forward→backward 期间）
  |  │   │
  |  │   │        ┌┐  ┌┐  ┌┐  ┌┐  ← 梯度切分阶段二：4 个矮峰，各 1 条候选
  |  │   │        ││  ││  ││  ││     峰值 W+A（+阶段一瞬时 no_grad 峰，未画出）
  |  │   │        ││  ││  ││  ││
  |  └───┘        └┘  └┘  └┘  └┘
  +---------------------------------> 训练步内时间
     基线          逐候选 c0 c1 c2 c3
   (4条同时)       (各1条，立即反传释放)
```

**预期结果**：基线单步激活峰值 \(\approx 4A\)，梯度切分 \(\approx A\)（默认 \(B=1,C=4\)），激活部分降到约 1/4；总显存下降幅度取决于固定开销 \(W\)（权重+优化器，由 ZeRO-2 管）的占比。脚本与手绘图应与此自洽。

> 若有 GPU，可进一步用 `torch.cuda.max_memory_allocated()` 在一个真实小模型上对比「一次性前向 4 条」与「逐候选前向-反传 4 次」的实测峰值，定性结论与本模拟一致（具体数值「待本地验证」）。

#### 4.4.5 小练习与答案

1. **问**：梯度切分把「同时前向的序列数」从 \(B\cdot C\) 压到 1。这是否意味着 `per_device_train_batch_size` 可以随便开大？
   **答**：不能。阶段一 `cand_num = inputs['input_ids'].shape[0]` 把第 0 维当候选数，隐含 \(B=1\)；\(B>1\) 时第 0 维 = \(B\cdot C\)，阶段二的逐候选切分会错位（4.1.5 练习 2）。所以该方案绑定 \(B=1\)，靠 `gradient_accumulation_steps` 放大有效批量（基线评分训练也是 `B=1`、累积 64）。

2. **问**：既然阶段一也要前向所有候选，它为什么不抵消掉阶段二的省显存？
   **答**：阶段一（按设计）在 `no_grad` 下运行，前向不留存激活、不建反传图，峰值是瞬时的且远低于「带反传图的前向」。真正决定 OOM 的是「带反传图、需留存到 backward 的激活」，这部分在阶段二被压到 1 条。

3. **问**：梯度切分省的是「激活」，DeepSpeed ZeRO-2 省的是什么？两者关系？
   **答**：ZeRO-2 省的是「优化器状态」的显存（把优化器状态切分到多卡，可选 offload 到 CPU，见 u3-l4），与批量无关；梯度切分省的是「激活」，与同时前向的序列数成正比。两者正交，叠加使用：ZeRO-2 压低 \(W\)，梯度切分压低激活峰值。

## 5. 综合实践

**任务**：对比 `mle_scoring.py`（基线，默认 `training_step`）与 `mle_scoring_grad_split.py`（自定义 `training_step`）的 `compute_loss` / `training_step`，**画出两者单步显存峰值差异示意图，说明为何后者更省显存**，并用一个可运行的最小模拟定量验证。

**第一步：确认两文件的「同与异」**。用 `diff` 或并排阅读确认：

- `compute_loss`（[scoring L219-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L219-L256) vs [grad_split L250-284](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L250-L284)）、`get_comp_loss`、`compare_loss`、数据管线**逐字相同**。
- grad_split **唯一新增**的是 `training_step`（[L285-344](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L285-L344)）。

> 你可以在仓库根目录运行只读命令核对（不修改任何源码）：
> `diff <(sed -n '219,256p' train/mle_scoring.py) <(sed -n '250,284p' train/mle_scoring_grad_split.py)`
> 预期：只有空格/注释差异，逻辑一致。

**第二步：画出单步显存峰值差异示意图**。在纸上或文档里画一张「激活占用 vs 训练步内时间」的图，至少包含：

- 基线：一个高度为 \(W+4A\) 的高台，横跨整个 `compute_loss`（一次前向 4 条）→ `backward`。
- 梯度切分：阶段一一个窄的 `no_grad` 前向峰（瞬时，不留存）；阶段二 4 个高度 \(W+A\) 的矮峰（逐候选，立即反传）。
- 标注：\(W\)=权重+优化器（ZeRO-2 管），\(A\)=单条序列激活；省的是激活部分的 \(4A\to A\)。

参考示意图见 4.4.4。**核心结论一句话**：基线让 4 条候选的激活同时驻留以支撑一次耦合反传；梯度切分先把耦合方向浓缩成 4 个标量（表征梯度），再逐候选独立反传，任意时刻只留 1 条候选激活。

**第三步：用最小模拟定量验证**。把 4.4.4 的 `explore_grad_split_memory.py` 跑一遍，确认默认 \((B=1,C=4)\) 下激活峰值降到约 1/4；再改 \(C=8\) 看倍数变化。

**第四步（进阶，可选，需 GPU 且需先修瑕疵）**：按 4.1.4 的清单补全缺失符号——给 `CompareTrainer` 加一个 `get_sftscore`（在 `torch.no_grad()` 下复用 `get_comp_loss` 内部的逐候选 NLL 管线产出 `prod`）、定义 `device`、给 `make_supervised_data_module` 补 `training_args`、把阶段二的 `idxs` 整成 `(1,1)`——然后在一个小模型（如 `facebook/opt-125m`，正好是 [grad_split L59](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L59) 的默认 `model_name_or_path`）上跑一步，用 `torch.cuda.max_memory_allocated()` 对比基线与梯度切分的实测峰值。**实测数值「待本地验证」**，定性应与本讲模型一致。

> ⚠️ 第四步涉及**修改源码**。本讲的约束是「不修改源码」，所以请在你自己的**副本**上做，或仅作为理解性练习在脑中推演；不要改动仓库内的 `train/mle_scoring_grad_split.py`。

## 6. 本讲小结

- 评分训练吃显存的根源：collator 把 1 条指令的 \(C\) 个候选拍平进 `input_ids` 第 0 维，基线 `mle_scoring.py` 用默认 `training_step`，让 `compute_loss` **一次性前向 \(B\cdot C\) 条序列**，所有候选的激活同时驻留。
- `mle_scoring_grad_split.py` **唯一的新增**是覆写的 `training_step`（其余与基线逐字相同）；它把单步训练拆成两阶段：**阶段一算表征梯度**、**阶段二逐候选前向-反传**。
- **阶段一（表征梯度）**：`get_sftscore` 产出每个候选的表征得分 `prod`，`prod.requires_grad_()` 把它登记为叶子，对代理损失 `inter_loss`（比较项 `compare_loss(-prod, rw)` + 锚点项 `prod[参考]`）反传，得到紧凑的方向向量 `represent_grad = prod.grad`；模型参数在此阶段不入反传图。
- **阶段二（逐候选加权反向）**：循环每个候选，`loss = represent_grad[i] * compute_loss(model, 单候选)`，**立即** `accelerator.backward(loss)` 累加进参数梯度；任意时刻只持有 1 条候选激活。总梯度 \(\approx \sum_i \text{represent\_grad}_i\cdot \partial\text{loss}_i/\partial\theta\)，是一阶解耦近似。
- **显存含义**：激活峰值从基线的 \((B\cdot C)\cdot A\) 降到约 \(A\)（默认 \(B=1,C=4\) 时约 1/4），使 `per_device_train_batch_size=1` 训练评分任务成为可能；与 ZeRO-2（管 \(W\)）正交叠加。
- **工程现实**：shipped 的 `training_step` 缺 `get_sftscore`、`device`、`amp` 的定义/导入，`train()` 还漏传 `training_args`、阶段二 `idxs` 标量化与 `compute_loss` 不兼容——**不能直接运行**，应作为「设计清晰的带瑕疵原型」来读，机制重于断行。

## 7. 下一步学习建议

- **u3-l4 DeepSpeed ZeRO-2 与分布式训练**：本讲的显存优化针对「激活」，而 ZeRO-2 针对「权重+优化器状态 \(W\)」。下一讲读 `train/ds_stage_2.json`，理解优化器状态切分与 CPU offload 如何与本讲的激活优化正交叠加，共同把大模型评分训练塞进有限显存。
- **回头对照 u3-l1 / u3-l2**：现在再读 [train/mle_scoring.py:219-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L219-L256) 的 `compute_loss`，应能一眼看出梯度切分「保留了完全相同的损失定义，只改了何时反传、反传多少」——损失的数学（u3-l2）一个字都没变。
- **扩展阅读**：本讲的「表征梯度 / 代理变量 + 一阶解耦」与梯度累积（gradient accumulation）、梯度检查点（gradient checkpointing）、以及推荐系统里「listwise 损失的解耦近似」同属「用计算换显存/解耦」的家族；可对照阅读 HuggingFace `Trainer` 的 `training_step` 源码（本讲覆写的对象），理解 `_prepare_inputs`、`compute_loss_context_manager`、`accelerator.backward` 各自的默认行为，从而明白这段覆写「保留了什么、改写了什么」。
