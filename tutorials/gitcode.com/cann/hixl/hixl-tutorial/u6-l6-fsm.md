# u6-l6 FSM：发送/接收状态机

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 LLM-DataDist 中 FSM（Finite State Machine，有限状态机）模块存在的意义：**服务端如何在没有专用网络协议栈的情况下，「发现」并「逐步执行」远端发来的传输请求**。
2. 掌握 `StateManager` 单例如何把 `IdleState`、`ReceiveState`、`SendState` 三个无状态单例注册成全局状态表。
3. 掌握 `idle → receive → send → idle` 循环的进入条件、每个状态的职责，以及 `INIT/ERROR/DESTROYED` 三个「离线态」的用法。
4. 理解角色切换（`SetRole` / switch roles）与 FSM 的关系：为什么切换前必须先 `Unlink`，切换到底切换了什么。
5. 能结合 `decoder_switch_roles` / `prompt_switch_roles` 样例画出两端实例在角色切换前后的状态机迁移图。

## 2. 前置知识

本讲建立在前几讲（尤其是 u6-l2 建链、u6-l3 Cache 管理、u6-l5 DataTransfer Job）之上，先回顾几个关键概念，再用通俗语言补充本讲新概念。

**已有认知回顾：**

- **CommEntity**：建链握手时两端各自创建的「通信实体」对象，保存在 `CommEntityManager` 的台账里。它持有 ACL stream、请求/响应缓冲区地址、CacheManager 引用等。本讲的 FSM 就挂在每个 CommEntity 上。
- **单边传输**：client（发起 Pull 的一端）可以直接读写 server 端注册过的内存，server 的 CPU 不需要参与数据搬运。这带来一个问题——**server 怎么知道有人要拉数据、要拉哪块？** 这正是 FSM 要回答的问题。
- **TransferCacheReq**：u6-l5 讲过的传输请求结构体，是两端之间的全部合同。client 把它写进 server 的请求缓冲区，FSM 负责取出来解析并执行。

**本讲新概念：**

- **状态机（FSM）**：把一个对象的行为拆成若干「状态」，每个状态只做自己该做的事，做完后迁移到下一个状态。好处是把一段复杂的长流程切成小步，每步可以被外部周期性地「推一下」继续走，而不是一个函数一口气跑完。
- **状态对象无状态**：三个 State 类不持有任何成员变量，所有可变数据（当前状态、请求、job、超时点）都存在 `CommEntity` 里。状态类只定义「在这个状态下该做什么」，因此三个状态可以用进程级单例共享给所有 CommEntity。
- **完成标志位（flag）轮询**：client 把请求写到 server 的请求缓冲区后，再单边写入 1 字节标志位置 1；server 侧的 FSM 线程不停轮询这个标志位，看到 1 就知道「有新请求了」。这是无网络协议栈场景下最朴素的通知方式，和 u5-l3 FabricMem 的 host flag、u4-l1 的 trans_flag 是同一思路。
- **volatile**：C++ 关键字，告诉编译器这个内存可能被「别人」（这里是 DMA 直接写入的设备侧数据）修改，每次都要真的去读内存，不要用寄存器里的旧值缓存。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/llm_datadist/common/common.h` | 定义 `FsmState` 枚举（七态）与 `TransferCacheReq` 请求结构体 |
| `src/llm_datadist/fsm/base_state.h` | `BaseState` 抽象基类，约定 `Preprocess/Process/Postprocess` 三接口 |
| `src/llm_datadist/fsm/state_manager.h/.cc` | `StateManager` 单例：状态注册表与查询 |
| `src/llm_datadist/fsm/idle_state.h/.cc` | 空闲态：空操作后立即迁往 receive |
| `src/llm_datadist/fsm/receive_state.h/.cc` | 接收态：轮询请求标志位，看到新请求迁往 send |
| `src/llm_datadist/fsm/send_state.h/.cc` | 发送态：解析请求、创建 DataTransferJob 并分批驱动执行（最重的一个状态） |
| `src/llm_datadist/link_mgr/comm_entity.h/.cc` | 状态机的「宿主」：持有 `cur_state_` 原子变量，提供 `ProcessState/ChangeState/SendRequest` 等 |
| `src/llm_datadist/link_mgr/comm_entity_manager.cc` | FSM 驱动线程（`ge_llm_fsm`）：遍历所有实体逐个推状态机 |
| `src/llm_datadist/api/llm_datadist_impl.cc` | `LlmDataDist::SetRole` 外壳：选项翻译后下沉 |
| `src/llm_datadist/llm_datadist_v2.cc`、`transfer_engine/hixl_transfer_engine.cc`、`link_mgr/llm_link_manager.cc` | `SwitchRole` 的三层下沉链，最终落到监听 daemon 的停启 |
| `examples/cpp/decoder_switch_roles.cpp`、`examples/cpp/prompt_switch_roles.cpp` | 角色切换双进程样例，本讲综合实践的素材 |

## 4. 核心概念与源码讲解

### 4.1 StateManager：三态注册中心

#### 4.1.1 概念说明

`StateManager` 回答一个很小的工程问题：**「状态编号 → 状态对象」的映射放在哪里？** 它是一个进程级单例，构造时把三个状态对象（同样是进程级单例）注册进数组，之后任何 CommEntity 都通过它按 `FsmState` 枚举取状态对象。

为什么三个状态对象可以全局共享？因为它们**完全没有成员变量**——`IdleState`、`ReceiveState`、`SendState` 的所有方法都是无状态的（`SendState` 的私有方法全是 `static`），可变数据一律通过 `CommEntity &entity` 参数传入。这是「策略无状态、上下文在实体」的经典状态机写法。

#### 4.1.2 核心流程

```text
进程首次访问 StateManager::GetInstance()
  └─ 构造函数
       ├─ static IdleState idle_state;      // 静态对象，进程级
       ├─ RegisterState(FSM_IDLE_STATE, &idle_state, "FSM_IDLE_STATE")
       ├─ static ReceiveState receive_state;
       ├─ RegisterState(FSM_RECEIVE_STATE, ...)
       ├─ static SendState send_state;
       └─ RegisterState(FSM_SEND_STATE, ...)

