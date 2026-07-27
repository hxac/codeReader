# Tile 声明与显存层级

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说出 GPU 显存的几个关键层级（global / shared / fragment-local / local-register），以及它们各自的容量、带宽、可见性特征。
2. 掌握 TileLang 提供的 tile 分配原语 `T.alloc_shared` / `T.alloc_fragment` / `T.alloc_local` / `T.empty`，并知道每类分配落在哪一层显存。
3. 区分 `fragment`（寄存器、可映射到 tensor core）与 `local`（线程私有标量）这两种容易混淆的分配。
4. 看懂 `T.Tensor` / `T.Buffer` / `make_tensor` 等「代理对象」的作用：它们主要用来声明 kernel 参数（默认在 global），其 `scope` 与 `alloc_*` 共用同一套命名。
5. 写出一个把 global 数据搬到 shared、再 reduce 到 fragment 的最小 kernel。

## 2. 前置知识

在开始前，请确认你了解下面这些概念（不熟悉的术语本讲会顺带解释）：

- **GPU 线程与块**：一个 kernel 由若干线程块（block / CTA）组成，每个块里又有若干线程（thread）。`blockIdx.x/y/z` 标识块，`threadIdx.x/y/z` 标识块内线程。这是上一讲 [u2-l1 T.Kernel](u2-l1-kernel-launch.md) 的内容。
- **显存（device memory）**：显卡上的存储。离计算单元越近、越小、越快；越远、越大、越慢。本讲的核心就是讲清楚「数据该放在哪一层」。
- **TIR / Buffer**：TileLang 建立在 TVM 之上，kernel 最终被解析成 TVM 的中间表示 TIR。TIR 里描述一块连续存储的对象叫 **Buffer**。本讲里所有 `T.alloc_*` 和 `T.Tensor` 返回的都是 TIR 的 `tir.Buffer`。
- **scope（存储域）**：TVM 用一个字符串标记一块 Buffer 属于哪一层显存，例如 `"global"`、`"shared.dyn"`、`"local.fragment"`。这是连接「Python 语法」与「底层硬件存储」的钥匙。

> 本讲承接 [u2-l1 T.Kernel](u2-l1-kernel-launch.md)——你已经会用 `T.Kernel(...)` 声明启动上下文，本讲讲的是进入这个上下文之后，如何为数据申请各级显存。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/allocate.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py) | 提供 `alloc_shared/alloc_fragment/alloc_local/alloc_var/empty` 等 tile 分配原语，每个函数封装 `T.alloc_buffer` 并写死对应的 `scope`。 |
| [tilelang/language/proxy.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py) | 提供 `Tensor/Buffer/FragmentBuffer/SharedBuffer/LocalBuffer` 等「代理对象」与 `make_tensor`，用于声明 kernel 参数和按指针构造 Buffer。 |
| [tilelang/language/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py) | 把上述函数/对象挂到 `tilelang.language`（惯用别名 `T`）命名空间，所以我们才能写 `T.alloc_shared`、`T.Tensor`。 |
| [tilelang/language/v2/builder.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py) | 定义 `OutTensor`，这是 `T.empty(...)` 的返回类型，用来在 JIT 工厂函数里声明「输出张量」。 |
| [docs/programming_guides/language_basics.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/language_basics.md) | 官方语言基础文档，其中「Memory Scopes and Allocation」一节是本讲的权威依据。 |
| [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) | matmul+relu 示例，展示了 shared / fragment 分配的典型用法。 |

---

## 4. 核心概念与源码讲解

### 4.1 GPU 显存层级与 tile 分配的对应关系

#### 4.1.1 概念说明

写 GPU kernel 的核心难题之一是：**数据放在哪一层显存**。TileLang 用一个字符串 `scope` 把「抽象的 tile」绑定到「具体的硬件存储」。我们先建立直觉，再讲源码。

一个 tile 计算程序里，数据通常经历这样的流动：

```
global memory (HBM/GDDR，显存，大而慢)
        │  T.copy（global → shared）
        ▼
shared memory (片上，块内所有线程可见，小而快)
        │  T.copy / T.gemm（shared → fragment）
        ▼
fragment / local (寄存器，线程私有，最快)
```

各层的关键特征对比（以 NVIDIA GPU 为典型，AMD/其他架构概念相通）：

