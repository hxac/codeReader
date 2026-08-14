# HCCL C 接口与通信域生命周期

## 1. 本讲目标

上一讲（u1-l4）我们从「使用者」视角跑通了 AllReduce 示例，知道怎么调用 `HcclCommInitRootInfo` 建立通信域。本讲切换到「源码读者」视角，学完后你应该能够：

1. 看懂 `include/hccl/hccl_comm.h` 中通信域初始化、查询、销毁三类 C 接口的参数与语义。
2. 解释「弱符号（weak symbol）声明 + 库内强符号实现」这套机制为什么能让 HCCL 算子层和 HCOMM 库解耦。
3. 从 `HcclCommInitRootInfo` 出发，一路跟踪到新架构 `coll_communicator_mgr` 中 `CollComm` 对象的创建与注册，画出完整时序图。
4. 说清一个通信域从「创建 → 查询 → 销毁」的完整生命周期，以及销毁时资源以什么顺序释放。

## 2. 前置知识

本讲需要几个前置概念，用通俗语言解释一下：

- **弱符号（weak symbol）**：C/C++ 链接器的一个特性。用 `__attribute__((weak))` 声明的函数是一个「占位」符号——如果程序链接时找不到别的实现，调用它不会报链接错误（运行时返回地址为空则崩溃）；如果链接了提供强符号实现的库（比如 HCOMM 的动态库），调用就会落到库里的实现。HCCL 的对外头文件全部用弱符号声明接口，这样上层框架（如 PyTorch 昇腾适配层）只需包含头文件就能编译，运行时再通过 `dlsym` 或动态链接拿到 HCOMM 的真实实现。
- **适配层（adapter）**：把一种调用形态翻译成另一种的中间层。本讲会见到两处适配：一是 C 接口到 C++ 内部实现的适配（`api_c_adpt` 目录和 legacy 框架的入口层），二是老流程（V1）到新架构（V2）的桥接。
- **通信域（communicator / comm）**：一组 rank 的「聊天群」。创建通信域就是让群内所有 rank 互相交换地址、探测拓扑、建立链路，最后每人拿到一个 `HcclComm` 句柄（本质是一个 C++ 对象指针）。
- **root info**：建群前由 root rank 生成的「群二维码」，包含 IP、端口、群标识符等。所有 rank 拿到同一份 root info 后各自调用初始化接口加入同一个群（回顾 u1-l4）。
- **单例（singleton）**：进程内只存在一份实例的对象。通信域管理器 `CollCommMgr` 就是单例，进程里所有通信域都登记在它那里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/hccl/hccl_comm.h` | 对外 C 接口声明：通信域初始化/销毁/查询、配置、同步内存等，全部带弱符号标记 |
| `src/coll_communicator_mgr/api_c_adpt/coll_comm_c_adpt.cc` | 新架构控制面的 C→C++ 适配层，目前承载 `HcclCommGetStatus` 等暂未对外的接口 |
| `src/legacy/ascend910/framework/op_base/src/op_base.cc` | C 接口的强符号实现入口层：`HcclGetRootInfo`、`HcclCommInitRootInfo`、`HcclCommDestroy` 等都在这里落地，并桥接到新架构 |
| `src/legacy/ascend910/framework/common/src/host/param_check_basic_v2.h` | `HCCLV2_FUNC_RUN` 宏：运行时探测 V2 能力，决定走新架构还是老流程 |
| `src/legacy/ascend910/framework/communicator/hccl_comm_host.cc` | `hcclComm` 类（owner 对象）：在这里 `new` 出 `CollComm` 并注册进 `CollCommMgr` |
| `src/legacy/ascend910/framework/communicator/hccl_comm.cc` | `hcclComm` 析构：注销 `CollComm`、释放 barrier 内存等 |
| `src/coll_communicator_mgr/communicator/coll_comm_mgr.h/.cc` | 新架构通信域管理器单例：登记/注销所有 `CollComm` |

一个提醒：C 接口的「入口实现」位于 `src/legacy/ascend910/framework/op_base/` 下，但别因此以为它只服务于旧芯片——这一层实际承担着「C 入口 → 新架构」的桥接职责（下文详解）。`src/legacy` 中真正「纯历史」的是 V1 老流程分支。

## 4. 核心概念与源码讲解

### 4.1 hccl_comm.h：C 接口全景与弱符号机制

#### 4.1.1 概念说明

`hccl_comm.h` 是用户与 HCOMM 之间的契约。它把接口分成几类：

- **建群类**：`HcclGetRootInfo`、`HcclCommInitRootInfo(Config)`、`HcclCommInitClusterInfo(Config)`、`HcclCommInitAll`、`HcclCreateSubCommConfig`；
- **查询类**：`HcclGetRankSize`、`HcclGetRankId`、`HcclGetCommName`、`HcclCommGetHandleWithName`、`HcclCommGetStatus`、`HcclGetCommAsyncError`；
- **销毁/管控类**：`HcclCommDestroy`、`HcclCommSuspend`/`HcclCommResume`；
- **配置类**：`HcclSetConfig`/`HcclGetConfig`、`HcclGetCommConfigCapability`、内联函数 `HcclCommConfigInit`（回顾 u1-l4 讲过的 ABI 头部协商）。

所有 `extern` 函数都带 `HCOMM_WEAK_SYMBOL`，即 `__attribute__((weak))`。这就是 HCCL 算子仓库能和 HCOMM 仓库独立编译、运行时动态加载的根基（u1-l1 讲过 dlsym 加载机制）。

#### 4.1.2 核心流程

用户建群的典型时序：

```text
rank 0:  HcclGetRootInfo(&rootInfo)        ── 生成"群二维码"
              │ 外部机制广播（如 MPI_Bcast）
