# u3-l3 下游评测解读：Kimi Linear 48B 上的全面收益

## 1. 本讲目标

u3-l2 回答的是「同等计算预算下，AttnRes 的训练损失是否更低」（答案是：低，且 Block AttnRes 匹配 1.25 倍计算量的基线）。但损失是**分布内下一个 token 的平均惊讶度**，它低不代表模型「会做题」。论文证据链的第二层正是下游能力——README 用一张九行结果表给出：

> [README.md:L105-L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105-L119)
> `### Downstream Performance (Kimi Linear 48B / 3B activated, 1.4T tokens)` …… 九项基准全部加粗领先，结论句为 `AttnRes improves across the board, with the largest gains on multi-step reasoning (+7.5 on GPQA-Diamond) and code generation (+3.1 on HumanEval).`

本讲把这张表读透、把表背后的评测方法学到手、再把方法搬回自己的实验台。学完本讲，你应该能够：

1. **精确读表**：掌握绝对分差、相对分差、错误率降幅、符号一致性四种读法；能亲手从表里算出派生列，并指出 README 总结句与逐行数据之间的关系（包括一处需要仔细核对的地方）。
2. **分清九项基准的维度与形态**：知道 MMLU/CMMLU/C-Eval/GPQA 类是「选择似然」打分、HumanEval/MBPP 类是「生成 + 判定」打分；理解把评测搬回迷你实验台时，能对应的是**形式**而不是**难度**。
3. **读懂实验底座的规格**：理解 [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) 一行里「48B / 3B activated / 1.4T tokens」各自的含义（MoE、激活参数、预训练规模），以及为什么在一个注意力算子与 FFN 形态都已被替换掉的底座上验证残差改动，反而是对 [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)「drop-in」主张的强背书。
4. **动手搭评测**：为 u2-l4 的迷你实验台补一套下游评测脚本——完形填空选择题（MCQ）+ 代码补全两套题、两把打分尺，按「随机水平自检 → 配对训练 → 对照趋势 → 诚实判读」跑通 Standard vs Block AttnRes 的对比。

本讲是 u3-l2 的直接续篇：实验台、`train()`、配对种子、参数量手算全部来自 u2-l4；「mini 规模不能证实/证伪 48B 结论」的诚实边界纪律来自 u3-l2。本讲新增的只有「下游评测」这一层设施。

## 2. 前置知识

### 2.1 训练损失与下游能力：为什么损失曲线还不够

训练损失（验证损失也一样）度量的是模型对**下一个 token** 的平均预测质量，是对整个训练分布取平均；下游基准则是对**具体能力点**的抽样探测——解一道研究生级选择题、写一段通过单元测试的代码。两者的映射不是线性的：

- **损失差很小，下游差可能很大**：GPQA-Diamond 基线只有 36.9 分，说明这类题处在模型能力的「边缘区」——损失曲线少许下移，边缘区的题就可能成批从错变对。绝对分差 +7.5、相对 +20.3%，正是这种放大效应的体现。
- **损失差很大，下游差也可能不明显**：如果改进都体现在高频词的预测质量上，基准考察的稀缺能力可能纹丝不动。

所以「残差连接的改动有没有用」需要两层证据：**损失层**（u3-l2：等预算下曲线整体下移，约 1.25 倍计算等效应）+ **能力层**（本讲：九项基准全面为正）。README 恰好按这个顺序组织 Results 部分——先 [L95-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L95-L99) 的 Scaling Laws，再 [L105-L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105-L119) 的 Downstream Performance。

### 2.2 选择题的对数似然打分

MMLU、CMMLU、C-Eval、GPQA-Diamond 这类基准的通行形态是**多选一**（MMLU 为 4 选 1，随机水平约 25 分）。语言模型做选择题不打草稿、不输出字母，而是给每个选项**打分**：把「题干 + 选项文本」当作模型的输入，计算选项部分每个字符（或 token）的对数概率，再取平均：

\[
\text{score}(o) \;=\; \frac{1}{|o|}\sum_{t=1}^{|o|} \log P\bigl(o_t \,\big|\, \text{ctx},\; o_{<t}\bigr)
\]

预测答案就是得分最高的选项，准确率为

\[
\text{acc} \;=\; \frac{1}{|Q|}\sum_{q \in Q} \mathbb{1}\Bigl[\,\arg\max_j \text{score}(o_j^{(q)}) = y_q\,\Bigr]
\]

其中 \(y_q\) 是金标选项。两个工程细节必须注意：

- **长度归一化**：选项长短不一时，直接用 logprob **总和**会系统性惩罚长选项（多乘若干个小于 1 的概率）。除以长度 \(|o|\) 后比较的是「单位长度的续写合理性」——这正是评测框架（如 lm-evaluation-harness）中 `acc_norm` 的口径。我们的字符级模型天然用「每字符」归一化。
- **打分只看相对排序**：绝对数值不可跨题比较，只有同一题内各选项的**相对高低**有意义。

### 2.3 生成式评测与 pass@1

HumanEval、MBPP 是代码生成基准：给函数签名与文档字符串，让模型**自由生成**补全，再跑单元测试判定功能正确性。通行指标是 pass@1：从模型采样的 \(n\) 个补全中有 \(c\) 个通过全部测试，单次使用（k=1）时的期望通过率即 \(c/n\)。更一般地，pass@k 有无偏估计（Codex 论文给出的形式）：

\[
\text{pass}@k \;=\; 1 - \frac{\binom{n-c}{k}}{\binom{n}{k}}
\]

它与选择题打分的本质区别在于**判定方式**：选择似然只比较若干给定候选的相对合理性（形式判定），单元测试检查生成代码的行为是否符合规格（语义判定）。MATH、TriviaQA 介于两者之间——自由生成答案，再与金标做字符串精确匹配（生成式，但判定是表面的）。

### 2.4 MoE、线性注意力与「激活参数」：读懂 L105 那行规格

[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) 的标题行 `Kimi Linear 48B / 3B activated, 1.4T tokens` 压缩了三个规格，先把通用背景补齐（这些是领域通识，README 本身未展开，Kimi Linear 的具体结构以论文与官方技术报告为准，待确认）：

- **MoE（Mixture-of-Experts，混合专家）**：把 Transformer 的 FFN（或注意力）替换为多份「专家」，每个 token 由一个路由器只激活其中少数几个。于是「总参数 48B」与「每 token 实际参与计算的参数 3B」是两回事——激活率约 \(3/48 = 6.25\%\)。模型容量随总参数增长，计算成本随激活参数增长，这是大模型扩容的主流手段之一。
- **线性注意力（Linear Attention）**：标准 softmax 注意力要保留全部历史 K/V，序列长度为 \(T\) 时计算与显存为 \(O(T^2)\)；线性注意力家族把历史压缩进一个**固定尺寸的循环状态**，复杂度随序列线性增长，适合超长上下文。模型名里的「Linear」即指此。
- **1.4T tokens**：预训练见过的 token 总量。u3-l2 已按 \(C \approx 6ND\) 估算过：以 3B 激活参数计，这份训练约 \(2.5\times 10^{22}\) FLOPs，约 290 PFLOP/s-day，比我们的迷你实验台大 6～7 个数量级。

