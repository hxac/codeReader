# 优化器参数分组：谁用 Muon，谁留给 AdamW

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂 `named_parameters()` 遍历出的参数名与形状，能独立回答「Qwen2 玩具模型里每个参数张量叫什么、长什么样、是几维的」。
2. 逐行读懂 `get_optimizer` 中 Muon 分支的参数分组逻辑：**二维及以上、且名称不含 `embed_tokens` / `lm_head` 的参数交给 Muon，其余全部交给 Muon 内嵌的 AdamW 分支**。
3. 说清楚两个「为什么」：为什么 embedding 和 lm_head 不做矩阵正交化？为什么一维参数（RMSNorm 的缩放向量）不能正交化？
4. 读懂 `Muon.__init__` 的构造流程：它如何把两组参数合并成**唯一一个 param_group**，再用 `state[p]["use_muon"]` 布尔标记在每次 `step()` 时动态分流。
5. 写出一个核对脚本，打印两组参数的名称、形状与数量，验证分组既不重复也不遗漏。

本讲是第 2 单元（Muon 优化器核心原理）的第一讲。后续讲义会分别精读 Newton-Schulz 正交化（u2-l2）、`Muon.step` 的动量与权重衰减（u2-l3）、更新 RMS 缩放（u2-l4）和内嵌 AdamW 分支（u2-l5）——而**所有这些代码都只作用于本讲划分好的参数子集**。分组是 Muon 优化器的「入口逻辑」，先把它吃透，后面的源码才不会迷路。

## 2. 前置知识

本讲用到的概念都不复杂，逐一过一遍。已在 u1 系列讲义中出现过的概念只做简要回顾。

**（1）Parameter、`named_parameters()` 与 `ndim`**

PyTorch 模型由一层层 `nn.Module` 组成，可训练的权重封装为 `nn.Parameter`（`Tensor` 的子类）。调用 `model.named_parameters()` 会按模块注册顺序遍历整棵模块树，逐个产出 `(名字, 参数张量)` 二元组，名字是点分路径，例如：

```text
model.embed_tokens.weight                      # 词嵌入表
model.layers.0.self_attn.q_proj.weight         # 第 0 层注意力的 Q 投影
model.layers.0.input_layernorm.weight          # 第 0 层的 RMSNorm 缩放向量
model.norm.weight                              # 最后一个 RMSNorm
```

- `p.ndim` 是张量的维数：矩阵是 2，向量是 1。
- `p.shape` 是每一维的大小。线性层（`nn.Linear`）的权重形状约定为 `[输出特征数, 输入特征数]`。

一个重要细节：`named_parameters()` 默认 `remove_duplicate=True`，**同一个参数张量被多个模块共享时，只在第一次遇到的名字下返回一次**。这一点在本讲讨论 `lm_head` 时会再次出现。

**（2）param_groups：PyTorch 优化器的参数组织方式**

`torch.optim.Optimizer` 把所有待优化参数放进 `self.param_groups`——一个列表，每个元素是一个字典，含 `params`（参数列表）和若干超参（`lr`、`weight_decay` 等）。常见做法是**用多个 param_group 给不同参数配不同超参**（比如 embedding 用更小的学习率）。本讲会看到 Moonlight 的 Muon 反其道而行：只用一个组，用另一个机制分流。

**（3）Muon 一句话回顾（承接 u1-l1）**

Muon = SGD 动量 + 对更新做 Newton-Schulz 矩阵正交化。正交化的输入是一个**二维矩阵**：把动量矩阵 \( G \) 变换成一组正交方向（近似 \( UV^\top \)，即奇异值全部拉平为 1），再作为更新施加到权重上。这个「最近正交矩阵」只在二维矩阵上有定义——一维向量没有正交化的概念。这就是为什么分组判据首先看维度。

**（4）embedding 与 lm_head 是什么**

- `embed_tokens`：词嵌入查找表，形状 `[vocab_size, hidden_size]`（本配置为 `[151936, 1024]`），第 \( i \) 行是词表中第 \( i \) 个 token 的嵌入向量。每个训练步里，**只有 batch 中出现过的那些 token 对应的行有梯度**，梯度按行稀疏。
- `lm_head`：输出分类头，把隐藏状态映射回词表 logits，形状同样是 `[vocab_size, hidden_size]`。
- 两者在数学上互为转置用途，且经常**共享权重**（tie，见下）。

**（5）权重共享（tie）**

本玩具模型的配置里 `tie_word_embeddings=True`（[examples/toy_train.py:274](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L274)）：`lm_head.weight` 与 `model.embed_tokens.weight` 是**同一个张量对象**。结合（1）中的去重行为，这意味着 `named_parameters()` 里只会出现 `model.embed_tokens.weight` 一个名字。

**（6）De Morgan 律**

布尔逻辑：\(\neg(A \wedge B \wedge C) = \neg A \vee \neg B \vee \neg C\)。本讲的 AdamW 参数判据正是 Muon 判据的整体取反，展开后就靠这条定律。

**（7）承接 u1-l3：get_optimizer 在主流程中的位置**

u1-l3 已经梳理过主循环「forward → backward → optimizer.step → scheduler.step → zero_grad」。本讲聚焦其中 `optimizer` 的**诞生过程**——`__main__` 里的这一行（[examples/toy_train.py:332-334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L332-L334)）。另外 u1-l2 已确认：`--wd` 被解析但未转发，`get_optimizer` 的 `wd` 走默认值 `0.1`。

## 3. 本讲源码地图

本讲的关键源码只有 `examples/toy_train.py` 一个文件（全仓库唯一的源码文件），但涉及它的四个区段：

| 位置 | 作用 | 本讲关注点 |
|---|---|---|
| `get_optimizer`（[examples/toy_train.py:287-313](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L313)） | 优化器工厂函数 | Muon 分支的两个参数过滤器（本讲主战场） |
| `Muon.__init__`（[examples/toy_train.py:106-140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L106-L140)） | 优化器构造 | 参数合并、单 param_group、`use_muon` 打标 |
| `Muon.step` 中对 `use_muon` 的消费（[examples/toy_train.py:168](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L168)、[examples/toy_train.py:209](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L209)） | 训练步 | 分组标记如何被使用（只看分流，不看算法） |
| `Qwen2Config` 构造（[examples/toy_train.py:256-281](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L256-L281)） | 模型配置 | 决定参数名称与形状的源头 |

