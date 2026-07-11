# SwiGLU 前馈网络与 MoE 路由

> 前置承接：本讲建立在 u3-l1（`MiniMindConfig` 与模型骨架）、u3-l2（`RMSNorm` 与 `Attention`）、u3-l3（`RoPE`）之上。你已经知道一个 `MiniMindBlock` 由「注意力 + 前馈」两段组成，本讲专门拆解**前馈这一段**：Dense 模型用 `FeedForward`（SwiGLU），MoE 模型把它升级成 `MOEFeedForward`（多专家路由），以及它们如何挂在 `MiniMindBlock` 上、`aux_loss` 如何一路传递到训练循环。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **SwiGLU** 为什么有三个投影（`gate_proj` / `up_proj` / `down_proj`），以及 `act_fn(gate(x)) * up(x)` 这一步在算什么。
- 看懂 **`MOEFeedForward`** 的完整路由流程：`softmax(gate)` → `topk` → `norm_topk_prob` → 按专家分桶 → `index_add_` 回填。
- 解释 `aux_loss`（负载均衡损失）的公式来源，以及它为何只在训练模式下计算。
- 说清「4 experts / top-1 为什么只比 dense 慢约 50%」背后的计算量与 kernel 开销权衡。
- 动手实例化一个 `use_moe=True` 的 `MiniMindBlock`，打印 `aux_loss`、对比参数量与推理耗时。

## 2. 前置知识

- **前馈网络（FFN）**：Transformer 每一层在注意力之后都有一个「逐位置」的两层 MLP，把每个 token 的隐藏向量先放大到一个更宽的中间维、再投回原维度。它不跨 token 交互，只做单 token 的非线性变换。
- **门控（Gating）**：与其让信息无脑通过，不如让模型自己决定「哪些维度该通过」。门控就是用一个分支产生 0~1 之间的系数去乘另一个分支，从而动态抑制或放大某些特征。
- **MoE（Mixture of Experts，混合专家）**：把一个大 FFN 换成若干个并行的小 FFN（专家），再训练一个「路由器（router/gate）」为每个 token 挑选最合适的 1 个或几个专家。这样**总参数量成倍增加、但每个 token 实际激活的参数不变**，用「稀疏激活」换取更大模型容量。
- **负载均衡（Load Balancing）**：如果路由器偷懒把所有 token 都送给同一个专家，其它专家就训练不到、模型退化成 dense。需要一个辅助损失 `aux_loss` 鼓励 token 均匀分到各专家。
- **`-100` 与 `index_add_`**：在 u2-l2 见过 `-100` 用作 loss 掩码；这里会遇到 `index_add_`，它是 PyTorch 里「按索引累加」的原地操作，MoE 用它把各专家的输出回填到对应 token 位置。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
|------|---------|------|
| `model/model_minimind.py` | `FeedForward`（L136-L146） | Dense 模型的 SwiGLU 前馈层 |
| `model/model_minimind.py` | `MOEFeedForward`（L148-L176） | MoE 的多专家路由前馈层，含 `aux_loss` |
| `model/model_minimind.py` | `MiniMindBlock`（L178-L194） | 把 `Attention` + `FeedForward`/`MOEFeedForward` 组装成一个 Transformer 块 |
| `model/model_minimind.py` | `MiniMindModel.forward`（L231） | 把各 MoE 层的 `aux_loss` 求和 |
| `model/model_minimind.py` | `MiniMindConfig` MoE 字段（L40-L45） | 专家数、top-k、`aux_loss` 系数等 |
| `trainer/train_pretrain.py` | `loss = res.loss + res.aux_loss`（L37） | 训练时把主损失与负载均衡损失相加 |
| `README.md` | 结构章节（L559、L563-L566） | SwiGLU 与 `4 experts / top-1` 甜点配置的官方说明 |

## 4. 核心概念与源码讲解

### 4.1 FeedForward：SwiGLU 前馈网络

#### 4.1.1 概念说明

`FeedForward` 是 Dense 模型（`use_moe=False`）每个 `MiniMindBlock` 里非注意力那段。MiniMind 用的是 **SwiGLU**（Swish-Gated Linear Unit），它和最朴素的 FFN 的区别在于：朴素 FFN 只有一条支路 `down(act(up(x)))`，而 SwiGLU 有**两条并行支路**，一条过激活函数（gate），一条不过（up），两条逐元素相乘后再投回原维度。

直觉上：gate 支路生成一个「门控信号」决定 up 支路的哪些特征能通过，相当于让模型在每个 token 上动态选择激活哪些维度。经验上 SwiGLU 比普通的 ReLU/GELU FFN 效果更好，代价是多了一个投影矩阵（参数量从 2 个变成 3 个矩阵）。

