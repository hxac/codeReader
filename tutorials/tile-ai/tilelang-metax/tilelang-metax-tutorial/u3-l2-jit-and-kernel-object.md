# JIT 编译与 kernel 对象

## 1. 本讲目标

在 [u3-l1](u3-l1-targets-and-config.md) 里我们知道了「target 决定 kernel 编给谁」，本讲回答下一个问题：**用户写好的 kernel 函数，是怎么变成一个可以拿 torch 张量直接调用的「kernel 对象」的？**

学完本讲，你应当能够：

1. 说清 `@tilelang.jit`、`tilelang.compile`、`tilelang.par_compile` 三者的关系，以及它们各自出现在什么时候。
2. 区分 **lazy（延迟）** 与 **eager（即时）** 两种 JIT 模式的判定规则与行为差异。
3. 熟练使用 `JITKernel` 对象的常用方法：`get_kernel_source`、`get_profiler`、`export_sources`、`export_library` 等。
4. 理解「执行后端（execution backend）」的概念，能区分 `tvm_ffi` / `cython` / `nvrtc` / `torch` / `cutedsl`，并知道它们与 target 的绑定关系。
5. 理解 TileLang 的**两级编译缓存**（进程内 + 磁盘），知道缓存键如何生成、何时失效。

本讲是连接「写 kernel」与「调优 kernel」之间的桥梁，后续 [u8-l1](u8-l1-autotuner.md) 的自动调优与 [u8-l3](u8-l3-profiling-and-benchmark.md) 的基准测试都建立在本讲的 `JITKernel` / `get_profiler` 之上。

## 2. 前置知识

本讲假设你已掌握：

- **`@T.prim_func` 与 `T.Kernel`**（见 [u2-l1](u2-l1-prim-func-and-type-system.md)、[u2-l2](u2-l2-kernel-launch-context.md)）：你写的是一个返回 `PrimFunc` 的 Python 函数，描述「算什么」与「怎么启动」。
- **target 体系**（见 [u3-l1](u3-l1-targets-and-config.md)）：`target` 回答「编给谁」，是字符串/字典/`Target` 对象之一。

这里补两个本讲会用到的概念：

- **TIR（Tensor IR）**：TileLang 继承自 TVM 的中间表示，`PrimFunc` 就是一棵 TIR 函数树。编译器后续的所有 pass、codegen 都作用在 TIR 上。
- **adapter（适配器）**：TIR 被 lower + codegen 之后得到的是「源码 + 动态库」，但用户拿到的是「可以直接 `kernel(a, b)` 调用」的对象。中间这层把 torch 张量翻译成设备指针、把返回值拼回 torch 张量的胶水代码，就叫 adapter。本讲的核心对象 `JITKernel` 内部就持有一个 adapter。

一句话直觉：**`@tilelang.jit` 把你的函数包成一个可调用对象 `JITImpl`，对它调用 `.compile(...)` 或直接传张量调用，就会走 `compile → cached → JITKernel` 这条流水线，最终产出一个「拿张量当参数的可调用 kernel」。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py) | `@jit` 装饰器、`JITImpl` 包装类、模块级 `compile` / `par_compile`，以及进程内缓存 `_CallFormCache`。是本讲的「指挥中心」。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py) | `JITKernel` 类：真正的「编译 + 包装 + 暴露方法」发生地。lower、执行后端分发、`get_kernel_source` / `get_profiler` 都在这里。 |
| [tilelang/cache/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/__init__.py) | `cached()`：编译入口的统一收口，决定走哪个执行后端的缓存器。 |
| [tilelang/cache/kernel_cache.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/kernel_cache.py) | `KernelCache`：磁盘 + 内存两级缓存的单例实现，缓存键生成、原子落盘、加载都在这里。 |
| [tilelang/backend/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py) | `ExecutionBackendSpec` 与注册/解析机制：把「target + 执行后端」解析成具体的 adapter 种类。 |
| [tilelang/cuda/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/execution_backend.py) / [tilelang/maca/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py) | 各 target 注册自己支持的执行后端清单。对比这两个文件就能看出 CUDA 与 MACA 的执行后端差异。 |
| [tilelang/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py) | 把 `jit` / `JITKernel` / `compile` / `par_compile` 从 `tilelang.jit` 导出为顶层 API。 |

## 4. 核心概念与源码讲解

### 4.1 `@tilelang.jit` 装饰器与 JITImpl

#### 4.1.1 概念说明

你在 [u1-l4](u1-l4-first-gemm-kernel.md) 里见过的 `@tilelang.jit` 是一个**装饰器**。它本身不编译任何东西，只是把你的 Python 函数「包」成一个名叫 `JITImpl` 的中间对象。

`JITImpl` 做两件事：

1. **暂存编译参数**：`target`、`execution_backend`、`out_idx`、`pass_configs` 等被记在 `JITImpl` 的字段里，等真正调用时才用上。
2. **在调用时按需编译**：你对 `JITImpl` 对象调用（例如 `matmul(1024, ...)`）才会触发编译。

关键设计：`JITImpl` 支持 **lazy / eager 两种模式**，并且模式是**自动推断**的：

- **lazy（延迟）**：你的函数内层定义并显式 `return` 一个 `@T.prim_func`。调用 `matmul(M=..., ...)` 返回的是一个**可调用的 kernel 对象**，需要再传张量才执行。适合「编译一次、反复跑」。
- **eager（即时）**：你的函数直接用 DSL builder 模式（`T.const`、`T.Tensor[[...], dtype]` 注解），调用 `gemm(a, b, c)` 时**立即编译并执行**，直接返回结果张量。适合「即写即跑」的交互式场景。

