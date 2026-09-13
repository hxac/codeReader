# 背景知识：标准残差连接为何会稀释与膨胀

## 1. 本讲目标

上一讲（[u1-l1 项目概览](u1-l1-project-overview.md)）我们确认了仓库结构，并留下一个伏笔：README 说标准残差会「稀释每层的贡献」并让隐藏状态幅度「无界增长」。本讲把这句话拆开讲透。读完本讲，你应该能够：

1. 写出标准残差连接的数学形式 \( \mathbf{h}_l = \mathbf{h}_{l-1} + \mathbf{v}_l \)，并亲手把它递推展开成「所有子层输出按固定权重 1 求和」。
2. 说清楚 **PreNorm** 结构长什么样、为什么它的残差主干上没有任何缩放，从而导致隐藏状态幅度随深度**无界增长**。
3. 理解同一枚硬币的另一面——**层贡献稀释**：深度越大，单个子层的输出在主干中的相对占比越小，梯度范数在各层之间也分布不均。
4. 会读 `assets/training_dynamics.png`：从图中指出基线的幅度增长曲线与梯度衰减曲线，并与 AttnRes 的有界曲线对照。
5. 完成主实践：用 PyTorch 写一个 12 层 PreNorm Transformer 前向骨架，用 **forward hook** 逐层记录隐藏状态 L2 范数，画出「范数–深度」曲线，亲眼看到幅度膨胀。

本讲只分析**问题**，不给解决方案；解决方案（\( \mathbf{h}_l = \sum \alpha_{i \to l} \mathbf{v}_i \)）是下一讲的主角。

## 2. 前置知识

上一讲已经用一句话带过了残差连接和 PreNorm，本讲要把它们变成可计算的数学对象。需要的铺垫如下：

- **向量范数（L2 范数）**：\( \|\mathbf{x}\| = \sqrt{\sum_j x_j^2} \)，衡量一个向量的"大小"。本讲说「隐藏状态幅度」，指的就是每个 token 的 \( d \) 维表示向量的 L2 范数。
- **子层（sublayer）与记号 \( \mathbf{v} \)**：一个 Transformer 层 = 一个注意力子层 + 一个 MLP 子层，共 2 个子层（README 伪代码注释也强调了这一点）。我们把第 \( l \) 个子层的输出记作 \( \mathbf{v}_l \)，它就是要被"加"进残差主干的那份增量。
- **LayerNorm / RMSNorm**：两种归一化层，作用都是把输入向量调整到稳定的幅度（去掉量纲膨胀）。RMSNorm 只做缩放不做去均值，计算更省，是现代大模型的主流选择——README 伪代码里出现的 `RMSNorm` 就是它。本讲只需要知道「归一化 = 把向量拉回固定幅度」。
- **PreNorm vs PostNorm**：归一化层放在子层**入口**（先 Norm 再进子层，主干不动）叫 PreNorm；放在子层**出口**（先累加再整体 Norm）叫 PostNorm。这一字之差正是本讲第 2 个模块的主题。
- **随机游走直觉**：每步随机走一步、方向独立，走 \( l \) 步后离原点的平均距离约 \( \sqrt{l} \) 步长——「累加独立增量，幅度按 \( \sqrt{l} \) 增长」。这是理解幅度增长的钥匙。
- **forward hook**：PyTorch 的 `module.register_forward_hook(fn)` 可以在某个模块前向结束后自动调用 `fn`，拿到的输出无需改动模型代码——是「探针式」测量隐藏状态的标准工具，本讲主实践用它。
- 承接上一讲的术语：**drop-in replacement**、**伪代码**、**einsum** 已在 u1-l1 讲过，不再重复。

> 说明：本讲的背景知识（PreNorm/PostNorm、范数增长）属于 Transformer 通用常识，仓库里与之直接相关的"源码"是 README 的问题陈述句与伪代码中残差累加的那几行；更严格的定理与实验设置在论文 PDF 中（具体页码待确认）。

## 3. 本讲源码地图

本讲的"源码"集中在 README 的三处文字与两张图上：

| 文件 | 行号/位置 | 作用 | 在本讲中的用法 |
|:---|:---|:---|:---|
| `README.md` | [L35-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L35-L43) | Overview：问题陈述 + AttnRes 公式 | 精读 L37 这句"问题总陈述"，逐词拆解 |
| `README.md` | [L67-L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L90) | `forward` 伪代码 | L80-L81、L87-L88 两处标准残差累加 + PreNorm 风格证据 |
| `README.md` | [L121-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L127) | Training Dynamics 小节 | L123 给出与本文问题一一对应的"解药"表述 |
| [assets/training_dynamics.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/training_dynamics.png) | 整图 | 三联图：验证损失 / 输出幅度 / 梯度幅度 | 本讲的核心实验证据，模块 4.3 逐面板精读 |
| [assets/overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) | 子图 (a) | 标准残差的均匀加法累加示意 | 模块 4.1 的直观配图 |

