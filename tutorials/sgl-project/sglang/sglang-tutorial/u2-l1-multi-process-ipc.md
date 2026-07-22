# 多进程架构与 ZMQ IPC 消息协议

## 1. 本讲目标

在 u1-l4 里我们已经知道：无论用 `sglang serve` 还是进程内 `Engine`，背后都是同一套引擎——由若干个子进程协作完成一次推理。本讲要回答的核心问题是：**这些进程到底是谁、各干什么、它们之间用什么语言说话？**

学完本讲你应该能够：

- 说出 SGLang 运行时由哪几个进程组成，以及每个进程的职责边界。
- 看懂 `io_struct.py` 里 `BaseReq` / `GenerateReqInput` / `TokenizedGenerateReqInput` / `BatchTokenIDOutput` / `BatchStrOutput` / `AbortReq` 这几类消息结构体的作用与归属。
- 画出一条请求在 TokenizerManager → Scheduler → DetokenizerManager 之间的 ZMQ 消息时序图。
- 理解 msgpack + pickle 混合序列化、`PickleWrapper`、`sock_send/sock_recv` 这些「收发信封」的工作方式。

本讲只讲「进程怎么连、消息长什么样」，**不**深入 Scheduler 内部如何调度、如何打 batch、如何命中 RadixCache——那是 u3 的主题。

## 2. 前置知识

本讲需要你带着 u1-l4 建立的心智模型继续：SGLang 的引擎由 `Engine._launch_subprocesses` 拉起一个主进程（TokenizerManager）和若干子进程（Scheduler、DetokenizerManager 等），它们协作完成推理。下面补充几个本讲会用到的术语：

- **进程间通信（IPC, Inter-Process Communication）**：不同进程拥有各自的内存空间，不能直接读写彼此的变量，必须通过操作系统提供的机制（管道、共享内存、socket 等）传递数据。SGLang 选用了 **ZMQ**。
- **ZMQ（ZeroMQ）**：一个高性能异步消息库，提供 `PUSH/PULL`、`PUB/SUB` 等多种「socket 模式」。SGLang 主要用 `PUSH/PULL`：一端 `PUSH`（只管发，发完不等回复），另一端 `PULL`（只管收），中间自带队列缓存。它天然适合「生产者—消费者」单向数据流。
- **msgspec**：一个极快的 Python 序列化库，用 `msgspec.Struct` 定义结构体，可编码成 msgpack 二进制。SGLang 用它定义所有跨进程消息。
- **msgpack**：一种紧凑的二进制序列化格式，比 JSON 更小更快，但需要双方约定「类型 schema」才能解码。
- **Pickle**：Python 自带的「把任意对象变成字节」的机制，能序列化几乎任何 Python 对象，但跨语言不通用、且有安全风险。SGLang 在需要传「无法用 msgspec 描述的对象」（如多模态预处理结果、torch 张量）时才用它。

一个关键直觉：ZMQ 负责「**怎么把字节从一个进程搬到另一个进程**」，msgspec/msgpack/pickle 负责「**把 Python 对象变成字节、再变回来**」。两者一外一内，合起来就是 SGLang 的 IPC。

## 3. 本讲源码地图

本讲围绕三个核心文件，外加 `server_args.py` 提供 socket 名字定义：

| 文件 | 角色 |
| --- | --- |
| `python/sglang/srt/managers/io_struct.py` | 「消息字典」。定义所有跨进程传递的结构体，以及 `sock_send/sock_recv` 等收发与序列化函数。 |
| `python/sglang/srt/managers/tokenizer_manager.py` | 「主进程」。接收用户请求、分词、转发给 Scheduler，并把最终结果回流给调用方。 |
| `python/sglang/srt/managers/detokenizer_manager.py` | 「解码子进程」。从 Scheduler 收 token id，解码成文本，回送给 TokenizerManager。 |
| `python/sglang/srt/server_args.py` | 提供 `PortArgs`，定义三个 ZMQ 端点的名字。 |

Scheduler 本身的源码留到 u3 精读；本讲只在拓扑层面引用它「从 tokenizer 收、向 detokenizer 发」的两个端点。

## 4. 核心概念与源码讲解

### 4.1 多进程拓扑与 ZMQ IPC 基础

#### 4.1.1 概念说明：为什么要把推理拆成多个进程

很多人第一次看 SGLang 会疑惑：一次「输入文字、输出文字」的推理，为什么要拆成好几个进程？原因有三：

1. **让 GPU 计算与 CPU 编码/解码重叠**。tokenize（文字→token id）和 detokenize（token id→文字）是纯 CPU 工作，而 GPU 在做前向。如果把它们和 GPU 调度塞在同一个进程的同一个事件循环里，CPU 的编码就会挡住 GPU 的下一步调度。拆进程后，DetokenizerManager 在一个核上猛解码，Scheduler 在另一个核上排下一批，GPU 几乎不用等。
2. **隔离故障与 GIL**。Python 有全局解释器锁（GIL），多线程并不能真正并行执行 Python 字节码。用多进程能绕开 GIL，让编码、调度、解码真正并行；同时一个进程崩溃也不会直接拖垮另一个（父进程通过信号感知并清理）。
3. **天然适配张量/数据并行**。当 `--tp` 或 `--dp` 大于 1 时，Scheduler 会有多个副本，每个副本是一个独立进程，各自持有一张卡。多进程架构让「多卡」就是「多 Scheduler 进程」，模型很统一。

