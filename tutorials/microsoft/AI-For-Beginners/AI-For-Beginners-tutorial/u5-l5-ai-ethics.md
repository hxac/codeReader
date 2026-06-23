# AI 伦理与负责任的 AI

## 1. 本讲目标

本讲是整个 AI-For-Beginners 课程在「技术内容」之后的收尾思考课。前面 23 课我们一直在学「怎么把模型训得更准」，这一课要换一个视角，学「一个更准的模型是否就一定是对的」。

学完本讲，你应该能够：

1. 说出 AI 系统中最主要的几类伦理风险（偏见、不透明、隐私泄露、不可靠、不可问责）。
2. 复述微软提出的负责任 AI（Responsible AI）六大原则，并能把每条原则对应到一类具体风险。
3. 学会用简单的度量与清单去评估一个真实 AI 应用的伦理风险，并给出缓解建议。

本讲不引入新的神经网络结构，而是把前几讲（尤其是 u4-l8 大语言模型、u5-l4 多模态 CLIP）训练出来的模型，放回真实社会场景中去审视。

## 2. 前置知识

在进入正文前，先澄清一个最容易混淆的术语——**「bias」一词在本课程里有两层完全不同的含义**：

| 出现位置 | 英文 | 中文 | 含义 |
| --- | --- | --- | --- |
| 神经网络数学（如 u2-l3 感知机） | bias \(b\) | 偏置 | 一个中性的数学参数，作用是把决策边界平移，本身不带褒贬 |
| AI 伦理（本讲） | bias / societal bias | 偏见 | 模型对某类人群系统性不利的不公平倾向，是我们要避免的 |

本讲里凡说到「偏见」「偏见」，一律指第二层含义，与感知机里的偏置 \(b\) 无关。请务必把这两个概念分开记，否则读到「bias 会导致不公平」时会误以为是那个数学参数在捣乱。

此外，理解本讲还需要回顾几个已经讲过的结论：

- **模型从数据中学规律（u1-l4）**：训练数据是什么样，模型就学成什么样；数据里的偏见会被模型忠实复制甚至放大。
- **过拟合（u2-l5）**：模型会把训练样本「背」下来。这种记忆能力在伦理上会变成隐私风险——本讲会用到这一联系。
- **神经网络只输出概率（u2-l4、u3-x 系列）**：模型给的是「置信度」而非「确定结论」，做决策时必须考虑它可能错。
- **大模型的缩放规律（u4-l8）**：模型越大、用得越广，一次偏见的危害面也越大。这正是伦理问题在今天变得紧迫的原因。

## 3. 本讲源码地图

本讲的源码极其简洁——**整门课里少有的「纯内容课」**：

| 文件 | 作用 |
| --- | --- |
| [lessons/7-Ethics/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md) | 本讲唯一源文件。它没有 Notebook、没有 lab、没有 assignment，只有一份正文，分别讲了「负责任 AI 六大原则」和「负责任 AI 工具箱」两块 |

也就是说，本课与前面大多数「README + 可执行 Notebook + lab」三件套的课不同，**它没有可运行的实验代码**。因此本讲的「代码实践」将以「源码阅读型实践 + 示例代码」两种形式展开：先精读 README，再用一段明确标注为示例的 Python 代码，把 README 里提到的公平性度量（FairLearn 思想）和可解释性分析（DiCE 思想）落到具体数字上。

> 本课还在 README 里附了 [课前测验 quiz/5](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L5) 与 [课后测验 quiz/6](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L40)。这两份测验如何由 `etc/quiz-src` 生成、又如何被 `etc/quiz-app` 渲染，将在后续 u6 单元讲解。

## 4. 核心概念与源码讲解

README 开篇先给了一个基调：本课程教的所有 AI，「不过是大矩阵运算」（nothing more than large matrix arithmetic），它是一个强大的工具，而**任何强大的工具都可以被善用，也可以被滥用**（can be used for good and for bad purposes, importantly, it can be *misused*）。这句话在 [lessons/7-Ethics/README.md:9](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L9)。它定下了本讲的核心立场：**AI 本身没有善恶，但它带来的后果有善恶，工程师必须为后果负责。**

