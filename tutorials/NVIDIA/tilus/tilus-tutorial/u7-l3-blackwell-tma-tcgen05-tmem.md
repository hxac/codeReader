# Blackwell：TMA、tcgen05 与 TMEM

## 1. 本讲目标

本讲聚焦 NVIDIA Blackwell（sm_100a）架构引入的三项关键硬件能力，以及 Tilus 如何用「指令组」把它们暴露给内核开发者。读完本讲你应该能够：

- 说清 **TMA（Tensor Memory Accelerator）** 的异步批量搬运语义，以及它如何用 `mbarrier` 追踪完成。
- 理解 **TMEM（Tensor Memory，张量内存）** 这一全新片上存储空间，以及 **tcgen05（第五代张量核）指令组** 如何管理它的分配、搬运、MMA 与异步完成。
- 读懂一份完整的 Blackwell matmul：从 TMA 加载、tcgen05 MMA 在 TMEM 中累加、到 TMA epilogue 回写，并解释 `store_shared` 与 `tma.shared_to_global` 之间那条 **proxy fence** 为何必不可少。
- 了解 **cluster（线程块簇）** 如何让多个 CTA 共享 TMA 多播与协作 MMA。

本讲建立在 u7-l2（Hopper：wgmma 与 cp_async）之上。Hopper 的 `wgmma`/`cp_async`/`mbarrier` 三件套在 Blackwell 上被升级为 `tcgen05`/`TMA`/`mbarrier` 三件套，异步思想一脉相承，但搬运引擎更强、累加器搬到了专用内存。

## 2. 前置知识

在进入本讲前，请确保你已经掌握以下概念（来自前面讲义）：

- **tile-level 编程模型**：内核以「一个线程块整体做什么」为视角书写，`self.sync()` 同步线程块，`with self.single_warp()` 把代码收窄到 32 线程执行（u1-l3、u2-l3）。
- **四种张量与内存空间**：寄存器（`RegisterTensor`）、共享内存（`SharedTensor`）、显存（`GlobalTensor`），以及本讲主角——Blackwell 专用的 **TMEM（`TMemoryTensor`）**（u4-l1）。
- **mbarrier 异步同步模型**：用「待到达计数（pending arrivals）+ tx-count（待完成字节数）」双归零、配合 **相位（phase）** 在 0/1 之间翻转来判定完成（u7-l2）。这是本讲反复出现的同步原语。
- **cp_async / wgmma 异步范式**：搬运或计算「发起后立即返回，完成靠分组等待」，本讲的 TMA 与 tcgen05 沿用同样的「发起—等待」骨架（u7-l2）。

三个本讲专用的直觉性比喻：

1. **TMA = 一个独立的搬运引擎**。普通 `load_global` 要占着 SM 的计算资源逐线程读显存；TMA 是一块专用硬件，你给它「源地址、目的地址、形状、偏移」，它自己在后台搬，不占 SM 算力，搬完自动给 `mbarrier` 的 tx-count 减字节数。
2. **TMEM = 张量核的专属草稿纸**。寄存器是「每个线程私有」的草稿纸，做 MMA 时累加器要在线程间倒来倒去；TMEM 是张量核自带的、跨 warp 共享的大块累加器存储，结果直接留在原地，省掉了寄存器溢出（register spilling）的开销。
3. **proxy fence = 内存代理之间的「刷新缓存」**。GPU 有多条访问共享内存的「路径」（generic 代理、async 代理）。`store_shared` 走 generic 代理写，TMA 走 async 代理读，两条路径各自有自己的可见性节奏；不加 fence，TMA 可能读到旧数据。

## 3. 本讲源码地图

本讲涉及的源码文件分为「示例」与「指令定义」两组：

| 文件 | 作用 |
| --- | --- |
| `examples/blackwell_matmul/matmul_v0.py` | v0：用 `copy_async`（非 TMA）加载 + tcgen05 MMA + TMEM 累加器的最简 Blackwell matmul |
| `examples/blackwell_matmul/matmul_v1.py` | v1：升级为 TMA 加载 + TMA epilogue 回写，引入 `slice` 与 proxy fence |
| `python/tilus/lang/instructions/tma.py` | `TmaInstructionGroup`：`global_to_shared`/`shared_to_global`/`commit_group`/`wait_group` |
| `python/tilus/lang/instructions/tcgen05.py` | `Tcgen05InstructionGroup`：TMEM 的分配/视图/搬运/MMA/同步全套指令 |
| `python/tilus/lang/instructions/mbarrier.py` | `BarrierInstructionGroup`：`alloc`/`arrive_and_expect_tx`/`wait` 等 |
| `python/tilus/lang/instructions/fence.py` | `FenceInstructionGroup`：`proxy_async` 等 proxy fence |
| `python/tilus/lang/instructions/cluster.py` | `BlockClusterInstructionGroup`：cluster 同步与跨 CTA 寻址 |
| `python/tilus/ir/layout/tmem_layout.py` | `TMemoryLayout`：TMEM 的 lane/column 物理坐标建模 |

