# u5-l2 Kernel 并行机制：AIV/AIC 协同与 FlashDecode

## 1. 本讲目标

本讲是专家层第 5 单元的第二讲，聚焦「多核如何合跑一个算子」。学完后你应该能够：

1. 区分昇腾 AI Core 上 **Vector 核（AIV）** 与 **Cube 核（AIC）** 的能力差异，并说清 FIA Sink 算子如何按 1:2 混合启动、按「算子类型」在两类核之间分工（Mmad 归 Cube，softmax/规约/格式转换归 Vector）。
2. 读懂 **跨核同步原语** `CrossCoreSetFlag` / `CrossCoreWaitFlag`：flag 编号如何配对、信号为何挂在 PIPE_FIX/PIPE_MTE3 之后、一个任务的 mm1→vec1→mm2→vec2 四级乒乓如何在两类核间接力，以及 `ExecuteTask` 如何用三级软件流水把两类核的空转窗口藏起来。
3. 掌握 **FlashDecode** 思路：当 batch 与序列都很小（decode 场景）时，把 KV 序列切成多份并行计算部分 softmax，再由一组纯 Vector 核做跨分片在线规约合并成最终输出。
4. 对照 **MHC sandwich 算子的 dualcore 实现**：同样是「两颗核算一个任务」，它走的是另一条路线——两颗 AIV 按 head 对半分、经 GM workspace 交换部分和、用自旋 sense-reversing barrier 对齐，并以「冗余计算换零同步」——并能总结两种协同路线的适用场景与取舍。

## 2. 前置知识

本讲默认你已读完 u2-l4（AscendC Kernel 入门）与 u4-l1（FIA Sink 算子总览）。下面把几个本讲反复出现的概念用通俗语言再过一遍。

### 2.1 Cube 核（AIC）与 Vector 核（AIV）

昇腾 AI Core 里有两类计算核，能力互补：

| 核类型 | 擅长 | 典型指令 | 私有存储 |
| --- | --- | --- | --- |
| Cube 核（AIC） | 矩阵乘（MAC 阵列） | `Mmad`、`LoadData`、`Fixpipe` | L1（A1）、L0A/L0B/L0C |
| Vector 核（AIV） | 逐元素/规约/指数等向量运算 | `Exp`、`Mul`、`Add`、`Cast`、`ReduceSum` | UB（统一缓冲） |

一个物理核组里 Cube 与 Vector 的数量比由芯片决定，本仓库 FIA Sink 面向的形态是 **AIC:AIV = 1:2**（一个 Cube 配两个 Vector）。UB 与 L1/L0 都是**核私有**的——两类核之间想传数据，只能走共享的 **GM（Global Memory，含 workspace）**。

### 2.2 两类「同步」不要混淆

- **核内同步**：`SetFlag<HardEvent::X_Y>(id)` / `WaitFlag<HardEvent::X_Y>(id)`。同一颗核内两条流水线（如 MTE2 搬入与 V 计算）之间的事件，u2-l4 已讲过 TQue 的 Alloc/EnQue/DeQue/Free 就是它的封装。
- **跨核同步**：`CrossCoreSetFlag<mode, PIPE>(id)` / `CrossCoreWaitFlag(id)`。**不同核之间**的发令与等待，本讲主角。模板参数里的 PIPE（PIPE_FIX / PIPE_MTE3）表示信号与哪条「会写 GM 的流出流水线」绑定——信号在该流水线排空后才对外可见，从而保证对方核读到的数据已经落地到 GM。这是正确性的关键：先写数据、后发信号。
- **全核栅栏**：`SyncAll()`，所有参与核到齐才放行，用于阶段间的粗粒度对齐。

事件 ID（本讲见到的 2、4~11 等）是**核内资源**，Cube 与 Vector 各自独立编号互不冲突；跨核配对靠「同一个 flag 数值 + Set/Wait 成对出现」这一约定，由 kernel 代码自己维护。

### 2.3 软件流水（software pipelining）

若一个任务必须「AIC 算一步 → AIV 算一步 → AIC 再算一步」，最朴素的写法是每步都互相死等，两类核交替空转。改进办法是把多个任务在时间上错开：AIC 算任务 i 的矩阵乘时，AIV 还在处理任务 i-1、i-2 的收尾——用「任务间并行」填满「任务内串行」留下的空洞。FIA Sink 的 `ExecuteTask` 用一个深度为 3 的环形任务缓存实现了这一点。

### 2.4 在线 softmax（flash-decoding 的数学基础）

