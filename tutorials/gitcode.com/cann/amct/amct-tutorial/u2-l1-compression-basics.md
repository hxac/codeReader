# 模型压缩与量化的基本原理

## 1. 本讲目标

在 [u1-l1](u1-l1-project-overview.md) 里我们已经建立了 AMCT 的全局认知（昇腾 NPU 原生的模型压缩工具包），在 [u1-l4](u1-l4-first-quant-cli.md) 里也跑通了 `eval → extract_ptq_data → ptq → deploy` 四条命令。但到目前为止，「量化」「压缩」对我们来说还只是几个名词。

本讲要把这些名词变成**可以讲清楚的概念**。学完本讲，你应当能够：

1. 说清楚**模型压缩要解决的三个问题**：减小体积、降低时延、提升推理效率。
2. 认识压缩的**方法家族**（量化、稀疏、蒸馏、张量分解、算子融合），并理解量化在其中扮演的角色。
3. 用自己的话讲明白**量化运行原理**：浮点数如何映射成低比特整数，scale/offset 和量化粒度是怎么回事。
4. **区分 PTQ（训练后量化）与 QAT（量化感知训练）**，并能判断什么场景该用哪一个。
5. 理解 AMCT 的设计取舍：**把量化和模型转换分开、对可量化算子独立量化**，并知道 AMCT 的 LLM 主流程属于 PTQ。

本讲是概念课，几乎不涉及 Python 源码，主要精读对象是 AMCT 的官方概念文档 `docs/zh/compression_concepts.md`。把这一篇读透，后面再去看算法和源码就不会被术语卡住。

## 2. 前置知识

本讲默认你已经读过 [u1-l1](u1-l1-project-overview.md)，对下面的术语有印象即可：

- **量化（Quantization）**：把高精度的浮点数（如 float32/float16）变成低比特表示（如 INT8/INT4）。
- **推理（Inference）**：模型训练好之后，用它做预测的阶段（区别于训练阶段）。
- **NPU / 昇腾 / CANN**：NPU 是华为的 AI 处理器，昇腾是它的产品品牌，CANN 是驱动它运行的软件栈。
- **PTQ / QAT**：两种量化路线，本讲的核心就是讲清它们的区别。
- **LLM**：大语言模型，AMCT 当前最重要的量化对象。

此外需要两个最基础的知识点：

- **浮点数与整数**：计算机里 `float32` 用 32 个比特表示一个实数，`int8` 只用 8 个比特表示一个整数（范围 -128 ~ 127）。比特数越少，能表示的数值越少、越粗糙，但占的存储和带宽也越少。
- **矩阵乘法（MatMul）**：神经网络里绝大多数计算都是大矩阵相乘。量化之所以能「加速」，本质上是因为低比特矩阵乘比浮点矩阵乘更快、更省显存。

如果你对「比特数」「浮点数」这些词完全陌生，建议先花十分钟搜索一下「浮点数表示」「int8 范围」再继续。

## 3. 本讲源码地图

本讲的「源码」其实是 AMCT 的**概念文档**，它集中解释了所有压缩术语，是后续读代码的概念词典。

| 文件 | 作用 |
| --- | --- |
| [docs/zh/compression_concepts.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md) | AMCT 官方压缩概念手册，覆盖量化、稀疏、蒸馏、张量分解、部署优化等全部概念。本讲的主要精读对象。 |
| [README.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/README.md) | 项目总览，其中性能收益表直观展示了压缩带来的体积/速度收益，可作为本讲的背景资料。 |

> 提示：`compression_concepts.md` 既讲了「经典图压缩」（classic 流程里的稀疏、张量分解），也讲了「量化」（LLM PTQ 主流程的核心）。本讲重点取**量化**部分；稀疏、蒸馏等只做全景式介绍，留到 [u9-l1](u9-l1-classic-graph-compression.md) 再细讲。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 模型压缩全景**：为什么要压缩，有哪些方法。
- **4.2 量化运行原理**：量化到底在做什么（本讲核心）。
- **4.3 PTQ vs QAT**：两种量化路线的取舍（本讲核心）。
- **4.4 AMCT 的量化设计哲学**：转换与量化分离、算子独立量化。

### 4.1 模型压缩全景：目标与方法家族