### 2.5 与前几讲的衔接：已有资产与本讲缺口

u2-l4 交付的实验台资产：`build_corpus` / `get_batch` / `evaluate`、带 `mode` 开关的 `MiniGPT`、内部重设种子的 `train()`、参数量手算式。u3-l2 又交付了计算量记账与「趋势复现而非数字复现」的纪律。但实验台至今只有一把尺子——**验证损失**。本讲要补的缺口是三件：两套**建题器**（MCQ、代码补全，语料取自训练时未见过的 holdout 段）、两把**打分尺**（选项平均 logprob、贪心补全精确匹配）、一套**随机水平自检与趋势对照**的判读流程。

## 3. 本讲源码地图

本仓库仍是那个只有 6 个文件的论文发布仓库（u1-l1 已确认）。本讲的策略与前两讲一致：README 的表格与文字提供**结论与规格依据**，评测设施全部为本讲义编写的「示例代码」，运行在 u2-l4 已搭好的实验台之上。

| 位置 | 内容 | 本讲用途 |
|:---|:---|:---|
| [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) | 底座规格：Kimi Linear 48B / 3B activated / 1.4T tokens | 4.1：证据规模的界定；4.3：规格逐项解读 |
| [README.md:L107-L117](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L107-L117) | 九行结果表（三类别 × Baseline/AttnRes） | 4.1：四种读法的原始数据；4.2：类别维度划分依据 |
| [README.md:L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L119) | 结论句：across the board；最大收益维度 | 4.1：与逐行数据互验；4.2：维度命名的出处 |
| [README.md:L95-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L95-L99) | Scaling Laws 小节 | 4.3：两层证据链的上一层（u3-l2 已精读） |
| [README.md:L71-L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71-L87) | 伪代码：attn_res 位点在子层之外，子层是黑盒 | 4.3：残差改动与注意力/FFN 形态正交的证据 |
| [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) | Block AttnRes：~8 块、marginal overhead | 4.3：48B 规模下开销费米估算的依据 |
| [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123) | Training Dynamics 小节 | 4.3：证据链第三层（u2-l5 已实测） |
| [README.md:L134-L141](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L134-L141) | 引用块（Kimi Team 等） | 4.3：结果出自何方团队的溯源 |
| [README.md:L15-L16](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L15-L16) | 论文 PDF 与 arXiv 链接 | 4.3：评测协议细节以论文为准的入口 |
| `Attention-Residuals-tutorial/u2-l4-minimal-testbed-training.md` | u2-l4 实验台（模型/训练/评测代码） | 全讲：直接复用的代码基座 |
| `Attention-Residuals-tutorial/u3-l2-scaling-law-experiments.md` | u3-l2 损失层证据与诚实边界纪律 | 全讲：证据链上一层与判读纪律 |

## 4. 核心概念与源码讲解

三个最小模块按「先把表读准、再给表配方法、最后看懂做实验的底座」的顺序展开：

1. **4.1 基准结果解读**——九行数字的四种读法与总结句互验；
2. **4.2 评测维度分类**——九项基准各测什么、用什么形态打分，并落成可运行的建题/打分代码；
3. **4.3 大模型背景（Kimi Linear）**——底座规格逐项解读、残差改动的正交性论证、48B 规模下的开销费米估算。

### 4.1 基准结果解读：九行数字的精确读法

#### 4.1.1 概念说明

先把原始表原样摆出来（这是本模块唯一的「源码」，值得逐行精读）：

> [README.md:L107-L117](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L107-L117)
> ```text
> | Category | Benchmark | Baseline | AttnRes |
> |:---|:---|:---:|:---:|
> | General | MMLU | 73.5 | **74.6** |
> | | GPQA-Diamond | 36.9 | **44.4** |
> | | BBH | 76.3 | **78.0** |
> | | TriviaQA | 69.9 | **71.8** |
> | Math & Code | Math | 53.5 | **57.1** |
> | | HumanEval | 59.1 | **62.2** |
> | | MBPP | 72.0 | **73.9** |
> | Chinese | CMMLU | 82.0 | **82.9** |
> | | C-Eval | 79.6 | **82.5** |
> ```

读这张表有四个层次，一层比一层信息量大：

**读法一：绝对分差 \(\Delta = \text{AttnRes} - \text{Baseline}\)。** 最直接的「涨了多少分」。九行全为正，从 +0.9（CMMLU）到 +7.5（GPQA-Diamond）。

**读法二：相对分差 \(\Delta_{\text{rel}} = \Delta / \text{Baseline}\)。** 同样 +2 的提升，发生在 40 分的基准和 80 分的基准上含义完全不同。GPQA 的 \(+7.5\) 换算成相对提升是 \(7.5/36.9 \approx +20.3\%\)，而 CMMLU 的 \(+0.9\) 只有约 \(+1.1\%\)——相对口径下头部尾部的差距比绝对口径更悬殊。

**读法三：错误率降幅 \(\Delta_{\text{err}} = \Delta / (100 - \text{Baseline})\)。** 基准分数接近 100 时会出现「天花板效应」：CMMLU 基线已 82.0，剩下的错误只有 18 分，\(+0.9\) 相当于把错误砍掉 \(0.9/18 = 5\%\)；GPQA 基线 36.9，错误有 63.1 分，\(+7.5\) 相当于错误砍掉 \(7.5/63.1 \approx 11.9\%\)。错误率视角回答的是「离满分还差的那部分，被收复了多少」。

**读法四：符号一致性。** 下游指标噪声大，单项涨跌都可能来自评测波动；但**九项全部同向为正**是一回事，五正四负是另一回事。若改动完全无效且各项独立，每项涨跌等概率，九项全胜的概率是 \((1/2)^9 = 1/512 \approx 0.002\)——符号检验（sign test）的直觉：这种同向性不太像偶然（注意其独立性假设的局限，见 4.1.5 练习 2）。

最后是与总结句互验的一处**仔细核对点**：

> [README.md:L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L119)
> `AttnRes improves across the board, with the largest gains on multi-step reasoning (+7.5 on GPQA-Diamond) and code generation (+3.1 on HumanEval).`

按绝对分差排序，前三名依次是 GPQA-Diamond（+7.5）、**Math（+3.6）**、HumanEval（+3.1）——MATH 的分差其实高于 HumanEval。总结句以「多步推理」与「代码生成」两个**维度各取一个代表基准**行文（MATH 在表里归入 Math & Code 类，本身就横跨两个维度）；严格引用数据时应以逐行为准，机制层面的解释（为什么多步推理收益最大）README 未给出，论文中可能另有分析（待确认）。

#### 4.1.2 核心流程

