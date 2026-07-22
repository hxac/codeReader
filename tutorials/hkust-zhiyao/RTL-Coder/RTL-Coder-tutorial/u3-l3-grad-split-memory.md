# 梯度切分显存优化 mle_scoring_grad_split.py

## 1. 本讲目标

u3-l1 读懂了 `CompareTrainer.compute_loss` 的「域损失 + 比较损失」乘性合成，u3-l2 把比较损失的数学（候选归一化、`diff`/`rw_diff` 差分矩阵、0.2/0.3 边距、`aval` 掩码）推到了底。但这两讲都绕开了一个工程痛点：**评分训练每个样本要同时前向多条候选序列，显存压力远大于标准 SFT**。本讲就读 `train/mle_scoring_grad_split.py` 的**梯度切分（gradient splitting）**方案来解决它。

学完本讲，你应当能够：

1. 说清基线 `mle_scoring.py` 为何吃显存：它沿用 HuggingFace 默认 `training_step`，让 `compute_loss` **一次性前向整批候选**，所有候选的激活与 `(B,C,L,V)` logits 同时驻留。
2. 看懂 `mle_scoring_grad_split.py` 覆写的 `training_step` 如何把「一次多候选前向」拆成「**表征梯度** + **逐候选前向**」两阶段。
3. 解释 **`represent_grad` 表征梯度**：把候选得分 `prod` 设成叶子张量、对代理损失 `inter_loss` 反传，得到一个紧凑的「每个候选应当朝哪个方向调整」的 C 维向量。
4. 理解阶段二的**逐候选加权反向** `represent_grad[i] * compute_loss(...)`，以及为什么它能让单步显存峰值与候选数 C **解耦**。
5. 画出基线与梯度切分两种方案的**单步显存峰值差异示意图**，定量说明后者为何更省显存、付出了什么代价。

> ⚠️ **一个必须先说的工程现实**：仓库里 shipped 的 `mle_scoring_grad_split.py` 的 `training_step` **并不能直接运行**——它调用的 `self.get_sftscore`、裸名 `device`、`amp` 在文件里都没有定义，`train()` 还漏传了参数，阶段二的 `idxs` 形状也对不上 `compute_loss`（详见 4.2.4 与 4.5）。本讲的任务是讲清这套**梯度切分的设计意图与机制**（这是真正有教学价值的部分），并对照真实源码逐行标注哪些已实现、哪些缺失。请把它当作「**带瑕疵的原型 / 设计草图**」来读，而不是可直接 `torchrun` 的成品。

## 2. 前置知识

- **评分训练的损失结构**：`loss = (compare_weight * comp_loss + 1) * domain_loss`。域损失只在最后一个候选（参考答案，`Score=1`）上做标准 SFT；比较损失跨所有候选做带边距排序。u3-l1 已讲透，本讲直接引用。
- **比较损失的数学**：`get_comp_loss` 把每个候选的「归一化对数似然」softmax 成预测相对得分；`compare_loss` 用 `diff`/`rw_diff` 差分矩阵 + `aval` 掩码（真实差 > 0.2 且预测差 < 0.3）做成对 hinge 排序。u3-l2 已推到底，本讲把它的输出当作一个「跨候选耦合的标量」来用。
- **多候选 batch 的形状**：`DataCollatorForSupervisedDataset` 把一条指令的 N 个候选拍平成 `(batch×cand, L)`，`compute_loss` 用 `idxs` 反推 `batch_size`、用 `view` 折回 `(batch, cand, L, V)`。默认 `per_device_train_batch_size=1`、候选数 C 时，一次前向的序列数就是 C。
- **训练步的显存来源**：前向时各层中间激活值（attention/MLP 输出）必须留存到反向才能算梯度，这是「训练比推理费显存」的根本原因；`gradient_checkpointing`（`model.gradient_checkpointing_enable()`）用重算换显存，但留存激活仍与「**同时**前向的序列数」成正比。
- **PyTorch 自动微分里的「叶子张量」**：对一个 `requires_grad_()` 的张量做运算再 `.backward()`，梯度会沉淀在它的 `.grad` 上，而**不会**继续往生成它的上游回传（前提是它已是叶子）。这是本讲「表征梯度」技巧的力学基础。
- **DeepSpeed / torchrun 启动**：评分系列训练用 `torchrun --nproc_per_node=4 ... --deepspeed ds_stage_2.json`（u3-l4 详讲），本讲只关注单卡单步的显存机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train/mle_scoring_grad_split.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py) | 本讲主角。`CompareTrainer` 在基线之上**额外覆写 `training_step`**（L285–L344），把单步训练拆成「阶段一表征梯度 + 阶段二逐候选反传」。其 `compute_loss` / `get_comp_loss` / `compare_loss` 与数据管线和基线**完全相同**。 |
| [train/mle_scoring.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py) | 基线方案。`CompareTrainer`（L177–L256）**没有**自定义 `training_step`，沿用 HuggingFace `Trainer` 默认实现——默认 `training_step` 会调用 `compute_loss`（L219–L256）一次性前向整批候选。 |
| README.md | L287 一句话点明本文件的存在意义：「If your gpu could't afford batch size 1 with these answer candidates, try the gradients splitting method.」 |

**两文件的唯一实质差异**：`mle_scoring_grad_split.py` 多了一个 `training_step`（L285–L344），其余代码（模型加载、数据集、collator、`compute_loss`、`get_comp_loss`、`compare_loss`）几乎逐行一致。所以本讲的全部内容都聚焦在「这个覆写的 `training_step` 到底换了什么」。

## 4. 核心概念与源码讲解

### 4.1 动机：为什么评分训练在 `batch_size=1` 也会 OOM