#### 4.1.1 概念说明

一个训练好的大模型，动辄几十、上百 GB。直接拿去推理会遇到三个痛点：

1. **存储/显存大**：权重太大，放不进显存，加载慢。
2. **时延高**：数据搬运和计算都多，响应慢。
3. **算力贵**：浮点矩阵乘耗费大量算力，吞吐量上不去。

**模型压缩（Model Compression）** 就是想办法在不明显掉精度的前提下，让模型变得更小、更快、更省。AMCT 文档开头一句话就把目标说清楚了：

> 「让最终生成的网络模型更加轻量化，从而达到节省网络模型存储空间、降低传输时延、提高计算效率」
> —— [docs/zh/compression_concepts.md:7](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L7)

压缩不是单一技术，而是一个**方法家族**。AMCT 文档介绍了五种主要手段：

| 方法 | 一句话原理 | 是否要重训练 |
| --- | --- | --- |
| **量化** | 把权重/激活从浮点变成低比特 | 看路线：PTQ 不用，QAT 要 |
| **稀疏** | 把不重要的权重置 0，减少有效计算 | 要重训练 |
| **逐层蒸馏** | 用原模型当老师，教小/量化模型 | 要训练（但无需标签） |
| **张量分解** | 把大卷积核分解成两个小核的连乘 | 通常需要重训练 |
| **模型部署优化（算子融合）** | 把多个算子合并成一个，减少运算次数 | 不用 |

本讲（以及整个 AMCT 的 LLM 主流程）聚焦**量化**。其余方法我们只做认识，知道它们存在于 AMCT 的 classic 经典流程里即可（见 [u1-l3](u1-l3-directory-structure.md) 里提到的 `classic/graph_based`）。

#### 4.1.2 核心流程

不同压缩方法达到「变小变快」的路径不同：

- **量化**：降低**每个数值的比特数**（32 bit → 8 bit → 4 bit）。体积直接按比特比例缩小。
- **稀疏**：降低**有效数值的个数**（部分权重归零）。配合专门硬件可跳过 0 的计算。
- **蒸馏**：训练一个**更小或量化的学生模型**去模仿大模型，属于「换一个更省的模型」。
- **张量分解**：把大矩阵/大卷积核**拆成两个小的连乘**，用低秩近似降低参数量和计算量。
- **算子融合**：在部署期把相邻算子**数学等价地合并**，减少访存和算子开销。

它们的共性是：**用近似换收益**，所以都面临「省多少 vs 掉多少精度」的权衡。

#### 4.1.3 源码精读

下面把文档里这几种方法的定位原文列出来，方便对照：

- **稀疏**总览——AMCT 有「通道稀疏」和「4 选 2 结构化稀疏」两种，且每次只能使能一种：
  [docs/zh/compression_concepts.md:194-198](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L194-L198)。通道稀疏颗粒度更大、性能收益更大但精度影响也更大（[L200-L204](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L200-L204)）；4 选 2 则在每 4 个连续权重里保留 2 个最重要的（[L207-L215](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L207-L215)）。

- **逐层蒸馏**——以原模型为教师网络监督量化模型训练，介于 PTQ 与 QAT 之间：
  [docs/zh/compression_concepts.md:221-230](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L221-L230)。文档明确给了相对定位：比 PTQ 精度更好、比 QAT 不需要标签数据且更快（[L227-L228](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L227-L228)）。

- **张量分解**——把大卷积核分解为低秩张量，文档举了一个减少 66.7% 计算量的例子：
  [docs/zh/compression_concepts.md:232-238](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L232-L238)。

- **模型部署优化**——算子融合，例如把卷积层和 BN 层融成一个卷积层：
  [docs/zh/compression_concepts.md:240-246](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L240-L246)。

> 注意：稀疏、张量分解主要面向 CV（计算机视觉）类卷积模型，属于 AMCT 的 classic 经典流程；本讲的量化主线才是 LLM 的主战场。

#### 4.1.4 代码实践

**实践目标**：建立压缩方法的「脑内索引」，以后看到术语能秒对应到方法。

**操作步骤**：

