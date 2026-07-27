# 内存层级与数据搬运

## 1. 本讲目标

上一讲（u2-l1）我们学会了如何用 `@T.prim_func` + `T.Tensor` + `with T.Kernel(...)` 把一个 kernel 的「骨架」搭起来。但骨架里写的是「从哪里读、写到哪、在哪种内存里算」，这些才是决定一个 GPU kernel 快不快的关键。

本讲聚焦 tilelang 里的**内存层级（memory hierarchy）**与**数据搬运（data movement）**。读完本讲你应该能够：

1. 说清 GPU 的 global / shared / fragment(register) / local 四级内存在 tilelang 里分别对应哪个分配函数、哪个 scope 字符串、最终生成什么样的 CUDA 存储修饰符。
2. 会用 `T.alloc_shared` / `T.alloc_fragment` / `T.alloc_local` / `T.alloc_var` 分配各级缓冲，并理解 `local.fragment` 在编译期被重映射为 `local`（寄存器）的过程。
3. 会用 `T.copy(src, dst)` 在不同 scope 之间搬运一个 tile，并知道它在后端如何被自动 lowering 成 TMA / cp.async / ldmatrix / 普通 SIMT 循环。
4. 会用 `T.fill` / `T.clear` 把局部缓冲初始化为零或指定值。

本讲覆盖的最小模块：`tilelang.language.allocate`、`tilelang.language.copy_op`，并附带讲解 `tilelang.language.fill_op`。

## 2. 前置知识

### 2.1 为什么不能直接在 global memory 上算

GPU 的显存（global memory）容量大但延迟高、带宽相对有限。粗略的数量级（以 NVIDIA GPU 为例）：

| 存储层级 | 典型容量（每 SM / 整卡） | 典型访问延迟（周期） | 可见性 |
|---|---|---|---|
| global memory（显存） | 几十 GB | 约 400–800 | 所有线程 |
| shared memory（共享内存） | 每 SM 约 48–228 KB | 约 20–30 | 同一个 block 内所有线程 |
| register / local（寄存器） | 每 SM 数千个 | 约 1 | 单个线程私有 |

如果一个 kernel 反复从 global memory 读写同一个数据，大量时间会浪费在等显存。高性能 kernel 的核心套路是：**把一个数据块（tile）一次性从 global 搬到片上的 shared memory，再从 shared 搬到寄存器里反复计算，最后把结果搬回 global。** 这就是 tilelang 这类「tile 级 DSL」要帮你表达的东西。

### 2.2 scope 是什么

TVM 的 TIR 用一个字符串 `scope` 标注一块缓冲「住在哪种内存里」，例如 `"global"`、`"shared.dyn"`、`"local.fragment"`。tilelang 的分配函数本质上就是「指定 shape + dtype + scope」去创建一个 TIR Buffer，scope 决定了它在后端代码生成时变成 `extern __shared__`、寄存器，还是别的。

### 2.3 前置讲义衔接

本讲依赖 u2-l1：你需要知道 `@T.prim_func`、`T.Tensor`、`with T.Kernel(...)` 的含义，以及「函数体里写的是搭建 IR 的指令，而非运行时流程」这个核心观念（详见 u2-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [tilelang/language/allocate.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py) | 各级内存的分配函数：`alloc_shared` / `alloc_local` / `alloc_fragment` / `alloc_var` / `alloc_global` 等。 |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py) | 数据搬运原语：`copy` / `async_copy` / `tma_copy` / `transpose` 等，最终生成 `tl.tileop.copy` 等 intrinsic。 |
| [tilelang/language/fill_op.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fill_op.py) | 缓冲初始化：`fill` / `clear`，生成 `tl.tileop.fill` intrinsic。 |
| [tilelang/language/common.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py) | 语言门面（facade），把上述函数统一 `from .allocate import ...` 挂到 `T.*` 上。 |
| [examples/elementwise/example_elementwise_add.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py) | 贯穿本讲的实战示例：global→shared→fragment→shared→global 的完整搬运链。 |
| [src/cuda/codegen/codegen_cuda.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc) | C++ 代码生成：把 scope 字符串翻译成 CUDA 的 `__shared__` 等存储修饰符。 |
| [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc) | Pass：把 `local.fragment` 重映射为 `local`，并展开各种 tile op。 |
| [src/cuda/op/copy_analysis.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/copy_analysis.cc) | C++ 端 copy lowering 分析：判断一次 `T.copy` 该用 TMA、cp.async 还是普通循环。 |