#### 4.1.1 概念说明

标准 SFT（u2-l7 的 `mle.py`）每个样本只前向**一条** query+response 序列，显存与序列长度成正比。而评分训练每个样本要前向**一整组**候选（一条指令配 N 个带分数的候选答案），Collator 把它们拍平成 `(B×C, L)` 一起喂给模型。

关键矛盾在于：HuggingFace 默认的 `training_step` 会调用一次 `compute_loss`，而 `compute_loss` **一次**就把 `B×C` 条序列全部前向，再**一次** `.backward()`。这意味着——

> 在整个反向传播完成之前，**所有 C 条候选的中间激活值、以及完整的 `(B, C, L, V)` logits 张量及其梯度**，必须同时驻留在显存里，无法提前释放。

于是即使把 `per_device_train_batch_size` 设到最小的 1，只要候选数 C 较大，单步峰值仍然很高。README 之所以单独给出 `mle_scoring_grad_split.py` 的命令，正是因为「batch=1 也跑不动」是评分训练的常见死法。

#### 4.1.2 核心流程

基线 `mle_scoring.py` 的单步（伪代码）：

```
默认 training_step(model, inputs):
    loss = compute_loss(model, inputs)      # ← 关键：一次前向 B×C 条
    accelerator.backward(loss)              # ← 一次反传，全候选图同时驻留
    return loss

compute_loss(model, inputs):                # mle_scoring.py L219-L256
    logits = model(input_ids)               # (B×C, L, V)  ← 激活全部留存
    logits = logits.view(B, C, L, V)        # ← (B,C,L,V) 张量 + 梯度常驻
    domain_loss = SFT(logits[:, -1])        # 只在参考候选上
    comp_loss   = get_comp_loss(logits)     # 跨所有候选，耦合整张图
    return (w*comp_loss + 1) * domain_loss
```

显存大头有两块，且都正比于 C：

1. **留存激活**：C 条序列各层中间状态，总量 \(\propto C\)。
2. **logits 张量**：形状 `(B, C, L, V)`。fp16 下单个元素 2 字节，单条候选的 logits 体积为 \(L \cdot V \cdot 2\) 字节；C 条候选再加其梯度，约 \(2 \cdot C \cdot L \cdot V \cdot 2\) 字节。

#### 4.1.3 源码精读

基线 `compute_loss` 的「一次前向 + view 折回四维」是吃显存的根因：

- [train/mle_scoring.py:223-230](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L223-L230) —— 一次 `model(...)` 前向 `B×C` 条序列得到 `logits`，随即 `view(B, C, L, V)`。这行之后，整张四维 logits 及其梯度要一直活到 `backward()` 结束。

```python
outputs = model(input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'])
logits  = outputs[0]
...
logits  = logits.view(batch_size, can_num, L, vocab)   # (B, C, L, V)
```

- [train/mle_scoring.py:253-254](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L253-L254) —— `comp_loss` 跨所有候选、`loss` 乘性合成。比较损失把所有候选的 logits **耦合进同一个反传图**，这正是「无法提前释放任一候选」的元凶。

```python
comp_loss = self.get_comp_loss(logits=logits, ...)
loss      = (self.args.compare_weight * comp_loss + 1) * domain_loss
```

- 基线 `CompareTrainer` 没有 `training_step` 方法（见 [train/mle_scoring.py:177-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L177-L256) 整个类只有 `get_comp_loss` / `compare_loss` / `compute_loss`），因此走 HuggingFace 默认实现：调用一次 `compute_loss`、一次 `backward`，全候选图同时驻留。

#### 4.1.4 代码实践

**目标**：用一张表把「候选数 C 如何撑爆显存」量化出来，建立对峰值正比于 C 的直觉。

**步骤**：

1. 取 `model_max_length = 2048`、词表大小 `V = 32000`（Mistral 量级）、fp16（2 字节）。
2. 单条候选的 logits 体积 \(= L \cdot V \cdot 2\) 字节。
3. 基线一次前向 C 条，logits + 其梯度共约 \(4 \cdot C \cdot L \cdot V\) 字节。
4. 填下表（待本地验证具体显存型号，这里只算 logits 张量本身，不含激活）：

| 候选数 C | 单条 logits | logits+梯度 (C 条) |
| --- | --- | --- |
| 1 | \(2048 \times 32000 \times 2 \approx 131\) MB | \(\approx 262\) MB |
| 4 | 131 MB | \(\approx 1.05\) GB |
| 8 | 131 MB | \(\approx 2.10\) GB |

**预期结果**：仅 logits 一项，C=8 时就要 2 GB 以上，再加上 C 条候选的留存激活，`batch_size=1` 也极易 OOM——这就是 README L287 那句话的由来。

**现象观察**：把 C 从 1 加到 8，峰值显存近似线性增长；这正是 4.4 要消灭的「峰值 ∝ C」关系。

> 若想看真实数字：在有 GPU 的机器上跑一段 `torch.cuda.max_memory_allocated()` 包裹的前向+反传，C 取 1/2/4/8，记录峰值，应看到近线性增长。本环境无 GPU，故标注「待本地验证」。

#### 4.1.5 小练习与答案

**Q1**：标准 SFT（`mle.py`）每个样本前向几条序列？评分训练呢？
**答**：标准 SFT 前向 1 条（query+response）；评分训练前向 C 条（一条指令的 C 个候选）。这就是评分训练更费显存的根本原因。

**Q2**：为什么把 `per_device_train_batch_size` 设成 1 仍可能 OOM？
**答**：因为显存大头来自「同一条指令的 C 个候选被一次性前向」，`batch_size=1` 只压住了指令条数，没压住候选数 C；logits 张量 `(1, C, L, V)` 与 C 条激活仍同时驻留。