1. 打开 [docs/zh/compression_concepts.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md)。
2. 滚动浏览一级标题（`## 量化`、`## 稀疏`、`## 组合压缩`、`## 逐层蒸馏`、`## 张量分解`、`## 模型部署优化`）。
3. 仿照上面的「方法家族」表，自己画一张表，每行写：方法名、原理一句话、是否要重训练、对应文档行号。

**需要观察的现象**：你会注意到文档把**量化**写得最详细（占了全文一大半），其余方法相对简短——这正反映了 AMCT 当前的重心在量化。

**预期结果**：得到一张 5 行的速查表，并意识到「量化」是后续学习的主线，其他方法是支线。

#### 4.1.5 小练习与答案

**练习 1**：稀疏和量化都能让模型变小，它们最本质的区别是什么？

> **参考答案**：量化降低的是**每个数值的比特宽度**（数值还在，但更粗糙）；稀疏降低的是**非零数值的个数**（直接把一部分权重置 0）。量化是「精度变粗」，稀疏是「数量变少」。

**练习 2**：文档里说通道稀疏「性能收益更大但精度影响也更大」，而 4 选 2 稀疏相反。请用「颗粒度」解释这句话。

> **参考答案**：通道稀疏一次性裁掉整条通道，颗粒度大，省的计算多但丢的信息也多；4 选 2 只在每 4 个权重里裁 2 个，颗粒度小，保留的信息更多、精度更好，但能省的计算相对少。

---

### 4.2 量化运行原理

#### 4.2.1 概念说明

**量化（Quantization）** 的定义在文档里很直白：

> 「量化是指对模型的权重（weight）和数据（activation）进行低比特处理」
> —— [docs/zh/compression_concepts.md:7](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L7)

这里出现两个关键对象：

- **权重（weight）**：模型本身自带的参数，训练完就固定了，是「静态」的。
- **激活（activation）**：数据流过网络时每一层的输出，是「动态」的，随输入变化。

量化的本质是**建立一个从浮点区间到低比特整数集合的映射**。以 INT8 为例：浮点数范围可能很大（比如 -100 ~ 100），但 int8 只能表示 -128 ~ 127 这 256 个整数。我们需要一个「标尺」把浮点区间压缩到这 256 个格子里，这个标尺就是**量化因子**。

文档用一个图说明了 INT8 量化的运行原理：

> 以量化到 INT8 数据类型为例，其运行原理如下图所示。
> —— [docs/zh/compression_concepts.md:9-11](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L9-L11)

#### 4.2.2 核心流程

量化的核心是一组数学变换。文档给出了 INT8 量化的核心公式：

\[
\text{int\_val} = \text{clip}\bigl(\text{round}(\text{float\_val} / \text{scale} + \text{offset}),\,-128,\,127\bigr)
\]

来源：[docs/zh/compression_concepts.md:144-146](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L144-L146)

公式里每个部分都对应一个直觉：

1. **`float_val / scale`**：用缩放因子 `scale` 把浮点数「压」到整数尺度。`scale` 越小，能表示的范围越大、但每个格子越粗。
2. **`+ offset`**：偏移量，用来对齐零点（当浮点数据的分布不以 0 为中心时有用）。
3. **`round(...)`**：四舍五入到最近整数——这一步**不可逆**，是量化误差的主要来源。
4. **`clip(..., -128, 127)`**：裁剪到 int8 的合法范围——超出范围的值会被「截断」（饱和），这也是一种误差。

反量化（把整数还原成浮点）就是反过来：`float_val ≈ (int_val - offset) * scale`。

**量化因子**分两套（[L148-L153](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L148-L153)）：

- `scale_d / offset_d`：**数据（激活）量化**因子。
- `scale_w / offset_w`：**权重**量化因子，支持标量（整层统一）或向量（更细粒度）两种模式。

**量化粒度（Granularity）** 决定「多少个数值共享一组 scale/offset」。文档列了四种常见粒度（[L156-L192](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L156-L192)）：

| 粒度 | 简称 | 量化对象 | 参数 shape | 直觉 |
| --- | --- | --- | --- | --- |
| per-tensor | T | 整个 Tensor | `(1,)` | 最粗：一整层共用一个 scale |
| per-channel | C | 右矩阵（权重） | `(n,)` | 每个输出通道一个 scale |
| per-group | G | 右矩阵（权重），AMCT 仅支持右矩阵 | `(k/gs, n)` | 在 reduce 轴上分组，更细 |
| per-token | K | 左矩阵（激活） | `(m,)` | 每个 token 一个 scale |