```text
读表流程:
复制 L107-L117 的 9 行 (基准, 类别, baseline, attnres)
→ 派生三列:  Δ绝对   = attnres − baseline
             Δ相对   = Δ绝对 / baseline
             错误率降幅 = Δ绝对 / (100 − baseline)
→ 按各列排序, 找头部/尾部
→ 与 L119 总结句互验 (总结句挑的是维度代表, 排序以数据为准)
→ 符号检验: 9 项全为正的同向概率 (1/2)^9 ≈ 0.002
```

派生列汇总如下（由上表数值直接计算，读者可用 4.1.4 的脚本复核）：

| 基准 | 类别 | Baseline | AttnRes | Δ绝对 | Δ相对 | 错误率降幅 |
|:---|:---|:---:|:---:|:---:|:---:|:---:|
| GPQA-Diamond | General | 36.9 | 44.4 | **+7.5** | **+20.3%** | +11.9% |
| Math | Math & Code | 53.5 | 57.1 | +3.6 | +6.7% | +7.7% |
| HumanEval | Math & Code | 59.1 | 62.2 | +3.1 | +5.2% | +7.6% |
| C-Eval | Chinese | 79.6 | 82.5 | +2.9 | +3.6% | +14.2% |
| TriviaQA | General | 69.9 | 71.8 | +1.9 | +2.7% | +6.3% |
| MBPP | Math & Code | 72.0 | 73.9 | +1.9 | +2.6% | +6.8% |
| BBH | General | 76.3 | 78.0 | +1.7 | +2.2% | +7.2% |
| MMLU | General | 73.5 | 74.6 | +1.1 | +1.5% | +4.2% |
| CMMLU | Chinese | 82.0 | 82.9 | +0.9 | +1.1% | +5.0% |

几个一眼可见的结构：绝对分差前三（GPQA、MATH、HumanEval）都是**多步推理/生成密集型**任务；知识记忆型选择题（MMLU、CMMLU）分差最小；有趣的是 C-Eval 的错误率降幅（+14.2%）仅次于 GPQA——它的基线较高（79.6），+2.9 的绝对分砍掉的错误比例反而很大。单一口径都会误导，多口径并读才完整。

#### 4.1.3 源码精读

**表的规格行——所有数字的适用范围都由这一行界定**：

> [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)
> `### Downstream Performance (Kimi Linear 48B / 3B activated, 1.4T tokens)`

九行数字只在「Kimi Linear 底座、48B 总参数、3B 激活、1.4T tokens 预训练」这一规格下成立。README 未说明 Baseline 与 AttnRes 两臂是否严格同数据、同超参、是否多种子平均（待确认，以论文实验节为准）——引用这批数字时应连规格一起引用。

**类别列——README 自己给出的维度划分**（[L107-L117](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L107-L117) 第一列）：General 四项（MMLU、GPQA-Diamond、BBH、TriviaQA）、Math & Code 三项（Math、HumanEval、MBPP）、Chinese 两项（CMMLU、C-Eval）。注意这个划分混合了**能力维度**（推理/代码/知识）与**语言维度**（中文单列），4.2 会在这份官方划分的基础上补一层「打分形态」的划分。

**结论句的两个关键词**：`across the board`（全面，对应符号一致性）与 `multi-step reasoning` / `code generation`（收益最大的两个维度）——前者是 4.1 读法四，后者是 4.2 维度分类的官方命名出处。

#### 4.1.4 代码实践：把派生列亲手算一遍

1. **实践目标**：用脚本从 README 表格的原始数字算出三种派生列与符号检验概率，确认 4.1.2 汇总表的每个数字（纯数据计算，无需 GPU，也无需训练任何模型）。
2. **操作步骤**：

```python
# 示例代码: README 结果表的二次分析 (纯数据计算)
rows = [  # (基准, 类别, baseline, attnres)  ← 逐行抄自 README L109-L117
    ("MMLU",         "General",     73.5, 74.6),
    ("GPQA-Diamond", "General",     36.9, 44.4),
    ("BBH",          "General",     76.3, 78.0),
    ("TriviaQA",     "General",     69.9, 71.8),
    ("Math",         "Math & Code", 53.5, 57.1),
    ("HumanEval",    "Math & Code", 59.1, 62.2),
    ("MBPP",         "Math & Code", 72.0, 73.9),
    ("CMMLU",        "Chinese",     82.0, 82.9),
    ("C-Eval",       "Chinese",     79.6, 82.5),
]

stats = []
for name, cat, b, a in rows:
    d = a - b
    stats.append((name, d, d / b, d / (100 - b)))

stats.sort(key=lambda x: -x[1])                       # 按绝对分差排序
print(f"{'基准':14s}{'Δ绝对':>8s}{'Δ相对':>9s}{'错误率降幅':>10s}")
for name, d, rel, err in stats:
    print(f"{name:14s}{d:>8.1f}{100*rel:>8.1f}%{100*err:>9.1f}%")

deltas = [s[1] for s in stats]
print(f"\n均值 {sum(deltas)/9:.2f}  中位数 {sorted(deltas)[4]:.1f}"
      f"  最大 {max(deltas):.1f}  最小 {min(deltas):.1f}")
wins = sum(1 for _, d, _, _ in stats if d > 0)
print(f"同向为正 {wins}/9 项; 若改动无效且各项独立, "
      f"9/9 同向概率 = (1/2)^9 = {0.5**9:.4f}")
```

3. **需要观察的现象**：排序后第一行是 GPQA-Diamond（+7.5，相对 +20.3%）；均值 2.73、中位数 1.9；最后一行打印 `9/9 同向概率 = 0.0020`。
4. **预期结果**：以上数字全部由表内数值的四则运算得出，是确定性的——应与 4.1.2 汇总表逐格一致（C-Eval 错误率降幅应显示 14.2%，是第二大的错误率降幅）。若不一致，先检查是否抄错了某行的 baseline。
5. 本实践不依赖任何训练或随机性，可直接验证；脚本中的 `rows` 是手工誊抄的，若仓库表格更新需同步（本表以 HEAD 85e2231 为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么要在绝对分差之外补充「错误率降幅」这一口径？用 MMLU 与 GPQA-Diamond 说明。

> **答案**：绝对分差忽略基线的起点。MMLU 基线 73.5，错误 26.5 分，\(+1.1\) 只收复错误的 \(1.1/26.5 \approx 4.2\%\)；GPQA 基线 36.9，错误 63.1 分，\(+7.5\) 收复 \(11.9\%\)。错误率口径把「离满分还有多远」考虑进来，避免了高分基准上同等分数被低估、也便于横向比较收复比例。

**练习 2**：符号检验的 \(0.002\) 依赖哪些假设？在这个场景里哪里可能被违反？

> **答案**：依赖 (i) 零效应下每项涨跌等概率、(ii) 九项相互独立。可能被违反之处：九项基准共享底层能力因子（同一模型的能力变化会同时影响多项，并非独立抛硬币）；两臂可能共享训练数据与超参，评测噪声相关；README 未说明结果是否为多种子平均（待确认）。因此 \(0.002\) 只能当作同向性「定性上很强」的参考，不是严格的统计检验 p 值。

**练习 3**：README 总结句说最大收益在 GPQA-Diamond 与 HumanEval；按绝对分差，前三名依次是什么？这说明引用结果时应注意什么？

