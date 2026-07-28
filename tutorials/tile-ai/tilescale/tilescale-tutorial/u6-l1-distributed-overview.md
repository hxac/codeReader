# 分布式总览与 HDA 愿景

## 1. 本讲目标

本讲是「分布式编程」单元（Unit 6）的第一篇，也是 TileScale 区别于单机 TileLang 的核心差异化能力的「地图课」。读完本讲，你应当能够：

- 说清 **HDA（层次化分布式架构）** 这个愿景要解决什么问题，它由哪三类基础资源构成。
- 区分两件事：README 里宣传的 `T.Scale` / `T.Kernel(device=, cta_cluster=)` 是**愿景（待确认）**，而仓库里真正落地的，是「单机 `T.Kernel` + NVSHMEM 多设备原语」这条务实路线。
- 在脑子里建立分布式子系统的目录地图：前端 DSL 原语、主机端运行时、`pynvshmem`、`tilescale_ext`、DeepEP 各自负责什么。
- 建立 **PE（处理单元）/ rank** 的基本概念，理解「同一段 kernel 代码在多个 PE 上同时跑（SPMD）」是怎么回事。

> 本讲只画地图、建立认知，不深入任何一个原语的参数细节——那是 u6-l2 之后各篇的任务。

## 2. 前置知识

本讲假设你已经具备 u1-l1（项目定位）和 u3-l1（编译总览）的认知，即：

- TileScale 是 TileLang 的分布式扩展，Python 包名仍叫 `tilelang`，底层编译栈基于 TVM。
- 一个 TileLang 程序的入口是 `@T.prim_func` 标注的内层函数 + `@tilelang.jit`/`tilelang.compile` 编译，产物是一个可在单 GPU 上启动的 `JITKernel`（grid/block 由 `T.Kernel` 声明）。

此外，本讲会用到几个分布式系统的通用概念，先用大白话解释：

- **SPMD（Single Program Multiple Data）**：同一段程序代码，复制多份，在多个「执行者」上同时跑，每个执行者处理不同的数据、或走不同的分支。CUDA 的 grid 启动是线程级 SPMD；多进程分布式是进程级 SPMD。
- **对称内存（symmetric memory）**：所有参与方各有一块**同样大小、逻辑对称地址**的显存。一方只要知道对方的「编号」就能读写对方那块显存——这是 NVSHMEM 的核心前提。
- **rank / PE**：分布式运行中，给每个执行者编一个号；本讲里 rank 与 PE（Processing Element，处理单元）是同一件事的两种叫法（详见 4.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md) | 项目定位、HDA 愿景、tile 接口，以及大量 `T.Scale` 示例（这些是愿景，需甄别）。 |
| [tilelang/distributed/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/__init__.py) | 主机端运行时入口：导出 `init_distributed` 等工具，并强制依赖 `tilescale_ext`。 |
| [tilelang/language/distributed/common.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py) | 前端 DSL：CP-engine 路线的分布式原语（`get_rank`/`put_block`/`wait_*`）。 |
| [tilelang/language/distributed/multi_device/nvshmem.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py) | 前端 DSL：NVSHMEM 路线的远程通信原语（`get_pe`/`putmem`/`signal`/`barrier`）。 |
| [examples/distributed/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/README.md) | 分布式示例的运行前置（编译 NVSHMEM、装 pynvshmem、启动方式）。 |
| [tilelang/distributed/utils.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py) | 主机端运行时实现：`init_distributed`、对称张量创建、IPC handle 同步、`perf_fn` 测速。 |
| [tilelang/distributed/launch.sh](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh) | 多进程启动脚本（`torch.distributed.run`），并设置分布式模式开关。 |
| [examples/distributed/example_simple_shift.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_simple_shift.py) | 最小的 NVSHMEM 路线示例：每个 PE 把数据 put 给下一个 PE。 |
| [examples/distributed/primitives/example_put_block.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py) | CP-engine 路线示例：用 `get_rank`/`put_block` + IPC 对称内存做配对拷贝。 |

---

## 4. 核心概念与源码讲解

### 4.1 HDA 愿景 vs NVSHMEM 实现现状

#### 4.1.1 概念说明

README 把 TileScale 的核心抽象叫做 **HDA（Hierarchical Distributed Architecture，层次化分布式架构）**。它的动机是：进入 scaling-law 时代后，AI 算力在两个方向上同时「变分布」——

