# 硬件适配与量化资料

> 本讲面向已经读过 u2-l5（大规模 EP 与 PD 分离）的进阶读者。
> 本仓库不含 SGLang 运行时源码，本讲所谓的「源码」指的是仓库内可阅读的 README 索引、AMD 主题的幻灯片（PDF）与博客链接。
> 涉及具体幻灯片内部页码、基准数字时，标注「待打开 PDF 确认」，绝不编造。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 SGLang 为什么要在 AMD GPU（尤其是 MI300X）上做专门适配，以及它走过怎样一条时间线。
- 解释 **fp8** 与 **mxfp（Microscaling FP）** 两种量化方案的区别，并能估算它们给 DeepSeek 这种大模型省下多少显存。
- 认识 **AITER**（AMD 的推理内核库），知道它在 AMD 版 SGLang 里扮演的角色，并对 **MoRI** 形成可继续查证的初步认识。
- 在 README 里快速定位「AMD 硬件适配 + 量化」这一资料簇，区分仓库内 PDF 与外链博客。

## 2. 前置知识

本讲不涉及运行时代码，但需要几个推理背景概念。先用大白话过一遍。

### 2.1 什么是量化（Quantization）

大模型的权重默认以 **fp16 / bf16**（每个数 16 位）存储。**量化**就是用更少的位数（8 位、4 位）来表示这些数，好处有三：

1. **省显存**：位数减半，权重体积减半。
2. **省带宽**：decode 阶段是访存密集型，搬的数据越少越快。
3. **用上专用算力**：现代 GPU（含 AMD MI300X）有低精度矩阵乘硬件单元，位数越低吞吐越高。

代价是精度损失，需要靠「选对格式 + 选对缩放粒度」来控制。

### 2.2 浮点数的三段式

一个浮点数由三部分组成：符号位（sign）、指数位（exponent，决定范围）、尾数位（mantissa，决定精度）。位数怎么分配，决定了这个格式的「能表示多大」和「能表示多准」。

### 2.3 AMD GPU 与 ROCm

- **MI300X**：AMD 的高端 AI 加速卡，单卡 **192 GB HBM3** 显存，架构代号 CDNA3。显存大是它的卖点，适合放下 DeepSeek 这种超大 MoE 模型。
- **ROCm**：AMD 的 GPU 软件栈，地位类似 NVIDIA 的 CUDA。SGLang 要跑在 AMD 上，就要对接 ROCm。
- **Triton**：一种高层 kernel 编写语言，可同时编译到 NVIDIA CUDA 与 AMD ROCm，是跨硬件写高性能算子的关键工具。

### 2.4 与前面讲义的衔接

- u2-l4 讲了 DeepSeek MLA 与「模型优化」——**量化正是模型优化的另一条主线**。
- u2-l5 讲了大规模 EP 与 PD 分离——那里强调「把模型拆到多卡」。本讲会看到：**正是因为量化让单卡能塞下更多权重，多卡部署才更从容**，二者是一套组合拳。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 类型 | 在本讲的作用 |
| --- | --- | --- |
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | 导航索引（可读） | AMD 资料的总入口：Announcement、AMD Meetup 幻灯片、AMD 博客三大区段 |
| [slides/sglang-fp8-mxfp-quantizations.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang-fp8-mxfp-quantizations.pdf) | 仓库内 PDF（2024-11-02） | 核心量化幻灯片，主题「Quantization on AMD」 |
| [slides/amd_meetup_aiter_mori.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/amd_meetup_aiter_mori.pdf) | 仓库内 PDF（2025-08-22） | AITER / MoRI 介绍幻灯片 |
| [slides/amd_meetup_sglang_roadmap.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/amd_meetup_sglang_roadmap.pdf) | 仓库内 PDF（2025-08-22） | SGLang on AMD 路线图 |
| [slides/amd_meetup_sglang_ep.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/amd_meetup_sglang_ep.pdf) | 仓库内 PDF（2025-08-22） | 关联资料：AMD 上的大规模部署（承接 u2-l5） |

> 阅读提示：本仓库的 PDF 是讲义式幻灯片，本讲无法逐页解析其图像内容；凡涉及某张幻灯片的具体结论，都会请你「打开 PDF 确认」。README 的文字内容则可精确到行号引用。

## 4. 核心概念与源码讲解

