# 零拷贝报文解析：zerocopy 与 LayoutVerified

## 1. 本讲目标

WireGuard 是一个跑在网络热路径（hot path）上的 VPN：每一个进出 TUN 网卡的明文 IP 包都要被封装、加密成传输报文，每一个进出的 UDP 密文报文都要被解析、解密。在万兆甚至更高速率的场景下，哪怕每个报文多做一次内存拷贝、多做一次堆分配，都会被吞吐放大成显著的 CPU 与延迟开销。

本讲不是讲某个新协议机制，而是讲一个**贯穿整个项目、在多个模块反复出现的工程手法**：用 `zerocopy` 库的 `LayoutVerified` 把一段原始字节「就地」当作结构体来读写，不做反序列化拷贝、不做堆分配。

学完后你应该能够：

1. 说清「先反序列化到结构体再处理」与「`LayoutVerified` 直接视图」两种报文解析范式的区别，以及后者为何适合本项目的高吞吐数据面。
2. 理解 `#[repr(packed)]` 结构体配合 `FromBytes`/`AsBytes`/`U32`/`U64` 如何与线路字节一一对应，并掌握 `LayoutVerified::new` / `new_from_prefix` 的返回值与「长度闸门」语义。
3. 看懂 `send.rs`、`receive.rs`、`ip.rs`、`route.rs`、`handshake/messages.rs` 中就地构造/改写报文头、就地加密密文的零拷贝模式。
4. 准确指出 `new_from_prefix` 解析失败时路由器返回哪个 `RouterError`，以及握手层返回哪个 `HandshakeError`。

## 2. 前置知识

在阅读本讲前，你需要先建立以下认知（已由前置讲义覆盖，本讲直接承接，不重复）：

- **WireGuard 报文有两类**：握手报文（Initiation/Response/CookieReply，定长）与传输报文（Transport，变长，承载数据或 keepalive）。它们的字段布局见 u4-l1。
- **传输报文的线路格式**：`TransportHeader（16 字节明文头）‖ 密文载荷 ‖ 16 字节 Poly1305 标签`，且发送缓冲在头部前还预留了 16 字节前缀空间，详见 u3-l2 与 u5-l2。
- **数据面的两阶段任务模型**：每个报文被拆成「可并行的 `parallel_work`」与「顺序敏感的 `sequential_work`」两段，详见 u5-l1、u5-l2、u5-l3。
- **收发的不对称职责**：发送用计数 nonce 的 `EncryptionState`，接收用防回放的 `DecryptionState`，详见 u5-l2、u5-l3、u5-l7。

本讲只关注**字节 ↔ 结构体**这一层，不涉及密码学计算与路由语义本身。

几个本讲会用到的 Rust 概念，先用一句话解释：

- **对齐（alignment）**：CPU 访问内存时，类型 `T` 的地址通常需要是 `align_of::<T>()` 的整数倍（例如 `u64` 要 8 字节对齐）。不对齐的访问在某些架构上是未定义行为（UB）。
- **`#[repr(packed)]`**：告诉编译器「不要给结构体字段插入对齐填充」，字段紧挨着排列。这正是网络报文的要求——线路上的字节没有填充，但代价是字段可能不对齐。
- **借用（borrow）与生命周期**：一个引用 `&[u8]` 或 `&mut [u8]` 指向某段已有内存；`LayoutVerified` 就是在这段内存上叠一个结构化「视图」，本身不拥有、不拷贝数据。

## 3. 本讲源码地图

本讲涉及的关键文件按「从数据面到控制面」排列：

| 文件 | 作用 |
| --- | --- |
| `src/wireguard/router/messages.rs` | 传输报文头 `TransportHeader` 的 `#[repr(packed)]` 定义（本讲最简单的样板）。 |
| `src/wireguard/router/send.rs` | 出站加密 `SendJob`：就地写报文头、就地加密、追加标签——零拷贝的完整范例。 |
| `src/wireguard/router/receive.rs` | 入站解密 `ReceiveJob`：并行解密 + 串行收尾，两段都用 `LayoutVerified` 视图。 |
| `src/wireguard/router/ip.rs` | `IPv4Header`/`IPv6Header` 视图与 `inner_length`，用于剥掉尾部标签写回 TUN。 |
| `src/wireguard/router/route.rs` | cryptokey 路由：用同一套 IP 头视图按目的/源地址查表。 |
| `src/wireguard/router/device.rs` | `Device::recv` 入口：`new_from_prefix` 失败即返回 `RouterError::MalformedTransportMessage`。 |
| `src/wireguard/router/types.rs` | `RouterError` 枚举定义。 |
| `src/wireguard/handshake/messages.rs` | 握手报文的 `#[repr(packed)]` 定义与 `parse()` 零拷贝解析入口。 |

> 说明：`Cargo.toml` 中依赖为 `zerocopy = "0.3"`（[Cargo.toml:31](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L31)）。这是一个较早的版本，其 API 与新版略有差异，本讲所有源码引用都基于该版本的真实行为。

## 4. 核心概念与源码讲解

### 4.1 网络报文为什么需要「零拷贝解析」

#### 4.1.1 概念说明

处理一个网络报文，最直觉的写法是「**先反序列化**」：从 socket 读到一段字节 `buf: Vec<u8>`，然后逐字段 `read_u32`、`copy_from_slice`，组装出一个拥有自己内存的 `struct Message { ... }`，再对这个结构体做处理；处理完再把字段 `copy` 回字节缓冲发出去。

这种写法在「每秒几十个报文」的管理面里完全没问题，但在 WireGuard 的数据面里会很贵：

- **每个报文两次堆拷贝**：一次读进结构体、一次从结构体写出，外加可能的堆分配。
- **拷贝本身是纯开销**：它既不是加密、也不是路由，不产生任何业务价值，只是把同样的字节挪个位置。
- **被吞吐放大**：千兆链路上每秒数十万报文，乘以每报文的拷贝字节数，就是可观的总线带宽与 CPU 占用。

