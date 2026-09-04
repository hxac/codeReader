# 搭建最小实验台：Standard 与 AttnRes 对比训练

## 1. 本讲目标

前三个进阶讲义把 Block AttnRes 的零件逐一读透了：u2-l1 逐行精读了 `block_attn_res` 的两次 `einsum`，u2-l2 精读了 `forward` 的块边界调度，u2-l3 打开了 proj 与 norm 两个黑盒并算清了开销。但到目前为止，所有分析都是**静态**的——只做过单次前向，权重永远停在初始化状态，「AttnRes 更好」这个判断完全来自 README 的图表，我们从未亲手训练过任何一个 AttnRes 模型。

本讲补上这一环：搭建一个**可训练的最小实验台**（字符级语言模型），把前几讲实现的组件原样接入，与标准残差基线在**相同数据、相同参数主干、相同训练预算**下对比训练。README 声称 AttnRes 是「a drop-in replacement for standard residual connections」（[README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)），本讲要把「drop-in」二字变成可运行的代码，把「更好」变成可检验的损失曲线。

学完本讲，你应该能够：

1. 把 README 的层内伪代码**装配**成完整模型：双状态 `(blocks, partial_block)` 穿过全部 L 层，词嵌入进入 `blocks[0]`，末端接 LM 头。
2. 用**同一个 `Block` 类 + 一个模式开关**实现标准残差基线与 Block AttnRes 两种形态，让「唯一不同的变量」就是残差接线方式——这是控制变量法的代码形态。
3. 掌握对比训练的完整流程：参数量对齐与如实汇报、配对种子、训练/验证损失曲线、最终差值的判读与诚实报告。
4. 会用三个低成本 sanity check（初始损失 ≈ ln V、单批过拟合、参数量手算）在训练前排除实现 bug。

本讲的主实践即任务规格指定的对比实验：在同一个字符级小语料上，训练参数量与步数完全对齐的两个模型，绘制训练/验证损失曲线并汇报最终验证损失差值。

## 2. 前置知识

### 2.1 语言模型在做什么：预测下一个字符

字符级语言模型的任务是：给定前缀，预测下一个字符。训练目标是最小化交叉熵损失：

\[ \mathcal{L} = -\frac{1}{BT}\sum_{b=1}^{B}\sum_{t=1}^{T} \log p_\theta\!\left(x_{b,t+1} \mid x_{b,1:t}\right), \qquad \mathrm{ppl} = e^{\mathcal{L}} \]

其中 B 是批大小、T 是序列长度、V 是字符表大小。困惑度 ppl 是损失的可读版本：字符级 ppl ≈ 4.5 意味着「模型平均把每个字符的不确定性压缩到约 4.5 个等价选项」。

为什么实验台选字符级而不是 BPE/子词级？

- **零依赖**：`sorted(set(text))` 两行就是分词器，不需要任何分词库；
- **词表小**（通常几十到一百多）：LM 头参数少，小模型的容量几乎全部花在主干上，残差方式的差异更「干净」；
- **信号密度高**：每个字符都是监督信号，小语料也能训出有意义的损失下降。

一个重要的理论检查点：**未训练模型的初始损失应约等于 \(\ln V\)**。随机初始化的网络 logits 接近零，softmax 输出近均匀 \(p \approx 1/V\)，于是 \(\mathcal{L}_0 = -\ln(1/V) = \ln V\)。V=65 时约为 4.17。若实测显著偏离，说明实现有 bug——这是 2.3 节三个 sanity check 之一。

### 2.2 控制变量法：让「结构」成为唯一变量

对比两种残差方案时，凡是能影响损失的因素都必须**钉死**，否则看到的差异无法归因：

| 必须相同的变量 | 原因 |
|:---|:---|
| 语料、训练/验证切分、字符表 | 数据分布不同，损失不可比 |
| 层数 L、宽度 d、头数、MLP 扩展倍数 | 主干容量不同，损失不可比 |
| 优化器、学习率、weight decay、梯度裁剪、步数 | 优化轨迹不同，损失不可比 |
| batch、序列长度 T | 每步处理的数据量与上下文长度必须一致 |
| 随机种子（配对，见 4.4） | 消除数据抽样噪声 |
| 评测协议（同一验证集、同一窗口采样方式） | 评测本身的方差要压到最低 |

唯一**允许不同**的是残差接线本身。u2-l3 已算出它带来的参数增量是每层 \(4d\)（两位点 × \(2d\)），所以「参数量对齐」的准确含义是：**主干完全相同 + attn_res 增量如实汇报**。以本讲对比配置（d=256、L=16）为例，增量为 \(4 \times 256 \times 16 = 16384\) 个参数，约占主干 0.13%——远小于种子间方差。是否要强行补齐到绝对相等，4.4 节专门讨论。

### 2.3 三个低成本 sanity check

在启动任何长训练之前，先花一分钟做三件事，能挡住绝大多数实现 bug：

1. **初始损失检查**：未训练模型在随机批次上的损失应 ≈ \(\ln V\)（2.1 节推导）。偏高偏低都说明 forward 或损失计算有问题。
2. **单批过拟合检查**：固定一个批次反复训练几百步，损失应能降到接近 0。若降不下去，说明梯度通路断了（比如某处 `detach` 了、或状态没有跨层传递）。
3. **参数量手算检查**：程序数出的参数量应与手工推导一致。对无偏置、MLP 扩展 4 倍的配置，每层主干为 \(4d^2\)（注意力）+ \(8d^2\)（MLP）+ \(2d\)（两个 norm 增益）；attnres 模式再每层加 \(4d\)（u2-l3 的结论）。

### 2.4 符号与默认配置

| 符号 | 含义 | 冒烟配置 | 对比配置 |
|:---:|:---|:---:|:---:|
| V | 字符词表大小 | 取决于语料 | 取决于语料 |
| B / T | 批大小 / 序列长度 | 32 / 128 | 64 / 256 |
| d | 隐藏维度 | 64 | 256 |
| L | transformer 层数 | 8 | 16 |
| block_size | 每块**子层**数（ATTN+MLP 计，每层 2 个） | 4 | 4 |
| k = block_size//2 | 每块**层数** | 2 | 2 |
| N | 末端已完成块数（含嵌入块） | 4 | 8 |
| seeds | 随机种子 | 0 | 0, 1, 2 |

k=2、L=16 时末端候选数为 8 块 + 1 部分和 = 9，正好落在 README「With ~8 blocks, it recovers most of Full AttnRes's gains」（[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)）的推荐邻域——这是选 L=16、block_size=4 的原因。

## 3. 本讲源码地图

