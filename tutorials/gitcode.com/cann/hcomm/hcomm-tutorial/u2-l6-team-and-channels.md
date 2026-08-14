# Team 机制：HcclWorldTeamCreate 与通道批量创建

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分 `HcclComm`（通信域）与 `HcommTeamHandle`（team）两级抽象，说清楚为什么要在通信域之上再引入 team。
2. 掌握 `HcclWorldTeamCreate` / `HcclSubTeamCreate` / `HcclTeamDestroy` 三个生命周期接口的参数含义与内部流程。
3. 理解窗口注册 `HcclTeamWindowRegister` 与通道批量创建 `HcclTeamChannelsCreate` 的配合关系，特别是「窗口必须经 ChannelsCreate 才生效」这条约束。
4. 顺着源码走通一条完整调用链：L2 对外 C 接口 → `hccl` 层适配与登记 → L3 `HcommTeamMgr` 落到 device 侧 `HcommTeam` 实体。

## 2. 前置知识

**team（团队）**：本讲中 team 是「一组 rank + 一份同步内存 + 一批点对点 channel」的组合体。通信域（`HcclComm`，见 u2-l1/u2-l2）解决的是「这些进程如何互相认识、拓扑长什么样」；team 解决的是「在这个通信域内，我要挑出一组成员（可以是全部，也可以是子集），并为它们配好点对点数据通路和同步内存，供通信算子直接使用」。

**world team 与 sub team**：world team 是在通信域上创建的「根 team」，其成员可以是通信域全部 rank，也可以是子集（源码注释明确「worldTeam 可为子集」，见 [hccl_team_c_adpt.cc:130](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L130)）。sub team 的成员必须落在某个 world team 成员集合内，形成父子关系。

**rankId 与 memberId**：rankId 是 rank 在通信域里的全局编号；memberId 是 rank 在某个 team 内部的下标（0 起）。同一个 rank 在 world team 和 sub team 里的 memberId 通常不同。本讲最重要的机制之一就是 L2 层维护的「memberId→rankId」翻译。

**窗口（window）**：一段被 team 各成员注册并互相交换过的 device 内存的抽象。本端把本地内存注册进 window，建链时通过 channel 把描述符交换给对端，之后远端就能直接对这段内存做单边读写（对应 u3-l7 将讲到的 Write/Read 原语）。

**syncMem（同步内存）**：team 粒度的一块 device 内存，大小为：

\[
\text{syncMemSize} = (\text{signalCount} + \text{counterCount} + \text{barrierCount}) \times 8\,\text{字节} \times \text{memberNum}
\]

各成员把这块内存交换给彼此后，可用于 signal/counter/barrier 等同步原语。当前版本 `signalCount` 和 `counterCount` 强制为 0，只支持 barrier。

**ABI 描述符**：与 u1-l4 讲过的 `HcclCommConfig`、u3-l4 将讲的 `HcommChannelDesc` 一样，team 相关描述符都遵循「头部 `CommAbiHeader`（size/magicWord/version）+ 尾部 reserved」的兼容设计，必须用对应的 `Init` 内联函数初始化，未设置字段保持 0xFF 哨兵值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/hccl/hccl_team.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h) | 对外 C 接口：team 创建/销毁、窗口注册/注销、通道批量创建，以及两个描述符的定义与 Init |
| [include/hcomm_team_defs.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_team_defs.h) | `HcommTeamHandle` / `HcommWindowHandle` 不透明句柄与 `HcommTeamSyncMemRequirement` |
| [src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc) | L2 适配层：把对外 C 接口翻译成对 L3 `Hcomm*` 接口的调用，并做参数校验、rankId→memberId 翻译、syncMem 分配 |
| [src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h) | L2 进程级 team 登记表：父子关系、syncMem 记账、window 列表、memberId→rankId 映射 |
| [src/coll_communicator_mgr/team/hcomm/hcomm_team.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team.h) | L3 内部 C 接口声明（暂未对外）：`HcommTeamCreate/Destroy`、BindChannels、BindRemoteSyncMem 等，及 L3 版描述符 |
| [src/coll_communicator_mgr/team/hcomm/hcomm_team_c_adpt.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_c_adpt.cc) | L3 C 接口薄适配：参数检查后直通 `HcommTeamMgr` 单例 |
| [src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.h) / [.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc) | L3 管理器：维护 TeamEntry/WindowEntry，把 host 侧实体同步到 device 侧 |
| [pkg_inc/hcomm/hcomm_team_entity_defs.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/pkg_inc/hcomm/hcomm_team_entity_defs.h) | host/device 共享的 ABI 实体：`HcommTeam`、`HcommWindow`、`HcommTeamSyncMem`（通信算子在 device 侧直接消费） |

注意目录命名：`team/hccl/` 是 L2（对外接口的适配与登记），`team/hcomm/` 是 L3（内部数据面资源）。这个分层与 u1-l3 讲的「控制面→数据面」依赖方向一致。

## 4. 核心概念与源码讲解

### 4.1 两级抽象与对外接口总览

#### 4.1.1 概念说明

