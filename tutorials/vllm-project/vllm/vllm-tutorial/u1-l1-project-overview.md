# 项目定位与核心特性

## 1. 本讲目标

本讲是整本 vLLM 学习手册的第一篇。读完后，你应该能够：

- 用一句话说清 vLLM 是什么、解决什么问题；
- 说出 vLLM 相比朴素 HuggingFace 推理的几项关键性能优化；
- 理解 PagedAttention 的核心动机——它解决了哪种显存问题；
- 知道连续批处理（continuous batching）、分块预填充（chunked prefill）、前缀缓存（prefix caching）、量化（quantization）这些名词大致指什么；
- 建立对项目能力边界和生态的整体认知，为后续阅读源码建立「地图感」。

本讲几乎不涉及深层代码细节，重点是把直觉讲清楚。真正的源码精读会在后续讲义展开。

## 2. 前置知识

阅读本讲前，建议你已经了解以下基础概念（不熟悉也没关系，下面会顺带解释）：

- **大语言模型（LLM）**：一种基于 Transformer 的文本生成模型，比如 Llama、Qwen、GPT 系列。
- **推理（inference）**：模型训练好后，给它一段输入（prompt），让它产出输出的过程。
- **服务（serving）**：把推理能力包装成一个常驻进程，通过网络接口（HTTP/gRPC）持续接收请求并返回结果。
- **KV 缓存（KV cache）**：Transformer 在生成每个 token 时，需要用当前 token 去和之前所有 token「算注意力」。为了避免重复计算，会把每层注意力的 Key、Value 张量缓存下来，这就是 KV 缓存。它是推理时显存占用的大头。
- **吞吐（throughput）**：单位时间内能处理多少请求或生成多少 token，是衡量推理引擎好坏的核心指标。

如果你对「为什么显存会成为瓶颈」还没有直觉，记住一句话：**推理时，显存常常比算力更先成为瓶颈**。vLLM 的多数优化，目标都是「在有限显存里塞下更多请求、把 GPU 算力用满」。

## 3. 本讲源码地图

本讲涉及的「源码」主要是文档与说明性文件，它们最适合用来建立全局认知：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md) | 项目的门面，用一段话和两个列表概括了 vLLM「快在哪、灵活在哪」，以及安装与上手方式。 |
| [docs/design/arch_overview.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md) | 架构总览，讲清入口（LLM 类、在线服务）、V1 多进程架构、Worker/ModelRunner/Model 的层次关系与设计取舍。 |
| [docs/design/paged_attention.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md) | PagedAttention 内核的历史设计文档（⚠️ 已标注为历史文档，但其中「分块 KV 缓存」的核心思想仍是理解 vLLM 的关键）。 |

> 提示：本讲引用的是**文档**而非 Python 代码，因为这一阶段重点是建立直觉。从下一单元开始，我们会进入真实 Python 源码。

## 4. 核心概念与源码讲解

本讲覆盖 4 个最小模块：**README（项目定位）**、**PagedAttention**、**连续批处理与前缀缓存**、**量化与高性能内核**。

### 4.1 vLLM 是什么：项目定位

#### 4.1.1 概念说明

vLLM 自我定位非常明确。README 里只有一句话：

> vLLM is a fast and easy-to-use library for LLM inference and serving.

翻译过来就是：**vLLM 是一个快速、易用的「LLM 推理与服务」库**。这里有两个关键词：

- **推理（inference）**：加载一个已训练好的模型，跑前向计算生成结果。
- **服务（serving）**：把推理能力以 OpenAI 兼容的 HTTP API 等形式对外提供。

为什么要单独做一个推理引擎，而不是直接用 HuggingFace 的 `transformers` 库？因为朴素推理在大规模服务时**又慢又费显存**。vLLM 的核心价值是：在相同硬件上，**显著提升吞吐量、降低单请求延迟**，同时保持易用（几行代码就能跑、兼容大量 HF 模型）。

vLLM 起源于加州大学伯克利分校的 Sky Computing Lab，如今已是社区维护、贡献者超过 2000 人的活跃开源项目。

