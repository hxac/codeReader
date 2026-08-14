# 基础通信层总览：资源管理器 HcommResMgr

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `src/base_comm/` 目录的三大组成部分（common / primitives / resources）以及各自的职责边界。
2. 理解 `HcommResMgr` 这个"按设备取单例"的进程级资源聚合器是如何工作的，它聚合了哪些底层单例，以及它为什么目前是一个"临时方案"。
3. 区分数据面的 `hcomm::CommEngineResMgr`（接口骨架）与控制面真正落地的 `hccl::CommEngineResMgr`（线程/Notify/引擎上下文的实际管理者），避免把两者混淆。
4. 建立数据面资源的整体心智模型：每类资源（Endpoint、注册内存、线程、引擎上下文）对应哪个对外创建接口。
5. 亲手绘制一张 base_comm 资源类关系图，标注创建接口入口。

本讲是数据面（base_comm）单元的第一讲，承接 u1-l3 建立的"控制面 coll_communicator_mgr / 数据面 base_comm"代码地图。后续讲义（Endpoint、内存注册、Channel、线程、原语）都会反复用到本讲建立的资源全景。

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（u1-l1、u1-l3、u2-l2 已讲过，这里只做一句话回顾）：

- **控制面 / 数据面**：控制面（`src/coll_communicator_mgr/`）负责通信域的生命周期、拓扑和配置；数据面（`src/base_comm/`）负责真正搬数据的资源与原语。依赖方向严格自上而下。
- **通信域（CollComm）**：一次集合通信任务的上下文聚合点（u2-l2），它持有数据面资源的入口。
- **通信引擎（CommEngine）**：执行通信任务的硬件/软件执行体，如 AICPU_TS、CPU_TS、AIV、CCU（u1-l1）。"引擎资源"就是围绕某个引擎的线程、Notify（通知量）和上下文内存。
- **单例（Singleton）**：一个进程里只存在一份的全局对象，通常通过 `Xxx::GetInstance()` 获取。本讲会大量出现"以 `GetInstance()` 触发静态对象声明"的写法。
- **Meyers 单例**：C++ 中把 `static` 局部变量放在函数内实现的单例，C++11 起由编译器保证其线程安全初始化。
- **设备物理 ID（devicePhyId）与逻辑 ID（deviceLogicId）**：昇腾设备有两种编号，物理 ID 是整机内的唯一编号，逻辑 ID 是进程视角的编号。`HcommResMgr` 按物理 ID 索引资源。

还要解释两个本讲新出现的术语：

