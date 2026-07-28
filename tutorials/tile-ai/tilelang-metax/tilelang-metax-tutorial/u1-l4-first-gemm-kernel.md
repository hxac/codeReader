# 第一个 kernel：跑通 GEMM 快速上手

## 1. 本讲目标

本讲是整个手册里「第一次真正写出并运行一个 kernel」的实战入门。学完本讲，你应当能够：

- 用 `@tilelang.jit` 把一段 Python 写成 GPU kernel 的「规格（specification）」，并用 `.compile(...)` 把它编译成一个可运行对象 `JITKernel`。
- 理解 `with T.Kernel(...)` 如何定义网格（grid）与线程（threads），并解释每个线程块计算输出矩阵的哪一块。
- 看懂 `T.copy`（数据搬运）和 `T.gemm`（分块矩阵乘）这两条最核心的 tile 原语，知道它们在 GEMM 中各自负责什么。
- 用 `get_kernel_source()` 取出编译器生成的 CUDA 源码，用 `get_profiler().do_bench()` 测量 kernel 的运行延迟。

本讲不要求你懂编译原理，只要求你已经读过 [u1-l1 项目概览](./u1-l1-project-overview.md) 与 [u1-l3 仓库目录结构](./u1-l3-repo-layout.md)，知道 TileLang 是「写规格、生成代码」的 DSL，并且 `tilelang/` 是 Python 前端、`src/` 是 C++ 编译核心。

## 2. 前置知识

在动手之前，先用三句话建立直觉。

**第一，什么是 GEMM。** GEMM（General Matrix Multiplication）即通用矩阵乘，记作 \( C = A \times B \)。若 \( A \) 形状为 \( (M, K) \)、\( B \) 形状为 \( (K, N) \)，则 \( C \) 形状为 \( (M, N) \)，其中每个元素为：

\[
C_{i,j} = \sum_{p=0}^{K-1} A_{i,p}\, B_{p,j}
\]

深度学习里几乎所有的「大算子」（注意力、卷积的 im2col 形式、线性层）最后都会落到 GEMM 上，所以把 GEMM 跑通是 GPU kernel 学习的「Hello World」。

**第二，为什么要「分块（tile）」。** GPU 的显存（global memory）很大但很慢；片上的共享内存（shared memory）很小但很快；寄存器（fragment/local）最快但更小。一次性把整个 \( A \)、\( B \) 装进共享内存不现实，所以我们把矩阵切成小块（tile）：每次把一小块 \( A \)（`block_M × block_K`）和一小块 \( B \)（`block_K × block_N`）搬进共享内存，做一次小矩阵乘累加到寄存器里的 `C_local`，循环遍历 K 维就得到完整结果。这就是 TileLang 名字里「Tile」的含义。

**第三，TileLang 写的是「规格」而非「实现」。** 你只描述「把哪块数据搬进来、做一次 tile 矩阵乘、再搬出去」，至于具体生成 `cp.async`、`TMA`、`mma` 还是 `wgmma` 指令，由编译器按 target 自动选择（详见 [u4-l2](./u4-l2-tileop-and-gemm-dispatch.md)）。所以本讲的代码很短，但它生成的 CUDA 源码会非常长。

> 名词速查：`grid` = 线程块网格，`block` = 一个线程块，`thread` = 线程；`shared` = 共享内存；`fragment`/`local` = 寄存器片段；`warp` = 一组同步执行的线程（CUDA 为 32，MACA 为 64，见 [u1-l3](./u1-l3-repo-layout.md)）。

## 3. 本讲源码地图

本讲围绕下面几个文件展开，按「先看示例、再追实现」的顺序：

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| [examples/quickstart.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/quickstart.py) | 官方快速上手脚本，一个带 ReLU 的 GEMM | 主示例，逐行讲解 |
| [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py) | 更精简的纯 GEMM 示例 | 综合实践的修改对象 |
| [examples/gemm/README.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/README.md) | GEMM 示例的文字讲解 | 概念佐证 |
| [tilelang/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py) | 顶层包，导出 `jit/compile/JITKernel/Profiler` | 确认「这些名字从哪来」 |
| [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py) | `jit` 装饰器、`compile`、`par_compile` | 模块 4.1 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py) | `JITKernel` 类（可运行对象） | 模块 4.1 / 4.4 |
| [tilelang/language/kernel.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py) | `T.Kernel` 启动上下文 | 模块 4.2 |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) | `T.copy` | 模块 4.3 |
| [tilelang/language/gemm_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py) | `T.gemm` | 模块 4.3 |
| [tilelang/profiler/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py) 与 [bench.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py) | `Profiler` 与 `do_bench` | 模块 4.4 |

## 4. 核心概念与源码讲解

先把本讲的「主角」——quickstart.py 的 kernel 主体——贴出来作为参照，后面四个最小模块都围着它转：

