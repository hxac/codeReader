# 训练动态：幅度有界与梯度均衡的验证

## 1. 本讲目标

u2-l4 完成了第一次真正的对比训练：两条验证损失曲线告诉我们**哪个模型更好**，却没告诉我们**为什么**。损失是「结果」层面的证据；本讲要打开模型内部，在训练过程中直接测量两个结构性量——**每层表示的幅度**与**每层参数梯度的范数**。它们正是 README 训练动态一节两句定性主张的对象：

> [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123)
> `### Training Dynamics` — AttnRes mitigates PreNorm dilution: output magnitudes remain bounded across depth and gradient norms distribute more uniformly across layers.

这句话配上 [assets/training_dynamics.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/training_dynamics.png) 的三联图，就是本讲要迷你复现的对象。

学完本讲，你应该能够：

1. 把「幅度有界」「梯度更均匀」翻译成**可测量、可证伪**的指标：深度剖面、增长比 R、变异系数 CV；
2. 熟练使用 PyTorch 的 hook 工具箱——模块前向 pre-hook、参数张量梯度 hook——并知道 `register_full_backward_hook` 为什么在本实验台上不可靠；
3. 在 u2-l4 的实验台上对 Standard 与 Block AttnRes 各测出三种剖面（层间状态 P1、子层输入 P2、参数梯度 P3），绘出论文三联图的迷你版；
4. 按照「登记预测 → 测量 → 对照 → 如实报告」的流程下结论：差异落在噪声以内时，如实写「本规模无显著差异」。

本讲**不新增任何模型代码**——所有测量都架在 u2-l4 的 `minitest.py` 之上；新增的只有一件「仪器」：`Probe` 探针类（4.2 节）。

## 2. 前置知识

### 2.1 训练动态：深度轴上的剖面，而不是步数轴上的曲线

「训练动态」（training dynamics）容易和「训练曲线」混淆，先分清：

| | 横轴 | 每个点回答的问题 | 已在哪里见过 |
|:---|:---:|:---|:---|
| 训练曲线 | 训练步数 | 训练到第 s 步，损失降到多少？ | u2-l4 的 `compare_val_loss.png` |
| 训练动态 | **深度（层号 l）** | 在某个训练时刻，第 0…L−1 层各自把表示/梯度维持在什么尺度？ | 本讲 + 论文三联图 |

做法是：在选定的几个训练时刻（如第 0、500、3000 步），各做一次「深度剖面」测量，再把这些剖面按时刻叠画——得到一张「深度 × 训练阶段」的二维图景。**损失曲线告诉你结果，深度剖面告诉你机理。**

为什么两种残差方案的差异恰恰会显形在深度轴上？这正是前几讲的结论链：

- u1-l2：标准残差主干是全 1 权重的累加，进入第 l 层的状态 = 嵌入 + \(2l\) 个子层输出，**项数随深度线性增加** → 幅度结构性膨胀、单层贡献被 \(1/\sqrt{l+1}\) 式稀释；
- u1-l3：AttnRes 用 softmax 凸组合聚合历史，输出范数被最大候选封顶——但那只是**初始化状态下、单次前向**的静态性质；
- u2-l4：两种模型已经可以在同一实验台上公平训练。

本讲要做的，就是把「凸组合 ⇒ 有界」这条数学上成立的性质放到**真实训练过程中**检验，并补上静态分析覆盖不到的另一半主张：梯度在各层之间是否更均匀。

### 2.2 hook：不侵入模型的观测仪器

hook（钩子）是 PyTorch 提供的「旁路观测点」：注册一个回调函数，框架在特定时机调用它，主计算完全不感知。写得好，hook 不改变任何计算结果——这正是「观测不扰动系统」的仪器要求。本讲用到与避开的挂载点如下：

| 挂载点 | 注册方法 | 触发时机 | 本讲用途 | 生命周期 |
|:---|:---|:---|:---|:---|
| 模块 forward 之前 | `module.register_forward_pre_hook(h)` | 每次前向、`forward` 执行前 | 读层间状态（P1）、子层输入（P2） | 随模块持久 |
| 模块 forward 之后 | `module.register_forward_hook(h)` | `forward` 执行后 | 备选（本讲不用） | 随模块持久 |
| 张量梯度 | `tensor.register_hook(h)` | backward 中算出该张量的梯度时 | 读参数梯度（P3） | 挂在**参数**上持久；挂在中间激活上只活当次前向 |
| 模块全量反向 | `module.register_full_backward_hook(h)` | 算出模块输出梯度时 | ⚠ 本实验台不可靠，见 4.2 | — |

三条使用须知：

1. **生命周期**：挂在模块和参数上的 hook 注册一次、长期有效；挂在中间激活张量（如某层输出）上的 hook 随该张量的计算图一起消亡，每次前向都要重挂——本讲因此只挂前两类。
2. **观测成本**：hook 里调用 `.item()` / `float()` 会触发 GPU→CPU 同步。把探针做成**可开关**的（只在测量步打开），平时训练零开销。
3. **别扰动梯度**：张量梯度 hook 允许返回新梯度替换原值；纯观测时返回 `None`（保持原梯度）即可。

u1-l2 已经用 forward hook 在**未训练的占位骨架**上看过幅度增长；本讲把它系统化：换上 u2-l4 的真实实验台、补上梯度侧、再加上训练阶段维度。

### 2.3 三个度量指标

对张量 \(x\)（形状 [B, T, D]）定义均方根：

\[ \mathrm{rms}(x) = \sqrt{\tfrac{1}{|x|}\textstyle\sum_j x_j^2} \]

它可解释为「平均每个分量的尺度」，且对固定形状与整体 L2 范数 \(\|x\|_2=\sqrt{|x|}\,\mathrm{rms}(x)\) 单调等价——两种模型形状完全相同，用哪个口径结论一致，rms 的数值更好读。

- **增长比**（幅度有界性的标量摘要）：\(R = \mathrm{rms}(\text{最深层}) / \mathrm{rms}(\text{第 0 层})\)。R ≈ 1 表示深度方向平稳；R 随训练变大表示膨胀在发展。
- **变异系数**（梯度均匀性的标量摘要）：对逐层梯度范数序列 \(\{g_l\}\)，

\[ \mathrm{CV} = \frac{\mathrm{std}(\{g_l\})}{\mathrm{mean}(\{g_l\})} \]

CV 越小，各层梯度尺度越接近「均匀」。辅助再报一个 max/min 比，对极端层最直观。

### 2.4 本讲配置（沿用 u2-l4）

| 配置 | d | L | block_size | k=block_size//2 | 用途 |
|:---|:---:|:---:|:---:|:---:|:---|
| 冒烟 | 64 | 8 | 4 | 2 | 4.2/4.3/4.4 的分模块实践，几分钟跑完 |
| 对比 | 256 | 16 | 4 | 2 | 综合实践，剖面深度轴 0…15 |

所有模型代码、数据设施（`build_corpus` / `get_batch` / `evaluate` / `train`）均来自 u2-l4，本讲直接 `import` 或复制进 `minitest.py` 后续写。

## 3. 本讲源码地图