SGLang 的最小拓扑是三个进程连成一个**环**：

```
  TokenizerManager  ──tokenized req──▶  Scheduler  ──batch token-id out──▶  DetokenizerManager
         ▲                                                                           │
         └────────────────────────── batch str out ─────────────────────────────────┘
```

- 用户请求从**左上**进入 TokenizerManager；
- TokenizerManager 分词后把请求**向右**推给 Scheduler；
- Scheduler 跑完前向、采样出 token id，把**整批结果向下**推给 DetokenizerManager；
- DetokenizerManager 把 token id 解码成文字，把**整批结果向左**推回 TokenizerManager；
- TokenizerManager 把结果按 `rid` 分发给各个等待中的请求，最终返回给用户。

注意箭头都是**单向**的（ZMQ PUSH/PULL）。请求不会「原路返回」，而是绕这个环一圈。

#### 4.1.2 核心流程：三个 ZMQ 端点连成环

环上的三条边对应三个 ZMQ 端点，名字全部定义在 `PortArgs` 里：

| 端点名（ipc） | 发送方（PUSH） | 接收方（PULL） | 承载的消息 |
| --- | --- | --- | --- |
| `scheduler_input_ipc_name` | TokenizerManager | Scheduler | `TokenizedGenerateReqInput` / `BatchTokenized*ReqInput` 等请求 |
| `detokenizer_ipc_name` | Scheduler | DetokenizerManager | `BatchTokenIDOutput` 批次结果 |
| `tokenizer_ipc_name` | DetokenizerManager | TokenizerManager | `BatchStrOutput` 解码后结果 |

这三条单向边用 PUSH/PULL 实现，天然带缓冲队列，发送方不会被接收方阻塞。一个端点可以被多个发送方连接（例如多个 TokenizerWorker 同时 PUSH 到一个 router），这正是多 tokenizer 模式的基础。

#### 4.1.3 源码精读：PortArgs 与三端点命名

`PortArgs` 用三个字段给出三条边的名字，注释清楚说明了方向：

[server_args.py:8278-8284](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L8278-L8284) — 定义三条 ZMQ 边的端点名，注意每个注释都写明了「谁发给谁」。

本机模式下，这三个名字是 `ipc://` 协议的临时文件路径：

[server_args.py:8350-8352](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L8350-L8352) — 用 `NamedTemporaryFile` 生成三个唯一 ipc 文件名，作为本机 ZMQ 端点。（分布式模式下会换成 `tcp://` 地址，见同文件 `NetworkAddress(...).to_tcp()` 处。）

Scheduler 侧则通过 `self.ipc_channels.recv_from_tokenizer`（PULL，绑 `scheduler_input_ipc_name`）和 `self.ipc_channels.send_to_detokenizer`（PUSH，绑 `detokenizer_ipc_name`）接入环，例如在批次产出后把结果推向 detokenizer：

[scheduler.py:4455-4455](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L4455-L4455) — `self.ipc_channels.send_to_detokenizer.send_output(recv_req, recv_req)`，Scheduler 把批次结果 PUSH 给 DetokenizerManager。

#### 4.1.4 代码实践

1. **目标**：在源码中亲眼确认「三条边」的两端各是谁。
2. **步骤**：
   - 打开 `python/sglang/srt/server_args.py` 定位 `class PortArgs`，读三个字段的注释。
   - 全仓搜索 `scheduler_input_ipc_name`、`detokenizer_ipc_name`、`tokenizer_ipc_name`，分别确认它们的 PUSH 端与 PULL 端落在哪个 manager 文件里。
3. **需要观察的现象**：你会发现 `scheduler_input_ipc_name` 的 PUSH 出现在 `tokenizer_manager.py`、PULL 出现在 scheduler 相关代码；`detokenizer_ipc_name` 的 PUSH 在 scheduler、PULL 在 `detokenizer_manager.py`；`tokenizer_ipc_name` 的 PUSH 在 `detokenizer_manager.py`、PULL 在 `tokenizer_manager.py`。
4. **预期结果**：三条边的两端正好拼成本节开头的环。这正是后续 4.3、4.4 要展开的两段代码。

#### 4.1.5 小练习与答案

**练习 1**：如果把 DetokenizerManager 进程杀掉，但 TokenizerManager 和 Scheduler 还活着，请求会卡在哪一步？为什么？

> **参考答案**：会卡在「等待结果回流」。Scheduler 仍能收请求、仍能前向并把 `BatchTokenIDOutput` PUSH 到 `detokenizer_ipc_name`（PUSH 不要求对端存活，会堆积在本地队列）；但 `BatchStrOutput` 永远回不到 TokenizerManager，于是 `generate_request` 里的 `_wait_one_response` 会一直等，直到被 watchdog/超时机制干预。

