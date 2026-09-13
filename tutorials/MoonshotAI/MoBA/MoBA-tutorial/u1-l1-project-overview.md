# MoBA 是什么：项目定位与长上下文注意力背景

## 1. 本讲目标

本讲是整本学习手册的第一讲，不进入任何复杂源码，只解决三个问题：

1. 理解**长上下文大模型（LLM）中全量注意力的平方复杂度瓶颈**：为什么序列一长，注意力计算就贵到难以承受。
2. 掌握 **MoBA（Mixture of Block Attention）的核心思想**：块稀疏注意力、无参数 top-k gating、以及"全量/稀疏无缝切换"的设计目标。
3. 看清**本仓库的定位与依赖**：这是一个只做一件事的小型项目——提供 MoBA 的 naive 教学实现与基于 flash-attn 的高效实现，并通过 transformers 的注意力注册机制对外暴露。

学完本讲，你应该能用自己的话说清"MoBA 是什么、为什么需要它、这个仓库里有什么"，并准备好进入下一讲的安装与运行实践。

## 2. 前置知识

本讲假设读者具备以下基础概念。如果你已经熟悉，可以直接跳到第 3 节。

- **Token 与上下文（context）**：LLM 处理文本时会把文字切分成 token。一段输入包含的全部 token 数称为上下文长度，记作序列长度 \( n \)。"长上下文"通常指几万到上百万 token 的输入（例如整本书、整个代码仓库）。
- **注意力机制（attention）的直觉**：Transformer 的核心操作。每个位置的 query 向量（Q）会与序列中所有位置的 key 向量（K）计算相似度，再按相似度加权汇聚 value 向量（V）。直觉上，"每个 token 看一遍所有前文，挑相关信息"。
- **多头注意力（Multi-Head Attention）**：把注意力并行做在多个 head 上，每个 head 有独立的 Q/K/V 投影。本讲用单 head 推导，结论对每个 head 独立成立。
- **大 O 记号**：描述计算量随规模增长的方式。\( O(n^2) \) 表示规模翻倍时计算量变为 4 倍。
- **MoE（Mixture of Experts，混合专家）**：一种"路由"思想——不是让每个输入走全部专家，而是由一个 router 为每个输入挑选最相关的少数专家。MoBA 名字里的 "Mixture of Block" 就是把这套思想搬到了注意力上：**把 KV 块当作"专家"来路由**。
- **flash-attn**：一个高效注意力 CUDA 库。它通过分块计算避免了 \( n \times n \) 注意力矩阵的显式物化，大幅省显存，但**不减少**注意力的乘加计算量。本仓库的高效实现正是构建在它之上。

## 3. 本讲源码地图

整个仓库非常小，全部有效代码只有一个 Python 包、一个示例和一个测试目录：

| 文件 | 作用 | 本讲使用方式 |
| --- | --- | --- |
| `README.md` | 项目门面：问题背景、核心特性、安装方式、性能声明 | **本讲主战场**，逐段精读 |
| `requirements.txt` | 依赖清单（5 个依赖） | 精读，理解环境约束 |
| `pyproject.toml` | 打包配置，声明依赖来源与包含的包 | 精读 |
| `moba/config.py` | `MoBAConfig` 数据类，只有 `moba_chunk_size` 和 `moba_topk` 两个超参数 | 概念印证 |
| `moba/__init__.py` | `register_moba`：把两种实现注册为 transformers 注意力后端 | 概念印证 |
| `moba/moba_naive.py` | naive 参考实现（基于 attention mask） | 仅在 4.2 节引用 gate 计算的几行作概念印证，逐行精读放在第二单元 |
| `moba/moba_efficient.py` | 基于 flash-attn 的高效实现 | 本讲不进入，第三单元精读 |
| `moba/wrapper.py` | 适配 transformers 注意力接口的包装层 | 本讲不进入，第四单元精读 |
| `examples/llama.py` | 命令行示例：加载 Llama 模型并生成 | 在 4.3 节看入口鸟瞰 |
| `tests/test_moba_attn.py`、`tests/test_moba_speedup.py` | 正确性测试与性能测试 | 仅了解存在性，第四单元精读 |

