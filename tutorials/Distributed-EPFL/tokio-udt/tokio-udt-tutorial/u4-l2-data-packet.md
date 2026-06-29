# 数据包格式：包头、消息分片与位置标记

## 1. 本讲目标

本讲是第 4 单元「UDT 包的线上格式」的第二篇。上一篇（u4-l1）我们知道了 `UdtPacket` 用**首比特**区分数据包与控制包。本讲专门拆开数据包这只「黑盒」，学完后你应当能够：

1. 画出 UDT 数据包头（`UdtDataPacketHeader`）16 字节、128 位的逐字段位域布局，并说出每个字段的位宽与掩码。
2. 解释 `PacketPosition`（First / Last / Only / Middle）这套 2 比特编码如何标记一条消息被切分成几个包。
3. 读懂 `serialize` / `deserialize` 里的大端字节序（big-endian）与位运算实现，能手算一个具体包头在线上的 16 字节。
4. 说清楚 `seq_number` 为何只有 31 位、`msg_number` 为何只有 29 位——它们都和首比特的「数据/控制」判别位共享同一个 32 位字。

本讲**只**讲数据包的**线上格式**（wire format），不涉及它如何被发送、重传、确认——那些是第 5、6 单元的内容。

---

## 2. 前置知识

阅读本讲前，你需要了解：

- **UDT 数据包 vs 控制包**：UDT 跑在 UDP 之上，它自己又定义了两类包。承载用户数据的叫「数据包」；承载握手、ACK、NAK、Shutdown 等信令的叫「控制包」。上一篇 u4-l1 讲过，二者靠首字节最高位（`raw[0] >> 7`）区分：`0` 是数据包，`1` 是控制包。
- **大端字节序（big-endian）**：网络协议惯用的字节序，即「最高有效字节在前」。Rust 里 `u32::to_be_bytes()` / `u32::from_be_bytes()` 就是干这个的。本讲所有多字节字段都用大端。
- **位运算与位域（bit field）**：用一个 `u32` 的不同比特段塞下多个字段，靠「左移 `<<` 拼装、右移 `>>` + 掩码 `&` 拆解」实现。这是本讲的核心技巧。
- **Rust 的 `try_into().unwrap()`**：把 `&[u8]` 切片转换成定长数组 `[u8; 4]`。因为前面已经做了长度检查，这里 `unwrap` 不会 panic。
- **序列号类型**：`SeqNumber`（包序列号）、`MsgNumber`（消息号）是 crate 自定义的类型，背后是 31 位 / 29 位的「循环算术」整数（详见 u4-l4）。本讲只需要知道它们都能用 `.number()` 取出内部的 `u32`。