为什么有了 `HcclComm` 还要 team？通信域是「重」资源：建通信域要走完整的 bootstrap（u2-l5 的 rank_info_detect）、拓扑构建（u2-l4 的 RankGraph）和资源装配（u2-l2 的 CollComm 十二步）。而通信算子经常需要更细粒度的组合：只对一部分 rank 通信、换一种网络层（netLayer）或协议重试、为特定内存段建立直通通路。team 是「轻」资源：它挂在已有通信域上，复用其拓扑与内存交换设施，只新增成员表、同步内存和 channel。

两级抽象的分工：

- `HcclComm`：owner 与拓扑归宿，team 创建时校验成员数不超通信域、销毁通信域时兜底清理 team（`HcclTeamMgr::ClearByCollComm`）。
- `HcommTeamHandle`：不透明句柄，本质是 device 侧一块 `HcommTeam` 结构体内存的地址（见 4.2），通信算子拿它在 device 上定位成员表、channel 数组和同步内存。

#### 4.1.2 核心流程

对外接口族共六个，按使用顺序：

```text
HcclCommInitRootInfo...          ← 建通信域（u1-l4/u2-l1，已学）
        │
HcclWorldTeamCreate              ← ① 在通信域上建 world team
        │
HcclSubTeamCreate (可多次)        ← ② 从 world team 派生 sub team
        │
HcclTeamWindowRegister (可多次)   ← ③ 注册业务内存窗口（仅 worldTeam）
        │
HcclTeamChannelsCreate           ← ④ 批量建链：channel + 内存交换 + 绑定
        │
HcclTeamWindowDeregister / HcclTeamDestroy   ← ⑤⑥ 释放
```

#### 4.1.3 源码精读