另外 `figures/` 目录下有论文配图（`running_example.png`、`moba_with_flash_attn.png`、`computation_time.png`、`needle-in-a-haystack.png`），建议在本地打开 README 时对照查看。

## 4. 核心概念与源码讲解

### 4.1 长上下文的平方复杂度瓶颈

#### 4.1.1 概念说明

标准（全量）注意力对序列中**每一对** (query, key) 都要算一次内积。设序列长度为 \( n \)、每个 head 的维度为 \( d \)，那么单个 head 的一次前向：

\[ \text{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right)V \]

其中 \( Q, K, V \in \mathbb{R}^{n \times d} \)。中间的分数矩阵 \( QK^\top \) 是 \( n \times n \) 的——这就是问题的根源：

- **计算量**：算出分数矩阵约需 \( n^2 d \) 次乘加，再乘上对 V 的加权汇聚又是 \( n^2 d \)，整体 \( O(n^2 d) \)。
- **显存**：朴素实现要把 \( n \times n \) 的分数矩阵（以及 softmax 的中间结果）完整存进显存，\( O(n^2) \)。flash-attn 通过分块在线计算消掉了这份显存，但 \( O(n^2 d) \) 的**浮点计算量一点没少**。

平方增长意味着什么？序列长度翻 4 倍，计算量翻 16 倍。当下游需求从 8K 上下文走向 128K、1M 时，全量注意力的开销按平方爆炸，成为长上下文 LLM 最直接的瓶颈。

README 的 Abstract 段落正是从这个痛点出发的，并点名了两类已有方案及其取舍（详见 4.1.3 的引用）。

#### 4.1.2 核心流程

全量注意力的执行流程与开销增长可以用下面的伪代码与表格描述：

```text
for 每个 head:
    scores = Q @ K.T / sqrt(d)      # n×n 矩阵，n²d 次乘加
    probs  = softmax(scores, 因果掩码)  # 朴素实现需物化 n×n
    out    = probs @ V              # 再 n²d 次乘加
```

固定 \( d = 128 \)，只看分数矩阵的元素个数 \( n^2 \)（即朴素实现的显存压力下界，也是计算量的规模）：

| 序列长度 \( n \) | 分数矩阵元素 \( n^2 \) | 相对 1K 的倍数 |
| ---: | ---: | ---: |
| 1,024 | \( 1.05 \times 10^6 \) | 1× |
| 4,096 | \( 1.68 \times 10^7 \) | 16× |
| 8,192 | \( 6.71 \times 10^7 \) | 64× |
| 32,768 | \( 1.07 \times 10^9 \) | 1,024× |
| 1,048,576 | \( 1.10 \times 10^{12} \) | 1,048,576× |

结论：**想让百万级上下文可行，必须让每个 query 不再"看全部前文"**——也就是稀疏注意力。问题是：少看哪些？谁来决定？这正是 MoBA 登场的地方。

#### 4.1.3 源码精读

README 的 Abstract 用两段话完整交代了"问题—已有方案—MoBA 的立场"：

- [README.md:24](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L24)：指出传统注意力机制的复杂度随上下文长度**平方增长**（quadratic increase in computational complexity），是禁止性开销；并点名已有两类方案——要么引入强偏置结构（如 sink attention、window/滑动窗口注意力，任务特定、不够通用），要么激进地把注意力改成线性近似（在复杂推理任务上的表现尚未被充分验证）。
- [README.md:26](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L26)：给出 MoBA 的回答——遵循 **"less structure"（少预设结构）原则**，让模型**自主决定 attend 到哪里**，而不是人为引入预定义偏置；具体做法是把 MoE 的思想应用到注意力机制上；并且已部署支撑 Kimi 的长上下文请求。

