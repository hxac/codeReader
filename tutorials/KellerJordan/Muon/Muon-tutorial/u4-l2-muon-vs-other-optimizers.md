# Muon vs AdamW/Shampoo/SOAP：理论背景与基准对比

## 1. 本讲目标

u3-l4 已经把「怎么迁移、怎么调参」打通了；本讲退后一步，回答一个更根本的问题：**Muon 到底比别的优化器好在哪里、好在哪一层证据上**。我们用两条线索交叉验证：一条是**理论**——把 SGD+momentum、AdamW、Shampoo、SOAP、Muon 放进同一个「预处理器」谱系里，用可手推的代数说清它们的亲缘与差别；另一条是**实证**——批判性地阅读 README 的战果清单与外部证据链（speedrun 纪录、124M 参数基准、Kimi 与 essential.ai 的大规模报告）。

读完本讲，你应该能够：

1. **从更新尺度角度解释 Muon 与 AdamW 的本质区别**：两者都是「归一化器 + lr」的尺度不变更新器，差别在归一化的几何——AdamW 逐坐标（对角统计），Muon 整矩阵（奇异谱）；由此推出 lr 量纲、宽度行为、状态显存的三重差异，并用探针实验实测验证。
2. **把 Muon 放进预处理器家族谱系**：证明「瞬时（不累积二阶统计的）Shampoo 更新 = 精确正交化 UV^T」，从而理解 Adam 是对角版、Shampoo/SOAP 是全矩阵累积版、Muon 是全矩阵瞬时版——以及这个「瞬时」省下了多少显存与计算。
3. **批判性阅读 README 的战果与外部证据**：分清「玩具实验 / speedrun / 124M 基准 / 工业规模报告」四级证据的适用范围，能复述大规模使用 Muon 的工程注意事项（参数划分、lr 缩放、权重衰减的位置、更新裁剪、分布式分摊）。
4. **完成一个多种子、三优化器微型基准**：小 transformer 上对照 AdamW、`SingleDeviceMuonWithAuxAdam`、SGD+momentum 各 3 个种子，产出「最终 loss + 到达目标 loss 步数」对比表，并写出 200 字结论。

本讲是 u4 单元的第二讲：u4-l1 用数值实验打开了 `ns_steps` 这个黑盒；本讲把整条 `muon_update` 流水线放回优化器设计的版图里，看它在坐标轴上究竟站在什么位置。

## 2. 前置知识

本讲综合前几讲的多处结论，先按出处快速回顾（细节都在对应讲义里）：

| 已建立的事实 | 出处 | 速查 |
|---|---|---|
| `muon_update` 五步流水线：EMA 动量 → Nesterov → 卷积核展平 → 正交化 → 尺度缩放 | u2-l3 | 末行尺度因子 \(\max(1, m/n)^{0.5}\) |
| 正交化 + 尺度缩放后，更新的 RMS ≈ \(1/\sqrt{n}\)（\(n\) 为列数 / fan-in） | u2-l3 | 相对步长与宽度无关（muP 式） |
| NS 迭代输出 US'V^T，S' 不是精确 1，而是落在以 1 为中心的窄带 | u2-l2 / u4-l1 | u4-l1 实测上界 ≈ 1.202，主体在 ≈[0.68, 1.13] |
| `adam_update` 与 `torch.optim.AdamW` 数学等价（eps 在根号外），默认 betas=(0.9, 0.95)、eps=1e-10 | u3-l1 | 首步偏差修正恰好约掉 \((1-\beta)\) 因子 |
| 参数划分两层判据（ndim 打底 + 语义修正）、muon 组与 adam 组 lr 量纲差约 67 倍 | u3-l4 / u1-l3 | README 配方是本讲基准脚本的蓝本 |
| 分布式 Muon 的参数分片 + all_gather | u3-l3 | 大规模注意事项之一 |

本讲新增的概念工具箱：

| 概念 | 通俗解释 |
|---|---|
| 预处理器（preconditioner） | 优化器里「把梯度变个形再下山」的部件。它定义了你眼中的地形几何：对角预处理器（Adam）把每个坐标轴单独拉伸；全矩阵预处理器（Shampoo）允许坐标轴之间交叉旋转拉伸。 |
| 白化（whitening） | 把二阶统计（各方向的方差/协方差）归一化到「各方向同尺度」。Adam 做对角白化；把矩阵梯度的整条奇异谱拉平则是「谱白化」，正是 Muon 做的事。 |
| 矩阵幂逆（matrix root） | 对矩阵求 \(-\frac{1}{2}\)、\(-\frac{1}{4}\) 次幂这类分数次逆。Shampoo 需要 \(L^{-1/4}GR^{-1/4}\)，通常靠周期性 SVD/特征分解计算——昂贵且数值上需要小心。 |
| 特征基（eigenbasis） | 由对称矩阵特征向量构成的一组正交坐标系。SOAP 的做法是「在 Shampoo 预处理器的特征基里跑 Adam」，特征基只需周期性刷新。 |
| 临界 batch size / 计算最优 batch | 「临界 batch size」指继续加大 batch 边际收益明显下降的拐点；「计算最优 batch」是给定算力下按 scaling law 推出的最优 batch。两者不重合时，优化器的算力利用率就打折。 |
| scaling law（规模定律） | 模型性能随参数量、数据量、算力按幂律变化的经验规律。essential.ai 的结论正是用 scaling law 框架评估 Muon 的效率。 |
| speedrun | 「用最少算力把模型练到指定成绩」的竞赛。Muon 出自 NanoGPT/CIFAR-10 speedrun 社区，README 的多条战果都是 speedrun 纪录。 |
| 多种子对照 | 同一配置换随机种子（初始化 + 数据顺序）重复实验，报告均值与波动。本讲综合实践的基准要求 3 个种子，目的就是把「随机性」从「优化器差异」里剥离。 |

阅读态度提醒：本讲会引用若干论文/博文结论，凡属外部报告的具体数字（如节省算力的百分比），本讲一律只做**定性转述**并标注出处，精确数字请以原文为准——这也是「不编造」原则在综述型讲义里的体现。

## 3. 本讲源码地图

本讲引用两个文件。muon.py 提供三段「对比用的标本」，README 提供证据链的原文：