| scope 字符串 | 硬件位置 | 可见性 | 典型容量 | 典型用途 |
| --- | --- | --- | --- | --- |
| `global` | 显存（HBM/GDDR） | 所有线程 | 数 GB ~ 数十 GB | kernel 的输入/输出张量 |
| `shared.dyn` / `shared` | 片上共享内存 | 同一 block 内所有线程 | 每 block 数 KB ~ 数百 KB | block 内线程协作、暂存 tile |
| `local.fragment` | 寄存器（tensor core fragment） | 线程私有，但布局由 warp 协作 | 寄存器堆 | gemm 累加器、被 tensor core 直接读写 |
| `local` | 寄存器（标量） | 线程私有 | 寄存器堆 | 线程私有标量、临时计算 |
| `local.var` | 寄存器（单元素） | 线程私有 | 寄存器堆 | 计数器、临时变量 |
| `shared.tmem` | Tensor Memory（Hopper+） | warp 级 | 专用片上 | tcgen05 MMA 累加器 |
| `shared.barrier` | 片上 | block 内 | 极小 | mbarrier 异步同步 |

两个最容易混淆的概念，先在这里点明（后续模块还会强调）：

- **`local.fragment` 不是 `local`**。两者都在寄存器里，但 `fragment` 的元素被刻意「打散」分布在 warp 内各线程上，专门用来对接 `wmma` / `wgmma` 等 tensor core 指令；而 `local` 是普通的线程私有标量。在 quickstart 里，`C_local = T.alloc_fragment(...)` 是 gemm 累加器，它会进入 tensor core；如果换成 `alloc_local`，就不能直接喂给 `T.gemm`。

- **`shared.dyn`（动态共享内存）** 是 TileLang 里 shared 的默认 scope。之所以叫 `dyn`，是因为多块 shared 缓冲在编译期会被合并（参见 `MergeSharedMemoryAllocations` pass），运行时共享同一段动态共享内存。

#### 4.1.2 核心流程

一个 tile 级计算的典型数据流（与 quickstart 一致）：

```
1. 在 global 声明输入/输出（kernel 参数，T.Tensor，scope=global）
2. 进入 T.Kernel，按 block 分配 shared tile（T.alloc_shared）       —— 暂存这一块数据
3. T.copy(global → shared)                                            —— 把 tile 搬到片上
4. 分配 fragment 累加器（T.alloc_fragment）                          —— 准备 tensor core 输出
5. T.gemm(shared, shared → fragment)                                 —— 片上计算
6. T.copy(fragment → global)                                          —— 把结果写回显存
```

整个过程中，**数据离计算单元越来越近，也越来越快，但每层容量更小**。TileLang 的工作就是让你用高层 `T.copy` / `T.gemm` 表达这套搬运，再由编译器选具体的 `TMA` / `cp.async` / `mma` 指令。

#### 4.1.3 源码精读

TileLang 的分配原语本质上都很薄——它们都调用 TVM 的 `T.alloc_buffer(shape, dtype, scope=...)`，只是把 `scope` 写死成对应层级的字符串。三个最常用的函数如下。

[allocate.py:40-55](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L40-L55) 是 `alloc_shared`，注意它默认 `scope="shared.dyn"`，并对 `bool` 类型做了一个特例（改用不可合并的 `"shared"`）：

```python
def alloc_shared(shape, dtype, scope="shared.dyn"):
    if dtype == "bool":
        scope = "shared"   # merge smem pass 暂不支持合并 bool
    return T.alloc_buffer(shape, dtype, scope=scope)
```

[allocate.py:58-69](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L58-L69) 是 `alloc_local`，默认 `scope="local"`，用于线程私有标量：

```python
def alloc_local(shape, dtype, scope="local"):
    return T.alloc_buffer(shape, dtype, scope=scope)
```

[allocate.py:72-83](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L72-L83) 是 `alloc_fragment`，默认 `scope="local.fragment"`，这就是能映射到 tensor core 的「寄存器 tile」：

```python
def alloc_fragment(shape, dtype, scope="local.fragment"):
    return T.alloc_buffer(shape, dtype, scope=scope)
```