#### 4.1.2 核心流程

```
@tilelang.jit                # 装饰器：把 func 包成 JITFunc，再包成 JITImpl
def matmul(...): ...

matmul.compile(M=..., ...)    # 路径 A：显式 .compile()，返回 JITKernel（lazy）
   └─ JITImpl.compile
        ├─ get_tir(...)        # 执行函数体，拿到 PrimFunc
        └─ compile(prim_func, ...)   # 模块级 compile，进缓存

matmul(a, b)                  # 路径 B：直接调用
   └─ JITImpl.__call__
        ├─ 推断 lazy/eager 模式
        ├─ parse_args 得到 cache key
        ├─ 查 _kernel_cache；miss 则 .compile() 并存入
        └─ eager: kernel(*args) 返回结果张量
           lazy: 返回 kernel 对象
```

#### 4.1.3 源码精读

**装饰器入口** `jit()` 把原始函数先变成 `JITFunc`（TileLang 自带的 prim_func 包装，见 [u2-l1](u2-l1-prim-func-and-type-system.md)），再用 `inspect` 抓取源码与签名，组装成 `JITImpl`：

[tilelang/jit/\_\_init\_\_.py:614-628](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L614-L628) — `jit` 装饰器把函数包成 `JITImpl`，初始 `mode="auto"`（尚未推断）：

```python
def decorator(func: Callable[_P, _T]):
    mode = "auto"
    pf: JITFunc[_P, _T] = prim_func(func, eager_jit=True)
    func_source = inspect.getsource(pf.orig_func)
    signature = inspect.signature(pf.orig_func)
    return JITImpl(func=pf, **compile_args, func_source=func_source,
                   signature=signature, mode=mode)
return decorator(func) if func is not None else decorator
```

注意它同时支持「裸装饰」`@tilelang.jit` 与「带参装饰」`@tilelang.jit(out_idx=[-1], target="cuda")` 两种用法。

**模式推断** 的核心是 `_infer_jit_mode`：

[tilelang/jit/\_\_init\_\_.py:370-391](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L370-L391) — 判定 lazy 还是 eager，并在 eager 模式下禁止 `out_idx`（eager 用 `T.empty()` 声明输出，见 [u2-l4](u2-l4-memory-hierarchy.md)）：

```python
def _infer_jit_mode(self, *args, **kwargs):
    if self.mode in ("lazy", "eager"):
        return self.mode
    if not isinstance(self.func, JITFunc):
        return "lazy"
    is_lazy_style = self.func._is_lazy_style(*args, **kwargs)
    return "lazy" if is_lazy_style else "eager"
```

**调用入口** `JITImpl.__call__` 是 lazy 与 eager 的分叉点，同时管理进程内缓存：

[tilelang/jit/\_\_init\_\_.py:495-540](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L495-L540) — 关键节选：

```python
def __call__(self, *args, **kwargs):
    ...
    has_tune_params = "__tune_params" in kwargs
    kwargs.update(kwargs.pop("__tune_params", {}))
    if self.mode == "auto":
        self.mode = self._infer_jit_mode(*args, **kwargs)
        self.func.set_mode(self.mode)

    # 进程内「快速通道」：仅对没有张量参数的 lazy 调用生效
    if self.is_lazy_mode() and self._can_use_call_form_cache(has_tune_params):
        kernel, call_form_key = self._call_form_cache.lookup(args, kwargs)
        if kernel is not _CALL_FORM_CACHE_MISS:
            return kernel

    key, kernel_args = self.func.parse_args(*args, **kwargs)
    kernel = self._kernel_cache.get(key, None)
    if kernel is None:
        kernel = self.compile(*args, **kwargs)   # 缓存 miss 才真正编译
        self._kernel_cache[key] = kernel
    ...
    if self.mode == "eager":
        return kernel(*kernel_args.values())     # 立即执行
    else:
        return kernel                            # 返回 kernel 对象
```

这里出现了**第一级缓存**：`self._kernel_cache`（按解析后的参数 tuple 作 key）。还有一个更激进的 `_call_form_cache`：

[tilelang/jit/\_\_init\_\_.py:46-88](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L46-L88) — `_CallFormCache` 用「上次调用形参」做快速比对，连哈希都省掉，专门为 `for` 循环里反复 `matmul(M, N, K, ...)` 这种 tight loop 优化：

```python
def lookup(self, args, kwargs):
    # 最快路径：避免重建并哈希 call-form key
    if self._matches_last(args, kwargs):
        return self.last_kernel, None
    call_form_key = (args, tuple(kwargs.items()))
    kernel = self.entries.get(call_form_key, _CALL_FORM_CACHE_MISS)
    ...
```

> 注意它的前提 `_can_use_call_form_cache`：只在 lazy 模式、无 `__tune_params`、且函数没有张量参数时才启用——因为它直接返回 kernel 对象，不去提取运行期张量。

`JITImpl.__post_init__` 初始化这三块缓存：

[tilelang/jit/\_\_init\_\_.py:345-354](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L345-L354)：

```python
def __post_init__(self):
    ...
    self._kernel_cache: dict[tuple, Kernel] = {}
    self._call_form_cache: _CallFormCache = _CallFormCache()
    self._tuner_cache: dict[tuple, Kernel] = {}
```

#### 4.1.4 代码实践

**实践目标**：直观感受 lazy 与 eager 的差异。

**操作步骤**：