- **bootstrap（自举）**：指"库在被正式使用之前，先把依赖的底层单例初始化好"的动作。`HcommResMgr::GetInstance` 的核心作用就是 bootstrap。
- **Device Reset 回调**：当用户调用 `aclrtResetDevice` 重置设备时，ACL 运行时会在重置前后发出状态通知；库注册回调后可以在设备销毁前清理 socket、RDMA 句柄等资源，避免悬挂引用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/base_comm/hcomm_res_mgr.h` | `HcommResMgr` 类声明：按设备取实例 + 注册设备复位回调 |
| `src/base_comm/hcomm_res_mgr.cc` | 实现：触发约 20 个底层单例的静态声明、注册 `OnDeviceResetPre` 回调 |
| `src/base_comm/resources/comm_engine_res/comm_engine_res_mgr.h` | 数据面 `hcomm::CommEngineResMgr` 声明（按引擎类型管理 `CommEngineRes`） |
| `src/base_comm/resources/comm_engine_res/comm_engine_res.h` | 数据面 `hcomm::CommEngineRes` 声明（线程申请/释放、引擎上下文） |
| `src/base_comm/resources/comm_engine_res/engine_ctxs/engine_ctx_mgr.h` | `EngineCtxMgr`：按 "引擎+OpTag" 键值管理引擎上下文内存 |
| `src/base_comm/resources/comm_engine_res/threads/thread.h` | `Thread` 抽象接口：通信引擎的并行资源（含 Notify） |
| `src/base_comm/primitives/api_c_adpt/hcomm_c_adpt.cc` | `HcommResMgrInit`：所有 L3 数据面 C 接口的统一 bootstrap 入口 |
| `src/coll_communicator_mgr/communicator/coll_comm_mgr.cc` | `CollCommMgr::InitBaseCommRes`：控制面创建通信域时触发数据面 bootstrap |
| `src/coll_communicator_mgr/resource_mgr/local/my_rank/comm_engine/comm_engine_res_manager.h` | 控制面 `hccl::CommEngineResMgr`：线程/Notify 的实际管理实现 |
| `src/coll_communicator_mgr/api_c_adpt/resource/thread_c_adpt.cc` | `HcclGetNotifyNumInThread` 等 C 接口如何路由到 `hccl::CommEngineResMgr` |
| `src/coll_communicator_mgr/api_c_adpt/resource/engine_ctx_c_adpt.cc` | `HcclEngineCtxCreate` 如何路由到 `EngineCtxs` / `ContextManager` |
| `include/hcomm_res.h` | L3 数据面对外 C 接口：Endpoint 创建、内存注册/导入、线程申请 |
| `include/hccl/hccl_res.h` | L2 资源对外 C 接口：`HcclThreadAcquire` 系列、`HcclEngineCtxCreate` |

## 4. 核心概念与源码讲解

### 4.1 base_comm 的组成：与业务无关的通信地基

#### 4.1.1 概念说明

官方架构文档对基础通信模块的定位只有一句话，但很关键：

> 基础通信模块，与业务无关，封装底层硬件与协议，为上层框架提供统一的通信抽象。（[docs/zh/architecture/base_comm/README.md:3](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/architecture/base_comm/README.md#L3)）

"与业务无关"是它和控制面的本质区别：base_comm 不知道什么是 AllReduce、什么是通信域，它只认识 Endpoint（端口）、Channel（通道）、内存、线程和读写原语这些"通信原子构件"。这样它才能同时被新架构（V2）、独立算子路径（IndependentOp）甚至 CCU 算子开发者复用。

#### 4.1.2 核心流程

base_comm 目录下分为四块（外加 `dfx` 维测）：

```text
src/base_comm/
├── common/          # 公共设施：引擎工具、EID 信息、协议工具、动态加载 urma 等
├── primitives/      # 对外原语与 C 接口适配（api_c_adpt、launch_context、aicpu 任务缓存）
├── resources/       # 资源实现：endpoints、reged_mems、comm_engine_res、hccp、ccu、
│                    #   southbound_adpt、endpoint_pairs
├── dfx/             # 数据面维测（异常回调管理等）
├── hcomm_res_mgr.h/cc  # 本讲主角：进程级资源聚合器
└── CMakeLists.txt
```

resources 下各子目录与对外接口的对应关系（这是本讲要求你记住的"资源地图"）：

| resources 子目录 | 管理的资源 | 对外创建/使用接口 |
| --- | --- | --- |
| `endpoints/` | 各协议端点（RoCE/URMA/HCCS/UB…） | `HcommEndpointCreate` / `HcommEndpointDestroy`（include/hcomm_res.h） |
| `reged_mems/` | 注册内存（本端注册、对端导入） | `HcommMemReg` / `HcommMemImport` / `HcommMemUnreg` |
| `comm_engine_res/` | 通信引擎资源（线程、Notify、引擎上下文） | `HcclThreadAcquire` 系列、`HcclEngineCtxCreate`（include/hccl/hccl_res.h） |
| `hccp/` | 进程间点对点通信服务（u4-l2 详讲） | 内部使用，经 endpoint_pairs 间接暴露 |
| `ccu/` | CCU 通信引擎资源（u5-l1 详讲） | CCU 算子开发接口 |
| `southbound_adpt/` | 南向适配器（HCCP/RTS/Runtime/URMA） | 纯内部，u4-l1 详讲 |
| `endpoint_pairs/` | 端点对之间的连接（socket 等） | 内部使用 |

primitives 则是"用这些资源做事情"的一层：`HcommLocalCopyOnThread`、`HcommWriteOnThread` 等原语在 u3-l6/u3-l7 详讲。

#### 4.1.3 源码精读

目录结构可以直接用 `ls` 验证（本讲实测，见 4.1.4 实践步骤 1）。对应的对外接口声明集中在两个头文件：

- L3 数据面资源接口，全部以弱符号 `extern` 声明：[include/hcomm_res.h:21-71](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_res.h#L21-L71) —— 这段依次声明了 Endpoint 创建/销毁/查询（L21-L42）、内存注册与导入（L44-L55）、线程资源查询与释放（L57-L64）、内存分配释放（L69-L71）。
- L2 线程与引擎上下文接口：[include/hccl/hccl_res.h:70-141](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_res.h#L70-L141) —— `HcclThreadAcquire`（L70）、`HcclThreadAcquireWithConfig`（L85）、`HcclThreadAcquireWithStream`（L99）、`HcclDedicatedThreadAcquire`（L122）、`HcclEngineCtxCreate`（L141）。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 base_comm 的目录划分，而不是只看讲义上的图。
2. **操作步骤**：
   ```bash
   ls src/base_comm/
   ls src/base_comm/resources/
   ls src/base_comm/primitives/
   ```
3. **需要观察的现象**：`resources/` 下是否正好有 4.1.2 表格中列出的 7 个子目录；`primitives/` 下是否只有 `aicpu`、`api_c_adpt`、`dfx`、`launch_context.*` 四项。
4. **预期结果**：与 4.1.2 的描述一致。若后续仓库演进导致目录变化，应以 `ls` 实际结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `southbound_adpt`（南向适配层）放在 base_comm 的 `resources/` 下，而不是单独作为一层放在 base_comm 与硬件之间？

**答案**：因为南向适配器本身就是"资源"的一种——它们是 Endpoint、内存、通道实现所依赖的底层句柄封装（HCCP、RTS、Runtime、URMA）。把它们放在 resources 内，使"资源类型"与"资源依赖的底层栈"在同一个模块里闭环，base_comm 对上只暴露统一抽象，对下的差异被限制在 resources 内部（u4-l1 详讲）。

**练习 2**：用户调用 `HcclAllReduce`（集合通信算子）时，请求会进入 base_comm 吗？为什么？

**答案**：会，但不是直接进入。`HcclAllReduce` 属于 L1 算子层，先进入控制面的通信域（CollComm），由控制面完成算法编排后，最终通过 base_comm 的资源（Channel、线程）和原语（Write/Reduce/Notify）执行实际数据搬运。base_comm 本身看不到"这是 AllReduce"。

### 4.2 HcommResMgr：按设备的进程级资源聚合器

#### 4.2.1 概念说明

base_comm 依赖大量底层单例（socket 管理器、RDMA 句柄管理器、CCU 组件、TP 管理器等）。这些单例分散在十几个头文件里，如果让每个调用方自己去"按正确顺序触发初始化"，时序极难保证。`HcommResMgr` 的职责就是把这些触发动作收敛到一个入口：

- **按设备索引**：每个设备物理 ID 对应一个 `HcommResMgr` 实例（静态数组实现），首次访问某设备时统一触发该设备相关单例的声明。
- **生命周期锚点**：注册设备复位回调，在设备被重置前清理 socket、RDMA 等全局资源。

注意类声明本身非常"轻"：

```cpp
class HcommResMgr {
public:
    static HcommResMgr& GetInstance(const uint32_t devicePhyId);
    static void RegisterDeviceResetCallback();
private:
    HcommResMgr();
    ~HcommResMgr();
    ...
    uint32_t devPhyId_{0};
};
```

它没有任何业务成员——真正的"管理"发生在 `GetInstance` 内部对其他单例的触达，以及源码注释里坦率的说明："临时方案：只声明单例对象做生命周期控制，不执行业务动作"。

#### 4.2.2 核心流程

`HcommResMgr::GetInstance(devPhyId)` 的执行流程：

1. 若 `devPhyId >= MAX_MODULE_DEVICE_NUM`（65，单 server 双模组最大设备数），打告警并回落到备份下标 `MAX_MODULE_DEVICE_NUM`。
2. 查函数内静态数组 `isInitialized`：该设备首次访问时，依次触发约 20 个底层单例的 `GetInstance(...)`（orion 通用平台层 → legacy CCU 单例 → 开源开放架构 CCU 单例）。
3. 返回函数内静态数组 `hcommResMgrs[devPhyId]` 中对应的引用，并置位 `isInitialized`。

设备复位路径：

```text
用户调用 aclrtResetDevice
  → ACL 运行时发出 ACL_RT_DEVICE_STATE_RESET_PRE 状态
  → OnDeviceResetPre(deviceId, state, args)
      → hrtGetDevicePhyIdByIndex 换算物理 ID
      → 依次 DeInit：SocketMgr / ServerSocketMgr / ServerSocketManager /
                     RdmaHandleManager / SocketHandleManager / HccpHdcManager
