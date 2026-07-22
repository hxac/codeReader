# mle_scoring.py 质量评分训练

## 1. 本讲目标

在上一讲（u2-l7）里，我们读了 `mle.py` 的标准监督微调（SFT）：一条指令配一个参考答案，模型只学「指令 → 参考代码」这一个映射。本讲往上走一步——**RTL-Coder 的核心贡献之一：基于代码质量评分的训练（scoring-based training）**。

学完本讲，你应当能够：

1. 看懂 `CompareTrainer.compute_loss` 如何把一个 batch 的多候选 logits 重排为 \((\text{batch}, \text{cand}, L, V)\)。
2. 说清「域损失（domain loss）+ 比较损失（compare loss）」两段结构：域损失只取最后一个候选（参考答案）做 SFT，比较损失跨所有候选做排序。
3. 解释 `compare_weight` 如何把两段损失加权合成，并理解为什么这种训练需要更大的显存（为下一讲 u3-l3 的梯度切分埋下伏笔）。

> 本讲聚焦「损失是怎么算出来的」。比较损失底层的数学推导（候选归一化、边距阈值）只做操作层面的介绍，完整推导留给 u3-l2。

## 2. 前置知识

- **SFT 与标签掩码**：因果语言模型训练时，损失只落在「答案」段，「指令」段用 `IGNORE_INDEX=-100` 屏蔽；`CrossEntropyLoss` 默认 `ignore_index=-100`，会自动跳过这些位置。这些在 u2-l7 已讲透。
- **多候选数据格式**：评分训练的每条样本是 `{Instruction, Input, Response[N], Score[N]}`——同一条指令配 N 条候选代码，每条候选带一个 0~1 的质量分；最后一个候选约定为参考答案，`Score=1`。这一点在 u1-l4 与 u2-l6 已建立。
- **DataCollator 的多候选展开**：u2-l6 已说明 `DataCollatorForSupervisedDataset` 把一条样本展开成 N 条 `query+response` 序列（指令段 `-100` 掩码），并额外产出 `idxs`（候选到样本的归属）与 `scores`。本讲把它当作已知，重点看这些张量在 `compute_loss` 里如何被消费。
- **张量 reshape 的几何直觉**：把一个扁平的 `(N, L)` 按行顺序「折」回 `(batch, cand, L)`，前提是候选在内存里按 `[样本0的cand0..candN, 样本1的cand0..candN, ...]` 排列——这正是 collator 的拼接顺序。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train/mle_scoring.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py) | 评分训练主脚本：`ScoreDataset`、`DataCollatorForSupervisedDataset`、`CompareTrainer`、`train()` |
| [train/scoring_data_sample.json](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/scoring_data_sample.json) | 评分训练数据样例（JSONL），每行一条 `{Instruction, Input, Response[4], Score[4]}` |
| README.md | 给出 `mle_scoring.py` 的 `torchrun` 启动命令与超参 |

关键代码点分布：

- `ScoreDataset` / `DataCollatorForSupervisedDataset`：构建多候选 batch（u2-l6 已概述，本讲只复用其输出）。
- `CompareTrainer.compute_loss`：本讲主角——前向、reshape、域损失、调比较损失、合成。
- `CompareTrainer.get_comp_loss` + `compare_loss`：比较损失的两层实现。
- `TrainingArguments.compare_weight`：控制比较损失权重的超参。

## 4. 核心概念与源码讲解

### 4.1 多候选 batch 的形成与 logits 重排

#### 4.1.1 概念说明

标准 SFT（u2-l7）里，一个 batch 的形状是 `(batch, L)`，每行一条序列。评分训练不同：**一条指令要同时喂 N 条候选代码**，所以 collator 把它们「拍平」成一个长 batch——形状变成 `(batch × cand, L)`。但在算损失时，我们又必须知道「哪些行属于同一条指令」，才能在同一组候选之间做比较。

