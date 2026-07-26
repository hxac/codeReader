# 第一个 Kernel：Quickstart GEMM 实跑

## 1. 本讲目标

本讲是 tilelang 的「Hello World」。读完本讲，你应当能够：

- 用 `@tilelang.jit` 写出一个可运行的 GEMM（矩阵乘）kernel；
- 看懂 `with T.Kernel(...)`、`T.alloc_shared / T.alloc_fragment`、`T.copy`、`T.gemm`、`T.Pipelined` 这些 DSL 原语的直观含义；
- 把 Python 函数「编译」成一个可调用对象，传入 PyTorch tensor 跑出结果；
- 用 `kernel.get_kernel_source()` 查看生成的设备代码（CUDA/HIP/...），用 `kernel.get_profiler().do_bench()` 测出一次延迟。

本讲不要求你已经理解编译器内部原理，只需照着 `examples/quickstart.py` 把整条「写 → 编译 → 调用 → 验证 → 看代码 → 测延迟」链路跑通即可。后续单元（u4 编译流水线、u6 Pass 与代码生成）才会拆开这条链路的黑盒。

## 2. 前置知识

- **矩阵乘（GEMM）**：计算 \(C = A \times B\)，其中 \(A \in \mathbb{R}^{M\times K}\)、\(B \in \mathbb{R}^{K\times N}\)、\(C \in \mathbb{R}^{M\times N}\)。每个 \(C_{i,j} = \sum_{k=0}^{K-1} A_{i,k} B_{k,j}\)。它是大模型里最核心、最吃性能的算子。
- **分块（tile）思想**：整张大矩阵装不进片上高速存储，于是把 \(A\)、\(B\)、\(C\) 切成小块（例如 \(128\times 128\)），每次只把一个块搬进片上存储，算完再搬回。tilelang 的名字正来源于这种「tile 级」编程模型。
- **GPU 三级内存**（直观版）：
  - **global memory**：显存，容量大、带宽相对低，所有线程可见，输入/输出张量就住在这里。
  - **shared memory**：一个线程块（block）内共享的片上高速存储，用于暂存分块数据。
  - **fragment / register**：寄存器级存储，通常是每个线程私有的累加器，速度最快。
- **网格与线程块**：GPU 的一次启动（grid）由许多线程块（block）组成，每个 block 内又包含若干线程（thread）。`T.Kernel(grid_x, grid_y, threads=...)` 就在描述这个结构。
- **PyTorch tensor**：tilelang 默认与 PyTorch 互操作，输入输出用 `torch.Tensor` 表示。

## 3. 本讲源码地图

本讲围绕下面几个文件展开：

| 文件 | 作用 |
| --- | --- |
| [examples/quickstart.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py) | **本讲主线**：最精简的可运行 GEMM 示例，串起定义→编译→调用→验证→看源码→测延迟全链路。 |
| [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py) | README 同款 GEMM 示例，多了一个 ReLU 后处理，演示 `T.Parallel` 元素级循环。 |
| [README.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md) | 项目首页，Quick Start 段落给出带注释的进阶 GEMM（含 swizzle 等）。 |
| [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py) | `@tilelang.jit` 装饰器、`compile()` 与 `JITImpl`（懒/急两种模式的总调度）。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py) | `JITKernel`：编译产物 + adapter 的封装，提供 `get_kernel_source()` / `get_profiler()` / `__call__`。 |
| [tilelang/profiler/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py) | `Profiler` 与 `do_bench()`，负责自动喂输入、跑 warmup、测延迟。 |
| [tilelang/language/](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/) | DSL 原语实现目录（`kernel.py`/`allocate.py`/`copy_op.py`/`gemm_op.py`/`loop.py` 等），本讲会逐一引用。 |

> 约定：本讲所有形如「`T.xxx`」的写法，前提是已经 `import tilelang.language as T`。

## 4. 核心概念与源码讲解

本讲把全链路拆成 4 个最小模块：

1. **tilelang.jit**：从 Python 函数到可调用 kernel；
2. **tilelang.language（上）**：`T.Kernel` 上下文、三级内存与数据搬运；
3. **tilelang.language（下）**：`T.gemm` 分块矩阵乘与 `T.Pipelined` 软件流水线；
4. **tilelang.profiler**：查看生成的设备代码与测量延迟。

