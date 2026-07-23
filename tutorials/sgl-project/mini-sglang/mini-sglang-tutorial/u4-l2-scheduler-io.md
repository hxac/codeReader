# Scheduler I/O 与多 rank 广播

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 `SchedulerIOMixin` 如何在「构造期」根据 `offline_mode` 与 `tp_info.size` 把 `receive_msg` / `send_result` 动态绑定到不同实现，做到「同一份循环代码，适配单卡/多卡/离线三种拓扑」。
- 说出 **rank 0 对外收发、其他 rank 静默** 的分工：只有 rank 0 直接连 tokenizer/detokenizer，其余 rank 从来不碰外部 ZMQ 队列。
- 理解多 rank 下的两段式广播：rank 0 用 ZMQ **pub/sub** 把每条原始消息字节原样转发给其他 rank，再用 `torch.distributed` 的 **broadcast 同步「消息条数」**。
- 解释清楚本讲最核心的一个问题——**为什么要广播"消息条数"而不是直接转发 tensor**，以及为什么 rank 1 不能靠自己的 `empty()` 去判断收了几条。
- 掌握 `sync_all_ranks` 这条 CPU 侧 `barrier` 的作用，以及它复用的是哪条 gloo 进程组。

本讲紧承 u4-l1。u4-l1 讲的是主循环「收消息 → 调度 → 前向 → 处理结果」的骨架，并把 `receive_msg(blocking=...)` 与 `send_result(reply)` 当成两个黑盒调用。本讲就是打开这两个黑盒，看清消息从哪来、到哪去、多卡之间如何对齐。循环内部「怎么挑下一批」（u4-l3/u4-l4）、「前向里算什么」（u5）仍不在本讲范围。

## 2. 前置知识

阅读本讲前，请确保你已经建立以下认知（来自前置讲义）：

- **进程架构与身份**（u1-l4）：每张 GPU 一个 Scheduler 进程，身份由 `DistributedInfo(rank, size)` 标识；`rank == 0` 即 `is_primary()`，是「队长」，独揽与 tokenizer/detokenizer 的交互。多卡必须保证各 rank 处理**同一批次**，这是张量并行正确性的前提。
- **主循环调用点**（u4-l1）：`overlap_loop` / `normal_loop` 每轮都会 `for msg in self.receive_msg(blocking=blocking)`，并在收尾时 `self.send_result(reply)`。本讲解释这两个方法的真实实现。
- **消息族与序列化**（u2-l3）：`TokenizeMsg → UserMsg → DetokenizeMsg → UserReply` 沿 `uid` 串成环；`serialize_type` / `deserialize_type` 靠 `__dict__` 把 dataclass 压平成字典再 msgpack 编码；`get_raw` / `put_raw` 搬的是**原始 msgpack 字节**，可省去重复编解码。
- **ZMQ 四种 socket**（u1-l4、u2-l3）：PUSH/PULL 是 1 对 1 的投递，PUB/SUB 是 1 对多的广播。本讲会用到全部四种。
- **通信两条干道**（u1-l4）：ZMQ 走轻量控制消息（token、元数据），NCCL/PyNCCL 走前向时的重型张量（`all_reduce` / `all_gather`）。本讲只讲 ZMQ 干道 + 一个 CPU 侧的 `broadcast`/`barrier`，不碰 NCCL 的重型张量。

> 名词解释：**gloo** —— PyTorch 自带的 CPU 后端进程组实现（对应 GPU 上的 NCCL）。本讲里同步"消息条数"用的 `broadcast`、`sync_all_ranks` 用的 `barrier`，跑的都是 gloo，传输的是单个整数这样的标量，开销极小。它和前向里的 NCCL 张量通信是两套独立链路。

一个直觉问题先放在脑子里：**rank 0 从 tokenizer 收到 3 条消息，它要怎样让 rank 1 也"恰好收到相同的 3 条"、既不多一条也不少一条？** 带着这个问题读下去。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [python/minisgl/scheduler/io.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py) | `SchedulerIOMixin`，Scheduler 的全部 I/O | `__init__` 动态装配、`_recv_msg_single_rank`、`_recv_msg_multi_rank0/1`、`_reply_tokenizer_rank0/1`、`sync_all_ranks` |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | Scheduler 主类（继承该 Mixin） | `class Scheduler(SchedulerIOMixin)`、`super().__init__` 传入 `tp_cpu_group`、循环里对 `receive_msg` / `send_result` / `sync_all_ranks` 的调用 |
| [python/minisgl/scheduler/config.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py) | 三条 ZMQ 地址属性 | `zmq_backend_addr` / `zmq_detokenizer_addr` / `zmq_scheduler_broadcast_addr` 与 `_unique_suffix` |
| [python/minisgl/utils/mp.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py) | ZMQ 队列封装 | `ZmqPullQueue.get_raw/decode`、`ZmqPubQueue.put_raw`、`ZmqSubQueue.get` 的语义 |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | Engine 初始化通信 | `_init_communication` 如何为广播准备 gloo 进程组 |
| [python/minisgl/server/launch.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py) | 进程启动 | 各 ZMQ 地址的 bind/connect 拓扑、`replace` 保留 `_unique_suffix` |

