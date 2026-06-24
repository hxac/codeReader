# 多语言绑定与集成生态

## 1. 本讲目标

BUS/RT 的「内核」是用 Rust + Tokio 写的，但现实世界里很多业务并不在 Rust 里跑——脚本、运维工具、前端服务、数据分析常常用 Python 或 JavaScript。本讲要回答的核心问题是：

> **为什么一个用 Rust 写的消息总线，可以被 Python、JavaScript 直接拿来用，而且客户端之间还能彼此互通？**

读完本讲，你应当能够：

- 说出 BUS/RT 官方提供了哪几种语言绑定，它们各自是同步还是异步。
- 理解「跨语言互通」的根本原因：所有绑定都各自**重新实现同一套二进制线协议**，而不是通过 FFI 调 Rust。
- 读懂 Python 同步绑定 `client.py` / `rpc.py`、异步绑定 `busrt_async`、以及 JS/TS 绑定 `busrt.ts` 的关键代码。
- 根据运行环境（普通线程 / 事件循环 / 实时调度）正确选择同步还是异步绑定。

本讲是整个手册的最后一篇。它把前面 u2（核心类型与协议）、u4（IPC 客户端）、u5（RPC 层）讲过的 Rust 实现当成「参照系」，去验证一个结论：**协议是契约，语言只是壳**。

## 2. 前置知识

本讲默认你已经掌握以下概念（它们在前置讲义中已建立，这里只做最小回顾）：

- **二进制线协议**（u2-l3）：客户端→代理是 9 字节头 `op_id(4) | flags(1) | len(4)`；代理→客户端是 6 字节头 `kind(1) | len(4) | realtime(1)`；握手发送 `0xEB` 魔数 + 小端版本号；`PING_FRAME` 是 9 字节全零。
- **flags 字节的位打包**（u2-l3）：\(\text{flags} = \text{op} \,\lor\, (\text{qos} \ll 6)\)，操作码占低 6 位、QoS 占高 2 位。
- **QoS 的位语义**（u2-l1、u7-l1）：低位 `qos & 0b1` 决定是否需要 ACK（`Processed`）；高位 `qos & 0b10` 决定是否实时刷新。
- **RPC 层协议**（u5-l1）：在普通消息帧的载荷首字节用 `0x00/0x01/0x11/0x12` 区分通知/请求/回复/错误；`id == 0` 表示「不需要回复」；错误码用 `-32xxx` 约定段。
- **`.broker` 核心 RPC**（u5-l3）：代理进程自己注册了一个名为 `.broker` 的内部客户端，内置 `test` / `info` / `stats` / `client.list` 方法，调用它就是一次普通的点对点 RPC。

如果你对这些概念感到陌生，建议先回到对应讲义。本讲不会再解释它们，而是**用另外三种语言把同样的协议再实现一遍**给你看。

## 3. 本讲源码地图

本讲涉及的源码全部在 `bindings/` 目录下，它们是独立于 Rust crate 之外的项目（各自有自己的 `setup.py` / `package.json`）：

| 文件 | 作用 | 语言 |
|------|------|------|
| [README.md:41-48](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L41-L48) | 总览所有官方语言绑定与发布渠道 | — |
| [bindings/python/busrt/README.md](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/README.md) | Python 同步绑定的安装与用法示例 | Python |
| [bindings/python/busrt/busrt/client.py](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py) | Python 同步客户端：握手、收发帧、ACK | Python |
| [bindings/python/busrt/busrt/rpc.py](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py) | Python 同步 RPC 层：notify/call0/call | Python |
| [bindings/python/busrt_async/busrt_async/client.py](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/client.py) | Python 异步客户端（asyncio 版） | Python |
| [bindings/python/busrt_async/busrt_async/rpc.py](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/rpc.py) | Python 异步 RPC 层 | Python |
| [bindings/js/busrt/src/busrt.ts](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts) | JS/TS 客户端 + RPC 层（单文件） | TypeScript |

