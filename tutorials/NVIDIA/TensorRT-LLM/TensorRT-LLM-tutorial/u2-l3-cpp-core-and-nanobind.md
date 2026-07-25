# C++ 核心与 Python↔C++ 绑定

> 本讲是「仓库结构与代码地图」单元的第三讲，承接 u2-l2（`tensorrt_llm` Python 包与公共 API）。上一讲我们看到 `import tensorrt_llm` 时会执行 `_init()`，并出现了一句神秘的 `from .bindings import MpiComm`。本讲就回答一个问题：**那个 `bindings` 到底是什么、它从哪里来、为什么 TensorRT-LLM 既是 Python 库又是 C++ 库？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `cpp/` 目录里每个子系统的职责（kernels / runtime / batch_manager / executor）。
- 理解「一个 Python 扩展模块 `bindings` 是怎么由一堆 `.cpp` 编译出来的」。
- 看懂 `bindings.cpp` 把 C++ 能力组织成哪些命名空间（子模块），并能说出每个子模块大致暴露了哪些类。
- 复述 TensorRT-LLM 的核心设计口诀——**「Python 调度、C++ 加速」**——并解释为什么 `Scheduler` / `KVCacheManager` 是「接口在 Python、实现可选 C++」。
- 在真实仓库里定位绑定代码与构建脚本，并能动手核验绑定暴露了什么。

## 2. 前置知识

阅读本讲前，建议你已经了解（u1-l1、u2-l2 已建立）：

- **monorepo**：TensorRT-LLM 把 Python 代码（`tensorrt_llm/`）和 C++/CUDA 代码（`cpp/`）放在同一个仓库里一起构建。
- **运行时全链路**：`HF Model → LLM API → Executor → Scheduler → Model Forward → Decoder → Sampling → Tokens`。
- **两个后端**：PyTorch（默认）与 AutoDeploy（beta），二者**共享**同一套 C++ 核心。

本讲会补充两个新概念：

- **Python 扩展模块（extension module）**：C++ 代码经过编译，产出一个可以被 `import` 的 `.so`（Linux）/ `.pyd`（Windows）文件，Python 调用它就像调用普通模块。在 TensorRT-LLM 里，这个模块名叫 `bindings`。
- **nanobind**：一个现代的、轻量的 C++→Python 绑定生成器（pybind11 的「精神继任者」，更小更快）。你只要在 C++ 里写形如 `nb::class_<Foo>(m, "Foo")...` 的语句，nanobind 就会帮你把 C++ 类 `Foo` 暴露成 Python 类。

> 一句话先建立直觉：**TensorRT-LLM 用 Python 写「该干什么」（调度、配置、请求生命周期），用 C++/CUDA 写「怎么算得快」（kernel、缓存搬运、采样算子），二者之间靠一个叫 `bindings` 的编译模块粘合起来。**

## 3. 本讲源码地图

| 文件 / 目录 | 角色 |
|------|------|
| `cpp/tensorrt_llm/` | C++/CUDA 引擎室，按子系统分子目录 |
| `cpp/tensorrt_llm/CMakeLists.txt` | 顶层构建脚本：把各子系统编成静态库，再聚合成 `libtensorrt_llm.so` |
| `cpp/tensorrt_llm/nanobind/CMakeLists.txt` | 用 `nanobind_add_module` 编译出 Python 扩展模块 `bindings` |
| `cpp/tensorrt_llm/nanobind/bindings.cpp` | 绑定的总入口，定义 `NB_MODULE`，把各子系统的 `initBindings` 串起来 |
| `cpp/tensorrt_llm/nanobind/executor/` | `executor` 子模块绑定（配置 / 请求 / 响应类型） |
| `cpp/tensorrt_llm/nanobind/runtime/` | `internal.runtime` 子模块绑定（运行时工具、解码器、MoE 均衡器） |
| `cpp/tensorrt_llm/nanobind/batch_manager/` | `internal.batch_manager` 子模块绑定（KVCacheManager、LlmRequest、SequenceSlotManager） |
| `tensorrt_llm/_common.py` | Python 侧：`from .bindings import ...` 并以 `RTLD_GLOBAL` 加载 `libtensorrt_llm.so` |
| `docs/source/torch/arch_overview.md` | 官方架构说明，明确「C++ 绑定 + Python 接口」的可定制性 |
| `AGENTS.md` | 仓库指南，给出「Shared C++ Core (via Nanobind)」的定位 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 C++ 目录结构**、**4.2 nanobind 绑定**、**4.3 共享核心组件**。