| 位置 | 内容 | 在本讲中的角色 |
|---|---|---|
| [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) | `muon_update` 全函数 | 4.1 的主角：正交化 + 尺度缩放 |
| [muon.py:L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40) | 尺度缩放一行 | 「lr 以谱范数为单位」的机制来源 |
| [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135) | `adam_update` 全函数 | 4.1 的对照组：对角白化 |
| [muon.py:L24-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L24-L27) | NS 迭代循环 | 4.2 的主角：「免矩阵求逆的谱白化器」 |
| [muon.py:L50-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L50-L53) | `Muon` docstring 算法自述 | 「SGD 动量 + 正交化后处理」的官方定位 |
| [muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61) | lr 单位定义 | 三种 lr 量纲对比的锚点 |
| [muon.py:L267-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267-L269) | muon 分支两行原地更新 | 4.3：「权重衰减并入更新」的代数等价形式 |
| [README.md:L49-L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L49-L51) | Benchmarks 一节 | 124M 参数、四优化器对比的官方入口 |
| [README.md:L53-L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L53-L59) | Accomplishments 五条战果 | 4.3 的「证据清单」 |
| [README.md:L61-L66](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L61-L66) | 学习资源四链接 | 4.3 的外部证据链（Bernstein 推导、Kimi 报告等） |
| [README.md:L3](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L3) | 项目首段两个链接 | Muon 的原始出处（X 帖 + writeup） |

一个先说明的事实：本仓库没有 tests、examples 目录，`muon.py` 也不含 Shampoo/SOAP 实现——**README 的链接就是这个项目的「示例与基准」**，所以本讲把 README 当作第一手源码来精读；凡涉及 Shampoo/SOAP 的描述均属文献背景，不在本仓库源码内（会明确标注）。

## 4. 核心概念与源码讲解

### 4.1 更新尺度的三分法：SGD+momentum、AdamW、Muon 并排对比

#### 4.1.1 概念说明

学习目标 1 说「从更新尺度角度解释 Muon 与 AdamW 的本质区别」。这句话的切入点是：**把优化器拆成「归一化器 + 学习率」两件套**。更新施加到权重上永远是同一副样子（\(\Delta p = -\text{lr}\cdot u\)，u 是更新方向），优化器的个性全在「u 怎么造」：

- **SGD+momentum：没有归一化器。** u 就是（Nesterov 加权的）动量本身，更新幅度完全继承梯度的原始尺度——梯度大的层走大步、梯度小的层走小步，梯度整体消失时寸步难行。
- **AdamW：对角归一化器。** u 逐元素除以梯度平方的历史均值 \(\sqrt{\hat v_i}\)，每个坐标的更新幅度被拉到约 ±lr。它对「逐坐标尺度」免疫，但**不做任何跨坐标的混合**。
- **Muon：整矩阵谱归一化器。** u 先过动量、再做正交化（奇异谱拉平到 1 附近的窄带），每步对权重矩阵的最大扰动（谱范数）约为 \(\text{lr}\times 1.2\)。它对「整矩阵的尺度」免疫。

「本质区别」由此可以一句话说死：**AdamW 用对角统计归一化逐坐标的幅度，Muon 用瞬时奇异谱归一化整矩阵的形状**。前者维护一份逐坐标的方差记忆（\(v\)），后者干脆不维护二阶记忆——每步当场白化。

这条分界线引出三个可检验的推论，构成本模块的分析框架：

1. **lr 量纲不同**：AdamW 的 lr 单位是「逐坐标更新幅度」，Muon 的 lr 单位是「每次更新的谱范数」（[muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61) 原文："The learning rate, in units of spectral norm per update."）。两套数字不可互抄——u3-l4 量过，默认值差约 67 倍。
2. **宽度行为不同**：设权重矩阵为 \((m, n)\)、\(n\) 为 fan-in。标准初始化的权重 RMS 约为 \(1/\sqrt{n}\)。AdamW 的更新 RMS ≈ lr（与形状无关）→ 权重越宽，**相对**步长越小，换宽度要重调 lr（muP 要解决的就是这件事）；Muon 经 [muon.py:L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40) 的尺度因子后更新 RMS ≈ \(1/\sqrt{n}\)（u2-l3 推导），与权重尺度**同步**缩放 → 固定 lr 的相对步长近似与宽度无关（README 的 muP 说法，[README.md:L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L47)）。
3. **状态显存不同**：SGD+momentum 存 1 份动量，AdamW 存 2 份（一阶 + 二阶），Muon 只存 1 份动量——正交化每步现算，不留统计状态。大规模训练里这是实打实的显存账。

#### 4.1.2 核心流程

三个更新器并排写成公式（施加前，lr 单独相乘）：

\[
\begin{aligned}
\text{SGD+M:}\quad & u = \beta m_{t-1} + (1-\beta)\,g_t, & \text{RMS}(u) &\propto \text{RMS}(g) \\
\text{AdamW:}\quad & u = \hat m_t \oslash (\sqrt{\hat v_t}+\varepsilon), & \text{RMS}(u) &\approx 1 \text{（逐元素}\approx\pm1\text{）} \\
\text{Muon:}\quad & u = \mathrm{NS}(\tilde m_t)\cdot\max\!\left(1, \tfrac{m}{n}\right)^{1/2}, & \text{RMS}(u) &\approx \tfrac{1}{\sqrt{n}}
\end{aligned}
\]

其中 \(\tilde m_t\) 是（Nesterov 之后的）动量，NS 输出的奇异值落在 ≈[0.68, 1.20] 的窄带（u4-l1 结论）。特别值得玩味的是**首步行为**——动量和方差统计都还没起步，三种规则的「裸形态」看得最清楚：

- AdamW 首步：\(m_1 = (1-\beta_1)g\)，\(v_1 = (1-\beta_2)g^2\)，偏差修正后 \(\hat m_1 = g\)、\(\hat v_1 = g^2\)，于是 \(u = g/(|g|+\varepsilon) \approx \mathrm{sign}(g)\)。**逐元素取符号，尺度信息全部丢弃。**
- Muon 首步：动量是 \((1-\beta)\) 乘梯度，Nesterov 再揉一下，但正交化对整体缩放不变（u2-l3），\(u = \mathrm{NS}(g)\)。**方向配对保留，谱被拉平，尺度同样全部丢弃。**
- SGD+M 首步：\(u = (1-\beta)g\)。**尺度原样保留。**

探针实验流程（4.1.4 会跑）：同一批梯度，喂给三种规则 → 量更新的 RMS → 变化 (a) 梯度整体尺度 ×100，(b) 形状从 (64,64) 到 (64,256)——预言：AdamW 的 RMS 只对 (b) 无反应、对 (a) 无反应、恒 ≈1；Muon 的 RMS 对 (a) 无反应、对 (b) 按 \(1/\sqrt{n}\) 变；SGD 的 RMS 对 (a) 线性放大、对 (b) 无反应。

#### 4.1.3 源码精读

两个六行函数并排（左边 [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41)，右边 [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135)）：

