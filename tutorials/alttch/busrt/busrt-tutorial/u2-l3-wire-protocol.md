# 线上协议与帧格式：握手与帧编解码

## 1. 本讲目标

本讲深入 BUS/RT 的「线上协议（wire protocol）」——也就是客户端与代理（broker）之间在 socket 上实际传输的那串字节。学完后你应当能够：

- 完整描述一次连接从建立到收发消息的握手流程；
- 画出「客户端 → 代理」帧的 9 字节头，并说明 `flags` 字节里 op 与 qos 的位划分；
- 画出「代理 → 客户端」帧的 6 字节头，并说明 sender/topic/payload 是如何用 `0x00` 分隔切分的；
- 解释 PING 与 ACK 两种控制帧的字节布局与作用；
- 拿到一段 BUS/RT 的抓包字节时，能手工逐字节解码出它的含义。

本讲是理解后续 IPC 客户端（u4）、broker 连接生命周期（u6）的基础：所有传输层（Unix / TCP / WebSocket）最终都归结到同一套字节格式。

## 2. 前置知识

阅读本讲前，你需要先掌握上一讲（u2-l1）建立的公共契约类型：

- **FrameOp**：客户端请求代理执行的操作（订阅、发布、点对点发送……），每个变体有一个 `OP_*` 字节值。
- **FrameKind**：帧本身的类型标签（消息、广播、发布、ACK……）。
- **QoS**：两个正交位 `needs_ack()`（低位）与 `is_realtime()`（高位）的组合。
- **FrameData / Frame = Arc<FrameData>**：内存中的帧结构，`payload_pos` 用来在统一缓冲里零拷贝切片出真实载荷。

还需要两个基本概念：

- **小端序（little-endian）**：BUS/RT 所有多字节整数都用小端序传输，即最低位字节在前。Rust 的 `u32::to_le_bytes()` / `u32::from_le_bytes()` 就是干这个的。
- **C 字符串式分隔**：协议大量使用 `0x00`（空字节）作为字段之间的分隔符，就像 C 语言里以 `\0` 结尾的字符串。

最后记住一个关键事实（u1-l3 已建立）：**协议常量与帧类型定义在 `src/lib.rs` 顶部，不受任何 Cargo feature 门控，始终编译**，是全库的公共契约；而握手与帧编解码逻辑分散在 `src/ipc.rs`（客户端侧）和 `src/broker.rs`（代理侧）。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 协议常量：`OP_*` 操作码、`GREETINGS`、`PROTOCOL_VERSION`、`PING_FRAME`、`RESPONSE_OK`，以及 `FrameOp`/`FrameKind`/`QoS` 类型。 |
| [src/ipc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs) | 客户端侧：`prepare_frame_buf!`/`send_frame!` 帧构造宏、`chat()` 握手函数、`handle_read()` 入站帧解析、`ping()`。 |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | 代理侧：`handle_peer()` 握手响应、`handle_reader()` 9 字节头解析与 `send_ack!`、`handle_writer()` 出站帧序列化、`handle_pinger()` 心跳。 |

> 小提示：客户端和代理用的是**两套不同长度**的帧头（客户端发的是 9 字节头，代理发的是 6 字节头），这是本讲最容易踩的坑，后文会反复对照。

## 4. 核心概念与源码讲解

### 4.1 协议常量：整条协议的「字典」

#### 4.1.1 概念说明

任何二进制协议都要先约定一组「魔数」与「操作码」，双方才能把一串字节解读成有意义的指令。BUS/RT 把这些约定集中放在 `src/lib.rs` 顶部，作为不依赖任何 feature 的公共契约。理解协议的第一步，就是把这本「字典」背下来。

字典里有四类条目：

1. **操作码 `OP_*`**：客户端请求代理执行的动作（发布、订阅、发送……）。
2. **握手魔数**：`GREETINGS`（连接开头互相打招呼的字节）与 `PROTOCOL_VERSION`（协议版本号）。
3. **应答码**：`RESPONSE_OK` 与一整套 `ERR_*` 错误码。
4. **PING 帧**：`PING_FRAME`，一个固定 9 字节的「全零」心跳帧。

#### 4.1.2 核心流程

字典本身是静态常量，没有「流程」，但它驱动了两件事：

- 握手时双方交换 `GREETINGS + PROTOCOL_VERSION`，校验彼此说的是同一种协议。
- 每一帧的 `flags` 字节里编码了一个 `OP_*` 操作码，代理据此分发处理；每个应答用一个 `RESPONSE_OK` 或 `ERR_*` 字节表达结果。

`OP_*` 的取值是精心错开的：普通操作码落在 `0x00`–`0x13` 区间，而 `OP_ACK = 0xFE` 故意被推到接近高位，避免和普通操作混淆。

#### 4.1.3 源码精读

操作码定义在 [src/lib.rs:10-19](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L10-L19)，每个常量即线上传输的那个字节：