于是问题变成：**如何从一个扁平的 `(batch×cand, L, V)` logits 里，恢复出 `(batch, cand, L, V)` 这个四维结构？** 这正是 `compute_loss` 开头要做的事，也是本讲最小模块「多候选 logits 重排」的核心。

#### 4.1.2 核心流程

1. collator 按顺序拼出 `input_ids`：`[样本0候选0, 样本0候选1, …, 样本0候选N, 样本1候选0, …]`，共 `batch × cand` 行。
2. collator 同时产出 `idxs`，形状 `(batch, cand)`，每行是 `[样本序号]*cand`，例如 batch=2、cand=4 时为 `[[0,0,0,0],[1,1,1,1]]`。
3. 模型前向，得到 logits `(batch×cand, L, V)`。
4. 用 `idxs[:, 0]` 的最大值 +1 反推 `batch_size`；用 `logits.size(0) / batch_size` 反推 `can_num`。
5. `logits.view(batch_size, can_num, L, V)` 把候选维度「折」回来。

#### 4.1.3 源码精读

`compute_loss` 的前向与重排逻辑：

[train/mle_scoring.py:223-234](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L223-L234) —— 模型前向得到 logits；`batch_size` 由 `idxs[:,0]` 的最大值 +1 反推（假设样本序号从 0 连续编号，`enumerate(instances)` 保证成立）；`can_num = logits.size(0)/batch_size` 隐含「每个样本候选数相同」的前提；最后 `view` 把候选维度折回，并切出 `logits_sft` / `lables_sft` 取**最后一个候选**（参考答案）。

```python
outputs = model(input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'])
logits = outputs[0]
batch_size, _ = torch.max(inputs['idxs'][:, 0], dim=0)
batch_size = batch_size.cpu().detach().item() + 1
can_num = int(logits.size(0) / batch_size)
...
logits = logits.view(batch_size, can_num, L, vocab)   # (batch, cand, L, V)
logits_sft = logits[:,-1,:,:]                          # (batch, L, V) 取最后候选
```

> 注意：`idxs` 在这里实际上只被用来「数 batch 有多少条」；它的 `(batch, cand)` 完整结构并未在别处使用，可理解为一个略显冗余但直观的归属表。另外，collator 用 `pad_sequence` 做**右填充**（`attention_mask` 标记真实位置），与 `train()` 里 tokenizer 的 `padding_side="left"`（服务于推理）互不冲突。

#### 4.1.4 代码实践

**目标**：直观看到「候选维度是如何被拍平、又如何能被折回」。

**步骤**：
1. 打开 `train/scoring_data_sample.json`，确认每行 `Response` 与 `Score` 都是长度 4 的列表，且 `Score[-1]==1`。
2. 在 `compute_loss` 第 230 行后临时加一行打印：`print(logits.shape, can_num, batch_size)`。
3. 用第 5 节的综合实践脚本实际跑出 collator 的输出形状来对照。

**需要观察的现象**：前向后 logits 第 0 维 = `batch × can_num`（例如 `per_device_train_batch_size=1`、cand=4 时为 4）。

**预期结果**：`can_num` 恒等于数据里每条样本的候选数（样例中为 4）；重排后第 1 维 = `can_num`。

> 该打印需在真实模型上运行（需 GPU），实际数值「待本地验证」。

#### 4.1.5 小练习与答案

1. **问**：如果某条样本只有 3 个候选、另一条有 4 个，`can_num = logits.size(0)/batch_size` 还成立吗？
   **答**：不成立。该公式隐含「每条样本候选数相同」。当前数据格式每条都是 4 个候选，所以安全；候选数不齐会直接算错 `can_num`。

2. **问**：为什么 `batch_size` 用「`idxs[:,0]` 最大值 +1」而不是直接 `len(instances)`？
   **答**：因为 `compute_loss` 拿到的是已 collate 的张量，看不到原始 `instances` 列表；只能从 `idxs` 里反推。它依赖样本序号从 0 起且连续。