**Q3**：`compute_loss` 里哪一行让「所有候选耦合进同一反传图」？
**答**：`comp_loss = self.get_comp_loss(logits=logits, ...)`。`get_comp_loss` 跨所有候选用 softmax 归一化算比较损失，于是任一候选的 logits 都进了同一个 `.backward()` 图，无法提前释放。

---

### 4.2 解法总览：覆写 `training_step`，把一次大反传拆成两阶段

#### 4.2.1 概念说明

梯度切分的核心思想一句话：**不要让所有候选的反传图同时存在**。

它把原来「一次前向 C 条 → 一次反传」改写成「两阶段」：

- **阶段一（表征梯度）**：前向一遍算出每个候选的「得分」`prod`，但**只用来求一个 C 维方向向量 `represent_grad`**，反传到此为止、不进入模型参数。
- **阶段二（逐候选反传）**：循环 C 次，每次只前向**一条**候选、算它自己的损失、立刻反传并释放；用阶段一得到的 `represent_grad[i]` 当作这条候选的标量权重。

这样任一时刻显存里只有**一条候选**的激活和 logits，峰值不再随 C 增长。代价是阶段二要做 C 次前向+反传（而不是 1 次批量），用「时间」换「显存」。

这是**代理梯度 / 解耦加权（surrogate gradient / decoupled weighting）**的一种写法：把跨候选的排序信号压缩进一个固定权重向量（阶段一），再在各自独立的小反传里施加（阶段二）。思路上类似 RLHF 里「先用 `no_grad` 算 advantage，再做加权 SFT」。

#### 4.2.2 核心流程

`mle_scoring_grad_split.py` 覆写的 `training_step`（伪代码，对照真实行号）：

```
training_step(model, inputs):                      # L285
    model.train()
    inputs = self._prepare_inputs(inputs)
    inputs_1 = copy.deepcopy(inputs)               # 阶段一用
    inputs_2 = copy.deepcopy(inputs)               # 阶段二用
    cand_num = inputs['input_ids'].shape[0]        # = B×C，这里 B=1 即 C

    # —— 阶段一：表征梯度 ——
    prod = self.get_sftscore(model, inputs_1)      # L309 每个候选的得分 (设计: no_grad)
    prod.requires_grad_()                          # L315 把 prod 登记为叶子
    inter_loss = w * compare_loss(-prod, rw)       # L317 代理损失（比较项）
                  + prod[argmax(rw)]               #       + 参考候选锚点项
    inter_loss.backward()                          # L318 反传，停在 prod
    represent_grad = prod.grad                     # L319 C 维方向向量

    # —— 阶段二：逐候选加权反传 ——
    for i in range(cand_num):                      # L323
        input = { 第 i 个候选的切片 }               # L324-L329 单条 (1, L)
        loss = represent_grad[i] * compute_loss(model, input)   # L332 单候选前向+损失
        self.accelerator.backward(loss)            # L342 单候选反传，图随即释放

    return print_loss.detach() / gradient_accumulation_steps   # L344 仅用于日志
```

#### 4.2.3 源码精读

- [train/mle_scoring_grad_split.py:285-306](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L285-L306) —— 覆写入口。`model.train()`、`_prepare_inputs`（把张量搬到设备）后，`deepcopy` 出两份输入分别给两个阶段用，避免互相干扰；`cand_num = inputs['input_ids'].shape[0]`，在 `batch_size=1` 时就等于候选数 C。

```python
def training_step(self, model, inputs):
    model.train()
    inputs = self._prepare_inputs(inputs)
    inputs_1 = copy.deepcopy(inputs)
    inputs_2 = copy.deepcopy(inputs)
    cand_num = inputs['input_ids'].shape[0]
```

- [train/mle_scoring_grad_split.py:323-344](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L323-L344) —— 阶段二循环：每次切出**第 i 个候选**的单条 `input_ids`，调用 `compute_loss` 算这一条的损失，乘以标量 `represent_grad[i]`，再 `accelerator.backward`。循环体每跑完一帧，该候选的计算图即被释放，下一帧才重新分配。

```python
for i in range(cand_num):
    input = {}
    input['input_ids']      = inputs_2['input_ids'][i]        # 单条 (1, L)
    input['attention_mask'] = inputs_2['attention_mask'][i]
    input['labels']         = inputs_2['labels'][i]
    input['idxs']           = inputs_2['idxs'][0][i]          # ⚠️ 见 4.5，标量化后会出错
    input['scores']         = inputs_2['scores'][0][i]
    with self.compute_loss_context_manager():
        loss = represent_grad[i] * self.compute_loss(model, input)
    ...
    self.accelerator.backward(loss)
```

- [train/mle_scoring_grad_split.py:354](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L354) —— `model.gradient_checkpointing_enable()`：与基线一致，进一步用「重算换显存」压低每条候选的留存激活 A，让阶段二的单帧峰值更小。

#### 4.2.4 代码实践

**目标**：把覆写的 `training_step` 与 HuggingFace 默认 `training_step` 逐行对照，标出「新增了什么」。

**步骤**：

1. 打开 [train/mle_scoring.py:177-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L177-L256)，确认基线 `CompareTrainer` **没有** `training_step` 方法 → 走默认实现（一次 `compute_loss` + 一次 `backward`）。
2. 打开 [train/mle_scoring_grad_split.py:285-344](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L285-L344)，把每一行归到三类之一：**阶段一** / **阶段二** / **仅日志**。
3. 在阶段二的循环体里，圈出「只持有一条候选」的三处证据：`input['input_ids'] = inputs_2['input_ids'][i]`（取单条）、`compute_loss(model, input)`（单条前向）、`accelerator.backward(loss)`（立即反传）。

