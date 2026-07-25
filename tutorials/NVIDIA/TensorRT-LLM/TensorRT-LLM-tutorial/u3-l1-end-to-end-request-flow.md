# 请求全链路：HF 模型到生成 token

## 1. 本讲目标

本讲是「端到端推理流程总览」的第一讲。前面 u1-l1 我们建立了一张高空流程图：

> HF 模型 → LLM API → Executor → Scheduler → 模型前向 → Decoder → Sampling → 生成 Token

但那只是口诀。从本讲开始，我们要把这条链路**逐段落到真实源码上**。学完本讲，你应该能够：

1. 复述这条「HF → 生成 Token」全链路的每一个环节，并说清**每一段由哪份源码负责**。
2. 说出 `LLM` 构造时究竟做了哪几件事，以及一次 `llm.generate()` 调用如何在内部被拆解。
3. 理解「生成任务」在 Executor 内部**用什么数据结构表示**（`GenerationRequest`），以及它如何被移交（`submit`）给真正的执行器。
4. 分清哪些环节是 **Python 调度逻辑**、哪些环节是 **C++ 加速实现**——这正是 TensorRT-LLM「Python 调度、C++ 加速」设计的核心。

本讲只打通**主链路的骨架**：`PyExecutor` 内部那套单步循环（取请求 → 调度 → 前向 → 解码）的细节留到下一讲 u3-l2；模型前向本身留到 u3-l3。本讲的任务是「把路走通」。

## 2. 前置知识

在进入源码前，先用三段通俗语言铺垫几个关键概念。它们在 u1/u2 已零散出现，这里再统一确认一次。

**① 同步 / 异步与 Future（future）对象。**
当你调 `llm.generate(prompts)` 时，调用方希望「拿到完整结果」才返回——这是同步语义。但引擎内部并不是一次性算完一个 prompt 再算下一个，而是把多个请求**丢进队列、异步推进**。于是每个请求会立即返回一个「占位结果」对象（`GenerationResult` / `RequestOutput`，下文统称 future），你可以调它的 `.result()` 来阻塞等待真正完成。`generate()` 就是「提交一批 future，然后逐个 `result()` 阻塞」的糖。

**② Token 化（tokenization）与去 token 化（detokenization）。**
模型只认整数 token id，不认字符串。`LLM` 实例托管了 tokenizer（字符串 → token id）与 detokenizer（token id → 字符串），所以你「字符串进、字符串出」。这件事在 u1-l3 已经讲过，本讲你会看到它在 `generate_async` 里发生在哪个位置。

**③ Proxy / Worker 两层执行器。**
真正的推理在 GPU 进程里跑，而你的 Python 主进程只是个「前台」。`GenerationExecutor` 用工厂方法 `create()` 决定返回一个 **Proxy**（前台代理，把请求通过 IPC 转发给后台 Worker 进程）还是一个 **Worker**（直接持有引擎、单进程跑）。无论哪一种，对外暴露的 `generate_async` / `submit` 接口都一样——这就是「抽象」的意义。本讲会讲清这条边界。

> 一句话口诀：**Python 负责「调度与编排」，C++ 负责「kernel 与运行时加速」**；两者用 `GenerationExecutor` 这个抽象缝合在一起。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。所有链接均指向当前 HEAD `4b7d7199752f41960eedbf2846755e174940f164`。

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `tensorrt_llm/llmapi/llm.py` | LLM API 主入口 | `LLM` / `_TorchLLM` / `BaseLLM` 三层：构造、`_build_model`、`generate` / `generate_async` |
| `tensorrt_llm/executor/executor.py` | Executor 抽象层 | `GenerationExecutor`（ABC）、`generate_async` 构建 `GenerationRequest`、`create` 工厂 |
| `tensorrt_llm/executor/request.py` | 请求数据结构 | `GenerationRequest`：生成任务在内部如何被表示 |
| `tensorrt_llm/executor/base_worker.py` | Worker 基类 | `submit` 创建 future、`_enqueue_request` 把请求送进引擎 |
| `docs/source/torch/arch_overview.md` | 架构总览文档 | PyExecutor 单步流程、Scheduler 两步、ResourceManager 三接口 |
| `AGENTS.md` | 项目导览 | 高层「请求流」与「调度/解码流水线」描述 |

