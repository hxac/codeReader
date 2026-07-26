# jit 装饰器与 lazy/eager 执行模式

## 1. 本讲目标

上一讲（u4-l1）我们看清了 `tilelang.lower()` 把一个 PrimFunc 变成可执行设备代码的端到端流程。但用户日常几乎不会手写 PrimFunc 再去调 `lower()`，而是用一行 `@tilelang.jit` 装饰器，像写普通 Python 函数一样定义 kernel。本讲就来回答：

- `@tilelang.jit` 到底把一个 Python 函数包装成了什么？
- tilelang 有 **lazy** 与 **eager** 两种执行模式，它们在「调用时返回什么」上有什么本质区别？
- `_infer_jit_mode` 是如何自动判断该用哪种模式的？
- `compile()` / `par_compile()` 与 `JITKernel` 之间是什么关系？
- `get_tir` / `get_kernel_source` / `compile` 这三个常用方法的调用链是怎样的？

学完本讲，你应当能针对同一个 GEMM，分别用 lazy 与 eager 两种风格写出来，并清楚它们在返回值类型、`out_idx` 用法、缓存键上的差异。

## 2. 前置知识

- **TIR PrimFunc**：tilelang 把 kernel 表示成 TVM 的张量中间表示（TIR）函数。详见 u2-l1、u4-l1。
- **builder 模式**：tilelang 的 DSL 不是解释执行的，而是在「编译期把函数体执行一次」，用 builder 把 `T.Kernel`、`T.copy` 等语句搭建成 TIR AST。详见 u5-l1。
- **JITKernel / adapter**：编译产物被包装成 `JITKernel`，内部再由 adapter（tvm_ffi / nvrtc / torch 等）接住 PyTorch tensor。本讲会展开，adapter 细节留到 u7-l1。
- **缓存与重编译**：换 shape 或编译参数通常会触发重编译，缓存能避免重复编译。本讲会讲 JIT 层的缓存。

一句话回顾：`@tilelang.jit` 修饰的函数体是「搭建 IR 的蓝图」，不是运行时计算逻辑。本讲关注的是「蓝图如何被编译并执行」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/jit/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py) | JIT 核心：`jit` 装饰器、`JITImpl` 包装类、模块级 `compile` / `par_compile`、模式推断 `_infer_jit_mode`、`_CallFormCache`。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py) | `JITKernel`：编译产物 + adapter 的封装，提供 `__call__` / `get_kernel_source` / `get_profiler` 等。 |
| [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) | `JITFunc`、`TirTemplate`、`_is_lazy_style`、`parse_args`、两阶段（phase1/phase2）TIR 构造。模式推断的真正实现落在这里。 |
| [tilelang/cache/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/__init__.py) | `cached()`：按 execution_backend 分发到各后端单例 `KernelCache`，复用编译产物。 |
| [examples/eager_jit/eagerjit.en.ipynb](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/eager_jit/eagerjit.en.ipynb) | eager 模式官方示例：`T.const` / `T.empty` / `compile` / `par_compile` 的用法与开销。 |
| [examples/quickstart.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py) | 一个 eager 风格的入门 GEMM，串起 compile → 调用 → 验证 → 看源码 → 测延迟。 |

> 本讲聚焦两个最小模块：**`tilelang.jit`**（装饰器、`JITImpl`、模式推断、`compile`/`par_compile`）与 **`tilelang.jit.kernel`**（`JITKernel`）。`tilelang.language.eager.builder` 与 `tilelang.cache` 是支撑这两个模块的「地基」，会按需引用。

## 4. 核心概念与源码讲解

### 4.1 lazy 与 eager：两种执行模式与自动推断

#### 4.1.1 概念说明

`@tilelang.jit` 修饰的函数，按「调用时返回什么」分成两种模式：

| 模式 | 函数体写法 | 调用 `f(...)` 返回什么 | `out_idx` | 典型场景 |
| --- | --- | --- | --- | --- |
| **lazy（惰性）** | 在函数内部定义一个 `@T.prim_func` 并 `return kernel` | 一个**已编译的 kernel 对象**（`JITKernel`），需再单独调用 `kernel(a, b)` 才执行 | 支持，用 `out_idx=[-1]` 指定返回哪些输出 | 想反复检视、复用、基准同一个 kernel |
| **eager（急切）** | 直接用 builder 模式：`A: T.Tensor[...]` 标注、`T.empty()` 声明输出、`return C` | **直接返回结果张量**（编译 + 执行一次完成） | 不支持（会报错），改用 `T.empty()` 声明输出 | 写起来最像普通函数，调用即得结果 |

一句话区分：**lazy 调用一次得到 kernel，eager 调用一次得到结果**。

这两种写法在源码注释里有官方对照（以下为 docstring 中的示例，懒加载风格内含 `@T.prim_func`）：

[.tilelang/jit/__init__.py:L271-L298 — JITImpl docstring 给出的 lazy 与 eager 两种写法对照](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L271-L298)：lazy 风格显式 `return kernel`（一个 PrimFunc），eager 风格用张量注解 + `T.empty`。

