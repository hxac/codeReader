# u6-l5 异步通信传输后端：SDMA、URMA 与 RDMA

## 1. 本讲目标

本讲是单元六通信线的第三层深入。u6-l1 建立了通信 ISA 的四象限地图，u6-l3 讲了异步指令族 `TPUT_ASYNC`/`TPUT_ASYNC_NOTIFY`/`TGET_ASYNC` 的编程模型（BuildAsyncSession → 提交 AsyncEvent → Wait/Test），当时把「DMA 引擎」当成一个黑盒。本讲打开这个黑盒，学完后你应该能：

1. 说清 **SDMA / URMA / RDMA 三条异步传输路径**的队列模型差异，以及 `TPUT_ASYNC_NOTIFY` 的 `peer` 参数在三种引擎下分别被如何使用。
2. 理解 **两级后端选择**：指令级 `DmaEngine`（模板参数）与网卡级 `RdmaBackend`（编译期 `PTO_RDMA_BACKEND=HNS_1825`），以及 `RdmaInfo` workspace 如何把两者串联。
3. 走读 **HNS1825 网卡 backend** 的设备侧实现：在 AIV 上手工填充 WQE、敲队列门铃（doorbell）、轮询 CQE 完成事件。
4. 掌握 RDMA ST 的 **host 控制面**：端点发现（rootinfo / topology / IP 回退三级）、对称缓冲 MR 注册、经 MPI 交换对端元数据。
5. 看懂 `tput_async_rdma` / `tput_async_notify_rdma` 双 rank ST 用例如何用 **canary（金丝雀）字节** 校验「4 字节信号写恰好只写 4 字节」。

## 2. 前置知识

阅读本讲前，你需要先具备以下认知（均在 u6-l1/u6-l3 建立，这里只做一句话回顾）：

- **异步指令编程模型**：`TPUT_ASYNC<engine>` 一类指令返回 `AsyncEvent`，用 `BuildAsyncSession<engine>()` 一次构建会话、多次复用，`event.Wait(session)` 按提交顺序排空。
- **rank / peer**：参与通信的每个 NPU 进程是一个 rank；`peer` 是「本次操作发往哪个对端 rank」的运行期编号。
- **MR（Memory Region，注册内存）**：RDMA 网卡只能直接读写事先注册过的内存区域；注册后获得 `lkey`（本地访问键）与 `rkey`（远端访问键）。这是 RDMA 与 SDMA/URMA 最大的语义差异——**远端地址必须落在对端注册区内才合法**。
- **事件同步**（u3-l1）：`set_flag`/`wait_flag` 按（源流水线， 目的流水线， id）三元组配对。本讲会看到 RDMA 设备代码在标量（S）与搬运（MTE2/MTE3）流水线之间反复使用它。
- **WQE / CQE**：RDMA 网卡队列模型的基本词汇。WQE（Work Queue Element，工作队列元素）是提交给网卡的一项任务描述；CQE（Completion Queue Element，完成队列元素）是网卡回填的完成凭证。发送队列 SQ 与完成队列 CQ 都是环形队列，用 head/tail 指针维护。
- **大端 / 小端**：网络与 RoCE 网卡内部通常按大端解释数据，而 AscendC 标量是小端，两者交界处必须显式字节序转换。
- **dcci**：data cache clean/invalidate 内建指令，用于让标量核丢弃缓存副本、重新从 GM 读最新值——网卡绕过 AI Core 缓存直接写 GM 时必不可少。

一个值得先建立的直觉：**SDMA/URMA 是「总线型 DMA」，而 RDMA 是「以太网卡型 DMA」**。前两者的队列由 CANN 运行时托管；后者的队列（SQ/CQ/WQE/CQE/doorbell）是 RoCE 协议标准结构，PTO 在 AIV 标量代码里直接手写这些结构——这就是为什么 RDMA 后端代码读起来像在写一个迷你网卡驱动。

## 3. 本讲源码地图

本讲涉及的关键文件（按「公共声明 → 设备侧 → host 控制面 → 测试」排列）：

| 文件 | 作用 |
|---|---|
| [include/pto/comm/pto_comm_inst.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp) | 通信指令公共声明；`TPUT_ASYNC(_NOTIFY)`/`TGET_ASYNC` 的 engine 模板参数与 peer 重载在此定义 |
| [include/pto/comm/comm_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp) | `DmaEngine` 枚举（SDMA/URMA/RDMA 三值） |
| [include/pto/comm/rdma_backend.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/rdma_backend.hpp) | `RdmaBackend` 枚举（NONE/HNS_1825） |
| [include/pto/comm/async_common/async_event_impl.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp) | 引擎无关的 `BuildAsyncSession` 各重载与 `AsyncEvent::Wait/Test` 分发 |
| [include/pto/comm/async_common/async_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp) | `AsyncSession` 聚合类型（含 RDMA 专属字段） |
| [pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp) | **RDMA 设备侧分发入口**：BuildSession/Write/Read/WriteNotify/Wait/Test 按 backend 分发 |
| [pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp) | `RdmaInfo` workspace 头与 `RdmaMemInfo`（MR 表）布局，host/device 共享 |
| [pkg_inc/pto/comm/async/rdma/rdma_device_common.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_device_common.hpp) | 事件句柄编解码（Encode/Decode/IsErrorHandle）与 workspace 头校验 |
| [pkg_inc/pto/comm/async/rdma/rdma_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_types.hpp) | 设备侧执行/事件上下文、`RdmaSendWr`、UB 暂存区约定 |
| [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp) | **HNS1825（Hi1825 RoCE）设备侧实现**：WQE 填充、doorbell、CQ 轮询 |
| [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp) | 网卡可见的 SQ/CQ 上下文、WQE/CQE 位域结构与常量 |
| [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp) | A5 侧 `TPUT_ASYNC_NOTIFY` 的三引擎分流（SDMA 回退 / URMA / RDMA），含 AtomicAdd 拒绝点 |
| [include/pto/comm/async/rdma/rdma_workspace_manager.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/rdma/rdma_workspace_manager.hpp) | **host 控制面门面**：Preflight 架构探测、Init/Finalize 生命周期 |
| [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_arch.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_arch.hpp) | 运行期 SoC 探测（仅 A5/3510 支持本后端） |
| [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp) | HNS1825 host 实现：建端点 → 注册 MR → 建 channel → 拷 RdmaInfo 入设备内存 |
| [tests/npu/a5/comm/st/CMakeLists.txt](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/CMakeLists.txt) | 把环境变量 `PTO_RDMA_BACKEND` 翻译成编译期宏 |
| [tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md) | RDMA ST 的运行方式、端点发现顺序与环境变量表 |
| [tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp) | 三个 RDMA ST 目标共用的测试内核与 host runner |
| [tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp) | 测试侧端点发现：phyId 解析、IP 三级回退、MPI 交换对端元数据 |
| [tests/npu/a5/comm/st/testcase/tput_async_notify_rdma/main.cpp](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_notify_rdma/main.cpp) | `Int32SetAndCanaries` gtest 入口（双 rank） |

