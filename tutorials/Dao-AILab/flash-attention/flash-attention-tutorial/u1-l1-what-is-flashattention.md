# FlashAttention 是什么：从朴素注意力到 IO 感知

> 本讲是《FlashAttention 源码学习手册》的第一篇，面向从零开始的读者。
> 本仓库当前主力是 FlashAttention-4（FA4，位于 `flash_attn/cute/`，用 Python + CuTeDSL 编写），
> 但本篇不读 kernel 代码——我们先建立最核心的直觉：**为什么需要 FlashAttention，它到底优化了什么**。
> 有了这个直觉，后续每一篇讲义你才知道「它在加速哪一段」。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出**朴素（标准）注意力**在长序列下为什么会「显存爆炸」和「变慢」，并能用复杂度记号描述它。
2. 说清 FlashAttention 的三个核心思想：**分块（tiling）**、**在线 softmax（online softmax）**、**不实例化 N×N 注意力矩阵**。
3. 复述 FlashAttention-1 → 2 → 3 → 4 的演进脉络，知道每一代主要改进了什么。
4. 动手用纯 PyTorch 实现一次朴素注意力，**实测**它的 O(N²) 显存行为。

本讲只引用 `README.md` 与 `usage.md` 两个项目文件。真正的 kernel 源码阅读从后续讲义开始。

---

## 2. 前置知识

本讲假设你已经知道以下名词的「大致含义」，下面用一句话帮你确认：

- **注意力（Attention）**：Transformer 里的一种运算，让序列中每个位置去「关注」其它位置，加权汇总信息。
- **矩阵乘法（matmul）**：`A @ B`，两个二维数组相乘。本讲里你会看到 `Q @ K^T`、`P @ V` 这类写法。
- **softmax**：把一组实数变成一组「和为 1」的概率。常用在注意力里产生加权系数。
- **GPU 显存（HBM）与片上缓存（SRAM）**：HBM 容量大（几十 GB）但慢；SRAM（共享内存 / 寄存器）极快但极小（几十~几百 KB）。这个「快慢差距」是 FlashAttention 一切优化的出发点。
- **序列长度（sequence length, 记作 N）**：一句话/一个样本里的 token 数。大模型时代 N 从几百涨到几万、几十万。

如果上面某项你完全没听过，建议先看一遍 `README.md` 顶部对论文和用途的介绍再回来。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
| --- | --- | --- |
| [README.md](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md) | 项目主文档，介绍 FA1/2/3/4、安装、用法、性能与显存对比 | 注意力公式、显存对比图、各代说明、Changelog |
| [usage.md](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/usage.md) | FlashAttention 被业界采用的情况清单 | 说明它的工程影响力与「事实标准」地位 |

> 提示：这两个文件都是纯文档，不含可执行 kernel，非常适合作为入门阅读。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 朴素注意力的复杂度与显存问题**——讲清楚「病」是什么。
- **4.2 Tiling 与 online softmax 思想**——讲清楚 FlashAttention 开的「药」是什么。
- **4.3 FA1/2/3/4 演进脉络**——讲清楚这味药是怎么一代代改良的。

### 4.1 朴素注意力的复杂度与显存问题

#### 4.1.1 概念说明

Transformer 用的标准注意力，数学上就是大家熟悉的 **Scaled Dot-Product Attention（SDPA）**：

\[ \text{Out} = \text{softmax}\!\left(\frac{Q K^{\mathsf{T}}}{\sqrt{d}}\right) V \]

其中：

- \( Q \)（Query）、\( K \)（Key）、\( V \)（Value）是三张形状相同的张量，沿「序列长度」方向都是 \( N \)，沿「头维度」方向是 \( d \)。
- \( Q K^{\mathsf{T}} \) 得到一个 \( N \times N \) 的矩阵，称为**注意力分数矩阵（attention scores）**。它第 \( i \) 行第 \( j \) 列表示「第 \( i \) 个 query 对第 \( j \) 个 key 的关注程度」。
- 对每一行做 softmax，得到 \( N \times N \) 的**注意力权重矩阵** \( P \)。
- 再用 \( P \) 对 \( V \) 加权求和，得到输出 \( \text{Out} \)。

