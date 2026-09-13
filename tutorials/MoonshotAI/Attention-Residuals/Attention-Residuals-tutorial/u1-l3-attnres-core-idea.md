# 核心思想：用跨深度注意力替换固定累加

## 1. 本讲目标

上一讲（[u1-l2 标准残差的问题](u1-l2-standard-residual-problem.md)）我们把标准残差拆解成「膨胀 + 稀释」同一枚硬币的两面，结尾留了两个问题：**① softmax 为什么天然解决"幅度有界"？② 伪查询 \( \mathbf{w}_l \) 和普通注意力的 query 有什么不同？** 本讲正式给出解药——Attention Residuals（AttnRes）的核心公式，并回答这两个问题。读完本讲，你应该能够：

1. 写出 AttnRes 的聚合公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \cdot \mathbf{v}_i \)，说清它与标准残差 \( \sum_i 1 \cdot \mathbf{v}_i \) 在「权重从哪来」上的本质区别，并用凸组合性质证明幅度有界（回答问题 ①）。
2. 理解**伪查询** \( \mathbf{w}_l \in \mathbb{R}^d \)：每层一个可学习的自由参数，并非由输入投影得到（回答问题 ②），并解释由它产生的深度权重 \( \alpha \) 为什么是**逐 token、输入依赖**的。
3. 区分 **Full AttnRes** 与 **Block AttnRes** 两种变体：候选集合有什么不同、显存为何从 \( O(Ld) \) 降到 \( O(Nd) \)、为什么块内可以保留标准残差。
4. 完成主实践：手写 `full_attn_res(previous_outputs, w_l)`，用两次 `einsum` + `softmax(0)` 实现跨深度注意力聚合，并在随机张量上验证输出形状与 \( \alpha \) 沿深度归一。

本讲仍属入门单元：只讲**思想与公式**，伪代码的逐行调度细节（块边界、partial_block 维护）留给第 2 单元第 1、2 讲。

## 2. 前置知识

- **softmax 与凸组合**：softmax 把一组实数分数 \( s_0, \dots, s_{l-1} \) 变成一组非负、和为 1 的权重：\( \mathrm{softmax}(s)_i = \frac{e^{s_i}}{\sum_j e^{s_j}} \)。用这样的权重做的加权求和 \( \sum_i \alpha_i \mathbf{v}_i \)（\( \alpha_i \ge 0 \)、\( \sum_i \alpha_i = 1 \)）叫**凸组合**——结果必然落在各 \( \mathbf{v}_i \) 的"包络"之内，不会比最大的那份更出格。
- **三角不等式**：\( \left\| \sum_i \alpha_i \mathbf{v}_i \right\| \le \sum_i \alpha_i \| \mathbf{v}_i \| \le \max_i \| \mathbf{v}_i \| \)。这一行不等式就是"幅度有界"的全部证明（4.1 节使用）。
- **注意力的 Q/K/V 词汇**：标准自注意力里，query（查询）与 key（键）点积打分、softmax 后对 value（值）加权求和。本讲的"注意力"发生在**深度方向**而不是序列方向：查询是伪查询 \( \mathbf{w}_l \)，键与值都来自历史各层的输出表示。
- **einsum 简记回顾**（u1-l1 讲过）：`torch.einsum('d, n b t d -> n b t', w, K)` 表示把 `w[d]` 与 `K[n,b,t,d]` 沿 `d` 维内积，输出 `[n,b,t]`——**`b`、`t` 在输入输出两侧都保留，意味着不跨样本、不跨 token 混合**。
- **RMSNorm 回顾**（u1-l2 讲过）：只缩放不去均值的归一化，把向量拉回固定幅度；README 伪代码中的 `norm` 就是它。
- **承接 u1-l2 的记号**：\( \mathbf{v}_l \) 是第 \( l \) 个子层的输出（\( \mathbf{v}_0 \) 视作词嵌入），PreNorm 主干只加不缩，稀释与膨胀源自权重全为 1 的求和算子。

## 3. 本讲源码地图

本仓库没有工程代码，本讲的"源码"全部集中在 README 的 Overview 小节、伪代码前半段与 overview 示意图上：

| 文件 | 行号/位置 | 作用 | 在本讲中的用法 |
|:---|:---|:---|:---|
| `README.md` | [L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33) | 定位句：drop-in 替换、selective、input-dependent、attention **over depth** | 模块 4.1 的术语来源 |
| `README.md` | [L39-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L39-L43) | AttnRes 公式 + 伪查询定义 | 本讲核心，逐句精读 |
| `README.md` | [L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47) | Block AttnRes 小节 | 模块 4.3 的核心文字 |
| `README.md` | [L53-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L65) | `block_attn_res` 伪代码 | 公式的代码形态（两次 einsum + softmax(0)） |
| `README.md` | [L67-L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L90) | `forward` 伪代码 | 只取 L71/L80 证明 h 的语义；调度细节留给 u2-l2 |
| `README.md` | [L25-L29](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L25-L29) | overview 图注 (a)(b)(c) | 模块 4.3 的配图 |
| [assets/overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) | 整图 | 三种残差方案示意图 | 4.1 与 4.3 的直观参照 |

## 4. 核心概念与源码讲解

本讲包含三个最小模块：**AttnRes 聚合公式**、**伪查询 \( \mathbf{w}_l \)**、**Full 与 Block 两种变体**。

### 4.1 AttnRes 聚合公式

#### 4.1.1 概念说明

上一讲的结论是：标准残差 \( \mathbf{h}_l = \mathbf{h}_{l-1} + \mathbf{v}_l \) 展开后就是 \( \sum_i 1 \cdot \mathbf{v}_i \)，权重恒为 1、与输入无关、无归一化；合格的替代算子必须**同时**满足两个条件——幅度有界、权重可按内容分配。AttnRes 的做法直白得几乎朴素：**把权重 1 换成 softmax 产生的 \( \alpha_{i \to l} \)**：