注意目录归属：**设备侧与网卡相关的头在 `pkg_inc/`**（按 CANN 约定不对外暴露的内部头，见 u1-l3），**host 控制面门面 `rdma_workspace_manager.hpp` 在 `include/`**。两条 include 路径都加进了 ST 编译（[tests/npu/a5/comm/st/CMakeLists.txt:93-99](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/CMakeLists.txt#L93-L99)），所以同一份 `pto/comm/async/rdma/...` 风格的包含名能同时解析到两处。

## 4. 核心概念与源码讲解

### 4.1 传输后端分发：DmaEngine 与 RdmaBackend 的两级选择

#### 4.1.1 概念说明

异步通信指令的「后端」其实由两个正交的选择组成：

1. **指令级引擎 `DmaEngine`**——用户在调用点用模板参数指定，决定数据走哪类传输机构。定义在 [include/pto/comm/comm_types.hpp:118-126](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/comm_types.hpp#L118-L126)：SDMA 支持 2D 搬运、URMA 支持 1D 搬运（HCCP V2 Jetty，仅 NPU_ARCH 3510）、RDMA 则由 `RdmaBackend` 进一步指明编译进二进制的网卡实现。
2. **网卡级 `RdmaBackend`**——RDMA 引擎之下「具体哪张网卡」的选择。当前枚举只有两个值，见 [include/pto/comm/rdma_backend.hpp:19-24](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/rdma_backend.hpp#L19-L24)：`NONE` 与 `HNS_1825`，且**一个进程最多激活一个 RDMA 后端**。

这个两级结构与 u6-l1 讲过的「公共声明在 include、实现在 pkg_inc」完全同构：`DmaEngine` 是 ISA 面孔上的词，`RdmaBackend` 是工程装配上的词。

#### 4.1.2 核心流程

`RdmaBackend` 的选择发生在 **CMake configure 阶段**，不是运行期：

```text
环境变量 PTO_RDMA_BACKEND=HNS_1825
        │  (tests/npu/a5/comm/st/CMakeLists.txt 读取 $ENV{PTO_RDMA_BACKEND})
        ▼
编译定义 PTO_RDMA_SUPPORTED + PTO_RDMA_BACKEND_HNS_1825_SUPPORTED
        │  (对 host 与 device 目标同时生效)
        ▼
pkg_inc 头里所有 #ifdef PTO_RDMA_BACKEND_HNS_1825_SUPPORTED 的分支被打开
        ▼
运行期：设备代码读 workspace 头 RdmaInfo.backend 字段做 switch 分发
```

关键点：**生成的二进制不再读这个环境变量**。换了 `PTO_RDMA_BACKEND` 必须重新 configure + 编译，这也是 README 强调「改变量后不要用 `-w/--without-build` 复用旧二进制」的原因。

#### 4.1.3 源码精读

先看 CMake 的翻译逻辑——只有 `HNS_1825` 一个合法值，空值静默禁用，其他值告警并禁用：

[tests/npu/a5/comm/st/CMakeLists.txt:14-27](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/CMakeLists.txt#L14-L27) 把环境变量翻译成 `PTO_RDMA_SUPPORTED` 与 `PTO_RDMA_BACKEND_HNS_1825_SUPPORTED` 两个编译定义；未设置/空值打印「RDMA disabled」；不支持的值打印 WARNING。两个宏成对出现，前者管「有没有 RDMA」，后者管「是哪张网卡」。

再看指令侧：`TPUT_ASYNC` 的 engine 模板参数与 peer 重载在 [include/pto/comm/pto_comm_inst.hpp:343-367](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L343-L367)——带 peer 的重载只在 A5 与 CPU stub 下声明，其文档注释一语道破三引擎的 peer 语义：「For URMA and RDMA: peer selects the per-peer queue and memory metadata; For SDMA: peer is ignored; addressing comes from the GlobalTensor VA」。`TGET_ASYNC` 的对应重载在 [include/pto/comm/pto_comm_inst.hpp:411-427](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_inst.hpp#L411-L427)，措辞相同。

peer 重载落到 A5 实现后的分流注释同样明确——[include/pto/comm/a5/async/TPutAsync.hpp:165-185](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/a5/async/TPutAsync.hpp#L165-L185) 写着「peer overload: URMA and RDMA use peer for queue/memory selection; SDMA ignores peer」，三个 `if constexpr` 分支分别转发到 SDMA（忽略 peer 调无 peer 版）、URMA 与 RDMA 实现。

三引擎对照表（本讲最重要的速查表）：

| 维度 | SDMA | URMA | RDMA |
|---|---|---|---|
| 队列宿主 | CANN 运行时托管 | HCCP V2 Jetty | RoCE 网卡 SQ/CQ（WQE/CQE 由 AIV 手写） |
| peer 参数 | **忽略**，远端 VA 来自 GlobalTensor | 选择 per-peer 队列与内存元数据，另选 notify 资源区 | 经 `MakeExecContext` 写入 `ctx.destRankId`，索引 per-peer SQ/CQ 与远端 MR 的 rkey |
| 远端地址合法性 | 由运行时翻译 | 注册内存基址 + 对称布局偏移 | 设备侧显式校验 `[addr, addr+len)` 落在对端 MR 内 |
| notify 形式 | A5 上退化为同步 MTE + 标量 SET/AtomicAdd | `__urma_put_async_notify` 支持 Set 与 AtomicAdd | **仅 Set**（fence 后的 4 字节 inline WRITE） |
| 完成事件 | flag payload ring / postId | CQE 计数（targetCqe） | 事件句柄编码 (destRankId, SQ head) |
| 典型场景 | 片内/HCCL 窗口路径 | 3510 片间 1D 大块 | 走 RoCE 以太网的跨机传输 |

#### 4.1.4 代码实践

**实践目标**：验证「两级选择」是编译期事实，并整理一份宏清单。

**操作步骤**：

1. 打开 [tests/npu/a5/comm/st/CMakeLists.txt](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/CMakeLists.txt)，确认 `PTO_RDMA_BACKEND` 只在 configure 时读取一次（第 17 行 `$ENV{PTO_RDMA_BACKEND}`）。
2. 用 Grep 在 `pkg_inc/` 下搜索 `PTO_RDMA_BACKEND_HNS_1825_SUPPORTED`，统计它出现在多少个文件、每个文件的 `#ifdef` 包住了什么（提示：分发入口里每个 switch case 都被它包住）。
3. 不设置该环境变量直接 configure 一次（若本机有 CANN 环境），观察 CMake 输出中的「PTO RDMA disabled for this build: PTO_RDMA_BACKEND is unset or empty」。

**需要观察的现象**：`rdma_async_intrin.hpp` 中所有 `case RdmaBackend::HNS_1825:` 分支都在 `#ifdef` 内——若宏未定义，设备侧分发只剩 `default: return EncodeErrorHandle(kRdmaBackendUnavailableError)`。

**预期结果**：整理出「环境变量 → 编译定义 → 代码分支」三列对照表。本机无昇腾环境时步骤 3 标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RdmaBackend` 不做成运行期参数，而要绑死在编译期？

**参考答案**：网卡实现直接决定设备代码里包含哪些头、调用哪些内建（如 HNS1825 的 `st_dev` 敲硬件门铃），这些无法在运行期动态绑定；同时 workspace 中 `RdmaInfo.backend` 是按「进程唯一后端」设计的（[pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp:34-36](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp#L34-L36) 注释明确「A process/session selects exactly one backend」），编译期单选可以避免多套网卡布局同时驻留二进制。

**练习 2**：`DmaEngine` 和 `RdmaBackend` 各自回答什么问题？

**参考答案**：`DmaEngine` 回答「这条异步指令走哪类传输机构」（用户在调用点用模板参数选）；`RdmaBackend` 回答「RDMA 这一类里编译进了哪张网卡的具体实现」（CMake 阶段由 `PTO_RDMA_BACKEND` 决定，设备代码运行期从 workspace 头读出做 switch）。

### 4.2 RDMA intrin 入口：rdma_async_intrin 的设备侧分发

#### 4.2.1 概念说明

[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp) 是公共异步代码进入 RDMA 世界的**唯一设备侧入口**。它的职责非常薄：校验 workspace 头、按 `info->backend` 分发到具体网卡实现、把网卡会话拍平成引擎无关的 `AsyncSession`。理解它的关键是三个数据结构：

- **`RdmaInfo`（workspace 头，64 字节）**：放在 workspace 起始处的进程级元数据表，字段见 [pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp:36-48](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp#L36-L48)：`magic`（`0x52444d41` 即 \"RDMA\" 四字符）、`version`、`backend`、`qpNum`、`rankCount`，以及四根数组指针 `sqPtr/rqPtr/scqPtr/rcqPtr`（SQ/RQ/发送 CQ/接收 CQ 上下文数组）和一根 `memPtr`（per-rank MR 表）。它由 host 控制面生成后**拷贝到设备内存**，内核拿到的 workspace 指针就指向它。
- **`RdmaMemInfo`（MR 表项，24 字节）**：[pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp:52-57](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp#L52-L57)，含 `size/addr/lkey/rkey`，按 rank 索引——「每个 rank 的对称缓冲注册在哪、键是多少」全在这张表里。
- **事件句柄编码**：RDMA 的 `AsyncEvent.handle` 是 64 位复合值，高 32 位是 destRankId、低 32 位是 SQ head，见 [pkg_inc/pto/comm/async/rdma/rdma_device_common.hpp:21-43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_device_common.hpp#L21-L43) 的 `EncodeHandle/DecodeHandle/IsErrorHandle`；错误句柄用保留 rank 值 `0xffffffff` 标记，错误码放在低 32 位。这样 `Wait(handle)` 就知道「去哪个 rank 的 CQ 等到第几个 WQE」。

#### 4.2.2 核心流程

一次完整 RDMA 异步写（用户视角 → 设备视角）：

```text
用户：BuildAsyncSession<RDMA>(scratchTile, workspace, myPe, session, syncId)
  └─ MakeTmpBufferFromTile：把 UB 上的 256B scratch tile 变成 RdmaTmpBuffer
  └─ rdma::BuildSession(workspace, myPe, ...)
       ├─ IsWorkspaceHeaderValid(info)：magic/version/backend/rankCount/qpNum 全部合格？
       └─ switch (info->backend) → hns_1825::BuildSession(...)   // 填 RdmaSession
  └─ StoreSession：RdmaSession(RDMA 专属) → AsyncSession(引擎无关)
       engine=RDMA, rdmaBackend, myPe, qpIdx, contextGm, tmpBuf, syncId

用户：TPUT_ASYNC<RDMA>(dst, src, session, peer)
  └─ a5/TPutAsync.hpp → rdma::Write(session, dst, src, len, peer)
       └─ MakeExecContext(session, peer)：ctx.destRankId = peer   ← peer 在这里落地
       └─ switch (ctx.backend) → hns_1825::Write(...)             // 返回 64 位句柄

用户：event.Wait(session) / event.Test(session)
  └─ AsyncEvent::Wait 按 session.engine 分发 → rdma::WaitEvent(handle, session)
       └─ DecodeHandle(handle) → (destRankId, curHead) → hns_1825::PollCq 等到 head
```

#### 4.2.3 源码精读

**会话构建与分发**。[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:27-43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L27-L43) 是 peer 无关版 `BuildSession`：先把 `session = {}` 清零，把 workspace 指针重解释为 `__gm__ RdmaInfo*`，校验头之后按 `info->backend` 分发。注意两个细节：其一，`switch` 的 `HNS_1825` case 整体包在 `#ifdef` 里，宏未定义时只剩 `default: return false`；其二，backend 选择读的是 **workspace 里的运行期字段**，而不是编译期宏——宏决定「有哪些 case 可选」，workspace 决定「实际走哪个 case」。

**RdmaSession → AsyncSession 的拍平**。[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:64-77](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L64-L77) 的 `StoreSession` 把网卡会话逐字段拷进引擎无关的 `AsyncSession`：`engine = DmaEngine::RDMA`、`rdmaBackend`、`myPe`、`qpIdx`、`contextGm`、`tmpBuf`、`syncId`。对照 [include/pto/comm/async_common/async_types.hpp:102-125](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_types.hpp#L102-L125) 可以看到 `AsyncSession` 里 SDMA 与 RDMA 的字段并存（SDMA 的 `channelGroupIdx/blockBytes`、RDMA 的 `rdmaBackend/myPe/qpIdx`）——这正是 u6-l3 讲过的「一份会话类型服务所有引擎」的物证。

**peer 的落地点**。[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:100-121](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L100-L121) 的 `MakeExecContext(session, peer)` 把调用点传入的 peer 写进 `ctx.destRankId`；不带 peer 的重载则回落到 session 里构建时绑定的 `destRankId`（[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:167-170](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L167-L170)）。也就是说：**peer 是每次调用的瞬时选择，session 只携带「我是谁」（myPe）与队列索引**，这解释了为什么一个 session 可以给多个对端发数据。

**数据面三个入口**。`Write`（远端写，dst 在对端）、`Read`（远端读，src 在对端）、`WriteNotify`（远端写 + 信号）都以同样的 switch 结构分发，见 [pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:123-159](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L123-L159)。不可用的 backend 统一返回 `EncodeErrorHandle(kRdmaBackendUnavailableError)`（错误码 `0x21000`，定义在 [pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp:61-65](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_workspace_types.hpp#L61-L65)，该段还定义了 workspace 无效、backend 不匹配、会话构建失败、不支持的操作等错误码）。

**远端地址换算**。[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:190-204](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L190-L204) 的 `PeerMrBaseAddr(workspace, peer)` 从 MR 表读出对端注册区基址——内核用「peerBase + 区内偏移」算出远端目标 VA。这是 RDMA 编程的关键习惯：**远端 VA 不是全局统一编址，而是「对端 MR 基址 + 对称布局偏移」**。

**完成事件**。[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:206-254](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L206-L254) 的 `WaitEventStatus/WaitEvent/TestEvent`：句柄为 0 直接视为已完成；错误句柄把错误码原样返回；正常句柄按 backend 分发。注意 `TestEvent` 对错误句柄返回 `true`（[pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:236-240](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L236-L240)）——「已终结」即可返回，错误详情由随后的 Wait 暴露。

最后看公共侧如何接入：[include/pto/comm/async_common/async_event_impl.hpp:71-101](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp#L71-L101) 定义了两个 RDMA 专属 `BuildAsyncSession` 重载——`(scratchTile, workspace, myPe, session)` 的 **peer 无关版**（注释说明显式 peer 的 TPUT/TGET 重载可复用它服务多个对端）与 `(scratchTile, workspace, destRankId, myPe, session)` 的 **peer 绑定版**（保留给不带显式 peer 的旧式调用）。`AsyncEvent::Wait/Test` 则在 [include/pto/comm/async_common/async_event_impl.hpp:108-148](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp#L108-L148) 按 `session.engine` 三路分发，RDMA 路径就在上面那组函数里。

顺带一提 scratch 约定：RDMA 的 UB 暂存区只需要 **256 字节**（`kRdmaScratchBytes`，[pkg_inc/pto/comm/async/rdma/rdma_types.hpp:25-30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_types.hpp#L25-L30)），因为数据面是 GM 直达、UB 只用来在 GM 里组装 WQE/CQE 这类控制记录。`MakeTmpBufferFromTile`（[pkg_inc/pto/comm/async/rdma/rdma_types.hpp:34-42](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_types.hpp#L34-L42)）用两条 static_assert 强制 scratch tile 必须是 `pto::Tile` 且位于 Vec(UB)——对照 u2-l4 的手工 UB 规划，这里是「通信指令自带 UB 需求」的实例。

#### 4.2.4 代码实践

**实践目标**：手工追踪一次会话构建的字段流动。

**操作步骤**：

1. 从 [include/pto/comm/async_common/async_event_impl.hpp:74-85](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async_common/async_event_impl.hpp#L74-L85) 出发，抄下 `BuildAsyncSession<RDMA>` 的实参列表。
2. 跟到 `rdma::BuildSession` → `hns_1825::BuildSession`（4.3 节会读后者），记录 `RdmaSession.execCtx / eventCtx` 各字段的赋值来源。
3. 再跟 `StoreSession`，画一张三列对照表：`RdmaSession 字段 → AsyncSession 字段 → 该字段的用途`。

**需要观察的现象**：`AsyncSession` 里 `myPe`、`qpIdx`、`rdmaBackend`、`contextGm` 四个字段只在 RDMA 路径被填充；SDMA 字段保持默认值。

**预期结果**：得到一张 10 行左右的字段映射表。纯源码走读，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WaitEvent` 需要 `session` 而 handle 里已经编码了 destRankId？

**参考答案**：句柄只记录「等哪个 rank 的 CQ 等到第几项」，但轮询 CQ 需要 workspace 地址（`contextGm`）、UB 暂存区（搬 CQE 用）与 `syncId`（MTE 事件配对）——这些都在 session 里。看 [pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp:113-121](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/rdma_async_intrin.hpp#L113-L121) 的 `MakeEventContext`：它从 session 抽出的正是这四样。

**练习 2**：`AsyncEvent` 句柄的高 32 位为什么选 destRankId 而不是 myPe？

**参考答案**：完成事件要定位的是「哪个对端方向上的 CQ 进度」——HNS1825 为每个 (peer, qp) 组合维护独立 CQ（见 4.3 节 `scqPtr + (pe * qpNum + qpIdx) * sizeof(RoceCqCtx)` 的索引式），所以句柄必须携带目的 rank 才能找回对应 CQ；myPe 在 session 构建时已固化进上下文，无需重复携带。

### 4.3 HNS1825 backend：在 AIV 上手写 WQE 与 doorbell

#### 4.3.1 概念说明

[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp) 实现 Hi1825 RoCE 网卡的设备侧后端。文件头注释（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:11-14](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L11-L14)）概括了全部要点：从 AIV 投递 RDMA WRITE/READ、WQE/CQE 暂存借用会话的 UB scratch、**不支持原子操作**。

网卡可见的数据结构在 [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp)：`SqContext`/`CqContext`（SQ/CQ 上下文，含环形缓冲地址、深度、head/tail 镜像地址、软件/硬件 doorbell 地址，[L25-L53](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp#L25-L53)）、16B WQE 控制段 + 32B RDMA 任务段 + 16B 数据段（[L55-L99](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp#L55-L99)）、32B CQE（[L101-L112](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp#L101-L112)）、64 位 SQ doorbell 寄存器位域（[L114-L131](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp#L114-L131)）。文件末尾用五条 `static_assert` 钉死这些结构的字节尺寸（[L176-L180](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp#L176-L180)）——网卡布局错一个字节整条链路都静默失效，所以尺寸检查必须前移到编译期，这与 u2 系列讲的「硬件约束 static_assert 化」是同一哲学。

关键常量（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp:133-151](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp#L133-L151)）：一条 WQE 占 64B（`kWriteReadWqeSize = kWqebbSize = 64`）；SQ 深度剩余 `kPollCqThreshold = 10` 时主动排空 CQ；单次传输上限 `kMaxTransferBytes = 0x7fffffff`；轮询超时 60 秒，且周期→微秒换算率随架构不同（A5 除以 1000，A2/A3 除以 50，由 [L145-L149](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_types.hpp#L145-L149) 的条件编译选择）——`AscendC::GetSystemCycle` 的节拍是架构相关的。

#### 4.3.2 核心流程

一次 `hns_1825::Write` 的完整时序（`PostSendReadWrite<OP_RDMA_WRITE>`）：

```text
1. 定位队列：sqCtx = scqPtr/sqPtr 数组中第 (destRankId * qpNum + qpIdx) 项
2. 读 head/tail 镜像（ld_dev 绕过标量缓存）
3. 水位检查：head - tail ≥ depth - 10 ？→ 先 PollCq 排空在途完成
4. 查 MR 表：wr.rkey = 对端 MR 的 rkey；wr.lkey = 本端 MR 的 lkey
5. 在 UB 组装 64B WQE：控制段(16B) + 任务段(32B) + 数据段(16B)
   （NIC 可见字段全部 Htobe* 转大端；载荷地址/长度按 NIC 格式填充）
6. WriteInvalidWqebb：把下一个 WQEBB 的 owner 字节预置为非法，让网卡停在这里
7. WriteUbToGmWithSync：S→MTE3 事件 + copy_ubuf_to_gm 把 WQE 拷进 SQ 槽位 + MTE3→S 事件 + dcci
8. RingSqDoorbell：更新 head 镜像 → 写软件 doorbell（大端 PI）→ st_dev 写 64 位硬件 doorbell
9. 返回 EncodeHandle(destRankId, 新 head) —— 这就是 AsyncEvent.handle

完成等待（WaitEvent → PollCq）：
  读 CQ tail → 逐项 dcci 失效 + GM→UB 搬 CQE → 检查 owner 位与 opcode
  → tail 推进到目标 → 敲 CQ doorbell（仅当 tail 确实推进过）
```

`WriteNotify` 在此之上变成**双 WQE 序列**：载荷 WQE（普通 RDMA WRITE）+ 信号 WQE（带 fence 的 4 字节 inline RDMA WRITE）。fence 位保证网卡先完成载荷写、再发出信号写，这就从队列层面实现了「载荷先于信号可见」的 u6-l3 语义承诺。

#### 4.3.3 源码精读

**（1）字节序与缓存原语**。文件先备好三组工具：`Htobe16/32/64` 手工字节序交换（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:43-58](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L43-L58)）；`ReadU32Gm/WriteU32Gm` 用 `ld_dev/st_dev` 直读直写 head/tail 镜像以绕过标量数据缓存（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:60-63](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L60-L63)）；`WriteUbToGmWithSync` 把「UB 里组装好的记录」发布到 GM（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:65-76](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L65-L76)）。后者是本文件最重要的一段同步代码，值得逐行读：

```cpp
// 示例代码（摘自 hns_1825_backend.hpp:68-76，注释为本讲所加）
set_flag(PIPE_S, PIPE_MTE3, syncId);    // 标量写 UB 完成 → 通知 MTE3
wait_flag(PIPE_S, PIPE_MTE3, syncId);   // MTE3 等 UB 内容就绪
copy_ubuf_to_gm_align_v2(/*GM 目的*/, /*UB 源*/, ..., size, ...);  // WQE 落入 SQ 槽位
set_flag(PIPE_MTE3, PIPE_S, syncId);    // 拷贝完成 → 通知标量
wait_flag(PIPE_MTE3, PIPE_S, syncId);   // 标量等拷贝真正完成
```

其注释（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:65-67](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L65-L67)）特别强调：拷贝后的同步**必须是 MTE3→S 而不是 MTE3→MTE2**，因为 RoCE 网卡走的是标量 doorbell 路径的消费者。这是 u3-l1「事件方向由消费者所在流水线决定」规则的真实用例。反方向的 `ReadGmToUb`（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:78-86](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L78-L86)）用 MTE2 搬入后以 MTE2→S 事件交给标量，配对逻辑一致。

**（2）WQE 组装**。`FillWqeCtrlSeg` 填 16B 控制段（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:183-204](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L183-L204)）：owner 位随 `curHead & depth` 的圈数翻转（网卡用它区分「同一槽位的新旧内容」）、`wf_bdsl` 段长、CQE 产生标志。`FillWqeTaskSeg` 填 32B 任务段（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:232-258](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L232-L258)）：opcode 区分 WRITE(0x04)/READ(0x08)、`signal=1` 要求产生 CQE、`fence` 参数控制栅栏、`va/rkey` 是远端目标与访问键、`data_len` 是载荷长度——除 `imm_data` 外全部转大端。`FillWqeDataSeg` 填 16B 数据段（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:260-271](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L260-L271)）：本地源地址、长度、`lkey`，并置 `kNextSgeInvalid` 位表示「单 SGE、无下一段」。

`FillWqeWriteRead` 把三段串起来（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:281-291](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L281-L291)）：UB 组装 → `WriteInvalidWqebb(sqCtx, curHead + 1)` 预置下一槽位为非法（网卡扫描到非法 owner 就停下，[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:273-279](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L273-L279)）→ `WriteUbToGmWithSync` 拷入 SQ。注释点明设计意图：**先在 UB 把整条 WQE 组好再一次拷入，避免对 GM 的零碎标量写**。

**（3）inline 信号写**。`FillWqeInlineSet` 组装「4 字节立即数版」WQE（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:293-306](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L293-L306)）：控制段由 `FillInlineWqeCtrlSeg` 生成（多置一个 `kDataInlineShift` 位表示数据内联在 WQE 里，[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:206-230](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L206-L230)），任务段的 fence 置位，`signalValue` 直接写进 WQE 尾部。注意其文档注释的细节：**信号值本身保持主机字节序**——它是载荷数据而不是控制字段，网卡会原样搬到远端内存，两端都是小端 Ascend，转一次反而错。这就是 RDMA notify「仅 Set」的物理形态：一次普通 4 字节写，没有任何读改写。

**（4）投递与门铃**。`PostSendReadWrite` 是 WRITE/READ 共用的投递主流程（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:343-379](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L343-L379)）：定位 `(pe * qpNum + qpIdx)` 的 SQ 上下文 → 读 head/tail → 水位不足先 `PollCq` 排空 → 从 MR 表取对端 `rkey` 与本端 `lkey`（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:367-370](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L367-L370)）→ 填 WQE、dcci、head+1 → `RingSqDoorbell`。`RingSqDoorbell`（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:308-341](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L308-L341)）依次做三件事：写 head 镜像（主机序）、把大端 PI 写到软件 doorbell 并 dcci、把拼好的 64 位 `SqDoorbell` 位域用一次 `st_dev` 写进硬件 doorbell 寄存器，最后 `pipe_barrier(PIPE_ALL)` 收束所有流水线。

`PostSendWriteNotify`（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:381-429](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L381-L429)）在两个 WQE 都入队后**只敲一次门铃**，其注释解释了不变量：两条 WQE 都带 signal、各占一个 WQEBB/CQE，因此既有的 head/tail 映射保持成立，SQ 一次性发布即可。开头的注释还说明 workspace 会拒绝「深度 ≤ 阈值」的队列，保证排空后必然放得下两条 WQE。

**（5）完成轮询**。`PollCq`（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:120-181](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L120-L181)）逐个消费 CQE：每项先 `dcci` 失效缓存行、`ReadGmToUb` 搬入、取 opcode 字段（CQE dw1 的高 5 位）判断是否有效、`CheckCqeOwner` 用 owner 位与消费进度的奇偶匹配防重放（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:94-101](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L94-L101)）。超时判定用 `AscendC::GetSystemCycle` 差值比较；错误 CQE 的 syndrome 直接作为状态返回。两个易错点都有注释保护：入口处若 `curTail` 已越过目标（共享 CQ 被后续事件推进过）立即成功返回——32 位单调索引在在途数远小于 2^31 时无歧义；超时时**不敲 CQ doorbell**，因为消费索引没有推进。轮询到新进度后才调 `RingCqDoorbell`（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:103-118](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L103-L118)）：更新 CQ/SQ 两处 tail 镜像（主机序），再把 24 位消费索引以大端发布到 CQ 软件 doorbell。

`TestEvent` 是「只看不取」的非阻塞版本（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:622-661](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L622-L661)）：只窥探 `curHead-1` 那一项 CQE、不推进 tail；窥探前同样必须 `dcci`，其注释点明「NIC 异步更新 CQ 内存，一次性 Test 读之前必须使缓存副本失效」。

**（6）入口三件套与校验**。backend 对外的 `Write/WriteNotify/Read` 入口（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:529-584](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L529-L584)）都先校验再投递：`ValidateTransfer`（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:439-457](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L439-L457)）检查上下文有效、长度不超过 0x7fffffff、**本地与远端区间都完整落在各自 MR 内**（`IsRangeInsideMr` 用 `addr - base <= size - len` 一行完成包含判断且防溢出，[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:431-437](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L431-L437)）；`ValidateNotifySignal` 额外要求信号地址 4 字节对齐且落在对端 MR 内（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:459-468](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L459-L468)）。设备侧 `BuildSession` 的合法性清单在 [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:476-526](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L476-L526)：workspace 头有效、backend 匹配、scratch 至少容纳一条 WQE（64B）、`syncId ≤ 7`（u3 系列讲过事件 ID 只有 0~7）、myPe 与（可选的）destRankId 都小于 rankCount；绑定 destRankId 的重载还会顺带校验该对端 CQE 尺寸能装进 scratch。

**（7）头文件纪律**。文件顶部（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:23-28](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L23-L28)）只包含 `kernel_operator_sys_var_intf.h` 取 `AscendC::GetSystemCycle`，注释警告**不要**引入完整 `kernel_operator.h`——那会拖进 adv_api/hccl 并与 ST 侧 common.hpp 的符号冲突。这类「include 纪律」在多库叠加的设备代码里是真实约束，值得记住。

#### 4.3.4 代码实践

**实践目标**：把 `PostSendWriteNotify` 的双 WQE 序列画成时间线。

**操作步骤**：

1. 通读 [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:381-429](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L381-L429)，标出载荷 WQE 与信号 WQE 各自的 `curHead` 值、`FillWqe*` 调用与 `dcci` 位置。
2. 在时间线右侧标注网卡视角：两条 WQE 各产生一个 CQE，fence 保证先后。
3. 回答：为什么两次 `FillWqe*` 之间只 `++curHead` 而不敲门铃？

**需要观察的现象**：两次 `WriteUbToGmWithSync` 中间没有 `RingSqDoorbell`；门铃只在两条 WQE 都落位后敲一次。

**预期结果**：一张含「head 值 / WQE 类型 / fence / CQE 数」四列的双行时间线。第三问参考答案：门铃携带的是生产者索引 PI，网卡看到 PI 后会一次扫描所有有效 WQE；提前敲铃会让网卡在信号 WQE 未就绪时消费到非法 owner 而停下（虽然 `WriteInvalidWqebb` 能兜住，但会多一次停顿与恢复）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SQ/CQ 的 head/tail 镜像保持主机序，而 WQE 与软件 doorbell 转大端？

**参考答案**：镜像只有本核标量代码读写（`ReadU32Gm/WriteU32Gm`），两端都是小端 Ascend；WQE 控制字段与软件 doorbell 是**网卡消费**的数据，RoCE 网卡按大端解释（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:43-44](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L43-L44) 的注释「NIC consumes big-endian; AscendC is little-endian」）。分界标准是「谁消费这段字节」。

**练习 2**：`kPollCqThreshold = 10` 起什么作用？太小或太大会怎样？

**参考答案**：它是 SQ 的在途水位线——`head - tail` 距 depth 不足 10 时先排空 CQ 再投递，防止 SQ 满后无处写 WQE。太小则可能在深度本就小的队列上频繁排空、丧失流水重叠；太大（接近 depth）则排空时机太晚，遇到 `WriteNotify` 这类一次占两条 WQE 的操作时可能放不下（代码注释提到 workspace 会直接拒绝「深度 ≤ 阈值」的队列来保证排空后必然够用）。

**练习 3**：为什么 `PollCq` 超时时不能敲 CQ doorbell？

**参考答案**：doorbell 发布的是**消费索引**，代表「这些 CQE 我已经处理完、环形缓冲可以覆写」。超时意味着有 CQE 尚未就绪、消费索引没有推进；此时敲铃等于向网卡谎报进度，后续新 CQE 可能覆盖未被读取的旧 CQE。

### 4.4 端点发现与 MR 注册：host 控制面

#### 4.4.1 概念说明

设备侧代码运行的前提，是有人已经把 `RdmaInfo` 写进设备内存：建好 RoCE 端点、注册了通信缓冲（MR）、为每个对端建好 channel，并把 SQ/CQ/MR 元数据汇总成 workspace。这套工作在 **host 控制面**完成，分两层：

- **门面层** [include/pto/comm/async/rdma/rdma_workspace_manager.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/rdma/rdma_workspace_manager.hpp)：`RdmaWorkspaceManager` 类，提供 `Preflight / Init / Finalize / GetWorkspaceAddr` 生命周期。它按编译期宏选择内部实现，当前只有 HNS1825 一个分支。
- **实现层** [pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp)：`hns_1825::WorkspaceManager` 基于 HCOMM 的 RoCE 控制面做实事。

注意这些头都有 `#if defined(__CCE_KT_TEST__) #error` 守卫（如 [include/pto/comm/async/rdma/rdma_workspace_manager.hpp:17-19](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/rdma/rdma_workspace_manager.hpp#L17-L19)）——**host-only 头禁止混进设备编译单元**，这是 host/device 同文件混编（bisheng 的 `-Xhost-start/-Xhost-end`）下的防火墙。

另一个本讲专有概念是**端点发现（endpoint discovery）**：RDMA 是走 IP 网络的，发起连接前每个 rank 必须知道「我是哪张网卡、我的 RoCE IP 是什么、每个对端的 IP 与注册缓冲基址是什么」。ST 测试把这整套发现做成了三级回退 + MPI 交换的独立头文件（bootstrap）。

#### 4.4.2 核心流程

```text
RdmaWorkspaceManager::Init(config)                     [门面]
  └─ Preflight：ProbeArchitecture()                    [架构探测]
       运行期 dlsym(aclrtGetSocName) → SoC 名含 Ascend950/dav-3510/3510？
       且编译期 __NPU_ARCH__ 为 0 或 3510 → supported
  └─ hns1825Backend_.Init(rankId, rankCount, phyId, localIp, basePort,
                          peerIps, peerPhyIds, peerSymAddrs,
                          symmetricAddr, symmetricSize) [HNS1825 实现]
       1. 创建本端 RoCE 端点
       2. 注册对称通信缓冲 → 得到 lkey/rkey
       3. 为每个对端建一条 channel，等所有 channel 就绪
       4. 读取 ChannelEntity 的 SQ/CQ/MR 元数据 → 拷贝 RdmaInfo 到设备内存
  └─ GetWorkspaceAddr() → 设备内存上的 RdmaInfo 地址
       （内核启动时作为 workspace 参数传入，见 4.5）

测试侧 bootstrap（在 Init 之前把 config 凑齐）：
  phyId    ：ResolvePhyId()（dlsym 运行时符号）→ PTO_ROCE_PHYIDS[rank] → 设备 id 兜底
  本地 IP  ：/etc/hccl_rootinfo.json 的 CLOS 项
             → /var/run/ascend-topologyd/virtualTopology.xml（GetRoceIpFromXml）
             → PTO_ROCE_LOCAL_IP → PTO_ROCE_IPS[rank]
  basePort ：PTO_ROCE_BASE_PORT（默认 60032），全 rank MPI_Allgather 校验一致
  对端信息：MPI_Allgather 交换 IP、phyId、注册缓冲基址（peerSymAddrs）
```

#### 4.4.3 源码精读

**架构探测**。[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_arch.hpp:36-64](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_arch.hpp#L36-L64) 的 `ProbeArchitecture` 用两条线索判定「当前机器是不是 A5」：编译期 `__NPU_ARCH__`（host 编译单元可能不定义，为 0）与运行期 `dlsym` 取 `aclrtGetSocName` 的返回（含 `Ascend950`/`dav-3510`/`3510` 即 3510 系）。两条线索都必须同意——注释（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_arch.hpp:36-38](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_arch.hpp#L36-L38)）写明「Accept HNS1825 only when every available signal agrees with 3510」。门面层的 `Preflight` 在不支持时打印架构描述并返回 ERROR（[include/pto/comm/async/rdma/rdma_workspace_manager.hpp:81-104](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/rdma/rdma_workspace_manager.hpp#L81-L104)），ST 据此在非 A5 机器上直接失败而不是误跑。

**配置结构**。[include/pto/comm/async/rdma/rdma_workspace_manager.hpp:42-53](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/rdma/rdma_workspace_manager.hpp#L42-L53) 的 `WorkspaceConfig` 把 bootstrap 的全部产出打包：本 rank 的 `rankId/phyId/localIp`、公共 `basePort`、**全体 rank** 的 `peerIps/peerPhyIds/peerSymAddrs`，以及本 rank 的对称缓冲 `symmetricAddr/symmetricSize`。`Init` 与 `Finalize` 的门面分发在 [include/pto/comm/async/rdma/rdma_workspace_manager.hpp:106-144](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/rdma/rdma_workspace_manager.hpp#L106-L144)，`GetWorkspaceAddr` 在 [L148-L158](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/rdma/rdma_workspace_manager.hpp#L148-L158)——它返回的正是 4.2 节设备代码当作 workspace 用的那个指针。

**HNS1825 实现的四步流**。类头注释（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp:45-55](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp#L45-L55)）列出了完整流程与逆序回收：Init 按「建端点 → 注册对称缓冲 → 每 peer 一条 channel 并等全部就绪 → 读 ChannelEntity 的 SQ/CQ/MR 元数据、把 RdmaInfo 拷入设备内存」推进；Finalize 按「channel → 注册内存 → 端点 → 设备元数据」的反向所有权序释放。`Init` 签名与 `WorkspaceConfig` 字段一一对应（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp:72-75](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp#L72-L75)），`GetWorkspaceAddr` 一行返回 `rdmaInfoDevice_`（[L83-L84](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_workspace_manager.hpp#L83-L84)）。

**测试侧 bootstrap 的三级回退**。[tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp) 是纯 host 的端点发现（文件头注释 L11-18 概述三步）。三个解析函数：

1. `ResolvePhyId`（[L52-L87](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L52-L87)）：`dlsym` 依次尝试 `aclrtGetPhyDevIdByUserDevId`（优先）、已废弃的 `aclrtGetPhyDevIdByLogicDevId`、`rtGetDevicePhyIdByIndex`——注释提醒废弃版接的其实也是 userId。符号缺失时优雅降级，由 `ResolveBootstrapPhyId`（[L297-L309](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L297-L309)）先查 `PTO_ROCE_PHYIDS` 再退回 ACL 设备号。之所以执着于 phyId，是因为 **HCCP 与拓扑数据都用物理设备号，而不是 ACL 逻辑设备号**。
2. `ResolveLocalRdmaIp`（[L204-L225](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L204-L225)）：无第三方 JSON 依赖地扫描固定路径 `/etc/hccl_rootinfo.json`（路径常量见 [L49-L50](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L49-L50)），按 `"device_id"` 分块找到本 phyId 的 rank 块，再取块内 `"net_type":"CLOS"` 之后的第一个 IPv4。
3. `ResolveLocalRdmaIpFromVirtualTopology`（[L227-L259](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L227-L259)）：`dlsym` 取 HCOMM 拓扑组件的 `GetRoceIpFromXml`（必要时 `dlopen libtopoaddrinfo.so`），按 phyId 解析 topologyd 的 `virtualTopology.xml`。注释强调**只读不改**这两份系统文件，且老安装可能没有该符号、必须保留回退。

`ResolveBootstrapLocalIp`（[L311-L323](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L311-L323)）按「rootinfo → topology → `PTO_ROCE_LOCAL_IP` → `PTO_ROCE_IPS[rank]`」的顺序取值；`BootstrapConfig::Init`（[L417-L433](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L417-L433)）串起全流程：解析 phyId → 解析并**集体确认**本地 IP（任一 rank 缺 IP 则整体 SKIP 而非误报通过，[L325-L339](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L325-L339)）→ 确认 basePort 全 rank 一致（[L341-L373](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L341-L373)）→ `MPI_Allgather` 交换全体 IP（[L375-L393](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L375-L393)）与 phyId、注册缓冲基址（[L395-L412](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L395-L412)）。结构体头注释（[L263-L270](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L263-L270)）把这个三级回退总结得最清楚。

环境变量速查（与 [tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md:48-57](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md#L48-L57) 的表格一致）：

| 变量 | 作用 | 默认/说明 |
|---|---|---|
| `PTO_RDMA_BACKEND` | configure 期后端选择 | 唯一合法值 `HNS_1825` |
| `PTO_ROCE_PHYIDS` | 逗号分隔、按 MPI rank 索引的物理设备号 | 可选 |
| `PTO_ROCE_LOCAL_IP` | 本 rank 的 IPv4 兜底 | 优先于 `PTO_ROCE_IPS` |
| `PTO_ROCE_IPS` | 全体 rank 的 IPv4 列表 | 须与 rank 序一致 |
| `PTO_ROCE_BASE_PORT` | channel 基准端口 | 60032，全 rank 必须一致 |
| `PTO_ROCE_VERBOSE` | 置 1 打印端点/MR/channel/清理日志 | 诊断用 |
| `HCCL_RDMA_TC` / `HCCL_RDMA_SL` | HCOMM 流量类别 / 服务等级 | 132 / 4 |

README 的排障节（[tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md:61-66](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md#L61-L66)）还给出一个典型坑：HCOMM 找不到 HNS1825 verbs 提供者时需设 `IBV_EXTEND_DRIVERS` 指向驱动提供的 `libhrn5-rdmav34.so`。

#### 4.4.4 代码实践

**实践目标**：整理「端点发现 → MR 注册 → WR 发送 → 信号 canary 校验 → AsyncEvent 等待」全链路的环境变量与函数调用清单。

**操作步骤**：

1. 按本文 4.4.2 的流程图，从 `RdmaTestContext::Setup`（见 4.5）出发向下标注每一步调用的函数与其所在文件。
2. 对每个环节记录：需要哪些环境变量 / 系统文件、失败时的行为（ERROR 还是集体 SKIP）。
3. 特别注意 `peerSymAddrs` 的来源（`MPI_Allgather` 交换的本 rank 注册缓冲基址）与去向（设备侧 `PeerMrBaseAddr` 读的就是它注册进 MR 表的值）。

**需要观察的现象**：IP 解析三级回退中，前两级成功时环境变量被完全忽略；任一 rank IP 缺失时全体 SKIP。

**预期结果**：一张五列时序表（阶段 / 函数 / 输入 / 输出 / 失败语义）。纯源码走读。

#### 4.4.5 小练习与答案

**练习 1**：为什么 bootstrap 用 `dlsym` 动态取符号，而不是直接链接 ACL/HCOMM 库？

**参考答案**：这些符号（`aclrtGetSocName`、`GetRoceIpFromXml`、三个 phyId 映射函数）在不同版本的驱动/HCOMM 里有无不定；`dlsym` 缺符号时返回 nullptr，代码据此优雅降级到下一级回退（[tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp:17-18](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/backends/hns_1825/hns_1825_bootstrap.hpp#L17-L18) 注释「missing symbols degrade gracefully」），硬链接则会在老环境直接加载失败。

**练习 2**：`Preflight` 为什么在编译期宏之外还要做运行期 SoC 探测？

**参考答案**：host 编译单元可能不定义 `__NPU_ARCH__`（arch.hpp 注释明说），仅凭编译期宏无法判断；而二进制可能被拷到非 A5 机器上运行。运行期 `aclrtGetSocName` 是最终事实来源，两条线索一致才放行，避免在错误架构上触碰不存在的 RoCE 寄存器。

### 4.5 RDMA ST 实测：双 rank 用例、canary 与缓存序

#### 4.5.1 概念说明

RDMA ST 放在 `tests/npu/a5/comm/st/testcase/` 下三个目录：`tput_async_rdma`（远端写）、`tget_async_rdma`（远端读，编译期加 `PTO_RDMA_GET_TEST`）、`tput_async_notify_rdma`（远端写 + Set 通知）——README（[tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md:1-5](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md#L1-L5)）说明三者**共用同一份内核实现**。这与其他 ST「目录名 = 指令名」的组织略有不同：这里一个 kernel 文件服务三个目标。

测试的核心设计有三个，都值得当成模板学：

1. **对称缓冲布局**：每个 rank 的通信缓冲按 `[64 × int32 头部][sendBuf: count × T][recvBuf: count × T]` 排布，且**各 rank 布局完全相同**。远端目标 VA 不是直接拿到对端指针，而是 `PeerMrBaseAddr(peer) + 区内偏移`——u6-l3 讲过的「注册内存基址 + 对称布局偏移」寻址法在 RDMA 下的再现（[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:68-77](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L68-L77) 的文件头注释写明了这一点）。
2. **canary（金丝雀）字节**：在信号槽前后各放一个魔数（`0x13572468` 与 `0x24681357`），收完后回读——若 inline 信号写越界或写错宽度，canary 必然变化。这是验证「4 字节写恰好只写 4 字节」的经典手段。
3. **接收端的缓存序**：网卡写的载荷绕过 AI Core 缓存，接收方在看到信号后必须先 `dcci(ENTIRE_DATA_CACHE)` 再读载荷，否则可能读到本地缓存的旧值——**信号可见不等于数据可见**，中间隔一次显式缓存失效。

#### 4.5.2 核心流程

`TPutAsyncNotifyRdmaKernelImpl`（双 rank，root=rank0，目标是 peer=1）一图看懂：

```text
头部 256B 布局（rank 双方对称）：
  [0]    deviceStatus（设备侧状态字）
  [4]    canaryBefore = 0x13572468
  [8]    signal       = 0        ← 远端信号槽
  [12]   canaryAfter  = 0x24681357
  [256+] sendBuf[256] / recvBuf[256]

root rank（发送方）                          非 root rank（接收方）
────────────────────────                    ──────────────────────────────
BuildAsyncSession<RDMA>(scratch,             限次轮询 TTEST(signal == 37)
  workspace, myPe, session, syncId)            （上限 10000000 次）
PeerMrBaseAddr(workspace, targetPeer)       ↙ 命中后：
TPUT_ASYNC_NOTIFY<RDMA>(                     dcci(0, ENTIRE_DATA_CACHE)
  dst   = peerBase+256+256 处,                逐元素核对 recvBuf == index
  src   = 本地 sendBuf,                            + rootRank*10000
  remoteSignal = peerBase+8,                 任一不符 → deviceStatus 置错
  signalValue = 37, NotifyOp::Set,
  session, targetPeer)
CompleteRdmaEvent：Test → Wait → 再 Test
```

host 侧在每个用例结束后回读 `signal / canaryBefore / canaryAfter` 三项并断言：非 root 的 signal 必须等于 37、root 保持 0、两个 canary 原封不动。

#### 4.5.3 源码精读

**（1）常量与类型约定**。[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:79-95](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L79-L95) 集中定义了本用例的全部刻度：公共事件错误码（`0x30000/0x30001`）、头部偏移 `kRdmaTestDataOffset = 64 × int32`、canary/信号偏移（`sizeof(uint32_t)`、`2 × sizeof(uint32_t)`、`3 × sizeof(uint32_t`））、信号值 37、两个 canary 魔数、轮询上限 `kRdmaNotifyPollLimit`，以及全动态五维 Shape/Stride、256B scratch tile（`kRdmaScratchBytes`，与 4.2 节设备约定对齐）和 `RdmaTestGlobal` 别名。内核入口标注 `[[bisheng::core_ratio(0, 1)]]`（如 [tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:274-277](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L274-L277)）——0 个 Cube 核、1 个 Vector 核，即纯 AIV 内核。

**（2）会话构建与三种完成模式**。`BuildRdmaTestSession`（[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:135-145](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L135-L145)）就是 4.2 节 peer 无关 `BuildAsyncSession<RDMA>` 的薄封装，失败时把 `kRdmaSessionBuildError` 写进 deviceStatus。完成侧定义了三档模式（枚举 `RdmaCompletionMode`）：`STATUS_WAIT_EACH` 每次提交后立即 Wait、`STATUS_WAIT_LAST` 只等最后一个事件、`PUBLIC_EVENT_WAIT_TEST` 走公共 API 的 `Test → Wait → Test` 三连——`CompleteRdmaEvent`（[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:97-111](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L97-L111)）的注释点明其意图：第一次 Test 允许为 false，Wait 必须能完成事件，完成后的 Test 必须观察到「已消费的目标索引」为完成——专门验证 u6-l3 讲的公共事件语义在 RDMA 下同样成立。

**（3）PUT/GET 循环**。`ExecutePutRdma`（[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:192-229](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L192-L229)）是 root 单方执行（与 u6-l1「集合类仅 root 执行」同构）：非 root 直接 `pipe_barrier` 返回；root 先 `TASSIGN(scratchTile, 0x0)` 绑定 scratch（u2-l4 的手工地址规划在通信场景的最小形态），建会话后对每个 `targetPeer != myPeer` 取 `PeerMrBaseAddr(rdmaWorkspace, targetPeer)`（[L219](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L219)），再把远端 recv 区包成 `RdmaTestGlobal`、以 `TPUT_ASYNC<DmaEngine::RDMA>(remoteRecvGlobal, sendGlobal, session, targetPeer)` 发送（`PostPutOperations`，[L147-L167](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L147-L167)）。GET 版 `ExecuteGetRdma`（[L232-L270](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L232-L270)）方向相反：root 从每个源 peer 的 send 区读回本地 recv 区，远端地址同样由 `PeerMrBaseAddr + 偏移` 算出（`PostGetOperations`，[L169-L190](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L169-L190)）。

**（4）notify 内核的收发两端**。[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:315-385](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L315-L385) 是 `TPutAsyncNotifyRdmaKernelImpl`。**接收端**（[L327-L354](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L327-L354)）：把本地 `localBytes+8` 包成 `pto::comm::Signal`，限次循环 `TTEST(signal, 37, WaitCmp::EQ)`；命中后先执行

```cpp
// 示例代码（摘自 tput_async_rdma_kernel.cpp:342-343）
__asm__ __volatile__("");
dcci(static_cast<__gm__ void*>(0), cache_line_t::ENTIRE_DATA_CACHE);
```

再逐元素核对 `recvBuf[index] == index + rootRank * 10000`。内联汇编空语句是屏障注记，防止编译器把 dcci 与前后读重排；`ENTIRE_DATA_CACHE` 级别的失效对应「网卡写的载荷完全绕过了本地缓存」。**发送端**（[L356-L376](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L356-L376)）：`targetPeer = myPeer + 1`（双 rank 下即对方），取 `peerBase`，把远端 recv 区、本地 send 区、远端信号槽（`peerBase + kRdmaNotifySignalOffset`）分别包好，一次 `TPUT_ASYNC_NOTIFY<DmaEngine::RDMA>(destination, source, remoteSignal, 37, NotifyOp::Set, session, targetPeer)`，最后以 `PUBLIC_EVENT_WAIT_TEST` 模式收尾。

**（5）host 侧上下文与黄金校验**。`RdmaTestContext`（[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:445-594](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L445-L594)）管理一个用例的完整生命周期：`SetupLocalResources`（setDevice、建流、`aclrtMalloc` 对称缓冲并清零）、`SetupBootstrap`（调 4.4 节的 `BackendBootstrap::Init`）、`ConnectRdma`（把 bootstrap 产出填进 `WorkspaceConfig` 并 `rdmaMgr.Init(config)`，[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:513-531](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L513-L531)）、`Cleanup`（逆序释放，每个用例独享一次「建 channel—拆 channel」周期，注释说这顺带验证了端口复用）。每个阶段之间用 `AllRanksReady`（MPI_Allgather）做集体确认，任何一 rank 失败全体失败——多 rank 测试不允许多数通过的假象。

`RunPutAsyncNotifyRdmaSetKernel`（[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:979-1065](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L979-L1065)）是 notify 用例的 host 主流程：先把 `signal=0` 与两个 canary 拷进各 rank 头部（[L1014-L1028](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L1014-L1028)），launch 内核（workspace 实参就是 `rdmaMgr.GetWorkspaceAddr()`，[L1033-L1036](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L1033-L1036)），同步后先按 PUT 规则核对载荷（`VerifyPutResult`：接收区等于发送方模式值、未写区保持哨兵值），再回读三项做 canary 断言（[L1042-L1062](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L1042-L1062)）：非 root 的 signal 必须是 37、root 保持 0、两 canary 不变，任一不符打印实际值并判负。输入/哨兵模式（`RdmaInputValue/RdmaSentinelValue`，[L597-L613](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L597-L613)）用确定性公式而非随机数——便于失败时定位是哪一位先错。

**（6）gtest 入口**。[tests/npu/a5/comm/st/testcase/tput_async_notify_rdma/main.cpp:17-27](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_notify_rdma/main.cpp#L17-L27) 的 `TEST(TPutAsyncNotifyRdma, Int32SetAndCanaries)` 只做三件事：非双 rank 则 SKIP、跑 `RunPutAsyncNotifyRdmaSet(2, 2, 0, 0)`、结果为 SKIPPED 时 SKIP（运行前提缺失不算失败）、否则断言 PASSED。`main` 先 `CommMpiInit` 再跑 gtest（[L29-L38](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_notify_rdma/main.cpp#L29-L38)）——MPI rank 由 mpirun 拉起，一个 rank 一个进程。README 明确当前 RDMA 后端不支持 AtomicAdd，因此**没有对应的 AtomicAdd 用例**（[tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md:32-34](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md#L32-L34)）。

**（7）为什么 RDMA 不支持 AtomicAdd——三处源码互证**。① backend 文件头直言「atomics are not supported」（[pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp:11-14](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/async/rdma/backends/hns_1825/hns_1825_backend.hpp#L11-L14)）；② 信号在设备上是「fence 后的 4 字节 inline RDMA WRITE」（4.3 节 `FillWqeInlineSet`），本质是普通写，网卡侧没有读改写环节——RoCE 的原子操作需要专门的 opcode 与扩展头，本实现没有走那条路；③ A5 分发层把它变成显式拒绝：`TPUT_ASYNC_NOTIFY_RDMA` 在 `notifyOp != NotifyOp::Set` 时先 `PTO_ASSERT(false, "...RDMA currently supports NotifyOp::Set only.")`，再返回编码了 `kRdmaUnsupportedOperationError` 的错误事件（[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:69-91](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L69-L91)，拒绝点在 [L79-L82](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L79-L82)）。作为对照，同一文件里 SDMA 回退路径用 `TNOTIFY_IMPL` 原生支持两种 NotifyOp（[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:33-50](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L33-L50)），URMA 路径把 `notifyOp` 原样传给 `__urma_put_async_notify`（[pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:52-67](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L52-L67)）——「一份签名、多套实现、能力刻度不同」再次应验。另注意 `TPUT_ASYNC_NOTIFY` 的整体三路分流骨架在 [pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp:95-124](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp#L95-L124)，`if constexpr` 按 engine 选中一条路径，宏未开时用 static_assert 给出可读的编译错误——这与 u4-l1 的指令分层手法一致。

#### 4.5.4 代码实践

**实践目标**：在有 HNS1825 网卡的 A5 双卡环境上跑通 notify 用例并读懂输出（无硬件则完成源码走读版）。

**操作步骤（有环境）**：

1. 按前置条件准备：A5 环境 + HNS1825 网卡 + 对应驱动/HCOMM、MPI 与 HCCL、各 rank 的 RoCE IPv4 可达（[tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md:7-11](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md#L7-L11)）。
2. 依次执行：

   ```bash
   export PTO_RDMA_BACKEND=HNS_1825
   export PTO_ROCE_VERBOSE=1
   python3 tests/script/run_st.py -r npu -v a5 -t comm/tput_async_notify_rdma \
       -g TPutAsyncNotifyRdma.Int32SetAndCanaries -d -n 2
   ```

   （完整三条目标见 [tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md:15-30](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/README.md#L15-L30)。）

3. 阅读输出：每 rank 的 `[RDMA][HNS_1825][case N][rank R] SETUP/CASE/CLEANUP` 追踪行、`PUT_NOTIFY Rank R Device D SyncRet 0 DevStatus 0x0` 状态行、以及 gtest 的 PASSED。

**需要观察的现象**：`SETUP bootstrap ready` 行里的 peers 列表（`peer:IP/phyN/sym0x...`）应与实际机组一致；`DevStatus 0x0` 表示设备侧无错误；若端点发现失败会看到 `[SKIP] ... could not resolve local IP`。

**预期结果**：`[  PASSED  ]` 且 signal/canary 断言无输出。本机无对应硬件，以上运行结果**待本地验证**。

**操作步骤（无环境，源码走读版）**：

1. 在 [tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:1042-1062](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L1042-L1062) 中找出三处 aclrtMemcpy 分别回读什么。
2. 回答：如果把 `kRdmaNotifySignalOffset` 从 `2*sizeof(uint32_t)` 改成 `1*sizeof(uint32_t)`，哪个断言会最先失败、为什么？

**预期答案**（走读版第 2 问）：canary 断言——信号槽会与 canaryBefore 重叠，4 字节 inline 写会覆盖 canaryBefore，`canaryBefore == kRdmaNotifyCanaryBefore` 不成立，同时 signal 落在错误偏移使接收端 TTEST 轮询超时。这正是 canary 设计要抓的错误类别。

#### 4.5.5 小练习与答案

**练习 1**：接收端在 `TTEST` 命中后、读载荷前为什么必须 `dcci(ENTIRE_DATA_CACHE)`？发送端 root 在 `CompleteRdmaEvent` 前为什么不需要？

**参考答案**：载荷与信号都是网卡直接写 GM 的，绕过了接收端 AI Core 的数据缓存；信号命中只代表网卡已完成写入，不代表接收端缓存已同步，必须整体失效后才能读到新载荷（[tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp:342-344](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_rdma/tput_async_rdma_kernel.cpp#L342-L344)）。root 侧读的是 CQE（网卡写的完成凭证），而 `PollCq/TestEvent` 内部在每次读 CQE 前已经自带 `dcci` + GM→UB 搬运（4.3 节第 (5) 点），所以公共事件路径无需用户再做。

**练习 2**：`STATUS_WAIT_EACH` 与 `STATUS_WAIT_LAST` 两种完成模式分别适合什么场景？

**参考答案**：`WAIT_EACH` 每次提交后立刻等待，正确性最直观，适合小规模正确性验证（也用于 SQ 水位敏感的场景）；`WAIT_LAST` 连续提交多次、只等最后一个句柄，利用「SQ 按序完成」摊薄等待开销，适合批量流水的吞吐场景。测试同时覆盖两种模式，验证 4.3 节 head/tail 单调推进假设在两种用法下都成立。

**练习 3**：为什么 PUT 用例的校验要区分「接收区等于发送方模式值」与「未写区保持哨兵值」两部分？

**参考答案**：前者验证「该到的数据到了」，后者验证「不该动的地方没动」——RDMA 写是按 MR 区间精确搬运的，越界或长度错误只会体现在哨兵区被破坏上。这与 canary 校验是同一思想的两种落点：canary 盯信号写的边界，哨兵盯载荷写的边界。

## 5. 综合实践

把本讲五条线串成一个任务：**为 `TPUT_ASYNC_NOTIFY` 的 RDMA 路径写一份「全链路审计笔记」**。

1. **调用链整理**：从 [tests/npu/a5/comm/st/testcase/tput_async_notify_rdma/main.cpp:22](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/comm/st/testcase/tput_async_notify_rdma/main.cpp#L22) 的 `RunPutAsyncNotifyRdmaSet(2, 2, 0, 0)` 出发，沿 `RdmaTestContext::Setup → BackendBootstrap::Init → RdmaWorkspaceManager::Init → hns_1825::WorkspaceManager::Init`（端点发现与 MR 注册）→ 内核内 `BuildAsyncSession<RDMA> → TPUT_ASYNC_NOTIFY<RDMA> → rdma::WriteNotify → hns_1825::WriteNotify → PostSendWriteNotify`（载荷 WQE + fence inline 信号 WQE）→ `CompleteRdmaEvent → PollCq`（canary 校验在 host 回读）画一张全链路时序图，每个节点标注文件与行号。
2. **环境变量清单**：在图上标出每个环节依赖的环境变量（`PTO_RDMA_BACKEND`、`PTO_ROCE_PHYIDS/LOCAL_IP/IPS/BASE_PORT/VERBOSE`、`HCCL_RDMA_TC/SL`、排障用 `IBV_EXTEND_DRIVERS`）与两份系统文件（`hccl_rootinfo.json`、`virtualTopology.xml`）。
3. **三问作答**（本讲规格中的三问）：
   - 为什么当前 RDMA 后端不支持 AtomicAdd 通知？（提示：4.5.3 第 (7) 点的三处互证）
   - `TPUT_ASYNC_NOTIFY` 的 `peer` 参数在 SDMA/URMA/RDMA 下分别如何被使用？（提示：4.1.3 的对照表 + `MakeExecContext` 的落地点）
   - 接收端「信号可见」到「数据可见」之间差一步什么操作？为什么公共事件路径不需要用户做？（提示：4.5.5 练习 1）
4. **延伸改动（选做）**：把 notify 用例的 `kRdmaNotifyCanaryBefore/After` 值改成两个新魔数，重新推演（不必运行）哪个断言在哪一行拦截「信号写越界」与「写错偏移」两类错误。

预期产出：一页时序图 + 一张环境变量表 + 三问文字答案。全部内容都能仅凭源码完成，不依赖硬件。

## 6. 本讲小结

- **两级后端选择**：`DmaEngine` 是指令级引擎选择（SDMA/URMA/RDMA 模板参数），`RdmaBackend` 是 RDMA 之下网卡级选择（`PTO_RDMA_BACKEND=HNS_1825` 在 CMake configure 期翻译成编译宏，改值必须重编）。
- **peer 语义三态**：SDMA 忽略 peer（地址来自 GlobalTensor VA）；URMA 用 peer 选 per-peer 队列与内存元数据并另选 notify 资源区；RDMA 把 peer 写进 `ctx.destRankId`，用于索引 per-peer SQ/CQ 与远端 MR 的 rkey，并编码进事件句柄高 32 位。
- **设备侧形态**：HNS1825 后端在 AIV 标量代码里手写 RoCE 队列——UB 组装 64B WQE、`WriteUbToGmWithSync` 以 S↔MTE3 事件发布、一次 `st_dev` 敲硬件 doorbell、`PollCq` 以 owner 位 + dcci 轮询完成；NIC 可见字段转大端、host 消费的镜像保持主机序。
- **WriteNotify = 载荷 WQE + fence 的 4 字节 inline WRITE**：fence 保证载荷先于信号；这也决定了 RDMA notify 仅支持 Set——inline 普通写没有读改写语义，AtomicAdd 被设备层显式拒绝。
- **host 控制面**：`RdmaWorkspaceManager` 门面 + HNS1825 实现按「建端点 → 注册对称缓冲 MR → 每 peer 一 channel → 拷 RdmaInfo 入设备内存」推进；测试侧 bootstrap 以 phyId 解析 + IP 三级回退（rootinfo → topology → 环境变量）+ MPI 交换凑齐配置，任何一 rank 缺失即集体 SKIP。
- **ST 的两类边界校验**：载荷边界靠「未写区保持哨兵值」，信号边界靠前后 canary 魔数；接收端信号命中后必须 `dcci(ENTIRE_DATA_CACHE)` 才能读载荷——信号可见 ≠ 数据可见。

## 7. 下一步学习建议

- **下一讲 u6-l6（A5 混合核 MegaMoE 融合算子）**：把本讲的 TPUSH/TPOP、u6-l3/u6-l4 的通信编排与 HCCL window/MPI 协作放进一个真实大算子（dispatch_mega_combine 的七段流水）里综合运用，是单元六的收官。
- 若想继续深挖传输后端本身，建议对照阅读 [include/pto/comm/async/urma/urma_async_intrin.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/urma/urma_async_intrin.hpp) 与 [include/pto/comm/async/sdma/sdma_async_intrin.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/async/sdma/sdma_async_intrin.hpp)，比较三套实现里「会话、句柄、水位、完成」四个概念的对应物，体会同一抽象在三类硬件上的不同投影。
- 结合 u7-l2（内存一致性与生产者-消费者顺序）回看本讲的两处顺序设计：`WriteUbToGmWithSync` 的 MTE3→S 事件方向、接收端信号命中后的 dcci——它们都是「事件先于数据可见」原则在网卡场景的具体化。
- 若你维护多机训练/推理集群，可把 bootstrap 的三级 IP 回退与 `AllRanksReady` 集体确认模式当作编写跨机 ST 的模板参考。