下面按三个最小模块拆解。

### 4.1 公平与偏见

#### 4.1.1 概念说明

**公平性（Fairness）** 指模型不应对某类人群造成系统性不利。它最直接的敌人是**模型偏见（model bias）**。

偏见的来源通常是**有偏的训练数据**。README 给了一个非常具体的例子（[lessons/7-Ethics/README.md:15](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L15)）：如果要训练一个模型去预测「某人成为软件开发者的概率」，由于历史上的软件行业以男性居多，训练数据本身就偏向男性，于是模型会倾向于给男性更高的得分。**模型只是如实反映了数据里的不平等，却会被当成「客观」的判断来使用，从而把不平等固化下来。**

这一点和本课程前面的逻辑是一致的：u1-l4 已经讲过「模型从数据中学规律」。当数据里的规律本身就是不公平的，模型学到的「规律」自然也是不公平的。**偏见不是模型坏掉了，而是模型太忠实地学会了世界的不公。**

偏见问题还和另外两条原则紧密相关：

- **包容性（Inclusiveness）**（[README:18](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L18)）：少数群体在数据里本来就少，模型对他们的表现往往更差。AI 的目标应是「增强人（augment）」而非「取代人」，尤其要保证边缘群体被正确对待。
- **可靠性与安全性（Reliability and Safety）**（[README:16](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L16)）：神经网络输出的是**概率**，每个模型都有精度（precision）和召回（recall）。如果只看整体准确率而不看「在哪个群体上错了」，就可能对某类人持续误判造成伤害。

#### 4.1.2 核心流程

偏见不是凭空产生的，它沿着一条「自我强化」的链条传播：

```
真实世界存在不公
      │
      ▼
数据采集 → 得到有偏数据（少数群体样本少 / 标签带历史歧视）
      │
      ▼
训练模型 → 模型学到有偏规律
      │
      ▼
模型做决策（招聘、放贷、推荐）→ 把不公施加到真实个体
      │
      ▼
这些决策又成为未来的数据 → 偏见被放大（反馈循环）
```

要打断这条链条，关键是**用度量把偏见变成可见的数字**。最常用的一种叫**人口平权（demographic parity）**：比较模型对不同群体的「正向预测率」是否接近。设 \(\hat{y}=1\) 表示模型给出有利预测（如「推荐面试」），\(A\) 表示敏感属性（如性别），则人口平权差为：

\[
\Delta_{\text{DP}} = \big|\, P(\hat{y}=1 \mid A=0) - P(\hat{y}=1 \mid A=1) \,\big|
\]

\(\Delta_{\text{DP}}\) 越接近 0，表示两个群体获得有利预测的机会越均等；差值越大，说明模型对某一群体存在系统性偏好。注意：人口平权只是众多公平定义之一（其它还有「机会均等 / equalized odds」要求两组的错误率一致等），不同定义之间有时互相冲突，没有「唯一正确」的公平，但**先把差距测出来，是缓解的前提**。

#### 4.1.3 源码精读

README 把公平性列为六大原则之首，原文是：

> **Fairness** is related to the important problem of *model biases*, which can be caused by using biased data for training...
> We need to carefully balance training data and investigate the model to avoid biases...

见 [lessons/7-Ethics/README.md:15](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L15)。这段话给出了两条缓解手段：**「balance training data」（平衡训练数据）** 和 **「investigate the model」（审查模型）**——前者对应数据采样的平衡，后者正是下面工具实践做的事。

而要做到「investigate the model」，README 在工具部分点名了 **FairLearn**（公平性仪表盘）：

> Fairness Dashboard (FairLearn)

见 [lessons/7-Ethics/README.md:27](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L27)。FairLearn 这类工具的本质，就是把上面 \(\Delta_{\text{DP}}\) 这类指标自动算出来并可视化，让你一眼看到模型在哪个群体上失衡。下面的代码实践就是它的最小内核。

#### 4.1.4 代码实践

下面是一段**示例代码**（不在本仓库中，仅用于把 README 第 15、27 行的思想落地）。它用「人口平权差」量化一个分类模型的偏见，依赖 `numpy`（ai4beg 环境自带）。

