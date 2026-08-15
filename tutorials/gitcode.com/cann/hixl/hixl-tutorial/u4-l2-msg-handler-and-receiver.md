# u4-l2 消息处理器与接收器：CS 控制面消息分发机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CS 控制面消息的线上格式（`CtrlMsgHeader` + 消息类型 + 消息体）以及三类消息（定长二进制结构体、变长 JSON、空体消息）的区别。
2. 讲出 server 侧一条消息的完整旅程：epoll 事件 → `MsgReceiver` 拆包 → `MsgHandler` 队列 → 线程池分发 → 具体处理器函数。
3. 掌握 `ConnMsgHandler`（建链类消息编解码）与 `MemMsgHandler`（内存导出消息编解码）这两个 client 侧静态工具类的职责边界。
4. 独立追踪「server 注册内存 → client 建链时拉取远端内存描述」这条链路上每个字段的流转。

本讲承接 u4-l1 建立的 CS 总体架构地图，深入其「神经系统」——控制面消息系统。下一讲（u4-l3）将进入 Channel、TransferPool 与 Endpoint 存储这些被消息驱动起来的「肌肉」。

## 2. 前置知识

### 2.1 控制面消息的线上格式

u4-l1 讲过：CS 层的控制面跑在一条 TCP socket 上，数据面跑在 HCCS/RDMA 链路上。控制面上传输的每条消息都遵循同一个三段式帧格式：

```
+---------------------+---------------------------+------------------+
| CtrlMsgHeader (12B) | CtrlMsgType (4B, int32)   | 消息体 (变长)     |
| magic + body_size   | 消息类型编号               | 定长结构体或JSON  |
+---------------------+---------------------------+------------------+
```

- `magic` 是固定值 `0xA4B3C2D1`，用来快速识别「这是一条 HIXL 控制消息」并对齐字节流。
- `body_size` 是「消息类型 + 消息体」的总长度，接收方据此判断一条消息何时结束。
- 消息体有两种形态：**定长 C 结构体**（建链类消息，直接按字节搬运）和 **JSON 字符串**（内存导出应答，内容随注册内存数量变化）。

TCP 是字节流协议，不保留消息边界，所以接收方必须自己「拆包」——这正是 `MsgReceiver` 存在的原因。

### 2.2 需要的几个基础概念

- **epoll**：Linux 提供的 I/O 多路复用机制。把一批 socket fd 注册进一个 epoll 实例，一个线程调用 `epoll_wait` 就能同时等待所有 fd 的可读/断连事件，是单线程事件循环的标准写法。
- **粘包 / 半包**：一次 `recv` 可能同时读到两条半消息（粘包），也可能只读到半条（半包）。接收方要用状态机 + 缓冲区把字节流重新切分回消息。
- **生产者-消费者队列**：一个线程往队列里放数据并通知，另一个线程等待通知后取走处理。本讲中 epoll 线程是生产者，线程池是消费者。
- **`nlohmann::json`**：C++ 常用的 JSON 库，本讲中用于把内存描述结构体序列化成字符串再发送。

## 3. 本讲源码地图

| 文件 | 位置（角色） | 作用 |
| --- | --- | --- |
| [src/hixl/common/ctrl_msg.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h) | 公共协议定义 | 控制消息头、消息类型枚举、各类请求/应答结构体、处理器函数签名 |
| [src/hixl/cs/msg_handler.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.h) / [.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc) | server 侧 | 消息分发中枢：请求队列 + 消费线程 + 线程池执行处理器 |
| [src/hixl/cs/msg_receiver.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.h) / [.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc) | server 侧 | 每个 client 连接一个，负责从 TCP 字节流中拆出完整消息 |
| [src/hixl/cs/conn_msg_handler.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.h) / [.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.cc) | client 侧 | 建链类消息（MatchEndpoint / CreateChannel）的发送与解析 |
| [src/hixl/cs/mem_msg_handler.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.h) / [.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc) | client 侧 | 内存导出消息（GetRemoteMem）的发送与 JSON 解析 |
| [src/hixl/cs/hixl_cs_server.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc) | server 侧 | epoll 事件循环、处理器注册、各消息的具体处理实现 |
| [src/hixl/cs/hixl_cs_client.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc) | client 侧 | 调用两个 handler 完成 Connect 三步握手 |

注意一个容易混淆的命名：`ConnMsgHandler` / `MemMsgHandler` 是 **client 侧** 的「编解码工具类」（静态方法、无状态），而 `MsgHandler` 是 **server 侧** 的「分发引擎」（有队列有线程）。三者名字相近，角色完全不同。

## 4. 核心概念与源码讲解

### 4.1 MsgHandler：server 侧消息分发中枢

#### 4.1.1 概念说明

server 是被动方，它不知道 client 什么时候会发来消息、发来什么消息。`MsgHandler` 解决的问题是：**把「从 socket 收到一坨字节」和「真正处理这条消息」解耦**。

它的设计是典型的「注册表 + 队列 + 线程池」三件套：