## 4. 核心概念与源码讲解

本讲包含三个最小模块：**标准残差累加**、**PreNorm 结构**、**幅度增长与稀释现象**。

### 4.1 标准残差累加

#### 4.1.1 概念说明

标准残差连接（residual connection / skip connection）是 2015 年 ResNet 提出的技巧：子层不直接替换输入，而是把子层输出**加**到输入上：

\[ \mathbf{h}_l = \mathbf{h}_{l-1} + \mathbf{v}_l \]

其中 \( \mathbf{h}_{l-1} \) 是进入第 \( l \) 个子层时残差主干（residual stream）里的表示，\( \mathbf{v}_l \) 是该子层的输出。它的好处是给梯度留了一条恒等捷径：反向传播时梯度可以原封不动地沿"+"流回浅层，深度网络才训得动。

但注意这个"+"在数学上做了什么——把递推**展开**（telescoping）：

\[ \mathbf{h}_L = \mathbf{h}_0 + \mathbf{v}_1 + \mathbf{v}_2 + \cdots + \mathbf{v}_L = \sum_{i=0}^{L} 1 \cdot \mathbf{v}_i \]

（\( \mathbf{v}_0 \) 视作词嵌入，即 \( \mathbf{h}_0 \)。）这就是 README 那句 "accumulate all layer outputs with **fixed unit weights**" 的字面意思：

- **权重恒为 1**：每个子层输出的权重都是 1，没有 0.5、没有 2，更不能按内容调整；
- **输入无关**：无论当前输入是什么 token、什么语义，权重都是同一组（全 1）；
- **无归一化**：求和没有任何缩放因子（对比：平均池化会除以 \( L+1 \)）。

一句话：标准残差 = **对所有历史子层输出做一次"权重全为 1 的加法聚合"**。AttnRes 要替换的正是这个聚合算子——下一讲你会看到它只是把权重 1 换成 softmax 出来的 \( \alpha_{i \to l} \)。

还有一个容易忽略的事实：**标准残差在 Block AttnRes 中并没有消失**。Block 变体只是把"对所有层做注意力"改成"块间做注意力"，**块内**仍然是标准残差累加——所以本讲分析的问题在 AttnRes 里被"限制在块内"，而不是被消灭。

#### 4.1.2 核心流程

标准 Transformer（基线）一个子层的执行流程：

```text
输入主干 h
  ↓
v = Sublayer( Norm(h) )     ← 子层吃"归一化后"的 h（PreNorm，见 4.2）
  ↓
h ← h + v                   ← 标准：固定权重 1 直接累加
  ↓
输出主干 h（传给下一个子层，主干本身不被缩放）
```

把整个深度方向串起来看：

```text
h_0 = 嵌入 v_0
h_1 = v_0 + v_1
h_2 = v_0 + v_1 + v_2
...
h_L = v_0 + v_1 + ... + v_L     ← 深度 L 的主干 = L+1 项等权求和
```

深度越大，求和项数越多——这就是后面两个毛病（膨胀与稀释）的共同根源。

#### 4.1.3 源码精读

**问题总陈述句。**

[README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) —— 这是全仓库对"问题"最完整的一句话，值得逐词读：标准残差连接以**固定单位权重**（fixed unit weights）累加**所有**层输出；随深度增长，这种**均匀聚合**（uniform aggregation）会**稀释**（dilutes）每一层的贡献，并使隐藏状态幅度**无界增长**（grow unboundedly）——这是 **PreNorm 的一个著名问题**。本讲三个模块恰好对应这句话的三个成分：累加机制（4.1）、PreNorm（4.2）、稀释与膨胀（4.3）。

**伪代码里的标准残差——块内累加的两行 "+"。**

[README.md:L80-L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80-L81) —— `attn_out = self.attn(self.attn_norm(h))` 之后紧跟 `partial_block = partial_block + attn_out ...`：注意力子层的输出以权重 1 加进块内主干 `partial_block`。注意 `partial_block` 直接参与 `+`，前后都没有任何归一化或缩放——这就是 4.1.1 展开式里的那个"+"。

[README.md:L87-L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87-L88) —— MLP 子层同理：`mlp_out = self.mlp(self.mlp_norm(h))`，然后 `partial_block = partial_block + mlp_out`。把 L81、L88 连起来看，一个 Transformer 层内发生了两次"标准残差累加"，对应 [README.md:L73-L74](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L73-L74) 注释所说的「block_size 按 ATTN+MLP 计数、每层 2 个子层」。

**嵌入是第 0 项。**

[README.md:L69-L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L69-L70) —— 注释 `# blocks already include token embedding` 说明词嵌入被当作第一个块表示参与后续聚合，对应展开式里的 \( \mathbf{v}_0 \)。

**直观配图。**