注意这两段话里对"滑动窗口注意力"的评价：它把每个 token 的视野限制在固定窗口内，开销从 \( O(n^2) \) 降到 \( O(n \cdot w) \)，但**窗口外的信息被硬性切断**，属于"任务特定"的强偏置结构。MoBA 与它的本质区别在于：**看哪里由模型自己（通过 gate）决定，而非人为固定**。

#### 4.1.4 代码实践

**实践目标**：用一段几行的脚本建立对平方增长的第一手感受。

**操作步骤**（示例代码，可在任何有 Python 的机器上运行，不需要 GPU）：

```python
# 示例代码：attention_complexity.py —— 感受 n² 增长
for n in [1024, 4096, 8192, 32768, 65536, 1048576]:
    n_attn = n * n                  # 单 head 注意力分数矩阵的元素个数
    flops = 4 * n * n * 128         # d=128 时一次前向的近似乘加次数（QK^T 与 probs@V 各 n²d）
    print(f"seqlen={n:>8}, 分数矩阵元素={n_attn:.2e}, "
          f"相对1K倍数={n_attn / 1024**2:,.0f}x, 近似FLOPs={flops:.2e}")
```

**需要观察的现象**：序列长度每变为原来的 \( k \) 倍，"相对 1K 倍数"一列就变为原来的 \( k^2 \) 倍。

**预期结果**：32,768 一行应显示相对 1K 约 1,024 倍；1,048,576（1M）一行应显示约 1,048,576 倍——正好是长度倍数 1024 的平方。这就是"百万上下文比 1K 上下文的注意力开销高约一百万倍"的直观来源。

#### 4.1.5 小练习与答案

**练习 1**：flash-attn 已经把注意力的显存从 \( O(n^2) \) 降到近似 \( O(n) \)，为什么长上下文还需要稀疏注意力？

**参考答案**：flash-attn 消除的是 \( n \times n \) 分数矩阵的**显式物化**（显存问题），但每个 query 仍要与每个 key 做内积，\( O(n^2 d) \) 的**浮点计算量**没有减少。长上下文的瓶颈不只是放不放得下，还有算不算得完，所以需要稀疏化来真正削减计算量。

**练习 2**：把序列长度从 4,096 提升到 65,536（16 倍），全量注意力的计算量变为多少倍？

**参考答案**：\( 16^2 = 256 \) 倍。平方复杂度下，长度倍数 \( k \) 对应计算量倍数 \( k^2 \)。

**练习 3**：README 的 Abstract 对"window attention"和"linear attention"分别给出了什么批评？

**参考答案**：window/sink attention 属于**强偏置结构**（strongly biased structures），是任务特定（task-specific）的，即硬性规定"只看窗口内"，可能切断必要信息；linear attention 则是对注意力机制的**激进修改/近似**，在复杂推理任务上的性能尚未被充分验证。

### 4.2 MoBA 核心概念：块稀疏 + 无参数 top-k gating + 无缝切换

#### 4.2.1 概念说明

MoBA = **Mixture of Block Attention**。它的三个关键词在 README 开头被并列列出（见 4.2.3 的引用），这里逐一拆解：

1. **块稀疏注意力（Block Sparse Attention）**：把 KV 序列按固定大小 `moba_chunk_size` 切成若干**块（block/chunk）**。稀疏的基本单位不是单个 token，而是整块——每个 query token 不再看清所有前文，而是只看清**被选中的少数几个块**。以块为单位有两个好处：稀疏模式规整、易于高效内核实现；选择问题的规模从 \( n \) 个 token 缩减为 \( n / \text{chunk\_size} \) 个块。

2. **无参数 gating（Parameter-less Gating）**：谁来决定每个 query 选哪些块？MoBA 用一个 **top-k gate**。称之为"无参数"是因为这个 gate **不引入任何可训练权重**——README 中 `moba_naive` 实现的做法是：对每个块内的 K 求均值得到该块的"代表向量"，再与 query 做内积得到该块的 gate 分数，取得分最高的 `moba_topk` 个块。这一点可以在 naive 实现的源码里直接得到印证（见 4.2.3）。