所有 rank: HcclCommInitRootInfo(nRanks, &rootInfo, myRank, &comm)
              │ 交换 rank 信息、探测拓扑、建立链路
              └─> 返回 HcclComm 句柄
所有 rank: HcclGetRankSize(comm)/HcclGetRankId(comm, &id)   ── 查询
所有 rank: HcclAllReduce(...)  ...  使用（其他头文件）
所有 rank: HcclCommDestroy(comm)          ── 销毁
```

#### 4.1.3 源码精读

弱符号的定义与默认值：

[include/hccl/hccl_comm.h:19-21](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L19-L21) 定义了 `HCOMM_WEAK_SYMBOL` 宏，展开为 `__attribute__((weak))`；头文件里每个对外函数声明末尾都挂上它。

几个核心接口的声明：

- [include/hccl/hccl_comm.h:74](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L74)：`HcclGetRootInfo(HcclRootInfo* rootInfo)`，生成建群所需的 root info（内容对用户不透明）。
- [include/hccl/hccl_comm.h:86-87](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L86-L87)：`HcclCommInitRootInfo(nRanks, rootInfo, rank, comm)`，4 个参数分别是群里 rank 总数、群二维码、自己的编号、输出句柄。
- [include/hccl/hccl_comm.h:100-102](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L100-L102)：`HcclCommInitRootInfoConfig`，多一个 `HcclCommConfig*` 用于传入通信域配置（buffer 大小、确定性计算、通信引擎等）。
- [include/hccl/hccl_comm.h:167](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L167)：`HcclCommDestroy(HcclComm comm)`，销毁通信域。
- [include/hccl/hccl_comm.h:141-150](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L141-L150)：`HcclGetRankSize`/`HcclGetRankId`，句柄查询接口。

注意 [include/hccl/hccl_comm.h:202-240](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L202-L240) 的 `HcclCommConfigInit` 是 `static inline` 函数而非弱符号——它直接写在头文件里，由编译器内联进用户代码，负责填充配置结构体的 size/magic/version 头部和各字段的「未设置」哨兵值。

#### 4.1.4 代码实践

1. **实践目标**：数一数弱符号接口，并验证「声明在头文件、实现不在头文件」。
2. **操作步骤**：
   ```bash
   grep -c "HCOMM_WEAK_SYMBOL;" include/hccl/hccl_comm.h
   grep -n "extern HcclResult" include/hccl/hccl_comm.h | head -20
   ```
   再到库实现里找强符号：`grep -n "^HcclResult HcclCommInitRootInfo" src -r`。
3. **需要观察的现象**：头文件中弱符号声明数量；`src` 下能找到 `HcclCommInitRootInfo` 的函数定义（strong symbol）位于 `src/legacy/ascend910/framework/op_base/src/op_base.cc:2111`。
4. **预期结果**：声明处带 `HCOMM_WEAK_SYMBOL`，实现处不带——链接时强符号覆盖弱符号，用户程序调用就会落到库实现上。（待本地验证：若在构建产物 `.so` 上执行 `nm -D | grep HcclCommInitRootInfo`，应看到类型为强符号 `T` 的表项。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hccl_comm.h` 里的函数声明要加 weak，而 `HcclCommConfigInit` 不用加？