之后每次状态迁移 / 状态推进：
  GetState(id) → state_[id]（O(1) 数组下标访问）
```

七态枚举（注意只有前三个有状态对象，后四个是「标记态」，见 4.2 节）：

```text
FSM_INIT_STATE      = 0   // 实体已创建但内存尚未就绪，FSM 线程跳过
FSM_IDLE_STATE      = 1   // 空闲态
FSM_RECEIVE_STATE   = 2   // 接收态
FSM_SEND_STATE      = 3   // 发送态
FSM_ERROR_STATE     = 4   // 出错态，FSM 线程跳过（不再推进）
FSM_DESTROYED_STATE = 5   // 已销毁，等待从台账摘除
FSM_INVALID_STATE   = 6   // 数组长度哨兵
```

#### 4.1.3 源码精读

枚举定义在公共头里：

- [src/llm_datadist/common/common.h:L22-L30](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/common/common.h#L22-L30) — `FsmState` 七态枚举，`FSM_INVALID_STATE` 同时被 `StateManager` 用作数组长度。

注册逻辑是构造函数的全部：

- [src/llm_datadist/fsm/state_manager.cc:L17-L29](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/state_manager.cc#L17-L29) — 单例 `GetInstance()`；构造函数里用函数级 `static` 对象保证三个状态只创建一次，随后逐个 `RegisterState`。

- [src/llm_datadist/fsm/state_manager.cc:L31-L42](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/state_manager.cc#L31-L42) — `RegisterState` 把指针和描述字符串按下标存入数组；`GetState`/`GetStateDesc` 是纯数组访问。

- [src/llm_datadist/fsm/state_manager.h:L36-L40](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/state_manager.h#L36-L40) — 两个定长数组 `state_[]`、`stateDesc_[]`，长度就是 `FSM_INVALID_STATE`；拷贝与移动构造全部 delete，单例不可复制。

状态对象的公共接口：

- [src/llm_datadist/fsm/base_state.h:L21-L33](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/base_state.h#L21-L33) — `BaseState` 纯虚三接口 `Preprocess/Process/Postprocess`。语义约定：`Preprocess` 在**进入**该状态时执行一次（含迁移决策），`Process` 被 FSM 线程**每次推进**时调用（可被反复调用），`Postprocess` 负责**迁出**到下一状态。

#### 4.1.4 代码实践

**实践目标**：验证「三个状态对象是进程级单例、对所有 CommEntity 共享」这一论断。

**操作步骤**（源码阅读型实践）：

1. 打开 `src/llm_datadist/fsm/idle_state.h`、`receive_state.h`、`send_state.h`，检查三个类的成员变量。
2. 对比 `send_state.h` 中私有方法的声明方式。

**需要观察的现象**：三个类除继承外**没有任何数据成员**；`SendState` 的全部私有方法带 `static` 修饰符（[send_state.h:L43-L53](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.h#L43-L53)），`IdleState`/`ReceiveState` 则连私有段都没有。

**预期结果**：确认状态类无状态，因此 `StateManager` 里的三个静态对象可被并发共享，不需要加锁。若将来有人给 State 类加成员变量，就会破坏这一前提——这是本模块最重要的一条隐形契约。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StateManager` 用定长数组而不是 `std::map<FsmState, BaseState*>`？

**答案**：`FsmState` 是从 0 开始连续取值的枚举，且 `FSM_INVALID_STATE` 天然充当数组长度，用数组下标访问是 O(1) 且零分配；FSM 线程高频调用 `GetState`，map 的红黑树查找与潜在的节点内存开销没有必要。此外状态集合在构造后永不变化，不需要动态增删。

**练习 2**：如果调用 `GetState(FSM_ERROR_STATE)` 会返回什么？会出问题吗？

**答案**：返回 `nullptr`（`RegisterState` 只注册了三个状态）。不会出问题，因为代码里所有取状态的路径要么取的是三个工作态之一，要么在取之前先判掉了 ERROR/DESTROYED/INIT（见 4.2.3 的 `ProcessState`：`GetState` 返回空时打日志返回 `ge::FAILED`，调用方会走 `MarkEntityError`，而 ERROR 态实体在遍历时被 `HandleAllEntities` 直接 continue 跳过，不会再次取状态）。

---

### 4.2 驱动模型：CommEntity 宿主与 ge_llm_fsm 线程

#### 4.2.1 概念说明

状态机光有状态对象还不够，需要回答三个问题：**状态存在哪（宿主）？谁来推（驱动）？怎么推（推进语义）？**

- **宿主是 CommEntity**：每个远端集群链路对应一个 CommEntity，它用一个 `std::atomic<FsmState> cur_state_` 保存当前状态。同一进程内可以有多个 CommEntity（同时连接多个对端集群），它们**各自独立**跑着同一个三态循环。
- **驱动者是专用线程**：`CommEntityManager::Initialize(start_service=true)` 时启动一条名为 `ge_llm_fsm` 的线程，死循环遍历所有实体，对每个实体调一次 `ProcessState()`（即推一下当前状态的 `Process`）。
- **推进语义是「迁移即入态」**：`ChangeState(next)` 先原子写入新状态，然后**立即**调用新状态的 `Preprocess`。由于 `IdleState` 和 `ReceiveState` 的三个方法都是首尾相接直通调用（Preprocess→Process→Postprocess→ChangeState），一次推进可能在一个调用链里连过几个状态；而 `SendState` 的 `Process` 会因为 job 未完成而提前返回，此时状态机「停在」SEND 态等下一轮推进。这就是「长流程切片」的实现方式。

#### 4.2.2 核心流程