\[ \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \cdot \mathbf{v}_i, \qquad \alpha_{i \to l} \ge 0, \quad \sum_{i=0}^{l-1} \alpha_{i \to l} = 1 \]

记号读法：\( \alpha_{i \to l} \) 是"从第 \( i \) 份历史流向第 \( l \) 层"的权重；候选集合是 \( \mathbf{v}_0 \)（嵌入）到 \( \mathbf{v}_{l-1} \)——**注意上界是 \( l-1 \)**：\( \mathbf{h}_l \) 是为第 \( l \) 个子层**准备的输入**，此刻 \( \mathbf{v}_l \) 还没被算出来（4.1.3 有伪代码证据）。

与标准残差逐项对照：

| 维度 | 标准残差 | AttnRes |
|:---|:---|:---|
| 权重取值 | 恒为 1 | \( \alpha_{i \to l} \)，由打分 softmax 而来 |
| 权重归一 | 无（和随深度无限增大） | \( \sum_i \alpha_{i \to l} = 1 \)（凸组合） |
| 输入依赖 | 无（任何 token 同一套全 1） | 逐 token、逐深度打分，内容不同权重不同 |
| 可学习性 | 无参数 | 打分向量 \( \mathbf{w}_l \) 可学习（4.2） |
| 读取方式 | 单条"滚雪球"式主干，每层只读上一层的和 | 每层对**全部历史**重新做一次加权聚合 |

**幅度有界（回答遗留问题 ①）**：由三角不等式，

\[ \| \mathbf{h}_l \| = \left\| \sum_i \alpha_{i \to l} \mathbf{v}_i \right\| \le \sum_i \alpha_{i \to l} \| \mathbf{v}_i \| \le \max_i \| \mathbf{v}_i \| \]

最后一行与深度 \( l \) **无关**——不管网络多深，聚合结果都被"最大那份候选"封顶。又因为 PreNorm 下各子层输出规模本就稳定（u1-l2 的 4.2），所以 AttnRes 的输出幅度跨深度有界，这正是 `training_dynamics.png` 面板 (b) 里 Block AttnRes 贴地曲线的来源。**梯度侧**同理定性变化：每份 \( \mathbf{v}_i \) 从第 \( l \) 层拿到的反向信号近似正比于 \( \alpha_{i \to l} \)，注意力成了梯度的"分配器"（精确展开含 softmax 耦合项，第 2 单元第 5 讲用 hook 实测）。

**一个容易混淆的对比**：u1-l2 结尾设想的"衰减式残差"（每次累加后乘 0.5）也能压住幅度，但它的权重仍是**事先写死的常数**，与输入无关。也就是说，"有界"（归一化）和"选择性"（内容依赖）是两个独立性质——AttnRes 用 softmax 一步同时拿到两者。反过来，若令 \( \mathbf{w}_l = \mathbf{0} \)，所有 logits 相等，\( \alpha \) 退化为均匀的 \( 1/l \)（各历史的平均）：有界但无选择性。这是理解"AttnRes ≠ 平均池化"的关键反例（见练习 2）。

**术语定位**：README 把这个机制称为 "learned, input-dependent attention **over depth**"——注意力作用在**深度轴**上，而不是序列轴上。序列方向的信息混合仍然由普通的自注意力子层（伪代码 L80 的 `self.attn`）负责；跨深度注意力对每个 token **独立**执行，两者互不越界。同时它仍是 **drop-in replacement**（L33）：被替换的只是"子层如何读取历史"，子层本身与 PreNorm 组织方式原样保留。

#### 4.1.2 核心流程

第 \( l \) 个子层开始前，跨深度聚合的执行流程（对每个 batch、每个 token 独立）：

```text
候选池 {v_0, ..., v_{l-1}}                ← 嵌入 v_0 + 之前各子层输出
      ↓ torch.stack 拼成 V ∈ [l, B, T, D]
K = RMSNorm(V)                            ← 只归一化"打分侧"，被聚合的 V 保持原值
logits = ⟨w_l, K⟩（逐 token 逐深度）       ← einsum 'd, n b t d -> n b t'
α = softmax(logits, dim=深度)              ← 每个 token 得到自己的 α 向量，Σα = 1
h_l = Σ_i α_{i→l} · v_i                    ← einsum 'n b t, n b t d -> b t d'
      ↓
子层读 Norm(h_l)（PreNorm 保留）→ 产出 v_l → v_l 加入候选池，供更深层打分
```

三条不变式贯穿始终：\( \alpha \ge 0 \)、\( \sum_i \alpha_{i \to l} = 1 \)、聚合只沿深度维缩并（`b`、`t` 两侧保留，不跨 token 混合）。

#### 4.1.3 源码精读

**定位句：三个关键词的出处。**

[README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33) —— "a drop-in replacement for standard residual connections ... enables each layer to *selectively* aggregate earlier representations via learned, input-dependent attention over depth."（标准残差连接的 drop-in 替换：让每层通过**可学习、输入依赖的跨深度注意力**，**选择性**地聚合更早的表示。）—— selective（选择性）、input-dependent（输入依赖）、over depth（跨深度）三个词分别是 4.1 的"权重可分配"、"逐 token 变化"、"注意力轴在深度方向"的原文出处。

**公式本体。**

[README.md:L39-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L39-L43) —— L39 一句话点题"AttnRes 用 softmax 注意力替换固定累加"；[L41](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L41) 给出公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \cdot \mathbf{v}_i \)；[L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43) 说明权重 \( \alpha \) 由**每层一个的可学习伪查询** \( \mathbf{w}_l \in \mathbb{R}^d \) 计算，并给出效果描述："This gives every layer selective, content-aware access to all earlier representations."（每层都获得对所有更早表示的、选择性的、内容感知的访问。）

**公式的代码形态：伪代码中两次 einsum。**

