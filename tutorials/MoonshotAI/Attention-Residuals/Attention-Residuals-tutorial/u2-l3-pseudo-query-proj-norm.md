# 关键组件：伪查询、投影层与 RMSNorm 的作用

## 1. 本讲目标

前两讲我们把 Block AttnRes 的骨架搭完了：u2-l1 逐行精读了 `block_attn_res` 的两次 `einsum`，u2-l2 精读了 `forward` 的块边界调度。但在那两讲里，有两样东西一直被当作**黑盒**使用：

- `proj: Linear` —— 我们只说了「直接取它的 `weight` 当伪查询」，没有展开为什么用 Linear 装一个向量、为什么 attn 前和 MLP 前要用**两套独立**的 proj；
- `norm: RMSNorm` —— 我们只说了「打分用 `K = norm(V)`、聚合用原始 `V`」，没有深究为什么必须归一化 K、为什么 V 绝不能归一化、为什么偏偏选 RMSNorm。

本讲就是打开这两个黑盒，并且回答一个工程上最关心的问题：**这套机制到底要付出多少参数和计算的代价？** README 声称它是「marginal overhead（边际开销）」，我们会亲手把这个说法算成数字。

学完本讲，你应该能够：

1. 解释**伪查询** \(\mathbf{w}_l \in \mathbb{R}^d\) 为什么是每层一个的自由可学习向量、输入依赖性到底从哪里进入、初始化时权重为什么接近均匀。
2. 说清 `attn_res_proj` / `mlp_res_proj` 的真实身份：Linear 在这里是**参数化容器**，它与 `einsum` 写法数学等价，而且 bias 在这里完全无效（能证明梯度恒为零）。
3. 理解 `attn_res_norm` / `mlp_res_norm` 的键值分离设计：归一化 K 是为了「只按方向打分」，保留 V 是为了让输出保持凸组合的有界性——这与 PreNorm 是同一种哲学。
4. 量化开销：每处 attn_res 约 \(2d\) 参数、每层 \(4d\)、相对主干占比约 \(1/(3d)\)，并大致估计 FLOPs 与激活显存。

本讲的主实践是一个**三组小型消融实验**：分别去掉 RMSNorm、换成 LayerNorm、去掉 proj 用裸参数，观察深度注意力权重分布与输出幅度的变化。

## 2. 前置知识

### 2.1 复习：标准注意力里的 query 从哪里来

标准自注意力中，每个 token 的查询由**它自己的当前表示**投影而来：

\[ \mathbf{q}_t = \mathbf{x}_t \mathbf{W}_q, \qquad \mathbf{W}_q \in \mathbb{R}^{d \times d} \]

也就是说，「我想找什么」取决于「我现在是什么」。这需要 \(d^2\) 个参数，且 query 的幅度会随 \(\mathbf{x}_t\) 的幅度变化。

AttnRes 走了另一条路（README 原文称其为 pseudo-query，伪查询）：

| | 标准自注意力 | AttnRes 的深度注意力 |
|:---|:---|:---|
| 查询来源 | 当前 token 表示投影 \(\mathbf{x}_t \mathbf{W}_q\) | 层私有的自由向量 \(\mathbf{w}_l\)，**不经任何输入投影** |
| 查询参数量 | \(d^2\) | \(d\) |
| 键来源 | \(\mathbf{x} \mathbf{W}_k\)（token 序列） | \(\mathrm{RMSNorm}(\mathbf{v}_n)\)（历史块表示） |
| 注意力轴 | 序列轴 T | 深度轴（N+1 个候选） |
| 输入依赖性 | 查询侧和键侧都有 | **只在键侧** |

### 2.2 RMSNorm 与 LayerNorm 的差别

u2-l1 已给出 RMSNorm 公式，这里补上与 LayerNorm 的对比（本讲消融实验会用到）：

\[ \mathrm{RMSNorm}(\mathbf{x}) = \frac{\mathbf{x}}{\sqrt{\tfrac{1}{d}\sum_k x_k^2 + \epsilon}} \odot \mathbf{g}, \qquad \mathrm{LayerNorm}(\mathbf{x}) = \frac{\mathbf{x} - \bar{x}}{\sqrt{\tfrac{1}{d}\sum_k (x_k - \bar{x})^2 + \epsilon}} \odot \mathbf{g} \]

关键差别只有一处：**LayerNorm 先减去各分量的均值 \(\bar{x}\)，RMSNorm 不减**。当向量带有明显的「直流偏置」（所有分量共同抬高一个常数）时，两者给出的键方向会明显不同。另外，RMSNorm 输出的均方根恒为 1（增益为 1 时），因此其 L2 范数恒为 \(\sqrt{d}\)——这个性质本讲会反复用到。

README 伪代码只声明了类型 `norm: RMSNorm`，内部实现细节（epsilon 取值、是否含增益 \(\mathbf{g}\)）仓库未给出，以论文为准（待确认）。

### 2.3 softmax 的两个性质

本讲的多个结论都建立在 softmax 的两个性质上：

1. **平移不变性**：给所有 logits 加同一个常数 \(c\)，softmax 输出不变：
   \[ \mathrm{softmax}(\mathbf{z} + c\mathbf{1}) = \mathrm{softmax}(\mathbf{z}) \]
   这是 4.2 节证明「bias 无效」的依据。
2. **对 logits 方差敏感**：logits 彼此接近 → 输出接近均匀；某个 logit 明显更大 → 权重向它集中（极端时接近 one-hot）。直觉规则：候选数为 \(N+1\) 时，logits 的标准差 \(\sigma\) 大致决定了分布形态——\(\sigma \ll 1\) 时接近均匀 \(1/(N+1)\)，\(\sigma \gg 1\) 时接近 one-hot。这是 4.1 节初始化分析和综合实践 (c) 组的理论基础。

### 2.4 数参数、数计算量的基本方法

- `nn.Linear(in, out, bias=False)`：参数量 `in × out`；本讲的 proj 是 `Linear(d, 1)`，参数 \(1 \times d = d\) 个，`weight` 形状 `[1, d]`。
- RMSNorm（含增益、无偏置的常见实现）：参数量 \(d\)。
- FLOPs 估算：按乘加次数（MAC）粗算，`einsum` 中每个被求和掉的下标贡献一次乘加。例如 `'d, n b t d -> n b t'` 对深度 \(n\) 的每个位置做 \(d\) 次乘加，总量 \((N{+}1) \cdot B \cdot T \cdot d\)。

### 2.5 符号表