> 经验法则：粒度越细，精度通常越好，但要存的量化参数也越多。LLM 量化里常见的组合是 **权重 per-group / 激活 per-token**。

**权重 vs 激活的量化时机也不同**（这是理解 PTQ 的关键，下一节用到）：

- **权重量化**：权重训练完就固定，可以**离线**算出量化参数（[L128-L130](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L128-L130)）。
- **激活量化**：激活随输入变化，分布未知，必须**在线**（推理/训练时）确定，所以需要校准数据来模拟它的分布（[L132-L134](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L132-L134)）。

#### 4.2.3 源码精读

把量化原理相关的文档段落集中对照一遍：

- 量化定义与目标（轻量化、省存储、降时延、提效率）：
  [docs/zh/compression_concepts.md:7](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L7)
- INT8 量化的本质——把 float 矩阵乘换成 int8 矩阵乘以加速和压缩：
  [docs/zh/compression_concepts.md:96-97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L96-L97)
- INT4 比 INT8 压缩更好但精度损失更大：
  [docs/zh/compression_concepts.md:99-101](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L99-L101)
- 权重量化 vs 激活量化的定义：
  [docs/zh/compression_concepts.md:128-134](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L128-L134)
- 量化因子 scale/offset 与 INT8 公式：
  [docs/zh/compression_concepts.md:140-153](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L140-L153)
- 四种量化粒度的定义与示意图：
  [docs/zh/compression_concepts.md:156-192](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L156-L192)

#### 4.2.4 代码实践

**实践目标**：用十几行 Python 手搓一个 INT8「伪量化」，亲眼看到 scale/clip/round 各自引入的误差，建立对量化公式的直觉。

**操作步骤**：

1. 准备一段示例输入（模拟一小段权重或激活），保存为 `fake_quant_demo.py`（这是**示例代码**，不是 AMCT 源码）：

   ```python
   # 示例代码：手动实现文档 L144-L146 的 INT8 伪量化公式
   import torch

   def fake_quant_int8(float_val, scale, offset=0.0):
       # int_val = clip(round(float_val/scale + offset), -128, 127)
       int_val = torch.clamp(torch.round(float_val / scale + offset), -128, 127)
       # 反量化还原成浮点，用于观察误差
       dequant = (int_val - offset) * scale
       return int_val, dequant

   float_val = torch.tensor([0.1, 0.5, 1.0, 2.0, -0.3, 5.0])
   scale = 0.02  # 可表示范围约 [-2.56, 2.54]
   int_val, dequant = fake_quant_int8(float_val, scale)

   print("原始浮点 :", float_val.tolist())
   print("量化整数 :", int_val.tolist())
   print("反量化值 :", dequant.tolist())
   print("误差     :", (float_val - dequant).tolist())
   ```

2. 运行：`python fake_quant_demo.py`（仅依赖 CPU 版 torch，无需 NPU）。
3. 把 `scale` 改成 `0.05`（范围变大、格子变粗）再跑一次，对比误差变化。

**需要观察的现象**：

- `5.0` 这个值会超出 `[-2.56, 2.54]` 的可表示范围，被 `clip` 截断成 `127`，反量化后变成 `2.54`——这就是**饱和误差**。
- 落在范围内的值（如 `0.1, 0.5, 1.0`）误差很小，来自 `round` 的四舍五入——这是**舍入误差**。
- 把 `scale` 调大后，能表示的范围变大了（`5.0` 不再饱和），但小值的舍入误差变大——这就是 **scale 选取的权衡**。

**预期结果**：

- 第一次（`scale=0.02`）：`5.0` 被截断为 `2.54`，误差约 `2.46`；其余值误差在 `0.01` 量级。
- 第二次（`scale=0.05`）：`5.0` 能被表示（`5.0/0.05=100`，未超 127），饱和误差消失，但 `0.1` 这类小值的舍入误差变大。

> 如果无法本地运行，明确标注「待本地验证」——但上述数值可以手算核对：`round(0.1/0.02)=5`，反量化 `5*0.02=0.1`；`round(5.0/0.02)=250`，`clip(250,-128,127)=127`。

#### 4.2.5 小练习与答案