[README.md:L25-L29](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L25-L29) —— overview 图注的 (a)：「标准残差 = 均匀的加法累加」。打开 [assets/overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) 子图 (a)，可以看到每个 \( \mathbf{v}_i \) 都以同一种连线汇入主干——没有粗细之分，因为权重全是 1。

#### 4.1.4 代码实践

**实践一：亲手验证「递推 = 等权求和」与贡献占比递减**

1. **实践目标**：用 20 行张量运算证明 4.1.1 的展开式成立，并观察"新子层输出的相对占比"如何随深度下降。
2. **操作步骤**：以下为**示例代码**（仓库无 Python 代码，此为按展开式编写的练习），保存为 `telescoping.py` 后运行（CPU 即可）：

   ```python
   # telescoping.py —— 标准残差的递推展开与贡献占比（示例代码）
   import torch
   torch.manual_seed(0)

   d, n_sub = 64, 12
   v = torch.randn(n_sub + 1, d)        # v[0] 视作词嵌入，v[1:] 是各子层输出

   h, rows = v[0].clone(), []
   for l in range(1, n_sub + 1):
       h = h + v[l]                     # 标准残差：h_l = h_{l-1} + v_l
       rows.append((l, h.norm().item(), (v[l].norm() / h.norm()).item()))

   print("递推结果 == 直接求和:", torch.allclose(h, v.sum(0)))  # 权重恒为 1 的直接证据
   print(f"{'深度':>4} {'||h_l||':>10} {'||v_l||/||h_l||':>14}")
   for l, hn, ratio in rows:
       print(f"{l:4d} {hn:10.3f} {ratio:14.4f}")
   print("独立假设理论值 sqrt(d)*sqrt(l+1) =", (d * (n_sub + 1)) ** 0.5)
   ```

3. **需要观察的现象**：第一行打印 `True`；表格里 \( \|\mathbf{h}_l\| \) 随 \( l \) 单调上升；最后一列 \( \|\mathbf{v}_l\| / \|\mathbf{h}_l\| \) 从约 0.7 一路降到约 0.28（\( 1/\sqrt{13} \approx 0.277 \)，独立随机向量情形）。
4. **预期结果**：递推与 `v.sum(0)` 完全一致（浮点误差内），印证「固定单位权重累加」；末层范数接近理论值 \( \sqrt{d}\cdot\sqrt{L+1} \)。具体数值随随机种子与维度变化，待本地验证。
5. 若把 `n_sub` 改成 48，占比会继续降到 \( 1/\sqrt{49} \approx 0.14 \)——深度翻 4 倍，单层相对影响又腰斩，这正是"稀释"的量化形态。

#### 4.1.5 小练习与答案

**练习 1**：用一句话归纳证明 \( \mathbf{h}_L = \sum_{i=0}^{L} \mathbf{v}_i \)。

**参考答案**：对 \( l \) 归纳：\( \mathbf{h}_1 = \mathbf{v}_0 + \mathbf{v}_1 \) 成立；若 \( \mathbf{h}_{l-1} = \sum_{i \le l-1} \mathbf{v}_i \)，则 \( \mathbf{h}_l = \mathbf{h}_{l-1} + \mathbf{v}_l = \sum_{i \le l} \mathbf{v}_i \)。每步只是把新的一项以系数 1 加进求和，没有任何缩放。

**练习 2**：README 伪代码中哪两行是"标准残差累加"？为什么说 Block AttnRes 仍保留了标准残差？

**参考答案**：[L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81)（`partial_block = partial_block + attn_out ...`）与 [L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L88)（`partial_block = partial_block + mlp_out`）。因为 Block AttnRes 只在**块间**用注意力替换聚合，**块内**依旧用 `partial_block` 做权重全 1 的累加——标准残差被"限制在块内"，作为块内部分和的维护方式保留下来。

**练习 3**：「固定单位权重」的"输入无关"是什么意思？举一个反例。

**参考答案**：无论输入 token 是什么、上下文是什么，聚合系数都恒为 1，不随内容变化。反例即 AttnRes：\( \alpha_{i \to l} \) 由伪查询与各层表示打分后 softmax 得到，逐 token、逐输入地变化——同一个深度位置，不同 token 可以从不同历史层取不同比例。

### 4.2 PreNorm 结构

#### 4.2.1 概念说明

归一化层放在哪，决定了残差主干会不会被"修剪"。两种经典摆法：

- **PostNorm**（原始 Transformer）：\( \mathbf{h} \leftarrow \mathrm{Norm}(\mathbf{h} + \mathrm{Sublayer}(\mathbf{h})) \)。归一化作用在**累加之后的主干**上，每过一层就把幅度拉回正常范围——幅度有界，但梯度捷径被 Norm 切断，深层训练不稳定，需要学习率 warmup 等技巧。
- **PreNorm**（现代主流）：\( \mathbf{h} \leftarrow \mathbf{h} + \mathrm{Sublayer}(\mathrm{Norm}(\mathbf{h})) \)。归一化只作用在**子层入口的分支**上，主干上没有任何缩放——梯度捷径畅通、训练稳定，代价是主干幅度**无界增长**。

