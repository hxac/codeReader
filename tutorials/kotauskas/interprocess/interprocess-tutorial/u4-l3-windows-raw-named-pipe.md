# Windows 原生 named pipe API

## 1. 本讲目标

u4-l2 让我们看清了「local socket 在 Windows 上是 interprocess 在 named pipe 之上构造的抽象」。当时我们把 `os::windows::named_pipe` 当成一个黑盒：公共层把 `ListenerOptions` 翻译成 `PipeListenerOptions`，丢进去，再冒出一个 `DuplexPipeStream<Bytes>`。

本讲要撬开这个黑盒。读完本讲你应当能够：

- 说清 named pipe 的两个正交维度——**模式**（字节 / 消息）与**方向**（单向 / 双工），以及 interprocess 用 `PipeMode`、`PipeDirection`、`PipeStreamRole` 三个枚举如何刻画它们；
- 解释 `PipeListener` 的**实例机制（instance mechanism）**：为什么监听器不是 Win32 对象、为什么它内部始终「装填一发」、`accept` 到底做了什么；
- 理解 Windows 独有的**「连接即就绪」**行为，以及「不及时 accept 会阻塞新连接」的根因；
- 用消息模式（`Messages`）写一对 server/client，验证消息边界被保留。

本讲只讲**同步** named pipe，异步（Tokio）变体留待 u6。

## 2. 前置知识

### 2.1 named pipe 到底是什么

Windows 的 **named pipe**（命名管道）是一种可按名字（路径形如 `\\.\pipe\名字`）定位的内核对象，支持服务端/客户端模型：服务端创建管道并等待连接，客户端按名字连上。它在概念上更接近 Unix 的 **Unix domain socket**（而非 Unix 的 FIFO 文件）——interprocess 正是因此把它当作 local socket 的 Windows 底层原语，参见模块开头的说明：

[src/os/windows/named_pipe.rs:1-15](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe.rs#L1-L15) — 模块文档明确指出：Unix 的 "named pipe" 与 Windows 完全不是一回事，故 interprocess 把 Unix 版本特称为 FIFO 文件；而 Windows named pipe 的行为更像 Unix domain socket。

> 本系列里「named pipe」一律指 Windows 原生 named pipe，请勿与 u4-l4 的 Unix FIFO 混淆。

### 2.2 「实例」概念

一个 named pipe 名字下可以存在多个**实例（pipe instance）**，每个实例是一个独立的句柄，由 `CreateNamedPipeW` 用同一个名字创建。当多个客户端同时连接时，内核会把它们分发到不同的空闲实例上。实例数量有上限，由创建时的 `max_instances` 参数决定。

### 2.3 承接 u4-l2 的黑盒

u4-l2 已经指出 local socket 的 Windows 后端 `Stream` 内部就是 `DuplexPipeStream<Bytes>`：

[src/os/windows/named_pipe/local_socket/stream.rs:21-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L21-L23) — local socket 包装层把 `StreamImpl` 定义为 `DuplexPipeStream<Bytes>`，即「字节模式 + 双工」的管道流。

本讲就从这行类型别名往下钻：`DuplexPipeStream` 是什么？`Bytes` 这个「模式标记」又是什么？

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/os/windows/named_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe.rs) | 模块根，声明子模块并 `pub use` 导出全部公共类型。 |
| [src/os/windows/named_pipe/enums.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs) | `PipeDirection`、`PipeStreamRole`、`PipeMode` 三个核心枚举及其相互转换。 |
| [src/os/windows/named_pipe/listener/options.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs) | `PipeListenerOptions` 构建器与服务端创建入口。 |
| [src/os/windows/named_pipe/listener/create_instance.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs) | 把选项翻译成 `CreateNamedPipeW` 参数、真正调用系统调用的核心。 |
| [src/os/windows/named_pipe/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs) | `PipeListener` 结构体定义、`accept` 循环、「连接即就绪」处理。 |
| [src/os/windows/named_pipe/listener/incoming.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/incoming.rs) | `Incoming` 无限迭代器，把 `accept` 包成 `Iterator`。 |
| [src/os/windows/named_pipe/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs) | `PipeStream` 结构体与 `DuplexPipeStream` 等类型别名、limbo 行为说明。 |
| [src/os/windows/named_pipe/stream/enums.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs) | `pipe_mode` 模块：把 `PipeMode` 「常量泛型化」为标记类型 `Bytes`/`Messages`/`None`。 |
| [src/os/windows/named_pipe/stream/impl.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs) | `PipeStream` 的方法：`split`/`reunite`/`set_nonblocking` 等。 |
| [src/os/windows/named_pipe/stream/impl/ctor.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs) | 客户端 `connect_by_path` 与连接时的读模式设置。 |
| [src/os/windows/named_pipe/stream/impl/send.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/send.rs) | 消息模式的 `send`、字节模式的 `Write`。 |
| [src/os/windows/named_pipe/stream/impl/recv_msg.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/recv_msg.rs) | 消息模式的 `recv_msg`，逐块读取一条完整消息。 |
| [examples/named_pipe/sync/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/named_pipe/sync/listener.rs) | 字节模式服务端示例。 |
| [examples/named_pipe/sync/stream/msg.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/named_pipe/sync/stream/msg.rs) | 消息模式客户端示例。 |