背景材料：README.md（论文贡献概述）与 `Moonlight.pdf`（技术报告）。另外 [examples/toy_train.py:46-47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47) 的注释标明本优化器改编自外部仓库 KellerJordan/Muon 的 `muon.py`，本讲会用到这一点来解释一处「文档与代码不一致」的现象。

## 4. 核心概念与源码讲解

### 4.1 参数命名与维度：先画出 Qwen2 的参数清单

#### 4.1.1 概念说明

分组逻辑是对「参数名 + 参数维度」做过滤，所以第一步是搞清楚过滤的对象。`get_model_and_dataloader` 手工构造了一个 12 层的 Qwen2 配置（不加载预训练权重，随机初始化），其全部可训练参数可以列成一张清单。

以默认配置（`--hidden_size 1024`，见 [examples/toy_train.py:325](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L325)）推算，每个参数张量的名称、形状与去向如下表（去向列先给出结论，判据在 4.2/4.3 论证）：

| 参数名模式（每层 `i` = 0…11） | 形状 | ndim | 去向 | 排除/入选原因 |
|---|---|---|---|---|
| `model.embed_tokens.weight` | `[151936, 1024]` | 2 | **AdamW** | 2 维但**名称**含 `embed_tokens` |
| `model.layers.i.self_attn.q_proj.weight` | `[1024, 1024]` | 2 | **Muon** | 2 维且名称干净 |
| `model.layers.i.self_attn.k_proj.weight` | `[1024, 1024]` | 2 | **Muon** | 同上 |
| `model.layers.i.self_attn.v_proj.weight` | `[1024, 1024]` | 2 | **Muon** | 同上 |
| `model.layers.i.self_attn.o_proj.weight` | `[1024, 1024]` | 2 | **Muon** | 同上 |
| `model.layers.i.mlp.gate_proj.weight` | `[4864, 1024]` | 2 | **Muon** | 同上 |
| `model.layers.i.mlp.up_proj.weight` | `[4864, 1024]` | 2 | **Muon** | 同上 |
| `model.layers.i.mlp.down_proj.weight` | `[1024, 4864]` | 2 | **Muon** | 同上 |
| `model.layers.i.input_layernorm.weight` | `[1024]` | 1 | **AdamW** | 只有 1 维 |
| `model.layers.i.post_attention_layernorm.weight` | `[1024]` | 1 | **AdamW** | 只有 1 维 |
| `model.norm.weight` | `[1024]` | 1 | **AdamW** | 只有 1 维 |
| `lm_head.weight` | （与 embed 同一张量） | 2 | — | `tie=True` 时**不单独出现**（见 2.(1)(5)） |

几个值得注意的推论（按当前 transformers 版本的 Qwen2 默认值推算，Qwen2 注意力与 MLP 默认不带 bias，请以 4.1.4 脚本的实际输出为准）：

- 参数张量总数 \( = 12 \times 9 + 2 = 110 \)（每层 7 个矩阵 + 2 个 norm 向量，再加 embed 和最终 norm）。
- **Muon 组：84 个**（\( 12 \times 7 \)，全部二维），**AdamW 组：26 个**（1 个二维的 embedding + 25 个一维 norm 向量）。
- 全模型**没有零维参数**（标量参数在本配置中不存在）。
- 注意 `embed_tokens.weight` 是货真价实的二维矩阵——它被排除出 Muon 靠的是**名称判据**而不是维度判据。两条判据各司其职，这是本讲最容易忽视的细节。

#### 4.1.2 核心流程

从配置到参数清单的推导链：

```text
Qwen2Config(hidden_size=1024, num_hidden_layers=12, num_attention_heads=16,
            num_key_value_heads=16, intermediate_size=4864,
            vocab_size=151936, tie_word_embeddings=True, ...)
        │
        ▼
Qwen2ForCausalLM(config)          # 随机初始化，不加载预训练权重
        │
        ▼
model.named_parameters()          # 按模块注册顺序产出 (名字, 张量)
        │
        ▼
110 个参数张量：84 个注意力/MLP 矩阵 + 1 个嵌入矩阵 + 25 个 norm 向量
```

形状的来历（以默认 `hidden_size = 1024` 为例）：

- 头数 16、每头维度 \( 1024/16 = 64 \)，注意力四个投影的输出/输入都是 \( 16 \times 64 = 1024 \)，故 q/k/v/o 均为 `[1024, 1024]`。注意本配置 `num_key_value_heads=16` 等于 `num_attention_heads`（未启用 GQA），所以 k/v 与 q 形状相同。
- MLP 中间维 `intermediate_size=4864`，gate/up 是 `[4864, 1024]`，down 反过来是 `[1024, 4864]`——**同一个门控 MLP 里两种方向的矩阵并存**，这个细节到 u2-l4（按形状调学习率）会变得非常重要。

#### 4.1.3 源码精读

配置构造于 [examples/toy_train.py:256-281](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L256-L281)：这段代码逐字段手写 `Qwen2Config`，其中与本讲直接相关的字段是——

- [examples/toy_train.py:262](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L262)：`hidden_size=hidden_size`，来自命令行，决定所有方阵和 norm 向量的大小；
- [examples/toy_train.py:264](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L264)：`intermediate_size=4864`，决定 gate/up/down 的长边；
- [examples/toy_train.py:269-270](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L269-L270)：`num_hidden_layers=12`、`num_key_value_heads=16`，决定层数（每层 9 个参数张量）与 k/v 形状；
- [examples/toy_train.py:274](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L274)：`tie_word_embeddings=True`，让 lm_head 与 embed 共享张量，是「lm_head 过滤在本配置下不生效」的根源；
- [examples/toy_train.py:279](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L279)：`vocab_size=151936`，决定嵌入矩阵的行数（u1-l4 已讲过它必须与分词器词表同源）。

