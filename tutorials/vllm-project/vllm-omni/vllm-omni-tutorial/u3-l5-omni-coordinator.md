# OmniCoordinator：副本注册与负载均衡

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **OmniCoordinator** 在 vLLM-Omni 分布式运行时里扮演的「中心协调者」角色，以及它用 ZMQ `ROUTER`/`PUB` 两条通道分别对接谁。
- 描述副本（replica）从注册、心跳、超时降级、到优雅下线的完整生命周期，以及 OmniCoordinator 如何把「当前哪些副本可用」广播出去。
- 掌握三种 `LoadBalancingPolicy`（`RANDOM` / `ROUND_ROBIN` / `LEAST_QUEUE_LENGTH`）的选择逻辑，并能判断一个具体场景下会被路由到哪个副本。
- 读懂 `ReplicaStatus` / `ReplicaEvent` / `ReplicaInfo` / `ReplicaList` 这组 wire protocol（线路协议）数据结构，以及 `OmniCoordClientForStage`（stage 端）与 `OmniCoordClientForHub`（hub 端）两类客户端如何协同。
- 理解 `run_omni_coordinator_proc` 与 `OmniCoordinatorRuntime` 如何把协调者包装成独立子进程，以及它为什么必须独立成进程。

本讲是 U3（多阶段运行时与编排）的收尾，承接 [u3-l4 OmniConnector 体系](u3-l4-omni-connectors.md)：如果说 OmniConnector 是在 **stage 之间搬运重数据（张量/KV）的数据面**，那么 OmniCoordinator 就是 **告诉 Orchestrator「每个 stage 当前有哪些副本、谁比较闲」的控制面**。

## 2. 前置知识

- **stage 与 replica**：在 vLLM-Omni 里，一个多阶段模型（如 Qwen3-Omni 的 Thinker / Talker / Code2wav）被拆成多个 `stage`；当某个 stage 需要横向扩展时，它可以有多个**副本（replica）**，每个副本是独立的 EngineCore（AR）或 Diffusion 子进程。详见 [u3-l1](u3-l1-async-omni-architecture.md)、[u3-l3](u3-l3-stage-process-runtime.md)。
- **单机模式 vs 分布式模式**：单机时所有副本都由本进程的 `StagePool` 用静态客户端直接管理（`StageRuntime`）；分布式时副本可能分布在多台机器上，需要中心协调者来发现与负载均衡（`DistStageRuntime`）。
- **ZMQ 套接字模型**：本讲用到四种 ZMQ 套接字。
  - `ROUTER`/`DEALER`：异步请求-回复对。`ROUTER`（服务端）能给每个连上来的 `DEALER`（客户端）打身份标签，从而区分消息来自哪个副本。
  - `PUB`/`SUB`：广播订阅对。`PUB` 单向广播，所有 `SUB` 都能收到；但 ZMQ 的 PUB 是「慢加入者（slow joiner）」——后连接的 SUB 收不到它连接之前的消息。
- **数据面 vs 控制面**：这是一个借自网络的术语。**数据面**搬运真正影响推理结果的「重」数据（prompt embedding、KV cache、生成结果）；**控制面**只搬运「轻」的元数据（谁在线、队列多长、要不要重新路由）。本项目的数据面是 OmniConnector（[u3-l4](u3-l4-omni-connectors.md)），控制面就是本讲的 OmniCoordinator。

## 3. 本讲源码地图

本讲涉及的关键源码全部位于 `vllm_omni/distributed/omni_coordinator/` 目录下，外加两处把它们接入运行时的「胶水」代码：

| 文件 | 作用 |
| --- | --- |
| `vllm_omni/distributed/omni_coordinator/messages.py` | wire protocol 数据结构：`ReplicaStatus`（状态枚举）、`ReplicaEvent`（上行事件）、`ReplicaInfo`（副本元信息）、`ReplicaList`（下行广播）。 |
| `vllm_omni/distributed/omni_coordinator/omni_coordinator.py` | 核心类 `OmniCoordinator`：维护副本注册表、跑两个后台线程（收事件 / 周期检查心跳并广播）。 |
| `vllm_omni/distributed/omni_coordinator/load_balancer.py` | 负载均衡抽象 `LoadBalancer` 及三种实现（`RandomBalancer`/`RoundRobinBalancer`/`LeastQueueLengthBalancer`）与策略枚举。 |
| `vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py` | stage 端客户端 `OmniCoordClientForStage`（DEALER）：副本向协调者注册、发心跳、上报队列长度、下线。 |
| `vllm_omni/distributed/omni_coordinator/omni_coord_client_for_hub.py` | hub 端客户端 `OmniCoordClientForHub`（SUB）：订阅广播、缓存最新副本列表，供 `StagePool` 路由查询。 |
| `vllm_omni/distributed/omni_coordinator/runtime.py` | 进程生命周期包装：`run_omni_coordinator_proc`（子进程入口）+ `OmniCoordinatorRuntime`（父进程侧的进程管理者）。 |
| `vllm_omni/engine/membership_controller.py` | 把 hub 客户端 + 负载均衡器注入每个 `StagePool`，并监视副本消失（接入点）。 |
| `vllm_omni/engine/stage_pool.py` | `StagePool.pick` 在分布式模式下调用 `LoadBalancer.select` 选副本（消费点）。 |
| `vllm_omni/engine/stage_runtime.py` | `_build_load_balancer_factory`（策略字符串→负载均衡器类）与 `_start_omni_master_server`（启动协调者进程）。 |

---

## 4. 核心概念与源码讲解

### 4.1 分布式协调的动机与 wire protocol 数据结构

#### 4.1.1 概念说明

在 [u3-l3](u3-l3-stage-process-runtime.md) 中我们建立了「每个 stage 是一个可寻址、可通信的子进程」。但那是在**单机、副本数固定**的前提下：`StagePool` 启动时就握有所有副本的客户端句柄，路由时只需在固定的 `replica_id` 列表里轮询。

一旦进入分布式场景，会冒出三个新问题：

1. **发现（discovery）**：head 节点上的 Orchestrator 怎么知道 stage1 在 worker 节点 B 上又新起了一个副本？副本地址是动态的（端口甚至可能 `port=0` 自动分配）。
2. **健康（liveness）**：某个副本所在机器宕机了，head 怎么及时知道、不再往它投请求？
3. **负载（load）**：stage1 有 3 个副本，一个队列已经堆积了 50 个请求，另外两个空闲——head 怎么把新请求优先发给空闲的副本？