这些指令组都通过 `InstructionInterface` 组合挂到 `self.*` 上（见 [python/tilus/lang/instructions/__init__.py:30-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/__init__.py#L30-L38)）：`self.tma`、`self.tcgen05`、`self.mbarrier`、`self.fence`、`self.cluster`。与 u2-l2 讲过的分层模型一致——通用指令做可移植逻辑，硬件指令组做显式性能，本讲的 `tma`/`tcgen05` 正是后者。

## 4. 核心概念与源码讲解

### 4.1 TMA：张量内存加速器的异步批量搬运

#### 4.1.1 概念说明

**TMA（Tensor Memory Accelerator）** 是 Hopper（sm_90）引入、Blackwell 继承的一块专用硬件引擎，用于在 **全局内存（DRAM）与共享内存（SRAM）之间异步搬运多维 tile**。它相比上一讲的 `cp_async` 有两点质变：

1. **多维原生**：你直接告诉它源/目的张量的形状、每个维度的偏移，TMA 内部完成多维索引计算与边界处理，不必逐线程算地址。
2. **不占 SM 算力**：发起 TMA 的线程立即返回，搬运在后台由 TMA 引擎执行，SM 可以同时做计算。

TMA 的所有搬运都是 **异步** 的，完成必须靠两类同步机制之一：

- **mbarrier**（用于 `global_to_shared`）：发起搬运时自动给 `mbarrier` 的 tx-count 加上本次字节数，TMA 搬完后硬件自动减回去；消费者调用 `mbarrier.wait` 阻塞到 tx-count 与到达计数双归零。
- **commit_group / wait_group**（用于 `shared_to_global`）：把若干次回写打包成组，`wait_group(n=0)` 等待全部完成。

#### 4.1.2 核心流程

一次典型的 TMA 加载（global→shared）流程：

1. 在共享内存分配目的 tile（`shared_tensor`）。
2. 分配一个 `mbarrier`，用 `mbarrier.arrive_and_expect_tx` 声明「期待收到多少字节」。
3. 发起 `tma.global_to_shared`，把源张量的一块搬进共享内存，TMA 完成后自动给该 `mbarrier` 的 tx-count 减字节数。
4. 调用 `mbarrier.wait`，等到 tx-count 归零，表示数据已就绪。

其时序可示意如下（phase 在每次 wait 后翻转）：

```
  发起方(单warp)              TMA引擎(后台)            消费方(全部线程)
  ─────────────              ──────────              ────────────
  arrive_and_expect_tx(bytes) │
  global_to_shared(A)  ───────► 搬运A, 完成后 tx-=sizeA
  global_to_shared(B)  ───────► 搬运B, 完成后 tx-=sizeB
                            (tx 归零, phase 翻转)
  mbarrier.wait(phase) ◄──────────────────────── 消费方继续读共享内存
```

#### 4.1.3 源码精读

`TmaInstructionGroup` 的类文档精炼地总结了 TMA 的异步本质与 mbarrier 追踪模型，见 [python/tilus/lang/instructions/tma.py:23-43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L23-L43)：发起搬运后「mbarrier 的 tx-count 自动增加，TMA 完成后自动减少，消费者用 `mbarrier.wait()` 阻塞」。

`global_to_shared` 是 TMA 加载的核心入口，签名与说明见 [python/tilus/lang/instructions/tma.py:45-119](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L45-L119)。几个关键点：

- `offsets` 指定在 **全局张量** 中从哪里取 tile，`dims` 指定全局张量的哪些维度映射到共享张量的各维（默认按顺序）。
- `mbarrier` 是完成追踪句柄；本指令会自动把搬运字节数累加进该 barrier 的 tx-count。
- 它还支持 `multicast_mask`（多播：把同一块全局数据分发给 cluster 内多个 CTA 的共享内存）和 `cta_group=2`（双 CTA 协作），为 4.4 节的 cluster 埋下伏笔。
- 硬件要求 sm_90+，线程组必须是 **warp 对齐**（32 的倍数）。

`shared_to_global` 是反向回写，签名见 [python/tilus/lang/instructions/tma.py:121-167](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L121-L167)。注意它与 `global_to_shared` 的两处关键差异：

1. **不用 mbarrier**，改用 `commit_group` / `wait_group` 同步。
2. **必须配 proxy fence**：如果共享内存里的数据是用 `store_shared`（generic 代理）写的，那么在 `shared_to_global` 之前必须插入 `fence.proxy_async`，否则 TMA 引擎（async 代理）可能读到旧数据。这条重要提示见 [python/tilus/lang/instructions/tma.py:138-142](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L138-L142)，是 4.4 节的核心。

`commit_group` / `wait_group` 用于给 `shared_to_global` 分组与等待，见 [python/tilus/lang/instructions/tma.py:169-210](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L169-L210)。`wait_group(n=0, read=False)` 表示等到没有挂起的组为止；当源共享内存要被复用、且后续没有指令读目的全局内存时，可用 `read=True` 只等「读源」完成。

在 v1 示例中，TMA 加载的真实用法在主循环里：

```python
with self.single_thread():
    self.mbarrier.arrive_and_expect_tx(
        tma_barrier, transaction_bytes=s_a.nbytes + s_b.nbytes
    )
self.tma.global_to_shared(src=g_a, dst=s_a, offsets=[offset_m, offset_k], mbarrier=tma_barrier)
self.tma.global_to_shared(src=g_b, dst=s_b, offsets=[offset_n, offset_k], mbarrier=tma_barrier)
self.mbarrier.wait(tma_barrier, phase=phase)
```

完整上下文见 [examples/blackwell_matmul/matmul_v1.py:58-78](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v1.py#L58-L78)。注意 `arrive_and_expect_tx` 用 `single_thread()` 包起来（只有 1 个到达，但把全部字节数 `s_a.nbytes + s_b.nbytes` 计入 tx-count），而 TMA 与 wait 由 `single_warp` 发起。

#### 4.1.4 代码实践

实践目标：用源码阅读理解 TMA 的「发起—等待」骨架。

操作步骤：

1. 打开 [examples/blackwell_matmul/matmul_v1.py:50-88](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v1.py#L50-L88)。
2. 找到第 51 行的两个 barrier：`tma_barrier`（追踪 TMA 完成）与 `mma_barrier`（追踪 MMA 完成）。
3. 跟踪一次循环迭代（57-87 行）：`arrive_and_expect_tx` → 两次 `global_to_shared` → `mbarrier.wait(tma_barrier)` → `tcgen05.mma` → `tcgen05.commit(mma_barrier)` → `mbarrier.wait(mma_barrier)` → `phase ^= 1`。

需要观察的现象：`tma_barrier` 的 tx-count 在 `arrive_and_expect_tx` 时被设为 `s_a.nbytes + s_b.nbytes`，两次 TMA 各搬完一块后硬件分别减回对应字节数；当 tx-count 归零且到达计数归零时，`wait` 才放行。这是 TMA 异步搬运能被可靠等待的根本。

预期结果：你能画出「TMA 字节数如何随搬运进度递减」的示意，并解释为什么 `arrive_and_expect_tx` 必须先于 `global_to_shared`（先声明期待值，否则 tx-count 可能在声明前就被 TMA 减成负数）。本实践为纯源码阅读，无需 GPU，故运行结果为「待本地验证」（确认需要 Blackwell sm_100a 硬件才能实际跑通）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `global_to_shared` 用 `mbarrier` 而 `shared_to_global` 用 `commit_group/wait_group`？

参考答案：`global_to_shared` 把数据搬 **进** 共享内存，后续 MMA 要立刻消费，需要精确知道「字节已到齐」；mbarrier 的 tx-count 机制天然适合「按字节数追踪异步搬运完成」。`shared_to_global` 是把结果 **写回** 显存，后续通常不再读同一块显存，用更轻量的 commit/wait 分组即可，无需逐字节追踪。

**练习 2**：如果把 `arrive_and_expect_tx` 删掉，只保留 `global_to_shared` 和 `wait`，会发生什么？

参考答案：`mbarrier` 的 tx-count 永远不会被增加（初始为 0），但它已被分配（到达计数已设）。问题在于 TMA 完成时仍会试图「减少 tx-count」，这会破坏 barrier 状态机；即使不崩，`wait` 也可能因为 tx-count 与字节数不匹配而永远等不到正确的归零时机。`arrive_and_expect_tx` 是让 barrier 提前知道「会有多少字节」的必要步骤。

### 4.2 TMEM：张量内存与 TMemoryLayout

#### 4.2.1 概念说明

Blackwell 在 SM 内部引入了一块全新的片上存储——**TMEM（Tensor Memory，张量内存）**，它是张量核的专属累加器空间。`TMemoryTensor` 的类文档把它描述为「Blackwell（SM 10.0+）独有的片上内存，为张量核私有，组织成 lane（行）与 column（列）的 2D 结构，每格 32 bit」，见 [python/tilus/ir/tensor.py:659-679](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L659-L679)。

理解 TMEM 要抓住它与另外三种张量的对比：

| 张量类型 | 物理位置 | 谁能访问 | 典型角色 |
| --- | --- | --- | --- |
| `RegisterTensor` | 寄存器（线程私有） | 单个线程 | 元素运算、累加器（Ampere/Hopper） |
| `SharedTensor` | 共享内存 SRAM | 整个 CTA | tile 中转、ldmatrix/wgmma 输入 |
| `GlobalTensor` | 显存 DRAM | 全部线程块 | 输入/输出 |
| `TMemoryTensor` | 张量内存 TMEM | 张量核（跨 warp 共享） | Blackwell MMA 的累加器 |

关键区别：**在 Ampere/Hopper 上，MMA 的累加器放在寄存器（每个 warp 各持一份），K 维循环里反复读写寄存器**；Blackwell 把累加器搬到 TMEM，结果是「累加器常驻张量核专属存储，跨循环迭代持久存在，省掉了寄存器倒腾与溢出」。这正是 `tcgen05.mma` 的输出 `d` 必须是 `TMemoryTensor` 的原因。

#### 4.2.2 核心流程

TMEM 的物理结构是 **128 lane × 512 column，每格 32 bit**（见 tcgen05 文档图注 [docs/source/python-api/instruction-groups/tcgen05.rst:11-15](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/python-api/instruction-groups/tcgen05.rst#L11-L15)）。`TMemoryLayout` 用三个字段把逻辑张量映射到这套 lane/column 原生坐标：

- `shape[0]` 是 **lane 轴（行）**，必须是 32、64 或 128 之一；
- 其余维度是 **column-strided（按列步长排布）**；
- `column_strides[0]`（行维的列步长）强制为 0（lane 之间不通过列步长区分）。

每个 32-bit 格子的容量与张量 dtype 共同决定一个 lane 能存多少元素。若 dtype 位宽为 \( b \)，一个 32-bit 格子能打包 \( 32/b \) 个元素，于是整个 TMEM 的总容量（按元素计）约为

\[ \text{capacity} \approx 128 \times 512 \times \frac{32}{b} \quad \text{个元素（理想打包下）} \]

这也是 `tcgen05.alloc` 为何要求 `128 % dtype.nbits == 0`（dtype 必须是 1/2/4/8/16/32/64/128 位之一），见 [python/tilus/lang/instructions/tcgen05.py:84-85](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L84-L85)。

#### 4.2.3 源码精读

`TMemoryLayout` 是一个不可变 frozen dataclass（`eq=False`，沿用 IR 的身份相等约定），完整定义见 [python/tilus/ir/layout/tmem_layout.py:23-48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/tmem_layout.py#L23-L48)。`create` 工厂方法做了三项合法性校验，恰好对应 TMEM 的硬件约束：

1. `shape[0]` 必须是 32/64/128（lane 数取值受限）。
2. `column_strides[0]` 必须为 0（行维不用列步长）。
3. 至少 2 维，且 `shape` 与 `column_strides` 长度一致。

与 u4 讲过的 Register/Shared/GlobalLayout 不同，`TMemoryLayout` 把逻辑索引映射到的是 **lane + column 的原生硬件坐标**，而非字节地址——因为 TMEM 不是按字节寻址的普通内存，而是张量核直连的寄存器堆式结构。这与 u4-l3 讲过的「不同 Layout 映射目标不同」一脉相承。

`TMemoryTensor` 同样支持 `optional_layout` 延迟绑定（u4-l1 讲过的三态协议），创建时可置 `None`，由布局推理 pass 自动补全，见 [python/tilus/ir/tensor.py:673-679](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L673-L679)。

在 v0 示例中，TMEM 累加器的分配只有一行：

```python
t_acc = self.tcgen05.alloc(dtype=float32, shape=[self.block_m, self.block_n])
```

见 [examples/blackwell_matmul/matmul_v0.py:49-50](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v0.py#L49-L50)。注意 `shape[0]=block_m`（如 128）恰好命中 32/64/128 之一，这是 `alloc` 校验通过的前提。

#### 4.2.4 代码实践

实践目标：理解 TMEM 的形状约束与生命周期。

操作步骤：

1. 阅读 [python/tilus/lang/instructions/tcgen05.py:48-87](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L48-L87) 的 `alloc` 与 [python/tilus/lang/instructions/tcgen05.py:89-105](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L89-L105) 的 `dealloc`。
2. 对照 [python/tilus/ir/layout/tmem_layout.py:29-48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/tmem_layout.py#L29-L48) 的三处校验。
3. 在 v0 示例中确认：`t_acc` 在循环前 `alloc`，在循环内被 MMA 反复原地累加，最后在第 91 行被 `dealloc`（[examples/blackwell_matmul/matmul_v0.py:89-91](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v0.py#L89-L91)）。

需要观察的现象：TMEM 必须显式 `alloc`/`dealloc`（这点像 `SharedTensor`，区别于随用随建的 `RegisterTensor`），且 **内核退出前所有 TMEM 必须释放**。v0 在 `dealloc` 前还调了一次 `self.sync()`，确保 MMA 已完成、TMEM 不再被读。

预期结果：你能解释「为何 `block_m` 必须是 32/64/128 之一」——因为它是 TMEM 的 lane 数，受硬件结构约束。运行验证「待本地验证」（需 Blackwell 硬件）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `column_strides[0]` 必须为 0？

参考答案：`shape[0]` 是 lane 轴，不同 lane 本身就由不同的硬件 lane 区分（lane 0、lane 1……），不需要再用列步长来区分行。列步长只用于「其余维度如何沿 column 方向排布」，所以行维的列步长恒为 0。

**练习 2**：TMEM 累加器相比寄存器累加器（如 Ampere 的 MMA）解决了什么问题？

参考答案：寄存器累加器在 K 维循环里要被反复读写，且大 tile 会导致寄存器不够用、被迫溢出到栈内存（register spilling）。TMEM 是张量核专属的大容量片上存储，累加器常驻其中、跨迭代持久，既省了寄存器倒腾的指令开销，又避免了溢出。

### 4.3 tcgen05 指令组：分配、搬运、MMA 与异步完成

#### 4.3.1 概念说明

**tcgen05（Tensor Core Generation 05）** 是 Blackwell 的第五代张量核指令组，它围绕 TMEM 提供一整套生命周期管理。`Tcgen05InstructionGroup` 的类文档把它的能力归纳为四类（见 [python/tilus/lang/instructions/tcgen05.py:25-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L25-L46)）：

- **分配**：`alloc` / `dealloc` 管理 TMEM 容量，`relinquish_alloc_permit` 在双 CTA 协作时让出分配权。
- **视图**：`slice`（取子区域）、`view`（换 dtype/shape 重解释），都是 **只改元数据、不搬数据**。
- **搬运**：`load`（TMEM→寄存器）、`store`（寄存器→TMEM）、`copy`（共享内存→TMEM），全部 **异步**，要配 `wait_load` / `wait_store` / `commit`。
- **计算**：`mma` 在 TMEM 中做矩阵乘累加，操作数 A 可来自共享内存或 TMEM。

与 Hopper `wgmma` 的关键区别：tcgen05 的 **累加器固定在 TMEM**（不在寄存器），且 `mma`/`commit` 必须 **由单个 warp 发起**（`single_warp`）。

#### 4.3.2 核心流程

一次 tcgen05 MMA 的完整异步协议（v0/v1 主循环内核）：

```
with self.single_warp():                  # MMA 由单 warp 发起
    tcgen05.mma(s_a, s_b.T, t_acc, enable_input_d=是否累加)
    tcgen05.commit(mbarrier=mma_barrier)  # 把挂起的 MMA 打包, 完成时通知 mbarrier
    mbarrier.wait(mma_barrier, phase)     # 等到 MMA 真正写完 TMEM
self.sync()                               # CTA 级同步, 让全 warp 看到一致状态
```

这里有三层同步，与 u7-l2 强调的「三层同步不可互替」一脉相承：

- **`tcgen05.commit` + `mbarrier.wait`**：追踪 **异步事务（MMA 写 TMEM）的完成**。
- **`self.sync()`**：同步 **线程的执行进度**（线程组级 `__syncthreads`）。

`enable_input_d` 控制语义：第一轮迭代 `offset_k == 0` 时为 `False`，算 `d = a@b`（覆盖 TMEM 初值）；后续为 `True`，算 `d = a@b + d`（原地累加）。这等价于把「初始化累加器」与「累加」合并在同一段代码里，见 v0 的 [examples/blackwell_matmul/matmul_v0.py:69-78](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v0.py#L69-L78)。

MMA 完成后，结果还在 TMEM 里，要取出来才能回写显存，于是有 epilogue：`tcgen05.load(t_acc)` 把 TMEM 搬进寄存器，但 `load` 也是异步的，必须 `tcgen05.wait_load()` 之后才能用寄存器数据，见 [python/tilus/lang/instructions/tcgen05.py:175-198](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L175-L198) 与 [python/tilus/lang/instructions/tcgen05.py:224-235](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L224-L235)。

#### 4.3.3 源码精读

`mma` 是 tcgen05 的计算核心，它根据 **A 操作数所在内存** 分两条路径，见 [python/tilus/lang/instructions/tcgen05.py:305-374](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L305-L374)：

- **SS 形态**：A、B 都在共享内存，调 `tcgen05_mma_ss`（对应 PTX `tcgen05.mma.kind::sm` 系列）。
- **TS 形态**：A 在 TMEM、B 在共享内存，调 `tcgen05_mma_ts`。

两者都要求由 **单 warp** 发起（校验 `num_threads == 32`，见 [python/tilus/lang/instructions/tcgen05.py:346-350](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L346-L350)），且累加器 `d` 必须是 `TMemoryTensor`。`cta_group=2` 时支持双 CTA 协作 MMA（详见类文档 [python/tilus/lang/instructions/tcgen05.py:316-325](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L316-L325)：每个 CTA 出一半 A、持一半 D）。

`commit` 把挂起的 tcgen05 异步操作（如 `mma`、`copy`）打包并在完成时通知一个 mbarrier，签名见 [python/tilus/lang/instructions/tcgen05.py:275-303](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L275-L303)。它同样强制单 warp（校验 32 线程）。

`copy`（共享→TMEM）则是另一种把数据送进 TMEM 的途径，异步、用 `commit` 同步，见 [python/tilus/lang/instructions/tcgen05.py:250-273](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L250-L273)。v0/v1 用 MMA 直接消费共享内存里的 s_a/s_b，所以没用到 `copy`；但 `copy` 在需要把预处理后的数据常驻 TMEM 的场景（如 TS 形态 MMA）很有用。

v0 主循环把上述原语串成最小的 Blackwell matmul 内核，见 [examples/blackwell_matmul/matmul_v0.py:61-91](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v0.py#L61-L91)：

```python
for offset_k in range(0, k_size, self.block_k):
    self.copy_async(src=g_a, dst=s_a, offsets=[offset_m, offset_k])   # 非 TMA 的异步拷贝
    self.copy_async(src=g_b, dst=s_b, offsets=[offset_n, offset_k])
    self.copy_async_wait_all()
    self.sync()
    with self.single_warp():
        self.tcgen05.mma(s_a, s_b.transpose(), t_acc, enable_input_d=offset_k != 0)
        self.tcgen05.commit(mbarrier=mbarriers[0])
        self.mbarrier.wait(mbarriers[0], phase=phase)
    self.sync()
    phase ^= 1
r_acc = self.tcgen05.load(t_acc)            # TMEM -> 寄存器
...
self.tcgen05.dealloc(t_acc)
```

注意 v0 的加载用的是 `copy_async`（上一讲的 cp_async，非 TMA），输出回写用的是普通的 `store_global`（非 TMA）。v1 在两端都升级成了 TMA，这正是下一节的主题。

#### 4.3.4 代码实践

实践目标：跟踪一次 tcgen05 MMA 的「异步发起—提交—等待」时序。

操作步骤：

1. 打开 [examples/blackwell_matmul/matmul_v0.py:68-80](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v0.py#L68-L80)。
2. 列出每条指令的「同步层级」：`copy_async`/`copy_async_wait_all`（cp_async 分组）、`tcgen05.mma`+`commit`+`mbarrier.wait`（tcgen05 异步事务）、`self.sync()`（线程执行同步）。
3. 思考：为什么 `mbarrier.wait` 必须在 `self.sync()` 之前？

需要观察的现象：`commit` 把 `mma` 挂起事务绑定到 `mbarriers[0]`；`wait` 阻塞该单 warp 直到 MMA 真的把结果写进 TMEM；随后 `self.sync()` 让整个 CTA 的 4 个 warp 都推进到一致点，进入下一轮迭代。`phase ^= 1` 是 mbarrier 的相位翻转（u7-l2 讲过）。

预期结果：你能解释「若把 `tcgen05.commit` 漏掉会怎样」——`mbarrier` 的 tx-count 不会被关联到 MMA，`wait` 可能等不到 MMA 完成或语义错乱。运行结果「待本地验证」（需 Blackwell 硬件）。

#### 4.3.5 小练习与答案

**练习 1**：`tcgen05.mma` 与 `wgmma`（u7-l2）在累加器位置上有何根本差异？

参考答案：`wgmma` 的累加器在寄存器（每个 warp 持有），MMA 完成后结果在寄存器里、需手动管理；`tcgen05.mma` 的累加器 `d` 必须是 `TMemoryTensor`，结果直接写在 TMEM、跨迭代常驻，省去寄存器倒腾。

**练习 2**：`tcgen05.load` 之后为什么还要调 `tcgen05.wait_load`？

参考答案：`load` 是异步的——发起后立即返回，数据未必已到寄存器。`wait_load`（PTX `tcgen05.wait::ld`）阻塞到所有挂起的 TMEM→寄存器搬运完成，之后才能安全使用寄存器里的 `r_acc`。v1 的 epilogue 中 `load` 与 `wait_load` 成对出现，见 [examples/blackwell_matmul/matmul_v1.py:99-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v1.py#L99-L100)。

### 4.4 TMA epilogue、proxy fence 与 cluster 协作

#### 4.4.1 概念说明

v1 相比 v0 有三处升级，构成本模块的三个要点：

1. **TMA epilogue（TMA 回写收尾）**：累加器从 TMEM 取出后，不直接 `store_global` 逐元素写，而是先写共享内存，再用 `tma.shared_to_global` 整块异步回写显存。为了让大 tile（如 `block_n=256`）的回写更可控，v1 用 `tcgen05.slice` 把累加器沿 N 维切成 `e_block_n` 宽的小条，逐条搬运。
2. **proxy fence**：`store_shared`（generic 代理写）与 `tma.shared_to_global`（async 代理读）走不同的内存访问路径，二者之间必须插 `fence.proxy_async` 保证可见性。
3. **cluster**：多个 CTA 组成「簇」，可直接互访共享内存、TMA 多播同一块数据给多个 CTA，还能做双 CTA 协作 MMA（`cta_group=2`）。v1 虽未显式用 cluster（`cluster_blocks` 默认为 1），但其指令组（`tma`/`tcgen05` 的 `multicast_mask`/`cta_group`、`cluster.sync`）都是为 cluster 设计的。

#### 4.4.2 核心流程

v1 的 TMA epilogue 数据流（把 TMEM 中的结果送回显存）：

```
TMEM(t_acc) --slice--> t_acc_slice(TMEM子视图)
        │ tcgen05.load + wait_load
        ▼
寄存器(r_acc) --cast fp16--> 共享内存(s_c)   # store_shared, 走 generic 代理
        │ fence.proxy_async(space="shared")  # 关键! 刷新 generic→async 可见性
        │ self.sync()
        ▼
共享内存(s_c) --tma.shared_to_global--> 显存(g_c)   # 走 async 代理
        │ tma.commit_group + tma.wait_group(n=0, read=True)
        ▼
显存(g_c) 最终结果
```

proxy fence 的必要性来自 GPU 的 **多代理（multi-proxy）** 设计。共享内存可被两条路径访问：

- **generic 代理**：普通 `store_shared`、寄存器→共享内存的写。
- **async 代理**：TMA 引擎的读写。

两条路径各自维护可见性，**generic 代理的写默认对 async 代理不可见**。`fence.proxy.async.shared::cta` 的作用就是强制把 generic 代理在此之前的共享内存写「冲刷」到 async 代理能看到的程度。CLAUDE.md 给出了这个问题的标准代码骨架，是本讲实践任务的核心。

#### 4.4.3 源码精读

v1 epilogue 的完整代码就是 proxy fence 教科书般的范例，见 [examples/blackwell_matmul/matmul_v1.py:89-113](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v1.py#L89-L113)：

```python
g_c = self.global_view(c_ptr, dtype=float16, shape=[m_size, n_size])
s_c = self.shared_tensor(dtype=float16, shape=[self.block_m, self.e_block_n])
for e_offset_n in range(0, self.block_n, self.e_block_n):
    t_acc_slice = self.tcgen05.slice(                  # 1. 切累加器子视图(只改元数据)
        t_acc, offsets=[0, e_offset_n], shape=[self.block_m, self.e_block_n], dims=[0, 1])
    r_acc = self.tcgen05.load(t_acc_slice)             # 2. TMEM -> 寄存器(异步)
    self.tcgen05.wait_load()                           #    等待 load 完成
    self.store_shared(s_c, r_acc.to(float16))          # 3. 寄存器 -> 共享内存(generic 代理写)
    self.fence.proxy_async(space="shared")             # 4. 关键 proxy fence!
    self.sync()
    with self.single_warp():
        self.tma.shared_to_global(                     # 5. 共享内存 -> 显存(async 代理读)
            s_c, g_c, offsets=[offset_m, offset_n + e_offset_n], dims=[0, 1])
        self.tma.commit_group()
        self.tma.wait_group(n=0, read=True)
    self.sync()
```

第 102 行的 `self.fence.proxy_async(space="shared")` 正是 CLAUDE.md 警告的那条 fence。`proxy_async` 的实现见 [python/tilus/lang/instructions/fence.py:40-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/fence.py#L40-L63)，其类文档把动机讲得很清楚：generic 代理写（`store_shared`）与 async 代理读（TMA）之间需要 proxy fence 保证可见性，见 [python/tilus/lang/instructions/fence.py:20-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/fence.py#L20-L38)。还有一个更轻量的 `proxy_async_release`（只做单向 generic→async 的 release，sm_90+），见 [python/tilus/lang/instructions/fence.py:65-78](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/fence.py#L65-L78)。

`tcgen05.slice` 是「只改元数据、不搬数据」的子视图操作，签名见 [python/tilus/lang/instructions/tcgen05.py:107-131](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tcgen05.py#L107-L131)。它让大累加器（如 `[128, 256]`）可以按列切成小块逐条回写，而无需复制 TMEM 内容。

cluster 的能力体现在多个指令组的可选参数上：`tma.global_to_shared` 的 `multicast_mask`/`cta_group`（[python/tilus/lang/instructions/tma.py:67-77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/tma.py#L67-L77)）、`tcgen05.mma`/`alloc` 的 `cta_group=2`、以及 `BlockClusterInstructionGroup` 的 cluster 级同步与跨 CTA 寻址。cluster 通过 `self.attrs.cluster_blocks`（启动期配置）声明，cluster 内 CTA 可互访共享内存——`cluster.sync()` 是簇级屏障（cluster 内所有 CTA 的所有线程都到达才放行），`map_shared_addr` 用 `mapa.shared::cluster` 把本 CTA 的共享内存地址翻译成 peer CTA 的地址，见 [python/tilus/lang/instructions/cluster.py:24-56](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/cluster.py#L24-L56) 与 [python/tilus/lang/instructions/cluster.py:110-138](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/cluster.py#L110-L138)。结合 `mbarrier.arrive_and_expect_tx_remote`（u7-l2）就能实现「一个 CTA 加载数据、通知另一个 CTA 的 mbarrier」的跨 CTA 生产—消费流水线。

#### 4.4.4 代码实践（本讲主任务）

实践目标：在 Blackwell 内核里定位 proxy fence 并解释其必要性（本讲指定的核心实践任务）。

操作步骤：

1. 打开 [examples/blackwell_matmul/matmul_v1.py:89-113](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v1.py#L89-L113)（TMA epilogue）。
2. 定位第 102 行 `self.fence.proxy_async(space="shared")`。
3. 回溯它的上下文：
   - 紧邻其 **前** 的是第 101 行 `self.store_shared(s_c, r_acc.to(float16))`（generic 代理写共享内存）。
   - 紧邻其 **后** 的是第 105 行 `self.tma.shared_to_global(s_c, g_c, ...)`（async 代理读共享内存）。
4. 对照 CLAUDE.md 中「Proxy fence required between `store_shared` and `tma.shared_to_global`」一节，确认这正是文档警告的场景。

需要观察的现象：若把第 102 行删掉重新编译运行（先删缓存目录强制重编，见 u8-l1），程序可能 **不报错但结果偶发错误**——因为 TMA 引擎读到的是 `store_shared` 之前的旧共享内存内容，而非刚写入的 fp16 结果。这种「不崩但算错」的 bug 极难定位，正是 proxy fence 存在的理由。

预期结果：你能用自己的话说清这条链路——`store_shared` 走 generic 代理，`tma.shared_to_global` 走 async 代理，`fence.proxy.async.shared::cta` 强制 generic 代理的写在 TMA 读之前对 async 代理可见。运行结果「待本地验证」（需 Blackwell sm_100a 硬件；可在 CPU 上对照阅读理解）。

> 说明：此实践以源码阅读为主。若你有 Blackwell 硬件，可复制 v1 到一个新文件，删掉第 102 行的 fence 后跑 `main(bench=False)`，观察 `torch.testing.assert_close` 是否偶发失败。

#### 4.4.5 小练习与答案

**练习 1**：`fence.proxy_async` 与 `proxy_async_release` 有何区别？何时用后者？

参考答案：`proxy_async` 是 **双向** fence（generic↔async），`proxy_async_release` 是 **单向** 的 generic→async release（更轻量，sm_90+）。当场景确定是「generic 写 → async 读」（如本例 `store_shared` → `tma.shared_to_global`）时，用 `proxy_async_release` 足够且开销更低。

**练习 2**：v1 的 epilogue 为什么用 `tcgen05.slice` + 循环，而不是一次性 `load` 整个累加器？

参考答案：大累加器（如 `[128, 256]`）一次性 load 进寄存器、再 `store_shared` 需要很大的共享内存与寄存器中转空间。用 `slice` 沿 N 维切成 `e_block_n` 宽的小条，逐条 load→store_shared→TMA 回写，把峰值共享内存占用压到 `[block_m, e_block_n]`（见 v1 第 91 行的 `s_c` 形状），且每条 TMA 回写可以和下一条的 load 部分重叠。`e_block_n` 本身也是 `@autotune` 的调优参数（见 [examples/blackwell_matmul/matmul_v1.py:15-19](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v1.py#L15-L19)）。

## 5. 综合实践

把 v0 与 v1 对照阅读，画出 Blackwell matmul 的 **完整数据流与同步时序图**，要求标注以下要素：

1. **加载阶段**：v0 用 `copy_async`（cp_async）、v1 用 `tma.global_to_shared`（TMA）。在图上标出两者的同步原语差异（`copy_async_wait_all` vs `arrive_and_expect_tx` + `mbarrier.wait`）。
2. **计算阶段**：两者都用 `tcgen05.mma` + `commit` + `mbarrier.wait` 在 TMEM 累加。标出 `enable_input_d` 在首轮与后续轮的取值。
3. **回写阶段**：v0 用 `tcgen05.load` + `store_global`（逐元素）；v1 用 `slice` + `load` + `wait_load` + `store_shared` + **proxy fence** + `tma.shared_to_global` + `commit_group`/`wait_group`。重点标出 proxy fence 的位置与其前后两条代理路径。
4. **相位翻转**：在每次 `mbarrier.wait` 后标 `phase ^= 1`。

完成后，回答一个开放问题：如果把 v1 的回写改成「`store_shared` 之后直接 `store_global`（不经 TMA）」，还需要 proxy fence 吗？为什么？

> 参考思路：`store_global` 走的是 generic 代理（与 `store_shared` 同路径），不涉及 async 代理，因此不需要 generic→async 的 proxy fence。proxy fence 只在 **跨代理** 访问同一块内存时才需要。

## 6. 本讲小结

- **TMA** 是 Blackwell/Hopper 的专用异步搬运引擎，`global_to_shared` 用 `mbarrier`（tx-count 追踪字节数）同步，`shared_to_global` 用 `commit_group`/`wait_group` 同步，且要求 warp 对齐线程组。
- **TMEM** 是 Blackwell 张量核专属的片上累加器存储（128 lane × 512 column × 32 bit），`TMemoryLayout` 用 lane/column 原生坐标建模，`shape[0]` 必须是 32/64/128。
- **tcgen05 指令组** 管理 TMEM 全生命周期：`alloc`/`dealloc` 分配、`slice`/`view` 改元数据视图、`load`/`store`/`copy` 异步搬运、`mma`（SS/TS 两形态）做张量核乘累加，异步操作用 `commit` + `mbarrier.wait` 或 `wait_load` 同步。
- **tcgen05 MMA 的累加器常驻 TMEM**，这是它与 Hopper `wgmma`（累加器在寄存器）的根本差异，省掉了寄存器倒腾与溢出。
- **proxy fence** 在跨内存代理访问同一块共享内存时必不可少：`store_shared`（generic 代理写）与 `tma.shared_to_global`（async 代理读）之间必须插 `fence.proxy_async`，否则 TMA 可能读到旧数据。
- **cluster** 让多个 CTA 互访共享内存、TMA 多播、双 CTA 协作 MMA（`cta_group=2`），配合 `map_shared_addr` 与远程 `mbarrier` 可构建跨 CTA 生产—消费流水线。

## 7. 下一步学习建议

本讲把 Blackwell 的三大硬件能力（TMA、tcgen05/TMEM、cluster）讲到了「单 CTA、无流水线」的程度。建议接着学：

- **u7-l4 异步软件流水线**：`examples/blackwell_matmul/matmul_v2.py` 及之后版本会用 `Pipeline` 抽象 + 多级 `mbarrier` 缓冲，把 TMA 加载与 tcgen05 MMA 重叠起来，掩盖显存延迟。本讲的 `arrive_and_expect_tx`/`wait`/`phase` 正是构建流水线的基本积木。
- **tcgen05 的 `cta_group=2` 与 cluster 深入**：结合 `BlockClusterInstructionGroup`（[python/tilus/lang/instructions/cluster.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/cluster.py)）与高阶示例（v3+），看双 CTA 如何各持一半累加器、TMA 如何多播。
- **后端发射器视角**：若想了解这些指令最终如何变成 PTX（如 `tcgen05.mma` → `tcgen05.mma`、`tma.global_to_shared` → `cp.async.bulk.tensor...`），可阅读 `python/tilus/backends/emitters/cuda/` 下对应的发射器（u6 讲过发射器注册机制）。

> 阅读提示：本讲所有示例的实际运行都需要 Blackwell（sm_100a）硬件；在非 Blackwell 机器上，可借助 Tilus 的 compile-only 测试模式（见 u8-l4）至少完成编译期校验，或以源码阅读理解数据流。