需要注意：仓库里 `examples/quickstart.py` 与 `examples/gemm/example_gemm.py` 虽然叫 "matmul"，但它们用的是 **eager 风格**（`T.const` + `T.empty` + `return C`），不是 lazy 风格。真正的 lazy 风格需要在函数内嵌套 `@T.prim_func` 并返回它。

#### 4.1.2 核心流程

模式并不是写死的，而是**自动推断**的。`JITImpl` 有一个 `mode` 字段，取值 `"auto" / "lazy" / "eager"`，初始为 `"auto"`：

```text
用户调用 f(...)
   │
   ▼
JITImpl.__call__：if mode == "auto" → _infer_jit_mode(*args, **kwargs)
   │
   ▼
_infer_jit_mode：
   - 若 mode 已是 "lazy"/"eager" → 直接返回
   - 若 func 不是 JITFunc（如已是 PrimFunc）→ "lazy"
   - 否则 → func._is_lazy_style(*args, **kwargs)
   │
   ▼
_is_lazy_style（在 JITFunc 上）：
   - 函数体内是否嵌套了 @T.prim_func？ → 是则 lazy
   - 否则试着调用一次：返回 PrimFunc？ → lazy
   - 调用时抛 JITNoBuilderError/EagerJITBuildError？ → eager
        （因为 eager 专属的 T.const()/T.Kernel() 在没有 builder 时会报错，
          这恰恰说明它是 eager 风格）
```

推断完成后，`mode` 被「固化」到 `JITImpl` 上（`set_mode`），后续调用不再重复推断。

#### 4.1.3 源码精读

**模块级 `jit` 装饰器**：把任意可调用对象包成 `JITImpl`，初始 `mode="auto"`。

[.tilelang/jit/__init__.py:L614-L628 — jit 装饰器内部用 prim_func(eager_jit=True) 得到 JITFunc，再包成 JITImpl(mode="auto")](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L614-L628)：注意 `mode="auto"` 是初始值，真正判定发生在第一次调用。

**`_infer_jit_mode`**：决策树入口。

[.tilelang/jit/__init__.py:L370-L383 — _infer_jit_mode：已固定模式直接返回，否则委托给 JITFunc._is_lazy_style](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L370-L383)：注意它把判定结果返回给调用方，真正的「固化」由 `initialize_jit_mode` 完成。

**`initialize_jit_mode`**：固化模式，并校验 eager 模式不能用 `out_idx`。

[.tilelang/jit/__init__.py:L385-L391 — initialize_jit_mode：auto→推断→set_mode，且 eager 模式下传 out_idx 会直接报错](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L385-L391)：这正是「eager 模式不支持 `out_idx`，改用 `T.empty()`」这条规则的来源。

**`_is_lazy_style`**：真正的判定逻辑（在 builder.py 的 `JITFunc` 上）。

[.tilelang/language/eager/builder.py:L1380-L1418 — _is_lazy_style：先看是否内嵌 @T.prim_func，再试调用，依返回类型与异常判定](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1380-L1418)：关键点有两个——(1) `has_internal_prim_func` 检测函数体内是否定义了 `@T.prim_func`；(2) eager 专属特性（`T.const()`/`T.Kernel()`）在没有 builder 上下文时会抛 `JITNoBuilderError`，被这里捕获作为「这是 eager 风格」的信号。

#### 4.1.4 代码实践

**实践目标**：亲手验证模式自动推断，并观察 eager 模式下 `out_idx` 的报错。

**操作步骤**（源码阅读 + 本地验证）：

1. 阅读上面的 `_is_lazy_style`，回答：为什么一个用 `T.const()` 的函数会被判成 eager？
2. 在有 CUDA GPU 的机器上（**待本地验证**）跑下面两段对照代码：

```python
# 示例代码 —— lazy 风格（内嵌 @T.prim_func 并返回）
import tilelang, tilelang.language as T

@tilelang.jit(out_idx=[-1])
def matmul_lazy(M, N, K, block_M, block_N, block_K, dtype="float16"):
    @T.prim_func
    def kernel(A: T.Tensor((M, K), dtype), B: T.Tensor((K, N), dtype),
               C: T.Tensor((M, N), dtype)):
        with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=128) as (bx, by):
            A_s = T.alloc_shared((block_M, block_K), dtype)
            B_s = T.alloc_shared((block_K, block_N), dtype)
            C_l = T.alloc_fragment((block_M, block_N), "float32")
            T.clear(C_l)
            for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
                T.copy(A[bx*block_M, k*block_K], A_s)
                T.copy(B[k*block_K, by*block_N], B_s)
                T.gemm(A_s, B_s, C_l)
            T.copy(C_l, C[bx*block_M, by*block_N])
    return kernel

ker = matmul_lazy(1024, 1024, 1024, 128, 128, 32)   # 返回 kernel 对象，未执行
print(type(ker))                                     # <class '...JITKernel'>
```