**练习 1**：为什么量化是「不可逆」的？哪两步造成了信息丢失？

> **参考答案**：因为 `round`（四舍五入）和 `clip`（截断）都不可逆。`round` 把连续浮点映射到离散整数，丢失了小数部分；`clip` 把超出范围的值压到边界，丢失了「到底超出多少」的信息。

**练习 2**：per-tensor 和 per-channel 哪个精度通常更好？为什么？

> **参考答案**：per-channel 通常更好。per-tensor 让整层所有数值共用一个 scale，如果不同通道的数值范围差异大，小范围通道会被「浪费」精度；per-channel 给每个通道独立的 scale，能各自适配自己的范围，量化误差更小，代价是要存更多 scale。

**练习 3**：为什么激活量化比权重量化更棘手？

> **参考答案**：权重是静态的，训练完就能离线算准它的范围和量化参数；激活是动态的，随输入变化，范围事先未知，必须靠校准数据去「采样」它的分布来估计量化参数，所以更难、更容易掉精度。

---

### 4.3 PTQ vs QAT：两条量化路线

#### 4.3.1 概念说明

量化根据「要不要重新训练」分成两条路线，文档在 [docs/zh/compression_concepts.md:13](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L13) 一句话点明：

> 「量化根据是否需要重训练，分为训练后量化（PTQ）和量化感知训练（QAT）」

- **PTQ（Post-Training Quantization，训练后量化）**：模型训练完之后才做量化，**不需要重新训练**权重。只需少量校准数据。
- **QAT（Quantization-Aware Training，量化感知训练）**：在**重新训练的过程中**引入量化，让模型「感知」到量化误差并主动适应它，**需要完整训练数据和训练资源**。

#### 4.3.2 核心流程

**PTQ 流程**（[L109-L115](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L109-L115)）：

```
训练好的浮点模型
        │
        ├─ 权重已固定 → 离线直接算出权重量化参数
        │
        └─ 激活范围未知 → 用少量校准数据跑前向
                              │
                              ▼
                   根据中间浮点结果算出激活量化参数
                              │
                              ▼
                     输出量化模型（不再训练）
```

文档对 PTQ 的特点总结得很到位：「简单易用，只需少量校准数据，适用于追求高易用性和缺乏训练资源的场景」（[L111](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L111)）。其权重离线算、数据在线校准的分工见 [L113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L113)。

**QAT 流程**（[L117-L124](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L117-L124)）：

```
训练好的浮点模型
        │
        ▼
在训练图里插入「伪量化」节点（浮点→定点→再回浮点）
        │
        ▼
用完整训练集继续训练 → 模型权重主动适应量化误差
        │
        ▼
     输出量化模型
```

QAT 的关键是**伪量化（fake quantization）**：在前向时模拟量化-反量化的过程，制造出和真实量化一样的误差，让反向传播去调整权重来抵消这些误差（[L119](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L119)）。

**两者取舍**（[L121](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L121)）：

| 维度 | PTQ | QAT |
| --- | --- | --- |
| 精度损失 | 相对大一些 | **更小** |
| 耗时 | **短**（几分钟~几十分钟） | 长（要重新训练） |
| 数据需求 | 少量校准集（几百条） | **完整训练集** |
| 训练资源 | 几乎不需要 | 需要 GPU/NPU 训练环境 |
| 易用性 | **高** | 低 |
| 适用场景 | 缺训练资源、追求易用、模型对量化不敏感 | 模型对量化敏感、PTQ 掉点严重、追求极致精度 |

**还有一个关键配角——校准数据集**（[L136-L138](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L136-L138)）：PTQ 用它来模拟线上数据的分布，从而估计激活量化参数。文档特别强调：校准集**必须有代表性**（推荐用测试集子集），否则算出来的量化参数在全量数据上表现差、掉点多。这正是 AMCT 的 `extract_ptq_data` 阶段要做的事——录制有代表性的校准数据（见 [u1-l4](u1-l4-first-quant-cli.md)）。

#### 4.3.3 源码精读

- PTQ/QAT 的分类原话：
  [docs/zh/compression_concepts.md:13](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L13)
- 训练后量化（PTQ）定义、特点、权重离线/激活校准的原理：
  [docs/zh/compression_concepts.md:109-115](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L109-L115)