[README.md:L61-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L64) —— 这四行就是公式的逐字翻译（ albeit Block 变体，见 4.3）：

- [L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61) `V = torch.stack(blocks + [partial_block])`：把候选池堆成 `[N+1, B, T, D]`——对应公式的 \( \{\mathbf{v}_i\} \)；
- [L62](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62) `K = norm(V)`：打分前对键做 RMSNorm（为什么只归一化 K，直觉见 4.2，细节在 u2-l3）；
- [L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63) `logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)`：伪查询与各候选逐 token 点积——公式的打分项；
- [L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L64) `h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)`：**沿深度维（dim 0）softmax 得 \( \alpha \)，再对 V 加权求和得 \( \mathbf{h} \)**——公式的主干一行完成。

**\( \mathbf{h}_l \) 是"输入"的证据。**

[README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71) → [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80) —— L71 先算出 `h = block_attn_res(blocks, partial_block, ...)`，L80 才 `attn_out = self.attn(self.attn_norm(h))`：聚合结果 h 被**喂给子层入口**（先过 `attn_norm`，PreNorm 风格未变）。这证明公式里的 \( \mathbf{h}_l \) 是第 \( l \) 个子层的输入聚合，因此求和上界才是 \( l-1 \)。另注意 [L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84) 在 MLP 前又调用了一次 `block_attn_res`——每个 transformer 层实际做**两次**跨深度聚合（各自独立的 proj/norm 对），本讲只需知道这件事，调度细节在 u2-l2。

#### 4.1.4 代码实践（本讲主实践）

**实践：手写 `full_attn_res`——把公式变成可运行的算子**

1. **实践目标**：实现任务书指定的 `full_attn_res(previous_outputs, w_l)`：用 einsum 计算伪查询与各层输出的 logits，沿深度 softmax 得到 \( \alpha \)，再加权求和；在随机张量上验证输出形状为 `[B, T, D]`、\( \alpha \) 沿深度求和为 1，并对比同一深度下标准残差主干的范数。
2. **操作步骤**：README 只给出了 Block 版伪代码，Full 版没有现成代码——下面的**示例代码**按公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \) 实现，聚合那三行与伪代码 L62-L64 **逐字符同构**（仅把候选从 `N+1` 份换成 `L` 份）。保存为 `full_attn_res.py`，在 u1-l1 装好的 PyTorch 环境运行（CPU 即可）：

   ```python
   # full_attn_res.py —— Full AttnRes 聚合算子的最小实现（示例代码）
   import torch
   torch.manual_seed(0)

   def rms_norm(x, eps=1e-6):                       # 对应伪代码 L62 的 norm（RMSNorm）
       return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + eps)

   def full_attn_res(previous_outputs, w_l):
       """
       previous_outputs: list[Tensor[B, T, D]]，长度 L，即 v_0(嵌入)..v_{L-1}
       w_l:              Tensor[D]，第 l 层的可学习伪查询
       返回: h [B, T, D]；alpha [L, B, T]（沿深度 softmax 的权重）
       """
       V = torch.stack(previous_outputs)            # [L, B, T, D]   对应 L61 的 stack
       K = rms_norm(V)                              # 打分侧归一化，V 保持原值  对应 L62
       logits = torch.einsum('d, n b t d -> n b t', w_l, K)    # 对应 L63
       alpha = logits.softmax(0)                    # 沿深度维 softmax → α_{i→l}
       h = torch.einsum('n b t, n b t d -> b t d', alpha, V)   # 对应 L64 的加权聚合
       return h, alpha

   B, T, D, L = 2, 8, 16, 12
   previous_outputs = [torch.randn(B, T, D) for _ in range(L)]  # 模拟 v_0..v_{L-1}
   w_l = torch.randn(D)                                         # 模拟第 l 层伪查询（未训练）

   h, alpha = full_attn_res(previous_outputs, w_l)
   print("h 形状:", tuple(h.shape), "（预期 (2, 8, 16) = [B, T, D]）")
   print("alpha 形状:", tuple(alpha.shape), "（预期 (12, 2, 8) = [L, B, T]）")
   print("alpha 沿深度求和 ≈ 1:", torch.allclose(alpha.sum(0), torch.ones(B, T), atol=1e-5))
   print("alpha 全部非负:", bool((alpha >= 0).all()))
   v_max = max(v.norm(dim=-1).max().item() for v in previous_outputs)
   print(f"凸组合上界 max_i ||v_i|| = {v_max:.3f}，实测 max ||h|| = {h.norm(dim=-1).max().item():.3f}")

   std_trunk = torch.stack(previous_outputs).sum(0)  # 同深度的标准残差主干 Σ 1·v_i
   print(f"同深度范数对比：标准残差主干 {std_trunk.norm(dim=-1).mean().item():.3f}"
         f" vs AttnRes 聚合 {h.norm(dim=-1).mean().item():.3f}")
   ```

3. **需要观察的现象**：形状两行分别打出 `(2, 8, 16)` 与 `(12, 2, 8)`；归一性与非负性两行均为 `True`；实测 `max ||h||` 不超过 `max_i ||v_i||`（凸组合上界）；最后一行里标准残差主干范数（随机独立向量下约 \( \sqrt{12} \approx 3.5 \) 倍于单层）明显大于 AttnRes 聚合范数。
4. **预期结果**：机制层面全部可验证——输出形状正确、\( \sum_i \alpha_{i \to l} = 1 \)、幅度被上界封住、同一深度下 AttnRes 不膨胀。随机初始化的 \( \mathbf{w}_l \) 打出的 \( \alpha \) 没有可解释结构（未训练），具体数值随种子变化，**待本地验证**。
5. 常见问题：`softmax(0)` 的维度 0 是 `alpha` 的**深度维**（stack 后的第一维），不要误写成对最后一维 softmax（那会变成对 d 维归一化，物理意义完全不同）。

#### 4.1.5 小练习与答案