```python
# 示例代码 —— eager 风格（T.const + T.empty + return C）
@tilelang.jit
def matmul_eager(A, B, block_M=128, block_N=128, block_K=32):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), "float16")
    B: T.Tensor((K, N), "float16")
    C = T.empty((M, N), "float32")
    with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=128) as (bx, by):
        # ...（同上 tile 主体，略）
        pass
    return C

import torch
A = torch.randn(1024, 1024, device="cuda", dtype=torch.float16)
B = torch.randn(1024, 1024, device="cuda", dtype=torch.float16)
C = matmul_eager(A, B)            # 返回的是结果张量
print(type(C), C.shape)           # <class 'torch.Tensor'> torch.Size([1024, 1024])
```

3. 试着把 eager 版本写成 `@tilelang.jit(out_idx=[-1])` 再调用，观察 `initialize_jit_mode` 抛出的 `ValueError`。

**需要观察的现象 / 预期结果**：lazy 版 `matmul_lazy(...)` 返回 `JITKernel`，需再 `ker(A, B)` 才得到结果；eager 版 `matmul_eager(A, B)` 直接返回 `torch.Tensor`；eager 版加 `out_idx` 会报「out_idx is only supported in lazy mode」。无 GPU 时标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 `@tilelang.jit` 函数既不内嵌 `@T.prim_func`，也不返回 PrimFunc，但函数体里没有任何张量注解，调用时会怎样？
**答案**：`_is_lazy_style` 会试着调用它；如果调用既不返回 PrimFunc、也不抛 `JITNoBuilderError/EagerJITBuildError`，则判为 eager，但随后 builder 阶段大概率因缺少合法的 kernel 上下文而报错。模式推断只判断 lazy/eager，不保证函数本身正确。

**练习 2**：为什么 eager 模式不允许用 `out_idx`？请用一句话说明。
**答案**：eager 模式用 `T.empty()` 在函数体内显式声明输出张量，输出由 `return` 决定（`out_idx` 在 lowering 时自动从 `T.empty` 推断），再叠加外部 `out_idx` 会语义冲突，因此 `initialize_jit_mode` 直接拒绝。

---

### 4.2 JITImpl 的调用流程：三层缓存与参数绑定

#### 4.2.1 概念说明

`JITImpl` 是 `@tilelang.jit` 返回的对象，它负责「把一次 Python 调用，变成一次编译或一次缓存命中」。核心难点是：同一个被装饰函数会被反复调用，必须用缓存避免重复编译。tilelang 在 JIT 层布置了**三层缓存**：

1. **`_call_form_cache`（调用形式缓存）**：仅对 **lazy 且无 tensor 参数**的函数生效。直接用原始 `(args, kwargs)` 当键，命中就返回已编译的 kernel 对象，跳过参数绑定与 parse。这是为「紧凑循环里反复 `f(1024, 1024, 128, 128, 32)`」准备的快路径。
2. **`_kernel_cache`（kernel 缓存）**：通用层，键是 `parse_args` 返回的 `(p1_key, p2_key)` 二元组（见下），值是编译好的 kernel。
3. **`p1_cache`（TIR 模板缓存）**：在 `JITFunc` 上，缓存「同一份 TIR 模板」，避免对相同编译期参数重复 trace 函数体。

`out_idx` 在 lazy 模式下用于告诉编译器「返回第几个输出张量」；eager 模式则由 `T.empty()` 自动处理。

#### 4.2.2 核心流程

`JITImpl.__call__` 的执行流程：

```text
__call__(*args, **kwargs)
  │  1. 弹出内部用的 __return_compile_arguments / __tune_params
  │  2. 若 mode=="auto"：推断并 set_mode
  │
  ├─ lazy 且 _can_use_call_form_cache？
  │     → _call_form_cache.lookup(args, kwargs)
  │       命中？ 直接 return kernel（快路径，结束）
  │
  ▼
  3. key, kernel_args = self.func.parse_args(*args, **kwargs)
        parse_args 返回 ((p1_key, p2_key), tensor_args)
  4. kernel = _kernel_cache.get(key)
        未命中？ → self.compile(*args, **kwargs) 并存入 _kernel_cache
  │
  ├─ 若又满足 call-form 条件 → 顺便存入 _call_form_cache
  │
  ▼
  5. mode=="eager" ?  kernel(*kernel_args.values())  → 返回结果张量
                     : return kernel                 → 返回 kernel 对象
```

`parse_args` 的键分两层，对应「两阶段」机制：

- **`p1_key`（phase-1 键）**：编译期参数（block 尺寸、dtype 等）归一化而成，用于识别「同一份 TIR 模板」。
- **`p2_key`（phase-2 键）**：从实际 tensor 的 shape/stride 抽出的值，用于把 `T.const` 占位符替换成具体数字（详见 u2-l4 与 4.4 节的 `TirTemplate`）。

