# u3-l4 论文精读与复现路线图

> **本讲定位（advanced）**：前三讲我们把仓库里「看得见」的材料读完了——伪代码（u2-l1/u2-l2）、训练对比（u2-l4）、训练动态（u2-l5）、复杂度（u3-l1）、Scaling 律拟合（u3-l2）、下游基准读法（u3-l3）。但这个仓库**没有一行可运行的官方代码**：公式推导、消融实验、训练超参这些「真货」全部在论文 PDF 里。本讲教你两件事：**怎样把论文当源码精读**，以及**怎样从论文结论里挑出能在小规模复现的部分，制定并执行一份严谨的复现计划**。本讲写作时已对照论文全文（arXiv:2603.15031v1，与仓库内 PDF 同源）核对所有数字；论文细节一律标注节号/表号——PDF 没有行号锚点，永久链接只能定位到文件级，请按节号在 [Attention_Residuals.pdf](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/Attention_Residuals.pdf) 内定位。

## 1. 本讲目标

学完本讲，你应该能够：

1. **建立完整知识地图**：对论文的每一条核心结论，说出它分别出现在 README 的哪一段、论文的哪一节、仓库的哪张图，以及哪些细节**只有论文里有**。
2. **做可复现性分级**：把论文结论分成「可小规模复现（A）」「只能复现趋势（B）」「资源上不可复现（C）」三类，并给出 FLOPs 量级的估算依据。
3. **设计消融实验**：读懂论文 Table 4 的消融设计（单因素、同超参、同算力），掌握「预注册 + 配对种子 + 噪声底线」三件套，并在 u2-l4 迷你实验台上落实。
4. **产出一份复现报告**：设置 → 结果 → 与论文对比 → 差异原因分析，四段齐全。

## 2. 前置知识

本讲是第三单元的收官方法论，需要以下概念（不懂的先补对应讲义）：

| 概念 | 一句话解释 | 详细来源 |
|:---|:---|:---|
| AttnRes / Block AttnRes | 用深度方向 softmax 注意力替代固定单位权重的残差累加 | u2-l1、[README.md:L33-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33-L47) |
| 最小实验台 | 本手册 u2-l4 搭的字符级 MiniGPT，`mode='standard'` / `mode='attnres'` 一拨即换 | u2-l4 |
| 深度探针 | u2-l5 的 Probe 工具：逐层输出幅度（P1）、梯度范数（P2）、学到的注意力权重（P3） | u2-l5 |
| 幂律拟合与算力倍率 | \(L(C) = aC^{-b}\)，等损失换算出 compute multiplier | u3-l2 |
| 三遍读论文法 | 第一遍读图表抓主张，第二遍读方法与实验，第三遍精读推导与细节 | 本讲 4.1 展开 |
| 消融实验（ablation） | 把系统里的一个组件拆掉/替换，看性能变化，以证明该组件的必要性 | 本讲 4.3 展开 |

两个容易混淆的词先分清：

- **复现（reproduce）**：用**自己的代码**、尽量接近的设置，重新得到论文的结论。分三个层次：**数值复现**（数字对上）、**趋势复现**（方向/排序对上）、**机制复现**（内部现象，如「幅度有界」对上）。
- **超参数 vs 结构选择**：学习率、batch size 是超参数（训练时人为给定）；块大小 \(S\)、用 softmax 还是 sigmoid、要不要 RMSNorm 是**结构选择**（消融的对象）。论文 Table 2 给前者，Table 4 消融后者。

## 3. 本讲源码地图

