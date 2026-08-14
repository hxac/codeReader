# u6-l2 资源、原语与拓扑的 dlsym 封装

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `src/common/hcomm_dlsym/` 下各 `_dl` 模块按「域」划分的职责：资源域、原语域、拓扑域、通信域配置域，并能判断一个 HCOMM 符号该落在哪个域。
2. 掌握 `hcomm_primitives_dl` 的数据面原语家族：Write/Read/Reduce 四类搬运 + Notify 两类同步 + Fence/超时等辅助，以及批量描述符 `HcclHcommBatchTransferDesc` 的九种传输类型。
3. 理解 `hccl_rank_graph_dl` 的六个拓扑查询接口如何被 topo 匹配器和 channel 计算消费，`hccl_res_dl` 的 Thread/Mem 获取接口如何被资源计算消费。
4. 沿着 `KernelRun → 数据搬运包装器 → _dl 弱符号 → libhcomm.so` 亲手跟踪一次「远端 Write + Notify」，并准确标注沿途哪些调用属于控制面、哪些属于数据面。
5. 了解本轮演进：`INIT_SUPPORT_FLAG` 等处统一改用 `HcclDlsym`、`dlopen` 收敛到 `HcclDlopen`，以及 `hccl_host_comm_dl.h` 新增 `HCCL_CONFIG_TYPE_HCCL_ALGO` 配置类型。

## 2. 前置知识

本讲是 u6-l1 的直接续篇，先回顾并补齐几个概念：

- **域（domain）划分**：u6-l1 讲了 `HcommDlInit` 如何加载 libhcomm.so 并把句柄分发给各域的 `XxxDlInit`。本讲逐域打开这些封装。一个「域」就是一类职责相近的 HCOMM 接口：资源（Thread/Mem/Channel 句柄的申请与销毁）、原语（数据搬移与同步）、拓扑（RankGraph 查询）、通信域配置（comm 状态与配置读取）。
- **控制面 / 数据面**：回顾 u1-l1 的架构硬约束——控制面负责资源管理与拓扑查询（「搭好路」），数据面负责真正的数据搬移与同步（「在路上跑车」）。HCCL 算子作为数据面消费方，只准通过 `_dl` 封装调用 HCOMM，不得耦合其内部实现。本讲的 `hccl_res_dl`/`hccl_rank_graph_dl` 基本属控制面，`hcomm_primitives_dl` 属数据面。
- **Thread / Channel / Notify**：回顾 u1-l2——通信引擎 = Thread（执行上下文）+ 线程调度器；Channel 是连接两个 rank 的通信通道；Notify 是通道上的轻量同步信号（record 点亮、wait 等待），一次数据面搬运的前后同步就是靠 `NOTIFY_IDX_ACK`（=0）与 `NOTIFY_IDX_DATA_SIGNAL`（=1）这两类 notify 档位完成的（见 [src/ops/op_common/inc/alg_param.h:76-77](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/inc/alg_param.h#L76-L77)）。
- **弱符号四步模式**：u6-l1 的核心结论——`DECL_WEAK_FUNC`（声明弱函数）→ `DEFINE_WEAK_FUNC`（兜底桩 + `HcommIsSupportXxx` 支持标志）→ `INIT_SUPPORT_FLAG`（dlsym 探测、置标志）→ `XxxDlInit`（域级绑定入口）。本讲大量出现，不再重复解释宏本身。
- **`HcclDlopen/HcclDlsym/HcclDlclose`**：本轮新增的 weak_alias 封装层（u6-l1 模块一）。外部可用强符号接管全部动态加载。本讲关注它落到了哪些调用点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/common/hcomm_dlsym/hcomm_dlsym.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc) | 绑定总入口 `HcommDlInit`：`HcclDlopen` libhcomm.so 后分发给 11 个域初始化 |
| [src/common/hcomm_dlsym/hccl_res_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.cc) | **资源域**：Thread 申请、共享内存、远端内存视图、引擎上下文销毁 |
| [src/common/hcomm_dlsym/hcomm_primitives_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.cc) | **原语域**：Write/Read/Reduce/Notify/Fence/超时/AICPU Task Cache 及批量传输 |
| [src/common/hcomm_dlsym/hccl_rank_graph_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_rank_graph_dl.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_rank_graph_dl.cc) | **拓扑域**：按网络层查询 TopoInst、拓扑类型、rank 集合、Endpoint 信息 |
| [src/common/hcomm_dlsym/hccl_host_comm_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_host_comm_dl.h) | **通信域配置域（host 形态）**：comm 状态、配置读取、AICPU kernel 下发、状态回调 |
| [src/common/hcomm_dlsym/hccl_device_comm_dl.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_device_comm_dl.h) | 同一批符号的 device（AICPU）编译形态声明 |
| [src/common/hcomm_dlsym/dlsym_common.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h) | 宏工具箱；本轮 `INIT_SUPPORT_FLAG` 改用 `HcclDlsym` |
| [src/ops/op_common/dlhcomm_function.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/dlhcomm_function.cc) | ops 层的懒加载函数指针绑定（`DlHcommFunction`），同样走 `HcclDlopen/HcclDlsym` |
| [src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc) | 模板与原语之间的数据搬运包装器（本讲综合实践的战场） |
| [src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc) | 样例模板：one-shot Mesh AllReduce，实践调用链的起点 |