```python
# 示例代码：用「人口平权差」量化一个分类模型的偏见
# 对应 README 第 15 行「公平性」原则与第 27 行 FairLearn 工具背后的核心度量思想。
# 本文件不在仓库中，仅为帮助理解而编写。

import numpy as np

# 场景：一个「是否推荐进入面试」的二分类模型。
# sensitive_group：0 = 少数群体, 1 = 多数群体（示意性别等敏感属性，每组各 5 人）
sensitive_group = np.array([0,0,0,0,0, 1,1,1,1,1])
# 模型给出的预测：1 = 推荐, 0 = 不推荐（明显偏向多数群体）
predictions     = np.array([0,0,1,0,0, 1,1,1,1,0])

def positive_rate(pred):
    """预测为 1 的比例，即正向预测率。"""
    return pred.mean()

rate_0 = positive_rate(predictions[sensitive_group == 0])  # 少数群体推荐率
rate_1 = positive_rate(predictions[sensitive_group == 1])  # 多数群体推荐率
demographic_parity_diff = abs(rate_0 - rate_1)

print(f"少数群体推荐率: {rate_0:.2f}")
print(f"多数群体推荐率: {rate_1:.2f}")
print(f"人口平权差 Δ_DP（越接近 0 越公平）: {demographic_parity_diff:.2f}")
```

**实践步骤：**

1. **目标**：亲手算出一个有偏模型的人口平权差，建立「偏见是可度量的数字」的直觉。
2. **操作**：把上述示例代码存为 `fairness_demo.py`，在 ai4beg 内核下运行 `python fairness_demo.py`（或在一个 Notebook 的 cell 里运行）。
3. **观察**：少数群体 5 人中只有 1 人被推荐（`rate_0 = 0.20`），多数群体 5 人中有 4 人被推荐（`rate_1 = 0.80`）。
4. **预期结果**：`Δ_DP = 0.60`，这是一个很大的差值，说明模型对少数群体存在明显不利。
5. **延伸思考（待本地验证）**：如果把 `predictions` 改成两组推荐率相同（如都给 2 人推荐），`Δ_DP` 应变为 0。这说明：**公平与否取决于预测在不同群体间的分布，而不是模型在整体上的准确率。**

#### 4.1.5 小练习与答案

**练习 1**：为什么「模型整体准确率达到 95%」并不代表它是公平的？

> **参考答案**：因为准确率是全体样本上的平均值，它掩盖了分群体的差异。模型可能在多数群体上准确率 99%、却在少数群体上只有 60%——只要少数群体样本占比小，整体准确率仍可能很高。因此评估公平性必须**分组**计算指标（如分群体的召回率、正向预测率），而不是只看总体。

**练习 2**：README 说缓解偏见要「balance training data」和「investigate the model」。请各举一个具体做法。

> **参考答案**：平衡数据——例如采集时对少数群体做过采样（oversampling），或对不同群体配平样本数量，使训练集不再偏斜；审查模型——按敏感属性分组计算精度/召回/正向预测率（即上面代码做的事），找出哪类人被系统性误判，再决定是否重新采样、重新加权或更换特征。

### 4.2 透明与隐私

#### 4.2.1 概念说明

这一模块对应两条原则。

**透明性（Transparency）**（[README:19](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L19)）有两层要求：

1. **让用户知道 AI 正在被使用**——不能把 AI 的输出伪装成人的判断。
2. **尽可能使用可解释（interpretable）的模型**——能说清「为什么会得出这个结论」。

这对深度学习是个挑战。前面 u4-l6 的 BERT、u4-l8 的 GPT 都是层数极深、参数上亿的黑箱，它们给出一个预测，但很难直观说明「是哪个词、哪个特征起了决定作用」。**模型越强大，往往越不透明**——这正是「能力」与「可解释性」之间的一对张力。

**隐私与安全（Privacy and Security）**（[README:17](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L17)）则点出一个 AI 特有的现象：**训练数据会被「整合（integrated）」进模型里**。README 把它描述为一把双刃剑——

