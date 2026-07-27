# JIT 即时编译与运行总流程

## 1. 本讲目标

在上一讲（u1-l4）里，我们跑通了第一个 GEMM 算子，看到了 `func(a, b)` 一行调用就能在昇腾 NPU 上算出正确结果。但这个 `func` 究竟是什么？从你写下 `@tilelang.jit` 到 NPU 真正执行，中间发生了什么？本讲就来拆解这条完整的「即时编译 + 运行」链路。

学完本讲，你应该能够：

- 说清 `@tilelang.jit` 装饰器把一个返回 `@T.prim_func` 的 Python 函数，变成了一个**可调用、带缓存**的 kernel 对象的全过程。
- 掌握 `lower()` 里的三个阶段：`LowerAndLegalize`（降级与合法化）、`OptimizeForTarget`（面向目标的优化）、`device_codegen`（设备代码生成与 ascendc/pto 分发）。
- 理解设备代码（Ascend C / PTO IR）如何被毕昇编译器（bisheng）编成 `.so`，再被 ctypes 加载、被 Cython 包装成 Python 可调用对象。
- 区分 **JIT（即时编译）** 与 **AOT（提前编译）** 两种模式。

本讲是后续所有进阶讲义（Pass 全景、双 Codegen、运行时等）的「总线」，务必把链路在脑子里跑通一遍。

## 2. 前置知识

阅读本讲前，你需要：

- 读懂 u1-l4 的 GEMM 示例，知道 `@tilelang.jit(out_idx=[-1])`、`@T.prim_func`、`T.Kernel` 这些写法（不要求理解内部实现）。
- 大致了解 u1-l3 的模块地图：`tilelang/` 是 Python 前端与驱动，`src/` 是 C++ 后端，二者通过 TVM 的 FFI（Foreign Function Interface）沟通。
- 知道几个名词：
  - **TIR（TensorIR）**：tile-lang 用的中间表示，`@T.prim_func` 标注的函数最终就是一段 TIR。
  - **Pass（编译 pass）**：对 TIR 做一次变换的函数，比如「推断 buffer 的存储层级」「插入同步指令」。多个 pass 串起来就是一条流水线。
  - **bisheng（毕昇编译器）**：CANN 提供的、能把 Ascend C/CCE 代码编译成 NPU 可执行二进制（`.so`）的编译器，相当于 GPU 世界的 `nvcc`。
  - **ctypes / Cython**：让 Python 调用 C/C++ 动态库的两种桥接技术。
  - **ACLStream（aclrtStream）**：昇腾运行时的命令流，类比 CUDA Stream，kernel 启动指令会提交到上面异步执行。

如果这些词还很陌生，建议先翻一下 u1-l1 到 u1-l4 再回来。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 语言 | 职责 |
|------|------|------|
| [tilelang/jit/__init__.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py) | Python | 提供 `@tilelang.jit` 装饰器、`tilelang.compile()`，负责装饰、缓存与触发编译 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py) | Python | `JITKernel` 类：调用 `lower()`、选择执行后端（cython/ctypes/dlpack）、对外暴露可调用对象 |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py) | Python | `lower()`：编排两阶段 Pass 流水线 + 设备 codegen，产出 `CompiledArtifact` |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | Python | `LowerAndLegalize` 与 `OptimizeForTarget` 两个 Pass 集合的具体内容 |
| [tilelang/jit/adapter/libgen.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py) | Python | `LibraryGenerator`：调用 bisheng 把源码编成 `.so`，并 ctypes 加载 |
| [tilelang/jit/adapter/cython/adapter.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py) | Python | `CythonKernelAdapter`：昇腾默认执行后端，负责编译、加载、参数转换 |
| [tilelang/jit/adapter/cython/cython_wrapper.pyx](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx) | Cython | `CythonKernelWrapper.forward`：运行时把 torch 张量指针打包，调用 `.so` 里的 `call` 符号 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | C++ | Ascend C codegen：把 TIR 翻译成 Ascend C 源码，生成 `_kernel` 与 host 侧 `call` 符号 |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 文档 | 官方编程手册，2.3 节描述编译/运行总流程，5.2 节给出打印生成代码的方法 |

建议对照这张地图阅读后面的源码精读小节。

## 4. 核心概念与源码讲解

### 4.1 JIT 装饰器：从 `@tilelang.jit` 到可调用对象

#### 4.1.1 概念说明

**JIT（Just-In-Time，即时编译）** 指的是「在程序运行过程中、第一次真正调用时才触发编译」。对应的是 **AOT（Ahead-Of-Time，提前编译）**：在运行前就把算子编成 `.so`，运行时直接加载。

为什么 tilelang-ascend 默认用 JIT？官方编程手册总结了三个理由（见 [docs/TileLang-Ascend Programming Guide.md:186-192](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L186-L192)）：

- **动态参数驱动**：JIT 在运行时解析实际传入张量的维度、数据类型，把这些信息传给 Codegen，从而支持动态 shape。
- **硬件约束适配**：可以在运行时检测 NPU 资源，指导生成符合硬件约束的 Ascend C 代码。
- **即时优化**：结合当前硬件状态优化指令分配。