### 4.1 AMD 平台适配与 roadmap

#### 4.1.1 概念说明

SGLang 最初主要在 NVIDIA GPU 上打磨，但 DeepSeek V3/R1 爆发后，**AMD MI300X 凭借 192 GB 大显存成为部署这些超大 MoE 模型的主力硬件之一**。要让 SGLang 在 AMD 上也跑得快，不能只做「能跑」的移植，而要：

- 把关键算子换成 **AMD 专用高性能内核**（见 4.3 AITER）。
- 用 **量化** 压住显存与带宽压力（见 4.2）。
- 跟上 AMD ROCm 的版本节奏（如 README 提到的「AMD nightly image」）。

也就是说，「硬件适配」不是一次性移植，而是一条持续迭代的 **roadmap**。

#### 4.1.2 核心流程：AMD 适配时间线

从 README 能还原出一条清晰的 AMD 适配时间线：

```text
2024-10  AMD Advancing AI 大会，首发「在 AMD 上高效推理」分享
2024-11  biweekly：Quantization on AMD（fp8/mxfp）正式进入议程
2024-11  ROCm 博客：SGLang 作为 AMD GPU 上的快速服务框架
2024-12  被宣布为「AMD 主导的 LLM 引擎」（dominant engine）
2025-01  多篇 DeepSeek-V3/R1 on MI300X 博客密集发布
2025-03  加入 PyTorch 生态，在 AMD nightly image 上取得 SOTA
2025-08  AMD SGLang Meetup：roadmap + EP 部署 + AITER/MoRI
```

这条线的规律是：**先有量化与框架级适配，再有大规模 DeepSeek 部署的成果发布，最后形成路线图与专用内核库**。

#### 4.1.3 源码精读

下面三段 README 文字，分别对应「采纳背书」「生态里程碑」「博客成果」，是理解 AMD 适配的三个锚点。

**① 2024 年 12 月的采纳背书——AMD 把 SGLang 作为主导引擎：**

[README.md:26-30](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L26-L30) 这段说明 SGLang 被采纳为「AMD 的 dominant LLM engine」与「xAI 的 default engine」，并指向 AMD ROCm 6.3 官方公告。它解释了为什么后续 AMD 投入资源做适配：因为已经战略性地选定了 SGLang。

**② 2025 年 3 月的里程碑——加入 PyTorch 生态、AMD 上 SOTA：**

[README.md:11-17](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L11-L17) 这段是 AMD 适配的关键结论句：「achieved SOTA performance on AMD nightly image」，并给出两篇配套博客（PyTorch 生态、DeepSeek-R1 on MI300X）。注意「nightly image」——它强调适配是跟着 ROCm 每日构建持续验证的。

**③ AMD 博客区段——四篇 DeepSeek on MI300X 实战博客：**

[README.md:128-136](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L128-L136) 这是 README 里最集中的 AMD 资料簇：从 2024-11 的「AMD GPU 上的快速服务框架」，到 2025-01/02/03 三篇围绕 DeepSeek-R1/V1 在 MI300X 上推理性能的博客。它们是验证「量化 + AITER 是否真的带来加速」的权威出处。

#### 4.1.4 代码实践：用 grep 画出 AMD 资料地图

这是本仓库里**可直接运行**的实践（仓库无运行时代码，但 README 可检索）。

1. **实践目标**：把 README 中所有 AMD 相关的触点一次性捞出来，验证 4.1.2 的时间线。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   grep -ni 'amd\|mi300\|rocm' README.md
   ```
3. **需要观察的现象**：输出会命中 Announcement、AMD SGLang Meetup、AMD 博客、Meta PyTorch、Microsoft Azure 等多个区段，说明 AMD 主题横跨多个 `##` 区段，是一个**资料簇**（参见 u1-l3）。
4. **预期结果**：你会看到命中行号分布在 README 的第 11–17、21、27、30、38–48、128–136、142、146 等处，与本讲列出的锚点一一对应。
5. 结论稳定性：上述行号基于当前 HEAD，若 README 后续追加 AMD 条目，行号会变，请以 `grep` 实时结果为准。

#### 4.1.5 小练习与答案

**练习 1**：README 里 AMD 相关资料同时出现在「Announcement」「Slides」「Blog」三大区段，为什么说它构成一个「资料簇」而不是单条记录？