为什么 PreNorm 的幅度一定会涨？两个观察合在一起就清楚了：

1. **子层入口被 Norm 固定**：子层看到的输入永远是 \( \mathrm{Norm}(\mathbf{h}) \)，其幅度被归一化层拉到稳定值（RMSNorm/LayerNorm 的输出幅度基本恒定）。于是每个子层的输出 \( \mathbf{v}_l \) 的"生产规模"是稳定的——不管主干已经涨到多大，新加进来的增量都差不多是 \( O(\sigma) \) 量级。
2. **主干从不缩水**：主干只是不断做 \( + \)，既不除以项数、也不过 Norm。\( L+1 \) 个 \( O(\sigma) \) 的增量堆在一起，幅度只增不减。

用随机游走类比：每层往主干里加一个方向"大致独立、步长稳定"的增量，\( L \) 步之后主干范数约 \( \sqrt{L}\cdot O(\sigma) \)；若各层增量方向相关（真实网络里很常见，比如多层都强化同一语义方向），增长会更快，接近线性 \( L \cdot O(\sigma) \)。无论哪种，都是**无界**的——这正是 [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) 说的 "a well-known problem with PreNorm"。

> 术语提示：不要把"子层入口有 Norm"误解成"主干被归一化"。PreNorm 的 Norm 在**旁路**上，主干是那条从头到尾只做加法的直线。

#### 4.2.2 核心流程

PreNorm 与 PostNorm 一层内的数据流对比：

```text
PreNorm（主干无归一化）             PostNorm（主干被归一化）
h ──┬──────────────(+)── h'        h ──(+)── Norm ── h'
    │                ↑                 ↑
    Norm → Sublayer ─┘v              Sublayer(h) ──┘
主干：只加、不缩放                    主干：每层都被拉回固定幅度
幅度：随深度无界增长 ↗↗              幅度：基本恒定 →
梯度：恒等捷径畅通、稳定             梯度：捷径被 Norm 打断、深层难训
```

推演 PreNorm 主干幅度的演化：

\[ \mathbf{h}_l = \sum_{i=0}^{l} \mathbf{v}_i,\quad \mathbb{E}\|\mathbf{h}_l\|^2 = \sum_{i=0}^{l} \mathbb{E}\|\mathbf{v}_i\|^2 \;\Rightarrow\; \|\mathbf{h}_l\| = O(\sqrt{l}\,\sigma) \]

（独立情形，交叉项为零；相关情形更快。）同时由于入口 Norm 的存在，\( \sigma \) 不随深度变化——于是比值 \( \|\mathbf{v}_l\| / \|\mathbf{h}_l\| \approx 1/\sqrt{l} \) 越来越小，这就是下一模块"稀释"的来源。

#### 4.2.3 源码精读

**伪代码中的 PreNorm 风格证据。**

[README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80) —— `attn_out = self.attn(self.attn_norm(h))`：`attn_norm` 包在 `self.attn` 的**入口**（先 norm 再进注意力），而主干 `partial_block` 不经过它。[README.md:L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87) —— `mlp_out = self.mlp(self.mlp_norm(h))` 同理。这两行就是「PreNorm：Norm 在子层入口、主干裸奔」的直接源码证据——即便是 AttnRes 的 forward，子层组织方式仍是标准 PreNorm。

**AttnRes 对 Norm 位置的沿用（伏笔）。**

[README.md:L61-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L64) —— `block_attn_res` 里 `K = norm(V)` 只归一化**打分用的 K**，被聚合的 `V` 保持原值。这种"归一化作用在评估侧、不作用在被累加的数值侧"的思路，与 PreNorm「归一化入口、不碰主干」一脉相承。细节留到第 2 单元第 3 讲，这里只需留下印象。

**问题与解药的一一对应。**

[README.md:L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L123) —— "AttnRes mitigates PreNorm dilution: output magnitudes remain bounded across depth and gradient norms distribute more uniformly across layers."（AttnRes 缓解 PreNorm 稀释：输出幅度跨深度保持有界，梯度范数在各层间分布更均匀。）把这句与 L37 对照，问题与解药逐项对应：稀释 → 更均匀的梯度分布；无界增长 → 有界的输出幅度。本讲先弄懂左边，右边由下一讲和第 2 单元第 5 讲验证。

#### 4.2.4 代码实践

**实践二：两种主干的对峙——只差一个 Norm**