**练习 2**：为什么三条边都用 PUSH/PULL，而不用 REQ/REP（请求-应答）？

> **参考答案**：REQ/REP 是严格的一问一答、强耦合，发送方必须等回复才能发下一个，且不能把同一批结果分发给多个消费者。SGLang 的数据流是单向、多生产者/单消费者、需要批处理与缓冲的，PUSH/PULL 的「发了就走、带队列」特性更贴合，也让 Scheduler 能持续打 batch 而不被解码拖慢。

### 4.2 io_struct.py：跨进程消息的结构体字典

`io_struct.py` 文件头一句话点明了它的职责：「定义在 TokenizerManager / DetokenizerManager / Scheduler 之间传递的对象」：

[io_struct.py:14-21](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L14-L21) — 文件定位说明，强调本文件只放 IPC 结构体定义，保持精简。

#### 4.2.1 概念说明：消息结构体的两个家族

这个文件里几十个结构体看起来眼花缭乱，但其实只有两个家族：

- **`BaseReq` 家族**：单条请求级的 IPC 载荷（[io_struct.py:74-82](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L74-L82)）。例如 `TokenizedGenerateReqInput`、`AbortReq` 都属于这个家族。
- **`BaseBatchReq` 家族**：批次级的 IPC 载荷（[io_struct.py:85-96](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L85-L96)）。例如 `BatchTokenizedGenerateReqInput`、`BatchTokenIDOutput`、`BatchStrOutput`。批次里用 `rids: List[str]` 同时承载多个请求的 id。

两个家族的关键设计是 `tag=True`：msgspec 会在编码时把「类名」作为标签写进字节流，解码时据此还原出确切的子类。这就是 DetokenizerManager 收到一坨字节后能判断「这是 `BatchTokenIDOutput` 还是 `BatchEmbeddingOutput`」的根据。

一个**容易踩坑**的点：用户最常用的 `GenerateReqInput` **不是** `BaseReq` 的子类，它是一个普通 `@dataclass`，是「进程内」的用户输入（由 FastAPI/Engine 接收），不会原样走 ZMQ。文件末尾甚至显式把它排除在命名检查之外：

[io_struct.py:2102-2105](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L2102-L2105) — `_IGNORE_REQ_TYPES_CHECK` 把 `GenerateReqInput` / `EmbeddingReqInput` 排除，因为它们是用户输入而非 IPC 结构体。

真正会进 ZMQ 的是它分词后的产物 `TokenizedGenerateReqInput`（BaseReq 子类）。理解「用户输入 vs IPC 消息」这一层区分，是看懂本文件的关键。

#### 4.2.2 核心流程：一条请求经历的消息形态

同一条逻辑请求，在不同进程边界上穿着不同的「消息外衣」：

```
用户 / FastAPI / Engine
   │  GenerateReqInput          (dataclass, 进程内, 含原始 text/sampling_params)
   ▼  [TokenizerManager 分词]
   │  TokenizedGenerateReqInput (BaseReq, 含 input_ids)
   ▼  [ZMQ PUSH → Scheduler]
   │  ……Scheduler 内部打 batch、前向、采样……
   ▼
   │  BatchTokenIDOutput        (BaseBatchReq, 含 decode_ids/finished_reasons)
   ▼  [ZMQ PUSH → DetokenizerManager]
   │  ……DetokenizerManager 解码……
   ▼
   │  BatchStrOutput            (BaseBatchReq, 含 output_strs)
   ▼  [ZMQ PUSH → TokenizerManager]
   │  ……按 rid 分发, 唤醒等待中的请求……
   ▼
用户拿到 text
```

可以看到：消息越往后越「结果化」——前面是「请帮我算」，后面是「这是算出来的结果」。取消请求则是另一条独立的控制消息 `AbortReq`，由 TokenizerManager 发给 Scheduler。

#### 4.2.3 源码精读：六类关键结构体

**(1) 用户输入 `GenerateReqInput`（进程内，不直接上 ZMQ）**

这是你在 `Engine.generate(text=...)` 或 `/generate` 接口里填的对象，字段非常全（text、input_ids、sampling_params、stream、image_data……）：

[io_struct.py:154-172](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L154-L172) — 用户请求输入的核心字段：`rid`、`text`、`input_ids`、`sampling_params`、`stream` 等。它带大量「批量化/校验」方法（`normalize_batch_and_arguments` 等），说明它服务于「把杂乱的用户输入整理成统一形态」。

它的 `__getitem__`（[io_struct.py:703-785](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L703-L785)）能把一个批量请求切成第 i 条子请求，是批处理的基础。

**(2) 分词后的 IPC 请求 `TokenizedGenerateReqInput`（上 ZMQ）**

分词后文字变成 token id，字段精简、全部是可 msgpack 编码的类型：

[io_struct.py:788-808](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L788-L808) — 注意它继承 `BaseReq`，`input_ids` 用 `array`（紧凑整数数组），`sampling_params` 是 `SamplingParams` 结构体。这才是真正 PUSH 给 Scheduler 的载荷。