```text
ge_llm_fsm 线程主循环（HandleCacheRequest）
  while (running_):
    HandleAllEntities():
      对 entity_map_ 中每个 entity:
        ① 状态为 INIT 或 ERROR        → 跳过（不推进）
        ② try_lock 实体互斥锁失败     → 跳过（说明 Pull 请求正在占用该实体）
        ③ 状态为 DESTROYED            → 从台账摘除
        ④ 否则 entity->ProcessState() → state->Process(*this)
             Process 失败 → entity->MarkEntityError()（实体进入 ERROR，之后不再推进）

单个实体的状态循环：
  IDLE.Process ──迁移──▶ RECEIVE.Preprocess
  RECEIVE.Process: 轮询请求标志位
      flag != 1 → 返回（停在 RECEIVE，等下一轮）
      flag == 1 → 清标志，迁移到 SEND
  SEND.Preprocess(即 Prepare):
      解析请求、查 Cache、建 DataTransferJob
      失败 → SendResponse(错误码) 并直接回 IDLE
  SEND.Process:
      超时   → SendResponse(LLM_TIMEOUT)，回 IDLE
      未完成 → 返回（停在 SEND，等下一轮继续推 job）
      完成   → （必要时 RemoveCacheKey）回 IDLE
```

注意 ② 的互斥锁：Pull 请求的 client 路径（`DataTransferClient`）与 FSM 线程共享同一个 CommEntity，`try_lock` 保证同一实体的请求缓冲区不会一边被 FSM 读、一边被新请求覆写。这也解释了 u6-l5 里「SendState 周期性非阻塞驱动 job」的由来——驱动源就是这条线程。

#### 4.2.3 源码精读