**预期结果**：你会得到一张两栏对照表——左栏「默认 training_step：1 次前向 C 条、1 次 backward」；右栏「覆写 training_step：1 次 no_grad 打分 + C 次（单条前向+单条 backward）」。这正是 4.4 画显存图的依据。

**现象观察**：阶段二的 `compute_loss` 被调用了 C 次，每次输入只有 1 条候选——这就是「峰值与 C 解耦」的直接原因（但也意味着前向次数从 1 变 C，更慢）。

#### 4.2.5 小练习与答案

**Q1**：为什么阶段一、阶段二要分别用 `deepcopy` 出来的 `inputs_1` / `inputs_2`？
**答**：两个阶段会对输入做不同的切片/变形（阶段一整批、阶段二逐条）。`deepcopy` 保证阶段一对输入的改动不污染阶段二，互不干扰。

**Q2**：覆写 `training_step` 后，返回的 `print_loss.detach() / gradient_accumulation_steps` 有什么作用？
**答**：它只用于**日志展示**（让上报的 loss 与累积步数对齐），真正的梯度累积由 `accelerator`/优化器在每个 micro-step 的 `backward` 后自动累加。`detach()` 防止它被误纳入反传图。

**Q3**：阶段二用「C 次单条前向+反传」替了「1 次批量前向+反传」，代价是什么？
**答**：墙钟时间变长、GPU 利用率下降（少了批并行）；这是典型的「时间换显存」。但换来了「单步峰值与 C 无关」，让大 C 也能训。

---

### 4.3 阶段一：用 `represent_grad` 把跨候选反传图压成一个 C 维向量

#### 4.3.1 概念说明

阶段一要回答一个问题：**比较损失对模型参数的梯度，能不能不经过那张巨大的 `(B,C,L,V)` 反传图就拿到？**

直接反传不行——`get_comp_loss` 把所有候选耦合在一起。但梯度可以「分两段」算（链式法则）：

\[
\underbrace{\frac{\partial \mathcal{L}_{\text{comp}}}{\partial \theta}}_{\text{想要}}
= \underbrace{\frac{\partial \mathcal{L}_{\text{comp}}}{\partial p}}_{\text{C 维，便宜}}
\cdot \underbrace{\frac{\partial p}{\partial \theta}}_{\text{阶段二逐候选处理}}
\]

其中 \(p = (p_1,\dots,p_C)\) 是每个候选的「得分」`prod`，\(\theta\) 是模型参数。左边那个 C 维向量就是 **`represent_grad`**：它告诉你「每个候选的得分应当朝哪个方向调整，才能降低比较损失」。

关键技巧是**叶子张量**：

1. `get_sftscore` 在 `no_grad` 下前向，**不存激活**，只产出 C 个得分 `prod`。
2. `prod.requires_grad_()` 把 `prod` 登记为新的叶子张量（与生成它的前向图脱钩）。
3. 构造只依赖 `prod`（和真实分数）的代理损失 `inter_loss`，`.backward()`。因为 `prod` 是叶子，反传**停在 `prod`**、不进入模型，于是 `prod.grad` 就是那个 C 维 `represent_grad`，且这一步几乎不占显存。

效果：跨候选的排序信号被压缩进一个 C 维向量，模型参数在本阶段完全不进入反传图。

#### 4.3.2 核心流程

阶段一的损失（[L317](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L317)）：

\[
\mathcal{L}_{\text{proxy}} \;=\; w \cdot \mathrm{compare\_loss}(-p,\; s) \;+\; p_{i^*}, \qquad i^* = \arg\max_i s_i
\]

- 第一项是比较项：用 u3-l2 的 `compare_loss`，对候选得分 \(p\) 与真实分 \(s\) 做带边距排序惩罚（`-p` 的符号约定取决于 `get_sftscore` 输出的语义，详见 4.5）。
- 第二项是**锚点项**：取真实分最高（参考答案，`Score=1`）那个候选的得分 \(p_{i^*}\)，把它拉向「最优」。

反传后：

\[
g \;=\; \nabla_p \mathcal{L}_{\text{proxy}} \;\in\; \mathbb{R}^C, \qquad g = \text{represent\_grad}
\]

这个 \(g\) 是**被 detach 的**：它不再连着模型图，阶段二把它当作固定标量权重用。

> 严格说，这套代理梯度与原始 \(\nabla_\theta\big((w\mathcal{L}_{\text{comp}}+1)\mathcal{L}_{\text{dom}}\big)\) 并非逐项相等（代理把乘积形式拆成了「方向权重 × 各候选域损失」，且 \(g\) detach 后丢掉了二阶项）。它保留的是**排序方向信号**，换取了显存——这是「代理梯度」方法常见的取舍。

#### 4.3.3 源码精读

- [train/mle_scoring_grad_split.py:308-315](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L308-L315) —— 阶段一前半：`get_sftscore` 产出每个候选的得分 `prod`（设计上应 `no_grad`）；`inputs['scores'][0].reshape(-1, cand_num)` 把真实分整理成 `(1, C)`；`prod.requires_grad_()` 把 `prod` 登记为叶子。

```python
cand_num = inputs['input_ids'].shape[0]
prod     = self.get_sftscore(model=model, inputs=inputs_1)   # ⚠️ 未定义，见 4.5
inputs_score = inputs['scores'][0].reshape(-1, cand_num)
inputs_score = inputs_score.to(device)                      # ⚠️ device 未定义，见 4.5
prod         = prod.to(device)
prod.requires_grad_()                                       # 登记为叶子张量
```

- [train/mle_scoring_grad_split.py:317-320](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L317-L320) —— 阶一后半：组装代理损失 `inter_loss`（比较项 + 参考候选锚点项），`.backward()` 反传后取 `prod.grad` 为 `represent_grad`。

