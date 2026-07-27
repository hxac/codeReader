# 定义 Kernel：prim_func、Kernel 与 Tensor/Buffer

## 1. 本讲目标

本讲是 DSL 语言基础的第一课。读完本讲后，你应该能够：

- 用 `@T.prim_func`（或外层 `@tilelang.jit`）从零定义一个 tilelang kernel；
- 用 `T.Tensor` / `T.Buffer` 给输入输出张量标注形状与数据类型；
- 用 `with T.Kernel(...)` 声明 kernel 的启动上下文，理解 `grid`（`blockIdx`）与 `threads`（`threadIdx`）的绑定关系；
- 理解「符号维度」的概念，掌握 `T.dynamic` / `T.const` 与运行时具体 shape 的关系。

本讲承接 [u1-l4 第一个 Kernel：Quickstart GEMM 实跑](u1-l4-quickstart-gemm.md)：上一讲你跑通了 GEMM 并粗略认识了 `T.Kernel`、`T.copy`、`T.gemm`；本讲把镜头拉近，逐行拆解「一个 kernel 是怎么被声明出来的」。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **GPU 启动模型**：一个 kernel 会用「网格（grid）× 线程块（block）× 线程（thread）」的三级结构启动。`blockIdx.{x,y,z}` 标识网格里哪一个线程块，`threadIdx.{x,y,z}` 标识块内哪一个线程。
- **TIR / PrimFunc**：tilelang 构建在 TVM 之上，kernel 的中间表示是 TVM 的 TIR（Tensor IR）。一个 kernel 在 TIR 层面就是一个 `PrimFunc`（基本函数）。
- **「执行时机」的两层**：你写的 Python 函数体在被装饰时会「执行一次」来搭建 IR（这是编译期行为），而不是每次调用 kernel 都执行。这一点在上一讲已经体现，本讲会再次强调。

关键术语速查：

| 术语 | 含义 |
| --- | --- |
| `PrimFunc` | TVM 的「基本函数」IR 节点，一个 kernel 对应一个 PrimFunc |
| `Tensor` / `Buffer` | tilelang 里描述「一段带形状和 dtype 的显存」的抽象 |
| `T.Kernel` | 声明 kernel 启动上下文：网格大小、每块线程数，并产出 `blockIdx` 绑定 |
| 符号维度 | 编译期未知、运行时由实际张量推断出来的维度（如动态 batch） |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py) | 实现 `T.Kernel` / `KernelLaunchFrame`，管理 block/thread 绑定 |
| [tilelang/language/proxy.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py) | 实现 `T.Tensor` / `T.Buffer` 等「张量代理」，把标注变成 TIR Buffer |
| [tilelang/language/common.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py) | 语言门面，把各子模块（kernel、proxy、loop 等）汇聚成 `tilelang.language` |
| [tilelang/language/symbolics.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/symbolics.py) | 提供 `T.dynamic`（符号维度变量） |
| [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) | eager builder，实现 `@T.prim_func`、`T.const`，把 Python 函数体搭建成 TIR |
| [docs/programming_guides/language_basics.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/language_basics.md) | 官方语言基础指南，含向量加法完整示例 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 用 `@T.prim_func` 定义 Kernel** —— 一个 Python 函数如何变成 TIR PrimFunc；
2. **4.2 `T.Tensor` / `T.Buffer`：张量与缓冲的标注** —— 形状与 dtype 怎么写、背后生成什么；
3. **4.3 `with T.Kernel(...)`：启动上下文与网格/线程绑定** —— grid/threads 与 blockIdx 的对应；
4. **4.4 符号维度：`T.dynamic` / `T.const`** —— 编译期未知维度如何处理。

### 4.1 用 `@T.prim_func` 定义 Kernel

#### 4.1.1 概念说明

在 tilelang 里，**一个 kernel 就是一个 TIR PrimFunc**。你用 Python 写一个普通函数，给参数加上 `T.Tensor(...)` 这样的类型标注，再用 `@T.prim_func` 装饰它——装饰器会在编译期「执行一次」函数体，把里面的 `T.Kernel`、`T.copy`、循环等语句逐步搭建成一棵 TIR AST，最终产出一个 `PrimFunc` 对象。