```python
def muon_update(grad, momentum, beta=0.95, ns_steps=5, nesterov=True):   # 谱归一化
    momentum.lerp_(grad, 1 - beta)                # 一阶动量：方向记忆
    update = grad.lerp_(momentum, beta) if nesterov else momentum
    if update.ndim == 4:
        update = update.view(len(update), -1)     # 卷积核压成矩阵
    update = zeropower_via_newtonschulz5(update, steps=ns_steps)   # 谱白化
    update *= max(1, update.size(-2) / update.size(-1))**0.5       # 对齐 fan-in 尺度
    return update

def adam_update(grad, buf1, buf2, step, betas, eps):                # 对角归一化
    buf1.lerp_(grad, 1 - betas[0])                # 一阶动量
    buf2.lerp_(grad.square(), 1 - betas[1])       # 二阶动量：逐坐标方差记忆
    buf1c = buf1 / (1 - betas[0]**step)           # 偏差修正
    buf2c = buf2 / (1 - betas[1]**step)
    return buf1c / (buf2c.sqrt() + eps)           # 对角白化
```

逐行对照着读，同构与分岔一目了然：

- **前两行同构**：两者都先维护一阶动量（`lerp_` 的 EMA）。Muon 的动量与 SGD 完全同源（u2-l3 证明只差一个常数因子，还会被正交化抹掉）——**Muon 的新意不在动量，在动量之后那一步**。
- **分岔在第三行**：AdamW 开始维护第二份记忆 `buf2`（逐坐标平方的 EMA）；Muon 没有二阶记忆，直接进入 `zeropower_via_newtonschulz5`（[muon.py:L39](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39)）。
- **归一化器的形状**：AdamW 的 `buf1c / (buf2c.sqrt() + eps)`（[muon.py:L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L135)）是**逐元素除法**——每个坐标只看自己的历史；Muon 的 NS 迭代（[muon.py:L24-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L24-L27)）是**矩阵乘法**——`A = X @ X.mT` 把整条奇异谱卷进来一起算。
- **尺度因子是 Muon 特有的收尾**：[muon.py:L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40) 的 `max(1, m/n)**0.5` 把更新 RMS 统一到 \(1/\sqrt{n}\)。没有这一行，转置形状（\(m>n\)）的更新会偏小 \(1/\sqrt{m/n}\) 倍——这正是 u2-l3 讲过的「尺度行修复」提交所解决的问题。
- **对梯度尺度的免疫方式**：AdamW 靠分母里的 \(\sqrt{\hat v}\)「跟踪」梯度尺度；Muon 靠 NS 入口的 Frobenius 归一化（[muon.py:L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L22)）当场除掉——一个需要历史，一个不需要。

最后把三家的对比收进一张总表（贯穿本讲的「速查卡」）：

| 维度 | SGD+momentum | AdamW（`adam_update`） | Muon（`muon_update`） |
|---|---|---|---|
| 归一化器 | 无 | 对角：\(\sqrt{\hat v_i}\) | 谱：奇异值拉平到 ≈[0.68, 1.20] |
| 梯度整体尺度 | 完全继承 | 免疫（靠历史 v） | 免疫（靠当场归一化） |
| 更新 RMS（首步） | \((1-\beta)\,\text{RMS}(g)\) | ≈1（=sign(g)） | ≈\(1/\sqrt{n}\) |
| lr 量纲 | 梯度单位 | 逐坐标幅度 | 谱范数（[muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61)） |
| 宽度增大时相对步长 | 随梯度尺度漂移 | 缩小（需重调 lr） | ≈不变（muP 式） |
| 每参数状态显存 | 1× | 2× | 1× |
| 每步额外计算 | 逐元素 | 逐元素 | +NS：约 15 次矩阵乘（bf16） |

#### 4.1.4 代码实践

**实践目标**：用探针实验实测上表的「更新 RMS」一行，亲手验证三种归一化器的尺度行为。

**操作步骤**：在自己的练习目录创建 `rms_probe.py`（需要先 `pip install git+https://github.com/KellerJordan/Muon`，或 `sys.path` 指到仓库根目录）：

```python
# 示例代码：rms_probe.py —— 三种更新规则的尺度探针
import torch
from muon import muon_update, adam_update

def probe(shape, scale, beta=0.95):
    torch.manual_seed(0)
    g = torch.randn(shape) * scale

    muon_u = muon_update(g.clone(), torch.zeros_like(g))   # g 会被原地改写（u2-l3），先 clone
    adam_u = adam_update(g.clone(), torch.zeros_like(g),   # 首步：step=1
                         torch.zeros_like(g), 1, (0.9, 0.95), 1e-10)
    sgd_u = (1 - beta) * g                                  # 首步动量（简化，不含 Nesterov）

    rms = lambda u: u.pow(2).mean().sqrt().item()
    print(f"{str(shape):12s} scale={scale:<5}"
          f"  muon_rms={rms(muon_u):.4f}  adam_rms={rms(adam_u):.4f}  sgd_rms={rms(sgd_u):.4f}")

for shape in [(64, 64), (64, 256), (256, 64)]:
    for scale in [0.1, 1.0, 10.0]:
        probe(shape, scale)
```

**需要观察的现象**：9 组输出的三个 RMS 列各自对「scale 变化」和「形状变化」的反应。

**预期结果**（方向性，**待本地验证**）：

- `muon_rms` 只随形状变：(64,64)≈0.12、(64,256)≈0.06、(256,64)≈0.12（即 \(1/8, 1/16, 1/8\)，误差约 ±15% 来自 u4-l1 的奇异值带宽与低尾），与 scale 无关；
- `adam_rms` 恒 ≈1.0，与形状、scale 都无关（首步即 sign(g)）；
- `sgd_rms` 严格随 scale 线性（0.005/0.05/0.5 附近，首步系数 \((1-\beta)=0.05\)）。

若把 `scale` 换成 1e6 再跑，Muon/AdamW 列应纹丝不动（AdamW 分母被 eps=1e-10 影响前的量级仍远大于 eps），SGD 列爆炸——这就是「没有归一化器」的代价。

#### 4.1.5 小练习与答案

**练习 1**：证明 AdamW 首步更新恰为 sign(g)（逐元素），并说明这为什么意味着「梯度尺度信息被完全丢弃」。

**答案**：`adam_update` 中 step=1 时 `buf1=(1-β1)g`、`buf2=(1-β2)g²`；偏差修正分别除以 `1-β1`、`1-β2` 后得 `buf1c=g`、`buf2c=g²`，于是返回 `g/(|g|+eps)`（[muon.py:L131-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L131-L135)）。当 |g|≫eps 时逐元素即为 ±1：输出只含符号，不含幅度——尺度信息只剩分母边缘那一小撮接近 eps 的元素。

**练习 2**：把 AdamW 调好的 lr=3e-4 直接抄给 Muon 组，按本讲的 RMS 公式估算会发生什么。

