# T.Kernel：启动配置与线程/块绑定

## 1. 本讲目标

学完本讲后，你应该能够：

- 写出一个 `T.Kernel(...)` 的启动配置，理解 `*blocks` 与 `threads` 两个参数分别对应 CUDA 的 `gridDim` 和 `blockDim`。
- 解释 `with T.Kernel(...) as (bx, by):` 里 `bx`、`by` 到底绑到了 `blockIdx.x`、`blockIdx.y` 还是 `blockIdx.z`，并能在 kernel 里正确使用这些块索引变量。
- 看懂 `KernelLaunchFrame` 这个「帧栈」对象如何把 grid/thread 维度组织成一个有序的 frame 列表，并理解「最后 4 个 frame 恒为 threadIdx + 主块」这条不变量。
- 会用 `get_thread_binding(s)` / `get_block_binding(s)` 等辅助 API 在 kernel 内部取回线程/块索引变量。
- 能独立写出一个 2D grid 的 elementwise kernel 并校验 `bx`、`by` 的语义。

本讲只解决一个问题：**一个 TileLang kernel 是如何声明「我需要多少个 block、每个 block 多少个 thread」的，以及这些索引变量是怎么被绑定、怎么被取用的。** 它是 [u1-l3 quickstart](u1-l3-quickstart.md) 里那句 `with T.Kernel(...) as (bx, by):` 的逐字逐句展开。

## 2. 前置知识

在进入源码前，先用三段话把基础概念说清楚。

**第一，CUDA 的 grid/block/thread 三级模型。** 一段 GPU 代码（kernel）被启动时，会被复制成很多份并行执行。组织方式是：

- 一个 **grid（网格）** 由若干 **block（线程块）** 组成，grid 的形状用 `gridDim.(x|y|z)` 描述，三段都不超过 65535 量级。
- 每个 **block** 由若干 **thread（线程）** 组成，block 的形状用 `blockDim.(x|y|z)` 描述，一个 block 内线程总数通常不超过 1024。
- 每份代码实例可以用内置变量 `blockIdx.(x|y|z)` 知道「我是第几个 block」，用 `threadIdx.(x|y|z)` 知道「我是这个 block 里的第几个 thread」。

所以「启动配置」本质上就是回答两个问题：grid 多大（要多少 block）、每个 block 多大（要多少 thread）。`T.Kernel` 就是这两个问题的 TileLang 写法。

**第二，Python 的 `with ... as ...:` 与上下文管理器。** `with` 语句会调用对象的 `__enter__` 方法，把它的返回值绑定到 `as` 后面的变量；离开 `with` 块时调用 `__exit__`。`T.Kernel` 正是一个上下文管理器：`__enter__` 负责把 `blockIdx` 绑定变量「交」给你，`__exit__` 负责收尾。理解这一点，才能看懂为什么 `bx`、`by` 是「凭空」从 `as` 里冒出来的。

**第三，承接 [u1-l3](u1-l3-quickstart.md)。** 你已经在 quickstart 里见过：

```python
with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
```

本讲要回答：为什么第一个参数绑到 `bx`、第二个绑到 `by`？为什么 `threads=128`？`bx`/`by` 又是什么类型的对象？答案全部在 [`tilelang/language/kernel.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py) 里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`tilelang/language/kernel.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py) | 本讲主角。定义 `Kernel()` 工厂、`KernelLaunchFrame` 帧对象、以及 `get_thread_binding(s)` / `get_block_binding(s)` 等模块级辅助函数。 |
| [`examples/quickstart.py`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) | 最小可运行样例，第 17 行的 `T.Kernel(...)` 是本讲反复引用的真实用法。 |
| [`src/ir.cc`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc) | C++ 侧的 `KernelLaunch` 实现：真正把 grid/thread 维度「物化」成一个有序的 frame 列表。本讲用它来解释「frames 列表长什么样」。 |

> 说明：`kernel.py` 是 Python 外壳，它把参数整理好后调用 C++ 的 `_ffi_api.KernelLaunch(...)`。本讲重点读 Python 侧；C++ 侧只用来确认 frames 列表的顺序（第 4.2 节）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 Kernel 函数签名与 threads 规范化** —— `Kernel(*blocks, threads, is_cpu, prelude)` 的参数怎么填、`threads` 怎么被补齐成三维。
2. **4.2 LaunchThreadFrame 与块/线程绑定** —— 帧栈对象如何把 grid/thread 组织成 frames 列表，`__enter__` 如何把 `bx/by/bz` 交给你。
3. **4.3 get_thread_binding(s)/get_block_binding(s) 辅助 API** —— 在 kernel 内部按维度取回索引变量的便捷函数。

