# A5 混合核 MegaMoE 融合算子：dispatch_mega_combine

## 1. 本讲目标

本讲是单元六的收官，走读 `kernels/manual/a5/dispatch_mega_combine`——一个跑在 Ascend 950（A5）混合核上的端到端 MoE（Mixture of Experts）融合算子。它把「token 量化 → 路由交换 → 分组 FFN → 合并 → 还原」整条链路压进**一个** device kernel，用 PTO Manual 内核写成。学完本讲你应该能：

1. 说出 MegaMoE 七段流水（FrontReorder→Dispatch→GMM1→SwiGLU→GMM2→Combine→Unpermute）各阶段的职责与数据依赖；
2. 讲清 AIC/AIV 混合核的 CV（Cube-to-Vector）pipe：TMOV 单模式直达搬运 + intra-block 旗标构成的单槽 FIFO；
3. 掌握 GMM 任务邮箱的 producer-consumer 票据调度：一张 P2C 票据表、一个 C2P 进度表、六状态的车道状态机；
4. 了解 HCCL window（远端窗口）与 MPI 在多 rank MoE 算子里的分工——MPI 管 host 侧 bootstrap，HCCL window 管设备侧跨 rank 可见数据；
5. 分析 256×256 tile 粒度、共享 L0C 累加器、FIFO depth=1 这组「反直觉」的容量/带宽取舍。

本讲综合运用 u5-l6（A5 平台与 MX 低精度）、u6-l4（ready_queue 生产者-消费者）和 u3-l1/u3-l2（事件与流水线）的知识。

## 2. 前置知识

### 2.1 MoE：把 FFN 拆成多个专家

MoE 层的核心想法：不把每个 token 送进同一个 FFN，而是由 router 给每个 token 挑 `topK` 个「专家」（各自的 FFN 权重），最后按路由概率加权求和：

```text
out[token] = Σ (topK 路由) probs[token, route] * FFN_expert(x[token])
```

多卡部署时专家分布在不同 rank 上，token 需要跨卡汇聚到专家所在的 rank（Dispatch）、算完再送回来（Combine）。这是 MoE 通信量的来源，也是本算子把 Dispatch/Combine 与计算融合的原因。

### 2.2 A5 混合核：AIC + AIV

A5 的每个物理 block（physical block）含 **1 个 AIC（Cube 核，矩阵乘）+ 2 个 AIV（Vector 核，向量/搬运）**。典型配置 36 AIC / 72 AIV。设备代码里：

- `__DAV_CUBE__` 分支编进 AIC，`__DAV_VEC__` 分支编进 AIV；
- `get_block_idx()` 返回物理 block 号（0..P-1），`get_subblockid()` 区分 AIV0（sub-block 0）与 AIV1（sub-block 1）；
- AIC 与同一 block 内的 AIV1 之间有**直达数据通路**（L0C→UB）和 **intra-block 旗标**（`set_intra_block`/`wait_intra_block`），这是本讲 CV pipe 的硬件基础。

### 2.3 CV pipe：算完直接递给隔壁核

传统写法是 AIC 把 GEMM 结果写回 GM、AIV 再从 GM 读回来——两次 GM 往返。CV pipe 则让 AIC 经 FIX 流水把 L0C 累加结果直接搬进配对 AIV1 的 UB 槽位，**中间不落 GM**。u5-l6 介绍过通用形态的 TPUSH/TPOP 指令；本算子选择用 `pto::TMOV`（`AccToVecMode::SingleModeVec1`，即定向到 AIV1）+ 手工 intra-block 旗标自建一个单槽 FIFO，因为它们需要 x/gate 两个半槽共用一个 256 KiB UB slot，还附带一条控制流。数据面与控制面分开，是理解本讲的钥匙。

### 2.4 与 u6-l4 的关系