- **注册表**：`map<CtrlMsgType, MsgProcessor>`，每种消息类型注册一个处理函数。业务模块（`HixlCSServer`）在初始化时注册，新增消息类型不需要改动分发器本身。
- **请求队列**：`queue<pair<int32_t, CtrlMsgPtr>>`，epoll 线程只负责往里塞消息（携带 fd 以便处理器知道该回复给谁），不做任何业务处理，保证事件循环不被阻塞。
- **线程池**：消息的消费与处理放在独立线程池里，慢消息（比如涉及底层驱动的内存导出）不会拖住其他 client 的消息。

#### 4.1.2 核心流程

```text
epoll 线程                          消费线程 (listener_)                线程池 worker
    |                                    |                                |
SubmitMsg(fd, msg)                       |                                |
    |-- 入队 req_queue_                  |                                |
    |-- notify_one() ------------------->|                                |
    |                              取出队首 (fd, msg)                      |
    |                              查 processors_[msg_type]               |
    |                              （未注册则丢弃并打日志）                  |
    |                              commit ------------------------------> SetCurrentContext()
    |                                    |                           proc(fd, msg->msg, len)
```

关键点：

1. 队列本身无界，但消息体在 `MsgReceiver` 已被限制在 4MB 以内（见 4.2）。
2. 消费线程查到未注册的消息类型时只打日志、不报错——分发器对未知消息保持宽容。
3. worker 线程执行处理器前必须先 `SetCurrentContext` 恢复 ACL context，因为线程池线程没有继承创建者的设备上下文（这一模式在 u3-l4 的 ConnectPoolExecutor 中也出现过）。

#### 4.1.3 源码精读