#### 4.2.3 源码精读

**三层缓存的初始化**（`__post_init__`）：

[.tilelang/jit/__init__.py:L345-L354 — JITImpl.__post_init__：初始化 _kernel_cache / _call_form_cache / _tuner_cache](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L345-L354)：三个 dict 分别缓存 kernel 对象、调用形式、autotuner 结果。

**`__call__` 主体**：上面的流程图对应这段代码。

[.tilelang/jit/__init__.py:L495-L540 — JITImpl.__call__：弹参 → 推断模式 → call-form 快路径 → parse_args → _kernel_cache → eager 执行 / lazy 返回](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L495-L540)：注意末尾的分支——eager 调 `kernel(*kernel_args.values())` 返回结果，lazy 直接 `return kernel`。

**call-form 缓存的命中条件**：只有「无 tune 参数 + 是 JITFunc + 没有 tensor 参数」时才启用。

[.tilelang/jit/__init__.py:L490-L493 — _can_use_call_form_cache：只对无 tensor 参数的 JITFunc 生效，因为它直接返回 kernel 对象](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L490-L493)：eager 函数有 tensor 参数，因此走不到这条快路径。

**`_CallFormCache`**：带「上一次命中」记忆，避免每次都重建并哈希调用形式键。

[.tilelang/jit/__init__.py:L75-L88 — _CallFormCache.lookup：先比对上次调用的 (args, kwargs)，再查 entries 字典](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L75-L88)：这是为紧凑循环优化的「最快路径」。

**`parse_args`（在 `JITFunc` 上）**：产出 `(p1_key, p2_key)` 二级键与 tensor 参数。

[.tilelang/language/eager/builder.py:L1437-L1450 — JITFunc.parse_args：无 tensor 时返回 ((p1_key, None), {})；否则查/建 TIR 模板并算 p2_key](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1437-L1450)：注意 `p1_cache` 在这里被填充——同一组编译期参数只 trace 一次函数体。

**`parse_cache_key`**：给 autotuner 用的键（与上面两层缓存不同），把位置参、关键字参、调优参各自排序打包。

[.tilelang/jit/__init__.py:L475-L481 — parse_cache_key：构造 (args, sorted(kwargs), sorted(tune_params)) 三段键](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L475-L481)：autotuner（u8-l1）会用它做结果缓存。

#### 4.2.4 代码实践

**实践目标**：观察缓存命中带来的开销差异。

**操作步骤**（**待本地验证**，源自 `examples/eager_jit/eagerjit.en.ipynb` 的「Overhead of argument matching」一节）：

1. 定义一个最小 eager kernel（无计算，仅用于测开销），先用一组 tensor 触发编译；
2. 用 `time.perf_counter_ns` 分别测量：连续 10000 次 `f(A, B)` 的平均耗时，与连续 10000 次 `f.parse_cache_key(A, B)` 的平均耗时。

```python
# 示例代码 —— 改编自 examples/eager_jit/eagerjit.en.ipynb
import time, torch, tilelang, tilelang.language as T

@tilelang.jit
def dummy_kernel(A, B):
    M, N = T.const("M, N")
    A: T.Tensor((M, N), T.float16)
    B: T.Tensor((M, N), T.float16)
    with T.Kernel(1) as _:
        pass

A = torch.randn(128, 128, dtype=torch.float16, device="cuda")
B = torch.randn(128, 128, dtype=torch.float16, device="cuda")
dummy_kernel(A, B)   # 先编译

def bench(f, n=10000):
    s = time.perf_counter_ns()
    for _ in range(n): f()
    return (time.perf_counter_ns() - s) / n / 1000

print(f"Kernel call    : {bench(lambda: dummy_kernel(A, B)):.2f} us")
print(f"Parse cache key: {bench(lambda: dummy_kernel.parse_cache_key(A, B)):.2f} us")
```

**需要观察的现象 / 预期结果**：notebook 中实测约为「Kernel call ≈ 7–8 us，Parse cache key ≈ 0.4 us」。也就是说参数绑定/键构造本身很便宜（每个常量注解约 200 ns），主要开销在实际 kernel 启动。你的机器数值会不同，但 parse_cache_key 应明显小于一次 kernel call。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_call_form_cache` 只对「无 tensor 参数」的 lazy 函数启用？
**答案**：因为它直接缓存并返回 kernel 对象，不做参数绑定；而带 tensor 参数的（eager）函数每次调用都需要把实际 tensor 喂给 kernel 执行，不能「命中就返回对象」。

**练习 2**：同一个 eager kernel 用 `(M=1024, N=1024)` 调用两次，第二次会重新编译吗？
**答案**：不会。两次调用的 `p1_key` 相同（编译期参数一致），tensor shape 一致则 `p2_key` 也相同，`(p1_key, p2_key)` 命中 `_kernel_cache`，直接复用已编译 kernel。

---

### 4.3 编译调用链：get_tir → compile → JITKernel，以及 par_compile

#### 4.3.1 概念说明

`JITImpl` 提供了一组配套方法，它们构成一条清晰的调用链：

```text
get_tir(*args)              → PrimFunc          （只取 IR，不编译）
   └─ initialize_jit_mode → func(...) 或 func 本身