随后 [examples/toy_train.py:281](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L281) 用该配置实例化 `Qwen2ForCausalLM`。参数名的消费点则在 [examples/toy_train.py:295](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L295)——`get_optimizer` 里对 `model.named_parameters()` 的遍历（4.2 精读）。

#### 4.1.4 代码实践

**实践目标**：不依赖训练、不下载数据集，直接打印玩具模型的完整参数清单，验证 4.1.1 的表格。

**操作步骤**：在仓库根目录新建 `inspect_params.py`（示例代码，非项目自带），只保留与分组相关的配置字段：

```python
# 示例代码：inspect_params.py —— 打印 Qwen2 玩具模型的参数名、形状与维度
from collections import Counter
from transformers import Qwen2Config, Qwen2ForCausalLM

config = Qwen2Config(
    hidden_size=1024,           # 对应 --hidden_size 默认值
    intermediate_size=4864,
    num_hidden_layers=12,
    num_attention_heads=16,
    num_key_value_heads=16,
    tie_word_embeddings=True,
    vocab_size=151936,
)
model = Qwen2ForCausalLM(config)   # 随机初始化，无需下载预训练权重

rows = [(name, tuple(p.shape), p.ndim) for name, p in model.named_parameters()]
for name, shape, ndim in rows:
    print(f"{name:55s} shape={shape!s:18s} ndim={ndim}")

print("参数张量总数:", len(rows))
print("维度分布:", dict(Counter(ndim for _, _, ndim in rows)))
print("名称含 lm_head 的参数:", [n for n, _, _ in rows if "lm_head" in n])
```

运行：`python inspect_params.py`（只需要 `transformers` 与 `torch`，不需要 `datasets`）。

**需要观察的现象**：

1. 打印顺序：`model.embed_tokens.weight` 最先出现，然后是 12 层、每层 7 个矩阵 + 2 个 norm，最后是 `model.norm.weight`——这正是模块注册顺序。
2. 形状与 4.1.1 表格逐项吻合；`down_proj` 与 `gate_proj`/`up_proj` 形状互为转置。
3. 维度分布为 `{2: 85, 1: 25}`（85 个二维 = 84 个投影矩阵 + 1 个嵌入矩阵）。
4. 「名称含 lm_head 的参数」打印为空列表——`tie=True` 下 lm_head 没有独立名字。

**预期结果**：参数张量总数 110；`ndim==2` 的 85 个、`ndim==1` 的 25 个。若你的 transformers 版本给 Qwen2 带上了 bias（理论上默认不带），数量会偏离，以实际输出为准（推算结果待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`k_proj.weight` 的形状为什么和 `q_proj.weight` 一样？什么情况下会不一样？
**答案**：本配置 `num_key_value_heads=16` 与 `num_attention_heads=16` 相同，k/v 投影输出宽度同为 \( 16 \times 64 = 1024 \)。若启用 GQA（`num_key_value_heads < num_attention_heads`，如 4 个 KV 头），k/v 形状变为 `[512, 1024]`，比 q「瘦」——分组逻辑不受影响（仍是二维矩阵），但形状差异会让 u2-l4 的 `adjust_lr_for_muon` 给它们不同的缩放。

**练习 2**：`model.embed_tokens.weight` 的 `ndim` 是多少？仅凭维度判据它会被分到哪一组？
**答案**：`ndim=2`（形状 `[151936, 1024]`）。仅凭 `ndim >= 2` 判据它会进入 Muon 组；它最终留在 AdamW 组是因为名称判据（名称含 `embed_tokens`）把它排除了。

**练习 3**：为什么不数一数就知道全模型有多少个参数张量？
**答案**：每层固定 9 个（q/k/v/o + gate/up/down 共 7 个二维矩阵，input/post_attention 两个一维 norm），共 12 层；再加 `embed_tokens`（lm_head 与它共享，不另计）和最终 `model.norm`，共 \( 12 \times 9 + 2 = 110 \)。

### 4.2 Muon 参数判据：二维及以上，且不是 embed / lm_head

#### 4.2.1 概念说明

Muon 分支的更新流程是「动量 → Newton-Schulz 正交化 → 施加更新」（细节在 u2-l2/u2-l3），其中正交化一步要求输入是矩阵，所以分组判据的第一个条件是维度：**`p.ndim >= 2`**。

但仅有维度条件不够，还要再加两个名称排除：**名称不含 `embed_tokens`，且不含 `lm_head`**。为什么这两个大矩阵要排除？代码本身没有写理由，结合 Muon 的工作方式与论文背景可以给出三层解释：

1. **梯度语义不匹配**。嵌入表的梯度按行稀疏——每个训练步只有 batch 中出现过的 token 的行有非零梯度，其余行是零。Newton-Schulz 正交化作用于整个矩阵，会把大量零行和少数非零行放在一起混合变换，破坏了「每个 token 的嵌入向量独立更新」的语义。
2. **计算开销**。`[151936, 1024]` 的矩阵比最大的投影矩阵（`[4864, 1024]`）大 30 多倍，每步对它做迭代正交化得不偿失。
3. **论文与上游实践的约定**。README 对技术贡献的概述（README.md 第 27 行）明确区分了「matrix and non-matrix parameters」两条更新路径；Muon 上游实现（KellerJordan/Muon，见 [examples/toy_train.py:46-47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47) 注释）同样把 embed/head 层留给 AdamW。Moonlight 技术报告（Moonlight.pdf）沿用了这一设计。

至于一维参数（RMSNorm 的缩放向量），根本原因更简单：**向量没有「正交化」的定义**——Newton-Schulz 迭代的第一步 \( A = X X^\top \) 对向量退化为标量，整个算法失去意义。

于是 Muon 参数判据可以写成集合表达式：

\[
M \;=\; \{\, p \;:\; \operatorname{ndim}(p) \ge 2 \;\wedge\; \texttt{embed\_tokens} \notin \operatorname{name}(p) \;\wedge\; \texttt{lm\_head} \notin \operatorname{name}(p) \,\}
\]

三个条件缺一不可：维度条件保证「能」正交化，两个名称条件保证「该」正交化。