**答案**：以 (64,256) 的隐藏权重为例（n=256，权重初始 RMS≈1/16≈0.0625）：AdamW 更新 RMS=3e-4，相对步长约 0.5%；Muon 更新 RMS=3e-4/16≈1.9e-5，相对步长约 0.03%——比合理值（lr=0.02 时约 2%）小两个数量级，训练几乎原地踏步。这就是 u3-l4 强调「两组 lr 量纲差 67 倍、不可互抄」的尺度来源。

**练习 3**：为什么说 SGD+momentum 对「层与层之间的尺度差」最敏感？这对深度网络意味着什么？

**答案**：SGD 的更新 ∝ 梯度本身，而深度网络里各层梯度的 Frobenius 范数可以差几个数量级（接近输出层的层梯度大、靠输入侧的小，还随损失锐度漂移）——同一个 lr 意味着有的层迈大步、有的层几乎冻结。AdamW/Muon 的归一化器把每层更新拉回统一量纲，正是它们「免逐层调 lr」的根源。

### 4.2 预处理器家族谱系：Adam（对角）、Shampoo/SOAP（全矩阵）、Muon（瞬时全矩阵）

#### 4.2.1 概念说明

本模块回答「正交化在高维矩阵更新中的理论优势」——但把它放进家族谱系里看才清楚。以下 Shampoo/SOAP 的描述属**文献背景**（本仓库源码不含其实现），出处见 README 的 Benchmarks 一节（[README.md:L49-L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L49-L51)）与所链基准目录。

谱系树（从「二阶统计用什么形状、是否累积」两个维度切）：

- **Adam（2014）**：对角预处理器。累积每个坐标的梯度平方 \(v_i\)，更新逐坐标除以 \(\sqrt{\hat v_i}\)。便宜、稳，但**假设坐标轴本身就是好的下山方向**——坐标之间没有任何交叉项。
- **Shampoo（2018）**：全矩阵预处理器。对 \((m, n)\) 权重的梯度累积两侧 Gram 统计 \(L_t\)（\(m\times m\)，\(\approx\sum GG^\top\)）与 \(R_t\)（\(n\times n\)），更新取 \(L^{-1/4} G R^{-1/4}\)。它能「旋转着」拉伸地形，理论更强，但要为每个矩阵存两块二次方显存，还要周期性做 \(-\frac14\) 次幂逆（SVD/特征分解），贵而重。
- **SOAP（2024）**：Shampoo 的 Adam 化——在 Shampoo 预处理器的**特征基**里跑 Adam，特征基只需周期性刷新。收敛质量看齐甚至超过 Shampoo，开销更低；README 链接的 124M 基准（[README.md:L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L51)）把它和 AdamW、Shampoo、Muon 放在同一起跑线上。
- **Muon（本仓库）**：全矩阵、但**瞬时**——不累积任何二阶统计，每步把动量矩阵当场白化（正交化）。它站在 Shampoo 的「全矩阵」一侧和 Adam 的「便宜」一侧的交点上。

「理论优势」的两种流行解释（分别对应 README 链接的两篇外部文章：Keller Jordan 的 writeup [README.md:L3](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L3) 与 Jeremy Bernstein 的推导 [README.md:L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L64)）：

1. **谱范数约束下的最速下降**（Bernstein）：若限制每步更新的谱范数 \(\|\Delta W\|_2 \le \rho\)（即限制对层输出的**最坏情况**扰动），最速下降方向就是把梯度正交化后乘 \(\rho\)。Adam 约束的是逐坐标幅度，与「层输出最多被扰动多少」并不对齐。
2. **方向能量的均衡**：矩阵梯度的奇异谱通常衰减很快（信息集中在少数方向）。Adam 的 sign 更新拉平了元素分布，却仍可能让更新的能量偏向少数强方向；正交化把单位能量摊到全部 \(\min(m,n)\) 个方向上——在高维矩阵上「雨露均沾」地前进，通常更快提升权重的有效秩利用。

#### 4.2.2 核心流程

本模块的核心是一个**可以手推的代数事实**：把 Shampoo 的二阶统计改为「只用当前这一步」（不累积、不 EMA，相当于 \(\beta_2=0\)），更新就精确等于正交化。设梯度 \(G = U\Sigma V^\top\)（SVD），取 \(L = GG^\top\)、\(R = G^\top G\)：

\[
L = GG^\top = U\Sigma^2U^\top, \qquad R = G^\top G = V\Sigma^2V^\top,
\]

\[
L^{-\frac12}\,G\,R^{-\frac12}
= \left(U\Sigma^{-1}U^\top\right)\left(U\Sigma V^\top\right)\left(V\Sigma^{-1}V^\top\right)
= U V^\top .
\]

也就是说：

\[
\boxed{\text{瞬时全矩阵 Shampoo} = \text{精确正交化} = \text{Muon 想近似的目标}}
\]

Muon 与「精确目标」之间只差 NS 迭代的近似（奇异值落在 [0.68, 1.20] 的带，u4-l1），与「Shampoo」之间差着一个 \(-\frac12\) vs \(-\frac14\) 次幂、以及「瞬时 vs 累积」——顺带一提，Adam 恰好是对角版本的「累积白化」。整个家族于是排成一张谱：

| 优化器 | 二阶统计形状 | 累积？ | 需要的重计算 | 每参数额外显存 | 一句话定位 |
|---|---|---|---|---|---|
| SGD+M | 无 | — | 逐元素 | 1×（动量） | 无预处理器基线 |
| AdamW | 对角 \(v_i\) | 是 | 逐元素除 | 2× | 对角白化 |
| Shampoo | 全矩阵 L、R | 是 | \(-\frac14\) 次幂逆（周期 SVD） | \(O(m^2+n^2)\) | 累积全矩阵白化 |
| SOAP | 全矩阵（特征基中） | 是 | 周期特征分解 + 基内 Adam | 介于两者 | Shampoo 的 Adam 化 |
| Muon | 全矩阵（瞬时） | **否** | NS 正交化（纯矩阵乘，bf16 稳定） | 1×（动量） | 瞬时全矩阵白化 |

「瞬时」换来两笔账：显存从 \(O(m^2+n^2)\) 降到 1×动量；计算从「周期性 SVD + 逐元素」变成「每步约 15 次 bf16 矩阵乘」（NS 五步、每步 3 次，[muon.py:L24-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L24-L27)）。代价是丢掉了跨步的曲率记忆——这就是谱系里各成员真正的取舍。

#### 4.2.3 源码精读

回到源码，看 Muon 是怎样把「矩阵幂逆」这个重活换掉的。NS 迭代循环（[muon.py:L24-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L24-L27)）：

```python
for _ in range(steps):
    A = X @ X.mT                    # Gram 矩阵：瞬时二阶统计
    B = b * A + c * A @ A           # 五次多项式组合
    X = a * X + B @ X               # 只用矩阵乘逼近 X 的"零次幂"
```