- **芯片间（inter-chip）**：大模型跑在多 GPU、多节点上，靠 NVLink / InfiniBand 互联；
- **芯片内（intra-chip）**：下一代加速器用 3D IC、近存/存内计算、晶圆级集成，单芯片内部也是分布式的。

HDA 想把这两类资源统一抽象成**一个虚拟的「mega-device」**，让用户只写 tile 级的计算/通信逻辑，编译器自动调度计算、访存、通信及其 overlap。

HDA 建立在**三类基础资源**之上：

1. **compute（计算单元）**：可层层组合，GPU 上是 thread → warp → SM → GPU → node；
2. **memory（存储）**：分多层，每层要么被同层计算单元**共享**（如 block 内 shared memory），要么**分发**给各单元私有（如寄存器）；
3. **network（网络）**：把同层并行单元连起来，芯片内是 NoC（如 Hopper CTA cluster 的片上网络），芯片间是 NVLink，节点间是 InfiniBand。

对应地，README 设计了三类 tile 接口：**compute / memory / communicate**，并宣称「同一原语可用于不同 scale」。

#### 4.1.2 核心流程（愿景版 vs 现实版）

**README 描绘的愿景流程**（用一个原语 `T.Scale` 控制算力层级）：

```
with T.Kernel(device=(4), block=(...), cta_cluster=(2), threads=256):
    with T.Scale("device") as dev_id, dev_num:   # 4 GPU 各自一份
        ...
    with T.Scale("block") as bx, by:             # block 级
        with T.Scale("warpgroup") as wg_id, wg_num:  # warp 级
            T.gemm(...); T.allreduce(...)         # 编译器自动插通信 + overlap
```

也就是说，**一个 `T.Kernel` 启动就横跨多 GPU**，用 `T.Scale(...)` 选择在哪一层做 SPMD，编译器负责跨层通信。

**仓库里真正落地的现实流程**（没有 `T.Scale`，靠「多进程 + NVSHMEM」摊平层级）：

```
# 主机端：用 launch.sh 拉起 N 个进程，每个进程绑定 1 个 GPU（= 1 个 PE）
init_distributed()                      # 建 torch.distributed 组 + 初始化 NVSHMEM
kernel = tilelang.compile(prim_func)    # 编译出一个【单 GPU】 kernel
kernel.initialize(allocator=...)        # 把各 PE 的远程基址表注入 kernel
kernel(local_A, local_B)                # 每个 PE 跑【同一份】单 GPU kernel
```

关键差别：现实里**层级被摊平**了——不是在一个 kernel 内用 `T.Scale("device")` 跨 GPU，而是**启动 N 个进程，每个进程是一个 device-PE**，各自跑一份普通的单机 `T.Kernel`，PE 之间通过 NVSHMEM 远程原语（`putmem`/`getmem`/`signal`/`barrier`）或 CP-engine 原语（`put_block`/`get_block`）通信。SPMD 发生在「进程之间」，而不是「一次 kernel 启动之内」。

#### 4.1.3 源码精读

**(a) HDA 愿景的「官方表述」在 README。** 三类资源、mega-device 的提法都在这里：

> [README.md:12-25](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L12-L25) —— 定义 HDA，点明它建立在 compute / memory / network 三类资源之上，把整个分布式系统虚拟成一个统一的 mega-device。

> [README.md:16](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L16) —— 原文「HDA is built upon three fundamental resources: *compute units, memory, and network*」。

**(b) `T.Scale` 是愿景的核心标志，但代码里根本不存在。** README 用它演示层级化 SPMD：

> [README.md:62-69](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L62-L69) —— 引入 `T.Scale` 原语，并给出 `with T.Scale("warp"): T.gemm(...)` 示例。

> [README.md:74-92](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L74-L92) —— 用 `T.Kernel(cta_cluster=(2), ...)` + `T.Scale("cta_cluster")` 表达 cluster 级 GEMM，用 `T.Scale("warpgroup") as wg_id, wg_num` 做 warp 特化。

> [README.md:103-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L103-L145) —— 一个 4-GPU Tensor Parallel GEMM 完整示例，通篇用 `T.Kernel(device=(4), ...)`、`T.Scale("device"/"block"/"warpgroup")`、`T.view(layout=...)`、`T.allreduce`。

但如果你在整个 `tilelang/` 源码树里搜 `def Scale`、`Scale =`、`"T.Scale"`、`cta_cluster`，**一个匹配都没有**——这些原语从未实现。`T.Kernel` 的真实签名也根本没有 `device=` / `cta_cluster=` 参数：

