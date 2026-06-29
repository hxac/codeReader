# 配置项与调优：UdtConfiguration

## 1. 本讲目标

学完本讲，你应该能够：

- 看着 `UdtConfiguration` 的 11 个字段，立刻说出每个字段的**类型、默认值、影响哪段代码路径**。
- 理解 `Default` 实现里这些「魔术数字」（1500、256000、81920、8 MB、10s…）分别代表什么、为什么这么取。
- 区分三类参数：**缓冲类**（mss / flight_flag_size / 收发缓冲 / UDP 缓冲）、**多路复用类**（udp_reuse_port / reuse_mux / rendezvous）、**关闭与排队类**（accept_queue_size / linger_timeout），并知道调它们分别会改变什么行为。
- 自己写出一个显式配置 `UdtConfiguration` 的程序，并能精确指出每个字段在源码里被读取的位置（文件:行号）。

本讲承接 u1-l4（公共 API 与配置的导出边界）和 u2-l1 / u2-l2（客户端 connect、服务端 bind/accept），不再重复「怎么连、怎么收发」，而是把焦点收敛到**那一份 `Option<UdtConfiguration>` 到底拧动了哪些底层旋钮**。

## 2. 前置知识

在进入字段细节前，先用一句话回顾几个本讲要用到的概念（详细版见 u1-l4、u2-l2）：

- **MSS（Maximum Segment Size）**：单个 UDT 数据包（含包头）的最大字节数。UDT 跑在 UDP 之上，所以 MSS 通常贴近网络 MTU，避免 IP 分片。
- **在途窗口（flight window / congestion window）**：发送方已经发出、但尚未被对端 ACK 确认的包数量上限。它直接决定「一次能往网络里灌多少数据」。
- **Multiplexer（多路复用器）**：tokio-udt 用**一个 UDP socket** 服务**多个 UdtSocket**，这个共享的 UDP socket 加上它的收发 worker，就是一个 multiplexer。多个 socket 能否共用同一个 multiplexer，由配置决定。
- **SO_REUSEPORT**：一个内核 socket 选项，允许同主机上多个 UDP socket 绑定**同一个端口**，并由内核把入站包在它们之间做负载均衡。
- **Linger（逗留）**：连接 `close()` 时，是否先等发送缓冲里的数据排空再真正关闭。

如果你还不清楚「配置从哪个入口喂进去」，记住一句结论即可：客户端 `UdtConnection::connect` 和服务端 `UdtListener::bind` 都接受 `config: Option<UdtConfiguration>`；传 `None` 就走 `Default`，传 `Some(cfg)` 就用你的值。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，但会顺着各字段的使用处跳到另外几个文件：

| 文件 | 在本讲的作用 |
|------|--------------|
| [src/configuration.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs) | **主角**。`UdtConfiguration` 结构体定义、字段注释、`Default` 实现、默认常量全在这里。 |
| [src/multiplexer.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs) | UDP socket 创建处：`udp_*_buf_size`、`udp_reuse_port` 在此被读取；`reuse_mux`、`mss` 决定 multiplexer 的复用与分片。 |
| [src/udt.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs) | 全局引擎：`accept_queue_size` 在此卡排队上限；`reuse_mux` 在此决定是否复用已有 mux；握手时回写 `mss`/`flight_flag_size`。 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | socket 核心：收发缓冲按 `snd_buf_size`/`rcv_buf_size` 创建；握手协商 `mss`；`close()` 按 `linger_timeout` 逗留。 |
| [src/listener.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs) | `rendezvous` 在此被检查：会合模式下 `bind`/`accept` 直接报 `Unsupported`。 |

## 4. 核心概念与源码讲解

### 4.1 配置的整体形态：字段总览与 Default

#### 4.1.1 概念说明

`UdtConfiguration` 是一个**纯数据结构**：11 个 `pub` 字段，没有方法逻辑（唯一的 `udt_version()` 只是返回常量 4）。它的设计哲学是「**所有调优旋钮集中在一个结构体里，构造时一次性给出，之后只读**」。

这意味着两件事：

1. 配置是**在连接/监听创建时**传入的，不是运行中动态改的。每个 `UdtSocket` 内部都用 `RwLock<UdtConfiguration>` 持有一份副本（见 [socket.rs:122](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L122)），但真正会被「写回」的只有 `mss` 和 `flight_flag_size` 两个字段（握手协商结果），其余字段一经构造基本不变。
2. 字段之间**有量纲差异**：有的以「字节」为单位（mss、udp 缓冲），有的以「包数」为单位（flight_flag_size、收发缓冲、accept 队列），有的以「时间」为单位（linger_timeout），有的只是开关（三个 bool）。混淆单位是最常见的调参错误。

#### 4.1.2 核心流程

配置的生命周期是：