本仓库是论文发布仓库，没有任何工程代码（u1-l1 已确认）。本讲的「源码」是 README 的主张原文与伪代码；实验台代码来自 u2-l4 讲义（读者本地的 `minitest.py`），`Probe` 等新代码均为本讲义编写的「示例代码」。

| 位置 | 内容 | 本讲用途 |
|:---|:---|:---|
| [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) | 问题陈述：固定单位权重累加 → 稀释 + 幅度无界增长 | 4.1：反面命题的出处 |
| [README.md:L39-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L39-L43) | AttnRes 公式与伪查询 | 4.1：正面机制的出处 |
| [README.md:L61-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L64) | `softmax(0)` 凸组合两行 einsum | 4.1/4.3：有界性的代码根据 |
| [README.md:L67-L68](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L68) | 层签名与 `partial_block = hidden_states` | 4.2/4.3：P1 挂载点与语义 |
| [README.md:L75-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L75-L77) | 块边界封存、partial 清空 | 4.3：锯齿剖面的结构性来源 |
| [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80) | `attn_out = self.attn(self.attn_norm(h))` | 4.2/4.4：P2 挂载点、P3 参数所属子层 |
| [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123) | 本讲要验证的两句主张 | 全讲 |
| [assets/training_dynamics.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/training_dynamics.png) | 论文训练动态三联图 | 4.1：对照目标 |
| `Attention_Residuals.pdf` | 论文全文 | 图表口径（测的是什么梯度、哪个训练时刻）以论文为准（待确认） |
| `Attention-Residuals-tutorial/u2-l4-minimal-testbed-training.md` | 实验台：`MiniGPT`/`Block`/`train`/`evaluate` | 4.2–4.4、5 的代码基座 |

## 4. 核心概念与源码讲解

四个最小模块按「先立命题、再造仪器、然后两侧测量」展开：

1. **4.1 训练动态分析**——把 README 的两句定性主张翻译成两条可证伪的测量命题（M1/M2），并说明机理；
2. **4.2 hook 工具**——造出同时记录三种剖面的 `Probe` 探针；
3. **4.3 隐藏状态范数统计**——测幅度：P1 锯齿与 P2 有界；
4. **4.4 梯度范数统计**——测梯度：P3 剖面与 CV 均匀性。

### 4.1 训练动态分析：把定性主张变成可证伪命题

#### 4.1.1 概念说明

[README.md:L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L123) 一句含两个主张，各自翻译成测量命题：

**M1（幅度有界）**。README 说的是「output magnitudes remain bounded across depth」，而 [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) 把同一现象叫「hidden-state magnitudes grow unboundedly」。口径上我们取**子层读到的表示 h**（即每层注意力入口处的主干状态）作为探测对象——残差结构的差异恰恰发生在「前面各层的输出如何汇流成这一层读到的 h」，v_l 本身（PreNorm 子层输出）在两种方案下尺度都可能稳定，看不出差异。于是：

- **baseline 预测**：\(\mathrm{rms}(h_l)\) 随 l 单调上升，\(R \gg 1\)，且随训练进行（子层输出长大）越涨越高；
- **attnres 预测**：\(\mathrm{rms}(h_l)\) 平稳有界（允许轻微台阶/起伏），\(R \approx 1\)，不随训练单调抬升。

**M2（梯度均匀）**。以「每层 `qkv` 权重梯度的范数 \(g_l\)」为探测对象：

- **baseline 预测**：\(\{g_l\}\) 跨层系统性倾斜（偏向哪一端因配置而异，以实测为准），CV 偏大；
- **attnres 预测**：\(\{g_l\}\) 更平坦，CV 更小。

**机理上为什么值得期待这两个结果？**