compile(*args)              → JITKernel         （编译，得到可调用对象）
   └─ get_tir(*args) → 模块级 compile(prim_func, ...) → cached(...) → JITKernel
get_kernel_source(*args)    → str               （编译后取生成的设备源码）
   └─ compile(*args) → kernel.get_kernel_source()
```

三个方法逐层「加码」：`get_tir` 最轻（只到 IR），`compile` 把 IR 编译成可执行 kernel，`get_kernel_source` 在 `compile` 基础上再取源码。

`par_compile` 则是 `compile` 的并行版本，用线程池同时编译多个 PrimFunc，是 autotuner（u8-l1）批量搜索配置的底层引擎。

#### 4.3.2 核心流程

**单函数编译链**：

```text
JITImpl.compile(*args)
  │  prim_func = self.get_tir(*args)          # 得到 PrimFunc
  │  kernel_result = compile(                 # 模块级 compile
  │      prim_func, out_idx=..., target=..., pass_configs=..., ...)
  │      │  func_attrs = prim_func.attrs
  │      │  合并函数级 tilelang_out_idx / tilelang_pass_configs / tilelang_compile_flags
  │      └─ cached(func=prim_func, ...)       # 进缓存层
  │             └─ 按 execution_backend 选单例 KernelCache.cached(...) → JITKernel
  └─ 可选：若 debug_root_path，把 kernel 源码与 IR 落盘
  → 返回 JITKernel
```

模块级 `compile` 在转交 `cached()` 之前，会**合并函数级属性**——这些属性是你在函数体里用 `T.empty`（产生 `tilelang_out_idx`）、`T.annotate_pass_configs`、`T.annotate_compile_flags` 写下的，与外部传入的参数按「外部覆盖函数级」的优先级合并。

**并行编译**：

```text
JITImpl.par_compile(configs, num_workers, ignore_error)
  │  for cfg in configs: get_tir(**cfg 或 *cfg)   # 先逐个 elaborate 成 PrimFunc
  └─ par_compile(funcs, ...)                      # 模块级 par_compile
        ThreadPoolExecutor → 每个 func submit 给 compile(...)
        tqdm 收集结果；ignore_error 时单点失败记 warning 并置 None
```

#### 4.3.3 源码精读

**`JITImpl.get_tir`**：取 PrimFunc。

[.tilelang/jit/__init__.py:L356-L368 — JITImpl.get_tir：先 initialize_jit_mode，再从 func 取 PrimFunc（直接是 PrimFunc 或调用 func 得到）](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L356-L368)：lazy 风格里 `func` 是可调用对象（调用返回 PrimFunc），eager 风格里 `func` 本身就是 PrimFunc 的工厂。

**`JITImpl.compile`**：get_tir → 模块级 compile → 可选落盘。

[.tilelang/jit/__init__.py:L442-L473 — JITImpl.compile：get_tir 后调模块级 compile，并在 debug_root_path 时写 kernel 源码与 PrimFunc script](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L442-L473)：`debug_root_path` 是调试利器——它把生成的 `.c`/`.cu` 与 IR 脚本直接写成文件。

**模块级 `compile`**：合并函数级属性 + 进缓存。

[.tilelang/jit/__init__.py:L137-L170 — 模块级 compile：先合并 func_attrs（out_idx/pass_configs/compile_flags），再 cached(...)](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L137-L170)：注意「外部 `pass_configs` 覆盖函数级」的合并语义，以及 `out_idx` 冲突（函数内有 `T.empty` 又外部传 `out_idx`）会报错。

**`cached()`**：按 execution_backend 分发到单例 KernelCache。

[.tilelang/cache/__init__.py:L67-L92 — cached：解析 target/execution_backend/verbose，选单例 KernelCache.cached(...) → JITKernel](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/__init__.py#L67-L92)：缓存层（u4-l3 会深入）在这里把「同一份 PrimFunc + 同一组编译选项」复用为同一个 `JITKernel`。

**`JITImpl.get_kernel_source`**：在 compile 基础上取源码。

[.tilelang/jit/__init__.py:L483-L485 — JITImpl.get_kernel_source：先 compile 再委托 kernel.get_kernel_source()](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L483-L485)：所以「看源码」也会触发一次编译（命中缓存则几乎零开销）。

**`JITImpl.par_compile`**：先逐个 elaborate，再委托模块级 `par_compile`。

[.tilelang/jit/__init__.py:L393-L440 — JITImpl.par_compile：把每个 config 用 get_tir 展开成 PrimFunc，再批量并行编译](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L393-L440)：config 可以是 dict（关键字）或 tuple（位置）。

**模块级 `par_compile`**：线程池 + 进度条 + 容错。

[.tilelang/jit/__init__.py:L221-L253 — 模块级 par_compile：ThreadPoolExecutor 把每个 func 交给 compile，tqdm 收集，ignore_error 时单点失败置 None](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L221-L253)：`ignore_error` 让大规模搜索时单个配置炸掉不会拖垮整批。

#### 4.3.4 代码实践

**实践目标**：用 `get_tir` / `compile` / `get_kernel_source` 三种方式查看同一个 kernel，体会调用链的逐层加码。

**操作步骤**（**待本地验证**，参考 `examples/quickstart.py`）：

```python
# 示例代码 —— 改编自 examples/quickstart.py（eager 风格）
import tilelang, tilelang.language as T

