# 控制包格式：握手、ACK、NAK 等类型

## 1. 本讲目标

上一讲（u4-l2）我们拆解了**数据包**的线上格式——承载用户字节流的包。本讲拆解它的孪生兄弟：**控制包（Control Packet）**。控制包不携带用户数据，只携带「信令」：建立连接的握手、确认收到的 ACK、报告丢包的 NAK、优雅关闭的 Shutdown 等等。

学完本讲你应该能够：

- 画出控制包**16 字节固定头**的位域布局，并说出每个字段装什么。
- 背出 `ControlPacketType` 的 7 种类型及其 `type_as_u15` 编码（含一个故意跳过的编号）。
- 理解 `HandshakeInfo` 的可变载荷布局，并能解释它如何仅凭一段「是否全零」的判断区分 IPv4/IPv6。
- 读懂 ACK / NAK / DropRequest 三类控制信息字段的语义，以及 `additional_info` 这个「复用字段」在不同包里装的是什么。
- 说清握手包里 `connection_type` 取值 `1`、`-1`、`1002` 各代表什么阶段与含义。

---

## 2. 前置知识

本讲是 u4-l1（`UdtPacket` 统一入口）和 u4-l2（数据包格式）的直接续篇，默认你已经掌握：

- **首比特 dispatch**：UDT 把所有包的第一个比特（最高位）当作「类型标志」——为 `0` 是数据包，为 `1` 是控制包。`UdtPacket::deserialize` 据此分流（详见 [u4-l1 讲义](u4-l1-udt-packet-dispatch.md)）。
- **大端字节序（big-endian）**：UDT 所有多字节整数都按大端序列化，即高位字节在前。
- **位运算**：掩码（`&`）、按位或（`|`）、移位（`<<` `>>`）。
- **Rust 的 `match` 与 `enum`**：本讲大量出现「按变体分发」的模式。
- **`SeqNumber` / `AckSeqNumber` / `MsgNumber`**：三种序列号，本讲只需知道它们底层都是 `u32`，可通过 `.number()` 取出原始值（循环算术细节留到 u4-l4）。

一个贯穿全讲的直觉：**控制包 = 16 字节固定头 + 变长控制信息（control info field）**。固定头对所有控制包都一样，变长部分则随类型而变。抓住这点，下面的细节就只是一张张「填表」。

---

## 3. 本讲源码地图

本讲只读两个文件，它们都不属于公共 API（全部 `pub(crate)`）：

| 文件 | 作用 |
|------|------|
| `src/control_packet.rs` | 控制包的全部定义：固定头结构体、`ControlPacketType` 枚举与编码、7 种类型对应的 4 个 `*Info` 载荷结构体（`HandShakeInfo`/`AckInfo`/`NakInfo`/`DropRequestInfo`），以及它们的 `serialize`/`deserialize`。 |
| `src/common.rs` | 只有一个工具函数 `ip_to_bytes`，把 `IpAddr` 统一压成 16 字节，是握手包处理 IPv4/IPv6 的关键。 |

此外会少量引用 `src/socket.rs` 中握手处理（`listen_on_handshake`、`compute_cookie`）和 `src/seq_number.rs` 中的 `MAX_NUMBER` 常量，用以说明 `connection_type` 的语义与 `msg_seq_number` 的掩码——但**本讲不展开**握手流程与循环算术，它们分别在 u8-l1 与 u4-l4 详解。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1** 固定头：`UdtControlPacket` 的 16 字节布局
- **4.2** `ControlPacketType` 编码表与类型分发
- **4.3** `HandshakeInfo`：握手控制包的可变载荷（最复杂）
- **4.4** `Ack` / `Nak` / `DropRequest` 控制信息

### 4.1 固定头：UdtControlPacket 的 16 字节布局

#### 4.1.1 概念说明

UDT 规定：**任何控制包都以一个 16 字节（128 位）的固定头开始**。这 16 字节对所有控制包类型都长得一样——这是「固定」的含义。固定头之后，再跟一段随类型变化的「控制信息字段（control info field）」。

为什么要有固定头？因为接收端在拆包时，**最先要做的事是：这包发给哪个 socket？这包是什么类型？** 这两个问题的答案必须出现在所有控制包里相同的位置，否则没法统一分发。固定头就是为「统一寻址 + 统一定型」服务的。

#### 4.1.2 核心流程

固定头的 128 位按以下布局（位编号从最高有效位算起，bit 0 = 整包的最高位）：

| 字节偏移 | 位 | 字段名 | 含义 |
|---------|-----|--------|------|
| 0–1 | 0 | 类型标志 | `1` 表示控制包（与数据包的 `0` 区分）|
| 0–1 | 1–15 | `packet_type` | 15 位类型码（`type_as_u15`）|
| 2–3 | 16–31 | `reserved` | 保留位，实现里恒为 `0` |
| 4–7 | 32–63 | `additional_info` | 「复用字段」，随类型装 ACK 序号 / msg id 等 |
| 8–11 | 64–95 | `timestamp` | 时间戳（实现里恒填 `0`，不真正使用）|
| 12–15 | 96–127 | `dest_socket_id` | 目标 socket id（解复用/分发用）|
| 16+ | 128+ | `control_info_field` | 变长控制信息，随类型而定 |