## 4. 核心概念与源码讲解

### 4.1 GPU 三级内存与 alloc_* 分配（tilelang.language.allocate）

#### 4.1.1 概念说明

tilelang 把 GPU 的软件可控内存抽象成几类 scope，每类对应一个分配函数：

| 分配函数 | 默认 scope | 物理含义 | 典型用途 |
|---|---|---|---|
| `T.alloc_shared(shape, dtype)` | `shared.dyn` | 共享内存（片上，block 内可见） | 暂存从 global 搬来的 tile，供 block 内线程共享 |
| `T.alloc_fragment(shape, dtype)` | `local.fragment` | 寄存器（线程私有，受 layout 推理） | tile 级累加器、矩阵乘的 C 片段 |
| `T.alloc_local(shape, dtype)` | `local` | 寄存器（线程私有，普通） | 线程私有标量/小数组 |
| `T.alloc_var(dtype, ...)` | `local.var` | 单元素寄存器变量 | 循环计数、临时标量 |
| `T.alloc_global(shape, dtype)` | `global` | 显存 workspace | 主要用于测试，**不建议**在生产 kernel 用 |

两点初学者最容易混：

- **`shared.dyn` 与 `shared` 的区别**：`shared.dyn` 对应 CUDA 的 `extern __shared__`（动态共享内存，由 launch 时指定大小，可被多个分配合并）；`shared` 对应静态 `__shared__`（固定大小）。`alloc_shared` 默认用 `shared.dyn`，因为 tilelang 有一个 `merge_shared_memory_allocations` pass 会把多个 `shared.dyn` 缓冲合并到同一块动态共享内存里，省着用。
- **`local.fragment` 不是一种新硬件内存**：它本质上还是寄存器（`local`），只是带了一层「layout 推理」语义——编译器会推断这个 2D tile 在 warp 的寄存器里该怎么排布，从而能对接张量核（Tensor Core / WGMMA）。在 lowering 阶段，`local.fragment` 会被重映射回 `local`。

#### 4.1.2 核心流程

一次分配在 Python 侧做的事非常薄：

```text
T.alloc_shared(shape, dtype)
   └── T.sblock_alloc_buffer(shape, dtype, scope="shared.dyn")
          └── 生成一个带 scope 标注的 TIR Buffer 节点（Allocate）
                 └── 后端 codegen 看到 scope="shared.dyn" → extern __shared__
                     后端 codegen 看到 scope="local"/"local.fragment" → 寄存器（无地址空间修饰符）
```

也就是说，Python 侧只负责「声明这块缓冲住哪个 scope」，真正的物理分配（`__shared__` 数组、寄存器分配）发生在 C++ 后端的代码生成阶段。

scope 在最终 CUDA 源码里的对应关系，由 `CodeGenTileLangCUDA::PrintStorageScope` 决定：

```text
scope == "shared" / "shared.barrier" / "shared.cluster_barrier"  →  __shared__ __align__(N)
scope == "shared.dyn"                                              →  extern __shared__ __align__(1024)
其它（global 除外）                                                →  无修饰符（即寄存器）
```

注意一个硬约束：CUDA 后端**不允许直接 `alloc_global`**——`PrintStorageScope` 里对 `global` 直接 `ICHECK_NE(scope, "global")` 报错，提示「所有 global 数组必须作为 kernel 参数传入」。这就是为什么 `T.Tensor` 参数默认就是 global，而你几乎不需要手写 `alloc_global`。

#### 4.1.3 源码精读

先看分配函数本体。三个核心分配函数都极其简短，差别只在传入的默认 scope：

