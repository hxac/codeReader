# Moonlight 项目总览：Muon 与大规模语言模型训练

> 本讲是 Moonlight 学习手册的第一讲。你不需要任何关于本项目的预备知识，只需要基本的 Python 和一点深度学习概念。学完本讲，你会清楚 Moonlight 是什么、仓库里有什么、Muon 优化器为什么值得研究，并准备好进入后续的源码精读。

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 Moonlight 项目的定位：它是 Moonshot AI 技术报告《Muon is Scalable for LLM Training》的配套开源仓库。
2. 复述项目的主要成果：训练出 Moonlight（16B 总参数 / 3B 激活参数的 MoE 模型，5.7T tokens），并给出「Muon 约只需 AdamW 52% 的训练 FLOPs」这一核心结论。
3. 用自己的话解释 Muon 相对 AdamW 的两大关键改进：**加入权重衰减（weight decay）** 与 **按参数形状调整更新尺度（per-parameter update scale）**。
4. 画出仓库的文件结构清单，说明每个文件的作用，知道唯一的源码文件是 `examples/toy_train.py`。
5. 完成第一次动手：克隆仓库、安装依赖、浏览技术报告摘要，并写下三个你最想弄懂的问题（后续单元会逐一回答）。

## 2. 前置知识

本讲面向初学者，但有几个概念最好先有个直觉。不熟悉也没关系，我们用最通俗的方式讲。

### 2.1 神经网络训练与优化器

- **训练**：给模型输入大量文本，模型对每个词做预测，预测错了就产生一个「损失」（loss，一个标量数字，越小越准）。
- **梯度**：损失对每个参数的「导数」，指出每个参数朝哪个方向改动能让损失变小。
- **优化器（optimizer）**：决定「拿到梯度之后，参数到底怎么改」的算法。最常见的是 SGD（沿梯度反方向走一小步）和 Adam/AdamW（给每个参数维护一阶、二阶动量，自适应地调整步长）。本项目的主角 **Muon** 就是一种新优化器，而 **AdamW** 是它要挑战的基线（baseline，即用来对比的标准方案）。
- **权重衰减（weight decay）**：每一步把参数本身稍微缩小一点，防止参数无限变大。AdamW 中的 W 就是它。

### 2.2 语言模型与 token

- **token**：模型处理文本的最小单位，一个词或词的一小段。模型读的是 token 序列，不是字符。
- **5.7T tokens**：约 5.7 万亿个 token，这是 Moonlight 模型的训练数据量级。

### 2.3 什么是 MoE（Mixture-of-Experts，混合专家）

普通 dense 模型（稠密模型）每层对所有参数做计算。MoE 模型里有多个「专家」网络，每个 token 只激活其中少数几个，因此**总参数量可以很大，而每一步实际参与计算的参数（激活参数）很小**。Moonlight 就是 16B 总参数、3B 激活参数的 MoE——这解释了后文表格里「Activated Param 2.24B / Total Params 15.29B」两行为什么差距巨大。

### 2.4 你需要的工具

- 会用命令行（`git`、`pip`、`python`）。
- 知道如何创建 Python 虚拟环境（本讲综合实践会给完整命令）。

## 3. 本讲源码地图

本讲涉及的文件及其作用：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| `README.md` | 项目说明书：摘要、三大贡献、性能对比表、模型下载、推理与训练示例命令 | 主要精读对象，几乎全部结论出自这里 |
| `Moonlight.pdf` | 技术报告《Muon is Scalable for LLM Training》正文，含公式推导与完整实验 | 导读：读摘要、建立全局印象 |
| `Moonlight_intermediate_checkpoints.pdf` | 中间检查点（训练过程中的存档）发布说明 | 只需知道它存在 |
| `requirements.txt` | Python 依赖清单（6 个包） | 精读，用于安装环境 |
| `examples/toy_train.py` | 仓库**唯一的源码文件**（约 360 行）：Muon 完整实现 + Qwen2 玩具训练管线 | 本讲只预览其中三个代码点，细节留给第二单元 |
| `LICENSE` / `.gitignore` / `figures/` | MIT 许可证；忽略规则；README 用图 | 粗略了解 |

一个先记住的事实：**这个仓库很小，但信息密度很高**。核心代码集中在一个文件里，其余是文档和图片；真正的大规模分布式实现并不在本仓库（见 4.3 节）。

## 4. 核心概念与源码讲解

### 4.1 项目背景与成果

#### 4.1.1 概念说明

Moonlight 仓库回答一个研究问题：**Muon 优化器能不能用来训练大规模语言模型？**

背景是这样的：

