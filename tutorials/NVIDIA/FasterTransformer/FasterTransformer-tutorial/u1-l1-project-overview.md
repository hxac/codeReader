# FasterTransformer 项目总览：它是什么、解决什么问题

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是带你建立对 FasterTransformer（以下简称 FT）的**全局认知**。读完本讲，你应当能够：

- 用一句话说清楚 FasterTransformer 是什么、解决什么问题、为什么需要它。
- 看懂 README 中的 **Support matrix（支持矩阵）**，知道某个模型在某个框架下支持哪些精度（FP16/INT8/FP8）和哪些并行方式（Tensor parallel / Pipeline parallel）。
- 了解项目的演进历史（Changelog）与已知限制（Known issues），知道它现在处于什么状态。

本讲**不涉及任何编译或运行命令**，全是「读文档」型实践。真正的动手编译放在第 2 讲（构建系统）和第 4 讲（第一个示例）。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先有个直觉：

- **Transformer**：目前自然语言处理（NLP）和视觉领域最流行的神经网络结构，核心是「自注意力机制（self-attention）」。BERT、GPT、T5 都是基于它的变体。
- **Encoder / Decoder（编码器 / 解码器）**：Transformer 有两个主要部件。Encoder 把输入序列编码成一堆向量；Decoder 则负责逐步「生成」输出序列。BERT 是纯 Encoder；GPT 是纯 Decoder；T5/BART 是 Encoder-Decoder。
- **推理（inference）**：模型训练好之后，拿它去预测/生成结果的过程。FasterTransformer **只做推理，不做训练**。
- **FP16 / INT8 / FP8**：数据精度。FP16 是半精度浮点；INT8 是 8 位整数（更省显存、更快，但有精度损失）；FP8 是 8 位浮点（Hopper 架构以后的新精度）。精度越低，通常越快越省，但需要量化（quantization）处理。
- **Tensor parallel（张量并行，TP）/ Pipeline parallel（流水并行，PP）**：把一个超大模型拆到多张 GPU 上跑的两种方式。TP 是「把每一层的矩阵按列/行切开，分给多卡同时算」；PP 是「把不同层分给不同卡，像流水线一样接力」。第 7 单元会专门讲。
- **CUDA / cuBLAS / cuBLASLt**：NVIDIA GPU 上的底层计算库。CUDA 是通用并行计算平台；cuBLAS 提供矩阵乘（GEMM）；cuBLASLt 是它的轻量升级版。FasterTransformer 直接构建在这些库之上。

> 一句话先验：**FasterTransformer 的本质，是用 CUDA/cuBLAS 把 Transformer 的推理过程「手写」到极致，从而比框架自带的实现快几倍。**

## 3. 本讲源码地图

本讲只涉及两份「文档型源码」，它们是项目最重要的入口说明：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目主说明。包含 Model overview（定位）、Support matrix（支持矩阵）、目录结构、Global Environment（环境变量）、Performance（性能）、Changelog（更新历史）、Known issues（已知问题）。 |
| `docs/QAList.md` | 常见问题清单（Questions and Answers）。用问答形式补充了目标用户、支持模型/框架/GPU、如何加载模型等关键背景。 |

后续每一讲的「源码地图」都会引用真实代码文件；本讲因为是从零建立认知，所以从这两份总览文档读起。

## 4. 核心概念与源码讲解

### 4.1 FasterTransformer 是什么：定位与优化目标

#### 4.1.1 概念说明

FasterTransformer 是 NVIDIA 维护的一个 **Transformer 推理加速库**。它的核心动机是：

> 框架（TensorFlow / PyTorch）自带的 Transformer 实现，在推理时往往不是最优的——比如注意力计算里有很多中间显存读写、padding（填充）带来大量无效计算、没有为 Tensor Core 做针对性融合。FasterTransformer 用 CUDA/cuBLAS/cuBLASLt 把这些瓶颈逐个优化掉，换取**几倍的速度提升**。

它的几个关键定位词：