另外会顺带提及 `tensorrt_llm/_torch/pyexecutor/py_executor.py`（`PyExecutor`，真正的引擎），但只点到「请求在此进入单步循环」为止——细节交给 u3-l2。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 LLM 入口**：`LLM(...)` 构造时做了什么、`generate()` 如何发起请求。
- **4.2 Executor 抽象**：`GenerationExecutor` 如何把「API 层」与「真正的执行器」解耦。
- **4.3 请求流图**：一个 `GenerationRequest` 从诞生到进入引擎队列的完整旅程。

---

### 4.1 LLM 入口：构造与 generate

#### 4.1.1 概念说明

用户面对的最外层类是 `tensorrt_llm.LLM`（即 `llmapi.llm.LLM`）。它做了两件大事：

1. **构造阶段**：把一个 HuggingFace 模型（路径或仓库 id）变成一个**可推理的引擎实例**。这包括解析参数、加载 tokenizer / 模型配置、构造 Executor。
2. **推理阶段**：`generate(prompts)` 接收字符串或 token id，返回生成结果。

`LLM` 在源码里是一个**三层继承**结构：`LLM` → `_TorchLLM` → `BaseLLM`。`BaseLLM` 装着与后端无关的通用逻辑（参数解析、MPI 会话、`generate`/`generate_async`）；`_TorchLLM` 装着 PyTorch 后端特有的构造（`_build_model` 里加载 tokenizer / config、创建 Executor）；`LLM` 只是个薄包装（补 docstring）。这种分层让「通用流程」与「后端特化」分开，AutoDeploy 后端将来也能复用 `BaseLLM`。

#### 4.1.2 核心流程

构造阶段（`__init__` → `_build_model`）的伪代码：

```
LLM(model=...)
  BaseLLM.__init__:
    选 backend → 选 llm_args_cls（TorchLlmArgs）→ 构造 self.args   # 解析参数
    （多 GPU 时）创建 MpiSession                                  # 拉起 worker 进程
    self._build_model()                                           # 关键
  _TorchLLM._build_model:
    BaseLLM._build_model: CachedModelLoader(...) → engine_dir, hf_model_dir
    加载 tokenizer / hf_model_config / generation_config
    self._executor = self._executor_cls.create(...)              # ← 造 Executor
```

推理阶段（`generate`）的伪代码：

```
LLM.generate(prompts, sampling_params)
  for each prompt:
    future = self.generate_async(prompt, ...)   # 异步提交，立即返回 future
      self._preprocess(...)                     # tokenize：字符串 → token id
      result = self._executor.generate_async(...)  # 交给 Executor
      return RequestOutput._from_generation_result(result, ...)  # 包成对外对象
  for f in futures: f.result()                  # 阻塞等待，同步语义
```

关键点：`generate()` 本身不是「干活」的地方，它只是「**提交 + 等待**」；真正干活从 `self._executor.generate_async(...)` 开始。

#### 4.1.3 源码精读

**构造参数解析与 backend 选择。** `BaseLLM.__init__` 先确定执行器类、解析 backend 与参数类：

[`llm.py:163`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L163) 默认执行器类是 `GenerationExecutor`（可被子类覆盖）；随后根据 `backend == "pytorch"` 选 `TorchLlmArgs`，并把所有用户 kwargs 喂进 Pydantic 配置对象 `self.args`：

[`llm.py:205-214`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L205-L214) 这一句把零散的用户参数固化成一个强类型的 `self.args`，是后续所有行为的总开关（u4-l1 会专讲它）。

**多 GPU 时拉起 worker 进程。** 若 `parallel_config.is_multi_gpu`，会创建一个 MPI 会话（`MpiPoolSession` 或 `MpiCommSession`），它负责后续把 worker 进程拉起来：

[`llm.py:256-265`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L256-L265) 这一步是「Python 调度」侧的编排——真正在 GPU 上跑的代码在这些 worker 进程里。