## 4. 核心概念与源码讲解

本讲按「先看 Mixin 怎么在构造期装配出 `receive_msg`/`send_result`，再看单 rank 的朴素收发，再进入多 rank 的 pub/sub 广播，再讲为什么还要额外 broadcast 一个计数，最后看 `sync_all_ranks`」的顺序展开，对应四个最小模块：`SchedulerIOMixin` 装配、多 rank 广播、broadcast 计数同步、`sync_all_ranks`。

### 4.1 SchedulerIOMixin：构造期装配出的 I/O

#### 4.1.1 概念说明

`SchedulerIOMixin` 是 Scheduler 的一个「能力混入」，只管「消息从哪收、往哪发」这一件事，不碰调度逻辑。它的精妙之处在于：**对外只暴露两个方法 `receive_msg` 和 `send_result`，但这两个方法在不同运行模式下指向完全不同的实现**——而这个「指向」是在 `__init__` 里一次性装配好的。于是主循环（u4-l1）里 `self.receive_msg(...)` 这一行代码永远不变，变的是它背后绑定的具体函数。

装配依据只有两个变量：

- `config.offline_mode`：离线模式（u11-l1 的 `LLM` 类）下，进程内直接收发，不走 ZMQ。
- `config.tp_info.size`（张量并行规模）与 `tp_info.is_primary()`：决定是「单 rank 自己收发」还是「rank 0 对外、其他 rank 从 rank 0 转发」。

这种设计的好处是：主循环、调度器、引擎都不需要 `if size == 1 ... else ...` 散落各处，所有拓扑差异被收敛到 Mixin 的构造函数里。

#### 4.1.2 核心流程

`__init__` 的装配决策树（伪代码）：

```
__init__(config, tp_cpu_group):
  保存 tp_cpu_group                                  # 后面 broadcast/barrier 要用

  if config.offline_mode:                            # 离线模式
    receive_msg = offline_receive_msg                #   交给子类(Llm)实现
    send_result = offline_send_result
    return                                           #   ★提前退出,不建任何 ZMQ socket

  if tp_info.is_primary():                           # rank 0 才连外部
    _recv_from_tokenizer = PULL(bind zmq_backend_addr)        # 收 tokenizer 来的
    _send_into_tokenizer  = PUSH(bind zmq_detokenizer_addr)   # 回送 detokenizer

  # 默认: 单 rank 路径
  receive_msg = _recv_msg_single_rank
  send_result = _reply_tokenizer_rank0

  if tp_info.size > 1:                               # 多 rank 才有广播
    if is_primary():                                 # rank 0
      receive_msg = _recv_msg_multi_rank0
      _send_into_ranks = PUB(bind zmq_scheduler_broadcast_addr)
    else:                                            # rank 1,2,...
      receive_msg = _recv_msg_multi_rank1
      send_result   = _reply_tokenizer_rank1          #   非队长: 结果回送什么也不做
      _recv_from_rank0 = SUB(connect 同一个 broadcast addr)
```

注意三个关键点：①`send_result` 只有两套实现（rank 0 真发、其他 rank 空操作），因为结果只需回送一次；②只有 rank 0 创建了连向 tokenizer/detokenizer 的 socket，其他 rank 对这两个地址**完全不知道**；③离线模式直接 `return`，连 socket 都不建。

#### 4.1.3 源码精读

类与 `__init__` 全貌：

[python/minisgl/scheduler/io.py:15-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L15-L65) —— `SchedulerIOMixin` 与 `__init__`。逐段说明：

- **第 30-33 行（离线短路）**：`offline_mode` 下把两个方法指向 `offline_*` 存根后立刻 `return`，因此离线进程根本不创建 ZMQ socket。这两个存根在 Mixin 里只是占位，真正实现由 `LLM` 子类提供（见 [io.py:70-74](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L70-L74)，`raise NotImplementedError`）。
- **第 35-45 行（rank 0 的两条外连）**：`_recv_from_tokenizer` 是 `ZmqPullQueue(..., create=True)`，即 **bind**（绑定）`zmq_backend_addr`；`_send_into_tokenizer` 是 `ZmqPushQueue(..., create=config.backend_create_detokenizer_link)`，默认 `backend_create_detokenizer_link=True`（见 [config.py:39-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L39-L41)），也是 bind `zmq_detokenizer_addr`。两条都在 `if tp_info.is_primary():` 内，说明**只有 rank 0** 持有对外 socket。
- **第 47-48 行（默认单 rank）**：先把 `recv`/`send` 默认设为单 rank 实现，下面多 rank 分支再按需覆盖。
- **第 49-62 行（多 rank 覆盖）**：`tp_info.size > 1` 时，rank 0 换成 `_recv_msg_multi_rank0` 并新建 `_send_into_ranks`（PUB，bind 广播地址）；非队长 rank 换成 `_recv_msg_multi_rank1`、`send_result` 换成 `_reply_tokenizer_rank1`（空操作），并新建 `_recv_from_rank0`（SUB，connect 同一个广播地址）。
- **第 64-65 行**：最终把选好的 `recv`/`send` 赋给 `self.receive_msg` / `self.send_result`，完成装配。