3. **问**：`logits_sft = logits[:,-1,:,:]` 取最后候选，对应数据里的哪个候选？
   **答**：`Response[-1]`，即参考答案（`Score=1` 的那条）。这是评分训练把「域损失」锚定在金标准答案上的关键。

### 4.2 域损失：用最后一个候选做 SFT

#### 4.2.1 概念说明

即便引入了评分排序，模型依然要先学会「把参考答案写对」。这部分损失叫**域损失（domain loss）**，本质上就是 u2-l7 里那套标准 SFT 的交叉熵，只不过**只在最后一个候选（参考答案）上算**。可以把它理解为「保底」：无论排序学得好不好，模型对金标准答案的语言模型概率都要被推高。

#### 4.2.2 核心流程

1. 从重排后的 `logits` 切出最后一个候选 `logits_sft (batch, L, V)`、`lables_sft (batch, L)`。
2. 对 batch 里每条样本，做因果语言模型的标准 shift：`logits[..., :-1, :]` 预测 `labels[..., 1:]`。
3. 用 `CrossEntropyLoss()`（默认 `ignore_index=-100`）算每条样本的均值损失。
4. 对 batch 取均值，得到标量 `domain_loss`。

#### 4.2.3 源码精读

域损失循环逐样本计算，再取 batch 均值：

[train/mle_scoring.py:236-249](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L236-L249) —— `shift_logits/shift_labels` 是因果 LM 的标准错位；`CrossEntropyLoss()` 默认 `ignore_index=-100`，配合 collator 给指令段与填充位写入的 `-100`，使损失只落在参考答案的真实 token 上。

```python
for i in range(batch_size):
    shift_logits = logits_sft[i,:,:].unsqueeze(0)[..., :-1, :].contiguous()
    shift_labels = lables_sft[i,:].unsqueeze(0)[..., 1:].contiguous()
    loss_fct = CrossEntropyLoss()
    domain_list.append(loss_fct(shift_logits.view(-1, vocab), shift_labels.view(-1)))
domain_loss = torch.mean(torch.stack(domain_list))
```

#### 4.2.4 代码实践

**目标**：验证「域损失只来自最后一个候选、且只落在答案段」。

**步骤**：
1. 读 [train/mle_scoring.py:145-152](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L145-L152)，确认 `labels` 的构造：指令段全是 `-100`、答案段（含 `eos`）是真实 token id。
2. 在第 246 行后加打印 `print(i, loss_temp.item())`。

**需要观察的现象**：每个 `i` 只打印一次（每条样本一个域损失值），数值为正且随训练下降。

**预期结果**：域损失曲线与标准 SFT 形态一致（指数式下降后趋平）。

> 实际数值「待本地验证」。

#### 4.2.5 小练习与答案

1. **问**：域损失为什么只取最后候选，而不是所有候选都做 SFT？
   **答**：其他候选是「模型生成的、质量参差」的答案（很多编译不过、`Score` 很低）。对它们做 SFT 会把错误代码也当正例学，反而拉低质量。只对金标准（最后候选）做 SFT，把「排序学习」交给比较损失。

2. **问**：这里 `CrossEntropyLoss()` 没有显式传 `ignore_index`，为什么指令段不参与损失？
   **答**：因为 `CrossEntropyLoss` 的 `ignore_index` 默认就是 `-100`，而 collator 把指令段与填充位都写成了 `-100`，所以自动被忽略。

3. **问**：域损失循环是逐样本算再取均值，而不是直接对整个 batch 一次算。这两种方式结果一定相同吗？
   **答**：在「每条样本 token 数相同」时近似相同；但逐样本 `mean` 再取 batch 均值，等价于「按样本等权」而非「按 token 等权」。当各样本答案长度差异大时，两者会有差别。代码选择按样本等权。

### 4.3 比较损失：get_comp_loss 与 compare_loss

#### 4.3.1 概念说明

域损失只管参考答案，但评分训练的真正卖点是用「质量分」教模型**排序**：在一条指令的 N 个候选里，模型应当给「高分候选」更高的似然、给「低分候选」更低的似然。这部分损失叫**比较损失（compare loss）**。