> 一句话复习：上一讲我们看到接收 worker 先 `UdtPacket::deserialize` 做首比特分发，为 `0` 时进入 `UdtDataPacket::deserialize`——这正是本讲要拆开的入口。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/data_packet.rs` | **本讲主角**。定义数据包 `UdtDataPacket`、包头 `UdtDataPacketHeader`、位置枚举 `PacketPosition`，以及它们的 `serialize` / `deserialize`。 |
| `src/common.rs` | 只有一个工具函数 `ip_to_bytes`。本讲会澄清：它**不**服务数据包，而是服务握手控制包。 |
| `src/packet.rs` | 上篇讲过的总入口。本讲引用它的首比特分发，作为数据包解析的「上游」。 |
| `src/seq_number.rs` | `SeqNumber`（31 位）与 `MsgNumber`（29 位）的定义，解释位宽的来源。 |
| `src/queue/snd_buffer.rs` | 真实构造 `UdtDataPacketHeader` 的地方，用来验证我们对字段语义的理解（尤其是 `position` 与 `in_order` 怎么来的）。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲数据包头 `UdtDataPacketHeader` 的位域布局，再讲消息分片标记 `PacketPosition`，最后把 `serialize` / `deserialize` 的位运算实现拆透。

### 4.1 数据包头 UdtDataPacketHeader 的位域布局

#### 4.1.1 概念说明

UDT 的数据包 = **16 字节固定包头** + **变长 payload**。固定包头里塞了 6 个字段，从「这个包是第几号」到「发给哪个 socket」，全部信息都在这 128 位里。之所以要精心设计位域，是因为 UDT 面向高速长肥管道（LFN），包头越短，有效载荷占比越高、吞吐越好。

整个包的结构定义在 [`src/data_packet.rs:7-11`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L7-L11)：

```rust
pub(crate) struct UdtDataPacket {
    pub header: UdtDataPacketHeader,
    pub data: Bytes,
}
```

`data` 是用户真正的字节流（payload），`header` 才是本讲的重点。包头常量 `UDT_DATA_HEADER_SIZE = 16` 定义在 [`src/data_packet.rs:5`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L5)。

#### 4.1.2 核心流程：128 位的字段排布

包头各字段的位域布局，直接对照源码注释 [`src/data_packet.rs:32-41`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L32-L41)：

| 比特位（wire） | 字段 | 位宽 | 类型 / 取值 | 说明 |
| --- | --- | --- | --- | --- |
| bit 0 | 标志位 | 1 | 固定 `0` | 数据包标志；控制包此位为 `1`（首比特 dispatch）|
| bits 1–31 | `seq_number` | 31 | `SeqNumber`，`[0, 2^31-1]` | 包序列号，按发送顺序递增（循环）|
| bits 32–33 | `position` | 2 | `PacketPosition` | 消息分片位置标记（见 4.2）|
| bit 34 | `in_order` | 1 | `bool` | 该消息是否要求按序交付 |
| bits 35–63 | `msg_number` | 29 | `MsgNumber`，`[0, 2^29-1]` | 消息号；同一条消息的多个分片共享一个 msg_number |
| bits 64–95 | `timestamp` | 32 | `u32` | 时间戳，单位微秒（相对连接起点）|
| bits 96–127 | `dest_socket_id` | 32 | `u32` | **目标** socket id（接收方用来解复用）|

校验一下位宽总和：

\[ 1 + 31 + 2 + 1 + 29 + 32 + 32 = 128 \text{ bit} = 16 \text{ byte} \]

正好 16 字节，与 `UDT_DATA_HEADER_SIZE` 吻合。

**两个容易踩坑的位宽问题**：

1. **为什么 `seq_number` 是 31 位而不是 32 位？** 因为它和「数据/控制」标志位共享第一个 32 位字：bit 0 留给标志位，bits 1–31 才是序列号。所以 [`src/data_packet.rs:51`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L51) 反序列化时要 `& 0x7fffffff` 把最高位（标志位）掩掉：

   ```rust
   let seq_number = u32::from_be_bytes(raw[0..4].try_into().unwrap()) & 0x7fffffff;
   ```

   这里 `0x7fffffff` 的二进制是 `0111...1111`（31 个 1），正好保留低 31 位。这也和 `SeqNumber` 的最大值 `0x7fff_ffff` 一致（见 [`src/seq_number.rs:87-92`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L87-L92)）。

2. **为什么 `msg_number` 是 29 位？** 因为它和 `position`（2 位）、`in_order`（1 位）共享第二个 32 位字：`2 + 1 + 29 = 32`。所以反序列化时用 `& 0x1fffffff`（低 29 位）取出它，见 [`src/data_packet.rs:54`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L54)；对应的 `MsgNumber` 最大值 `0x1fff_ffff` 见 [`src/seq_number.rs:103-110`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs#L103-L110)。

**几个字段的语义补充**：

- `dest_socket_id`：注意是**目标**（对端）socket id，不是自己的。它就是上一篇 u4-l1 里 `get_dest_socket_id` 取出来的解复用键——接收方靠它把包投递给正确的 `UdtSocket`。
- `timestamp`：由发送方写入，值为 `(start_time.elapsed().as_micros() & u32::MAX)`，即「自连接起点以来流逝的微秒数」截断到 32 位（见 [`src/queue/snd_buffer.rs:45`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L45)）。接收方用它做包到达速率 / 带宽估计（留待 u7-l1 的 `UdtFlow`）。
- `in_order`：标记本消息是否要求按序交付，属于 UDT「messaging 服务」的语义。在当前实现里它由发送缓冲 `SndBuffer::add_message` 的参数透传进包头（见 [`src/queue/snd_buffer.rs:19`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L19) 与 [`src/queue/snd_buffer.rs:43`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L43)）。

#### 4.1.3 源码精读

包头结构体本身（含逐字段位注释）见 [`src/data_packet.rs:32-41`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L32-L41)：

```rust
pub(crate) struct UdtDataPacketHeader {
    // bit 0 = 0
    pub seq_number: SeqNumber,    // bits 1-31
    pub position: PacketPosition, // bits 32-33
    pub in_order: bool,           // bit 34
    pub msg_number: MsgNumber,    // bits 35-63
    pub timestamp: u32,           // bits 64-95
    pub dest_socket_id: u32,      // bits 96-127
}
```

注意 `seq_number` 字段**不包含** bit 0 那个标志位——注释 `// bit 0 = 0` 是在提醒：线上第一个字的最高位恒为 0，序列号只占 bits 1–31。