可以看到三个函数结构几乎一样，**唯一的区别就是 scope 默认值**。也就是说，TileLang 的「显存层级」抽象，最终落到 TVM scope 这一个字符串上。这也解释了为什么 docs 里的描述很简洁——[language_basics.md:119-133](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/language_basics.md#L119-L133) 把这三类分配用几行示例就讲清楚了。

quickstart 里恰好用到了 `shared` 和 `fragment` 两类，[quickstart.py:18-20](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L18-L20)：

```python
A_shared = T.alloc_shared((block_M, block_K), dtype)   # shared.dyn
B_shared = T.alloc_shared((block_K, block_N), dtype)   # shared.dyn
C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)  # local.fragment
```

变量名 `C_local` 容易让人误以为是 `alloc_local`，但实际它是 `alloc_fragment`——这是因为累加器要喂给 `T.gemm`，必须用 fragment。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 scope 字符串如何影响 kernel 生成，建立「分配原语 = 写死 scope 的 alloc_buffer」的直觉。

**操作步骤**：

1. 阅读 [allocate.py:40-83](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L40-L83)，确认 `alloc_shared/alloc_local/alloc_fragment` 的默认 scope。
2. 复制 quickstart.py，在其中临时把 `C_local = T.alloc_fragment(...)` 改成 `T.alloc_local(...)`。
3. 重新运行。

**需要观察的现象**：改成 `alloc_local` 后，`T.gemm(A_shared, B_shared, C_local)` 会因为累加器不在 `local.fragment` scope 而报错或行为异常（fragment 是 gemm 输出的硬性要求）。

**预期结果**：编译阶段报出与 fragment/scope 相关的错误，说明 `T.gemm` 依赖 `alloc_fragment` 而非 `alloc_local`。

> 待本地验证：具体报错信息取决于 TileLang 版本，重点是观察到「scope 不匹配会导致 gemm 不可用」这一现象。验证后请改回 `alloc_fragment`。

#### 4.1.5 小练习与答案

**练习 1**：`alloc_shared` 为什么对 `dtype == "bool"` 做了特例处理？  
**答案**：因为 TileLang 的 `MergeSharedMemoryAllocations` pass（合并多块动态共享内存）目前不支持 `bool` 类型，所以 bool 的 shared buffer 退化为不可合并的静态 `"shared"` scope，避免合并出错。详见 [allocate.py:51-55](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L51-L55) 的注释。

**练习 2**：quickstart 里变量名叫 `C_local`，但它用的是哪个分配原语？为什么不能用另一个？  
**答案**：用的是 `T.alloc_fragment`（scope=`local.fragment`）。因为 `C_local` 是 `T.gemm` 的累加器，必须落在能被 tensor core 直接读写的 fragment 布局上；`alloc_local`（scope=`local`）是线程私有标量，不能作为 gemm 输出。

---

### 4.2 tile 分配原语：alloc_shared / alloc_fragment / alloc_local / empty

#### 4.2.1 概念说明

上一模块讲了三个最常用的分配。本模块把它们和「次要」的分配原语一起讲全，并重点解析两个稍复杂的：`alloc_var`（带初始化的标量变量）和 `empty`（声明输出张量，不是真正的显存分配）。

TileLang 在 [__init__.py:38-51](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L38-L51) 一次性导出了全部分配原语：

| 原语 | 默认 scope | 用途 |
| --- | --- | --- |
| `alloc_shared` | `shared.dyn` | 片上共享，block 内协作 |
| `alloc_local` | `local` | 线程私有标量/临时 |
| `alloc_fragment` | `local.fragment` | tensor core fragment（累加器等） |
| `alloc_var` | `local.var` | 单元素变量（计数器、临时） |
| `alloc_barrier` | `shared.barrier` | mbarrier 异步屏障 |
| `alloc_tmem` | `shared.tmem` | Hopper+ Tensor Memory（tcgen05） |
| `alloc_reducer` | `local.fragment` | 配合 `T.Parallel` 的线程级归约缓冲 |
| `empty` | （非分配） | 声明 JIT 输出张量，返回 `OutTensor` |

> 前六个都真正在 kernel 内部分配显存；`empty` 是个例外——它不分配显存，而是给外层 `@jit` 工厂函数声明「这个返回值是输出张量」，下面会专门讲。

#### 4.2.2 核心流程

`alloc_var` 的流程稍复杂，因为它支持「带初值」：

```
alloc_var(dtype, init=...)
   │
   ├─ T.alloc_buffer([1], dtype, scope="local.var")    # 恒为单元素
   │
   └─ 若指定 init：
        ├─ init 是常量 → block_attr({"tl.local_var_init": {buf.data: dtype(init)}})
        └─ init 是表达式 → T.buffer_store(buf, init, 0)
```

`empty` 的流程则完全是「记录元数据」：

```
empty(shape, dtype) → OutTensor(shape, dtype)   # 仅记录形状与类型，不分配显存
```

`OutTensor` 在 [v2/builder.py:112-118](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/v2/builder.py#L112-L118) 是一个仅含 `shape` / `dtype` 的 dataclass，它告诉 JIT 编译器「请为这个返回值分配一块 global 输出 tensor」。

#### 4.2.3 源码精读

先看 `alloc_var`。[allocate.py:94-148](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L94-L148) 支持 `T.alloc_var('int32', 1)`、`T.alloc_var('int32', 'local.var')`、`T.alloc_var('int32', init=1)` 等多种调用顺序，函数主体先解析位置参数，再据此决定是写入常量初值还是表达式初值：

```python
buffer = T.alloc_buffer([1], dtype, scope=parsed_scope)   # 恒为单元素
if parsed_init is not None:
    if isinstance(parsed_init, (int, float, IntImm, FloatImm)):
        block_attr({"tl.local_var_init": {buffer.data: tl_dtype(dtype)(parsed_init)}})
    else:
        T.buffer_store(buffer, parsed_init, 0)
```

关键点：`alloc_var` 的 shape 恒为 `[1]`（单元素），常量初值走 `tl.local_var_init` 这个 block 属性记录（让编译器在更早阶段插入初值），表达式初值则直接生成一条 `buffer_store`。

再看 `empty`。[allocate.py:271-279](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L271-L279) 把多种传参形式统一归一到 `OutTensor`：

```python
def empty(*shape, dtype: str = _dtypes.float32):
    if len(shape) == 1 and isinstance(shape[0], (tuple, list)):
        return OutTensor(shape[0], dtype)
    elif len(shape) == 2 and isinstance(shape[0], (tuple, list)) and isinstance(shape[1], str):
        return OutTensor(shape[0], shape[1])
    elif all([isinstance(x, (int, PrimExpr)) for x in shape]):
        return OutTensor(shape, dtype)
    else:
        raise RuntimeError(f"Invalid shape {shape}")
```

注意：**`empty` 与 PyTorch/NumPy 的 `empty` 同名但语义完全不同**——它不分配显存，也不在 kernel 内部使用，而是用于 `@jit` 外层工厂函数的返回值声明，等价于「告诉编译器这是一个输出张量」。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试，理解 `alloc_var` 的初始化语义。

**操作步骤**：

1. 在仓库里搜索 `alloc_var` 的使用与测试。
2. 重点看 [testing/python/language/test_tilelang_language_reduce.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/language/test_tilelang_language_reduce.py) 中 `reduce_max_test`（第 32-48 行）——它演示了 `alloc_fragment` 作为 reduce 输入/输出的典型用法。
3. 仿照 `reduce_max_test`，写一段使用 `T.alloc_var('int32', init=0)` 作为循环计数器的伪代码，注释它在 `local.var` scope。

**需要观察的现象**：`alloc_var` 返回的是一个 shape 为 `[1]` 的单元素 Buffer，初值由 `init` 决定。

**预期结果**：你应能描述出 `alloc_var` 与 `alloc_local` 的差别——前者恒为单元素、支持初值、scope 为 `local.var`；后者可以是任意 shape、不支持初值语法、scope 为 `local`。

> 待本地验证：`local.var` 与 `local` 在生成的 CUDA 里是否落到同样的寄存器分配，需对比 `get_kernel_source()` 才能确认。

#### 4.2.5 小练习与答案

**练习 1**：`T.empty((M, N), 'float32')` 返回什么？它有没有在 GPU 上分配显存？  
**答案**：返回一个 `OutTensor(shape, dtype)` 对象，**没有**分配显存。它仅记录「这是一个输出张量」的元数据，供 JIT 编译器在调用时分配并返回该 global tensor。详见 [allocate.py:271-279](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L271-L279)。

**练习 2**：`T.alloc_var('int32', 1)` 和 `T.alloc_var('int32', init=1)` 等价吗？  
**答案**：等价。前者用位置参数传初值，后者用关键字参数；[allocate.py:121-135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L121-L135) 的参数解析会把这两种写法都归一到 `parsed_init=1`。文档示例在 [allocate.py:109-114](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L109-L114)。

---

### 4.3 代理对象：T.Tensor / T.Buffer / make_tensor

#### 4.3.1 概念说明

除了 `alloc_*`（在 kernel 内部分配各级显存），TileLang 还提供一组「代理对象」用于**声明 kernel 参数**和**按指针构造 Buffer**。它们都挂在 `T.*` 命名空间下，由 [__init__.py:17](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L17) 从 `proxy.py` 导入：

```python
from .proxy import ptr, make_tensor, Buffer, Tensor, StridedTensor, FragmentBuffer, SharedBuffer, LocalBuffer
```

需要先厘清三组关系：

1. **`T.Tensor` 与 `T.Buffer`**：`T.Buffer` 是**已废弃**的旧写法（[proxy.py:19](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L19)、[proxy.py:47](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L47) 都标了 `@deprecated`），新代码应统一用 `T.Tensor`。两者都返回 `tir.Buffer`。
2. **`T.Tensor` 默认在 `global`**：它主要用来标注 kernel 参数（输入/输出），这些参数天然在显存里，所以默认 scope 是 `"global"`，并且默认按行优先连续 strides。
3. **scope 共用**：`FragmentBuffer` / `SharedBuffer` / `LocalBuffer` 这几个代理对象的唯一作用，就是把 `default_scope` 改成 `local.fragment` / `shared.dyn` / `local`，等价于「换 scope 的 Tensor」。它们和 `alloc_*` 用的是同一套 scope 命名。

最后，`make_tensor` 用来**从一个已有指针（Var）构造一个 Buffer**，常在底层 FFI 或自定义场景里使用。

#### 4.3.2 核心流程

代理对象的设计核心是 `BaseTensorProxy`：它持有一组类级默认值（`default_scope` / `default_align` / `default_offset_factor`），调用 `__call__` 时把默认值填进去，再转交给 TVM 的 `buffer()` 构造器：

```
Tensor(shape, dtype)
   │
   ├─ TensorProxy._construct_strides(shape)   # 自动算行优先连续 strides
   │
   └─ BaseTensorProxy.__call__(...)
        └─ tvm.script.ir_builder.tir.buffer(shape, dtype, scope="global", strides=...)
                 ↓
              tir.Buffer
```

子类通过覆盖 `default_scope` 来切换层级，无需重写 `__call__`：

```
FragmentBufferProxy : default_scope = "local.fragment"
SharedBufferProxy   : default_scope = "shared.dyn"
LocalBufferProxy    : default_scope = "local"
```

#### 4.3.3 源码精读

`BaseTensorProxy` 是所有代理的基类，[proxy.py:71-111](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L71-L111)。它的 `__call__` 把 `None` 的参数替换成类默认值，再调用 TVM 的 `buffer()`：

```python
class BaseTensorProxy:
    default_scope = "global"
    default_align = 0
    default_offset_factor = 0

    def __call__(self, shape, dtype="float32", ..., scope=None, ...):
        scope = scope or self.default_scope          # 用类默认 scope
        align = align or self.default_align
        offset_factor = offset_factor or self.default_offset_factor
        return buffer(shape, dtype=dtype, ..., scope=scope, ...)
```

`TensorProxy` 在此基础上自动算出连续 strides，[proxy.py:136-154](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L136-L154)：

```python
class TensorProxy(BaseTensorProxy):
    @staticmethod
    def _construct_strides(shape):
        s, strides = 1, [1]
        for dim in shape[:0:-1]:      # 从倒数第二维往前累乘
            s *= dim
            strides.append(s)
        return tuple(reversed(strides))

    def __call__(self, shape, dtype="float32", data=None, scope=None):
        if isinstance(shape, (int, PrimExpr)):
            shape = (shape,)          # 标量 shape 也允许
        return super().__call__(shape, dtype=dtype,
                                strides=TensorProxy._construct_strides(shape),
                                data=data, scope=scope)
```

三个「换 scope」的子类非常薄，只改 `default_scope`：

- [proxy.py:169-176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L169-L176) `FragmentBufferProxy` → `"local.fragment"`
- [proxy.py:179-186](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L179-L186) `SharedBufferProxy` → `"shared.dyn"`
- [proxy.py:189-196](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L189-L196) `LocalBufferProxy` → `"local"`

最后在 [proxy.py:246-250](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L246-L250) 实例化为模块级对象，正是它们被导出为 `T.Tensor` / `T.FragmentBuffer` 等：

```python
Tensor = TensorProxy()
StridedTensor = StridedTensorProxy()
FragmentBuffer = FragmentBufferProxy()
SharedBuffer = SharedBufferProxy()
LocalBuffer = LocalBufferProxy()
```

`make_tensor` 是一个普通函数（不是代理类），[proxy.py:277-278](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L277-L278) 直接转调 `Tensor.from_ptr`：

```python
def make_tensor(ptr, shape, dtype="float32", strides=None):
    return Tensor.from_ptr(ptr, shape, dtype, strides)
```

`from_ptr` 内部用 TVM 的 `match_buffer`，把一个指针 Var 绑定为指定 shape/dtype/strides 的 Buffer，[proxy.py:120-133](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L120-L133)。

#### 4.3.4 代码实践

**实践目标**：确认 `T.Tensor` 默认是 global、连续布局，并理解它和 `alloc_*` 共用 scope 体系。

**操作步骤**：

1. 写一个最小的 elementwise kernel（可参考 [language_basics.md:153-185](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/language_basics.md#L153-L185) 的 vector add），用 `T.Tensor` 标注三个参数：
   ```python
   @T.prim_func
   def add_kernel(
       A: T.Tensor((N,), dtype),   # scope 默认 global
       B: T.Tensor((N,), dtype),
       C: T.Tensor((N,), dtype),
   ):
       ...
   ```
2. 阅读源码，回答：为什么参数 `A/B/C` 不需要写 `scope="global"`？
3. （源码阅读型）对比 `T.Tensor((M,N), 'float16')` 与 `T.alloc_shared((M,N), 'float16')`，说明前者调用 `TensorProxy`（scope=global、带连续 strides），后者调用 `alloc_shared`（scope=shared.dyn、无 strides）。

**需要观察的现象**：参数 Buffer 的 scope 是 `"global"`，且 strides 是行优先连续的。

**预期结果**：能复述 [proxy.py:79](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L79)（`default_scope = "global"`）和 [proxy.py:151-154](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L151-L154)（自动构造连续 strides）这两点，并解释「代理对象 vs 分配原语」的分工。

> 待本地验证：若想看到 scope 真实落点，可在 `tilelang.lower` 后用 `mod.show()` 打印 TIR，参数 Buffer 的 `storage_scope` 应为 `global`。

#### 4.3.5 小练习与答案

**练习 1**：`T.Buffer` 和 `T.Tensor` 有什么关系？新代码该用哪个？  
**答案**：两者都返回 `tir.Buffer`，但 `T.Buffer`（`BufferProxy`）已被 `@deprecated` 标注，新代码应统一用 `T.Tensor`（`TensorProxy`）。见 [proxy.py:19](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L19) 与 [proxy.py:47](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L47)。

**练习 2**：`T.FragmentBuffer` 和 `T.alloc_fragment` 都涉及 `local.fragment`，它们的差别是什么？  
**答案**：`T.alloc_fragment(shape, dtype)` 是在 kernel 内部分配一块 fragment 显存；`T.FragmentBuffer` 是一个代理对象，把 `BaseTensorProxy` 的默认 scope 改成 `local.fragment`，多用于声明/构造而非内部分配。两者共用 scope 字符串，但职责不同（分配 vs 声明）。见 [proxy.py:169-176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L169-L176) 与 [allocate.py:72-83](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L72-L83)。

**练习 3**：`make_tensor(ptr, shape, dtype)` 解决什么问题？  
**答案**：它把一个已有的指针变量 `Var` 通过 `match_buffer` 绑定为指定 shape/dtype/strides 的 `tir.Buffer`，常用于从裸指针（如 FFI、外部存储）构造可索引的 Buffer。见 [proxy.py:277-278](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L277-L278)。

---

## 5. 综合实践

**任务**：实现一个把 global tensor 加载到 shared、再做 reduce 到 fragment 的 kernel，并在每一步注释数据所在的显存层级。这是本讲三个最小模块的串联：global → shared → fragment。

**操作步骤**：

1. 新建 `my_reduce.py`，参考 [testing/python/language/test_tilelang_language_reduce.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/language/test_tilelang_language_reduce.py) 的 `reduce_max_test`（第 32-48 行）与 quickstart 的结构，写出下面这个 kernel（**示例代码**，非仓库原有文件）：

   ```python
   import tilelang
   import tilelang.language as T

   M, N = 128, 256

   @tilelang.jit
   def my_reduce(M: int, N: int, dtype=T.float16):
       @T.prim_func
       def reduce_max_kernel(
           A: T.Tensor((M, N), dtype),      # (1) global：输入张量，scope=global
           B: T.Tensor((M,), dtype),        # (5) global：输出张量，scope=global
       ):
           with T.Kernel(1) as _:
               # (2) shared：把整块 A 搬到片上共享内存
               A_shared = T.alloc_shared((M, N), dtype)   # scope=shared.dyn
               T.copy(A, A_shared)                         # global → shared

               # (3) fragment：reduce 结果落进寄存器 fragment
               B_frag = T.alloc_fragment((M,), dtype)      # scope=local.fragment
               T.reduce_max(A_shared, B_frag, dim=1)       # shared → fragment（沿 N 维取 max）

               # (4) global：把 fragment 结果写回显存
               T.copy(B_frag, B)                           # fragment → global

       return reduce_max_kernel

   import torch
   a = torch.randn(M, N, device="cuda", dtype=torch.float16)
   b = torch.empty(M, device="cuda", dtype=torch.float16)

   kernel = my_reduce(M, N)
   kernel(a, b)

   torch.testing.assert_close(b, a.max(dim=1).values, rtol=1e-2, atol=1e-2)
   print("reduce_max kernel matches torch reference.")
   ```

2. 在注释中标注每一步数据所在层级：`(1)` global、`(2)` shared、`(3)` fragment、`(4)`/`(5)` global。

3. **进阶观察**：把 `T.reduce_max(A_shared, B_frag, dim=1)` 的输入从 `A_shared`（shared）改成 `A`（global），观察 TileLang 是否仍能编译（reduce 原语对不同 scope 输入有不同处理路径，见 [reduce_op.py:42-75](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L42-L75)）。

**需要观察的现象**：
- kernel 能正确运行，`b` 等于 `a.max(dim=1)`。
- 改输入 scope 后，编译行为可能变化（reduce 内部会根据 `is_shared`/`is_fragment` 选择不同的搬运路径）。

**预期结果**：你应能用一句话描述数据在 `global → shared → fragment → global` 之间的搬运，并指出每个 `alloc_*` 落在哪一层显存。

> 待本地验证：本实践需要 CUDA 环境与 tilelang 正确安装（参见 [u1-l2 环境搭建](u1-l2-installation.md)）。reduce 对 global 输入的支持情况以本地编译结果为准。

---

## 6. 本讲小结

- GPU 显存分多个层级，TileLang 用 `scope` 字符串（`global` / `shared.dyn` / `local.fragment` / `local` / `local.var` / `shared.tmem` / `shared.barrier`）把抽象 tile 绑定到具体硬件存储。
- `T.alloc_shared` / `T.alloc_fragment` / `T.alloc_local` 都是对 `T.alloc_buffer` 的薄封装，**唯一区别是默认 scope**；分别落在片上共享、tensor core fragment、线程私有标量。
- **`fragment`（`local.fragment`）不是 `local`**：fragment 的元素分布在 warp 各线程上，专门对接 tensor core，是 `T.gemm` 累加器的硬性要求；`local` 只是线程私有标量。
- `T.alloc_var` 恒为单元素、支持 `init` 初值；`T.empty` 不分配显存，仅用 `OutTensor` 声明 JIT 输出张量。
- `T.Tensor` / `T.Buffer` / `FragmentBuffer` 等是「代理对象」，主要声明 kernel 参数（默认 global、连续 strides）；`T.Buffer` 已废弃，应改用 `T.Tensor`。
- 代理对象的 `scope` 与 `alloc_*` 共用同一套命名——这是 TileLang 把「Python 语法」连到「硬件存储」的统一抽象。

## 7. 下一步学习建议

- 本讲只讲了「分配」，还没讲「搬运」。下一讲 [u2-l5 数据搬运：T.copy 与 T.view](u2-l5-copy-view.md) 会展开 `T.copy` 在 global/shared/fragment 之间的具体语法与切片，与本讲的显存层级紧密配合。
- 想看「计算如何用上 fragment」可继续读 [u2-l3 计算原语：gemm 与 reduce](u2-l3-compute-primitives.md)，理解 `T.gemm` 为什么必须配 `alloc_fragment` 累加器。
- 对 fragment 布局如何被推导成 tensor core 所需形状感兴趣的读者，可以提前预览 [u4-l1 Layout 推理机制](u4-l1-layout-inference.md)，那里会解释 `local.fragment` 背后的线程级布局推理。
