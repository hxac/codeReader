# 项目定位与愿景：TileScale 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在「不写任何代码」的前提下，建立对 TileScale 的整体认知。读完本讲，你应该能够：

1. 用一句话说清 TileScale 与 TileLang、与 TVM 之间的关系。
2. 复述 HDA（Hierarchical Distributed Architecture，层次化分布式架构）的三类基础资源是什么。
3. 说清楚 README 里宣传的「愿景」和仓库里「真正已经实现」的东西有什么区别。
4. 看懂 TileScale 系统总览图里的五个模块（frontend / compiler / tile-kernel / cost model / backend）各自负责什么。

> 特别提醒：本讲会反复出现「待确认」三个字。这是因为 README 里宣传的部分能力（最典型的是 `T.Scale` 原语）目前还停留在「愿景 / 设计文档」阶段，代码里并没有真正实现。我们会用真实源码把「宣传」和「现实」区分开。

## 2. 前置知识

本讲面向零基础读者，不要求你写过 CUDA 或编译器。但有几个名词先解释清楚，后面会反复用到：

| 名词 | 通俗解释 |
| --- | --- |
| DSL（领域专用语言） | 为某一类问题专门设计的编程语言。TileScale 就是为「深度学习算子」专门设计的 DSL。 |
| Tile（瓦片/分块） | 把一个大矩阵切成一块一块的小矩阵，每块叫一个 tile。GPU 上的高性能计算基本都以 tile 为单位搬运和计算。 |
| GPU 显存层级 | 从快到慢大致是：寄存器（register）→ 共享内存（shared memory / L1）→ 全局显存（global memory / L2/HBM）。越快的越小。 |
| NVLink / InfiniBand | 多 GPU、多节点之间的高速互联网络，相当于 GPU 之间的「高速公路」。 |
| TVM | 一个开源的深度学习编译器框架。TileLang（TileScale 的单机部分）建立在 TVM 之上。 |
| PE（Processing Element） | 「处理单元」的统称，在不同尺度下可以指一个线程、一个 warp、一个 SM，甚至一张 GPU。 |

如果你对 GPU 的 thread / warp / block / grid 还没有概念也不用担心，后面的讲义（Unit 2）会从 `T.Kernel` 开始讲起。本讲只关心「项目是什么」。

## 3. 本讲源码地图

本讲只读两个文件，它们是理解项目定位的核心：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md) | 项目的「门面」，讲清楚 TileScale 是什么、想解决什么问题、HDA 愿景、系统总览。本讲 80% 的内容来自这里。 |
| [docs/get_started/overview.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/overview.md) | TileLang 语言简介，讲三种编程接口（初学者/开发者/专家）和编译流程。 |

此外，本讲还会引用两段「证据代码」，用来证明哪些是已实现、哪些是待确认：

| 文件 | 用来证明 |
| --- | --- |
| `tilelang/language/kernel.py` | `T.Kernel` 的真实签名（证明 README 的 `device=`/`cta_cluster=` 参数不存在） |
| `tilelang/distributed/__init__.py` | 真实的分布式扩展入口（证明 NVSHMEM 路线已实现） |

> 注意：这个项目的 **Python 包名是 `tilelang`**，而不是 `tilescale`。`tilelang` 是单机 tile 编程语言，TileScale 是它在分布式方向的扩展。两者共用同一个代码库。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应「为什么需要 → 用什么抽象 → 怎么编程 → 系统怎么组织」这条理解链。

### 4.1 项目背景：scaling-law 时代的分布式计算需求

#### 4.1.1 概念说明

TileScale 的第一句话就点明了它的定位：

> TileScale is a distributed extension of TileLang.

也就是说，TileScale = TileLang + 分布式能力。README 用一段很长的背景说明了「为什么现在要做这件事」。核心论点有两个：

1. **模型在变大**：大模型（scaling-law 时代）已经在多 GPU、多节点上跑，GPU 之间靠 NVLink / InfiniBand 互联。
2. **芯片也在变「分布」**：下一代加速器（3D IC、近存计算、晶圆级芯片）把原本「一颗芯片内部」也变成了分布式结构。