`UdtDataPacket::deserialize` 在 [`src/data_packet.rs:14-18`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L14-L18) 把包头和 payload 切开：

```rust
pub fn deserialize(raw: &[u8]) -> Result<Self> {
    let header = UdtDataPacketHeader::deserialize(&raw[..UDT_DATA_HEADER_SIZE])?;
    let data = Bytes::copy_from_slice(&raw[UDT_DATA_HEADER_SIZE..]);
    Ok(Self { header, data })
}
```

即「前 16 字节是头，其余是 payload」。对应的 `serialize` 在 [`src/data_packet.rs:24-29`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L24-L29) 反向拼回，`Vec::with_capacity(1500)` 这个预分配容量就是典型的 MSS（最大分段大小，默认 1500，见 u1-l4）。

#### 4.1.4 代码实践

> **实践目标**：把上面那张位域表「读活」——不写代码，只用手和纸，从字段值反推出线上 16 字节。
>
> **操作步骤**：
> 1. 取一个具体包头：`seq_number = 10`、`position = Only`（编码 `11`，见 4.2）、`in_order = false`、`msg_number = 3`、`timestamp = 0`、`dest_socket_id = 0x01020304`。
> 2. 按字节推算：
>    - 第一个字（bytes[0..4]）只放 `seq_number`，最高位（标志位）必须为 0。`10` 的大端表示是 `00 00 00 0A`。
>    - 第二个字（bytes[4..8]）打包 `position | in_order | msg_number`（打包公式见 4.3）。
>    - 第三个字（bytes[8..12]）是 `timestamp = 0` → `00 00 00 00`。
>    - 第四个字（bytes[12..16]）是 `dest_socket_id = 0x01020304` → `01 02 03 04`。
>
> **需要观察的现象 / 预期结果**：第二个字怎么算？等学完 4.3 的打包公式后你会得到 `C0 00 00 03`，于是完整 16 字节为：
>
> ```
> 00 00 00 0A | C0 00 00 03 | 00 00 00 00 | 01 02 03 04
> ```
>
> 4.3 节会给出验证，并确认这串字节能被 `deserialize` 还原回原值。

#### 4.1.5 小练习与答案

**练习 1**：`seq_number` 字段在源码里存的是 31 位还是 32 位？为什么反序列化要 `& 0x7fffffff`？

> **答案**：31 位。因为它和「数据/控制」标志位（bit 0）共享第一个 32 位字；`& 0x7fffffff` 把最高位（标志位）掩掉，只留下 bits 1–31 的序列号。

**练习 2**：`msg_number`、`position`、`in_order` 三者位宽之和是多少？为什么正好等于 32？

> **答案**：`29 + 2 + 1 = 32`。因为它们共享第二个 32 位字。

**练习 3**：`dest_socket_id` 描述的是「自己的」还是「对端的」socket id？接收方拿到它之后做什么？

> **答案**：是**目标（对端）**socket id。接收方用它作为解复用键，把包投递给对应的 `UdtSocket`（即 u4-l1 里 `get_dest_socket_id` 的用途）。

---

### 4.2 消息分片标记 PacketPosition

#### 4.2.1 概念说明

UDT 同时支持「数据流（streaming）」和「消息（messaging）」两种服务。在 messaging 模式下，应用可能一次 `send` 一条很大的消息（比如几 MB），超过单个包的 MSS，于是发送方要把这条消息**切成多个数据包**发出。接收方需要知道：

- 这条消息从哪个包开始、到哪个包结束？
- 某个包是消息的「开头」「结尾」「中间」还是「独占一包」？