### 4.1 Kernel 函数签名与 threads 规范化

#### 4.1.1 概念说明

`T.Kernel` 是一个**工厂函数**，它的返回值是一个上下文管理器（`KernelLaunchFrame`）。你只需要告诉它两件事：

- **`*blocks`**：1 到 3 个整数表达式，描述 grid 三个维度的大小（`gridDim.x/y/z`）。例如 `T.ceildiv(N, block_N)` 表示「N 维度按 block_N 切成若干块」。
- **`threads`**：描述每个 block 的 thread 数（`blockDim`）。可以是一个整数（只设 `blockDim.x`），也可以是列表/元组（设多维 `blockDim`）。

此外还有两个不常用的开关：`is_cpu=True` 表示这是一段 CPU kernel（不绑任何 threadIdx/blockIdx），`prelude` 是一段要注入到生成代码前面的 C 字符串。完整签名见 [kernel.py:228-233](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L228-L233)。

#### 4.1.2 核心流程

`Kernel()` 内部做的事情很少，关键是把 `threads` **规范化成统一形状**，再把整理好的参数交给 C++。流程如下：

```text
用户调用 Kernel(gx, gy, threads=128)
        │
        ├─ 若 not is_cpu 且 threads is None → threads = 128（默认值）
        │
        ├─ 规范化 threads：
        │     int          → [int, 1, 1]
        │     list/tuple   → 补 1 到长度 3
        │     其它（None）  → 仅当 is_cpu 才允许
        │
        ├─ 组装 attrs：
        │     is_cpu=True   → attrs["tilelang.is_cpu_kernel_frame"] = True
        │     prelude 非空  → attrs["pragma_import_c"] = prelude
        │
        └─ return _ffi_api.KernelLaunch(blocks, threads, attrs)
                 （进入 C++ 侧，见 4.2）
```

**关键设计：`threads` 永远被规范化成 3 元素列表**（GPU 情况）。这意味着无论你写 `threads=128` 还是 `threads=(64, 2)`，C++ 侧拿到的 block 维度都是三维的，只是高位可能为 1。这条不变量是 4.2 节「最后 4 个 frame 恒为 threadIdx.x/y/z + 主块」成立的前提。

线程总数满足：

\[
\text{num\_threads} = \text{threadDim}.x \times \text{threadDim}.y \times \text{threadDim}.z
\]

例如 `threads=(64, 2)` 规范化为 `[64, 2, 1]`，则每 block 共 128 个线程。

#### 4.1.3 源码精读

**函数签名与文档**，[tilelang/language/kernel.py:228-281](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L228-L281)：文档串里给出了三种典型用法，值得直接读——1D 启动 `as bx`、2D grid + 多维 thread `as (bx, by)`、CPU kernel `as (i,)`。

**默认值与规范化**，[tilelang/language/kernel.py:284-294](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L284-L294)：

```python
if not is_cpu and threads is None:
    threads = 128  # default thread number

if isinstance(threads, int):
    threads = [threads, 1, 1]
elif isinstance(threads, list):
    threads = threads + [1] * (3 - len(threads))
elif isinstance(threads, tuple):
    threads = list(threads) + [1] * (3 - len(threads))
else:
    assert is_cpu, "threads must be an integer or a list of integers"
```

- 第 284 行：不写 `threads` 时，GPU kernel 默认每 block 128 个线程。
- 第 287–292 行：把 `int / list / tuple` 三种写法统一补成三维。
- 第 293–294 行：`threads` 为 `None`（即没规范化成功）只允许出现在 `is_cpu` 情况——CPU kernel 没有 thread 概念。

**attrs 组装与转交 C++**，[tilelang/language/kernel.py:296-302](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L296-L302)：把 `is_cpu`、`prelude` 编码进一个 `attrs` 字典，最后 `return _ffi_api.KernelLaunch(blocks, threads, attrs)`。注意 `blocks` 是 `*blocks` 收集来的元组，原样作为 grid 三个维度传下去。

**真实用法**，[examples/quickstart.py:17](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L17)：

```python
with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
```

这里 `blocks = (ceildiv(N, block_N), ceildiv(M, block_M))`，`threads=128 → [128, 1, 1]`。所以 grid 是 2D，block 是 1D（128 线程）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `threads` 规范化规则，观察不同写法最终都变成三维。

