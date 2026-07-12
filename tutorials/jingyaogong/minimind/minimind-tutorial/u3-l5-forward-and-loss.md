# CausalLM 前向传播与交叉熵损失

## 1. 本讲目标

学完本讲你应能：

- 说清一次 `forward` 调用从 `input_ids` 到 `loss` 经过的每一站：`embed → blocks → norm → lm_head → 位移交叉熵`。
- 解释 `logits_to_keep` 切片为什么是一个效率钩子，以及为什么默认值 `0` 反而代表「保留全部位置」。
- 用数学公式写出位移交叉熵，并解释 `ignore_index=-100` 如何实现 SFT 的「只对回答算 loss」。
- 解释 `aux_loss` 如何在 `MiniMindModel.forward` 里被聚合成一个标量，以及 Dense 模型时它为何恰好是 0。
- 在训练脚本里看懂 `loss = res.loss + res.aux_loss` 的组合，以及日志中 `loss / logits_loss / aux_loss` 三者的口径关系。

## 2. 前置知识

- **下一词预测（next-token prediction）**：语言模型在每个位置 i，依据前 i 个 token 预测第 i+1 个 token，这是预训练和 SFT 共同的训练目标。
- **交叉熵（Cross-Entropy）**：衡量模型给出的概率分布与「真实下一个词」这个确定性分布的距离，是分类任务的标准损失。
- **位移（shift）**：要把「位置 i 预测位置 i+1」这件事套进一次矩阵运算，做法是把 logits 砍掉最后一个时间步、labels 砍掉第一个时间步，再逐位对齐。
- **ignore_index=-100**：PyTorch 交叉熵里被忽略的标签值；u2-l2 里 `SFTDataset` 就是用 -100 把「提问 / padding」位置屏蔽，只留「回答」位置参与 loss。
- **aux_loss（负载均衡损失）**：u3-l4 讲过，MoE 里为了让 token 均匀分到各专家而加的辅助损失；本讲只关心它如何被**收集、聚合、并与主损失相加**，不重复它的推导。

## 3. 本讲源码地图

| 文件与行号 | 作用 |
|---|---|
| model/model_minimind.py:209-232 | `MiniMindModel.forward`：躯干前向，输出 hidden_states 并聚合 aux_loss |
| model/model_minimind.py:245-253 | `MiniMindForCausalLM.forward`：外壳前向，lm_head + 位移交叉熵 + 打包返回 |
| model/model_minimind.py:5 | 从 transformers 借用 `MoeCausalLMOutputWithPast` 作为统一返回容器 |
| trainer/train_pretrain.py:36-55 | 训练侧 `res.loss + res.aux_loss` 的组合，以及 `logits_loss/aux_loss` 的日志分解 |

## 4. 核心概念与源码讲解

### 4.1 MiniMindModel.forward：躯干如何把 token 变成 hidden_states 并聚合 aux_loss

#### 4.1.1 概念说明

`MiniMindModel` 是「躯干」：它把 token id 序列变成连续的隐藏向量序列，但**不算损失，也不产生词表 logits**。它的输出是 `[batch, seq, hidden]` 的 hidden_states，再往下游交给 `lm_head`。躯干同时承担两件「顺带」的工作：收集每一层的 KV Cache（供增量推理复用）、把所有 MoE 层的 `aux_loss` 加总成一个标量。Dense 和 MoE 共用同一份躯干代码，区别只在每层 `MiniMindBlock` 内部用的是 `FeedForward` 还是 `MOEFeedForward`（见 u3-l4）。

#### 4.1.2 核心流程

```
input_ids (B, T)
   │ embed_tokens: [vocab, hidden] 查表
   ▼
hidden_states (B, T, hidden)
   │ dropout
   │ 按 start_pos 切出 cos/sin（RoPE，见 u3-l3）
   ▼
for 每一层 MiniMindBlock:            ← Pre-Norm + 注意力残差 + FFN 残差
    hidden_states, present = layer(hidden_states, position_embeddings,
                                   past_key_value, use_cache, attention_mask)
    presents.append(present)         ← 收集 KV Cache
   ▼
末层 RMSNorm
   ▼
aux_loss = Σ(所有 MOEFeedForward 层的 aux_loss)   ← Dense 时为 0
return (hidden_states, presents, aux_loss)
```

关键点：`start_pos` 由 KV Cache 的已有长度推断，这让同一段代码既支持整段训练（`start_pos=0`），也支持增量解码（`start_pos=已缓存长度`）。

#### 4.1.3 源码精读

`MiniMindModel.forward` 的返回值是一个三元组 `(hidden_states, presents, aux_loss)`：