它分两层：
- `get_comp_loss`：先把每个候选的「平均 token 对数似然」算出来，再用 softmax 在候选间归一化，得到模型对每个候选的「预测相对得分」。
- `compare_loss`：把「预测相对得分」和「真实 Score」两两做差，凡真实分差足够大、但预测分差还不够大的候选对，就罚它——一个带边距（margin）的排序损失。

#### 4.3.2 核心流程

`get_comp_loss`（对每个样本分组）：

1. 对该样本的每个候选 \(i\)，算 token 级 NLL（用 `attention_mask` 屏蔽填充），再除以 token 数得到平均 NLL。
2. 取负号得「平均对数似然」\( \text{prod}_i \)（模型越觉得该候选自然，值越大）。
3. 对 \( \{\text{prod}_i\} \) 做 softmax，得到预测相对得分 \( \hat{s}_i \)：

\[
\hat{s}_i = \frac{\exp(\text{prod}_i)}{\sum_{j} \exp(\text{prod}_j)}
\]

4. 把 \( \hat{s} \) 与真实分 \( r \) 送进 `compare_loss`。

`compare_loss`：

1. 预测差矩阵 \( D_{ij} = \hat{s}_i - \hat{s}_j \)，真实差矩阵 \( R_{ij} = r_i - r_j \)。
2. 可学样本对掩码：\( R_{ij} > 0.2 \) 且 \( D_{ij} < 0.3 \)——「真实排名里 i 明显优于 j，但模型预测的领先幅度还不够 0.3」。
3. 损失：\( \mathcal{L}_{\text{comp}} = -\sum_{(i,j)\in \text{mask}}(D_{ij} - 0.3) \)。当 \( D_{ij}<0.3 \)，\( -(D_{ij}-0.3)>0 \) 为正惩罚，梯度把 \( D_{ij} \) 往 0.3 推。

> 0.2 与 0.3 这两个边距阈值的来源、取值理由与候选归一化的完整推导，是 u3-l2 的主题，这里只做操作层面认识。

#### 4.3.3 源码精读

`get_comp_loss` 逐候选算 NLL 并 softmax 归一化：

[train/mle_scoring.py:179-209](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L179-L209) —— 关键三步：token 级 NLL（`CrossEntropyLoss(reduction="none")` + `attention_mask` 相乘，再按 token 求和）、取负除以 token 数得 `prod`、`exp` 归一化得 `prod_normalized`。

```python
loss = torch.nn.CrossEntropyLoss(reduction="none")(
    logit[..., :-1, :].contiguous().view(-1, logit.size(-1)),
    label[..., 1:].contiguous().view(-1),
).view(...)
loss = loss * mask[..., 1:].contiguous()
prod.append(-loss/mask.sum(-1))                 # 平均对数似然
prod_normalized = torch.exp(prod_tensor) / torch.sum(torch.exp(prod_tensor))
comp_loss = self.compare_loss(scores=prod_normalized, rw_scores=scores[batch_id]...)
```

`compare_loss` 用差分矩阵与掩码实现带边距的排序：

[train/mle_scoring.py:211-217](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L211-L217) —— `diff` 是预测差、`rw_diff` 是真实差；`aval` 同时要求「真实差 > 0.2」且「预测差 < 0.3」；只对被选中的样本对累加 `-(diff-0.3)`。

```python
diff = new_scores.unsqueeze(1) - new_scores.unsqueeze(-1)
rw_diff = rw_scores.unsqueeze(1) - rw_scores.unsqueeze(-1)
aval = torch.bitwise_and(rw_diff - 0.2 > 0, diff - 0.3 < 0)
return -(diff[aval] - 0.3).sum()
```

#### 4.3.4 代码实践

**目标**：理解「预测得分」如何从对数似然归一化得到，以及掩码如何挑选可学样本对。