```

一个关键设计点：`isInitialized` 和 `hcommResMgrs` 都是 **`GetInstance` 函数内的静态变量**（Meyers 单例变体），首次调用时由 C++ 运行时保证线程安全初始化；而"哪些单例已被触发"的记忆也在同一处，避免重复 bootstrap。

#### 4.2.3 源码精读

类的公开接口只有两个静态方法：[src/base_comm/hcomm_res_mgr.h:18-30](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.h#L18-L30) —— 声明了 `GetInstance(devicePhyId)` 与 `RegisterDeviceResetCallback()`，构造/析构/拷贝全部私有或删除，是典型的单例写法。

`GetInstance` 的完整实现：[src/base_comm/hcomm_res_mgr.cc:49-96](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.cc#L49-L96)。三个关键片段：

- [hcomm_res_mgr.cc:51-60](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.cc#L51-L60)：函数内静态 `isInitialized` 数组 + 越界回落备份设备。
- [hcomm_res_mgr.cc:61-89](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.cc#L61-L89)：首次访问时按"orion 通用平台层单例（L65-L75）→ legacy CCU 单例（L77-L79）→ 开源开放架构 CCU 单例（L81-L88）"三组触发约 20 个 `GetInstance`，源码注释明确写着"临时方案：只声明单例对象做生命周期控制，不执行业务动作"。
- [hcomm_res_mgr.cc:91-95](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.cc#L91-L95)：返回函数内静态数组 `hcommResMgrs` 中的引用——每个设备一份实例。

设备复位回调：[hcomm_res_mgr.cc:110-135](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.cc#L110-L135) —— `OnDeviceResetPre` 只处理 `ACL_RT_DEVICE_STATE_RESET_PRE` 状态，把逻辑 ID 换算成物理 ID 后依次 `DeInit` 六个 socket/RDMA 相关管理器；整体包在 try/catch 里，回调中不允许抛异常。注册动作在 [hcomm_res_mgr.cc:137-150](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.cc#L137-L150)：用互斥锁 + 全局布尔量保证 `aclrtRegDeviceStateCallback` 只注册一次。

那么谁来调用 `HcommResMgr`？两条入口：

1. **数据面自己的 C 接口**：`HcommResMgrInit`（[src/base_comm/primitives/api_c_adpt/hcomm_c_adpt.cc:71-91](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/primitives/api_c_adpt/hcomm_c_adpt.cc#L71-L91)）在缺省 `devPhyId` 时先解析当前设备物理 ID，再触发 `HcommResMgr::GetInstance`。所有 L3 接口（Endpoint、Channel、Thread 的 C 适配层）入口处都会先调一次 `HcommResMgrInit()`，例如 [src/base_comm/resources/endpoints/builtin_endpoint_ops.h:37](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/endpoints/builtin_endpoint_ops.h#L37) 和 [src/base_comm/primitives/api_c_adpt/hcomm_thread_c_adpt.cc:40](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/primitives/api_c_adpt/hcomm_thread_c_adpt.cc#L40)。由于内部有 `isInitialized` 记忆，重复调用代价极小。
2. **控制面创建通信域时**：[src/coll_communicator_mgr/communicator/coll_comm_mgr.cc:107](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L107) 的 `CollCommMgr::InitBaseCommRes(devId)` 一行转调 `HcommResMgrInit(devId)`——这正是 u2-l2 中 CollComm 装配流程的数据面触发点。

此外，Endpoint 相关 C 接口还会注册设备复位回调，例如 [src/base_comm/primitives/api_c_adpt/hcomm_endpoint_c_adpt.cc:214](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/primitives/api_c_adpt/hcomm_endpoint_c_adpt.cc#L214)（同样带"只注册一次"保护）。

#### 4.2.4 代码实践

1. **实践目标**：数清 `HcommResMgr::GetInstance` 触发的单例清单，并按注释里的三组分类。
2. **操作步骤**：
   ```bash
   grep -n "GetInstance" src/base_comm/hcomm_res_mgr.cc
   grep -rn "HcommResMgrInit" src/base_comm/primitives/api_c_adpt/ | head
   grep -rn "HcommResMgrInit\|InitBaseCommRes" src/coll_communicator_mgr/ | head
   ```
3. **需要观察的现象**：`hcomm_res_mgr.cc` 内 `GetInstance` 调用的数量与三段注释分组；`HcommResMgrInit` 在各 C 适配层入口的分布密度；控制面只有 `InitBaseCommRes` 一处转调。
4. **预期结果**：`hcomm_res_mgr.cc` L65-L88 共约 20 个 `GetInstance` 调用；C 适配层几乎每个对外接口入口都有一次 `(void)HcommResMgrInit()`；控制面仅 `coll_comm_mgr.cc:107` 一处。（grep 计数为本地实测，后续版本可能增删。）

#### 4.2.5 小练习与答案

**练习 1**：`HcommResMgr` 的构造函数和析构函数都是空的（"临时方案：最小化修改不做处理"），这会带来什么风险？源码注释里规划的最终形态是什么？

**答案**：风险是进程退出/设备卸载时没有统一的销毁时序，各单例按静态对象析构的逆序自行销毁，跨单例依赖可能产生悬挂引用。注释规划的最终形态是：把各种单例转为 `HcommResMgr` 的成员变量，在构造函数中声明、在析构函数中主动调用销毁流程，保证销毁时序。（见 [hcomm_res_mgr.cc:98-108](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.cc#L98-L108)。）

**练习 2**：为什么 `OnDeviceResetPre` 里要先 `hrtGetDevicePhyIdByIndex` 把逻辑 ID 换成物理 ID，再执行各管理器的 `DeInit`？

**答案**：因为 `HcommResMgr` 及其触发的各设备级单例都是按**物理 ID** 索引的（`GetInstance(devicePhyId)`），而 ACL 回调给出的是用户进程视角的逻辑 `deviceId`。两者编号体系不同，必须换算到同一索引空间，`DeInit` 才能命中正确的资源集合。

**练习 3**：如果用户进程先后在设备 0 和设备 1 上创建通信域，`GetInstance` 被调用两次，第二次还会执行那约 20 个单例触发吗？

**答案**：会。`isInitialized` 是按 `devPhyId` 下标记忆的，设备 1 首次访问仍会触发一遍（其中按设备索引的单例如 `SocketMgr::GetInstance(devicePhyId)` 会为设备 1 建新实例；进程级单例如 `Hccl::HccpHdcManager::GetInstance()` 则只是再次确认已初始化）。

### 4.3 comm_engine_res：通信引擎资源的两种面孔

#### 4.3.1 概念说明

"通信引擎资源"指围绕某一类引擎（AICPU_TS / CPU_TS / AIV / CCU）的线程、Notify（通知量）和引擎上下文内存。仓库里有**两个同名类** `CommEngineResMgr`，初学者极易混淆，本讲专门把它们区分清楚：

| | 数据面 `hcomm::CommEngineResMgr` | 控制面 `hccl::CommEngineResMgr` |
| --- | --- | --- |
| 位置 | `src/base_comm/resources/comm_engine_res/comm_engine_res_mgr.h` | `src/coll_communicator_mgr/resource_mgr/local/my_rank/comm_engine/comm_engine_res_manager.h` |
| 状态 | **声明骨架**：`.cc` 文件是空的 `namespace hcomm {}` | **实际落地**：被 `thread_c_adpt.cc` 等真实调用 |
| 设计意图 | 按引擎类型管理 `CommEngineRes`（未来形态） | 按 `ThreadMgr` + `NotifyManager` 管理线程与通知量（现行实现） |
| 持有者 | ——（当前无人持有） | `CollComm`（u2-l2 讲过的 `commEngineResMgr_` 成员）与 `IndependentOp` |

同样要注意：`hcomm::CommEngineRes`（[comm_engine_res.h:22-47](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/comm_engine_res.h#L22-L47)）声明了 `AllocateThreads`/`ReleaseThreads`/`AcquireEngineCtx` 等接口，但 [comm_engine_res.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/comm_engine_res.cc#L11) 的实现体同样是空命名空间。**这是开源仓库常见的"接口先行"现象：头文件表达了目标架构，实现尚未（在开源分支）落地。** 阅读时不要假设这些函数真的能链接调用。

不过 `comm_engine_res` 目录并非空壳——它下面**已实现**的子模块才是现行数据面的真实构件：

- `threads/`：`Thread` 抽象基类及 AICPU_TS / AIV / CPU_TS 三种实现（u3-l5 详讲）。
- `engine_ctxs/`：`EngineCtxMgr` 引擎上下文管理器（已实现，96 行 `.cc`）。
- `launch/`：AICPU launch 管理器。
- `hcomm_mem_alloc.*`：数据面内存分配。

#### 4.3.2 核心流程

一次 `HcclThreadAcquire`（用户视角）的完整路径：

```text
HcclThreadAcquire(comm, ...)                      # include/hccl/hccl_res.h 声明
  → thread_c_adpt.cc 适配层
      ├─ V2 架构: collComm->GetCommEngineResMgr()  # hccl::CommEngineResMgr（控制面）
      └─ 独立算子: independentOp.GetCommEngineResMgr()
          → ThreadMgr / NotifyManager              # 实际创建 Thread 与 Notify
              → threads/ 下的 AicpuTsThread / AivThread / CpuTsThread（base_comm 实现）
