# 目录结构与代码地图：lang 与 srt

## 1. 本讲目标

SGLang 是一个体量极大的项目，`python/sglang/` 顶层就有十几个目录，运行时 `srt/` 下又细分成 40 多个子系统。本讲不深入任何机制，目标是给你一张「能在哪个目录找到哪类问题」的导航地图。

学完后你应该能够：

- 记住 `lang/`、`srt/`、`cli/`、`kernels/`、`test/` 五个顶层目录各自的职责。
- 说出 `import sglang` 时，公共 API 是从哪里导出来的，以及为什么 `Engine` 这个名字会被「重新赋值」一次。
- 在 `srt/` 的 40 多个子目录里，快速定位「请求入口、调度、模型执行、KV 缓存、算子、分布式」分别归哪个目录管。
- 独立绘制一份 sglang 目录思维导图，作为后续阅读源码的索引。

## 2. 前置知识

阅读本讲前，请确认你已经理解 u1-l1 建立的两个核心认知：

1. **两层架构**：SGLang 物理上分为前端语言层 `lang/`（用 `@function`、`gen`、`select` 等 DSL 描述生成流程）与运行时层 `srt/`（加载模型、调度 batch、GPU 执行、管理 KV 缓存与分布式）。前端 `lang` 是可选的，多数用户直接用运行时入口。
2. **`Engine` 是别名**：`sglang.Engine` 这个名字在前端 `lang.api` 与运行时 `srt.entrypoints.engine` 各被定义一次，最终生效的是运行时引擎。

下面要补充的两个概念是「**包（package）**」和「**懒导入（LazyImport）**」：

- Python 的「包」就是一个含 `__init__.py` 的目录。`import sglang` 实际执行的就是 `python/sglang/__init__.py` 这个文件。所以「公共 API 清单」本质上就是这个 `__init__.py`。
- 「懒导入」指把真正的 `import` 推迟到第一次使用时才执行。SGLang 用它避免一启动就加载 torch、cuda 等重依赖。你会看到很多 API 用 `LazyImport(...)` 注册名字，但真正 import 发生在你第一次调用它时。

## 3. 本讲源码地图