u6-l4 的 gemm_ar 用 host 侧 `ready_queue`（GM 上的生产者-消费者队列）解耦计算核与通信核。本讲的 GMM 任务邮箱是同一思想在**设备侧、多消费者**场景的升级：不再是一个队列，而是「每 AIC 一个票据槽（P2C）+ 每 AIC 一个进度槽（C2P）+ 一个中心 producer」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `kernels/manual/a5/dispatch_mega_combine/README.md` | 算子全景：七段流水、tiling 参数、核分组表、HCCL window 布局、运行方式 |
| `kernels/manual/a5/dispatch_mega_combine/main.cpp` | host 入口：MPI bootstrap、ACL/HCCL 初始化、tiling、launch、校验与计时 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h` | 设备流水入口：按 `__DAV_CUBE__`/`__DAV_VEC__` 给 AIC/AIV 分配角色 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine_tiling.h` | 设备侧 tiling 结构与全局常量（含 `kMegaMoeGmmTileM/N`） |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1.h` | 第一段分组矩阵乘：direct wave0 / fixed wave / mailbox 三种模式 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h` | GMM 任务邮箱 producer：票据发现、车道状态机、P2C 发布 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h` | GMM1→SwiGLU CV pipe 常量与旗标原语 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm2_combine_cv_pipe.h` | GMM2→Combine CV pipe 常量 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_common.h` | GMM 公共 tile 切分、swizzle、流水线类型别名 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/pto_gmm_mx_preload.hpp` | GMM 计算流水（MX 预取、L0C 复用、CV 直达搬运） |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/hccl_window.hpp` | 设备侧远端窗口访问：窗口基址翻译、信号槽布局、epoch 等待 |
| `kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/const_args.hpp` | UB 预算等常量（216 KiB 主区 + 40 KiB 同步保留区） |

## 4. 核心概念与源码讲解

### 4.1 MoE 流水拆解：七段流水与数据依赖

#### 4.1.1 概念说明

算子把 MoE FFN 主路径拆成七段，每段绑定固定的核角色，段与段之间只通过「GM 工作区 + CV pipe + 远端窗口」传递数据：

```text
FrontReorder -> Dispatch -> GMM1 -> SwiGLU -> GMM2 -> Combine -> Unpermute
```

各段职责（以 2 rank、`expertPerRank=16`、`topK=8`、MXFP8 数据为默认）：

| 阶段 | 运行核 | 输入 | 输出 |
| --- | --- | --- | --- |
| FrontReorder | 全部 AIV | `x[M,K]` BF16、`expertId[M,topK]` | 源 token 的 MXFP8 记录（HCCL window）；每专家路由位掩码；`cumsumMM`/`expertTokenNums` |
| Dispatch | AIV0 前缀（20/21/24 个） | 各源 rank 的掩码+记录 | 目的 rank 工作区的 `gmA`/`gmAScale`（按专家紧凑排列）+ `routeMeta` |
| GMM1 | AIC | `gmA`×`weight1`（MXFP8） | x/gate 两个 BF16 半 tile（CV 直达，不落 GM） |
| SwiGLU | 配对 AIV1 | x/gate CV tile | `silu(x)*gate` 再动态量化为 MXFP8 → `gmSwigluA/Scale` |
| GMM2 | AIC | SwiGLU 输出×`weight2` | BF16 结果 tile（CV 直达 Combine） |
| Combine | 配对 AIV1 | GMM2 CV tile + `cumsumMM`/`preSum` | 写各源 rank 的 `combineOutputByRouteSlot`（远端窗口） |
| Unpermute | 全部 AIV | `expandedRowIdx`+远端输出+`probs` | TopK 加权还原为 `out[M,K]` |

#### 4.1.2 核心流程

设备入口只做两件事：先全 AIV 跑 FrontReorder，再进入按核分组的 `ProcessFixedGroups`：

```text
MegaMoe::Process()
  ├─ __DAV_VEC__: FrontReorderProcess(...)      // 量化 + 路由掩码发布
  └─ ProcessFixedGroups()
       ├─ __DAV_CUBE__（AIC 分支）
       │    ├─ fixedWave 模式: GMM1 独占流水 → 发布 wave 结束票据 → 新流水跑 GMM2
       │    └─ wave0+mailbox 模式: GMM1/GMM2 共享流水，邮箱票据衔接
       └─ __DAV_VEC__（AIV 分支，按 subblockId/物理块号分角色）
            ├─ 最后一个 AIV0: GMM 任务 producer（本讲 4.3）
            ├─ 中段 AIV0: 延迟路由元数据 / 进度协调器
            ├─ 前段 AIV0: Dispatch 拉取
            ├─ 所有 AIV1: SwiGLU → Combine（配对本 block 的 AIC）
            └─ 全部: Unpermute（两阶段时先 16 个 AIV0 快照消费）
```

host 侧按 AIC 数查表选分组（28/32/36 三档，见 README 的默认映射表），AIV 数恒为 `2*aicNum`。

#### 4.1.3 源码精读

入口头的 include 区已经体现了「一文件两编译」：`__DAV_VEC__` 下拉入全部向量侧阶段头，`__DAV_CUBE__` 下只拉 GMM1/GMM2——[dispatch_mega_combine.h:L20-L34](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L20-L34)。AIC 分支中，fixed-wave 模式下 GMM1 用独立流水、发布 `kGmmMailboxGmm1Wave0EndTicket` 后**新建** `gmm2Pipeline`；mailbox 模式下 GMM1/GMM2 **共享**同一条流水，第一个 GMM2 任务由邮箱交接直接带过来——[dispatch_mega_combine.h:L123-L142](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L123-L142)。

AIV 分支的角色判定全部基于 `subblockId` 与 `physicalBlockId`，见 [dispatch_mega_combine.h:L143-L189](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L143-L189)：producer 固定在「最后一个物理块」的 AIV0；它前一个物理块的 AIV0 兼任 `Gmm2ExpertProgressCoordinator`；中段 AIV0 构建延迟路由元数据，其中 0 号 worker 额外负责「等 Dispatch 完成 + 所有源 rank preSum 可见」后才放行 GMM2 入口。AIV1 先跑 SwiGLU 再跑 Combine——[dispatch_mega_combine.h:L211-L227](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L211-L227)。全流程描述与架构图见 [README.md:L173-L195](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L173-L195)。

一个关键工程细节：Unpermute 的 worker 编号刻意做成**稳定逻辑序**——AIV1 取 `[0,P)`、AIV0 取 `[P,2P)`（[dispatch_mega_combine.h:L229-L233](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L229-L233)），让 token 到 worker 的分派不随物理拓扑漂移。

#### 4.1.4 代码实践

1. **实践目标**：建立七段流水的「核分工」心智图。
2. **操作步骤**：通读 [README.md:L197-L307](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L197-L307)（各 Stage 小节）；再对照 [README.md:L110-L123](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L110-L123) 的默认分组表，画一张 36 AIC 拓扑的横向条形图：横轴是物理块 0..35，每格标注该块 AIC/AIV0/AIV1 分别承担哪些阶段。
3. **观察现象**：Dispatch 用 0..23 号 AIV0，producer 占 35 号 AIV0，进度协调器占 34 号，AIV1 全部「SwiGLU+Combine」复用。
4. **预期结果**：图中应出现明显的「前缀 AIV0 = Dispatch、后缀 AIV0 = 元数据/producer、AIC = GMM（分组随 wave 切换）、AIV1 = CV 消费者、全员 = Unpermute」分层；能指出 GMM2 初始组的 AIC 编号区间（36 AIC 时为 24..35）。
5. 本实践为源码阅读型，无需运行硬件；图形输出「待本地验证」具体样式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FrontReorder 要把 token 量化成 MXFP8 记录放到 HCCL window，而不是直接暴露原始 BF16 `x`？
**答案**：Dispatch 是按路由掩码「拉取」紧凑行，源 rank 只需发布一份紧凑记录；量化一次（E4M3 数据 + E8M0 scale）后，跨 rank 传输量降为 BF16 的一半左右，且 GMM1 直接吃 MXFP8 输入，省去目的 rank 的重复量化（见 README「Mask Pull Front」，[README.md:L95-L98](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L95-L98)）。

**练习 2**：GMM2 入口为什么要等「延迟元数据 + 本地 Dispatch 完成 + 所有源 rank 的 preSum 行可见」三件事？
**答案**：GMM2 的任务描述符由延迟元数据 worker 生成（邮箱的前提）；GMM2 读 SwiGLU 输出，而 SwiGLU 依赖 GMM1，GMM1 又依赖 Dispatch 写的 `gmA`；Combine 写远端 compact 行时需要各源 rank 的 `preSumBeforeRank` 基址。三者在 [dispatch_mega_combine.h:L170-L183](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L170-L183) 由 0 号元数据 worker 统一汇聚后才 `PublishGmm2EntryReady`。

### 4.2 混合核 CV pipe：AIC→AIV 的单槽直达 FIFO

#### 4.2.1 概念说明

「CV pipe」指 AIC（Cube）把 L0C 上的结果经 FIX 流水直接送进同 block AIV1 的 UB，未经 GM。本算子把它组织成**深度为 1 的 FIFO**：

- **数据面**：`pto::TMOV<..., AccToVecMode::SingleModeVec1>`——底层 `pto_copy_matrix_cc_to_ub` 以 `subBlockId=1` 定向写入 AIV1 的 UB（[TMov.hpp:L190-L235](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TMov.hpp#L190-L235)）；
- **控制面**：intra-block 旗标。payload 用 ready=7 / free=8 两个旗标，control 流用 9/11 起的按槽旗标；
- **GMM1→SwiGLU**：一个 256 KiB slot 同时装 x（前 128 KiB）与 gate（后 128 KiB），深度 1；
- **GMM2→Combine**：一个 256×256 BF16 tile（128 KiB）单槽，深度 1。

深度 1 看似「没有流水」，实际上它是一个**信用制单缓冲**：AIC 只有等到 AIV 释放（free 旗标）才复用槽位；由于 AIC 的下一块 Cube 计算与 AIV 的当前块消费可以重叠，单槽已足够掩盖大部分延迟，同时把 UB 占用压到最低——这正是 0c9d93c9 重构的取舍（见 4.5）。

#### 4.2.2 核心流程

以 GMM1→SwiGLU 为例，一次「生产-消费」回合：

```text
AIC 侧（生产者）                              AIV1 侧（消费者）
──────────────────────                       ──────────────────────
ProducerAllocate:                            ConsumerWait:
  tileIndex >= 1 ?                             wait_intra_block(PIPE_V, ready=7)
  wait_intra_block(FIX, free=8@AIV1)               ↓ 读 x/gate 半槽
  ↓                                          计算 silu(x)*gate + TQUANT 量化
