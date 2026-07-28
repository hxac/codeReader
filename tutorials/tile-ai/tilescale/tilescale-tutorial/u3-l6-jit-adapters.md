# JIT 适配器与运行时调用

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `tilelang.compile` / `tilelang.jit` / `tilelang.lazy_jit` / `tilelang.par_compile` 四个入口的差别，以及它们最终都汇聚到同一个产物 `JITKernel`。
- 解释 `execution_backend` 的全部取值，知道哪些是「对用户可见的后端」（`tvm_ffi`/`cython`/`nvrtc`/`torch`/`cutedsl`），并理解为什么 `dlpack` 只是 `tvm_ffi` 的历史别名、`ctypes` 只是内部适配器类。
- 看懂「适配器（Adapter）」如何把编译产物（`CompiledArtifact`/`rt_mod`）封装成一个可以像普通函数一样被调用的对象，以及它如何与 PyTorch 的张量和 CUDA stream 对齐。
- 用 `kernel.get_profiler().do_bench()` 量出一个 kernel 的延迟，并理解 `TensorSupplyType` 是如何给基准测试供给输入张量的。

本讲是 u3（编译流水线）的收尾：u3-l1~u3-l5 讲了「从 PrimFunc 到 IR、再到 CUDA/HIP 源码」的编译链路，本讲负责「最后一步」——把那堆源码变成一个 Python 里随手可调、可测速的对象。

## 2. 前置知识

- **JIT（Just-In-Time）**：程序运行时才把「中间表示」编译成可执行代码，而不是提前（AOT）编译好。TileLang 默认走 JIT：第一次用某组 shape 调用 kernel 时才真正触发 nvcc/NVRTC 编译，之后再调用直接复用。
- **DLPack**：一个跨框架的张量交换标准（一个 C 结构体，含数据指针、shape、stride、dtype）。PyTorch / TVM / CuPy 都能把自己的张量「零拷贝」地导出成 DLPack，对方再零拷贝地「吃」进来。它是 TileLang 让「TVM 的运行时」和「PyTorch 的张量」互通的桥梁。
- **CUDA stream**：GPU 上的任务队列，同一 stream 内的算子按序执行。要让 TileLang 的 kernel 跟用户已有的 PyTorch 算子正确排队，就必须让 kernel 跑在「当前 stream」上。
- **rt_mod（runtime module）**：TVM 编译产物里的一段可加载运行时模块，封装了「启动 device kernel」的 host 代码。u3-l5 讲过 `device_codegen`/`host_codegen` 会产出它。
- 建议先读过 **u3-l1**（编译总览）和 **u3-l5**（代码生成与目标后端），本讲直接承接 `lower()` 产出的 `CompiledArtifact`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/jit/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py) | JIT 入口集：`compile`/`par_compile`/`jit`/`lazy_jit`，以及把「装饰器」与「缓存」串起来的 `JITImpl`。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py) | `JITKernel`：编译产物与适配器的组合体，也是用户最终拿到的「可调用 kernel」。 |
| [tilelang/jit/adapter/base.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/base.py) | `BaseKernelAdapter`：所有适配器的抽象基类，统一了「输出下标合法化、stream/device 对齐、可调用」等行为。 |
| [tilelang/jit/adapter/tvm_ffi.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/tvm_ffi.py) | `TVMFFIKernelAdapter`：默认后端，用 TVM 运行时的 `Executable` 跑 kernel，PyTorch 张量经 DLPack 直通。 |
| [tilelang/jit/adapter/dlpack.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/dlpack.py) | `TorchDLPackKernelAdapter`：**遗留**适配器（当前未被使用），讲解「dlpack 别名」时用它说明历史。 |
| [tilelang/contrib/dlpack.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/contrib/dlpack.py) | 上游 TVM 的 DLPack 桥接工具 `convert_func`，是 PyTorch↔TVM 张量互转的底层机制。 |
| [tilelang/jit/execution_backend.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/execution_backend.py) | 后端解析器：把用户给的 `execution_backend`（含别名 `dlpack`、哨兵 `auto`）解析成具体后端并做合法性校验。 |
| [tilelang/cache/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/cache/__init__.py) 与 [tilelang/cache/kernel_cache.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/cache/kernel_cache.py) | `cached()` 与 `KernelCache`：编译缓存（内存 + 磁盘），`compile` 真正的落点。 |
| [tilelang/profiler/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/__init__.py) 与 [tilelang/profiler/bench.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/bench.py) | `Profiler` 与 `do_bench`：基准测试，负责输入张量供给与延迟测量。 |
| [tilelang/utils/tensor.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py) | `TensorSupplyType` 枚举与 `get_tensor_supply`：定义基准测试的输入张量如何生成。 |

## 4. 核心概念与源码讲解

### 4.1 编译入口与产物：compile / par_compile / jit / lazy_jit → JITKernel

#### 4.1.1 概念说明