本讲涉及的关键文件，按阅读顺序列出：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/README.md#L1-L19) | 顶层目录的一句话说明，最权威的「官方地图」 |
| [__init__.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L1-L116) | `import sglang` 真正执行的文件，公共 API 的总清单 |
| [launch_server.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/launch_server.py#L15-L52) | 服务启动的总分发：根据参数选择 HTTP / gRPC / Ray / PD 分离 |
| [cli/main.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/cli/main.py#L16-L46) | `sglang serve / generate / version` 子命令分发 |
| [lang/api.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L1-L51) | 前端 DSL 的公共函数：`function`、`gen`、`select` 等 |
| [srt/entrypoints/http_server.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L14-L15) | 运行时 HTTP 服务入口（SRT = SGLang Runtime） |
| [srt/server_args.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L413) | 运行时配置中枢 `ServerArgs` 所在地 |

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 顶层包结构**、**4.2 srt 子系统概览**。

### 4.1 顶层包结构

#### 4.1.1 概念说明

`python/sglang/` 既是项目根目录下的一个 Python 包，也是你 `pip install sglang` 后导入的那个 `sglang`。它由三类东西组成：

1. **入口脚本**：`launch_server.py`、`bench_one_batch.py` 等可独立运行的脚本。
2. **子包**：`lang/`、`srt/`、`cli/`、`kernels/`、`test/` 等目录。
3. **公共 API 装配文件**：`__init__.py`，它决定 `import sglang` 后你拿到哪些名字。

一句话定位每个顶层目录（与官方 [README.md](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/README.md#L1-L19) 一致）：

| 目录/文件 | 一句话职责 |
| --- | --- |
| `lang/` | 前端 DSL：用代码描述生成流程（可选层） |
| `srt/` | 运行时引擎：模型加载、调度、GPU 执行（SRT = SGLang Runtime） |
| `cli/` | 命令行入口：`sglang serve / generate / version` |
| `kernels/` | 高性能算子注册表（JIT / AOT） |
| `test/` | 测试工具与 CI 测试 |
| `eval/` | 评测工具（accuracy） |
| `multimodal_gen/` | 图像/视频生成推理框架 |
| `benchmark/` | 基准测试脚本 |

#### 4.1.2 核心流程

理解顶层结构的关键，是看清「一条请求从外部进入运行时」经过的目录链：

```text
用户代码
  │
  ├─ 命令行：sglang serve ... ──────► cli/main.py（分发） ──► cli/serve.py
  │                                                          │
  ├─ 同进程：sglang.Engine(...) ─┐                            │
  │                              ▼                            ▼
  └─ HTTP：POST /v1/... ──► srt/entrypoints/（http_server / engine）
                                    │
                                    ▼
                            srt/managers/（调度核心）
                                    │
                                    ▼
                          srt/model_executor/（GPU 执行）
```

不管你从哪个入口进来，最终都会汇聚到 `srt/`。`launch_server.py` 就是这层「入口分发」的集中体现。

#### 4.1.3 源码精读

**(1) `import sglang` 做了什么**

[`__init__.py`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L34-L65) 前半段先导入前端 DSL 的公共函数，这是 `@function`、`gen`、`select`、`image`、`video` 等名字的来源：

```python
# Frontend Language APIs
from sglang.global_config import global_config
from sglang.lang.api import (
    Engine,
    Runtime,
    assistant,
    function,
    gen,
    ...
    select,
    ...
    video,
)
```

注意第 37 行这里也导入了一个名为 `Engine` 的名字——它是前端的 `Engine`（实际转发到运行时，见 4.1.3(3)）。但紧接着在文件后半段，[`__init__.py:78-79`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L77-L79) 用懒导入**重新定义了同名变量**：

```python
# Runtime Engine APIs
ServerArgs = LazyImport("sglang.srt.server_args", "ServerArgs")
Engine = LazyImport("sglang.srt.entrypoints.engine", "Engine")
```

因为这两行在文件中靠后，会**覆盖**前面导入的前端 `Engine`。这正是 u1-l1 提到的「`Engine` 最终指向运行时引擎」的代码依据。这两行还顺带暴露了运行时配置中枢 `ServerArgs`。

中间的 [`__init__.py:68-75`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L67-L75) 用 `LazyImport` 注册了一批第三方后端名字：

```python
from sglang.utils import LazyImport
...
OpenAI = LazyImport("sglang.lang.backend.openai", "OpenAI")
```

`LazyImport` 的作用是：先给名字 `OpenAI`，但只有在你第一次真正调用它时，才会去 `import torch` 这类重依赖。这样 `import sglang` 本身保持轻量。

**(2) 服务启动的总分发：`launch_server.py`**

[`run_server`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/launch_server.py#L15-L52) 是一个纯分支函数，根据 `server_args` 的标志选择不同的服务形态：

```python
def run_server(server_args):
    if server_args.encoder_only:        # PD 分离中的 encode 节点
        ...                             # → srt/disaggregation/encode_server
    elif server_args.smg_grpc_mode:     # gRPC 模式
        ...                             # → srt/entrypoints/grpc_server
    elif server_args.use_ray:           # Ray 后端
        ...                             # → srt/ray/http_server
    else:                               # 默认 HTTP 模式
        from sglang.srt.entrypoints.http_server import launch_server
        launch_server(server_args)
```

这段代码本身就是一张「服务形态 → 落在哪个目录」的对照表：默认 HTTP 模式进 `srt/entrypoints/http_server.py`，这是后续服务端架构单元的主入口。

> 提示：`python -m sglang.launch_server` 仍然可用，但官方更推荐 `sglang serve`。见 [`launch_server.py:55-62`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/launch_server.py#L55-L62) 的告警。

**(3) 前端公共 API 的真实实现：`lang/api.py`**

虽然 `__init__.py` 暴露了 `function`、`gen`、`select` 等名字，它们的真实定义在前端 [`lang/api.py`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L23-L32)：

```python
def function(func=None, num_api_spec_tokens=None):
    if func:
        return SglFunction(func, num_api_spec_tokens=num_api_spec_tokens)
    ...
```

而前端 `Engine` 也只是转发到运行时引擎，见 [`lang/api.py:42-46`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L42-L46)：

```python
def Engine(*args, **kwargs):
    from sglang.srt.entrypoints.engine import Engine
    return Engine(*args, **kwargs)
```

这说明：**前端 `lang/` 不自带推理引擎，它最终还是要去 `srt/` 取模型能力**。理解这一点，你就能明白为什么运行时 `srt/` 才是项目的主体。

**(4) 配置中枢：`global_config.py` 的历史包袱**

[`global_config.py`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py#L1-L3) 顶部有一句重要注释：

```python
# FIXME: deprecate this file and move all usage to sglang.srt.environ or sglang.__init__.py
```

它说明：历史上运行时配置散落在模块级全局变量里，项目正在把它们迁移到 `srt/environ.py` 的环境变量体系（u3-l5 会专门讲）。看到 `global_config` 时要意识到它属于「待迁移」状态。

#### 4.1.4 代码实践

**实践目标**：用源码验证「`import sglang` 到底暴露了哪些名字」。

**操作步骤**：

1. 在能联网、已 `pip install "sglang[all]"` 的环境里，打开 Python REPL。
2. 执行下面这段「源码阅读型」脚本（不需要真实起服务，所以用 try/except 包住）：

   ```python
   import sglang
   print("version:", sglang.__version__)
   # 看 __init__.py 的 __all__ 清单里都有哪些名字
   print("public names count:", len(sglang.__all__))
   # 验证 Engine 指向运行时引擎
   print("Engine module hint:", type(sglang.Engine).__name__)
   ```

3. 对照 [`__init__.py:81-116`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L81-L116) 的 `__all__`，确认 REPL 打印的数量是否一致。

**需要观察的现象**：`sglang.Engine` 在不调用时不会真正 import torch（懒导入），因此 `type(...).__name__` 反映的是 `LazyImport` 包装器的类型，而不是真正的 `Engine` 类。

**预期结果**：`public names count` 与 `__all__` 长度相等；不调用 `sglang.Engine(...)` 时不会触发重依赖导入。若环境未装好依赖，会得到 ImportError，此时应标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 里 `Engine` 出现了两次，最终生效的是哪一个？

**答案**：第一次出现在 [`__init__.py:37`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L36-L59) 的 `from sglang.lang.api import (... Engine ...)`，是前端转发版；第二次出现在 [`__init__.py:79`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L77-L79) 的 `Engine = LazyImport("sglang.srt.entrypoints.engine", "Engine")`。后者靠后执行，覆盖了前者，所以最终 `sglang.Engine` 指向运行时引擎 `sglang.srt.entrypoints.engine.Engine`。

**练习 2**：`launch_server.run_server` 有四个分支，分别对应哪四类服务形态？

**答案**：`encoder_only`（PD 分离的 encode 节点，进 `srt/disaggregation/`）、`smg_grpc_mode`（gRPC，进 `srt/entrypoints/grpc_server.py`）、`use_ray`（Ray 后端，进 `srt/ray/http_server.py`）、默认（HTTP，进 `srt/entrypoints/http_server.py`）。

---

### 4.2 srt 子系统概览

#### 4.2.1 概念说明

`srt/`（SGLang Runtime）是项目的主体，目录开头的注释把它定义得非常清楚——[`http_server.py:14-15`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L14-L15)：

```python
"""
The entry point of inference server. (SRT = SGLang Runtime)
This file implements HTTP APIs for the inference engine via fastapi.
```

`srt/` 下有 40 多个子目录，初看会让人迷失。诀窍是把它们按「请求生命周期」归到几条主线上：

- **入口层**：请求怎么进来。
- **调度层**：请求怎么被组装成 batch。
- **执行层**：batch 怎么在 GPU 上跑。
- **存储层**：KV 缓存怎么管。
- **算子层**：具体计算怎么做。
- **扩展层**：各种高级特性。
- **基础设施层**：配置、分布式、平台、可观测性。

#### 4.2.2 核心流程

下表把最重要的子目录按「请求生命周期」串起来，建议你把它当作速查表：

| 子目录 | 一句话职责 | 代表文件 |
| --- | --- | --- |
| `entrypoints/` | HTTP/gRPC/Engine 等所有服务入口 | `http_server.py`、`engine.py`、`openai/` |
| `managers/` | 调度核心：TokenizerManager/Scheduler/DetokenizerManager | `scheduler.py`、`schedule_batch.py`、`io_struct.py` |
| `model_executor/` | GPU 一次前向的编排：ModelRunner、ForwardBatch、CUDA graph | `model_runner.py`、`forward_batch_info.py` |
| `model_loader/` | 模型加载与自动注册 | `loader.py`、`auto_loader.py` |
| `models/` | 各模型的具体实现 | `llama.py` 等 |
| `mem_cache/` | KV 缓存内存池与 RadixAttention 前缀缓存 | `radix_cache.py`、`memory_pool.py` |
| `layers/` | 神经网络层：注意力、量化、线性层、采样 | `attention/`、`quantization/`、`sampler.py` |
| `distributed/` | TP/PP/DP 并行状态与集合通信 | `parallel_state.py`、`device_communicators/` |
| `sampling/` | 采样参数与 logits 处理 | `sampling_params.py`、`sampling_batch_info.py` |
| `configs/` | 模型与服务配置 | `model_config.py` |
| `server_args.py` | 运行时配置中枢 `ServerArgs` | （单文件） |
| `runtime_context.py` | 运行时上下文与资源租约 | （单文件） |
| `environ.py` | `SGLANG_*` 环境变量集中定义 | （单文件） |

另外有一批「高级特性」子目录，每个对应一种可独立学习的能力（这些是后续进阶/专家单元的主题）：

| 子目录 | 一句话职责 |
| --- | --- |
| `speculative/` | 投机解码（EAGLE/NGRAM 等） |
| `disaggregation/` | PD（prefill/decode）分离式部署 |
| `lora/` | LoRA 多适配器推理 |
| `constrained/` | 结构化输出 / 约束解码（JSON schema、正则） |
| `multimodal/` | 多模态（图像/视频/音频）输入处理 |
| `eplb/` | MoE 专家并行负载均衡 |
| `platforms/` | 设备抽象（CUDA/ROCm/CPU） |
| `hardware_backend/` | 非默认硬件后端（MLX、Ascend NPU、Intel XPU） |
| `compilation/` | torch.compile 与编译 pass |
| `plugins/` | 插件钩子注册 |
| `observability/` | 指标与 trace 采集 |
| `checkpoint_engine/` | 权重热更新与检查点 |
| `kernels/`（顶层，非 srt 内） | JIT/AOT 高性能算子（见 4.1） |

> 小提示：`srt/` 下还有不少更细的目录（如 `arg_groups/`、`batch_overlap/`、`session/`、`function_call/`、`grpc/`、`ray/`、`kv_canary/`、`state_capturer/` 等），不必一次记住，遇到时回到本表对照即可。

#### 4.2.3 源码精读

**(1) 运行时配置中枢 `ServerArgs`**

整个 `srt/` 里最大的文件之一是 [`server_args.py`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L413)，里面的 [`class ServerArgs`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L413) 是「运行时单一配置源」——`--tp`、`--mem-fraction-static`、`--context-length` 等所有命令行参数最终都落到这个类上。文件末尾的 [`prepare_server_args`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8682) 负责解析命令行并构建 `ServerArgs`。本讲你只需记住：**要找某个运行时参数，先去 `srt/server_args.py`**。详细机制留到 u3-l3。

**(2) 服务入口的真实位置**

回到 [`http_server.py:14-15`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L14-L15) 的注释——它把 `srt/entrypoints/http_server.py` 明确标记为「inference server 的入口」。这个文件有 2600+ 行，但核心是两个函数：`launch_server`（拉起子进程并组装引擎，见 [L2648](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2648)）和 [`_setup_and_run_http_server`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2399-L2412)（设置全局状态、挂中间件、启动 uvicorn）：

```python
def _setup_and_run_http_server(server_args, tokenizer_manager, template_manager, port_args, ...):
    """Set up global state, configure middleware, and run uvicorn.
    Called by launch_server after subprocesses have been launched."""
```

注释里的「Called by launch_server after subprocesses have been launched」揭示了 srt 进程拓扑的起点——它会在拉起子进程（TokenizerManager / Scheduler / DetokenizerManager）之后再启动 HTTP 服务。这正是 u3-l2 要讲的进程拓扑。

**(3) 配置如何影响启动路径**

`server_args` 的字段直接决定了 `launch_server.run_server` 走哪个分支（见 4.1.3(2)）。这说明 `srt/server_args.py`（配置）与 `launch_server.py`（分发）是一对：前者定义「有哪些开关」，后者定义「开关拨到哪里会进哪个目录」。

#### 4.2.4 代码实践

**实践目标**：把本讲的「速查表」变成你自己的、经过源码核对的思维导图。

**操作步骤**：

1. 在 `srt/` 下逐个核对下表中列出的子目录确实存在，并为每个子目录**找出一个代表文件**（用 `ls` 或在 IDE 里展开目录即可，不运行代码）：

   | 子目录 | 你要找的代表文件（建议） |
   | --- | --- |
   | `managers/` | `scheduler.py` |
   | `model_executor/` | `model_runner.py` |
   | `mem_cache/` | `radix_cache.py` |
   | `layers/` | `radix_attention.py`（在 `layers/` 下） |
   | `distributed/` | `parallel_state.py` |
   | `speculative/` | `spec_registry.py` |
   | `disaggregation/` | `prefill.py` |
   | `lora/` | `lora_manager.py` |
   | `constrained/` | `grammar_manager.py` |

2. 对每个代表文件，用一句话写出它的职责（可以参考本讲的速查表，但要用自己的话）。

3. 把上面 9 个子目录按「请求生命周期」画成一张树状思维导图：根节点是 `srt/`，下分「入口 / 调度 / 执行 / 存储 / 扩展」五条枝。

**需要观察的现象**：你会确认这 9 个子目录都真实存在，并且每个都至少有一个「顾名思义」的代表文件。

**预期结果**：得到一份 9 节点 + 5 分枝的思维导图。如果某个目录名与代表文件对不上（例如 `lora/` 下没有 `lora_manager.py`），说明你查错了，请回查 `ls srt/lora/`。

> 待本地验证项：代表文件的存在性依赖当前 HEAD，本讲引用的文件名均基于当前仓库核对，但若你切到其他分支可能略有差异。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 bug 表现为「生成结果不符合 JSON schema」，你应该去哪个子目录排查？

**答案**：`srt/constrained/`（结构化输出 / 约束解码），代表文件 `grammar_manager.py`。约束解码通过在采样时施加文法 mask 来保证输出合法。

**练习 2**：`srt/` 下负责「GPU 一次前向」的是哪个子目录？它和 `srt/managers/` 的边界在哪？

**答案**：`srt/model_executor/`（代表文件 `model_runner.py`、`forward_batch_info.py`）。边界是：`managers/` 负责「把哪些请求组成一个 batch、何时送 GPU」（CPU 侧调度），`model_executor/` 负责「这个 batch 在 GPU 上具体怎么算」（GPU 侧执行）。

**练习 3**：为什么 `srt/server_args.py` 既被 `launch_server.py` 用，又被 `srt/entrypoints/http_server.py` 用？

**答案**：因为 `ServerArgs` 是「运行时单一配置源」。`launch_server.py` 读它的字段决定走哪种服务形态（HTTP/gRPC/Ray/PD），`http_server.py` 读它的字段决定如何启动 HTTP 服务、是否开 metrics 等。两个文件共用同一份配置，避免参数在多处各定义一套。

---

## 5. 综合实践

把本讲的两个最小模块串起来，完成一份「**带入口链路的 sglang 全景地图**」：

1. 画一张从「用户」出发的流程图，包含三条入口分支：
   - 命令行 `sglang serve` → `cli/main.py` → `cli/serve.py`；
   - 同进程 `sglang.Engine(...)` → `__init__.py` 的懒导入 → `srt/entrypoints/engine.py`；
   - HTTP `POST /v1/...` → `srt/entrypoints/http_server.py`。
2. 三条分支汇聚到 `srt/` 后，标注请求接下来依次经过 `entrypoints/` → `managers/`（调度）→ `model_executor/`（GPU 执行）→ `mem_cache/`（KV 缓存）这条主链。
3. 在图侧边单独列出 `server_args.py`、`runtime_context.py`、`environ.py` 三个「基础设施」文件，用一句话说明它们为何被整条主链共用（答案：它们是配置/上下文/环境变量的单一来源）。

完成后，你应该能用这张图回答「我要找 X 功能，该去哪个目录」的任意提问。这就是后续所有单元阅读源码的索引。

## 6. 本讲小结

- `python/sglang/` 顶层分为入口脚本（`launch_server.py`、`bench_*.py`）、子包（`lang/`、`srt/`、`cli/`、`kernels/`、`test/`）和公共 API 装配文件 `__init__.py`。
- `import sglang` 执行的是 `__init__.py`，其中 `Engine` 被定义两次，靠后的懒导入 `LazyImport("sglang.srt.entrypoints.engine", "Engine")` 生效，所以 `sglang.Engine` 指向运行时引擎。
- `launch_server.run_server` 是服务形态总分发：根据 `server_args` 的标志进入 `srt/entrypoints/`、`srt/ray/` 或 `srt/disaggregation/`。
- `srt/`（SGLang Runtime）是项目主体，40+ 子目录可按「入口 / 调度 / 执行 / 存储 / 算子 / 扩展 / 基础设施」七条主线归类。
- 请求主链是 `entrypoints/` → `managers/` → `model_executor/` → `mem_cache/`；配置中枢是 `srt/server_args.py` 的 `ServerArgs`。
- 前端 `lang/` 不自带推理能力，最终都要去 `srt/` 取模型能力。

## 7. 下一步学习建议

本讲只建立了「地图」，没有进入任何机制的内部。建议按以下顺序深入：

1. **若你想先看前端**：进入 u2-l1「前端 DSL 基础」，读 `lang/` 下的 `function`、`gen`、`select` 与 `ir.py`。
2. **若你想直接看运行时（推荐）**：进入 u3-l1「服务启动全流程」，从 `sglang serve` 一路追到 `http_server.launch_server`，把本讲画的入口链路补上「拉起子进程」的细节。
3. **并行阅读**：随手翻一遍 [`srt/server_args.py`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L413) 里 `ServerArgs` 的字段注释（不必全读），感受「运行时配置源」的规模，为 u3-l3 做准备。