**操作步骤**：

1. 在装好 `tilelang` 的环境里，打开 Python，直接调用 `Kernel`（不进入 `with`，只看它构造的对象）。注意 `Kernel` 内部会调 `_ffi_api`，需要在合法上下文里；这里我们只读它规范化后的 `threads` 形状——可以通过给 `KernelLaunchFrame` 喂参数后检查 `get_thread_extents()`。

2. 写一个最小 kernel，分别用三种 `threads` 写法编译，打印线程数：

```python
# 示例代码：仅用于观察 threads 规范化，不是项目原有代码
import tilelang
import tilelang.language as T

@tilelang.jit
def make_add(block_M, block_N, threads):
    @T.prim_func
    def add_kernel(
        A: T.Tensor((block_M, block_N), "float32"),
        B: T.Tensor((block_M, block_N), "float32"),
        C: T.Tensor((block_M, block_N), "float32"),
    ):
        with T.Kernel(1, 1, threads=threads) as (bx, by):
            A_local = T.alloc_fragment((block_M, block_N), "float32")
            B_local = T.alloc_fragment((block_M, block_N), "float32")
            C_local = T.alloc_fragment((block_M, block_N), "float32")
            T.copy(A, A_local)
            T.copy(B, B_local)
            for i, j in T.Parallel(block_M, block_N):
                C_local[i, j] = A_local[i, j] + B_local[i, j]
            T.copy(C_local, C)
    return add_kernel
```

3. 想象（或本地编译后查看生成的 CUDA 源码 `kernel.get_kernel_source()`）`threads=128`、`threads=(64,2)`、`threads=[32,2,2]` 三种写法生成的 `blockDim`。

**需要观察的现象**：

- `threads=128` → `blockDim.x=128, .y=1, .z=1`。
- `threads=(64,2)` → `blockDim.x=64, .y=2, .z=1`，总线程仍为 128。
- `threads=[32,2,2]` → `blockDim=(32,2,2)`，总线程 128。

**预期结果**：三种写法的「总线程数」可以不同（你也可以写 256），但 C++ 侧拿到的永远是长度为 3 的列表，缺失的高位维度被补成 1。

> **待本地验证**：上述 `blockDim` 具体数值建议你用 `kernel.get_kernel_source()` 取出生成的 CUDA 源码，在其中搜索 `<<<grid, block>>>` 或 `blockDim` 来确认；本讲不假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：把 `threads` 完全省略（`T.Kernel(4, 4)`），默认每个 block 多少线程？

**答案**：128。见 [kernel.py:284-285](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L284-L285)，`not is_cpu and threads is None` 时设为 128。

**练习 2**：`T.Kernel(8, threads=[4, 4])` 规范化后的 `threads` 列表是什么？每个 block 共多少线程？

**答案**：`[4, 4, 1]`（list 分支用 `threads + [1]*(3-2)` 补到三维），总线程 \(4 \times 4 \times 1 = 16\)。

**练习 3**：为什么 `threads=None` 时要 `assert is_cpu`？

**答案**：GPU kernel 必须有 thread 维度（默认 128）；只有 CPU kernel 才允许不设 `threads`，因为 CPU 没有 threadIdx/blockIdx 的概念，见 [kernel.py:293-294](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L293-L294)。

---

### 4.2 LaunchThreadFrame 与块/线程绑定

#### 4.2.1 概念说明

`Kernel()` 返回的对象类型是 `KernelLaunchFrame`（[kernel.py:94-95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L94-L95)）。它身上挂着一个关键属性 `frames`——一个**有序的 frame 列表**，每个 frame 代表一个 grid/thread 维度（在 TVM 里就是一个 `IterVar` 循环）。

理解本节的核心，是搞清楚 **`frames` 列表里元素的排列顺序**。一旦顺序清楚了，`as (bx, by)` 里那个 `bx`、`by` 是什么、`get_block_binding(0)` 取的是谁，全都一目了然。

#### 4.2.2 核心流程