TileLang 暴露在 `tilelang` 顶层（`from .jit import jit, lazy_jit, JITKernel, compile, par_compile`）的四个 API，本质是「同一件事的四种用法」：

- **`tilelang.compile(func, ...)`**：最底层入口。传一个已经构造好的 `PrimFunc`，返回一个 `JITKernel`。适合「我已经写好了 TIR 函数，想直接编译」。
- **`@tilelang.jit`**：装饰器。包在一个**返回 PrimFunc 的工厂函数**外层（如 quickstart 里 `def matmul(...)` 返回 `matmul_relu_kernel`），让你「按参数生成 kernel」。它返回的是一个 `JITImpl` 对象，**每次调用会按入参特化并缓存** `JITKernel`。
- **`@tilelang.lazy_jit`**：延迟版装饰器，配合 `tilelang.language.v2` 的 `LazyJITFunc`，支持**按 shape 延迟特化**（u3-l2 已介绍 `LazyJITFunc`/`T.const`）。它的调用语义是「第一次调用即编译并执行」。
- **`tilelang.par_compile(funcs, ...)`**：并行编译多个 `PrimFunc`，内部用线程池 + 进度条，常被 `JITImpl.par_compile` 用于自动调优时批量编译候选配置。

无论走哪条入口，最终都会汇聚到同一个产物：**`JITKernel`**——它持有编译产物 `CompiledArtifact` 和一个可调用的适配器 `adapter`，并对外暴露 `__call__`。

#### 4.1.2 核心流程

```
用户写法                      落点                         产物
─────────────────────────────────────────────────────────────────
compile(func)        ──┐
@tilelang.jit          ├──> cached() ──> KernelCache.cached() ──> JITKernel
@tilelang.lazy_jit     │        │            (内存命中？/磁盘命中？/新编译)
par_compile(funcs)   ──┘        │
                       resolve_execution_backend()  解析后端
                       env.get_default_*()          读环境变量
```

关键认知：

1. `compile` 本身**不做编译**，它只做参数规范化（`determine_target`、Metal 校验、`out_idx` 冲突检查），然后交给 `cached()`。
2. `cached()` 是**唯一的后端解析与环境变量处理点**：读 `TILELANG_TARGET`/`TILELANG_EXECUTION_BACKEND`/`TILELANG_VERBOSE`，调 `resolve_execution_backend()` 把 `auto`/`dlpack` 解析成具体后端，再分派给对应 `KernelCache`。
3. `KernelCache.cached()` 维护**两级缓存**：内存字典 → 磁盘缓存目录（`~/.cache/tilelang/...`）；命中则直接返回 `JITKernel`，未命中才 `JITKernel(...)` 真编译。
4. `JITKernel.__init__` 内部调 `tilelang.lower()`（u3-l1），再按 `execution_backend` 选适配器。这一步就是「编译真正发生的地方」。

#### 4.1.3 源码精读

先看四个入口的「同源」。`compile` 把活儿全交给 `cached()`：

[tilelang/jit/__init__.py:L49-L91](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L49-L91) —— `compile()` 的签名与文档。注意 `execution_backend` 的类型：`Literal["auto", "dlpack", "tvm_ffi", "cython", "nvrtc", "torch", "cutedsl"]`，其中 `auto`/`dlpack` 都会被解析掉，真正落地的是后五个。

[tilelang/jit/__init__.py:L108-L117](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L108-L117) —— `compile()` 的尾部：规范化后直接 `return cached(...)`。

`cached()` 在哪读环境变量、在哪解析后端：