`Scheduler` 如何把这个 Mixin 接进来：

[python/minisgl/scheduler/scheduler.py:45-76](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L45-L76) —— `class Scheduler(SchedulerIOMixin)`，在 `__init__` 末尾调用 `super().__init__(config, self.engine.tp_cpu_group)`。注意第二个参数 `self.engine.tp_cpu_group` 就是从 Engine 传来的 gloo 进程组，它被 Mixin 存为 `self.tp_cpu_group`，供后面的 `broadcast` / `barrier` 使用。

#### 4.1.4 单 rank 的朴素收发与 `send_result` 分流

装配好之后看最简单的单 rank 路径：

[python/minisgl/scheduler/io.py:79-86](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L79-L86) —— `_recv_msg_single_rank`：`blocking=True` 时先调 `run_when_idle()`（做闲置检查，见 u4-l1 提到的 `check_integrity`）再**阻塞**取一条；随后用 `while not empty()` 把队列里**已经到达**的消息全部非阻塞地取干。返回一个消息列表。

`send_result` 在 rank 0 侧的分流：

[python/minisgl/scheduler/io.py:124-133](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L124-L133) —— `_reply_tokenizer_rank0`：1 条就单发、多条就打包成 `BatchTokenizerMsg` 一并发；`_reply_tokenizer_rank1` 直接 `_ = reply` 啥也不做。这正对应「结果只需回送一次」——detokenizer 是带流式状态的、不可水平扩展（u3-l2），所以多卡时只有 rank 0 负责把 `DetokenizeMsg` 送回去。

#### 4.1.5 代码实践（源码阅读型）

1. **目标**：验证「装配决策树」与实际代码一一对应。
2. **步骤**：
   - 在 [io.py:27-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L27-L65) 旁标注每个 `if` 改写了 `receive_msg` 还是 `send_result`，以及它新建了哪个 socket。
   - 在 [launch.py:59-69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L59-L69) 确认：父进程只构造一次 `server_args`（`_unique_suffix` 取父进程 PID），循环里用 `replace(server_args, tp_info=DistributedInfo(i, world_size))` 派生每个 rank 的配置——`replace` 只改 `tp_info`，**保留 `_unique_suffix`**，所以所有 rank 的三条 ipc 地址完全一致，PUB/SUB 才能 bind 与 connect 到同一个地址。
3. **观察现象**：rank 0 与 rank 1 的 `zmq_scheduler_broadcast_addr` 字符串相同，只是 rank 0 用 `create=True`（bind）、rank 1 用 `create=False`（connect）。
4. **预期结果**：你能说清「地址相同 + 一端 bind 一端 connect」是多 rank PUB/SUB 能连通的前提。
5. 无需运行，纯阅读即可。

#### 4.1.6 小练习与答案

**练习 1**：为什么 `_send_into_tokenizer` 的 `create` 参数要写成 `config.backend_create_detokenizer_link` 而不是固定 `True`？
**答案**：这是一个「谁是 bind 端、谁是 connect 端」的开关。在 SchedulerConfig 里它默认 `True`（[config.py:39-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L39-L41)），即 rank 0 bind、detokenizer connect；这个属性被子类覆写以便在不同部署拓扑下切换 bind/connect 归属，避免两端都 bind 或都 connect 而连不上。

**练习 2**：离线模式为什么可以直接 `return`，不创建任何 ZMQ socket？
**答案**：离线模式（`LLM` 类）在**单进程内**直接收发消息（`offline_receive_msg` / `offline_send_result`，u11-l1 会展开），既没有 tokenizer/detokenizer 子进程，也没有其他 rank，自然不需要任何跨进程 ZMQ 队列。

### 4.2 多 rank 广播：rank 0 pub、其他 rank sub

#### 4.2.1 概念说明

当 `tp_info.size > 1` 时，rank 0 不再独享收到的消息——它必须把每条消息**原样复制**给其余 rank，否则各卡拿到的批次不一致，张量并行的 `all_reduce` 就会对不上。Mini-SGLang 用 ZMQ 的 PUB/SUB 模式做这件事：

- **rank 0** 持有一个 `ZmqPubQueue`（`_send_into_ranks`，bind 广播地址），每收到一条原始字节就 `put_raw` 广播出去。
- **rank 1, 2, ...** 各持有一个 `ZmqSubQueue`（`_recv_from_rank0`，connect 同一广播地址，订阅全部主题），用 `get()` 收。

关键技巧是**搬原始字节**而不是搬对象：rank 0 从自己的 PULL 队列里已经拿到了 msgpack 序列化后的 `bytes`，直接把这串 `bytes` 丢进 PUB，rank 1 收到后用**同一个 decoder** 解码即可，省掉「rank 0 反序列化 → 再序列化 → rank 1 反序列化」的重复开销（这也是 u2-l3 强调 `get_raw` / `put_raw` 的用武之地）。