#### 4.1.2 核心流程

vLLM 的能力可以归为两组（对应 README 的两个列表）：

1. **「快」的一面（性能优化）**：PagedAttention、连续批处理、分块预填充、前缀缓存、CUDA/HIP Graphs、量化、优化注意力内核、优化 GEMM/MoE 内核、推测解码、torch.compile 图变换、分离式 prefill/decode。
2. **「灵活易用」的一面**：无缝对接 HuggingFace 模型、多种解码算法（并行采样、beam search）、张量/流水/数据/专家/上下文并行、流式输出、结构化输出、工具调用、OpenAI 兼容 API、多 LoRA、跨硬件（NVIDIA/AMD/Intel/CPU 等）。

上手只需要一行安装：

```bash
uv pip install vllm
```

#### 4.1.3 源码精读

README 用两段列表把项目能力讲得非常清楚，建议通读：

[README.md:24](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L24) —— vLLM 的一句话定位「fast and easy-to-use library for LLM inference and serving」。

[README.md:28-39](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L28-L39) —— 「vLLM is fast with」的性能特性清单。本讲后续 3 个模块（PagedAttention、连续批处理、量化）正是从这里展开。

[README.md:41-51](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L41-L51) —— 「vLLM is flexible and easy to use with」的灵活性清单，说明它支持哪些部署形态和模型类型。

[README.md:66-70](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L66-L70) —— 安装方式 `uv pip install vllm`（推荐用 `uv`）。

#### 4.1.4 代码实践

1. **实践目标**：确认 vLLM 的安装与版本，建立「它能跑起来」的直观感受。
2. **操作步骤**：
   - 如果你本机有 GPU 环境，执行：
     ```bash
     uv pip install vllm
     python -c "import vllm; print(vllm.__version__)"
     ```
   - 如果没有 GPU 或不想安装，改为**源码阅读型实践**：打开 [README.md:64-74](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L64-L74)，阅读 Getting Started 段落，并访问文档链接。
3. **需要观察的现象**：导入是否报错；打印出的版本号是多少。
4. **预期结果**：能成功打印一个形如 `0.x.y` 的版本字符串。
5. **待本地验证**：具体版本号、是否需要 CUDA 环境，取决于你的机器，**请以本地实际输出为准**。

#### 4.1.5 小练习与答案

**练习 1**：vLLM 同时支持「离线推理」和「在线服务」。请各举一个典型入口。
**参考答案**：离线推理用 Python 的 `vllm.LLM` 类（`vllm/entrypoints/llm.py`）；在线服务用 `vllm serve <model>` 命令启动 OpenAI 兼容服务。这一点在后续 `u2-l1`、`u2-l2` 会详细讲。

**练习 2**：README 把 vLLM 的优势分成「fast」和「flexible」两组。请各举一个属于性能、一个属于灵活性的特性。
**参考答案**：性能侧——PagedAttention / 连续批处理；灵活性侧——OpenAI 兼容 API / 多 LoRA 支持。

---

### 4.2 PagedAttention：分块 KV 缓存如何解决显存问题

#### 4.2.1 概念说明

PagedAttention 是 vLLM 的招牌特性。要理解它，先得理解朴素 KV 缓存**浪费在哪**。

朴素做法：为每个请求预分配一整块**连续**的 KV 缓存，按「该请求可能达到的最大长度」来分配。问题在于：

- **内部碎片（internal fragmentation）**：实际生成往往远短于最大长度，但预分配的空间用不满。vLLM 论文指出，朴素 KV 缓存的实际利用率常常只有 20%~40%，即**六到八成的显存被白白浪费**。
- **外部碎片（external fragmentation）**：请求来来走走，显存里留下大小不一的空洞，难以拼成大块给新请求。

PagedAttention 借鉴了操作系统的**虚拟内存分页（paging）**思想：把 KV 缓存切成固定大小的**块（block）**，每个块存放固定数量 token 的 KV 数据。一个请求的逻辑 KV 通过一张「块表（block table）」映射到物理块。这样 KV 缓存可以**按需、按块**分配——生成几个 token 就申请几个块，几乎消除碎片。

