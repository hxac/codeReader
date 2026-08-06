# 发送管道：加密（SendJob）

## 1. 本讲目标

本讲精读 `src/wireguard/router/send.rs`，它是 WireGuard **出站数据面**的加密引擎。学完后你应该能够：

1. 说出传输报文（transport message）的线路格式，以及 `TransportHeader` 三个字段的含义。
2. 解释 nonce（一次性随机数）是如何用「4 字节零 + 8 字节 counter」构造的，并理解为何绝不能重复。
3. 看懂 `SendJob` 如何把「并行加密」与「串行发送 + 回调」拆成 `parallel_work` / `sequential_work` 两阶段。
4. 算清楚单个会话密钥最多能加密多少个报文（`REJECT_AFTER_MESSAGES` 约束）。
5. 理清 `Peer::send` 如何分配 nonce、如何在无密钥时把包暂存（staged）。

本讲承接 [u5-l1 路由器总览](u5-l1-router-overview.md)：那里讲了「并行加密、有序发送」的两层队列骨架，本讲把骨架里 `SendJob` 这条出站支线彻底打开。

---

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 AEAD 与「nonce 不可重复」铁律

传输报文用 **ChaCha20-Poly1305** 这类 AEAD（认证加密）算法加密。AEAD 有一个绝对铁律：**同一把密钥下，nonce 绝不能重复**。一旦 (key, nonce) 组合被复用，ChaCha20 的密钥流会重复暴露，攻击者异或两份密文即可还原明文，Poly1305 的认证也会被攻破。

WireGuard 的解决方案极其简洁：发送端维护一个**单调递增的 64 位计数器 counter**，每个报文 counter 加 1，把 counter 直接编码进 nonce。只要 counter 不回绕、不重复，nonce 就天然唯一。因此「counter 不能超过 `REJECT_AFTER_MESSAGES`」不是性能调参，而是**密码学安全边界**。

### 2.2 原地加密（in-place / seal-in-place）

性能敏感的数据面不会「先 copy 出一份明文再加密」。WireGuard 直接在承载报文的同一块缓冲区上做加密：

- 缓冲区前 16 字节预留为传输头 `TransportHeader`（加密前写入明文头字段）。
- 中间是明文 IP 包载荷。
- 末尾 `extend` 出 16 字节空间存放 Poly1305 认证标签。

加密就在中间区域就地完成，标签写到末尾。一块缓冲区从「明文包」原地变身为「完整密文报文」，零拷贝。

### 2.3 两阶段任务：parallel_work 与 sequential_work

加密（CPU 密集、可并行）与「按序写到 UDP」（有副作用、必须保序）是两种性质相反的工作。路由器把它们拆开：

- `parallel_work`：任意一个 worker 线程都可以执行，只做无副作用的加密，完成后置 `ready=true`。
- `sequential_work`：由 per-peer 的保序队列 `Queue` 保证**按入队顺序**逐个执行，做真正发包 + 触发定时器回调。

这正是 u5-l1 讲的「并行加密、有序发送」。本讲的 `SendJob` 就是这套机制在出站方向的具体实现。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/wireguard/router/send.rs` | 本讲主角。`SendJob` 的定义、`parallel_work`（加密）、`sequential_work`（发送+回调）。 |
| `src/wireguard/router/messages.rs` | 传输报文头 `TransportHeader` 与类型常量 `TYPE_TRANSPORT`。 |
| `src/wireguard/router/peer.rs` | `Peer::send`（分配 nonce、暂存 staged 包）、`KeyWheel`、`EncryptionState`。 |
| `src/wireguard/router/device.rs` | `Device::send` 出站入口（按目的 IP 选 peer）；`EncryptionState` 定义。 |
| `src/wireguard/router/worker.rs` | worker 线程循环：取 `JobUnion::Outbound` → `parallel_work` → `consume`。 |
| `src/wireguard/constants.rs` | `REJECT_AFTER_MESSAGES`、`REKEY_AFTER_MESSAGES` 等密码学边界常量。 |
| `src/wireguard/router/mod.rs` | `SIZE_TAG`、`SIZE_MESSAGE_PREFIX`、`CAPACITY_MESSAGE_POSTFIX` 推导。 |

依赖方向（回顾）：`tun_worker`（读 TUN）→ `Device::send`（选 peer）→ `Peer::send`（造 `SendJob`）→ worker 线程池 → `SendJob::parallel_work` / `sequential_work` → `send_raw` → UDP。

---

## 4. 核心概念与源码讲解

### 4.1 传输报文线路格式与 TransportHeader

#### 4.1.1 概念说明

WireGuard 完成握手后，所有用户数据都封装成**传输报文（transport message）**通过 UDP 发送。它的线路布局非常规整：

```
+-------------------+-----------------------------+------------+
| TransportHeader   | 密文载荷 (encrypted payload) | Poly1305   |
| (16 字节, 明文)    |                              | tag (16B)  |
+-------------------+-----------------------------+------------+
```

其中头部是明文（它告诉对端用哪把密钥、哪个 counter 解密），载荷被加密，末尾是 16 字节认证标签。注意「头部明文」是设计如此——头部只含 receiver id 和计数器，不是秘密。

#### 4.1.2 核心流程

`TransportHeader` 是一个 16 字节的 `#[repr(packed)]` 结构，三个字段从低到高依次铺开：