#### 4.2.2 核心流程

rank 0 的多 rank 收消息（`_recv_msg_multi_rank0`）骨架：

```
_recv_msg_multi_rank0(blocking):
  pending = []
  if blocking:
    raw = _recv_from_tokenizer.get_raw()     # 阻塞取 1 条原始字节
    _send_into_ranks.put_raw(raw)            # ★立刻广播给其他 rank
    pending.append(_recv_from_tokenizer.decode(raw))   # 自己也解码一份

  rest = []
  while not _recv_from_tokenizer.empty():    # 把已到达的也取干
    rest.append(_recv_from_tokenizer.get_raw())

  ★ broadcast(len(rest))                    # 告诉其他 rank「后面还有几条」(下一节展开)

  for raw in rest:
    _send_into_ranks.put_raw(raw)            # 逐条广播
    pending.append(_recv_from_tokenizer.decode(raw))
  return pending
```

rank 1 的多 rank 收消息（`_recv_msg_multi_rank1`）与之**严格对称**：

```
_recv_msg_multi_rank1(blocking):
  pending = []
  if blocking:
    pending.append(_recv_from_rank0.get())   # 阻塞取 1 条(来自 rank 0 的广播)

  ★ dst = broadcast_recv(root=0)             # 收「后面还有几条」
  for _ in range(dst):
    pending.append(_recv_from_rank0.get())   # 按数取
  return pending
```

注意对称性：阻塞分支里两端都恰好处理 1 条；之后的「额外条数」则由 broadcast 对齐（见 4.3）。

#### 4.2.3 源码精读

rank 0 侧：

[python/minisgl/scheduler/io.py:88-107](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L88-L107) —— `_recv_msg_multi_rank0`。

- **第 92-94 行**：阻塞分支里，`get_raw()` 拿到原始 `bytes`，`put_raw(raw)` 直接把这串字节塞进 PUB 队列广播（**没有先 decode 再 encode**），随后再 `decode(raw)` 给自己用。这一行就是「rank 0 收到即转发」的核心。
- **第 96-98 行**：把 PULL 队列里**已经到达**（`empty()` 为假）的剩余消息也取干，存进 `pending_raw_msgs`（注意这里先存 raw，还没广播）。
- **第 100-102 行**：本节先按下不表，见 4.3。
- **第 104-106 行**：对每条剩余 raw，先 `put_raw` 广播、再 `decode` 自己用。

rank 1 侧：

[python/minisgl/scheduler/io.py:109-122](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L109-L122) —— `_recv_msg_multi_rank1`。

- **第 111-113 行**：阻塞分支里从 SUB 队列取 1 条（这条对应 rank 0 第 92-94 行广播的那条）。
- **第 116-118 行**：先按下不表，见 4.3。
- **第 120-121 行**：按广播来的条数 `for _ in range(dst_length)` 从 SUB 取。

底层 PUB/SUB 的 `put_raw` / `get` 语义在封装层：

[python/minisgl/utils/mp.py:117-118](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L117-L118) —— `ZmqPubQueue.put_raw` 直接 `self.socket.send(raw, copy=False)`，原样转发字节。[python/minisgl/utils/mp.py:142-144](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L142-L144) —— `ZmqSubQueue.get` 用同一个 `decoder`（这里是 `BaseBackendMsg.decoder`）解出对象。两端用同一套编解码，所以字节级转发可行。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：确认「字节级转发」能成立的前提是两端用同一个 decoder。
2. **步骤**：
   - 在 [io.py:52-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L52-L53) 看到 rank 0 的 PUB 用 `encoder=BaseBackendMsg.encoder`；在 [io.py:58-62](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L58-L62) 看到 rank 1 的 SUB 用 `decoder=BaseBackendMsg.decoder`。
   - 但注意 rank 0 走的是 `put_raw`（[mp.py:117](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L117)），它**根本没用到 `encoder`**，直接转发 PULL 队列产出的 raw 字节。而那串字节正是 tokenizer 端 `BaseBackendMsg.encoder` + msgpack 编出来的（见 [mp.py:24-26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L24-L26)）。
3. **观察现象**：一条消息的 `bytes` 由 tokenizer 的 encoder 生成，被 rank 0 原封不动转给 rank 1，rank 1 用 `BaseBackendMsg.decoder` 还原——全程只编/解码各一次（rank 0 自己 decode 是第二次，用于本地处理）。
4. **预期结果**：你能画出「tokenizer encode → rank0 raw 转发 → rank1 decode」的字节流，并说明为何这比「每跳都重新序列化」更省。
5. 纯阅读即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 rank 0 在 `put_raw` 之后还要自己 `decode(raw)`？
**答案**：rank 0 既是「转发者」也是「执行者」——它自己也要处理这条消息（放进 `pending_msgs` 返回给主循环），所以必须解码出 Python 对象；而 `put_raw` 只是顺带把原始字节广播给其他 rank，二者共用同一份 raw 字节，各取所需。