- 好的一面：模型部署后并不直接暴露原始数据库，看起来更安全。
- 坏的一面：我们必须清楚「模型是用哪些数据训出来的」，因为模型可能**记住**了具体的训练样本，从而泄露隐私。

这与 u2-l5 讲的**过拟合**直接相关：一个把训练集「背」得滚瓜烂熟的过拟合模型，其实就是一个会泄露训练数据的模型。例如 u4-l8 的大语言模型如果过度记忆了训练语料里的某段私人信息，就可能在生成时把它原样吐出来。**过拟合不只是技术问题，更是隐私问题。**

#### 4.2.2 核心流程

透明性的实践流程是：

```
披露 AI 在使用 → 用可解释方法说明决策依据 → 如实传达不确定性（模型给的是概率，不是定论）
```

隐私风险的传导链是：

```
敏感训练数据 → 模型过度记忆（过拟合）→ 对外服务时被特定查询「套出」原始数据（成员推断 / 记忆泄露）
```

README 的工具箱里有两个工具正好服务于这两条链：

- **InterpretML**（可解释性仪表盘，[README:26](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L26)）：把模型的决策拆解成各个特征的贡献，回答「是哪些因素导致了这个预测」，支撑透明性。
- **DiCE**（反事实分析，[README:32](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L32)）：回答「如果我把输入的哪个特征改一点点，预测结果就会翻转」。这既能用来解释决策，也能用来发现**不公平的特征**（例如「只要把性别改一下，贷款就被批准」说明模型在用不该用的特征）。

#### 4.2.3 源码精读

透明性原则的原文：

> **Transparency**. This includes making sure that we are always clear about AI being used. Also, wherever possible, we want to use AI systems that are *interpretable*.

见 [lessons/7-Ethics/README.md:19](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L19)。

隐私与安全原则的原文：

> **Privacy and Security** have some AI-specific implications. For example, when we use some data for training a model, this data becomes somehow "integrated" into the model.

见 [lessons/7-Ethics/README.md:17](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L17)。注意这里的「integrated」正是隐私风险的源头。

DiCE 工具的描述：

> DiCE - tool for Counterfactual Analysis allows you to see which features need to be changed to affect the decision of the model

见 [lessons/7-Ethics/README.md:32](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L32)。

此外，**问责性（Accountability）**（[README:20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L20)）也常被归到透明这一族：模型做了决策，但「谁来负责」并不总是清楚，README 的解法是**把人放进决策回路（human in the loop）**，让真实的人对重要决策负责，而不是让模型独自拍板。

#### 4.2.4 代码实践

下面这段**示例代码**手工模拟 DiCE 的「反事实分析」：给定一个被拒绝的申请，找出**翻转预测所需的最小特征改动**。这能把 README 第 32 行抽象的「which features need to be changed」变成具体数字。

```python
# 示例代码：手工「反事实」分析——找到翻转预测的最小特征改动
# 对应 README 第 32 行 DiCE 工具的核心思想。本文件不在仓库中，仅作演示。

def score(years_employed, income):
    """示意性贷款打分函数：工作年限越长、收入越高，得分越高。"""
    return years_employed * 2 + income * 0.01

THRESHOLD = 60.0  # 达到 60 分即「批准」

def approved(years_employed, income):
    return score(years_employed, income) >= THRESHOLD

# 某申请人当前状态：被拒绝
y, inc = 5, 1000
print("当前是否批准:", approved(y, inc), " 得分:", score(y, inc))

# 反事实：单独把哪个特征改到「足够大」就能翻转为批准？
# 真实 DiCE 会在连续空间搜索最小扰动；这里用穷举作示意。
for delta_y in range(0, 30):                # 多工作几年
    if approved(y + delta_y, inc):
        print(f"仅增加「工作年限」+{delta_y} 年即可批准"); break
for delta_i in range(0, 100000, 500):       # 收入增加
    if approved(y, inc + delta_i):
        print(f"仅增加「收入」+{delta_i} 即可批准"); break
```

**实践步骤：**