这就是 `PacketPosition` 解决的问题——用 **2 个比特**标记一个包在所属消息里的位置。同一条消息的所有分片共享同一个 `msg_number`，靠 `position` 区分首尾。

#### 4.2.2 核心流程：四种位置与编码

`PacketPosition` 是一个枚举，定义在 [`src/data_packet.rs:82-88`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L82-L88)：

```rust
pub(crate) enum PacketPosition {
    First = 2,
    Last = 1,
    Only = 3,
    Middle = 0,
}
```

把它的「枚举值」当成 2 位二进制看，就是 wire 上的编码：

| 枚举变体 | 数值 | 2 位编码 | 含义 |
| --- | --- | --- | --- |
| `Middle` | 0 | `00` | 消息的中间分片（既非首也非尾）|
| `Last` | 1 | `01` | 消息的最后一个分片 |
| `First` | 2 | `10` | 消息的第一个分片 |
| `Only` | 3 | `11` | 整条消息只有一个包（首尾合一）|

注意一个**反直觉**点：枚举值不是按 `First=0, Middle=1, ...` 这种「自然顺序」排的，而是刻意让 `Only = 3 = 0b11`、`Middle = 0 = 0b00`。这其实很巧妙——`Only`（独占）的两位都置 1，相当于「既是 First 又是 Last」；`Middle`（中间）两位都置 0，相当于「既不是 First 也不是 Last」。从线上的 2 位编码看：

- 高位为 1 ⇒ 是消息开头（First / Only）
- 低位为 1 ⇒ 是消息结尾（Last / Only）

`First` 的值 `2 = 0b10` 用 wire bits 32–33 表示时，bit 32（高位）= 1、bit 33（低位）= 0，正好「是开头、不是结尾」。这种编码让接收方用简单的位测试就能判断边界。

> 注意：wire bits 32–33 里，**bit 32 是 position 的高位**（对应「是否 First」），**bit 33 是低位**（对应「是否 Last」）。因为 serialize 时 `position` 左移了 30 位塞进 32 位字的最高 2 位（见 4.3）。

#### 4.2.3 源码精读

反序列化时，从第二个字节的最高 2 位取出 position，再用 `TryFrom<u8>` 转成枚举，见 [`src/data_packet.rs:52`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L52)：

```rust
let position: PacketPosition = ((raw[4] & 0b11000000) >> 6).try_into()?;
```

- `raw[4] & 0b11000000`：掩出 byte[4] 的最高 2 位（即 wire bits 32–33）。
- `>> 6`：右移到最低 2 位，得到 `0..=3` 的数值。
- `.try_into()?`：调用下面的 `TryFrom`，见 [`src/data_packet.rs:90-105`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L90-L105)：

```rust
fn try_from(raw_position: u8) -> Result<Self> {
    match raw_position {
        0b10 => Ok(PacketPosition::First),
        0b01 => Ok(PacketPosition::Last),
        0b11 => Ok(PacketPosition::Only),
        0b00 => Ok(PacketPosition::Middle),
        _ => Err(...),
    }
}
```

理论上 2 位只有 4 种取值，`_` 分支永远不会命中，但 Rust 的 `match` 要求穷尽，这里也顺手防御了非法输入。

**`position` 是怎么被真实赋值的？** 看发送缓冲 `SndBuffer::add_message` 切分消息的代码 [`src/queue/snd_buffer.rs:88-98`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L88-L98)：

```rust
position: {
    if idx == 0 && chunks_len == 1 {
        PacketPosition::Only
    } else if idx == 0 {
        PacketPosition::First
    } else if idx == chunks_len - 1 {
        PacketPosition::Last
    } else {
        PacketPosition::Middle
    }
},
```

逻辑非常直白：把消息按 `payload_size`（即 MSS）切成 `chunks`，对每个分片按它的下标 `idx` 与总片数 `chunks_len` 判定位置——只有一片就是 `Only`；第一片是 `First`；最后一片是 `Last`；其余是 `Middle`。同一消息的所有分片共享同一个 `msg_number`（见 [`src/queue/snd_buffer.rs:84`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L84)）。这条数据流贯穿「应用一次 send → 切成多个包 → 每个包带 position/msg_number」的完整链路，是本讲与第 5 单元（发送数据通路）的衔接点。