- 量化感知训练（QAT）定义、伪量化机制、精度/耗时/数据权衡：
  [docs/zh/compression_concepts.md:117-124](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L117-L124)
- 校准数据集的作用与「代表性」要求：
  [docs/zh/compression_concepts.md:136-138](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L136-L138)

> 承接 [u1-l4](u1-l4-first-quant-cli.md)：AMCT 的 LLM 主流程（`eval → extract_ptq_data → ptq → deploy`）就是一条典型的 **PTQ** 链路——它不需要重新训练模型，`extract_ptq_data` 负责录制校准数据，`ptq` 用这些数据估计/优化量化参数。AMCT 目前**不提供 LLM 的 QAT 流程**。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：通过精读文档，形成自己对 PTQ 与 QAT 适用场景的判断，为后续选算法打基础。

**操作步骤**：

1. 仔细阅读 [docs/zh/compression_concepts.md:109-124](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L109-L124)（「训练后量化」与「量化感知训练」两节）。
2. 写一段 200 字左右的对比说明，回答两个问题：
   - **什么场景该用 PTQ？什么场景必须上 QAT？**
   - 各举一个**典型例子**（例如：把一个 7B 对话模型快速压成 INT4 部署 vs. 把一个对量化极度敏感的模型压到极限精度）。
3. 在你的对比里，至少用到这三个关键词：**校准数据**、**伪量化**、**重训练**。

**需要观察的现象**：在写的过程中，你会发现自己必须权衡「易用性 vs. 精度」「耗时 vs. 数据量」——这正是文档 [L121](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L121) 想表达的核心张力。

**预期结果**：得到一段逻辑清晰的对比。参考要点——PTQ 适合：缺训练资源、要快速上线、模型对量化不敏感、只有少量数据；QAT 适合：PTQ 掉点无法接受、有完整训练集和训练算力、追求极致精度。

#### 4.3.5 小练习与答案

**练习 1**：PTQ 里「权重量化参数」和「激活量化参数」分别是怎么得到的？

> **参考答案**：权重训练完就固定，可以直接根据权重的数值分布**离线**算出量化参数；激活是动态的、范围事先未知，需要用**校准数据集**跑前向、拿到中间浮点结果，再**离线**估算激活量化参数（见 [L113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L113)）。

**练习 2**：QAT 里的「伪量化」为什么能提高最终精度？

> **参考答案**：伪量化在前向时模拟「浮点→定点→浮点」的过程，制造出与真实部署一致的量化误差；由于误差出现在前向计算里，反向传播的梯度会引导权重去**主动适应、抵消**这些误差，使训练后的模型对量化更鲁棒，最终精度比 PTQ 更高。

**练习 3**：如果校准数据集选得不好（比如分布和真实线上数据差很远），PTQ 会出什么问题？

> **参考答案**：算出来的激活量化参数不能代表真实数据分布，部署到线上后量化误差会显著放大，模型掉点严重甚至不可用。文档明确警告：「量化损失大，量化后精度低」（[L138](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L138)）。这就是 AMCT 要专门做 `extract_ptq_data` 来录制有代表性校准数据的原因。

---

### 4.4 AMCT 的量化设计哲学

#### 4.4.1 概念说明

理解了量化原理和 PTQ/QAT，最后来看 AMCT 自己的设计取舍。文档第 9 行有一句很关键的话：

> 「AMCT 将量化和模型转换分开，实现对模型中可量化算子的独立量化，并输出量化后的模型。」
> —— [docs/zh/compression_concepts.md:9](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L9)

这句话包含两个设计决策：

1. **「将量化和模型转换分开」**：AMCT **只负责压缩这一环**，不负责「把浮点模型转成 NPU 可执行格式」——后者是 CANN 那一套（编译器、runtime）的工作。回忆 [u1-l1](u1-l1-project-overview.md) 的三段式链路「浮点模型 → **AMCT 量化** → 昇腾 NPU 低比特推理」，AMCT 只占中间一段。

2. **「对可量化算子的独立量化」**：模型里**不是所有算子都适合量化**。一般只有数据密集的矩阵乘类算子（如 `Linear`/`MatMul`，在 LLM 里就是 `q/k/v/o_proj`、`gate/up/down_proj` 等）才量化；而 LayerNorm、Softmax、激活函数等通常保持浮点。AMCT 对每个可量化算子**单独**决定量化策略（位宽、算法、粒度），而不是一刀切。