```text
UdtConfiguration::default()        # 或用户手写
        │
        ▼  经 connect / bind 的 config: Option<UdtConfiguration>
Udt::new_socket(config)            # 存进 socket.configuration (RwLock)
        │
        ├──► UdtSocket::new:    snd_buf_size / rcv_buf_size → 收发缓冲容量
        ├──► UdtMultiplexer::new/bind:
        │       udp_snd_buf_size / udp_rcv_buf_size / udp_reuse_port → UDP socket 属性
        │       reuse_mux / mss → mux 是否复用 + 分片大小
        ├──► 握手协商:           mss / flight_flag_size 取双方较小值并写回
        ├──► new_connection:     accept_queue_size → 卡排队上限
        └──► close:              linger_timeout → 逗留时长
```

#### 4.1.3 源码精读

先看结构体本身和默认常量。

[configuration.rs:3-6 — 四个默认常量](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L3-L6)：

```rust
const DEFAULT_MSS: u32 = 1500;
const DEFAULT_UDT_BUF_SIZE: u32 = 81920;
const DEFAULT_UDP_BUF_SIZE: usize = 8_000_000;
const UDT_VERSION: u32 = 4;
```

- `1500` 是以太网典型 MTU，作为 MSS 默认值最稳妥。
- `81920` 个包作为发送缓冲，`81920 * 2 = 163840` 作为接收缓冲（接收缓冲默认比发送大一倍，给乱序重组留余量）。
- `8_000_000` 字节（8 MB）作为 UDP 收发缓冲请求值。

[configuration.rs:8-48 — 结构体与字段注释](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L8-L48)：每个字段都带有解释性 doc 注释，是这份「调优手册」最权威的参考。建议你打开链接通读一遍注释，本讲后续就是对它们的逐条展开。

[configuration.rs:56-72 — Default 实现](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L56-L72)：

```rust
impl Default for UdtConfiguration {
    fn default() -> Self {
        Self {
            mss: DEFAULT_MSS,
            flight_flag_size: 256000,
            snd_buf_size: DEFAULT_UDT_BUF_SIZE,
            rcv_buf_size: DEFAULT_UDT_BUF_SIZE * 2,
            udp_snd_buf_size: DEFAULT_UDP_BUF_SIZE,
            udp_rcv_buf_size: DEFAULT_UDP_BUF_SIZE,
            udp_reuse_port: false,
            linger_timeout: Some(Duration::from_secs(10)),
            reuse_mux: true,
            rendezvous: false,
            accept_queue_size: 1000,
        }
    }
}
```

把默认值汇总成一张「调优速查表」：

| 字段 | 类型 | 默认值 | 单位 | 影响层面 |
|------|------|--------|------|----------|
| `mss` | u32 | 1500 | 字节 | 包格式 / 缓冲分片 |
| `flight_flag_size` | u32 | 256000 | 包数 | 拥塞/流控上限窗口 |
| `snd_buf_size` | u32 | 81920 | 包数 | 发送缓冲容量 |
| `rcv_buf_size` | u32 | 163840 | 包数 | 接收缓冲容量 + 流控窗口 |
| `udp_snd_buf_size` | usize | 8 000 000 | 字节 | UDP socket 发送缓冲 |
| `udp_rcv_buf_size` | usize | 8 000 000 | 字节 | UDP socket 接收缓冲 |
| `udp_reuse_port` | bool | false | 开关 | SO_REUSEPORT |
| `reuse_mux` | bool | true | 开关 | 同端口复用 multiplexer |
| `rendezvous` | bool | false | 开关 | 会合模式（**未实现**） |
| `accept_queue_size` | usize | 1000 | 连接数 | listener 排队上限 |
| `linger_timeout` | Option\<Duration\> | Some(10s) | 时间 | close 逗留时长 |

这张表是本讲的「主页」，下面三个小节分别按「缓冲 / 多路复用 / 关闭排队」三组展开。

#### 4.1.4 代码实践

实践目标：亲手构造一个非默认配置，确认它能编译并正确流入 `bind`。

操作步骤：

1. 在仓库根目录新建 `examples/config_demo.rs`（仓库目前没有 `examples/` 目录，Cargo 会自动发现它；若无写权限，可对照 `src/bin/udt_receiver.rs` 阅读理解）。下面是**示例代码**：

   ```rust
   use std::net::Ipv4Addr;
   use std::time::Duration;
   use tokio_udt::{UdtConfiguration, UdtListener};

   #[tokio::main]
   async fn main() -> tokio::io::Result<()> {
       let mut cfg = UdtConfiguration::default(); // 先拿默认值
       cfg.mss = 1400;                            // 再覆盖个别字段
       cfg.linger_timeout = Some(Duration::from_secs(5));
       println!("cfg = {:#?}", cfg);              // 借助 derive(Debug) 打印全量

       let listener = UdtListener::bind(
           (Ipv4Addr::UNSPECIFIED, 9000).into(),
           Some(cfg),                             // 把自定义配置喂进去
       ).await?;
       println!("listening, mss at socket = {}",
           listener.socket.configuration.read().unwrap().mss);
       Ok(())
   }
   ```

   > 注意：`UdtListener` 内部的 `socket` 字段及其 `configuration` 是否对外可见取决于版本。上面这行读 `mss` 的代码若编译不过，就只用 `println!("{:#?}", cfg)` 观察构造结果即可。

