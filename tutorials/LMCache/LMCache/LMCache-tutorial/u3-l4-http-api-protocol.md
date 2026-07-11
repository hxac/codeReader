# HTTP API 与通信协议

## 1. 本讲目标

本讲是 LMCache 多进程（MP）架构下的「通信协议」专题。学完本讲，你应当能够：

- 区分 LMCache 里**三套并存的通信协议**：独立 server 的二进制 socket 协议、MP daemon 的 ZMQ RPC 协议、面向控制面的 HTTP/REST API，并说出它们各自的承载者与边界。
- 读懂 `v1/protocol.py` 中 `ClientMetaMessage` / `ServerMetaMessage` 的定长二进制头部格式，并解释为什么 `ServerReturnCode` 用 200/400。
- 读懂 `multiprocess/protocol.py` 如何作为门面（facade）委托给 `protocols/` 子包，理解 `RequestType` / `ProtocolDefinition` / `HandlerType` 三件套，以及那套「枚举与定义必须一一对应」的启动期校验机制。
- 说明 `http_api_registry.py` + `router_discovery.py` 如何用「约定优于配置」的方式让一个 `*_api.py` 文件自动注册成 HTTP endpoint。
- 读懂 `mp_coordinator/schemas.py` 里 Pydantic 模型作为「双端共享线缆契约」的设计，以及 `encode_tokens` 这类紧凑编码的动机。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（来自前置讲义）：

- **MP 架构的三类进程**（u3-l1）：vLLM engine 进程、MP cache server daemon、MP coordinator daemon。
- **worker 与 daemon 之间的传输管道**（u3-l2）：控制流走 ZMQ 消息队列（`mq.py`），GPU 张量走 CUDA IPC / 共享内存零拷贝；一条请求的 ZMQ 帧形如 `[identity, request_uid, request_type, *payloads]`，KV 张量**从不过 ZMQ**，只传 block id。
- **coordinator 的舰队级职责**（u3-l3）：注册发现（`/instances`）、blend 指纹目录、配额与淘汰，全部以 FastAPI/uvicorn 暴露为 REST。
- **进程入口**（u1-l4）：`lmcache_server` → `v1/server`（裸 TCP）；`lmcache_controller` → `v1/api_server`；MP coordinator 用 `python -m lmcache.v1.mp_coordinator`。

如果你还没读过以上三讲，建议先补，因为本讲不再重复进程拓扑，只聚焦「这些进程之间到底用什么字节/什么结构对话」。

几个本讲会用到的术语：

- **线缆契约（wire contract）**：通信两端对「字节怎么排布、字段什么类型」的约定。改一边不改另一边就会解析错乱。
- **定长头部（fixed-length header）**：先发一段固定字节数的元信息，对方读到后再决定读多少字节的数据体。
- **门面（facade）**：一个对外暴露简单接口、对内委托给子系统实现的模块。
- **msgspec**：一个高性能的二进制序列化库，比 JSON/pickle 更快且类型安全，LMCache 用它编解码 MP 协议的请求。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 一句话职责 |
|------|------|-----------|
| `lmcache/v1/protocol.py` | 独立 server 二进制协议 | 定义裸 TCP 上的 `ClientMetaMessage`/`ServerMetaMessage` 定长头部与 PUT/GET 命令 |
| `lmcache/v1/multiprocess/protocol.py` | MP daemon 协议门面 | 暴露 `get_payload_classes` / `get_response_class` / `get_handler_type` 三个查询函数 |
| `lmcache/v1/multiprocess/protocols/base.py` | MP 协议基础类型 | `RequestType` 枚举、`ProtocolDefinition`、`HandlerType` |
| `lmcache/v1/multiprocess/protocols/__init__.py` | MP 协议校验器 | `initialize_protocols()` 收集并校验所有协议定义 |
| `lmcache/v1/multiprocess/protocols/engine.py` 等 | MP 协议分模块定义 | 各类操作（engine/controller/debug/blend/p2p）的 payload 与响应类型 |
| `lmcache/v1/multiprocess/http_api_registry.py` | MP server HTTP 注册器 | 自动发现 `http_apis/*_api.py` 并挂载到 FastAPI app |
| `lmcache/v1/utils/router_discovery.py` | 通用路由发现 | `pkgutil` 扫描目录，收集带 `router` 属性的 `*_api` 模块 |
| `lmcache/v1/mp_coordinator/schemas.py` | coordinator REST 线缆契约 | Pydantic 模型，coordinator 与 mp server 双端共享 |

一句话总览：**独立 server 用手写 `struct` 二进制头部，MP daemon 用 msgspec + 枚举驱动的协议表，HTTP/REST 用 FastAPI 路由自动发现 + Pydantic 模型**。三者服务不同进程、不同抽象层级，本讲逐一拆解。

## 4. 核心概念与源码讲解

### 4.1 独立 server 的二进制 socket 协议（v1/protocol.py）

#### 4.1.1 概念说明

`v1/protocol.py` 服务的是 `lmcache_server` 这个**独立 KV 存储 TCP 服务**（见 u1-l4）。它是一个极简的「远程字典」：客户端用 PUT 存一段字节、用 GET 取回，server 用一个固定长度的「元信息头部（meta message）」描述这段字节是什么，再在头部之后跟上原始数据体。

这是一个**纯二进制、定长头部 + 变长数据体**的协议，没有任何 JSON、没有 HTTP 头，全部用 Python 标准库 `struct` 手工打包。它存在的原因是：独立 server 要尽量轻、尽量快，不依赖任何 web 框架，socket 直接收发字节。