1. **实践目标**：用最小张量实验对比「PreNorm 式主干（累加后不归一化）」与「PostNorm 式主干（累加后立即归一化）」的幅度演化，验证"主干上有没有 Norm"是幅度是否无界的开关。
2. **操作步骤**：以下为**示例代码**（简化模型：假设各子层输出是独立固定幅度的向量），保存为 `trunk_compare.py` 运行：

   ```python
   # trunk_compare.py —— PreNorm 式主干 vs PostNorm 式主干（示例代码）
   import torch
   torch.manual_seed(0)

   def rms_norm(x, eps=1e-6):          # 与伪代码 L62 的 RMSNorm 同类：只缩放
       return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + eps)

   d, n_sub = 64, 24
   v = torch.randn(n_sub, d)           # 24 个子层输出，每个分量 ~N(0,1)

   h_pre, h_post = torch.zeros(d), torch.zeros(d)
   print(f"{'子层':>4} {'PreNorm主干范数':>16} {'PostNorm主干范数':>16}")
   for i in range(n_sub):
       h_pre  = h_pre + v[i]           # PreNorm 式：主干只加不缩
       h_post = rms_norm(h_post + v[i])  # PostNorm 式：累加后立刻拉回固定幅度
       print(f"{i:4d} {h_pre.norm().item():16.3f} {h_post.norm().item():16.3f}")
   ```

3. **需要观察的现象**：左列从约 8（= \( \sqrt{d} \)）一路涨到约 39（≈ \( 8\sqrt{24} \)）；右列几乎恒定在 8 附近（RMSNorm 把每个分量的 RMS 固定为 1，\( d \) 维向量范数恒为 \( \sqrt{d} \)）。
4. **预期结果**：PreNorm 式主干范数按 \( \sqrt{i+1} \) 量级增长、PostNorm 式恒定。具体数值随种子变化，待本地验证。
5. 注意本实践的简化：真实 PostNorm 的子层输入是未归一化的主干，训练稳定性差，这里只对比"主干幅度"这一个维度，不涉及可训练性。

#### 4.2.5 小练习与答案

**练习 1**：写出 PreNorm 与 PostNorm 单子层的更新式，指出哪一条路径上没有归一化层。

**参考答案**：PreNorm：\( \mathbf{h} \leftarrow \mathbf{h} + \mathrm{Sub}(\mathrm{Norm}(\mathbf{h})) \)，主干（左侧的 \( \mathbf{h} \) 与求和结果）上没有 Norm；PostNorm：\( \mathbf{h} \leftarrow \mathrm{Norm}(\mathbf{h} + \mathrm{Sub}(\mathbf{h})) \)，主干累加结果整体过 Norm。

**练习 2**：为什么 PreNorm 下每个子层输出的"生产规模"大致稳定，而主干却越滚越大？

**参考答案**：子层的输入是 \( \mathrm{Norm}(\mathbf{h}) \)，归一化层把输入幅度拉回固定值，与主干当前多大无关，所以输出 \( \mathbf{v}_l \) 的尺度 \( O(\sigma) \) 基本不随深度变；而主干是 \( l+1 \) 个这样增量的裸加和，量级 \( O(\sqrt{l}\,\sigma) \) 甚至更大。"生产速度恒定，但只进不出"，总量自然无界。

**练习 3**：从 README 伪代码中找出一行证据，说明 AttnRes 的 forward 采用的是 PreNorm 风格组织。

**参考答案**：[L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80) `attn_out = self.attn(self.attn_norm(h))`：`attn_norm` 在注意力入口（先归一化再进子层），主干 `partial_block` 不经过归一化；[L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87) 的 `mlp_norm` 同理。

### 4.3 幅度增长与稀释现象

#### 4.3.1 概念说明

把 4.1 的等权求和与 4.2 的 PreNorm 主干合起来，得到两个孪生问题——它们是**同一个累加式的两种读法**：

**读法一：膨胀（幅度无界增长）。** \( \mathbf{h}_l = \sum_{i=0}^{l} \mathbf{v}_i \) 的项数随深度增加，主干范数随之增长。两种极端给出增长区间：

\[ \text{独立增量：} \|\mathbf{h}_l\| \approx \sqrt{l+1}\,\sigma; \qquad \text{同向增量：} \|\mathbf{h}_l\| \approx (l+1)\,\sigma \]

真实网络介于两者之间（各层输出往往正相关，例如多层持续强化同一语义方向），因此实测曲线常常比 \( \sqrt{l} \) 更陡。幅度大本身不直接等于"错误"，但它迫使后续所有组件（注意力 QK 打分、logits 投影）面对数值越来越大的输入，训练对精度与学习率更敏感。

**读法二：稀释（层贡献占比萎缩）。** 第 \( l \) 个子层刚把自己辛苦算出的 \( \mathbf{v}_l \) 加进主干时，它只占主干的

\[ \frac{\|\mathbf{v}_l\|}{\|\mathbf{h}_l\|} \approx \frac{\sigma}{\sqrt{l+1}\,\sigma} = \frac{1}{\sqrt{l+1}} \quad \text{（独立情形；同向情形为 } \tfrac{1}{l+1} \text{）} \]