**练习 2**：如果某个 rank 1 的 SUB 队列在 rank 0 `put_raw` 之前还没 connect 上会怎样？
**答案**：ZMQ 的 PUB/SUB 默认会丢弃订阅者尚未连上时发送的消息（"slow joiner" 问题）。这正是为什么启动流程要先用 `ack_queue` 等所有子进程就绪、再开始服务（u1-l2 / launch.py），以及为什么消息条数要靠 broadcast 兜底——保证进入循环时所有 SUB 都已就绪。

### 4.3 broadcast 计数同步：为什么同步"条数"而非转发 tensor

这是本讲最关键的一节，也是规格里要求重点讲清的难点。

#### 4.3.1 概念说明

rank 0 已经用 PUB/SUB 把每条消息广播出去了，看起来 rank 1 自己 `get()` 不就能收全吗？为什么还要额外 broadcast 一个「条数」？原因有三层：

1. **PUB/SUB 没有可靠的"队列长度"语义。** rank 0 连续 `put_raw` 了 3 条，rank 1 想知道"还有几条可收"时，只能靠 `ZmqSubQueue.empty()`（[mp.py:146-147](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L146-L147)，本质是 `socket.poll(timeout=0)`）。但 ZMQ 是异步投递，rank 0 刚 `put_raw` 的第 3 条可能还没到 rank 1 的本地缓冲，此时 rank 1 的 `empty()` 会误报"没有了"——它就会少收一条，批次就和 rank 0 不一致。

2. **多卡必须处理"完全相同"的批次。** 张量并行要求每个 rank 独立调度出同一个 batch，再在前向里 `all_reduce`。只要任何一卡少收/多收一条消息，后续 `_schedule_next_batch` 选出的 batch 就会分叉，`all_reduce` 立刻对不上维度，直接报错或算出错误结果。

3. **需要一个确定性的"集合点"。** 于是 rank 0 在取干 PULL 队列后，明确地数出"额外还有 N 条"，用 `torch.distributed.broadcast` 把这个**整数 N** 同步给所有 rank。这是一个 CPU 侧的集合通信（gloo），传的只是一个标量，但它是确定性的：rank 1 拿到 N 后，就**确切知道**要循环 `get()` N 次，再也不会因为投递时延而漏收。

那么——**为什么是广播"条数"，而不是干脆把整条消息用 broadcast/NCCL 转发过去？** 因为这里要同步的是**异构的控制消息**（`UserMsg` 带变长 1D token 张量、`AbortBackendMsg`、`ExitMsg` 等），它们已经被 msgpack 压成一段变长 `bytes`。ZMQ pub/sub 天生擅长搬运这种变长字节流（且天然 1 对多）；而 NCCL / `torch.distributed.broadcast` 擅长的是**同构的大块 GPU 张量**（前向里的激活、权重梯度），传变长 bytes 反而别扭。所以工程上的分工是：**ZMQ 搬消息体（变长 bytes），gloo broadcast 搬"有几条"（一个 int）**——各用最合适的工具，且两者解耦。

一句话总结：broadcast 的不是消息内容，而是**消息条数**这个"元信息"，用来给不可靠的 SUB `empty()` 兜底，保证各 rank 收到**等数量、等内容**的批次。

#### 4.3.2 核心流程

计数同步的时序（以 blocking=True、PULL 里除了阻塞取到的那条还积压了 2 条为例）：

```
rank 0 (_recv_msg_multi_rank0)          rank 1+ (_recv_msg_multi_rank1)
─────────────────────────────────       ─────────────────────────────────
① get_raw() → raw₀ (阻塞,取 1 条)        ① get() → msg₀ (阻塞,收 1 条)
② put_raw(raw₀) 广播 ──────────────►     (SUB 收到 msg₀)
③ decode(raw₀) → 自己用                  ③ (pending 已有 1 条)
   while not empty: 取干 → [raw₁, raw₂]
④ src_tensor = tensor(2)                 ④ dst_tensor = tensor(-1)
   broadcast(src_tensor, root=0) ◄═══►   broadcast(dst_tensor, root=0)
   wait()                                  wait()  → dst_length = 2
⑤ for raw in [raw₁, raw₂]:               ⑤ for _ in range(2):
     put_raw(raw) 广播 ───────────►         get() → msg₁, msg₂
     decode(raw) → 自己用
⑥ 返回 [msg₀, msg₁, msg₂] (3 条)         ⑥ 返回 [msg₀, msg₁, msg₂] (3 条)
```

两端最终都拿到**完全相同的 3 条消息**。第 ④ 步的 broadcast 就是那个确定性的"集合点"。

#### 4.3.3 源码精读

rank 0 的计数广播：

[python/minisgl/scheduler/io.py:100-102](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L100-L102) —— `src_tensor = torch.tensor(len(pending_raw_msgs))` 把"额外条数"装进一个标量张量，`self.tp_cpu_group.broadcast(src_tensor, root=0).wait()` 以 rank 0 为根把它广播出去。`.wait()` 阻塞到所有 rank 都收到这个数，确保后续的 `put_raw` / `get` 两端步调一致。

