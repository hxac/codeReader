# 目录结构与模块地图

## 1. 本讲目标

本讲是一张「全项目地图」。读完本讲，你应该能够：

- 看懂 [`src/lib.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs) 里所有 `mod` 声明，并说出它们各自对应磁盘上的哪个文件。
- 理解 `queue/`、`state/` 这两个「子目录模块」的组织方式，以及 `mod.rs` 在其中扮演的角色。
- 把全部 26 个源码文件归类到五条主线：**入口 / 协议格式 / 数据通路 / 可靠性 / 拥塞控制**，从而在后续读任何一篇讲义前，都知道该文件属于「哪一层」。

本讲**不深入任何算法**（重传、ACK、拥塞控制都留到后面的单元），只解决一个问题：**这个 crate 到底由哪些文件组成，它们怎么拼在一起。**

## 2. 前置知识

### 2.1 Rust 的模块系统（最小回顾）

- **crate**：一次编译的单元。tokio-udt 既是一个库 crate（`src/lib.rs` 是入口），又附带两个可执行 binary（`src/bin/` 下）。
- **`mod xxx;`**：声明一个子模块。编译器会按固定规则去找文件：
  - 先找 `src/xxx.rs`；
  - 或 `src/xxx/mod.rs`（子目录形式）。
- **`pub use`**：把某个私有模块里的类型「重新导出」到 crate 根，让外部用户能直接 `use tokio_udt::UdtConnection`。
- **`pub(crate)`**：只在本 crate 内部可见，外部用户看不到。tokio-udt 大量使用它来隐藏实现细节。

### 2.2 承接前两讲

- **u1-l1** 已建立总览：UDT 是建在 UDP 之上的可靠传输协议；tokio-udt 对外只暴露 5 个公共类型，对内声明了 17 个私有子模块。
- **u1-l2** 已说明 `src/bin/` 下的 `.rs` 文件由 Cargo **自动发现**为 binary，不需要在 `lib.rs` 里声明。

本讲就顺着这两条线索，把 17 个私有模块和 2 个子目录**逐个落到文件上**。

## 3. 本讲源码地图

本讲只读三个「门面」文件，它们决定了整个 crate 的骨架：

| 文件 | 作用 |
|------|------|
| [`src/lib.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs) | crate 根：顶层文档注释、17 个 `mod` 声明、5 个 `pub use` 导出。 |
| [`src/queue/mod.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/mod.rs) | `queue` 子目录的入口：声明 4 个收发相关子模块并 `pub(crate) use` 重导出。 |
| [`src/state/mod.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/mod.rs) | `state` 子目录的入口：声明 1 个 `socket_state` 子模块并重导出。 |

辅助理解时还会顺带瞥一眼：[`src/udt.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs)（全局单例）和 [`src/multiplexer.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs)（多路复用器），用来给它们归类。

## 4. 核心概念与源码讲解

### 4.1 lib.rs 模块树：crate 的模块声明与导出

#### 4.1.1 概念说明

`src/lib.rs` 是整个库 crate 的根。它做三件事：

1. 顶部一大段 `/*! ... */` 是 crate 级文档注释，会被 `cargo doc` 渲染成首页（里面就是 README 里的 server / client 示例）。
2. 中间一串 `mod xxx;` 把 17 个内部模块「挂」到 crate 根上——这就是 tokio-udt 的**模块树主干**。
3. 底部 5 个 `pub use` 从这 17 个私有模块里挑出 5 个类型，作为**对外公共 API**。

关键区分：**模块的可见性**（`mod` vs `pub mod`）和**类型的可见性**（`pub` vs `pub(crate)`）是两件事。tokio-udt 的 17 个 `mod` **全部是私有的**（没有 `pub`），它对外只通过 `pub use` 暴露少量类型。这意味着外部用户看不见模块结构，只能用 5 个公共类型；而 crate 内部代码则可以自由访问所有 17 个模块。

#### 4.1.2 核心流程

理解模块树的流程是：

1. 打开 `src/lib.rs`，定位到 `mod` 声明区。
2. 对每个 `mod xxx;`，按规则映射到磁盘文件：`xxx` → `src/xxx.rs`，除非它是个子目录（`queue`、`state`）→ `src/xxx/mod.rs`。
3. 看 `pub use` 区，确认哪些类型是外部可用的。
4. 其余模块都是 `pub(crate)` 的实现细节。