TMOV x  半槽 → AIV1 UB[0      ..128KiB)       存 GM（E4M3+E8M0）
TMOV gate半槽 → AIV1 UB[128KiB..256KiB)       等 MTE3 存储完成
pipe_barrier(PIPE_FIX)  ← TMOV 异步！          ConsumerRelease:
set_intra_block(FIX, ready=7@AIV1)               set_intra_block(PIPE_V, free=8)
  ↑ 只有both半块都落 UB 才置 ready
```

注意生产者的旗标号要加偏移 16（`kMegaMoeFixedSecondAivSubblockFlagOffset`）才能寻址到 AIV1 子块的旗标空间；消费者（自己就是 AIV1）用裸旗标号。控制流（携带 GMM 任务描述符，告诉 AIV1 下一个 tile 属于哪个专家/行列）比 payload 流多一个深度（`depth+1=2`），保证 stage-end 控制字能提前发布。

#### 4.2.3 源码精读

槽位几何由常量锁定，并用 `static_assert` 把「半槽=128 KiB、整槽=整个 UB(256 KiB)、旗标号不越界」写死在编译期——[gmm1_swiglu_cv_pipe.h:L18-L55](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h#L18-L55)：`kGmm1SwigluCvTileRows=256`、`kGmm1SwigluCvOutputCols=256`、`kGmm1SwigluCvFifoDepth=1`、`kGmm1SwigluControlFifoDepth=2`。

生产者三原语（Allocate/Record/Drain）在 [gmm1_swiglu_cv_pipe.h:L72-L118](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h#L72-L118)；消费者三原语在 [gmm1_swiglu_cv_pipe.h:L120-L145](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h#L120-L145)。特别注意 `RecordPairDirect` 里的注释——「TMOV 在 FIX 上是异步的，AIV 的 ready 旗标绝不能先于两个半块都到位而可见」，所以先 `pipe_barrier(PIPE_FIX)` 再置 ready（[pto_gmm_mx_preload.hpp:L140-L147](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/pto_gmm_mx_preload.hpp#L140-L147)）。这是 u7-l2「事件先于数据写回」原则在跨核场景的镜像：**旗标必须后于数据**。

真正的搬运在 `EnqueuePairHalfToAiv1`：把 `Gmm1SwigluCvHalfTile` TASSIGN 到 CV 槽的 x 或 gate 偏移，M→FIX 事件对齐后发 TMOV（[pto_gmm_mx_preload.hpp:L360-L370](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/pto_gmm_mx_preload.hpp#L360-L370)）。GMM2 侧同构：`EnqueueDirectReserved` 先在「Cube 已下发、环可能满」的正确位置做 `Gmm2CombineProducerAllocate` 等待，再 TMOV 整个 256×256 tile（[pto_gmm_mx_preload.hpp:L159-L173](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/pto_gmm_mx_preload.hpp#L159-L173)），其常量见 [gmm2_combine_cv_pipe.h:L19-L31](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm2_combine_cv_pipe.h#L19-L31)。

消费端 `Swiglu::ComputeDirectPayload`（[swiglu.h:L144-L186](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/swiglu.h#L144-L186)）展示 UB 生命周期复用：x/gate 直接 TASSIGN 在 CV 槽上读入，量化工作 tile 复用同一区域，**只有 E4M3/E8M0 的 GM 存储双双完成后才释放槽位**（L183 注释），随后 `atomicAdd` 递增该专家的 GMM2 依赖计数（[swiglu.h:L210-L222](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/swiglu.h#L210-L222)）——这个计数正是 4.3 中 producer「就绪发现」的输入。

#### 4.2.4 代码实践

1. **实践目标**：验证「删掉一个同步点会怎样」的推演能力（CPU 模拟器跑不了混合核，本实践为源码推演型）。
2. **操作步骤**：抄录 [pto_gmm_mx_preload.hpp:L140-L147](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/pto_gmm_mx_preload.hpp#L140-L147) 的 `RecordPairDirect`，注释掉 `pipe_barrier(PIPE_FIX)`；再对照 [gmm1_swiglu_cv_pipe.h:L126-L132](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h#L126-L132) 写出 AIV1 侧对应的 wait 点。
3. **观察现象**（推演）：ready 旗标可能插队到 gate 半块 TMOV 之前可见；AIV1 读到的 gate 是上一轮的旧数据或未定义值。
4. **预期结果**：SwiGLU 输出错乱且错误呈现「按 tile 伪随机」分布——这正是 u3-l1 所说「CPU 模拟器检不出、必须上真机/sim 才能暴露」的事件链错误类别；不要真的把改动带进构建。
5. 结论「待本地验证」——需要 A5 真机或 CANN 仿真环境才能观测。

#### 4.2.5 小练习与答案

**练习 1**：为什么控制流 FIFO 深度是 `payload depth + 1 = 2`？
**答案**：GMM1 结束时要发布一个 `kGmmTaskFlagStageEnd` 控制字让 SwiGLU 收尾（[gmm1.h:L474-L478](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1.h#L474-L478)）。若控制流与 payload 同深，最后一个任务的控制字会与「最后一个 payload 尚未确认」形成死锁；多一格缓冲让 stage-end 控制字能在 payload 结算期间入队。

**练习 2**：TPUSH/TPOP（u5-l6）与这里的 TMOV+旗标方案是什么关系？
**答案**：两者都是 AIC→AIV 的 CV 交接。TPUSH/TPOP 是 A5 后端提供的通用 FIFO 指令对（`include/pto/npu/a5/TPush.hpp`/`TPop.hpp`，自带派发与同步语义）；本算子需要 x/gate 共槽、控制流复用与精确的旗标编排，故用 `TMOV<SingleModeVec1>` 做数据面、`set/wait_intra_block` 自建控制面。能力等价、控制粒度更细。

### 4.3 GMM 任务邮箱：producer-consumer 票据调度

#### 4.3.1 概念说明

AIC 的 GMM 任务不再由静态波次（fixed wave）独占，而是引入**任务邮箱**：一个专职 producer（最后一个物理块的 AIV0）观察「哪些 tile 的输入已经就绪」，把任务以**票据（ticket）**形式派发给空闲 AIC；AIC 消费完把进度写回，producer 看到进度等于已发票据才算该槽位空出、可派下一张。两张表：

- **P2C（producer-to-consumer）**：每 AIC 一个槽，存「下一个要执行的任务票据」；
- **C2P（consumer-to-producer）**：每 AIC 一个槽，存「已完成到的票据」。

票据值即任务序号：`0`=空、`1..N`=GMM1 任务、`gmm2TicketBase..`=GMM2 任务、`UINT32_MAX`=终止符（[gmm_task_queue.h:L20-L22](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_queue.h#L20-L22)）。这套设计解决了 MoE 的「负载不均」：每个专家的 token 数运行期才知道（`currentM` 来自 `cumsumMM`），静态切分会让拿到小专家的 AIC 提前空转，而邮箱能动态填满。

#### 4.3.2 核心流程

producer 是一个协作式多任务循环，每轮做两类工作（节流周期不同）：

```text
MegaMoeGmmTaskProducer::ProcessMailbox()
  等待 front-metadata epoch（入口同步）
  预构建 ExpertTaskLayout：每专家 currentM + GMM1/GMM2 任务前缀和
  while (未全部车道 Done):
    Task A（每 ~1000 ticks）就绪发现：
      取当前专家的 ready 计数快照（一条 MTE2 跨距载入）
      只推进「连续就绪前缀」readyTail —— 保证任务按序可发
    Task B（每 ~50 ticks）邮箱推进：
      等上一拍 C2P 快照 → 发下一拍快照（双缓冲，2 槽）
      检查 GMM2 门（Gmm2EntryReady）
      for 每条 AIC 车道（轮转起点 laneCursor）：
        观测 wave0-end → 迁移车道相位
        C2P 进度 == 已发票据 ? 回收槽位（终止票据 → 车道 Done）
        相位推进（GMM1Pc→GMM2Pc→TerminalReady）
        TakeReadyTicket → 暂存进 P2C 影子 tile
      FlushMailboxP2c：影子 tile 一次性 MTE3 写回 GM
    无进展 → GmmPollBackoff 退避
