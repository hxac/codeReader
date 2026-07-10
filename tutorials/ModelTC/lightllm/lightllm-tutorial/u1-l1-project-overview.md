# LightLLM 项目总览与定位

> 本讲是 LightLLM 学习手册的第一篇，面向**完全没接触过这个项目**的读者。
> 你不需要先会跑它，只要读完这一篇，就能回答三个问题：**它是谁、它解决什么问题、它最值得学的地方在哪里。**

---

## 1. 本讲目标

读完本讲，你应当能够：

1. 用一句话说清 **LightLLM 是什么**，以及它在大模型推理生态中的定位。
2. 说出它区别于 vLLM / TGI / FasterTransformer 的 **三大技术特色**（纯 Python、token 级 KV Cache 管理、多进程架构）。
3. 从 `setup.py` 中读出当前**版本号、Python 版本要求与核心依赖**，理解 `install_requires` 与 `requirements.txt` 的差别。
4. 知道项目背后的**论文与参考实现**在哪里，为后续学习找好入口。

本讲只覆盖两个最小模块：**项目说明** 和 **版本与依赖**。具体怎么安装、怎么启动服务，会在下一篇 `u1-l2-install-and-quickstart.md` 中讲。

---

## 2. 前置知识

在开始之前，建议你先具备以下常识（不熟悉也没关系，下面会顺带解释）：

| 概念 | 一句话解释 |
| --- | --- |
| LLM（大语言模型） | 像 Llama、Qwen、DeepSeek 这样的生成式语言模型，输入一串 token（词片），输出后续 token。 |
| 推理（Inference） | 用已经训练好的模型做前向计算、生成回答的过程，区别于训练。 |
| 服务框架（Serving Framework） | 把"单次推理"包装成一个**常驻服务**，对外提供 HTTP API，能并发处理很多请求的程序框架。 |
| KV Cache | Transformer 自回归生成时，把每层注意力的 K、V 缓存下来，避免对历史 token 重复计算的关键数据结构。 |
| 张量并行（Tensor Parallel, TP） | 把一个大模型的权重按维度切分到多张 GPU 上，每张卡算一部分，再通信合并。 |

如果你对 vLLM、TGI 这类项目略有耳闻，会更容易理解本讲的"定位"对比部分；但即便完全没听过，也不影响继续往下读。

---

## 3. 本讲源码地图

本讲涉及的关键文件非常少，都是项目最外层的"门面文件"：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md) | 项目的"自我介绍"：定位、特色、版本动态、生态与参考实现。 |
| [setup.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py) | Python 打包脚本，定义了**包名、版本、Python 要求和核心依赖**。 |
| requirements.txt | （辅助）完整锁定版本的依赖清单，比 `setup.py` 的 `install_requires` 更具体。 |
| lightllm/ | （辅助）源码包根目录，下设 `common / distributed / models / server / utils` 五个子模块。 |

> 提示：本讲引用的永久链接都基于当前 HEAD `5d59e490`，方便你点开直接对照阅读。

---

## 4. 核心概念与源码讲解

### 4.1 项目说明

#### 4.1.1 概念说明

**LightLLM 是一个用于大语言模型推理与服务的 Python 框架。**

这句话里有三个关键词需要拆开理解：

- **推理（Inference）**：模型已经训练好，我们要用它来生成文本。推理阶段关心的是"算得快、吞吐高、显存省"，而不是梯度更新。
- **服务（Serving）**：把推理能力做成一个**一直开着的进程**，通过 HTTP 接口接收请求（例如 OpenAI 风格的 `/v1/chat/completions`），同时处理来自多个用户的并发请求，再把生成的 token 流式返回。
- **框架（Framework）**：它不是某一个模型，而是一套**通用骨架**，只要按规则接入新模型，就能复用同一套调度、KV 管理、采样的能力。实际上 LightLLm 目前支持了 **44 个模型族**（如 llama、qwen3、deepseek2、glm4_moe_lite 等），你可以在 `lightllm/models/` 目录下看到全部。

为什么需要专门做一个框架，而不是直接用 HuggingFace `transformers` 跑模型？因为原生 `transformers` 是为"单条、顺序"的实验设计的，而真实服务需要**批处理、并发调度、KV Cache 复用、多卡并行**，这些工程能力都需要框架来提供。

#### 4.1.2 核心流程（LightLLM 如何自我定位）

LightLLM 官方对自己的定位可以用下面这张"对照表"概括，它强调三点：