**`_build_model` 的 PyTorch 特化。** `BaseLLM._build_model` 调 `CachedModelLoader` 把 HF 模型落到磁盘目录（`engine_dir` / `hf_model_dir`）：

[`llm.py:1360-1366`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L1360-L1366) 注意它返回的是目录，不是内存模型对象——因为 PyTorch 后端的 Executor 接受的是「检查点目录」路径，真正的模型加载发生在 worker 进程里。

然后 `_TorchLLM._build_model` 加载 tokenizer 与 HF 配置，并最终创建 Executor：

[`llm.py:1552-1560`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L1552-L1560) 先 `super()._build_model()` 拿到目录，再加载 tokenizer/config（必须在 `model_loader()` 之后，因为可能要先从 HF Hub 下载模型）。

[`llm.py:1611-1628`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L1611-L1628) 这一行 `self._executor = self._executor_cls.create(...)` 是**整条链路的「缝」**：它把 LLM API 层和 Executor 层接起来。`self.args`（全部运行时配置）、`mpi_session`、tokenizer 等都作为参数传进去。

**`generate` 的「提交 + 等待」模式。**

[`llm.py:496-511`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L496-L511) 对每条输入调 `generate_async(...)` 拿到一个 future。

[`llm.py:513-517`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L513-L517) 用 `tqdm` 包裹逐个 `future.result()`——这正是「同步语义」的实现：提交是异步的，等待是阻塞的。

**`generate_async` 把字符串变成 token id 再交给 Executor。**

[`llm.py:597-603`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L597-L603) 这里调用 `self._preprocess(...)` 完成 tokenize（若传入的已经是 `PreprocessedInputs` 则跳过）。tokenize 发生在**主进程的 Python 侧**。

[`llm.py:616-634`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L616-L634) `result = self._executor.generate_async(...)`——请求正式交给 Executor。`prompt_token_ids`（已 token 化）是主参数。

[`llm.py:640-644`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L640-L644) 把 Executor 返回的内部 future 包成对外的 `RequestOutput`，并挂上 tokenizer（用于将来 detokenize）。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 的前提下，靠「读源码 + 静态分析」理清 `LLM` 构造时究竟调用了哪些关键函数。

**操作步骤**：

