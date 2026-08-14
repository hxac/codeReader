# u2-l2 CollComm 通信域对象与管理器

## 1. 本讲目标

上一讲（u2-l1）我们跟踪了 `HcclCommInitRootInfo` 从 C 接口一路走到 `hcclComm::InitCollComm`，在那里创建了一个 `CollComm` 对象并注册进单例管理器 `CollCommMgr`。本讲就深入这个"通信域对象本体"：

1. 理解 `CollComm` 封装了哪些关键状态与资源，能按「状态 / 资源 / 调度」给成员变量分类。
2. 掌握 `fullMode` 与 `simpleMode` 两种初始化模式的差异及其背后的芯片代际原因。
3. 理清 `CollComm` 析构时各资源的释放顺序，以及为什么顺序不能乱。
4. 掌握 `CollCommMgr` 的单例实现、按设备索引的共享资源表与多把互斥锁的并发控制方式。
5. 认识同目录下的独立算子（independent op）对象 `IndependentOp`，理解它与 `CollComm` 的关系。

## 2. 前置知识

- **通信域（communicator）**：一组 rank 的"户口本"，记录了谁在组里、每个人编号多少、走什么链路互联。回顾 u1-l1：控制面 `coll_communicator_mgr` 管通信域，数据面 `base_comm` 搬数据。
- **owner 对象与 C 化句柄**：回顾 u2-l1，`HcclComm` 句柄本质是 C++ 对象指针的 C 化转型；`CollComm` 持有的 `comm_` 则反向指回它的 owner（`Hccl::HcclCommunicator`，即 V2 新架构的通信域对象）。
- **芯片代际**：源码注释中 A2/A3 指 ascend910 系列（老架构，走 legacy 流程），A5 指 ascend950 及后续（新架构）。`CollComm` 要同时服务两代芯片，因此设计了两种初始化模式。
- **RAII 与智能指针**：C++ 中 `std::unique_ptr`/`std::shared_ptr` 在对象析构时自动释放所管理的资源。C++ 类成员的析构顺序与声明顺序**相反**（先构造的后析构），这一点在分析 `CollComm` 析构时会用到。
- **Meyers 单例**：函数内 `static` 局部变量在 C++11 起保证线程安全的惰性初始化，是实现单例的经典手法。
- **KFC / HDCommunicate**：host 与 device 上 AICPU 之间的共享内存命令通道（H2D 下发命令、D2H 回传状态），`HDCommunicate` 是其封装，本讲只需把它理解成"主机和 AICPU 之间的信箱"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/coll_communicator_mgr/communicator/coll_comm.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h) | `CollComm` 通信域上下文类声明，本讲主角之一 |
| [src/coll_communicator_mgr/communicator/coll_comm.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc) | `CollComm` 的初始化、对称内存、挂起/恢复与析构实现 |
| [src/coll_communicator_mgr/communicator/coll_comm_mgr.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.h) | `CollCommMgr` 管理器声明：多通信域注册表 + 按设备共享的资源表 |
| [src/coll_communicator_mgr/communicator/coll_comm_mgr.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc) | 单例获取、注册/注销、CCU 通信域预留、legacy 兼容接口实现 |
| [src/coll_communicator_mgr/communicator/independent_op.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/independent_op.h) | `IndependentOp` 独立算子资源对象声明 |
| [src/coll_communicator_mgr/communicator/independent_op.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/independent_op.cc) | 独立算子配置装配与 AICPU 侧通信域初始化 kernel 下发 |
| [src/base_comm/resources/comm_engine_res/engine_ctxs/independent_op_context_manager.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/engine_ctxs/independent_op_context_manager.h) | `ContextManager`：按 tag + 通信引擎管理的设备侧上下文内存表（被 `coll_comm.h` 包含，类型名 `ContextManager`） |

## 4. 核心概念与源码讲解

### 4.1 CollComm 对象全景：成员变量分类

#### 4.1.1 概念说明

一个通信域在本进程内需要记住三样东西：

- **我是谁**——rank 编号、通信域名、配置、当前状态；
- **我有什么**——拓扑图、通信引擎资源、内存、通道、维测句柄等一堆资源；
- **我怎么干活**——组调度器、owner 注入的回调函数。