> [tilelang/language/kernel.py:228-233](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L228-L233) —— 真实的 `Kernel(*blocks, threads=None, is_cpu=False, prelude=None)`，参数里只有 grid 的 `*blocks`、`threads`、`is_cpu`、`prelude`，没有任何层级/设备维度的入参。

这就是本讲最重要的判断：**README 的 HDA 完整愿景（含 `T.Scale`、跨多 GPU 的单次 `T.Kernel` 启动）目前是「待确认」，真正能用的是下面这条 NVSHMEM 多设备路线。**

**(c) README 自己也承认项目处于早期实验阶段：**

> [README.md:239-240](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L239-L240) —— 「TileScale is in its early experimental stage」，并号召社区贡献。这与「愿景多、落地少」的现状相互印证。

**(d) 真正落地的路线：多进程 + NVSHMEM。** 看最小的真实示例 `example_simple_shift.py`——它没有任何 `T.Scale`，`T.Kernel` 也是普通的单机启动，分布式完全靠 `T.get_pe()` 取编号 + `T.putmem_nbi_block()` 远程写入：

> [examples/distributed/example_simple_shift.py:13-21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_simple_shift.py#L13-L21) —— 一个 ring-shift：每个 PE 用 `T.get_pe()`/`T.get_pe_num()` 算出下一个 PE 的编号 `peer = (mype+1) % npes`，再用 `T.putmem_nbi_block` 把本地 `A` put 到下一个 PE 的 `B`。

> [examples/distributed/example_simple_shift.py:26-30](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_simple_shift.py#L26-L30) —— 主机端 `init_distributed()` + 普通 `tilelang.compile`，注意编译出的就是一个**单 GPU** kernel，多 PE 是靠多进程（`launch.sh`）摊开的。

#### 4.1.4 代码实践

> **实践目标**：亲手验证「README 宣传的能力」与「代码已实现的能力」之间的落差，建立对「愿景 vs 现状」的清醒认知。这是本讲的核心实践任务，也是规格里指定的任务。

**操作步骤**（纯源码阅读，无需 GPU）：

1. 打开 [README.md:62-100](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L62-L100) 与 [README.md:103-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L103-L145)，把里面出现的所有「宣传的能力」列出来，至少包括：`T.Scale("warp"/"cta_cluster"/"device"/"warpgroup")`、`T.Kernel(device=..., cta_cluster=...)`、`T.view(layout=T.FullCol/FullRow/Replica)`、`T.alloc(..., level="l0"/"l1"/"l2")`、`T.allreduce`、编译器自动 overlap。
2. 对每一项，用搜索工具在 `tilelang/` 源码树里查证它是否真实存在。可执行：
   ```bash
   # 期望：在 tilelang/ 下 0 命中（仅 README 有）
   grep -rn "def Scale\|T\.Scale\|cta_cluster" tilelang/
   grep -rn "FullCol\|FullRow\|level=\"l0\"\|level=\"l2\"" tilelang/language/
   ```
3. 同时查证「已实现」的真实入口，例如：
   ```bash
   grep -rn "def get_pe\|def putmem\|def signal_wait_until" tilelang/language/distributed/
   grep -rn "def put_block\|def get_rank" tilelang/language/distributed/common.py
   ```
4. 把结果整理成下面这张表（**参考答案**，依据见行号）：

| README 宣传的能力 | 现状 | 依据 |
| --- | --- | --- |
| `T.Scale("warp"/"device"/"cta_cluster"/"warpgroup")` | **待确认（未实现）** | `tilelang/` 内搜 `Scale`/`cta_cluster` 零命中 |
| `T.Kernel(device=(4), cta_cluster=(2))` | **待确认（未实现）** | [kernel.py:228-233](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L228-L233) 签名无此参数 |
| `T.view(layout=T.FullCol/FullRow/Replica)` 跨设备布局 | **待确认** | 该布局枚举未见实现 |
| `T.alloc(..., level="l0"/"l1"/"l2")` 层级分配 | **待确认** | 现实用 `alloc_shared`/`alloc_fragment`（见 u2-l2），无 `level=` |
| `T.allreduce`（设备/cluster 级） | **待确认** | 未见对应 intrin |
| NVSHMEM 远程原语 `putmem`/`getmem`/`signal`/`barrier` | **已实现** | [multi_device/nvshmem.py:97-176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L97-L176) |
| CP-engine 远程原语 `put_block`/`get_block`/`wait_*` | **已实现** | [common.py:83-118](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L83-L118) |
| `T.get_pe()` / `T.get_rank()` 取本端编号 | **已实现** | 见 4.3 |
| 多进程分布式启动 | **已实现** | [launch.sh](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh) |

**需要观察的现象**：第 2 步的 grep 在 `tilelang/` 下应**零命中** `T.Scale`/`cta_cluster`；第 3 步应有命中。这正是「愿景 vs 现实」最直接的代码证据。

> 说明：本实践为「源码阅读型实践」，无需运行；若想进一步在多卡环境验证「已实现」项，见 4.2.4。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `T.Scale` 即使实现了，和「多进程 NVSHMEM」也是两种不同的 SPMD 粒度？

> **参考答案**：`T.Scale` 的愿景是**在一次 kernel 启动内**选择算力层级做 SPMD（层级在芯片内/芯片间连续展开）；而现实的多进程 NVSHMEM 是**进程级 SPMD**——每个进程启动一次普通的单机 kernel，层级被「摊平」成「一个进程 = 一个 device-PE」，跨 PE 通信靠显式的远程原语完成，编译器并不自动 overlap。

**练习 2**：README 的 4-GPU GEMM 示例（[README.md:107-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L107-L145)）现在能直接跑吗？为什么？

> **参考答案**：不能。它依赖 `T.Kernel(device=(4))`、`T.Scale("device"/"warpgroup")`、`T.view(layout=...)` 等尚未实现的愿景原语。要实现同等功能，目前需用「多进程 + NVSHMEM/CP-engine 原语」手工拆解（参考 `examples/distributed/example_summa.py` 等，见 u6-l7）。

---

### 4.2 分布式子系统目录与职责

#### 4.2.1 概念说明

「分布式」不是某一个文件，而是横跨前端 DSL、主机运行时、Python 绑定、C++ 扩展、外部库的一整套子系统。理解它，关键是把目录和职责对上号。TileScale 的分布式子系统可以分成五块：

1. **前端 DSL 原语**：用户在 `@T.prim_func` 里写的 `T.putmem`、`T.get_pe`、`T.put_block` 等，都只是拼装一条 `tir.call_intrin`（这点和 u2-l3 的计算原语一致），真正的远程指令由 C++ lowering 生成。
2. **主机端运行时**：初始化进程组、创建对称显存张量、跨进程同步 IPC handle、测速。
3. **`pynvshmem`**：NVSHMEM C 库的 Python 封装，提供主机端 API。
4. **`tilescale_ext`**：一个 C++ 扩展（产物装在顶层 `tilescale_ext/`），负责「从裸指针构造 torch 张量」和「跨进程 IPC 显存共享」。
5. **DeepEP**：第三方 MoE 专家路由库，作为可选集成。

#### 4.2.2 核心流程（一次分布式运行的组成）

```
                       ┌─ 前端 DSL（tilelang/language/distributed/）
                       │    T.putmem / T.get_pe / T.put_block ...  → call_intrin
用户 kernel ─compile─► │
                       │
                       ├─ 主机运行时（tilelang/distributed/）
                       │    launch.sh 多进程 → init_distributed() → 编译 → initialize() → 调用
                       │
                       ├─ pynvshmem（tilelang/distributed/pynvshmem/）
                       │    init_nvshmem_by_uniqueid / 创建对称堆张量 / barrier
                       │
                       └─ tilescale_ext（顶层产物，源在 tilelang/utils/ts_ext/）
                            _create_tensor / _create_ipc_handle / _sync_ipc_handles
```

#### 4.2.3 源码精读

**(a) 主机端运行时入口强制依赖 `tilescale_ext`：**

> [tilelang/distributed/__init__.py:1-4](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/__init__.py#L1-L4) —— `from .utils import *` 之后，**无条件** `from tilescale_ext import _create_tensor, _create_ipc_handle, _sync_ipc_handles`。意味着：只要 `import tilelang.distributed`，就必须装好 `tilescale_ext`，否则直接 ImportError。

> 注意这与 `import tilelang`（顶层包）不同：顶层包把 `tilescale_ext` 当**可选**依赖（[tilelang/__init__.py:151-158](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L151-L158) 把 `tensor`/`get_allocator` 在缺失时置为 `None`）。也就是说「核心单机能力」不依赖分布式扩展，但「分布式子系统」依赖。

**(b) `tilescale_ext` 的产物与职责：**

> [tilescale_ext/__init__.py:1-7](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilescale_ext/__init__.py#L1-L7) —— 从编译产物 `tilescale_ext._C` 导出 `tensor_from_ptr`、`_create_tensor`、`_create_ipc_handle`、`_sync_ipc_handles`、`create_host_device_tensor`。前缀 `_` 的是内部 API，供 `tilelang.distributed.utils` 调用（详见 u6-l5）。

**(c) 主机端运行时的关键函数都在 `utils.py`：**

> [tilelang/distributed/utils.py:66-97](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L66-L97) —— `init_distributed`：从环境变量读 `WORLD_SIZE`/`RANK`/`LOCAL_RANK`，初始化 NCCL 进程组与 `TP_GROUP`，并在 `init_nvshmem=True` 时调 `pynvshmem.init_nvshmem_by_uniqueid(TP_GROUP)` 把 NVSHMEM 也建起来。返回值里 `TP_GROUP` 是所有 rank 的通信组。

> [tilelang/distributed/utils.py:100-129](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L100-L129) —— 对称/分布式张量的创建链：`create_tensor` 调 `tilescale_ext._create_tensor`（显式 `cudaMalloc`，因 IPC 只认这种分配），`create_dist_tensor` 用 NCCL `all_gather_object` 把各进程的 IPC handle 收集起来，再调 `_sync_ipc_handles` 在 GPU 上建立可远程寻址的指针表。

**(d) 多进程启动脚本：**

> [tilelang/distributed/launch.sh:4-5](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L4-L5) —— 用两个环境变量打开分布式模式：`TILELANG_USE_NVSHMEM=1`、`TILELANG_USE_DISTRIBUTED=1`。

> [tilelang/distributed/launch.sh:41-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/launch.sh#L41-L45) —— 实际启动命令是 `python -m torch.distributed.run --nproc_per_node=<GPU数> --nnodes=<节点数> ...`，即用 PyTorch 的 torchrun 拉起多进程，每个进程绑一张卡。这就是「多进程摊平层级」的落地点。

**(e) 前端 DSL 原语如何暴露成 `T.*`：**

> [tilelang/language/__init__.py:111-113](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/__init__.py#L111-L113) —— 把 `distributed.multi_device.nvshmem`、`multi_device.cpengine`、`distributed.common` 三个模块 `import *` 进 `tilelang.language` 命名空间，所以示例里能直接写 `T.get_pe()`、`T.putmem_nbi_block()`、`T.put_block()`。

**(f) 分布式示例的运行前置：**

> [examples/distributed/README.md:12-30](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/README.md#L12-L30) —— 跑分布式示例前，要先 `source build_nvshmem.sh` 编出 NVSHMEM 设备库，再 `python setup.py install` 装 `pynvshmem`，并设好 `LD_LIBRARY_PATH`。这印证了「分布式子系统 = TileLang 核心 + NVSHMEM + pynvshmem + tilescale_ext」的组合关系。

#### 4.2.4 代码实践

> **实践目标**：在不读实现细节的前提下，只用目录结构和入口文件，画出分布式子系统的「职责地图」，并验证 `tilescale_ext` 的「可选 vs 必需」边界。

**操作步骤**（源码阅读 + 可选运行）：

1. 用 `git ls-files` 列出五个子系统的文件归属（**示例命令**，可在仓库根执行）：
   ```bash
   git ls-files tilelang/language/distributed | head      # 前端 DSL
   git ls-files tilelang/distributed | grep -v pynvshmem  # 主机运行时
   git ls-files | grep -E "pynvshmem/(src|python)"        # pynvshmem
   git ls-files tilelang/utils/ts_ext                      # tilescale_ext 源码
   git ls-files | grep -i deepep | head                   # DeepEP
   ```
2. 对照 [tilelang/__init__.py:151-158](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L151-L158)（顶层 `import tilelang` 把 `tilescale_ext` 当可选）与 [tilelang/distributed/__init__.py:4](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/__init__.py#L4)（`import tilelang.distributed` 把它当必需），用自己的话解释这两层 import 策略的区别与目的。
3. 画一张目录树，给每个关键目录标注「前端 DSL / 主机运行时 / pynvshmem / C++ 扩展 / 第三方集成」之一的职责标签。

**需要观察的现象**：第 1 步应看到 `tilescale_ext` 的**源码**在 `tilelang/utils/ts_ext/`，而**产物**导入路径却是顶层 `tilescale_ext/_C`——这种「源码与产物分离」是 TileScale 的刻意设计（见 u1-l4）。

**预期结果**：得到一张五块职责清晰的子系统地图。第 2 步的结论应类似：「顶层 import 容错，保证单机用户不受影响；分布式子模块强制依赖，因为缺了 `tilescale_ext` 根本无法创建对称张量。」

> 运行验证（**待本地验证**，需多卡 + NVSHMEM）：若本机有多张 GPU 且已按 [examples/distributed/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/README.md) 编好 NVSHMEM/pynvshmem，可执行 `./tilelang/distributed/launch.sh examples/distributed/example_simple_shift.py`，观察是否拉起 `nproc_per_node` 个进程。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tilescale_ext` 在「顶层 `import tilelang`」时可选、在「`import tilelang.distributed`」时必需？

> **参考答案**：顶层包要保证只做单机 kernel 的用户即使没装分布式扩展也能正常 `import tilelang`，所以用 try/except 把 `tensor`/`get_allocator` 置 `None`；而 `tilelang.distributed` 这个子模块本身的存在意义就是分布式，它的对称张量创建（`_create_tensor`）、IPC 同步（`_sync_ipc_handles`）离开 `tilescale_ext` 无法实现，因此无条件导入、失败即报错。

**练习 2**：`launch.sh` 用 `torch.distributed.run` 拉起多进程，这和「`T.Scale("device")` 在一次启动内跨多 GPU」有何本质不同？

> **参考答案**：torchrun 拉起的是**多个独立 Python 进程**，每个进程各自编译并启动一次单机 kernel，靠 NVSHMEM 对称堆通信；而 `T.Scale("device")` 的愿景是**单个进程、单次 kernel 启动**就跨多 GPU。前者是进程级 SPMD（已实现），后者是 launch 内层级 SPMD（待确认）。

---

### 4.3 PE（处理单元）/ rank 基本概念

#### 4.3.1 概念说明

**PE（Processing Element，处理单元）** 是分布式运行中「一个执行者」的抽象。在 TileScale 当前的实现里，**一个 PE = 一个进程 = 一张 GPU**（由 `launch.sh` 按每卡一进程拉起）。每个 PE 有：

- 一个**编号**：本端是第几号、一共多少号；
- 一块**对称显存**：与其它 PE 同样大小、可被远程寻址的缓冲区；
- 一份**相同的 kernel 代码**：所有 PE 跑同一份 `T.Kernel`（SPMD），靠编号区分行为。

「rank」是 MPI/NCCL/torch.distributed 世界的习惯叫法，「PE」是 NVSHMEM 世界的习惯叫法。在 TileScale 里**两者指同一个东西**，但不幸的是仓库里两套原语各用各的命名（见 4.3.3），初学者最容易在这里犯迷糊——所以本节专门把它讲透。

#### 4.3.2 核心流程（PE 视角的一次 ring-shift）

```
PE 0:  me=0, n=4, peer=(0+1)%4=1  →  把本地 A put 给 PE1 的 B
PE 1:  me=1, n=4, peer=(1+1)%4=2  →  把本地 A put 给 PE2 的 B
PE 2:  me=2, n=4, peer=(2+1)%4=3  →  把本地 A put 给 PE3 的 B
PE 3:  me=3, n=4, peer=(3+1)%4=0  →  把本地 A put 给 PE0 的 B
```

每个 PE 执行**完全相同**的代码，只因 `me` 不同而算出不同的 `peer`，这就是进程级 SPMD。编号 `me` 在 kernel 内由 `T.get_pe()`（NVSHMEM 路线）或 `T.get_rank()`（CP-engine 路线）取得；总数 `n` 由 `T.get_pe_num()` / `T.get_num_ranks()` 取得。

#### 4.3.3 源码精读（两套「我是谁」原语）

仓库里有两套并行的分布式原语，分别走不同的 C++ intrin，命名也不同：

**(a) NVSHMEM 路线——用 PE 命名：**

> [tilelang/language/distributed/multi_device/nvshmem.py:6-13](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L6-L13) —— `get_pe()` → `tl.GetPE`、`get_pe_num()` → `tl.GetPENum`。配套的是 `putmem`/`getmem`/`signal`/`barrier` 等 NVSHMEM 风格原语（见 u6-l2）。`example_simple_shift.py` 用的就是这套。

**(b) CP-engine 路线——用 rank 命名：**

> [tilelang/language/distributed/common.py:11-18](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L11-L18) —— `get_rank()` → `tl.get_rank`、`get_num_ranks()` → `tl.get_num_ranks`。配套的是 `put_block`/`get_block`/`wait_*` 等 CP-engine 原语（见 u6-l3）。

> [tilelang/language/distributed/common.py:83-99](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L83-L99) —— `put_block(src, dst, size, dst_pe=-1)`：注意参数名叫 `dst_pe`（目的 PE 编号），`-1` 表示本地拷贝。代码注释也点明「block 级通信当前基于 NVSHMEM-style copy 实现」。

**(c) 两套命名在示例里的真实使用：**

> [examples/distributed/example_simple_shift.py:17-21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_simple_shift.py#L17-L21) —— NVSHMEM 路线：`mype[0]=T.get_pe()`、`npes[0]=T.get_pe_num()`、`peer=(mype+1)%npes`，再 `T.putmem_nbi_block(..., peer)`。

> [examples/distributed/primitives/example_put_block.py:21-30](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py#L21-L30) —— CP-engine 路线：`rank[0]=T.get_rank()`、`num_rank[0]=T.get_num_ranks()`，`T.put_block(..., dst_pe=rank[0]^1)`（用异或实现两两配对：0↔1、2↔3…）。这里 `get_rank` 取的「rank」和 `put_block` 的 `dst_pe` 其实是同一个编号空间。

**(d) 主机端的 PE 身份来自环境变量与进程组：**

> [tilelang/distributed/utils.py:66-80](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/utils.py#L66-L80) —— `WORLD_SIZE`/`RANK`/`LOCAL_RANK` 三个环境变量决定本进程的 PE 身份（`RANK` 是全局编号，`LOCAL_RANK` 是节点内 GPU 编号），再用 `torch.distributed.init_process_group` 建组。kernel 内的 `T.get_pe()`/`T.get_rank()` 最终读到的就是这套身份。

**(e) 把远程基址表注入 kernel：**

> [examples/distributed/primitives/example_put_block.py:41-46](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py#L41-L46) —— CP-engine 路线的关键一步：`tilelang.get_allocator(..., is_distributed=True, ...)` 建分布式分配器，`kernel.initialize(allocator=allocator)` 把各 PE 的远程基址表注入编译好的 kernel。这样 kernel 内的 `put_block(..., dst_pe=k)` 才知道「PE k 的目标缓冲区在哪」。这是承接 u3-l6「`init_table` 注入 rank/远程基址表」的具体入口。

#### 4.3.4 代码实践

> **实践目标**：通过对比两个真实示例，彻底厘清「PE 与 rank 是同一概念、但分属两套原语」这一点。

**操作步骤**（源码阅读型）：

1. 并排打开 [example_simple_shift.py:17-21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_simple_shift.py#L17-L21)（NVSHMEM 路线，`get_pe`/`get_pe_num`/`putmem_nbi_block`）与 [example_put_block.py:21-30](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py#L21-L30)（CP-engine 路线，`get_rank`/`get_num_ranks`/`put_block`）。
2. 在两张纸上各画一个「4 PE 环形/配对」示意：标注每个 PE 的编号、peer 编号、数据流方向。
3. 用一句话回答：「`T.get_pe()` 和 `T.get_rank()` 返回的值，在同一个 4 进程任务里，对同一个进程是否相等？」

**需要观察的现象**：两个示例的 SPMD 结构完全同构——都是「取本端编号 → 算对端编号 → 远程写」，只是取编号的原语和远程写的原语来自两套不同的 intrin。

**预期结果**：能说出「相等/同义」——它们是同一编号空间的两种 API 叫法，区别只在底层走 NVSHMEM 还是 CP-engine。**待本地验证**：若在多卡环境分别跑这两个示例并打印编号，可进一步确认两者数值一致。

#### 4.3.5 小练习与答案

**练习 1**：`example_put_block.py` 里 `dst_pe = rank[0] ^ 1`，请说明 4 个 PE 时各 PE 的对端分别是谁。

> **参考答案**：`^1` 把最低位翻转，故 PE0↔PE1、PE2↔PE3 两两配对。这是一种常见的「相邻配对」写法，常用于 allreduce 的蝶形（butterfly）步骤。

**练习 2**：为什么 kernel 里要先 `T.alloc_local([1], "int32")` 再 `mype[0] = T.get_pe()`，而不是直接写 `peer = (T.get_pe()+1) % T.get_pe_num()`？

> **参考答案**：`T.get_pe()` 返回的是一个 TIR `call_intrin` 表达式，每次调用都生成一条新的 intrin；用 `alloc_local` 把它存进局部变量再复用，可以避免在同一段代码里重复发射取编号的指令，也便于调试时观察编号值。这是 TileLang 里处理「运行时取值」的常见写法。

---

## 5. 综合实践

> **综合任务**：把本讲三个模块串起来，产出一份**「TileScale 分布式能力速查表」**，作为你阅读 u6-l2 之后各篇的随身参考。

要求：

1. **愿景 vs 现状**：从 [README.md:62-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L62-L145) 提取所有 HDA/T.Scale 宣传点，标注「待确认」，并对应写出仓库里**真正替代它**的实现（例：`T.Scale("device")` → 多进程 + `launch.sh`；`T.allreduce` → 手写 `putmem`+`reduce`，见 u6-l7）。
2. **子系统地图**：用 4.2 的五块分类，给下列每个路径标注职责，并写出一句话说明：
   - `tilelang/language/distributed/common.py`
   - `tilelang/language/distributed/multi_device/nvshmem.py`
   - `tilelang/distributed/utils.py`
   - `tilelang/distributed/launch.sh`
   - `tilelang/distributed/pynvshmem/`
   - `tilescale_ext/__init__.py`（产物）与 `tilelang/utils/ts_ext/`（源码）
3. **PE 模型**：用 4 PE 画一个 ring-shift 时序图，标出每个 PE 的 `get_pe()` 值、peer 值、以及一次 `putmem_nbi_block` 的源/目的，并指出同步点（提示：`putmem_nbi` 是非阻塞的，需要 `quiet`/`barrier_all` 才能保证送达，见 u6-l2）。

完成后，你应能用一句话向别人解释：「TileScale 现在的分布式，就是把 HDA 愿景暂时摊平成多进程 NVSHMEM，每个进程一个 PE，跑同一份单机 kernel，靠远程原语通信。」

## 6. 本讲小结

- **HDA 是愿景基石**：它要把 compute/memory/network 三类资源统一抽象成一个「mega-device」，让用户只写 tile 级 compute/memory/communicate 逻辑。
- **`T.Scale` / `T.Kernel(device=, cta_cluster=)` 目前是「待确认」**：在整个 `tilelang/` 源码树里搜不到实现，`T.Kernel` 真实签名也没有这两个参数。
- **真正落地的是「多进程 + NVSHMEM」**：用 `launch.sh`（torchrun）每卡拉一个进程，每个进程是一个 PE，跑同一份单机 `T.Kernel`，层级被摊平成进程级 SPMD。
- **分布式子系统分五块**：前端 DSL 原语（`tilelang/language/distributed/`）、主机运行时（`tilelang/distributed/utils.py` 等）、`pynvshmem`、`tilescale_ext`（C++ 扩展，源在 `tilelang/utils/ts_ext/`）、DeepEP。
- **PE 与 rank 同义**：NVSHMEM 路线叫 `get_pe`/`get_pe_num`，CP-engine 路线叫 `get_rank`/`get_num_ranks`，是同一编号空间的两种 API。
- **远程基址靠 `kernel.initialize(allocator=...)` 注入**：这是把主机端建好的对称堆/IPC 指针表喂给编译好的 kernel 的关键入口。

## 7. 下一步学习建议

本讲只画了地图。接下来按依赖顺序深入：

- **u6-l2 NVSHMEM 多设备通信原语**：逐个拆解 `putmem`/`getmem`/`signal`/`barrier`/`quiet`/`fence` 的语义与同步规则（本讲提到的「`putmem_nbi` 非阻塞、需 `quiet`/`barrier`」会在那里讲清）。
- **u6-l3 CP-engine 远程 get/put 原语**：拆解 `put_block`/`get_block`/`wait_*` 与 `unroll_factor`、向量化。
- **u6-l4 pynvshmem 与启动**：深入 `init_distributed`、对称堆张量创建与 `launch.sh` 多进程细节。
- **u6-l5 IPC 张量与 tilescale_ext 内存管理**：拆解 `_create_tensor`/`_create_ipc_handle`/`_sync_ipc_handles` 的所有权与跨进程共享机制。
- **u6-l7 分布式实战**：用 allgather / all2all / SUMMA 等示例把通信与计算 overlap 串起来。

建议在进入 u6-l2 前，先把本讲 4.1.4 的「愿景 vs 现状」表和 4.2 的子系统地图放在手边——后面所有原语篇都是在「NVSHMEM/CP-engine 这条现实路线」上展开的。