1. 打开 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)，注意它是 lazy 风格——函数体里 `with T.Kernel(...)` 后 `return C`，并在最外层函数没有张量参数（`M, N, K = T.const("M, N, K")`）。
2. 阅读它的 `main()`，看到 `matmul.compile(M=..., ...)` 返回的 `kernel` 是一个对象，随后 `c = kernel(a, b)` 才执行。

**需要观察的现象**：`matmul.compile(...)` 调用后，控制台会打印（受 `TILELANG_PRINT_ON_COMPILATION` 控制，默认开）形如 `TileLang begins to compile kernel ...` / `TileLang completes to compile kernel ...` 的日志，说明编译发生在 `.compile()` 这一步。

**预期结果**：`kernel` 是可调用对象，类型为 `JITKernel`；再次对同一 `matmul` 调用 `.compile(...)`（同样参数）不会重复打印「begins to compile」，因为命中了 `JITImpl._kernel_cache`。

> 待本地验证：若无 GPU，`.compile()` 在 `target="auto"` 探测不到设备时会报错；可用 `target="cuda"` 配合 NVRTC，或参见 [u3-l3](u3-l3-running-on-metax-maca.md) 的纯源码生成方式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_CallFormCache` 只对「没有张量参数的 lazy 调用」生效？

**参考答案**：因为它直接返回 kernel 对象、跳过 `func.parse_args` 提取张量的步骤；只有当调用参数里不含运行期张量（即参数都是用于烘焙形状的标量，如 `M, N, K, block_M`）时，kernel 对象才能脱离张量独立缓存与复用。

**练习 2**：在 [example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py) 的 `main()` 中，把 `matmul.compile(M=1024, ...)` 调用两次（同样参数），第二次会重新编译吗？

**参考答案**：不会。`JITImpl.__call__` / `.compile` 走 `self._kernel_cache.get(key)`，第二次命中直接返回缓存对象，不再进入 `tilelang.compile`。

---

### 4.2 `compile` 入口与编译缓存

#### 4.2.1 概念说明

`JITImpl.compile` 内部调用的模块级 `tilelang.compile(func, ...)` 才是「真正的编译入口」。它做三件事：

1. 合并来自 `PrimFunc` 属性的 `out_idx` / `pass_configs` / `compile_flags`（函数体内可用装饰器风格的标注覆盖）。
2. 把一切交给 `cached()`——TileLang 把「环境变量处理、target 归一化、执行后端解析」**只集中在 `cached()` 这一处**，这是源码注释里反复强调的设计纪律。
3. `cached()` 进一步委托给 `KernelCache` 单例，完成**第二级缓存**：磁盘 + 内存。

> 为什么需要两级缓存？
> - `JITImpl._kernel_cache`（进程内）只在一次 Python 进程里有效，进程退出即丢。
> - `KernelCache`（磁盘）跨进程、跨次运行复用，避免每次 `import` 都重新编译——GPU kernel 编译动辄数秒，这一层对迭代效率至关重要。

#### 4.2.2 核心流程

```
tilelang.compile(func, out_idx, execution_backend, target, ...)
   ├─ 合并 func.attrs 里的 tilelang_out_idx / tilelang_pass_configs / tilelang_compile_flags
   └─ cached(...)                                          # cache/__init__.py
        ├─ _resolve_cache_dispatch(target, execution_backend)
        │     ├─ determine_target(target) → norm_target
        │     └─ resolve_execution_backend(...) → 选定 backend 名
        └─ cache.cached(func, ..., target=norm_target, execution_backend=...)
             # KernelCache.cached (kernel_cache.py)
             ├─ 缓存关闭？ → 直接 new JITKernel
             ├─ _generate_key(...) → SHA256 key（含 TIR 脚本、target、版本…）
             ├─ 查 _memory_cache → 命中返回
             ├─ _load_kernel_from_disk(key) → 命中返回（并回填内存）
             └─ miss → new JITKernel(...) → _save_kernel_to_disk（原子落盘）
```

#### 4.2.3 源码精读

**模块级 `compile`**：合并属性后直接调 `cached`：

[tilelang/jit/\_\_init\_\_.py:137-170](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L137-L170) — 关键节选：

```python
assert isinstance(func, PrimFunc), ...
func_attrs = func.attrs
if func_attrs and "tilelang_out_idx" in func_attrs:
    func_out_idx = list(func_attrs["tilelang_out_idx"])
    if out_idx is not None:
        raise ValueError("Out index conflict ...")
    out_idx = func_out_idx
# 同样合并 tilelang_pass_configs / tilelang_compile_flags ...
return cached(func=func, out_idx=out_idx, execution_backend=execution_backend,
              target=target, target_host=target_host, verbose=verbose,
              pass_configs=pass_configs, compile_flags=compile_flags)
```

**`cached()` 收口**：先解析出执行后端，再分发到对应 `KernelCache` 实例：

[tilelang/cache/\_\_init\_\_.py:32-92](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/__init__.py#L32-L92)：

```python
_dispatch_map: dict[str, KernelCache] = {
    "tvm_ffi": TVMFFIKernelCache(), "cython": CythonKernelCache(),
    "nvrtc": NVRTCKernelCache(), "cutedsl": CuTeDSLKernelCache(),
    "torch": TorchKernelCache(),
}

def _resolve_cache_dispatch(target, execution_backend, verbose):
    if target is None: target = env.get_default_target()
    if execution_backend is None: execution_backend = env.get_default_execution_backend()
    ...
    norm_target = _determine_target(target, return_object=True)
    resolved_backend = resolve_execution_backend(requested_backend, norm_target)
    ...
    return _dispatch_map[resolved_backend], norm_target, resolved_backend, verbose