- **只做推理**：不训练，所以可以放手做很多训练时做不了的激进优化（比如把多个小算子融合成一个 kernel）。
- **C++ 为底座**：所有源码都是 C++/CUDA，框架集成只是外层封装。
- **多框架集成**：提供 TensorFlow OP、PyTorch OP、Triton backend、TensorRT plugin 等多种「外壳」，方便嵌进不同技术栈。
- **由 NVIDIA 测试和维护**：性能和正确性有官方背书。

> ⚠️ 项目当前状态：README 顶部明确写着，FT 的开发已经迁移到 **TensorRT-LLM**，本仓库保留但不再更新。这很重要，决定了我们学习它的目的是「理解推理加速的思想与实现」。

#### 4.1.2 核心流程（如何理解它的优化目标）

可以用一条「问题 → 对策」的链路来理解 FT 的优化目标：

1. **朴素 Transformer 推理慢在哪？** → 注意力的 `QKᵀ → softmax → PV` 如果逐算子写，会产生多次显存读写；序列不等长时 padding 浪费算力；小算子太多导致 kernel launch 开销大。
2. **FT 的对策：**
   - 用 **融合 kernel**（fused kernel）把多步合一，减少显存读写；
   - 用 **Effective FasterTransformer** 去除 padding（remove padding），只算有效 token；
   - 用 **Tensor Core** 友好的 FP16/INT8/FP8 矩阵乘；
   - 用 **多 GPU 张量/流水并行** 把超大模型拆开。

这些对策的量化结果，README 的 Performance 章节里有具体数字（例如 BERT 在 T4 上相比 PyTorch TorchScript 可达 4x~6x 加速）。

#### 4.1.3 源码精读

**项目的一句话定位**，在 README 开篇 Model overview：

[README.md:L28-L32](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L28-L32) —— 这段说明 FT 实现了一个「高度优化的 transformer layer」，覆盖 encoder 和 decoder，在 Volta/Turing/Ampere GPU 上当数据和权重为 FP16 时会自动启用 Tensor Core。

紧接着这句点明了技术底座与多框架集成方式：

[README.md:L32-L32](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L32-L32) —— 「FasterTransformer is built on top of CUDA, cuBLAS, cuBLASLt and C++」，并会为 TensorFlow / PyTorch / Triton backend 至少提供一种 API。

项目当前「不再更新、迁移到 TensorRT-LLM」的状态声明，在 README 最顶部：

[README.md:L1-L1](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L1-L1) —— 这条 Note 是学习本仓库前必须知道的前提。

QAList.md 的第 1 个问题，用官方口吻再次总结了定位与对标对象（对标 TensorRT demo BERT，性能仅略慢，但更灵活、还支持翻译和 GPT-2）：

