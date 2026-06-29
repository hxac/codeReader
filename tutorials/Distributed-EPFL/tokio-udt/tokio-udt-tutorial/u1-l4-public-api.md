# 公共 API 全貌与配置

## 1. 本讲目标

通过前几讲，你已经知道 tokio-udt 的目录骨架（u1-l3）和两个示例程序的运行方式（u1-l2）。本讲聚焦一个更实用的问题：**作为使用者，我能从 `tokio_udt` 这个 crate「触摸」到哪些东西？**

学完本讲，你应当能够：

1. 说出 crate 通过 `pub use` 对外暴露的全部公共类型，并解释每种类型扮演什么角色。
2. 看懂 [`UdtConfiguration`](#) 这个配置结构体的每一个字段，理解默认值与它们如何影响缓冲、多路复用与关闭行为。
3. 区分「公共 API」（用户可依赖的稳定接口）与 crate 内部用 `pub(crate)` 标注的实现细节，从而判断哪些东西将来可能变化、哪些是你编写应用代码时应该只调用的那一层。

本讲不写协议逻辑，只读两个门面文件 [`src/lib.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs) 与 [`src/configuration.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs)，并顺着配置字段去仓库里追几处真实使用点。

---

## 2. 前置知识

### 2.1 模块可见性与 `pub use`

在 Rust 中：

- `pub mod foo;` 把子模块 `foo` 暴露给父模块；
- `pub use foo::Bar;` 把 `foo` 里的类型 `Bar`「重新导出」到当前路径，外部就能用 `crate::Bar` 而不是 `crate::foo::Bar` 访问它；
- `pub(crate) use ...` / `pub(crate) struct ...` 表示「在整个 crate 内部可见，但对 crate 外的用户不可见」。

tokio-udt 把所有实现细节藏在私有模块里，只在 crate 根 `lib.rs` 用 `pub use` 挑选少数几个类型对外暴露。理解这条「导出边界」是本讲的核心。

### 2.2 序列号（seq number）的初步印象

你不需要在本讲搞懂序列号的全部算术（那是 u4-l4 的内容），只需知道：UDT 用一个不断递增、会回绕的整数来标识「第几个包」。`SeqNumber` 是 tokio-udt 对外导出的、用来表示这种序列号的类型，本讲只把它当作「一个被导出的公共类型」看待即可。

### 2.3 MTU 与 MSS

- **MTU**（Maximum Transmission Unit）：链路层一帧能承载的最大字节数，以太网常见值是 1500 字节。
- **MSS**（Maximum Segment Size）：在 MTU 之内，扣掉各层头部后，应用层一次能发送的最大数据块大小。

tokio-udt 默认 `mss = 1500`（见下文配置），这决定了单个 UDT 数据包最多能装多少应用数据，进而影响发送缓冲按多大切片（u5-l1 会展开）。

---

## 3. 本讲源码地图

| 文件 | 一句话职责 |
| --- | --- |
| [`src/lib.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs) | crate 根：顶层文档、私有模块声明、5 个公共 `pub use` 导出。 |
| [`src/configuration.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs) | `UdtConfiguration` 结构体定义、默认常量、`Default` 实现。 |

此外，为了说明「配置如何流入代码」，本讲会**引用**（但不在本讲精读）以下文件中的若干使用点：

- [`src/multiplexer.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs)：读取 `udp_reuse_port`、`reuse_mux`。
- [`src/udt.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs)：读取 `flight_flag_size`、`accept_queue_size`、`reuse_mux`。
- [`src/socket.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs)：读取 `rcv_buf_size`、`flight_flag_size`、`linger_timeout`。
- [`src/connection.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs)：消费 `UdtConfiguration`、暴露 `rate_control()` 等公共方法。
- [`src/listener.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs)：`bind` 接受 `Option<UdtConfiguration>`。

---

## 4. 核心概念与源码讲解

### 4.1 公共 API 全貌：五个 `pub use` 导出

#### 4.1.1 概念说明

一个库对外的「公共 API」就是它声明「允许用户依赖」的那一层接口。在 Rust 里，这层接口通常出现在 crate 根（`lib.rs`）的 `pub use` 行。tokio-udt 只对外暴露 **5 个类型**，用户写的所有应用代码都只围绕这 5 个类型展开。

#### 4.1.2 核心流程

```text
src/lib.rs
├── 私有 mod 声明（17 个，全部不对外）   ← 实现细节
└── pub use（5 个）                      ← 公共 API
        ├── UdtConfiguration   （配置）
        ├── UdtConnection      （客户端连接：AsyncRead/AsyncWrite）
        ├── UdtListener        （服务端监听：bind + accept）
        ├── RateControl        （只读/可写地观察拥塞控制指标）
        └── SeqNumber          （UDT 序列号类型）
```

这 5 个类型按使用场景可分为三组：

| 角色 | 类型 | 何时用 |
| --- | --- | --- |
| 配置 | `UdtConfiguration` | 想覆盖默认行为（如缓冲大小、复用端口）时构造它 |
| 入口 | `UdtListener`、`UdtConnection` | 写服务端用 `UdtListener`，写客户端用 `UdtConnection` |
| 观察 | `RateControl`、`SeqNumber` | `RateControl` 读取/调节拥塞指标；`SeqNumber` 在需要操作序列号时使用 |

#### 4.1.3 源码精读

`lib.rs` 末尾的 5 行 `pub use` 就是全部公共 API 的来源：

[文件路径:lib.rs:86-90 — 五个 pub use 导出](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90)

```rust
pub use configuration::UdtConfiguration;
pub use connection::UdtConnection;
pub use listener::UdtListener;
pub use rate_control::RateControl;
pub use seq_number::SeqNumber;
```

注意：导出的源模块本身是**私有**的（`mod configuration;` 而非 `pub mod configuration;`），见 [lib.rs:68-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L68-L84)：

```rust
mod ack_window;
mod common;
mod configuration;
// ... 其余 14 个私有 mod
```

这意味着用户写 `use tokio_udt::UdtConfiguration;` 可以，但写 `use tokio_udt::configuration::...` 是不行的——`configuration` 模块对 crate 外不可见。tokio-udt 通过这种方式「只露类型、不露实现路径」。

> 小提示：README 的两个示例只用到 `UdtListener` 和 `UdtConnection` 两个类型，这也是绝大多数用户日常接触的全部。`RateControl` 与 `SeqNumber` 是进阶用途，本系列后文（u7 拥塞控制、u4-l4 序列号）会深入。

#### 4.1.4 代码实践

实践目标：确认「公共 API 只有 5 个类型」这条结论，并熟悉从文档查找它们的方式。

操作步骤：

1. 在项目根目录执行：
   ```bash
   cargo doc --no-deps --open
   ```
2. 在打开的浏览器文档里，找到 `tokio_udt` 这个 crate 的根页面。
3. 数一数「Re-exports」一节里列出的类型，应当正好是这 5 个。

需要观察的现象：

- 文档根页面「Modules」一节是空的（因为所有 mod 都是私有的，不会出现在公共文档里）。
- 「Structs」一节会列出这 5 个被重导出的类型。

预期结果：你在 `cargo doc` 里看到的公共类型集合，与 [lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90) 的 5 行 `pub use` 完全一致。如果不一致，说明你看到的代码版本与本讲不是同一个 HEAD。

> 若本地无法联网或编译环境受限，可直接阅读 [lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90)，结论相同。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mod configuration;` 写成 `mod` 而不是 `pub mod`，但用户仍然能用 `UdtConfiguration`？

> **答案**：因为模块虽然私有，但 `pub use configuration::UdtConfiguration;` 把 `UdtConfiguration` 这个**类型本身**重导出到了 crate 根。可见性是「按路径」的：只要有一条「全 pub」的路径（这里是 crate 根的重导出）能到达类型，外部就能用。`mod configuration` 私有只是阻止了用户写 `tokio_udt::configuration::UdtConfiguration` 这条**路径**，但通过重导出后的短路径 `tokio_udt::UdtConfiguration` 仍然可达。

**练习 2**：如果一个新类型想加入「公共 API」，最少要改 `lib.rs` 的几处？

> **答案**：两处协同即可——先确保定义它的模块里有 `pub struct X`（类型本身是 `pub` 的），再在 [lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90) 加一行 `pub use ...::X;`。注意版本尚处于 0.1.0-alpha，新增/移除公共类型会破坏 API 兼容性。

---

### 4.2 UdtConfiguration：协议参数容器

#### 4.2.1 概念说明

`UdtConfiguration` 是 tokio-udt 唯一的「配置对象」，承载所有可调参数：包大小、各类缓冲容量、多路复用开关、关闭行为等。它的设计很朴素——一个纯数据结构体，所有字段都是 `pub` 的，你可以直接构造或修改它；它还实现了 `Default`，所以「不想改任何东西」时直接传 `None`（见 4.3）即可走默认值。

#### 4.2.2 核心流程

```text
用户 → UdtConfiguration::default()  （或自定义字段）
     → 传入 UdtConnection::connect(.., Some(config)) / UdtListener::bind(.., Some(config))
     → 内部 UdtSocket::new(.., Some(config)) 持有这份配置（用 RwLock 包裹）
     → 收发过程中各模块按需读取对应字段
```

配置在内部被存进 `UdtSocket` 的 `configuration: Arc<RwLock<UdtConfiguration>>` 字段里（u3-l2 会详细讲），所以它既是「创建时的参数」，又是「运行时可被读取的快照」（例如 `close()` 时还要读 `linger_timeout`）。

#### 4.2.3 源码精读

整张字段表来自这个结构体：

[文件路径:configuration.rs:10-48 — UdtConfiguration 结构体](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L10-L48)

逐字段含义与默认值汇总如下（默认值取自 [configuration.rs:56-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L56-L72) 的 `Default` 实现）：

| 字段 | 类型 | 默认值 | 含义与影响 |
| --- | --- | --- | --- |
| `mss` | `u32` | `1500` | 单个数据包最大字节数；连接双方会协商取**较小值**（见 [socket.rs:122](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L122) 写回 `hs.max_packet_size`）。 |
| `flight_flag_size` | `u32` | `256000` | 最大在途窗口（包数）；握手时上报给对端（[udt.rs:123](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L123)），并约束接收侧流量（[socket.rs:185](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L185)）。注释要求「不小于 `rcv_buf_size`」。 |
| `snd_buf_size` | `u32` | `81920` | 发送缓冲能暂存的包数（[configuration.rs:61](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L61)，`DEFAULT_UDT_BUF_SIZE`）。 |
| `rcv_buf_size` | `u32` | `163840` | 接收缓冲能暂存的包数（默认是 `DEFAULT_UDT_BUF_SIZE * 2`，见 [configuration.rs:62](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L62)）。 |
| `udp_snd_buf_size` | `usize` | `8_000_000` | 底层 UDP socket 的发送缓冲字节数（受内核 `net.core.wmem_max` 限制）。 |
| `udp_rcv_buf_size` | `usize` | `8_000_000` | 底层 UDP socket 的接收缓冲字节数（受内核 `net.core.rmem_max` 限制）。 |
| `udp_reuse_port` | `bool` | `false` | 是否对 UDP socket 设 `SO_REUSEPORT`（[multiplexer.rs:44](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L44)），Linux 下可把多客户端流量分到不同线程/多路复用器。 |
| `reuse_mux` | `bool` | `true` | 绑定同一端口时是否复用已存在的 multiplexer（[multiplexer.rs:64](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L64)、[multiplexer.rs:89](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L89)），并影响 [udt.rs:196](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196) 的查找。 |
| `rendezvous` | `bool` | `false` | 「会合模式」开关，**当前未实现**（见注释 [configuration.rs:42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L42)）。 |
| `accept_queue_size` | `usize` | `1000` | 已握手但尚未被 `accept` 取走的连接数上限，超过则拒绝（[udt.rs:145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145)）。 |
| `linger_timeout` | `Option<Duration>` | `Some(10s)` | `close()` 时最多等待发送缓冲排空的时长（[socket.rs:1159-1168](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1159-L1168)）；`None` 表示「不等待」（`unwrap_or(Duration::ZERO)`）。 |

另外，结构体上还附带一个简单方法，返回协议版本号常量：

[文件路径:configuration.rs:50-54 — udt_version 返回常量 4](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L50-L54)

```rust
pub fn udt_version(&self) -> u32 {
    UDT_VERSION
}
```

而顶部的几个常量定义了默认值来源：

[文件路径:configuration.rs:3-6 — 默认常量](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L3-L6)

```rust
const DEFAULT_MSS: u32 = 1500;
const DEFAULT_UDT_BUF_SIZE: u32 = 81920;
const DEFAULT_UDP_BUF_SIZE: usize = 8_000_000;
const UDT_VERSION: u32 = 4;
```

#### 4.2.4 代码实践

实践目标：用一个自定义 `UdtConfiguration` 启动服务端，观察 `accept_queue_size` 在真实代码里的判定点。

操作步骤：

1. 在 `examples/` 或临时 `cargo run --example` 中写一小段服务端（若仓库无 `examples/` 目录，可直接对照 `src/bin/udt_receiver.rs` 理解）：
   ```rust
   use std::net::Ipv4Addr;
   use tokio_udt::{UdtConfiguration, UdtListener};

   # #[tokio::main]
   # async fn main() -> tokio::io::Result<()> {
   let mut cfg = UdtConfiguration::default();
   cfg.accept_queue_size = 2;   // 故意调小，便于观察「排队已满」
   let listener = UdtListener::bind((Ipv4Addr::UNSPECIFIED, 9000).into(), Some(cfg)).await?;
   # let _ = listener;
   # Ok(())
   # }
   ```
   （上述为**示例代码**，展示构造与传参方式；`# ` 前缀行用于隐藏到文档测试之外。）

2. 打开 [udt.rs:145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145)，阅读 `queued_sockets.len() >= config.accept_queue_size` 这条判定。

需要观察的现象：

- 当并发发起的握手连接数超过 `accept_queue_size`（此处为 2）且尚未 `accept` 取走时，新连接会收到 `Too many queued sockets` 错误。

预期结果：把 `accept_queue_size` 调到很小的值后，并发连接会被拒绝；调大（默认 1000）则正常排队。这一现象依赖运行环境，**若无法本地多连接压测，标注「待本地验证」**。

#### 4.2.5 小练习与答案

**练习 1**：`linger_timeout` 设为 `None` 和设为 `Some(Duration::ZERO)` 有区别吗？结合源码说明。

> **答案**：运行效果等价。在 [socket.rs:1159-1164](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1159-L1164)，`None` 被 `unwrap_or(Duration::ZERO)` 转成零时长，而 [socket.rs:1166-1171](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1166-L1171) 的 `while` 条件 `now.elapsed() < linger_timeout` 在零时长下立即为假，循环不执行——即「不等排空直接关闭」。语义上 `None` 表示「未设置/不等待」，`Some(0)` 表示「显式等 0 秒」，结果一样。

**练习 2**：为什么 `udp_snd_buf_size` 设了 8 MB，实际生效值可能远小于它？

> **答案**：注释 [configuration.rs:24-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L24-L25) 说明，这个值是「请求值」，最终由内核上限 `net.core.wmem_max` 截断。可用 `sysctl net.core.wmem_max` 查看当前上限。

**练习 3**：`flight_flag_size` 与 `rcv_buf_size` 在哪里被一起使用？

> **答案**：在 [socket.rs:185](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L185) 处取两者的较小值。这也是字段注释 [configuration.rs:16](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L16) 提醒「应设为不小于 `rcv_buf_size`」的原因。

---

### 4.3 配置如何流入代码：connect / bind / accept 的签名

#### 4.3.1 概念说明

光知道 `UdtConfiguration` 有哪些字段还不够，还得知道它「从哪个入口喂进去」。tokio-udt 的两个入口——客户端 `UdtConnection::connect` 和服务端 `UdtListener::bind`——都接受 `config: Option<UdtConfiguration>`。传 `None` 就用默认值，传 `Some(cfg)` 就用你的配置。

#### 4.3.2 核心流程

```text
传 None ──► 内部 UdtSocket::new(.., None) ──► 用 UdtConfiguration::default()
传 Some(cfg) ──► UdtSocket::new(.., Some(cfg)) ──► 直接持有 cfg
```

无论哪条路，最终 `UdtSocket` 都持有一份配置，后续模块（multiplexer、queue、socket 自身）按字段名读取。

#### 4.3.3 源码精读

服务端入口（注意第二个参数就是配置）：

[文件路径:listener.rs:14 — UdtListener::bind 接受 Option<UdtConfiguration>](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L14)

```rust
pub async fn bind(bind_addr: SocketAddr, config: Option<UdtConfiguration>) -> Result<Self> {
```

客户端有三个相关方法（[connection.rs:19-37](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L19-L37)）：`connect`、`bind_and_connect`、以及内部的 `_bind_and_connect`，它们都接受 `config: Option<UdtConfiguration>`：

[文件路径:connection.rs:19-32 — connect 与 bind_and_connect 签名](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L19-L32)

```rust
pub async fn connect(addr: impl ToSocketAddrs, config: Option<UdtConfiguration>) -> Result<Self> { ... }
pub async fn bind_and_connect(bind_addr: SocketAddr, connect_addr: impl ToSocketAddrs,
                              config: Option<UdtConfiguration>) -> Result<Self> { ... }
```

它们最终把 `config` 透传给 `Udt::new_socket(.., config)`，见 [connection.rs:39-41](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L39-L41)：

```rust
let socket = {
    let mut udt = Udt::get().write().await;
    udt.new_socket(SocketType::Stream, config)?.clone()
};
```

`UdtConnection` 除了建立连接，还另外暴露了几个公共方法（用于读写与观察），这里一并了解：

[文件路径:connection.rs:74-95 — send/recv/rate_control/close/socket_id](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L74-L95)

```rust
pub async fn send(&self, msg: &[u8]) -> Result<()> { ... }
pub async fn recv(&self, buf: &mut [u8]) -> Result<usize> { ... }
pub fn rate_control(&self) -> std::sync::RwLockWriteGuard<'_, crate::rate_control::RateControl> { ... }
pub async fn close(&self) { ... }
pub fn socket_id(&self) -> u32 { ... }
```

其中 `rate_control()` 返回的是一把**写锁守卫**（`RwLockWriteGuard`），这正是 u1-l2 里 sender / receiver 打印速率指标的入口——拿到守卫后可以读取 `pkt_send_period`、`congestion_window_size` 等字段（这些字段的调节算法在 u7-l2 详述）。

#### 4.3.4 代码实践

实践目标：亲手写一个「显式传配置」的客户端，确认 `Option<UdtConfiguration>` 的传参方式。

操作步骤：

1. 仿照 README 客户端示例，把 `None` 换成自定义配置（**示例代码**）：
   ```rust
   use std::net::Ipv4Addr;
   use tokio::io::AsyncWriteExt;
   use tokio_udt::{UdtConfiguration, UdtConnection};

   # async fn _demo() -> tokio::io::Result<()> {
   let mut cfg = UdtConfiguration::default();
   cfg.mss = 1400;                       // 把 MSS 改小一点
   let mut conn = UdtConnection::connect((Ipv4Addr::LOCALHOST, 9000), Some(cfg)).await?;
   conn.write_all(b"hello").await?;
   # Ok(())
   # }
   ```
2. 跟踪 `config` 的去向：`connect` → [`_bind_and_connect` (connection.rs:34-42)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L34-L42) → `Udt::new_socket(.., config)`。

需要观察的现象：

- 改 `mss` 后，握手阶段本端上报的 `max_packet_size` 会随之变化（见 [socket.rs:122](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L122)，握手响应里写回 `configuration.mss`），最终双方协商取较小值。

预期结果：自定义 `mss` 通过握手反映到对端；具体报文大小可在抓包工具（如 Wireshark / `tcpdump`）里看到。**是否方便抓包取决于本地环境，必要时标注「待本地验证」**。

#### 4.3.5 小练习与答案

**练习 1**：README 服务端示例里 `UdtListener::bind(addr, None)`，这个 `None` 最终变成什么？

> **答案**：进入 `UdtSocket::new(.., None)` 后，内部用 `UdtConfiguration::default()`（[configuration.rs:56-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L56-L72)）构造一份默认配置，即 mss=1500、reuse_mux=true、accept_queue_size=1000、linger_timeout=10s 等。

**练习 2**：`rate_control()` 返回 `RwLockWriteGuard` 而不是只读引用，意味着什么？

> **答案**：说明调用方不仅能读拥塞控制指标，还能**写**它们（例如手工调节 `pkt_send_period`）。这也是为什么 u1-l2 的示例能直接读取这两个字段——读和写共用同一把写锁守卫。具体可写字段与算法见 u7-l2。

---

### 4.4 公共 API 与 `pub(crate)` 内部实现的边界

#### 4.4.1 概念说明

知道「能用什么」之后，同样重要的是知道「什么不该依赖」。tokio-udt 用 `pub(crate)` 把大量内部实现细节限制在 crate 内部，这些细节**没有出现在 `lib.rs` 的 `pub use` 里**，因此对用户不可见、将来可能随时重构。理解这条边界，能帮你判断：哪些是稳定 API、哪些是「内部实现，别依赖」。

#### 4.4.2 核心流程

判断一个东西是否属于公共 API 的速查规则：

```text
在 src/lib.rs 里出现 pub use 吗？
  是 ──► 公共 API（用户可依赖）
  否（只有 mod / pub(crate)） ──► 内部实现（不要依赖）
```

#### 4.4.3 源码精读

对比 `lib.rs` 的两段：

- **私有 mod 声明**（[lib.rs:68-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L68-L84)）：17 个模块（`ack_window`、`common`、`control_packet`、`data_packet`、`flow`、`loss_list`、`multiplexer`、`packet`、`queue`、`rate_control`、`seq_number`、`socket`、`state`、`udt` 等）全是 `mod`，没有 `pub`。

- **公共导出**（[lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90)）：仅 5 个类型。

由此可推出一个有意思的现象：很多「名字出现在公共 API 里」的类型，其**所在模块**是私有的。例如：

- `RateControl` 被 `pub use` 导出，但它来自私有模块 `mod rate_control;`。
- `UdtConnection` 的方法 `rate_control()` 返回类型签名里写的是 `crate::rate_control::RateControl`（见 [connection.rs:83-87](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L83-L87)），这条签名之所以合法，是因为 `RateControl` 类型本身是 `pub` 的（被重导出过）。

再如 `UdtConnection::new` 是 `pub(crate)` 的（[connection.rs:15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L15)）：

```rust
pub(crate) fn new(socket: SocketRef) -> Self { ... }
```

它只能在 crate 内部调用（比如 `UdtListener::accept` 拿到 socket 后构造 `UdtConnection` 返回给用户），用户无法直接 `UdtConnection::new(...)`。这就是「内部构造、外部使用」的典型边界。

#### 4.4.4 代码实践

实践目标：用编译器验证「公共 vs 内部」的边界。

操作步骤：

1. 在一个**依赖了 tokio-udt 的外部 crate** 里，尝试写以下代码并编译：
   ```rust
   // (A) 合法：走重导出路径
   use tokio_udt::UdtConfiguration;

   // (B) 非法：私有模块路径
   // use tokio_udt::configuration::UdtConfiguration;   // 注释掉，否则编译失败
   ```
2. 对照 [lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90) 与 [lib.rs:70](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L70)（`mod configuration;`）。

需要观察的现象：

- (A) 能编译通过；
- (B) 报「`configuration` is private」之类的错误。

预期结果：编译器的可见性错误正好印证了「私有 mod + pub use」的导出策略。

> 若没有独立的外部 crate 可测，可在仓库内用 `cargo check --tests` 配合一段引用 `tokio_udt::configuration::...` 的注释代码来观察编译器提示，**不修改源码**。结论与外部 crate 一致。

#### 4.4.5 小练习与答案

**练习 1**：用户能不能调用 `UdtConnection::new`？为什么？

> **答案**：不能。它是 `pub(crate)` 的（[connection.rs:15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L15)），只在 crate 内部可见。用户只能通过 `UdtConnection::connect` / `bind_and_connect`（或从 `UdtListener::accept` 拿到）来获得一个 `UdtConnection` 实例。

**练习 2**：`AckWindow`、`LossList`、`UdtMultiplexer` 这些类型属于公共 API 吗？

> **答案**：不属于。它们对应的模块 `ack_window`、`loss_list`、`multiplexer` 都在 [lib.rs:68-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L68-L84) 以私有 `mod` 声明，且没有出现在 [lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90) 的 `pub use` 里，是 crate 内部实现。它们是后续进阶讲义（u5、u6、u8）要拆解的对象，但**不应被你的应用代码直接依赖**。

---

## 5. 综合实践

把本讲的「公共 API」与「配置」串起来，完成下面这个「配置自检表」任务：

1. 列出 [lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90) 里全部 `pub use` 的类型（应得 5 个），并用一句话写出每个类型的用途。
2. 从 `UdtConfiguration`（[configuration.rs:10-48](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L10-L48)）里挑出下面 5 个关键字段，用一句话说明「它会影响哪段代码路径」，并**附上使用处的永久链接**：
   - `reuse_mux` —— 提示：搜索 `multiplexer.rs` 与 `udt.rs`。
   - `accept_queue_size` —— 提示：搜索 `udt.rs`。
   - `linger_timeout` —— 提示：搜索 `socket.rs` 的 `close`。
   - `udp_reuse_port` —— 提示：搜索 `multiplexer.rs`。
   - `mss` —— 提示：搜索 `socket.rs` 的握手响应。
3. 把上述结论整理成一张 Markdown 表格，作为你自己的「tokio-udt 公共 API 速查表」。

参考答案（核对用，不要先看）：

| 字段 | 影响 | 出处 |
| --- | --- | --- |
| `reuse_mux` | 绑定同端口时是否复用已存在的 multiplexer | [multiplexer.rs:64](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L64)、[udt.rs:196](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196) |
| `accept_queue_size` | 已握手待 accept 连接数的上限 | [udt.rs:145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145) |
| `linger_timeout` | `close()` 时等待发送缓冲排空的最长时间 | [socket.rs:1159-1168](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1159-L1168) |
| `udp_reuse_port` | 是否对底层 UDP socket 设 `SO_REUSEPORT` | [multiplexer.rs:44](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L44) |
| `mss` | 握手时上报的 `max_packet_size`，双方取较小值 | [socket.rs:122](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L122) |

---

## 6. 本讲小结

- tokio-udt 对外只暴露 **5 个公共类型**：`UdtConfiguration`、`UdtConnection`、`UdtListener`、`RateControl`、`SeqNumber`，全部来自 [lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90) 的 `pub use`。
- 这 5 个类型按角色分三组：**配置**（`UdtConfiguration`）、**入口**（`UdtListener` / `UdtConnection`）、**观察**（`RateControl` / `SeqNumber`）。
- `UdtConfiguration` 是 11 个 `pub` 字段的纯数据结构体，默认值集中在 [configuration.rs:56-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L56-L72)；关键字段（`mss`、`reuse_mux`、`accept_queue_size`、`linger_timeout`、`udp_reuse_port` 等）各自影响一段明确代码路径。
- 配置通过入口的 `Option<UdtConfiguration>` 参数流入：传 `None` 走默认，传 `Some(cfg)` 走自定义；`UdtConnection` 还额外暴露 `send/recv/rate_control/close/socket_id`。
- crate 内部用 `pub(crate)`（如 `UdtConnection::new`）和私有 `mod` 隐藏实现细节，**未出现在 `pub use` 里的类型（如 `AckWindow`、`LossList`、`UdtMultiplexer`）不属于公共 API**，不应被应用代码依赖。

---

## 7. 下一步学习建议

本讲之后，你已经掌握了「能用什么」和「怎么配」。接下来按角色深入：

- 想立刻把客户端/服务端写起来 → 进入 **u2-l1（客户端 connect 与 AsyncWrite）** 与 **u2-l2（服务端 bind 与 accept）**。
- 想逐字段吃透每一个配置参数的默认值与取舍 → 进入 **u2-l3（配置项与调优：UdtConfiguration）**。
- 想理解 `rate_control()` 返回的那把写锁背后到底是什么 → 暂存到 **u7-l2（RateControl：慢启动与 AIMD）** 再回头看。

建议保存本讲的「公共 API 速查表」，后续阅读源码时随时对照「这个类型是公共 API 还是内部实现」。