```

**缓存键生成**——这是理解「何时缓存失效」的钥匙：

[tilelang/cache/kernel_cache.py:241-282](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/kernel_cache.py#L241-L282)：

```python
func_binary = func.script(show_meta=True).encode()
key_data = {
    "func": sha256(func_binary).hexdigest(),   # TIR 脚本的哈希
    "out_idx": ...,
    "target": str(target),
    "target_host": str(target_host) if target_host else None,
    "execution_backend": execution_backend,
    "pass_configs": pass_configs,
    "compile_flags": compile_flags,
    **self._get_base_key(),                    # 含 tilelang 版本号、可选的 lib 印章
}
key_string = json.dumps(key_data, sort_keys=True)
return sha256(key_string.encode()).hexdigest()
```

含义：**只要 TIR 脚本、target、执行后端、pass 配置、tilelang 版本中任一变化，缓存键就变，视为新 kernel。** 可选的 `_get_tilelang_lib_stamp()`（开关 `TILELANG_KERNEL_CACHE_USE_LIB_STAMP`）会进一步把 `libtilelang.so` 的内容哈希纳入键——开发期改 C++ pass 后强制缓存失效，避免「TIR 没变但生成的 kernel 变了」的陈旧命中。

**两级缓存命中逻辑**：

[tilelang/cache/kernel_cache.py:326-418](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/kernel_cache.py#L326-L418) — 节选三段：

```python
if not env.is_cache_enabled():                 # ① 缓存被禁用 → 直接编译
    return JITKernel(func, ...)

key = self._generate_key(...)
with self._lock:
    if key in self._memory_cache:               # ② 内存命中
        ...
        return self._memory_cache[key]

kernel = self._load_kernel_from_disk(key, ...) # ③ 磁盘命中
if kernel is not None:
    ...
    self._memory_cache[key] = kernel            #   回填内存
    return kernel

# ④ 都 miss：编译 + 原子落盘
with jit_phase("cache.compile", ...):
    kernel = JITKernel(func, ...)
self._save_kernel_to_disk(key, kernel, func, verbose)
self._memory_cache[key] = kernel
return kernel
```

**原子落盘**：`_save_kernel_to_disk` 先写进 staging 临时目录，校验所有必需文件齐全后再 `os.rename` 原子替换，保证其他进程永远不会读到「写了一半」的缓存条目：

[tilelang/cache/kernel_cache.py:494-547](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/kernel_cache.py#L494-L547) — 节选：

```python
staging_path = os.path.join(self._get_staging_root(), f"{key}_{os.getpid()}_{...}")
os.makedirs(staging_path)
# 写 device_kernel.cu / host_kernel.cu / kernel_lib.so / params.pkl ...
missing_files = self._get_missing_complete_cache_files(staging_path)
if missing_files:
    raise RuntimeError("Incomplete cache staging directory ...")
self._remove_incomplete_cache_dir(cache_path)
try:
    os.rename(staging_path, cache_path)        # 原子可见
except OSError as exc:
    if not self._is_rename_collision(exc): raise
    shutil.rmtree(staging_path, ignore_errors=True)   # 别的进程赢了竞争
```

**并行编译** `par_compile`：用 `ThreadPoolExecutor` 把多个 `PrimFunc` 并行交给 `compile`，是 autotuner「分组编译」的底层支撑：

[tilelang/jit/\_\_init\_\_.py:221-253](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L221-L253)：

```python
with concurrent.futures.ThreadPoolExecutor(num_workers, "tl-par-comp") as executor:
    for i, func in enumerate(funcs):
        future = executor.submit(compile, func=func, ...)
        future_map[future] = i
    ...
    for future in tqdm(concurrent.futures.as_completed(futures), ...):
        results[idx] = future.result()         # ignore_error=True 时吞掉单条异常