3. **MoE 视角**：把每个 KV 块看成一个"专家"，gate 就是 router，每个 query 只被路由到 top-k 个专家——这正是名字 "Mixture of **Block**" 的由来。

另外两个使用层面的关键事实：

- **当前块必选与因果性**：gate 分数经过因果规则修正——query 只能选到它位置之前的块，且**当前所在块强制选中**（保证每个 token 至少能看到紧邻上文）。这一规则的代码形态（把当前块 gate 置 \( +\infty \)、未来块置 \( -\infty \)）将在第二单元精读。
- **需要继续训练，不是即插即用**：README 用醒目的 Note 说明 MoBA 需要对现有模型做 continue training 才能获得加速收益，**不是**可以直接套在预训练模型上的 drop-in 稀疏注意力方案。结合 README 特性列表里 "each query token **learns** to attend to the most relevant KV blocks" 的措辞可以理解原因：块选择行为本身是模型要学的东西，未经适配的模型不一定会把 gate 用好。

#### 4.2.2 核心流程

MoBA 前向的概念流程（忽略实现细节）：

```text
输入: Q, K, V（长为 n），超参数 chunk_size 与 topk

1. 分块:  把 KV 序列切成 n / chunk_size 个块
2. 代表:  每块内 K 求均值 → 该块的 gate key（块级代表向量）
3. 打分:  gate 分数 = Q 与各块代表向量的内积        # 无任何可训练参数
4. 修正:  因果规则——未来块分数置 -inf；当前块分数置 +inf（必选）
5. 选块:  每个 query（按 head）取分数最高的 topk 个块
6. 计算:  只在被选中的块上做注意力，合并得到输出
```

稀疏度（计算量占比）的粗略估计：每个 query 大约只 attends 到 \( \text{topk} \times \text{chunk\_size} \) 个 key，因此与全量注意力相比的计算量比例约为

\[ \frac{\text{topk} \times \text{chunk\_size}}{n} \quad (\text{当 } n \gg \text{chunk\_size} \text{ 时}) \]

例如 \( n = 32768 \)、`chunk_size = 2048`、`topk = 3` 时比例约为 \( 6144 / 32768 \approx 18.75\% \)，即理论计算量降到约五分之一——序列越长，这个比例越低，收益越大。这也解释了 MoBA 为何是"为长上下文而生"的。

**无缝切换（seamless transition）**：README 强调 MoBA 可以作为全量注意力的灵活替代，允许在 full 与 sparse 两种模式间无缝切换。其设计层面的含义是：MoBA 没有改变注意力的数学形式（仍是标准 softmax 注意力），只是限制了每个 query 可见的 KV 范围，且"当前块必选"等规则保证了模式的连续性；切换到 full attention 不需要改模型结构。实现层面如何保证这一点（高效实现把每个 batch 的最后一块留给一条全量 self-attention 支路），将在第三单元 `moba_efficient.py` 精读时展开。

#### 4.2.3 源码精读