三个关键点对照谱系：

- `A = X @ X.mT` 就是瞬时 Shampoo 统计 \(GG^\top\) 的同款 Gram 矩阵——**Muon 并没有绕开二阶信息，它只是不累积**。u2-l2 证明过：迭代对每个奇异值独立施加 \(h(\sigma)=3.4445\sigma-4.7750\sigma^3+2.0315\sigma^5\)，把谱推向 1，而奇异向量（方向结构）精确不变。
- 循环体**只有矩阵乘**——这正是 docstring 强调的工程优势（[muon.py:L52-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L52-L53)）："For efficient orthogonalization we use a Newton-Schulz iteration, which has the advantage that it can be stably run in bfloat16 on the GPU." 对照 Shampoo/SOAP 依赖的 SVD/特征分解：低精度下既慢又容易碎。
- 整个 `zeropower_via_newtonschulz5` **无状态**（[muon.py:L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31)）：不写 self.state、不留缓冲，纯粹「矩阵进、矩阵出」。「瞬时」二字在代码层面就体现为函数签名里根本没有累积参数——与 `adam_update` 必须由调用方长期携带 `buf1/buf2`（[muon.py:L130-L132](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L132)）形成鲜明对照。

理论定位的原始出处两处：`Muon` docstring 第一句的自我介绍（[muon.py:L46-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L46-L53)："Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-processing step"）；以及 README 首段链接的 writeup（[README.md:L3](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L3)）与 Bernstein 的推导博文（[README.md:L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L64)）。四优化器的实测对比入口在 [README.md:L49-L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L49-L51)：modded-nanogpt 仓库的 102924_Optimizers 记录目录，训练的是 124M 参数 transformer——那是把本节的谱系表「落到实数」的地方。

#### 4.2.4 代码实践

**实践目标**：数值验证 4.2.2 的代数恒等式——「瞬时 Shampoo 更新 = UV^T」，并量出 NS 输出与精确正交化的差距。

**操作步骤**：创建 `instant_shampoo.py`：

```python
# 示例代码：instant_shampoo.py —— 验证 瞬时Shampoo == UV^T == NS 的近似目标
import torch
from muon import zeropower_via_newtonschulz5

torch.manual_seed(0)
G = torch.randn(256, 256, dtype=torch.float64)

U, S, Vh = torch.linalg.svd(G)          # 精确正交化目标
UVt = U @ Vh

def inv_sqrt(M):                        # M 对称半正定，求 M^{-1/2}（用特征分解，仅实验用）
    w, Q = torch.linalg.eigh(M)
    return Q @ torch.diag(w.clamp_min(1e-12).rsqrt()) @ Q.mT

Lm, Rm = inv_sqrt(G @ G.mT), inv_sqrt(G.mT @ G)     # 瞬时 Shampoo 的两侧统计
inst = Lm @ G @ Rm

print("‖瞬时Shampoo − UVᵀ‖_max =", (inst - UVt).abs().max().item())
Qns = zeropower_via_newtonschulz5(G.float(), steps=5)
print("‖NS输出    − UVᵀ‖_max =", (Qns.double() - UVt).abs().max().item())
sv = torch.linalg.svdvals(Qns)
print(f"NS 奇异值范围: [{sv.min():.3f}, {sv.max():.3f}]（u4-l1 预言主体在 [0.68, 1.13]）")
```

**需要观察的现象**：两个 max 差值的数量级差——第一个应是浮点噪声，第二个应比第一个大好几个数量级但仍然「小」。

**预期结果**（**待本地验证**）：`‖瞬时Shampoo − UVᵀ‖` ≈ 1e-12~1e-14（纯浮点误差，恒等式成立）；`‖NS − UVᵀ‖` ≈ 0.1~0.4 量级（来自奇异值带 0.68~1.20 偏离 1 的部分）；NS 奇异值最小值可能有低于 0.68 的低尾（u4-l1 结论 4：未爬完坡的小奇异值）。这个实验同时验证了 4.2.2 的代数与 u4-l1 的动力学分析。

#### 4.2.5 小练习与答案

**练习 1**：不查资料，从 SVD 出发复证 \(L^{-1/2}GR^{-1/2} = UV^\top\)（\(L=GG^\top\)，\(R=G^\top G\)）。

**答案**：代入 \(G=U\Sigma V^\top\)：\(L=U\Sigma^2U^\top \Rightarrow L^{-1/2}=U\Sigma^{-1}U^\top\)；\(R=V\Sigma^2V^\top \Rightarrow R^{-1/2}=V\Sigma^{-1}V^\top\)。三因子相乘时 \(U^\top U=I\)、\(V^\top V=I\) 相消，得 \(U\Sigma^{-1}\Sigma\Sigma^{-1}V^\top=UV^\top\)。要点：\(-\frac12\) 次幂恰好把两侧奇异值同时归一，故谱被完全拉平。

**练习 2**：Shampoo 用 \(-\frac14\) 次幂（两侧合计 \(-\frac12\)），瞬时版却等价于两侧各 \(-\frac12\)（合计 \(-1\)）。这矛盾吗？

**答案**：不矛盾。Shampoo 的 \(L_t\)、\(R_t\) 是**累积**统计（许多步 Gram 的和），单侧 \(-\frac14\) 是设计者折中「白化强度 vs 噪声/稳定性」的选择；瞬时版只有一个样本的 \(GG^\top\)，两侧各 \(-\frac12\) 才把这一步的谱完全拉平。幂次与累积窗口是同一根旋钮的两端，Muon 选择了「不累积 + 完全白化」。

**练习 3**：按谱系表估算：一个 d_model=4096、120 层的 transformer（每层约 12 个 \((4096,4096)\) 矩阵），Shampoo 式预处理器要多存多少显存？Muon 呢？

**答案**：Shampoo 每矩阵存 L、R 两块 \(4096^2\)（合计约 2×16.8M×4B≈134MB/矩阵，仅 fp32 统计），120 层 × 12 矩阵 ≈ 16GB 量级的**优化器额外**统计（还不含动量）；Muon 的二阶统计为 0，只多存每参数 1 份动量（与 SGD 相同），NS 中间量用完即弃。这就是「瞬时」二字在大规模训练里的分量（具体数字按实际层数/形状复核）。

### 4.3 实证证据链：README 战果、124M 基准与大规模训练报告

#### 4.3.1 概念说明

理论说「正交化应该有优势」，但优化器的结论最终要靠实验裁决。本模块教你怎么**分级读证据**。把 README 里出现过的所有实证材料按可信规模排成四级：