**练习 1**：证明：若 \( \alpha_i \ge 0 \)、\( \sum_i \alpha_i = 1 \)，则 \( \left\| \sum_i \alpha_i \mathbf{v}_i \right\| \le \max_i \| \mathbf{v}_i \| \)。

**参考答案**：先用三角不等式 \( \left\| \sum_i \alpha_i \mathbf{v}_i \right\| \le \sum_i \| \alpha_i \mathbf{v}_i \| = \sum_i \alpha_i \| \mathbf{v}_i \| \)，再把每个 \( \| \mathbf{v}_i \| \) 放大到 \( \max_j \| \mathbf{v}_j \| \)：\( \sum_i \alpha_i \| \mathbf{v}_i \| \le \left( \sum_i \alpha_i \right) \max_j \| \mathbf{v}_j \| = \max_j \| \mathbf{v}_j \| \)。上界与候选份数无关，深度再深也不破界。

**练习 2**：若令伪查询 \( \mathbf{w}_l = \mathbf{0} \)，AttnRes 退化成什么？它与标准残差还有什么本质区别？

**参考答案**：所有 logits 相等（均为 0），softmax 给出均匀权重 \( \alpha_i = 1/l \)，即**各历史输出的平均**。它与标准残差的区别：标准残差是未归一化的全 1 求和（权重为 1 而非 \( 1/l \)，幅度无界），平均池化是归一化但权重固定。这个退化情形说明"归一化（有界）"与"选择性（内容依赖）"是两个独立性质——AttnRes 的价值在于用可学习的 \( \mathbf{w}_l \) 同时拿到两者。

**练习 3**：公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \) 的求和上界为什么是 \( l-1 \) 而不是 \( l \)？伪代码里哪两行的先后顺序证明了这一点？

**参考答案**：因为 \( \mathbf{v}_l \) 是第 \( l \) 个子层的输出，而聚合发生在子层运行**之前**——\( \mathbf{h}_l \) 是子层的输入。证据：[L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71) 先计算 `h = block_attn_res(...)`，[L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80) 才执行 `attn_out = self.attn(self.attn_norm(h))`；子层输出 `attn_out` 在 [L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81) 才加入主干、成为更深层的历史候选。

### 4.2 伪查询 \( \mathbf{w}_l \)

#### 4.2.1 概念说明

权重 \( \alpha_{i \to l} \) 从哪来？README 的回答是：每层配一个**可学习伪查询**（pseudo-query）\( \mathbf{w}_l \in \mathbb{R}^d \)，用它给每份历史候选打分：

\[ \alpha_{i \to l} = \frac{\exp\left( \langle \mathbf{w}_l, \, \mathrm{norm}(\mathbf{v}_i) \rangle \right)}{\sum_{j=0}^{l-1} \exp\left( \langle \mathbf{w}_l, \, \mathrm{norm}(\mathbf{v}_j) \rangle \right)} \]

（\( \langle \cdot, \cdot \rangle \) 是 d 维点积；伪代码里未见缩放因子（如标准注意力常用的 \( 1/\sqrt{d} \))，是否另有温度等细节以论文为准，**待确认**。）

理解这个设计的三个要点：

1. **为什么叫"伪"查询（回答遗留问题 ②）**：标准注意力里 query 由**当前输入**线性变换而来（\( \mathbf{q} = W_q \mathbf{x} \)，逐 token 不同）；伪查询则是一个**自由参数**，与输入无关、训练中由梯度直接更新。每个"层 × 应用位置"一个：伪代码 [L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)/[L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84) 显示注意力前用 `attn_res_proj`、MLP 前用 `mlp_res_proj`，两套独立。
2. **输入依赖从哪来**：查询虽固定，但**键** \( \mathrm{norm}(\mathbf{v}_i) \) 是逐 token、逐层不同的——同一个 \( \mathbf{w}_l \) 面对不同 token 的历史表示会打出不同的 logits，softmax 后得到不同的 \( \alpha \)。于是"查询侧提供层的读取偏好，键侧提供内容"，两者相乘天然实现 input-dependent。
3. **每层一个的意义**：不同深度需要不同的历史配比——浅层可能更依赖嵌入 \( \mathbf{v}_0 \)，深层可能更依赖近期的块表示。每层独立的 \( \mathbf{w}_l \) 让每层学出自己的"读取画像"，而不是全网共享一套权重。

**打分为什么对 K 归一化、对 V 不归一化**（直觉版，细节在 u2-l3）：键决定打分，RMSNorm 把各候选拉回统一幅度，避免某层表示数值大就垄断 logits——稳定训练；值是被聚合的内容，保留原始幅度，输出不被强行拉平。这与 u1-l2 见过的 PreNorm 思路一脉相承：**归一化放在"读取入口"，不动被传递的数值本身**。

**开销直觉**：每处 attn_res 只引入一个 d 维查询向量 \( \mathbf{w}_l \) 加一个 RMSNorm 的缩放参数（约 \( 2d \) 个参数），相对子层本体（\( O(d^2) \) 量级）可忽略——这是 L33 敢称 drop-in 的底气之一，量化分析在 u2-l3。

#### 4.2.2 核心流程

单个 token 视角，第 \( l \) 层的深度注意力打分流程：

```text
w_l（固定，可学习）─┐
                    ├─→ logit_i = ⟨w_l, norm(v_i)⟩   对 i = 0..l-1 各算一次
v_0..v_{l-1}（逐 token 不同）─┘
        ↓
α = softmax({logit_i})               ← 该 token 专属的深度权重向量
        ↓
h_l = Σ α_i · v_i                    ← 用未归一化的 v_i 聚合
```

关键性质：\( \alpha \) 的形状是 `[深度, B, T]`——**每个 (batch, token) 一条独立的权重分布**；einsum 缩并只发生在 `d` 和 `n`（深度）上，序列方向零混合。

#### 4.2.3 源码精读

**伪查询的定义句。**