一个直观的理解：JIT 让你用 Python 写算子逻辑，但「真正编译」这件事被推迟到你第一次 `func(a, b)` 的那一刻，而且**按实际 shape 编译、按 shape 缓存**。换一组 shape，就再编一次、再缓存一次。

#### 4.1.2 核心流程

`@tilelang.jit` 的工作流程可以用下面这段伪代码概括：

```
@tilelang.jit(out_idx=[-1])
def matmul(M, N, K, block_M, block_N, K_L1, ...):
    @T.prim_func
    def main(A, B, C):  # 返回一个 TIR PrimFunc
        ...
    return main
        │
        ▼  装饰阶段：jit() 把 matmul 包成 wrapper
func = matmul(1024, 1024, 1024, 128, 256, 64)   # 此时还没编译，只是返回 wrapper
        │
        ▼  首次调用 func(a, b)
wrapper(*args):
    key = (args, kwargs)               # 用入参生成缓存键
    if key not in _kernel_cache:       # 缓存未命中
        program = matmul(*args)        # 1) 调原函数，拿到 TIR PrimFunc
        kernel  = compile(program,...) # 2) 编译（lower + codegen + bisheng）
        _kernel_cache[key] = kernel    # 3) 缓存
    return _kernel_cache[key]          # 命中则直接复用
```

关键点有两个：**装饰**只是包装，**首次调用**才真正编译；之后相同入参直接命中缓存。

#### 4.1.3 源码精读

公开入口是 `jit()` 函数。它要同时支持两种写法：无参 `@tilelang.jit` 和带参 `@tilelang.jit(out_idx=[-1])`，因此通过判断 `func` 是不是可调用对象来分流：

[tilelang/jit/__init__.py:318-352](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L318-L352) —— 两种用法都构造一个 `_JitImplementation` 实例并返回（无参时立刻应用，带参时返回装饰器本身）。

真正干活的是 `_JitImplementation.__call__`，它返回的 `wrapper` 才是被用户实际调用的 `func`。我们重点看 wrapper 内部的三步：生成缓存键、缓存未命中时编译、写回缓存。

[tilelang/jit/__init__.py:211-255](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L211-L255) 用中文逐段说明：

- 第 216-218 行：用位置参数 `args` 和排序后的关键字参数 `kwargs` 拼出一个 `key`。这就是「按入参缓存」的依据——同一个 `matmul`，传不同的 `M/N/K` 或不同的实际张量 shape，会得到不同的 key。

```python
key_args_tuple = args
key_kwargs_tuple = tuple(sorted(kwargs.items()))
key = (key_args_tuple, key_kwargs_tuple)
```

- 第 220-228 行：缓存未命中时，先调原始函数 `func(*args, **kwargs)` 拿到 TIR `PrimFunc`。注意这里区分了 `func` 是 `PrimFunc` 还是可调用对象——在我们的 GEMM 例子里它是可调用对象（工厂函数），所以会真正执行函数体、`return main`。

- 第 230-241 行：把 `PrimFunc` 交给 `compile(...)` 编译，得到一个 `JITKernel` 对象。这一步是整条链路最重的部分（4.2、4.3、4.4 会展开）。

- 第 253 行：`self._kernel_cache[key] = kernel_result`，把结果存进**实例级缓存**（字典）。下次同样入参再来，第 220 行的 `if` 直接跳过编译。

除了这个实例级缓存，`compile()` 内部还会走一层**磁盘 + 内存缓存**（`tilelang.cache.cached`），避免重启进程后重新编译。我们会在 4.1.4 实践里观察到它。

`compile()` 本身很薄，主要做两件事：把 `pass_configs` 和 `compile_flags` 规范化（影响后续 pass 和 bisheng），然后委托给 `cached()`：

[tilelang/jit/__init__.py:85-103](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L85-L103)。