| 证据等级 | 材料 | 规模 | 能回答什么 / 不能回答什么 |
|---|---|---|---|
| Ⅰ 玩具实验 | 本手册各讲的 toy 脚本 | 数千参数 | 机制验证（如 4.1.4 的 RMS 探针）；不能外推性能结论 |
| Ⅱ speedrun 纪录 | CIFAR-10 airbench、NanoGPT 记录（[README.md:L38-L42](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L38-L42)、[README.md:L53-L57](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L53-L57)） | 10M~160M | 「能否更快练到指定成绩」；但 speedrun 是极端调参的竞赛，超参不通用 |
| Ⅲ 受控基准 | modded-nanogpt 的 102924_Optimizers 目录（[README.md:L49-L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L49-L51)） | 124M transformer | AdamW vs Shampoo vs SOAP vs Muon 的同台对比，最接近「公平比较」 |
| Ⅳ 工业规模报告 | Kimi/Moonlight 技术报告（[README.md:L58](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L58)、[README.md:L65](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L65)）、essential.ai 研究（[README.md:L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L59)） | 3B+ MoE / scaling law | 「大规模是否仍成立、有哪些工程坑」；不是受控实验，混杂因素多 |

读法：**结论沿链条向上或向下传播时都要打折**。玩具上 Muon 赢不了 124M 上未必输；speedrun 赢了不代表默认超参就能赢。本讲的综合实践产出的是第 Ⅰ 级证据——它的价值在机制层（更新尺度的预言是否兑现），不在「Muon 比 AdamW 快 x%」这种性能句式。

#### 4.3.2 核心流程

把 README 五条战果（[README.md:L53-L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L53-L59)）读成一条证据链的流程：

```text
1. 定位声明类型：纪录（可复现入口）？论文（arXiv 编号）？推文（线索）？
2. 查规模与对照物：多大的模型、跟谁比、什么指标
3. 归入四级证据
4. 提取可复用结论 + 工程注意事项
5. 与本地机制实验（4.1/4.2）互相印证或存疑
```

第 4 步沉淀出的大规模注意事项清单（本讲学习目标 3，逐条注明出处）：

1. **参数划分**：embedding / 输出头 / 一维参数默认交给 AdamW（[muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56)）；大规模报告把 Muon 扩展到 embedding/输出头等全部矩阵参数时，配合了额外的尺度处理（见第 3 条），不是无脑换。
2. **lr 缩放**：Muon 的 lr 以谱范数为单位、自带 muP 式宽度无关性（[README.md:L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L47)、u2-l3 的尺度因子推导）；AdamW 侧扩宽度仍需按 muP 调 lr；两套 lr 不可互抄（67×）。混合优化器里两组要**各自**调。
3. **权重衰减的位置与更新裁剪**：Kimi/Moonlight 报告（[README.md:L65](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L65)，arXiv 2502.16982）在大规模使用时做的两处修改——把权重衰减并入更新、对更新的 RMS 做上限裁剪——其中前者的代数形式与本仓库的两行原地更新**等价**（4.3.3 详证）；后者控制极端更新幅度（细节以论文原文为准）。
4. **分布式分摊**：NS 的矩阵乘开销按参数分片摊到各 rank、每轮一次 all_gather（u3-l3）——大规模下这是 Muon 侧的主要通信模式；Adam 分支零通信、靠梯度一致 + 确定性计算保持同步。
5. **低精度**：NS 只含矩阵乘、对 bf16 稳定（[muon.py:L52-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L52-L53)）；u4-l1 已实测其精度损失不构成瓶颈。

#### 4.3.3 源码精读

**（1）五条战果逐条读**（[README.md:L53-L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L53-L59)，Accomplishments 一节）：

- [README.md:L55](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L55) CIFAR-10 speedrun：训练到 94% 精度的纪录从 3.3 A100-秒降到 2.6 A100-秒（约 21% 算力节省，Ⅱ 级证据，链接指向 airbench 仓库）。
- [README.md:L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L56) GPT-2（XL）性能只需 $175 算力（Ⅱ 级，推文入口）。
- [README.md:L57](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L57) 把 GPT-2（small）的训练速度纪录提高 1.35 倍（Ⅱ 级，Muon 出圈的起点）。
- [README.md:L58](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L58) Kimi 用 Muon 做规模化 LLM 训练（Ⅳ 级入口；技术报告见下）。
- [README.md:L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L59) essential.ai（Ashish Vaswani 的实验室）表明 Muon 尤其适合大 batch 训练（arXiv 2505.02222）。

**（2）外部证据链四链接**（[README.md:L61-L66](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L61-L66)，"More learning resources"）：

- Jianlin Su（RoPE 作者）的解读博文（L63）：社区视角的理论梳理，后来的「Why we chose Muon」推文（L66）记录了 Kimi 选型 Muon 的推理链。
- Jeremy Bernstein 的推导博文（L64）：4.2.1 提到的「谱范数约束最速下降」视角即出于此——Muon 理论背景的第一参考。
- Kimi/Moonlight 技术报告（L65，arXiv 2502.16982）：Muon 首次进入 MoE 大模型规模训练的报告。据报告，Muon 在同等性能目标下显著降低所需训练量（论文口径约为「省下近一半训练计算」级别，精确数字以原文为准），并给出 4.3.2 第 3 条的两处工程修改。
- essential.ai（L59，arXiv 2505.02222）：其结论是 Muon 的最优/临界 batch size 明显大于 AdamW、与计算最优 batch 更接近（定性转述，详见论文）——这解释了「为什么大规模训练（天然大 batch）特别受益」。

**（3）一个代数等价：Moonlight 的「权重衰减并入更新」就是本仓库的两行**。单机版 muon 分支收尾（[muon.py:L267-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L267-L269)）：

```python
update = muon_update(p.grad, state["momentum_buffer"], beta=group["momentum"])
p.mul_(1 - group["lr"] * group["weight_decay"])   # 先按解耦方式衰减
p.add_(update.reshape(p.shape), alpha=-group["lr"])  # 再施加更新
```

两行合起来即

\[
p_{t+1} = (1-\eta\lambda)\,p_t - \eta\,u_t \;=\; p_t - \eta\,\bigl(u_t + \lambda\,p_t\bigr),
\]

右端正是「把衰减项 \(\lambda p\) 并进更新再一步施加」的形式。也就是说，本仓库采用的 AdamW 式解耦写法与大规模报告强调的「weight decay 作用于更新」代数上同一件事——读外部论文时能这样落回本地源码，才算真正读通。adam 分支的两行（[muon.py:L283-L284](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L283-L284)）同理。

#### 4.3.4 代码实践

**实践目标**：建立自己的「证据笔记本」——把本模块的分级阅读流程走一遍，产出一张可长期维护的证据表（阅读型实践）。

**操作步骤**：

