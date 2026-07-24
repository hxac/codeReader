# 控制平面与 ZMQ 消息

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 SGLang-Omni 的「控制平面」是什么、它与「数据平面」为什么必须分离。
- 读懂 `pipeline/control_plane.py` 里的四类 ZMQ 套接字（PUSH / PULL / PUB / SUB）各自的连接拓扑，以及谁 bind、谁 connect。
- 说出消息在线上的统一格式：每条消息先 `to_dict()` 再 `msgpack` 序列化，接收端反序列化后交给 `parse_message` 分发。
- 把 `proto/messages.py` 里的 11 个消息类按用途分类（协调 / 数据就绪 / 完成 / 流 / 中止 / 关闭），并解释为什么只有 `AbortMessage` 走 PUB/SUB。
- 理解 `DataReadyMessage.data_ref` 的「双重身份」：它既可能是一个直接 CUDA IPC 信封，也可能是一个指向 relay 后端的 `DataRef` 字典。

## 2. 前置知识

本讲承接 [u3-l1 Stage 抽象与 IO 外壳]——你已经知道 Stage 是一个 IO 外壳，把所有计算 dispatch 给 scheduler。本讲要回答的下一个问题是：**Stage 与 Stage 之间、Stage 与 Coordinator 之间，到底用什么方式传递命令和状态？**

在阅读源码前，先建立三个直觉：

1. **控制消息很小，数据张量很大。** 一次跨阶段搬运可能是几十 MB 的 hidden state，而「我这边算好了，你来取」这样一句话只有几百字节。把它们塞进同一条管道会让大张量阻塞小命令。SGLang-Omni 的解法是把两者拆成两条独立的「平面」：
   - **控制平面（control plane）**：用 ZMQ + msgpack 传递小而快的命令与状态，本讲的主角。
   - **数据平面（data plane / relay）**：用 CUDA IPC / 共享内存 / NIXL / mooncake 等搬运大张量，[u3-l3] 会专门讲。
2. **ZMQ 不是普通的 socket。** 它在底层 socket 之上提供「消息模式」：PUSH/PULL 是负载均衡的点对点投递，PUB/SUB 是一对多广播。本讲会看到这两者各自用在哪。
3. **bind 与 connect 的方向有讲究。** 在 ZMQ 里，对一条 `ipc://` 通道而言，先 `bind`（创建监听点）的一方通常是不会动的那一端，`connect`（主动连过去）的一方是可以随时增减的那一端。本讲会反复出现「谁 bind、谁 connect」。

如果你对 ZMQ 完全陌生，只需记住一句话就够跟读本讲：**ZMQ 把「连接」和「收发」解耦——你可以先 send 再 connect，消息会在缓冲里等着；模式（PUSH/PULL/PUB/SUB）决定了消息怎么流动。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/proto/messages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py) | 定义全部控制消息类型，每个类都有 `to_dict()` / `from_dict()`；`parse_message` 按 `type` 字段反序列化。 |
| [sglang_omni/pipeline/control_plane.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py) | ZMQ 套接字封装（PushSocket / PullSocket / PubSocket / SubSocket），以及面向 Stage 与 Coordinator 的两个高层门面。 |
| [sglang_omni/pipeline/coordinator.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py) | Coordinator 用控制平面提交请求、收完成、广播 abort。 |
| [sglang_omni/pipeline/stage/runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py) | Stage 用控制平面收工作、发数据就绪、发完成/流。这里能看到 `data_ref` 的两种处理分支。 |
| [sglang_omni/pipeline/runtime_config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/runtime_config.py) | `allocate_endpoints` 为 completion / abort / 每个 stage 生成 `ipc://` 端点。 |
| [sglang_omni/comm/data_ref.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/data_ref.py) | `DataRef` 结构——`data_ref` 的「relay 身份」长什么样。 |

## 4. 核心概念与源码讲解

### 4.1 ZMQ PUSH/PULL：点对点的工作投递通道

#### 4.1.1 概念说明

绝大多数控制消息是「单播」的：Coordinator 把一个新请求投给入口 stage；stage A 把「数据就绪」通知 stage B；stage 把完成结果回传给 Coordinator。这些场景的共同点是**一个发送方、一个（逻辑上的）接收方**，正好对应 ZMQ 的 **PUSH/PULL** 模式。