先给一张域划分总览（来自 [hcomm_dlsym.cc:63-87](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_dlsym.cc#L63-L87)）：

| 域初始化函数 | 所属文件 | 面 | 一句话职责 |
| --- | --- | --- | --- |
| `HcclResDlInit` | hccl_res_dl.cc | 控制面 | 申请/回收 Thread、Mem、引擎上下文 |
| `HcclRankGraphDlInit` | hccl_rank_graph_dl.cc | 控制面 | 查询网络拓扑（层/实例/rank/Endpoint） |
| `HcommPrimitivesDlInit` | hcomm_primitives_dl.cc | **数据面** | Write/Read/Reduce/Notify 搬运与同步 |
| `HcclCommDlInit` | hccl_host_comm_dl.cc | 控制面 | comm 状态、配置读取、AICPU kernel 下发 |
| `HcclInnerDlInit` / `HcommProfilingDlInit` / `HcclResExptDlInit` | 其他 | 混合 | 内部入口、打点、扩展资源（本讲不展开） |
| `CcuResDlInit` / `HcclCcuResDlInit` / `CcuLaunchDlInit` / `CcuPrimitivesImplDlInit` | ccu 相关 | 混合 | CCU 引擎专用资源与原语（对照 u5-l4） |

## 4. 核心概念与源码讲解

### 4.1 模块一：hccl_res_dl 资源获取

#### 4.1.1 概念说明

算法要跑起来，先得「借到硬件资源」：若干个 Thread（执行上下文，每个带若干 Notify 档位）、一块通信内存（cclBuff / 共享内存）、以及对端内存在本 rank 的可见视图。这些资源全部由 HCOMM 统一管理（避免多个算子重复占用），HCCL 只能按 tag 申请、用完归还。`hccl_res_dl` 就是这层「资源租借窗口」的 dlsym 封装——典型控制面。

#### 4.1.2 核心流程

```text
Executor 资源计算（u3-l4 CalcRes）
        │  产出 AlgResourceRequest：需要几个 thread、每 thread 几个 notify、多大 mem
        ▼
op_common.cc HcclGetThread / GetAlgRes*
        │
        ├── HcommIsSupportHcclThreadAcquireWithConfig()？          ← 支持标志（INIT_SUPPORT_FLAG 置位）
        │       是 → HcclThreadAcquireWithConfig(comm, 引擎, 数量, ThreadConfig{notifyNum}, out)
        │       否 → 旧接口 HcclThreadAcquire（按最大 notify 数粗放申请）
        ├── HcclDevMemAcquire(comm, "DPUTAG", &size, &ptr, &newCreated)   ← 按 tag 申请共享内存
        └── HcclChannelGetRemoteMems(comm, channel, ...)            ← 拿对端注册内存的远端视图
        ▼
得到 ThreadHandle[] / 内存地址，装进 resCtx，随算子下发（u5-l1 的 OpParam 资源上下文）
```

#### 4.1.3 源码精读

先看声明面。资源域共声明 10 个弱符号，可分三组（[hccl_res_dl.h:78-103](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.h#L78-L103)）：

- **Thread 组**：`HcclThreadExportToCommEngine`（把 thread 迁移到目标引擎）、`HcclThreadAcquireWithConfig`（带 `ThreadConfig` 精确指定每 thread 的 notify 数）、`HcclDedicatedThreadAcquire`（申请专用线程，如 host 按序下发线程）。
- **Mem 组**：`HcclDevMemAcquire`（按 memTag 申请设备内存，幂等——`newCreated` 告诉你是不是第一个创建者）、`HcclCommMemReg`（把内存注册进通信域）、`HcclGetRemoteIpcHcclBuf` / `HcclChannelGetRemoteMems`（拿远端 rank 的 cclBuff / 注册内存视图）。
- **生命周期组**：`HcclTaskRegister/UnRegister`（注册回调）、`HcclEngineCtxDestroy`（销毁引擎上下文）。

注意头文件前半部分是成片的「版本桩」：当编译所用的 CANN 版本低于 9.0.0/9.1.0/9.2.0 时，`HcclMemHandle`、`ThreadConfig`、`HcclDedicatedThreadType` 等类型由 HCCL 自己补齐定义（[hccl_res_dl.h:22-72](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.h#L22-L72)）。这是两仓解耦的代价：新接口的类型旧头文件里没有，HCCL 必须自带兼容定义才能编译通过。

绑定入口 `HcclResDlInit` 与 u6-l1 讲过的模式完全一致——10 个 `INIT_SUPPORT_FLAG` 逐个 dlsym 探测（[hccl_res_dl.cc:41-53](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.cc#L41-L53)）。

真正的消费者在 op_common.cc。新路径按每个从 thread 的实际需要填 `ThreadConfig` 再申请（[op_common.cc:1541-1555](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L1541-L1555)）：

```cpp
// 示例代码（节选自原文件，注释为讲解所加）
CHK_RET(HcclThreadAcquireWithConfig(
    comm, COMM_ENGINE_AICPU, threadNum, THREAD_TYPE_TS, threadConfigs.data(), threads.data()));
```

同文件还保留了旧 HCOMM 的降级路径——支持标志为假时退回 `HcclThreadAcquire`，用「最大 notify 数 + 1」一刀切（[op_common.cc:1584-1596](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L1584-L1596)）。这正是弱符号机制的意义：新库走新接口，旧库自动走旧接口，HCCL 一份代码通吃。

另外两个典型消费点：DPU 场景按 `"DPUTAG"` 申请 100MB 共享内存再对半分给 NPU→DPU / DPU→NPU 两个方向（[op_common.cc:2865-2872](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L2865-L2872)）；host 按序下发线程用 `HcclDedicatedThreadAcquire` 申请专用线程（[order_launch.cc:162-168](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/order_launch.cc#L162-L168)）。

#### 4.1.4 代码实践

1. **实践目标**：搞清一次 AllReduce 一共从 HCOMM 借了哪些资源、每个资源的 key 是什么。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n "HcommIsSupportHccl" src/ops/op_common/op_common.cc | head -20`，收集所有资源域支持标志的判定点；
   - 对每个判定点向上看 20 行，回答：这个分支申请的资源（thread？mem？远端内存视图？）被存进了 `resCtxHost` 的哪个字段。
3. **需要观察的现象**：同一资源在不同入口（AICPU 引擎、DPU 场景、图模式）的申请路径不同，但都会落到 `hccl_res_dl` 的同一批弱符号上。
4. **预期结果**：能画出「`AlgResourceRequest` → `HcclThreadAcquireWithConfig` / `HcclDevMemAcquire` / `HcclChannelGetRemoteMems` → `resCtx` 字段」的资源装配表。
5. 运行行为「待本地验证」（需上板环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HcclDevMemAcquire` 要返回 `bool* newCreated`，而 `HcclThreadAcquireWithConfig` 不需要？
**答案**：内存是按 `memTag` 幂等申请的——同 tag 第二个调用者拿到同一块内存，需要知道「是不是我创建的」来决定谁负责初始化/释放；thread 是独占性资源，每次申请都得到新的句柄，不存在「复用已有实例」的语义。

**练习 2**：`HcclThreadAcquireWithConfig` 相比旧的 `HcclThreadAcquire` 省了什么？
**答案**：旧接口只能传一个「最大 notify 数」，所有 thread 都按峰值配 notify（见 op_common.cc 降级分支的 `GetMaxNotifyNum`）；新接口用 `ThreadConfig` 数组逐 thread 精确申报 `notifyNumPerThread`，notify 是硬件资源，按需分配能支撑更多并发 thread。

**练习 3**：`HcclDfxOpInfoCompat`（[hccl_res_dl.h:107-133](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_res_dl.h#L107-L133)）为什么自带 `reserve[96]` 预留字段？
**答案**：它是跨 HCCL/HCOMM 两仓传递的 DFX（可观测性）结构，两仓独立演进，HCOMM 侧加字段时只要落在预留区内就不破坏 ABI，HCCL 无需重编。

### 4.2 模块二：hcomm_primitives_dl 数据搬移与同步原语

#### 4.2.1 概念说明

这是唯一的数据面域。算法模板（u3-l5/u5-l1）编排出的每一步「把这段数据写到对端 cclBuff」「把对端数据读回来边读边归约」「通知对端我写完了」，最终都落到这个文件里的原语家族：

| 类别 | 代表符号（均为弱符号） | 语义 |
| --- | --- | --- |
| 写 | `HcommWriteNbiOnThread` / `HcommWriteWithNotifyOnThread` | 把本地 src 写到远端 dst；带 Notify 版本写完点亮对端 notify |
| 写归约 | `HcommWriteReduceWithNotifyOnThread` | 写到对端的同时按 `dataType/reduceOp` 归约 |
| 读 | `HcommReadNbiOnThread` | 从远端地址读到本地 |
| 读归约 | `HcommReadReduceOnThread` | 读回来边读边归约 |
| 同步 | `HcommChannelNotifyRecordOnThread` / `HcommChannelNotifyWaitOnThread` | 点亮 / 等待某个 notify 档位 |
| 屏障 | `HcommChannelFenceOnThread` / `HcommFlush` | 保证此前的下发已生效 |
| 批量 | `HcclHcommBatchTransferOnThread` | 一次提交一串描述符（下文详述） |
| 缓存 | `HcommAicpuTsTaskCacheLookup/Start/End/Execute/Clear` | AICPU Task Cache 回放接口（u5-l2 主题） |

注意命名规律：`...OnThread` 后缀表示「绑定到某个 ThreadHandle 上下文执行」，不带后缀的（如 `HcommWriteNbi`）则只按 channel 提交。

#### 4.2.2 核心流程

单条原语逐次下发，host↔device 交互次数随 slice 数线性增长。批量传输把「一串动作」压成一次调用：

```text
模板 KernelRun
  └─ 数据搬运包装器（alg_data_trans_wrapper.cc，4.3/4.4 详述）
        │  把 N 个 DataSlice 逐个转成 HcclHcommBatchTransferDesc
        ▼
HcclHcommBatchTransferOnThread(thread, channel, descs[], n)   ← 本模块导出的 C 包装
        │  g_HcommBatchTransferOnThread 为空？（HCOMM 太旧）
        │     是 → 报错返回 -1；上游包装器会先查 IsHcommBatchTransferOnThreadSupported() 并回退单条路径
        ▼
libhcomm.so 中的 HcommBatchTransferOnThread —— 逐条解释描述符并下发
```

九种描述符类型（[hcomm_primitives_dl.h:26-37](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.h#L26-L37)）本质是「四类基本动作 × 是否带归约 × 是否带通知」的组合：`WRITE / WRITE_REDUCE / WRITE_WITH_NOTIFY / WRITE_REDUCE_WITH_NOTIFY / READ / READ_REDUCE / NOTIFY_RECORD / NOTIFY_WAIT / NOTIFY_WAIT_WITH_DEFAULT_TIMEOUT`。每类动作的参数装在 56 字节联合体里（[hcomm_primitives_dl.h:39-79](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.h#L39-L79)），联合体保证描述符定长，才能放进数组一次传递。

#### 4.2.3 源码精读

`HcommBatchTransferOnThread` 是「新到旧 HCOMM 头文件还没有声明」的符号，所以它不走 `DECL_WEAK_FUNC` 四步，而是手工函数指针绑定——这是本文件里唯一一处非宏封装（[hcomm_primitives_dl.cc:82-99](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.cc#L82-L99)）：

```cpp
// 示例代码（节选）：指针为空 = HCOMM 未提供该符号，调用即报错
extern "C" int32_t HcclHcommBatchTransferOnThread(...)
{
    if (g_HcommBatchTransferOnThread == nullptr) {
        HCCL_COMPAT_ERROR("[HcclWrapper] HcommBatchTransferOnThread not supported");
        return -1;
    }
    return g_HcommBatchTransferOnThread(thread, channel, transferDescs, transferDescNum);
}
```

绑定发生在 `HcommPrimitivesDlInit` 末尾，本轮把裸 `dlsym` 换成了 `HcclDlsym`（[hcomm_primitives_dl.cc:140-147](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.cc#L140-L147)）——这是本讲开头提到的「本轮改用 HcclDlsym」的一个具体落点；同文件前半部分其余 30+ 符号则统一经由 `INIT_SUPPORT_FLAG` 宏间接受益（宏内部改成了 `HcclDlsym`，见 [dlsym_common.h:166-175](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/dlsym_common.h#L166-L175)）。

文件尾部还有一组「兼容包装」，值得注意 `HcclThreadNotifyWaitOnThreadDefault`（[hcomm_primitives_dl.cc:179-185](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.cc#L179-L185)）：新 HCOMM 支持「默认超时」版 wait 就用它，否则回退到显式传超时的旧版。这属于弱符号之外的「运行期二选一」兼容，与 4.1 的 thread 双路径是同一思想。

#### 4.2.4 代码实践

1. **实践目标**：验证「九种传输类型 = 组合枚举」的设计，并数清本文件绑定了多少符号。
2. **操作步骤**：
   - 打开 `hcomm_primitives_dl.h`，把 26-37 行的枚举抄成「基本动作 × 归约 × 通知」的三维表，标出每格对应的枚举值；
   - 执行 `grep -c "INIT_SUPPORT_FLAG" src/common/hcomm_dlsym/hcomm_primitives_dl.cc`，与手工数出的 `DEFINE_WEAK_FUNC` 数量对比。
3. **需要观察的现象**：枚举九个值恰好填满组合表（notify 的 record/wait 除外，它们是纯同步动作）。
4. **预期结果**：`INIT_SUPPORT_FLAG` 计数 = 弱符号总数 + 5 个 `DECL_SUPPORT_FLAG` 声明对应项（task cache 与 default timeout 系列），能对上即说明绑定清单无遗漏。
5. 静态阅读即可完成，无需环境。

#### 4.2.5 小练习与答案

**练习 1**：`HcommWriteWithNotifyOnThread` 与「`HcommWriteNbiOnThread` + 单独 `HcommChannelNotifyRecordOnThread`」效果等价吗？为什么还要前者的批量版本 `WRITE_WITH_NOTIFY`？
**答案**：语义上等价，但前者把「写 + 点通知」压成一条指令/一个描述符，减少下发次数；在批量描述符里还允许 `FuseNotifyToLastWriteReduceDesc`（见 4.4.3）把通知融合进最后一条写归约描述符，进一步省一次交互。

**练习 2**：`Nbi`（non-blocking immediate）后缀的原语，调用返回时数据搬完了吗？
**答案**：没有。`Nbi` 表示异步下发、立即返回，完成情况靠后续的 Notify record/wait 或 Fence 来确认；这正是包装器代码里「写完之后做后同步告诉对面写完了」注释的由来。

**练习 3**：为什么 `HcommBatchTransferOnThread` 不能像其他符号一样用 `DEFINE_WEAK_FUNC` 兜底？
**答案**：`DEFINE_WEAK_FUNC` 的桩只返回 -1，而批量接口的调用方（包装器）需要区分「不支持，请回退单条路径」和「支持但执行失败」两种情况；用手工函数指针 + `HcommIsSupportHcommBatchTransferOnThread()` 显式探测，才能让上游做正确的降级决策（见 alg_data_trans_wrapper.cc 各 `Do*` 模板函数开头的 `IsHcommBatchTransferOnThreadSupported()` 检查）。

### 4.3 模块三：hccl_rank_graph_dl 拓扑查询

#### 4.3.1 概念说明

u1-l2 讲过 RankGraph：HCOMM 用 Node/Endpoint/Edge/netLayer 描述整个集群网络。HCCL 的 topo 匹配器（u3-l3）和 channel 计算（u6-l3）需要的所有拓扑事实，都通过 `hccl_rank_graph_dl` 的六个只读查询接口获取——它不下发任何数据，是纯控制面。六个接口正好构成一条查询漏斗：

```text
HcclRankGraphGetTopoInstsByLayer(comm, netLayer)   → 本 rank 在第 netLayer 层属于哪些拓扑实例？
        ▼（对每个实例）
HcclRankGraphGetTopoType(comm, netLayer, instId)   → 该实例是 CLOS / MESH / ...？
HcclRankGraphGetRanksByTopoInst(comm, netLayer, instId) → 实例里有哪些 rank？（升序）
        ▼（更细粒度）
HcclRankGraphGetEndpointNum / GetEndpointDesc      → 实例里有多少通信端点、各是什么
HcclRankGraphGetEndpointInfo(comm, rankId, desc, attr)  → 端点属性（带宽系数 / die id / 位置）
```

#### 4.3.2 核心流程

以 u3-l3 的多级拓扑匹配为例（`topo_match_multilevel.cc`）：先查第 0 层实例集合，取本 rank 所在实例的 rank 列表得到「节点内子通信域」，再逐层向外，直到覆盖全部 rank——最终产出 `AlgHierarchyInfoForAllLevel`（多级子通信域描述），供 executor 切分 ReduceScatter/AllGather 的层级（对照 u3-l3 的 Layer0/1/2 切分）。

channel 计算则用 Endpoint 漏斗判定「某条链路属于哪个拓扑实例」：把链路的源 Endpoint 与每个实例的 Endpoint 列表比对（见 4.3.3 的 channel.cc 引用）。

#### 4.3.3 源码精读

六个声明集中在 [hccl_rank_graph_dl.h:38-54](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_rank_graph_dl.h#L38-L54)，绑定入口是六个 `INIT_SUPPORT_FLAG`（[hccl_rank_graph_dl.cc:33-41](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_rank_graph_dl.cc#L33-L41)）。头文件同样带 9.0.0 版本桩（`EndpointAttr`、`CommTopo` 扩展值，[hccl_rank_graph_dl.h:17-32](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_rank_graph_dl.h#L17-L32)）。

三个典型消费点：

- 多级拓扑匹配从第 0 层开始自底向上（[topo_match_multilevel.cc:26-33](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/topo/topo_match_multilevel.cc#L26-L33)），其注释还点明一个契约：返回的 rank 列表**由 HCOMM 保证升序**（底层是 `std::set`）——HCCL 依赖这个顺序做取模同位切分，是典型的「跨仓隐式契约」。
- channel 计算里 `GetTopoTypeByLink` 把链路归到拓扑实例：遍历实例 → 查 Endpoint 数量与描述 → 与链路源端点比对（[channel.cc:778-813](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/executor/channel/channel.cc#L778-L813)）。注意它被 `#if defined(AICPU_COMPILE) || CANN_VERSION_NUM < ...` 包住——同一封装的可用性还叠加了「编译形态 + 版本」双门控。
- topoInfo 的构建也在 op_common 里直接调用（[op_common.cc:3513-3529](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L3513-L3529)），即 u3-l3 讲过的「Selector 阶段算出并缓存 topoInfo」的取数处。

#### 4.3.4 代码实践

1. **实践目标**：找出六个拓扑接口的全部调用方，验证「topo 域只被控制面消费」。
2. **操作步骤**：
   - `grep -rn "HcclRankGraphGet" src/ops --include="*.cc" | grep -v dlsym` 列出所有调用；
   - 按文件归类：topo 匹配器（topo_match_*）、channel 计算（channel.cc）、topoInfo 构建（op_common.cc）。
3. **需要观察的现象**：没有任何 template / 数据搬运包装器直接调用这六个接口——它们只在算子执行前的资源计算与拓扑匹配阶段出现。
4. **预期结果**：调用清单全部落在控制面文件，可作为「控制面/数据面分离」约束的直接证据。
5. 静态阅读即可完成。

#### 4.3.5 小练习与答案

**练习 1**：`netLayer` 参数的取值含义是什么？为什么查询要按层进行？
**答案**：对应 RankGraph 的网络分层（Layer0 = Server 内、Layer1 = Server 间……，见 u1-l2）。不同层的实例集合、拓扑类型不同，分级通信（ReduceScatter→AllReduce→AllGather）正是要按层拿到子通信域，所以查询接口以 `netLayer` 为第一入参。

**练习 2**：如果 HCOMM 是旧版、六个符号一个都探测不到，会发生什么？
**答案**：所有查询落到 `DEFINE_WEAK_FUNC` 桩，打印 `not supported` 并返回 -1；上游 topo 匹配/资源计算会失败或走版本门控前的老路径（对照 u2-l2 的 `GetHcommVersion() < 9.0.0` 回退），不会静默产出错误拓扑。

**练习 3**：`EndpointAttr`（带宽系数、die id、位置）这类属性查询，最可能的下游用途是什么？
**答案**：供代价/资源决策使用——带宽系数可用于链路选择或代价建模（对照 u8 的 CostModel 带宽参数），die id/位置用于判断卡间亲缘（同 die/跨 die）以选择 Mesh/2Die 类算法（u5-l4）。

### 4.4 模块四：host/device_comm_dl 与 ops 层的 DlHcommFunction

#### 4.4.1 概念说明

通信域配置域封装「读 HCOMM 管理的 comm 状态与配置」：`HcclCommGetStatus`（通信域是否就绪）、`HcclConfigGetInfo`（按 `HcclConfigType` 读配置项）、`HcclGroupStatusGet`、`HcclAicpuKernelLaunch`（AICPU kernel 下发入口，u5-l1 已精读）、`HcclCommRegCommStateCallback`（注册 comm 状态回调）。它有两份几乎相同的声明：host 形态（`hccl_host_comm_dl.h`）与 device/AICPU 编译形态（`hccl_device_comm_dl.h`），因为同一份源码要双形态编译（对照 u4-l4 的 `AICPU_COMPILE` 守卫）。

此外还有一个「不走 `XxxDlInit`」的旁路：ops 层的 `DlHcommFunction` 单例，懒加载自己的 libhcomm.so 句柄并只绑三个函数指针。

#### 4.4.2 核心流程

```text
算子入口 / Selector 需要读配置
        ▼
DlHcommFunction::GetInstance()          ← 首次调用时 HcclDlopen("libhcomm.so")（懒加载，与总入口句柄相互独立）
        ▼
dlHcclConfigGetInfo(comm, HcclConfigType, ...)
        ├── HCCL_CONFIG_TYPE_OP_EXPANSION_MODE   → 决定引擎展开模式（u2-l4）
        └── HCCL_CONFIG_TYPE_HCCL_ALGO（本轮新增） → 读通信域级 HCCL_ALGO 配置
```

#### 4.4.3 源码精读

本轮在 [hccl_host_comm_dl.h:34-35](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hccl_host_comm_dl.h#L34-L35) 新增了 `#define HCCL_CONFIG_TYPE_HCCL_ALGO 1`，注释写明「后续新增字段参照此处定义」——`HcclConfigType` 枚举旧头文件里没有的取值，用宏补号。它的唯一消费者是 `HcclGetHcclAlgo`（[op_common.cc:3178-3201](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc#L3178-L3201)）：版本 ≤ 9.2.0_beta1 直接跳过，否则经 `dlHcclConfigGetInfo` 把通信域级的算法配置字符串读回来——这是 u8 新选择器「HCCL_ALGO 可来自通信域配置」这条数据通路的取数点。

`DlHcommFunction` 的绑定全部使用本轮的弱符号封装（[dlhcomm_function.cc:36-60](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/dlhcomm_function.cc#L36-L60)）：`HcclDlopen` 打开句柄、`HcclDlsym` 绑定 `HcclThreadResGetInfo` / `HcclConfigGetInfo` / `HcommThreadResGetInfo` 三个指针，析构时 `HcclDlclose`。可以看到 `HcclDlopen/HcclDlsym/HcclDlclose` 已经成为全部三处 dlopen 点（总入口 `hcomm_dlsym.cc:68`、kernel 侧 `hcomm_device_dlsym.cc:31`、ops 旁路 `dlhcomm_function.cc:55`）的统一入口。

#### 4.4.4 代码实践

1. **实践目标**：确认 `HCCL_CONFIG_TYPE_HCCL_ALGO` 从宏定义到消费点的完整链路。
2. **操作步骤**：`grep -rn "HCCL_CONFIG_TYPE_HCCL_ALGO" src/`，然后阅读 `HcclGetHcclAlgo` 全函数，再 `grep -rn "HcclGetHcclAlgo" src/ops/op_common/selector/ src/common/` 找它的调用者。
3. **需要观察的现象**：定义点与消费点各只有一处，中间隔着版本门控与懒加载单例。
4. **预期结果**：能写出「comm 配置字符串 → CostModel/HCCL_ALGO 过滤（u8-l3）」的接力说明。
5. 静态阅读即可完成。

#### 4.4.5 小练习与答案

**练习 1**：`HcclCommDlInit`（host_comm）与 `HcclDeviceCommDlInit`（device_comm）绑定同一批符号，为什么要分两个入口？
**答案**：一份源码双形态编译（host 侧 libhccl.so 与 AICPU kernel 侧 libccl_kernel.so，见 u4-l4），两边各自 dlopen 的库与链接环境不同，需要独立的句柄和绑定入口；声明也各自带 `extern "C"` 与版本桩。

**练习 2**：`DlHcommFunction` 与 `HcommDlInit` 都会 dlopen libhcomm.so，重复加载有开销吗？
**答案**：dlopen 对已加载库走引用计数，返回同一句柄，开销可忽略；真正区别是绑定时机与符号集——总入口在库构造期一次性绑全部域，`DlHcommFunction` 在 ops 层首次使用时懒加载、只绑三个指针，属于「不依赖构造顺序」的防御性设计。

## 5. 综合实践：跟踪一次「远端 Write + Notify」的完整落链

本讲的综合实践把四个模块串起来：**沿着 template 的 KernelRun → 数据搬运包装器 → hcomm_primitives_dl，说明一次远端数据 Write+Notify 如何经 dlsym 落到 HCOMM 基础通信层，并标注控制面/数据面**。

以 one-shot Mesh AllReduce（u3-l5 精读过它的三步搬移）为样本：

1. **实践目标**：写出从 `KernelRun` 到 libhcomm.so 的完整调用链，并给每一环标注「控制面 / 数据面」。
2. **操作步骤**：
   - **第 0 步（控制面，铺垫）**：确认本次执行使用的 thread 与 channel 从哪来——`HcclThreadAcquireWithConfig`（op_common.cc:1545）申请 threads，`HcclChannelGetRemoteMems`（op_common.cc:2838）取对端 cclBuff 远端地址，二者都是 `hccl_res_dl` 弱符号。模板里 `templateResource.threads` / `channels` 就是这批控制面成果；
   - **第 1 步（数据面入口）**：阅读 [ins_temp_all_reduce_mesh_1D_one_shot.cc:134-191](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L134-L191) 的 `RunAllReduce`：主流先 `LocalCopy`（本地拷贝），随后每个从 thread 对一个邻居 rank 调 `SendRecvBatchWrite(sendRecvInfo, threads[queIdx])`（:186 行），其中 `txDstSlice` 的基址 `linkSend.remoteCclMem.addr` 正是控制面拿到的**对端内存远端视图**；
   - **第 2 步（数据面同步前置）**：进入 [alg_data_trans_wrapper.cc:244-270](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc#L244-L270) 的 `DoSendRecvBatchTx`：先 `HcommChannelNotifyRecordOnThread(..., NOTIFY_IDX_ACK)` 向对端报「我的 buffer 可用了」，再 `HcommChannelNotifyWaitOnThread(..., NOTIFY_IDX_ACK, execTimeout)` 等对端的同样信号（host 侧只是向 device 下任务，并不阻塞，注释里写得很清楚）；
   - **第 3 步（数据面搬运）**：`RunBatchTransferAndNotify` → [RunBatchTransfer:134-163](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc#L134-L163)：把每个 DataSlice 转成 `HcclHcommBatchTransferDesc`（`MakeBatchTransDesc` 造 `WRITE` 型），最后调 `HcclHcommBatchTransferOnThread(thread, channel.handle, descs, n)`——一次提交全部写；
   - **第 4 步（数据面同步后置）**：若支持融合（[FuseNotifyToLastWriteReduceDesc:102-132](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc#L102-L132) 只对写归约生效），纯写场景则单独补一条 `HcommChannelNotifyRecordOnThread(..., NOTIFY_IDX_DATA_SIGNAL)` 告诉对端「数据写完了」；
   - **第 5 步（穿透 dlsym）**：`HcclHcommBatchTransferOnThread` 的实现在 [hcomm_primitives_dl.cc:90-99](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/hcomm_dlsym/hcomm_primitives_dl.cc#L90-L99)，转手调 `g_HcommBatchTransferOnThread`——该指针在 `HcommPrimitivesDlInit` 里由 `HcclDlsym(libHcommHandle, "HcommBatchTransferOnThread")` 绑定（:141），至此控制权交给 libhcomm.so 的基础通信层。若 HCOMM 过旧不支持批量接口，`DoSendRecvBatchTx` 开头的 `IsHcommBatchTransferOnThreadSupported()` 为假，回退到 `SendRecvWrite` 逐条 `HcommWriteOnThread` 的老路径（[alg_data_trans_wrapper.cc:428-463](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/template/wrapper/alg_data_trans_wrapper.cc#L428-L463)），逐条路径同样终结于本模块的弱符号。
3. **需要观察的现象**：整条链上，控制面动作只发生在执行前（thread/channel/buffer 的申请与拓扑查询）；`KernelRun` 之后的每一步——包括 ACK 同步、批量写、DATA_SIGNAL 通知——全部是数据面原语，且都以控制面借来的 `ThreadHandle`/`ChannelHandle` 为参数。这正是「数据面消费控制面成果、但不反过来触碰控制面内部」的架构约束落地的样子。
4. **预期结果**：能独立画出下面的落链图，并回答「为什么模板代码里看不到任何 dlopen/dlsym」——因为模板只面对包装器，包装器面对 `_dl` 弱符号，动态绑定被收敛在 `hcomm_dlsym` 一处。
5. 上板运行与抓取真实日志「待本地验证」（可用 `HCCL_DEBUG` 打开 `TraceDataSlice` 级日志观察每个 slice 的 src/dst 地址）。

落链图：

```text
[控制面] HcclThreadAcquireWithConfig / HcclChannelGetRemoteMems / HcclRankGraphGet*
              │  产出 threads / channels（含对端 cclBuff 远端地址）
              ▼
[数据面] InsTempAllReduceMesh1DOneShot::KernelRun → RunAllReduce
              │  LocalCopy + 每邻居 SendRecvBatchWrite
              ▼
     DoSendRecvBatchTx（ACK record/wait → 批量写 → DATA_SIGNAL record）
              ▼
     HcclHcommBatchTransferOnThread（HCCL 侧 C 包装，hcomm_primitives_dl.cc）
              │  g_HcommBatchTransferOnThread（HcclDlsym 绑定）
              ▼
     libhcomm.so::HcommBatchTransferOnThread —— HCOMM 基础通信层执行真实搬运
```

## 6. 本讲小结

- `hcomm_dlsym` 按**域**组织 dlsym 封装：资源域（`hccl_res_dl`）、原语域（`hcomm_primitives_dl`）、拓扑域（`hccl_rank_graph_dl`）、通信域配置域（`hccl_host_comm_dl`/`hccl_device_comm_dl`）等 11 个 `XxxDlInit`，由 `HcommDlInit` 统一驱动。
- **资源域与拓扑域是控制面**：Thread/Mem/Channel 的申请与 RankGraph 六接口查询，全部发生在算子执行前的资源计算阶段；**原语域是数据面**：Write/Read/Reduce/Notify/Fence 家族在 `KernelRun` 之后才登场，以控制面借来的 handle 为参数。
- 批量传输是数据面的性能关键：`HcclHcommBatchTransferDesc` 用 56 字节联合体把九种传输类型（写×归约×通知的组合）压成定长描述符，一次 `HcclHcommBatchTransferOnThread` 提交整串动作；不支持时逐条弱符号路径兜底。
- 拓扑六接口构成「层 → 实例 → rank → Endpoint」的查询漏斗，topo 匹配器与 channel 计算是它唯一的消费者，且依赖「rank 列表升序」这类跨仓隐式契约。
- 本轮演进三处落点：`INIT_SUPPORT_FLAG`/手工绑定统一改用 `HcclDlsym`（含三处 dlopen 收敛到 `HcclDlopen`）；`hccl_host_comm_dl.h` 新增 `HCCL_CONFIG_TYPE_HCCL_ALGO` 配置类型，打通「通信域级 HCCL_ALGO → 新选择器 CostModel」数据通路（承接 u8）；ops 层 `DlHcommFunction` 懒加载单例成为弱符号四步模式之外的第二个绑定范式。

## 7. 下一步学习建议

- 下一讲 **u6-l3 控制面/数据面分离架构**：把本讲的各 `_dl` 模块与 channel.h 的通道计算正式归类到两个面，从架构约束角度回答「为什么算子层不得耦合 HCOMM 控制面内部」。
- 若想先看数据面的另一端：回顾 **u5-l1**（AICPU kernel 如何在 device 侧消费这些原语）与 **u5-l2**（AICPU Task Cache——本模块末尾那五个 `HcommAicpuTsTaskCache*` 符号的完整故事）。
- 若对 `HCCL_CONFIG_TYPE_HCCL_ALGO` 的去向好奇：直奔 **u8-l3**（HCCL_ALGO 解析与 CostModel 过滤），看这个字符串如何影响算法选择。
