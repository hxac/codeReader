# 指令与指令组（Instruction Groups）

## 1. 本讲目标

在 [u1-l3](u1-l3-first-kernel-vector-add.md) 和 [u1-l5](u1-l5-naive-matmul-tilus-script.md) 中，你已经写过 `self.load_global(...)`、`self.store_global(...)`、`self.dot(...)` 这样的调用，并且在 [u2-l1](u2-l1-script-init-call-semantics.md) 中理解了 `Script` 的实例化与 `__call__` 的边界。但你可能一直没搞清楚一个问题：**这些 `self.*` 方法到底从哪里来？为什么 `self.wgmma.mma(...)` 这种写法能直接驱动 Hopper 的硬件单元？**

本讲读完你应该能够：

1. 说出 `self.*` 上「通用指令」与「硬件指令组」的分层来源，并能从源码指出它们各自的定义位置。
2. 区分每类指令对应的 GPU 架构能力（Ampere / Hopper / Blackwell），判断某条指令在你的硬件上是否可用。
3. 理解 `InstructionInterface` 如何用组合 + 全局 builder 上下文的机制，把 9 个指令组挂到 `self` 上，并让用户写出的 Python 调用变成 Tilus IR 语句。

## 2. 前置知识

- **tile-level 编程模型**：见 [u1-l1](u1-l1-project-overview.md)。Tilus 以「一个线程块整体做什么」为视角，张量是一等公民。
- **Script 骨架与 `__call__` 转译**：见 [u2-l1](u2-l1-script-init-call-semantics.md)。转译器（Transpiler）会遍历 `__call__` 的 Python AST，把里面的 `self.xxx(...)` 调用翻译成 Tilus IR 的 `InstStmt`。
- **三种内存空间**：全局内存（DRAM）、共享内存（片上 SRAM）、寄存器（线程私有）。对应 `GlobalTensor` / `SharedTensor` / `RegisterTensor`。本讲会反复用到这套术语。
- **GPU 架构与算力（compute capability）**：Ampere ≈ `sm_80`，Hopper ≈ `sm_90a`，Blackwell ≈ `sm_100`。指令组会标注自己要求的最低算力。