#### 4.1.2 核心流程

记隐藏维度为 \(d\)（默认 768），中间维度为 \(d_{\text{ff}}\)（默认 2432，见 u3-l1：\(d_{\text{ff}}=\lceil d\pi/64\rceil\cdot 64\)）。SwiGLU 的数学形式为：

\[
\text{SwiGLU}(x)=W_{\downarrow}\Big(\text{SiLU}(W_{\text{gate}} x)\odot W_{\uparrow} x\Big)
\]

其中：

- \(W_{\text{gate}},W_{\uparrow}\in\mathbb{R}^{d_{\text{ff}}\times d}\) 是两条并行支路的权重；
- \(W_{\downarrow}\in\mathbb{R}^{d\times d_{\text{ff}}}\) 把中间维投回 \(d\)；
- \(\odot\) 是逐元素乘；
- \(\text{SiLU}(x)=x\odot\sigma(x)\)，\(\sigma\) 是 sigmoid，SiLU 又叫 Swish。

伪代码：

```
g = act_fn(gate_proj(x))   # (..., d_ff)，过 SiLU，作门控
u = up_proj(x)             # (..., d_ff)，线性，不过激活
h = down_proj(g * u)       # 逐元素相乘后投回 d
```

> 小知识：因为 SwiGLU 有 3 个矩阵而朴素 FFN 只有 2 个，为了把总参数量控制在和「4× 扩展」的朴素 FFN 差不多的水平，业界通常把中间维缩到约 \(8d/3\)。MiniMind 取了一个略大一点、且圆整到 64 的倍数（利于张量核）的值 \(d_{\text{ff}}\approx \pi d\)。

#### 4.1.3 源码精读

[model/model_minimind.py:136-146](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L136-L146) 定义了 `FeedForward`：

```python
class FeedForward(nn.Module):
    def __init__(self, config: MiniMindConfig, intermediate_size: int = None):
        super().__init__()
        intermediate_size = intermediate_size or config.intermediate_size
        self.gate_proj = nn.Linear(config.hidden_size, intermediate_size, bias=False)
        self.down_proj  = nn.Linear(intermediate_size, config.hidden_size, bias=False)
        self.up_proj    = nn.Linear(config.hidden_size, intermediate_size, bias=False)
        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, x):
        return self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))
```

逐行说明：

- `intermediate_size = intermediate_size or config.intermediate_size`：允许外部传入中间维。这一点很关键——`MOEFeedForward` 会把每个专家的中间维换成 `moe_intermediate_size`，正是靠这个参数注入的。
- 三个 `nn.Linear(..., bias=False)`：对应 \(W_{\text{gate}}/W_{\downarrow}/W_{\uparrow}\)，都没有偏置（这是当前主流 LLM 的惯例，省参数、对 RMSNorm 友好）。
- `self.act_fn = ACT2FN[config.hidden_act]`：从 `transformers` 的激活函数表里取。`config.hidden_act` 默认是 `'silu'`（见 [L25](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L25)），所以 `act_fn` 就是 SiLU。
- `forward` 一行：`down_proj( act_fn(gate_proj(x)) * up_proj(x) )`，和上面的公式一一对应。

#### 4.1.4 代码实践

**实践目标**：用一段独立代码验证 `FeedForward` 的前向确实等于「`down(silu(gate(x)) * up(x))`」，并直观看到门控的效果。

**操作步骤**（保存为 `model/_probe_ffn.py`，临时练习文件，不要提交）：

```python
# 示例代码：仅用于学习，非项目原有文件
import torch
from model_minimind import MiniMindConfig, FeedForward

torch.manual_seed(0)
cfg = MiniMindConfig(hidden_size=768, num_hidden_layers=1, use_moe=False)
ff = FeedForward(cfg)
print('intermediate_size =', cfg.intermediate_size)   # 预期 2432

x = torch.randn(2, 16, 768)
# (a) 直接调用 forward
y1 = ff(x)
# (b) 手动复现公式
y2 = ff.down_proj(ff.act_fn(ff.gate_proj(x)) * ff.up_proj(x))
print('forward 与手写公式是否一致:', torch.allclose(y1, y2, atol=1e-5))  # 预期 True
print('输出形状:', tuple(y1.shape))                                     # 预期 (2, 16, 768)
```

**需要观察的现象**：