```rust
pub const OP_NOP: u8 = 0x00;        // 心跳/空操作
pub const OP_PUBLISH: u8 = 0x01;    // 发布到主题
pub const OP_SUBSCRIBE: u8 = 0x02;  // 订阅主题
pub const OP_UNSUBSCRIBE: u8 = 0x03;
pub const OP_EXCLUDE: u8 = 0x04;    // 排除某主题
pub const OP_UNEXCLUDE: u8 = 0x05;
pub const OP_PUBLISH_FOR: u8 = 0x06;// 发布给指定接收者
pub const OP_MESSAGE: u8 = 0x12;    // 点对点消息
pub const OP_BROADCAST: u8 = 0x13;  // 广播
pub const OP_ACK: u8 = 0xFE;        // 应答帧（代理→客户端）
```

握手与应答常量在 [src/lib.rs:21-37](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L21-L37)：

```rust
pub const PROTOCOL_VERSION: u16 = 0x01;          // 协议版本号
pub const RESPONSE_OK: u8 = 0x01;                // 握手/ACK 成功码
pub const PING_FRAME: &[u8] = &[0, 0, 0, 0, 0, 0, 0, 0, 0]; // 9 字节全零
...
pub const GREETINGS: [u8; 1] = [0xEB];           // 握手魔数
```

注意 `PING_FRAME` 恰好是 9 个零字节——这正好等于「客户端 → 代理」帧头的长度（4.3 节会看到），而且它的 `flags` 字节（第 5 个字节）是 0，代理据此识别成「空操作/心跳」。

帧类型契约在 [src/lib.rs:318-332](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L318-L332)（`FrameOp`）与 [src/lib.rs:385-394](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L385-L394)（`FrameKind`）里复用同一批 `OP_*` 字节，但视角不同：`FrameOp` 是「客户端要代理做什么」，`FrameKind` 是「这一帧本身是什么」。`FrameKind` 多了两个特殊值：`Prepared = 0xff`（本地哨兵，表示帧已预先序列化好、**绝不上线**）和 `Acknowledge = OP_ACK`。

QoS 的位定义在 [src/lib.rs:352-370](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L352-L370)，它是后续 `flags` 字节高位 2 bit 的来源：

```rust
pub enum QoS { No = 0, Processed = 1, Realtime = 2, RealtimeProcessed = 3 }
impl QoS {
    pub fn is_realtime(self) -> bool { self as u8 & 0b10 != 0 } // 高位
    pub fn needs_ack(self)   -> bool { self as u8 & 0b1  != 0 } // 低位
}
```

#### 4.1.4 代码实践

**目标**：把协议字典「打印出来」，建立字节直觉。

**步骤**：

1. 新建一个二进制 crate（或在 examples 下加一个文件），启用 `ipc` feature 引入 `busrt`。
2. 写一段程序，遍历打印关键常量的十六进制值。

```rust
// 示例代码：打印协议字典
use busrt::{OP_SUBSCRIBE, OP_PUBLISH, OP_MESSAGE, OP_ACK,
            GREETINGS, PROTOCOL_VERSION, PING_FRAME, QoS};

fn main() {
    println!("GREETINGS = 0x{:02X}", GREETINGS[0]);
    println!("PROTOCOL_VERSION = 0x{:04X}", PROTOCOL_VERSION);
    println!("OP_SUBSCRIBE=0x{:02X} OP_PUBLISH=0x{:02X} OP_MESSAGE=0x{:02X} OP_ACK=0x{:02X}",
             OP_SUBSCRIBE, OP_PUBLISH, OP_MESSAGE, OP_ACK);
    println!("PING_FRAME ({} bytes) = {:?}", PING_FRAME.len(), PING_FRAME);
    for q in [QoS::No, QoS::Processed, QoS::Realtime, QoS::RealtimeProcessed] {
        println!("QoS {:?}: byte=0x{:02X} needs_ack={} is_realtime={}",
                 q, q as u8, q.needs_ack(), q.is_realtime());
    }
}
```

**需要观察的现象**：`PING_FRAME` 长度恰好是 9；QoS 的字节值是 0/1/2/3，正好能塞进 2 个 bit。

