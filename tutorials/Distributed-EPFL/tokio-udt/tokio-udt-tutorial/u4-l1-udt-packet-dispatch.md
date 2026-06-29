# UdtPacket：数据/控制包统一入口

## 1. 本讲目标

本讲是「第 4 单元：UDT 包的线上格式」的第一篇。在前面三个单元里，我们已经认识了 tokio-udt 的公共 API（`UdtListener`/`UdtConnection`/`UdtConfiguration`）、全局单例 `Udt`、`UdtSocket` 状态机，以及共享 UDP 的多路复用器 `UdtMultiplexer`。但这些都还停留在「架构」层面——我们一直把「一个 UDT 包」当成一个黑盒。

从本讲开始，我们要打开这个黑盒。学完本讲后，你应该能够：

- 说出 `UdtPacket` 这个枚举在整个 crate 里扮演的「统一入口」角色，以及它为什么只有两个变体 `Control` 和 `Data`。
- 看懂 `UdtPacket::deserialize` 如何**仅凭第一个字节的最高位**就把一段原始字节分流到「数据包」或「控制包」两条解析路径。
- 解释 `serialize`、`get_dest_socket_id`、`handshake` 三个方法的分发逻辑，以及它们在接收 worker（`rcv_queue.worker`）的「收包 → 解复用 → 路由」流程里各自被用在哪一步。

本讲**只讲入口层的分发**，不深入数据包/控制包内部的字段布局——那是 u4-l2 和 u4-l3 的内容。本讲要建立的是一张「总览图」：所有 UDT 线上字节，无论多么复杂，都先经过 `UdtPacket` 这一道门。

## 2. 前置知识

阅读本讲前，你最好已经具备以下概念（前序讲义已覆盖，这里只做一句回顾）：

- **UDT 跑在 UDP 之上**：UDP 只提供「把一段字节尽力发给对端」，不保证可靠、不保证顺序。UDT 在应用层自己实现可靠性（ACK/NAK/重传）和拥塞控制。这一切都靠收发自定义的「UDT 包」完成。（见 u1-l1）
- **多路复用器共享一个 UDP socket**：多个 `UdtSocket` 可以复用同一个底层 UDP socket 收发。接收 worker 收到一个 UDP 数据报后，需要判断「这个包该交给哪个 socket」。判断依据就是 UDT 包头里的 `dest_socket_id` 字段。（见 u3-l3）
- **大端字节序（big-endian）**：UDT 线上格式统一用大端序传输多字节整数，即高位字节在前。例如 `0x8001` 在线上就是两个字节 `0x80 0x01`。
- **位运算与掩码**：`>> 7` 表示把一个 `u8` 右移 7 位，结果是 0 或 1，正好取出最高位。`& 0x7FFF` 表示保留低 15 位、清掉最高位。这些是解析定长二进制协议的常见手段。

如果你对「为什么要在应用层自己实现可靠性」还有疑问，建议先回看 u1-l1。

## 3. 本讲源码地图

本讲核心只围绕一个文件，但会顺带提及其上下游：

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `src/packet.rs` | 定义 `UdtPacket` 枚举及其 `serialize/deserialize/get_dest_socket_id/handshake`，是本讲的绝对主角。 | ✅ 精读 |
| `src/queue/rcv_queue.rs` | 接收 worker：调用 `deserialize` 解包、用 `get_dest_socket_id` 解复用、用 `handshake` 路由握手包。演示「入口」如何被使用。 | 部分（仅 worker 主循环） |
| `src/socket.rs` | `process_packet` 在拿到解复用后的包后，对 `Control`/`Data` 再次 match 分发到 `process_ctrl`/`process_data`。 | 仅看分发那一小段 |
| `src/data_packet.rs` | `UdtDataPacket` 的完整定义。本讲只引用其「首比特 = 0」的约定和 `deserialize` 入口。 | 略读（u4-l2 精读） |
| `src/control_packet.rs` | `UdtControlPacket` 的完整定义。本讲只引用其「首比特 = 1」的约定和 type 编码入口。 | 略读（u4-l3 精读） |

一句话定位：`packet.rs` 是**协议格式的统一前门**，`data_packet.rs` 和 `control_packet.rs` 是门后的**两条走廊**，`rcv_queue.rs` 和 `socket.rs` 则是**来访登记处**（决定把客人引向哪条走廊、交给哪个房间）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，恰好对应规格要求：

1. **UdtPacket 枚举**：为什么需要这个统一类型，它有哪些方法。
2. **首比特 dispatch**：仅用一比特区分数据包与控制包的精妙设计。
3. **序列化/反序列化分发与接收路由**：`serialize`/`deserialize` 的 match 分发，以及 `get_dest_socket_id`/`handshake` 在接收流程里的作用。