```

#### 4.2.4 代码实践

**实践目标**：观察磁盘缓存的生成与命中。

**操作步骤**：

1. 设 `export TILELANG_VERBOSE=1`，运行 [example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py) 的 `main()` 一次。日志会打印 `Generated cache key: <sha256> for kernel ...`，记下这个 key。
2. 到默认缓存目录 `~/.tilelang/cache/` 下，按 `<version>/<platform>-<machine>/kernels/<key>/` 找到该目录，确认里面包含 `device_kernel.cu`、`host_kernel.cu`、`kernel_lib.so`、`params.pkl`。
3. **再次运行同一脚本**（不重启、或重启进程均可）。

**需要观察的现象**：第二次运行时，verbose 日志会显示命中磁盘缓存（`Found kernel in disk cache ...`），且不再触发编译日志。

**预期结果**：缓存命中后从磁盘加载 `kernel_lib.so` 与 `params.pkl`，用 `JITKernel.from_database` 重建对象，省去整条 lower + codegen 流水线。

> 待本地验证：若改一个参数（例如 `block_K=64`），TIR 脚本变化，缓存 key 随之改变，会重新编译。

#### 4.2.5 小练习与答案

**练习 1**：你在开发期修改了 `src/` 下的某个 C++ pass，但 TIR 脚本没变。默认情况下会重新编译吗？怎样强制它重编？

**参考答案**：默认不会——缓存键不含 C++ 库内容，会陈旧命中。设置环境变量 `TILELANG_KERNEL_CACHE_USE_LIB_STAMP=1`（见 [kernel_cache.py:82-134](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cache/kernel_cache.py#L82-L134)）即可把 `libtilelang.so` 的 SHA-256 纳入键，库一变即失效。

**练习 2**：`compile()` 里那段 `if out_idx is not None: raise ValueError("Out index conflict ...")` 是在防什么？

**参考答案**：防止用户既在 `@T.prim_func` 内部用 `T.empty` 声明了输出张量（`func_attrs["tilelang_out_idx"]` 已记录），又在外部 `compile(out_idx=...)` 显式传了输出索引——两者会冲突，必须二选一。

---

### 4.3 JITKernel 对象：编译与常用方法

#### 4.3.1 概念说明

`JITKernel` 是用户最终拿在手里的「kernel 对象」。它持有：

- `prim_func`：原始 TIR（便于追溯）。
- `artifact`：`tilelang.lower` 的产物，含 host/device 源码、运行时模块 `rt_mod`、参数 `params`。
- `adapter`：把上述产物包装成「拿 torch 张量调用」的可调用 `torch_function`。

`JITKernel` 自身是可调用的：`kernel(a, b)` 等价于 `kernel.torch_function(a, b)`。它还提供一大套**自省与导出方法**，是调试与基准测试的主入口。

#### 4.3.2 核心流程

`JITKernel.__init__` 的主干是 `_compile_and_create_adapter`：

```
JITKernel.__init__(func, target, execution_backend, ...)
   ├─ determine_target(target) → self.target
   ├─ resolve_execution_backend_spec(execution_backend, target)
   │     → self.execution_backend_spec (含 enable_host_codegen / enable_device_compile)
   └─ _compile_and_create_adapter(func, out_idx)
        ├─ with PassContext(opt_level=3, config=pass_configs), self.target:
        │     artifact = tilelang.lower(func, ..., enable_host_codegen, enable_device_compile)
        ├─ 按 execution_backend 选 adapter 类：
        │     tvm_ffi → TVMFFIKernelAdapter
        │     cython → CythonKernelAdapter
        │     nvrtc  → NVRTCKernelAdapter
        │     torch  → MetalKernelAdapter   (assert is_metal_target)
        │     cutedsl→ CuTeDSLKernelAdapter (assert is_cutedsl_target)
        └─ self.adapter = adapter; self.torch_function = adapter.func
```

#### 4.3.3 源码精读

**构造与编译**：

[tilelang/jit/kernel.py:100-141](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L100-L141) — 关键节选：

```python
self.pass_configs = normalize_pass_configs(pass_configs)
self.target = determine_target(target, return_object=True)
self.execution_backend_spec = resolve_execution_backend_spec(execution_backend, self.target)
self.execution_backend = self.execution_backend_spec.name
...
if env.is_print_on_compilation_enabled():
    func_name = func.attrs.get("global_symbol")
    logger.info(f"TileLang begins to compile kernel `{func_name}` with `{out_idx=}`")
adapter = self._compile_and_create_adapter(func, out_idx)
...
self.adapter = adapter
self.torch_function = adapter.func
```

**lower + adapter 分发**：

[tilelang/jit/kernel.py:253-264](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L253-L264) — 在 `PassContext` 与 `self.target` 上下文里调用 `tilelang.lower`（lower 流程见 [u4-l1](u4-l1-lowering-pipeline.md)）：

```python
with (jit_phase("lower", verbose=verbose, **phase_context),
      tvm.transform.PassContext(opt_level=3, config=pass_configs, instruments=pass_instruments),
      self.target):
    artifact = tilelang.lower(
        tilelang_func, target=target, target_host=target_host,
        enable_host_codegen=enable_host_codegen,
        enable_device_compile=enable_device_compile,
    )
```

[tilelang/jit/kernel.py:273-353](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L273-L353) — 按 `execution_backend` 选 adapter 类，并构造（节选首尾）：

```python
if execution_backend == "tvm_ffi":
    assert artifact.rt_mod is not None, "tvm_ffi backend requires a runtime module."
    adapter = create_adapter(TVMFFIKernelAdapter, params=..., rt_mod=artifact.rt_mod, ...)
elif execution_backend == "cython":
    adapter = create_adapter(CythonKernelAdapter, ...)
elif execution_backend == "nvrtc":
    from tilelang.jit.adapter import NVRTCKernelAdapter
    adapter = create_adapter(NVRTCKernelAdapter, ...)
elif execution_backend == "torch":
    assert is_metal_target(target)
    adapter = create_adapter(MetalKernelAdapter, ...)
elif execution_backend == "cutedsl":
    assert is_cutedsl_target(target)
    adapter = create_adapter(CuTeDSLKernelAdapter, ...)
else:
    raise ValueError(f"Invalid execution backend: {execution_backend}")
```

**调用入口**——非常薄，转发给 adapter 的函数：

[tilelang/jit/kernel.py:186-202](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L186-L202)：

```python
def __call__(self, *args, **kwds):
    return self.torch_function(*args, **kwds)