| 符号 | 含义 | 本讲实践取值 |
|:---:|:---|:---:|
| B | batch 大小 | 2 |
| T | 序列长度 | 16 |
| D（d） | 隐藏维度 | 64 |
| N | 已完成块数；候选共 N+1 份 | 8（候选 9 份） |
| L | 模型总层数（每层 2 个子层） | 举例用 48 |
| \(\mathbf{w}_l\) | 第 \(l\) 个位点（attn 前 / MLP 前）的伪查询 | — |
| \(\mathbf{v}_n\) | 第 \(n\) 份候选（块表示或部分和） | — |
| \(\alpha_n\) | 深度注意力权重，\(\sum_n \alpha_n = 1\) | — |

## 3. 本讲源码地图

本仓库是论文发布仓库，没有工程代码，README 中的伪代码块是唯一可精读的「源码」。本讲涉及的全部材料如下：

| 位置 | 内容 | 本讲用途 |
|:---|:---|:---|
| [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33) | AttnRes 定位：drop-in 替换 | 开篇语境 |
| [README.md:L41-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L41-L43) | 核心公式 + 伪查询定义 | 4.1 节精读 |
| [README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47) | Block 划分、~8 块、marginal overhead | 4.4 节精读 |
| [README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) | PyTorch 风格伪代码全块 | 4.2 / 4.3 节精读（L53、L61-L64、L71、L84） |
| [README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28) | 图注：O(Ld) → O(Nd) | 4.4 节显存分析 |
| [README.md:L121-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L127) | 训练动态：幅度有界、梯度均匀 | 4.3 节佐证 |
| `Attention_Residuals.pdf` | 论文全文 | norm 内部细节、消融数据以论文为准 |
| `assets/training_dynamics.png` | 幅度/梯度三联图 | 4.3 节对照 |

## 4. 核心概念与源码讲解

### 4.1 伪查询设计：一个不属于任何 token 的查询向量

#### 4.1.1 概念说明

README 对伪查询的正式定义只有一句话：

> the weights \(\alpha_{i \to l}\) are computed via a single learned pseudo-query \(\mathbf{w}_l \in \mathbb{R}^d\) per layer（[README.md:L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43)）

拆开读，有四个要点：

1. **「per layer / 每层」**：每个 transformer 层有自己的 \(\mathbf{w}_l\)。结合 u2-l2 讲过的两个位点，更精确地说是**每个位点一个**——attn 前一个、MLP 前一个（见 4.2 节）。
2. **「learned / 可学习」**：它是被梯度下降直接优化的自由参数，初始化后与输入无关。
3. **「pseudo / 伪」**：它不从任何 token 的表示投影而来。标准注意力里 query 是「我问的问题取决于我现在的内容」；伪查询是「这一层永远问同一个问题」。
4. **\(\in \mathbb{R}^d\) 而不是 \(\mathbb{R}^{d \times d}\)**：是一个向量，不是一个投影矩阵——这是开销只有 \(d\) 量级的根源。

那么权重怎么做到「输入依赖」（README L33 强调 learned, input-dependent）？答案是**输入依赖性完全由键侧承担**：候选 \(\mathbf{v}_n\) 是各块在**每个 token 位置** \((b, t)\) 上的表示，归一化后的键 \(\mathbf{k}_n(b,t)\) 逐位置不同，因此点积 \(\langle \mathbf{w}_l, \mathbf{k}_n(b,t) \rangle\) 逐位置不同，softmax 后的 \(\alpha\) 自然逐位置不同。一个类比：伪查询是**固定的检索词**，每个 token 面对的资料库（历史块在该位置的表示）不同，所以每个 token 检索到的内容配比不同。

为什么设计成自由向量而不是从输入投影？（README 未给出理由，以下是依据结构性质的设计分析，标注为推断；论文或有更详细的论证，待确认。）

- **避免鸡生蛋**：位点处的 \(\mathbf{h}\)（聚合结果）正是这次 `block_attn_res` 的**输出**，拿它投影 query 在计算顺序上说不通；若改用 `partial_block` 投影，又把「选谁」和「当前部分和的内容」耦合死了。
- **幅度解耦**：u1-l2 实测过 PreNorm 部分和幅度随深度增长。若 query 来自输入，其范数会随深度漂移，logits 的整体尺度失去锚点；固定 \(\mathbf{w}_l\) 的范数由优化器直接掌管，logits 尺度稳定可控。
- **参数经济**：\(d\) 对 \(d^2\)，省下的量级正是 4.4 节「边际开销」结论的一半来源。

#### 4.1.2 核心流程

单个位点上的打分过程（逐 token 独立）：

```text
输入: 候选 V = [v_0, ..., v_N]  (N+1 份, 每份 [B, T, D])，伪查询 w ∈ R^D
1. K = RMSNorm(V)                      # 键：单位均方根，只保留方向
2. 对每个位置 (b, t)、每个候选 n:
       logit_n(b,t) = ⟨w, k_n(b,t)⟩
3. α_(·)(b,t) = softmax_n( logits_(·)(b,t) )   # 沿深度归一化
输出: α ∈ [N+1, B, T]（非负、沿深度和为 1）
```

两个定量的性质（增益为 1 时）：

- **logit 的解析形式**：RMSNorm 输出的 L2 范数恒为 \(\sqrt{d}\)，所以
  \[ \mathrm{logit}_n = \lVert \mathbf{w} \rVert \cdot \sqrt{d} \cdot \cos\theta_n \]
  其中 \(\theta_n\) 是 \(\mathbf{w}\) 与键方向的夹角。logit 的绝对值被 \(\lVert\mathbf{w}\rVert\sqrt{d}\) 封顶，**方向匹配度是唯一的自由变量**。
- **初始化接近均匀**：`nn.Linear` 默认初始化为 \(\mathrm{U}(-1/\sqrt{d},\, 1/\sqrt{d})\)，每个分量方差 \(1/(3d)\)；键的分量均方根为 1。于是初始 logit 的方差约为
  \[ \mathrm{Var}(\mathrm{logit}) \approx d \cdot \frac{1}{3d} \cdot 1 = \frac{1}{3}, \qquad \sigma \approx 0.58 \]
  按 2.3 节的直觉规则，\(\sigma \approx 0.58 \ll 1\)，softmax 输出接近均匀——这从数学上解释了 u2-l1 结尾那句「初始化时接近均匀（约 1/(N+1)），选择性由训练习得」。

#### 4.1.3 源码精读

**伪查询的定义**（README 正文）：

> [README.md:L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43)
> where the weights $\alpha_{i \to l}$ are computed via a single learned pseudo-query $\mathbf{w}_l \in \mathbb{R}^d$ per layer.

这一行是伪查询唯一的「官方定义」，注意 \(\in \mathbb{R}^d\)——向量而非矩阵。

**函数签名中伪查询的载体**：

> [README.md:L53](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53)
> `def block_attn_res(blocks, partial_block, proj: Linear, norm: RMSNorm) -> Tensor:`

伪查询不是独立参数传入，而是**藏在 `proj: Linear` 里**——这是 4.2 节的主题。

