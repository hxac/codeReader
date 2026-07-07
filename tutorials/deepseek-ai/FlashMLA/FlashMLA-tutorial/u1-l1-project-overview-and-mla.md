# FlashMLA 项目定位与 MLA 背景

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是帮你建立对 FlashMLA 的「全局第一印象」。读完本讲，你应当能够：

- 说清楚 FlashMLA 是什么、为谁服务、包含哪四类 kernel；
- 理解 Multi-head Latent Attention（MLA）为什么要区分 MQA / MHA 两种模式，以及 `head_dim_k` / `head_dim_v` 的含义；
- 看懂 README 的支持矩阵，知道一块给定 GPU（SM90 还是 SM100）能走哪几条 kernel 路径；
- 理解性能指标 `TFlops`（算力）与 `GB/s`（带宽）分别衡量什么，以及为什么同一个 kernel 会同时报两个数字。

本讲不涉及任何 CUDA 代码细节，只读两份「文档型源码」：`README.md` 与 `docs/20250422-new-kernel-deep-dive.md`。真正的 kernel 源码精读从 Unit 2 开始。

## 2. 前置知识

在开始之前，用最通俗的话过一遍几个基础概念。如果你已经熟悉，可以跳过本节。

- **注意力（Attention）**：Transformer 里，每个 query token 要去和所有 key token 算相似度（`Q @ Kᵀ`），再用相似度对 value 加权求和（`P @ V`），得到输出。计算量随序列长度增长。
- **Prefill（预填充）与 Decode（解码）**：大模型推理分两阶段。Prefill 阶段一次性处理整段输入 prompt（序列长、矩阵大、偏算力受限）；Decode 阶段逐个生成 token，每步只有 1 个（或少量）新 query 去 attend 已有的 KV（序列长但 query 少、偏带宽受限）。
- **KV Cache**：Decode 时，历史 token 的 Key/Value 不重算，而是缓存复用。如何压缩这块缓存、如何快速读取它，是 MLA 与 FlashMLA 的核心关切。
- **GPU 的两类性能指标**：
  - `TFlops`（每秒万亿次浮点运算）：衡量 **算力（compute）**，主要由 Tensor Core 提供。
  - `GB/s`（每秒千兆字节）：衡量 **访存带宽（memory bandwidth）**，即从显存搬数据的速度。
  - 一个 kernel 是「算力受限（compute-bound）」还是「带宽受限（memory-bound）」，取决于它的 **算术强度（FLOPs / byte）** 与 GPU 的算力/带宽之比。这个判定会在本讲末尾和 Unit 3 详细展开。

## 3. 本讲源码地图

本讲只涉及两份文档文件，但它们是理解整个项目的「地图」：

| 文件 | 作用 |
| :--- | :--- |
| `README.md` | 项目门面：定位、新闻、性能数据、**支持矩阵**、安装、Python 接口用法。本讲主要读它的「定位」「性能」「要求」「支持矩阵」四节。 |
| `docs/20250422-new-kernel-deep-dive.md` | DeepSeek 官方技术博客，解释新 MLA 解码 kernel 为何是 compute-bound、seesaw 调度等设计。本讲只取其中「理论分析」一节来支撑性能指标的讲解。 |

## 4. 核心概念与源码讲解

### 4.1 项目定位与四类 kernel

#### 4.1.1 概念说明