> **答案**：GPQA-Diamond（+7.5）、Math（+3.6）、HumanEval（+3.1）。说明总结句是**按维度挑代表**的行文（MATH 与 HumanEval 同属 Math & Code 类，且都是多步生成密集任务），不是严格的逐行排序声明；引用时应区分「README 的叙述」与「表格的原始数据」，以后者为准，并保留一位小数原样引用。

### 4.2 评测维度分类：九项基准测什么、怎么打分

#### 4.2.1 概念说明

4.1 用「数值口径」读表，本模块换用「能力维度 × 打分形态」读表。README 的 Category 列给出官方维度划分（General / Math & Code / Chinese），但在动手复刻评测时，更有用的划分是**打分形态**——它决定了评测脚本的写法：

| 维度 | 基准 | 通行打分形态 | 随机水平 | Δ绝对（L109-L117） |
|:---|:---|:---|:---:|:---:|
| 学术知识（英文） | MMLU | 4 选 1，选项似然 | ≈25 | +1.1 |
| 研究生级多步推理 | GPQA-Diamond | 4 选 1，选项似然 | ≈25 | **+7.5** |
| 综合推理 | BBH | 多选/精确匹配 | 视任务而定 | +1.7 |
| 事实回忆 | TriviaQA | 生成 + 精确匹配 | ≈0 | +1.9 |
| 数学多步推理 | MATH | 生成 + 答案匹配 | ≈0 | +3.6 |
| 代码生成 | HumanEval | 生成 + 单元测试 pass@1 | ≈0 | +3.1 |
| 代码生成 | MBPP | 生成 + 单元测试 pass@1 | ≈0 | +1.9 |
| 学术知识（中文） | CMMLU | 4 选 1，选项似然 | ≈25 | +0.9 |
| 综合知识（中文） | C-Eval | 4 选 1，选项似然 | ≈25 | +2.9 |

说明两点。其一，「通行打分形态」是这些基准在评测社区（如 lm-evaluation-harness、各自论文）的常规口径，属于领域通识；README **没有**给出每项基准的评测协议——few-shot 数量、打分口径、温度、是否多种子平均均未说明（以论文为准，待确认）。其二，把分差与形态对照能看出一个模式：**生成式/多步推理任务（GPQA、MATH、HumanEval）的分差整体大于选择似然式的知识问答（MMLU、CMMLU）**。这与 [L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L119) 的叙述一致；机制上可以有一个合理猜想——AttnRes 让每层选择性回看历史表示（u1-l3），长链条的多步推理更依赖深层对早期信息的精确复用，稀释问题（u1-l2）在那里最伤——但这只是猜想，README 未论证（待确认，论文可能有分析）。

把评测搬回自己的实验台时，要时刻记住一条纪律：**能复刻的是形态，不是难度**。我们的字符级小模型做不了研究生级考题，但「选项似然打分」与「生成 + 匹配判定」两套机制可以 1:1 复刻——这正是本模块代码实践要做的。

#### 4.2.2 核心流程

选择题打分管线（MMLU/CMMLU/C-Eval/GPQA 类）：

```text
建题: 从 holdout 语料挖词 → 1 个金标 + 3 个干扰项 → 打乱顺序
打分: 对每个选项 oj:
        score(oj) = oj 作为「上下文续写」的平均每字符 logprob   (2.2 的公式)
预测: pred = argmax_j score(oj)
计分: acc += 1[pred == 金标]     随机水平 = 1/选项数 = 0.25
```

生成式打分管线（HumanEval/MBPP 类的迷你形态）：

```text
建题: 从 holdout 代码取「上文 + 下一行」
生成: 贪心逐字符生成, 直到换行 (或上限)
判定: 与金标行 strip 后精确匹配 → 命中 (pass@1 的表面匹配版)
辅助: 目标行的平均每字符 logprob (连续口径, 比命中率更平滑)
```

两条管线共享同一个原子操作——「某串文本作为上下文续写的平均 logprob」，它就是 4.2.4 代码里的 `option_logprob`。

#### 4.2.3 源码精读

**维度划分的官方出处**——表格第一列（三类别、组内首行标注）：

> [README.md:L109-L117](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L109-L117)
> ```text
> | General | MMLU | 73.5 | **74.6** |
> | | GPQA-Diamond | 36.9 | **44.4** |
> ...
> | Math & Code | Math | 53.5 | **57.1** |
> | | HumanEval | 59.1 | **62.2** |
> ...
> | Chinese | CMMLU | 82.0 | **82.9** |
> | | C-Eval | 79.6 | **82.5** |
> ```

四项 General、三项 Math & Code、两项 Chinese。这个划分提示了评测设计的一个原则：**覆盖面比单项深度重要**——九项横跨英文/中文、知识/推理/代码、选择/生成，才撑得起 `across the board` 的结论。

**「多步推理」与「代码生成」两个收益维度的命名出处**：

> [README.md:L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L119)
> `...the largest gains on multi-step reasoning (+7.5 on GPQA-Diamond) and code generation (+3.1 on HumanEval).`

**评测协议的空缺**：README 全文没有说明九项基准用什么 harness、几 shot、何种打分（这正是本模块「通行形态」列必须标注待确认的原因）。所有表格数字的协议细节以 [README.md:L15-L16](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L15-L16) 指向的 `Attention_Residuals.pdf` 为准。**引用论文数字时永远连规格一起引用**——这也是 4.2.4 实践里「随机水平自检」要写进脚本的原因：自己的评测也要把自己的口径显式化。

#### 4.2.4 代码实践：两套建题器 + 两把打分尺 + 随机水平自检

1. **实践目标**：在 u2-l4 实验台上新增下游评测的全部设施——混合语料与 holdout 切分、MCQ 建题、代码补全建题、选项似然打分、贪心生成判定——并用**未训练模型**验证度量衡：MCQ 准确率应接近随机水平 0.25，代码命中率应接近 0。
2. **操作步骤**：

