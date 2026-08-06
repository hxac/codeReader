# 握手消息与线路格式

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 WireGuard 握手阶段在 UDP 上传输的三类消息（Initiation、Response、CookieReply）的字段构成与字节大小，并能徒手推导。
- 理解 `repr(packed)` + `zerocopy` 的 `FromBytes`/`AsBytes`/`LayoutVerified` 是如何让网络报文「不拷贝、不对齐转换」地被当作结构体读写的。
- 看懂消息类型常量（`TYPE_INITIATION/RESPONSE/COOKIE_REPLY`）与 `MAX_HANDSHAKE_MSG_SIZE` 的来历和用途。
- 解释为什么 `f_static`、`f_timestamp` 这类字段的长度是「明文长度 + SIZE_TAG」。

本讲只讲「握手消息长什么样」，**不**讲 Noise 密码学计算（那是 u4-l3），也**不**讲 MAC/Cookie 抗 DoS（那是 u4-l4）。我们只关心报文的**线路格式（wire format）**与**零拷贝解析**。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 握手报文是「定长、紧凑、无填充」的字节序列

WireGuard 是一个追求极简的 VPN 协议，所有握手消息在网络上都是**固定长度**、**字段紧挨**的字节流，没有版本号、没有 TLV（类型-长度-值）、没有对齐填充。这意味着只要知道消息类型，就精确知道它有多少字节。这也意味着可以用 `#[repr(packed)]` 的 Rust 结构体去「盖」在这段内存上，按字段直接读。

### 2.2 AEAD 密文 = 明文 + 认证标签

WireGuard 用 ChaCha20-Poly1305 这种 AEAD（认证加密）算法加密某些字段。AEAD 有两个特点：

1. **密文长度等于明文长度**：ChaCha20 是流密码，加密不膨胀。
2. **末尾追加一个 16 字节的 Poly1305 认证标签（tag）**：用于校验完整性。

所以一段明文经 AEAD 加密后，在线路上的字节数 = `明文字节数 + 16`。本项目中这个 16 叫 `SIZE_TAG`。理解了这一点，`f_static`（加密后的静态公钥）为什么是 `32 + 16 = 48` 字节就一目了然了。

### 2.3 零拷贝解析：用视图「盖」在原始字节上

传统做法是把字节流「反序列化」进一个结构体，处理完再「序列化」回去——要拷贝两次。wireguard-rs 走的是另一条路：用 `zerocopy::LayoutVerified` 把一段字节切片**直接视为**某个结构体的内存视图，读写视图就是在读写原始字节，零拷贝。这要求结构体与字节流**布局完全一致**，这正是 `#[repr(packed)]` 的作用——它告诉编译器「不要在字段间插填充字节，按我写的顺序紧排」。

> 术语速查：
> - **repr(packed)**：取消结构体字段间对齐填充，保证内存布局与网络字节序一一对应。
> - **FromBytes / AsBytes**：`zerocopy` 提供的 trait，标记「任意字节序列都可安全转为本类型」/「本类型可安全当作字节序列」，二者是零拷贝转换的安全保证。
> - **LayoutVerified**：一个「字节切片 ↔ 结构体」的双向视图，是本项目零拷贝解析的核心类型。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/wireguard/handshake/messages.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs) | 本讲主角：定义三类握手消息及其内部子结构、尺寸常量、类型常量、`parse()` 零拷贝方法。 |
| [src/wireguard/handshake/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/mod.rs) | 握手模块入口，声明子模块并把 `TYPE_*`、`MAX_HANDSHAKE_MSG_SIZE` 通过 `pub use` 对外导出。 |
| [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs) | `process()` 在这里按消息类型分用并调用各 `parse()`，是 `parse()` 的真实调用方，用于我们理解「解析后做什么」。 |
| [src/wireguard/handshake/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs) | 定义 `HandshakeError`，`parse()` 失败时返回 `InvalidMessageFormat` 变体。 |