> **参考答案**：因为同一个主题（AMD 适配）被按不同形态分别记录——里程碑公告放 Announcement、幻灯片放 Slides 下的 AMD Meetup 子区段、深度文章放 Blog 下的 AMD 子区段。按 u1-l3 的方法，应当把它们当成一组整体阅读，而不是只看某一条。

**练习 2**：为什么 README 要特意强调「AMD nightly image」上的 SOTA，而不是说「某个 ROCm 正式版」？

> **参考答案**：适配是持续进行的，nightly image 反映最新 ROCm 构建的最新状态；强调 nightly 说明当时性能仍在快速迭代，尚未固化为某个正式大版本的承诺。

---

### 4.2 fp8 / mxfp 量化

#### 4.2.1 概念说明

本模块是本讲的技术核心。两个词要先分清：

- **fp8**：8 位浮点。把每个权重/激活值从 16 位压到 8 位，是当下推理量化的「主力格式」。
- **mxfp（Microscaling FP，微缩放浮点）**：来自 OCP（开放计算项目）微缩放格式规范。核心是「**块缩放**」——把张量切成每 32 个元素一块，块内共享一个缩放因子，元素本身用更低位数（mxfp4 用 4 位、mxfp8 用 8 位）。

直觉上：fp8 是「**每个数都 8 位**」；mxfp 是「**每 32 个数共用一把尺子，数本身更短**」。后者在精度和压缩之间取得了更好的平衡。

#### 4.2.2 核心流程与原理

**(a) fp8 的两种格式**

业界（OCP）标准定义了两种 fp8：

| 格式 | 指数位 / 尾数位 | 偏置（bias） | 典型用途 |
| --- | --- | --- | --- |
| E4M3 | 4 / 3 | 7 | 权重与前向激活（精度优先） |
| E5M2 | 5 / 2 | 15 | 梯度（范围优先） |

对 SGLang 这种**推理服务**而言，主要用 **E4M3**。一个 E4M3 数的值（忽略特殊值）为：

\[
v = (-1)^{s} \cdot 2^{(e-7)} \cdot \left(1 + \frac{m}{2^{3}}\right)
\]

其中 \(s\) 是符号，\(e\) 是 4 位指数，\(m\) 是 3 位尾数。它的可表示范围约为 ±448。

为什么推理敢用 fp8？因为 **DeepSeek V3/R1 本身就是用 fp8 训练的**（DeepSeek 公开过其 fp8 训练框架），所以推理时用 fp8 几乎不引入额外精度损失——这是「量化 + 模型特性」配合的关键。

**(b) mxfp 的块缩放**

mxfp 把每 32 个元素划为一块，块内共享一个 8 位缩放因子（shared micro-exponent），反量化时：

\[
\hat{x}_i = s_b \cdot \tilde{x}_i, \qquad i \in \text{block } b
\]

其中 \(s_b\) 是第 \(b\) 块的共享缩放，\(\tilde{x}_i\) 是该块内低精度元素。相比「整个张量共用一个缩放」（per-tensor），**按块缩放更贴合数值的实际分布**，因而同样位数下精度更高；相比「每个元素一个缩放」（per-element），开销又小得多（32 个数才摊一个缩放）。

**(c) 量化如何喂饱多卡部署（与 u2-l5 联动）**

DeepSeek V3 是约 671B 参数的 MoE 模型。粗算权重显存（约值）：

| 精度 | 每元素字节 | 671B 权重显存 |
| --- | --- | --- |
| fp16/bf16 | 2 | ~1342 GB |
| fp8 | 1 | ~671 GB |
| mxfp4 | ~0.5（+极小的共享开销） | ~336 GB |

单张 MI300X 只有 192 GB，连 fp8 的 671 GB 都装不下。8 张 MI300X 合计 1536 GB：fp8 可舒服放下并留出 KV cache 空间；mxfp4 则能进一步降低压力、提升访存带宽利用率。**这正是 u2-l5「EP 把专家拆到多卡」的前提之一——量化让拆分后的每卡负载更轻。**

#### 4.2.3 源码精读

README 中量化的入口在这一行（位于 `## Slides → SGLang Biweekly Meeting` 子区段）：