### 4.1 模块一：tilelang.jit —— 从 Python 函数到可调用 kernel

#### 4.1.1 概念说明

`@tilelang.jit` 是用户接触 tilelang 的第一个 API。它把一个普通 Python 函数包装成一个「可编译、可调用」的对象 `JITImpl`。被装饰的函数体里写的不是真正的 Python 运行逻辑，而是用 DSL（`T.Kernel`、`T.copy`、`T.gemm`……）描述的「kernel 蓝图」。tilelang 会在合适的时候把这份蓝图编译成目标硬件（CUDA/HIP/...）上的真实 kernel。

tilelang 有两种执行模式（本讲的示例主要走显式编译路径，但你应当知道两种都存在）：

- **lazy（懒）模式**：函数体内 `return` 一个 `@T.prim_func` 定义的 IR 函数。调用 `matmul(...)` 返回一个**编译好的 kernel 对象**，需要再单独调用它才会真正算。
- **eager（急）模式**：函数体用 `A: T.Tensor(...)` 注解 + builder 模式书写，并 `return` 一个用 `T.empty` 声明的输出张量。调用 `matmul(a, b)` 会**立刻编译并执行**，直接返回结果张量。

模式由 tilelang 根据函数写法**自动推断**，初学阶段不必显式指定。

#### 4.1.2 核心流程

显式编译路径（本讲示例采用）：

```
@tilelang.jit                       # 1. 装饰：得到 JITImpl 对象 matmul
def matmul(A, B, block_M, ...):     #    函数体是 DSL 蓝图
    ...

kernel = matmul.compile(M=..., ...) # 2. 编译：返回 JITKernel（包含 adapter）
c = kernel(a, b)                    # 3. 调用：传入 torch.Tensor，得到结果
```

`matmul.compile(...)` 的内部链路：`JITImpl.compile()` → 拿到 TIR 函数 → 调 `tilelang.jit.compile()` → 经缓存层 `cached()` → 构造 `JITKernel`。`JITKernel` 在构造时真正调用 `tilelang.lower(...)`（编译器入口，u4 会详讲）把 TIR 编成设备代码，并用一个 **adapter** 把产物包成「能用 PyTorch tensor 调用」的可调用对象。

#### 4.1.3 源码精读

`@tilelang.jit` 装饰器的定义与两个重载：

[tilelang/jit/\_\_init\_\_.py:564-628](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L564-L628) —— `jit()` 装饰器：把被装饰函数经 `prim_func(func, eager_jit=True)` 转成 `JITFunc`，再包成 `JITImpl`，初始 `mode="auto"`（模式待首次调用时推断）。

模式自动推断的核心：

[tilelang/jit/\_\_init\_\_.py:370-383](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L370-L383) —— `_infer_jit_mode()`：若函数直接返回 `PrimFunc` 则为 lazy，否则为 eager。

显式编译入口：

[tilelang/jit/\_\_init\_\_.py:442-473](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L442-L473) —— `JITImpl.compile()`：先 `get_tir(...)` 取得 TIR 函数，再交给模块级 `compile()`，必要时把生成的 kernel 源码与 TIR 脚本写到 `debug_root_path`。

模块级 `compile()` 会合并函数级属性（`out_idx`/`pass_configs`/`compile_flags`）并走缓存：

[tilelang/jit/\_\_init\_\_.py:91-170](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L91-L170) —— `compile()`：断言输入是 `PrimFunc`，合并函数级 attrs，最后 `return cached(func=func, ...)`。

`JITImpl.__call__` 决定「调用时是直接返回 kernel 还是当场执行」：

[tilelang/jit/\_\_init\_\_.py:495-540](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L495-L540) —— `__call__`：eager 模式 `return kernel(*kernel_args.values())`（当场执行返回结果），lazy 模式 `return kernel`（返回 kernel 对象）。

`JITKernel` 的核心：构造时调用 `tilelang.lower(...)` 真正编译，并按 `execution_backend` 选择 adapter：

[tilelang/jit/kernel.py:268-283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L268-L283) —— 在 `tvm.transform.PassContext` 与 `self.target` 作用域内调用 `tilelang.lower(...)`，得到 `artifact`（含 host/device 模块、kernel 源码、参数描述）。

编译好之后，调用 kernel 就是调用 adapter：

