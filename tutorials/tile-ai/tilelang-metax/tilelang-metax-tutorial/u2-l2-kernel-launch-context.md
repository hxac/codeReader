# T.Kernel 启动上下文与线程模型

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `with T.Kernel(...)` 到底定义了什么，以及它和 `@T.prim_func` 的关系。
- 正确配置 kernel 的 **grid（线程块网格）** 和 **threads（每块线程数）**，并理解它们的默认值与归一化规则。
- 解释 `as (bx, by)` 里的 `bx`/`by` 是怎么来的、它们最终映射成 GPU 的哪个量。
- 看懂 TileLang 把「启动上下文」写成 **target 中立** 的 IR、再由 `MaterializeKernelLaunch` pass 物化成各后端真实启动形式的整套设计。
- 了解 `T.ClusterKernel`（线程块集群，SM90+）的用途。

## 2. 前置知识

本讲默认你已学过 [u2-l1（prim_func 与类型系统）](u2-l1-prim-func-and-type-system.md)，并跑通过 GEMM 示例（[u1-l4](u1-l4-first-gemm-kernel.md)）。下面补充几个 GPU 概念，方便理解「启动上下文」。

GPU 执行一段 kernel 代码时，硬件按两级层次来调度：

- **线程（thread）**：最小的执行单位。每个线程有自己的寄存器。
- **线程块（thread block / CTA）**：一组线程。同一个块内的线程共享一块片上共享内存（shared memory），可以用 `__syncthreads` 这类栅栏同步。
- **网格（grid）**：所有线程块的集合，即整个 kernel 启动的总规模。

每个线程在启动时会被赋予两个坐标：

- `blockIdx.{x,y,z}`：当前线程块在网格里的编号，取值范围由 `gridDim.{x,y,z}` 决定。
- `threadIdx.{x,y,z}`：当前线程在本块里的编号，取值范围由 `blockDim.{x,y,z}` 决定。

一个 kernel 启动的总线程数为：

\[
\text{总线程数} = (\text{gridDim}.x \cdot \text{gridDim}.y \cdot \text{gridDim}.z) \times (\text{blockDim}.x \cdot \text{blockDim}.y \cdot \text{blockDim}.z)
\]

> 名词速查：**CTA**（Cooperative Thread Array）就是线程块的硬件称呼；**warp** 是 GPU 硬件实际调度的线程束（CUDA 中 32 线程一束，MACA 中 64 线程一束，见 [u7-l5](u7-l5-maca-vs-cuda-vs-rocm.md)）；**SIMT**（Single Instruction, Multiple Thread）指「同一条指令驱动多个线程」的执行模型，CUDA/ROCm/MACA 都是 SIMT 后端。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tilelang/language/kernel.py` | `T.Kernel` 的 Python 实现：构造启动帧 `KernelLaunchFrame`、归一化 grid/threads、暴露 `get_thread_binding` 等查询函数、`ClusterKernel`。 |
| `src/ir.cc` | C++ 侧 `KernelLaunch`：把 grid/threads 翻译成一串绑定到 `blockIdx.*/threadIdx.*` 的 `kThreadBinding` 循环帧。 |
| `src/transform/materialize_kernel_launch.cc` | `MaterializeKernelLaunch` pass：把中立的启动循环物化成后端真实形式（GPU 的 `thread_extent` / CPU 的串行循环）。 |
| `examples/gemm/example_gemm.py` | 实践对象：一个 2D grid 的 GEMM kernel。 |

## 4. 核心概念与源码讲解

### 4.1 T.Kernel：启动上下文的容器

#### 4.1.1 概念说明

回顾 [u2-l1](u2-l1-prim-func-and-type-system.md)：`@T.prim_func` 在编译期把一个带类型注解的 Python 函数重写成 TIR `PrimFunc`，描述的是「计算规格」——做什么计算、张量形状如何。

但光有计算规格还不够，还得告诉编译器**怎么启动它**：要用多少个线程块、每块多少线程。这正是 `with T.Kernel(...)` 的职责。它是一个 **上下文管理器（context manager）**，你在 `with` 块里写的代码描述的是 **一个线程块要做的事**；编译器会自动把这个「单块」逻辑复制到网格里的每一个块上，并用 `blockIdx` 区分它们。

一句话区分：

- `@T.prim_func`：定义「**算什么**」。
- `with T.Kernel(...)`：定义「**怎么启动、每个块负责哪一块数据**」。

绝大多数 TileLang kernel 的 `PrimFunc` 体最外层就是一个 `with T.Kernel(...)`。

#### 4.1.2 核心流程

`T.Kernel` 在 Python 侧的工作非常薄，核心只有三步：

1. 检查当前是否处于 `@tilelang.jit` / `@T.prim_func` 的 Builder 上下文中（否则报错）。
2. 把 `threads` 归一化成三维列表。
3. 调用 C++ 侧 `_ffi_api.KernelLaunch(blocks, threads, attrs)`，返回一个 `KernelLaunchFrame`。

随后 `with` 语句触发帧的进入/退出，把启动嵌套写进正在构建的 TIR 里。注意：**这一步产生的 IR 与具体后端无关**——它只是「打了 `blockIdx.x`/`threadIdx.x` 标签的循环」，真正的物化留给后面的 pass（见 4.3）。

#### 4.1.3 源码精读

`Kernel` 函数定义在 [tilelang/language/kernel.py:258-321](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L258-L321)，它的 docstring 里点明了设计意图——发射的是 target 中立的 `thread_binding` 循环，由后端 pipeline 里的 `MaterializeKernelLaunch` 物化：

```python
def Kernel(*blocks, threads=None, prelude=None):
    ...
    if Builder.current() is None:
        raise JITNoBuilderError("T.Kernel() can only be used inside @tilelang.jit or @T.prim_func context.")
    attrs: dict = {}
    threads = _normalize_threads(threads)      # 归一化为三维
    if prelude is not None:
        attrs["pragma_import_c"] = prelude
    return _ffi_api.KernelLaunch(blocks, threads, attrs)   # 进入 C++ 构帧
