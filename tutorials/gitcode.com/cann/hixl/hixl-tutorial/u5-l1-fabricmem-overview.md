# FabricMem 模式：概念与设计

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 FabricMem 模式解决什么问题：为什么在超节点内做 D2RH（设备到远端主机 DRAM）传输时，RoCE 中转或 HCCS 直连都不够用。
2. 理解 FabricMem 模式的整体方案：基于 CANN VMM（Virtual Memory Manager）的物理内存导出/导入与统一编址，让 NPU 借助 HCCS 链路直接读写远端 DRAM。
3. 掌握 `FabricMemEngine` 在 Engine 抽象体系（u3-l1）中的位置：它是与 `HixlEngine`、`CommEngine` 并列的第三个引擎实现，是一个不侵入旧路径的「旁路引擎」。
4. 建立 `src/hixl/fabric_mem/` 模块的文件地图，能按「控制面 / 内存 / 传输 / 统计」四类快速定位文件，为 u5-l2（内存体系）与 u5-l3（传输服务）导航。

本讲是单元五的「概念课 + 地图课」，只做总体设计，不深入分配器与传输服务的实现细节。

## 2. 前置知识

阅读本讲前，建议你已理解以下概念（均在前置讲义中建立）：

- **Engine 抽象体系**（u3-l1）：`Engine` 是 src 内部抽象基类，`EngineFactory::CreateEngine` 是引擎选择的唯一决策点；`HixlImpl` 对引擎种类零感知。本讲要讲的 `FabricMemEngine` 正是工厂分支中的第一个。
- **注册内存与零拷贝**（u2-l3）：注册是把「地址 + 长度」登记为对端可直接访问的授权。FabricMem 模式下「注册」的底层含义变成了「把物理内存导出为可跨进程共享的句柄」。
- **HCCS 与 RDMA 两条链路**（u1-l1）：HCCS 是同超节点内的高带宽片间链路，RDMA/RoCE 是跨主机网络链路。FabricMem 模式本质上「借用 HCCS 链路访问远端主机的 DRAM」。
- **控制面与数据面分离**（u1-l3）：控制面用 TCP socket 交换元信息（这里是「共享内存句柄表」），数据面走真正的传输介质（这里是 SDMA/HCCS）。FabricMem 模式延续了这一分工。

补充两个本讲新术语：

- **超节点（Supernode）**：多台主机通过交换平面（L1/L2 交换平面）把各自的 NPU 与 DRAM 连成一个统一通信域的硬件形态，如 Atlas 800T A3。
- **VMM（Virtual Memory Manager）**：CANN 提供的虚拟内存管理机制，把「申请物理内存 → 预留虚拟地址 → 建立映射」三步解耦，从而允许把同一个物理内存映射进多个进程的地址空间。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/FabricMem.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md) | 用户视角的 FabricMem 模式说明：背景、VMM 方案、运行依赖与硬件范围 |
| [docs/zh/design/FabricMem模式设计.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/FabricMem模式设计.md) | 内部设计文档：类图、时序图、处理流程与并发锁设计，是本讲最重要的文档 |
| [src/hixl/engine/fabric_mem_engine.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc) / [.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.h) | `FabricMemEngine` 实现：薄门面，编排 TransferService / LocalMemory / ControlServer |
| [src/hixl/fabric_mem/fabric_mem_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_types.h) | fabric_mem 模块内部共享数据结构：`ShareHandleInfo`、`AsyncSlot`、`AsyncRecord` 等 |
| [src/hixl/fabric_mem/fabric_mem_config.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_config.h) | `FabricMemConfig` 配置结构体（容量、起始地址、流数量、AICPU unfold 开关） |
| [src/hixl/fabric_mem/fabric_mem_transfer_service.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.h) | 传输服务抽象基类，持有 ChannelManager 与 SlotPool，是 u5-l3 的主角 |
| [src/hixl/fabric_mem/virtual_memory_manager.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.h) | 虚拟内存管理器单例：预留/释放全局虚拟地址区间 |
| [src/hixl/engine/engine_factory.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc) | 引擎工厂——FabricMem 分支的入口（u3-l1 已精读，本讲只回顾该分支） |

`src/hixl/fabric_mem/` 目录共 27 个文件，本讲只建立地图，逐文件精读留给 u5-l2、u5-l3。

## 4. 核心概念与源码讲解

### 4.1 FabricMem 模式的设计动机

#### 4.1.1 概念说明