2. 运行 `cargo run --example config_demo`（若你用了 examples 方式）。

需要观察的现象：

- 打印的 `cfg` 里 `mss` 应为 1400、`linger_timeout` 应为 `5s`，其余字段仍是默认值（`udp_reuse_port: false`、`reuse_mux: true`、`accept_queue_size: 1000` 等）。这验证了「default + 局部覆盖」的用法。

预期结果：你能用 `UdtConfiguration::default()` 作起点、按需改几个字段，再把 `Some(cfg)` 传给 `bind`/`connect`。若本地无法编译运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接给 `UdtConfiguration` 一个 `new(mss, buf, ...)` 构造函数，而是用 11 个 `pub` 字段 + `Default`？

> **答案**：11 个字段里绝大多数场景都用默认值即可，只有少数几个需要调。用 `pub` 字段 + `Default`，调用方写 `let mut cfg = UdtConfiguration::default(); cfg.mss = 1400;` 只覆盖关心的字段，**不需要为每个字段都填一个参数**，也避免了「构造函数参数顺序记错」这类错误。这是 Rust 生态里「builder-lite」的常见写法。

**练习 2**：把 `cfg.rcv_buf_size` 改成比 `flight_flag_size` 还小，会出什么问题？结合字段注释回答。

> **答案**：[configuration.rs:16](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L16) 的注释明确提醒「应把 `flight_flag_size` 设为不小于 `rcv_buf_size`」。原因是握手时会取 `min(rcv_buf_size, flight_flag_size)` 作为流控窗口（见 4.2.3），`rcv_buf_size` 偏小会把窗口压低，反而限制吞吐。

---

### 4.2 缓冲类参数：mss / flight_flag_size / 收发缓冲 / UDP 缓冲

#### 4.2.1 概念说明

这一组参数都围绕「**一个包多大、一次能存多少包、底层 UDP 缓冲多大**」三个层次。理解它们的关键是分清**三个缓冲层级**：

```text
应用层数据 ──► [SndBuffer: snd_buf_size 个包]   ──► UDT 发送逻辑(受 flight_flag_size/拥塞窗口限流)
                                                        │ 按 mss 分片
                                                        ▼
                                                  [UDP socket: udp_snd_buf_size 字节] ──► 网络
```

接收侧镜像对称：UDP socket 收包 → `RcvBuffer`（`rcv_buf_size` 个包）重组 → 应用读取。

- `mss` 决定**分片粒度**：一段应用消息会被切成多少个 UDT 数据包，每个包多大。
- `snd_buf_size` / `rcv_buf_size` 是**应用与网络之间的弹性蓄水池**，单位是「包数」。发送缓冲既存「还没轮到发的」，也存「发了但可能要重传的」。
- `flight_flag_size` 是**往网络里灌数据的闸门**，单位是「在途包数」。
- `udp_snd_buf_size` / `udp_rcv_buf_size` 是**操作系统内核**给那个底层 UDP socket 的缓冲，单位是「字节」，且会被内核上限 `net.core.wmem_max` / `rmem_max` 截断。

#### 4.2.2 核心流程

mss 的关键行为是**握手时双方协商取较小值**，避免一端发的包另一端处理不了：

```text
本端 mss 配置  ┐
               ├─► 握手包里带 max_packet_size ─► 双方各自 min(自己, 对方) ─► 写回 configuration.mss
对端 mss 配置  ┘                                  (同时 flight_flag_size/max_window_size 也对齐)
```

随后 `mss` 会在多处被使用：计算单包有效载荷（`mss - 包头 - IP/UDP 头`）、给接收队列的批量收包缓冲分块、初始化速率控制等。

#### 4.2.3 源码精读

**收发缓冲容量**在 socket 创建时就固定下来：

[socket.rs:109-113 — 按 buf_size 创建收发缓冲](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L109-L113)：

```rust
snd_buffer: Mutex::new(SndBuffer::new(configuration.snd_buf_size)),
rcv_buffer: Mutex::new(RcvBuffer::new(
    configuration.rcv_buf_size,
    initial_seq_number,
)),
```

`SndBuffer::new` / `RcvBuffer::new` 接收的正是「包数」容量。

**UDP socket 缓冲与 SO_REUSEPORT** 在 multiplexer 创建底层 UDP socket 时设置：

[multiplexer.rs:41-46 — 用 socket2 配置 UDP socket](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L41-L46)：