```
        ┌─────────────────────────────────────────────┐
        │             用户 HTTP 请求                   │
        │   /generate   /v1/chat/completions   ...     │
        └───────────────────────┬─────────────────────┘
                                ▼
        ┌─────────────────────────────────────────────┐
        │  HttpServer (接收/tokenize/回流文本)          │  ← 进程 1
        ├─────────────────────────────────────────────┤
        │  Router (调度循环：组 batch、选 token 配额)   │  ← 进程 2
        ├─────────────────────────────────────────────┤
        │  ModelBackend (每张 GPU 一个，做真实前向计算) │  ← 进程 3..N
        ├─────────────────────────────────────────────┤
        │  Detokenization (token→文本，流式推回 http)   │  ← 进程 N+1
        └─────────────────────────────────────────────┘
```

它的三个核心卖点：

1. **纯 Python 设计**：不像 FasterTransformer 那样以 C++/CUDA 为主体，LightLLM 主体逻辑用 Python 写，**易于阅读、修改和二次开发**，性能关键路径才下沉到 Triton/CUDA kernel。
2. **token 级 KV Cache 管理**：KV Cache 的分配/回收粒度精确到**单个 token**（而不是按 batch 或按序列），这让显存利用更紧凑，也为 chunked prefill、RadixCache 前缀复用打下基础。
3. **多进程协作架构**：HTTP 接入、调度、推理、反 token 化由**不同进程**承担，进程间用 zmq + rpyc + 共享内存通信，职责清晰，便于分别优化。

这三点也正是后续讲义要逐层展开的主线。

#### 4.1.3 源码精读

**(1) 项目自我介绍——最权威的一句话定位**

README 第 18 行直接给出了项目的官方定义：

> LightLLM is a Python-based LLM (Large Language Model) inference and serving framework, notable for its lightweight design, easy scalability, and high-speed performance. LightLLM harnesses the strengths of numerous well-regarded open-source implementations, including but not limited to FasterTransformer, TGI, vLLM, and FlashAttention.

[README.md:18](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L18) — 这是整个项目最权威的"一句话定位"，强调三大特色：**lightweight（轻量）、easy scalability（易扩展）、high-speed performance（高性能）**，并明确承认它吸收了 FasterTransformer / TGI / vLLM / FlashAttention 的优点。

**(2) "纯 Python + token 级 KV Cache"是官方亲口强调的设计哲学**

README 在介绍它为何适合做研究基础时写道：

> Also, LightLLM's pure-python design and token-level KC Cache management make it easy to use as the basis for research projects.

[README.md:63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L63) — 这里直接点名了"纯 Python 设计"和"token 级 KV Cache 管理"（原文 "KC Cache" 是笔误，实为 KV Cache）。这是 LightLLM 区别于其他框架最本质的两条设计取舍。

**(3) 版本动态与里程碑**

README 的 News 区记录了项目主要版本节点：

[README.md:26-31](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L26-L31) — 这里能看到：`v1.0.0` 在 2025/02 发布，主打"单台 H200 上最快的 DeepSeek-R1 服务"；最新 `v1.1.0` 在 2025/09 发布。**本讲基于的 HEAD 处于 v1.1.0 线。**

**(4) 学术成果（论文）——理解设计动机的钥匙**

LightLLM 围绕多个组件发表了论文，这些论文解释了某些模块为什么这样设计：

[README.md:99-121](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L99-L121) — 这里列出了两篇代表性论文：
- **约束解码**：Pre³（ACL2025 杰出论文奖），解释了结构化输出能力。
- **请求调度器**：Past-Future Scheduler（ASPLOS'25），解释了 router 的调度策略。

> 提示：你不需要现在去读这些论文，只要记住：当后续讲义讲到"调度"或"约束解码"时，背后都有对应论文支撑，可回头深挖。

**(5) 参考实现——LightLLM 站在谁的肩膀上**

[README.md:83-92](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L83-L92) — 致谢清单说明 LightLLM 借鉴了 FasterTransformer、TGI、vLLM、SGLang、flashinfer、FlashAttention、OpenAI Triton。**理解这条很重要**：当你发现 LightLLM 某个 kernel 或思路眼熟，往往就是从这里来的；反过来，vLLM、SGLang 也引用了 LightLLM 的 kernel（见 [README.md:54-57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L54-L57)），说明这是一个**双向互鉴**的生态。

#### 4.1.4 代码实践

> **实践目标**：建立对 LightLLM 定位的准确认知，能用一段话向别人讲清楚它和主流框架的差异。

**操作步骤**：

