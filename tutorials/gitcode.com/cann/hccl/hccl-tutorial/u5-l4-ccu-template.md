# u5-l4 CCU 模板与硬化通信单元

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CCU（Collective Communication Unit）作为 IO Die 上专用集合通信协处理器的定位：高带宽、低时延、少占计算核，但受片上资源限制。
2. 理解 CCU 路径的「软件组指令、硬件搬数据」模型：模板的 `KernelRun` 只负责 launch 一个 CCU Kernel，kernel 体通过 `ccu::*` 原语把 Read/Write/LocalCopy/LocalReduce **录制**成 CCU 指令流，真正搬数据的是 CCU 硬件经 URMA 执行指令流。
3. 掌握 `CcuAlgTemplateBase`、`ccu_kernel_utils`、`ccu_kernel_alg_base` 三层代码的组织方式，以及 `Loop`/`LoopGroup` 如何用「循环体录制 + 运行期变量」表达海量重复搬移。
4. 精读 `CcuTempAllReduceMesh1D`（ReduceScatter + AllGather 两阶段）模板与它的 CCU kernel 体，并了解本轮演进为全部 CCU 模板新增的 `CalcCostCoeff` 代价标定。

## 2. 前置知识

- **模板三级生命周期**（u3-l5）：每个算法模板都要实现 `CalcRes`（host 侧算资源：kernel 数、channel、notify）与 `KernelRun`（下发执行），外加静态 `CalcCostCoeff`（给新选择器申报 A/B/C 代价系数）。
- **CCU 引擎**（u1-l2、u2-l4）：三大通信引擎之一。AICPU_TS 用 Task 描述符下发、不占计算核；AIV 占 Vector 核但延迟低；CCU 则是**专用硬化加速单元**，Thread 在 CCU 语境下抽象为 **Mission**。
- **URMA**（Unified Remote Memory Access，统一远端内存访问）：CCU 执行指令流时使用的远端内存访问机制，等价于「CCU 硬件替你做的 RDMA」。
- **dlsym 解耦**（u6 系列）：所有 `ccu::*` C++ 包装最终经 `src/common/hcomm_dlsym/` 下的 `Ccu*` 弱符号落到 HCOMM 仓，本仓不编译期依赖 HCOMM 私有头。
- **代价模型**（u8 系列，只需结论）：新选择器用 \( T(n) = A \cdot n + B \cdot n + C \) 对每个候选算法估算耗时，其中 A 建模跨卡传输带宽项、B 建模本地拷贝/归约、C 是固定时延常数；模板用静态 `CalcCostCoeff` 申报这三个系数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ops/op_common/template/ccu_alg_template_base.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu_alg_template_base.h) | 所有 CCU 模板的直接基类 `CcuAlgTemplateBase`，含 2Die 通道划分等公共工具 |
| [src/ops/op_common/template/ccu/ccu_kernel_utils.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.cc) | CCU 版本判定、Loop/并行/偏移等**指令参数的位域编码**、低精度扩展倍数计算 |
| [src/ops/op_common/template/ccu/ccu_kernel_alg_base.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc) | **Group 级组合原语**：`GroupReduce`/`GroupBroadcast`/`GroupCopy` 等，把「一次归约/广播」录制为 Loop/LoopGroup 指令序列 |
| [src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc) | 本讲精读的具体模板：CCU Mesh 1D AllReduce |
| [src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.cc) | 该模板对应的 **CCU kernel 体**：跑在 CCU 侧、录制指令流的函数 `CcuAllReduceMesh1DKernel` |
| [src/common/hcomm_dlsym/ccu/ccu_primitives_dl.hpp](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_primitives_dl.hpp) | `ccu::Read/Write/LocalCopy/LocalReduce/Event` 等 C++ 包装，经 dlsym 落到 HCOMM |
| [src/common/hcomm_dlsym/ccu/ccu_loop_dl.hpp](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_loop_dl.hpp) | `ccu::Loop`/`ccu::LoopGroup`：循环体录制与指令组提交 |
| [src/common/hcomm_dlsym/ccu_launch_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu_launch_dl.h) | `HcommCcuKernelRegister` / `HcommCcuKernelLaunch` 弱符号：kernel 注册与下发 |

另外，`src/ops/*/template/ccu/` 下共有 60 余个 CCU 模板（all_reduce、all_gather、reduce_scatter、broadcast、reduce、scatter、all_to_all_v 等算子各有一族），命名后缀包括 `mesh_1D`、`mesh_1D_one_shot`、`mesh_1D_2die_oneshot`、`*_mem2mem`、`*_multi_jetty`、`concurrent_mesh_nhr` 等——这是本轮演进大幅扩容后的「CCU 模板家族」。

## 4. 核心概念与源码讲解

### 4.1 CCU 引擎与 CcuAlgTemplateBase 抽象

#### 4.1.1 概念说明