[README.md:102-102](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L102-L102) 这一行记录了 `[2024-11-02] [Quantization on AMD](slides/sglang-fp8-mxfp-quantizations.pdf)`。文件名本身就透露了主题：**fp8 与 mxfp 两种量化**，且明确是 **on AMD**——也就是说，它讲的是「在 AMD 硬件上做这两种量化」的具体实践，而非泛泛的量化理论。

> 待打开 PDF 确认：该幻灯片内部具体比较了哪些模型、哪些精度组合、给出的加速/精度数字是多少。本讲不编造这些页内数字。

量化主题还和 README 的 PyTorch 生态区段联动（量化工具链）：

[README.md:142-142](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L142-L142) 这里指向「Accelerating LLM Inference with GemLite, TorchAO and SGLang」博客。**TorchAO**（PyTorch 架构优化）与 **GemLite**（低比特线性层）正是 PyTorch 生态里的量化/低比特 GEMM 工具，与 fp8/mxfp 同属一条技术线。

#### 4.2.4 代码实践：手算量化带来的显存收益

1. **实践目标**：用一张表算清楚 fp16 → fp8 → mxfp4 各自能给 DeepSeek V3 省多少显存，并判断「几张 MI300X 才放得下」。
2. **操作步骤**：
   - 取 DeepSeek V3 总参数量约 671B。
   - 按 4.2.2(c) 的表，分别算 fp16、fp8、mxfp4 的权重显存（参数量 × 每元素字节）。
   - 用结果除以单卡 192 GB，算「最少需要几张 MI300X」。
3. **需要观察的现象**：fp8 比 fp16 显存减半；mxfp4 再减半；所需卡数随之下降。
4. **预期结果**：fp16 ≈ 1342 GB → 至少 8 张（8×192=1536）；fp8 ≈ 671 GB → 4 张起（4×192=768），但为留 KV cache 余量实战常用 8 张；mxfp4 ≈ 336 GB → 2 张理论可放，但 MoE 还需考虑激活与通信缓冲。
5. 结论：**量化直接决定了「同一模型需要几张卡」**，这是它与 u2-l5 大规模部署强耦合的原因。
6. 待本地验证：上述为按参数量推算的「权重显存」下限，未含激活、KV cache、通信缓冲；真实部署显存以实测为准。

#### 4.2.5 小练习与答案

**练习 1**：同样降到「每元素 8 位」，fp8 与 mxfp8 的区别是什么？

> **参考答案**：fp8 是每个元素独立 8 位浮点；mxfp8 的元素也是 8 位，但它额外把每 32 个元素组成一块、共享一个缩放因子，相当于在 8 位之外又加了「按块自适应量程」，精度通常更好。

**练习 2**：为什么 DeepSeek 在 SGLang 上用 fp8 推理的精度损失很小？

> **参考答案**：因为 DeepSeek V3/R1 训练阶段就采用了 fp8，权重「天生」就是 fp8 量程下的产物；推理再按 fp8 表示，基本是回到它原本的数值分布，所以额外损失小。

**练习 3**：从 README 的 `[2024-11-02] [Quantization on AMD]` 这一行，能推断 SGLang 量化工作的哪两个特点？

> **参考答案**：一是时间早（2024-11 就进入 biweekly 议程），说明量化是长期主线而非临时工作；二是明确标 «on AMD»，说明量化是和 AMD 硬件适配**绑定推进**的。

---

### 4.3 AITER / MoRI 介绍

#### 4.3.1 概念说明

光有量化还不够。模型在 GPU 上跑得快不快，最终落在**算子（kernel）**上——注意力、MoE 分组矩阵乘、归一化、激活融合，每一项都需要为特定硬件定制的高性能实现。SGLang 在 NVIDIA 上大量使用 FlashInfer、Triton 等内核；到了 AMD 上，对应的角色就是 **AITER**。

**AITER**（AMD Instinct Triton-based Engine for ROCm）是 AMD 面向 MI300X 的开源高性能推理内核库：用 Triton 编写、编译到 ROCm，提供 DeepSeek 这类 MoE 模型推理所需的注意力、分组 GEMM、融合算子等。**它在 AMD 版 SGLang 里的地位，类似 FlashInfer 在 NVIDIA 版里的地位**——是「跑得快」的底层来源。

**MoRI** 则是在同一场 AMD Meetup 中与 AITER 一起被介绍的组件（README 标题为「AITER/MoRI Introduction」）。它与 AMD 上 DeepSeek/MoE 推理内核栈相关；其确切全称与边界**需打开对应 PDF 确认**，本讲不臆造。