```python
@tilelang.jit
def matmul(A, B, block_M: int, block_N: int, block_K: int):
    M, N, K = T.const("M, N, K")
    dtype = T.float16
    accum_dtype = T.float32
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)

    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_K, block_N), dtype)
        C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)
        T.clear(C_local)
        for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by * block_M, ko * block_K], A_shared)
            T.copy(B[ko * block_K, bx * block_N], B_shared)
            T.gemm(A_shared, B_shared, C_local)
        T.copy(C_local, C[by * block_M, bx * block_N])
    return C
```

这段代码出自 [examples/quickstart.py:8-48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/quickstart.py#L8-L48)。下面我们逐块拆开。

---

### 4.1 jit：把 Python 函数变成可编译的 kernel

#### 4.1.1 概念说明

`@tilelang.jit` 是一个**装饰器**：你写一个普通 Python 函数，它把这个函数包成一个 `JITImpl` 对象。被装饰的函数本身**不会立即执行任何 GPU 代码**——它只是一个「kernel 规格」。真正生成 GPU 代码的动作，发生在你调用 `.compile(...)`（拿到一个可运行对象）或直接带张量参数调用它（立即编译并执行）的时候。

TileLang 的 JIT 支持两种风格（由装饰器自动推断，无需你声明）：

- **lazy 模式**：函数内部用 `@T.prim_func` 显式定义并 `return` 一个 PrimFunc，调用装饰后的函数返回编译好的 kernel 对象，你自己决定何时运行。
- **eager（构建器）模式**：函数直接用 `T.Tensor` 类型注解声明输入、用 `T.empty` 声明输出、用 `with T.Kernel(...)` 写 body，函数 `return` 一个张量。本讲的两个示例（quickstart.py、example_gemm.py）都是 eager 模式。

两种风格共享同一套底层：`compile()` → `JITKernel`。本讲聚焦 eager 模式，因为它最短、最适合入门。

#### 4.1.2 核心流程

把 `@tilelang.jit` + `.compile()` 的生命周期画成流程：

```text
@tilelang.jit 装饰 matmul
        │  得到 JITImpl 对象（func_source、signature、mode="auto" 都已记录）
        ▼
matmul.compile(M=..., block_M=...)        # 用户传入符号维的具体值
        │
        │  JITImpl.compile() 内部：
        │    1) get_tir(...)  —— 用具体参数「实例化」函数，构建出 TIR PrimFunc
        │    2) compile(prim_func, target=..., ...)  —— 进 cached() 真正编译
        ▼
JITKernel                                  # 可运行对象
   ├── artifact   (CompiledArtifact: rt_mod / params / kernel_source)
   ├── adapter    (按 execution_backend 选择 TVMFFI/Cython/NVRTC/...)
   └── torch_function  (一个像 PyTorch 函数一样可直接 (a, b) 调用的可调用对象)
        ▼
kernel(a, b)  →  c   # 直接喂 torch 张量，拿到结果张量
```

关键点：**「实例化」这一步用具体维数把符号 M/N/K 代入**。函数里的 `M, N, K = T.const("M, N, K")` 声明了符号维（详见 [u2-l1 类型系统](./u2-l1-prim-func-and-type-system.md)），它们在 `.compile(M=1024, N=1024, K=1024, ...)` 时才被赋成具体整数，编译器由此才能算出 grid 大小、shared memory 用量。

#### 4.1.3 源码精读

**(1) 装饰器入口**。`jit` 在 [tilelang/jit/\_\_init\_\_.py:564-628](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L564-L628) 定义。它支持「裸用」`@tilelang.jit`（quickstart.py 的写法）和「带参」`@tilelang.jit(target="cuda", out_idx=[...])` 两种形式。核心是内部 `decorator(func)`，它用 `prim_func(func, eager_jit=True)` 把原函数包成 `JITFunc`，再返回一个 `JITImpl` 实例：

```python
def decorator(func: Callable[_P, _T]):
    mode = "auto"
    pf: JITFunc[_P, _T] = prim_func(func, eager_jit=True)
    func_source = inspect.getsource(pf.orig_func)
    signature = inspect.signature(pf.orig_func)
    return JITImpl(func=pf, **compile_args, func_source=func_source, signature=signature, mode=mode)
return decorator(func) if func is not None else decorator
```

> 见 [tilelang/jit/\_\_init\_\_.py:614-628](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L614-L628)。注意 `mode="auto"`：eager 还是 lazy 要等第一次调用时由 `_infer_jit_mode` 推断。

**(2) `.compile()` 做了什么**。`JITImpl.compile` 在 [tilelang/jit/\_\_init\_\_.py:442-473](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L442-L473)。它先 `self.get_tir(*args, **kwargs)` 实例化出 PrimFunc，再调用模块级的 `compile(prim_func, ...)`：

```python
def compile(self, *args, **kwargs) -> _Ret:
    prim_func = self.get_tir(*args, **kwargs)
    kernel_result = compile(
        prim_func,
        out_idx=self.out_idx,
        execution_backend=self.execution_backend,
        target=self.target,
        ...
    )
    return kernel_result
```

模块级 `compile` 在 [tilelang/jit/\_\_init\_\_.py:91-170](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L91-L170)，它把 PrimFunc 交给 `cached(...)`（带磁盘缓存）真正编译，最终返回一个 `JITKernel`。

**(3) `JITKernel` 是最终的可运行对象**。它在 [tilelang/jit/kernel.py:39](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L39) 定义，构造时调用 `_compile_and_create_adapter`（[tilelang/jit/kernel.py:204](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L204)）执行真正的 `tilelang.lower(...)`：

```python
with (jit_phase("lower", ...), tvm.transform.PassContext(opt_level=3, ...), self.target):
    artifact = tilelang.lower(
        tilelang_func, target=target, target_host=target_host,
        enable_host_codegen=enable_host_codegen,
        enable_device_compile=enable_device_compile,
    )
```

> 见 [tilelang/jit/kernel.py:253-264](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L253-L264)。`lower` 是「下译」主入口，后续 [u4-l1 lowering 流程](./u4-l1-lowering-pipeline.md) 会专门讲它；本讲只需知道它把 PrimFunc 变成了可加载的 runtime module + 源码。

**(4) 运行 kernel**。`JITKernel.__call__` 极其简单，就是把参数转交给内部的可调用对象（[tilelang/jit/kernel.py:186-202](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L186-L202)）：

```python
def __call__(self, *args, **kwds) -> _T:
    return self.torch_function(*args, **kwds)
```

这就是为什么 quickstart.py 里 `c = matmul_relu_kernel(a, b)` 能像普通函数一样调用，且参数和返回值都是 torch 张量。

**(5) 这些名字从哪来**。顶层 [tilelang/\_\_init\_\_.py:186-192](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/__init__.py#L186-L192) 把 `jit / JITKernel / compile / par_compile` 和 `Profiler / TensorSupplyType` 全部导出，所以你只需 `import tilelang` 就能用。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 quickstart.py，确认 `.compile()` 返回的是一个 `JITKernel`，并理解「实例化」发生在何时。

**操作步骤**：

1. 确认已按 [u1-l2 环境搭建](./u1-l2-build-and-install.md) 装好 tilelang-metax 与 torch，且 `import tilelang` 不报错。
2. 在 Python 交互环境里，只执行到「编译」这一步，先不喂张量：

   ```python
   import tilelang, tilelang.language as T   # 示例代码片段，非项目原文件
   from examples.quickstart import matmul    # 导入被 @tilelang.jit 装饰的函数
   print(type(matmul))                       # 期望: JITImpl
   k = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
   print(type(k))                            # 期望: tilelang.jit.kernel.JITKernel
   ```

3. 再执行 `k.get_kernel_source()`（4.4 会用到），确认它返回一段很长的 CUDA 字符串。

**需要观察的现象**：

- `type(matmul)` 是 `JITImpl`，不是普通 `function`——说明装饰器已经接管。
- 在 `.compile(...)` 之前，没有任何 GPU 代码被生成；调用 `.compile(...)` 时终端会打印类似 `TileLang begins to compile kernel ...` 的日志（见 [tilelang/jit/kernel.py:125-129](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L125-L129)）。

**预期结果**：`type(k)` 为 `JITKernel`；`k.get_kernel_source()` 是非空字符串。

> 若无可用 GPU：可设 `target={"kind":"llvm"}` 或在仅阅读源码时跳过实际运行，结论标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@tilelang.jit` 去掉，直接 `matmul.compile(...)` 会怎样？
**答**：`matmul` 变回普通 Python 函数，没有 `.compile` 方法，会抛 `AttributeError`。`@tilelang.jit` 的职责就是把函数升级成带 `.compile / __call__` 的 `JITImpl`。

**练习 2**：同一个被装饰的 `matmul`，用不同的 `block_M` 连续调用两次 `.compile(...)`，会编译几次？
**答**：会编译两次——不同的符号参数实例化出不同的 PrimFunc，是两个不同的 kernel。`JITImpl` 内部还有按参数元组键控的 `_kernel_cache`（[tilelang/jit/\_\_init\_\_.py:495-540](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/__init__.py#L495-L540)），所以**相同参数**的二次调用会命中缓存、不会重编译。

---

### 4.2 T.Kernel：启动上下文与线程模型

#### 4.2.1 概念说明

`with T.Kernel(*grid, threads=...) as (bx, by):` 这一行定义了 kernel 的**启动上下文**：告诉编译器「这个 kernel 要开多少个线程块、每个块多少线程」，并给你两个绑定变量 `bx`、`by`（即 `blockIdx.x`、`blockIdx.y`）用来区分不同的线程块。

你可以把它类比成 CUDA 里的 `<<<grid, block>>>` 启动配置，只不过 TileLang 把它写进了 kernel 内部、由 `MaterializeKernelLaunch` pass（见 [u5-l2](./u5-l2-transform-passes.md)）在编译期物化成真正的启动代码。它**与 target 无关**——同一份 kernel 可以编译到 cuda/hip/maca/llvm 任何一个 target，CPU 后端会忽略线程维度（见 `Kernel` 的文档字符串 [tilelang/language/kernel.py:265-269](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L265-L269)）。

#### 4.2.2 核心流程

quickstart.py 的启动行是：

```python
with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
```

它的含义：

- 网格两个维度分别是 `ceildiv(N, block_N)` 与 `ceildiv(M, block_M)`，即「输出矩阵 C 一共要切成多少块」。`T.ceildiv(a, b)` 是向上取整 \( \lceil a/b \rceil \)。
- 每个线程块负责 C 中一块 `block_M × block_N` 的子矩阵。具体地，块 `(bx, by)` 负责的行范围是 `[by*block_M, (by+1)*block_M)`、列范围是 `[bx*block_N, (bx+1)*block_N)`。
- `threads=128`：每个块 128 个线程。这也是 `T.copy`、`T.gemm` 这些 tile 原语内部用来并行展开循环的线程数。

把它画成数据流：

```text
grid = ( ceildiv(N, block_N) , ceildiv(M, block_M) )
          └── bx ──┘             └── by ──┘
块 (bx, by) 计算:  C[ by*block_M:(by+1)*block_M ,  bx*block_N:(bx+1)*block_N ]
```

每个块独立计算自己的输出块，块之间没有依赖——这正是 GEMM 可以高度并行的原因。

#### 4.2.3 源码精读

**(1) `Kernel` 函数本体**在 [tilelang/language/kernel.py:258-321](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L258-L321)。两个要点：

```python
def Kernel(*blocks, threads=None, prelude=None):
    ...
    if Builder.current() is None:
        raise JITNoBuilderError("T.Kernel() can only be used inside @tilelang.jit or @T.prim_func context.")
    attrs: dict = {}
    threads = _normalize_threads(threads)
    ...
    return _ffi_api.KernelLaunch(blocks, threads, attrs)
```

- 它先检查「当前是否有 Builder」——这就是为什么 `T.Kernel` 必须写在被 `@tilelang.jit` 或 `@T.prim_func` 装饰的函数里（[tilelang/language/kernel.py:312-313](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L312-L313)）。
- `_normalize_threads` 把 `threads=128` 归一化成三维 `[128, 1, 1]`；若不传，**默认就是 128**（[tilelang/language/kernel.py:98-111](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L98-L111)）。quickstart.py 写的 `threads=128` 其实就是默认值。

**(2) `with ... as (bx, by)` 的绑定**来自 `KernelLaunchFrame.__enter__`（[tilelang/language/kernel.py:137-151](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L137-L151)）。它把「前几个网格维度」对应的循环变量作为 `bx/by/bz` 返回给用户，把「后三维」留给 `threadIdx.xyz`：

```python
# 返回 grid 循环变量（去掉末尾 4 个 frame: threadIdx.x/y/z + 带 attr 的 block frame）
return _normalize_bindings([frame.vars[0] for frame in self.frames[0:-4]])
```

> 见 [tilelang/language/kernel.py:149-151](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L149-L151)。`KernelLaunchFrame` 是一个自定义的 TIRFrame，用栈管理进入/退出（[tilelang/language/kernel.py:130-161](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L130-L161)）。

**(3) 单维度的便利写法**。`_normalize_bindings`（[tilelang/language/kernel.py:87-95](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L87-L95)）保证：只有 1 个网格维度时，`as bx` 和 `as (bx,)` 两种写法都可以。

#### 4.2.4 代码实践

**实践目标**：直观感受 `grid` 与 `block_M/block_N` 的关系，确认每个块算的是 C 的哪一块。

**操作步骤**：

1. 取 M=N=1024、block_M=block_N=128。手动算 `ceildiv(1024,128)` = 8，所以 grid 是 `(8, 8)`，共 64 个块。
2. 在 quickstart.py 的 `with T.Kernel(...)` 之后、`T.clear` 之前，**临时**加一行调试打印（仅用于理解，理解后请还原，勿提交）：

   ```python
   # 示例代码片段，仅用于理解，非项目原有代码
   T.print(bx, by)   # 运行时会在每个块打印它的块号
   ```

   `T.print` 的用法见 [u9-l3 调试工具](./u9-l3-debug-tools.md)。

**需要观察的现象**：kernel 启动后，`(bx, by)` 会遍历 `(0,0)..(7,7)` 的组合。

**预期结果**：共 64 组 `(bx, by)` 输出；与 `8 × 8` 一致。

> 若无 GPU：可读 `get_kernel_source()` 生成的 CUDA，找到形如 `blockIdx.x / blockIdx.y` 的使用点，确认每个块只读写自己负责的 C 子矩阵。结论可标「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把 `block_N` 从 128 改成 64（M=N=1024 不变），grid 会变成多少？
**答**：x 维 `ceildiv(1024,64)=16`，y 维仍 `ceildiv(1024,128)=8`，grid = `(16, 8)`，共 128 个块；每块算更窄（64 列）的输出。

**练习 2**：为什么 `threads=128` 是 tile 原语展开循环的依据？
**答**：因为 `T.copy`/`T.gemm` 内部要把一个 tile 的工作量分配给块内所有线程并行执行；`KernelLaunchFrame` 把 `threads` 记录为 `threadIdx` 维度（[tilelang/language/kernel.py:186-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L186-L198)），编译器据此把 tile 循环映射到 128 个线程上。

---

### 4.3 T.copy 与 T.gemm：数据搬运与分块矩阵乘

#### 4.3.1 概念说明

`T.Kernel` 之内，主角是三组动作：**搬进来、算、搬出去**。前两步分别由 `T.copy` 和 `T.gemm` 这两条 tile 原语承担。

- **`T.copy(src, dst)`**：在内存区域之间搬运数据。它不是「朴素 memcpy」，而是会被编译器降低成高效的批量搬运指令——在 NVIDIA 上可能是 `cp.async` 或 TMA，在 Metax/MACA 上是 `memcpy_async`（见 [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) 里的 `maca_async_copy`，L496）。它会自动把搬运工作并行分配给块内所有线程。
- **`T.gemm(A, B, C)`**：在 shared/fragment buffer 上做一次分块矩阵乘累加 \( C \mathrel{+}= A \times B \)。它会被分派到当前 target 的张量核指令（NVIDIA 的 `mma/wgmma/tcgen05`、AMD 的 `mfma`、MACA 的 `mfma`）。具体分派逻辑在 [u4-l2](./u4-l2-tileop-and-gemm-dispatch.md) 详讲。

要理解它们，还要先认识 GEMM 例子里出现的三个内存分配（定义在 [tilelang/language/allocate.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py)）：

| 原语 | 作用域 | 作用 |
| --- | --- | --- |
| `T.alloc_shared` | `shared.dyn`（共享内存） | 块内所有线程共享，放 A/B 的 tile |
| `T.alloc_fragment` | `local.fragment`（寄存器片段） | 线程私有、由 layout 推断分配，放累加器 C_local |
| `T.alloc_local` | `local`（寄存器） | 线程私有标量/向量 |

定义见 [tilelang/language/allocate.py:34](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py#L34)（`alloc_shared`）、[L66](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py#L66)（`alloc_fragment`）、[L52](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py#L52)（`alloc_local`）。

#### 4.3.2 核心流程

把 quickstart.py 的 kernel body 抽象成「每个线程块 `(bx, by)` 内」的伪代码：

```text
A_shared  = shared[block_M, block_K]        # 给 A 的 tile 预留共享内存
B_shared  = shared[block_K, block_N]        # 给 B 的 tile 预留共享内存
C_local   = fragment[block_M, block_N]      # 给 C 的 tile 预留寄存器累加器
clear(C_local)                              # 累加器清零
for ko in Pipelined(ceildiv(K, block_K), num_stages=3):
    copy( A[by*block_M, ko*block_K : ]  →  A_shared )   # 搬一块 A 进 shared
    copy( B[ko*block_K, bx*block_N : ] →  B_shared )    # 搬一块 B 进 shared
    gemm(A_shared, B_shared, C_local)                   # C_local += A_shared @ B_shared
copy(C_local → C[by*block_M, bx*block_N])               # 把结果搬回 global
```

对应的数学含义：把 K 维拆成长度为 `block_K` 的小段 \( K = k_0 + k_1 + \dots \)，则

\[
C_{i,j} = \sum_{ko} \sum_{p} A_{i,\, ko\cdot block_K + p}\; B_{ko\cdot block_K + p,\, j}
\]

每个 `ko` 迭代完成一次「搬数据 + 一次 tile 矩阵乘累加」。

**关于 `T.Pipelined`**：它把 `ko` 循环变成**软件流水线**——当 `num_stages=3`，编译器会维护多份 shared buffer，让第 `ko` 次的「计算」与第 `ko+1`、`ko+2` 次的「搬运」重叠起来，隐藏访存延迟。`num_stages` 表示流水线深度（最多同时存在几个 stage 的 buffer）。它定义在 [tilelang/language/loop.py:112-191](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L112-L191)，背后的 pass 在 [u4-l4 软件流水线](./u4-l4-software-pipeline.md) 详讲。

**关于 `T.copy` 的索引写法**：`T.copy(A[by * block_M, ko * block_K], A_shared)` 里 `A[by*block_M, ko*block_K]` 是「以这个起点取一个与 `A_shared` 等大的子区域」的语法糖。`copy` 会自动推导两边形状并匹配（[tilelang/language/copy_op.py:16-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L16-L50) 的 `_normalize_copy_regions`）。

#### 4.3.3 源码精读

**(1) `T.copy` 的本体**在 [tilelang/language/copy_op.py:53-133](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L53-L133)。核心逻辑：先 `_normalize_copy_regions` 把 `src/dst` 归一化成带 region 的可推导形式，再发射一个 `tl.tileop.copy` intrinsic：

```python
def copy(src, dst, *, coalesced_width=None, disable_tma=False,
         eviction_policy=None, prefer_instruction=None, ...):
    src, dst = _normalize_copy_regions(src, dst)
    ...
    return tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.copy"), src, dst, annotations=ann if ann else None)
```

> 见 [tilelang/language/copy_op.py:109-133](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L109-L133)。注意可选参数 `disable_tma`、`prefer_instruction="tma"/"cp_async"` 等——它们是让你微调搬运指令的旋钮，初学时不用管，编译器会自动选。

`tl.tileop.copy` 是一个**注册到 TVM Op 系统的 tile 算子**，真正的「生成并行 copy 循环 / 选 TMA / 选 cp.async」发生在 C++ 侧的 `Lower()`（见 [u9-l2 新增 tile 算子](./u9-l2-add-new-tile-op.md)）。所以 Python 这一层只负责「描述意图 + 形状校验」。

**(2) `T.gemm` 的本体**在 [tilelang/language/gemm_op.py:149-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L149-L198)。它只是 `_gemm_impl` 的薄封装，发射 `tl.tileop.gemm`：

```python
def gemm(A, B, C, transpose_A=False, transpose_B=False,
         policy=GemmWarpPolicy.Square, clear_accum=False, k_pack=1, mbar=None):
    return _gemm_impl("tl.tileop.gemm", A, B, C, transpose_A, transpose_B,
                      policy, clear_accum, k_pack, 0, mbar)
```

`_gemm_impl`（[tilelang/language/gemm_op.py:22-146](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L22-L146)）做了大量形状/stride/offset 校验，例如断言 C 是 2D、A 与 B 的 K 维一致、M 维与 C 的 M 一致等：

```python
assert len(C_shape) == 2, "current only support C as a 2D tensor"
...
assert prim_expr_equal(K, K_B), f"T.gemm K shape check failed: K_A = {K}, K_B = {K_B}"
```

> 见 [tilelang/language/gemm_op.py:71-97](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L71-L97)。这些 assert 就是你写 GEMM 时「形状对不上」报错的来源。`policy=GemmWarpPolicy.Square` 控制 warp 如何瓜分输出 tile，详情见 [u4-l2](./u4-l2-tileop-and-gemm-dispatch.md)。

**(3) 「默认同步」语义**。`T.gemm` 的文档明确说：在 Hopper 上若编译器选了 WGMMA，它会**自动插入 wait**；在 Blackwell TCGEN5MMA 上会自动插 `mbarrier_wait_parity`（[tilelang/language/gemm_op.py:161-169](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L161-L169)）。也就是说，初学者用 `T.gemm` 不需要手动同步；想要手控异步才用 `T.wgmma_gemm`/`T.tcgen05_gemm`。

#### 4.3.4 代码实践

**实践目标**：通过「故意写错形状」感受 `T.gemm` 的校验，并对比 `T.copy` 在 global↔shared 与 fragment↔global 两种方向上的用法。

**操作步骤**：

1. 复制 quickstart.py 的 `matmul`，把 `T.gemm(A_shared, B_shared, C_local)` 改成 `T.gemm(A_shared, B_shared, A_shared)`（故意让 C 端形状/作用域不对）。
2. 重新 `matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)`。

**需要观察的现象**：编译期就会抛出 assert 错误（来自上面引用的形状校验），而非运行期出错。

**预期结果**：报错信息会指向 `_gemm_impl` 里的某条 `assert`，提示 C 必须是 2D / 形状不匹配。把这行改回 `C_local` 后恢复正常。

> 这是「源码阅读型实践」：你无需 GPU 也能完成，错误发生在 `.compile()` 阶段。具体报错文本「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：quickstart.py 里 `T.copy(A[by*block_M, ko*block_K], A_shared)` 为什么不需要写两层循环？
**答**：因为 `T.copy` 是 tile 原语，内部会自动按 `A_shared` 的形状（`block_M × block_K`）推导搬运范围，并把元素级搬运并行分配给块内 128 个线程。手写两层 `T.Parallel` 循环是更底层的写法（见 README 的「Parallel Copy」示例 [examples/gemm/README.md:205-211](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/README.md#L205-L211)）。

**练习 2**：`C_local` 是 fragment、`C` 是 global。最后一步 `T.copy(C_local, C[...])` 是哪种方向的搬运？为什么要单独的 `accum_dtype=float32`？
**答**：方向是 fragment（寄存器）→ global（显存）。用 `float32` 累加是为了减少 K 维求和时的数值误差；输出再以 `dtype=float16` 写回 C，等价于「高精度累加、低精度存储」。

---

### 4.4 profiler：取出源码与测量延迟

#### 4.4.1 概念说明

写完 kernel，你通常要做两件事：**(a) 看编译器到底生成了什么代码**；**(b) 量它跑得多快**。TileLang 把这两个能力直接挂在 `JITKernel` 上：

- `kernel.get_kernel_source()`：返回生成的设备端源码（CUDA/HIP/MACA 字符串）。
- `kernel.get_profiler()`：返回一个 `Profiler` 对象，其 `do_bench()` 用带 L2 cache 控制的精确计时测量延迟（单位 ms）。

quickstart.py 同时演示了这两件事（[examples/quickstart.py:79-87](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/quickstart.py#L79-L87)）：

```python
cuda_source = matmul_relu_kernel.get_kernel_source()
profiler = matmul_relu_kernel.get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Normal)
latency = profiler.do_bench()
```

#### 4.4.2 核心流程

`do_bench` 的测量思路（定义在 [tilelang/profiler/bench.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py)）：

```text
1. 先跑一次 fn() 做初始化 + synchronize
2. 分配一块 ~256MB 的 cache buffer，用来每次测量前「冲刷」L2 cache
3. 用 5 次迭代估计单次耗时 estimate_ms
4. 由 warmup(25ms)/estimate 与 rep(100ms)/estimate 自动算出 warmup 次数与计时次数
5. 进入计时阶段，按 backend 选择计时方式：
     - "event"     ：CUDA Event 计时（默认）
     - "cupti"     ：torch.profiler (CUPTI) 计时，会扣除 cache 冲刷自身耗时
     - "cudagraph" ：CUDA Graph 重放，最小化 launch 开销