深度越大，新层的"嗓门"越小：第 1 层占比约七成时，第 100 层的输出只是主干里百分之一量级的一份子。更深一层看，每个子层读到的输入是 \( \mathrm{Norm}(\mathbf{h}) \)——归一化虽然稳住了幅度，但**信息占比**没变：主干内容被越来越多的历史层"平均"，单层写入的信号在下游读出时占比持续摊薄。这就叫**层贡献稀释**。

**稀释的梯度侧写。** 前向占比被摊薄，反向同样不均：经验上（见下面读图）基线模型各层梯度范数随深度明显衰减——靠近输入的层拿到的梯度大、深层的层拿到的梯度小，意味着深层参数得到的修正信号弱、学习慢。这是 README 说"dilutes each layer's contribution"在训练动态上的体现。（梯度不均的严格推导论文中有展开，仓库未提供，细节待确认。）

**为什么说两个现象是"同一枚硬币"**：都源自 \( \sum 1 \cdot \mathbf{v}_i \) 这个算子——项数越多，求和越大（膨胀），同时每项占比越小（稀释）。所以修复方案必须**同时**改掉"权重恒为 1"这一点：让聚合变成有界的加权组合，且权重可学习、可按内容分配——这正是 AttnRes 公式 \( \mathbf{h}_l = \sum_i \alpha_{i \to l} \mathbf{v}_i \)（softmax 保证 \( \sum_i \alpha = 1 \)）的设计动机。

#### 4.3.2 核心流程

读 `assets/training_dynamics.png` 的流程（三联图，从左到右）：

```text
(a) Validation Loss vs Step        → 先确认"换了残差，损失确实更低"（效果存在）
        ↓
(b) Output Magnitude vs Layer      → 本讲核心：基线幅度随层号爬升；AttnRes 压平
        ↓
(c) Gradient Magnitude vs Layer    → 基线梯度跨层衰减；AttnRes 分布均匀（机理侧证）
```

读图要点：横轴 (b)(c) 都是 **Layer（层号/深度）**，纵轴分别是输出幅度与梯度幅度（(c) 标注 ×10⁻⁵）。比较的对象始终是两条线：**Baseline**（基线，标准残差）与 **Block AttnRes**。这张图是"问题（本讲）→ 解药验证（第 2 单元第 5 讲你将用 hook 亲自复测）"的桥梁。

#### 4.3.3 源码精读

**图的嵌入位置与结论句。**

[README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123) —— Training Dynamics 小节只有一句话加一张图：AttnRes 缓解 PreNorm 稀释——输出幅度跨深度保持有界、梯度范数跨层分布更均匀。

[README.md:L125-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L125-L127) —— 这里用 `<img>` 嵌入 [assets/training_dynamics.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/training_dynamics.png)。逐面板目测读图（数值为目测近似，精确值以论文为准，待确认）：

- **(a) 验证损失 vs Step**（横轴约 20k–100k 步）：两条曲线都从约 1.4+ 单调下降，**Block AttnRes（红线）全程压在 Baseline（蓝线）之下**——换了残差连接方式，损失确实更低，先把"有没有效"钉死。
- **(b) 输出幅度 vs Layer**（横轴约 0–26 层）：**Baseline 从接近 0 一路爬升，末段（约 20 层之后）明显加速，末层目测到 12 上下**——这就是 4.3.1 说的"比 \( \sqrt{l} \) 更陡"的无界增长实锤；**Block AttnRes 全程贴地在 0–2 之间小幅波动**，对应"幅度有界"。这个面板就是本讲主实践要复现的曲线形态。
- **(c) 梯度幅度 vs Layer**（纵轴标注 ×10⁻⁵）：**Baseline 从浅层约 2.4 的峰值沿层号单调衰减到 0.1 量级**——梯度集中在浅层、深层几乎拿不到修正信号；**Block AttnRes 整体平坦**（约 0.1–0.5 间波动），跨层分布均匀得多——对应"稀释被缓解"的梯度侧写。

**回到问题陈述句收束。**

[README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) —— 现在再读这句应能逐词对应实物：uniform aggregation ↔ 4.1 的全 1 权重；grow unboundedly ↔ 图 (b) 蓝线；dilutes each layer's contribution ↔ \( 1/\sqrt{l+1} \) 占比与图 (c) 蓝线的梯度衰减；PreNorm ↔ 4.2 的主干裸奔结构。

#### 4.3.4 代码实践（本讲主实践）

**实践三：12 层 PreNorm 前向骨架 + hook 逐层范数探针**