**预期结果**：输出与上面源码常量逐一对应。若你在本机未配置好编译环境，可标注为「待本地验证」，但仍应能手工推算出每个值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OP_ACK = 0xFE` 要选这么大的值，而不是接着 `0x13` 往后排？
**答案**：ACK 是「代理 → 客户端」方向的帧类型标签，和客户端发出的操作码属于不同语义集合；把它推到接近高位（且与普通操作码区间 `0x00–0x13` 远离），可以在解析时一眼区分，也避免未来新增操作码时与之冲突。

**练习 2**：`QoS::RealtimeProcessed` 的字节值是多少？它的两个位分别代表什么？
**答案**：值是 `3`（二进制 `0b11`）。低位 `1` 表示 `needs_ack`（要等代理回 ACK），高位 `1` 表示 `is_realtime`（立即刷新出站）。

---

### 4.2 连接握手：`chat()` 与 `handle_peer` 的对话

#### 4.2.1 概念说明

握手（handshake）是连接建立后、正式收发业务帧之前的「对暗号」阶段。BUS/RT 的握手要完成三件事：

1. **协议认亲**：双方交换 `GREETINGS + PROTOCOL_VERSION`，确认说的是同一种协议、同一个版本。
2. **客户端自报家门**：客户端把自己的名字（如 `test.client.sender`）发给代理。
3. **代理登记造册**：代理校验名字合法性、做访问控制（AAA）、把客户端登记进注册表，然后回 OK。

握手是由代理主动发起的（代理先发问候），客户端回应。这在 u4 的 IPC 客户端 `chat()` 函数和 u6 的 broker `handle_peer()` 里是一一对应的「镜像」实现。

#### 4.2.2 核心流程

完整的握手时序（C = 客户端，B = 代理）：

```
1. B -> C : GREETINGS(0xEB) + PROTOCOL_VERSION(2 字节)     // 3 字节
2. C -> B : GREETINGS(0xEB) + PROTOCOL_VERSION(2 字节)     // 3 字节，原样回声
3. B -> C : RESPONSE_OK(0x01)                               // 1 字节
4. C -> B : name_len(2 字节) + name(UTF-8 字节)
5. B -> C : RESPONSE_OK(0x01) 或 ERR_*                      // 1 字节
```

注意第 1、2 步是**对称的**：双方都发出完全相同的 3 字节问候。代理先发，客户端校验后把同样 3 个字节「回声」回去；代理再校验一次。这种「双向互验」比单向声明更稳健——任何一方版本不对都会立刻失败。

第 5 步若失败（名字非法、AAA 拒绝、名字冲突），代理发对应的 `ERR_*` 字节并关闭连接。

#### 4.2.3 源码精读

**客户端侧**的握手在 [src/ipc.rs:618-656](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L618-L656) 的 `chat()` 函数。核心是先读代理的问候、校验、回声，再发送名字：

```rust
// 读代理的 3 字节问候：GREETINGS + 版本号
reader.read_exact(&mut buf).await?;          // buf = [u8; 3]
if buf[0] != GREETINGS[0] { return Err(... "Invalid greetings"); }
if u16::from_le_bytes(buf[1..3].try_into().unwrap()) != PROTOCOL_VERSION { ... }
writer.write_all(&buf).await?;               // 回声同样 3 字节
// 读代理的 RESPONSE_OK
reader.read_exact(&mut buf).await?;          // buf = [u8; 1]
if buf[0] != RESPONSE_OK { return Err(...); }
// 发送名字：2 字节长度(小端 u16) + 名字字节
writer.write_all(&(name.len() as u16).to_le_bytes()).await?;
writer.write_all(&n).await?;
// 读注册结果
reader.read_exact(&mut buf).await?;
if buf[0] != RESPONSE_OK { return Err(...); }
```

`chat()` 被 `connect_broker!` 宏在 [src/ipc.rs:234-250](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L234-L250) 调用，紧随其后才 spawn 出 `handle_read` 读帧循环——也就是说**握手成功之前不会解析任何业务帧**。

**代理侧**的握手在 [src/broker.rs:1748-1814](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1748-L1814) 的 `handle_peer()` 开头，与客户端严格镜像：

```rust
// 1. 代理先发问候
let mut buf = GREETINGS.to_vec();
buf.extend_from_slice(&PROTOCOL_VERSION.to_le_bytes()); // 0xEB + 0x01 0x00
write_and_flush!(&buf);
// 2. 读客户端回声并校验
reader.read_exact(&mut buf).await?;            // [u8; 3]
if buf[0] != GREETINGS[0] { write_and_flush!(&[ERR_NOT_SUPPORTED]); ... }
if u16::from_le_bytes(buf[1..3]...) != PROTOCOL_VERSION { ... }
write_and_flush!(&[RESPONSE_OK]);              // 3. 回 OK
// 4. 读名字：2 字节长度 + 名字
reader.read_exact(&mut buf).await?;            // [u8; 2]
let len = u16::from_le_bytes(buf);
let mut buf = vec![0; len as usize];
reader.read_exact(&mut buf).await?;
let client_name = std::str::from_utf8(&buf)?.to_owned();
// 名字合法性、AAA、注册……
db.register_client(client.clone()).await?;
write_and_flush!(&[RESPONSE_OK]);              // 5. 注册成功
```

代理在 [src/broker.rs:1768-1771](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1768-L1771) 校验名字不能为空、不能以 `.` 开头（因为 `.broker` 这类以点开头的名字是代理内部保留目标），非法就用 `ERR_DATA` 拒绝。

#### 4.2.4 代码实践

**目标**：把握手时序画成一张时序图，并标注每一步的字节数与含义。

**步骤**：

1. 对照上面的「核心流程」时序，在纸上或文本里画出 C 与 B 两条竖线。
2. 在每条箭头上标出字节数和字段（如 `B→C: [0xEB, 0x01, 0x00]`）。
3. 标出两个失败点：问候/版本不符（`ERR_NOT_SUPPORTED`）、名字注册失败（`ERR_ACCESS`/`ERR_DATA`/冲突）。

**需要观察的现象**：第 1、2 步两边发的字节是否完全相同；名字长度为什么用 `u16`（最多 65535 字节，远超任何合理客户端名）。

**预期结果**：得到一张 5 步时序图，其中前 3 步共传输 7 字节（3+3+1），第 4 步传输 `2 + 名字长度` 字节。这是「源码阅读型实践」，无需运行即可完成。

#### 4.2.5 小练习与答案

**练习 1**：如果客户端连的是一个完全不同的服务（比如一个 HTTP 服务器），握手会在哪一步失败？
**答案**：第 1 步——客户端读到代理发来的第 1 个字节不是 `0xEB`（GREETINGS），`chat()` 立刻返回 `Error::not_supported("Invalid greetings")`。

**练习 2**：为什么名字长度用 2 字节 `u16` 而不是 1 字节 `u8`？
**答案**：`chat()` 在 [src/ipc.rs:623-625](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L623-L625) 明确限制了 `name.len() > u16::MAX` 就报错，用 `u16` 既给长名字（含二级客户端后缀 `%%N`）留足空间，又把单帧握手开销控制在 2 字节。

---

### 4.3 客户端 → 代理帧：9 字节头与 `flags` 位划分

#### 4.3.1 概念说明

握手完成后，客户端发出的每一帧（无论是订阅、发布、点对点消息还是心跳）都遵循同一种格式：**固定的 9 字节头 + 可变长度的主体**。这是本讲最重要的一张图。

9 字节头分为三段：

| 偏移 | 长度 | 字段 | 含义 |
| --- | --- | --- | --- |
| 0–3 | 4 字节 | `op_id` | 帧编号（小端 `u32`），用于和代理回的 ACK 对账 |
| 4 | 1 字节 | `flags` | 低 6 位 = 操作码 `OP_*`，高 2 位 = `QoS` |
| 5–8 | 4 字节 | `len` | 主体的字节长度（小端 `u32`） |

`flags` 的位划分是核心巧思：操作码只用低 6 位（最多 64 种，足够），QoS 只用高 2 位（4 种），两者拼进同一个字节，省下了一个字节。

#### 4.3.2 核心流程

客户端发送一帧的流程被拆成三个宏（都为了**内联**展开、避免多余 future）：

1. `prepare_frame_buf!`：自增 `frame_id`，写入 `op_id`（4 字节）和 `flags`（1 字节）。
2. `send_frame!`：根据「有无 target / 有无 receiver」选择主体布局，补上 `len`（4 字节）和主体。
3. `send_frame_and_confirm!`：若 `QoS` 要 ACK，先在 `responses` 映射里登记一个 oneshot 通道，再把缓冲写出。

主体布局有三种变体（见 `send_frame!` 的三个匹配臂）：

- **带 target**（消息/广播/发布）：`target + 0x00 + payload`，`len = target.len() + payload.len() + 1`。
- **带 target + receiver**（`publish_for`）：`target + 0x00 + receiver + 0x00 + payload`。
- **无 target**（订阅/取消订阅/排除等）：直接 `topic(s) + [0x00 ...]`，`len = 主体长度`。

代理侧的 `handle_reader` 用一个 `[0u8; 9]` 缓冲精确读取这 9 字节头，再按位拆解 `flags`。

#### 4.3.3 源码精读

帧头构造在 [src/ipc.rs:138-146](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L138-L146) 的 `prepare_frame_buf!` 宏：

```rust
macro_rules! prepare_frame_buf {
    ($self: expr, $op: expr, $qos: expr, $expected_header_len: expr) => {{
        $self.increment_frame_id();
        let mut buf = Vec::with_capacity($expected_header_len + 4 + 1);
        buf.extend($self.frame_id.to_le_bytes());        // op_id: 4 字节
        buf.push($op as u8 | ($qos as u8) << 6);          // flags: op | qos<<6
        buf
    }};
}
```

注意 `flags = op | (qos << 6)`：qos 左移 6 位放到最高 2 位，op 留在低 6 位。`increment_frame_id` 在 [src/ipc.rs:399-406](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L399-L406) 让编号在 `1..=u32::MAX` 之间循环（刻意绕过 0），保证每帧都有非零、可对账的 `op_id`。

`send_frame!` 宏的「无 target」臂（订阅用）在 [src/ipc.rs:226-231](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L226-L231)：

```rust
// send w/o a target
($self: expr, $payload: expr, $op: expr, $qos: expr) => {{
    let mut buf = prepare_frame_buf!($self, $op, $qos, 4);
    buf.extend_from_slice(&($payload.len() as u32).to_le_bytes()); // len: 4 字节
    send_frame_and_confirm!($self, &buf, $payload, $qos)
}};
```

于是客户端调用 `subscribe("foo/bar", QoS::Processed)`（见 [src/ipc.rs:478-480](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L478-L480)）时，`op=OP_SUBSCRIBE=0x02`、`qos=1`、`payload=b"foo/bar"`，整帧字节为：

```
偏移  0    1    2    3    4    5    6    7    8    9   10   11   12   13   14
     [01] [00] [00] [00] [42] [06] [00] [00] [00] [66] [6F] [6F] [2F] [62] [61] [72]
      \_______ op_id=1 _____/ \fl/ \______ len=6 _____/ \_______ "foo/bar" ______/
                            0x42