rank 1 的计数接收：

[python/minisgl/scheduler/io.py:116-118](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L116-L118) —— `dst_tensor = torch.tensor(-1)`（初值 -1 是哨兵，便于发现"没被覆盖"的异常），同样的 `broadcast(dst_tensor, root=0).wait()` 把根上的值复制过来，`int(dst_tensor.item())` 取出整数 `dst_length`。随后 [io.py:120-121](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L120-L121) 的 `for _ in range(dst_length)` 就精确地收这么多条。

这里用的 `tp_cpu_group` 是 Engine 在初始化时准备好的 gloo 进程组：

[python/minisgl/engine/engine.py:112-137](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L112-L137) —— `_init_communication`。两条分支殊途同归地保证 `tp_cpu_group` 是一个 **gloo**（CPU）进程组：要么整体用 gloo（`size==1` 或 `use_pynccl` 时，第 114-121 行），要么在 NCCL 之外额外 `new_group(backend="gloo")`（第 135 行）。所以本讲的 `broadcast` / `barrier` 永远是 CPU 侧的轻量集合通信，与前向里的 NCCL 张量通信互不干扰。

#### 4.3.4 代码实践（重点：画 tp=2 时序并解释）

这是本讲的核心实践，对应规格里的练习任务。

1. **目标**：画出 `tp=2`（rank 0 + rank 1）下一轮 `receive_msg(blocking=True)` 的消息收发时序，并回答"为何用 broadcast 同步条数而非直接转发 tensor"。
2. **步骤**：
   - 假设此刻 PULL 队列（rank 0 的 `_recv_from_tokenizer`）里积压了 2 条 `UserMsg`（`uid=7`、`uid=8`），且本轮 `blocking=True`。
   - 在纸上按 4.3.2 的时序图，把 rank 0 与 rank 1 两列对齐，标注每一步用了哪条链路：`①②⑤` 走 ZMQ（PULL→PUB→SUB），`④` 走 gloo broadcast。
   - 标出每一步两端各自 `pending_msgs` 的长度变化。
3. **需要观察的现象**：
   - rank 0 第 ① 步阻塞取 1 条（这条是本轮"必须等到"的新消息），第 ③ 步又取干 2 条；broadcast 的是 `2`（额外条数），不是 `3`（总数）。
   - rank 1 第 ① 步也阻塞收 1 条，第 ④ 步收到 `dst_length=2`，第 ⑤ 步再收 2 条。
4. **预期结果（时序图）**：

```
时间 ──►

rank 0 (对外 + 广播)                     rank 1 (只从 rank 0 收)
────────────────────────────             ────────────────────────────
① PULL.get_raw() → uid=7  (阻塞)        ① SUB.get() → uid=7  (阻塞)
② PUB.put_raw(uid=7)  ───ZMQ───►        (SUB 缓冲 uid=7)
③ PULL 取干 → [uid=8]...假设还有 uid=9
   pending_raw_msgs = [uid=8, uid=9]
④ broadcast(2)            ══gloo══►     ④ broadcast → dst_length=2
⑤ PUB.put_raw(uid=8)  ───ZMQ───►        ⑤ SUB.get() → uid=8
   PUB.put_raw(uid=9)  ───ZMQ───►           SUB.get() → uid=9
   返回 [uid=7,8,9]                       返回 [uid=7,8,9]
```

5. **回答核心问题——为何用 broadcast 同步"条数"而非直接转发 tensor**：
   - **(a) 不可靠性兜底**：ZMQ pub/sub 是异步投递，rank 1 用 `empty()` 判断"还有几条"会被时延欺骗（第 ⑤ 步的消息可能还没到本地缓冲）。broadcast 一个确切的整数 N，让 rank 1 用 `for _ in range(N)` 精确接收，杜绝漏收。
   - **(b) 数据类型不匹配**：要同步的是 msgpack 编码后的**变长 bytes**（异构控制消息），不是同构 GPU 张量。ZMQ pub/sub 天生适合搬变长字节，而 NCCL/`broadcast` 适合搬同构大张量。把变长 bytes 硬塞进 tensor 通信既别扭又低效。
   - **(c) 职责解耦**：消息**内容**靠 ZMQ 1 对多复制（rank 0 已经在搬字节），消息**数量**靠 gloo broadcast 兜底（一个 int）。两者用最合适的工具，互不耦合——rank 0 即使再多发几条，只要 broadcast 的数对上，rank 1 就能收全。
   - **(d) 不重不漏才能对齐批次**：多卡要求各 rank 调度出**完全相同**的 batch。任何一卡多收或少收一条，`_schedule_next_batch` 选出的 batch 维度就会分叉，前向的 `all_reduce` 立刻对不上。broadcast 条数是保证"等数量"的最低成本保险。
6. 若本地有 GPU，可选运行 `--tensor-parallel-size 2` 并对照日志确认两端收到相同 `uid` 列表；若无 GPU，结论由阅读得出，"待本地验证"数值。

#### 4.3.5 小练习与答案