**答案**：带 weak 的函数实现在 HCOMM 动态库中，用户程序只包含头文件也能通过链接（运行时动态绑定）；`HcclCommConfigInit` 是 `static inline` 的纯初始化逻辑，不依赖库内任何符号，编译期就内联完成，不存在「链接不到」的问题。

**练习 2**：`HcclCommInitRootInfo` 与 `HcclCommInitRootInfoConfig` 的差别是什么？什么时候必须用后者？

**答案**：后者多一个 `HcclCommConfig*` 参数。需要自定义通信域行为（如指定 commName、buffer 大小、确定性计算、通信引擎 `hcclOpExpansionMode`）时必须用后者；用前者则全部走默认配置。

**练习 3**：如果用户忘了调用 `HcclCommConfigInit` 直接把栈上的 `HcclCommConfig` 传给 Config 接口，可能出什么问题？

**答案**：结构体头部 `size/magicWord/version` 是垃圾值，库侧的 ABI 协商（校验 magic、按 size 判断字段存在性）会失败，返回参数错误。所以必须先 `HcclCommConfigInit` 再改字段（u1-l4 已演示）。

### 4.2 通信域创建调用链：从 C 接口到 CollComm 注册

#### 4.2.1 概念说明

这是本讲的主线。C 接口进来后并不是一步到位创建对象，而是穿过多层：

1. **入口层**（`op_base.cc`）：C 符号落地、参数校验、可选的异步任务封装；
2. **能力分派**（`HCCLV2_FUNC_RUN`）：运行时询问运行时接口 `hrtGetHcclV2Support`，支持则走 V2 新路径并直接返回，否则继续走 V1 老流程；
3. **V2 实现**（如 ascend950 的 `op_base_v2.cc`）：创建底层 `Hccl::HcclCommunicator`（完成 rank 间信息交换、拓扑探测、链路建立）；
4. **桥接层**（`HcclCommInitCollComm`）：从 V2 对象抽取 rankSize/commName/cclBuffer/rankGraph，构造 owner 对象 `hcclComm`；
5. **新架构控制面**（`hcclComm::InitCollComm` → `CollComm` → `CollCommMgr`）：`new` 出 `CollComm` 并登记到管理器单例。

这个分层解释了 u1-l1 的一句话：「控制面 coll_communicator_mgr 管通信域」——`CollCommMgr` 就是所有通信域的户口本。

#### 4.2.2 核心流程

以 `HcclCommInitRootInfo` 为例的调用链（ascend910 桥接路径）：