1. `intermediate_size` 打印为 2432。
2. `(b)` 与 `forward` 结果一致（`allclose=True`），证明 SwiGLU 就是这条公式。
3. 输出形状仍是 `(2, 16, 768)`——前馈不改变维度。

**预期结果**：`allclose=True`，形状 `(2, 16, 768)`。若不一致说明你改错了 `gate/up/down` 的顺序。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `forward` 改成 `self.down_proj(self.act_fn(self.up_proj(x)))`（去掉 `gate_proj` 那条支路），它退化成什么？

> **答案**：退化成最朴素的两层 FFN `down(act(up(x)))`，也就是「单支路 + 一个激活」。这正好是 GPT-2 早期用的结构。SwiGLU 相比它多了 `gate_proj` 这条门控支路。

**练习 2**：SwiGLU 的三个矩阵里，哪一个的形状和另外两个不同？

> **答案**：`down_proj`。`gate_proj` 与 `up_proj` 都是 `hidden_size → intermediate_size`，而 `down_proj` 是 `intermediate_size → hidden_size`（权重张量形状为 `[hidden_size, intermediate_size]`）。

---

### 4.2 MOEFeedForward：混合专家与 top-k 路由

#### 4.2.1 概念说明

`MOEFeedForward` 是 MoE 模型（`use_moe=True`）的前馈层。它的核心思想是：与其用一个大 FFN 处理所有 token，不如训练 **N 个小 FFN（专家）**，再让一个 **路由器（gate）** 对每个 token 算一组得分，**只挑得分最高的 k 个专家** 来处理这个 token。

MiniMind 默认是 `num_experts=4`、`num_experts_per_tok=1`（4 专家 / top-1）。这意味着：

- **总参数量** ≈ 4 × 单个专家的参数（dense 的约 4 倍），所以 `minimind-3-moe` 总参数达 198M。
- **激活参数量**（每个 token 实际算到的）≈ 1 个专家 ≈ 和 dense 相同，所以标称「A64M」（Activated 64M）。
- 用「稀疏激活」换「大容量」：模型能记住更多知识，但单次前向的计算量与 dense 相当。

还有一个必须配套的机制——**负载均衡损失 `aux_loss`**：如果路由器把 token 都送给少数专家，其余专家收不到 token、训练不到，模型就退化。`aux_loss` 鼓励 token 在专家间均匀分布。

#### 4.2.2 核心流程

记专家数为 \(N\)、每 token 选 \(k\) 个专家（默认 \(k=1\)）。对形状 `(B, S, d)` 的输入：

1. **展平**：把前两维合并成 `M = B*S` 个 token，得 `x_flat ∈ R^{M×d}`。
2. **打分**：`gate` 线性层把每个 token 映射成 \(N\) 维 logits，再做 `softmax` 得到各专家的概率分布 \(p\in\mathbb{R}^{M×N}\)。
3. **选专家**：`torch.topk(p, k)` 取每个 token 概率最高的 \(k\) 个专家，得到 `topk_weight` 和 `topk_idx`。
4. **归一化（可选）**：若 `norm_topk_prob=True`，把选中的 \(k\) 个权重重新归一化为和为 1。对 top-1 而言，归一化后该权重恒为 1，即**被选中的专家输出按满权重参与**。
5. **分桶执行**：遍历每个专家 \(i\)，找出哪些 token 选中了它（`mask = (topk_idx == i)`），把这些 token 喂给该专家，乘以权重后用 `index_add_` 累加回结果张量对应位置。
6. **负载均衡损失**（仅训练）：按 Switch Transformer 的公式算 `aux_loss`。

路由输出的数学形式：

\[
y_t=\sum_{i\in\text{topk}(t)}\tilde{p}_{t,i}\cdot E_i(x_t)
\]

其中 \(\tilde{p}_{t,i}\) 是归一化后的权重，\(E_i\) 是第 \(i\) 个专家。对 top-1，求和只剩一项。

**负载均衡损失**（Switch Transformer 形式）：

\[
L_{\text{aux}}=N\cdot c\cdot\sum_{i=1}^{N} f_i\cdot P_i
\]

- \(f_i\)：实际被分到专家 \(i\) 的 token 比例（硬统计，不可导）；
- \(P_i\)：所有 token 对专家 \(i\) 的平均路由概率（软，可导）；
- \(c\)：`router_aux_loss_coef`（默认 `5e-4`）；
- \(N\)：专家数。

当所有专家均衡时 \(f_i=P_i=1/N\)，\(L_{\text{aux}}\) 取到最小（常数级）；越不均衡越大。由于 \(P_i\) 可导，梯度能通过它回到路由器，从而把 token「推向」冷门专家。

