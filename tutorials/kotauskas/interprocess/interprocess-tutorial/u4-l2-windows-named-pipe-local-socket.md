# Windows 后端：named pipe local socket

## 1. 本讲目标

本讲把上一讲（u4-l1，Unix 后端）打开的「壳/芯」视角，平移到 Windows 平台。学完后你应当能够：

- 说清 Windows 上 local socket 是由什么底层原语实现的，以及「壳」与「芯」分别是哪个类型。
- 画出 `create_sync()` 在 Windows 上的完整派发链，指出哪些类型是公共枚举、哪些是后端实现。
- 逐字段说明 `ListenerOptions` 是如何被翻译成 `PipeListenerOptions` 的（这正是本讲的实践任务）。
- 解释 Windows 后端为什么用 `AtomicEnum<ListenerNonblockingMode>` 而不是 `AtomicBool`，以及 `accept` 出来的流为什么需要「事后调整」非阻塞模式。
- 看懂 `Stream` 包装器如何把连接、超时、`split`/`reunite` 代理到底层 `DuplexPipeStream`。

## 2. 前置知识

本讲默认你已经读过以下讲义，相关结论不再重复，只做承接：

- **u2-l3（impmod 与平台后端注入）**：建立了「公共层为壳、平台后端为芯」的全库分层模型，`impmod!` 把后端类型注入公共模块。
- **u3-l1（ListenerOptions 与服务端创建）**：`ListenerOptions` 构建器的位标志字段、`ListenerNonblockingMode` 四态、`create_sync_as` 派发链。
- **u4-l1（Unix 后端）**：Unix 后端的 `from_options` 如何用 `ReclaimGuard`、`mode`、`try_overwrite`；以及「`create_sync_as`/`connect_sync_as` 在同一条派发链上被调用两次」这一关键事实。

这里补三个 Windows 专属术语，后续不再展开：

- **named pipe（命名管道）**：Windows 原生的 IPC 原语，用一个形如 `\\.\pipe\名字` 的字符串在内核命名空间里登记，客户端按名字连接。注意它和 Unix 的 FIFO 是完全不同的东西（见 u1-l1 的术语警告）。
- **pipe instance（管道实例）**：同一个管道名字可以挂多个实例，每个实例是一条独立的连接通道。`PipeListener` 内部始终预先创建好一个「待命实例」，`accept` 时把它交给客户端，并立刻再建一个新实例待命。
- **ConnectNamedPipe**：Windows 让服务端「等待客户端连进来」的系统调用。与 BSD socket 的 `accept` 语义不同——客户端一旦连上，管道实例就立即进入「已连接」态，若服务端没及时 `accept`，这条实例会变成死连接挡住后续客户端。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/os/windows/named_pipe/local_socket/listener.rs` | Windows 后端的 `Listener` 包装器：实现 `traits::Listener`，桥接 `ListenerOptions` 与 `PipeListenerOptions`，并用 `AtomicEnum` 存非阻塞模式。 |
| `src/os/windows/named_pipe/local_socket/stream.rs` | Windows 后端的 `Stream`/`RecvHalf`/`SendHalf` 包装器：实现 `traits::Stream`，代理连接、读写、拆分重聚。 |
| `src/os/windows/local_socket/dispatch_sync.rs` | 同步派发入口：把公共 `ListenerOptions`/`ConnectOptions` 路由到后端 `np_impl`，并把后端类型包回公共枚举。 |
| `src/os/windows/named_pipe.rs`（模块声明） | 在 `named_pipe` 模块下声明 `pub mod local_socket`，并 re-export `listener`、`stream` 子模块。 |
| `src/os/windows/named_pipe/listener.rs` | 底层 `PipeListener` 的真正实现（`ConnectNamedPipe`、`accept` 循环、实例管理），是「芯的芯」。 |
| `src/os/windows/named_pipe/listener/options.rs` | `PipeListenerOptions` 构建器，Windows 原生 named pipe 的全部可配置项。 |
| `src/atomic_enum.rs` | `AtomicEnum<E>` / `ReprU8`：把 `#[repr(u8)]` 枚举塞进 `AtomicU8` 的工具。 |
| `src/local_socket/listener/trait.rs` | `traits::Listener` 契约与 `ListenerNonblockingMode` 四态定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 Windows 后端的定位与派发入口 dispatch_sync

#### 4.1.1 概念说明