```python
# 示例代码: 下游评测的建题与打分设施 (复用 minitest.py 中的 MiniGPT)
import re, random, torch
import torch.nn.functional as F

def build_mixed_corpus(text_path, code_path, hold_frac=0.05):
    """混合语料: 文本+代码拼为训练语料, 各自尾部切出互不重叠的 holdout 供建题。
    字符表只从训练段构建 → holdout 里的生字符在建题时被显式过滤 (避免泄漏)。"""
    def load(path):
        s = open(path, encoding='utf-8').read()
        n = int(len(s) * (1 - hold_frac))          # 前 (1-hold) 训练, 尾部 holdout
        return s[:n], s[n:]
    tr_t, ho_t = load(text_path)
    tr_c, ho_c = load(code_path)
    chars = sorted(set(tr_t + tr_c))
    stoi = {c: i for i, c in enumerate(chars)}
    itos = {i: c for c, i in stoi.items()}
    ids = torch.tensor([stoi[c] for c in tr_t + '\n' + tr_c], dtype=torch.long)
    n_train = int(len(ids) * 0.9)
    return ids[:n_train], ids[n_train:], stoi, itos, ho_t, ho_c, tr_t

def build_mcq(hold_text, train_text, stoi, n_q=40, ctx_len=120, seed=7):
    """完形填空选择题: 从 holdout 挖一个完整词, 干扰项取自训练语料词表。"""
    rng = random.Random(seed)
    pool = sorted({w for w in re.findall(r"[a-z]{4,10}", train_text)
                   if all(c in stoi for c in w)})
    qs = []
    hits = sorted(re.finditer(r"[a-z]{4,10}", hold_text),
                  key=lambda _: rng.random())       # 随机洗牌遍历
    for m in hits:
        if len(qs) >= n_q:
            break
        gold = m.group(0)
        ctx = hold_text[max(0, m.start() - ctx_len):m.start()]
        if len(ctx) < ctx_len // 2:                 # 上下文太短, 题目信息量不足
            continue
        if any(c not in stoi for c in ctx + gold):  # OOV 过滤
            continue
        opts = [gold] + rng.sample([w for w in pool if w != gold], 3)
        rng.shuffle(opts)
        qs.append(dict(ctx=ctx, options=opts, label=opts.index(gold)))
    return qs

def build_codeq(hold_code, stoi, n_q=40, ctx_len=200, seed=11):
    """代码补全题: 上文为提示 (以换行结尾), 目标为下一行原样保留 (含缩进)。"""
    rng = random.Random(seed)
    lines = hold_code.split('\n')
    qs = []
    for i in sorted(range(len(lines) - 1), key=lambda _: rng.random()):
        if len(qs) >= n_q:
            break
        raw = lines[i + 1]                          # 目标行: 保留缩进, 供打分
        if not (8 <= len(raw.strip()) <= 60):       # 太短无区分度, 太长难命中
            continue
        ctx = ('\n'.join(lines[max(0, i - 40):i + 1]) + '\n')[-ctx_len:]
        if any(c not in stoi for c in ctx + raw):
            continue
        qs.append(dict(ctx=ctx, target=raw))
    return qs

@torch.no_grad()
def option_logprob(model, ctx_ids, opt_ids):
    """选项串紧跟上下文的平均每字符 logprob —— MCQ 打分的原子操作。
    长度归一化: 对应评测框架 acc_norm 的口径 (2.2 的公式)。"""
    seq = torch.cat([ctx_ids, opt_ids]).unsqueeze(0)     # [1, T]
    logits, _ = model(seq)                               # [1, T, V]
    logp = F.log_softmax(logits[0], dim=-1)              # logp[t] 预测 seq[t+1]
    n = len(opt_ids)
    tgt = seq[0, -n:]                                    # 选项的 n 个字符
    char_lp = logp[-n - 1:-1].gather(1, tgt.unsqueeze(1)).squeeze(1)
    return char_lp.mean().item()

@torch.no_grad()
def eval_mcq(model, questions, stoi):
    """MCQ 准确率: 每题取平均 logprob 最高的选项, 与金标比对。"""
    model.eval()
    hit = 0
    for q in questions:
        ctx = torch.tensor([stoi[c] for c in q['ctx']], dtype=torch.long)
        scores = [option_logprob(model, ctx,
                  torch.tensor([stoi[c] for c in o], dtype=torch.long))
                  for o in q['options']]
        hit += int(max(range(len(scores)), key=scores.__getitem__) == q['label'])
    return hit / len(questions)

@torch.no_grad()
def greedy_complete(model, ctx_ids, itos, max_new=80):
    """贪心逐字符生成直到换行 (代码补全的迷你生成器)。"""
    model.eval()
    ids = ctx_ids.clone()
    for _ in range(max_new):
        window = ids[:, -model.wpe.num_embeddings:]  # 超过 t_max 时滑窗截断
        logits, _ = model(window)
        nxt = int(logits[0, -1].argmax())
        ids = torch.cat([ids, torch.tensor([[nxt]])], dim=1)
        if itos[nxt] == '\n':
            break
    return ''.join(itos[int(i)] for i in ids[0, len(ctx_ids[0]):])

@torch.no_grad()
def eval_code(model, questions, stoi, itos):
    """两个口径: 贪心补全命中率 (pass@1 的表面匹配版) + 目标行平均 logprob。"""
    model.eval()
    hit, lps = 0, []
    for q in questions:
        ctx = torch.tensor([stoi[c] for c in q['ctx']],
                           dtype=torch.long).unsqueeze(0)
        gen = greedy_complete(model, ctx, itos)
        hit += int(gen.strip() == q['target'].strip())
        tgt = torch.tensor([stoi[c] for c in q['target']], dtype=torch.long)
        lps.append(option_logprob(model, ctx[0], tgt))
    return hit / len(questions), sum(lps) / len(lps)

# ---- 度量衡自检: 未训练模型应落在随机水平上 ----
train_ids, val_ids, stoi, itos, ho_t, ho_c, tr_t = build_mixed_corpus(
    'text.txt', 'code.txt')     # 如 tiny shakespeare 与一批本地 .py 合并的纯文本
V = len(stoi)
MCQ, CODEQ = build_mcq(ho_t, tr_t, stoi), build_codeq(ho_c, stoi)
print(f"词表 {V}, 建成 MCQ {len(MCQ)} 道, 代码补全 {len(CODEQ)} 道")

torch.manual_seed(0)
raw = MiniGPT(V, d=256, n_head=8, n_layer=16, block_size=4, mode='attnres')
acc0 = eval_mcq(raw, MCQ, stoi)
hit0, lp0 = eval_code(raw, CODEQ, stoi, itos)
print(f"未训练: MCQ acc = {acc0:.3f} (随机水平 0.25), "
      f"code hit = {hit0:.3f} (随机水平 ≈0), code lp = {lp0:.3f}")
```

3. **需要观察的现象**：建题打印两套题量（各 40 道左右；若语料里小写英文单词太少，MCQ 可能少于 40，属正常）；未训练模型的 MCQ 准确率在 0.25 附近（40 题的二项噪声约 \(\sqrt{0.25\times 0.75/40} \approx 0.07\)，落在 0.15～0.35 都算正常）；代码命中率接近 0；平均 logprob 是一个不大的负数。
4. **预期结果**：MCQ ≈ 0.25 是「近均匀分布下四选一等概率」的解析性质（u2-l4 的 `ln V` 自检在评测侧的对应物）；命中率 ≈0 是因为未训练模型的最可能字符几乎不可能拼出目标行。若 MCQ 显著偏离 0.25（如超过 0.4），优先检查：金标是否总是排在选项固定位置（忘了 `shuffle`）、或打分没有做长度归一。具体数值待本地验证。
5. 代码中 `text.txt` / `code.txt` 需自备（各几百 KB 即可，如 tiny shakespeare 与若干 Python 源文件合并），与 u2-l4「任选本地语料」的约定一致；两份文件在训练前就切出尾部 holdout，保证建题用的文本从未参与训练。

#### 4.2.5 小练习与答案