「**零拷贝解析（zero-copy parsing）**」追求的是：**报文从网卡到达用户态后，直到被加密/解密、发出去为止，整段字节尽量原地处理**。我们不把字节「翻译」成一个独立的结构体，而是直接在那段字节上叠一个「**视图（view）**」——告诉你「这 4 个字节是大端的 total_len，那 8 个字节是计数器」，但底层还是原来那块缓冲，没有复制。

`zerocopy` crate 就是 Rust 生态里实现这种视图的标准工具。它的核心思路是：让结构体声明自己是「可以安全地按字节解释」的（`FromBytes`/`AsBytes`），然后用 `LayoutVerified` 把字节切片安全地转写成这种结构体的引用。

#### 4.1.2 核心流程：两种范式对比

用一个简化的传输报文头（4 字节 type + 4 字节 receiver + 8 字节 counter = 16 字节）对比两种写法：

```text
【范式 A：先反序列化（有拷贝）】
  buf: [u8; 16]  ──copy──▶  TransportHeader { type: u32, receiver: u32, counter: u64 }
                              （独立的栈/堆对象，字段重新对齐）
                          ──读取/修改──▶
                          ──copy──▶ 写回 buf 发送

【范式 B：LayoutVerified 直接视图（零拷贝）】
  buf: &mut [u8; 16]  ──视图──▶  LayoutVerified<&mut [u8], TransportHeader>
                              （不复制，header.f_type 直接指向 buf[0..4]）
                          ──原地读写 header.f_receiver.set(...)──▶  buf 本身已被改写
                          ──直接发送 buf──▶
```

范式 B 的关键收益：

1. **无拷贝**：`header` 只是一个「带类型的借用」，读 `header.f_receiver` 读的就是 `buf` 里的字节，改它就是改 `buf`。
2. **无独立分配**：视图绑定在调用方已有的缓冲上（如 `send.rs` 里 `tun_worker` 预分配的那一个 `msg`），不产生新的堆对象。
3. **天然就地加密友好**：AEAD 加解密（`seal_in_place`/`open_in_place`）本来就要求「在原缓冲上改写密文」，视图与就地密码学无缝衔接（见 4.3）。

代价是：视图要求结构体满足严格的内存布局约束，下面用源码说明。

#### 4.1.3 源码精读：`repr(packed)` + `FromBytes`/`AsBytes`