句柄与同步内存需求定义在 [include/hcomm_team_defs.h:L22-L34](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_team_defs.h#L22-L34)：`HcommTeamHandle`/`HcommWindowHandle` 都是 `uint64_t*`（指向 device 内存地址），`HcommTeamSyncMemRequirement` 用三个计数加 reserved 表达同步内存需求。

对外描述符 `HcclTeamCreateDesc` 在 [include/hccl/hccl_team.h:L23-L33](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h#L23-L33)：

- `rankIds`/`rankNum`/`selfRankId`：用户以 **rankId 域**描述成员与本 rank；
- `netLayer`：希望使用的网络层，0 表示默认（对应 u2-l4 的 netLayer 分层）；
- `protocol`：`-1` 表示保留（由库根据拓扑决定），一个 team 只支持一个协议；
- `requirement`：同步内存需求（当前 signalCount/counterCount 必须为 0，barrierCount ≥ 1）。

`HcclTeamCreateChannelsDesc` 在 [include/hccl/hccl_team.h:L35-L43](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h#L35-L43)：`engine`（AICPU_TS/CCU 等，对应 u1-l1 讲的四种通信引擎）、`notifyNum`（每个 channel 的 notify 数量）、`protocol`、`channelCnt`（每对成员建几个 channel）。

两个描述符的魔数/版本常量在 [include/hccl/hccl_team.h:L45-L49](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h#L45-L49)（`0x0fcf0f14` / `0x0fcf0f15`），`HcclTeamCreateDescInit` 在 [include/hccl/hccl_team.h:L51-L75](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h#L51-L75)：先整体 memset 成 0xFF 哨兵值，再填 ABI 头部与各字段默认值。

六个对外函数的声明集中在 [include/hccl/hccl_team.h:L107-L163](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h#L107-L163)。其中窗口注册上方有两行关键中文注释（[L140-L141](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h#L140-L141)）：所有 team 上的 rank 都必须调用；且 window 必须在调用 `HcclTeamChannelsCreate` 之后才能生效——只注册不建链，window 不可用。

#### 4.1.4 代码实践

1. **实践目标**：建立接口全景，明确每个接口的输入域（rankId 还是 memberId）。
2. **操作步骤**：打开 [include/hccl/hccl_team.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_team.h)，为六个接口各写一行「入参 → 出参 → 调用前提」的表格记录。
3. **需要观察的现象**：`HcclWorldTeamCreate` 的第一个参数是 `HcclComm`，而 `HcclSubTeamCreate` 的第一个参数是 `HcommTeamHandle`——参数类型本身就揭示了层级关系。
4. **预期结果**：得到一张 6 行速查表；特别注意 `HcclTeamWindowRegister` 只接受 worldTeam（头文件注释与实现双重约束）。

#### 4.1.5 小练习与答案

**练习 1**：`HcclTeamCreateDesc` 里用户填的是 rankId 还是 memberId？谁负责翻译？
**答案**：用户填 rankId（`rankIds`/`selfRankId`）。L2 适配层的 `FindSelfMemberId` 与 `BuildSubTeamWorldMemberIds`（见 4.2）负责把它翻译成 memberId 域再下沉 L3；L2 的 `HcclTeamMgr::TeamEntry::rankIds` 显式注释「memberId→rankId 映射，下标=memberId，值=rankId。L2 维护，rankId 不下沉 L3」（[hccl_team_mgr.h:L52-L53](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h#L52-L53)）。

**练习 2**：为什么 `HcommTeamHandle` 定义为 `uint64_t*` 而不是结构体指针？
**答案**：它是不透明句柄，指向 device 侧一块按 `HcommTeam` ABI 布局的内存。对外头文件不暴露 `HcommTeam` 定义（定义在 pkg_inc，属包间共享），用整型指针避免调用方误解引用，也便于 device 侧按地址直接访问。

### 4.2 World/Sub Team 创建：从 rankId 域到 device 实体

#### 4.2.1 概念说明

创建分两层走：L2 的 `HcclWorldTeamCreate`/`HcclSubTeamCreate` 负责「翻译与记账」（校验、rankId→memberId、分配 syncMem、登记父子关系），L3 的 `HcommTeamMgr::TeamCreate` 负责「落 device」（构造 host 侧 `HcommTeam` 实体，申请 device 内存，H2D 拷贝）。返回给用户的句柄就是 device 侧 `HcommTeam` 的地址。

#### 4.2.2 核心流程

以 `HcclWorldTeamCreate` 为例（sub team 多一步反查）：

```text
HcclWorldTeamCreate(comm, desc, &worldTeam)
  ├─ 0. 参数校验：rankNum≥2、rankIds 非空、signal/counter==0、barrier≥1
  │     且 rankNum ≤ comm 的 rankSize（worldTeam 可为通信域子集）
  ├─ 1. FindSelfMemberId：在 rankIds 中找 selfRankId 的下标 → memberId
  ├─ 2. FillHcommTeamCreateDesc：构造 L3 描述符（worldTeam 的 worldMemberIds=nullptr）
  ├─ 3. HcommTeamCreate(nullptr, &hcommDesc, worldTeam, &syncMemSize)
  │     └─ HcommTeamMgr::TeamCreate → InitTeamEntry + AllocAndSyncTeam
  │        ├─ AllocAndCopyWorldTeamIds：worldTeam 生成 [0,memberNum) 序列并拷到 device
  │        ├─ hrtMalloc(sizeof(HcommTeam)) → devTeam（这就是返回的句柄）
  │        └─ SyncTeamToDevice：H2D 拷贝实体
  ├─ 4. AllocTeamSyncMem：hrtMalloc(syncMemSize)，失败则回滚销毁 team
  └─ 5. HcclTeamMgr::RegisterWorldTeam：登记 collComm/syncMem/rankIds
```

sub team 的差异在第 1、2 步之间多出 `BuildSubTeamWorldMemberIds`：把 subTeam 的每个 rankId 反查到 worldTeam 的 memberId，得到 `worldMemberIds` 数组——这正是 L3 校验与 device 侧寻址的依据。

#### 4.2.3 源码精读

**L2 适配层**（[src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc)）：

- [L38-L48](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L38-L48) `FindSelfMemberId`：在 `rankIds` 里找 `selfRankId` 的下标作为 memberId，找不到返回 `HCCL_E_NOT_FOUND`。注释给了例子：rankIds=[1,3,5,7]、selfRankId=3，则 memberId=1。
- [L52-L66](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L52-L66) `FillHcommTeamCreateDesc`：先用 L3 头文件的 `HcommTeamCreateDescInit` 初始化 ABI 头部，再覆盖业务字段；`signalCount/counterCount` 强制清 0（不支持配置）。
- [L70-L91](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L70-L91) `BuildSubTeamWorldMemberIds`：用 `unordered_map` 预建 rankId→worldMemberId 反查表，O(N+M) 避免双重循环；subTeam 的 rankId 不在 worldTeam 中即报 `HCCL_E_PARA`。
- [L107-L177](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L107-L177) `HcclWorldTeamCreate` 主体：第 0 步校验 `rankNum > commRankSize` 拒绝（[L130-L136](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L130-L136)）；第 3 步调用 L3；第 4 步 [L94-L105](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L94-L105) `AllocTeamSyncMem` 失败时回滚 `HcommTeamDestroy`；第 5 步 `RegisterWorldTeam` 失败同样逐级回滚（free syncMem → destroy team）。每一步失败路径都收尾干净，这是读这段代码时最值得学习的工程习惯。
- [L179-L240](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L179-L240) `HcclSubTeamCreate`：结构与 world 版几乎一致，仅第 1 步换成反查 worldMemberIds、第 3 步把 worldTeam 句柄传入 L3、第 5 步 `RegisterSubTeam` 建父子关系。

**L2 登记表**（[src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h)）：

- [L41-L54](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h#L41-L54) `TeamEntry`：`collComm`（反查通信域）、`worldTeam`（父句柄）、`syncMemPtr/syncMemSize/syncMemHandle/syncMemTag`（同步内存记账）、`windows`（仅 world team 条目填充的 window 列表）、`rankIds`（memberId→rankId 映射）。类注释（[L25-L31](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h#L25-L31)）总结了三件职责：父子关系、syncMem、worldTeam↔window 的 1:N。
- [L104-L107](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h#L104-L107) `ClearByCollComm`：通信域析构的兜底清理入口，保证 team 生命周期不超过其 owner 通信域（与 u2-l2 讲的「注销由 owner 驱动」一脉相承）。

**L3 落 device**（[src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc)）：

- [L382-L404](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L382-L404) `ValidateSubTeam`：memberNum 不得超 world team，每个 worldMemberId 必须小于 world 的 memberNum。
- [L406-L428](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L406-L428) `InitTeamEntry`：填 `HcommTeam` 的 ABI 头部、memberNum、selfMemberId、netLayer，并按前述公式计算 `syncMemSize`。
- [L101-L151](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L101-L151) `AllocAndCopyWorldTeamIds`：worldTeam（src==nullptr）时生成 `[0, memberNum)` 连续序列；host/device 各存一份。
- [L430-L455](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L430-L455) `AllocAndSyncTeam`：hrtMalloc 一块 `sizeof(HcommTeam)` 的 device 内存作为 `devTeam`，再 `SyncTeamToDevice` 做 H2D 拷贝——**返回给用户的句柄就是这块 device 内存的地址**。
- [L457-L496](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L457-L496) `TeamCreate` 主体：查 worldEntry（读锁）→ 校验 → 构建 TeamEntry → 分配并同步 → 写入 `teams_` 表（写锁），返回 `*team = entry->devTeam` 与 `*outSyncMemSize`。

**device 侧实体**（[pkg_inc/hcomm/hcomm_team_entity_defs.h:L41-L54](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/pkg_inc/hcomm/hcomm_team_entity_defs.h#L41-L54)）：`HcommTeam` 含 `engine`、`memberNum/selfMemberId`、`channelsBaseAddr`（连续 ChannelEntity 数组基地址）、`channelNumPerMember`（device 指针）、`netLayer`、`worldTeamIds`（device 指针）、内嵌 `HcommTeamSyncMem`。注释写明「hcommTeamRes 存放 worldTeam 的资源，使用 subTeam 来寻找 hcommTeamRes」——即 device 侧通过 worldTeamIds 把 sub team 成员映射回 world team 资源。这个结构是 host/device 共享 ABI，与 u1-l3 讲的 `ChannelEntity` 同属一类契约。

L3 入口 [src/coll_communicator_mgr/team/hcomm/hcomm_team_c_adpt.cc:L20-L45](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_c_adpt.cc#L20-L45) 的 `HcommTeamCreate` 只做纯参数检查（含「sub team 必须带 worldMemberIds」）后直通单例，是典型的薄适配层。

#### 4.2.4 代码实践

1. **实践目标**：走通「rankId → memberId → device 句柄」的翻译链。
2. **操作步骤**：从 [hccl_team_c_adpt.cc:L107](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L107) 的 `HcclWorldTeamCreate` 入手，沿调用链依次打开 `FindSelfMemberId` → `HcommTeamCreate` → `HcommTeamMgr::TeamCreate` → `AllocAndSyncTeam`，为每层记下「所在文件:行号、输入域、输出域、分配的资源」。
3. **需要观察的现象**：哪一层开始不再出现 rankId（只剩 memberId）？syncMemSize 在哪一行算出、又在哪一层申请？
4. **预期结果**：得到一条五层调用链清单。rankId 在 L2 适配层就被翻译完毕；syncMemSize 在 `InitTeamEntry`（[hcomm_team_mgr.cc:L417-L420](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L417-L420)）计算、在 L2 的 `AllocTeamSyncMem` 申请。

#### 4.2.5 小练习与答案

**练习 1**：rankIds=[2,0,3]、selfRankId=0，memberId 是多少？sub team 的 worldMemberIds 应该怎么算？
**答案**：memberId=1（selfRankId=0 在 rankIds 中下标为 1）。worldMemberIds 则需看 world team 的 rankIds：设 worldTeam rankIds=[0,1,2,3]，则反查得 [2,0,3]——即 sub 成员在 world 中的 memberId。

**练习 2**：为什么 `AllocTeamSyncMem` 失败时要调 `HcommTeamDestroy(*team)` 回滚，而不是直接返回错误？
**答案**：第 3 步 `HcommTeamCreate` 已在 L3 分配了 device 内存（devTeam、devWorldTeamIds）并登记进 `teams_` 表；若不回滚就返回，这些资源将成为泄漏。回滚保证「要么全成、要么全无」。

**练习 3**：world team 和 sub team 传给 L3 的 `worldMemberIds` 有何区别？
**答案**：world team 传 nullptr（L3 生成 [0,memberNum) 恒等序列，见 [hcomm_team_mgr.cc:L103-L112](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L101-L112) 附近注释）；sub team 传反查得到的「sub 成员在 world 中的 memberId 数组」，长度等于 sub 的 memberNum。

### 4.3 窗口注册：HcclTeamWindowRegister

#### 4.3.1 概念说明

窗口把「一段本地 device 内存」升级为「team 可见的对称内存」：注册阶段只做两件事——在 L3 创建 window 实体、把 localMem 注册为可交换内存（拿到 memHandle 与 memTag）。真正的跨 rank 描述符交换发生在 `HcclTeamChannelsCreate`（见 4.4）。所以头文件注释才强调「window 必须在 ChannelsCreate 之后才能生效」。

三条硬约束：

1. `localMem` 必须是 **device 内存**（`COMM_MEM_TYPE_DEVICE`）；
2. `flag` 当前只支持 0（`HCOMM_TEAM_WINDOW_FLAG_SYMMETRIC`，对称窗口）；
3. 入参 team **只能是 worldTeam**——window 归 worldTeam 所有（1:N），sub team 通过「找到所属 worldTeam」共享这些 window。

还有一个优化：如果新注册的 localMem 是某个已有 window 注册内存的子集，直接复用旧 window（`TryReuseWindow`），避免重复建链交换。

#### 4.3.2 核心流程

```text
HcclTeamWindowRegister(comm, worldTeam, localMem, &window, flag)
  ├─ 校验：flag==0、localMem 为 device 内存且非空
  ├─ 校验 worldTeam 确实是 worldTeam 且属于 comm
  ├─ TryReuseWindow：已有 window 的 registeredLocalMem 覆盖 localMem？
  │    └─ 是 → 返回旧 window 句柄，结束
  └─ CreateNewWindow：
       ├─ HcommTeamWindowRegister（L3：建实体 + hrtMalloc devWindow + H2D）
       ├─ commMem->CommRegMem(userTag, localMem, &userHandle)  ← 注册为可交换内存
       │    └─ 失败则 HcommTeamWindowDeregister 回滚
       └─ HcclTeamMgr::AddWorldTeamWindow：记录 handle/localMem/userHandle/userTag
```

userTag 格式为 `HCCL_TEAM_USERMEM_TAG_PREFIX + commId + "_addr_..." + "_size_..."`，建链交换后对端靠 tag 前缀识别这是用户业务内存。

#### 4.3.3 源码精读

- [hccl_team_c_adpt.cc:L314-L327](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L314-L327) `TryReuseWindow`：委托 `HcclTeamMgr::FindReusableWindow` 找「registeredLocalMem 是入参 localMem 超集」的 window，命中即复用。
- [hccl_team_c_adpt.cc:L330-L355](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L330-L355) `CreateNewWindow`：L3 建 window → `CommRegMem` 注册 localMem（tag 带 `HCCL_TEAM_USERMEM_TAG_PREFIX` 前缀）→ 登记进 worldTeam 的 window 列表；注册失败时回滚销毁 window。
- [hccl_team_c_adpt.cc:L357-L399](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L357-L399) `HcclTeamWindowRegister` 主体：[L364-L369](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L364-L369) 校验 flag 与内存类型；[L377-L384](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L377-L384) 用 `FindWorldTeam(worldTeam) != worldTeam` 拒绝 sub team，并校验 worldTeam 属于当前 comm。
- [hccl_team_c_adpt.cc:L401-L425](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L401-L425) `HcclTeamWindowDeregister`：从 worldTeam 的 window 列表移除记录后调 L3 销毁；注释明确「不注销 localMem 的 MemReg（由通信域析构兜底清理）」。
- L3 侧 [hcomm_team_mgr.cc:L514-L559](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L514-L559) `WindowRegister`：先在**读锁**内校验「team 存在且非 subTeam」（注释专门解释了 shared_lock 不可延伸到 unique_lock 区间，否则自死锁），再 hrtMalloc devWindow、H2D 同步，最后**同时持有两把写锁**原子登记 `windows_` 与 `windowToTeamMap_`（锁顺序固定 windows→windowToTeam）。
- window 的实体定义在 [pkg_inc/hcomm/hcomm_team_entity_defs.h:L22-L28](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/pkg_inc/hcomm/hcomm_team_entity_defs.h#L22-L28)：`HcommWindow` 含 `memsNum`、`mems`（各成员内存数组）与 `worldTeam`（给数据面校验用）。注意注册阶段 `desc` 不被消费（[hcomm_team_c_adpt.cc:L79-L80](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_c_adpt.cc#L79-L80) 注释），`mems` 由后续 `HcommTeamWindowBindRemoteMems` 单独绑定。

#### 4.3.4 代码实践

1. **实践目标**：理解「注册」与「生效」两阶段分离。
2. **操作步骤**：阅读 [hccl_team_c_adpt.cc:L357-L399](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L357-L399)，列出 `HcclTeamWindowRegister` 里所有会返回 `HCCL_E_PARA` 的分支条件；再追进 `CreateNewWindow` 数一数窗口创建总共涉及几次内存/资源分配。
3. **需要观察的现象**：注册路径中没有任何跨 rank 通信——所有交换都被推迟到 ChannelsCreate。
4. **预期结果**：得到一份「参数拒绝清单」（flag≠0、非 device 内存、addr 空、size 0、team 非 worldTeam、worldTeam 不属于 comm）。资源分配共 3 类：L3 window 实体（device）、localMem 的 MemReg 句柄（host 侧登记）、HcclTeamMgr 里的 WindowInfo 记录。

#### 4.3.5 小练习与答案

**练习 1**：为什么 window 只能注册在 worldTeam 上，而 sub team 也能用？
**答案**：window 归 worldTeam 所有（1:N），其 `mems` 维度是 worldMemberNum。sub team 在 ChannelsCreate 时通过 `FindWorldTeam` 找到所属 world team 的全部 window（[hccl_team_c_adpt.cc:L473-L509](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L473-L509) 的 `GetWorldTeamContext`），并靠 device 侧 `worldTeamIds` 映射回 world 资源。集中持有避免了每个 sub team 重复注册与交换。

**练习 2**：`TryReuseWindow` 的复用判据是什么？为什么这样设计？
**答案**：已有 window 的 `registeredLocalMem` 是新入参 localMem 的超集即可复用（[hccl_team_mgr.h:L89-L91](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_mgr.h#L89-L91)）。因为建链交换按「已注册内存描述符」进行，若新旧内存有包含关系，对端已能覆盖新内存的地址范围，重复交换纯属浪费。

### 4.4 HcclTeamChannelsCreate：批量建链与远端内存绑定

#### 4.4.1 概念说明

`HcclTeamChannelsCreate` 是 team 机制里最「重」的接口：它为本 rank 与 team 内每个其他成员各建 `channelCnt` 条 channel，在建链过程中顺带完成 syncMem 与所有 window 的内存描述符交换，最后把 channel 句柄数组、远端内存数组全部绑定回 L3 并同步到 device。调用返回后，device 侧的 `HcommTeam` 实体（channelsBaseAddr/channelNumPerMember/syncMem）即可被通信算子直接使用。

这里能直接看到前几讲知识的复用：查链路用 u2-l4 的 `HcclRankGraphGetLinks`（按 netLayer 拿本 rank 到 peer 的 `CommLink`，从中取协议与两端 endpoint），建 channel 用 `HcclChannelAcquire`（channel 资源属 u3-l4 的数据面）。

#### 4.4.2 核心流程

`HcclTeamChannelsCreate`（[hccl_team_c_adpt.cc:L666-L707](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L666-L707)）拆成六步辅助函数：

```text
HcclTeamChannelsCreate(comm, team, desc)
  ├─ 校验 channelCnt≠0、team 属于 comm
  ├─ GetTeamMemberInfo：取 rankIds，算 memberNum 与 selfMemberId
  ├─ RegisterTeamSyncMem：把 team 的 syncMem 注册为可交换内存（tag 带
  │    HCCL_TEAM_SYNCMEM_TAG_PREFIX，仅首次注册一次）
  ├─ GetWorldTeamContext：取 worldTeam 的 windows、待交换 memHandles
  │    （syncMemHandle + 各 window 的 localMemHandle）、curToWorld 映射
  ├─ AcquireChannels：对每个 peer member × channelCnt 条
  │    ├─ FillChannelDescForPeer：HcclTeamGetNetLayer + HcclRankGraphGetLinks
  │    │    → 取 link 的协议、本端/对端 endpoint 填进 HcclChannelDesc
  │    └─ HcclChannelAcquire(comm, engine, descs, cnt, handles)
  │         （建链时把待交换 memHandles 附到 channel 上带去对端）
  ├─ BindTeamChannels：构造 HcommTeamBindChannelsDesc → HcommTeamBindChannels
  ├─ CollectRemoteMems：对每条 channel 调 HcclChannelGetRemoteMems，
  │    按 tag 前缀分流：SYNCMEM 前缀 → syncMem 槽（memberId 维）；
  │    USERMEM 前缀 → remoteMemsByWindow（window × worldMemberId 维）
  └─ BindWindowsAndSyncMem：
       ├─ per window：HcommTeamWindowBindRemoteMems（self 槽填本地 registeredLocalMem）
       └─ HcommTeamBindRemoteSyncMem（绑定各成员远端 syncMem）
```

L3 的 `BindChannels` 之后，`HcommTeamMgr::AllocAndCopyChannels`（[hcomm_team_mgr.cc:L225-L249](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L225-L249)）会把所有 channel 的 `ChannelEntity` 本体按「前缀和偏移」D2D 拷贝进一块连续 device 内存，基地址写入 `hostTeam.channelsBaseAddr`——device 侧算子按 `sum(channelNumPerMember[0..peer-1]) + channelIdx` 的偏移公式直接寻址（[hcomm_team_entity_defs.h:L47-L49](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/pkg_inc/hcomm/hcomm_team_entity_defs.h#L47-L49)）。

销毁侧的级联关系在 [hccl_team_c_adpt.cc:L242-L271](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L242-L271) `HcclTeamDestroy`：若入参是 worldTeam，先递归销毁其所有 subTeam，再注销其所有 window，最后 `UnregisterTeam`（hrtFree syncMem）+ L3 `HcommTeamDestroy`。

#### 4.4.3 源码精读

- [hccl_team_c_adpt.cc:L429-L450](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L429-L450) `FillChannelDescForPeer`：先经 `HcommTeamGetNetLayer` 拿 team 的 netLayer（来自创建时的 desc），再调 u2-l4 学过的 `HcclRankGraphGetLinks` 取第一条 link，把 `linkProtocol`、`srcEndpointDesc`、`dstEndpointDesc` 填进 `HcclChannelDesc`——team 的协议最终由拓扑链路决定。
- [hccl_team_c_adpt.cc:L512-L546](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L512-L546) `AcquireChannels`：跳过 self member；每条 channel 填 `notifyNum`，并把 `ctx.memHandles` 附到描述符上（`memHandles/memHandleNum` 字段），随 `HcclChannelAcquire` 建链时交换给对端。
- [hccl_team_c_adpt.cc:L576-L623](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L576-L623) `CollectRemoteMems`：`HcclChannelGetRemoteMems` 拿到对端交换来的内存与 tag 后，按 tag 前缀（`HCCL_TEAM_SYNCMEM_TAG_PREFIX` / `HCCL_TEAM_USERMEM_TAG_PREFIX`）分流到 syncMem 槽或 window 槽——**tag 是跨 rank 识别内存用途的钥匙**。
- [hccl_team_c_adpt.cc:L626-L664](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L626-L664) `BindWindowsAndSyncMem`：每个 window 的 mems 数组以 worldMemberNum 为长度，self 槽填本地 `registeredLocalMem`，与 peer 槽的远端 CommMem 对称——这就是「对称窗口」的含义。
- [hccl_team_c_adpt.cc:L275-L310](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L275-L310) `RegisterTeamSyncMem`：syncMem 是 team 粒度、只注册一次；tag 里带 commId、team 指针、地址与大小，天然全局唯一。
- L3 的 channel 落盘见 [hcomm_team_mgr.cc:L153-L200](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L153-L200) `AllocChannelEntities` 与 [L202-L223](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L202-L223) `AllocChannelNumsArray`：前者逐条 D2D 拷贝 ChannelEntity 到连续数组，后者把每成员 channel 数拷到 device。
- L3 绑定入口 [hcomm_team_c_adpt.cc:L53-L69](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_c_adpt.cc#L53-L69)（BindChannels/BindRemoteSyncMem）与 [L84-L92](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_c_adpt.cc#L84-L92)（WindowBindRemoteMems）均为「查空 + 直通单例」的薄适配。

#### 4.4.4 代码实践（本讲综合实践入口）

1. **实践目标**：写出 world team → sub team → 通道批量创建的完整调用骨架，并打印各阶段句柄。
2. **操作步骤**：以下为**示例代码**（基于 u1-l4 的 01_one_device_per_process 示例改造，仅演示接口用法；当前仓库 examples 未包含 team 示例，待本地验证）：

```cpp
// 示例代码：team 创建骨架（非仓库原有代码）
#include "hccl.h"
#include "hccl_team.h"

HcclComm comm;
HcommTeamHandle worldTeam = nullptr;
HcommTeamHandle subTeam = nullptr;

// ① 通信域初始化（root info 方式，见 u1-l4）...

// ② 创建 world team：成员为通信域全部 rank，只要 barrier 同步
HcclTeamCreateDesc worldDesc;
HCCLCHECK(HcclTeamCreateDescInit(&worldDesc));
uint32_t rankIds[WORLD_RANK_NUM] = {0, 1, 2, 3};   // 示例：4 rank
worldDesc.rankIds = rankIds;
worldDesc.rankNum = WORLD_RANK_NUM;
worldDesc.selfRankId = myRank;
worldDesc.requirement.barrierCount = 1;             // signal/counter 必须为 0
HCCLCHECK(HcclWorldTeamCreate(comm, &worldDesc, &worldTeam));
printf("worldTeam handle = %p\n", (void*)worldTeam);   // device 侧 HcommTeam 地址

// ③ 从 world team 派生 sub team：例如只取 rank 0 和 1
HcclTeamCreateDesc subDesc;
HCCLCHECK(HcclTeamCreateDescInit(&subDesc));
uint32_t subRankIds[2] = {0, 1};
subDesc.rankIds = subRankIds;
subDesc.rankNum = 2;
subDesc.selfRankId = myRank;                        // 必须能在 subRankIds 中找到
subDesc.requirement.barrierCount = 1;
if (myRank < 2) {                                   // 不在 sub team 内的 rank 不调用
    HCCLCHECK(HcclSubTeamCreate(worldTeam, &subDesc, &subTeam));
    printf("subTeam handle = %p\n", (void*)subTeam);
}

// ④ 批量建链（所有 team 内 rank 都要调用；channelCnt 至少为 1）
HcclTeamCreateChannelsDesc chDesc;
HCCLCHECK(HcclTeamCreateChannelsDescInit(&chDesc));
chDesc.engine = COMM_ENGINE_AICPU_TS;               // 按需选择引擎
chDesc.channelCnt = 1;
chDesc.notifyNum = 1;
HCCLCHECK(HcclTeamChannelsCreate(comm, worldTeam, &chDesc));

// ⑤ 按依赖逆序释放：subTeam → worldTeam（worldTeam 销毁会级联 subTeam 与 window）
if (subTeam != nullptr) { HCCLCHECK(HcclTeamDestroy(subTeam)); }
HCCLCHECK(HcclTeamDestroy(worldTeam));
```

3. **需要观察的现象**：worldTeam 与 subTeam 打印出的两个不同地址；plog（HCCL_INFO 日志）中 `TeamCreate`、`HcclTeamChannelsCreate` 两条 success 日志里的 memberNum/syncMemSize/channelCnt 字段。
4. **预期结果**：日志中 world team 的 syncMemSize = 1×8×4 = 32 字节（barrierCount=1、memberNum=4），sub team 为 1×8×2 = 16 字节；与 [hcomm_team_mgr.cc:L417-L420](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L417-L420) 的公式一致。运行结果待本地验证（需昇腾环境）。
5. **思考题**：若第 ④ 步只对 world team 调了 ChannelsCreate，sub team 的 channel 从哪来？（见练习 1）

#### 4.4.5 小练习与答案

**练习 1**：sub team 的 channel 是在 `HcclSubTeamCreate` 时建的吗？
**答案**：不是。sub team 创建只落了成员表与 syncMem 大小；channel 要对本 sub team 再调一次 `HcclTeamChannelsCreate`（`AcquireChannels` 按 team 的 memberNum 为每个 peer 建链）。`HcclSubTeamCreate` 全程没有任何跨 rank 数据交换。

**练习 2**：对端怎么区分交换来的内存是 syncMem 还是用户业务内存？
**答案**：靠注册时的 memTag 前缀。syncMem 的 tag 带 `HCCL_TEAM_SYNCMEM_TAG_PREFIX`，window 业务内存带 `HCCL_TEAM_USERMEM_TAG_PREFIX`；`CollectRemoteMems` 用 `tag.compare(0, strlen(prefix), prefix)` 分流（[hccl_team_c_adpt.cc:L609-L618](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L609-L618)）。

**练习 3**：直接销毁 worldTeam 而忘了先销毁 subTeam 会泄漏吗？
**答案**：不会。`HcclTeamDestroy` 检测到入参是 worldTeam 时，先递归销毁其全部 subTeam、再注销其全部 window，最后才清理自身（[hccl_team_c_adpt.cc:L246-L260](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L246-L260)）。反过来先销毁 subTeam 再销毁 worldTeam 也是合法顺序。

## 5. 综合实践

**任务：为「2 节点 barrier + 数据搬运」场景规划 team 资源并画出资源账本。**

场景：通信域共 4 个 rank（0~3）；rank 0/1 组成 sub team A 做参数同步（barrier），rank 0/1/2/3 用 world team 做全量通信；业务缓冲区是一段 64MB 的 device 内存。

要求完成：

1. 写出正确的调用顺序（接口名列表），标出哪些调用要求「team 内所有 rank 都参与」、哪些只由 sub team 成员调用。
2. 参照 4.2 的调用链，画一张「资源账本」图：横轴为调用步骤（WorldTeamCreate → SubTeamCreate → WindowRegister → ChannelsCreate → Destroy），纵轴为资源（device HcommTeam、device HcommWindow、syncMem、MemReg 句柄、ChannelEntity 连续数组、worldTeamIds 数组），标注每步在哪一层（L2/L3/device）分配或释放了什么。
3. 用源码行号佐证账本中至少 5 个条目（例如 syncMem 在 [hccl_team_c_adpt.cc:L94-L105](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hccl/hccl_team_c_adpt.cc#L94-L105) 申请、ChannelEntity 数组在 [hcomm_team_mgr.cc:L153-L200](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L153-L200) 分配）。
4. 进阶：若业务内存后来换成了 128MB（覆盖原 64MB），说明为什么第二次 `HcclTeamWindowRegister` 会命中 `TryReuseWindow` 而不新建 window。

## 6. 本讲小结

- team 是挂在通信域上的「轻」资源：成员表 + 同步内存 + channel + 窗口，两级抽象中 `HcclComm` 管 bootstrap 与拓扑，`HcommTeamHandle`（device 侧 `HcommTeam` 地址）供通信算子直接寻址。
- 创建链路分两层：L2（`hccl_team_c_adpt.cc`）做校验、rankId→memberId 翻译、syncMem 分配与登记（`HcclTeamMgr`），L3（`HcommTeamMgr::TeamCreate`）构造 host 实体并 H2D 同步到 device；每步失败均有回滚。
- window 只能注册在 worldTeam 上（对称窗口、device 内存、flag=0），且必须经 `HcclTeamChannelsCreate` 完成描述符交换后才生效；localMem 被旧 window 覆盖时直接复用。
- `HcclTeamChannelsCreate` 是批量编排接口：查拓扑链路（`HcclRankGraphGetLinks`）→ 按 peer×channelCnt 建 channel（`HcclChannelAcquire`）→ 按 memTag 前缀回收远端内存并绑定 syncMem 与各 window → channel 实体按前缀和偏移排入连续 device 数组。
- 销毁按「subTeam → window → worldTeam」级联；通信域析构还有 `ClearByCollComm` 兜底，team 生命周期永远不超过其 owner。
- device 侧契约（`HcommTeam`/`HcommWindow`/`HcommTeamSyncMem`）定义在 pkg_inc 的 `hcomm_team_entity_defs.h`，是 host/device 共享 ABI，延续了 CommAbiHeader 的版本化兼容设计。

## 7. 下一步学习建议

本讲结束后，你已经掌握控制面 team 机制的全貌，接下来自然过渡到**数据面 base_comm**（第三单元）：

- **u3-l2 Endpoint 端点资源**：本讲 `FillChannelDescForPeer` 填进 `HcclChannelDesc` 的 `localEndpoint/remoteEndpoint`，其本体就是 Endpoint 描述符，下一讲讲它如何创建与多协议实现。
- **u3-l4 Channel 通道与 ABI 兼容**：本讲大量出现 `HcclChannelAcquire`/`HcclChannelDesc`，下一讲拆解 `hcomm_channel.h` 的描述符版本演进与 QoS 字段。
- **u3-l3 内存注册与交换**：本讲的 `CommRegMem`/`HcclChannelGetRemoteMems` 只是入口，下一讲沿 reged_mems 模块看「本端注册→描述符导出→对端导入」的完整机制。

建议同步阅读 [examples/01_communicators](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/README.md) 对照通信域初始化方式，并把 4.4.4 的骨架程序留到学完 u3-l7 后扩展成真正的 ping-pong 测试。