几个要点：

- **bit 0** 是「整包的最高位」。序列化时第一个 `u16` 字是 `0x8000 | type_as_u15`：`0x8000` 就是把 bit 0 置 1，恰好让接收端的 `raw[0] >> 7 == 1`，从而被 u4-l1 的首比特 dispatch 判定为控制包。
- `additional_info` 是个「四不像」字段：物理上固定 4 字节，但**语义随类型变化**（ACK 装确认序号、DropRequest 装 msg id、其余为 0）。这是 UDT 为省空间做的复用设计。
- `dest_socket_id` 是接收端解复用的关键（见 u4-l1 的分发链路）。

#### 4.1.3 源码精读

结构体定义本身就注释了每个字段占据哪些位：

[src/control_packet.rs:8-15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L8-L15) —— `UdtControlPacket` 结构体，注释写清了 bit 0=1、各字段对应的位区间。

序列化函数把结构体按上表顺序、大端地拼成字节流。关键是第一行：`0x8000 + type` 同时完成了「置类型标志位」和「写入 15 位类型码」两件事：

[src/control_packet.rs:123-132](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L123-L132) —— `serialize`：先写 `0x8000 + type_as_u15()` 这 2 字节，再依次写 `reserved`/`additional_info`/`timestamp`/`dest_socket_id`（各按大端），最后追加变长的 `control_info_field()`。

反序列化是镜像过程，从固定位置切片读回各字段，并要求至少 16 字节：

[src/control_packet.rs:134-154](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L134-L154) —— `deserialize`：校验 `raw.len() < 16` 后，从 `raw[2..4]`、`raw[4..8]`、`raw[8..12]`、`raw[12..16]` 分别读回 `reserved`/`additional_info`/`timestamp`/`dest_socket_id`，类型部分委托给 `ControlPacketType::deserialize`。

注意 `deserialize` **没有读 `raw[0..2]` 的类型标志位**——它直接信任「调用方已经通过首比特 dispatch 确认这是控制包」，只把 `raw` 整段透传给 `ControlPacketType::deserialize` 去解析类型码与控制信息（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：手工验证「16 字节固定头中 `dest_socket_id` 落在最后 4 字节」。

**操作步骤**（源码阅读型，因为 `UdtControlPacket` 是 `pub(crate)`，外部无法直接构造）：

1. 打开 [src/control_packet.rs:123-132](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L123-L132)，对照 `serialize` 的写入顺序。
2. 假设有一个 `KeepAlive` 控制包，`dest_socket_id = 0x01020304`。逐字节推算它 `serialize()` 的前 16 字节：
   - 字节 0–1：`0x8000 + 0x0001`（KeepAlive 编码）= `0x8001` → 大端为 `0x80, 0x01`
   - 字节 2–3：`reserved = 0` → `0x00, 0x00`
   - 字节 4–7：`additional_info = 0` → `0x00, 0x00, 0x00, 0x00`
   - 字节 8–11：`timestamp = 0` → `0x00, 0x00, 0x00, 0x00`
   - 字节 12–15：`dest_socket_id = 0x01020304` → `0x01, 0x02, 0x03, 0x04`
   - 字节 16+：KeepAlive 的 `control_info_field()` 为空（见 4.2），故总共恰好 16 字节。
3. 反过来，确认 `deserialize` 从 `raw[12..16]` 取回的就是 `0x01020304`。

**需要观察的现象**：整包 16 字节中，`dest_socket_id` 的 4 个字节 `01 02 03 04` 确实出现在末尾，且类型码 `0x8001` 出现在最前 2 字节。

**预期结果**：手工推算的 16 字节与「先类型字、再 reserved、再 additional_info、再 timestamp、最后 dest_socket_id」的顺序一致。本结论可直接从源码读出，无需运行（标注「待本地验证」的是你想用 `println!` 实测时的情形——可在 `UdtMultiplexer` 发包处临时加日志，但不在本讲要求范围内）。

#### 4.1.5 小练习与答案

**练习 1**：为什么固定头里 `timestamp` 字段在 `new_*` 构造函数里一律填 `0`，而数据包头却认真填了时间戳？

**参考答案**：控制包的语义大多与「立即处理的信令」相关（握手、确认、关闭），不需要逐包时间戳；UDT 把定时/超时逻辑放在 socket 层的 `check_timers`（见 u7-l3），用 socket 本地的时钟即可，故控制包头的时间戳字段在本实现里未使用，恒为 0。数据包的时间戳则用于 RTT 估计等，所以认真填写。

**练习 2**：若一个 UDP 数据报只有 10 字节就被当作控制包去 `deserialize`，会发生什么？

**参考答案**：[src/control_packet.rs:135-140](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L135-L140) 的长度校验 `raw.len() < 16` 会命中，返回 `Err(InvalidData("control packet header is too short"))`。调用方（接收 worker）会用 `.ok()?` 把这个错误静默丢弃——这是应对 UDP 乱序/残包的常规处理（见 u4-l1）。