`CollComm` 就是把这三样打包的"通信域上下文"。头文件注释写得很清楚：它要同时容纳老芯片（91092/91093）的通信域、新架构（91095）的通信域指针以及新独立算子架构的通信域。

#### 4.1.2 核心流程

读者可按下表给成员分类（本讲实践任务的核心产出）：

| 分类 | 成员 | 含义 |
| --- | --- | --- |
| 状态 | `comm_`、`rankId_`、`commId_`、`config_`、`commStatus_`、`deviceLogicId_`、`index_`、`initMode_`、`isCleaned_` | 身份标识与生命周期状态 |
| 资源 | `rankgraph_` / `rankGraphOwner_`、`commEngineResMgr_`、`contextMgr_`、`commMemMgr_`、`channelMgr_`、`myRank_`、`hcclCommDfx_`、`symmetricMemory_`、`cclBuffer_`、`binHcclHandle_`、`kfcControlTransferH2D_` / `kfcStatusTransferD2H_`、`rankIpPortMap_`、`addr_` / `size_` / `memType_` | 拓扑、引擎、内存、通道、维测等各类资源 |
| 调度 | `groupScheduleMgr`（public）、`callbacks_` | group 模式调度器与 owner 注入的回调 |

其中值得注意的三个设计细节：

1. `rankgraph_` 是裸指针、`rankGraphOwner_` 是 `unique_ptr`——同一个拓扑图两种持有方式：fullMode 下自己 new（放进 owner），simpleMode 下只是借用别人（`hccl::Communicator` 的静态对象）的指针、不负责释放。
2. `groupScheduleMgr` 是唯一声明为 public 的数据成员，注释 `// for group` 表明它直接供 group 调度路径使用，绕过了 getter 封装。
3. `binHcclHandle_` 单独配了一把 `binHcclmutex_`，说明二进制加载/卸载可能与其他路径并发。

#### 4.1.3 源码精读