PUSH/PULL 的语义要点：

- **PUSH** 端只 `send`，**PULL** 端只 `recv`。
- 若一个 PUSH 连了多个 PULL，消息会**轮询（round-robin）**分发；本讲里大多数通道只有一个对端，所以等价于点对点。
- PUSH 在没有 PULL 接收时不会丢消息，而是阻塞/入队（受 high-water mark 限制），这正是「控制消息不能丢」所需要的。

#### 4.1.2 核心流程

一次请求生命周期里，PUSH/PULL 承载的主要控制流是：

```text
Coordinator --SubmitMessage-->  入口 Stage        (PUSH→PULL)
Stage A    --DataReadyMessage--> Stage B          (PUSH→PULL, 附 data_ref)
Stage B    --DataAckMessage-----> Stage A         (PUSH→PULL, 确认已取走)
Stage      --CompleteMessage----> Coordinator     (PUSH→PULL)
Stage      --StreamMessage------> Coordinator     (PUSH→PULL)
```

每一跳都是「发送方用一个 PUSH socket connect 到接收方的 PULL socket」。谁 bind？**接收方 bind**，因为它常驻、生命周期长；发送方 connect，因为发送方可能动态增减。

#### 4.1.3 源码精读

先看发送侧封装。`PushSocket` 创建一个 `zmq.PUSH` socket 并 `connect` 到目标端点：

[sglang_omni/pipeline/control_plane.py:75-95](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L75-L95) —— `connect()` 建 PUSH socket，`send()` 把消息序列化后发出。

接收侧 `PullSocket` 默认 `bind=True`，即由接收方在本地端点上监听：

[sglang_omni/pipeline/control_plane.py:104-130](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L104-L130) —— `start()` 按 `bind` 标志决定 bind 还是 connect，`recv()` 收到字节后反序列化。

端点本身是怎么来的？`allocate_endpoints` 为整条管线一次性生成三条 `ipc://` 通道（completion / abort / 每个 stage 各一条）：