| 字段 | 类型 | 字节 | 含义 |
|------|------|------|------|
| `f_type` | `U32<LittleEndian>` | 4 | 报文类型，固定为 `TYPE_TRANSPORT = 4` |
| `f_receiver` | `U32<LittleEndian>` | 4 | 接收方 receiver id，告诉对端「用哪把会话密钥解密我」 |
| `f_counter` | `U64<LittleEndian>` | 8 | 计数器，用于构造 nonce 与防回放 |

合计 \(4+4+8 = 16\) 字节，正是 `SIZE_MESSAGE_PREFIX`。

#### 4.1.3 源码精读

[src/wireguard/router/messages.rs:5-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/messages.rs#L5-L13) 定义了传输头与类型常量：

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

几个要点：

- `#[repr(packed)]`：取消结构体填充，字段在内存里与线路字节一一对应，没有对齐空隙。
- `U32`/`U64` 来自 `zerocopy::byteorder`，是小端序的「网络字节包装类型」，跨平台一致。
- `FromBytes`/`AsBytes`：zerocopy 的 trait，允许把这 16 字节当作结构体安全读写而无需序列化拷贝。

`SIZE_MESSAGE_PREFIX` 由编译期 `size_of` 推导得到 16，见 [src/wireguard/router/mod.rs:26-28](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L26-L28)：

```rust
pub const SIZE_TAG: usize = 16;
pub const SIZE_MESSAGE_PREFIX: usize = mem::size_of::<TransportHeader>(); // = 16
pub const CAPACITY_MESSAGE_POSTFIX: usize = SIZE_TAG;
```

这两个常量决定了缓冲区布局：前缀 16 字节给头、后缀 16 字节给标签。

#### 4.1.4 代码实践

**目标**：亲手确认 `TransportHeader` 大小与字段布局。

**步骤**：

1. 阅读上面的 `messages.rs:5-13`。
2. 在草稿纸上按小端序画出 `f_type=4`、`f_receiver=1`、`f_counter=42` 时的 16 字节：`04 00 00 00 | 01 00 00 00 | 2a 00 00 00 00 00 00 00`。

**预期结果**：

- `SIZE_MESSAGE_PREFIX` = 16。
- 整条传输报文最小长度（keepalive，载荷为空）= 头 16 + 标签 16 = 32 字节，这与 `send_keepalive` 分配 `vec![0u8; SIZE_MESSAGE_PREFIX]`（仅 16 字节头）后再由 `parallel_work` 补 16 字节标签一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `f_receiver` 要放在报文头里、且是明文？

**答案**：接收端在解密之前必须先知道「这条报文该用哪把会话密钥解密」。`f_receiver` 是握手阶段协商出的 4 字节 receiver id，相当于密钥的索引。它必须是明文，否则对端无从选择解密密钥。它本身不是秘密——知道了 id 也无法推出密钥。

**练习 2**：传输头是 `repr(packed)`，但代码里直接用 `header.f_counter.set(...)` 写值，会有未对齐访问风险吗？

**答案**：不会出问题，因为 zerocopy 的 `U32`/`U64` 内部用 `read_unaligned` / `write_unaligned` 访问，显式支持 `packed` 结构体的非对齐读写，这正是 zerocopy 配合 `repr(packed)` 解析网络报文的标准用法。

---

### 4.2 nonce 构造与 REJECT_AFTER_MESSAGES 约束

#### 4.2.1 概念说明

ChaCha20-Poly1305 的 nonce 长度是 **96 位（12 字节）**。WireGuard 把这 12 字节规定为：

```
nonce = [0u8; 4]  ||  counter (8 字节, 小端)
        \_ 4 字节 _/   \_______ 8 字节 ______/
```

即**高 4 字节恒零，低 8 字节是单调递增的 counter**。这样 nonce 与 counter 一一对应，只要 counter 唯一，nonce 就唯一，满足 AEAD 安全铁律。

但 counter 是 64 位无符号整数，而一台机器理论上能发无穷多个包。于是必须有上限：当 counter 接近 `REJECT_AFTER_MESSAGES` 时，**密钥被视为「过期」并被丢弃**，触发重新握手换取新密钥。这是一个密码学安全阀，不是性能参数。

#### 4.2.2 核心流程

- ChaCha20-Poly1305 的 nonce 长度恒为 12 字节（代码用 `CHACHA20_POLY1305.nonce_len()` 校验）。
- nonce 构造：先建 12 字节全零数组，再把 `f_counter`（8 字节小端）拷进 `nonce[4..12]`，高 4 字节保持零。
- 安全边界：`REJECT_AFTER_MESSAGES` 取 `u64::MAX - 16`。发送前在 `Peer::send` 用 `state.nonce >= REJECT_AFTER_MESSAGES - 1` 判断密钥是否到期。

#### 4.2.3 源码精读

nonce 构造在 [src/wireguard/router/send.rs:88-92](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L88-L92)：

```rust
// create a nonce object
let mut nonce = [0u8; 12];
debug_assert_eq!(nonce.len(), CHACHA20_POLY1305.nonce_len());
nonce[4..].copy_from_slice(header.f_counter.as_bytes());
let nonce = Nonce::assume_unique_for_key(nonce);
```

- `nonce[4..]` 即第 4..12 字节（共 8 字节）写入 counter；前 4 字节保持零。
- `header.f_counter.as_bytes()` 取出 `U64<LittleEndian>` 的小端字节表示。

`REJECT_AFTER_MESSAGES` 定义在 [src/wireguard/constants.rs:4-5](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L4-L5)：

```rust
pub const REKEY_AFTER_MESSAGES: u64 = 1 << 60;
pub const REJECT_AFTER_MESSAGES: u64 = u64::MAX - (1 << 4);
```

注意两个常量的区别（详见小练习）：

- `REKEY_AFTER_MESSAGES = 2^60`：到这个量**主动**重新握手（提前换key，未雨绸缪）。
- `REJECT_AFTER_MESSAGES = u64::MAX - 16 = 2^64 - 17`：到这个量**强制拒绝**（硬性安全边界，必须丢弃密钥）。

`parallel_work` 里还有一道调试期断言 [src/wireguard/router/send.rs:80-83](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L80-L83)，确保进入加密时 counter 一定在边界内：

```rust
debug_assert!(
    job.counter < REJECT_AFTER_MESSAGES,
    "should be checked when assigning counters"
);
```

真正的运行期拦截在 `Peer::send`（4.5 节）。

#### 4.2.4 代码实践

**目标**：写一段注释解释 nonce 布局，并计算单密钥最大可加密报文数。

**操作步骤**（源码阅读 + 计算型实践）：

1. 打开 `src/wireguard/router/send.rs:88-92`，在 `nonce[4..].copy_from_slice(...)` 上方加注释（仅本地学习用，不提交）：

   ```rust
   // nonce 布局（共 12 字节）：
   //   字节 [0..4]  = 0x00 00 00 00   （高 4 字节恒零）
   //   字节 [4..12] = counter 小端     （低 8 字节为单调计数器）
   // 因为 ChaCha20-Poly1305 nonce 是 96 位，而 counter 是 64 位，
   // 剩余的高 32 位填零即可。nonce 与 counter 一一对应，
   // counter 不重复 ⇒ nonce 不重复 ⇒ 满足 AEAD 安全性。
   ```

2. 计算「单密钥最大可加密报文数」。在 `Peer::send` 里密钥过期判定为 `state.nonce >= REJECT_AFTER_MESSAGES - 1`（[peer.rs:266](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L266)），即只有 `nonce < REJECT_AFTER_MESSAGES - 1` 才能分配。

**计算过程**：

\[
\text{REJECT\_AFTER\_MESSAGES} = u64::MAX - 16 = 2^{64} - 1 - 16 = 2^{64} - 17
\]

允许分配的 nonce 满足 `nonce < (2^{64}-17) - 1`，即 `nonce \le 2^{64} - 19`。可取值 \(0, 1, \dots, 2^{64}-19\)，共

\[
\text{最大报文数} = (2^{64}-19) - 0 + 1 = 2^{64} - 18 = 18{,}446{,}744{,}073{,}709{,}551{,}598
\]

即约 \(1.84 \times 10^{19}\) 个报文。

**预期结果**：单个会话密钥最多加密 \(2^{64} - 18\) 个报文。这是一个天文数字，实际中密钥远在此之前（\(2^{60}\) 或 120 秒）就会被 `REKEY_AFTER_MESSAGES` / 定时器主动轮换。`REJECT_AFTER_MESSAGES` 只是最后一道不可逾越的密码学护栏。

> 待本地验证：可在测试里断言 `REJECT_AFTER_MESSAGES == u64::MAX - 16` 并打印上述计算结果。

#### 4.2.5 小练习与答案

**练习 1**：`REKEY_AFTER_MESSAGES`（\(2^{60}\)）和 `REJECT_AFTER_MESSAGES`（\(2^{64}-17\)）为什么相差如此之大？

**答案**：前者是「主动换 key」阈值——在密钥被用坏之前提前、从容地重新握手，保证换 key 期间不丢包；后者是「硬性拒绝」阈值——AEAD 安全所允许的 counter 上限，永远不该真的触达。两者之间留出 \(2^{64}-2^{60}\) 的巨大余量，确保即使主动换 key 失败（对端无响应），仍有充裕时间重试，而不会撞上硬边界导致通信中断。

**练习 2**：如果把 nonce 的低 8 字节改成只放 4 字节 counter，会发生什么？

**答案**：counter 最多只能到 \(2^{32}\)，远小于 `REJECT_AFTER_MESSAGES`，单密钥可用报文数从 \(2^{64}-18\) 暴跌到约 42 亿，且需要更频繁地重新握手。WireGuard 用满 64 位 counter 正是为了把密钥利用率推到 AEAD 允许的极限。

---

### 4.3 SendJob::parallel_work：原地加密

#### 4.3.1 概念说明

`SendJob` 是出站方向的「加密任务单元」。每个待发送的 IP 包对应一个 `SendJob`，它持有：缓冲区、分配到的 counter、所用密钥对 `KeyPair`、所属 peer。`parallel_work` 完成全部密码学计算——**写头、构造 nonce、原地加密、附加标签**，完成后把 `ready` 置 true。这一步没有任何 IO 副作用，可以被任意 worker 线程并发执行。

#### 4.3.2 核心流程

`parallel_work` 的执行序列（伪代码）：

```
parallel_work(job):
    msg = job.buffer.lock()
    msg.extend([0u8; 16])                      # 末尾腾出标签空间
    (header, packet) = LayoutVerified::new_from_prefix(msg)
    header.f_type     = TYPE_TRANSPORT
    header.f_receiver = keypair.send.id        # 告诉对端用哪把密钥
    header.f_counter  = job.counter             # 构造 nonce 用
    nonce = [0;4] || counter_le64
    key   = LessSafeKey(CHACHA20_POLY1305, keypair.send.key)
    tag   = key.seal_in_place_separate_tag(nonce, Aad::empty(),
                                           packet[.. tag_offset])
    packet[tag_offset..] = tag                  # 附加标签
    job.ready = true (Release)
```

要点：

- `LayoutVerified::new_from_prefix` 把缓冲区首 16 字节「视图化」为 `TransportHeader`，其余视为载荷 `packet`——零拷贝。
- `seal_in_place_separate_tag` 是 ring 的就地加密 API：直接在 `packet` 上加密，**单独返回** 16 字节标签（因为末尾空间是临时 extend 出来的，标签要显式拷回）。
- `Aad::empty()`：传输报文不带附加认证数据（握手报文才会用 H 作 Aad，见 u4-l3）。

#### 4.3.3 源码精读

`SendJob` 与其内部状态见 [src/wireguard/router/send.rs:17-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L17-L49)：

```rust
struct Inner<E: Endpoint, C: Callbacks, T: tun::Writer, B: udp::Writer<E>> {
    ready: AtomicBool,
    buffer: Mutex<Vec<u8>>,
    counter: u64,
    keypair: Arc<KeyPair>,
    peer: Peer<E, C, T, B>,
}
```

`SendJob` 本身只是 `Arc<Inner>` 的包装，可廉价 clone 进队列与 worker。`ready` 用原子布尔标记加密是否完成，是串行阶段 `is_ready` 的判据。

`parallel_work` 主体见 [src/wireguard/router/send.rs:59-109](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L59-L109)，关键三段：

第一段——腾标签空间 + 头部视图化（[send.rs:68-77](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L68-L77)）：

```rust
let mut msg = job.buffer.lock();
msg.extend([0u8; SIZE_TAG].iter());           // 末尾补 16 字节放标签
let (mut header, packet): (LayoutVerified<&mut [u8], TransportHeader>, &mut [u8]) =
    LayoutVerified::new_from_prefix(&mut msg[..])
        .expect("earlier code should ensure that there is ample space");
```

第二段——写头字段（[send.rs:84-86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L84-L86)）：

```rust
header.f_type.set(TYPE_TRANSPORT);
header.f_receiver.set(job.keypair.send.id);
header.f_counter.set(job.counter);
```

第三段——构造密钥、就地加密、附加标签（[send.rs:95-104](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L95-L104)）：

```rust
let tag_offset = packet.len() - SIZE_TAG;
let key = LessSafeKey::new(
    UnboundKey::new(&CHACHA20_POLY1305, &job.keypair.send.key[..]).unwrap(),
);
let tag = key
    .seal_in_place_separate_tag(nonce, Aad::empty(), &mut packet[..tag_offset])
    .unwrap();
packet[tag_offset..].copy_from_slice(tag.as_ref());
```

最后把 `ready` 以 `Release` 顺序写入（[send.rs:108](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L108)），`Release` 保证上面的加密写入对随后 `Acquire` 读 `ready` 的串行阶段可见——这是跨线程「加密完成」可见性的内存屏障。

> 术语解释：`seal_in_place_separate_tag` 中「seal」= 加密并生成认证标签，「in_place」= 在原缓冲区上加密不拷贝，「separate_tag」= 标签不追加在密文尾部而是作为返回值单独给出（因为本项目要把它写到预留的固定位置）。

#### 4.3.4 代码实践

**目标**：跟踪一处加密缓冲区从「明文 IP 包」到「完整密文报文」的长度变化。

**操作步骤**（源码阅读型）：

1. 假设 `tun_worker` 读到一个 100 字节的明文 IP 包（已按 16 对齐，padding 后仍 100）。
2. 缓冲区进入 `Peer::send` 时长度 = `SIZE_MESSAGE_PREFIX + 100 = 116` 字节（前 16 是头预留，后 100 是载荷）。
3. 在 `parallel_work` 中：
   - `extend([0u8; 16])` → 长度变 132。
   - 头 16 字节视图化为 `TransportHeader`，载荷 `packet` = 中间 \(132-16 = 116\) 字节？注意：此时 `packet` 实际是去掉头之后的 116 字节，其中前 100 是明文载荷、后 16 是刚 extend 的标签空间。
   - `tag_offset = packet.len() - 16 = 100`；加密 `packet[..100]`（就地），生成 16 字节标签写入 `packet[100..116]`。
4. 最终 `msg` = 16（头）+ 100（密文）+ 16（标签）= 132 字节的完整传输报文。

**预期结果**：传输报文长度 = 头 16 + 明文载荷长度 + 标签 16。这与 `router/mod.rs` 的 `message_data_len(payload) = payload + size_of::<TransportHeader>() + SIZE_TAG` 完全一致。

**需要观察的现象**：加密前后 `msg` 的前 16 字节（头）会被填入 `type/receiver/counter`，中间被改写为不可识别的密文，末尾 16 字节从零变成标签。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `parallel_work` 用 `expect("...ample space")` 而 `recv` 路径（u5-l3）的 `LayoutVerified` 失败会返回 `RouterError`？

**答案**：发送缓冲区的布局完全由本设备自己控制（`tun_worker` 按 `SIZE_MESSAGE_PREFIX + padding` 分配），头空间必然充足，失败只可能是内部 bug，故用 `expect` 直接 panic。而接收报文来自网络、由对端构造，长度可能畸形，必须优雅处理为 `MalformedTransportMessage` 错误而非 panic。

**练习 2**：`ready.store(true, Ordering::Release)` 为何用 `Release`，而 `is_ready` 用 `Acquire`？

**答案**：`Release`/`Acquire` 是配对的内存序。worker 线程在 `Release` 写 `ready` 前，对缓冲区的加密写入必须对「稍后 `Acquire` 读到 `ready==true` 的串行线程」可见。若用 `Relaxed`，串行线程可能看到 `ready==true` 却读到未加密完成的旧数据——这是典型的跨线程发布/订阅场景，必须用 Release-Acquire 建立 happens-before 关系。

---

### 4.4 SendJob::sequential_work：发送与回调

#### 4.4.1 概念说明

加密完成后，`SendJob` 进入串行阶段。这一阶段由 per-peer 的保序队列保证按入队顺序执行（详见 u5-l4 的 `Queue::consume` 与 contenders 互斥），职责是两件**有副作用**的事：

1. **真正发包**：把完整密文报文通过 UDP 发往对端端点。
2. **触发回调**：调用 `C::send`，通知上层（定时器）「这个包发完了、大小多少、是否成功」。

之所以拆出来串行，是因为「按序发包」对 WireGuard 的防回放与定时器语义至关重要——报文乱序到达会影响 keepalive 计时与密钥确认逻辑。

#### 4.4.2 核心流程

```
sequential_work(job):                       # 仅当 is_ready() == true 才被调用
    msg = job.buffer.lock()
    xmit = job.peer.send_raw(&msg[..]).is_ok()   # 发包，记录是否成功
    C::send(&peer.opaque, msg.len(), xmit,       # 回调：大小、是否发出
            &job.keypair, job.counter)
```

`send_raw` 会取 peer 端点锁，经设备的 outbound writer 把密文写向 UDP（无端点则报错）。`C::send` 是路由器对外暴露的钩子，由 `wireguard/timers.rs` 的 `PeerInner` 实现来驱动定时器。

#### 4.4.3 源码精读

`SequentialJob` 实现见 [src/wireguard/router/send.rs:112-136](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L112-L136)：

```rust
impl ... SequentialJob for SendJob<...> {
    fn is_ready(&self) -> bool {
        self.0.ready.load(Ordering::Acquire)
    }

    fn sequential_work(self) {
        debug_assert_eq!(self.is_ready(), true, "doing sequential work on an incomplete job");
        log::trace!("processing sequential send job");

        let job = &self.0;
        let msg = job.buffer.lock();
        let xmit = job.peer.send_raw(&msg[..]).is_ok();

        // trigger callback (for timers)
        C::send(&job.peer.opaque, msg.len(), xmit, &job.keypair, job.counter);
    }
}
```

- `is_ready` 用 `Acquire` 读，与 `parallel_work` 的 `Release` 写配对（见 4.3.5）。
- `sequential_work` 消费 `self`（按值），因为保序队列只会执行它一次。
- `xmit` 是布尔：`send_raw` 成功为 true，失败（如无端点）为 false——回调用它区分「真发出去了」还是「因无端点被丢弃」。

`C::send` 的签名见 [src/wireguard/router/types.rs:29-35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L29-L35)：

```rust
pub trait Callbacks: Send + Sync + 'static {
    type Opaque: Opaque;
    fn send(opaque: &Self::Opaque, size: usize, sent: bool, keypair: &Arc<KeyPair>, counter: u64);
    fn recv(opaque: &Self::Opaque, size: usize, sent: bool, keypair: &Arc<KeyPair>);
    fn need_key(opaque: &Self::Opaque);
    fn key_confirmed(opaque: &Self::Opaque);
}
```

注意 `send` 比 `recv` 多一个 `counter: u64` 参数——发送侧需要知道自己用的是哪个 counter，以便定时器判断「当前密钥用了多少、是否该 rekey」（参见 u7-l1 的 `keep_key_fresh`）。

`send_raw` 的实现见 [src/wireguard/router/peer.rs:225-242](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L225-L242)：它取 peer 的 `endpoint` 锁，若端点存在且设备 outbound 开启（`outbound.0 == true`），则调用 `udp::Writer::write(msg, endpoint)` 发出；端点缺失返回 `RouterError::NoEndpoint`。

worker 线程如何驱动这两个阶段见 [src/wireguard/router/worker.rs:29-32](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/worker.rs#L29-L32)：

```rust
Ok(JobUnion::Outbound(job)) => {
    job.parallel_work();   # 1. 加密
    job.queue().consume(); # 2. 按序触发 sequential_work（仅队首 ready 的任务）
}
```

`parallel_work` 由当前 worker 立刻执行；`consume` 则在 per-peer 队列上按入队顺序推进，保证多个 worker 并发加密后仍按序发送（机制见 u5-l4）。

#### 4.4.4 代码实践

**目标**：理解 `xmit`（发送成功与否）如何影响定时器回调。

**操作步骤**（源码阅读型 + 推理）：

1. 阅读 `send.rs:131-134`，确认 `xmit = send_raw(...).is_ok()`。
2. 阅读 `peer.rs:225-242` 的 `send_raw`，列出 `xmit == false` 的两种情况：
   - peer 没有端点（`endpoint` 为 `None`）→ `NoEndpoint`。
   - 设备 outbound 被关闭（`outbound.0 == false`，即 `down` 态）→ 走 `Ok(())` 分支……注意此时 `is_ok()` 为 true！
3. 打开 `src/wireguard/timers.rs`（u7-l1 详解），找到 `impl Callbacks for PeerInner` 里的 `send`，观察它如何用 `sent` 参数更新 `tx_bytes` 与定时器。

**需要观察的现象**：

- down 态时 `send_raw` 返回 `Ok(())` 但实际不发包（`outbound.0 == false` 直接 `Ok(())`），所以 `xmit` 仍为 true——这是个微妙点：回调会收到 `sent=true` 即便没真发。
- 无端点时 `xmit=false`，定时器据此不计数、并可能触发 `need_key`/端点学习。

**预期结果**：能口头说清「`xmit` 不完全等于『真发出 UDP』，而是『`send_raw` 没报错』」。

> 待本地验证：在 `timers.rs` 的 `send` 回调里加日志，分别在没有 endpoint 与设备 down 时触发一次发包，对比 `sent` 值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `parallel_work` 是「无副作用、可并发」，而 `sequential_work` 必须「按序串行」？

**答案**：`parallel_work` 只在缓冲区上做加密并置 `ready`，不触碰任何共享可变状态（缓冲区是 job 私有的），多个 job 可由不同 worker 并发加密、互不干扰。而 `sequential_work` 调 `send_raw` 真正发包并更新对端收发字节统计、驱动定时器——这些是有序敏感的副作用，若并发执行会导致报文乱序、计数错乱，故必须由保序队列按入队顺序逐个执行。

**练习 2**：`sequential_work(self)` 取得 `self` 的所有权（按值消费），有何深意？

**答案**：它表示这个 job 在串行阶段「只执行一次」。保序队列的 `consume` 取出队首 ready 任务后调用它，调用完 job 即被销毁、`Arc` 引用计数减少。按值消费在类型层面阻止了「同一 job 被发送两次」的逻辑错误。

---

### 4.5 Peer::send：nonce 分配与暂存（staged）

#### 4.5.1 概念说明

`SendJob` 描述「单个包如何加密」，而 `Peer::send` 描述「peer 收到一个待发包后如何处置」。它是出站流水线在 peer 层的入口，由 `Device::send`（按目的 IP 选好 peer 后）调用。它要解决两个问题：

1. **nonce 分配**：从当前加密密钥的 `EncryptionState` 取出下一个 counter，造 `SendJob` 入队，并 `nonce += 1`。
2. **暂存（staged）**：若此刻**没有可用密钥**（尚未握手成功）或**密钥即将过期**，不能直接丢包——把包暂存到 `staged_packets`，等密钥就绪后由 `send_staged` 补发，保证用户数据不丢。

#### 4.5.2 核心流程

```
Peer::send(msg, stage):
    enc_key = self.enc_key.lock()
    match enc_key:
        None:                              # 完全没有密钥
            if stage: staged_packets.push(msg)
            need_key = true                # 触发握手
        Some(state):
            if state.nonce >= REJECT_AFTER_MESSAGES - 1:   # 密钥过期
                *enc_key = None             # 丢弃密钥
                if stage: staged_packets.push(msg)
                need_key = true
            else:
                job = SendJob::new(msg, state.nonce, state.keypair, peer)
                if self.outbound.push(job):   # 入保序队列
                    state.nonce += 1
                else:                          # 队列满，丢弃（不补发）
                    ...
    if need_key:
        C::need_key(&opaque)                # 请求上层发起握手
    if let Some(job):
        device.work.send(JobUnion::Outbound(job))  # 投递给 worker 池
```

三种结局：

- 有密钥且未过期 → 正常加密发送。
- 无密钥 / 过期 → 暂存 + 请求新密钥。
- 保序队列满 → 静默丢弃（背压），不暂存也不请求。

#### 4.5.3 源码精读

`Peer::send` 见 [src/wireguard/router/peer.rs:252-298](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L252-L298)。核心三分支：

无密钥分支（[peer.rs:256-263](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L256-L263)）：

```rust
None => {
    log::debug!("no key encryption key available");
    if stage {
        self.staged_packets.lock().push_back(msg);
    };
    (None, true)   // job=None, need_key=true
}
```

密钥过期分支（[peer.rs:265-272](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L265-L272)）——注意 `- 1` 与整数溢出防护：

```rust
if state.nonce >= REJECT_AFTER_MESSAGES - 1 {
    log::debug!("encryption key expired");
    *enc_key = None;          # 主动丢弃过期密钥
    if stage {
        self.staged_packets.lock().push_back(msg);
    }
    (None, true)
}
```

正常加密分支（[peer.rs:274-283](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L274-L283)）：

```rust
let job = SendJob::new(msg, state.nonce, state.keypair.clone(), self.clone());
if self.outbound.push(job.clone()) {
    state.nonce += 1;           # 分配成功，计数器前进
    (Some(job), false)
} else {
    (None, false)               # 队列满，丢弃且不再请求 key
}
```

收尾——请求密钥与投递任务（[peer.rs:288-297](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L288-L297)）：

```rust
if need_key {
    C::need_key(&self.opaque);              # 触发上层发起/重发握手
}
if let Some(job) = job {
    self.device.work.send(JobUnion::Outbound(job))   # 投给 worker 池
}
```

配套的暂存补发见 `send_staged`，[peer.rs:300-314](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L300-L314)：它在密钥确认（`confirm_key`）或新密钥就绪后，循环 `pop_front` 取出暂存包，以 `stage=false` 重新走 `send`（即密钥还在就发，没了也不再暂存，避免无限堆积）。

`EncryptionState` 的定义见 [src/wireguard/router/device.rs:41-44](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L41-L44)：

```rust
pub struct EncryptionState {
    pub(super) keypair: Arc<KeyPair>, // keypair
    pub(super) nonce: u64,            // next available nonce
}
```

`Device::send` 作为出站总入口，按目的 IP 选 peer 后调用 `peer.send(msg, true)`，见 [src/wireguard/router/device.rs:181-201](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L181-L201)：

```rust
pub fn send(&self, msg: Vec<u8>) -> Result<(), RouterError> {
    let packet = &msg[SIZE_MESSAGE_PREFIX..];      # 跳过头前缀看载荷
    let peer = self.state.table.get_route(packet)  # 按目的 IP 最长前缀匹配
        .ok_or(RouterError::NoCryptoKeyRoute)?;
    peer.send(msg, true);                           # stage=true：无密钥则暂存
    Ok(())
}
```

#### 4.5.4 代码实践

**目标**：跟踪一个包在「无密钥 → 握手成功 → 补发」全过程中的 staged 流转。

**操作步骤**（源码阅读 + 调用链追踪）：

1. 在 `Device::send`（device.rs:199）设想的调用点：设备刚启动、尚未握手，用户发一个包。此时 `enc_key` 为 `None`，包被 `push_back` 进 `staged_packets`，并触发 `C::need_key`。
2. 上层（timers）收到 `need_key`，发起握手（u4-l6）。
3. 握手成功，`add_keypair` 被调用（peer.rs:436）。若新密钥是 initiator，会调 `send_staged`（peer.rs:485）尝试补发；若 `send_staged` 没发出任何包，则发 keepalive 确认密钥（peer.rs:487）。
4. 对响应方，密钥在首个传输报文到达时由 `confirm_key`（peer.rs:316）转正，`confirm_key` 末尾也会调 `send_staged`（peer.rs:348）补发暂存包。

**需要观察的现象**：暂存包总是以「先进先出」（`ArrayDeque` 的 `push_back`/`pop_front`）补发，顺序得到保持。

**预期结果**：能画出「用户包 → staged_packets → (握手) → send_staged → SendJob → UDP」的完整时序，并指出 `stage=true` 仅在首次 `Device::send` 入口为 true，补发时为 false（防无限堆积）。

> 待本地验证：参考 `src/wireguard/tests.rs` 的 `test_pure_wireguard`（u7-l4），在握手前先注入一个包，观察握手完成后对端是否收到该包（即 staged 补发生效）。

#### 4.5.5 小练习与答案

**练习 1**：`Peer::send` 里密钥过期判断为何写成 `state.nonce >= REJECT_AFTER_MESSAGES - 1` 而非 `>= REJECT_AFTER_MESSAGES`？

**答案**：两个原因。其一，`- 1` 是为了在 `parallel_work` 的 `debug_assert!(job.counter < REJECT_AFTER_MESSAGES)` 留出一字节余量：用 `REJECT_AFTER_MESSAGES - 1` 作上界，分配到的 counter 最大为 `REJECT_AFTER_MESSAGES - 2`，严格小于 `REJECT_AFTER_MESSAGES`，断言恒成立。其二，`state.nonce` 是「下一个可用」counter，用 `>=` 提前一步停止，避免把临界值分配出去。

**练习 2**：当 `outbound.push` 返回 false（保序队列满）时，为何既不暂存也不 `need_key`？

**答案**：保序队列（容量 `INORDER_QUEUE_SIZE = 1024`）满意味着 worker 加密跟不上入队速度，是瞬时背压，与「有无密钥」无关。此时再请求新密钥无济于事，暂存也只是把队列压力转移到 `staged_packets`。WireGuard 选择直接丢弃该包（类似网络拥塞丢包），由上层协议（TCP）重传来恢复——这是有意的反压策略。

---

## 5. 综合实践

把本讲的知识串起来，完成一个**端到端调用链追踪 + 计算验证**任务：

**任务**：假设 MTU=1420，追踪一个 1300 字节明文 IPv4 包从 `tun_worker` 读出到 UDP 发出的完整旅程，回答下列问题。

1. **缓冲区演变**：写出 `msg` 在 `tun_worker` 分配时、进入 `Peer::send` 时、`parallel_work` 的 `extend` 后、加密完成后的四次长度。
2. **nonce 与 counter**：若此包是该密钥的第 5 个包（`state.nonce=4`），写出 `TransportHeader.f_counter` 的 8 字节小端值，并写出完整 12 字节 nonce。
3. **密钥寿命**：该密钥还能再加密多少个包？（用 4.2 节的结论）
4. **回调参数**：若 `send_raw` 成功，`C::send` 收到的 5 个参数分别是什么？

**参考答案**：

1. padding 后载荷按 16 对齐，1300 → 1300（1300 已是 4 的倍数但需对齐 16：⌈1300/16⌉×16 = 82×16 = 1312）。故：
   - `tun_worker` 分配：`1420 + 16 + 1 + 16 = 1453`（`size + CAPACITY_MESSAGE_POSTFIX`）。
   - `truncate(16 + 1312) = 1328` 进入 `Peer::send`。
   - `extend([0u8;16])` 后：1344。
   - 加密完成（不变）：1344 = 16 头 + 1312 密文 + 16 标签。
   - （padding 细节以 `padding()` 实际计算为准，可在本地用 `cargo test` 验证 1300→1312。）
2. `f_counter=4` → 小端 8 字节：`04 00 00 00 00 00 00 00`；nonce = `00 00 00 00 04 00 00 00 00 00 00 00`。
3. 已用 counter 0..4 共 5 个，剩余 = \((2^{64}-18) - 5 = 2^{64} - 23\) 个。
4. `C::send(opaque, msg.len()=1344, sent=true, &keypair, counter=4)`。

> 待本地验证：在 `parallel_work` 与 `sequential_work` 的 `log::trace!` 处开 `RUST_LOG=trace` 跑 `test_pure_wireguard`，对照真实日志验证上述长度与 counter。

---

## 6. 本讲小结

- 传输报文 = `TransportHeader`(16B 明文) + 密文载荷 + Poly1305 标签(16B)；头含 `f_type/f_receiver/f_counter`，`f_receiver` 让对端选密钥。
- nonce = `[0u8;4] || counter_le64`，counter 单调递增保证 nonce 唯一，满足 AEAD 安全铁律。
- `SendJob` 把出站拆成两阶段：`parallel_work` 做无副作用的就地 ChaCha20-Poly1305 加密并置 `ready`，`sequential_work` 按序发包并触发 `C::send` 回调。
- 单密钥最多加密 \(2^{64}-18\) 个报文（`REJECT_AFTER_MESSAGES = u64::MAX - 16` 约束），实际远在此之前由 `REKEY_AFTER_MESSAGES`(\(2^{60}\)) 主动轮换。
- `Peer::send` 负责 nonce 分配（`state.nonce += 1`）；无密钥或密钥过期时把包暂存 `staged_packets` 并 `C::need_key` 触发握手，握手后由 `send_staged` 补发，保数据不丢。
- `Release`/`Acquire` 内存序是「加密完成」跨线程可见性的关键；保序队列保证并行加密后仍按序发送。

---

## 7. 下一步学习建议

本讲只讲了**出站加密 + 发送**。要形成完整闭环，建议继续：

1. **u5-l3 接收管道（ReceiveJob）**：对称地看入站如何用同一套 `parallel_work`/`sequential_work` 模型做解密、防回放、确认密钥。
2. **u5-l4 有序队列 Queue**：深入 `contenders` 原子互斥如何实现「多线程并行加密、单线程按序发送」，彻底理解 `consume` 的保序机制。
3. **u5-l6 KeyWheel 与 Peer 生命周期**：看 `EncryptionState` 是如何由 `confirm_key`/`add_keypair` 装入、由 `Peer::send` 消费、过期后丢弃的——本讲的 `enc_key` 与 `staged_packets` 都在那里被驱动。
4. **u7-l1 定时器状态机**：看 `C::send`/`C::need_key` 回调如何驱动 `keep_key_fresh` 与重新握手，把本讲的回调与密钥轮转闭环。

建议阅读的下一个源码文件：`src/wireguard/router/receive.rs`（与 `send.rs` 对照阅读，体会对称设计）。