注意力输出需要对所有 KV 位置做 softmax。KV 太长时把序列切成多份并行，每份只能记录**局部**统计量（局部最大值 \(m_i\)、局部指数和 \(l_i\)）与局部输出，最后再合并。合并公式是 FlashAttention 在线 softmax 的跨分片版本，见 4.3.2 节。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L122-L168) | kernel 入口：声明 `KERNEL_TYPE_MIX_AIC_1_2` 混合核启动，按 TilingKey 实例化模板 |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L39-L249) | 编排层：持有三个 service，按 AIC/AIV 分派任务，实现软件流水 |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L30-L272) | Cube 服务：mm1/mm2 两次矩阵乘，L1/L0 乒乓，发出/等待跨核信号 |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_nonquant_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_nonquant_sink.h#L1185-L1199) | Vector 服务：softmax（vec1）与输出整理（vec2），与 Cube 侧信号配对 |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L35-L138) | FlashDecode 服务：纯 Vector 的跨分片规约 |
| [ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/kernel_common_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/kernel_common_sink.h#L23-L126) | 两类核**共享**的设备侧工具函数（序列长度解析、稀疏跳块判断） |
| [ascendc/src/ops-transformer/attention/common/op_kernel/fia_public_define.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/op_kernel/fia_public_define.h#L60-L149) | 公共定义：FIAType/FDparams/RunInfo/ConstInfo（含 FIA_SYNC_MODE2） |
| [ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L26-L318) | MHC kernel 类：Init/Process、核映射、同步区初始化、三路分发 |
| [ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L18-L202) | MHC 双核路径：按 head 对半分工、冗余计算、屏障交换 |
| [ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L16-L145) | MHC IO 原语：标量交换、sense-reversing barrier、partner x2 读取 |
| [ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h#L16-L218) | MHC 计算 building blocks：RMSNorm、MHC_Post、Gate（单/双核路径共用） |
| [ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_common.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_common.h#L23-L53) | MHC 常量与 workspace 布局（同步块偏移等） |

> 一个先澄清的事实：**`kernel_common_sink.h` 里并没有跨核同步调用**。它是被 Cube/Vector 两侧共同 include 的工具头（序列长度解析、稀疏跳块），真正出现 `CrossCoreSetFlag/CrossCoreWaitFlag` 的是 `fia_block_cube_nonquant_sink.h`、`fia_block_vec_nonquant_sink.h`、`fia_kernel_nonquant_sink.h` 三个文件。「共享头文件」与「同步所在文件」是两回事，这一点在 4.2 节用 grep 可以亲手验证。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：AIV/AIC 分工 → 跨核同步 → FlashDecode → MHC dualcore 对照。

### 4.1 AIV/AIC 分工：混合核启动与三类 Block 服务

#### 4.1.1 概念说明

注意力计算 = 两次大矩阵乘（\(QK^T\) 与 \(P \cdot V\)）夹一次 softmax。矩阵乘只有 Cube 核能高效完成，softmax/尺度变换/cast/布局整理是向量操作，Vector 核更合适。所以 FIA Sink 的做法是：

- 以 **AIC:AIV = 1:2** 的混合核形态启动一次 kernel；
- 每个核组内，1 个 Cube 核负责两次 `Mmad`，2 个 Vector 核负责 softmax 与输出整理；
- 中间结果（mm1 结果、softmax 结果、mm2 结果）放在 **GM workspace** 里，两类核通过它交换数据。

代码上这种分工被封装成三个「Block 服务」类，编排层（`FiaKernelNonQuant`）持有它们并按核类型调用：

- `FiaBlockCubeNonQuantGqa`（matmulService）——只在 AIC 上跑；
- `FiaBlockVecNonQuant`（vectorService）——只在 AIV 上跑；
- `FiaBlockVecFlashDecode`（fdService）——只在 AIV 上跑，FlashDecode 阶段专用。

#### 4.1.2 核心流程

一次 `FiaKernelNonQuant::Process()` 的执行流程（伪代码）：

```text
入口: KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)   # 声明 1:2 混合核
每个核:
    if 是 AIV: aiCoreIdx = blockIdx / 2        # 两个 Vector 共享一个 Cube 的编号
    else:      aiCoreIdx = blockIdx            # Cube 自己就是编号
    if 是 AIV: 先把输出区清零 (InitOutputSingleCore) 并 SyncAll
    FlashAttention():
        按 metadata 领到本核组的 [bN2Start..bN2End) 任务区间
        循环: 生成任务(CreateTask) → 执行任务(ExecuteTask, 软件流水)
    if 是 FlashDecode 场景且 fdFlag:
        FlashDecode(): SyncAll 后, 仅 AIV 参与跨分片规约
```

#### 4.1.3 源码精读

**（1）入口声明混合核。** kernel 入口在解包 tiling 之前先声明任务类型：

[ai_infra_fused_infer_attention_sink.cpp:L162-L168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L162-L168) 中 `TPipe tPipe`、`GetUserWorkspace(workspace)` 取出用户 workspace，最关键的一行是 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)`——告诉运行时本 kernel 要以「1 个 Cube 配 2 个 Vector」为单位调度核。入口随后按 `TILING_KEY_IS` 巨型分支把编译期场景（dtype/布局/PA/FD，u4-l1 已讲）落到模板实例化上；每个分支通过 [ai_infra_fused_infer_attention_sink.cpp:L72-L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L72-L78) 的宏把 `CubeBlockType / VecBlockType / FdBlockType` 三个模板参数组装成 kernel 对象——分工在类型系统里就已经定好。

**（2）blockIdx → 核角色的映射。**

[fia_kernel_nonquant_sink.h:L485-L491](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L485-L491)：AIV 分支 `tmpBlockIdx = GetBlockIdx(); aiCoreIdx = tmpBlockIdx / 2;`（注释 `vec:0-47`），AIC 分支 `aiCoreIdx = tmpBlockIdx;`（注释 `cube:0-23`）。也就是说 **两个 Vector 核映射到同一个「核组编号」aiCoreIdx**，任务按核组切分，组内两个 Vector 天然共享同一份任务区间与 workspace 槽位。宏 `ASCEND_IS_AIV` / `ASCEND_IS_AIC` 是编译期判定，使得同一份源码在两类核上编出两份不同指令流。

**（3）编排层持有的三个 service。**

[fia_kernel_nonquant_sink.h:L109-L111](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L109-L111) 声明了 `matmulService`（Cube）、`vectorService`（Vector）、`fdService`（Vector，FD）。[fia_kernel_nonquant_sink.h:L1276-L1299](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L1276-L1299) 的 `Process()` 中：AIV 只初始化 `vectorService` 的 buffer 与事件，AIC 只初始化 `matmulService` 的——**各自只碰自己那套存储**（Vector 是 UB，Cube 是 L1/L0），互不越界。

**（4）AIV 的额外杂务：清零输出。**

[fia_kernel_nonquant_sink.h:L339-L379](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L339-L379) 的 `InitOutputSingleCore` 只在 AIV 分支被调用（[fia_kernel_nonquant_sink.h:L514-L516](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L514-L516)）。它把输出张量与可选的 softmaxLse 均摊到所有核清零/填 `-inf`，注意 [fia_kernel_nonquant_sink.h:L350-L353](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L350-L353) 里除数是 `2 * usedCoreNum`，注释写明 `2 means c:v = 1:2`——按 Vector 核数摊任务。结尾 [fia_kernel_nonquant_sink.h:L377](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L377) 的 `SyncAll()` 保证清零全部落地后才开始正式计算。

**（5）Cube 侧的存储层级。**

[fia_block_cube_nonquant_sink.h:L219-L259](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L219-L259) 展示了 Cube 独有的三级私有存储：`qpBufL1/kvBufL1`（L1，A1 位置）、`tmpBufL0A/tmpBufL0B`（L0A/L0B，Mmad 的操作数）、`tmpBufL0C`（L0C，Mmad 结果）。[fia_block_cube_nonquant_sink.h:L505-L523](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L505-L523) 的 `InitBuffers` 对每块都 `* 2`——全部做成乒乓双缓冲，让「搬运下一块」与「计算当前块」重叠。计算主循环里，[fia_block_cube_nonquant_sink.h:L696-L737](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L696-L737) 用 `LoadData` 把 L1 数据搬进 L0A/L0B 后发 `Mmad`；[fia_block_cube_nonquant_sink.h:L743-L761](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L743-L761) 再用 `Fixpipe` 把 L0C 结果写回 GM workspace（K 维分块累计时用 `SetAtomicAdd` 保证多块结果正确叠加）。对比 Vector 侧只有一层 UB（见 [fia_block_vec_flashdecode_sink.h:L124-L137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L124-L137) 的 `TBuf` 列表），两类核的编程模型差异一目了然。

**（6）共享工具头。** [kernel_common_sink.h:L25-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/kernel_common_sink.h#L25-L77) 提供 `GetActualQSeqLength/GetActualKVSeqLength/SeqLenFromTensorList`（从设备侧张量读真实序列长度），[kernel_common_sink.h:L79-L124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/kernel_common_sink.h#L79-L124) 的 `IsSkipCal` 判断当前 S2 块是否落在稀疏 mask 的有效边界外（sink 张量永不跳过）。这些函数被两侧 include，是「分工」之外的「共享」维度。

#### 4.1.4 代码实践

**实践目标**：不运行代码，仅靠静态阅读把 op_kernel 目录下的文件按「入口 / 编排 / Cube 服务 / Vector 服务 / FD 服务 / 共享工具」归类，并验证「FdBlockType 恒为 FiaBlockVecFlashDecode」。

**操作步骤**：

1. 列出目录：`ls ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/`，共 12 个文件。
2. 统计入口的分发规模：`grep -c "TILING_KEY_IS" ai_infra_fused_infer_attention_sink.cpp`。
3. 查三类 Block 的接线：`grep -n "INVOKE_FIA_OP_GENERAL_IMPL\|FiaBlockVecFlashDecode" ai_infra_fused_infer_attention_sink.cpp | head -20`。
4. 确认哪些文件 include 了共享头：`grep -rn "kernel_common_sink.h" ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/`。

**需要观察的现象 / 预期结果**：

- 入口文件约 2300 行，绝大多数是 `TILING_KEY_IS` 分支（数量级上百）；
- 所有 `INVOKE_FIA_OP_GENERAL_IMPL` 的第 4 个模板参数（FdBlockType）都是 `FiaBlockVecFlashDecode`——FlashDecode 阶段永远是纯 Vector 实现；
- include `kernel_common_sink.h` 的至少有编排层 `fia_kernel_nonquant_sink.h`（[fia_kernel_nonquant_sink.h:L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L26)），Cube/Vector 服务经它间接使用共享函数。

本实践为纯源码阅读，无需硬件；grep 计数结果待本地验证（不同 grep 版本对宏折行计数略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `aiCoreIdx = tmpBlockIdx / 2` 只出现在 AIV 分支？如果写成 AIC 也除以 2 会怎样？

**答案**：1:2 混合核下，2 个 Vector 与 1 个 Cube 构成一个核组；任务区间（`bN2Start/End` 等）按**核组**从 metadata 读取，Cube 的 blockIdx 天然等于核组号，Vector 需要除以 2 折算到核组号，才能与搭档 Cube 领到同一份任务与 workspace 偏移。若 AIC 也除以 2，Cube 会读到错误核组的任务区间，与 Vector 的数据错位。

**练习 2**：`InitOutputSingleCore` 为什么交给 AIV 而不是 AIC？

**答案**：清零是纯向量搬出操作（`InitOutput` 走 MTE3/Vector 通路），AIV 擅长且此时 Cube 尚无任务可做；同时输出清零按 `2 * usedCoreNum` 摊到全部 Vector 核，用满 1:2 配比下的所有核，最后 `SyncAll()` 对齐。

**练习 3**：`matmulService.InitBuffers` 分配的是 UB 吗？

**答案**：不是。它用 `TBuf<TPosition::A1>`（L1）、`TBuf<TPosition::A2/B2/CO1>`（L0A/L0B/L0C）分配 Cube 专属存储；UB（VECCALC）是 Vector 侧 `vectorService/fdService` 的 `TBuf/TQue` 所用的位置。

### 4.2 跨核同步：CrossCoreSetFlag/WaitFlag 与任务内四级乒乓

#### 4.2.1 概念说明

一个注意力任务在核组内要经过四步：mm1（Cube）→ softmax（Vector）→ mm2（Cube）→ 输出整理（Vector）。每一步的输入都是上一步写在 GM workspace 里的结果，因此每一步开始前必须确认上一步**在另一类核上**已完成。`CrossCoreSetFlag/CrossCoreWaitFlag` 就是这对「发令/等令」原语：

- flag 用一个小整数编号，双方约定一致；Set 与 Wait 按任务成对出现；
- Set 的模板参数指定绑定的流水线（`PIPE_FIX` 是 Cube 侧 Fixpipe 写 GM 的通道，`PIPE_MTE3` 是 Vector 侧搬出通道），信号在对应流水线完成后才可见——**数据先落地，信号后发出**；
- mode 常量 `FIA_SYNC_MODE2 = 2` 定义在 [fia_public_define.h:L130-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/op_kernel/fia_public_define.h#L130-L132)（注释「CUBE与VEC核间同步的模式」），是这类跨核信号的硬件工作模式。

#### 4.2.2 核心流程

flag 编号在 [fia_kernel_nonquant_sink.h:L118-L124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L118-L124) 集中定义，再经 [fia_kernel_nonquant_sink.h:L295-L301](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L295-L301) 灌进 `constInfo` 传给两侧 service。一个任务 t 的四级乒乓：

```text
Cube  ComputeMm1(t)   ──写 mm1ResGm──▶ Set(C1V1, PIPE_FIX)
Vector ComputeVec1(t) ◀──Wait(C1V1)─── 读 mm1Res, 做softmax ──写 vec1ResGm──▶ Set(V1C2, PIPE_MTE3)
Cube  ComputeMm2(t)   ◀──Wait(V1C2)─── 读 vec1Res, Mmad·V ──写 mm2ResGm──▶ Set(C2V2, PIPE_FIX)
Vector ComputeVec2(t) ◀──Wait(C2V2)─── 读 mm2Res, 缩放/cast/排布 ──写 attentionOut ──▶ Set(V2C2, PIPE_MTE3)
```

四级信号的名字就是方向：`C1V1` = Cube 第 1 步 → Vector 第 1 步，以此类推。

在此之上，[fia_kernel_nonquant_sink.h:L1024-L1051](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L1024-L1051) 的 `ExecuteTask` 把多个任务在时间上错开成三级软件流水（`FIA_PRELOAD_TASK_CACHE_SIZE = 3` 的环形缓存）：

```text
第 loop 轮 ExecuteTask:
  AIC : Mm1(任务 i)          # 本轮新建任务
  AIV : Vec1(任务 i-2)        # 两轮前创建的任务
  AIC : Mm2(任务 i-2)         # 等 Vec1(i-2) 的 V1C2 信号
  AIV : Vec2(任务 i-1)        # 一轮前创建的任务
```

于是 AIC 永远比 AIV 「超前」约 2 个任务：AIC 算任务 i 的 QK^T 时，AIV 还在消化任务 i-1/i-2 的 softmax 与输出，两类核几乎不空转。**任务内是四级串行，任务间是深度 2~3 的流水**——这是「block 级流水」的含义。

#### 4.2.3 源码精读

**（1）Cube 侧：mm1 结束发 C1V1，mm2 开头等 V1C2。**

[fia_block_cube_nonquant_sink.h:L929-L995](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L929-L995) 的 `ComputeMm1` 按 `M_SPLIT_SIZE=256 / K_SPLIT_SIZE=512 / N_SPLIT_SIZE=128`（[fia_block_cube_nonquant_sink.h:L936-L938](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L936-L938)）三层循环切块调用 `DealMm1SingleMKN`，全部块写完 GM 后，最后一行 [fia_block_cube_nonquant_sink.h:L994](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L994) `CrossCoreSetFlag<ConstInfo::FIA_SYNC_MODE2, PIPE_FIX>(constInfo.syncC1V1);` 通知 Vector「mm1 数据已齐」。`ComputeMm2` 则在函数第一行 [fia_block_cube_nonquant_sink.h:L1004](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L1004) `CrossCoreWaitFlag(constInfo.syncV1C2);` 先等 Vector 的 softmax 结果，算完末尾 [fia_block_cube_nonquant_sink.h:L1036](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L1036) 发 `syncC2V2`。注意两次 Set 都挂 `PIPE_FIX`——Fixpipe 是 Cube 写 GM 的唯一通道，信号挂在其后即保证数据可见性。

**（2）Vector 侧：对称的 Wait/Set。**

[fia_block_vec_nonquant_sink.h:L1185-L1199](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_nonquant_sink.h#L1185-L1199)：`ComputeVec1` 是 `Wait(C1V1) → ProcessVec1SingleBuf → Set(V1C2, PIPE_MTE3)`，`ComputeVec2` 是 `Wait(C2V2) → ProcessVec2SingleBuf → Set(V2C2, PIPE_MTE3)`。与 Cube 侧逐 flag 严格镜像，Set 挂 `PIPE_MTE3`（Vector 写 GM 的搬出通道）。

**（3）首尾配平与退出保护。**

主循环 [fia_kernel_nonquant_sink.h:L1235-L1237](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L1235-L1237)：进入分发循环前 AIV 先 `Set(syncV2C2)`；循环结束后 [fia_kernel_nonquant_sink.h:L1272-L1274](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L1272-L1274) AIC `Wait(syncV2C2)`——AIC 必须等 AIV 把最后一个任务的 Vec2 做完才能收工，防止 kernel 提前结束导致流上还有未完成的写。入口的一次预置 Set 与出口的一次 Wait 恰好配平。

**（4）变体的额外信号。** GQA 版 Cube 服务里 [fia_block_cube_nonquant_gqa_sink.h:L1286](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_gqa_sink.h#L1286) / [L1299](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_gqa_sink.h#L1299) / [L1422](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_gqa_sink.h#L1422) / [L1437](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_gqa_sink.h#L1437) 是同一套 pattern 的 N 维双缓冲版本；MLA 版还引入 `C2V1`、`V1NupdateC2` 等编号（见 `fia_block_vec_nonquant_mla_sink.h` 的同名调用），服务于 rope 拆分等额外阶段。模式不变，编号按需扩展。

**（5）核内事件的对照。** Cube 服务内部的乒乓用的是**核内**事件：[fia_block_cube_nonquant_sink.h:L244-L259](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L244-L259) 定义 `MTE1_MTE2`（L1 搬入）、`M_MTE1`（L0 装载 vs Mmad）、`FIX_M`（Mmad vs Fixpipe）三组 EventID，[fia_block_cube_nonquant_sink.h:L525-L537](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_sink.h#L525-L537) 的 `AllocEventID` 用普通 `SetFlag` 预置。也就是说：**核内流水靠 HardEvent，核间接力靠 CrossCore**，两套机制在一个文件里同时出现，阅读时务必分清。

#### 4.2.4 代码实践

**实践目标**：亲手枚举 FIA Sink op_kernel 目录中全部跨核同步调用点，标注每处的方向（AIC→AIV 还是 AIV→AIC）与所在流水线。

**操作步骤**：

```bash
cd inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel
grep -n "CrossCore" *.h
# 再单独看公共定义里的 mode:
grep -n "FIA_SYNC_MODE2" ../../../common/op_kernel/fia_public_define.h
```

**需要观察的现象 / 预期结果**（以 `fia_kernel_nonquant_sink.h` / `fia_block_cube_nonquant_sink.h` / `fia_block_vec_nonquant_sink.h` 为准）：

| 文件 | 调用点 | 方向 | 绑定流水线 |
| --- | --- | --- | --- |
| fia_kernel_nonquant_sink.h:1236 | Set(V2C2) | AIV→AIC | PIPE_MTE3 |
| fia_kernel_nonquant_sink.h:1273 | Wait(V2C2) | AIC 等 AIV | — |
| fia_block_cube_nonquant_sink.h:994 | Set(C1V1) | AIC→AIV | PIPE_FIX |
| fia_block_cube_nonquant_sink.h:1004 | Wait(V1C2) | AIC 等 AIV | — |
| fia_block_cube_nonquant_sink.h:1036 | Set(C2V2) | AIC→AIV | PIPE_FIX |
| fia_block_vec_nonquant_sink.h:1188/1190 | Wait(C1V1) / Set(V1C2) | AIV 侧 | PIPE_MTE3（Set） |
| fia_block_vec_nonquant_sink.h:1196/1198 | Wait(C2V2) / Set(V2C2) | AIV 侧 | PIPE_MTE3（Set） |

`kernel_common_sink.h` 在这次 grep 中**零命中**——验证第 3 节的澄清。grep 输出条数待本地验证（还应包含 gqa/mla 变体文件中的调用）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ComputeMm1` 末尾的 `CrossCoreSetFlag` 从 `PIPE_FIX` 改挂到别的流水线，最可能出什么问题？

**答案**：Fixpipe 是 Cube 把 L0C 结果写进 GM workspace 的通道。若信号不再等到 FIX 完成，Vector 可能在 mm1 结果尚未全部落地时就开始读 `mm1ResGm`，读到旧值/脏值，softmax 结果错误。「先数据后信号」的绑定关系是正确性前提。

**练习 2**：软件流水里，为什么 AIV 处理的是「两轮前」的任务（extraInfo2）而不是「上一轮」的？

**答案**：`ExecuteTask` 的安排是 AIC 先 `Mm1(任务 i)`、AIV `Vec1(任务 i-2)`、AIC `Mm2(任务 i-2)`、AIV `Vec2(任务 i-1)`。Vec1 必须等同一任务的 Mm1 完成（C1V1），Mm2 必须等同一任务的 Vec1 完成（V1C2）；把 Vec1/Mm2 对齐到 i-2、Vec2 对齐到 i-1，恰好让每类核在每轮都有活干，两类核之间保持约 2 个任务的相位差，掩盖各自的执行延迟。这是深度为 2~3 的错相流水。

**练习 3**：跨核 flag 编号（如 6~9）与 Vector 服务内部的 SetFlag 编号（如 `SYNC_LSE_SUM_BUF1_FLAG=6`）数值相同，会冲突吗？

**答案**：不会。事件 ID 是**每颗核各自**的资源：Vector 核内的 `SetFlag/WaitFlag` 只作用于本核流水线；跨核信号经 `CrossCoreSetFlag` 走核间通道，由「编号 + Set/Wait 配对」约定区分。两类编号空间互不干扰（但同一条核内的编号仍需避免冲突，所以 Cube 侧核内事件特意用了 EVENT_ID2~5 段）。

### 4.3 FlashDecode：向量核并行分解注意力

#### 4.3.1 概念说明

decode 场景（每 batch 只有 1 个 query token）里，任务数 = batch × head 数，往往远少于核数，大量核闲置；而每个任务的 KV 序列却很长。**FlashDecode** 的思路是把「核间并行维度」从「任务数」切换到「KV 序列切分」：

1. **计算阶段**：多个核组各自算一段 KV 的部分注意力，产出三样东西写到 workspace：部分输出 \(O_i\)（`accumOutGm`）、局部最大值 \(m_i\)（`lseMaxFdGm`）、局部指数和 \(l_i\)（`lseSumFdGm`）；
2. **规约阶段**：另一批纯 Vector 核把所有分片的结果按在线 softmax 公式合并成最终输出。

本算子是否走 FlashDecode 由 host/AICPU metadata 决定：[fia_kernel_nonquant_sink.h:L502](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L502) 从 metadata 读 `NUM_OF_FD_INDEX` 得到 `fdFlag`；分片方案与参与规约的核数（`usedVecNumOfFd`）正是 u4-l2 讲过的 AICPU metadata 算子算好写进 1024 元素 metadata 张量的。

#### 4.3.2 核心流程

设第 i 个 KV 分片记录了局部最大值 \(m_i\)、局部指数和 \(l_i=\sum_{j\in i} e^{s_j-m_i}\) 以及分片内已归一化的输出 \(O_i\)，全局最大值 \(m^*=\max_i m_i\)，则：

\[ O \;=\; \frac{\displaystyle\sum_i l_i\, e^{m_i-m^*}\, O_i}{\displaystyle\sum_i l_i\, e^{m_i-m^*}} \]

直觉：每个分片对自己那段 softmax 得到 \(O_i\) 与「段内权重总量」\(l_i e^{m_i-m^*}\)，全局 softmax 就是把这些段按权重重新配比再归一化。`ComputeScaleValue` 算的正是每片的系数 \(w_i / \sum_j w_j\)，其中 \(w_i = l_i e^{m_i-m^*}\)；`ReduceFinalRes` 用该系数加权累加 \(O_i\)。

规约阶段的执行流程（`FiaBlockVecFlashDecode::FlashDecode`）：

```text
仅 AIV 参与 (KERNEL_TYPE_MIX_AIC_1_2 下 Vector 核)
SyncAll 对齐 → 每个 Vector 核按 metadata 领 [fdTaskPrevEnd, fdTaskEnd] 个规约任务
for 每个规约任务 (b,n2,gS1 组合):
    for 该任务内的 M 分块 (fdS1gOuterMIdx):
        CopyLseIn:        读入该 (b,n2) 的所有分片 m_i / l_i   (乒乓双缓冲)
        ComputeScaleValue: ColMax→RowSub→Exp→Mul→ColAdd→Div 得每片系数
        预取 preLoadNum 份 O_i 到 fdMm2ResBuf1/2           (乒乓双缓冲)
        for 每个分片 i:
            ReduceFinalRes:  reduceOut += O_i × 系数_i
        CopyFinalResOut:  cast 到 OUT_T 后按输出布局写 attentionOut
```

#### 4.3.3 源码精读

**（1）触发与参数装配。**

[fia_kernel_nonquant_sink.h:L831-L883](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L831-L883)：`FlashDecode()` 先 `fdService.InitBuffers(pipe)`、`ICachePreLoad`（指令缓存预取），然后 `SyncAll()` 等所有核（包括刚干完活的 Cube）到齐，接着**整个函数体包在 `if ASCEND_IS_AIV` 里**——Cube 核到此只参与 SyncAll，不再干活。AIV 从 metadata 的 AIC/AIV 两个区读出规约任务表：每核的规约任务边界 `gS1IdxEndOfFdHead`（AIV 区，[fia_kernel_nonquant_sink.h:L860-L867](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L860-L867)）、每个任务的 bN2/gS1/分片数（AIC 区），组装成 [fia_public_define.h:L76-L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/op_kernel/fia_public_define.h#L76-L86) 定义的 `FDparams` 结构（含 `usedVecNumOfFd` 与 `gS1BaseSizeOfFd`），交 `fdService.FlashDecode(fdParams)`。

**（2）领任务：核号切分。**

[fia_block_vec_flashdecode_sink.h:L483-L497](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L483-L497)：`FlashDecode` 第一件事 `if (blockIdx >= fd.usedVecNumOfFd) return;`——只有 metadata 分配的 Vector 核才继续。随后用 `fd.gS1IdxEndOfFdHead[blockIdx-1]`（上一核结束在第几个规约任务）与 `fd.gS1IdxEndOfFdHeadSplit`（该任务内第几个 M 分块）算出本核的**左闭右闭**任务区间，保证同一规约任务的 M 分块不会被两核争抢也不会遗漏。[fia_block_vec_flashdecode_sink.h:L498-L512](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L498-L512) 还逐任务累计 `combineTaskPrefixSum` 求出各分片结果在 workspace 里的全局位次，注释画出了 `|Task0-0|Task0-1|Task1-0|...` 的平铺布局。

**（3）在线 softmax 系数。**

[fia_block_vec_flashdecode_sink.h:L252-L277](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L252-L277) 的 `CopyLseIn` 把该 (b,n2) 的全部分片 \(m_i/l_i\) 读进 UB，双缓冲按 `cntM % 2` 选 `fdSumBuf1/2`、`fdMaxBuf1/2`，用 `V_MTE2 / MTE2_V` 核内事件保证搬运与计算重叠。[fia_block_vec_flashdecode_sink.h:L279-L316](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L279-L316) 的 `ComputeScaleValue` 依次：`ColMax`（求全局 \(m^*\)，[L295-L296](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L295-L296)）→ `RowSub` + `Exp`（得 \(e^{m_i-m^*}\)，[L299-L303](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L299-L303)）→ `Mul`（\(w_i = l_i e^{m_i-m^*}\)）→ `ColAdd`（\(\sum w_i\)）→ `MatDivsVec`（系数 \(w_i/\sum w_j\)）。每步之间 `PipeBarrier<PIPE_V>()` 排空向量流水线，避免读写冒险。

**（4）加权累加与写出。**

[L528-L533](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L528-L533) 先按 `constInfo.preLoadNum` 预取前两片 \(O_i\)；[L605-L617](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L605-L617) 主循环里边算边补搬：`CopyAccumOutIn`（[fia_block_vec_flashdecode_sink.h:L229-L250](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L229-L250)，`DataCopyPad` 处理 headDim 非 8 对齐尾巴）读 \(O_i\)，`ReduceFinalRes`（[fia_block_vec_flashdecode_sink.h:L414-L430](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L414-L430)）做 `RowMuls` + `Add` 累加（第 0 片直接写 reduceOut 省一次 Add）。最后 [L618](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L618) 调 `CopyFinalResOut`（[fia_block_vec_flashdecode_sink.h:L432-L451](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L432-L451)）：fp32 按 `CAST_RINT`（bf16 四舍六入五成双）/`CAST_ROUND`（fp16）cast 成输出 dtype，再由 `Bmm2DataCopyOutTrans` 按 BSH/BNSD/NBSD/TND/NTD 五种输出布局（[fia_block_vec_flashdecode_sink.h:L318-L396](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L318-L396)）写回 `attentionOutGm`。若 `softmaxLseFlag` 打开，[L538-L599](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L538-L599) 还会用 `ComputeSoftMaxLse`（\(lse=\log l + m^*\)）算出全局 LSE 并按布局写出，无效行用 `AdjustSoftMaxRes` 填大数。

**（5）纯 Vector 的物证。** `InitBuffers` 只在 [fia_block_vec_flashdecode_sink.h:L178-L179](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L178-L179) 的 `if ASCEND_IS_AIV` 里分配 UB（并 `pipe->Reset()` 重置之前 vectorService 用过的管道）；核内同步 [fia_block_vec_flashdecode_sink.h:L205-L227](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_flashdecode_sink.h#L205-L227) 全是普通 `SetFlag/WaitFlag`（`V_MTE2`、`MTE3_V`），一个 CrossCore 都没有——规约阶段核间无数据依赖，各核独立处理自己的 (b,n2,gS1) 组合。

#### 4.3.4 代码实践

**实践目标**：用纸笔验证在线 softmax 规约公式的正确性，体会 `ComputeScaleValue` 每一步在算什么。

**操作步骤**：

1. 构造最小数值例子（示例题目，非项目代码）：两个 KV 分片，各只有 1 个 token；分片 1 的 score \(s_1=2\)，对应 \(V\) 向量 \(v_1\)；分片 2 的 score \(s_2=3\)，对应 \(v_2\)。则 \(m_1=2, l_1=1, O_1=v_1\)；\(m_2=3, l_2=1, O_2=v_2\)。
2. 手算：\(m^*=3\)，\(w_1 = 1\cdot e^{2-3} = 0.3679\)，\(w_2 = 1\cdot e^{3-3} = 1\)。
3. 代入公式：\(O = (0.3679\,v_1 + 1\,v_2)/(0.3679+1) = 0.2689\,v_1 + 0.7311\,v_2\)。
4. 对照直接对两个 score 做 softmax：\(\mathrm{softmax}(2,3) = (0.2689, 0.7311)\)——两法一致。

**需要观察的现象 / 预期结果**：分片合并结果与整段 softmax 完全一致，说明「局部 max/sum + 重配比」的分解无损。再对照源码：步骤 2 的 \(w_i\) 对应 `RowSub→Exp→Mul`，分母 \(\sum w_i\) 对应 `ColAdd`，配比对应 `MatDivsVec`，加权求和对应 `ReduceFinalRes`。

本实践为纸笔推演；若想上机验证完整算子，可参照 `tests/st/test_npu_fused_infer_attention_sink.py` 构造小规模 decode 输入（待本地验证，需昇腾环境）。

#### 4.3.5 小练习与答案

**练习 1**：FlashDecode 规约阶段为什么完全不需要 CrossCore 同步？

**答案**：计算阶段结束时，各分片的 \(O_i/m_i/l_i\) 已全部写入 GM workspace（那里有 `SyncAll` 与上一阶段的信号兜底）；规约任务按 (b,n2,gS1) 粒度整块划分给各 Vector 核，任务之间不共享任何中间数据、输出区间互不重叠，各核只需核内 MTE2/V/MTE3 流水同步即可。

**练习 2**：`usedVecNumOfFd` 与 `gS1IdxEndOfFdHead` 为什么要在 kernel 里从 metadata 现读，而不是 host 下发 tiling 时直接给常数？

**答案**：分片与分核方案依赖 `actual_seq_lengths_kv` 等**设备张量的数值**，host 侧 tiling 读不到（u4-l2 讲过），因此由 AICPU metadata 算子在设备上算好写进 metadata 张量；kernel 侧（[fia_kernel_nonquant_sink.h:L843-L867](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L843-L867)）再从 metadata 的 AIC/AIV 区把这张「分核表」搬进局部数组使用。

**练习 3**：`ReduceFinalRes` 中 `cntKV == 0` 时把 `RowMuls` 结果直接写 `reduceOut`、否则写 `mm2Res` 再 `Add`，为什么？

**答案**：第 0 片的加权结果就是累加器的初值，直接落位可省掉一次对全零缓冲的 `Add`；后续片先在 `mm2Res`（乒乓搬运缓冲）原地乘系数，再累加到 `reduceOut`，避免「读写同一缓冲」的流水冒险。

### 4.4 对照组：MHC sandwich 的 dualcore 双核协同

#### 4.4.1 概念说明

MHC sandwich 算子（u4-l6 精读过）是纯向量算子，入口声明 [KERNEL_TASK_TYPE_AIV_ONLY](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L25-L45)。当 token 数少于核数一半时，host tiling 把 `coresPerToken` 置 2：**两颗 Vector 核合算一个 token**，按 head 维对半分工。它与 FIA 的协同思路完全不同：

- FIA 按**算子类型**分工（Cube 算乘、Vector 算 softmax），数据在两类核间单向流动，同步用硬件 CrossCore 信号；
- MHC 按**数据维度**分工（每核算 N/2 个 head），但 RMSNorm、gate 等步骤需要**全 N 个 head 的全局统计量**（如 sumSq），于是两核必须交换部分和——同步改为「GM workspace + 自旋 sense-reversing barrier」，并用「冗余重算对方那份」来减少同步次数。

#### 4.4.2 核心流程

一个 token 的 `ProcessTokenDualCore` 五阶段（以 `coreRole_` 0/1 区分搭档）：

```text
Phase A  RMSNorm_0:           两核各自全量算 (冗余), 顺手异步预取 residual
Phase B  MHC_Post:            各算自己的 N/2 个 head → x2
Phase B' RMSNorm_mid(可选):   需要 N*D 全局 sumSq → 各自冗余重算搭档 head 的 x2 求平方和 (免同步)
Phase C  sumSqPre + phi matmul: 各算自己 head 的部分和 mySumSqPre、myXHat[]
─── 屏障 1 ─── DualCoreSyncExchange: 把 mySumSqPre/myXHat 写进 workspace 自己的槽位
              WriteX2ToWorkspace:   把自己 N/2 个 head 的 x2 写进 workspace
              SyncAndReadPartner:   sense-reversing barrier 对齐后读搭档的标量并求和
ComputeGateValues:             用全局 sumSq 与全部 xHat 算 gate[N] (两核各自算, 结果一致)
Phase D  h_in:                各算自己 head 的 gate·x2; 逐 head ReadPartnerX2Tile 读搭档 x2 累加
─── (输出阶段不需要第二次屏障: x2 workspace 已在屏障 1 前写好) ───
Phase E  RMSNorm_1:           各算各的; 仅 coreRole_==0 写出 h_in_prime
```

sense-reversing barrier 的轮次交替：本轮期待值 `syncVal = (syncRound_ & 1) ? 0 : 1`，写自己的槽、自旋读搭档的槽直到等于本轮值，随后 `syncRound_++`。因为每轮期待值与上一轮写入值相反，**不需要任何清零阶段**。

#### 4.4.3 源码精读

**（1）核映射。**

[ai_infra_mhc_sandwich_norm_post_preonly_kernel.h:L74-L91](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L74-L91)：`coreRole_ = blockIdx % DUAL_CORE_COUNT`（`DUAL_CORE_COUNT=2`，[common.h:L28](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_common.h#L28)），`pairIdx = blockIdx / 2`，`headStart_ = coreRole_ * myHeadCount_`——**物理上相邻的两颗核结成一个 pair**，role 0 管 head [0, N/2)、role 1 管 [N/2, N)。token 区间按 pairIdx 划分（blockPivot 处理不整除）。

**（2）同步区的准备。**

[ai_infra_mhc_sandwich_norm_post_preonly_kernel.h:L151-L160](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L151-L160)：workspace 里的同步区由 host tiling 计算的 `syncGmOffsetFloats_` 定位（注释说明 host 按 `coreNumAiv * wsPerUnit_MAX` 预留，kernel 不会写进数据区中部），每核一个 `WS_SYNC_BLOCK=8` 个 int32 的槽（32B DMA 对齐，布局见 [common.h:L38-L53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_common.h#L38-L53)：`[0..7]` 是 core0 的 sync1 块、`[8..15]` 是 core1 的，之后是两核的 x2 交换区）。[ai_infra_mhc_sandwich_norm_post_preonly_kernel.h:L177-L187](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L177-L187) 在 Process 开头把自己的槽清零，为 sense-reversing 初态做准备。

**（3）屏障本体。**

[ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h:L57-L95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L57-L95) 的 `SyncBarrier`：先 `syncVal = (syncRound_ & 1) ? 0 : 1`（[L66](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L66)），把 syncVal 经 UB `DataCopy` 写进 **自己的** 槽（只写自己的，注释说明这是为了消除跨核 GM 写、规避 mssanitizer 越界问题），然后在 [L85-L91](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L85-L91) 的 `while(true)` 里反复 `DataCopy` 读**搭档的**槽，直到等于本轮 syncVal 才跳出——源码注释（L81-84）把三步讲得很清楚。最后 `syncRound_++` 翻转极性。

**（4）数据交换。**

[ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h:L20-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L20-L43) 的 `DualCoreSyncExchange` 把 `mySumSqPre + myXHat[]` 打包写进自己槽位；[L98-L119](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L98-L119) 的 `SyncAndReadPartner` 先过屏障再读搭档槽位，把两边部分和相加得全局量。[ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h:L135-L145](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L135-L145) 的 `ReadPartnerX2Tile` 在 Phase D 逐 head 读搭档的 x2。调用点在 [ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h:L161-L166](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L161-L166)。

**（5）冗余计算换零同步。**

[ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h:L28-L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L28-L42)：Phase A 的 RMSNorm_0 注释明写 `redundant on both cores`——归一化统计量两核各算一遍，免一次交换；[L66-L91](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L66-L91)：可选 RMSNorm_mid 需要全部 N 个 head 的 sumSq，两核宁可**重算搭档 head 的 x2**（Muls/Axpy 一遍再做 QuantizeRoundTrip）也不插一次屏障——注释 `redundant computation, no sync needed`。真正无法绕开的只有 Phase C 之后那一次交换（gate 依赖全局统计量）。最后 [L198-L201](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L198-L201)：Phase E 仅 `coreRole_ == 0` 的核写出 `h_in_prime`，避免双写。计算积木（`ReduceSumPrecise`/`ComputeX2MyHeads`/`ComputeGateValues` 等，[ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h:L82-L218](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h#L82-L218)）被单核/双核/多 tile 三条路径共用，是「一份逻辑、多种并行策略」的典型。

#### 4.4.4 代码实践

**实践目标**：验证 sense-reversing（极性翻转）设计的必要性——如果每轮都写固定值 1 会发生什么。

**操作步骤**（纸笔推演，示例代码为伪代码）：

1. 假设屏障改成「每轮双方都写 1、等读到 1」：第 1 轮双方写 1、互相读到 1、通过，此时两槽都停留在 1。
2. 推演第 2 轮：核 A 率先到达屏障，读搭档槽——**上一轮残留的 1 立即满足条件**，未等搭档真正抵达就放行。
3. 对照源码 [ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h:L64-L95](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L64-L95)：第 2 轮 `syncRound_` 已翻为奇数，期待值变 0，而槽里是 1——必须等搭档本轮真正写入 0 才通过。

**需要观察的现象 / 预期结果**：固定值版本在第 2 轮出现「提前放行」（旧值误判），产生读写到一半数据的竞态；翻转极性后旧值永远不等于本轮期待值，无需清零即可区分轮次。这正是「sense-reversing barrier」名字的由来。

#### 4.4.5 小练习与答案

**练习 1**：MHC dualcore 为什么把相邻两核配成一个 pair（`pairIdx = blockIdx / 2`），而不是让 role0 全在前半、role1 全在后半？

**答案**：相邻核通常共享更近的 L2/NoC 邻域，pair 内经 GM workspace 交换 x2 与标量的往返延迟更小；同时 `blockIdx % 2` 取 role 让 pair 的分布与 token 划分解耦，tiling 只需按 pair 数切 token。这属于实现选择，注释未展开论证（动机待确认），但「物理相邻配对」是多核向量算子的常见做法。

**练习 2**：FIA 的跨核同步走硬件 CrossCore 信号，MHC 为什么改用 GM+自旋屏障？

**答案**：CrossCore 原语服务于「Cube 与 Vector 两类核之间」的硬件信号通道；MHC 是 AIV_ONLY，两颗同类型 Vector 核之间没有现成的信号配对机制可用，于是用最朴素的办法：把标志位放 GM，一方写、另一方循环读。代价是自旋占计算资源，所以设计上用冗余计算把每 token 的屏障压到只剩一次。

**练习 3**：既然两核各自算出的 gate 完全一致，为什么不让 role0 算完广播给 role1，省一份计算？

**答案**：广播需要一次「算完→屏障→读」的往返，比冗余计算（各自用已就位的全局 sumSq 与 xHat 直接算，输入在屏障后已齐）更慢；且 gate 计算量很小（N≤4 个 sigmoid），冗余成本远低于一次 GM 往返。这是「以冗余计算换跨核同步」哲学的又一处体现。

## 5. 综合实践

把本讲三个机制串成一份「双核协同调研笔记」，产出三样东西：

**任务 A：FIA 一轮迭代的跨核时序图。** 基于 4.2 的四级乒乓与软件流水，画出 AIC 与 AIV 两条时间轴上 **连续 3 个任务（T0/T1/T2）** 的时序图（文本画法即可）：

```text
AIC : [Mm1(T0)]──Set C1V1──[Mm1(T1)]──Set C1V1──[Mm2(T0)]←Wait V1C2──[Mm1(T2)]...
AIV : ...←Wait C1V1──[Vec1(T0)]──Set V1C2──...←Wait C2V2──[Vec2(T0)]──Set V2C2──[Vec1(T1)]...
GM  : mm1Res(T0)──────vec1Res(T0)────────mm2Res(T0)────────attentionOut(T0)→
```

要求：标注每个 Set/Wait 的 flag 名与绑定流水线（C1V1/C2V2 挂 PIPE_FIX，V1C2/V2C2 挂 PIPE_MTE3），并用 4.2.4 的 grep 命令核对图中每一处信号都能在源码中找到对应行号。

**任务 B：MHC 一轮 token 的双核时序图。** 画出 role0/role1 两条轴：各跑 Phase A（冗余）→ B → B'（冗余重算搭档 sumSq）→ C →【写自己的 sync1 槽与 x2 区】→ SyncBarrier（写己方槽/自旋读对方槽）→ 读对方标量求和 → ComputeGate → D（逐 head 读对方 x2）→ E（仅 role0 写出），在屏障处用竖线对齐两轴，标出「冗余」与「交换」两类动作。

**任务 C：异同对比表。** 至少覆盖：核组合（AIC+AIV 1:2 vs AIV+AIV）、启动声明（`KERNEL_TYPE_MIX_AIC_1_2` vs `KERNEL_TYPE_AIV_ONLY`，两处入口源码各给一行链接）、分工依据、同步原语、通信介质、数据流向（单向流水 vs 双向交换）、冗余策略、写出者、适用场景（计算密集的大矩阵乘 vs token 稀少时的负载均衡）。

**预期结果**：两张图能互相解释「为什么 FIA 选流水、MHC 选交换」——FIA 每步数据量大且步骤天然异构，适合按算子分工流水；MHC 每步计算小但需要全局统计量，适合按数据对半分+一次交换。全部结论均有源码行号支撑；时序图为静态分析结果，波形级行为待本地验证（需昇腾环境与 profiling 工具）。

## 6. 本讲小结

- **分工**：FIA Sink 以 `KERNEL_TYPE_MIX_AIC_1_2` 混合启动，AIV 用 `blockIdx/2` 折算到核组号与 Cube 结对；Cube 侧 mm1/mm2 走 L1→L0A/L0B→Mmad→L0C→Fixpipe 的私有存储层级，Vector 侧 softmax/规约/cast 全在 UB；分工封装为 matmulService/vectorService/fdService 三个 Block 类。
- **跨核同步**：一个任务四级乒乓 `Mm1→Set C1V1→Vec1→Set V1C2→Mm2→Set C2V2→Vec2→Set V2C2`，Set 挂在写 GM 的流出流水线（PIPE_FIX/PIPE_MTE3）之后保证数据先落地；`ExecuteTask` 用深度 3 的任务环形缓存把两类核错相约 2 个任务，实现 block 级软件流水；`kernel_common_sink.h` 是共享工具头而非同步所在。
- **FlashDecode**：decode 场景把并行维度从任务数切到 KV 序列，计算阶段各核产出 \(O_i/m_i/l_i\) 三件套，规约阶段纯 AIV 按 metadata 分核表领任务，用在线 softmax 公式 \(O=\sum_i l_i e^{m_i-m^*}O_i / \sum_i l_i e^{m_i-m^*}\) 加权合并，全程无跨核信号。
- **MHC dualcore 对照**：两颗 AIV 按 head 对半、经 GM workspace 交换部分和、sense-reversing 自旋屏障（极性逐轮翻转免清零）对齐，并以「冗余重算搭档份额」把每 token 的同步压到一次；与 FIA 代表了「数据流水」与「数据交换」两种多核协同范式。

## 7. 下一步学习建议

- 下一讲 **u5-l3（Fallback 回退与统一错误处理）**：转向容错面——公共 fallback 框架与 ops_err/ops_log 错误码体系。
- 想继续深挖本讲主题，推荐三条阅读线：
  1. `fia_block_cube_nonquant_gqa_sink.h` 与 `fia_block_vec_nonquant_mla_sink.h`——同一套四级乒乓在 GQA/MLA 变体上如何扩展出 `C2V1`、`V1NupdateC2` 等额外信号；
  2. `ai_infra_mhc_sandwich_norm_post_preonly_dualcore_mt.h`——D=5120 时双核+多 tile 的组合路径，看 workspace 布局如何随之变化；
  3. `ai_infra_quant_lightning_indexer_kernel.h`——另一个 `KERNEL_TYPE_MIX_AIC_1_2` 混合核算子（grep 可见其成对的 CrossCore 调用），可作为独立练习检验本讲方法论。
- 结合 u6-l1 的 UT 框架回看本讲：tiling 的分核参数（如 `coresPerToken`、`usedVecNumOfFd` 的来源 split_core.cpp）可以在纯 CPU 上用 faker 验证，这是把「多核协同」拆成可测试单元的工程手段。
