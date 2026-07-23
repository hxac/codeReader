# 讲义标题：项目总览与定位 —— 走进 Mini-SGLang

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在还没有读任何源码细节之前，先建立对 Mini-SGLang 的整体认知。读完本讲，你应该能够：

- 说清楚 **Mini-SGLang 是什么**、它和上游项目 SGLang 是什么关系。
- 一口气列出它的 **核心特性**（Radix Cache、Chunked Prefill、Overlap Scheduling、Tensor Parallelism、FlashAttention/FlashInfer），并能用一句话解释每个特性解决什么问题。
- 说出它依赖哪些 **外部 CUDA 库**（sgl-kernel、flashinfer、tvm-ffi、quack-kernels），以及这些库大致负责什么。
- 知道它 **支持哪些模型、支持哪些平台**，哪些事情它现在不做。

本讲不涉及算法实现细节，所有结论都来自项目自身的 `README.md`、`docs/features.md` 和 `pyproject.toml`。后续讲义才会逐层深入源码。

---

## 2. 前置知识

本讲面向零基础读者，但有几个名词先做个通俗铺垫，后面会反复出现：

- **LLM 推理（Inference）**：把一个已经训练好的大语言模型加载到显卡上，让它根据输入文本生成输出。和「训练」相对，推理不更新模型权重，只做前向计算。
- **KV Cache**：Transformer 在生成每一个 token 时，都要用到之前所有 token 算出的 Key/Value。把这些 K/V 缓存下来避免重复计算，是现代推理框架提速的关键。本讲的 Radix Cache、Chunked Prefill 都围绕它展开。
- **Prefill / Decode**：一次生成通常分两阶段。Prefill 阶段一次性「读入」整段提示词（prompt）并算出 KV；Decode 阶段则一个一个地「吐」出新 token。两阶段的计算特性很不一样，所以框架常对它们做不同优化。
- **Tensor Parallelism（张量并行，TP）**：把模型权重切成几份，分别放在多张 GPU 上同时计算，再把结果合并。用 `--tp 4` 表示用 4 张卡。
- **CUDA kernel**：跑在 NVIDIA GPU 上的底层高性能算子。Mini-SGLang 自己不写全部算子，而是调用社区现成的高性能库。

如果你对其中某几个还不熟，没关系，本讲只需要建立直觉，细节会在后面单元展开。

---

## 3. 本讲源码地图

本讲只读三份「项目说明书」性质的非代码文件，它们是了解项目全貌最快的入口：

| 文件 | 作用 | 本讲用它来回答什么 |
| --- | --- | --- |
| `README.md` | 项目首页，给出定位、特性、安装、启动、基准 | 项目是什么、有什么特性、怎么跑 |
| `docs/features.md` | 每个特性的详细说明和对应的命令行参数 | 每个特性对应哪个 CLI 开关、支持哪些模型 |
| `pyproject.toml` | Python 包定义，列出全部运行期依赖和开发依赖 | 项目依赖哪些库、这些库分别是什么 |

补充一个仓库整体结构供心里有数（后续讲义会逐个深入）。项目的 Python 源码集中在 `python/minisgl/` 下，按职责拆成 14 个子包：

```
python/minisgl/
├── server/      # FastAPI 前端、启动器、CLI 参数
├── tokenizer/   # tokenize / detokenize 进程
├── scheduler/   # 调度器主循环、prefill/decode 排队
├── engine/      # 执行引擎、前向、采样、CUDA Graph
├── kvcache/     # KV Cache 池、Radix 基数树、CacheManager
├── attention/   # 注意力后端抽象与 fa/fi/trtllm 实现
├── layers/      # Linear/Embedding/Norm/RoPE 等基础层
├── models/      # Llama/Qwen3 等模型实现与权重加载
├── distributed/ # 张量并行通信（NCCL / PyNCCL）
├── moe/         # Fused MoE 专家网络
├── kernel/      # tvm-ffi JIT 的自定义算子入口
├── message/     # 进程间消息与序列化
├── llm/         # 离线推理 LLM 接口
└── utils/       # 杂项工具
```

> 提示：这一节的目录结构只是为了建立「全景图」，本讲不会深入任何一个子包。第 u1-l3 讲会专门讲目录与模块地图。

---

## 4. 核心概念与源码讲解

### 4.1 项目定位

#### 4.1.1 概念说明