#### 4.2.2 核心流程

判据的代码形态是一个列表推导式，逐参数做一次布尔判定：

```text
for (name, p) in model.named_parameters():
    keep = (p.ndim >= 2) and ("embed_tokens" not in name) and ("lm_head" not in name)
    if keep: muon_params.append(p)
```

对 4.1.1 的 110 个张量跑一遍：

- 84 个投影矩阵：三个条件全真 → 进入 `muon_params`；
- `embed_tokens.weight`：`ndim>=2` 为真，但名称含 `embed_tokens` → 短路排除；
- 25 个 norm 向量：第一个条件即假 → 排除；
- `lm_head`：本配置下根本不出现在 `named_parameters()` 中，该排除条件属于「防御性代码」（4.3 详述）。

结果：`muon_params` 恰好 84 个，且全部是 `[1024,1024]`、`[4864,1024]`、`[1024,4864]` 三种形状的矩阵。

#### 4.2.3 源码精读

Muon 参数判据位于 [examples/toy_train.py:293-297](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L293-L297)——`get_optimizer` 的 `muon` 分支，一个列表推导式同时完成遍历与三重过滤：

```python
muon_params = [
    p
    for name, p in model.named_parameters()
    if p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name
]
```

值得注意的是，`get_optimizer` 用的是 `ndim >= 2`，而 `Muon.__init__` 里对收到的参数断言**严格等于 2 维**（[examples/toy_train.py:134-137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L134-L137)）：

```python
for p in muon_params:
    assert p.ndim == 2, p.ndim
    self.state[p]["use_muon"] = True
```

两处判据不一致（`>=2` 对 `==2`）：对 Qwen2 没有影响（合格参数恰好全是 2 维矩阵），但如果给一个带三维以上权重（如卷积核）的模型套用 `get_optimizer`，构造 `Muon` 时会立刻触发 `AssertionError`。`Muon.step` 内部其实留有对更高维梯度的善后代码——[examples/toy_train.py:180-181](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L180-L181) 的 `if g.ndim > 2: g = g.view(g.size(0), -1)` 会把多维梯度展平成矩阵——但由于 `__init__` 的断言更严，这条路径在本仓库中实际到不了（上游版本允许 `>=2` 维参数，属于改编时留下的痕迹）。

打上的 `use_muon=True` 标记会在每次 `step()` 时被消费：[examples/toy_train.py:168](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L168) 从参数组里筛出标记为真的参数走正交化更新。也就是说，分组判据虽然在 `get_optimizer` 里执行，但**效力贯穿整个训练过程**。

#### 4.2.4 代码实践

**实践目标**：亲手复现 Muon 判据，核对进入 Muon 的参数清单与数量。

**操作步骤**：在 `inspect_params.py` 的基础上追加（示例代码）：

```python
muon_params = [
    (name, tuple(p.shape))
    for name, p in model.named_parameters()
    if p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name
]
print("Muon 参数个数:", len(muon_params))
for name, shape in muon_params[:8]:
    print(f"  {name:55s} {shape}")
# 观察形状种类
print("形状种类:", sorted({s for _, s in muon_params}))
```

**需要观察的现象**：个数恰为 84；前 8 项是第 0 层的 q/k/v/o/gate/up/down 和第 1 层的 q_proj；形状只有三种——`[1024, 1024]`、`[4864, 1024]`、`[1024, 4864]`；列表里找不到任何 `embed_tokens`、`lm_head`、`layernorm` 字样。

**预期结果**：`Muon 参数个数: 84`（\( 12 \times 7 \)，待本地验证）。若把过滤条件中的 `"embed_tokens" not in name` 删掉再运行，个数会变成 85——多出来的正是 `[151936, 1024]` 的嵌入矩阵，这直接验证了 4.2.1 的第 1 点：它是被名称条件、而非维度条件挡在门外的。

#### 4.2.5 小练习与答案

**练习 1**：把 `"embed_tokens" not in name` 条件删掉，训练还能跑起来吗？会发生什么？
**答案**：能跑——`ndim==2` 的断言不会报错（嵌入矩阵本来就是 2 维），但每一步 `Muon.step` 要对 `[151936, 1024]` 的动量矩阵做 Newton-Schulz 迭代（u2-l2 精读）：计算量大增，且正交化会混合稀疏梯度中的零行与非零行，破坏嵌入按行独立更新的语义。训练效果一般会明显变差（这一点可自行小规模验证，待本地验证），也正是论文与上游都把 embed 留给 AdamW 的原因。

**练习 2**：如果给一个含 4 维卷积权重的模型调用 `get_optimizer("muon", ...)`，会在哪一行、以什么方式失败？
**答案**：4 维权重满足 `ndim >= 2` 且名称不含 embed/lm_head，进入 `muon_params`；随后在 [examples/toy_train.py:136](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L136) 的 `assert p.ndim == 2` 处抛出 `AssertionError`，优化器构造失败。这说明 `get_optimizer` 的 `>=2` 与 `Muon.__init__` 的 `==2` 并不一致，玩具脚本隐含假设了「模型权重全是二维矩阵」。

**练习 3**：`muon_params` 里为什么一个 bias 都没有？
**答案**：Qwen2 默认注意力与 MLP 的投影不带 bias（本配置未显式开启），所以模型里根本没有 bias 参数；即便有，bias 是一维张量，会在维度条件处被排除，走 AdamW 分支。

### 4.3 AdamW 参数判据：一个精心写成的补集

#### 4.3.1 概念说明

AdamW 组的过滤条件不是重新列举，而是把 Muon 判据**整体取反**：

\[
A \;=\; \overline{M} \;=\; \{\, p \;:\; \operatorname{ndim}(p) < 2 \;\vee\; \texttt{embed\_tokens} \in \operatorname{name}(p) \;\vee\; \texttt{lm\_head} \in \operatorname{name}(p) \,\}
\]

（由 De Morgan 律，三个 `与` 取反变成三个 `或`。）

这种「补集写法」有一个非常重要的工程性质：**互斥且完备**。