```

车道相位是六状态机：`kAwaitGmm1Wave0End → kGmm1Pc → (kAwaitGmm2Wave0End) → kGmm2Pc → kTerminalReady → kDone`（[gmm_task_producer.h:L49-L56](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L49-L56)）。GMM1 侧 wave0（首波专家）由全部 AIC 直接执行、不走邮箱；邮箱只管「后缀」任务——这就是 README 的「Dual-mode GMM1 scheduling」。

#### 4.3.3 源码精读

就绪发现 `DiscoverReadyTickets`（[gmm_task_producer.h:L260-L300](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L260-L300)）：先 `LoadReadyExpertSnapshot` 把该专家全部 `blockM` 槽的计数用**一条带跨距的 GM→UB 载入**拿进 UB（[gmm_task_producer.h:L246-L258](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L246-L258)，GMM1 看 Dispatch 的 64 B 隔离计数槽，GMM2 看 SwiGLU 的 atomicAdd 依赖槽），再只推进连续前缀——跳过任何未就绪 tile 会破坏按序性。

主循环 [gmm_task_producer.h:L407-L516](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L407-L516) 里，C2P 观察是「一次 MTE 载入看全部 36 条车道」——`IssueProgressSnapshot` 与消费侧 wait 构成双缓冲快照（2 个 slot 乒乓，[gmm_task_producer.h:L338-L347](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L338-L347)）；P2C 发布同理：票据先写进 UB 影子 tile（`StageMailboxTicket`，[gmm_task_producer.h:L170-L179](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L170-L179)），一轮车道巡访结束后 `FlushMailboxP2c` 用**一次** MTE3 向量存储整表刷回（[gmm_task_producer.h:L321-L336](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L321-L336)），把 36 次标量写合并成 1 次 burst——producer 自身的带宽开销被压到最低。

AIC 消费侧的等待点是 `WaitDispatchTileReady`（[gmm1.h:L122-L136](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1.h#L122-L136)）：对目标 tile 的计数槽循环执行 `dcci`（失效缓存行）+`dsb`+回读，配 `EpochPollBackoff` 退避，直到观测行数等于 `actualM`——**GMM1 只等它即将消费的那个输入 tile**，而不是等整个专家就绪。mailbox 消费在 `Gmm1::ProcessMailbox`（[gmm1.h:L409-L492](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1.h#L409-L492)）：当前 tile 计算与前继 tile 的 FIX 存储、后继票据的邮箱探测（`panelProbe.SuccessorProbe()`）三者重叠；当 `next.ticket >= gmm2TicketBase` 时该 AIC 无缝转入 GMM2——**没有全局 GMM1/GMM2 barrier**。

#### 4.3.4 代码实践

1. **实践目标**：总结 producer 循环中的全部「等待点」，理解它的活性依赖。
2. **操作步骤**：精读 [gmm_task_producer.h:L349-L523](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L349-L523)，列出所有会阻塞/空转的位置及其解除条件。
3. **需要观察的现象**：等待点分三类——入口同步、节流轮询、进度回收。
4. **预期结果**（参考清单）：
   - 入口 `WaitEpochAcquire(front-metadata)`（L112-L114）：等 Front 元数据发布；
   - Task A 的隐式等待：`DiscoverReadyTickets` 未达标时不推进，靠 ~1000 ticks 周期重探；
   - Task B 的 `wait_flag(MTE2,S)` 等 C2P 快照载入完成（L434）；
   - `gmm2Gate` 未开前不派任何 GMM2 票据（L440-L445，L493）；
   - P2C 写忙碌时的 `wait_flag(MTE3,S)`（`AcquireMailboxP2cWrite`，L162-L168）；
   - 车道未回收票据时不派新票（L463-L474）；
   - 全部车道收到终止票据后退出循环（L509-L511）。
5. 本实践为源码阅读型，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么就绪发现只推进「连续前缀」，而不是「谁就绪发谁」？
**答案**：票据序号同时是任务执行序。GMM1 的 tile 按 swizzle 顺序编号、依赖 Dispatch 的按行就绪计数；若乱序派发，`readyTail` 与 `publishTail` 的语义就会被破坏，producer 也无法用「前缀和表 + 一个尾指针」O(1) 定位任务归属专家（`AdvanceExpert`，[gmm_task_producer.h:L216-L222](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L216-L222)）。

**练习 2**：AIC 怎么知道「自己的下一张票」来了？
**答案**：轮询自己的 P2C 槽：`WaitGmmMailboxTask` 循环读票据值，跳过「空(0)」与「上一张已处理过的值」，见 [gmm_task_queue_device.h:L218-L235](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_queue_device.h#L218-L235)；票据落在 `gmm2TicketBase..` 区间即触发 GMM1→GMM2 阶段迁移。

**练习 3**：producer 为什么要轮转起点（`laneCursor` 每轮 +1）？
**答案**：若每轮都从车道 0 开始巡访，低编号 AIC 的槽位总是先被检查、先拿到票据，高编号车道在任务紧张时会被饿死；轮转起点让派发在车道间公平轮询（`AdvanceMailboxLane`，[gmm_task_producer.h:L154-L160](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L154-L160)）。

### 4.4 HCCL window 与 MPI 多 rank 协作

#### 4.4.1 概念说明

多 rank MoE 需要每张卡都能读写其他卡的「跨 rank 可见数据」。本算子用 HCCL 的 RDMA window：每个 rank 暴露一段对称窗口内存，`windowIn[rankId]` 记录所有 rank 的窗口本端地址，于是「写远端」=「把窗口内偏移翻译到对端基址」。窗口里放 `sourceTokenRecords`、`routeMaskSlots`、`combineOutputByRouteSlot`、`preSumBeforeRank` 和信号尾部（epoch/进度槽）；纯本地的工作区（`gmA`、`gmSwigluA`、队列/邮箱等）放普通 GM。

MPI 不参与设备侧数据面，只做 **host 侧 bootstrap**：拉起进程组、交换 HCCL root info、对齐校验结论。设备侧一切跨 rank 同步走窗口信号（epoch 轮转 + `dcci` 失效 + `dsb`）。

#### 4.4.2 核心流程

```text
host（每 rank 一份 main.cpp）
  MPI_Init → rank/worldSize
  deviceId = firstDevice + rank          // 连续物理卡映射
  aclInit / aclrtSetDevice
  aclrtGetDeviceInfo(AICORE_CORE_NUM)    // 28/32/36 选档
  rank0: HcclGetRootInfo → MPI_Bcast     // 全 rank 拿到同一份通信根信息
  RunOneRank: HCCL 初始化 + 窗口建立 + tiling + launch + 校验/计时
  MPI_Barrier 对齐退出