`README.md` 里就是这么定义它的核心函数的：

> The main functions implement scaled dot product attention (softmax(Q @ K^T * softmax_scale) @ V)
> —— 见 [README.md:L221-L222](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L221-L222)

朴素实现的「病」在于中间那一步：**\( Q K^{\mathsf{T}} \) 必须把整个 \( N \times N \) 矩阵算出来、写进显存**。当 \( N \) 很大时，这个矩阵会变得巨大。

#### 4.1.2 核心流程

把朴素 SDPA 写成显式的四步伪代码：

```text
# 输入: Q, K, V  形状 (N, d)，以及缩放 scale = 1/sqrt(d)
S = Q @ K.T * scale        # (N, N)  ← 注意力分数矩阵
P = softmax(S, dim=-1)     # (N, N)  ← 注意力权重矩阵
Out = P @ V                # (N, d)  ← 输出
return Out
```

关键观察：**\( S \) 和 \( P \) 都是 \( N \times N \)**。我们来算复杂度：

| 量 | 计算量（FLOPs） | 显存占用（HBM） |
| --- | --- | --- |
| \( Q, K, V \) 读写 | \( O(N d) \) | \( O(N d) \) |
| \( S = Q K^{\mathsf{T}} \) | \( O(N^2 d) \) | \( O(N^2) \) ← **瓶颈** |
| \( P = \text{softmax}(S) \) | \( O(N^2) \) | \( O(N^2) \) ← **瓶颈** |
| \( \text{Out} = P V \) | \( O(N^2 d) \) | \( O(N d) \) |

两个结论：

1. **计算量是 \( O(N^2 d) \)**：序列每翻倍，算力需求约变 4 倍。这是注意力本质的二次代价，FlashAttention 也**绕不开**。
2. **额外显存是 \( O(N^2) \)**：那个 \( N \times N \) 的中间矩阵。序列翻倍，它变 4 倍。

举个例子：\( N = 4096 \)，fp16 下单个 \( 4096 \times 4096 \) 矩阵约 \( 4096^2 \times 2 \text{B} \approx 33.5 \text{MB} \)。看起来不大？但这只是**一个 head、一个 batch**。真实模型有几十上百个 head、若干 batch，再叠加反向传播需要保留的中间量，\( N^2 \) 项迅速吞满显存。

更要命的是**读写带宽**：\( S \) 和 \( P \) 要在 HBM 里来回读写，而 GPU 的算力（TFLOPs）增长远快于显存带宽（TB/s）。于是注意力逐渐变成一个**被显存带宽卡住**的算子——算单元在空转，等着数据搬来搬去。

> 一句话总结：朴素注意力的问题不是「算错」，而是**把一个本可以不落地的大矩阵反复落地到慢速显存**。

#### 4.1.3 源码精读

`README.md` 的「Performance / Memory」一节用一张图和几句话点明了这个病根：

> Memory savings are proportional to sequence length -- since standard attention has memory quadratic in sequence length, whereas FlashAttention has memory linear in sequence length. We see 10X memory savings at sequence length 2K, and 20X at 4K.
> —— 见 [README.md:L513-L515](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L513-L515)

这段话直接给出了「标准注意力显存 ∝ \( N^2 \)，FlashAttention 显存 ∝ \( N \)」的对比，并给出实测数字：序列长 2K 时省 10 倍、4K 时省 20 倍。本讲后面的实践任务，就是让你**亲手复现**这个「\( N^2 \)」的现象。

另外，`README.md` 在测试说明里强调 FlashAttention 与参考实现「数值上等价」：

> We test that FlashAttention produces the same output and gradient as a reference implementation, up to some numerical tolerance.
> —— 见 [README.md:L550-L556](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L550-L556)

请记住这个关键事实：**FlashAttention 是精确的（exact），不是近似**。它省的是显存和带宽，不是精度。后面 4.2 会解释它凭什么能做到「又省又精确」。

#### 4.1.4 代码实践

**实践目标**：亲手实现朴素 SDPA，观察它的峰值显存随序列长度增长的方式，验证「近似 \( O(N^2) \)」。