这三个问题需要**一个所有副本都认识的中心点**来汇总信息、广播视图。这就是 `OmniCoordinator`。它解决的是「**副本的成员关系（membership）与负载视图（load view）**」，完全是轻量元数据，**与真正搬张量的 OmniConnector 数据面相互独立**。

为了让协调者、stage 客户端、hub 客户端三方用同一种「语言」交流，先约定一组 wire protocol（线路协议）数据结构。它们定义在 `messages.py` 里，是本讲后续所有逻辑的基础。

#### 4.1.2 核心流程

四种数据结构按「上行 / 下行」分为两组：

```text
上行（stage → coordinator，经 ROUTER）        下行（coordinator → hub，经 PUB）
┌──────────────┐                              ┌──────────────┐
│ ReplicaEvent │  注册/心跳/更新/下线的事件      │ ReplicaList  │ 当前所有副本的快照
│  - status    │  ───────────────────────────► │  - replicas  │ ──────► hub 缓存
│  - queue_len │                               │  - timestamp │
└──────────────┘                               └──────────────┘
        │                                             ▲
        │ 内部存储                                     │ 元素类型
        ▼                                             │
┌──────────────┐                                     │
│ ReplicaInfo  │  ─── 注册表里每条记录的完整字段 ──────┘
│  + last_heartbeat / registered_at
└──────────────┘

状态语义（ReplicaStatus）：
  UP    可用      → 出现在下行的 active 列表里
  DOWN  优雅下线  → 不再出现在 active 列表
  ERROR 故障/超时 → 不再出现在 active 列表
```

关键点：`ReplicaEvent` 是**事件流**（「我刚上线」「我还活着，队列长度=3」），`ReplicaInfo` 是**注册表里的一条记录**（带 `last_heartbeat`、`registered_at` 等协调者内部才用的时间戳），`ReplicaList` 是协调者向下游广播的**全量快照**（只含 UP 副本）。

#### 4.1.3 源码精读

先看状态枚举。`ReplicaStatus` 是一个字符串枚举，三态语义清晰：

