# Scaling Law 实验解读：1.25 倍计算等效应

## 1. 本讲目标

u2-l4 的对比实验在**单一规模**上回答了「同预算下谁更低」，但它留下了一个致命的开放问题：AttnRes 的优势到底是「小模型上优化更快」，还是「把整条损失-计算曲线往下平移」？前者随规模增大可能消失，后者才会随规模增大兑现成真金白银的算力。README 的证据恰恰是后一种形态：

> [README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99)
> AttnRes consistently outperforms the baseline across all compute budgets. Block AttnRes matches the loss of a baseline trained with **1.25x more compute**.

本讲把这句话拆开读懂、算懂、再动手复现其方法论。学完本讲，你应该能够：

1. **读懂 scaling law 图**：知道 [assets/scaling_law.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/scaling_law.png) 里横轴、纵轴、三条曲线、「1.25×」箭头各自在陈述什么命题。
2. **会拟合幂律**：掌握 \(\mathcal{L}(C) = a\cdot C^{-b}\) 的双对数最小二乘拟合，理解截距 \(a\) 与斜率 \(b\) 各自的含义，并把「曲线下移」换算成「等损失计算量之比」这一计算乘数。
3. **会做计算量对齐**：理解横轴的「货币」是训练 FLOPs（\(C \approx 6ND\)）而非墙钟时间，掌握 iso-step 规模阶梯的设计与两条对比臂的公平性条件。
4. **能跑迷你多规模实验**：在 u2-l4 的实验台上扫 4 个规模的 Standard 与 Block AttnRes 两个系列，拟合各自的幂律、计算迷你版的计算乘数，并诚实判读它与论文结论的关系（趋势复现，而非数字复现）。

本讲是 u2-l4 的直接续篇：那一讲的 `MiniGPT`、`train()`、`evaluate()`、配对种子与参数量手算式全部原样复用，本讲只新增「规模轴」与「拟合层」两件工具。

## 2. 前置知识

### 2.1 幂律与双对数坐标：为什么 scaling law 是一条直线

scaling law（标度律）是深度学习的经验规律：把「最终训练损失」对「训练计算量 \(C\)」画出来，两者近似服从幂律

\[
\mathcal{L}(C) = a \cdot C^{-b}, \qquad a > 0,\; b > 0
\]

其中 \(a\) 控制曲线的**高低**（同等计算下损失多少），\(b\) 控制曲线的**陡缓**（每增加一倍算力，损失再降多少）。对两边取自然对数：

\[
\ln \mathcal{L} = \ln a - b \ln C
\]

在双对数坐标下幂律变成**直线**：斜率为 \(-b\)、截距为 \(\ln a\)。这就是 scaling law 图的标准画法与标准拟合方法——对 \((\ln C,\ \ln \mathcal{L})\) 做一次线性回归即可。论文图里纵轴只显示了约 1.7～1.9 的窄区间，在如此窄的范围内对数纵轴与线性纵轴肉眼几乎一样，但**拟合必须在双对数空间做**，因为拟合的对象是幂律本身。

一个换算直觉：\(b \approx 0.058\) 意味着计算量每翻 10 倍，损失变为原来的 \(10^{-0.058} \approx 0.875\) 倍——收益缓慢但从不停止，这正是「继续堆算力仍然划算」的定量依据。

### 2.2 最小二乘拟合与 \(R^2\)

给定 \(n\) 个点 \((C_i, \mathcal{L}_i)\)，最小二乘法找一条直线使残差平方和最小；`numpy.polyfit(x, y, 1)` 直接返回斜率与截距。拟合质量用 \(R^2\) 度量：

\[
R^2 = 1 - \frac{\sum_i (y_i - \hat{y}_i)^2}{\sum_i (y_i - \bar{y})^2}
\]

\(R^2\) 越接近 1，点越贴近直线。注意：本讲每条曲线只有 3～4 个规模点（论文图约 5 个），拟合自由度很低，它只能当**趋势线**用，不能当作精确物理定律。

### 2.3 FLOPs：横轴的「货币」与 PFLOP/s-day

「计算预算」的通用计量是训练 FLOPs（浮点运算次数），而非墙钟时间——墙钟混入了硬件与实现效率，FLOPs 是模型与数据本身的属性，可复现、可跨机器比较。通行估算：

\[
C \approx 6ND
\]

其中 \(N\) 是参数量，\(D\) 是训练中处理过的 token 总数：前向约 \(2ND\) 次乘加，反向约是前向的两倍（\(4ND\)），合计 \(6ND\)。

论文图的横轴单位是 **PFLOP/s-day**（每秒 1 petaFLOP 持续一天的计算量）：

\[
1\ \text{PFLOP/s-day} = 10^{15} \times 86400 \approx 8.64 \times 10^{19}\ \text{FLOP}
\]

图中数据点大致落在 0.7～5.5 PFLOP/s-day，折合约 \(6\times10^{19} \sim 5\times10^{20}\) FLOPs（读图所得，精确值以论文为准，待确认）。记住这个量级：第 5 节会看到我们的迷你实验台比它小 4～7 个数量级。

### 2.4 与 u2-l4 的衔接：已有资产与本讲缺口

u2-l4 已经交付：字符语料与确定性评测、`mode` 开关共用的 `MiniGPT`（`standard` / `attnres` 两形态仅差残差接线）、带内部配对种子的 `train()`、参数量手算式（每层 \(12d^2+2d\)，attnres 每层再加 \(4d\)）、以及「Δ 必须与种子标准差比较后判读」的纪律。

u2-l4 的练习 3 已经点破本讲的动机：单规模实验**无法区分**「优化更快」与「曲线整体下移」。区分二者必须沿规模轴扫多个点——这正是本讲要补的缺口：一个规模阶梯（4 档 \((L, d)\)）、一套计算量记账、一层幂律拟合。

## 3. 本讲源码地图

本仓库仍是那个只有 6 个文件的论文发布仓库（u1-l1 已确认），没有可运行的实验代码。本讲的策略与 u2-l4 一致：README 的文字与图给出**结论形态与配置依据**，实验设施全部为本讲义编写的「示例代码」，运行在 u2-l4 已搭好的实验台之上。