- **互斥**：\( M \cap A = \varnothing \)，任何参数不会同时出现在两个列表里——否则同一次 `step()` 中它会被 Muon 和 AdamW 各更新一次，等于学习率翻倍还不自知。
- **完备**：\( M \cup A = \) 全部参数，任何参数不会落空——PyTorch 优化器只更新注册进 `param_groups` 的参数，**一个没进任何组的参数会被静默冻结**，不报错、不警告，是极难察觉的 bug。

用一行 `not (...)` 取反，两条安全性同时得到保证，还免去了维护两份判据的同步成本（改 Muon 判据时 AdamW 判据自动跟随）——这是本讲最值得学走的工程技巧。

补集里的成员按被排除的原因分两类：

1. **因维度被排除**：25 个一维 norm 向量（不能正交化）；
2. **因名称被排除**：`embed_tokens.weight`（能正交化但不该，见 4.2.1）。

而 `lm_head` 过滤在当前配置下是一条**防御性**规则：`tie_word_embeddings=True` 时 `lm_head.weight` 与 `embed_tokens.weight` 共享同一张量，`named_parameters()` 去重后只剩 `model.embed_tokens.weight` 一个名字，「名称含 lm_head」的条件永远为假。它防的是**权重不共享的模型**——一旦换到 untied 配置，`lm_head.weight`（同为 `[151936, 1024]` 的二维矩阵）就会现身，这条过滤确保它照样被挡在 Muon 门外、留在 AdamW 组里。

#### 4.3.2 核心流程

```text
全集 U = named_parameters()                    # 110 个 (name, p)
M     = {p : ndim≥2 ∧ 无embed ∧ 无lm_head}      # Muon 判据
A     = U − M                                   # 补集 = AdamW 判据

分类结果：
  M（84 个）：12×(q,k,v,o,gate,up,down)
  A（26 个）：embed_tokens.weight（2 维，因名称排除）
           + 12×2 个层内 norm + model.norm（1 维，因维度排除）
```

注意进入 AdamW 组的 26 个参数里有 1 个是二维矩阵——「AdamW 组 = 一维参数组」这个想当然的等式是错的，**AdamW 组的真实定义是「Muon 不处理的参数」**。

#### 4.3.3 源码精读

AdamW 参数判据位于 [examples/toy_train.py:298-304](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L298-L304)，把 Muon 判据原样包进 `not (...)`：

```python
adamw_params = [
    p
    for name, p in model.named_parameters()
    if not (
        p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name
    )
]
```

与它配套的 tie 开关在 [examples/toy_train.py:274](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L274)（`tie_word_embeddings=True`），决定了 `lm_head` 是否会以独立名字出现。补集的消费点在 `Muon.step` 的 AdamW 分支：[examples/toy_train.py:209](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L209) 用 `not self.state[p]["use_muon"]` 筛出这批参数，走手写的 AdamW 更新（u2-l5 精读）。

顺带对照 `get_optimizer` 的另一分支：[examples/toy_train.py:288-291](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L288-L291) 的 `adamw` 分支把 `model.parameters()` **不加区分地**全部交给 `torch.optim.AdamW`——这正是 u1-l2 强调的对照实验设计：换 `--optimizer` 时模型、数据、其余超参全部不变，优化器策略是唯一变量。Muon 分支的精细分组只作用在自己身上。

#### 4.3.4 代码实践

**实践目标**：复现 AdamW 判据，并程序化验证「互斥、完备、lm_head 缺席」三件事。

**操作步骤**：继续在 `inspect_params.py` 中追加（示例代码）：

```python
def is_muon(name, p):
    return p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name

muon_names  = {n for n, p in model.named_parameters() if is_muon(n, p)}
adamw_names = {n for n, p in model.named_parameters() if not is_muon(n, p)}
all_names   = {n for n, _ in model.named_parameters()}

print("Muon 组大小:", len(muon_names), " AdamW 组大小:", len(adamw_names))
print("交集（应为空）:", muon_names & adamw_names)
print("并集覆盖全部参数（应为 True）:", muon_names | adamw_names == all_names)
print("AdamW 组中的二维参数（应只有 embed）:",
      [n for n in adamw_names if dict(model.named_parameters())[n].ndim == 2])
```

**需要观察的现象**：交集为空集合；并集覆盖为 `True`；AdamW 组里二维参数只有 `model.embed_tokens.weight` 一个。

**预期结果**：`Muon 组大小: 84  AdamW 组大小: 26`（待本地验证）。第 5 节的综合实践会进一步用 `tie_word_embeddings=False` 验证 lm_head 过滤的「防御性」。

#### 4.3.5 小练习与答案

**练习 1**：把 AdamW 判据中的 `not (...)` 展开成三个 `or` 条件。
**答案**：`p.ndim < 2 or "embed_tokens" in name or "lm_head" in name`。语义：一维（及零维）参数，或名称含 embed_tokens，或名称含 lm_head——后两条合起来就是「词表两侧的大矩阵」。

**练习 2**：假如 `adamw_params` 的条件被误写成和 `muon_params` 一样（忘了 `not`），训练时会看到什么现象？
**答案**：两组变成同一个列表。`Muon.__init__` 里先对 `muon_params` 打 `True`、再对 `adamw_params` 打 `False`，同一参数的标记被后者覆盖为 `False`，于是**所有参数都走 AdamW 分支**，Muon 完全失效；又因为 `params.extend` 后参数在组里出现两次，AdamW 分支还会对每个参数**重复更新两次**。训练不报错但结果完全错误——这就是互斥性被破坏的代价。

**练习 3**：为什么说本配置下 `lm_head` 过滤条件是「防御性代码」？它在什么情况下真正生效？
**答案**：`tie_word_embeddings=True`（[examples/toy_train.py:274](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L274)）使 lm_head 与 embed 共享张量，`named_parameters()` 去重后没有名字含 `lm_head` 的参数，条件恒为假；当模型不共享权重（如许多大规模模型的常见做法）时，`lm_head.weight` 以独立名字出现，这条过滤把它挡在 Muon 门外、送进 AdamW 组。

### 4.4 优化器构造流程：单 param_group 与 use_muon 标记