```

返回的 `KernelLaunchFrame` 是一个自定义 TIR 帧，类定义在 [tilelang/language/kernel.py:130-256](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L130-L256)。它的 `__enter__` 把自己压入一个线程局部的栈（`_get_current_stack`），并返回 grid 的绑定变量：

```python
def __enter__(self):
    super().__enter__()
    _get_current_stack().push(self)
    # 最后 4 个帧是 threadIdx.x/y/z + 带 attr 的 block，前面才是 grid 帧绑定
    return _normalize_bindings([frame.vars[0] for frame in self.frames[0:-4]])
```

这个 `frames[0:-4]` 的切片是理解绑定关系的关键，下一节展开。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 `T.Kernel` 必须在 Builder 上下文里使用。

1. 打开 `examples/gemm/example_gemm.py`，确认 `with T.Kernel(...)` 出现在被 `@tilelang.jit` 装饰的 `matmul` 函数体里（ Builder 在 `@tilelang.jit` 调用时建立）。
2. 想象一下：如果把 `with T.Kernel(...)` 这行挪到一个普通 Python 函数（没有 `@tilelang.jit` / `@T.prim_func`）里直接调用，会怎样？

**预期结果**：会抛出 `JITNoBuilderError`，提示 `T.Kernel() can only be used inside @tilelang.jit or @T.prim_func context.`（对应 [tilelang/language/kernel.py:312-313](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L312-L313)）。这印证了「启动上下文必须依附于一个正在被编译的 prim_func」。

#### 4.1.5 小练习与答案

**练习 1**：`@T.prim_func` 和 `with T.Kernel(...)` 各自负责描述什么？

> **答案**：`@T.prim_func` 描述「算什么」（计算规格、张量形状与 dtype）；`with T.Kernel(...)` 描述「怎么启动」——线程块网格的形状、每块线程数，以及每个块用 `blockIdx` 负责哪一块数据。

**练习 2**：为什么 `T.Kernel` 不在调用时直接生成 CUDA 的 `<<<grid, block>>>` 启动语法？

> **答案**：因为 TileLang 是多后端的（CUDA/ROCm/MACA/Metal/CPU…）。`T.Kernel` 只生成 target 中立的 `kThreadBinding` 循环，把「翻译成哪种具体启动形式」推迟到知道 target 之后的 `MaterializeKernelLaunch` pass，从而让同一份 kernel 能编译到任意后端。

---

### 4.2 grid 与 threads：线程块网格的形状

#### 4.2.1 概念说明

`T.Kernel` 的签名是：

```python
T.Kernel(*blocks, threads=None, prelude=None)
```

- **`*blocks`（位置参数，1~3 个）**：定义 **grid**，即每个维度的线程块数量，对应 `gridDim.x/y/z`。
- **`threads`（关键字参数）**：定义 **每块的线程数**，对应 `blockDim`。可以是单个整数（只设 `blockDim.x`），也可以是列表/元组（设多个维度）。**默认值是 128**。

在分块（tiling）场景里，grid 通常用「总规模除以块大小、向上取整」算出来。TileLang 借用了 TVM TIR script 命名空间里的 `T.ceildiv(a, b)`，它就是 \(\lceil a/b \rceil\)（来自 `from tvm.tirx.script.parser import *`，见 [tilelang/language/__init__.py:10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L10)）。

#### 4.2.2 核心流程

以 GEMM 为例，输出矩阵 C 形状是 \((M, N)\)。把 C 切成 \(\text{block\_M} \times \text{block\_N}\) 的小块，则网格两个维度分别是：

\[
\text{gridDim}.x = \lceil N / \text{block\_N} \rceil, \qquad
\text{gridDim}.y = \lceil M / \text{block\_M} \rceil
\]

于是总块数为 \(\text{gridDim}.x \cdot \text{gridDim}.y\)。注意 tilelang 例子里 **第一个位置参数对应 `bx`（x 方向，沿 N 切）**，第二个对应 `by`（y 方向，沿 M 切）——这是一个容易记混的点。

`threads` 的归一化规则由 `_normalize_threads` 实现，它会把任何输入统一成 **三维列表** `[blockDim.x, blockDim.y, blockDim.z]`：

| 输入 | 归一化结果 |
|------|-----------|
| `None` | `[128, 1, 1]`（默认 128 线程） |
| `128`（int） | `[128, 1, 1]` |
| `(64, 2)`（tuple/list） | `[64, 2, 1]`（不足 3 维补 1） |

每块总线程数为 \(blockDim.x \cdot blockDim.y \cdot blockDim.z\)。

#### 4.2.3 源码精读

归一化函数 [_normalize_threads](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L98-L111)（`tilelang/language/kernel.py:98-111`）：

```python
def _normalize_threads(threads):
    if threads is None:
        threads = 128              # 默认 128
    if isinstance(threads, int):
        return [threads, 1, 1]     # 标量 -> blockDim.x
    if isinstance(threads, list):
        return threads + [1] * (3 - len(threads))   # 不足 3 维补 1
    if isinstance(threads, tuple):
        return list(threads) + [1] * (3 - len(threads))
    raise ValueError("threads must be an integer or a list of integers")