一个关键直觉先建立起来：**Tilus 的指令是「分层的」**。底层是一组跟硬件无关的、像 PyTorch 算子一样的通用指令（`load_global`、`dot`、`cast`…），上层是按 GPU 代际封装的硬件指令组（`wgmma`、`tma`、`tcgen05`…）。两者最终都通过同一个 `self` 暴露给用户。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/lang/instructions/base.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py) | 指令组的抽象基类 `InstructionGroup`、全局 builder 上下文 `builder_context` 与 `_current_builder`。这是「指令如何知道当前在往哪个 builder 里写」的底层机制。 |
| [python/tilus/lang/instructions/root.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py) | `RootInstructionGroup`：所有与硬件无关的通用指令（`global_view`/`load_global`/`store_global`/`register_tensor`/`dot`/`cast`/`assume`/`range`/`sync` 等）。 |
| [python/tilus/lang/instructions/__init__.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/__init__.py) | `InstructionInterface`：把通用指令与 8 个硬件指令组组合成最终接口。 |
| [python/tilus/lang/instructions/wgmma.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py) | `WgmmaInstructionGroup`：Hopper 的 warp-group MMA 指令组，作为硬件指令组的典型代表精读。 |
| [python/tilus/lang/script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py) | `Script(InstructionInterface)`：用户内核类继承自 `InstructionInterface`，于是 `self.*` 就天然拥有全部指令。 |
| docs/source/python-api/instruction-groups/*.rst | 每个硬件指令组对应一篇官方 RST 文档，是实践任务的依据。 |

## 4. 核心概念与源码讲解

### 4.1 通用指令（RootInstructionGroup）：与架构无关的 tile-level 基本指令

#### 4.1.1 概念说明

不管你跑在 Ampere、Hopper 还是 Blackwell 上，写内核时总要做这些事：

- 在全局内存上开一个「视图」看某段数据；
- 把一段全局数据搬到寄存器里做运算；
- 在寄存器里做矩阵乘、类型转换、元素级运算；
- 把结果写回全局内存。

这些操作跟具体硬件代际无关，是 tile-level 编程的「公共词汇」。Tilus 把它们集中放在 **`RootInstructionGroup`** 里。它之所以叫 Root（根），是因为它是整棵指令组继承树的根——所有硬件指令组都继承自 `InstructionGroup`，而 `RootInstructionGroup` 是用户 `self` 上「不带前缀」的那些方法的提供者。

通用指令的另一个重要性质：**它们大多可以由任意大小的线程组执行**（你在每个方法的 docstring 里都会看到 `Thread group: Can be executed by any sized thread group`）。也就是说，它不挑线程数，你在一个 warp、一个 warp-group、或整个线程块里都能调用。

#### 4.1.2 核心流程

把通用指令按「数据流」分类，可以画出一条贯穿内核的链路：

```text
        ┌──────────── 指针 / 视图 ────────────┐
全局内存 │  global_view(ptr, dtype, shape)      │  → GlobalTensor（不搬数据，只建视图）
        └──────────────┬───────────────────────┘
                       │ load_global(offsets, shape)
                       ▼
        ┌──────────── 寄存器运算 ──────────────┐
寄存器   │  register_tensor(...)  分配累加器      │
        │  dot(a, b, acc)        张量乘加         │
        │  cast(x, dtype) / add / sum / where … │
        └──────────────┬───────────────────────┘
                       │ store_global(offsets)
                       ▼
全局内存            写回结果 GlobalTensor
```

中间还会用到 `shared_tensor` / `store_shared` / `load_shared` 在共享内存里中转，以及 `sync()` 做线程块内同步。这条链路正是 [u1-l5](u1-l5-naive-matmul-tilus-script.md) naive matmul 的真实数据流。

> 提示：`register_tensor`、`shared_tensor`、`global_tensor` 是「分配」类指令；`global_view` 是「建视图」指令（不分配新显存，只把裸指针解释成带 dtype/shape/layout 的 `GlobalTensor`）。这一区别在 [u1-l3](u1-l3-first-kernel-vector-add.md) 已建立。

#### 4.1.3 源码精读

`RootInstructionGroup` 继承自 `InstructionGroup`，类定义只有一行，但里面塞了上百个方法：

[python/tilus/lang/instructions/root.py:32](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L32) —— `RootInstructionGroup` 的起点。

`global_view` 把裸指针 + dtype + shape 组装成 `GlobalTensor` 视图，内部按需假设紧凑行优先布局：

[python/tilus/lang/instructions/root.py:422-470](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L422-L470) —— 若 `strides` 未给，则调用 `global_row_major(*shape)` 构造行优先布局，最终交给 `self._builder.global_view(...)` 真正建视图。

`load_global` 把全局张量的一个切片加载进寄存器：

[python/tilus/lang/instructions/root.py:472-522](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L472-L522) —— 校验 `offsets` 维数后，调用 `self._builder.load_global(...)`；`store_global`（[L524-L566](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L524-L566)）是对称的写回。

矩阵乘 `dot` 计算 `out = a @ b + c`，要求三者都是 2D 且形状匹配：

[python/tilus/lang/instructions/root.py:852-932](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L852-L932) —— 若未提供累加器 `c`，则按 `acc_dtype` 分配一个全零累加器；最终交给 `self._builder.dot(...)`。注意 docstring 注明它 `Requires compute capability 7.0+ (sm_70) for tensor core MMA, or any GPU for SIMT fallback`——也就是说通用 `dot` 自己会按目标硬件选 MMA 或 SIMT 后备路径，这是它「与架构无关」的体现。

类型转换 `cast`：

[python/tilus/lang/instructions/root.py:934-953](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L934-L953) —— 单行委托 `self._builder.cast(x=x, dtype=dtype)`。

编译器提示 `assume` 与循环 `range` 是两个「非数据」类通用指令，它们影响编译而非搬运数据：

[python/tilus/lang/instructions/root.py:64-94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L64-L94) —— `assume` 接受形如 `a % c == 0` 的整除性约束，转给 `self._builder.assume(cond)`，供后续标量分析使用。

[python/tilus/lang/instructions/root.py:96-159](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L96-L159) —— `range` 创建可迭代对象，支持 `unroll="all"` / `unroll=N` 展开提示，比内置 `range` 多了循环展开控制。

线程块内同步 `sync`：

[python/tilus/lang/instructions/root.py:1885-1895](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L1885-L1895) —— 一行 `self._builder.syncthreads()`，对应 PTX `bar.sync`。

观察规律：**通用指令的方法体几乎都是「做一点参数校验，然后把活儿交给 `self._builder`」**。`self._builder` 是谁、怎么来的，是第 4.3 节要解开的关键。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，把通用指令按「分配 / 视图 / 搬运 / 运算 / 同步 / 提示」分类，建立一张属于你自己的指令速查表。

**操作步骤**：

1. 打开 [python/tilus/lang/instructions/root.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py)，从 `class RootInstructionGroup`（第 32 行）开始浏览。
2. 把每个 `def` 方法按下面的类别归档：
   - **分配**：`register_tensor`、`shared_tensor`、`global_tensor`
   - **视图/形状**：`global_view`、`reshape`、`squeeze`、`unsqueeze`、`transpose`、`view`
   - **搬运**：`load_global`、`store_global`、`load_shared`、`store_shared`、`copy_async` 系列
   - **运算**：`dot`、`cast`、`add`、`maximum`、`where`、`sum`/`max`/`min`、`exp`/`log`/`sqrt` 等
   - **同步**：`sync`、`lock_semaphore`、`release_semaphore`
   - **提示/控制流**：`assume`、`range`、`thread_group`、`single_thread`
3. 对每个你关心的方法，记下它的 docstring 里 `Notes` 段的 **Thread group** 要求。

**需要观察的现象**：绝大多数方法的 `Notes` 都写着 `Thread group: Can be executed by any sized thread group`；少数（如 `lock_semaphore`）写着 `Must be executed by a single thread`。

**预期结果**：你会得到一张表，确认「通用指令 ≈ 与硬件代际无关、对线程数宽容的 tile 级算子集合」。

**待本地验证**：若你想亲眼确认，可在任意一个已有内核（如 `examples/matmul/matmul_v0.py`）上，给某个指令加上 `self.print_tensor("acc=", acc)` 并用 `tilus.option.debug.dump_ir()` 导出 IR，观察该调用确实生成了对应的 `InstStmt`。

#### 4.1.5 小练习与答案

**练习 1**：`global_view` 和 `global_tensor` 都是「得到一个 `GlobalTensor`」，它们的根本区别是什么？

> **答案**：`global_view` 把一个**已存在的裸指针**解释成带 dtype/shape/layout 的视图，不分配新显存；`global_tensor` 则**分配**一段新的全局内存（可由 `requires_clean` 控制是否清零），生命周期跟随整个 kernel。前者用于读用户传入的指针，后者用于申请 workspace。

**练习 2**：为什么 `dot` 的 docstring 说它「与架构无关」，却又标注了 `Requires compute capability 7.0+`？

> **答案**：`dot` 的**语义**（`out = a @ b + c`）与架构无关，用户无需关心后端细节；但它的**实现**会按目标硬件选择张量核 MMA（sm_70+）或 SIMT 后备路径。所以它对用户是「通用」的，对编译器是「可适配」的。这与 `wgmma.mma`（强制 Hopper、用户需自己管 fence/commit/wait）形成鲜明对比。

**练习 3**：下列哪条指令不是「通用指令」？A. `self.cast` B. `self.dot` C. `self.wgmma.mma` D. `self.sync`

> **答案**：C。`wgmma.mma` 属于硬件指令组（Hopper 专用），需要 `wgmma.` 前缀；其余三个都在 `RootInstructionGroup` 上，直接以 `self.` 调用。

---

### 4.2 硬件指令组：各 GPU 架构的能力分层

#### 4.2.1 概念说明

通用指令把可移植性留给了编译器，但代价是你无法精确控制新一代 GPU 的硬件单元——比如 Hopper 的 warp-group MMA、TMA 异步搬运引擎，Blackwell 的张量内存（TMEM）和第五代张量核。为了让你能直接、显式地驱动这些单元，Tilus 把它们封装成一个个 **硬件指令组（hardware instruction group）**，挂在 `self` 的不同属性下：

- `self.tma.*` —— TMA 引擎（Hopper+）
- `self.wgmma.*` —— warp-group MMA（Hopper）
- `self.tcgen05.*` —— 第五代张量核 + TMEM（Blackwell）
- `self.mbarrier.*` —— 内存屏障，给异步操作做同步（Hopper+）
- `self.atomic.*` —— tile 级原子读改写
- `self.fence.*` —— 跨内存代理（proxy）的定序
- `self.cluster.*` —— 多 CTA 簇同步（Hopper+）
- `self.clc.*` —— Cluster Launch Control，动态调度（Blackwell）

每个指令组的类 docstring 都会明确写出它**对应的架构**和**典型用途**，这正是本讲实践任务的依据。注意：硬件指令组对**线程组**的要求通常比通用指令严格——例如 `wgmma` 要求整整一个 warp group（4 个 warp = 128 线程）。

#### 4.2.2 核心流程

一条硬件指令从「用户调用」到「落到 PTX」要经过：

```text
self.wgmma.mma(a, b, d)        # 用户在 __call__ 里写
        │ （方法体里做形状/类型校验）
        ▼
self._builder.wgmma_mma_ss(...)  # 委托给 builder，生成一条 IR 指令
        │
        ▼
InstStmt(WgmmaMmaInst(...))      # 进入 Tilus IR
        │ （后续 Pass / 布局推理 / lowering）
        ▼
backends/emitters/cuda/wgmma.py  # 代码生成阶段发射 PTX: wgmma.mma_async.sync.aligned
```

这与通用指令的链路**完全一致**——差别只在于：硬件指令组生成的 IR 指令类型更专用、要求的线程组更苛刻、最终发射的 PTX 是特定代际才有的指令。换句话说，**通用指令和硬件指令组共享同一套机制，只是封装的「硬件能力粒度」不同**。

#### 4.2.3 源码精读

8 个硬件指令组在 `InstructionInterface` 中被组合进来（详见 4.3）。这里先看每个组的类定义与架构定位。

`WgmmaInstructionGroup`——Hopper 的 warp-group MMA，类 docstring 把它的异步协议讲得很清楚：

[python/tilus/lang/instructions/wgmma.py:24-43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py#L24-L43) —— 说明 WGMMA 以 warp group（4 个连续 warp、128 线程）为单位执行异步 MMA，必须遵循 `fence → mma → commit_group → wait_group` 四步协议。

`mma` 方法根据 `a` 在共享内存还是寄存器，分派到两条 builder 路径：

[python/tilus/lang/instructions/wgmma.py:94-131](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/wgmma.py#L94-L131) —— `a` 为 `SharedTensor` 时调用 `wgmma_mma_ss`（shared-shared），为 `RegisterTensor` 时调用 `wgmma_mma_rs`（register-shared）。docstring 标注 `Requires compute capability 9.0a+ (sm_90a)`，PTX 为 `wgmma.mma_async.sync.aligned`。

其余硬件指令组的定位（按类定义行号引用）：

| 指令组 | 类定义 | 架构 | 典型用途（取自类 docstring） |
| --- | --- | --- | --- |
| `tma` | [tma.py:23](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L23) | Hopper+ | TMA 引擎：global↔shared 异步批量搬运，支持 multicast |
| `tcgen05` | [tcgen05.py:25](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L25) | Blackwell (sm_100) | TMEM 分配/视图/搬运 + 第五代张量核 MMA |
| `mbarrier` | [mbarrier.py:23](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/mbarrier.py#L23) | Hopper+ | 64 位内存屏障，给 TMA/cp_async 等异步操作做同步 |
| `atomic` | [atomic.py:30](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/atomic.py#L30) | 通用 | tile 级原子读改写（element-wise 与 scatter 两种） |
| `fence` | [fence.py:20](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/fence.py#L20) | sm_80+ | proxy fence：generic 与 async 代理间的内存定序 |
| `cluster` | [cluster.py:24](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/cluster.py#L24) | Hopper+ | 多 CTA 簇：cluster 级同步、跨 CTA 共享内存寻址 |
| `clc` | [clc.py:22](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/clc.py#L22) | Blackwell | Cluster Launch Control：动态取消/抢占待启动的 cluster |

一个值得单独点出的细节：`fence` 组的存在解释了 CLAUDE.md 里那条著名提醒——在 `store_shared`（generic proxy 写）和 `tma.shared_to_global`（async proxy 读）之间必须插一道 `fence.proxy.async.shared::cta`：

[python/tilus/lang/instructions/fence.py:20-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/fence.py#L20-L38) —— 类 docstring 直接给出了「generic proxy 写 → async proxy 读需要 proxy fence」的典型场景。

> 小结：通用指令做「逻辑」，硬件指令组做「性能」。一个成熟的内核通常先用通用指令写正确，再用硬件指令组替换热点（例如把 `dot` 换成 `wgmma.mma`、把 `load_global` 换成 `tma.global_to_shared`）来榨取硬件红利。这条演进路线正是 [u7](u7-l1-ampere-matmul-deep-dive.md) 系列要讲的内容。

#### 4.2.4 代码实践

**实践目标**：浏览 docs 中 instruction-groups 的 RST，整理出「每个指令组对应 GPU 架构与典型用途」的表格。这是本讲指定的实践任务，也是日后选指令的依据。

**操作步骤**：

1. 列出全部 RST（它们与指令组一一对应）：
   ```bash
   ls docs/source/python-api/instruction-groups/
   # 预期看到: atomic.rst cluster.rst clc.rst fence.rst
   #           mbarrier.rst tcgen05.rst tma.rst wgmma.rst
   ```
2. 逐个打开 RST，例如 [docs/source/python-api/instruction-groups/wgmma.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/python-api/instruction-groups/wgmma.rst)，它用 `.. autoclass:: WgmmaInstructionGroup` 把对应 Python 类的 docstring 抽成文档，并在 `Instructions` 下用 `autosummary` 列出该组的全部方法（`fence` / `commit_group` / `wait_group` / `mma`）。
3. 对照 4.2.3 的源码表格，把「架构」一列从**源码类 docstring** 里逐字摘出来（不要凭印象），把「典型用途」用自己的话概括成一句话。
4. 额外：对每个指令组，记下它「要求的最小线程组」（在方法 docstring 的 `Notes` 段）。

**需要观察的现象**：RST 本身不重复写架构信息，而是通过 `autoclass` 复用 Python 源码里的 docstring——也就是说**文档和源码是同一份真相**。

**预期结果**：得到一张 8 行的表，列含 `指令组 | self 属性 | 架构 | 最小线程组 | 典型用途 | 代表方法`。它应与 4.2.3 的表格一致，但「代表方法」一列由你从 RST 的 `autosummary` 补全。

**待本地验证**：RST 渲染需要构建 Sphinx 文档；如果你没构建，直接读 `.rst` 原文与对应 `.py` 源码即可得到全部信息，不影响结论。

#### 4.2.5 小练习与答案

**练习 1**：你想在 Blackwell 上写一个用 TMEM 累加器的 matmul，应该用哪个指令组？它要求的最低算力是多少？

> **答案**：`self.tcgen05.*`。类 docstring（[tcgen05.py:25-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L25-L46)）说明 TMEM 是 Blackwell 引入的片上高带宽累加器空间，`alloc` 方法注明 `Requires compute capability 10.0+ (sm_100)`。

**练习 2**：`tma.global_to_shared` 是异步的，调用方立即返回。消费方如何知道数据已就绪？

> **答案**：通过 mbarrier。`tma.global_to_shared` 在发起搬运时会**自增**传入 mbarrier 的 tx-count，TMA 引擎完成搬运后**自减** tx-count；消费方调用 `self.mbarrier.wait(...)` 阻塞直到该阶段所有搬运完成（见 [tma.py:23-43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L23-L43) 的类 docstring）。这是「异步搬运 + mbarrier 同步」的标准生产者-消费者模式。

**练习 3**：为什么把通用 `dot` 换成 `wgmma.mma` 后，用户还要自己写 `fence/commit_group/wait_group`？

> **答案**：通用 `dot` 是**同步**语义（调用返回时结果已就绪），编译器替你处理了同步；而 `wgmma.mma` 是**异步**的——它只是「提交」一条 MMA 请求，结果要等 `wait_group` 之后才保证可见。把同步责任交给用户，是为了能在一个 commit group 里塞入多条 MMA、并用多个在途 group 来**掩盖延迟**（软件流水线）。这是用「显式控制」换取「更高性能」的典型取舍。

---

### 4.3 InstructionInterface 组合：self.* 上的统一接口与 builder context 机制

#### 4.3.1 概念说明

前面两节分别讲了通用指令和硬件指令组，但还没回答最关键的问题：**它们是怎么「挂」到 `self` 上的？用户在 `__call__` 里写 `self.load_global(...)` 或 `self.wgmma.mma(...)` 时，这些调用怎么就变成了 IR 语句？**

答案藏在三处：

1. **组合**：`InstructionInterface` 把 `RootInstructionGroup`（作为基类）和 8 个硬件指令组（作为类属性）组合在一起，形成一个「全功能」接口。
2. **继承**：用户的 `Script` 继承自 `InstructionInterface`，于是 `self` 天然拥有所有指令。
3. **全局 builder 上下文**：所有指令组都不自己持有 builder，而是通过一个进程级的全局变量 `_current_builder` 找到「当前正在转译的 builder」。转译器在执行用户 `__call__` 前，会把自己设为这个全局变量。

这套设计的好处是：**用户写的 `self` 和转译器内部完全是两套对象，但通过全局上下文「无缝对接」**。用户感觉自己在「调用方法」，实际上每次调用都是在往转译器的语句构造器里追加一条 IR 语句。

#### 4.3.2 核心流程

```text
        用户内核                           转译器（Transpiler）
        ────────                           ──────────────────
class Matmul(tilus.Script):           def build(...):
    def __call__(self, a, b):             ...
        ...                                with builder_context(self):   # ① 设全局 _current_builder = 转译器
        self.load_global(a, ...)    ───►      script.__call__(...)      # ② 执行用户代码
        self.wgmma.mma(...)         ───►          load_global(...)       # ③ 读全局 _current_builder
                                                       └─► builder.load_global(...)  # ④ 追加 InstStmt
                                                   wgmma.mma(...)
                                                       └─► builder.wgmma_mma_ss(...)
```

四个步骤：

1. 转译器进入 `with builder_context(self):`，把全局 `_current_builder` 设为自己。
2. 转译器调用用户的 `__call__`。
3. 用户代码里的 `self.load_global(...)` 触发 `InstructionGroup._builder` 属性，读取全局 `_current_builder`。
4. 拿到 builder 后，调用其 `load_global(...)` 方法，真正往 IR 里追加一条 `InstStmt`。

> 注意：`self`（用户的 `Matmul` 实例）和 `_current_builder`（转译器）是**不同对象**。用户的 `self` 提供方法签名和参数校验，builder 才是真正「干活」的那个。这种「用户对象只负责门面、全局上下文负责接线」的模式，是 Tilus 把 Python DSL 嵌入转译器的核心技巧。

#### 4.3.3 源码精读

**第 1 块：全局 builder 上下文**。`base.py` 用一个模块级全局变量 + 上下文管理器实现「当前 builder」：

[python/tilus/lang/instructions/base.py:19](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py#L19) —— 模块级 `_current_builder: Optional[StmtBuilder] = None`。

[python/tilus/lang/instructions/base.py:22-32](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py#L22-L32) —— `InstructionBuilderContext` 在 `__enter__` 时把全局变量设为传入的 builder，`__exit__` 时清空。

[python/tilus/lang/instructions/base.py:43-44](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py#L43-L44) —— `builder_context(builder)` 就是这个上下文管理器的工厂函数。

**第 2 块：指令组基类**。所有指令组（包括 `RootInstructionGroup` 和 8 个硬件组）都继承自 `InstructionGroup`，共享同一个 `_builder` 属性：

[python/tilus/lang/instructions/base.py:35-40](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py#L35-L40) —— `InstructionGroup` 只有一个 `_builder` 属性：`assert _current_builder is not None` 后返回它。这就是「指令不持有 builder、运行时去全局取」的实现。

**第 3 块：组合接口**。`InstructionInterface` 继承 `RootInstructionGroup`（从而拥有全部通用指令），并把 8 个硬件组作为**类属性**挂上：

[python/tilus/lang/instructions/__init__.py:30-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/__init__.py#L30-L38) —— 这里就是 `self.tma`/`self.wgmma`/`self.tcgen05`/… 的来源。注意每个硬件组都是一个**模块级单例实例**（如 `tma = TmaInstructionGroup()`），所有内核共享同一组指令组对象——这没问题，因为它们不带状态，状态全在全局 `_current_builder` 里。

**第 4 块：用户入口**。`Script` 继承 `InstructionInterface`，闭环形成：

[python/tilus/lang/script.py:41](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L41) —— `class Script(InstructionInterface)`。正因为这一句，你写的 `class Matmul(tilus.Script)` 的 `self` 才同时拥有 `self.load_global`（继承自 `RootInstructionGroup`）和 `self.wgmma.mma`（来自 `InstructionInterface` 的类属性）。

**第 5 块：转译器接线**。转译器在执行用户 `__call__` 前，用 `builder_context(self)` 把自己注册为当前 builder：

[python/tilus/lang/transpiler/transpiler.py:186-187](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L186-L187) —— `with builder_context(self): self.transpile_call(script.__call__, ...)`。`self`（Transpiler）本身是一个 `StmtBuilder` 子类，于是用户 `__call__` 体里的每次 `self.load_global(...)` 都会读到它，并往它的语句缓冲区追加 IR。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `self.load_global(...)` 调用，亲眼看清「用户 self → 全局 builder → builder.load_global」的完整路径，把第 4.3.2 节的流程图落实成代码定位。

**操作步骤**（源码阅读型实践）：

1. 从 [script.py:41](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L41) 出发，确认 `Script` 继承 `InstructionInterface`。
2. 打开 [__init__.py:30-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/__init__.py#L30-L38)，确认 `InstructionInterface` 继承 `RootInstructionGroup`——所以 `self.load_global` 来自 `RootInstructionGroup`。
3. 跳到 [root.py:472-522](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L472-L522)，看到 `load_global` 末尾调用 `self._builder.load_global(...)`。
4. 跳到 [base.py:36-40](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py#L36-L40)，确认 `self._builder` 返回的是全局 `_current_builder`。
5. 跳到 [transpiler.py:186](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L186)，确认正是转译器在 `with builder_context(self):` 里调用了用户 `__call__`，从而把「自己」设成了那个全局 builder。

**需要观察的现象**：这条链路上，`self` 在第 2-3 步指「用户内核实例」，在第 4-5 步指「转译器实例」——是两个不同的 `self`，靠全局变量桥接。

**预期结果**：你能用一句话讲清「为什么用户在 `__call__` 里写 `self.xxx(...)` 就能生成 IR」：因为转译器在调用 `__call__` 前把自己注册为全局 `_current_builder`，而所有指令的方法体最终都委托给这个 builder。

**待本地验证**：可在 `base.py` 的 `_builder` 属性里临时加一行 `print("builder resolved:", type(_current_builder))`（仅用于学习，勿提交），跑一个最小内核，观察打印时机与 `__call__` 执行的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：8 个硬件指令组都是模块级单例（`tma = TmaInstructionGroup()`），多个内核共享它们。为什么这不会出问题？

> **答案**：因为指令组对象**本身无状态**——它既不存 builder，也不存任何中间结果。所有「当前写到哪个 builder、当前线程组是什么」之类的动态信息，都通过全局 `_current_builder`（以及 builder 内部的 `tg_stack` 等结构）在运行时获取。共享一个无状态的门面对象完全安全。

**练习 2**：如果在用户 `__call__` 里**不在**转译上下文中直接调用 `self.load_global(...)`（比如在 `__init__` 里调），会发生什么？

> **答案**：会触发 [base.py:38-39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py#L38-L39) 的 `assert _current_builder is not None` 失败，抛出 `AssertionError`。因为此时全局 `_current_builder` 为 `None`——只有转译器进入 `with builder_context(self):` 后它才非空。这也是为什么指令只能写在 `__call__` 里、不能写在 `__init__` 里。

**练习 3**：假设你要新增一个硬件指令组 `self.foo.*`，至少要改哪两处？

> **答案**：(1) 新建一个 `FooInstructionGroup(InstructionGroup)` 类（如 `foo.py`），方法体里通过 `self._builder.xxx(...)` 委托；(2) 在 [__init__.py:30-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/__init__.py#L30-L38) 的 `InstructionInterface` 里加一行 `foo = FooInstructionGroup()` 并 import 它。之后用户的 `self.foo.*` 即可使用。（完整的新增指令还需要 IR 定义、布局推理规则、发射器等，那是 [u8-l5](u8-l5-writing-custom-pass-and-extension.md) 的主题。）

## 5. 综合实践

把本讲三个模块串起来，完成一次「指令考古」任务：

1. **建表**：执行 4.2.4 的实践任务，产出一张 8 行的「硬件指令组 → 架构 → 典型用途」表。
2. **归类**：再从 [root.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py) 里挑出 10 个通用指令，按「分配/视图/搬运/运算/同步/提示」归类。
3. **画图**：选一条具体调用（例如 `self.wgmma.mma(a, b, d)`），画出从「用户 self」到「PTX wgmma.mma_async」的完整路径，标出经过的源码文件与行号（参考 4.3.3 的五块结构）。
4. **判断**：假设你的目标 GPU 是 Ampere（sm_80），对照你建的表判断——上面 8 个硬件指令组里，哪些可用、哪些不可用？不可用的应该用什么替代？（提示：Ampere 上用 `dot` 走 MMA、用 `copy_async` 走 cp.async，而非 `wgmma`/`tma`。）

完成这项任务后，你不仅记住了一堆 API，更建立起了「指令从哪里来、按什么分层、如何落到硬件」的心智模型，这是后续阅读 [u3](u3-l1-compilation-pipeline-overview.md) 编译流水线和 [u7](u7-l1-ampere-matmul-deep-dive.md) 高性能内核实践的认知基础。

## 6. 本讲小结

- Tilus 的指令分两层：**通用指令**（`RootInstructionGroup`，与架构无关、对线程数宽容）与**硬件指令组**（8 个，按 GPU 代际封装专用硬件单元）。
- 通用指令涵盖分配（`register_tensor`/`shared_tensor`）、视图（`global_view`）、搬运（`load_global`/`store_global`/`copy_async`）、运算（`dot`/`cast`/`add`…）、同步（`sync`）与提示（`assume`/`range`），方法体统一委托 `self._builder`。
- 硬件指令组对应明确架构：`wgmma`=Hopper、`tcgen05`/`clc`=Blackwell、`tma`/`mbarrier`/`cluster`=Hopper+、`fence`=sm_80+、`atomic`=通用；它们对线程组要求更严（如 `wgmma` 需 128 线程）。
- `InstructionInterface` 用**组合**把 8 个硬件组挂为类属性，用**继承**让 `Script` 拥有全部通用指令。
- 关键机制是**全局 builder 上下文**：转译器在执行用户 `__call__` 前 `with builder_context(self)` 把自己注册为 `_current_builder`，所有指令通过 `InstructionGroup._builder` 读到它并追加 IR——这就是 `self.*` 变成 IR 语句的底层原理。
- 通用指令做「逻辑与可移植性」，硬件指令组做「性能与显式控制」；成熟内核通常先用通用指令写对，再用硬件组替换热点。

## 7. 下一步学习建议

- **横向**：阅读各硬件指令组的 RST 全文（`docs/source/python-api/instruction-groups/`），把每个组的全部方法列一遍，尤其是 `tma`、`mbarrier`、`tcgen05`——它们是 [u7](u7-l1-ampere-matmul-deep-dive.md) 的直接素材。
- **纵向（推荐下一步）**：进入 [u2-l3 控制流、线程组与 assume 提示](u2-l3-control-flow-and-thread-groups.md)，深挖本讲多次提到的 `thread_group`/`single_thread`/`warp_group` 上下文——它们决定了「这段指令由哪些线程执行」，是使用硬件指令组的前置条件。
- **更远**：等学完 [u3 编译流水线](u3-l1-compilation-pipeline-overview.md) 后再回头看本讲，你会更清楚 `self._builder.xxx(...)` 追加的那条 `InstStmt` 之后会经历怎样的 Pass 与代码生成，最终变成 PTX。