```rust
let socket = Socket::new(domain, Type::DGRAM, None)?;
socket.set_recv_buffer_size(config.udp_rcv_buf_size)?;
socket.set_send_buffer_size(config.udp_snd_buf_size)?;
socket.set_reuse_port(config.udp_reuse_port)?;
socket.set_nonblocking(true)?;
socket.bind(&bind_addr.into())?;
```

这里 `set_recv_buffer_size` / `set_send_buffer_size` 对应内核的 `SO_RCVBUF` / `SO_SNDBUF`，注释 [configuration.rs:22-29](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L22-L29) 提醒：实际值受 `net.core.rmem_max` / `wmem_max` 限制。

**握手协商 mss / flight_flag_size**：listener 在回送握手时上报自己的 `mss` 和 `flight_flag_size`：

[udt.rs:122-123 — 握手响应写入 max_packet_size / max_window_size](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L122-L123)：

```rust
hs.max_packet_size = configuration.mss;
hs.max_window_size = configuration.flight_flag_size;
```

客户端在 `connect_on_handshake` 里取较小值并写回：

[socket.rs:177-185 — mss 取小 + 流控窗口取 min(rcv_buf_size, flight_flag_size)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L177-L185)：

```rust
if hs.max_packet_size > configuration.mss {
    hs.max_packet_size = configuration.mss;
} else {
    configuration.mss = hs.max_packet_size;          // 取双方较小值写回
}
self.flow.write().unwrap().flow_window_size = hs.max_window_size;
hs.max_window_size =
    std::cmp::min(configuration.rcv_buf_size, configuration.flight_flag_size);
```

注意最后一行：本端**上报**给对端的窗口是 `min(rcv_buf_size, flight_flag_size)`——这正是 4.1.5 练习 2 里「`rcv_buf_size` 太小会压低窗口」的来源。服务端在握手收尾时则反向把对端的 `mss`/`flight_flag_size` 写进自己的配置：

[socket.rs:470-472 — post connect 写回协商结果](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L470-L472)：

```rust
let mut configuration = self.configuration.write().unwrap();
configuration.mss = hs.max_packet_size;
configuration.flight_flag_size = hs.max_window_size;
```

**单包有效载荷**由 mss 减去包头与 IP/UDP 头得到（IPv6 多减 40，IPv4 多减 28）：

[socket.rs:762-763 — get_max_payload_size](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L762-L763)：

```rust
Some(IpAddr::V6(_)) => configuration.mss - 40 - UDT_DATA_HEADER_SIZE as u32,
_ => configuration.mss - 28 - UDT_DATA_HEADER_SIZE as u32,
```

**mss 还参与接收队列分块**（按 mss 切批量收包缓冲）与**速率控制初始化**：

- [rcv_queue.rs:138](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L138) 处 `vec![0_u8; self.mss as usize * 100]` 用 mss 估算「一次最多收 100 个包」的缓冲。
- [rate_control.rs:65](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L65) 处 `self.mss = mss as f64` 把 mss 存进速率控制器，用于后续拥塞窗口增长公式（速率控制细节见 u7 单元）。

#### 4.2.4 代码实践

实践目标：通过修改 `mss`，观察它如何同时影响「握手上报值」和「接收批量缓冲」。

操作步骤：

1. 复制 `src/bin/udt_receiver.rs` 的思路，写一个 receiver，把 `mss` 显式设为 `1316`（一个常用于视频流的值，避开 IP/UDP/UDT 头后载荷为整数）：

   ```rust
   // 示例代码
   let mut cfg = UdtConfiguration::default();
   cfg.mss = 1316;
   let listener = UdtListener::bind((Ipv4Addr::UNSPECIFIED, 9000).into(), Some(cfg)).await?;
   ```

2. 在 [udt.rs:122](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L122) 和 [rcv_queue.rs:138](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L138) 各加一行 `println!`（仅用于学习，不要提交），打印协商前后的 `max_packet_size` 和 `self.mss * 100`。

需要观察的现象：

- 握手阶段 listener 上报的 `max_packet_size` 应为 1316；若客户端用默认 1500，协商后双方 `mss` 都变成 1316（取较小值）。
- 接收队列批量缓冲大小变为 `1316 * 100` 字节。

预期结果：mss 改小后，单包载荷变小、相同数据量需要更多包；批量收包缓冲也随之缩小。这一现象依赖真实网络往返，**若无法本地起一对收发端，标注「待本地验证」**，至少完成「在源码里定位到 mss 被使用的所有位置」这一阅读目标。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `udp_snd_buf_size` 设了 8 MB，实际生效值可能远小于它？

> **答案**：[configuration.rs:22-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L22-L25) 注释说明，这个值是「请求值」，内核会把它截断到 `net.core.wmem_max`。可用 `sysctl net.core.wmem_max` 查看上限；Linux 上内核通常还会把请求值翻倍（预留开销），但最终仍受 max 限制。