> 注意：Dart 绑定由社区维护（[github.com/AndreiLosev/busrt_client](https://github.com/AndreiLosev/busrt_client)），不在本仓库内，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 多语言绑定总览：为什么跨语言能互通

#### 4.1.1 概念说明

先看 README 给出的官方绑定清单：

[README.md:41-48](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L41-L48) 列出了 Python（同步 `busrt` / 异步 `busrt-async`）、JavaScript（Node.js `busrt`）、Dart 四种绑定，外加 Rust crate 本身。

这里有一个关键认知：**这些绑定和 Rust crate 之间没有任何 FFI（外部函数接口）调用关系**。Python 包 `busrt` 不会去链接 Rust 的 `.so`，JS 包 `busrt` 也不会去 require Rust 编译产物。它们能和 Rust 写的 `busrtd` 互通，纯粹是因为**它们各自用本语言把同一套二进制协议从头实现了一遍**。

这带来三个直接后果：

1. **互通的唯一契约是字节流**。只要握手、帧头、ACK、RPC 字节布局对得上，Rust / Python / JS 客户端可以同时连同一个 `busrtd`，彼此收发消息、互相发起 RPC，互不感知对方用什么语言写的。
2. **绑定是「平行的重新实现」**。每种语言的客户端各自处理自己的 socket、并发、缓冲、错误，但发出去的线协议字节完全一致。
3. **协议常量在各绑定里被重复定义**。你会看到 `GREETINGS = 0xEB`、`OP_MESSAGE = 0x12` 这些魔法数字在 Python、JS、Rust 三处各出现一次——它们必须保持同步，否则就连不上。

#### 4.1.2 核心流程：一次跨语言调用的字节旅程

假设一个 Python 客户端要向 Rust 代理发起一次 RPC `call`。整个数据流是语言无关的：

```text
Python rpc.call()
   │  拼装 RPC_REQUEST header: [0x01][id:4][method][0x00]
   ▼
Python client.send()  ──拼 9 字节头──▶  socket 字节流
   │   flags = type | (qos << 6)
   ▼
        ╔════════ 线协议（与 Rust ipc::Client 发出的字节完全相同） ════════╗
        ▼
Rust broker handle_reader  ──解析 9 字节头──▶  按 op 分发
   │
   ▼
Rust .broker 核心 RPC 处理 ──回复──▶  6 字节头帧
   ▼
        ╔════════ 线协议 ════════╗
        ▼
Python _t_reader 解析 6 字节头 ──按 call_id 兑现 oneshot/Event──▶ 返回结果
```

关键点：中间那段「线协议」字节是**语言中立的**。Python 端怎么拼、Rust 端怎么解，是两套独立代码，但只要字节一致就能通。

#### 4.1.3 源码精读：协议常量的「三处镜像」

为了让你直观看到「平行实现」，下面把同一些协议常量在 Rust、Python、JS 三处的定义并排放出来。注意它们的值必须**完全相等**。

Rust 侧（`src/lib.rs`，u2-l3 已讲）：

```rust
pub const PROTOCOL_VERSION: u16 = 0x01;   // src/lib.rs:21
pub const RESPONSE_OK: u8 = 0x01;         // src/lib.rs:23
pub const GREETINGS: [u8; 1] = [0xEB];    // src/lib.rs:37
```

Python 同步侧（[client.py:8-30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L8-L30)）：

```python
GREETINGS = 0xEB
PROTOCOL_VERSION = 1
OP_MESSAGE = 0x12
OP_BROADCAST = 0x13
OP_ACK = 0xFE
RESPONSE_OK = 0x01
PING_FRAME = b'\x00' * 9
```

JS/TS 侧（[busrt.ts:6-9](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L6-L9)）：

```typescript
const GREETINGS = 0xeb;
const PROTOCOL_VERSION = 1;
const PING_FRAME = Buffer.from([0, 0, 0, 0, 0, 0, 0, 0, 0]);
const RESPONSE_OK = 1;
```

三处的 `0xEB`、`1`、9 字节全零 ping、`RESPONSE_OK = 1` 一模一样。这就是「协议一致性」的物证。

> 小提醒：Python 绑定里只定义了用到的那几个常量（如 `OP_PUBLISH=1`、`OP_MESSAGE=0x12`），而 Rust `lib.rs` 是完整的「字典」。绑定的常量表是 Rust 的子集——只包含客户端需要发送或解析的部分。

#### 4.1.4 代码实践：协议常量交叉核对

**实践目标**：亲手验证三种语言的协议常量确实一致，建立「字节是唯一契约」的直觉。

**操作步骤**：

1. 打开 `src/lib.rs`，找到 `GREETINGS`、`PROTOCOL_VERSION`、`OP_*`、`ERR_*` 常量。
2. 打开 `bindings/python/busrt/busrt/client.py` 第 8–30 行，逐行比对同名常量。
3. 打开 `bindings/js/busrt/src/busrt.ts` 第 6–19 行的 `BusOp` 枚举与常量。

**需要观察的现象**：三处的操作码（`OP_PUBLISH=1`、`OP_SUBSCRIBE=2`、`OP_MESSAGE=0x12`、`OP_BROADCAST=0x13`、`OP_ACK=0xFE`）取值完全相同。

**预期结果**：你能画出一张「常量名 → 数值」的对照表，且三个语言版本无差异。这张表就是跨语言互通的字节级保证。

> 待本地验证：若你修改其中任一绑定的某个常量值，该绑定将无法连接 `busrtd`（握手或帧解析会失败）——这反向证明常量必须严格一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Python 绑定不需要 `#[link]` 或 `ctypes` 去调用 Rust 的 `ipc::Client`？

**参考答案**：因为 BUS/RT 的跨语言互通建立在「同一套二进制线协议」之上，Python 客户端自己用标准库 `socket` 直接说「协议语言」，不需要调用 Rust 函数。Rust 只在代理（`busrtd`）一端运行。

**练习 2**：JS 绑定的 `BusErrorCode` 枚举里，`Timeout = -32120`（[busrt.ts:38](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L38)）。请对照 Rust 的 `ERR_TIMEOUT = 0x78`，说明这个 `-32120` 是怎么算出来的。

**参考答案**：`0x78 = 120`。RPC 错误码约定为 `-32000 - code`（见 u5-l1 与 [rpc.py:97-98](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L97-L98)），所以 \(-32000 - 120 = -32120\)。三种语言的错误码换算公式完全一致。

---

### 4.2 Python 同步绑定：client.py 与 rpc.py

#### 4.2.1 概念说明

`busrt`（[pypi.org/project/busrt](https://pypi.org/project/busrt/)）是官方 Python **同步**绑定。它的设计哲学是「零外部依赖、纯标准库」——只用了 `socket`、`threading`、`logging`、`time`，连 msgpack 都是用户自己在业务层引入的（见 [README.md:66-103](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/README.md#L66-L103) 里 `import msgpack`）。

「同步」在这里的含义：API 是阻塞调用（`bus.connect()`、`bus.send(...).wait_completed()`），并发靠**操作系统线程**实现——一个读线程、一个 ping 线程、外加你的业务线程。这与 Rust 侧的 `sync` 模块（u7-l4）思路一致，对应 Rust 的 `ipc::Client`（异步）和 `sync::ipc::Client`（同步）这对兄弟。

它分成两层，和 Rust 的结构完全对应：

| Python 类（sync） | 对应 Rust 概念 | 作用 |
|-------------------|----------------|------|
| `client.Client` | `ipc::Client` | 握手、帧编解码、收发 |
| `client.Frame` | `FrameData` / `borrow::Cow` 载荷 | 一条业务帧 |
| `client.ClientFrame` | `OpConfirm`（oneshot） | 等待 ACK 的句柄 |
| `rpc.Rpc` | `RpcClient` + `processor()` | RPC 客户端 + 事件循环 |
| `rpc.Event` / `Request` / `Reply` | `RpcEvent` | RPC 语义对象 |

#### 4.2.2 核心流程：连接与收发的三线程模型

`Client` 启动后会常驻**两个守护线程**，外加调用方自己的业务线程：

```text
connect()  ──握手成功──▶  spawn _t_reader 线程  (常驻：解析入站帧)
                         spawn _t_pinger 线程  (常驻：每 ping_interval 秒发 PING_FRAME)
业务线程  ──send()──▶  socket_lock 串行化写  ──▶  socket
                       (若 qos & 0b1：登记 ClientFrame 到 self.frames[frame_id])
```

握手流程（[client.py:53-89](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L53-L89)）严格复刻 u2-l3：

1. 按 path 后缀选 Unix socket（`.sock`/`.socket`/`.ipc`/以 `/` 开头）或 TCP（关 Nagle）。
2. 读 3 字节问候：校验 `buf[0] == 0xEB` 与 `buf[1:3]` 小端版本号。
3. 把读到的 3 字节**原样回写**（对称握手），读 1 字节 `RESPONSE_OK`。
4. 发 `u16` 小端长度 + 名字字节，再读 1 字节 `RESPONSE_OK`。
5. 置 `connected=True`，拉起 reader / pinger 两个线程。

发送流程（[client.py:174-213](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L174-L213)）拼出 9 字节头：

```text
frame_id(4,Little-Endian) | flags(1) | len(4,Little-Endian) | body
```

其中 `flags = frame.type | (frame.qos << 6)`（[client.py:184](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L184)），与 Rust 的位打包公式完全相同。若 `qos & 0b1 != 0`（需要 ACK），就把 `ClientFrame` 按 `frame_id` 登记进 `self.frames`，等 reader 线程收到 `OP_ACK` 帧后兑现。

读线程（[client.py:111-155](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L111-L155)）解析 6 字节头：

- `buf[0] == OP_NOP`：丢弃（保活）。
- `buf[0] == OP_ACK`：取 `buf[1:5]` 的 `op_id`，从 `self.frames` 弹出对应 `ClientFrame`，把 `buf[5]`（结果码）写入并 `set()` 唤醒业务线程——这正是 u4-l1 里 `OpConfirm` 的 Python 版兑现。
- 其余：读 `buf[1:5]` 长度的 body，按 `0x00` 切出 sender / topic / payload，封装成 `Frame`，回调 `self.on_frame`。

#### 4.2.3 源码精读

**Frame 与零拷贝 header 字段**（[client.py:252-260](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L252-L260)）：

```python
class Frame:
    def __init__(self, payload=None, tp=OP_MESSAGE, qos=0):
        self.payload = payload
        # used for zero-copy
        self.header = None
        self.type = tp
        self.qos = qos
```

`header` 字段对应 Rust 的 `FrameData.header`（线程内 / RPC 免拼接控制字节）。Python 在 `send()` 里把它夹在 `target` 与 `payload` 之间（[client.py:199-206](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L199-L206)）：先写 `header`，再单独 `sendall(payload)`，让大载荷成为一次独立的系统调用——这是 Python 版的「分两次写」零拷贝技巧（对照 Rust 用 `TtlBufWriter` 分段刷新）。

**RPC 层：call0 与 call**（[rpc.py:72-115](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L72-L115)）。这里复刻了 u5-l1 / u5-l2 的全部约定：

```python
def call0(self, target, request):
    # id 填全零 => 不需要回复
    request.header = RPC_REQUEST_HEADER + b'\x00\x00\x00\x00' + \
            request.method + b'\x00'
    return self.client.send(target, request)

def call(self, target, request):
    with self.call_lock:
        call_id = self.call_id + 1
        if call_id == 0xffff_ffff:
            self.call_id = 0           # 回绕到 0，下一轮变 1，避开"id=0=不回复"
        else:
            self.call_id = call_id
    ...
    request.header = RPC_REQUEST_HEADER + call_id.to_bytes(4, 'little') + \
                     request.method + b'\x00'
```

注意拼出的 header 字节布局 `[0x01][id:4][method][0x00]`，与 Rust `prepare_call_payload`、JS `RpcRequest.header` **逐字节相同**。`call_id` 在 `0xffff_ffff` 处回绕，与 Rust `RpcClient` 的回绕逻辑一致（u5-l2）——都是为了避开「id=0 表示不需要回复」这个哨兵。

**RPC 事件循环：`_t_handler`**（[rpc.py:123-178](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L123-L178)）。`Rpc` 把自己的 `_handle_frame` 注册成 client 的 `on_frame`（[rpc.py:58](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L58)），收到帧后 `spawn` 一个新线程处理（[rpc.py:117-121](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L117-L121)）。这就是 Python 版的「processor 事件循环」，每个 RPC 请求一条线程——和 Rust `processor()` 里 `spawn` 每个请求一个 task 是同构的，只是把 tokio task 换成了 OS 线程。

收到 `RPC_REQUEST` 且 `call_id != 0` 时，它会调用 `on_call` 拿到返回值，拼成 `RPC_REPLY_HEADER + call_id_b`（`0x11`+id）回送；若 handler 抛 `RpcException`，则拼 `RPC_ERROR_REPLY_HEADER + call_id_b + code(2,有符号小端)`（`0x12`+id+code）（[rpc.py:141-156](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L141-L156)）。这和 u5-l1 描述的错误帧布局 `[0x12][id:4][code:2]` 完全吻合。

#### 4.2.4 代码实践：运行官方 sender 示例

**实践目标**：用 Python 同步客户端向 Rust 代理发消息，亲眼看到跨语言互通。

**操作步骤**：

1. 在一个终端启动 Rust 代理（参照 u1-l2 的 `test.sh server`）：
   ```bash
   cargo run --features server --bin busrtd -- -B /tmp/busrt.sock
   ```
2. 安装 Python 绑定：`pip3 install busrt msgpack`。
3. 运行仓库自带的发送示例 [bindings/python/busrt/example_sender.py](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/example_sender.py)。

**需要观察的现象**：示例会先打印一条 `send` 的返回码（十六进制，`qos=1` 时应为 `0x1` 即 `RESPONSE_OK`），再打印一条广播的返回码，最后静默发出一条 `qos=0` 的 publish。

**预期结果**：Python 进程成功连上 Rust 代理、完成握手、收到 ACK 码 `0x1`。若同时用 Rust 的 `busrt` CLI 执行 `listen #`（u8-l2），你能看到 Python 发出的 `test/topic` publish 消息——证明两种语言在同一个总线上互通。

> 待本地验证：实际返回码与 CLI 是否收到消息，需在本地真实运行确认。

#### 4.2.5 小练习与答案

**练习 1**：`ClientFrame.wait_completed()`（[client.py:243-249](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L243-L249)）在 `qos & 0b1 == 0` 时直接返回 `RESPONSE_OK`，为什么？

**参考答案**：QoS 低位为 0 表示「不需要 ACK」（`QoS::No`），发送时根本没有把 `ClientFrame` 登记进 `self.frames`，也不会有 ACK 帧回来。所以 `wait_completed` 立即返回成功，对应 Rust `make_confirm_channel!` 返回 `None`（u3-l3）。

**练习 2**：Python 绑定的 RPC 回复为什么要把 `call_id_b`（原始 4 字节）原样塞回 reply header，而不是重新 `to_bytes`？

**参考答案**：为了保证「回复帧的 id 与请求帧的 id 逐字节相同」，直接复用请求里解析出的 4 字节小端 `call_id`，避免任何编码差异。客户端收到回复后用这 4 字节作 key 去 `self.calls` 里 `pop` 出等待对象并兑现（[rpc.py:161](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L161)）。

---

### 4.3 Python 异步绑定：busrt_async 的并发模型差异

#### 4.3.1 概念说明

`busrt_async`（[pypi.org/project/busrt-async](https://pypi.org/project/busrt-async/)）是官方 Python **异步**绑定，包名是 `busrt_async`（注意下划线）。它的 API 形状和同步版几乎一一对应（[busrt_async/README.md](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/README.md)），但所有方法都变成 `async def`，并发模型从「多线程」换成「单线程事件循环 + asyncio」。

这正是 Rust 侧 `sync::ipc::Client`（u7-l4）与 `ipc::Client`（u4-l2）的区别在 Python 世界的翻版。选同步还是异步，取决于你的程序是否已经在 asyncio 事件循环里跑——**两个绑定不能混用**（你不能在 async 函数里调用同步版的阻塞 `send`，否则会卡住整个事件循环）。

#### 4.3.2 核心流程：从线程到协程的「一一映射」

异步版的代码结构和同步版是**镜像**的，只是把同步原语换成 asyncio 等价物：

| 概念 | 同步版（threading） | 异步版（asyncio） |
|------|---------------------|-------------------|
| 连接 | `socket.connect` | `asyncio.open_unix_connection` / `open_connection` |
| 互斥 | `threading.Lock` | `asyncio.Lock` |
| 等待事件 | `threading.Event` | `asyncio.Event` |
| 后台读循环 | `threading.Thread(_t_reader)` | `asyncio.ensure_future(_t_reader)` |
| 后台 ping | `threading.Thread(_t_pinger)` | `asyncio.ensure_future(_t_pinger)` |
| sleep | `time.sleep` | `await asyncio.sleep` |
| 超时控制 | `socket.settimeout` | `await asyncio.wait_for(..., timeout)` |

**线协议字节完全不变**。异步版的常量定义（[busrt_async/client.py:8-30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/client.py#L8-L30)）与同步版逐行相同；`send` 拼出的 9 字节头公式（[busrt_async/client.py:184-216](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/client.py#L184-L216)）也逐字节相同。换言之，**异步不是另一种协议，而是同一种协议的另一种并发实现**。

#### 4.3.3 源码精读

**异步连接**（[busrt_async/client.py:55-88](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/client.py#L55-L88)）：

```python
reader, writer = await asyncio.open_unix_connection(self.path, limit=self.buf_size)
...
self.pinger_fut = asyncio.ensure_future(self._t_pinger())
self.reader_fut = asyncio.ensure_future(self._t_reader(reader))
```

握手步骤与同步版一致（读 3 字节问候→回写→读 OK→发名字→读 OK），只是每一步读写都包了 `await asyncio.wait_for(..., timeout=self.timeout)`。读循环 `_t_reader`（[busrt_async/client.py:108-158](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/client.py#L108-L158)）用 `await reader.readuntil(b'\x00')` 流式切出 sender/topic，比同步版的「先读全长再 split」更贴合 asyncio 的流式 API，但解析出的字段含义完全一致。

**异步 RPC call**（[busrt_async/rpc.py:77-116](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/rpc.py#L77-L116)）：与同步版结构相同——`call_lock` 换成 `asyncio.Lock`，`wait_completed` 换成 `await`。回调 `on_call` / `on_notification` 也变成 `async def`，handler 内部可以 `await` 异步 IO（[busrt_async/rpc.py:118-173](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/rpc.py#L118-L173)）。这是异步绑定相对同步绑定最大的能力差异：**RPC handler 可以是协程，能做异步 IO 而不阻塞事件循环**。

#### 4.3.4 代码实践：源码阅读型——对比同步与异步的 send

**实践目标**：通过逐行对比，确认同步与异步绑定发出的是同一串字节。

**操作步骤**：

1. 打开 [client.py:174-213](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L174-L213)（同步 `send`）。
2. 打开 [busrt_async/client.py:184-216](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/client.py#L184-L216)（异步 `send`）。
3. 逐行比对 header 拼装：`frame_id.to_bytes(4,'little') + flags.to_bytes(1) + len.to_bytes(4,'little')`。

**需要观察的现象**：两段代码拼装 9 字节头的逻辑、`flags = frame.type | frame.qos << 6` 的公式、按 `qos & 0b1` 登记等待的逻辑**逐字符相同**，差别只在写 socket 的方式（`socket.sendall` vs `writer.write + await drain`）。

**预期结果**：你得出结论——给定相同的 target / payload / qos / type，同步与异步绑定写出完全相同的字节序列，因此可以连同一个代理、彼此互换。

#### 4.3.5 小练习与答案

**练习 1**：异步绑定里 `ClientFrame.completed` 在 `qos & 0b1 == 0` 时是 `None`（[busrt_async/client.py:246-248](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt_async/busrt_async/client.py#L246-L248)），而同步版是「不创建 `threading.Event`」。这两种处理为什么等价？

**参考答案**：两者都表达「这条消息不需要 ACK，没有等待对象」。异步版显式置 `None`，同步版靠 `qos & 0b1` 分支不创建 Event；调用方在 `wait_completed` 里都走「立即返回 `RESPONSE_OK`」的快路径。语义一致，只是数据表示不同。

**练习 2**：为什么不能在 `busrt_async` 的 `on_call` 协程里直接 `await rpc.call(...)` 调用自己（同一个 Rpc 实例）？

**参考答案**：因为 `_handle_frame` 是单线程事件循环里顺序处理的（参考 u5-l2 的处理器死锁陷阱与 u7-l3 的告诫）。一个 `call` 在等回复时，回复帧也要经同一个 `_t_reader` → `_handle_frame` 才能兑现；若 handler 内同步等待自己的回复，事件循环被占住，回复永远进不来，形成死锁。应在单独的 task 里发起调用。

---

### 4.4 JavaScript / TypeScript 绑定：busrt.ts

#### 4.4.1 概念说明

JS 绑定（[npmjs.com/package/busrt](https://www.npmjs.com/package/busrt)）是一个**单文件** TypeScript 实现（[busrt.ts](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts)），面向 Node.js。它和 Python 绑定的关系，就像 Rust `ipc::Client`（异步）和 `sync::ipc::Client`（同步）的关系——Node.js 天生单线程事件循环，所以 JS 绑定本质上是**异步**的，用 `Promise` + `async/await`。

它在协议上和 Python / Rust 完全一致，但在 API 风格上有两点不同：

1. **QoS 是真正的枚举**。JS 定义了完整的 `QoS { No=0, Processed=1, Realtime=2, RealtimeProcessed=3 }`（[busrt.ts:22-27](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L22-L27)），把 u2-l1 讲的四个等级都暴露出来；而 Python 绑定只让你传裸整数 `qos=0/1`。
2. **消息类型用 `BusOp` 枚举 + 独立方法**。JS 有 `bus.send()` / `bus.publish()` / `bus.subscribe()` 三个方法分别对应 `BusOp.Message` / `BusOp.Publish` / `BusOp.Subscribe`（[busrt.ts:719-885](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L719-L885)）；Python 则统一用 `send(target, Frame(tp=...))`，靠 `tp` 参数区分。

#### 4.4.2 核心流程：基于 PromiseSocket 与 Mutex 的异步模型

JS 绑定的并发骨架：

```text
connect(path)
   ├─ PromiseSocket（基于 node:net Socket）连上后做 3 字节握手
   ├─ process.nextTick(() => this._tReader(this))   // 读循环
   └─ process.nextTick(() => this._tPing(this))      // ping 循环

_send(frame, target)
   ├─ socket_lock（async-mutex）串行化写
   ├─ 拼装 9 字节头，写 socket
   └─ 若 qos & 0b1：OpResult 先 lock() 占住内部 Mutex，登记到 frames.get(frameId)
```

握手（[busrt.ts:600-639](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L600-L639)）和 Python 完全同构：读 3 字节问候、校验魔数与 `readUInt16LE` 版本号、回写、读 1 字节 OK、写 `writeUInt16LE` 名字长度 + 名字、读 OK。读循环 `_tReader`（[busrt.ts:642-693](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L642-L693)）解析 6 字节头，`BusOp.Ack` 兑现等待、业务帧按 `indexOf(0)` 切 sender/topic——和 Python 的 split 逻辑等价。

#### 4.4.3 源码精读

**用 Mutex 实现「等待完成」**（[busrt.ts:102-158](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L102-L158)）。这是 JS 绑定最巧妙、也最值得和 Python 对照的地方。Python 用 `threading.Event` / `asyncio.Event` 表达「等 ACK」，JS 没有 Event，于是用一个 `Mutex` 的「锁住—释放」来模拟：

```typescript
// 发送时（_send 内）：先抢锁，使 Mutex 处于「已锁」状态
if ((frame.qos & 0b1) != 0) {
  await o.lock();                 // acquire => locked
  this.frames.set(frameId, o);
}
// 等待时（_waitCompletedCode）：再 acquire 会阻塞，直到 ACK 释放锁
async _waitCompletedCode() {
  const r = await this.locker?.acquire();   // 阻塞，直到 ACK handler release()
  if (r) r();
  return this.result;
}
// ACK 到达时（_tReader 内）：释放锁，唤醒等待者
o.result = buf[5];
(o as any).release();
```

机制：发送时 `lock()` 抢占 Mutex（占用态），ACK 到达时 `release()` 归还，于是 `_waitCompletedCode` 里的第二次 `acquire()` 才能返回——一次「锁的释放」就代表「一次 ACK 到达」。这是用互斥量当信号量的经典技巧，效果等价于 Python 的 Event。

**RPC call0 与 call**（[busrt.ts:385-449](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L385-L449)）。`call0` 的 header 拼装：

```typescript
request.header = Buffer.concat([
  Buffer.from([RpcOp.Request, 0, 0, 0, 0]),   // 0x01 + 4 字节全零 id
  request.method,
  Buffer.alloc(1)                              // 0x00 分隔符
]);
```

与 Python 的 `RPC_REQUEST_HEADER + b'\x00\x00\x00\x00' + method + b'\x00'`（[rpc.py:73-74](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L73-L74)）**逐字节相同**。`call` 版本把中间 4 字节换成 `callIdBuf.writeUInt32LE(callId)`（[busrt.ts:424-433](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L424-L433)），同样在 `0xffff_ffff` 处回绕（[busrt.ts:419](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L419)）。

**RPC 回复与错误帧**（[busrt.ts:485-510](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L485-L510)）：成功回复 header `[0x11][callId:4]`；错误回复 header `[0x12][callId:4][code:2,有符号小端]`，与 Python（[rpc.py:152-154](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L152-L154)）和 u5-l1 完全一致。注意 JS 在 handler 抛错时用 `writeInt16LE(code)` 写错误码——code 来自 `BusError` 的 `.code` 字段（[busrt.ts:499-500](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L499-L500)）。

#### 4.4.4 代码实践：运行官方 JS 示例

**实践目标**：用 JS 客户端连 Rust 代理，验证 Node.js 与 Python/Rust 三方互通。

**操作步骤**：

1. 启动 Rust 代理：`cargo run --features server --bin busrtd -- -B /tmp/busrt.sock`。
2. 在 `bindings/js/busrt/` 下安装依赖并编译运行 README 的 client 示例（[bindings/js/busrt/README.md:5-45](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/README.md#L5-L45)）：
   ```bash
   npm install && node dist/example.js   # 示例代码取自 README
   ```
   示例会 `subscribe`、`unsubscribe`、再 `send("target", "hello")`。

**需要观察的现象**：JS 客户端 `bus.isConnected()` 返回 `true`，`subscribe` 与 `send` 的 `waitCompleted()` 正常 resolve（不抛 `BusError`）。

**预期结果**：JS 客户端连上 Rust 代理，完成握手与若干次帧收发。若你把 `target` 改成之前 Python 监听的同名客户端，Python 端的 `on_frame` 会收到 JS 发来的消息——证明 JS↔Python 经同一个 Rust 代理互通。

> 待本地验证：Node 环境与编译产物的具体路径需在本地确认；README 示例里 `bus.connect(("localhost", 9924))` 的 TCP 写法应以源码 `connect(path: string)`（[busrt.ts:600](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L600)）为准，传 `host:port` 字符串。

#### 4.4.5 小练习与答案

**练习 1**：JS 绑定的 `QoS` 枚举有 `Realtime = 2` 和 `RealtimeProcessed = 3`，但 Python 绑定的示例里只见过 `qos=0` 和 `qos=1`。Python 端能不能发送实时消息？

**参考答案**：能。Python 的 `flags = frame.type | frame.qos << 6`（[client.py:184](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L184)）对 `qos=2/3` 同样成立，会把实时位打进高 2 位。Python 只是没在示例里演示，但协议层面完全支持；代理侧会按 `realtime` 位即时刷新（u7-l1）。不过 Python 客户端本身不保证实时调度（受 GIL 影响），实时性主要在 Rust 端兑现。

**练习 2**：JS 用 `Mutex` 模拟 Event 来等 ACK（[busrt.ts:113-144](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L113-L144)）。如果同一个 `OpResult` 被两个 `await waitCompleted()` 同时调用，会发生什么？

**参考答案**：两个调用都会去 `acquire()` 同一把 Mutex。第一个拿到后立即 `r()` 释放（因为 ACK 已到、锁已被 release 过），返回 `result`；第二个同样能拿到。由于 Mutex 已被 ACK handler 释放，二者都能读到 `this.result`，不会死锁。这是一种「单次事件、多次 await 都能通过」的语义，与 Event 的「set 之后所有 wait 立即返回」等价。

---

## 5. 综合实践：Python 客户端 × Rust 代理的跨语言闭环

把本讲内容串起来，完成一个完整的跨语言闭环：**用 Python 绑定连接 Rust `busrtd`，执行一次 publish（给主题发消息）和一次 RPC call（调用代理内置方法），并用 Rust 工具从旁验证协议一致。**

### 5.1 准备

1. 启动带核心 RPC 的 Rust 代理（`init_default_core_rpc` 是 `busrtd` 默认行为，见 u5-l3、u8-l1）：
   ```bash
   cargo run --features server --bin busrtd -- -B /tmp/busrt.sock
   ```
2. 安装 Python 同步绑定：`pip3 install busrt msgpack`。

### 5.2 编写跨语言验证脚本

把下面这段保存为 `cross_lang.py`（基于仓库示例 [example_sender.py](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/example_sender.py) 与 [example_rpc_call.py](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/example_rpc_call.py) 改写，标注为「示例代码」）：

```python
# 示例代码：跨语言验证 publish + rpc call
import busrt
import msgpack

bus = busrt.client.Client('/tmp/busrt.sock', 'test.client.python.cross')
bus.connect()

# 1) publish 到主题 cross/lang（qos=1，需要 ACK）
r = bus.send('cross/lang',
             busrt.client.Frame(b'hello from python',
                                tp=busrt.client.OP_PUBLISH, qos=1))
print('publish ack =', hex(r.wait_completed()))   # 预期 0x1

# 2) RPC call 调用代理内置的 .broker 核心 RPC 的 test 方法
rpc = busrt.rpc.Rpc(bus)
result = rpc.call('.broker',
                  busrt.rpc.Request('test', b'')).wait_completed()
print('rpc reply   =', msgpack.loads(result.get_payload(), raw=False))
# .broker 的 test 方法返回 {"ok": true}（见 u5-l3）
```

### 5.3 执行与交叉验证

1. **先开一个 Rust CLI 监听者**（另开终端），订阅所有主题：
   ```bash
   cargo run --features cli --bin busrt -- -s /tmp/busrt.sock listen '#'
   ```
2. **再运行上面的 Python 脚本**。

### 5.4 需要观察的现象与预期结果

| 步骤 | 预期现象 | 说明 |
|------|----------|------|
| Python `publish ack` | 打印 `0x1`（`RESPONSE_OK`） | Python 发出的 publish 帧被 Rust 代理接收并回 ACK |
| Rust CLI `listen` 终端 | 打印 `cross/lang` 主题的 `hello from python` | Rust 客户端收到了 Python 发的 publish，**跨语言 pub/sub 互通** |
| Python `rpc reply` | 打印 `{'ok': True}` | Python 经同一协议调用 Rust 代理内部 RPC 方法并拿到 msgpack 回复，**跨语言 RPC 互通** |

这三步共同证明：Python 客户端、Rust CLI、Rust 代理三方说着**同一种字节语言**。Python 拼出的 9 字节头被 Rust 代理的 `handle_reader` 正确解析；Rust `.broker` 回送的 6 字节头被 Python 的 `_t_reader` 正确解析。

### 5.5 对照 Rust 客户端说明协议一致性

把 Python 的 `send`（[client.py:174-213](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/client.py#L174-L213)）与 Rust `ipc::Client` 的 `send_frame!` 宏（u4-l2）对照，二者产出字节一致：

```text
客户端→代理帧：  [op_id:4 LE][flags:1][len:4 LE][target\0][header?][payload]
flags 字节：    op(低6位) | (qos << 6)(高2位)
```

把 Python 的 `rpc.call` header（[rpc.py:86-87](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/python/busrt/busrt/rpc.py#L86-L87)）与 Rust `prepare_call_payload`（u5-l1）对照：

```text
RPC 请求 header：[0x01][call_id:4 LE][method][0x00]
```

逐字节相同。这就是「跨语言互通」的全部秘密——**协议常量与字节布局在三种语言里严格一致，每种绑定只是用本语言最顺手的方式把同样的字节写进 socket**。

> 待本地验证：上述命令、返回码与 CLI 输出需在本地真实运行确认。若 `pip`/`cargo`/网络环境受限，可退化为「源码阅读型实践」：逐行核对 Python `send` 与 Rust `send_frame!` 拼出的字节是否一致。

## 6. 本讲小结

- BUS/RT 的跨语言互通**不依赖 FFI**，而是因为 Python（同步 `busrt` / 异步 `busrt-async`）、JS（`busrt.ts`）、Dart 等绑定各自**重新实现了同一套二进制线协议**。
- 协议常量（`GREETINGS=0xEB`、`PROTOCOL_VERSION=1`、`OP_*`、`PING_FRAME=9 字节全零`、`RESPONSE_OK=1`）与帧布局（9 字节发送头、6 字节接收头、`flags=op|(qos<<6)`）在 Rust、Python、JS 三处**逐字节一致**，这是互通的字节级保证。
- RPC 层（`0x00/0x01/0x11/0x12`、`id=0` 不回复、`-32xxx` 错误码、`call_id` 在 `0xffff_ffff` 回绕）在三种语言里也是平行实现，对应 u5-l1/u5-l2 的同一协议。
- **同步 vs 异步是并发模型差异，不是协议差异**：Python 同步版用 `threading`，异步版用 `asyncio`，JS 用 Node 事件循环 + `Promise`，但写出相同字节。选择依据是宿主环境（普通线程 / 事件循环 / 实时调度），两者不能混用。
- JS 绑定用 `Mutex` 的「锁住—释放」模拟 Event 来等 ACK（[busrt.ts:113-157](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/bindings/js/busrt/src/busrt.ts#L113-L157)），是三种语言里最独特的实现细节，效果等价于 Python 的 `Event`。
- 调用 Rust 代理的 `.broker` 核心 RPC（如 `test` 方法）是验证跨语言 RPC 互通的最简方式——无需自己写 handler，代理自带。

## 7. 下一步学习建议

本讲是 BUS/RT 学习手册的最后一篇。到此你已经从「项目定位」一路读到「多语言生态」，建议按以下方向继续：

- **动手做一个真实集成**：挑一种你最熟的语言（Python 异步或 JS），写一个小服务同时作为 RPC 服务端和 pub/sub 订阅者，跑在 `busrtd` 上，体验协议一致性带来的「混搭」自由。
- **回看协议源头**：若想更深入理解绑定为「何如此拼字节」，回到 u2-l3（线协议）和 u5-l1（RPC 协议）对照 Rust 实现，会发现每个绑定的每一行都能在 Rust 侧找到对应。
- **关注协议演进风险**：`PROTOCOL_VERSION`（当前为 `1`）是绑定与代理之间的版本契约。若未来 Rust 侧升级协议版本，所有绑定必须同步更新常量与握手逻辑——这是维护多语言生态的核心成本点。
- **探索社区绑定**：Dart 绑定（[github.com/AndreiLosev/busrt_client](https://github.com/AndreiLosev/busrt_client)）由社区维护，可对照本讲的方法自行阅读，验证它是否也严格遵循同一字节协议。
- **性能对比**：用 `busrt` CLI 的 `benchmark`（u8-l2）分别压测 Rust 与 Python 客户端，直观感受「同一协议、不同语言实现」在吞吐与延迟上的差距，理解 README 里那张基准表（[README.md:62-77](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L62-L77)）的来源。