**操作步骤**：保存下面这段**示例代码**为 `naive_attn_mem.py` 并运行（需要一块 NVIDIA GPU 与 PyTorch；若无 GPU，见末尾的 CPU 备选说明）。

```python
# 示例代码（非项目自带代码，供学习用）
import torch

def naive_sdpa(q, k, v, scale=None):
    """朴素 SDPA：会实例化完整的 N x N 注意力矩阵。"""
    d = q.shape[-1]
    scale = 1.0 / (d ** 0.5) if scale is None else scale
    scores = (q @ k.transpose(-2, -1)) * scale   # (batch, nheads, N, N)  ← 瓶颈
    attn = scores.softmax(dim=-1)                # (batch, nheads, N, N)
    out = attn @ v                               # (batch, nheads, N, d)
    return out

def measure_peak_mem(seqlen, headdim=64, nheads=8, batch=1):
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    q = torch.randn(batch, nheads, seqlen, headdim, dtype=torch.float16, device="cuda")
    k = torch.randn_like(q)
    v = torch.randn_like(q)
    out = naive_sdpa(q, k, v)
    torch.cuda.synchronize()
    return torch.cuda.max_memory_allocated() / (1024 ** 2)  # 单位 MB

for n in [512, 1024, 2048, 4096, 8192]:
    try:
        peak = measure_peak_mem(n)
        print(f"seqlen={n:5d}  peak_mem={peak:8.2f} MB")
    except RuntimeError as e:
        print(f"seqlen={n:5d}  OOM: {e}")
        break
```

**需要观察的现象**：当 `seqlen` 从 512 → 1024 → 2048 → 4096 → 8192（每次翻倍）时，峰值显存应**大约以 4 倍**的步长增长（因为主导项是 \( N^2 \)）。

**预期结果**：你会看到一张近似二次增长的表；其中 `seqlen=4096` 的峰值显存（取决于 head 数与是否 OOM）明显高于线性增长所预测的值。把它记下来。

> CPU 备选（无 GPU 时）：把 `device="cuda"` 改成 `device="cpu"`，用 `tracemalloc` 或 `psutil.Process().memory_info().rss` 代替 `max_memory_allocated`，趋势应当一致，但绝对值与单位不同。结论不变：**显存随 \( N^2 \) 增长**。
>
> 说明：本讲未在此环境实际运行上述脚本，具体峰值数字**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：若把 `headdim` 从 64 改成 128，朴素注意力的**额外**显存（即 \( N \times N \) 矩阵那部分）会如何变化？

**参考答案**：**不变**。\( N \times N \) 矩阵只依赖序列长度 \( N \)，与头维度 \( d \) 无关。\( d \) 只影响 \( Q/K/V \) 本身的大小（\( O(Nd) \)）和 FLOPs（\( O(N^2 d) \)），不影响那个二次项。

**练习 2**：为什么说「注意力是被显存带宽卡住的算子」？请用本节的复杂度表解释。

**参考答案**：计算量 \( O(N^2 d) \) 与访存量 \( O(N^2) \) 的比值是 \( d \)（头维度，约几十~上百）。GPU 的算力/带宽比（arith-to-byte ratio）通常远高于这个值，意味着数据搬运跟不上计算单元的胃口，于是计算单元空转，瓶颈落在 HBM 带宽上。

---

### 4.2 Tiling 与 online softmax 思想

#### 4.2.1 概念说明

FlashAttention 的「药」只有三味：

1. **分块（Tiling）**：不一次性算完整个 \( N \times N \)。把 \( Q \) 切成一行行的 tile（Q 块），把 \( K, V \) 切成一列列的 tile（KV 块）。**Q 块加载一次常驻在快速 SRAM**，然后流式地遍历所有 KV 块，每来一块 KV 就增量更新这一行 Q 的输出。
2. **在线 softmax（Online softmax）**：标准 softmax 需要一次性看到整行才能算分母。但 online softmax 允许我们**一边遍历 KV 块，一边维护「当前最大值」和「当前分母」**，每加入一块就修正之前的结果。这样就不需要先攒出整行 \( N \) 个分数。
3. **不实例化 \( N \times N \) 矩阵**：因为有了 1 和 2，那个 \( N \times N \) 的分数矩阵永远只以「一小块」的形式出现在 SRAM 里，算完即丢，**从不写回 HBM**。