- Muon 是一种基于「矩阵正交化」的优化器（原始实现见 [KellerJordan/Muon](https://github.com/KellerJordan/Muon)），在**小规模**语言模型训练中已经展示了很强的效果。
- 但它**从未被证明可以扩展（scale）到大规模训练**——大模型训练动辄烧掉几百万 GPU 小时，没人敢直接换一个未经大规模验证的优化器。
- Moonshot AI 团队系统分析了 Muon 在规模化时的障碍，补上两个关键技术（见 4.2 节），然后用它训练了一个 16B 的 MoE 模型，证明 Muon 不仅可行，还**更省算力**。

这个 16B 模型被命名为 **Moonlight**，仓库也因此得名。注意区分：**Moonlight 是训练出来的模型，Muon 是训练它用的优化器**，而本仓库的名字 Moonlight 指向整个项目。

#### 4.1.2 核心流程

整个项目的研究路径可以概括为：

```text
Muon（小规模已验证）
    │
    ├─ 问题诊断：直接放大到大模型会不稳定 / 需要调参
    │
    ├─ 两大修复：① 加权重衰减  ② 按参数形状调整更新尺度
    │
    ├─ scaling law 实验：Muon vs AdamW 多规模对比
    │       → 结论：达到同等性能，Muon 约只需 52% 训练 FLOPs（≈2 倍计算效率）
    │
    ├─ 工程落地：ZeRO-1 风格的分布式 Muon 实现（内存最优、通信高效）
    │
    └─ 最终验证：训练 Moonlight（16B-A3B MoE，5.7T tokens）→ 公开发布权重与检查点
```

其中「52% 训练 FLOPs」的含义：FLOPs 是浮点运算次数，衡量训练消耗的算力。如果两个模型最终性能相同，而 Muon 训练的那个总共消耗的 FLOPs 只有 AdamW 版本的约 52%，就相当于**用一半的算力得到了同样的效果**——这是本工作最吸引人的结论。

#### 4.1.3 源码精读

**（1）摘要：项目的一句话定位。**

[README.md:14-19](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L14-L19) 是技术报告摘要的转载。这段话信息量极大，逐句拆开看：

- 「the Muon optimizer ... has demonstrated strong results in training small-scale language models, but the scalability to larger models has not been proven」——Muon 小规模有效、大规模未验证，这就是研究动机。
- 「We identify two crucial techniques for scaling up Muon: (1) adding weight decay and (2) carefully adjusting the per-parameter update scale」——两大关键技术，本讲 4.2 节展开。
- 「Muon achieves ∼ 2× computational efficiency compared to AdamW」——约 2 倍计算效率。
- 「we introduce **Moonlight**, a 3B/16B-parameter Mixture-of-Expert (MoE) model trained with 5.7T tokens using Muon」——3B 激活 / 16B 总参数的 MoE，5.7T tokens。
- 「We open-source our distributed Muon implementation ... release the pretrained, instruction-tuned, and intermediate checkpoints」——开源分布式实现与各种检查点。

**（2）三大技术贡献。**

[README.md:23-31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L23-L31) 列出 Key Ingredients，对应论文的三大贡献：

1. **Muon 有效规模化的分析**（L27）：发现权重衰减对 Muon 的可扩展性至关重要；提出通过逐参数的更新尺度调整，让不同形状参数的更新均方根（update RMS）保持一致，显著增强训练稳定性。
2. **高效分布式实现**（L29）：ZeRO-1 风格的分布式 Muon，在保持算法数学性质的同时做到内存最优、通信开销更低（ZeRO-1 是一种把优化器状态切分到多卡以省显存的并行技术，第三单元详讲）。
3. **Scaling law 验证**（L31）：与强 AdamW 基线做 scaling law 对比（见 [README.md:33-36](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L33-L36) 的图），结论是达到同等性能只需约 52% 训练 FLOPs。

**（3）性能对比表：Moonlight 到底有多好。**

[README.md:39-65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L39-L65) 把 Moonlight 和三个同量级公开模型对比（表格数据摘录如下）：

| 指标 | Llama3.2-3B | Qwen2.5-3B | DSV2-Lite | **Moonlight** |
|---|---|---|---|---|
| 激活参数（不含 embedding） | 2.81B | 2.77B | 2.24B | **2.24B** |
| 总参数（不含 embedding） | 2.81B | 2.77B | 15.29B | **15.29B** |
| 训练 tokens | 9T | 18T | 5.7T | **5.7T** |
| 优化器 | AdamW | 未公开 | AdamW | **Muon** |
| MMLU | 54.75 | 65.6 | 58.3 | **70.0** |
| HumanEval（代码） | 28.0 | 42.1 | 29.9 | **48.1** |
| MATH（数学） | 8.5 | 42.6 | 17.1 | **45.3** |
| CMMLU（中文） | - | 75.0 | 64.3 | **78.2** |

关键观察：**DeepSeek-v2-Lite（DSV2-Lite）是最公平的对照组**——它与 Moonlight 总参数（15.29B）、激活参数（2.24B）、训练量（5.7T tokens）完全相同，主要差异就是优化器（AdamW vs Muon）。在这个对照下 Moonlight 全面领先（例如 MMLU 70.0 对 58.3）。

**（4）模型下载入口。**

[README.md:71-78](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L71-L78) 给出 HuggingFace 上的两个模型：`Moonlight-16B-A3B`（预训练）与 `Moonlight-16B-A3B-Instruct`（指令微调），都是 16B 总参数 / 3B 激活参数 / 8K 上下文。命名里的 A3B 就是「激活约 3B」的意思。

#### 4.1.4 代码实践

**实践目标**：亲手从 README 的表格里提取证据，验证「同等算力下 Muon 更好」的说法，而不是听别人转述。

**操作步骤**：

1. 打开 [README.md:47-65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L47-L65) 的对比表。
2. 只看 Moonlight 与 DSV2-Lite 两列（两者训练 tokens、参数量完全一致）。
3. 用计算器或 Python 算出 Moonlight 相对 DSV2-Lite 在 MMLU、MMLU-pro、BBH、HumanEval、GSM8K、MATH 六个基准上的提升幅度。

**需要观察的现象 / 预期结果**：六个基准上 Moonlight 全部占优；以 MMLU 为例提升为 \(70.0 - 58.3 = 11.7\) 分，MATH 提升为 \(45.3 - 17.1 = 28.2\) 分（数学类基准提升尤其大）。同时注意 GSM8K 一列 Qwen2.5-3B（79.1）仍是最高——单一基准的冠军并不属于 Moonlight，它赢在整体前沿。

**待本地验证**：以上数字直接摘自 README 表格，请以你打开仓库时看到的最新数值为准。

#### 4.1.5 小练习与答案

**练习 1**：Moonlight 模型的总参数量、激活参数量、训练 token 数分别是多少？仓库表格中标注的数字和摘要中的「3B/16B」有什么对应关系？

> **答案**：总参数 16B、激活参数 3B、训练 5.7T tokens。表格中「Activated Param 2.24B / Total Params 15.29B」是不含 embedding 参数的口径（表格脚注 † 说明了这一点），与摘要的 3B/16B 是同一件事的两种说法。

**练习 2**：「Muon 约只需 AdamW 52% 的训练 FLOPs」和「Muon 有约 2 倍计算效率」这两句话矛盾吗？

> **答案**：不矛盾，是同一件事的两种表述：\(1/0.52 \approx 1.92 \approx 2\)。消耗的 FLOPs 减半，等价于计算效率翻倍。

**练习 3**：为什么说 DSV2-Lite 是比 Llama3.2-3B 更合适的对照组？

> **答案**：DSV2-Lite 与 Moonlight 的总参数（15.29B）、激活参数（2.24B）、训练 tokens（5.7T）完全相同，且都是 MoE，唯一主要变量是优化器；Llama3.2-3B 则是 dense 模型、参数量不同、训练 tokens（9T）也不同，变量太多，结论不干净。

### 4.2 Muon 核心思想

#### 4.2.1 概念说明

**Muon 是什么？** 名字是缩写：**MomentUm Orthogonalized by Newton-schulz**（动量 + 用 Newton-Schulz 迭代做正交化）。它的直觉是：

- 神经网络里绝大多数参数是**二维矩阵**（比如注意力投影、前馈层的权重）。
- 优化器算出的「该往哪走」的更新量，对每个矩阵参数来说也是一个矩阵。
- Muon 的做法：先像普通 SGD-momentum 一样累计动量，然后把这个**更新矩阵替换成与它最接近的正交矩阵**（这一步叫正交化 / 矩阵零次幂）。正交矩阵的特点是所有方向被「一视同仁」——奇异值全为 1，更新能量不会集中在少数方向上。

**为什么正交化后会出问题（论文要解决的事）？** 一个 \(A \times B\) 的正交化更新矩阵 \(U\)，其 Frobenius 范数是 \(\sqrt{\min(A,B)}\)，于是它的更新均方根（RMS，所有元素平方平均后开根号）为：

\[
\mathrm{RMS}(U) = \sqrt{\frac{\min(A,B)}{A \cdot B}} = \frac{1}{\sqrt{\max(A,B)}}
\]

也就是说，**不做处理时，参数矩阵形状不同，更新的量级就不同**——一个 \(4096\times4096\) 矩阵的更新 RMS 只有 \(1/64\)，而一个 \(16\times 16\) 矩阵的更新 RMS 高达 \(1/4\)。这种不一致在大规模训练里会造成不稳定。

**Moonlight 的两大修复**（对应摘要里的 two crucial techniques）：

1. **加权重衰减**：论文分析认为这是 Muon 能否规模化的关键（README L27）；没有它，大模型长训练中参数会持续变大，更新相对于参数的「力度」失衡。
2. **按参数形状调整更新尺度**：给学习率乘上 \(0.2\sqrt{\max(A,B)}\)。代入上面的 RMS 公式：

\[
\underbrace{\frac{1}{\sqrt{\max(A,B)}}}_{\text{正交化更新的 RMS}} \times \underbrace{0.2\sqrt{\max(A,B)}}_{\text{形状缩放}} = 0.2
\]

于是**无论矩阵什么形状，更新 RMS 都近似恒定为 0.2**，与 AdamW 的典型更新量级对齐——这就是「update RMS 一致化」。这也解释了为什么 Moonlight 可以「免调参开箱即用」（out-of-the-box）。

> 注：严格说 Newton-Schulz 得到的是近似正交矩阵（奇异值约在 0.5~1.5 之间），所以 RMS 是「约等于」0.2；完整推导留到第二单元 u2-l4 讲。

#### 4.2.2 核心流程

Muon 对每个二维矩阵参数的单步更新流程（伪代码）：

```text
输入：参数 p（A×B 矩阵）、梯度 g、动量缓存 buf

1. buf = momentum * buf + g            # SGD 动量
2. 若 nesterov：g_eff = g + momentum * buf，否则 g_eff = buf
3. u = NewtonSchulz5(g_eff, ns_steps)   # 正交化：近似矩阵零次幂（不需要 SVD）
4. adjusted_lr = lr * 0.2 * sqrt(max(A, B))   # ★ 按形状调整更新尺度
5. p = p * (1 - lr * wd)                # ★ 解耦权重衰减
6. p = p - adjusted_lr * u              # 应用更新
```

另外有一条重要约定：**不是所有参数都走 Muon**。embedding 层（`embed_tokens`）、输出头（`lm_head`）和所有一维参数（bias、norm 的 scale）不做矩阵正交化，交给 Muon 内嵌的 AdamW 分支处理（对应代码见 [examples/toy_train.py:292-311](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L292-L311) 的参数分组，细节在 u2-l1 讲）。

#### 4.2.3 源码精读

本讲只看三个代码点——它们正是「论文两大贡献」落在代码里的位置，也是第二单元要精读的入口。

**（1）Muon 类的文档字符串：官方自述。**

[examples/toy_train.py:79-104](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L79-L104) 说明了 Muon 的定位：「Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-processing step, in which each 2D parameter's update is replaced with the nearest orthogonal matrix」（内部跑标准 SGD 动量，然后把每个二维参数的更新替换为最近的正交矩阵）。文档还给出两条坦诚的警告：小 batch size 下可能效果不好、未在微调场景充分测试（L88-90）。

**（2）`adjust_lr_for_muon`：更新 RMS 一致化的落点。**

[examples/toy_train.py:142-148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148) 中，`adjusted_ratio = 0.2 * math.sqrt(max(A, B))` 就是 4.2.1 推导的形状缩放，注释直接写明「as describeted in the paper」——论文公式与代码一一对应。

**（3）两处权重衰减：解耦实现。**

- Muon 分支：[examples/toy_train.py:196-203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L196-L203) 先 `p.data.mul_(1 - lr * wd)` 再 `p.data.add_(u, alpha=-adjusted_lr)`——先按比例缩小参数、再加更新，这就是「解耦」的权重衰减。
- AdamW 分支：[examples/toy_train.py:233-237](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L233-L237) 用同样的 `p.data.mul_(1 - lr * weight_decay)` 方式处理非 Muon 参数，保证两套分支的衰减行为一致。

#### 4.2.4 代码实践

**实践目标**：用 10 行示例代码验证「形状缩放 × 正交化更新 = 恒定 RMS」的数学直觉。

**操作步骤**：

1. 新建一个独立脚本（不要改动仓库源码），构造几个不同形状的随机矩阵，做 SVD 正交化（用 `torch.linalg.svd` 得到精确的 \(UV^T\)，作为 Newton-Schulz 的替身），再乘上 `0.2 * math.sqrt(max(A,B))`，最后计算每个结果矩阵的 RMS。
2. 示例代码（**示例代码**，非仓库原有文件）：

```python
import torch

def rms(m):  # 更新矩阵的均方根
    return m.pow(2).mean().sqrt()

for A, B in [(64, 64), (256, 32), (1024, 128)]:
    G = torch.randn(A, B)
    U, S, Vh = torch.linalg.svd(G, full_matrices=False)
    O = U @ Vh                       # 精确正交化（Newton-Schulz 的近似目标）
    scaled = 0.2 * (max(A, B) ** 0.5) * O
    print(f"shape=({A},{B})  RMS(O)={rms(O):.4f}  RMS(scaled)={rms(scaled):.4f}")
```

**需要观察的现象**：无论形状如何，`RMS(scaled)` 一列都约等于 0.2，而 `RMS(O)` 一列随 \(\max(A,B)\) 增大而变小（等于 \(1/\sqrt{\max(A,B)}\)）。

**预期结果**：三行输出的 `RMS(scaled)` 都在 0.2 附近（浮点误差内），直观印证「按参数形状调整更新尺度 → 更新 RMS 一致化」。

**待本地验证**：本讲未替你运行该脚本，请在本机执行后核对数值。

#### 4.2.5 小练习与答案

**练习 1**：Muon 的名字展开是什么？它内部先跑哪种经典优化器？

> **答案**：MomentUm Orthogonalized by Newton-schulz；内部先跑标准 SGD-momentum，再把每个二维参数的更新矩阵正交化。

**练习 2**：一个 \(4096 \times 4096\) 的参数矩阵，其（未缩放的）正交化更新 RMS 是多少？乘上形状缩放后又是多少？

> **答案**：未缩放时 RMS \(= 1/\sqrt{4096} = 1/64 \approx 0.0156\)；乘上 \(0.2\sqrt{4096} = 0.2 \times 64 = 12.8\) 后，RMS \(= 0.0156 \times 12.8 = 0.2\)。

**练习 3**：为什么 embedding 和 lm_head 不交给 Muon 正交化？

> **答案**：正交化是针对「二维权重矩阵」设计的更新后处理，而 embedding/输出头本质是查表/投影字典，语义不同；仓库代码把这些参数与一维参数一起交给内嵌的 AdamW 分支（见 `get_optimizer` 的分组逻辑，[examples/toy_train.py:292-311](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L292-L311)）。更细的讨论见 u2-l1。

### 4.3 仓库文件结构

#### 4.3.1 概念说明

很多开源项目「代码占 95%、文档占 5%」，Moonlight 正相反：**文档与代码之比极高**。理解它的文件结构，就是理解「东西都在哪」：

```text
Moonlight/
├── README.md                          # 项目说明书（本讲主读）
├── Moonlight.pdf                      # 技术报告正文
├── Moonlight_intermediate_checkpoints.pdf  # 中间检查点发布说明
├── requirements.txt                   # 6 个 Python 依赖
├── LICENSE                            # MIT 许可证
├── .gitignore                         # 忽略 logs/ 与 *.bin
├── examples/
│   └── toy_train.py                   # ★ 唯一源码文件：Muon + Qwen2 训练管线
└── figures/                           # README 插图（banner、scaling 图等）
```

两个容易踩的认知坑：

1. **「分布式 Muon 实现开在哪里？」** 不在这个仓库。README 顶部链接的 [NVIDIA/Megatron-LM PR #1428](https://github.com/NVIDIA/Megatron-LM/pull/1428)（链接见 [README.md:8-11](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L8-L11)）才是工业级分布式实现；本仓库提供的是可复现核心算法的玩具训练脚本。第三单元 u3-l4 会专门讲这个方向。
2. **「16B 模型的训练代码在哪？」** 也不在。`examples/toy_train.py` 训练的是一个**手工构造的小 Qwen2 模型**（默认 hidden_size 1024、12 层），用来演示 Muon 算法本身，不是 16B MoE 的训练脚本。

#### 4.3.2 核心流程

拿到仓库后「从零到能跑」的路径：

```text
git clone → 读 README → pip install -r requirements.txt
        → examples/toy_train.py 里有什么？
             ├── MoonDataset（数据加载、分词、缓存、切块）     → u1-l4 精读
             ├── zeropower_via_newtonschulz5（正交化核心）     → u2-l2 精读
             ├── Muon 类（参数分组 / step / adjust_lr）        → u2-l1、u2-l3、u2-l4 精读
             ├── get_model_and_dataloader（Qwen2Config 构造）  → u1-l3、u3-l2
             └── __main__（argparse + 训练循环）               → u1-l3 精读
```

这个文件只有约 360 行，却是整本学习手册第二单元的全部精读对象——因为它就是论文核心算法的可执行版本。

#### 4.3.3 源码精读

**（1）requirements.txt：全部依赖只有 6 行。**

[requirements.txt:1-6](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/requirements.txt#L1-L6) 逐行说明：

| 包 | 版本 | 用途 |
|---|---|---|
| `torch` | 2.6.0 | 深度学习框架（训练与 `@torch.compile`） |
| `transformers` | 4.49.0 | 提供 `Qwen2Config`/`Qwen2ForCausalLM`/`Qwen2Tokenizer` 与学习率调度器 |
| `datasets` | 3.3.2 | 从 HuggingFace Hub 加载 `openwebtext-100k` 数据集 |
| `loguru` | 0.7.3 | 训练日志（`from loguru import logger`） |
| `tqdm` | 4.67.1 | 分词进度条 |
| `numpy` | 2.2.3 | 数值计算基础依赖 |

**（2）.gitignore：两个忽略项都对应代码里的「可再生成本物」。**

[.gitignore:1-2](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/.gitignore#L1-L2) 忽略 `logs` 和 `*.bin`。对照源码即可解释：`examples/toy_train.py` 第 327 行 `logger.add(f"logs/train_...log")` 会写日志目录；第 27-33 行 `MoonDataset._tokenize_texts` 会把分词结果缓存到 `{dataset_name}.bin`（[examples/toy_train.py:26-33](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L26-L33)）。两者都是首次运行后自动生成的本地产物，不该进版本库——`.gitignore` 是读懂仓库运行时行为的线索。

**（3）LICENSE：MIT。**

[LICENSE:1-2](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/LICENSE#L1-L2) 表明这是 MIT 许可证，版权归属 Moonshot AI——意味着你可以自由地学习、修改、再分发（保留版权声明即可）。

**（4）README 中的训练入口命令。**

[README.md:130-137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L130-L137) 给出两条训练命令，分别是用 Muon 和 AdamW 训练 Qwen 类 dense 模型。下一讲（u1-l2）会逐个参数拆解并实际运行它们。

#### 4.3.4 代码实践

**实践目标**：把仓库克隆到本地，装好依赖，并确认环境可用。

**操作步骤**：

```bash
# 1. 克隆仓库
git clone https://github.com/MoonshotAI/Moonlight.git
cd Moonlight

# 2. 创建并激活虚拟环境（Python >= 3.10 为佳）
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 快速自检：确认 torch 和 transformers 已就位
python -c "import torch, transformers, datasets, loguru, tqdm; print(torch.__version__, transformers.__version__)"
```

**需要观察的现象**：第 3 步安装约几个 GB（含 CUDA 版 torch，取决于平台）；第 4 步打印出版本号且无 ImportError。

**预期结果**：输出类似 `2.6.0 4.49.0`（具体串待本地验证，取决于你安装的 CUDA/平台变体）。

**注意**：`pip install -r requirements.txt` 会安装 CUDA 版 PyTorch，体积较大；若只在 CPU 上练习，可自行改装 CPU 版 torch，但版本行为可能与锁定版本有差异。安装是否成功、训练是否可跑，均**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：仓库里唯一的 `.py` 源码文件是什么？它包含哪几个主要组件？

> **答案**：`examples/toy_train.py`，包含 `MoonDataset`（数据管线）、`zeropower_via_newtonschulz5`（正交化）、`Muon` 优化器类、`get_model_and_dataloader`（模型与数据装配）、`get_optimizer`（优化器选择）以及 `__main__` 训练循环。

**练习 2**：`.gitignore` 忽略的两类文件分别由代码的哪一行产生？

> **答案**：`logs/` 由 `toy_train.py` 第 327 行 `logger.add(f"logs/train_...log")` 产生；`*.bin` 由第 27-33 行 `MoonDataset._tokenize_texts` 的 token 缓存 `torch.save(self.tokens, f"{self.dataset_name}.bin")` 产生。

**练习 3**：如果你想在生产环境用分布式 Muon 训练自己的大模型，应该去哪里找实现？

> **答案**：不在本仓库；按 README 顶部链接（[README.md:8-11](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L8-L11)）前往 NVIDIA/Megatron-LM 的 PR #1428。本仓库的 `toy_train.py` 是单卡算法参考实现。

### 4.4 技术报告导读

#### 4.4.1 概念说明

`Moonlight.pdf` 是技术报告《Muon is Scalable for LLM Training》的正式版本，也是本仓库一切结论的出处。它已发表于 arXiv，编号 **2502.16982**（见 [README.md:142-154](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L142-L154) 的引用信息，作者列表以 Jingyuan Liu、Jianlin Su 等为首的 Moonshot AI 团队）。

技术报告（tech report）介于论文与工程文档之间：它有论文级的实验与分析，但重点是把「我们怎么做的、为什么有效」讲清楚，供社区复现。读它不需要从头到尾顺读，**先摘要、再图、再结论、最后按需查公式**是更高效的路线。

另外，仓库里还有第二份 PDF：[Moonlight_intermediate_checkpoints.pdf](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/Moonlight_intermediate_checkpoints.pdf)，专门说明训练中途各个检查点（intermediate checkpoints）的发布情况，供研究训练动态的人员使用。注意一个小不一致：[README.md:139-140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L139-L140) 的「Intermediate Checkpoints」小节仍写着 "Coming soon..."，而仓库实际上已经放入了这份 PDF——以 PDF 与 HuggingFace 页面为准（README 文案滞后）。

#### 4.4.2 核心流程

推荐的第一遍阅读路线：

```text
1. 摘要（第 1 页）            ← 与 README.md 的 Abstract 逐字对应，可互相印证
2. 引言 + Figure 1（scaling 图）← README.md:33-36 内嵌了同一张图的说明文字
3. 结论：52% FLOPs / 2× 效率   ← 对应 README.md:31
4. （选读）方法章节：权重衰减分析、update RMS 调整公式
5. （选读）分布式实现章节：ZeRO-1 式状态切分与通信优化
```

第二单元和第三单元的讲义会分别在讲到 `adjust_lr_for_muon`（u2-l4）和分布式方向（u3-l4）时，再回到 PDF 的对应章节精读。PDF 具体章节编号本讲不臆测（**待确认**——请以你手上的 PDF 目录为准）。

#### 4.4.3 源码精读

本节「精读」的对象是 README 中与技术报告对应的三个锚点，它们是你打开 PDF 后的定位参照：

**（1）摘要锚点。**

[README.md:14-19](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L14-L19)：README 的 Abstract 段就是论文摘要的转载。读 PDF 前先读懂这一段，可以带着框架去核对原文。

**（2）核心图锚点：scaling law 图。**

[README.md:33-36](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L33-L36) 内嵌了技术报告的 Figure 1（`figures/scaling.png`），配文说明两件事：(a) Muon 比 Adam 样本效率高约 2 倍；(b) Moonlight 的 MMLU 表现把「性能-训练 FLOPs」的帕累托前沿（Pareto frontier，即「不存在全方位更优对手」的最优集合）向前推进了。这张图是全文最重要的一张图。

**（3）模型与检查点锚点。**

[README.md:71-78](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L71-L78)（两个 HF 模型卡）与 [Moonlight.pdf](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/Moonlight.pdf)（正文：模型设计、训练配置、评测细节）。PDF 没有行号可引用，以上文件级链接可直接跳转。

#### 4.4.4 代码实践

**实践目标**：完成技术报告的第一次定向阅读，并沉淀出你自己的三个问题——这三个问题就是你在本学习手册里的「私人学习目标」。

**操作步骤**：

1. 打开仓库根目录的 `Moonlight.pdf`（或到 arXiv 下载 2502.16982），只读**摘要**和**引言末尾的贡献列表**，允许跳过公式。
2. 对照 [README.md:23-31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L23-L31) 的三大贡献，确认你能在摘要里找到它们的影子。
3. 看一眼 `figures/scaling.png`（即 README 内嵌的 scaling 图），找到横纵坐标各是什么。
4. 写下三个你最想弄懂的问题，存进你自己的笔记文件（建议放仓库外，如 `moonlight-notes.md`，避免弄脏仓库）。示例问题：
   - 「正交化为什么用 Newton-Schulz 迭代而不用 SVD？」（→ u2-l2 回答）
   - 「权重衰减到底解决了什么失衡？」（→ u2-l3 回答）
   - 「0.2 这个数字是怎么定下来的？」（→ u2-l4 回答）

**需要观察的现象**：读完摘要后，你应当能不看资料说出「两大技术」和「52%」这两个关键词；看 scaling 图时能指出哪条曲线是 Muon。

**预期结果**：得到一份三问清单。它没有标准答案，价值在于让后续每一讲都「有的放矢」。

**待本地验证**：PDF 的排版与图的位置因阅读器而异，无法预先描述。

#### 4.4.5 小练习与答案

**练习 1**：技术报告的标题和 arXiv 编号是什么？

> **答案**：《Muon is Scalable for LLM Training》，arXiv:2502.16982（见 [README.md:145-153](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L145-L153) 的 BibTeX）。

**练习 2**：README 的 Abstract 和 PDF 的 Abstract 是什么关系？这对你快速读论文有什么用？

> **答案**：README 的 Abstract 就是论文摘要的转载。用处：可以在读 PDF 之前先用 README（纯文本、可搜索、带链接）建立框架，再进 PDF 核对细节；反过来也可用 PDF 验证 README 转载是否有出入。

**练习 3**：仓库中 `Moonlight_intermediate_checkpoints.pdf` 的作用是什么？它与 README 的「Intermediate Checkpoints」小节有什么不一致？

> **答案**：它说明训练中间检查点的发布情况（供研究训练过程用）；README 该小节仍写 "Coming soon..."，但仓库已实际包含这份 PDF，说明 README 文案滞后于实际发布状态，应以 PDF / HuggingFace 为准。

## 5. 综合实践

把本讲全部内容串起来的任务：**建立你的 Moonlight 学习工作区，并生成你的「三问清单」**。

1. **目标**：完成环境准备 + 文档初读 + 问题沉淀，为第二讲实际运行训练扫清障碍。
2. **操作步骤**：
   1. 按下图完成「克隆 → 虚拟环境 → 装依赖」：

      ```bash
      git clone https://github.com/MoonshotAI/Moonlight.git
      cd Moonlight
      python3 -m venv .venv && source .venv/bin/activate
      pip install -r requirements.txt
      ```

   2. 浏览仓库结构（`ls` 对照 4.3.1 的目录树），确认 `examples/toy_train.py`、两份 PDF、`requirements.txt` 各就各位。
   3. 通读 [README.md](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md) 一遍（此时可跳过推理代码细节），重点标记 Abstract、Key Ingredients、对比表、训练命令四块。
   4. 打开 `Moonlight.pdf` 只读摘要与引言（按 4.4.2 的路线）。
   5. 写下三个你最想弄懂的问题（可参考 4.4.4 的示例），存入仓库外的个人笔记。
   6. 预习热身（可选，不下载数据集、不建模型，几乎零成本）：

      ```bash
      python3 examples/toy_train.py --help
      ```

3. **需要观察的现象**：依赖安装无报错；`--help` 打印出 `--model`、`--optimizer`、`--lr`、`--wd`、`--dataset`、`--hidden_size` 六个参数的用法说明（它们定义在 [examples/toy_train.py:319-326](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L319-L326)）。
4. **预期结果**：`--help` 的具体输出格式**待本地验证**，但参数集合应与上述六个一致——这正是下一讲 u1-l2 的起点。
5. **交付物**：三问清单 + 一个能 `import torch` 的虚拟环境。

## 6. 本讲小结

- Moonlight 仓库是 Moonshot AI 技术报告《Muon is Scalable for LLM Training》（arXiv:2502.16982）的配套开源项目，核心结论是：经改造的 Muon 可以可靠地用于大规模训练，达到同等性能约只需 AdamW 52% 的训练 FLOPs（≈2 倍计算效率）。
- 两大关键技术：**加权重衰减** 与 **按参数形状调整更新尺度（\(0.2\sqrt{\max(A,B)}\)，使更新 RMS 一致化为约 0.2）**——两者都能在 `examples/toy_train.py` 中找到对应代码点（`p.data.mul_(1 - lr*wd)` 与 `adjust_lr_for_muon`）。
- 最终验证载体是 Moonlight 模型：16B 总参数 / 3B 激活参数的 MoE，5.7T tokens，在与 DSV2-Lite 完全同规模的对照下各基准全面领先（MMLU 70.0 vs 58.3）。
- 仓库结构极简：唯一的源码文件 `examples/toy_train.py`（约 360 行）承载了 Muon 完整实现与 Qwen2 玩具训练管线；分布式工业实现在外部 Megatron-LM PR #1428，不在本仓库。
- 仓库还发布了预训练 / 指令微调 / 中间检查点（HuggingFace `moonshotai/Moonlight-16B-A3B` 系列），且 Moonlight 与 DeepSeek-V3 同构，可被 vLLM / SGLang 等主流引擎部署（[README.md:128](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L128)）。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：《跑通第一个训练：环境、命令与参数》。你将实际运行 `python3 examples/toy_train.py --model qwen --optimizer muon ...`，逐个理解 `--model/--optimizer/--lr/--wd/--dataset/--hidden_size` 六个参数，并观察两种优化器的 loss 走向。
- **提前预热**：打开 `examples/toy_train.py` 的 `__main__` 部分（第 316-359 行），数一数你能认出几个组件（argparse？DataLoader？loss.backward？）——认不全很正常，第一单元结束时你会全部认识。
- **带着问题读**：把你综合实践里写下的三问清单贴在显眼处；每完成一讲就检查是否有问题被回答了。
- **想先睹为快论文**：可先只读 `Moonlight.pdf` 的摘要与结论段，方法章节等学完第二单元再看会顺畅得多。