#### 4.4.1 概念说明

分组完成后的最后一步是把两组参数交给 `Muon` 构造函数。这里有一个与 PyTorch 惯例**不同**的设计值得细品：

- **惯例**：不同超参的参数放进**多个 param_groups**（如 `torch.optim.AdamW([{...embedding 超参...}, {...其余超参...}])`），每个组各有一份 `lr`/`weight_decay`。
- **本实现**：`Muon.__init__` 把两组参数**拼接进唯一一个 param_group**，用 `self.state[p]["use_muon"]` 这个布尔标记记录每个参数的身份；每次 `step()` 时再按标记把参数临时分成两拨（[examples/toy_train.py:168](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L168) 与 [examples/toy_train.py:209](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L209)），分别执行 Muon 更新和手写 AdamW 更新。

为什么可以只用一个组？因为两条分支**共享同一份超参**：`lr` 和 `wd` 都从同一个 `group` 字典里读（[examples/toy_train.py:170-171](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L170-L171)、[examples/toy_train.py:210-213](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L210-L213)）。学习率调度器（u1-l3 讲过的 cosine warmup）只需改写一个组的 `lr`，两条分支同步生效——Muon 分支还会在此基础上按参数形状再乘缩放系数（[examples/toy_train.py:197](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L197) 调用 `adjust_lr_for_muon`，u2-l4 的主题）。分支之间的差异是**算法层面**的（要不要正交化），不是**超参层面**的，所以无需拆组。

另外注意 `defaults` 字典（[examples/toy_train.py:119-127](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L119-L127)）里混装了两套算法的超参：`momentum`/`nesterov`/`ns_steps` 只有 Muon 分支用，`adamw_betas`/`adamw_eps` 只有 AdamW 分支用，`lr`/`wd` 两边共用——一份字典服务两种算法，各取所需。

最后是一处「文档与代码不一致」：类 docstring 中 [examples/toy_train.py:98-99](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L98-L99) 写着「`muon_params` 中 0/1 维或被检测为 embed/lm_head 的参数也会被 AdamW 优化」。但在当前代码里，`__init__` **不做**任何重排——重排逻辑已经上移到调用方 `get_optimizer`（4.2/4.3 的两个过滤器）。这段 docstring 描述的是上游 KellerJordan/Muon 的旧行为（上游在 `__init__` 内部做参数重筛），改编时把逻辑搬走了、文字却留了下来。如果直接绕过 `get_optimizer` 手动构造 `Muon`，请自己保证传进 `muon_params` 的都是二维矩阵——`assert p.ndim == 2` 会兜底，但报错信息只有一个数字，不如从源头传对。这也提醒我们：**读源码时，注释与文档要和代码互相印证，冲突时以代码为准**。

#### 4.4.2 核心流程

从 `get_optimizer("muon", ...)` 到可用的优化器，完整流程的伪代码：

```text
get_optimizer("muon", model, lr, wd):
    muon_params  = [p | ndim(p)≥2 ∧ 无embed_tokens ∧ 无lm_head]     # 4.2
    adamw_params = 其余参数                                          # 4.3（补集）
    return Muon(lr, wd, muon_params, adamw_params)

Muon.__init__:
    defaults = {lr, wd, momentum=0.95, nesterov=True, ns_steps=5,
                adamw_betas=(0.9,0.95), adamw_eps=1e-8}
    params = list(muon_params) + list(adamw_params)   # 两组拼成一个列表
    super().__init__(params, defaults)                # → 唯一一个 param_group
    for p in muon_params:  assert p.ndim == 2; state[p]["use_muon"] = True
    for p in adamw_params: state[p]["use_muon"] = False

# 之后每个训练步（u2-l3/u2-l5 精读算法细节）：
Muon.step:
    for group in self.param_groups:                   # 只有 1 个组
        Muon 分支:  params = [p | state[p]["use_muon"]]      # 正交化更新
        AdamW 分支: params = [p | not state[p]["use_muon"]]  # 常规 AdamW 更新
```

三个要点：① 两个列表拼接后经 `super().__init__(params, defaults)` 注册——因为 `defaults` 是单个字典而非字典列表，PyTorch 生成**长度为 1** 的 `param_groups`；② `use_muon` 标记在构造期写入 `self.state`，训练期只读；③ 分流发生在每次 `step()` 内部，而不是构造期硬拆两个优化器。

#### 4.4.3 源码精读

`Muon.__init__` 签名与默认值在 [examples/toy_train.py:106-117](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L106-L117)（`momentum=0.95`、`nesterov=True`、`ns_steps=5` 等默认值即在此设定）。

- [examples/toy_train.py:119-127](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L119-L127)：构造 `defaults` 字典，两套算法的超参混装在一起，构造后存入 param_group 供 `step()` 按需取用。
- [examples/toy_train.py:129-132](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L129-L132)：核心三行——`params = list(muon_params)`、`params.extend(adamw_params)`、`super().__init__(params, defaults)`。两组参数合并成单个列表交给父类，因为 `defaults` 是单字典，最终 `param_groups` 长度为 1。Muon 参数排在前、AdamW 参数排在后（拼接顺序即组内顺序）。
- [examples/toy_train.py:134-137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L134-L137)：对 `muon_params` 逐个断言二维并打 `use_muon=True`。注意 [examples/toy_train.py:135](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L135) 的注释说的仍是上游「在 __init__ 内重排」的旧语义，而代码实际只做断言加打标——又一处改编痕迹。
- [examples/toy_train.py:138-140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L138-L140)：对 `adamw_params` 打 `use_muon=False`。

标记的消费在 `step()`：[examples/toy_train.py:168](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L168)（`if self.state[p]["use_muon"]` 筛出 Muon 分支）与 [examples/toy_train.py:209](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L209)（`if not self.state[p]["use_muon"]` 筛出 AdamW 分支）。正因为 `step()` 直接按下标取 `self.state[p]["use_muon"]`，**任何进入 param_group 的参数都必须先被打标**，否则第一次 `step()` 就会 `KeyError`——构造期的两段打标循环因此不可或缺。