- [README.md:13](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L13)：第一条特性——**Trainable Block Sparse Attention**：全文按块划分，每个 query token 学习 attend 到最相关的 KV 块。注意 "learns" 一词，它呼应了"需要继续训练"的使用前提。
- [README.md:14](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L14)：第二条特性——**Parameter-less Gating**：新颖的无参数 top-k gating 机制为每个 query token 挑选最相关的块，保证模型只聚焦信息量最大的块。
- [README.md:15](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L15)：第三条特性——**Seamlessly Transition between Full and Sparse Attention**：MoBA 被设计为全量注意力的灵活替代品，可在全量/稀疏模式间无缝切换。
- [README.md:21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L21)：使用前提的 Note——MoBA 需要 continue training 才能获得加速收益，不是无需额外训练即可套用的 drop-in 方案。
- [moba/config.py:5-7](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/config.py#L5-L7)：MoBA 的全部超参数就浓缩在这个数据类里——`moba_chunk_size`（块大小）与 `moba_topk`（每个 query 选的块数）。整个机制的"旋钮"只有这两个。
- "无参数 gate"并非营销话术，可在 naive 实现中直接验证：[moba/moba_naive.py:42-50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L42-L50) 对每个块内的 K 做 `mean` 得到 `key_gate_weight`（块代表向量），[moba/moba_naive.py:54-55](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L54-L55) 用一个 `einsum` 把 Q 与它做内积得到 gate——全程没有任何 `nn.Parameter`。因果修正则体现在 [moba/moba_naive.py:60-61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L60-L61)：未来块 gate 置 \( -\infty \)、当前块置 \( +\infty \)。本讲只需看懂这三处的**语义**，逐行精读留给第二单元。

#### 4.2.4 代码实践

**实践目标**：通过纸笔推演理解 `chunk_size` 与 `topk` 如何共同决定稀疏度。

**操作步骤**：

1. 取 \( n = 32768 \)、`chunk_size = 2048`、`topk = 3`（即 README 性能声明的测试配置）。在纸上回答：一共有多少个块？每个 query 最多 attend 多少个 key？计算量占比是多少？
2. 把 `chunk_size` 改成 512 重算一遍；再把 `topk` 改成 8 重算一遍。
3. 保持 `chunk_size = 2048, topk = 3` 不变，把 \( n \) 改成 131,072（128K）再算一遍占比。

**需要观察的现象**：块数 = \( n / \text{chunk\_size} \)；占比随 `topk × chunk_size` 增大而升高、随 \( n \) 增大而下降。

**预期结果**：

- 步骤 1：16 个块；每 query 最多 \( 3 \times 2048 = 6144 \) 个 key；占比 \( \approx 18.75\% \)。
- 步骤 2：`chunk_size=512` 时 64 个块、占比 \( 3 \times 512 / 32768 \approx 4.7\% \)；`topk=8`（chunk 2048）时占比 \( 8 \times 2048 / 32768 = 50\% \)。
- 步骤 3：128K 时占比 \( \approx 4.7\% \)——序列越长，MoBA 相对全量注意力的理论节省越可观。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 MoBA 的 gate 是"无参数"的？这和一般 MoE 的 router 有什么不同？

**参考答案**：MoBA 的 gate 分数由 query 与"块内 K 的均值向量"直接内积得到（`moba_naive.py` 中就是一个 `einsum`），没有引入任何可训练权重；一般 MoE 的 router 是一个带权重的线性层，需要随模型训练。MoBA 的"可训练性"体现在整个注意力行为随模型继续训练而适配，而不是 gate 本身有参数。

**练习 2**：`moba_chunk_size` 和 `moba_topk` 这两个超参数分别控制什么？把 `topk` 调得非常大（超过块数）会发生什么？

**参考答案**：`moba_chunk_size` 决定 KV 分块的大小（块数 = 序列长度除以它），`moba_topk` 决定每个 query 最多选几个块。当 `topk` 不小于总块数时，每个 query 会选中所有（因果允许范围内的）块，行为退化为全量注意力——这也是"全量/稀疏无缝切换"设计的一个自然推论（naive 实现里 topk 实际取 `min(moba_topk, num_block)`，见 `moba_naive.py` 第 64 行，第二单元会详细讲）。

**练习 3**：README 为什么强调 MoBA "不是 drop-in 方案"？

**参考答案**：因为块选择行为需要模型通过 continue training 学会——预训练时模型从未见过"只能看部分块"的注意力模式，直接替换无法保证效果与加速收益。README 第 21 行的 Note 明确了这一点。

### 4.3 项目依赖与仓库定位：naive 教学实现 + 高效实现

#### 4.3.1 概念说明

这个仓库的哲学是"一件事，两种实现"：

- **`moba_naive`：教学参考实现**。基于 attention mask 的朴素实现，慢，但逻辑一目了然，而且**与 `moba` 后端共享同一套 `MoBAConfig`**。它的价值是当"教科书"和"黄金参考"——高效实现正确与否，靠与它对齐来验证（第四单元的测试就是这么做的）。README 还特别提示：你可以把它的注意力掩码存下来可视化，直观看到块选择过程。
- **`moba`（即 `moba_efficient`）：生产实现**。基于 flash-attn 的 varlen 接口组合实现，是 README 推荐 practical applications 使用的版本。

两者的关系类似"参考答案"与"考场快解"：先读懂 naive，再去看 efficient 为什么快、以及如何证明它和 naive 算的是同一个东西。

**依赖与环境**是初学者最容易踩坑的地方：README 明确说当前 kernel 实现依赖 **`flash-attn==2.6.3`（精确锁定版本）和 `torch>=2.1.0`**。`requirements.txt` 里还加上 `transformers>=4.48.3`、`accelerate>=1.3.0`、`einops>=0.8.1`。flash-attn 是带 CUDA 编译的库，版本不匹配经常导致接口对不上，所以这里用 `==` 精确锁定——安装时请严格按 README 的步骤来。

#### 4.3.2 核心流程

从克隆仓库到跑通，官方路径是：

```text
1. conda create -n moba python=3.10 && conda activate moba   # 建议的隔离环境
2. pip install .                                             # 安装 moba 包及其依赖
3. pytest tests/test_moba_attn.py                            # 正确性单元测试
4. python3 examples/llama.py --model meta-llama/Llama-3.1-8B --attn moba   # 端到端示例
```

安装后，代码层面的调用链鸟瞰（细节在后续各讲展开）：

```text
examples/llama.py
  └─ register_moba(MoBAConfig(chunk_size, topk))     # 注册两个注意力后端
  └─ AutoModelForCausalLM.from_pretrained(..., attn_implementation="moba")
       └─ transformers 按 "moba" 名字分发到 moba_layer（wrapper.py）
            ├─ "moba"       → moba_attn_varlen        （moba_efficient.py，高效实现）
            └─ "moba_naive" → moba_attn_varlen_naive  （moba_naive.py，参考实现）
```

也就是说，MoBA 对外不是一个独立的模型，而是 **transformers 的一个自定义注意力后端**——这也是它能"即选即用"（`--attn` 一换）的原因。

#### 4.3.3 源码精读

- [README.md:42-49](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L42-L49)：环境搭建。第 43 行的 Note 是关键约束：当前 kernel 实现依赖 `flash-attn==2.6.3` 与 `torch >= 2.1.0`；随后给出 `conda create` + `pip install .` 的标准安装步骤。
- [README.md:51-58](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L51-L58)：Quick Start。说明这是 transformers 友好的实现，可用 `--attn` 在 `moba` 与 `moba_naive` 两个后端间选择，并给出 Llama-3.1-8B 的示例命令。
- [README.md:60-62](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L60-L62)：**Implementation Details**，两个后端的官方定位：第 61 行说 `moba_naive` 是基于 attention mask 的 naive 实现，目的是帮助理解 MoBA 如何选择 chunk，可以保存并可视化 attention mask 观察块选择过程；第 62 行说 `moba_efficient` 是面向性能的生产实现，**相对 `moba_naive` 最高 40 倍加速**，测试条件为 **32K 序列长度、1 个注意力头、MoBA Block 2048、MoBA Topk 3**，推荐实际应用使用。
- [README.md:65-68](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L65-L68)：单元测试入口 `pytest tests/test_moba_attn.py`。
- [requirements.txt:1-5](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/requirements.txt#L1-L5)：五个依赖——`flash-attn==2.6.3`（唯一精确锁版本的依赖）、`transformers>=4.48.3`、`accelerate>=1.3.0`、`einops>=0.8.1`（张量重排的 DSL，高效实现大量使用）、`torch>=2.1.0`。
- [pyproject.toml:1-14](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/pyproject.toml#L1-L14)：打包配置。第 7 行 `dynamic = ["dependencies"]` 加第 9-10 行把依赖指向 `requirements.txt`（所以装的是上面那份清单）；第 12-14 行声明只打包 `moba*` 包——再次印证"核心就一个 `moba/` 目录"。
- [moba/__init__.py:9-11](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py#L9-L11)：`register_moba` 把 `moba_naive` 和 `moba` 两个名字注册进 transformers 的 `ALL_ATTENTION_FUNCTIONS`，分别绑定 naive 与高效两个内核和同一份 `MoBAConfig`——上一节调用链鸟瞰的代码出处。
- [examples/llama.py:13-18](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L13-L18)：`--attn` 参数只有三个合法值：`flash_attention_2`（原生 flash-attn 基线）、`moba`、`moba_naive`；[examples/llama.py:21-27](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/examples/llama.py#L21-L27)：先 `register_moba(MoBAConfig(...))` 再以 `attn_implementation=args.attn` 加载模型，完成接线。

#### 4.3.4 代码实践

**实践目标**：完成 README 的精读检验——这是本讲的主实践。

**操作步骤**：

1. 通读 [README.md](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md) 全文（重点是第 11-21 行特性与 Note、第 60-62 行 Implementation Details），并本地打开 `figures/` 下的四张配图。
2. 用自己的话写一段**约 100 字**的总结，说清 MoBA 与全量注意力、滑动窗口注意力（window attention）的区别。
3. 列出 `moba` 与 `moba_naive` 两个注意力后端在 README 中的定位差异（各自面向什么场景、README 推荐用哪个）。
4. 指出 README 中"40x speedup"声明的**对比对象**与**全部测试条件**（提示在第 62 行，注意对比对象不是全量 flash attention）。
5. 环境自查（可选，需已 `pip install .`）：运行 `python -c "import flash_attn, torch; print(flash_attn.__version__, torch.__version__)"`，确认版本满足 `flash-attn==2.6.3`、`torch>=2.1.0`。无环境时可跳过，标注"待本地验证"。

**需要观察的现象**：第 4 步中最容易看漏的是"40 倍是相对谁"。

**预期结果（参考）**：

- 100 字总结示例（仅供参考写法）：*"MoBA 是 Moonshot 提出的块稀疏注意力：把上下文切成块，让每个 query 通过无参数 gate 只挑选最相关的 top-k 个 KV 块参与计算，从而把长序列注意力的平方开销降为与所选块数成正比。与全量注意力相比它更省算力；与滑动窗口相比它不预设固定视野，看哪里由模型自己学习决定，且可在全量与稀疏间无缝切换，但需要继续训练。"*
- 后端定位差异：`moba_naive`——基于 attention mask 的朴素实现，慢，用于理解 chunk 选择机制、可可视化掩码；`moba`（efficient）——生产级性能优化实现，README 明确推荐实际应用使用。
- 40x 的条件：**相对 `moba_naive`**（不是相对全量 flash attention），测试配置为 32K 序列长度、1 个注意力头、MoBA Block（chunk_size）2048、MoBA Topk 3。
- 版本自查输出应为 `2.6.3` 与 `2.1.0` 或更高的 torch 版本（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `requirements.txt` 里唯独 `flash-attn` 用 `==` 精确锁定版本，其他依赖都用 `>=`？

**参考答案**：`flash-attn` 是带 CUDA 编译的底层库，其 Python 接口在不同版本间变动较大；本仓库的高效实现直接调用其 varlen 前向/反向的内部接口，行为与 2.6.3 强绑定。其他依赖（transformers/torch/accelerate/einops）对外接口相对稳定，只需最低版本约束。

**练习 2**：不看调用代码，仅凭 `pyproject.toml` 判断：`pip install .` 之后，`import moba` 能用，但 `import examples` 呢？

**参考答案**：不能用。`pyproject.toml` 第 12-14 行的 `[tool.setuptools.packages.find]` 只包含 `moba*`，`examples/` 和 `tests/` 不会被安装为包，它们只是仓库里的脚本，需在仓库根目录下直接运行。

**练习 3**：如果你只想"看懂 MoBA 怎么选块"，README 建议用哪个后端？如果想在自己的服务里用呢？

**参考答案**：看懂选块过程用 `moba_naive`（README 第 61 行：可保存并可视化 attention mask 来观察 block selection）；实际应用用 `moba`（第 62 行：production-ready，最高相对 naive 40 倍加速）。

## 5. 综合实践

把本讲三个模块串成一份**《MoBA 项目速览笔记》**（一个 Markdown 文件即可，建议放在本讲义同目录）：

1. **问题背景**：粘贴 4.1.4 的示例脚本输出，写一句话解释为什么 1M 上下文的全量注意力不可行。
2. **机制卡片**：画一张 MoBA 流程草图（分块 → 块代表向量 → gate 打分 → 因果修正 → top-k → 稀疏注意力），并在旁边标注两个超参数 `moba_chunk_size`、`moba_topk`（出处 `moba/config.py`）各自管什么。
3. **稀疏度计算**：给出 4.2.4 三组配置的手算结果，并用公式 \( \text{topk} \times \text{chunk\_size} / n \) 验证。
4. **仓库速览**：列出仓库 8 个核心文件（`moba/` 下 4 个 + `examples/llama.py` + 2 个测试 + `pyproject.toml`）及一句话作用，注明本手册第几单元会精读它。
5. **关键事实清单**：MoBA 需要继续训练（非 drop-in）；40x 是相对 `moba_naive` 且条件为 32K/单头/block 2048/topk 3；依赖锁定 `flash-attn==2.6.3`、`torch>=2.1.0`。

这份笔记将是你后续阅读 `moba_naive.py` 与 `moba_efficient.py` 时的"地图"。

## 6. 本讲小结

- 全量注意力的计算量与显存压力随序列长度**平方增长**（\( O(n^2 d) \)），是长上下文 LLM 的核心瓶颈；flash-attn 只省显存、不省计算量。
- 已有稀疏方案各有代价：window/sink attention 引入任务特定的强偏置，linear attention 是激进的近似——MoBA 选择 **"less structure"**：让模型自己决定看哪里。
- MoBA = 把 MoE 思想用于注意力：KV 按 `moba_chunk_size` 分块，每个 query 由**无参数 gate**（与块内 K 均值内积）打分后选 `moba_topk` 个块，当前块必选、未来块被因果规则排除。
- MoBA **需要 continue training** 才能获得加速收益，不是即插即用的 drop-in 方案；但它支持全量/稀疏模式的无缝切换。
- 本仓库极小而聚焦：`moba_naive`（基于 mask 的教学参考实现，可可视化块选择）与 `moba`（基于 flash-attn 的生产实现，相对 naive 最高 40x：32K 序列、单头、block 2048、topk 3），通过 `register_moba` 注册为 transformers 注意力后端，用 `--attn` 即可切换。
- 环境硬约束：`flash-attn==2.6.3`、`torch>=2.1.0`、`transformers>=4.48.3`。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：《快速上手：安装、注册注意力后端并运行示例》——动手完成 `pip install .`、`pytest tests/test_moba_attn.py`，并用不同 `--attn` / `--moba-chunk-size` / `--moba-topk` 参数运行 `examples/llama.py`，把本讲的参数概念变成肌肉记忆。
- **随后（u1-l3）**：补齐读源码前的张量基础知识（HF 布局 vs flash-attn 布局、varlen 与 `cu_seqlens`、GQA 与 prefill/decode），这是进入第二单元的门票。
- **提前热身（可选）**：浏览 [moba/moba_naive.py:1-90](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L1-L90)，不求全懂，只找一找本讲提到的 `mean`（块代表向量）、`einsum`（gate 打分）、`+inf/-inf`（因果修正）分别在哪几行——第二单元将从这里逐行展开。
- 若想读原始论文，README 顶部链接了 arXiv:2502.13189 与仓库内的 `MoBA_Tech_Report.pdf`。