[README.md:L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43) —— "the weights \( \alpha_{i \to l} \) are computed via a **single learned pseudo-query** \( \mathbf{w}_l \in \mathbb{R}^d \) **per layer**"：三个信息点——单个向量（不是矩阵）、可学习、每层一个。

**伪查询在代码里长什么样：`proj.weight.squeeze()`。**

[README.md:L53](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53) —— 函数签名里 `proj: Linear`：伪查询被包在一个线性层里；[L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63) 用 `proj.weight.squeeze()` 把权重压成 d 维向量充当查询。从 `squeeze` 的必要性可推断该 Linear 输出维度为 1（权重形状 `[1, d]` → `[d]`）；注意打分只用了 `weight`，即使 Linear 带 bias 也没参与（此为依伪代码的推断，精确定义以论文为准，**待确认**）。

**归一化与打分、聚合三行的分工。**

[README.md:L62-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62-L64) —— L62 `K = norm(V)`（键侧归一化）；L63 查询与 K 点积出 logits；L64 `softmax(0)` 沿深度归一化后聚合**原始的 V**。三行合起来正是 4.2.1 的公式。

**两套独立的 proj/norm 对。**

[README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71) 与 [README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84) —— 注意力前用 `self.attn_res_proj` + `self.attn_res_norm`，MLP 前用 `self.mlp_res_proj` + `self.mlp_res_norm`：同一层内两次跨深度聚合各有自己的伪查询与归一化，即"每层两个 \( \mathbf{w} \)"。

#### 4.2.4 代码实践

**实践：伪查询的"每层不同 + 逐 token 输入依赖"**

1. **实践目标**：用最小实验看到 4.2.1 的两个性质——不同的 \( \mathbf{w}_l \) 给出不同的深度权重分布（每层读取画像不同）；同一个 \( \mathbf{w}_l \) 面对不同 token 的历史表示也给出不同分布（输入依赖）。
2. **操作步骤**：以下为**示例代码**（单 token、无训练，仅验证机制），保存为 `pseudo_query_probe.py` 运行：

   ```python
   # pseudo_query_probe.py —— 伪查询机制探针（示例代码）
   import torch
   torch.manual_seed(0)

   def rms_norm(x, eps=1e-6):
       return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + eps)

   def depth_alpha(V, w):                      # V: [L, D]，w: [D] → 权重 [L]
       return torch.einsum('d, l d -> l', w, rms_norm(V)).softmax(0)

   D, L = 16, 6
   V_a = torch.randn(L, D)                     # token A 的历史表示 v_0..v_{L-1}
   V_b = torch.randn(L, D)                     # token B 的历史表示
   w_shallow, w_deep = torch.randn(D), torch.randn(D)   # 两个不同层的伪查询

   a1 = depth_alpha(V_a, w_shallow)
   a2 = depth_alpha(V_a, w_deep)
   a3 = depth_alpha(V_b, w_shallow)
   show = lambda a: " ".join(f"{x:.3f}" for x in a.tolist())
   print("层1伪查询 × tokenA 的 α:", show(a1))
   print("层5伪查询 × tokenA 的 α:", show(a2), " ← 换查询，分布变了")
   print("层1伪查询 × tokenB 的 α:", show(a3), " ← 换 token，分布也变了")
   print("三者各自求和:", f"{a1.sum():.6f} {a2.sum():.6f} {a3.sum():.6f}")
   ```

3. **需要观察的现象**：三行 α 各不相同（换查询向量会变、换键内容也会变）；每行内部求和均为 1.000000。
4. **预期结果**：验证"读取画像逐层不同"与"权重逐 token 输入依赖"两个机制。随机未训练的分布没有可解释结构，具体数值**待本地验证**。
5. 思考延伸：若把两个伪查询换成同一个（`w_shallow is w_deep`），第 2、3 行会怎样？（第 2 行与第 1 行完全一致——差异全部来自查询向量。）

#### 4.2.5 小练习与答案

**练习 1**：伪查询与标准自注意力里的 query 有什么相同与不同？

**参考答案**：相同：都是与键点积打分、经 softmax 变成聚合权重的"查询"角色。不同：标准 query 由当前输入线性变换而来，逐 token 不同、参数是一个矩阵 \( W_q \)；伪查询是每层（每应用位置）一个的自由参数向量，不经输入投影，本身与输入无关——输入依赖完全由键侧（各层表示）提供。

**练习 2**：`torch.einsum('d, n b t d -> n b t', w, K)` 会不会把不同 token 的信息混在一起？从 einsum 记法说明。

**参考答案**：不会。`b`、`t` 在输入与输出下标中都出现，是"保留维"；缩并只发生在 `d` 上，输出的每个 `(n, b, t)` 元素只来自同一 `(b, t)` 的深度方向内积。因此跨深度注意力对每个 token 独立计算，序列方向的混合仍完全由自注意力子层负责。

**练习 3**：为什么打分时对 K 做 RMSNorm、却不把 V 也归一化？（直觉层面回答即可）

**参考答案**：K 决定打分——若各层表示幅度差异大，数值大的层会凭"块头"垄断 logits，训练不稳；归一化后所有候选在统一尺度上比"内容匹配度"。V 是被聚合的数值——保留原幅度让各层贡献的真实信息不被强行拉平。这与 PreNorm"归一化在读取入口、不作用于被传递数值"的思路一致（u1-l2 埋过伏笔），完整分析见 u2-l3。

### 4.3 Full AttnRes 与 Block AttnRes 两种变体

#### 4.3.1 概念说明

公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \) 描述的是**Full AttnRes**：候选池是此前**每一份**子层输出。它有个工程上致命的代价：每个子层都要对全部历史**逐份打分**，所以所有 \( \mathbf{v}_i \) 必须原样保留在显存里——标准残差可以"边走边加"折叠成一份运行和，Full AttnRes 不能。深度 \( L \)、隐藏维 \( d \) 时，残差路径上每 token 的激活占用是 \( O(Ld) \)，随深度线性膨胀。