在 Windows 上，local socket **不是**操作系统原语，而是 interprocess 在 Windows 原生 named pipe 之上构造的抽象。这正对应 u2-l1 讲过的「local socket 是抽象而非 OS 原语」。

因此 Windows 后端承担两层职责：

1. **壳**：公共的 `interprocess::local_socket::Listener` / `Stream` 枚举（由 `mkenum`/`dispatch!` 生成，见 u2-l2）。它们不干活，只把方法调用转发给当前平台后端。
2. **芯**：`os::windows::named_pipe::local_socket` 模块里的 `Listener` / `Stream` 结构体。它们才是真正实现 `traits::Listener` / `traits::Stream`、把调用翻译成 named pipe 操作的地方。

把后端「芯」注册成 named pipe 的一个子模块，是在 `named_pipe.rs` 里完成的：

- [named_pipe.rs:29-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe.rs#L29-L32) —— 声明 `pub mod local_socket` 并 re-export `listener`、`stream`，这就是后端类型对外（对公共层）的可见出口。

而把公共壳与后端芯连接起来的「派发入口」，是极薄的 `dispatch_sync.rs`。

#### 4.1.2 核心流程

承接 u4-l1 揭示的派发链，Windows 上的服务端创建流程是：

```
ListenerOptions::create_sync()                         // 公共构建器消费入口
  └─ create_sync_as::<公共 Listener 枚举>()            // L::from_options(self)
       └─ 公共 Listener::from_options                  // mkenum 生成的 match，转发到后端
            └─ dispatch_sync::listen(options)          // 本讲的派发入口
                 └─ options.create_sync_as::<np_impl::Listener>()  // 第二次调用，这次 Self 是后端
                      └─ np_impl::Listener::from_options(options)  // 真正落地系统调用
                 └─ .map(Listener::from)               // 把后端芯包回公共壳
```

注意 `create_sync_as` 在这条链上出现两次（u4-l1 已强调过这个关键事实）：第一次的泛型 `L` 是公共 `Listener` 枚举，第二次是后端 `np_impl::Listener`。`dispatch_sync::listen` 正是这两次调用之间的「中转站」。

#### 4.1.3 源码精读

整个派发入口只有两个函数，非常薄：

- [dispatch_sync.rs:7-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L7-L10) —— `listen`：调用 `options.create_sync_as::<np_impl::Listener>()` 让后端真正建监听器，再用 `Listener::from`（`mkenum` 生成的 `From` 实现，见 u2-l2）把后端芯包回公共壳。
- [dispatch_sync.rs:11-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L11-L14) —— `connect`：客户端侧对称的入口，调 `np_impl::Stream::from_options` 再包回公共 `Stream`。

这里的 `np_impl` 是第 2 行的别名导入：

- [dispatch_sync.rs:1-5](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L1-L5) —— `use super::super::named_pipe::local_socket as np_impl;`，即后端芯模块的别名。这与 u2-l3 里 unnamed pipe 用 `impmod!` 注入别名是同一种思路，只是这里手写 `use`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 Windows 后端的派发链确实「绕一圈」回到后端。

**操作步骤**：

1. 打开 [dispatch_sync.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs)。
2. 跟着 `listen` 的调用，跳到 `ListenerOptions::create_sync_as`（[listener/options.rs:206-207](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L206-L207)），确认它就是 `L::from_options(self)`。
3. 注意 `create_sync_as` 的泛型 `L` 在这里被实例化为后端 `np_impl::Listener`，于是跳到后端的 `from_options`（下一节 4.2 精读）。

**需要观察的现象**：从公共 `create_sync()` 到后端 `from_options`，中间没有任何直接系统调用，全部是「转发 + 别名 + 包回壳」。

**预期结果**：你能画出一张只有「转发」没有「干活」的派发链，真正干活（`CreateNamedPipe`）发生在后端 `from_options` → `PipeListenerOptions::create` 之内。

> 待本地验证：在 Windows 机器上用 `cargo doc` 或 IDE 的「跳转到定义」逐步跟随，可以直观看到这条链。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dispatch_sync::listen` 末尾要 `.map(Listener::from)`，而后端 `from_options` 已经返回了后端 `Listener`？

**参考答案**：因为 `create_sync_as::<np_impl::Listener>()` 返回的是后端芯类型 `np_impl::Listener`，而公共 API（`create_sync` 的签名）承诺返回公共壳枚举 `local_socket::Listener`。`Listener::from`（由 `mkenum` 生成）负责这次「芯→壳」的包装。

**练习 2**：对比 u4-l1 的 Unix 后端，Unix 的派发入口在哪个文件？两者结构是否对称？

**参考答案**：Unix 的派发入口在 `src/os/unix/local_socket/dispatch_sync.rs`，结构与 Windows 完全对称：都是 `listen`/`connect` 两个函数，都先 `create_sync_as::<后端>()` 再 `.map(公共枚举::from)`。差别仅在 `np_impl` 换成 `uds_impl` 之类的后端别名。

---

### 4.2 Listener 包装器：from_options 的选项映射

#### 4.2.1 概念说明

Windows 后端的 `Listener` 是一个**包装器（wrapper）**：它包着一个底层 `PipeListener`，外加一个记录非阻塞模式的原子字段。

- [listener.rs:20-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L20-L24) —— `Listener` 结构体，两个字段：`listener: PipeListener<Bytes, Bytes>` 和 `nonblocking: AtomicEnum<ListenerNonblockingMode>`。

类型别名 `ListenerImpl = PipeListener<Bytes, Bytes>`（[listener.rs:17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L17)）透露一个关键决定：local socket 选用了 `pipe_mode::Bytes`（字节流模式），并且收发都用 `Bytes`，即 **duplex（双工）**管道。这呼应 u2-l1 的结论——interprocess 绝不插入消息分帧，字节流完全透明。

这个包装器的核心使命，就是把跨平台的 `ListenerOptions`（u3-l1 讲过的位标志构建器）翻译成 Windows 原生的 `PipeListenerOptions`。

#### 4.2.2 核心流程

`from_options` 的执行过程可以分成四步：

```
1. 从 ListenerOptions 位标志里读出两个 bool：
     nb_accept  = get_nonblocking_accept()
     nb_stream  = get_nonblocking_stream()
   合成四态枚举：ListenerNonblockingMode::from_bool(nb_accept, nb_stream)

2. 构造一个空的 PipeListenerOptions::new()

3. 只映射三个字段：
     path                ← NameInner::NamedPipe(path) 解出的路径
     nonblocking         ← nb_accept（注意：只取 accept 维度！）
     security_descriptor ← options.security_descriptor（Windows 专有）

4. impl_options.create()  →  得到 PipeListener
   包成 Self { listener, nonblocking: AtomicEnum::new(四态) }
```

注意第 3 步的精髓：`PipeListenerOptions.nonblocking` 只能表达一个布尔（accept 维度），而 local socket 的非阻塞是四态。所以 accept 维度直接给底层，stream 维度则**先存进 `AtomicEnum`**，等每次 `accept` 出新流时再施加（详见 4.3）。

#### 4.2.3 源码精读

- [listener.rs:30-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L30-L42) —— `from_options` 全文。逐行：
  - 第 31-33 行读两个 bool 并合成四态。
  - 第 35 行 `PipeListenerOptions::new()` 取默认值（[listener/options.rs:81-95](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L81-L95) 可见默认 `mode = Bytes`、缓冲区 512 等）。
  - 第 36 行用 `let NameInner::NamedPipe(path) = options.name.0;` 解构名字——这同时起到断言作用：Windows 上 `Name` 内部必然是 `NamedPipe` 变体（见 u2-l4 的 `NameInner` 平台变体）。
  - 第 37-39 行只设置了 `path`、`nonblocking = nb_accept`、`security_descriptor` 三个字段，其余（`instance_limit`、`write_through`、`accept_remote`、缓冲区大小、`wait_timeout`、`inheritable`）全部用默认值。
  - 第 41 行 `impl_options.create()` 真正发起 `CreateNamedPipe`（见 [listener/options.rs:153-157](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L153-L157)），并把四态存进 `AtomicEnum`。

对照 Unix 后端 [uds_local_socket/listener.rs:32-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L32-L50)，差异立刻浮现：Unix 的 `from_options` 要处理 `reclaim_name`（建 `ReclaimGuard`）、`mode`（传给 `create_listener`）、`try_overwrite`/`max_spin_time`（覆盖逻辑）；这些在 Windows 上要么无对应、要么无意义。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：用一张映射表说清 Windows 后端 `from_options` 如何对待 `ListenerOptions` 的每一个字段——这是本讲规格里指定的实践任务。

**操作步骤**：

1. 打开 `ListenerOptions` 的字段定义 [listener/options.rs:17-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L17-L26) 与各 setter 的文档注释（[listener/options.rs:80-170](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L80-L170)）。
2. 对照 Windows 后端 `from_options`（[listener.rs:30-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L30-L42)）逐字段判断「被映射 / 被忽略 / 平台不存在」。
3. 填出下表（参考答案见下方）。

**需要观察的现象**：`ListenerOptions` 共有 `name`、`nonblocking`（四态）、`reclaim_name`、`try_overwrite`、`max_spin_time`、`security_descriptor`（仅 Windows）等配置项，但 Windows 后端 `from_options` 里只动用了其中少数几个。

**预期结果（参考答案表）**：

| `ListenerOptions` 字段 | Windows 后端的处理 | 落点 |
|---|---|---|
| `name` | 解构 `NameInner::NamedPipe(path)` 取路径 | `PipeListenerOptions.path` |
| `nonblocking`（四态） | accept 维度 `nb_accept` | `PipeListenerOptions.nonblocking` |
| `nonblocking`（四态） | stream 维度 `nb_stream` | 存入 `AtomicEnum`，accept 时施加（4.3） |
| `security_descriptor`（Windows 专有） | 原样透传 | `PipeListenerOptions.security_descriptor` |
| `reclaim_name` | **无对应**：named pipe 无 socket 文件需要 unlink | `do_not_reclaim_name_on_drop` 是空操作（[listener.rs:59](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L59)） |
| `try_overwrite` | **无对应**：named pipe 无法被覆盖 | setter 文档明说「Does nothing」（[listener/options.rs:129-130](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L129-L130)） |
| `max_spin_time` | **无对应** | setter 文档明说「Currently not used for anything」（[listener/options.rs:150-151](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L150-L151)） |
| `mode`（仅 Unix 存在） | 字段本身在 Windows 不编译 | —— |

**与 Unix 的对照小结**：Windows 后端「丢弃」了 Unix 的 `reclaim_name`/`try_overwrite`/`max_spin_time`/`mode`（它们要么无意义、要么字段不编译），「新增」了 `security_descriptor`；而非阻塞被拆成「accept 给底层、stream 存起来」两路。这正是跨平台抽象「同接口、异实现」的典型体现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Windows 后端敢于用 `let NameInner::NamedPipe(path) = options.name.0;` 这种不可失败的模式匹配？

**参考答案**：因为 `Name` 在 Windows 上构造时，其内部 `NameInner` 只可能是 `NamedPipe` 变体（u2-l4 讲过，`NameInner` 的四个变体由 `#[cfg]` 平台互斥，Windows 编译时其它三个变体根本不存在）。所以这是一个编译期就已确定成立的断言。

**练习 2**：`PipeListenerOptions` 默认的 `mode` 是什么？为什么 Windows 后端 `from_options` 没有显式设置 `mode`？

**参考答案**：默认 `mode = PipeMode::Bytes`（[listener/options.rs:85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L85)）。Windows 后端用 `PipeListenerOptions::new()` 取默认值后没有覆盖 `mode`，因为 local socket 要的是字节流（`PipeListener<Bytes, Bytes>`），而默认正是 `Bytes`，无需改动。

---

### 4.3 accept 与 AtomicEnum 非阻塞状态机

#### 4.3.1 概念说明

这是 Windows 后端最巧妙的部分。问题来源是接口形状不匹配：

- **公共接口** `traits::Listener::set_nonblocking` 接收的是四态 `ListenerNonblockingMode`（[trait.rs:51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L51)）。
- **底层接口** `PipeListener::set_nonblocking` 只接收一个 `bool`（[named_pipe/listener.rs:95](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L95)），它只能控制 accept 维度。

也就是说，底层 named pipe 的非阻塞只有「accept 是否非阻塞」一维，根本没有「将来 accept 出来的流是否非阻塞」这个独立开关——流的非阻塞状态是每条流各自的属性。于是 Windows 后端必须**自己记住**「用户想要的 stream 非阻塞状态」，并在每次 `accept` 出新流时手动施加。这个「记忆」就是 `nonblocking: AtomicEnum<ListenerNonblockingMode>` 字段。

对比 Unix 后端 [uds_local_socket/listener.rs:26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L26)：Unix 用的是 `AtomicBool`，因为它只需要记 stream 维度（accept 维度直接存在 `UnixListener` 上）。Windows 之所以用 `AtomicEnum<四态>` 而不是 `AtomicBool`，是因为它要把完整的四态都记下来（accept 维度也要存，以便 `accept` 时判断该不该事后调整）。

#### 4.3.2 核心流程

设 `nb_accept`/`nb_stream` 为用户想要的两维。底层 `PipeListener` 实例的非阻塞性在创建时就被设成 `nb_accept`（见 4.2，`PipeListenerOptions.nonblocking = nb_accept`）。于是 `accept` 出来的流默认非阻塞性 = `nb_accept`。要让流的非阻塞性变成 `nb_stream`，需要事后修正：

| 四态 | `nb_accept` | `nb_stream` | 流的默认状态 | accept 后是否需要调整 |
|---|---|---|---|---|
| `Neither` | false | false | 阻塞 | 否（已正确） |
| `Accept` | true | false | 非阻塞 | **是**：调成阻塞 |
| `Stream` | false | true | 阻塞 | **是**：调成非阻塞 |
| `Both` | true | true | 非阻塞 | 否（已正确） |

所以代码里 `accept` 只对 `Accept` 和 `Stream` 两个「两维不一致」的状态做事后调整，`Neither`/`Both` 落空不调。`set_nonblocking` 则把 accept 维度施加给底层，并把完整四态存入 `AtomicEnum`。

#### 4.3.3 源码精读

- [listener.rs:43-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L43-L53) —— `accept`：
  - 第 45 行 `self.listener.accept()` 调底层 `PipeListener::accept`，它在内部用 `ConnectNamedPipe` 等待连接（[named_pipe/listener.rs:63-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L63-L79)），并把结果包成 `Stream`。
  - 第 46 行 `self.nonblocking.load(Acquire)` 读出记下的四态。
  - 第 47-48 行 `Accept` → `stream.set_nonblocking(false)`（把默认非阻塞的流调回阻塞）。
  - 第 49-50 行 `Stream` → `stream.set_nonblocking(true)`（把默认阻塞的流调成非阻塞）。
  - `Neither`/`Both` 不匹配任何分支，不做调整。
- [listener.rs:54-58](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L54-L58) —— `set_nonblocking`：第 55 行只把 accept 维度 `nonblocking.accept_nonblocking()` 施加给底层 `PipeListener`，第 56 行把完整四态 `store` 进 `AtomicEnum`（`Release`）。

`AtomicEnum` 本身是个把 `#[repr(u8)]` 枚举塞进 `AtomicU8` 的轻量工具：

- [atomic_enum.rs:13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/atomic_enum.rs#L13) —— `AtomicEnum<E: ReprU8>(AtomicU8, PhantomData<E>)`。
- [atomic_enum.rs:18-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/atomic_enum.rs#L18-L23) —— `load`/`store` 通过 `ReprU8` 的 `to_u8`/`from_u8`（本质是 `transmute`）在枚举与 `u8` 间转换。
- [trait.rs:97](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L97) —— `unsafe impl crate::ReprU8 for ListenerNonblockingMode {}`，这之所以合法，是因为该枚举是 `#[repr(u8)]`（[trait.rs:66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L66)），四个判别值恰好是 0/1/2/3。

关于内存序：这里用 `Acquire`/`Release` 偏保守。底层 `PipeListener` 的注释甚至自嘲说它的 `nonblocking` 字段「其实都不需要是 atomic」（[named_pipe/listener.rs:66-68](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L66-L68)），因为真正的同步靠内部 `Mutex` 完成。Windows 后端这里的 `AtomicEnum` 同理，主要是为了满足 `set_nonblocking(&self)`（只取共享引用）的签名。

#### 4.3.4 代码实践

**实践目标**：验证 4.3.2 那张「事后调整」表的正确性。

**操作步骤**：

1. 打开 `ListenerNonblockingMode` 的定义与两个辅助方法：[trait.rs:65-96](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L65-L96)。确认 `accept_nonblocking()` 对 `Accept|Both` 为真、`stream_nonblocking()` 对 `Stream|Both` 为真。
2. 对照 `accept` 的两个 `matches!` 分支（[listener.rs:47-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L47-L50)），逐一验证四种模式下「流的默认非阻塞性（= `nb_accept`）」与「目标（= `nb_stream`）」是否一致。

**需要观察的现象**：当代码 `match` 命中 `Accept` 时，`nb_accept=true` 而 `nb_stream=false`，默认非阻塞的流需要被调回阻塞；命中 `Stream` 时正好相反。

**预期结果**：四种模式中只有 `Accept` 和 `Stream` 命中分支、各做一次相反方向的 `set_nonblocking`；`Neither`/`Both` 因为两维一致而无需调整。逻辑与 4.3.2 的表完全吻合。

> 待本地验证：可在 Windows 上写一个测试，分别用四种 `ListenerNonblockingMode` 建监听器，accept 后检查流的非阻塞行为（例如非阻塞读是否返回 `WouldBlock`）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `AtomicEnum<ListenerNonblockingMode>` 换成 `AtomicBool`（像 Unix 那样只记 stream 维度），会出什么问题？

**参考答案**：会丢失 accept 维度的信息。`accept` 时就无法区分「这是 `Accept` 模式（accept 非阻塞、流要阻塞）」还是「这是 `Neither` 模式（都阻塞）」，也就无法决定要不要把流调回阻塞。所以 Windows 必须记完整四态。

**练习 2**：`accept` 里为何用 `load(Acquire)`、`set_nonblocking` 里用 `store(Release)`？

**参考答案**：保证「写者存入四态」与「读者在 accept 中读到四态」之间的 happens-before 关系——`Release` 存储 + `Acquire` 读取配对，使读者能看到写者此前对相关状态的全部修改。不过实际上这里的并发安全性主要由底层 `PipeListener` 的 `Mutex` 兜底，原子操作更多是为了满足 `&self` 签名，内存序偏保守。

**练习 3**：`do_not_reclaim_name_on_drop` 在 Windows 后端为什么是空函数体？

**参考答案**：因为 Windows named pipe 没有需要清理的文件系统对象——管道名字登记在内核命名空间，监听器 drop 时句柄关闭即可，不存在 Unix 那种「僵尸 socket 文件需要 unlink」的问题（见 u3-l1 的名称回收概念）。所以「不回收名字」这件事在 Windows 上是天然成立的，函数无需做任何事。

---

### 4.4 Stream 包装器：连接、超时与拆分重聚

#### 4.4.1 概念说明

客户端侧的 `Stream` 同样是包装器，类型定义极其简洁：

- [stream.rs:30-31](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L30-L31) —— `pub struct Stream(pub(super) StreamImpl);`，其中 `StreamImpl = DuplexPipeStream<Bytes>`（[stream.rs:21-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L21-L23)）。

`pub(super)` 的元组结构体字段意味着：这个字段对 `local_socket` 父模块可见，但对外部用户不公开——既允许同模块的 `RecvHalf`/`SendHalf` 互访，又把底层实现藏好。与 `Listener` 一样，它用 `Bytes` 模式（双工字节流）。

`Stream` 实现了完整的 `traits::Stream` supertrait 链，但有几个 Windows 专属的「坑」需要留意，它们承接 u3-l3 已建立的结论。

#### 4.4.2 核心流程

`Stream` 的几个关键行为：

```
from_options（连接）:
  解构 NameInner::NamedPipe(path)
  → DuplexPipeStream::connect_by_path(path)      // 真正 CreateFile 连接 named pipe
  → 若 get_nonblocking_stream() 为真，set_nonblocking(true)
  → 包成 Stream

set_recv_timeout / set_send_timeout:
  恒返回 Err(Unsupported)                        // named pipe 不支持收发超时

split:
  self.0.split() → (RecvPipeStream, SendPipeStream)
  → 包成 (RecvHalf, SendHalf)

reunite:
  StreamImpl::reunite(rh.0, sh.0) → 失败时归还两半所有权
```

#### 4.4.3 源码精读

- [stream.rs:38-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L38-L45) —— `from_options`（连接）。注意它**不读 `wait_mode`**——这印证了 u3-l2 的结论：Windows local socket 包装层恒为 `Unbounded` 连接。非阻塞连接则由 `get_nonblocking_stream()` 单独控制。
- [stream.rs:53-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L53-L55) —— `set_recv_timeout`/`set_send_timeout` 都返回 `no_timeouts()`。错误信息见 [stream.rs:25-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L25-L27)：`"named pipes do not support I/O timeouts"`。这与 u3-l3 讲的「Windows named pipe 后端这两个方法恒返回 `Err(Unsupported)`」一致。
- [stream.rs:58-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L58-L66) —— `split`/`reunite` 直接转发底层 `DuplexPipeStream` 的同名方法（[named_pipe/stream/impl.rs:35-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L35-L45)），并把结果包成 `RecvHalf`/`SendHalf` newtype。失败时通过 `map_err` 把底层 `ReuniteError` 里的两半重新包回公共的 `RecvHalf`/`SendHalf`，原样归还所有权（承接 u7-l3 的 `ReuniteError` 所有权归还语义）。
- [stream.rs:69-76](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L69-L76) —— `StreamCommon`：`take_error` 恒返回 `Ok(None)`（Windows named pipe 没有类似 Unix `SO_ERROR` 的暂存错误机制，承接 u3-l3）；`peer_creds` 取 `peer_process_id()` 填 `pid`（承接 u3-l4，Windows 只有 PID 这一个字段可用）。
- [stream.rs:126-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L126-L137) —— `multimacro!` 批量为 `Stream` 套上转发宏（`forward_sync_read`、`derive_sync_mut_write`、`forward_try_clone`、`forward_as_handle` 等）。这是 u2-l3 / u7-l1 讲过的「宏的宏」机制：让 newtype 壳自动继承芯的全部 trait 实现，无需手写样板。

#### 4.4.4 代码实践

**实践目标**：对比 `set_recv_timeout` 在 Windows 与 Unix 后端的差异，体会「同接口、异实现」。

**操作步骤**：

1. 读 Windows 后端的 `set_recv_timeout`：[stream.rs:52-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L52-L53)，确认它返回 `Err(Unsupported)`。
2. 找到 Unix 后端的同名实现（在 `src/os/unix/uds_local_socket/stream.rs` 中），观察它如何调用 `setsockopt` 的 `SO_RCVTIMEO` 真正设置超时。

**需要观察的现象**：同样的 `stream.set_recv_timeout(Some(Duration::from_secs(1)))` 调用，在 Unix 上会真正生效，在 Windows 上直接报 `Unsupported` 错误。

**预期结果**：你能在源码层面确认，公共 `traits::Stream` 接口承诺的 `set_recv_timeout` 在两个后端行为不同——Windows named pipe 根本没有等价的内核超时机制，所以后端诚实地返回 `Unsupported`，而不是静默忽略。这也是为什么 u3-l3 强调「调用方必须处理 `Unsupported`」。

> 待本地验证：在两个平台上各写一段 `set_recv_timeout` 的调用并打印返回值，对比结果。

#### 4.4.5 小练习与答案

**练习 1**：`Stream(pub(super) StreamImpl)` 的 `pub(super)` 改成 `pub(crate)` 或 `pub` 会有什么后果？

**参考答案**：改成 `pub` 会把底层 `DuplexPipeStream` 暴露给外部用户，破坏封装（用户能绕过 local socket 抽象直接调 named pipe 特有方法）。`pub(super)` 让字段只对 `local_socket` 父模块内的兄弟类型（如 `RecvHalf`/`SendHalf`、`Listener::accept` 的包装）可见，刚好满足内部协作需要又守住了封装边界。

**练习 2**：为什么 Windows 后端的 `peer_creds` 只填了 `pid`，而 Unix 后端能填 `euid`/`egid`/`groups`？

**参考答案**：因为 named pipe 只暴露对端进程的 PID（`GetNamedPipeClientProcessId`，封装在 `peer_process_id()` 里），Windows 内核不像 Unix 的 `SO_PEERCRED`/`getpeereid` 那样在连接时刻记录对端的用户/组凭据。所以 `PeerCreds` 在 Windows 上只有 `pid` 有值，其余字段连方法都不存在（u3-l4 讲过的 `cfg` 编译期分层）。

**练习 3**：`reunite` 失败时，为什么 `map_err` 里要把底层的 `rh`/`sh` 重新包回 `RecvHalf`/`SendHalf`？

**参考答案**：因为底层 `StreamImpl::reunite` 返回的 `ReuniteError` 里装的是底层类型（`RecvPipeStream`/`SendPipeStream`），而公共 API 必须把所有权归还成公共的 `RecvHalf`/`SendHalf`，否则调用方拿到的是无法使用的内部类型。重新包装保证了「失败时原样归还两半所有权」这一契约对调用方成立。

---

## 5. 综合实践

把本讲的两条主线——**选项映射**（4.2）和**非阻塞状态机**（4.3）——串起来，完成下面这个综合任务：

**任务**：假设你要给同事写一份「Windows local socket 监听器创建」的内部说明，请完成以下三件事：

1. **画数据流**：从用户写下 `ListenerOptions::new().name(name).nonblocking(ListenerNonblockingMode::Stream).create_sync()` 开始，画出直到 `CreateNamedPipe` 发生为止的完整调用栈，标出每一层的类型（公共 `Listener` 枚举 / 后端 `np_impl::Listener` / 底层 `PipeListener`）。

2. **解释字段归宿**：针对上面这个 `Stream` 模式的监听器，说明 `nonblocking(Stream)` 这个四态值分别流向了哪里——哪一维给了底层 `PipeListenerOptions.nonblocking`，哪一维被存进了 `AtomicEnum`。

3. **预测 accept 行为**：当有客户端连进来时，`accept` 里的两个 `matches!` 分支哪一个会被命中？它会调用 `stream.set_nonblocking(true)` 还是 `false`？为什么？

**参考答案要点**：

1. 调用栈：`create_sync()` → `create_sync_as::<公共 Listener>()` → 公共 `Listener::from_options`（mkenum 的 match）→ `dispatch_sync::listen` → `create_sync_as::<np_impl::Listener>()` → 后端 `np_impl::Listener::from_options` → `PipeListenerOptions::create`（底层 `PipeListener::from_handle_and_options`）→ `CreateNamedPipe`。类型依次为：公共枚举 → 公共枚举 → 后端 `Listener` → 底层 `PipeListener`。

2. `ListenerNonblockingMode::Stream` 的 `accept_nonblocking()=false`、`stream_nonblocking()=true`。所以 accept 维度（false）流向 `PipeListenerOptions.nonblocking`（第 38 行），stream 维度（true）连同完整四态被存进 `AtomicEnum`（第 41 行）。

3. 命中 `Stream` 分支（[listener.rs:49-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L49-L50)），调用 `stream.set_nonblocking(true)`。因为底层实例是用 `nb_accept=false` 建的，accept 出来的流默认是阻塞的，而用户要的是非阻塞流，所以必须事后调成非阻塞。

> 待本地验证：在 Windows 上实际运行，确认 `Stream` 模式下 accept 出的流确实表现为非阻塞（例如没有数据时读立即返回 `WouldBlock`）。

## 6. 本讲小结

- Windows 上 local socket 由 **named pipe** 实现；后端类型 `os::windows::named_pipe::local_socket::{Listener, Stream}` 是「芯」，公共枚举是「壳」，二者由极薄的 `dispatch_sync` 入口粘合。
- 派发链 `create_sync()` → `create_sync_as` → `dispatch_sync::listen` → 后端 `from_options` → `CreateNamedPipe`，其中 `create_sync_as` 被调用两次（公共枚举一次、后端一次），与 Unix 后端完全对称。
- `from_options` 把 `ListenerOptions` 翻译成 `PipeListenerOptions`：只映射 `path`、`nonblocking`（仅 accept 维度）、`security_descriptor` 三项；Unix 专有的 `reclaim_name`/`try_overwrite`/`max_spin_time`/`mode` 在 Windows 上要么无意义、要么不编译。
- 非阻塞的四态被拆成两路：accept 维度直接给底层 `PipeListener`，stream 维度存进 `AtomicEnum<ListenerNonblockingMode>`，在每次 `accept` 时按需事后调整——这正是 Windows 后端用 `AtomicEnum` 而非 `AtomicBool` 的原因。
- `Stream` 包装器把连接、`split`/`reunite`、读写代理到底层 `DuplexPipeStream<Bytes>`；`set_recv_timeout`/`set_send_timeout` 恒返回 `Err(Unsupported)`，`take_error` 恒返回 `Ok(None)`——这些都是 Windows named pipe 与 Unix 的本质差异。
- 无论是选项映射的差异还是非阻塞拆分的技巧，都体现了跨平台抽象「同接口、异实现」的核心张力：公共 trait 承诺统一行为，各后端诚实落地（包括诚实地返回 `Unsupported`）。

## 7. 下一步学习建议

- **u4-l3（Windows 原生 named pipe API）**：本讲把 `PipeListener`/`DuplexPipeStream` 当作黑盒「芯」使用，下一讲将钻进 `os::windows::named_pipe` 模块，看清 `ConnectNamedPipe` 的 accept 循环、`PipeMode`（字节/消息）、实例机制，以及「不及时 accept 会阻塞新连接」的根因。
- **u8-l1（linger_pool）**：本讲提到 `Stream` 的 `flush` 是空操作——那 drop 时缓冲数据谁来刷？答案在 Windows 专有的 `linger_pool` 延迟刷新池，建议在学完 u4-l3 后阅读。
- **u9-l2（非阻塞、超时与等待模式）**：本讲只讲了 Windows 后端的非阻塞落点，系统对比四态 `ListenerNonblockingMode`、收发超时、`ConnectWaitMode` 三态在两个平台后端的实现差异，留待 u9-l2 收口。