[docs/QAList.md:L1-L5](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/QAList.md#L1-L5) —— FT 的目标用户是「需要高效 transformer 推理又需要灵活性」的人。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用自己的话复述 FT 的定位，而不是背术语。
2. **操作步骤**：打开 `README.md` 的 Model overview 段落（L28-L32），再读 `docs/QAList.md` 的 Q1（L1-L5）和 Q2（L7-L9）。
3. **需要观察的现象**：注意官方如何用「对标对象（TensorRT demo BERT）+ 差异点（更灵活、支持翻译/GPT-2）」来定义自己。
4. **预期结果**：你能写出一句不超过 40 字的中文定义。参考：「FasterTransformer 是 NVIDIA 用 CUDA/cuBLAS 高度优化的 Transformer 推理库，提供多框架封装，比框架自带实现快数倍。」

#### 4.1.5 小练习与答案

**练习 1**：FT 是训练库还是推理库？为什么不训练反而能做得更快？
**参考答案**：FT 是**推理库**。正因为不做训练，它可以把训练时无法合并的小算子融合成单个 CUDA kernel、删除梯度相关逻辑、激进地复用显存，从而在推理这一特定场景下做到极致速度。

**练习 2**：FT 的底层计算依赖是哪几个库？
**参考答案**：CUDA、cuBLAS、cuBLASLt，外加 C++ 实现。这些是它所有 kernel 和矩阵乘的根基（详见第 2 单元的构建选项）。

---

### 4.2 支持矩阵：模型 × 框架 × 精度 × 并行

#### 4.2.1 概念说明

「支持矩阵（Support matrix）」是 README 里最重要的一张表。它回答一个极其具体的问题：

> 我想用 FT 跑**某个模型**，在**某个框架**下，能不能用**某种精度**，能不能用**多 GPU 并行**？

表的一行 = 一个「模型 + 框架」组合；表的列 = FP16 / INT8 / Sparsity（稀疏）/ Tensor parallel / Pipeline parallel / FP8。单元格里的 `Yes` 表示支持，`-` 表示不支持，`On-going` 表示在做。

理解这张表的关键，是明白「**能力是正交叠加的**」：

- 模型维度：BERT、GPT/OPT、T5、ViT、Swin、BLOOM、GPT-J、GPT-NeoX、BART、Longformer、XLNet、WeNet、DeBERTa……
- 框架维度：C++（所有模型的底层）、TensorFlow、PyTorch、Triton backend、TensorRT。
- 精度维度：FP16（几乎所有模型都支持）、INT8（主要是 BERT/Swin/ViT）、FP8（仅 GPT/BERT，实验性）。
- 并行维度：Tensor parallel、Pipeline parallel（主要面向 GPT 系大模型）。

> 注意 README 表格下方的一句脚注：**所有模型在 C++ 下都可用**，因为全部源码都是 C++ 写的。其它框架只是封装。

#### 4.2.2 核心流程（如何读懂一行支持矩阵）

读表的步骤：

1. **先锁定模型**：例如你关心 GPT。
2. **再锁定框架**：例如 PyTorch。
3. **逐列看能力**：FP16？INT8？TP？PP？FP8？
4. **结合硬件前提**：表头注明了 `INT8 (after Turing)`、`Sparsity (after Ampere)`、`FP8 (after Hopper)`——即这些能力依赖 GPU 架构代际（Turing=6.x/7.x、Ampere=8.x、Hopper=9.x）。

一个判断模式：**面向大语言模型（GPT/BLOOM/GPT-J/GPT-NeoX/T5/BART）的组合，几乎都同时支持 TP + PP**，因为大模型必须切分到多卡；而**面向编码器的小模型（BERT 在 TF/C++、ViT、Swin）通常不标 TP/PP**，因为单卡放得下。

#### 4.2.3 源码精读

支持矩阵的完整表格在这里：

[README.md:L36-L72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L36-L72) —— 表头列含义：Models / Framework / FP16 / INT8 (after Turing) / Sparsity (after Ampere) / Tensor parallel / Pipeline parallel / FP8 (after Hopper)。

举三个有代表性的行，帮助你建立读表直觉：

- **BERT / PyTorch**（[README.md:L39-L39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L39-L39)）：FP16=Yes, INT8=Yes, Sparsity=Yes, TP=Yes, PP=Yes, FP8=-。是少数精度/并行能力最全的组合。
- **GPT/OPT / PyTorch**（[README.md:L50-L50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L50-L50)）：FP16=Yes, TP=Yes, PP=Yes, FP8=Yes（实验性），但 INT8/Sparsity=-。
- **T5/UL2 / Triton backend**（[README.md:L59-L59](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L59-L59)）：FP16=Yes, TP=Yes, PP=Yes，其余=-。

表格下方的「C++ 通用支持」脚注，以及「细节见 `docs/xxx_guide.md`」的指引在这里：

[README.md:L73-L75](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L73-L75) —— 提醒：每个模型的细节文档在 `docs/` 下的 `xxx_guide.md`，而通用问答在 `docs/QAList.md`。

QAList.md 的 Q2、Q3 进一步回答了「支持哪些模型/框架」：

[docs/QAList.md:L7-L13](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/QAList.md#L7-L13) —— FT 本质上提供「高度优化的 transformer block」，需要高效 transformer 的场景都能受益；框架上提供 C API + TensorFlow/PyTorch OP，其它框架可自行封装 C++。

#### 4.2.4 代码实践（源码阅读型，即本讲主实践）

1. **实践目标**：把 README 支持矩阵中 BERT、GPT、T5 三个模型在 **PyTorch** 和 **Triton backend** 下的能力整理成一张表，并写出结论。
2. **操作步骤**：打开支持矩阵（[README.md:L36-L72](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L36-L72)），定位 BERT、GPT/OPT、T5/UL2 在 PyTorch 与 Triton 两列的 6 行，逐列抄录。
3. **需要观察的现象**：注意哪些列三个模型都一样（FP16、TP、PP），哪些列只有个别模型是 `Yes`（INT8、FP8、Sparsity）。
4. **预期结果**：得到下面这张表（参考答案）。

| 模型 | 框架 | FP16 | INT8 | Sparsity | Tensor parallel | Pipeline parallel | FP8 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BERT | PyTorch | Yes | Yes | Yes | Yes | Yes | - |
| BERT | Triton backend | Yes | - | - | Yes | Yes | - |
| GPT/OPT | PyTorch | Yes | - | - | Yes | Yes | Yes |
| GPT/OPT | Triton backend | Yes | - | - | Yes | Yes | - |
| T5/UL2 | PyTorch | Yes | - | - | Yes | Yes | - |
| T5/UL2 | Triton backend | Yes | - | - | Yes | Yes | - |

**结论**：

- **三个模型在 PyTorch 和 Triton 下都支持 FP16 + Tensor parallel + Pipeline parallel**——这是 FT 大模型推理的「标配能力」。
- **INT8 是 BERT 在 PyTorch 下的独有能力**（GPT/T5 在这两个框架下均不支持 INT8）。
- **FP8 是 GPT/OPT 在 PyTorch 下的实验性独有能力**（也是全表少数标 Yes 的 FP8 行之一，另一条是 BERT/C++）。
- **Sparsity（稀疏）同样只有 BERT/PyTorch 支持**。
- 也就是说：**精度/稀疏优化（INT8/FP8/Sparsity）目前只覆盖个别模型，而并行能力（TP/PP）已覆盖几乎所有大模型组合。**

5. **运行结果说明**：本实践为纯文档阅读，不需要运行命令；表格已由支持矩阵直接给出，**待本地验证**仅指「你亲手对照 README 复核一遍」。

#### 4.2.5 小练习与答案

**练习 1**：我想在 Triton backend 上跑 GPT 并启用 FP8，行不行？
**参考答案**：不行。支持矩阵 [README.md:L51-L51](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L51-L51) 显示 GPT/OPT 在 Triton backend 下 FP8 为 `-`。FP8 目前只在 GPT/OPT 的 **PyTorch** 与 BERT 的 **C++** 组合下（实验性）支持。

**练习 2**：为什么 BERT 在 TF 下没有 Tensor parallel，但在 PyTorch 下有？
**参考答案**：TF 集成主要面向单卡/小规模部署的编码器场景，而 PyTorch 集成面向需要多卡切分的大模型场景，所以 TP/PP 能力主要补在 PyTorch（和 Triton）侧。这也体现了「能力按需分配」，不是所有框架都做全功能镜像。

---

### 4.3 项目演进历史与已知限制

#### 4.3.1 概念说明

了解一个项目「从哪来、到哪去、有哪些坑」，能帮你判断它适不适合你的场景。FT 的演进有几个里程碑：

- **1.0（2019.7）**：高度优化的 BERT 等价 transformer 层，含 C++ API、TF OP、TensorRT plugin。
- **3.0（2020.9）**：支持 GPT-2、INT8 量化。
- **4.0（2021.4）**：支持 **多 GPU、多节点** 的 GPT 推理（C++ + PyTorch），这是走向大模型的关键一步。
- **5.0（2022.4）**：大规模重构，默认 GEMM 累加类型改为 FP32，支持 bfloat16、ViT。
- **5.1 / 5.2（2022.8 / 2022.12）**：交互式生成、流式生成、shared context、min length penalty。
- **2023.1**：支持 GPT MoE、**实验性 FP8**（Bert 和 GPT）、DeBERTa。
- **2023.5**：修复生成 early stopping 的 bug（这是仓库目前最后的更新方向之一）。
- **当前**：开发迁移到 TensorRT-LLM，本仓库冻结。

「已知限制（Known issues）」是踩坑预警，**强烈建议在动手编译前先读**。

#### 4.3.2 核心流程（如何用历史与限制指导决策）

1. **判断功能是否成熟**：Changelog 里标 `Experimental` 或 `preview` 的功能（如 FP8、w8a8 int8 for GPT）不要在生产环境直接依赖。
2. **判断硬件前提**：INT8 需要 Turing 及以后、Sparsity 需要 Ampere 及以后、FP8 需要 Hopper 及以后；custom all-reduce 仅在 DGX-A100、TP=8、且 CUDA 支持 cudaMallocAsync 时可用。
3. **规避已知坑**：Known issues 列出的编译/导入/数值差异问题，遇到时直接对照排查。
4. **判断项目生命力**：已迁移到 TensorRT-LLM，所以新项目应评估是否直接用 TensorRT-LLM；学习 FT 的价值在于理解推理加速的工程思想。

#### 4.3.3 源码精读

**Changelog（更新历史）** 的开头几条，能看到最近的里程碑与 FP8 实验性支持：

[README.md:L216-L238](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L216-L238) —— 例如「January 2023 - Support GPT MoE / Support FP8 for Bert and GPT (Experimental) / Support DeBERTa」。

**Known issues（已知问题）** 全部内容，是编译与运行前必看的踩坑清单：

[README.md:L413-L419](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L413-L419) —— 涵盖：TF 2.10 无法编译（undefined symbol）、导入扩展时的 undefined symbol（需先 `import torch`，可能是 C++ ABI 不兼容）、TF 与 OP 在 decoding 时结果可能不同（累积对数概率导致）、TF OP 建议用 gcc/g++ 4.8（尤其 TF 1.14）。

**目录结构** 总览（虽然会在第 3 讲详讲，但此处先建立直觉）：

[README.md:L79-L101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L79-L101) —— 说明 `src/fastertransformer` 下 `kernels/layers/models/utils` 以及 `th_op/tf_op/triton_backend/tensorrt_plugin` 各自的职责。

**QAList.md 的硬件与加载相关问答**，补充了 GPU 门槛和模型加载方式：

[docs/QAList.md:L21-L23](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/QAList.md#L21-L23) —— 官方验证过 Compute Capability >= 7.0 的 GPU（V100、T4、A100）。

[docs/QAList.md:L33-L39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/QAList.md#L33-L39) —— 模型加载方式：C++ 下用户自行加载并拷贝到 GPU；TF/PyTorch 下可直接把 checkpoint 的权重张量喂给 FT；多 GPU GPT 特殊，需先用工具转换 OpenAI/Megatron checkpoint。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：从 Changelog 与 Known issues 中提炼「与我相关的 3 条信息」。
2. **操作步骤**：
   - 读 Changelog（[README.md:L216-L338](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L216-L338)），找出你想用的功能首次出现的版本，以及是否标了 Experimental/preview。
   - 读 Known issues（[README.md:L413-L419](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L413-L419)），判断是否会卡在你的环境上。
3. **需要观察的现象**：注意哪些功能带 `Experimental` / `preview` 字样，这些是「能用但不保证稳定」的信号。
4. **预期结果**：写出 3 条与己相关的笔记。例如：① FP8 是 Experimental，生产慎用；② 我用 TF 2.10 会无法编译；③ custom all-reduce 仅限 DGX-A100 + TP=8。
5. **运行结果说明**：纯阅读，无需运行；具体编译命令在第 2 讲给出。

#### 4.3.5 小练习与答案

**练习 1**：FT 从哪个版本开始支持多 GPU、多节点的 GPT 推理？
**参考答案**：4.0（2021 年 4 月）。见 [README.md:L330-L338](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L330-L338)，其中明确「Support multi-gpus and multi-nodes inference for GPT model on C++ and PyTorch」。这是 FT 走向大模型推理的分水岭。

**练习 2**：导入 PyTorch 扩展时遇到 `undefined symbol`，README 给的第一条排查建议是什么？
**参考答案**：先 `import torch`。如果已导入仍报错，则可能是 C++ ABI 不兼容——需确认编译期与运行期使用的 PyTorch 一致，或检查 PyTorch 编译方式与 GCC 版本。见 [README.md:L416-L417](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L416-L417)。

**练习 3**：官方验证支持的最低 GPU 架构是什么？
**参考答案**：Compute Capability >= 7.0，即 V100 / T4 / A100 这一代起。见 [docs/QAList.md:L21-L23](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/QAList.md#L21-L23)。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「项目认知卡片」小任务：

> 假设你要向团队评估「能否用 FasterTransformer 在 **8 张 A100 上、PyTorch 框架下** 跑一个 **GPT 大模型**，要求 **FP16 + 张量并行**，未来可能想试 **FP8**」。请基于本讲内容产出一份不超过 200 字的评估备忘。

要求你的备忘必须包含：

1. **可行性结论**：从支持矩阵（4.2）判断 FP16 + TP 是否可行、FP8 是否可行。
2. **硬件前提**：从 QAList 与表头脚注（4.2.2、4.3.3）说明 GPU 架构要求。
3. **风险提示**：从 Changelog（4.3）指出 FP8 的成熟度风险。
4. **下一步**：指出后续应读哪份 guide（提示：`docs/gpt_guide.md`）。

**参考要点**：

- 可行性：GPT/OPT 在 PyTorch 下 FP16=Yes、Tensor parallel=Yes、FP8=Yes（[README.md:L50-L50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L50-L50)），需求成立。
- 硬件：A100（CC 8.0）满足 `>= 7.0`；但 FP8 表头标注 `after Hopper`，A100 上 FP8 实际不可用——这是容易踩的坑，需换 H100 或放弃 FP8。
- 风险：FP8 在 Changelog 中标为 **Experimental**（[README.md:L223-L224](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L223-L224)），生产环境需谨慎。
- 下一步：阅读 `docs/gpt_guide.md` 了解 GPT 模型结构与构建命令（这也是第 6 单元的主题）。

## 6. 本讲小结

- FasterTransformer 是 NVIDIA 维护的 **Transformer 推理加速库**，基于 CUDA/cuBLAS/cuBLASLt 与 C++，**只做推理不做训练**，靠融合 kernel、去 padding、Tensor Core、多 GPU 并行换取数倍加速。
- **支持矩阵**是判断「模型 × 框架 × 精度 × 并行」能力的核心工具：FP16 + TP + PP 是大模型的标配；INT8/FP8/Sparsity 只覆盖少数模型，且有硬件代际门槛（Turing/Ampere/Hopper）。
- 所有模型在 **C++ 下都可用**，TensorFlow/PyTorch/Triton/TensorRT 只是封装外壳；细节文档在 `docs/xxx_guide.md`，通用问答在 `docs/QAList.md`。
- 官方验证的最低 GPU 架构是 **Compute Capability >= 7.0**（V100/T4/A100）。
- 项目里程碑：4.0 起支持多 GPU/多节点 GPT；5.0 大重构；2023 年加入实验性 **FP8** 和 **MoE**。
- **重要现状**：FT 开发已迁移到 **TensorRT-LLM**，本仓库冻结；学习 FT 的价值在于掌握推理加速的思想与实现，后续新项目可评估直接用 TensorRT-LLM。

## 7. 下一步学习建议

本讲建立了全局认知，但还没碰任何代码与编译。建议接下来按顺序：

1. **第 2 讲（u1-l2 构建系统）**：学习顶层 `CMakeLists.txt` 的 option 体系（`BUILD_PYT`、`BUILD_TF`、`BUILD_TRT`、`BUILD_MULTI_GPU`、`ENABLE_FP8`、`SPARSITY_SUPPORT`），动手写出第一条 cmake 命令。
2. **第 3 讲（u1-l3 目录结构）**：结合本讲引用的 [README.md:L79-L101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/README.md#L79-L101)，深入 `kernels/layers/models/utils` 的职责划分。
3. **第 4 讲（u1-l4 第一个示例）**：真正编译并运行 BERT / GPT 的 C++ example。
4. 如果你想先看某个具体模型怎么做，可以直接跳到 `docs/bert_guide.md` 或 `docs/gpt_guide.md`（不过建议先完成第 2~4 讲，建立运行能力后再读模型细节）。