它的 `wrap_pickle_fields` / `unwrap_pickle_fields`（[io_struct.py:884-892](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L884-L892)）把多模态输入、计时统计等「非 msgspec」字段塞进 `PickleWrapper`，保证外层结构体能被 msgpack 编码。

批量版本 `BatchTokenizedGenerateReqInput`（[io_struct.py:895-907](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L895-L907)）就是把一组 `TokenizedGenerateReqInput` 装进 `batch: List[...]`。

**(3) Scheduler 的批次输出 `BatchTokenIDOutput`（Scheduler → DetokenizerManager）**

Scheduler 每跑完一批，把所有请求的 token id、完成原因、token 计数等打包发出：

[io_struct.py:1209-1227](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L1209-L1227) — 关键字段：`finished_reasons`（完成原因）、`decode_ids`（增量 token id）、`read_offsets`（增量解码偏移）、各种 token 计数。注意它是「增量」的：流式生成时每一步只发新产出的 token。

**(4) 解码后的批次输出 `BatchStrOutput`（DetokenizerManager → TokenizerManager）**

DetokenizerManager 把 token id 解码成文字后，发回这个结构体，字段与 `BatchTokenIDOutput` 高度对应，但把 token id 换成了 `output_strs`：

[io_struct.py:1300-1312](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L1300-L1312) — `output_strs: List[str]` 是解码后的文字，`output_ids` 保留原始 token id 供需要者使用。两个 `Batch*Output` 字段几乎对称，是同一条信息在「token 视图」与「文字视图」间的映射。

**(5) 取消请求 `AbortReq`（TokenizerManager → Scheduler）**

取消是独立控制消息：

[io_struct.py:1795-1805](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L1795-L1805) — `abort_all`（是否取消全部）、`finished_reason`（带 abort 原因）、`abort_message`。它的 `__post_init__` 把 `rid=None` 改成空串，是为了兼容历史代码。

**(6) 任意对象的「信封」`PickleWrapper` 与序列化函数**

不是所有东西都能用 msgpack 描述（torch 张量、多模态预处理结果、自定义对象）。SGLang 的做法是：外层结构体仍是 msgspec，把「说不清」的字段先用 pickle 打包成 bytes，再装进 `PickleWrapper`：

[io_struct.py:99-109](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L99-L109) — `PickleWrapper.data: bytes`。注释解释了 msgpack 模式下它如何让「不透明载荷」搭乘 msgspec 结构体。

真正收发的入口是 `sock_send` / `sock_recv`，它们根据 `_USE_PICKLE_IPC` 开关决定用纯 pickle 还是 msgpack：

[io_struct.py:2253-2266](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L2253-L2266) — `sock_send` / `sock_recv`：默认走 `msgpack_encode`/`msgpack_decode`，开了 `SGLANG_USE_PICKLE_IPC` 则退化为 zmq 自带的 `send_pyobj`/`recv_pyobj`。

msgpack 模式下，`array` / `torch.Tensor` / `np.ndarray` 这些「数值容器」由 `enc_hook` / `dec_hook` 专门处理：

[io_struct.py:2153-2173](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L2153-L2173) — `enc_hook` 把 array/tensor/ndarray 转成 `(描述, 原始字节)` 元组，让 msgpack 能编码；对端的 `dec_hook` 再还原。这是 SGLang 在「msgpack 紧凑」与「能传张量」之间的折中。

全局编码/解码器只构建一次（[io_struct.py:2213-2215](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L2213-L2215)），解码器绑定 `Union[_all_types]`，所以任何能上线的类型必须在文件里注册过——这也是为什么所有 IPC 结构体都集中在本文件。

#### 4.2.4 代码实践

1. **目标**：亲手把「请求输入 / 批次输出 / 取消」三类消息结构体找出来，并判断它们各自在环上走哪条边。
2. **步骤**：
   - 在 `io_struct.py` 中分别定位：`GenerateReqInput`（请求输入，进程内）、`TokenizedGenerateReqInput`（上 ZMQ 的请求）、`BatchTokenIDOutput` 与 `BatchStrOutput`（两类批次输出）、`AbortReq`（取消）。
   - 对每个结构体，写下它的父类（`BaseReq` / `BaseBatchReq` / 都不是）。
3. **需要观察的现象**：请求类用 `BaseReq`/单数；批次输出类用 `BaseBatchReq`/复数 `rids`；`GenerateReqInput` 既不是 `BaseReq` 也没有走 ZMQ 的标记。
4. **预期结果**：得到一张「消息 → 家族 → 所在边」的对照表（见本讲 5. 综合实践）。

> 本实践为源码阅读型，无需运行服务。

#### 4.2.5 小练习与答案

**练习 1**：`TokenizedGenerateReqInput` 里为什么用 `array` 而不是 `List[int]` 存 `input_ids`？

> **参考答案**：`array.array` 是紧凑的 C 数组（如 `array('q')` 用 8 字节/元素），比 `List[int]`（每个 int 是一个 Python 对象，约 28 字节）省内存、序列化更快。配合 `enc_hook` 把它转成 `(typecode, raw_bytes)`，msgpack 编码后体积小、解码快，适合高频 IPC。