## 4. 核心概念与源码讲解

### 4.1 模式与方向：named pipe 的两个维度

#### 4.1.1 概念说明

Windows named pipe 有两个相互独立的维度，初学者极易混淆：

1. **模式（mode）——数据是否保留消息边界**
   - **字节模式（byte stream）**：数据是无边界的字节流，连续两次 `send` 的内容可能被对端一次 `read` 全部读出（就像 TCP 字节流）。
   - **消息模式（message stream）**：每次 `send` 是一条**完整消息**，内核记住边界，对端一次 `read`/`recv` 只会取到一条消息（就像 UDP 数据报，但有序且可靠）。

2. **方向（direction）——谁能收、谁能发**
   - 客户端 → 服务端（单向）、服务端 → 客户端（单向）、双工（双向）。

interprocess 用**三个枚举**来刻画：`PipeMode` 描述「模式」，`PipeDirection` 描述「方向」，`PipeStreamRole` 描述「某一端扮演的角色」。

#### 4.1.2 核心流程：模式维度的两个正交设置

named pipe 的「模式」其实在 Win32 层面分两个独立标志：

- `PIPE_TYPE_*`（管道类型）：**写入端**如何把数据写进管道。这影响**所有**写入的数据，无论谁写。
- `PIPE_READMODE_*`（读模式）：**读取端**如何解释数据。一个消息型管道也可以被按字节读取（此时读取端会丢失边界）。

interprocess 把这两个标志合并到一个 `pipe_mode` 位字段里，详见 4.2。先看枚举本身：

[src/os/windows/named_pipe/enums.rs:177-202](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L177-L202) — `PipeMode` 是 `#[repr(u32)]` 枚举，`Bytes = PIPE_TYPE_BYTE`、`Messages = PIPE_TYPE_MESSAGE`。`to_pipe_type()` 取 `PIPE_TYPE_*`，`to_readmode()` 取对应的 `PIPE_READMODE_*`——同一个枚举值能产出两种不同的底层常量，正对应上面两个独立标志。

> 关键：`mode`（管道类型）决定**写**的方式，`recv_mode`（读模式）决定**读**的方式。本地 socket 包装层（u4-l2）恒用 `Bytes`，所以 local socket 永远是字节流；要用消息边界必须直接使用原生 named pipe API。

#### 4.1.3 源码精读：PipeDirection 与 PipeStreamRole

`PipeDirection` 是**绝对**含义——同样的取值对客户端和服务端意思一致：

[src/os/windows/named_pipe/enums.rs:12-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L12-L26) — `ClientToServer = PIPE_ACCESS_INBOUND`（客户→服务）、`ServerToClient = PIPE_ACCESS_OUTBOUND`（服务→客户）、`Duplex = PIPE_ACCESS_DUPLEX`（双向）。注意判别值直接用 Win32 常量，故 `#[repr(u32)]` 可与底层常数 `mem::transmute` 互转（见同文件 [L94-96](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L94-L96)）。

而 `PipeStreamRole` 是**相对**含义——`Recver` 表示「我这一端只收」，到底是哪个方向取决于你是服务端还是客户端：

[src/os/windows/named_pipe/enums.rs:97-112](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L97-L112) — `Recver`/`Sender`/`RecverAndSender`。文档明确点出它**不**与 `PIPE_ACCESS_*` 布局兼容。

两者通过 `direction_as_server()` / `direction_as_client()` 互转：

[src/os/windows/named_pipe/enums.rs:133-139](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L133-L139) — 服务端视角：`Recver → ClientToServer`、`Sender → ServerToClient`、`RecverAndSender → Duplex`。反向的 `client_role()`/`server_role()`（[L46-52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L46-L52)）则把方向转成某一端的角色。

> 记忆口诀：`PipeDirection` 描述「水流方向」，`PipeStreamRole` 描述「我在这头是进水口还是出水口」。

#### 4.1.4 把模式「常量泛型化」：pipe_mode 标记类型

`PipeMode` 是运行期枚举，但 interprocess 希望**在编译期**就确定某条流是字节流还是消息流，从而让 `PipeStream` 只在合适时实现 `send`/`recv_msg`。做法是把 `PipeMode` 编码成一组**标记类型（marker types）**：