本仓库是论文发布仓库，**没有任何可运行的工程代码**（u1-l1 已确认：仅 README、论文 PDF 与四张图片共 6 个文件）。因此本讲的策略是：以 README 伪代码为**集成蓝图**，其余训练设施（数据、循环、评测）全部是本讲义编写的「示例代码」。

| 位置 | 内容 | 本讲用途 |
|:---|:---|:---|
| [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33) | AttnRes 定位：drop-in 替换 | 4.2 节：drop-in 的代码含义 |
| [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) | 标准残差：固定单位权重累加 | 4.2 节：基线的定义出处 |
| [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) | Block 划分、~8 块、边际开销 | 2.4 / 4.3 节：block_size 配置依据 |
| [README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) | PyTorch 风格伪代码全块 | 4.2 / 4.3 节：集成蓝图 |
| [README.md:L95-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L95-L99) | Scaling law：各计算预算下持续占优、匹配 1.25 倍计算基线 | 4.4 节：损失曲线要迷你复现的结论形态 |
| [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) | 下游结果出自 Kimi Linear 48B / 1.4T tokens | 4.4 节：规模差异的诚实声明 |
| [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123) | 训练动态：幅度有界、梯度均匀 | 第 7 节：预告 u2-l5 |
| `Attention_Residuals.pdf` | 论文全文 | 训练超参细节以论文为准（待确认） |
| `assets/scaling_law.png` | 损失-计算量曲线 | 4.4 节对照 |

## 4. 核心概念与源码讲解

本讲的四个最小模块按「先造轮子和地基，再装发动机，最后上路比赛」的顺序展开：

1. **4.1 最小训练脚本**——数据、批次采样、训练循环、评测，与残差方式无关的公共设施；
2. **4.2 标准残差基线模型**——把伪代码「拆掉」AttnRes 后剩下的骨架，也是控制变量的参照系；
3. **4.3 Block AttnRes 模型**——把 u2-l1 的函数与 u2-l2 的调度装配进完整模型，双状态穿过 L 层；
4. **4.4 对比训练流程**——控制变量清单、配对种子、损失曲线的画法与判读。

### 4.1 最小训练脚本：字符语料、批次采样与训练循环

#### 4.1.1 概念说明

训练脚本要回答四个问题：**数据从哪来、批次怎么采、梯度怎么更新、效果怎么量**。它与残差方式完全解耦——这正是它作为「公共设施」的价值：两种模型跑在同一个训练脚本上，差异只能来自模型本身。

- **数据**：仓库内没有任何数据文件，任选一份几百 KB 以上的纯文本即可（如本地小说、合并的源码文件，或 nanoGPT 生态常用的 tiny shakespeare，字符表为 65；README 本身只有约 7KB，只够冒烟测试）。按 90/10 切分训练/验证集。
- **批次采样**：从训练集随机截取 `[i, i+T]` 作输入、`[i+1, i+T+1]` 作目标——目标就是输入右移一位，这是「预测下一个字符」的数据形态。
- **训练循环**：采样 → 前向算损失 → 反向 → 裁剪梯度 → AdamW 更新；每 `eval_every` 步在验证集上评一次。
- **评测**：验证损失必须**确定性**——每次评同样的窗口，否则曲线上的抖动分不清是模型变化还是评测噪声。

#### 4.1.2 核心流程

```text
build_corpus(path):
    text → 字符表 V → 全文编码为 id 序列 → 90/10 切分

get_batch(ids, T, B):
    随机取 B 个起点 i → x = ids[i:i+T], y = ids[i+1:i+T+1]

train(model, seed):
    重设种子(配对数据流, 见 4.4)
    循环 steps 步:
        x, y ← get_batch;  loss ← model(x, y)
        反向 + 裁剪 + AdamW 更新
        每 eval_every 步: val ← evaluate(model)  # 确定性窗口
    返回 (step, train, val) 三列历史
```

每步消耗 \(B \times T\) 个字符的监督信号；两种模型的 steps、B、T 完全一致，即「计算预算对齐」的第一层含义。

#### 4.1.3 源码精读

**仓库没有训练代码——这是本讲必须首先承认的事实。** README 的 Results 部分直接以「损失 vs 计算预算」的形态给出证据：

> [README.md:L95-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L95-L99)
> `### Scaling Laws` ... AttnRes consistently outperforms the baseline across all compute budgets. Block AttnRes matches the loss of a baseline trained with **1.25x more compute**.

「across all compute budgets」说明论文的对比方式是**同一坐标系下两条损失曲线**（横轴计算量、纵轴损失，见 `assets/scaling_law.png`）——本讲实验台产出的正是这张图的迷你版：横轴换成训练步数。训练超参（优化器、学习率、调度）README 一概未给，以论文为准（待确认）；因此下面脚本里的超参是本讲义自定的「示例代码」，对两种模型完全一致即可。

#### 4.1.4 代码实践：搭好数据与评测设施，先验证「度量衡」

1. **实践目标**：建立语料、批次采样与确定性评测三件套；并用一个与模型无关的小实验验证「未训练 ⇒ 损失 ≈ \(\ln V\)」这把尺子本身是准的。
2. **操作步骤**：

```python
# 示例代码
import torch
import torch.nn.functional as F

def build_corpus(path, train_frac=0.9):
    text = open(path, encoding='utf-8').read()
    chars = sorted(set(text))
    stoi = {c: i for i, c in enumerate(chars)}
    ids = torch.tensor([stoi[c] for c in text], dtype=torch.long)
    n = int(len(ids) * train_frac)
    return ids[:n], ids[n:], len(chars)

def get_batch(ids, block, batch):
    ix = torch.randint(len(ids) - block, (batch,))
    x = torch.stack([ids[i:i + block] for i in ix])
    y = torch.stack([ids[i + 1:i + block + 1] for i in ix])
    return x, y

@torch.no_grad()
def evaluate(model, ids, block, n_windows=100):
    """按固定步长取窗口, 确定性评估, 不引入评测随机性。"""
    model.eval()
    stride = max(1, (len(ids) - block - 1) // n_windows)
    losses = []
    for start in range(0, len(ids) - block - 1, stride):
        x = ids[start:start + block].unsqueeze(0)
        y = ids[start + 1:start + block + 1].unsqueeze(0)
        _, loss = model(x, y)
        losses.append(loss.item())
    model.train()
    return sum(losses) / len(losses)

train_ids, val_ids, V = build_corpus('input.txt')       # 任选本地 txt
print(f"语料 {len(train_ids) + len(val_ids)} 字符, 词表 V = {V}")
print(f"未训练模型的期望初始损失 ln(V) = {torch.log(torch.tensor(float(V))):.4f}")

# 度量衡自检: 用「近均匀 logits」模拟未训练模型, CE 应 ≈ ln(V)
torch.manual_seed(0)
mock_logits = 0.01 * torch.randn(10_000, V)             # 小随机 logits ≈ 未训练输出
mock_targets = torch.randint(0, V, (10_000,))
print("mock CE =", F.cross_entropy(mock_logits, mock_targets).item())
```