device（hccl_window.hpp 的 PtoRemoteWindow）
  RemotePtr(localPtr, rank) = windowIn[rank] + (localPtr - windowIn[myRank])
  LocalSignalBase  = 本端窗口末尾 1 MB
  RemoteSignalBase = 信号基址翻译到对端
```

#### 4.4.3 源码精读

host 主流程在 [main.cpp:L782-L880](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/main.cpp#L782-L880)：L789-L798 取 rank 并映射物理卡；L818-L823 用 `aclrtGetDeviceInfo(..., ACL_DEV_ATTR_AICORE_CORE_NUM, ...)` 查询核数并选档；L868-L878 rank0 生成 `HcclRootInfo` 后 `CommMpiBcast` 广播、`CommMpiBarrier` 对齐，再各自 `RunOneRank`。

设备侧 `PtoRemoteWindow` 类（[hccl_window.hpp:L133-L171](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/hccl_window.hpp#L133-L171)）是窗口的设备视图：`Init` 从 GM 里的 `PtoRemoteWindowContext`（host 已拷入设备）恢复 rank/rankSize/窗口大小；`RemotePtr` 做基址平移翻译；信号区固定在窗口**末尾 1 MB**。信号槽布局是一张精心排布的静态表（[hccl_window.hpp:L28-L81](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/hccl_window.hpp#L28-L81)）：每个生产者槽独占一个缓存行（16 个 int32，`*_STRIDE=16`），覆盖 Dispatch 释放、Combine 数据就绪、专家进度、Front/preSum epoch、Unpermute 两阶段掩码等全部跨 rank 信号，末尾用 `static_assert` 校验快照区不越界且 32B 对齐（L107-L112）。

设备侧消费这些信号的例子：元数据 0 号 worker 等所有源 rank 的 preSum（[dispatch_mega_combine.h:L176-L182](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L176-L182)）；Unpermute 协调器用一条 MTE 载入快照全部 rank 的专家进度再发布 phase-1 掩码（[dispatch_mega_combine.h:L242-L259](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h#L242-L259)）。窗口承载哪些缓冲、各放哪里，见 [README.md:L309-L328](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L309-L328) 的表。

#### 4.4.4 代码实践

1. **实践目标**：梳理「一个 token 的完整跨 rank 旅程」。
2. **操作步骤**：以 rank1 的 token t（被路由到 rank0 的专家 e）为线索，按顺序在源码中找到六次跨 rank/跨缓冲移动：① FrontReorder 量化记录写入本 rank 窗口；② Dispatch（rank0 的 AIV0）经 `RemotePtr` 从 rank1 窗口拉记录到本地 `gmA`；③ GMM1/SwiGLU/GMM2 本地计算；④ Combine 把结果写回 rank1 窗口的 `combineOutputByRouteSlot`；⑤ rank1 的 Unpermute 经 `expandedRowIdx` 读回该行；⑥ 乘 `probs` 累加进 `out`。
3. **需要观察的现象**：①⑤ 发生在「窗口 ↔ 工作区」边界，②④ 是真正的跨 rank RDMA 读写。
4. **预期结果**：一张六行表格，每行标注数据、发起阶段、访问原语（普通 GM 读写 or `RemotePtr` 翻译）与所需同步信号。
5. 源码阅读型实践；如需实测，两 rank 冒烟命令见 [README.md:L349-L355](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L349-L355)（需 A5 环境，MPICH 必需，OpenMPI 不受支持）——「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么每个信号槽要独占一个缓存行（stride=16 个 int32）？
**答案**：多 rank/多核并发原子更新相邻的 32 位信号时，若共享缓存行会互相「乒乓」失效（伪共享）；隔离到独立缓存行后各生产者互不干扰。这是 u6-l5「信号可见≠数据可见」之外另一条窗口信号工程守则。

**练习 2**：HCCL window 与 u6-l2 的 HCCL 窗口翻译有何异同？
**答案**：同：都把「远端地址」表示为「本端窗口基址 + 对称偏移」，靠 `windowIn[]` 表翻译。异：u6-l2 的 TGET 异步路径由 SDMA 引擎翻译窗口地址，host 侧用 HCCL 通信原语建窗；本算子在**设备代码**里直接以 `PtoRemoteWindow::RemotePtr` 做指针平移，配合普通 GM 读写与自定义 epoch 信号完成拉/推，通信完全内联在算子流水里。

### 4.5 tile 粒度调优：128×256 → 256×256 的取舍

#### 4.5.1 概念说明

提交 `0c9d93c9`（"tile 粒度 128*256 改为 256*256"）对本算子做了一组联动重构：GMM tile 从 128×256 升到 256×256；GMM1→SwiGLU 的 CV 槽改为「x/gate 各 128 KiB、共用一个 256 KiB UB slot、深度 1」；GMM1 的 x/gate **复用同一块 L0C 累加器**；M=2048 时 Group2 前 4 个 full-AIC wave 继续留在 GMM1。直觉上「更大的 tile」意味着更高的单块延迟与更大的缓冲，为什么反而更快？

关键在于三个乘积效应：

1. **算术强度**：256×256×K 的 Cube 块把 MTE 装载的 A/B 面板摊到 2 倍的计算量上，每字节带宽买到的 FLOPs 翻倍，流水更容易被 Cube 喂满；
2. **同步次数减半**：tile 数量减半 ⇒ CV 旗标握手、邮箱票据、ready 计数、依赖 atomicAdd 的**次数**全部减半——这些标量操作在 36 AIC 上是串行化点；
3. **L0C 复用消除双缓冲**：一个 256×256 float 累加器恰好占满 L0C；与其为 x/gate 各备一块（放不下）或降到 128 行（粒度损失），不如让两半**先后共用同一块**，用 FIX→M 事件串行化——「占用更少」与「同步更少」同时达成。

#### 4.5.2 核心流程

重构后一次 x/gate 成对计算的时间线（单 AIC 内三流水线协作）：

```text
M 流水（Cube）                FIX 流水                AIV1
─────────────                ────────                ──────
matmul x → L0C[0]
                              TMOV x  → UB[0..128KiB)