**练习 2**：`snd_buf_size` 和 `flight_flag_size` 都以「包数」为单位，它们是同一个东西吗？

> **答案**：不是。`snd_buf_size` 是**发送缓冲的容量**（能暂存多少待发/待重传的包），是一个存储上限；`flight_flag_size` 是**拥塞/流控的最大在途窗口**，限制「同时飞在网络里未被 ACK 的包数」。前者管「放得下」，后者管「放得出」。一个连接可以有巨大的发送缓冲但很小的拥塞窗口（数据堆在缓冲里发不出去），反之窗口再大也不能超过缓冲容量。

---

### 4.3 多路复用类参数：udp_reuse_port / reuse_mux / rendezvous

#### 4.3.1 概念说明

这一组三个 bool 决定「**底层 UDP socket 怎么共享、能不能共享、要不要会合**」。它们最容易混淆，但理解后是 tokio-udt 做多客户端高吞吐调优的关键。

先厘清两个层面：

- **应用层复用（reuse_mux）**：tokio-udt 自己的机制。多个 `UdtSocket` 是否**共用同一个 multiplexer（即同一个 UDP socket + 同一对收发 worker）**。
- **内核层复用（udp_reuse_port）**：操作系统机制。是否允许**多个不同的 UDP socket** 绑定同一个端口，并由内核做负载均衡。

`reuse_mux=true`（默认）时，第二个绑定同端口的 socket 会**复用第一个 mux**，于是只有**一个** UDP socket 在监听该端口——没有内核级负载均衡。若你想让多个 listener/worker 各自拥有独立 UDP socket、由内核把入站包打散到不同线程（这对多客户端高吞吐很有用），就需要 `reuse_mux=false` **并且** `udp_reuse_port=true`，否则第二个 `bind` 会因端口冲突（`EADDRINUSE`）失败。

字段注释 [configuration.rs:35-40](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L35-L40) 正是这层意思：`reuse_mux` 是「复用既有 mux」的开关，并提示「多客户端最优吞吐可改用 `udp_reuse_port`」。

`rendezvous`（会合模式）则是另一种连接建立方式：双方同时主动发起握手（没有严格的 client/server 之分）。但 [configuration.rs:42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L42) 标注 **NOT IMPLEMENTED**，目前把它设为 `true` 只会让 `bind`/`accept` 报错。

#### 4.3.2 核心流程

绑定一个 socket 时，`update_mux` 决定「复用还是新建」：

```text
update_mux(socket, bind_addr):
  if reuse_mux 且 bind_addr 有端口:
      遍历已有 mux:
          if mux.reusable && mux.port==端口 && mux.mss==本端mss:
              复用该 mux → return          # 共用同一个 UDP socket
  否则 / 没匹配到:
      新建一个 mux（新建一个 UDP socket）   # 走 new_udp_socket，应用 udp_reuse_port
```

匹配条件有三个：mux 本身 `reusable`（即创建它的配置 `reuse_mux=true`）、端口相同、mss 相同。三者全满足才复用。

#### 4.3.3 源码精读

**reuse_mux 的复用判定**：

[udt.rs:196-208 — 同端口+mss 时复用已有 mux](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196-L208)：

```rust
if socket.configuration.read().unwrap().reuse_mux {
    if let Some(bind_addr) = bind_addr {
        let port = bind_addr.port();
        if port > 0 {
            for mux in self.multiplexers.values() {
                let socket_mss = socket.configuration.read().unwrap().mss;
                if mux.reusable && mux.port == port && mux.mss == socket_mss {
                    socket.set_multiplexer(mux);
                    return Ok(());
                }
            }
        }
    }
}
// A new multiplexer is needed ...
```

**mux 是否可被复用**由创建它的配置 `reuse_mux` 决定，写入 `mux.reusable` 字段：

[multiplexer.rs:64-65 与 89-90 — reusable 与 mss 写入 mux](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L64-L65)（`new` 与 `bind` 两个构造点都一样）：

```rust
reusable: config.reuse_mux,
mss: config.mss,
```

**udp_reuse_port 落到 UDP socket**（见 4.2.3 的 [multiplexer.rs:44](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L44)）：`socket.set_reuse_port(config.udp_reuse_port)`。只有新建 mux 时才会走到这里，所以「复用已有 mux」时这个开关对新 socket 无意义。

**rendezvous 的拒绝**：

[listener.rs:20-25 — bind 时拒绝会合模式](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L20-L25)：

```rust
if socket.configuration.read().unwrap().rendezvous {
    return Err(Error::new(
        ErrorKind::Unsupported,
        "listen is not supported in rendezvous connection setup",
    ));
}
```

[listener.rs:50-55 — accept 时同样拒绝](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L50-L55) 重复同一检查。

#### 4.3.4 代码实践

实践目标（本讲核心实践之一）：动手设置 `udp_reuse_port` 与 `reuse_mux`，并在源码里定位它们的读取处。

