# ConnectOptions 与客户端连接

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `ConnectOptions` 构建器链式配置客户端，并解释它与 `Stream::connect` 一行写法的等价关系。
- 读懂「构建器 → `from_options` → `dispatch_sync::connect` → 平台后端」这条连接派发链，并知道公共层为什么从不直接做系统调用。
- 精确区分 `ConnectWaitMode` 三种模式（`Deferred` / `Timeout` / `Unbounded`）的语义，以及它们在 Unix 与 Windows 后端上的真实落地差异。
- 解释 `ConnectOptions` 如何用「位标志」把多个是/否选项压进一个 `u8`。

## 2. 前置知识

本讲承接以下已建立的认知（不重复展开）：

- **local socket 是抽象，不是 OS 原语**（u2-l1）：它在 Windows 上由 named pipe 实现、在 Unix 上由 Unix domain socket 实现。
- **enum dispatch 双层设计**（u2-l2）：公共 `Stream` 枚举用 `mkenum!` 生成，方法调用经 `dispatch!` 宏转发到当前平台的唯一后端，单平台编译时另一变体被 `#[cfg]` 排除、`match` 仅剩一臂，派发零开销。
- **`impmod!` 后端注入**（u2-l3）：公共层通过 `impmod!` 把后端的类型与函数以统一别名「注射」进来。
- **名称系统**（u2-l4）：连接前需要先用 `to_ns_name::<GenericNamespaced>()` 或 `to_fs_name::<GenericFilePath>()` 构造一个 `Name`。
- **构建器 + 位标志**（u3-l1）：`ListenerOptions` 已经演示过「把是/否开关压进一个 `u8`、用 `set_bit`/`has_bit` 读写」的套路；本讲的 `ConnectOptions` 用的是同一套手法。

一个补充术语：**非阻塞连接（nonblocking connect）**。常规 `connect()` 在连接未完成前会一直阻塞调用线程；把 socket 设为非阻塞后，`connect()` 会立即返回，连接在内核后台继续进行，程序随后用 `poll`/`select` 等待「可写」事件来确认连接是否真正建立。这正是 `Deferred` / `Timeout` 模式赖以实现的底层机制。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs) | 定义顶层枚举 `ConnectWaitMode`（三种等待模式）及其辅助方法 `timeout_or_unsupported`。 |
| [src/local_socket/stream/options.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs) | `ConnectOptions` 构建器本体：字段、位标志、setter、getter、构造方法。 |
| [src/local_socket/stream/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs) | `traits::Stream` 接口契约：`connect`、`from_options`、`set_nonblocking`、超时、`split` 等。 |
| [src/local_socket/stream/enum.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs) | 公共 `Stream` 枚举：`from_options` 在此把调用转发给后端 `dispatch_sync::connect`。 |
| [src/os/unix/local_socket/dispatch_sync.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs) / [src/os/windows/local_socket/dispatch_sync.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs) | 平台派发层：把 `ConnectOptions` 交给具体后端的 `Stream::from_options`。 |
| [src/os/unix/uds_local_socket/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs) / [src/os/windows/named_pipe/local_socket/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs) | 后端真正读 `wait_mode`、发起系统调用的地方。 |
| [src/os/windows/named_pipe/stream/impl/ctor.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs) | Windows 原生 named pipe 的连接构造器，`Deferred` 在此被判定为不支持。 |
| [src/macros.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs) | `builder_setters!` 宏，自动生成 `name` 这类按值 setter。 |
| [tests/local_socket/no_server.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/no_server.rs) | 「连接不存在的服务器」边界测试，是本讲实践的重要事实依据。 |

## 4. 核心概念与源码讲解

### 4.1 ConnectOptions：客户端构建器

#### 4.1.1 概念说明

`ConnectOptions` 是 local socket **客户端**的统一入口。它是一个「构建器（builder）」：先用 `new()` 取一份默认配置，再链式调用 setter 改字段，最后用一个 `connect_sync()` / `connect_sync_as()` 方法消费它，产出 `Stream`。