1. 打开 [`tensorrt_llm/llmapi/llm.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py)，定位 `class LLM`（约 1648 行）、`class _TorchLLM`（1480 行）、`class BaseLLM`（146 行）。
2. 沿继承链画出调用顺序：`LLM.__init__` → `_TorchLLM.__init__`（1486 行）→ `super().__init__` 即 `BaseLLM.__init__`（150 行）→ 内部 `self._build_model()`（280 行）→ `_TorchLLM._build_model`（1552 行）。
3. 在 `_TorchLLM._build_model` 里找到创建 Executor 的那一行（1611 行 `self._executor = self._executor_cls.create(...)`），把传给 `create` 的实参列成一张表（如 `engine=self._engine_dir`、`mpi_session=self.mpi_session`、`tokenizer=self.tokenizer`、`llm_args=self.args` 等）。

**需要观察的现象**：你会注意到 tokenizer / config 的加载（1558–1560 行）被刻意放在 `super()._build_model()`（即模型落盘）**之后**，因为模型可能需要先从 HF Hub 下载。

**预期结果**：得到一张「构造调用栈」草图，能指出「真正创建 Executor」发生在 `_TorchLLM._build_model` 的末尾。若无法在本地运行，标注「待本地验证」即可（本实践为源码阅读型，无需 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：`LLM` 为什么要拆成 `BaseLLM` / `_TorchLLM` / `LLM` 三层？把「构造 Executor」放在 `_TorchLLM` 而不是 `BaseLLM` 有什么好处？

> **答案**：`BaseLLM` 装后端无关的通用流程（参数解析、MPI、`generate`/`generate_async`），`_TorchLLM` 装后端特化逻辑。把「创建 Executor」放 `_TorchLLM` 是因为不同后端（PyTorch vs AutoDeploy）构造 Executor 的方式不同；通用流程则可被复用，避免重复。

**练习 2**：`generate()` 最后那段 `for f in futures: f.result()`（513–517 行）的作用是什么？如果删掉会怎样？

> **答案**：它把「异步提交」转成「同步返回」——阻塞直到每个请求真正完成。删掉的话 `generate()` 会在请求还没算完时就返回一批未完成的 future，违反同步语义。

---

### 4.2 Executor 抽象：从 API 到执行器

#### 4.2.1 概念说明

`LLM` 层不直接碰 GPU。中间隔着一个抽象：`GenerationExecutor`。它的职责是**把「一个生成请求」转交给真正的执行器（PyTorch 的 PyExecutor、AutoDeploy、或 C++ 经典执行器）**，并对外暴露统一的接口。

为什么需要这一层？因为同一个 `llm.generate_async` 调用，底下可能是：

- **单进程 Worker**（TP1 或调试场景）：Executor 直接持有引擎对象，`submit` 同进程调用。
- **多进程 Proxy**（多 GPU 默认）：Executor 是个代理，`submit` 把请求通过 IPC 队列发给后台 Worker 进程。
- **Ray / RPC**：跨节点或跨进程的远程执行器。

把这套差异藏在 `GenerationExecutor` 抽象后面，上层 `LLM` 就完全不用关心「到底在哪跑」。

#### 4.2.2 核心流程

`GenerationExecutor` 提供两类接口：

- **抽象方法**（子类必须实现）：`submit(request)`、`abort_request(request_id)`。
- **具体方法**（基类已实现，复用）：`generate_async(...)`、`generate(...)`。

其中 `generate_async(...)` 是关键：它接收原始参数（token id、采样参数等），**构造一个 `GenerationRequest` 对象**，然后调 `self.submit(request)`。也就是说：

```
generate_async(token_ids, sampling_params, ...)     # 基类具体方法
  → 构造 GenerationRequest(...)
  → self.submit(request)                            # 抽象方法，由 Proxy/Worker 实现
```

于是「构造请求对象」与「递交请求」被干净地分开：前者逻辑统一在基类，后者因部署形态而异。

工厂方法 `GenerationExecutor.create(...)` 根据 `orchestrator_type`（ray / rpc / ipc）和世界规模，选择返回哪种执行器。下图概括：

| 部署形态 | `create()` 返回 | `submit` 行为 |
|---------|----------------|--------------|
| Ray 编排 | `RayExecutor` | 跨 Ray actor 调用 |
| RPC 编排 | `GenerationExecutorRpcProxy` | 跨进程 RPC |
| IPC 单进程（TP1 / 调试） | `GenerationExecutorWorker` | 同进程，直接持有引擎 |
| IPC 多进程（多 GPU 默认） | `GenerationExecutorProxy` | 通过 IPC 队列转发给 Worker |

#### 4.2.3 源码精读

**`GenerationExecutor` 是 ABC，`submit` 是抽象方法。**

[`executor.py:83-120`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/executor.py#L83-L120) 定义了这个抽象基类，并声明 `submit(request: GenerationRequest) -> GenerationResult` 为抽象方法（118–120 行）。注意 `__init__` 里还初始化了错误队列、统计队列、KV 事件队列等（85–116 行）——这些是所有执行器共享的「基础设施」。

**`generate_async` 把原始参数打包成 `GenerationRequest` 再 `submit`。**

[`executor.py:158-180`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/executor.py#L158-L180) 这段是「抽象的精华」：`request = GenerationRequest(prompt_token_ids, sampling_params, ...)` 把所有参数装进一个数据对象，然后 `result = self.submit(request)`。注意它返回的 `result` 还不是最终文本，而是一个可等待的 `GenerationResult`。

**`GenerationRequest` 是生成任务的内部表示。**

[`request.py:92-165`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/request.py#L92-L165) 它承载了 prompt_token_ids、sampling_params、lora_request、disaggregated_params、priority 等全部「这个请求要怎么生成」的信息。注意 118–128 行：token ids 统一规整成 `list[int]`（或 int32 buffer 的懒加载形式），这是后续跨进程传递与 C++ 消费的统一格式。`priority` 在 [0,1] 区间（162–165 行校验），默认 `0.5`。

**`create` 工厂根据编排方式分流。**

[`executor.py:538-595`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/executor.py#L538-L595) 工厂签名（539 行）。先处理「附加到已运行 worker」的多前端服务场景（575–599 行），再按 `orchestrator_type` 分流：`ray`（612–621 行）→ Ray；否则进入 IPC/RPC 分支（627–712 行）。例如多 GPU 默认走 [`executor.py:689-695`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/executor.py#L689-L695) 的 `_create_ipc_executor(use_worker=False)`，返回一个 **Proxy**。

**Proxy vs Worker 的 `submit` 差异。** `BaseWorker.submit` 是 Worker（同进程）的实现，它创建 future 并把请求送进引擎：

[`base_worker.py:591-623`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/base_worker.py#L591-L623) 它先分配 `client_id`，创建 `GenerationResult` future 存进 `self._results[client_id]`，再调 `self._enqueue_request(request)` 把请求真正送进去。注意 595–599 行的断言：只有 rank 0 才能 `submit`——这解释了为什么 `llm.generate(...)` 必须包在 `if __name__ == "__main__":` 里。

Proxy 的 `submit`（在 `proxy.py`，本讲不展开）做的事类似，但不是直接进引擎，而是序列化后通过 IPC 队列发给后台 Worker 进程。

#### 4.2.4 代码实践

**实践目标**：验证「`generate_async`（基类方法）→ 构造 `GenerationRequest` → `submit`（子类方法）」这条链，并区分 Python / C++ 边界。

**操作步骤**：

1. 在 [`executor.py:126-180`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/executor.py#L126-L180) 的 `generate_async` 中，圈出两件事：(a) 第 158 行 `GenerationRequest(...)` 构造；(b) 第 176 行 `self.submit(request)`。
2. 用 Grep 确认 `submit` 有几个实现：搜索 `def submit` 出现在 `base_worker.py:591`（Worker）、`proxy.py`（Proxy）、`ray_executor.py`（Ray）。这说明同一个抽象方法有多种实现。
3. 在 `base_worker.py` 里确认 `self.engine` 是谁：看 [`base_worker.py:170-213`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/base_worker.py#L170-L213)——当 `_backend == "pytorch"` 时，`create_executor = create_py_executor`，最终 `self.engine` 就是一个 `PyExecutor` 对象（纯 Python）。

**需要观察的现象**：`generate_async` 与 `GenerationRequest` 全是 **Python**；而 `submit` 之后进入 `self.engine`（PyExecutor），PyExecutor 内部调度用 Python、kernel 调用走 C++/CUDA。

**预期结果**：你能用一句话说清——「请求对象在 Python 里构造，递交动作（submit）的实现因部署形态而异，但最终都汇入同一个 `PyExecutor` 引擎」。无法运行时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate_async` 定义在基类 `GenerationExecutor` 里，而 `submit` 是抽象方法？把它们合并成一个方法行不行？

> **答案**：`generate_async` 负责「把零散参数打包成 `GenerationRequest`」，这段逻辑对所有执行器都一样，放基类避免重复。`submit` 负责「把请求递交出去」，递交方式（同进程 / IPC / Ray）因部署形态而异，所以抽象。合并会逼每个子类都重复实现打包逻辑，违背 DRY。

**练习 2**：`create()` 在什么情况下返回 `GenerationExecutorWorker`（单进程）而不是 `GenerationExecutorProxy`（多进程）？

> **答案**：当 `return_logits=True`、或开启了 TP1 单进程调试（`enable_worker_single_process_for_tp1()`）时（见 `executor.py:656-674`），会走 `use_worker=True` 返回 Worker；多 GPU 默认（676–695 行）返回 Proxy。

---

### 4.3 请求流图：GenerationRequest 的旅程

#### 4.3.1 概念说明

前两个模块讲清了「入口」和「抽象」。本模块把它们**串成一张完整时序图**：从用户敲下 `llm.generate("你好")`，到一个 token 被生成出来，请求一路经过了哪些对象、跨了哪些进程边界、哪里是 Python、哪里是 C++。

这条链路的核心叙事是：**请求对象的「身份」在不断变化，但「生成任务」本身一直在被传递。**

- 在 `LLM` 层：它是一段字符串 `"你好"`。
- 经过 `_preprocess`：变成 `prompt_token_ids`（一串 int）。
- 进入 `generate_async`：被包成 `GenerationRequest` 数据对象。
- 经过 `submit` / `_enqueue_request`：被翻译成 C++ 运行时能消费的 `tllm.Request`（`executor_request`）。
- 进入 `PyExecutor`：进入请求队列，被单步循环消费（u3-l2 详述）。

「in-flight batching（在途批处理）」就发生在最后这一段：多个请求在队列里**混合推进**，而不是排队逐个处理。这是高吞吐的关键，但细节留到 u3-l2。本模块只关心请求**怎么进队列**。

#### 4.3.2 核心流程

下面这张时序图（文字版）概括了从 `generate` 到请求入队的全过程。`--Py-->` 表示 Python 侧，`--C++-->` 表示 C++ 侧。

```
[用户代码] llm.generate(["你好"])
   │  --Py-->  LLM.generate (llm.py:420)
   │            ├─ 对每条 prompt 调 generate_async
   │            │   --Py--> LLM.generate_async (llm.py:525)
   │            │            ├─ self._preprocess(...)        # tokenize: 字符串→token ids (Py)
   │            │            ├─ self._executor.generate_async(...)   # 进入 Executor 抽象
   │            │            │   --Py--> GenerationExecutor.generate_async (executor.py:126)
   │            │            │            ├─ 构造 GenerationRequest (executor.py:158)  (Py)
   │            │            │            └─ self.submit(request)                     (Py, 抽象)
   │            │            │                 │
   │            │            │       ┌─────────┴─────────── 根据 create() 的部署形态 ───────────┐
   │            │            │       ▼ (Proxy) 多进程 IPC          ▼ (Worker) 单进程
   │            │            │   Proxy.submit (proxy.py)       BaseWorker.submit (base_worker.py:591)
   │            │            │       │ 序列化→IPC 队列              ├─ 分配 client_id
   │            │            │       │                            ├─ 创建 GenerationResult future
   │            │            │       │                            └─ self._enqueue_request(request)
   │            │            │       │                                  └─ self.engine.enqueue_request(executor_request)
   │            │            │       │                                       │
   │            │            │       │                              --进入 PyExecutor 单步循环 (u3-l2)--
   │            │            │       │                                       ├─ Scheduler 调度      (接口 Py / 实现 C++)
   │            │            │       │                                       ├─ Model Forward       (Py 调度 / C++ kernel)
   │            │            │       │                                       ├─ Decoder             (Py)
   │            │            │       │                                       └─ Sampling            (Py 调度 / C++ kernel)
   │            │            │       ▼ (worker 进程) BaseWorker.submit ... 同上
   │            │            └─ 返回 RequestOutput future (llm.py:640)  # 包上 tokenizer 供 detok
   │            └─ for f in futures: f.result()    # 阻塞等待 (Py)
   ▼  返回 List[RequestOutput]（已生成完成的文本）
```

配套的官方高层描述在 `AGENTS.md`：

[`AGENTS.md:65-68`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/AGENTS.md#L65-L68) 这就是本讲一直在落实的那张「HF 模型 → LLM API → Executor → Scheduler → 模型前向 → Decoder → Sampling → 生成 Token」口诀图。

而 `arch_overview.md` 给出了 PyExecutor 单步循环的标准描述（本模块只引用、不展开细节）：

[`arch_overview.md:25-31`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L25-L31) 「取请求 → 调度 → 前向 → 解码 → 追加 token」五步，正是请求进入队列后被消费的方式。

#### 4.3.3 源码精读

本模块聚焦「请求如何翻译成 C++ 运行时可消费的形式并送进引擎」——这是 Python 与 C++ 边界上最关键的一步。

**`_enqueue_request` 把 `GenerationRequest` 翻译成 C++ `tllm.Request`。**

[`base_worker.py:334-337`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/base_worker.py#L334-L337) 入口。函数体内会处理 LoRA、prompt adapter、多模态、分离式参数等，最终组装出一个 C++ `executor_request`：

[`base_worker.py:567-586`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/base_worker.py#L567-L586) 各种 `py_*` 属性（如 `py_scheduling_params`、`py_multimodal_data`）是「Python 侧动态挂到 C++ Request 上的旁路通道」——因为有些对象（logits processor、Python 调度参数）无法用 C++ binding 表达，就用 `py_` 前缀的动态属性携带。

**最后一行 `self.engine.enqueue_request(executor_request)`：请求正式进入 PyExecutor 队列。**

[`base_worker.py:574-586`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/base_worker.py#L574-L586) `self.engine` 对 PyTorch 后端就是 `PyExecutor`（由 `create_py_executor` 造出）。`enqueue_request` 返回一个 `req_id`，被记录在 `self._client_id_to_request_id` 里——这是将来 `abort_request` 取消请求时要用到的映射。

**`PyExecutor` 的位置。** 真正的单步循环在：

[`py_executor.py:501`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py#L501) `class PyExecutor` 定义在此。请求进入它之后，就被单步循环（取请求 → 调度 → 前向 → 解码 → 处理响应）消费。其中 `_handle_executed_batch`（3118 行）和 `_handle_responses`（6775 行）负责把生成的 token 收集起来回填到对应的 `GenerationResult` future。**这些细节全部留给 u3-l2**。

#### 4.3.4 代码实践

**实践目标**：亲手画出一张「标注 Python / C++ 责任」的调用时序图（这是本讲规格里指定的实践任务）。

**操作步骤**：

1. 准备一张白纸或绘图工具。从「`llm.generate(["你好"])`」开始，按 4.3.2 的文字时序图，把每一个函数调用画成一个方框，方框之间用箭头连接。
2. 用**两种颜色**（或实线/虚线）区分：实线 = 纯 Python 逻辑；虚线 = 触及 C++/CUDA 的环节。具体标注：
   - `LLM.generate` / `generate_async` / `_preprocess` / `GenerationExecutor.generate_async` / `GenerationRequest` 构造 / `BaseWorker.submit` / `_enqueue_request` 组装 → **实线（Python）**。
   - `self.engine.enqueue_request` 之后的 Scheduler 决策（C++ binding）、Model Forward 的 kernel、Sampling 的 kernel → **虚线（C++/CUDA）**。
3. 用一条竖虚线标出「进程边界」：在 Proxy 部署下，`Proxy.submit`（主进程）与 `BaseWorker.submit`（worker 进程）之间跨了一次 IPC。

**需要观察的现象**：你会发现「请求对象的传递」几乎全是 Python；而真正「算 token」的 kernel 全是 C++/CUDA。Python 侧的 `_enqueue_request` 是这两个世界的**翻译官**。

**预期结果**：得到一张能回答「这一步是 Python 还是 C++」的时序图。如果你有 GPU 且能跑通 u1-l3 的 quickstart，可在 `LLM(model=...)` 与 `llm.generate(...)` 处各打一条日志验证调用顺序；否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：在请求入队时，`base_worker.py:574-586` 的那些 `py_` 前缀动态属性（如 `py_scheduling_params`）为什么存在？为什么不直接全用 C++ binding？

> **答案**：有些对象是纯 Python 的、无法或不宜用 C++ binding 表达（如用户自定义的 logits processor、Python 调度参数）。`py_` 前缀属性是一条「Python 侧旁路通道」，把这些 Python 对象挂在 C++ `Request` 上，让运行时在合适的时机取回它们在 Python 里执行。

**练习 2**：请按出现顺序排出请求「身份」的演变：① `tllm.Request`（executor_request）② 字符串 ③ `GenerationRequest` ④ `prompt_token_ids`。

> **答案**：② 字符串 → ④ `prompt_token_ids`（`_preprocess` tokenize 后）→ ③ `GenerationRequest`（`generate_async` 打包）→ ① `tllm.Request`（`_enqueue_request` 翻译后）。

**练习 3**：`client_id`（由 `BaseWorker.submit` 分配）和 `req_id`（由 `engine.enqueue_request` 返回）是同一个东西吗？为什么需要两个？

> **答案**：不是。`client_id` 是 Executor/前台层给请求编的号（对外、给 future 用）；`req_id` 是底层引擎运行时给请求编的号。`self._client_id_to_request_id` 维护二者的映射，这样 `abort_request(client_id)` 才能找到底层真正要取消的 `req_id`。

## 5. 综合实践

**任务**：写一份《请求的一生》源码导览文档，把本讲三个模块串成一个完整故事。

具体要求：

1. **起点**写用户代码 `from tensorrt_llm import LLM; llm = LLM(model=...); llm.generate(["你好"])`。
2. **第一段**（构造期）：追踪 `LLM(...)` → `BaseLLM.__init__` → `_TorchLLM._build_model` → `self._executor = self._executor_cls.create(...)`。引用 [`llm.py:280`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L280)（`_build_model` 调用点）与 [`llm.py:1611`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L1611)（Executor 创建点）。
3. **第二段**（推理期）：追踪 `generate` → `generate_async` → `_preprocess` → `self._executor.generate_async` → `GenerationRequest` 构造 → `submit` → `_enqueue_request` → `engine.enqueue_request`。每一步给出源码行号锚点（[`llm.py:616`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L616)、[`executor.py:158`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/executor.py#L158)、[`base_worker.py:617`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/executor/base_worker.py#L617)）。
4. **第三段**（边界与未来）：在 `engine.enqueue_request` 处画一条竖线，标明「线左边是 Python 调度，线右边（PyExecutor 单步循环）是 u3-l2 的地盘」。
5. **一张总表**：用「环节 / 文件:行 / Python 还是 C++ / 下一讲是否展开」四列总结全部节点。

**验收标准**：任何一个没读过 TensorRT-LLM 的同事，看完你的导览后，能指着源码说出「用户的一句 `llm.generate` 在这里变成了 `GenerationRequest`、在那里进了引擎队列」。完成本任务后，你就掌握了本讲的全部要点，也为 u3-l2（PyExecutor 单步循环）准备好了精确的入口点。

## 6. 本讲小结

- **全链路口诀**：HF 模型 → LLM API → Executor → Scheduler → 模型前向 → Decoder → Sampling → 生成 Token；本讲把前半段（API → Executor → 请求入队）落到了源码。
- **`LLM` 三层结构**：`LLM` → `_TorchLLM`（后端特化的 `_build_model`）→ `BaseLLM`（通用 `generate`/`generate_async`）；构造 Executor 发生在 [`llm.py:1611`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L1611)。
- **`generate` = 提交 + 等待**：异步提交 future（`generate_async`），再 `f.result()` 阻塞（[`llm.py:513-517`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L513-L517)）。
- **`GenerationExecutor` 抽象**：基类的 `generate_async` 负责构造 `GenerationRequest` 再调抽象 `submit`；`create()` 工厂按 ray/rpc/ipc 选 Proxy/Worker/Ray。
- **请求身份演变**：字符串 → `prompt_token_ids` → `GenerationRequest` → C++ `tllm.Request`；翻译官是 `BaseWorker._enqueue_request`。
- **Python / C++ 边界**：`engine.enqueue_request` 是分水岭——之前全是 Python 调度，之后进入 PyExecutor 单步循环（kernel 走 C++/CUDA）。

## 7. 下一步学习建议

本讲到 `self.engine.enqueue_request` 为止——请求已经进了 `PyExecutor` 的队列，但我们刻意没有打开它。接下来的学习顺序：

1. **u3-l2 PyExecutor 单步循环**：打开 [`tensorrt_llm/_torch/pyexecutor/py_executor.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_torch/pyexecutor/py_executor.py)，看请求如何被「取出来 → 调度 → 前向 → 解码 → 回填 token」。这是本讲的直接续集。
2. **u3-l3 ModelEngine 与模型前向**：聚焦单步循环里「前向」这一步，看 `PyTorchModelEngine.forward` 如何执行一次模型计算。
3. **横向补充**：想理解请求在引擎里的状态机（CONTEXT_INIT / GENERATION / COMPLETE），可预习 u8-l2；想理解调度器如何决定「这一步跑哪些请求」，可预习 u8-l1。

建议在进入 u3-l2 前，先把本讲「综合实践」的时序图画完——它会让你带着一张清晰的地图，去拆解 PyExecutor 的内部循环。