**练习 1**：如果把 [io.py:100-102](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L100-L102) 的 broadcast 整段删掉，让 rank 1 改用 `while not self._recv_from_rank0.empty(): get()` 来取剩余消息，会出什么问题？
**答案**：典型的竞态——rank 0 连续 `put_raw` 的某条消息可能还没到 rank 1 的 SUB 本地缓冲，rank 1 的 `empty()` 此刻为真就提前退出循环，少收若干条；于是 rank 1 调度出的 batch 比 rank 0 小，前向 `all_reduce` 维度不匹配，报错或算错。broadcast 条数正是为了避免这种时延带来的漏收。

**练习 2**：`dst_tensor = torch.tensor(-1)` 里的 `-1` 有什么用？
**答案**：它是一个哨兵初值。如果 broadcast 因故没有把根上的值写进来（理论上不应发生），`-1` 会让随后的 `range(dst_length)` 立刻暴露异常（`range(-1)` 为空、或语义不对），比用 `0` 更容易被发现排查。

### 4.4 sync_all_ranks：CPU 侧的 barrier

#### 4.4.1 概念说明

除了收消息时的 broadcast，Mixin 还提供一个更简单的同步原语 `sync_all_ranks`：它就是一个 `barrier`——所有 rank 都到达这一行后才能一起继续。它复用的同样是 4.3 里那条 gloo CPU 进程组，开销极小（只同步、不传数据）。

`sync_all_ranks` 在两个地方被调用：①启动后所有 rank 就绪的对齐（`launch_server` 里 `Scheduler` 构造完立刻 `sync_all_ranks()`，保证没有任何 rank 抢跑）；②`shutdown` 时做一次收尾同步（确保各 rank 都把活干完再退出）。

#### 4.4.2 核心流程

```
sync_all_ranks():
  self.tp_cpu_group.barrier().wait()     # 所有 rank 到齐才放行
```

`barrier()` 返回一个 Work 对象，`.wait()` 阻塞当前 rank 直到全部 rank 都抵达 barrier。

#### 4.4.3 源码精读

[python/minisgl/scheduler/io.py:76-77](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L76-L77) —— `sync_all_ranks` 的全部实现就一行：`self.tp_cpu_group.barrier().wait()`。

启动时的调用点：

[python/minisgl/server/launch.py:21-25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L21-L25) —— `_run_scheduler` 里构造完 `Scheduler`（含 Mixin 的 I/O 装配与 Engine 的通信初始化）后，立刻 `scheduler.sync_all_ranks()`，**然后**才由 rank 0 往 `ack_queue` 放 "Scheduler is ready"。这保证：父进程收到 ack 时，所有 rank 的 socket、进程组、KV cache 都已就绪，避免 rank 0 抢跑广播、其他 rank 还没 connect 上的 slow-joiner 问题（与 4.2 的隐患呼应）。

收尾时的调用点：

[python/minisgl/scheduler/scheduler.py:133-136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L133-L136) —— `shutdown` 先 `torch.cuda.synchronize` 等 GPU 静默，再 `sync_all_ranks()` 让各 rank 对齐，最后 `engine.shutdown()`。一次有序的收尾。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：找出 `sync_all_ranks` 的全部调用点，理解它为何出现在那些位置。
2. **步骤**：用检索工具搜 `sync_all_ranks` 的引用，应定位到 `io.py`（定义）、`launch.py`（启动）、`scheduler.py` 的 `shutdown`（收尾）三处。
3. **观察现象**：两处调用都在「循环之外」——启动后、关闭前，而不是每轮循环里。每轮循环里的同步靠的是 `receive_msg` 内部的 broadcast，不需要额外 barrier。
4. **预期结果**：你能区分「每轮的细粒度同步（broadcast 条数）」与「生命周期级同步（barrier）」的不同用途。
5. 纯阅读即可。

#### 4.4.5 小练习与答案

**练习 1**：`sync_all_ranks` 用的 `barrier` 是跑在 GPU 上还是 CPU 上？为什么这里不用 NCCL？
**答案**：跑在 CPU（gloo）上，因为 `tp_cpu_group` 是 gloo 进程组（见 4.3.3 的 `_init_communication`）。这里只是要"等所有 rank 到齐"，不传任何数据，用 CPU barrier 足矣且更轻；NCCL 的 a barrier 反而需要 GPU 上下文，没必要。

**练习 2**：如果删掉 `launch.py` 里构造后的那次 `sync_all_ranks()`，最可能在哪里出问题？
**答案**：rank 0 可能在其他 rank 的 SUB 还没 connect 到广播地址时就开始第一轮 `receive_msg` 并 `put_raw`，那条消息会被 ZMQ pub/sub 丢弃（slow joiner），导致 rank 1 漏收、批次分叉。`sync_all_ranks` 保证所有 rank 的通信设施都就绪后才允许任何人开跑。

## 5. 综合实践

把本讲四个模块串起来，做一个 **tp=2 消息收发全链路追踪 + 拓扑图绘制** 的任务。