#### 4.4.2 核心流程

这两个设计决策落到 AMCT 的实际工作流上：

```
浮点模型
   │
   ▼
AMCT 识别「可量化算子」（如各层 Linear）
   │
   ├── 为每个算子独立选择：位宽(bit_config) + 算法(algos) + 数据类型(quant_dtype)
   │
   └── 用 PTQ 流程估算/优化每个算子的量化参数
   │
   ▼
输出量化后的模型权重（交给 CANN 去做模型转换与部署）
```

这套「算子独立量化」的思想，正是 [u1-l4](u1-l4-first-quant-cli.md) 里那些参数的根源：

- `--quant_target mlp/moe/attn-linear/attn-cache`：选择**量化哪些算子组**（独立选择）。
- `--bit_config`：为不同算子组**独立配置位宽**（如 attention 用 W8A8、mlp 用 W4A8）。
- `--algos`：为权重或激活**独立选算法**。
- `--quant_dtype int/mxfp/hifp`：为算子**独立选数据类型**。

也就是说，「可量化算子独立量化」不是一句空话，而是 AMCT 整个 CLI 参数体系的出发点。

#### 4.4.3 源码精读

- AMCT 的核心设计陈述（量化与转换分离、算子独立量化）：
  [docs/zh/compression_concepts.md:9](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L9)
- AMCT 量化后模型「可以在昇腾 AI 处理器上运行」——印证它只做压缩、不做最终部署转换：
  [docs/zh/compression_concepts.md:9](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L9)

> 说明：本节是概念性总结，具体「哪些算子被识别为可量化」「位宽如何按组配置」的源码实现，会在 [u3-l4（BitPolicy 位宽配置）](u3-l4-bit-policy-config.md) 和 [u5-l3（量化算子挂载 quant_apply）](u5-l3-quant-apply.md) 详细展开。本讲只建立「为什么这么设计」的直觉。

#### 4.4.4 代码实践

**实践目标**：把本讲的抽象概念和 [u1-l4](u1-l4-first-quant-cli.md) 的真实命令对应起来，验证「算子独立量化」是 AMCT 的底层逻辑。

**操作步骤**：