**伪查询参与运算的唯一一行**：

> [README.md:L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63)
> `logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)`

`'d, n b t d -> n b t'`：向量 \(\mathbf{w}\) 与每个候选的键做点积（对 \(d\) 求和），**输出逐 \((n, b, t)\) 一个标量**——这就是「固定查询 + 逐位置键 ⇒ 逐位置权重」的代码形态。对照 u2-l1 讲过的维度表：输入侧的 `d` 下标消失（被求和），`n b t` 全部保留。

#### 4.1.4 代码实践：验证「固定查询也能产生逐 token 的输入依赖权重」

1. **实践目标**：用最小实验确认两件事——(i) 同一层内，不同 token 位置得到**不同**的深度权重 \(\alpha\)；(ii) 权重的输入依赖性来自键侧：换一批内容不同的候选，\(\alpha\) 随之改变。
2. **操作步骤**：

```python
# 示例代码
import torch

torch.manual_seed(0)
B, T, D, N = 1, 6, 32, 4

w = torch.nn.Linear(D, 1, bias=False).weight.squeeze().detach()  # 伪查询 [D]
blocks = [torch.randn(B, T, D) for _ in range(N)]                # N 份块表示
partial = torch.randn(B, T, D)                                   # 部分和

V = torch.stack(blocks + [partial])                              # [N+1, B, T, D]
K = torch.nn.functional.rms_norm(V, (D,))                        # RMSNorm 键
logits = torch.einsum('d, n b t d -> n b t', w, K)
alpha = logits.softmax(0)                                        # [N+1, B, T]

ent = -(alpha * alpha.clamp_min(1e-12).log()).sum(0) / torch.log(
    torch.tensor(N + 1.0))                                       # 归一化熵, 接近 1 = 均匀
for t in range(T):
    print(f"t={t}  argmax={alpha[:, 0, t].argmax().item()}  "
          f"max={alpha[:, 0, t].max():.3f}  entropy={ent[0, t]:.3f}")

# 换一批内容不同的候选, 观察 alpha 随之变化
blocks2 = [torch.randn(B, T, D) for _ in range(N)]
V2 = torch.stack(blocks2 + [torch.randn(B, T, D)])
alpha2 = torch.einsum('d, n b t d -> n b t', w,
                      torch.nn.functional.rms_norm(V2, (D,))).softmax(0)
print("候选变化后权重平均绝对变化:",
      (alpha2 - alpha).abs().mean().item())
```

3. **需要观察的现象**：每个 `t` 打印出的 `argmax`、`max`、熵各不相同（同一查询、不同资料库 ⇒ 不同检索结果）；换候选后平均绝对变化明显大于 0。
4. **预期结果**：初始权重接近均匀（熵接近 1，`max` 略高于 \(1/(N{+}1)=0.2\)），但**逐 token 有差异**；换候选后 \(\alpha\) 整体改变。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么叫「伪」查询？它和标准注意力 query 的本质区别是什么？

> **答案**：标准注意力的 query 由当前 token 的表示投影而来（\(\mathbf{x}_t \mathbf{W}_q\)），「问什么」取决于「现在是什么」；伪查询是层私有的自由可学习向量，不经任何输入投影，「问什么」与输入无关。输入依赖性全部由键侧（逐位置的块表示）提供。

**练习 2**：若把伪查询改成由 `partial_block` 投影产生的真 query（\(\mathbf{q} = \mathbf{h}_{partial}\mathbf{W}_q\)，\(\mathbf{W}_q \in \mathbb{R}^{d\times d}\)），每处参数量变为多少？结合 u1-l2 的结论说一个潜在风险。

> **答案**：参数量变为 \(d^2\)（是现在 \(2d\) 的 \(d/2\) 倍）。风险：u1-l2 实测 PreNorm 部分和幅度随深度增长，query 范数会随深度漂移，导致 logits 的整体尺度随深度失控（浅层近均匀、深层近 one-hot），破坏各层选择行为的稳定性。

**练习 3**：判断对错：深度权重 \(\alpha\) 对每个 token 位置 \((b,t)\) 都是独立计算的一组分布。

> **答案**：对。`einsum` 输出 `[N+1, B, T]`，softmax 沿第 0 维（深度）独立进行，`B×T` 个位置各得一个 \(N{+}1\) 维分布；注意力只作用于深度轴、不跨 token（u1-l3 已确立这一性质，这里从代码维度再次印证）。

### 4.2 投影层：Linear 在这里的真实身份

#### 4.2.1 概念说明

再看一遍伪代码中 proj 的用法，你会发现一件反直觉的事：**`proj` 从未被当作层来调用**——没有 `proj(x)`，只有 `proj.weight.squeeze()`。`nn.Linear` 在这里不是「变换」，而是**装着伪查询向量的参数化容器**：

- `nn.Linear(d, 1, bias=False)` 的 `weight` 形状是 `[1, d]`，`squeeze()` 后就是 `[d]` 的伪查询 \(\mathbf{w}_l\)；
- 优化器对 `weight` 的更新与对任何参数一样，训练中 \(\mathbf{w}_l\) 逐步学到「这一层想检索什么」。

为什么用 Linear 容器，而不是裸的 `nn.Parameter(torch.zeros(d))`？两个实实在在的理由（第二点可量化）：

1. **接口统一**：以模块形式注入（`self.attn_res_proj`），与模型其他组件的管理方式一致，方便整体搬运、保存、替换。
2. **默认初始化尺度恰好合适**：Linear 默认初始化 \(\mathrm{U}(-1/\sqrt{d}, 1/\sqrt{d})\)，由 4.1.2 的推导，初始 logit 标准差 \(\approx 0.58\)，softmax 近均匀——**起步平滑**。若裸用 `torch.randn(d)`，范数约为 \(\sqrt{d}\)，初始 logit 标准差约为 \(\sqrt{d}\)，对 \(d=64\) 就是 \(\sigma \approx 8\)：初始权重即近 one-hot，相当于训练还没开始就随机锁定了「只看某一个块」。综合实践的 (c) 组会实测这一差别。

**为什么 attn 前和 MLP 前要用两套独立的 proj？** 伪代码在两个位点分别传入了 `self.attn_res_proj`（[README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)）与 `self.mlp_res_proj`（[README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84)）。理由是两个位点的**检索任务不同**：注意力前要取「适合拿去做 token 间混合」的表示，MLP 前要取「适合做逐通道非线性变换」的表示——问题不同，伪查询理应不同（README 未明说动机，属设计分析；论文是否做了共享/独立的消融，待确认）。

#### 4.2.2 核心流程