先看协议与分发器定义。[ctrl_msg.h:L109-L110](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h#L109-L110) 定义了两个核心类型：`CtrlMsgPtr` 是装消息的共享指针，`MsgProcessor` 是处理器函数签名——入参只有 fd 和消息体字节串，说明处理器要自己负责反序列化和回包：

```cpp
using CtrlMsgPtr = std::shared_ptr<CtrlMsg>;
using MsgProcessor = std::function<Status(int32_t fd, const char *msg, uint64_t msg_len)>;
```

[msg_handler.cc:L16-L22](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc#L16-L22) 是生产者入口：加锁入队、解锁后通知。`SubmitMsg` 是唯一被 epoll 线程调用的方法：

```cpp
void MsgHandler::SubmitMsg(int32_t fd, const CtrlMsgPtr &msg) {
  {
    std::lock_guard<std::mutex> lock(req_mutex_);
    req_queue_.emplace(fd, msg);
  }
  req_cv_.notify_one();
}
```

[msg_handler.cc:L24-L34](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc#L24-L34) 初始化：创建名为 `cs_server` 的弹性线程池（最小 4、最大 1024 线程，即 u3-l4 讲过的 ThreadPool），捕获当前 ACL context，启动消费线程：

```cpp
constexpr uint32_t kMinThreadPoolSize = 4U;
constexpr uint32_t kMaxThreadPoolSize = 1024U;
thread_pool_ = MakeUnique<ThreadPool>("cs_server", kMinThreadPoolSize, kMaxThreadPoolSize);
HIXL_CHK_STATUS_RET(ctx_.GetCurrentContext(), "GetCurrentContext failed");
listener_ = std::thread([this]() { HandleMsg(); });
```

[msg_handler.cc:L65-L72](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc#L65-L72) 注册处理器：同一类型重复注册返回 `PARAM_INVALID`，防止两个处理器争抢同一类消息：

```cpp
HIXL_CHK_BOOL_RET_STATUS(it == processors_.cend(), PARAM_INVALID, "msg_type:%d, has been registered.", ...);
processors_[msg_type] = msg_processor;
```

[msg_handler.cc:L74-L101](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc#L74-L101) 是消费主循环：条件变量等待 → 取消息 → 查注册表 → 提交线程池。注意 `(void)thread_pool_->commit(...)` 忽略返回值，且任务里先恢复 context 再调用处理器：

```cpp
(void)thread_pool_->commit([this, req, proc]() -> void {
  HIXL_CHK_STATUS(ctx_.SetCurrentContext(), "SetCurrentContext failed");
  (void)HandleMsg(req.first, req.second, proc);
});
```

那么到底注册了哪些处理器？答案在 server 初始化处。[hixl_cs_server.cc:L222-L236](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L222-L236) 逐一注册了四类消息的处理函数：

| 消息类型 | 处理函数 | 语义 |
| --- | --- | --- |
| `kMatchEndpointReq` | `MatchEndpointMsg` | 用 client 携带的远端端点描述匹配本地端点 |
| `kCreateChannelReq` | `CreateChannel` | 在匹配到的端点上创建数据面 channel |
| `kGetRemoteMemReq` | `ExportMem` | 导出本端注册的内存描述给对端 |
| `kDestroyChannelReq` | `DestroyChannel` | 销毁该 fd 对应的 channel |

对照 [ctrl_msg.h:L29-L47](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h#L29-L47) 的 16 种消息类型枚举，server 核心只处理这 4 种请求（外加各自应答由处理函数直接同步回写）；`kHeartBeat`、`kNotify` 等类型由引擎其他控制面逻辑使用，不经过 `MsgHandler`。

还有一个精妙的细节：[hixl_cs_server.cc:L585-L607](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L585-L607) 中，当 epoll 检测到 client 断连时，server 会**伪造一条空的 `kDestroyChannelReq` 消息**塞进同一个分发通道，让 channel 清理和其他消息一样排队执行，而不是在 epoll 线程里直接做清理：

```cpp
auto msg = MakeShared<CtrlMsg>();
if (msg != nullptr) {
  msg->msg_type = CtrlMsgType::kDestroyChannelReq;   // 空消息体，仅凭 fd 清理
  msg_handler_.SubmitMsg(fd, msg);
}
```

这体现了「一切清理皆消息」的设计哲学：epoll 线程永远只做 O(1) 的轻量操作。

#### 4.1.4 代码实践

**实践目标**：验证「注册表」机制的真实性——确认 server 恰好注册 4 个处理器，并理解注册时机。

**操作步骤**（源码阅读型实践，无需硬件）：

1. 在仓库根目录执行 `grep -n "RegisterMsgProcessor" src/hixl/cs/hixl_cs_server.cc`，统计调用点。
2. 对每个调用点，记录消息类型与被绑定的成员函数。
3. 再执行 `grep -rn "SubmitMsg" src/hixl/cs/`，找出消息的所有生产者。

**需要观察的现象**：

- `RegisterMsgProcessor` 恰好出现 4 次（另一处在 `RegProc` 这个转发函数里，见 [hixl_cs_server.cc:L569-L575](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L569-L575)，它暴露给上层注册自定义类型如心跳处理器）。
- `SubmitMsg` 的生产者只有两个：`ProClientMsg`（正常收到的消息）与 `CleanupClient`（伪造的销毁消息）。

**预期结果**：4 个核心处理器 + 2 个消息生产者，与 4.1.3 的表格吻合。若想看到运行时证据，可在真实昇腾环境运行 quickstart 样例并设置 `export ASCEND_SLOG_PRINT_TO_STDOUT=1`，[msg_handler.cc:L57-L61](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_handler.cc#L57-L61) 的 `HIXL_EVENT` 日志会打印 `handle msg begin, msg type:%d`，可据此确认分发到的类型编号（运行结果待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把一个耗时 5 秒的内存导出操作直接放在 epoll 线程里做，会发生什么？

**答案**：epoll 线程被阻塞 5 秒，期间所有 client 的连接建立、消息接收、断连检测全部停摆——单线程事件循环最忌讳在循环体内做慢操作。这正是 `MsgHandler` 用队列 + 线程池把「收」与「处理」分离的原因（参见 [hixl_cs_server.cc:L577-L583](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L577-L583) 中 `ProClientMsg` 只做 `IRecv` + `SubmitMsg`）。

**练习 2**：为什么 `SubmitMsg` 要把 fd 和消息一起入队，而不是入队消息、处理时再查 socket？

**答案**：fd 是「回复地址」。server 可能同时服务多个 client 连接，每条消息必须记住自己来自哪个 fd，处理器才能通过 `CtrlMsgPlugin::Send(fd, ...)` 把应答回给正确的对端（例如 [hixl_cs_server.cc:L387-L396](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L387-L396) 的 `SendMatchEndpointResp` 就直接用 fd 回包）。

---

### 4.2 MsgReceiver：每连接一台「拆包机」

#### 4.2.1 概念说明

`MsgReceiver` 解决的问题是：**把 TCP 字节流还原成一条条完整的 `CtrlMsg`**。它是一个纯解析组件：

- 每个 client 连接（fd）对应一个实例，在 `Accept` 成功时创建并登记到 `clients_` 映射表（[hixl_cs_server.cc:L624-L627](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L624-L627)）。
- 实例内部保存该连接独有的接收缓冲区和解析进度，因此不同连接的半包状态互不干扰。
- 名字里的 `I`（`IRecv`）表示非阻塞语义：有多少读多少，凑不出完整消息就等下一次 epoll 事件。

#### 4.2.2 核心流程

`IRecv` 是一个两状态状态机：

```text
              recv(fd, buf, 4096)          缓冲区 >= 12B?
WAITING_FOR_HEADER ──────────> 检查 magic / body_size 合法性
                                     │ 合法 → 摘除 header，memmove 剩余字节
                                     ▼
                              缓冲区 >= body_size?
WAITING_FOR_BODY ────────────────> 组装 CtrlMsg{msg_type, msg}
   ▲    │ 不足 → break，等下次 recv        │ 有剩余字节 → memmove 回移，回到 HEADER 态
   │    │                                  │ 恰好用完 → 回到 HEADER 态并 break
   └────┴──────────────────────────────────┘
```

两道防线保证解析器不会被恶意或损坏的报文打崩：

1. `magic != 0xA4B3C2D1` → `PARAM_INVALID`（字节流错位/版本不符）。
2. `body_size` 必须落在 \([4, 4\mathrm{MB}]\) 区间——下限保证至少装得下消息类型，上限防止畸形长度撑爆内存。

#### 4.2.3 源码精读

[msg_receiver.cc:L17-L18](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc#L17-L18) 定义两个关键常量：每次 `recv` 的块大小 4KB 与消息体上限 4MB（与 mem_msg_handler.cc 中 GetRemoteMemResp 的 4MB 上限呼应）。

[msg_receiver.cc:L21-L45](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc#L21-L45) 是 header 解析：校验 magic 与 body_size 区间，然后把缓冲区里 header 之后可能已经到达的「提前量」字节前移，转入等 body 状态：

```cpp
HIXL_CHK_BOOL_RET_STATUS(header->magic == kMagicNumber, PARAM_INVALID, "Invalid magic number:%u received.", ...);
HIXL_CHK_BOOL_RET_STATUS(expected_size_ <= kMaxBodySizeInBytes, PARAM_INVALID, ...);
recv_state_ = RecvState::WAITING_FOR_BODY;
```

[msg_receiver.cc:L47-L59](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc#L47-L59) 的 `CheckRecv` 处理三种 recv 结果：负值且 errno 为 `EAGAIN`/`EWOULDBLOCK`/`EINTR` 属于非阻塞 socket 的正常「暂时没数据」；负值且其他 errno 是真错误；返回 0 表示对端关闭连接——这个信息会被上层 epoll 的 `EPOLLRDHUP` 事件确认后走 `CleanupClient`。

[msg_receiver.cc:L61-L108](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc#L61-L108) 是核心循环。一次 recv 后，缓冲区可能含 0 条、1 条或多条完整消息，`while (true)` 循环把它们全部拆出来；组装消息的代码揭示了 `CtrlMsg` 的内存布局约定——body 的前 4 字节就是消息类型，其余是纯消息体：

```cpp
ctrl_msg->msg_type = *reinterpret_cast<CtrlMsgType *>(recv_buffer_.data());
ctrl_msg->msg = std::string(recv_buffer_.data() + sizeof(CtrlMsgType), expected_size_ - sizeof(CtrlMsgType));
msgs.emplace_back(ctrl_msg);
```

若拆完一条后还有剩余字节（粘包），用 `memmove_s` 把剩余部分搬回缓冲区头部继续拆，这正是 u2-l4 提过的 `CtrlMsg`（类型 + 字符串体）与 `MsgProcessor(fd, const char*, uint64_t)` 签名的衔接点。

#### 4.2.4 代码实践

**实践目标**：手工推演一次「半包 → 粘包」的完整解析过程，检验对状态机的理解。

**操作步骤**（纸面推演，无需运行）：

1. 假设 client 连发两条消息：A（header 12B + body 20B，共 32B）和 B（header 12B + body 40B，共 52B）。
2. 推演三种到达模式下的 `IRecv` 行为：
   - 第一次 recv 恰好收到 32B；
   - 第一次 recv 只收到 15B（header 12B + 3B 提前量）；
   - 第一次 recv 收到 84B（两条全部到达）。

**需要观察的现象 / 预期结果**：

- 模式一：拆出 A 一条，`received_size_` 归零，状态回 `WAITING_FOR_HEADER`，break。
- 模式二：header 合法后执行 [msg_receiver.cc:L32-L43](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc#L32-L43) 的 memmove，把 3B 提前量前移，body 不足 20B，break 等下次。
- 模式三：一次循环内拆出 A、B 两条消息，`msgs` 向量大小为 2。
- 三种模式最终都产出完全相同的两条 `CtrlMsg`——这就是拆包状态机的正确性标准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `recv_buffer_` 要按 `received_size_ + 4096` 增长，而不是一次性开 4MB？

**答案**：绝大多数控制消息只有几十字节（如 `MatchEndpointReq`），按需增长避免为每条连接预付 4MB 内存；上限校验（`kMaxBodySizeInBytes`）只在 header 里做，buffer 只跟随实际需要扩展（[msg_receiver.cc:L62-L64](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc#L62-L64)）。

**练习 2**：如果 client 用了错误的库版本，magic 不一致会发生什么？

**答案**：`RecvHeader` 校验失败返回 `PARAM_INVALID`，该连接本次收到的字节被丢弃、消息不会被分发；这是协议层的前向兼容防线，配合 `HIXL_LOGE` 日志可快速定位版本不匹配问题（[msg_receiver.cc:L24-L25](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/msg_receiver.cc#L24-L25)）。

**练习 3**：`IRecv` 里收到 0 字节（对端关闭）时为什么不直接清理连接？

**答案**：`CheckRecv` 只返回 false 结束本次 `IRecv`；真正的清理由 epoll 层的 `EPOLLERR|EPOLLHUP|EPOLLRDHUP` 事件触发 `CleanupClient` 完成（[hixl_cs_server.cc:L630-L634](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L630-L634)），单一来源避免两处清理互相竞争。

---

### 4.3 ConnMsgHandler：client 侧建链消息编解码

#### 4.3.1 概念说明

u4-l1 讲过 client 的 `Connect` 内部要做「匹配端点 → 预取远端内存 → 建数据面 channel」三步握手。`ConnMsgHandler` 负责其中第一步和第三步的报文编解码——它是一组**静态方法工具类**，不持有任何状态，socket fd 由调用方传入。

它编码的两类消息都是**定长二进制结构体**：

- `MatchEndpointReq`：client 把自己想连的远端端点描述 `EndpointDesc` 发给 server；server 匹配成功后回 `MatchEndpointResp`（含 `dst_ep_handle`、监听端口、`channel_index`）。
- `CreateChannelReq`：两端各自在本地创建 channel，`channel_index` 由 server 侧原子计数器生成（[hixl_cs_server.cc:L40-L41](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L40-L41)），保证两端拼出一致的 channelName。

#### 4.3.2 核心流程

client 视角的建链报文交换（省略中间的内存预取，见 4.4）：

```text
client                                          server
  |--- header+type+MatchEndpointReq(dst) --------->|  MatchEndpointMsg
  |<-- header+type+MatchEndpointResp --------------|  (dst_ep_handle, port, channel_index)
  |    ... 此处插入 GetRemoteMem 预取（4.4 节）...
  |--- header+type+CreateChannelReq(src,hdl,...) ->|  CreateChannel
  |    [本地同时调用 Endpoint::CreateChannel]        |  (server 侧也建 channel)
  |<-- header+type+CreateChannelResp(result) -------|
```

注意收发两侧的对称性：client 发完 `CreateChannelReq` 后**不等应答先建自己的 channel**（[hixl_cs_client.cc:L1223-L1236](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1223-L1236)），两端并行建链以缩短握手时间，最后才收取应答确认。

#### 4.3.3 源码精读

[conn_msg_handler.cc:L19-L27](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.cc#L19-L27) 的 `SendHeaderTypeBody` 是所有建链消息发送的公共底座——按「header、类型、体」三次 `Send`，把帧格式固定在一处：

```cpp
HIXL_CHK_STATUS_RET(hixl::CtrlMsgPlugin::Send(socket, &header, static_cast<uint64_t>(sizeof(header))));
HIXL_CHK_STATUS_RET(hixl::CtrlMsgPlugin::Send(socket, &msg_type, static_cast<uint64_t>(sizeof(msg_type))));
HIXL_CHK_STATUS_RET(hixl::CtrlMsgPlugin::Send(socket, body, body_size));
```

[conn_msg_handler.cc:L29-L42](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.cc#L29-L42) 的接收侧校验值得注意：除了 magic，还要求 `body_size` **精确等于**期望值（`sizeof(CtrlMsgType) + sizeof(具体Resp结构体)`）——因为定长结构体消息不应该有任何偏差，偏差即意味着协议版本不一致：

```cpp
HIXL_CHK_BOOL_RET_STATUS(header.body_size == expect_body_size, hixl::PARAM_INVALID, ...);
```

[conn_msg_handler.cc:L152-L167](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.cc#L152-L167) 发送 MatchEndpoint 请求：`body_size` 按定长公式计算，远端端点描述整体拷入请求体：

```cpp
header.body_size = static_cast<uint64_t>(sizeof(CtrlMsgType) + sizeof(MatchEndpointReq));
MatchEndpointReq body{};
body.dst = dst;
```

[conn_msg_handler.cc:L100-L128](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.cc#L100-L128) 解析 MatchEndpoint 应答：先校验类型确实是 `kMatchEndpointResp`，再 `memcpy_s` 出结构体，最后检查 `result == SUCCESS` 并把三个关键字段交给调用方——其中 `dst_ep_handle` 是后续所有消息（包括 GetRemoteMemReq）定位 server 端点的「通行证」：

```cpp
remote_endpoint_handle = resp.dst_ep_handle;
remote_listen_port = resp.port;
channel_index = resp.channel_index;
```

server 侧的对应处理在 [hixl_cs_server.cc:L398-L440](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L398-L440) 的 `MatchEndpointMsg`：直接把 `msg` 按定长结构体反解（`reinterpret_cast`，前面已用 `msg_len == sizeof(MatchEndpointReq)` 保证安全），调用 `endpoint_store_.MatchEndpoint` 匹配，失败时回 `PARAM_INVALID`，成功时填写句柄、端口和自增的 `channel_index`。

#### 4.3.4 代码实践

**实践目标**：画出 Connect 期间建链类报文的完整时序，并标注每条报文的字段来源。

**操作步骤**：

1. 阅读 [hixl_cs_client.cc:L1196-L1242](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1196-L1242) 的 `ExchangeEndpointAndCreateChannel`。
2. 对每一步标注：调用了 ConnMsgHandler 的哪个方法、报文里关键字段来自哪个变量（如 `remote_endpoint_`、`remote_endpoint_handle_`、`g_next_server_channel_index`）。
3. 特别注意 `CreateChannelReq` 的构造（L1219-L1222）：`channel_index` 用的是上一步应答里带回的值——解释为什么两端必须用同一个值。

**需要观察的现象 / 预期结果**：时序图共 4 条报文（2 请求 2 应答）；`channel_index` 若两端不一致，拼出的 channelName 不同，channel 将无法互通——这就是它必须由 server 统一发放、随 MatchEndpointResp 带回的原因。

#### 4.3.5 小练习与答案

**练习 1**：`RecvAndCheckHeader` 对定长消息要求 `body_size` 精确相等，而 `MsgReceiver`（server 侧）只要求区间合法，为什么校验强度不同？

**答案**：server 面对任意 client，只能先按通用帧格式收下（且要容纳变长 JSON 消息），所以只做安全区间校验；client 侧 `ConnMsgHandler` 解析的是与 server 版本强绑定的定长结构体，长度偏差即协议失配，宁可失败也不容偏差（[conn_msg_handler.cc:L38-L40](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/conn_msg_handler.cc#L38-L40)）。

**练习 2**：`MatchEndpointResp` 里的 `port` 是做什么用的？

**答案**：它是匹配到的端点在数据面（如 UB/RDMA 底层）的监听端口，client 收到后会 `local_endpoint_->SetPort(remote_listen_port)` 设置到自己本地端点上，供后续创建数据面 channel 使用（[hixl_cs_client.cc:L1211](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1211)；server 侧来源见 [hixl_cs_server.cc:L417-L431](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L417-L431) 的 `HcommProxy::EndpointGetListenPort`）。

---

### 4.4 MemMsgHandler：client 侧内存导出消息编解码

#### 4.4.1 概念说明

单边传输的前提是 client 知道（并能 DMA 访问）server 注册的内存。u2-l3 讲过注册的两端分工：

- **server 的 `RegisterMem` 是本地调用**，不走网络——它把内存登记到每个端点上（[hixl_cs_server.cc:L318-L356](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L318-L356)），外加一个内置的 `_hixl_builtin_*_trans_flag` 完成标志（u4-l1 讲过的 trans_flag）。
- **client 通过 GetRemoteMem 请求把这些登记拉过来**：server 把内存「导出」为一串描述符（含底层驱动生成的 export_desc，如 RDMA rkey 类信息），client 收到后交给底层「导入」，才得到可单边读写的远端地址。

`MemMsgHandler` 就是这一「请求-应答」的编解码器。它与 `ConnMsgHandler` 最大的差别：**应答体是 JSON 而非定长结构体**——因为内存条数不定（上限 4096 + 1 条内置 flag，见 [mem_msg_handler.cc:L23-L27](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc#L23-L27)）。

#### 4.4.2 核心流程

```text
client HixlCSClient::GetRemoteMemImpl                server (线程池 worker)
  |                                                    |
  |  MemMsgHandler::SendGetRemoteMemRequest            |  MsgHandler 分发到 ExportMem
  |--- header + type + GetRemoteMemReq{dst_ep_handle} ->|
  |                                                    |  endpoint->ExportMem(mem_descs)
  |                                                    |  Serialize: nlohmann::json dump
  |  MemMsgHandler::RecvGetRemoteMemResponse           |  SendRemoteMemResp
  |<-- header + type + JSON 字符串 ---------------------|
  |  解析 JSON → vector<HixlMemDesc>                    |
  |  ImportRemoteMem(descs) → 可 DMA 的远端内存列表      |
```

触发时机有两个：一是 client `Connect` 内部作为三步握手的第二步**预取**（prefetch，[hixl_cs_client.cc:L1216](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1216)），让链路一建好就具备传输条件；二是链路建立后 server 又注册了新内存时，client 主动调用 `HixlCSGetRemoteMem` 增量拉取。

#### 4.4.3 源码精读

先看协议。[ctrl_msg.h:L85-L97](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h#L85-L97) 定义了内存描述与应答结构——注意 `HixlMemDesc` 是 C++ 结构体（带 `std::string` 和裸指针），**不能**直接按字节发送，这正是它走 JSON 序列化的原因：

```cpp
struct HixlMemDesc {
  CommMem mem;                       // {type, addr, size}：内存三元组
  bool is_imported = false;
  std::string tag;                   // 注册时的 mem_tag
  void *export_desc = nullptr;       // 底层导出描述（变长字节串）
  uint32_t export_len = 0U;
  void *registered_dev_mem = nullptr; // host 内存注册后的 device 映射地址
};
struct GetRemoteMemResp {
  Status result;
  std::vector<HixlMemDesc> mem_descs;
};
```

**发送侧**（client）：[mem_msg_handler.cc:L244-L261](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc#L244-L261) 与建链消息同构——header + 类型 + 定长 `GetRemoteMemReq`（只有一个 `dst_ep_handle` 字段，即 MatchEndpoint 应答带回的通行证），仍是三段 `Send`。

**server 处理侧**：[hixl_cs_server.cc:L530-L550](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L530-L550) 的 `ExportMem` 反解请求、按句柄取端点、调用 `ep->ExportMem` 填充描述列表，随后回包。失败兜底用了 scope guard：任何异常路径都会回一条 `result = FAILED` 的应答，避免 client 无限等待：

```cpp
const auto req = reinterpret_cast<const GetRemoteMemReq *>(msg);
auto ep = endpoint_store_.GetEndpoint(handle);
GetRemoteMemResp resp{};
HIXL_CHK_STATUS_RET(ep->ExportMem(resp.mem_descs), "Failed to export mem");
resp.result = SUCCESS;
```

**server 序列化侧**：[hixl_cs_server.cc:L482-L528](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L482-L528) 用 `to_json` 把结构体转 JSON——`export_desc` 的每个字节被逐个 `push_back` 进数组（保证跨端安全，避免字节序/对齐歧义），`body_size` 此时才按 JSON 实际长度计算：

```cpp
j["export_desc"].push_back(static_cast<int>(data_ptr[i]));
...
header.body_size = static_cast<uint64_t>(sizeof(CtrlMsgType) + msg_str.size());
```

**接收侧**（client）：[mem_msg_handler.cc:L61-L78](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc#L61-L78) 的 header 校验是「开区间 + 上界」：`body_size` 必须大于类型长度（JSON 至少 1 字节）且不超过 4MB——与定长消息的精确校验形成对比。[mem_msg_handler.cc:L263-L281](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc#L263-L281) 串起接收四步：收 header → 收 body → 剥出类型与 JSON 指针 → 解析。

[mem_msg_handler.cc:L89-L116](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc#L89-L116) 的 JSON 解析有三层防御：先查 `result` 字段且必须 SUCCESS，再查 `mem_descs` 必须是数组，所有异常（含 JSON 库抛出的）都被捕获并翻译为 hixl 错误码。[mem_msg_handler.cc:L139-L170](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc#L139-L170) 把 JSON 数组还原为 `malloc` 的字节缓冲——数组里每个元素都校验是 \([0,255]\) 的整数，并用 scope guard 保证失败路径不留内存泄漏：

```cpp
const int v = j_export[i].get<int>();
HIXL_CHK_BOOL_RET_STATUS(v >= 0 && v <= UINT8_MAX, hixl::PARAM_INVALID, ...);
```

最后 [hixl_cs_client.cc:L1278-L1296](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1278-L1296) 的 `GetRemoteMemImpl` 把解析结果交给 `ImportRemoteMem` 完成底层导入——至此 client 拿到了可单边读写的远端内存清单。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：追踪「server `RegisterMem` 之后、client 建链拉取」这一完整过程中的请求/应答字段流转，写出字段流转表。

**操作步骤**：

1. 阅读 server 注册路径 [hixl_cs_server.cc:L318-L356](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L318-L356)：注意每块内存按 `ShouldRegisterEndpointForMem` 筛选后登记到各端点，`mem_tag` 一路携带。
2. 阅读内置 flag 注册 [hixl_cs_server.cc:L143-L168](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L143-L168)：确认 host 侧 8 字节 flag 以 `_hixl_builtin_host_trans_flag` 为 tag 注册——它也会出现在导出列表里。
3. 沿 client 路径走一遍：`Connect`（L1185-L1193）→ `ExchangeEndpointAndCreateChannel`（L1196-L1242）→ `GetRemoteMemImpl`（L1278-L1296）。
4. 填写下面的字段流转表。

**参考答案（字段流转表）**：

| 阶段 | 方向 | 消息 / 调用 | 字段 | 来源 → 去处 |
| --- | --- | --- | --- | --- |
| 0. server 本地 | 不走网络 | `HixlCSServer::RegisterMem` | `mem_tag`, `CommMem{type,addr,size}` | 用户注册参数 → 登记到各匹配端点（`reg_mems_` 台账） |
| 0b. server 本地 | 不走网络 | `RegisterHostTransFinishedFlag` | `_hixl_builtin_host_trans_flag` | 8 字节页对齐 host 标志 → 也注册进端点 |
| 1. 请求 | client → server | `GetRemoteMemReq` | `dst_ep_handle` | `MatchEndpointResp.dst_ep_handle`（通行证）→ 定位 server 端点 |
| 2. server 导出 | 线程池内 | `ExportMem` → `ep->ExportMem` | `mem_descs[]` | 端点登记的内存 → 逐条导出 |
| 3. 应答序列化 | server → client | `GetRemoteMemResp`（JSON） | `result` | 执行结果（SUCCESS/FAILED）→ client 判错依据 |
| | | | `mem_descs[].mem.{type,addr,size}` | `CommMem` 三元组 → JSON → 还原为 `CommMem` |
| | | | `mem_descs[].tag` | 注册时的 `mem_tag` → client 侧 `mem_tag_list` |
| | | | `mem_descs[].export_desc[]` | 底层驱动导出描述（变长字节串，逐字节进 JSON 数组）→ client `malloc` 还原后交给 `ImportRemoteMem` |
| | | | `mem_descs[].registered_dev_mem`（可选） | host 内存的 device 映射地址 → 供 UBoE/UB_RTP 类映射协议使用 |
| 4. client 导入 | 本地 | `ImportRemoteMem` | 整个 `mem_descs` | 解析结果 → 底层导入，产出可 DMA 的远端内存列表 |
| 5. 下一步 | client → server | `CreateChannelReq` | `src`, `dst_ep_handle`, `channel_index`, `qos`… | 预取完成后建数据面 channel（回到 4.3 流程） |

**需要观察的现象 / 预期结果**：

- 注册路径**没有任何网络消息**——`RegisterMem` 是纯本地调用，网络上只出现在 client 拉取时的 `GetRemoteMemReq/Resp` 一对。
- 应答 JSON 中除了用户注册的内存，还必然多一条 tag 为 `_hixl_builtin_*_trans_flag` 的 8 字节条目（u4-l1 讲过的完成感知机制）。
- 若在真实环境运行 quickstart 并开启 `ASCEND_SLOG_PRINT_TO_STDOUT=1`，可在 client 日志中看到 `remote mem serialize success, str:...`（server 侧 [hixl_cs_server.cc:L521](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L521) 会打印完整 JSON），可直接对照本表逐字段核对（运行结果待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `GetRemoteMemResp` 用 JSON 而不用定长结构体？

**答案**：两个原因——内存条数不定（1 到 4097 条），定长结构体要么浪费要么装不下；`HixlMemDesc` 含 `std::string` 和变长 `export_desc`，本身就不是可平凡拷贝的类型。JSON 以体积换通用性，且该消息只在建链/低频拉取时收发一次，性能不敏感。

**练习 2**：client 侧解析 `export_desc` 时为什么坚持逐元素校验 \([0,255]\) 而不是直接按字节数组收？

**答案**：JSON 数组元素是通用数字，恶意或损坏的报文可能塞入负数或超 255 的值；逐元素校验加上失败路径的 `FreeExportDesc` 全量释放（[mem_msg_handler.cc:L196-L220](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/mem_msg_handler.cc#L196-L220)）保证了「部分解析失败不泄漏、不越界」。

**练习 3**：server 的 `ExportMem` 处理失败时 client 会看到什么？

**答案**：scope guard 保证 client 一定会收到一条 `result = FAILED` 的 JSON 应答（[hixl_cs_server.cc:L531-L535](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L531-L535)），client 侧 `ParseResultAndGetArray` 检查到 `result != SUCCESS` 后直接返回该错误码——请求-应答模式永远有回应，不会静默挂起。

## 5. 综合实践

**任务：写一篇《一条 GetRemoteMem 消息的一生》追踪笔记，把本讲四个组件串起来。**

具体要求：

1. 以 quickstart 样例（u1-l3）为背景：server 端注册一块 device 内存后，client 发起 `Connect`。
2. 按时间顺序写出 7 个关键节点，每个节点注明「所在文件:行号 + 所属组件」：
   - (1) server `RegisterMem` 本地登记（hixl_cs_server.cc:318）；
   - (2) client `Connect` 发起 TCP 连接并调 `ExchangeEndpointAndCreateChannel`（hixl_cs_client.cc:1196）；
   - (3) `ConnMsgHandler` 完成 MatchEndpoint 问答，client 拿到 `dst_ep_handle`（conn_msg_handler.cc:152 / 169）；
   - (4) `MemMsgHandler::SendGetRemoteMemRequest` 发出请求（mem_msg_handler.cc:244）；
   - (5) server `MsgReceiver::IRecv` 在 epoll 线程拆出 `CtrlMsg`（msg_receiver.cc:61）；
   - (6) `MsgHandler::HandleMsg` 入队、分发到线程池、执行 `ExportMem`（msg_handler.cc:74 / hixl_cs_server.cc:530）；
   - (7) client `RecvGetRemoteMemResponse` 解析 JSON 并 `ImportRemoteMem`（mem_msg_handler.cc:263 / hixl_cs_client.cc:1288）。
3. 为 (4)~(6) 之间的报文填写 4.4.4 节格式的字段流转表。
4. 回答收尾问题：这条消息经过了几个线程？（答案要点：client 调用线程发出；server epoll 线程收到并拆包；`cs_server` 线程池某 worker 执行 `ExportMem` 并同步回包；client 调用线程阻塞在 `Recv` 上收应答——共 3 个活跃线程协同。）

若有昇腾环境，运行 `examples/cpp/hixl_example_quickstart` 并设置 `ASCEND_SLOG_PRINT_TO_STDOUT=1`，用日志中 `handle msg begin, msg type:%d` 与 `remote mem serialize success` 两条事件验证笔记中的时序（待本地验证）。

## 6. 本讲小结

- 控制面消息统一采用「`CtrlMsgHeader`（magic + body_size）+ 消息类型 + 消息体」三段式帧格式；消息体分定长二进制结构体（建链类）与变长 JSON（内存导出类）两种形态。
- server 侧的接收链路是三段分工：epoll 线程（`DoWait`/`ProClientMsg`）只做事件循环，`MsgReceiver` 按两状态机拆包（防粘包/半包，body 上限 4MB），`MsgHandler` 用「注册表 + 队列 + 线程池」把消息异步分发给四类核心处理器。
- `ConnMsgHandler` 与 `MemMsgHandler` 是 client 侧静态编解码工具类：前者处理定长的 MatchEndpoint/CreateChannel 问答（body_size 精确校验），后者处理变长的 GetRemoteMem 请求与 JSON 应答（三层防御式解析）。
- `dst_ep_handle` 是贯穿建链与内存拉取的「通行证」，由 `MatchEndpointResp` 发放；`channel_index` 由 server 原子计数器统一发放，保证两端 channelName 一致。
- server 的 `RegisterMem` 是纯本地调用；client 通过 GetRemoteMem 拉取导出描述并导入，才获得可单边读写的远端内存——导出列表中永远多一条内置 trans_flag。
- 工程亮点可迁移：epoll 线程只做 O(1) 操作（连清理都伪装成消息入队）、处理器注册表模式、scope guard 保证失败路径也回包/不泄漏。

## 7. 下一步学习建议

本讲讲完了控制面的「神经系统」。下一讲 **u4-l3「Channel、TransferPool 与 Endpoint 存储」** 将学习被这些消息驱动起来的数据面组件：`MatchEndpointMsg` 背后的 `EndpointStore`、`CreateChannel` 背后的 `Channel`，以及设备侧传输的调度中枢 `TransferPool`。建议先自行阅读 [src/hixl/cs/endpoint.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc) 中的 `ExportMem`/`RegisterMem` 实现，弄清端点内部的内存台账结构，再进入下一讲。