#### 4.2.3 源码精读

`MOEFeedForward` 的构造见 [model/model_minimind.py:148-154](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L148-L154)：

```python
class MOEFeedForward(nn.Module):
    def __init__(self, config: MiniMindConfig):
        super().__init__()
        self.config = config
        self.gate = nn.Linear(config.hidden_size, config.num_experts, bias=False)
        self.experts = nn.ModuleList(
            [FeedForward(config, intermediate_size=config.moe_intermediate_size) for _ in range(config.num_experts)]
        )
        self.act_fn = ACT2FN[config.hidden_act]
```

说明：

- `self.gate`：路由器，输出维度 = 专家数 \(N\)。它**不和专家共享权重**，是独立的打分网络。
- `self.experts`：\(N\) 个独立的 `FeedForward`，注意它把 `intermediate_size=config.moe_intermediate_size` 显式传了进去（用到了 4.1.3 里那个可注入参数）。默认 `moe_intermediate_size` 等于 `intermediate_size`（见 [L43](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L43)）。

核心前向见 [model/model_minimind.py:156-176](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L156-L176)：

```python
def forward(self, x):
    batch_size, seq_len, hidden_dim = x.shape
    x_flat = x.view(-1, hidden_dim)                                   # (M, d)
    scores = F.softmax(self.gate(x_flat), dim=-1)                     # (M, N)
    topk_weight, topk_idx = torch.topk(scores, k=self.config.num_experts_per_tok, dim=-1, sorted=False)
    if self.config.norm_topk_prob:
        topk_weight = topk_weight / (topk_weight.sum(dim=-1, keepdim=True) + 1e-20)
    y = torch.zeros_like(x_flat)
    for i, expert in enumerate(self.experts):
        mask = (topk_idx == i)                                         # (M, k) bool
        if mask.any():
            token_idx = mask.any(dim=-1).nonzero().flatten()          # 选中专家 i 的 token 下标
            weight = topk_weight[mask].view(-1, 1)
            y.index_add_(0, token_idx, (expert(x_flat[token_idx]) * weight).to(y.dtype))
        elif self.training:
            y[0, 0] += 0 * sum(p.sum() for p in expert.parameters())   # 占位：让 DDP 仍同步该专家梯度
    if self.training and self.config.router_aux_loss_coef > 0:
        load = F.one_hot(topk_idx, self.config.num_experts).float().mean(0)   # 各专家的 token 比例
        self.aux_loss = (load * scores.mean(0)).sum() * self.config.num_experts * self.config.router_aux_loss_coef
    else:
        self.aux_loss = scores.new_zeros(1).squeeze()
    return y.view(batch_size, seq_len, hidden_dim)
```

关键点逐段拆解：

- **打分与选专家**：`scores` 是 softmax 概率；`topk(sorted=False)` 取最大的 \(k\) 个，不排序以省一点时间。`norm_topk_prob` 把权重归一化——对 top-1，每个权重除以自身，结果恒为 1。
- **`for i, expert in enumerate(self.experts)`**：逐专家处理。`mask = (topk_idx == i)` 形状 `(M, k)`，标记哪些 (token, 槽位) 选中了专家 \(i\)；`mask.any(dim=-1)` 把 k 维压掉，得到「该 token 是否选中专家 \(i\)」的 `(M,)` 布尔，再 `nonzero().flatten()` 取这些 token 的下标。
- **`y.index_add_(0, token_idx, ...)`**：把专家 \(i\) 对这些 token 的输出（乘以权重）累加到 `y` 的对应行。对 top-2/top-k，一个 token 可能被多个专家处理，累加正好实现加权求和。
- **`elif self.training: y[0,0] += 0 * sum(...)`**：这是一个工程技巧。当某专家本轮**一个 token 都没收到**时，它的参数不会出现在前向计算图里，DDP（分布式数据并行）会因「未使用参数」报错。加一个 `0 * 参数和` 的占位项，让该专家的参数仍连进计算图、产生零梯度，从而避免 DDP 报错。仅训练时需要。
- **`aux_loss`**：`load` 是 one-hot 后对 token 维求均值（即各专家实际分到的 token 比例 \(f_i\)），`scores.mean(0)` 是各专家的平均路由概率 \(P_i\)，二者逐元素相乘求和，再乘 \(N\) 和系数 \(c\)，正是 4.2.2 的公式。**仅在 `self.training=True` 且系数大于 0 时计算**，否则置 0。

#### 4.2.4 代码实践