之所以需要构建器，是因为「连接」这件事其实有很多可调旋钮：连到哪个名字、要不要让得到的流非阻塞、以及「连接操作本身怎么等待」。构建器把这些旋钮集中管理，避免出现一长串重载函数。

它解决的核心问题：**让平台无关的公共代码不必关心连接细节，只负责收集选项；真正的系统调用全部下沉到平台后端。**

#### 4.1.2 核心流程

连接的端到端调用链如下（同步、公共 `Stream` 枚举）：

```text
用户: ConnectOptions::new().name(n).wait_mode(m).connect_sync()
        │  connect_sync() 调 connect_sync_as::<Stream>()
        ▼
公共枚举 Stream::from_options(options)          # enum.rs
        │  转发给后端派发函数
        ▼
dispatch_sync::connect(options)                  # os/<plat>/local_socket/dispatch_sync.rs (impmod! 注入)
        │  调 options.connect_sync_as::<后端 Stream>()
        ▼
后端 Stream::from_options(options)               # 真正发起 connect() 系统调用
        │  返回 后端 Stream
        ▼
.map(Stream::from)                               # 包回成公共枚举
```

注意一个精巧点：`connect_sync_as::<S>()` 在这条链里被调用了**两次**——第一次 `S` 是公共 `Stream` 枚举（用户视角），第二次 `S` 是后端具体 `Stream`（派发函数视角）。公共枚举的 `from_options` 是「分发器」，后端的 `from_options` 才是「执行者」。

#### 4.1.3 源码精读

**构建器本体与字段。** 三个字段：`name`、`flags`、`timeout`。

[src/local_socket/stream/options.rs:17-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L17-L22) —— `name` 是「连接目标的名字」（u2-l4 讲过的 `Name`），`flags` 是一个 `u8` 位标志（压缩多个开关），`timeout` 单独存超时时长（因为位标志只能存是/否，存不下时长）。

**位标志编码。** 连接选项里有两个「带语义」的开关（超时、延迟）和一个 `nonblocking_stream` 开关，被压进同一个 `u8`：

[src/local_socket/stream/options.rs:25-35](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L25-L35) —— 定义三个位移常量与读写原语。位含义如下表：

| bit | 位移常量 | 含义 |
|-----|---------|------|
| bit 0 | `SHFT_NONBLOCKING_STREAM` | 得到的流是否非阻塞 |
| bit 1 | `SHFT_TIMEOUT` | 等待模式为 `Timeout` |
| bit 2 | `SHFT_DEFERRED` | 等待模式为 `Deferred` |

bit 1 与 bit 2 互斥（同一时刻至多置一），两者都不置则为 `Unbounded`。`ALL_BITS = (1<<3)-1 = 0b111` 只用了低 3 位；`WAITMODE_UNMASK = 0b001` 用来在切换等待模式时清掉旧的 bit 1/2 而保留 bit 0。

`set_bit` 的位运算是经典套路：先 `flags & (ALL_BITS ^ (1<<pos))` 清掉目标位，再 `| ((val as u8) << pos)` 写入新值。

**默认值与 `name` setter。**

[src/local_socket/stream/options.rs:45-56](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L45-L56) —— `new()` 给出 `flags: 0`（即 `Unbounded` + 非非阻塞）、`timeout: Duration::ZERO`。`name` 的 setter 不是手写的，而是由 `builder_setters!` 宏生成。

[src/macros.rs:75-100](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L75-L100) —— 该宏展开出一个按值 setter：`pub fn name(mut self, name: Name<'n>) -> Self { self.name = name.into(); self }`。注意它是**按值消费 `self` 并返回 `Self`**（典型 builder 模式），所以链式调用时必须接住返回值——每个 setter 都标了 `#[must_use]`，忘了接住会被 lint 警告。

**构造方法。**

[src/local_socket/stream/options.rs:134-145](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L134-L145) —— `connect_sync()` 直接调 `connect_sync_as::<Stream>()`，后者只有一行：`S::from_options(self)`。也就是说，**构建器的终点就是把整个 `ConnectOptions` 借给目标类型的 `from_options`**。

**派发链的两端。**