**练习 2**：为什么 `GenerateReqInput` 用 `@dataclass` 而 IPC 消息用 `msgspec.Struct`？

> **参考答案**：`GenerateReqInput` 要对接 FastAPI/Pydantic 的请求校验，并支持灵活的「单条/批量/并行采样」归一化逻辑，用 `@dataclass` + 普通方法更顺手；而 IPC 消息追求「极快序列化 + 严格类型 + 带 tag」，`msgspec.Struct` + `array_like=True` 编码更紧凑、解码能凭 tag 还原子类，所以走 msgspec。

**练习 3**：`PickleWrapper` 解决了什么问题？它和 `enc_hook` 的分工是什么？

> **参考答案**：`PickleWrapper` 解决「载荷类型无法事先用 msgspec 描述」（如多模态预处理对象、计时统计对象）的问题——先 pickle 成 bytes 再塞进结构体字段。`enc_hook` 则处理「类型已知、但 msgpack 不原生支持」（array / torch.Tensor / np.ndarray）的情况——给出确定性的元组编码。前者是「逃生通道」，后者是「一等公民的专用快车道」。

### 4.3 TokenizerManager 类

TokenizerManager 是**主进程**：它直接面对 FastAPI/Engine，是请求进入引擎的入口；它负责分词，把请求 PUSH 给 Scheduler；同时它后台跑一个事件循环，从 DetokenizerManager 那条边收结果，按 `rid` 分发回各个等待中的请求。

#### 4.3.1 概念说明：一个进程，两个方向

TokenizerManager 同时干两件方向相反的事：

- **正向（发请求）**：调用方 → `generate_request` → 分词 → `_send_one_request` → PUSH 到 `scheduler_input_ipc_name`。
- **反向（收结果）**：后台 `handle_loop` 一直 PULL `tokenizer_ipc_name` → 收到 `BatchStrOutput` → `_handle_batch_output` 按 `rid` 找到等待中的 `ReqState` → 唤醒它 → `generate_request` 把结果 `yield` 给调用方。

它用 **asyncio + `zmq.asyncio`**（异步 ZMQ），因为它本质是个 Web 服务器进程，要同时服务成百上千个并发请求，不能用阻塞式 socket。

#### 4.3.2 核心流程：`generate_request` 的生命周期

一次请求在 TokenizerManager 内部的伪代码：

```
generate_request(obj):              # 异步生成器，调用方 async for 取结果
    obj.normalize_batch_and_arguments()   # 1. 归一化（单条/批量/并行采样）
    _init_req_state(obj)                 # 2. 为每个 rid 建 ReqState + asyncio.Event
    tokenized = await _tokenize_one_request(obj)   # 3. 分词 + 多模态预处理
    _send_one_request(tokenized)         # 4. wrap_pickle_fields 后 PUSH 给 Scheduler
    async for response in _wait_one_response(obj): # 5. 等 handle_loop 唤醒并 yield
        yield response
```

与之并行，后台的 `handle_loop` 不断把 DetokenizerManager 回送的结果派发到对应 `ReqState`，从而让第 5 步能拿到数据。这两条线通过 `self.rid_to_state: Dict[str, ReqState]` 这个共享字典关联——发请求时建表项，收结果时查表项。

#### 4.3.3 源码精读

**(1) IPC 通道初始化：一 PUSH 一 PULL**

[tokenizer_manager.py:412-433](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L412-L433) — `init_ipc_channels`：`recv_from_detokenizer` 是 PULL（绑 `tokenizer_ipc_name`），`send_to_scheduler` 是 PUSH（单 tokenizer 时绑 `scheduler_input_ipc_name`）。这正是 4.1 拓扑里 TokenizerManager 这一节点的两条边。多 tokenizer 模式下 `send_to_scheduler` 改连到一个 router 端点。

**(2) 请求入口 `generate_request`**