**实践目标**：手动观察路由——给一段随机输入，看每个 token 被分给哪个专家，并验证「token 均衡分布时 `aux_loss` 较小、全部挤到同一专家时 `aux_loss` 较大」。

**操作步骤**（接 4.1.4 的临时文件，或新建 `model/_probe_moe.py`）：

```python
# 示例代码：仅用于学习，非项目原有文件
import torch
from model_minimind import MiniMindConfig, MOEFeedForward

torch.manual_seed(0)
cfg = MiniMindConfig(hidden_size=768, num_hidden_layers=1, use_moe=True)
moe = MOEFeedForward(cfg).eval()   # 先用 eval，便于手动看路由

x = torch.randn(1, 20, 768)        # 20 个 token
# 手动复现路由过程，看分配
scores = torch.softmax(moe.gate(x.view(-1, 768)), dim=-1)
topk_w, topk_idx = torch.topk(scores, k=1, dim=-1)
print('每个 token 选中的专家:', topk_idx.squeeze(-1).tolist())
print('各专家收到的 token 数:', [(i, int((topk_idx == i).sum())) for i in range(4)])

# 切到训练模式，读 aux_loss
moe.train()
_ = moe(x)
print('aux_loss =', moe.aux_loss.item())
```

**需要观察的现象**：

1. 每个 token 只选中 1 个专家（top-1）。
2. 由于输入是随机的、参数是随机的，20 个 token 在 4 个专家间的分布通常不均（比如 `8, 6, 4, 2`）。
3. `aux_loss` 为一个正的小数（量级取决于不均衡程度）。理论上当 4 个专家各收 5 个 token、且路由概率均匀时它最小。

**预期结果**：能打印出路由分布和一个非零 `aux_loss`。具体数值「待本地验证」（依赖随机种子与硬件），但你应能观察到分布越不均、`aux_loss` 越大的趋势。

#### 4.2.5 小练习与答案

**练习 1**：默认 top-1 配置下，`norm_topk_prob` 把权重归一化后，被选中专家的权重是多少？这意味着什么？

> **答案**：恒为 1.0（因为只有一个元素，除以自身）。这意味着 top-1 时被选中专家的输出**按满权重**参与，softmax 概率本身只用于「选谁」和「算 `aux_loss`」，不再缩放输出。

**练习 2**：为什么 `aux_loss` 用 \(f_i \cdot P_i\) 而不是只用 \(f_i\)？

> **答案**：\(f_i\) 是硬统计（token 实际去了哪个专家），对路由器权重**不可导**；\(P_i\)（平均 softmax 概率）才可导。两者相乘后，梯度能通过 \(P_i\) 回传到 gate，把路由分布推向均匀。只用 \(f_i\) 则无法提供梯度。

**练习 3**：`elif self.training: y[0,0] += 0 * sum(p.sum() ...)` 这行删掉，单卡训练能跑吗？多卡 DDP 呢？

> **答案**：单卡通常能跑（PyTorch 对未使用参数容忍度较高）。多卡 DDP 会报「expected to have finished reduction in the prior iteration」之类错误——因为某专家本轮没收到 token，其参数未参与前向，DDP 不知道该同步它的梯度。这行 `0*` 占位就是为了让所有专家参数都进入计算图。

---

### 4.3 MiniMindBlock：把注意力与前馈组装起来（含 aux_loss 传递）

#### 4.3.1 概念说明

`MiniMindBlock` 是一个完整的 Transformer 层：先做自注意力（u3-l2），再做前馈（本讲 4.1/4.2），两段都用 **Pre-Norm + 残差**。它对 Dense 和 MoE 是同一份代码，靠 `config.use_moe` 在构造时决定 `self.mlp` 到底是 `FeedForward` 还是 `MOEFeedForward`。MoE 的 `aux_loss` 就挂在 `self.mlp.aux_loss` 上，由外层 `MiniMindModel` 收集、最终在训练循环里和主损失相加。

#### 4.3.2 核心流程

一个 block 的前向（Pre-Norm 结构）：

```
# 第一段：注意力 + 残差
h = x + self_attn(input_layernorm(x))
# 第二段：前馈 + 残差
out = h + mlp(post_attention_layernorm(h))
```

注意「先归一化、再进子层、再残差相加」——归一化在子层**之前**，所以叫 Pre-Norm。它的好处是梯度流经残差捷径更顺畅，利于深层稳定。

`aux_loss` 的传递链路：

```
MOEFeedForward.aux_loss          # 每层 MoE 各算一个
  ↓
MiniMindModel.forward 把所有层的 aux_loss 求和   # L231
  ↓
MiniMindForCausalLM.forward 把它放进 MoeCausalLMOutputWithPast.aux_loss   # L253
  ↓
train_*.py 里 loss = res.loss + res.aux_loss     # train_pretrain.py L37
```