这两个趋势合在一起，导致现代 AI 计算系统变成了「多层、混合的分布式架构」。传统 GPU 编程（CUDA SIMT 模型）假设「单设备、线程级计算」，已经不够用了——这正是 TileScale 想填补的空白。

#### 4.1.2 核心流程

用一句话概括 TileScale 的「雄心」：

> 把「芯片内」和「芯片间」的计算资源统一抽象成一个虚拟的「mega-device（巨型设备）」，用户只写 tile 级逻辑，编译器自动调度计算、访存、通信以及它们之间的 overlap（重叠）。

这个目标可以拆成三步：

```text
用户写出 tile 级计算/通信逻辑
        │
        ▼
TileScale 编译器：自动调度 计算 + 访存 + 通信 + overlap
        │
        ▼
编译到任意符合 HDA 抽象的硬件架构
```

#### 4.1.3 源码精读

README 开头的定位陈述直接引自源码：

- [README.md:1-5](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L1-L5)：这一段说明 TileScale 是 TileLang 的分布式扩展，把 tile 级编程推广到多 GPU、多节点、甚至分布式芯片架构。

- [README.md:6-8](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L6-L8)：scaling-law 时代的背景，以及「第一个统一芯片内/芯片间资源、虚拟成单一 mega-device 的编程与编译栈」的定位。这一句是整个项目愿景的核心。

同样要注意一个诚实声明，README 末尾承认项目处于早期实验阶段：

- [README.md:239-240](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L239-L240)：「TileScale is in its early experimental stage」——这解释了为什么很多愿景能力尚未落地。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：理解 README 如何把「现实问题」转化为「产品定位」。
2. **操作步骤**：打开 [README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md)，重点读第 6、8、240 行附近。
3. **需要观察的现象**：注意 README 用了「scaling-law era」「next-gen AI accelerators」「mega-device」这些关键词，把硬件趋势和软件设计挂钩。
4. **预期结果**：你能用自己的话说出「传统 GPU 编程模型的局限」和「TileScale 想统一的两个趋势」。
5. 由于这是纯阅读任务，不涉及运行，结果可立即得出。

#### 4.1.5 小练习与答案

- **练习 1**：TileScale 是「TileLang 的扩展」，方向是什么？
  - **答案**：分布式方向（distributed extension），把单机 tile 编程推广到多 GPU、多节点、分布式芯片。
- **练习 2**：README 说现代 AI 计算系统正变成「混合、多层的分布式架构」，主要来自哪两个趋势？
  - **答案**：① 模型规模变大，跑在多 GPU/多节点上；② 下一代芯片内部（3D IC、近存计算、晶圆级）也变成分布式结构。

### 4.2 HDA：compute / memory / network 三类基础资源

#### 4.2.1 概念说明

为了把「多层分布式系统」抽象出来，TileScale 提出了 **HDA（Hierarchical Distributed Architecture，层次化分布式架构）**。HDA 是整个 TileScale 的硬件抽象基石。

HDA 建立在三类**基础资源**之上：

1. **compute（计算单元）**：从最小的 thread（线程）→ warp（线程束，例如 32 线程）→ SM（流式多处理器，能跑一个 thread block）→ GPU → node（节点）→ super-node……层数和名字由硬件定义。
2. **memory（内存）**：分多层，每一层要么被「共享」要么被「分发」。例如 shared memory 是整个 thread block 共享的，而寄存器只能被单个线程访问。
3. **network（网络）**：同一层级的并行单元之间通过网络互联。例如 Hopper GPU 的 SM cluster 内部靠 NoC（片上网络）互联，节点内的多 GPU 靠 NVLink 互联。

一句话：**HDA = 层次化的计算 + 层次化的内存 + 层次化的网络**。

#### 4.2.2 核心流程

HDA 的层次结构可以这样理解（以 GPU 为例）：

```text
thread   ─┐
warp     ─┤   ← 不同 scale（尺度），对应不同大小的计算能力
SM/block ─┤
GPU      ─┤
node     ─┘

每一层都有对应的 memory（可共享/可分发），
同层之间都有 network 互联。
```