## 4. 核心概念与源码讲解

### 4.1 尺寸基础常量与消息类型常量

#### 4.1.1 概念说明

要徒手算出每条消息多大，必须先知道它的「零件」尺寸。messages.rs 顶部用一组 `const` 给出了所有原子字段的字节数。同时，WireGuard 用首 4 字节的小端 `u32` 作为消息类型（type）字段来区分三类握手消息，这三个类型值是协议级常量。

#### 4.1.2 核心流程

解析任何一条握手消息时，外界（`device.rs::process`）先读报文首 4 字节判定类型，再按类型调用对应 `parse()`。因此类型常量既出现在消息体内部（每个消息自带 `f_type`），也被外部用来分用。

#### 4.1.3 源码精读

零件尺寸与类型常量定义在文件开头：

[定义 SIZE_* 基础尺寸常量：messages.rs:L15-L24](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L15-L24)

逐个含义（带注释）：

```rust
const SIZE_MAC: usize = 16;          // mac1/mac2 各 16 字节（BLAKE2s 截断）
const SIZE_TAG: usize = 16;          // Poly1305 AEAD 认证标签
const SIZE_XNONCE: usize = 24;       // XChaCha20 nonce（用于 cookie 加密）
const SIZE_COOKIE: usize = 16;       // cookie 明文长度
const SIZE_X25519_POINT: usize = 32; // Curve25519 公钥长度
const SIZE_TIMESTAMP: usize = 12;    // TAI64N 时间戳长度

pub const TYPE_INITIATION: u32 = 1;
pub const TYPE_RESPONSE: u32 = 2;
pub const TYPE_COOKIE_REPLY: u32 = 3;
```

这组常量是后续所有消息尺寸推导的「公理」。注意它们都是小写 `const`，除 `TYPE_*` 外不对外导出（`mod.rs` 只 `pub use` 了 `TYPE_*` 和 `MAX_HANDSHAKE_MSG_SIZE`）：