```

其中 `flags = 0x02 | (1<<6) = 0x02 | 0x40 = 0x42`；`len = "foo/bar".len() = 6`。

代理侧的 9 字节头解析在 [src/broker.rs:1904-1931](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1904-L1931)：

```rust
let mut header_buf = [0u8; 9];
reader.read_exact(&mut header_buf).await?;
let flags = header_buf[4];
if flags == 0 { /* OP_NOP => ping */ continue; }   // 全零帧 = 心跳
let op_id = &header_buf[0..4];
let op:  FrameOp = (flags & 0b0011_1111).try_into()?; // 低 6 位 = op
let qos: QoS     = (flags >> 6 & 0b0011_1111).try_into()?; // 高 2 位 = qos
let len = u32::from_le_bytes(header_buf[5..9].try_into().unwrap());
```

`flags == 0` 这一行揭示了一个关键事实：**全 9 字节都是 0 的帧（即 `PING_FRAME`）会被识别为心跳**，因为 `op=0` 就是 `OP_NOP`。客户端的 `ping()` 在 [src/ipc.rs:530-534](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L530-L534) 就是直接写出 `PING_FRAME`：

```rust
async fn ping(&mut self) -> Result<(), Error> {
    send_data_or_mark_disconnected!(self, PING_FRAME, Flush::Instant);
    Ok(())
}
```

需要 ACK 时的登记逻辑在 [src/ipc.rs:165-180](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L165-L180) 的 `send_frame_and_confirm!`：若 `qos.needs_ack()`，就以 `frame_id` 为 key 插入一个 oneshot sender，将来代理回的 ACK（带同一个 `op_id`）就能唤醒它。

#### 4.3.4 代码实践

**目标**：手工解码一个订阅帧，并自行构造一个发布帧的字节序列。

**步骤**：

1. 对照上面的字节图，逐字节解释 `subscribe("foo/bar", QoS::Processed)` 的 15 字节。
2. 自行计算 `publish("news/tech", b"hi", QoS::No)`（`op=OP_PUBLISH=0x01`，使用「带 target」臂，`len = target.len()+payload.len()+1`）的字节，假设 `frame_id=2`。
3. 把计算结果写成一张字节布局图。

**需要观察的现象**：发布帧比订阅帧多出一个 `0x00`（target 后的分隔符）；`QoS::No` 时 `flags` 的高 2 位是 0。

**预期结果**（发布帧，可对照自检）：

```
偏移  0    1    2    3    4    5    6    7    8    9..17          18   19   20
     [02] [00] [00] [00] [01] [0C] [00] [00] [00] "news/tech"(9B) [00] [68] [69]
      \_______ op_id=2 _____/ \fl/ \_____ len=12 ____/ \_target_/ \0/ \_payload_/
                            0x01