---

### 4.2 ControlPacketType 编码表与类型分发

#### 4.2.1 概念说明

固定头里的 15 位类型码（bits 1–15）决定「这是个什么控制包」。tokio-udt 用一个 `ControlPacketType` 枚举枚举了全部 7 种业务类型加 1 种「用户自定义」，并给每个变体一个固定的 15 位编码。这个编码表是**线上协议契约**——收发双方必须用同一套数字，否则无法互通。

注意：这些类型与 UDT 参考实现（UDT4，C++）保持线上兼容，所以编号是有「历史包袱」的，并不完全连续。

#### 4.2.2 核心流程

`ControlPacketType` 的编码表（来自 `type_as_u15`）：

| 枚举变体 | u15 编码 | 是否携带 control_info | control_info 结构 |
|----------|---------|----------------------|-------------------|
| `Handshake(HandShakeInfo)` | `0x0000` | 是（48 字节） | `HandShakeInfo` |
| `KeepAlive` | `0x0001` | 否 | 无 |
| `Ack(AckInfo)` | `0x0002` | 是（4 或 24 字节） | `AckInfo` |
| `Nak(NakInfo)` | `0x0003` | 是（变长） | `NakInfo` |
| ——（`0x0004` 未用）—— | `0x0004` | —— | 解析时报「unknown control packet type」 |
| `Shutdown` | `0x0005` | 否 | 无 |
| `Ack2` | `0x0006` | 否 | 无 |
| `MsgDropRequest(DropRequestInfo)` | `0x0007` | 是（8 字节） | `DropRequestInfo` |
| `UserDefined` | `0x7fff` | 否 | 无 |

**两个关键观察**：

1. **`0x0004` 被故意跳过**：编码从 `0x0003`（Nak）直接跳到 `0x0005`（Shutdown）。这是因为参考实现 UDT4 中 `0x0004` 预留给「Congestion Warning（拥塞警告）」，tokio-udt 未实现该类型，但为保持线上兼容，`Shutdown` 仍沿用 `0x0005`。若线上真来了一个 `0x0004` 的包，[src/control_packet.rs:206-211](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L206-L211) 会落入 `_ =>` 分支报错。
2. **只有 4 种类型携带 control_info**：`Handshake`/`Ack`/`Nak`/`MsgDropRequest`（它们带关联数据）；`KeepAlive`/`Shutdown`/`Ack2`/`UserDefined` 的 `control_info_field()` 返回空 `vec![]`。

类型分发的两条路：

- **序列化方向**（`control_info_field`）：`match self` → 各变体调用自己的 `serialize()`，无数据的返回空。
- **反序列化方向**（`ControlPacketType::deserialize`）：先 `raw[0..2] & 0x7FFF` 取出 15 位类型码，再 `match type_id` 分发到对应 `*Info::deserialize(&raw[16..])`（即把固定头之后的字节交给对应结构体解析）。

#### 4.2.3 源码精读

枚举定义：注意 `Handshake`/`Ack`/`Nak`/`MsgDropRequest` 是**带关联数据的变体**，其余是**单元变体**：

[src/control_packet.rs:157-167](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L157-L167) —— `ControlPacketType` 枚举，7 种业务类型 + `UserDefined`。

编码表 `type_as_u15`（注意 `0x0004` 的空缺）：

[src/control_packet.rs:170-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L170-L181) —— 每个变体映射到固定的 15 位码。

序列化分发：

[src/control_packet.rs:183-191](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L183-L191) —— `control_info_field`：带数据的变体调用各自的 `serialize()`，其余走 `_ => vec![]`。

反序列化分发——这是类型识别的总开关：

[src/control_packet.rs:193-214](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L193-L214) —— `ControlPacketType::deserialize`：`& 0x7FFF` 抹掉最高位（类型标志位）得到 `type_id`，再 `match` 分发。带数据的变体把 `&raw_control_packet[16..]`（固定头之后）交给对应 `*Info::deserialize`。注意它读的是**整包** `raw_control_packet[0..2]` 取类型，但传 `raw[16..]` 给载荷——固定头与载荷在字节流里就是前后相接的。

#### 4.2.4 代码实践

**实践目标**：把编码表「吃透」，并验证 `additional_info` 这个复用字段的取数逻辑。

**操作步骤**：

1. 对照 [src/control_packet.rs:170-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L170-L181)，默写出 7 种业务类型的 u15 编码，并标出 `0x0004` 的空缺。
2. 阅读 `additional_info` 的「按类型取数」逻辑：

   [src/control_packet.rs:106-121](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L106-L121) —— `ack_seq_number()` 只对 `Ack`/`Ack2` 返回 `Some`（取自 `additional_info`），`msg_seq_number()` 只对 `MsgDropRequest` 返回 `Some`。

3. 回答：一个 `Ack2` 包里 `additional_info` 装的是什么？一个 `MsgDropRequest` 包里又装什么？