大模型推理的内存压力催生了**多级缓存架构**：显存（HBM）作一级缓存，分布式 DRAM 池（如 Mooncake store）作二级缓存。这意味着 KV Cache 需要频繁地在 NPU 与远端主机 DRAM 之间搬运（D2RH / RH2D）。但在 A3 超节点场景下，原有两条路都不理想：

- **RoCE（RDMA）路径**：满载带宽约 20GB/s，在超节点内部署时成为明显瓶颈（跨主机网络是它唯一的存在理由）。
- **传统 HCCS 路径**（HCCL 接口）：不支持 D2RH 传输；若用「NPU→本机 HBM→CPU 拷贝→网络」的中转模式，则会额外占用宝贵的 HBM 带宽，影响模型推理。

A3 服务器提供的 **FabricMemory 技术**是破局点：超节点内所有节点的 DRAM 统一编址，NPU 可以通过 HCCS 高速链路直接访问远端节点内存。FabricMem 模式就是把这一硬件能力包装成 HIXL 的一个引擎，把超节点内传输带宽提升至百 GB/s 级别，同时保持「无需对端 CPU 介入的单边通信」语义。

一句话对比：

| 路径 | D2RH 支持 | 超节点内带宽 | 对 HBM 的占用 |
| --- | --- | --- | --- |
| RoCE/RDMA | 支持 | 约 20GB/s（瓶颈） | 不占 |
| HCCS（HCCL） | 不支持 | 高 | — |
| 中转模式 | 支持 | 受限 | 占用大，影响推理 |
| **FabricMem** | **支持** | **百 GB/s 级** | **不占（直达 DRAM）** |

#### 4.1.2 核心流程

FabricMem 的底层依托 CANN VMM 机制，分四步实现「任意进程访问任意进程的内存」：

1. 每个进程通过 `aclrtMallocPhysical` 申请物理内存，`aclrtReserveMemAddress` 预留虚拟地址，`aclrtMapMem` 建立映射；
2. 进程间交换物理内存的共享句柄（share handle）；
3. 访问方把远端物理内存映射进自己的页表；
4. 发起 SDMA 访问，即可读写任何进程的片上内存和 DRAM 内存。

从本机 NPU 写远端主机 DRAM 的物理路径是：

```
NPU1 → L1 交换平面 → L2 交换平面 → 对端 L1 交换平面 → CPU → DDR
```

注意这条路径**不经过任何一端的 HBM**——这正是它优于中转模式的根本原因。

#### 4.1.3 源码精读

设计动机的权威表述在两份文档中：