#### 4.2.2 核心流程

PagedAttention 的关键数据布局（来自历史设计文档中的概念定义）：

- **Block（块）**：KV 缓存的最小分配单元。每个块在某一个 head 上存放 `BLOCK_SIZE` 个 token 的 K 或 V。
- **Sequence（序列）**：一个客户端请求，由若干 token 组成。
- 一个请求的完整 KV 不再是「一整块连续显存」，而是「若干物理块的集合」，由块表串联。

一个块能装多少元素？以文档中的例子（`BLOCK_SIZE = 16`，`HEAD_SIZE = 128`）：

\[
\text{每块元素数} = \text{BLOCK\_SIZE} \times \text{HEAD\_SIZE} = 16 \times 128 = 2048
\]

注意其中的 `x`：为了配合 GPU 向量化访存，一个 head 的 `HEAD_SIZE` 个元素会被再细分成若干 `x` 大小的段。

注意力计算本身仍是标准的 \(QK \to \text{softmax} \to V\) 流程，只是 K/V 的读取要**按块、按块表里的物理块号**去取，而不是连续地址。文档中给出的归一化 softmax 为：

\[
m(x) := \max_i x_i,\qquad f(x)_i = e^{x_i - m(x)},\qquad \ell(x) := \sum_i f(x)_i,\qquad \text{softmax}(x) := \frac{f(x)}{\ell(x)}
\]

这是一种数值稳定的 softmax（先减最大值再求指数），避免溢出。

#### 4.2.3 源码精读