磁盘缓存的核心逻辑在 [tilelang/cache/kernel_cache.py:121-230](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/cache/kernel_cache.py#L121-L230)：先用 `func.script()`（TIR 的文本）+ target + pass_configs 等算一个 SHA256 key，依次查内存缓存、磁盘缓存（`.so` + 源码 + 参数），命中就直接 `JITKernel.from_database` 重载，未命中才真正 `JITKernel(...)` 编译并落盘。也就是说：**key 综合了「算子逻辑 + 目标 + 配置」三方面**，任何一个变化都会触发重编。

> 小贴士：磁盘缓存目录默认是 `~/.tilelang/cache`（见 [tilelang/env.py:78](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py#L78)），可以用环境变量 `TILELANG_CACHE_DIR` 改。`example_gemm.py` 开头的 `tilelang.cache.clear_cache()` 就是为了每次跑都重新编译，方便观察。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「首次调用触发编译、再次调用命中缓存」。

**操作步骤**：

1. 复制 `examples/gemm/example_gemm.py` 为 `my_gemm_cache.py`，把第 7 行 `tilelang.cache.clear_cache()` 删掉（或注释掉），让它能命中磁盘缓存。
2. 在 `func = matmul(...)` 之后、`c = func(a, b)` 之前，加上两段计时：

```python
import time
t0 = time.time()
c = func(a, b)      # 首次调用：触发完整 JIT 编译
print("first call (compile):", time.time() - t0, "s")

t1 = time.time()
c = func(a, b)      # 第二次调用：相同 shape，命中实例缓存
print("second call (cached):", time.time() - t1, "s")
```

3. 运行两次这个脚本：第一次会重新编译（因为之前清过缓存或换了机器）；第二次运行时，第一次调用应该明显变快——因为命中了磁盘缓存里已有的 `.so`。

**需要观察的现象**：
- 同一次运行内，第二次调用远快于第一次（实例缓存 `_kernel_cache` 生效）。
- 脚本第二次整体运行时，第一次调用也比上一次运行快（磁盘缓存生效）。

**预期结果**：第二次调用耗时通常在毫秒级，而首次编译调用可能要数秒甚至更久（取决于 bisheng 编译速度）。如果运行环境没有真实 NPU/CANN，此实践**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `func(a, b)` 里的 `a`、`b` 换成不同 shape（比如 `M=512`），会发生什么？会不会复用之前 1024 的编译结果？

> **参考答案**：不会复用。`a`、`b` 的实际 shape 不同，会导致 wrapper 内的 `key`（由 `args` 决定）不同，进而触发一次新的编译，并作为新的条目存进缓存。这正是 JIT「按 shape 编译、按 shape 缓存」的特性。

**练习 2**：`@tilelang.jit` 的实例缓存 `_kernel_cache` 和 `tilelang.cache.cached` 的磁盘缓存，作用范围分别是什么？

> **参考答案**：`_kernel_cache` 是 `_JitImplementation` 实例上的字典，只在**同一个被装饰的函数对象、同一个进程**内有效；进程结束就没了。`tilelang.cache.cached` 则把编译产物（`.so`、源码、参数）写到 `~/.tilelang/cache`，**跨进程、跨次运行**都能命中，是更持久的缓存层。

---

### 4.2 JITKernel：把 TIR 编译成可执行适配器

#### 4.2.1 概念说明

`compile()` 最终产出的是一个 `JITKernel` 对象（见 [tilelang/jit/kernel.py:22](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L22)）。它是用户在 Python 侧直接打交道的东西：`func(a, b)` 实际调用的就是 `JITKernel.__call__`。

`JITKernel` 解决的问题是：**TIR 是中间表示，不能直接跑**；要让它变成「给几个 torch 张量就能在 NPU 上跑」的函数，需要两步——

1. **编译**：把 TIR 经过 pass 流水线和 codegen，变成 Ascend C 源码（这一步在 `lower()`，见 4.3）。
2. **落地**：把源码编成 `.so`、加载、把 torch 张量指针喂给设备函数（这一步靠「执行后端 adapter」，见 4.4）。

`JITKernel` 就是把这两步串起来的外壳。它内部持有一个 **adapter（适配器）**，不同 adapter 对应不同的参数桥接方式：`cython`（默认，昇腾用这个）、`ctypes`、`dlpack`。

#### 4.2.2 核心流程

```
JITKernel(func, out_idx, ..., execution_backend="cython")
    │
    ▼  __init__
_compile_and_create_adapter(func, out_idx, workspace_idx):
    with tvm.transform.PassContext(config=pass_configs):
        artifact = tilelang.lower(func, target, ...)   # ① 编译：TIR → Ascend C 源码
    # 从 device_mod 里读出 auto_gm_indices（workspace 消除用）
    adapter = CythonKernelAdapter(artifact.params, ...,
                                  kernel_global_source=artifact.kernel_source)  # ② 落地
    self.torch_function = adapter.func                 # ③ 对外可调用对象
    │
    ▼  __call__(a, b)
self.torch_function(a, b)  # 最终走到 cython_wrapper.forward → lib.call(...)
```

注意第 ① 步：`tilelang.lower()` 在昇腾默认路径下只负责「生成 Ascend C **源码**」，**不**负责把它编成二进制——那是 adapter 第 ② 步里 bisheng 干的事。这是理解整条链路的关键分界。

#### 4.2.3 源码精读

构造函数里真正干活的是 `_compile_and_create_adapter`。先看它怎么调用 `lower()`：

[tilelang/jit/kernel.py:227-235](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L227-L235) —— 在一个 `PassContext(opt_level=3, config=pass_configs)` 里调用 `tilelang.lower(...)`。`pass_configs`（比如 `tl.ascend_auto_sync`）就是通过这个 `PassContext` 一路传给 C++ pass 的。

```python
with tvm.transform.PassContext(opt_level=3, config=pass_configs):
    artifact = tilelang.lower(
        tilelang_func,
        target=target,
        target_host=target_host,
        platform=self.platform,
        enable_host_codegen=enable_host_codegen,
        enable_device_compile=enable_device_compile,
    )
```

> 关于 `enable_host_codegen` / `enable_device_compile`：它们对 ctypes/cython 路径都是 `False`（见 [kernel.py:225-226](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L225-L226)），含义是「host/device 的最终二进制编译不在 `lower()` 里做，而是交给 JIT 自己的 adapter（bisheng）」。所以 `lower()` 只产出源码级 `CompiledArtifact`。

拿到 `artifact` 后，JITKernel 还会从 `device_mod` 里读出一个重要属性 `auto_gm_indices`（第 242-250 行）——它记录哪些参数是「自动分配的 GM workspace」（与 workspace 消除机制有关，u5-l4 会讲），运行时要据此分配显存。

接着根据 `execution_backend` 选择 adapter：

[tilelang/jit/kernel.py:271-286](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L271-L286) —— 昇腾默认走 `cython` 分支，构造 `CythonKernelAdapter`，把 `artifact.kernel_source`（Ascend C 源码）、`host_mod`、`device_mod`、params 等都传进去。

最后，`JITKernel.__call__` 非常简单——直接转发给 adapter 生成的可调用函数：

[tilelang/jit/kernel.py:184-201](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L184-L201)。其中 `_generate_extra_args` 会把动态 shape 的实际维度补进参数（动态 shape kernel 的关键，u2 会细讲）。

还有一个对调试极其重要的方法——`get_kernel_source()`，它返回生成的 Ascend C 源码：

[tilelang/jit/kernel.py:378-389](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L378-L389)。对 ctypes/cython 后端，它委托给 adapter；这正是本讲综合实践要用的接口。

#### 4.2.4 代码实践

**实践目标**：用源码阅读方式，确认 `JITKernel` 在 `cython` 后端下「先 lower 出源码、再交给 adapter」的结构。

**操作步骤**：

1. 打开 [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py)，定位 `_compile_and_create_adapter`（203 行起）。
2. 跟踪第 228 行 `tilelang.lower(...)` 的返回值 `artifact`：它的类型是 `CompiledArtifact`（从 [tilelang/engine/param.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/param.py) 导入）。在 `lower()` 的 return 语句（[lower.py:237](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L237)）可以看到 `artifact` 由 `(None, mod, params, codegen_mod.get_source())` 构造——最后一个就是 Ascend C 源码字符串。
3. 再看第 272 行的 `CythonKernelAdapter(...)` 调用，确认 `kernel_global_source=artifact.kernel_source` 这一项，即把上一步的源码字符串喂给 adapter。

**需要观察的现象**：`lower()` 产出的 `artifact` 里**没有任何 `.so` 或二进制**，只有源码字符串和 IRModule；真正的二进制编译发生在 adapter 内部。

**预期结果**：能在源码里清晰指出「lower → artifact.kernel_source（源码）→ CythonKernelAdapter（编译成 .so）」这条数据流。

#### 4.2.5 小练习与答案

**练习 1**：tilelang-ascend 默认用 `execution_backend="cython"`。如果把它换成 `"dlpack"`，第 227 行那段 `enable_host_codegen`/`enable_device_compile` 会变成什么？为什么？

> **参考答案**：会都变成 `True`（见 [kernel.py:225-226](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L225-L226)）。因为 dlpack 后端依赖 TVM 自带的 runtime module（需要 TVM 帮忙把 device 代码编成可加载模块），而 cython/ctypes 后端走的是 tilelang 自己的 bisheng 编译链路，不需要 TVM 做这步。昇腾默认用 cython，所以这两个开关都是 `False`。

**练习 2**：`JITKernel.__call__` 为什么要调用 `_generate_extra_args` 而不是直接转发用户参数？

> **参考答案**：因为 kernel 可能用了**动态 shape**（比如 `T.Tensor((M, K), ...)` 里的 `M` 是符号）。这些符号的实际数值只有在运行时从用户传入的张量 shape 里才能读出来。`_generate_extra_args`（[kernel.py:173-182](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L173-L182)）就是把这些动态维度的实际值补到参数列表末尾，一起传给设备函数。

---

### 4.3 lower()：三阶段编译核心

#### 4.3.1 概念说明

`lower()` 是整条链路的「编译大脑」。它把高层 TIR（你用 `T.copy`、`T.gemm_v0`、`T.Parallel` 写的代码）一步步降级成贴近硬件的低层 TIR，再交给 codegen 翻译成 Ascend C。

它内部明确分为**三个阶段**（对应本讲学习目标里要求掌握的内容）：

1. **`LowerAndLegalize`（降级与合法化）**：把高层 tile 原语、内存分配、并行循环等降级成底层 IR，并保证语义合法。
2. **`OptimizeForTarget`（面向目标优化）**：做软件流水、CV 合并、存储重写、同步插入等面向具体硬件的优化。
3. **`device_codegen`（设备代码生成）**：根据 `target.model` 是 `ascendc` 还是 `pto`，分发到不同的 C++ codegen，产出 Ascend C 源码。

这三个阶段的每一个 Pass 在后续进阶讲义里都有专门讲解（u6 整个单元）。本讲只需建立「全景顺序」的印象。

#### 4.3.2 核心流程

`lower()` 函数主体非常清晰，就是三步顺序调用：

```python
# tilelang/engine/lower.py:193-237 核心三步
mod = LowerAndLegalize(mod, target)          # 阶段一
mod = OptimizeForTarget(mod, target, platform)  # 阶段二
codegen_mod = device_codegen(mod, target, platform)  # 阶段三：生成源码
return CompiledArtifact(None, mod, params, codegen_mod.get_source())
```

注意返回值：`codegen_mod.get_source()` 拿到的是**源码字符串**（Ascend C），不是二进制——再次印证 4.2 的结论。

`device_codegen` 的分发逻辑也很直接：根据 `target.model` 选 codegen：

```python
# tilelang/engine/lower.py:159-170 简化
if target.model == "ascendc" or target.model == "auto":
    device_mod = get_global_func("target.build.tilelang_ascend")(...)   # Ascend C 路线
elif target.model == "pto":
    device_mod = get_global_func("target.build.tilelang_ascend_pto")(...)  # PTO IR 路线
```

这两条 codegen 路线（ascendc 稳妥主线、pto 较新且支持 A5 仿真）是 u6-l2 的主题，这里只要知道「`lower()` 是分发的起点」即可。

#### 4.3.3 源码精读

先看 `lower()` 的全貌：

[tilelang/engine/lower.py:193-237](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L193-L237)。几个要点（用中文说明）：

- 第 213-218 行：如果传入的是单个 `PrimFunc`，就包成一个 `IRModule`（TVM 的基本编译单元）。
- 第 221-222 行：把 `platform`（如 `"A3"`/`"A5"`）注入到每个 PrimFunc 的属性里，这样 C++ pass 能读到目标平台。
- 第 224 行：`target = tvm.target.Target({"kind": "llvm", "model": target})`——注意这里的写法，kind 固定 `llvm`，真正的后端区别放在 `model` 字段里（`ascendc`/`pto`/`auto`）。这是 tilelang-ascend 复用 TVM 基础设施的一个技巧。
- 第 227、230、232 行：就是上面说的三阶段。
- 第 237 行：`return CompiledArtifact(None, mod, params, codegen_mod.get_source())`。

再看两个阶段各包含哪些 Pass。**阶段一 `LowerAndLegalize`**：

[tilelang/engine/phase.py:49-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L49-L90)。挑几个与前面讲义呼应的看（不需要现在全懂）：

- `AscendInferBufferScope`（第 52 行）：按上下文自动推断 buffer 该落在 L1/UB/L0A 等哪个存储（u3-l1 讲过）。
- `AscendVidReduction`（第 54 行）：vid 消除（u5-l3）。
- `AscendLowerParallelToVector`（第 65 行）：把 `T.Parallel` 降级成向量指令（u3-l5）。
- `LowerTileOp`（第 70 行）：把高层 tile op 降成底层操作（u6-l6）。
- `AscendWorkspaceReduction`（第 80 行）：workspace 消除（u5-l4）。
- `LegalizeVectorizedLoop` / `LegalizeSafeMemoryAccess`（第 82-84 行）：合法性保证。

**阶段二 `OptimizeForTarget`**：

[tilelang/engine/phase.py:93-121](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L93-L121)。同样挑几个：

- `CrossCorePipeline`（第 98 行）/ `CombineCV`（第 99 行）：跨核流水、CV 合并（u5）。
- `PipelinePlanning` / `InjectSoftwarePipeline`（第 100-101 行）：软件流水（u3-l6）。
- `AscendStorageRewrite`（第 110 行）：buffer 地址分配（u6-l5）。
- `AscendMemoryPlanning`（第 117 行）：缓冲复用（u6-l5）。
- `AscendSyncInsert` / `AscendSyncInsertVS`（第 118-119 行）：自动同步插入（u4-l3）。

> 阶段一偏「语义降级」，阶段二偏「硬件优化」。本讲只要记住这个分工和顺序即可，每个 pass 后面都有专门讲义。

最后是 `device_codegen` 的分发：

[tilelang/engine/lower.py:159-170](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L159-L170)。`target.build.tilelang_ascend` 和 `target.build.tilelang_ascend_pto` 是两个在 C++ 侧注册的全局函数（注册在 `src/target/rt_mod_ascend*.cc`），它们驱动 `CodeGenTileLangAscend` 等类把 TIR 翻译成 C++ 源码。

#### 4.3.4 代码实践

**实践目标**：用 `func.get_kernel_source()` 取到 GEMM 编译产物，确认 `lower()` 确实产出了 Ascend C 源码（而不是二进制）。

**操作步骤**：

1. 在 `examples/gemm/example_gemm.py` 末尾加一行（注意放在 `func = matmul(...)` 之后，但**不需要**真的跑 `func(a,b)`——因为 `get_kernel_source` 只需要编译产物；不过实例化 `func` 即 `matmul(...)` 返回的 wrapper 还没编译，需要在调用一次后才编译。所以最稳妥是放在 `c = func(a, b)` 之后）：

```python
print(func.get_kernel_source())   # 打印生成的 Ascend C 代码
```

2. 运行脚本，把输出重定向到文件方便查看：

```bash
python examples/gemm/example_gemm.py > gemm_src.txt 2>&1
```

3. 在 `gemm_src.txt` 里搜索 `matmul_kernel`（或 `main_kernel`）和 `extern "C" void call`。

**需要观察的现象**：输出是一段**可读的 C++ 代码**，里面能看到 `#include` 了 catlass/shmem 等模板库头文件，能看到形如 `<global_symbol>_kernel(...)` 的设备函数，以及一个 `extern "C" void call(...)` 的 host 函数。

**预期结果**：能确认 `lower()` 产出的是源码文本。如果环境无 NPU/CANN，可改为纯源码阅读：对照 [src/target/codegen_ascend.cc:1184-1299](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1184-L1299) 的 `AddFunction` 理解它如何生成源码，**待本地验证**运行结果。

#### 4.3.5 小练习与答案

**练习 1**：`LowerAndLegalize` 和 `OptimizeForTarget` 两个阶段，哪个更偏「语义降级」、哪个更偏「硬件优化」？请各举一个 pass 例子。

> **参考答案**：`LowerAndLegalize` 偏语义降级，例如 `LowerTileOp` 把高层 tile op 降成底层 IR；`OptimizeForTarget` 偏硬件优化，例如 `AscendSyncInsert` 自动插入同步指令、`AscendStorageRewrite` 分配 buffer 地址。前者回答「这段 TIR 在 Ascend 上合法吗、底层操作是什么」，后者回答「怎样在 Ascend 上跑得更快、更省显存」。

**练习 2**：为什么 `lower()` 返回的是源码字符串而不是 `.so` 二进制？

> **参考答案**：因为 tilelang-ascend 把「源码 → 二进制」这一步交给了 CANN 的 bisheng 编译器，放在 JIT 的 adapter 层（`LibraryGenerator.compile_lib`）去做。这样设计可以让 codegen（C++，TVM 侧）和最终编译（bisheng，CANN 侧）解耦，也方便用户用 `get_kernel_source()` 直接查看/修改生成的 Ascend C 代码。

---

### 4.4 bisheng 编译与运行时调用

#### 4.4.1 概念说明

上一步 `lower()` 只给出了 Ascend C 源码。要让它在 NPU 上真正跑起来，还差三步：

1. **bisheng 编译**：用毕昇编译器把 C++ 源码编成 `.so` 动态库。
2. **加载**：用 Python 的 `ctypes` 把 `.so` 加载进进程。
3. **调用**：把 torch 张量的设备指针打包，调用 `.so` 里导出的 `call` 符号，由它把 kernel 提交到 aclrtStream 上执行。

这三步都发生在 `CythonKernelAdapter` 里（昇腾默认后端）。本模块把它们串起来。

> 名词解释：`call` 符号是 codegen 在生成的源码里特意导出的一个 `extern "C"` 函数，它是 host 侧的「kernel 启动器」——Python 只需要调用它，它内部再去 launch 真正的设备函数 `_kernel`。这样 Python 侧就不用关心 NPU 启动的细节。

#### 4.4.2 核心流程

```
CythonKernelAdapter.__init__:
  wrapper = TLWrapper("npu")
  wrapped_source = wrapper.wrap(kernel_source)   # NPU 下直接返回原源码（已含 call）
  lib_generator.update_lib_code(wrapped_source)
  lib_generator.compile_lib()                    # ① bisheng 编出 .so
  lib = lib_generator.load_lib()                 # ② ctypes.CDLL 加载
  cython_wrapper = CythonKernelWrapper(..., lib) # ③ 包装成 Python 可调用
        │
        ▼  运行时 func(a, b)
cython_wrapper.forward([a, b], stream):
  把 a/b 的 data_ptr() 打包成 ctypes.c_void_p
  追加动态 shape 值 + aclrtStream
  self.lib.call(*call_args)                      # 调用 .so 里的 call 符号
```

#### 4.4.3 源码精读

先看 bisheng 是怎么被调用的。`LibraryGenerator.compile_lib()` 根据 target 拼出两条不同的命令：

[tilelang/jit/adapter/libgen.py:152-183](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L152-L183) —— **ascendc** 路线用 `bisheng ... -xasc`，并 `-I` 引入 catlass/shmem 模板库（这正是 u1-l2 讲的「wheel 里要打包这些头文件」的原因）：

```cpp
"bisheng", "--npu-arch=dav-2201", "-std=c++17", "-xasc",
f"-I{TL_ROOT}/3rdparty/catlass/include",
f"-I{TL_ROOT}/3rdparty/shmem/include",
... "-lruntime", "-lascendcl", ... "--shared", src.name
```

[tilelang/jit/adapter/libgen.py:184-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L184-L228) —— **pto** 路线用 `bisheng ... -xcce`，目标架构按平台选 `dav-c310`（A5）或 `dav-c220`，并 `-I` 引入 pto-isa。

命令拼好后，真正执行编译的是这两行：

[tilelang/jit/adapter/libgen.py:264-270](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L264-L270) —— `subprocess.run(command)` 调起 bisheng，返回码非 0 就抛 `Compilation Failed!`。

加载则很简单，就是 `ctypes.CDLL`：

[tilelang/jit/adapter/libgen.py:130-140](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L130-L140)。注意它还会读 `TL_RUN_MODE`：如果是 `sim`（仿真），会把仿真器库路径加到 `LD_LIBRARY_PATH`，从而用 `libruntime_camodel` 替代 `libruntime`——这就是 u7-l5 要讲的 camodel 仿真的钩子点。

把上面三步串起来的是 `CythonKernelAdapter.__init__`：

[tilelang/jit/adapter/cython/adapter.py:260-287](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L260-L287) 用中文逐行说明：

- 第 260 行：`self.wrapper = TLWrapper("npu")`。
- 第 263-266 行：把优化后的 IRModule、pass_configs、host/device mod 都交给 wrapper。
- 第 267 行：`self.wrapped_source = self.wrapper.wrap(self.get_kernel_source(kernel_only=True))`——对 NPU，`wrap` 直接返回原源码（见 [wrapper.py:648-651](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/wrapper.py#L648-L651) 的 `# TODO: support NPU` 注释处），因为 host `call` 函数已经在 C++ codegen 阶段生成好了。
- 第 269-271 行：`update_lib_code` + `compile_lib`（bisheng 编译）+ `load_lib`（ctypes 加载）。
- 第 281-286 行：构造 `CythonKernelWrapper`，并塞进各种「参数映射表」（动态 shape、dtype、设备等），运行时靠它们做参数转换。

运行时调用的核心在 Cython 侧的 `forward`：

[tilelang/jit/adapter/cython/cython_wrapper.pyx:194-197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L194-L197) —— 把动态 shape 值和 stream 追加进参数后，`self.lib.call(*call_args)` 调用 `.so` 里导出的 `call` 符号。这就是 Python → C 的最后一跳。

那么 `call` 符号是谁生成的？答案是 C++ codegen 的 `PrintHostFunc`：

[tilelang/target/codegen_ascend.cc:1131-1182](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1131-L1182)。关键几行（用中文说明）：

- 第 1138 行：`os << "extern \"C\" void call(";`——导出 host 侧 `call` 符号。
- 第 1153 行：参数列表末尾追加 `aclrtStream stream`。
- 第 1162 行：`os << name << "<<<" << core << ", nullptr, stream>>>(";`——用昇腾的 `<<<core, ..., stream>>>` 语法启动设备函数（`name` 就是 `<symbol>_kernel`），类比 CUDA 的 `<<<grid, block, stream>>>`。

而设备函数 `_kernel` 的名字由 `AddFunction` 生成：

[src/target/codegen_ascend.cc:1214](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1214) —— `auto func_name = ... + "_kernel";`，即把 TIR 的 `global_symbol`（我们的例子里是 `main`）拼上 `_kernel`，得到 `main_kernel`。

> 小结：codegen 在同一段源码里生成了两个东西——设备函数 `main_kernel`（真正在 AI Core 上跑的计算）和 host 函数 `call`（负责启动它）。Python 侧只调 `call`，`call` 再启动 `main_kernel`。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `func(a, b)` 调用，记录从 Python 到 NPU 的每一跳涉及的文件。

**操作步骤**：

1. 在 `func(a, b)` 处下「心断点」——按下面的调用链，逐层打开源码确认：
   - `JITKernel.__call__`（[kernel.py:184](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L184)）→ `self.torch_function(...)`
   - `torch_function` 是 adapter 的 `_convert_torch_func` 返回的 lambda（[adapter.py:451-457](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L451-L457)）→ `cython_wrapper.forward(...)`
   - `forward`（[cython_wrapper.pyx:75](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L75)）打包参数 → `self.lib.call(*call_args)`（第 197 行）
   - `.so` 里的 `call` 符号（由 [codegen_ascend.cc:1138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1138) 生成）→ 启动 `main_kernel`（[codegen_ascend.cc:1214](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1214)）
2. 画一张「调用栈」图，标注每一跳所在的文件和行号。

**需要观察的现象**：能清晰看到「Python 调用 → Cython → ctypes → C 函数 `call` → 设备函数 `main_kernel`」这条链，每一步都对应一个具体源码位置。

**预期结果**：得到一张包含 5 个节点、标注了文件名的调用链图。

#### 4.4.5 小练习与答案

**练习 1**：为什么 ascendc 用 `-xasc`、pto 用 `-xcce`？这俩标志大致对应什么？

> **参考答案**：`-xasc` 让 bisheng 按 Ascend C（高层 Kernel API）模式编译，`-xcce` 让它按 CCE（更底层的昇腾指令/PTO IR）模式编译。两条 codegen 路线生成的源码风格不同，因此需要告诉 bisheng 用哪种前端去解析。详见 u6-l2、u6-l4。

**练习 2**：`forward` 里第 197 行 `self.lib.call(*call_args)`，这个 `call` 是哪来的？如果 codegen 没导出它会怎样？

> **参考答案**：`call` 是 C++ codegen 的 `PrintHostFunc` 在源码里导出的 `extern "C" void call(...)`（[codegen_ascend.cc:1138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1138)），ctypes 加载 `.so` 后通过 `self.lib.call` 访问。如果没导出，`self.lib.call` 会找不到符号，运行时报 `undefined symbol: call` 之类的错误。它是 Python 与设备 kernel 之间的固定「约会地点」。

## 5. 综合实践

把本讲四个模块串起来，做一个端到端的小任务：**让 GEMM 的 Ascend C 代码「现形」并读懂它的两个关键符号**。

**任务步骤**：

1. 准备脚本：复制 `examples/gemm/example_gemm.py` 为 `my_gemm_inspect.py`，在 `c = func(a, b)` 之后加入：

```python
src = func.get_kernel_source()
print(src)

# 自动检查两个关键符号是否都在
assert "main_kernel" in src or "_kernel" in src, "未找到设备函数 _kernel"
assert 'extern "C" void call(' in src, "未找到 host 侧 call 符号"
print(">>> OK: 找到 _kernel 与 call")
```

2. 运行并把输出存盘：

```bash
python my_gemm_inspect.py > gemm_inspect.txt 2>&1
```

3. 打开 `gemm_inspect.txt`，完成下面三件事：
   - **定位设备函数**：找到 `main_kernel`（或 `<name>_kernel`），记下它的参数列表。对照你在 u1-l4 里写的 `T.copy`、`T.gemm_v0`、`T.barrier_all`，看看它们分别变成了哪些 Ascend C 调用（可能是 `DataCopy`、`MMA`、`SetFlag/WaitFlag` 之类）。
   - **定位 host 启动器**：找到 `extern "C" void call(...)`，确认它末尾有 `aclrtStream stream` 参数，并且用 `<<<core, ..., stream>>>` 语法启动了 `main_kernel`。
   - **回溯链路**：在 `call` 符号旁边写一行注释，标注「这个符号被 [cython_wrapper.pyx:197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L197) 的 `self.lib.call` 调用」。

4. （进阶）把 `pass_configs` 传进 `@tilelang.jit`，开启一个 Ascend 专属开关，对比生成代码的变化：

```python
@tilelang.jit(out_idx=[-1], pass_configs={"tl.ascend_auto_sync": True})
def matmul(...):
    ...
```

再次打印源码，搜索 `SetFlag` / `WaitFlag`，观察自动同步插入（u4-l3）的效果。

**预期结果**：你能指着 `gemm_inspect.txt` 里某一行 Ascend C 代码，说出它对应 `lower()` 的哪个阶段产出、由哪条 codegen 路线生成、最终由 4.4 的哪一跳调用。如果环境没有 NPU/CANN，第 2 步的运行**待本地验证**，可退化为纯源码阅读（对照 [codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) 推断输出形态）。

## 6. 本讲小结

- `@tilelang.jit` 是一个**装饰器**：它只在首次 `func(a, b)` 时触发编译，并按入参（实际 shape）做实例缓存；`compile()` 内部还有一层磁盘缓存。
- 完整链路是：**装饰 → wrapper（按 shape 缓存）→ `compile` → `JITKernel` → `tilelang.lower()` → adapter（bisheng 编 .so + ctypes 加载）→ `lib.call`**。
- `JITKernel` 是外壳，它先调 `lower()` 拿到 Ascend C **源码**（不是二进制），再交给 `CythonKernelAdapter` 做落地。
- `lower()` 分三阶段：`LowerAndLegalize`（语义降级/合法化）、`OptimizeForTarget`（硬件优化）、`device_codegen`（按 `ascendc`/`pto` 分发生成源码）。
- bisheng 用 `-xasc`（ascendc）或 `-xcce`（pto）把源码编成 `.so`；codegen 在源码里同时生成了设备函数 `<symbol>_kernel` 和 host 启动器 `extern "C" void call(...)`。
- `func.get_kernel_source()` 是观察这条链路最直接的窗口，也是后续所有调优、调试讲义的起点。

## 7. 下一步学习建议

本讲建立的是「总线」级别的全景认知。接下来可以按兴趣选择方向：

- **想深入 Pass 流水线**：进入 u6-l1《编译 Pass 全景与配置》，把本讲 4.3 里一带而过的每个 pass 逐个搞懂。
- **想深入两条 codegen**：进入 u6-l2《Ascend C / PTO 双 Codegen》，理解 `target.build.tilelang_ascend` 与 `target.build.tilelang_ascend_pto` 的差异。
- **想继续打牢语言基础**：进入 u2 单元，学习 `@T.prim_func`、`T.Tensor`、`T.Kernel` 的完整写法，写出自己的第一个动态 shape kernel。
- **想理解运行时与仿真**：进入 u6-l4《运行时加载与 Bisheng 设备编译》和 u7-l5《A5 仿真运行（camodel）》，搞清 `TL_RUN_MODE=sim` 时 `libruntime_camodel` 是如何替换 `libruntime` 的。

无论选哪条线，建议时常回到本讲，把新学的细节挂回这条「装饰 → lower → bisheng → call」的主链上，避免迷失在细节里。