三者合起来，效果是：**计算量仍是 \( O(N^2 d) \)（没法减少），但 HBM 读写从 \( O(N^2) \) 大幅下降，额外显存从 \( O(N^2) \) 降到 \( O(N) \)**。这就是 `README.md` 所说的「linear in sequence length」。

#### 4.2.2 核心流程

先讲清楚 **online softmax 的数学**，这是整个 FlashAttention 的灵魂。

对一行分数 \( x \in \mathbb{R}^{N} \)，数值稳定的 softmax 是：

\[ m = \max_{j} x_j, \qquad \ell = \sum_{j} e^{x_j - m}, \qquad y_i = \frac{e^{x_i - m}}{\ell} \]

朴素做法必须**先扫一遍整行**才能得到 \( m \) 和 \( \ell \)。Online softmax 的诀窍是：把行切成若干块，**逐块更新** \( m \) 和 \( \ell \)。设当前已累加得到 \( m^{\text{old}}, \ell^{\text{old}} \)，新来一块的局部最大值为 \( m^{B} = \max(\text{本块}) \)，则：

\[ m^{\text{new}} = \max\!\left(m^{\text{old}},\, m^{B}\right) \]

\[ \ell^{\text{new}} = \ell^{\text{old}} \cdot e^{m^{\text{old}} - m^{\text{new}}} \;+\; \sum_{j \in \text{本块}} e^{x_j - m^{\text{new}}} \]

关键在 \( e^{m^{\text{old}} - m^{\text{new}}} \) 这个**重缩放因子（correction factor）**：当新的全局最大值 \( m^{\text{new}} \) 比旧的大时，它把旧的累加分母「打折」回新的基准上，从而保持数值精确。这正是 4.1 里说的「又省又精确」的来源——没有任何近似。

把这套思路套到注意力上（固定一个 Q 块，遍历 KV 块），每个 KV 块更新三样东西：

```text
# 固定 Q 块, 维护: m (行最大), ℓ (行和), O (输出累加器), 初始 m=-inf, ℓ=0, O=0
for each KV block:
    S = Q_block @ K_block.T * scale          # 本块分数 (只活在 SRAM 里)
    m_block = rowmax(S)
    m_new = max(m, m_block)
    P = exp(S - m_new)                        # 本块权重 (本块局部, 未归一)
    # 重缩放旧的 O 和 ℓ 到新基准:
    O = O * exp(m - m_new) + P @ V_block
    ℓ = ℓ * exp(m - m_new) + rowsum(P)
    m = m_new
O = O / ℓ                                     # 最后归一
```

对照朴素版本，差别一目了然：**那个 \( N \times N \) 的 \( S/P \) 从头到尾没有完整存在过**，每次只有「一个 Q 块 × 一个 KV 块」那么大，活在 SRAM 里算完即弃。

> 关于 IO 复杂度：在 SRAM 大小为 \( M \) 的前提下，FlashAttention 对 HBM 的访问量约为 \( O\!\left(\frac{N^2 d^2}{M}\right) \)，远小于朴素的 \( O(N^2) \) 落地读写（细节会在后续「前向 Kernel」讲义展开，这里只需记住定性结论）。

#### 4.2.3 源码精读

`README.md` 的「Performance / Memory」用一张图（`assets/flashattn_memory.jpg`）和一句话给出结论：

> since standard attention has memory quadratic in sequence length, whereas FlashAttention has memory linear in sequence length. We see 10X memory savings at sequence length 2K, and 20X at 4K.
> —— 见 [README.md:L514-L515](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L514-L515)

这正是 4.2.1 三个思想的最终效果：显存从 \( O(N^2) \) 降到 \( O(N) \)，序列越长省得越多。

FlashAttention 这个名字本身就来自 FA1 论文标题里的 **IO-Awareness**：

> FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness
> —— 见 [README.md:L6-L9](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L6-L9)