要理解的关键点是：**函数体里写的是「搭建 IR 的指令」，而不是「运行时的计算流程」**。例如 `with T.Kernel(...) as bx:` 在搭建期注册了一个启动上下文并产出一个表示 `blockIdx.x` 的变量 `bx`；这个 `bx` 是一个 TIR 符号变量，会被编进最终 kernel。

有两种常见的写法：

- **lazy 风格**：外层 `@tilelang.jit` 返回一个 Python 函数，函数体内嵌套一个 `@T.prim_func` 函数；调用时返回一个 kernel 对象。维度（如 `N`）作为 Python 整数从外层传入，被「烤进（bake in）」生成的 TIR。
- **eager 风格**：直接用 `@tilelang.jit` 装饰一个函数，参数本身用 `T.Tensor` 标注、用 `T.const` 声明动态维度；调用时即编译即执行，直接返回结果张量。

本讲的「代码实践」用 lazy 风格（与官方指南一致），4.4 节会展示 eager 风格里的 `T.const`。

#### 4.1.2 核心流程

```text
@T.prim_func 装饰
   │
   ▼
mutate(func): 用 AST 分析函数，得到 IRGenerator
   │
   ▼
Builder() 进入 prim_func 上下文，逐语句执行函数体
   │   （T.Kernel / T.copy / for 循环 … 都在「搭建 IR」）
   ▼
builder.get()  →  取出搭建好的 PrimFunc
   │
   ▼
返回一个 tvm.tirx.PrimFunc（= 一个 kernel 的 IR）
```

#### 4.1.3 源码精读

`@T.prim_func` 的实现在 eager builder 中。核心逻辑：分析函数签名与类型标注，进入 builder 的 `prim_func` 上下文，执行 `ir_gen.gen(builder)` 把函数体「跑一遍」来生成 IR：

这段代码处理装饰过程：取出签名、用 `mutate(func)` 得到 IR 生成器、收集类型标注，最后用 builder 搭建并 `get()` 出 PrimFunc —— [tilelang/language/eager/builder.py:1505-1545](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1505-L1545)。

注意其中 `builder = Builder()` 与 `with builder.prim_func(func.__name__):` 两行——这就是「搭建期执行函数体」的入口；如果函数体里用了 `T.Kernel`，此时就会触发 kernel 启动上下文的搭建（见 4.3）。