```

**常用方法一栏**（这些都直接对应源码，建议对照阅读）：

| 方法 | 作用 | 源码位置 |
| --- | --- | --- |
| `get_kernel_source(kernel_only=True)` | 返回生成的设备端源码（如 `.cu`） | [kernel.py:466-477](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L466-L477) |
| `get_host_source()` | 返回 host 端源码 | [kernel.py:479-486](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L479-L486) |
| `show_source(which=...)` | 把 kernel/host/both 源码打印到 stdout | [kernel.py:491-524](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L491-L524) |
| `export_sources(kernel_path=, host_path=)` | 把源码写到文件 | [kernel.py:526-562](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L526-L562) |
| `get_profiler(tensor_supply_type=)` | 返回 `Profiler`，用于 `do_bench` 测延迟 | [kernel.py:450-464](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L450-L464) |
| `run_once(func=None)` | 跑一次（可用于 sanity check） | [kernel.py:488-489](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L488-L489) |
| `export_library(kernel_file)` | 导出运行时 `.so`（仅 `tvm_ffi`） | [kernel.py:693-719](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L693-L719) |
| `show_ptx()` / `export_ptx(path)` | 编译并查看/导出 PTX（仅 CUDA） | [kernel.py:745-783](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L745-L783) |
| `show_sass()` / `export_sass(path)` | 反汇编 SASS（仅 CUDA） | [kernel.py:807-845](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L807-L845) |
| `update_tuner_result(...)` / `get_tuner_result()` | autotuner 记录/读取延迟与配置 | [kernel.py:592-634](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L592-L634) |

`get_profiler` 的实现——把自身 adapter 接到 `Profiler` 上：

[tilelang/jit/kernel.py:450-464](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L450-L464)：

```python
def get_profiler(self, tensor_supply_type: TensorSupplyType = TensorSupplyType.Auto) -> Profiler:
    return Profiler(self.params, self.out_idx, tensor_supply_type).with_default_adapter(self.adapter)
```

几个 `@property` 也很常用：`out_idx`（[kernel.py:636-638](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L636-L638)）、`params`（[kernel.py:640-642](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L640-L642)）、`kernel_source`（[kernel.py:644-651](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L644-L651)）、HIP 专属的 `n_regs`/`n_spills`/`n_max_threads`（[kernel.py:678-691](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L678-L691)）。

#### 4.3.4 代码实践

**实践目标**：用一组常用方法完整地「看清」一个编译好的 kernel。

**操作步骤**（基于 [example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)，`main()` 里已有前两步）：

1. `kernel = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)`
2. `print(kernel.get_kernel_source())` —— 打印生成的 CUDA 源码。
3. `kernel.export_sources(kernel_path="/tmp/gemm_kernel.cu", host_path="/tmp/gemm_host.cc")` —— 导出设备/host 源码到文件。
4. `profiler = kernel.get_profiler(); latency = profiler.do_bench(backend="cupti"); print(latency)` —— 测延迟。

**需要观察的现象**：步骤 2 打印出的源码里能看到 `blockIdx`、`__shared__`、`wgmma`/`mma` 之类的设备端构造（具体取决于 target 与架构），印证「你写的是规格，跑的是生成代码」。

**预期结果**：`export_sources` 后 `/tmp/gemm_kernel.cu` 非空；`do_bench` 返回一个毫秒级浮点数。

> 待本地验证：`backend="cupti"` 需要 CUDA + cupti；无设备时可只做到步骤 3，源码仍可生成（见 [u3-l3](u3-l3-running-on-metax-maca.md) 的纯源码路径）。

#### 4.3.5 小练习与答案

**练习 1**：`get_kernel_source` 对 `tvm_ffi/cython/nvrtc/cutedsl` 走 adapter，其余（如 `torch`）走 `self.artifact.kernel_source`。为什么 `torch`（Metal）要单独处理？

**参考答案**：见 [kernel.py:466-477](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L466-L477)。Metal 的 `MetalKernelAdapter` 用 `kernel_global_source` 而非 `get_kernel_source`，构造参数也不同（见 [kernel.py:321-335](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L321-L335)），所以源码取自 `artifact.kernel_source`（对应 `kernel_source` property 的回退逻辑 [kernel.py:644-651](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L644-L651)）。

**练习 2**：`export_library` 抛 `AttributeError` 时提示「请用 `execution_backend="tvm_ffi"`」。结合源码说明原因。

**参考答案**：见 [kernel.py:709-712](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L709-L712)。导出依赖 `self.artifact.rt_mod.export_library`，而 `rt_mod`（TVM 运行时模块）只在 `tvm_ffi` 后端被要求非空（`assert artifact.rt_mod is not None`，[kernel.py:276](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L276)）；其他后端可能不产生 `rt_mod`。

---

### 4.4 执行后端（execution backend）

#### 4.4.1 概念说明

「执行后端」回答一个独立于 target 的问题：**编译产物用什么方式加载和调用？**

- **target** 决定「源码长什么样」（CUDA / HIP / MACA / Metal）。
- **execution backend** 决定「这段源码怎么变成可调用函数」（tvm 运行时模块 / Cython 编译 / NVRTC 运行时编译 / Metal 命令队列 / CuTe DSL）。

两者是正交的维度，但**不是任意组合都合法**：每个 target 只允许一组特定的执行后端。例如 CUDA 允许 `tvm_ffi/nvrtc/cython/cutedsl`，而 MACA 允许 `tvm_ffi/mcrtc/cython/cutedsl`。

各执行后端速览：

| 名称 | 含义 | 典型 target |
| --- | --- | --- |
| `tvm_ffi` | 走 TVM 运行时模块（`rt_mod`），经 DLPack 与 PyTorch 互操作；默认首选 | cuda / hip / maca |
| `nvrtc` | 用 NVRTC 在运行时把 CUDA 源码编译成 PTX/cubin，免依赖外部 nvcc | cuda |
| `mcrtc` | MACA 的运行时编译器（对应 CUDA 的 nvrtc） | maca |
| `cython` | 把 host/device 源码编译成 Cython 扩展加载 | cuda / hip / maca |
| `torch` | Metal 专用，借助 PyTorch 的 Metal 后端 | metal |
| `cutedsl` | NVIDIA CuTe DSL 路径（target 带 `cutedsl` key） | cuda / maca |

> 别名：`dlpack` 会被规范化为 `tvm_ffi`（见 [execution_backend.py:12-25](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L12-L25)）。

#### 4.4.2 核心流程

```
用户给 execution_backend（None/"auto"/具体名）+ target
   └─ resolve_execution_backend_spec(requested, target)
        ├─ canonicalize_execution_backend：归一化别名（dlpack→tvm_ffi）
        ├─ _matching_specs(target)：按 target.kind 懒加载并筛出匹配 spec
        │     └─ 懒加载：register_lazy_execution_backends 记录的 import_path
        ├─ 若 requested 是 None/"auto"：返回首个「可用」spec（is_available()）
        └─ 否则校验 requested 在「允许」列表里、且当前「可用」