[tokenizer_manager.py:624-668](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L624-L668) — 完整呈现「归一化 → 建状态 → 分词 → 发送 → 等待回流」的主干。注意 `obj.normalize_batch_and_arguments()`（[io_struct.py:334-358](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/io_struct.py#L334-L358)）是请求进引擎后做的第一件事，把千变万化的用户输入统一成确定形态。

**(3) 发送：`_send_one_request`**

[tokenizer_manager.py:1367-1377](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L1367-L1377) — 发送前先 `wrap_pickle_fields()`（把多模态/计时字段塞进 PickleWrapper），再调 `_dispatch_to_scheduler` 实际 PUSH。批量发送见 `_send_batch_request`（[tokenizer_manager.py:1379-1399](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L1379-L1399)），它把多条 `TokenizedGenerateReqInput` 打包成 `BatchTokenizedGenerateReqInput` 一次发出。

`_dispatch_to_scheduler` 本身很薄（[tokenizer_manager.py:435-443](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L435-L443)）：多 tokenizer 模式下给对象盖一个 `http_worker_ipc` 戳（用于结果路由），然后 `sock_send`/`async_sock_send`。

**(4) 后台结果循环 `handle_loop` 与分发 `_handle_batch_output`**

[tokenizer_manager.py:1884-1897](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L1884-L1897) — `handle_loop`：循环 `async_sock_recv(self.recv_from_detokenizer)`，收到 `BatchStrOutput` / `BatchEmbeddingOutput` / `BatchTokenIDOutput` 就交给 `_handle_batch_output`，否则交给控制类结果分发器。这个循环由 `auto_create_handle_loop`（[tokenizer_manager.py:1859-1868](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L1859-L1868)）在首次有请求时懒启动。

[tokenizer_manager.py:1899-1932](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L1899-L1932) — `_handle_batch_output` 遍历 `recv_obj.rids`，按 `rid` 从 `self.rid_to_state` 取出请求状态，组装 `meta_info`（完成原因、token 计数等），最终唤醒对应的 `ReqState.event`，让 `generate_request` 那一端能 `yield` 出去。这就是「按 rid 把一整批结果拆回每条请求」的地方。

#### 4.3.4 代码实践

1. **目标**：跟踪一条请求在 TokenizerManager 内「正向发送」与「反向回流」两条线的交汇点。
2. **步骤**：
   - 在 `tokenizer_manager.py` 中定位 `generate_request`、`_send_one_request`、`handle_loop`、`_handle_batch_output` 四个方法。
   - 找到把它们关联起来的共享状态：`self.rid_to_state`（在 `init_running_status` 里初始化，[tokenizer_manager.py:445-449](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L445-L449)）。
3. **需要观察的现象**：发送链路在 `rid_to_state` 里「登记」rid；回流链路在 `rid_to_state` 里「查询」rid 并唤醒。两条线通过这个字典解耦。
4. **预期结果**：能写出「请求 rid 在 TokenizerManager 内部的登记—查询」时机，理解为什么结果能精确回到原请求。

> 本实践为源码阅读型，无需运行服务。

#### 4.3.5 小练习与答案

**练习 1**：`handle_loop` 是在 TokenizerManager 构造时就启动的吗？

> **参考答案**：不是。它是懒启动的：`generate_request` 第一行调用 `auto_create_handle_loop()`（[tokenizer_manager.py:629](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L629)），仅在 `self.event_loop is None` 时才创建 asyncio task 跑 `handle_loop`（[tokenizer_manager.py:1859-1868](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/tokenizer_manager.py#L1859-L1868)）。

**练习 2**：为什么 TokenizerManager 用异步 ZMQ（`zmq.asyncio`），而 DetokenizerManager 用同步 ZMQ？

> **参考答案**：TokenizerManager 是 Web 服务入口，要并发处理大量 HTTP 请求，必须用 asyncio 事件循环 + 异步 socket 才不会互相阻塞；DetokenizerManager 是专用解码进程，逻辑就是「收一批、解一批、发一批」的简单循环，用同步 socket 配 `while True` 更简单、开销更低。

### 4.4 DetokenizerManager 类

DetokenizerManager 是一个**专职解码子进程**：它从 Scheduler 收 `BatchTokenIDOutput`，把 token id 解码成文字，封装成 `BatchStrOutput` 回送给 TokenizerManager。

#### 4.4.1 概念说明：为什么解码要独立成进程

把 token id 还原成文字（detokenize）看似简单，但有两点让它值得独立进程：

1. **增量解码有状态**。流式生成时，每一步只来几个新 token，但 BPE 分词器不能「逐 token」解码（一个词可能被切成多个 token，边界处会变）。必须为每条请求维护 `DecodeStatus`（已解码文本、偏移量），从上次的断点续解。这是有状态的 CPU 工作。
2. **与 GPU/调度彻底解耦**。解码是纯 CPU、可能耗时（尤其大批次），放进 Scheduler 进程会抢占调度循环的 Python 时间。独立进程 + 独立核后，Scheduler 的 `event_loop` 几乎只关心 GPU，解码在旁边并行跑，这正是「零开销调度器」的前提之一。

#### 4.4.2 核心流程：极简的三步循环

DetokenizerManager 的主循环是整个引擎里最清爽的部分，伪代码：

```
event_loop():
    while True:
        recv_obj = sock_recv(recv_from_scheduler)   # 1. PULL 一批 token id
        output = dispatcher(recv_obj)               # 2. 按类型解码（BatchTokenIDOutput → BatchStrOutput）
        if output is not None:
            sock_send(send_to_tokenizer, output)    # 3. PUSH 解码后文字回 TokenizerManager
```

解码逻辑由一个 `TypeBasedDispatcher` 按「收到的消息类型」分发到对应 handler：收到 `BatchTokenIDOutput` 走 `handle_batch_token_id_out`，收到 `BatchEmbeddingOutput` 走 embedding handler，收到控制类（`FreezeGCReq` 等）就就地处理。

#### 4.4.3 源码精读

**(1) 构造与四段式初始化**

[detokenizer_manager.py:94-109](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L94-L109) — `__init__` 分四步：建 IPC 通道、加载 tokenizer、初始化运行状态（含 `decode_status` 容量字典）、建请求分发器。结构清晰，每步一个 `init_*` 方法。

**(2) IPC 通道：又是 PULL + PUSH**

[detokenizer_manager.py:111-122](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L111-L122) — `recv_from_scheduler` 是 PULL（绑 `detokenizer_ipc_name`，对应 4.1 拓扑里 Scheduler→DetokenizerManager 那条边），`send_to_tokenizer` 是 PUSH（绑 `tokenizer_ipc_name`）。这就把环的下半段接上了。

**(3) 分发器：按类型路由**

[detokenizer_manager.py:156-164](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L156-L164) — `init_request_dispatcher` 用 `TypeBasedDispatcher` 注册四条规则：`BatchEmbeddingOutput → handle_batch_embedding_out`、`BatchTokenIDOutput → handle_batch_token_id_out`、`FreezeGCReq → handle_freeze_gc_req`、`ConfigureLoggingReq → handle_configure_logging_req`。`TypeBasedDispatcher` 正是靠 msgspec 的 tag 还原出的确切类型来查这张表。

**(4) 主循环 `event_loop`**

[detokenizer_manager.py:166-174](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L166-L174) — 就是 4.4.2 伪代码的真实版：`sock_recv` → `_request_dispatcher` → `sock_send`。配合一个软看门狗 `soft_watchdog`。这是理解整条数据流最值得记住的一段代码。

**(5) 解码 handler `handle_batch_token_id_out`**

[detokenizer_manager.py:430-484](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L430-L484) — 把 `BatchTokenIDOutput` 转成 `BatchStrOutput`：先 `_decode_batch_token_id_output` 得到每条请求的文字（内部用每条请求的 `DecodeStatus` 做增量解码），把 `routed_experts` / `indexer_topk` 张量 base64 编码（避免在 tokenizer 热路径上序列化大张量），然后把其余字段原样搬运。这正解释了 4.2 里「两个 Batch 输出字段几乎对称」的现象。

**(6) 进程入口 `run_detokenizer_process`**

[detokenizer_manager.py:512-534](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L512-L534) — 这是 Engine/launch_server 派生 DetokenizerManager 子进程时执行的函数：设进程名为 `sglang::detokenizer`、`kill_itself_when_parent_died()`（父进程死了自己跟着死）、构造 manager 并跑 `event_loop`（单 tokenizer）或 `multi_http_worker_event_loop`（多 tokenizer）。异常时给父进程发 `SIGQUIT` 触发整体清理。

#### 4.4.4 代码实践

1. **目标**：把 DetokenizerManager 的「收—解—发」三步与具体的消息类型对应起来。
2. **步骤**：
   - 在 `detokenizer_manager.py` 中读 `event_loop`（[L166-174](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L166-L174)）。
   - 跟进 `_request_dispatcher` 注册表（[L156-164](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L156-L164)），确认 `BatchTokenIDOutput` 进入 `handle_batch_token_id_out`。
   - 在 `handle_batch_token_id_out`（[L430-484](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/detokenizer_manager.py#L430-L484)）里圈出「解码文字」「base64 张量」「字段搬运」三段。
3. **需要观察的现象**：handler 返回的是 `BatchStrOutput`，正是 4.2 里 DetokenizerManager → TokenizerManager 那条边承载的消息。
4. **预期结果**：能复述「一个 `BatchTokenIDOutput` 进来、一个 `BatchStrOutput` 出去」的完整转换。

> 本实践为源码阅读型，无需运行服务。

#### 4.4.5 小练习与答案

**练习 1**：DetokenizerManager 怎么知道收到的字节该还原成哪个结构体？

> **参考答案**：靠 msgspec 的 `tag=True`。`BaseReq` / `BaseBatchReq` 都带 tag，msgpack 编码时把类名写进字节流；`sock_recv` → `msgpack_decode` 解码时，绑定 `Union[_all_types]` 的解码器会据 tag 还原出确切子类（如 `BatchTokenIDOutput`），`TypeBasedDispatcher` 再据这个类型查 handler 表。

**练习 2**：`DecodeStatus` 为什么要保存 `surr_offset` / `read_offset` / `sent_offset` 这些偏移量？

> **参考答案**：BPE 分词器的 token 与字符不是一一对应，且流式下每步只来增量 token。`surr_offset`/`read_offset` 记录「已稳定可输出的字符边界」，避免在 token 边界处输出半截词（surrogate/不完整 UTF-8）；`sent_offset` 记录「已经上报给 tokenizer 的位置」，保证增量推送不重复、不遗漏。这些偏移让增量解码既正确又高效。

## 5. 综合实践：画出一条请求的进程间消息时序图

本任务把全讲串起来。请完成下面四件事：

**第一步：建立消息—家族—边的对照表。** 仿照下表，把你在 4.2.4 找到的结构体填进去（答案见下文）：

| 消息结构体 | 家族（BaseReq / BaseBatchReq / 进程内） | 在环上走哪条边 | 方向 |
| --- | --- | --- | --- |
| `GenerateReqInput` | ? | 不上 ZMQ | 用户→TokenizerManager |
| `TokenizedGenerateReqInput` | ? | ? | TokenizerManager→Scheduler |
| `BatchTokenIDOutput` | ? | ? | Scheduler→DetokenizerManager |
| `BatchStrOutput` | ? | ? | DetokenizerManager→TokenizerManager |
| `AbortReq` | ? | ? | TokenizerManager→Scheduler |

**第二步：画时序图。** 用任意工具（纸笔、mermaid、excalidraw）画出一条**流式** chat 请求的完整时序，至少包含：

1. 调用方 → TokenizerManager：`generate_request(obj)`，obj 是 `GenerateReqInput`。
2. TokenizerManager 内部：`normalize_batch_and_arguments` → `_tokenize_one_request` → `wrap_pickle_fields` → `_dispatch_to_scheduler`（PUSH `TokenizedGenerateReqInput` 到 `scheduler_input_ipc_name`）。
3. Scheduler：PULL 收到 → 打 batch → 前向 → 采样 → `send_to_detokenizer`（PUSH `BatchTokenIDOutput` 到 `detokenizer_ipc_name`）。每生成一步就发一次增量。
4. DetokenizerManager：`event_loop` PULL 收到 → `handle_batch_token_id_out` 增量解码 → `sock_send`（PUSH `BatchStrOutput` 到 `tokenizer_ipc_name`）。
5. TokenizerManager：`handle_loop` PULL 收到 → `_handle_batch_output` 按 rid 唤醒 `ReqState` → `generate_request` `yield` 一段文字给调用方。
6. 重复 3-5 直到 `finished_reasons` 标记完成。

**第三步：标注序列化。** 在时序图上每条 ZMQ 箭头旁注明：默认走 msgpack（`enc_hook` 处理 array/tensor），不透明字段经 `PickleWrapper`，开了 `SGLANG_USE_PICKLE_IPC` 则全走 pickle。

**第四步：验证。** 对照 4.1 的拓扑环与 4.3、4.4 的源码，检查你的时序图里每一步是否都能在源码里找到对应函数。如果你画出的链路能回答「为什么结果能精确回到原请求」「为什么解码不会拖慢 GPU」，本讲就过关了。

> 对照表参考答案：`GenerateReqInput`=进程内；`TokenizedGenerateReqInput`=BaseReq，走 `scheduler_input_ipc_name`；`BatchTokenIDOutput`=BaseBatchReq，走 `detokenizer_ipc_name`；`BatchStrOutput`=BaseBatchReq，走 `tokenizer_ipc_name`；`AbortReq`=BaseReq，走 `scheduler_input_ipc_name`。

> 若想在真实环境核对：用 `sglang serve --model-path <小模型>` 启动后，发一个 `stream=True` 的请求，在日志里能看到 Scheduler 每步产出的增量；但「进程间消息」本身不会打印，时序图主要靠源码核对，运行仅作辅助验证。

## 6. 本讲小结

- SGLang 运行时是**多进程**架构，最小拓扑是 TokenizerManager → Scheduler → DetokenizerManager 连成一个**环**，三条边都是 ZMQ 的 PUSH/PULL 单向通道（`scheduler_input_ipc_name` / `detokenizer_ipc_name` / `tokenizer_ipc_name`）。
- 拆进程是为了让 CPU 编码/解码与 GPU 前向并行、绕开 GIL、天然适配多卡并行。
- `io_struct.py` 是「消息字典」，所有跨进程结构体集中在两个家族：单条级 `BaseReq` 与批次级 `BaseBatchReq`，靠 `tag=True` 在解码时还原确切子类。
- 关键区分：用户输入 `GenerateReqInput` 是**进程内** dataclass，**不**直接上 ZMQ；真正进 ZMQ 的是分词后的 `TokenizedGenerateReqInput`。
- 一条请求的消息演化：`GenerateReqInput` → `TokenizedGenerateReqInput` →（Scheduler 内部）→ `BatchTokenIDOutput` → `BatchStrOutput` → 回到调用方；取消走独立的 `AbortReq`。
- 序列化默认用 msgpack（`enc_hook`/`dec_hook` 处理 array/tensor/ndarray），说不清的字段用 `PickleWrapper` 兜底，`sock_send`/`sock_recv` 是统一收发入口。

## 7. 下一步学习建议

- 下一讲 **u2-l2 启动流程与 ServerArgs 配置** 会讲这些进程是**怎么被拉起来**的（`launch_server.run_server` → 派生各子进程、绑定 PortArgs 端点），把本讲的静态拓扑变成动态启动链路。
- 之后 **u2-l3 请求端到端流转** 会把本讲的时序图再细化一层，补上 OpenAI 接口到 `GenerateReqInput` 的转换。
- 当你想深入 Scheduler 内部「打 batch、命中缓存、采样」时，进入 **u3 调度器与连续批处理**。本讲只把请求送到了 Scheduler 门口，u3 才打开 Scheduler 的大门。
- 建议顺便扫一眼 `python/sglang/srt/managers/data_parallel_controller.py`，它在本讲拓扑之上再加一层「DP 路由」，是 **u8-l2** 的主题；现在只需知道它是环外的一个可选路由器即可。