```text
用户代码
 └─ HcclCommInitRootInfo()                     op_base.cc:2111  （C 入口，参数校验/异步任务封装）
     └─ HcclCommInitRootInfoInner()            op_base.cc:1994  （nRanks/rank 合法性校验）
         ├─ [V2 支持] HCCLV2_FUNC_RUN(lambda)  op_base.cc:2020  （hrtGetHcclV2Support 探测）
         │   ├─ HcclCommInitRootInfoV2()       op_base_v2.cc:1855 （创建 Hccl::HcclCommunicator，commV2）
         │   └─ HcclCommInitCollComm()         op_base.cc:448   （V2 → 新架构桥接）
         │       ├─ 从 commV2 取 rankSize/commName/cclBuffer/rankGraph
         │       ├─ make_shared<hcclComm>(...)                   （owner 对象）
         │       └─ hcclCommPtr->InitCollComm() hccl_comm_host.cc:325
         │           ├─ make_unique<CollComm>(commV2, rank, ...)  hccl_comm_host.cc:361
         │           ├─ collComm_->Init(rankGraph, binHandle_, cclBuffer, ...)  :365
         │           └─ CollCommMgr::RegisteCollComm(collComm_)   :371 ← 通信域登记进户口本
         └─ [V2 不支持] 走 V1 老流程 InitCommRootInfo() ...（legacy 维护态路径）
```

`HcclGetRootInfo` 则是另一条准备链路：创建 `TopoInfoDetect` 服务并 `SetupServer` 监听，把 `HcclRootHandle`（IP、端口、群标识符）拷进不透明的 `rootInfo->internal`，供后续其他 rank 连上来交换信息（细节在 u2-l5 展开）。

#### 4.2.3 源码精读

**C 入口与异步封装**。[src/legacy/ascend910/framework/op_base/src/op_base.cc:2111-2130](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L2111-L2130) 是 `HcclCommInitRootInfo` 的强符号实现：若处于 group 模式（`hcclGroupDepth > 0`，即用户调过 `HcclGroupStart`），把参数打包成 `hcclCommInitAsyncJob` 追加到初始化任务队列延迟执行；否则直接同步调 `HcclCommInitRootInfoInner`。这就是「同一接口既支持同步建群、也支持组模式下批量延迟建群」的实现方式。