1. 打开 [README.md:L49-L66](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L49-L66)，逐条点开 Benchmarks、Accomplishments、Resources 里的链接；
2. 在自己的练习目录（不动仓库）新建 `evidence.md`，画一张五列表格：`声明 | 证据等级(Ⅰ-Ⅳ) | 规模 | 对照物 | 可复现入口`；
3. 把每条战果/资源填一行（共约 9 行：5 条战果 + 4 个资源）；
4. 对其中最关键的 Ⅲ 级证据（modded-nanogpt 的 102924_Optimizers 目录）额外记录：它对比了哪四个优化器、在什么规模、用什么指标（步数/算力）；
5. 回头检查：你的第 Ⅰ 级实验（4.1.4、4.2.4）验证了链条的哪一环？

**需要观察的现象**：填表时会发现五条战果没有一条是「受控对照实验」——它们要么是纪录，要么是推文入口；真正的受控对比只有 Ⅲ 级那一个目录。

**预期结果**：一张约 9 行的证据表，且「Kimi 报告的省算力结论」与「essential.ai 的大 batch 结论」这两行会发现没有本地可复现入口（只能引论文），而 CIFAR-10 speedrun 那行有完整脚本入口（airbench 仓库）。这一不对称本身就是结论：**可复现性是证据分级里最值钱的属性**。具体每行内容待你阅读链接后填写。

#### 4.3.5 小练习与答案

**练习 1**：README 五条战果中哪一条直接支撑「Muon 适合大 batch」？它属于哪一级证据？

**答案**：[README.md:L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L59)（essential.ai，arXiv 2505.02222）。属 Ⅳ 级（工业/学术规模研究），且是五条中最接近「受控研究」的一条——有论文、有 scaling law 框架，但没有开源的一键复现入口。

**练习 2**：用代数说明 Moonlight 报告「把 weight decay 并入更新」的修改，与本仓库 [muon.py:L268-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L268-L269) 的两行是同一件事。

**答案**：仓库两行 \(p\leftarrow(1-\eta\lambda)p-\eta u\) 展开即 \(p\leftarrow p-\eta(u+\lambda p)\)：衰减项 \(\lambda p\) 与更新 \(u\) 在同一步、以 lr 为公共系数被施加——这正是「decay 作用于 update」的形式。区别只在叙述：AdamW 文献称为 decoupled weight decay，Moonlight 强调的是它相对「原始 Muon 无衰减」补上了这一项。

**练习 3**：你在 d_model=64 的玩具上发现 AdamW 与 Muon 打平，能否据此说「124M 上 Muon 无优势」？

**答案**：不能。Ⅰ 级证据不能向上否定 Ⅲ 级证据：玩具模型的宽度、深度、数据复杂度都不足以让「谱白化 vs 对角白化」的差异显形（小矩阵的奇异谱本来就没多少结构可拉平）；受控对比要看 [README.md:L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L51) 的 124M 基准。正确用法是：玩具实验回答机制问题（更新尺度、谱行为），性能问题交给对应规模的证据。

## 5. 综合实践

**任务**：完成讲义规格要求的微型基准——固定一个 2 层、d_model=64 的小 transformer，在相同合成数据上分别用 AdamW、`SingleDeviceMuonWithAuxAdam`、SGD+momentum 各训练 **3 个种子**，产出「最终 loss + 到达目标 loss 所需步数」对比表，并写 200 字总结 Muon 相对 AdamW 的适用范围与限制。这是 u3-l4 的 `train_compare.py` 的升级版：换了 transformer 模型、加了第三种优化器、加了多种子。

设计要点（把本讲理论用进实验设计）：

1. **三种 lr 量纲互不相同**（4.1 结论）→ 不能共用 lr，每个优化器先做小网格扫描（种子 0），再各自用最优 lr 跑 3 种子；
2. **参数划分照抄 README 配方**（u3-l4）：`model.blocks` 内二维权重进 muon 组，其余（LN、embedding、head）进 adam 组，adam 组超参与 AdamW 基线一致；
3. **多种子只改初始化与批采样**，数据集本身固定，避免把数据差异算进优化器差异。

完整脚本（示例代码，CPU 可跑，每轮约几分钟量级）：

```python
# 示例代码：bench.py —— 三优化器 × 3 种子微型基准
import time
import torch, torch.nn as nn, torch.nn.functional as F
from muon import SingleDeviceMuonWithAuxAdam   # 未 pip 安装时先 sys.path.insert(0, "<仓库根目录>")

V, T, D, L, STEPS, TARGET = 16, 16, 64, 2, 400, 1.0

class Block(nn.Module):
    def __init__(self, d):
        super().__init__()
        self.ln1 = nn.LayerNorm(d)
        self.q, self.k, self.v, self.o = (nn.Linear(d, d, bias=False) for _ in range(4))
        self.ln2 = nn.LayerNorm(d)
        self.fc1, self.fc2 = nn.Linear(d, 4 * d, bias=False), nn.Linear(4 * d, d, bias=False)
    def forward(self, x):
        h = self.ln1(x)                                   # (B, T, d)
        a = F.scaled_dot_product_attention(self.q(h).unsqueeze(1), self.k(h).unsqueeze(1),
                                           self.v(h).unsqueeze(1), is_causal=True).squeeze(1)
        x = x + self.o(a)
        h = self.ln2(x)
        return x + self.fc2(F.gelu(self.fc1(h)))

class ToyTransformer(nn.Module):
    def __init__(self):
        super().__init__()
        self.embed = nn.Embedding(V, D)
        self.blocks = nn.ModuleList(Block(D) for _ in range(L))
        self.ln_f = nn.LayerNorm(D)
        self.head = nn.Linear(D, V, bias=False)
    def forward(self, x):
        h = self.embed(x)
        for b in self.blocks:
            h = b(h)
        return self.head(self.ln_f(h))

def make_data(n=8192, seed=0):                            # 数据集固定，与训练种子无关
    g = torch.Generator().manual_seed(seed)
    x = torch.randint(0, V, (n, T), generator=g)
    prev = torch.cat([torch.zeros(n, 1, dtype=torch.long), x[:, :-1]], dim=1)
    return x, (x + 2 * prev) % V                          # y_t = (x_t + 2·x_{t-1}) mod V，因果可解

data_x, data_y = make_data()

# ---- 三个优化器构造器（lr 量纲各自独立，见 4.1）----
def build_adamw(model, lr):
    return torch.optim.AdamW(model.parameters(), lr=lr, betas=(0.9, 0.95), weight_decay=0.01)

def build_sgd(model, lr):
    return torch.optim.SGD(model.parameters(), lr=lr, momentum=0.95)

def build_muon(model, lr):
    hidden = [p for p in model.blocks.parameters() if p.ndim >= 2]        # README 配方：body 二维
    rest = [p for p in model.blocks.parameters() if p.ndim < 2] \
           + [*model.ln_f.parameters(), *model.embed.parameters(), *model.head.parameters()]
    return SingleDeviceMuonWithAuxAdam([
        dict(params=hidden, use_muon=True,  lr=lr,     weight_decay=0.01),
        dict(params=rest,   use_muon=False, lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
    ])

def run(build, lr, seed, steps=STEPS):
    torch.manual_seed(seed)                               # 只控制初始化 + 批采样
    model = ToyTransformer()
    opt = build(model, lr)
    first_hit, losses = None, []
    for step in range(steps):
        idx = torch.randint(0, len(data_x), (128,))
        loss = F.cross_entropy(model(data_x[idx]).reshape(-1, V), data_y[idx].reshape(-1))
        opt.zero_grad(); loss.backward(); opt.step()
        losses.append(loss.item())
        if first_hit is None and loss.item() <= TARGET:
            first_hit = step
    return losses, first_hit

OPTS = {  # 名字: (构造器, lr 候选) —— 每家量纲不同，各自扫
    "AdamW": (build_adamw, [3e-4, 1e-3, 3e-3]),
    "SGD+M": (build_sgd,   [0.01, 0.03, 0.1]),
    "Muon":  (build_muon,  [0.02, 0.05]),
}

best = {name: min(lrs, key=lambda lr: min(run(b, lr, seed=0)[0]))
        for name, (b, lrs) in OPTS.items()}

print(f"{'优化器':<8}{'最优lr':<9}{'3种子最终loss':<28}{'到达loss<=' + str(TARGET) + '步数'}")
for name, (build, _) in OPTS.items():
    finals, hits = [], []
    for seed in (0, 1, 2):
        losses, hit = run(build, best[name], seed)
        finals.append(losses[-1]); hits.append(hit if hit is not None else f">{STEPS}")
    print(f"{name:<8}{best[name]:<9}{str([round(f, 3) for f in finals]):<28}{hits}")
```