#### 4.1.3 源码精读

**17 个私有模块声明**（[src/lib.rs:68-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L68-L84)）：

```rust
mod ack_window;
mod common;
mod configuration;
mod connection;
mod control_packet;
mod data_packet;
mod flow;
mod listener;
mod loss_list;
mod multiplexer;
mod packet;
mod queue;       // → src/queue/mod.rs（子目录）
mod rate_control;
mod seq_number;
mod socket;
mod state;        // → src/state/mod.rs（子目录）
mod udt;
```

数一下恰好 17 行。其中 `queue` 和 `state` 两个会落到子目录（因为有 `mod.rs`），其余 15 个都直接对应 `src/<名字>.rs`。

**5 个对外公共类型**（[src/lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90)）：

```rust
pub use configuration::UdtConfiguration;
pub use connection::UdtConnection;
pub use listener::UdtListener;
pub use rate_control::RateControl;
pub use seq_number::SeqNumber;
```

这正好印证 u1-l1 的结论：外部用户只能 `use tokio_udt::{UdtConfiguration, UdtConnection, UdtListener, RateControl, SeqNumber}`，其他 12 个模块（`ack_window`、`udt`、`socket`、`packet`……）对用户完全不可见。

**README 上的示例被当作 doctest 跑**（[src/lib.rs:92-93](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L92-L93)）：

```rust
#[cfg(doctest)]
doc_comment::doctest!("../README.md");
```

这条说明项目没有独立的 `examples/` 目录——示例就嵌在 lib.rs 顶部文档（第 10-66 行）和 README 里，通过 `cargo test --doc` 执行。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`mod` 声明 → 磁盘文件」的映射关系，并区分公共 API 与内部模块。

**操作步骤**：