#### 4.1.2 核心流程

一次 PUT 的完整往返：

```
client                                         server (v1/server)
  |                                               |
  | -- ClientMetaMessage (186 bytes, PUT) ------> |
  | -- length 字节的数据体 ----------------------> |
  |                                               | 存入 data_store
  | <-- ServerMetaMessage (36 bytes, SUCCESS) --- |
  |                                               |
```

一次 GET 的完整往返：

```
client                                         server (v1/server)
  |                                               |
  | -- ClientMetaMessage (186 bytes, GET) ------> |
  |                                               | 查 data_store
  | <-- ServerMetaMessage (36 bytes, SUCCESS) --- | (命中时)
  | <-- length 字节的数据体 <-------------------- |
  |   或                                          |
  | <-- ServerMetaMessage (36 bytes, FAIL) ------ | (未命中时，code=400)
```

关键点：**头部先行、定长**，这样接收方可以先读 `packlength()` 字节、解析出命令与长度，再决定是否继续读数据体。`ServerReturnCode` 取了 200/400 这两个看起来像 HTTP 状态码的数字，但它**不是 HTTP**，只是作者借用了「200=成功 / 400=失败」的语义记忆点。

#### 4.1.3 源码精读

先看命令与返回码两个枚举：

[lmcache/v1/protocol.py:24-34](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L24-L34) —— `ClientCommand` 与 `ServerReturnCode`。`ClientCommand` 用 `auto()` 自增（PUT=1, GET=2, EXIST=3, LIST=4, HEALTH=5）；`ServerReturnCode` 显式写死 200/400。

再看客户端请求头部 `ClientMetaMessage`：

[lmcache/v1/protocol.py:210-247](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L210-L247) —— 这是请求头部。它打包成一个 `struct` 串，格式串是：

```
iiiiiiiii{MAX_KEY_LENGTH}s
```

即 **9 个 4 字节整数 + 一个 150 字节的定长 key 字符串**。9 个整数字段依次是：`command, length, fmt, dtype, location, shape0, shape1, shape2, shape3`。其中：

- `dtype` / `location` 不是直接存字符串，而是先用 `DTYPE_TO_INT` / `LOCATION_TO_INT` 查表转成小整数（见 [L37-74](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L37-L74)），省空间、对齐快。
- `shape` 用 `pad_shape_to_4d` 补齐到 4 维（不足 4 维尾随补 0），见 [L99-124](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L99-L124)。注释明确：「4 是 memory object 的最大维度，bytes 对象传 `[x,0,0,0]`」。
- key 字符串用 `ljust(MAX_KEY_LENGTH)` 右侧补空格到定长 150，反序列化时 `.strip()` 去掉。

`packlength()` 给出固定长度（[L265-268](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L265-L268)）：

\[ \text{packlength} = 4 \times 9 + 150 = 186 \text{ 字节} \]

再看 server 应答头部 `ServerMetaMessage`：

[lmcache/v1/protocol.py:271-302](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L271-L302) —— 结构对称，但没有 key 字段（server 应答不需要再回传 key），格式串是 `iiiiiiiii`，即 9 个整数：

\[ \text{packlength} = 4 \times 9 = 36 \text{ 字节} \]

字段顺序为 `code, length, fmt, dtype, shape0-3, location`。注意 server 把 `location` 放在**最后**，而 client 把 `location` 放在 `dtype` 之后、`shape` 之前——两边的字段顺序**各自独立约定**，必须严格按各自的格式串解包，不能混用。

这套头部协议在独立 server 的主循环里被实际消费：

[lmcache/v1/server/__main__.py:47-94](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L47-L94) —— server 先 `receive_all` 读 `ClientMetaMessage.packlength()`（186）字节头部，`deserialize` 后用 `match meta.command` 分派到 PUT/GET/EXIST/HEALTH。PUT 分支读 `meta.length` 字节数据体，GET 分支查 `data_store` 后回 `ServerMetaMessage(SUCCESS, ...)` + 数据体，未命中则回 `FAIL`。