1. 打开并通读 [README.md](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md)（全文不长，约 120 行）。
2. 重点看三处：第 18 行的自我介绍、第 63 行的设计哲学、第 83–92 行的致谢/参考实现。
3. 带着下面两个问题做笔记：
   - LightLLM 说自己"轻量、易扩展"，**靠什么实现**？（提示：纯 Python 主体 + 模块化）
   - 它和 vLLM / TGI 的**根本差异**是什么？（提示：实现语言主体、KV Cache 粒度、进程组织方式）

**需要观察的现象 / 预期结果**：

完成下面这段"填空式"总结（这是本讲实践任务的核心产出）：

> LightLLM 与 vLLM / TGI 的主要差异在于：______（例如：vLLM 以高度优化的 C++/CUDA 为主、追求极致吞吐；TGI 偏向 HuggingFace 生态的服务封装；而 LightLLM 以 ______ 为主体，强调 ______ 和 ______，因而更便于阅读与二次开发）。它最核心的三个技术特色是：① ______；② ______；③ ______。

参考填法（建议自己先写，再对照）：

> ……而 LightLLM 以**纯 Python**为主体，强调**轻量、易扩展**和**token 级 KV Cache 管理**，因而更便于阅读与二次开发。三个核心特色：① 纯 Python 设计；② token 级 KV Cache 管理；③ 多进程协作架构（HttpServer/Router/ModelBackend/Detokenization 分离）。

**说明**：这是一次**源码阅读 + 归纳型实践**，不需要运行任何命令，重点是把你对项目的认知固化成文字。后续讲义会有大量需要真正跑起来的实践。

#### 4.1.5 小练习与答案

**练习 1**：README 里说 LightLLM "harnesses the strengths of numerous well-regarded open-source implementations"。请列出至少 4 个它参考的项目，并各用一句话说明它们大致贡献了什么。

**参考答案**：
- **FasterTransformer**：高度优化的 Transformer 推理实现思路（C++/CUDA）。
- **TGI（Text Generation Inference）**：服务化、tokenize/detokenize 与流式输出的工程经验。
- **vLLM**：PagedAttention 式的 KV 分页管理思想与高吞吐调度理念。
- **FlashAttention / flashinfer**：高性能注意力 kernel。
- **OpenAI Triton**：用 Python 写 GPU kernel 的方式，LightLLM 大量自定义算子用 Triton 实现。

**练习 2**：为什么说"纯 Python 主体"会让 LightLLM 更适合做研究基础？至少给出两点理由。

**参考答案**：
1. 研究者通常更熟悉 Python，纯 Python 主体让核心逻辑（调度、KV 管理、采样）可直接阅读和修改，不必改动底层 C++/CUDA。
2. 真正的性能关键路径才下沉为 Triton/CUDA kernel，形成"易改的逻辑层 + 高性能的算子层"的清晰分层，便于在逻辑层快速试验新算法。

**练习 3**：本讲基于的 HEAD 处于哪个大版本线？该版本的标志性能力是什么？

**参考答案**：处于 **v1.1.0** 线（见 [README.md:26-31](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L26-L31)）。其上一里程碑 `v1.0.0` 的标志是"单台 H200 上最快的 DeepSeek-R1 服务性能"。

---

### 4.2 版本与依赖

#### 4.2.1 概念说明

光知道"LightLLM 是什么"还不够，作为一个 Python 项目，它还携带着两个关键元信息：**版本号**和**依赖列表**。这两个信息决定了"你能装在哪、和什么库一起跑"。

先澄清三个容易混淆的概念：

| 概念 | 位置 | 作用 |
| --- | --- | --- |
| **包名 + 版本** | `setup.py` | 通过 `pip install lightllm` 安装时，对外暴露的身份与版本。 |
| **`install_requires`** | `setup.py` | 安装本包时**必需**的"最小依赖集合"，通常不锁死版本。 |
| **`requirements.txt`** | 项目根 | 一份**完整且锁死版本**的依赖清单，常用于复现某一确定环境。 |

简单记：`setup.py` 的依赖是"**最少能跑起来**"，`requirements.txt` 是"**官方推荐的确切环境**"。

#### 4.2.2 核心流程（依赖如何决定可运行性）

LightLLM 是一个**重 GPU、重通信**的推理框架，它的依赖反映了它的技术选型：

```
install_requires（setup.py，必需）
   │
   ├── pyzmq        → 进程间消息通信（zmq），多进程架构的基石
   ├── rpyc         → 跨进程远程调用（router ↔ model backend）
   ├── uvloop       → 高性能事件循环，http server 用
   ├── transformers → 加载 HF 格式的 tokenizer/config/权重
   ├── safetensors  → 安全高效地加载模型权重文件
   ├── einops       → 张量形状重排（注意力 reshape）
   ├── triton       → 用 Python 写/编译 GPU kernel
   ├── orjson       → 高速 JSON 序列化（HTTP 请求/响应）
   ├── ninja        → 编译加速（JIT 编译 kernel）
   └── packaging    → 版本号解析
```