[tilelang/jit/kernel.py:188-204](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L188-L204) —— `JITKernel.__call__` 直接转发 `self.torch_function(*args, **kwds)`。

#### 4.1.4 代码实践

**实践目标**：跑通 `examples/quickstart.py` 的编译与调用，确认输出与 PyTorch 参考一致。

**操作步骤**：

1. 确认环境里有 CUDA GPU（无 GPU 见本模块末尾的 CPU 变体）。
2. 执行：
   ```bash
   python examples/quickstart.py
   ```
3. 关注脚本里这几行：
   - [examples/quickstart.py:30](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L30) `kernel = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)` —— 显式编译。
   - [examples/quickstart.py:37](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L37) `c = kernel(a, b)` —— 传 torch tensor 调用。

**需要观察的现象**：脚本会先打印 `c` 与 `ref_c` 两段矩阵，随后打印 `All check passed.`，最后打印一段 CUDA 源码和一行延迟。

**预期结果**：`torch.testing.assert_close(c, ref_c, rtol=1e-2, atol=1e-2)` 通过，说明 tilelang kernel 与 `a @ b` 数值一致（fp16 容差内）。

**CPU 变体（无 GPU 时）**：源码注释明确 target 可为 `"cuda"`/`"hip"`/`"cpu"`（见 [examples/quickstart.py:5-6](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L5-L6)）。把装饰器改为 `@tilelang.jit(target="cpu")`，并把输入改为 CPU tensor：`a = torch.randn(1024, 1024, dtype=torch.float16)`，即可在纯 CPU 上跑通同样的逻辑（性能/代码生成为 LLVM 后端）。具体延迟数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `examples/quickstart.py` 改成「eager 直调」写法：不调用 `.compile()`，而是直接 `c = matmul(a, b)`。这时 `c` 的类型是什么？
**答案**：`c` 是一个 `torch.Tensor`（kernel 被当场编译并执行，返回的是结果张量，而不是 kernel 对象）。这正是 `JITImpl.__call__` 里 eager 分支 `return kernel(*kernel_args.values())` 的行为。

**练习 2**：`JITImpl` 的 `mode` 字段初始值是什么？什么时候才被确定？
**答案**：初始为 `"auto"`（见 `jit()` 装饰器）。在首次 `__call__`/`get_tir` 时由 `_infer_jit_mode()` 根据函数是否返回 `PrimFunc` 确定为 `"lazy"` 或 `"eager"`。

---

### 4.2 模块二：tilelang.language（上）—— Kernel 上下文、三级内存与数据搬运

#### 4.2.1 概念说明