**需要观察的现象**：

1. 三家扫描阶段的最优 lr 是否落在各自候选区间的「舒服位置」（AdamW 的 1e-3 附近、Muon 的 0.02~0.05、SGD 偏大端）——验证 4.1 的量纲论；
2. 同一优化器 3 个种子的最终 loss 波动有多大（把波动与优化器间差距比一比）；
3. （可选加分项）用 `time.perf_counter()` 给每次 `run` 计时，把「到达目标 loss 的秒数」也列出来——NS 的矩阵乘开销会拉高 Muon 的单步成本，但在 GPU 上结论可能反转。

**预期结果**（方向性，**待本地验证**）：初始 loss ≈ \(\ln 16 \approx 2.77\)；三家都能下降。方向性预期是 Muon 与 AdamW 接近或 Muon 略优、SGD+momentum 明显偏慢（transformer + LayerNorm 场景下无归一化器最吃亏，正对应 4.1 练习 3）。若 SGD 在扫过的 lr 里都不收敛属正常现象——把「SGD 需要更精细的 lr/裁剪」如实写进结论即可。**注意**：这是 Ⅰ 级证据（4.3 的分级），性能结论不外推；它验证的是本讲机制预言（lr 量纲、更新尺度、参数划分配方在 transformer 上成立）。

**交付物**：

1. 一张结果表（优化器 × 3 种子的最终 loss，加一列到达目标 loss 的步数）；
2. 三条 loss 曲线（每种子的均值曲线，matplotlib 或手抄关键点）；
3. 约 200 字总结，回答：本任务上 Muon 相对 AdamW 的优势/劣势？适用范围（矩阵参数、谱结构明显）与限制（embedding/head 需另配 AdamW、lr 量纲独立、NS 计算开销）各是什么？SGD 落后是否印证 4.1 的尺度分析？

## 6. 本讲小结

- **更新尺度三分法**：SGD+momentum 无归一化器（更新 ∝ 梯度尺度）；AdamW 对角白化（逐元素 ≈±1，lr 单位是逐坐标幅度）；Muon 谱白化（更新 RMS ≈ \(1/\sqrt{n}\)，lr 单位是谱范数，[muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61)）——本质区别是归一化几何：对角统计 vs 瞬时奇异谱。
- **家族谱系一条线**：Adam=对角累积白化、Shampoo/SOAP=全矩阵累积白化、Muon=全矩阵瞬时白化；瞬时 Shampoo 更新 \(L^{-1/2}GR^{-1/2}\) 恰等于正交化 \(UV^\top\)（4.2.2 手推 + 4.2.4 实测）。「瞬时」换来 1× 动量显存与纯矩阵乘的 bf16 稳定性（[muon.py:L52-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L52-L53)）。
- **证据要分级读**：玩具（Ⅰ）→ speedrun（Ⅱ，[README.md:L53-L57](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L53-L57)）→ 124M 受控基准（Ⅲ，[README.md:L49-L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L49-L51)）→ 工业规模报告（Ⅳ，[README.md:L58-L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L58-L59)）；结论跨级传播必须打折。
- **大规模注意事项**：参数划分（embedding/head 默认归 AdamW）、lr 两套量纲独立缩放（muP 式宽度无关只对 muon 组成立）、权重衰减并入更新（与 [muon.py:L268-L269](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L268-L269) 两行代数等价）、大规模实践另加更新 RMS 裁剪、分布式下 NS 按参数分片摊薄（u3-l3）。
- **微型基准的方法论**：三优化器 lr 各自扫描再多种子对比；把「优化器间差距」与「种子间波动」分开报告；性能结论只属于实验规模，机制结论（尺度、谱、划分）才可跨规模迁移。

## 7. 下一步学习建议

- **u4-l3（定制 Muon 变体）**：本讲把 `muon_update` 当「对比标本」，下一讲把它当「改造模板」——加梯度裁剪、改尺度因子、调 `ns_steps`，本讲的尺度分析（改动如何影响 lr 的谱范数语义）正是改造时的安全检查清单。
- **跑一遍 124M 基准（有 GPU 时）**：进入 [README.md:L51](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L51) 链接的 modded-nanogpt 102924_Optimizers 目录，复现 AdamW/Shampoo/SOAP/Muon 四家同台对比，亲眼看看 Ⅲ 级证据与你 Ⅰ 级玩具结论的异同。
- **读两篇外部文章**：Bernstein 的推导（[README.md:L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L64)，对应 4.2.1 的最速下降视角）与 Kimi/Moonlight 报告（[README.md:L65](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L65)，对应 4.3 的注意事项清单）——分别补齐本讲理论与实证两侧的原始细节。
- **重温 Keller 的 writeup**（[README.md:L3](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L3)）：学完本手册再读原始出处，检验自己能否一眼看出每段话对应 muon.py 的哪几行——这是「读懂一个项目」的最终检验。