1. **实践目标**：实现任务书指定的实验——搭建 12 层 PreNorm Transformer 前向骨架（注意力与 MLP 用占位模块），用 **forward hook** 逐层记录隐藏状态的 L2 范数，绘制范数–深度曲线，复现图 (b) 蓝线的增长形态。
2. **操作步骤**：以下为**示例代码**（占位子层用小型 MLP 模拟"入口被归一化、输出规模稳定"的子层），保存为 `prenorm_probe.py`，在 u1-l1 已装好的 PyTorch 环境运行（CPU 即可）：

   ```python
   # prenorm_probe.py —— 12 层 PreNorm 骨架 + 逐层范数探针（示例代码）
   import torch
   import torch.nn as nn
   import torch.nn.functional as F

   torch.manual_seed(0)
   D, T, B, L = 64, 32, 2, 12          # 隐藏维度、序列长度、batch、层数

   class PlaceholderSublayer(nn.Module):
       """占位子层：输入已被归一化，输出幅度大致稳定（模拟 Attn/MLP）。"""
       def __init__(self, d):
           super().__init__()
           self.fc1, self.fc2 = nn.Linear(d, 4 * d), nn.Linear(4 * d, d)
       def forward(self, x):                    # x: [B, T, D]
           return self.fc2(F.gelu(self.fc1(x)))

   class PreNormLayer(nn.Module):
       """PreNorm 层：Norm 在子层入口，主干只做 '+'（对应伪代码 L80-L81、L87-L88）。"""
       def __init__(self, d):
           super().__init__()
           self.attn_norm, self.mlp_norm = nn.LayerNorm(d), nn.LayerNorm(d)
           self.attn, self.mlp = PlaceholderSublayer(d), PlaceholderSublayer(d)
       def forward(self, h):                    # h: [B, T, D]
           h = h + self.attn(self.attn_norm(h))  # 子层入口归一化；主干直接累加
           h = h + self.mlp(self.mlp_norm(h))
           return h

   class MiniPreNorm(nn.Module):
       def __init__(self, d, n_layers):
           super().__init__()
           self.embed = nn.Embedding(100, d)
           self.layers = nn.ModuleList(PreNormLayer(d) for _ in range(n_layers))
       def forward(self, ids):
           h = self.embed(ids)
           for layer in self.layers:
               h = layer(h)
           return h

   model, norms = MiniPreNorm(D, L), []
   def make_hook(name):                          # 探针：不动模型代码，只观测
       def hook(module, inputs, output):
           norms.append((name, output.norm(dim=-1).mean().item()))
       return hook

   model.embed.register_forward_hook(make_hook("embedding"))      # 深度 0
   for i, layer in enumerate(model.layers, start=1):             # 深度 1..12
       layer.register_forward_hook(make_hook(f"layer {i:02d}"))

   model(torch.randint(0, 100, (B, T)))
   for name, n in norms:
       print(f"{name:>10s}  mean-token-n2norm = {n:8.3f}")
   ```

   绘图（可选，需 `pip install matplotlib`）：把 `norms` 的序号作 x、范数作 y，`plt.plot(...); plt.xlabel("depth (0 = embedding)"); plt.ylabel("mean token L2 norm")`，保存为 `norm_vs_depth.png`。
3. **需要观察的现象**：13 行打印（embedding + 12 层）中范数随深度**单调上升**；增长形态介于 \( \sqrt{l} \) 与线性 \( l \) 之间（占位子层输出与主干存在相关性，通常偏线性一侧）；末层范数达到嵌入层的好几倍。
4. **预期结果**：曲线形态与 `training_dynamics.png` 面板 (b) 的蓝线（Baseline）定性一致——随层号持续爬升、无饱和迹象。随机初始化下具体数值随种子与维度变化，**待本地验证**；但"PreNorm 主干无归一化 ⇒ 范数单调增长"这一趋势由结构决定，是确定性的。
5. 常见问题：若 hook 打印顺序乱，检查是否对 `self.layers` 整体注册了 hook（应对每个子层分别注册）；范数应按 `dim=-1` 对每个 token 的 D 维向量计算后再对 token/batch 求均值，而不是对整个张量求范数。

#### 4.3.5 小练习与答案

**练习 1**：独立增量假设下，12 个子层输出的主干范数约是单层输出的多少倍？若各层输出完全同向呢？

**参考答案**：独立情形 \( \|\mathbf{h}\| \approx \sqrt{12+1}\,(\text{嵌入也计入}) \approx 3.6 \) 倍；同向情形 \( \approx 13 \) 倍。真实网络介于两者之间且往往偏大——图 (b) 蓝线末段加速说明各层增量明显正相关。

**练习 2**：只看 `training_dynamics.png` 的面板 (c)，如何向同学解释"稀释"在梯度上的表现？

**参考答案**：Baseline 的梯度范数从浅层约 2.4（×10⁻⁵）沿层号单调衰减到约 0.1，说明反向修正信号集中在浅层、深层拿到的梯度小——深层子层写入主干的贡献本就被摊薄，训练它的信号也弱，这是前向稀释在反向的镜像。Block AttnRes 的曲线平坦（约 0.1–0.5），梯度跨层分配均匀，说明稀释被缓解。（数值为目测近似，精确值以论文为准，待确认。）

