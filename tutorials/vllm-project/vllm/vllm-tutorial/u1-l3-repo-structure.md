# 仓库目录结构总览

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 vLLM 仓库的顶层布局，知道根目录下每个文件夹（`csrc`、`docs`、`examples`、`tests`、`requirements`、`setup.py` 等）各自承担什么职责。
- 打开 `vllm/` 这个 Python 包，说出 `entrypoints`、`v1`、`engine`、`model_executor`、`config`、`distributed` 等关键子目录的作用。
- 明确区分「V1 当前架构」（`vllm/v1`）与「旧引擎兼容层」（`vllm/engine`），并能解释为什么会出现这种双层结构。
- 在阅读后续讲义之前，建立一张「在哪个目录找哪类代码」的心智地图。

本讲是**地图讲**：重在建立全局方位感，不深入任何单个函数的实现。目录结构是后续所有源码阅读的坐标，先认路，再认字。

## 2. 前置知识

本讲假设你已经读过 [u1-l1 项目定位与核心特性](u1-l1-project-overview.md)，了解以下概念：

- **推理 / 服务（inference / serving）**：用一个大语言模型生成文本。离线推理是「写脚本直接跑」，在线服务是「起一个 HTTP 服务器让别的程序通过网络调用」。
- **KV 缓存（KV cache）**：注意力机制中保存的历史 key/value，按 **block（块）** 管理是 PagedAttention 的核心思想。
- **Engine Core 进程 / GPU Worker 进程**：vLLM V1 用多进程架构，调度与执行分别落在不同进程。
- **吞吐 / 显存 / 分片（sharding）/ 量化**：vLLM 的所有优化都围绕「在有限显存里尽可能多地并行算」。

如果你对这些词还陌生，建议先回顾 u1-l1。本讲会在用到时简要提醒，但不再从头解释。

此外，你需要理解一个仓库常识：**一个大型 Python 项目通常把源码拆成多个子包（子目录）**，每个子包用 `__init__.py` 标记为可导入的包。vLLM 的主包就是 `vllm/`。

## 3. 本讲源码地图

本讲涉及的关键文件如下，主要用来「定位」而非「精读」：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md) | 项目定位与核心特性清单，是认识仓库的起点 |
| [docs/design/arch_overview.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md) | 官方架构总览，讲清入口、V1 进程架构与类层次设计 |
| `setup.py` / `CMakeLists.txt` / `csrc/` / `cmake/` / `rust/` | 构建系统与本地扩展（C++/CUDA/Rust）源码，详见 [u1-l2](u1-l2-install-and-build.md) |
| `vllm/__init__.py` | 包的公共入口，定义懒加载机制 |
| `vllm/entrypoints/` | 离线推理类与在线服务 CLI/HTTP 的入口 |
| `vllm/v1/` | **V1 当前引擎架构**（调度、引擎核心、worker、采样等） |
| `vllm/engine/` | 旧引擎目录，现在主要是兼容别名 |
| `vllm/config/` | 全局配置对象 `VllmConfig` 及其子配置 |
| `vllm/model_executor/` | 模型实现、层、权重加载器 |
| `vllm/distributed/` | 张量/流水/数据并行与进程组管理 |

> 提示：本讲所有永久链接都指向当前 HEAD `f0de1a604`，行号以此版本为准。

## 4. 核心概念与源码讲解

本讲把目录拆成 6 个最小模块来讲：先看仓库顶层（4.1），再看 `vllm/` 包的公共入口（4.2），然后按「请求怎么进来 → 引擎怎么跑 → 模型与并行怎么落地」的顺序，依次讲 `entrypoints`（4.3）、`v1`（4.4）、`engine` + `config`（4.5）、`model_executor` + `distributed`（4.6）。

### 4.1 仓库顶层布局

#### 4.1.1 概念说明

一个项目的根目录就是它的「门面」。vLLM 不是纯 Python 库——它还带着一批用 C++/CUDA/Rust 写的本地扩展（`.so` 文件），所以根目录同时包含 Python 源码、原生源码、构建脚本、文档和测试。认全顶层布局，你才能知道「想跑示例去哪、想改内核去哪、想看文档去哪」。

#### 4.1.2 核心流程

根目录可以按职责分成几组：