**Block AttnRes** 的解决办法是"分而治之"：

- 把 \( L \) 个子层划分成 \( N \) 个**块**（block）；
- **块内**回到标准残差：块内的子层输出逐步累加进一个部分和 `partial_block`（运行和，只占一份显存）；
- **块间**才做注意力：候选池只有 \( N \) 份已完成的块表示 + 当前块的部分和，共 \( N+1 \) 份，对它们做 4.1/4.2 的 softmax 聚合。

于是每 token 的残差路径占用降到 \( O(Nd) \)——候选份数从"随深度线性增长"变成"被块数封顶"。README 给出的甜点位是 **约 8 个块即可恢复 Full 的大部分收益**，且作为 drop-in 替换只带边际开销（[L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)）。论文主实验（scaling law、Kimi Linear 48B 下游基准、训练动态图）用的都是 Block 版——它是"实用形态"，Full 版是"概念完全体"。

**为什么块内敢用标准残差**（呼应 u1-l2）：稀释与膨胀源自"权重全 1 的求和项数无界增长"。块内子层数被块大小**封顶**，膨胀/稀释只在块内有界地发生；而跨块的整体读取又被块间 softmax 注意力重新归一化分配——深度方向的选择性由块间注意力接管。也就是说，Block AttnRes 不是消灭了标准残差，而是把它**关进了有界的块里**。

两种变体的对照：

| 维度 | Full AttnRes | Block AttnRes |
|:---|:---|:---|
| 候选池内容 | 每一份子层输出 \( \mathbf{v}_0..\mathbf{v}_{l-1} \) | \( N \) 份块表示 + 当前块部分和（共 \( N+1 \) 份） |
| 块内聚合方式 | 无块概念，全程注意力 | 标准残差累加（部分和） |
| 每 token 残差路径显存 | \( O(Ld) \)，随深度线性涨 | \( O(Nd) \)，被块数封顶 |
| 粒度 | 最细：逐子层选择 | 块级：块间选择、块内均匀 |
| 定位 | 概念完全体，规模上不可行 | 实用 drop-in（论文实验所用） |

#### 4.3.2 核心流程

Full 与 Block 在第 \( l \) 个子层前的候选池构造对比：

```text
Full AttnRes（l 个候选）                Block AttnRes（≤ N+1 个候选）
v_0   v_1   v_2  ...  v_{l-1}          [块1表示] [块2表示] ... [块N表示] [当前部分和]
  │    │    │         │                  ↑块内 Σ1·v   ↑块内 Σ1·v        ↑块内进行中
  └────┴────┴────┬────┘                  └───────────┴────────┬─────────┘
        softmax 打分 α_{i→l}                                  块间 softmax 打分 α
        Σ α_i · v_i                                            Σ α_s · (块_s 表示)
  显存：l 份，随深度线性增长                                  显存：N+1 份，封顶
```

同一套打分—归一化—聚合算子（4.1 的公式、4.2 的伪查询）两种变体通用，**唯一的区别是候选池怎么构造**——这正是伪代码 `block_attn_res` 中 `V = torch.stack(blocks + [partial_block])` 一行所做的事。

#### 4.3.3 源码精读

**Block 小节全文（本模块的核心陈述）。**