### 4.1 UdtPacket 枚举：统一入口的设计

#### 4.1.1 概念说明

UDT 协议线上只有两大类包：

- **数据包（Data Packet）**：承载真正要传的用户数据，是「搬运工」。
- **控制包（Control Packet）**：承载协议自身的信令，例如握手（Handshake）、确认（ACK）、否定确认（NAK）、关闭（Shutdown）、保活（KeepAlive）等，是「调度员」。

这两类包**共用同一个 UDP 数据报通道**，对端收到的只是一段原始字节，必须先判断「这是数据还是控制」，再分别解析。

`UdtPacket` 就是把这两类包捏合在一起的枚举。它的存在解决了两个工程问题：

- **类型统一**：接收侧、发送侧、测试代码只需要面对一个类型 `UdtPacket`，不必到处写 `Either<UdtDataPacket, UdtControlPacket>` 之类的临时类型。
- **集中分发**：所有「按类型走不同分支」的逻辑（解析、序列化、取目标 socket id、判断是否握手包）都集中在 `impl UdtPacket` 里，避免散落各处。

需要特别说明的是：`UdtPacket` 是 `pub(crate)` 的（模块 `mod packet;` 在 [src/lib.rs:78](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs#L78) 声明为私有），也就是说它是 crate 内部的「实现细节」，**不在公共 API 里**。用户用 `UdtConnection` 收发数据时，完全感知不到 `UdtPacket` 的存在——这是有意的封装。

#### 4.1.2 核心流程

`UdtPacket` 的生命周期可以画成一条简单的双向流水线：

```text
           发送方向 (出网)
真实业务 ──▶ UdtDataPacket  ─┐
                             ├──▶ UdtPacket ──▶ serialize() ──▶ Vec<u8> ──▶ UDP socket
协议信令 ──▶ UdtControlPacket ─┘

           接收方向 (入网)
UDP socket ──▶ &[u8] ──▶ deserialize() ──▶ UdtPacket ──▶ match ──▶ { Control(p) | Data(p) }
                                                                  │
                                                                  ├─ get_dest_socket_id()  决定交给哪个 socket
                                                                  └─ handshake()           判断是不是握手包
```

关键点：`UdtPacket` 是一个**交汇点**——发送时两类包在这里汇合成字节流，接收时字节流在这里分裂成两类包。它自己几乎不含算法，只做「分发」和「委托」。

#### 4.1.3 源码精读

枚举定义极其简洁，只有两个变体：

[packet.rs:5-9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L5-L9) — `UdtPacket` 枚举，`Control` 持有 `UdtControlPacket`，`Data` 持有 `UdtDataPacket`：

```rust
#[derive(Debug)]
pub(crate) enum UdtPacket {
    Control(UdtControlPacket),
    Data(UdtDataPacket),
}
```

为了让构造 `UdtPacket` 更顺手，源码还实现了两个 `From` 转换。这样发送侧可以直接 `.into()` 把一个具体包转成统一的 `UdtPacket`：

[packet.rs:52-62](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L52-L62) — `From<UdtControlPacket>` 和 `From<UdtDataPacket>`，让 `ctrl.into()` / `data.into()` 直接得到 `UdtPacket`。

`impl UdtPacket` 一共暴露四个方法，本模块先看它们各自的「职责一句话」，逐行精读放到 4.2 和 4.3：

| 方法 | 职责一句话 | 位置 |
| --- | --- | --- |
| `get_dest_socket_id(&self) -> u32` | 无论哪种包，都从包头里取出「目标 socket id」（接收解复用的关键）。 | [packet.rs:12-17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L12-L17) |
| `serialize(&self) -> Vec<u8>` | 把包序列化成线上字节，内部分发到两个子类型的 `serialize`。 | [packet.rs:19-24](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L19-L24) |
| `deserialize(&[u8]) -> Result<Self>` | 从字节反序列化，**用首比特决定走哪条解析路径**。 | [packet.rs:26-39](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L26-L39) |
| `handshake(&self) -> Option<&HandShakeInfo>` | 只有「控制包里的 Handshake」才返回 `Some`，其余一律 `None`。 | [packet.rs:41-49](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L41-L49) |

可以看到，这四个方法都是**对 `match self` 的薄封装**：`UdtPacket` 自己不解析字节、不计算 RTT，它只负责「把请求转交给正确的子类型」。这种「门面 + 委托」是协议解析代码里非常典型的分层。

#### 4.1.4 代码实践

> **实践目标**：在源码里建立「`UdtPacket` 是所有线上包的唯一入口」的整体印象，明确它被哪些地方构造、被哪些地方消费。

**操作步骤（源码阅读型）**：

1. 打开 [src/packet.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs)，确认 `UdtPacket` 只有 `Control`/`Data` 两个变体、四个方法、两个 `From`。
2. 在仓库内搜索 `UdtPacket::` 的使用点（重点关注 `rcv_queue.rs` 与 `socket.rs`），把每一处归类到下表。
3. 搜索 `.into()` 或 `UdtPacket::from(` 附近构造数据包/控制包的地方，理解「发送侧如何把具体包塞进统一入口」。

**需要观察的现象**：

- `UdtPacket` 的**消费点**几乎都集中在接收路径：`deserialize`（解包）、`get_dest_socket_id`（解复用）、`handshake`（路由握手）、`match`（再分发到 `process_ctrl`/`process_data`）。
- 构造点则分散在发送侧，借助 `From` 实现优雅地把具体包转成 `UdtPacket`。

**预期结果**：你能填出下面这张「入口使用图」。

| 位置 | 对 `UdtPacket` 做了什么 |
| --- | --- |
| `rcv_queue.rs` worker | `deserialize` 出包 → `get_dest_socket_id` 解复用 → `handshake` 路由 |
| `socket.rs` `process_packet` | `match packet { Control => process_ctrl, Data => process_data }` |
| 发送侧（构造点） | 经 `From` 把 `UdtDataPacket`/`UdtControlPacket` 转成 `UdtPacket` |

#### 4.1.5 小练习与答案

**练习 1**：`UdtPacket` 为什么不做成 `pub`（公共 API），而是 `pub(crate)`？

> **答案**：因为 `UdtPacket` 是协议实现细节，用户应当通过 `UdtConnection` 的 `AsyncRead`/`AsyncWrite` 来收发数据，根本不需要直接接触包结构。把它隐藏在 crate 内部，可以自由重构协议格式而不破坏公共 API 的兼容性。

**练习 2**：`get_dest_socket_id` 对 `Control` 和 `Data` 分别从哪里取 `dest_socket_id`？为什么返回值类型是统一的 `u32`？

> **答案**：对 `Control(p)` 取 `p.dest_socket_id`（控制包结构体的顶层字段），对 `Data(p)` 取 `p.header.dest_socket_id`（数据包的 `dest_socket_id` 藏在 header 子结构里）。两者底层都是 `u32`（数据包 header 的 dest_socket_id 字段是 `u32`，控制包的是 `SocketId`，而 `SocketId` 本身就是 `u32` 的别名），因此返回统一 `u32`，方便接收侧解复用时统一比较。

---

### 4.2 首比特 dispatch：一比特区分两类包

#### 4.2.1 概念说明

接收侧拿到一段字节，第一件事就是判断「这是数据包还是控制包」。UDT 协议用一个极其经济的约定来回答这个问题：

> **看整段字节第一个字节的最高位（most significant bit）。**
> - 最高位为 `0` → 数据包（Data）。
> - 最高位为 `1` → 控制包（Control）。

这个约定在数据包头和控制包头的注释里各写了一句，是理解整个分发的钥匙：

- 数据包：[data_packet.rs:34](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L34) 注释 `// bit 0 = 0`。
- 控制包：[control_packet.rs:9](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L9) 注释 `// bit 0 = 1`。

注意这里的「bit 0」指的是**协议文档里的第 0 位（最高有效位）**，不是 Rust 数组下标意义上的第 0 个最低位。它对应字节里数值最大的那一位，权重是 \(2^7 = 128\)。

为什么用最高位？因为数据包的第一个字段是「序列号」（`seq_number`），占用 31 位；最高位空出来正好留给「类型标志」，二者复用同一个 32 位字的最高位，不浪费空间。控制包则把高位 1 + 低 15 位「类型码」打包在第一个 16 位字里。

#### 4.2.2 核心流程

判别逻辑在数学上就是：

\[
\text{is\_control} = \left( \text{raw}[0] \gg 7 \right) \ne 0
\]

即把首字节右移 7 位，结果要么是 0（数据包），要么是 1（控制包）。流程伪代码：

```text
fn deserialize(raw):
    if raw 为空: 报错 InvalidData
    first_bit = (raw[0] >> 7) != 0
    if first_bit == false:  # 最高位 0
        return Data( UdtDataPacket::deserialize(raw) )
    else:                   # 最高位 1
        return Control( UdtControlPacket::deserialize(raw) )
```

对应的「反方向」也成立：控制包序列化时，会把首 16 位写成 `0x8000 + 类型码`，确保最高位恒为 1：

[control_packet.rs:125](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L125) — `0x8000 + self.packet_type.type_as_u15()`，其中 `0x8000` 就是把最高位置 1 的「标记位」。反序列化时用 `& 0x7FFF` 把它摘掉再取类型码：[control_packet.rs:194](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L194)。

而数据包序列化时，序列号本身只用低 31 位，反序列化时用 `& 0x7fffffff` 显式清掉最高位以容错：[data_packet.rs:51](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L51) — `... & 0x7fffffff`。

#### 4.2.3 源码精读

判别逻辑集中在 `deserialize` 开头三行，是本讲最核心的代码：

[packet.rs:26-39](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L26-L39) — `deserialize`：先防空，再用 `raw[0] >> 7` 判类型并分发：

```rust
pub fn deserialize(raw: &[u8]) -> Result<Self> {
    if raw.is_empty() {
        return Err(Error::new(
            ErrorKind::InvalidData,
            "cannot deserialize empty packet",
        ));
    }
    let first_bit = (raw[0] >> 7) != 0;
    let packet = match first_bit {
        false => Self::Data(UdtDataPacket::deserialize(raw)?),
        true => Self::Control(UdtControlPacket::deserialize(raw)?),
    };
    Ok(packet)
}
```

逐行拆解：

- **防空**：空字节无法判类型，直接返回 `InvalidData`。注意这里只防「完全为空」，不校验长度——长度校验交给两个子类型的 `deserialize`（数据包要求 ≥16 字节，见 [data_packet.rs:45-50](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L45-L50)；控制包同理要求 ≥16 字节，见 [control_packet.rs:135-140](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L135-L140)）。这是分层校验：入口只管「能不能分流」，长度合法性由各走廊自己负责。
- **取首比特**：`(raw[0] >> 7) != 0`。`raw[0]` 是 `u8`，右移 7 位后只剩最高位，结果非 0 即 1，转成 `bool`。注意这里**没有**用更直接但容易写错的位掩码判断（如 `raw[0] & 0x80 != 0`），语义完全等价，但 `>> 7` 更贴近协议文档「第 0 位」的表述。
- **match 分流**：`false`（最高位 0）走数据包，`true`（最高位 1）走控制包。两条路径都带 `?`，把子类型的解析错误向上传播。

这条三行判别是整个协议解析的「总开关」——后面 u4-l2、u4-l3 学的所有字段布局，都必须先过了这一关。

#### 4.2.4 代码实践

> **实践目标**：亲手构造两段字节，验证 `(raw[0] >> 7)` 的判别会把首字节最高位为 0 的字节导向「数据包」、为 1 的导向「控制包」。

由于 `UdtPacket` 是 `pub(crate)`，外部代码无法直接调用真正的 `deserialize`。下面给两段实践：第一段是**可直接运行的示例代码**（复刻判别逻辑，不依赖 crate 内部类型），第二段说明**如何在本 crate 内**用真实函数端到端验证。

**① 可直接运行的示例代码（不依赖 crate）**

> 标注：以下为**示例代码**，不是项目原有代码，旨在让你亲手跑一遍首比特判别逻辑。可放进 `rust` playground 或任意一个 `cargo` 项目里运行。

```rust
// 示例代码：复刻 packet.rs 中 (raw[0] >> 7) != 0 的判别
fn is_control(raw: &[u8]) -> bool {
    assert!(!raw.is_empty(), "cannot classify empty packet");
    (raw[0] >> 7) != 0
}

fn main() {
    // 数据包：首字节最高位 0。这里构造一个合法的数据包头首字节：
    // seq_number = 0x0000000A (10)，首字节 = 0x00，最高位 = 0
    let data_packet: [u8; 16] = [
        0x00, 0x00, 0x00, 0x0A, // seq_number = 10
        0x00, 0x00, 0x00, 0x00, // position / in_order / msg_number 全 0
        0x00, 0x00, 0x00, 0x00, // timestamp
        0x01, 0x02, 0x03, 0x04, // dest_socket_id = 0x01020304
    ];

    // 控制包：首字节最高位 1。构造一个 KeepAlive 控制包：
    // (0x8000 + 0x0001).to_be_bytes() = [0x80, 0x01]，首字节 = 0x80，最高位 = 1
    let ctrl_packet: [u8; 16] = [
        0x80, 0x01,             // type = KeepAlive (0x8001)
        0x00, 0x00,             // reserved
        0x00, 0x00, 0x00, 0x00, // additional_info
        0x00, 0x00, 0x00, 0x00, // timestamp
        0x01, 0x02, 0x03, 0x04, // dest_socket_id = 0x01020304
    ];

    println!("data_packet  is_control = {}", is_control(&data_packet));  // 期望 false
    println!("ctrl_packet  is_control = {}", is_control(&ctrl_packet));  // 期望 true
}
```

**需要观察的现象 / 预期结果**：

- `data_packet` 首字节 `0x00`，`0x00 >> 7 = 0`，判为数据包（输出 `false`）。
- `ctrl_packet` 首字节 `0x80`（十进制 128），`0x80 >> 7 = 1`，判为控制包（输出 `true`）。

**② 在本 crate 内端到端验证（源码阅读 + 可选动手）**

> 标注：因为类型是 `pub(crate)`，以下测试必须在 **crate 内部**添加（例如放进 `src/packet.rs` 末尾的 `#[cfg(test)] mod tests`），**不能**在外部 crate 里写。本讲不修改源码，仅说明做法。

```rust
// 示例代码：若在 src/packet.rs 内部添加测试
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dispatch_data_vs_control_by_first_bit() {
        // 数据包：16 字节，首字节 0x00。UdtDataPacket::deserialize 要求 >=16 字节。
        let data = [0u8; 16];
        assert!(matches!(UdtPacket::deserialize(&data), Ok(UdtPacket::Data(_))));

        // 控制包：KeepAlive(0x8001)，16 字节，首字节 0x80。
        let mut ctrl = [0u8; 16];
        ctrl[0] = 0x80; ctrl[1] = 0x01;
        assert!(matches!(UdtPacket::deserialize(&ctrl), Ok(UdtPacket::Control(_))));
    }
}
```

**需要观察的现象 / 预期结果**：数据包字节被解析为 `UdtPacket::Data(_)`，控制包字节被解析为 `UdtPacket::Control(_)`，证明首比特确实决定了 `match first_bit` 走哪个分支。这两个 16 字节缓冲都能完整反序列化（数据包 header 恰好 16 字节、KeepAlive 的 control_info_field 为空），不会触发长度校验错误。

> ⚠️ 若构造的是 **Handshake** 控制包（类型码 `0x0000`），则 `ControlPacketType::deserialize` 会继续读 `raw[16..]` 解析握手信息，需要额外 32 字节（共 48 字节）才不会越界。本练习特意选了 `KeepAlive`，正是因为它的 `control_info_field()` 为空、16 字节即可。

#### 4.2.5 小练习与答案

**练习 1**：`raw[0] >> 7` 和 `raw[0] & 0x80` 在「判断最高位」这件事上等价吗？为什么源码选了前者？

> **答案**：等价。`0x80` 即二进制 `1000_0000`，`raw[0] & 0x80` 同样只保留最高位。源码选 `>> 7` 可能是为了贴近 UDT 协议文档「bit 0（最高位）」的表述习惯；二者生成的机器指令也几乎一样，纯属风格选择。

**练习 2**：如果对端发来一个首字节为 `0xC0`（二进制 `1100_0000`）的包，会被判成什么？首字节里的其余位（这里是次高位 1）会被忽略吗？

> **答案**：判成**控制包**，因为最高位是 1。次高位 `1` 属于控制包首 16 位「类型码」的一部分，**不会被忽略**——它会被 `ControlPacketType::deserialize` 用 `& 0x7FFF` 一并参与类型解码（`0xC000 & 0x7FFF = 0x4000`，这是个未定义类型码，会报 `unknown control packet type`）。所以首比特只决定「走哪条走廊」，走廊里如何解读其余位是子类型自己的事。

---

### 4.3 序列化/反序列化分发与接收路由

#### 4.3.1 概念说明

本模块把 `UdtPacket` 的四个方法串起来看，重点回答两个问题：

1. **序列化/反序列化如何分发**：`serialize` 和 `deserialize` 都只是 `match` 一下，把活儿转交给 `UdtDataPacket` / `UdtControlPacket`。
2. **`get_dest_socket_id` 与 `handshake` 在接收流程里到底干什么**：它们是接收 worker 把一个包「解复用」到正确 socket、并把握手包「路由」给 listener 的关键工具。

理解这两点，就能把 4.1 的「枚举」、4.2 的「首比特」、以及实际收包流程完全打通。

#### 4.3.2 核心流程

接收 worker（`rcv_queue.worker`）处理一个 UDP 数据报的主链路：

```text
UDP 收到一个数据报 (buf, nbytes, addr)
  │
  ▼
UdtPacket::deserialize(&buf[..nbytes])          # 4.2 的首比特 dispatch 在这里发生
  │   (失败则 .ok()? 静默丢弃，见下方源码说明)
  ▼
packet.get_dest_socket_id()                      # 取目标 socket id
  │
  ├──== 0 ──▶ packet.handshake()                 # socket_id==0 的是握手包
  │             │
  │             ├── Some(hs) ──▶ listener.listen_on_handshake(addr, hs)   # 交给 listener
  │             └── None     ──▶ 报错 "non-handshake packet with socket 0"
  │
  └──!= 0 ──▶ get_socket(socket_id)              # 在注册表里找本地 socket
                │
                ├── 找到且 peer_addr 匹配且存活 ──▶ socket.process_packet(packet)
                │                                      │
                │                                      ▼ match
                │                                 Control ─▶ process_ctrl
                │                                 Data    ─▶ process_data
                └── 找不到 / 不匹配 ──▶ UDT_DEBUG 下打印并忽略
```

要点：

- `socket_id == 0` 是 UDT 协议约定的「广播/握手」特殊 id——客户端还不知道服务端会分配哪个 socket id，只能发给 0，由对端 listener 接住。
- `process_packet` 里的第二次 `match` 是「业务级分发」（区分控制信令与用户数据），与 `deserialize` 里的「格式级分发」（决定怎么解析字节）层次不同，但形式相似。

#### 4.3.3 源码精读

**① serialize / deserialize 的分发**

[packet.rs:19-24](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L19-L24) — `serialize`：按变体委托给子类型：

```rust
pub fn serialize(&self) -> Vec<u8> {
    match self {
        Self::Control(p) => p.serialize(),
        Self::Data(p) => p.serialize(),
    }
}
```

`deserialize` 已在 4.2.3 精读，不再重复。注意对称性：序列化时由「内存中的变体」决定走哪条；反序列化时由「字节里的首比特」决定走哪条。

**② get_dest_socket_id 的分发**

[packet.rs:12-17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L12-L17) — 取目标 socket id，两种包字段位置不同：

```rust
pub fn get_dest_socket_id(&self) -> u32 {
    match self {
        Self::Control(p) => p.dest_socket_id,
        Self::Data(p) => p.header.dest_socket_id,
    }
}
```

数据包的 `dest_socket_id` 在 `header` 子结构里（[data_packet.rs:40](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs#L40)），控制包的则是结构体顶层字段（[control_packet.rs:14](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L14)）。这个方法把两者的差异抹平，让接收侧只用一个 `u32` 就能解复用。

**③ handshake 的分发**

[packet.rs:41-49](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L41-L49) — 两层 `match`：先确认是控制包，再确认是 Handshake 类型：

```rust
pub fn handshake(&self) -> Option<&HandShakeInfo> {
    match self {
        Self::Control(ctrl) => match &ctrl.packet_type {
            ControlPacketType::Handshake(info) => Some(info),
            _ => None,
        },
        _ => None,
    }
}
```

它本质是个「类型窄化」助手：把 `UdtPacket` → `Option<&HandShakeInfo>`，只有「控制包 + Handshake 子类型」才返回 `Some`。这对接收侧至关重要——握手包要被路由给 listener，而不是交给已建立的连接。

**④ 接收 worker 如何串起这一切**

[rcv_queue.rs:162-198](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L162-L198) — worker 主循环的解复用 + 路由段（关键行已标注）：

```rust
for (packet, addr) in packets.into_iter().flatten() {
    let socket_id = packet.get_dest_socket_id();          // ← get_dest_socket_id 在这里用
    if socket_id == 0 {
        if let Some(handshake) = packet.handshake() {     // ← handshake() 在这里用
            // ... 取出该 mux 的 listener，调用 listen_on_handshake
            listener.listen_on_handshake(addr, handshake).await?;
        } else {
            return Err(Error::new(
                ErrorKind::InvalidData,
                "received non-hanshake packet with socket 0",  // 原文拼写如此
            ));
        }
    } else if let Some(socket) = self.get_socket(socket_id).await {
        if socket.peer_addr() == Some(addr) && socket.status().is_alive() {
            socket.process_packet(packet).await?;          // ← 第二次 match 在这里发生
            // ...
        }
    } else {
        // ... 找不到对应 socket，UDT_DEBUG 下打印并忽略
    }
}
```

这里能看到 `UdtPacket` 的三个方法被依次调用：`get_dest_socket_id`（解复用键）→ `handshake`（握手路由判定）→ 最终 `process_packet` 内部的 `match`（业务分发）。另外注意上游 [rcv_queue.rs:147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L147) 的 `UdtPacket::deserialize(&buf[..nbytes]).ok()?`——`.ok()` 把 `Result` 转成 `Option`，再 `?` 直接丢弃解析失败的包，意味着**格式不合法的包会被静默丢弃**而不是让 worker 崩溃。

**⑤ process_packet 的第二次 match**

[socket.rs:435-440](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L435-L440) — 业务级分发，与 `deserialize` 的格式级分发形式一致但语义不同：

```rust
pub(crate) async fn process_packet(&self, packet: UdtPacket) -> Result<()> {
    match packet {
        UdtPacket::Control(ctrl) => self.process_ctrl(ctrl).await,
        UdtPacket::Data(data) => self.process_data(data).await,
    }
}
```

至此，一个包从「字节」到「被具体业务处理」的完整链路就清晰了：`deserialize`（格式分发）→ `get_dest_socket_id`（解复用）→ `process_packet`（业务分发）。

#### 4.3.4 代码实践

> **实践目标**：跟踪一次完整的「收包 → 解复用 → 路由」调用链，说清 `get_dest_socket_id` 和 `handshake` 各自在哪一步发挥作用。

**操作步骤（源码阅读型）**：

1. 打开 [src/queue/rcv_queue.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs) 的 `worker` 方法（[L137](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L137) 起）。
2. 从 [L147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L147) 的 `deserialize` 开始，逐行走到 [L184](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L184) 的 `process_packet`。
3. 跳到 [src/socket.rs:435-440](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L435-L440) 看 `process_packet` 的 `match`，再各自追到 `process_ctrl`（[L442](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L442)）和 `process_data`（[L675](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L675)）。

**需要观察的现象**：

- `get_dest_socket_id` 的返回值在 [L164](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L164) 被拿来和 `0` 比较，决定走「握手路由」还是「解复用到已连接 socket」。
- `handshake()` 在 [L165](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L165) 用 `if let Some(handshake)` 取出握手信息，只有握手包才会被交给 `listener.listen_on_handshake`。
- 若 `socket_id == 0` 但不是握手包，会直接报错返回（[L176-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L176-L181)）——这是协议一致性的保护：发到 socket 0 的本应只有握手包。

**预期结果**：你能用自己的话复述下面这条链路，并能指出每一步用的是 `UdtPacket` 的哪个方法或哪段代码：

```text
UdtPacket::deserialize   (格式分发：首比特)
  └▶ get_dest_socket_id  (解复用键)
       ├─ ==0  ─▶ handshake() ─▶ listener.listen_on_handshake
       └─ !=0  ─▶ get_socket ─▶ process_packet (业务分发：match 变体)
```

> ⚠️ 本实践为源码阅读型，不产生可运行产物；若要观察运行时行为，可在 `rcv_queue.rs` 的 [L163](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L163) 与 [L165](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L165) 临时加日志（本讲不修改源码，仅建议）。运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`deserialize` 用首比特做「格式分发」，`process_packet` 用枚举变体做「业务分发」。两者为什么不能合并成一次 `match`？

> **答案**：它们处于不同阶段、面向不同对象。`deserialize` 面向的是**原始字节**，必须先判首比特才知道调哪个子类型的解析器；此时包还没进任何 socket。`process_packet` 面向的是**已经解析好的 `UdtPacket` 值**，且已经解复用到某个具体 socket，目的是把包交给该 socket 的控制处理或数据处理逻辑。合并它们会混淆「字节解析层」和「socket 业务层」两个职责。

**练习 2**：`rcv_queue.rs` 里 `socket_id == 0` 的包为什么要单独走 `handshake()` 分支，而不是像普通包那样 `get_socket(0)`？

> **答案**：`socket_id == 0` 不是任何一个真实本地 socket 的 id，它是协议约定的「发往对端 listener 的广播地址」。此时连接尚未建立、注册表里没有对应 socket，`get_socket(0)` 必然找不到。所以必须用 `handshake()` 把它识别为握手包，再交给该 multiplexer 的 `listener` 去做握手应答（SYN cookie 流程，详见 u8-l1）。若 `socket_id == 0` 却不是握手包，说明对端违反协议，直接报错。

**练习 3**：为什么 `deserialize` 失败时 [rcv_queue.rs:147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L147) 用 `.ok()?` 静默丢弃，而不是让 worker 报错退出？

> **答案**：UDP 是不可靠通道，可能收到来自任意源的杂包、损坏包或攻击者构造的畸形包。如果每遇到一个坏包就让接收 worker 崩溃，整个 multiplexer（及其上所有连接）都会瘫痪。用 `.ok()?` 把坏包过滤掉、继续处理后续好包，是协议实现里典型的「鲁棒性优先」选择——坏包被当成噪声丢弃，不影响正常流量。

## 5. 综合实践

**任务：手工「伪造」两类包并口算它们在 `UdtPacket::deserialize` 与接收路由里的命运。**

结合本讲三个最小模块，完成下面这个串联练习：

1. **构造字节**：参考 4.2.4 的示例，分别写出两段各 16 字节的缓冲：
   - 缓冲 A：模拟一个目标 `dest_socket_id = 0x0A0B0C0D`、序列号 `seq = 5` 的**数据包**（首字节最高位为 0）。
   - 缓冲 B：模拟一个发往 `socket_id = 0` 的 **Handshake 控制包**（首字节最高位为 1，类型码 `0x0000`）。
2. **预测分发**：
   - 对缓冲 A，写出 `(raw[0] >> 7) != 0` 的结果是 `false`，因此 `deserialize` 会调 `UdtDataPacket::deserialize`，得到 `UdtPacket::Data(_)`；`get_dest_socket_id()` 返回 `0x0A0B0C0D`。
   - 对缓冲 B，写出首比特为 `true`，会调 `UdtControlPacket::deserialize`；但因 Handshake 需要读取 `raw[16..]` 的 32 字节握手信息，**只有 16 字节的缓冲 B 会在子类型解析阶段失败**（待本地验证其具体报错位置：是长度不足还是越界 panic）。
3. **预测路由**：假设缓冲 B 被补足到合法长度（48 字节）并成功解析为 Handshake 控制包，画出它在 `rcv_queue.worker` 里的路由路径——`socket_id == 0` → `handshake()` 返回 `Some` → `listener.listen_on_handshake`。
4. **延伸思考**：缓冲 B 当前 16 字节会被 [rcv_queue.rs:147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L147) 的 `.ok()?` 静默丢弃，**不会**触发 worker 报错。请结合 4.3.5 练习 3 解释这种设计的好处。

> 这个练习把「枚举」「首比特 dispatch」「序列化/反序列化分发」「接收路由」四个知识点串成一条线。如果你能不看讲义独立完成，说明本讲的核心已经掌握。其中「缓冲 B 16 字节具体报什么错」一项标注为**待本地验证**——建议你在 crate 内部按 4.2.4 ②的方式加一个测试亲眼看一看（本讲不修改源码）。

## 6. 本讲小结

- `UdtPacket` 是 crate 内部（`pub(crate)`）的**统一入口枚举**，只有 `Control(UdtControlPacket)` 和 `Data(UdtDataPacket)` 两个变体，把协议线上两大类包捏合在一起。
- 判别数据包还是控制包**只看第一个字节的最高位**：`raw[0] >> 7 != 0` 为真即控制包，否则数据包；这是 `deserialize` 的总开关（[packet.rs:33](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/packet.rs#L33)）。
- `serialize`/`deserialize`/`get_dest_socket_id`/`handshake` 都是 `match self` 的薄封装，把活儿委托给两个子类型，`UdtPacket` 自身不含算法。
- 接收 worker 依次调用 `deserialize`（格式分发）→ `get_dest_socket_id`（解复用键）→ `handshake`（握手路由）→ `process_packet`（业务分发），四步正好对应 `UdtPacket` 的全部职责。
- `socket_id == 0` 是「发往 listener 的握手广播地址」，由 `handshake()` 识别后交给 listener；其余包按 `dest_socket_id` 解复用到具体 socket。
- 解析失败的包会被 `.ok()?` 静默丢弃，体现了协议实现对不可靠 UDP 通道的鲁棒性处理。

## 7. 下一步学习建议

本讲只打开了 `UdtPacket` 这道「门」，门后两条走廊的内部结构还没看。建议按顺序继续：

1. **u4-l2 数据包格式**：精读 [src/data_packet.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/data_packet.rs)，逐位拆解 16 字节数据包头（序列号、`PacketPosition` 分片标记、消息号、时间戳、dest_socket_id），理解 `Only`/`First`/`Middle`/`Last` 如何标记消息边界。
2. **u4-l3 控制包格式**：精读 [src/control_packet.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs)，掌握 `ControlPacketType` 的 u15 编码表，以及 Handshake/Ack/Nak/Drop 各自的 `control_info_field` 结构。
3. **u4-l4 序列号与循环算术**：精读 [src/seq_number.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/seq_number.rs)，理解为什么数据包里的 `seq_number` 要在 31 位循环空间上做加减法。

学完第 4 单元四篇，你就能完整读懂 UDT 线上的每一个字节。之后再进入第 5 单元「发送与接收数据通路」，看这些包如何在收发队列与缓冲里流动。