**任务**：基于真实源码，绘制 `--tensor-parallel-size 2` 下「一条 `UserMsg` 的完整收发链路」，并标注每一段用 ZMQ 还是 gloo，最后用一段话解释"广播条数"的必要性。

**步骤**：

1. **画拓扑图（进程 + ZMQ 地址）**。先从 [config.py:23-33](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L23-L33) 抄下三条 ipc 地址：
   - `zmq_backend_addr` = `ipc:///tmp/minisgl_0<pid>`
   - `zmq_detokenizer_addr` = `ipc:///tmp/minisgl_1<pid>`
   - `zmq_scheduler_broadcast_addr` = `ipc:///tmp/minisgl_2<pid>`
2. **标注每端的 socket 类型与 bind/connect**，依据 [io.py:35-62](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L35-L62) 与 [launch.py:73-103](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L73-L103)：
   - `minisgl_0`：tokenizer PUSH（connect）→ rank0 PULL（bind）。
   - `minisgl_1`：rank0 PUSH（bind）→ detokenizer PULL（connect）。
   - `minisgl_2`：rank0 PUB（bind）→ rank1 SUB（connect）。
3. **画时序图**：复用 4.3.4 的时序，把一条入站 `UserMsg`（tokenizer → rank0 PULL）的"广播给 rank1 + broadcast 条数对齐"过程画出来，再把一条出站 `DetokenizeMsg`（rank0 PUSH → detokenizer）画上去——注意出站**只有 rank0** 发，rank1 在 `_reply_tokenizer_rank1` 里静默（[io.py:132-133](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L132-L133)）。
4. **写结论**：用 4.3.4 第 5 点的四条理由（不可靠性兜底 / 数据类型不匹配 / 职责解耦 / 对齐批次）解释"为何用 broadcast 同步条数而非直接转发 tensor"。

**需要观察的现象**：

- 入站链路里，ZMQ（pub/sub 搬消息体）与 gloo（broadcast 搬条数）分工明确，且二者都必须存在。
- 出站链路里，只有 rank0 一条线回 detokenizer，rank1 完全不参与回送。
- NCCL（前向的 `all_reduce`）在本讲的收发链路里**完全没出现**——它是另一条干道，藏在 Engine 前向内部（u5/u9）。

**预期结果**：一张图分清「控制面（ZMQ + gloo broadcast/barrier）」与「数据面（NCCL 张量）」；一段话讲清 broadcast 条数的必要性。若本地有 GPU，可启动 `tp=2` 服务并在 rank0 的 `_recv_msg_multi_rank0` 临时加 `logger.debug` 打印 `len(pending_raw_msgs)`，对照 rank1 收到的 `dst_length` 是否一致；无 GPU 则结论由阅读得出，数值"待本地验证"。

## 6. 本讲小结

- `SchedulerIOMixin` 在**构造期**根据 `offline_mode` 与 `tp_info.size` 把 `receive_msg` / `send_result` 动态绑定到不同实现，让主循环代码对所有拓扑保持不变。
- **rank 0 对外、其他 rank 静默**：只有 rank 0 持有连向 tokenizer/detokenizer 的 PULL/PUSH socket，结果回送（`send_result`）也只有 rank 0 真正执行，rank 1+ 的 `_reply_tokenizer_rank1` 是空操作。
- 多 rank 下 rank 0 用 ZMQ **PUB/SUB** 把每条**原始 msgpack 字节**（`get_raw` / `put_raw`）广播给其他 rank，字节级转发省去重复编解码。
- 本讲核心：rank 0 再用 gloo **broadcast 同步"消息条数"**（一个 int），给不可靠的 SUB `empty()` 兜底，保证各 rank 收到**等数量、等内容**的批次——这是张量并行对齐 batch 的前提。
- broadcast 同步的是"条数"而非 tensor，因为消息是变长 bytes（适合 ZMQ），而 NCCL/broadcast 适合同构大张量；二者职责解耦、各用最合适的工具。
- `sync_all_ranks` 是一行 gloo `barrier`，用于启动后与关闭前的生命周期级同步，确保所有 rank 通信就绪后才开跑、避免 slow-joiner 丢消息。

## 7. 下一步学习建议

- 想知道 `receive_msg` 拿到 `UserMsg` 后，`_process_one_msg` 如何入队、`_schedule_next_batch` 如何挑批，请读 **u4-l3 Prefill 调度与 Chunked Prefill** 与 **u4-l4 Decode 调度、TableManager 与 TokenPool**。
- 想知道前向里 NCCL 的 `all_reduce` / `all_gather` 这条"数据面"干道如何实现，请读 **u9-l1 张量并行 Linear 与分布式通信**（`DistributedCommunicator` 与 PyNCCL）。
- 想知道离线模式如何把 `offline_receive_msg` / `offline_send_result` 这两个存根真正实现（绕过 ZMQ 在进程内收发），请读 **u11-l1 LLM 离线推理接口与基准**。
- 推荐顺带重读 **u2-l3 进程间消息与序列化**，把本讲的 `get_raw`/`put_raw` 字节级转发与 `serialize_type`/`deserialize_type` 的编解码机制对上。