```python
inter_loss = self.args.compare_weight * self.compare_loss(-prod, inputs_score) \
             + prod[torch.argmax(inputs_score[0], dim=0)]
inter_loss.backward()
represent_grad = prod.grad
print_loss     = inter_loss
```

`inter_loss` 只依赖叶子 `prod` 与（被视作常量的）真实分，所以这次 backward 既便宜、又**不触碰模型参数**——模型在本阶段不入反传图，激活无须留存。

#### 4.3.4 代码实践

**目标**：用一个最小例子验证「叶子张量技巧」——对一个 `requires_grad_()` 的向量算代理损失、反传，得到它的 `.grad`，且梯度不继续往上游模型走。

**步骤**：运行下面这段**示例代码**（非项目代码，仅演示机制）：

```python
# 示例代码：演示 represent_grad 的「叶子张量」机制
import torch

# 假装 get_sftscore 在 no_grad 下算出的 4 个候选得分（已与模型图脱钩）
prod = torch.tensor([0.9, 0.4, 0.6, 1.1])     # 模型预测的候选得分
prod.requires_grad_()                          # 登记为叶子，对应 L315

rw_scores = torch.tensor([0.0, 0.0, 0.0, 1.0]) # 真实分，参考候选在 index 3

# 代理损失：把参考候选得分拉高（模拟 L317 的锚点项）
inter_loss = -prod[torch.argmax(rw_scores)]    # 最小化 -prod[参考] 等价于最大化 prod[参考]
inter_loss.backward()

represent_grad = prod.grad
print("represent_grad =", represent_grad)      # 期望: tensor([0., 0., 0., -1.])
print("是否有上游模型图被建立？", prod.is_leaf)  # True —— 反传停在 prod
```

**预期结果**：`represent_grad` 只有参考候选位置非零（方向指向「提高参考候选得分」），且 `prod.is_leaf == True` 说明反传确实停在了 `prod`、没有建立模型反传图。

**现象观察**：把 `-prod[argmax]` 换成 `+prod[argmax]`，`represent_grad` 符号翻转——这就是为什么 L317 里比较项与锚点项的符号约定会直接影响阶段二每个候选是被「加强」还是「抑制」。结合 4.5 关于 `get_sftscore` 语义未定的说明，你会理解为什么这套符号必须和 `get_sftscore` 的输出语义配套定义。

#### 4.3.5 小练习与答案

**Q1**：为什么 `prod.requires_grad_()` 之后，`inter_loss.backward()` 不会把梯度传进模型参数？
**答**：`requires_grad_()` 把 `prod` 变成叶子张量，与生成它的前向图脱钩。`inter_loss` 只依赖 `prod`，反传因此停在 `prod`、沉淀为 `prod.grad`，不进入模型。

**Q2**：`represent_grad` 的维度是多少？它编码了什么信息？
**答**：维度是候选数 C。它编码「每个候选的得分应当朝哪个方向、变多少，才能降低比较损失」——一个被 detach 的方向向量。

**Q3**：代理梯度与原始 \(\nabla_\theta\mathcal{L}\) 完全相等吗？
**答**：不完全相等。代理把乘积形式损失拆成「方向权重 × 各候选域损失」，并 detach 了 \(g\)，丢掉了二阶项。它保留排序方向、换取显存，是代理梯度方法的固有取舍。

---

### 4.4 阶段二：逐候选加权反传，让显存峰值与候选数 C 解耦

#### 4.4.1 概念说明

阶段二把阶段一得到的 `represent_grad` 当作**每个候选的固定标量权重**，逐条独立地前向+反传：

\[
\nabla_\theta \;\approx\; \sum_{i=1}^{C} g_i \,\nabla_\theta \ell_i,
\qquad
\ell_i = \mathrm{compute\_loss}(\text{候选 } i)
\]

每个 \(g_i \,\nabla_\theta \ell_i\) 在自己的循环帧里算完即释放。于是——

> 任一时刻显存里只有**一条候选**的激活与 logits `(1, L, V)`，**与候选总数 C 无关**。

这正是梯度切分的全部价值：把「峰值 ∝ C」改写成「峰值 ∝ 1」。你因此可以加大 C（更多候选 → 排序信号更丰富）而不爆显存。

#### 4.4.2 核心流程

对比两种方案的单步显存峰值（记单条候选的留存激活为 \(A\)，序列长 \(L\)，词表 \(V\)，fp16 每元素 \(b=2\) 字节）：

- **基线 `mle_scoring.py`**（一次前向 C 条、一次反传）：

\[
\text{Peak}_{\text{base}} \;\approx\; C \cdot A \;+\; 2 \cdot C \cdot L \cdot V \cdot b
\]

激活与 logits 都随 C 线性增长，且因比较损失耦合，全部要撑到 `backward()` 结束才释放。

- **梯度切分 `mle_scoring_grad_split.py`**：

\[
\text{Peak}_{\text{split}} \;\approx\; \underbrace{0}_{\text{阶段一 no\_grad，不留激活}} \;+\; \big(A \;+\; 2 \cdot L \cdot V \cdot b\big)
\]

阶段一不留激活；阶段二每帧只持有一条候选，峰值是**常数**，与 C 无关。

可视化（柱高示意单步峰值，B=1）：