[tilelang/cache/__init__.py:L44-L86](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/cache/__init__.py#L44-L86) —— 这是「后端解析的唯一真相来源」：`target`/`execution_backend`/`verbose` 为 `None` 时分别读 `env.get_default_*()`；然后用 `resolve_execution_backend()` 得到具体后端；最后按后端名分派（`_dispatch_map`）。

[tilelang/cache/__init__.py:L21-L27](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/cache/__init__.py#L21-L27) —— `_dispatch_map`：每种后端一个单例 `KernelCache`。注意这里**只有五个后端**：`tvm_ffi/cython/nvrtc/cutedsl/torch`。没有 `dlpack`（它已被别名解析成 `tvm_ffi`），也没有 `ctypes`（它不是注册后端）。

`JITImpl` 是装饰器 `jit`/`lazy_jit` 真正返回的对象，它把「调用即缓存」做出来：

[tilelang/jit/__init__.py:L393-L426](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L393-L426) —— `JITImpl.__call__`：用「入参元组 + kwargs + 调优参数」当缓存键；命中则直接返回（`jit`）或直接执行（`lazy_jit`）；未命中才 `self.compile(...)`。这就是 `@tilelang.jit` 装饰的函数「按 shape 自动特化并缓存」的实现。

[tilelang/jit/__init__.py:L550-L585](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L550-L585) —— `lazy_jit`：用 `prim_func(func, lazy_jit=True)` 包出 `LazyJITFunc`，再构 `JITImpl(lazy_jit=True)`。

最终产物 `JITKernel` 的「对外可调用」与「编译发生点」：

[tilelang/jit/kernel.py:L194-L210](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L194-L210) —— `JITKernel.__call__` 直接转发给 `self.torch_function`（也就是适配器的 `func`）。所以「调 kernel」=「调适配器」。

[tilelang/jit/kernel.py:L212-L256](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L212-L256) —— `_compile_and_create_adapter`：这里**就是 u3-l1 讲的 `tilelang.lower()` 的调用点**。注意两行关键的开关：

[tilelang/jit/kernel.py:L244-L254](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L244-L254) —— `enable_host_codegen = enable_device_compile = (execution_backend == "tvm_ffi")`。这意味着：只有 `tvm_ffi` 后端会要求 `lower()` 产出**可运行的 `rt_mod`**；其它后端（`cython`/`nvrtc`/`cutedsl`）只需要源码，自己另起一条编译路径（这正是 u3-l5 讲的「两条编译路径」）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：看清「四个入口同源」。
2. **步骤**：在仓库根目录，用 `Grep` 搜索 `def compile`、`def jit`、`def lazy_jit`、`def par_compile`，确认它们都定义在 `tilelang/jit/__init__.py` 里；再追踪 `tilelang/__init__.py:142`（`from .jit import jit, lazy_jit, JITKernel, compile, par_compile`）确认它们都挂在顶层 `tilelang` 命名空间。
3. **观察现象**：你会发现 `compile` 与 `par_compile` 是「函数」，`jit` 与 `lazy_jit` 是「装饰器工厂」，但 `JITImpl.compile` 最终都调用模块级 `compile`。
4. **预期结果**：能画出 `用户入口 → cached() → KernelCache.cached() → JITKernel.__init__ → lower() → adapter` 这条链。
5. 「待本地验证」：若你已装好 tilelang，可在 Python 里 `import tilelang; print(tilelang.compile, tilelang.jit, tilelang.lazy_jit, tilelang.par_compile)` 验证它们都来自 `tilelang.jit`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `@tilelang.jit` 装饰的函数第二次用相同参数调用时几乎不耗时？
**答**：因为 `JITImpl.__call__` 用入参元组当缓存键，命中 `self._kernel_cache` 后直接返回已编译的 `JITKernel`，不再触发 `lower()`/nvcc。

**练习 2**：`par_compile` 与在循环里反复调 `compile` 相比，优势在哪？
**答**：`par_compile` 用 `ThreadPoolExecutor` 并发提交多个 `compile`（[tilelang/jit/__init__.py:L166-L198](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L166-L198)），并带 `ignore_error` 选项把失败配置降为 `None`，适合自动调优时批量编译几十上百个候选配置。

---

### 4.2 execution_backend 适配器体系：后端的解析与取舍

#### 4.2.1 概念说明

「执行后端（execution backend）」回答的是：**编译产物用什么方式被加载、调用、把参数传进去**。它和「编译目标 target（cuda/hip/metal/c）」是正交但耦合的两个维度——target 决定「编成什么源码」，execution_backend 决定「怎么把源码跑起来」。

TileLang 真正注册的五个后端（见 4.1.3 的 `_dispatch_map`）：

| 后端 | 加载方式 | 适用 target | 说明 |
| --- | --- | --- | --- |
| `tvm_ffi` | TVM 运行时 `Executable`（rt_mod），PyTorch 张量经 DLPack 直通 | cuda / hip / c（默认） | **默认后端**。`enable_host_codegen=enable_device_compile=True`，要求 `lower()` 产出可运行的 rt_mod。 |
| `cython` | 把 host 代码包成 `.pyx` 编成 `.so`，用 `ctypes` 调 | cuda / hip / c | 需要 C++ 编译器（`get_cplus_compiler()`）。 |
| `nvrtc` | 用 `cuda.bindings` + NVRTC 在运行时把 device 源码编成 cubin 并加载 | **仅 cuda** | 需要额外装 `cuda-python`；`is_nvrtc_available` 检测。 |
| `torch` | 走 PyTorch 的 metal 扩展 | **仅 metal** | 名字叫 torch，实际是「Metal 后端的专用适配器」`MetalKernelAdapter`。 |
| `cutedsl` | NVIDIA CuTe DSL（Python 描述的 device kernel） | cutedsl | 走 `CuTeDSLKernelAdapter`。 |

两个「不在 `_dispatch_map` 里、但常被提及」的名字，必须分清：

- **`dlpack` 是历史别名，不是独立后端**。`_CANONICAL_MAP = {"dlpack": "tvm_ffi"}` 把它映射成 `tvm_ffi`。`tilelang/jit/adapter/dlpack.py` 里的 `TorchDLPackKernelAdapter` 是**遗留类，当前代码从不实例化**（全仓搜不到任何 `import`/实例化点，它依赖的 `to_pytorch_func` 甚至已不存在）。所以「dlpack 后端」≈「tvm_ffi 后端」，PyTorch 张量互通是通过 `tvm_ffi` 适配器内部的 DLPack 直通实现的，不是走那个遗留类。
- **`ctypes` 是内部适配器，不是注册后端**。`tilelang/jit/adapter/ctypes/adapter.py` 里有 `CtypesKernelAdapter`，但它没有出现在 `adapter/__init__.py` 的导出里，也没有进 `_dispatch_map`，只是 `cython`/`nvrtc` 早期实现的内部基类，用户不能 `execution_backend="ctypes"`。

#### 4.2.2 核心流程

```
用户传 execution_backend (可能为 None / "auto" / "dlpack" / 具体后端)
        │
        ▼
resolve_execution_backend(requested, target)
        │
        ├─ _canon_backend():  "dlpack" -> "tvm_ffi"
        │
        ├─ 若 req in (None, "auto"):
        │     cuda -> "tvm_ffi" ; metal -> "torch" ; 其它 -> "cython"
        │
        ├─ allowed_backends_for_target(target):
        │     cuda:["tvm_ffi","nvrtc","cython"] ; hip:["tvm_ffi","cython"]
        │     metal:["torch"] ; c:["cython","tvm_ffi"] ; cutedsl:["cutedsl"]
        │
        └─ 校验: 不在允许集合 -> 抛 ValueError 并给"Tip: use execution_backend='auto'"
```

简言之：`auto` 按 target 选默认（cuda→tvm_ffi、metal→torch、其余→cython），`dlpack` 当作 `tvm_ffi`，非法组合直接报错并提示可选值。

#### 4.2.3 源码精读

[tilelang/jit/execution_backend.py:L9-L18](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/execution_backend.py#L9-L18) —— `dlpack` → `tvm_ffi` 的别名映射。这就是「dlpack 不是独立后端」的铁证。

[tilelang/jit/execution_backend.py:L26-L59](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/execution_backend.py#L26-L59) —— `allowed_backends_for_target`：每个 target kind 的允许后端清单。注意 `include_unavailable=False` 时还会把「装了 `cuda-python` 才能用的 nvrtc」过滤掉。

[tilelang/jit/execution_backend.py:L66-L108](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/execution_backend.py#L66-L108) —— `resolve_execution_backend`：`auto` 的默认选择 + 合法性校验 + 友好报错。读这一段就能回答「我这个 target 能用哪些后端」。

后端选定后，`JITKernel._compile_and_create_adapter` 按 `if/elif` 分发到不同适配器构造函数：

[tilelang/jit/kernel.py:L258-L333](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L258-L333) —— 五个分支：`tvm_ffi` 用 `TVMFFIKernelAdapter`（要求 `rt_mod is not None`）、`cython` 用 `CythonKernelAdapter`、`nvrtc` 用 `NVRTCKernelAdapter`（惰性 import）、`torch` **强制 `is_metal_target`** 用 `MetalKernelAdapter`、`cutedsl` **强制 `is_cutedsl_target`** 用 `CuTeDSLKernelAdapter`。

[tilelang/jit/kernel.py:L303-L316](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L303-L316) —— 注意 `torch` 分支里有 `assert is_metal_target(target)`。所以在 CUDA 机器上**不能**用 `execution_backend="torch"`——这是本讲综合实践里一个容易踩的坑。

而 `JITKernel.__init__` 自身的合法后端清单：

[tilelang/jit/kernel.py:L114-L124](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L114-L124) —— `assert execution_backend in ["tvm_ffi","cython","nvrtc","torch","cutedsl"]`。再次印证「dlpack/ctypes 不在这层」。

至于遗留的 `TorchDLPackKernelAdapter`，只用来理解历史：

[tilelang/jit/adapter/dlpack.py:L10-L42](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/dlpack.py#L10-L42) —— 它调用 `to_pytorch_func(self.mod)`，但该函数已不存在，类也无人调用。当前真正的 PyTorch 互通在 4.3 节的 `TVMFFIKernelAdapter`。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：用源码回答「CUDA 上能用哪几个后端？默认是哪个？」。
2. **步骤**：读 [tilelang/jit/execution_backend.py:L36-L37](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/execution_backend.py#L36-L37)（cuda 的 `allowed`）与 [L83-L84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/execution_backend.py#L83-L84)（`auto` 下 cuda 的 `choice`）。
3. **预期结果**：CUDA 上允许 `tvm_ffi`/`nvrtc`/`cython`，`auto` 默认选 `tvm_ffi`；`torch` 仅 metal，`cutedsl` 仅 cutedsl target。
4. 「待本地验证」：若已装好环境，可用 `tilelang.compile(..., execution_backend="torch")` 在 CUDA 上编译，应看到 `AssertionError`（`is_metal_target` 断言失败）。

#### 4.2.5 小练习与答案

**练习 1**：用户写 `execution_backend="dlpack"`，最终跑的是哪个适配器？为什么？
**答**：跑 `TVMFFIKernelAdapter`。因为 `dlpack` 被 `_CANONICAL_MAP` 别名解析成 `tvm_ffi`，而 `dlpack.py` 里的 `TorchDLPackKernelAdapter` 是遗留死代码、从未被实例化。

**练习 2**：为什么 `nvrtc` 后端在 `allowed_backends_for_target(..., include_unavailable=False)` 里可能被剔除？
**答**：因为 NVRTC 需要 `cuda-python`（`cuda.bindings.driver`），`is_nvrtc_available` 检测不到时就把它从「可用」集合里去掉，让用户在报错时看到更靠谱的可选项（[tilelang/jit/execution_backend.py:L48-L57](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/execution_backend.py#L48-L57)）。

---

### 4.3 Adapter 机制与 DLPack 张量桥接

#### 4.3.1 概念说明

「适配器（Adapter）」是编译产物与「Python 可调用对象」之间的胶水层。它要解决三个问题：

1. **参数排布**：用户传进来的是「输入张量」，但底层 kernel 期望「输入 + 输出」按 `params` 列表的固定顺序排列。适配器要按 `result_idx` 在中间**插空、分配输出张量**，拼成完整参数表。
2. **张量互通**：把 PyTorch 张量转成 TVM 运行时认得的 DLPack 张量（零拷贝）。
3. **执行环境对齐**：让 kernel 跑在「当前 CUDA stream、当前 device」上，与用户的 PyTorch 上下文一致。

所有适配器都继承自 `BaseKernelAdapter`，必须实现一个抽象方法 `_convert_torch_func()`——返回真正可调用的 `func`。构造时 `_post_init()` 会自动调它，把结果存到 `self.func`。

#### 4.3.2 核心流程（以默认的 `tvm_ffi` 为例）

```
TVMFFIKernelAdapter.__init__
   ├─ self.executable = runtime.Executable(rt_mod)     # 把 rt_mod 包成可执行对象
   ├─ param_dtypes / param_shapes 预计算               # 预热，避免每次调用都 FFI
   └─ _post_init -> _convert_torch_func() 返回 func

func(*inputs):                                          # 用户每次调用 kernel 时走这里
   ├─ 校验输入个数 == len(params) - len(result_idx)
   ├─ for i in range(len(params)):
   │     if i in result_idx:  分配 torch.empty 输出张量
   │     else:                取 inputs[ins_idx]
   ├─ executable(*tensor_list)                          # TVM 运行时执行（DLPack 直通 PyTorch）
   └─ 按 result_idx 返回（单个或列表）
```

DLPack 的角色：`tvm.runtime.Executable` 接受的 `tvm.nd`/DLPack 数组，与 `torch.Tensor` 共享同一块显存指针，**不做数据拷贝**。`tilelang/contrib/dlpack.py` 的 `convert_func`（来自上游 TVM）就是这种桥接的通用工具：把任意「支持 DLPack 导出」的框架张量，经 `to_dlpack` 转成 TVM 认得的数组。

#### 4.3.3 源码精读

先看基类统一的「骨架」：

[tilelang/jit/adapter/base.py:L11-L18](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/base.py#L11-L18) —— `BaseKernelAdapter.__init__`：存 `mod`、`params`、`result_idx`（合法化后），然后 `_post_init()`。

[tilelang/jit/adapter/base.py:L42-L44](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/base.py#L42-L44) —— 抽象方法 `_convert_torch_func`：每个子类必须实现，返回真正的 `callable`。

[tilelang/jit/adapter/base.py:L86-L96](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/base.py#L86-L96) —— `__call__` 直接转发 `self.func`；`_post_init` 在构造末尾把 `self.func = self._convert_torch_func()`。这就是「构造完即可调用」的关键。

再看「stream/device 对齐」这对工具方法——这是理解「为什么 TileLang kernel 能正确排在 PyTorch 算子后面」的核心：

[tilelang/jit/adapter/base.py:L47-L84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/base.py#L47-L84) —— `get_current_stream_functor` / `get_current_device_functor` 返回的是 **thunk（延迟求值的 lambda）**，而不是当场取值。这样每次 `func(...)` 真正执行时才读「此刻」的 stream/device，尊重用户在调用前可能做的 `with torch.cuda.stream(...)` 切换。CUDA 不可用时 stream 返回 `0`、device 返回 CPU。

然后是默认后端 `TVMFFIKernelAdapter` 的核心：

[tilelang/jit/adapter/tvm_ffi.py:L160-L164](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/tvm_ffi.py#L160-L164) —— 把 `rt_mod` 包成 `runtime.Executable`，这是真正执行 kernel 的对象。

[tilelang/jit/adapter/tvm_ffi.py:L205-L259](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/tvm_ffi.py#L205-L259) —— `func` 的实现：校验输入数；遍历 `params`，遇 `result_idx` 就 `torch.empty` 分配输出（输出张量 device 取自第一个输入或当前 device）；最后 `executable(*tensor_list)` 执行；按 `result_idx` 返回。注意动态 shape 的处理（`dynamic_symbolic_map`）：输出张量的某维若依赖输入张量的 shape/stride，会在运行时从已构造的 `tensor_list` 里回查。

最后看 DLPack 桥接的「底层机制」——上游 TVM 的通用工具：

[tilelang/contrib/dlpack.py:L22-L58](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/contrib/dlpack.py#L22-L58) —— `convert_func(tvm_func, tensor_type, to_dlpack_func)`：把一个 TVM 函数包成「接受任意 DLPack 框架张量」的函数；`adapt_tensor` 对每个参数做 `runtime.from_dlpack(to_dlpack_func(arg))`，零拷贝。注意 float8 的特殊处理——PyTorch 的 `float8_e4m3fn` 等需先 `view(torch.int8)` 再转，因为 DLPack 对 8-bit 的 dtype 编码与 PyTorch 不同。

#### 4.3.4 代码实践（源码阅读 + 可选运行）

1. **目标**：看清「输出张量是谁分配的」。
2. **步骤**：读 [tilelang/jit/adapter/tvm_ffi.py:L220-L248](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/tvm_ffi.py#L220-L248)。在 quickstart 里，`C` 是用户预先 `torch.empty` 好的，还是 kernel 内部分配的？对照 [examples/quickstart.py:L65-L68](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L65-L68)（`c = torch.empty(...)` 后 `matmul_relu_kernel(a, b, c)`）。
3. **观察/预期**：quickstart 里 `C` 是**第三个参数**，不在 `out_idx` 里（`@tilelang.jit` 没传 `out_idx`），所以它是「输入位」由用户提供；若改成 `out_idx=[2]`，则 `C` 会由适配器内部 `torch.empty` 分配并作为返回值返回。「待本地验证」：可本地分别试两种写法，观察返回值与是否需要预分配 `c`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_current_stream_functor` 返回 lambda 而不是直接返回当前 stream 指针？
**答**：因为适配器在**构造时**就把 `func` 闭包好了，但用户可能在构造之后、调用之前切换 stream。返回 thunk 保证每次**调用时**才读当前 stream，与用户当下的 PyTorch 上下文一致（[tilelang/jit/adapter/base.py:L47-L66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/base.py#L47-L66)）。

**练习 2**：`TVMFFIKernelAdapter` 里 `param_dtypes`/`param_shapes` 为什么要在 `__init__` 里预先算好，而不是每次调用现算？
**答**：为了把 TVM→Python 的 FFI 转换（`param.torch_dtype()`、`tir.IntImm`→`int`）从「每次调用」提前到「构造一次」，减少热路径开销。

---

### 4.4 Profiler 与 TensorSupplyType 张量供给

#### 4.4.1 概念说明

`JITKernel.get_profiler()` 返回一个 `Profiler`，它是「给 kernel 喂输入、量延迟、做正确性校验」的一站式工具。两个核心概念：

- **`TensorSupplyType`**：枚举，决定「自动生成的输入张量长什么样」。不同的供给策略适用于不同的数值范围（比如 GEMM 用 `Normal`/`Uniform` 的浮点，整数 kernel 用 `Integer`）。
- **`do_bench`**：底层基准测试函数，做 L2 cache flush、warmup、CUDA event 计时，返回毫秒级延迟。

`Profiler` 通过 `with_default_adapter(adapter)` 持有 kernel 的适配器，于是它既能 `__call__`（调 kernel），又能 `assert_allclose`（和参考实现比）、`do_bench`（测延迟）。

#### 4.4.2 核心流程

```
JITKernel.get_profiler(tensor_supply_type)
    └─ Profiler(params, out_idx, supply_type).with_default_adapter(self.adapter)

Profiler.do_bench():
    ├─ determine_profiler(func):  tvm.runtime.Module -> "tvm"，否则 "torch"
    ├─ "torch" 路径:
    │     ins = self._get_inputs()        # 用 get_tensor_supply 生成输入
    │     bench_func = partial(adapter, *ins)
    │     return do_bench(bench_func, warmup, rep, ...)   # bench.py 的底层实现
    └─ "tvm" 路径（直接传 tvm Module 时）:
          用 mod.time_evaluator 量时

底层 do_bench(fn, warmup=25, rep=100):
    ├─ 先跑 5 次估时 estimate_ms（含 L2 flush）
    ├─ n_warmup = max(1, ⌊warmup / estimate_ms⌋)
    ├─ n_repeat = max(1, ⌊rep   / estimate_ms⌋)
    ├─ warmup，然后每个 repeat: flush L2 -> record start -> fn() -> record end
    └─ 按 return_mode(mean/median/min/max) 聚合返回
```

其中估计迭代次数用了向上取整的思想：用预算时间除以单次估计耗时。行内表达为 \(n_{\text{warmup}} = \max\!\left(1,\left\lfloor t_{\text{warmup}} / t_{\text{est}}\right\rfloor\right)\)。

#### 4.4.3 源码精读

`Profiler` 数据类与构造：

[tilelang/profiler/__init__.py:L40-L59](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/__init__.py#L40-L59) —— `Profiler` 字段：`params`、`result_idx`、`supply_type`、`adapter`；`__post_init__` 合法化 `result_idx` 并用 `get_tensor_supply` 造出 `self.supply`（一个「按 `KernelParam` 生成 torch 张量」的函数）。

[tilelang/profiler/__init__.py:L107-L112](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/__init__.py#L107-L112) —— `_get_inputs`：遍历 `params`，跳过 `result_idx`（输出位），对每个输入参数调 `self.supply(param)` 生成张量。这就是「自动喂输入」的实现。

[tilelang/profiler/__init__.py:L310-L356](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/__init__.py#L310-L356) —— `do_bench`：先 `determine_profiler` 判后端，再按 `torch`/`tvm` 分流；`torch` 路径把 `partial(adapter, *ins)` 交给底层 `bench.do_bench`。

底层计时的关键：

[tilelang/profiler/bench.py:L100-L135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/bench.py#L100-L135) —— 先建一个 256MB 的 L2 flush 缓冲；用 5 次「flush + fn」估时；据此算 `n_warmup`/`n_repeat`；按 `backend`（`event` 或 `cupti`）进入计时。

[tilelang/profiler/bench.py:L138-L170](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/bench.py#L138-L170) —— `_bench_with_cuda_events`：每个 repeat 先 `cache.zero_()`（清 L2，保证每次访存都真去显存拿）再 event 计时；最后按 `return_mode` 用 `torch.mean/median/min/max` 聚合。

`TensorSupplyType` 的取值与含义：

[tilelang/utils/tensor.py:L28-L35](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L28-L35) —— 七种供给：`Integer`/`Uniform`/`Normal`/`Randn`/`Zero`/`One`/`Auto`。

[tilelang/utils/tensor.py:L126-L176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L126-L176) —— `Auto` 会按 dtype 自动选（浮点→`Uniform(-1,1)`，无符号/float8/bool→小范围整数）；`Normal` 用 `normal_(-1,1)`；`Randn` 用标准正态；`Zero`/`One` 全 0/全 1。

#### 4.4.4 代码实践（可运行 / 待本地验证）

1. **目标**：用 `get_profiler().do_bench()` 量 quickstart 的 matmul+relu 延迟，并比较两种 `TensorSupplyType`。
2. **步骤**：在 quickstart 末尾已有 `profiler = matmul_relu_kernel.get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Normal)` 与 `latency = profiler.do_bench()`（见 [examples/quickstart.py:L83-L87](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L83-L87)）。再分别用 `TensorSupplyType.Uniform` 和 `TensorSupplyType.Auto` 各 `do_bench()` 一次。
3. **需要观察的现象**：打印三种供给下的延迟（应大致接近，因为 matmul 延迟主要由 shape/dtype 决定，对具体数值分布不敏感）；以及 `do_bench` 会先做若干次 warmup 再计时。
4. **预期结果**：得到一个毫秒级延迟数字（具体数值「待本地验证」，取决于 GPU 与 block 参数）。
5. 「待本地验证」：若无 GPU，可只读 `bench.py` 的估时逻辑，确认 `n_warmup`/`n_repeat` 是怎么由 `warmup=25ms`/`rep=100ms` 预算推出来的。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `do_bench` 在每次计时前都要 `cache.zero_()`？
**答**：用一个 256MB 缓冲清空 L2 cache，保证每次 kernel 都真去显存读数据、而不是命中上一次留下的 L2，从而得到稳定、可复现的延迟（[tilelang/profiler/bench.py:L151-L155](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/profiler/bench.py#L151-L155)）。

**练习 2**：`TensorSupplyType.Auto` 和 `Normal` 对一个 `float16` 的 GEMM 输入分别生成什么？
**答**：`Auto` 对 `float16` 会用 `uniform_(-1,1)`（[L139-L140](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L139-L140)）；`Normal` 用 `normal_(-1,1)`（[L167-L168](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L167-L168)）。

---

## 5. 综合实践

**任务**：用同一个 matmul kernel，分别以 `dlpack`（即 `tvm_ffi`）和另一个 CUDA 可用后端编译运行，对比可用性与延迟；并理解为什么本讲的「torch 后端」在 CUDA 上不可用。

**操作步骤**（基于 quickstart 改造，示例代码）：

```python
# 示例代码（基于 examples/quickstart.py 改造，非仓库原文件）
import tilelang, tilelang.language as T, torch

# quickstart 原本的 matmul 工厂函数保持不变，省略...
from examples.quickstart import matmul  # 假设可导入

M = N = K = 1024; bM = bN = 128; bK = 32
a = torch.randn(M, K, device="cuda", dtype=torch.float16)
b = torch.randn(K, N, device="cuda", dtype=torch.float16)

# 后端 1：dlpack（别名）-> 解析为 tvm_ffi，CUDA 默认后端
pf_tvmffi = matmul(M, N, K, bM, bN, bK)  # @tilelang.jit 默认 auto -> tvm_ffi
lat1 = pf_tvmffi.get_profiler().do_bench()

# 后端 2：nvrtc（CUDA 上另一个合法后端，需装 cuda-python）
import tilelang
pf = matmul(M, N, K, bM, bN, bK)  # 同样的工厂函数
# 注意：@tilelang.jit 无法在装饰器层指定后端，需用底层 compile 指定
prim = matmul(M, N, K, bM, bN, bK)        # 得到 PrimFunc
kernel_nvrtc = tilelang.compile(prim, execution_backend="nvrtc", target="cuda")
lat2 = kernel_nvrtc.get_profiler().do_bench()

print(f"tvm_ffi(dlpack): {lat1:.4f} ms,  nvrtc: {lat2:.4f} ms")
```

**需要观察/回答的问题**：

1. 两个后端都能在 CUDA 上跑通吗？正确性是否都和 `torch.relu(a@b)` 对齐？（用 `kernel.get_profiler().assert_allclose(ref_func, ...)` 校验。）
2. 试着把上面换成 `execution_backend="torch"`：应触发 `AssertionError`（`is_metal_target(target)` 失败）。这说明 `torch` 后端是 Metal 专用，CUDA 上「torch vs dlpack」的对比本身不成立——CUDA 上有意义的对比是 `tvm_ffi` vs `nvrtc` vs `cython`。
3. 试着把 `dlpack` 换成 `tvm_ffi`：行为应完全一致（别名解析）。

**预期结果**：`tvm_ffi` 与 `nvrtc` 给出量级相近、数值略不同的延迟（具体「待本地验证」）。`torch` 后端在 CUDA 上报错。

**为什么这个任务能串起本讲**：它同时用到「后端解析（4.2）」「适配器封装与可调用（4.1/4.3）」「Profiler 测速（4.4）」，并强迫你直面 `execution_backend` 的真实约束。

## 6. 本讲小结

- 四个入口 `compile`/`jit`/`lazy_jit`/`par_compile` 同源：都经 `cached()` → `KernelCache.cached()` → `JITKernel`，区别只在「谁来构造 PrimFunc、何时编译、是否并行」。
- 真正注册的执行后端只有五个：`tvm_ffi`/`cython`/`nvrtc`/`torch`/`cutedsl`；`dlpack` 是 `tvm_ffi` 的历史别名（不是独立后端），`ctypes` 是内部适配器类（不对外）。
- 默认 `auto`：CUDA→`tvm_ffi`、Metal→`torch`、其它→`cython`；`torch` 仅 Metal、`cutedsl` 仅 cutedsl target、`nvrtc` 仅 CUDA 且需 `cuda-python`。
- 只有 `tvm_ffi` 要求 `lower()` 产出可运行的 `rt_mod`（`enable_host/device_compile=True`），其它后端只要源码、自走编译路径——这就是 u3-l5 的「两条编译路径」。
- 适配器 `BaseKernelAdapter` 统一了「输出下标合法化、stream/device 对齐（thunk）、构造即可调用」；默认 `TVMFFIKernelAdapter` 用 `runtime.Executable` + DLPack 零拷贝直通 PyTorch 张量。
- `Profiler` + `TensorSupplyType` + `do_bench` 提供「自动喂输入、L2 flush、CUDA event 计时」的一站式基准测试。

## 7. 下一步学习建议

- 想搞清楚 `lower()` 在 `tvm_ffi` 路径下到底产出什么？回到 **u3-l1**（`CompiledArtifact` 五字段）和 **u3-l5**（host/device codegen）对照阅读。
- 想理解 `cython`/`nvrtc` 的「另一条编译路径」如何把源码编成 `.so`/cubin？去读 [tilelang/jit/adapter/nvrtc/libgen.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/nvrtc/libgen.py) 与 [tilelang/jit/adapter/cython/cython_wrapper.pyx](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/cython/cython_wrapper.pyx)。
- 进入 **u5（自动调优）**：`AutoTuner` 正是用 `par_compile` 批量编译候选、用 `Profiler.do_bench` 量延迟、用 `update_tuner_result` 记录最优配置。
- 若你对分布式感兴趣，`JITKernel.initialize`（[tilelang/jit/kernel.py:L465-L495](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L465-L495)）和 `TVMFFIKernelAdapter.init_table`（[tilelang/jit/adapter/tvm_ffi.py:L322-L357](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/tvm_ffi.py#L322-L357)）是 TileScale 分布式 kernel 注入 rank/远程基址表的入口，可衔接到 **u6**。