#### 4.3.3 源码精读

`MiniMindBlock` 见 [model/model_minimind.py:178-194](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L178-L194)：

```python
class MiniMindBlock(nn.Module):
    def __init__(self, layer_id: int, config: MiniMindConfig):
        super().__init__()
        self.self_attn = Attention(config)
        self.input_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.mlp = FeedForward(config) if not config.use_moe else MOEFeedForward(config)

    def forward(self, hidden_states, position_embeddings, past_key_value=None, use_cache=False, attention_mask=None):
        residual = hidden_states
        hidden_states, present_key_value = self.self_attn(
            self.input_layernorm(hidden_states), position_embeddings,
            past_key_value, use_cache, attention_mask
        )
        hidden_states += residual
        hidden_states = hidden_states + self.mlp(self.post_attention_layernorm(hidden_states))
        return hidden_states, present_key_value
```

要点：

- **`self.mlp = FeedForward(config) if not config.use_moe else MOEFeedForward(config)`**：整个 Dense↔MoE 的差异就被这一行三目运算收敛了，其余（Attention、两个 Norm、残差结构）完全共用。这就是为什么一份代码能同时产出 `minimind-3`（Dense）和 `minimind-3-moe`。
- 两个 `RMSNorm`：`input_layernorm` 用于注意力之前，`post_attention_layernorm` 用于前馈之前。
- 残差：`hidden_states += residual`（注意力残差），`hidden_states = hidden_states + self.mlp(...)`（前馈残差）。两段都是「子层输出 + 子层输入」。

`aux_loss` 的聚合在 [model/model_minimind.py:231](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L231)：

```python
aux_loss = sum(
    [l.mlp.aux_loss for l in self.layers if isinstance(l.mlp, MOEFeedForward)],
    hidden_states.new_zeros(1).squeeze()
)
```

它把每一层里「`mlp` 是 `MOEFeedForward`」的那些 `aux_loss` 加起来。对 Dense 模型，这个列表为空，初值是 0 张量，所以 `aux_loss=0`——这也是 Dense 训练日志里 `aux_loss: 0.0000` 的来源。

最后，[model/model_minimind.py:245-253](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L245-L253) 的 `MiniMindForCausalLM.forward` 把 `aux_loss` 随主损失一起返回：

```python
hidden_states, past_key_values, aux_loss = self.model(input_ids, attention_mask, past_key_values, use_cache, **kwargs)
...
loss = F.cross_entropy(x.view(-1, x.size(-1)), y.view(-1), ignore_index=-100)
return MoeCausalLMOutputWithPast(loss=loss, aux_loss=aux_loss, logits=logits, ...)
```

注意 `loss` 和 `aux_loss` 是**分开返回**的，并不在这里相加。真正相加发生在训练脚本里，例如 [trainer/train_pretrain.py:37](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L37)：

```python
loss = res.loss + res.aux_loss
```

并且日志里把它们分开打出来（[train_pretrain.py:54-L59](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L54-L59)）：

```python
current_aux_loss = res.aux_loss.item() if res.aux_loss is not None else 0.0
current_logits_loss = current_loss - current_aux_loss
... Logger(f'... loss: {current_loss:.4f}, logits_loss: {current_logits_loss:.4f}, aux_loss: {current_aux_loss:.4f}, ...')
```

所以训练时你会看到三栏：`loss`（含 aux）、`logits_loss`（纯语言模型损失，等于 `loss - aux_loss`）、`aux_loss`（负载均衡）。MoE 训练时 `aux_loss` 是小正数，Dense 训练时恒为 0。

#### 4.3.4 代码实践

**实践目标**：验证 Dense block 的 `mlp` 是 `FeedForward`、MoE block 的 `mlp` 是 `MOEFeedForward`，且 Dense 下没有 `aux_loss`、MoE 下有。

**操作步骤**：

```python
# 示例代码：仅用于学习，非项目原有文件
import torch
from model_minimind import MiniMindConfig, MiniMindBlock, FeedForward, MOEFeedForward

d = 768
blk_d = MiniMindBlock(0, MiniMindConfig(hidden_size=d, num_hidden_layers=1, use_moe=False))
blk_m = MiniMindBlock(0, MiniMindConfig(hidden_size=d, num_hidden_layers=1, use_moe=True ))

print('dense block.mlp 类型:', type(blk_d.mlp).__name__)   # FeedForward
print('moe   block.mlp 类型:', type(blk_m.mlp).__name__)   # MOEFeedForward
print('dense 的 mlp 有 aux_loss 属性吗:', hasattr(blk_d.mlp, 'aux_loss'))  # False（forward 前不存在）
```