```

其中 `flags = 0x01 | (0<<6) = 0x01`，`len = 9+2+1 = 12 = 0x0C`，主体 = `"news/tech" + 0x00 + "hi"`。这是「源码阅读型实践」，纯手工推算，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：若把 `QoS::Processed` 改成 `QoS::Realtime`，订阅帧的第 5 个字节（`flags`）会变成多少？
**答案**：`QoS::Realtime = 2`，`flags = 0x02 | (2<<6) = 0x02 | 0x80 = 0x82`。

**练习 2**：为什么代理读到 `flags == 0` 就 `continue`，而不是当成一个普通的 `OP_NOP` 帧去处理主体？
**答案**：`PING_FRAME` 只有 9 字节头、没有主体（`len=0`），且心跳不需要任何业务处理；`continue` 直接跳过本次循环，既不读主体也不做分发，是最廉价的保活方式。

---

### 4.4 代理 → 客户端帧：6 字节头、sender/topic 切分与 ACK

#### 4.4.1 概念说明

代理回送给客户端的帧用的是**另一种**帧头——只有 6 字节，而且字段排列与客户端帧不同。这是初学者最容易混淆的地方，务必牢记两个方向长度不同。

代理 → 客户端的 6 字节头：

| 偏移 | 长度 | 字段 | 含义 |
| --- | --- | --- | --- |
| 0 | 1 字节 | `kind` | 帧类型 `FrameKind`（`Message`/`Broadcast`/`Publish`/`Acknowledge`/`Nop`） |
| 1–4 | 4 字节 | `len` | 主体的字节长度（小端 `u32`） |
| 5 | 1 字节 | `realtime` | 实时标志（非 0 表示客户端应立即刷新） |

主体用 `0x00` 分隔字段：

- **Message / Broadcast**：`sender + 0x00 + payload`。
- **Publish**：`sender + 0x00 + topic + 0x00 + payload`（多一个 topic）。
- **Acknowledge / Nop**：无主体外的额外字段（ACK 见下）。

#### 4.4.2 核心流程

出站方向有两个特殊帧值得单独说明：

**ACK 帧（应答）**：当客户端发的帧带 `QoS::Processed` 时，代理处理完后回一个 ACK。ACK 是定长 6 字节，复用 6 字节头的位置但含义不同：`byte0 = OP_ACK(0xFE)`，`byte1..5 = 被应答的 op_id`，`byte5 = 结果码（RESPONSE_OK 或 ERR_*）`。客户端收到后用 `op_id` 找到当初登记的 oneshot 通道并兑现结果。

**NOP 帧（心跳）**：代理的 `handle_pinger` 周期性地向每个客户端发一个「全 6 字节零」的 NOP（`kind=Nop=0x00`，`len=0`，`realtime=0`），既做保活，也借此探测客户端队列是否溢出（队列满就强制注销）。注意它与客户端方向的 `PING_FRAME`（9 字节零）**长度不同**，因为两个方向的头长度不同。

序列化由代理的 `handle_writer` 完成；反序列化由客户端的 `handle_read` 完成。

#### 4.4.3 源码精读

代理的出站序列化在 [src/broker.rs:2213-2263](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2213-L2263) 的 `handle_writer`。它从客户端的发送队列收 `Frame`，再拼成线上字节：

```rust
buf.push(frame.kind as u8);                              // byte 0: kind
let frame_len = extra_len + frame.buf.len() - frame.payload_pos;
buf.extend_from_slice(&(frame_len as u32).to_le_bytes()); // bytes 1-4: len
buf.push(u8::from(frame.realtime));                      // byte 5: realtime
if let Some(s) = sender { buf.extend_from_slice(s); buf.push(0x00); } // sender\0
if let Some(t) = topic.as_ref() { buf.extend_from_slice(t); buf.push(0x00); } // topic\0
write_data!(&buf, Flush::No);
write_data!(frame.payload(), frame.realtime.into());     // payload
```

`extra_len` 把 `sender+0x00` 和（若有）`topic+0x00` 的长度算进 `len`，所以 `len` 是「主体全部字节数」。于是代理回送一条来自 `sender="a.b"`、`payload=b"hi"` 的 Message，字节为：

```
偏移  0    1    2    3    4    5    6    7    8    9    10   11
     [12] [06] [00] [00] [00] [00] [61] [2E] [62] [00] [68] [69]
      kind \_____ len=6 _____/  rt  \_ "a.b" _/ \0/ \_ "hi" _/
      0x12                        0x00=非实时