- [src/llm_datadist/link_mgr/comm_entity.h:L199](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.h#L199) — `std::atomic<FsmState> cur_state_{FsmState::FSM_INIT_STATE}`：状态存在实体上，初始为 INIT（建链握手完成、内存准备好后才会进入工作态）。

- [src/llm_datadist/link_mgr/comm_entity.cc:L331-L345](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.cc#L331-L345) — `ProcessState`（取当前状态调 `Process`，取不到报 FAILED）与 `ChangeState`（先原子写新状态，再调新状态 `Preprocess`——「迁移即入态」的实现就在这两行）。

- [src/llm_datadist/link_mgr/comm_entity.cc:L318-L329](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.cc#L318-L329) — 三个标记态的写入函数：`MarkEntityDestroyed`/`MarkEntityError`/`MarkEntityIdle`，都是对 `cur_state_` 的原子 store。它们不经过 `ChangeState`（不会触发 Preprocess），因为这些状态没有对应的状态对象。

- [src/llm_datadist/link_mgr/comm_entity_manager.cc:L148-L154](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L148-L154) — FSM 驱动线程入口 `HandleCacheRequest`：`pthread_setname_np` 把线程命名为 `ge_llm_fsm`，恢复 ACL context 后进入 `while (running_) HandleAllEntities();` 死循环。注意循环体内**没有 sleep**，是忙轮询——这也意味着每个实体的 RECEIVE 轮询开销极低但 CPU 占有一个核。

- [src/llm_datadist/link_mgr/comm_entity_manager.cc:L124-L146](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L124-L146) — `HandleAllEntities`：完整的推进策略。INIT/ERROR 跳过；`try_lock` 失败跳过（实体正被 Pull 请求占用）；DESTROYED 就地摘除；`ProcessState` 失败立即 `MarkEntityError`，失败实体从此冻结，直到断链重建。

- [src/llm_datadist/link_mgr/comm_entity_manager.cc:L156-L167](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L156-L167) — `Initialize(start_service)`：只有 `start_service=true` 才起 FSM 线程（client-only 实例不需要服务远端请求）。

再看请求是怎么「打进来」的——client 侧的单边 PUT（FSM 的存在动机）：

- [src/llm_datadist/link_mgr/comm_entity.cc:L624-L642](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.cc#L624-L642) — `CommEntity::SendRequest`：先在本地把请求缓冲区首字节标志位置 1 并 H2D 拷入 device 缓冲，然后两条 `BatchPutAsync`——第一条把 `req_size` 字节的 `TransferCacheReq` 单边写进 server 的请求区，第二条单独把 1 字节标志位写到 server 的标志位地址。**先写请求、后写标志**的顺序保证 server 看到标志位为 1 时请求内容已就绪（同一 stream 上有序）。

- [src/llm_datadist/data_transfer/data_transfer_client.cc:L183-L205](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L183-L205) — client 侧对称的等待逻辑 `SynchronizeStreamTask`：带超时的流同步之后，轮询**本端**的响应标志位（server 执行完会单边写回），拿到 1 即清零返回。请求/响应两个方向用的是同一套「标志位 + 单边写」机制。

#### 4.2.4 代码实践

**实践目标**：在运行环境中亲眼看到 `ge_llm_fsm` 线程，并理解它是「每个 LlmDataDist 实例一条」还是「进程一条」。

**操作步骤**：

1. 在两台互通的昇腾机器上分别启动 `prompt_push_cache_and_blocks` 与 `decoder_pull_cache_and_blocks`（或本讲的 switch_roles 样例）。
2. 在 server 侧机器执行：`ps -T -p $(pgrep -f prompt_push) | grep fsm`。
3. 对照源码 `HandleCacheRequest` 的 `pthread_setname_np` 确认线程名来源。

**需要观察的现象**：进程线程列表中出现名为 `ge_llm_fsm` 的线程，且只有一条，无论连接多少个对端集群。

**预期结果**：一条 FSM 线程串行遍历所有 CommEntity（`HandleAllEntities` 持管理器锁逐个推进），多链路共享一条驱动线程。若无昇腾硬件环境，此步骤**待本地验证**，可改为纯源码验证：从 `Initialize` 只创建一个 `cache_engine_thread_`（[comm_entity_manager.cc:L163](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity_manager.cc#L163)）得出同样结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HandleAllEntities` 里对每个实体用 `try_lock` 而不是阻塞 `lock`？

**答案**：FSM 线程要公平地推进所有实体。若某个实体正被用户的 Pull 请求（`DataTransferClient` 路径）长时间持有互斥锁，阻塞 lock 会让 FSM 线程卡在这个实体上，其他所有实体的请求都得不到处理。try_lock 失败就跳过本轮、下轮再来，把「慢实体」的影响隔离在自身。

**练习 2**：一个实体进入 `FSM_ERROR_STATE` 之后还能自动恢复吗？

**答案**：不能。`HandleAllEntities` 对 ERROR 态实体直接 continue，永远不会调用它的 `ProcessState`，也就没有任何代码路径把它迁回 IDLE。恢复手段只有断链重建（Unlink 后重新 Link，销毁旧实体、创建新实体，新实体从 INIT 重新开始）。这是「失败冻结、销毁重建」的设计，与 u4-l1 CS 层的 `transfer_failure_latched_` 失败闩锁思路一致。

---

### 4.3 IdleState 与 ReceiveState：轻量的等待状态

#### 4.3.1 概念说明

这两个状态是三态循环中的「等待半区」，代码量极小，但恰好体现了状态机拆分的意图：

- **IdleState（空闲态）**：一个「呼吸节点」。它什么都不做，唯一的动作就是迁移到 RECEIVE。它的存在让循环有一个明确的起点和汇合点——SEND 结束后回到 IDLE 而不是直接回 RECEIVE，使得「处理完一个请求」与「开始等下一个请求」之间有一个清晰的状态边界，也方便日志和统计观察请求间隙。
- **ReceiveState（接收态）**：真正的「等活儿」状态。它轮询本实体请求缓冲区的标志位：为 0 就原地返回（本轮推进结束，下轮再查）；为 1 说明 client 已单边写入了新请求，清掉标志位后迁移到 SEND 去处理。

注意一个细节：进入 RECEIVE 的时机是「空闲即进入」，而不是「有请求才进入」。也就是说 FSM 线程绝大多数时间都停在 RECEIVE 态反复查标志位——这正是 4.2 节说「忙轮询」的含义。

#### 4.3.2 核心流程

```text
IDLE（三步直通，一次推进即穿过）:
  Preprocess → Process → Postprocess: ChangeState(RECEIVE)
      └─ ChangeState 内立即调 RECEIVE.Preprocess
         └─ RECEIVE.Preprocess → Process（开始查标志位）

RECEIVE:
  Process:
    flag = *entity.GetCacheInfoFlag()      // volatile 读
    flag != 1 → return SUCCESS             // 停在 RECEIVE
    flag == 1 → ClearReqFlag()（写 0）→ Postprocess
  Postprocess: ChangeState(SEND)
      └─ 立即调 SEND.Preprocess（即 Prepare，见 4.4）
```

标志位的内存布局（u6-l2 建链时两端交换的请求/响应缓冲区，u6-l5 曾提到「控制消息也走单边 PUT」）：

```text
server 侧请求缓冲区:  [flag 1字节 | TransferCacheReq ...]
                       ▲            ▲
                       │            └─ client 的 SendRequest 第一条 PUT 写这里
                       └─ client 的 SendRequest 第二条 PUT 写这里（置 1）
server 处理完 → SendResponse 单边写 client 侧响应缓冲区 [flag | ResponseInfo]
```

#### 4.3.3 源码精读

- [src/llm_datadist/fsm/idle_state.cc:L13-L25](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/idle_state.cc#L13-L25) — IdleState 全部逻辑：三个方法互相直通，最终 `entity.ChangeState(FsmState::FSM_RECEIVE_STATE)`。只有两条 DEBUG 日志。

- [src/llm_datadist/fsm/receive_state.cc:L18-L27](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/receive_state.cc#L18-L27) — ReceiveState::Process：`volatile int8_t *volatile flag` 双 volatile（指针与指向对象都不许缓存优化）；`*flag == 1` 时递增统计计数 `sync_flag_get_times`、`ClearReqFlag()` 清零、进入 Postprocess。

- [src/llm_datadist/fsm/receive_state.cc:L29-L32](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/receive_state.cc#L29-L32) — Postprocess 迁移到 SEND。

- [src/llm_datadist/link_mgr/comm_entity.cc:L347-L354](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.cc#L347-L354) — `GetCacheInfoFlag`/`ClearReqFlag`：标志位就是请求缓冲区（`info_.local_req_flag_ptr`，u6-l2 的 `SetInfo` 布置的 receive area）首字节，清零是普通写。

- [src/llm_datadist/link_mgr/comm_entity.cc:L370-L379](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/comm_entity.cc#L370-L379) — 注释里画出了 receive area / send area 的内存布局：`flag + content` 两段式，请求与响应各一块。

#### 4.3.4 代码实践

**实践目标**：通过日志观察状态迁移节奏（RECEIVE 停留多久、何时进 SEND）。

**操作步骤**：

1. 阅读日志宏 `LLMLOGD` 的级别归属（`src/llm_datadist/common/llm_log.h`），确认需要 DEBUG 级日志开关。
2. 运行 switch_roles 样例（见 4.5 节步骤），开启 DEBUG 日志后过滤关键字 `Enter receive state` / `Enter idle state`。

**需要观察的现象**：`Enter idle state` 与 `Enter receive state` 成对高频出现（FSM 线程每轮穿过 IDLE 进 RECEIVE）；`Finish receive state`（即迁入 SEND）只在 client 发起 Pull 时出现。

**预期结果**：日志频率印证「忙轮询 + 事件驱动迁移」模型。若无硬件环境，**待本地验证**；可替代为源码推演：数一遍 `Enter receive state` 与 `Finish receive state` 的比值即轮空次数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ReceiveState 里 `ClearReqFlag()` 要在迁移到 SEND **之前**执行？

**答案**：防止重复消费。若先迁移到 SEND 再清标志，FSM 线程下一轮推进时（SEND.Process 返回未完成、实体仍在处理中）虽然不会回 RECEIVE，但一旦处理完成回到 RECEIVE，残留的标志位 1 会让状态机把**同一个请求再消费一次**。先清零保证「标志位为 1」这个事件与「进入 SEND 处理」严格一一对应。

**练习 2**：IdleState 看起来是空操作，删掉它、让 SEND 直接迁回 RECEIVE 行不行？

**答案**：功能上大概率可行（RECEIVE 本身也是等待态），但会丢失三个东西：①请求间隙的明确边界（观测/统计上无法区分「处理中」与「处理完待命」）；②`MarkEntityIdle()` 语义落点——外部代码可以把实体显式置回 IDLE 表示「复位到干净的等待起点」；③扩展点——若未来需要在请求间隙做资源回收（如释放 job 缓存），IDLE 是天然挂载点。SEND.Postprocess 里 `SetDataTransferJob(nullptr)` 已经在做类似的清理，IDLE 为这类动作留了位置。

---

### 4.4 SendState：请求解析与 Job 编排

#### 4.4.1 概念说明

SendState 是三态中唯一「干活」的状态，也是 u6-l5 DataTransfer Job 体系的服务端驱动源。它的职责按时间顺序分三段：

1. **入态（Preprocess → Prepare）**：从实体取出 client 写入的 `TransferCacheReq`，设置超时点，查询本端 CacheEntry（按 cache_key 或 cache_id 两条路径），校验参数，按「源 placement × 目的 placement」解析传输类型（D2D/D2H/H2D），创建对应的 `DataTransferJob` 并 `Initialize`。任何一步失败都**不进 PROCESS**，直接 `SendResponse(错误码)` 把结果写回 client，然后回 IDLE——保证 client 一定能等到应答，不会挂死在响应标志位轮询上。
2. **推进（Process，被 FSM 线程反复调用）**：先查超时（超时同样回错误码并回 IDLE），再调用 `job->Process(is_done)` 非阻塞推一小步（u6-l5 讲过的「分批执行」就在这里被逐批驱动）。`is_done == true` 时，若该请求对应需要回收的 cache_key（u6-l3 讲过的延迟释放），顺手 `RemoveCacheKey`，然后回 IDLE。
3. **出态（Postprocess）**：把实体的 job 指针清空，迁回 IDLE。

一个值得注意的细节：`Preprocess` 失败路径里 `SendResponse(ret)` 之后直接 `Postprocess`，**跳过了 Process**——这就是「入态失败即出态」的短路设计。

#### 4.4.2 核心流程

```text
SEND.Preprocess = Prepare(entity):
  1. timeout = request.timeout_in_ms > 0 ? request.timeout_in_ms : 1800s
     entity.SetTimeoutPoint(now + timeout)
  2. QueryCacheEntryAndOffset:
       is_pull_block == 1 → 按 (req_id, model_id) 查 Blocks Cache，offset = 0
       否则:
         cache_id ≥ 0 且能映射到 CacheKey → 按 cache_key 查（记录待回收 key）
         否则                             → 按 cache_id 查，offset = batch_index * stride
       查不到 → LLM_KV_CACHE_NOT_EXIST
  3. CheckParam: num_tensors 匹配、block/非 block 一致、pull_size ≤ stride、层区间合法
  4. ResolveTransferType:
       src=DEVICE, dst=DEVICE → D2D
       src=DEVICE, dst=HOST   → D2H
       src=HOST,   dst=DEVICE → H2D
       其他（含 HOST→HOST）   → -1 → LLM_FEATURE_NOT_ENABLED
  5. ValidateTransferRequest: dst_addr_count / transfer_info_count 上限、buffer_len > 0
  6. 按 transfer_type 创建 D2D/D2H/H2D DataTransferJob 并 Initialize

SEND.Process（每轮推进）:
  now > TimeoutPoint → SendResponse(LLM_TIMEOUT) → 回 IDLE
  job->Process(is_done):
    is_done == false → return（停在 SEND 等下一轮）
    is_done == true  → （若有待回收 key → RemoveCacheKey）→ 回 IDLE
```

其中步骤 2 的 offset 计算 \( \text{offset} = \text{batch\_index} \times \text{stride} \) 就是 u6-l3「batch 内定长切分」的体现：cache 按 batch 组织，每个 batch 条目等长（stride），batch_index 直接换算成字节偏移。

#### 4.4.3 源码精读

- [src/llm_datadist/fsm/send_state.cc:L74-L81](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L74-L81) — `SendState::Preprocess`：`Prepare` 失败时 `SendResponse(ret)` 后直接 `Postprocess`，短路出态；成功才进 `Process`。

- [src/llm_datadist/fsm/send_state.cc:L83-L113](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L83-L113) — `Prepare` 主流程：超时点设置（默认 1800 秒）、查 Cache、参数检查、传输类型解析、请求上限校验、按类型三选一创建 Job（`D2HDataTransferJob` / `H2DDataTransferJob` / `D2DDataTransferJob`）并 `Initialize`。这正是 u6-l5 讲过的「SendState 按 源 placement × 目的 placement 三分派创建 job」的现场。

- [src/llm_datadist/fsm/send_state.cc:L115-L132](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L115-L132) — `SendState::Process`：超时检查、`job->Process(is_done)` 非阻塞推进、完成后按 `GetCacheKeyToRemove()` 决定是否 `RemoveCacheKey`（`{UINT64_MAX, UINT64_MAX}` 是「无需回收」哨兵值）。

- [src/llm_datadist/fsm/send_state.cc:L134-L137](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L134-L137) — `Postprocess`：`SetDataTransferJob(nullptr)` 释放 job，迁回 IDLE，循环闭合。

- [src/llm_datadist/fsm/send_state.cc:L139-L173](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L139-L173) — `QueryCacheEntryAndOffset`：三条查表路径（Blocks / cache_key / cache_id）与 offset 计算；`is_pull_block`、`is_prefix`、`is_owned` 共同决定是否登记待回收 cache_key（对应 u6-l3 的延迟释放：Pull 完成后才真正 Remove）。

- [src/llm_datadist/fsm/send_state.cc:L207-L220](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L207-L220) — `ResolveTransferType`：placement 组合到 D2D/D2H/H2D 的映射，HOST→HOST 返回 -1（不支持）。

- [src/llm_datadist/fsm/send_state.cc:L49-L72](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/fsm/send_state.cc#L49-L72) — `ValidateTransferRequest`：对 `dst_addr_count`、`transfer_info_count` 做上限校验。注意 L64-L69 的大段注释解释了**为何故意不校验 `req_size` 相等**：内存安全不依赖线上传来的 req_size，而是由上面两个计数上限保证所有 `transfer_infos[]` 访问都落在固定缓冲区内——这是服务端解析不可信输入时「以本地校验为准」的安全范式，值得一读。

#### 4.4.4 代码实践

**实践目标**：追踪一次 PullKvBlocks 在 server 侧 FSM 中的完整路径，标出每个错误码的触发点。

**操作步骤**（源码阅读型实践）：

1. 从 [data_transfer_client.cc:L165-L181](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/data_transfer/data_transfer_client.cc#L165-L181) `SendCacheInfoToRemote` 出发，确认请求经 `CommEntity::SendRequest` 单边 PUT 到 server。
2. 沿 `ReceiveState::Process → SendState::Preprocess(Prepare)` 顺序，把 `Prepare` 中每个 `LLM_CHK_*_RET_STATUS` 可能返回的错误码（`LLM_KV_CACHE_NOT_EXIST`、`LLM_PARAM_INVALID`、`LLM_FEATURE_NOT_ENABLED`…）列成表。
3. 对每个错误码，追 `SendResponse(ret)` → `CommEntity::SendResponse`（单边写回 client 响应区）→ client 侧 `SynchronizeStreamTask` 拿到 flag → `GetResponseInfo` 把 ret_code 透出为 Pull 接口返回值。

**需要观察的现象**：server 侧任何失败最终都变成 client `PullKvBlocks` 的同步返回码，client 不会无限等待（有超时兜底）。

**预期结果**：得到一张「错误码 → 触发条件 → client 可见性」三列表。全部结论可由源码静态推出，无需硬件；若要运行验证，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`SendState::Process` 里超时检查为什么放在 `job->Process` 之前而不是之后？

**答案**：超时的语义是「整个请求的总时限已到，不应再投入任何执行」。若放在之后，即使已超时也会先把本轮 job 推完再判超时，浪费计算且拉长 client 等待；放在之前可以让已超时的请求立刻走 `SendResponse(LLM_TIMEOUT)` + 回 IDLE 的收尾路径，尽快释放实体。超时点在 `Prepare` 里一次性设定（`steady_clock` 单调时钟，不受系统改时间影响）。

**练习 2**：`Postprocess` 里的 `SetDataTransferJob(nullptr)` 如果漏掉会怎样？

**答案**：job 对象会滞留在实体上。下一次请求进 SEND 时 `Prepare` 又会 `SetDataTransferJob(MakeUnique<...>())` 直接覆盖旧指针——若是指针赋值语义则会泄漏/悬空（取决于 setter 实现），且滞留的 job 可能还持有 stream/event 等设备资源。清空它保证 IDLE 态的实体不携带任何请求相关资源，「等待下一个请求」的状态干净。

**练习 3**：为什么 SendState 的方法可以是 `static` 的？

**答案**：因为所有状态（请求、cache 查询结果、job、超时点、待回收 key）都在 `CommEntity` 上，SendState 自身无需任何成员。static 也从编译器层面强制了「不得给状态类加成员」这一契约（4.1.4 的结论）。

---

### 4.5 角色切换：switch_roles 样例与 FSM 的关系

#### 4.5.1 概念说明

u6-l1 曾确立两个事实：`LlmRole`（kPrompt/kDecoder/kMix）只是**业务标签**，不影响传输；真正的 Server/Client 由是否配置 `OPTION_LISTEN_IP_INFO` 决定。本节把角色切换与 FSM 串起来，澄清一个常见误解：

**切换角色 ≠ 切换状态机。** FSM 的三态循环挂在 CommEntity 上、由 `ge_llm_fsm` 线程驱动，与 `LlmRole` 无关。`SetRole` 真正做的事情在链路管理层——**停/启监听 daemon**：

- 切换时若带 `OPTION_LISTEN_IP_INFO`，等于宣告「我以后要做 server」，会启动（或改端口重启）监听 daemon；
- 切换时不带该选项，等于宣告「我以后只做 client」，会关掉已有 daemon；
- 前提是**当前没有任何链路**（`GetLinkSize() == 0`），否则返回 `LLM_EXIST_LINK`——因为daemon 停了，挂在链路上的 CommEntity 及其 FSM 就没了请求来源。

所以在 switch_roles 样例里，decoder 之所以能从「拉数据的一方」变成「被推数据的一方」，不是因为它的 FSM 变了，而是：**它 Unlink 掉旧链路（旧实体销毁）→ SetRole(kPrompt, 监听 26001) 启动监听 → 对端 prompt 侧 SetRole(kDecoder) 关掉监听、主动 Link 过来 → 新链路握手创建新的 CommEntity → 这个新实体上跑起同样的 IDLE→RECEIVE→SEND 循环，只不过这次它处理的是 Push 请求。** 状态机是同一个，换的是实体和方向。

另外注意 HIXL 后端的差异：`HixlTransferEngine::SwitchRole` 不管理 daemon，只校验「传入的监听信息与 Initialize 时一致」，是个轻实现；daemon 停启逻辑在默认 HCCL 后端（`CommTransferEngine` → `LLMLinkManager::SwitchRole`）里。本讲样例默认走 HCCL 后端。

#### 4.5.2 核心流程

```text
SetRole 调用链（HCCL 后端）:
  LlmDataDist::SetRole(role, options)                      [api 层外壳]
    └─ LlmDataDistImpl::SetRole: 解析 OPTION_LISTEN_IP_INFO → ip/port
        └─ LLMDataDistV2::SwitchRole: 恢复 ACL context
            └─ CommTransferEngine::SwitchRole
                └─ LLMLinkManager::SwitchRole:
                     ① GetLinkSize() != 0 → LLM_EXIST_LINK（必须先 Unlink）
                     ② 带监听选项: 端口变化则 StopDaemon + StartDaemon(新端口)
                                   端口相同则什么都不做
                     ③ 不带监听选项: StopDaemon（转纯 client）

switch_roles 样例时序（decoder 视角）:
  t0: SetRole(隐式, 监听 26001)… 实际 decoder 构造时角色 kDecoder
  t1: Link(对端 prompt:26000) → 实体A 创建，prompt 侧 FSM 处理 decoder 的 Pull
  t2: PullKvBlocks/PullKvCache  → 本地是 client，走 DataTransferClient
  t3: Unlink                    → 实体A 销毁（FSM_DESTROYED → 摘除）
  t4: SetRole(kPrompt, 监听 26001) → daemon 已在该端口，无需重启
  t5: 对端 prompt 切 kDecoder 后 Link 过来 → 实体B 创建
  t6: prompt PushKvBlocks/PushKvCache → 实体B 的 FSM 走 RECEIVE→SEND 处理请求
```

#### 4.5.3 源码精读

- [src/llm_datadist/api/llm_datadist_impl.cc:L280-L304](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L280-L304) — 公开外壳 `LlmDataDistImpl::SetRole`：校验已初始化、角色合法，把 `OPTION_LISTEN_IP_INFO` 拆成 ip/port 下沉 `SwitchRole`，成功后更新本地 `role_` 标签。

- [src/llm_datadist/llm_datadist_v2.cc:L366-L372](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/llm_datadist_v2.cc#L366-L372) — V2 层：`TemporaryRtContext` 恢复初始化时捕获的 ACL context（与 u3-l4 ConnectPoolExecutor 的做法同源），再转给 transfer_engine。

- [src/llm_datadist/link_mgr/llm_link_manager.cc:L101-L138](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc#L101-L138) — `LLMLinkManager::SwitchRole` 的全部逻辑：第 103 行就是「有链路不许切」的 `LLM_EXIST_LINK` 检查；随后按是否带监听选项走 StopDaemon/StartDaemon 分支；注意 `(void)role;`——**角色字符串被显式忽略**，再次印证 LlmRole 只是业务标签。

- [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:L246-L261](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L246-L261) — HIXL 后端的 `SwitchRole`：仅校验监听信息与初始化一致，否则 `LLM_FEATURE_NOT_ENABLED`；不管理 daemon。

- [examples/cpp/decoder_switch_roles.cpp:L251-L262](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_switch_roles.cpp#L251-L262) — decoder 侧关键序列：**先 Unlink（第 6 步）再 SetRole(kPrompt)（第 7 步）**——正是 `LLM_EXIST_LINK` 检查要求的顺序；随后 sleep 等待对端 Push 并校验 buffers 中出现新数据 `{4,5,6,7}`。

- [examples/cpp/decoder_switch_roles.cpp:L64-L74](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_switch_roles.cpp#L64-L74) — decoder 的 `SetRole` 封装：带 `OPTION_LISTEN_IP_INFO`（本机 IP:26001），即宣告监听。

- [examples/cpp/prompt_switch_roles.cpp:L64-L72](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_switch_roles.cpp#L64-L72) 与 [L236](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_switch_roles.cpp#L236) — prompt 侧对称操作：Unlink 后 `SetRole(kDecoder)`（不带监听信息，关 daemon 转纯 client），再主动 Link 到 decoder 的 26001 端口并发起第二轮 Push。

#### 4.5.4 代码实践

**实践目标**：画出角色切换前后两个实例的状态机迁移图（本讲综合实践的预热版）。

**操作步骤**：

1. 通读 `decoder_switch_roles.cpp` 的 `RunDecoderSample`（[L213-L277](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_switch_roles.cpp#L213-L277)）与 `prompt_switch_roles.cpp` 的对应主流程，把两个进程的每一步（Initialize/Register/Link/Pull/Unlink/SetRole/Push/Check）按时间排成一列。
2. 为每一步标注：此刻本进程有没有 CommEntity？实体的 `cur_state_` 处于什么状态？谁在驱动它？
3. 用 mermaid 或纸笔分别画出「第一轮（decoder 拉）」和「第二轮（decoder 被推）」中 **server 端实体** 的状态迁移：`INIT → IDLE → RECEIVE →（收到 Pull 请求）SEND →（job 分批执行，多次停留）→ IDLE → …`。

**需要观察的现象**：两个实例用**同一个**状态机，只是 server 角色从 prompt 侧换到了 decoder 侧；实体随链路创建/销毁，状态机不随之销毁（状态对象是进程级单例）。

**预期结果**：一张双泳道时序图 + 两张状态迁移图。切换失败分支也画出来：若去掉第 6 步 Unlink 直接 SetRole，应得到 `LLM_EXIST_LINK`（源码 [llm_link_manager.cc:L103](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/link_mgr/llm_link_manager.cc#L103)）。运行验证依赖双机昇腾环境，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：如果不调用 `UnlinkLlmClusters` 直接 `SetRole`，会发生什么？

**答案**：返回 `LLM_EXIST_LINK`（0x5010Bxxx 段错误码），角色不切换、daemon 不动。设计原因：监听 daemon 是链路的接纳入口，链路还活着时停 daemon 会让对端的 CommEntity 失去控制面来源，正在跑的 FSM 请求也无法收尾。所以必须先断链（实体销毁干净）再切换。

**练习 2**：切换到 kPrompt 但**不带** `OPTION_LISTEN_IP_INFO`，实例会变成什么？

**答案**：一个挂着 Prompt 业务标签的**纯 client**。daemon 会被关掉（`llm_link_manager.cc` 的 else 分支 StopDaemon），它可以主动 Link 别人并 Push，但没人能 Link 进来。业务标签 kPrompt/kDecoder 只影响上层框架（如 vLLM 的调度）怎么用它，不影响 LLM-DataDist 的传输行为。

**练习 3**：为什么 `HixlTransferEngine::SwitchRole` 只做「监听信息一致性」校验，而不像 HCCL 后端那样管理 daemon？

**答案**：两个后端的链路建立机制不同。HCCL 后端由 LLM-DataDist 自己维护监听 daemon 完成握手；HIXL 后端的控制面/数据面由 HIXL Engine 的 `local_engine`（Initialize 时已固定为 ip:port）承担，LLM-DataDist 无 daemon 可管。因此切换角色时唯一要保证的是「用户期望的监听点与已初始化的 HIXL engine 一致」，不一致说明用户想换端口——这需要重新 Initialize，故返回 `LLM_FEATURE_NOT_ENABLED`。

## 5. 综合实践

**任务：跑通 switch_roles 双进程样例，产出「角色切换 × 状态机迁移」全景图。**

前置：两台互通的昇腾机器（或单机双卡互通），CANN 环境已加载，已按 u1-l2 完成 `bash build.sh --examples` 构建。

1. **启动 decoder**（机器 A）：
   `./hixl_example_decoder_switch_roles <device_id> <A的IP> <B的IP>`
   它会：Initialize → RegisterKvCache → Link 到 prompt:26000 → PullKvBlocks/PullKvCache → 校验 `{0,1,2,3}` → Unlink → SetRole(kPrompt, 监听 26001) → 等待被推 → 校验 `{4,5,6,7}`。
2. **启动 prompt**（机器 B）：按 `prompt_switch_roles.cpp` 的主流程，先作为 Prompt 注册并接受第一轮 Pull，然后 Unlink → SetRole(kDecoder) → Link 到 A:26001 → 第二轮 PushKvBlocks/PushKvCache。
3. **观测**：
   - `ps -T` 确认两进程各自的 `ge_llm_fsm` 线程；
   - 过滤日志中 `Enter idle state` / `Finish receive state` / `Query cache entry success` / `transfer type =` 等关键字，对齐两轮传输的时间线；
   - 统计第一轮与第二轮中 `Finish receive state`（进 SEND）出现的次数，应与 Pull/Push 的请求下发次数对应。
4. **产出物**（本实践的核心交付）：
   - 双泳道时序图（decoder/prompt 两条生命线，标出每次实体创建与销毁）；
   - 两张状态迁移图：第一轮 server（prompt 侧实体A）与第二轮 server（decoder 侧实体B）的 `INIT → IDLE ⇄ RECEIVE → SEND → IDLE` 循环，并标注 SEND 态内 job 分批推进的停留次数；
   - 一份对照表：角色切换改变了什么（daemon 归属、实体归属、请求方向）/ 没改变什么（状态机代码、ge_llm_fsm 线程、三态循环本身）。
5. **附加实验**（可选）：把 decoder 主流程中第 6 步 Unlink 注释掉重跑，记录 `SetRole` 的返回码，验证 `LLM_EXIST_LINK`；改完记得还原（不要把修改提交进仓库）。

若无硬件环境，整个实践退化为「源码推演版」：以上所有图和表都可以仅凭本讲引用的源码静态画出，运行结论标注**待本地验证**。

## 6. 本讲小结

- LLM-DataDist 的 FSM 是**服务端请求处理循环**：`IDLE → RECEIVE → SEND → IDLE`，每个 CommEntity（每条链路）独立一份状态，由进程内唯一的 `ge_llm_fsm` 线程忙轮询驱动，`try_lock` 保证与用户 Pull 调用互斥。
- 三个状态对象（`IdleState`/`ReceiveState`/`SendState`）**完全无状态**，由 `StateManager` 单例注册共享；可变数据全部住在 `CommEntity` 上（原子变量 `cur_state_` + 请求缓冲区 + job 指针 + 超时点）。
- `ChangeState` 的语义是「迁移即入态」（写状态后立刻调新状态 `Preprocess`），轻状态一次调用链即可穿过多态；`SendState::Process` 因 job 未完成会提前返回，把长传输切成小步逐轮推进——这就是 u6-l5 Job 体系「周期性非阻塞驱动」的驱动源。
- 请求通知靠 **1 字节标志位 + 单边 PUT**：client 先写请求体再置标志，server 的 RECEIVE 轮询到 1 即进 SEND；响应方向对称。没有任何专用网络协议参与数据面请求分发。
- `INIT/ERROR/DESTROYED` 是没有状态对象的标记态：INIT 等内存就绪、ERROR 冻结实体直到断链重建、DESTROYED 触发台账摘除。
- **角色切换不换状态机**：`SetRole` 实际管理的是监听 daemon（HCCL 后端）或校验监听一致性（HIXL 后端），必须先 Unlink（`LLM_EXIST_LINK`）；switch_roles 样例的本质是销毁旧实体、在对端新建实体，让同一个三态循环换个方向再跑一轮。

## 7. 下一步学习建议

- **u6-l7（传输后端：TransferEngine 与 HIXL 适配）**：本讲已多次出现 `transfer_engine_->SwitchRole` 的分叉，下一讲系统讲 `TransferEngine` 抽象、`CommAdapter` 桥接与 HIXL 后端的完整适配层。
- **延伸阅读源码**：`src/llm_datadist/link_mgr/comm_entity.cc` 的 `SetInfo`/`SendRequest`/`SendResponse`（标志位协议全貌）；`src/llm_datadist/data_transfer/data_transfer_client.cc`（client 侧对偶状态：构造请求 → 单边 PUT → 轮询响应）；`tests/cpp/llm_datadist/` 下与 cache/transfer 相关的单测（看测试如何在不连真机的情况下驱动 FSM 路径）。
- **带着问题读**：FSM 线程是忙轮询（无 sleep），思考在多实体、低请求频率的部署下这对 CPU 的代价，以及若要改成事件驱动（如中断/notify）需要改动哪些点——这会把你对本讲的理解串成一条线。