matmul gate → L0C[0]  ←———— FIX→M 事件：x 半块已离开 L0C
（同一块累加器！）
                              TMOV gate→ UB[128KiB..)
                              barrier(FIX); set ready
matmul 下一块 x …             （异步）                 wait ready → 消费
                                                      量化存 GM → set free
```

#### 4.5.3 源码精读

tile 粒度常量：`kMegaMoeGmmTileM = kMegaMoeGmmTileN = 256`（[dispatch_mega_combine_tiling.h:L24-L25](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine_tiling.h#L24-L25)），并贯穿 `GmmCommonPipeline = PtoGmmMxPreloadPipeline<256, 256, 256, 256, 256, 128, ...>` 的 L1/L0 各级缓冲形状（[gmm_common.h:L27](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_common.h#L27)）。CV 槽几何与编译期断言（半槽 128 KiB、整槽=256 KiB 全部 UB）在 [gmm1_swiglu_cv_pipe.h:L22-L53](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h#L22-L53)。

L0C 复用的核心在 `ComputePairDirect`：先算 x、TMOV 走 x，随后那句注释「A 256x256 accumulator fills L0C. Complete the x FIX transfer before reusing the same accumulator for gate」——用 `SetFlag/WaitFlag<PIPE_FIX, PIPE_M>(kCReuseEvent)` 把「x 半块已离开 L0C」变成 Cube 可复用累加器的许可，再算 gate（[pto_gmm_mx_preload.hpp:L107-L128](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/pto_gmm_mx_preload.hpp#L107-L128)）。等待位置本身也经过调优：`EnqueueDirectReserved` 特意把「环满等待」放在 Cube 指令已下发**之后**，避免标量等待阻塞下一条 Cube 命令（[pto_gmm_mx_preload.hpp:L166-L169](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/pto_gmm_mx_preload.hpp#L166-L169) 注释）。

UB 预算的账本：A5 UB 共 256 KiB，尾部 40 KiB 留作同步快照保留区，主区 216 KiB，全部用 `static_assert` 钉死（[const_args.hpp:L53-L65](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/utils/const_args.hpp#L53-L65)）。CV 整槽虽然「占满 256 KiB」，但 SwiGLU 的量化工作 tile 复用该槽已消费的区域（4.2.3），实际驻留峰值仍受 216 KiB 约束。

性能结论（提交信息内附，A5 实测）：M=512 提升 13.76%、M=1024 提升 16.99%、M=2048 提升 11.34%，小 M 也有 3.33%~6.26% 稳定收益。commit 全文可用 `git show 0c9d93c9` 查看。

#### 4.5.4 代码实践

1. **实践目标**：用 `git show` 复盘一次真实性能重构，训练「从 diff 读出设计动机」的能力。
2. **操作步骤**：
   ```bash
   git show 0c9d93c9 --stat                       # 看改动面
   git show 0c9d93c9 -- kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h
   git show 0c9d93c9 -- kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine_tiling.h
   git show 0c9d93c9 | grep -n "FifoDepth\|TileRows\|GmmTileM" | head -20
   ```
   逐条核对提交说明中的六项改动在 diff 里的落点。
3. **需要观察的现象**：`kGmm1SwigluCvTileRows` 128→256、`Endpoint` 拆成 `ProducerEndpoint/ConsumerEndpoint`（消费者不再需要 payload 索引——深度 1 的直接体现）、tiling 常量 256 化。
4. **预期结果**：写一段 200 字左右的分析，回答「为什么增大 tile 粒度反而减少同步等待与 L0C 占用」——要点应覆盖：握手/票据/计数次数随 tile 数减半；256×256 float 累加器恰好填满 L0C，x/gate 共用一块比两块 128×256 各占更省且免去二次分配同步；单槽深度 1 + UB 生命周期复用让 CV 驻留不超预算；算术强度上升使 Cube 流水掩盖同步开销。
5. diff 内容可直接本地验证；性能数字「待本地验证」（需 A5 环境复测）。

#### 4.5.5 小练习与答案

**练习 1**：GMM2→Combine 的槽只有 128 KiB（256×256 BF16），为什么 GMM1→SwiGLU 需要 256 KiB？
**答案**：GMM1 一次交接的是**成对**的 x 与 gate 两个 BF16 半 tile（SwiGLU 要同时读两者算 `silu(x)*gate`），2×128 KiB=256 KiB；GMM2 输出单块结果，128 KiB 足够（对比 [gmm1_swiglu_cv_pipe.h:L27-L30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm1_swiglu_cv_pipe.h#L27-L30) 与 [gmm2_combine_cv_pipe.h:L19-L22](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm2_combine_cv_pipe.h#L19-L22)）。

**练习 2**：如果强行把 FIFO 深度改成 2（双缓冲），最先撞上什么墙？
**答案**：UB。GMM1 槽已占满 256 KiB 全部 UB（`static_assert(kGmm1SwigluCvSlotBytes == AtlasA5::UB_SIZE)`），深度 2 需要再复制一整槽——物理上放不下；除非把 tile 缩回 128 行（即回到重构前的 128×256+深度 2 组合），那就重新引入 tile 数翻倍带来的同步开销。深度 1 + 256×256 是这对约束下的联合最优。

**练习 3**：M=2048 场景为什么让 Group2（初始 GMM2 组）在前 4 个 full-AIC wave 继续参与 GMM1？
**答案**：M 大时 GMM1 任务多、早期 GMM2 的输入（SwiGLU 输出）尚未就绪，Group2 若过早切换到 GMM2 只能空转等依赖计数；让它们多打 4 波 GMM1 把 GMM1 前浪推快，等 GMM2 门开时再切换，两端都不空转（提交说明与 [README.md:L119-L123](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L119-L123) 的 22:14 覆盖描述）。

## 5. 综合实践

**任务：绘制 MegaMoE 混合核的「核分工 × 流水 × FIFO」全景图，并复盘 0c9d93c9 重构。**

1. 准备：通读 [README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md) 与 [dispatch_mega_combine.h](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/dispatch_mega_combine.h)。
2. **图一（核分工图）**：以 36 AIC 拓扑为横轴，纵轴画三行（AIC、AIV0、AIV1），用色块标出每格参与的阶段；在 AIC 行上额外标出 GMM1 组（0..21）与初始 GMM2 组（22..35）的分界，以及 wave0 期间的全员 GMM1。
3. **图二（CV pipe FIFO 示意）**：画两条「AIC → AIV1」的通道（GMM1→SwiGLU、GMM2→Combine），每条标出槽大小、深度、ready/free 旗标号与控制流深度；在 GMM1 通道上把一个 slot 划成 x/gate 两半并标注 128 KiB 分界。
4. **分析**：`git show 0c9d93c9` 后写一段分析（≥200 字），论证「tile 128×256→256×256、X/Gate 共享 256 KiB UB slot、FIFO depth=1、GMM1 复用同一块 L0C 累加器」这组改动为什么能同时减少同步等待与 L0C 占用；再对照 [gmm_task_producer.h:L407-L521](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/op_kernel/gmm_task_producer.h#L407-L521) 总结 producer 的全部等待点（参考 4.3.4 的清单自查）。
5. 交付：两张图 + 一段分析。有 A5 环境的读者可加选：跑 [README.md:L349-L355](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L349-L355) 的两 rank 冒烟命令，读取 `[KERNEL_PERF]` 摘要（输出格式见 [README.md:L377-L382](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/dispatch_mega_combine/README.md#L377-L382)），把实测耗时与提交信息中的性能表对照；无环境则标注「待本地验证」。

## 6. 本讲小结

- **七段流水一份核分工**：FrontReorder→Dispatch→GMM1→SwiGLU→GMM2→Combine→Unpermute 全部内联在一个混合核 kernel 里；AIC 管 Cube，AIV0 分饰 Dispatch/元数据/producer，AIV1 是配对的 CV 消费者（SwiGLU/Combine），全员收官 Unpermute。
- **CV pipe = TMOV 数据面 + intra-block 旗标控制面**：GMM1→SwiGLU 以 256 KiB 单槽装 x/gate 成对半块（深度 1、控制流深度 2），GMM2→Combine 单 128 KiB 槽；「旗标后于数据」（pipe_barrier(FIX) 再 set ready）是正确性红线。
- **任务邮箱是设备侧 producer-consumer**：专职 AIV0 producer 用「连续就绪前缀 + 票据前缀和表 + P2C/C2P 双表 + 六状态车道状态机」动态派发 GMM 任务；P2C 整表一次 MTE3 刷回、C2P 一次载入看全部车道，producer 自身开销被压到最低；AIC 侧 GMM1→GMM2 靠票据区间无缝迁移，没有全局 barrier。
- **MPI 只管 bootstrap，HCCL window 管数据面**：root info 经 MPI_Bcast 分发；设备侧用 `RemotePtr` 基址平移 + 窗口尾部 1 MB 信号区（每槽独占缓存行）完成全部跨 rank 读写与 epoch 同步。
- **粒度即带宽、粒度即同步**：256×256 tile 让握手/票据/计数次数减半、算术强度翻倍；「x/gate 共用一块恰满 L0C 的累加器 + depth=1 单槽 + UB 生命周期复用」在 256 KiB UB 硬约束下同时拿到更低占用与更少等待，实测大 M 提升 11%~17%。
- **CPU 模拟器边界再次显形**：本算子是 `__DAV_CUBE__`/`__DAV_VEC__` 双编译的混合核，交叉核旗标、dcci/dsb、窗口信号都只在真机/sim 有意义——这类内核的正确性验证必须走 NPU 路径。

## 7. 下一步学习建议

- **收束单元六**：回看 u6-l4 的 host 侧 ready_queue 与本讲设备侧任务邮箱，写一份「生产者-消费者模式三梯队」笔记（GM 轮询队列 → 事件队列 → 票据邮箱）各自的适用粒度。
- **进入单元七**：本讲的 `dcci`+`dsb`+epoch、旗标先于/后于数据的纪律正是 u7-l2「内存一致性与生产者-消费者顺序」的素材，建议紧接着读 `docs/isa/memory-model/`。
- **性能方法论**：拿本讲的「握手次数 × 粒度」分析框架去读 u7-l3（`docs/coding/opt.md`），练习用 msprof 数据判别 CUBE/MTE/Vector bound。
- **源码延伸**：`op_kernel/gmm2.h`、`combine.h`、`unpermute.h` 本讲只带到了接口层，可作为自读材料；`utils/mega_wave_schedule.hpp` 里的 `GetExpertWaveRange`/`MegaMoeCoreTileBalancer` 是波次与负载均衡的另一半，值得精读。
- **贡献方向**：对照 u8-l2 的清单思考——若要给该算子新增一种调度模式（例如 producer 的就绪发现从轮询改为事件驱动），需要同步改动哪些文件与文档。