关键性质是：**某一尺度上的计算单元，可以访问自己尺度内所有层的内存**。比如一个 SM 级任务既能访问自己的寄存器，也能访问共享内存。

如果我们把第 \(i\) 层的计算单元数记为 \(U_i\)、该层内存容量记为 \(M_i\)、该层网络带宽记为 \(B_i\)，那么 HDA 描述的就是一个三元组序列 \((U_i, M_i, B_i)\) 的层次组合，从「又少又快又小」（如寄存器）到「又多又慢又大」（如跨节点内存）。TileScale 的目标就是让用户在任意关心的某一层（scale）上写程序。

#### 4.2.3 源码精读

HDA 这一节几乎完全出自 README：

- [README.md:12-16](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L12-L16)：HDA 的定义，以及「built upon three fundamental resources: compute units, memory, and network」这句话——这就是本模块的核心。
- [README.md:17-25](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L17-L25)：分别解释三类资源——thread→warp→SM 的层次、shared/register 内存的共享与分发、NoC/NVLink 的网络互联。

> 小提示：README 里有一些被 `<!-- -->` 注释掉的段落（比如 DSMEM、L0/L1 内存的详细映射），属于「写了但暂时没展示」的设计草稿，本讲不展开，但说明 HDA 抽象仍在演进中。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把「三类基础资源」对应到你熟悉的 GPU 概念上。
2. **操作步骤**：阅读 [README.md:17-25](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L17-L25)，画一张三列表格。
3. **需要观察的现象**：把 compute / memory / network 三列，分别填入「thread/warp/SM」「register/shared memory/global memory」「NoC/NVLink」。
4. **预期结果**：你得到一张能一眼看清 HDA 三类资源的表，加深对「层次化」的直观印象。
5. 纯阅读任务，结果可立即得出。

#### 4.2.5 小练习与答案

- **练习 1**：HDA 建立在哪三类基础资源之上？
  - **答案**：compute（计算单元）、memory（内存）、network（网络）。
- **练习 2**：举一个「内存被分发而非共享」的例子。
  - **答案**：寄存器（register）只能被单个线程访问，是分发到单个线程的；而 shared memory 是整个 block 共享的。
- **练习 3**：README 提到 Hopper GPU 的 SM cluster 内部靠什么互联？
  - **答案**：NoC（Network-on-Chip，片上网络），可支持 peer SM memory access（跨 SM 访存，即 DSMEM 能力）。

### 4.3 tile-based 编程接口：compute / memory / communicate

#### 4.3.1 概念说明

有了 HDA 的硬件抽象，TileScale 对用户暴露的是一套**层次化的 tile 级编程接口**。tile（瓦片）是 TileScale 中计算的基本单位——一小块数据，可以被一个 warp、一个 block 甚至一张 GPU 拥有和操作。

接口分三类，和 HDA 三类资源一一对应：

| 接口类别 | 作用 | 例子 |
| --- | --- | --- |
| **Compute（计算）** | 输入若干 tile，输出若干 tile | `T.gemm`、reduce 等 |
| **Memory（内存）** | 在同一内存层搬运 tile，或跨内存层搬运 | `T.copy`、`T.alloc_shared` |
| **Communicate（通信）** | 通过网络在不同计算单元间传 tile，并管理同步 | put/get、AllReduce、All2All 等 |

设计哲学是：**同一个原语可以用在不同 scale 上**，高层 scale 的原语可以由低层 scale 的原语组合实现。比如一个 block 级的算子，可以由一组 warp 级或 thread 级原语实现。

> ⚠️ **重要区分（本讲最关键的一点）**：README 在「Parallel task scheduling」一节宣传了一个 `T.Scale` 原语，用来声明「当前计算跑在哪个硬件 scale 上」。但是——**`T.Scale` 在当前代码里并不存在**。这是 TileScale 最典型的「愿景 vs 现实」差异，我们用源码来证明。

#### 4.3.2 核心流程

README 给出的愿景写法是这样的（注意这是**目标语法，尚未实现**）：