```

一次 `HcclEngineCtxCreate` 的路径（[engine_ctx_c_adpt.cc:25-74](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/resource/engine_ctx_c_adpt.cc#L25-L74)）：

```text
HcclEngineCtxCreate(comm, ctxTag, engine, size, &ctx)
  ├─ V2 架构: myRank->GetEngineCtxs()->CreateCommEngineCtx(...)   # 按 ctxTag+engine 建/查
  └─ 独立算子: independentOp.GetContextManager().CreateCommEngineCtx(...)
```

两条路径共同点：**接口都从控制面进入，数据面提供实现构件**；这就是 u1-l3 强调的"依赖方向自上而下"。

#### 4.3.3 源码精读

数据面骨架声明：[src/base_comm/resources/comm_engine_res/comm_engine_res_mgr.h:23-34](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/comm_engine_res_mgr.h#L23-L34) —— 类注释"职责：管理不同的通信引擎的资源"，用 `unordered_map<CommEngineType, shared_ptr<CommEngineRes>>` 按引擎类型持有资源，互斥锁保护。但对照实现文件 [comm_engine_res_mgr.cc:11](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/comm_engine_res_mgr.cc#L11)：只有一行空的 `namespace hcomm {}`，`GetEngineRes` 全仓库检索不到任何实现或调用——确认是"声明先行"的骨架。`CommEngineType` 的枚举定义来自依赖头文件（经 `threads/thread.h` 引入的平台公共头），仓库内未检索到其定义处（待确认具体位置）。

`CommEngineRes` 声明：[src/base_comm/resources/comm_engine_res/comm_engine_res.h:22-47](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/comm_engine_res.h#L22-L47) —— 注释"职责：管理同一种通信引擎下的不同资源"，声明了线程申请/释放、引擎上下文获取/释放四个接口，成员为 `threads_` 与 `engineCtxs_` 两个容器。同样只有声明。

真正落地的 `EngineCtxMgr`：[src/base_comm/resources/comm_engine_res/engine_ctxs/engine_ctx_mgr.h:20-39](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/engine_ctxs/engine_ctx_mgr.h#L20-L39) —— 注释"职责：管理不同通信引擎下的内存 Ctx"，用 `unordered_map<string, void*>` 以 `GenerateCtxKey(engineType, opTag)` 生成的字符串键管理上下文，`AcquireEngineCtx` 支持"存在即复用、不存在即新建"（`newCreated` 出参）。

`Thread` 抽象接口：[src/base_comm/resources/comm_engine_res/threads/thread.h:65-79](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/threads/thread.h#L65-L79) —— 纯虚接口定义了并行资源的生命周期（`Init`/`DeInit`）、Notify 管理（`GetNotifyNum`/`GetNotify`/`SupplementNotify`）与任务下发（`LaunchTask`/`TryLaunchTask`）；同文件 [thread.h:62-64](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/resources/comm_engine_res/threads/thread.h#L62-L64) 定义了容量上限常量（每线程 Notify 上限 64、线程数上限 1000 等）。

现行实现侧 `hccl::CommEngineResMgr`：[src/coll_communicator_mgr/resource_mgr/local/my_rank/comm_engine/comm_engine_res_manager.h:22-48](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/resource_mgr/local/my_rank/comm_engine/comm_engine_res_manager.h#L22-L48) —— 聚合 `ThreadMgr` 与 `NotifyManager` 两个 unique_ptr，对外提供 `HcclThreadAcquire` 系列、`HcclAllocNotify`、`HcclDedicatedThreadAcquire` 等十余个方法（实现在同目录 `comm_engine_res_manager.cc`）。

适配层的真实路由：[src/coll_communicator_mgr/api_c_adpt/resource/thread_c_adpt.cc:56-63](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/resource/thread_c_adpt.cc#L56-L63) —— `HcclGetNotifyNumInThread` 先判断 `hcclComm->IsCommunicatorV2()`：V2 走 `collComm->GetCommEngineResMgr()`，否则走独立算子路径的 `GetIndependentOp().GetCommEngineResMgr()`。这正是 u2-l2 讲过的"两条装配线"在线程资源上的体现。

#### 4.3.4 代码实践

1. **实践目标**：用 grep 证据区分"声明骨架"与"实际落地"，学会识别开源仓库中的接口先行代码。
2. **操作步骤**：
   ```bash
   # 1) 查看两个 .cc 的内容差异
   cat src/base_comm/resources/comm_engine_res/comm_engine_res_mgr.cc
   wc -l src/coll_communicator_mgr/resource_mgr/local/my_rank/comm_engine/comm_engine_res_manager.cc
   # 2) 验证 hcomm::CommEngineResMgr::GetEngineRes 没有任何实现/调用
   grep -rn "GetEngineRes" src include pkg_inc
   # 3) 查看 hccl::CommEngineResMgr 被真实调用的位置
   grep -rn "GetCommEngineResMgr" src/coll_communicator_mgr/api_c_adpt/resource/ | head
   ```
3. **需要观察的现象**：第 1 步前者只有空命名空间、后者有完整实现；第 2 步只命中声明处一行；第 3 步在 `thread_c_adpt.cc` 中命中十余处调用。
4. **预期结果**：与 4.3.1 表格的"状态"列一致。结论：现行线程/Notify 管理由控制面 `hccl::CommEngineResMgr` 承担，数据面同名类是目标架构的预留骨架。

#### 4.3.5 小练习与答案

**练习 1**：如果你在代码评审中看到有人写了 `hcomm::CommEngineResMgr mgr; mgr.GetEngineRes(...)`，会出现什么问题？

**答案**：链接失败（undefined reference）。`GetEngineRes` 只有声明，`.cc` 为空命名空间，且构造函数虽是 `= default` 但类在 `hcomm_res_mgr.cc` 之外无任何翻译单元实例化使用——实际结果是无法通过链接。这正是"声明先行"代码的典型陷阱：看头文件以为能用，实际没有实现。

**练习 2**：`EngineCtxMgr::AcquireEngineCtx` 的 `newCreated` 出参解决了什么问题？

**答案**：引擎上下文按 `(engineType, opTag)` 键复用。多个调用方用相同 tag 获取时会拿到同一块内存，`newCreated` 告诉调用方这块内存是刚新建的（需要初始化）还是复用已有的（不可重复初始化），避免多初始化或竞态。这与 `HcclEngineCtxCreate` C 接口"存在即返回"的语义配套。

**练习 3**：为什么线程资源（`hccl::CommEngineResMgr`）挂在通信域（CollComm/IndependentOp）上，而 socket/RDMA 管理器（`HcommResMgr` 触发的那些）挂在进程/设备级？

**答案**：生命周期与共享粒度不同。线程和 Notify 是通信域的私有资源，随通信域销毁而释放，且不同通信域互不影响；socket、RDMA 句柄、EID 信息等是设备/进程级的公共设施，多个通信域共享，提前到 `HcommResMgr` 按 `devPhyId` 统一初始化和清理（设备复位时统一 DeInit）。

## 5. 综合实践

**任务：绘制 base_comm 资源全景图并标注接口入口。** 把本讲三个模块的知识串成一张图：

1. **画分层框架**（纸或任意画图工具），三层：
   - 顶层：对外 C 接口（`include/hcomm_res.h` 的 Endpoint/内存接口、`include/hccl/hccl_res.h` 的线程/引擎上下文接口）。
   - 中层：管理器（`HcommResMgr`、`hccl::CommEngineResMgr`、`EngineCtxs/EngineCtxMgr`、`EndpointMonitor` 等），用虚线框标出 `HcommResMgr` 的特殊位置——它不管理具体业务资源，而是所有设备级单例的 bootstrap 锚点。
   - 底层：资源实现（`endpoints/` 各协议端点、`reged_mems/` 各协议内存、`comm_engine_res/threads/` 三种 Thread、`southbound_adpt/` 四适配器）。
2. **标注创建接口**：每个资源类旁边写上对应的外部创建/使用接口名（参考 4.1.2 的表格，用 grep 验证：`grep -n "extern" include/hcomm_res.h include/hccl/hccl_res.h`）。
3. **标注两条 bootstrap 路径**：`CollCommMgr::InitBaseCommRes → HcommResMgrInit → HcommResMgr::GetInstance`（控制面路径）和各 L3 C 接口入口的 `(void)HcommResMgrInit()`（数据面自举路径）。
4. **标注骨架与实现**：用两种颜色区分"已落地"与"声明先行"（`hcomm::CommEngineResMgr`、`hcomm::CommEngineRes` 属于后者）。
5. **验证**：图中每条边至少用一个 grep 命令佐证（如 `grep -rn "GetCommEngineResMgr" src/coll_communicator_mgr/`）。预期产物是一张你可以对着源码逐条核对的图；后续 u3-l2～u3-l7 每讲深入其中一个资源框。

## 6. 本讲小结

- `src/base_comm/` 是与业务无关的通信地基，分 `common`（公共设施）、`primitives`（原语与 C 适配）、`resources`（七类资源实现）三块，外加 `dfx` 维测。
- `HcommResMgr` 是按设备物理 ID 索引的进程级资源聚合器：首次访问某设备时统一触发约 20 个底层单例（orion 平台层、legacy CCU、开源开放 CCU 三组），并注册设备复位回调在 reset 前清理 socket/RDMA 资源。
- bootstrap 有两条入口：控制面 `CollCommMgr::InitBaseCommRes`（建通信域时）和数据面各 L3 C 接口入口处的 `HcommResMgrInit()` 自举；靠 `isInitialized` 数组保证幂等。
- 仓库里有两个 `CommEngineResMgr`：数据面 `hcomm::` 版本是"声明先行"的骨架（`.cc` 为空、`GetEngineRes` 无实现无调用），控制面 `hccl::` 版本（`ThreadMgr` + `NotifyManager`）才是现行线程/Notify 管理的实现，由 `thread_c_adpt.cc` 按 V2/独立算子两条路径路由。
- `comm_engine_res` 目录下已落地的构件是 `threads/`（Thread 抽象与三种引擎实现）、`engine_ctxs/`（按 engine+opTag 复用的上下文管理）和 `launch/`。
- 每类资源都对应一个对外 C 接口：Endpoint → `HcommEndpointCreate`，注册内存 → `HcommMemReg/HcommMemImport`，线程 → `HcclThreadAcquire` 系列，引擎上下文 → `HcclEngineCtxCreate`。

## 7. 下一步学习建议

- 下一讲 **u3-l2 Endpoint 端点资源**：深入 `resources/endpoints/`，看 `HcommEndpointCreate` 如何根据描述符分派到 RoCE/URMA/HCCS/UB 各协议实现。
- 若对线程资源感兴趣，可提前阅读 `comm_engine_res/threads/thread.h` 及 `aicpu_ts_thread.h`、`aiv_thread.h`、`cpu_ts_thread.h`，为 u3-l5 做准备。
- 建议同时重读 u2-l2 中 `CollComm` 的"资源"类成员列表，你会发现它们与本讲的资源地图一一对应——两讲互相印证后，控制面/数据面的边界会真正清晰。