```
候选数 C →           1      2      3      4      5
─────────────────────────────────────────────────────
mle_scoring.py       ▓      ▓▓     ▓▓▓    ▓▓▓▓   ▓▓▓▓▓   ← 峰值随 C 线性增长
                     └ logits (1,C,L,V) + 梯度 + 全候选激活图同时驻留

mle_scoring_grad_split.py
 阶段一(no_grad)     ·      ·      ·      ·      ·      ← 不存激活，仅留 C 个标量 prod
 阶段二(逐候选)      ▓      ▓      ▓      ▓      ▓      ← 峰值恒定，与 C 无关
                     └ 任一时刻只持有一份 (1,L,V) + 单候选激活图
```

**代价**：阶段二要做 C 次前向+反传，墙钟时间约增至原来的 C 倍量级，GPU 批并行利用率下降。时间换显存。

#### 4.4.3 源码精读

- [train/mle_scoring_grad_split.py:331-342](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L331-L342) —— 阶段二的核心两行：`represent_grad[i] * self.compute_loss(model, input)`（用阶段一的标量权重 × 当前候选的损失），随后 `self.accelerator.backward(loss)` 把这一条候选的梯度累加进模型参数。

```python
with self.compute_loss_context_manager():
    loss = represent_grad[i] * self.compute_loss(model, input)
if self.args.n_gpu > 1:
    loss = loss.mean()
if self.use_apex:
    with amp.scale_loss(loss, self.optimizer) as scaled_loss:   # ⚠️ amp 未导入，见 4.5
        scaled_loss.backward()
else:
    self.accelerator.backward(loss)
```

- 注意 `represent_grad[i]` 是一个**标量**（C 维向量第 i 个分量），它把阶段一的排序方向作为固定权重施加到这一条候选的损失上。循环每帧的 `compute_loss` 只前向 `input_ids[i]` 这一条，图用完即弃。
- [train/mle_scoring_grad_split.py:344](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L344) —— `return print_loss.detach() / self.args.gradient_accumulation_steps`：返回值仅供日志，与 `gradient_accumulation_steps` 对齐（梯度累积由 accelerator 在每帧 backward 后自动累加）。

#### 4.4.4 代码实践（本讲主实践）

**目标**：用一段可运行的**示例代码**直观对比「一次反传所有候选」与「逐候选反传」的计算图驻留差异，并画出两者的单步显存峰值示意图，说明为何后者更省显存。

**步骤 1**：运行下面这段示例代码（非项目代码，仅演示机制；CPU 即可跑，真实峰值字节需 GPU + `torch.cuda.max_memory_allocated`）。

```python
# 示例代码：对比「全候选一次反传」(路线A) 与「逐候选反传」(路线B) 的计算图驻留
import torch, torch.nn as torch_nn

V, d, L, C = 3200, 256, 64, 5          # 缩小规模便于 CPU 运行；真实项目 V≈32000, L=2048
class Tiny(torch_nn.Module):
    def __init__(s):
        super().__init__()
        s.net = torch_nn.Sequential(torch_nn.Linear(d,d), torch_nn.ReLU(), torch_nn.Linear(d,d), torch_nn.Linear(d,V))
    def forward(s, x): return s.net(x)

torch.manual_seed(0)
model = Tiny()
xs = [torch.randn(L, d) for _ in range(C)]
ys = [torch.randint(0, V, (L,)) for _ in range(C)]
weights = torch.tensor([0.1, 0.2, 0.3, 0.4, 1.0])   # 模拟 represent_grad（逐候选标量权重）

# —— 路线 A：所有候选一起前向、一起反传（对应 mle_scoring.py）——
model.zero_grad()
logits_all = torch.stack([model(x) for x in xs])     # (C, L, V) —— C 份计算图同时存在
loss_a = sum(torch_nn.functional.cross_entropy(l, y) for l, y in zip(logits_all, ys))
loss_a.backward()                                    # 全候选图一起反传
print("A: logits_all.shape =", tuple(logits_all.shape), "（C 份图同时驻留到 backward 结束）")

# —— 路线 B：逐候选前向反传、每次即释放（对应 mle_scoring_grad_split.py）——
model.zero_grad()
for i in range(C):
    logits_i = model(xs[i])                          # 单份 (L, V)
    loss_i = torch_nn.functional.cross_entropy(logits_i, ys[i])
    (weights[i] * loss_i).backward()                 # represent_grad[i] * loss_i
    del logits_i, loss_i                             # 显式释放，下一帧才重新分配
print("B: 逐候选完成，任一时刻仅持有一份 (L, V) 计算图")
```

**预期结果**：路线 A 打印 `logits_all.shape = (5, 64, 3200)`，说明 C 份计算图同时存在到 `backward()` 结束；路线 B 每帧只建一份 `(64, 3200)` 的图、用完即弃。

**步骤 2**：把 4.4.2 里的 ASCII 峰值图抄到你的笔记里，在路线 A/B 下方各标注一行解释：
- A：峰值 \(\propto C\)，因为 C 份激活 + `(C,L,V)` logits + 梯度同时驻留，且比较损失耦合使它们必须撑到反传结束。
- B：峰值 \(\propto 1\)，因为阶段一 `no_grad` 不留激活，阶段二每帧只有一份候选；`represent_grad[i]` 作为固定权重把排序信号注入各帧。

**现象观察**：把示例代码里的 `C` 从 5 调到 20，路线 A 的 `logits_all` 体积线性变大（更接近 OOM），路线 B 的单帧体积不变——这就是「峰值与 C 解耦」的直观体现。

> 真实峰值测量：把示例搬到 GPU，用 `torch.cuda.reset_peak_memory_stats()` + `torch.cuda.max_memory_allocated()` 包住路线 A/B，应看到 A 随 C 增长、B 基本持平。本环境无 GPU，标注「待本地验证」。

#### 4.4.5 小练习与答案

**Q1**：阶段二为什么能保证「峰值与 C 无关」？
**答**：因为它把 C 条候选拆成 C 次独立的「单条前向 + 单条 backward」，每次只建一份候选的计算图、用完即弃；`represent_grad[i]` 是固定标量权重，不引入跨候选耦合图。