```python
# 愿景语法（README:65-69），当前代码不支持 T.Scale
with T.Scale("warp"):
    T.gemm(A, B, C)
```

而当前仓库**真正可用**的单机 tile 编程，用的是 `T.Kernel` 启动 + `T.copy` / `T.gemm` 这类原语，并没有 `T.Scale`。分布式部分则走的是另一条**已实现**的路线——基于 NVSHMEM 的多设备原语（见 Unit 6）。

简单说：

```text
README 宣传的愿景接口          当前已实现的真实接口
─────────────────────         ────────────────────────
T.Scale("warp"/"device"…)      （不存在）
T.Kernel(device=, cta_cluster=) T.Kernel(*blocks, threads=, is_cpu=)
T.allreduce / T.allgather      NVSHMEM put/get + 集合通信原语
```

#### 4.3.3 源码精读

先看 README 里宣传的愿景接口（注意 `T.Scale`、`device=`、`cta_cluster=`）：

- [README.md:61-69](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L61-L69)：宣传 `T.Scale` 原语，声明 `with T.Scale("warp"):`。
- [README.md:71-81](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L71-L81)：宣传 cluster 级 GEMM，用到 `T.Kernel(cta_cluster=(2), ...)` 和 `T.Scale("cta_cluster")`。
- [README.md:102-145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L102-L145)：一个完整的 4-GPU GEMM 示例，大量使用 `T.Scale("device")` / `T.Scale("warpgroup")` / `T.allreduce`。

再看**真实代码**，证明这些参数并不存在：

- [tilelang/language/kernel.py:228-233](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L228-L233)：真实的 `T.Kernel` 签名是 `Kernel(*blocks, threads=None, is_cpu=False, prelude=None)`，**没有** `device=`，**没有** `cta_cluster=`。在全仓库里也搜不到 `T.Scale` 的定义。

最后看**已实现**的分布式入口，证明 NVSHMEM 路线是真实的：