「IO-Aware」即「意识到显存层级的存在」——它知道 HBM 慢、SRAM 快，于是刻意把数据组织成「小块常驻 SRAM、流式过 HBM」的形式。tiling 与 online softmax 都是为这个目标服务的。

`usage.md` 则告诉你这套思路已经成为业界事实标准，被 PyTorch、HuggingFace、DeepSpeed、Megatron-LM 等纷纷集成：

> Integrated into machine learning frameworks — Pytorch: integrated into core Pytorch in nn.Transformer. Huggingface's transformers library ...
> —— 见 [usage.md:L9-L13](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/usage.md#L9-L13)

#### 4.2.4 代码实践

**实践目标**：用纯 PyTorch 实现一个「分块 online softmax」版本的注意力，验证它与朴素版本**数值等价**，从而理解 online softmax 的重缩放是怎么工作的。

**操作步骤**：下面是**示例代码**。它把 K、V 沿序列方向切成块，逐块用 4.2.2 的递推更新 \( m, \ell, O \)。

```python
# 示例代码（非项目自带代码，供学习用）
import torch

def block_online_attention(q, k, v, block_n=64, scale=None):
    """分块 + online softmax 的注意力, 数值上等价于朴素 SDPA。"""
    N, d = q.shape[-2], q.shape[-1]
    scale = 1.0 / (d ** 0.5) if scale is None else scale
    # 用更宽的 fp32 累加, 以便看清数值等价
    O = torch.zeros_like(q)
    m = torch.full(q.shape[:-1] + (1,), float("-inf"), dtype=torch.float32, device=q.device)
    ell = torch.zeros(q.shape[:-1] + (1,), dtype=torch.float32, device=q.device)
    O_acc = torch.zeros(q.shape, dtype=torch.float32, device=q.device)

    for j0 in range(0, N, block_n):
        kb = k[..., j0:j0 + block_n, :]          # KV 块
        vb = v[..., j0:j0 + block_n, :]
        S = (q @ kb.transpose(-2, -1) * scale).float()   # 本块分数 (仅存于内存一小会)
        m_block = S.amax(dim=-1, keepdim=True)
        m_new = torch.maximum(m, m_block)
        P = torch.exp(S - m_new)                 # 本块权重, 未归一
        c = torch.exp(m - m_new)                 # 旧累加器的重缩放因子
        O_acc = O_acc * c + P @ vb.float()
        ell = ell * c + P.sum(dim=-1, keepdim=True)
        m = m_new

    return (O_acc / ell).to(q.dtype)

# 对拍: 与朴素 SDPA 对比
N, d = 1024, 64
q = torch.randn(1, 8, N, d, dtype=torch.float16, device="cuda")
k = torch.randn_like(q); v = torch.randn_like(q)

ref = (q.float() @ k.float().transpose(-2, -1) * (d ** -0.5)).softmax(-1) @ v.float()
out = block_online_attention(q, k, v, block_n=128)
print("max abs diff =", (ref - out.float()).abs().max().item())
```

**需要观察的现象**：`max abs diff` 应当是一个很小的数（与 fp16 的舍入误差同量级，例如 1e-2 ~ 1e-3 级别），说明分块 online 版本与完整 softmax **数值等价**。

**预期结果**：误差在 fp16 容忍范围内，印证 4.1.3 引用的「same output ... up to some numerical tolerance」。注意这个示例**没有真正省显存**（PyTorch 仍会把每块的张量分配在 HBM），它只演示**算法的等价性**；真正的省显存要靠后续讲义里的 CUDA/CuTeDSL kernel 把数据真正留在 SRAM。**待本地验证**具体误差数值。

#### 4.2.5 小练习与答案

**练习 1**：在 online softmax 的递推里，如果某一步 \( m^{\text{new}} = m^{B} \)（即新块的最大值就是新的全局最大值），重缩放因子 \( e^{m^{\text{old}} - m^{\text{new}}} \) 是大于、等于还是小于 1？

**参考答案**：**小于 1**（除非 \( m^{\text{old}} = m^{\text{new}} \) 时等于 1）。因为 \( m^{\text{old}} < m^{\text{new}} \)，指数为负，因子 \( <1 \)，即把旧累加器「打折」，这与「换到更大的基准」一致。

**练习 2**：为什么 online softmax 能让 FlashAttention 保持「精确」而非「近似」？

**参考答案**：因为每一步的重缩放因子 \( e^{m^{\text{old}}-m^{\text{new}}} \) 在数学上**完全补偿**了最大值的变化，分块累加得到的 \( m, \ell, O \) 与一次性计算的值在精确算术下完全相等。误差只来自浮点舍入，不来自算法本身。

**练习 3**：4.2.4 的示例代码「没有真正省显存」，为什么？真正的 kernel 是靠什么省的？

**参考答案**：PyTorch 的张量默认分配在 HBM，示例里 `S`、`P` 等中间张量仍会落地 HBM。真正的 FlashAttention kernel 把 Q 块、KV 块、\( S/P \) 都放进 GPU 的 SRAM（共享内存/寄存器），算完即弃，从不写回 HBM，从而既省显存又省带宽。

---

### 4.3 FA1/2/3/4 演进脉络

#### 4.3.1 概念说明

FlashAttention 不是一个静态算法，而是一条**持续演进的产品线**。本仓库同时收纳了多代实现。理解演进脉络，能帮你在阅读源码时不至于「张冠李戴」。

| 代次 | 核心改进 | 目标硬件 | 在本仓库的位置 |
| --- | --- | --- | --- |
| **FA1** (2022) | 提出 tiling + online softmax + IO-aware，奠定全部基础 | A100 (Ampere, SM80) | 论文，思想贯穿全仓 |
| **FA2** (2023) | 更好的并行度与工作划分（work partitioning），约 2× 提速 | Ampere / Ada / Hopper | 顶层 `flash_attn/` + `csrc/` (C++/CUDA) |
| **FA3** (2024) | 用上 Hopper 的 WGMMA/TMA/异步拷贝，FP16/BF16 前后向 + FP8 前向 | H100 (Hopper, SM90) | `hopper/` |
| **FA4** (当前主力) | 用 **Python + CuTeDSL** 重写，运行时 JIT 编译为 PTX/CUBIN；新增 Blackwell 的 UMMA/2CTA 等 | Hopper **与** Blackwell (SM90/SM100/SM110) | `flash_attn/cute/` |

> 注意：本讲只需建立「代际地图」。各目录的细致结构与多代共存机制，是下一篇讲义 **u1-l2《仓库结构与多代代码共存》** 的主题。

#### 4.3.2 核心流程

把演进画成一条时间线（以本仓库 `README.md` 的章节为依据）：

```text
FA1 (2022, NeurIPS)
  └─ 论文: Fast and Memory-Efficient Exact Attention with IO-Awareness
     核心: tiling + online softmax, 显存 O(N^2)->O(N)
        │
        ▼
FA2 (2023, ICLR)
  └─ 论文: Faster Attention with Better Parallelism and Work Partitioning
     核心: 重排线程工作, 减少非矩阵乘法, ~2x 提升
     Changelog 2.0~2.7 累积特性: causal 对齐 / 推理优化(SplitKV雏形) /
              滑动窗口 / ALiBi / Paged KV / Softcap / torch.compile
        │
        ▼
FA3 (2024, beta, 在 hopper/)
  └─ 面向 Hopper: WGMMA warp-group MMA + TMA 异步批量拷贝 + 异步 softmax
     FP16/BF16 前后向, FP8 前向
        │
        ▼
FA4 (当前主力, 在 flash_attn/cute/)
  └─ 用 CuTeDSL (Python) 重写, JIT -> PTX/CUBIN
     面向 Hopper + Blackwell: 引入 UMMA(tcgen05) / 片上 tmem 累加 /
              persistent kernel / 2CTA / MLA 等
```

要点：

- **算法骨架没变**：从 FA1 到 FA4，tiling + online softmax 的核心始终一样，变的是「如何更高效地把它映射到新一代硬件」。
- **特性在累积**：FA2 Changelog 里列出的滑窗、ALiBi、Paged KV、Softcap 等，在 FA4 里都作为参数（`window_size`、`score_mod`、`block_sparse_tensors`、paged KV 等）保留并重写。
- **编写语言在变**：FA2 是 C++/CUDA，FA3 也是，而 **FA4 改用 Python（CuTeDSL）**，可读性大幅提升——这正是本手册把 FA4 作为主线来精读的原因。

#### 4.3.3 源码精读

**FA1 与 FA2 的论文定位**——见 [README.md:L6-L15](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L6-L15)：

> FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness ... FlashAttention-2: Faster Attention with Better Parallelism and Work Partitioning

**FA3（Hopper 专用，beta）**——见 [README.md:L30-L46](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L30-L46)：它强调「optimized for Hopper GPUs (e.g. H100)」，目前发布 FP16/BF16 前后向与 FP8 前向，要求 H100/H800 + CUDA ≥ 12.3。

**FA4（CuTeDSL，当前主力）**——见 [README.md:L80-L99](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L80-L99)：

> FlashAttention-4 is written in CuTeDSL and optimized for Hopper and Blackwell GPUs (e.g. H100, B200).
> —— 调用方式：`from flash_attn.cute import flash_attn_func`

**FA2 的特性累积（Changelog）**——见 [README.md:L402-L486](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L402-L486)，其中几条值得留意，因为它们都演化成了 FA4 的功能：

- 2.2「Optimize for inference」：把 KV 切到多个 thread block 再用单独 kernel 合并——这正是 FA4 **SplitKV + Combine kernel** 的雏形。
- 2.3「Local (sliding window) attention」：演化成 FA4 的 `window_size` / 滑窗掩码。
- 2.5「Paged KV cache」：演化成 FA4 的 `PagedKVManager`。
- 2.6「Softcapping」：演化成 FA4 的 `softcap` / `score_mod`。

把这些「历史功能」和后面要读的 FA4 源码对应起来，你会发现 FA4 不是凭空出现的，而是把过去散落的特性用一套统一的 CuTeDSL 抽象重新实现了一遍。

#### 4.3.4 代码实践

**实践目标**：不写代码，做一次**只读探查**，把「代次」与「目录」对应起来，建立仓库的脑内地图。

**操作步骤**（只读）：

1. 在仓库根目录查看顶层结构，确认 FA2 的 C++/CUDA 入口在 `csrc/`、Python 接口在 `flash_attn/`。
2. 确认 FA3 在 `hopper/` 目录。
3. 确认 FA4 在 `flash_attn/cute/` 目录（本手册主线）。
4. 打开 `README.md` 的 FA4 章节 [README.md:L80-L99](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L80-L99)，确认安装与最小调用方式。

**需要观察的现象**：三代实现物理上互不重叠地躺在不同目录，可以同时存在。

**预期结果**：你应当能用一句话指认「FA4 的代码在 `flash_attn/cute/`」。**具体的目录树绘制与多代共存机制，留到 u1-l2 完成**——本练习只要求建立最粗的定位。

#### 4.3.5 小练习与答案

**练习 1**：FA1、FA2、FA3、FA4 中，哪一代的**核心算法思想**（tiling + online softmax）发生了根本改变？

**参考答案**：**都没有**。从 FA1 到 FA4，核心算法思想始终是 tiling + online softmax。各代的改进主要在「并行度 / 工作划分 / 新硬件指令（WGMMA、TMA、UMMA）」等工程层面，而不是算法原理层面。

**练习 2**：为什么本手册选择把 FA4（`flash_attn/cute/`）作为精读主线，而不是更成熟的 FA2？

**参考答案**：两个原因。其一，FA4 是当前开发重点，特性最全（SplitKV、paged KV、MLA、2CTA 等）。其二，FA4 用 **Python（CuTeDSL）** 编写，源码可读性远高于 C++/CUDA 的 FA2/FA3，更适合用于学习。

**练习 3**：FA2 的 Changelog 提到「2.2 Optimize for inference ... split the loading across different thread blocks, with a separate kernel to combine results」。这个机制在 FA4 里对应什么？

**参考答案**：对应 FA4 的 **SplitKV**（把 KV 切分到多个 thread block 并行）与 **Combine kernel**（`flash_fwd_combine.py`，用 log-sum-exp 合并各 split 的部分结果）。它的数学基础正是 4.2 的 online softmax——多个 split 各自维护 \( m, \ell, O \)，最后用同样的重缩放原理合并。

---

## 5. 综合实践

把本讲的两个模块（4.1 朴素注意力的显存病、4.2 online softmax 的解药）串起来，完成下面这个综合任务。

**任务**：实现两个版本的注意力，对比它们的**峰值显存**与**数值一致性**，亲眼看到「online softmax 思想」带来的差别。

**步骤**：

1. 实现朴素 SDPA（4.1.4 已给），测量 `seqlen = 1024, 2048, 4096` 时的峰值显存。
2. 实现 4.2.4 的分块 online softmax 版本，对相同输入测量峰值显存，并与朴素版本对拍，记录 `max abs diff`。
3. 在一张表里写下：每个 `seqlen` 下两个版本的峰值显存，以及朴素版本显存随 `seqlen` 翻倍的增长倍数。

**预期结论**（请用自己的实测数据填空，**待本地验证**）：

| seqlen | 朴素峰值显存 | online 版峰值显存 | 朴素相对上一档的增长倍数 |
| --- | --- | --- | --- |
| 1024 | __ MB | __ MB | — |
| 2048 | __ MB | __ MB | ≈ ?× |
| 4096 | __ MB | __ MB | ≈ ?× |

**思考题（写在报告里）**：

- 朴素版本的显存增长倍数是否接近 4？这印证了什么复杂度？
- 为什么 4.2.4 的 online 版本在 PyTorch 里**显存下降不明显**？真正能让显存降到 \( O(N) \) 的关键是什么？（提示：把数据真正留在 SRAM，由后续 kernel 讲义解答。）

> 这一综合实践帮你建立本讲最核心的两个直觉：① 朴素注意力有 \( O(N^2) \) 显存病；② online softmax 是「精确 + 省显存」的算法基础，但要真正兑现收益需要硬件级的 kernel 实现——那正是后续讲义的主题。

---

## 6. 本讲小结

- 注意力的本质运算是 \(\text{softmax}(QK^{\mathsf{T}}/\sqrt{d})V\)；朴素实现会实例化一个 \( N \times N \) 的中间矩阵，带来 \( O(N^2) \) 的**额外显存**和沉重的 HBM 读写。
- 这个二次代价让长序列下注意力变成**被显存带宽卡住**的算子，即使算力充足也跑不快。
- FlashAttention 用三味药解决：**分块（tiling）**、**在线 softmax（online softmax）**、**不实例化 \( N \times N \) 矩阵**——计算量不变，但显存从 \( O(N^2) \) 降到 \( O(N) \)，且**结果精确**。
- online softmax 的关键是一个重缩放因子 \( e^{m^{\text{old}}-m^{\text{new}}} \)，它在数学上完全补偿最大值的变化，因此分块累加与一次性计算等价。
- FA1→2→3→4 的**算法骨架不变**，改进集中在并行度、工作划分与新一代硬件指令；FA4 用 Python（CuTeDSL）重写，是当前开发重点与本手册的精读主线。
- `README.md` 给出实测：序列 2K 省约 10×、4K 省约 20× 显存；这套方法已被 PyTorch、HuggingFace、Megatron 等广泛集成，成为业界事实标准。

---

## 7. 下一步学习建议

- **下一篇 u1-l2《仓库结构与多代代码共存》**：把本讲 4.3 的「代次地图」细化成真实的目录树，弄清 FA2 / FA3 / FA4 各自的代码位置与构建入口。
- **随后 u1-l3《安装并第一次调用 FA4》**：动手安装 `flash-attn-4`，跑通 `from flash_attn.cute import flash_attn_func` 的最小调用。
- **进阶方向**：等你具备 kernel 阅读基础后，可以从 `softmax.py`（在线 softmax 的源码实现）开始，验证本讲 4.2 的递推公式是如何在真实代码里落地的——那对应讲义 u4-l1《在线 Softmax 数值核心》。
- **延伸阅读**：FA1 论文（IO-awareness，[README.md:L6-L9](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L6-L9)）与 FA2 论文（[README.md:L12-L15](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L12-L15)）是理解后续所有源码的理论基础，强烈推荐配合阅读。