1. **目标**：理解「反事实分析」如何成为可解释性与公平性审查的工具。
2. **操作**：在 ai4beg 内核下运行上述示例代码。
3. **观察**：当前得分 \(5\times2 + 1000\times0.01 = 20\)，远低于 60，被拒绝。
4. **预期结果**：程序会指出「仅增加工作年限 +20 年」或「仅增加收入 +4000」即可翻转。这说明模型对这两个特征是敏感的、且决策是可解释的。
5. **伦理延伸（待本地验证）**：如果你发现「只要把某个**敏感属性**（如性别、种族）改一下，预测就翻转」，那就说明模型在使用不该用的特征——这正是 DiCE 用来**抓偏见**的方式。请思考：这个模型有没有把任何敏感属性当成了打分依据？

#### 4.2.5 小练习与答案

**练习 1**：为什么说过拟合的模型同时也是隐私风险最高的模型？

> **参考答案**：过拟合意味着模型把训练样本「记」进了权重里，而不是学到泛化规律。那么只要构造恰当的查询，就可能让模型把记忆里的具体训练数据「套」出来（成员推断攻击 / 生成式模型直接复述训练文本）。所以 u2-l5 教的对抗过拟合手段（Dropout、早停、正则化），同时也在保护隐私。

**练习 2**：透明性的两层要求分别是什么？为什么深度学习模型实现第二层更困难？

> **参考答案**：两层是「①始终向用户说明 AI 正在被使用；②尽可能采用可解释的模型」。深度模型层数深、参数多，输入到输出之间经过大量非线性变换，很难直观说清「是哪个特征导致了这个结论」，因此可解释性差。这也是 InterpretML、DiCE 这类事后解释工具存在的原因——模型本身不透明，就靠外部工具去「翻译」它的决策。

### 4.3 负责任 AI 原则

#### 4.3.1 概念说明

前两个模块分别讲了「公平」和「透明/隐私」。本模块把它们收拢成一套**完整的框架——微软负责任 AI 六大原则**。README 在 [第 11-13 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L11-L13) 指出：为避免 AI 被「意外或故意地滥用（accidental or purposeful misuse）」，微软提出了这六大原则：

| 原则 | 核心问题 | README 行号 |
| --- | --- | --- |
| **公平性 Fairness** | 模型是否对某类人群不利（偏见） | [:15](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L15) |
| **可靠性与安全性 Reliability & Safety** | 模型会犯错，输出只是概率，要防误判伤人 | [:16](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L16) |
| **隐私与安全 Privacy & Security** | 训练数据被「整合」进模型，可能泄露 | [:17](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L17) |
| **包容性 Inclusiveness** | AI 要增强人而非取代人，照顾少数群体 | [:18](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L18) |
| **透明性 Transparency** | 告知 AI 在用、尽量用可解释模型 | [:19](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L19) |
| **问责性 Accountability** | 谁来为决策负责？把人放回决策回路 | [:20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L20) |

这六条不是互相孤立的口号，而是**一组互相补位的防线**：公平关心「对谁有利」，可靠关心「会不会错」，隐私关心「数据安不安全」，包容关心「谁被遗漏」，透明关心「能不能说清」，问责关心「出了事谁负责」。一个真实的 AI 系统需要同时满足这六条，缺任何一条都可能酿成事故。

#### 4.3.2 核心流程

把六大原则落到工程上，就是一个贯穿「数据 → 模型 → 部署」的**负责任 AI 生命周期**：

```
1. 定义问题与利益相关方
        │  谁会被这个模型影响？哪些是弱势群体？
        ▼
2. 审查数据（公平性 + 包容性）
        │  数据是否平衡？是否覆盖了少数群体？
        ▼
3. 训练并分组评估（可靠性 + 公平性）
        │  不仅看总体精度，还要分群体看 precision/recall/正向预测率
        ▼
4. 解释与记录（透明性 + 问责性）
        │  用 InterpretML/DiCE 解释决策；记录数据来源与模型版本
        ▼
5. 保护数据（隐私与安全）
        │  记得模型用了哪些数据，防止过拟合记忆导致的泄露
        ▼
6. 人参与决策（问责性）
        │  重要决策保留人工复核（human in the loop）
        ▼
7. 部署后持续监控
        │  数据分布会漂移，偏见会随时间重新出现，需要长期监测
```