1. **项目说明与规范**：`README.md`、`AGENTS.md`/`CLAUDE.md`（贡献规范）、`LICENSE`、`SECURITY.md`、`CONTRIBUTING.md`。
2. **Python 打包元数据**：`pyproject.toml`（依赖与构建后端）、`setup.py`（编译总指挥）、`MANIFEST.in`。
3. **原生扩展构建**：`CMakeLists.txt` + `cmake/`（CMake 模块）+ `csrc/`（C++/CUDA 源码）+ `rust/` + `build_rust.sh` + `rust-toolchain.toml`。
4. **主 Python 包**：`vllm/`（绝大多数源码都在这里）。
5. **文档、示例、测试、基准**：`docs/`、`examples/`、`tests/`、`benchmarks/`。
6. **依赖声明与工具**：`requirements/`（分场景依赖文件）、`tools/`、`scripts/`、`docker/`。

#### 4.1.3 源码精读

`README.md` 一句话定义了项目定位（[README.md:L24-L51](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/README.md#L24-L51)）：vLLM 是「快速且易用的 LLM 推理与服务库」，随后用一组 bullet 列出 PagedAttention、连续批处理、CUDA Graphs、量化、推测解码等能力。读这一段就能知道仓库顶层每个目录大致是为哪个能力服务的。

`docs/design/arch_overview.md` 的「Entrypoints」一节（[arch_overview.md:L7-L79](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L7-L79)）则把「离线推理 `LLM` 类」与「在线服务 `vllm serve`」这两条入口讲清了——这正是 `entrypoints/` 目录要承载的两类用法。

#### 4.1.4 代码实践

1. **实践目标**：在终端里肉眼数一遍顶层目录，建立第一印象。
2. **操作步骤**：在仓库根目录执行 `ls -1`（或在文件浏览器里查看）。
3. **需要观察的现象**：你会看到上面 6 组里的文件/目录都真实存在。
4. **预期结果**：能够不查文档，说出 `csrc/` 是原生 C++/CUDA 源码、`tests/` 是测试、`examples/` 是示例脚本。
5. 离线环境也能做：纯目录列举，无需运行模型。

#### 4.1.5 小练习与答案

- **练习 1**：如果想给项目加一个全新模型的 Python 实现，顶层哪个目录最相关？
  - **答案**：`vllm/`（具体是 `vllm/model_executor/models/`）；顶层 `examples/` 只是示例，不是实现落点。
- **练习 2**：`requirements/` 与 `pyproject.toml` 里的依赖有什么不同？
  - **答案**：`pyproject.toml` 声明核心/默认依赖；`requirements/` 按 `cuda`、`test`、`lint` 等场景拆分额外的可选依赖文件。

### 4.2 vllm/ 包总览与公共 API 懒加载

#### 4.2.1 概念说明

`vllm/` 是真正的 Python 源码主包。但它很重——导入它会触发大量重型依赖（`torch`、CUDA 扩展等）。为了「`import vllm` 时不把所有东西都加载一遍」，vLLM 用了**懒加载（lazy import）**：包的 `__init__.py` 里维护一张「属性名 → 模块路径」的表，只有在你真正访问某个属性时，才去 import 对应的模块。

#### 4.2.2 核心流程

1. 解释器执行 `import vllm`，加载 `vllm/__init__.py`。
2. 文件顶部立即加载轻量对象（如版本号）。
3. 定义 `MODULE_ATTRS` 字典：键是公开属性名（如 `"LLM"`、`"SamplingParams"`），值是 `"模块路径:属性名"`。
4. 定义 `__getattr__(name)`：当访问 `vllm.LLM` 时，查表、按需 import、返回真正的对象。

#### 4.2.3 源码精读

版本号在顶部直接加载（[vllm/__init__.py:L7](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L7)）：

```python
from .version import __version__, __version_tuple__  # isort:skip
```

懒加载表与拦截逻辑在（[vllm/__init__.py:L16-L69](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L16-L69)）：

```python
MODULE_ATTRS = {
    # "LLM": "vllm.entrypoints.llm:LLM",
    # "SamplingParams": "vllm.sampling_params:SamplingParams",
    ...
}

def __getattr__(name: str) -> typing.Any:
    if name in MODULE_ATTRS:
        module_name, attr_name = MODULE_ATTRS[name].split(":")
        ...  # 按需 import module_name，再取 attr_name
```

> 这意味着 `vllm/` 顶层既有许多**子目录**（`v1`、`engine`、`model_executor` 等），也通过 `MODULE_ATTRS` 把散落在各处的公共类「挂」到 `vllm.` 命名空间下。这是理解整个包组织方式的关键。

#### 4.2.4 代码实践

1. **实践目标**：体会懒加载的好处。
2. **操作步骤**：在装好 vLLM 的环境里执行 `python -c "import vllm; print(vllm.__version__)"`，再用 `python -c "import vllm; print(vllm.LLM)"` 对比。
3. **需要观察的现象**：打印版本号很快；访问 `vllm.LLM` 时会明显多加载若干模块。
4. **预期结果**：说明版本号是即时加载，而 `LLM` 走 `__getattr__` 懒加载。
5. 若环境无 GPU/未安装，**待本地验证**；可改为阅读 `__init__.py` 的 `MODULE_ATTRS` 字典，列出至少 5 个被懒加载的公共名字。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 vLLM 不直接在 `__init__.py` 顶部 `from vllm.entrypoints.llm import LLM`？
  - **答案**：那会强制每个 `import vllm` 都加载 `LLM`、进而加载 `torch` 与引擎，拖慢启动并占用内存；懒加载让「用到才付钱」。
- **练习 2**：`MODULE_ATTRS` 的值用 `:` 分隔「模块:属性」，这种写法的好处是什么？
  - **答案**：可以用一行字符串同时表达「去哪个模块 import」和「取哪个属性」，便于统一解析与注册。

### 4.3 vllm/entrypoints：进入系统的两扇门

#### 4.3.1 概念说明

`entrypoints` 直译是「入口」。它是用户与 vLLM 打交道的最外层：一扇门是**离线推理**（`LLM` 类，写 Python 脚本直接生成），另一扇门是**在线服务**（`vllm serve` 启动 OpenAI 兼容 HTTP 服务）。所有用户可见的接口、CLI 命令、协议适配（OpenAI/Anthropic/Cohere/gRPC）都在这里。

#### 4.3.2 核心流程

1. **离线门**：`entrypoints/llm.py` 定义 `LLM` 类，用户 `llm = LLM(model=...)` 后调用 `llm.generate(...)`。
2. **在线门**：`entrypoints/cli/main.py` 是 `vllm` 命令行总入口；`cli/serve.py` 实现 `vllm serve` 子命令；`launcher.py` 负责把参数组装好拉起真正的服务。
3. **HTTP 层**：`entrypoints/openai/api_server.py` 是 OpenAI 兼容 API 服务器（也是被 `serve` 最终拉起的目标）。
4. **多协议适配**：`anthropic/`、`cohere/`、`grpc_server.py`、`mcp/` 等子目录/文件提供不同协议的兼容层。

#### 4.3.3 源码精读

`LLM` 类的定义（[vllm/entrypoints/llm.py:L67](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L67)）：

```python
class LLM(BeamSearchOfflineMixin, PoolingOfflineMixin, OfflineInferenceMixin):
```

注意它通过多个 mixin 组合出离线推理的全部能力（生成、beam search、pooling/embedding），这是 vLLM 里常见的「组合优于继承」写法。

`arch_overview.md` 也明确指出 `LLM` 类在 `vllm/entrypoints/llm.py`，而 `vllm` CLI 在 `vllm/entrypoints/cli/main.py`（[arch_overview.md:L52-L63](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L52-L63)）。

#### 4.3.4 代码实践

1. **实践目标**：在 `entrypoints/` 里把「两扇门」对上号。
2. **操作步骤**：用 `ls -1 vllm/entrypoints` 查看，再打开 `llm.py` 与 `cli/main.py` 各看一眼。
3. **需要观察的现象**：`entrypoints/` 下既有 `llm.py`（离线），也有 `cli/`、`openai/`（在线）。
4. **预期结果**：能说出「离线用 `LLM` 类、在线用 `vllm serve`/`api_server.py`」。
5. 无需运行模型即可完成。

#### 4.3.5 小练习与答案

- **练习 1**：`vllm serve <model>` 最终拉起的是哪个文件里的服务器？
  - **答案**：`vllm/entrypoints/openai/api_server.py`（OpenAI 兼容 API server）；`cli/serve.py` 负责解析参数并经由 `launcher.py` 拉起它。
- **练习 2**：为什么离线推理和在线服务放在同一个 `entrypoints/` 目录？
  - **答案**：它们都是「用户进入系统的入口」，共享输入处理（tokenization、多模态）等逻辑，集中放置便于复用与维护。

### 4.4 vllm/v1：当前 V1 引擎架构（核心）

#### 4.4.1 概念说明

**这是本讲最重要、也是后续多讲的主战场。** `vllm/v1` 是 vLLM 当前的引擎架构（V1）。它用多进程把「接收请求 / 调度 / 在 GPU 上执行」拆开，以最大化吞吐。理解 `v1/` 的内部目录划分，就等于理解了 vLLM 的运行时骨架。

> **V1 当前架构标记**：在后续讲义里，凡是提到「调度器」「引擎核心」「worker」「采样器」「KV 缓存管理」，默认都在 `vllm/v1` 下。

#### 4.4.2 核心流程

V1 的进程架构（来自 [arch_overview.md:L81-L127](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L81-L127)）有三类核心进程，对应 `v1/` 里的不同子目录：

1. **API Server 进程**：处理 HTTP、做输入处理（tokenization、多模态加载），通过 ZMQ 与引擎核心通信。代码入口在 `entrypoints/`（前述）与 `vllm/v1/utils.py`。
2. **Engine Core 进程**：跑调度器、管理 KV 缓存、协调 worker。每个 data-parallel rank 一个。代码在 `vllm/v1/engine/core.py` 与 `vllm/v1/engine/utils.py`。
3. **GPU Worker 进程**：每张 GPU 一个，加载权重、跑 forward。代码在 `vllm/v1/executor/multiproc_executor.py` 与 `vllm/v1/worker/gpu_worker.py`。
4. （可选）**DP Coordinator 进程**：仅在数据并行时存在，做负载均衡。代码在 `vllm/v1/engine/coordinator.py`。

进程数量公式（[arch_overview.md:L117-L127](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L117-L127)）：

| 进程类型 | 数量 |
| --- | --- |
| API Server | `A`（默认等于 `DP`） |
| Engine Core | `DP`（默认 1） |
| GPU Worker | `N = DP × PP × TP` |
| DP Coordinator | `DP > 1` 时为 1，否则 0 |

例如 4 卡 `tp=4`：1 个 API server + 1 个 Engine Core + 4 个 worker = **6 个进程**。

`v1/` 内部的子目录职责如下：

| 子目录 | 职责 |
| --- | --- |
| `v1/engine/` | 引擎核心主循环、AsyncLLM、输入处理、detokenizer、输出处理 |
| `v1/core/` | 调度器（`core/sched/`）、KV 缓存管理（`kv_cache_manager.py`、`block_pool.py`） |
| `v1/worker/` | GPU/CPU worker 与 model runner（准备输入、跑 forward、捕获 cudagraph） |
| `v1/executor/` | 拉起并管理 worker 进程（uniproc/multiproc/ray） |
| `v1/attention/` | V1 注意力后端（FlashAttention、FlashInfer、MLA 等） |
| `v1/sample/` | 采样器与采样 metadata |
| `v1/spec_decode/` | 推测解码（EAGLE、n-gram 等） |
| `v1/structured_output/` | 结构化输出（xgrammar、guidance） |
| `v1/metrics/` | 指标与可观测性 |
| `v1/request.py` / `v1/outputs.py` | V1 内部请求与输出数据结构 |

#### 4.4.3 源码精读

引擎核心类 `EngineCore` 定义在（[vllm/v1/engine/core.py:L103](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L103)）：

```python
class EngineCore:
    ...
```

而它被包装成可在独立进程里运行的 `EngineCoreProc`（[vllm/v1/engine/core.py:L1008](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1008)）：

```python
class EngineCoreProc(EngineCore):
    ...
```

这正是「Engine Core 是一个独立进程」在代码层面的体现——`EngineCore` 是逻辑，`EngineCoreProc` 给它加上了进程化、ZMQ 通信、busy loop 等外壳。`arch_overview.md` 也确认 Engine Core 代码位于这两个文件（[arch_overview.md:L93-L99](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L93-L99)）。

#### 4.4.4 代码实践

1. **实践目标**：把进程架构映射到 `v1/` 的具体文件。
2. **操作步骤**：
   - 读 [arch_overview.md:L81-L127](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L81-L127)。
   - 在 `v1/` 里找到与「Engine Core / Worker / Executor / 调度 / 采样」对应的子目录。
3. **需要观察的现象**：四类进程的代码落点都能在 `v1/` 下找到。
4. **预期结果**：写出一行映射，如「Engine Core → `v1/engine/core.py`，Worker → `v1/worker/gpu_worker.py`，调度 → `v1/core/sched/`」。
5. 纯阅读型实践，无需 GPU。

#### 4.4.5 小练习与答案

- **练习 1**：给定 `tp=2, dp=4`（共 8 卡），各类进程分别多少个？
  - **答案**：API server 默认 = `dp` = 4；Engine Core = `dp` = 4；GPU Worker = `dp×tp` = 8；DP Coordinator = 1（因为 `dp>1`）。合计 4+4+8+1 = **17 个进程**。
- **练习 2**：为什么要把调度（Engine Core）和执行（Worker）拆到不同进程？
  - **答案**：让 CPU 上的调度循环与 GPU 上的计算重叠，调度器可以在 worker 算的同时为下一步准备输入，从而提高吞吐；进程隔离也让调度与执行互不阻塞。

### 4.5 vllm/engine 与 vllm/config：引擎与全局配置

#### 4.5.1 概念说明

这里讲两个相邻但命运不同的目录：

- **`vllm/engine/`**：**旧引擎目录**。vLLM 早期（V0）的 `LLMEngine` / `AsyncLLMEngine` 住在这里。随着 V1 成为默认架构，这个目录现在主要是**兼容别名**——保留旧导入路径不报错，但实际指向 V1 实现。
- **`vllm/config/`**：**全局配置体系**。vLLM 把所有配置聚合成一个对象 `VllmConfig`，它像「引擎级全局状态」一样在各个类之间传递。`config/` 目录把这个大对象和它的子配置拆成了多个文件。

#### 4.5.2 核心流程

`engine/` 的兼容化流程：

1. 旧代码 `from vllm.engine.llm_engine import LLMEngine`。
2. 该文件不再含真正实现，而是从 V1 导入并起别名。
3. 因此无论从旧路径还是新路径导入，得到的都是 V1 的 `LLMEngine`。

`config/` 的聚合流程：

1. 各个子配置（`ModelConfig`、`CacheConfig`、`SchedulerConfig`、`ParallelConfig`、`QuantizationConfig` 等）各自定义在 `config/` 下的文件里。
2. `VllmConfig`（在 `config/vllm.py`）把它们组合成一个对象。
3. 这个对象在引擎、worker、模型之间一路传递，任何类按需读取自己关心的字段。

`arch_overview.md` 把 `VllmConfig` 描述为「可被当作引擎级全局状态、在所有 vLLM 类之间共享」（[arch_overview.md:L313-L315](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L313-L315)）。

#### 4.5.3 源码精读

旧引擎文件现在是别名 shim（[vllm/engine/llm_engine.py:L4-L6](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/engine/llm_engine.py#L4-L6)）：

```python
from vllm.v1.engine.llm_engine import LLMEngine as V1LLMEngine

LLMEngine = V1LLMEngine  # type: ignore
"""The `LLMEngine` class is an alias of vllm.v1.engine.llm_engine.LLMEngine."""
```

这段代码就是「`engine/` 是旧路径、`v1/` 才是真身」的铁证。

`VllmConfig` 在配置包里被导出（[vllm/config/__init__.py:L55](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/__init__.py#L55) 与 [L139](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/__init__.py#L139)）：

```python
from .vllm import (
    ...
    VllmConfig,
    ...
)
```

`config/` 下的文件按子配置切分，例如 `model.py`、`cache.py`、`scheduler.py`、`parallel.py`、`quantization.py` 等——每个文件对应一类配置，这正是 u3 系列讲义要逐个精读的内容。

#### 4.5.4 代码实践

1. **实践目标**：亲手验证 `engine/` 的别名性质，并数清 `config/` 的子配置文件。
2. **操作步骤**：
   - `cat vllm/engine/llm_engine.py`，确认它只是从 `v1` 导入并赋别名。
   - `ls -1 vllm/config`，数一数有多少个 `*.py` 配置文件。
3. **需要观察的现象**：`engine/llm_engine.py` 极短（仅几行）；`config/` 下有二十多个配置文件。
4. **预期结果**：能解释「旧路径仍可用但已指向 V1」，并说出至少 3 个配置文件名（如 `cache.py`、`scheduler.py`、`parallel.py`）。
5. 离线即可完成。

#### 4.5.5 小练习与答案

- **练习 1**：既然 `vllm/engine/` 已经是别名，为什么不直接删掉它？
  - **答案**：为了向后兼容——大量第三方代码和旧示例仍用 `from vllm.engine...` 导入；保留别名可以避免升级 vLLM 后这些代码立刻报错。
- **练习 2**：为什么要把配置拆成 `VllmConfig` 一个大对象到处传，而不是每个类只接收自己需要的参数？
  - **答案**：见 `arch_overview.md` 的「可扩展性」设计（[L215-L228](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L215-L228)）：新增功能时只需在 `VllmConfig` 里加字段，无需改动引擎/worker/模型的构造函数签名，避免层层透传参数的麻烦。

### 4.6 vllm/model_executor 与 vllm/distributed：模型实现与并行

#### 4.6.1 概念说明

最后两个目录回答「模型长什么样」和「怎么在多卡上跑」：

- **`vllm/model_executor/`**：存放**所有模型实现**与**可复用的层**。这是 vLLM 能支持 200+ HF 模型的根本所在——每种架构在这里都有一个对应实现，而所有实现又共用同一套并行层（linear、attention、embedding 等）。
- **`vllm/distributed/`**：存放**并行与进程组**基础设施。张量并行（TP）、流水并行（PP）、数据并行（DP）的协调、跨进程/跨机通信都在这里。

#### 4.6.2 核心流程

模型落地的流程：

1. `model_executor/models/` 里放具体模型（Llama、Qwen、LLaVA 等数百个文件）。
2. `model_executor/models/registry.py` 维护「HF 架构名 → vLLM 模型类」的映射。
3. 模型内部不自己写矩阵乘，而是用 `model_executor/layers/` 下的并行层（如 `ColumnParallelLinear`、`RowParallelLinear`、`Attention`、`VocabParallelEmbedding`）。
4. `model_executor/model_loader/` 负责加载权重（并支持初始化时分片/量化）。

并行的流程：

1. `distributed/parallel_state.py` 在启动时初始化进程组，划分 TP/PP/DP 组。
2. 模型里的并行层根据当前进程所在的组，对权重做切分，并在前向时插入 `all-reduce` / `all-gather` 等通信。

`arch_overview.md` 的「Worker」一节指出 worker 用 `rank`（全局编排）与 `local_rank`（分配设备、访问本地资源）来标识（[arch_overview.md:L186-L194](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L186-L194)）。

#### 4.6.3 源码精读

`model_executor/layers/` 下集中了可复用的算子层，例如：

```
vllm/model_executor/layers/
├── linear.py                 # 并行线性层（列/行并行、QKV 合并等）
├── attention/                # 注意力层抽象
├── rotary_embedding/         # 旋转位置编码 RoPE
├── vocab_parallel_embedding.py
├── fused_moe/                # 融合 MoE 内核与层
└── quantization/             # 量化（FP8、GPTQ、AWQ 等）
```

`model_executor/models/` 下有约 **288 个条目**（含子目录），覆盖了绝大多数主流开源模型——这就是「200+ 架构」在文件层面的体量。

`distributed/parallel_state.py` 提供了进程组初始化的两个关键入口（[vllm/distributed/parallel_state.py:L1588](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/distributed/parallel_state.py#L1588) 与 [L1746](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/distributed/parallel_state.py#L1746)）：

```python
def init_distributed_environment(...): ...
def initialize_model_parallel(...): ...
```

此外，`arch_overview.md` 解释了一个贯穿 `model_executor` 与 `config` 的设计——**初始化时分片/量化**（[arch_overview.md:L282-L301](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L282-L301)）：与其先加载完整权重再切分（峰值显存爆炸），不如在每层初始化时就只创建自己那一片。这也是为什么模型构造签名统一为 `__init__(self, *, vllm_config, prefix="")`（[arch_overview.md:L241-L247](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L241-L247)）。

#### 4.6.4 代码实践

1. **实践目标**：感受模型实现的规模，并把并行基础设施对上号。
2. **操作步骤**：
   - `ls -1 vllm/model_executor/layers | head`，浏览可复用层。
   - `ls -1 vllm/model_executor/models | wc -l`，感受模型数量。
   - 打开 `vllm/distributed/parallel_state.py`，定位上面两个初始化函数。
3. **需要观察的现象**：层目录里能看到 `linear.py`、`attention/`、`quantization/` 等；模型目录条目极多。
4. **预期结果**：能说出「模型实现 + 可复用并行层 + 权重加载」是 `model_executor` 的三大组成；`distributed` 负责进程组与通信。
5. 离线即可完成。

#### 4.6.5 小练习与答案

- **练习 1**：为什么模型不直接用 `torch.nn.Linear`，而要用 `model_executor/layers/linear.py` 里的并行层？
  - **答案**：并行层会在初始化时按 TP 切分权重，并在前向时自动插入必要的集合通信，让同一份模型代码在单卡和多卡上都能正确运行。
- **练习 2**：`rank` 和 `local_rank` 有何区别？
  - **答案**：`rank` 是全局唯一编号，用于跨进程/跨机编排；`local_rank` 是单机内的本地编号，主要用来指定使用哪张 GPU、访问本机共享内存等本地资源。

## 5. 综合实践

把本讲的地图知识串起来，完成一份**仓库目录树说明书**：

1. 在仓库根目录执行 `ls -1` 与 `ls -1 vllm`，绘制一份目录树（至少包含顶层主要目录与 `vllm/` 下的关键子目录）。
2. 为 `vllm/` 下**至少 8 个子目录**各写一句话职责说明，至少覆盖：`entrypoints`、`v1`、`engine`、`config`、`model_executor`、`distributed`、`attention`、`compilation`、`multimodal`、`platforms`、`lora`（任选 8 个）。
3. 在树状图里**用标记（如 `★ V1 当前架构`）标出哪些目录属于 V1 当前架构**——记住：`vllm/v1/` 整体是当前架构，`vllm/engine/` 是旧兼容层。
4. 用一条「请求流动线」把目录串起来：用户请求 → `entrypoints/`（HTTP/LLM）→ `v1/engine/`（Engine Core 调度）→ `v1/worker/`（执行）→ 内部调用 `model_executor/` 的模型 → 模型里用 `distributed/` 做并行。把这条线写在你的说明书底部。

**预期产物**：一份 Markdown 或文本文件，含目录树 + 每个目录一句话 + V1 标记 + 请求流动线。这是你后续阅读所有讲义时的「随身地图」。

## 6. 本讲小结

- vLLM 仓库顶层同时包含 Python 包（`vllm/`）、原生扩展源码（`csrc/`/`cmake/`/`rust/`）、构建脚本（`setup.py`/`CMakeLists.txt`）、文档/示例/测试/基准。
- `vllm/__init__.py` 用 `MODULE_ATTRS` + `__getattr__` 实现**懒加载**，让 `import vllm` 不立即拖入全部重型模块。
- `entrypoints/` 是两扇门：离线推理用 `LLM` 类，在线服务用 `vllm serve` / `api_server.py`。
- **`vllm/v1/` 是 V1 当前架构**，按 `engine`/`core`/`worker`/`executor`/`attention`/`sample` 等子目录承载调度、引擎核心、执行、采样等运行时职责。
- `vllm/engine/` 是**旧引擎兼容层**（`llm_engine.py` 已是 V1 实现的别名）；`vllm/config/` 用 `VllmConfig` 聚合所有子配置，作为引擎级全局状态。
- `model_executor/` 放模型实现与可复用并行层，`distributed/` 管进程组与 TP/PP/DP 通信；二者共同支撑「初始化时分片/量化」的设计。

## 7. 下一步学习建议

有了地图之后，下一步建议：

- **先跑通一次推理**：进入 u2 单元，按 [u2-l1 离线推理：LLM 类与 generate/chat](u2-l1-offline-llm-class.md) 用 `LLM` 类生成文本，把 `entrypoints/llm.py` 用起来。
- **再深入配置**：按 [u3-l2 VllmConfig 全局配置对象](u3-l2-vllm-config-system.md) 把本讲提到的 `config/` 子配置逐个读懂。
- **想理解运行时**：直接跳到 u3/u4 单元，沿着 `v1/engine` → `v1/core/sched` → `v1/worker` 的顺序看请求如何流动。
- **想理解模型与并行**：按 u6 单元（`model_executor` 的层、注意力、注册）与 u9-l1（`distributed` 的并行）展开。