@tilelang.jit
def matmul(A, B, block_M: int, block_N: int, block_K: int):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), T.float16)
    B: T.Tensor((K, N), T.float16)
    C = T.empty((M, N), T.float16)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_s = T.alloc_shared((block_M, block_K), T.float16)
        B_s = T.alloc_shared((block_K, block_N), T.float16)
        C_l = T.alloc_fragment((block_M, block_N), T.float32)
        T.clear(C_l)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by*block_M, k*block_K], A_s)
            T.copy(B[k*block_K, bx*block_N], B_s)
            T.gemm(A_s, B_s, C_l)
        T.copy(C_l, C[by*block_M, bx*block_N])
    return C

# (1) 只取 TIR，不编译
tir = matmul.get_tir(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
print(type(tir))                 # PrimFunc

# (2) 编译，得到可复用 kernel
ker = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
print(type(ker))                 # JITKernel

# (3) 直接拿生成的设备源码（内部会 compile）
src = matmul.get_kernel_source(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
print(src[:200])
```

**需要观察的现象 / 预期结果**：`get_tir` 返回 PrimFunc（可 `.script()` 打印 IR）；`compile` 返回 `JITKernel`；`get_kernel_source` 返回一段 CUDA/HIP 字符串。三者耗时应递增（`get_tir` 最快，因为它不跑 Pass 流水线之后的代码生成）。

#### 4.3.5 小练习与答案

**练习 1**：在函数体里写 `T.annotate_pass_configs({PassConfigKey.TL_ENABLE_FAST_MATH: True})`，又在外部 `compile(..., pass_configs={...})` 传了同名键，最终用哪个？
**答案**：用外部的。模块级 `compile` 在合并时，`func_pc.update(pass_configs)`——外部传入的 `pass_configs` 覆盖函数级（[L146-L151](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L146-L151)）。

**练习 2**：`par_compile(configs, ignore_error=True)` 返回的列表里可能出现什么？
**答案**：可能出现 `None`——某个 config 编译失败时，若 `ignore_error=True`，失败项记 warning 并置 `None`，其余正常返回 `JITKernel`。

---

### 4.4 JITKernel：编译产物的封装与执行

#### 4.4.1 概念说明

`JITKernel`（`tilelang.jit.kernel`）是「编译完成」的产物，也是 lazy 模式下 `f(...)` 的返回值、eager 模式下 `f.compile(...)` 的返回值。它把两样东西捏在一起：

- **`artifact`（`CompiledArtifact`）**：`tilelang.lower()` 的产物，含 host/device IRModule、运行时模块（`rt_mod`）、参数描述（`params`）、生成的设备源码。
- **`adapter`（`BaseKernelAdapter`）**：把上述产物包装成「能直接吃 PyTorch tensor 的可调用对象」。adapter 有 tvm_ffi / cython / nvrtc / torch(metal) / cutedsl 等多种，由 `execution_backend` 决定（adapter 细节见 u7-l1）。

对用户而言，`JITKernel` 是一个**像函数一样的对象**：`kernel(a, b)` 即执行；同时提供 `get_kernel_source()` / `get_profiler()` / `out_idx` / `params` 等便利接口。

#### 4.4.2 核心流程

`JITKernel.__init__` 的核心是 `_compile_and_create_adapter`：

```text
__init__(func, out_idx, execution_backend, target, ...)
  │  determine_target(target)              → TVM Target
  │  resolve_execution_backend_spec(...)   → 选定 adapter 与是否需要 host/device codegen
  │
  └─ _compile_and_create_adapter(func, out_idx)
        │  组装 pass_configs（合并 compile_flags → TL_DEVICE_COMPILE_FLAGS）
        │  装配 pass instruments（DumpIR / pass timing）
        │  with PassContext(opt_level=3, config=pass_configs), self.target:
        │      artifact = tilelang.lower(func, target, target_host,
        │                              enable_host_codegen, enable_device_compile)
        │
        └─ 按 execution_backend 选 adapter 类（tvm_ffi/cython/nvrtc/torch/cutedsl）
              用 artifact 里的字段构造 adapter
        → self.adapter = adapter; self.torch_function = adapter.func
```

执行时 `JITKernel.__call__(*args)` 直接委托给 `self.torch_function(*args)`，也就是 adapter 的可调用函数。

#### 4.4.3 源码精读

**`JITKernel.__init__`**：解析 target 与 execution_backend，再编译。

[.tilelang/jit/kernel.py:L102-L143 — JITKernel.__init__：normalize pass_configs、determine_target、resolve_execution_backend_spec、再 _compile_and_create_adapter，最后 torch_function = adapter.func](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L102-L143)：注意 `from_database=True` 时会跳过编译，直接走 `from_database` 备选构造器（用于命中磁盘缓存）。

**`_compile_and_create_adapter`**：打开 PassContext 与 target，调 `tilelang.lower`，再选 adapter。

[.tilelang/jit/kernel.py:L268-L309 — _compile_and_create_adapter 的核心：with PassContext + target 调 tilelang.lower(...) 得 artifact，再用 TVMFFIKernelAdapter 等包装](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L268-L309)：这段是 u4-l1 讲的 `lower()` 与本讲 JIT 层的衔接点——PassContext 在这里被打开，`tilelang.lower` 在其中运行整个 Pass 流水线。

**按 execution_backend 分发 adapter**：

[.tilelang/jit/kernel.py:L292-L372 — 按 execution_backend 选 TVMFFIKernelAdapter / Cython / NVRTC / Metal(torch) / CuTeDSL](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L292-L372)：注意 `torch` 后端对应的是 Metal（`assert is_metal_target(target)`），`cutedsl` 对应 CuTeDSL——后端名与 adapter 并非一一对应，此处是个易混点。

**`__call__`**：委托给 adapter 的可调用函数。

[.tilelang/jit/kernel.py:L188-L204 — JITKernel.__call__：直接 self.torch_function(*args, **kwds)](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L188-L204)：所以 `kernel(a, b)` 的实际开销 = adapter 启动 kernel 的开销。

**几个常用接口**：

- [get_kernel_source（L485-L496）](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L485-L496)：按后端从 adapter 或 artifact 取设备源码。
- [get_profiler（L469-L483）](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L469-L483)：返回 `Profiler`，用于 `do_bench()` 测延迟（u8-l3）。
- [out_idx 属性（L655-L657）](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L655-L657)：取自 `adapter.result_idx`。
- [params 属性（L659-L661）](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L659-L661)：优先取 `artifact.params`，否则取 `adapter.params`。

**两阶段 TIR 构造（`TirTemplate`）**：这是 eager 模式「一次模板、多 shape 复用」的关键，承接 u2-l4 的 `T.const`。

[.tilelang/language/eager/builder.py:L1097-L1109 — TirTemplate.get_tir：lazy 直接返回 prim_func；eager 则用 phase2 把 constexpr 占位符替换为实际 shape 后重建](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1097-L1109)：phase1 用 `T.const` 生成带占位符的模板（[L1420-L1433](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1420-L1433)），phase2 从实参 tensor 的 shape/stride 抽值替换（[L1074-L1095](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1074-L1095)）。

#### 4.4.4 代码实践

**实践目标**：编译一个 GEMM，检查 `JITKernel` 的 `out_idx` / `params`，并用它执行与基准。

**操作步骤**（**待本地验证**，改编自 `examples/gemm/example_gemm.py`）：

```python
# 示例代码
import tilelang, tilelang.language as T, torch

@tilelang.jit
def matmul(A, B, block_M, block_N, block_K, dtype=T.float16, accum_dtype=T.float32):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_s = T.alloc_shared((block_M, block_K), dtype)
        B_s = T.alloc_shared((block_K, block_N), dtype)
        C_l = T.alloc_fragment((block_M, block_N), accum_dtype)
        T.clear(C_l)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by*block_M, k*block_K], A_s)
            T.copy(B[k*block_K, bx*block_N], B_s)
            T.gemm(A_s, B_s, C_l)
        T.copy(C_l, C[by*block_M, bx*block_N])
    return C