官方指南给出的最小定义形态如下，`@T.prim_func` + `T.Tensor((N,), dtype)` 标注就是声明一个 kernel 的全部要求 —— [docs/programming_guides/language_basics.md:26-34](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/language_basics.md#L26-L34)：

```python
@T.prim_func
def add_kernel(
    A: T.Tensor((N,), dtype),    # dtype 可以是 'float32' | T.float32 | torch.float32
    B: T.Tensor((N,), dtype),
    C: T.Tensor((N,), dtype),
):
    ...  # kernel body
```

> 小提示：`@T.prim_func` 本身不要求 GPU，它只是「Python → TIR」的搭建器。真正决定跑在哪块硬件上的是编译时的 `target`（见 [u4-l4 目标判定与多后端支持](u4-l4-targets-and-backends.md)）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「函数体在装饰期被执行一次」。

1. 打开 [tilelang/language/eager/builder.py:1505-1545](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1505-L1545)。
2. 在 `prim_func` 函数体里临时（仅本地阅读，不改源码）找到 `ir_gen.gen(builder)(**annot)` 这一行，理解它就是在「调用用户函数」以搭建 IR。
3. **观察现象**：你会发现 `prim_func` 里并没有真正去 GPU 上执行任何东西，它只是产出 `builder.get()` 返回的 IR 对象。这就是「搭建期」与「运行期」的分离。

**预期结果**：能用自己的话讲清「为什么 kernel 函数体里能写 `for`、`with` 这些 Python 语句，但它们不会在每次调用 kernel 时重新执行」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@T.prim_func` 去掉，直接调用函数会怎样？
**参考答案**：函数会作为普通 Python 函数执行，`T.Kernel(...)` 等语句因为找不到 builder 上下文会抛出 `JITNoBuilderError`（见 4.3.3 里 `Builder.current() is None` 的检查）。

**练习 2**：`@T.prim_func` 搭建出的对象，其 Python 类型是什么？
**参考答案**：是 `tvm.tirx.PrimFunc`（见 [builder.py:779-790](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L779-L790)），即 TVM 的基本函数 IR 节点。

---

### 4.2 `T.Tensor` / `T.Buffer`：张量与缓冲的标注

#### 4.2.1 概念说明

`T.Tensor` 是 tilelang 里描述「一段带形状（shape）和数据类型（dtype）的内存」的抽象。kernel 的输入输出参数用它来标注，例如 `A: T.Tensor((M, N), "float16")` 表示 A 是一个 M×N 的 float16 张量。`T.Buffer` 是更早期的名字，目前已被标记为**已弃用**，官方推荐用 `T.Tensor`，二者最终都生成同一种 TIR Buffer 对象。

要点：

- **默认 scope 是 `global`**：用 `T.Tensor` 标注的参数默认在显存（device memory）里，对应 CUDA 的全局内存。片上内存（shared/fragment）由 `T.alloc_shared` / `T.alloc_fragment` 另行分配（见 [u2-l2 内存层级与数据搬运](u2-l2-memory-hierarchy-and-copy.md)）。
- **dtype 三种等价写法**：字符串 `"float32"`、tilelang 常量 `T.float32`、框架常量 `torch.float32` 都可以，tilelang 会统一归一化（类型系统的细节在 [u2-l4 类型系统与数据类型](u2-l4-type-system-and-dtypes.md)）。
- **`T.Tensor` 是「代理」**：它本身是一个可调用对象（`TensorProxy` 的实例），用 `T.Tensor(shape, dtype)` 或 `T.Tensor[shape, dtype]` 都能生成 TIR Buffer。

#### 4.2.2 核心流程

```text
T.Tensor((M, N), "float16")
   │  (TensorProxy.__call__)
   ▼
构造 row-major strides（默认连续）
   │
   ▼
BaseTensorProxy.__call__ → scope 默认 "global"
   │
   ▼
tvm tir buffer(shape, dtype=..., scope="global", ...)
   │
   ▼
得到一个 tirx.Buffer（一段带形状/dtype/scope 的显存抽象）
```

#### 4.2.3 源码精读

`T.Tensor` 实际上是 `TensorProxy` 的实例，它会在标注被求值时生成一个 TIR Buffer。`TensorProxy` 默认 scope 为 `global`，并自动按 row-major 构造 stride（默认连续）—— [tilelang/language/proxy.py:150-168](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py#L150-L168)：

```python
class TensorProxy(BaseTensorProxy):
    """Main tensor proxy class for global scope buffers.
    ... the tensor should be by default contiguous."""
    @staticmethod
    def _construct_strides(shape): ...
    def __call__(self, shape, dtype="float32", data=None, scope=None):
        if isinstance(shape, (int, PrimExpr)):
            shape = (shape,)
        return super().__call__(shape, dtype=dtype,
                                strides=TensorProxy._construct_strides(shape),
                                data=data, scope=scope)
```

所有「张量代理」的公共基类 `BaseTensorProxy` 统一调用 TVM 的 `buffer(...)`，并用类属性给出默认 scope —— `TensorProxy` 继承的默认 `default_scope = "global"` —— [tilelang/language/proxy.py:91-124](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py#L91-L124)。其它 scope 的代理也在同一文件里：`SharedBufferProxy`（`shared.dyn`）、`FragmentBufferProxy`（`local.fragment`）、`LocalBufferProxy`（`local`），它们与 4.3 之后的 `alloc_shared`/`alloc_fragment` 对应。

模块底部把代理实例化为 `Tensor`、`Buffer` 等名字 —— 注意 `Buffer` 已被标注为弃用 —— [tilelang/language/proxy.py:213-264](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py#L213-L264)：

```python
Buffer = BufferProxy()
...
Tensor = TensorProxy()
StridedTensor = StridedTensorProxy()
FragmentBuffer = FragmentBufferProxy()
SharedBuffer = SharedBufferProxy()
LocalBuffer = LocalBufferProxy()
```

最后，这些名字通过语言门面 `common.py` 暴露为 `T.Tensor` / `T.Buffer` —— [tilelang/language/common.py:20](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L20)（`from .proxy import ptr, make_tensor, ..., Buffer, Tensor, StridedTensor, FragmentBuffer, SharedBuffer, LocalBuffer`）。这也是为什么 `import tilelang.language as T` 之后 `T.Tensor` 就可用的原因。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证 `T.Tensor` 与 `T.Buffer` 生成同一种对象，且 scope 默认是 global。

1. 阅读 [proxy.py:18-56](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py#L18-L56)，注意 `BufferProxy` 上的 `@deprecated("T.Buffer(...)", "T.Tensor(...)")` 装饰器——它会在使用时打印弃用警告。
2. 对比 `TensorProxy`（L150）与 `SharedBufferProxy`（L193 附近）的 `default_scope`，体会「同一个基类、不同 scope」的设计。

**预期结果**：能回答「为什么 `T.Tensor` 默认在 global 显存，而 `T.alloc_shared` 分配的在 shared 显存」——因为二者用了不同的 scope 代理。

> 待本地验证：如果你有 GPU 环境，可以打印 `T.Tensor((4,4),"float32").scope()` 看是否为 `global`（需在 builder 上下文内）。

#### 4.2.5 小练习与答案

**练习 1**：`T.Tensor((8,), "float32")` 和 `T.Tensor((8,), T.float32)` 有区别吗？
**参考答案**：没有实质区别。`T.float32` 是 tilelang 的 dtype 常量，最终都会被归一化成 TVM 能识别的 `"float32"`。

**练习 2**：为什么 `T.Buffer` 仍能工作但不推荐？
**参考答案**：`BufferProxy` 上挂了 `@deprecated` 装饰器（[proxy.py:22](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/proxy.py#L22)），它仍会生成 TIR Buffer，但会提醒你改用 `T.Tensor`，未来版本可能移除。

---

### 4.3 `with T.Kernel(...)`：启动上下文与网格/线程绑定

#### 4.3.1 概念说明

`with T.Kernel(...)` 用来声明 kernel 的**启动上下文**：告诉编译器「这个 kernel 要用多大的网格、每块多少线程」，并给你返回表示 `blockIdx.{x,y,z}` 的变量。

最常见用法：

```python
with T.Kernel(T.ceildiv(N, block), threads=block) as bx:
    # bx 就是 blockIdx.x
    ...
```

- `*blocks` 是 1~3 个整数（或符号表达式），表示 `gridDim.(x|y|z)`，即网格在每一维上有多少个线程块。
- `threads` 表示 `blockDim`：传一个整数就是 `blockDim.x`；传列表/元组 `(tx, ty)` 可指定多维权程。
- `as` 后面的变量就是 `blockIdx` 绑定：单维时可直接写 `as bx`，多维时写成 `as (bx, by)`。

`T.ceildiv(a, b)` 是「向上取整除法」，用来在 tile 不能整除问题规模时算出足够的网格数：

\[
\text{grid} = \left\lceil \frac{N}{\text{block}} \right\rceil = \left\lfloor \frac{N + \text{block} - 1}{\text{block}} \right\rfloor
\]

#### 4.3.2 核心流程

```text
with T.Kernel(grid_x, grid_y, threads=128) as (bx, by):
   │  (_ffi_api.KernelLaunch 创建 KernelLaunchFrame)
   ▼
__enter__: 把 frame 压栈
   │  frames 结构：[blockIdx 维度…, threadIdx.x, threadIdx.y, threadIdx.z, 属性行]
   ▼
返回 [frames[0:-4] 的第 0 个 var]  →  即 (bx, by)
   │
   ▼
with 块内的语句被搭建成「在每个 block 内执行」的 IR
   │
__exit__: 弹栈
```

这里有个关键设计：`KernelLaunchFrame` 内部用一个 `frames` 列表，**前若干个 frame 是 `blockIdx` 维度，最后 4 个 frame 分别是 `threadIdx.x/y/z` 和一个带属性的块帧**。所以「blockIdx 绑定」就是 `frames[0:-4]` 里每个 frame 的循环变量。

#### 4.3.3 源码精读

`T.Kernel` 是一个普通函数，它先校验「当前必须在 builder 上下文里」，再把 `threads` 归一化成三维，最后调用 C++ 侧的 `KernelLaunch` 创建启动帧 —— [tilelang/language/kernel.py:277-340](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L277-L340)：

```python
def Kernel(*blocks, threads=None, prelude=None):
    from tilelang.language.eager.builder import Builder
    if Builder.current() is None:
        raise JITNoBuilderError("T.Kernel() can only be used inside "
                                "@tilelang.jit or @T.prim_func context. ...")
    attrs = {}
    threads = _normalize_threads(threads)
    if prelude is not None:
        attrs["pragma_import_c"] = prelude
    return _ffi_api.KernelLaunch(blocks, threads, attrs)
```

注意第一处校验：`Builder.current() is None` 时直接报错——这就是「`T.Kernel` 必须在 `@T.prim_func` / `@tilelang.jit` 内使用」的来源。

`threads` 的归一化把「整数 / 列表 / 元组 / None」统一成三维 `[x, y, z]`，`None` 时默认 128，并禁止非正值（否则会启动一个空线程块的 kernel，静默什么都不写）—— [tilelang/language/kernel.py:98-130](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L98-L130)。

真正的「blockIdx 绑定」魔法发生在 `KernelLaunchFrame.__enter__`：它把自身压入线程局部的栈，然后取 `frames[0:-4]`（跳过末尾的 threadIdx.x/y/z 和属性行）的第一个变量返回给 `as` —— [tilelang/language/kernel.py:156-170](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L156-L170)：

```python
def __enter__(self):
    super().__enter__()
    _get_current_stack().push(self)
    last_block_frame = self.frames[-1]
    assert isinstance(last_block_frame, SBlockFrame), ...
    # 返回 grid 循环变量（排除末尾 4 个 frame：
    # threadIdx.x, threadIdx.y, threadIdx.z 和带属性的 block frame）
    return _normalize_bindings([frame.vars[0] for frame in self.frames[0:-4]])
```

`_normalize_bindings` 的作用是：当只有单个 block 维度时，直接返回那个 `Var`（于是你能写 `as bx` 而不必写 `as (bx,)`）；多维时返回列表 —— [tilelang/language/kernel.py:87-95](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L87-L95)。这就是为什么单维和多维 `with T.Kernel(...) as ...` 写法不同的原因。

> 顺带一提：文件顶部还给 `tvm.tirx.Var` 打了 `__iter__`/`__len__` 补丁（[kernel.py:18-26](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L18-L26)），让单维绑定既能当标量用、也能当可迭代对象用，解决了一个真实的解包问题（issue #830）。

#### 4.3.4 代码实践（参数观察型）

**目标**：直观感受 grid 与 threads 的归一化。

1. 阅读官方指南里 2-D grid + 多维权程的例子 —— [docs/programming_guides/language_basics.md:87-91](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/language_basics.md#L87-L91)：

   ```python
   with T.Kernel(grid_x, grid_y, threads=128) as (bx, by):
       ...  # bx/by are blockIdx.x/y
   ```

2. 把本讲综合实践（第 5 节）里的 `threads=block` 改成 `threads=(64, 2)`，重新编译。
3. **观察现象**：调用 `kernel.get_kernel_source()`（见 [u1-l4](u1-l4-quickstart-gemm.md)）查看生成代码里 `blockDim.x / blockDim.y` 的变化。

**预期结果**：`threads` 传整数与传元组时，生成 kernel 的 `blockDim` 维度数量不同，但 `blockIdx` 绑定数量只由 `*blocks` 的个数决定。

> 待本地验证：具体生成的 CUDA 源码细节依赖你的 GPU 与 target，可在本地编译后确认。

#### 4.3.5 小练习与答案

**练习 1**：`with T.Kernel(10, threads=128) as bx:` 里 `bx` 的取值范围是多少？
**参考答案**：`bx` 表示 `blockIdx.x`，取值范围是 `[0, 10)`，即 0~9，共 10 个线程块。

**练习 2**：为什么 `T.Kernel` 不能在 `@T.prim_func` / `@tilelang.jit` 之外调用？
**参考答案**：因为它依赖 builder 上下文（`Builder.current()`）来搭建 IR；在外面调用时 `Builder.current() is None`，会抛 `JITNoBuilderError`（见 [kernel.py:331-332](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L331-L332)）。

---

### 4.4 符号维度：`T.dynamic` / `T.const`

#### 4.4.1 概念说明

很多算子的维度在**写 kernel 时是未知的**（比如 batch size、序列长度），要到运行时拿着真实张量才能确定。tilelang 用「符号维度」来描述这种「编译期未知、运行时确定」的维度，并提供两种**已在本仓库源码中验证**的机制：

1. **`T.dynamic(name, dtype="int32")`**：创建一个 TIR `Var`，可直接用在表达式和循环边界里（term-level 符号变量）。这是 `tilelang.language` 实际导出的接口（见 `symbolics.py`）。
2. **`T.const("M, N")`**（eager 模式专用）：在 `@tilelang.jit` 的 eager 风格里声明「运行时从实际张量推断」的形状常量。这是当前 examples 中**最常见**的写法。

> 关于 `T.dyn['K']`：官方 `language_basics.md` 还文档化了一种类型层（type-level）的 `T.dyn['K']` 注解简写。本讲作者在当前 HEAD（`c6294f07`）的 `tilelang/language` 包内**未能定位到 `dyn` 的定义/导出**，因此对其可用性标注为「待确认 / 以官方文档与本地 `dir(T)` 为准」。可验证的写法以上面两条为准。

两者的关系可以这样理解：

- **具体 shape**（如 `1024`）：编译期完全已知，会被烤进 IR；换一个 shape 通常要重新编译。
- **符号维度**（如 `T.dynamic("N")` 或 `T.const("N")`）：编译期用一个符号占位，运行时绑定到真实尺寸，从而支持「一次编译、多种 shape」。

#### 4.4.2 核心流程

```text
# 方式 A：T.dynamic（term-level 符号变量）
K = T.dynamic("K")           # 产出一个 tirx.Var("K", "int32")
@T.prim_func
def foo(A: T.Tensor((K,), "float32")):
    for i in T.serial(K):     # K 可直接用在循环边界
        ...

# 方式 B：T.const（eager 模式，运行时从张量推断）
@tilelang.jit
def add(A, B, ...):
    M, N = T.const("M, N")    # 声明两个形状符号
    A: T.Tensor((M, N), in_dtype)
    ...
```

`T.const` 的两阶段行为（见源码）：在 eager JIT 的「phase1」阶段创建 constexpr 变量占位；在「phase2」阶段用真实张量的尺寸替换它们。

#### 4.4.3 源码精读

`T.dynamic` 的实现非常直接：返回一个带名字和 dtype 的 TIR `Var`；名字里含逗号或空格时还能一次创建多个变量 —— [tilelang/language/symbolics.py:13-30](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/symbolics.py#L13-L30)：

```python
def dynamic(name: str, dtype: DType = "int32") -> tuple[tirx.Var, ...] | tirx.Var:
    if "," in name:
        names = re.split(r"\s*,\s*", name)
        return tuple(tirx.Var(n, dtype) for n in names)
    if " " in name:
        names = re.split(r"\s+", name)
        return tuple(tirx.Var(n, dtype) for n in names)
    return tirx.Var(name, dtype)
```

同文件里 `T.symbolic` 是 `T.dynamic` 的**已弃用别名** —— [tilelang/language/symbolics.py:33-36](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/symbolics.py#L33-L36)（`@deprecated("T.symbolic(...)", "T.dynamic(...)", "v0.1.9")`），所以新代码请用 `T.dynamic`。

`T.const` 是 eager 模式专属，文档明确写了它的用途（声明运行时从张量推断的形状维度），并在 phase1 创建 constexpr 变量 —— [tilelang/language/eager/builder.py:922-952](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L922-L952)：

```python
def const(name: str, dtype: str = "int32") -> Var | tuple[Var, ...]:
    """
    Declare constexpr variables for dynamic tensor dimensions (eager mode only).
    In eager mode, use T.const() to declare shape dimensions that will be
    inferred from actual tensor arguments at runtime.
    ...
    """
    builder = Builder.current()
    if builder is None or builder.eager_jit == "none":
        raise JITNoBuilderError("T.const() can only be used inside @tilelang.jit (eager mode)")
    if builder.eager_jit == "phase1":
        # in stage 1, we create constexpr variables
        if "," in name:
            names = re.split(r"\s*,\s*", name)
            return tuple(builder.constexpr(n, dtype) for n in names)
        ...
```

`T.const` 同样支持「逗号/空格分隔一次声明多个」。它在 phase1 调用的 `builder.constexpr` 就是创建一个普通 TIR `Var` 并登记为 constexpr —— [tilelang/language/eager/builder.py:754-758](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L754-L758)。

官方文档对「动态符号维度」的两种写法（类型层 `T.dyn[...]` 与 term-level `T.dynamic(...)`）的描述可参考 —— [docs/programming_guides/language_basics.md:48-75](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/language_basics.md#L48-L75)。

仓库里真实使用 `T.const` 的 eager 风格示例（elementwise add）：用 `M, N = T.const("M, N")` 声明形状，再用 `T.Tensor((M, N), in_dtype)` 标注 —— [examples/elementwise/example_elementwise_add.py:13-17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L13-L17)：

```python
@tilelang.jit
def elementwise_add(A, B, block_M, block_N, in_dtype, out_dtype, threads):
    M, N = T.const("M, N")
    A: T.Tensor((M, N), in_dtype)
    B: T.Tensor((M, N), in_dtype)
    C = T.empty((M, N), out_dtype)
    ...
```

#### 4.4.4 代码实践（对比型）

**目标**：对比「具体 shape」与「符号维度」对重编译的影响。

1. 用 lazy 风格写一个 `add(N: int)`，把 `N` 当作编译期常量烤进去（见第 5 节综合实践）。
2. 再仿照上面的 `elementwise_add` 用 `T.const("M, N")` 写一个 eager 版本。
3. **观察现象**：
   - lazy 版：调用 `add(1024)` 和 `add(2048)` 通常会触发**两次**编译（N 不同 → IR 不同）。
   - eager 版：用不同尺寸的张量调用同一个被 `@tilelang.jit` 装饰的函数，形状符号在运行时绑定。

**预期结果**：能说清「为什么 lazy 把 Python 整数烤进 IR，而 eager 用 `T.const` 延后到运行时」。

> 待本地验证：重编译行为还受 JIT 缓存影响（见 [u4-l3 编译缓存机制](u4-l3-compilation-cache.md)），精确次数以本地计时为准。

#### 4.4.5 小练习与答案

**练习 1**：`T.symbolic` 和 `T.dynamic` 是什么关系？
**参考答案**：`T.symbolic` 是 `T.dynamic` 的已弃用别名（自 v0.1.9 起），新代码应使用 `T.dynamic`（见 [symbolics.py:33-36](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/symbolics.py#L33-L36)）。

**练习 2**：`T.const` 能在「没有 `@tilelang.jit`」的普通函数里用吗？
**参考答案**：不能。`T.const` 是 eager 模式专用，`builder is None or builder.eager_jit == "none"` 时会抛 `JITNoBuilderError`（见 [builder.py:940-941](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L940-L941)）。

**练习 3**：把一个具体维度（如 `N=1024`）改成符号维度，会带来什么代价与好处？
**参考答案**：好处是「一次编译、多种 shape」，不必每个尺寸都重编译；代价是编译器在某些优化上只能按符号推理，可能不如常量尺寸激进（部分边界、展开决策会保守）。

---

## 5. 综合实践

把本讲四个模块串起来：写一个 **elementwise 向量加法 kernel（A+B→C）**，用 `@tilelang.jit` 装饰，传入 torch tensor 调用，并断言结果与 `torch` 一致。这个例子直接对应官方指南的最小端到端示例 —— [docs/programming_guides/language_basics.md:161-193](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/language_basics.md#L161-L193)。

### 5.1 实践目标

- 用 `@tilelang.jit` + 内嵌 `@T.prim_func` 定义 kernel（模块 4.1）；
- 用 `T.Tensor((N,), dtype)` 标注输入输出（模块 4.2）；
- 用 `with T.Kernel(T.ceildiv(N, block), threads=block) as bx` 声明启动上下文（模块 4.3）；
- 体会 `N` 作为编译期常量被烤进 IR（模块 4.4）。

### 5.2 操作步骤

新建一个 `vec_add.py`（放在你自己的工作目录，不要改动仓库源码），写入下面**示例代码**（基于官方指南改写，已标注为示例代码）：

```python
# 示例代码（基于 docs/programming_guides/language_basics.md 的向量加法示例）
import torch
import tilelang
import tilelang.language as T
from tilelang import jit

@jit  # lazy 风格：返回 PrimFunc，N 作为编译期常量被烤进 IR
def add(N: int, block: int = 256, dtype: str = "float32"):

    @T.prim_func
    def add_kernel(
        A: T.Tensor((N,), dtype),   # 模块 4.2：T.Tensor 标注
        B: T.Tensor((N,), dtype),
        C: T.Tensor((N,), dtype),
    ):
        # 模块 4.3：声明启动上下文，bx = blockIdx.x
        with T.Kernel(T.ceildiv(N, block), threads=block) as bx:
            for i in T.Parallel(block):
                gi = bx * block + i
                # 模块 4.1：这里的语句在“搭建期”被翻译成 TIR
                C[gi] = A[gi] + B[gi]

    return add_kernel

# Host 侧调用
N = 1 << 20
A = torch.randn(N, device="cuda", dtype=torch.float32)
B = torch.randn(N, device="cuda", dtype=torch.float32)
C = torch.empty(N, device="cuda", dtype=torch.float32)

kernel = add(N)        # 首次调用触发编译
kernel(A, B, C)        # 在 GPU 上运行
torch.testing.assert_close(C, A + B)
print("向量加法正确性校验通过 ✔")
```

> 进阶：想看 eager 风格（`T.const`），可参考 [examples/elementwise/example_elementwise_add.py:11-32](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L11-L32)，它额外用 shared/fragment 缓冲搬运数据。

### 5.3 需要观察的现象

1. **编译发生在哪一步**：`add(N)` 返回的是一个 kernel 对象（lazy 模式），真正发起 GPU 计算的是 `kernel(A, B, C)`。
2. **`bx * block + i` 的含义**：把「block 索引 × 块大小 + 块内偏移」映射成全局元素下标 `gi`，这是 tile 化 kernel 最基本的下标换算。
3. **越界保护**：上面没有写 `if gi < N`。官方注释指出，当访问可能越界时，`LegalizeSafeMemoryAccess` 这个 Pass 会自动插入边界 guard（详见 [u2-l3 控制流与循环原语](u2-l3-control-flow-and-loops.md)）。

### 5.4 预期结果

- 程序打印「向量加法正确性校验通过 ✔」，`C` 与 `A + B` 数值一致。
- 想查看生成的设备代码，可调用（参见 [u1-l4](u1-l4-quickstart-gemm.md)）：`print(add(N).get_kernel_source())`，应能看到含 `blockIdx.x`、`threadIdx.x` 的 CUDA kernel。

### 5.5 无 GPU 环境的退路

> 待本地验证：本实践需要 CUDA 环境。若无 GPU，可尝试把 host 张量放到 CPU 并在编译时指定 `target="llvm"`（target 机制见 [u4-l4](u4-l4-targets-and-backends.md)）；具体能否在 CPU 上直接跑通以本地验证为准。退而求其次，也可只做源码阅读：对照上面的 `with T.Kernel` 与第 4.3 节的 frame 结构，画出 `bx` 是如何从 `frames[0:-4]` 里取出来的。

---

## 6. 本讲小结

- **kernel = PrimFunc**：`@T.prim_func`（或外层 `@tilelang.jit`）在编译期把 Python 函数体「搭建」成 TIR PrimFunc，函数体写的是搭建 IR 的指令。
- **`T.Tensor` 标注显存参数**：默认 scope 是 `global`，由 `TensorProxy` 生成 TIR Buffer；`T.Buffer` 是已弃用别名。
- **`T.Kernel` 声明启动上下文**：`*blocks` 决定 `gridDim`/`blockIdx` 维度，`threads` 决定 `blockDim`；绑定变量来自 `KernelLaunchFrame` 的 `frames[0:-4]`。
- **单维与多维绑定**：单维可直接 `as bx`，多维写 `as (bx, by)`，由 `_normalize_bindings` 处理。
- **符号维度有两种已验证机制**：term-level 的 `T.dynamic`（生成 TIR `Var`）与 eager 模式的 `T.const`（运行时从张量推断形状）。
- **必须在 builder 上下文内**：`T.Kernel` / `T.const` 都依赖 `Builder.current()`，否则抛 `JITNoBuilderError`。

## 7. 下一步学习建议

- 学完本讲，你已经能定义 kernel 的「骨架」与输入输出。下一讲 **[u2-l2 内存层级与数据搬运](u2-l2-memory-hierarchy-and-copy.md)** 会进入 `T.alloc_shared` / `T.alloc_fragment` / `T.copy`，把数据在 global→shared→fragment 之间搬动——这是 tilelang 区别于 Triton 的核心能力。
- 如果你更想先搞清楚「编译到底怎么跑的」，可以跳到 **[u4-l1 编译总流程：DSL → TIR → 后端](u4-l1-compile-pipeline-overview.md)**，再回头看本讲的 `@T.prim_func` 如何接入 `tilelang.lower()`。
- 想深入「`T.dyn` 与类型层动态维度」的读者，建议在本地用 `dir(tilelang.language)` 核对可用属性，并继续跟踪 [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) 里 `T.const` 的 phase2 替换逻辑。