Mini-SGLang 是一个**面向大语言模型（LLM）的轻量级、高性能推理框架**。它有两个并列的身份：

1. **一个能用的推理引擎**：你确实可以用它部署模型、对外提供服务，它追求的是「state-of-the-art」的吞吐和延迟。
2. **一份透明可读的教学参考实现**：它把现代 LLM 服务系统的复杂机制压缩到 **约 5000 行 Python** 代码里，让研究者和开发者能真正读懂「一个工业级推理框架是怎么搭起来的」。

它和上游 [SGLang](https://github.com/sgl-project/sglang) 的关系是：**Mini-SGLang 是 SGLang 的精简实现**（a compact implementation of SGLang）。SGLang 是功能完整的生产级框架，代码量大；Mini-SGLang 砍掉了大量工程化的边角，但保留了核心思想，便于学习。

#### 4.1.2 核心流程

从「用户视角」看，Mini-SGLang 在整个 LLM 应用栈里所处的位置可以这样理解：

```
你的应用 / OpenAI 客户端
        │  HTTP (OpenAI 兼容接口)
        ▼
┌────────────────────────────┐
│      Mini-SGLang 服务       │  ← 本项目：负责把模型高效地跑起来
│  (调度 + 执行 + KV Cache)   │
└────────────────────────────┘
        │  调用 CUDA kernel
        ▼
   NVIDIA GPU (驱动 + CUDA Toolkit)
```

换言之，Mini-SGLang 处于「业务应用」和「裸 GPU」之间，它替你解决：怎么把请求排队、怎么管理显存里的 KV Cache、怎么把模型切到多张卡上、怎么调用最快的注意力算子。

#### 4.1.3 源码精读

项目的自我定位写在 README 开头。第一句直接点明它的性质：

[README.md:5-7](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md#L5-L7) —— 标题与一句话定位：**「A lightweight yet high-performance inference framework for Large Language Models.」**（一个轻量但高性能的 LLM 推理框架）。

紧接着的一段给出了它与 SGLang 的关系以及「双身份」定位：

[README.md:11](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md#L11) —— 说明它是 SGLang 的 compact 实现，代码量约 5000 行 Python，既是推理引擎也是透明参考。

#### 4.1.4 代码实践

**实践目标**：用只读 git 命令亲自验证「约 5000 行 Python」这个说法，建立对项目体量的直觉。

**操作步骤**：

1. 在仓库根目录执行（统计 `python/minisgl` 下的 Python 代码行数）：
   ```bash
   git ls-files 'python/minisgl/*.py' | xargs wc -l | tail -1
   ```
2. 再看一下源码文件总数：
   ```bash
   git ls-files 'python/minisgl/*.py' | wc -l
   ```

**需要观察的现象**：总行数应当在几千行的量级（与 README 宣称的「~5,000 行」同一数量级），文件数量大概几十个。

**预期结果**：你会直观感受到「这是一个能被一个人完整读完的项目」，这正是它适合作为学习材料的原因。

> 如果无法运行（例如没有 git 仓库），明确标注「待本地验证」即可，不要编造数字。

#### 4.1.5 小练习与答案

**练习 1**：Mini-SGLang 和 SGLang 是什么关系？为什么不直接学 SGLang？
> **参考答案**：Mini-SGLang 是 SGLang 的精简实现。SGLang 是功能完整的生产级框架，代码量大、工程细节多；Mini-SGLang 把核心机制压缩到约 5000 行，删去了大量边角工程，便于学习者真正读懂整套推理系统的设计，同时仍保留可用的高性能。

**练习 2**：Mini-SGLang 的两个并列身份是什么？
> **参考答案**：① 一个可实际部署、追求高吞吐低延迟的推理引擎；② 一份透明、可读、供研究和学习用的参考实现。

---

### 4.2 核心特性清单

#### 4.2.1 概念说明

README 在「Key Features」里把卖点归纳成三层：高性能、轻量可读、以及一组**进阶优化**。其中最关键的是这 5 个优化，它们构成了 Mini-SGLang 区别于「朴素推理循环」的核心价值，也基本对应了本手册后续要逐个深入的主题：

| 特性 | 一句话作用 | 后续讲义 |
| --- | --- | --- |
| **Radix Cache** | 用基数树复用不同请求之间**共享前缀**的 KV，避免重复算 | u6 |
| **Chunked Prefill** | 把超长 prompt **切成小块**分批 prefill，降低峰值显存、防 OOM | u4 |
| **Overlap Scheduling** | 让 CPU 调度开销与 GPU 计算**重叠**执行，隐藏 CPU 等待 | u4 |
| **Tensor Parallelism** | 把模型分到**多张 GPU** 上并行，提升算力 | u9 |
| **Optimized Kernels** | 集成 **FlashAttention / FlashInfer** 等高性能注意力算子 | u7 |

#### 4.2.2 核心流程

这 5 个特性并非彼此孤立，它们在一条请求的生命周期里协同工作。一个简化图景：

```
请求到达
  │
  ├─ Chunked Prefill：长 prompt 被切成多块逐块送入 ──┐
  │                                                  │
  ├─ Radix Cache：先查有没有共享前缀可复用 KV ────────┤  调度阶段
  │                                                  │ （CPU）
  ├─ Overlap Scheduling：上面这些调度与下面的 GPU 计算重叠
  │                                                  │
  ├─ Optimized Kernels(FA/FI)：在 GPU 上跑注意力 ─────┤  执行阶段
  │                                                  │ （GPU）
  └─ Tensor Parallelism：上面计算被切分到多张卡 ──────┘
```

直觉上：前三个特性主要在「省时间、省显存、藏延迟」，后两个主要在「把算力堆上去」。后续单元会逐一拆解它们的实现。

#### 4.2.3 源码精读

README 的「Advanced Optimizations」一节把这 5 个特性列得很清楚：

[README.md:17-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md#L17-L23) —— 逐条列出 Radix Cache、Chunked Prefill、Overlap Scheduling、Tensor Parallelism 与 Optimized Kernels（FlashAttention/FlashInfer），并各附一句话说明。

每个特性在 `docs/features.md` 里都有更详细的段落，并给出对应的命令行开关。例如 Radix Cache 默认开启，可用 `--cache naive` 切换：

[docs/features.md:47-49](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L47-L49) —— 说明 Radix Cache 默认启用，可用 `--cache naive` 改用朴素策略。

Overlap Scheduling 同样默认开启，可以通过环境变量关掉做消融实验：

[docs/features.md:54-56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L54-L56) —— 说明 Overlap Scheduling 借鉴自 NanoFlow，把 CPU 调度与 GPU 计算重叠。

#### 4.2.4 代码实践

**实践目标**：把「特性 → 命令行开关」对应起来，熟悉如何通过参数控制这些特性。

**操作步骤**：

1. 阅读 `docs/features.md` 全文，逐节找出每个特性对应的 CLI 参数。可重点对照这些段落：
   - Chunked Prefill 对应 `--max-prefill-length`：[docs/features.md:29-31](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L29-L31)
   - Page Size 对应 `--page-size`：[docs/features.md:33-35](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L33-L35)
   - Attention Backends 对应 `--attn`：[docs/features.md:37-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L37-L41)
   - CUDA Graph 对应 `--cuda-graph-max-bs`：[docs/features.md:43-45](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L43-L45)
2. 运行 `python -m minisgl --help` 查看这些参数是否真的出现在帮助里（**待本地验证**：需要先完成安装，见下一讲 u1-l2）。

**需要观察的现象**：每个特性都能找到一个对应的开关，并且大多「默认开启」。

**预期结果**：整理出一张「特性 → CLI 参数 → 默认值」对照表，例如：CUDA Graph 默认开，`--cuda-graph-max-bs 0` 可关闭。

#### 4.2.5 小练习与答案

**练习 1**：Radix Cache 和 Chunked Prefill 各解决什么不同的问题？
> **参考答案**：Radix Cache 解决「不同请求有共享前缀时，重复算 KV 浪费算力」的问题，靠复用已算好的 KV；Chunked Prefill 解决「单个超长 prompt 一次性 prefill 会撑爆显存」的问题，靠把 prompt 切小块分批处理。前者省算力，后者防 OOM。

**练习 2**：为什么 Overlap Scheduling 能提升吞吐？
> **参考答案**：调度（选哪些请求、分配显存页等）发生在 CPU 上，而模型计算在 GPU 上。如果串行执行，CPU 调度时 GPU 空闲。Overlap Scheduling 把「处理上一批结果/准备下一批」的 CPU 工作与「当前批的 GPU 计算」重叠起来，从而隐藏 CPU 开销，提升整体吞吐。

---

### 4.3 技术栈与依赖

#### 4.3.1 概念说明

Mini-SGLang 本身是用 **Python** 写的（要求 Python ≥ 3.10），但它不是一个「纯 Python」项目——它的高性能严重依赖一批**外部 CUDA 库**。理解依赖结构，就理解了项目的「分工」：

- **基础框架**：PyTorch（`torch`）做张量计算和 GPU 调度。
- **模型生态**：HuggingFace `transformers` 用来读取模型配置和 tokenizer；`accelerate`、`modelscope` 辅助加载。
- **高性能算子（关键外部 CUDA 库）**：`flashinfer-python`、`sgl_kernel`、`apache-tvm-ffi`、`quack-kernels`。
- **服务与通信**：`fastapi` + `uvicorn` 提供 HTTP 服务；`pyzmq` 做进程间通信；`msgpack` 做消息序列化。
- **交互**：`prompt_toolkit` 支撑交互式 shell；`openai` 客户端用于基准测试。

#### 4.3.2 核心流程

这些依赖在运行时大致分层协作：

```
        HTTP 请求 (fastapi + uvicorn)
                 │
   进程间消息 (pyzmq + msgpack)
                 │
  ┌──────────────┴───────────────┐
  │       PyTorch (torch)         │  张量与模型
  └──────────────┬───────────────┘
                 │
   高性能算子 (flashinfer / sgl_kernel / tvm-ffi / quack)
                 │
              GPU 计算
```

模型配置/tokenizer 由 `transformers` 读入，张量与模型前向由 `torch` 驱动，而真正「快」的那部分计算（注意力、KV 写入、通信等）被外包给上面那几个专用 CUDA 库。这也是为什么下一节会强调「平台限制」：这些库只在 Linux + NVIDIA GPU 上好用。

#### 4.3.3 源码精读

运行期依赖完整列表在 `pyproject.toml`：

[pyproject.toml:24-39](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L24-L39) —— `dependencies` 数组，其中关键的 CUDA 库包括 `flashinfer-python>=0.5.3`、`apache-tvm-ffi>=0.1.4`、`sgl_kernel>=0.3.17.post1`、`quack-kernels`；此外还有 `torch<2.10.0`、`transformers>=4.56.0,<=4.57.3`、`fastapi`、`pyzmq`、`msgpack` 等。

开发期依赖（测试与质量工具）单独列在可选依赖里：

[pyproject.toml:41-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L41-L52) —— `dev` 可选依赖，包含 `pytest`、`pytest-cov`、`black`、`ruff`、`mypy`、`pre-commit` 等，说明项目用 pytest 做测试、用 ruff/black/mypy 保证代码质量。

项目基本信息也在这里：

[pyproject.toml:5-11](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L5-L11) —— 包名 `minisgl`，版本 `0.1.0`，要求 `requires-python = ">=3.10"`，MIT 许可证。

#### 4.3.4 代码实践

**实践目标**：把每个外部 CUDA 库与它「大致负责的事」对应起来，建立依赖的心智模型。

**操作步骤**：

1. 对照 [pyproject.toml:24-39](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L24-L39) 的依赖列表。
2. 在仓库里搜索这些库名的 import，看它们被哪里使用。例如（只读搜索）：
   ```bash
   # 在 python/minisgl 下搜索 flashinfer 的引用位置
   grep -rn "import flashinfer" python/minisgl
   ```
3. 结合搜索结果，给每个 CUDA 库写一句话职责猜测。

**需要观察的现象**：`flashinfer` 主要出现在 `attention/`（注意力后端）；`sgl_kernel` / `tvm-ffi` / `quack` 主要出现在 `kernel/`、`layers/`、`moe/` 等需要高性能算子的地方。

**预期结果**：一张「库名 → 大致职责 → 主要使用位置」表格。注意：精确职责要到后续单元才能完全确认，本讲只做粗粒度映射。

> 如果没有 GPU 环境无法实际跑搜索，可以仅阅读 `python/minisgl/kernel/__init__.py` 等入口文件来推断，并标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：Mini-SGLang 为什么不把所有算子都用纯 PyTorch 实现，而要依赖 sgl-kernel、flashinfer 等外部库？
> **参考答案**：纯 PyTorch 算子通用但不是最优。注意力、KV 写入、MoE 等热点计算有专门的融合/分块算法（如 FlashAttention），社区专门的 CUDA/Triton 库实现得远比朴素 PyTorch 快。依赖这些库才能达到「state-of-the-art」的吞吐延迟，这也是项目「高性能」定位的来源。

**练习 2**：`pyzmq` 和 `msgpack` 在这个项目里分别扮演什么角色？
> **参考答案**：`pyzmq` 提供 ZMQ 进程间/进程内消息队列，Mini-SGLang 用它在多个进程（API Server、Tokenizer、Scheduler 等）之间传递消息；`msgpack` 负责把消息对象序列化成字节流再传输。一个管「怎么传」，一个管「怎么编码」。

---

### 4.4 支持模型与平台限制

#### 4.4.1 概念说明

了解一个推理框架，还要知道它的「能力边界」：支持哪些模型、跑在什么平台上。对 Mini-SGLang：

- **模型范围**：目前支持三类 **dense（稠密）** 模型架构——Llama-3 系列、Qwen-3 系列（含 MoE 版）、Qwen-2.5 系列。
- **平台范围**：**只支持 Linux**（x86_64 与 aarch64），不支持原生 Windows / macOS，原因是它依赖的 Linux 专用 CUDA kernel（`sgl-kernel`、`flashinfer`）。Windows 用户需走 WSL2 或 Docker。
- **硬件要求**：需要 **NVIDIA GPU** 和匹配版本的 **CUDA Toolkit**，因为关键算子是 JIT 编译的 CUDA 代码。

#### 4.4.2 核心流程

「能不能跑某个模型」取决于两个环节是否都满足：

```
模型架构是否在支持列表里？
        │ 是            │ 否
        ▼               ▼
 平台/硬件是否满足？    需要自行接入新模型（见 u10-l3）
   │ 是            │ 否
   ▼               ▼
 可以直接部署       用 WSL2/Docker 或换硬件
```

换句话说：模型要在白名单内，且运行环境要满足 Linux + NVIDIA CUDA。两个条件缺一不可。

#### 4.4.3 源码精读

支持的模型清单写在 `docs/features.md`：

[docs/features.md:21-27](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L21-L27) —— 明确当前支持 Llama-3、Qwen-3（含 MoE）、Qwen-2.5 三类 dense 模型架构。

平台限制写在 README 的 Quick Start 顶部醒目提示里：

[README.md:27](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md#L27) —— 说明仅支持 Linux（x86_64/aarch64），Windows/macOS 因依赖 Linux 专用 CUDA kernel（`sgl-kernel`、`flashinfer`）而不被支持，建议 Windows 用户用 WSL2 或 Docker。

CUDA Toolkit 的前置要求在同节：

[README.md:39](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md#L39) —— 强调依赖 JIT 编译的 CUDA kernel，需要安装 NVIDIA CUDA Toolkit 且版本与驱动匹配，可用 `nvidia-smi` 查看。

#### 4.4.4 代码实践

**实践目标**：确认项目支持模型清单在源码层面的依据，而不仅停留在文档。

**操作步骤**：

1. 在 `python/minisgl/models/` 目录下列出已实现的模型文件（只读）：
   ```bash
   ls python/minisgl/models/
   ```
2. 对照 [docs/features.md:21-27](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md#L21-L27) 的声明，看文档里说的三类架构是否都能找到对应实现文件（例如 `llama.py`、`qwen3.py`、`qwen3_moe.py` 等）。

**需要观察的现象**：文档宣称支持的每个模型系列，在 `models/` 目录下应能找到对应的实现文件。

**预期结果**：建立「文档声明 → 实际实现文件」的对应关系，理解到「支持某模型」本质上是「有对应的模型实现代码 + 权重加载规则」。这部分会在 u8、u10 详细讲。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Mini-SGLang 在原生 Windows 上跑不起来？
> **参考答案**：因为它依赖 `sgl-kernel`、`flashinfer` 等 Linux 专用的 CUDA kernel，这些库在原生 Windows 上不可用。解决办法是用 WSL2（在 Windows 里跑 Linux）或 Docker 容器。

**练习 2**：文档说支持「dense 模型架构」，但 Qwen-3 又标注「including MoE」，这矛盾吗？
> **参考答案**：不矛盾。「dense 架构」是相对于「需要额外推理运行时（如 MoE 的特殊调度）」而言的总体范畴；Qwen3 的 MoE 变体作为已支持的特例被纳入。Mini-SGLang 内部有专门的 Fused MoE 实现（见 u10-l1）来处理这类模型的专家网络。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这份「能力边界」清单。这是本讲的核心实践任务。

**实践目标**：通读 README 与 `docs/features.md`，产出一份结构化的「Mini-SGLang 能做什么 / 不能做什么」文档，并说清它依赖哪些外部 CUDA 库。

**操作步骤**：

1. 通读 [README.md](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md) 与 [docs/features.md](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/features.md) 全文。
2. 建立一张「能做什么 / 不能做什么」对照表，例如：
   - **能做**：OpenAI 兼容的在线服务、交互式 shell、多卡张量并行、Radix Cache 共享前缀复用、Chunked Prefill、Overlap Scheduling、CUDA Graph、多种注意力后端（fa/fi/trtllm）……
   - **不能做（当前限制）**：不支持原生 Windows/macOS、不支持列表外的模型架构（需自行接入）、不支持非 NVIDIA GPU……
3. 从 [pyproject.toml:24-39](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L24-L39) 中挑出**外部 CUDA 库**，为每个写一句话职责说明：
   - `sgl_kernel` —— SGLang 社区的高性能算子集合（融合算子、通信等）。
   - `flashinfer-python` —— FlashInfer，提供 paged KV 的高效注意力，常用于 decode。
   - `apache-tvm-ffi` —— tvm-ffi，用于 JIT 编译并加载项目自定义的 CUDA/Triton kernel（见 `kernel/` 包）。
   - `quack-kernels` —— 面向特定硬件（如 AMD / 特定后端）的补充算子库。
4. 对照第 3 节的子包地图，给每个 CUDA 库标注它最可能服务的子包（如 flashinfer → `attention/`，tvm-ffi → `kernel/`），并标注「待后续讲义确认」。

**需要观察的现象**：你会发现「高性能」几乎全部建立在这几个外部 CUDA 库之上；一旦离开 Linux + NVIDIA 平台，这些库失效，项目也就无法运行。

**预期结果**：一份一页纸的「能力边界 + 依赖职责」清单，作为后续深入源码前的参照框架。

> 说明：本实践是「文档阅读型实践」，不需要 GPU 即可完成对文档和依赖的整理；对源码层面的职责确认，标注「待本地验证 / 待后续讲义确认」即可。

---

## 6. 本讲小结

- Mini-SGLang 是 SGLang 的**精简实现**，约 5000 行 Python，既是可用的高性能 LLM 推理引擎，也是透明可读的教学参考。
- 五大核心特性：**Radix Cache**（复用共享前缀 KV）、**Chunked Prefill**（切块降峰值显存）、**Overlap Scheduling**（CPU 调度与 GPU 计算重叠）、**Tensor Parallelism**（多卡并行）、**Optimized Kernels**（FlashAttention/FlashInfer）。
- 项目用 **Python ≥ 3.10**，但高性能依赖一批外部 CUDA 库：`sgl_kernel`、`flashinfer-python`、`apache-tvm-ffi`、`quack-kernels`，外加 `torch`、`transformers`、`fastapi`、`pyzmq` 等。
- 支持的模型为 **dense 架构**：Llama-3、Qwen-3（含 MoE）、Qwen-2.5；平台仅限 **Linux + NVIDIA GPU**，Windows 用户需用 WSL2/Docker。
- 源码集中在 `python/minisgl/` 下 14 个按职责划分的子包，本讲只建立全景图，后续逐层深入。
- 每个特性都对应明确的 CLI 开关（如 `--cache`、`--attn`、`--cuda-graph-max-bs`、`--tp`），且大多默认开启。

---

## 7. 下一步学习建议

本讲只建立了「项目是什么」的整体认知，还没有真正运行它。建议按以下顺序继续：

1. **u1-l2 安装与快速运行**：亲手把 Mini-SGLang 装起来，用 `python -m minisgl` 启动一次服务、用 `curl` 调一次接口、体验 `--shell` 交互模式，把本讲的特性变成可操作的现实。
2. **u1-l3 目录结构与模块地图**：结合 `docs/structures.md`，把本讲第 3 节给出的 14 个子包职责表细化，建立「目录 → 功能 → 关键文件」的精确映射。
3. **u1-l4 进程架构与请求生命周期**：理解在线服务背后「API Server / Tokenizer / Detokenizer / Scheduler」的多进程分工，这是理解后续所有调度、执行、KV Cache 机制的前提。

在阅读后续讲义时，建议随时回到本讲的「特性 → 子包」对照表，把新学的实现细节挂回到这张全景图上。