**练习 1**：MCQ 打分为什么用「平均每字符 logprob」而不是 logprob 总和？

> **答案**：选项长度不等时，logprob 总和等于把若干个小于 1 的概率连乘，选项每多一个字符就多乘一项——长选项被系统性压低分数，模型会因此偏爱短选项，与语义无关。除以长度后比较的是「单位长度的续写合理性」，对应评测框架的 `acc_norm` 口径；字符级模型里归一化单位自然就是字符。

**练习 2**：HumanEval 的 pass@1 与我们的「贪心补全命中率」有何本质差别？

> **答案**：判定标准不同。pass@1 让模型自由生成完整函数体，再跑**单元测试**判定功能正确性（语义层面：代码行为对不对）；命中率是贪心生成一行后与金标做**字符串精确匹配**（表面层面：写得像不像）。所以我们的口径只能测「表面补全能力」，与 HumanEval 对应的是评测**形式**（生成 + 判定），不是**难度**；报告时应明确这一差距。

**练习 3**：若 MCQ 的干扰项恰好也是上下文中的合理续写词，题目会怎样？如何缓解？

> **答案**：题目退化为「多答案皆可」，模型选谁都算对或算错，区分度下降，整套评测向随机水平退化。缓解办法：用词频或共现统计过滤干扰项（避免高频搭配词）、要求金标在上下文中有较强搭配（如人名、地名、固定短语）、或人工抽查 20 题确认只有一个合理答案。建题质量决定评测的上限——这也是把题量、随机水平一起写进报告的原因。

### 4.3 大模型背景（Kimi Linear）：规格、正交性与边际开销

#### 4.3.1 概念说明

这批下游数字的分量，取决于「在什么底座上做出来」。[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) 的规格行说了三件事，逐项展开（2.4 已补背景通识）：

**其一，规模。** 48B 总参数、1.4T tokens 是工业级预训练的量级。u3-l2 算过：以 3B 激活参数与 1.4T tokens 计，训练计算量约 \(2.5\times 10^{22}\) FLOPs（约 290 PFLOP/s-day），比我们的迷你实验台大 6～7 个数量级。残差连接是 2015 年就有了的「老结构」，改它的收益假说必须在足够大的规模上检验——很多小模型上的技巧到了这个规模会消失，而这张表说明 AttnRes 没有。

**其二，底座不是「标准 Transformer」。** 这是本模块最关键的一点。Kimi Linear 的两个关键词——MoE 与线性注意力——意味着这个模型的 FFN 与注意力算子都已经被替换掉了：不是 dense FFN 而是稀疏激活的专家；不是 softmax 注意力而是固定状态压缩的线性注意力（结构细节以论文与官方技术报告为准，待确认）。而 AttnRes 改的是**第三样东西**：层与层之间的残差接线。看伪代码就清楚三者的边界：

> [README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)
> `h = block_attn_res(blocks, partial_block, self.attn_res_proj, self.attn_res_norm)`
>
> [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)
> `attn_out = self.attn(self.attn_norm(h))`

`block_attn_res` 的输出 `h` 喂给 `self.attn`——对残差机制而言，`self.attn` 内部是 softmax 还是线性注意力、`self.mlp` 是 dense 还是 MoE，**完全不可见**（黑盒）。反过来，子层也算不出 `h` 是怎么聚合来的。这就是**正交性**：残差接线、注意力算子、FFN 形态是三个独立的替换维度。在一个已经替换了后两者的 SOTA 级底座上，替换第一项仍然全面收益——说明 AttnRes 的效果**不依赖**「vanilla softmax 注意力 + dense FFN」这个经典组合。这对二次开发（u3-l5）是最强的背书：把它移植进自己的模型时，不必担心自己的注意力变体或 MoE 结构与它冲突。

**其三，公平性口径。** README 未说明 Baseline 与 AttnRes 两臂是否严格同数据、同超参、同训练步数（待确认，以论文为准）。这类对照实验的通行做法是除残差接线外全同（u2-l4 在迷你台上正是这么做的：`mode` 开关只改两个位点的 `h` 来源），引用数字时把这个口径说明白。

#### 4.3.2 核心流程

至此，README Results 部分的三节构成一条完整证据链，本讲是最后一环：

```text
第一层 损失 (u3-l2, L95-L99):  等计算预算下损失更低, ~1.25× 计算等效应
        ↓  损失差如何兑现成能力?
第二层 下游 (本讲, L105-L119): 9 项基准全面为正, 多步推理/代码收益最大
        ↓  为什么这个改动可行且便宜?
第三层 机制 (u2-l5, L121-L123): 幅度有界、梯度均匀 —— 改动有效的微观解释
        ↓  为什么能搬到任意模型?
正交性 (L71/L84 vs L80/L87):   残差接线独立于注意力算子与 FFN 形态
边际开销 (L47):                ~8 块即恢复大部分收益, marginal overhead
```

#### 4.3.3 源码精读

**规格行（唯一出处）**：

> [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)
> `### Downstream Performance (Kimi Linear 48B / 3B activated, 1.4T tokens)`

README 全文对 Kimi Linear 的描述只有这一行；MoE 与线性注意力的具体结构（专家数、路由方式、状态尺寸）在仓库中均无，以论文与其官方技术报告为准（待确认）。

**正交性的伪代码证据**——两个 attn_res 位点都发生在子层调用**之外**：

> [README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84)
> `h = block_attn_res(blocks, partial_block, self.mlp_res_proj, self.mlp_res_norm)`
>
> [README.md:L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87)
> `mlp_out = self.mlp(self.mlp_norm(h))`

位点 2（L84）产出的 `h` 同样只是喂给 `self.mlp` 的黑盒输入。u2-l4 的 `mode` 开关实验把这个结构事实变成了代码事实：换残差方式时注意力与 MLP 的模块代码**一行不改**。

**边际开销的出处与费米估算依据**：

> [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)
> `With ~8 blocks, it recovers most of Full AttnRes's gains while serving as a practical drop-in replacement with marginal overhead.`

u2-l3 已算过参数开销是每层 \(4d\)（两个位点 × \(2d\)）；u3-l1 已算过激活状态从 \(O(Ld)\) 降到 \(O(Nd)\)。4.3.4 把这两笔账搬到 48B 规模上验证「marginal」二字。

**结果出自何方**——引用块的作者列表（Kimi Team 领衔）与论文入口：

> [README.md:L134-L136](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L134-L136)
> `@misc{chen2026attnres,` / `title = {Attention Residuals},` / `author = {Kimi Team and Chen, Guangyu and ...}`

论文正文（[L15-L16](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L15-L16) 的 PDF 与 arXiv 链接）承载所有本讲标注「待确认」的细节，u3-l4 将系统性地精读它。

#### 4.3.4 代码实践：48B 规模下的边际开销费米估算