| 位置 | 内容 | 本讲用途 |
|:---|:---|:---|
| [README.md:L97-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L97-L99) | Scaling Laws 结论：各预算下持续占优；匹配 1.25 倍计算基线 | 4.1 节：两个命题的精确含义与数学化 |
| [README.md:L101-L103](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L101-L103) | 嵌入 `assets/scaling_law.png` | 4.1 节：逐元素读图 |
| [assets/scaling_law.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5a5e6/assets/scaling_law.png) | 损失-计算量曲线（三条幂律 + 1.25× 箭头） | 4.1 节：拟合常数与乘数的读出来源 |
| [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33) | AttnRes 定位：drop-in 替换 | 4.3 节：扫的两个系列共享一切超参的依据 |
| [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) | Block 划分、~8 块、边际开销 | 4.2 节：阶梯的 block_size 配置依据 |
| [README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) | PyTorch 风格伪代码 | 4.3 节：被扫描模型的核心机制出处 |
| [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) | 证据规模：Kimi Linear 48B / 1.4T tokens | 4.3 节：迷你实验的诚实边界 |
| `Attention-Residuals-tutorial/u2-l4-minimal-testbed-training.md` | u2-l4 实验台（模型/训练/评测代码） | 全讲：直接复用的代码基座 |

## 4. 核心概念与源码讲解

本讲的三个最小模块按「先学读图与拟合、再设计横轴、最后上实验台」的顺序展开：

1. **4.1 scaling law 拟合**——把图读懂数学化：幂律拟合与「计算乘数」的推导；
2. **4.2 计算量对齐设置**——让横轴真的在量计算：FLOPs 记账、规模阶梯与公平性条件；
3. **4.3 迷你多规模实验**——把 u2-l4 的配对实验沿规模轴复制成两条幂律。

### 4.1 scaling law 拟合：把「1.25×」从一句话变成一个公式

#### 4.1.1 概念说明

先逐元素读图。[assets/scaling_law.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/scaling_law.png)（嵌于 [README.md:L101-L103](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L101-L103)）画的是：

- **横轴**：训练计算量，单位 PFLOP/s-day，对数刻度，数据点约覆盖 0.7～5.5；
- **纵轴**：损失，约落在 1.7～1.9 的窄区间（这是论文自家数据与分词下的损失，**不可**与我们字符级实验台的损失直接比较）；
- **三条曲线**：Baseline、Full AttnRes、Block AttnRes，每条约 5 个计算档位（星形散点），图例同时给出各自的幂律拟合式——形如 \(\mathcal{L} \approx a\cdot C^{-b}\)，读图可得 \(a\) 约在 1.86～1.89、\(b\) 约在 0.057～0.058（常数从图例读出，精度有限，**以论文为准，待确认**）；
- **一个关键细节**：三条拟合线的指数几乎相同——曲线**近乎平行**，AttnRes 两条只是整体向下平移；
- **「1.25×」箭头**：一条水平箭头从 Baseline 曲线指向 AttnRes 曲线，箭头两端在**同一损失水平**上——它量的不是竖直方向的损失差，而是水平方向「达到同一损失所需的计算量之差」。

这三件事合起来就是 README 那句话的完整数学化。设两条臂的幂律分别为 \(\mathcal{L}_{\text{base}} = a_{\text{base}} C^{-b}\) 与 \(\mathcal{L}_{\text{att}} = a_{\text{att}} C^{-b}\)（共享斜率 \(b\)，因为曲线平行）。所谓「matches the loss of a baseline trained with 1.25x more compute」，即存在计算乘数

\[
M \;=\; \frac{C_{\text{base}}}{C_{\text{att}}} \;=\; \left(\frac{a_{\text{base}}}{a_{\text{att}}}\right)^{1/b} \;\approx\; 1.25
\]

使得对任意损失水平 \(\mathcal{L}^\*\)，基线需要 \(M\) 倍于 AttnRes 的计算量才能达到。推导只需两行：令 \(a_{\text{base}} C_{\text{base}}^{-b} = a_{\text{att}} C_{\text{att}}^{-b}\)，整理即得。

为什么这个换算值得做？因为它把「损失低 0.02」这种难以定价的数字，翻译成工程语言：「**等于白赚 25% 的算力**」。而且它揭示了一个反直觉的关系：同样的损失差距，**曲线越平（\(b\) 越小），换算出的计算乘数越大**——\(M\) 对 \(1/b\) 是指数依赖。这也解释了为什么平行（同 \(b\)）是好消息：只要斜率不变，下移量就凝结成一个与损失水平无关的单一数字 \(M\)。

#### 4.1.2 核心流程

拟合与换算的完整流水线：

```text
输入: 每条臂的 {(C_i, L_i)}   # C = 训练 FLOPs, L = 该预算下的最终验证损失
1. 对 (ln C_i, ln L_i) 做一次线性最小二乘 → 斜率 -b、截距 ln a
2. 计算 R², 检查点是否真的近似一条直线
3. 两条臂分别拟合 → (a_base, b_base), (a_att, b_att)
4. 若 b_base ≈ b_att (曲线平行): 取 b_eff = 平均, M = (a_base/a_att)^(1/b_eff)
   若斜率明显不同: M 依赖损失水平, 须指定参考损失 L* 分别解 C(L*)
5. 报告: 拟合式、R²、M 及其对 b 的敏感性 (b ± std 代入后的 M 区间)
```

第 4 步是本模块最容易忽略的检查：**斜率不同时「计算乘数」不是常数**。两条曲线若在双对数图里不平行，收益会随损失水平（也即随规模）放大或缩小——那时单一数字 \(M\) 会误导，应当改为报告两条拟合线在观测损失区间内的水平间距范围。

#### 4.1.3 源码精读

**两个命题的原文出处**：

> [README.md:L97-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L97-L99)
> `### Scaling Laws`
> AttnRes consistently outperforms the baseline across all compute budgets. Block AttnRes matches the loss of a baseline trained with **1.25x more compute**.

这句话包含两个可分别检验的命题：

- **命题 A（consistently across all compute budgets）**：在**每一个**计算档位上 AttnRes 损失更低——对应图上「整条曲线下移」，而非只在某一档偶然占优。它排除了「小规模下的优化便利随规模消失」的解释。
- **命题 B（matches 1.25x more compute）**：下移的幅度恰好折算成 1.25 倍计算——对应图上的水平箭头与 4.1.1 的乘数公式。

