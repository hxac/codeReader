# cuTile Python 是什么：tile DSL 与 Tile IR

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在还没有读懂任何一行编译器源码之前，先建立对 cuTile Python 的全局直觉**。读完本讲你应该能做到：

- 用一句话说清楚 cuTile Python 是什么、解决什么问题。
- 看懂 README 里的 `vector_add` 示例，并能解释「一个用 `@ct.kernel` 装饰的 Python 函数，在被 `cuLaunchKernel` 真正发射到 GPU 之前，经历了哪些编译阶段」。
- 区分三种**执行空间**（execution space）：host 代码、SIMT 代码、tile 代码。
- 理解 cuTile、Tile IR、tileiras 这三者各自扮演的角色，以及它们如何串成一条完整的编译链路。

本讲只覆盖三个最小模块：**cuTile Python**、**Tile IR**、**tileiras**。其余 API 细节、目录结构、构建方式留到后续讲义。

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更顺：

- **GPU / CUDA**：GPU 是擅长大量并行计算的显卡；CUDA 是 NVIDIA 提供的并行计算平台，让程序能在 GPU 上跑。
- **内核（kernel）**：在 GPU 并行计算里，"kernel" 指一段会被 GPU 上大量并行执行单元同时运行的代码（注意：它和操作系统的 kernel 毫无关系）。
- **SIMT**：Single Instruction, Multiple Threads，"单指令多线程"。这是传统 CUDA 的执行模型——你写一个线程的代码，GPU 让成千上万个线程同时跑它。
- **DSL**：Domain-Specific Language，领域专用语言。cuTile 不是通用语言，而是专门为"在 NVIDIA GPU 上写高性能并行程序"这一件事量身定制的语言。
- **cubin**：CUDA binary，NVIDIA GPU 能直接执行的二进制机器码文件。
- **JIT / AOT**：Just-In-Time（即时编译，运行时编译）与 Ahead-Of-Time（预先编译，发布前编译）。

如果你对上面任意一个名词完全陌生，没关系——本讲会在用到时再次提醒。

## 3. 本讲源码地图

本讲引用的关键文件（均为项目真实文件）：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目门面，给出 `vector_add` 示例、系统要求、安装与构建方式 |
| `docs/source/index.rst` | 官方文档首页，定义 cuTile 的定位、kernel 与 array/tile 概念 |
| `docs/source/execution.rst` | 执行模型文档，讲清 grid/block 与三种执行空间 |
| `docs/source/compilation.rst` | 编译与导出文档，描述 JIT/AOT 链路与 `cuLaunchKernel` 调用约定 |
| `src/cuda/tile/_compile.py` | 编译流水线的实现入口（本讲只用它来确认阶段顺序，不深入） |

> 说明：本讲是「项目认知」篇，引用以 README 与文档为主；`_compile.py` 仅用于在「代码实践」里验证阶段顺序，相关源码机制留到 U5/U7 详讲。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**cuTile Python**（4.1）、**Tile IR**（4.2）、**tileiras**（4.3）。三者构成一条流水线，先逐个理解，最后在 4.3 的实践里把它们串起来。

### 4.1 cuTile Python：面向 NVIDIA GPU 的 tile 级 DSL

#### 4.1.1 概念说明

cuTile Python 是一个面向 NVIDIA GPU 的并行编程模型，同时也是一个基于 Python 的 DSL。它的官方定义写在文档首页：