可以看到，即使是"最小依赖"也已经把**多进程通信（pyzmq/rpyc）、HTTP 服务（uvloop）、模型加载（transformers/safetensors）、GPU 算子（triton）**四大支柱都点出来了。更重的可选依赖（如 `torch`、`flashinfer-python`、`cupy`、`nixl`）则放在 `requirements.txt` 里。

#### 4.2.3 源码精读

`setup.py` 非常短，但信息密度很高。我们逐段看。

**(1) 包名与版本**

```python
setup(
    name="lightllm",
    version="1.1.0",
    ...
```

[setup.py:5-6](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L5-L6) — 包名 `lightllm`，当前版本 `1.1.0`，与 README News 里的最新发布一致。

**(2) Python 版本要求**

```python
    python_requires=">=3.9.16",
```

[setup.py:18](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L18) — 要求 **Python 3.9.16 及以上**。这是你准备运行环境的第一条硬约束。

**(3) 核心依赖清单**

```python
    install_requires=[
        "pyzmq",
        "uvloop",
        "transformers",
        "einops",
        "packaging",
        "rpyc",
        "ninja",
        "safetensors",
        "triton",
        "orjson",
    ],
```

[setup.py:19-30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L19-L30) — 这 10 个就是上一节画的依赖图里的"必需集合"。注意这里**没有锁版本**（没有 `==`），意思是"只要装了这几个包即可"。这与 `requirements.txt` 形成对比——后者锁定了 `torch==2.11.0`、`transformers==5.8.0`、`flashinfer-python==0.6.12` 等确切版本（见 [requirements.txt:62-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt#L62-L83)）。

> **为什么 `setup.py` 里不锁版本？** 这是 Python 打包的常见做法：作为"被安装的库"，它只声明"我需要这些能力"，把具体版本选择权交给使用者；而 `requirements.txt` 是给"想完整复现官方环境"的人准备的精确清单。

**(4) 包数据与打包范围**

```python
package_data = {"lightllm": ["common/all_kernel_configs/*/*.json", "common/triton_utils/*/*/*/*/*.json"]}
packages=find_packages(exclude=("build", "include", "test", "dist", "docs", "benchmarks", "lightllm.egg-info")),
```

[setup.py:3](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L3) 与 [setup.py:7](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L7) — 这两行说明：LightLLM 在打包时，会带上若干 **JSON 配置文件**（kernel configs、triton utils）。这暗示了它的一些 kernel 行为是**配置驱动**的，这些 JSON 不是源代码，而是数据资源——这也是为什么 `package_data` 要显式声明。

#### 4.2.4 代码实践

> **实践目标**：亲手验证你即将安装的 LightLLM 版本与依赖，建立"环境体检"的直觉。

**操作步骤**（任选一种环境；若无 GPU/未安装，可只做第 1、4 步）：

1. 用 git 确认当前 HEAD 与版本一致性：
   ```bash
   git rev-parse HEAD
   # 期望前缀：5d59e490...
   grep 'version=' setup.py
   # 期望输出：version="1.1.0",
   ```
2. （若已 `pip install -e .`）查看已安装版本与元信息：
   ```bash
   pip show lightllm
   ```
   **预期**：`Version: 1.1.0`，`Requires:` 列出 pyzmq、uvloop、transformers 等。
3. 对比 `setup.py` 与 `requirements.txt` 的依赖差异：
   ```bash
   # 只看 setup.py 里 install_requires 的包名是否都出现在 requirements.txt 中
   ```
   **预期**：`pyzmq`、`rpyc`、`uvloop`、`transformers`、`safetensors`、`einops`、`triton`、`orjson` 都能在 `requirements.txt` 找到对应锁定版本（如 `pyzmq==25.1.1b2`、`rpyc==5.3.1`、`triton` 则由 `torch==2.11.0` 间接带入）。
4. 无论是否安装，**直接阅读** [requirements.txt](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/requirements.txt)，找出这三个"重量级"依赖及其用途：
   - `torch==2.11.0`：深度学习与 GPU 计算后端。
   - `flashinfer-python==0.6.12`：高性能注意力/采样后端。
   - `nixl==1.2.0`：PD 分离场景下的 KV 传输后端（见后续 u7 单元）。

**需要观察的现象 / 预期结果**：你能清楚回答"`pip install lightllm` 最少会拉哪些包"（即 `install_requires` 那 10 个），并理解为什么 `torch` 不在其中却必不可少（因为它是"运行时必需"但通常由使用者按 CUDA 版本自行安装）。

> 说明：步骤 2、3 需要 Python 环境就绪；如果你的环境尚未配置，可标注"待本地验证"并先完成纯阅读部分。

#### 4.2.5 小练习与答案

**练习 1**：LightLLM 当前版本号是多少？最低要求哪个 Python 版本？

**参考答案**：版本 `1.1.0`（[setup.py:6](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L6)），要求 Python `>=3.9.16`（[setup.py:18](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L18)）。

**练习 2**：`setup.py` 的 `install_requires` 里没有 `torch`，但 LightLLM 明显离不开它。为什么？应从哪里安装 `torch`？

**参考答案**：`torch` 是**运行时必需但版本强依赖 CUDA**的库，不同机器的 CUDA 版本不同，所以官方不在 `install_requires` 里锁它，而把选择权交给使用者。正确做法是按你机器的 CUDA 版本（如 CUDA 12.x / 13.x）从 PyTorch 官方渠道安装对应 wheel；`requirements.txt` 里给出的 `torch==2.11.0` 是官方推荐的确切版本之一。

**练习 3**：`rpyc` 和 `pyzmq` 分别在多进程架构里扮演什么角色？请结合 `install_requires` 说明。

**参考答案**：`pyzmq` 提供**基于消息（PUB/SUB、REQ/REP 等）的进程间通信**，例如 router 把生成 token 推送给 detokenization、http server 接收回流文本；`rpyc` 提供**跨进程的远程方法调用**，例如 router 调用每张 GPU 上 ModelBackend 的 `prefill_batch` / `decode_batch`。两者一起构成了 LightLLM 多进程协作的通信底座。

---

## 5. 综合实践

把本讲两个模块串起来，完成一份**"LightLLM 项目体检报告"**（一页纸，纯文字）：

1. **定位**：用一句话定义 LightLLM（参考 4.1.1）。
2. **三大特色**：纯 Python、token 级 KV Cache、多进程架构，各配一句"为什么有用"。
3. **生态坐标**：列出它参考了谁、又被谁引用（参考 4.1.3 的致谢与互鉴段落）。
4. **环境基线**：写下版本 `1.1.0`、Python `>=3.9.16`，并列出你认为最关键的 5 个依赖及用途（从 `setup.py` 和 `requirements.txt` 中挑）。

完成后，建议你**对照本讲的"参考填法"自检**，并把这份报告保留下来——它是你后续阅读源码时的"坐标系"，每学完一篇讲义都可以回来更新一次。

---

## 6. 本讲小结

- LightLLM 是一个**纯 Python 的 LLM 推理与服务框架**，主打**轻量、易扩展、高性能**。
- 它的三大技术特色是：**纯 Python 主体**、**token 级 KV Cache 管理**、**多进程协作架构**（HttpServer/Router/ModelBackend/Detokenization 分离）。
- 它站在 **FasterTransformer / TGI / vLLM / SGLang / flashinfer / FlashAttention / Triton** 等项目的肩膀上，同时自己的 kernel 也被 vLLM、SGLang 引用，是一个**双向互鉴**的生态。
- 当前版本 **`1.1.0`**，要求 **Python ≥ 3.9.16**；`setup.py` 的 `install_requires` 给出 10 个最小依赖（pyzmq/rpyc/uvloop/transformers/safetensors/einops/triton/orjson/ninja/packaging），更重的可选依赖锁在 `requirements.txt`。
- 项目背后有多篇论文支撑（约束解码 Pre³ / 调度器 Past-Future Scheduler），是理解其设计动机的钥匙。
- 本讲只看了"门面文件"（README、setup.py），**还没有涉及任何运行逻辑**——这是故意的，先把地图印在脑子里，下一讲再动手装和跑。

---

## 7. 下一步学习建议

本讲结束后，建议按以下顺序继续：

1. **`u1-l2-install-and-quickstart.md`**：动手安装 LightLLM 并用一行命令启动服务，第一次真实跑通一个推理请求。
2. **`u1-l3-repo-structure.md`**：建立完整的源码目录地图，知道每类功能该去哪个子目录找。
3. **`u1-l4-entry-and-cli-args.md`**：从入口 `api_server.py` 与 `api_cli.py` 进入，理解启动参数。

如果你急于看"代码长什么样"，现在就可以打开 [lightllm/server/](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/) 目录随便翻一翻，但不必理解细节——本系列的第二单元会带你系统走一遍请求链路。