[FabricMem模式设计.md:L5-L11](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/FabricMem模式设计.md#L5-L11) 说明需求背景：Mooncake 等分布式 DRAM 缓存池对 D2RH 传输性能的要求，以及现有 HCCS 模式不支持 D2RH、中转模式占用 HBM 带宽两条劣势。

[FabricMem.md:L12-L18](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md#L12-L18) 给出整体方案的三个核心价值：超节点内 DRAM 统一编址、D2RH/RH2D 高带宽双向通道、无需 CPU 介入的单边通信。

[FabricMem.md:L22-L27](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md#L22-L27) 描述 VMM 四步机制（申请物理内存→预留虚拟地址→映射→SDMA 访问），即 4.1.2 节流程的原文。

运行依赖与硬件范围在 [FabricMem.md:L61-L73](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md#L61-L73)：需要 HDK 25.5 以上、灵衢计算网络 1.5.0 以上、CANN 9.0 以上；**仅支持 Atlas A3 训练/推理系列**。特别注意 L69 的说明：25.5 HDK 不支持 `aclrtMemRetainAllocationHandle` 接口，必须用 adxl 提供的 `MallocMem`/`FreeMem` 管理 HOST 内存；26.0 以上才能直接用 acl 接口。启用方式是在 options 中配置 `OPTION_ENABLE_USE_FABRIC_MEM = "1"`。

#### 4.1.4 代码实践

**实践目标**：把「为什么需要 FabricMem」从文档结论变成自己可复述的推理。

**操作步骤**：

1. 通读 [docs/zh/FabricMem.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md) 全文（约 80 行）。
2. 通读 [docs/zh/design/FabricMem模式设计.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/FabricMem模式设计.md) 的「介绍」（L3-L11）与「处理」（L205-L241）两节。
3. 用 Grep 在仓库内搜索 `OPTION_ENABLE_USE_FABRIC_MEM`，确认它的定义位置与所有消费点（提示：定义在 `include/hixl/hixl_types.h`，消费点包括 options 解析、EngineFactory 与本文各处）。

**需要观察的现象**：文档中「中转模式占用 HBM 带宽」这一劣势描述，与 4.1.1 节表格的对应关系；`OPTION_ENABLE_USE_FABRIC_MEM` 的消费点数量远少于 `OPTION_GLOBAL_RESOURCE_CONFIG` 等通用选项。

**预期结果**：能画出「NPU→L1→L2→对端 L1→CPU→DDR」数据流向图，并能回答「为什么这条路径不占 HBM 带宽」。运行样例部分待本地验证（需要 A3 硬件环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FabricMem 模式不复用 `HixlEngine` 的 HCCS 通道，而是新建一个引擎？

**答案**：底层 HCCL 接口的 HCCS 传输模式不支持 D2RH；FabricMem 的核心是「物理内存导出为共享句柄 + 跨进程 VMM 映射」，与 `HixlEngine` 基于 endpoint/channel/TransferPool 的机制完全不同。设计文档 L50 明确说明 FabricMem 由独立的 `FabricMemEngine` 承载，「不再侵入 HixlEngine、AdxlInnerEngine、ChannelMsgHandler 或 Channel」，是刻意的旁路设计。

**练习 2**：FabricMem 模式与 RoCE 路径各自的适用场景是什么？

**答案**：FabricMem 只适用于**超节点内部**（A3 系列，DRAM 统一编址、HCCS 链路可达），带宽百 GB/s 级；RoCE 适用于跨主机、跨超节点的网络传输，带宽约 20GB/s。二者是范围互补关系，不是替代关系。

---

### 4.2 FabricMemEngine：Engine 抽象的第三个实现

#### 4.2.1 概念说明

回顾 u3-l1：`EngineFactory::CreateEngine` 按固定分支顺序选择引擎，其中 **FabricMem 开关是第一优先级**。`FabricMemEngine` 与 `HixlEngine`（主力双角色引擎）、`CommEngine`（ADXL 兼容壳）并列，是 `Engine` 抽象基类的第三个实现。

它的角色定位是**薄门面（thin facade）**：

- 对上：完整实现 `Engine` 的全部纯虚接口（Initialize/Finalize、RegisterMem、Connect/Disconnect、TransferSync/Async/GetTransferStatus、SendNotify/GetNotifies），因此 `HixlImpl` 完全无感地复用了既有外壳。
- 对下：只做「门卫检查 + 恢复 ACL context + 转发」，真正的状态分散在四个成员中——`FabricMemTransferService`（传输编排）、`FabricMemLocalMemory`（本地内存注册与句柄导出）、`FabricMemControlServer`（控制面服务）、`FabricMemStatistic`（统计）。

设计文档 L247 对此有一句精准概括：「FabricMemEngine：薄门面，编排 TransferService / LocalMemory / ControlServer；持有 FabricMemLocalMemory，不持有连接表或 async record」。

#### 4.2.2 核心流程

**初始化流程**（伪代码）：

```
Initialize(options):
  持 mutex_
  ├─ 过滤不支持的选项（仅打警告，不报错）
  ├─ 强校验 EnableUseFabricMem 必须为 1（否则 PARAM_INVALID）
  ├─ aclrtGetDevice / aclrtCreateContext 创建专属 context（RAII 持有）
  ├─ ApplyFabricMemoryOptions: 解析容量/起始地址/流数/AICPU unfold
  ├─ InitFabricMem（带失败回滚 guard）:
  │   ├─ VirtualMemoryManager 单例: 设置容量与起始地址并 Initialize
  │   ├─ FabricMemStatistic: StartPeriodicDump
  │   ├─ StartControlServer: 启动控制面，注册共享句柄提供者
  │   └─ InitTransferService: 按 enable_aicpu_unfold
  │       选 Aicpu 或 Host 传输服务并 Initialize
  ├─ is_initialized_ = true
  └─ 若 auto_connect 或本端是 server（带端口）→ 启动 keepalive 监控线程
```

**传输流程**（以 TransferSync 为例）：

```
TransferSync(remote, op, descs, timeout):
  门卫检查（已初始化、descs 非空）
  ├─ EnsureAutoConnected: auto_connect 开启时隐式建链（3 秒超时）
  ├─ fabric_mem_transfer_service_->TransferSync(...)   # 全部实际工作在 service
  └─ 失败且 auto_connect 时 → DisconnectOnTransferError 自动断链
```

这与 u2-l5 讲过的 `HixlEngine` 传输路径（经 ClientManager→HixlClient→ClientHandler→CS 层）形成对照：FabricMem 模式下**没有 ClientHandler、没有 endpoint 匹配、没有 Hcomm 通道**，控制面只交换「共享内存句柄表」，数据面是 `aclrtMemcpyAsync`（Host 服务）或 AICPU 下发 SDMA（AICPU 服务）。

#### 4.2.3 源码精读

**工厂选择**——FabricMem 分支位于所有分支之首：

[engine_factory.cc:L47-L50](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L47-L50)：解析选项后，只要 `EnableFabricMem()` 为真，立即创建 `FabricMemEngine` 并打 `selected engine` 事件日志——不会落入 LocalCommRes、protocol_desc 等后续分支。

**类声明与成员**——四个被编排的组件一目了然：

[fabric_mem_engine.h:L81-L90](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.h#L81-L90)：`fabric_mem_config_`（配置）、`fabric_mem_statistic_`（统计）、`local_memory_`（本地内存）、`fabric_mem_transfer_service_`（传输服务，shared_ptr）与 `fabric_mem_control_server_`（控制面，unique_ptr）；另持有专属 `aclrt_context_`，用 `shared_ptr<void>` 自定义删除器做 RAII。

**初始化装配线**：

- [fabric_mem_engine.cc:L106-L143](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L106-L143)（`InitializeLocked`）：先校验 `EnableUseFabricMem` 必须为 1，再创建 ACL context（失败有 scope guard 回滚），随后依次应用选项、初始化 FabricMem 各组件，最后根据 `auto_connect_ || listen_port > 0` 决定是否启动 keepalive 监控——server 角色（local_engine 带端口）即使不配 auto_connect 也要监控对端存活。
- [fabric_mem_engine.cc:L93-L104](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L93-L104)（`InitFabricMem`）：用 `HIXL_DISMISSABLE_GUARD` 包住全部初始化步骤，任一步失败即回调 `CleanupFabricMemLocked()` 回滚，不留半初始化状态——与 u2-l1 讲过的 `HixlImpl` 回滚策略一致。
- [fabric_mem_engine.cc:L82-L87](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L82-L87)：按 `enable_aicpu_unfold` 在 `FabricMemAicpuTransferService` 与 `FabricMemHostTransferService` 之间二选一——这是 u5-l3 两条传输路径的源头。

**配置解析**：

[fabric_mem_engine.cc:L145-L175](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L145-L175)（`ApplyFabricMemoryOptions`）：从 `OPTION_GLOBAL_RESOURCE_CONFIG` 读取 `fabric_memory.max_capacity`（虚拟内存池容量）、`start_address`（起始地址）、`task_stream_num` 与 `enable_aicpu_unfold`；关键约束在 L164-L170——**AICPU unfold 只支持 `task_stream_num=1`**，配置了其他值直接 `PARAM_INVALID`。默认值见 [fabric_mem_config.h:L22-L32](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_config.h#L22-L32)：`max_stream_num` 默认 512、`task_stream_num` 默认 1、`enable_aicpu_unfold` 默认 true。

**门面转发模式**：

- [fabric_mem_engine.cc:L193-L213](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L193-L213)：`RegisterMem` 门卫检查后直接转 `local_memory_.RegisterMem`；`DeregisterMem` 额外要求先断开全部链路（`HasChannels()` 为真则 FAILED）——与 u2-l3 讲过的 HIXL「解注册前必须断链」合同一致。
- [fabric_mem_engine.cc:L259-L296](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L259-L296)：`TransferSync/TransferAsync` 都是「检查 → `TemporaryRtContext` 恢复 context → `EnsureAutoConnected` → 转发 service → 失败时 `DisconnectOnTransferError`」的固定套路。`TemporaryRtContext` 是关键细节：worker 线程可能没有 ACL context，必须先切到引擎持有的 context 才能调用 aclrt 接口。
- [fabric_mem_engine.cc:L323-L327](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L323-L327) 与 [L390-L394](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L390-L394)：批量 `GetTransferStatus`（`GetTransferStatusArgs` 版本）与 `RegisterCallbackProcessor` 均直接返回 `UNSUPPORTED`——这两个接口是 `HixlEngine` 路径特有的，FabricMem 明确声明不支持，而不是静默失败。

**收尾清理**：

[fabric_mem_engine.cc:L329-L350](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L329-L350)（`CleanupFabricMemLocked`）：清理顺序是 **transfer_service → control_server → local_memory → statistic → context**，注释明确说明 transfer service 先 Finalize 的原因——它要停 keepalive 监控并逐个断开远端（abort 在途传输、解除远端内存映射、归还槽位）。

#### 4.2.4 代码实践

**实践目标**：验证 FabricMemEngine「薄门面」的定位，量化「转发」与「做事」的比例。

**操作步骤**：

1. 打开 [src/hixl/engine/fabric_mem_engine.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc)（全文约 397 行），逐个函数阅读公开接口（Initialize、RegisterMem、Connect、TransferSync、SendNotify 等）。
2. 统计每个公开接口中「实际逻辑」的行数（门卫检查、日志、context 恢复、一行转发之外的内容）。你会发现几乎所有接口都在 20 行以内。
3. 对比阅读 `src/hixl/engine/hixl_engine.cc` 中同名接口，感受门面与实体引擎的厚度差异。

**需要观察的现象**：`TransferSync` 与 `TransferAsync` 的函数体几乎逐行同构（检查 → context → auto connect → 转发 → 错误断链）；`GetTransferStatus`（单查版，L298-L321）是少数有真实逻辑的接口——它在查到 COMPLETED 时上报 prof 数据、查到 FAILED 时触发自动断链。

**预期结果**：得出结论「FabricMemEngine 的全部复杂度都委托给了 fabric_mem 模块」，并能在源码中指出 `enable_aicpu_unfold` 在哪两行决定服务实现类（L83-L87）。

#### 4.2.5 小练习与答案

**练习 1**：`FabricMemEngine::InitializeLocked` 中 `start_keepalive_monitor` 的取值条件是什么？为什么 server 端即使不配置 auto_connect 也要启动？

**答案**：条件是 `auto_connect_ || listen_port > 0`（[fabric_mem_engine.cc:L137-L140](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L137-L140)），即 local_engine 带端口（server 角色）时必然启动。因为 server 被动持有已导出的共享内存句柄，若对端异常下线而不被感知，本地资源与导入映射会泄漏；keepalive 监控让 server 也能主动发现失活连接并清理（设计文档检查点 9「对端异常下线」）。

**练习 2**：如果用户在 options 里同时配置了 `OPTION_ENABLE_USE_FABRIC_MEM=1` 和 `local_comm_res`，会走哪个引擎？如果配置了 FabricMem 模式不认识的选项呢？

**答案**：走 `FabricMemEngine`——工厂的 FabricMem 分支在最前（engine_factory.cc L47-L50），后续分支不再评估。不认识的选项只会打 `Unsupported option ... will be ignored` 警告并被忽略（[fabric_mem_engine.cc:L107-L111](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L107-L111)），不会失败；但 `EnableUseFabricMem` 不为 1 时会以 `PARAM_INVALID` 失败（L112-L113）——这是「经工厂创建 FabricMemEngine 却没开开关」的防御性校验。

**练习 3**：为什么 `DeregisterMem` 前要求先断开所有链路？

**答案**：[fabric_mem_engine.cc:L209-L210](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L209-L210) 检查 `HasChannels()`。已连接的对端导入并映射了本端内存的共享句柄，若先解除注册、释放物理内存句柄，对端的映射会指向已释放的物理内存，造成悬空访问。先断链（解除对端映射）再解注册，才能安全释放。

---

### 4.3 fabric_mem 模块的文件组织与关键数据结构

#### 4.3.1 概念说明

`src/hixl/fabric_mem/` 是 FabricMem 的实体实现目录，共 27 个文件，可按职责分为五组：

| 分组 | 文件 | 职责 |
| --- | --- | --- |
| 内存 | `fabric_mem_memory.{h,cc}`（`FabricMemLocalMemory`/`FabricMemRemoteMemory`）、`fabric_mem_allocator.{h,cc}`、`virtual_memory_manager.{h,cc}` | 本地内存注册与共享句柄导出、远端句柄导入与映射、物理内存分配、全局虚拟地址预留 |
| 控制 | `fabric_mem_control.{h,cc}`（`FabricMemControlServer`/`FabricMemControlClient`） | 控制面 TCP 服务：拉取对端共享句柄表、keepalive 心跳、Notify 转发 |
| 传输 | `fabric_mem_transfer_service.{h,cc}`（基类）、`fabric_mem_host_transfer_service.{h,cc}`、`fabric_mem_aicpu_transfer_service.{h,cc}`、`fabric_mem_aicpu_dispatcher.{h,cc}`、`fabric_mem_aicpu_types.h` | 传输编排门面与 Host/AICPU 两条拷贝下发路径 |
| 通道与槽位 | `fabric_mem_channel_manager.{h,cc}`、`fabric_mem_slot_pool.{h,cc}` | 每远端一个 channel 的连接表与请求路由；异步传输槽位（stream + host flag）池 |
| 配置/统计/类型 | `fabric_mem_config.h`、`fabric_mem_statistic.{h,cc}`、`fabric_mem_types.h`、`acl_compat.h` | 配置结构、周期统计 dump、模块内共享数据结构、ACL 版本兼容层 |

这个分组对应设计文档 L246-L252 的组件职责表，也预告了后续两讲的分工：**u5-l2 讲内存组，u5-l3 讲传输组**。

#### 4.3.2 核心流程

FabricMem 模式下一次完整使用的组件协作（依据设计文档时序图 L161-L203 归纳）：

```
Initialize:  Engine → VMM 单例初始化 + ControlServer.Start(注册句柄提供者)
                      + TransferService.Initialize(SlotPool + ChannelManager)

RegisterMem: Engine → LocalMemory.RegisterMem
                      → aclrtMemRetainAllocationHandle（取物理内存句柄）
                      → aclrtMemExportToShareableHandleV2（导出共享句柄）
                      → 记入 share_handles_ 表

Connect:     Engine → Service.Connect → ChannelManager.Connect
                      → ControlClient 向对端 ControlServer Fetch 共享句柄表
                      → RemoteMemory.Import（aclrtMemImportFromShareableHandleV2
                         + aclrtMapMem 映射进本进程页表）
                      → 登记为 channel（含 keepalive_fd）

TransferSync: Engine → Service：取 channel + 地址转换（用户 VA → 映射 VA）
                       → SlotPool 取槽位 → 下发拷贝 → 同步等待 → 归还槽位
                       → Statistic 记录耗时/字节/op 数
```

并发设计的一句话概括（详细锁表见设计文档 L254-L288，本讲不展开）：传输提交与断链用 `submit_gate`（`shared_mutex`）互斥——**提交持共享锁可并行，断链持独占锁先 drain 在途提交再 abort/unmap**，这保证了「断链立即 abort 不等传输完成」与「不出现悬空映射」两个性质同时成立。

#### 4.3.3 源码精读

**模块共享数据结构**（`fabric_mem_types.h`）：

[fabric_mem_types.h:L31-L44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_types.h#L31-L44)：`VaInfo`（虚拟地址 + 长度）与 `ShareHandleInfo`。后者是 FabricMem 模式最核心的结构：一条已注册内存除了 `va_addr/len/mem_type`，还携带 `share_handle`（`aclrtMemFabricHandle`，导出给对端的凭证）、`imported_handle/imported_va`（导入远端内存后的本地句柄与映射地址）与 `is_retained` 标志——「注册」在这个模式下的本质就是把这张表填好。

[fabric_mem_types.h:L46-L65](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_types.h#L46-L65)：`AsyncSlot`，异步传输槽位。注释解释了 AICPU unfold 模式下的流模型：每槽一对「控制流 + worker SDMA 流」，配一个 device-only notify 把 worker 的完成信号桥接回控制流，另有 host flag 供 host 侧轮询。这是 u5-l3 的伏笔。

[fabric_mem_types.h:L67-L79](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_types.h#L67-L79)：`AsyncRecord`，一次在途异步请求的全部簿记：槽位、起止时间、字节数、op 数、channel 归属与 prof 元数据——`GetTransferStatus` 查询的就是它。

**传输服务抽象**：

[fabric_mem_transfer_service.h:L49-L80](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.h#L49-L80)：`FabricMemTransferService` 基类。注释点明架构意图：「拥有 channel manager 与 slot pool；Host/AICPU 子类实现不同的并发模型与拷贝提交路径」。连接类方法（Connect/Disconnect/HasChannels/StartKeepaliveMonitor）是基类非虚实现，传输类方法（TransferSync/Async/GetTransferStatus/CleanupAsyncTransfer）是纯虚、由子类各自实现。L79-L80 的静态 `MallocMem/FreeMem` 就是文档「25.5 HDK 下必须用它管理 HOST 内存」所指的接口。

[fabric_mem_transfer_service.h:L128-L131](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.h#L128-L131)：成员声明顺序刻意让 `slot_pool_` 先于 `channel_manager_` 构造、后析构——channel manager 的 keepalive/断链路径要把槽位归还给池，槽位池必须活得比它久。这是「成员声明顺序即析构逆序」这一 C++ 规则的工程化应用。

**虚拟内存管理器**：

[virtual_memory_manager.h:L24-L54](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/virtual_memory_manager.h#L24-L54)：进程级单例，对外提供 `ReserveMemory/ReleaseMemory`（预留/释放虚拟地址区间）与 `SetVirtualMemoryCapacity/SetGlobalStartAddress`（由 `FabricMemEngine::ApplyVirtualMemoryConfig` 在初始化最前面配置，见 [fabric_mem_engine.cc:L47-L57](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L47-L57)）。内部用 `bitmap_`（块占用位图）与 `allocations_`（地址→大小表）管理一块进程级预留的大虚拟地址空间——所有远端内存导入与本地映射都在这块空间里分配地址。

#### 4.3.4 代码实践

**实践目标**：不看讲义，仅凭目录名与头文件注释，独立完成 fabric_mem 模块的文件归类。

**操作步骤**：

1. 列出 `src/hixl/fabric_mem/` 全部文件（可用 `git ls-files src/hixl/fabric_mem`）。
2. 逐个打开 `.h` 文件的类声明与文件头注释，判断它属于 4.3.1 表格中的哪一组。
3. 用 Grep 验证归类：例如搜索 `class FabricMemControlServer`、`class FabricMemLocalMemory`、`class FabricMemSlotPool`，确认它们各自的定义文件与被持有关系（谁 `#include` 谁、谁作为成员出现在哪个类里）。
4. 把结果与设计文档 L53-L159 的 mermaid 类图对照，检查你推断的持有关系（如 `FabricMemChannel` 持有 `FabricMemRemoteMemory`）是否一致。

**需要观察的现象**：`fabric_mem_types.h` 被模块内几乎所有文件包含（它是共享类型层）；`acl_compat.h` 体积很小，只做 ACL 新旧接口的兼容封装。

**预期结果**：产出一张五组分类表（可复用 4.3.1 的表格自检），并能对任意文件说出「它被谁持有、它持有谁」。全程为源码阅读型实践，无需硬件。

#### 4.3.5 小练习与答案

**练习 1**：`ShareHandleInfo` 中 `share_handle` 与 `imported_handle` 的方向有何不同？

**答案**：`share_handle`（`aclrtMemFabricHandle`）是**本端内存导出**给对端用的凭证，由 `aclrtMemExportToShareableHandleV2` 产生、经控制面发给对端；`imported_handle`（`aclrtDrvMemHandle`）是**导入远端内存**后本端拿到的驱动句柄，配合 `imported_va`（映射后的本地虚拟地址）供本端直接访问。一个向外发、一个向里收，同一个结构体同时承载两个方向，是因为一条 channel 的内存账目里既有「我导出的」也有「我导入的」。

**练习 2**：为什么 `FabricMemTransferService` 把连接管理做成基类非虚方法、把传输方法做成纯虚方法？

**答案**：连接生命周期（Fetch 句柄 → Import 映射 → 登记 channel → keepalive）与传输拷贝方式无关，Host 与 AICPU 两条路径完全可以复用同一套 ChannelManager 逻辑；而拷贝提交与并发模型差异巨大——Host 路径用 `aclrtMemcpyAsync` + stream 同步，AICPU 路径经 dispatcher 向 AICPU 内核下发 SDMA 描述符——所以必须各自实现。基类注释「concrete Host/AICPU subclasses implement distinct concurrency models and copy submission paths」说的就是这层取舍。

**练习 3**：`VirtualMemoryManager` 为什么必须是单例？

**答案**：它管理的是**进程级**的一块全局预留虚拟地址空间（`global_virtual_memory_` + 位图）。同一进程内可能有多个 FabricMemEngine 实例（例如 u1-l5 见过的单进程双 engine 样例），若各持一份管理器，会对同一地址空间重复预留、互相冲突；单例保证容量与起始地址在进程内只有一份账本。

---

## 5. 综合实践

**任务：画出 FabricMem 模式下一次 D2D WRITE 传输的组件交互图，并与普通 HCCS 路径对比。**

具体步骤：

1. **画 FabricMem 侧**。以设计文档 [L161-L203](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/design/FabricMem模式设计.md#L161-L203) 的时序图为底稿，补上你从源码读到的细节：`TemporaryRtContext`（引擎入口恢复 context）、`EnsureAutoConnected`（auto_connect 隐式建链）、`submit_gate` 共享锁（提交与断链互斥）、`SlotPool` 槽位获取与归还、`FabricMemStatistic` 记账。参与方至少包括：User、FabricMemEngine、FabricMemTransferService、FabricMemChannelManager、FabricMemSlotPool、FabricMemControl（Server/Client）、FabricMemLocalMemory、FabricMemRemoteMemory、AscendRuntime（aclrt* 接口）。

2. **画普通 HCCS 侧**。依据 u3-l1/u3-l2/u4-l1 已建立的知识，画出 `HixlEngine` 路径的对应交互：ClientManager → HixlClient → ClientHandler（DIRECT）→ CS 层（MatchEndpoint/CreateChannel/GetRemoteMem 控制消息 → HcommChannel → HixlBatchPut 内核）。

3. **写对比分析**，至少覆盖四个维度：

   | 维度 | FabricMem | 普通 HCCS（HixlEngine+CS） |
   | --- | --- | --- |
   | 控制面交换物 | 共享内存句柄表（JSON over TCP） | endpoint 列表 + 内存导出描述 |
   | 数据面机制 | VMM 映射 + aclrtMemcpy/SDMA | Hcomm channel + AICPU 内核 |
   | D2RH 支持 | 原生支持 | 不支持 |
   | 内存注册含义 | 物理句柄导出 | 地址区间登记（MemStore 台账） |

4. **（可选，需 A3 环境）** 运行 `examples/cpp/fabric_mem_d2d.cpp` 样例验证你的交互图，对照日志中出现的关键事件（selected engine、registration、connect）核对每个组件是否按你画的顺序登场。此步待本地验证。

## 6. 本讲小结

- FabricMem 模式是为**超节点内 D2RH/RH2D 高带宽传输**而生：借 A3 的 FabricMemory 统一编址技术，让 NPU 经 HCCS 直达远端 DRAM，绕开 RoCE 带宽瓶颈且不占 HBM，服务于 Mooncake 等分布式 KV Cache 池场景。
- 底层依托 CANN **VMM 三步机制**（申请物理内存→预留虚拟地址→映射），进程间交换共享句柄后即可 SDMA 直访对端内存；`VirtualMemoryManager` 单例用位图管理进程级虚拟地址空间。
- `FabricMemEngine` 是 Engine 抽象的第三个实现、工厂的第一优先级分支，定位是**薄门面**：编排 TransferService / LocalMemory / ControlServer / Statistic 四个组件，自身只做门卫检查、ACL context 恢复与转发；批量状态查询与回调注册明确返回 `UNSUPPORTED`。
- `src/hixl/fabric_mem/` 按内存、控制、传输、通道与槽位、配置统计五组组织；`ShareHandleInfo`（导出/导入双向句柄）、`AsyncSlot`（控制流+SDMA 流+notify+host flag）、`AsyncRecord`（在途请求簿记）是三个核心数据结构。
- 并发设计的核心是 `submit_gate` 读写锁：传输提交持共享锁并行、断链持独占锁先 drain 再 abort/unmap，兼顾「断链不等传输」与「无悬空映射」。
- 运行约束：仅支持 Atlas A3 系列，需 HDK ≥ 25.5、灵衢 ≥ 1.5.0、CANN ≥ 9.0；25.5 HDK 下 HOST 内存必须用 `FabricMemTransferService::MallocMem/FreeMem` 管理；`enable_aicpu_unfold` 强制 `task_stream_num=1`。

## 7. 下一步学习建议

- **u5-l2（FabricMem 内存体系）**：精读 `fabric_mem_allocator.cc`、`fabric_mem_slot_pool.cc`、`virtual_memory_manager.cc` 与 `fabric_mem_memory.cc`，弄清一次注册从 allocator 到 share handle 表的完整链路——即本讲 `ShareHandleInfo` 的填表过程。
- **u5-l3（FabricMem 传输服务）**：对比 `fabric_mem_host_transfer_service.cc` 与 `fabric_mem_aicpu_transfer_service.cc` 两条拷贝路径的触发条件与并发模型，理解 `AsyncSlot` 双流模型的真实用法。
- **u5-l4（FabricMem 实战）**：运行 `examples/cpp/fabric_mem_d2d.cpp`，把本讲的综合实践交互图落到真实日志上。
- 若想回看对照面：重读 u4-l1（CS 架构）与 u3-l2（ClientHandler），体会「同一套公开 API、两套完全不同的数据面」正是 Engine 抽象 + 工厂模式的价值所在。