- [tilelang/distributed/__init__.py:4](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/__init__.py#L4)：`from tilescale_ext import _create_tensor, _create_ipc_handle, _sync_ipc_handles`——这是真实存在的 C++ 扩展（`tilescale_ext`），提供 IPC 张量与跨进程显存共享，是分布式能力的运行时基石。

这一组对比就是本讲的结论：**README 用 `T.Scale` 描绘了 HDA 编程的终态愿景，而当前版本真正落地的是「单机 `T.Kernel` + NVSHMEM 多设备原语」这条务实路线。**

#### 4.3.4 代码实践（源码阅读型，含验证）

1. **实践目标**：亲手验证「`T.Scale` 不存在」这个结论。
2. **操作步骤**：
   - 在仓库根目录执行（只读，不修改任何文件）：
     ```bash
     # 1) 看 T.Kernel 的真实签名
     sed -n '228,233p' tilelang/language/kernel.py
     # 2) 全仓库搜索 T.Scale 的定义（预期搜不到 def Scale）
     grep -rn "def Scale" tilelang/ src/ || echo "未找到 T.Scale 定义"
     # 3) 确认分布式入口真实存在
     sed -n '1,5p' tilelang/distributed/__init__.py
     ```
3. **需要观察的现象**：
   - `T.Kernel` 签名里没有 `device`、`cta_cluster` 参数；
   - `grep "def Scale"` 没有结果；
   - `distributed/__init__.py` 能 import `tilescale_ext`。
4. **预期结果**：三个现象合起来，证实「愿景接口未实现 / NVSHMEM 路线已实现」。
5. 这是只读命令，结果可立即得出；若你的环境未编译 `tilescale_ext`，第 3 步的 `import` 可能报缺失，但不影响前两步的结论。

#### 4.3.5 小练习与答案

- **练习 1**：tile 接口分哪三类？分别对应 HDA 的哪类资源？
  - **答案**：Compute / Memory / Communicate，分别对应 compute（计算）/ memory（内存）/ network（网络）。
- **练习 2**：README 宣传的 `T.Scale` 原语当前是否可用？如何用源码证明？
  - **答案**：不可用（待确认）。证明方式：`T.Kernel` 的真实签名（`kernel.py:228`）没有 `device`/`cta_cluster` 参数，且全仓库搜不到 `T.Scale` 的定义。
- **练习 3**：当前版本真正落地的分布式路线基于什么技术？
  - **答案**：NVSHMEM 多设备原语（配合 `tilescale_ext` 的 IPC 张量与 `pynvshmem` 运行时）。

### 4.4 系统总览：frontend / compiler / tile-kernel / cost model / backend

#### 4.4.1 概念说明

知道「写什么、抽象成什么」之后，最后一个问题是「系统怎么把用户程序变成可执行代码」。TileScale 把自己分成五个模块：

| 模块 | 职责 |
| --- | --- |
| **frontend（前端）** | 提供 tile 原语、Python 绑定和相关编程语法 |
| **compiler（编译器）** | 把前端程序 lower 成中间表示（IR），跑优化 pass，把高层 tile 原语降级成低层原语 |
| **tile-kernel（tile 内核库）** | 一个库，包含所有 tile 原语的实现 |
| **cost model（代价模型）** | 建立性能数据库，为具体优化方案提供轻量性能反馈 |
| **backend（后端）** | 按 HDA 抽象定义可配置的硬件架构，把程序编译到任意用户自定义架构 |

其中最关键的思想是「**可配置后端**」：和「只为少数几种硬件」的编译器不同，TileScale 旨在能编译到**任何符合 HDA 抽象的架构**。

#### 4.4.2 核心流程

整个编译流水线可以概括为：

```text
用户 tile 程序 (frontend)
        │
        ▼
IR (compiler 多个 pass)          ← cost model 给优化方案打分
        │
        ▼
低层源码 (C / CUDA / HIP / …)    ← tile-kernel 提供原语实现
        │
        ▼
硬件可执行 (backend / 可配置架构)
```

TileLang 文档还把这条流水线细化成三个层次的用户接口（初学者 / 开发者 / 专家），以及「Tile Program → IRModule → 源码生成 → 可执行」的具体阶段。本讲只做总览，细节留给 Unit 3（编译流水线）。

注意 cost model 与 compiler 是双向的：compiler 在做优化选择时，会向 cost model 查询某个方案的性能反馈，从而挑出更优的调度。

#### 4.4.3 源码精读

- [README.md:45-51](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L45-L51)：系统总览的原文，逐一定义 frontend / compiler / tile-kernel / cost model / backend 五个模块，并强调「TileScale can compile a program to any user-defined architecture」。
- [docs/get_started/overview.md:32-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/overview.md#L32-L50)：编译流程的阶段化描述——Tile Program → （Tile Library / Thread Primitives）→ IRModule → 源码生成（C/CUDA/HIP/LLVM）→ 硬件可执行。
- [docs/get_started/overview.md:52-84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/overview.md#L52-L84)：tile 编程模型的具体例子，介绍 `T.alloc_shared`（共享内存）、`T.alloc_fragment`（寄存器/fragment）、`T.copy`（跨内存层搬运）——这些才是**当前真实可用**的单机原语，与愿景里的 `T.Scale` 形成对照。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：把「五模块系统图」和「编译阶段」对上号。
2. **操作步骤**：对照 [README.md:45-51](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L45-L51) 和 [docs/get_started/overview.md:32-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/overview.md#L32-L50)。
3. **需要观察的现象**：overview.md 的「Tile Program → IRModule → 源码生成 → 可执行」四阶段，分别落在 README 五模块里的哪几个（compiler / backend）。
4. **预期结果**：你能画出一条从 frontend 到 backend 的箭头链，并标出 IR 和 codegen 发生的位置。
5. 纯阅读任务，结果可立即得出。

#### 4.4.5 小练习与答案

- **练习 1**：TileScale 系统由哪五个模块组成？
  - **答案**：frontend、compiler、tile-kernel、cost model、backend。
- **练习 2**：cost model 和 compiler 是什么关系？
  - **答案**：cost model 为 compiler 的优化方案提供轻量性能反馈，compiler 据此挑选更优调度；两者是双向配合关系。
- **练习 3**：TileScale 后端和传统编译器相比最大的不同是什么？
  - **答案**：后端按 HDA 抽象**可配置**，能把程序编译到任意用户自定义架构，而不只是固定几种硬件。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个阅读 + 整理任务（无需运行任何代码）：

**任务：写一份「TileScale 项目定位 + 问题清单」**

1. **实践目标**：检验你是否真正理解了 TileScale 的定位，并具备「区分宣传与现实」的判断力。
2. **操作步骤**：
   1. 重读 [README.md:1-10](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L1-L10) 和 [README.md:239-240](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/README.md#L239-L240)。
   2. 用自己的话写一段 **200 字以内**的项目定位说明，必须包含：① TileScale 是什么（与 TileLang/TVM 的关系）；② 它抽象了什么（HDA 三类资源）；③ 它的五个系统模块。
   3. 列出 **3 个 TileScale 想解决的问题**，并逐一标注是「README 已实现」还是「待确认」。建议从下表挑选并核实：

   | 候选问题 | 参考判定 | 核实依据 |
   | --- | --- | --- |
   | 单 GPU 上的 tile 级高性能算子编程 | **已实现**（单机路线） | `T.Kernel`/`T.copy`/`T.gemm` 真实存在（[kernel.py:228](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L228)） |
   | 多 GPU/多节点的 tile 级通信 | **已实现**（NVSHMEM 路线） | `tilescale_ext` 真实存在（[distributed/__init__.py:4](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/distributed/__init__.py#L4)） |
   | 用 `T.Scale` 在任意硬件 scale 上编程 | **待确认**（愿景） | 全仓库搜不到 `T.Scale` 定义 |
   | `T.Kernel(device=, cta_cluster=)` 多设备/cluster 启动 | **待确认**（愿景） | `T.Kernel` 真实签名无此参数 |

3. **需要观察的现象**：在写「定位说明」时，你会发现自己能否准确说出「单机已实现、分布式已实现、HDA 统一愿景未实现」这三层。
4. **预期结果**：一段 200 字定位 + 一张 3 行的问题清单（带「已实现/待确认」标注）。如果你把 `T.Scale` 标成了「已实现」，说明需要回到 4.3 节再读一遍。
5. 这是纯阅读整理任务，结果可立即得出。

## 6. 本讲小结

- **TileScale = TileLang + 分布式**，是把 tile 级编程推广到多 GPU/多节点/分布式芯片的 DSL 与编译栈，Python 包名为 `tilelang`。
- 它回应的是 scaling-law 时代的两个趋势：模型跨多机、芯片内部也变分布式，目标是把这一切抽象成统一的「mega-device」。
- **HDA（层次化分布式架构）**建立在三类基础资源上：**compute（计算）/ memory（内存）/ network（网络）**，层层嵌套。
- 编程接口分三类——**compute / memory / communicate**，对应 HDA 三类资源；同一个原语可用于不同 scale。
- 系统由 **frontend / compiler / tile-kernel / cost model / backend** 五模块组成，后端可配置、可编译到任意 HDA 架构。
- **最重要的判断**：README 宣传的 `T.Scale`、`T.Kernel(device=/cta_cluster=)` 属**愿景/待确认**；当前真正落地的是「单机 `T.Kernel` + NVSHMEM 多设备原语」这条务实路线。

## 7. 下一步学习建议

本讲只建立了「地图」，还没有让你亲手运行任何东西。建议按下面的顺序继续：

1. **下一讲 [u1-l2 安装与环境搭建](u1-l2-installation.md)**：先把 `tilelang` 装好，能成功 `import`，为后续跑 kernel 做准备。
2. **再下一讲 [u1-l3 quickstart 详解](u1-l3-quickstart.md)**：亲手跑通第一个单 GPU matmul+relu kernel，验证「单机 tile 编程已实现」这条结论。
3. 想提前感受「已实现的分布式路线」，可先扫一眼 [examples/distributed/README.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/README.md)，但真正读懂要等到 Unit 6。
4. 想深入了解编译流水线，Unit 3 会带你逐 pass 走完 `frontend → IR → codegen` 这条链。