**需要观察的现象**：同一个 4 字节物理字段 `additional_info`，在不同类型里语义完全不同——这就是「字段复用」。

**预期结果**：`Ack2` 的 `additional_info` = 被确认的 ACK 序号（由 `new_ack2` 的 `seq.number()` 写入，见 [src/control_packet.rs:40-48](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L40-L48)）；`MsgDropRequest` 的 `additional_info` = 被丢弃消息的 msg id（由 `new_drop` 的 `msg_id.number()` 写入，见 [src/control_packet.rs:50-66](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L50-L66)）。`ack_seq_number()`/`msg_seq_number()` 正是为「按类型安全地取出这个复用字段」而设计的。

#### 4.2.5 小练习与答案

**练习 1**：`msg_seq_number()` 里有一行 `self.additional_info & MsgNumber::MAX_NUMBER`（见 [src/control_packet.rs:117](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L117)）。`MsgNumber::MAX_NUMBER` 是多少？这个掩码有必要吗？

**参考答案**：`MsgNumber::MAX_NUMBER = 0x1fff_ffff`（29 位，见 [src/seq_number.rs:106](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L106)）。msg id 写入时用的是 `msg_id.number()`，本来就 ≤ 29 位，所以这个掩码在**正常路径下是冗余的**——它只是防御性地把高 3 位清零，确保即使 `additional_info` 高位有脏数据也不会越界。属于稳健性设计。

**练习 2**：为何 `Ack2` 不携带 `control_info_field`，却能完成「确认某个 ACK」的功能？

**参考答案**：因为被确认的 ACK 序号已经被塞进了固定头的 `additional_info`（`new_ack2` 写入 `seq.number()`）。ACK2 的全部信息就是「这一个序号的 ACK 我收到了」，不需要额外载荷，所以 `control_info_field()` 返回空，整包恰好 16 字节。详见 u6-l4 对 ACK2/RTT 测量的讲解。

---

### 4.3 HandshakeInfo：握手控制包的可变载荷

#### 4.3.1 概念说明

握手包（`Handshake`，编码 `0x0000`）是控制包里**最复杂**的一种，因为建立连接需要协商大量参数：UDT 版本、socket 类型、初始序列号、最大包大小、最大窗口、连接类型、socket id、SYN cookie、IP 地址。这些全部塞进握手包的 `control_info_field`，由 `HandShakeInfo` 结构体承载。

握手包 = 16 字节固定头 + 48 字节握手信息 = **共 64 字节**（无 payload）。

其中最巧妙的设计是 **IP 地址字段的 IPv4/IPv6 自描述**：UDT 给 IP 地址留了固定 16 字节，但 IPv4 只有 4 字节。怎么知道对端发的是 v4 还是 v6？答案是看「后面 12 字节是否全零」——本节的重点。

#### 4.3.2 核心流程

`HandShakeInfo` 在 control_info（即整包 `raw[16..]`）中的字节布局：

| control_info 偏移 | 整包偏移 | 字段 | 类型 |
|-------------------|---------|------|------|
| 0–3 | 16–19 | `udt_version` | u32 |
| 4–7 | 20–23 | `socket_type` | u32（Stream=1, Datagram=2）|
| 8–11 | 24–27 | `initial_seq_number` | u32（SeqNumber）|
| 12–15 | 28–31 | `max_packet_size` | u32（即 MSS）|
| 16–19 | 32–35 | `max_window_size` | u32 |
| 20–23 | 36–39 | `connection_type` | i32（1 / -1 / 1002）|
| 24–27 | 40–43 | `socket_id` | u32 |
| 28–31 | 44–47 | `syn_cookie` | u32 |
| 32–47 | 48–63 | `ip_address` | 16 字节 |

**IPv4/IPv6 判别逻辑**（ deserialize 中 `raw` 指 control_info 切片，即整包 `raw_packet[16..]`）：

- `ip_to_bytes` 序列化 IPv4 时：4 字节 octets 写在前 4 字节（`raw[32..36]`），后 12 字节（`raw[36..48]`）全填 0。
- 序列化 IPv6 时：16 字节 octets 全部写入（`raw[32..48]`）。
- 反序列化时：检查 `raw[36..48]`（IP 字段的后 12 字节）是否**全零**。全零 → 当 IPv4（只取 `raw[32..36]`）；非全零 → 当 IPv6（取全部 16 字节）。

用布尔表达式表达判别条件：

\[
\text{is\_v4} = \bigl(\forall b \in \texttt{raw[36..48]},\ b = 0\bigr)
\]

**`connection_type` 的三态语义**（本节只讲编码含义，握手往返流程见 u8-l1）：

| 取值 | 含义 |
|------|------|
| `1` | 客户端发起的「常规连接」握手（listener 收到后会计算并回 SYN cookie）|
| `-1` | 客户端回带的、已携带 cookie 的握手（listener 收到后校验 cookie）|
| `1002` | 拒绝码：版本或 socket_type 不匹配时回此值（注释称沿用 C++ 实现的错误码）|

#### 4.3.3 源码精读

结构体定义（注意 `connection_type` 是 `i32`，可装负值与 `1002`）：