**Q2**：`represent_grad[i] * self.compute_loss(model, input)` 里，`represent_grad[i]` 起什么作用？
**答**：它是阶段一算出的、被 detach 的标量权重，把「该候选应当被加强还是抑制、强度多少」这个排序方向信号，施加到本条候选的损失梯度上。

**Q3**：既然梯度切分更省显存，为什么不总是用它？
**答**：因为它慢——阶段二要做 C 次前向+反传而非 1 次批量，GPU 利用率下降。它是「batch=1 也 OOM 时的兜底方案」（README L287），不是默认首选；显存够时优先用更快的 `mle_scoring.py`。

---

### 4.5 工程现实：shipped 原型的缺口（读源码必看）

#### 4.5.1 概念说明

前面四节讲的是**设计意图**——机制清晰、教学价值高。但忠实地说，仓库里 shipped 的 `mle_scoring_grad_split.py` 是一份**不完整的原型**，按 README L289 的命令直接 `torchrun` 会立即报错。读这份文件必须带着批判眼光：哪些是已实现的机制，哪些是留白。下表把缺口逐一标出，目的不是否定它，而是让你**知道边界在哪、要补什么才能真正跑起来**。

#### 4.5.2 核心流程

| # | 位置 | 缺口 | 触发后果 |
| --- | --- | --- | --- |
| 1 | [L309](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L309) `self.get_sftscore(...)` | **未定义**：全仓库只有这一处调用，没有任何 `def get_sftscore` | 运行到阶段一即 `AttributeError` |
| 2 | [L312-313](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L312-L313) `inputs_score.to(device)` | 裸名 `device` **未定义**（无赋值、无导入） | `NameError` |
| 3 | [L339](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L339) `amp.scale_loss(...)` | `amp` **未导入**（文件无 `from apex import amp`） | 仅当 `self.use_apex` 为真时 `NameError`；走 `else` 分支则不影响 |
| 4 | [L379](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L379) `make_supervised_data_module(tokenizer=..., data_args=...)` | 漏传 `training_args`（函数签名 L198 要求三个参数） | `TypeError: missing argument training_args`，启动即崩 |
| 5 | [L328](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L328) `input['idxs'] = inputs_2['idxs'][0][i]` | 把 `(B,C)` 的 `idxs` 切成**标量**，但 `compute_loss` 在 [L254](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L254) 做 `inputs['idxs'][:, 0]`（要求二维） | 阶段二首次 `compute_loss` 即 `IndexError` |

#### 4.5.3 源码精读

- 缺口 1（`get_sftscore`）：用 `grep` 在整个仓库搜 `get_sftscore`，**唯一**命中就是 [L309](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L309) 的调用处，没有任何定义。按设计意图，它应当：在 `torch.no_grad()` 下对 C 条候选前向、用类似 `get_comp_loss` 的「归一化对数似然」产出每个候选的得分 `prod`（一维、长度 C），从而阶段一不留激活。
- 缺口 4（`make_supervised_data_module`）：对比 [mle_scoring.py:290](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L290) 的正确调用 `make_supervised_data_module(tokenizer=..., data_args=..., training_args=training_args)`，可见基线传了 `training_args`，而 [grad_split L379](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L379) 漏了——这是个复制粘贴遗漏。
- 缺口 5（`idxs` 形状）：阶段二把单条候选的 `idxs` 设成 `inputs_2['idxs'][0][i]`（标量），但 `compute_loss` 开头 `batch_size, _ = torch.max(inputs['idxs'][:, 0], dim=0)` 默认 `idxs` 是二维 `(B,C)`。两者不兼容，说明**阶段二本应调用一个简化版的「单候选损失」函数**，而 shipped 代码直接复用了为整批设计的 `compute_loss`。

#### 4.5.4 代码实践

**目标**：源码阅读型实践——对照上表，把 shipped 文件「补成可运行」需要做的事列成清单，验证你对机制的理解。

**步骤**：

1. 在 [mle_scoring_grad_split.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py) 里逐条核对缺口 1–5 的行号与症状（只读，不改源码）。
2. 为每个缺口写一句「最小修法」：
   - 缺口 1：补一个 `def get_sftscore(self, model, inputs)`，在 `no_grad` 下前向 C 条候选、返回长度为 C 的得分张量（可复用 `get_comp_loss` 里 `prod = -loss/mask.sum(-1)` 的归一化对数似然）。
   - 缺口 2：在 `train()` 里定义 `device = accelerator.device`（或 `model.device`）。
   - 缺口 3：若要用 apex，补 `from apex import amp`；否则确保 `use_apex=False` 走 `accelerator.backward` 分支。
   - 缺口 4：把 L379 改为 `make_supervised_data_module(tokenizer=tokenizer, data_args=data_args, training_args=training_args)`。
   - 缺口 5：给阶段二写一个「单候选域损失」函数（只前向 `(1,L)`、返回该候选的 SFT 损失），不再走整批 `compute_loss` 的 `idxs` 重排逻辑。
3. （可选）参照 u3-l1 的 `get_comp_loss`，把缺口 1 的 `get_sftscore` 草稿写出来，注意它必须在 `no_grad` 下、只返回得分而不留激活——这正是阶段一省显存的前提。

**预期结果**：你得到一份「shipped 原型 → 可运行」的修补清单，且每条修法都对应本讲讲过的一个机制点（`get_sftscore` 对应阶段一、`idxs` 修补对应阶段二、`device`/`training_args` 对应工程接缝）。

> 本实践不要求真正改源码运行（worker 规则禁止改源码），重在通过「补全」检验你是否真正理解了两阶段机制。