> 补充：`protocol.py` 里还有一个 `RemoteMetadata`（[L159-204](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L159-L204)），它用于「多组张量」场景，长度随组数变化，格式为 `i * (2 + 5*num_groups)`，需先调 `init_remote_metadata_info(num_groups)` 设全局格式（[L77-90](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py#L77-L90)）。它属于同一套二进制体系，本讲不深入。

#### 4.1.4 代码实践

**实践目标**：在不启动 server 的情况下，亲手打包并解包一个 `ClientMetaMessage`，验证它是定长 186 字节、且能无损往返。

**操作步骤**（示例代码，可直接在安装了 LMCache + torch 的环境里 `python` 跑）：

```python
# 示例代码：手动验证 ClientMetaMessage 的定长头部
import torch
from lmcache.utils import CacheEngineKey
from lmcache.v1.memory_management import MemoryFormat
from lmcache.v1.protocol import ClientCommand, ClientMetaMessage

key = CacheEngineKey("vllm", "model", 0, 0, 0, 0)
msg = ClientMetaMessage(
    command=ClientCommand.PUT,
    key=key,
    length=128,
    fmt=MemoryFormat.KV_2LTD,
    dtype=torch.bfloat16,
    shape=torch.Size([2, 8, 256, 64]),
    location="LocalCPUBackend",
)
raw = msg.serialize()
print(len(raw), ClientMetaMessage.packlength())          # 186 186
back = ClientMetaMessage.deserialize(raw)
print(back.command, back.length, back.dtype, back.shape)  # ClientCommand.PUT 128 torch.bfloat16 torch.Size([2,8,256,64])
```

> 字段名（如 `KV_2LTD`）请以你本地 `lmcache.v1.memory_management.MemoryFormat` 的实际枚举成员为准；若枚举名有出入，换成任意一个真实成员即可，本练习只关心定长头部的往返与字节数。

**需要观察的现象**：

1. `msg.serialize()` 返回的字节数恰好等于 `ClientMetaMessage.packlength()`（186）。
2. `ClientMetaMessage.deserialize(msg.serialize())` 得到的对象，其 `command`、`length`、`dtype`、`shape`、`location`、`key` 与原对象逐一相等（`shape` 会保留 4 维 `[2,8,256,64]`）。
3. 把 `shape` 改成 2 维 `torch.Size([1024, 0])`，序列化→反序列化后 `strip_shape_padding` 会还原成 `torch.Size([1024])`（尾随 0 被剥掉）。

**预期结果**：定长头部可往返、4D 补零/剥零对称工作。若你的环境无 GPU/torch，可只验证字节数与 `packlength()` 相等这一条（不依赖 torch 的部分）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ServerReturnCode` 用 200/400 而不是 0/1？

**答案**：这是一个语义记忆点——作者借用 HTTP 的「200 成功 / 400 客户端错误」约定，让阅读二进制抓包的人一眼分辨成功失败。它与 HTTP 无关，仅仅是数字取值的约定。

**练习 2**：`ClientMetaMessage` 的 key 为什么要 `ljust(150)` 补到定长？

**答案**：因为 `struct` 的 `{N}s` 格式要求字符串恰好 N 字节。定长 key 让整个头部字节数固定（186），接收方可以一次性 `receive_all(186)` 读完整头部，不必先读一个长度字段再读变长 key——少一次往返、解析更简单。

---

### 4.2 MP daemon 的请求类型协议与校验系统（multiprocess/protocol.py）

#### 4.2.1 概念说明

MP cache server daemon（u3-l1）和 worker 之间用的是另一套完全不同的协议。它**不是**手写 `struct` 头部，而是基于 ZMQ 多帧消息（u3-l2 讲过帧结构 `[identity, request_uid, request_type, *payloads]`）+ msgspec 二进制编码。

这套协议的核心抽象是：**每一种请求（RequestType）都有一张「协议定义（ProtocolDefinition）」**，说明它带哪些 payload、返回什么类型、用什么方式执行。`multiprocess/protocol.py` 只是一个**门面**——它把所有定义委托给 `protocols/` 子包，对外只暴露三个查询函数。真正的协议知识分散在 `protocols/` 下的若干分模块里。

为什么要这样设计？因为 MP 协议的命令种类很多（注册、存、取、查、blend、p2p……），而且会随功能演进而增长。把「枚举」与「定义」分开、再在启动时强校验两者一致，能避免「加了枚举忘了加定义」这类隐蔽 bug。

#### 4.2.2 核心流程

协议系统的初始化与查询流程：

```
进程启动
  │
  ▼
initialize_protocols()            # protocols/__init__.py
  │  遍历 _PROTOCOL_MODULES (engine/controller/debug/blend/p2p/...)
  │  每个 module.get_protocol_definitions() 返回 {名字: ProtocolDefinition}
  │  把名字 -> RequestType[name] 枚举值
  │  校验：枚举成员 ⊆ 定义集合，定义集合 ⊆ 枚举成员
  ▼
_PROTOCOL_DEFINITIONS: dict[RequestType, ProtocolDefinition]   # protocol.py 模块级缓存
  │
  ▼
运行时 mq.py 收到一帧 -> get_payload_classes(req_type) / get_response_class(req_type) / get_handler_type(req_type)
```

一次 STORE 请求的 ZMQ 往返（对照 u3-l2）：

```
worker (DEALER)                              server (ROUTER)
  | msgspec 编码 request_uid                    |
  | msgspec 编码 request_type=STORE             |
  | msgspec 编码 4 个 payload                   |
  | send_multipart([uid, type, *payloads]) ---> | 解码 type -> get_payload_classes
  |                                             | 解码 payloads -> 调 handler
  |                                             | 返回 tuple[bytes,bool]
  | <-- send_multipart([uid, type, response]) - | msgspec 编码 response
```

注意：STORE 的 payload 是 `[KeyType, int(instance_id), list[list[int]](gpu_block_ids), bytes(event_ipc_handle)]`——**KV 张量本身不在消息里**，只有 GPU block id 和一个 CUDA event 的 IPC handle。这与独立 server「头部 + 原始字节」截然不同：MP 协议从不搬运张量字节（u3-l2 的零拷贝设计）。

#### 4.2.3 源码精读

先看基础类型三件套：

[lmcache/v1/multiprocess/protocols/base.py:12-23](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/base.py#L12-L23) —— `HandlerType` 枚举，定义 handler 怎么执行：

- `SYNC`：直接在 ZMQ 主循环里跑（快、非阻塞操作）。
- `BLOCKING`：可能阻塞，丢进线程池（I/O、慢操作）。
- `NON_BLOCKING`：预留，暂未支持。

[lmcache/v1/multiprocess/protocols/base.py:94-106](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/base.py#L94-L106) —— `ProtocolDefinition` 是一个 dataclass，三个字段：`payload_classes`（有序的 payload 类型列表）、`response_class`（响应类型，无响应则 `None`）、`handler_type`（SYNC/BLOCKING）。

[lmcache/v1/multiprocess/protocols/base.py:26-92](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/base.py#L26-L92) —— `RequestType` 枚举，按类别组织：Engine 操作（REGISTER_KV_CACHE/STORE/RETRIEVE/LOOKUP/...）、Controller 操作（CLEAR/GET_CHUNK_SIZE/PING）、Debug（NOOP）、Blend（CB_*）、P2P（P2P_*）。注释里写明了**新增请求类型的三步法**：①在此加枚举成员；②在对应 `protocols/*.py` 加定义；③校验系统会保证两者同步。

再看校验器（这是整套设计的灵魂）：

[lmcache/v1/multiprocess/protocols/__init__.py:47-121](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/__init__.py#L47-L121) —— `initialize_protocols()` 做三件事：

1. 遍历所有协议模块，收集 `{名字: ProtocolDefinition}`，**跨模块查重**（重名直接抛 `ProtocolInitializationError`）。
2. 把字符串名字转成 `RequestType[name]` 枚举值；名字找不到对应枚举成员就抛错，并提示「去 base.py 加枚举」。
3. 反向校验：每个 `RequestType` 枚举成员都必须有定义（[L108-116](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/__init__.py#L108-L116)），否则抛错提示「加定义或删枚举」。

这是一个典型的「双向一致性校验」：枚举集 == 定义集。任何一边多写或少写，进程都**启动失败并大声报错**（fail-fast），而不是运行时才在某条命令上崩。

然后看具体定义长什么样，以 STORE / RETRIEVE 为例：

[lmcache/v1/multiprocess/protocols/engine.py:133-152](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/engine.py#L133-L152) —— STORE 的 payload 是 `[KeyType, int, list[list[int]], bytes]`，响应是 `tuple[bytes, bool]`（CUDA event handle + 成功标志），handler_type=`BLOCKING`（因为要同步 GPU）。注释写得很清楚：payload 里的 `gpu_block_ids` 是「按 LMCache KV group 索引的 GPU block id 列表」，`event_ipc_handle` 是用于同步的 CUDA event IPC handle——**数据本体通过这些 id/handle 间接访问，不在帧里**。

最后是门面本身：

[lmcache/v1/multiprocess/protocol.py:25-29](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocol.py#L25-L29) —— 模块加载时立即调用 `initialize_protocols()`，把结果缓存到 `_PROTOCOL_DEFINITIONS`。

[lmcache/v1/multiprocess/protocol.py:32-86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocol.py#L32-L86) —— 三个查询函数 `get_payload_classes` / `get_response_class` / `get_handler_type`，签名一致：传入 `RequestType`，从缓存表里取出对应定义的某个字段；未知类型抛 `ValueError`。它们就是 `mq.py` 在收发消息时唯一调用的协议 API。

`mq.py` 的实际调用点印证了这一点：

[lmcache/v1/multiprocess/mq.py:298-323](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L298-L323) —— 客户端发送时，`get_payload_classes(request_type)` 拿到期望的 payload 类型列表，逐个 `msgspec_encode(payload, cls=cls)`，然后 `send_multipart([b_request_uid, b_request_type] + b_payloads)`。

[lmcache/v1/multiprocess/mq.py:605-620](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L605-L620) —— 服务端接收时，解包出 `identity, b_request_uid, b_request_type, *payloads`，与 u3-l2 讲的帧结构完全一致。

#### 4.2.4 代码实践

**实践目标**：体会「枚举与定义双向校验」的 fail-fast 行为。

**操作步骤**（示例代码）：

```python
# 示例代码：观察协议校验
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.multiprocess.protocol import (
    get_payload_classes, get_response_class, get_handler_type,
)

# 1. 查询一个真实存在的请求类型
rt = RequestType.STORE
print("STORE payloads:", get_payload_classes(rt))   # [KeyType, int, list[list[int]], bytes]
print("STORE response:", get_response_class(rt))    # tuple[bytes, bool]
print("STORE handler :", get_handler_type(rt))      # HandlerType.BLOCKING
```

**需要观察的现象**：

1. STORE 的 `payload_classes` 长度为 4，`response_class` 是 `tuple[bytes, bool]`。
2. 试着自己造一个错：在 `protocols/base.py` 的 `RequestType` 里临时加一个 `FAKE_OP = enum.auto()`，再 `import lmcache.v1.multiprocess.protocol`，会立刻抛 `ProtocolInitializationError`，提示该枚举成员没有定义。（**注意：这只是思想实验，本讲禁止改源码，请在心里或临时副本里推演。**）
3. 反过来，若只在某个 `protocols/*.py` 的 `get_protocol_definitions()` 里加一个 `"FAKE_OP"` 定义而不动枚举，同样启动失败，提示「没有对应枚举成员，去 base.py 加」。

**预期结果**：枚举与定义任何一侧不一致，进程在 import 阶段就报错退出，绝不带着残缺协议进入运行。这就是「大声失败」。

#### 4.2.5 小练习与答案

**练习 1**：独立 server 的 PUT/GET 与 MP 协议的 STORE/RETRIEVE，在「数据怎么传」上最本质的区别是什么？

**答案**：独立 server 在头部之后**直接跟原始 KV 字节**（`meta.length` 字节）；MP 协议的 STORE/RETRIEVE 帧**只带 block id 和 CUDA event IPC handle**，KV 张量通过预先注册的 IPC handle 在进程间零拷贝共享，从不进入 ZMQ 字节流。

**练习 2**：为什么 `STORE` 的 `handler_type` 是 `BLOCKING` 而 `REGISTER_KV_CACHE` 是 `SYNC`？

**答案**：STORE/RETRIEVE 要和 GPU 同步（等 CUDA event、搬显存），是慢且可能阻塞的操作，必须丢线程池，避免卡死 ZMQ 主循环；REGISTER_KV_CACHE 只是登记元数据，纯 CPU 簿记、很快，可直接在主循环跑。把慢操作放池里、快操作留主循环，是 u3-l2 讲过的 AFFINITY/NORMAL 线程池分工在协议层的体现。

---

### 4.3 HTTP API 自动发现与扩展（http_api_registry.py）

#### 4.3.1 概念说明

前面两套协议（二进制 socket、ZMQ RPC）都是**数据面**（搬 KV cache 的热路径）。而 HTTP/REST 是**控制面/管理面**：注册实例、查配额、改配置、blend 指纹上报、预取……这些操作频率低、但要给人或运维系统调用，所以用人类友好的 HTTP + JSON。

LMCache 用 FastAPI 实现 HTTP，并设计了一套「**约定优于配置**」的插件式自动发现机制：你只要往 `http_apis/` 目录丢一个名字以 `_api` 结尾、且模块级有一个 `router`（`APIRouter`）的 Python 文件，它就会被自动挂到 app 上——不用改任何注册表。

这套机制由两个文件配合：`http_api_registry.py`（MP server 用的注册器类）和 `router_discovery.py`（通用扫描函数）。值得一提的是，**coordinator 不用 `HTTPAPIRegistry` 这个类，而是直接调 `discover_api_routers`**——同一个发现函数，两个宿主各自取用。

#### 4.3.2 核心流程

MP server 的 HTTP API 装配流程：

```
FastAPI app 创建
  │
  ▼
HTTPAPIRegistry(app).register_all_apis()
  │  apis_path = .../multiprocess/http_apis
  │  discover_api_routers(apis_path, package)
  │     pkgutil.iter_modules 扫描目录
  │     过滤：模块名以 "_api" 结尾 且 拥有 router(APIRouter) 属性
  │  把每个 router include 进 self.router
  ▼
app.include_router(self.router)
```

coordinator 的装配流程几乎一样，只是少了一层包装：

```
for router in discover_api_routers(apis_path, package):
    app.include_router(router)
```

#### 4.3.3 源码精读

先看通用扫描函数 `discover_api_routers`：

[lmcache/v1/utils/router_discovery.py:17-54](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/utils/router_discovery.py#L17-L54) —— 它接受一个目录路径、对应的包名、后缀（默认 `_api`）和可选的排除列表。核心逻辑：

1. `pkgutil.iter_modules([search_path])` 列出目录下所有模块名。
2. 只保留名字以 `suffix`（`_api`）结尾的模块。
3. `importlib.import_module` 导入它，检查是否有 `router` 属性且是 `APIRouter` 实例。
4. 命中则收集进结果列表。

这套约定的「契约」就是：**文件名 `xxx_api.py` + 模块级 `router = APIRouter()`**。满足这两条即被自动注册，不满足则被忽略——典型的插件模式。

再看 MP server 的注册器：

[lmcache/v1/multiprocess/http_api_registry.py:15-44](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/http_api_registry.py#L15-L44) —— `HTTPAPIRegistry` 在 `register_all_apis()` 里：定位 `http_apis` 目录，调 `discover_api_routers` 拿到一批 router，先全部 `include` 进自己的聚合 `self.router`，最后一次性 `app.include_router(self.router)`。它本质上是对「发现 + 聚合 + 挂载」三步的封装。

MP server 的 `http_apis/` 目录确实遵循这个约定，文件名都是 `*_api.py`：`cache_api.py`、`common_api.py`、`config_api.py`、`info_api.py`、`quota_api.py`、`reconfigure_api.py`。其中 `common_api.py` 还演示了**嵌套发现**：

[lmcache/v1/multiprocess/http_apis/common_api.py:32-46](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/http_apis/common_api.py#L32-L46) —— `common_api.py` 自己是一个 `*_api` 模块（有 `router`），但它内部又用 `discover_api_routers` 去扫描 `internal_api_server/common` 目录，把那里的路由合并进来。这样 MP server 复用了与 vLLM-embedded API server 共享的一批路由，并用 `_MP_INCOMPATIBLE_MODULES`（[L36](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/http_apis/common_api.py#L36)）排除依赖 vLLM 专属属性的路由。

最后看 coordinator 怎么用同一个函数：

[lmcache/v1/mp_coordinator/app.py:186-189](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py#L186-L189) —— coordinator 的 `create_app` 里直接 `for router in discover_api_routers(apis_path, package): app.include_router(router)`，扫描的是 `mp_coordinator/http_apis/`（`instances_api.py`、`quota_api.py`、`cache_api.py`、`blend_directory_api.py`）。这印证了：**发现逻辑是公共底座，MP server 包成类、coordinator 直接用函数**。

#### 4.3.4 代码实践

**实践目标**：验证 `_api` 后缀 + 模块级 `router` 是自动注册的两个必要条件。

**操作步骤**（源码阅读型实践，不改源码）：

1. 进入 `lmcache/v1/multiprocess/http_apis/`，列出所有 `*_api.py` 文件，逐个打开确认它们顶部都有 `router = APIRouter()`。
2. 进入 `lmcache/v1/mp_coordinator/http_apis/`，做同样确认。
3. 阅读一个具体路由，例如 [instances_api.py:31-37](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/instances_api.py#L31-L37)：`router = APIRouter()` 后用 `@router.post("/instances")` 装饰一个 handler，handler 形参 `body: RegisterRequest` 直接绑定 Pydantic 模型（下一节讲）。

**需要观察的现象**：

1. 两个 `http_apis/` 目录里，所有被自动注册的文件都满足「文件名 `_api` 结尾 + 模块级 `router`」。
2. `dependencies.py`、`error_handlers.py`、`schemas.py` 这些文件**没有**以 `_api` 结尾，因此即使放在同目录也不会被当作路由模块——它们是辅助代码（依赖注入、异常处理、数据模型）。
3. `common_api.py` 同时是「被发现的插件」和「发现别人的宿主」。

**预期结果**：你能口头复述「丢一个 `myfeature_api.py` + 里面写 `router = APIRouter()`，重启服务后它的路由就出现在 app 里，无需改任何注册表」。

#### 4.3.5 小练习与答案

**练习 1**：如果你想给 MP server 加一个 `/hello` endpoint，最少要改几处？

**答案**：理论上 0 处现有代码——只需新建 `lmcache/v1/multiprocess/http_apis/hello_api.py`，里面 `router = APIRouter()` 并 `@router.get("/hello")`。自动发现会把它挂上。前提是你的 endpoint 不需要特殊的 `app.state` 属性（否则要像 `common_api.py` 那样处理兼容性）。

**练习 2**：为什么 coordinator 用 `discover_api_routers` 函数，而 MP server 多包了一层 `HTTPAPIRegistry` 类？

**答案**：MP server 的 HTTP 路由来自**多个异构来源**（自身的 `*_api` + 经 `common_api` 合并的 `internal_api_server/common`），需要先聚合成一个 `APIRouter` 再挂载，故封装成类更整洁；coordinator 的路由只来自单一目录 `mp_coordinator/http_apis/`，逐个 `include_router` 即可，不必引入额外抽象。这是「按需复杂度」的取舍。

---

### 4.4 schemas.py：REST 线缆契约与双端共享

#### 4.4.1 概念说明

HTTP/REST 用 JSON 传消息，但「字段叫什么、什么类型、取值范围」不能靠默契，必须写成机器可校验的模型。`mp_coordinator/schemas.py` 用 Pydantic `BaseModel` 定义了 coordinator REST API 的全部请求/响应模型。

它最关键的设计原则写在文件头注释里：**「coordinator 与 mp server 双端共享同一份 schema」**。也就是说，coordinator 用这些模型校验入站请求、塑形出站响应；mp server 在注册（`registrar.py`）和 blend 上报（`blend_client.py`）时 `import` 同一组模型来构造请求体、解析应答。**一处定义、两端共用，避免各写各的字典导致字段漂移。**

这套模型按业务域分成几组：成员管理（membership）、配额（quota）、用量上报（usage）、blend 指纹目录（blend）、预取/钉住（prefetch/pin）。

#### 4.4.2 核心流程

一个 REST 请求在 coordinator 侧的生命周期：

```
mp server 构造 RegisterRequest(instance_id=..., ip=..., http_port=...)
  │  registrar.py 用同一模型 POST 到 coordinator
  ▼
coordinator FastAPI 收到 JSON body
  │  Pydantic 自动校验 -> 不合法返回 422（不是 500）
  ▼
route handler 收到已校验的 RegisterRequest 对象
  │  调 registry.register(...)
  ▼
返回 RegisterResponse(instance_id=..., re_registered=...)
  │  Pydantic 序列化为 JSON
  ▼
mp server 解析为同一个 RegisterResponse 类型
```

校验发生在 handler **之前**，所以 handler 里可以假设字段一定合法——这是 Pydantic + FastAPI 的标准好处。注意 `BlendMatchRequest.tokens_b64` 还用 `@field_validator` 在校验期就拒绝坏 base64，让 FastAPI 返回 422 而非把 `ValueError` 漏成 500（[L324-344](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L324-L344)）。

#### 4.4.3 源码精读

先看成员注册模型：

[lmcache/v1/mp_coordinator/schemas.py:66-102](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L66-L102) —— `RegisterRequest` 的字段约束很说明问题：

- `instance_id`：`StringConstraints(strip_whitespace=True)`，默认空串——空则由 coordinator 生成并返回（见 `RegisterResponse`）。
- `ip`：`min_length=1`，空白被拒，因为 coordinator 要回连这个地址。
- `http_port`：`Field(ge=1, le=65535)`，端口范围约束。
- `p2p_advertised_url` / `mq_port`：可选，P2P 不参与时为空/0。

[lmcache/v1/mp_coordinator/http_apis/instances_api.py:34-65](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/instances_api.py#L34-L65) —— 这就是模型的消费侧：handler 形参 `body: RegisterRequest`，FastAPI 自动校验并注入；handler 内直接用 `body.ip`、`body.http_port`，返回 `RegisterResponse(...)`。两端用同一个类，字段不可能漂移。

再看配额与用量模型：

[lmcache/v1/mp_coordinator/schemas.py:118-166](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L118-L166) —— `SetQuotaRequest`（`limit_gb >= 0`、`tier` 默认 L2）、`QuotaConfigRequest`（`default_limit_gb` 可为 `None`，表示未配额的 salt 免于淘汰——这正是最近 commit `2756b828` 引入的「默认配额上限」语义）。

[lmcache/v1/mp_coordinator/schemas.py:180-219](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L180-L219) —— `UsageEvent` + `ReportUsageRequest`：mp server 批量上报 store/lookup/delete 事件，带单调递增 `seq`（从 1 开始），coordinator 据此做用量统计与淘汰决策（呼应 u3-l3 的 quota_manager）。

接着是 blend 指纹目录模型（最复杂的一组）：

[lmcache/v1/mp_coordinator/schemas.py:253-368](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L253-L368) —— `StoreRangeModel`（上报一段已存 token 区间，每 chunk 对应一个 `object_key`）、`BlendMatchRequest`（用 `tokens_b64` 描述请求 token）、`BlendMatchResponse`（返回 `GlobalMatchModel` 列表：`object_key` + `old_st`（存储序列里的位置，re-RoPE 源）+ `cur_st`（请求里的位置，re-RoPE 目标））。这与 u3-l3 讲的「coordinator 做所有哈希、按 token 描述内容」完全对应——调用方**无法构造内部 cache key**，只能描述 token。

最后看那个紧凑编码工具：

[lmcache/v1/mp_coordinator/schemas.py:24-37](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L24-L37) —— `encode_tokens` 把 token id 列表先转成小端 `uint32` 数组，再 base64。注释说明动机：token id 不超过 uint32，所以小端 uint32 buffer 比 JSON 整数列表**紧凑得多**，且接收端一次 `np.frombuffer` 就能解码（[L40-63](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L40-L63) 的 `decode_tokens` 直接喂给哈希器）。这是「REST 虽然用 JSON，但在热点字段上仍做二进制紧凑编码」的实用折中。

#### 4.4.4 代码实践

**实践目标**：体会 Pydantic 模型的「校验前置」与双端共享。

**操作步骤**（示例代码）：

```python
# 示例代码：用 RegisterRequest 体验校验
from pydantic import ValidationError
from lmcache.v1.mp_coordinator.schemas import RegisterRequest, RegisterResponse

# 合法请求
req = RegisterRequest(instance_id="", ip="10.0.0.1", http_port=8000)
print(req.instance_id, req.ip, req.http_port)   # "" 10.0.0.1 8000

# 非法请求：ip 为空 / 端口越界 -> 抛 ValidationError
for bad in [dict(ip="", http_port=8000), dict(ip="10.0.0.1", http_port=99999)]:
    try:
        RegisterRequest(**bad)
    except ValidationError as e:
        print("rejected:", e.errors()[0]["type"])
```

**需要观察的现象**：

1. 合法请求构造成功；`instance_id=""` 被保留（由 coordinator 生成）。
2. `ip=""` 触发 `string_too_short`（因 `min_length=1`），`http_port=99999` 触发 `greater_than_le`（因 `le=65535`）。
3. 这些错误在 FastAPI 里会自动变成 HTTP 422 响应，而不会进入 handler——handler 拿到的对象一定是合法的。

**预期结果**：你能解释「为什么 `instances_api.py` 的 handler 里没有一行 `if not body.ip` 的检查」——因为 Pydantic 已经在更前面拦住了。如果本地未装 pydantic，标注「待本地验证」，仅做源码阅读：在 [schemas.py:86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L86) 找到 `min_length=1` 与 [L87](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/schemas.py#L87) 的 `ge=1, le=65535`，说明约束来源。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BlendMatchRequest` 用 `tokens_b64`（base64 的 uint32 buffer）而不是 `tokens: list[int]`？

**答案**：blend match 的请求可能带几千上万个 token。JSON 整数列表每个 token 至少几字节还有逗号分隔，体积大、解析慢；小端 uint32 buffer 每 token 恰好 4 字节，base64 后紧凑，接收端 `np.frombuffer` 一步解码直接喂多项式哈希器。在 REST 的便利性与热点的效率之间取了平衡。

**练习 2**：`QuotaConfigRequest.default_limit_gb` 为什么允许 `None`？

**答案**：`None` 是有语义的——表示「未显式配额的 salt 免于淘汰」（exempt）。这与「配额为 0（禁止写）」不同。最近 commit `2756b828` 给了未配额 salt 一个默认上限，把 `None` 的「免淘汰」语义变成了可配置项，是 quota 演进的一部分（呼应 u3-l3、u4-l5）。

---

## 5. 综合实践

**综合任务**：把本讲三套协议串起来，完成一张「LMCache 通信协议全景对照表」，并用它解释一个真实问题。

### 步骤 1：填写对照表

按下表逐项填写（答案见下方「参考答案」，建议先自己填再核对）：

| 维度 | 独立 server (`v1/protocol.py`) | MP daemon (`multiprocess/protocol.py`) | HTTP/REST (`http_apis` + `schemas.py`) |
|------|------|------|------|
| 承载进程 | ? | ? | ? |
| 传输载体 | 裸 TCP socket | ? | HTTP/JSON |
| 编码方式 | `struct` 定长二进制 | ? | Pydantic 模型 ↔ JSON |
| PUT 等价命令 / meta 字段 | `ClientCommand.PUT`，头部 9 整数+150 字节 key | ? | （无 PUT，对应 POST `/cache/*`） |
| GET 等价命令 / meta 字段 | `ClientCommand.GET`，头部同上 | ? | （对应 GET `/cache/*`） |
| 张量字节是否经过协议 | 是（头部后跟 length 字节） | ? | 否（控制面不搬张量） |
| 扩展方式 | 改 `ClientCommand` 枚举 + 分支 | ? | 丢一个 `*_api.py` 文件 |

### 步骤 2：用对照表回答

回答这个真实问题：「为什么 LMCache 不把 MP 协议也设计成独立 server 那样的定长头部，而要搞 msgspec + 枚举 + Pydantic 三套？」

### 参考答案（步骤 1）

| 维度 | 独立 server | MP daemon | HTTP/REST |
|------|------|------|------|
| 承载进程 | `lmcache_server`（独立 TCP KV 字典） | MP cache server daemon | coordinator + MP server 控制面 |
| 传输载体 | 裸 TCP socket | ZMQ ROUTER/DEALER 多帧 | HTTP/JSON |
| 编码方式 | `struct` 定长二进制 | msgspec/msgpack + `RequestType` 查表 | Pydantic ↔ JSON |
| PUT 等价 | `ClientCommand.PUT`，`ClientMetaMessage`（186 字节：command/length/fmt/dtype/location/shape×4/key150） | `RequestType.STORE`，payload=`[KeyType, instance_id, gpu_block_ids, event_ipc_handle]`，响应 `tuple[bytes,bool]` | POST 类 endpoint（如 `/cache/objects`、blend 上报） |
| GET 等价 | `ClientCommand.GET`，头部同 PUT | `RequestType.RETRIEVE`，payload=`[KeyType, instance_id, gpu_block_ids, event_ipc_handle, skip_first_n_tokens]` | GET 类 endpoint |
| 张量字节 | 经过协议 | **不经过**（IPC handle 零拷贝，u3-l2） | 不经过 |
| 扩展方式 | 改 `ClientCommand` + 加 `case` 分支 | 加 `RequestType` 枚举 + 加 `ProtocolDefinition`（启动校验保同步） | 新建 `*_api.py` + 模块级 `router`（自动发现） |

### 参考答案（步骤 2）

三者服务的场景不同，抽象层级不同：独立 server 是「极简单机远程字典」，定长头部解析最快、依赖最少；MP daemon 是「高频、多命令、跨进程零拷贝」的数据面，命令种类多且会增长，用枚举 + 定义表 + 启动校验才能在演进中不漏不错，且必须避开搬运张量字节；HTTP/REST 是「低频、人/运维可读」的控制面，用 Pydantic 模型把线缆契约固化为可校验代码，并用自动发现支持插件式扩展。**一套尺寸不能通吃三个面**，所以 LMCache 让它们各司其职。

## 6. 本讲小结

- LMCache 同时存在三套通信协议：独立 server 的裸 TCP 二进制头部、MP daemon 的 ZMQ + msgspec RPC、HTTP/REST 控制面，分别服务 `lmcache_server`、MP cache daemon、coordinator/MP server 管理面。
- `v1/protocol.py` 用 `struct` 把 `ClientMetaMessage` 打成定长 186 字节（9 整数 + 150 字节 key）、`ServerMetaMessage` 36 字节；`ServerReturnCode` 借用 200/400 的 HTTP 语义但本身不是 HTTP。
- `multiprocess/protocol.py` 是门面，真正的协议知识在 `protocols/` 子包；核心是 `RequestType` 枚举 + `ProtocolDefinition`（payload/response/handler_type）+ `HandlerType`（SYNC/BLOCKING）三件套。
- `initialize_protocols()` 在启动期做「枚举 ⇄ 定义」双向一致性校验，任何一边不一致就 fail-fast，绝不带残缺协议运行。
- MP 协议的 STORE/RETRIEVE 只传 block id + CUDA event IPC handle，**张量字节不过 ZMQ**，与独立 server「头部 + 原始字节」形成本质对比。
- HTTP 路由靠「文件名 `_api` 结尾 + 模块级 `router`」的约定自动发现（`discover_api_routers`），MP server 包成 `HTTPAPIRegistry`、coordinator 直接用函数；`schemas.py` 的 Pydantic 模型是 coordinator 与 mp server 双端共享的线缆契约。

## 7. 下一步学习建议

- **想深入 MP daemon 的收发实现**：回到 u3-l2，结合本讲的 `RequestType`/`ProtocolDefinition`，重读 `mq.py` 的 `send_multipart` / `recv_multipart` 与 handler 分派，把「帧结构 + 协议表 + 线程池」三件事对齐。
- **想理解 coordinator 的业务逻辑**：本讲只讲了 schemas 的「形状」，下一站读 `mp_coordinator/app.py` 的 lifespan（health/eviction/resync 后台循环）、`registry.py` 的会员判活、`blend_directory.py` 的指纹匹配（u3-l3 已铺垫）。
- **想看协议如何被 handler 消费**：读 `v1/server/__main__.py` 的 `match meta.command` 分派，对比 MP server 侧 handler 如何据 `HandlerType` 决定走主循环还是线程池。
- **想扩展 HTTP API**：仿照 `instances_api.py`，在一个 `*_api.py` 里加一个最小 endpoint，验证自动发现机制；若有兴趣做协议扩展，可在临时副本里体会 `protocols/` 的「加枚举 + 加定义 + 启动校验」流程（注意本讲禁止改真实源码）。
- **后续单元衔接**：本讲是 u3（MP 架构）的收尾之一；接下来进入 u4 专家层，`u4-l2`/`u4-l3` 会讲 distributed L1/L2 存储与可插拔 L2 适配器——那里的 `EncodedObjectKey`（schemas 里反复出现的类型）就是其线缆表示，本讲为它打了基础。