[README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47) —— [L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 一句话讲完 Block 版：Full 直接但规模上要 \( O(Ld) \) 显存；Block 把层划分成 \( N \) 块、块内标准残差累加、只在块级表示上做注意力；约 8 块即可恢复 Full 的大部分收益，作为带边际开销的实用 drop-in。

**overview 图注：三联对照的原文。**

[README.md:L25-L29](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L25-L29) —— 图注三行对应 [assets/overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) 的三个面板：(a) 标准残差的均匀加法累积（[L26](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L26)）；(b) Full AttnRes——每层对所有先前输出做注意力（[L27](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L27)）；(c) Block AttnRes——层分组成块，显存从 \( O(Ld) \) 降到 \( O(Nd) \)（[L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28)）。目测原图可印证：(a) 中每份 \( \mathbf{v}_i \) 以同样大小的权重标记（全 1）汇入求和；(b) 中各历史到当前层的连线带**大小不一**的权重圆点（α 有了强弱之分），并标注 \( O(Ld) \) 显存；(c) 中若干层先在块内均匀求和成块表示，块间再做带 α 的注意力，标注 \( O(Nd) \) 显存（图为目测描述，精确图例以原图为准）。

**候选池构造的伪代码。**

[README.md:L53-L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L61) —— 函数签名的类型注释与文档字符串写明了两类候选：`blocks` 是 N 个 `[B, T, D]` 张量（**已完成的块表示**），`partial_block` 是 `[B, T, D]` 的**块内部分和**（文档字符串记作 \( b_n^i \)）；[L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61) 把它们堆成 `[N+1, B, T, D]` 的 V——这一行就是"Block 版候选池"，换成 Full 版即"全部历史输出堆成 `[l, B, T, D]`"（本讲主实践已实现）。随后的 [L62-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62-L64) 与变体无关，两种共用。

**块边界与部分和的维护（预告）。**

[README.md:L68-L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L68-L81) —— [L68](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L68) `partial_block = hidden_states` 与 [L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L70) 注释「blocks 已包含 token 嵌入」说明嵌入是第一块的内容；[L75-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L75-L77) 在块边界把完成的部分和升级为块表示、开启新块；[L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81)/[L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L88) 两行 `+` 是块内标准残差。完整调度逻辑是 u2-l2 的主题，本讲只看结构。

**Block 版是实验主角的证据。**

[README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99) —— "Block AttnRes matches the loss of a baseline trained with **1.25x more compute**"：scaling law 结论明确落在 Block 版上，佐证它是实用形态。

#### 4.3.4 代码实践

**实践：Full vs Block 的候选份数与显存记账**

1. **实践目标**：把 \( O(Ld) \to O(Nd) \) 从符号变成数字——对给定深度，计算两种变体在残差路径上每 token 需保留的表示份数与 fp32 字节数，并观察 Block 的候选数如何被块数封顶。
2. **操作步骤**：以下为**示例代码**（纯记账，不需要 GPU），保存为 `block_memory.py` 运行：

   ```python
   # block_memory.py —— Full vs Block 的候选份数与激活记账（示例代码）
   L, d = 80, 4096          # 40 个 transformer 层 = 80 个子层（README L74：每层 2 个子层）

   full_bytes = L * d * 4   # Full：最深处要保留 v_0..v_{L-1} 共 L 份
   print(f"Full  AttnRes：每 token 保留 {L} 份 × {d} 维 × 4B = {full_bytes/1024:.0f} KB")
   for N in (2, 4, 8, 16):
       kept = N + 1         # Block：N 份块表示 + 1 份 partial_block（运行和只占一份）
       print(f"Block N={N:2d}：每 token 保留 {kept} 份 ≈ {kept*d*4/1024:3.0f} KB，"
             f"为 Full 的 {kept/L:.1%}")

   # 候选数随深度的演化（以 N=8、每块 10 个子层为例）
   n_sub = L // 8
   for l in (1, 9, 10, 11, 45, 79):
       print(f"子层 {l:2d}：Block 候选 = {l // n_sub} 个块表示 + 1 个部分和 = {l // n_sub + 1} 份"
             f"（Full 需要 {l} 份）")
   ```

3. **需要观察的现象**：Full 为 1280 KB/token；N=8 时 Block 约 144 KB/token（约省 8.9 倍）；右半段输出中 Block 候选数从 1 缓涨到 8 封顶，而 Full 的候选数就是子层号本身、线性增长——深层子层（如 79）Full 要对 79 份历史打分，Block 只对 8 份。
4. **预期结果**：数值由公式直接算出，可手工复核；"块内运行和只占一份显存"是 Block 省显存的机理核心。注意本记账**只算残差路径的表示激活**，不含注意力矩阵等其他激活；总显存的实测基准是第 3 单元第 1 讲的实践。
5. 思考：如果块数开到 \( N = L \)（每块 1 个子层），Block 退化成什么？（每份"块表示"就是单层输出，候选数 \( L+1 \)——回到 Full，只是多了一层冗余求和。）

#### 4.3.5 小练习与答案

**练习 1**：Full AttnRes 为什么没法像标准残差那样"边走边加"折叠显存？

**参考答案**：标准残差每层只读上一层的运行和，新增量加完即可丢弃历史；Full AttnRes 的第 \( l \) 层要对**每一份**历史 \( \mathbf{v}_0..\mathbf{v}_{l-1} \) 分别打分（键必须逐份在场），且更深层还要重复打分，所以所有历史输出都得保留——激活随深度线性增长 \( O(Ld) \)。

**练习 2**：Block AttnRes 在块内保留了标准残差，为什么这不是"问题原样保留"？

**参考答案**：u1-l2 证明稀释/膨胀来自求和项数无界增长；块大小固定，块内项数有界，膨胀与稀释只在块内有界发生。跨块的读取由块间 softmax 注意力重新分配权重（凸组合、幅度有界、内容依赖），深度方向的选择性由块间接管。标准残差被"关进有界的块里"，而非原样保留。

**练习 3**：80 个子层、\( d = 4096 \)、fp32、\( N = 8 \) 时，两种变体残差路径每 token 的激活各约多少？比值是多少？

**参考答案**：Full ≈ \( 80 \times 4096 \times 4\,\mathrm{B} = 1280\,\mathrm{KB} \)；Block ≈ \( (8+1) \times 4096 \times 4\,\mathrm{B} = 144\,\mathrm{KB} \)；比值 \( 80/9 \approx 8.9 \) 倍。

## 5. 综合实践

**综合任务：用代码画出三种残差方案的「深度权重图谱」**——把本讲三个模块串成一张可以和 [assets/overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) 三联图 (a)(b)(c) 直接对照的矩阵图。

思路：第 \( l \) 层读取历史时，对每个候选 \( \mathbf{v}_i \) 用一个权重。把这权重排成 \( L \times L \) 的下三角矩阵（行 = 读取层 \( l \)，列 = 候选 \( i \)），三种方案各有各的"指纹"：标准残差是**全 1**（未归一化）；Full AttnRes 是逐层 softmax 出的**不规则行**；Block AttnRes 是**分段常数行**（块内共享同一权重）。

```python
# depth_weight_map.py —— 三种残差方案的深度权重图谱（示例代码）
import torch
torch.manual_seed(0)

def rms_norm(x, eps=1e-6):
    return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + eps)

def depth_alpha(cands, w):                    # cands: [C, D], w: [D] → 权重 [C]
    return torch.einsum('d, c d -> c', w, rms_norm(cands)).softmax(0)

D, L, N = 16, 12, 3                           # 12 个子层、3 个块（每块 4 个子层）
n_sub = L // N
V = torch.randn(L + 1, D)                     # v_0(嵌入)..v_L，单 token 简化
W = torch.randn(L + 1, D)                     # 每层一个伪查询（未训练，仅演示机制）

std_map  = torch.tril(torch.ones(L + 1, L + 1), diagonal=-1)  # (a) 全 1、未归一化
full_map = torch.zeros(L + 1, L + 1)                           # (b) 逐层 softmax α
blk_map  = torch.zeros(L + 1, L + 1)                           # (c) 块级 α 摊回块内格点

for l in range(1, L + 1):
    full_map[l, :l] = depth_alpha(V[:l], W[l])                 # Full：候选 = v_0..v_{l-1}
    done = l // n_sub                                           # 已完成的完整块数
    rest = V[done * n_sub: l]                                   # 当前块的部分序列
    reps = torch.stack([V[s * n_sub:(s + 1) * n_sub].sum(0) for s in range(done)]
                       + ([rest.sum(0)] if len(rest) else []))  # 块表示 + 部分和
    a = depth_alpha(reps, W[l])
    for s in range(done):                                       # 块权重摊到整块 → 分段常数
        blk_map[l, s * n_sub:(s + 1) * n_sub] = a[s]
    if len(rest):
        blk_map[l, done * n_sub: l] = a[-1]

for name, m in (("标准残差(全1)", std_map), ("Full AttnRes", full_map), ("Block AttnRes", blk_map)):
    row = m[8]
    print(f"{name:>14s} 第8行权重:", " ".join(f"{x:4.2f}" for x in row.tolist()),
          f" 行和 = {row.sum():.2f}")

# 可选绘图（pip install matplotlib）：三联 imshow，对照 overview.png 的 (a)(b)(c)
# import matplotlib.pyplot as plt
# fig, axes = plt.subplots(1, 3, figsize=(12, 3.6), sharey=True)
# for ax, m, t in zip(axes, [std_map, full_map, blk_map],
#                     ["(a) standard: all 1", "(b) full: softmax", "(c) block: piecewise"]):
#     im = ax.imshow(m, cmap="viridis", aspect="auto")
#     ax.set_title(t); ax.set_xlabel("候选 i (v_i)"); ax.set_ylabel("读取层 l")
# fig.colorbar(im, ax=axes, shrink=0.8); plt.savefig("depth_weight_map.png", dpi=150)
```

按以下步骤完成：

1. **跑通并读数**：运行后观察第 8 行权重——标准残差应为 8 个 1.00（行和 8.00，未归一化）；Full 行是 8 个不规则的正数（行和 1.00）；Block 行是 4+4 的分段常数（行和 1.00）。再打印 `m[1]`（最浅层）：三者几乎一致（只有一个候选时 softmax 必为 1），差异随深度才显现。
2. **画图对照**：取消注释生成 `depth_weight_map.png`，与 [overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) 的 (a)(b)(c) 并排目测比较结构。
3. **写三句结论**，对应回 README：(i) 全 1 行 ↔ [L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) 的 "fixed unit weights"；(ii) softmax 行有界且内容依赖 ↔ [L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43) 的 "selective, content-aware"；(iii) 分段常数行用 \( N+1 \) 个自由度近似 Full 的 \( l \) 个 ↔ [L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 的 "attention only over block-level representations"。
4. **诚实标注**：这里的 \( \mathbf{w}_l \) 是随机数，图谱只演示"权重不再全 1"的机制；训练后 \( \alpha \) 会呈现什么结构（如偏好嵌入还是近期块）论文才有一手分析，**待确认**。具体数值随种子变化，**待本地验证**。

预期：三个矩阵的行和分别是 \( l \)（无界）、1、1；Full 与 Block 的差异集中在"行内自由度"——Full 每个候选一个权重，Block 同块候选共享权重。

## 6. 本讲小结

- **聚合公式**：AttnRes 把标准残差的 \( \sum_i 1 \cdot \mathbf{v}_i \) 换成 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \)（[README.md:L39-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L39-L43)）；softmax 保证 \( \alpha \ge 0 \)、\( \sum_i \alpha = 1 \)，输出是凸组合，幅度被 \( \max_i \|\mathbf{v}_i\| \) 封顶——遗留问题 ① 的答案。
- **\( \mathbf{h}_l \) 的语义**：它是第 \( l \) 个子层的**输入**聚合（求和上界 \( l-1 \)），证据是伪代码先 [L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71) 算 h、后 [L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80) 让子层消费 h；注意力发生在**深度轴**（attention over depth），逐 token 独立，不与序列方向的 self-attention 混淆。
- **伪查询**（遗留问题 ② 的答案）：\( \mathbf{w}_l \in \mathbb{R}^d \) 是每层（每应用位置）一个的可学习自由参数（代码形态是 `proj.weight.squeeze()`，[L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63)），不经输入投影；输入依赖由逐 token 的键 \( K=\mathrm{norm}(V) \) 提供——"查询定读取画像，键定内容"。
- **两种变体**：Full 候选是全部历史（必须逐份保留，\( O(Ld) \) 显存）；Block 块内标准残差累加、块间对 \( N+1 \) 份块级表示做注意力（\( O(Nd) \)，[L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47)），约 8 块恢复大部分收益；块内标准残差之所以可接受，是因为求和项数被块大小封顶。
- **主实践**：`full_attn_res` 用 `stack → rms_norm → 两次 einsum + softmax(0)` 实现了与伪代码 L61-L64 同构的聚合算子，验证了形状、归一性、非负性与幅度上界。
- **方法收获**：把三种残差方案统一看成"深度 × 候选"的权重矩阵（全 1 / softmax / 分段常数），是理解本仓库所有实验图的钥匙。

## 7. 下一步学习建议

下一讲（`u2-l1-block-attn-res-function.md`：伪代码导读：block_attn_res 函数逐行解析）将进入第 2 单元，把本讲的公式逐一落到 README 伪代码的每一行。建议：

- 重读 [README.md:L53-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L65)，带着三个问题：① `blocks` 列表里的元素什么时候生成？② `partial_block` 为什么可能为 `None`？③ `proj.weight.squeeze()` 的形状推导你能否独立写出？
- 保留本讲的 `full_attn_res.py` 与 `depth_weight_map.png`——第 2 单元第 4 讲搭建最小训练台时，`full_attn_res` 会被改造成可训练模块接入模型；深度权重图谱在训练后重画，就能看到学习到的 \( \alpha \) 结构。
- 延伸思考：跨深度注意力的计算量随深度如何增长（每层对 \( l \) 份候选打分）？Full 与 Block 的 FLOPs 差异和显存差异是否同阶？——这是第 3 单元第 1 讲（复杂度分析）的入口问题。