[model/model_minimind.py:209-232](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L209-L232) —— 躯干前向：embed → 逐层 Block → 末层 norm，最后把所有 MoE 层的 aux_loss 加总。

聚焦其中三段最关键的代码：

[model/model_minimind.py:213-214](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L213-L214) —— 由 KV Cache 长度推断 `start_pos`，再做 embedding 查表。整段训练时 `past_key_values` 为 None，`start_pos=0`。

[model/model_minimind.py:219](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L219) —— 按 `[start_pos : start_pos + seq_length]` 切出当前这批位置需要的 RoPE cos/sin，配合 KV Cache 实现「位置不重排」的增量解码。

[model/model_minimind.py:231](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L231) —— aux_loss 聚合（本讲最重要的「一行兼容 Dense/MoE」）：

```python
aux_loss = sum([l.mlp.aux_loss for l in self.layers if isinstance(l.mlp, MOEFeedForward)],
               hidden_states.new_zeros(1).squeeze())
```

- 对 MoE：把每一层 `MOEFeedForward.aux_loss`（u3-l4 里用 \(N\cdot c\cdot \sum f_i P_i\) 算出的负载均衡损失）累加。
- 对 Dense：没有任何层是 `MOEFeedForward`，列表为空，`sum` 返回初值 `hidden_states.new_zeros(1).squeeze()`——一个与 hidden_states 同设备、同 dtype 的标量 0。
- 这一行让 Dense 和 MoE 走完全相同的下游代码，无需任何 if 分支。

#### 4.1.4 代码实践

实践目标：直接调用躯干 `clf.model(...)`，确认它返回的是 hidden_states 而不是 logits，并观察 Dense/MoE 下 aux_loss 的差别。

操作步骤（示例代码，非项目原有文件）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

ids = torch.randint(0, 6400, (1, 8))
for use_moe in [False, True]:
    cfg = MiniMindConfig(hidden_size=512, num_hidden_layers=2, use_moe=use_moe)
    clf = MiniMindForCausalLM(cfg).eval()
    with torch.no_grad():
        h, presents, aux = clf.model(ids)   # 直接调躯干
    print(use_moe, tuple(h.shape), len(presents), float(aux))
```

需要观察的现象：

- `h.shape` 应为 `(1, 8, 512)`，即隐藏向量，**不是**词表维度。
- `len(presents)` 等于层数（这里是 2），每个元素是那一层的 (key, value)。
- Dense 时 `aux` 为 `0.0`；MoE 时为一个正的小数。

预期结果：Dense 输出 `aux=0.0`，MoE 输出 `aux>0`。具体数值待本地验证。

#### 4.1.5 小练习与答案

**Q1**：如果把 `clf.model(ids)` 换成 `clf(ids)`（直接调外壳）会怎样？
**A1**：外壳 `forward` 会再调用躯干，然后多走一步 `lm_head`，返回的是 `MoeCausalLMOutputWithPast` 容器而不是三元组；要拿隐藏向量得用 `outputs.hidden_states`。

**Q2**：为什么 `aux_loss` 的初值要用 `hidden_states.new_zeros(...)` 而不是直接写 `0`？
**A2**：要保证它是一个「和 hidden_states 同设备、同 dtype」的 tensor，在后续 `res.loss + res.aux_loss`（train_pretrain.py:37）以及 autocast / DDP 环境下类型一致；裸 Python `0` 在混合精度里可能引发隐式类型问题。

---

### 4.2 MiniMindForCausalLM.forward：lm_head、logits_to_keep 与位移交叉熵

#### 4.2.1 概念说明

`MiniMindForCausalLM` 是套在躯干外的「外壳」，多了一个 `lm_head`（`[hidden, vocab]` 的线性层），把 hidden_states 映射成词表大小的 logits。当调用者传入 `labels` 时，外壳顺手算出位移交叉熵损失；不传 labels（纯推理）时则不算损失。外壳还提供一个效率钩子 `logits_to_keep`：只对真正需要的尾部若干个位置跑 lm_head，省掉大块 `[hidden × vocab]` 矩阵乘。

#### 4.2.2 核心流程

```
input_ids, labels?
   │ self.model(...)  ← 调躯干，拿 (hidden_states, past_kv, aux_loss)
   ▼
按 logits_to_keep 切 hidden_states 的尾部
   │ lm_head: [hidden, vocab]
   ▼