> 关键认知：本模块的核心结论是「**AMD 版 SGLang 依赖一套专用内核库（以 AITER 为代表）来追平 NVIDIA 上的性能**」，这一点 README 的 SOTA 表述已侧面证实（见 4.1.3 ②）。MoRI 的具体定位请在实践中查证。

#### 4.3.2 核心流程：跨硬件内核栈的对照

理解 AITER 最有效的方式，是把它放进「**NVIDIA ↔ AMD**」的对照框架里：

```text
[算子需求]      NVIDIA 侧常见实现        AMD 侧对应实现
注意力          FlashInfer / FlashAttention   AITER 提供的 attention 内核
MoE 分组 GEMM   Triton / CUTLASS 算子         AITER / MoRI 提供的 grouped GEMM
通用融合算子    Triton                        Triton（编译到 ROCm）+ AITER 封装
```

这样看就清楚了：**SGLang 的上层调度（u2-l2）、结构化解码（u2-l3）、MLA（u2-l4）、大规模 EP（u2-l5）在 NVIDIA 与 AMD 上是共通的；差异主要在最底层算子**。AITER 的工作，就是把 AMD 这一层补齐到「够快」。

#### 4.3.3 源码精读

AITER/MoRI 的资料入口在 README 的 AMD Meetup 子区段：

[README.md:38-48](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L38-L48) 这一段是 2025-08-22 的 AMD SGLang Meetup，共五份幻灯片：roadmap、大规模部署（EP）、highlights、SGLang×Wave、以及本模块的主角 **AITER/MoRI Introduction**（[第 48 行](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L48-L48)）。注意它们被放在同一个 `### AMD SGLang Meetup` 子标题下——这是一次活动的完整资料包，应整体阅读。

把 AITER 放回 AMD 适配成果链里看：

[README.md:11-13](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L11-L13) 这里「achieved SOTA performance on AMD nightly image」就是 AITER 等专用内核工作的**结果验证**——没有底层算子补齐，谈不上 SOTA。

> 待打开 PDF 确认：`amd_meetup_aiter_mori.pdf` 内部具体介绍了 AITER 的哪些算子、MoRI 的确切定义与作用、以及给出的性能对比数据。

#### 4.3.4 代码实践：跟踪「跨硬件内核栈」的资料对照

1. **实践目标**：建立「同一类算子，NVIDIA 与 AMD 各自的内核库」的对照表，体会 AITER 的定位。
2. **操作步骤**：
   - 复习 u3-l1 精读博客里对 **FlashInfer**（NVIDIA 侧 attention 内核库）的描述。
   - 打开 [slides/amd_meetup_aiter_mori.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/amd_meetup_aiter_mori.pdf)，找出 AITER 覆盖的算子类别。
   - 自行填写一张三列表：算子类别 / NVIDIA 侧实现 / AMD 侧实现。
3. **需要观察的现象**：AMD 侧的实现会把 AITER（可能还有 MoRI）放在「NVIDIA 侧 FlashInfer/Triton」对应的位置。
4. **预期结果**：你会得出「AITER ≈ AMD 版的 FlashInfer + MoE 内核集合」的直觉判断。
5. 待本地验证：表中 AMD 侧的具体算子清单与 MoRI 的准确定义，以 PDF 实际内容为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能「把 NVIDIA 上的 CUDA 算子直接拿来在 AMD 上跑」？

> **参考答案**：CUDA 是 NVIDIA 专有，AMD GPU 走 ROCm/HIP。直接移植既缺驱动栈也拿不到硬件特性红利；用 Triton 这类可跨平台编译的语言重写、或用 AITER 这类 AMD 原生内核库，才能用上 MI300X 的专用矩阵乘与访存能力。

**练习 2**：AITER 与 u2-l4 讲的「DeepSeek 模型优化」是什么关系？

> **参考答案**：模型优化分两条线——一是算法/数值层（如 MLA、量化），二是算子层（如 AITER）。AITER 属于算子层：它把 MLA 注意力、MoE 分组 GEMM 等「DeepSeek 特别依赖」的算子在 AMD 上做到高性能，是模型优化能在 AMD 上落地的底层支撑。

---

## 5. 综合实践