**步骤**：
1. 取消 [train/mle_scoring.py:202-205](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L202-L205) 的注释 `print`，跑一步训练，观察 `prod_tensor` 与 `prod_normalized`。
2. 对照同一样本的 `scores[batch_id]`（真实分），看「真实分最高的候选」是否也得到最高的 `prod_normalized`。

**需要观察的现象**：训练初期，`prod_normalized` 与真实 `Score` 的排序常常不一致（这正是比较损失要纠正的）；训练后逐渐对齐。

**预期结果**：`prod_normalized` 是和为 1 的概率分布；被 `aval` 选中的样本对数随训练减少。

> 实际数值「待本地验证」。纯数学复现见 u3-l2。

#### 4.3.5 小练习与答案

1. **问**：`prod.append(-loss/mask.sum(-1))` 里，为什么取负号、又为什么除以 `mask.sum(-1)`？
   **答**：`loss` 是 NLL（正数，越小越好），取负号变成「对数似然」（越大越好）；除以真实 token 数得到「平均每个 token 的对数似然」，消除不同候选长度不同带来的偏差，使长短候选可比。

2. **问**：softmax 归一化后 \( \hat{s}_i \) 的物理含义是什么？
   **答**：它是模型「把似然质量分配给第 i 个候选」的比例，可看作模型对该候选的**预测相对质量分**，与真实 `Score` 同维可比。

3. **问**：`aval` 同时要求 `rw_diff-0.2>0` 与 `diff-0.3<0`，缺一不可。如果只保留第二个条件会怎样？
   **答**：会连「真实分本来就相近甚至更低」的候选对也去硬拉开预测差距，引入噪声甚至反向排序。第一个条件确保只在「真实排名确实有显著差距」时才施压。

### 4.4 compare_weight 加权与整体损失合成

#### 4.4.1 概念说明

最后一步是把域损失与比较损失合成一个标量交给优化器。本项目的合成方式很特别——**不是相加，而是相乘**：

\[
\mathcal{L} = (w_c \cdot \mathcal{L}_{\text{comp}} + 1)\cdot \mathcal{L}_{\text{domain}}
\]

其中 \( w_c \) 是 `compare_weight`（默认 1.0）。这里比较损失被当作域损失的**乘性放大器**：排序越差（`comp_loss` 越大），总损失整体被放大；排序学好后（`comp_loss → 0`），总损失收敛回纯域损失。

#### 4.4.2 核心流程

1. `compute_loss` 末尾把 `attention_mask` 与 `scores` 重排为 `(batch, cand, L)` 与 `(batch, cand)`。
2. 调 `get_comp_loss` 得标量 `comp_loss`。
3. 按 `(compare_weight * comp_loss + 1) * domain_loss` 合成最终 `loss`。

#### 4.4.3 源码精读

`compare_weight` 超参与最终合成：

[train/mle_scoring.py:53](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L53) —— `compare_weight: float = field(default=1.0)`，是 `TrainingArguments` 上新增字段，可在命令行用 `--compare_weight` 覆盖。

[train/mle_scoring.py:251-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L251-L256) —— 把 mask/scores 重排后调 `get_comp_loss`，再用乘性公式合成，返回 `loss`。

```python
attention_mask = inputs['attention_mask'].view(batch_size, can_num, L)
scores = inputs['scores'].view(batch_size, can_num)
comp_loss = self.get_comp_loss(logits=logits, labels=lable_reshape, attention_mask=attention_mask, scores=scores)
loss = (self.args.compare_weight * comp_loss + 1) * domain_loss
return (loss, scores) if return_outputs else loss
```

> 乘性合成有一个值得注意的耦合：当 `domain_loss` 已经很小（参考答案学得很好），即便排序仍差，乘积项也会被压小。换句话说，这个损失「优先保证参考答案、其次纠排序」。把 `compare_weight` 调大可相对提升排序项的影响。

#### 4.4.4 代码实践

**目标**：直观感受 `compare_weight` 对损失曲线的影响。