类声明与职责注释见 [coll_comm.h:43-57](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h#L43-L57)：构造函数接收 owner 指针、rankId、通信域名、回调表和初始化模式。

两种初始化模式的枚举定义在 [coll_comm.h:48-51](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h#L48-L51)：

```cpp
enum class CollCommInitMode {
    fullMode, // 全功能模式：给A5及后续新架构使用，完整的CollComm初始化和资源管理
    simpleMode // 简化模式：给A2/A3老芯片使用，仅将RankGraph、MyRank等放入CollComm管理
};
```

资源型成员集中在 [coll_comm.h:163-185](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h#L163-L185)：从 `rankgraph_` 到 `binHcclmutex_`，几乎每个成员对应 `src/` 下一个子模块（RankGraph、comm_engine_res、comm_mem_manager、channel_manager、dfx、symmetric_memory）。可以说 **读 `CollComm` 的成员列表，就是在读控制面的模块清单**。

对外暴露资源的一组 inline getter 见 [coll_comm.h:63-69](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h#L63-L69)。注意 `GetChannelManager()` / `GetCommMemMgr()` 对应的成员在 `coll_comm.cc` 中没有创建代码，属于预留接口，是否在别的路径赋值——待确认。

#### 4.1.4 代码实践

「成员变量分类表」实践（即本讲总实践任务的第一步）：

1. **实践目标**：亲手整理 `CollComm` 成员分类，建立对通信域上下文的整体心智模型。
2. **操作步骤**：
   - 打开 `src/coll_communicator_mgr/communicator/coll_comm.h`，定位到 private 成员区（L150 起）。
   - 逐个成员判断它属于「状态 / 资源 / 调度」哪一类，填入类似 4.1.2 的表格。
   - 对每个"资源"成员，用 `Grep` 在仓库中搜索其类型名（如 `CommEngineResMgr`），确认该类型定义在哪个子目录。
3. **需要观察的现象**：几乎每个资源成员的类型都对应 `src/coll_communicator_mgr/` 或 `src/base_comm/` 下的一个子目录。
4. **预期结果**：得到一张约 25 行的分类表，且"资源"行的"类型来源"列能覆盖控制面大半子模块。
5. 本实践为纯源码阅读，无需硬件，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rankgraph_` 和 `rankGraphOwner_` 要分成裸指针和 `unique_ptr` 两个成员？

**答案**：fullMode 下 `CollComm` 自己创建并拥有拓扑图（存进 `rankGraphOwner_`，析构自动释放）；simpleMode 下拓扑图是 `hccl::Communicator` 中的静态对象，`CollComm` 只通过裸指针 `rankgraph_` 借用、不能释放。用一个指针成员无法区分"拥有"与"借用"两种语义。

**练习 2**：`GetRankSize()`（[coll_comm.h:84-97](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h#L84-L97)）失败时返回什么？这种设计有什么隐患？

**答案**：返回 `0`，同时打 `HCCL_ERROR` 日志。隐患是调用方若不检查日志，会把"出错"当成"组内没有 rank"，属于用哨兵值表达错误的常见取舍。

### 4.2 初始化流程：fullMode 与 simpleMode

#### 4.2.1 概念说明

`CollComm::Init` 是一个分发器：按构造时传入的 `initMode_` 把初始化分给 `InitFullMode` 或 `InitSimpleMode`。这样老芯片和新架构共用同一个 `CollComm` 外壳，但内部装配的资源集完全不同——这正是头文件注释"当前需包含原有的 91092/91093 的通信域、原有的 91095 的通信域及新独立算子架构的通信域"的落地方式。

#### 4.2.2 核心流程

`InitFullMode` 的装配顺序（对应源码行号见 4.2.3）：

```
Init(rankGraph, binHandle, cclBuffer, opExpansionMode)
 ├─ fullMode? ──是──> InitFullMode:
 │    1. DlHalFunctionInit            // 南向适配层符号加载
 │    2. rankGraphOwner_ = new RankGraphV2(rankGraph)   // 包装 owner 传入的拓扑
 │    3. GetRankIpPortMap             // 从 comm_ 取 rank→(IP→端口) 映射
 │    4. commEngineResMgr_->Init      // 通信引擎资源（线程数/notify 数用 0xffffffff 哨兵=未配置）
 │    5. contextMgr_ = new ContextManager  // 独立算子上下文表
 │    6. myRank_->Init                // 本 rank 资源（cclBuffer 等）
 │    7. hrtGetDevice(&deviceLogicId_)
 │    8. InitSymmetricMemory          // 对称内存（URMA 模式）
 │    9. InitHDCommunicate            // H2D/D2H 两条命令通道
 │    10. hcclCommDfx_->Init          // 维测
 │    11. InitTaskExceptionHandler    // 注册任务异常处理
 │    12. InitKfcAndRegisterCollComm  // 把命令通道交给 myRank，状态置 READY
 └─ 否 ──> InitSimpleMode:
      1. DlHalFunctionInit
      2. rankgraph_ = static_cast<RankGraph*>(rankGraph)  // 只借用裸指针
      3. myRank_->Init
      4. commStatus_ = READY
```

注意初始化顺序体现了依赖关系：`deviceLogicId_` 必须在第 8~11 步之前取到，因为对称内存、HD 通道、异常处理器都按设备逻辑 ID 索引。

#### 4.2.3 源码精读

分发入口 [coll_comm.cc:66-73](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L66-L73)：`Init` 只做 fullMode/simpleMode 二选一。

fullMode 主体 [coll_comm.cc:103-152](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L103-L152)，其中引擎资源配置片段：

```cpp
u32 threadNum = 0xffffffff;          // 哨兵值：表示"配置未设置"，由下层取默认
u32 notifyNumPerThread = 0xffffffff;
if (!commEngineResMgr_) {
    commEngineResMgr_ = std::make_unique<CommEngineResMgr>();
    CHK_PRT(commEngineResMgr_->Init(threadNum, notifyNumPerThread, commId_, binHandle, callbacks_));
}
```

simpleMode 主体 [coll_comm.cc:76-101](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L76-L101)，关键差异在 [coll_comm.cc:84-85](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L84-L85) 的注释：*"SimpleMode: A2/A3 的 RankGraph 是保存在 hccl::Communicator 中的静态对象裸指针，CollComm 不负责释放"*。

状态置 READY 与 Kfc 注册见 [coll_comm.cc:335-340](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L335-L340)：`InitKfcAndRegisterCollComm` 把 H2D/D2H 通道注入 `myRank_`，最后 `commStatus_ = HCCL_COMM_STATUS_READY`。

#### 4.2.4 代码实践

「跟踪初始化调用链」实践：

1. **实践目标**：把 u2-l1 学到的入口链与本讲的 `Init` 串成一条完整链路。
2. **操作步骤**：
   - 用 `Grep` 在 `src/` 中搜索 `->Init(` 且参数含 `rankGraph` 的调用点，找到谁调用了 `CollComm::Init`。
   - 沿调用点向上回溯到 `HcclCommInitCollComm`（u2-l1 已分析过），确认传入的 `rankGraph`、`binHandle`、`cclBuffer` 分别来自哪里。
   - 记录 fullMode 装配的 12 个步骤中，哪些步骤依赖 `deviceLogicId_`。
3. **需要观察的现象**：`deviceLogicId_` 的赋值（L133）位于使用它的初始化步骤之前。
4. **预期结果**：画出从 `HcclCommInitRootInfo` 到 `commStatus_ = READY` 的完整时序草图；若某些调用点在 legacy 或打包后代码中，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`threadNum = 0xffffffff` 传给 `CommEngineResMgr::Init` 是什么含义？

**答案**：这是"配置未设置"的哨兵值。`independent_op.h:30` 中专门定义了常量 `HCCL_COMM_THREADNUM_CONFIG_NOT_SET = 0xffffffff`，说明该哨兵是全模块约定，引擎资源管理器收到后应按默认策略决定线程数。

**练习 2**：如果 `InitFullMode` 中第 6 步 `myRank_->Init` 失败，前面已创建的 `commEngineResMgr_`、`contextMgr_` 会怎样？

**答案**：`CollComm` 的 `Init` 返回错误码，由上层（owner）决定是否销毁 `CollComm`。销毁时这些 `unique_ptr` 成员自动析构——这正是用智能指针持有资源的意义：半初始化状态也能被完整回收。

### 4.3 销毁流程与资源释放顺序

#### 4.3.1 概念说明

销毁一个通信域远不止 `delete` 一个对象：要先停掉可能还在运行的 AICPU 侧任务、注销异常回调、卸载设备二进制，最后才能释放内存。顺序错了会出现"资源已释放但回调还在引用"的悬垂访问。`~CollComm` 的注释把这类考量写得非常直白。

#### 4.3.2 核心流程

```
~CollComm
 ├─ 0. simpleMode? ──是──> 直接 return（资源不归我管）
 ├─ 1. TaskExceptionHost::UnRegister(this)   // 先注销异常回调，防止后续销毁触发 rts 回调悬垂
 ├─ 2. HcclBinaryUnLoad()                    // 卸载 AICPU 二进制（持 binHcclmutex_）
 ├─ 3. HcclTeamMgr::ClearByCollComm(this)    // 兜底释放所有 team 的 syncMem 本地内存
 ├─ 4. hcclCommDfx_->ReportAllTasks(false)   // DFX 兜底上报（异常退出也捕获，避免二次崩溃）
 ├─ 5. DestroyAicpuComm()                    // 经 H2D 通道发 DESTROY_AICPU_COMM，轮询 D2H 等完成（最长 10 秒）
 └─ 6. 成员自动析构（声明逆序）：symmetricMemory_、myRank_、hcclCommDfx_、
        commEngineResMgr_、rankGraphOwner_、cclBuffer_ ...
```

设计要点：**跨进程/跨设备的资源先收口（回调、二进制、team、AICPU 通信域），纯本进程内存最后靠 RAII 兜底**。

#### 4.3.3 源码精读

析构函数主体 [coll_comm.cc:40-64](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L40-L64)，第一步的注释是顺序正确性的直接证据：

```cpp
// 先注销TaskException，再销毁通信域资源，防止通信域资源销毁后rts回调TaskException
hcomm::TaskExceptionHost* handler = hcomm::TaskExceptionHost::GetInstance(deviceLogicId_);
if (handler != nullptr) {
    (void)handler->UnRegister(reinterpret_cast<u64>(this));
}
```

`DestroyAicpuComm` 见 [coll_comm.cc:342-374](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L342-L374)：仅当回调 `callbacks_.getAicpuCommState()` 为真（即 AICPU 侧通信域已初始化）时才执行；通过 `kfcControlTransferH2D_->Put` 下发 `DESTROY_AICPU_COMM` 命令，再在 10 秒超时内轮询 `kfcStatusTransferD2H_->Get` 等待 `DESTROY_AICPU_COMM_DONE`。

二进制加载/卸载是一对带锁的惰性接口：`GetHcclBinHandle`（[coll_comm.cc:518-541](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L518-L541)）首次调用时从磁盘加载 `libscatter_aicpu_kernel.json`，`HcclBinaryUnLoad`（[coll_comm.cc:543-559](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L543-L559)）对称卸载，二者共用 `binHcclmutex_`。

另外，通信域还有一条"非销毁"的状态机路径——快恢（NS recovery）：`Suspend`（[coll_comm.cc:417-430](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L417-L430)）挂起下发、`Clean`（[coll_comm.cc:432-452](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L432-L452)）清理、`Resume`（[coll_comm.cc:454-479](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L454-L479)）恢复，由 `commStatus_`（INVALID / READY / SUSPENDING）和 `isCleaned_` 双标志约束状态迁移。

#### 4.3.4 代码实践

「释放顺序排序」实践（本讲总实践任务的第二步）：

1. **实践目标**：理解销毁顺序背后的依赖约束，能说出"为什么 A 必须在 B 前"。
2. **操作步骤**：
   - 阅读 `~CollComm`（L40-64）与 `DestroyAicpuComm`（L342-374）。
   - 把 6 个销毁步骤写在小卡片上打乱，尝试还原顺序，并为每一步写一句"为什么它在这个位置"。
   - 挑战题：如果把第 1 步（注销 TaskException）移到最后一步，构造一个具体的故障场景。
3. **需要观察的现象**：每一步要么在释放"会被别人回调引用的东西"，要么在释放"依赖前面资源才可达的东西"。
4. **预期结果**：能写出类似"步骤 5 依赖步骤 9 创建的 kfc 通道，所以必须在成员析构前手动执行"的因果说明。
5. 本实践为纯源码阅读，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DestroyAicpuComm` 里要自己写轮询 + 10 秒超时，而不是同步等待？

**答案**：命令经共享内存下发给 AICPU 侧异步执行，host 只能轮询 D2H 状态通道确认 `DESTROY_AICPU_COMM_DONE`。超时上限（`WAIT_CMD_TIMEOUT = 10 * 1000` 毫秒）防止 AICPU 卡死时析构永久挂起，超时返回 `HCCL_E_TIMEOUT`。

**练习 2**：`Suspend` → `Clean` → `Resume` 与 `~CollComm` 是什么关系？

**答案**：前者是"可恢复"的运行时状态机，用于故障快恢（通信域还要继续用）；后者是最终销毁（通信域不再存在）。`Clean` 要求先 `Suspend` 成功（状态必须是 SUSPENDING，否则返回 `HCCL_E_NOT_SUPPORT`），`Resume` 则把状态推回 READY 并复位 `isCleaned_`。

### 4.4 CollCommMgr：单例、并发控制与多通信域共享

#### 4.4.1 概念说明

一个进程里可能同时存在多个通信域（world 组、多个 sub group、CCU 通信域……），还有一些资源天然是"每设备一份、跨通信域共享"的（集群监控、保序下发线程）。`CollCommMgr` 承担两个角色：

1. **户口本**：`allCollComms_` 以 commId 为 key 登记所有存活的 `CollComm`；
2. **共享资源表**：按 `deviceLogicId` 索引的 `ClusterMonitor`、`OrderLaunchThreadMgr` 数组，加上全局唯一的 `HcclTaskAbortHandler`。

#### 4.4.2 核心流程

- **单例**：Meyers 单例（函数内 static），进程唯一。
- **注册/注销**：均持 `mutex_` 全程加锁；注册时顺带把 `CollComm` 挂到任务中止处理器和该设备的保序下发管理器上；注销时还要从集群监控摘除。
- **并发控制分三把锁**，粒度按数据划分：

| 锁 | 保护的数据 | 场景 |
| --- | --- | --- |
| `mutex_` | `allCollComms_` | 通信域创建/销毁并发 |
| `ccuMsCommMutex_` | `ccuMsCommIds_` | 多通信域抢占"每设备唯一 CCU 通信域"资格 |
| `opHcomInfosMutex_` | `opHcomInfos_` / `baseCommInited_` | legacy 路径按设备初始化 base_comm 资源 |

- **CCU 通信域预留**：`TryReserveCcuMsComm` 是典型的"检查并占位"（check-then-set）临界区：同一设备上第一个到达者占住 commId，后来者拿到 `reserved = false`。

#### 4.4.3 源码精读

单例实现 [coll_comm_mgr.cc:17-21](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L17-L21)：

```cpp
CollCommMgr& CollCommMgr::GetInstance()
{
    static CollCommMgr instance;   // C++11 起线程安全
    return instance;
}
```

注册/注销 [coll_comm_mgr.cc:86-103](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L86-L103)：`RegisteCollComm` 在锁内完成"登记 + 挂调度钩子"，`UnRegisteCollComm` 对称地做"摘钩子 + 除名"，且额外从 `ClusterMonitor` 注销——这呼应 u2-l1 的结论："注册/注销由 owner 驱动"。

CCU 预留临界区 [coll_comm_mgr.cc:42-59](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L42-L59)：先做参数与越界校验，再在 `ccuMsCommMutex_` 内检查 `ccuMsCommIds_[deviceLogicId]` 是否为空、为空则占位。

成员与锁的声明 [coll_comm_mgr.h:49-60](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.h#L49-L60)：注意 `opHcomInfos_` 与 `baseCommInited_` 数组长度是 `MAX_MODULE_DEVICE_NUM + 1`，多出的一个槽位作为"未指定设备时的兜底"（见 `LegacyGetHcclOpInfoCtx` 中 `devId = MAX_MODULE_DEVICE_NUM` 的用法）。

以 `Legacy` 前缀标记的三个接口（[coll_comm_mgr.h:43-46](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.h#L43-L46)）服务老接口兼容，注释明确"仅做 bug 修复与兼容维护，不再承接新特性"，与 u1-l3 讲过的 legacy 定位一致。其中 [coll_comm_mgr.cc:114-115](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L114-L115) 有一条很有价值的锁注释：`baseCommInited_` 本身不加锁，因为它"在生产路径中始终在外层 `opHcomInfosMutex_` 锁内访问"——锁的职责上提了。

#### 4.4.4 代码实践

「锁与数据对应关系」实践：

1. **实践目标**：掌握多把锁按数据划分粒度的并发设计。
2. **操作步骤**：
   - 在 `coll_comm_mgr.cc` 中搜索全部 `std::lock_guard` 出现点。
   - 为每个出现点记录：加的是哪把锁、临界区内读写了哪些成员、临界区多长（行数）。
   - 检查 `GetInstance()` 和 `GetClusterMonitor()` 是否加锁，思考为什么可以不加。
3. **需要观察的现象**：三把锁从不嵌套；`GetClusterMonitor` 不加锁但做了越界检查并回退到 0 号槽位。
4. **预期结果**：一张「锁 → 数据 → 调用点」对照表；能解释 Meyers 单例为什么自身不需要锁。
5. 纯源码阅读实践，无需硬件。

#### 4.4.5 小练习与答案

**练习 1**：如果两个线程同时对同一设备调用 `TryReserveCcuMsComm`，结果如何？去掉锁会怎样？

**答案**：有锁时恰好一个线程拿到 `reserved = true`，另一个 `reserved = false`。去掉锁后两个线程可能同时读到 `owner.empty()` 为真、同时写入自己的 commId，导致同一设备出现两个"唯一"的 CCU 通信域（check-then-set 竞态）。

**练习 2**：`UnRegisteCollComm` 里为什么除了从 `allCollComms_` 除名，还要从 `ClusterMonitor` 和 `OrderLaunchThreadMgr` 注销？

**答案**：注册时把 `CollComm` 挂到了这两个按设备共享的组件上（`RegisteCollComm` L91-92）。若只除名不摘钩，共享组件会持有指向已析构 `CollComm` 的悬垂指针，后续集群监控回调或保序下发会访问已释放内存。

### 4.5 独立算子 IndependentOp 与 ContextManager

#### 4.5.1 概念说明

"独立算子（independent op）"是相对 group 集合通信而言的：不经过 HCCL 算子层编排、由用户/上层框架直接使用 HCOMM 资源接口组装的通信方式（对应 u1-l3 介绍的 L2-res/L3-prim 新接口体系）。同一目录下的 `IndependentOp` 类就是这条路径的"迷你版 CollComm"：它同样聚合了 `CommMemMgr`、`CommEngineResMgr`、`ContextManager`、`ChannelManager` 四大管理器，但不拥有 RankGraph 与 MyRank，也不再经 owner 创建。

`ContextManager`（头文件名 `independent_op_context_manager.h`，被 `coll_comm.h` 包含）按 `(tag, CommEngine)` 二级键管理设备侧上下文内存，是独立算子在数据面 base_comm 中的落脚点之一。

#### 4.5.2 核心流程

`IndependentOp` 的装配流程：

```
SetIndependentOpConfig(commConfig, rankTable, topoAttr, binHandle, ...)
 ├─ 1. 线程数/notify数 置 0xffffffff 哨兵（未配置）
 ├─ 2. 注册三个 lambda 回调：getAicpuCommState / setAicpuCommState / kernelLaunchAicpuCommInit
 ├─ 3. engineResMgr_.Init(...)   // 引擎资源
 ├─ 4. channelMgr_.Init(...)     // 通道（含 QoS）
 └─ 5. 组装 commAicpuParam_（AICPU 侧初始化参数）
KernelLaunchAicpuCommInit()
 ├─ 创建在线流并设 aicpu 模式
 ├─ AicpuAclKernelLaunch(..., "RunAicpuCommInit", ...)   // 下发初始化 kernel
 ├─ hcclStreamSynchronize(...)                            // 同步等待
 └─ HcommProfilingReportKernel(...)                       // 上报耗时
```

与 `CollComm` 的对比：`CollComm::InitFullMode` 装配 12 步、含拓扑与对称内存；`IndependentOp::SetIndependentOpConfig` 只装配 4 类管理器，回调表是**自己构造 lambda 注入自己**（扮演了 owner 的角色），而 `CollComm` 的 `callbacks_` 由外部 owner 传入。

#### 4.5.3 源码精读

`IndependentOp` 类声明与成员见 [independent_op.h:33-80](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/independent_op.h#L33-L80)：private 区四个管理器成员（L73-76）与 `CollComm` 的资源成员同名同类型，直观体现"迷你通信域"的定位。

回调自注入见 [independent_op.cc:36-45](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/independent_op.cc#L36-L45)：

```cpp
ManagerCallbacks callbacks;
callbacks.getAicpuCommState = [this]() { return this->GetAicpuCommState(); };
callbacks.setAicpuCommState = [this](bool state) { this->SetAicpuCommState(state); };
callbacks.kernelLaunchAicpuCommInit = [this]() { return this->KernelLaunchAicpuCommInit(); };
```

AICPU 侧通信域初始化 kernel 下发见 [independent_op.cc:77-103](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/independent_op.cc#L77-L103)：kernel 名为 `RunAicpuCommInit`，携带 `commAicpuParam_` 参数结构，同步执行后打点上报。

`ContextManager` 的二级映射结构见 [independent_op_context_manager.h:38-51](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/engine_ctxs/independent_op_context_manager.h#L38-L51)：`contextMap_` 的类型是 `unordered_map<tag, unordered_map<CommEngine, HcclMem>>`，即"每个标签下、每种通信引擎各有一块上下文内存"，Create/Get/Copy/Destroy 四个操作均由 `mutex_` 保护。它位于 `src/base_comm/` 下，说明独立算子的上下文最终落在数据面——这正是"控制面 → 数据面"依赖方向的又一次体现。

#### 4.5.4 代码实践

「双对象对照」实践：

1. **实践目标**：理解 `CollComm` 与 `IndependentOp` 的同构关系及各自适用路径。
2. **操作步骤**：
   - 并排打开 `coll_comm.h` 与 `independent_op.h` 的 private 成员区。
   - 列出两者共同拥有的管理器成员（提示：`CommMemMgr`、`CommEngineResMgr`、`ContextManager`、`ChannelManager`）。
   - 用 `Grep` 搜索 `IndependentOp` 在 `src/` 中的实例化位置，确认它由哪条入口路径创建（提示：可从 `SetIndependentOpConfig` 的调用方向上回溯）。
3. **需要观察的现象**：`IndependentOp` 的管理器成员是**值成员**（栈内直接包含），而 `CollComm` 用智能指针持有——思考两者生命周期的差异。
4. **预期结果**：一张双栏对照表；能指出 `CollComm` 走 HCCL 集合通信路径、`IndependentOp` 服务独立算子直连资源路径。实例化入口若链路过长，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`ContextManager::contextMap_` 为什么是"tag → (引擎 → 内存)"的两级 map，而不是一级？

**答案**：同一个 tag（可理解为同一通信域/同一业务标签）可能同时在多种通信引擎（AICPU/AIV/CCU 等，回顾 u1-l1 的四种引擎）上持有各自的上下文。两级 map 允许同一标签按引擎隔离互不覆盖，销毁时也可只销毁某一引擎的上下文（`DestroyCommEngineCtx(tag, engine)`）。

**练习 2**：`IndependentOp` 的 `ManagerCallbacks` 与 `CollComm` 的 `callbacks_` 来源有何不同？为什么？

**答案**：`CollComm` 的回调由外部 owner（`HcclCommunicator` 创建方）在构造时传入，维持"依赖自上而下"；`IndependentOp` 没有外部 owner，于是在 `SetIndependentOpConfig` 内用 lambda 捕获 `this` 自己构造回调，自己扮演 owner。

## 5. 综合实践

**任务：编写一份《CollComm 成员清单与生命周期报告》。**

1. 打开 [coll_comm.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h)，把全部成员变量列成表格，包含四列：成员名、分类（状态/资源/调度）、类型来源模块、创建位置（`coll_comm.cc` 行号或"自动默认初始化"）。
2. 对照 [coll_comm.cc:40-64](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.cc#L40-L64) 的析构函数，为每个资源成员标注释放方式："手动步骤 1~5 之一"或"RAII 自动析构"。
3. 用一段文字回答关键问题：手动释放的 5 个步骤分别保护了哪类跨边界资源（回调/设备二进制/team 内存/DFX 上报/AICPU 侧通信域），为什么它们不能交给 RAII？
4. 进阶（可选）：对比 `CollCommMgr::RegisteCollComm` / `UnRegisteCollComm`，说明"对象析构"与"从管理器除名"为什么必须由 owner 按固定顺序先后触发，而不是都塞进 `~CollComm`。

预期成果：一份可直接归档到团队 wiki 的 markdown 报告。全程无需昇腾硬件。

## 6. 本讲小结

- `CollComm` 是通信域上下文的唯一聚合点，其成员列表几乎一一对应控制面各子模块：状态类记录身份与生命周期，资源类持有拓扑/引擎/内存/通道/维测，调度类包含 `groupScheduleMgr` 与 owner 回调。
- 初始化按 `CollCommInitMode` 分流：A5 新架构走 12 步的 `InitFullMode`（自持 RankGraphV2、对称内存、KFC 通道、DFX），A2/A3 老芯片走只借用拓扑裸指针的 `InitSimpleMode`。
- 析构顺序有严格约束：先注销 TaskException 回调 → 卸载二进制 → 兜底清 team 内存 → DFX 上报 → 通知 AICPU 销毁通信域，之后才交给 RAII 按声明逆序释放内存。
- `CollCommMgr` 是 Meyers 单例，用 `mutex_`、`ccuMsCommMutex_`、`opHcomInfosMutex_` 三把锁按数据划分并发粒度，同时管理跨通信域共享的每设备 `ClusterMonitor` 与 `OrderLaunchThreadMgr`。
- `IndependentOp` 是独立算子路径的"迷你 CollComm"：同构地聚合四大管理器，但无 owner、回调自注入，AICPU 侧初始化通过下发 `RunAicpuCommInit` kernel 完成。

## 7. 下一步学习建议

- 下一讲 u2-l3 将进入 `config_mgr`，讲解 `HcclSetConfig`/`HcclGetConfig` 背后的 `CommConfig`——本讲中反复出现的 `config_` 成员正是它的实例。
- 若想先补齐本讲引用的子模块，建议按顺序阅读：`my_rank.h`（`MyRank` 本 rank 资源）、`comm_engine_res_manager.h`（`CommEngineResMgr`）、`hcclCommDfx.h`（维测），它们都将在 u6-l2 DFX 讲义中再次出现。
- 对对称内存感兴趣的读者可以提前浏览 `src/base_comm` 下 `symmetric_memory/` 目录，u3-l3 内存注册讲义会系统展开。