README 在 [第 22-32 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L22-L32) 给出的 **Responsible AI Toolbox（负责任 AI 工具箱）**，就是支撑上述每一步的软件：

- **InterpretML**（[:26](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L26)）——可解释性，支撑第 4 步；
- **FairLearn**（[:27](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L27)）——公平性度量，支撑第 2、3 步；
- **Error Analysis Dashboard**（[:28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L28)）——错误分析，找出模型在哪类样本上错得最多；
- **EconML**（因果分析，[:31](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L31)）——回答 what-if 因果问题；
- **DiCE**（反事实分析，[:32](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L32)）——反事实解释，支撑第 4 步与偏见审查。

#### 4.3.3 源码精读

README 用两段话框定了整个原则体系。原则部分的开篇：

> To avoid this accidental or purposeful misuse of AI, Microsoft states the important Principles of Responsible AI. The following concepts underpin these principles:

见 [lessons/7-Ethics/README.md:11-13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L11-L13)。

工具部分的开篇：

> Microsoft has developed the Responsible AI Toolbox which contains a set of tools...

见 [lessons/7-Ethics/README.md:22-24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L22-L24)。

最后，README 还特别说明本课没有 assignment，而是**把带作业的实践指向了姊妹课程 ML-For-Beginners 的公平性课**：

> For more information about AI Ethics, please visit this lesson on the Machine Learning Curriculum which includes assignments.

见 [lessons/7-Ethics/README.md:34](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L34)。这正是本课「纯内容、无 Notebook」的原因，也是本讲实践采用「清单评估」形式的依据。README 末尾还推荐了一条系统学习的 [Learn Path（[:38](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L38)）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L38)。

#### 4.3.4 代码实践（本讲主实践）

这是本讲规格要求的**主代码实践任务**：针对一个你接触过的真实 AI 应用，写一份**伦理风险评估清单**。它属于「源码阅读型实践」——必须先精读 README 的六大原则，再据此为真实应用逐条评估。

**实践步骤：**

1. **目标**：把 README 抽象的六大原则，转化成对具体 AI 应用的、可操作的风险检查。
2. **选一个应用**：从你日常接触的 AI 应用里挑一个，例如：招聘/简历筛选系统、贷款审批模型、短视频/新闻推荐算法、人脸识别门禁、或 u4-l8 讲的那类聊天大模型。
3. **填表**：按下面模板，为六大原则各写一行——「该应用在这一条上的潜在风险」+「你建议的缓解/检查措施」+「你用来判断的证据或问题」。模板示例（以「AI 简历筛选系统」为例）：

| 原则 | 潜在风险 | 建议措施 | 怎么判断 |
| --- | --- | --- | --- |
| 公平性 | 历史招聘以男性居多，模型可能压低女性评分 | 平衡训练数据；分性别计算正向预测率（用 4.1 的代码） | 分组 \(\Delta_{\text{DP}}\) 是否显著大于 0 |
| 可靠性 | 把合格者误筛掉（漏召） | 设人工复核通道，关注召回而非仅精度 | 抽查被拒简历中有多少其实合格 |
| 隐私 | 简历含身份证、住址等敏感信息，可能被模型记忆 | 限制可训练字段；定期检查是否泄露 | 用特定查询测试能否套出训练数据 |
| 包容性 | 残障人士、转行者等少数群体样本少 | 主动补充这类样本；不把模型当唯一裁决 | 少数群体上的准确率是否过低 |
| 透明性 | 应聘者不知道自己被 AI 筛选，也不知道为何被拒 | 明示 AI 在使用；用 DiCE/InterpretML 给出关键因素 | 能否用一两句话解释每次拒绝 |
| 问责性 | 算法误判时责任不清 | 关键岗位保留人工复核（human in the loop） | 是否有真人能推翻模型决定 |

4. **观察**：填完后你会发现，不同应用的风险分布不同——推荐算法的「公平/包容」风险高，聊天大模型的「可靠（胡编）/隐私」风险高，人脸识别的「公平/问责」风险高。
5. **预期结果**：得到一份 6 行的清单，每一行都把抽象原则连到了具体可执行的检查项。**这不是「假装运行」，而是一份真实的、可以直接拿去给团队评审的交付物。**