```

其中 `kind=OP_MESSAGE=0x12`，`len = (3+1) + 2 = 6`，`realtime=0`。

客户端的反序列化在 [src/ipc.rs:555-616](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L555-L616) 的 `handle_read`，先读 6 字节头，再按 `kind` 分流：

```rust
let mut buf = [0_u8; 6];
reader.read_exact(&mut buf).await?;
let frame_type: FrameKind = buf[0].try_into()?;
let realtime = buf[5] != 0;
match frame_type {
    FrameKind::Nop => {}                                 // 心跳，忽略
    FrameKind::Acknowledge => {
        let ack_id = u32::from_le_bytes(buf[1..5].try_into().unwrap());
        let tx_channel = { responses.lock().remove(&ack_id) }; // 兑现 oneshot
        if let Some(tx) = tx_channel { let _r = tx.send(buf[5].to_busrt_result()); }
    }
    _ => {
        let frame_len = u32::from_le_bytes(buf[1..5].try_into().unwrap());
        let mut buf = vec![0; frame_len as usize];
        reader.read_exact(&mut buf).await?;
        // Publish: 按 0x00 切 3 段 = sender, topic, payload
        // 其它:   按 0x00 切 2 段 = sender, payload
        ...
    }
}
```

ACK 的构造在代理侧的 `send_ack!` 宏，定义在 [src/broker.rs:1946-1972](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1946-L1972)。它构造一个 `FrameKind::Prepared` 的帧（`Prepared` 表示「buf 里已经是线上字节、不要再加头」），buf 就是 6 字节：

```rust
let mut buf = [0u8; 6];
buf[0] = OP_ACK;                       // 0xFE
buf[1..5].copy_from_slice(op_id);      // 回声被应答的 op_id
buf[5] = $code;                        // RESPONSE_OK 或 ERR_*
```

`handle_writer` 见到 `FrameKind::Prepared` 就直接把 `frame.buf` 原样写出（[src/broker.rs:2228-2229](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2228-L2229)）——这就是 `Prepared` 这个本地哨兵存在的意义。

代理的心跳在 [src/broker.rs:1871-1884](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1871-L1884) 的 `handle_pinger`：每隔 `timeout/2` 检查队列是否已满（满则强制注销），并发送一个 `FrameData::new_nop()`，经 `handle_writer` 序列化成 6 字节全零的 NOP。

#### 4.4.4 代码实践

**目标**：画出代理回送的「Publish 消息帧」和「ACK 帧」两种字节布局。

**步骤**：

1. 设 `sender="a.b"`、`topic="news/tech"`、`payload=b"hi"`、非实时，按 `handle_writer` 计算 Publish 帧的 `len` 与字节序列。
2. 设某条 `op_id=2` 的订阅请求被代理以 `RESPONSE_OK` 应答，写出 ACK 的 6 字节。
3. 把两者都画成带偏移的字节图。

**需要观察的现象**：Publish 帧的 `len` 比 Message 帧多出 `topic.len()+1`；ACK 的 `len` 字段位置（bytes 1-4）放的是 `op_id` 而非长度。

**预期结果**（Publish 帧，可自检）：

```
偏移  0    1    2    3    4    5    6..8       9    10..18       19   20   21
     [01] [10] [00] [00] [00] [00] "a.b"(3B) [00] "news/tech"(9B)[00] [68] [69]
      kind \_____ len=16 ____/  rt  \sender/ \0/ \__topic__/ \0/ \payload/
      0x01