**为什么扫的是 Block 而不只看 Full**：[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 已说明 Full 需要 O(Ld) 显存、Block 以约 8 块恢复大部分收益并保持边际开销；图注 [README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28) 同样点出 Block 把显存从 O(Ld) 降到 O(Nd)。所以论文图同时画 Full 与 Block：Full 是「机制上限」，Block 是「可实用形态」，而 1.25× 的命题明确落在 **Block** 上——这也是本讲迷你实验扫 `mode='attnres'`（即 Block 形态）的原因。

**用读图常数验算一遍乘数**（数值为图例读数、精度有限，仅示范方法）：

\[
M_{\text{block}} = \left(\frac{1.891}{1.870}\right)^{1/0.0575} = e^{\,17.4 \times \ln(1.0112)} \approx e^{0.194} \approx 1.21
\]

\[
M_{\text{full}} = \left(\frac{1.891}{1.865}\right)^{1/0.0575} \approx e^{0.241} \approx 1.27
\]

得到 Block ≈ 1.21×、Full ≈ 1.27×，与 README 的「1.25×」在读图误差内一致（Block 略低于、Full 略高于 1.25，趋势合理——Block 是 Full 的省显存近似）。精确常数与拟合方法（是否固定共享指数、用哪些点）README 未给出，**以论文为准，待确认**。

**证据的规模出处**（后面判读要反复用到）：这些点来自 [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) 标注的 Kimi Linear 48B / 1.4T tokens 量级的训练体系。

#### 4.1.4 代码实践：在合成数据上验证拟合与乘数这把「尺子」

1. **实践目标**：先不训练任何模型，用真值已知的合成幂律数据验证 `fit_power_law` 与 `compute_multiplier` 两个工具能正确恢复参数——这是 u2-l4「先验度量衡再实验」纪律的延续。
2. **操作步骤**：

```python
# 示例代码: 幂律拟合与计算乘数 —— 合成数据自检
import numpy as np

rng = np.random.default_rng(0)

def fit_power_law(computes, losses):
    """L(C) = a * C**(-b): 双对数空间一次线性回归。返回 (a, b, R^2)。"""
    x, y = np.log(np.asarray(computes, float)), np.log(np.asarray(losses, float))
    slope, intercept = np.polyfit(x, y, 1)      # 斜率 = -b, 截距 = ln a
    b, a = -slope, np.exp(intercept)
    pred = intercept + slope * x
    r2 = 1 - np.sum((y - pred) ** 2) / np.sum((y - y.mean()) ** 2)
    return a, b, r2

def compute_multiplier(a_base, a_att, b):
    """等损失水平下, baseline 与 attnres 所需计算量之比 (共享斜率 b)。"""
    return (a_base / a_att) ** (1 / b)

# 合成两条平行下移的幂律, 预设真值 M = 1.25
b_true, a_base, M_true = 0.058, 1.89, 1.25
a_att = a_base / M_true ** b_true          # 保证 (a_base/a_att)^(1/b) = 1.25
C = np.geomspace(0.7, 5.5, 5)              # 模仿论文图的横轴布局
noise = lambda: np.exp(rng.normal(0, 0.003, len(C)))   # 乘性噪声 ~0.3%
L_base = a_base * C ** (-b_true) * noise()
L_att  = a_att  * C ** (-b_true) * noise()

ab, bb, r2b = fit_power_law(C, L_base)
aa, ba, r2a = fit_power_law(C, L_att)
b_eff = (bb + ba) / 2                       # 平行假设: 取公共斜率
print(f"baseline: a={ab:.3f}  b={bb:.4f}  R2={r2b:.4f}")
print(f"attnres : a={aa:.3f}  b={ba:.4f}  R2={r2a:.4f}")
print(f"恢复的计算乘数 M = {compute_multiplier(ab, aa, b_eff):.3f}  (真值 {M_true})")

# 敏感性: 斜率估计不准时 M 波动多大
for b_try in [b_eff - 0.002, b_eff, b_eff + 0.002]:
    print(f"  b={b_try:.4f} -> M={compute_multiplier(ab, aa, b_try):.3f}")
```

3. **需要观察的现象**：两个拟合的 \(a\) 接近 1.89 / 1.866、\(b\) 接近 0.058、\(R^2 > 0.99\)；恢复的 \(M\) 在 1.25 附近（噪声扰动约 ±0.02）；\(b\) 扰动 ±0.002 时 \(M\) 的变化明显大于噪声本身的影响。
4. **预期结果**：给定 seed=0 结果可复现（拟合是确定性的，具体打印值待本地验证）。敏感性一行会显示 \(M\) 对 \(b\) 的指数放大——这正是 4.1.1 强调的：**小规模实验里 \(b\) 估不准，\(M\) 就估不准**，报告时必须给出区间而不是单点。

#### 4.1.5 小练习与答案

**练习 1**：为什么把「损失低 0.02」换算成「计算乘数 1.25×」更有说服力？

> **答案**：损失差的绝对值不可定价（依赖数据、分词、损失口径），而计算乘数直接等价于「达到同损失少花 20% 训练成本」，是硬件与经费层面的通用语言。且在曲线平行（共享 \(b\)）的前提下，\(M\) 与所处损失水平无关，一个数字概括整条曲线的关系。

**练习 2**：若两条拟合线的斜率 \(b\) 明显不同，\(M = (a_{\text{base}}/a_{\text{att}})^{1/b}\) 还成立吗？该怎么办？

> **答案**：不成立——共享 \(b\) 是推导的前提。斜率不同时，两曲线在双对数图里相交或发散，等损失计算比依赖所取的损失水平。正确做法是指定参考损失 \(\mathcal{L}^\*\)，分别从两条拟合式解出 \(C_{\text{base}}(\mathcal{L}^\*)\) 与 \(C_{\text{att}}(\mathcal{L}^\*)\) 再求比值，并报告它在观测损失区间内的变化；斜率差本身就是重要结论（收益随规模增大或缩小）。

**练习 3**：用 4.1.3 的读图常数（\(a_{\text{base}} \approx 1.891\)，\(a_{\text{block}} \approx 1.870\)，\(b \approx 0.0575\)）手算 Block 的计算乘数，并说明它与 1.25 的差可能来自哪里。

> **答案**：\(M = (1.891/1.870)^{1/0.0575} \approx 1.21\)。差距的来源：(i) 图例常数的读数误差——\(M\) 对 \(1/b\) 指数敏感，\(b\) 或 \(a\) 差千分之几就会移动 \(M\) 数个百分 点；(ii) 论文可能用共享指数或不同点集拟合。所以本讲把它当「方法验算一致」，精确值以论文为准（待确认）。

### 4.2 计算量对齐设置：让横轴真的在量「计算」

#### 4.2.1 概念说明

scaling law 实验的第二块基石是把横轴造对。四个设计决定：

**其一，横轴用什么计量。** 用训练 FLOPs 的估算 \(C \approx 6ND\)（2.3 节），不用墙钟时间。理由：(i) 硬件无关、可复现；(ii) 墙钟会把**实现效率**混进「计算量」——比如 attnres 的 `torch.stack` 实现慢一点，墙钟横轴会把它当成「用了更多计算」，而 FLOPs 口径下它只是同样的计算被执行得慢了。两件事都要测量，但必须分开报。

**其二，规模阶梯怎么设计（iso-step 设计）。** 标准做法有两种：

| 设计 | 做法 | 横轴差异来源 | 适用问题 |
|:---|:---|:---|:---|
| **iso-step**（本讲采用） | 4 档 \((L,d)\) 递增，**全部同 steps / B / T**，同数据同配方 | 只来自参数量 \(N\) | 「曲线下移 / 计算乘数」这类**两臂对比** |
| iso-compute | 每档训练到**相同总预算**（大模型少步数） | 预算被强行对齐 | 「固定预算下最优规模/形状」 |

本讲的问题是两臂对比，iso-step 最简单也最贴 u2-l4 的现成设施：每档内两臂看到完全相同的数据流（配对种子），横轴坐标 \(C_i = 6 N_i D\) 由 \(N_i\) 唯一决定。代价是档位分布受 \((L,d)\) 阶梯控制，需要先算一遍记账表确认横轴跨度足够（见 4.2.4）。

**其三，两臂的公平性条件。** scaling 对比的核心公平性条件是：**在每个规模档上，两臂除残差接线外一切相同**——这正是 [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)「drop-in」的含义在实验设计层的投影。u2-l4 用一个 `mode` 开关在代码结构上保证了它；本讲再叠加「同阶梯、同配方」——每档用同一 LR、同 steps 训练两臂。至于 LR 是否对每档都最优，**不影响两臂对比的公平性**（两臂同等「次优」），但会影响拟合出的 \(b\) 的绝对值——解读时要记住这个口径。两臂的 \(C\) 仍有两个微小差别，都要如实记账：参数差（attnres 每层多 \(4d\)，见 u2-l3，占比随规模从约 0.5% 降到 0.2%）与 attn_res 机制本身的 FLOPs 开销（u2-l3 估算约 1% 量级）。合计横轴偏差 ≲2%，远小于要检验的 25% 乘数，方向已知（attnres 的 \(C\) 略大），写进报告即可。

**其四，block_size 沿阶梯怎么设。** 固定 `block_size=4`（k=2，与 u2-l4 对比配置一致）。后果用 u2-l4 的候选数闭式 \(C_{\text{mlp}}(L-1) = \lfloor (L-1)/k \rfloor + 2\) 算清楚：L=8/12/16/24 档的末端块数为 4/6/8/12、末端候选数 5/7/9/13——只有 L=16 档恰好落在 [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)「~8 blocks」的推荐邻域。替代方案是让 block_size 随 L 缩放、把每档末端都钉在约 8 块——但小 L 档会退化成 k=1（Full AttnRes，见 u2-l4 练习 2），机制反而被换掉。本讲选固定 block_size（机制实现简单、档内两臂严格同Config），把「候选数沿阶梯漂移」作为已知混杂因素如实汇报；想消除它，可在同阶梯上补扫 block_size（与 u3-l1 的开销扫描天然衔接）。

#### 4.2.2 核心流程

```text
design_ladder():
    选 4 档 (L, d): 参数量 N 跨度至少一个数量级 → 横轴 C 跨度 ~25x 以上
    每档固定: block_size=4, n_head=d//16, T, B, steps, lr, 语料, 种子集合
    记账: N_base = L(12d²+2d) + 2Vd + T·d + d      # u2-l4 手算式
          N_att  = N_base + 4dL                     # attnres 增量
          C      = 6ND,  D = steps·B·T
    自检: (i) 手算 N == cnt(model);  (ii) 两臂 C 之比 ∈ [1, 1.02]
          (iii) C 沿阶梯单调且跨度足够; (iv) 末端候选数符合闭式
```

#### 4.2.3 源码精读

**「预算」是一等公民的原文证据**：

> [README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99)
> AttnRes consistently outperforms the baseline **across all compute budgets**.

「across all compute budgets」说明对比是在**多个预算点**上做的——这正是本模块设计规模阶梯的依据：单点对比无法支撑「consistently」这个副词。论文图每条曲线约 5 个点，即约 5 个计算档位；迷你实验受算力限制取 4 档。

**被扫描模型的机制出处与配置依据**：两臂的核心差异来自 [README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) 的 `block_attn_res` 与 `forward` 伪代码（u2-l1/u2-l2 已逐行精读、u2-l4 已装配为 `mode='attnres'`），块配置的推荐值来自 [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 的「~8 blocks」。论文各档的具体超参（LR 调度、warmup、数据量是否随规模变）README 未给出——**以论文为准，待确认**；本讲所有超参为示例自定，但**两臂严格相同**。

**规模的诚实声明**：

> [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)
> `### Downstream Performance (Kimi Linear 48B / 3B activated, 1.4T tokens)`

scaling 图与下游表出自同一套大规模训练体系。我们的阶梯最大约 \(1.6\times10^{15}\) FLOPs，与之相差约 4～7 个数量级——这条鸿沟规定了本讲实验「复现方法论与趋势，不复现数字」的定位。

#### 4.2.4 代码实践：算清阶梯的记账表

1. **实践目标**：为 4 档阶梯 \((L,d) \in \{(8,64),(12,96),(16,128),(24,192)\}\) 生成参数量与计算量记账表，验证三个确定性自检：手算参数量与实数相等、两臂 \(C\) 之比小于 1.02、横轴跨度足够。
2. **操作步骤**：

```python
# 示例代码 (需已运行 u2-l4 minitest.py 中的 MiniGPT 与 cnt 定义)
LADDER = [(8, 64), (12, 96), (16, 128), (24, 192)]   # 规模阶梯
T, B, STEPS = 256, 32, 3000
D = STEPS * B * T                    # 每次训练处理的 token 数 = 2.4576e7
PF_DAY = 1e15 * 86400                # 1 PFLOP/s-day 的 FLOPs

def hand_params(L, d, V=65, t_max=T):
    """u2-l4 的手算式: 每层 12d^2+2d, 加 emb/wpe/head/final_norm。"""
    return L * (12 * d * d + 2 * d) + 2 * V * d + t_max * d + d

print(f"{'L':>3} {'d':>4} {'N_base':>10} {'ΔN%':>6} {'C_base(FLOP)':>13} "
      f"{'C(PFD-day)':>11} {'末端块数':>6}")
for L, d in LADDER:
    n_b = hand_params(L, d)
    n_a = n_b + 4 * d * L                      # attnres: 每层 4d (u2-l3)
    c_b = 6 * n_b * D
    n_blocks = (L - 1) // 2 + 1                # k=2: 边界在 0,2,4,... 层
    print(f"{L:>3} {d:>4} {n_b:>10,} {100*(n_a-n_b)/n_b:>5.2f}% "
          f"{c_b:>13.3e} {c_b/PF_DAY:>11.3e} {n_blocks:>6}")

# 自检 1: 手算 vs 实数 (最小档)
torch.manual_seed(0)
m = MiniGPT(V, d=64, n_head=4, n_layer=8, block_size=4,
            mode='standard', t_max=T)
print("手算 N =", hand_params(8, 64), " 实数 N =", cnt(m),
      " 相等:", hand_params(8, 64) == cnt(m))

# 自检 2: 两臂 C 之比 (attnres 还含 ~1% 量级机制 FLOPs, 未计入, 如实汇报)
L, d = 24, 192
print("最大档两臂 C 之比 (仅参数差) =",
      f"{(6*(hand_params(L,d)+4*d*L)*D) / (6*hand_params(L,d)*D):.4f}")

# 自检 3: 横轴跨度
Cs = [6 * hand_params(L, d) * D for L, d in LADDER]
print(f"横轴跨度 = {Cs[-1]/Cs[0]:.1f}x")
```

3. **需要观察的现象**：四行记账表的 \(\Delta N\%\) 从约 0.49% 递减到约 0.17%（u2-l3 的 \(4d/(12d^2)\) 规律）；\(C\) 沿阶梯单调上升，跨度约 25×；手算与实数严格相等；两臂 \(C\) 之比 ≈ 1.002。
4. **预期结果**：三条自检全部是**结构决定的确定性检查**，应严格通过（\(C\) 的具体数值待本地验证）。若手算不等，先核对 `t_max` 是否传了 T、head 是否 `bias=False`。跨度 25× 意味着双对数横轴上四点铺开约 1.4 个十进制位——虽不及论文图的 0.7～5.5 PFD，但足以拟合趋势线。

#### 4.2.5 小练习与答案

**练习 1**：为什么横轴用 FLOPs 估算 \(6ND\) 而不是实测墙钟时间？这个选择对 attnres 尤其重要，为什么？

> **答案**：FLOPs 是模型与数据本身的属性，硬件无关、可复现、可跨论文比较；墙钟混入硬件与实现效率。对 attnres 尤其重要：它的机制开销（`stack` + 两次 `einsum`）在墙钟口径下会被误记成「用了更多计算」，使本应「近乎同预算」的两臂在横轴上被人为拉开；FLOPs 口径把「计算量」与「执行效率」分开，效率差异另行报告（如每步耗时比）。

**练习 2**：iso-step 与 iso-compute 两种设计分别适合回答什么问题？本讲为什么选前者？

> **答案**：iso-compute（每档同总预算，大模型少训步数）回答「固定预算下哪种规模/结构最优」；iso-step（同 steps，预算随规模自然增长）适合**两臂对比**——每档内数据流、步数、配方完全一致，横轴差异只来自参数量，且直接复用 u2-l4 的配对实验设施。代价是无法在同一档内比较不同规模的「性价比」，但那不是本讲的问题。

**练习 3**：固定 `block_size=4` 使各档末端块数为 4/6/8/12。这会混杂什么？如果不介意小档退化成 Full AttnRes，怎么消除它？

> **答案**：混杂「模型规模」与「深度注意力的候选数」两个因素——候选数偏离推荐值 ~8 的档位，attnres 的收益可能被低估（块太少）或开销偏高（块太多），使拟合曲线不能干净归因于规模。消除办法：让 `block_size = 2L/8`（即 k = L/8），把每档末端块数都钉在约 8——但 L=8 档会得 k=1，退化成 Full AttnRes（u2-l4 练习 2）。两种方案都合法，关键是选定后如实汇报候选数轨迹。

### 4.3 迷你多规模实验：在实验台上扫出两条幂律

#### 4.3.1 概念说明

现在把 4.1 的尺子与 4.2 的横轴装到 u2-l4 的实验台上。一次 sweep 的定义：

```text
for 每个规模档 (L, d) in 阶梯:
    for 每个模式 mode in {standard, attnres}:
        for 每个种子 seed in 种子集:
            训练 (同 D、同配方、配对种子) → 记录 final val loss
横坐标: C = 6·N(mode, 档)·D     # 4.2.4 的记账
纵坐标: 该 (档, 模式) 上种子平均的 final val, 误差条 = 种子 std
```

解读规则（对照 4.1 的两个命题）：

- **命题 A 的迷你版**：attnres 的各档均值点是否**每一档**都 ≤ baseline（在 std 范围内）？拟合线是否整体在下？
- **命题 B 的迷你版**：用共享斜率算 \(M\)，看它落在什么区间——**不要期待 1.25**。论文的 1.25 来自 PFLOP/s-day 量级、BPE 分词、整调超参的大模型训练；我们的 \(M\) 只度量「本阶梯、本语料、本配方」上的等效应，其价值在于**方法论跑通 + 方向性证据**。
- **第三个观察点（论文图顺带给出的信息）**：两条拟合线的**斜率差**。若 attnres 的 \(b\) 与 baseline 几乎相同（平行下移），对应论文形态「收益不随规模消失」；若明显更陡或更平，说明在本阶梯内收益随规模变化——这本身就是值得报告的发现。

判读纪律沿用 u2-l4 并加两条：(i) 每档的差距先与该档种子 std 比较，再谈曲线；(ii) 4 个点的拟合只有 2 个自由度，\(M\) 必须报区间（对 \(b\) 的敏感性，见 4.1.4），不报单点。

还有一个值得预期的小规模现象：字符级小模型的幂律指数通常比论文的 0.058 **更陡**（我们在远离损失地板的「易改善」区间，且数据有限），损失绝对值也与论文的 1.7～1.9 不可比——可比的只有**两条曲线的相对关系**。

#### 4.3.2 核心流程

```text
sweep():
    ladder ← 4.2 的阶梯;  RUN ← 同一训练配方 (两臂共用)
    records = []
    for (L, d) in ladder:
        for mode in {standard, attnres}:
            finals = []
            for seed in SEEDS:
                torch.manual_seed(seed); model = MiniGPT(..., mode=mode)
                hist = train(model, ..., seed=seed)   # 内部重设种子 → 数据流配对
                finals.append(hist['val'][-1])
            C = 6 * cnt(model) * D                    # 该 (档, 模式) 的横坐标
            records.append({L, d, mode, C,
                            mean(finals), std(finals), N=cnt(model)})

analyze(records):
    for mode: a, b, R² = fit_power_law(该模式各档 (C, mean))
    b_eff = 两斜率平均 (若接近)
    M = (a_base/a_att)^(1/b_eff);  对 b_eff±std 报 M 区间
    画双对数散点(误差条) + 两条拟合线;  汇总表
```

#### 4.3.3 源码精读

**被扫描的两臂只差残差接线**——公平性的结构保证：

> [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)
> This is the official repository for **Attention Residuals (AttnRes)**, a drop-in replacement for standard residual connections...

u2-l4 用一个 `Block` 类 + `mode` 开关实现了这一点：attnres 臂的核心计算就是 [README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) 伪代码的原样装配。因此本节 sweep 扫出的任何差异，在代码结构上只能来自残差接线（加上每层 \(4d\) 的参数增量与约 1% 的机制 FLOPs——均已记账）。

**结论映射表**——README 命题与迷你实验可观测量的一一对应：

| README 命题（出处） | 论文证据形态 | 迷你实验对应观测量 |
|:---|:---|:---|
| 各预算下持续占优（[README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99)） | 图中 AttnRes 曲线在每个档位低于 Baseline | 各档 attnres 均值 ≤ baseline 均值（含 std），拟合线整体在下 |
| 匹配 1.25 倍计算（[README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99)） | 图中「1.25×」水平箭头 | 共享斜率乘数 \(M=(a_{\text{base}}/a_{\text{att}})^{1/b_{\text{eff}}}\) 及其区间 |
| 曲线平行（图例拟合式，读图待确认） | 三条拟合线指数几乎相同 | 两臂拟合斜率之差与各自 std 的比较 |
| Block 为实用形态（[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)） | 图中同时画 Full 与 Block | 本实验只扫 Block（`mode='attnres'`）；Full 留作扩展 |

**规模的鸿沟**：论文点位于 0.7～5.5 PFLOP/s-day（约 \(6\times10^{19}\sim5\times10^{20}\) FLOPs，读图所得、待确认），出自 48B/1.4T tokens 体系（[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)）；我们的阶梯约 \(6\times10^{13}\sim1.6\times10^{15}\) FLOPs。差 4～7 个数量级——迷你实验检验的是**趋势与方法**，不是数字。

#### 4.3.4 代码实践：pilot——先扫最小的两档

1. **实践目标**：只跑阶梯最小的两档（(8,64) 与 (12,96)）× 两臂 × 2 种子，端到端验证 sweep 流水线（训练→记账→拟合→画图）能跑通、损失沿横轴单调下降——用最小算力先把 bug 清完，再投入全量扫描。
2. **操作步骤**：

```python
# 示例代码 (接 u2-l4 minitest.py 的 MiniGPT / train / cnt 与 4.2.4 的记账)
import numpy as np
import matplotlib.pyplot as plt

RUN = dict(steps=1500, batch=32, block=256, lr=3e-4, eval_every=750)
SEEDS_PILOT = [0, 1]

records = []
for L, d in LADDER[:2]:                          # pilot: 只扫前两档
    for mode in ['standard', 'attnres']:
        finals = []
        for seed in SEEDS_PILOT:
            torch.manual_seed(seed)
            model = MiniGPT(V, d=d, n_head=d // 16, n_layer=L,
                            block_size=4, mode=mode, t_max=RUN['block'])
            hist = train(model, train_ids, val_ids, seed=seed,
                         steps=RUN['steps'], batch=RUN['batch'],
                         block=RUN['block'], lr=RUN['lr'],
                         eval_every=RUN['eval_every'])
            finals.append(hist['val'][-1])
        mean = sum(finals) / len(finals)
        std = (sum((f - mean) ** 2 for f in finals)
               / (len(finals) - 1)) ** 0.5
        records.append(dict(L=L, d=d, mode=mode, N=cnt(model),
                            C=6 * cnt(model) * RUN['steps']
                               * RUN['batch'] * RUN['block'],
                            mean=mean, std=std))
        print(f"L={L:2d} d={d:3d} {mode:9s} final val "
              f"{mean:.4f} ± {std:.4f}")

# 每臂只有 2 个点还拟合不了幂律 —— pilot 只检查:
#   (i) 损失随 C 增大而下降;  (ii) 流水线无报错;  (iii) 差距量级
for mode in ['standard', 'attnres']:
    rs = sorted((r for r in records if r['mode'] == mode), key=lambda r: r['C'])
    print(mode, "单调下降:",
          all(a['mean'] > b['mean'] for a, b in zip(rs, rs[1:])))
```

3. **需要观察的现象**：四个组合全部正常训练（无 NaN、无形状错误）；两臂的损失都随档位升高（\(C\) 增大）而下降；两档上 attnres 与 baseline 的差距均在零点零几以内（小规模 + 短步数下大概率落在 std 内，属预期）。
4. **预期结果**：单调性应成立（规模增大损失降低是幂律的方向）；具体损失值与差距待本地验证。若某臂在最小档就不收敛或 NaN，回到 u2-l4 的三连检排障，不要带病进入全量扫描。

#### 4.3.5 小练习与答案

**练习 1**：为什么 sweep 的纵坐标用 final val（最后一步的验证损失）而不是 best val？

> **答案**：横轴的预算口径是「训练了 \(C = 6ND\) 的计算」，final val 与这个口径严格对应；best val 隐含一次早停选择，各点实际消耗的预算不再可比，还会引入「挑最好点」的乐观偏差。同预算对比必须用同预算读数（可另附 best val 作参考，但主口径是 final）。

**练习 2**：拟合出的 \(b\) 与论文的约 0.058 相差很大，这说明实现有 bug 吗？

> **答案**：不说明。幂律指数依赖规模区间、数据集、分词与配方——字符级小模型处在远比大模型「陡」的区间，指数更大是常态。本实验的检验对象是**两臂的相对关系**（是否下移、乘数量级、是否平行），不是复现论文指数。若两臂在同档内的差距远超 std 且方向稳定，那才是有效信号。

**练习 3**：假设扫出的两条拟合线不平行——attnres 的斜率更陡（\(b\) 更大）。这对「1.25 倍等效应」的解读意味着什么？

> **答案**：意味着收益随规模（沿损失下降方向）在**扩大**，等效应乘数不再是常数：小 \(C\) 端乘数小、大 \(C\) 端乘数大。此时报单点 \(M\) 会误导，应给出观测区间内 \(M(\mathcal{L}^\*)\) 的变化范围；对论文结论而言，这既是好消息（收益在增长）也是需要复核的信息（与论文「平行」形态不同，可能是小规模伪象，也可能是真实差异——如实记录，留给更大规模验证）。

## 5. 综合实践

### 5.1 任务：四档阶梯上的双臂 scaling 扫描

在 pilot 通过后执行完整任务：**用 4 档阶梯 \((8,64)/(12,96)/(16,128)/(24,192)\)、每档两臂 × 3 个种子（算力紧张时大档可减为 2 个，如实注明），训练并拟合两条损失-计算幂律，计算迷你版计算乘数 \(M\) 及其区间，再在最大档做一次「1.25× 计算」的直接对照，撰写简短分析报告。**这是 4.1（尺子）、4.2（横轴）、4.3（流水线）的串联，也是 u2-l4 单规模对比的最终升级。

### 5.2 主脚本

```python
# 示例代码: 综合实践驱动脚本 (复用 u2-l4 minitest.py 与 4.1.4/4.2.4 的定义)
import math
import numpy as np
import matplotlib.pyplot as plt

CFG_RUN = dict(steps=3000, batch=32, block=256, lr=3e-4, eval_every=1000)
SEEDS = [0, 1, 2]

records = []
for L, d in LADDER:
    seeds_here = SEEDS if L <= 16 else SEEDS[:2]      # 大档可减种子, 如实注明
    for mode in ['standard', 'attnres']:
        finals = []
        for seed in seeds_here:
            torch.manual_seed(seed)
            model = MiniGPT(V, d=d, n_head=d // 16, n_layer=L,
                            block_size=4, mode=mode, t_max=CFG_RUN['block'])
            hist = train(model, train_ids, val_ids, seed=seed, **CFG_RUN)
            finals.append(hist['val'][-1])
        mean = sum(finals) / len(finals)
        std = (sum((f - mean) ** 2 for f in finals)
               / (len(finals) - 1)) ** 0.5
        records.append(dict(L=L, d=d, mode=mode, N=cnt(model),
                            C=6 * cnt(model) * CFG_RUN['steps']
                               * CFG_RUN['batch'] * CFG_RUN['block'],
                            mean=mean, std=std, seeds=len(finals)))
        print(f"L={L:2d} d={d:3d} {mode:9s} "
              f"val {mean:.4f} ± {std:.4f}")

# ---- 拟合与画图 ----
plt.figure(figsize=(6.5, 4.2))
fits = {}
for mode, color in [('standard', 'tab:gray'), ('attnres', 'tab:blue')]:
    rs = [r for r in records if r['mode'] == mode]
    Cs = np.array([r['C'] for r in rs])
    Ls = np.array([r['mean'] for r in rs])
    Es = np.array([r['std'] for r in rs])
    a, b, r2 = fit_power_law(Cs, Ls)
    fits[mode] = (a, b, r2)
    plt.errorbar(Cs, Ls, yerr=Es, fmt='*', color=color, capsize=3)
    xs = np.geomspace(Cs.min() * 0.8, Cs.max() * 1.2, 50)
    plt.plot(xs, a * xs ** (-b), '--', color=color,
             label=f"{mode}: L≈{a:.3f}·C^(-{b:.3f})  R2={r2:.3f}")
plt.xscale('log'); plt.xlabel('training FLOPs (C ≈ 6ND)')
plt.ylabel('final val loss'); plt.grid(True, which='both', alpha=0.3)
plt.legend(); plt.tight_layout(); plt.savefig('mini_scaling_law.png', dpi=150)

# ---- 计算乘数与区间 ----
(a_b, b_b, _), (a_a, b_a, _) = fits['standard'], fits['attnres']
b_eff = (b_b + b_a) / 2
print(f"斜率: base {b_b:.4f} vs attnres {b_a:.4f} (平行性检查)")
for b_try in [max(b_eff - 0.01, 1e-3), b_eff, b_eff + 0.01]:
    print(f"  b={b_try:.3f} -> M = {compute_multiplier(a_b, a_a, b_try):.3f}")
```

### 5.3 结果记录表模板

| 档位 (L, d) | N_base | N_att | C (FLOPs) | base val ± std | attnres val ± std | Δ（base−att） | 末端块数 |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| (8, 64) | | | | | | | 4 |
| (12, 96) | | | | | | | 6 |
| (16, 128) | | | | | | | 8 |
| (24, 192) | | | | | | | 12 |
| **拟合** | | | | \(a_{\text{base}}, b_{\text{base}}\) | \(a_{\text{att}}, b_{\text{att}}\) | | |

另记：\(M\) 及其区间、两臂斜率差、每档种子数、每步耗时比、语料与 V、\(D\) 与每档总 FLOPs。

### 5.4 「1.25× 计算」的直接对照（可选但推荐）

拟合给出的 \(M\) 之外，还可以在**最大档**做一次不依赖拟合的直接检验（u2-l4 第 5.5 节的单规模版推广）：

```python
# 示例代码: baseline 训 1.25× 步数, 与 attnres@3000 比较
torch.manual_seed(0)
m = MiniGPT(V, d=192, n_head=12, n_layer=24, block_size=4,
            mode='standard', t_max=256)
hist_long = train(m, train_ids, val_ids, seed=0,
                  steps=int(CFG_RUN['steps'] * 1.25),   # 3750 步 ≈ 1.25C
                  batch=CFG_RUN['batch'], block=CFG_RUN['block'],
                  lr=CFG_RUN['lr'], eval_every=1000)
print("baseline@1.25C final val =", hist_long['val'][-1],
      " vs attnres@1.0C final val =",
      [r for r in records if r['L'] == 24 and r['mode'] == 'attnres'][0]['mean'])
```

若两者接近（甚至 baseline@1.25C 仍略高），即「匹配 1.25 倍计算」在迷你尺度的一个直接回声；若 baseline@1.25C 反超很多，说明本尺度上 attnres 的等效应乘数小于 1.25——与拟合出的 \(M\) 互相印证。结论待本地验证。

### 5.5 预期现象与判读（定性预期，具体数值待本地验证）

1. **单调性**：两臂的 final val 都随 \(C\) 增大而下降，双对数散点近似排成直线（\(R^2\) 希望在 0.9 以上；4 个点、2 自由度，低了也如实报）。
2. **方向性**：attnres 的拟合线**不高于** baseline 是与 README L99 一致的可期待方向；但字符级小规模下，各档差距很可能与 std 同量级——**「无显著差异」是合法结论**，如实写。
3. **斜率**：两臂 \(b\) 之差大概率覆盖不住各自的拟合不确定度（近似平行）；若显著不平行，按 4.3 练习 3 解读并单独报告。
4. **乘数**：\(M\) 的区间大概率在 1.0～1.2 之间（小规模收益通常不足 25%）；报告时与论文的 1.25 并排放置，强调规模与口径差异，不强行对齐。
5. **红线**：任何一档 attnres 显著**差于** baseline 且超 std——先排障（候选数探针、配对种子、末端候选数是否符合闭式）再怀疑结论，排查清单见 u2-l4 第 5.4 节。
6. **成本**：24 次训练（4 档 × 2 臂 × 3 种子，大档减种子则更少）在单张现代 GPU 上预计数小时内完成；CPU 上请把 STEPS 与阶梯整体减半（待本地验证）。

### 5.6 报告检查清单

- [ ] 阶梯记账表（N、C、两臂 \(C\) 之比、末端块数）随报告附上；
- [ ] 两臂超参与数据流完全相同的声明（配对种子在 `train()` 内部重设）；
- [ ] 拟合式、\(R^2\)、双对数图（误差条）；
- [ ] \(M\) 报区间不报单点，并注明 \(b\) 的敏感性；
- [ ] 与论文结论的关系写成「趋势/方向」语言，注明规模差距 4～7 个数量级；
- [ ] attnres 的参数增量（各档 0.17%～0.49%）与机制 FLOPs 开销（约 1%，未计入横轴）如实披露。

## 6. 本讲小结

- **Scaling law 图的三要素读法**：横轴训练计算量（PFLOP/s-day，对数）、纵轴损失、三条近平行幂律曲线；「1.25×」水平箭头量的是**等损失下的计算量之比**，不是竖直损失差（[README.md:L97-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L97-L99)、[assets/scaling_law.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/scaling_law.png)）。
- **拟合与乘数**：\(\mathcal{L}(C)=aC^{-b}\) 在双对数空间是一次线性回归；曲线平行（共享 \(b\)）时，下移量凝结为与损失水平无关的计算乘数 \(M=(a_{\text{base}}/a_{\text{att}})^{1/b}\)，用图例读数验算得 Block ≈ 1.21×、Full ≈ 1.27×，与 1.25 在读数误差内一致（精确以论文为准，待确认）。
- **横轴的造法**：FLOPs 记账 \(C \approx 6ND\)（墙钟混入实现效率，弃用）；iso-step 阶梯（同 steps/B/T，横轴差异只来自 \(N\)）最适合两臂对比；两臂 \(C\) 偏差 ≲2%（参数 +4dL 与约 1% 机制 FLOPs），方向已知、如实记账；固定 block_size=4 使末端块数 4/6/8/12 沿阶梯漂移，是迷你规模下的已知混杂因素。
- **迷你 sweep**：u2-l4 的配对实验沿 4 档阶梯复制，纵坐标统一用 final val（同预算口径），先 pilot 两档再全量；判读依次看各档差距 vs std、拟合线相对位置、斜率平行性、\(M\) 区间。
- **诚实边界**：论文证据在 0.7～5.5 PFLOP/s-day、48B/1.4T tokens 体系（[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)），迷你实验台小 4～7 个数量级且用字符级语料——它检验**趋势与方法**，不复现数字；「无显著差异」是合法结论。
- 单规模的 u2-l4 无法区分「优化更快」与「曲线下移」，本讲的多规模扫描正是那个悬置问题的答案形态——这也是 AttnRes 主张里最有分量的部分：收益不随规模消失，反而折算成恒定的计算乘数。

## 7. 下一步学习建议

- **下一讲 u3-l3（下游评测解读）**：scaling 图回答「训练损失更低」，下一讲看这些损失差在 Kimi Linear 48B 的九项下游基准上兑换成什么（GPQA +7.5、HumanEval +3.1 等），并动手给自己的迷你模型写一个简单下游评测脚本。
- **向后衔接 u3-l4（论文精读与复现路线图）**：本讲所有「待确认」的细节——各档位的模型形状与预算、是否 iso-compute、拟合是否固定共享指数、LR/warmup 配方——都应回到 `Attention_Residuals.pdf` 逐项核对，并把核对结果写进你的复现报告。
- **与 u3-l1 交叉**：把本讲的阶梯当作 u3-l1 显存/耗时基准的模型来源，或在同一阶梯上补扫 block_size（k=1/2/4/8），检验「~8 blocks 恢复大部分收益」在本尺度是否成立——开销侧结论（u3-l1）与收益侧结论（本讲）拼起来才是完整的 N 取舍。
- **动手巩固**：给 sweep 加第三条臂（Full AttnRes，`block_size=2`），复现论文图的三曲线形态；或把阶梯换成「同参数不同形状」（L 翻倍 d 减半），观察残差方式与模型形状的交互。