操作步骤：

1. 写一个 listener 配置，**显式**关掉 mux 复用、打开端口复用（这是「多 listener 共享同端口、内核负载均衡」的典型组合）：

   ```rust
   // 示例代码
   let mut cfg = UdtConfiguration::default();
   cfg.udp_reuse_port = true;   // 允许多个 UDP socket 绑同一端口
   cfg.reuse_mux = false;       // 不复用既有 mux → 每个 bind 各起一个 UDP socket
   let listener = UdtListener::bind((Ipv4Addr::UNSPECIFIED, 9000).into(), Some(cfg)).await?;
   ```

2. 在仓库里搜索这两个字段名的使用处，**记录文件:行号**并填写下表（答案见下方，先自己搜）：

   | 字段 | 读取处（文件:行号） | 作用 |
   |------|----------------------|------|
   | `udp_reuse_port` | ? | ? |
   | `reuse_mux` | ?（两处：判定 + 写入） | ? |

3. 对比：把 `reuse_mux` 改回 `true`（默认），再启动第二个绑定 9000 的 listener，思考会发生什么。

需要观察的现象：

- `udp_reuse_port=true` + `reuse_mux=false` 时，可以成功 `bind` 第二个 9000 端口的 listener（内核 SO_REUSEPORT 允许）。
- `reuse_mux=true` 时，第二个 bind 会复用第一个 mux，不会新建 UDP socket，也就谈不上内核负载均衡。

预期结果（参考答案）：

| 字段 | 读取处 | 作用 |
|------|--------|------|
| `udp_reuse_port` | [multiplexer.rs:44](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L44) | 新建 UDP socket 时设置 `SO_REUSEPORT` |
| `reuse_mux` | [udt.rs:196](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196)（判定是否复用）+ [multiplexer.rs:64](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L64) / [multiplexer.rs:89](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L89)（写入 `mux.reusable`） | 决定是否复用同端口已有 mux |

多 listener 同端口的真实行为依赖内核与 `SO_REUSEPORT` 支持，**若本地无法起两个进程验证，标注「待本地验证」**。

#### 4.3.5 小练习与答案

**练习 1**：默认配置（`reuse_mux=true, udp_reuse_port=false`）下，在同一进程里连续 `bind` 两个 listener 到同一端口，会怎样？

> **答案**：第二个 `bind` 在 [udt.rs:196-208](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196-L208) 命中复用分支：第一个 mux 满足 `reusable && port 相同 && mss 相同`，于是第二个 socket **复用同一个 UDP socket**，不会报端口冲突。结果是两个 listener 共享一个 mux、一个 UDP socket——没有内核级负载均衡。

**练习 2**：为什么注释说「多客户端最优吞吐可改用 `udp_reuse_port`」？

> **答案**：单 UDP socket 的收包最终由一个线程的 worker 处理，多客户端高并发时这会成为瓶颈。打开 `udp_reuse_port`（并配合 `reuse_mux=false`）后，每个 worker 拥有独立的 UDP socket 绑同一端口，内核把入站包**按四元组哈希分散**到不同 socket/线程，从而真正并行处理多客户端流量。

**练习 3**：把 `rendezvous` 设为 `true` 再调用 `UdtListener::bind`，会返回什么？

> **答案**：返回 `Err(ErrorKind::Unsupported)`，见 [listener.rs:20-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L20-L25)。会合模式尚未实现。

---

### 4.4 关闭与排队类参数：linger_timeout / accept_queue_size

#### 4.4.1 概念说明

剩下两个字段分别管「**连接怎么关**」和「**服务端能攒多少未取走的连接**」：

- `linger_timeout: Option<Duration>`（默认 `Some(10s)`）：`close()` 时，若发送缓冲还有数据没发完，最多等这么久让它排空，再真正关闭。
- `accept_queue_size: usize`（默认 `1000`）：listener 已经握手完成、但应用还没调用 `accept()` 取走的连接，最多能排多少个。

注意 `linger_timeout` 是 `Option`：`None` 表示「不设置」，`Some(d)` 表示「显式等 d」。本讲的练习会厘清它在代码里和 `Some(Duration::ZERO)` 的微妙关系。

#### 4.4.2 核心流程

`close()` 的逗留逻辑：

```text
close():
  若已是 Closed/Closing → 直接返回
  读 linger_timeout，None 当作 0
  while (状态==Connected 且 发送缓冲非空 且 已等待 < linger_timeout):
      wait_for_next_ack_or_empty_snd_buffer()   # 等下一个 ACK 或缓冲清空
  从 snd_queue 移除自己；若自己是 listener，清掉 mux.listener
  若仍是 Connected → 发 Shutdown 包 → 置 Closing
```

`accept_queue_size` 的卡点：

```text
new_connection(listener, 握手包):
  if listener.queued_sockets.len() >= accept_queue_size:
      return Err("Too many queued sockets")     # 排队已满，拒绝新连接
  否则创建新 socket、握手、插入 queued_sockets、notify accept
```