```

#### 4.4.3 源码精读

**`ExecutionBackendSpec`**——一个执行后端的能力描述：

[tilelang/backend/execution_backend.py:28-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L28-L37)：

```python
@dataclass(frozen=True, slots=True)
class ExecutionBackendSpec:
    name: str
    is_available: AvailabilityCheck = _always_available   # 依赖是否就绪
    supports_target: TargetPredicate | None = None        # 是否匹配该 target
    enable_host_codegen: bool = False                     # lower 时是否做 host codegen
    enable_device_compile: bool = False                   # 是否编译设备码
```

`enable_host_codegen` / `enable_device_compile` 直接传给 `tilelang.lower`（见 4.3.3），决定编译流水线跑哪些阶段。

**注册与解析**：

[tilelang/backend/execution_backend.py:94-116](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L94-L116) — 解析逻辑节选：

```python
def resolve_execution_backend_spec(requested, target):
    requested_name = canonicalize_execution_backend(requested)
    allowed_all_specs = _matching_specs(target, include_unavailable=True)
    allowed_available_specs = _matching_specs(target, include_unavailable=False)
    ...
    if requested_name in (None, "auto"):
        if not allowed_available_specs:
            raise ValueError(f"No available execution backend for target '{target.kind.name}'. ...")
        return allowed_available_specs[0]              # auto：取首个可用
    if requested_name not in allowed_all:
        raise ValueError(f"Invalid execution backend '{requested}' for target '{target.kind.name}'. ...")
    if requested_name not in allowed_available:
        raise ValueError(f"Execution backend '{requested}' requires extra dependencies ...")
    return next(spec for spec in allowed_available_specs if spec.name == requested_name)
```

**「auto」的语义**：`_matching_specs` 按**注册顺序**返回 spec 列表，`auto` 取列表里第一个 `is_available()` 为真的。所以「谁是默认」由注册顺序决定。

**CUDA 注册清单**——注意顺序就是 auto 的优先级：

[tilelang/cuda/execution_backend.py:34-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/execution_backend.py#L34-L58)：

```python
register_execution_backend("cuda", ExecutionBackendSpec(
    "tvm_ffi", supports_target=_is_plain_cuda_target,
    enable_host_codegen=True, enable_device_compile=True), override=True)
register_execution_backend("cuda", ExecutionBackendSpec(
    "nvrtc", is_available=_is_nvrtc_available, supports_target=_is_plain_cuda_target), override=True)
register_execution_backend("cuda", ExecutionBackendSpec(
    "cython", supports_target=_is_plain_cuda_target), override=True)
register_execution_backend("cuda", ExecutionBackendSpec(
    "cutedsl", is_available=_is_cutedsl_available, supports_target=_is_cutedsl_target), override=True)
```

`tvm_ffi` 永远可用且排第一，所以 CUDA 的 auto 默认就是 `tvm_ffi`。

**MACA 注册清单**——结构对称，但第二顺位是 `mcrtc`（而非 `nvrtc`）：

[tilelang/maca/execution_backend.py:34-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L34-L58)：

```python
register_execution_backend("maca", ExecutionBackendSpec(
    "tvm_ffi", supports_target=_is_plain_maca_target,
    enable_host_codegen=True, enable_device_compile=True), override=True)
register_execution_backend("maca", ExecutionBackendSpec(
    "mcrtc", is_available=_is_mcrtc_available, supports_target=_is_plain_maca_target), override=True)
register_execution_backend("maca", ExecutionBackendSpec(
    "cython", supports_target=_is_plain_maca_target), override=True)
register_execution_backend("maca", ExecutionBackendSpec(
    "cutedsl", is_available=_is_cutedsl_available, supports_target=_is_cutedsl_target), override=True)
```

`_is_mcrtc_available` 懒导入 `tilelang.jit.adapter.mcrtc.is_mcrtc_available`（[maca/execution_backend.py:16-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L16-L21)）——这就是 metax 分支相对 CUDA 多出的运行时编译后端（见 [u3-l3](u3-l3-running-on-metax-maca.md)）。

#### 4.4.4 代码实践

**实践目标**：看清 target 与 execution backend 的绑定关系。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [tilelang/cuda/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/execution_backend.py) 与 [tilelang/maca/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py) 并排对照。
2. 列出两者各自注册的 `(backend, is_available, supports_target, enable_*)` 四元组。
3. 回答：若 `target={"kind":"maca"}` 且 `execution_backend="nvrtc"`，会发生什么？

**需要观察的现象**：MACA 没有 `nvrtc` 这一条注册，`nvrtc` 不在 maca 的 `allowed_all` 列表里。

**预期结果**：`resolve_execution_backend_spec` 抛 `ValueError: Invalid execution backend 'nvrtc' for target 'maca'. Allowed: tvm_ffi, mcrtc, cython, cutedsl. Tip: use execution_backend='auto'.`（见 [execution_backend.py:106-110](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L106-L110)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_is_plain_cuda_target` 要排除 `"cutedsl" in target.keys` 的 target？