构造的起点在主流程 [examples/toy_train.py:332-334](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L332-L334)：`get_optimizer(args.optimizer, model, lr=args.lr)`——只传了 `lr`，`wd` 走函数默认值 `0.1`（[examples/toy_train.py:287](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287)，u1-l2 已分析过 `--wd` 未转发的问题）。最终构造调用在 [examples/toy_train.py:306-311](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L306-L311)，把两个列表连同 `lr`/`wd` 一起交给 `Muon`。

#### 4.4.4 代码实践

**实践目标**：本讲的主实践——调用**真实的** `get_optimizer` 构造优化器，从外部核对分组结果与内部结构（这就是任务要求的「打印 muon_params 与 adamw_params 的名称、形状与数量」）。

**操作步骤**：在仓库根目录新建 `grouping_report.py`（示例代码），通过 `PYTHONPATH` 直接复用项目源码，**无需下载数据集**（`get_optimizer` 只需要模型，不需要 DataLoader）：

```python
# 示例代码：grouping_report.py —— 用项目真实的 get_optimizer 核对参数分组
# 运行：PYTHONPATH=examples python grouping_report.py
from transformers import Qwen2Config, Qwen2ForCausalLM
from toy_train import get_optimizer   # 复用项目源码

config = Qwen2Config(
    hidden_size=1024, intermediate_size=4864, num_hidden_layers=12,
    num_attention_heads=16, num_key_value_heads=16,
    tie_word_embeddings=True, vocab_size=151936,
)
model = Qwen2ForCausalLM(config)
opt = get_optimizer("muon", model, lr=1e-3)

# 1) 内部结构：单 param_group
print("param_groups 个数:", len(opt.param_groups))
group = opt.param_groups[0]
print("组内参数个数:", len(group["params"]))

# 2) 按 use_muon 标记还原两个分组
name2p = dict(model.named_parameters())
muon  = [(n, tuple(p.shape)) for n, p in name2p.items() if opt.state[p]["use_muon"]]
adamw = [(n, tuple(p.shape)) for n, p in name2p.items() if not opt.state[p]["use_muon"]]

print(f"\n== Muon 组：{len(muon)} 个参数 ==")
for n, s in muon[:7]:
    print(f"  {n:55s} {s}")
print(f"\n== AdamW 组：{len(adamw)} 个参数 ==")
for n, s in adamw:
    print(f"  {n:55s} {s}")

# 3) 汇总
muon_elems  = sum(p.numel() for p in name2p.values() if opt.state[p]["use_muon"])
adamw_elems = sum(p.numel() for p in name2p.values() if not opt.state[p]["use_muon"])
print(f"\nMuon 组元素总数: {muon_elems:,}   AdamW 组元素总数: {adamw_elems:,}")
```

**需要观察的现象**：

1. `param_groups 个数: 1`，`组内参数个数: 110`——验证「单组」设计。
2. Muon 组 84 个，全部是层内投影矩阵；AdamW 组 26 个，第一项是二维的 `model.embed_tokens.weight`，其余 25 个是 `[1024]` 的 norm 向量。
3. 元素数量上 AdamW 组**远大于**直观感受：仅嵌入矩阵就有 \( 151936 \times 1024 \approx 1.55 \times 10^8 \) 个元素，与 84 个投影矩阵的总元素量级相当甚至更大——但按张量个数只占 26/110。「按个数」与「按元素量」两种视角差异巨大。

**预期结果**：`Muon 84 / AdamW 26 / 总 110`，AdamW 组元素总数约 \( 1.55\times10^8 + 25 \times 1024 \approx 1.556 \times 10^8 \)（待本地验证）。

**解释分组理由**（实践要求写出的分析，参考答案）：进入 Muon 的是「每步都被稠密梯度覆盖、且二维」的层内变换矩阵，正交化让它们的更新方向互相正交、谱范数受控；留在 AdamW 的是「不能正交化（一维 norm 向量）」或「不该正交化（嵌入/输出层大矩阵，梯度按行稀疏、NS 开销大）」的参数。两类参数各走各的最优更新路径，这正是 Moonlight 技术报告中「matrix 与 non-matrix 参数分别处理」思想在最简代码里的体现。

#### 4.4.5 小练习与答案

**练习 1**：为什么本实现选「单 param_group + use_muon 标记」而不是「两个 param_groups」？换成两组会有什么麻烦？
**答案**：两条分支共享 `lr`/`wd`，差异在算法而非超参，单组让调度器只改一处、两分支自动同步。若拆成两组：`get_cosine_schedule_with_warmup` 会遍历并改写每个组的 `lr`（u1-l3），需要保证两组初值一致；`step()` 里两组要各自维护超参读取逻辑；日志里 `param_groups[0]['lr']` 的语义也要重新确认。能做，但平添维护成本且没有收益。

**练习 2**：如果某个参数进了 param_group 却没被打上 `use_muon` 标记（比如你手动往 `opt.param_groups[0]["params"]` 里 append 了一个新参数），第一次 `step()` 会发生什么？
**答案**：`self.state[p]["use_muon"]` 抛 `KeyError`。`Optimizer.state` 是 `defaultdict(dict)`，会自动创建参数对应的外层字典，但 `use_muon` 这个键不存在。所以「注册参数」和「打标」必须成对完成——`__init__` 的两个循环正是为此。

**练习 3**：docstring（[examples/toy_train.py:98-99](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L98-L99)）说 `muon_params` 里的 0/1 维或 embed/lm_head 参数也会被 AdamW 接管，代码真是这样吗？如果不是，实际行为是什么？
**答案**：不是。这是从上游 KellerJordan/Muon 继承的旧描述——上游在 `__init__` 内部重排参数，本版本把重排上移到了 `get_optimizer`。当前代码里 `__init__` 只做两件事：`assert p.ndim == 2` 和打标。如果你绕过 `get_optimizer` 直接构造 `Muon` 并传入一维参数，得到的不是「AdamW 接管」而是直接的 `AssertionError`。

## 5. 综合实践