#### 4.4.3 源码精读

**close 与 linger**：

[socket.rs:1153-1171 — close 的逗留循环](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1153-L1171)：

```rust
pub async fn close(&self) {
    let status = self.status();
    if status == UdtStatus::Closed || status == UdtStatus::Closing {
        return;
    }
    let now = Instant::now();
    let linger_timeout = self
        .configuration
        .read()
        .unwrap()
        .linger_timeout
        .unwrap_or(Duration::ZERO);          // None → 0

    while self.status() == UdtStatus::Connected
        && !self.snd_buffer_is_empty()
        && now.elapsed() < linger_timeout
    {
        self.wait_for_next_ack_or_empty_snd_buffer().await;
    }
    // ...随后从 snd_queue 移除、发 Shutdown、置 Closing
}
```

关键是 [socket.rs:1163](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1163) 的 `.unwrap_or(Duration::ZERO)`：`None` 被转成零时长，于是 `while` 条件 `now.elapsed() < linger_timeout` 立即不成立，**循环一次都不执行**，等于「不等排空直接关」。

**accept 排队上限**：

[udt.rs:144-147 — accept_queue_size 卡点](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L144-L147)：

```rust
let config = listener_socket.configuration.read().unwrap().clone();
if listener_socket.queued_sockets.read().await.len() >= config.accept_queue_size {
    return Err(Error::new(ErrorKind::Other, "Too many queued sockets"));
}
```

注意它读的是 **listener socket 自己的配置副本**（握手时克隆给新连接，见 [udt.rs:144](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L144)），所以这个上限由 `UdtListener::bind` 时传入的配置决定。

#### 4.4.4 代码实践

实践目标：显式设置 `accept_queue_size` 与 `linger_timeout`，并定位它们的代码路径。

操作步骤：

1. 写一个服务端配置，把队列调小、逗留调短，便于观察：

   ```rust
   // 示例代码
   use std::time::Duration;
   let mut cfg = UdtConfiguration::default();
   cfg.accept_queue_size = 5;                          // 排队上限设为 5
   cfg.linger_timeout = Some(Duration::from_millis(500));
   let listener = UdtListener::bind((Ipv4Addr::UNSPECIFIED, 9000).into(), Some(cfg)).await?;
   ```

2. 在仓库搜索这两个字段名，记录使用处：

   | 字段 | 读取处（文件:行号） | 作用 |
   |------|----------------------|------|
   | `accept_queue_size` | ? | ? |
   | `linger_timeout` | ? | ? |

3. （可选压测）用 `src/bin/udt_sender.rs` 的思路，并发发起超过 5 个连接、且不 `accept`，观察第 6 个是否被拒。

需要观察的现象：

- `accept_queue_size=5` 时，未取走连接超过 5 个后，新连接握手阶段就被拒（`Too many queued sockets`）。
- `linger_timeout=500ms` 时，`close()` 最多等 500ms 让发送缓冲排空。

预期结果（参考答案）：

| 字段 | 读取处 | 作用 |
|------|--------|------|
| `accept_queue_size` | [udt.rs:145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145) | 判断 `queued_sockets.len()` 是否已达上限 |
| `linger_timeout` | [socket.rs:1163](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1163) | `close()` 逗留循环的上限，`None`→0 |

并发拒连与逗留计时都依赖真实多连接环境，**若无法本地压测，标注「待本地验证」**。

#### 4.4.5 小练习与答案

**练习 1**：`linger_timeout` 设为 `None` 与 `Some(Duration::ZERO)`，行为有区别吗？

> **答案**：运行效果等价。[socket.rs:1163](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1163) 用 `unwrap_or(Duration::ZERO)` 把 `None` 转成零时长，随后 [socket.rs:1166-1168](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1166-L1168) 的 `while ... && now.elapsed() < linger_timeout` 在零时长下立即为假，循环不执行。语义上 `None` 表「未设置/不等待」，`Some(0)` 表「显式等 0 秒」，结果都是「不等排空直接关」。

**练习 2**：`accept_queue_size` 是按「每秒新建连接数」还是「累计未取走连接数」限制？

> **答案**：是「**当前尚未被 `accept()` 取走的连接数**」，即 `queued_sockets` 集合的大小（[udt.rs:145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145)）。只要应用及时 `accept` 把连接取走，队列就会腾出位置，新连接仍能进入；它不是速率限制，而是「积压上限」。

---

## 5. 综合实践

把本讲四个最小模块串起来：**自己拼一份「多客户端高吞吐服务端」的配置，并完整记录每个字段落到了哪段代码**。

任务：

1. 构造一个 `UdtConfiguration`，至少显式设置这 4 个字段，并为每一项写出你**期望**的行为：
   - `udp_reuse_port = true`
   - `reuse_mux = false`
   - `accept_queue_size = 200`
   - `linger_timeout = Some(Duration::from_secs(3))`