[src/control_packet.rs:217-228](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L217-L228) —— `HandShakeInfo` 字段。

序列化：前 5 个 u32 用数组 + `flat_map(to_be_bytes)` 拼接，再依次 `chain` 后续字段，最后用 `ip_to_bytes` 处理 IP：

[src/control_packet.rs:231-246](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L231-L246) —— `HandShakeInfo::serialize`。注意 `socket_type as u32` 把枚举（Stream=1/Datagram=2）转成数字。

`ip_to_bytes` 是 IPv4/IPv6 统一为 16 字节的工具：

[src/common.rs:3-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/common.rs#L3-L12) —— IPv4 填前 4 字节、后 12 字节补 0；IPv6 直接写 16 字节。这就是「后 12 字节全零 ⇔ IPv4」得以成立的根源。

反序列化与 IPv4/IPv6 判别——本讲的核心代码：

[src/control_packet.rs:248-274](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L248-L274) —— `HandShakeInfo::deserialize`。第 [251-261 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L251-L261) 即判别逻辑：`raw[36..48].iter().all(|b| *b == 0)` 为真走 IPv4 分支（取 `raw[32..36]`），否则走 IPv6 分支（取 `raw[32..48]`）。

`connection_type` 三态在握手处理中的使用（来自 `socket.rs`，本讲只做语义印证）：

[src/socket.rs:376-433](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L376-L433) —— `listen_on_handshake`：第 [385 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L385) 判 `connection_type == 1` 时计算并回 cookie；第 [395 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L395) 校验「只能是 1 或 -1」；第 [404-405 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L404-L405) 校验 cookie（同时接受当前分钟与上一分钟的 cookie，见下）；第 [413-423 行](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L413-L423) 在版本/socket_type 不匹配时回 `connection_type = 1002` 拒绝。

`compute_cookie` 用 SHA256 生成 cookie，并按分钟级时间戳轮换：

[src/socket.rs:356-366](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L356-L366) —— `compute_cookie`：`timestamp = elapsed_secs / 60 + offset`，cookie = `SHA256(salt:host:port:timestamp)` 的前 4 字节。`offset` 参数正是为了让校验端同时接受「当前分钟（offset=0）」与「上一分钟（offset=-1）」的 cookie，避免跨分钟边界时握手失败。SYN cookie 的防伪造与每分钟轮换机制详见 u8-l1。

#### 4.3.4 代码实践

**实践目标**：亲手推算一个握手包的字节，并验证 IPv4/IPv6 判别逻辑。

**操作步骤**（源码阅读 + 手工推算）：

1. 假设客户端发起握手，`HandShakeInfo` 关键字段为：`udt_version=5`、`socket_type=Stream(1)`、`initial_seq_number=1000`、`max_packet_size=1500`、`max_window_size=8192`、`connection_type=1`、`socket_id=0x11223344`、`syn_cookie=0`、`ip_address=127.0.0.1`。
2. 按 [src/control_packet.rs:231-246](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L231-L246) 的顺序，推算 control_info 的 48 字节：
   - `udt_version=5` → `00 00 00 05`
   - `socket_type=1` → `00 00 00 01`
   - `initial_seq_number=1000` → `00 00 03 E8`
   - `max_packet_size=1500` → `00 00 05 DC`
   - `max_window_size=8192` → `00 00 20 00`
   - `connection_type=1`（i32）→ `00 00 00 01`
   - `socket_id=0x11223344` → `11 22 33 44`
   - `syn_cookie=0` → `00 00 00 00`
   - `ip_address=127.0.0.1`：经 `ip_to_bytes` → 前 4 字节 `7F 00 00 01`，后 12 字节全 `00`。
3. 检验反序列化的 IPv4 判别：control_info 的 IP 字段位于偏移 32–47（即上面最后 16 字节），其中偏移 36–47（后 12 字节）全零 → 判为 IPv4，取前 4 字节 `7F 00 00 01` 还原成 `127.0.0.1`。✓

**需要观察的现象**：由于 `127.0.0.1` 是 IPv4，序列化后 IP 字段后 12 字节为 `00`，反序列化命中「全零 → IPv4」分支，正确还原。

**预期结果**：手工推算的 48 字节 control_info 与源码 `serialize` 顺序一致；反序列化能正确还原为 IPv4。若把 `ip_address` 换成 IPv6（如 `::1`），则 `ip_to_bytes` 写满 16 字节（`00…00 01`），后 12 字节不全零 → 走 IPv6 分支。

**关于 `connection_type` 1/-1/1002 的验证**：阅读 [src/socket.rs:385](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L385)、[src/socket.rs:395](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L395)、[src/socket.rs:416](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L416) 三处分支，复述：`1` 触发「回 cookie」、非 `1`/`-1` 报错、版本或类型不匹配回 `1002`。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 IPv6 地址恰好其后 12 字节全为零（例如形如 `xxxx:xxxx:xxxx:xxxx::` 的地址），`HandShakeInfo::deserialize` 会怎么处理？这是不是一个 bug？

**参考答案**：会被**误判为 IPv4**——因为判别条件只看 `raw[36..48]` 是否全零，而后 12 字节全零的 IPv6 地址会满足该条件，于是只取前 4 字节当成 IPv4，地址被破坏。理论上这是一个边角 bug。但在实际 UDT 部署中，对端地址几乎不会是「后 12 字节恰好全零」的 IPv6 地址（这类地址极为罕见），所以实践中未造成问题。这是「用启发式（heuristic）而非显式 type 字段区分 v4/v6」带来的固有取舍。

**练习 2**：为什么 cookie 校验要同时接受 `offset=0` 与 `offset=-1` 两个值（[src/socket.rs:404-405](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L404-L405)）？

**参考答案**：因为 `compute_cookie` 的时间戳是 `elapsed_secs / 60`，**每分钟才变化一次**。客户端拿到 listener 回的 cookie 后，可能拖到「下一分钟」才回带 cookie 的握手包；此时 listener 用当前时间戳重算的 cookie 已与客户端携带的不一致。同时校验「上一分钟」的 cookie（`offset=-1`）即可容忍跨分钟边界，避免在边界附近握手失败。

---

### 4.4 Ack / Nak / DropRequest 控制信息

#### 4.4.1 概念说明

除了握手包，还有三种带 `control_info` 的控制包：`Ack`（确认）、`Nak`（负确认/丢包报告）、`MsgDropRequest`（消息丢弃请求）。它们承载可靠性机制的核心信令：

- **ACK**：接收方告诉发送方「我已按序收到 seq < N 的所有包」。分**轻量 ACK**（只报下一个期望序号）与**完整 ACK**（额外携带 RTT、带宽、缓冲余量等供拥塞控制使用）。
- **NAK**：接收方告诉发送方「这几个包丢了，请重传」。载荷是一串 `u32` 丢失条目。
- **MsgDropRequest**：在消息（messaging）模式下，告知对端丢弃某条消息的一段序号区间（流式 streaming 模式下基本不用）。

注意：这三类的「序号类」字段都塞在 control_info，而**少量元信息（如 ACK 序号、msg id）则复用了固定头的 `additional_info`**——这是 4.1 提到的「字段复用」在具体类型上的体现。

#### 4.4.2 核心流程

三种 control_info 的结构：

**AckInfo**（变长：4 字节或 24 字节）：

| 字段 | 字节数 | 说明 |
|------|-------|------|
| `next_seq_number` | 4 | 下一个期望的包序号（之前的都已收到，不含本号）|
| `AckOptionalInfo`（可选） | 0 或 20 | `rtt`、`rtt_variance`、`available_buf_size`、`pack_recv_rate`、`link_capacity`，各 4 字节 |

轻/全 ACK 的区分：`deserialize` 时若 `raw.len() <= 4` → 无可选信息（light ACK）；否则读出 5 个字段（full ACK）。

**NakInfo**（变长）：

| 字段 | 字节数 | 说明 |
|------|-------|------|
| `loss_info: Vec<u32>` | 4 × N | 一串丢失条目，每条 4 字节 |

`NakInfo` 本身只是个 `Vec<u32>` 载体，**单点丢失 vs 区间丢失的编码由调用方（socket.rs）负责**：单点直接写序号；区间则把首条目的最高位（`0x8000_0000`）置 1 表示「这是区间起点」，下一条是区间终点。这个编码细节见 [src/socket.rs:620](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L620) 与 [src/socket.rs:734](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L734)，本讲不展开（u6-l3 详解）。

**DropRequestInfo**（固定 8 字节）：

| 字段 | 字节数 | 说明 |
|------|-------|------|
| `first_seq_number` | 4 | 丢弃区间起点 |
| `last_seq_number` | 4 | 丢弃区间终点 |

注意：被丢弃消息的 **msg id 不在 control_info，而在固定头的 `additional_info`**（`new_drop` 写入 `msg_id.number()`），用 `msg_seq_number()` 取出。

#### 4.4.3 源码精读

**AckInfo**：

[src/control_packet.rs:277-283](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L277-L283) —— 结构体：`next_seq_number` + `Option<AckOptionalInfo>`。

[src/control_packet.rs:286-309](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L286-L309) —— `deserialize`：先读 `next_seq_number`；`raw.len() <= 4` → `info=None`（light）；否则读 5 个字段（full）。

[src/control_packet.rs:311-326](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L311-L326) —— `serialize`：`None` 时只写 4 字节；`Some` 时写 6 个 u32（next_seq_number + 5 个可选字段）。

[src/control_packet.rs:329-337](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L329-L337) —— `AckOptionalInfo`：RTT(µs)、rtt_variance、可用缓冲、收包速率、链路容量。这些正是 u7 拥塞控制的输入。

**NakInfo**：

[src/control_packet.rs:339-364](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L339-L364) —— 纯 `Vec<u32>` 载体。`serialize` 逐个 `to_be_bytes` 拼接；`deserialize` 按 4 字节 `chunks(4)` 读回，跳过不足 4 字节的尾部。区间编码（`0x8000_0000` 标志位）不在这里处理，由 socket.rs 在构造/解析 `loss_info` 时完成。

**DropRequestInfo**：

[src/control_packet.rs:366-392](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L366-L392) —— 两个 `SeqNumber`（first/last），共 8 字节。msg id 通过固定头 `additional_info` 携带（见 `new_drop`）。

**`new_ack` 构造函数**——看 ACK 序号如何被放进固定头：

[src/control_packet.rs:88-104](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L88-L104) —— `new_ack`：把 `ack_number.number()` 写进 `additional_info`，把 `next_seq_number` 与可选 `info` 装进 `AckInfo`（control_info）。这印证了「ACK 包有两个序号」：`additional_info` 里的是**ACK 自身的序号**（供对端回 ACK2），`AckInfo.next_seq_number` 是**已按序收到的数据序号水位**。

#### 4.4.4 代码实践

**实践目标**：搞清 ACK 包里「两个序号」分别是什么，以及 light/full ACK 的字节差异。

**操作步骤**：

1. 阅读 [src/control_packet.rs:88-104](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L88-L104)，对照填下表：

   | 信息 | 放在哪 | 字段 |
   |------|-------|------|
   | ACK 自身的序号（供对端回 ACK2） | 固定头 `additional_info` | `ack_number.number()` |
   | 已按序收到的数据水位（不含） | control_info | `AckInfo.next_seq_number` |
   | RTT/带宽/缓冲等拥塞反馈（可选） | control_info | `AckOptionalInfo` |

2. 推算一个 light ACK 的字节长度：16（固定头）+ 4（仅 `next_seq_number`）= **20 字节**。
3. 推算一个 full ACK 的字节长度：16 + 4 + 20（5 个可选字段）= **40 字节**。
4. 在 [src/socket.rs:802](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L802) 与 [src/socket.rs:860](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L860) 附近，观察调用 `new_ack` 时何时传 `Some(AckOptionalInfo)`（full）、何时传 `None`（light）。

**需要观察的现象**：同一个 `Ack` 类型，因 `info` 是否为 `Some` 而在线上占 20 或 40 字节；接收端靠 `raw.len() <= 4`（control_info 部分的长度）自动区分二者。

**预期结果**：light ACK 20 字节、full ACK 40 字节。何时发 light、何时发 full 由 `send_ack` 的定时策略决定（详见 u6-l2）。本结论可纯源码读出；若要实测可在 `new_ack` 临时加 `println!` 后跑 u1-l2 的 sender/receiver，属「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`AckInfo::deserialize` 用 `raw.len() <= 4` 判断 light/full。为什么用 `<=` 而不是 `==`？

**参考答案**：因为 `raw` 是 control_info 切片，light ACK 的 control_info 恰好是 `next_seq_number` 的 4 字节。用 `<= 4` 而非 `== 4` 是一种**防御性写法**：万一 UDP 截断了 control_info（少于 4 字节），`get_u32(0)` 仍可能因切片越界而 panic，但若恰好尾部少了几字节却仍 ≥ 已读部分，`<= 4` 能把它当 light 处理而不强行读 full 的 5 个字段，避免读到垃圾数据。结合 UDP 的不可靠性，这是稳健的处理。

**练习 2**：`MsgDropRequest` 的 msg id 为什么不放进 `DropRequestInfo`（control_info），而要放进固定头的 `additional_info`？

**参考答案**：这是 UDT 协议的固定布局约定：msg id 是「单个标量」，恰好能塞进固定头里那个 4 字节的复用字段 `additional_info`；而 `DropRequestInfo` 的 control_info 只放「区间两端」`first/last_seq_number`。把标量塞固定头、把变长/成对数据塞 control_info，可以让 `DropRequest` 的 control_info 保持固定 8 字节，解析更简单。读取时用 `msg_seq_number()` 统一取回（见 4.2）。

---

## 5. 综合实践

把本讲四节串起来，完成一次「**手工拆解一个真实的握手控制包**」的练习。

**任务**：给定一段据称是「客户端发起的握手包」的完整 64 字节（16 字节固定头 + 48 字节 `HandShakeInfo`），手工走一遍 `UdtControlPacket::deserialize` 的解析过程，回答 6 个问题。

**给定字节**（十六进制，示例数据，非项目原生产数据）：

```
80 00 00 00  00 00 00 00  00 00 00 00  00 00 00 2A
00 00 00 05  00 00 00 01  00 00 03 E8  00 00 05 DC
00 00 20 00  00 00 00 01  11 22 33 44  00 00 00 00
7F 00 00 01  00 00 00 00  00 00 00 00  00 00 00 00
```

**操作步骤**：

1. **固定头（字节 0–15）**：
   - 字节 0–1 `80 00` → `0x8000 & 0x7FFF = 0x0000` → 类型码 `0` = **Handshake**；最高位 `1` 确认是控制包。
   - 字节 2–3 `00 00` → `reserved = 0`。
   - 字节 4–7 `00 00 00 00` → `additional_info = 0`（Handshake 不用它）。
   - 字节 8–11 → `timestamp = 0`。
   - 字节 12–15 `00 00 00 2A` → `dest_socket_id = 0x2A = 42`。
2. **control_info（字节 16–63）**，按 `HandShakeInfo::deserialize`：
   - 字节 16–19 `00 00 00 05` → `udt_version = 5`。
   - 字节 20–23 `00 00 00 01` → `socket_type = 1` = Stream。
   - 字节 24–27 `00 00 03 E8` → `initial_seq_number = 1000`。
   - 字节 28–31 `00 00 05 DC` → `max_packet_size = 1500`。
   - 字节 32–35 `00 00 20 00` → `max_window_size = 8192`。
   - 字节 36–39 `00 00 00 01` → `connection_type = 1`。
   - 字节 40–43 `11 22 33 44` → `socket_id = 0x11223344`。
   - 字节 44–47 `00 00 00 00` → `syn_cookie = 0`。
   - 字节 48–63：IP 字段。检查后 12 字节（字节 52–63）→ 全 `00` → **IPv4**，取字节 48–51 `7F 00 00 01` → `127.0.0.1`。
3. **回答问题**：
   1. 这是哪一类控制包？→ Handshake（类型码 0）。
   2. `dest_socket_id` 是多少？→ 42（这是「目标」socket，即 listener 的 socket id）。
   3. `connection_type = 1` 说明什么？→ 客户端发起的常规握手，listener 收到后会回 SYN cookie。
   4. `syn_cookie = 0` 说明什么？→ 客户端首轮握手尚未拿到 cookie，故填 0。
   5. 这是 IPv4 还是 IPv6？依据是什么？→ IPv4；依据是 IP 字段后 12 字节全零。
   6. 这个包由谁发送、listener 收到后会做什么？→ 由客户端发送；listener 会用 `compute_cookie` 算出 cookie 回填、把 `connection_type` 置 `-1` 回发（见 [src/socket.rs:385-392](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L385-L392)）。

**预期结果**：6 个问题都能从字节中读出答案，且与源码 `deserialize` 逻辑完全一致。如果想实测，可在 `UdtMultiplexer` 收包处或 `process_ctrl` 的 Handshake 分支临时加 `println!("{:02X?}", packet_bytes)`，跑 u1-l2 的 sender/receiver 抓第一个握手包——但属于「待本地验证」。

---

## 6. 本讲小结

- 控制包 = **16 字节固定头 + 变长 control_info**；固定头对七种类型统一，control_info 随类型而变。
- 固定头里 bit 0（最高位）= `1` 标志控制包，bits 1–15 是类型码；`additional_info` 是个「复用字段」，ACK 装 ACK 序号、DropRequest 装 msg id、其余为 0。
- `ControlPacketType` 有 7 种业务类型，编码表里 **`0x0004` 被跳过**（参考实现中预留给 Congestion Warning），`Shutdown` 沿用 `0x0005` 以保持线上兼容；`0x0004` 的包会被当作未知类型丢弃。
- 握手包最复杂：48 字节 `HandShakeInfo` 装下版本/类型/ISN/MSS/窗口/`connection_type`/socket_id/cookie/IP；其中 IP 字段用「后 12 字节是否全零」启发式区分 IPv4/IPv6。
- `connection_type` 三态：`1`（客户端首轮，listener 回 cookie）、`-1`（客户端回带 cookie，listener 校验）、`1002`（版本/类型不匹配时的拒绝码）。
- ACK 有 light（4 字节 control_info，只报水位）与 full（24 字节，附 RTT/带宽等拥塞反馈）两种；NAK 是 `Vec<u32>` 丢失条目载体（区间编码 `0x8000_0000` 由 socket.rs 处理）；DropRequest 的 msg id 复用固定头 `additional_info`。

---

## 7. 下一步学习建议

本讲把「控制包的线上格式」讲完了，但**这些包是如何被产生和处理的**还没展开。建议接下来按以下顺序学习：

- **u4-l4（序列号与循环算术）**：本讲频繁出现 `SeqNumber`/`AckSeqNumber`/`MsgNumber` 的 `.number()` 取值与 `MAX_NUMBER` 掩码。读完 u4-l4 你会彻底理解这些序号在 31/29 位循环空间上的加减法，以及为何 `Sub` 返回 `i32`——这对理解 NAK 区间比较至关重要。
- **u6-l2（接收数据与 ACK 生成）**：看 `send_ack` 何时发 light、何时发 full ACK，以及 ACK 序号如何配合 `ack_window`。
- **u6-l3（丢包检测与 NAK：LossList）**：看 `NakInfo` 的 `loss_info` 里 `0x8000_0000` 区间编码是如何在 socket.rs 中构造与解析的。
- **u6-l4（ACK2、AckWindow 与 RTT 测量）**：看 `Ack2` 包（本讲编码 `0x0006`）如何配合 `AckWindow` 完成 RTT 测量。
- **u8-l1（连接建立与握手：SYN cookie）**：完整复述 `connection_type` 1→-1 的握手往返与 `compute_cookie` 的 SYN cookie 防伪造机制。