**参考答案**：同一个 `cuda` kind 下有两种子情形：普通 CUDA（走 tvm_ffi/nvrtc/cython）与 CuTe DSL（走 cutedsl）。用 `target.keys` 里是否含 `"cutedsl"` 把两者分流到各自的 spec，避免它们在同一次解析里都匹配、互相干扰。

**练习 2**：把 `execution_backend` 设成 `"dlpack"` 会怎样？

**参考答案**：`canonicalize_execution_backend("dlpack")` 把它映射成 `"tvm_ffi"`（[execution_backend.py:21-25](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/execution_backend.py#L21-L25)），等价于显式指定 `tvm_ffi`，是历史别名。

---

## 5. 综合实践

把本讲的四条主线串起来：用 `matmul.compile(...)` 编译一个 GEMM，**打印源码 → 导出源码 → 测延迟 → 触发并验证磁盘缓存**。

参考 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)，编写下面这段脚本（示例代码，非项目原有文件）：

```python
# 示例代码
import os
os.environ["TILELANG_VERBOSE"] = "1"      # 打开 verbose，便于观察缓存命中
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul(A, B, block_M, block_N, block_K, dtype=T.float16, accum_dtype=T.float32):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_K, block_N), dtype)
        C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
        T.clear(C_local)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by * block_M, k * block_K], A_shared)
            T.copy(B[k * block_K, bx * block_N], B_shared)
            T.gemm(A_shared, B_shared, C_local)
        T.copy(C_local, C[by * block_M, bx * block_N])
    return C

# 1. 编译（第一次：cache miss，触发 lower + codegen + 落盘）
kernel = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)

# 2. 打印生成的设备源码
print(kernel.get_kernel_source()[:200] + " ...")

# 3. 导出源码到文件
kernel.export_sources(kernel_path="/tmp/gemm_kernel.cu", host_path="/tmp/gemm_host.cc")

# 4. 测延迟（需要真实设备）
profiler = kernel.get_profiler()
print("latency =", profiler.do_bench(backend="cupti"), "ms")

# 5. 再次编译同样参数 —— 应命中进程内/磁盘缓存，不再打印 "begins to compile"
kernel2 = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
assert kernel is kernel2   # 进程内缓存命中，是同一个对象
```

**验收清单**：

- 步骤 1 的日志出现 `Generated cache key: <sha256>` 与 `begins to compile`。
- 步骤 3 后 `/tmp/gemm_kernel.cu` 非空。
- 步骤 5 不再出现 `begins to compile`，且 `kernel is kernel2` 成立。
- 进阶：把 `block_K` 改成 `64` 再编译，确认 `begins to compile` 重新出现（缓存键变化）。

> 待本地验证：步骤 4 的 `do_bench` 需要真实 GPU；若仅做源码层面验证，可跳过步骤 4。

## 6. 本讲小结

- **`@tilelang.jit`** 只是把函数包成 `JITImpl`，暂存编译参数；真正的编译发生在调用 `.compile(...)` 或 `__call__` 时。`JITImpl` 支持 lazy / eager 两种模式，按「函数是否显式返回 `PrimFunc`」自动推断。
- **`tilelang.compile`** 是真正的编译入口，它把环境变量处理、target 归一化、执行后端解析**集中**到 `cached()` 一处，再委托 `KernelCache`。
- **两级缓存**：`JITImpl._kernel_cache` / `_call_form_cache`（进程内，tight loop 优化）+ `KernelCache`（单例，内存 + 磁盘，原子落盘）。缓存键由 TIR 脚本、target、执行后端、pass 配置、tilelang 版本共同决定。
- **`JITKernel`** 是用户拿到的 kernel 对象，`__init__` 里完成 `lower + adapter 分发`；它既是可调用对象（`kernel(a, b)`），又提供 `get_kernel_source` / `get_profiler` / `export_sources` / `export_library` / `show_ptx` 等自省与导出方法。
- **执行后端** 与 target 正交但受限：每个 target 注册一组 `ExecutionBackendSpec`，`auto` 取首个可用；CUDA 的默认是 `tvm_ffi`，MACA 比 CUDA 多一个 `mcrtc`（运行时编译）。
- 关键链路：`@jit → JITImpl.__call__/.compile → compile() → cached() → KernelCache.cached → JITKernel.__init__ → _compile_and_create_adapter → tilelang.lower + adapter`。

## 7. 下一步学习建议

- 想看 `tilelang.lower` 在 `JITKernel` 内部到底跑了哪些 pass？进入 [u4-l1 下译流程](u4-l1-lowering-pipeline.md)，本讲的 `artifact = tilelang.lower(...)` 就在那里展开。
- 想理解 `JITKernel.get_profiler().do_bench()` 的精确计时原理（L2 冲刷、`backend` 选项）？直接读 [u8-l3 性能剖析与基准测试](u8-l3-profiling-and-benchmark.md)。
- 想在 MACA（MetaX）上真正跑一个 kernel、用 `mcrtc` 后端？继续 [u3-l3 在 Metax GPU 上运行](u3-l3-running-on-metax-maca.md)。
- 想用 `par_compile` / `JITImpl.par_compile` 做大规模搜参？它们正是 [u8-l1 自动调优 autotuner](u8-l1-autotuner.md) 的底层。