6. 每次计时前先 cache.zero_() 冲 L2，再 fn()，记录起止时间
7. 按 return_mode(mean/median/min/max) 聚合，返回 ms
```

这样得到的延迟**排除了 L2 cache 命中带来的虚高**，更接近真实端到端性能。

#### 4.4.3 源码精读

**(1) `JITKernel.get_kernel_source`**：[tilelang/jit/kernel.py:466-477](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L466-L477)。对于 `tvm_ffi/cython/nvrtc/cutedsl` 后端，从 adapter 取；否则从 `artifact.kernel_source` 取。

**(2) `JITKernel.get_profiler`**：[tilelang/jit/kernel.py:450-464](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L450-L464)。它构造一个 `Profiler` 并用 `.with_default_adapter(self.adapter)` 把 kernel 的 adapter 接上：

```python
def get_profiler(self, tensor_supply_type=TensorSupplyType.Auto) -> Profiler:
    return Profiler(self.params, self.out_idx, tensor_supply_type).with_default_adapter(self.adapter)
```

`Profiler` 是 dataclass（[tilelang/profiler/\_\_init\_\_.py:21-35](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L21-L35)），`tensor_supply_type` 决定它自动生成的输入张量是随机、全零还是其它（`TensorSupplyType`）。`Auto` 会按 dtype 选合适的供给。

**(3) `Profiler.do_bench`**：[tilelang/profiler/\_\_init\_\_.py:220-281](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L220-L281)。它把 kernel 包成无参函数，转交给模块级 `do_bench`：

```python
bench_func = partial(bench_target, *ins)   # 把输入张量预先固定好
return do_bench(bench_func, warmup=warmup, rep=rep, _n_warmup=n_warmup,
                _n_repeat=n_repeat, quantiles=quantiles, backend=backend,
                return_mode=return_mode, device=device)