3. **需要观察的现象**：`mock CE` 与 `ln(V)` 打印值在小数点后两位内一致（V=65 时都约为 4.17）。
4. **预期结果**：一致（这是交叉熵在近均匀分布上的解析性质）；若差距大，检查损失是否算错。语料字符数与 V 取决于你选的文本，具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么未训练模型的交叉熵损失近似 \(\ln V\)？这个检查为什么有价值？

> **答案**：随机初始化使 logits 接近零，softmax 近均匀 \(p \approx 1/V\)，\(\mathcal{L} = -\ln(1/V) = \ln V\)。价值在于它是**不训练就能算出的理论值**：实测显著偏离即可在花任何训练时间之前定位 forward/损失实现的 bug。

**练习 2**：为什么对比实验必须看验证损失，而不能只看训练损失？

> **答案**：训练损失可以靠记忆语料下降，与泛化无关；结构对比问的是「同等数据和预算下谁学得更好」，验证损失度量的才是对未见文本的泛化。此外两模型参数量有 4Ld 的微小差异，验证损失是更公平的口径。

**练习 3**：`evaluate` 为什么按固定步长扫窗口，而不是像 `get_batch` 一样随机采样？

> **答案**：评测需要**可复现且低噪**。随机采样会让两次评测之间产生与模型无关的波动，混入损失曲线后掩盖真实的结构差异；固定窗口让每个评测点的噪声来源只剩模型本身。

### 4.2 标准残差基线模型：伪代码「拆掉」AttnRes 后剩下的骨架

#### 4.2.1 概念说明

基线就是最普通的 PreNorm Transformer：残差主干上，子层输出按固定权重 1 逐个累加——README 对它的描述正是本实验要挑战的对象：

> [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37)
> Standard residual connections accumulate all layer outputs with fixed unit weights.

本讲实现基线的方式刻意「绕远」：**不用一套独立代码，而是与 AttnRes 共用同一个 `Block` 类**，只留一个 `mode` 开关。这样做的理由是控制变量法的代码化——注意力、MLP、两个 norm 的模块代码只有一份，两种模型不可能在共享部分出现任何差异，唯一的不同被压缩到「喂给子层的 `h` 从哪里来」这一件事上。这也正是 [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)「drop-in replacement」的可执行含义：替换只发生在两个位点上，其余逐行不变。

**基线与 AttnRes 在伪代码上的逐行对应**（`mode` 开关拨到哪边，哪一列生效）：

| README 伪代码 | `mode='attnres'` | `mode='standard'`（基线） |
|:---|:---|:---|
| L68 `partial_block = hidden_states` | 部分和从输入起算 | **残差流**从输入起算 |
| L71 `h = block_attn_res(..., attn_res_*)` | 聚合得 `h` | `h = partial`（直接读残差流） |
| L75-L77 边界判断与封存 | 生效，blocks 增长 | 跳过，blocks 恒为空表 |
| L80-L81 `attn_out` 并入 partial | 相同 | 相同 |
| L84 `h = block_attn_res(..., mlp_res_*)` | 聚合得 `h` | `h = partial` |
| L87-L88 `mlp_out` 并入 partial | 相同 | 相同 |
| L90 `return blocks, partial_block` | 双状态 | `(空表, 残差流)` |

可以看出：**标准残差 = Block AttnRes 去掉「封存」与「聚合」两个动作后的退化形态**。u1-l2 讲过的幅度膨胀与稀释问题，在这个退化形态里原样保留——这正是实验的对照基础。

#### 4.2.2 核心流程

统一 `Block` 的前向（`mode` 分支只在两处出现）：

```text
输入: blocks(列表), hidden_states [B,T,D]
partial = hidden_states                          # L68
h = block_attn_res(blocks, partial, attn_res_*)  # L71   [attnres 模式]
h = partial                                      #       [standard 模式]
若 attnres 且 layer_number % (block_size//2) == 0:       # L75
    blocks ← blocks + [partial];  partial ← None         # L76-L77
attn_out = attn(attn_norm(h))                    # L80   两种模式相同
partial = attn_out if partial is None else partial + attn_out   # L81
h = block_attn_res(blocks, partial, mlp_res_*)   # L84   [attnres 模式]
h = partial                                      #       [standard 模式]
partial = partial + mlp(mlp_norm(h))             # L87-L88
返回 (blocks, partial)                           # L90
```

模型级流程（两种模式共用）：

```text
state = 词嵌入(idx) + 位置嵌入(pos)     # 「token embedding」实际是两者之和
blocks = []
for 每一层:  blocks, state = layer(blocks, state)
logits = LM头(final_norm(state))
loss = CE(logits, 右移一位的目标)
```

注意两个装配决定：位置嵌入是本讲义补充的（伪代码只画层内，不含模型级细节；两模型同配即公平），封存进 `blocks[0]` 的「嵌入」因此是「词嵌入+位置嵌入」之和；`layer_number` 按 u2-l2 的结论取 0 起点计数。

#### 4.2.3 源码精读

**子层这两行在两种模式下逐字相同**——drop-in 的「不动子层」证据：

> [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)
> `attn_out = self.attn(self.attn_norm(h))`
>
> [README.md:L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87)
> `mlp_out = self.mlp(self.mlp_norm(h))`

两种残差方案都执行这两行，差别只在 `h` 的来源（聚合结果还是残差流）。因此实验里注意力与 MLP 的**模块代码、参数形状、初始化方式完全一致**，公平性有代码结构保证。

**部分和/残差流的起点**：

> [README.md:L68](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L68)
> `partial_block = hidden_states`

基线模式下这行就是标准残差的「主干继承」：`x_{l}` 直接作为第 l 层的起点，子层输出按权重 1 加回主干——u1-l2 精读过的 \(\mathbf{h}_l = \mathbf{h}_{l-1} + \mathbf{v}_l\) 递推。

#### 4.2.4 代码实践：实现共用骨架 + 基线三连检

1. **实践目标**：写出两种模式共用的全部模块代码（本讲核心代码资产，4.3 直接复用）；对基线模式做三个 sanity check：初始损失 ≈ \(\ln V\)、单批可过拟合、参数量与手算一致。
2. **操作步骤**：