1. 打开 [`src/lib.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs)，数 `mod` 声明的行数，确认是 17 个。
2. 对照仓库 `src/` 目录，为每个 `mod xxx;` 找到对应文件（15 个 `src/xxx.rs` + 2 个子目录）。
3. 运行 `cargo doc --no-deps --open`（可选），在浏览器里看「tokio_udt」条目下**只列出 5 个公共类型**，而看不见 17 个私有模块——直观体会 `pub use` 与私有 `mod` 的边界。

**需要观察的现象**：

- `cargo doc` 生成的文档里，模块列表只有 `UdtConfiguration / UdtConnection / UdtListener / RateControl / SeqNumber`（及它们的方法）。
- 私有模块如 `socket`、`packet`、`udt` 在文档里**找不到**。

**预期结果**：17 个 `mod` 中，只有 5 个对应的类型被 `pub use` 提升为公共 API；其余模块是 `pub(crate)` 实现细节。具体渲染结果「待本地验证」（取决于本机是否安装了文档工具链）。

#### 4.1.5 小练习与答案

**练习 1**：用户在自己的代码里写 `use tokio_udt::packet::UdtPacket;` 会成功吗？为什么？

> **答案**：不会成功。`mod packet;` 是私有的（没有 `pub`），且 `UdtPacket` 本身也是 `pub(crate)`。外部用户根本看不见 `packet` 模块，只能用 5 个 `pub use` 出来的类型。

**练习 2**：为什么 `RateControl` 被设计成公共类型（`pub struct RateControl`），而它所在的模块文件里结构体字段却是私有的？

> **答案**：因为 u1-l2 提到，发送端会通过 `UdtConnection::rate_control()` 拿到一把写锁，**只读地**打印 `pkt_send_period` / `congestion_window_size` 这两个指标。把类型设为 `pub` 是为了让用户能持有这把锁的 guard；而字段私有（靠 `get_*` 方法暴露）是为了防止用户随意改写拥塞控制状态、破坏协议正确性。

**练习 3**：如果把 `mod udt;` 改成 `pub mod udt;`，会发生什么？

> **答案**：编译上 `udt` 模块会变成对外可见，但里面大多数类型（如 `Udt`、`SocketRef`）仍是 `pub(crate)`，所以外部依然用不了——可见性是「逐层收紧」的，外层放开也救不了内层私有。这正好说明 tokio-udt 有意把全局引擎藏起来。

---

### 4.2 queue 子模块：收发队列与缓冲

#### 4.2.1 概念说明

UDT 是个传输协议，**数据通路**（数据怎么从「用户写进来」走到「UDP 发出去」，又怎么从「UDP 收进来」走到「用户读出去」）是最大的一块代码。为了避免 `src/` 下平铺一堆 `xxx_queue.rs`、`xxx_buffer.rs`，作者把它们收进了一个子目录 `queue/`，由 `queue/mod.rs` 统一管理。

`queue/` 下有 4 个文件，正好对应「发送侧 / 接收侧」各两个职责：

- **队列（Queue）**：负责「什么时候发 / 收」，是**调度**与**收发主循环**。
- **缓冲（Buffer）**：负责「数据长什么样」，是**切片、重传、乱序重组**。

| 子模块文件 | 侧 | 角色 |
|-----------|----|----|
| `snd_queue.rs` | 发送 | 定时调度队列（决定何时发包） |
| `snd_buffer.rs` | 发送 | 发送缓冲（把消息切成包、记录可重传块） |
| `rcv_queue.rs` | 接收 | 收包队列（收 UDP 包并按 socket 分发） |
| `rcv_buffer.rs` | 接收 | 接收缓冲（乱序重组、按序读出） |

`mod.rs` 只做两件事：声明这 4 个子模块，再用 `pub(crate) use` 把它们的 4 个主类型**重新导出**到 `queue` 模块根，方便其他文件写 `use crate::queue::{SndBuffer, RcvBuffer, ...};`。

#### 4.2.2 核心流程

子目录模块的加载流程：

1. crate 根的 `mod queue;`（[src/lib.rs:79](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L79)）让编译器找 `src/queue/mod.rs`。
2. `queue/mod.rs` 里的 `mod snd_queue;` 等四行，让编译器在 `src/queue/` 目录下找 `snd_queue.rs` 等。
3. `pub(crate) use snd_queue::UdtSndQueue;` 把类型提到 `crate::queue::` 路径下，但仅限 crate 内部可见。
4. 外部模块（如 `socket.rs`）通过 `use crate::queue::{RcvBuffer, SndBuffer};` 引用。

#### 4.2.3 源码精读

**`queue/mod.rs` 全文**（[src/queue/mod.rs:1-9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/mod.rs#L1-L9)）：

```rust
mod rcv_buffer;
mod rcv_queue;
mod snd_buffer;
mod snd_queue;

pub(crate) use rcv_buffer::RcvBuffer;
pub(crate) use rcv_queue::UdtRcvQueue;
pub(crate) use snd_buffer::SndBuffer;
pub(crate) use snd_queue::UdtSndQueue;
```

上半部分声明 4 个子模块（私有），下半部分 `pub(crate) use` 把 4 个主类型提到 `queue` 模块根。注意都是 `pub(crate)`——外部用户依然看不见，只有 crate 内部能用。

**各子模块的主类型**（用于建立直觉，不必记字段）：

- 发送调度队列 `UdtSndQueue` 内部是一个按时间排序的 `BinaryHeap`（[src/queue/snd_queue.rs:4](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L4) 引入 `BinaryHeap`），节点 `SendQueueNode`（[src/queue/snd_queue.rs:12-16](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L12-L16)）记录「该 socket 在哪个时间戳该发包」。
- 接收队列 `UdtRcvQueue`（[src/queue/rcv_queue.rs:22-28](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L22-L28)）持有一个共享 `channel: Arc<UdpSocket>` 和一张 `socket_refs` 表，负责把收到的包按目标 socket id 分发。
- 发送缓冲 `SndBuffer`，基础块 `SndBufferBlock`（[src/queue/snd_buffer.rs:13-20](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L13-L20)）记录消息号、origin_time、ttl、分片位置。
- 接收缓冲 `RcvBuffer`（[src/queue/rcv_buffer.rs:7-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L7-L12)）用 `BTreeMap<SeqNumber, UdtDataPacket>` 存乱序到达的包，并用 `next_to_read` / `next_to_ack` 两个游标分别管理「读」和「确认」。

> 这四个数据结构的内部机制（如何切片、如何重组、如何调度）是第 5 单元（u5-l1、u5-l2）的主题，本讲只标注它们在地图上的位置。

#### 4.2.4 代码实践

**实践目标**：验证子目录模块的加载规则，并理解 `pub(crate) use` 重导出的作用。

**操作步骤**：

1. 在仓库根目录确认 `src/queue/` 下有 `mod.rs`、`rcv_buffer.rs`、`rcv_queue.rs`、`snd_buffer.rs`、`snd_queue.rs` 共 5 个文件。
2. 用搜索工具在 `src/` 下查找 `use crate::queue::` 的所有出现，记录哪些文件依赖了 `queue` 模块（提示：`socket.rs`、`multiplexer.rs` 会命中）。
3. 思考：如果把 `queue/mod.rs` 第 6-9 行的 `pub(crate) use` 删掉，`src/socket.rs` 里的 `use crate::queue::{RcvBuffer, SndBuffer};` 还能编译吗？

**需要观察的现象**：

- `queue/` 是个 5 文件的子目录，入口是 `mod.rs`。
- 依赖 `queue` 的文件集中在「数据通路」相关模块。

**预期结果**：

- 删掉 `pub(crate) use` 后，`use crate::queue::{RcvBuffer, ...};` 会报「`RcvBuffer` 是私有的」之类错误，因为类型只能通过 `rcv_buffer::RcvBuffer` 这种全路径访问，而 `rcv_buffer` 模块本身又是 `queue` 内部私有的——所以重导出是「让兄弟模块能用」的必要手段。
- 具体编译报错信息「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `UdtSndQueue` 用 `BinaryHeap`（堆）而不是 `VecDeque`（普通队列）？

> **答案**：因为发送是**按时间调度**的——哪个 socket 的下一个包到点了，就先发谁的（[src/queue/snd_queue.rs:18-23](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L18-L23) 的 `Ord` 实现把「时间小的排前面」）。堆能在 O(log n) 内取出「最早到点」的节点；普通队列做不到按时间动态排序。

**练习 2**：`queue/mod.rs` 里的 `use` 为什么写成 `pub(crate) use` 而不是普通 `use`？

> **答案**：普通 `use` 只是给当前模块（`queue`）自己用；`pub(crate) use` 是「重导出」，把类型挂到 `crate::queue::` 这条公开路径上，让 `socket.rs` 等兄弟模块能直接 `use crate::queue::SndBuffer`。如果没有它，别的模块得写 `crate::queue::snd_buffer::SndBuffer`，而 `snd_buffer` 又是私有的，根本写不出来。

**练习 3**：`queue` 子目录里，「队列」和「缓冲」的分工区别是什么？

> **答案**：**队列**关心「时序与调度」——什么时候发包（`snd_queue`）、收到的包归哪个 socket（`rcv_queue`）；**缓冲**关心「数据本身」——把消息切成包并支持重传（`snd_buffer`）、把乱序包重组为有序流（`rcv_buffer`）。一个管「动作何时发生」，一个管「数据如何存放」。

---

### 4.3 state 子模块：socket 收发簿记容器

#### 4.3.1 概念说明

除了「队列 + 缓冲」这条数据通路，每个 UDT socket 还需要一堆**簿记状态**：上一次确认到哪个序号了、哪些包丢了还没收到、距离下次该发 ACK 还有多久、上次响应对端是什么时候……这些状态既不属于发送缓冲，也不属于接收缓冲，而是 socket 级别的「记账本」。

tokio-udt 把这本「账」抽成一个独立结构 `SocketState`，放在 `state/` 子目录里。`state/` 目前只有一个文件 `socket_state.rs`，但作者仍然用了子目录形式，**为将来扩展留位置**（比如以后再加 `connection_state.rs` 等）。

#### 4.3.2 核心流程

`state` 模块的加载和 `queue` 一样：

1. crate 根 `mod state;`（[src/lib.rs:83](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L83)）→ 找 `src/state/mod.rs`。
2. `state/mod.rs` 声明 `mod socket_state;` 并 `pub(crate) use socket_state::SocketState;`。
3. `socket.rs` 通过 `use crate::state::SocketState;`（[src/socket.rs:10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L10)）拿到它。

#### 4.3.3 源码精读

**`state/mod.rs` 全文**（[src/state/mod.rs:1-3](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/mod.rs#L1-L3)）：

```rust
mod socket_state;

pub(crate) use socket_state::SocketState;
```

**`SocketState` 的字段全貌**（[src/state/socket_state.rs:8-30](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L8-L30)）——这本「账」分两大块：

```rust
pub(crate) struct SocketState {
    pub last_rsp_time: Instant,
    // 接收相关
    pub last_sent_ack: SeqNumber,
    pub curr_rcv_seq_number: SeqNumber,
    pub last_ack_seq_number: AckSeqNumber,
    pub rcv_loss_list: LossList,       // 接收侧丢失列表
    // 发送相关
    pub last_ack_received: SeqNumber,
    pub curr_snd_seq_number: SeqNumber,
    pub snd_loss_list: LossList,       // 发送侧丢失列表（重传依据）
    pub next_ack_time: Instant,
    pub interpacket_interval: Duration,
    // ...
}
```

不必背字段，只需抓住分类直觉：

- **接收侧簿记**：`curr_rcv_seq_number`（已连续收到到哪）、`rcv_loss_list`（哪些包丢了要发 NAK）。
- **发送侧簿记**：`curr_snd_seq_number`（已发到哪）、`last_ack_received`（对端确认到哪）、`snd_loss_list`（哪些要重传）。
- **定时簿记**：`last_rsp_time`（多久没收到对端响应，用于判定超时/Broken）、`next_ack_time`（下次何时发 ACK）。

可以看到 `SocketState` 复用了 `loss_list` 模块里的 `LossList`（[src/loss_list.rs:5-7](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L5-L7)），这正是模块化的好处——收发两侧共用同一套「丢失区间」数据结构。

> 这些字段的具体用法（如何判定丢包、如何触发重传、如何超时）属于第 6、7 单元。本讲只确认：**`SocketState` 是 socket 的「记忆」，被 `UdtSocket` 持有，归类在「数据通路」层的簿记容器**。

#### 4.3.4 代码实践

**实践目标**：确认 `state` 子目录的组织，并体会「把簿记状态单独抽出」的设计意图。

**操作步骤**：

1. 打开 [src/state/socket_state.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs)，浏览全部字段，按「接收相关 / 发送相关 / 定时相关」三类给字段分组。
2. 在 [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) 中搜索 `state` 字段（`UdtSocket` 结构体里通常有类似 `state: Mutex<SocketState>` 的成员），确认 `SocketState` 是被 `UdtSocket` 持有的。
3. 思考：为什么 `SocketState` 不直接写进 `socket.rs`，而要单独成一个子模块？

**需要观察的现象**：

- `state/` 只有 `mod.rs` 和 `socket_state.rs` 两个文件。
- `SocketState` 字段量大（接收、发送、定时三大类），如果塞进 `socket.rs` 会让那个本就很大的文件更臃肿。

**预期结果**：

- 你会发现 `SocketState` 的字段都是「数值/时间戳/小集合」，本身不含复杂逻辑（大部分方法在 `socket.rs` 里），所以它更像一个**数据容器**。把它单独放一个文件，是为了让 `socket.rs` 专注于「行为」（如何发包、如何处理 ACK），把「状态」交给 `state/`——这是典型的「行为/数据分离」组织方式。
- `UdtSocket` 具体如何持有 `SocketState`（包在 `Mutex` 还是 `RwLock` 里）「待确认」，建议在 u3-l2（UdtSocket 状态机）那一讲细看。

#### 4.3.5 小练习与答案

**练习 1**：`SocketState` 同时有 `rcv_loss_list` 和 `snd_loss_list` 两个 `LossList`，它们是干什么的？

> **答案**：`rcv_loss_list` 记录**接收侧**发现丢了、还没收到的包序号（用来发 NAK 通知对端重传）；`snd_loss_list` 记录**发送侧**被对端要求重传（或自己超时判定要重传）的包序号（用来决定下次发送时优先重传哪些）。两者共用 `LossList` 这套区间数据结构，但语义相反——一个是「我缺什么」，一个是「我要补什么」。

**练习 2**：`state/` 只有一个 `socket_state.rs`，为什么还要用子目录（`mod.rs`）而不是直接 `src/socket_state.rs`？

> **答案**：用子目录形式是**为未来留扩展位**。如果以后新增 `connection_state.rs` 或其他状态结构，可以都放进 `state/` 下，而 crate 根的 `mod state;` 不用改。这是项目组织上的「预留命名空间」习惯。

**练习 3**：`SocketState` 里的 `last_rsp_time`（上次收到对端响应的时间）最可能被谁用到？

> **答案**：被**超时检测**逻辑用到。每隔一段时间检查「现在距 `last_rsp_time` 多久了」，如果超过阈值（结合指数退避），就判定对端无响应、把 socket 置为 `Broken`。这是第 7 单元 u7-l3（定时器）的核心判定之一。

## 5. 综合实践

把三个最小模块串起来，亲手产出一张「全项目模块地图」。这是本讲最重要的产出，建议**自己动手画一遍**再对答案。

### 任务

依据 [src/lib.rs:68-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L68-L84) 的 `mod` 声明，画出完整的模块树（含 `queue/`、`state/` 子目录展开），为每个 `.rs` 文件写一句话职责，并标注它属于 **入口 / 协议格式 / 数据通路 / 可靠性 / 拥塞控制** 哪一类。

### 参考答案（模块树 + 职责 + 分类）

> 说明：u1-l1 提到「17 个私有模块」，加上 `queue/` 的 4 个、`state/` 的 1 个子文件，共 22 个库源文件；再加上 `src/bin/` 下 2 个 binary，全 crate 共 24 个被引用的 `.rs`（不含 `lib.rs` 自身）。下面分类里，「数据通路」一条涵盖最广（含运行时基础设施），`udt` 我额外标了「运行时引擎」——它是个全局单例注册表，更接近基础设施而非纯数据通路。

| 文件 | 一句话职责 | 分类 |
|------|-----------|------|
| `src/lib.rs` | crate 根：文档 + 17 个 `mod` + 5 个 `pub use` | 入口 |
| `src/configuration.rs` | `UdtConfiguration`：MSS / 缓冲大小 / 复用 / linger 等配置项（[src/configuration.rs:9-10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L9-L10)） | 入口 |
| `src/connection.rs` | `UdtConnection`：包装 `UdtSocket`，实现 `AsyncRead/AsyncWrite`（[src/connection.rs:10-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L10-L12)） | 入口 |
| `src/listener.rs` | `UdtListener`：`bind` + `accept`，被动等待连接（[src/listener.rs:8-14](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L8-L14)） | 入口 |
| `src/packet.rs` | `UdtPacket` 枚举：用首比特统一区分数据包/控制包（[src/packet.rs:5-9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L5-L9)） | 协议格式 |
| `src/data_packet.rs` | 数据包：16 字节包头位域 + payload（[src/data_packet.rs:5-11](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L5-L11)） | 协议格式 |
| `src/control_packet.rs` | 控制包：固定头 + Handshake/ACK/NAK 等类型（[src/control_packet.rs:7-15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L7-L15)） | 协议格式 |
| `src/seq_number.rs` | `GenericSeqNumber`：31 位循环序列号算术（[src/seq_number.rs:14-21](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L14-L21)） | 协议格式 |
| `src/common.rs` | 工具：`ip_to_bytes` 把 IP 统一成 16 字节（[src/common.rs:3-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/common.rs#L3-L12)） | 协议格式 |
| `src/ack_window.rs` | `AckWindow`：ACK seq → 发送时刻，用于算 RTT（[src/ack_window.rs:6-10](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/ack_window.rs#L6-L10)） | 可靠性 |
| `src/loss_list.rs` | `LossList`：BTreeMap 存丢失区间，支持合并/拆分（[src/loss_list.rs:5-7](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L5-L7)） | 可靠性 |
| `src/flow.rs` | `UdtFlow`：包到达速率与链路带宽估计（[src/flow.rs:8-15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L8-L15)） | 拥塞控制 |
| `src/rate_control.rs` | `RateControl`：慢启动 + AIMD 拥塞窗口/发送周期（[src/rate_control.rs:8-15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L8-L15)） | 拥塞控制 |
| `src/socket.rs` | `UdtSocket`：核心枢纽，状态机 + 发送主流程 + ACK + 定时器 + 握手 + 关闭 | 数据通路（核心） |
| `src/udt.rs` | 全局单例 `Udt`：socket/multiplexer/peer 注册表 + GC worker（[src/udt.rs:17-27](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L17-L27)） | 运行时引擎（基础设施） |
| `src/multiplexer.rs` | `UdtMultiplexer`：单个 UDP socket 服务多 socket，跑收发两个 worker（[src/multiplexer.rs:14-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L14-L25)） | 数据通路（运行时） |
| `src/queue/mod.rs` | `queue` 子目录入口：声明 4 个子模块并重导出（[src/queue/mod.rs:1-9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/mod.rs#L1-L9)） | 数据通路 |
| `src/queue/snd_queue.rs` | `UdtSndQueue`：按时间排序的发送调度堆（[src/queue/snd_queue.rs:4](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L4)） | 数据通路 |
| `src/queue/snd_buffer.rs` | `SndBuffer`：消息切片 + 重传块 + TTL（[src/queue/snd_buffer.rs:13-20](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L13-L20)） | 数据通路 |
| `src/queue/rcv_queue.rs` | `UdtRcvQueue`：收 UDP 包并按 socket id 分发（[src/queue/rcv_queue.rs:22-28](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L22-L28)） | 数据通路 |
| `src/queue/rcv_buffer.rs` | `RcvBuffer`：乱序重组 + 按序读出（[src/queue/rcv_buffer.rs:7-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_buffer.rs#L7-L12)） | 数据通路 |
| `src/state/mod.rs` | `state` 子目录入口：声明 `socket_state` 并重导出（[src/state/mod.rs:1-3](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/mod.rs#L1-L3)） | 数据通路 |
| `src/state/socket_state.rs` | `SocketState`：socket 收发簿记容器（接收/发送/定时三类字段，[src/state/socket_state.rs:8-30](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L8-L30)） | 数据通路 |
| `src/bin/udt_sender.rs` | binary：客户端，死循环发数据并打印速率指标（不在模块树，Cargo 自动发现） | 入口（binary） |
| `src/bin/udt_receiver.rs` | binary：服务端，bind + accept 并统计吞吐（不在模块树，Cargo 自动发现） | 入口（binary） |

### 五条主线一句话总结

- **入口**（4 + 2 binary）：用户最先接触的——配置、连接、监听、示例程序。
- **协议格式**（5）：UDT 在 UDP 之上定义的「线上字节长什么样」——包、数据包头、控制包头、序列号、IP 编码工具。
- **数据通路**（9）：数据从用户态走到 UDP 再走回来的整条流水线——socket 核心、多路复用器、收发队列与缓冲、簿记状态。
- **可靠性**（2）：让 UDP 变「可靠」的两块砖——ACK 窗口（算 RTT）、丢失列表（丢包重传）。
- **拥塞控制**（2）：让发送「不压垮网络」——流量/带宽估计、速率/窗口控制。

> 注：`udt.rs`（全局单例）严格说更偏「运行时基础设施」，我单列一类；你也可以把它并入「数据通路」，只要你能说清理由。这种分类的**判断力**正是本练习想训练的。

## 6. 本讲小结

- [`src/lib.rs`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs) 用 17 个私有 `mod` 搭起模块树主干，其中 `queue`、`state` 落到子目录（有 `mod.rs`），其余 15 个直接对应 `src/xxx.rs`。
- 对外公共 API 只有 5 个 `pub use`（[src/lib.rs:86-90](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L86-L90)）；其余模块一律 `pub(crate)`，对用户不可见。
- `queue/` 子目录（[src/queue/mod.rs:1-9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/mod.rs#L1-L9)）收 4 个收发相关文件，靠 `pub(crate) use` 重导出，分工是「队列管调度、缓冲管数据」。
- `state/` 子目录（[src/state/mod.rs:1-3](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/mod.rs#L1-L3)）目前只放 `SocketState`——一本 socket 级别的「收发簿记账本」。
- 全部源文件可归入五条主线：**入口 / 协议格式 / 数据通路 / 可靠性 / 拥塞控制**；`src/bin/` 下两个 binary 不在模块树里，由 Cargo 自动发现。
- 这张地图是后续所有讲义的「索引页」——以后读到任何一篇，先回来查它在哪条主线、哪个文件。

## 7. 下一步学习建议

本讲只看了「门面」，下一步建议：

1. **u1-l4（公共 API 全貌与配置）**：趁热把 5 个公共类型逐一过一遍，重点读 `UdtConfiguration` 的字段——它决定了入口层的全部可调参数。
2. **进入第 2 单元（u2-l1/u2-l2）**：动手用 `UdtConnection::connect` 和 `UdtListener::bind` 跑通一对收发，把「入口」这条主线吃透。
3. **暂时不要**急着钻进 `queue/`、`socket.rs` 的实现——它们属于第 3 单元起的「数据通路」深潜，需要先有 u3（全局引擎与状态机）的整体架构认知再读会更顺。本讲末尾的模块地图，就是为后面这些深潜准备的「坐标系」。