[mod.rs 对外导出：mod.rs:L23-L24](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/mod.rs#L23-L24)

```rust
pub use device::Device;
pub use messages::{MAX_HANDSHAKE_MSG_SIZE, TYPE_COOKIE_REPLY, TYPE_INITIATION, TYPE_RESPONSE};
```

mod.rs 顶部的注释点明了整个 handshake 子树实现的是哪一种 Noise 模式：

[协议模式声明：mod.rs:L1-L7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/mod.rs#L1-L7)

即 `Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s`。本讲不深入 Noise，只需记住：三类消息就是这个模式在线路上的三种报文形态。

### 4.2 三类握手消息的字段布局

#### 4.2.1 概念说明

WireGuard 握手报文分两层：

- **外层结构（Initiation / Response / CookieReply）**：对应网络上完整的一条消息。
- **内层子结构**：
  - `NoiseInitiation` / `NoiseResponse`：被 Noise 加密/认证保护的「内部载荷」，外层 Initiation/Response 把它和 `MacsFooter` 拼在一起。
  - `MacsFooter`：两条 16 字节 MAC（mac1、mac2），用于抗 DoS（不属 AEAD，是明文计算的）。
  - `CookieReply` 没有 Noise 内层，字段直接铺开。

#### 4.2.2 核心流程

Initiation 与 Response 的组织方式完全对称，都是：

```
外层消息 = Noise 内层载荷 || MacsFooter(mac1 || mac2)
```

而 `macs` 字段（`MacsFooter`）**不**被 Noise 的 AEAD 覆盖，它是单独基于对端公钥和 cookie 计算的明文认证码，用来在耗时的密码学运算**之前**先做一轮便宜的过滤（抗 DoS，详见 u4-l4）。device.rs 在拿到 `parse()` 结果后，正是先调 `check_mac1`、再调 `check_mac2`，最后才进入昂贵的 `consume_initiation`。

#### 4.2.3 源码精读

**MacsFooter** —— Initiation/Response 共用的尾部：

[MacsFooter 结构：messages.rs:L63-L68](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L63-L68)

```rust
#[repr(packed)]
#[derive(Copy, Clone, FromBytes, AsBytes)]
pub struct MacsFooter {
    pub f_mac1: [u8; SIZE_MAC], // 16
    pub f_mac2: [u8; SIZE_MAC], // 16
}
```

合计 `16 + 16 = 32` 字节。

**NoiseInitiation** —— Initiation 的内部载荷：

[NoiseInitiation 结构：messages.rs:L70-L78](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L70-L78)

```rust
pub struct NoiseInitiation {
    pub f_type: U32<LittleEndian>,              // 4   消息类型(=1)
    pub f_sender: U32<LittleEndian>,            // 4   本端分配的 sender id
    pub f_ephemeral: [u8; SIZE_X25519_POINT],   // 32  临时公钥(明文)
    pub f_static: [u8; SIZE_X25519_POINT + SIZE_TAG], // 32+16=48 加密的静态公钥
    pub f_timestamp: [u8; SIZE_TIMESTAMP + SIZE_TAG], // 12+16=28 加密的时间戳
}
```

要点：

- `f_ephemeral` 是**明文**的 Curve25519 临时公钥（Noise 中 ephemeral 必须明文，对端才能做 DH）。
- `f_static` 和 `f_timestamp` 是**加密**字段，所以各自多了 16 字节 tag。这正是「明文长度 + SIZE_TAG」的由来。
- 用 `U32<LittleEndian>` 而非 `u32`：保证多字节整数按**小端序**读写，与协议规定一致，且避免跨平台字节序歧义。

合计 `4 + 4 + 32 + 48 + 28 = 116` 字节。

**外层 Initiation**：

[Initiation 结构：messages.rs:L45-L50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L45-L50)

```rust
pub struct Initiation {
    pub noise: NoiseInitiation, // 116
    pub macs: MacsFooter,       // 32
}
```

合计 `116 + 32 = 148` 字节。

**NoiseResponse** —— Response 的内部载荷：

[NoiseResponse 结构：messages.rs:L80-L88](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L80-L88)

```rust
pub struct NoiseResponse {
    pub f_type: U32<LittleEndian>,            // 4  (类型=2)
    pub f_sender: U32<LittleEndian>,          // 4  本端 sender id
    pub f_receiver: U32<LittleEndian>,        // 4  对端在 initiation 里的 sender id
    pub f_ephemeral: [u8; SIZE_X25519_POINT], // 32 临时公钥(明文)
    pub f_empty: [u8; SIZE_TAG],              // 16 加密的"空"载荷(只有 tag)
}
```

注意 `f_empty`：响应消息里没有需要加密的额外载荷，但 Noise 协议这一步仍需一次 AEAD 运算以认证 transcript，于是加密一段「空明文」，结果只留下 16 字节的 tag，故名 `f_empty`。合计 `4 + 4 + 4 + 32 + 16 = 60` 字节。

**外层 Response**：

[Response 结构：messages.rs:L38-L43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L38-L43)

合计 `60 (noise) + 32 (macs) = 92` 字节。

**CookieReply** —— 结构独立，无 Noise 内层、无 macs：

[CookieReply 结构：messages.rs:L52-L59](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L52-L59)

```rust
pub struct CookieReply {
    pub f_type: U32<LittleEndian>,               // 4  (类型=3)
    pub f_receiver: U32<LittleEndian>,           // 4  对端 sender id
    pub f_nonce: [u8; SIZE_XNONCE],              // 24 XChaCha20 nonce
    pub f_cookie: [u8; SIZE_COOKIE + SIZE_TAG],  // 16+16=32 加密的 cookie
}
```

合计 `4 + 4 + 24 + 32 = 64` 字节。

三个外层消息都标注 `#[repr(packed)]` 并 `derive(Copy, Clone, FromBytes, AsBytes)`。`packed` 保证字段紧排、与线路字节一致；`FromBytes`/`AsBytes` 是零拷贝读写的安全凭证；`Copy + Clone` 让消息可以按值传递、用 `Default::default()` 零初始化后逐字段填值（device.rs 构造响应时就是这么做的）。

#### 4.2.4 代码实践：徒手推导三类消息长度

**实践目标**：用源码常量独立算出三类消息字节数，并理解为什么 `f_static`、`f_timestamp` 是「明文 + SIZE_TAG」。

**操作步骤**（纯纸笔推导，对照上面的常量）：

1. 计算 `MacsFooter = 16 + 16`。
2. 计算 `NoiseInitiation = 4 + 4 + 32 + (32+16) + (12+16)`，再 `Initiation = NoiseInitiation + MacsFooter`。
3. 计算 `NoiseResponse = 4 + 4 + 4 + 32 + 16`，再 `Response = NoiseResponse + MacsFooter`。
4. 计算 `CookieReply = 4 + 4 + 24 + (16+16)`。

**预期结果**：

| 消息 | 字节长度 |
|------|---------|
| Initiation | 148 |
| Response | 92 |
| CookieReply | 64 |

> 说明 `f_static = 32 + 16`：静态公钥明文 32 字节，经 ChaCha20-Poly1305 加密后密文仍是 32 字节（流密码不膨胀），并追加 16 字节 Poly1305 tag，故线路占 48 字节。`f_timestamp = 12 + 16` 同理：TAI64N 时间戳明文 12 字节 + 16 字节 tag = 28 字节。这正是「明文长度 + SIZE_TAG」。

> 待本地验证：你可以在 `src/wireguard/handshake/messages.rs` 的 `#[cfg(test)] mod tests` 里追加 `assert_eq!(std::mem::size_of::<Initiation>(), 148);` 等断言，用 `cargo test --lib handshake::messages` 验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `f_ephemeral` 是明文，而 `f_static` 是密文？

> 参考答案：Noise 协议中，对端必须先用本端的临时公钥 `f_ephemeral` 计算 DH 共享密钥，才能解密后续字段；所以 ephemeral 必须明文。`f_static`（静态公钥）在本端会话密钥派生之后才被加密，以隐藏本端长期身份。

**练习 2**：`NoiseResponse` 里为什么没有像 Initiation 那样的 `f_static`/`f_timestamp`？

> 参考答案：静态公钥和时间戳只在发起方（Initiation）首次传递以建立身份与防重放；响应方（Response）无需再重复，只需要回送自己的临时公钥和一次 AEAD 认证（即 `f_empty` 的 16 字节 tag）。

### 4.3 MAX_HANDSHAKE_MSG_SIZE 的推导

#### 4.3.1 概念说明

`udp_worker`（见 u3-l3）在接收时，需要预先分配一个足够容纳**任何一种**握手报文的缓冲区。为避免为每种类型单独估大小，handshake 模块导出了一个常量 `MAX_HANDSHAKE_MSG_SIZE`，表示三类消息中的最大字节数。

#### 4.3.2 核心流程

由于三类消息定长，只需取三者 `size_of` 的最大值。messages.rs 用一个 `const fn max` 在编译期完成这一计算，结果是一个编译期常量，可直接用于数组/缓冲区定长分配。

#### 4.3.3 源码精读

[编译期 max 与 MAX_HANDSHAKE_MSG_SIZE：messages.rs:L26-L34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L26-L34)

```rust
const fn max(a: usize, b: usize) -> usize {
    let m: usize = (a > b) as usize;
    m * a + (1 - m) * b
}

pub const MAX_HANDSHAKE_MSG_SIZE: usize = max(
    max(mem::size_of::<Response>(), mem::size_of::<Initiation>()),
    mem::size_of::<CookieReply>(),
);
```

要点：

- 用自定义 `const fn max` 而非 `std::cmp::max`，是因为早期 Rust 的 `std::cmp::max` 未必能在所有 `const` 上下文稳定使用；这里用算术 `(a>b) as usize` 选择分支，纯算术、可在 `const fn` 中编译期求值。
- 代入上节结果：`max(max(92, 148), 64) = max(148, 64) = 148`。

因此 **`MAX_HANDSHAKE_MSG_SIZE = 148`**。

它的真实消费者在 `udp_worker`：

[udp_worker 中按 MAX_HANDSHAKE_MSG_SIZE 分配缓冲：workers.rs:L107](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L107)

```rust
let size = mtu + MAX_HANDSHAKE_MSG_SIZE;
```

这里 `mtu + MAX_HANDSHAKE_MSG_SIZE` 是因为同一个 `udp_worker` 既要收握手报文（≤ 148），也要收数据报文（≤ mtu），所以缓冲取两者之和即可覆盖最坏情况。

#### 4.3.4 代码实践：断言 MAX_HANDSHAKE_MSG_SIZE

**实践目标**：用一行断言把推导结果固定下来。

**操作步骤**：在 messages.rs 的测试模块末尾加：

```rust
// 示例代码（非项目原有）
#[test]
fn max_handshake_msg_size_is_largest() {
    assert_eq!(MAX_HANDSHAKE_MSG_SIZE, std::mem::size_of::<Initiation>());
    assert_eq!(MAX_HANDSHAKE_MSG_SIZE, 148);
    assert!(std::mem::size_of::<Response>() <= MAX_HANDSHAKE_MSG_SIZE);
    assert!(std::mem::size_of::<CookieReply>() <= MAX_HANDSHAKE_MSG_SIZE);
}
```

**预期结果**：四个断言全部通过，确认 148 是三者最大值。

> 待本地验证：运行 `cargo test --lib max_handshake_msg_size_is_largest`。

#### 4.3.5 小练习与答案

**练习**：如果未来协议给 Initiation 多加一个 8 字节字段，`MAX_HANDSHAKE_MSG_SIZE` 会自动更新吗？需要改 `udp_worker` 的分配吗？

> 参考答案：会自动更新——因为它由 `mem::size_of::<Initiation>()` 编译期推导而来，无需手改常量。`udp_worker` 用 `mtu + MAX_HANDSHAKE_MSG_SIZE`，也自动跟随，**无需修改**。这就是用 `size_of` 派生而非手写魔数的好处。

### 4.4 零拷贝解析 parse()

#### 4.4.1 概念说明

`parse()` 是把「收到的字节切片」变成「可按字段访问的视图」的入口。三类消息各有一个 `parse()`，返回 `LayoutVerified<B, Self>`——一个盖在原始字节上的结构体视图，而非一份拷贝。除零拷贝外，`parse()` 还顺便校验了消息类型字段，把「格式/类型不符」转化为统一的 `HandshakeError::InvalidMessageFormat`。

#### 4.4.2 核心流程

1. 调 `LayoutVerified::new(bytes)`：若字节长度与结构体不匹配（过长或过短）返回 `None`，`parse()` 据此返回 `InvalidMessageFormat`。
2. 读取视图中的 `f_type`，与期望类型比对，不符也返回 `InvalidMessageFormat`。
3. 返回视图，调用方即可像访问结构体字段一样访问报文内容。

#### 4.4.3 源码精读

以 `Initiation::parse` 为例（Response、CookieReply 完全同构）：

[Initiation::parse 零拷贝解析：messages.rs:L92-L103](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L92-L103)

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
}
```

要点：

- 泛型 `<B: ByteSlice>`：让 `parse()` 同时接受 `&[u8]`、`&mut [u8]` 及其各种切片类型，返回的视图也保留对应的可变/不可变性。`ByteSlice` 是 `zerocopy` 对「可当作字节序列」的抽象。
- `LayoutVerified::new`：这是**长度与对齐**的安全闸门。`Initiation` 是 `repr(packed)`，对齐要求为 1，因此对齐总是满足；剩下只校验长度恰好等于 `size_of::<Initiation>()`(148)。长度不符即 `None` → 报错。
- `msg.noise.f_type.get()`：`.get()` 是 `U32<LittleEndian>` 的方法，按小端序读出 `u32`。注意因为 `repr(packed)`，对多字节字段**不能**直接 `.f_type` 取引用（未对齐引用在 Rust 中是 UB），必须用 `.get()` 按值读出——这正是用 `U32<LittleEndian>` 类型而非裸 `u32` 的关键原因。
- `Response::parse`、`CookieReply::parse` 同理，只是校验的类型常量不同：

[Response::parse：messages.rs:L105-L116](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L105-L116) ｜ [CookieReply::parse：messages.rs:L118-L129](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L118-L129)

**`parse()` 的真实调用方**在 `device.rs::process`。它先用首 4 字节分用，再按分支调对应 `parse`：

[device.rs 按类型分用并调用 parse：device.rs:L329-L332](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L329-L332)

```rust
match LittleEndian::read_u32(msg) {
    TYPE_INITIATION => {
        let msg = Initiation::parse(msg)?;
        keyst.macs.check_mac1(msg.noise.as_bytes(), &msg.macs)?;
        // ...后续昂贵的密码学运算
    }
    // TYPE_RESPONSE / TYPE_COOKIE_REPLY 分支同理
```

注意 `parse` 返回的视图可直接 `msg.noise.as_bytes()`——既能当结构体读字段，又能瞬间退回字节切片喂给 MAC 校验，这正是零拷贝视图的灵活之处。`?` 把 `InvalidMessageFormat` 等错误向上传播，错误类型定义在：

[HandshakeError 枚举：types.rs:L37-L49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L37-L49)

#### 4.4.4 代码实践：跟踪一次完整的 parse 往返

**实践目标**：通过现有测试理解「构造消息 → 序列化为字节 → parse 回视图 → 字段一致」的零拷贝往返。

**操作步骤**：

1. 打开 [messages.rs 测试：L297-L363](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L297-L363)，阅读 `message_response_identity` 与 `message_initiate_identity`。
2. 观察这两个测试的三步：
   - `let msg: Response = Default::default();` 后逐字段 `set`/赋值；
   - `let buf: Vec<u8> = msg.as_bytes().to_vec();` —— 用 `AsBytes::as_bytes()` 把结构体转成字节（这是唯一的拷贝，仅为测试持有）；
   - `let msg_p = Response::parse(&buf[..]).unwrap();` —— 零拷贝 parse，再 `assert_eq!(msg, *msg_p.into_ref())` 验证字段一致。
3. 运行测试。

**需要观察的现象**：`as_bytes()` 输出的字节流长度恰好等于结构体尺寸；`parse` 能无损还原每个字段。

**预期结果**：测试通过，证明 `repr(packed)` + zerocopy 的「视图」与原始结构体在内存表达上完全等价。

> 待本地验证：`cargo test --lib handshake::messages::tests`。

#### 4.4.5 小练习与答案

**练习 1**：如果把一段长度为 100 的字节喂给 `Initiation::parse`，会发生什么？返回什么错误？

> 参考答案：`LayoutVerified::new` 因长度(100) ≠ `size_of::<Initiation>()`(148) 返回 `None`，`parse` 据此返回 `Err(HandshakeError::InvalidMessageFormat)`，不会触及 `f_type` 校验。

**练习 2**：为什么在 `repr(packed)` 结构体里，读 `f_type` 用 `.get()` 而不是直接 `msg.noise.f_type`？

> 参考答案：`packed` 结构体的字段可能位于任意字节偏移，不保证对齐。直接对它取引用（`&msg.noise.f_type`）会创建未对齐引用，是未定义行为（UB）。`U32<LittleEndian>::get()` 内部用 `ptr::read_unaligned` 按值读出，既安全又能正确处理小端序。

## 5. 综合实践

把本讲的知识串起来，完成一个「线路格式速查表 + 自动校验」小任务：

1. **画一张字节布局图**：横向画出 Initiation 的 148 字节，标注每个字段（`f_type` 4 / `f_sender` 4 / `f_ephemeral` 32 / `f_static` 48 / `f_timestamp` 28 / `f_mac1` 16 / `f_mac2` 16）的偏移区间，并标出哪些是明文、哪些是「密文+tag」。
2. **写一个校验测试**（示例代码，加在 messages.rs 测试模块）：

```rust
#[test]
fn handshake_message_layout_cheatsheet() {
    use std::mem;

    // 零件
    assert_eq!(SIZE_MAC, 16);
    assert_eq!(SIZE_TAG, 16);
    assert_eq!(SIZE_X25519_POINT, 32);
    assert_eq!(SIZE_TIMESTAMP, 12);

    // 外层消息总长
    assert_eq!(mem::size_of::<Initiation>(), 148);
    assert_eq!(mem::size_of::<Response>(), 92);
    assert_eq!(mem::size_of::<CookieReply>(), 64);

    // 「明文 + SIZE_TAG」字段
    assert_eq!(
        std::mem::size_of::<NoiseInitiation>()
            - 4 - 4 - SIZE_X25519_POINT, // 减去 type/sender/ephemeral
        SIZE_X25519_POINT + SIZE_TAG + SIZE_TIMESTAMP + SIZE_TAG // static + timestamp
    );

    // 最大握手消息
    assert_eq!(MAX_HANDSHAKE_MSG_SIZE, 148);
}
```

3. 运行 `cargo test --lib handshake_message_layout_cheatsheet`，确认全绿。

这个任务把「零件尺寸 → 子结构 → 外层消息 → MAX 常量 → parse 长度闸门」整条链路打通：一旦你改动了任何字段定义，这个测试会立刻告诉你线路长度变化了哪里。

## 6. 本讲小结

- WireGuard 握手只有三类定长报文：Initiation(148B)、Response(92B)、CookieReply(64B)，字段紧凑无填充，靠 `#[repr(packed)]` 与线路字节一一对应。
- `f_static`、`f_timestamp`、`f_cookie` 等「密文」字段遵循 **AEAD 密文 = 明文 + 16 字节 Poly1305 tag**，故其字节长度是「明文长度 + SIZE_TAG」；`f_ephemeral` 是明文临时公钥。
- `TYPE_INITIATION=1 / RESPONSE=2 / COOKIE_REPLY=3` 是首 4 字节小端类型字段，既内嵌于消息，也被 `device.rs::process` 用来分用。
- `MAX_HANDSHAKE_MSG_SIZE` 由 `const fn max` 在编译期对三者 `size_of` 取最大值得 148，`udp_worker` 据此分配接收缓冲。
- `parse()` 用 `zerocopy::LayoutVerified` 把字节切片零拷贝地视图化为结构体，`LayoutVerified::new` 充当长度闸门，`U32<LittleEndian>::get()` 用 `read_unaligned` 安全读取 packed 字段。

## 7. 下一步学习建议

本讲只讲了「报文长什么样、怎么零拷贝读」。接下来建议：

- **u4-l2 握手 Device 与 peer 管理**：看 `Device` 如何用公钥和 receiver id 映射 peer，以及 `allocate()` 如何分配 `f_sender`/`f_receiver` 这些本讲出现的 id 字段。
- **u4-l3 Noise IK 握手核心**：进入密码学，看 `create/consume_initiation` 如何填充本讲的 `f_ephemeral`、加密 `f_static`/`f_timestamp`、并最终派生会话密钥。
- **u4-l4 抗拒绝服务**：深入 `MacsFooter` 的 mac1/mac2 与 cookie 机制，以及 `CookieReply` 报文在 under-load 时的交互流程。

继续阅读建议直接打开 [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs)，对照本讲的字段名看它们在真实握手流程中被读写的位置。