**运行时能力分派**。[src/legacy/ascend910/framework/common/src/host/param_check_basic_v2.h:18-25](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/common/src/host/param_check_basic_v2.h#L18-L25) 的 `HCCLV2_FUNC_RUN` 宏先调 `hrtGetHcclV2Support(&isSupportV2)` 探测当前运行时/硬件是否支持 V2 架构，支持则执行传入的 lambda 并**直接 return**。于是 [op_base.cc:2020-2034](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L2020-L2034) 里同一段函数体出现了两条互斥路径：V2 分支（新架构）命中即返回；未命中则继续向下走 V1 的 `InitCommRootInfo` 老流程。这样新老芯片可以共用同一个 C 入口。

**V2 实现里通信域对象的诞生**（ascend950 路径）。[src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc:1855-1897](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc#L1855-L1897) 的 `HcclCommInitRootInfoV2`：把 `rootInfo->internal` 还原为 `HcclRootHandleV2`，取出群标识符 identifier，然后交给 `CommInitRootInfo` 完成实际建群；其内部（[op_base_v2.cc:1803-1835](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc#L1803-L1835)）`new Hccl::HcclCommunicator(commParams)` 并调用其 `Init(rankTable)`，成功后 `*comm = static_cast<HcclComm>(pComm.get())`——**句柄就是 C++ 对象指针的 C 化转型**。

**桥接层**。[op_base.cc:448-500](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L448-L500) 的 `HcclCommInitCollComm` 是理解「新旧架构并存」的关键函数：它从 V2 对象 `commV2` 上取回 rankNum、commName、CCL buffer 地址和 rankGraph，构造 `hccl::hcclComm`（[472-473 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L472-L473)），再调 `hcclCommPtr->InitCollComm(...)`（[482 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L482)），最后把它登记进 `opBaseHcom.opGroup2CommMap`（按 commName 索引，供后续按名查找与销毁）。

**新架构对象创建**。[src/legacy/ascend910/framework/communicator/hccl_comm_host.cc:325-397](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm_host.cc#L325-L397) 的 `hcclComm::InitCollComm`：

- [361 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm_host.cc#L361)：`collComm_ = std::make_unique<CollComm>(commV2, userRank, commName, callbacks, initMode)` —— **新架构控制面的通信域对象在这里诞生**；
- [365 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm_host.cc#L365)：`collComm_->Init(rankGraph, binHandle_, cclBuffer, configOpExpansionMode)` 完成初始化（CollComm 内部细节是 u2-l2 的主题）；
- [371 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm_host.cc#L371)：`CollCommMgr::GetInstance().RegisteCollComm(collComm_.get())`，注释明确说明「由 owner(hcclComm) 负责注册/注销，避免 CollComm 反向依赖 CollCommMgr」——依赖方向依然自上而下（u1-l1 的架构铁律在这里也有体现）。

**户口本本身**。[src/coll_communicator_mgr/communicator/coll_comm_mgr.h:29-61](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.h#L29-L61) 定义 `CollCommMgr`：核心容器是 `std::unordered_map<std::string, CollComm*> allCollComms_`（按 commId 索引），还按设备维度持有 `ClusterMonitor`、`OrderLaunchThreadMgr` 等。注册逻辑在 [coll_comm_mgr.cc:86-93](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L86-L93)：加锁写入 map，同时把通信域注册到 taskAbortHandler 和保序下发线程管理器——也就是说，「登记」不只是记录，还接入了维测与调度设施。单例构造见 [coll_comm_mgr.cc:17-21](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L17-L21)（Meyers 单例，C++11 保证线程安全）。

**root info 的生成侧**。[op_base.cc:1370-1424](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L1370-L1424) 的 `HcclGetRootInfo`：同样先走 `HcclGetRootInfoV2`；随后创建 `TopoInfoDetect` 服务、`SetupServer(rootHandle)` 启动监听，把 `rootHandle`（含 ip/port/identifier）memcpy 进 `rootInfo->internal`（[1398-1412 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L1398-L1412) 还做了长度防溢出校验），最后把探测服务登记到 `CollCommMgr::GetInstance().LegacyGetHcclOpInfoCtx(...)` 的 map 里，等建群时复用。

#### 4.2.4 代码实践（本讲主实践：跟踪调用链画时序图）

1. **实践目标**：以 `HcclCommInitRootInfo` 为起点，用文本工具走完「C 入口 → V2 分派 → 桥接 → CollComm 注册」全链路，产出一张标注文件与行号的时序图。
2. **操作步骤**：
   ```bash
   # ① 找 C 入口强符号
   grep -n "HcclResult HcclCommInitRootInfo(" src -r
   # ② 打开 op_base.cc:2111，向下追 HcclCommInitRootInfoInner（同文件 1994 行）
   # ③ 观察 2020 行的 HCCLV2_FUNC_RUN，跳到 param_check_basic_v2.h 看 hrtGetHcclV2Support 语义
   # ④ 追 lambda 内的 HcclCommInitRootInfoV2（ascend950 路径，op_base_v2.cc:1855）
   #    和 HcclCommInitCollComm（op_base.cc:448）
   # ⑤ 追 hccl_comm_host.cc:325 的 InitCollComm，定位 make_unique<CollComm> 与 RegisteCollComm
   # ⑥ 最后读 coll_comm_mgr.cc:86 的 RegisteCollComm，看注册时还做了什么
   ```
   每跳一层，在笔记里记下「函数名 @ 文件:行号 → 做了一件事」。
3. **需要观察的现象**：链路共 5~6 跳；`HCCLV2_FUNC_RUN` 是唯一分叉点；`*comm` 句柄最终被赋值两次（V2 内部一次、桥接层一次），两次含义不同。
4. **预期结果**：得到类似 4.2.2 节的时序图，但由你亲手从源码验证每一跳。完成后回答：为什么 `CollComm` 不自己注册进 `CollCommMgr`？（提示：看 hccl_comm_host.cc:370 的注释与依赖方向。）
5. 本实践为纯源码阅读型，无需硬件，**可直接完成**。

#### 4.2.5 小练习与答案

**练习 1**：`hrtGetHcclV2Support` 返回 false 时，`HcclCommInitRootInfoInner` 会走哪条路径？这条路径的维护策略是什么？

**答案**：继续执行 V1 老流程（`InitCommRootInfo` 等 legacy 代码）。维护策略是只做 bug 修复与兼容维护，不承接新特性、不再演进（与 `CollCommMgr` 中 `Legacy` 前缀接口的注释一致，[coll_comm_mgr.h:43-46](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.h#L43-L46)）。

**练习 2**：`HcclComm` 句柄到底是什么？

**答案**：一个 C++ 对象指针经 `static_cast` 得到的 C 指针。ascend950 路径中它是 `Hccl::HcclCommunicator*`（op_base_v2.cc:1835）；ascend910 桥接路径中最终交给用户的句柄是 `hccl::hcclComm*`（op_base.cc:496，`*comm = static_cast<HcclComm>(hcclCommPtr.get())`）。所以句柄不能跨设备/跨进程共享，也不可解析内容。

**练习 3**：`RegisteCollComm` 除了写 map 还做了什么？为什么？

**答案**：还把通信域注册到 `taskAbortHandler_`（任务中止处理）和对应设备的 `OrderLaunchThreadMgr`（保序下发线程管理器，[coll_comm_mgr.cc:91-92](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L91-L92)）。因为通信域一旦存在，就必须能被维测（异常中止）和调度（保序下发）体系感知，注册即接入。

### 4.3 查询、销毁与 api_c_adpt 适配层模式

#### 4.3.1 概念说明

通信域创建后有一组「查询接口」让用户自查身份（rankSize、rankId、commName、状态），销毁时则要保证：从管理器注销、通知维测设施、释放 V2 对象与内存、从按名索引的 map 中移除。这一讲最后我们把目光投向 `src/coll_communicator_mgr/api_c_adpt/` —— 新架构自己的 C 适配层。目前它只承载少量暂未对外的接口（如 `HcclCommGetStatus`），但它展示了一个「新架构直连 C 接口」的模式：**不再绕道 legacy 入口层，C 函数直接落在 `api_c_adpt`，做指针校验后调 C++ 方法**。可以预期未来新接口会更多走这条路（该目录下已有 `coll_comm_res_c_adpt.cc`、`exchange_info_c_adpt.cc`、`hccl_channel_config.cc` 等多个适配文件）。

#### 4.3.2 核心流程

销毁一个通信域的简化流程：

```text
HcclCommDestroy(comm)
 ├─ 从 opGroup2CommMap 按名查找 hcclComm（找不到 → 报错返回）
 ├─ 检查通信域状态（如 in use → 警告并稍后重试语义）
 ├─ HcclCommDestroyV2(commV2)            销毁底层 V2 通信域对象
 ├─ 从 map 中 erase，触发 hcclComm 引用计数归零
 │    └─ ~hcclComm()
 │        ├─ CollCommMgr::UnRegisteCollComm(collComm_)   从户口本除名 + 摘除维测/调度挂钩
 │        ├─ 释放 barrier 内存、卸载二进制
 │        └─ unique_ptr<CollComm> 成员析构 → ~CollComm
 └─ 返回
```

查询接口则轻量得多：句柄 → 对象指针 → 取成员/调方法返回。

#### 4.3.3 源码精读

**新架构适配层的样板代码**。[src/coll_communicator_mgr/api_c_adpt/coll_comm_c_adpt.cc:18-27](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_c_adpt.cc#L18-L27) 是 `HcclCommGetStatus` 的全部实现，仅 10 行：空指针校验 → `HcomGetCommHandleByGroup(commId, &comm)` 按名取句柄（实现在 [src/legacy/ascend910/framework/hcom/hcom_common.cc:218](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/hcom/hcom_common.cc#L218)）→ `hcclComm->GetCommStatus(*status)`。头文件 [coll_comm_c_adpt.h:20-21](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_c_adpt.h#L20-L21) 的注释写明其职责：「集合通信的通信域管理的 C 接口的 C 到 C++ 适配（暂未对外的接口）」。对比 4.2 节的五层长链，这里是「C → C++」一步到位——两代适配风格并存正是仓库演进中的真实状态。

**销毁入口**。[op_base.cc:3624](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L3624) 起的 `HcclCommDestroy`：group 模式下同样走异步任务封装（`commInitTaskAppend` + `HcclCommDestroyWrapper`）；随后调 `HcclCommDestroyV2`，并按 commName 从 `opGroup2CommMap` 移除该通信域；若通信域正被使用（如还有未完成的集合通信任务），仅打 WARNING 提示稍后重试（[3563、3675 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L3563-L3563)）。

**析构中的注销顺序**。[src/legacy/ascend910/framework/communicator/hccl_comm.cc:53-66](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm.cc#L53-L66) 的 `~hcclComm()`：先判断 `collComm_` 为 fullMode 时调 `CollCommMgr::UnRegisteCollComm`（[58-59 行](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm.cc#L58-L59)），注释解释了为什么注销放在析构函数体而不是 `~CollComm`——避免 `CollComm` 反向依赖 `CollCommMgr`；随后释放 barrier 内存、注销任务中止处理、卸载二进制。对应的 [coll_comm_mgr.cc:95-103](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm_mgr.cc#L95-L103) `UnRegisteCollComm` 与注册严格对称：erase map、摘除 taskAbortHandler、从 ClusterMonitor 与保序线程管理器注销。

**查询接口示例**。[include/hccl/hccl_comm.h:141-150](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L141-L150) 声明的 `HcclGetRankSize`/`HcclGetRankId` 在 ascend950 侧的实现是 [op_base_v2.cc:1051-1103](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc#L1051-L1103)：按句柄取出 `HcclCommunicator`，读其 `GetId()`/rankSize 返回——典型的「句柄 → 对象 → 成员」查询模式。

#### 4.3.4 代码实践

1. **实践目标**：验证「注册/注销对称性」，即建群时挂上的每个钩子在销毁时都会被摘下。
2. **操作步骤**：
   ```bash
   # 读注册
   sed -n '86,103p' src/coll_communicator_mgr/communicator/coll_comm_mgr.cc
   # 读注销触发点
   grep -n "UnRegisteCollComm" src -r
   # 读销毁入口里对 opGroup2CommMap 的 erase
   grep -n "opGroup2CommMap.erase\|opGroup2CommMap" src/legacy/ascend910/framework/op_base/src/op_base.cc | head
   ```
   然后画一张两列对照表：左列「创建时做了什么」，右列「销毁时逆序撤销了什么」。
3. **需要观察的现象**：`RegisteCollComm` 的三个动作（map 写入、taskAbortHandler、OrderLaunchThreadMgr）在 `UnRegisteCollComm` 中逐一对应；此外 `~hcclComm` 里还有 ClusterMonitor 的注销。
4. **预期结果**：得到一张「创建/销毁对称表」，并理解为什么析构顺序必须与构造顺序相反（注销依赖注册进 map 的有效指针，因此必须先于成员析构执行）。
5. 本实践为源码阅读型，**可直接完成**；若手头有昇腾环境，可在 u1-l4 示例返回前用 `HcclGetCommName` 打印 commName，再销毁并观察日志中的 `HcclCommDestroy` 记录（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`HcclCommGetStatus` 为什么放在 `api_c_adpt` 而不是像其他接口那样在 `op_base.cc` 实现？

**答案**：它是新架构控制面直接提供的 C 接口（暂未对外），采用新的适配模式——C 函数直接落在 `api_c_adpt` 适配层，校验指针后直调 C++ 方法，不再经过 legacy 入口层的 V1/V2 分派长链。旧接口因历史兼容留在 op_base 入口层。

**练习 2**：如果销毁时通信域「is in use」，库的行为是什么？这提示用户应养成什么习惯？

**答案**：只打 WARNING（"comm is in use, please try again later"）提示稍后重试，不强行销毁。提示用户销毁前应先 `aclrtSynchronizeStream` 等待所有流上的通信任务完成，再调 `HcclCommDestroy`（与 u1-l4 强调的释放顺序一致）。

**练习 3**：`CollCommMgr::GetInstance()` 使用的是什么单例写法？多线程同时建群时 `RegisteCollComm` 安全吗？

**答案**：Meyers 单例（函数内 `static CollCommMgr instance;`，C++11 起初始化线程安全，见 coll_comm_mgr.cc:17-21）。`RegisteCollComm`/`UnRegisteCollComm` 内部用 `std::lock_guard<std::mutex> lock(mutex_)` 保护 map 写入，因此多线程并发建群是安全的。

## 5. 综合实践

**任务：为一次真实建群写一份「生命周期档案」。** 结合 u1-l4 的示例程序与本讲的源码，完成：

1. 在示例（`examples/01_communicators/01_one_device_per_process/main.cc`）的 `HcclGetRootInfo`、`HcclCommInitRootInfoConfig`、`HcclGetRankSize`、`HcclCommDestroy` 四个调用点各加一行 `printf` 标记（这是你自己的测试代码，不修改仓库源码，复制出来改即可）。
2. 运行示例（无硬件则跳过运行，做静态部分），把每条日志与源码链路对应：`HcclGetRootInfo` 对应 op_base.cc:1370 的 `TopoInfoDetect::SetupServer`；`HcclCommInitRootInfoConfig` 对应 4.2.2 节时序图的五跳；`HcclCommDestroy` 对应 4.3.2 节的销毁流程。
3. 产出一张表格，列为：阶段（创建/查询/使用/销毁）× 用户侧调用 × 库内关键函数 @ 文件:行号 × 管理器侧动作（Registe/UnRegiste）。
4. 思考题（写在档案末尾）：如果把 `CollCommMgr` 的 `allCollComms_` 从 `unordered_map` 改成 `vector`，哪些接口会受影响？（提示：按 commId 查找、`GetAllCollComms` 的消费方式。）

## 6. 本讲小结

- `hccl_comm.h` 的所有对外函数带 `HCOMM_WEAK_SYMBOL` 弱符号，这是 HCCL 算子层与 HCOMM 库解耦、动态加载的根基；`HcclCommConfigInit` 是内联函数，不走这套机制。
- C 接口强实现位于 legacy 框架的 `op_base.cc` 入口层；`HCCLV2_FUNC_RUN` 通过运行时 `hrtGetHcclV2Support` 探测，把调用分派到 V2 新架构路径或 V1 老流程，一套入口服务新老芯片。
- `HcclCommInitRootInfo` 主链路：入口层 → V2 实现（创建 `HcclCommunicator`）→ `HcclCommInitCollComm` 桥接 → `hcclComm::InitCollComm` 中 `make_unique<CollComm>` → `CollCommMgr::RegisteCollComm` 登记进单例户口本。
- `HcclComm` 句柄本质是 C++ 对象指针的 C 化转型，不能跨设备/进程共享，也不可解析。
- 查询接口是「句柄 → 对象 → 成员」的轻量读取；销毁与创建严格对称：map erase、维测设施注销、内存与二进制释放，且注销由 owner（`hcclComm`）在析构中驱动以保持依赖方向自上而下。
- `api_c_adpt` 目录展示了新架构「C → C++ 一步适配」的新模式（如 `HcclCommGetStatus`），与 legacy 入口层的长链适配并存，是仓库演进的真实切片。

## 7. 下一步学习建议

- 下一讲（u2-l2）深入 `CollComm` 对象本身：它的成员如何按「状态/资源/调度」分类、`Init` 与销毁的内部细节，以及独立算子（independent_op）的组织方式——本讲停在 `collComm_->Init(...)` 门口，下一讲推门进去。
- 想先弄清 root info 背后的网络交互（bootstrap 监听、socket 交换、白名单），可跳读 u2-l5 的 `rank_info_detect` 模块，其入口正是本讲出现的 `TopoInfoDetect::SetupServer`。
- 建议同步阅读 `src/coll_communicator_mgr/communicator/coll_comm.h` 与 `coll_comm_mgr.cc` 全文，它们篇幅不长，是控制面的枢纽代码。