```

注意它 **总是返回长度为 3 的列表**——这一点很重要，它保证了 C++ 侧永远收到 3 个线程维度，使得帧布局是确定的（见 4.3）。

GEMM 里的真实用法在 [examples/gemm/example_gemm.py:13](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L13)：

```python
with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
    ...
    T.copy(C_local, C[by * block_M, bx * block_N])   # 写回输出 tile
```

#### 4.2.4 代码实践

**目标**：亲手改 grid 与 threads，理解每个线程块处理哪块输出。

1. 在 `examples/gemm/example_gemm.py` 的 `main()` 里，`compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)`。
2. 手算 grid：`gridDim.x = ceil(1024/128) = 8`，`gridDim.y = ceil(1024/128) = 8`，共 \(8 \times 8 = 64\) 个线程块。
3. 把 `threads=128` 改成 `threads=256`，重新运行 `python examples/gemm/example_gemm.py`。

**需要观察的现象**：
- 数值结果（`All check passed.`）应仍然通过——threads 只改变块内并行度，不改每个块负责的输出 tile。
- 延迟 `tilelang Latency` 可能变化（256 线程每块占用更多寄存器/共享内存资源，不一定更快）。
- 用 `kernel.get_kernel_source()` 打印生成的 CUDA 源码，能看到启动处 `blockDim.x` 从 128 变成 256。

**预期结果**：grid 形状不变，仍是 \(8 \times 8\) 块；改变 threads 只影响每块的线程数与块内循环的并行方式，不影响 `bx`/`by` 与输出 tile 的对应关系。若手头没有 GPU，「待本地验证」延迟数字，但上述源码变化可凭 `get_kernel_source()` 在任意能编译的环境观察到。

#### 4.2.5 小练习与答案

**练习 1**：若 `M=N=1024, block_M=block_N=128`，`threads` 不填，每块有多少线程？整个 kernel 总共启动多少线程？

> **答案**：`threads` 默认 128，即每块 128 线程；grid 为 \(8 \times 8 = 64\) 块；总线程数 \(= 64 \times 128 = 8192\)。

**练习 2**：`T.ceildiv(N, block_N)` 写成普通 Python 表达式是什么？

> **答案**：\(\lceil N / \text{block\_N} \rceil\)，等价于 `-(-N // block_N)` 或 `(N + block_N - 1) // block_N`。用 `ceildiv` 而非 `/` 是为了避免浮点、保持整数 IR。

---

### 4.3 block 绑定：bx/by 从哪里来

#### 4.3.1 概念说明

`with T.Kernel(...) as (bx, by):` 里的 `bx`、`by` 叫做 **block 绑定（block binding）**。它们是编译期产生的 TIR 循环变量，最终会变成 GPU 里的 `blockIdx.x` / `blockIdx.y`。在你的 kernel 体内，你用 `bx`、`by` 来计算「我这块要读/写哪个数据 tile」。

同理，每个线程也有自己的坐标 `threadIdx.x/y/z`，不过 TileLang 鼓励你用结构化循环（`T.Parallel` 等）而不是裸线程索引——大多数 kernel 用不到 `threadIdx`。

#### 4.3.2 核心流程

`T.Kernel` 在 C++ 侧把启动上下文展开成一串 **帧（frame）**，顺序是：

```
[ bx, by, (bz), tx, ty, tz, DeviceMainBlock ]
   └ grid 帧 ─┘  └ thread 帧 ┘  └ 主体 block ┘
```

- grid 帧：1~3 个，变量名 `bx`/`by`/`bz`，标签 `blockIdx.x/y/z`。
- thread 帧：**固定 3 个**（因为 `_normalize_threads` 永远返回 3 维），变量名 `tx`/`ty`/`tz`，标签 `threadIdx.x/y/z`。
- 末尾 1 个 `DeviceMainBlock`：承载 kernel 主体与属性。

所以 `frames[0:-4]` 取出的正好是 grid 帧（去掉最后 4 个 = 3 thread + 1 block），`frames[-4:-1]` 是 3 个 thread 帧。这正是 4.1 里 `__enter__` 切片的依据。

这些帧一开始都写成 `ForKind::kThreadBinding` 的循环——**target 中立**。随后 `MaterializeKernelLaunch` pass 根据 target 把它们物化：

- **SIMT 后端（CUDA/ROCm/MACA/Metal）**：每个启动循环变成 `thread_extent` 属性语句，循环变量直接复用（`bx` 就真的成了 `blockIdx.x`）。
- **非 SIMT 后端（如 CPU）**：`blockIdx.*` 变成普通串行 `for` 循环遍历 grid；`threadIdx.*` 被忽略，退化为单次循环（变量固定为 0）。

正因为有这一步，**同一份 `T.Kernel` 写法可以编译到任意 target**。

#### 4.3.3 源码精读

C++ 侧 [KernelLaunch](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L264-L297)（`src/ir.cc:264-297`）按固定名字/标签造帧：

```cpp
static const char *kBlockVarNames[3] = {"bx", "by", "bz"};
static const char *kBlockTags[3]     = {"blockIdx.x", "blockIdx.y", "blockIdx.z"};
static const char *kThreadVarNames[3] = {"tx", "ty", "tz"};
static const char *kThreadTags[3]     = {"threadIdx.x", "threadIdx.y", "threadIdx.z"};

for (size_t i = 0; i < grid_size.size(); i++)            // grid 帧
  n->frames.push_back(MakeThreadBindingFrame(kBlockVarNames[i], kBlockTags[i], grid_size[i]));
for (size_t i = 0; i < block_size.size(); i++)           // thread 帧（恒为 3）
  n->frames.push_back(MakeThreadBindingFrame(kThreadVarNames[i], kThreadTags[i], block_size[i]));
// 末尾再压一个空 DeviceMainBlock 承载主体与 attrs
```

每个帧由 [MakeThreadBindingFrame](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L31-L55)（`src/ir.cc:31-55`）生成一个 `ForKind::kThreadBinding` 循环，标签就是 `blockIdx.x` 这类 thread_tag：

```cpp
IterVar iter_var(Range{nullptr}, Var(thread_tag, vars[0]->dtype),
                 IterVarType::kThreadIndex, thread_tag);
return For(vars[0], doms[0]->min, doms[0]->extent, ForKind::kThreadBinding, body,
           /*thread_binding=*/iter_var, ...);
```

物化逻辑在 [MaterializeKernelLaunch pass](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L1-L22)（`src/transform/materialize_kernel_launch.cc`，文件头注释把两种后端策略讲得很清楚）。核心 `ConvertNest` 在 [L73-L96](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L73-L96)：

```cpp
if (lower_thread_binding_) {                 // SIMT 后端
  // 启动循环 -> AttrStmt(thread_extent)，循环变量复用，bx 即 blockIdx.x
  return AttrStmt(iter_var, tirx::attr::thread_extent, op->extent, body);
}
// 非 SIMT（CPU）：blockIdx -> 串行 for；threadIdx -> 单次循环（extent 置 1）
PrimExpr extent = IsThreadBinding(op) ? IntImm(...,1) : op->extent;
return For(op->loop_var, op->min, extent, ForKind::kSerial, body, ...);
```

Python 侧还提供了查询当前绑定的便捷函数，例如 [get_thread_binding / get_block_binding](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L468-L513)（`tilelang/language/kernel.py:468-513`），它们从线程局部栈顶取当前 `KernelLaunchFrame` 返回对应变量。

#### 4.3.4 代码实践（源码阅读型）

**目标**：把「bx/by 是 blockIdx.x/y」这件事在生成的源码里坐实。

1. 复用 4.2 的 GEMM，`kernel = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)`。
2. 打印 `kernel.get_kernel_source()`，在生成的 CUDA 源码里找到 kernel 签名附近的启动规模（通常形如 `dim3 block(128, 1, 1), grid(8, 8, 1)`）和 `blockIdx.x` / `blockIdx.y` 的使用位置。
3. 对照源码里的 `A[by * block_M, ...]`、`C[by * block_M, bx * block_N]`，确认 `by` 对应 `blockIdx.y`（沿 M），`bx` 对应 `blockIdx.x`（沿 N）。

**需要观察的现象**：生成的源码里 `blockIdx.x` 出现在计算 `bx * block_N` 这类地址里；`blockIdx.y` 出现在 `by * block_M` 里。

**预期结果**：每个线程块 `(bx, by)` 负责输出矩阵 C 的子块 `C[by*block_M : (by+1)*block_M, bx*block_N : (bx+1)*block_N]`。若环境无 GPU，仅做源码阅读，标注「待本地验证」运行部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `frames[0:-4]` 能稳定地取出 grid 绑定，而不用管 grid 是 1 维还是 3 维？

> **答案**：因为 `_normalize_threads` 永远把 `threads` 归一化为 3 维，所以末尾恒为「3 个 thread 帧 + 1 个主体 block = 4 个帧」。`frames[0:-4]` 去掉这固定的 4 个，剩下的就必然是 grid 帧，与 grid 维数无关。

**练习 2**：把同一个 GEMM kernel 分别编译到 `cuda` 和 CPU（`llvm`），生成的启动代码有何本质区别？

> **答案**：`cuda` 下启动循环被物化成 `thread_extent` AttrStmt，`bx`/`by` 即 `blockIdx.x/y`，每块由硬件并行调度；CPU（`lower_thread_binding=false`）下 `blockIdx.*` 退化为串行 `for` 循环遍历 grid，`threadIdx.*` 退化为单次循环（固定 0），即「每个块单线程顺序跑」。这就是 `MaterializeKernelLaunch` 的两种策略。

---

### 4.4 ClusterKernel：线程块集群（SM90+）

#### 4.4.1 概念说明

`T.ClusterKernel` 是 `T.Kernel` 的 **CUDA 专用变体**，用于 Hopper（SM90）及更新架构的 **线程块集群（thread block cluster）**。

普通 kernel 里，线程块之间是互相独立的，各自有私有的共享内存。集群则把 **多个线程块（CTA）编成一组**，它们可以访问彼此的共享内存（分布式共享内存, DSMEM）、用集群级栅栏同步、做 TMA 多播。这为大规模 GEMM/Attention 提供了更灵活的协作方式。

用法上，`ClusterKernel` 和 `Kernel` 几乎一样，只是多了一个必填的 `cluster_dims` 参数，表示集群由几个 CTA 组成（如 `(2,1,1)` 表示 2 个 CTA 一个集群）。

> ⚠️ 集群是 CUDA SM90+ 特性，MACA 后端不支持（见 [u7-l5](u7-l5-maca-vs-cuda-vs-rocm.md)）。本 fork 的核心是 MACA 后端，这里仅作扩展知识了解。

#### 4.4.2 核心流程

`ClusterKernel` 的流程与 `T.Kernel` 完全一致，区别只在于把 `cluster_dims` 归一化后写进 `attrs["cluster_dims"]`，最终驱动 CUDA 用 `cudaLaunchKernelEx` + `cudaLaunchAttributeClusterDimension` 启动。`cluster_dims` 归一化成三维 `[cx, cy, cz]`，若为 `[1,1,1]`（即没有集群效果）则记为 `None`。

#### 4.4.3 源码精读

[ClusterKernel](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L324-L374)（`tilelang/language/kernel.py:324-374`）：

```python
def ClusterKernel(*blocks, cluster_dims, threads=None, prelude=None):
    ...
    threads = _normalize_threads(threads)
    cluster_dims = _normalize_cluster_dims(cluster_dims)
    if cluster_dims is not None:
        attrs["cluster_dims"] = cluster_dims
    return _ffi_api.KernelLaunch(blocks, threads, attrs)   # 仍是同一个 KernelLaunch
```

注意它 **底层仍调用同一个 `_ffi_api.KernelLaunch`**，只是多带了 `cluster_dims` 属性——启动上下文的帧结构完全复用 `T.Kernel`。归一化函数 [_normalize_cluster_dims](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L114-L127)（`tilelang/language/kernel.py:114-127`）把输入补成三维，并在全 1 时返回 `None`。

一个真实用例见集群测试 [testing/python/language/test_tilelang_language_cluster.py:14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/language/test_tilelang_language_cluster.py#L14)：

```python
with T.ClusterKernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M),
                     threads=128, cluster_dims=(2, 1, 1)) as (bx, by):
    ...
```

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解 `cluster_dims` 如何落到生成的 host 启动代码里。

1. 阅读 [testing/python/language/test_tilelang_language_cluster.py:74-94](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/language/test_tilelang_language_cluster.py#L74-L94) 的 `run_cython_cluster_launch` / `run_tvm_ffi_cluster_launch`。
2. 注意断言：`assert "clusterDim = {2, 1, 1}" in mod.get_host_source()`，以及 tvm-ffi 后端里把 `2/1/1` 写进启动参数。

**预期结果**：`cluster_dims=(2,1,1)` 会以 `clusterDim = {2, 1, 1}` 的形式出现在 host 侧启动代码里。无 SM90+ 设备时，仅做源码阅读即可，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`ClusterKernel` 和 `Kernel` 在底层帧结构上有区别吗？

> **答案**：没有。两者都调用 `_ffi_api.KernelLaunch`，产生同样的 grid/thread 帧结构；`ClusterKernel` 只是把 `cluster_dims` 作为额外属性带上，由后端在启动时用 `cudaLaunchKernelEx` 集群方式启动。

**练习 2**：`_normalize_cluster_dims(1)` 返回什么？为什么？

> **答案**：返回 `None`。因为 `1` 被归一化成 `[1,1,1]`，而全 1 等于「不组成集群」，所以函数显式返回 `None` 表示不设置该属性，避免给后端传递无意义的集群维度。

## 5. 综合实践

把本讲的 grid/threads/绑定串起来，完成下面这个小任务（无 GPU 也可做源码与手算部分）：

1. **手算并验证 grid**：对 `examples/gemm/example_gemm.py`，给定 `M=N=K=1024, block_M=block_N=128, block_K=32`，写出 `gridDim.x`、`gridDim.y`、总块数。
2. **解释 tile 归属**：用一句话说明线程块 `(bx=3, by=5)` 负责输出 C 的哪个子块（用行列范围表示）。
3. **改 threads 观察**：把 `threads=128` 改为 `threads=256` 重跑（或仅打印 `get_kernel_source()`），说明哪些东西变了、哪些没变，并给出你的判断依据（引用本讲讲过的 `MaterializeKernelLaunch` / `_normalize_threads`）。
4. **（进阶）源码追踪**：在 [src/ir.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L264-L297) 与 [src/transform/materialize_kernel_launch.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L73-L96) 里，画出「`T.Kernel(...)` 的位置参数 → `kThreadBinding` 帧 → `thread_extent` AttrStmt」的数据流。

参考答案：

1. `gridDim.x = ⌈1024/128⌉ = 8`，`gridDim.y = 8`，总块数 \(64\)。
2. 块 `(bx=3, by=5)` 负责 `C[5*128:6*128, 3*128:4*128] = C[640:768, 384:512]`（行对应 `by`/M，列对应 `bx`/N）。
3. 变的是每块线程数（128→256）及块内并行方式；不变的是 grid 形状（仍 \(8\times8\)）、`bx/by` 与输出 tile 的对应、数值正确性。依据：`threads` 只进 `_normalize_threads` 影响 `blockDim`，不进 grid 计算；grid 由位置参数 `T.ceildiv(...)` 决定；`MaterializeKernelLaunch` 对 SIMT 后端把 `threadIdx.*` 物化成 `thread_extent`，与 grid 无关。
4. 数据流：`T.Kernel(*blocks, threads)` →（Python）`_normalize_threads` 得三维 threads →（C++ `KernelLaunch`）按 `bx/by/...` 名与 `blockIdx.x/...` 标签造 `kThreadBinding` 帧 → `MaterializeKernelLaunch` 把这些帧改成 `AttrStmt(thread_extent)`（SIMT）或串行 `For`（CPU）。

## 6. 本讲小结

- `with T.Kernel(*blocks, threads=...)` 定义 kernel 的 **启动上下文**：`blocks` 决定 grid（`gridDim`），`threads` 决定每块线程数（`blockDim`，默认 128，归一化为三维）。
- `as (bx, by)` 解包的是 grid 绑定变量，它们最终映射成 `blockIdx.x/y`；每个块用它们计算自己负责的数据 tile。
- C++ `KernelLaunch` 按固定顺序造帧：grid 帧（1~3）+ thread 帧（恒 3）+ 1 个主体 block，这正是 `frames[0:-4]` 切片的由来。
- 启动上下文是 **target 中立** 的 `kThreadBinding` 循环，由 `MaterializeKernelLaunch` 物化：SIMT 后端变 `thread_extent`，CPU 后端退化为串行循环——这就是「一份 kernel 编译到任意后端」的关键。
- `T.ClusterKernel` 是 SM90+ 集群变体，底层复用同一个 `KernelLaunch`，只多带 `cluster_dims` 属性（MACA 不支持）。

## 7. 下一步学习建议

- 想掌握 kernel 体内的循环与并行写法（`T.serial`/`T.Parallel`/`T.Pipelined`），继续学 [u2-l3（循环与控制流）](u2-l3-loops-and-control-flow.md)——本讲的「线程块」是外层 grid，「线程」则主要靠这些循环结构在块内表达。
- 想了解内存层级（`alloc_shared`/`alloc_fragment`）与 `T.copy` 如何配合 grid 绑定搬运数据，学 [u2-l4（内存层级与显存分配）](u2-l4-memory-hierarchy.md)。
- 想从编译器视角看启动上下文如何被各 pass 消费，后续可读 [u5-l2（transform pass 体系）](u5-2-transform-passes.md) 里的 `materialize_kernel_launch` 与 `split_host_device`。