**需要观察的现象**：Dense block 的 `mlp` 是 `FeedForward`，它**没有** `aux_loss` 属性（`MOEFeedForward` 才在 forward 里写 `self.aux_loss`）；MoE block 的 `mlp` 是 `MOEFeedForward`。

**预期结果**：如上。这也解释了为什么 [L231](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L231) 要用 `isinstance(l.mlp, MOEFeedForward)` 做过滤——否则 Dense 模型取 `l.mlp.aux_loss` 会直接 `AttributeError`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `MiniMindBlock` 的两段残差都去掉（即不让 `x + ...`），训练会发生什么？

> **答案**：退化成纯子层堆叠，没有残差捷径，梯度难以回传到浅层，深层几乎学不动，loss 很难下降甚至发散。残差是深层 Transformer 能训起来的关键。

**练习 2**：为什么 `MiniMindModel` 用 `isinstance(l.mlp, MOEFeedForward)` 判断、而不是 `if self.config.use_moe`？

> **答案**：用类型判断更稳健——即使将来某层混用 Dense/MoE（比如只在前几层用 MoE），这段聚合代码也不用改。而且 `l.mlp.aux_loss` 只在 `MOEFeedForward` 上存在，必须先过滤再访问属性，否则 Dense 层会报 `AttributeError`。

---

## 5. 综合实践

把 4.1~4.3 串起来：实例化 Dense 与 MoE 两个 `MiniMindBlock`，对比**参数量**、**`aux_loss`**、**前向耗时**，亲手验证「4 experts / top-1 只比 dense 慢约 50%」。

保存为 `model/_probe_block.py`（临时练习文件），在仓库根目录运行 `python model/_probe_block.py`：

```python
# 示例代码：仅用于学习，非项目原有文件
import sys, os, time
sys.path.append(os.path.dirname(os.path.abspath(__file__)))  # 让 model_minimind 可被导入
import torch
from model_minimind import (
    MiniMindConfig, MiniMindBlock, MOEFeedForward, precompute_freqs_cis
)

device = 'cuda' if torch.cuda.is_available() else 'cpu'
torch.manual_seed(0)
d, L = 768, 8
cfg_d = MiniMindConfig(hidden_size=d, num_hidden_layers=L, use_moe=False)
cfg_m = MiniMindConfig(hidden_size=d, num_hidden_layers=L, use_moe=True)

print(f'intermediate_size={cfg_d.intermediate_size}, '
      f'moe_intermediate_size={cfg_m.moe_intermediate_size}, '
      f'experts={cfg_m.num_experts}, top-k={cfg_m.num_experts_per_tok}')

blk_d = MiniMindBlock(0, cfg_d).to(device)
blk_m = MiniMindBlock(0, cfg_m).to(device).train()   # 训练模式才能拿到 aux_loss

# 构造输入 + position_embeddings（RoPE 的 cos/sin 表，切到 seq 长度）
B, S = 2, 64
x = torch.randn(B, S, d, device=device)
cos, sin = precompute_freqs_cis(dim=cfg_m.head_dim, end=S)
pos = (cos.to(device), sin.to(device))

# (1) 前向 + aux_loss
out_m, _ = blk_m(x, pos)
print('MoE block 输出形状:', tuple(out_m.shape),
      '| aux_loss:', blk_m.mlp.aux_loss.item(),
      '| 是 MoE?', isinstance(blk_m.mlp, MOEFeedForward))

# (2) 参数量对比
def nparams(m): return sum(p.numel() for p in m.parameters())
p_d = nparams(blk_d.mlp); p_m = nparams(blk_m.mlp)
print(f'dense FFN 参数: {p_d:,}   |   MoE FFN 参数: {p_m:,}   (约 {p_m/p_d:.1f}x，但单 token 只激活 1 个专家)')

# (3) 计时：dense vs MoE
def bench(blk, n=300):
    for _ in range(30): blk(x, pos)                       # 预热
    if device == 'cuda': torch.cuda.synchronize()
    t0 = time.time()
    for _ in range(n): blk(x, pos)
    if device == 'cuda': torch.cuda.synchronize()
    return (time.time() - t0) / n * 1000

td = bench(blk_d); tm = bench(blk_m)
print(f'dense: {td:.2f} ms/次   |   MoE: {tm:.2f} ms/次   |   比值 {tm/td:.2f}x')
```