> ⚠️ 重要：[paged_attention.md:3-5](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md#L3-L5) 明确标注这是一份**历史文档**（based on the original paper），「不再描述 vLLM 当前的代码」。但它阐述的「分块 KV 缓存」思想仍是理解 vLLM 的核心。我们用它来建立直觉，**具体 Python 实现会在 `u4-l4`（KVCacheManager / BlockPool）讲**。

[paged_attention.md:7-14](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md#L7-L14) —— 说明当前 vLLM 用自己的多头注意力内核（`csrc/attention/attention_kernels.cu`），它兼容「分块的 KV 缓存」，其中 key/value 缓存被存储在**分离的块**中。

[paged_attention.md:77-84](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md#L77-L84) —— **Sequence** 概念：一个序列代表一个客户端请求。

[paged_attention.md:104-109](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md#L104-L109) —— **Block** 概念：KV 缓存被切成块，每块在某 head 上存放固定数量（`BLOCK_SIZE`）token 的数据。这正是「分页」思想的落点。

[README.md:31](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L31) —— README 把 PagedAttention 列为「高效管理 attention key/value 显存」的关键手段。

#### 4.2.4 代码实践

1. **实践目标**：用直觉解释 PagedAttention 解决了什么显存问题。
2. **操作步骤**：
   - 阅读上面引用的 [Block 概念定义](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md#L104-L109)。
   - 在纸上画一个对比：左图画「朴素连续 KV 缓存」——3 个请求，每个预分配到最大长度 2048，但实际只用 300/800/1500，标出浪费的灰色区域；右图画「分块 KV 缓存」——同样 3 个请求，每个只占若干 16-token 的块，没有大片灰色浪费。
3. **需要观察的现象**：朴素方案里浪费面积随「最大长度−实际长度」增大而增大；分块方案里浪费最多不超过一个块（即 15 个 token 以内）。
4. **预期结果**：你能用自己的话说出——PagedAttention 把 KV 缓存从「按最大长度整块预分配」改成「按固定大小块按需分配」，从而把显存浪费从 60%~80% 降到接近一个块。
5. **待本地验证**：这是阅读理解型实践，不需要运行。

#### 4.2.5 小练习与答案

**练习 1**：朴素 KV 缓存有哪两类碎片？PagedAttention 主要解决了哪一类？
**参考答案**：内部碎片（预分配用不满）和外部碎片（空洞难复用）。PagedAttention 主要消除了内部碎片——按需按块分配，几乎不留用不满的空间。

**练习 2**：PagedAttention 的「块」借用了操作系统的什么思想？
**参考答案**：虚拟内存的分页（paging）/页表机制，用「块表（block table）」把逻辑 KV 映射到物理块。

---

### 4.3 连续批处理、分块预填充与前缀缓存

#### 4.3.1 概念说明

这一组优化都围绕「**让 GPU 别闲着**」。

- **连续批处理（continuous batching）**：朴素批处理在请求进入时凑成一个 batch，然后**所有序列必须一起算完**才能放新请求进来——于是 GPU 经常在等那个最长的序列。连续批处理改成**每一步（iteration）都可以动态调整 batch**：谁生成完了就移出，谁刚到就加进来。GPU 几乎不再空转。
- **分块预填充（chunked prefill）**：一个请求第一次进入时，要把整段 prompt 一次性算出 KV（这叫 **prefill**，计算量很大）。如果 prompt 很长，单独一条 prefill 会霸占 GPU、把一堆短的 decode 请求卡住。分块预填充把长 prefill **切成多块**，和 decode 请求**混在同一个 step** 里一起算，避免长请求独占。
- **前缀缓存（prefix caching）**：很多请求共享相同前缀（比如同一段 system prompt）。如果这段前缀的 KV 已经算过并缓存，后续请求就能**直接复用**这些 KV 块，省掉重复的 prefill 计算。

#### 4.3.2 核心流程

一个推理 step 在连续批处理下的大致组成：

```
一个 step 的批 =  [若干部 prefill token（可能来自分块的长 prompt）]  +  [若干正在进行 decode 的请求的各 1 个 token]
                       │                                                    │
                       └─ 计算量大，被切成块，与 decode 混算 ──────────────────┘
```

关键收益：

- **吞吐提升**：GPU 每个 step 都被填满，不再等待最慢序列。
- **延迟均衡**：长 prefill 被切块后，短请求不用排队等它。
- **计算节省**：前缀缓存让相同前缀只算一次。

这三者都依赖 PagedAttention 提供的「按块管理 KV」能力——正是因为 KV 是按块组织的，才能方便地「随时增减请求」「把 prefill 切块混算」「按 hash 复用前缀块」。

#### 4.3.3 源码精读

[README.md:32](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L32) —— README 把这三者并列为一条特性：「Continuous batching of incoming requests, chunked prefill, prefix caching」。

[arch_overview.md:81-107](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L81-L107) —— V1 多进程架构里，**Engine Core 进程**专门「runs the scheduler … continuously schedules requests」（持续调度请求）。这正是连续批处理得以实现的载体：调度器在 busy loop 里每一步都重新决定 batch 组成。

> 说明：调度器与队列的具体源码（`vllm/v1/core/sched/scheduler.py`）会在 `u4-l2`、`u4-l3` 精讲；前缀缓存的实现（`vllm/v1/core/kv_cache_utils.py`）在 `u4-l5` 精讲。

#### 4.3.4 代码实践

1. **实践目标**：用例子说明「长 prefill + 多条短 decode 混算」如何提升吞吐。
2. **操作步骤**：
   - 假设当前 step 里有：1 条 prefill 请求（prompt 长 2000 token），3 条 decode 请求（各只需算 1 个新 token）。
   - 对比两种策略：
     - **朴素策略**：先单独算完那条 2000-token 的 prefill，GPU 这期间 decode 的 3 条请求只能干等。
     - **分块预填充策略**：把 2000-token prefill 切成若干块（比如每块 512），每个 step 只算 1 块 prefill，同时把 3 条 decode 也算掉。
3. **需要观察的现象**：朴素策略下，3 条 decode 的延迟被那条长 prefill 严重拖慢；分块策略下，decode 几乎不受影响，且长 prefill 在多个 step 内分摊完成。
4. **预期结果**：你能解释「把长 prefill 切碎、和 decode 混在同一 step」既能让短请求响应更快，又能让 GPU 每 step 都有活干，从而提升整体吞吐。
5. **待本地验证**：阅读理解型实践；若想实测，可在 `u2` 单元跑通推理后用 metrics 观察（`u10-l2` 会讲指标）。

#### 4.3.5 小练习与答案

**练习 1**：连续批处理相对静态批处理，解决的核心问题是什么？
**参考答案**：静态批处理要等最慢的序列算完才能换批，GPU 空闲多；连续批处理每步可动态增减请求，让 GPU 持续满载。

**练习 2**：前缀缓存为什么能省计算？它复用的是哪一部分？
**参考答案**：多个请求共享相同前缀时，这段前缀的 KV 只需 prefill 一次并被缓存；后续请求复用的是这些**已算好的 KV 块**，省掉了重复的 prefill 计算。

---

### 4.4 量化与高性能内核

#### 4.4.1 概念说明

模型越大，权重占的显存越多、矩阵乘（GEMM）越慢。**量化（quantization）**就是用更低的数值精度来存权重（有时也包括激活），从而「**省显存、提速度**」。

- 朴素模型权重常用 FP16/BF16（每个元素 16 bit）。
- 量化后可能用 **FP8、INT8、INT4** 甚至 **4-bit**（MXFP4/NVFP4）等，把显存和带宽需求压到原来的 1/2 ~ 1/4。
- 代价是**精度损失**——不同量化方法在精度与速度间做不同取舍。

vLLM 支持非常多的量化方案，README 一口气列了一长串。理解时记住三类即可：

1. **标准整数/浮点格式**：FP8、INT8、INT4。
2. **微缩放格式（microscaling）**：MXFP8、MXFP4、NVFP4——把一组元素共享一个缩放因子，精度更高。
3. **社区/第三方格式**：GPTQ、AWQ、GGUF、compressed-tensors、ModelOpt、TorchAO 等。

除了量化，vLLM 还有一整套**高性能内核**：优化过的注意力内核（FlashAttention、FlashInfer、FlashMLA、Triton 等）和优化过的 GEMM/MoE 内核（基于 CUTLASS、TRTLLM-GEN、CuTeDSL）。

#### 4.4.2 核心流程

量化在 vLLM 中有一个特别重要的设计：**「在初始化时就完成分片与量化」**，而不是先加载完整权重再改。原因（来自架构文档的例子）：

> 假设要在一组 80GB GPU 上跑一个约 810GB 权重的 405B 模型。如果先加载完整权重再分片，每张卡都要先装下完整 810GB 再切——显存根本放不下。若在初始化时就分片，每一层只创建自己需要的那一份，显存开销小得多。

量化同理：在初始化时直接以低精度创建权重，避免「先建高精度再转」的峰值显存翻倍。

一个粗略的显存节省估算（仅权重部分）：

\[
\text{显存占比} = \frac{\text{量化后每元素比特}}{\text{量化前每元素比特}}
\]

例如 FP16 \(\to\) INT4：

\[
\frac{4}{16} = 0.25
\]

即权重显存理论上可降到约 1/4（实际还会受激活、KV 缓存等影响）。

#### 4.4.3 源码精读

[README.md:34](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L34) —— README 的量化特性清单：FP8, MXFP8/MXFP4, NVFP4, INT8, INT4, GPTQ/AWQ, GGUF, compressed-tensors, ModelOpt, TorchAO 等。

[README.md:35-36](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L35-L36) —— 优化的注意力内核与 GEMM/MoE 内核列表（FlashAttention/FlashInfer/TRTLLM-GEN/FlashMLA/Triton、CUTLASS/CuTeDSL）。

[arch_overview.md:282-301](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L282-L301) —— 「Sharding and Quantization at Initialization」设计取舍：解释为什么要在初始化时分片/量化，并以 405B 模型为例说明峰值显存问题。

> 说明：量化配置体系（`vllm/model_executor/layers/quantization/`）会在 `u8-l4` 精讲。

#### 4.4.4 代码实践

1. **实践目标**：建立一个量化的定量直觉。
2. **操作步骤**：
   - 假设某模型权重在 FP16 下为 14 GB。
   - 分别估算它量化到 INT8、INT4 后的**纯权重视存**：
     - INT8：\(14 \times \frac{8}{16} = 7\) GB
     - INT4：\(14 \times \frac{4}{16} = 3.5\) GB
3. **需要观察的现象**：精度（比特数）每减半，权重视存大约减半。
4. **预期结果**：你能填出 7 GB 和 3.5 GB，并说出「省显存带来的代价是潜在精度下降」。
5. **待本地验证**：这是估算练习；真实模型还包含激活、KV 缓存、框架开销，实际显存会更高，**请以本地实测为准**。

#### 4.4.5 小练习与答案

**练习 1**：vLLM 为什么选择「在初始化时」而非「加载后」做量化/分片？
**参考答案**：加载后再改需要先在每张卡上装下完整高精度权重（如 810GB 的 405B 模型），峰值显存爆炸、放不下；初始化时就分片/量化，每层只创建自己那份，峰值显存小得多。

**练习 2**：把一个 FP16 模型量化到 FP8，权重视存大约变成原来的多少？
**参考答案**：约 \(8/16 = 0.5\)，即原来的一半。

---

## 5. 综合实践

把本讲 4 个模块串起来，完成下面这个阅读 + 表达任务：

1. **任务**：打开 [README.md:28-39](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L28-L39) 的「vLLM is fast with」清单。
2. **产出 1**：从中挑选 **3 项**你认为对「相比朴素 HuggingFace 推理提升最大」的性能优化，用 1~2 句话说明每项分别优化了什么（算力利用率？显存？还是带宽？）。建议覆盖：PagedAttention（显存）、连续批处理（算力利用率）、量化（显存+带宽）。
3. **产出 2**：用自己的话写一段不超过 5 行的文字，回答：**PagedAttention 到底解决了什么显存问题？** 要求出现「内部碎片」「按需分配」这两个词。
4. **自检**：回头对照本讲 4.2 节，确认你的描述没有把 PagedAttention 和连续批处理混淆——前者管「KV 显存怎么存」，后者管「GPU batch 怎么凑」。

> 这个任务不需要运行任何代码，是源码/文档阅读型综合实践。完成它，你就达成了本讲的全部学习目标。

## 6. 本讲小结

- vLLM 是一个面向**高吞吐 LLM 推理与服务**的库，核心价值是在相同硬件上把吞吐和延迟做到极致，同时保持易用、兼容大量 HuggingFace 模型。
- **PagedAttention** 借鉴操作系统分页思想，把 KV 缓存切成固定大小的块、按需分配，解决了朴素连续 KV 缓存的内部/外部碎片问题，大幅提升显存利用率。
- **连续批处理 + 分块预填充 + 前缀缓存** 让 GPU 每 step 都被填满、长 prefill 不再独占、相同前缀只算一次，三者都建立在「按块管理 KV」之上。
- **量化**（FP8/INT8/INT4 等）和一系列**优化内核**（FlashAttention、CUTLASS GEMM 等）压低显存与带宽需求；vLLM 选择在**初始化时**完成分片/量化以避免峰值显存爆炸。
- vLLM 的能力远不止于此——分布式并行（TP/PP/DP）、推测解码、多 LoRA、结构化输出、跨硬件等将在后续讲义逐一展开。
- 本讲引用的多为**文档**，真正的 Python 源码精读从下一单元开始。

## 7. 下一步学习建议

- 想跑起来看看：进入 **`u2-l1`（离线推理：LLM 类与 generate/chat）**，用 `vllm.LLM` 跑通第一次推理。
- 想搞懂怎么安装/构建：阅读 **`u1-l2`（安装与环境构建方式）**，理解 `pyproject.toml`、`uv`、预编译安装。
- 想建立目录地图：阅读 **`u1-l3`（仓库目录结构总览）**，为后续源码阅读打底。
- 想深入了解 PagedAttention 的实现：在学完 `u3`（架构与配置）后，直接跳到 **`u4-l4`（PagedAttention 与 KV 缓存管理）** 看 `KVCacheManager` / `BlockPool` 真实源码。

> 建议顺序：`u1-l2 → u1-l3 → u1-l4 → u2-*`，先建立地图、跑通推理，再沿「请求如何流动」的主线深入。