[ReplicaStatus 枚举](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/messages.py#L10-L15) — 定义副本的三种状态：`UP`（可用）、`DOWN`（优雅关闭）、`ERROR`（出错或心跳超时）。继承 `str` 是为了 JSON 序列化后仍是可读字符串。

`ReplicaEvent` 是 stage 发给协调者的上行事件：

[ReplicaEvent 上行事件](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/messages.py#L18-L31) — 关键字段：`event_type` 取值 `"update"`（注册/状态变化）或 `"heartbeat"`（周期心跳）；`queue_length` 是负载均衡最关心的实时负载指标。

`ReplicaInfo` 比 `ReplicaEvent` 多了两个时间戳，因为协调者内部要据此判断心跳是否超时：

[ReplicaInfo 注册表记录](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/messages.py#L34-L48) — 注意 `last_heartbeat`（最后心跳时间）和 `registered_at`（注册时间），它们是协调者判定副本存活、决定何时 GC 的依据；这两个字段会随 `ReplicaList` 一起广播给 hub。

最后是下行的广播容器：

[ReplicaList 下行快照](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/messages.py#L51-L61) — 每次副本视图变化，协调者就把当前所有 UP 副本打包成 `ReplicaList` 广播；`timestamp` 让 hub 能判断缓存的新旧。

#### 4.1.4 代码实践

**实践目标**：亲手把四种 wire protocol 结构在内存里构造一遍，确认它们能正确往返 JSON（这正是协调者与客户端之间实际做的序列化）。

**操作步骤**（示例代码，不是项目原有文件）：

```python
# 示例代码：在项目根目录运行 python
from dataclasses import asdict
import json
from vllm_omni.distributed.omni_coordinator import (
    ReplicaEvent, ReplicaInfo, ReplicaList, ReplicaStatus,
)

# 1. 构造一个 stage 上报的事件（模拟 DEALER 端发送的内容）
event = ReplicaEvent(
    input_addr="tcp://worker0:10001",
    output_addr="tcp://worker0:10002",
    stage_id=1,
    event_type="update",
    status=ReplicaStatus.UP,
    queue_length=3,
)
wire = json.dumps(asdict(event)).encode("utf-8")   # 这就是 ZMQ 上实际传输的字节
print("上行 wire bytes:", wire)

# 2. 构造协调者内部注册表里的一条记录（比 event 多两个时间戳）
info = ReplicaInfo(
    input_addr=event.input_addr, output_addr=event.output_addr,
    stage_id=event.stage_id, status=event.status, queue_length=event.queue_length,
    last_heartbeat=0.0, registered_at=0.0,
)

# 3. 构造下行广播的全量快照（只含 UP 副本）
snapshot = ReplicaList(replicas=[info], timestamp=0.0)
print("下行 snapshot:", json.dumps(asdict(snapshot)))
```

**需要观察的现象**：上行 `event` 序列化后不包含 `last_heartbeat`/`registered_at`（这是协调者内部的字段，stage 不需要也不应该上报）；而下行的 `ReplicaList` 反而把这两个字段也带上了（因为 hub 缓存时复用同一个 `ReplicaInfo` 结构）。

**预期结果**：上行 JSON 的键是 `input_addr/output_addr/stage_id/event_type/status/queue_length` 六个；下行 JSON 多出 `last_heartbeat/registered_at`，且没有 `event_type`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ReplicaEvent` 不需要 `last_heartbeat` 字段，而 `ReplicaInfo` 需要？

**参考答案**：`last_heartbeat` 是协调者**接收**事件时盖上「当前时间」得到的，是服务端的账本字段，客户端自己上报毫无意义（客户端时钟可能与服务端不同步）。所以它只存在于协调者内部注册表（`ReplicaInfo`），不作为上行字段（`ReplicaEvent`）的一部分。

**练习 2**：`ReplicaStatus` 为什么要继承 `str`（`class ReplicaStatus(str, Enum)`）而不是普通 `Enum`？

**参考答案**：wire protocol 用 JSON 序列化。普通 `Enum` 经 `json.dumps` 会变成 `"ReplicaStatus.UP"` 这种带类名的难看字符串；继承 `str` 后枚举值本身就是 `"up"`/`"down"`/`"error"`，`json.dumps` 直接得到干净字符串，对端用 `ReplicaStatus(s)` 也能反序列化回来。

---

### 4.2 OmniCoordinator：副本注册表、心跳与双通道发布

#### 4.2.1 概念说明

`OmniCoordinator` 是中心协调者本体。它做三件事：

1. **收**：用 `ROUTER` 套接字接收来自所有 stage 副本（DEALER）的事件（注册、心跳、更新、下线）。
2. **管**：维护一张内存注册表 `dict[str, ReplicaInfo]`，键是副本的 `input_addr`；周期性检查心跳，把超时副本降级为 `ERROR`。
3. **发**：用 `PUB` 套接字把「当前 UP 副本列表」广播给所有 hub（SUB）。

它和 [u3-l2](u3-l2-orchestrator.md) 的 Orchestrator 一样跑后台线程，但定位完全不同：Orchestrator 是**请求编排**（请求怎么在 stage 间流动），OmniCoordinator 是**成员关系管理**（哪些副本可用、谁更闲）。Orchestrator 通过 `StagePool` 间接消费 OmniCoordinator 的视图来决定「这一跳发给哪个副本」。

#### 4.2.2 核心流程

`OmniCoordinator` 启动后开**两个守护线程**，各司其职：

```text
┌─────────────────────── OmniCoordinator（中心进程）───────────────────────────┐
│                                                                              │
│  _recv_loop 线程            ROUTER 套接字(收)            _periodic_loop 线程  │
│  ┌──────────────┐  ◄─── DEALER(stage副本) ───┐      ┌─────────────────────┐  │
│  │ recv_multipart│                            │      │ 周期(约 100ms 一跳): │  │
│  │ json 解析     │                            │      │  ① 每 ~5s 查心跳:    │  │
│  │ _handle_event │                            │      │     超时 → ERROR      │  │
│  └──────┬───────┘                             │      │  ② flush 待发广播     │  │
│         │ 写注册表 _replicas (加锁)           │      │     (最多 100ms 一次) │  │
│         │ _schedule_broadcast() 置位          │      └──────────┬──────────┘  │
│         ▼                                     │                 │             │
│  ┌──────────────┐                             │                 ▼             │
│  │ _replicas:   │                             │        publish_replica_list_  │
│  │  addr→Info   │                             │        update()               │
│  └──────────────┘                             │          │ PUB 套接字(发)       │
│                                               │          ▼                     │
│                                               │   ──────► SUB(hub) ── 缓存     │
└───────────────────────────────────────────────┘ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
```

事件处理的核心流程（`_handle_event`）按 `event_type` 分流：

- `heartbeat`：刷新 `last_heartbeat`；若 `queue_length` 变了或副本从 `ERROR` 恢复成 `UP`，才触发广播。
- 非 heartbeat（`update`）：若 `input_addr` 不在注册表 → 新增（注册）；若 `status==DOWN` → 标记下线；否则更新字段。任何状态变化都触发广播。

广播采用**合并（coalesce）策略**：多个事件短时间内涌入时，`_schedule_broadcast()` 只把一个 `_pending_broadcast` 标志位置 `True`，由 `_periodic_loop` 最多每 100ms（`_publish_min_interval`）真正发一次，避免事件风暴压垮 PUB。

心跳检查节奏由 `heartbeat_timeout` 推导：`heartbeat_interval = max(1.0, min(heartbeat_timeout / 2, 5.0))`。默认 `heartbeat_timeout=30` 秒时，每 5 秒检查一次；只要两次检查间隔内收到心跳，副本就被认为存活。

#### 4.2.3 源码精读

构造函数建好两条 ZMQ 通道与两个线程：

[OmniCoordinator.__init__：建立 ROUTER/PUB 双通道](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L32-L79) — `ROUTER` 绑定 `router_zmq_addr` 收事件、`PUB` 绑定 `pub_zmq_addr` 发广播；`getsockopt_string(zmq.LAST_ENDPOINT)` 是为了在 `port=0` 自动分配端口时回填真实地址（见 [L53](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L53) 与 [L59](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L59)）；`zmq.RCVTIMEO=100`（[L73](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L73)）让收循环每 100ms 醒一次以检查 `_running` 标志。

收事件线程只做「解析 + 派发」：

[_recv_loop：收事件并派发](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L216-L242) — `frames[-1]` 取最后一帧（ROUTER 会把发送方身份放在前面帧），`json.loads` 后交给 `_parse_replica_event` 解析；解析失败仅 warning 并丢弃，不让单条坏消息打垮整个循环。

事件派发的心跳分支是负载均衡数据新鲜度的关键：

[_handle_event 的心跳分支](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L299-L314) — 心跳会刷新 `queue_length`（[L306-L308](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L306-L308)），这是 `LeastQueueLengthBalancer` 唯一的周期性实时负载来源；若不在此传播，LEAST_QUEUE_LENGTH 策略只能在陈旧数据上路由。同时若副本原为 `ERROR`，收到心跳即「晋升」回 `UP`（[L309-L311](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L309-L311)），实现自愈。

新增副本的逻辑把 `event` 翻译成注册表记录：

[_add_new_replica_locked：新增副本](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L333-L351) — 校验 `input_addr` 非空、`stage_id >= 0`（失败抛 `KeyError`，会被 `_handle_event` 捕获并丢弃），盖上 `last_heartbeat = registered_at = now` 后写入 `_replicas`。

周期循环把「心跳检查」与「合并广播」合在一个线程里：

[_periodic_loop：心跳检查 + 合并广播](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L244-L286) — 注意 [L254](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L254) 的心跳检查节奏 `max(1.0, min(heartbeat_timeout/2, 5.0))`；[L261-L270](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L261-L270) 每次心跳检查后都强制排一次广播——这是为了「保活广播」，让晚加入的 hub（SUB 慢加入者错过了之前的 PUB）最多等一个心跳周期就能拿到当前副本列表。

心跳超时的判定与垃圾回收：

[_check_heartbeat_timeouts：超时降级与 GC](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L143-L163) — `UP` 副本超过 `heartbeat_timeout` 没心跳 → 标 `ERROR`；`DOWN`/`ERROR` 副本超过 `gc_ttl=600` 秒（10 分钟）仍无动静 → 从注册表物理删除。

广播本身是尽力而为（best-effort）：

[publish_replica_list_update：尽力广播](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L110-L128) — 用 `zmq.NOBLOCK` 发送，PUB 没就绪就静默丢弃本次（下一个心跳周期会补发）；只把 UP 副本打包进 `ReplicaList`（[L84](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L84)）。

#### 4.2.4 代码实践

**实践目标**：阅读测试，理解「副本注册→广播→hub 收到 UP 列表」「心跳超时→降级 ERROR→从 UP 列表消失」两条路径的端到端行为。

**操作步骤**：

1. 打开 `tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py`，它用一个真实的 `PUB` 套接字模拟协调者广播，验证 hub 能缓存副本列表（[test_hub_client_caches_replica_list_from_pub](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py#L35-L93)）。
2. 重点看 [L63-L72](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py#L63-L72)：广播的 3 个副本里有一个 `status="error"`，但 hub 仍收到全部 3 条（协调者广播的是全量视图，**过滤 UP 是消费侧 `get_active_replicas` / `_collect_serviceable_replicas` 的事**——参见本讲 4.4.3）。
3. 运行该测试：
   ```bash
   pytest tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py -v
   ```

**需要观察的现象**：测试里 `client.get_replicas_for_stage(0)` 只返回 `stage_id==0` 的副本——说明 hub 侧的 `get_replicas_for_stage` 做了按 stage 过滤（见 [omni_coord_client_for_hub.py:151-155](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_hub.py#L151-L155)）。

**预期结果**：测试通过，3 个副本被缓存，`get_replicas_for_stage(0)` 返回 2 个、`get_replicas_for_stage(1)` 返回 1 个。

#### 4.2.5 小练习与答案

**练习 1**：`_periodic_loop` 在每次心跳检查后都无条件 `_schedule_broadcast()`，这是不是浪费带宽？

**参考答案**：不是。ZMQ PUB 不缓存历史消息（慢加入者问题）：一个在初始 UP 事件之后才连上的 hub SUB，会**永久错过**之前所有广播，从而永远看不到当前副本列表。心跳检查点上的强制保活广播，把这种「陈旧」上限约束在一个心跳周期（默认 5 秒）内，是必须的兜底，而合并策略已经把实际发包频率压到 ≥100ms 一次。

**练习 2**：副本从 `ERROR` 恢复成 `UP`，是协调者主动探测的，还是被动接收的？

**参考答案**：被动接收。`_handle_event` 的心跳分支在 [L309-L311](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L309-L311) 里检查：只要一个 `ERROR` 副本又发来心跳，就把它晋升回 `UP`。协调者不主动 ping 副本，全靠副本自己周期性上报心跳。

---

### 4.3 LoadBalancer 与 LoadBalancingPolicy：副本选择策略

#### 4.3.1 概念说明

协调者把「当前 stage 有哪些 UP 副本、各自队列多长」广播出去后，**谁来决定新请求发给哪个副本**？答案是 `StagePool`，它持有一个 `LoadBalancer`。负载均衡器是一个极简的纯函数式抽象：给定一个任务和一组候选副本，返回**选中副本在列表里的下标**。

抽象基类 `LoadBalancer` 只有一个方法 `select(task, replicas) -> int`。三种策略各有取舍：

| 策略 | 选择逻辑 | 适用场景 | 依赖实时负载 |
| --- | --- | --- | --- |
| `RANDOM` | 均匀随机选一个 | 默认策略；副本同质、无负载差异时简单有效 | 否 |
| `ROUND_ROBIN` | 维护游标，按顺序轮转 | 希望请求数严格均摊 | 否 |
| `LEAST_QUEUE_LENGTH` | 选 `queue_length` 最小的；并列则随机 | 副本速度不均、想让请求避开拥塞副本 | **是**（依赖心跳上报） |

#### 4.3.2 核心流程

负载均衡发生在 `StagePool.pick` 里，分布式模式下的决策流：

```text
新请求 request_id 到达 StagePool.pick()
   │
   ├─① 粘性（sticky）：_affinity 里已绑过且仍可用？ → 直接返回原副本
   │      （保证同一请求的多跳/多步落在同一副本）
   │
   ├─② 继承亲和（affinity_request_id）：父请求绑过？ → 复用父副本
   │      （如 CFG 伴生请求与父请求共享副本）
   │
   └─③ 全新选择：
        _collect_serviceable_replicas()   # 从 hub 快照筛 UP + 已挂客户端的副本
              │
              ▼
        self._lb.select(task, candidates) # 负载均衡器返回下标
              │
              ▼
        记录 _affinity[request_id] = 选中副本的 input_addr
        返回 replica_id
```

关键细节：

- **粘性路由（sticky / affinity）**：一旦某请求被路由到副本 A，后续对同一 `request_id` 的投递都会粘在 A（[L300-L307](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L300-L307)）。负载均衡只在「首次选择」时起作用。
- **候选集先于策略**：`_collect_serviceable_replicas` 先剔除非 UP、未挂客户端的副本（[L380-L395](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L380-L395)），负载均衡器只在这个**已过滤**的候选集上选择。也就是说「不可用副本」根本到不了负载均衡器。
- **有界等待**：候选集为空时，`pick` 不会立刻失败，而是最多等 `DISPATCH_WAIT_TIMEOUT_S = 10` 秒（每 0.1 秒重试），等 hub 快照里出现可用副本；超时才抛 `RuntimeError`（[L320-L333](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L320-L333)）。

#### 4.3.3 源码精读

策略枚举与抽象基类：

[LoadBalancingPolicy 枚举](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L27-L36) — 三种策略的字符串值 `random`/`round-robin`/`least-queue-length`，即 CLI `--omni-lb-policy` 接受的取值。

[LoadBalancer 抽象基类](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L39-L61) — 契约就是 `select(task, replicas) -> int`，返回下标；空列表抛 `ValueError`。

三种实现，逻辑都很短但各有要点：

[RandomBalancer](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L64-L71) — `random.randrange(len(replicas))`，均匀随机，不看 `task`、不看负载。

[RoundRobinBalancer](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L74-L99) — 维护 `_next_index`，`idx = _next_index % n` 后自增；**关键约束**：它依赖 `replicas` 列表「顺序与长度稳定」，否则会跳过/重复（见类文档 [L77-L80](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L77-L80)）；用 `threading.Lock` 串行化游标更新，保证多线程调用安全。

[LeastQueueLengthBalancer](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L102-L121) — 取 `min(queue_lengths)`，把所有并列最小的下标收集成 `candidates`，再 `random.choice(candidates)` 随机选一个；任何副本 `queue_length < 0` 抛 `ValueError`（防御脏数据）。

策略字符串到负载均衡器类的映射在运行时工厂里：

[_build_load_balancer_factory](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L92-L104) — 先用 `LoadBalancingPolicy(policy)` 校验字符串合法性（非法值列出所有合法取值并抛错），再返回对应的**类**（不是实例），因为这个工厂会被每个 `StagePool` 各调一次以得到独立实例（尤其 `RoundRobinBalancer` 需要各自独立的游标）。

负载均衡器注入与消费的两端：

[membership_controller：注入 hub + 负载均衡器](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/membership_controller.py#L57-L61) — `MembershipController` 在构造时为每个 `StagePool` 挂上同一个共享 `OmniCoordClientForHub`（副本视图来源）和一个**新建**的负载均衡器实例（[L60-L61](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/membership_controller.py#L60-L61)）。

[StagePool.pick 中的 select 调用](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L318-L328) — 全新选择时构造 `Task`，调 `self._lb.select(task, [rep for rep,_ in candidates])` 拿到下标，再写亲和表。

CLI 侧对策略的校验：

[serve.py：--omni-lb-policy 校验](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/cli/serve.py#L155-L164) — 启动时若给了 `--omni-lb-policy`，立刻用 `LoadBalancingPolicy(...)` 校验，非法值在服务起之前就报错退出；默认值是 `"random"`（见 [arg_utils.py:197](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/arg_utils.py#L197)）。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：设计「stage1 有 3 个副本且队列长度不同」的场景，先用推理判断 `LEAST_QUEUE_LENGTH` 会选谁，再用源码与真实测试验证。

**场景设定**：

| 副本下标 | input_addr | queue_length |
| --- | --- | --- |
| 0 | `tcp://host:10001` | 4 |
| 1 | `tcp://host:10002` | 1 |
| 2 | `tcp://host:10003` | 7 |

**先做推理**：`LeastQueueLengthBalancer.select` 会算 `queue_lengths = [4, 1, 7]`，`min_q = 1`，`candidates = [1]`（只有下标 1 的队列等于最小值），`random.choice([1]) = 1`。所以**必然选下标 1**（`tcp://host:10002`）。这里没有随机性，因为最小值唯一。

**操作步骤**：

1. 对照 [load_balancer.py:112-121](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L112-L121) 逐行确认上面的推理。
2. 用真实测试验证。`test_least_queue_length_balancer_picks_min_queue` 正好就是「3 个副本队列长度 2/0/5，应选下标 1」的断言（[test_load_balancer.py:139-173](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_load_balancer.py#L139-L173)）。运行它：
   ```bash
   pytest tests/distributed/omni_coordinator/test_load_balancer.py::test_least_queue_length_balancer_picks_min_queue -v
   ```
3. 再自己造一个并列场景验证随机性。把 3 个副本队列都设成相同值（如 3/3/3），按源码 `candidates=[0,1,2]`，结果应是 `random.choice` 的三者之一。对应的真实测试是 `test_least_queue_length_balancer_equal_queues_uses_choice`（[L181-L217](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_load_balancer.py#L181-L217)），它用 `mocker.patch` 把 `random.choice` 钉死返回 2，断言结果就是 2。

**需要观察的现象**：

- 最小值唯一时，结果**确定**（无随机性）。
- 最小值并列时，结果在并列候选里**随机**（测试必须 mock `random.choice` 才能写出确定断言，否则会 flaky）。
- 任一副本 `queue_length < 0` 会抛 `ValueError`（见 `test_least_queue_length_balancer_negative_queue_raises`，[L220-L234](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_load_balancer.py#L220-L234)）。

**预期结果**：上述三个测试全部通过，与你对照源码的手工推理一致。

**待本地验证**：若你的环境没装 `pytest`/`mocker`（`pytest-mock`），第 3 步可能因缺少依赖跳过；前两步只依赖纯 Python 逻辑，应能稳定通过。

#### 4.3.5 小练习与答案

**练习 1**：同一个 `ROUND_ROBIN` 负载均衡器实例被多个 `StagePool` 共享，会有什么问题？

**参考答案**：会出错。`RoundRobinBalancer` 维护单个 `_next_index` 游标，假设调用者提供的 `replicas` 列表「顺序与长度稳定」（见 [L77-L80](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/load_balancer.py#L77-L80)）。不同 stage 的副本数不同，共享一个游标会让轮转序列错乱（下标对不同长度的列表取模会跳过/重复）。正因如此，`_build_load_balancer_factory` 返回的是**类**，`MembershipController` 为每个 `StagePool` 各 `factory()` 一次得到独立实例（[membership_controller.py:60-61](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/membership_controller.py#L60-L61)）。

**练习 2**：`LeastQueueLengthBalancer` 依赖的 `queue_length` 从哪里来？如果副本停止上报，策略会怎样退化？

**参考答案**：`queue_length` 来自 stage 副本经 `OmniCoordClientForStage` 的**心跳**上报（见 4.5.3 与本讲 4.2.3 心跳分支）。协调者只在心跳里刷新它并广播。若副本停止上报，协调者会在 `heartbeat_timeout` 后把它降级为 `ERROR`、移出 UP 列表，于是 `_collect_serviceable_replicas` 不再把它列入候选——`LEAST_QUEUE_LENGTH` 自然不会选到一个「陈旧拥塞」的副本，而是直接排除它。

---

### 4.4 进程生命周期：run_omni_coordinator_proc 与 OmniCoordinatorRuntime

#### 4.4.1 概念说明

`OmniCoordinator` 本身只是一个跑着两个线程的对象。但在生产部署里，它被刻意放进**独立子进程**运行，由 `OmniCoordinatorRuntime` 在父进程（head 节点的 `DistStageRuntime`）里管理。这样做有两个工程动机（写在 [runtime.py 模块文档](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L4-L14)）：

1. **物理隔离避免 GIL 争用**：协调者要频繁收发 ZMQ 消息、持锁更新注册表，若和繁重的推理/编排同进程，会被 Python GIL 拖慢。
2. **让直接对象耦合不可能**：放进独立进程后，父进程**无法**直接拿到 `OmniCoordinator` 实例，只能通过 ZMQ 与它通信——这从架构上强制了「只能走协议、不能走函数调用」，避免控制面与请求面耦合。

> 设计借鉴：模块文档明确说这「matching vLLM's DPCoordinator pattern」（对齐 vLLM 的 DPCoordinator 模式）。

#### 4.4.2 核心流程

```text
DistStageRuntime._start_omni_master_server()
        │  ① get_open_ports_list(count=2) 拿两个空闲端口
        │  ② router_address = tcp://{host}:{router_port}
        │     pub_address    = tcp://{host}:{pub_port}
        ▼
OmniCoordinatorRuntime.__init__()
        │  ③ 选 mp 上下文（优先 fork，否则 spawn）
        │  ④ ctx.Process(target=run_omni_coordinator_proc, ...)
        │  ⑤ proc.start()  ── 子进程 ──► run_omni_coordinator_proc()
        │  ⑥ parent_conn.recv() 阻塞等子进程发 "ready"（最多 30s）
        │  ⑦ weakref.finalize 注册退出时 _shutdown_proc
        ▼
   返回后，router_address 交给 OmniMasterServer（供副本注册时拿到协调者地址）
            pub_address    交给 MembershipController（用它建 hub 客户端）
```

子进程入口 `run_omni_coordinator_proc` 极简：忽略 `SIGINT`（由父进程统一处理信号）、构造 `OmniCoordinator`、往 ready pipe 发 `"ready"`、然后 `wait_for_shutdown()` 阻塞直到被叫停。

`fork` vs `spawn` 的选择有讲究（见 [`_get_coordinator_mp_context` 文档](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L54-L71)）：`spawn` 会让子进程重新 import 一遍沉重的 CLI/模型栈才能应答 ready pipe，可能超过启动超时；所以优先 `fork`。但 `fork` 必须发生在父进程**初始化 CUDA 或持有长生命周期 ZMQ 套接字之前**，否则子进程会继承不安全状态。

#### 4.4.3 源码精读

子进程入口与 ready 握手：

[run_omni_coordinator_proc](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L33-L51) — `signal.signal(SIGINT, SIG_IGN)` 让子进程不被 Ctrl-C 直接打断（交给父进程统一终止）；构造完协调者立即 `ready_pipe.send("ready")`，再 `coordinator.wait_for_shutdown()` 阻塞——`wait_for_shutdown` 内部就是 `self._stop_event.wait()`（[omni_coordinator.py:193-200](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L193-L200)）。

父进程侧的进程管理者：

[OmniCoordinatorRuntime.__init__](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L93-L151) — 关键点：[L104](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L104) 用 `get_open_ports_list(count=2)` 一次性拿两个端口（避免 ROUTER/PUB 端口分别申请时撞端口）；[L127-L141](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L127-L141) 阻塞等 ready，30 秒内没起来就 `terminate` 并抛错；[L143](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L143) 用 `weakref.finalize` 注册「对象被 GC 时自动关闭子进程」，防止泄漏。

优雅终止子进程：

[_shutdown_proc](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L74-L81) — 三级递进：`terminate`（SIGTERM）→ 等 5s → 还活着就 `kill`（SIGKILL）→ 再等 2s。`OmniCoordinatorRuntime.close()` 幂等地调用它并 `detach` 掉 finalizer（[L153-L159](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L153-L159)）。

实际启动点在 `DistStageRuntime`：

[_start_omni_master_server：创建协调者进程](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L902-L941) — [L926-L929](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L926-L929) 构造 `OmniCoordinatorRuntime`，拿到 `router_address`/`pub_address` 后分别交给 `OmniMasterServer`（副本注册时回填协调者地址）和后续的 `MembershipController`（建 hub 客户端）。注意 [L904-L907](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L904-L907)：这条路径只在「single_stage_mode」即分布式模式下才走，单机 `StageRuntime` 完全不碰协调者。

#### 4.4.4 代码实践

**实践目标**：理解「ready 握手」为何必要，以及 `fork` 时机约束的真实含义。

**操作步骤（源码阅读型）**：

1. 在 [runtime.py:111-125](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L111-L125) 找到 `ctx.Process(...).start()`，注意 `kwargs` 把 `child_conn` 作为 `ready_pipe` 传进子进程。
2. 在 [runtime.py:48-49](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L48-L49) 确认子进程构造完 `OmniCoordinator`（即 ZMQ 套接字已 bind）后才 `ready_pipe.send("ready")`。
3. 回到父进程 [runtime.py:127-141](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L127-L141)，确认父进程是「收到 ready 才认为启动成功」。
4. 阅读 [_get_coordinator_mp_context 的 TODO](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L54-L71)，理解为什么作者写「若协调者启动时机后移，需改成 spawn」。

**需要观察的现象**：ready 信号在「ZMQ bind 完成」之后才发——这意味着父进程拿到 ready 时，子进程的 ROUTER/PUB 已经可以接受连接，不会有「地址已分配但还没 listen」的竞态。

**预期结果**：你能用自己的话解释「为什么 ready 握手必须等 bind 完成」「为什么 fork 必须早于 CUDA 初始化」这两条约束。

**待本地验证**：fork/spawn 的实际行为依赖平台（Windows 无 fork，走 spawn）。若你在 Linux 上观察，`multiprocessing.get_all_start_methods()` 应包含 `fork`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `OmniCoordinatorRuntime` 故意不暴露内部的 `OmniCoordinator` 实例（见 [类文档 L88-L91](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L88-L91)）？

**参考答案**：因为协调者在另一个进程里，父进程拿不到真实对象引用（进程间不共享内存对象）。即使能拿到代理，也会诱导调用者绕过 ZMQ 协议直接操作，破坏「控制面与请求面解耦」的架构约束。所以类文档明确：「callers consume it only via ZMQ through OmniCoordClientForStage and OmniCoordClientForHub」。

**练习 2**：`weakref.finalize(self, _shutdown_proc, self._proc)`（[L143](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L143)）解决什么问题？

**参考答案**：防止协调者子进程泄漏。如果调用者忘了 `close()`，`OmniCoordinatorRuntime` 对象被 GC 回收时，`weakref.finalize` 会自动触发 `_shutdown_proc` 终止子进程。`close()` 里则先 `detach()` 掉这个 finalizer（[L159](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/runtime.py#L159)），避免「主动关闭一次 + GC 又关闭一次」的重复终止。

---

### 4.5 hub/stage 两类客户端：协调者如何被消费

#### 4.5.1 概念说明

协调者是一个中心点，但它要服务的对象有两类，需求完全相反，所以设计成两个客户端：

- **`OmniCoordClientForStage`（stage 端，DEALER）**：每个 stage 副本各持一个。它的职责是**上报**——「我上线了（注册）」「我还活着，队列长度=3（心跳）」「我要下线了（DOWN）」。它是数据的**生产者**。
- **`OmniCoordClientForHub`（hub 端，SUB）**：head 节点的 `MembershipController` 持有一个。它的职责是**订阅并缓存**协调者广播的副本列表，供 `StagePool` 路由时查询。它是数据的**消费者**。

二者用不同 ZMQ 套接字接入协调者的不同通道：stage→ROUTER，hub←PUB。这正好对应协调者的「收/发」两个职责。

#### 4.5.2 核心流程

```text
        stage 副本 0                stage 副本 1              stage 副本 N
   OmniCoordClientForStage     OmniCoordClientForStage        ...
   (DEALER)                    (DEALER)
        │ 注册/心跳/下线              │                          │
        ▼                            ▼                          ▼
   ┌──────────────── OmniCoordinator (ROUTER 收 / PUB 发) ────────────────┐
   │                                                                      │
   │                            PUB 广播 ReplicaList                       │
   └────────────────────────────────┬─────────────────────────────────────┘
                                    │ SUB 订阅
                                    ▼
                          OmniCoordClientForHub        (head 节点)
                          ├─ get_replica_list()         全量缓存
                          └─ get_replicas_for_stage(id) 按 stage 过滤
                                    │
                                    ▼
                          StagePool._collect_serviceable_replicas()
                          → LoadBalancer.select() → 选副本
```

stage 客户端的生命周期：构造时立即发一条 `update`（注册）→ 启动心跳线程每 `heartbeat_interval=5` 秒发一次 → `update_info()` 在状态/队列变化时主动发 `update` → `close()` 时先停心跳线程、再发一条 `status=DOWN` 的 `update`、最后关套接字。

hub 客户端的生命周期：构造时连 SUB、起收线程；收到广播就更新内存里的 `_replica_list`；调用方随时 `get_replica_list()` / `get_replicas_for_stage()` 读最新缓存。

#### 4.5.3 源码精读

stage 客户端的发送与重连：

[OmniCoordClientForStage._send_event](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L109-L155) — 用 `zmq.NOBLOCK` 发送；发送缓冲满（`zmq.Again`）就静默丢弃（[L139-L141](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L139-L141)）；真正的 ZMQ 错误则触发最多 3 次、每次间隔 5 秒的重连（`_reconnect`，[L68-L107](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L68-L107)），重连成功后再补发一次。

心跳线程与「即时刷新队列长度」钩子：

[OmniCoordClientForStage._heartbeat_loop](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L180-L201) — 关键在 [L190-L193](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L190-L193)：每次发心跳前先调可选的 `_on_heartbeat` 钩子，让 engine 子进程**当场**从实时调度状态读出 `queue_length`，再随心跳上报。这正是 `LEAST_QUEUE_LENGTH` 策略能拿到新鲜负载数据的源头。钩子抛异常被 `contextlib.suppress` 吞掉，避免拖垮心跳循环。

工厂函数封装钩子：

[create_stage_coord_client](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L235-L259) — 接收一个 `queue_length_getter` 回调，包成 `_refresh_queue_length` 赋给 `_on_heartbeat`（[L250-L258](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L250-L258)）；`max(int(...), 0)` 防御负值（与 `LeastQueueLengthBalancer` 的非负校验呼应）。实际调用点在 [stage_engine_core_proc.py:162](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L162)。

优雅关闭的顺序：

[OmniCoordClientForStage.close](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L203-L232) — 先 `_stop_event.set()` + join 心跳线程（避免关闭期间并发发送），再置 `_closing=True`，发最后一条 `status=DOWN` 的 update（[L220-L224](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L220-L224)），最后关套接字。这条 DOWN 消息会让协调者把该副本移出 UP 列表。

hub 客户端的接收与缓存：

[OmniCoordClientForHub._recv_loop](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_hub.py#L64-L138) — SUB 套接字在**线程内**创建（[L67-L71](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_hub.py#L67-L71)），因为 ZMQ context/socket 不能跨线程共享；连接失败会重试（每 1 秒）；收到消息就 `_decode_replica_list` 后写 `_replica_list`（加锁）。

hub 客户端的查询接口：

[get_replica_list / get_replicas_for_stage](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_hub.py#L140-L155) — `get_replica_list` 返回最新缓存（还没收到过就返回空列表 + `timestamp=0.0`）；`get_replicas_for_stage` 在缓存上按 `stage_id` 过滤——所以**按 stage 路由是消费侧做的**，协调者广播的是全量视图。

MembershipController 把 hub 接入运行时：

[MembershipController._watch_replica_list](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/membership_controller.py#L128-L149) — 每 0.5 秒比一次 hub 快照，发现某 UP 副本从列表消失就触发 `handle_unregister`（清理 head 侧客户端句柄、给受影响请求发错误）。这是「副本宕机 → 协调者降级 → hub 视图更新 → MembershipController 主动摘除」链路的最后一环。

#### 4.5.4 代码实践

**实践目标**：用真实测试验证 hub 客户端「收到广播→缓存→按 stage 过滤」的完整行为，并对照 stage 客户端理解收发对称性。

**操作步骤**：

1. 阅读 [test_omni_coord_client_for_hub.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py)，注意 [L40-L41](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py#L40-L41) 的 `time.sleep(0.2)` ——这是为了等 SUB 完成连接（慢加入者），否则第一条 PUB 消息会丢。
2. 运行：
   ```bash
   pytest tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py -v
   ```
3. 作为对照，打开 [omni_coord_client_for_stage.py 的 _send_event](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_stage.py#L109-L155)，确认 stage 客户端发的字段（`input_addr/output_addr/stage_id/event_type/status/queue_length`）正好是 hub 客户端 `_decode_replica_list`（[omni_coord_client_for_hub.py:44-62](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coord_client_for_hub.py#L44-L62)）能解出的字段的超集（hub 侧还多解 `last_heartbeat/registered_at`，那两个由协调者补）。

**需要观察的现象**：测试里人为构造的广播 payload 带 `status="error"` 的副本，hub 照单全收缓存（[L67](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/distributed/omni_coordinator/test_omni_coord_client_for_hub.py#L67)）——说明 hub **不做** UP 过滤，过滤推迟到 `StagePool._collect_serviceable_replicas`（[stage_pool.py:386-388](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L386-L388)）。

**预期结果**：测试通过；你能解释「为什么 hub 不过滤、把过滤留到 StagePool」——因为 hub 只是缓存层，多个消费者可能需要不同过滤口径，过滤逻辑放在真正做路由决策的 StagePool 更内聚。

#### 4.5.5 小练习与答案

**练习 1**：为什么 hub 客户端的 SUB 套接字要在线程内创建，而不是在 `__init__` 主线程里创建？

**参考答案**：ZMQ 规定 context 和 socket **不能跨线程使用**。hub 客户端的 SUB 套接字由后台 `_recv_loop` 线程长期 `recv`，所以必须在该线程内创建并独占。如果在主线程创建再传给子线程，ZMQ 会抛错或行为未定义。对比 stage 客户端：它的 DEALER 套接字虽有发送锁保护、可被多线程访问，但创建与生命周期仍由持有者线程统一管理。

**练习 2**：stage 客户端 `close()` 为什么要先停心跳线程，再发 DOWN 消息？

**参考答案**：心跳线程每 5 秒会调 `_send_event`，若不先停掉它，关闭期间就会与 `close()` 里的最后一次 `_send_event` 并发发送，竞争同一 `_send_lock` 甚至在套接字已关闭后仍尝试发送。先 `_stop_event.set()` + `join` 心跳线程，保证关闭期间只有 `close()` 自己在发那条 DOWN 消息，避免竞态。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「**从副本注册到请求路由的全链路推演**」任务。

**场景**：分布式部署 Qwen3-Omni，stage1（Talker）有 3 个副本 R0/R1/R2，分别位于 3 台 worker 机器，使用 `--omni-lb-policy=least-queue-length`。

**任务**：

1. **画时序图**：画出从「3 个副本依次启动」到「head 收到第 1 个请求并路由」的完整时序，至少包含以下事件，并标注每个事件发生在本讲哪个类/文件：
   - R0 启动 → `OmniCoordClientForStage.__init__` 发首条 `update(UP)`；
   - `OmniCoordinator._recv_loop` 收到 → `_handle_event` → `_add_new_replica_locked` → `_schedule_broadcast`；
   - `_periodic_loop` flush → `publish_replica_list_update` → PUB；
   - `OmniCoordClientForHub._recv_loop` 收到 → 更新缓存；
   - R1、R2 重复上述过程；
   - 心跳线程每 5 秒上报 `queue_length`（经 `_on_heartbeat` 钩子即时刷新）；
   - head 收到请求 → `StagePool.pick` → `_collect_serviceable_replicas`（过滤 UP+已挂客户端）→ `LeastQueueLengthBalancer.select` → 选中 R1（假设 R1 队列最短）→ 写 `_affinity`。

2. **故障注入推演**：假设 R2 所在机器突然宕机（进程被杀，来不及发 DOWN）。
   - 协调者侧：`_check_heartbeat_timeouts` 在多少秒后把 R2 标 `ERROR`？（提示：`heartbeat_timeout` 默认 30 秒。）
   - hub 侧：`MembershipController._watch_replica_list` 多久后发现 R2 消失并触发 `handle_unregister`？（提示：`WATCH_INTERVAL_S`。）
   - 路由侧：此后 `LEAST_QUEUE_LENGTH` 还会选到 R2 吗？为什么？（提示：`_collect_serviceable_replicas` 的过滤。）

3. **策略对比**：把同一个场景分别用 `random`、`round-robin`、`least-queue-length` 跑一遍（仅推理，不必真起集群），写出三种策略在「R0 队列=50、R1=0、R2=0」时的首次路由选择，并讨论哪种策略在此场景下最合理。

**参考要点**：

- 时序图中「事件→类」映射应严格对应本讲 4.2/4.3/4.5 的源码。
- R2 宕机：协调者约 30 秒后降级 ERROR（`_check_heartbeat_timeouts`，[omni_coordinator.py:153](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_coordinator/omni_coordinator.py#L153)）；hub 约 0.5 秒内（下一次 `_watch_replica_list` tick，[membership_controller.py:147](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/membership_controller.py#L147)）发现 R2 不在 UP 列表；路由侧不会再选 R2，因为 `_collect_serviceable_replicas` 只收 `status==UP`（[stage_pool.py:387](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_pool.py#L387)）。
- 策略对比：`random` 可能选到已拥塞的 R0（1/3 概率）；`round-robin` 按顺序可能正好轮到 R0；`least-queue-length` 在 R1/R2 间随机（二者队列都最小且并列），**避开** R0，最合理。

---

## 6. 本讲小结

- **OmniCoordinator 是控制面中心**：用 `ROUTER`（收 stage 事件）+ `PUB`（发副本视图）两条 ZMQ 通道，与搬运重张量的 OmniConnector 数据面（[u3-l4](u3-l4-omni-connectors.md)）严格分离。
- **副本生命周期四态流转**：注册（`update/UP`）→ 心跳保活（刷新 `queue_length` 与 `last_heartbeat`）→ 超时降级（`ERROR`，默认 30 秒）→ 优雅下线（`DOWN`）→ 10 分钟后 GC；`ERROR` 副本收到心跳可自愈回 `UP`。
- **广播是合并 + 保活**：`_schedule_broadcast` 只置位，`_periodic_loop` 最多每 100ms 真发一次；每次心跳检查点都强制排一次广播，兜底 SUB 慢加入者。
- **负载均衡是纯函数式抽象**：`select(task, replicas) -> int`，三种策略 `RANDOM`/`ROUND_ROBIN`/`LEAST_QUEUE_LENGTH`；策略字符串经 `_build_load_balancer_factory` 映射到类，`MembershipController` 为每个 `StagePool` 注入独立实例；默认 `random`。
- **选择前先过滤 + 粘性路由**：`StagePool.pick` 先用 `_collect_serviceable_replicas` 剔除非 UP/未挂客户端的副本，再让负载均衡器在候选集上选；同一 `request_id` 首次选择后写 `_affinity`，后续粘在同一副本。
- **进程隔离强制解耦**：`OmniCoordinatorRuntime` 把协调者放进独立子进程（优先 fork），用 ready pipe 握手、`weakref.finalize` 兜底回收；父进程只能经 ZMQ 消费，杜绝控制面与请求面的对象级耦合。

---

## 7. 下一步学习建议

- **回到请求面**：本讲讲的是「副本怎么被发现与选择」，但请求真正投递到副本后如何被执行，请回到 [u3-l3 阶段进程与运行时](u3-l3-stage-process-runtime.md) 复习 `StageEngineCoreClient` 的 ZMQ 通信，体会「控制面（本讲）选副本、数据面（u3-l3/u3-l4）传请求」的分工。
- **进入 AR/Diffusion 子系统**：协调者对副本一视同仁，但 AR stage 与 Diffusion stage 的内部执行截然不同。建议进入 [U4 AR 模块](u4-l1-ar-module-overview.md) 与 [U5 Diffusion 模块](u5-l1-diffusion-engine.md)，理解被协调者管理的两类执行单元。
- **想动手验证**：运行本讲引用的两组测试，并把综合实践里的时序图画出来：
  ```bash
  pytest tests/distributed/omni_coordinator/ -v
  ```
- **扩展阅读**：若对「为什么用独立进程而非线程」感兴趣，可阅读 `runtime.py` 顶部模块文档与 `_get_coordinator_mp_context` 的 TODO 注释，理解 fork/spawn 取舍与未来演进方向。