```

`kind=OP_PUBLISH=0x01`，`len = (3+1) + (9+1) + 2 = 16 = 0x10`。

ACK 帧（`op_id=2`，成功）：

```
偏移  0    1    2    3    4    5
     [FE] [02] [00] [00] [00] [01]
      ACK  \____ op_id=2 _____/  OK=0x01
```

这是「源码阅读型实践」，无需运行；若想实证，可参照 4.5 综合实践抓真实字节。

#### 4.4.5 小练习与答案

**练习 1**：客户端方向的 `PING_FRAME` 是 9 字节全零，代理方向的 NOP 心跳却是 6 字节全零，为什么长度不同？
**答案**：因为两个方向用的帧头长度不同——客户端→代理是 9 字节头，代理→客户端是 6 字节头。心跳都是「头全零、无主体」，所以各自等于其头长度。

**练习 2**：客户端收到 ACK 时，用什么字段去匹配当初发出的请求？
**答案**：用 ACK 里的 `op_id`（bytes 1–4）去 `responses` 映射里查 oneshot sender；它正是发送时由 `send_frame_and_confirm!` 以 `frame_id`（即 `op_id`）为 key 登记的。

---

### 4.5 PING / ACK 控制帧小结（速查表）

为方便复习，把两种控制帧与两种业务帧并列对照：

| 方向 | 帧 | 头长度 | 字节布局（示例） | 用途 |
| --- | --- | --- | --- | --- |
| C→B | 业务帧 | 9 | `op_id(4) \| flags(1) \| len(4) \| 主体` | 订阅/发布/发送 |
| C→B | PING | 9 | `00×9`（`PING_FRAME`） | 客户端保活，代理见 `flags==0` 即忽略 |
| B→C | 业务帧 | 6 | `kind(1) \| len(4) \| realtime(1) \| sender\0 [topic\0] payload` | 投递消息 |
| B→C | ACK | 6 | `FE \| op_id(4) \| code(1)` | 兑现 `QoS::Processed` 的请求结果 |
| B→C | NOP | 6 | `00×6`（`new_nop`） | 代理保活 + 队列溢出探测 |

## 5. 综合实践

**任务**：写一个最小的「协议解码器」小程序，把 BUS/RT 线上字节「双向」都串起来，验证你对帧格式的理解。

**步骤**：

1. 在一个新的二进制 crate 里（启用 `ipc` feature 引入 `busrt`），手工拼出一段完整的连接开场字节序列：
   - 握手：代理先发的 3 字节问候（你模拟客户端视角，只需准备「读到 `0xEB 0x01 0x00` 后回声同样 3 字节，再读 1 字节 OK，再发 `00 11 + 名字`」）；
   - 紧接着一条 `subscribe("#", QoS::Processed)` 帧（`op=0x02`，`flags = 0x02 | 0x40 = 0x42`，主体为 `b"#"`）。
2. 把上述字节逐段打印成十六进制，并在注释里标注每段对应 4.x 哪一节的哪个字段。
3. 再写一个函数 `decode_broker_frame(bytes: &[u8])`，输入代理方向的一帧（如 4.4 给出的 Message 帧 `[12 06 00 00 00 00 61 2E 62 00 68 69]`），解析出 `kind / len / realtime / sender / payload`，并断言 `sender == "a.b"`、`payload == b"hi"`。

**参考骨架**（示例代码）：

```rust
// 示例代码：手工拼装客户端开场 + 订阅帧
use busrt::{GREETINGS, PROTOCOL_VERSION, RESPONSE_OK, OP_SUBSCRIBE, QoS};