`alloc_shared`，注意 `bool` 类型有个特殊回退（因为合并共享内存的 pass 暂不支持 bool，所以回退到静态 `shared`）：

[allocate.py:34-49](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py#L34-L49) — `alloc_shared`：默认 scope 为 `shared.dyn`，bool 回退为 `shared`，最终调用 `T.sblock_alloc_buffer`。

`alloc_local` 与 `alloc_fragment`：

[allocate.py:52-63](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py#L52-L63) — `alloc_local`：默认 scope 为 `local`。

[allocate.py:66-77](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/allocate.py#L66-L77) — `alloc_fragment`：默认 scope 为 `local.fragment`，函数体与 `alloc_local` 完全一致，差别仅是 scope 字符串，意味着「fragment = 带 layout 推理语义的 local」。

再看后端如何把 scope 翻译成 CUDA 修饰符：

[codegen_cuda.cc:1542-1553](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L1542-L1553) — `PrintStorageScope`：`shared`/`shared.barrier`/`shared.cluster_barrier` → `__shared__`；`shared.dyn` → `extern __shared__`；`global` 直接报错。

以及 `local.fragment` 被重映射为 `local` 的位置（layout 推理阶段）：

[lower_tile_op.cc:90-95](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L90-L95) — 注释说明 `makeBufferWithLayout` 会把 storage scope 从 `local.fragment` 重映射为 `local`。

最后看一个真实例子，elementwise 加法里同时分配了 shared、fragment 两种缓冲：

[example_elementwise_add.py:20-23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L20-L23) — 同时分配了 `A_shared`/`B_shared`/`C_shared`（shared.dyn）和 `C_local`（local.fragment），演示了一个 tile 的完整驻留地。

#### 4.1.4 代码实践

**实践目标**：直观感受 scope 与 CUDA 存储修饰符的对应关系。

**操作步骤**：

1. 确认已按 u1-l2 安装好 tilelang（需要 CUDA 环境）。
2. 写一个最小 kernel，分配 shared、fragment、local 三种缓冲但不做实际计算（示例代码）：

```python
# 示例代码：仅用于观察生成的存储修饰符
import tilelang
import tilelang.language as T

@tilelang.jit
def probe(block_M: int = 32, block_N: int = 32, threads: int = 128):
    M, N = T.const("M, N")
    A = T.Tensor((M, N), "float32")
    C = T.empty((M, N), "float32")

    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=threads) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_N), "float32")   # shared.dyn
        C_local  = T.alloc_fragment((block_M, block_N), "float32") # local.fragment -> 寄存器
        tmp      = T.alloc_local((8,), "float32")                  # local -> 寄存器
        T.copy(A[by * block_M, bx * block_N], A_shared)
        T.clear(C_local)
        T.copy(C_local, C[by * block_M, bx * block_N])
```

3. 调用 `probe.get_kernel_source()`（或在 lazy 模式下先 `kernel = probe.compile(...)` 再取源码），在生成的 CUDA 源码里搜索关键字。

**需要观察的现象**：

- 能找到 `extern __shared__ ...`（对应 `shared.dyn`）。
- **找不到** `A_shared` / `C_local` 对应的数组声明——因为 fragment/local 是寄存器，已被展开成标量/寄存器变量，没有数组形态。

**预期结果**：shared 缓冲以 `extern __shared__` 出现；fragment/local 缓冲不以数组形式出现。GPU 行为**待本地验证**（无 GPU 时可用 `target="cuda"` 编译但不可运行，`get_kernel_source()` 仍可查看）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `T.alloc_shared((32,32), "bool")` 改成 `T.alloc_shared((32,32), "float32")`，生成的 scope 修饰符会变吗？

**参考答案**：修饰符类别不变（都是 `extern __shared__`）。差别在于 bool 时 scope 会被回退成静态 `"shared"`（见 `alloc_shared` 里的 bool hack），float32 时是默认的 `"shared.dyn"`；前者是 `__shared__`，后者是 `extern __shared__`。

**练习 2**：为什么 tilelang 几乎不让用户写 `T.alloc_global`？

**参考答案**：CUDA 后端 `PrintStorageScope` 对 `global` 直接报错，要求所有 global 数组作为 kernel 参数传入（由框架/torch allocator 管理）。`alloc_global` 主要用于测试，且并非所有后端（如 CuTeDSL）都支持。

---

### 4.2 T.copy：在 scope 之间搬运一个 tile（tilelang.language.copy_op）

#### 4.2.1 概念说明

`T.copy(src, dst)` 是 tilelang 里最核心的数据搬运原语。它的语义是：**把 src 指向的那块 tile，整块复制到 dst 指向的 tile**。两侧可以是不同 scope，例如：

- `T.copy(A[global 地址], A_shared)` —— global → shared（搬一个 tile 上片）
- `T.copy(A_shared, A_fragment)` —— shared → fragment（搬到寄存器准备算）
- `T.copy(C_fragment, C[global 地址])` —— fragment → global（把结果写回显存）

它接受三类参数形态：

1. **Buffer**：整个缓冲，如 `T.copy(A_shared, B_shared)`。
2. **BufferRegion**：一个显式区域，如 `T.copy(A_shared[0:32, 0:32], B_shared)`。
3. **BufferLoad（「头地址」语法糖）**：如 `T.copy(A[by*BM, bx*BN], A_shared)`——这里 `A[by*BM, bx*BN]` 是一个标量 `BufferLoad`，代表「从这个地址开始的一整块」，tilelang 会从 dst 的形状反推出要搬运的范围。这是最常用的写法。

`T.copy` 还有一组「显式控制 lowering 走向」的关键字参数，初学阶段了解即可：

- `disable_tma=True`：禁止后端用 TMA（Hopper 的张量内存加速器）。
- `prefer_instruction="tma"|"cp_async"|"sync"`：指定偏好 lowering 成哪种指令。
- `coalesced_width=N`：控制合并访存（coalescing）的向量化宽度。
- `eviction_policy="evict_first"|"evict_last"|"evict_normal"`：L2 cache 驱逐策略提示。

此外还有两个变体：

- `T.async_copy(src, dst)`：显式的异步 global→shared（cp.async），**不会自动插入 wait**，需要用户手动 `T.ptx_wait_group(...)`。
- `T.tma_copy(src, dst, barrier=...)`：用户自管同步的 TMA 拷贝，只发射 producer 部分（expect_tx + load），wait 交给你。

#### 4.2.2 核心流程

`T.copy` 在 Python 侧的工作分两步：**归一化两侧区域** + **生成 intrinsic 调用**。

```text
T.copy(src, dst)
  ├── _normalize_copy_regions(src, dst)
  │     ├── 若两侧都是 Buffer：assert_structural_equal(src.shape, dst.shape)
  │     ├── get_extent(src/dst)：推断每侧的形状
  │     │     Buffer -> shape, BufferRegion -> [r.extent], BufferLoad -> 编码区域
  │     ├── 若两侧都是标量 BufferLoad：直接返回，后续降级为 BufferStore
  │     └── to_buffer_region(..., extents=...)：用 tl.region 把两侧包成带范围的读写区域
  ├── 若两侧都是标量 load：return tirx.BufferStore(dst.buffer, src, dst.indices)  # 降级为单次赋值
  └── 否则：return tirx.call_intrin("handle", Op.get("tl.tileop.copy"), src, dst, annotations=ann)
```

也就是说，`T.copy` 本身**不生成任何循环**，它只生成一个 `tl.tileop.copy` intrinsic 节点，把「src 区域、dst 区域、各种 lowering 提示」打包交给后端。真正的循环展开和指令选择发生在 C++ 的 `lower_tile_op` pass 与 `src/cuda/op/copy.cc` + `copy_analysis.cc` 里。

后端 copy 分析会根据 **src/dst 的 scope 组合** 自动选择指令：

| src scope | dst scope | 典型 lowering（SM90+） |
|---|---|---|
| global | shared.dyn/shared | TMA bulk load（满足对齐/stride/dtype 约束时），否则 cp.async 或普通 load |
| shared.dyn/shared | global | TMA bulk store，否则普通 store |
| shared | fragment | ldmatrix（把 shared 数据按矩阵载入寄存器，对接 mma/wgmma） |
| fragment | shared | stmatrix |
| shared | shared | SM 间拷贝 / 普通循环 |

例如 `CheckBulkLoad` 会判定一次 global→shared 是否能用 TMA：要求 src 在 global、dst 在 shared/shared.dyn，最后一维 bit 数对齐 128 字节，且 dtype 组合合法。

#### 4.2.3 源码精读

`T.copy` 主体（含完整的 docstring，里面讲清了「头地址语法糖」与 extent 推断规则）：

[copy_op.py:54-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L54-L134) — `copy`：归一化两侧区域；若两侧都是标量 `BufferLoad` 则降级为 `BufferStore`；否则构造 `tl.tileop.copy` intrinsic，并把 `disable_tma` / `prefer_instruction` / `eviction_policy` / `coalesced_width` 等打包进 `annotations`。

区域归一化的关键逻辑：

[copy_op.py:17-51](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L17-L51) — `_normalize_copy_regions`：两侧都是 Buffer 时校验形状一致；从 `get_extent` 推断每侧 extent；两侧都是标量 load 时直接返回（走快速赋值路径）；否则用 `legalize_pairwise_extents` 右对齐/广播 extent，再用 `to_buffer_region` 包成读写区域。

最终生成 intrinsic 的那一行：

[copy_op.py:134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L134) — `tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.copy"), src, dst, annotations=...)`，这是 Python 侧能看到的「搬运」的全部产物。

异步拷贝变体（注意它只支持 global→shared，且需要 SM80+）：

[copy_op.py:190-231](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L190-L231) — `async_copy`：生成 `tl.tileop.async_copy` intrinsic，后端发射 `cp.async` + `commit_group`，**不自动插 wait**。

后端对 TMA 可用性的判定：

[copy_analysis.cc:191-227](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/copy_analysis.cc#L191-L227) — `CheckBulkLoad`：仅当 src 在 global、dst 在 shared/shared.dyn、最后一维对齐 128 字节、dtype 组合合法、全局 stride 合法时，才允许走 TMA bulk load；否则回退。

`async_copy` 的约束提示（说明它只做 global→shared）：

[copy_analysis.cc:521-538](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/copy_analysis.cc#L521-L538) — `MakeAsyncUnavailableReason`：`T.async_copy` 仅支持 global→shared/shared.dyn，且要求目标支持 cp.async（SM80+）。

真实示例里的搬运链：

[example_elementwise_add.py:25-30](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L25-L30) — 四次 `T.copy`：`A[...]→A_shared`、`B[...]→B_shared`（global→shared，头地址语法糖）、`C_local→C_shared`（fragment→shared）、`C_shared→C[...]`（shared→global）。

#### 4.2.4 代码实践

**实践目标**：对比「经 shared 中转」与「直接读写 global」两种写法的正确性与生成指令差异。

**操作步骤**：

1. 准备一份「直读 global」的对照 kernel（示例代码），与 `example_elementwise_add.py` 做对照：

```python
# 示例代码：直接在 global 上做加法，不经 shared
import tilelang
import tilelang.language as T

@tilelang.jit
def add_direct(A, B, block_M, block_N, in_dtype, out_dtype, threads):
    M, N = T.const("M, N")
    A: T.Tensor((M, N), in_dtype)
    B: T.Tensor((M, N), in_dtype)
    C = T.empty((M, N), out_dtype)

    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=threads) as (bx, by):
        # 不分配 shared，直接从 global 读、直接写回 global
        for local_y, local_x in T.Parallel(block_M, block_N):
            C[by * block_M + local_y, bx * block_N + local_x] = (
                A[by * block_M + local_y, bx * block_N + local_x]
                + B[by * block_M + local_y, bx * block_N + local_x]
            )
    return C
```

2. 用同样输入分别调用 `elementwise_add` 与 `add_direct`，断言两者结果一致（`torch.testing.assert_close`）。
3. 分别 `get_kernel_source()`，对比两份 CUDA 源码。

**需要观察的现象**：

- 正确性：两个 kernel 输出应完全一致（都是逐元素加）。
- 生成代码：`elementwise_add` 版本里能看到 shared 缓冲与（在 SM90 上）类似 TMA/cp.async 的搬运指令；`add_direct` 版本里只有直接对 global 的 load/store。
- 性能（可选）：在 H100/A100 上用 `kernel.get_profiler().do_bench()` 测延迟，分块版本通常更快。

**预期结果**：结果一致；分块版本的生成源码含 shared 暂存与显式搬运，直读版本则更短但访存更散。性能数字**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`T.copy(A[by*BM, bx*BN], A_shared)` 里，`A[by*BM, bx*BN]` 是一个标量取值，tilelang 怎么知道要搬多大一块？

**参考答案**：通过 `_normalize_copy_regions` 里的 `get_extent`。当一侧（这里是 src 的标量 `BufferLoad`）extent 为空时，用另一侧（dst `A_shared` 的 shape）作为长度匹配的依据，再经 `legalize_pairwise_extents` 右对齐。文档里把这叫做「头地址语法糖」。

**练习 2**：`T.copy`、`T.async_copy`、`T.tma_copy` 三者最关键的语义差别是什么？

**参考答案**：`T.copy` 是「同步」语义（后端自动插好 arrive/wait）；`T.async_copy` 走 cp.async 且**不自动插 wait**，需手动 `T.ptx_wait_group`；`T.tma_copy` 是「用户自管同步」的 TMA，只发 producer 部分（expect_tx + load），wait 交由用户的 `mbarrier_wait_parity`。

---

### 4.3 T.fill / T.clear：初始化局部缓冲（tilelang.language.fill_op）

#### 4.3.1 概念说明

分配出来的 shared/fragment 缓冲**不会自动清零**。最典型的场景：矩阵乘的累加器 `C_local = T.alloc_fragment(...)` 在被 `T.gemm` 累加之前必须先清零，否则会累加上垃圾值。tilelang 提供两个初始化原语：

- `T.fill(buffer, value)`：把整块缓冲（或某个区域）填成 `value`。
- `T.clear(buffer)`：等价于 `T.fill(buffer, 0)`。

它们都接受 `Buffer`、`BufferRegion`、`BufferLoad` 三种形态，会自动推导要填充的 extent。

#### 4.3.2 核心流程

```text
T.fill(buffer, value)
  ├── 若 buffer 是带 let 值的 Var：解引用出真实对象
  ├── 推断 extent：
  │     Buffer -> shape；BufferRegion -> [r.extent]；BufferLoad -> 编码区域
  └── return tirx.call_intrin("handle", Op.get("tl.tileop.fill"), to_buffer_region(buffer, "w", extents), value)

T.clear(buffer)
  └── return fill(buffer, 0)   # 本质就是 fill with 0
```

和 `T.copy` 一样，`T.fill` 也只生成一个 `tl.tileop.fill` intrinsic，真正的「怎么填」由后端 lowering 决定（对 fragment 可能展开成寄存器赋值，对 shared 可能展开成并行填充循环）。

#### 4.3.3 源码精读

`fill`：

[fill_op.py:10-37](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fill_op.py#L10-L37) — `fill`：归一化 let 变量、从 buffer 形态推断 extents、构造 `tl.tileop.fill` intrinsic。

`clear`：

[fill_op.py:40-63](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fill_op.py#L40-L63) — `clear`：把 buffer 解引用后调用 `fill(..., 0)`。

语言门面统一导出（让 `T.fill` / `T.clear` / `T.copy` / `T.alloc_*` 都挂到 `T` 上）：

[common.py:42-67](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L42-L67) — `from .allocate import ...`、`from .copy_op import ...`、`from .fill_op import fill, clear`。

#### 4.3.4 代码实践

**实践目标**：体会「不清零会出错」与 `T.clear` 的作用。

**操作步骤**：

1. 复用 4.1.4 的 `probe` 思路，写一个累加 kernel：在 K 维循环里多次把 `A_shared` 的值累加到 `C_local`。
2. 分别测试「循环前调用 `T.clear(C_local)`」与「注释掉 `T.clear`」两种情况。

```python
# 示例代码片段
C_local = T.alloc_fragment((block_M, block_N), "float32")
T.clear(C_local)              # 注释掉这一行观察差异
for k in T.serial(num_k):
    T.copy(A[by*block_M, k*block_N], A_shared)
    for i, j in T.Parallel(block_M, block_N):
        C_local[i, j] += A_shared[i, j]   # 累加
T.copy(C_local, C[by*block_M, bx*block_N])
```

**需要观察的现象**：

- 有 `T.clear`：结果等于各 tile 之和，与参考实现一致。
- 无 `T.clear`：`C_local` 初值是垃圾，结果错乱。

**预期结果**：清零版本正确；未清零版本错误。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`T.clear(buf)` 和直接写 `T.fill(buf, 0)` 有区别吗？

**参考答案**：没有实质区别。源码里 `clear` 就是先解引用 let 变量，再调用 `fill(..., 0)`。

**练习 2**：为什么 `T.gemm(A_s, B_s, C_local)` 之前通常要先 `T.clear(C_local)`？

**参考答案**：`T.gemm` 默认是**累加**语义（`clear_accum=False`），即 `C_local += A_s @ B_s`。若 `C_local` 未初始化，累加的是寄存器里的垃圾值。所以要么先 `T.clear`，要么显式让 gemm 不累加。

---

## 5. 综合实践

把本讲三个模块串起来：实现一个把 global 数据先 copy 到 shared、再 copy 到 fragment、做缩放后写回 global 的 kernel，并和「直接在 global 上读写」的版本对比正确性。

**任务**：给定输入 `A`（shape `(M, N)`）和标量 `alpha`，计算 `C = alpha * A`。要求 tile 版本走完整的 `global → shared → fragment → (scale) → shared → global` 链路。

**参考实现**（示例代码）：

```python
# 示例代码：tile 版本的标量缩放
import torch
import tilelang
import tilelang.language as T

@tilelang.jit
def scale_tile(A, alpha, block_M, block_N, dtype, threads):
    M, N = T.const("M, N")
    A: T.Tensor((M, N), dtype)
    C = T.empty((M, N), dtype)

    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=threads) as (bx, by):
        # 1) 分配各级缓冲
        A_shared = T.alloc_shared((block_M, block_N), dtype)   # shared.dyn
        C_local  = T.alloc_fragment((block_M, block_N), dtype) # local.fragment -> 寄存器
        C_shared = T.alloc_shared((block_M, block_N), dtype)   # shared.dyn

        # 2) global -> shared（头地址语法糖，extent 由 A_shared 推断）
        T.copy(A[by * block_M, bx * block_N], A_shared)

        # 3) shared -> fragment，并在寄存器里做缩放
        T.copy(A_shared, C_local)
        for i, j in T.Parallel(block_M, block_N):
            C_local[i, j] = C_local[i, j] * alpha

        # 4) fragment -> shared -> global
        T.copy(C_local, C_shared)
        T.copy(C_shared, C[by * block_M, bx * block_N])
    return C


# 直读 global 的对照版本（示例代码）
@tilelang.jit
def scale_direct(A, alpha, block_M, block_N, dtype, threads):
    M, N = T.const("M, N")
    A: T.Tensor((M, N), dtype)
    C = T.empty((M, N), dtype)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=threads) as (bx, by):
        for i, j in T.Parallel(block_M, block_N):
            C[by * block_M + i, bx * block_N + j] = (
                A[by * block_M + i, bx * block_N + j] * alpha
            )
    return C


if __name__ == "__main__":
    M = N = 1024
    a = torch.randn(M, N, dtype=torch.float32, device="cuda")
    alpha = 2.5

    out_tile  = scale_tile(a, alpha, block_M=32, block_N=32, threads=128,
                           dtype="float32")
    out_direct = scale_direct(a, alpha, block_M=32, block_N=32, threads=128,
                              dtype="float32")

    torch.testing.assert_close(out_tile, a * alpha, rtol=1e-2, atol=1e-2)
    torch.testing.assert_close(out_tile, out_direct, rtol=1e-2, atol=1e-2)
    print("正确性校验通过")
```

**操作步骤**：

1. 在 CUDA 机器上运行上述脚本。
2. 用 `scale_tile.get_kernel_source()` 查看生成源码，找到：`extern __shared__` 声明（shared）、global→shared 的搬运指令、fragment 上的乘法、shared→global 的写回。
3. （可选）用 `scale_tile.get_profiler().do_bench()` 与 `scale_direct` 对比延迟。

**需要观察的现象与预期结果**：

- 两个版本输出都等于 `a * alpha`，且彼此一致。
- tile 版本的生成源码里能看到 shared 暂存与多次 `T.copy` 对应的搬运；直读版本更短但访存未优化。
- 性能对比与 TMA/cp.async 是否触发**待本地验证**（取决于 GPU 架构与 tile 对齐情况）。

> 提示：这一段把本讲的三个核心动作——**分配各级缓冲**、**用 `T.copy` 搬运**、**用 `T.clear`/直接计算初始化**——全部串了起来。后续学 `T.gemm`（u3-l1）与软件流水线（u3-l3）时，你会看到同样的 `alloc_shared + alloc_fragment + T.copy + T.clear` 骨架反复出现。

## 6. 本讲小结

- GPU 内存分 global（显存，慢但大）、shared（片上，block 内共享）、fragment/local（寄存器，线程私有，最快）；高性能 kernel 的套路是把 tile 经 `global → shared → fragment` 搬到寄存器里反复算，再写回。
- tilelang 用 scope 字符串标注一块缓冲住哪种内存：`alloc_shared` → `shared.dyn`，`alloc_fragment` → `local.fragment`，`alloc_local` → `local`，`alloc_var` → `local.var`。
- scope 在后端 codegen 里被翻译成 CUDA 修饰符：`shared.dyn` → `extern __shared__`，`shared` → `__shared__`，`local*` → 寄存器（无修饰符）；`local.fragment` 会在 lowering 时被重映射为 `local`。
- `T.copy(src, dst)` 只生成一个 `tl.tileop.copy` intrinsic，extent 由两侧推断（含「头地址语法糖」）；真正的循环与指令选择（TMA / cp.async / ldmatrix / 普通循环）由 C++ 后端根据 scope 组合自动决定。
- `T.async_copy` 与 `T.tma_copy` 是「用户自管同步」的变体，前者只支持 global→shared（SM80+）且不自动 wait，后者只发 TMA producer 部分。
- `T.fill(buf, v)` 与 `T.clear(buf)` 生成 `tl.tileop.fill` intrinsic，用于初始化局部缓冲；累加型操作（如默认的 `T.gemm`）之前必须先 `T.clear`。

## 7. 下一步学习建议

- **下一讲 u2-l3（控制流与循环原语）**：本讲的 fragment 缩放循环用了 `T.Parallel`，下一讲会系统讲 `T.serial` / `T.unroll` / `T.Parallel` 的区别与适用场景，以及 `LegalizeSafeMemoryAccess` 如何自动给越界访问加保护。
- **u3-l1（T.gemm 与 tile op 体系）**：本讲的 `alloc_shared + alloc_fragment + T.copy + T.clear` 骨架，加上 `T.gemm` 就是完整的分块矩阵乘；届时你会看到 fragment 累加器与张量核是如何对接的。
- **延伸阅读源码**：想更深入理解 copy 的指令选择，可读 [src/cuda/op/copy_analysis.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/copy_analysis.cc) 与 [src/cuda/op/copy.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/copy.cc)；想理解 shared 合并可读 `src/transform/merge_shared_memory_allocations.cc`。