[sglang_omni/pipeline/runtime_config.py:160-173](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/runtime_config.py#L160-L173) —— 注意这里全部用 `ipc://`（Unix 域套接字），说明同主机内的 stage 间控制消息走本地 IPC，比 TCP 轻量。

两个高层门面把上述 socket 组装成「面向角色」的接口。Stage 侧持有：一个 `PullSocket(bind=True)` 收工作、一个 `PushSocket` 发完成给 Coordinator、一个 `PushSocket` 字典发往下游 stage：

[sglang_omni/pipeline/control_plane.py:246-260](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L246-L260) —— `StageControlPlane.start()` 建立三类 socket。

Coordinator 侧则相反：它 bind 自己的 PULL（收完成）和 PUB（广播 abort），并对每个 stage 持有一个 connect 的 PUSH：

[sglang_omni/pipeline/control_plane.py:367-377](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L367-L377) —— `CoordinatorControlPlane.start()`。

把这条链路在业务代码里坐实：Coordinator 提交一个新请求，就是构造 `SubmitMessage` 并 PUSH 给入口 stage：

[sglang_omni/pipeline/coordinator.py:401-407](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L401-L407) —— `submit_to_stage(..., SubmitMessage(...))`。

而 Stage 的主循环就是在 `control_plane.recv()` 上阻塞等待这些 PUSH 过来的消息：

[sglang_omni/pipeline/stage/runtime.py:254-275](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L254-L275) —— Stage 的 `run()` 主循环，`msg = await self.control_plane.recv()` 之后按类型分发。

#### 4.1.4 代码实践

**实践目标**：确认 PUSH/PULL 的「接收方 bind、发送方 connect」拓扑，并亲眼看到一条消息被序列化、发出、接收、还原。

**操作步骤**：

1. 阅读 [control_plane.py:75-146](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L75-L146)，对照下表填空。

| 通道 | 发送方 | 接收方 | bind 在哪一侧 |
| --- | --- | --- | --- |
| Coordinator→入口 stage 的 Submit | Coordinator (PUSH) | Stage (PULL) | ？ |
| Stage→Coordinator 的 Complete | Stage (PUSH) | Coordinator (PULL) | ？ |
| Stage A→Stage B 的 DataReady | Stage A (PUSH) | Stage B (PULL) | ？ |

2.（可选，源码阅读型）在本机已装好 `sglang_omni` 的 venv 里运行下面这段「回环」脚本，验证序列化与收发（**待本地验证**：需要 `pip install pyzmq msgpack` 且 `sglang_omni` 可 import）：

```python
# 示例代码：验证 PushSocket/PullSocket 的一次往返
import asyncio
from sglang_omni.pipeline.control_plane import PushSocket, PullSocket
from sglang_omni.proto import SubmitMessage

async def main():
    ep = "ipc:///tmp/u3l2_submit.sock"
    pull = PullSocket(ep, bind=True)
    push = PushSocket(ep)
    await pull.start()
    await push.connect()
    await push.send(SubmitMessage(request_id="req-demo", data={"text": "hi"}))
    msg = await pull.recv()
    print("收到:", type(msg).__name__, msg.request_id, msg.data)
    pull.close(); push.close()

asyncio.run(main())
```

**需要观察的现象 / 预期结果**：上表三行都应是「接收方 bind」。脚本应打印 `收到: SubmitMessage req-demo {'text': 'hi'}`，证明 PUSH→PULL 的一次投递与 `parse_message` 还原成功。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Stage 的 `_recv_socket` 用 `bind=True`，而它发给 Coordinator 的 `_coordinator_socket` 用 `connect`？

**参考答案**：接收方常驻、端点固定，由它 bind 到自己的 `ipc://stage_xxx.sock`；发送方数量可变、生命周期短，主动 connect 过去更自然。这与 ZMQ「稳定的端 bind，流动的端 connect」的惯例一致。

**练习 2**：如果两个 stage 共享同一个 OS 进程（colocated），它们之间的控制消息还会走 ZMQ 吗？

**参考答案**：仍然会走 ZMQ（端点照常分配、socket 照常建），这是保持「Stage 不分支于部署形态」这一不变量的代价；同进程的优化（`local_object` 直传）发生在**数据平面**，不在控制平面。

---

### 4.2 PUB/SUB 广播：abort 信号为什么要广播

#### 4.2.1 概念说明

有一类消息不能用单播：**abort（中止）**。当用户取消一个请求、或某个终态失败触发 fail-fast 时，Coordinator 必须「通知所有 stage：把这个请求清理掉」。用 PUSH/PULL 的问题是：

- Coordinator **不知道请求此刻停在哪个 stage**——它可能正在入口 stage 排队，也可能已经流到下游，甚至在多个 stage 同时活跃（fan-out / streaming）。
- PUSH/PULL 是「投给某一个消费者」，投错了就没人清理，留下僵尸状态。

因此 abort 用 **PUB/SUB**：Coordinator 是唯一的 publisher，所有 stage 都是 subscriber，一条消息广播给所有人。`AbortMessage` 本身也只有 `request_id` 一个字段——它**不写收件人**，因为收件人就是「全体」。

PUB/SUB 有一个著名的「慢加入（slow-joiner）」问题：**订阅者在 publisher 发出消息之后才连上来，会错过那条消息。** 本讲代码用一个 0.1 秒的 sleep 来缓解它。

#### 4.2.2 核心流程

```text
Coordinator (PUB, bind)
   │  AbortMessage(request_id)   ← 只广播，不点对点
   ├──► Stage A (SUB, connect)   → _on_abort(request_id)
   ├──► Stage B (SUB, connect)   → _on_abort(request_id)
   └──► Stage C (SUB, connect)   → _on_abort(request_id)
```

每个 Stage 在 `start()` 时 connect 到 abort 端点并 `SUBSCRIBE b""`（订阅全部主题），再起一个独立的 `_abort_listener` 任务阻塞等待。收到后调用 `scheduler.abort(request_id)` 并清理本 stage 与该请求相关的资源。abort 对「已经完成的请求」是幂等的——Coordinator 在广播前会先查状态机。

#### 4.2.3 源码精读

`AbortMessage` 极其简单，只有一个 `request_id`，`to_dict` 也只产出一个 `type` 加一个字段：

[sglang_omni/proto/messages.py:153-164](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L153-L164) —— 注意它没有任何 `from_stage` / `to_stage`，印证了「收件人即全体」。

Publisher 侧：`PubSocket.bind()` 先绑定，再 `await asyncio.sleep(0.1)` 给订阅者留出连接时间——这正是为缓解 slow-joiner：

[sglang_omni/pipeline/control_plane.py:156-163](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L156-L163) —— bind 后的 0.1 秒等待。

Subscriber 侧：`SubSocket.connect()` 之后 `setsockopt(zmq.SUBSCRIBE, b"")` 表示「订阅所有消息」，`recv()` 还多了一道校验，确保收到的确实是 `AbortMessage`：

[sglang_omni/pipeline/control_plane.py:187-204](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L187-L204) —— SUB 端的连接与接收。

业务侧的广播点：Coordinator 的 `abort()` 在校验状态后，调用 `broadcast_abort`：

[sglang_omni/pipeline/coordinator.py:452-453](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L452-L453) —— 广播 `AbortMessage`。

同样的广播也用于 fail-fast：某个终态失败时，Coordinator 也会广播 abort 让其它 stage 停手：

[sglang_omni/pipeline/coordinator.py:527-536](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L527-L536) —— 任一终态失败 → 广播 abort。

Stage 端的监听与处理：

[sglang_omni/pipeline/stage/runtime.py:1536-1547](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1536-L1547) —— `_abort_listener` 阻塞在 `recv_abort()` 上。
[sglang_omni/pipeline/stage/runtime.py:1557-1561](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1557-L1561) —— `_on_abort` 记录、清理、`scheduler.abort()`。

#### 4.2.4 代码实践

**实践目标**：解释「为什么 abort 必须用 PUB/SUB，而不能用 PUSH/PULL」。

**操作步骤**：

1. 在 [messages.py:153-164](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L153-L164) 确认 `AbortMessage` 没有 `to_stage` 字段。
2. 在 [coordinator.py:441-453](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L441-L453) 看 `abort()` 是否查询「请求当前在哪个 stage」。

**需要观察的现象 / 预期结果**：你会看到 `abort()` 完全不依赖 stage 定位信息，直接广播。写出两句解释：(1) Coordinator 无法廉价地知道请求此刻位于哪个 stage；(2) 即便知道，streaming / fan-out 也可能让请求同时存在于多个 stage，只有广播才能保证全部清理。

#### 4.2.5 小练习与答案

**练习 1**：`PubSocket.bind()` 里为什么有一句 `await asyncio.sleep(0.1)`？删掉会有什么后果？

**参考答案**：缓解 PUB/SUB 的 slow-joiner——订阅者 connect 得比 publisher bind 晚时，会错过之前发布的消息。删掉后，启动瞬间广播的 abort 可能被还没连上的 SUB 丢弃。注意这只能缓解、不能根除（0.1s 之后才连上的订阅者仍会错过），所以代码注释明确写了「Give subscribers time to connect」。

**练习 2**：abort 对一个已经 COMPLETED 的请求会发生什么？

**参考答案**：Coordinator 的 `abort()` 先检查 `info.state`，若已是 `COMPLETED/FAILED/ABORTED` 就直接返回 `False`，不会广播——即 abort 对已完成请求幂等（详见 [coordinator.py:444-450](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/coordinator.py#L444-L450)）。

---

### 4.3 msgpack 序列化：消息的统一线格式

#### 4.3.1 概念说明

11 种消息类要在同一条 ZMQ 通道上混传，必须解决两个问题：**怎么变成字节**、**收到字节怎么认出是哪种消息**。SGLang-Omni 的做法分两层：

- **序列化层**：统一用 [msgpack](https://msgpack.org/)（一种比 JSON 更紧凑的二进制格式）。每条消息先调自己的 `to_dict()` 变成一个普通 dict（并打上 `type` 标签），再 `msgpack.packb` 成字节。
- **分发层**：接收端 `msgpack.unpackb` 还原成 dict，交给 `parse_message(dict)`，它读 `dict["type"]` 决定构造哪个类。

为什么选 msgpack 而不是 JSON？控制消息里会带 `data_ref` 这种嵌套结构，msgpack 对二进制友好、体积更小、解析更快；而「人类可读」对控制平面并不重要。

#### 4.3.2 核心流程

```text
发送：  msg.to_dict()  ──►  msgpack.packb(..., use_bin_type=True)  ──►  bytes  ──►  zmq.send
接收：  zmq.recv  ──►  bytes  ──►  msgpack.unpackb(..., raw=False)  ──►  dict  ──►  parse_message(dict)  ──►  对象
```

关键约定：每个 dict 都带一个字符串字段 `type`（如 `"submit"`、`"data_ready"`、`"abort"`），它是 `parse_message` 的分派键。

#### 4.3.3 源码精读

序列化与反序列化只有各一行实质逻辑，但定义了整个控制平面的线格式：

[sglang_omni/pipeline/control_plane.py:43-51](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L43-L51) —— `serialize_message` / `deserialize_message`。

`ControlMessage` 是所有可走控制平面的消息的联合类型，序列化函数的签名就建立在这上面：

[sglang_omni/pipeline/control_plane.py:28-45](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L28-L45)。

分发逻辑是朴素的 if/elif 链，但它是「线格式 ↔ 类」的唯一真相表，值得通读：

[sglang_omni/proto/messages.py:340-380](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L340-L380) —— `parse_message` 按 `type` 分派；未知类型抛 `ValueError`。

每个类的 `to_dict()` 里都显式写了 `type` 字段，例如 `DataReadyMessage`：

[sglang_omni/proto/messages.py:42-49](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L42-L49) —— `"type": "data_ready"` 就是分派键。

注意 `to_dict()` 还承担了**契约校验**：`DataReadyMessage` 要求「流式信号（`is_done` 或 `error`）不得携带 `data_ref` / `chunk_id`」，这类规则在序列化前就被强制执行，避免非法消息上线：

[sglang_omni/proto/messages.py:25-41](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L25-L41)。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「序列化 → 字节 → 反序列化」的往返一致，并理解 `type` 字段的作用。

**操作步骤**：

1. 在 REPL 或脚本里跑下面的往返（**待本地验证**：需 `msgpack` 与可 import 的 `sglang_omni`）：

```python
# 示例代码：验证消息往返与 type 分派
import msgpack
from sglang_omni.proto import CompleteMessage, parse_message

obj = CompleteMessage(request_id="r1", from_stage="decode", success=True, result={"text": "hi"})
blob = msgpack.packb(obj.to_dict(), use_bin_type=True)
print("bytes:", blob)
back = parse_message(msgpack.unpackb(blob, raw=False))
print("type back:", type(back).__name__, back.result)
assert isinstance(back, CompleteMessage) and back.result == {"text": "hi"}
```

2. 把 `CompleteMessage` 换成 `AbortMessage`，观察 `to_dict()` 的输出体积——abort 只有两个键。

**需要观察的现象 / 预期结果**：脚本断言通过；你会看到一条完成消息被打包成几十字节的二进制，反序列化后类型与 `result` 完全还原。这正是控制平面「小而快」的体现。

#### 4.3.5 小练习与答案

**练习 1**：如果有人新增了一种消息却不更新 `parse_message`，会发生什么？

**参考答案**：该消息能被 `to_dict()` + `packb` 正常发出，但接收端 `parse_message` 走到 `else` 分支抛 `ValueError(f"Unknown message type: {msg_type}")`。所以「加消息类型」必须同时改 `messages.py` 的类与 `parse_message` 的分派链。

**练习 2**：`unpackb(..., raw=False)` 与 `raw=True` 的区别对这里有什么影响？

**参考答案**：`raw=False` 让 msgpack 把字节串按 UTF-8 解码成 `str`，于是 `type`、`request_id` 等键值都是字符串，`parse_message` 的字符串比较才成立；若用 `raw=True` 会得到 `bytes`，分派会失败。

---

### 4.4 控制消息类型大全：分类与 data_ref 的双重身份

#### 4.4.1 概念说明

`proto/messages.py` 一共定义了 11 个消息类。本节先把它们按用途分成六大类（对应本讲实践任务要求），再重点拆解其中最复杂的 `DataReadyMessage`——它的 `data_ref` 字段有「双重身份」，是理解控制平面与数据平面如何衔接的关键。

六大分类：

| 分类 | 消息类 | 方向 | 典型 socket |
| --- | --- | --- | --- |
| 协调 / 生命周期 | `SubmitMessage`、`ShutdownMessage`、`AdminMessage`、`AdminResultMessage`、`ProfilerStartMessage`、`ProfilerStopMessage` | Coordinator↔Stage | PUSH/PULL |
| 数据就绪 | `DataReadyMessage`、`DataAckMessage` | Stage→Stage | PUSH/PULL |
| 完成 | `CompleteMessage` | Stage→Coordinator | PUSH/PULL |
| 流 | `StreamMessage` | Stage→Coordinator | PUSH/PULL |
| 中止 | `AbortMessage` | Coordinator→所有 Stage | **PUB/SUB** |
| 关闭 | `ShutdownMessage`（归入协调） | Coordinator→Stage | PUSH/PULL |

> 说明：`ShutdownMessage` 既属于「协调」也兼具「关闭」语义，表中按实践任务的六分类归到协调/关闭。**只有 `AbortMessage` 走 PUB/SUB**，原因见 4.2。

#### 4.4.2 核心流程：data_ref 的双重身份

`DataReadyMessage` 的字段里最关键的是 `data_ref: dict | None`。它**不是数据本身**，而是一个「指针」——告诉下游「数据在数据平面的哪里取」。根据传输方式不同，这个 dict 有两种形态：

```text
形态 A（直接 CUDA IPC 信封）：
  同 GPU / 同进程时，data_ref 内含 torch 的 IPC 句柄信息，
  下游直接用 torch.cuda 重新打开张量，不经过 relay。

形态 B（relay DataRef）：
  跨 GPU / 跨节点时，data_ref 是一个带 _type="DataRef" 的字典，
  指向 cuda_ipc / shm / mooncake / nixl 等 relay 后端里的缓冲区。
```

无论哪种形态，真正的「大张量」都不在控制消息里——控制消息只带一个小信封。这就是「控制平面与数据平面分离」在代码里的直接体现。

#### 4.4.3 源码精读

先看 `DataReadyMessage` 的字段定义：`data_ref` 是 `dict[str, Any] | None`，外加 `chunk_id`、`is_done`、`error` 三个用于流式信号的字段：

[sglang_omni/proto/messages.py:13-23](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L13-L23) —— 字段定义。

再看接收端 Stage 怎么「分叉」处理 `data_ref`。`_on_data_ready` 先试探形态 A（直接 CUDA IPC），不命中才走形态 B（relay）：

[sglang_omni/pipeline/stage/runtime.py:394-410](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L394-L410) —— 形态 A：`is_direct_cuda_ipc_payload_ref` 命中则直接反序列化 IPC 信封。

[sglang_omni/pipeline/stage/runtime.py:412-434](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L412-L434) —— 形态 B：把 `msg.data_ref` 当 relay `DataRef`，经 `relay.read_payload` 取数据，并回 `DataAckMessage`。

形态 B 的 dict 如何变成对象？`_data_ref_from_message` 直接 `DataRef.from_dict(msg.data_ref)`：

[sglang_omni/pipeline/stage/runtime.py:646-649](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L646-L649)。

而 `DataRef` 这个「relay 身份」长什么样？它带 `transport`、`buffer`（后端引用）、`tensors`（每个张量的 shape/dtype/device/offset）等——全是「在哪取、怎么取」的元信息，没有张量本体：

[sglang_omni/comm/data_ref.py:112-127](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/data_ref.py#L112-L127) —— `DataRef` 结构。

发送侧则对称：Stage 发送一个 payload 时，先尝试构造直接 CUDA IPC 信封塞进 `data_ref`：

[sglang_omni/pipeline/stage/runtime.py:1146-1155](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1146-L1155) —— `DataReadyMessage(data_ref=direct_ref)`。

最后，`DataAckMessage` 是「数据就绪」类的另一半：下游取完数据后用它回 ACK，发送方据此释放发送池里的 slot（详见 [u3-l3]）：

[sglang_omni/proto/messages.py:98-107](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L98-L107) —— `DataAckMessage` 字段。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把 `proto/messages.py` 里的全部消息类按「协调 / 数据就绪 / 完成 / 流 / 中止 / 关闭」分类，并说明哪类走 PUB/SUB、为什么。

**操作步骤**：

1. 打开 [messages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py)，逐个读每个 `@dataclass` / `msgspec.Struct` 的 docstring 与 `to_dict()` 里的 `type` 值。
2. 完成下表（参考答案见后）：

| 消息类 | `type` 值 | 分类 | socket 模式 |
| --- | --- | --- | --- |
| `SubmitMessage` | `"submit"` | 协调 | PUSH/PULL |
| `DataReadyMessage` | ？ | ？ | ？ |
| `DataAckMessage` | ？ | 数据就绪 | PUSH/PULL |
| `CompleteMessage` | ？ | 完成 | ？ |
| `StreamMessage` | ？ | ？ | PUSH/PULL |
| `AbortMessage` | ？ | 中止 | **PUB/SUB** |
| `ShutdownMessage` | ？ | 关闭/协调 | PUSH/PULL |
| `AdminMessage` / `AdminResultMessage` | ？ | 协调 | PUSH/PULL |
| `ProfilerStartMessage` / `ProfilerStopMessage` | ？ | 协调 | PUSH/PULL |

3. 在 [control_plane.py:346-434](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L346-L434) 与 [246-343](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/control_plane.py#L246-L343) 中确认：只有 `abort` 通道用了 `PubSocket`/`SubSocket`，其余全是 `PushSocket`/`PullSocket`。

**参考答案**：

- `DataReadyMessage`：`"data_ready"`，**数据就绪**，PUSH/PULL。
- `CompleteMessage`：`"complete"`，**完成**，PUSH/PULL。
- `StreamMessage`：`"stream"`，**流**，PUSH/PULL。
- `AbortMessage`：`"abort"`，**中止**，**PUB/SUB**。
- `ShutdownMessage`：`"shutdown"`，关闭/协调，PUSH/PULL。
- `AdminMessage`/`AdminResultMessage`：`"admin"`/`"admin_result"`，协调，PUSH/PULL。
- `ProfilerStartMessage`/`ProfilerStopMessage`：`"profiler_start"`/`"profiler_stop"`，协调，PUSH/PULL。

**关于「哪类走 PUB/SUB、为什么」的一句话结论**：**只有 `AbortMessage` 走 PUB/SUB**。因为中止信号必须送达每一个 stage（Coordinator 不跟踪请求当前停在哪、且 streaming/fan-out 时请求可能同时存在于多个 stage），广播是唯一能保证「全部清理」的投递方式；其余消息都是点对点的命令或回执，用 PUSH/PULL 足矣。

**需要观察的现象 / 预期结果**：你应能独立得出「仅 abort 走 PUB/SUB」的结论，并用「无法廉价定位请求当前位置 + 广播保证全清理」两点支撑它。

#### 4.4.5 小练习与答案

**练习 1**：`DataReadyMessage` 的 `to_dict()` 为什么禁止「`is_done=True` 同时带 `data_ref`」？

**参考答案**：`is_done`/`error` 是**流式信号**（表示「这条流结束了」或「这条流出错了」），它本身不携带数据；而带 `data_ref` 的是**数据消息**（表示「这里有一份数据来取」）。两种语义混在一起会让接收端困惑（[messages.py:30-36](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L30-L36) 有显式校验），所以序列化前就强制互斥。

**练习 2**：`DataAckMessage` 里的 `object_id` 是干什么用的？

**参考答案**：它定位「刚才被取走的那一个数据面对象」。发送方在发送池里按对象/slot 管理已发出的缓冲，收到带 `object_id` 的 ACK 后才能安全释放对应区间（一次 ACK 释放一段区间，详见 [u3-l3]）。这把「控制确认」与「数据缓冲释放」解耦。

**练习 3**：`AdminMessage` 与 `ProfilerStartMessage` 为什么也算「控制消息」而不是「数据消息」？

**参考答案**：它们都不搬运用户张量，只携带**控制意图**（暂停生成、更新权重、启动 profiler……），体积小、需要可靠投递与点对点回执，正是控制平面的典型负载，所以与 Submit/Shutdown 同属「协调」类，走 PUSH/PULL。

## 5. 综合实践

把本讲四块知识串起来：**画一张「控制平面消息流」时序图，并标注每段用的 socket 与序列化方式。**

任务设定：一次 **流式 chat completion**，管线为 `preprocess → thinker →(stream_to)→ talker →(terminal)→ Coordinator`，用户在生成中途**取消请求**。

要求：

1. 用箭头画出下列消息，每条箭头标注「PUSH/PULL 还是 PUB/SUB」与发送方 bind/connect 角色：
   - Coordinator → preprocess：`SubmitMessage`
   - talker → Coordinator：`StreamMessage`（逐 chunk）
   - Coordinator → 全体 stage：`AbortMessage`（用户取消时）
2. 在 `SubmitMessage` 箭头上标注序列化方式：`to_dict() → msgpack.packb → zmq.send`，并在接收端写 `zmq.recv → msgpack.unpackb → parse_message`。
3. 在 `preprocess → thinker` 这一段额外画出 `DataReadyMessage`（带 `data_ref`）与回程的 `DataAckMessage`，并在 `data_ref` 旁注明它的**两种可能形态**（直接 CUDA IPC 信封 / relay `DataRef`）。
4. 解释：为什么用户取消时用 PUB/SUB 广播 abort，而不是给「当前正在算的 stage」发一条 PUSH？

**验收标准**（自检）：

- 全图只有 abort 一处是 PUB/SUB，其余都是 PUSH/PULL。
- 每条 PUSH/PULL 都能说清「谁 bind、谁 connect」。
- 能用一句话说清 `data_ref` 为什么是「信封」而不是「本体」。

> 本实践为源码阅读型，不需要 GPU；若要真正跑通端到端，需先完成 [u1-l4] 的服务启动。

## 6. 本讲小结

- SGLang-Omni 把**控制平面**（ZMQ + msgpack，小而快的命令/状态）与**数据平面**（relay，大张量）显式分离；控制消息只带「指针」，不带张量本体。
- 点对点的命令与回执（Submit / DataReady / DataAck / Complete / Stream / Shutdown / Admin / Profiler）走 **PUSH/PULL**，拓扑约定「接收方 bind、发送方 connect」，端点是 `ipc://` 本地套接字。
- **只有 `AbortMessage` 走 PUB/SUB**，因为中止信号必须广播给所有 stage——Coordinator 无法廉价定位请求当前位置，且 streaming/fan-out 时请求可能同时在多个 stage 活跃；`PubSocket.bind` 后的 0.1s sleep 用于缓解 slow-joiner。
- 线格式统一为：`msg.to_dict()`（带 `type` 标签）→ `msgpack.packb`；接收端 `msgpack.unpackb` → `parse_message` 按 `type` 分派；`to_dict()` 还兼任契约校验。
- 11 个消息类可分六类：协调 / 数据就绪 / 完成 / 流 / 中止 / 关闭；其中 `DataReadyMessage.data_ref` 有双重身份——直接 CUDA IPC 信封（同 GPU/同进程）或 relay `DataRef` 字典（跨 GPU/跨节点），由 Stage 接收端分叉处理。

## 7. 下一步学习建议

- 接着读 [u3-l3 Relay 数据平面与传输后端]：本讲的 `data_ref` 形态 B（relay `DataRef`）会落到具体的 `cuda_ipc` / `shm` / `nixl` / `mooncake` 后端实现上，重点看发送池、slot 分配与 `DataAckMessage` 如何触发释放。
- 读 [u4-l1 调度器接口与 SimpleScheduler]：本讲里反复出现的「Stage 把工作推入 `scheduler.inbox`」就是控制消息转化为 scheduler 输入的那一跳，看完能闭环理解「控制消息 → 计算」的边界。
- 若对性能可视化感兴趣，可跳读 [u6-l3 请求级 Profiler]：`ProfilerStartMessage` / `ProfilerStopMessage` 这两条控制消息如何驱动请求级事件记录。