ker = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
print("out_idx:", ker.out_idx)        # 由 T.empty 推断
print("params :", [p for p in ker.params])  # 输入/输出张量参数描述

a = torch.randn(1024, 1024, device="cuda").half()
b = torch.randn(1024, 1024, device="cuda").half()
c = ker(a, b)                          # 执行
torch.testing.assert_close(c, a @ b, rtol=1e-2, atol=1e-2)

lat = ker.get_profiler().do_bench()    # 基准
print(f"latency = {lat} ms")
```

**需要观察的现象 / 预期结果**：`out_idx` 反映 `T.empty` 声明的输出位置；`params` 列出 kernel 的张量参数；`ker(a, b)` 返回结果张量且与 `a @ b` 对齐；`do_bench()` 给出一次延迟。

#### 4.4.5 小练习与答案

**练习 1**：`JITKernel.__call__(a, b)` 实际调用的是什么？
**答案**：`self.torch_function(a, b)`，即 adapter（如 `TVMFFIKernelAdapter`）包装出的可调用函数，它负责把 PyTorch tensor 转成运行时参数并启动 kernel。

**练习 2**：为什么 `execution_backend="torch"` 时会 `assert is_metal_target(target)`？
**答案**：在 tilelang 的命名里，`torch` 执行后端专指「通过 PyTorch 的 Metal MPS 后端运行」，因此只对 Metal target 有效（见 [L340-L354](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L340-L354)）。CUDA/ROCm 应分别用 `tvm_ffi`/`nvrtc` 等。

---

## 5. 综合实践

**任务**：把同一个 GEMM 分别用 **lazy** 与 **eager** 两种风格实现，调用它们，对比「返回值类型」「`out_idx` 的用法」与「触发重编译的条件」。

**建议步骤**：

1. 写一个 lazy 风格 `matmul_lazy`：内部嵌套 `@T.prim_func`，`return kernel`，装饰器带 `@tilelang.jit(out_idx=[-1])`。调用 `matmul_lazy(1024,1024,1024,128,128,32)`，确认返回 `JITKernel`，再 `ker(a,b)` 得到结果。
2. 写一个 eager 风格 `matmul_eager`：用 `M,N,K = T.const("M, N, K")`、`T.Tensor` 注解、`C = T.empty(...)`、`return C`。直接 `matmul_eager(a, b)`，确认返回 `torch.Tensor`。
3. 对比 `out_idx`：lazy 用装饰器参数 `out_idx=[-1]`；eager 不传 `out_idx`（强行传会报错），输出由 `T.empty` + `return` 决定。
4. 触发重编译：对 lazy 版换 `block_M`；对 eager 版换输入 tensor 的 `M`。用 `get_tir`/`compile` 配合计时，观察 `_kernel_cache` 命中与未命中的耗时差。
5. 用 `par_compile` 对其中一种风格批量编译几种 `block_M/block_N/block_K` 组合，打印返回的 `JITKernel` 列表长度。

**验收标准**：能口述「lazy 调用一次得到 kernel、eager 调用一次得到结果」；能解释三种缓存（`_call_form_cache` / `_kernel_cache` / `p1_cache`）各自拦截哪一类重复调用；能画出 `get_tir → compile → JITKernel → adapter → torch_function` 的调用链。

> 若本地无 GPU：把第 1–3 步的「调用」改为「读源码 + 静态分析」，即只读 `JITImpl.__call__`、`_infer_jit_mode`、`_is_lazy_style`，书面推导两种写法分别走哪条分支、返回什么类型，并标注「待本地验证」。

## 6. 本讲小结

- `@tilelang.jit` 把 Python 函数包成 `JITImpl`，初始 `mode="auto"`，由 `_infer_jit_mode` → `JITFunc._is_lazy_style` 在首次调用时自动判定 lazy / eager 并固化。
- **lazy**：函数内嵌 `@T.prim_func` 并返回它，调用返回 **kernel 对象**（`JITKernel`），支持 `out_idx`；**eager**：用 `T.const` + `T.Tensor` 注解 + `T.empty` + `return`，调用返回**结果张量**，不支持 `out_idx`。
- `JITImpl.__call__` 走三层缓存：`_call_form_cache`（lazy 无 tensor 快路径）→ `parse_args` 产出 `(p1_key, p2_key)` → `_kernel_cache`；eager 命中后执行 `kernel(*tensor_args)`，lazy 直接返回 kernel。
- 编译调用链逐层加码：`get_tir`（取 IR）→ `compile`（IR + Pass + codegen → `JITKernel`）→ `get_kernel_source`（编译后取源码）；`par_compile` 用线程池并行编译多个 PrimFunc，是 autotuner 的底层引擎。
- `JITKernel` = `CompiledArtifact`（`tilelang.lower` 产物）+ `BaseKernelAdapter`（按 `execution_backend` 选择，把产物变成吃 PyTorch tensor 的可调用对象）；`__call__` 委托给 `adapter.func`。
- eager 模式的「一次模板、多 shape」由 `TirTemplate` 的两阶段机制实现：phase1 用 `T.const` 建占位模板，phase2 用实参 shape/stride 替换。

## 7. 下一步学习建议

- **u4-l3 编译缓存机制**：本讲多次提到 `cached()` 与 `_kernel_cache`，下一讲会深入 `KernelCache`、CUDA binary cache、`enable_cache/disable_cache` 等完整缓存体系。
- **u5-l1 eager builder 与 prim_func 转换**：若你想搞清 `JITFunc`/`TirTemplate`/两阶段 builder 的更多细节（本讲的「地基」），那是正餐。
- **u7-l1 执行后端与 kernel adapter**：本讲把 adapter 当黑盒用了，下一阶段会拆开 `TVMFFIKernelAdapter` / `NVRTCKernelAdapter` 等，讲清「tensor 怎么进、结果怎么出」。
- **u8-l1 Autotuner**：本讲的 `par_compile` 与 `parse_cache_key` 是 autotuner 的基石，学完调优讲义会对这套缓存与并行编译有更深的体会。