**Linear 调用与 einsum 写法的数学等价**。设键张量 \(\mathbf{K} \in \mathbb{R}^{(N+1) \times B \times T \times d}\)、`proj` 的权重 \(\mathbf{W} \in \mathbb{R}^{1 \times d}\）、偏置 \(b \in \mathbb{R}\)，则：

\[ \mathrm{proj}(\mathbf{K}) = \mathbf{K}\mathbf{W}^\top + b \in \mathbb{R}^{(N+1) \times B \times T \times 1} \]

最后 `squeeze(-1)` 恰好就是 [README.md:L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63) 的 `logits`（外加一个常数 \(b\)）。所以「Linear 容器」并不勉强——把它真的当 Linear 用，算出的就是同一批 logits。

**bias 在这里注定无效**。`Linear(d, 1)` 的 bias 形状是 `[1]`，是一个标量，会给**所有候选**的 logit 加上同一个 \(b\)。由 softmax 平移不变性（2.3 节）：

\[ \alpha = \mathrm{softmax}(\mathbf{z} + b\mathbf{1}) = \mathrm{softmax}(\mathbf{z}) \]

损失与 \(b\) 完全无关，因此 \(\partial \mathcal{L}/\partial b \equiv 0\)——bias 参数永远停在初始值，不参与任何学习。等价地，这里的 Linear 就是 `bias=False` 的。这也解释了为什么伪代码只取 `weight`：**不是省略，是 bias 根本没有作用**。

**每层的参数归属**（承接 u2-l2 的调度）：每个 transformer 层持有 4 个与 attn_res 有关的模块——`attn_res_proj`、`attn_res_norm`（注意力前位点用）与 `mlp_res_proj`、`mlp_res_norm`（MLP 前位点用），全部是层内 `self.` 属性，跨层不共享。

#### 4.2.3 源码精读

**签名声明了两个组件的类型**：

> [README.md:L53](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53)
> `def block_attn_res(blocks: list[Tensor], partial_block: Tensor, proj: Linear, norm: RMSNorm) -> Tensor:`

`proj: Linear`、`norm: RMSNorm` 以参数形式注入——u2-l1 说过这让 `block_attn_res` 成为无状态的纯计算函数，参数归层所有。

**取 weight 而不调用层**：

> [README.md:L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63)
> `logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)`

`proj.weight` 形状 `[1, d]`，`squeeze()` 成 `[d]` 才能满足 einsum 第一个操作数 `'d'` 的一维下标要求。

**两个位点各传一套独立参数**：

> [README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)
> `h = block_attn_res(blocks, partial_block, self.attn_res_proj, self.attn_res_norm)`（注意力前）
>
> [README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84)
> `h = block_attn_res(blocks, partial_block, self.mlp_res_proj, self.mlp_res_norm)`（MLP 前）

同名成对出现（`attn_res_*` / `mlp_res_*`），结构对称、参数独立。

#### 4.2.4 代码实践：验证「Linear 调用 ≡ einsum 打分」与「bias 无效」

1. **实践目标**：用代码证实 4.2.2 的两个论断——(i) `proj(K).squeeze(-1)` 与 einsum 的 logits 数值相同；(ii) 加不加 bias，深度权重 \(\alpha\) 完全不变。
2. **操作步骤**：

```python
# 示例代码
import torch

torch.manual_seed(0)
B, T, D, N = 2, 5, 16, 3
K = torch.randn(N + 1, B, T, D)

proj_nb = torch.nn.Linear(D, 1, bias=False)     # 无 bias
proj_b  = torch.nn.Linear(D, 1, bias=True)      # 有 bias
with torch.no_grad():
    proj_b.weight.copy_(proj_nb.weight)         # 两组 weight 对齐, 只差 bias

logits_einsum = torch.einsum('d, n b t d -> n b t',
                             proj_nb.weight.squeeze(), K)
logits_linear = proj_nb(K).squeeze(-1)          # 把 Linear 真的当层调用
print("einsum 与 Linear 调用一致:",
      torch.allclose(logits_einsum, logits_linear, atol=1e-6))

a_nb = logits_einsum.softmax(0)
a_b  = proj_b(K).squeeze(-1).softmax(0)
print("加 bias 后权重不变:", torch.allclose(a_nb, a_b, atol=1e-6))
print("proj 参数量:", proj_nb.weight.numel(), "=", D)
```

3. **需要观察的现象**：两个 `allclose` 都应为 `True`；参数量打印为 `d`。
4. **预期结果**：全部成立（等价性是数学恒等式，bias 平移被 softmax 抵消是严格性质）；浮点上可能有 1e-6 量级差异。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`Linear(d, 1)` 的 bias 为什么在这个场景里学不到任何东西？

> **答案**：bias 是形状 `[1]` 的标量，给所有 \(N{+}1\) 个候选的 logit 同加一个常数；由 softmax 平移不变性，\(\alpha\) 与 bias 无关，损失对 bias 的梯度恒为零，参数停留在初始值。

**练习 2**：`attn_res_proj` 与 `mlp_res_proj` 语法上可以共享同一个模块。共享会损失什么？

> **答案**：会强迫注意力前和 MLP 前两个位点使用同一个伪查询，即「问同一个问题」。两个位点的下游变换不同（token 间混合 vs 逐通道变换），理想的检索偏好可能不同；共享后只能折中，表达力下降。代价方面共享每层省 \(d\) 个参数，与 \(12d^2\) 的主干相比两头都微不足道，所以独立是「便宜的自由」。（README 未提供共享消融，待确认。）

**练习 3**：`proj.weight` 为什么要 `squeeze()`？不 squeeze 会怎样？

> **答案**：einsum 表达式 `'d, n b t d -> n b t'` 要求第一个操作数是一维（下标 `d`）；`weight` 形状是 `[1, d]`，不 squeeze 维度不匹配会直接报错。也可以改写成 `'o d, n b t d -> o n b t'` 保留 `o=1` 维再 squeeze 输出，但那样更绕。

### 4.3 归一化：打分用 K、聚合计原料

#### 4.3.1 概念说明

伪代码里 norm 只出现了一次，却在键和值两个角色之间划了一条清晰的界线：

> [README.md:L62-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62-L64)
> ```python
> K = norm(V)
> logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
> h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
> ```
> 第 62 行：**打分用归一化后的 K**；第 64 行：**聚合用原始的 V**。

这就是 u2-l1 点到为止的「键值分离」，本讲把两侧的「为什么」补齐。

**为什么 K 必须归一化——只按方向打分**。若直接用 \(\mathbf{v}_n\) 打分，logit \(= \langle \mathbf{w}, \mathbf{v}_n \rangle\) 的大小同时取决于方向匹配**和幅度大小**：

\[ \langle \mathbf{w}, \mathbf{v}_n \rangle = \lVert\mathbf{w}\rVert \, \lVert\mathbf{v}_n\rVert \cos\theta_n \]

u1-l2 实测过：PreNorm 下块和的幅度随深度增长。于是**深层的块会单纯因为「个子大」而在 softmax 中胜出或出局**（取决于 \(\cos\theta\) 的符号），「按内容选择」退化成「按大小选择」——u1-l2 的幅度病理从残差主干潜入了选择权重本身。归一化 K 之后，\(\lVert\mathbf{k}_n\rVert = \sqrt{d}\) 恒定，logit 只剩方向项 \(\lVert\mathbf{w}\rVert\sqrt{d}\cos\theta_n\)：**候选无法靠变大争宠，只能靠方向对路**。这直接保障了「selective, content-aware」（README L33）这句话里 content-aware 的成色。

**为什么 V 绝不能跟着归一化——凸组合的有界性**。聚合若用 \(\mathrm{norm}(V)\)，输出 \(\mathbf{h}\) 会变成单位均方根向量的组合，**候选的幅度信息被整体抹掉**：块累计了多少内容、各块孰轻孰重，这些信号全部丢失。用原始 V 聚合时：

\[ \mathbf{h} = \sum_{n=0}^{N} \alpha_n \mathbf{v}_n, \qquad \alpha_n \ge 0, \; \sum_n \alpha_n = 1 \quad\Longrightarrow\quad \mathbf{h} \in \mathrm{conv}\{\mathbf{v}_0, \dots, \mathbf{v}_N\} \]

\(\mathbf{h}\) 落在候选的**凸包**内，范数被最大候选封顶且与深度无关——这正是 u1-l3 论证的「输出幅度有界」的来源，也是 README L123「output magnitudes remain bounded across depth」的机制基础。一句话：**有界性来自权重侧（softmax），不来自对数值动手脚**。

**与 PreNorm 的同构**。这套「归一化发生在使用处、不发生在数值通路上」的设计，和 PreNorm 是同一种哲学：

| | 标准 PreNorm 层 | AttnRes 的块间注意力 |
|:---|:---|:---|
| 归一化的对象 | 残差主干（喂给子层前） | 候选 V（喂给打分前） |
| 不归一化的对象 | 残差主干本身（累加保持原值） | 候选 V 本身（聚合保持原值） |
| 一句话 | 「读归一化版，存原始版」 | 「打分用归一化版，聚合用原始版」 |

**为什么选 RMSNorm 而不是 LayerNorm**。README 在签名里直接声明了 `norm: RMSNorm`（[README.md:L53](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53)），与 `attn_norm` / `mlp_norm` 等现代 LLM 组件的惯例一致（LLaMA 系等均用 RMSNorm）：省去减均值，计算更省、无 bias 项。两者的实际差别集中在「候选带明显直流偏置时」（见 2.2 节与综合实践 (b) 组）。README 未解释选型理由，是否在论文中做了 norm 类型消融，待确认。

#### 4.3.2 核心流程

`K = norm(V)` 一次完成所有候选的归一化：RMSNorm 沿最后一维 D 操作，前面的 \(N{+}1\)、B、T 维全部当作独立批次（u2-l1 的 2.4 节已确认这一点），因此无需循环。

**尺度不变性**（本模块最核心的性质）：对任意候选乘正数 \(c\)，

\[ \mathbf{v}_n \to c\,\mathbf{v}_n \;\Rightarrow\; \mathbf{k}_n = \frac{c\,\mathbf{v}_n}{\mathrm{RMS}(c\,\mathbf{v}_n)} = \frac{c\,\mathbf{v}_n}{c\,\mathrm{RMS}(\mathbf{v}_n)} = \frac{\mathbf{v}_n}{\mathrm{RMS}(\mathbf{v}_n)} \;\text{不变} \;\Rightarrow\; \alpha \text{ 不变} \]

**输出侧的有界性**：由凸组合，
\[ \lVert \mathbf{h}(b,t) \rVert_2 \le \max_n \lVert \mathbf{v}_n(b,t) \rVert_2 \]
输出幅度只由「被选中的候选里最大有多大」决定，与模型深度 \(L\)、已完成块数 \(N\) 都无关。

#### 4.3.3 源码精读

**归一化只发生在键上**：

> [README.md:L62](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62)
> `K = norm(V)   # [N+1, B, T, D]`

对堆叠后的 V 整体做一次 RMSNorm，得到打分专用的键。

**聚合用的是原始 V**：

> [README.md:L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L64)
> `h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)`

第二个操作数是 `V` 而不是 `K`——凸组合作用于原始候选，幅度信息与有界性同时保住。

**与下游 PreNorm 的对照（设计同构的代码证据）**：

> [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)
> `attn_out = self.attn(self.attn_norm(h))`
>
> [README.md:L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87)
> `mlp_out = self.mlp(self.mlp_norm(h))`

聚合结果 \(\mathbf{h}\) 在进入子层前再由 `attn_norm` / `mlp_norm` 归一化一次——「使用处归一化」在 attn_res 内部和外部各出现一次，风格完全一致。

**实证图对照**：[README.md:L121-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L127) 与 `assets/training_dynamics.png`：AttnRes 的输出幅度随深度保持有界、梯度范数跨层更均匀——前者正是凸组合有界性的宏观表现。

#### 4.3.4 代码实践：验证尺度不变性与「不归一化会怎样」

1. **实践目标**：证实 (i) 把任意一个候选放大 100 倍，深度权重 \(\alpha\) 纹丝不动；(ii) 若去掉 norm 直接用 V 打分，\(\alpha\) 立刻被大块垄断；(iii) 输出范数不超过最大候选。
2. **操作步骤**：

```python
# 示例代码
import torch

torch.manual_seed(0)
B, T, D, N = 1, 4, 32, 4
w = torch.nn.Linear(D, 1, bias=False).weight.squeeze().detach()

blocks = [torch.randn(B, T, D) for _ in range(N)]
partial = torch.randn(B, T, D)

def attn_res(blocks, partial, use_norm=True):
    V = torch.stack(blocks + [partial])
    K = torch.nn.functional.rms_norm(V, (D,)) if use_norm else V
    logits = torch.einsum('d, n b t d -> n b t', w, K)
    alpha = logits.softmax(0)
    h = torch.einsum('n b t, n b t d -> b t d', alpha, V)
    return alpha, h

alpha0, _ = attn_res(blocks, partial, use_norm=True)
blocks_scaled = [b * 100 if i == 2 else b for i, b in enumerate(blocks)]
alpha1, _ = attn_res(blocks_scaled, partial, use_norm=True)
print("有 norm 时放大某块 100 倍, alpha 不变:",
      torch.allclose(alpha0, alpha1, atol=1e-5))

alpha2, _ = attn_res(blocks, partial, use_norm=False)
alpha3, _ = attn_res(blocks_scaled, partial, use_norm=False)
print("无 norm 时放大前后的平均 |Δalpha|:",
      (alpha3 - alpha2).abs().mean().item())

_, h0 = attn_res(blocks, partial, use_norm=True)
V = torch.stack(blocks + [partial])
print("||h|| <= max||v|| :",
      bool((h0.norm(dim=-1) <= V.norm(dim=-1).max(0).values + 1e-5).all()))
```

3. **需要观察的现象**：第一个打印为 `True`（尺度不变性）；第二个打印明显大于 0（无 norm 时权重被幅度扰动）；第三个为 `True`（凸组合有界）。
4. **预期结果**：三条全部成立——尺度不变性与凸包上界是严格数学性质；无 norm 时放大 100 倍的块会显著改变甚至垄断 \(\alpha\)（方向符号决定是被追捧还是被打压）。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：有人建议「不必归一化 K，只要给 logits 除以 \(\sqrt{d}\) 缩放（类似注意力的 temperature）」。这个方案能替代 K 归一化吗？

> **答案**：不能。\(\sqrt{d}\) 缩放是对**所有** logits 的统一温度调节，不改变候选之间的相对大小；而某个候选幅度大时，它家 logit 的绝对值大（正负号由 \(\cos\theta\) 决定），softmax 后无论方向对不对都会被推向极端——幅度支配问题原样保留。K 归一化是在**每个候选各自**的源头消除幅度因子，两者作用层面不同。

**练习 2**：把 RMSNorm 换成 LayerNorm，什么情况下权重分布会明显不同？

> **答案**：当候选带有明显**直流偏置**（各分量整体抬高一个常数）时。LayerNorm 会先减去分量均值，偏置被扣除，键反映内容的差异；RMSNorm 不减，所有键被共同的偏置方向主导、彼此趋同，logits 挤在一起，\(\alpha\) 趋向均匀、对内容不敏感。综合实践 (b) 组会构造这种情形实测。

**练习 3**：判断：\(\mathbf{h}\) 的幅度依赖候选的幅度吗？

> **答案**：依赖但被封顶。\(\mathbf{h}\) 是原始候选的凸组合，其范数上界是 \(\max_n \lVert\mathbf{v}_n\rVert\)；候选集体变小则 \(\mathbf{h}\) 随之变小，但**无论深度多深、块数多少**，上界始终由当轮候选决定——「有界」指的就是与深度无关这一性质。

### 4.4 开销分析：为什么敢说是「边际开销」

#### 4.4.1 概念说明

[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 的原话是：Block AttnRes 是「a practical drop-in replacement with **marginal overhead**（边际开销）」。这一节把「边际」二字算成数字，分三个维度：**参数量、计算量（FLOPs）、激活显存**。先给结论总表，再逐项推导：

| 维度 | 每层增量 | 与每层主干对比 | 量级判断 |
|:---|:---|:---|:---|
| 参数 | \(4d\)（2 位点 × \(2d\)） | 主干约 \(12d^2\) | 占比 \(1/(3d)\)：0.065%（d=512）→ 0.008%（d=4096） |
| 前向 FLOPs | 约 \(6(N{+}1)BTd\) 每层 | 注意力 \(4BTd + 2BT^2d\)、MLP 约 \(16BTd\) | \(N \approx 8 \ll T\)，占比约 1% 以下 |
| 激活显存 | 约 \(2(N{+}1)BTd\) 每层 | Full AttnRes 约 \(2LBTd\) | O(Ld) → O(Nd)（README L28） |

（主干 FLOPs 按 expansion=4 的常规 MLP 估，系数是粗略估计；现代模型用门控/MoE 时系数变化，量级结论不变。）

#### 4.4.2 核心流程

**参数量推导**。每处 attn_res 位点引入两个组件：

- `proj = Linear(d, 1)`：参数 \(1 \times d = d\) 个；
- `norm = RMSNorm(d)`：增益 \(\mathbf{g}\) 为 \(d\) 个（按含增益、无偏置的常见实现计，以论文为准，待确认）；

合计每处 \(2d\)。每层两个位点（attn 前 + MLP 前），每层 \(4d\)；L 层模型共 \(4Ld\)。对照主干：每层注意力投影（Q/K/V/O）约 \(4d^2\)，MLP（expansion=4）约 \(8d^2\)，合计约 \(12d^2\)，占比

\[ \frac{4d}{12d^2} = \frac{1}{3d} \]

代入几个宽度：d=512 → 0.065%；d=1024 → 0.033%；d=4096 → 0.008%。以 d=4096、L=48 的模型为例，attn_res 全部参数 \(4 \times 48 \times 4096 \approx 78.6\text{万}\)，而每层主干就有约 2 亿参数——**这就是「边际」的底气**。

**FLOPs 推导（前向，按 MAC 粗算）**。每处位点的三步（对照 [README.md:L61-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L64)）：

| 操作 | MAC 量 |
|:---|:---|
| `K = norm(V)` | 约 \(3(N{+}1)BTd\)（求平方和、开方、逐点除/乘增益） |
| 第一次 einsum（打分） | \((N{+}1)BTd\) |
| 第二次 einsum（聚合） | \((N{+}1)BTd\) |
| softmax | 约 \(3(N{+}1)BT\)，可忽略 |
| 合计 | 约 \(6(N{+}1)BTd\) 量级 |

取 N=8：约 \(54\,BTd\)。对照每层主干：注意力投影 \(4BTd\)、打分与加权 \(2BT^2d\)、MLP 约 \(16BTd\)。当序列长 \(T\) 达到数千（如 T=4096）时，仅 \(2BT^2d\) 一项就是 attn_res 增量的数百倍；且 T 越大，attn_res 占比越低——**开销随序列变长相对消失**。

**激活显存推导**。反向传播需要保留 `V` 与 `K` 两个 \([N{+}1, B, T, D]\) 张量（logits、\(\alpha\) 很小可忽略），即每 token 约 \(2(N{+}1)d\) 个标量。取 N=8：约 \(18d\)；对照 Full AttnRes 需要保留全部前层输出，约 \(2Ld\)，L=48 时为 \(96d\)——Block 版约为 Full 版的 1/5。这正是 [README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28) 图注「reducing memory from O(Ld) to O(Nd)」的量化版本。

**块数 N 不影响参数**。注意一个容易混淆的点：`block_size`（或等价的块数 N）只进入 FLOPs 与显存的 \((N{+}1)\) 因子；参数量 \(4Ld\) 只由层数和位点数决定——proj/norm 挂在层上，不挂在块上。u3-l1 会系统地做 N 的基准测试与取舍分析。

#### 4.4.3 源码精读

**「边际开销」的原始声明**：

> [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)
> With ~8 blocks, it recovers most of Full AttnRes's gains while serving as a practical drop-in replacement with marginal overhead.

「约 8 个块恢复 Full 版大部分收益 + 边际开销」——本节的推导就是这句话的展开；N≈8 也是上面 FLOPs/显存估算的取值依据。

**显存复杂度的图注声明**：

> [README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28)
> (c) Block AttnRes: layers are grouped into blocks, reducing memory from O(Ld) to O(Nd).

**全部开销来源就这四行**（伪代码中所有新增张量操作）：

> [README.md:L61-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L64)
> ```python
> V = torch.stack(blocks + [partial_block])  # 拼装, 无乘加
> K = norm(V)                                 # 约 3(N+1)BTd MAC
> logits = torch.einsum(...)                  # (N+1)BTd MAC
> h = torch.einsum(...)                       # (N+1)BTd MAC
> ```

没有别的隐藏成本：参数在 proj/norm 里，计算在这四行里。

#### 4.4.4 代码实践：把「边际」扫成一张表

1. **实践目标**：写一个参数量计数脚本，扫描不同宽度 d，验证 attn_res 参数占比按 \(1/(3d)\) 下降；同时用 `torch` 模块实测，确认推导与实现一致。
2. **操作步骤**：

```python
# 示例代码
import torch

def attn_res_params():
    proj = torch.nn.Linear(64, 1, bias=False)
    norm = torch.nn.RMSNorm(64)          # 若环境无 RMSNorm, 可用 nn.LayerNorm 代替计数
    return sum(p.numel() for p in proj.parameters()) + \
           sum(p.numel() for p in norm.parameters())

per_site = attn_res_params()
print(f"单位点实测参数: {per_site} (理论 2d = {2*64})")

print(f"{'d':>6} {'每层增量 4d':>10} {'主干 ~12d^2':>12} {'占比 1/(3d)':>10}")
for d in [512, 1024, 2048, 4096]:
    print(f"{d:>6} {4*d:>10} {12*d*d:>12} {100/(3*d):>9.3f}%")
```

3. **需要观察的现象**：首位点实测应等于 `2d`；表格中占比一列随 d 翻倍而减半。
4. **预期结果**：`单点位参数 = 128`（d=64 时）；d=512/1024/2048/4096 的占比分别约 0.065%/0.033%/0.016%/0.008%。注意 `nn.RMSNorm` 需要较新版本的 PyTorch（含增益、无偏置，参数恰为 d）；若版本不含该模块，用 LayerNorm 计数会多出 d 个 bias，可手动减去。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：把 `block_size` 减半（块数 N 翻倍），attn_res 的参数量、FLOPs、激活显存各自怎么变？

> **答案**：参数量**不变**（\(4Ld\) 只由层数与位点数决定，proj/norm 挂在层上）；FLOPs 与激活显存中的 \((N{+}1)\) 因子约翻倍。所以「调块大小」是一个纯计算/显存旋钮，不碰参数预算。

**练习 2**：为什么每处位点的开销是 \(2d\) 而不是 \(2d^2\)？根源在哪一行？

> **答案**：根源在伪查询是自由向量而非投影矩阵——`proj` 是 `Linear(d, 1)`（参数 d 个）而不是 `Linear(d, d)`（参数 d² 个）。对应 [README.md:L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43) 的 \(\mathbf{w}_l \in \mathbb{R}^d\)（向量）与 [README.md:L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63) 直接取 `proj.weight`（形状 `[1, d]`）。

**练习 3**：Full AttnRes 和 Block AttnRes 的参数开销谁大？

> **答案**：基本相同——两者每处位点都可以只挂 \(2d\) 参数（一个伪查询 + 一个 norm），差别不在参数而在**候选数量**：Full 的候选数随深度增长到 L，FLOPs/显存是 O(Ld)；Block 的候选数是 N+1，为 O(Nd)。README L28 的对比指的是显存，不是参数。

## 5. 综合实践

### 5.1 任务：三组小型消融——norm 与 proj 各自在守什么

本讲三个模块分别论证了：norm 守**尺度不变性**（打分只看方向）、proj 守**初始化尺度**（起步近均匀）。综合实践用一组消融实验把这两件事「拆开看」：故意破坏一个组件，观察深度注意力权重分布与输出幅度如何变化。

**实验设计**（对应任务规格中的 (a)(b)(c) 三组 + 基线）：

- **基线**：README 原版——`RMSNorm` 打分 + Linear 默认初始化的伪查询；
- **(a) 去掉 RMSNorm**：`K = V`，打分直接用原始幅度；
- **(b) 换成 LayerNorm**：打分前去均值；
- **(c) 去掉 proj**：伪查询改用裸参数 `nn.Parameter(torch.randn(d))`。

**数据构造**：为了把差别「放大到肉眼可见」，候选要模拟真实痛点——块幅度随深度增长（u1-l2 的 PreNorm 现象），并给部分块注入直流偏置（放大 (b) 组的LayerNorm/RMSNorm 分歧）：

```python
# 示例代码: 三组消融的完整脚本
import torch

torch.manual_seed(42)
B, T, D, N = 2, 16, 64, 8          # 9 个候选

def make_candidates(with_bias=True):
    blocks, partial = [], torch.randn(B, T, D)
    for i in range(N):
        v = torch.randn(B, T, D) * (1.0 + i)      # 幅度随深度线性增长
        if with_bias and i % 3 == 0:
            v = v + 3.0                            # 直流偏置
        blocks.append(v)
    return blocks, partial

def block_attn_res(blocks, partial, w, norm_kind):
    V = torch.stack(blocks + [partial])                       # [N+1,B,T,D]
    if norm_kind == "rms":
        K = torch.nn.functional.rms_norm(V, (D,))
    elif norm_kind == "ln":
        K = torch.nn.functional.layer_norm(V, (D,))
    else:                                                      # "none"
        K = V
    logits = torch.einsum('d, n b t d -> n b t', w, K)
    alpha = logits.softmax(0)
    h = torch.einsum('n b t, n b t d -> b t d', alpha, V)
    return alpha, h

def norm_entropy(alpha):                        # 1 = 完全均匀, 0 = one-hot
    p = alpha.clamp_min(1e-12)
    return (-(p * p.log()).sum(0) / torch.log(torch.tensor(N + 1.0))).mean()

blocks, partial = make_candidates()
V = torch.stack(blocks + [partial])
cand_rms = V.pow(2).mean(dim=-1).sqrt()          # 各候选的 RMS

configs = {
    "baseline(rms+linear-init)": (torch.nn.Linear(D, 1, bias=False).weight.squeeze().detach(), "rms"),
    "(a) no-norm":               (torch.nn.Linear(D, 1, bias=False).weight.squeeze().detach(), "none"),
    "(b) layer-norm":            (torch.nn.Linear(D, 1, bias=False).weight.squeeze().detach(), "ln"),
    "(c) randn-pseudo-query":    (torch.nn.Parameter(torch.randn(D)).detach(), "rms"),
}

print(f"{'配置':<28}{'平均归一化熵':>12}{'平均 max α':>12}"
      f"{'argmax 落在最大幅度块的比例':>20}{'输出 RMS/最大候选 RMS':>22}")
for name, (w, kind) in configs.items():
    alpha, h = block_attn_res(blocks, partial, w, kind)
    big = cand_rms.argmax(0)                     # 每 (b,t) 幅度最大的候选
    hit = (alpha.argmax(0) == big).float().mean()
    ratio = h.pow(2).mean(dim=-1).sqrt().mean() / cand_rms.amax(dim=0).mean()
    print(f"{name:<28}{norm_entropy(alpha):>12.3f}{alpha.max(0).values.mean():>12.3f}"
          f"{hit:>20.3f}{ratio:>22.3f}")
```

### 5.2 记录表模板

| 配置 | 平均归一化熵 | 平均 max α | argmax 命中最大幅度块比例 | 输出 RMS / 最大候选 RMS | 主要现象（一句话） |
|:---|:---:|:---:|:---:|:---:|:---|
| baseline（rms + Linear 初始化） | | | | | |
| (a) 去掉 RMSNorm | | | | | |
| (b) 换 LayerNorm | | | | | |
| (c) randn 伪查询 | | | | | |

### 5.3 预期现象与判读（理论预期，具体数值待本地验证）

1. **基线**：熵接近 1（初始 logits \(\sigma \approx 0.58\)，近均匀）、max α 略高于 \(1/9\)；输出 RMS 是候选的凸平均，明显小于最大候选 RMS。这是「平滑起步」。
2. **(a) 去掉 RMSNorm**：熵显著下降，argmax 集中到**幅度极端**的候选上——方向与 \(\mathbf{w}\) 夹角为锐角的大块垄断权重、钝角的大块被强力打压（\(|\mathrm{logit}| \propto \lVert\mathbf{v}\rVert\)）。判读：norm 守的是「按内容而非按大小选择」。
3. **(b) 换 LayerNorm**：无直流偏置的候选上与基线接近；带直流偏置的候选（脚本中 `i % 3 == 0` 的块）行为分化——LayerNorm 扣除均值后这些块的方向信息恢复，而基线的 RMSNorm 键被偏置方向拉齐。判读：两种 norm 的分歧集中在直流偏置上，选型应对齐模型内其他 norm 的惯例。
4. **(c) randn 伪查询**：熵大幅跌落、max α 接近 1（\(\lVert\mathbf{w}\rVert \approx \sqrt{d}\)，初始 logits \(\sigma \approx \sqrt{d} = 8\)，softmax 近 one-hot）。判读：proj 的 Linear 容器不只是「装向量」，其默认初始化尺度是**平滑起步的隐形保障**——这也解释了 4.2 节「为什么用 Linear 而不用裸 Parameter」。
5. **输出幅度**：四组的输出 RMS 都应不超过最大候选 RMS（凸组合上界在任何配置下都成立，因为它只依赖 softmax 权重与用原始 V 聚合，与打分方式无关）——这是本讲唯一在四组中**不变**的性质，值得在表格最后一列特意确认。

### 5.4 进阶（可选）

把 (c) 组的裸参数从 `randn` 换成 `randn / d**0.25`（范数约 \(d^{1/4}\)，介于两种初始化之间），观察熵随初始化尺度的变化曲线；再对比 (a) 组在「候选幅度不随深度增长」（把 `(1.0 + i)` 改成 `1.0`）时是否仍明显劣化——这能区分「幅度支配」与「方向不匹配」两种效应各自的贡献。

## 6. 本讲小结

- **伪查询**是层私有的自由向量 \(\mathbf{w}_l \in \mathbb{R}^d\)（每位点一个）：不经输入投影，输入依赖性完全由键侧逐位置的块表示提供；logit \(= \lVert\mathbf{w}\rVert\sqrt{d}\cos\theta\)，只由方向匹配度决定。
- **proj 的真实身份是参数化容器**：`Linear(d,1)` 的 `weight`（squeeze 后 `[d]`）就是 \(\mathbf{w}_l\)；它与 `proj(K).squeeze(-1)` 数学等价，且 bias 是标量、被 softmax 平移不变性完全抵消（梯度恒零）——attn 前与 MLP 前各持一套独立 proj/norm，因为两个位点检索目的不同。
- **norm 是键值分离的执行者**：打分用 `K = RMSNorm(V)` 保证尺度不变性（候选无法靠变大争宠，杜绝 PreNorm 幅度病理渗入选择权重）；聚合用原始 V 保证输出落在候选凸包内、幅度与深度无关——与 PreNorm「使用处归一化、通路保原值」同构。
- **初始化尺度是隐形设计**：Linear 默认初始化使初始 logits \(\sigma \approx 0.58\)、权重近均匀；裸 `randn(d)` 会造成初始近 one-hot 的「随机路由」。
- **开销确实是边际的**：每处 \(2d\)、每层 \(4d\) 参数，占比 \(1/(3d)\)（d=4096 时 0.008%）；FLOPs 约 \(6(N{+}1)BTd\)，激活显存每 token 约 \(2(N{+}1)d\)——O(Ld)→O(Nd)；块数 N 是纯计算/显存旋钮，不影响参数量。
- 三组消融把以上结论拆开验证：去 norm 丢尺度不变性、换 LayerNorm 只在直流偏置处分歧、去 proj 丢平滑起步；而凸组合的幅度上界在所有配置下都成立。

## 7. 下一步学习建议

- **下一讲 u2-l4（搭建最小实验台）**：本讲的组件分析仍是「静态」的——权重分布只看了初始化。下一讲把 `block_attn_res` 与 u2-l2 的调度组装成完整可训练的 Transformer，在真实数据上对比 Standard 与 AttnRes 的训练损失，届时本讲的「初始近均匀、训练习得选择性」才能被动态验证。
- **向后衔接 u3-l1（复杂度分析）**：本讲 4.4 的估算是量级推导，u3-l1 会用基准脚本实测多组 L/N 配置下的峰值显存与耗时，把 O(Ld)→O(Nd) 变成实测曲线。
- **回读论文**：`Attention_Residuals.pdf` 中应能找到 norm 的内部实现细节（epsilon、增益）、norm 类型与 proj 初始化是否有过消融——这些在仓库 README 中均未给出（待确认），精读时建议对照本讲 4.2/4.3 的推断逐条核对。
- **动手巩固**：把综合实践的脚本扩展成可复用的小工具（输入候选列表 + 配置，输出权重分布画像），下一讲分析训练动态时可以直接复用它观察「选择性随训练的演化」。