| 材料 | 作用 | 本讲用途 |
|:---|:---|:---|
| [README.md:L33-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33-L47) | AttnRes 主张与核心公式（= 论文 Eq.4） | 4.1 精读入口 |
| [README.md:L53-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L65) | `block_attn_res` 伪代码（= 论文 Fig.2） | 4.3 消融轴定位 |
| [README.md:L67-L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L90) | 单层 `forward` 调度伪代码 | 4.3 块边界逻辑 |
| [README.md:L97-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L97-L99) | Scaling Laws 结论（1.25×） | 4.1 数字溯源、4.2 分级 |
| [README.md:L105-L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105-L119) | 下游基准表（= 论文 Table 3 的 9/16 行） | 4.2 C 类结论 |
| [README.md:L121-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L127) | Training Dynamics 结论（= 论文 Fig.5） | 4.2 A 类结论 |
| [Attention_Residuals.pdf](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/Attention_Residuals.pdf)（全文；[arXiv:2603.15031](https://arxiv.org/abs/2603.15031)） | 一切细节的唯一来源：§3 公式、§4 系统、§5 实验 | 4.1–4.3 全程对照 |
| `assets/scaling_law.png`、`assets/training_dynamics.png`、`assets/overview.png` | 论文 Fig.4 / Fig.5 / Fig.1 的仓库快照 | 图文互证 |
| 本手册 `u2-l4-minimal-testbed-training.md` | 迷你实验台（复现的执行载体） | 4.2、4.3、综合实践 |
| 本手册 `u2-l5-training-dynamics.md` | 深度探针（机制复现的工具） | 4.2 |

> 提醒：本仓库 git 只跟踪 5 个文件（README、PDF、4 张图）。**任何声称「来自官方代码」的东西都不存在**——复现必须从伪代码出发自己写，这正是本手册第二单元做过的事。

## 4. 核心概念与源码讲解

### 4.1 论文精读方法：从 README 到论文细节

#### 4.1.1 概念说明

README 是「摘要的摘要」：它告诉你**结论**（更好、1.25×、+7.5），但不告诉你**依据和条件**（哪个模型、什么超参、和谁比、差多少算显著）。复现最常犯的错误，就是把 README 的口号当成实验设置。论文才是细节载体——对这个仓库而言，论文 PDF 是**唯一**能回答以下问题的地方：

- 核心公式里 \(\alpha_{i \to l}\) 的精确定义（核函数、归一化）；
- 消融实验消了什么、数字是多少；
- 每个模型规模的超参（层数、lr、batch）；
- 伪查询 \(\mathbf{w}_l\) 的初始化要求。

精读方法采用经典的三遍读法（three-pass），并把它映射到本仓库的材料顺序上。

#### 4.1.2 核心流程

三遍读法在本仓库的落地流程：

```text
第 0 遍（仓库侦察，5 分钟）
  README 全文 → 知道有哪些主张（4 大块：方法/伪代码/Scaling/下游/动态）
  ↓
第 1 遍（论文骨架，20 分钟）
  只读：Abstract → 各节标题 → 图表（Fig.1 结构、Fig.4 曲线、Fig.5 动态、Table 3 下游）
  产出：主张清单 claim list，每条标「README 已给数字 / 只有论文有」
  ↓
第 2 遍（方法与实验，2 小时）
  §3 公式（Eq.1–6）→ §5.1–5.3 实验设置与消融
  产出：claim → evidence（哪张表/图）→ detail（什么条件）三栏表
  ↓
第 3 遍（按需精读）
  §4 系统优化、§5.4 分析、§6 讨论、附录 B 推导
  只在复现触及时读（例如复现块大小扫描时精读 §5.3 与 Fig.6）
```

配套的**三栏笔记法**（第 2 遍的核心产出）：

| 列 | 内容 | 例 |
|:---|:---|:---|
| claim | 论文主张 | Block AttnRes 匹配 1.25× 算力的基线 |
| evidence | 支撑证据的位置 | 论文 §5.1 Fig.4 + 拟合系数；README L99 |
| detail | 成立条件（模型/超参/对比对象） | 5 档规模、8192 ctx、组内共享超参、5.6 PFLOP/s-days 处换算 |

#### 4.1.3 源码精读

**(a) README 的公式是论文 Eq.(4) 的展示版。** README 核心公式（[README.md:L41-L43](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L41-L43)）：

\[
\mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \cdot \mathbf{v}_i
\]

这一行对应论文 §3.1 的 Eq.(4)。但论文在这之前还给出了 README 没有的两步：Eq.(2) 定义核函数与 softmax 归一化

\[
\alpha_{i \to l} = \frac{\phi(q_l, k_i)}{\sum_{j=0}^{l-1}\phi(q_l, k_j)},
\qquad
\phi(q,k) = \exp\!\big(q^\top \mathrm{RMSNorm}(k)\big)
\]

以及 Eq.(3) 定义来源：\(q_l = \mathbf{w}_l\)（可学习伪查询），\(k_i = v_i\)，其中 \(v_0 = \mathbf{h}_1\) 是**词嵌入**，\(v_i = f_i(h_i)\)（\(i \ge 1\)）是第 \(i\) 个子层输出；论文记号里每个 self-attention 或 MLP 各算一个「层」。复现时这两个公式缺一不可——README 伪代码里 `K = norm(V)` 那一行（[README.md:L61-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L64)）就是 \(\phi\) 里的 RMSNorm，正是 4.3 节要消融的组件。

**(b) 「1.25×」在 README 里是口号，在论文里是可以验算的数字。** README 只说「Block AttnRes matches the loss of a baseline trained with 1.25x more compute」（[README.md:L97-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L97-L99)）。论文 §5.1 给出三条拟合曲线（\(C\) 单位为 PFLOP/s-days）：

\[
L_{\text{base}} = 1.891\,C^{-0.057},\quad
L_{\text{block}} = 1.870\,C^{-0.058},\quad
L_{\text{full}} = 1.865\,C^{-0.057}
\]

代入 \(C = 5.6\)：基线 \(= 1.891 \times 5.6^{-0.057} \approx 1.714\)，Block \(= 1.870 \times 5.6^{-0.058} \approx 1.692\)（与论文正文给的数字一致）。再反解基线需要多少算力才能达到 1.692：

\[
1.891\,{C'}^{-0.057} = 1.692
\;\Rightarrow\;
C' = \left(\tfrac{1.891}{1.692}\right)^{1/0.057} \approx 7.0
\;\Rightarrow\;
C'/C \approx 7.0/5.6 \approx 1.25
\]

这就是 u3-l2 学过的 compute multiplier 换算的实战版本——**README 的头条数字可以从论文系数独立验算出来**，这是精读最有成就感的时刻。

**(c) README 完全没有、只存在于论文里的关键细节**（复现前必须逐条抄进笔记）：

| 细节 | 论文位置 | 内容 |
|:---|:---|:---|
| 伪查询零初始化 | §5 Architecture Details | 「all pseudo-query vectors must be initialized to zero」——初始 \(\alpha\) 均匀 → 起步等权平均，防止训练震荡（论文称经过实证验证） |
| Scaling 实验超参 | §5.1 Table 2 | 5 档激活参数 194M–528M；token 数 38.7B–119.0B；\(L_b\)（Transformer 块数）12→17；d_model 896→1264（\(d_{ff}/d_{model}\approx 0.45\)）；lr 2.99e-3→2.02e-3（cosine）；batch 192→432；上下文 8192 |
| 组内公平比较 | §5.1 | 同规模组内三个变体**共享同一组在基线下选出的超参**——刻意偏向基线，比较是保守的 |
| 48B 主模型配方 | §5.2 | Kimi Linear 结构（KDA:MLA = 3:1 混排 + MoE），27 个 Transformer 块（54 子层），8/256 路由专家 + 1 共享专家；**每块 6 子层 → 9 块 + 嵌入 = 10 个深度来源**；Muon 优化器、WSD 调度、全局 batch 8M token、4096 ctx；两阶段：1T token 预训练 + ≈400B 高质量 token mid-training，再扩到 32K ctx |
| 消融模型身份 | §5.3 Table 4 | 用 Table 2 的 436M 档（\(L_b=16\)，即 32 个子层；证据：Table 4 基线损失 1.766 与 Table 2 该档完全一致；Fig.8 也注明该模型 16 个注意力 + 16 个 MLP 子层） |
| 系统开销数字 | §4 | 流水线并行下训练开销 <4%，推理时延开销 <2%；跨阶段缓存把通信从 \(O(C)\) 降到 \(O(P)\)；两阶段计算 + online softmax 合并 |

**(d) 仓库图片 ↔ 论文图的对应关系**：`assets/overview.png` ↔ 论文 Fig.1（三种残差对比）；`assets/scaling_law.png` ↔ Fig.4（含 1.25× 标注与三条拟合式）；`assets/training_dynamics.png` ↔ Fig.5（损失/输出幅度/梯度幅度三联图）。看图时永远以论文版为准——分辨率更高且带坐标轴刻度。

#### 4.1.4 代码实践：「论文细节侦探」

1. **实践目标**：建立「README 说不清的问题 → 论文哪一节回答」的检索能力，产出 5 条复现必需的细节记录。
2. **操作步骤**：
   - 打开仓库内 [Attention_Residuals.pdf](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/Attention_Residuals.pdf)（或 [arXiv:2603.15031](https://arxiv.org/abs/2603.15031)），先只读 Abstract 与所有图表（第 1 遍）。
   - 对下面 5 个问题，在论文中定位到节/表，并各写一行答案：
     1. \(\mathbf{w}_l\) 的初始化是什么？为什么？
     2. Scaling 实验的 5 档模型各训练多少 token？lr 范围？
     3. 消融用的是哪一档模型？证据是什么？
     4. 「1.25×」从论文的哪三个数字算出来？
     5. 48B 模型每块几个子层、共几个块、几个深度来源？
   - 把答案填进自己的三栏表（claim / evidence / detail）。
3. **需要观察的现象**：你会发现 5 个问题里**没有一个**能从 README 或本手册前几讲的伪代码直接回答——它们全部来自论文 §5 的正文与表格。
4. **预期结果**：与 4.1.3 (c) 的表格逐条对得上（零初始化 / 38.7B–119.0B 与 2.99e-3–2.02e-3 / 436M 档 / 1.891、1.870 与 5.6 处的 1.714、1.692 / 每块 6 子层共 9 块 + 嵌入 = 10 来源）。这是阅读型实践，无需运行代码；具体页码因 PDF 版式而异，以节号为准。

#### 4.1.5 小练习与答案

**练习 1**：README 的核心公式（[README.md:L41](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L41)）相对论文 §3.1 缺了什么？
**答**：缺 Eq.(2)（核函数 \(\phi(q,k)=\exp(q^\top\mathrm{RMSNorm}(k))\) 与 softmax 归一化）和 Eq.(3)（\(v_0\) 是嵌入、伪查询 \(q_l = \mathbf{w}_l\)、k=v 的身份）。没有这两步，\(\alpha_{i\to l}\) 只是一个抽象记号，无法实现。

**练习 2**：为什么「1.25×」这个数字无法从 README 自身验证，却能从论文验证？
**答**：README 只给结论句（L99），没有任何可代入的量。论文给出拟合系数 \(L = A C^{-\alpha}\) 与换算点 \(C=5.6\)，代入即可数值验算（见 4.1.3 (b)）。

**练习 3**：「伪查询必须零初始化」为什么不可能出现在 README 伪代码里，复现者却必须知道？
**答**：README 伪代码只描述前向计算结构，不涉及初始化与训练稳定性；这类「训练技巧」写在论文 §5 的 Architecture Details 里。不知道它，复现的 AttnRes 起步就不是等权平均，训练可能更震荡，且与论文设置不可比。

### 4.2 复现计划制定：可复现性分级与实验设计

#### 4.2.1 概念说明

论文实验动辄 \(10^{20}\) FLOPs 起步，个人复现者唯一的出路是**分级**：先判断每条结论「值得且能够在我的预算内验证到什么程度」，再投入算力。分级依据三个问题：

1. **结论的类型**是机制/趋势（如「幅度有界」「块数中间最优」），还是绝对数值（如「GPQA 44.4」）？前者有希望小规模复现，后者基本没有。
2. **实现载体**我有没有？本手册 u2-l4 的实验台 + u2-l5 的探针就是载体。
3. **算力差距**多大？差 2–3 个数量级可以指望趋势相似；差 6 个数量级以上，连趋势都可能变。

关键的数量级账（用 Chinchilla 式估算 \( \mathrm{FLOPs} \approx 6ND \)，\(N\) 为参数量、\(D\) 为训练 token 数）：

- 论文消融档（436M 激活参数 × 87.9B token）：\(6 \times 4.36\times10^8 \times 8.79\times10^{10} \approx 2.3\times10^{20}\) FLOPs；
- u2-l4 迷你实验台（约 \(8\times10^5\) 参数 × 约 \(2\times10^7\) token，具体以你的实际 run 为准）：\(\approx 10^{14}\) FLOs 量级；
- **差距约 6–7 个数量级**。结论：数值复现不可能，机制/趋势复现可以尝试，且必须多种子 + 诚实报告噪声。

#### 4.2.2 核心流程

对论文结论逐条过筛的分级流程：

```text
论文结论
  ├─ 依赖特定基础设施（多卡流水线 / 48B 权重 / 1.4T token）？ ──是──▶ C 类：不可复现
  ├─ 是绝对数值（基准分数、损失值、倍率数字）？ ────────────是──▶ C 类（但可做 u3-l2 式的拟合/换算练习）
  └─ 是机制或趋势？
       ├─ 迷你实验台能表达自变量？ ──是──▶ A 类：小规模直接复现
       └─ 只在多规模下才有意义（Scaling 曲线）？ ──────▶ B 类：复现趋势形态
```

对论文主要结论的分级结果（本讲复现计划的骨架）：

| 论文结论 | 证据位置 | 分级 | 理由与载体 |
|:---|:---|:---:|:---|
| 训练动态：输出幅度有界（块内周期模式）、梯度更均匀 | §5.2 Fig.5(b)(c)；[README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123) | **A** | u2-l5 探针 P1/P2 直接对应，字符级小模型即可观察（u2-l5 已验证） |
| 块大小趋势：块数太少 → 趋向基线；中间块数 ≈ Full | §5.3 Fig.6 | **A** | u2-l4 实验台 `block_size` 直接可调（见 4.3） |
| RMSNorm 消融：去掉变差，Block 比 Full 更依赖 | §5.3 Table 4 | **A** | 伪代码一行改动（L62 `K = norm(V)`），见 4.3 |
| 零初始化 → 起步等权平均、训练更稳 | §5 Architecture Details | **A**（定性） | 对比零初始化 vs 默认初始化的早期损失曲线 |
| 学到的权重：对角占优 + 嵌入持续有权重 | §5.4.2 Fig.8 | **A** | u2-l5 探针 P3 可视化 |
| Scaling 曲线：AttnRes 各档一致更低、指数近似不变 | §5.1 Fig.4/Table 2 | **B** | 需要 5 档×3 变体；u3-l2 已在迷你规模做过趋势版 |
| 1.25× compute multiplier | §5.1；[README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99) | **C** | 数值验证需整组 scaling 实验（~\(10^{21}\) FLOPs）；换算可纸面验算（4.1.3 (b)） |
| 48B 下游基准提升（GPQA +7.5 等） | §5.2 Table 3；[README.md:L107-L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L107-L119) | **C** | 需要 48B×2 次完整预训练 + 全套评测 |
| PP 训练开销 <4%、推理时延 <2% | §4 | **C** | 需要多卡流水线与推理集群 |

A 类就是本讲综合实践的候选池。制定计划的最后一步是**预注册（pre-registration）**——跑实验之前白纸黑字写下：假设、自变量、控制变量、种子数、判据。这是防止「跑完再挑好看的结果」的唯一办法。

#### 4.2.3 源码精读

**(a) 论文自己的「公平比较」声明是复现计划的模板。** §5.1：「同规模组内所有变体共享同一组在基线下选出的超参，这一设置刻意偏向基线，使比较保守」；§5.3 开头：「所有模型共享相同的超参与算力预算」。翻译成复现计划的检查项：

1. 对比双方**参数量差应远小于噪声**（AttnRes 每层只加 \(4d\)，u2-l3 的结论）；
2. 同数据、同批次调度、同优化器与 lr；
3. 同种子集合，报告均值 ± 标准差（u2-l4 已建立的多种子纪律）。

**(b) 结构对齐：u2-l4 实验台恰好是消融模型的同构缩微版。** 论文消融档是 \(L_b = 16\) 个 Transformer 块 = 32 个子层，Block AttnRes 取 \(S=4\)（每块 4 子层）→ 8 块（§5.1 正文「Block AttnRes with ≈8 blocks」）。u2-l4 的对比配置 `MiniGPT(V, d=D, n_head=8, n_layer=16, block_size=4, mode=...)`：16 个 Transformer 层 = 32 个子层、`block_size=4` → 每块 2 个 Transformer 层、末端 8 块 + 部分和，**与论文消融模型在深度结构上完全同构**，差的是宽度（d=64 vs d_model=1168）、数据（字符级 vs 8192-token 网络语料）、架构细节（朴素注意力 vs KDA/MLA 混排 + MoE）与优化器（AdamW vs Muon）。这四个「差」就是复现报告「差异原因分析」一节的固定素材。

**(c) 48B 的块划分再次印证 README 的「~8 blocks」。** [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 说约 8 块即可恢复 Full 的大部分收益；论文 §5.2 给出 48B 的实际取法：54 子层、每块 6 子层 → 9 块 + 嵌入 = 10 个深度来源。注意论文在不同规模上取的是「约 8–9 块」而不是固定块大小——块数 \(N\) 才是被稳定的量。这提示迷你复现里扫 `block_size` 时，应把结论表述为「块数 N 的效应」而不是「S 的效应」。

#### 4.2.4 代码实践：写预注册 + 实验台冒烟

1. **实践目标**：为综合实践选定的消融结论写一份预注册表，并确认 u2-l4 实验台两种模式都能正常训练。
2. **操作步骤**：
   - 建一个 `repro_plan.md`（放在你自己的实验目录，不放本手册），抄下这个模板并填空：

     ```markdown
     # 预注册：复现论文 §5.3「w/o RMSNorm」消融（或块大小扫描）
     - 假设：去掉 RMSNorm 后，attnres 模式验证损失变差（方向与 Table 4 一致）
     - 自变量：res_norm ∈ {rmsn, none}（或 block_size ∈ {2,4,8,16}）
     - 控制变量：d、n_head、n_layer、block_size（除自变量外）、数据、steps、batch、lr、优化器
     - 种子：≥3 个（如 0/1/2），两臂共用同一种子集合（配对种子）
     - 噪声底线：先跑 3 次基线，取验证损失的标准差 σ；效应 < 2σ 判为「不可分辨」
     - 判据（跑之前写死）：均值差 ≥ 2σ 才声称复现了方向
     ```
   - 冒烟运行：用 u2-l4 的 `MiniGPT` 分别以 `mode='standard'` 与 `mode='attnres'` 训练约 200 步。
3. **需要观察的现象**：两种模式的训练损失都在下降；attnres 模式参数量比 standard 恰好多 \(4d \times\) 层数（u2-l3 结论）。
4. **预期结果**：两条损失曲线正常收敛、attnres 不发散。**待本地验证**（具体损失值取决于你的语料与超参）。另外检查一点：你的 `attn_res_proj` 权重是否**零初始化**？论文 §5 明确要求；若 u2-l4 代码用了 PyTorch 默认（随机）初始化，先对齐这一点，并在报告中记为「初始对齐项」。

#### 4.2.5 小练习与答案

**练习 1**：「流水线并行下训练开销 <4%」应分到哪一级？为什么？
**答**：C 类。它依赖多卡流水线并行与跨阶段缓存（论文 §4.1 的 Fig.3、Eq.7–8），单卡/单机迷你实验台连自变量都无法表达。

**练习 2**：迷你实验台（~\(10^{14}\) FLOPs）与消融档（~\(2.3\times10^{20}\) FLOPs）差多少个数量级？这个差距决定了复现报告里哪种主张是合法的？
**答**：约 6–7 个数量级。合法主张只有「机制/趋势方向是否一致」（如「去 norm 变差」「块数过少趋向基线」），任何「数值吻合」的主张都不合法。

**练习 3**：论文为什么强调「同组变体共享在基线下选出的超参」是保守比较？
**答**：超参按基线调优，AttnRes 并未享受为自己调过的超参；若 AttnRes 仍然更好，结论就更强。复现时照抄这一原则：两组用完全相同的 lr/steps/batch，不做任何偏袒性调参。

### 4.3 消融实验设计：以论文 Table 4 为蓝本

#### 4.3.1 概念说明

消融实验回答的问题是：「这个组件到底值多少？」设计上有四条铁律：

1. **单因素**：每一行只改一个东西，其余全部冻结——否则归因失效。
2. **有基线锚点**：所有行都相对同一个基线（Table 4 里是 PreNorm 1.766）和同一个完整方法（Full 1.737）报告差值。
3. **同预算同超参**：变体之间参数量、算力、学习率保持可比。
4. **先写判据后跑**：效应量与噪声比过不了 2σ 就不声称结论。

论文 Table 4（16-\(L_b\) 消融模型，验证损失，越低越好）原文数字：

| 变体 | 验证损失 | 相对 Full (1.737) |
|:---|:---:|:---:|
| Baseline (PreNorm) | 1.766 | +0.029 |
| DenseFormer（静态系数跨层访问） | 1.767 | +0.030 |
| mHC（多流混合） | 1.747 | +0.010 |
| **AttnRes Full** | **1.737** | 0 |
| w/ input-dependent query（查询由隐状态投影） | 1.731 | −0.006 |
| w/ input-independent mixing（可学习静态标量） | 1.749 | +0.012 |
| w/ sigmoid（sigmoid 替代 softmax） | 1.741 | +0.004 |
| w/o RMSNorm（Full 版去 K 归一化） | 1.743 | +0.006 |
| SWA（滑窗 W = 1+8：嵌入 + 最近 8 层） | 1.764 | +0.027 |
| Block (S=4) | 1.746 | +0.009 |
| w/ multihead（H=16，逐头深度聚合） | 1.752 | +0.015 |
| w/o RMSNorm（Block 版去 K 归一化） | 1.750 | +0.013 |

这张表信息密度极高，论文 §5.3 对它做了三层解读，值得整段消化：

- **内容依赖是灵魂**：DenseFormer 允许跨层访问但系数静态（1.767 ≈ 基线），换成可学习静态标量也只到 1.749——都不如真 softmax 注意力（1.737）。**访问所有历史 ≠ 会选择**。
- **远处访问 vs 近处窗口**：滑窗 SWA 只看最近 8 层 + 嵌入，1.764 几乎退回基线——说明收益主要来自「有选择地拿远处」，不是「多拿几层近处」。
- **各组件的边际贡献**：sigmoid（+0.004，缺竞争归一化、选择不够锐）、multihead（+0.015 相对 Block，深度混合在通道间基本一致）、RMSNorm（Full +0.006、Block +0.013/相对 Block +0.004——块表示累加多个子层、幅度差异更大，更怕没归一化）。唯一「更好」的变体是 input-dependent query（−0.006），但每层多一个 \(d\times d\) 投影且解码需顺序访存，论文因此默认仍用可学习伪查询——**工程代价否决了裸性能**，这是消融表教给设计者的另一课。

配套的 Fig.6 把块大小从 S=1（即 Full，1.737）一路扫到 S=32：S=2/4/8 都落在 ≈1.746，S=16/32 向基线 1.766 退回——「优雅降级」，且实践中因基础设施效率固定取 ≈8 块（§5.3 末尾）。

#### 4.3.2 核心流程

把上述蓝本移植到迷你实验台的五步设计法：

```text
① 定锚点：standard 模式 = 基线（对应 PreNorm 1.766）
          attnres 模式 + 默认组件 = 完整方法（对应 1.737/1.746）
② 定轴：从 Table 4 里选一行作为你的消融轴
          推荐 A：res_norm ∈ {rmsn, none}     （对应 w/o RMSNorm 行）
          推荐 B：block_size ∈ {2,4,8,16}     （对应 Fig.6 扫描）
③ 定控制：除自变量外一切冻结（含参数量校验：两臂差应 ≈ 0）
④ 定噪声底线：3 个种子的基线 σ；效应 < 2σ → 判「不可分辨」（不判「复现失败」）
⑤ 预注册判据 → 跑 → 按报告模板填写（综合实践）
```

一个必须提前算的清醒账：论文效应量只有 0.004–0.03 nat，而字符级迷你模型的种子间标准差通常也在 0.01–0.03 nat 这个量级（**待本地验证**：以你 3 种子基线的实测 σ 为准）。所以迷你复现的合理预期是：**强效应（块数 2 vs 8）大概率可分辨，弱效应（sigmoid 的 +0.004）大概率落进噪声**——这本身就是关于「消融结论的规模依赖性」的有价值发现，写进报告而不是硬凑显著性。

#### 4.3.3 源码精读

Table 4 的每个消融轴都能在 README 伪代码里找到唯一的对应行——这就是「论文结论 ↔ 仓库源码」的最后一块地图：

| Table 4 消融轴 | 伪代码位置 | 改法（迷你版） |
|:---|:---|:---|
| w/o RMSNorm | [README.md:L61-L62](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L62)：`V = torch.stack(...)` 后 `K = norm(V)` | `K = V`（norm 换成恒等） |
| w/ sigmoid | [README.md:L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L64)：`logits.softmax(0)` | `w = torch.sigmoid(logits)`，加权求和时不归一 |
| w/ input-independent mixing | [README.md:L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63)：`einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)` | logits 换成形状 `[n]` 的可学习标量（不依赖 token） |
| w/ input-dependent query | 同上 L63 | `proj.weight.squeeze()` 换成 `proj_q(h)`（由当前隐状态投影，形状 `[b,t,d]`） |
| 块大小 S（Fig.6） | [README.md:L74-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74-L77)：`if self.layer_number % (self.block_size // 2) == 0` | 构造参数 `block_size` 直接改（u2-l4 已参数化） |
| w/ multihead | L63–L64 的 einsum | 把 d 拆成 H 组分别算权重（改动较大，本讲不实践） |

以「w/o RMSNorm + sigmoid」两个轴为例，u2-l4 实验台中 `block_attn_res` 的等价改写（**示例代码**，基于 u2-l4 的实现修改）：

```python
# 示例代码：在 u2-l4 的 block_attn_res 上加两个消融开关
def block_attn_res(blocks, partial_block, proj, norm, agg='softmax', use_norm=True):
    V = torch.stack(blocks + [partial_block])          # [N+1, B, T, D]
    K = norm(V) if use_norm else V                     # 消融轴 1：w/o RMSNorm
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
    if agg == 'softmax':
        h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
    else:                                              # 消融轴 2：w/ sigmoid
        w = torch.sigmoid(logits)                      # 不做竞争归一化
        h = torch.einsum('n b t, n b t d -> b t d', w, V)
    return h
```

注意两处细节：① sigmoid 分支的权重和不再恒为 1，输出幅度会随之变化——这正是论文说它「缺竞争归一化」的可观察面；② `use_norm=False` 时伪查询与未归一化的 V 做内积，块间幅度差异会直接进入 logits——论文对 Block 版去 norm 掉得更多（+0.004 相对 Block）的解释就在这里。

#### 4.3.4 代码实践：两臂冒烟消融

1. **实践目标**：在 u2-l4 实验台上实现 4.3.3 的两个开关，用 1 个种子跑通「attnres（默认）vs attnres（w/o RMSNorm）」两臂，确认改动可用、方向初步可见。
2. **操作步骤**：
   - 按上表修改你 u2-l4 版本的 `block_attn_res`（加 `agg`、`use_norm` 两个参数，默认值保持原行为）；
   - 参数量断言：改动前后 `sum(p.numel() for p in model.parameters())` 应完全不变（这两个消融不增删参数）；
   - 用同一份数据、同一种子（如 seed=0）、同 steps/lr 分别训练 `use_norm=True` 与 `use_norm=False` 两臂（`block_size=4`）。
3. **需要观察的现象**：两条训练/验证损失曲线都能正常下降；观察 w/o RMSNorm 臂是否更差、差多少（nat）。
4. **预期结果**：按论文 Table 4 的方向，去 norm 应变差；但在单种子迷你规模上，差距（若 < 噪声）可能不可见。**待本地验证**——单种子结果只用于检查改动没写坏（曲线不发散），不得用于下结论；下结论要靠综合实践的多种子版。

#### 4.3.5 小练习与答案

**练习 1**：DenseFormer（1.767）几乎没超过基线，而 AttnRes Full（1.737）明显更好。两者都允许访问全部历史输出，差在哪？
**答**：DenseFormer 的跨层系数是**静态的**（训练后固定、与输入无关）；AttnRes 的 \(\alpha_{i\to l}\) 由当前内容经 softmax **动态**产生。论文用这组对照论证「input-dependent 加权」才是收益来源（§5.3 Comparison with prior methods）。

**练习 2**：为什么 Block 版去 RMSNorm（相对 Block +0.004，绝对 1.750）比 Full 版去 RMSNorm（+0.006，绝对 1.743）在论文的叙述里被说成「更依赖归一化」？
**答**：看相对幅度：Full 去掉 norm 损失 1.743 仍优于 Block 原版 1.746；而 Block 去掉后 1.750 跌破了自己的原版。机制上：块表示 \(b_n\) 是整块子层输出的**和**（论文 Eq.5），块与部分和之间的幅度差异比单层输出之间的差异更大，softmax logits 更容易被大幅度块主导——归一化的保护作用更强（§5.3 Component design 原文论证）。

**练习 3**：迷你实验台上，为什么「块大小扫描」比「multihead 消融」更适合作为第一个复现实验？
**答**：三个理由：① 块大小是 u2-l4 已参数化的构造参数（[README.md:L75](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L75) 一行条件），改动零成本，而 multihead 要重写 einsum 的维度语义；② Fig.6 的效应量（1.737→1.766，约 0.03 nat）在 Table 4 里最大，最可能穿过迷你规模的噪声底线；③ 扫描给出的是一条可整体比较的趋势线，单点噪声的影响比单行消融小。

## 5. 综合实践：迷你消融复现报告

**任务**：从论文 Table 4 / Fig.6 中选定**一个**消融结论（推荐「w/o RMSNorm」或「块大小扫描」），在 u2-l4 迷你实验台上设计并执行对应的复现实验，产出一份完整复现报告。这贯穿本讲三个模块：精读（找结论与条件，4.1）→ 分级与预注册（4.2）→ 消融执行（4.3）。

**执行流程**：

1. **选题与预注册**：按 4.2.4 的模板填 `repro_plan.md`。两个推荐选题的假设分别是——
   - RMSNorm 轴：`use_norm=False` 使验证损失变差（方向对齐 Table 4 的 1.746→1.750）；
   - 块大小轴：`block_size ∈ {2,4,8,16}`（即 N=16/8/4/2 块）中，过粗的块（N=2）趋向 standard 基线，中间块数最好（对齐 Fig.6 的「优雅降级」形态）。
2. **噪声底线**：3 个种子跑 standard 基线，记录验证损失均值 ± σ。
3. **正式实验**：每个设置 ≥3 个配对种子（两臂共用种子），同数据、同 steps、同 lr；块大小轴需 4×3=12 次 run（可先用 d=64、较短 steps 控制时长）。
4. **附加观察（可选加分）**：用 u2-l5 的 P1 探针对比「RMSNorm 有/无」或「N=2 vs N=8」的逐层输出幅度曲线——把 A 类「机制复现」与消融捆绑。
5. **写报告**：用下面的模板（这是本讲的最终交付物）。

**复现报告模板**：

```markdown
# 迷你复现报告：<所选结论，如 Block AttnRes 的 RMSNorm 消融>
## 1. 目标结论（论文原句 + 位置）
   例：Table 4「w/o RMSNorm 1.750 vs Block (S=4) 1.746」（§5.3，436M 档）
## 2. 实验设置
   模型：MiniGPT d=64, n_head=?, n_layer=16, block_size=4（32 子层、8 块 —— 与消融档同构）
   数据：字符级语料 <名称/大小>；steps=?, batch=?, block=?, lr=?, 优化器 AdamW
   种子：{0,1,2}；两臂配对
   与论文的已知差异：宽度、数据、KDA/MLA+MoE vs 朴素注意力、Muon vs AdamW、
   零初始化对齐情况（见 4.2.4）
## 3. 结果
   表：设置 × 种子 → 验证损失；均值 ± σ；与基线的差（nat）
   噪声底线：σ_base = ?；判据：|Δ| ≥ 2σ 才声称方向
## 4. 与论文对比
   方向是否一致？效应量比论文大/小/落入噪声？
## 5. 差异原因分析（逐条给证据或合理论证）
   ① 规模差 6–7 个数量级（4.2.1 的 FLOPs 账）
   ② 字符级短程语料 vs 8192-token 网络语料（「远处检索」的价值可能更小）
   ③ 朴素 dense 注意力 vs KDA/MLA 混排 MoE（深度混合的收益与主干表达力耦合）
   ④ AdamW vs Muon、无 WSD 两阶段
   ⑤ 单种子噪声 vs 论文大预算下的小噪声
## 6. 结论
   复现成功/部分成功/不可分辨 + 一句话理由
```

**评判标准**（对齐本讲目标 3、4）：预注册在跑之前完成；多种子；效应量与 2σ 判据一起报告；差异分析至少覆盖上面 ①②⑤ 三条。**待本地验证**：所有数值结果以你的实际运行为准。

## 6. 本讲小结

- **论文是唯一的细节来源**：仓库只有 README（结论）+ 伪代码（结构）+ 4 张图；核函数定义（Eq.2–3）、超参（Table 2）、消融数字（Table 4）、零初始化要求、48B 配方全部只在论文里——精读三遍法 + claim/evidence/detail 三栏表是把这些细节钉死的工具。
- **README 的头条数字可以验算**：1.25× 来自拟合式 \(1.891C^{-0.057}\) 与 \(1.870C^{-0.058}\) 在 \(C=5.6\) 处的等损失换算——精读不止于「读懂」，还能「算对」。
- **复现先分级**：A 类（训练动态、块大小趋势、RMSNorm 消融、零初始化、权重图谱——迷你实验台可复现）/ B 类（Scaling 形态——只能比趋势）/ C 类（48B 下游分数、1.25×、系统开销——预算不可达）；分级依据是 FLOPs 差距（本仓迷你台与消融档差 6–7 个数量级）。
- **消融四铁律**：单因素、同锚点、同预算同超参、先预注册判据后跑；论文 Table 4 的每个消融轴都能映射到伪代码的某一行（L61–64、L75），改动成本极低。
- **诚实报告**：论文效应量 0.004–0.03 nat 与迷你规模的种子噪声同量级——弱效应落进噪声时，正确结论是「不可分辨」而非「复现失败」。

## 7. 下一步学习建议

- **下一讲（u3-l5）**将把视野拉到 Attention Residuals 之外：相关工作谱系（论文 Table 5 的三族方法：单状态递归 / 多流递归 / 跨层访问，DenseFormer、mHC、MUDDFormer、MRLA 等）、序列-深度对偶（§6.1）与深度混合矩阵的结构化视角（§6.2），以及这个方向留给我们自己动手的开放问题。本讲产出的复现报告正好是你判断「哪些改进值得试」的证据基础。
- **继续精读的源码**：论文 §5.4.1 的固定算力架构扫描（5×5 网格，AttnRes 把最优深宽比从 \(d/L_b\approx60\) 推到 \(\approx45\)——AttnRes 让「更深更窄」变得划算）与附录 B 的两阶段推理 I/O 推导，都是 mini 实验台可以延伸的方向（前者可在 u2-l4 台上做 3×3 缩水版扫描）。
- **动手延伸**：把综合实践的块大小扫描扩展成「块数 N 固定 ≈8、深度 L 变化」的实验，直接检验论文「稳定的是块数而非块大小」这一阅读发现（4.2.3 (c)）。