```python
# 示例代码: 两种残差形态共用的模型骨架 (mode 开关)
import torch
import torch.nn as nn
import torch.nn.functional as F

class RMSNorm(nn.Module):
    """与 nn.RMSNorm 等价的手工实现(兼容旧版 PyTorch), 参数量恰为 d。"""
    def __init__(self, d, eps=1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(d))
        self.eps = eps
    def forward(self, x):
        rms = x.pow(2).mean(-1, keepdim=True).add(self.eps).rsqrt()
        return x * rms * self.weight

class CausalSelfAttention(nn.Module):
    def __init__(self, d, n_head):
        super().__init__()
        assert d % n_head == 0
        self.n_head, self.hd = n_head, d // n_head
        self.qkv = nn.Linear(d, 3 * d, bias=False)      # 参数 3d^2
        self.proj = nn.Linear(d, d, bias=False)         # 参数 d^2
    def forward(self, x):                               # [B, T, D]
        B, T, D = x.shape
        q, k, v = self.qkv(x).split(D, dim=2)
        q = q.view(B, T, self.n_head, self.hd).transpose(1, 2)
        k = k.view(B, T, self.n_head, self.hd).transpose(1, 2)
        v = v.view(B, T, self.n_head, self.hd).transpose(1, 2)
        att = (q @ k.transpose(-2, -1)) / self.hd ** 0.5
        mask = torch.triu(torch.ones(T, T, dtype=torch.bool, device=x.device), 1)
        y = F.softmax(att.masked_fill(mask, float('-inf')), dim=-1) @ v
        return self.proj(y.transpose(1, 2).reshape(B, T, D))

class MLP(nn.Module):
    def __init__(self, d, expansion=4):
        super().__init__()
        self.fc1 = nn.Linear(d, expansion * d, bias=False)   # 4d^2
        self.fc2 = nn.Linear(expansion * d, d, bias=False)   # 4d^2
    def forward(self, x):
        return self.fc2(F.gelu(self.fc1(x)))

def block_attn_res(blocks, partial_block, proj, norm):
    """README L53-L65 原样搬运 (u2-l1 已逐行精读)。"""
    V = torch.stack(blocks + [partial_block])              # [N+1, B, T, D]
    K = norm(V)
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
    h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
    return h

class Block(nn.Module):
    """一个 transformer 层。mode='standard' 即基线; mode='attnres' 见 4.3。"""
    def __init__(self, d, n_head, layer_number, block_size, mode, expansion=4):
        super().__init__()
        self.mode, self.layer_number, self.block_size = mode, layer_number, block_size
        self.attn, self.mlp = CausalSelfAttention(d, n_head), MLP(d, expansion)
        self.attn_norm, self.mlp_norm = RMSNorm(d), RMSNorm(d)
        if mode == 'attnres':                    # u2-l3: 每位点 2d、每层 4d 参数
            self.attn_res_proj = nn.Linear(d, 1, bias=False)
            self.attn_res_norm = RMSNorm(d)
            self.mlp_res_proj = nn.Linear(d, 1, bias=False)
            self.mlp_res_norm = RMSNorm(d)

    def forward(self, blocks, hidden_states):
        partial = hidden_states                                   # L68
        if self.mode == 'attnres':                                # L71 位点 1
            h = block_attn_res(blocks, partial,
                               self.attn_res_proj, self.attn_res_norm)
        else:
            h = partial
        if self.mode == 'attnres' and \
           self.layer_number % (self.block_size // 2) == 0:       # L75 边界
            blocks, partial = blocks + [partial], None            # L76-L77
        attn_out = self.attn(self.attn_norm(h))                   # L80
        partial = partial + attn_out if partial is not None else attn_out  # L81
        if self.mode == 'attnres':                                # L84 位点 2
            h = block_attn_res(blocks, partial,
                               self.mlp_res_proj, self.mlp_res_norm)
        else:
            h = partial
        partial = partial + self.mlp(self.mlp_norm(h))            # L87-L88
        return blocks, partial                                    # L90

class MiniGPT(nn.Module):
    def __init__(self, vocab, d, n_head, n_layer, block_size, mode, t_max=256):
        super().__init__()
        self.emb = nn.Embedding(vocab, d)
        self.wpe = nn.Embedding(t_max, d)     # 位置嵌入: 本讲义补充, 两模式同配
        self.layers = nn.ModuleList(
            Block(d, n_head, i, block_size, mode) for i in range(n_layer))
        self.final_norm = RMSNorm(d)
        self.head = nn.Linear(d, vocab, bias=False)

    def forward(self, idx, targets=None):
        B, T = idx.shape
        state = self.emb(idx) + self.wpe(torch.arange(T, device=idx.device))
        blocks = []                          # 每次前向重置; 第 0 层封存嵌入
        for layer in self.layers:
            blocks, state = layer(blocks, state)
        logits = self.head(self.final_norm(state))
        if targets is None:
            return logits, None
        loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)),
                               targets.reshape(-1))
        return logits, loss

# ---- 基线三连检 (d=64, L=8 的冒烟配置) ----
cnt = lambda m: sum(p.numel() for p in m.parameters())
torch.manual_seed(0)
base = MiniGPT(V, d=64, n_head=4, n_layer=8, block_size=4, mode='standard')

x, y = get_batch(train_ids, block=128, batch=8)          # 检查 1: 初始损失
with torch.no_grad():
    _, l0 = base(x, y)
hand = 8 * (12 * 64**2 + 2 * 64) + V*64 + 128*64 + V*64 + 64   # 检查 3: 手算
print(f"初始损失 {l0.item():.4f} vs ln(V)={torch.log(torch.tensor(float(V))):.4f}")
print(f"参数量 {cnt(base)} vs 手算 {hand}")

opt = torch.optim.AdamW(base.parameters(), lr=1e-3)      # 检查 2: 单批过拟合
for i in range(300):
    _, loss = base(x, y)
    opt.zero_grad(); loss.backward(); opt.step()
    if i % 100 == 0:
        print(f"过拟合 step {i}: loss {loss.item():.4f}")
```

3. **需要观察的现象**：初始损失与 \(\ln V\) 两位小数内一致；参数量与手算完全相等（手算式：每层 \(12d^2+2d\)，加 emb/wpe/head/final_norm）；单批 300 步后损失从约 4.2 降到远低于 1。
4. **预期结果**：三条全部通过则基线实现可信。参数量等式是确定性的（结构决定），应严格相等；损失数值待本地验证。若单批过拟合失败，优先检查 `Block` 是否忘记返回/接收 `partial`。

#### 4.2.5 小练习与答案

**练习 1**：基线模式中，子层读到的 `h` 对应伪代码里的哪个量？AttnRes 模式把它换成了什么？

> **答案**：`h = partial`，即当前残差流（伪代码中的部分和变量）；AttnRes 模式换成 `block_attn_res` 的聚合输出。整个 drop-in 替换在层内就发生在这两处赋值上（L71、L84）。

**练习 2**：写出标准 PreNorm 层的递推式，并与伪代码 L80-L81、L87-L88 对应。