2. 用它启动一个 `UdtListener`（参照 README 服务端示例 + u2-l2）。可以借助 `derive(Debug)` 打印配置确认。

3. 填写下面这张「配置 → 代码路径」对照表（这是本讲规格要求的实践任务，建议先自己搜再对答案）：

   | 字段 | 读取处（文件:行号） | 该处代码做什么 |
   |------|----------------------|----------------|
   | `udp_reuse_port` |  |  |
   | `reuse_mux` |  |  |
   | `accept_queue_size` |  |  |
   | `linger_timeout` |  |  |

4. 进阶思考（选做）：若你想在同一台机器上跑 **N 个独立 worker 进程**共同监听 9000 端口以提升多客户端吞吐，4 个字段应分别怎么设？为什么 `reuse_mux` 必须为 `false`？

参考答案（对照表）：

| 字段 | 读取处 | 该处代码做什么 |
|------|--------|----------------|
| `udp_reuse_port` | [multiplexer.rs:44](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L44) | 新建 UDP socket 时设置 `SO_REUSEPORT` |
| `reuse_mux` | [udt.rs:196](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196)（复用判定）+ [multiplexer.rs:64](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L64)/[89](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L89)（写入 `reusable`） | 决定是否复用同端口已有 mux |
| `accept_queue_size` | [udt.rs:145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145) | 判断排队连接数是否达上限 |
| `linger_timeout` | [socket.rs:1163](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1163) | `close()` 逗留上限，`None` 转 0 |

进阶答案：N 个独立 worker 进程要各自拥有独立 UDP socket 才能被内核 `SO_REUSEPORT` 分流，因此 `reuse_mux=false`（否则进程内会复用同一个 mux，跨进程也无意义）、`udp_reuse_port=true`（允许同端口多 socket）。`accept_queue_size` 与 `linger_timeout` 按单 worker 的承受能力设。

## 6. 本讲小结

- `UdtConfiguration` 是 11 个 `pub` 字段的纯数据结构 + 一个 `Default`，配置在 `connect`/`bind` 时经 `Option<UdtConfiguration>` 一次性传入，存进每个 socket 的 `RwLock<UdtConfiguration>`。
- 默认值的「魔术数字」都有出处：mss=1500（以太网 MTU）、收发缓冲按包数计（发送 81920 / 接收 163840）、UDP 缓冲 8 MB（受内核 `rmem_max`/`wmem_max` 截断）、linger 10s、accept 队列 1000。
- **缓冲类**：mss 握手时双方取较小值并写回（[socket.rs:177-185](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L177-L185)）；流控窗口取 `min(rcv_buf_size, flight_flag_size)`；UDP 缓冲与 SO_REUSEPORT 在 [multiplexer.rs:42-44](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L42-L44) 设置。
- **多路复用类**：`reuse_mux` 决定是否复用同端口已有 mux（[udt.rs:196-208](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L196-L208)），`udp_reuse_port` 决定是否允许多 UDP socket 共享端口；多客户端高吞吐的典型组合是 `reuse_mux=false` + `udp_reuse_port=true`；`rendezvous` 未实现，设 true 会被 `bind`/`accept` 拒绝。
- **关闭排队类**：`linger_timeout` 控制 `close()` 逗留（[socket.rs:1153-1171](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1153-L1171)，`None`→0 即不等待）；`accept_queue_size` 是 listener 未取走连接的积压上限（[udt.rs:145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145)）。
- 调参时最常见的错误是**混淆单位**（字节 vs 包数 vs 时间）和**忽视字段间的耦合**（如 `rcv_buf_size` 过小会压低流控窗口）。

## 7. 下一步学习建议

本讲把 `UdtConfiguration` 的每个旋钮都连到了具体代码路径，但有些路径只点到了「这里读了配置」，没展开它后续触发的机制。建议接下来：

- 想搞清楚 **mss / flight_flag_size 协商之后**，窗口到底怎么影响发送节奏 → 进入 **u5（发送/接收数据通路）** 和 **u6（可靠性与发送主流程）**，重点读 `next_data_packets` 里拥塞窗口与 flow window 的限流。
- 想搞清楚 **reuse_mux / 多路复用** 的全貌 → 进入 **u3-l3（UdtMultiplexer）**，读 `UdtMultiplexer::run` 启动的收发两个 worker。
- 想搞清楚 **linger / close / accept 队列** 背后的连接生命周期 → 进入 **u8-l2（关闭、linger 与垃圾回收）** 和 **u8-l1（握手与 SYN cookie）**。
- 若你对拥塞控制的数学（慢启动、AIMD）更感兴趣，可直接跳到 **u7（拥塞控制算法与定时器）**，那里会用到本讲提到的 mss、flight_flag_size 在速率控制器里的真实计算。