```

**(4) 模块级 `do_bench` 的 L2 控制与多后端**：[tilelang/profiler/bench.py:65-135](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L65-L135)。其中 cache buffer 的分配在 [bench.py:185-188](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L185-L188)：

```python
cache_bytes = cache_size * 1024 * 1024     # 默认 256MB
cache_numel = cache_bytes // 4 if fast_flush else cache_bytes
cache = torch.empty(cache_numel, dtype=cache_dtype, device=...)
```

三个后端实现分别是 `_bench_with_cuda_events`（[bench.py:221](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L221)）、`_bench_with_cupti`（[bench.py:257](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L257)）、`_bench_with_cudagraph`（[bench.py:302](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L302)）。`cupti` 后端会专门扣除 cache 冲刷自身的耗时（[bench.py:281-299](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L281-L299)），因此最干净；example_gemm.py 用的就是它（[examples/gemm/example_gemm.py:55](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L55)）。

#### 4.4.4 代码实践

**实践目标**：测出 quickstart.py 这个 GEMM+ReLU kernel 的延迟，并把它换算成 TFLOPS，建立「延迟→算力」的直觉。

**操作步骤**：

1. 直接运行 quickstart.py 全文：

   ```bash
   python examples/quickstart.py
   ```

   它会在 [examples/quickstart.py:85-87](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/quickstart.py#L85-L87) 打印 `Latency: ... ms`。
2. 用延迟算 TFLOPS。对 \( M=N=K=1024 \)、float16，GEMM 的浮点运算量是 \( 2MNK \)（乘加各一次）：

   \[
   \text{TFLOPS} = \frac{2 \times 1024^3}{\text{latency(ms)} \times 10^{9}}
   \]

3. 再试一次 `backend="cupti"`（仿照 [examples/gemm/example_gemm.py:55](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L55) 的写法），对比与默认 `event` 的差异。

**需要观察的现象**：终端先打印数值校验通过（`Kernel output matches PyTorch reference.`），再打印一大段生成的 CUDA 源码，最后打印延迟。

**预期结果**：得到一个量级合理的延迟（1024³ 的 fp16 GEMM 在现代 GPU 上通常远低于 1ms），换算后 TFLOPS 处于该卡的理论峰值的一个合理比例。

> 精确数值取决于具体 GPU 与 target，「待本地验证」。无 GPU 时可只读 `get_kernel_source()` 的输出，结合源码理解 `_bench_with_cupti` 的计时逻辑。

#### 4.4.5 小练习与答案

**练习 1**：`do_bench()` 为什么每次测量前都要 `cache.zero_()`？
**答**：为了冲刷 L2 cache，避免上一轮的数据残留在 cache 里让本轮访存「假快」。`do_bench` 分配了一块约 256MB 的 buffer 专门干这件事（[bench.py:185-188](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L185-L188)）。

**练习 2**：`get_profiler(tensor_supply_type=...)` 不传输入张量，profiler 拿什么数据测？
**答**：`Profiler` 会按 `tensor_supply_type` 自动生成输入（[tilelang/profiler/\_\_init\_\_.py:62-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L62-L70) 的 `_get_inputs`）。所以测延迟时不必自己造 torch 张量；但要校验数值正确性时，建议像 quickstart.py 那样自己造张量并和 `torch.relu(a@b)` 比对。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「调参 + 观察源码 + 测延迟」的小任务。对象是更精简的 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)（不带 ReLU，逻辑更干净，主体见 [example_gemm.py:5-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L5-L26)）。

**任务**：固定 \( M=N=K=1024 \)，依次用下面三组 tile 参数编译并运行，记录每组「生成的 kernel 源码长度」与「延迟」：

| 组 | block_M | block_N | block_K |
| --- | --- | --- | --- |
| A（基线） | 128 | 128 | 32 |
| B | 64 | 64 | 32 |
| C | 128 | 128 | 64 |

**操作步骤**：

1. 把 [example_gemm.py:30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L30) 的 `matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)` 复制三份，分别换成 A/B/C 的参数。
2. 对每个 kernel 调 `get_kernel_source()`，用 `len(src)` 量源码长度；调 `get_profiler().do_bench(backend="cupti")` 拿延迟。
3. 顺便检查每组是否都通过数值校验（`torch.testing.assert_close`，[example_gemm.py:46](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L46)）。

**需要观察与思考的现象**：

- 三组都应通过数值校验——因为 tile 参数只影响「怎么算」，不影响「算什么」。
- block 越大，单个线程块要做的工作越多、grid 越小、kernel 源码里循环展开的形态会变化；延迟会随参数变化，且存在一个较优区间。
- C 组 `block_K=64` 会让每次循环搬更大的 K 切片，shared memory 占用翻倍；若超出硬件 shared memory 上限，编译会失败——这正好让你体会 tile 参数的「硬件约束」。

**预期结果**：得到一张「参数 → 源码长度 → 延迟」的小表。延迟最优的那组，就是你在这张卡上的「较优 tile 配置」之一（系统化搜优在 [u8-l1 autotuner](./u8-l1-autotuner.md)）。

> 精确延迟「待本地验证」。若无法运行，至少把三组对应的 `get_kernel_source()` 各读一遍，找出 grid 维度、shared memory 分配、`for` 循环次数的差异。

## 6. 本讲小结

- `@tilelang.jit` 把 Python 函数包成 `JITImpl`；调用 `.compile(M=..., block_M=...)` 用具体维数实例化、下译，得到可运行对象 `JITKernel`。运行只需 `kernel(a, b)`。
- `with T.Kernel(*grid, threads=128) as (bx, by):` 定义启动上下文：grid 决定开多少块、每块算 C 的哪一块；`threads` 是 tile 原语并行展开的线程数，默认 128。
- GEMM 的 body 就是「搬进来 → 算 → 搬出去」：`T.alloc_shared/alloc_fragment` 分层分配显存，`T.copy` 在 global/shared/fragment 间搬运（自动并行、自动选 cp.async/TMA），`T.gemm` 做分块矩阵乘累加（自动分派到 mma/wgmma/mfma 等张量核指令）。
- `T.Pipelined(num_stages=3)` 把 K 维循环变成软件流水线，重叠搬运与计算以隐藏访存延迟。
- `kernel.get_kernel_source()` 取出生成的设备源码；`kernel.get_profiler().do_bench()` 用带 L2 冲刷的精确计时（event/cupti/cudagraph 三后端）测延迟。

## 7. 下一步学习建议

本讲你跑通了第一个 kernel，但很多「为什么」被刻意略过了。建议接下来：

1. [u2-l1 kernel 定义、prim_func 与类型系统](./u2-l1-prim-func-and-type-system.md)：搞懂 `T.const("M, N, K")`、`T.Tensor`、`T.empty` 这些类型与符号维的细节。
2. [u2-l2 T.Kernel 启动上下文与线程模型](./u2-l2-kernel-launch-context.md)：更系统地理解 grid/threads/block 绑定与 `ClusterKernel`。
3. [u2-l4 内存层级与显存分配](./u2-l4-memory-hierarchy.md)：深入 shared/fragment/local 的差别与 layout 推断。
4. [u3-l2 JIT 编译与 kernel 对象](./u3-l2-jit-and-kernel-object.md)：把本讲的 `JITKernel` 全部方法（`get_kernel_source / get_profiler / export_sources / par_compile`）讲透。
5. 若你更关心 Metax/MACA 后端，可跳到 [u3-l3 在 Metax GPU 上运行](./u3-l3-running-on-metax-maca.md)，把这里的 target 换成 `{"kind":"maca"}` 再跑一遍。