被 `@tilelang.jit` 装饰的函数体里，所有 `T.xxx` 都来自 `tilelang.language`。`import tilelang.language as T` 默认加载的是 **CUDA 方言**（见 [tilelang/language/\_\_init\_\_.py:11-14](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/__init__.py#L11-L14)，它 re-export `tilelang.cuda.language`）。

本模块先讲三个最基础的原语：

- **`T.Kernel(grid_x, grid_y, threads=...)`**：声明 kernel 的启动上下文，等价于 CUDA 里的 `gridDim` 与 `blockDim`。
- **`T.alloc_shared` / `T.alloc_fragment`**：在 shared memory / fragment(register) 上分配分块缓冲。
- **`T.copy(src, dst)`**：在不同内存层级之间搬运一段数据（global↔shared、shared↔fragment 等）。

它们一起表达了 tile 编程的核心动作：「**把数据从 global 搬到片上，算完再搬回 global**」。

#### 4.2.2 核心流程

以 `examples/quickstart.py` 的 kernel 体为例，每个线程块做一件事：

```
with T.Kernel(ceildiv(N,block_N), ceildiv(M,block_M), threads=128) as (bx, by):
    # 1) 在片上分配三个分块缓冲
    A_shared = T.alloc_shared((block_M, block_K), dtype)   # shared
    B_shared = T.alloc_shared((block_K, block_N), dtype)   # shared
    C_local  = T.alloc_fragment((block_M, block_N), accum) # fragment(累加器)

    T.clear(C_local)                                        # 清零累加器
    for k in T.Pipelined(ceildiv(K, block_K), num_stages=3):
        T.copy(A[by*block_M, k*block_K], A_shared)          # global -> shared
        T.copy(B[k*block_K, bx*block_N], B_shared)          # global -> shared
        T.gemm(A_shared, B_shared, C_local)                 # shared x shared -> fragment
    T.copy(C_local, C[by*block_M, bx*block_N])              # fragment -> global
```

网格与输出坐标的对应关系：

- `grid_x = ceildiv(N, block_N)`，`grid_x` 上的索引 `bx` 负责 C 的「列方向」第 `bx` 个块；
- `grid_y = ceildiv(M, block_M)`，`grid_y` 上的索引 `by` 负责 C 的「行方向」第 `by` 个块；
- 因此输出块左上角落在 `C[by*block_M, bx*block_N]`。

`ceildiv(a, b)` 即向上取整除法 \(\lceil a/b \rceil\)，保证不丢尾巴的那一个块。

`T.copy` 是「**语法糖式的并行拷贝**」（源码注释原文为 *sugar syntax for parallelized copy*）：你只写一行，tilelang 会自动把它展开成由 block 内所有线程协作完成的并行加载/存储（在合适硬件上还会用 TMA/`cp.async` 等指令，u3/u6 会讲）。

#### 4.2.3 源码精读

`T.Kernel` 的定义：

[tilelang/language/kernel.py:277-340](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L277-L340) —— `Kernel()`：把 `blocks`（1~3 维 grid）与 `threads`（默认 128，见 [`_normalize_threads` tilelang/language/kernel.py:98-130](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L98-L130)）规整为 `[x,y,z]`，最终调用 `_ffi_api.KernelLaunch(...)` 生成一个 `KernelLaunchFrame`。文档明确：launch nest 以「目标无关」形式发射，CPU 等 SIMT 之外的 backend 会在编译期忽略线程维度，同一份 kernel 可编译到任意 target。

分配片上缓冲的三个函数：

[tilelang/language/allocate.py:34-49](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py#L34-L49) —— `alloc_shared()`：默认 scope `shared.dyn`，返回 shared memory 上的 buffer。
[tilelang/language/allocate.py:66-77](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py#L66-L77) —— `alloc_fragment()`：默认 scope `local.fragment`，用作寄存器/累加器。
[tilelang/language/allocate.py:339-342](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py#L339-L342) —— `empty()`：声明输出张量（住在 global memory）。

`T.copy` 的定义：

[tilelang/language/copy_op.py:54-90](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L54-L90) —— `copy()`：参数包括 `disable_tma`、`eviction_policy`、`prefer_instruction` 等，说明它内部会根据硬件选择 TMA/`cp_async`/同步拷贝等不同实现（本讲只用到最朴素的两参形式）。

`T.ceildiv`：

[tilelang/language/tir/op.py:3450](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/tir/op.py#L3450) —— 向上取整除法，常用于由总维度与块大小推出 grid 大小。

回到示例，把上面这些原语串起来：

[examples/quickstart.py:13-21](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L13-L21) —— 张量注解 `A: T.Tensor((M,K), dtype)` 与 `T.empty((M,N), dtype)` 声明输入输出；`T.Kernel(...)` 与三个 `alloc_*` 搭起 kernel 骨架与片上缓冲。

#### 4.2.4 代码实践

**实践目标**：体会「片上缓冲 + T.copy」的必要性。

**操作步骤**：

1. 打开 `examples/quickstart.py`，定位到 [examples/quickstart.py:19-21](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L19-L21) 的三行 `alloc_*`。
2. 阅读时画一张内存搬运示意：`A(global) --copy--> A_shared(shared) --gemm--> C_local(fragment) --copy--> C(global)`。
3.（选做）打印生成的 CUDA 源码（见 4.4），在其中找到 `__shared__`（shared memory）与寄存器/fragment 相关的声明，印证片上缓冲确实被实体化。

**需要观察的现象**：生成的 kernel 源码里应出现 `__shared__` 数组对应 `A_shared`/`B_shared`，以及一段加载循环对应 `T.copy(global→shared)`。

**预期结果**：能口头解释「为什么不能让所有线程直接读写 global 算 GEMM」——因为 global 带宽低、且每步都重复加载同一块数据；先 copy 进 shared 再算，能让 block 内线程复用这些数据。具体源码片段以本地生成的为准。

#### 4.2.5 小练习与答案

**练习 1**：`T.Kernel("a", threads=128)` 与 `T.Kernel("a", "b", threads=128)` 分别对应几维 grid？`as` 后面分别解包成什么？
**答案**：分别对应 1 维和 2 维 grid。1 维时 `as (bx,)`（或直接 `as bx`，见 [`_normalize_bindings` tilelang/language/kernel.py:87-95](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L87-L95)，单绑定时返回裸 `Var`）；2 维时 `as (bx, by)`。

**练习 2**：`alloc_shared` 和 `alloc_fragment` 的默认 scope 字符串分别是什么？这暗示它们对应 GPU 的哪级存储？
**答案**：`shared.dyn` 与 `local.fragment`，分别对应 shared memory 与寄存器/fragment 级存储（详见 [allocate.py:34-77](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py#L34-L77)）。

---

### 4.3 模块三：tilelang.language（下）—— T.gemm 分块矩阵乘与 T.Pipelined 软件流水线

#### 4.3.1 概念说明

数据搬到片上之后，真正的计算由两个原语完成：

- **`T.gemm(A, B, C)`**：分块矩阵乘。在 shared 缓冲 `A`、`B` 上做矩阵乘并**累加**到 fragment 累加器 `C`（即 \(C \mathrel{+}= A \times B\)）。它会自动 dispatch 到当前硬件的张量核指令——NVIDIA 走 CuTe（CUTLASS），AMD 走 HIP/MFMA。
- **`T.Pipelined(range, num_stages=N)`**：软件流水线。把 K 维循环改造成「**搬下一块数据**」和「**算上一块数据**」重叠执行的流水线，用多级缓冲（`num_stages` 级）隐藏访存延迟。

另外两个在本讲示例里出现的小帮手：

- **`T.clear(buf)`**：把 fragment 缓冲清零。因为 `T.gemm` 默认是累加（`clear_accum=False`），首轮必须先清零累加器。
- **`T.Parallel(extents)`**：把一段元素级循环标记为「block 内所有线程并行执行」，如示例里的 ReLU 后处理。

#### 4.3.2 核心流程

K 维分块累加的数学形式：

\[
C_{i,j} = \sum_{k_o=0}^{\lceil K/block_K \rceil - 1} \sum_{k_i} A_{i,\,k_o\cdot blockK+k_i}\, B_{k_o\cdot blockK+k_i,\,j}
\]

外层 `for k in T.Pipelined(ceildiv(K, block_K), num_stages=3)` 就是在遍历 \(k_o\)，每次取 `A`、`B` 的一个 K-块，做一次 `T.gemm` 累加进 `C_local`。

软件流水线（`num_stages=3`）的直观效果：朴素循环里「搬数据」和「算」是串行的；流水线化后，当线程在算第 \(k_o\) 块时，硬件已在异步搬运第 \(k_o+1\)、\(k_o+2\) 块的数据（用 3 份缓冲轮流切换），从而把访存时间藏在计算时间里。

`T.gemm` 的累加语义示意：

```
T.clear(C_local)              # C_local = 0
for k in T.Pipelined(...):
    T.copy(A_tile, A_shared)  # 载入 A 的第 k 块
    T.copy(B_tile, B_shared)  # 载入 B 的第 k 块
    T.gemm(A_shared, B_shared, C_local)   # C_local += A_shared @ B_shared
```

#### 4.3.3 源码精读

`T.gemm` 的实现入口（`_gemm_impl`）：

[tilelang/language/gemm_op.py:22-90](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L22-L90) —— `_gemm_impl()`：把 A/B/C 规整为 `BufferRegion`，校验形状（断言 `C` 为 2D、`M_A == M` 等），其中 `clear_accum: bool = False`（见 [gemm_op.py:30](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L30)）正是「默认累加、不清零」的由来——这就解释了为什么示例要先 `T.clear(C_local)`。

`T.Pipelined` 的定义与 `num_stages` 含义：

[tilelang/language/loop.py:112-191](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L112-L191) —— `Pipelined()`：参数 `num_stages`（生产者与消费者之间复用的最大缓冲数）、`order`/`stage`（手动流水线注解）。文档说明：`num_stages=0` 时不启用流水线；自动推断流水线下用 `num_stages`，手动调度则用 `order`/`stage`（u3-l3 会深入）。

`T.clear` 与 `T.Parallel`：

[tilelang/language/fill_op.py:40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fill_op.py#L40) —— `clear()`：把 buffer 清零（实现为 `fill(buf, 0)` 的语义）。
[tilelang/language/loop.py:13](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L13) —— `Parallel()`：把循环体标记为 block 内并行（`example_gemm.py` 的 ReLU 用到它）。

把这些放回 `example_gemm.py` 的完整循环体：

[examples/gemm/example_gemm.py:18-24](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py#L18-L24) —— `T.clear(C_local)` → `for k in T.Pipelined(...)` 内三行 `T.copy`×2 + `T.gemm` → 最后 `T.copy(C_local, C[...])` 写回。这正是上面数学公式的直译。

#### 4.3.4 代码实践

**实践目标**：感受 `num_stages` 对性能的影响。

**操作步骤**：

1. 复制 `examples/quickstart.py` 为本地脚本。
2. 把 [examples/quickstart.py:24](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L24) 的 `num_stages=3` 分别改成 `1`、`2`、`3`，其余不变。
3. 用 4.4 介绍的 `get_profiler().do_bench()` 测三组延迟。

**需要观察的现象**：`num_stages=1`（等于不开流水线）通常最慢；增大到 2、3 后延迟下降；继续增大可能因寄存器/shared 压力反而变慢或无法编译。

**预期结果**：三组延迟数值 **待本地验证**，但相对趋势应为「流水线级数适中时最快」。注意 `num_stages` 过大会导致 shared memory 不足而在 semantic check / 编译期报错。

#### 4.3.5 小练习与答案

**练习 1**：如果把示例里的 `T.clear(C_local)` 删掉，结果会怎样？为什么？
**答案**：结果会出错（累加器初值不确定/非零）。因为 `T.gemm` 默认 `clear_accum=False`，即 `C_local += A@B`，必须先清零才能保证累加从一个干净的初值开始。

**练习 2**：`T.Pipelined(n, num_stages=0)` 与 `T.Pipelined(n, num_stages=3)` 的区别是什么？
**答案**：`num_stages=0` 时流水线**不启用**，循环退化为朴素串行（搬数据→算→搬下一块……）；`num_stages=3` 启用 3 级软件流水线，用 3 份缓冲让「搬运」与「计算」重叠，隐藏访存延迟。

---

### 4.4 模块四：tilelang.profiler —— 查看生成代码与测量延迟

#### 4.4.1 概念说明

kernel 编出来之后，tilelang 提供两类「观察」手段：

- **看生成的设备代码**：`kernel.get_kernel_source()` 返回编译器最终生成的 CUDA/HIP/... 源码字符串。这是把 tilelang 当作「编译器」最直观的证据——你写的 Python 被翻译成了一段真实的设备 kernel。
- **测延迟**：`kernel.get_profiler().do_bench()` 会自动构造随机输入、做 warmup、重复计时多次，返回平均延迟（毫秒）。

`Profiler` 还能控制输入数据的供给方式（`TensorSupplyType`），例如用正态分布 `Normal`、均匀分布 `Uniform` 或全整数 `Integer` 来喂输入。

#### 4.4.2 核心流程

```
kernel = matmul.compile(M=..., ...)             # 已编译的 JITKernel

print(kernel.get_kernel_source())               # 1) 看设备源码

profiler = kernel.get_profiler(                 # 2) 取 profiler（默认输入供给）
    tensor_supply_type=tilelang.TensorSupplyType.Normal)
latency = profiler.do_bench()                   # 3) 自动 warmup + 计时
print(f"Latency: {latency} ms")
```

`do_bench` 的计时口径（来自源码）：默认 `warmup=25`（ms 预热）、`rep=100`（重复次数）、`backend="event"`（计时后端，可选 `"cupti"`、`"cudagraph"`）、`return_mode="mean"`（返回多次计时的均值）。

#### 4.4.3 源码精读

`JITKernel` 上两个关键方法：

[tilelang/jit/kernel.py:485-496](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L485-L496) —— `get_kernel_source()`：对 `tvm_ffi`/`nvrtc`/`cython`/`cutedsl` 后端走 adapter 取源码，否则取 `artifact.kernel_source`。
[tilelang/jit/kernel.py:469-483](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L469-L483) —— `get_profiler()`：用 `self.params`（参数描述）+ `self.out_idx`（输出下标）构造 `Profiler`，并 `.with_default_adapter(self.adapter)` 绑定可调用对象。

`Profiler` 与 `do_bench`：

[tilelang/profiler/\_\_init\_\_.py:21-35](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L21-L35) —— `Profiler` dataclass：持有 `params`、`result_idx`、`supply_type`、`adapter`。
[tilelang/profiler/\_\_init\_\_.py:220-283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L220-L283) —— `do_bench()`：根据 `supply_type` 自动生成输入，用 `partial(bench_target, *ins)` 把「带输入的 kernel 调用」交给底层 `do_bench` 计时。

输入供给类型枚举：

[tilelang/utils/tensor.py:32-39](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/tensor.py#L32-L39) —— `TensorSupplyType`：`Integer=1`、`Uniform=2`、`Normal=3`、…、`Auto=7`。`Normal` 对应正态分布随机输入（示例所用）。

回到示例脚本：

[examples/quickstart.py:51](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L51) `print(kernel.get_kernel_source())` —— 打印生成的 CUDA 源码。
[examples/quickstart.py:54-57](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py#L54-L57) `profiler = kernel.get_profiler(); latency = profiler.do_bench(backend="cupti")` —— 用 CUPTI 后端测延迟并打印。`example_gemm.py` 用的是不带参数的 `do_bench()`（默认 `event` 后端，见 [example_gemm.py:54-57](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py#L54-L57)）。

#### 4.4.4 代码实践

**实践目标**：打印生成源码、测出一次延迟，并对照源码理解「DSL 被翻译成了什么」。

**操作步骤**：

1. 运行 `python examples/quickstart.py`。
2. 在 stdout 里找到 `CUDA Source:` 之后的整段代码，重点找：
   - `__global__` kernel 函数签名（对应 `T.Kernel` 的启动上下文）；
   - `__shared__` 数组（对应 `alloc_shared`）；
   - 形如 `cute::` 或 CUTLASS/MFMA 相关的矩阵乘调用（对应 `T.gemm`）；
   - 多级缓冲与 `cp.async`/barrier 相关的同步（对应 `T.Pipelined`）。
3. 记录最后打印的 `tilelang Latency: xxx ms`。

**需要观察的现象**：生成的 CUDA 是一段结构清晰、含 shared memory 与张量核调用的 kernel；延迟为一个具体毫秒数。

**预期结果**：能从生成源码里至少指出 ① shared memory 声明、② 矩阵乘/张量核调用、③ K 维循环与流水线同步 这三类痕迹。延迟绝对值**待本地验证**（取决于 GPU 型号与频率）。

> 进阶提示：`JITKernel` 还提供 `show_source("both")`（同时打印 host/device 源码）、`show_ptx()` / `show_sass()`（CUDA 专属，看 PTX/SASS）、`export_library(path)`（导出 `.so`）等（见 [kernel.py:510-543](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L510-L543) 与 [712-762](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L712-L762)），本讲暂不展开。

#### 4.4.5 小练习与答案

**练习 1**：`get_profiler()` 默认的 `tensor_supply_type` 是什么？示例里为什么显式传 `Normal`？
**答案**：默认是 `TensorSupplyType.Auto`（见 [kernel.py:469](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L469)）。示例显式传 `Normal` 是为了用正态分布随机数据做基准测试，更贴近真实推理输入分布。

**练习 2**：`do_bench(backend="cupti")` 与 `do_bench()`（默认 backend）在计时后端上有何不同？
**答案**：前者用 NVIDIA CUPTI 做更精确的 GPU 端计时；后者默认 `backend="event"`（CUDA event 计时）。此外还有 `"cudagraph"` 用 CUDA Graph 捕获后计时。三者口径略有差异，做对比时应在同一后端下测量。

---

## 5. 综合实践

把本讲四个模块串成一个完整任务。

**任务**：基于 `examples/quickstart.py`，写一个 **「带 ReLU 的分块 GEMM」**，并完成「编译 → 调用 → 数值验证 → 看源码 → 测延迟」全流程。

**参考做法**（可与 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py) 与 [README.md:136-227](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/README.md#L136-L227) 对照）：

```python
# 示例代码（基于 examples/quickstart.py 改写，非项目原有文件）
import tilelang
import tilelang.language as T
import torch

@tilelang.jit
def matmul_relu(A, B, block_M, block_N, block_K, dtype=T.float16, accum_dtype=T.float32):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)

    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_K, block_N), dtype)
        C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)

        T.clear(C_local)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by * block_M, k * block_K], A_shared)
            T.copy(B[k * block_K, bx * block_N], B_shared)
            T.gemm(A_shared, B_shared, C_local)

        # 用 T.Parallel 做元素级 ReLU（综合 4.3 模块）
        for i, j in T.Parallel(block_M, block_N):
            C_local[i, j] = T.max(C_local[i, j], 0)

        T.copy(C_local, C[by * block_M, bx * block_N])
    return C

M = N = K = 1024
kernel = matmul_relu.compile(M=M, N=N, K=K, block_M=128, block_N=128, block_K=32)

a = torch.randn(M, K, device="cuda", dtype=torch.float16)
b = torch.randn(K, N, device="cuda", dtype=torch.float16)
c = kernel(a, b)

torch.testing.assert_close(c, torch.relu(a @ b), rtol=1e-2, atol=1e-2)
print(kernel.get_kernel_source())
print("Latency:", kernel.get_profiler().do_bench(), "ms")
```

**验收清单**：

1. `assert_close` 通过（数值与 `torch.relu(a@b)` 一致）；
2. 能在打印的源码里指出 shared memory 声明、矩阵乘调用、ReLU 对应的 `max(...,0)` 循环；
3. 得到一个延迟数值（**待本地验证**），并尝试调 `num_stages` 观察变化。

> 通过这个任务，你实际走过了 tilelang 的「写 DSL → 编译 → adapter → 调用 → 看代码 → 测性能」整条主干，为后续拆解编译器内部（u4/u6）打下直觉。

## 6. 本讲小结

- `@tilelang.jit` 把 Python 函数包成可编译对象，有 lazy/eager 两种模式（自动推断）；`.compile(...)` 走显式编译返回 `JITKernel`，直接 `matmul(a,b)` 则 eager 执行返回结果张量。
- DSL 三件套：`T.Kernel(grid..., threads=...)` 描述启动上下文，`T.alloc_shared/alloc_fragment` 分配片上缓冲，`T.copy(src,dst)` 在内存层级间并行搬运数据。
- 计算核心：`T.gemm(A,B,C)` 默认**累加**（`clear_accum=False`，需先 `T.clear`），自动 dispatch 到 CuTe/HIP 张量核；`T.Pipelined(range, num_stages=N)` 用多级缓冲把访藏到计算里。
- `JITKernel` 是「编译产物 + adapter」的封装：`get_kernel_source()` 看生成的 CUDA/HIP 源码，`get_profiler().do_bench()` 自动测延迟。
- `tilelang.language` 默认是 CUDA 方言（re-export `tilelang.cuda.language`），其它后端用 `tilelang.<backend>.language` 显式引入——这呼应了 u1-l1 讲的全景。
- `Profiler` 通过 `TensorSupplyType` 控制输入供给（`Normal`/`Uniform`/`Integer`/`Auto`），`do_bench` 支持 `event`/`cupti`/`cudagraph` 三种计时后端。

## 7. 下一步学习建议

本讲只让你「会用」并建立了「Python 被翻译成设备代码」的直觉。接下来建议：

- **想系统学 DSL 语法** → 进入 **u2（DSL 语言基础）**：`prim_func`/`Kernel`/`Tensor` 的正式定义（u2-l1）、内存层级与 `T.copy` 细节（u2-l2）、控制流与循环原语（u2-l3）、类型系统与 dtype（u2-l4）。
- **想懂编译链路** → 进入 **u4（编译流水线与 JIT）**：`tilelang.engine.lower` 如何把 TIR 经 Pass 流水线变成设备代码（u4-l1），lazy/eager 与 `JITKernel` 的完整关系（u4-l2），编译缓存机制（u4-l3）。
- **想懂 `T.gemm` 背后** → 进入 **u3（核心计算与调度原语）**：tile op registry 与 dispatch（u3-l1）、`T.Pipelined` 的自动推断（u3-l3）。
- **继续阅读源码**：先精读 [examples/quickstart.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/quickstart.py) 与 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py)，再浏览 [examples/](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/) 下 FlashAttention、MLA 等更复杂的真实算子。