### 4.1 C++ 目录结构

#### 4.1.1 概念说明

`cpp/tensorrt_llm/` 是整个项目「跑得快」的资本。它把所有性能关键路径都放在 C++/CUDA 里实现，而对外的「骨架」（API、配置、调度逻辑）放在 Python 里。理解 `cpp/` 的目录划分，就是理解「哪些东西被加速了」。

`AGENTS.md` 给出了一句精炼的定位——两条后端**共享**同一套 C++ 核心，并通过 nanobind 暴露给 Python：

> **Scheduling pipeline**: Scheduler → BatchManager (in-flight batching) → KV Cache Manager
> **Decoding pipeline**: Decoder (token generation orchestration) → Sampling

参见 [AGENTS.md:59-63](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/AGENTS.md#L59-L63)。

#### 4.1.2 核心流程

`cpp/tensorrt_llm/` 下的一级子目录可以这样归类记忆：

| 子目录 | 大致职责 | 你会在这里看到的代表文件 |
|--------|----------|--------------------------|
| `common/` | 跨子系统共享的基础设施：数据类型（`tllmDataType`）、量化描述（`quantization.h`）、字符串/日志工具 | `common/tllmDataType.h`、`common/quantization.h` |
| `kernels/` | **CUDA kernel**：注意力、采样、beam search、MoE 通信、KV cache 拷贝、各类融合 norm/GEMM | `kernels/decodingKernels.cu`、`kernels/decoderMaskedMultiheadAttention.cu`、`kernels/mlaKernels.cu` |
| `layers/` | C++ 层实现（可与 kernel 配合的高层算子封装） | `layers/` |
| `runtime/` | 运行时基础设施：显存 buffer 管理、CUDA stream、`ModelConfig`/`WorldConfig`、MPI/NCCL 通信器、GPT 解码器、MoE 负载均衡器 | `runtime/tllmBuffers.h`、`runtime/ncclCommunicator.h`、`runtime/gptJsonConfig.h` |
| `batch_manager/` | 请求与批管理：KVCacheManager（v1/v2）、cache 收发器（transceiver）、PeFT 缓存、调度块管理 | `batch_manager/kvCacheManagerV2Utils.h`、`batch_manager/cacheTransferLayer.h` |
| `executor/` | 执行器抽象与请求/响应**类型**定义（`executor.h`、`types.h`） | `executor/executor.h` |
| `nanobind/` | 绑定层：把上面这些 C++ 能力暴露成 Python 模块 `bindings` | `nanobind/bindings.cpp` |
| `thop/` | PyTorch 自定义算子（TorchOp）的 C++ 实现，经 `torch.classes.load_library` 加载 | `thop/` |
| `deep_ep/`、`deep_gemm/`、`flash_mla/`、`cutlass_extensions/` | 专用 kernel 库（DeepEP 专家通信、DeepGemm、FlashMLA、CUTLASS 扩展），按需编译 | — |

> 记忆口诀：`common`（地基）→ `kernels`/`layers`（加速）→ `runtime`（运行时）→ `batch_manager`/`executor`（编排）→ `nanobind`（对外暴露）。

#### 4.1.3 源码精读

顶层 `CMakeLists.txt` 的关键在于「分层编译」：先把每个子系统编成**静态库**，再用 `WHOLE_ARCHIVE` 整段塞进共享库 `libtensorrt_llm.so`。

先看子系统的静态库化与聚合：

[cpp/tensorrt_llm/CMakeLists.txt:143-165](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/CMakeLists.txt#L143-L165) —— 依次 `add_subdirectory(common/kernels/layers/runtime)`，再为 `batch_manager` 与 `executor` 定义静态库目标（`tensorrt_llm_batch_manager_static`、`tensorrt_llm_executor_static`）。

[cpp/tensorrt_llm/CMakeLists.txt:245-267](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/CMakeLists.txt#L245-L267) —— `add_library(tensorrt_llm SHARED)` 把所有 kernel/runtime 静态库链接进来，并对 `batch_manager`、`executor` 用 `$<LINK_LIBRARY:WHOLE_ARCHIVE,...>` 保证符号全部保留（否则 Python 侧动态加载时会找不到符号）。注意注释里点明了「Cyclic dependency」：`batch_manager` 与 `executor` 反过来又依赖主共享库。

最后，构建脚本在末尾才把绑定层加进来：

[cpp/tensorrt_llm/CMakeLists.txt:296](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/CMakeLists.txt#L296) —— `add_subdirectory(nanobind)`，编译 Python 扩展模块。

> 为什么要 `WHOLE_ARCHIVE`？因为 Python 在运行时通过符号名动态查找 C++ 类，链接器若按默认规则裁掉「没人引用」的静态库符号，绑定就会失败。`WHOLE_ARCHIVE` 强制保留全部符号。

#### 4.1.4 代码实践

**目标**：亲手核验 `cpp/` 的子系统划分。

**步骤**：

1. 在仓库根目录运行 `ls cpp/tensorrt_llm/`，对照本讲的「子目录职责表」逐项标注。
2. 进入 `cpp/tensorrt_llm/kernels/`，挑 3 个 `.cu` 文件，从文件名猜测它们加速了哪个环节（例如 `decodingKernels.cu` → 采样/解码、`kvCachePartialCopy.cu` → KV cache 搬运、`mlaKernels.cu` → MLA 注意力）。
3. 进入 `cpp/tensorrt_llm/batch_manager/`，确认存在 `kvCacheManagerV2Utils.h`（KV cache 管理 v2 的工具）。

**需要观察的现象**：`kernels/` 目录文件数量远多于其它目录——这印证了「kernel 是性能主战场」。

**预期结果**：你能用一句话向别人解释每个子目录的职责，并能指出「注意力采样相关的加速代码在 `kernels/`，缓存管理的数据结构在 `batch_manager/`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `batch_manager` 的静态库要和主共享库互相依赖（cyclic dependency）？

> **参考答案**：`batch_manager`（如 KVCacheManager、LlmRequest）会调用主共享库里的 runtime/kernel 能力（如 buffer 管理、采样），而主共享库又需要包含 `batch_manager` 的实现来完整暴露功能。CMake 用「先建静态库 → `WHOLE_ARCHIVE` 进共享库 → 静态库 `INTERFACE` 反向依赖共享库」打破循环。

**练习 2**：`executor/` 目录里定义的是「执行过程」还是「执行配置/类型」？

> **参考答案**：主要是**配置与类型**（`ExecutorConfig`、`Request`、`Response`、各类 stats）。注意：PyTorch 后端的真正执行循环（`PyExecutor` 的单步循环）其实跑在 Python 里（见 u3-l2），C++ 侧并不直接暴露一个可调用的「Executor」类——这点会在 4.3 节再次出现。

---

### 4.2 nanobind 绑定

#### 4.2.1 概念说明

`cpp/tensorrt_llm/nanobind/` 是 Python 与 C++ 之间的「翻译官」。它**只做一件事**：把 C++ 类、枚举、函数登记成 Python 对象。翻译官本身不实现业务逻辑，业务逻辑都在 `kernels/`、`runtime/`、`batch_manager/` 里。

nanobind 的写法很直观：在一个 `NB_MODULE(模块名, m)` 宏里，用 `nb::class_<CppFoo>(m, "PyFoo")...` 把 C++ 类 `CppFoo` 注册成 Python 名字 `PyFoo`；`m.def_submodule("xxx")` 创建子模块；`nb::enum_<...>` 注册枚举。整个 `bindings.cpp` 就是一张「登记表」。

#### 4.2.2 核心流程

绑定模块的编译与加载分两步：

```text
[编译期] 各 .cpp 绑定源文件 ──nanobind_add_module──>  bindings.cpython-3xx-<arch>.so
                                                                │
[运行期] Python: from .bindings import MpiComm  ────────────────┘
         _init(): ctypes.CDLL("libs/libtensorrt_llm.so", RTLD_GLOBAL)
```

1. **编译期**：`nanobind/CMakeLists.txt` 用 `nanobind_add_module(bindings, ${SRCS})` 把所有绑定 `.cpp`（来自 `batch_manager/`、`executor/`、`runtime/`、`process_group/`、`thop/`、`suffixAutomaton/`、`userbuffers/`、`testing/` 等）编译进**同一个**扩展模块，名为 `bindings`。
2. **运行期**：Python 侧 `tensorrt_llm/_common.py` 执行 `from .bindings import MpiComm`，触发加载 `bindings.*.so`；同时 `_init()` 用 `ctypes.CDLL(..., mode=ctypes.RTLD_GLOBAL)` 把 `libtensorrt_llm.so` 的符号提升到进程全局符号表。

#### 4.2.3 源码精读

**① 模块名就是 `bindings`。** 这是理解 u2-l2 那句 `from .bindings import MpiComm` 的钥匙：

[cpp/tensorrt_llm/nanobind/CMakeLists.txt:1-37](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/CMakeLists.txt#L1-L37) —— 第 1 行 `set(TRTLLM_NB_MODULE bindings)`；第 6-28 行列出全部绑定源文件；第 36 行 `nanobind_add_module(${TRTLLM_NB_MODULE} ${SRCS})`。

[cpp/tensorrt_llm/nanobind/CMakeLists.txt:62-73](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/CMakeLists.txt#L62-L73) —— 设置 RPATH：`$ORIGIN/libs`（TRTLLM 库）、`$ORIGIN/../../..`（系统共享库）、`$ORIGIN/../nvidia/nccl/lib`（NCCL）。这让 `bindings.so` 在 wheel 安装后仍能找到 `libtensorrt_llm.so` 与 NCCL。

**② 绑定的总入口：分层子模块。** `bindings.cpp` 的 `NB_MODULE` 块先定义文档、创建一批子模块，再调用各子系统的 `initBindings(...)`：

[cpp/tensorrt_llm/nanobind/bindings.cpp:83-144](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/bindings.cpp#L83-L144) —— 关键摘录：

```cpp
NB_MODULE(TRTLLM_NB_MODULE, m)
{
    m.doc() = "TensorRT LLM Python bindings for C++ runtime";
    // ...MpiComm、CudaStream 等直接挂在顶层 m 上...

    auto mExecutor = m.def_submodule("executor", "Executor bindings");
    auto mInternal = m.def_submodule("internal", "Internal submodule of TRTLLM runtime");
    auto mInternalProcessGroup = mInternal.def_submodule("process_group", ...);
    auto mInternalRuntime      = mInternal.def_submodule("runtime", ...);
    auto mInternalBatchManager = mInternal.def_submodule("batch_manager", ...);
    auto mInternalThop         = mInternal.def_submodule("thop", ...);

    tensorrt_llm::nanobind::executor::initBindings(mExecutor);
    tensorrt_llm::nanobind::runtime::initBindingsEarly(mInternalRuntime);
    // ...其余各子系统 initBindings...
}
```

于是从 Python 看，这个模块呈现为分层命名空间：

- `tensorrt_llm.bindings`（顶层）：`MpiComm`、`CudaStream`、`ModelConfig`、`WorldConfig`、`SamplingConfig`、`GptJsonConfig`、`QuantMode`、`DataType`、`KVCacheType`、`LlmRequestState`、`MemoryCounters`、`IpcNvlsHandle`、`PeftCacheManagerConfig`。
- `tensorrt_llm.bindings.executor`：执行器相关**配置/请求/响应类型**。
- `tensorrt_llm.bindings.internal.runtime`：运行时工具。
- `tensorrt_llm.bindings.internal.batch_manager`：KV cache 与请求管理。
- 还有 `internal.process_group`、`internal.thop`、`internal.algorithms`、`internal.userbuffers`、`internal.suffix_automaton`、`internal.testing`、`exceptions`、`BuildInfo`。

> u2-l2 提到的 `tllme`（`tensorrt_llm.bindings.executor`）就是这里的 `mExecutor` 子模块。`SamplingParams` 在 Python 里把字段「翻译」成 `tllme.SamplingConfig`，正是这一绑定的直接消费者。

**③ Python 侧如何加载。** 见 [tensorrt_llm/_common.py:26](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_common.py#L26)：`from .bindings import MpiComm`。随后 `_init()` 把主共享库提升为全局符号：

[tensorrt_llm/_common.py:48-56](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_common.py#L48-L56) —— 注释解释：KV cache 传输代理（`libtensorrt_llm_nixl_wrapper.so`、`libtensorrt_llm_ucx_wrapper.so`）在运行时 `dlopen`，并不显式链接 `libtensorrt_llm.so`，需要从全局符号表解析其符号；而 Python 扩展模块默认按 `RTLD_LOCAL` 加载，所以这里手动用 `RTLD_GLOBAL` 把符号「提升」上去。

> 这个细节解释了 u2-l2 提到的「以 `RTLD_GLOBAL` 提升 `libtensorrt_llm.so` 供 NIXL/UCX 解析」。

#### 4.2.4 代码实践

**目标**：验证 `bindings` 模块的分层结构（**源码阅读型实践**，无需 GPU）。

**步骤**：

1. 打开 [cpp/tensorrt_llm/nanobind/bindings.cpp:129-145](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/bindings.cpp#L129-L145)，数一数 `def_submodule` 创建了哪些子模块。
2. 在 [cpp/tensorrt_llm/nanobind/CMakeLists.txt:6-28](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/CMakeLists.txt#L6-L28) 里，确认 `batch_manager/*.cpp`、`executor/*.cpp`、`runtime/*.cpp`、`process_group/bindings.cpp` 都被列入 `SRCS`。
3. （若有可运行环境）在 Python 里执行：
   ```python
   import tensorrt_llm
   b = tensorrt_llm.bindings
   print([x for x in dir(b) if not x.startswith('__')][:20])
   print('executor 子模块:', hasattr(b, 'executor'))
   ```
   对照 4.2.3 列出的命名空间。

**需要观察的现象**：`dir(tensorrt_llm.bindings)` 里能看到 `MpiComm`、`ModelConfig`、`executor`、`internal` 等；`tensorrt_llm.bindings.executor` 下能看到 `ExecutorConfig`、`Request`、`Response`、`SchedulerConfig`、`KvCacheConfig` 等。

**预期结果**：你能在脑中画出 `bindings` → `executor` / `internal.{runtime,batch_manager,...}` 的树状图。若环境无法 `import`，则以源码中 `initBindings` 调用为准，明确写「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TensorRT-LLM 把所有子系统的绑定**合编成一个**扩展模块 `bindings`，而不是每个子系统一个 `.so`？

> **参考答案**：单模块的好处是 (1) C++ 类型（如 `ModelConfig`、`LlmRequestState`）跨子系统共享时只注册一次、跨模块引用零成本；(2) 减少运行时 `dlopen` 次数与符号解析复杂度；(3) 与单一 `libtensorrt_llm.so` 对齐，便于 RPATH 管理。代价是模块体积大，但对推理库可接受。

**练习 2**：`bindings.cpp` 里 `m.def_submodule("internal", ...)` 的「internal」想表达什么？

> **参考答案**：`internal` 下的绑定（`runtime`、`batch_manager`、`thop` 等）是**内部实现细节**，不属于稳定的对外 API，版本间可能变动；而顶层与 `executor` 子模块更接近公共契约。命名上用 `internal` 提醒使用者不要强依赖。

---

### 4.3 共享核心组件

#### 4.3.1 概念说明

「共享核心」指 PyTorch 后端与 AutoDeploy 后端**共同复用**的 C++ 能力——主要是调度流水线（Scheduler → BatchManager → KV Cache Manager）与解码流水线（Decoder → Sampling）。这部分是本讲最重要、也最容易被误解的地方：

> **关键结论**：共享核心**用 C++ 实现以追求性能，但它的接口定义在 Python**。因此同一个 Python 接口，可以挂上「C++ 高性能实现」，也可以换成「纯 Python 自定义实现」。

这就是「Python 调度、C++ 加速」的真正含义：Python 定义了**契约**（基类/协议、方法签名），C++ 绑定类是这个契约的一个高性能实现。当你需要自定义行为时，不必改 C++，写一个满足同样接口的 Python 类即可。

#### 4.3.2 核心流程

以 `KVCacheManager` 为例，它的「接口在 Python、实现可选 C++」可用下图概括：

```text
        Python 接口（基类 BaseResourceManager / KVCacheManager 协议）
        prepare_resources() / update_resources() / free_resources()
                          ▲                    ▲
           （默认，高性能）│       （自定义）  │
                          │                    │
        ┌─────────────────┴──────┐   ┌─────────┴──────────┐
        │ C++ 绑定实现            │   │ 纯 Python 实现     │
        │ bindings.internal.      │   │ 你自己继承基类写   │
        │ batch_manager.          │   │ 的 KVCacheManager  │
        │ KVCacheManager          │   │                    │
        └─────────────────────────┘   └────────────────────┘
```

`ResourceManager` 是一个「资源容器」，里面装着若干继承自 `BaseResourceManager` 的资源管理器；KV cache 就是其中最关键的一种。三种生命周期接口在 PyExecutor 的单步循环里被调用：

- `prepare_resources`：每步前向**之前**，为当前 batch 准备资源（如分配 KV cache 块）。
- `update_resources`：每步**结束**时更新资源状态（如登记新生成的 KV）。
- `free_resources`：请求**完成**时释放资源。

#### 4.3.3 源码精读

**① 官方架构文档明确点出「可定制性」。** 这是理解本模块的权威依据：

[docs/source/torch/arch_overview.md:44-52](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L44-L52) —— 关于 Scheduler：

> Both CapacityScheduler and MicroBatchScheduler currently use C++ bindings.
> However, since the interfaces are implemented in Python, customization is possible.

[docs/source/torch/arch_overview.md:64-69](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L64-L69) —— 关于 KVCacheManager：

> Currently, the KVCacheManager uses C++ binding. However, customization in Python is possible,
> as its interface is implemented in Python.

这两段是本讲「接口在 Python、实现可选 C++」结论的直接出处。

**② `BaseResourceManager` 三段式生命周期。** 官方文档如此描述：

[docs/source/torch/arch_overview.md:54-64](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L54-L64) —— `ResourceManager` 是 `BaseResourceManager` 的容器，定义了 `prepare_resources` / `update_resources` / `free_resources` 三个接口；KV Cache 对应的 `BaseResourceManager` 就是 `KVCacheManager`。

**③ C++ 侧 KVCacheManager 如何被绑定进来。** 在 `bindings.cpp` 末尾，各 batch_manager 组件的 `initBindings` 被挂到 `internal.batch_manager` 子模块：

[cpp/tensorrt_llm/nanobind/bindings.cpp:512-525](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/bindings.cpp#L512-L525) —— 关键几行：

```cpp
tpb::Buffers::initBindings(mInternalBatchManager);
tensorrt_llm::nanobind::runtime::initBindings(mInternalRuntime);
// ...
tb::kv_cache_manager::KVCacheManagerConnectorBindings::initBindings(mInternalBatchManager);
tb::kv_cache_manager::KVCacheManagerBindings::initBindings(mInternalBatchManager);
tb::BasePeftCacheManagerBindings::initBindings(mInternalBatchManager);
tb::CacheTransceiverBindings::initBindings(mInternalBatchManager);
tb::kv_cache_manager_v2::KVCacheManagerV2UtilsBindings::initBindings(mInternalBatchManagerKvCacheV2Utils);
```

因此 `tensorrt_llm.bindings.internal.batch_manager` 下你能看到 `KVCacheManager`、`BaseKVCacheManager`、`LlmRequest`、`GenericLlmRequest`、`SequenceSlotManager`、`CacheTransceiver`、`PeftCacheManager`、`AttentionType`、`KVCacheEventManager` 等（具体类名见各 `initBindings` 内的 `nb::class_<>` 登记）。

**④ 解码流水线的 C++ 投影。** `internal.runtime` 暴露了解码相关组件：`IGptDecoder`（解码器接口）、`GptDecoderBatched`（批处理实现）、`DecodingInput` / `DecodingOutput`（解码输入输出缓冲）、以及 `SpeculativeDecodingMode`（投机解码模式枚举）、`MoeLoadBalancer`（MoE 负载均衡器）等。这些是 Decoder → Sampling 流水线的 C++ 加速件。

> 注意一个重要事实：**C++ 侧并不暴露一个可直接 `new` 出来的「Executor」类**——`executor/` 子模块只绑定 `ExecutorConfig`、`Request`、`Response` 等**类型**（参见 [cpp/tensorrt_llm/nanobind/executor/executorConfig.cpp:573](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/executor/executorConfig.cpp#L573) 里 `nb::class_<tle::ExecutorConfig>`）。真正的单步执行循环由 Python 的 `PyExecutor` 驱动（u3-l2 详解），它在循环内调用这些 C++ 组件。这正是「Python 调度、C++ 加速」的体现。

#### 4.3.4 代码实践

**目标**：列举 `internal.batch_manager`、`internal.runtime`、`executor` 三个绑定模块各暴露了哪些 C++ 类，并解释 Scheduler/KVCacheManager 为何「接口在 Python、实现可选 C++」（对应本讲的总实践任务）。

**步骤**：

1. 在 `cpp/tensorrt_llm/nanobind/batch_manager/*.cpp`、`runtime/*.cpp`、`executor/*.cpp` 中检索 `nb::class_<` 与 `nb::enum_<`，统计每个模块登记的 Python 类名（在支持的环境可用：`grep -hoE 'nb::(class_|enum_)<[^>]*>\([^,]+, "[^"]+"' <对应目录>/*.cpp`）。
2. 按模块归类，例如：
   - `executor`：`ExecutorConfig`、`Request`、`Response`、`SchedulerConfig`、`KvCacheConfig`、`ParallelConfig`、`DecodingConfig`、`SpeculativeDecodingConfig`、`Result`、`FinishReason`、各类 `*Stats`/`*Metrics`、`KVCacheEventManager` 等。
   - `internal.runtime`：`BufferManager`、`CudaEvent`、`CudaVirtualMemoryManager`、`IGptDecoder`、`GptDecoderBatched`、`DecodingInput`、`DecodingOutput`、`SpeculativeDecodingMode`、`MoeLoadBalancer`、`AllReduceFusionOp` 等。
   - `internal.batch_manager`：`KVCacheManager`、`BaseKVCacheManager`、`LlmRequest`、`GenericLlmRequest`、`SequenceSlotManager`、`CacheTransceiver`、`BaseCacheTransceiver`、`PeftCacheManager`、`AttentionType`、`KVCacheEventManager`、`DecoderInputBuffers`/`DecoderOutputBuffers` 等。
3. 阅读 [docs/source/torch/arch_overview.md:44-69](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L44-L69)，用自己的话写出「为什么可定制」的两条理由。

**需要观察的现象**：三个模块的类名分布很有规律——`executor` 偏「配置/协议/统计」，`internal.runtime` 偏「底层算力与解码」，`internal.batch_manager` 偏「显存与请求状态」。

**预期结果**：你能输出一张三列表格（模块 / 代表类 / 对应全链路中的角色），并能说清楚「Python 定义 `BaseResourceManager` 三段式接口 → C++ `KVCacheManager` 是其默认实现 → 自定义时写 Python 子类即可，不必动 C++」。

#### 4.3.5 小练习与答案

**练习 1**：假设你想让 KV cache 在分配时多打印一行日志，应该改 C++ 还是改 Python？为什么？

> **参考答案**：**改 Python**。因为接口定义在 Python，你可以写一个继承 `BaseResourceManager`（或 KVCacheManager 协议）的 Python 类，在 `prepare_resources` 里加日志，其余转发给实现。这正是官方文档「customization in Python is possible」的设计意图。只有在追求极致性能、且新逻辑无法用 Python 高效表达时，才需要动 C++ 并重新编译。

**练习 2**：`KVCacheManagerBindings` 和 `KVCacheManagerV2UtilsBindings` 分别挂在哪个子模块？为什么 v2 utils 单独成模块？

> **参考答案**：前者挂在 `mInternalBatchManager`（`internal.batch_manager`），后者挂在 `mInternalBatchManagerKvCacheV2Utils`（`internal.batch_manager.kv_cache_manager_v2_utils`，见 [bindings.cpp:136-137](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/cpp/tensorrt_llm/nanobind/bindings.cpp#L136-L137)）。单独成模块是因为 v2 的工具类较多且自成体系，单独命名空间便于组织与未来演进，也避免与 v1 管理器混在一起。

**练习 3**：既然 `executor/` 子模块不暴露「Executor 类」，那 Python 端的执行循环由谁驱动？

> **参考答案**：由 Python 的 `PyExecutor`（位于 `tensorrt_llm/_torch/pyexecutor/`）驱动。它在单步循环里取请求、调度、调模型前向、解码、处理响应；其中调度与 KV cache 管理会调用 C++ 绑定类（如 `internal.batch_manager.KVCacheManager`）。这条链路将在 u3-l2（PyExecutor 单步循环）拆解。

---

## 5. 综合实践

把本讲三块知识串起来，完成一份**「绑定地图」小报告**：

1. **目录层**：列出 `cpp/tensorrt_llm/` 的子系统目录与职责（4.1）。
2. **编译层**：用一段话说明「静态库 → `libtensorrt_llm.so`（WHOLE_ARCHIVE）→ `bindings` 扩展模块」的聚合关系，并指出是哪一行 CMake 决定了扩展模块叫 `bindings`（4.2）。
3. **加载层**：解释 `_common.py` 里 `from .bindings import MpiComm` 与 `RTLD_GLOBAL` 各自的作用（4.2）。
4. **能力层（核心任务）**：在 `cpp/tensorrt_llm/nanobind/{executor,runtime,batch_manager}` 下检索 `nb::class_<>`，**列举每个绑定模块大致暴露了哪些 C++ 类**，并按「配置类 / 运行时类 / 缓存与请求类」分类填入下表：

   | 绑定模块（Python 路径） | 代表 C++ 类 | 全链路中的角色 |
   |---|---|---|
   | `bindings.executor` | `ExecutorConfig`、`Request`、`Response`、`SchedulerConfig`、`KvCacheConfig`… | 请求/响应/调度的**类型契约** |
   | `bindings.internal.runtime` | `BufferManager`、`GptDecoderBatched`、`DecodingInput/Output`、`MoeLoadBalancer`… | 解码与显存**底层加速** |
   | `bindings.internal.batch_manager` | `KVCacheManager`、`LlmRequest`、`SequenceSlotManager`、`CacheTransceiver`… | KV cache 与请求**状态管理** |

5. **设计层（必答）**：用自己的话回答——**为什么 `Scheduler` / `KVCacheManager` 是「接口在 Python、实现可选 C++」？** 提示：从「Python 调度、C++ 加速」「可定制性」「`BaseResourceManager` 三段式接口」三个角度组织，并引用 [arch_overview.md:44-69](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/docs/source/torch/arch_overview.md#L44-L69) 作为依据。

> 完成后，你应该能用一张图把「Python 接口（`PyExecutor` 单步循环）→ C++ 绑定（`bindings.internal.*`）→ C++ 实现（`batch_manager`/`runtime`/`kernels`）」三层关系画清楚，这正是后续 u3（端到端流程）、u7（KV Cache）、u8（调度）等讲义的共同底座。

## 6. 本讲小结

- `cpp/tensorrt_llm/` 是 C++/CUDA 引擎室：`kernels`（加速）+ `runtime`（运行时）+ `batch_manager`（缓存与请求）+ `executor`（类型）+ `nanobind`（绑定）。
- 顶层 `CMakeLists.txt` 把各子系统编成静态库，用 `WHOLE_ARCHIVE` 聚合成 `libtensorrt_llm.so`，再由 `nanobind/` 编译出 Python 扩展模块 **`bindings`**。
- `bindings.cpp` 的 `NB_MODULE` 把 C++ 能力组织成分层命名空间：顶层公共类、`executor`（配置/请求/响应类型）、`internal.{runtime,batch_manager,thop,...}`（内部实现）。
- Python 侧通过 `from .bindings import ...` 加载扩展，并用 `RTLD_GLOBAL` 提升 `libtensorrt_llm.so` 符号，供 NIXL/UCX 等运行时 `dlopen` 的传输代理解析。
- **核心设计口诀**：「Python 调度、C++ 加速」。共享核心（Scheduler、KVCacheManager）用 C++ 实现性能，但接口定义在 Python，因此可纯 Python 自定义。
- 一个关键事实：C++ 侧**不暴露可执行的 Executor 类**，真正的单步循环在 Python 的 `PyExecutor` 里，它按需调用这些 C++ 组件。

## 7. 下一步学习建议

本讲建立了「Python ↔ C++ 绑定」的静态地图。接下来：

- **u3-l1（请求全链路）**：把这张静态地图「通电」，看请求如何从 `LLM.generate()` 一路走到生成 token，以及在哪几个环节真的调用了 C++ 绑定。
- **u3-l2（PyExecutor 单步循环）**：本讲多次提到的 `PyExecutor` 循环将在此详细拆解，你会看到 `BaseResourceManager` 三段式接口在每一步的确切调用时机。
- 若你对 KV cache 的实现细节更感兴趣，可跳读 `docs/source/torch/kv_cache_manager.md`，它在 u7（KV Cache 与显存管理）单元会被系统讲解。
- 若你想亲手编译这套 C++ 代码，回顾 u1-l2 的「从源码构建」，入口是 `scripts/build_wheel.py`。