#### 4.2.4 代码实践

> **实践目标**：理解 `position` 编码与消息切分的关系。
>
> **操作步骤**：
> 1. 假设 MSS（payload_size）= 1500 字节，应用一次 `send` 了一条 4000 字节的消息。它会被切成几片？每片的 `position` 分别是什么？
> 2. 阅读 [`src/queue/snd_buffer.rs:74-75`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_buffer.rs#L74-L75) 的 `data.chunks(self.payload_size)`，确认你的切分结果。
>
> **预期结果**：4000 / 1500 ⇒ 3 片（1500 + 1500 + 1000）。`idx=0` 命中 `idx==0 && chunks_len!=1` ⇒ `First`；`idx=1` 命中 else ⇒ `Middle`；`idx=2` 命中 `idx==chunks_len-1` ⇒ `Last`。即 `position` 序列为 `[First, Middle, Last]`，三者共享同一个 `msg_number`。
>
> **待本地验证**：若想真跑，可在 u1-l2 的 `udt_sender` 里把单次写入改大（超过 MSS），再用抓包工具（如 `tcpdump` UDP）观察同 `msg_number` 的连续包的 position 字段变化。

#### 4.2.5 小练习与答案

**练习 1**：一条恰好等于 MSS 的消息（一片）会带什么 `position`？

> **答案**：`Only`（`0b11`）。代码里 `idx == 0 && chunks_len == 1` 命中第一个分支。

**练习 2**：为什么 `Only` 的枚举值是 `3`，而不是 `0`？

> **答案**：`3 = 0b11`，表示「既是 First 又是 Last」；高位和低位都置 1。这样接收方用「高位测首、低位测尾」就能统一处理，`Only` 自然落在「既是首也是尾」的位置。

**练习 3**：反序列化 position 时 `raw[4] & 0b11000000` 取的是哪两个 wire bit？为什么再 `>> 6`？

> **答案**：取 wire bits 32–33（byte[4] 的最高 2 位），它们对应 `position` 字段。`>> 6` 把这两位移到最低 2 位，得到 `0..=3` 的数值，供 `TryFrom` 匹配。

---

### 4.3 serialize / deserialize 的位运算实现

#### 4.3.1 概念说明

位域布局是「设计」，`serialize` / `deserialize` 是「实现」。核心就两件事：

- **打包（serialize）**：把多个字段用左移 `<<` 拼到一个 `u32` 里，再按大端写入字节。
- **拆包（deserialize）**：按大端读出 `u32`，用掩码 `&` 和右移 `>>` 把各字段拆出来。

难点集中在**第二个 32 位字**——它塞了 `position`（2 位）、`in_order`（1 位）、`msg_number`（29 位）三个字段。第一个字相对简单（只有一个 31 位 `seq_number` + 1 位标志位）。

#### 4.3.2 核心流程：打包与拆包公式

第二个字的**打包公式**（见 [`src/data_packet.rs:71-73`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L71-L73)）：

```rust
let block: u32 = ((self.position as u32) << 30)
    + ((self.in_order as u32) << 29)
    + self.msg_number.number();
```

用数学写清楚（设 \(p\) = position，\(o\) = in_order，\(m\) = msg_number）：

\[
\text{block} = (p \ll 30) \;+\; (o \ll 29) \;+\; m
\]

各字段的「落位」：

| 字段 | 移位 | 落在 32 位字的哪些 bit |
| --- | --- | --- |
| `position`（2 位）| `<< 30` | bits 30–31（最高 2 位）|
| `in_order`（1 位）| `<< 29` | bit 29 |
| `msg_number`（29 位）| 不移位 | bits 0–28 |

三者位段不重叠，相加等价于按位或（代码用 `+`，因为不重叠时两者结果相同）。

对应的**拆包**（见 [`src/data_packet.rs:52-54`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L52-L54)）：

```rust
let position: PacketPosition = ((raw[4] & 0b11000000) >> 6).try_into()?;
let in_order = (raw[4] & 0b00100000) != 0;
let msg_number = u32::from_be_bytes(raw[4..8].try_into().unwrap()) & 0x1fffffff;
```

- `position`：`raw[4] & 0b11000000` 取 byte[4] 最高 2 位，`>> 6` 落到最低 2 位。
- `in_order`：`raw[4] & 0b00100000` 取 byte[4] 的 bit 5（即 32 位字的 bit 29），非零即真。
- `msg_number`：把整个 bytes[4..8] 当 `u32` 读出，`& 0x1fffffff`（低 29 位）取出。

> 为什么 `position` / `in_order` 只对 `raw[4]`（单字节）操作，而 `msg_number` 要读 `raw[4..8]`（4 字节）？因为 `position` 和 `in_order` 都落在最高字节 byte[4] 里；而 `msg_number` 横跨 byte[4] 的低 5 位 + byte[5..8] 全部，所以干脆把 4 字节整体读出再掩码。

`timestamp` 和 `dest_socket_id` 各自独占一个 32 位字，没有位域混合，直接 `to_be_bytes` / `from_be_bytes` 即可（见 serialize 的 [`src/data_packet.rs:76-77`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L76-L77) 与 deserialize 的 [`src/data_packet.rs:55-56`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L55-L56)）。

#### 4.3.3 源码精读

完整的 `serialize` 见 [`src/data_packet.rs:67-79`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L67-L79)：

```rust
pub fn serialize(&self) -> Vec<u8> {
    let mut buffer: Vec<u8> = Vec::with_capacity(UDT_DATA_HEADER_SIZE);
    buffer.extend_from_slice(&self.seq_number.number().to_be_bytes());

    let block: u32 = ((self.position as u32) << 30)
        + ((self.in_order as u32) << 29)
        + self.msg_number.number();

    buffer.extend_from_slice(&block.to_be_bytes());
    buffer.extend_from_slice(&self.timestamp.to_be_bytes());
    buffer.extend_from_slice(&self.dest_socket_id.to_be_bytes());
    buffer
}
```

注意第一个字只写了 `seq_number.number().to_be_bytes()`，**没有显式写 bit 0 的 0**。这之所以正确，是因为 `SeqNumber::number()` 最大为 `0x7fff_ffff`，最高位恒为 0——也就是说，序列号自身的 31 位天然就把标志位「占」成了 0。这是一个隐式约定：数据包的标志位由 `seq_number` 字段的位宽保证，而不是单独写一位。

完整的 `deserialize` 见 [`src/data_packet.rs:44-65`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L44-L65)，开头有一道长度防线 [`src/data_packet.rs:45-50`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L45-L50)：

```rust
if raw.len() < 16 {
    return Err(Error::new(ErrorKind::InvalidData, "data packet header is too short"));
}
```

不足 16 字节直接报 `InvalidData`。注意：能走到这里说明 `UdtPacket::deserialize` 已经判定首比特为 0（是数据包），见 [`src/packet.rs:33-37`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L33-L37)；这道长度检查是数据包自己的第二道防线。

最后用 `.into()` 把裸 `u32` 包回 `SeqNumber` / `MsgNumber`（见 [`src/data_packet.rs:58-61`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L58-L61)），它们的循环算术语义留到 u4-l4。

#### 4.3.4 代码实践（承接 4.1.4 的手算）

> **实践目标**：验证 4.1.4 里手算的 16 字节，并回答「`ip_to_bytes` 服务哪类包」。
>
> **操作步骤**：
> 1. 复用 4.1.4 的取值：`position = Only(3)`、`in_order = false(0)`、`msg_number = 3`、`seq = 10`、`timestamp = 0`、`dest_socket_id = 0x01020304`。
> 2. 用 4.3.2 的打包公式算第二个字：
>    \[ \text{block} = (3 \ll 30) + (0 \ll 29) + 3 = \texttt{0xC0000000} + 0 + 3 = \texttt{0xC0000003} \]
>    大端字节序：`C0 00 00 03`。
> 3. 拼出完整 16 字节（每个字都按大端）：
>
>    ```
>    seq=10        block         timestamp=0   dest=0x01020304
>    00 00 00 0A | C0 00 00 03 | 00 00 00 00 | 01 02 03 04
>    ```
> 4. **反向验证**（用 deserialize 的逻辑读回）：
>    - `seq_number = 0x0000000A & 0x7fffffff = 10` ✓
>    - `position = (0xC0 & 0b11000000) >> 6 = 0xC0 >> 6 = 3 = Only` ✓
>    - `in_order = (0xC0 & 0b00100000) != 0 = false` ✓
>    - `msg_number = 0xC0000003 & 0x1fffffff = 3` ✓
>    - `timestamp = 0` ✓
>    - `dest_socket_id = 0x01020304` ✓
>
> 全部还原成功，证明手算与源码一致。
>
> 5. **回答 `ip_to_bytes` 的问题**：在仓库里搜索 `ip_to_bytes`，它只在 [`src/control_packet.rs:2`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L2) 被 import，唯一调用点在握手信息序列化 [`src/control_packet.rs:244`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L244)：
>
>    ```rust
>    .chain(ip_to_bytes(self.ip_address))
>    ```
>    而它的实现在 [`src/common.rs:3-12`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/common.rs#L3-L12)，把 `IpAddr`（IPv4 左对齐补零到 16 字节，IPv6 直接 16 字节）转成定长字节数组。
>
> **结论**：`ip_to_bytes` 服务的是**握手控制包**（`HandShakeInfo`，承载对端 IP），**不是**数据包。数据包头里根本没有 IP 字段——它只有 `dest_socket_id` 这种逻辑标识。这也呼应 u4-l1：IP 寻址由底层 UDP 负责，UDT 层只认 socket id。
>
> **待本地验证**：以上字节推算可手动复核；若要在机器上验证，可在 crate 内部临时加一个 `#[cfg(test)]` 单测构造 `UdtDataPacketHeader` 并断言 `serialize()` 的输出等于上面的 16 字节（注意 `UdtDataPacketHeader` 是 `pub(crate)`，只能在 crate 内测试，不能从外部 crate 构造）。本仓库目前没有 data_packet 的现成单测（无 `tests/` 目录），需要你自行添加临时测试，**测完请还原，勿提交对源码的改动**。

#### 4.3.5 小练习与答案

**练习 1**：`serialize` 里第一个字为什么没有显式写 bit 0 的 `0`？

> **答案**：因为 `seq_number` 字段只有 31 位（最大 `0x7fff_ffff`），它的最高位天然为 0，正好充当数据包标志位。不需要单独写一位。

**练习 2**：把 `position = First(2)`、`in_order = true(1)`、`msg_number = 5` 打包，第二个字的值是多少？

> **答案**：\((2 \ll 30) + (1 \ll 29) + 5 = \texttt{0x80000000} + \texttt{0x20000000} + 5 = \texttt{0xA0000005}\)，大端字节 `A0 00 00 05`。验证：byte[4]=`0xA0`=`1010_0000`，最高 2 位 `10` ⇒ First ✓；bit 5（`0x20` 位）= 1 ⇒ in_order=true ✓；`0xA0000005 & 0x1fffffff = 5` ✓。

**练习 3**：`deserialize` 里 `msg_number` 为什么要读 `raw[4..8]` 整个 4 字节、而 `position` 只读 `raw[4]` 一个字节？

> **答案**：`position` 和 `in_order` 只占第二个 32 位字的最高 3 位（落在 byte[4]），所以单字节操作即可；而 `msg_number` 横跨 byte[4] 的低 5 位加上 byte[5..8] 全部，必须把 4 字节整体读出成 `u32` 再用 `& 0x1fffffff` 取低 29 位。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**全手工协议编解码**」：

**任务**：你是一名 UDT 协议调试者，手上抓到一个数据包的前 16 字节（十六进制）：

```
80 00 00 05  40 00 00 02  00 00 07 D0  12 34 56 78
```

请仅凭本讲学到的位域布局与拆包公式，回答：

1. 这是一个数据包还是控制包？为什么？（提示：看第一个字节的最高位。）
2. `seq_number`、`position`、`in_order`、`msg_number`、`timestamp`、`dest_socket_id` 各是多少？
3. 这条消息大概率是单包消息还是多包消息？依据是什么？

**参考答案**：

1. 第一个字节 `0x80` = `1000_0000`，最高位（bit 0）= **1**，所以这其实是一个**控制包**，不是数据包！它会被 `UdtPacket::deserialize` 走 `first_bit == true` 分支（[`src/packet.rs:34-37`](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L34-L37)）交给 `UdtControlPacket::deserialize`，**不会**进入本讲的 `UdtDataPacket::deserialize`。这个「陷阱」正是 u4-l1 首比特 dispatch 的意义：数据包的 bit 0 恒为 0。
2. 由于它是控制包，用数据包格式去拆是**错误**的——但如果你机械套用本讲公式，会得到 `seq_number = 0x80000005 & 0x7fffffff = 5`，这只是巧合性的数字，并不代表真实语义。真实字段布局见下一讲 u4-l3（控制包格式）。
3. 无意义（前提不成立）。

**换个真能用本讲公式解的数据包**（bit 0 = 0）：

```
00 00 00 05  40 00 00 02  00 00 07 D0  12 34 56 78
```

- 标志位：byte[0]=`0x00` 最高位 = 0 ⇒ **数据包** ✓
- `seq_number` = `0x00000005 & 0x7fffffff = 5`
- byte[4]=`0x40`=`0100_0000`：`position = (0x40 & 0xC0) >> 6 = 0x40 >> 6 = 1` ⇒ **Last**；`in_order = (0x40 & 0x20) != 0 = false`
- `msg_number` = `0x40000002 & 0x1fffffff = 2`
- `timestamp` = `0x000007D0 = 2000`（µs）
- `dest_socket_id` = `0x12345678`
- `position = Last` ⇒ 这是某条多包消息的**最后一个分片**（不是单包消息）。

> 通过这个「正反两个例子」的对比，你应该深刻记住：**bit 0 是数据包与控制包的总开关**，拆包前必须先判首比特——这正是上一讲 u4-l1 的核心，也是本讲 `deserialize` 能够成立的前提。

---

## 6. 本讲小结

- UDT 数据包 = **16 字节固定包头** + 变长 payload；包头 128 位塞了 6 个字段：`seq_number`(31b)、`position`(2b)、`in_order`(1b)、`msg_number`(29b)、`timestamp`(32b)、`dest_socket_id`(32b)，加 1 位数据/控制标志位正好 128 位。
- `seq_number` 是 31 位（与标志位共享首字）、`msg_number` 是 29 位（与 `position`/`in_order` 共享次字），位宽由共享字决定；反序列化分别用 `& 0x7fffffff`、`& 0x1fffffff` 取出。
- `PacketPosition` 用 2 位编码消息分片位置：`Middle=00`、`Last=01`、`First=10`、`Only=11`；`Only=11` 表示「既是首也是尾」，由 `SndBuffer::add_message` 按 `data.chunks(MSS)` 切片时赋值，同一消息的分片共享一个 `msg_number`。
- `serialize` 用左移 `<<` 把多字段打包进一个 `u32`（公式 \(\text{block}=(p\ll30)+(o\ll29)+m\)），再大端写入；`deserialize` 用掩码 `&` + 右移 `>>` 拆回。所有多字节字段用大端字节序。
- 标志位（bit 0）并不单独写入，而是由 `seq_number` 的 31 位位宽天然保证最高位为 0——这是数据包编解码的一个隐式约定。
- `ip_to_bytes`（`src/common.rs`）只服务**握手控制包** `HandShakeInfo`，与数据包无关；数据包层没有 IP 字段，寻址靠 `dest_socket_id`，IP 由底层 UDP 负责。

---

## 7. 下一步学习建议

本讲把数据包格式拆透了。建议接下来：

1. **u4-l3 控制包格式**：去看 `UdtControlPacket` 的 16 字节固定头与 `ControlPacketType` 编码表，对比数据包——你会发现二者都是 16 字节头、都用首比特区分，但字段布局完全不同（控制包的 bit 0 = 1，且握手包才用到 `ip_to_bytes`）。综合实践里那个 `0x80...` 的「陷阱」会在那里得到正解。
2. **u4-l4 序列号与循环算术**：本讲多次出现的 `SeqNumber`（31 位）、`MsgNumber`（29 位）背后是 `GenericSeqNumber` 的循环加减法（`rem_euclid`、`Sub` 返回 `i32`），这是理解丢包检测、ACK、重传的算术基础。
3. **u5-l1 发送数据通路**：去看 `SndBuffer::add_message`（本讲 4.2.3 引用过）如何把用户消息切片、赋 `position`/`msg_number`，并由 `next_data_packets` 真正发出——把本讲的「格式」接上「发送流程」。