先看项目里最简单的样板——传输报文头（[src/wireguard/router/messages.rs:5-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/messages.rs#L5-L13)）：

```rust
pub const TYPE_TRANSPORT: u32 = 4;

#[repr(packed)]
#[derive(Copy, Clone, FromBytes, AsBytes)]
pub struct TransportHeader {
    pub f_type: U32<LittleEndian>,
    pub f_receiver: U32<LittleEndian>,
    pub f_counter: U64<LittleEndian>,
}
```

逐项理解这段定义为什么能成为「线路字节的直接映射」：

- **`#[repr(packed)]`**：取消所有对齐填充。没有它，编译器为了让 `f_counter`（`U64`）8 字节对齐，会在 `f_receiver` 后插 0 字节填充（本例恰好不需填充，但布局原则必须显式声明），导致「结构体大小」≠「线路大小」。`packed` 保证三个字段紧挨着，结构体恰好 16 字节，与线路上 `type(4)‖receiver(4)‖counter(8)` 一一对应。
- **`U32<LittleEndian>` / `U64<LittleEndian>`**：这是 `zerocopy::byteorder` 提供的「带字节序的整数包装类型」。WireGuard 传输报文规定小端序（握手报文也是小端，见 u4-l1），所以用 `LittleEndian`；而 IP 报文头是大端，所以 `ip.rs` 里用 `BigEndian`（见 4.4）。它们本身就是 4/8 字节、对齐要求宽松的类型，在 `packed` 结构里也能安全存在。
- **`FromBytes`**：zerocopy 的 trait，承诺「任意字节序列都可以被安全地 reinterpret 成这个类型」。这是「把字节当结构体读」的安全前提——zerocopy 在编译期检查字段类型都满足该约束，否则 derive 失败。
- **`AsBytes`**：反方向的承诺「这个类型的内存表示就是它的字节，可以安全地取成 `&[u8]`」。这是「把结构体当字节写/读」的前提，例如后面 `header.f_counter.as_bytes()`。

> 注意 `packed` 结构体里取字段引用是对齐未定义行为的重灾区。zerocopy 的 `U32`/`U64` 提供 `.get()`/`.set()` 方法，内部用 `read_unaligned`/`write_unaligned` 安全地读写未对齐字段（详见 u4-l1 对握手消息 `parse` 的讲解）。这正是用包装类型而非裸 `u32`/`u64` 的原因。

握手报文用的是同一套配方，只是字段更多。例如（[src/wireguard/handshake/messages.rs:70-88](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L70-L88)）：

```rust
#[repr(packed)]
#[derive(Copy, Clone, FromBytes, AsBytes)]
pub struct NoiseResponse {
    pub f_type: U32<LittleEndian>,
    pub f_sender: U32<LittleEndian>,
    pub f_receiver: U32<LittleEndian>,
    pub f_ephemeral: [u8; SIZE_X25519_POINT],
    pub f_empty: [u8; SIZE_TAG],
}
```

这里 `[u8; N]` 数组字段天然 `FromBytes`/`AsBytes`，整段结构因此也能 derive。可推导出 `TransportHeader` 与各类握手消息的大小（`mem::size_of`），它们进而决定了缓冲分配与 `MAX_HANDSHAKE_MSG_SIZE`（见 u4-l1）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`#[repr(packed)]` 结构体大小 = 字段大小之和」与「普通结构体可能更大」的差异。

**操作步骤**（这是可在项目内任意 `#[cfg(test)]` 位置编写的**示例代码**，仅用于理解，不改变生产行为）：

1. 在 `src/wireguard/router/messages.rs` 的测试模块里临时加一个测试：

```rust
#[test]
fn size_of_transport_header_is_packed() {
    use core::mem;
    // packed：4 + 4 + 8 = 16，无填充
    assert_eq!(mem::size_of::<TransportHeader>(), 16);
    // 对一个等价但不 packed 的结构体会看到同样的 16（本例恰好不触发填充），
    // 可自行删掉 #[repr(packed)] 对照噪声头（含 u64）的情形体会差异。
}
```

2. 运行 `cargo test --package wireguard-rs size_of_transport_header_is_packed`。

**需要观察的现象**：测试通过，确认头部恰好 16 字节。这与 `src/wireguard/router/mod.rs` 里 `SIZE_MESSAGE_PREFIX = size_of::<TransportHeader>()` 一致（[src/wireguard/router/mod.rs:26-28](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L26-L28)）。

**预期结果**：`16`。这也是 `tun_worker` 为何给缓冲预留 16 字节前缀的由来——正好放一个 `TransportHeader`。

> **待本地验证**：不同 `zerocopy` 版本下 derive 宏的内部实现略有差异，但 `size_of` 结果由 `#[repr(packed)]` 与字段类型唯一决定，结论稳定。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TransportHeader` 的 `#[repr(packed)]` 去掉，`size_of::<TransportHeader>()` 一定会变大吗？

**答案**：不一定。本题三个字段是 `U32`、`U32`、`U64`，自然布局下 `U64` 需要 4 字节对齐，而前两个 `U32` 已占 8 字节，正好满足，所以无填充，大小仍是 16。但只要字段顺序或类型变化（例如把 `U64` 放最前、`U32` 放后面并扩展），就可能引入填充。`#[repr(packed)]` 的意义是**保证**无填充，与字段顺序无关，这对「结构体布局必须等于线路布局」的网络代码是必须的确定性。

**练习 2**：为什么传输头用 `U32<LittleEndian>` 而不是裸 `u32`？

**答案**：两个原因。其一，显式标注字节序，让「这段字节是小端」这一协议事实进入类型系统，避免手写 `from_le_bytes`。其二，`packed` 结构里裸 `u32` 的引用可能不对齐，直接 `&self.f` 是 UB；`U32` 的 `.get()`/`.set()` 内部用 `read_unaligned` 安全访问，且 `U32` 本身对齐要求为 1，可安全存在于 `packed` 结构中。

### 4.2 LayoutVerified：把字节切片「当作」结构体

#### 4.2.1 概念说明

`LayoutVerified<B, T>` 是 zerocopy 的核心类型：它是一个**证明**——证明底层字节缓冲 `B`（可以是 `&[u8]`、`&mut [u8]`、`Vec<u8>` 等）的长度与对齐都满足类型 `T` 的要求，因此可以安全地把 `B` 当作 `T` 来读写。

它有两个最常用的构造方法：

- **`LayoutVerified::new(bytes)`**：要求整段 `bytes` 恰好是一个 `T`（长度 `== size_of::<T>()`）。用于「整条消息就是一个结构体」的场景，例如定长握手报文。
- **`LayoutVerified::new_from_prefix(bytes)`**：要求 `bytes` 的**前缀**是一个 `T`，返回 `(T 的视图, 剩余字节)` 二元组。用于「头部 + 变长载荷」的场景，例如传输报文 `TransportHeader ‖ body ‖ tag`。

两者都返回 `Option<...>`——当字节长度不足、或对齐不满足时返回 `None`。这个 `None` 就是「长度闸门」：把不可信输入的「太短/不合法」情况挡在密码学处理之前。

#### 4.2.2 核心流程：返回值与长度闸门

`new_from_prefix` 的行为（zerocopy 0.3）：

```text
输入：bytes: &mut [u8]   （或 &[u8]，长度 N）
前提：N >= size_of::<T>()  且  对齐满足 T
输出：Some((LayoutVerified<_, T>, &mut [u8]))   ← (头部视图, 去掉头后的剩余字节)
      None                                       ← 长度不足或对齐不符
```

两种调用形态：

| 场景 | 代码 | 含义 |
| --- | --- | --- |
| 只读解析 | `let (h, rest): (LayoutVerified<&[u8], H>, &[u8]) = LayoutVerified::new_from_prefix(&buf[..])?;` | 借用只读视图，`h.f_xxx.get()` 读字段，`rest` 是后续载荷。 |
| 就地改写 | `let (mut h, body): (LayoutVerified<&mut [u8], H>, &mut [u8]) = LayoutVerified::new_from_prefix(&mut buf[..]).unwrap();` | 借用可变视图，`h.f_xxx.set(v)` 直接改写原缓冲，`body` 可就地加密。 |

`new` 的行为类似，但不返回剩余字节，因为整段就是一个 `T`。

#### 4.2.3 源码精读：握手 `parse` 与 `device.recv`

**只读、整条消息视图**——握手报文的 `parse()`（[src/wireguard/handshake/messages.rs:92-103](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L92-L103)）：

```rust
impl Initiation {
    pub fn parse<B: ByteSlice>(bytes: B) -> Result<LayoutVerified<B, Self>, HandshakeError> {
        let msg: LayoutVerified<B, Self> =
            LayoutVerified::new(bytes).ok_or(HandshakeError::InvalidMessageFormat)?;

        if msg.noise.f_type.get() != (TYPE_INITIATION as u32) {
            return Err(HandshakeError::InvalidMessageFormat);
        }
        Ok(msg)
    }
    // ...
}
```

要点：

- 用的是 `LayoutVerified::new`（整条消息 = 一个 `Initiation`），不是 `new_from_prefix`。
- `LayoutVerified::new(bytes)` 返回 `Option`，`.ok_or(HandshakeError::InvalidMessageFormat)?` 把「长度不足」翻译成握手错误。**这就是握手层的「长度闸门」失败时返回的错误。**
- 返回的 `msg` 是一个视图，后续在 `device.rs::process` 里直接读 `msg.noise`、`msg.macs` 而不复制（[src/wireguard/handshake/device.rs:332](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L332) 起：`let msg = Initiation::parse(msg)?; ... keyst.macs.check_mac1(msg.noise.as_bytes(), &msg.macs)?;`）。注意这里 `msg.noise.as_bytes()` 把内层结构又当字节传给 MAC 计算——`AsBytes` 的用法。
- 单元测试里用 `into_ref()` 把视图转回普通引用做相等比较（[src/wireguard/handshake/messages.rs:325-327](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L325-L327)）：`let buf: Vec<u8> = msg.as_bytes().to_vec(); let msg_p = Response::parse(&buf[..]).unwrap(); assert_eq!(msg, *msg_p.into_ref());`——这闭环验证了「结构体→字节（`as_bytes`）→视图（`parse`）→结构体（`into_ref`）」无损往返。

**头部 + 载荷视图、失败映射到 `RouterError`**——路由器入站入口 `Device::recv`（[src/wireguard/router/device.rs:211-222](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L222)）：

```rust
pub fn recv(&self, src: E, msg: Vec<u8>) -> Result<(), RouterError> {
    // ...
    // parse / cast
    let (header, _) = match LayoutVerified::new_from_prefix(&msg[..]) {
        Some(v) => v,
        None => {
            return Err(RouterError::MalformedTransportMessage);
        }
    };
    let header: LayoutVerified<&[u8], TransportHeader> = header;
    // ... 用 header.f_receiver.get() 查 recv 表定位 peer ...
}
```

要点：

- 这里用 `new_from_prefix`，因为传输报文是「头 + 变长密文」，只关心头部的 `f_receiver` 用来选 peer。
- **`new_from_prefix` 返回 `None`（即缓冲比 16 字节还短）时，返回 `RouterError::MalformedTransportMessage`。** 这是本讲实践任务要指出的错误。
- `RouterError` 定义在 [src/wireguard/router/types.rs:37-44](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L37-L44)，`MalformedTransportMessage` 的 `Display` 文案是 `"Transport header is malformed"`（[src/wireguard/router/types.rs:50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L50)）。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次 `new_from_prefix` 失败，确认它对应 `MalformedTransportMessage`。

**操作步骤**：

1. 阅读 `Device::recv`（[src/wireguard/router/device.rs:211-249](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L249)），看清 `None` 分支返回哪个错误。
2. 在 `src/wireguard/router/tests/` 下编写一个**示例测试**（参考该目录已有的端到端测试搭建 dummy 平台与 peer，详见 u7-l4）：构造一个只有 10 字节的缓冲，调用 `device.recv(src, short_buf)`。

```rust
// 示例代码：仅示意调用形态，具体 dummy 平台装配见 router/tests/mod.rs 的现有测试
let too_short: Vec<u8> = vec![0u8; 10]; // 少于 size_of::<TransportHeader>() = 16
match device.recv(endpoint, too_short) {
    Err(RouterError::MalformedTransportMessage) => { /* 预期命中 */ }
    other => panic!("expected MalformedTransportMessage, got {:?}", other),
}
```

**需要观察的现象**：因为 10 < 16，`new_from_prefix` 返回 `None`，`recv` 直接返回 `MalformedTransportMessage`，**不会**进入查表、不会创建 `ReceiveJob`、不会做任何密码学。

**预期结果**：断言命中 `MalformedTransportMessage`。这说明长度闸门把畸形报文挡在了所有昂贵处理之前。

> **待本地验证**：dummy 平台的 `Endpoint`/`Device` 装配细节请照搬 `router/tests/mod.rs` 中现有用例，本讲不重复其脚手架。

#### 4.2.5 小练习与答案

**练习 1**：握手层用 `LayoutVerified::new`，传输层入口却用 `new_from_prefix`，为什么不同？

**答案**：握手报文是**定长**的（Initiation/Response/CookieReply 各自固定大小，见 u4-l1），整条消息就是一个结构体，故用 `new`（整段 = `T`）。传输报文是「定长头 + 变长载荷」，头部之后还有密文与标签，故用 `new_from_prefix` 取出头部视图并保留剩余字节供后续解密。

**练习 2**：`Device::recv` 里写成 `let (header, _) = ...`，丢弃的 `_` 是什么？为什么这里能安全丢弃？

**答案**：`_` 是 `new_from_prefix` 返回的「去掉头部后的剩余字节切片」。在 `recv` 入口这一步只需要头部的 `f_receiver` 来定位 peer，剩余密文连同头部一起被 `ReceiveJob::new(msg, ...)` 整体接管（传入的是完整的 `msg`），后续 `ReceiveJob::parallel_work` 会**重新**对这个完整 `msg` 调一次 `new_from_prefix` 拿到 `(header, packet)`（见 4.3）。所以这里丢弃 `_` 不丢数据。

### 4.3 传输报文的就地构造与就地加密（send / receive）

#### 4.3.1 概念说明

传输报文是本项目零拷贝收益最大的地方，因为它最热、最长、还要就地加密。`SendJob` 把出站处理拆成两段（详见 u5-l2）：

- `parallel_work`：无副作用的就地加密（可多线程并行）。
- `sequential_work`：按序发送并触发回调。

零拷贝体现在 `parallel_work`：它**不创建新缓冲**，而是直接在 `tun_worker` 预分配的那一个 `msg` 上完成「预留前缀 → 写头部 → 追加标签 → 就地加密」。`ReceiveJob` 的 `parallel_work` 对称地在同一个入站缓冲上「视图 → 就地解密 → 路由校验」。

这两个函数完整展示了「`AsBytes`/`FromBytes` + `LayoutVerified` + 就地 AEAD」三位一体的零拷贝模式。

#### 4.3.2 核心流程

`SendJob::parallel_work` 的就地加密流程（伪代码）：

```text
msg（已含 16 字节前缀 + 明文 IP 包）
  │
  ├─1. msg.extend([0u8; SIZE_TAG])            # 末尾追加 16 字节给 Poly1305 标签腾位
  │
  ├─2. (header, packet) = new_from_prefix(&mut msg[..])
  │      # header 视图覆盖 buf[0..16]（TransportHeader）
  │      # packet 视图覆盖 buf[16..]（载荷 + 预留标签位）
  │
  ├─3. header.f_type / f_receiver / f_counter.set(...)   # 原地写头部
  │
  ├─4. nonce = [0u8;4] ‖ header.f_counter.as_bytes()      # 用 AsBytes 取计数器字节
  │
  ├─5. tag = seal_in_place_separate_tag(nonce, &mut packet[..tag_offset])  # 载荷就地加密
  │
  ├─6. packet[tag_offset..].copy_from_slice(tag.as_ref()) # 把标签填进预留位
  │
  └─7. ready.store(true, Release)                         # 发布加密完成
```

`ReceiveJob::parallel_work` 的就地解密流程是它的镜像（见 4.3.3）。

#### 4.3.3 源码精读

**出站：`SendJob::parallel_work`**（[src/wireguard/router/send.rs:59-109](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L59-L109)）：

```rust
fn parallel_work(&self) {
    // encrypt body
    {
        let job = &*self.0;
        let mut msg = job.buffer.lock();
        msg.extend([0u8; SIZE_TAG].iter());          // ① 追加 16 字节标签位

        // cast to header (should never fail)
        let (mut header, packet): (LayoutVerified<&mut [u8], TransportHeader>, &mut [u8]) =
            LayoutVerified::new_from_prefix(&mut msg[..])   // ② 视图
                .expect("earlier code should ensure that there is ample space");

        header.f_type.set(TYPE_TRANSPORT);           // ③ 原地写头部
        header.f_receiver.set(job.keypair.send.id);
        header.f_counter.set(job.counter);

        // create a nonce object
        let mut nonce = [0u8; 12];
        nonce[4..].copy_from_slice(header.f_counter.as_bytes());  // ④ AsBytes 取计数器
        let nonce = Nonce::assume_unique_for_key(nonce);

        // encrypt contents of transport message in-place
        let tag_offset = packet.len() - SIZE_TAG;
        let key = LessSafeKey::new(/* CHACHA20_POLY1305, send key */);
        let tag = key
            .seal_in_place_separate_tag(nonce, Aad::empty(), &mut packet[..tag_offset])  // ⑤ 就地加密
            .unwrap();
        packet[tag_offset..].copy_from_slice(tag.as_ref());   // ⑥ 填标签
    }
    self.0.ready.store(true, Ordering::Release);             // ⑦ 发布
}
```

零拷贝要点逐条对应：

- **① `extend` 标签位**：`packet` 的尾部 16 字节是给 AEAD 标签预留的空间，不是数据。先 `extend` 把缓冲撑大到「头 + 载荷 + 标签」。
- **② `new_from_prefix`**：返回 `(header, packet)`，`header` 覆盖前 16 字节、`packet` 覆盖剩余。注意都是 `&mut [u8]` 视图，改动直接落到原 `msg`。这里用 `.expect` 而非 `?`，因为前序代码（`tun_worker` 按 `SIZE_MESSAGE_PREFIX + mtu + CAPACITY_MESSAGE_POSTFIX` 分配）已保证空间充足（见 u3-l2），失败只可能是内部不变量被破坏。
- **③ `.set(...)` 原地写头**：`f_type`/`f_receiver`/`f_counter` 都是 `U32`/`U64` 包装类型，`.set()` 直接写对应字节。这就是 4.1 说的「视图原地改写」。
- **④ `.as_bytes()`**：nonce 前 4 字节恒零、后 8 字节是计数器（小端），用 `header.f_counter.as_bytes()` 把 `U64<LittleEndian>` 取成 8 字节塞进 nonce[4..]。这是 `AsBytes` 的典型用法——**反过来**把结构化字段当字节读。nonce 构造的语义详见 u5-l2。
- **⑤ `seal_in_place_separate_tag`**：ring 的就地加密 API，直接在 `packet[..tag_offset]` 上把明文改写成密文，标签单独返回（不就地写，因为 ring 该 API 不写标签）。这正是零拷贝与就地 AEAD 的契合点——缓冲既是输入又是输出。
- **⑥ 填标签**：把单独返回的标签 `copy_from_slice` 进预留位，至此整条 `msg` 成为完整的、可发送的密文报文。

入站 `ReceiveJob` 是镜像（[src/wireguard/router/receive.rs:66-124](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L66-L124)）：

```rust
fn parallel_work(&self) {
    // ...
    let mut msg = job.buffer.lock();
    let ok = (|| {
        // cast to header followed by payload
        let (header, packet): (LayoutVerified<&mut [u8], TransportHeader>, &mut [u8]) =
            match LayoutVerified::new_from_prefix(&mut msg.1[..]) {
                Some(v) => v,
                None => return false,
            };
        // ... 构造 nonce，open_in_place 就地解密 packet ...
        match key.open_in_place(nonce, Aad::empty(), packet) {
            Ok(_) => (),
            Err(_) => return false,
        }
        // ... check_route 按 source 地址反查 ...
        packet.len() == SIZE_TAG || peer.device.table.check_route(&peer, &packet)
    })();
    if !ok {
        msg.1.truncate(0);   // 失败则清空缓冲，杜绝未认证字节流向 TUN
    }
    self.0.ready.store(true, Ordering::Release);
}
```

要点：

- 同样 `new_from_prefix` 拿 `(header, packet)`，但这里 `None` 不报错而是 `return false`（标记失败），随后 `truncate(0)` 把缓冲清零——这是一个纵深防御设计：认证/路由失败时清空缓冲，让串行阶段再次 `new_from_prefix` 必然失败而提前返回，绝不让未认证字节写进 TUN（详见 u5-l3）。
- `open_in_place` 在 `packet` 上就地解密，与发送的 `seal_in_place` 对称。
- 串行阶段 `sequential_work` **第三次**用 `new_from_prefix`（只读视图）读 `header.f_counter` 做防回放（[src/wireguard/router/receive.rs:148-161](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L148-L161)）。同一缓冲被多次视图、零拷贝贯穿始终。

#### 4.3.4 代码实践

**实践目标**：跟踪 nonce 的零拷贝构造，体会 `AsBytes` 如何让「结构化字段 → 字节」零分配完成。

**操作步骤**：

1. 阅读 [src/wireguard/router/send.rs:88-92](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L88-L92) 的 nonce 构造。
2. 写一段注释（加在源码旁的笔记即可，不改逻辑）解释：nonce 为何是 `[0u8;4] ‖ counter(8)` 共 12 字节、为何 `header.f_counter.as_bytes()` 能直接给出小端的 8 字节而不需要 `to_le_bytes`。
3. 对照 [src/wireguard/router/receive.rs:90-94](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L90-L94) 的入站 nonce 构造，确认收发两侧用同一公式从 `f_counter` 恢复 nonce。

**需要观察的现象**：发送侧从 `job.counter` 经 `.set()` 写入 `f_counter`、再经 `.as_bytes()` 读回构造 nonce；接收侧从报文里的 `f_counter` 经 `.as_bytes()` 直接构造 nonce。两侧公式完全对称。

**预期结果**：你应当能得出结论——因为 `f_counter` 的类型 `U64<LittleEndian>` 已经把「小端」编码进类型，所以 `.as_bytes()` 返回的就是线路上的小端字节，省去了任何手动字节序转换，也省去了额外的临时数组（复用 `header` 视图背后的内存）。

> **待本地验证**：可在测试里打印 `header.f_counter.as_bytes()` 与 `job.counter.to_le_bytes()`，断言两者逐字节相等。

#### 4.3.5 小练习与答案

**练习 1**：`SendJob::parallel_work` 里 `new_from_prefix` 用的是 `.expect(...)`，而 `ReceiveJob::parallel_work` 与 `Device::recv` 都对 `None` 做了处理。为什么发送侧可以「断言永不失败」？

**答案**：因为发送缓冲的大小由己方代码完全掌控——`tun_worker` 按 `SIZE_MESSAGE_PREFIX + mtu + CAPACITY_MESSAGE_POSTFIX + 1` 分配（见 u3-l2），且 `parallel_work` 执行前已 `extend(SIZE_TAG)`，头部前缀 16 字节必然存在。这是处理「自己产生的、可信的」数据，失败只能意味着内部不变量被破坏，故用 `expect` 直接 panic 合理。而接收侧处理的是对端发来的、不可信的报文，长度任意，必须处理 `None`。

**练习 2**：为什么接收侧认证失败要 `truncate(0)`，而不是直接 `return` 一个错误？

**答案**：`parallel_work` 的返回值是 `()`（见 `ParallelJob` trait，[src/wireguard/router/queue.rs:15-19](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L15-L19)），无法向上传错误。清空缓冲是一个约定信号：串行阶段 `sequential_work` 会对同一缓冲再调 `new_from_prefix`，空缓冲必然返回 `None` 而提前返回（[src/wireguard/router/receive.rs:148-155](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L148-L155)），从而既丢弃了报文、又绝不把未认证字节写进 TUN。这是用「数据形状」而非「错误返回」传递失败状态的设计。

### 4.4 IP 报文头视图与 inner_length（ip.rs / route.rs）

#### 4.4.1 概念说明

传输报文剥掉 `TransportHeader` 与 AEAD 标签后，载荷是一个明文 IP 包。路由器需要：

- **出站**：按 IP 包的**目的地址**在 cryptokey 路由表里选 peer（`get_route`）。
- **入站**：解密后按 IP 包的**源地址**反查，校验该源是否确实属于这个 peer（`check_route`，防伪造）。
- **写回 TUN**：剥掉尾部 AEAD 标签后，要知道真实 IP 包长度（`inner_length`）。

这些都依赖对 IP 报文头的零拷贝视图。与传输头不同的是，**IP 头字段是大端（网络字节序）**，所以这里用 `U16<BigEndian>`，且 `byteorder::BigEndian` 由 `byteorder` crate 提供。

#### 4.4.2 核心流程

`inner_length(packet)` 的判定逻辑（伪代码）：

```text
取 packet[0] >> 4  （IP 版本号）
  ├─ 4 (IPv4) → new_from_prefix 取 IPv4Header 视图，返回 f_total_len（整包长度）
  ├─ 6 (IPv6) → new_from_prefix 取 IPv6Header 视图，返回 f_len + 40（载荷+固定头）
  └─ 其它    → None（keepalive/畸形，不写 TUN）
```

注意 IPv4 的 `f_total_len` 本身就是整包长度（含头），而 IPv6 的 `f_len` 只是载荷长度，固定头 40 字节要另加——这是两类 IP 头的语义差异，零拷贝视图把它直白地暴露成字段读取。

#### 4.4.3 源码精读

**IP 头视图定义**（[src/wireguard/router/ip.rs:11-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L11-L29)）：

```rust
#[repr(packed)]
#[derive(Copy, Clone, FromBytes, AsBytes)]
pub struct IPv4Header {
    _f_space1: [u8; 2],
    pub f_total_len: U16<BigEndian>,
    _f_space2: [u8; 8],
    pub f_source: [u8; 4],
    pub f_destination: [u8; 4],
}

#[repr(packed)]
#[derive(Copy, Clone, FromBytes, AsBytes)]
pub struct IPv6Header {
    _f_space1: [u8; 4],
    pub f_len: U16<BigEndian>,
    _f_space2: [u8; 2],
    pub f_source: [u8; 16],
    pub f_destination: [u8; 16],
}
```

要点：

- 用 `_f_space1`/`_f_space2` 这类「占位字段」把不关心的字节占住，使结构体的字段偏移与真实 IP 头对齐。例如 IPv4 头里 `f_total_len` 在偏移 2、`f_source` 在偏移 12、`f_destination` 在偏移 16，这与 RFC 791 一致。这是「**只解析需要的字段**」的零拷贝技巧——不必为每个字节都命名。
- `f_source`/`f_destination` 直接是 `[u8; 4]` / `[u8; 16]` 字节数组（IP 地址本身就是字节序无关的），而长度字段 `f_total_len`/`f_len` 是 `U16<BigEndian>`（网络序）。

**`inner_length`：剥标签的依据**（[src/wireguard/router/ip.rs:31-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L31-L49)）：

```rust
pub fn inner_length(packet: &[u8]) -> Option<usize> {
    match packet.get(0)? >> 4 {
        VERSION_IP4 => {
            let (header, _): (LayoutVerified<&[u8], IPv4Header>, _) =
                LayoutVerified::new_from_prefix(packet)?;
            Some(header.f_total_len.get() as usize)
        }
        VERSION_IP6 => {
            let (header, _): (LayoutVerified<&[u8], IPv6Header>, _) =
                LayoutVerified::new_from_prefix(packet)?;
            Some(header.f_len.get() as usize + mem::size_of::<IPv6Header>())
        }
        _ => None,
    }
}
```

它在 `ReceiveJob::sequential_work` 里被用来决定写多少字节进 TUN（[src/wireguard/router/receive.rs:174-180](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L174-L180)）：解密后的 `packet` 末尾还拖着 16 字节 AEAD 标签，`inner_length` 给出真实 IP 包长 `inner`，只有 `inner + SIZE_TAG <= packet.len()` 时才写 `&packet[..inner]`，从而剥掉标签。keepalive 报文（载荷为空）没有合法 IP 头，`inner_length` 返回 `None`，于是不写 TUN（但仍触发 `C::recv` 保活回调，详见 u5-l3）。

**cryptokey 路由：同一套视图查表**（[src/wireguard/router/route.rs:74-114](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L74-L114)）：

```rust
pub fn get_route(&self, packet: &[u8]) -> Option<T> {
    match packet.get(0)? >> 4 {
        VERSION_IP4 => {
            let (header, _): (LayoutVerified<&[u8], IPv4Header>, _) =
                LayoutVerified::new_from_prefix(packet)?;
            // ... 用 header.f_destination 做 longest_match 选 peer ...
        }
        VERSION_IP6 => { /* 同理用 IPv6Header.f_destination */ }
        v => { /* 未知版本 */ None }
    }
}
```

`check_route`（[src/wireguard/router/route.rs:116-138](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L116-L138)）结构对称，只是读 `header.f_source` 并校验 `p == peer`。两者都把「解析 IP 头」这件事压缩成一次 `new_from_prefix` + 字段读取，无拷贝、无分配。出站 `get_route` 失败（无路由）映射为 `RouterError::NoCryptoKeyRoute`（在 `Device::send` 中，详见 u5-l2/u5-l5）。

#### 4.4.4 代码实践

**实践目标**：验证 `inner_length` 能正确区分「真实数据包」与「keepalive」。

**操作步骤**：

1. 阅读 [src/wireguard/router/ip.rs:31-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L31-L49) 与 [src/wireguard/router/receive.rs:172-180](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L172-L180)。
2. 写一段说明：一个 keepalive 报文解密后 `packet.len() == SIZE_TAG`（即载荷为空），此时 `inner_length(packet)` 为何返回 `None`，从而被拦在 TUN 门外。
3. （可选，**示例代码**）在测试里构造一个首字节版本号为 0 的 16 字节缓冲，断言 `inner_length` 返回 `None`。

**需要观察的现象**：keepalive 的载荷为空，`packet` 只有 AEAD 标签，首字节不是合法 IP 版本（4 或 6），`match` 落入 `_ => None`。

**预期结果**：keepalive 不触发 TUN 写入，但仍触发 `C::recv` 回调以满足保活语义——这正是 `inner_length` 返回 `Option` 而非直接 `usize` 的设计目的。

> **待本地验证**：keepalive 的实际构造与判定（`packet.len() == SIZE_TAG`）见 `ReceiveJob::parallel_work`（[src/wireguard/router/receive.rs:112](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L112)），可在 router 测试中复现。

#### 4.4.5 小练习与答案

**练习 1**：`IPv4Header` 里 `f_total_len` 是 `U16<BigEndian>`，而传输头 `f_counter` 是 `U64<LittleEndian>`。为什么同个项目里两种字节序混用？

**答案**：因为它们遵循**不同的外部协议规范**。WireGuard 自定义的传输/握手报文规定小端（见 u4-l1），所以用 `LittleEndian`；而内层载荷是标准 IP 包，IP 头的字段遵循网络字节序（大端，RFC 791/8200），所以用 `BigEndian`。`zerocopy::byteorder` 的类型参数让我们在同一套零拷贝机制下精确表达两种字节序。

**练习 2**：`IPv4Header` 用 `_f_space1: [u8; 2]` 这样的占位字段，好处是什么？有没有别的写法？

**答案**：好处是**只命名关心的字段**（`f_total_len`/`f_source`/`f_destination`），用占位数组跳过不关心的字节（版本/IHL、TOS、ID、标志、片偏移、TTL、协议、校验和），结构体仍然 `packed` 且偏移正确。另一种写法是用 `LayoutVerified::new_offset` 按偏移取单个字段，或手算偏移 `u16::from_be_bytes([packet[2], packet[3]])`——但那样放弃了类型安全与零拷贝视图的便利。占位字段是本项目选择的、可读性与安全性兼顾的方案。

## 5. 综合实践

把本讲所有最小模块串起来，完成下面的**源码阅读 + 写作型实践**（即本讲指定的实践任务）。

**任务**：写一段技术说明（约 300–500 字），对比「先反序列化到结构体再处理」与「`LayoutVerified` 直接视图」在本项目高吞吐数据面场景下的差异，并准确指出 `new_from_prefix` 失败时返回的 `RouterError`。

**建议的写作骨架**：

1. **场景与代价**：以 `SendJob::parallel_work`（[src/wireguard/router/send.rs:59-109](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L59-L109)）为例，指出每个出站报文若用「先反序列化」范式，需要：把 16 字节头 copy 进一个 `TransportHeader`、把载荷 copy 进另一个缓冲、加密后再 copy 回发送缓冲——每个报文至少 2~3 次额外拷贝与若干临时分配。
2. **视图范式的收益**：`new_from_prefix` 直接在 `tun_worker` 预分配的唯一 `msg` 上叠视图，`.set()` 原地写头、`seal_in_place` 就地加密、`copy_from_slice` 填标签，全程零额外分配、零数据拷贝。再说明 `AsBytes`（`f_counter.as_bytes()` 构造 nonce）与 `FromBytes`（视图读取）如何省掉手动字节序转换。
3. **失效场景与错误**：`new_from_prefix` 返回 `None`（字节长度不足 `size_of::<T>()` 或对齐不符）。在传输报文入口 `Device::recv`（[src/wireguard/router/device.rs:215-220](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L215-L220)），这被映射为 **`RouterError::MalformedTransportMessage`**（定义见 [src/wireguard/router/types.rs:38-50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L38-L50)，文案 `"Transport header is malformed"`）。出站无路由则是另一个错误 `NoCryptoKeyRoute`，不要混淆。
4. **对照握手层**：握手层用 `LayoutVerified::new`（整条定长消息），失败映射为 `HandshakeError::InvalidMessageFormat`（[src/wireguard/handshake/messages.rs:93-95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L93-L95)），与路由器的 `MalformedTransportMessage` 区分。

**自检清单**（写完核对）：

- [ ] 是否说明了视图范式在每个报文上省下了几次拷贝/分配？
- [ ] 是否解释了 `AsBytes`/`FromBytes` 各自的方向（结构↔字节）？
- [ ] 是否准确写出了 `RouterError::MalformedTransportMessage` 这个变体名，并指出它发生在 `Device::recv`？
- [ ] 是否区分了传输层（`new_from_prefix` → `MalformedTransportMessage`）与握手层（`new` → `InvalidMessageFormat`）？

> 如果你在本地补了一个触发 `MalformedTransportMessage` 的测试（见 4.2.4），把它附在说明末尾作为佐证，效果更好。

## 6. 本讲小结

- **零拷贝解析的动机**：WireGuard 数据面在热路径上处理海量报文，每个报文多做一次拷贝都会被吞吐放大；`zerocopy` 的 `LayoutVerified` 让我们在已有缓冲上叠「视图」直接读写，省掉反序列化拷贝与堆分配。
- **样板配方**：`#[repr(packed)]`（无填充，布局=线路）+ `FromBytes`/`AsBytes`（安全按字节解释）+ `U16`/`U32`/`U64`（带字节序、可未对齐访问的包装类型）。传输头与 IP 头都是这个配方。
- **两个构造方法**：`LayoutVerified::new`（整段=`T`，用于定长握手报文）与 `new_from_prefix`（前缀=`T` + 剩余字节，用于「头+变长载荷」的传输报文与 IP 头），二者都返回 `Option`，`None` 即「长度闸门」。
- **就地加密的契合点**：`SendJob` 用 `extend` 预留标签位 → `new_from_prefix` 取 `(header, packet)` → `.set()` 原地写头 → `seal_in_place` 在 `packet` 上就地加密 → 填标签；`ReceiveJob` 用 `open_in_place` 对称解密。收发两侧还用 `f_counter.as_bytes()` 零拷贝构造 nonce。
- **失败映射**：传输报文入口 `Device::recv` 中 `new_from_prefix` 失败返回 **`RouterError::MalformedTransportMessage`**；接收侧 `parallel_work` 失败则 `truncate(0)` 清空缓冲作为「数据形状信号」让串行阶段提前返回，绝不污染 TUN。
- **字节序混用是刻意的**：WireGuard 自有报文小端、内层 IP 包大端，zerocopy 的字节序类型参数让同一套机制精确表达两种规范。

## 7. 下一步学习建议

本讲把「字节 ↔ 结构体」这一横切手法讲透了，接下来建议：

- **横向巩固**：重读 u4-l1（握手消息线路格式与 `parse`）、u5-l2（SendJob 加密）、u5-l3（ReceiveJob 解密）、u5-l5（cryptokey 路由与 IP 头），你会发现自己已经能看懂其中每一处 `LayoutVerified`/`as_bytes` 的意图。
- **纵深安全**：阅读 u7-l2（密钥材料清零），了解 `Key::Drop` 如何在「零拷贝视图共享同一块内存」的前提下，保证密钥用完即抹——零拷贝与清零是一对需要协同的约束。
- **测试视角**：阅读 u7-l4（测试策略），看 `router/tests` 如何用 dummy 平台与 `pnet` 构造的 IP 报文端到端验证本讲描述的零拷贝管道，并尝试自己补一个 `MalformedTransportMessage` 用例。
- **延伸阅读（项目外）**：zerocopy crate 的设计文档（关于「soundness」与「未对齐访问」的论证），以及 RFC 6479/WireGuard 白皮书对传输报文格式的规定，能帮你把「为什么这样布局」理解到协议层。