把本讲三块内容串起来，完成一个综合阅读与写作任务。

**任务**：结合本讲三份幻灯片（fp8/mxfp 量化、AITER/MoRI、AMD roadmap）与 README 中 AMD 相关博客，写一段 **300 字左右** 的说明，回答：

> SGLang 在 AMD MI300X 上为 DeepSeek 提供加速的关键技术有哪些？

建议按以下步骤推进：

1. **建资料簇**：用 `grep -ni 'amd\|mi300' README.md` 捞出全部 AMD 触点，把幻灯片（仓库内 PDF）与博客（外链）分成两组。
2. **读量化**：打开 `slides/sglang-fp8-mxfp-quantizations.pdf`，结合 4.2，提炼「fp8 + mxfp 如何省显存/提速」。
3. **读内核**：打开 `slides/amd_meetup_aiter_mori.pdf`，结合 4.3，提炼「AITER 如何补齐 AMD 算子」。
4. **读路线图**：打开 `slides/amd_meetup_sglang_roadmap.pdf`，结合 4.1，提炼「适配的演进方向」。
5. **交叉验证**：把提炼出的要点与 [README.md:128-136](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L128-L136) 的 AMD 博客相互印证——博客里的性能结论应能被「量化 + AITER」解释。
6. **落笔**：你的说明里应至少出现三类关键词——**量化（fp8/mxfp）**、**专用内核（AITER/MoRI）**、**硬件特性（MI300X 大显存 / ROCm）**，并指出它们如何协同。

**预期结果示例骨架**（仅结构，不含具体数字）：

> SGLang 在 MI300X 上加速 DeepSeek 靠「软硬协同」三条腿：其一，用 fp8/mxfp 量化把 671B 权重压进有限显存并缓解访存瓶颈；其二，用 AITER 等专用内核补齐 attention 与 MoE grouped GEMM，让 AMD 算子追平 NVIDIA；其三，借助 MI300X 192 GB 大显存与 ROCm 持续迭代，支撑大规模 EP 部署。三者共同造就 README 所述「AMD nightly image 上的 SOTA」。

> 待本地验证：骨架中的定性结论以 README 文字为据；任何具体加速倍数请引用 PDF/博客原文，不要自行填写。

## 6. 本讲小结

- **AMD 适配是一条 roadmap**：从 2024-10 首次分享，经量化、框架级适配，到 2025-03 的 PyTorch 生态 + AMD nightly SOTA，再到 2025-08 的专用内核（AITER/MoRI），资料横跨 README 多区段，构成资料簇。
- **fp8 与 mxfp 是两种量化思路**：fp8 是每元素 8 位浮点；mxfp 采用「每 32 元素共享一个缩放」的块缩放，精度与压缩更平衡；DeepSeek 因原生 fp8 训练，推理量化损失小。
- **量化直接决定多卡规模**：粗算显示 fp8 把 DeepSeek V3 权重压到约 671 GB、mxfp4 约 336 GB，这是 u2-l5 大规模 EP 部署能成立的前提之一。
- **AITER 是 AMD 版的「FlashInfer」**：用 Triton 写、编译到 ROCm，补齐 DeepSeek 所需的 attention 与 MoE 内核；它与 MoRI 同属 AMD 推理内核栈，具体边界以 PDF 为准。
- **资料阅读方法**：本仓库无可运行代码，实践以「grep 检索 + PDF 精读 + 博客交叉验证」为主；凡幻灯片页内具体数字，一律查原文，不编造。

## 7. 下一步学习建议

- **横向对照 NVIDIA 路线**：读 u3-l1 里的 FlashInfer 部分，与本讲 AITER 建立跨硬件对照，巩固「上层共通、底层各异」的认知。
- **回到模型优化主线**：结合 u2-l4（DeepSeek MLA）重新理解「模型优化 = 数值层（量化）+ 结构层（MLA）+ 算子层（AITER）」的三层框架。
- **向安全与边界延伸**：本单元下一篇 u3-l3（KV Cache 侧信道）会讨论共享 KV cache 的安全问题——它与本讲的「量化省显存、共享提速」是一体两面：省与快会放大共享带来的隐患。
- **亲手补一张表**：把本仓库所有 AMD 相关资料（幻灯片 + 博客 + 公告）按日期排成时间线，作为你个人学习路径（u4-l2）的素材。