1. 回顾 [u1-l4](u1-l4-first-quant-cli.md) 里 `ptq` 命令的参数 `--quant_target`、`--bit_config`、`--algos`、`--quant_dtype`。
2. 打开 [docs/zh/compression_concepts.md:9](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/compression_concepts.md#L9)。
3. 写一段话（3~5 句）说明：这条 `ptq` 命令是如何体现「对可量化算子的独立量化」的——具体指出哪个参数对应「选算子」、哪个对应「选位宽」、哪个对应「选算法」。
4. 再说明：为什么 AMCT 不需要你提供「模型转换」相关参数？（结合「量化与模型转换分开」。）

**需要观察的现象**：你会发现 [u1-l4](u1-l4-first-quant-cli.md) 的四条命令里**没有任何一条**在做「编译成 NPU 可执行文件」——`deploy` 只是导出量化后的权重和量化配置，真正的模型转换发生在 CANN 侧。

**预期结果**：得到一段说明，要点是——`--quant_target` 选算子组、`--bit_config` 选位宽、`--algos` 选算法、`--quant_dtype` 选数据类型，四者共同实现「每个可量化算子独立量化」；而 AMCT 不管模型转换，那是 CANN 的事。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AMCT 要「把量化和模型转换分开」，而不是做成一条龙？

> **参考答案**：职责分离让 AMCT 专注于压缩算法本身，模型转换（编译成 NPU 可执行格式）交给 CANN 这套专门的部署软件栈。这样 AMCT 可以快速迭代量化算法，而不被底层编译器绑死；同时也复用了 CANN 既有的部署能力。对应 [u1-l1](u1-l1-project-overview.md) 的三段式链路，AMCT 只占中间一段。

**练习 2**：「对可量化算子的独立量化」里，为什么不一刀切把所有算子都量化？

> **参考答案**：因为不是所有算子都适合量化。矩阵乘类算子（Linear/MatMul）参数和计算量大、量化收益高、且对量化相对鲁棒，适合量化；而 LayerNorm、Softmax、激活函数等算子对数值精度敏感或计算量小，量化收益低、风险高，通常保持浮点。一刀切会带来不必要的精度损失。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**概念导图任务**：

1. 在一张纸上（或一个 Markdown 文件）画出 AMCT 的压缩全景：
   - 顶层写「模型压缩」，分支列出**量化 / 稀疏 / 蒸馏 / 张量分解 / 算子融合**五种方法，各用一句话注明原理。
   - 在「量化」分支下，再分出 **PTQ** 和 **QAT** 两条路线，标注各自的数据需求（少量校准 vs 完整训练集）、是否重训练、精度/耗时取舍。
2. 在导图的 PTQ 路线旁，标注 AMCT 的四条命令 `eval → extract_ptq_data → ptq → deploy` 分别对应 PTQ 流程的哪个环节（提示：`extract_ptq_data` 对应「校准数据录制」，`ptq` 对应「量化参数估算/优化」）。
3. 在导图角落写上 AMCT 的两条设计原则：「量化与模型转换分离」「可量化算子独立量化」，并各配一个你自己的解释。

完成这张导图后，你应该能不看任何资料，向别人讲清楚：AMCT 是干什么的、量化是怎么把模型变小的、PTQ 为什么不用重训练、AMCT 为什么只压缩不转换。如果哪个环节讲卡壳了，就回到对应模块重读。

> 这个任务无需运行代码，是纯概念梳理。它的价值在于：**后面读源码时，这张导图就是你的「术语锚点」**——看到 `calib` 就想到校准，看到 `bit_config` 就想到算子独立量化，看到 `ptq` 命令就想到 PTQ 流程。

## 6. 本讲小结

- **模型压缩**的目标是减小体积、降低时延、提升推理效率；方法家族包括量化、稀疏、蒸馏、张量分解、算子融合，AMCT 的重心在**量化**。
- **量化**的本质是建立「浮点区间 → 低比特整数集合」的映射，核心公式是 `int_val = clip(round(float_val/scale + offset), -128, 127)`，误差来自 `round`（舍入）和 `clip`（饱和）。
- **量化粒度**（per-tensor/per-channel/per-group/per-token）决定多少个数值共享一组 scale；粒度越细精度越好但参数越多。
- **权重**静态可离线量化，**激活**动态需校准——这是 PTQ 流程设计的根本原因。
- **PTQ** 不重训练、只需少量校准数据、易用但精度稍逊；**QAT** 要重训练、需完整训练集、精度更好但耗时耗资源。AMCT 的 LLM 主流程是 **PTQ**。
- **AMCT 的设计**：把量化和模型转换分开（只做压缩，转换交给 CANN），对可量化算子独立量化（每个算子单独选位宽/算法/数据类型）——这套思想是 [u1-l4](u1-l4-first-quant-cli.md) 全部 CLI 参数的出发点。

## 7. 下一步学习建议

本讲建立了量化的**概念地基**，接下来可以分两个方向继续：

- **横向扩展概念**：[u2-l2 量化数据类型全览](u2-l2-quant-dtypes-overview.md) 会细讲 INT8/INT4/MXFP8/MXFP4/HiFloat8 这些数据类型各自长什么样、适用什么场景；[u2-l3 PTQ 算法选型矩阵](u2-l3-algorithm-selection.md) 会讲 AWQ/GPTQ/SmoothQuant 等算法怎么选。建议先学这两篇，把概念层补全。
- **纵向进入源码**：如果想直接看 AMCT 怎么把量化落地，可以跳到 [u3 单元（LLM 量化工程主链路）](u3-l1-cli-args.md)，从 CLI 参数体系和 Workflow 编排骨架开始读。

建议的学习顺序：**u2-l2 → u2-l3 → u3-l1**。先把数据类型和算法选型这两个概念补齐（否则读源码时会反复卡在术语上），再进入工程主链路。本讲提到的「可量化算子独立量化」会在 [u3-l4（BitPolicy）](u3-l4-bit-policy-config.md) 和 [u5-l3（quant_apply）](u5-l3-quant-apply.md) 看到真正的代码实现，到时候你会对这句话有完全不同的理解深度。