fn client_opening_subscribe(name: &str, frame_id: u32) -> Vec<u8> {
    let mut out = Vec::new();
    // 1) 回声代理的问候（实际应从 socket 读，这里只构造我们要发出的部分）
    out.extend_from_slice(&GREETINGS);                 // 0xEB
    out.extend_from_slice(&PROTOCOL_VERSION.to_le_bytes()); // 0x01 0x00
    // 2) 收到 RESPONSE_OK 后发送名字：u16 长度 + 名字
    let _ = RESPONSE_OK; // 占位：实际应先读 1 字节校验
    out.extend_from_slice(&(name.len() as u16).to_le_bytes());
    out.extend_from_slice(name.as_bytes());
    // 3) 收到注册 OK 后发订阅帧
    out.extend_from_slice(&frame_id.to_le_bytes());    // op_id
    let flags = OP_SUBSCRIBE as u8 | (QoS::Processed as u8) << 6; // 0x42
    out.push(flags);
    let topic = b"#";
    out.extend_from_slice(&(topic.len() as u32).to_le_bytes()); // len
    out.extend_from_slice(topic);
    out
}

fn decode_broker_frame(b: &[u8]) -> (&str, &[u8]) {
    // 6 字节头：kind(1) len(4) realtime(1)
    assert_eq!(b[0], 0x12); // OP_MESSAGE
    let len = u32::from_le_bytes(b[1..5].try_into().unwrap()) as usize;
    let body = &b[6..6 + len];
    let mut sp = body.splitn(2, |c| *c == 0); // sender\0 payload
    let sender = std::str::from_utf8(sp.next().unwrap()).unwrap();
    sp.next(); // 跳过 0x00
    let payload = sp.next().unwrap();
    (sender, payload)
}

fn main() {
    let bytes = client_opening_subscribe("c1", 1);
    println!("client opening hex: {:02X?}", bytes);
    let frame = [0x12, 0x06, 0x00, 0x00, 0x00, 0x00,
                 0x61, 0x2E, 0x62, 0x00, 0x68, 0x69];
    let (sender, payload) = decode_broker_frame(&frame);
    assert_eq!(sender, "a.b");
    assert_eq!(payload, b"hi");
    println!("decoded: sender={:?} payload={:?}", sender, payload);
}
```

**需要观察的现象**：`client opening hex` 里能看到 `EB 01 00`（问候）、名字长度与名字、以及 `01 00 00 00 42 01 00 00 00 23`（订阅帧头 + `#`）；`decode_broker_frame` 能正确切出 `sender="a.b"` 与 `payload="hi"`。

**预期结果**：断言全部通过。若本机暂未配置 Rust 编译环境，可将运行结果标注为「待本地验证」，但每段字节的来源都应能对照 4.2/4.3/4.4 的源码讲清楚。

## 6. 本讲小结

- BUS/RT 协议的「字典」（`OP_*`、`GREETINGS`、`PROTOCOL_VERSION`、`PING_FRAME`、`RESPONSE_OK`）定义在 [src/lib.rs:10-37](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L10-L37)，不受 feature 门控，是全库公共契约。
- 握手由代理发起：双方对称交换 3 字节问候（`0xEB + 版本号`），客户端再发 `u16` 长度 + 名字，代理登记后回 `RESPONSE_OK`。
- **客户端 → 代理**帧用 **9 字节头**：`op_id(4) | flags(1) | len(4)`，其中 `flags = op | (qos << 6)`，把操作码（低 6 位）和 QoS（高 2 位）塞进一个字节。
- **代理 → 客户端**帧用 **6 字节头**：`kind(1) | len(4) | realtime(1)`，主体用 `0x00` 分隔 `sender`、可选 `topic`、`payload`。
- PING（客户端发，9 字节全零，代理见 `flags==0` 即忽略）与 NOP 心跳（代理发，6 字节全零）长度不同，因为两个方向头长度不同。
- ACK 是定长 6 字节（`0xFE | op_id(4) | code(1)`），靠 `op_id` 与发送端登记的 oneshot 通道对账，兑现 `QoS::Processed` 的结果。

## 7. 下一步学习建议

- 下一讲 **u3-l1（创建 Broker 与注册内部客户端）** 会进入「进程内通信」——届时你会看到，当客户端和代理在同一个进程里时，帧**不上线**，而是直接以 `FrameData` 结构在异步通道里传递，`header` 字段正是为这条零拷贝路径准备的。
- 想直接看真实帧字节，可继续读 **u4-l2（ipc::Client）**，那里会把本讲的宏放进完整的 `connect → chat → handle_read` 流程中。
- 对出站缓冲与刷新策略感兴趣的话，提前翻一眼 [src/comm.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs) 的 `TtlBufWriter`，它决定了本讲里那些 `Flush::No` / `Flush::Instant` 参数的实际效果（详见 u4-l3）。