> **答案**：\(\mathbf{x}' = \mathbf{x} + \mathrm{Attn}(\mathrm{Norm}(\mathbf{x}))\)，\(\mathbf{x}'' = \mathbf{x}' + \mathrm{MLP}(\mathrm{Norm}(\mathbf{x}'))\)。L80-L81 是第一个式子（`attn_out` 按权重 1 加回主干），L87-L88 是第二个式子。

**练习 3**：如果在基线模式里也保留 L75-L77 的封存分支会发生什么？这说明什么？

> **答案**：`blocks` 会不断增长却从不被读取（基线没有 attn_res 位点），白白多存 \(N\) 份激活、拖慢训练，行为与不封存完全等价。说明「封存」与「聚合」必须成对出现——块缓存存在的唯一意义就是作为深度注意力的候选。

### 4.3 Block AttnRes 模型：双状态穿过全部 L 层

#### 4.3.1 概念说明

4.2 的 `Block` 类其实已经把 AttnRes 模式写完了——`mode='attnres'` 一拨即用。本模块关注的是**装配层**的五个新问题，它们在单层伪代码里看不到，只有把 L 层串起来才会冒出来：

1. **双状态线程化**：每层的签名是 `forward(self, blocks, hidden_states) -> (blocks, partial_block)`，返回值里携带两个状态。模型装配的直接后果就是一个显式循环：`for layer: blocks, state = layer(blocks, state)`。u2-l2 强调过「张量重绑定不返回即丢失」——这里就是那条规则在模型级的落点。
2. **每次前向重置 `blocks = []`**：`blocks` 是「本批样本的前向历史」，跨批次复用会导致形状错误与梯度串批。伪代码把状态放在参数与返回值里传递（而非存在 `self` 上），天然避免了这个问题。
3. **词嵌入如何进入候选**：第 0 层的边界判断 `0 % k == 0` 恒真，于是（含位置嵌入的）词嵌入在进入第一个注意力之前就被封存为 `blocks[0]`——伪代码 L70 的注释「blocks already include token embedding」即此意（u2-l2 已推导，这里只看它在装配里的位置）。
4. **末端输出接哪根线**：README 只给了层内代码，模型级「最后一层输出什么给 LM 头」未给出（待确认）。本讲选**最小方案**：最后一层返回的 `partial_block`（最后一个块的部分和）经 `final_norm` 后接 LM 头——它是「最后一个子层输出可直接到达输出」的自然选择。可选变体是在 head 前再做一次 `block_attn_res` 聚合（需新增一对 head 侧 proj/norm），让全部块直接可达输出；两种接法孰优，论文未说明，可作为消融（待本地验证）。
5. **实现细节的等价改写**：伪代码 L76 用原地 `blocks.append(partial_block)`，本讲写成 `blocks = blocks + [partial]`——语义相同（都是尾部追加一个元素），函数式写法避免共享列表的副作用；Python 列表操作本身不参与自动微分，两种写法梯度行为一致。

另有一个值得注意的初始化现象：u2-l3 证明初始深度权重近均匀（约 \(1/(N{+}1)\)），所以初始时 `h` 近似各候选的**平均**而非基线的**求和**，幅度天然小一截——但子层入口的 `attn_norm(h)` 会把尺度归一掉（RMSNorm 输出均方根恒定），PreNorm 的包裹让这次 drop-in 在尺度上是安全的。

#### 4.3.2 核心流程

模型级前向（attnres 模式）：

```text
state = 词嵌入 + 位置嵌入;  blocks = []
第 0 层:  位点1 候选 = [嵌入] (仅 1 个, 退化为恒等)
          边界触发 → blocks = [嵌入], partial 从 attn_out 起算
第 l 层:  位点1/位点2 候选 = 已封存块 + 当前部分和
          每 k 层一次边界 → 封存部分和, partial 清空重起
末端:  state = 最后的 partial_block → final_norm → LM 头
```

候选数随深度增长的闭式（\(k = \text{block\_size}//2\)，层号 0 起点）：

\[ C_{\text{attn 位点}}(l) = \left\lfloor \tfrac{l+k-1}{k} \right\rfloor + 1, \qquad C_{\text{mlp 位点}}(l) = \left\lfloor \tfrac{l}{k} \right\rfloor + 2 \]

即候选数每 k 层加一。对照三档配置（L=16）：k=1（`block_size=2`）时每层都是边界，候选一路涨到 17——**Block 退化成 Full AttnRes**；k=2 时末端 9 个候选（8 块 + 部分和），对齐 README L47 的「~8 blocks」；k=16 时永远只有 2 个候选（嵌入 vs 全模型部分和）。块大小就是在 Full 与极粗块之间连续可调的旋钮，这正是 u3-l1 要系统扫描的参数。

#### 4.3.3 源码精读

**层的签名决定了模型的装配方式**：

> [README.md:L67](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67)
> `def forward(self, blocks: list[Tensor], hidden_states: Tensor) -> tuple[list[Tensor], Tensor]:`

输入输出都带 `blocks`——状态必须由**调用方**（模型外壳）逐层搬运。对比标准写法 `forward(self, x) -> x`，这正是本模型外壳必须写成显式循环、不能用 `nn.Sequential` 的原因。

**嵌入入块的注释**：

> [README.md:L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L70)
> `# blocks already include token embedding`

装配层要保证的不变式：除第 0 层位点 1 外，任何位点被调用时 `blocks[0]` 都是词嵌入（第 0 层位点 1 时它以部分和身份出现在候选里）——`MiniGPT.forward` 里 `blocks = []` 起步 + 第 0 层边界封存共同实现它。

**边界与封存（u2-l2 已逐行精读，此处仅看装配位置）**：

> [README.md:L75-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L75-L77)
> `if self.layer_number % (self.block_size // 2) == 0:` / `blocks.append(partial_block)` / `partial_block = None`

边界判断夹在两个 attn_res 位点之间（L71 之后、L80 之前）：旧块在封存前被位点 1 完整读取最后一次，新块从本层 `attn_out` 白纸起算。

**块数推荐的配置依据**：

> [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)
> With ~8 blocks, it recovers most of Full AttnRes's gains while serving as a practical drop-in replacement with marginal overhead.

L=16、block_size=4 使末端恰为 8 块 + 部分和，这是本讲对比配置的选择依据。

**计算单元原样搬运**：[README.md:L53-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L65) 的 `block_attn_res` 不做任何修改地进入实验台（见 4.2.4 代码），u2-l1 已逐行精读。

#### 4.3.4 代码实践：装配检查三连

1. **实践目标**：验证 AttnRes 装配的三个确定性性质——(i) 参数量差**恰好**等于 \(4dL\)；(ii) 第 0 层位点 1 退化为恒等（softmax 对单候选 = 1）；(iii) 每个位点的候选数严格符合 4.3.2 的闭式。
2. **操作步骤**：

```python
# 示例代码 (接 4.2.4 的类定义)
cnt = lambda m: sum(p.numel() for p in m.parameters())

# (i) 参数量差恰为 4dL —— 与语料/词表无关的结构不变量
D, L, BS = 256, 16, 4
base = MiniGPT(V, d=D, n_head=8, n_layer=L, block_size=BS, mode='standard')
att  = MiniGPT(V, d=D, n_head=8, n_layer=L, block_size=BS, mode='attnres')
print("参数差:", cnt(att) - cnt(base), "理论 4dL =", 4 * D * L,
      f"相对增量 {100*(cnt(att)-cnt(base))/cnt(base):.3f}%")

# (ii) blocks 为空时, block_attn_res 退化为恒等
torch.manual_seed(0)
proj, norm = nn.Linear(64, 1, bias=False), RMSNorm(64)
x = torch.randn(2, 5, 64)
print("单候选时 h == x:", torch.allclose(block_attn_res([], x, proj, norm), x))

# (iii) 候选数轨迹探针: 运行时替换全局函数名, Block 内部按名字调用
block_attn_res_plain, seen = block_attn_res, []
def block_attn_res_probe(blocks, partial_block, proj, norm):
    seen.append(len(blocks) + 1)
    return block_attn_res_plain(blocks, partial_block, proj, norm)
block_attn_res = block_attn_res_probe

att_smoke = MiniGPT(V, d=64, n_head=4, n_layer=8, block_size=4, mode='attnres')
att_smoke(torch.zeros(2, 32, dtype=torch.long))        # 一次前向
block_attn_res = block_attn_res_plain                   # 恢复

k = 4 // 2
expected = []
for l in range(8):
    expected += [(l + k - 1) // k + 1,      # 该层 attn 位点
                 l // k + 2]                # 该层 mlp 位点
print("候选数轨迹符合公式:", seen == expected)
print("首 8 个位点候选数:", seen[:8], " 末端:", seen[-1])
```

3. **需要观察的现象**：参数差打印恰为 `4*256*16 = 16384`；`h == x` 为 `True`；候选数轨迹为 `True`，前 8 个位点为 `[1,2,2,2,2,3,3,3]`（L=8、k=2 时），末端为 5。
4. **预期结果**：三条都是**结构决定的确定性检查**，应全部严格通过（浮点上 (ii) 是精确恒等：单元素 softmax 恒为 1）；若 (iii) 失败，按 `expected` 逐层比对定位边界判断的层号起点是否弄错。相对增量 d=256 时约 0.13%（冒烟配置 d=64 时会升到约 0.5%，呼应 u2-l3 的 \(1/(3d)\) 规律）。

#### 4.3.5 小练习与答案

**练习 1**：为什么每次前向都要重新 `blocks = []`？如果把 `blocks` 存在 `self` 上跨批次复用会怎样？

> **答案**：`blocks` 是本批样本的深度历史，批次间不通用。跨批次复用轻则候选数越积越多（计算/显存膨胀）、梯度把无关批次串起来，重则因序列长度不同直接形状报错。伪代码用「参数进、返回值出」的状态管理，正是让状态的生命周期与单次前向严格绑定。

**练习 2**：`block_size=2`（k=1）时 Block AttnRes 退化成什么？这说明了什么？

> **答案**：每层都是边界 → 每层输出（连同嵌入）都成为独立候选 → 深度注意力覆盖所有前层输出，即 **Full AttnRes**（L=16 时末端 17 个候选）。说明 Full 与 Block 不是两套机制，块大小是同一机制在「候选粒度」上的旋钮：k=1 最细（Full），k 越粗越省显存（O(Ld) → O(Nd)，README L28 图注）。

**练习 3**：末端输出选 `final_norm(最后的 partial)` 而不是「最后一次聚合 h」或「再做一次聚合」，理由是什么？这个选择有唯一正确答案吗？

> **答案**：选 partial 的理由：它是「最后一个子层的输出直接可达 LM 头」的最小方案，且不引入任何新参数。没有唯一答案——README 未给模型级装配（待确认）；「head 前再聚合一次」的变体让全部块直接可达输出，代价是多一对 head 侧 proj/norm 与一次 `block_attn_res`。工程上的正确做法是把它当作消融项实测（待本地验证），而不是拍板。

### 4.4 对比训练流程：控制变量、配对种子与损失曲线判读

#### 4.4.1 概念说明

模型都就绪之后，「跑个训练比一比」仍然有三个容易踩坑的环节：

**其一，参数量的「对齐」怎么算**。任务要求「参数量完全对齐」，但 u2-l3 已证明 attnres 必然多出每层 \(4d\)（proj+norm 的职责所在，砍掉就不是 AttnRes 了）。本讲的处理：**主干逐项相同 + 增量如实汇报**（d=256、L=16 时 +0.13%）。不建议给基线补一个同规模的「哑参数」凑绝对相等——哑参数要么不参与计算（形同虚设），要么参与计算（反而改变了基线本身）。0.13% 的差异远小于种子方差，不会污染结论；关键是**写进报告**，而不是藏起来。

**其二，方差从哪来、怎么压**。小模型的损失曲线对随机性敏感，来源有三：初始化、数据批次顺序、评测窗口。评测窗口已被 4.1 的确定性 `evaluate` 消除；数据批次顺序用**配对种子**消除——在 `train()` 内部（而不是外部）重设种子，两种模型用同一 seed 训练时看到**完全相同的数据批次序列**（模型无 dropout，前向不消耗随机数），差异被进一步归因到「初始化 + 结构」上。剩下的初始化差异是处理效应的一部分，不可消除也不应消除。多跑 3 个种子、报均值 ± 标准差，是本规模的最低配。

**其三，曲线怎么读才不算过度解读**。本实验台产出的两条损失曲线，是 README 这张图的迷你版：

> [README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99)
> AttnRes consistently outperforms the baseline across all compute budgets. Block AttnRes matches the loss of a baseline trained with **1.25x more compute**.

判读规则：(i) 看验证损失而非训练损失；(ii) 差距要与种子间标准差比较，\(\Delta\) 不超过 std 就只能说「无显著差异」；(iii) 一次小规模实验**既不能证实也不能证伪**论文结论——README 的证据取自 Kimi Linear 48B / 3B 激活 / 1.4T tokens 的训练（[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)），我们的实验台小了五个数量级以上。实验台的价值是「把伪代码变成可提问的工具」，不是复现 48B 的数字。

另一个容易忽略的口径：**按步数对齐 ≈ 按计算量对齐**。u2-l3 4.4 节估算 attnres 每层 FLOPs 增量约 1% 量级，同 steps 下两模型计算量之差约 1%，可以接受；若要严格按 FLOPs 对齐（或做「1.25 倍计算」复现），用步数乘以每步开销折算即可。

#### 4.4.2 核心流程

单次配对实验：

```text
run_pair(seed, steps, ...):
    torch.manual_seed(seed); 构建基线模型        # 各自初始化
    hist_base = train(基线, seed, ...)           # train 内部再重设种子 → 数据流配对
    torch.manual_seed(seed); 构建 attnres 模型
    hist_att  = train(attnres, seed, ...)
    返回两条 (step, train, val) 历史
```

完整对比：`for mode × seed` 收集全部曲线 → 按模式分组绘验证损失（种子作淡色重复）→ 汇报各模式 final val 的均值 ± std 与 \(\Delta\)、ppl → （可选）基线加训 25% 步数复现「1.25 倍计算」的迷你对照。

#### 4.4.3 源码精读

**「drop-in」是公平对比的前提**：

> [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)
> This is the official repository for **Attention Residuals (AttnRes)**, a drop-in replacement for standard residual connections...

正因为替换只发生在两个位点（4.2 的对应表），两种模型才能共用除残差接线外的一切——代码结构本身就是控制变量法的执行。

**要迷你复现的结论形态**：[README.md:L95-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L95-L99)（Scaling Laws）与 `assets/scaling_law.png`：横轴计算预算、纵轴损失，AttnRes 曲线在各预算下低于基线。我们的迷你版把横轴换成步数，期望形态是「attnres 的 val 曲线整体不高于基线」。

**规模声明的出处**：

> [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)
> `### Downstream Performance (Kimi Linear 48B / 3B activated, 1.4T tokens)`

**下讲的接口**：[README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123)（幅度有界、梯度更均匀）——本讲搭好的实验台正是 u2-l5 挂 hook 测训练动态的载体。

#### 4.4.4 代码实践：写出可复现的配对训练函数

1. **实践目标**：实现带**内部配对种子**与确定性评测的 `train()`，并在冒烟配置下把两种模型各训 500 步，确认曲线行为正常（损失下降、无 NaN、attnres 不报错）。
2. **操作步骤**：

```python
# 示例代码
def train(model, train_ids, val_ids, steps, batch, block, lr,
          eval_every, seed):
    torch.manual_seed(seed)          # 关键: 训练内重设种子 → 同 seed 两种模型
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.1)
    hist = {'step': [], 'train': [], 'val': []}
    for step in range(steps):
        x, y = get_batch(train_ids, block, batch)
        _, loss = model(x, y)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        if step % eval_every == 0 or step == steps - 1:
            v = evaluate(model, val_ids, block)
            hist['step'].append(step)
            hist['train'].append(loss.item())
            hist['val'].append(v)
            print(f"step {step:5d} | train {loss.item():.4f} | val {v:.4f}")
    return hist

torch.manual_seed(0)
smoke = dict(d=64, n_head=4, n_layer=8, block_size=4,
             steps=500, batch=32, block=128, lr=3e-4, eval_every=100)
for mode in ['standard', 'attnres']:
    torch.manual_seed(0)
    m = MiniGPT(V, mode=mode, t_max=smoke['block'],
                **{k: v for k, v in smoke.items()
                   if k in ('d', 'n_head', 'n_layer', 'block_size')})
    print(f"--- {mode} ---")
    train(m, train_ids, val_ids, seed=0, **{k: v for k, v in smoke.items()
          if k not in ('d', 'n_head', 'n_layer', 'block_size')})
```

3. **需要观察的现象**：两种模型的第一个 eval 点损失都 ≈ \(\ln V\)；500 步内 val 单调或近似单调下降；attnres 模式无形状/显存错误且速度与基线相近。
4. **预期结果**：两条曲线都正常下降；此配置下两者的差距大概率肉眼难辨（规模太小，正是综合实践用对比配置 + 多种子的原因）。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：参数量差 0.13%，要不要给基线补哑参数凑到绝对相等？为什么？

> **答案**：不要。0.13% 远小于种子方差，对结论无实质影响；哑参数要么不被使用（骗计数器），要么被使用（改变了基线）。正确做法是主干逐项相同、增量如实写进报告——「对齐」的意义是**可比**，不是**相等**。

**练习 2**：为什么把 `torch.manual_seed(seed)` 放在 `train()` 内部，而不是脚本开头？

> **答案**：模型构建会消耗随机数（初始化），且两种模式的模块数量不同、消耗量不同；若只在开头设种子，训练开始时的随机状态已经不同，数据批次序列就不再配对。在 `train()` 内部重设，保证同 seed 的两种模型看到**完全相同**的批次序列——配对设计消除数据抽样噪声。

**练习 3**：实验结束时 attnres 的 final val 比基线低 0.02，且大于 3 个种子的标准差。能下什么结论？不能下什么结论？

> **答案**：能下：在本配置（该语料、该规模、该预算、该超参）下，Block AttnRes 的验证损失显著更低。不能下：(i) 外推到大规模训练（README 证据在 48B/1.4T tokens，规模差五个数量级以上）；(ii) 断言「泛化更好」而非「优化更快」——区分二者需要匹配计算的完整预算扫描（u3-l2 的多规模实验正是为此）。

## 5. 综合实践

### 5.1 任务：同语料、同预算的 Standard vs AttnRes 对比训练

把 4.1 的数据/训练设施、4.2 与 4.3 的模型骨架放进同一个文件（如 `minitest.py`），然后执行本任务：**在同一字符语料上，用完全对齐的参数主干与训练步数，训练标准残差基线与 Block AttnRes 各 3 个种子，绘制训练/验证损失曲线，汇报最终验证损失差值**。这是任务规格指定的主实践，也是前三个模块的串联。

产出物清单：

- 一张损失曲线图（`compare_val_loss.png`）：两种模式各 3 条验证损失曲线；
- 一张结果表（5.3 模板）：final val 的均值 ± std、\(\Delta\)、困惑度；
- 一段结论文字：差异是否显著、与 README 结论（L95-L99）的关系、实现层面的观察。

### 5.2 主脚本

```python
# 示例代码: 综合实践驱动脚本 (复用 minitest.py 中的全部定义)
import math
import matplotlib.pyplot as plt

CFG = dict(d=256, n_head=8, n_layer=16, block_size=4,   # k=2 → 末端 8 块+部分和
           t_max=256)
RUN = dict(steps=3000, batch=64, block=256, lr=3e-4, eval_every=250)
SEEDS = [0, 1, 2]

results = {}
for mode in ['standard', 'attnres']:
    for seed in SEEDS:
        torch.manual_seed(seed)                          # 各自初始化
        model = MiniGPT(V, mode=mode, **CFG)
        print(f"=== {mode} seed={seed} 参数量 {cnt(model)} ===")
        results[(mode, seed)] = train(model, train_ids, val_ids,
                                      seed=seed, **RUN)

plt.figure(figsize=(7, 4.5))
for mode, color in [('standard', 'tab:gray'), ('attnres', 'tab:blue')]:
    for seed in SEEDS:
        h = results[(mode, seed)]
        plt.plot(h['step'], h['val'], color=color, alpha=0.45,
                 label=mode if seed == SEEDS[0] else None)
plt.xlabel('step'); plt.ylabel('val CE loss'); plt.grid(True); plt.legend()
plt.savefig('compare_val_loss.png', dpi=150)

for mode in ['standard', 'attnres']:                     # 汇报
    finals = [results[(mode, s)]['val'][-1] for s in SEEDS]
    m = sum(finals) / len(finals)
    sd = (sum((f - m) ** 2 for f in finals) / (len(finals) - 1)) ** 0.5
    print(f"{mode:9s} final val = {m:.4f} ± {sd:.4f}  (ppl ≈ {math.exp(m):.2f})")
delta = (sum(results[('standard', s)]['val'][-1] for s in SEEDS)
         - sum(results[('attnres', s)]['val'][-1] for s in SEEDS)) / len(SEEDS)
print(f"Δval (baseline − attnres) = {delta:+.4f}")
```

### 5.3 记录表模板

| 种子 | 基线 final val | AttnRes final val | Δ（基线−AttnRes） | 基线 ppl | AttnRes ppl |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 0 | | | | | |
| 1 | | | | | |
| 2 | | | | | |
| **均值 ± std** | | | | | |

另记：语料与字符数、V、两种模型参数量与相对差、训练步数与每步耗时比、块末端候选数（应为 9）。

### 5.4 预期现象与判读（定性预期，具体数值待本地验证）

1. **两条曲线都正常训练**：从 ≈ \(\ln V\) 起步、数百分内快速下降；若某模式 NaN 或停滞，回到 4.2/4.3 的三连检排障。
2. **AttnRes 的 val 曲线整体不高于基线**是 README L99「consistently outperforms across all compute budgets」在本规模的可期待方向；但字符级小模型的差距完全可能落在种子方差以内。
3. **若 \(\Delta\) 小于 std**：如实报告「本规模下无显著差异」。这不是失败——它说明该收益需要更大模型/数据才能显形，正好把问题交给 u3-l2 的多规模 scaling 实验；此时曲线图里两种颜色的带状重叠本身就是结论。
4. **若 \(\Delta\) 为负**（attnres 更差且超出 std）：先查实现再怀疑结论——最常见的是块边界层号起点弄错（候选数探针 (iii) 会暴露）、末端候选数不是 9、或忘记在 `train()` 内重设种子。
5. **每步耗时**：attnres 应与基线相近（u2-l3 估算 FLOPs 增量约 1% 量级）；若显著变慢，检查是否无意中保留了不必要的计算图或封存了过大的列表。

### 5.5 进阶（可选）：「1.25 倍计算」的迷你复现

README L99 的第二个论断是「Block AttnRes 匹配 1.25 倍计算量的基线」。迷你复现：基线额外训 25% 步数（3000 → 3750），比较 `attnres@3000` 与 `baseline@3750` 的 final val——若两者接近（甚至前者更低），就是这条 scaling 论断在本实验台上的微缩回声。注意 3000 步的 attnres 与 3750 步的基线已不是同预算比较，报告时要分开表述。效果待本地验证。

## 6. 本讲小结

- **最小训练脚本**是公共设施：字符语料 + 右移一位的目标 + 确定性评测（`evaluate` 按固定步长扫窗口），两种模型跑在同一设施上，差异只能来自结构；未训练损失 ≈ \(\ln V\) 是训练前的度量衡自检。
- **标准残差基线与 Block AttnRes 共用一个 `Block` 类**，`mode` 开关只改两件事：两个位点上 `h` 的来源（聚合输出 vs 残差流 partial）、以及是否封存块——这正是 [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)「drop-in」的代码形态，也让公平性由代码结构保证。
- **AttnRes 装配的五个要点**：双状态 `(blocks, partial)` 由模型外壳显式逐层搬运；每次前向 `blocks = []` 重新起算；第 0 层边界把（含位置的）词嵌入封存为 `blocks[0]`；末端用最后的 partial 接 `final_norm` + LM 头（模型级装配 README 未给出，变体待消融）；候选数每 k 层加一，L=16、block_size=4 时末端 9 个候选，对齐「~8 blocks」。
- **确定性检查先于训练**：参数量差恰为 \(4dL\)（约 0.13%，如实汇报而非补齐）、空 blocks 时 `block_attn_res` 退化为恒等、候选数轨迹符合闭式——三条全过再开训。
- **对比流程三件套**：`train()` 内部重设种子实现数据流配对；多种子报均值 ± std；\(\Delta\) 与 std 比较后再下结论，一次小规模实验不能证实也不能证伪 48B 规模的论文结论（L105）。
- 损失曲线是 README scaling 图（L95-L99、`assets/scaling_law.png`）的迷你版：本讲看「同预算下谁更低」，u3-l2 再看「多规模下趋势是否保持」。

## 7. 下一步学习建议

- **下一讲 u2-l5（训练动态）**：直接复用本讲的实验台——给两种模型注册 forward/backward hook，逐层记录隐藏状态范数与梯度范数，验证 [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123)「幅度有界、梯度更均匀」的定性结论。本讲 4.3 的候选数探针就是 hook 写法的预演。
- **向后衔接 u3-l1（复杂度分析）**：把本讲的 block_size 当旋钮扫一遍（k=1/2/4/8/16），实测显存与耗时候选数闭式 \(C(l)\) 的预测——O(Ld)→O(Nd) 从估算变实测。
- **向后衔接 u3-l2（scaling law）**：本讲单一规模的对比是那里多规模实验的原子操作；用 3-4 个递增的 (L, d) 重复本讲流程，拟合损失-计算量幂律，才能检验「1.25 倍计算等效应」。
- **回读论文**：本讲所有训练超参（优化器、学习率、调度、warmup、block_size 与层数的配比）都是示例自定，README 未给出——精读 `Attention_Residuals.pdf` 时逐项核对论文设置（待确认），尤其注意模型级输出装配（head 前是否再聚合一次）是否与 4.3 的最小方案一致。
- **动手巩固**：给实验台加一个生成函数（`model.eval()` 下逐字符采样），用肉眼对比两种模型学到的字符级风格；再试 4.3 练习 3 提到的「head 前聚合」变体，做成一张小消融表。