**练习 3**：为什么说"膨胀"和"稀释"不可能只修一个？

**参考答案**：它们都来自同一个算子 \( \sum_i 1 \cdot \mathbf{v}_i \)：项数增加必然让和变大（膨胀）、同时每项占比变小（稀释），是同一事实的幅度视角与占比视角。任何只在主干上加缩放（例如除以 \( l \)）的方案，虽然压住幅度，却进一步均匀化了权重、不解决"按内容选择"的问题；只有把权重本身变成可学习、按输入分配的量（softmax 保证 \( \sum_i \alpha_{i \to l} = 1 \)，输出天然是凸组合、幅度有界），两个问题才同时被触及——这就是下一讲的出发点。

## 5. 综合实践

**综合任务：产出一份《PreNorm 幅度与稀释诊断报告》**，把本讲三个模块的实验串成一张对照 [training_dynamics.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/training_dynamics.png) 面板 (b) 的自制证据图：

1. **双探针**：扩展实践三的骨架，除主干范数外，再在每个子层累加处记录相对贡献 \( \|\mathbf{v}_l\| / \|\mathbf{h}_l\| \)（提示：把 `PreNormLayer.forward` 里的两次 `+` 拆开，暂存 `v = self.attn(self.attn_norm(h))` 后先记录 `v.norm(dim=-1).mean()` 与 `h.norm(dim=-1).mean()` 再累加）。
2. **双曲线**：画一张双子图——左图「主干范数 vs 深度」（对应图 (b) 蓝线），右图「相对贡献 vs 深度」（稀释的量化曲线，应随深度下降）。
3. **加对照**：把实践二的 PostNorm 式主干（累加后 `rms_norm`）作为第三条曲线加入左图，观察它幅度恒定但有均匀化副作用。
4. **写结论**：用三句话把观察映射回 README——(i) 哪些现象对应 [L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) 的 "grow unboundedly"；(ii) 哪些对应 "dilutes each layer's contribution"；(iii) 一个合格的替代算子必须同时满足哪两个条件（幅度有界 + 权重可按内容分配）。

预期：左图 PreNorm 曲线单调上升、PostNorm 曲线走平；右图占比曲线单调下降。具体数值待本地验证。

## 6. 本讲小结

- 标准残差的递推 \( \mathbf{h}_l = \mathbf{h}_{l-1} + \mathbf{v}_l \) 可展开为 \( \mathbf{h}_L = \sum_{i=0}^{L} 1 \cdot \mathbf{v}_i \)——**权重恒为 1、与输入无关、无缩放**；README 伪代码 [L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81)/[L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L88) 的两行 `+` 就是它，且在 Block AttnRes 中被保留为**块内**累加方式。
- **PreNorm** 把 Norm 放在子层入口（[L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)/[L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87)），主干上没有任何缩放：子层输出规模稳定（入口被归一化）但主干只加不减，幅度以 \( O(\sqrt{l}\,\sigma) \sim O(l\sigma) \) **无界增长**。
- **膨胀与稀释是同一枚硬币**：项数越多求和越大（幅度膨胀），单层占比 \( \approx 1/\sqrt{l+1} \sim 1/(l+1) \) 越小（贡献稀释），梯度也随之跨层分布不均。
- `training_dynamics.png` 三联图给出实证：Baseline 输出幅度随层爬升至约 12、梯度范数从约 2.4 衰减到约 0.1；Block AttnRes 幅度贴地（0–2）、梯度平坦——与 [L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L123) 的结论逐词对应（数值为目测，精确值以论文为准）。
- 主实践用 12 层 PreNorm 骨架 + forward hook 复现了"范数–深度"增长曲线：不改动模型代码、只插探针，是后续所有训练动态分析（第 2 单元第 5 讲）的方法雏形。
- 修复方向的预告：把全 1 权重换成 softmax 权重 \( \alpha_{i \to l} \)（凸组合保证有界、可学习保证选择性）——下一讲正式展开。

## 7. 下一步学习建议

下一讲（`u1-l3-attnres-core-idea.md`：核心思想：用跨深度注意力替换固定累加）将给出本讲问题的"解药"。建议：

- 先自己重读 [README.md:L39-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L39-L43)，带着两个问题读：① softmax 为什么天然解决"幅度有界"？② 伪查询 \( \mathbf{w}_l \in \mathbb{R}^d \) 和普通注意力的 query 有什么不同？
- 把综合实践里你画出的"范数–深度"曲线保存好——第 2 单元第 5 讲你会给 Block AttnRes 版骨架装上同样的探针，两条曲线叠在一起就是你自己复现的 `training_dynamics.png` 面板 (b)。
- 想提前热身的读者可以想一想：实践三里如果给主干每次累加后乘一个 0.5（衰减式残差），幅度会有界吗？能解决"按内容选择"吗？（答案：幅度有界，但权重仍与输入无关——这正是 AttnRes 与一切固定系数方案的分界线。）