logits (B, T', vocab)        ← T' = 保留的位置数
   │ 若 labels is None：跳过损失，loss = None
   │ 若 labels 给定：
   │     x = logits[:, :-1, :]   （位置 0..T'-2 的预测）
   │     y = labels[:, 1:]       （位置 1..T'-1 的目标）
   │     loss = cross_entropy(x, y, ignore_index=-100)
   ▼
打包成 MoeCausalLMOutputWithPast 返回
```

位移交叉熵的数学形式（单条样本、单个位置）：

\[
\mathcal{L}_{\text{CE}} = -\log p_\theta(w_{i+1}\mid w_{\le i}) = -z_{w_{i+1}} + \log\sum_{j=1}^{V}\exp(z_j)
\]

其中 \(z\) 是该位置的 logits 向量，\(V\) 是词表大小（MiniMind 取 6400）。整条序列的 loss 是所有「非 -100 位置」的均值。

#### 4.2.3 源码精读

[model/model_minimind.py:245-253](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L245-L253) —— 外壳前向全文只有 9 行，是 MiniMind 最精简的核心之一。

逐行解读：

[model/model_minimind.py:246](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L246) —— 调躯干拿三元组，`aux_loss` 顺带透传出来。

[model/model_minimind.py:247-248](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L247-L248) —— `logits_to_keep` 切片 + lm_head：

```python
slice_indices = slice(-logits_to_keep, None) if isinstance(logits_to_keep, int) else logits_to_keep
logits = self.lm_head(hidden_states[:, slice_indices, :])
```

注意一个反直觉点：`logits_to_keep=0` 时，`slice(-0, None)` 等于 `slice(0, None)`，即「保留全部位置」而**不是**「一个都不保留」。因此：

- **训练时**必须用默认的 0（保留全部），因为位移交叉熵需要每个位置的 logits。
- **推理 prefill 时**如果只关心最后一个位置的下一个词，可传 `logits_to_keep=1`，让 lm_head 只算 1 个位置而不是整段，省下大量 `[hidden × vocab]` 乘法。
- 内置 `generate`（见 u3-l6）每步只喂 1 个新 token，seq 本身就是 1，所以不传也无所谓。

[model/model_minimind.py:250-252](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L250-L252) —— 位移交叉熵：

```python
if labels is not None:
    x, y = logits[..., :-1, :].contiguous(), labels[..., 1:].contiguous()
    loss = F.cross_entropy(x.view(-1, x.size(-1)), y.view(-1), ignore_index=-100)
```

- `x = logits[..., :-1, :]`：用位置 `0..T-2` 的隐藏状态去「预测」。
- `y = labels[..., 1:]`：目标是位置 `1..T-1` 的真实 token。
- 对齐后，位置 i 的 logit 正好对着第 i+1 个真实 token，实现了 next-token prediction。
- `ignore_index=-100` 让 labels 里被标 -100 的位置（u2-l2 里 SFT 的提问段、padding）自动从均值里剔除。

#### 4.2.4 代码实践

实践目标：验证「只有 labels ≠ -100 的位置才影响 loss」。

操作步骤（示例代码）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

torch.manual_seed(0)
cfg = MiniMindConfig(hidden_size=256, num_hidden_layers=1)
clf = MiniMindForCausalLM(cfg).eval()
ids = torch.randint(0, 6400, (1, 6))

# 情况 A：只有最后一个位置参与 loss
lab_A = torch.full_like(ids, -100); lab_A[0, -1] = ids[0, -1]
# 情况 B：全部位置参与 loss（位移后是 5 个位置）
lab_B = ids.clone()

with torch.no_grad():
    print("A", clf(ids, labels=lab_A).loss.item())
    print("B", clf(ids, labels=lab_B).loss.item())
```

需要观察的现象：A 的 loss 只由 1 个位置决定，B 的 loss 是 5 个位置（位移后 `ids[1:]` 长度 5）的均值，两者数值通常不同。

预期结果：两次 loss 不同；若把 `lab_A` 里那个非 -100 位置挪到别的下标，loss 数值会随之变化。具体数值待本地验证。

#### 4.2.5 小练习与答案

**Q1**：为什么是 `logits[:-1]` 对 `labels[1:]`，而不是反过来？
**A1**：语言模型用「前面」预测「后面」，位置 i 的 logit 要对齐到第 i+1 个真实词；所以砍 logits 尾、砍 labels 头，让两者错开一位。

**Q2**：如果训练时误传了 `logits_to_keep=1`，会发生什么？
**A2**：`lm_head` 只算最后 1 个位置的 logits，`x = logits[:-1]` 会变成空，交叉熵要么报错要么 loss 失去意义。所以训练必须用默认 0。

---

### 4.3 MoeCausalLMOutputWithPast：统一返回容器与 CE+aux_loss 组合

#### 4.3.1 概念说明

`MoeCausalLMOutputWithPast` 是 transformers 提供的「带名字字段」的返回容器（一个 dataclass-like 对象）。MiniMind 哪怕是 Dense 模型也借用这个 MoE 风格的类型，好处是：下游训练 / 推理代码只需认这一种返回结构，不用为 Dense / MoE 写两套。它把一次 forward 的全部产物——主损失、辅助损失、logits、KV Cache、隐藏状态——打包在一起返回。

#### 4.3.2 核心流程

```
forward 返回:
  loss              ← 交叉熵（labels 为 None 时是 None）
  aux_loss          ← 躯干聚合的负载均衡损失（Dense 时为 0）
  logits            ← lm_head 输出
  past_key_values   ← 每层 KV Cache 列表
  hidden_states     ← 躯干输出的隐藏向量

训练侧 (train_pretrain.py):
  res = model(input_ids, labels=labels)
  loss = res.loss + res.aux_loss        ← Dense: CE+0；MoE: CE+aux
  loss.backward()
```

Dense 与 MoE 的差异，在训练循环里**完全不可见**：因为 Dense 的 aux_loss 恒为 0，同一行 `res.loss + res.aux_loss` 自动退化为纯 CE。

#### 4.3.3 源码精读

[model/model_minimind.py:5](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L5) —— 从 transformers 直接 import 这个返回类型，不自造 dataclass。

[model/model_minimind.py:253](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L253) —— 把全部产物打包返回：

```python
return MoeCausalLMOutputWithPast(
    loss=loss, aux_loss=aux_loss, logits=logits,
    past_key_values=past_key_values, hidden_states=hidden_states)
```

注意 `loss` 在不传 labels 时是 `None`（[L249](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L249) 初始化为 None），这是推理路径的常态。

训练侧的组合与日志分解：

[trainer/train_pretrain.py:36-37](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L36-L37) —— 把主损失与辅助损失相加，再按梯度累积步数缩放：

```python
res = model(input_ids, labels=labels)
loss = res.loss + res.aux_loss
loss = loss / args.accumulation_steps
```

[trainer/train_pretrain.py:54-55](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L54-L55) —— 日志里把总 loss 拆回纯 CE 项，方便观察语言建模本身是否在收敛：

```python
current_aux_loss = res.aux_loss.item() if res.aux_loss is not None else 0.0
current_logits_loss = current_loss - current_aux_loss
```

所以训练日志（[L58](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L58)）里的三个词含义是：

- `loss` = CE + aux（真正回传的总损失）。
- `logits_loss` = CE（用 `loss - aux` 反推得到的纯语言建模损失）。
- `aux_loss` = 负载均衡损失，Dense 时恒为 0，MoE 时是个很小的正数（系数 `router_aux_loss_coef=5e-4` 已压得很低，见 u3-l4）。

#### 4.3.4 代码实践（本讲主任务）

实践目标：构造一段 `input_ids + labels`，分别用 Dense 与 MoE 配置调用 `forward`，打印 `loss / aux_loss / total`，验证 aux_loss 对总损失的贡献。

操作步骤（示例代码）：

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

torch.manual_seed(42)
ids = torch.randint(0, 6400, (2, 16))
labels = ids.clone()
labels[:, :8] = -100   # 模拟 SFT：前半段（提问）不算 loss，后半段（回答）才算

for use_moe in [False, True]:
    cfg = MiniMindConfig(hidden_size=512, num_hidden_layers=2, use_moe=use_moe)
    clf = MiniMindForCausalLM(cfg).eval()
    with torch.no_grad():
        res = clf(ids, labels=labels)
    ce, aux = res.loss.item(), res.aux_loss.item()
    print(f"use_moe={use_moe:5}: CE(loss)={ce:.4f}, aux_loss={aux:.6f}, "
          f"total(CE+aux)={ce+aux:.4f}, logits.shape={tuple(res.logits.shape)}")
```

需要观察的现象：

- Dense：`aux_loss` 严格为 `0.000000`，`total` 与 `CE` 完全相等。
- MoE：`aux_loss` 是一个很小的正数（量级通常在 1e-3 ~ 1e-2，因 `router_aux_loss_coef=5e-4` 已缩放）；`total = CE + aux` 略大于 `CE`。
- 两种配置的 `logits.shape` 都应是 `(2, 16, 6400)`（默认 `logits_to_keep=0` 保留全部位置）。

预期结果：Dense 的 `total - CE == 0`；MoE 的 `total - CE == aux_loss > 0`，与训练脚本 `loss = res.loss + res.aux_loss` 的口径一致。具体数值待本地验证。

#### 4.3.5 小练习与答案

**Q1**：为什么 Dense 模型也能用 `MoeCausalLMOutputWithPast` 而不出错？
**A1**：这个类型只是一组命名字段，不强制 aux_loss 非 0；Dense 时 aux_loss 是躯干返回的标量 0，字段照常填充。

**Q2**：训练日志里 `logits_loss` 和 `loss` 哪个更能反映「模型语言能力」？
**A2**：`logits_loss`（纯 CE）。`loss` 里混入了 MoE 路由约束项 aux_loss，后者并非语言建模误差；看语言能力时应剥离它。

---

## 5. 综合实践

把本讲三站串起来：自己造一份「SFT 风格」的标签（前半 -100、后半有效），分别跑 Dense 和 MoE 两个模型，逐字段打印返回容器，并手算验证 loss 口径。

```python
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM, MOEFeedForward

torch.manual_seed(7)
ids = torch.randint(0, 6400, (1, 10))
labels = ids.clone()
labels[:, :5] = -100        # 前 5 个是「提问」，后 5 个是「回答」

for moe in [False, True]:
    cfg = MiniMindConfig(hidden_size=384, num_hidden_layers=3,
                         use_moe=moe, num_experts=4, num_experts_per_tok=1)
    clf = MiniMindForCausalLM(cfg).eval()
    with torch.no_grad():
        res = clf(ids, labels=labels)

    # 1) 躯干产物：统计有多少个 MoE 层在贡献 aux_loss
    n_moe_layers = sum(1 for l in clf.model.layers if isinstance(l.mlp, MOEFeedForward))
    # 2) 外壳产物
    print(f"--- use_moe={moe} ---")
    print("hidden_states:", tuple(res.hidden_states.shape))
    print("logits      :", tuple(res.logits.shape), " (logits_to_keep=0 → 全部位置)")
    print("loss(CE)    :", round(res.loss.item(), 4))
    print("aux_loss    :", round(res.aux_loss.item(), 6), f" (来自 {n_moe_layers} 个 MoE 层)")
    print("total       :", round((res.loss + res.aux_loss).item(), 4))
```

需要观察与思考：

1. 两种配置 `hidden_states` 形状一致 `(1, 10, 384)`，`logits` 形状一致 `(1, 10, 6400)`——说明 Dense / MoE 在接口上完全一致。
2. Dense 的 `n_moe_layers=0`，`aux_loss=0`；MoE 的 `n_moe_layers=3`，`aux_loss>0`。
3. 验证 `total == loss + aux_loss`，与 train_pretrain.py:37 的口径一致。
4. 把 `labels[:, :5] = -100` 改成 `labels[:, :8] = -100`（只留 2 个有效位置），观察 `loss(CE)` 数值变化——有效位置越少，单个位置对 loss 的权重越高。

预期结果：上述 4 点全部成立。具体 loss 数值待本地验证。

## 6. 本讲小结

- `MiniMindModel.forward`（躯干）只产出 hidden_states、KV Cache 和聚合后的 aux_loss，**不算损失也不出 logits**；Dense / MoE 共用同一份代码。
- aux_loss 的聚合（L231）用一行 `sum(..., new_zeros)` 同时兼容两种模型：Dense 返回标量 0，MoE 返回各层负载均衡损失之和。
- `MiniMindForCausalLM.forward`（外壳）多了一步 `lm_head`，把 hidden_states 变成词表 logits；`logits_to_keep=0` 表示「保留全部位置」（反直觉），训练时必须如此。
- 位移交叉熵用 `logits[:-1]` 对 `labels[1:]` 实现 next-token prediction，`ignore_index=-100` 屏蔽掉提问 / padding，对应 u2-l2 的 loss_mask 思路。
- `MoeCausalLMOutputWithPast` 是 Dense / MoE 统一的返回容器；训练侧 `loss = res.loss + res.aux_loss`，Dense 自动退化为纯 CE。
- 训练日志里 `loss / logits_loss / aux_loss` 分别是「总损失 / 纯 CE / 负载均衡」，关系为 `logits_loss = loss - aux_loss`。

## 7. 下一步学习建议

- 下一讲 **u3-l6 自定义 generate** 会用到本讲的 `forward` 与 `past_key_values`，讲解自回归生成循环、采样与流式输出，重点看 KV Cache 如何一步一步累积。
- 想深入 aux_loss 的来源，回看 **u3-l4** 的 `MOEFeedForward`（L171-L175）里 Switch Transformer 负载均衡损失的推导。
- 想看 loss 在真实训练里如何回传，进入 **u5-l1 预训练**，结合 `train_pretrain.py` 的 `train_epoch` 看 autocast、梯度累积与 `res.loss + res.aux_loss` 的完整协作。
