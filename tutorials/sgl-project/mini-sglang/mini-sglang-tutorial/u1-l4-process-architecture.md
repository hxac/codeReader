# 进程架构与请求生命周期

## 1. 本讲目标

本讲是理解 Mini-SGLang 的「骨架」篇。读完本讲，你应该能够：

- 说清 Mini-SGLang 由哪几类进程组成、它们的数量关系是什么（特别是「每个 GPU 一个 Scheduler」）。
- 区分两条通信干道：**ZMQ 负责控制消息、NCCL 负责重型张量数据**，并知道它们各自出现在数据流的哪一段。
- 逐步复述一个请求从用户发出到收到回复的 **8 步生命周期**。
- 对照 `launch_server` 的真实源码，画出进程拓扑和 ZMQ 地址连接图。

本讲只讲「宏观架构与数据流向」，不深入任何一个进程的内部算法（Scheduler 主循环、Engine 前向、KV cache 等都有后续专讲）。

## 2. 前置知识

本讲用到的几个基础概念，先用最直白的方式解释：

- **进程（process）**：操作系统里独立运行的程序实例，拥有自己的内存空间。Mini-SGLang 故意把不同工作拆到不同进程里，让 CPU 工作（如分词）和 GPU 工作（如模型计算）互不阻塞。
- **多进程 vs 多线程**：多线程共享同一份内存，多进程各自独立。Python 因为有 GIL（全局解释器锁），多线程不能真正并行跑 CPU 密集任务，所以推理框架普遍用多进程。
- **LLM 推理两阶段**：`prefill`（处理输入 prompt，一次性算完）和 `decode`（逐个生成新 token）。本讲只关心请求在进程间怎么流动，不关心两阶段细节。
- **TP（Tensor Parallelism，张量并行）**：把一个模型切成几份，分别放到几张 GPU 上同时算，算完再把结果合并。`--tp-size` 就是 GPU 数量。
- **ZMQ（ZeroMQ）**：一个轻量级消息库，像「邮政系统」——发件人把信投进信箱，收件人从信箱取信。本讲里它走 `ipc://`（本机进程间通信）地址。
- **NCCL**：NVIDIA 的 GPU 间高速通信库，专门传大块张量数据，比 ZMQ 快得多，但只能在 GPU 之间用。

如果你对「请求怎么变成 token」还不熟悉，可以回头先看 [u1-l3 目录结构与模块地图](u1-l3-directory-structure.md)，本讲承接它建立的模块地图。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `python/minisgl/server/launch.py` | **总编排器**：解析参数，用 `multiprocessing` 启动所有子进程，等待它们就绪后再开服务。 |
| `python/minisgl/server/args.py` | 定义 `ServerArgs` 配置类，包含所有 ZMQ 地址属性与 CLI 参数。 |
| `python/minisgl/scheduler/config.py` | `SchedulerConfig` 定义三条 ZMQ 地址（backend / detokenizer / broadcast）与 pid 后缀。 |
| `python/minisgl/distributed/info.py` | `DistributedInfo(rank, size)` 描述「我是第几张卡、一共几张卡」。 |
| `python/minisgl/scheduler/io.py` | `SchedulerIOMixin`：Scheduler 怎么收消息、怎么在多 rank 间广播、怎么回送结果。 |
| `python/minisgl/distributed/impl.py` | `DistributedCommunicator`：TP 的 all_reduce / all_gather，在 torch.distributed 与 PyNCCL 间切换。 |
| `python/minisgl/tokenizer/server.py` | `tokenize_worker`：同一个函数既能分词也能反分词，按消息类型分流。 |
| `python/minisgl/server/api_server.py` | `run_api_server` / `FrontendManager`：FastAPI 前端，用 ZMQ 队列连到 tokenizer。 |
| `python/minisgl/scheduler/scheduler.py` | `Scheduler.__init__` 与 `run_forever`：每个 GPU 进程的主循环。 |
| `docs/structures.md` | 官方架构说明，含请求生命周期 8 步描述。 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**进程模型**、**TP rank**、**ZMQ/NCCL 通信分工**、**请求生命周期**。

### 4.1 进程模型：四类进程

#### 4.1.1 概念说明