- 公共枚举侧（分发器）：[src/local_socket/stream/enum.rs:84-87](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L84-L87) —— `Stream::from_options` 调用 `dispatch_sync::connect(options)`（`dispatch_sync` 由 `impmod!` 按 `cfg` 注入）。
- 后端侧（执行器，以 Unix 为例）：[src/os/unix/local_socket/dispatch_sync.rs:11-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs#L11-L14) —— 调 `options.connect_sync_as::<uds_impl::Stream>()`，即触发后端 `from_options`，再用 `.map(Stream::from)` 包成公共枚举。Windows 派发几乎对称：[src/os/windows/local_socket/dispatch_sync.rs:11-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L11-L14)。

#### 4.1.4 代码实践

**实践目标：** 亲手走一遍「构建器 → 连接」的最小调用，确认 `Stream::connect` 与 `ConnectOptions` 写法等价。

**操作步骤：**

1. 在仓库内启用 tokio 之外的普通构建（同步即可），新建一个临时二进制或在 `examples/local_socket/sync/stream.rs` 旁复制一份。
2. 分别用两种写法连接同一个名字：
   ```rust
   // 写法 A：一行 connect（trait 默认方法）
   let s1 = Stream::connect(name.clone())?;
   // 写法 B：等价的构建器写法
   let s2 = ConnectOptions::new().name(name).connect_sync()?;
   ```

**需要观察的现象：** 两种写法编译都能通过，行为一致（在没有服务器时都返回错误）。

**预期结果：** 两者等价——这正是 trait 文档对 `connect` 的定义（见 4.2.3）。若手边没有运行中的服务器，连接会立即报错，这是正常的，原因见 4.3。具体错误类型「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `name` 的 setter 用 `name.into()` 而不是直接 `self.name = name`？

**答案：** 宏为了通用，对所有字段统一走 `into()`；对 `Name<'n>` 而言 `Name: Into<Name>` 是恒等转换，编译期消除，无运行时开销。

**练习 2：** 假如有人连续调用 `.wait_mode(Timeout(t1)).wait_mode(Deferred)`，最终 `flags` 里 bit 1 和 bit 2 分别是什么？

**答案：** bit 2（Deferred）置 1，bit 1（Timeout）为 0。因为 `wait_mode` 先用 `WAITMODE_UNMASK`（`0b001`）清掉 bit 1/2 再设置新值，后一次调用会覆盖前一次。

---

### 4.2 traits::Stream：连接的接口契约

#### 4.2.1 概念说明

`traits::Stream`（注意是 trait，和公共 `Stream` 枚举同名但不同物）定义了「一个 local socket 字节流**能做什么**」。它声明了连接（`connect`/`from_options`）、非阻塞切换（`set_nonblocking`）、收发超时（`set_recv_timeout`/`set_send_timeout`）、拆分重聚（`split`/`reunite`）等方法。

它的关键作用是**封印接口边界**：后端类型（如 Unix 的 `uds_local_socket::Stream`、Windows 的 `named_pipe::local_socket::Stream`）实现这个 trait，公共枚举再通过 `dispatch!` 转发；外部用户则只和公共 `Stream` 枚举打交道。`from_options` 正是连接的「构造入口」，被刻意设计为「通常不该直接调用」。

#### 4.2.2 核心流程

`Stream` trait 里与「连接」直接相关的两个方法形成一对：

```text
便捷入口  connect(name)            = ConnectOptions::new().name(name).connect_sync_as::<Self>()
完整入口  from_options(&options)   = 真正由后端实现，发起系统调用
```

`connect` 是 trait 上的**默认方法**（带默认实现），它只是把单参数便捷写法展开成构建器写法；`from_options` 没有默认实现，每个后端必须各自实现。所以「连接」的全部真实逻辑都集中在后端的 `from_options` 里。

#### 4.2.3 源码精读

**`connect` 默认方法。**

[src/local_socket/stream/trait.rs:28-34](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L28-L34) —— 文档明确写道：等价于 `ConnectOptions::new().name(name).connect_sync_as::<Self>()`。这就把 4.1.4 实践里「两种写法等价」的结论钉死在源码里。

**`from_options` 契约。**

[src/local_socket/stream/trait.rs:67-71](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L67-L71) —— 注释提醒：通常**不要**直接调 `from_options`，应该用 `ConnectOptions` 上的 `connect_sync` / `connect_sync_as`。这是库作者刻意引导的用法边界。

**连接之外的配置方法（铺垫 4.3 与 u3-l3）。**

[src/local_socket/stream/trait.rs:36-51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L36-L51) —— `set_nonblocking`、`set_recv_timeout`、`set_send_timeout`。注意它们作用在**连接成功之后**得到的流上，是「连接后的运行时配置」，与连接时的 `wait_mode` 是两个不同阶段的概念，不要混淆（详见 4.3 的对比表）。

#### 4.2.4 代码实践

**实践目标：** 通过阅读一处测试，确认 `Stream::connect` 在「无服务器」时的错误类型，作为 4.3 实践的事实依据。

**操作步骤：** 阅读 [tests/local_socket/no_server.rs:12-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/no_server.rs#L12-L24)。

**需要观察的现象 / 预期结果：** 该测试断言：连接到一个不存在的 local socket 时，`Stream::connect` 返回的错误种类必须是 `NotFound` 或 `ConnectionRefused` 之一，否则测试失败。这是一个**关键事实**：对端不存在时，OS 的 `connect()` 系统调用本身立即失败（不是「连接进行中」），因此错误在构造流这一步就返回了。

#### 4.2.5 小练习与答案

**练习 1：** `Stream::connect` 既然有默认实现，后端为什么还要实现 `from_options`？

**答案：** `connect` 的默认实现最终调用的就是 `from_options`，后者没有默认实现。系统调用层面的连接逻辑（处理地址、非阻塞、等待）因平台而异，必须由各后端各自落地。

**练习 2：** `set_recv_timeout` 与 `ConnectOptions::wait_mode(Timeout(..))` 都是「超时」，区别是什么？

**答案：** `wait_mode` 控制的是**连接建立**阶段的等待（connect 时）；`set_recv_timeout` 控制的是**连接建立后、读取数据**阶段的等待（流上）。二者作用在生命周期的不同阶段（见 4.3.2 的对比）。

---

### 4.3 ConnectWaitMode：三种连接等待模式

#### 4.3.1 概念说明

`ConnectWaitMode` 回答一个问题：**当连接不能立刻完成时，`connect` 这一调用本身要不要等、等多久？** 它定义在 crate 根（`src/lib.rs`），是个带数据的枚举：

- `Deferred`：立即返回。连接在后台继续建立，随后的 I/O 会阻塞直到真正连上；若后台连接出错，该错误会在**下一次 I/O** 时暴露。
- `Timeout(Duration)`：进入等待态，最多等指定时长。超时仍未连上则返回 `TimedOut` 错误。
- `Unbounded`（默认）：进入等待态，无限期等待直到连上。

要强调一点：这三种模式只描述「**连接进行中（in-progress）**」时怎么办。如果连接是**立刻成功**或**立刻硬失败**（例如对端根本不存在），等待模式根本不会介入——这点是 4.3.4 实践的核心。

#### 4.3.2 核心流程

先看枚举本体与一个平台无关的辅助方法：

[src/lib.rs:42-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L42-L66) —— `timeout_or_unsupported` 把三态折叠成一个 `Option<Duration>`：`Unbounded → None`（无限等）、`Timeout(t) → Some(t)`（限时等）、`Deferred → Err(Unsupported)`（直接报「不支持」）。后端用这一个方法就把 `Deferred` 的「不支持」语义统一表达了出来。

**构建器如何把 `ConnectWaitMode` 写进位标志：**

[src/local_socket/stream/options.rs:86-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L86-L98) —— 先用 `WAITMODE_UNMASK` 清掉旧的 bit 1/2，再按变体置对应位（`Deferred` → bit 2，`Timeout` → bit 1 并写 `timeout` 字段，`Unbounded` → 两者都不置）。默认是 `Unbounded`。

**读取（getter）按优先级解码：**

[src/local_socket/stream/options.rs:119-128](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L119-L128) —— 先判 Deferred（bit 2），再判 Timeout（bit 1），否则 Unbounded。

**两阶段「超时」对照表**（务必区分）：

| 概念 | 配置入口 | 作用阶段 | 触发返回 |
|------|----------|----------|----------|
| 连接等待 | `ConnectOptions::wait_mode(Timeout(t))` | `connect` 建立连接时 | 连接未在 `t` 内完成 → `TimedOut` |
| 收发超时 | `Stream::set_recv_timeout` / `set_send_timeout` | 连接已建立、读写数据时 | 读写阻塞超时 → `WouldBlock`/`TimedOut` |

`wait_mode` 还附带一张「Unix 上额外 `fcntl` 次数」表，见 [src/local_socket/stream/options.rs:62-75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L62-L75)，它揭示：在 Unix 上 `Timeout`/`Deferred` 会用非阻塞 socket 连接，最终流的非阻塞态可能与期望不一致，需要额外的 `fcntl` 校正——这正是后端 `from_options` 里那段「比较并修正非阻塞态」逻辑的由来。

#### 4.3.3 源码精读

**Unix 后端如何消费 `wait_mode`。**

[src/os/unix/uds_local_socket/stream.rs:31-52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L31-L52) —— 三步：

1. 判断「是否要用非阻塞方式发起连接」：只要 `wait_mode` 是 `Timeout` 或 `Deferred`，就 `nonblocking_connect = true`（`Unbounded` 则阻塞连接）。
2. 调 `c_wrappers::create_client(addr, nonblocking_connect)` 建立非阻塞 socket 并尝试 `connect()`。该函数返回 `(fd, inprog)`，`inprog` 表示是否处于「连接进行中」（内核返回 `EINPROGRESS`/`EAGAIN`）。底层见 [src/os/unix/c_wrappers.rs:263-279](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L263-L279)。
3. **只有 `Timeout` 且确实处于进行中（`inprog`）时**，才调 `wait_for_connect(fd, Some(timeout))` 用 `poll` 限时等待；若超时返回 `TimedOut`，见 [src/os/unix/c_wrappers.rs:286-303](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L286-L303)。`Deferred` 则**跳过等待**直接返回，连接由后台完成。

关键推论：如果对端根本不存在，`create_client` 里的 `connect()` 会**立刻**返回 `NotFound`/`ConnectionRefused`（`inprog=false`），那么第 3 步的等待分支根本进不去——`wait_mode` 此时**不产生任何可观察差异**。这与 `no_server.rs` 测试的断言完全吻合。

**Windows 后端的处理：local socket 层与原生 pipe 层不一致。**

[src/os/windows/named_pipe/local_socket/stream.rs:38-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L38-L45) —— **local socket 包装层并不读取 `wait_mode`**，它直接调 `connect_by_path`（固定走 `Unbounded`），只处理 `nonblocking_stream`。也就是说，通过 `ConnectOptions` 走 local socket 时，三种等待模式在 Windows 上**被静默当作 `Unbounded`**（不会因为 `Deferred` 而报错）。

而**原生 named pipe** 层则真正消费 `wait_mode`：

[src/os/windows/named_pipe/stream/impl/ctor.rs:25-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L25-L53) —— 调 `wait_mode.timeout_or_unsupported(...)`，于是 `Deferred` 在此被判定为不支持、返回 `Unsupported` 错误；`Timeout`/`Unbounded` 则进入自旋/阻塞等待。`connect_by_path` 默认就是 `Unbounded`，见 [src/os/windows/named_pipe/stream/impl/ctor.rs:83-86](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L83-L86)。

> ⚠️ 注意：`wait_mode` 的文档注释（[options.rs:77-84](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L77-L84)）声称 Windows 上 `Deferred` 会在 `connect_*` 时报错——这描述的是**原生 named pipe** 行为；而 local socket 包装层实际并不读取 `wait_mode`。文档与 local socket 代码路径存在出入，以代码为准。

#### 4.3.4 代码实践

**实践目标：** 验证规格里「分别用 `Deferred` 和 `Timeout` 连接一个尚未启动的服务器，观察返回差异」这一设想，并解释你**实际**看到的现象。

**操作步骤：**

1. 写一个最小客户端，对同一个名字（无服务器）分别用三种 `wait_mode` 尝试连接，打印结果与耗时：

   ```rust
   // 示例代码（非项目原有代码）
   use interprocess::local_socket::{prelude::*, ConnectOptions, GenericFilePath, GenericNamespaced};
   use interprocess::ConnectWaitMode;
   use std::time::{Duration, Instant};

   fn name() -> std::io::Result<interprocess::local_socket::Name<'static>> {
       if GenericNamespaced::is_supported() {
           "no-such-server.sock".to_ns_name::<GenericNamespaced>()
       } else {
           "/tmp/no-such-server.sock".to_fs_name::<GenericFilePath>()
       }
   }

   fn try_mode(label: &str, m: ConnectWaitMode) {
       let t = Instant::now();
       let r = ConnectOptions::new().name(name().unwrap()).wait_mode(m).connect_sync();
       println!("{label}: {:?} after {:?}", r.as_ref().err().map(|e| e.kind()), t.elapsed());
   }

   fn main() {
       try_mode("Deferred", ConnectWaitMode::Deferred);
       try_mode("Timeout(2s)", ConnectWaitMode::Timeout(Duration::from_secs(2)));
       try_mode("Unbounded", ConnectWaitMode::Unbounded);
   }
   ```

2. 在 Unix 上运行（Windows 行为见下文「预期结果」的说明）。

**需要观察的现象 / 预期结果：**

- 在 **Unix** 上，三种模式几乎都会**立即**返回错误，错误种类为 `NotFound` 或 `ConnectionRefused`（与 `no_server.rs` 断言一致），耗时都在毫秒级——**并不会**看到 `Timeout(2s)` 真的等满 2 秒。
- 原因（结合 4.3.3）：对端不存在时 `connect()` 是**立刻硬失败**（`inprog=false`），`Timeout` 模式里那段「限时 `poll` 等待」分支根本进不去。所以本场景下 `wait_mode` 不带来可观察差异。`Timeout` 的「等满时长再返回 `TimedOut`」只在连接真正处于进行中时才发生（对 local socket 而言这种情形较难构造）。
- 在 **Windows** 上：由于 local socket 包装层不读 `wait_mode`（4.3.3），三种模式行为一致、立即失败；**不要**期待 `Deferred` 在此报 `Unsupported`——那是原生 named pipe（`connect_by_path_with_wait_mode`）的行为。
- 具体错误文本与精确耗时**待本地验证**（取决于平台、内核与名字类型）。

> 说明：如果你确实想观察到 `Deferred` 与 `Timeout` 的**真实**差异，最干净的途径是改用 **Windows 原生 named pipe API**（`PipeStream::connect_by_path_with_wait_mode`，见 [ctor.rs:94-105](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/ctor.rs#L94-L105)）：传入 `Deferred` 会立刻返回 `Unsupported` 错误，而 `Timeout`/`Unbounded` 会进入自旋/阻塞等待——这才是两种模式可观察差异最明显的场景（Windows，待本地验证）。这属于 u4-l3 范畴。

#### 4.3.5 小练习与答案

**练习 1：** 为什么对不存在的服务器，`Timeout(2s)` 不会等满 2 秒？

**答案：** 等待分支（`wait_for_connect`）只在 `inprog == true` 时进入。对端不存在时 `connect()` 立刻返回 `NotFound`/`ConnectionRefused`，`inprog` 为假，等待逻辑被跳过，错误立即抛出。

**练习 2：** `timeout_or_unsupported(ConnectWaitMode::Deferred)` 返回什么？谁依赖了这个返回值？

**答案：** 返回 `Err(io::Error::Unsupported(...))`。Windows 原生 named pipe 的 `RawPipeStream::connect`（ctor.rs）依赖它，把 `Deferred` 直接判为不支持；Unix 后端则没有用这个方法，而是用 `matches!` 直接判断。

**练习 3：** 通过 local socket 层在 Windows 上设置 `wait_mode(Deferred)`，会报错吗？

**答案：** 不会。local socket 包装层的 `from_options`（stream.rs:38-45）根本不读 `wait_mode`，它恒走 `Unbounded`。报错的 `Unsupported` 只在原生 named pipe 路径上出现。

## 5. 综合实践

**任务：** 画一张「连接生命周期」时序图，把本讲三个最小模块串起来，并改一个参数验证你的理解。

1. **画图**：画出一个客户端从 `ConnectOptions::new()` 到拿到可读写 `Stream` 的完整时序，至少标出：
   - 构建器阶段：`name` / `wait_mode` / `nonblocking_stream` 三个旋钮分别落到 `ConnectOptions` 的哪个字段/位；
   - 派发阶段：`connect_sync` → `Stream::from_options`（公共枚举）→ `dispatch_sync::connect` → 后端 `from_options`；
   - 系统调用阶段：Unix 的 `create_client` + 可选 `wait_for_connect`，以及「连接后」的 `set_nonblocking`/`set_recv_timeout` 属于哪个阶段。
2. **改参数**：参考 `examples/local_socket/sync/stream.rs` 的客户端（[stream.rs:18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L18) 把 `Stream::connect(name)` 换成完整的 `ConnectOptions::new().name(name).wait_mode(ConnectWaitMode::Deferred).connect_sync()`。
3. **预测并验证**：先在「无服务器」情况下预测行为（结合 4.3.4），再起一个服务器（如 u1-l4 的 listener 示例）让它**正常工作**一次，确认改写后通信依然成功。
4. 若 `Deferred` 在你的平台「似乎没区别」，请在时序图旁用一句话解释原因（提示：local socket 多为同步连接，无 in-progress 态；Windows local socket 层不读 wait_mode）。

**预期：** 时序图应清楚区分「连接等待（wait_mode）」与「连接后收发超时（set_recv_timeout/set_send_timeout）」两个阶段；改写后的客户端在服务器存在时能正常收发，行为与原 `Stream::connect` 一致。

## 6. 本讲小结

- `ConnectOptions` 是 local socket 客户端构建器：`name`、`flags`（位标志）、`timeout` 三字段；多个开关压进一个 `u8`，靠 `set_bit`/`has_bit` 读写，套路与 `ListenerOptions` 一致。
- 构建器终点 `connect_sync()` → `connect_sync_as::<S>()` → `S::from_options(&options)`；公共 `Stream` 枚举只做派发，真正系统调用在后端 `from_options`，且 `connect_sync_as` 在链中被调用两次（一次公共枚举、一次后端类型）。
- `traits::Stream::connect` 是默认方法，等价于 `ConnectOptions::new().name(name).connect_sync_as::<Self>()`；`from_options` 无默认实现，由各后端各自落地。
- `ConnectWaitMode` 三态（`Deferred`/`Timeout`/`Unbounded`，默认 `Unbounded`）只管「连接进行中」怎么办；`timeout_or_unsupported` 把 `Deferred` 折叠成 `Unsupported`、`Timeout` 折叠成 `Some(t)`、`Unbounded` 折叠成 `None`。
- 对端不存在时，OS 的 `connect()` 立刻硬失败（`NotFound`/`ConnectionRefused`，`no_server.rs` 为证），`wait_mode` 此时不产生可观察差异。
- Windows 的 local socket 包装层**不读** `wait_mode`（恒 `Unbounded`）；`Deferred` 报 `Unsupported` 只发生在原生 named pipe 层。

## 7. 下一步学习建议

- **u3-l3（Stream 的读写、拆分与重聚）**：本讲只解决「连接」，下一讲进入连接成功后的 `set_nonblocking`、`set_recv_timeout/set_send_timeout`、`split`/`reunite`，与本讲的「两阶段超时对照表」形成闭环。
- 想看 `wait_mode` 真正起作用的场景，可接着读 **u4-l3（Windows 原生 named pipe API）**，对照 `connect_by_path_with_wait_mode` 与 `RawPipeStream::connect` 的自旋等待。
- 对连接派发链里 `impmod!`/`dispatch!` 的宏机制仍想深挖，回顾 **u2-l2 / u2-l3**；本讲的 `builder_setters!` 则属于 u7-l1 宏系统全景的一部分。