frame 列表由 C++ 侧的 `KernelLaunch` 构造（[src/ir.cc:249-315](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc#L249-L315)）。对一个 **GPU** kernel，列表顺序是：

```text
frames = [ blockIdx.x, blockIdx.y, blockIdx.z?,   ← grid 维度（0~3 个，按你给的 blocks 数）
           threadIdx.x, threadIdx.y, threadIdx.z, ← thread 维度（恒 3 个，因 4.1 已规范化）
           MainBlock ]                            ← 主块，承载 attrs（1 个）
```

由于 4.1 节保证了 thread 维度恒为 3 个、主块恒为 1 个，所以**列表末尾的 4 个元素永远是 `[threadIdx.x, threadIdx.y, threadIdx.z, MainBlock]`**。这就是贯穿本节的「**最后 4 个 frame**」不变量。于是：

- **block 绑定**（你 `as` 出来的 `bx/by/bz`）= `frames[0 : -4]`，即除最后 4 个之外的前缀。
- **thread 绑定** = `frames[-4 : -1]`（去掉最后那个主块）。
- 单个维度：`get_block_extent(dim)` 取 `frames[dim]`，`get_thread_extent(dim)` 取 `frames[-4+dim]`。

对 **CPU** kernel（`is_cpu=True`），没有 thread 维度，列表更简单：

```text
frames = [ block_var_0, block_var_1, ..., MainBlock ]
```

此时 block 绑定 = `frames[0 : -1]`（只去掉最后的主块），见 [kernel.py:113-121](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L113-L121)。

`__enter__` 做两件事：(1) 把自己压进一个线程本地的帧栈；(2) 按上面的规则从 `frames` 里挑出 block 绑定，**如果只有 1 个就解包成裸 `Var`，否则返回列表**——这就是为什么你既能写 `as bx` 又能写 `as (bx,)`。`__exit__` 把自己从栈顶弹出（[kernel.py:123-131](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L123-L131)）。

> **帧栈为什么是线程本地（thread-local）的？** 见 [kernel.py:72-80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L72-L80)。因为 TileLang 可能在多线程里同时构造多个 kernel，用 `threading.local()` 隔离每个线程「当前正在写哪个 kernel」，避免互相串扰。辅助 API（4.3 节）正是靠这个栈找到「当前 kernel」。

#### 4.2.3 源码精读

**C++ 构造 GPU frames 的顺序**，[src/ir.cc:271-304](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc#L271-L304)。关键片段：

```cpp
// grid 维度：只 push 你实际给的那几个
if (!grid_size.empty())
  n->frames.push_back(LaunchThread(CreateEnvThread("bx", "blockIdx.x", ...), grid_size[0]));
if (grid_size.size() > 1)
  n->frames.push_back(LaunchThread(CreateEnvThread("by", "blockIdx.y", ...), grid_size[1]));
if (grid_size.size() > 2)
  n->frames.push_back(LaunchThread(CreateEnvThread("bz", "blockIdx.z", ...), grid_size[2]));
// thread 维度：恒 push 3 个（block_size 已规范化为 3）
if (block_size.defined()) {
  ...push_back tx, ty, tz...
}
```

可见 `blockIdx.x/y/z` 用的是环境线程名 `"bx"/"by"/"bz"`，`threadIdx.x/y/z` 用 `"tx"/"ty"/"tz"`。CPU 路径见 [src/ir.cc:261-270](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc#L261-L270)，只 push `block_var_i`，且 `ICHECK(block_size.empty())` 强制 CPU 不能有 block size。

**主块挂 attrs**，[src/ir.cc:306-312](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc#L306-L312)：最后那个 `MainBlock` 把 `is_cpu`/`prelude` 等 attrs 带上，所以它要单独占一个 frame 位（这就是「最后 4 个」里除了 3 个 threadIdx 之外的第 4 个）。

**`__enter__` 取绑定**，[tilelang/language/kernel.py:101-121](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L101-L121)：

```python
maybe_cpu = last_block_frame.annotations.get("tilelang.is_cpu_kernel_frame", False)
if maybe_cpu:
    return _normalize_bindings([frame.vars[0] for frame in self.frames[0:-1]])
else:
    return _normalize_bindings([frame.iter_var.var for frame in self.frames[0:-4]])
```

- GPU：返回 `frames[0:-4]` 每个 frame 的 `iter_var.var`，正是 `bx/by/bz` 这几个 `Var`。
- CPU：返回 `frames[0:-1]` 每个 frame 的 `vars[0]`。

**单绑定的解包**，[tilelang/language/kernel.py:83-91](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L83-L91)：`_normalize_bindings` 在只有 1 个绑定时返回裸 `Var`，否则返回列表。配套地，[kernel.py:14-22](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L14-L22) 给 `tvm.tir.Var` 打了 `__iter__`/`__len__` 补丁，让单维度绑定也能像元组一样被遍历（修的是 issue #830），所以 `as bx` 和 `as (bx,)` 都能用。

**块/线程 extent 的取法**印证了上面的顺序，[tilelang/language/kernel.py:142-154](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L142-L154)（block 用 `frames[dim]`）与 [tilelang/language/kernel.py:156-168](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L156-L168)（thread 用 `frames[-4+dim]`）。

回到 quickstart，[examples/quickstart.py:17](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L17)：

```python
with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
```

- `blocks[0] = ceildiv(N, block_N)` → `frames[0]` → `bx` = `blockIdx.x`，范围 \(0 \dots \lceil N/\text{block\_N}\rceil-1\)，覆盖 N（列）方向。
- `blocks[1] = ceildiv(M, block_M)` → `frames[1]` → `by` = `blockIdx.y`，覆盖 M（行）方向。
- 这就是 [u1-l3](u1-l3-quickstart.md) 里「`by` 绑 M 维（行）、`bx` 绑 N 维（列）」的来源。kernel 内 `A[by * block_M, ...]`（A 是 `(M,K)`，按行取）、`B[..., bx * block_N]`（B 是 `(K,N)`，按列取）正好对上。

#### 4.2.4 代码实践

**实践目标**：用「坐标编码」的方式，把 `bx`、`by` 的语义变成**可观测、可校验**的输出（在 GPU kernel 里逐线程 `print` 会产生海量噪声，所以这里改用「让输出值等于全局坐标」的办法来「打印」语义）。

**操作步骤**：

1. 写一个 kernel，把每个元素写成它自己的**全局行号** `gi = by * block_M + i`：

```python
# 示例代码：用于观测 by -> M(行) 的语义
import tilelang
import tilelang.language as T
import torch

M, N = 256, 512
block_M, block_N = 64, 64

@tilelang.jit
def encode_row(M, N, block_M, block_N):
    @T.prim_func
    def enc(C: T.Tensor((M, N), "float32")):
        # 2D grid: blocks[0]=N方向绑 bx, blocks[1]=M方向绑 by
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
            C_local = T.alloc_fragment((block_M, block_N), "float32")
            for i, j in T.Parallel(block_M, block_N):
                # by 是 blockIdx.y, 对应 M(行) 方向；i 是块内行偏移
                C_local[i, j] = T.cast(by * block_M + i, "float32")
            T.copy(C_local, C[by * block_M, bx * block_N])
    return enc

kernel = encode_row(M, N, block_M, block_N)
c = torch.empty(M, N, device="cuda", dtype=torch.float32)
kernel(c)
```

2. 校验：如果 `by` 真的绑在 M（行）方向，那么结果应当满足 `C[i, j] == i`（每一行所有列相等，且等于行号）：

```python
ref = torch.arange(M, device="cuda", dtype=torch.float32).unsqueeze(1).expand(M, N)
torch.testing.assert_close(c, ref, rtol=0, atol=0)
print("by -> M(行) 语义验证通过")
```

3. 同理写一个 `encode_col`，把元素写成全局列号 `gj = bx * block_N + j`，校验 `C[i, j] == j`，即可证明 `bx -> N(列)`。

**需要观察的现象**：

- `encode_row` 的输出每一行是常数、且第 `i` 行等于 `i`。
- `encode_col` 的输出每一列是常数、且第 `j` 列等于 `j`。

**预期结果**：两个 assert 都通过，从而**间接证明** `bx = blockIdx.x` 对应 N（列）、`by = blockIdx.y` 对应 M（行）——这正是「打印 bx、by 语义」的可靠等价做法。

> **待本地验证**：`T.cast(value, "float32")` 的写法参考仓库示例 `examples/distributed/example_overlapping_allgather.py:48`；坐标编码逻辑本讲未实际运行，请在本地 GPU 环境确认 assert 通过。

#### 4.2.5 小练习与答案

**练习 1**：一个 3D grid kernel `T.Kernel(gx, gy, gz, threads=128)`，它的 `frames` 列表共有几个元素？分别是什么？

**答案**：8 个 = 3(blockIdx) + 3(threadIdx) + 1(MainBlock) + … 注意 grid 给 3 个时 `frames[0:3]=bx,by,bz`，`frames[3:6]=tx,ty,tz`，`frames[6]=MainBlock`，共 7 个。若你只给 2 个 grid 维度则是 6 个。关键是末尾恒为 `[tx,ty,tz,MainBlock]` 这 4 个。

**练习 2**：为什么 `__enter__` 在 GPU 路径返回的是 `frames[0:-4]` 而不是 `frames[0:-1]`？

**答案**：因为末尾 4 个 frame 是 `[threadIdx.x, threadIdx.y, threadIdx.z, MainBlock]`，它们不是 block 绑定，必须一并排除；只排除最后 1 个（主块）会把 3 个 threadIdx 也当成 block 绑定返回，语义错误。见 [kernel.py:121](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L121)。

**练习 3**：CPU kernel（`is_cpu=True`）的 `frames` 末尾是几个？

**答案**：只 1 个 `MainBlock`（没有 threadIdx），所以 CPU 的 block 绑定是 `frames[0:-1]`，见 [kernel.py:117](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L117)。

---

### 4.3 get_thread_binding(s)/get_block_binding(s) 辅助 API

#### 4.3.1 概念说明

`with T.Kernel(...) as (bx, by):` 只把**block 绑定**交给了你。但有时你需要在 kernel 内部用到 **threadIdx**（例如手写 reduce、按线程切分工作），或者按维度取 block 绑定。TileLang 提供了一组模块级辅助函数，它们都从 4.2 节那个线程本地帧栈里找到「当前 kernel」，再调用 `KernelLaunchFrame` 上对应的方法：

| 模块级函数（`T.xxx`） | 返回 | 对应的 frame 方法 |
| --- | --- | --- |
| `get_thread_binding(dim=0)` | 单个 `Var`（threadIdx.dim） | `get_thread_binding` |
| `get_thread_bindings()` | `[tx, ty, tz]` | `get_thread_bindings` |
| `get_block_binding(dim=0)` | 单个 `Var`（blockIdx.dim） | `get_block_binding` |
| `get_block_bindings()` | 所有 block 绑定列表 | `get_block_bindings` |
| `get_thread_extent(s)` / `get_block_extent(s)` | 维度大小（int） | `get_*_extent` |

#### 4.3.2 核心流程

每个辅助函数都是「三步走」：

```text
T.get_thread_binding(dim)
   │
   ├─ KernelLaunchFrame.Current()   ← 从线程本地栈取当前帧（找不到就 assert）
   │
   ├─ frame.get_thread_binding(dim) ← frames[-4 + dim].iter_var.var
   │
   └─ 返回该维度的 Var
```

`dim=0` 对应 `.x`，`dim=1` 对应 `.y`，`dim=2` 对应 `.z`。这套函数的意义在于：**你不必把所有索引都写进 `as` 里**，需要时随时可以「回查」。

#### 4.3.3 源码精读

**模块级封装**，[tilelang/language/kernel.py:305-326](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L305-L326)：

```python
def get_thread_binding(dim: int = 0) -> Var:
    assert KernelLaunchFrame.Current() is not None, "KernelLaunchFrame is not initialized"
    return KernelLaunchFrame.Current().get_thread_binding(dim)

def get_block_binding(dim: int = 0) -> Var:
    assert KernelLaunchFrame.Current() is not None, "KernelLaunchFrame is not initialized"
    return KernelLaunchFrame.Current().get_block_binding(dim)
```

每个函数都先 `assert` 「当前必须有 kernel 帧」，防止你在 `with T.Kernel` 之外误调用。extent 系列同理，见 [kernel.py:329-350](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L329-L350)。

**帧方法实现**印证 4.2 的顺序：

- `get_thread_binding(dim)` 取 `frames[-4 + dim].iter_var.var`，[kernel.py:170-175](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L170-L175)（`-4+0=-4`=tx，`-4+1=-3`=ty，`-4+2=-2`=tz）。
- `get_thread_bindings()` 取 `frames[-4:-1]`，即 `[tx, ty, tz]`，[kernel.py:177-182](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L177-L182)。
- `get_block_binding(dim)` 取 `frames[dim].iter_var.var`，[kernel.py:193-198](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L193-L198)。
- `get_block_bindings()` 取 `frames[0:-4]`，[kernel.py:200-204](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L200-L204)。

**便捷属性**：`KernelLaunchFrame` 还提供 `.blocks` / `.threads` / `.num_threads` 三个 property，[kernel.py:206-225](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L206-L225)。其中 `num_threads` 由 `get_num_threads()` 算 \(tx \times ty \times tz\)，[kernel.py:184-191](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L184-L191)。

**`Kernel` 文档串里的真实示例**正好演示了这组 API 的用法，[kernel.py:267-273](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L267-L273)：

```python
with T.Kernel(grid_x, grid_y, threads=(64, 2)) as (bx, by):
    tx, ty = T.get_thread_bindings()
    ...
```

这里 grid 给了 2 维（`bx, by`），thread 给了 2 维 `(64, 2)`，于是 `T.get_thread_bindings()` 返回 `[tx, ty]`（`tz` 因规范化也存在，但通常用不到）。

#### 4.3.4 代码实践

**实践目标**：在 kernel 内部用 `get_thread_bindings()` 取回 threadIdx，做一个「每个 thread 处理一个元素」的 elementwise kernel，体会 block 绑定（`as`）与 thread 绑定（`get_thread_bindings`）的分工。

**操作步骤**：

```python
# 示例代码：用 threadIdx 做逐线程 elementwise
import tilelang
import tilelang.language as T
import torch

E = 1024           # 元素总数
threads = 128      # 每 block 128 线程

@tilelang.jit
def threadwise_add(E, threads):
    @T.prim_func
    def add_kernel(
        A: T.Tensor((E,), "float32"),
        B: T.Tensor((E,), "float32"),
        C: T.Tensor((E,), "float32"),
    ):
        # blocks[0] = ceildiv(E, threads) 绑 bx(=blockIdx.x)
        with T.Kernel(T.ceildiv(E, threads), threads=threads) as bx:
            tx = T.get_thread_binding(0)                 # threadIdx.x
            # bx, tx 都是 Var，可用表达式计算全局元素下标
            tid_global = bx * threads + tx
            C[tid_global] = A[tid_global] + B[tid_global]
    return add_kernel

kernel = threadwise_add(E, threads)
a = torch.randn(E, device="cuda", dtype=torch.float32)
b = torch.randn(E, device="cuda", dtype=torch.float32)
c = torch.empty(E, device="cuda", dtype=torch.float32)
kernel(a, b, c)
torch.testing.assert_close(c, a + b, rtol=1e-3, atol=1e-3)
print("threadwise A+B 正确")
```

**需要观察的现象**：

- `bx` 来自 `as bx`（block 绑定），`tx` 来自 `T.get_thread_binding(0)`（thread 绑定）。
- 全局下标 `bx * threads + tx` 正确覆盖 `0..E-1`。

**预期结果**：assert 通过，说明 block 绑定与 thread 绑定能配合表达任意全局坐标。

> **待本地验证**：直接用 `Var` 做下标写单元素（`C[tid_global] = ...`）是否被当前版本的 TileLang 前端接受，建议本地编译；若前端要求走 `T.Parallel`/`T.copy` 风格，可改写为等价的 `T.Parallel(threads)` 循环形式。

#### 4.3.5 小练习与答案

**练习 1**：在 `with T.Kernel(...)` 之外调用 `T.get_thread_binding(0)` 会发生什么？

**答案**：触发 `assert KernelLaunchFrame.Current() is not None`，抛出 `AssertionError: KernelLaunchFrame is not initialized`。见 [kernel.py:307](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L307)。

**练习 2**：`get_thread_binding(2)` 取的是 frames 列表的第几个元素（用负索引表示）？

**答案**：`frames[-4 + 2] = frames[-2]`，即 `threadIdx.z`。见 [kernel.py:170-175](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L170-L175)。

**练习 3**：`KernelLaunchFrame.num_threads` 是怎么算出来的？

**答案**：三个 thread 维度 extent 相乘，\(tx \times ty \times tz\)，见 [kernel.py:184-191](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L184-L191)。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**2D grid elementwise kernel**（对应本讲的 practice_task），并用「坐标编码」把 `bx`、`by` 的语义打印出来。

**任务**：实现 `C = A + B`，其中 `A, B, C` 形状均为 `(M, N)`，用 2D grid、每个 block 处理 `block_M × block_N` 的 tile。要求：

1. 用 `T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by)` 启动，并在注释里写清 `bx → blockIdx.x(N/列)`、`by → blockIdx.y(M/行)`。
2. kernel 内部用 `T.alloc_fragment` 放累加器、用 `T.copy` 搬运 tile、用 `T.Parallel` 做元素加法、再用 `T.copy` 写回（参考 quickstart 的结构）。
3. 校验：与 PyTorch 的 `a + b` 用 `torch.testing.assert_close` 比对。
4. **附加（打印语义）**：复制一份 kernel 改成「把每个元素写成全局行号」，验证 `C[i,j] == i`，从而证明 `by` 绑在 M（行）方向；再改成全局列号验证 `bx` 绑在 N（列）方向。

参考实现框架（综合了 4.1.4、4.2.4、4.3.4 的片段）：

```python
# 示例代码：综合实践
import tilelang
import tilelang.language as T
import torch

M, N = 512, 1024
block_M, block_N = 64, 64

@tilelang.jit
def elementwise_add(M, N, block_M, block_N):
    @T.prim_func
    def add_kernel(
        A: T.Tensor((M, N), "float32"),
        B: T.Tensor((M, N), "float32"),
        C: T.Tensor((M, N), "float32"),
    ):
        # bx = blockIdx.x -> N(列) 方向, 共 ceildiv(N, block_N) 个
        # by = blockIdx.y -> M(行) 方向, 共 ceildiv(M, block_M) 个
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
            A_local = T.alloc_fragment((block_M, block_N), "float32")
            B_local = T.alloc_fragment((block_M, block_N), "float32")
            C_local = T.alloc_fragment((block_M, block_N), "float32")
            T.copy(A[by * block_M, bx * block_N], A_local)   # 按行/列偏移取 tile
            T.copy(B[by * block_M, bx * block_N], B_local)
            for i, j in T.Parallel(block_M, block_N):
                C_local[i, j] = A_local[i, j] + B_local[i, j]
            T.copy(C_local, C[by * block_M, bx * block_N])
    return add_kernel

kernel = elementwise_add(M, N, block_M, block_N)
a = torch.randn(M, N, device="cuda", dtype=torch.float32)
b = torch.randn(M, N, device="cuda", dtype=torch.float32)
c = torch.empty(M, N, device="cuda", dtype=torch.float32)
kernel(a, b, c)
torch.testing.assert_close(c, a + b, rtol=1e-3, atol=1e-3)
print("2D grid elementwise A+B 校验通过")
```

完成后再做第 4 步的坐标编码变体（见 4.2.4），即可把 `bx`/`by` 的语义「打印」成可校验的数据。

> **待本地验证**：以上未在本讲中实际执行，请在本地 GPU 环境运行确认 assert 通过与延迟数值。

## 6. 本讲小结

- `T.Kernel(*blocks, threads=..., is_cpu=..., prelude=...)` 是启动配置工厂：`*blocks` 描述 `gridDim`（1~3 维），`threads` 描述 `blockDim`，缺失时 GPU 默认 128。
- `threads` 永远被规范化成 3 元素列表（[kernel.py:287-292](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L287-L292)），这是后续 frame 不变量的前提。
- `KernelLaunchFrame.frames` 是有序列表：GPU 路径末尾恒为 `[threadIdx.x, threadIdx.y, threadIdx.z, MainBlock]`（「最后 4 个 frame」），block 绑定是 `frames[0:-4]`。
- `with ... as (bx, by)` 里的绑定由 `__enter__` 从 `frames` 里挑出（[kernel.py:101-121](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L101-L121)）；`blocks[0]→bx=blockIdx.x`、`blocks[1]→by=blockIdx.y`，与 quickstart 的 N/M 维度一一对应。
- 单维度绑定时 `_normalize_bindings` 解包成裸 `Var`，加上 `Var.__iter__` 补丁，使 `as bx` 与 `as (bx,)` 都合法。
- `T.get_thread_binding(s)` / `T.get_block_binding(s)` 等辅助函数从线程本地帧栈回查索引变量（[kernel.py:305-350](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L305-L350)），便于在 kernel 内按需取用 threadIdx/blockIdx。

## 7. 下一步学习建议

本讲只解决了「怎么启动一个 kernel、索引怎么绑」。接下来：

- **[u2-l2 Tile 声明与显存层级](u2-l2-tile-alloc.md)**：本讲的 `T.alloc_fragment` / `T.copy` 只是顺手用了，下一讲正式讲 `alloc_shared/alloc_fragment/alloc_local` 与 global/shared/register 三级显存的对应关系。
- **[u2-l4 循环与控制流](u2-l4-loops-control-flow.md)**：本讲的 `T.Parallel` 只是元素级并行，下一讲讲 `T.Pipelined`（软件流水）、`T.serial`、`T.Unroll` 等循环原语的语义。
- **想看编译侧**：若你想知道 `KernelLaunchFrame` 最终如何变成 CUDA 的 `<<<grid, block>>>`，可预习 [src/transform/lower_device_kernel_launch.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/lower_device_kernel_launch.cc) 与 [u3 编译流水线](u3-l1-compile-overview.md)。