#### 4.5.5 小练习与答案

**Q1**：仅看缺口 1（`get_sftscore` 未定义），能否判断它「应当」在什么梯度上下文下运行？
**答**：应当 `torch.no_grad()` 下运行。因为阶段一的目标是不留激活、只产出得分 `prod`，随后 `requires_grad_()` 才把 `prod` 重新登记为叶子——若前向时存了激活，阶段一就丧失了省显存的意义。

**Q2**：缺口 5 的 `idxs` 形状问题，反映出阶段二复用 `compute_loss` 的什么设计瑕疵？
**答**：`compute_loss` 是为「整批 `(B,C,...)`」设计的（开头用 `idxs[:,0]` 反推 batch_size 并 `view` 折回四维），而阶段二每次只喂一条候选。直接复用必然形状不符；阶段二需要一个只处理单条 `(1,L)` 的简化损失函数。

**Q3**：既然 shipped 文件跑不起来，它的学习价值在哪？
**答**：它用约 60 行 `training_step` 把「梯度切分 / 代理梯度 / 解耦加权」的完整设计表达得非常清晰——叶子张量技巧、`represent_grad` 方向向量、逐候选加权反传、峰值与 C 解耦。机制本身是可复用的训练工程范式，价值远超这几处工程接缝的瑕疵。

## 5. 综合实践

把本讲四节串成一个完整任务：**给一份「梯度切分 vs 基线」的单步显存分析报告**。

1. **读两份 `compute_loss`**：打开 [mle_scoring.py:219-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L219-L256) 与 [mle_scoring_grad_split.py:250-284](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring_grad_split.py#L250-L284)，确认两者**几乎逐行相同**（唯一区别在 grad_split 多了 `training_step`）。
2. **画两张图**：
   - 调用链图：基线「默认 `training_step` → `compute_loss`（一次前向 C 条）→ `backward`」；切分「覆写 `training_step` → 阶段一 `get_sftscore`+`represent_grad` → 阶段二循环 `represent_grad[i]*compute_loss` + `backward`」。
   - 显存峰值图：抄 4.4.2 的 ASCII 图，并代入 4.1.4 的数值（C=4/8 时基线 logits 体积 vs 切分单帧体积）。
3. **写一段结论**：用峰值公式 \(\text{Peak}_{\text{base}}\propto C\) 与 \(\text{Peak}_{\text{split}}\propto 1\) 说明「为何后者能让 `per_device_train_batch_size=1` 也跑得动评分任务」，并指出代价是阶段二的 C 次前向+反传（时间换显存）。
4. **附一节「工程现实」**：列出 4.5 的 5 处缺口与最小修法，标注「shipped 不可直接运行」。

完成后，你应当能向别人讲清：这套梯度切分「换了什么、省了什么、付了什么代价、还要补什么才能跑」。

## 6. 本讲小结

- **动机**：评分训练每样本前向 C 条候选，基线 `mle_scoring.py` 沿用默认 `training_step`，一次前向整批、一次反传，所有候选激活与 `(B,C,L,V)` logits 同时驻留，导致 `batch=1` 也可能 OOM（README L287 的由来）。
- **解法骨架**：`mle_scoring_grad_split.py` 覆写 `training_step`，把单步拆成**阶段一（表征梯度）+ 阶段二（逐候选反传）**，让任一时刻显存里只有一条候选。
- **阶段一 `represent_grad`**：`get_sftscore` 在 `no_grad` 下产出候选得分 `prod`，`requires_grad_()` 登记为叶子，对代理损失 `inter_loss = w·compare_loss(-prod, rw) + prod[参考]` 反传，得到 C 维方向向量 `represent_grad = prod.grad`；模型参数在本阶段不入反传图。
- **阶段二逐候选加权反传**：循环 C 次，每次 `represent_grad[i] * compute_loss(候选 i)` 再 `accelerator.backward`，图用完即弃；单步峰值从 \(\propto C\) 降为 \(\propto 1\)，与候选数解耦。
- **代价与定位**：阶段二做 C 次前向+反传，更慢、GPU 利用率更低；它是「显存兜底方案」，显存够时优先用更快的 `mle_scoring.py`。
- **工程现实**：shipped 文件是带瑕疵的原型——`get_sftscore`/`device`/`amp` 未定义、`train()` 漏传 `training_args`、阶段二 `idxs` 标量化与 `compute_loss` 不兼容；不能直接运行，应作为「设计清晰的机制范本」来读。

## 7. 下一步学习建议

- **u3-l4（DeepSpeed ZeRO-2 与分布式训练）**：本讲只讲单卡单步的显存机制，真正的 4 卡训练靠 `ds_stage_2.json` 的 ZeRO-2 + 优化器 CPU offload 把「优化器状态/梯度」也切分掉，与本讲的「激活/logits 切分」正交、可叠加。
- **回头重读 u3-l1/u3-l2**：现在你已看到 `compute_loss` 与比较损失如何被「拆解」进两阶段，再去读 `get_comp_loss`/`compare_loss` 的数学会有更深的「它为何这样设计」的理解。
- **延伸阅读**：「代理梯度 / 解耦加权」「REINFORCE 风格的 advantage 加权 SFT」「gradient checkpointing」——这三条线索能帮你把本讲的技巧放进更大的训练工程版图。
- **动手（若有 GPU）**：参照 4.5 的修补清单，把 `get_sftscore` 与单候选损失补齐，用一个小模型（如 `facebook/opt-125m`）和 `scoring_data_sample.json` 跑通梯度切分，对比它与 `mle_scoring.py` 在同一 C 下的 `max_memory_allocated`，验证「峰值与 C 解耦」。