**步骤**：
1. 用 README 提供的命令启动评分训练（见第 5 节），默认 `compare_weight=1.0`。
2. 在第 254 行前加打印 `print("domain", domain_loss.item(), "comp", comp_loss.item())`。
3. 另跑一次加 `--compare_weight 0.0`（即纯域损失），对比 `loss` 曲线。

**需要观察的现象**：`compare_weight=0` 时 `loss == domain_loss`；`compare_weight=1` 时 `loss` 整体偏高且波动更大（多了排序项）。

**预期结果**：`compare_weight` 越大，排序纠偏越强，但训练也更易不稳。

> 实际数值「待本地验证」。

#### 4.4.5 小练习与答案

1. **问**：`compare_weight=0` 时，评分训练退化为哪种训练？
   **答**：退化为只在最后一个候选（参考答案）上做 SFT——即一条指令只学一个答案的标准 SFT，比较损失完全失效。

2. **问**：为什么说这个合成公式是「乘性」而非「加性」？加性写法会是什么样？
   **答**：加性写法是 `loss = domain_loss + compare_weight * comp_loss`。当前代码是 `loss = (compare_weight*comp_loss + 1)*domain_loss`，`comp_loss` 作为 `domain_loss` 的倍率出现，二者是相乘关系。

3. **问**：`return (loss, scores) if return_outputs else loss` 里的 `scores` 有什么用？
   **答**：当 HuggingFace `Trainer` 以 `return_outputs=True` 调用时（如某些评估/回调路径），除损失外还需返回 outputs；这里把原始 `scores` 一并返回。常规训练步骤只会取 `loss`。

## 5. 综合实践

**任务**：在 `scoring_data_sample.json` 上手工构造一个 batch，调用 `DataCollatorForSupervisedDataset` 打印 `input_ids / labels / idxs / scores` 的形状，亲手验证「多候选维度是如何形成的」，并对照 `compute_loss` 里的 reshape 逻辑。

下面的脚本**无需下载大模型**即可运行（用一个最小 `FakeTokenizer` 代替真实 tokenizer，仅用于观察形状；真实 tokenizer 产出的形状与之相同）。

```python
# 示例代码：观察多候选 batch 的形状
# 运行：python explore_scoring_collator.py
# 需先 pip install torch（无需 transformers/真实模型）
import json
import torch
from train.mle_scoring import DataCollatorForSupervisedDataset   # import 不会触发训练

class FakeTokenizer:
    """仅模拟 collator 用到的接口，按字符朴素分词，只为观察形状。"""
    def __init__(self):
        self.eos_token = "<EOS>"
        self.pad_token_id = 0
        self.model_max_length = 2048
    def __call__(self, text, return_tensors="pt", max_length=None, truncation=True):
        ids = [ord(c) % 1000 + 1 for c in text]   # 每个字符 -> 一个 id
        if max_length:
            ids = ids[:max_length]
        return {"input_ids": torch.tensor([ids], dtype=torch.long)}

# 取前 2 条样本作为一个 batch
with open("train/scoring_data_sample.json") as f:
    records = [json.loads(line) for line in f if line.strip()][:2]

# 复刻 ScoreDataset.__getitem__ 的「伪装」：input_ids 字段里放原始记录
instances = [dict(input_ids=rec) for rec in records]

collator = DataCollatorForSupervisedDataset(tokenizer=FakeTokenizer())
batch = collator(instances)

print("input_ids:", batch["input_ids"].shape)   # 期望 (2*4, L) = (8, L)
print("labels:  ", batch["labels"].shape)       # 期望 (8, L)
print("idxs:    ", batch["idxs"].shape, batch["idxs"].tolist())  # 期望 (2,4): [[0,0,0,0],[1,1,1,1]]
print("scores:  ", batch["scores"].shape, batch["scores"].tolist())  # 期望 (2,4)
```

**操作步骤**：