1. **实践目标**：用 u2-l3/u3-l1 的两个开销结论（每层 \(4d\) 参数；块缓存 \(O(Nd)\)），在 48B 规模的量级上验证 [L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 的「marginal overhead」——纯算术，无需 GPU。
2. **操作步骤**：

```python
# 示例代码: 48B 规模下 AttnRes 边际开销的费米估算 (纯计算)
TOTAL = 48e9                                   # README L105: 48B 总参数
print("== 新增参数: 每层 4d (u2-l3), 对 d/L 扫网格 (真实值待确认) ==")
for d in [2048, 4096, 8192]:
    for L in [32, 64, 96]:
        add = 4 * d * L
        print(f"d={d:5d} L={L:3d}: 新增 {add/1e6:7.2f}M 参数 "
              f"({100*add/TOTAL:.5f}% of 48B)")

print("== 每 token 残差状态 (fp16, 2 字节/元素): 全层保留 vs 块缓存 ==")
d, L = 4096, 64
for N in [8, 16]:
    block_kb = (N + 1) * d * 2 / 1024           # N+1 份块级候选 (u3-l1)
    full_kb = L * d * 2 / 1024                  # Full AttnRes: 保留全部 L 层
    print(f"N={N:2d}: Block 缓存 {block_kb:5.0f} KB/token, "
          f"Full 需 {full_kb:5.0f} KB/token, 比值 1/{full_kb/block_kb:.0f}")
```

3. **需要观察的现象**：参数列全部不超过 3.2M，占 48B 的比例都在 0.01% 以内（最大配置 d=8192、L=96 约 3.1M，占 0.0066%）；块缓存的每 token 显存是几十 KB（N=8、d=4096 时约 72 KB），比 Full 的每 token 全层保留（约 512 KB）小约 7 倍。
4. **预期结果**：结论对 `d`、`L` 的具体取值**不敏感**——即使不知道 Kimi Linear 的真实 \(d\) 与 \(L\)（待确认），扫出来的全部网格配置都支持「参数开销边际、显存开销远小于 Full」这两句话。这就是费米估算的价值：量级结论不依赖未知细节。具体数值待本地运行核对。
5. 延伸一步的换算：48B 总参数、每层主干约 \(12d^2\)（u2-l4 手算式），粗略反推 \(d\) 在数千量级、层数在数十层——正落在扫过的网格内，量级估算自洽。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 AttnRes 与「注意力算子的选择」正交？从伪代码找出证据。

> **答案**：两个 attn_res 位点（L71、L84）都发生在 `self.attn` / `self.mlp` 调用**之前且之外**，它们只决定喂给子层的 `h` 从哪来；子层内部（L80、L87）把 `self.attn`、`self.mlp` 当表达式调用，看不到 `h` 的来历。因此把 softmax 注意力换成线性注意力、dense FFN 换成 MoE，attn_res 的接线逻辑一行不改——三个替换维度互相独立。

**练习 2**：「48B 总参数、3B 激活」是什么含义？这对「两臂对比的公平性」有什么隐含要求？

> **答案**：MoE 路由让每个 token 只激活约 6.25% 的参数（3B/48B），容量与计算解耦。隐含要求是：Baseline 与 AttnRes 两臂必须在**同一底座**（同专家配置、同路由、同数据、同训练长度）上对比，唯一差异是残差接线——否则差异可能来自底座而非残差。README 未给出该对照的协议细节（待确认，以论文实验节为准）。

**练习 3**：我们迷你实验台与该实验的规模差多少？用三个口径分别算。

> **答案**：参数口径：迷你台约 \(1.3\times 10^7\)，对 3B 激活约 230 倍、对 48B 总参数约 3700 倍；数据口径：迷你台 \(3000 \times 64 \times 256 \approx 4.9\times 10^7\) 个训练字符，对 1.4T tokens 约差 28000 倍；计算口径（\(C \approx 6ND\)，u3-l2 的 PFLOP/s-day 换算）约差 6～7 个数量级。这也是本讲综合实践反复强调「只对照形式与方向、不对照数值」的定量依据。

## 5. 综合实践

### 5.1 任务：迷你下游评测——MCQ + 代码补全的两臂对比

这是本讲的主实践，也是任务规格指定的形式：**为你的迷你模型编写一个简单下游评测脚本（多道选择题与代码补全任务各若干条），对比标准残差与 Block AttnRes 的得分，并分析能力维度的差异是否与论文趋势一致**。

完整流程：在混合语料（文本 + 代码）上，用 u2-l4 的配对流程训练 Standard 与 Block AttnRes 各 3 个种子（4.2.4 已完成建题与打分设施的自检），然后用两把打分尺评测全部 6 个模型，产出一张「final val / MCQ acc / code hit / code lp」四列对比表，最后按 5.4 的规则与论文趋势对照。

产出物清单：

- 一个评测脚本（`downstream_eval.py`，复用 `minitest.py` 与 4.2.4 的全部定义）；
- 一张四列对比表（5.3 模板，均值 ± std）；
- 一段趋势对照分析：两臂差异的方向、显著性、以及与 [README.md:L107-L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L107-L119) 九项基准的「形式对应 vs 数值对应」判定。

### 5.2 主脚本

```python
# 示例代码: 综合实践驱动脚本
# (minitest.py 提供 MiniGPT / train / cnt; 4.2.4 提供 build_mixed_corpus /
#  build_mcq / build_codeq / eval_mcq / eval_code)
import statistics as st

CFG = dict(d=256, n_head=8, n_layer=16, block_size=4, t_max=256)  # k=2 → 末端 9 候选
RUN = dict(steps=3000, batch=64, block=256, lr=3e-4, eval_every=500)
SEEDS = [0, 1, 2]

train_ids, val_ids, stoi, itos, ho_t, ho_c, tr_t = build_mixed_corpus(
    'text.txt', 'code.txt')
V = len(stoi)
MCQ, CODEQ = build_mcq(ho_t, tr_t, stoi, n_q=40), build_codeq(ho_c, stoi, n_q=40)
print(f"词表 {V}, MCQ {len(MCQ)} 道, 代码补全 {len(CODEQ)} 道")

rows = []
for mode in ['standard', 'attnres']:
    for seed in SEEDS:
        torch.manual_seed(seed)                       # 各自初始化 (u2-l4 4.4.4)
        model = MiniGPT(V, mode=mode, **CFG)
        hist = train(model, train_ids, val_ids, seed=seed, **RUN)   # 配对数据流
        mcq = eval_mcq(model, MCQ, stoi)
        hit, clp = eval_code(model, CODEQ, stoi, itos)
        rows.append(dict(mode=mode, seed=seed, val=hist['val'][-1],
                         mcq=mcq, hit=hit, clp=clp))
        print(f"{mode:9s} seed={seed}  val={rows[-1]['val']:.4f}  "
              f"MCQ={mcq:.3f}  code_hit={hit:.3f}  code_lp={clp:.4f}")

print(f"\n{'模式':9s} {'final val':>17s} {'MCQ acc':>17s} "
      f"{'code hit':>17s} {'code lp (↑)':>17s}")
for mode in ['standard', 'attnres']:
    r = [x for x in rows if x['mode'] == mode]
    cells = [f"{st.mean([x[k] for x in r]):.4f} ± {st.stdev([x[k] for x in r]):.4f}"
             for k in ['val', 'mcq', 'hit', 'clp']]
    print(f"{mode:9s} " + " ".join(f"{c:>17s}" for c in cells))
```

`code lp` 是平均每字符 logprob，**越大（越接近 0）越好**，与命中率构成「连续口径 + 离散口径」的互补：命中率粗粒度但直观（对应 pass@1 的形态），logprob 平滑但只反映表面似然。

### 5.3 记录表模板

| 模式 | 种子 | final val | MCQ acc | code 命中率 | code lp |
|:---|:---:|:---:|:---:|:---:|:---:|
| standard | 0 / 1 / 2 | | | | |
| attnres | 0 / 1 / 2 | | | | |
| **standard 均值 ± std** | | | | | |
| **attnres 均值 ± std** | | | | | |
| **Δ（attnres − standard）** | | | | | |

另记：语料构成（文本/代码各多少字符）、词表 V、两套题量、未训练自检值（MCQ ≈ 0.25、hit ≈ 0）、两种模型参数量与相对差（应仍为每层 \(4d\)，与语料无关）。

### 5.4 判读规则：mini 结果如何与论文趋势对照

1. **先看度量衡，再看模型分**：未训练自检（MCQ ≈ 0.25、hit ≈ 0）不过关，后面一切数字作废。
2. **训练后两项都应显著高于随机水平**：MCQ 测的是分布内完形（holdout 与训练语料同源），字符级模型应明显超过 0.25；代码命中率预期不高（可能 0～0.3），因为贪心逐字符精确匹配一行代码很难，缩进或常见行（如 `return self`）才有机会命中——具体数值待本地验证。
3. **显著性先于方向**：40 题的二项噪声约 \(\sqrt{p(1-p)/40}\)（p=0.4 时约 0.077）；两臂均值差小于「题噪声 + 种子 std」之和时，只能报告「本规模无显著差异」，这与 u2-l4/u3-l2 的纪律一致。
4. **与论文对照的三条正确姿势**：
   - **形式对应**：我们的 MCQ 对应 MMLU/CMMLU 类（选择似然），代码补全对应 HumanEval/MBPP 类（生成 + 判定）——形态 1:1 复刻；
   - **方向检验**：论文的收益排序是「多步推理/代码 > 知识问答」，我们的两项任务难度都低且都在分布内，**检验不了这个排序**；能检验的只有「AttnRes 是否同向不劣」这一最弱命题；
   - **数值不对应**：4.3.5 练习 3 已算过规模差 6～7 个数量级，任何数值比较都没有意义；若 mini 台上 AttnRes 两项全胜且超噪声，是与 [L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L119) 方向一致的**弱证据**；互有胜负则如实报告——mini 规模既不能证实也不能证伪 48B 的结论。
5. **维度覆盖的诚实清单**：论文九项覆盖英文/中文/知识/推理/代码，我们的迷你台只有「英文风格文本 + Python 代码」两类——中文维度（CMMLU/C-Eval）与多步推理维度（GPQA/MATH）没有对应物。结论表述时把覆盖差距写明。

### 5.5 进阶（可选）：pass@k 的多样本版

把 `greedy_complete` 换成温度采样（`torch.multinomial(F.softmax(logits[0,-1]/temp, -1), 1)`），每题采 \(n=5\) 个补全，命中数记 \(c\)，用 2.3 的无偏估计算 pass@1（\(= c/n\)）与 pass@2。三个可调旋钮各做一行：temp ∈ {0.7, 1.0}、`max_new` ∈ {40, 80}、目标行长度上限。观察命中口径从「贪心精确匹配」换成「多样本任一命中」后，两臂差距是否变得更可测。效果待本地验证。

## 6. 本讲小结

- **读表四种口径**：绝对分差、相对分差、错误率降幅 \(\Delta/(100-\text{Baseline})\)、符号一致性——九项基准（[README.md:L107-L117](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L107-L117)）全部为正，绝对分差前三为 GPQA-Diamond（+7.5）、MATH（+3.6）、HumanEval（+3.1）；总结句（[L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L119)）按维度挑代表，严格排序以逐行数据为准。
- **九项基准两种形态**：MMLU/GPQA/BBH/TriviaQA/CMMLU/C-Eval 走「选项似然/答案匹配」，HumanEval/MBPP 走「生成 + 单元测试 pass@1」；形态决定评测脚本写法，复刻到迷你台时能对应的是**形式**而非**难度**（评测协议 README 未给出，以论文为准，待确认）。
- **底座规格**（[L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)）：Kimi Linear 48B 总参数、3B 激活（MoE）、1.4T tokens——工业级规模；证据链三层：损失层（u3-l2）→ 能力层（本讲）→ 机制层（u2-l5 的训练动态）。
- **正交性**：attn_res 位点在子层之外（[L71/L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)），注意力算子与 FFN 形态是黑盒——在「线性注意力 + MoE」底座上仍全面收益，是对 drop-in 主张（[L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)）最强的背书；48B 规模下每层 \(4d\) 参数占比不足 0.01%（费米估算，对未知 d/L 不敏感）。
- **迷你评测三件套**：holdout 建题（MCQ 完形 + 代码下一行）、两把打分尺（平均每字符 logprob 的选项似然、贪心精确匹配的命中率）、随机水平自检（0.25 / ≈0）——判读纪律：显著性先于方向、形式对应而非数值对应、维度覆盖差距写明。

## 7. 下一步学习建议

- **下一讲 u3-l4（论文精读与复现路线图）**：本讲累积了一批「待确认」——九项基准的评测协议（few-shot、打分口径、是否多种子）、两臂对照的超参口径、Kimi Linear 的结构与训练配方——全部要在 `Attention_Residuals.pdf` 里逐项核对；u3-l4 还会教你选定一个消融结论（如块数 N）设计小规模复现实验。
- **向后衔接 u3-l5（移植到你自己的模型）**：本讲 4.3 的正交性论证是移植的信心来源；把 Block AttnRes 装进一个开源迷你 GPT 实现时，用本讲的评测脚本做移植前后的对照，比只看损失曲线更有说服力。
- **扩展评测设施**：如果想向工业口径靠拢，可以阅读 lm-evaluation-harness 的多选题任务实现（`acc_norm` 的归一化方式与本讲 2.2 一致），把 `option_logprob` 的接口对齐到它的「loglikelihood + length-normalize」约定；再给迷你台补一个中文语料维度，回应 5.4 第 5 条的覆盖差距。
- **回读 README 的证据链顺序**：[L95-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L95-L99)（损失）→ [L105-L119](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105-L119)（下游）→ [L121-L127](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L127)（训练动态）——三节分别对应 u3-l2、本讲、u2-l5，三条证据线在论文里如何互相支撑，值得作为写作范式体会。
- **动手巩固**：把 5.5 的多样本 pass@k 做完，再给 MCQ 建题器加干扰项难度过滤（4.2.5 练习 3），观察「题目质量」对两臂差距可测性的影响——评测工程与模型结构同样是本讲的主题。