#### 4.3.5 小练习与答案

**练习 1**：六大原则中，哪一条最强调「把人放回决策回路」？为什么这一条对所有原则都至关重要？

> **参考答案**：问责性（Accountability，[:20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L20)）。因为模型本身无法承担道德与法律责任——模型只会输出概率，不承担后果。只有让真实的人在重要决策上保留复核与推翻权（human in the loop），公平、可靠、透明等其它原则才有了被追究和纠偏的落点。

**练习 2**：为什么 README 说 AI「可以被善用也可以被滥用」之后，紧接着就要谈原则？这两者是什么关系？

> **参考答案**：因为正是由于 AI 是强大而中立（只是大矩阵运算）的工具，它既可能造福也可能伤人，所以才需要一套外部约束（六大原则）来引导它的使用方向。原则不是模型的属性，而是**使用模型的人和组织**的行为规范——它把「能力」和「正当使用」区分开来。

## 5. 综合实践

把本讲三个模块串起来，做一次迷你「负责任 AI 审计」。挑一个你真实使用过的 AI 应用（如某个推荐 App、某个 AI 写作助手，或一个虚构的「AI 简历筛选系统」），完成三件事：

1. **度量偏见（4.1）**：用 4.1.4 的人口平权差示例代码，为该应用构造一个最小场景（哪怕是 10 个样本的玩具数据），算出 \(\Delta_{\text{DP}}\)，判断它是否对某类用户不利。
2. **做一次反事实检查（4.2）**：用 4.2.4 的示例代码思路，设想该应用的一个决策，找出「改变哪个最小特征就能翻转结果」，并判断它是否依赖了不该依赖的敏感属性。
3. **出一份六原则清单（4.3）**：填写 4.3.4 的风险清单表格。

最后，用一段话（150 字以内）写出你的**总体建议**：这个应用在伦理上是否可以上线？如果不能，最该先解决哪一条原则对应的问题？把这三步的产出整理成一份 `ethics-audit.md`（写到你的本地笔记即可，不要写入仓库），它就是本讲的最终交付物。

## 6. 本讲小结

- 本课是全课程唯一的**纯内容课**（无 Notebook、无 lab），核心源文件就是 [lessons/7-Ethics/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md)，立场是「AI 只是大矩阵运算，是可被善用也可被滥用的工具」。
- **公平与偏见**：偏见来自有偏的训练数据，会沿「数据→模型→决策→新数据」的链条自我放大；可用**人口平权差**等指标把它量化，对应工具 FairLearn。
- **透明与隐私**：透明要求「告知 AI 在用 + 尽量可解释」；隐私风险源于训练数据被「整合」进模型，而过拟合的模型同时是隐私风险最高的模型；对应工具 InterpretML、DiCE。
- **负责任 AI 原则**：微软六大原则（公平、可靠、隐私、包容、透明、问责）是一组互相补位的防线，配合 Responsible AI Toolbox 形成从数据到部署的治理生命周期。
- 关键澄清：神经网络里的 bias（偏置参数 \(b\)）和伦理里的 bias（偏见）是**两个完全不同的概念**，不要混淆。
- 实践产出：一份针对真实 AI 应用的**六原则伦理风险评估清单**，把抽象原则落到可执行的检查项。

## 7. 下一步学习建议

1. **动手真正的公平性工具**：README 指向了带作业的姊妹课程——[ML-For-Beginners 的公平性课](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L34)，那里有用 FairLearn 的真实代码作业，建议完成以补足本课缺的实践。
2. **系统学习**：跟随 README 推荐的 [Responsible AI Learn Path](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/7-Ethics/README.md#L38) 通读微软官方的负责任 AI 培训。
3. **回到工程视角**：本课是 AI 内容的收尾。接下来进入 **u6 配套工具与维护机制** 单元——你将看到本课反复提到的课前/课后测验（quiz/5、quiz/6）是如何由 `etc/quiz-src` 生成、又如何被 `etc/quiz-app` 这个 Vue 应用渲染出来的，从「课程内容」转入「课程仓库的工程维护」。