**任务**：把前面的碎片检查升级为一个可复用的分组审计工具 `describe_grouping(model)`，并用它做一组对照实验，亲眼验证 `lm_head` 过滤的防御性。

**步骤 1**：基于 4.4.4 的 `grouping_report.py` 改写出汇总函数（示例代码）：

```python
def describe_grouping(model, title):
    opt = get_optimizer("muon", model, lr=1e-3)
    name2p = dict(model.named_parameters())
    muon  = [n for n, p in name2p.items() if opt.state[p]["use_muon"]]
    adamw = [n for n, p in name2p.items() if not opt.state[p]["use_muon"]]
    ndim2_in_adamw = [n for n in adamw if name2p[n].ndim == 2]
    print(f"\n=== {title} ===")
    print(f"参数张量总数      : {len(name2p)}")
    print(f"Muon 组           : {len(muon)} 个, 元素 {sum(name2p[n].numel() for n in muon):>12,}")
    print(f"AdamW 组          : {len(adamw)} 个, 元素 {sum(name2p[n].numel() for n in adamw):>12,}")
    print(f"AdamW 组里的二维  : {ndim2_in_adamw}")
    print(f"含 lm_head 的名字 : {[n for n in name2p if 'lm_head' in n]}")
```

**步骤 2**：跑两组对照——唯一的变量是 `tie_word_embeddings`：

```python
base = dict(hidden_size=1024, intermediate_size=4864, num_hidden_layers=12,
            num_attention_heads=16, num_key_value_heads=16, vocab_size=151936)
describe_grouping(Qwen2ForCausalLM(Qwen2Config(**base, tie_word_embeddings=True)),
                  "tie=True（toy_train.py 的配置）")
describe_grouping(Qwen2ForCausalLM(Qwen2Config(**base, tie_word_embeddings=False)),
                  "tie=False（untied 对照）")
```

**步骤 3**：把两次输出整理成一张对照表（模板）：

| 指标 | tie=True | tie=False（预期） |
|---|---|---|
| 参数张量总数 | 110 | 111 |
| Muon 组个数 | 84 | 84 |
| AdamW 组个数 | 26 | 27 |
| AdamW 组里的二维参数 | `model.embed_tokens.weight` | embed 与 `lm_head.weight` 两个 |
| 名称含 lm_head 的参数 | 无 | `lm_head.weight` |

**需要观察的现象与预期结果**：`tie=False` 时多出的 `lm_head.weight`（形状 `[151936, 1024]`、二维）**没有**进入 Muon 组，而是出现在 AdamW 组——4.3 中「lm_head 过滤是防御性代码」的论断得到直接验证；若没有这条名称过滤，这个 15 万行的大矩阵每步都要做一次 Newton-Schulz 迭代（其代价可在 u2-l2 学完后回头体会）。两组 Muon 个数都保持 84 不变，说明 tie 与否不影响层内矩阵的分组。（表中数字为推算，待本地验证。）

**步骤 4**：最后用 100–150 字写下你对「为什么这样分组」的解释，对照 4.4.4 给出的参考答案查漏补缺。

## 6. 本讲小结

- `get_optimizer` 的 Muon 分支把参数分成互斥完备的两组：**二维及以上且名称不含 `embed_tokens`/`lm_head` 的走 Muon，其余（补集）走内嵌 AdamW**——补集写法一行 `not (...)` 同时保证了「不重复更新」和「不静默冻结」。
- 两条判据各司其职：**维度判据**回答「能不能正交化」（一维 norm 向量不能），**名称判据**回答「该不该正交化」（嵌入与输出层的大矩阵梯度按行稀疏、NS 开销大，不该）。
- 默认配置（12 层、hidden 1024、tie=True）下的分组结果：**Muon 84 个张量**（12 层 × q/k/v/o/gate/up/down），**AdamW 26 个**（1 个二维嵌入矩阵 + 25 个一维 norm 向量），共 110 个。
- `tie_word_embeddings=True` 时 `lm_head.weight` 与嵌入共享张量、被 `named_parameters()` 去重，因此 `lm_head` 过滤在本配置下恒不触发，是为 untied 模型准备的防御性代码（综合实践已验证）。
- `Muon.__init__` 的设计与 PyTorch 惯例不同：两组参数**合并进唯一一个 param_group**，靠构造期写入的 `state[p]["use_muon"]` 标记在每次 `step()` 时动态分流；`step()` 按下标读标记，所以「注册」与「打标」必须成对完成。
- 源码里有两处上游改编的痕迹：docstring 与注释仍描述上游「在 `__init__` 内重排参数」的旧行为，而实际重排已上移到 `get_optimizer`；且 `get_optimizer` 的 `ndim >= 2` 与 `__init__` 的 `assert p.ndim == 2` 不一致——读源码要以代码为准、与文档互相印证。

## 7. 下一步学习建议

分组解决了「谁走哪条路」，下一讲开始走「路本身」：

1. **u2-l2（Newton-Schulz 迭代）**：精读 [examples/toy_train.py:48-76](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L48-L76) 的 `zeropower_via_newtonschulz5`——本讲反复提到的「正交化」到底怎么算，为什么不用 SVD，系数 `(3.4445, -4.7750, 2.0315)` 从哪来。
2. **u2-l3（Muon.step 上篇）**：看 [examples/toy_train.py:168-203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L168-L203) 的 Muon 分支如何消费本讲的 `use_muon=True` 参数：动量、Nesterov、展平、正交化、解耦权重衰减。
3. **u2-l4（更新 RMS 一致化）**：本讲埋了一个伏笔——AdamW 组里唯一的二维矩阵是 `[151936, 1024]` 的嵌入，Muon 组里最大的是 `[4864, 1024]`、最小的是 `[1024, 1024]`。`adjust_lr_for_muon` 的 `0.2*sqrt(max(A,B))` 缩放正是为了让这些不同形状矩阵的更新量级可比，届时可回头再看 4.1.1 的形状表。
4. 若想核对上游差异，可对照阅读 KellerJordan/Muon 仓库的 `muon.py`（外部仓库，本讲标注的两处「改编痕迹」均源于它），体会 Moonlight 为规模化训练所做的取舍。