> cuTile is a parallel programming model for NVIDIA GPUs and a Python-based DSL (Domain-Specific Language). It automatically leverages advanced hardware capabilities, such as tensor cores and tensor memory accelerators, while providing portability across different NVIDIA GPU architectures.
>
> 见 [docs/source/index.rst:8-11](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/index.rst#L8-L11)

这段话有三个关键词，正是 cuTile 的设计目标：

1. **并行编程模型**：它不是给 CPU 用的，而是面向 GPU 的并行计算。
2. **自动利用高级硬件**：张量核（tensor cores）、张量内存加速器（tensor memory accelerators）这些硬件特性，cuTile 会**自动**帮你用上，你不需要手写汇编级别的指令。
3. **跨架构可移植**：同一份 cuTile 代码可以在不同代际的 NVIDIA GPU 上运行并自动启用该架构的最新特性。

**它和传统 CUDA 的核心区别在于抽象层级**。传统 CUDA 是 SIMT 模型——你写"一个线程"的代码，要操心线程、warp、共享内存、同步这些底层细节。cuTile 则把抽象层级抬高到 **tile（瓦片）**：你不再写"一个线程做什么"，而是写"一个 block 一次性处理一整块数据（一个 tile）做什么"，cuTile 编译器再负责把它 lowering 到具体的线程/warp/张量核上。

README 的系统要求里也点明了 cuTile 的产出物：

> cuTile Python generates kernels based on Tile IR which requires NVIDIA Driver r580 or later to run. Furthermore, the tileiras compiler (version 13.2) only supports Blackwell GPU and Ampere/Ada GPU.
>
> 见 [README.md:53-58](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L53-L58)

注意"generates kernels based on Tile IR"——cuTile 的最终产物是 GPU kernel，而这个 kernel 是**基于 Tile IR** 生成的。这就引出了 4.2。

#### 4.1.2 核心流程

先用一个最小例子建立直觉。这是 README 里的 `vector_add` 内核（只保留关键行）：

```python
import cuda.tile as ct

TILE_SIZE = 16

# cuTile kernel for adding two dense vectors. It runs in parallel on the GPU.
@ct.kernel
def vector_add_kernel(a, b, result):
    block_id = ct.bid(0)
    a_tile = ct.load(a, index=(block_id,), shape=(TILE_SIZE,))
    b_tile = ct.load(b, index=(block_id,), shape=(TILE_SIZE,))
    result_tile = a_tile + b_tile
    ct.store(result, index=(block_id,), tile=result_tile)
```

> 见 [README.md:22-31](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L22-L31)

读懂这段代码，就抓住了 cuTile 的编程范式。逐行拆解：

1. `@ct.kernel`：把这个 Python 函数标记为一个 cuTile kernel 的入口。注意它**不能在 host 端直接调用**——host 只能用 `ct.launch(...)` 把它排队送到 GPU 执行。
2. `ct.bid(0)`：当前 block 在第 0 维的编号。一个 kernel 由很多 block 并行执行，`bid(0)` 让每个 block 知道"我是第几号"。
3. `ct.load(a, index=(block_id,), shape=(TILE_SIZE,))`：从全局数组 `a` 里，加载一个一维、长度为 16 的 tile，起点由 `block_id` 决定。`a_tile` 是一个 **tile**，不是普通数组。
4. `result_tile = a_tile + b_tile`：两个 tile 逐元素相加，得到新 tile。这一步**整个 block 集体并行**完成，而不是逐线程。
5. `ct.store(result, index=(block_id,), tile=result_tile)`：把结果 tile 写回全局数组 `result` 对应位置。

host 端启动它的代码：

```python
result = cupy.zeros_like(a)
grid = (ct.cdiv(a.shape[0], TILE_SIZE), 1, 1)
ct.launch(cupy.cuda.get_current_stream(), grid, vector_add_kernel, (a, b, result))
```

> 见 [README.md:40-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L40-L42)

其中 `grid` 决定启动多少个 block。`ct.cdiv(a.shape[0], TILE_SIZE)` 是向上取整除法，数学上即：

\[
\text{grid} = \left\lceil \frac{N}{\text{TILE\_SIZE}} \right\rceil
\]

本例 \(N=128\)，\(\text{TILE\_SIZE}=16\)，所以 \( \text{grid}=8 \)，即 8 个 block 各处理 16 个元素。

**一句话总结 cuTile 的范式**：在 host 端用 `ct.launch` 启动 kernel；kernel 内部用 `ct.load` 把全局数组的一块（tile）搬进来，对 tile 做集体计算，再用 `ct.store` 写回去。这就是贯穿整本手册的 load–compute–store 范式。

#### 4.1.3 源码精读

cuTile 文档首页用一段对比，点明了它最核心的两个数据概念：**array** 与 **tile**。

- **Array（数组）**：存在全局显存里，可变，有物理的、带 strides 的内存布局；在 kernel 内部只能做有限操作（主要是 load/store）。
- **Tile（瓦片）**：是不可变的值，没有定义存储，只存在于 kernel 代码内部；tile 的每一维必须是编译期已知的、2 的幂的常量；tile 支持大量运算（逐元素算术、矩阵乘、归约、形状变换等）。

原文：

> Arrays are stored in the global memory. They are mutable and have physical, strided memory layouts... Tiles are immutable values without defined storage that only exist in the kernel code. Tile dimensions must be compile-time constants that are powers of two.
>
> 见 [docs/source/index.rst:64-73](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/index.rst#L64-L73)

这张 array/tile 对照表是理解 cuTile 全部 API 的钥匙，务必记牢（后续 U2 会专门讲数据模型）。

#### 4.1.4 代码实践

**实践目标**：在不运行代码的前提下，纯靠阅读，把 README 的 `vector_add` 示例「翻译」成你能讲给别人听的中文流程。

**操作步骤**：

1. 打开 [README.md:22-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L22-L42)。
2. 在每一行旁边用注释标注它属于下面哪个角色：`host 代码` / `tile 代码` / `数据搬运（load/store）` / `tile 计算`。
3. 计算当 `a.shape[0] = 128`、`TILE_SIZE = 16` 时，`grid` 的值，以及每个 block 负责处理哪一段元素。

**需要观察的现象**：你会注意到「真正描述计算逻辑的只有 `result_tile = a_tile + b_tile` 这一行」，其余都是数据搬运和定位（`bid`、`index`、`load`、`store`）。这正是 tile DSL 的特征——计算和数据搬运被显式分开。

**预期结果**：

- `grid = (8, 1, 1)`，即 8 个 block。
- 第 `i` 号 block 处理输入下标区间 \([16i,\; 16i+16)\) 的 16 个元素。

> 待本地验证：若你已在虚拟环境装好 cuTile（见 U1-L2），可把 `TILE_SIZE` 改成 8 重算 `grid`，确认数值结果仍然正确。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cuTile kernel 不能用普通的 `vector_add_kernel(a,b,result)` 方式直接调用，而必须用 `ct.launch(...)`？

> **参考答案**：因为 kernel 是要在 GPU 上并行执行的，它需要 host 提供启动所需的全部上下文（CUDA stream、grid 维度、参数）。`ct.launch` 的职责正是把这些信息打包，把内核 JIT 编译并最终通过 `cuLaunchKernel` 发射到 GPU。直接调用只是一个普通 Python 函数调用，既不会编译也不会上 GPU。

**练习 2**：把 `result_tile = a_tile + b_tile` 说成"一个线程把两个数相加"对吗？为什么？

> **参考答案**：不对。`a_tile` 和 `b_tile` 是长度为 16 的 tile，相加是**整个 block 集体并行**完成的 16 个元素的逐元素相加，cuTile 不暴露单个线程。这正是它与 SIMT CUDA 的根本区别。

---

### 4.2 Tile IR：cuTile 的中间表示

#### 4.2.1 概念说明

cuTile Python 用 Python 语法写内核，但 Python 代码本身不能直接跑在 GPU 上。中间需要一个"翻译"。这个翻译的目标语言就是 **Tile IR**（Tile Intermediate Representation，瓦片中间表示）。

README 里明确写到："cuTile Python generates kernels based on **Tile IR**"（[README.md:53-54](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L53-L54)）。

可以把 Tile IR 理解为：

- 一套**独立于 Python 的、面向 tile 级并行**的中间语言/指令集。
- cuTile Python 把 Python kernel 编译成 Tile IR；下游的 `tileiras` 编译器再把 Tile IR 编译成 GPU 二进制（cubin）。
- Tile IR 还有自己的**字节码（bytecode）**格式，可以被序列化、缓存、AOT 导出（见 U7）。

为什么要有这一层中间表示，而不是从 Python 直接编译到 cubin？核心原因是**解耦与可复用**：

- Tile IR 让"前端语言"和"后端硬件"分离——理论上不同的前端语言、不同的后端 GPU 架构都可以围绕同一套 Tile IR 对接。
- Tile IR 是结构化的（有 Block、Operation、类型系统），便于做各种**优化 pass**（死代码消除、整除性传播等，见 U6）。

#### 4.2.2 核心流程

从 Python kernel 到 Tile IR 的概念流水线（细节留到 U5）：

```
Python kernel 函数
      │  （取 AST）
      ▼
   HIR  高层 IR（get_function_hir，ast2hir）
      │
      ▼
   Tile IR（hir2ir：HIR 分派为具体 IR Op）
      │
      ▼
   优化后的 Tile IR（_transform_ir：DCE / 整除传播 / token 排序 等）
      │
      ▼
   TileIR 字节码（generate_bytecode_for_kernel，ir2bytecode）
```

这个顺序不是凭空写的，它对应 `src/cuda/tile/_compile.py` 里真实的 import 与调用：

> 见 [src/cuda/tile/_compile.py:41-65](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_compile.py#L41-L65) —— 这里能看到 `get_function_hir`（ast2hir）、`hir2ir`、`generate_bytecode_for_kernel`（ir2bytecode）等阶段被依次引入。

> 本讲只要求你记住"有这么一条流水线"，每个阶段的具体实现在 U5（前端）和 U6（优化 pass）详讲。

#### 4.2.3 源码精读

执行模型文档对 tile 级并行的定义，能帮助你理解 Tile IR 为什么叫 "tile"：

> A tile kernel is executed by logical thread blocks organized in a 1D, 2D, or 3D grid... Tile programs express block-level parallelism only with no exposure to individual threads within the block.
>
> 见 [docs/source/execution.rst:13-24](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L13-L24)

也就是说，Tile IR 表达的是 **block 级**并行，刻意不暴露单个线程。这是 Tile IR 与传统 SIMT IR（如 LLVM IR + NVVM）最本质的不同——它的"最小并行单位"是 block 上的一个 tile 运算，而不是一条线程指令。

#### 4.2.4 代码实践

**实践目标**：通过阅读文档，确认 Tile IR 在整条链路中的"承上启下"位置。

**操作步骤**：

1. 阅读 [README.md:53-58](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L53-L58) 与 [docs/source/index.rst:8-11](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/index.rst#L8-L11)。
2. 在纸上画一条从左到右的箭头链：`Python kernel → (?) → cubin`，把中间那个 `?` 填成 **Tile IR**。
3. 在 Tile IR 这个节点上方标注"由谁产生"（cuTile Python 前端），下方标注"由谁消费"（tileiras 编译器）。

**需要观察的现象**：你会发现 Tile IR 是一个**独立节点**，前端和后端各自只和它打交道，互不直接耦合。

**预期结果**：你画出的图应当强调"Tile IR 是 cuTile 与 tileiras 之间的契约（contract）"。

#### 4.2.5 小练习与答案

**练习 1**：如果 cuTile 把 kernel 直接从 Python 编译到 cubin，跳过 Tile IR，会损失什么？

> **参考答案**：会损失"前端/后端解耦"和"独立优化空间"。没有结构化中间表示，就无法在 Python 与 GPU 之间插入 DCE、整除性传播、token 排序等优化 pass；也无法把字节码序列化用于 AOT 导出和磁盘缓存；前端语言的更换也会牵连后端。

**练习 2**：Tile IR 表达的是线程级并行还是 block 级并行？

> **参考答案**：block 级并行。Tile IR 不暴露单个线程，其最小并行单位是 block 上的一个 tile 运算（见 [docs/source/execution.rst:13-24](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L13-L24)）。

---

### 4.3 tileiras：把 Tile IR 编译成 cubin 的后端编译器

#### 4.3.1 概念说明

**tileiras** 是 Tile IR 的后端编译器（"tile-ir-as"，类比 `ptxas`）。它的职责是把 Tile IR（通常是 TileIR 字节码）编译成 NVIDIA GPU 能执行的二进制 **cubin**。

tileiras 是一个**独立组件**，不一定要装在 Python 环境里。README 给了两种安装方式：

- `pip install cuda-tile[tileiras]`：把 tileiras 直接装进 Python 虚拟环境（[README.md:64-69](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L64-L69)）。
- `pip install cuda-tile`（不带可选依赖），然后自行安装 CUDA Toolkit 13.1+，cuTile 会去系统 CTK 里找 tileiras（[README.md:72-76](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L72-L76)）。

注意 tileiras 还依赖 `ptxas` 和 `libnvvm`（来自 CUDA Toolkit），见 [docs/source/quickstart.rst:28-35](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L28-L35)。

#### 4.3.2 核心流程

把三个模块串起来，一个 cuTile kernel 从书写到执行，完整经过这些阶段：

```
1. 书写      Python kernel 函数（@ct.kernel）
2. 前端      AST → HIR（get_function_hir / ast2hir）
3. 前端      HIR → Tile IR（hir2ir）
4. 优化      Tile IR 优化 pass（_transform_ir：DCE、整除传播、token 排序…）
5. 序列化    Tile IR → TileIR 字节码（generate_bytecode_for_kernel / ir2bytecode）
6. 后端      字节码 → cubin（tileiras 编译器：compile_cubin）
7. 缓存      cubin 落盘缓存（SQLite，见 U7-L4）
8. 启动      cubin 加载到 GPU，cuLaunchKernel 发射
```

第 6 步是 tileiras 的主场；第 8 步里的 `cuLaunchKernel` 是 CUDA Driver API 的函数，编译文档明确把 cuTile 的调用约定和它绑定在一起：

> A calling convention defines... the binary format and the order of kernel arguments, e.g. as passed to the cuLaunchKernel() CUDA Driver API function... The only currently implemented calling convention is cutile_python_v1.
>
> 见 [docs/source/compilation.rst:59-71](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/compilation.rst#L59-L71)

同时，编译文档说明这套流程是在 **JIT**（运行时即时编译）下自动发生的：

> When a kernel function marked with @ct.kernel is launched using ct.launch(), it is specialized and compiled just in time (JIT) for the concrete launch arguments.
>
> 见 [docs/source/compilation.rst:10-13](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/compilation.rst#L10-L13)

也就是说，当你调 `ct.launch(...)` 时，上面 2–8 步会按需自动发生（已编译且缓存的则直接跳到第 8 步）。

#### 4.3.3 源码精读

tileiras 的硬件支持范围写得很明确，理解它有助于解释"为什么我的 GPU 跑不起来"：

> Furthermore, the tileiras compiler (version 13.2) only supports Blackwell GPU and Ampere/Ada GPU. Hopper GPU will be supported in the coming versions.
>
> 见 [README.md:55-56](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L55-L56)

`tileiras` 在 `_compile.py` 中对应 `compile_cubin` 阶段——这是把字节码变成 cubin 的那一步，本讲只点到为止，定位与调用细节见 U7-L3。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：回答本讲开篇提出的问题——「一个 `@ct.kernel` 装饰的函数，在被 `cuLaunchKernel` 发射到 GPU 之前，经历了哪些编译阶段？」并把阶段名标注在 README 的 `vector_add` 示例旁。

**操作步骤**：

1. 打开 [README.md:22-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L22-L42) 的 `vector_add` 示例。
2. 在 `@ct.kernel` 那一行旁边，用注释依次写下阶段：`# 阶段2 AST→HIR  阶段3 HIR→IR  阶段4 IR优化  阶段5 IR→字节码  阶段6 tileiras→cubin  阶段8 cuLaunchKernel`。
3. 在 `ct.launch(...)` 这一行旁边标注：`# JIT 入口：触发上面的全部编译阶段`。
4. 用一段话（3–5 句）解释：为什么第一次 `launch` 较慢、第二次较快。

**需要观察的现象**：你会意识到 `ct.launch` 一行代码背后藏着完整的编译器 + 缓存系统；这些阶段对你（kernel 作者）是完全透明的。

**预期结果（参考答案段）**：

> 当 `ct.launch(...)` 被调用时，cuTile 先对**具体的启动参数**做特化（JIT）。随后：前端把 Python kernel 解析成 AST，再经 `get_function_hir`（ast2hir）转成 HIR，经 `hir2ir` 转成 Tile IR；优化器 `_transform_ir` 对 Tile IR 跑一系列 pass（如死代码消除、整除性传播、内存 token 排序）；`generate_bytecode_for_kernel`（ir2bytecode）把优化后的 IR 序列化成 TileIR 字节码；后端的 `tileiras` 编译器把字节码编译成 cubin；cubin 经磁盘缓存后加载到 GPU，最终由 `cuLaunchKernel` 按 `cutile_python_v1` 调用约定发射。第一次慢是因为要走完整 2–7 步，第二次快是因为命中了 cubin 缓存，直接跳到加载与启动。

> 说明：上面这段参考答案即本讲「代码实践任务」的标准产出。它把 cuTile Python、Tile IR、tileiras 三个模块串成了一条完整链路。

> 待本地验证：上述阶段顺序基于 `_compile.py` 的 import 与文档描述推断；若你想在机器上"看见"中间产物，可在 U1-L2 装好环境后设置 `CUDA_TILE_DUMP_TILEIR=1` 之类（具体环境变量见 U8-L5）观察 dump 出的字节码/IR，但本讲不要求运行。

#### 4.3.5 小练习与答案

**练习 1**：tileiras 的输入和输出分别是什么？

> **参考答案**：输入是 TileIR 字节码（由 cuTile 前端产生），输出是 NVIDIA GPU 二进制 cubin。

**练习 2**：如果你用 `pip install cuda-tile`（不带 `[tileiras]`），cuTile 还能编译内核吗？

> **参考答案**：能，前提是你自行安装了 CUDA Toolkit 13.1+。此时 cuTile 会去系统 CTK 的位置寻找 `tileiras`（以及它依赖的 `ptxas`、`libnvvm`）。`[tileiras]` 可选依赖只是把 tileiras 也装进 Python 虚拟环境，省去系统 CTK 的依赖。

**练习 3**：cuTile 的调用约定 `cutile_python_v1` 与哪个 CUDA Driver API 函数直接相关？

> **参考答案**：`cuLaunchKernel()`。该调用约定定义了传给 `cuLaunchKernel` 的二进制参数格式与顺序（见 [docs/source/compilation.rst:59-71](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/compilation.rst#L59-L71)）。

---

## 5. 综合实践

把本讲三个模块融会贯通：

**任务**：用一张图 + 一段话，向一个完全没听过 cuTile 的同事解释「cuTile Python 程序是如何变成 GPU 上运行的」。

**要求**：

1. 图中至少包含这些节点：`Python kernel`、`HIR`、`Tile IR`、`TileIR 字节码`、`cubin`、`GPU（cuLaunchKernel）`，并标注每个节点由谁负责（cuTile 前端 / cuTile 优化器 / tileiras / 运行时）。
2. 在图上标出 **array** 与 **tile** 这两个概念分别出现在哪一端（提示：array 是 host 传进来的、tile 是 kernel 内部 load 出来的）。
3. 用一句话点明 cuTile 与传统 SIMT CUDA 的最大区别。

**参考要点**（不是唯一答案）：

- 数据流：`array`（host 端，如 cupy/torch 张量）→ kernel 内 `ct.load` 得到 `tile` → 计算 → `ct.store` 写回 `array`。
- 编译流：`Python kernel → HIR → Tile IR →（优化）→ 字节码 →（tileiras）→ cubin → cuLaunchKernel`。
- 最大区别：cuTile 表达 block 级 / tile 级并行，不暴露单个线程，并自动利用张量核等硬件特性。

## 6. 本讲小结

- cuTile Python 是面向 NVIDIA GPU 的 tile 级并行 DSL，用 Python 语法写 kernel，自动利用张量核等硬件、跨架构可移植。
- 它的编程范式是 **load–compute–store**：host 用 `ct.launch` 启动；kernel 内 `ct.load` 把全局 array 的一块搬成 tile，对 tile 做集体计算，再 `ct.store` 写回 array。
- **array**（全局显存、可变、带 strides）与 **tile**（kernel 内部、不可变、每维为 2 的幂）是理解全部 API 的两把钥匙。
- cuTile kernel 最终产物基于 **Tile IR**——一套独立于 Python 的 block 级中间表示，是前端与后端之间的契约。
- **tileiras** 是后端编译器，把 TileIR 字节码编译成 cubin；可随 pip 安装，也可来自系统 CUDA Toolkit。
- 从 `@ct.kernel` 到 `cuLaunchKernel` 的完整链路：`AST → HIR → Tile IR →（优化 pass）→ 字节码 →（tileiras）cubin →（缓存）→ 启动`，整个过程在 `ct.launch` 时 JIT 完成。

## 7. 下一步学习建议

下一讲 **U1-L2「环境搭建与第一个内核」** 会带你真正把 cuTile 跑起来：安装 `cuda-tile[tileiras]`、从源码可编辑构建、运行 `VectorAdd_quickstart.py` 并验证结果。建议：

- 想先动手：直接进 U1-L2，把本讲的 `vector_add` 在真实机器上跑通。
- 想先看全貌：进 U1-L3「源码目录结构与构建系统」，看清 `src/cuda/tile` 下各子包（前端/优化/后端/运行时）如何组织。
- 想理解 API：进 U1-L4「顶层 API 全景」，把 `cuda.tile` 导出的全部符号分类成速查表。