1. 把上面脚本存为项目根目录的 `explore_scoring_collator.py`（**注意：这只用于本地观察，不要提交到仓库，也不要放进 `train/`**）。
2. 运行 `python explore_scoring_collator.py`。
3. 对照输出回答：`input_ids` 第 0 维为什么是 8？`idxs` 为什么是 `(2,4)` 且每行相同？`scores` 的最后一列是不是都接近 1？
4. 把 `idxs` 的形状 `(2,4)` 与 [train/mle_scoring.py:225-230](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L225-L230) 的 `batch_size/can_num` 反推对上：`8 / 2 = 4 = can_num`，于是 `(8, L, V) → view(2, 4, L, V)`。

**需要观察的现象**：

- `input_ids` 第 0 维 = `批大小 × 候选数 = 2×4 = 8`，候选被「拍平」进同一维。
- `idxs` 形状 `(2,4)`，第 i 行全是 i，正是「第 i 个样本的 4 个候选」的归属表。
- `scores` 形状 `(2,4)`，每行最后一列 = 1（参考答案）。
- `labels` 与 `input_ids` 同形，但指令段为 `-100`（可用 `(batch['labels'][0] == -100).sum()` 验证）。

**预期结果**：形状完全对上；`labels` 中 `-100` 的数量 ≈ 该候选指令段长度。

> 真实训练命令（需 4 张 GPU）见 README：
> ```
> torchrun --nproc_per_node=4 mle_scoring.py \
>     --model_name_or_path <model path> --data_path <data path> \
>     --fp16 True --per_device_train_batch_size 1 --gradient_accumulation_steps 64 \
>     --gradient_checkpointing True --deepspeed ds_stage_2.json --model_max_length 2048
> ```
> 注意评分训练的 `per_device_train_batch_size=1`、`gradient_accumulation_steps=64`，相比 `mle.py` 的 `2 / 32` 更吃显存——因为每个样本要前向 4 条候选序列。这正是下一讲 u3-l3「梯度切分」要解决的痛点。

## 6. 本讲小结

- 评分训练的 batch 由 `DataCollator` 把「一条指令的 N 个候选」拍平成 `(batch×cand, L)`，再由 `compute_loss` 用 `idxs` 反推 `batch_size`、用 `view` 折回 `(batch, cand, L, V)`。
- **域损失**只在最后一个候选（参考答案，`Score=1`）上做标准 SFT，是「保底学写对答案」；依赖 `CrossEntropyLoss` 默认的 `ignore_index=-100` 跳过指令与填充。
- **比较损失**两层实现：`get_comp_loss` 把每个候选的平均对数似然 softmax 成「预测相对得分」，`compare_loss` 再用边距掩码（真实差 > 0.2 且预测差 < 0.3）挑出可学候选对，做带边距的排序惩罚。
- 两段损失用**乘性**公式 `loss = (compare_weight * comp_loss + 1) * domain_loss` 合成，`compare_weight` 默认 1.0；排序项充当域损失的放大器。
- 评分训练每个样本要前向多条候选，显存压力远大于标准 SFT，故 README 用 `per_device_train_batch_size=1`，并为显存不足的场景准备了梯度切分方案（u3-l3）。

## 7. 下一步学习建议

- **u3-l2 比较损失的数学原理与候选归一化**：本讲对 `get_comp_loss`/`compare_loss` 只做了操作层面介绍。下一讲深入推导 `prod_normalized` 的含义、`diff/rw_diff` 差分矩阵，以及 0.2 / 0.3 两个边距阈值与 `aval` 掩码的设计理由，并用 numpy 复现。
- **u3-l3 梯度切分显存优化 `mle_scoring_grad_split.py`**：本讲结尾提到评分训练显存吃紧。下一讲读它如何覆写 `training_step`、把一次多候选前向拆成逐候选前向 + 表征梯度，让 `per_device_train_batch_size=1` 也能跑。
- **建议同时对照阅读**：[train/mle.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py) 的 `preprocess` 与 `DataCollator`，比较「单候选」与「多候选」两套数据管线的异同，能加深对 `compute_loss` 里 reshape 的理解。