CCU 是位于 **IO Die** 的专用集合通信协处理器。与 AICPU「通用核上跑 kernel、向 TS 投递 Task 描述符」不同，CCU 执行的是**预置指令流**：Host 侧把一串 CCU 可识别的指令写进指令空间，CCU 硬件逐条执行这些指令，经 URMA 完成远端读写。架构简介用三步概括这一流程（见 [docs/zh/architecture/architecture-brief.md:145-157](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/docs/zh/architecture/architecture-brief.md#L145-L157)）：

1. Host 将 CCU 指令序列下发至 CCU 指令空间，同时提交 CCU Kernel 任务至任务队列；
2. CCU Kernel 被调度器调度后发送至 CCU 执行；
3. CCU 执行对应指令流，并利用 URMA 完成数据搬运。

其代价与收益（architecture-brief 要点）：**高带宽、低时延**且少占计算核与访存带宽，但受片上资源限制、支持的通信域数量有限（Ascend 950PR/950DT）——这就是标题里「资源受限特性」的含义，也是 CCU 模板普遍限定 rankSize ≤ 8 之类条件的物理原因。

所有 CCU 模板的直接基类是 `CcuAlgTemplateBase`，它继承自通用模板基类 `CommonAlgTemplateBase`（u3-l5），补上 CCU 特有的公共成员（rank、数据类型、buffInfo、子通信域等）和一组 2Die 场景的通道划分工具。

#### 4.1.2 核心流程

一个 CCU 模板的完整执行分四步：

```text
CalcRes（host，算资源）
  ├─ 声明 kernel 数（ccuKernelNum）
  ├─ 为每个 kernel 填 CcuKernelInfo：函数名 + kernelFunc 指针 + CcuKernelArg + channels
  └─ 由执行器经 HcommCcuKernelRegister 注册
KernelRun（host，下发）
  ├─ 切片计算 + CalGoSize 得到搬运参数
  ├─ 组 taskArgs（输入/输出地址、token、offset、goSize 四元组）
  └─ HcommCcuKernelLaunch 提交 kernel 任务      ← 架构三步中的第 1 步
CCU Kernel 体（CCU 侧，录制指令）
  ├─ LoadArgs 装载运行期参数，PreSync 交换地址变量
  ├─ 调 GroupReduce/GroupBroadcast 等组合原语录制指令流  ← 第 2 步
  └─ PostSync 收尾同步
CCU 硬件执行指令流，经 URMA 搬数据               ← 第 3 步
```

注意与 u2-l4 的衔接：CCU FastLaunch 快速路径回放的正是 `KernelRun` 里缓存的 `submitInfos`，跳过重新算资源。

#### 4.1.3 源码精读

基类声明（本轮新增了默认空实现的 `CalcCostCoeff`）：

- [ccu_alg_template_base.h:28-37](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu_alg_template_base.h#L28-L37)：`CcuAlgTemplateBase` 继承 `CommonAlgTemplateBase`；`static std::vector<CostModelParam> CalcCostCoeff(CalcCostCoeffParam) { return {}; }` 是本轮 diff 新增的默认实现——返回空表示「该模板未标定代价」，新选择器会直接跳过它。每个具体模板用同名静态函数**遮蔽**（shadow）这个默认实现来申报真实系数。
- [ccu_alg_template_base.h:90-99](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu_alg_template_base.h#L90-L99)：protected 成员 `myRank_`、`templateRankSize_`、`dataType_`、`buffInfo_`、`subCommRanks_` 等，是所有 CCU 模板共享的上下文。
- [ccu_alg_template_base.h:58-88](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu_alg_template_base.h#L58-L88)：一组 static 的 2Die 通道划分工具（`GetChannelDieId`、`SplitChannelsByDie`、`PartitionChannelsFor2Die` 等），服务于 `*_2die_*` 系列模板——单芯片多 Die 时把通道按 Die 分组、决定起几个 kernel。

CCU 版本判定是理解后面所有 V1/V2 双路径代码的钥匙：

- [ccu_kernel_utils.h:21-36](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.h#L21-L36)：`GetCcuVersion()` 先按设备类型分流——`DEV_TYPE_950`（A5）用 `CCU_V1`，其余（如 960）用 `CCU_V2`；若 HCOMM 不支持 V2 接口则降级回 V1 并打 WARNING。这呼应 u4-l2：设备类型是特性的能力开关。

#### 4.1.4 代码实践

1. **实践目标**：确认「本轮 diff 给 CCU 模板家族统一加了什么」。
2. **操作步骤**：执行 `git diff 757867153ef03d005ec8752e6cb8f802cecd1e0a..HEAD --stat -- 'src/ops/*/template/ccu/*' src/ops/op_common/template/ccu_alg_template_base.h`。
3. **观察现象**：all_reduce 目录下 8 个模板 `.cc` 各有 14~24 行纯新增，`.h` 各加 2 行；`ccu_alg_template_base.h` 加 3 行；没有删改。
4. **预期结果**：所有新增都是 `CalcCostCoeff` 静态函数及其声明、基类默认空实现——纯增量，不改变执行路径，只服务于新选择器的离线比价。
5. 本实践为只读 git 操作，可本地直接验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CcuAlgTemplateBase::CalcCostCoeff` 默认返回空向量而不是一个全 0 的 `CostModelParam`？

**答案**：返回空是「未标定」的哨兵语义。若返回 `{0,0,0}`，新选择器会认为该模板耗时恒为 0 而总是选它，导致错误决策；空返回让代价模型直接把该模板从候选中剔除（见 u3-l5「未标定即跳过」）。

**练习 2**：`GetCcuVersion()` 在什么情况下会把 V2 降级为 V1？这说明 CCU 能力由哪两方共同决定？

**答案**：设备类型不是 `DEV_TYPE_950`（即选了 V2）但 `HcommIsSupportCcuV2()` 返回假时降级。说明 CCU 能力由「芯片代际（HCCL 探测的 deviceType）」和「HCOMM 基础库是否实现了 V2 接口」共同决定——两仓解耦下，能力判定也要双边确认。

### 4.2 ccu_kernel_utils：指令参数的位域编码工具

#### 4.2.1 概念说明

CCU 指令流里有大量「控制参数」：循环迭代多少次、从哪个地址偏移开始、并行展开几路、低精度数据扩展几倍。这些参数最终都要塞进 64 位整数，按**位域**编进指令。`ccu_kernel_utils` 就是这层编码的字典：它不搬数据，只负责把人能读懂的配置翻译成硬件认识的位串。

#### 4.2.2 核心流程

以 `GetLoopParam(loopCtxId, gsaOffset, loopIterNum)` 为例，它把三个字段拼进一个 64 位字：

\[ \text{loopParam} = (\text{ctxId} \ll 45) \;|\; (\text{gsaOffset} \ll 13) \;|\; \text{loopIterNum} \]

其中 gsaOffset 占 32 位、loopIterNum 占 13 位。`GetParallelParam` 在 V1/V2 两代硬件上布局完全不同（V1 把三个字段放在高位 55/48/41，V2 放在低位 19/10/0）——这就是「CCU V121 Loop 规格变化适配」注释的含义，也是很多函数都要带 `CcuVersion` 参数的原因。

#### 4.2.3 源码精读

- [ccu_kernel_utils.cc:28-40](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.cc#L28-L40)：`SetBits` 位掩码辅助与 `GetMaxLoopIterNum`（12 位全 1，即单循环指令最多 4095 次迭代上限的编码）。
- [ccu_kernel_utils.cc:42-52](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.cc#L42-L52)：`GetLoopParam` 按 ctxId(8bit@45)/gsa(32bit@13)/iterNum(13bit@0) 拼装循环控制字。
- [ccu_kernel_utils.cc:61-97](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.cc#L61-L97)：`GetParallelParam` 的 V1/V2 双布局与 `GetOffsetParam`（gsa@21/ms@10/cke@0）。
- [ccu_kernel_utils.cc:99-105](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.cc#L99-L105)：`GetExpansionParam` 把扩展倍数（1/2/4）编码到 Bit[53-54]，写进目标地址 token——低精度归约「一份输入膨胀为多字节输出」就靠它表达。
- [ccu_kernel_utils.cc:107-124](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_utils.cc#L107-L124)：`GetReduceExpansionNum` 计算扩展倍数：FP8/HIF8/INT8 等低精度输入若未指定输出类型，默认升到 FP32，`expansionNum = sizeof(outputType)/sizeof(dataType)`（如 FP8→FP32 为 4）。

#### 4.2.4 代码实践

1. **实践目标**：亲手解码一个循环控制字。
2. **操作步骤**：读 `GetLoopParam` 的位宽定义，然后手工计算 `GetLoopParam(3, 0x1000, 5)` 的结果（只需纸笔，不必运行）。
3. **观察现象**：ctxId=3 落在 Bit[52:45]，gsaOffset=0x1000 落在 Bit[44:13]，iterNum=5 落在 Bit[12:0]。
4. **预期结果**：\( 3 \times 2^{45} + 0x1000 \times 2^{13} + 5 \)。若想验证，可在 `test/ut` 下仿照现有单测写一个 5 行的断言小程序（属示例代码，仓库没有现成的该函数单测）。
5. 运行验证「待本地验证」（需要搭建 UT 编译环境）。

#### 4.2.5 小练习与答案

**练习 1**：FP8E4M3 做 SUM 归约、未指定输出类型时，`GetReduceExpansionNum` 返回多少？对 `GroupReduce` 的目标地址有什么影响？

**答案**：FP8 输入默认升精度到 FP32 输出，返回 4/1 = 4。在 `GroupReduceV1` 里会先 `dst.token = dst.token + GetExpansionParam(4)`，且写入长度用 `loopLenExp = sliceSize * expansionNum`——即同一份输入归约后要写 4 倍字节的输出。

**练习 2**：`GetParallelParam` 为什么必须带 `CcuVersion` 参数？

**答案**：因为 V1 与 V2（V121 规范）硬件对 parallel 控制字的位布局不同：V1 是 repeatNum@55/repeatLoopIndex@48/totalLoopNum@41，V2 是 repeatNum@19/repeatLoopIndex@10/totalLoopNum@0。编码错位会导致硬件按错误次数展开循环，所以每次拼参数都必须知道目标 CCU 代际。

### 4.3 ccu_kernel_alg_base：Group 组合原语与 Loop/LoopGroup

#### 4.3.1 概念说明

单条 `ccu::Read`/`ccu::Write` 只搬一小段数据。一次 AllReduce 要搬几十上百 MB，CCU 的解法不是下发几十万条指令，而是提供两层控制流抽象：

- **`ccu::Func` + `ccu::Loop`**：把一段「原语序列」录制成循环体，由**运行期变量**（`ccu::Variable`）决定迭代次数——指令流里存一份循环体，迭代次数在 kernel 启动后才由 `LoadArg` 装载的参数决定。这就是 `CcuKernelCtxBase` 里 loopMap/body/loops 的由来。
- **`ccu::LoopGroup`**：把多个 `Loop` 打包成组，按 `parallelParam` 做**多路并行展开**（loopCount 路、路间间隔 msInterleave 个内存片），提高指令级并行度。

`ccu_kernel_alg_base` 在这两层之上再封装一层「Group 级组合原语」：`GroupReduce`（远端读 + 本地归约 + 写回）、`GroupBroadcast`（本地拷 + 远端写）、`GroupCopy`（纯本地搬运）、`GroupLocalReduce`（多 scratch 归约）及其 WithoutMyRank 变体。**具体模板不直接碰 `ccu::Read`，只调 Group 原语。**

#### 4.3.2 核心流程

以 `GroupReduce` 为例（一次「从 N-1 个对端读数据 + 本地一份，归约成一份写回」）：

```text
CreateMultiOpReduceV1（只做一次，建循环体）
  for 每路 loop (index 0/1):
    录制 Func 体：
      for i in 通道数:  ccu::Read(通道i, ccuBuf[bufBase+i], 远端源, len, evt, 1<<i)   # 并发读
      ccu::LocalCopy(ccuBuf[bufBase+N], 本地源, len, evt, 1<<N)                        # 本地一份
      ccu::EventWait(evt, (1<<(N+1))-1)                                                # 等 N+1 路到齐
      ccu::LocalReduce(&ccuBuf[bufBase], N+1, ...)                                     # 片上归约
      ccu::LocalCopy(目标, ccuBuf[bufBase], lenExp, evt, 1)                            # 写回
GroupReduceV1（每次调用，装参数）
  CalGoSize 拆出的 goSize 驱动两个 CCU_IF 分支：
    addrOffset 分支：整片数据 → 主 LoopGroup（串行迭代 m 次 × 并行 128 路）
    parallelParam 分支：尾块 → 双 Loop 的并行 LoopGroup（残余块 + 整片块）
```

数据切分由 `CalGoSize` 完成：把总大小按 `loopCount × memSlice`（默认 128 × 4096B）做带余除法，拆成 \( m \)（整循环轮数）、\( n \)（零头片数）、\( p \)（尾字节数），输出四元组 `{offset, loopIterNum, loopExtendNum, tailSize}`。同步用 `CcuEventGroup`：CCU 物理事件只有 16 位信号空间，超过 16 个信号时自动分组编码。

#### 4.3.3 源码精读

- [ccu_kernel_alg_base.h:40-103](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.h#L40-L103)：`CcuEventGroup`——把多个 16 位物理 `ccu::Event` 封装成逻辑信号空间，`Record`/`WaitAll`/`WaitAllExcept` 内部自动算 event 分组与 mask。这是「资源受限」在同步机制上的体现。
- [ccu_kernel_alg_base.h:105-123](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.h#L105-L123)：`LoopGroupConfig`（msInterleave 步长 / loopCount 并行度 / memSlice 单片字节）与 `GroupOpSizeVars`（addrOffset/loopParam/parallelParam/residual 四个**运行期变量**——它们是 `ccu::Variable`，值在 kernel 启动后由 `LoadArg` 装载，指令流录制时还不知道具体数值）。
- [ccu_kernel_alg_base.h:153-181](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.h#L153-L181)：`CcuKernelCtxBase`——每个 CCU kernel 的上下文基类，`loopMap` 按名字（"reduce"/"broadcast"/"localcopy"…）缓存已录制的循环体（`IsLoopEntityRegistered` 保证只录一次），`moRes` 持有 event/buffer 资源池。
- [ccu_kernel_alg_base.cc:57-99](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L57-L99)：`CalGoSize` 的 m/n/p 带余除法与四元组输出；[ccu_kernel_alg_base.cc:34-55](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L34-L55) `AllocGoResource` 按需分配 `loopCount×ckeNum` 个 event 与 `loopCount×msInterleave` 个 CcuBuffer（V2 每个 loop 克隆要偏移 ckeNum 个 event，实现 loop 间事件隔离）。
- [ccu_kernel_alg_base.cc:101-152](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L101-L152)：`CreateMultiOpReduceV1` 的循环体录制——lambda 里的 `ccu::Read`/`ccu::LocalCopy`/`ccu::LocalReduce`/`ccu::EventWait` 调用发生**一次**，效果是把这些原语录进 loop body；随后 `new ccu::Loop(loopParam, body)` 绑定运行期迭代变量。V2 版本（[ccu_kernel_alg_base.cc:154-208](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L154-L208)）用三个隔离 event（读/归约/写各一个）替代单 event 多 mask，并给 Loop 额外绑 addrOffset 变量。
- [ccu_kernel_alg_base.cc:210-222](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L210-L222)：`GroupReduce` 按 `CcuVersion` 分发到 V1/V2 实现（Broadcast/Copy 同理），让模板层对硬件代际无感。
- [ccu_kernel_alg_base.cc:435-473](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L435-L473)：`CreateMultiOpBroadcastV1`——广播循环体：本地拷入 ccuBuf，等完成后向每个通道 `ccu::Write` 对端地址，再写本地目标，最后 `EventWait` 全部完成位。
- [ccu_kernel_alg_base.cc:1153-1200](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L1153-L1200)：`CreateReduceLoop`（"local_reduce"）——mem2mem 类模板用的纯本地多 scratch 归约循环体。

再看 dlsym 层如何把「录制」落成指令、把「组」提交给硬件：

- [ccu_loop_dl.hpp:75-90](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_loop_dl.hpp#L75-L90)：`Loop::ComposeLoopBody`——`CcuLoopCreate` 建循环句柄后，`_CcuLoopBodyEnter`/`func.RunBody`/`_CcuLoopBodyExit` 三明治式地把 Func 体里的原语录进循环体。这证实了「录制」语义：执行 lambda ≠ 搬数据，而是生成指令。
- [ccu_loop_dl.hpp:101-164](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_loop_dl.hpp#L101-L164)：`LoopGroup` 构造时调 `CcuLoopGroupCreateFromVar[V2]`/`CcuLoopGroupAddLoopFromVar[V2]` 把整组循环登记进指令空间。
- [ccu_primitives_dl.hpp:102-180](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_primitives_dl.hpp#L102-L180)：`LocalCopy`/`LocalReduce`/`Read`/`Write` 的 C++ 重载全部一行转调 `CcuLocalCopy*`/`CcuReadMemTo*`/`CcuWriteMemTo*` 等 dlsym 符号——每个「远端读/写」就是一条经 URMA 执行的 CCU 指令。
- [ccu_control_flow_macro_dl.h:50-62](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_control_flow_macro_dl.h#L50-L62)：`CCU_IF` 宏——不是宿主 if！它把条件分支（`CcuIfBegin`/`CcuFlushPendingIfs`）也录进指令流，由 CCU 在执行期根据变量值决定走哪段。`GroupReduceV1` 里 `CCU_IF(goSize.addrOffset != 0)` 的含义是「指令流里录制一个运行期条件」，而非 host 分支。

#### 4.3.4 代码实践

1. **实践目标**：验证「Create 只录一次、Group 每次装参数」的两段式设计。
2. **操作步骤**：在 [ccu_kernel_alg_base.cc:107-110](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L107-L110) 与 [ccu_kernel_alg_base.cc:161-165](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/ccu/ccu_kernel_alg_base.cc#L161-L165) 观察两个 Create 函数开头的 `IsLoopEntityRegistered` 短路；再对照 `GroupReduceV1` 每次都重新填 `var.loopRemoteSrc/loopDst/loopLen`。
3. **观察现象**：同名 loopType 第二次调用 Create 直接返回；而 Group 函数没有这层短路。
4. **预期结果**：循环体（指令序列）在 kernel 生命周期内只录制一次并被复用，每次 Group 调用只改绑定在循环体上的运行期变量与地址参数——这是 CCU 指令空间有限背景下控制指令膨胀的关键设计。
5. 纯源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`GroupOpSizeVars` 的四个成员为什么是 `ccu::Variable` 而不是普通 `uint64_t`？

**答案**：因为它们的值来自 `LoadArgs` 装载的 kernel 启动参数（如 goSize 四元组），录制指令流时还未知。`ccu::Variable` 是「运行期才取值」的指令操作数，配合 `CCU_IF`/`Loop` 让同一段指令流适配任意数据规模。

**练习 2**：默认 `LoopGroupConfig` 下（interleave=8、loopCount=128、memSlice=4096B），一轮 LoopGroup 并行搬运能覆盖多少数据？`CalGoSize` 里 `GetMaxLoopIterNum()+1` 又把上限扩到多少？

**答案**：一轮 = 128 loop × 4096B = 512KB（分 8 路 interleave 摆放的 CcuBuffer）。串行迭代上限 \( m \le 4095 \)（12 位），故单个 Loop 最大覆盖约 \( 512\text{KB} \times 4096 \approx 2\text{GB} \)，超出部分由尾块双 Loop 分支兜底。

### 4.4 CcuTempAllReduceMesh1D：模板与 CCU kernel 体

#### 4.4.1 概念说明

`CcuTempAllReduceMesh1D` 是 CCU 引擎下 Mesh 1D 拓扑的 AllReduce 模板（selector 产出的 algName 形如 `CcuMSAllReduceSoleMesh`，见 u3-l2 命名约定）。算法上是经典两阶段：

\[ \text{AllReduce} = \text{ReduceScatter} + \text{AllGather} \]

每张卡只「负责」自己那一片：ReduceScatter 阶段把所有卡上属于自己片的数据读过来归约；AllGather 阶段把自己归约好的片广播给所有卡。两个阶段分别对应一次 `GroupReduce` 和一次 `GroupBroadcast`——4.3 的组合原语在这里被装配成完整算法。

每个模板由两半组成：host 侧的模板类（`CalcRes`/`KernelRun`/`FastLaunch`）和 CCU 侧的 kernel 体（`CcuAllReduceMesh1DKernel`），后者放在 `kernel/` 子目录。

#### 4.4.2 核心流程

```text
host: CalcRes
  资源申报：0 个从线程、0 个主线程 notify、1 个 CCU kernel（GetThreadNum()==1，即 1 个 Mission）
  为 kernel 填 CcuKernelInfo{函数名"CcuKernelAllReduceMesh1D", kernelFunc, Arg(rank/rankId/opParam/subCommRanks), channels}
  通道计算：普通拓扑 CalcChannelRequestMesh1D；MESH_1D_CLOS 拓扑走带优先级版本并只保留 UBC_CTP 协议通道
host: KernelRun
  CheckCcuDataType 校验 → CalcSliceInfo 均匀切片 → CalGoSize 拆循环参数
  taskArgs = {inputAddr, outputAddr, token, offSet, goSize×4} 共 8 个 uint64
  HcommCcuKernelLaunch(threads[0], ccuKernels[0], taskArgs)   ← 下发
  FillCachedArgs 缓存 submitInfo（供 FastLaunch 回放）
CCU: CcuAllReduceMesh1DKernel
  ParseKernelArg → InitResource（按通道取每个对端的 input/output/token 变量）
  LoadArgs（8 个参数装载为运行期变量）→ PreSync（把本卡三个地址变量写给所有对端并等齐）
  DoAllReduce：GroupReduce（ReduceScatter 相位）+ GroupBroadcast（AllGather 相位）
  PostSync（通知所有对端本卡完成）
```

#### 4.4.3 源码精读

**模板类侧：**

- [ccu_temp_all_reduce_mesh_1D.h:19-52](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.h#L19-L52)：类声明。`Describe` 返回带 tempRankSize 的自描述串；声明 `CalcRes`/`KernelRun`/`FastLaunch` 与本轮新增的静态 `CalcCostCoeff`。
- [ccu_temp_all_reduce_mesh_1D.cc:19-38](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L19-L38)：本轮新增的 `CalcCostCoeff`——`rankSize > 8` 直接返回空（未标定，对应 CCU 资源受限的适用范围）；代价建模为「与 two-shot 相同」：`CalcMeshParam(2n, netType, portNum, rankSize, A)` 算 A（CLOS 网 8 端口、其他 1 端口；传输量按两相位 \( 2n \) 计），`CalcLatencyParams(taskNum=20, EngineType::CCU, C)` 算时延常数，B=0（片上归约由 CCU 硬件完成，不单列本地拷贝项）。
- [ccu_temp_all_reduce_mesh_1D.cc:63-90](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L63-L90)：`CalcSliceInfo`——按 `⌈dataSize / (R×unitSize)⌉ × unitSize` 均匀切块并校验总和恰好等于 dataSize。
- [ccu_temp_all_reduce_mesh_1D.cc:92-139](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L92-L139)：`CalcRes`——申报 `notifyNumOnMainThread=0`、`slaveThreadNum=0`、`ccuKernelNum={1}`；把 kernel 函数指针与 `CcuKernelArgAllReduceMesh1D`（rankSize/rankId/opParam/subCommRanks）装进 `CcuKernelInfo`；通道按拓扑分流，`MESH_1D_CLOS` 时只保留 `COMM_PROTOCOL_UBC_CTP` 协议的通道（[ccu_temp_all_reduce_mesh_1D.cc:111-124](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L111-L124)）。
- [ccu_temp_all_reduce_mesh_1D.cc:141-167](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L141-L167)：`CheckCcuDataType` 与其上方注释——CCU 数据类型规则：高精度模式（dataType==outputDataType）支持 FP32/FP16/BF16/UINT8/INT16/INT32；低精度模式对 AllReduce **不支持**（直接报 `HCCL_E_PARA`）。这与 4.2 的 `GetReduceExpansionNum` 形成对照：指令层支持低精度扩展，但该模板的业务语义层禁用。
- [ccu_temp_all_reduce_mesh_1D.cc:169-214](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L169-L214)：`KernelRun`——算切片与 goSize 后组装 8 个 `uint64` 的 `taskArgs`，调 `HcommCcuKernelLaunch` 下发；成功后 `FillCachedArgs` 把参数缓存进 `submitInfos`。
- [ccu_temp_all_reduce_mesh_1D.cc:216-243](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L216-L243)：`FastLaunch`——u2-l4 CCU FastLaunch 快速路径的回放端：直接取缓存的 `cachedArgs`，仅刷新 input/output 两个地址（缓冲区基址可能变化），再次 `HcommCcuKernelLaunch`。地址在 args 数组中的下标（0/1 与 8/9）与 `KernelRun` 的 `taskArgs` 布局严格对应。
- [ccu_temp_all_reduce_mesh_1D.cc:245](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L245)：`GetThreadNum() = 1`——单 Mission，印证「Thread 抽象为 Mission」。

**kernel 体侧：**

- [ccu_kernel_all_reduce_mesh1d.h:21-39](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.h#L21-L39)：kernel 参数结构 `CcuKernelArgAllReduceMesh1D` 与上下文 `AllReduceMesh1DContext`（继承 `CcuKernelCtxBase`，即自带 loopMap/资源池）。
- [ccu_kernel_all_reduce_mesh1d.cc:33-59](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.cc#L33-L59)：`InitResource`——对每个对端用 `ccu::GetResByChannel<Variable>(channel, INPUT_XN_ID/OUTPUT_XN_ID/TOKEN_XN_ID)` 取回**对端预先登记**的三个地址变量。变量交换的前置正是 PreSync。
- [ccu_kernel_all_reduce_mesh1d.cc:61-98](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.cc#L61-L98)：`LoadArgs` 把 8 个 taskArgs 依序装载为运行期变量（与 host 侧 `taskArgs` 的拼装顺序一一对应）；`PreSync` 用 `WriteVariableWithNotify` 把本卡 input/output/token 三个变量写给每个通道对端，再 `NotifyWait` 等所有对端写齐——**每张卡的输入输出地址在运行期互相广播**，这就是 kernel 启动前 host 不必知道对端地址的原因。
- [ccu_kernel_all_reduce_mesh1d.cc:115-175](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.cc#L115-L175)：`DoAllReduce` 两相位——ReduceScatter 相位构造「N-1 个对端远端源 + 本地一份」，目标为本卡 output 的自己那片（加 `ctx.offset`），调 `GroupReduce`；AllGather 相位以本卡归约结果为源、N-1 个对端 output 为远端目标，调 `GroupBroadcast`。两者都透传 `GetCcuVersion()`。
- [ccu_kernel_all_reduce_mesh1d.cc:180-207](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.cc#L180-L207)：kernel 主入口 `CcuAllReduceMesh1DKernel`——`ParseKernelArg → InitResource → LoadArgs → PreSync → DoAllReduce → PostSync` 六步；`PostSync` 在所有通道上 Record/Wait `POST_SYNC_ID` 通知位，保证对端不会提前复用缓冲。

**下发通道：**

- [ccu_launch_dl.h:26-33](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu_launch_dl.h#L26-L33)：`HcommCcuKernelRegister`（登记 kernel 函数与实参）与 `HcommCcuKernelLaunch`（把 kernel 任务提交到某个 Thread/Mission）的弱符号声明——host 侧「下发」动作的全部出口就这两个。

#### 4.4.4 代码实践

1. **实践目标**：对照架构简介 CCU 三步流程，在源码中找到每一步的落点，并说明 CCU 与 AICPU 下发机制的本质区别。
2. **操作步骤**：
   - 第 1 步「Host 下发 CCU 指令序列 + 提交 kernel 任务」：从 [ccu_temp_all_reduce_mesh_1D.cc:200-205](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L200-L205) 的 `HcommCcuKernelLaunch` 入手，追到 [ccu_launch_dl.h:31-33](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu_launch_dl.h#L31-L33)；「指令序列」则来自 kernel 体内的录制（下一步）。
   - 第 2 步「CCU Kernel 被调度执行」：kernel 体 `CcuAllReduceMesh1DKernel`（[ccu_kernel_all_reduce_mesh1d.cc:180-207](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.cc#L180-L207)）在 `DoAllReduce` 里调 `GroupReduce/GroupBroadcast`，后者经 [ccu_loop_dl.hpp:75-90](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_loop_dl.hpp#L75-L90) 的 `_CcuLoopBodyEnter → RunBody → _CcuLoopBodyExit` 把原语录成指令。
   - 第 3 步「CCU 执行指令流 + URMA 搬数据」：确认 [ccu_primitives_dl.hpp:140-172](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/ccu/ccu_primitives_dl.hpp#L140-L172) 的 `ccu::Read/ccu::Write` 只是声明指令，本仓中**没有任何拷贝循环的宿主代码**。
3. **观察现象**：与 u5-l1 对照——AICPU 的 `HcclLaunchAicpuKernel` 是跑在 AICPU 上的 kernel，运行期才展开 OpParam、逐条产出 `HcclHcommBatchTransferDesc` 投递给 TS 执行器；CCU 的 `CcuAllReduceMesh1DKernel` 则在调度后把 Read/Write/LocalReduce 连同 Loop/LoopGroup/CCU_IF 控制流**一次性录制为静态指令流**，迭代次数、地址偏移全部以 `ccu::Variable` 挂在指令上，由硬件在执行期解析。
4. **预期结论（本质区别）**：AICPU 是「通用核上运行时动态生成 Task 描述符、TS 逐条调度」；CCU 是「软件预先编排紧凑指令流（含循环与分支）、硬化单元按流执行并经 URMA 直接搬数据」。前者灵活、支持任意规模；后者路径短、高带宽低时延，但受指令空间与片上资源约束（这也是 `CalcCostCoeff` 里 rankSize>8 不标定的原因之一）。
5. 纯源码阅读型实践，结论可直接从代码结构推出；若要在真机上观察，需 950 类设备（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`PreSync` 交换的三个变量（INPUT_XN_ID/OUTPUT_XN_ID/TOKEN_XN_ID）解决什么问题？如果不做 PreSync 会怎样？

**答案**：解决「对端缓冲区地址的运行期发现」。host 侧 launch 时只传了本卡的 inputAddr/outputAddr/token；每张卡把自己这三个值经通道变量写给所有对端并等齐，之后 `DoAllReduce` 才能构造出 `RemoteAddr`。没有 PreSync，本卡无法知道对端 input/output 在哪里，远端读/写指令无从生成。

**练习 2**：`FastLaunch` 为什么只刷新 `args[0]`（inputAddr）和 `args[1]`（outputAddr），而 goSize 等其余参数原样复用？

**答案**：FastLaunch 的缓存键（u2-l4 的 fastLaunchTag）保证了回放时算子类型、count、数据类型、拓扑等全部相同，因此切片与 goSize 不变；唯一可能变化的是两次调用分配到的输入/输出缓冲区基址。`FillCachedArgs` 同时缓存了 `inBuffBaseOff/outBuffBaseOff`（args[8]/args[9]），回放时 `新基址 + 旧偏移` 即得新地址。

**练习 3**：`CcuTempAllReduceMesh1D::CalcCostCoeff` 中为什么 `CalcMeshParam` 的第一个参数是 `2 * param.n`，而 B 恒为 0？

**答案**：`2n` 对应两相位各搬 \( n \) 字节（ReduceScatter 读入 \( n \) 的 1/R 片等价于全量读 + AllGather 写出）；CCU 的本地归约在硬件内完成、与搬运流水重叠，模板作者选择把这部分开销并入 A 与 C（时延常数按 taskNum=20 条指令估算），故 B=0。注释「和twoshot相同」说明该代价模型直接复用了 AICPU two-shot 模板的标定思路。

## 5. 综合实践

**任务：画出 CCU AllReduce 的「host 资源—下发—指令录制—硬件执行」全链路时序图，并标注三个架构步骤。**

具体步骤：

1. 从 [ccu_temp_all_reduce_mesh_1D.cc:92-139](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L92-L139)（CalcRes）出发，列出该模板申报的全部资源：几个 kernel、几条 channel、几个从线程；说明 `CcuKernelInfo` 里的 kernelFunc 与 channels 分别被谁消费。
2. 沿 [ccu_temp_all_reduce_mesh_1D.cc:169-214](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/ccu_temp_all_reduce_mesh_1D.cc#L169-L214)（KernelRun）写出 taskArgs 的 8 个字段及来源（切片、goSize）。
3. 在 kernel 体 [ccu_kernel_all_reduce_mesh1d.cc:180-207](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/ccu/kernel/ccu_kernel_all_reduce_mesh1d.cc#L180-L207) 中标出六步流程，并把 `DoAllReduce` 的两相位映射回 4.3 的 `GroupReduce`/`GroupBroadcast` 录制逻辑。
4. 在图上用三种颜色区分：host 一次性完成的（资源/下发）、录制期完成的（指令生成）、执行期 CCU 硬件完成的（URMA 搬数据 + Variable 解析 + Event/Notify 同步）。
5. 最后在图旁写一段 100 字左右的对比：同样的 AllReduce，AICPU 路径（u5-l1 的 NHR 模板）与本 CCU 路径在「谁生成搬运指令、谁来执行搬运、数据切片粒度」上的三点差异。

预期成果：一张时序/分层图 + 一段对比文字。全部素材都在本讲引用的源码内，不需要真机；若想进一步在真机上验证 algName 是否真的选中 `CcuMSAllReduceSoleMesh`，可在 950 设备上设 `HCCL_DEBUG_CONFIG` 打开选择器日志观察（待本地验证）。

## 6. 本讲小结

- CCU 是 IO Die 上的专用集合通信协处理器：**软件组指令、硬件经 URMA 搬数据**，高带宽低时延、少占计算核，但受片上资源限制（通信域数量有限、模板普遍限定 rankSize ≤ 8），设备代际决定 V1/V2 指令规格。
- `CcuAlgTemplateBase` 是所有 CCU 模板基类；本轮演进为其及 60 余个具体模板统一新增静态 `CalcCostCoeff`（默认空返回＝未标定即被新选择器跳过），纯增量、不改执行路径。
- `ccu_kernel_utils` 负责**指令参数位域编码**（Loop/并行/偏移/低精度扩展），V1/V2 布局不同是双路径代码的根源；`GetReduceExpansionNum` 表达 FP8→FP32 等低精度升位置。
- `ccu_kernel_alg_base` 提供 `GroupReduce/GroupBroadcast/GroupCopy` 等 Group 级组合原语，靠 `Func+Loop`（录制一次循环体、运行期变量定迭代）与 `LoopGroup`（128 路并行展开）抑制指令膨胀；`CcuEventGroup` 用 16 位物理事件拼出更大逻辑信号空间。
- `CcuTempAllReduceMesh1D` 展示完整装配：host 侧 `CalcRes` 申报单 kernel 单 Mission、`KernelRun` 组 8 个 taskArgs 并 `HcommCcuKernelLaunch`、`FastLaunch` 仅刷新地址回放；CCU 侧 kernel 体经 PreSync 交换地址变量后做 `GroupReduce`（ReduceScatter）+ `GroupBroadcast`（AllGather）。
- 与 AICPU 的本质区别：AICPU 在通用核上运行期动态生成 Task 描述符交给 TS；CCU 预录紧凑指令流（含循环/分支），由硬化单元按流执行。

## 7. 下一步学习建议

- 下一讲 u6 系列：回到 `ccu_primitives_dl.hpp` 背后的机制——`Ccu*` 符号如何经 dlsym 落到 HCOMM（u6-l1），以及 `ccu_res_dl`/`hcomm_primitives_dl` 中资源与原语的控制面/数据面归属（u6-l2、u6-l3）。
- 若对新选择器如何消费本讲的 `CalcCostCoeff` 感兴趣，先读 u3-l5 的代价标定一节打基础，再进入 u8-l2（CostModel/CostTable）。
- 扩展阅读源码：对比 `ccu_temp_all_reduce_mesh_1D_one_shot.cc`（单相位直写）与 `ccu_temp_all_reduce_mesh_1D_2die_oneshot.cc`（2Die 分 kernel），体会同一引擎下「轮数 × 拓扑 × Die 划分」如何派生出模板家族；以及 `ccu_temp_all_reduce_concurrent_mesh_nhr.cc` 如何在 CCU 上实现两级（Mesh+NHR）编排。