Mini-SGLang 不是「一个大程序跑到底」，而是「一群分工的小进程」协同工作。官方把它们分成四类（见 [docs/structures.md:7-12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md#L7-L12)）：

1. **API Server（前端）**：用户入口，提供 OpenAI 兼容的 HTTP 接口（如 `/v1/chat/completions`），接收 prompt、返回生成文本。它跑在主进程里。
2. **Tokenizer Worker**：把用户输入的文本转成模型能理解的数字（token id）。
3. **Detokenizer Worker**：把模型生成出来的数字 token 转回人类可读的文本。
4. **Scheduler Worker**：核心计算进程，负责排队、调度、调用 GPU 算下一个 token。**多 GPU 时，每张 GPU 一个 Scheduler。**

为什么要拆开？核心动机是**让 CPU 任务和 GPU 任务互不阻塞**：分词/反分词是纯 CPU 工作，模型前向是 GPU 工作。如果放在一个进程里，分词时 GPU 会空闲；拆开后，一边分词一边还能在 GPU 上算上一批。

> 小贴士：在本项目里，Tokenizer 和 Detokenizer 其实由**同一个函数** `tokenize_worker` 实现，只是按消息类型分流。详见 [u3-l2](u3-l2-tokenizer-worker.md)。

#### 4.1.2 核心流程

进程的启动由 `launch_server` 编排，流程是：

```text
launch_server()
  ├── parse_args()           # 解析 CLI，得到 ServerArgs
  ├── run_api_server(...)    # 在主进程启动 FastAPI 前端
  │     └── start_subprocess()   # 前端就绪前，先拉起所有后台进程
  │           ├── 启动 world_size 个 Scheduler 进程
  │           ├── 启动 1 个 Detokenizer 进程
  │           ├── 启动 num_tokenizers 个 Tokenizer 进程
  │           └── 阻塞等待 ack（就绪回执）收齐
  └── uvicorn.run(app)       # ack 齐了，才开始对外服务
```

进程数量满足一个简单公式：

\[ \text{后台进程总数} = \text{world\_size} + \text{num\_tokenizers} + 1 \]

其中 `world_size` 就是 `--tp-size`（GPU 数），`num_tokenizers` 默认是 0（此时分词工作并入那 1 个 detokenizer 进程）。

#### 4.1.3 源码精读

总编排函数 [launch.py:40-113](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L40-L113) 内部的 `start_subprocess` 是关键。它先设置 `spawn` 启动方式，再依次拉起三类进程。

**第一类：Scheduler 进程**（[launch.py:59-69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L59-L69)）。循环 `world_size` 次，每次用 `mp.Process` 启动一个 `_run_scheduler`，并给它一个不同的 `tp_info`：

```python
world_size = server_args.tp_info.size
for i in range(world_size):
    new_args = replace(server_args, tp_info=DistributedInfo(i, world_size))
    mp.Process(target=_run_scheduler, args=(new_args, ack_queue),
               name=f"minisgl-TP{i}-scheduler").start()
```

注意 `replace(server_args, tp_info=DistributedInfo(i, world_size))`：每个 scheduler 进程拿到**同一份配置**，但 `tp_info.rank` 被改成自己的序号 `i`。这就是「每个 GPU 一个 Scheduler、各自知道自己是第几张卡」的实现方式。

**第二类：Detokenizer 进程**（[launch.py:73-87](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L73-L87)），永远只有 1 个，名字固定 `minisgl-detokenizer-0`，`tokenizer_id` 被设为 `num_tokenizers`。

**第三类：Tokenizer 进程**（[launch.py:88-103](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L88-L103)），循环 `num_tokenizers` 次。当 `num_tokenizers=0`（默认）时，这个循环根本不执行，分词工作由上面那个 detokenizer 进程一并承担。

最后是**就绪同步**（[launch.py:110-111](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L110-L111)）：主进程阻塞地从 `ack_queue` 取 `num_tokenizers + 2` 条「我准备好了」消息。这个数字 = 1（只有 rank0 scheduler 发 ack）+ num_tokenizers + 1（detokenizer）。没收齐就不开 uvicorn，保证不会接到还没就绪的请求。

#### 4.1.4 代码实践

**实践目标**：用源码验证「进程数量公式」。

**操作步骤**：

1. 打开 [launch.py:59-111](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L59-L111)。
2. 分别数清楚：`mp.Process(...)` 被调用了几次？分别在哪些循环里？
3. 思考三种典型配置下后台进程总数：
   - `--tp-size 1`（默认，单卡）：world_size=1，num_tokenizers=0。
   - `--tp-size 2`：world_size=2，num_tokenizers=0。
   - `--tp-size 2 --num-tokenizer 2`：world_size=2，num_tokenizers=2。

**需要观察的现象 / 预期结果**：

- 单卡默认：`1(scheduler) + 0 + 1(detokenizer) = 2` 个后台进程。
- 双卡默认：`2 + 0 + 1 = 3` 个后台进程。
- 双卡 + 2 tokenizer：`2 + 2 + 1 = 5` 个后台进程。

ack 等待数始终是 `num_tokenizers + 2`（因为多 rank 时只有 rank0 scheduler 发 ack）。如果无法在本地跑多卡，这条结论可通过纯阅读源码得出，无需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ack 数是 `num_tokenizers + 2` 而不是 `world_size + num_tokenizers + 1`？

**答案**：因为只有 **rank 0 的 scheduler** 会往 `ack_queue` 发就绪消息（见 [launch.py:24-25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L24-L25) 里 `if args.tp_info.is_primary(): ack_queue.put(...)`）。其余 rank 通过 `sync_all_ranks()` 的 barrier 隐式同步，不发 ack。所以 ack = 1(rank0) + num_tokenizers + 1(detokenizer)。

**练习 2**：默认配置下（`num_tokenizer=0`）一共有几个 `tokenize_worker` 进程在跑？

**答案**：1 个。虽然代码里写了「detokenizer」和「tokenizer」两组 `mp.Process`，但 tokenizer 那组的循环 `for i in range(0)` 不执行，只剩 detokenizer 那一个进程，它同时承担分词和反分词。

---

### 4.2 TP Rank：一个 GPU 一个 Scheduler

#### 4.2.1 概念说明

「TP rank」就是张量并行里的「卡号」。`rank=0` 是第 0 张 GPU，`rank=1` 是第 1 张，依此类推。Mini-SGLang 给每张 GPU 启动一个独立的 Scheduler 进程，于是 rank 既是 GPU 编号，也是进程编号。

rank 之间**不是平等的**，rank 0 扮演「队长」角色，承担所有与外部（tokenizer/detokenizer）的交互；其他 rank 只跟 rank 0 通信。这种「主从」设计避免了多张卡同时去抢 tokenizer 造成的混乱。

承载这个概念的数据结构是 `DistributedInfo`，见 [distributed/info.py:6-15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/info.py#L6-L15)：

```python
@dataclass(frozen=True)
class DistributedInfo:
    rank: int
    size: int
    def is_primary(self) -> bool:
        return self.rank == 0
```

只有两个字段：`rank`（我是谁）和 `size`（一共几个），外加 `is_primary()` 判断「我是不是 rank 0」。

#### 4.2.2 核心流程

rank 的赋值与使用链路：

```text
CLI: --tensor-parallel-size N
   └─> parse_args 里先造一个 rank=0 的占位 tp_info   (args.py:262)
        └─> launch_server 里 for i in range(N):
               给第 i 个 scheduler 进程注入 tp_info=DistributedInfo(i, N)  (launch.py:60-63)
                  └─> Scheduler 进程内：is_primary() 决定要不要发 ack、要不要收发外部消息
```

单卡（`size==1`）时 rank 恒为 0，所有「多 rank 广播」逻辑都被跳过，整个系统退化成最简单的单进程后端。

#### 4.2.3 源码精读

**rank 的初始注入**在 [args.py:262-263](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L262-L263)：

```python
kwargs["tp_info"] = DistributedInfo(0, kwargs["tensor_parallel_size"])
del kwargs["tensor_parallel_size"]
```

注意这里 `--tensor-parallel-size`（CLI 名）被转成了 `tp_info`（代码里用的名字），而且**初始 rank 写死为 0**——因为主进程自己只代表 rank 0。真正的「每进程不同 rank」是在 `launch_server` 里逐个 `replace` 出来的（见 4.1.3）。

**rank 0 的特权**体现在 `_run_scheduler`（[launch.py:16-37](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L16-L37)）：

```python
scheduler = Scheduler(args)
scheduler.sync_all_ranks()              # 所有 rank 在这里对齐
if args.tp_info.is_primary():           # 只有 rank 0
    ack_queue.put("Scheduler is ready") # 向主进程报告就绪
...
scheduler.run_forever()
```

而在 I/O 层（下一个模块详讲），rank 0 还独享「从 tokenizer 收消息」「向 detokenizer 发结果」的权利。

`DistributedInfo` 还带一个**断言保护**（[info.py:11-12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/info.py#L11-L12)）：`assert 0 <= self.rank < self.size`，防止构造出非法的 rank（比如 rank=2 但 size=2）。

#### 4.2.4 代码实践

**实践目标**：理解 `size==1` 时多 rank 逻辑如何被跳过。

**操作步骤**：

1. 阅读 [io.py:27-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L27-L65) 的 `SchedulerIOMixin.__init__`。
2. 关注这两处分支：
   - `if tp_info.is_primary():` —— 只有 rank 0 创建 `_recv_from_tokenizer` 和 `_send_into_tokenizer`。
   - `if tp_info.size > 1:` —— 只有 size>1 才创建广播/订阅队列，否则用单 rank 版本。

**需要观察的现象 / 预期结果**：单卡时，`receive_msg` 被绑定为 `_recv_msg_single_rank`，`send_result` 绑定为 `_reply_tokenizer_rank0`，广播相关的 `_send_into_ranks` / `_recv_from_rank0` 根本不会被创建。这说明**单卡用户走的代码路径比多卡短得多**，理解单卡就理解了架构主干。

#### 4.2.5 小练习与答案

**练习 1**：如果用户运行 `--tp-size 0` 会发生什么？

**答案**：`DistributedInfo(0, 0)` 的 `__post_init__` 断言 `0 <= rank < size` 即 `0 <= 0 < 0` 为假，会直接 `AssertionError`。这是项目用断言做的早期参数校验。

**练习 2**：为什么不让每张卡都直接连 tokenizer，而要 rank 0 当「队长」？

**答案**：让 N 张卡同时去 tokenizer 拉消息，会导致同一批请求被重复拉取、各卡批次不一致。让 rank 0 唯一地收消息并广播给其他 rank，能保证**所有 rank 处理完全相同的批次**——这是张量并行正确性的前提（每张卡算的是同一个 batch 的不同切片）。

---

### 4.3 ZMQ 与 NCCL 的通信分工

#### 4.3.1 概念说明

四类进程之间需要交换两种「重量」完全不同的东西：

- **轻量控制消息**：请求元数据（uid、采样参数）、分词后的 token id 列表、生成的 token、中止信号。体积小、频率高、走 CPU。
- **重量张量数据**：模型前向过程中，各张 GPU 算出的中间激活（比如 MLP 的部分和），需要合并（all_reduce）或拼接（all_gather）。体积大、必须走 GPU 显存。

Mini-SGLang 给这两类数据用**两条完全不同的干道**（见 [docs/structures.md:14-16](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md#L14-L16)）：

| 干道 | 传输内容 | 实现库 | 走在哪 |
| --- | --- | --- | --- |
| **ZMQ** | 控制消息、token、元数据 | ZeroMQ（`ipc://` 本机套接字） | CPU 进程之间 |
| **NCCL** | 重型张量 all_reduce / all_gather | `torch.distributed` 或 PyNCCL | GPU 显存之间 |

一句话记忆：**ZMQ 管「信件」，NCCL 管「货运」**。

#### 4.3.2 核心流程

**ZMQ 侧**有 5 条命名的 ipc 通道，名字是 `ipc:///tmp/minisgl_N` 加上一个 pid 后缀（保证多次启动不撞地址）：

| 属性名 | ipc 文件 | 连接方向 | 用途 |
| --- | --- | --- | --- |
| `zmq_backend_addr` | `minisgl_0` | Tokenizer → rank0 Scheduler | 把分词结果送进后端 |
| `zmq_detokenizer_addr` | `minisgl_1` | rank0 Scheduler → Detokenizer | 把待反分词的 token 送出 |
| `zmq_scheduler_broadcast_addr` | `minisgl_2` | rank0 Scheduler → rank1+ Scheduler | 广播请求给其他卡 |
| `zmq_frontend_addr` | `minisgl_3` | Detokenizer → API Server | 把回复文本送回前端 |
| `zmq_tokenizer_addr` | `minisgl_4` | API Server → Tokenizer | 把用户文本送去分词（仅多 tokenizer 时独立） |

这 5 条地址的来源见 [scheduler/config.py:24-33](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L24-L33) 和 [args.py:26-51](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L26-L51)。

> 默认配置（`num_tokenizer=0`）下，`zmq_tokenizer_addr` 被故意设成和 `zmq_detokenizer_addr` 同一个地址（见 [args.py:29-35](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L29-L35)），由那唯一的共享进程一并处理，省掉一条通道。

**多 rank 广播的精妙之处**：rank0 要把同一批消息发给 N-1 张卡。它用的是 **ZMQ pub/sub**（一发多收）+ **torch.distributed broadcast**（同步消息条数）的组合：

```text
rank0:                                   rank1, rank2, ...
  从 minisgl_0 收到 K 条原始消息            |
  broadcast(K)  ────────────────────────> 收到 K（一个标量）
  for 每条消息: pub 到 minisgl_2           for K 次: sub 从 minisgl_2 收
```

**为什么要广播「条数」而不是直接转发张量？** 这是一个值得理解的原理：ZMQ 的 pub/sub 是「即发即弃」的，订阅方何时连上、何时收到没有强保证。如果不先用一个带确认的通道约定好「这轮一共 K 条」，rank1 就不知道要等几条消息才算齐。`broadcast(K)` 用的是 `torch.distributed` 的 CPU 进程组（`tp_cpu_group`），它**自带阻塞同步语义**，正好用来做这个「点齐人数」的动作。真正的大块消息字节仍由 ZMQ 高效搬运。

#### 4.3.3 源码精读

**rank0 收消息并广播**（[io.py:88-107](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L88-L107)）核心片段：

```python
src_tensor = torch.tensor(len(pending_raw_msgs))           # 这轮收到几条
self.tp_cpu_group.broadcast(src_tensor, root=0).wait()     # 广播条数给所有 rank
for raw in pending_raw_msgs:
    self._send_into_ranks.put_raw(raw)                     # ZMQ pub 每条原文
    pending_msgs.append(self._recv_from_tokenizer.decode(raw))
```

**rank1+ 收消息**（[io.py:109-122](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L109-L122)）：

```python
dst_tensor = torch.tensor(-1)
self.tp_cpu_group.broadcast(dst_tensor, root=0).wait()     # 收到条数 K
dst_length = int(dst_tensor.item())
for _ in range(dst_length):                                # 按 K 条订阅
    pending_msgs.append(self._recv_from_rank0.get())
```

**回送结果的不对称**：只有 rank0 把结果发回 detokenizer，其他 rank 啥也不做（[io.py:124-133](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L124-L133)）。因为张量并行下，最终 token 由 rank0 汇总，发一份回去就够了。

**NCCL 侧**——重型张量通信抽象在 [impl.py:63-90](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L63-L90)。`DistributedCommunicator` 维护一个「插件栈」，默认是 `TorchDistributedImpl`（用 `torch.distributed`，底层走 NCCL）；调用 `enable_pynccl_distributed` 后会**追加**一个 `PyNCCLDistributedImpl` 到栈顶，之后 `all_reduce` / `all_gather` 就改走自研的 PyNCCL 通道：

```python
class DistributedCommunicator:
    plugins: List[DistributedImpl] = [TorchDistributedImpl()]
    def all_reduce(self, x): return self.plugins[-1].all_reduce(x)   # 总是用栈顶
```

注意：这个 `DistributedCommunicator` 用的是 **GPU 上的通信组**（PyNCCL），和上面 `broadcast` 用的 `tp_cpu_group`（CPU 组）是**两个不同的组**——一个管 GPU 张量同步，一个管 CPU 控制同步。这正是「ZMQ/NCCL 分工」在代码里的落点。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：画出 tp=2 时的进程拓扑与通信链路图，标注每条链路走 ZMQ 还是 NCCL。

**操作步骤**：

1. 先列出进程框：`API Server`、`Tokenizer/Detokenizer`（默认合并为 1 个）、`Scheduler rank0`、`Scheduler rank1`。
2. 对照本模块的「5 条 ZMQ 通道」表格，把它们画成箭头，标注 `minisgl_0`～`minisgl_4`。
3. 在两个 Scheduler 之间再画两条线：
   - 一条标 `ZMQ pub/sub (minisgl_2)` —— 控制消息广播。
   - 一条标 `NCCL all_reduce (GPU)` —— 前向时的重型张量合并。
   - 再细标一条 `torch.distributed broadcast (CPU组)` —— 同步消息条数。
4. 在图上用两种颜色区分：ZMQ 链路一种色、NCCL 链路另一种色。

**需要观察的现象 / 预期结果**：你应该得到一张类似下面的图（文字版示意）：

```text
            minisgl_4/1           minisgl_0
 User ──> API Server ────────> Tokenizer/Detok ────────> Scheduler(rank0)
            ^   ^                                            │  │
            │   │           minisgl_3                        │  │ minisgl_2 (ZMQ pub)
            │   └────────────────────────────────────────────┘  │      + broadcast(CPU组)
            │                                                   v
            └──────────── Detokenizer <─── minisgl_1 ──── Scheduler(rank0)
                                                            ╲       ╲
                                                    NCCL all_reduce ╲  (GPU 组)
                                                              ╲       ╲
                                                          Scheduler(rank1)
```

**自检问题**：图里「API Server → Tokenizer」这条边，在默认配置下走的是 `minisgl_4` 还是 `minisgl_1`？

**预期结果**：默认 `num_tokenizer=0` 时走 `minisgl_1`（与 detokenizer 共享），只有 `--num-tokenizer>0` 时才独立走 `minisgl_4`。如果你画对了，说明你理解了「共享 tokenizer」优化。

> 本实践是「源码阅读型实践」，无需 GPU，纯靠对照源码与配置属性即可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么广播用 ZMQ pub/sub，回送结果却用 ZMQ push/pull？

**答案**：广播是「1 对多」（rank0 → 多个 rank），用 pub/sub 自然；回送结果是「1 对 1」（rank0 → detokenizer），用 push/pull 更简单可靠，且 push/pull 有排队语义，detokenizer 没及时取也不会丢。

**练习 2**：`tp_cpu_group` 和 `DistributedCommunicator` 用的通信组是同一个吗？

**答案**：不是。`tp_cpu_group` 是 **CPU 进程组**（`torch.distributed`），用于 broadcast 条数、barrier 同步等控制信令；`DistributedCommunicator` 默认也用 torch.distributed，但 `enable_pynccl_distributed` 后栈顶切换到 **PyNCCL 的 GPU 组**，专门搬 GPU 显存里的大张量。一个管控制、一个管数据。

---

### 4.4 请求生命周期：8 步数据流

#### 4.4.1 概念说明

把前面三个模块串起来，一个请求从用户发出到收到回复，要经历 8 个步骤。官方在 [docs/structures.md:20-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/docs/structures.md#L20-L29) 给出了完整描述。理解这 8 步，就理解了 Mini-SGLang 的「血液循环」。

注意一个关键点：前 7 步大多走 **ZMQ（控制消息）**，只有第 5 步「各 Scheduler 触发 Engine 计算」内部才会触发 **NCCL（张量通信）**。也就是说，NCCL 是被「包」在第 5 步里面的。

#### 4.4.2 核心流程

8 步生命周期：

1. **User → API Server**：用户发 HTTP 请求到 `/v1/chat/completions`。
2. **API Server → Tokenizer**：前端把文本封装成 `TokenizeMsg`，经 ZMQ（`minisgl_4` 或共享的 `minisgl_1`）送去分词。
3. **Tokenizer → Scheduler(rank0)**：分词得到 `input_ids`，封装成 `UserMsg`，经 ZMQ（`minisgl_0`）送进后端。
4. **Scheduler(rank0) → 其他 Scheduler**：rank0 把请求广播给其他 rank（ZMQ pub `minisgl_2` + CPU broadcast 同步条数）。
5. **所有 Scheduler 调度并算下一个 token**：各 rank 跑 Engine 前向，**期间用 NCCL 做 all_reduce/all_gather 合并张量**。
6. **Scheduler(rank0) → Detokenizer**：rank0 把生成的 token 封装成 `DetokenizeMsg`，经 ZMQ（`minisgl_1`）送出反分词。
7. **Detokenizer → API Server**：反分词得到文本，封装成 `UserReply`，经 ZMQ（`minisgl_3`）送回前端。
8. **API Server → User**：前端把结果流式（SSE）或一次性返回给用户。

可以看到，**ZMQ 通道形成一个环**：消息从 API Server 出发，经 Tokenizer、Scheduler、Detokenizer，再回到 API Server。

#### 4.4.3 源码精读

我们对照真实代码，把消息类型的转换点标出来。

**前端送出分词请求**：[api_server.py:431-442](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L431-L442) 里 `FrontendManager` 持有两条 ZMQ 队列——`send_tokenizer`（发往 `zmq_tokenizer_addr`）和 `recv_tokenizer`（从 `zmq_frontend_addr` 收）：

```python
_GLOBAL_STATE = FrontendManager(
    config=config,
    recv_tokenizer=ZmqAsyncPullQueue(config.zmq_frontend_addr, create=True, ...),
    send_tokenizer=ZmqAsyncPushQueue(config.zmq_tokenizer_addr, create=..., ...),
)
```

**Tokenizer 进程的分流**：[tokenizer/server.py:43-45](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L43-L45) 里，同一个进程同时连了「发给后端」和「发给前端」两条出口：

```python
send_backend  = ZmqPushQueue(backend_addr, ...)   # 分词结果 → 后端 (minisgl_0)
send_frontend = ZmqPushQueue(frontend_addr, ...)  # 反分词结果 → 前端 (minisgl_3)
recv_listener = ZmqPullQueue(addr, create=create, ...)  # 入口（minisgl_1 或 minisgl_4）
```

然后在主循环（[tokenizer/server.py:60-108](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L60-L108)）里**按消息类型分流**：

- 收到 `TokenizeMsg` → 调 `tokenize_manager.tokenize` → 封装成 `UserMsg` → `send_backend.put`（去后端）。
- 收到 `DetokenizeMsg` → 调 `detokenize_manager.detokenize` → 封装成 `UserReply` → `send_frontend.put`（回前端）。

这一步清晰展示了「同一个进程，靠消息类型决定走哪条出口」的设计。

**Scheduler 的主循环**：[scheduler.py:120-131](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L120-L131) 的 `run_forever` 就是第 5 步的发动机，它不断 `receive_msg`（收）→ `_schedule_next_batch`（排）→ `_forward`（算）→ `_process_last_data`（处理结果并回送）。第 5 步内部的 NCCL 通信就发生在 `_forward` 调用 Engine、Engine 再调用各 TP Linear 层的 `all_reduce` 时。

#### 4.4.4 代码实践

**实践目标**：跟踪一条消息的字段变化，验证「消息类型转换链」。

**操作步骤**：

1. 从 [tokenizer/server.py:87-101](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L87-L101) 读出：`TokenizeMsg` 经过 tokenize 后，哪些字段被搬进了 `UserMsg`？（提示：`uid`、`input_ids`、`sampling_params`）
2. 再从 [tokenizer/server.py:71-85](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L71-L85) 读出：`DetokenizeMsg` 经过 detokenize 后，哪些字段被搬进了 `UserReply`？（提示：`uid`、`incremental_output`、`finished`）
3. 注意两次转换都**保留了 `uid`**——思考 `uid` 在整个生命周期里起什么作用。

**需要观察的现象 / 预期结果**：`uid` 是请求的唯一标识，它在 API Server 创建（见 `FrontendManager.new_user`，[api_server.py:109-114](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L109-L114)），然后**原封不动地穿越** TokenizeMsg → UserMsg → DetokenizeMsg → UserReply。正因为 uid 全程不变，前端才能用 `ack_map[uid]` 把陆续返回的增量回复正确归到对应的 HTTP 请求上。

> 本实践为「源码阅读型实践」，若无法本地运行，通过阅读以上两段代码即可得出结论。

#### 4.4.5 小练习与答案

**练习 1**：在第 4 步广播时，为什么「消息条数」用 CPU 组的 broadcast 同步，而不是直接把消息也用 NCCL 发？

**答案**：消息是控制信令（小、含 Python 对象语义、需 ZMQ 序列化），适合 ZMQ；NCCL 只擅长搬 GPU 上的同构大张量。用 broadcast 同步条数只是为了「点齐人数」，让订阅方知道要等几条 ZMQ 消息，二者各司其职。

**练习 2**：如果 `DetokenizeMsg` 里的 `uid` 被意外改掉了，会发生什么？

**答案**：前端 `listen()`（[api_server.py:116-123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L116-L123)）里 `if msg.uid not in self.ack_map: continue`——这个 uid 找不到对应的 `ack_map` 项，回复会被**静默丢弃**，对应的 HTTP 请求会一直挂起等不到结果。可见 uid 的全程一致性是生命周期的关键不变量。

---

## 5. 综合实践

**任务**：画出一张完整的「进程拓扑 + 通信链路」图，覆盖 `tp=2` 且 `--num-tokenizer 1` 的配置，并写一份逐链路说明。

**要求**：

1. 列出所有进程（含数量）：API Server、1 个独立 Tokenizer、1 个 Detokenizer、rank0 Scheduler、rank1 Scheduler。
2. 在图上画出全部 5 条 ZMQ 通道（`minisgl_0`～`minisgl_4` 此时是**独立**的，因为 `num_tokenizer>0`），并标出 push/pull 或 pub/sub 方向。
3. 在两个 Scheduler 之间额外标出 3 条链路：ZMQ pub/sub（`minisgl_2`）、CPU 组 broadcast、NCCL all_reduce（GPU 组）。
4. 用一句话写出每个 ZMQ 通道「谁 bind（create=True）、谁 connect（create=False）」。提示：rank0 scheduler 对 `minisgl_0` 是 `create=True`（[io.py:36-40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L36-L40)），tokenizer 对它 `create=False`（[server.py:43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L43)）。
5. 在图上把 8 步生命周期用①～⑧标在对应链路旁，验证它们正好绕 ZMQ 一圈。

**验收标准**：

- 5 条 ZMQ 通道方向、bind/connect 全部正确。
- 能指出 NCCL 只出现在第⑤步内部。
- 能解释默认（`num_tokenizer=0`）配置下，哪两条通道会合并成一个 `minisgl_1`。

> 这是一个纯设计/阅读型综合实践，目标是让你把本讲的「进程模型、TP rank、ZMQ/NCCL 分工、8 步生命周期」四条线索在一张图上统一起来。

## 6. 本讲小结

- Mini-SGLang 是**多进程**架构：1 个 API Server（主进程）+ 1 个 Detokenizer + `num_tokenizers` 个 Tokenizer + `world_size`（= `--tp-size`）个 Scheduler。默认配置下 Tokenizer 并入 Detokenizer。
- **每个 GPU 一个 Scheduler 进程**，由 `DistributedInfo(rank, size)` 标识身份；rank 0 是「队长」，独揽与 tokenizer/detokenizer 的交互，并向其他 rank 广播。
- 通信分两条干道：**ZMQ 搬控制消息**（token、元数据，5 条 `ipc://` 通道），**NCCL/PyNCCL 搬重型张量**（前向时的 all_reduce/all_gather）。控制同步用 CPU 进程组，张量同步用 GPU 组。
- 多 rank 广播靠 **ZMQ pub/sub + torch.distributed broadcast 同步条数** 的组合，保证各 rank 处理同一批次。
- 一个请求经历 **8 步生命周期**，ZMQ 通道绕成一个环：API Server → Tokenizer → Scheduler(rank0) → 其他 Scheduler → 回 rank0 → Detokenizer → API Server → User；NCCL 只藏在第 5 步的 Engine 前向里。
- 全程不变量是 **uid**，它串起同一次请求在所有消息里的身份。

## 7. 下一步学习建议

本讲建立了「宏观骨架」，接下来建议按数据流自顶向下深入：

- 想搞懂**前端怎么把 HTTP 请求挂起等待、又怎么流式回吐** → 看 [u3-l1 API Server 与 OpenAI 兼容接口](u3-l1-api-server.md)，它会展开 `FrontendManager` 的 uid/ack/event 机制。
- 想搞懂**那个一身二任的 tokenizer 进程内部** → 看 [u3-l2 Tokenizer / Detokenizer Worker](u3-l2-tokenizer-worker.md)。
- 想搞懂**第 4 步广播之后、Scheduler 内部怎么排队和算** → 进入第 4 单元，从 [u4-l1 Scheduler 主循环与 Overlap Scheduling](u4-l1-scheduler-main-loop.md) 开始。
- 想搞懂**第 5 步里 NCCL 张量通信的算子细节** → 留到第 9 单元 [u9-l1 张量并行 Linear 与分布式通信](u9-l1-linear-tp-distributed.md)。

建议先把本讲的拓扑图亲手画一遍并收进笔记，后续每一讲都能把新知识「挂」到这张图的某个节点上。