FlashMLA 是 DeepSeek 开源的 **高性能注意力（attention）kernel 库**，专门为 [DeepSeek-V3](https://github.com/deepseek-ai/DeepSeek-V3) 与 [DeepSeek-V3.2-Exp](https://github.com/deepseek-ai/DeepSeek-V3.2-Exp) 这两个模型服务。它把这些模型推理中最耗时的注意力计算，用 CUDA kernel + CUTLASS 写到了接近硬件极限的速度。

「MLA」是 **Multi-head Latent Attention（多头潜在注意力）** 的缩写，是 DeepSeek 自家设计的注意力变体（详见 4.2）。所以「FlashMLA」可以直译为「极速 MLA」。

项目把 kernel 按两个维度组织：

- **阶段（stage）**：prefill（预填充）还是 decode（解码）；
- **稀疏性（sparsity）**：dense（稠密，attend 全部 KV token）还是 sparse（稀疏，只 attend 部分被选中的 token）。

两两组合就是 **四类 kernel**：

| | Dense | Sparse |
| :--- | :--- | :--- |
| **Prefill** | Dense Prefill | Sparse Prefill |
| **Decode** | Dense Decoding | Sparse Decoding（带 FP8 KV cache） |

其中 **Sparse（稀疏）** kernel 服务于 DeepSeek V3.2 引入的 DeepSeek Sparse Attention（DSA）：不是每个 query 都去看所有 KV，而是先选出若干「相关 token」再只对这些 token 算注意力，从而在长上下文下大幅省算力。Sparse Decoding 还额外使用 **FP8 KV cache**，把每个 token 的缓存压到 656 字节（详见 4.2.3）。

#### 4.1.2 核心流程

从一个使用者的视角，四类 kernel 在推理流程中的位置大致如下：

```
              ┌─────────────────────────── 推理请求 ───────────────────────────┐
              │                                                                │
       Prefill 阶段（处理整段 prompt）              Decode 阶段（逐 token 生成）
              │                                                                │
   ┌──────────┴──────────┐                                              ┌──────┴──────┐
   │                     │                                              │             │
全量 attend            选 token attend                          全量 attend    选 token attend
(Dense Prefill)      (Sparse Prefill)                       (Dense Decoding) (Sparse Decoding, FP8 KV)
```

四类 kernel 的输入/输出语义在 `README.md` 的 Usage 章节里分别给出（本讲先不展开签名，留到 Unit 1 的 u1-l4）。

#### 4.1.3 源码精读

先看 README 开头对项目定位和四类 kernel 的官方描述：

- [README.md:5-17](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L5-L17)：明确 FlashMLA 是 DeepSeek 的优化注意力 kernel 库，服务 V3/V3.2，并按 Sparse / Dense 两族列出四类实现。注意 Sparse 那族特别标注了 decode 阶段带 **FP8 KV cache**。

README 还点出了 Sparse kernel 与论文的对应关系——它们驱动 DeepSeek Sparse Attention（DSA），并附了 V3.2 论文链接。这解释了「为什么要单独做稀疏 kernel」：模型本身的注意力结构就是稀疏的。

#### 4.1.4 代码实践

**实践目标**：动手把四类 kernel 的「阶段 × 稀疏性」分类内化为一张自己画的表。

**操作步骤**：

1. 打开 `README.md`，定位到「Introduction」一节。
2. 找到 "Sparse Attention Kernels" 与 "Dense Attention Kernels" 两个小标题。
3. 把每个 bullet 复制到你自己的笔记里，重新整理成「阶段（prefill/decode）× 稀疏性（dense/sparse）」的二维表。
4. 在 Sparse Decode 一格旁边，用红字标注「FP8 KV cache」。

**需要观察的现象**：你会发现 README 的 bullet 是按「族（Sparse / Dense）」分组的，而不是按「阶段」分组；整理成二维表后，四个格子各恰好填一个 kernel，没有遗漏也没有重复。

**预期结果**：你得到一张 2×2 的表，与 4.1.1 中的表完全一致。

#### 4.1.5 小练习与答案

**练习 1**：FlashMLA 的四类 kernel 中，哪一类特别依赖 FP8 数据格式？为什么？

> **答案**：Sparse Decoding。因为 decode 阶段对带宽极其敏感，而 KV cache 是大头；把每个 token 的 KV 压成 FP8（656 字节）能显著降低访存量。注意它只把 KV 存成 FP8，矩阵乘法（MMA）仍以 bfloat16 进行。

**练习 2**：如果某个模型用的是「全量注意力」（没有任何稀疏化），它应该用 FlashMLA 的哪两类 kernel？

> **答案**：Dense Prefill（prefill 阶段）与 Dense Decoding（decode 阶段）。

---

### 4.2 MLA / MQA / MHA 背景

#### 4.2.1 概念说明

要理解 FlashMLA 的接口和配置，必须先理解 **MLA（Multi-head Latent Attention）**。

**MLA 的核心思想**：普通注意力的 KV cache 很大（每个 token 要存所有层的 K 和 V）。MLA 借鉴 Low-Rank 思路，**只缓存一个压缩后的「潜在向量（latent）」**，K 和 V 在需要时从这个潜在向量「现算（up-project）」出来。这样 KV cache 体积大幅缩小，是 DeepSeek-V3 能支持超长上下文的关键。

一个直接后果是：在 MLA 里，**K 和 V 实际来自同一份潜在向量**，博客原话是「in MLA, K and V are the same with different names」（K 和 V 是同一个东西的不同名字）。这一点很重要，它解释了为什么 `head_dim_k` 和 `head_dim_v` 可以不同——V 只是 K 的一部分。

**FlashMLA 在 README 里把 MLA 分成两种「模式」**，对应不同的 `head_dim`：

- **MQA 模式（Multi-Query Attention）**：`head_dim_k = 576`，`head_dim_v = 512`。用于 **Dense Decoding / Sparse Decoding / Sparse Prefill**。
- **MHA 模式（Multi-Head Attention）**：`head_dim_k = 192 或 128`，`head_dim_v = 128`。用于 **Dense Prefill**。

> ⚠️ 注意：这里的「MQA / MHA」是 FlashMLA 文档里的**约定叫法**，与学术界通用的 MQA（多 query 共享一组 KV）、MHA（每头独立 KV）并不完全等同。在本项目里，你可以先把它理解为「**两种不同的 head_dim 配置**」：MQA 模式头维很大（576/512），MHA 模式头维较小（128）。精确定义以 README 脚注为准。

**为什么 MQA 模式下 `head_dim_k`（576）比 `head_dim_v`（512）大 64？** 因为 K 的 576 维里，有 64 维是专门给 **RoPE（旋转位置编码）** 用的部分，而 V 不需要位置编码，所以只有 512 维。这个 576 = 512 + 64 的拆分，正是 FP8 布局里「512 字节 NoPE 部分 + 128 字节 RoPE 部分」的来源。

#### 4.2.2 核心流程

把上面这些概念串起来，MLA 的注意力计算可以简化理解为：

```
latent（压缩缓存）  ──up-project──▶  K（576 维：512 NoPE + 64 RoPE）
                                   V（512 维：只取 NoPE 部分）

Q @ Kᵀ ──▶ scores ──softmax──▶ P
P @ V   ──▶ out
```

注意 `head_dim_v < head_dim_k`，正是因为 V 不含 RoPE 那部分。这也意味着 `Q @ Kᵀ` 和 `P @ V` 这两个矩阵乘的「内维」不同（一个是 576，一个是 512），是 MLA kernel 区别于普通 MHA kernel 的一个细节。

在 decode 的 FP8 KV cache 中，每个 token 占 **656 字节**，正好对应上面这套拆分：

| 偏移 | 长度 | 内容 | 说明 |
| :--- | :--- | :--- | :--- |
| 0 | 512 B | 512 个 `float8_e4m3` | 量化后的 NoPE 部分（即 V 的来源） |
| 512 | 16 B | 4 个 `float32` scale | 每 128 个 fp8 共用一个 scale（tile 级量化） |
| 528 | 128 B | 64 个 `bfloat16` | RoPE 部分，**不量化**，保精度 |

> 这张表的数据来自 README 对 FP8 格式的描述，精确的字节布局与量化/反量化代码在 `tests/quant.py`，会在 Unit 5（u5-l1）精读。本讲只需记住「656 = 512(fp8) + 16(scale) + 128(rope bf16)」这个事实。

#### 4.2.3 源码精读

- [README.md:70](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L70)：README 脚注 [2] 给出 MLA Mode 的权威定义——MQA = `head_dim_k=576, head_dim_v=512`；MHA = `head_dim_k=192/128, head_dim_v=128`。这是全项目最关键的一行配置说明之一。
- [README.md:118-123](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L118-L123)：描述 FP8 with scale 格式，656 字节 = 512 fp8 NoPE + 16 字节 scale（4 个 fp32）+ 128 字节 RoPE（64 个 bf16），并明确 RoPE 部分不量化。本节的字节表就源自这里。
- [docs/20250422-new-kernel-deep-dive.md:44](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L44)：博客原文「in MLA, K and V are the same with different names」，是理解「为什么 K 和 V 同源、head_dim 可以不同」的直接依据。

#### 4.2.4 代码实践

**实践目标**：通过阅读 README，把「两种 MLA 模式 → 四类 kernel」的对应关系理清楚。

**操作步骤**：

1. 打开 `README.md` 的支持矩阵（4.3 会精读）。
2. 对每一类 kernel，查它的「MLA Mode」列是 MQA 还是 MHA。
3. 在笔记里画一张映射：

```
Dense Decoding  ──MQA──▶ head_dim_k=576, head_dim_v=512
Sparse Decoding ──MQA──▶ head_dim_k=576, head_dim_v=512
Sparse Prefill  ──MQA──▶ head_dim_k=576, head_dim_v=512
Dense Prefill   ──MHA──▶ head_dim_k=192/128, head_dim_v=128
```

4. 回答：为什么 Dense Prefill 单独用 MHA 模式而其余三类用 MQA？（提示：与该 kernel 由谁贡献、对应什么模型结构有关，详见 4.3 与 Unit 7。）

**需要观察的现象**：你会发现 MQA 模式（大头维 576/512）只出现在服务 DeepSeek-V3/V3.2 原生 MLA 的三类 kernel 上；Dense Prefill 的 MHA 模式是 NVIDIA 贡献的标准 MHA 前向/反向 kernel（README News 里提到 2025.08.01 的 PR）。

**预期结果**：你能背下「MQA → 576/512，MHA → 192或128/128」这两组维度。

#### 4.2.5 小练习与答案

**练习 1**：MQA 模式下 `head_dim_k = 576`，其中 64 维去哪了？

> **答案**：这 64 维是 RoPE（旋转位置编码）部分。V 不需要位置编码，所以 `head_dim_v = 512 = 576 - 64`。FP8 布局里这 64 维以 64 个 bf16（128 字节）单独存放且不量化。

**练习 2**：为什么说在 MLA 里 `Q @ Kᵀ` 和 `P @ V` 的内维不同？

> **答案**：`Q @ Kᵀ` 的内维是 `head_dim_k = 576`（含 RoPE），而 `P @ V` 的内维是 `head_dim_v = 512`（不含 RoPE）。普通 MHA 里这俩通常相等，MLA 不等，这是 MLA kernel 的一个设计细节。

---

### 4.3 kernel 支持矩阵与性能指标

#### 4.3.1 概念说明

**支持矩阵**回答一个问题：「我手上这块 GPU，到底能跑 FlashMLA 的哪几类 kernel？」它是你在动手安装前必须先看的一张表。

FlashMLA 只支持两代 NVIDIA 架构：

- **SM90**：Hopper 架构，代表卡 H100 / **H800 SXM5**。
- **SM100**：Blackwell 架构，代表卡 **B200**。

并不是每类 kernel 都在两代架构上都实现了。README 的支持矩阵把「kernel × 架构 × MLA 模式 × KV cache 格式」四列摆在一起。

**性能指标**则回答「这 kernel 跑得多快」。FlashMLA 对同一类 kernel 往往同时报两个数字：

- `GB/s`（带宽利用率）：在 **memory-bound** 配置下能达到的显存带宽；
- `TFlops`（算力利用率）：在 **compute-bound** 配置下能达到的算力。

为什么会有两个数字？因为同一个 kernel 在不同输入规模下，瓶颈会在「带宽」和「算力」之间切换。这背后是经典的 **roofline 判定**。

#### 4.3.2 核心流程

**支持矩阵**（来自 README）：

| Kernel | GPU 架构 | MLA 模式 | KV cache 格式 |
| :---: | :---: | :---: | :---: |
| Dense Decoding | SM90 | MQA | BF16 |
| Sparse Decoding | SM90 & SM100 | MQA | FP8 |
| Dense Prefill | SM100 | MHA | — |
| Sparse Prefill | SM90 & SM100 | MQA | — |

读这张表的方法：**先看你有什么卡（SM90 还是 SM100），再看你想跑哪类 kernel，交叉查可用性。** 例如：

- 你有一块 H800（SM90）：能跑 Dense Decoding、Sparse Decoding、Sparse Prefill，**不能**跑 Dense Prefill。
- 你有一块 B200（SM100）：能跑 Sparse Decoding、Dense Prefill、Sparse Prefill，**不能**跑 Dense Decoding。

**性能数据**（来自 README，均需 CUDA 12.8+，SM100 需 12.9+）：

| Kernel | 卡 | 性能 |
| :--- | :--- | :--- |
| Dense MLA Decoding | H800 SXM5 | memory-bound 配置 **3000 GB/s**；compute-bound 配置 **660 TFLOPS** |
| Sparse MLA Decoding | H800 SXM5 | compute-bound **410 TFLOPS**（用 FP8 KV，但 MMA 在 bf16 下做） |
| Sparse MLA Decoding | B200 | **350 TFlops**（官方注明「尚未充分优化」） |
| Dense MHA Prefill | B200 | 前向 **1460 TFlops**，反向 **1000 TFlops**（NVIDIA 报告） |
| Sparse MLA Prefill | H800 SXM5 | 前向 **640 TFlops** |
| Sparse MLA Prefill | B200 | 前向 **1450 TFlops**（CUDA 12.9） |

**关于 compute-bound 判定**（取自博客理论分析，详细推导留到 Unit 3 的 u3-l1）：

一次 MLA 解码的算术强度（FLOPs / byte）约为：

\[ \text{FLOPs/byte} \;\approx\; 2\,h_q s_q \]

其中 \(h_q\) 是 query 头数，\(s_q\) 是每条请求的 query token 数（无 MTP 时为 1）。H800 SXM5 的峰值带宽约 3.35 TB/s、降频后实际峰值算力约 865 TFlops，于是分界点约为：

\[ h_q s_q \;\ge\; \frac{1}{2}\cdot\frac{865}{3.35} \;\approx\; 128 \]

DeepSeek 解码实例不做 Tensor Parallel，故 \(h_q = 128\)，正好踩在分界点上偏 compute-bound 一侧——这就是为什么 Dense MLA Decoding kernel 的优化目标是「喂饱 Tensor Core」，也是它能在 H800 上做到 660 TFLOPS 的理论依据。

> 小贴士：你不需要现在就完全消化这个推导。本节只是用来说明「为什么性能要同时报 GB/s 和 TFlops 两个数」。完整推导、与 H800 峰值的逐项对比在 **u3-l1**。

#### 4.3.3 源码精读

- [README.md:53-57](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L53-L57)：环境要求——SM90/SM100、CUDA 12.8+（SM100 需 12.9+）、PyTorch 2.0+。这是支持矩阵成立的前提。
- [README.md:61-66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L61-L66)：**支持矩阵本体**。本节 4.3.2 的第一张表即照搬自此。注意 Dense Decoding 仅 SM90、Dense Prefill 仅 SM100 这种「不对称」分布。
- [README.md:35](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L35)：Dense/Sparse MLA Decoding 的性能（3000 GB/s、660 TFLOPS、410 TFLOPS、B200 350 TFlops）。
- [README.md:43](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L43)：Dense MHA Prefill 在 B200 上的前向 1460 / 反向 1000 TFlops。
- [README.md:51](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L51)：Sparse MLA Prefill 在 H800 640 TFlops、B200 1450 TFlops。
- [docs/20250422-new-kernel-deep-dive.md:11](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L11)：算术强度 ≈ \(2 h_q s_q\) 的推导。
- [docs/20250422-new-kernel-deep-dive.md:13](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L13)：compute/memory bound 的分界条件 \(h_q s_q \ge 128\)。
- [docs/20250422-new-kernel-deep-dive.md:15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L15)：DeepSeek 解码不用 TP、\(h_q=128\)，故 kernel 是 compute-bound。

#### 4.3.4 代码实践

**实践目标**：用一段 Python 自动判断「当前这块卡能跑 FlashMLA 的哪几条 kernel 路径」，并与 README 支持矩阵对照。

**操作步骤**：

1. 确认环境已装 PyTorch（`pip show torch`）。
2. 把下面的 **示例代码** 存成 `check_arch.py` 并运行（无 GPU 时也能跑，会走假想分支）。
3. 对照 [README.md:61-66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L61-L66) 的支持矩阵，验证脚本输出是否一致。

```python
# 示例代码：根据 GPU 架构判断可用的 FlashMLA kernel 路径
import torch

def classify_gpu_and_kernels():
    # torch.cuda.get_device_capability() 返回 (major, minor)
    if not torch.cuda.is_available():
        print("当前环境无可用 GPU，下面用假想值演示推理过程。")
        major, minor = 9, 0  # 假想值，可改成 (10, 0) 体验 B200 分支
    else:
        major, minor = torch.cuda.get_device_capability()
        print(f"torch.cuda.get_device_capability() = ({major}, {minor})")

    # NVIDIA: Hopper=SM90, Blackwell=SM100
    if (major, minor) == (9, 0):
        tag = "SM90"
    elif (major, minor) == (10, 0):
        tag = "SM100"
    else:
        tag = "未知/不支持"
    print(f"推断架构: {tag}\n")

    # 对照 README.md 支持矩阵（L61-L66）
    support = {
        "Dense Decoding":  ["SM90"],
        "Sparse Decoding": ["SM90", "SM100"],
        "Dense Prefill":   ["SM100"],
        "Sparse Prefill":  ["SM90", "SM100"],
    }
    print(f"{'Kernel':<18}{'可用':<8}{'README 要求'}")
    for kernel, archs in support.items():
        ok = tag in archs
        print(f"{kernel:<18}{'是' if ok else '否':<8}{'/'.join(archs)}")

if __name__ == "__main__":
    classify_gpu_and_kernels()
```

**需要观察的现象**：

- 在 H800（SM90）上，`Dense Prefill` 应显示「否」，其余三类「是」；
- 在 B200（SM100）上，`Dense Decoding` 应显示「否」，其余三类「是」。

**预期结果**：脚本输出与你手工查支持矩阵的结论一致。

> 待本地验证：上述运行结果取决于你实际所在的 GPU。本环境未必有 CUDA 设备，若 `torch.cuda.is_available()` 为 `False`，脚本会走假想值分支——此时把 `major, minor` 分别改成 `(9,0)` 和 `(10,0)` 各跑一遍，即可观察到两种架构下的差异。

#### 4.3.5 小练习与答案

**练习 1**：你拿到一块 B200（SM100），想跑 Dense MLA Decoding，能做到吗？为什么？

> **答案**：不能。支持矩阵显示 Dense Decoding 只支持 SM90（Hopper），SM100（Blackwell）没有这条路径。你需要改用 Sparse Decoding（SM100 支持），或换一块 H800。

**练习 2**：Dense MLA Decoding 同时报告了 3000 GB/s 和 660 TFLOPS 两个数，它们分别对应什么配置？为什么会有两个？

> **答案**：3000 GB/s 对应 memory-bound 配置（query 少、KV 长，瓶颈在搬 KV 的带宽）；660 TFLOPS 对应 compute-bound 配置（query 多，瓶颈在算力）。同一个 kernel 在不同输入规模下瓶颈会切换，所以要分别报两个数字。

**练习 3**：为什么 DeepSeek 的 MLA 解码 kernel 是 compute-bound 的？用一句话回答。

> **答案**：因为解码实例不做 Tensor Parallel，\(h_q=128\)，算术强度约 \(2 h_q s_q \ge 256\)，超过了 H800 的算力/带宽分界点（\(h_q s_q \approx 128\)），所以瓶颈在算力而非带宽。

## 5. 综合实践

**综合任务**：假设你是某团队的推理工程师，团队刚采购了两台机器：A 是 8×H800（SM90），B 是 8×B200（SM100），都要部署 DeepSeek-V3.2（使用 DSA 稀疏注意力 + FP8 KV cache）。

请完成一份一页纸的《FlashMLA kernel 选型说明》，内容需包含：

1. **支持矩阵自查**：从 [README.md:61-66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L61-L66) 出发，分别列出 A、B 两台机器各自能跑的 kernel 子集。
2. **阶段匹配**：V3.2 推理有 prefill 和 decode 两个阶段，且用的是稀疏注意力——为 A 和 B 各自选定合适的 kernel（提示：A 机器 prefill 只能走 Sparse Prefill，因为 Dense Prefill 不支持 SM90；decode 走 Sparse Decoding）。
3. **head_dim 说明**：引用 [README.md:70](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L70) 说明你选的这些 kernel 走 MQA 模式，`head_dim_k=576 / head_dim_v=512`，并解释 576 与 512 之差的来源。
4. **性能预期**：引用 [README.md:35](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L35) 与 [README.md:51](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L51)，给出 A 机器 decode≈410 TFlops、prefill≈640 TFlops，B 机器 decode≈350 TFlops、prefill≈1450 TFlops 的预期，并指出 B200 的 decode 数字「尚未充分优化」。
5. **环境前提**：引用 [README.md:53-57](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L53-L57) 提醒 B 机器必须用 CUDA 12.9+。

这份说明不涉及任何代码，但会逼你把本讲的三个最小模块（项目定位与四类 kernel、MLA 模式、支持矩阵与性能）全部用一遍。

## 6. 本讲小结

- FlashMLA 是 DeepSeek 的高性能注意力 kernel 库，服务 V3/V3.2，按 **阶段（prefill/decode）× 稀疏性（dense/sparse）** 划分为四类 kernel。
- MLA（Multi-head Latent Attention）只缓存压缩的潜在向量，K/V 同源，故 `head_dim_k`（576，含 64 维 RoPE）> `head_dim_v`（512）。
- README 用 **MQA / MHA** 两种模式区分 head_dim：MQA = 576/512（用于 decode 与 sparse prefill），MHA = 192或128/128（用于 dense prefill）。
- FP8 KV cache 每 token 656 字节 = 512(fp8 NoPE) + 16(4×fp32 scale) + 128(64×bf16 RoPE)，RoPE 部分不量化。
- 支持矩阵是非对称的：Dense Decoding 仅 SM90，Dense Prefill 仅 SM100，两类 Sparse 同时支持 SM90/SM100。
- 性能同时报 `GB/s`（memory-bound）与 `TFlops`（compute-bound）；DeepSeek 解码因 \(h_q=128\) 不做 TP 而落 compute-bound，分界点约 \(h_q s_q \approx 128\)。

## 7. 下一步学习建议

本讲只建立了「是什么」的印象，还没有碰一行可运行代码。建议接下来的学习顺序：

1. **u1-l2（环境准备与源码构建安装）**：动手把 FlashMLA 编译装好，理解 `setup.py` 如何根据 NVCC 版本选择 `sm_90a` / `sm_100f` 编译目标。
2. **u1-l3（仓库目录结构与代码组织）**：在跑通之前先建立「代码空间感」，知道四类 kernel 分别在 `csrc/` 的哪个子目录。
3. **u1-l4（Python 接口与最小运行示例）**：照着 README 跑通最小解码示例，第一次真正调用 FlashMLA。
4. 若你对 compute-bound 判定感兴趣，可以直接跳读 **u3-l1**，那里有 FLOPs/byte 的完整推导。

进入 Unit 2 之后，我们会沿着「Python → pybind → C++ → CUDA kernel」的真实调用链，开始源码精读。