- M1 的根据是 u1-l3 的凸组合上界，这里复述成一行：\(\alpha=\mathrm{softmax}\ge 0\)、\(\sum_n\alpha_n=1\)，故
  \[ \|\mathbf{h}\| = \Big\|\sum_n \alpha_n \mathbf{V}_n\Big\| \le \sum_n \alpha_n\,\|\mathbf{V}_n\| \le \max_n \|\mathbf{V}_n\| \]
  再加上 Block 版的候选本身只有 \(2k\) 个子层输出（k 层 × 每层 attn+MLP 两个，见 [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 的分块），项数被封顶、与总深度 L 无关。对照 baseline：项数 \(= 2l+1\) 随深度线性增长。**把「幅度」拆成「项数 × 单项尺度」，两种方案的单项尺度可比，差别全在项数——一个线性增长、一个封顶。**
- M2 与 M1 相连。以线性层 \(y = xW^\top\) 为例，\(\partial\mathcal{L}/\partial W = \delta^\top x\)（\(\delta\) 是上游传回的输出梯度），范数大致随 \(\|\delta\|\cdot\|x\|\) 走。PreNorm 让 \(\|x\|\)（归一化后的输入）跨层近常数，因此 P3 剖面的倾斜主要读出 **\(\delta\) 的跨层分布**：baseline 中每层输出的「相对话语权」按 \(1/\sqrt{l+1}\) 递减（u1-l2 的稀释），梯度分布随之倾斜；attnres 中每层经学得的 \(\alpha\) 获得一条不随深度衰减的发声通路（[README.md:L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43)：every layer selective, content-aware access），前向尺度又深度无关，反向信号更可能保持同量级。——这是与论文主张一致的**定性机理阐释**，严格分析以 `Attention_Residuals.pdf` 为准（待确认）。

#### 4.1.2 核心流程

本讲的方法论是「预注册」（先把预测写死，再测量，防止事后找说法）：

```text
① 登记预测: M1/M2 两张对照表(baseline vs attnres 各自的预期形态与指标方向)
② 造仪器:   Probe 探针(4.2), P1/P2/P3 三种剖面一次装好
③ 定点测量: 在训练第 {0, 500, 1500, 3000} 步各做一次快照(固定测量批)
④ 对照判读: 实测剖面 vs 登记预测; R 与 CV 两个标量定量化
⑤ 如实报告: 支持就说支持; 不显著就写「本规模无显著差异」; 反向则先查实现
证伪判据: attnres 幅度剖面单调上升且 R 与 baseline 同量级, 或 CV 反而更大
         → 先查实现(候选数探针/探针挂点/测量步不配对), 再考虑「本规模不显现」
```

#### 4.1.3 源码精读

**问题的原文（反面命题）**：

> [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37)
> Standard residual connections accumulate all layer outputs with fixed unit weights. As depth grows, this uniform aggregation dilutes each layer's contribution and causes hidden-state magnitudes to grow unboundedly — a well-known problem with PreNorm.

这句同时给出 M1 的两个关键词：*dilutes each layer's contribution*（M2 的根源）与 *magnitudes grow unboundedly*（M1 的 baseline 预测）。

**机制的原文（正面命题）**：

> [README.md:L39-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L39-L43)
> **AttnRes** replaces this fixed accumulation with softmax attention over preceding layer outputs: \(\mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \cdot \mathbf{v}_i\) … where the weights are computed via a single learned pseudo-query \(\mathbf{w}_l \in \mathbb{R}^d\) per layer.

「replaces this fixed accumulation」——被替换的正是 L37 那个累加，所以两句话一体两面，测量也应在同一探测点上对照。

**凸组合的代码落点（有界性的根据）**：

> [README.md:L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L64)
> `h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)`

`softmax(0)` 沿深度归一化（u2-l1 精读）⇒ 权重非负、和为 1 ⇒ h 落在候选凸包内 ⇒ \(\|\mathbf{h}\|\le\max_n\|\mathbf{V}_n\|\)，与深度无关。

**要验证的主张原文**：

> [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123)
> ### Training Dynamics
> AttnRes mitigates PreNorm dilution: output magnitudes remain bounded across depth and gradient norms distribute more uniformly across layers.

**对照目标三联图**：[assets/training_dynamics.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/training_dynamics.png) 共三幅子图，Baseline 与 Block AttnRes 两条曲线同图对比；打开原图重点看两件事——幅度子图中 Baseline 随深度单调攀升、Block AttnRes 保持在有界带内；梯度相关子图中 Baseline 跨层分布倾斜、Block AttnRes 更平。各子图的确切口径（对哪个量取范数、参数梯度还是激活梯度、取自哪个训练时刻）README 未标注，以图片标注与论文为准（待确认）。

#### 4.1.4 代码实践：先把「上界」从推导变成数值事实

1. **实践目标**：在动手测模型之前，用随机张量验证 M1 的理论基石——softmax 凸组合的范数上界与深度归一化。这是一个确定性检查，不依赖训练。
2. **操作步骤**：

```python
# 示例代码
import torch

torch.manual_seed(0)
V = torch.randn(5, 2, 4, 32)          # [N+1, B, T, D]: 模拟 5 个块级候选
logits = torch.randn(5, 2, 4)         # 逐 token 的深度 logits
alpha = logits.softmax(0)             # 沿深度归一化, u2-l1 的 L64
h = torch.einsum('nbt,nbtd->btd', alpha, V)

# 检查 1: 每个 (b,t) 位置上, h 的范数不超过最大候选的范数
bound = V.norm(dim=-1).amax(dim=0)    # [B, T]: max_n ||V_n||
print("上界成立:", bool((h.norm(dim=-1) <= bound + 1e-5).all()))

# 检查 2: 权重沿深度和为 1(凸组合的前提)
print("alpha 沿深度求和(应全为 1):", alpha.sum(0).min().item(),
      alpha.sum(0).max().item())

# 检查 3: 对照「全 1 权重」——同样 5 个候选直接求和, 范数随候选数增长
sum_all = V.sum(0)
print("凸组合 rms =", f"{h.pow(2).mean().sqrt():.4f}",
      " 求和 rms =", f"{sum_all.pow(2).mean().sqrt():.4f}")
```

3. **需要观察的现象**：`上界成立` 打印 `True`；alpha 沿深度求和最小、最大都为 1.0；凸组合的 rms 明显小于直接求和的 rms（随机方向下求和约按 \(\sqrt{N+1}\) 放大）。
4. **预期结果**：前两条是数学必然而非随机事件，应严格成立（浮点容差内）；第三条约 \(\sqrt{5}\approx 2.2\) 倍。这就是 M1 的全部理论：**baseline 把候选求和，attnres 把候选凸组合**——训练只是让这一差别带上真实的尺度。

#### 4.1.5 小练习与答案

**练习 1**：用三角不等式证明 \(\|\sum_n \alpha_n \mathbf{V}_n\| \le \max_n \|\mathbf{V}_n\|\)（\(\alpha\) 非负、和为 1）。

> **答案**：\(\|\sum_n \alpha_n \mathbf{V}_n\| \le \sum_n \alpha_n \|\mathbf{V}_n\|\)（三角不等式）；\(\sum_n \alpha_n \|\mathbf{V}_n\|\) 是各 \(\|\mathbf{V}_n\|\) 的凸组合（\(\alpha\) 非负、和为 1），故不超过其最大值 \(\max_n\|\mathbf{V}_n\|\)。两步串联即得。

**练习 2**：M1/M2 分别怎样才算被「证伪」？写出判据。

> **答案**：M1 被证伪：attnres 的 P2 剖面呈单调上升、R 与 baseline 同量级（且排除实现 bug——用 u2-l4 的候选数探针确认调度正确）。M2 被证伪：同一步、同一测量批下 \(\mathrm{CV}_{attnres} \ge \mathrm{CV}_{baseline}\) 且差距超出种子方差。注意「未证伪」≠「证实」：小规模结果只能给定性支持，不能外推到 48B（README 的证据规模见 [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)）。

**练习 3**：为什么探测对象选「子层读到的 h」，而不是各子层的输出 v_l 或逐层参数量？

> **答案**：残差方案的差异发生在「历史输出如何汇流成下一层输入」。PreNorm 下子层输入先被归一化、输出尺度由子层自身决定，v_l 在两种方案下都未必有系统性差异；而 h 在 baseline 是不断累加的主干、在 attnres 是凸组合——差异被结构放大。参数量则是静态量（u2-l3 已算清 4d/层），与训练动态无关。

### 4.2 hook 工具：三类可用挂载点与一个坑

#### 4.2.1 概念说明

本模块造仪器：一个 `Probe` 类，安装一次、长期在位，但只在「测量步」打开，平时对训练零干扰。它同时记录三种剖面：

- **P1（层间状态）**：挂在每个 `Block` 上的 forward pre-hook，读 `Block` 的第二个输入 `hidden_states`——即上一层返回的 partial（attnres）或残差流（baseline）。它是「深度方向的血液流动」最直接的读数。
- **P2（子层输入）**：挂在每层 `attn_norm` 上的 forward pre-hook，读其输入 `h`——这正是 M1 的探测对象（4.1.1 的口径）。
- **P3（参数梯度）**：挂在每层 `attn.qkv.weight` 这个**参数张量**上的梯度 hook——M2 的探测对象。

**为什么不用 `register_full_backward_hook` 挂在 `Block` 上？** 该接口面向「输出为张量（或张量元组）」的模块；而 `Block.forward` 返回 `(blocks 列表, partial 张量)`（[README.md:L67](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67)），Python 列表不参与自动微分、没有对应的 grad_output，这条挂载路径在本实验台上不可靠（具体行为随 PyTorch 版本而异，可能不触发或告警，待本地验证）。把梯度探针挂到参数张量上是最稳的写法：参数每次 backward 必然算梯度。另一个容易混淆的点：即使挂在 `self.attn`（输出是张量，可行）上，`grad_output` 是**输出激活的梯度**，与**参数梯度**是两个口径——记录时必须写清口径，本讲 P3 取参数梯度。

**两个工程细节**：其一，backward 沿反拓扑序执行，**最后一层的梯度先算**——梯度记录必须带上层号（闭包捕获），绝不能依赖 append 顺序；其二，Python 闭包的迟绑定陷阱——循环里直接 `lambda` 捕获的循环变量会在调用时取终值，必须用「闭包工厂」把层号固化。

#### 4.2.2 核心流程

```text
安装(一次):
    for l, layer in enumerate(model.layers):
        layer               ← register_forward_pre_hook → P1[l] = rms(args[1])
        layer.attn.attn_norm ← register_forward_pre_hook → P2[l] = rms(args[0])
        layer.attn.qkv.weight ← register_hook          → P3[l] = rms(grad)
        (所有句柄存起来, 用完 remove)

测量(每步可选):
    probe.P1 = probe.P2 = probe.P3 = {}   # 清空
    probe.active = True
    ① no_grad 前向(固定批) → 记录 P1/P2
    ② zero_grad → 前向+loss.backward()   → 记录 P3 → zero_grad(测量梯度不留给优化器)
    probe.active = False
读取: P?{l} 按层号排好即深度剖面; 用完 remove() 卸载
```

#### 4.2.3 源码精读

**P2 挂载点的语义依据**——子层的入口：

> [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)
> `attn_out = self.attn(self.attn_norm(h))`

`attn_norm` 的输入恰是「注意力子层读到的 h」；这一行在 standard 与 attnres 两种模式下逐字相同（u2-l4 的对应表），所以挂在 `attn_norm` 上的一次探针在两种模型下语义严格一致——这是公平测量的结构保证。P3 选 `qkv.weight` 也是因为它属于这一行的 `self.attn`：每层恰一个、形状跨层一致、范数可比。

**P1 挂载点与语义依据**——层间状态的来源：

> [README.md:L67-L68](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L68)
> `def forward(self, blocks: list[Tensor], hidden_states: Tensor) -> tuple[list[Tensor], Tensor]:` / `partial_block = hidden_states`

pre-hook 收到的 `args` 是位置参数元组 `(blocks, hidden_states)`，因此 `args[1]` 就是层间状态：baseline 模式下它是是上一层累加后的残差流，attnres 模式下它是当前块的部分和——同一条探针、两种语义，4.3 会看到它们形态迥异。

**为什么 full backward hook 无从挂起**——层返回值的类型（[README.md:L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L90) `return blocks, partial_block`）：`blocks` 是 list，autograd 对它没有梯度定义，模块级反向 hook 拿不到完整 grad_output——梯度观测只能下沉到张量级。

#### 4.2.4 代码实践：Probe 仪器 + 触发顺序验证

1. **实践目标**：实现完整的 `Probe` 类（本讲核心代码资产，4.3/4.4/5 直接复用）；并用一次前向 + 一次 backward 验证三件结构性事实：forward hook 按层序 0…L−1 触发；梯度 hook 按 L−1…0 触发（反向）；baseline 模式下 P1 与 P2 逐层相等（h 就是残差流本身）。
2. **操作步骤**：

```python
# 示例代码: 观测仪器 (接 u2-l4 minitest.py 的类定义)
import torch

class Probe:
    """P1 层间状态 / P2 子层输入 / P3 参数梯度, 三剖面一次装好, 可开关."""
    def __init__(self, model):
        self.active = False                     # 只在测量步打开, 平时零开销
        self.P1, self.P2, self.P3 = {}, {}, {}
        self._handles = []
        for l, layer in enumerate(model.layers):
            self._handles.append(layer.register_forward_pre_hook(
                self._mk_fwd(self.P1, l, idx=1)))          # Block(blocks, hidden_states)
            self._handles.append(layer.attn.attn_norm.register_forward_pre_hook(
                self._mk_fwd(self.P2, l, idx=0)))          # attn_norm(h)
            self._handles.append(layer.attn.qkv.weight.register_hook(
                self._mk_grad(l)))

    @staticmethod
    def _rms(x):
        return float(x.detach().float().pow(2).mean().sqrt())

    def _mk_fwd(self, store, l, idx):
        # 闭包工厂: 把层号 l 与参数位 idx 固化进闭包, 避开迟绑定
        def hook(module, args):
            if self.active:
                store[l] = Probe._rms(args[idx])
        return hook

    def _mk_grad(self, l):
        def hook(grad):                          # grad: 与 qkv.weight 同形
            if self.active:
                self.P3[l] = Probe._rms(grad)
            return None                          # 纯观测: 返回 None 保持原梯度
        return hook

    def remove(self):
        for h in self._handles:
            h.remove()

# ---- 热身: 验证触发顺序 (确定性结构事实, 与数值无关) ----
torch.manual_seed(0)
m = MiniGPT(V, d=64, n_head=4, n_layer=8, block_size=4,
            mode='attnres', t_max=128)
probe = Probe(m)
probe.P1, probe.P2, probe.P3 = {}, {}, {}
probe.active = True
x, y = get_batch(train_ids, block=128, batch=4)

import collections
fwd_order = []                                   # 另挂一个只记顺序的探针
def seq_hook(module, args):
    fwd_order.append(len(fwd_order))
h_extra = m.layers[0].register_forward_pre_hook(seq_hook)

with torch.no_grad():
    m(x)                                         # 只触发 P1/P2
print("P2 记录层数:", len(probe.P2), " (应为 8)")

m.zero_grad(set_to_none=True)
_, loss = m(x, y)
loss.backward()                                  # 触发 P3 (反向)
print("P3 记录层数:", len(probe.P3), " (应为 8)")
probe.active = False
h_extra.remove(); m.zero_grad(set_to_none=True)

# baseline 模式下 P1 与 P2 应逐层相等 (h 就是残差流)
torch.manual_seed(0)
mb = MiniGPT(V, d=64, n_head=4, n_layer=8, block_size=4,
             mode='standard', t_max=128)
pb = Probe(mb); pb.active = True; pb.P1 = pb.P2 = {}
with torch.no_grad():
    mb(x)
pb.active = False
print("baseline P1==P2 逐层:",
      all(abs(pb.P1[l] - pb.P2[l]) < 1e-6 for l in range(8)))
```

3. **需要观察的现象**：P2/P3 各记录 8 层；baseline 的 P1 与 P2 逐层相等（同一张量的两次读数）。再想验证「梯度先算最后一层」：在 `Probe` 里临时给 `_mk_grad` 加一行 `print(l, end=' ')`，反向时会打印 `7 6 5 4 3 2 1 0`（前向则按 0…7）。
4. **预期结果**：`P1==P2` 为 `True` 是结构必然（standard 模式 `h = partial`，见 u2-l4 的对应表）；层数检查由模块结构决定，应严格通过。顺序观察的具体打印形式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么梯度探针必须用闭包捕获层号，而不能按「记录先后」当作深度顺序？

> **答案**：backward 沿计算图的反拓扑序执行，最后一层的参数梯度先被计算、最早触发 hook。若靠 append 顺序，梯度剖面会被整个倒序，得出「梯度集中在前层」的镜像假象。forward hook 恰好按执行序（0…L−1）触发——两个方向的顺序都不可依赖直觉，必须显式携带层号。

**练习 2**：把观测 hook 挂在 `nn.Parameter` 上与挂在中间激活张量上，生命周期有什么区别？

> **答案**：参数对象在模型存续期间不变，hook 注册一次、每次 backward 都触发；中间激活张量属于某一次前向的计算图，图在 backward 后释放，hook 一次性失效、每次前向要重挂。本讲 P3 挂参数、P1/P2 挂模块，都属于「装一次用到底」；若要测 ∂L/∂h 这类激活梯度，就得在前向时现挂（见 5.5 进阶）。

**练习 3**：hook 里为什么要 `detach()` 之后才计算 rms？不 detach 会怎样？

> **答案**：测量用的均方根若在带 `requires_grad` 的张量上计算，会在训练计算图上额外挂出观测节点（浪费内存，极端情况下还影响反向）；`detach()` 把观测与自动微分隔离。另外在测量 pass 里我们对 P1/P2 用 `torch.no_grad()` 前向，本身就是同样的隔离思想。

### 4.3 隐藏状态范数统计：P1 锯齿与 P2 有界

#### 4.3.1 概念说明

有了仪器，先测幅度（M1）。两个探测点给出**互补**的两幅图景：

| 探测点 | baseline 语义 | attnres 语义 | 预期形态 |
|:---|:---|:---|:---|
| P1 层间状态 | 残差流 = 嵌入 + \(2l\) 个子层输出，项数随 l 线性增 | 当前块部分和，项数封顶 \(2k\)（k=2 即最多 4 项），每过 k 层清零重起 | baseline：单调上升；attnres：**周期为 k 的锯齿**，包络不随深度抬升 |
| P2 子层输入 | 与 P1 相同（h = 残差流，4.2 已证逐层相等） | 候选的凸组合 \(\sum_n\alpha_n V_n\) | baseline：单调上升；attnres：**有界、相对平滑**（允许轻微台阶），\(\le\max_n\|V_n\|\) |

attnres 的 P1 锯齿从哪来？[README.md:L75-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L75-L77) 的边界封存：每 k 层把攒满的部分和封进 `blocks`、`partial` 清空从零重攒。于是部分和的「满度」在层间周期波动——k=2 时进入偶数层前是 4 项（局部峰）、进入奇数层前是 2 项（局部谷）。**关键在包络**：无论 L 多大，峰的高度只由「2k 个子层输出」决定，不存在随深度线性增长的项数——baseline 的结构性膨胀因子被移除了。

P2 则把锯齿平滑掉：它是所有候选的加权平均，权重 softmax 归一。理论上界是最大候选的范数；实测中各候选尺度相近时，P2 大致落在候选尺度的「平均带」内。

还有两个**免费的结构校验**（不需要任何额外代码）：

1. **同一起点**：两种模型第 0 层的 P2 应相等——baseline 读嵌入，attnres 在空候选下 `block_attn_res` 退化为恒等（u2-l4 实践 (ii) 已证），读的也是嵌入；而两种模型的嵌入初始化相同（构建时最先消耗随机数）。曲线应从同一点出发，分岔在后。
2. **baseline 双线重合**：P1 与 P2 逐层相等（4.2.4 已验证）——若不重合说明探针挂错了位置。

#### 4.3.2 核心流程

预期的剖面形态（横轴深度 l，纵轴 rms，定性示意）：

```text
rms
 │        baseline P1=P2: ╱‾╱‾  ↗ 单调上升(√l 至线性), 训练越久抬得越高
 │      attnres P1:      ∿∿∿∿∿∿  周期 k 的锯齿, 包络不随深度抬升
 │      attnres P2:      ──────  有界, 平滑(≤ max 候选范数)
 │  起点校准 ●  ← 两种模型第 0 层应同为嵌入的 rms
 └────────────────────────────── 深度 l →
```

测量流程：

```text
snapshot_norms(model, 固定批):
    probe.P1 = probe.P2 = {}; probe.active = True
    model.eval(); with no_grad: model(x)     # 前向一次即得两条剖面
    probe.active = False; model.train()
判读: 画 P1/P2 vs l; 算 R = P2[L-1]/P2[0]; 检查 attnres P1 相邻峰距 = k
```

#### 4.3.3 源码精读

**P1 归零重起的结构来源**：

> [README.md:L75-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L75-L77)
> `if self.layer_number % (self.block_size // 2) == 0:` / `blocks.append(partial_block)` / `partial_block = None`

第 L77 行 `partial_block = None` 是锯齿的「下降沿」：partial 的累加项数从此清零（下一子层从 `attn_out` 白纸起算，见 [README.md:L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81)）。baseline 没有这三行，主干永不清零——这就是两条 P1 曲线形态分岔的全部代码差异。

**P2 有界的代码根据**（4.1.3 已引，此处看它在剖面中的角色）：

> [README.md:L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L64)
> `h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)`

P2 读到的就是这个 h：softmax 沿深度归一 ⇒ 凸组合 ⇒ \(\|h\|\le\max_n\|V_n\|\)。候选 \(\max_n\|V_n\|\) 本身又只是块级部分和（P1 的峰），所以 P2 的上界与总深度 L 无关。

**项数对照的出处**——baseline 的膨胀在问题陈述里就是「累加」二字（[README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) 的 *accumulate all layer outputs*）：所有前层输出全数进主干；attnres 只有块级候选进打分、部分和项数封顶（[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 的 *partitions layers into N blocks, accumulates within each block*）。

#### 4.3.4 代码实践：未训练 vs 训练 500 步的幅度剖面

1. **实践目标**：在冒烟配置（d=64、L=8、k=2）下测两种模型的 P1/P2 深度剖面，先看初始化时刻，再看短训 500 步之后——观察 baseline 增长随训练发展、attnres 锯齿与有界带成形。
2. **操作步骤**：

```python
# 示例代码 (接 4.2.4 的 Probe 与 u2-l4 的 train)
import matplotlib.pyplot as plt

def snapshot_norms(model, probe, x):
    probe.P1, probe.P2 = {}, {}
    probe.active = True
    model.eval()
    with torch.no_grad():
        model(x)
    model.train(); probe.active = False
    return dict(probe.P1), dict(probe.P2)

x_fix, _ = get_batch(train_ids, block=128, batch=8)     # 固定测量批
profiles = {}
for mode in ['standard', 'attnres']:
    torch.manual_seed(0)
    m = MiniGPT(V, d=64, n_head=4, n_layer=8, block_size=4,
                mode=mode, t_max=128)
    p = Probe(m)
    p1_0, p2_0 = snapshot_norms(m, p, x_fix)            # 初始时刻
    train(m, train_ids, val_ids, steps=500, batch=32,
          block=128, lr=3e-4, eval_every=10**9, seed=0) # 大间隔=跳过评测打印
    p1_t, p2_t = snapshot_norms(m, p, x_fix)            # 训练 500 步后
    p.remove()
    profiles[mode] = dict(init=(p1_0, p2_0), trained=(p1_t, p2_t))

plt.figure(figsize=(7, 4.5))
for mode, color in [('standard', 'tab:red'), ('attnres', 'tab:blue')]:
    for stage, alpha in [('init', 0.35), ('trained', 1.0)]:
        p1, p2 = profiles[mode][stage]
        ls = '-' if mode == 'standard' else '--'
        plt.plot(sorted(p2), [p2[l] for l in sorted(p2)], color=color,
                 linestyle=ls, alpha=alpha,
                 label=f'{mode} P2 @{stage}')
        if mode == 'attnres':
            plt.plot(sorted(p1), [p1[l] for l in sorted(p1)], color=color,
                     linestyle=':', alpha=alpha, label=f'attnres P1 @{stage}')
plt.xlabel('depth l'); plt.ylabel('rms'); plt.yscale('log')
plt.grid(True); plt.legend(fontsize=8)
plt.savefig('magnitude_profile.png', dpi=150)

for mode in ['standard', 'attnres']:
    p2 = profiles[mode]['trained'][1]
    print(f"{mode:9s} R(trained) = {p2[7] / p2[0]:.2f}")
```

3. **需要观察的现象**：初始时刻 baseline 的 P2 可能只有轻微上翘（子层初始输出小）；训练 500 步后上翘明显加强。attnres 的 P1 呈周期 2 的锯齿（峰在进入偶数层前），且初始与训练后的包络都大致持平；P2 平稳有界。两种模型第 0 层的点重合。
4. **预期结果**：锯齿周期 = k = 2 与「第 0 层重合」是结构必然，应严格可见；baseline 单调上升的方向高概率成立（u1-l2 的统计趋势），但**增幅大小**取决于语料与训练时长——具体数值待本地验证。若 baseline 的 R ≈ 1，先延长训练再下结论（初始子层输出小，膨胀需要训练「养」出来）。

#### 4.3.5 小练习与答案

**练习 1**：attnres 的 P1 锯齿周期为什么恰是 k？峰出现在哪些层号前？k=2 的 L=8 配置里列出峰的位置。

> **答案**：边界每 k 层触发一次（`layer_number % k == 0`），partial 从清零到攒满恰好经历 k 层，故 P1（= 进入第 l 层的部分和）以 k 为周期波动。峰出现在进入边界层之前（部分和最满时）。k=2、层号 0…7：进入第 0 层是嵌入（1 项），进入第 1 层 2 项，进入第 2 层 4 项（峰），进入第 3 层 2 项，……峰在 l = 2, 4, 6（进入这些层之前攒满 4 项），谷在奇数层。注意第 0 层是特例（嵌入单独成块）。

**练习 2**：为什么 attnres 的 P2 没有 P1 那样的锯齿？

> **答案**：P2 是全部候选的凸组合，各候选是「不同块的部分和」——满的、半满的、嵌入都在候选里，加权平均把单块的部分和波动平滑掉；且凸组合的输出被 \(\max_n\|V_n\|\)（≈锯齿的峰）封顶，深度方向没有系统性抬升因素。

**练习 3**：baseline 的 P1/P2 剖面如果实测不是严格单调（略有起伏），与 u1-l2 的「单调增长」矛盾吗？

> **答案**：不矛盾。\(\sqrt{l}\) 至线性增长是**项数结构**决定的统计趋势（假设子层输出方向大致不相关）；逐层实测还叠加了各子层输出自身尺度的波动（有的层输出大、有的小），短序列上也有限样本噪声。判读看**包络趋势**（如对 l 做线性回归的斜率、或首末比 R），而不是要求逐点单调。

### 4.4 梯度范数统计：逐层梯度探针与均匀性度量

#### 4.4.1 概念说明

再测梯度（M2）。三个设计决定：

**测哪个梯度？** 参数梯度，且选每层 `attn.qkv.weight` 这一个代表：每层恰一个、形状跨层一致（范数可比）、每次 backward 必算（hook 必触发）。逐层扫全部参数的聚合范数也可行，但多一层聚合就多一层解读负担；单代表参数是干净的口径。论文图的口径（参数梯度还是激活梯度）README 未注明，以 PDF 为准（待确认）。

**为什么这个口径能读出「均匀性」？** 对线性层 \(y = xW^\top\)：\(\partial\mathcal{L}/\partial W = \delta^\top x\)，\(\delta\) 为上游输出梯度。PreNorm 让 \(\|x\|\)（`attn_norm(h)` 的输出尺度）跨层近常数，于是 \(g_l\) 的跨层变化主要反映 \(\delta_l\)——**上游给各层的反向信号强度**。baseline 的稀释让各层「对最终状态的影响力」按 \(1/\sqrt{l+1}\) 式递减（u1-l2），反向信号随之倾斜；attnRes 每层经学得的 \(\alpha\) 保有对后续所有层的直达通路（[README.md:L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43)），且前向幅度深度无关（M1），反向信号更可能同量级——M2 的均匀性部分是 M1 有界性的下游后果。

**何时测、和谁比？** 梯度范数的绝对值随批次内容与训练阶段波动，**跨 step 不可比**；必须在「同一训练步、同一固定测量批」下配对比较两种模型。测量 pass 独立于训练 step：前后 `zero_grad`、不做优化器更新，不污染训练状态。

均匀性指标用 CV（2.3 节），辅以 max/min 比；剖面形态（偏向浅层还是深层）两种方案各自长什么样，以实测为准——我们不预登记方向，只登记「attnres 更平」这一相对命题。

#### 4.4.2 核心流程

```text
snapshot_grads(model, probe, x, y):        # 独立测量 pass
    probe.P3 = {}; probe.active = True
    model.zero_grad(set_to_none=True)      # 先清: P3 是这个批的纯梯度
    _, loss = model(x, y); loss.backward() # 反向触发参数 hook (反序, 已带层号)
    probe.active = False
    model.zero_grad(set_to_none=True)      # 后清: 测量梯度不留给优化器
读取: {l: g_l} → 曲线; CV = std/mean; max/min
交叉验证: 同批再做一次 backward, 不靠 hook 手动扫 qkv.weight.grad
         → 两组应逐层一致(证明 hook 没写错)
```

#### 4.4.3 源码精读

**主张原文（本模块要验证的第二句）**：

> [README.md:L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L123)
> … gradient norms distribute more uniformly across layers.

注意比较级「more」——主张的是**相对均匀**，因此指标设计成两模型同条件下的 CV 对比，而不是绝对阈值。

**P3 参数所属的子层**：

> [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)
> `attn_out = self.attn(self.attn_norm(h))`

`qkv.weight` 属于 `self.attn`，其输入是归一化后的 h——这解释了 4.4.1 的口径分析：输入尺度被 PreNorm 抹平，P3 读出的倾斜来自上游 \(\delta\)。

**「每层都能发声」的机制出处**：

> [README.md:L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L43)
> This gives every layer selective, content-aware access to all earlier representations.

每个位点都对全部块候选打分，等价地，每个块候选都经 \(\alpha\) 与后续所有层直连——梯度沿这些学得权重的通路回传，不随深度衰减到 1/√L 的量级。

#### 4.4.4 代码实践：梯度剖面 + CV + hook 交叉验证

1. **实践目标**：短训 500 步后，在同一固定批上测两种模型的 P3 剖面；用「无 hook 手动扫描」交叉验证 hook 记录正确；计算 CV 与 max/min，检验 \(\mathrm{CV}_{attnres} < \mathrm{CV}_{baseline}\) 是否在本规模成立。
2. **操作步骤**：

```python
# 示例代码 (接 4.2.4 的 Probe 与 u2-l4 的 train)
def cv(v):
    m = sum(v) / len(v)
    sd = (sum((x_ - m) ** 2 for x_ in v) / (len(v) - 1)) ** 0.5
    return sd / m

torch.manual_seed(0)
xg, yg = get_batch(train_ids, block=128, batch=8)      # 固定测量批

for mode in ['standard', 'attnres']:
    torch.manual_seed(0)
    m = MiniGPT(V, d=64, n_head=4, n_layer=8, block_size=4,
                mode=mode, t_max=128)
    train(m, train_ids, val_ids, steps=500, batch=32,
          block=128, lr=3e-4, eval_every=10**9, seed=0)

    p = Probe(m)
    p.P3 = {}; p.active = True
    m.zero_grad(set_to_none=True)
    _, loss = m(xg, yg); loss.backward()
    hooked = dict(p.P3)
    p.active = False

    # 交叉验证: 不靠 hook, 手动读 .grad
    m.zero_grad(set_to_none=True)
    _, loss = m(xg, yg); loss.backward()
    manual = {l: float(m.layers[l].attn.qkv.weight.grad
                       .pow(2).mean().sqrt()) for l in range(8)}
    m.zero_grad(set_to_none=True)
    p.remove()

    ok = all(abs(hooked[l] - manual[l]) <= 1e-5 * max(manual[l], 1e-12)
             for l in range(8))
    g = [manual[l] for l in range(8)]
    print(f"{mode:9s} hook==manual: {ok}  CV={cv(g):.3f}  "
          f"max/min={max(g) / min(g):.2f}  g0={g[0]:.2e} g7={g[7]:.2e}")
```

3. **需要观察的现象**：`hook==manual` 为 `True`（同一批两次 backward 的确定性计算应逐层一致；GPU 上个别算子非确定性时允许相对误差 1e-5 量级）；两种模型的 g 曲线形态（倾斜方向）与 CV 差值——**这是本实践真正的观测对象**，具体数值待本地验证。
4. **预期结果**：交叉验证应通过（不通过说明 hook 写错，先修仪器再读数）。CV 对比：若 \(\mathrm{CV}_{attnres} < \mathrm{CV}_{baseline}\)，支持 M2；若两者接近甚至反超，如实记录——小模型（L=8）的梯度分布本就相对均匀，主张可能需要更深的模型才显形；判读时最好对 3 个种子重复并比较 CV 的种子间方差（综合实践一并做）。

#### 4.4.5 小练习与答案

**练习 1**：为什么梯度剖面必须在「同一训练步、同一批次」下配对测量？

> **答案**：梯度范数绝对值随损失景观位置（训练阶段）与批次内容波动，跨 step、跨批的比较会把「测量条件差异」误读成「结构差异」。固定批 + 同一步 + 两模型配对（同种子，u2-l4 的配对思想），差异才能归因到残差结构。

**练习 2**：CV 指标衡量什么？它的盲区是什么？

> **答案**：CV = std/mean，衡量「各层梯度尺度的相对离散度」，对尺度缩放不敏感（两模型梯度整体大小不同也能比均匀性）。盲区：不反映梯度方向的信息（如各层梯度间的冲突/对齐程度），也不反映绝对尺度——一个模型可能梯度更均匀但整体更小（训练更慢）。下结论时应连同均值一起报。

**练习 3**：实测 \(\mathrm{CV}_{baseline} < \mathrm{CV}_{attnres}\)，下一步做什么？

> **答案**：按顺序排查：(i) 仪器——hook 交叉验证是否通过、两种模型是否测于同一批同一步；(ii) 实现——u2-l4 的候选数探针确认 attnres 调度正确；(iii) 解读——L=8/d=64 的小模型梯度分布可能本就均匀，结构性差异需更深模型才显形（把 L 加大再测，或留给 u3-l2 的多规模实验）。全部排除后如实报告「本规模不支持 M2」，这本身是有价值的负结果——小规模不能证实也不能证伪 48B 规模的论文主张。

## 5. 综合实践

### 5.1 任务：两种模型的「深度 × 训练阶段」动态剖面

把 u2-l4 的 `minitest.py` 与本讲的 `Probe` 放进同一目录，执行本任务：**在对比配置（d=256、L=16、block_size=4）下训练 Standard 与 Block AttnRes 各一个种子，在第 0、500、1500、3000 步各做一次快照（P1/P2/P3，固定测量批），产出论文三联图 [assets/training_dynamics.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/training_dynamics.png) 的迷你版**。

产出物清单：

- `training_dynamics_mini.png`：左图幅度剖面（P2 为主、attnres 的 P1 虚线展示锯齿），右图梯度剖面（P3，取最终测量点）；
- 一张指标表（5.3 模板）：两模型的 R、CV、max/min、P1 锯齿周期；
- 一段结论文字：M1/M2 各自「支持 / 不显著 / 反向」哪一档，以及与 README L121-L123 的关系。

### 5.2 主脚本

```python
# 示例代码: 综合实践驱动脚本 (复用 minitest.py 的定义与 4.2.4 的 Probe)
import matplotlib.pyplot as plt

CFG    = dict(d=256, n_head=8, n_layer=16, block_size=4, t_max=256)
MEAS   = [0, 500, 1500, 3000]                 # 测量时刻(步)
BATCH, BLOCK = 16, 256
L = CFG['n_layer']

torch.manual_seed(123)                         # 测量批独立于训练种子
xm, ym = get_batch(val_ids, BLOCK, BATCH)      # 固定测量批, 取自验证集

def take(model, probe):
    probe.P1 = probe.P2 = probe.P3 = {}
    probe.active = True
    model.eval()
    with torch.no_grad():
        model(xm)                              # P1/P2
    model.train()
    model.zero_grad(set_to_none=True)
    _, loss = model(xm, ym); loss.backward()   # P3
    probe.active = False
    model.zero_grad(set_to_none=True)
    return dict(P1=dict(probe.P1), P2=dict(probe.P2),
                P3=dict(probe.P3))

records = {}
for mode in ['standard', 'attnres']:
    torch.manual_seed(0)
    model = MiniGPT(V, mode=mode, **CFG)
    probe = Probe(model)
    snap = {0: take(model, probe)}
    prev = 0
    for i, ms in enumerate(MEAS[1:], 1):       # 分段训练到各测量点
        train(model, train_ids, val_ids, steps=ms - prev, batch=64,
              block=BLOCK, lr=3e-4, eval_every=10**9,
              seed=1000 + i)   # 每段不同 seed(段间不重放), 两模式同 seed(配对)
        snap[ms] = take(model, probe)
        prev = ms
    probe.remove()
    records[mode] = snap

colors = {'standard': 'tab:red', 'attnres': 'tab:blue'}
fig, axes = plt.subplots(1, 2, figsize=(11, 4))
for mode in ['standard', 'attnres']:
    for i, tag in enumerate(MEAS):
        p2 = records[mode][tag]['P2']
        axes[0].plot(sorted(p2), [p2[l] for l in sorted(p2)],
                     color=colors[mode], linestyle='-' if mode == 'standard' else '--',
                     alpha=0.3 + 0.7 * i / (len(MEAS) - 1),
                     label=f'{mode}@{tag}' if tag == MEAS[-1] else None)
    p1 = records[mode][MEAS[-1]]['P1']
    axes[0].plot(sorted(p1), [p1[l] for l in sorted(p1)],
                 color=colors[mode], linestyle=':', alpha=0.8,
                 label=f'{mode} P1(层间)')
    p3 = records[mode][MEAS[-1]]['P3']
    axes[1].plot(sorted(p3), [p3[l] for l in sorted(p3)],
                 color=colors[mode], marker='o', label=mode)
axes[0].set_xlabel('depth l'); axes[0].set_ylabel('rms'); axes[0].set_yscale('log')
axes[1].set_xlabel('depth l'); axes[1].set_ylabel('rms(grad qkv)'); axes[1].set_yscale('log')
for ax in axes: ax.grid(True); ax.legend(fontsize=8)
plt.savefig('training_dynamics_mini.png', dpi=150)

for mode in ['standard', 'attnres']:
    s = records[mode][MEAS[-1]]
    R  = s['P2'][L - 1] / s['P2'][0]
    g  = [s['P3'][l] for l in range(L)]
    print(f"{mode:9s} R={R:5.2f}  CV={cv(g):.3f}  "
          f"max/min={max(g) / min(g):.1f}")
```

（`cv` 取 4.4.4 的定义。）

### 5.3 记录表模板

| 指标 | Standard | AttnRes | 判读要点 |
|:---|:---:|:---:|:---|
| P2 第 0 层 rms（校准点） | | | 两模式应近似相等（同为嵌入） |
| P2 末层 rms | | | |
| R = 末/首 | | | \(R_{attnres} \ll R_{baseline}\) 支持 M1 |
| P1 形态 | 单调（无锯齿） | 锯齿周期 = k = 2 | 结构性检查，应严格成立 |
| CV(P3) | | | \(\mathrm{CV}_{attnres} < \mathrm{CV}_{baseline}\) 支持 M2 |
| max/min(P3) | | | 同上，对极端层敏感 |

另记：语料与 V、测量批来源（验证集）、测量时刻表、训练超参、每步耗时比（应与 u2-l4 一致，观测本身几乎零开销）。

### 5.4 预期现象与判读（定性预期，具体数值待本地验证）

1. **M1 大概率清晰可见**：baseline 的 P2 剖面随训练从近似平坦长成单调上升（R 明显大于 1 且逐次增大）；attnres 的 P1 是周期 2 的锯齿、P2 平稳有界，3000 步内包络不应单调抬升。这一对比是结构性的，是本实践最稳的预期。
2. **M2 可能不显著**：L=16 的小模型梯度分布未必表现出论文量级的倾斜，CV 差距可能落在噪声内。此时如实写「本规模 M2 不显著」，并可把 L 加倍再测一次（更深的模型稀释更重，主张更可能显形）。
3. **测量零污染检查**：对比「有探针训练」与「无探针训练」的最终验证损失（探针平时关闭，二者应一致到随机性以内）——仪器不扰动系统。
4. **反向结果的处理**：若 attnres 的幅度剖面也单调膨胀，先跑 u2-l4 的候选数探针（边界与封存是否正确），再看 P1 是否真的周期归零；结构检查全过而现象仍在，才是真正的「与主张不符」，如实报告。
5. **规模声明**：本实验比 README 的证据规模（Kimi Linear 48B / 1.4T tokens，[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)）小五个数量级以上，全部结论限定在「本配置下的定性复现」。

### 5.5 进阶（可选）

- **激活梯度探针**：在 4.3 的测量前向里，对每层捕获的 h 张量现挂 `h.register_hook(lambda g: ...)`，记录 \(\partial\mathcal{L}/\partial h_l\) 的范数——这是「反向信号沿深度衰减多快」的更直接读数（注意中间激活 hook 一次性，每次前向要重挂，见 4.2 练习 2）。
- **k 扫描衔接 u3-l1**：把 block_size 取 2/4/8 重跑本实践，观察 P1 锯齿周期随之变为 1/2/4、候选数与 P2 上界的变化——为下一讲的显存/复杂度实测预热。

## 6. 本讲小结

- **训练动态是深度轴上的剖面**：在选定训练时刻测「每层表示幅度 / 每层梯度范数」随层号的分布，与步数轴的损失曲线互补——损失看结果，剖面看机理；[README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123) 的两句主张被翻译成可证伪命题 M1（幅度剖面 + 增长比 R）与 M2（梯度剖面 + 变异系数 CV）。
- **机理一句话**：把幅度拆成「项数 × 单项尺度」——baseline 的主干项数随深度线性增（[README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37)），attnres 的部分和项数封顶 \(2k\)、聚合又是凸组合（[README.md:L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L64)），故 P2 有界；有界且深度无关的前向尺度 + 每层经 \(\alpha$ 的直达发声通路，是梯度更均匀（M2）的定性根源。
- **仪器是一件可开关的 `Probe`**：P1 挂 `Block` pre-hook（层间状态）、P2 挂 `attn_norm` pre-hook（子层输入，两种模式下语义严格一致）、P3 挂 `qkv.weight` 参数梯度 hook；梯度记录必须带层号（backward 反序触发），`register_full_backward_hook` 因 `Block` 返回含列表而不可靠。
- **幅度测量的三个结构必然**：baseline 的 P1 与 P2 逐层相等；attnres 的 P1 是周期 k 的锯齿（边界封存即下降沿）；两模型第 0 层 P2 同为嵌入（空候选退化恒等）——三条都不需要训练就能当 sanity check。
- **梯度测量的规范**：同一步、同一固定批、配对比较；测量 pass 前后 `zero_grad` 且不 step；hook 记录与手动扫 `.grad` 交叉验证；CV 的差距要和种子方差比较后才下结论，不显著就如实写不显著。
- 本讲全部测量架在 u2-l4 实验台上，未新增任何模型代码——训练动态证据与损失曲线证据（u2-l4）至此成对，下一讲起进入专家层的开销分析与多规模实验。

## 7. 下一步学习建议

- **下一讲 u3-l1（复杂度分析）**：本讲的 P1 锯齿与候选数直接连着显存话题——把 block_size 当旋钮（k=1/2/4/8/16）实测峰值显存与耗时，检验 O(Ld)→O(Nd) 的估算与「~8 blocks 恢复大部分收益」（[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)）。
- **向后衔接 u3-l2（scaling law）**：本讲的机理证据（幅度有界、梯度均匀）与 u2-l4 的损失证据互补；多规模重复训练后拟合损失-计算量幂律，才能检验「1.25 倍计算等效应」——届时可顺带验证 M1/M2 的显形是否随规模增强。
- **向后衔接 u3-l4（论文精读）**：带着本讲遗留的口径问题去读 `Attention_Residuals.pdf`——三联图各子图测的确切对象（参数梯度还是激活梯度）、在哪个训练时刻测量、深度轴如何对齐；核对你 mini 复现的形态与论文是否一致（待确认）。
- **动手巩固**：完成 5.5 的两个进阶项；再把本讲的 `Probe` 接到 5.5 的 k 扫描上，做一张「k vs P2 上界 / CV」的小表，你会同时看到结构（锯齿周期）与统计（均匀性）两条线如何随块大小移动。