[src/os/windows/named_pipe/stream/enums.rs:9-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs#L9-L21) — `pipe_mode` 模块文档说明：它是 `PipeMode` 的「常量泛型化身」，用 `PipeStream` 的两个泛型参数 `Rm`/`Sm` 决定实现哪些 trait。

[src/os/windows/named_pipe/stream/enums.rs:87-96](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/enums.rs#L87-L96) — 由 `present_tag!` 宏生成三个标记：`None`（`MODE = None`，表示该方向不存在）、`Bytes`（`Some(Bytes)`）、`Messages`（`Some(Messages)`）。`PipeModeTag::MODE: Option<PipeMode>` 这个关联常量就是标记与运行期枚举之间的桥。

于是 `PipeStream<Rm, Sm>` 的两个泛型参数分别约束「收」与「发」的能力：

- `PipeStream<Bytes, Bytes>`（即 `DuplexPipeStream<Bytes>`，默认参数可省略）：字节双工流，正是 local socket 用的那个。
- `PipeStream<Messages, Messages>`（即 `DuplexPipeStream<Messages>`）：消息双工流。
- `PipeStream<Messages, None>`（即 `RecvPipeStream<Messages>`）：只收的消息流（split 的收半边）。

类型别名见：

[src/os/windows/named_pipe/stream.rs:62-72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L62-L72) — `DuplexPipeStream<M> = PipeStream<M, M>`，`RecvPipeStream<M> = PipeStream<M, None>`，`SendPipeStream<M> = PipeStream<None, M>`。

`Rm`/`Sm` 的组合还能反推出 `PipeStreamRole`，省去运行期判断：

[src/os/windows/named_pipe/enums.rs:167-174](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L167-L174) — `get_for_rm_sm` 由 `(Rm::MODE, Sm::MODE)` 推出 `RecverAndSender`/`Recver`/`Sender`。

#### 4.1.5 代码实践：读文档理解三枚举的关系

1. **实践目标**：在不运行代码的前提下，用 `client_role()`/`server_role()`/`direction_as_server()` 的文档示例，验证三枚举的对应关系。
2. **操作步骤**：阅读 [src/os/windows/named_pipe/enums.rs:29-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L29-L45) 中 `client_role()` 的 doctest。
3. **需要观察的现象**：`ClientToServer.client_role() == Sender`，而 `ClientToServer.server_role() == Recver`（[L58-65](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/enums.rs#L58-L65)）——同一方向下，客户端与服务端角色相反。
4. **预期结果**：在纸上画出表格，三行（三种方向）×两列（client/server 角色）应完全互补。
5. 运行结果：待本地验证（doctest 在 Windows 上随 `cargo test --doc` 执行）。

#### 4.1.6 小练习与答案

**练习 1**：为什么 interprocess 要把 `PipeMode` 再拆成 `pipe_mode::Bytes/Messages/None` 三个标记类型，而不是直接在 `PipeStream` 上存一个 `PipeMode` 字段？

> **答案**：为了让「能否 `send`/能否 `recv_msg`」成为**编译期**类型约束。例如 `send` 只对 `Sm = Messages` 的流定义（见 4.3），`recv_msg` 只对 `Rm = Messages` 的流定义；如果用运行期字段，调用方就得每次处理「这条流其实是字节流」的错误情形，而类型系统在编译期就排除了这种可能。

**练习 2**：`PipeDirection::Duplex` 对应的客户端角色和服务端角色分别是什么？

> **答案**：都是 `RecverAndSender`（见 `client_role()`/`server_role()` 对 `Duplex` 的分支）。

---

### 4.2 PipeListener 的实例机制与创建

#### 4.2.1 概念说明

`PipeListener` 是 interprocess 的发明，**不**对应任何 Win32 对象。它的职责是「持续地接待客户端连接并产出 `PipeStream`」。理解它的关键是**实例机制**：

- 监听器内部**始终预创建一个实例**（一个已经 `CreateNamedPipeW` 好的句柄）「装在膛里」，等待客户端连入。
- 每次 `accept`：先在「膛里」那个实例上等待连接（`ConnectNamedPipe`），**同时**新建一个实例装回膛里，再把刚连上的那个实例交给调用方。
- 于是监听器自始至终只有一个实例在待命——但一旦 `accept` 完毕，立刻补上一个新的。

这种「单实例循环」与 Unix 的 `listen()` backlog 队列截然不同，是 Windows named pipe 的本质特性。

#### 4.2.2 核心流程：从一个实例到无限接待

```
PipeListener（始终持有 1 个待命实例）
        │
        │ accept()
        ▼
 ┌─────────────────────────────────────────────┐
 │ 1. 锁住 stored_instance                       │
 │ 2. block_on_connect(膛里实例)  ← 阻塞等客户端  │
 │ 3. create_instance() 建新实例                  │
 │ 4. replace：新实例装膛，旧(已连)实例交出        │
 │ 5. 包装成 PipeStream 返回                      │
 └─────────────────────────────────────────────┘
```

创建监听器的入口是 `PipeListenerOptions` 构建器，与 u3-l1 的 `ListenerOptions` 同套路：

[src/os/windows/named_pipe/listener/options.rs:19-75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L19-L75) — `PipeListenerOptions` 字段表。注意 `path` 字段注释：`\\.\pipe\` 前缀**不会**自动补，须调用方提供完整名；`mode` 字段（管道类型）在所有方向下都必填，因为它影响**所有**写入。

[src/os/windows/named_pipe/listener/options.rs:81-95](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L81-L95) — `new()` 的默认值：`mode = Bytes`、`nonblocking = false`、`instance_limit = None`（无上限）、收发缓冲各 512 字节。

消费构建器产出监听器：

[src/os/windows/named_pipe/listener/options.rs:153-177](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L153-L177) — `create::<Rm, Sm>()` 由泛型参数选定流的类型；`create_duplex::<M>()` 是双工简写；`create_recv_only`/`create_send_only` 是单向简写。

#### 4.2.3 源码精读：把选项翻译成 CreateNamedPipeW

真正的系统调用在 `create_instance` 里。这是全模块最关键的函数：

[src/os/windows/named_pipe/listener/create_instance.rs:32-80](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L32-L80) — `create_instance`。

逐段拆解：

- [L39-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L39-L46)：**一致性校验**——若 `recv_mode` 是 `Messages` 但 `mode` 是 `Bytes`，直接报错。原因见 4.1.2：管道类型（`PIPE_TYPE_*`）决定所有写入的方式，一个字节型管道无法承载消息边界，强行按消息读取没有意义。
- [L48-49](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L48-L49)：把方向、读模式分别算出 `open_mode`、`pipe_mode` 两个位字段。
- [L56-63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L56-L63)：`max_instances` 的处理——`None` 映射为 255（Win32 的「无上限」哨兵），而**用户显式设 255 会被拒绝**，因为 255 已被当作哨兵占用。
- [L65-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L65-L79)：`unsafe` 调用 `CreateNamedPipeW`，用 `handle_or_errno()` 把 `INVALID_HANDLE_VALUE` 翻成 `Err`，否则包成 `OwnedHandle`。

`open_mode` 与 `pipe_mode` 两个位字段的拼装：

[src/os/windows/named_pipe/listener/create_instance.rs:82-104](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L82-L104) — `open_mode` 由方向（`role.direction_as_server()`）+ 首实例标志 + 写穿透标志拼成；`pipe_mode` 由**管道类型**（`self.mode.to_pipe_type()`）+ **读模式**（`recv_mode.to_readmode()`）+ 非阻塞 + 拒绝远程客户端拼成。这正是 4.1.2「两个独立标志」在代码里的落地。

#### 4.2.4 源码精读：PipeListener 结构体

[src/os/windows/named_pipe/listener.rs:42-48](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L42-L48) — 结构体三字段一标记：

- `config: PipeListenerOptions<'static>`——保存创建参数，因为**每次 `accept` 都要用它建新实例**（这就是为什么 u4-l2 强调「公共层把 `ListenerOptions` 翻译成 `PipeListenerOptions` 并存起来」）。
- `stored_instance: Mutex<OwnedHandle>`——「膛里」那个待命实例。
- `nonblocking: AtomicBool`——非阻塞开关。

注意 `config` 用 `'static` 生命周期：构建器把借用的名字 `to_owned()` 成拥有的，见 [options.rs:99-122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L99-L122)。

`from_handle_and_options` 是「用已建好的首实例 + 配置」构造监听器：

[src/os/windows/named_pipe/listener.rs:106-121](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L106-L121) — `create()` 在 [create_instance.rs:19-28](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L19-L28) 的 `_create` 里先 `to_owned` 配置、再 `create_instance(first=true, ...)` 建首实例，二者一起喂给 `from_handle_and_options`。

#### 4.2.5 代码实践：观察 create_instance 的两段一致性校验

1. **实践目标**：亲手触发 4.2.3 提到的两个错误分支，确认它们的存在。
2. **操作步骤**：
   - 写一个 `PipeListenerOptions::new().path(...).create_duplex::<pipe_mode::Messages>()`（**不**设 `.mode(PipeMode::Messages)`），调用 `.create()`。
   - 再写一个 `.instance_limit(Some(NonZeroU8::new(255).unwrap()))`，调用 `.create()`。
3. **需要观察的现象**：第一个应返回 `InvalidInput`，消息含「byte type but receives messages」；第二个返回 `InvalidInput`，消息含「255 being a reserved value」。
4. **预期结果**：两条错误信息分别对应 [create_instance.rs:42-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L42-L45) 与 [L57-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L57-L60)。
5. 运行结果：待本地验证（仅 Windows 可编译运行）。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `PipeListener` 要把 `PipeListenerOptions` 整个存下来，而不是只存必要字段？

> **答案**：因为每次 `accept` 都要用**完全相同的参数**调用 `CreateNamedPipeW` 建一个新实例（[listener.rs:123-125](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L123-L125) 的 `create_instance` 委托给 `self.config`）。存整张选项表能保证每个新实例与首实例行为一致（缓冲大小、安全描述符、写穿透等）。

**练习 2**：用户把 `instance_limit` 设成 `Some(1)` 会发生什么？

> **答案**：`max_instances = 1`，意味着整个管道只允许存在 1 个实例。而监听器自身就始终占用 1 个待命实例，于是 `accept` 里试图建第二个实例时会失败——这正是 [options.rs:43-47](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L43-L47) 文档所说的「设为 1 会破坏 `.accept()`」。

---

### 4.3 accept 循环与「连接即就绪」

#### 4.3.1 概念说明

Windows named pipe 有一个 Unix 没有的怪行为：**客户端「连接」一个实例时，内核直接把该实例置为已连接状态**，不需要服务端 `accept` 参与。这与 socket 的 `connect`+`accept` 握手模型不同——interprocess 把这种行为称为「连接即就绪（connecting puts the pipe into a connected state）」。

由此带来一个必须处理的后果：**如果一个客户端连上某个实例后断开，而服务端始终没对这个实例调用 `accept`，这个实例就成了「到货即死（dead-on-arrival）」的连接，会一直占着，阻止新客户端连入**——直到 `accept` 把它清掉。

#### 4.3.2 核心流程：accept 的四步

`accept` 的完整逻辑：

[src/os/windows/named_pipe/listener.rs:52-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L52-L79) — 方法上方的文档（[L57-62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L57-L62)）正是「连接即就绪 + 不及时 accept 会阻塞」的官方说明。

方法体四步：

1. **加锁** `stored_instance`（[L64-65](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L64-L65)）——互斥保证同一时刻只有一个 `accept` 在操作膛里实例。
2. **在膛里实例上等待连接**：`block_on_connect_clearing_empty_conns`（[L70](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L70)）。
3. **建新实例装膛**：`create_instance`（[L72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L72)）。
4. **交换**：`replace` 把新实例放进膛、把已连的旧实例取出（[L73](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L73)），随后包装成 `PipeStream::new_server` 返回（[L76-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L76-L78)）。

#### 4.3.3 源码精读：连接等待与「死连接」清理

`block_on_connect_clearing_empty_conns` 是处理「连接即就绪」的核心：

[src/os/windows/named_pipe/listener.rs:154-161](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L154-L161) — 一个循环：调用 `block_on_connect` 等待客户端；若返回 `ERROR_NO_DATA`（实例连上了一个**已断开**的客户端，即「死连接」），就 `disconnect_if_connected` 清掉它再重试；其余结果直接返回。

[src/os/windows/named_pipe/listener.rs:163-167](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L163-L167) — `block_on_connect` 就是 `unsafe` 调 `ConnectNamedPipe(handle, null)`（同步阻塞，无 OVERLAPPED），用 `true_val_or_errno` 把「返回 0」翻成 errno。

[src/os/windows/named_pipe/listener.rs:169-177](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L169-L177) — `thunk_accept_error` 把两个特殊 errno 翻译掉：
- `ERROR_PIPE_CONNECTED`（客户端在 `ConnectNamedPipe` 之前就**已经**连上了——正是「连接即就绪」的直接证据）→ 视为成功 `Ok(())`。
- `ERROR_PIPE_LISTENING`（非阻塞下还没有客户端）→ 翻成 `WouldBlock`。

[src/os/windows/named_pipe/listener.rs:179-183](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L179-L183) — `disconnect_if_connected` 调 `DisconnectNamedPipe` 重置实例，对「本来就没连」的 `ERROR_PIPE_NOT_CONNECTED` 视作成功。

> 至此「不及时 accept 会阻塞新连接」的根因清楚了：监听器只持有 1 个待命实例；若它被一个 dead-on-arrival 连接占住，`accept` 必须先 `disconnect` 清理才能继续；若服务端长期不 `accept`，这唯一的实例就一直被占，新客户端无实例可连。

#### 4.3.4 源码精读：incoming 迭代器

[src/os/windows/named_pipe/listener/incoming.rs:6-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/incoming.rs#L6-L23) — `Incoming` 是个包装了 `&PipeListener` 的无限迭代器，`next()` 每次返回 `Some(self.0.accept())`，并实现了 `FusedIterator`。它让服务端主循环写成 `for conn in listener.incoming() { ... }`，与 u3-l1 的 `Incoming` 模式一致。

字节模式服务端示例就用了这个迭代器：

[examples/named_pipe/sync/listener.rs:29-52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/named_pipe/sync/listener.rs#L29-L52) — 主循环：`incoming().filter_map(...).map(BufReader::new)`，对每条连接先 `read_line` 再 `get_mut().write_all` 回写。注释（[L33-38](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/named_pipe/sync/listener.rs#L33-L38)）解释了为何要先收后发：单线程下同时收发会因缓冲满而死锁（与 u1-l4 同理）。

注意示例创建监听器用的是字节模式：

[examples/named_pipe/sync/listener.rs:19-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/named_pipe/sync/listener.rs#L19-L21) — `create_duplex::<pipe_mode::Bytes>()`，未设 `.mode(...)`（默认 `Bytes`）。这正是 4.1 强调的：要用消息模式必须显式 `.mode(PipeMode::Messages)` + `create_duplex::<pipe_mode::Messages>()`。

#### 4.3.5 代码实践：跟踪一次 accept 的死连接清理

1. **实践目标**：理解 `block_on_connect_clearing_empty_conns` 为何需要循环。
2. **操作步骤**：阅读 [listener.rs:154-183](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L154-L183)，在纸上模拟这个场景：客户端 A 连上实例 X 后立刻断开（服务端尚未 `accept`），客户端 B 随后连上。
3. **需要观察的现象**：服务端首次 `accept` 时，`block_on_connect` 在 X 上返回 `ERROR_NO_DATA`（A 已断），进入 `disconnect_if_connected` 清掉 A，循环再来一次 `block_on_connect`，此时 B 连上，返回成功。
4. **预期结果**：`accept` 最终把 X（承载 B 的连接）交给调用方，并在膛里装上新建的实例 Y。
5. 运行结果：待本地验证（源码阅读型实践，无需运行）。

#### 4.3.6 小练习与答案

**练习 1**：假设服务端在 `accept` 中卡住等待时，一个客户端连上又立刻断开。`block_on_connect` 返回什么 errno？后续如何处理？

> **答案**：返回 `ERROR_NO_DATA`（[listener.rs:157](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L157)）。`block_on_connect_clearing_empty_conns` 捕获它，调用 `disconnect_if_connected` 重置实例，然后循环重试 `block_on_connect` 等待下一个客户端。

**练习 2**：为什么 `ERROR_PIPE_CONNECTED` 被当作成功而不是错误？

> **答案**：因为它意味着「客户端在 `ConnectNamedPipe` 被调用之前就已经连上了」——这正是「连接即就绪」行为。从服务端视角，目标（实例已与某客户端连接）已经达成，所以视为成功（[listener.rs:170-171](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L170-L171)）。

---

### 4.4 PipeStream：消息模式的收发

#### 4.4.1 概念说明

`PipeStream<Rm, Sm>` 是 named pipe 连接的用户面类型，由监听器 `accept` 产出（服务端）或 `connect_by_path` 创建（客户端）。它的两个能力维度由 `Rm`/`Sm` 决定：

- `Sm = Messages` → 有 `send(&[u8])` 方法，发送一条完整消息；
- `Rm = Messages` → 实现 `recvmsg::RecvMsg`，可用 `recv_msg` 接收一条完整消息；
- `Sm = Bytes` → 实现标准库 `Write`；
- `Rm = Bytes` → 实现标准库 `Read`。

本节聚焦**消息模式**，因为它最能体现 named pipe 区别于 local socket（字节流）的能力：**保留消息边界**。

#### 4.4.2 核心流程：客户端连接如何切到消息读模式

客户端用 `CreateFile` 打开管道时，读模式默认是**字节**。要按消息读，连接成功后必须显式把读模式设成 `PIPE_READMODE_MESSAGE`。interprocess 在 `RawPipeStream::connect` 里自动完成：

[src/os/windows/named_pipe/stream/impl/ctor.rs:25-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L25-L53) — 连接逻辑：先调 `connect_without_waiting` 真正连上（[L31-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L31-L32)），再处理 `ConnectWaitMode`（[L33-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L33-L42)，与 u3-l2 的三态等待模式衔接）；**最关键的是 [L44-51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L44-L51)**：若 `recv == Some(Messages)`，调 `set_np_handle_state(PIPE_READMODE_MESSAGE)` 把读模式改成消息。

对外入口：

[src/os/windows/named_pipe/stream/impl/ctor.rs:80-86](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L80-L86) — `connect_by_path` 用默认的 `ConnectWaitMode::Unbounded`。注意文档（[L81-82](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L81-L82)）：`\\<hostname>\pipe\` 前缀同样不自动补。

#### 4.4.3 源码精读：send 与 recv_msg

`send` 只对 `Sm = Messages` 定义：

[src/os/windows/named_pipe/stream/impl/send.rs:49-54](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/send.rs#L49-L54) — `send(&self, buf)` 委托 `RawPipeStream::send`，后者调 `c_wrappers::write_exsync` 写入，并在成功时 `mark_dirty`（标记需要 flush，关系到 drop 时的 limbo，见 stream.rs 的 limbo 说明）。返回值是实际写入字节数（消息模式下通常等于 `buf.len()`）。

对比字节模式：[send.rs:56-67](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/send.rs#L56-L67) 为 `PipeStream<Rm, Bytes>` 实现标准库 `Write`，背后走同一条 `send` 路径——差异只在「是否有边界」由管道类型决定。

`recv_msg` 只对 `Rm = Messages` 定义，实现稍复杂，因为它要处理「一条消息比缓冲还大」的情形（内核返回 `ERROR_MORE_DATA`）：

[src/os/windows/named_pipe/stream/impl/recv_msg.rs:36-97](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/recv_msg.rs#L36-L97) — 核心循环：

- 先把缓冲的 fill 置 0、`has_msg` 置 false（[L37-38](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/recv_msg.rs#L37-L38)）。
- 循环读取，遇到 `ERROR_MORE_DATA`（[L71-75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/recv_msg.rs#L71-L75)）说明当前消息还没读完，标记 `partial=true`、继续；缓冲不够则 `grow`，grow 失败返回 `RecvResult::QuotaExceeded` 并丢弃消息剩余部分（[L54-63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/recv_msg.rs#L54-L63)）。
- 对端断开（`BrokenPipe`）返回 `RecvResult::EndOfStream`（[L76-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/recv_msg.rs#L76-L79)）。
- 一条消息读完后置 `has_msg = true`，返回 `Fit`（未扩容）或 `Spilled`（扩容过）。

对外通过 `recvmsg` crate 的 `RecvMsg` trait 暴露：

[src/os/windows/named_pipe/stream/impl/recv_msg.rs:111-134](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/recv_msg.rs#L111-L134) — 为 `PipeStream<Messages, Sm>` 及其引用实现 `RecvMsg`，`recv_msg(buf, None)` 返回 `io::Result<RecvResult>`。`RecvResult` 是 `recvmsg` crate 提供的枚举（`Fit`/`Spilled`/`QuotaExceeded`/`EndOfStream`）。

消息模式客户端示例展示了完整用法：

[examples/named_pipe/sync/stream/msg.rs:14-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/named_pipe/sync/stream/msg.rs#L14-L43) — 用 `MsgBuf::from(Vec::with_capacity(128))` 做缓冲，`DuplexPipeStream::<pipe_mode::Messages>::connect_by_path(name)` 连接，`conn.send(MESSAGE)` 发一条消息，`conn.recv_msg(&mut buffer, None)` 收一条消息，最后 `String::from_utf8_lossy(buffer.filled_part())` 转字符串打印。

#### 4.4.4 源码精读：PipeStream 的结构、split 与 limbo

[src/os/windows/named_pipe/stream.rs:57-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L57-L60) — `PipeStream` 只有两个字段：`raw: MaybeArc<RawPipeStream>` 与 `_phantom`。`MaybeArc` 让未拆分流零开销、拆分后才升级为 `Arc`（详见 u8-l2）。

[src/os/windows/named_pipe/stream.rs:24-44](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L24-L44) — 文档说明 interprocess 在 Win32 之上额外做的事：把 `ERROR_PIPE_NOT_CONNECTED`/`BrokenPipe` 翻译成 EOF；以及 drop 时的 **limbo**（limbo/linger_pool 是 u8-l1 的主题，这里只需知道：发过数据但没 flush 的流 drop 时不会立刻关闭，而是交给后台线程先 `FlushFileBuffers` 再关，避免对端收到截断数据）。

`split`/`reunite` 与 u3-l3 同构，按值消费，故不走 `dispatch!`：

[src/os/windows/named_pipe/stream/impl.rs:31-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L31-L53) — `split` 用 `refclone` 复制底层引用、`reunite` 用 `MaybeArc::ptr_eq` 判同源，失败归还两半。

#### 4.4.5 代码实践：消息模式的两条消息（见第 5 节综合实践的前置）

详见第 5 节。这里先给一个最小对照：把 4.4.3 的客户端示例改造成「连发两条消息」，观察服务端必须 `recv_msg` **两次**才能各取一条——这正是字节模式做不到的边界保留。

#### 4.4.6 小练习与答案

**练习 1**：消息模式下，服务端连续调用两次 `send(b"AAA")` 与 `send(b"BBB")`，客户端缓冲足够大时，分别 `recv_msg` 两次会得到什么？

> **答案**：第一次得到 `"AAA"`（`RecvResult::Fit`），第二次得到 `"BBB"`（`Fit`）。两次发送的边界被内核保留，不会合并成 `"AAABBB"`。若改成字节模式 + `read`，则可能一次读到全部 6 字节。

**练习 2**：为什么客户端连接消息管道后，代码里要额外调一次 `set_np_handle_state(PIPE_READMODE_MESSAGE)`，而服务端监听器创建时不需要？

> **答案**：服务端在 `create_instance` 的 `pipe_mode` 位字段里就同时设了 `PIPE_TYPE_MESSAGE` 和 `PIPE_READMODE_MESSAGE`（见 4.2.3 [create_instance.rs:93-104](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L93-L104)）。客户端用 `CreateFile` 打开管道，默认读模式是字节，所以连接后必须补设（[ctor.rs:44-51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L44-L51)）。

---

## 5. 综合实践

**任务**：用原生 named pipe API 写一对**消息模式**的 server/client。服务端连发两条带边界的消息，客户端验证能**分两次**各收到一条完整消息——这是字节模式（local socket）做不到的。

### 5.1 服务端（示例代码，基于 examples/named_pipe/sync/listener.rs 改造）

```rust
// 示例代码：消息模式 named pipe 服务端
#[cfg(windows)]
fn main() -> std::io::Result<()> {
    use interprocess::os::windows::named_pipe::{pipe_mode, PipeListenerOptions, PipeMode};
    use std::path::Path;

    let pipe_name = r"\\.\pipe\MsgExample";

    // 关键：mode 必须显式设为 Messages，泛型参数也要是 pipe_mode::Messages
    let listener = PipeListenerOptions::new()
        .path(Path::new(pipe_name))
        .mode(PipeMode::Messages)
        .create_duplex::<pipe_mode::Messages>()?;

    eprintln!("Server running at {pipe_name}");

    for conn in listener
        .incoming()
        .filter_map(|c| c.map_err(|e| eprintln!("accept failed: {e}")).ok())
    {
        // 连发两条独立消息
        let n1 = conn.send(b"first")?;
        let n2 = conn.send(b"second")?;
        assert_eq!(n1, 5);
        assert_eq!(n2, 6);
        drop(conn); // drop 触发 limbo 刷新，确保对端收到完整数据
    }
    Ok(())
}

#[cfg(not(windows))]
fn main() {
    eprintln!("This example is not available on platforms other than Windows.");
}
```

### 5.2 客户端（示例代码，基于 examples/named_pipe/sync/stream/msg.rs 改造）

```rust
// 示例代码：消息模式 named pipe 客户端
#[cfg(windows)]
fn main() -> std::io::Result<()> {
    use {interprocess::os::windows::named_pipe::*, recvmsg::prelude::*};

    let name = r"\\.\pipe\MsgExample";
    let mut buf = MsgBuf::from(Vec::with_capacity(128));

    // 消息模式双工流；连接时 interprocess 会自动把读模式设为 PIPE_READMODE_MESSAGE
    let mut conn = DuplexPipeStream::<pipe_mode::Messages>::connect_by_path(name)?;

    // 第一次接收：应恰好拿到 "first"
    conn.recv_msg(&mut buf, None)?;
    let m1 = String::from_utf8_lossy(buf.filled_part());
    println!("message 1 = {m1}");

    // 第二次接收：应恰好拿到 "second"，而非 "firstsecond"
    conn.recv_msg(&mut buf, None)?;
    let m2 = String::from_utf8_lossy(buf.filled_part());
    println!("message 2 = {m2}");

    drop(conn);
    Ok(())
}

#[cfg(not(windows))]
fn main() {
    eprintln!("This example is not available on platforms other than Windows.");
}
```

### 5.3 操作步骤

1. 在仓库 `examples/` 下仿照现有示例新增两个 `[[example]]`（参考 [Cargo.toml:133-140](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L133-L140) 的 `named_pipe_sync_server` / `named_pipe_sync_client_msg` 命名风格），或直接修改现有示例文件。
2. 在 **Windows** 上，开两个终端：先 `cargo run --example <server>`，再 `cargo run --example <client>`。

### 5.4 需要观察的现象与预期结果

- 客户端两次 `recv_msg` 分别打印 `message 1 = first`、`message 2 = second`，**两条消息边界分明**。
- 对照实验：把两端都改成 `pipe_mode::Bytes` + `mode(PipeMode::Bytes)`，服务端用 `write_all` 发两段、客户端用 `read` 收，则客户端很可能**一次** `read` 就拿到 `firstsecond`——字节流抹掉了边界。这正是消息模式存在的意义。

### 5.5 待本地验证

上述示例仅在 Windows 上可编译运行（仓库当前运行环境为 Linux，故标注「待本地验证」）。`recvmsg` 依赖已存在于 [Cargo.toml:63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L63)，无需额外添加。

## 6. 本讲小结

- named pipe 有两个正交维度：**模式**（`PipeMode` 字节/消息，且「管道类型」与「读模式」是两个独立 Win32 标志）与**方向**（`PipeDirection` 绝对 / `PipeStreamRole` 相对）。
- `PipeMode` 被「常量泛型化」为 `pipe_mode::{Bytes, Messages, None}` 标记类型，充当 `PipeStream<Rm, Sm>` 的泛型参数，让 `send`/`recv_msg` 成为编译期能力。
- `PipeListener` 是 interprocess 的发明，不对应 Win32 对象；它内部始终「装填一发」待命实例，每次 `accept` 在该实例上等连接、再补建一个新实例。
- Windows named pipe **连接即就绪**：客户端连接会直接把实例置为已连接；若客户端连后断开而服务端不 `accept`，实例变成 dead-on-arrival，会阻塞新连接——`accept` 用 `block_on_connect_clearing_empty_conns` 清理这种情况。
- 消息模式由 `CreateNamedPipeW` 的 `PIPE_TYPE_MESSAGE`+`PIPE_READMODE_MESSAGE` 开启；客户端连接后需补设读模式；`send` 发一条、`recv_msg` 收一条，边界由内核保留。

## 7. 下一步学习建议

- **u5（Unnamed Pipes）**：把 Windows named pipe 的「实例/连接」模型与匿名管道对照，理解两者的句柄传递差异。
- **u6-l1/l2（Tokio 集成）**：本讲的同步 `accept` 会**阻塞线程**；Tokio 版用 `tokio::net::windows::named_pipe` 把等待变成事件驱动，可对照 `src/os/windows/named_pipe/tokio/` 阅读。
- **u8-l1（linger_pool）**：本讲多次提到的 drop 时 limbo 刷新机制，其后台线程池实现是 Windows named pipe 最精巧的内部之一，建议接着读 `src/os/windows/linger_pool.rs`。
- **u8-l2（maybe_arc）**：`PipeStream` 的 `MaybeArc<RawPipeStream>` 如何让未拆分流零开销、拆分后升级为 `Arc`，是理解 `split`/`reunite` 性能的关键。
- **u9-l1（FFI 封装层）**：本讲引用的 `CreateNamedPipeW`、`ConnectNamedPipe`、`c_wrappers::*` 都属 unsafe FFI 层，u9-l1 会系统讲解 `OrErrno`/`handle_or_errno` 等错误转换工具。