**需要观察与思考**：

1. **参数量**：MoE 的 FFN 参数约为 dense 的 4 倍（4 个专家），但单 token 只走 1 个专家，激活参数与 dense 相同。这正是 `minimind-3-moe` 标称「198M-A64M」的由来。
2. **耗时比值**：理想情况下 `tm/td` 在 1.5 上下（README 称「约慢 50%」）。**精确数值待本地验证**——它取决于硬件（CPU/GPU）、`B*S` 大小、`torch.compile` 是否开启。在你机器上可能是 1.3x~2x，但不应是 4x。

**为什么只慢约 50%（关键解释）**：

- **计算量并不翻 4 倍**。top-1 路由下，每个 token 只被 1 个专家处理；4 个专家按桶各处理约 1/4 的 token，**总算量 ≈ 全部 token × 单专家前向**，与 dense 相当。
- **多出来的开销**是「非矩阵」部分：`gate` 投影 + softmax + `topk` 的路由决策、4 次「分桶→小矩阵乘→`index_add_` 回填」带来的 **kernel 启停与调度**、Python 层 `for` 循环开销。README（[L566](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L566)）的原话是：「原生训练时带来的 kernel 启停和调度开销会急剧变重……`4 experts / top-1` 这个甜点配置大约只比 dense 模型慢 50% 左右。」
- **为何专家更多反而慢得多**：专家数从 4 增到 8/16 时，循环与 kernel 开销随之线性增加，而每个专家分到的 token 更少、矩阵更小（GPU 利用率下降），开销占比迅速上升——这就是「MoE 推理/训练反而更慢」的直觉来源，需要 Triton/DeepSpeed-MoE/Megatron 这类融合算子才能优化。MiniMind 为了保留「纯 PyTorch、易学」选择了现实的折中。

**进阶观察（可选）**：把 `cfg_m` 改成 `MiniMindConfig(..., num_experts=8, num_experts_per_tok=2)`，重跑计时，看 `tm/td` 是否明显升高，验证「专家越多、相对越慢」。

## 6. 本讲小结

- **SwiGLU**（`FeedForward`）= `down(silu(gate(x)) * up(x))`，三个无偏置投影；门控支路 `gate` 过 SiLU 后与线性支路 `up` 逐元素相乘，比朴素 FFN 多一条门控、效果更好。
- **`MOEFeedForward`** 用一个 `gate` 路由器给每个 token 打分，`softmax → topk → norm_topk_prob` 选出 k 个专家，再用 `for` 循环 + `index_add_` 把 token 分桶送进对应专家并加权回填。
- **`aux_loss`** 采用 Switch Transformer 公式 \(N\cdot c\cdot\sum f_i P_i\)，用可导的平均概率 \(P_i\) 把梯度引到 gate，缓解专家负载不均；**只在训练模式**下计算，推理时为 0。
- **DDP 兼容技巧**：`y[0,0] += 0 * sum(p.sum() ...)` 让没收到 token 的专家参数仍进入计算图，避免分布式训练「未使用参数」报错。
- **`MiniMindBlock`** 用一行三目 `FeedForward if not use_moe else MOEFeedForward` 收敛 Dense/MoE 差异，Pre-Norm + 双残差结构不变；`aux_loss` 经 `MiniMindModel`（L231）聚合、由 `MoeCausalLMOutputWithPast` 返回、在训练脚本里与主损失相加。
- **4 experts / top-1 是甜点配置**：总参数约 4 倍、激活参数与 dense 相同，因 top-1 让总算量与 dense 相当，多出的约 50% 是路由与 kernel 调度开销。

## 7. 下一步学习建议

- 本讲只到「一个 block 的前馈与 MoE」为止。下一讲 **u3-l5（CausalLM 前向与交叉熵损失）** 会把 `MiniMindForCausalLM.forward` 讲透：`logits_to_keep` 切片、labels 位移交叉熵、以及 MoE 下 `loss = CE + aux_loss` 如何真正拼起来——直接承接本讲的 `aux_loss`。
- 想看 MoE **训练**全貌的读者，可跳到 **u5-l1（预训练）** 并加上 `--use_moe 1`，对照训练日志里的 `loss / logits_loss / aux_loss` 三栏，观察 `aux_loss` 在训练初期偏大、随路由稳定后下降的过程。
- 对 MoE 工程优化（融合算子、专家并行）感兴趣的读者，可阅读 README 参考资料里的 [Mistral-MoE 论文](https://arxiv.org/pdf/2401.04088)，理解为何原生 PyTorch 实现在专家变多后会变慢。
