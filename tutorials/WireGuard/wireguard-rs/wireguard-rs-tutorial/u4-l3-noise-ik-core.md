# Noise IK 握手核心

## 1. 本讲目标

本讲打开 `src/wireguard/handshake/noise.rs`，深入 WireGuard 握手的密码学心脏。读完本讲你应当能够：

- 说清 WireGuard 使用的握手模式 `Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s` 每一步做了什么 DH、HKDF、AEAD 操作，以及转录哈希（transcript hash）是如何一步步推进的。
- 看懂 `HASH/HMAC/KDF1/KDF2/KDF3/SEAL/OPEN` 这一整套宏如何把 Noise 规范的数学公式落地为 Rust 代码。
- 把 `create_initiation` 与 `consume_initiation`、`create_response` 与 `consume_response` 的「对称结构」一一对应起来。
- 理解如何从最终的链密钥经 `KDF2` 派生出收发密钥，并组装成 `KeyPair` 交给路由器。
- 解释 `shared_secret` 的零值检查与 `clear_stack_on_return_fnone` 的安全意义。

本讲承接 u4-l2（`Device` 与 peer 管理）。`Device::begin/process` 是「调度入口」，而本讲讲的是它们调用的真正算密码的四个函数。不涉及报文线路格式（u4-l1）、抗 DoS（u4-l4）、时间戳重放（u4-l5）。

## 2. 前置知识

### Noise 协议框架

Noise 是一套「模式化」的密钥协商框架。一个 Noise 协议由模式名完全决定，例如 WireGuard 用的 `Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s`：

- `Noise`：框架名。
- `IK`：握手模式。`I` 表示发起者（initiator）预先知道响应者的静态公钥；第二个字母描述后续报文里的 DH 操作。
- `psk2`：在第二条消息（response）之后混入一个预共享密钥（PSK）。
- `25519`：DH 用 Curve25519（X25519）。
- `ChaChaPoly`：AEAD 用 ChaCha20-Poly1305。
- `BLAKE2s`：哈希与 HMAC 用 BLAKE2s。

Noise 的核心是两个随握手推进而不断更新的状态量：

- **链密钥 C（chaining key）**：32 字节。每做一次 DH 都通过 HKDF 把结果「揉」进 C，使 C 不断进化。
- **哈希转录 H（hash transcript）**：32 字节。每发出/收到一段数据（临时公钥、密文）都把它哈希进 H，用来「防篡改 + 绑定上下文」。H 也常作为 AEAD 的附加认证数据（AD）。

握手结束时，C 再做一次 KDF，拆出对称的收发密钥，交给数据面。

### HKDF 与 Noise 的 KDF

Noise 的 `KDF` 本质是两段 HKDF（基于 HMAC）。给定链密钥 `ck` 和输入 `input`：

\[ \text{prk} = \text{HMAC}(\text{ck},\ \text{input}) \]

然后派生若干个输出块：

\[
\begin{aligned}
t_1 &= \text{HMAC}(\text{prk},\ 0\text{x}01) \\
t_2 &= \text{HMAC}(\text{prk},\ t_1 \,\|\, 0\text{x}02) \\
t_3 &= \text{HMAC}(\text{prk},\ t_2 \,\|\, 0\text{x}03)
\end{aligned}
\]

`KDF1` 只取 \(t_1\)；`KDF2` 取 \((t_1, t_2)\)；`KDF3` 取 \((t_1, t_2, t_3)\)。约定上，\(t_1\) 常作为新的链密钥 C，\(t_2\) 作为本次加密用的密钥 k。

### AEAD（ChaCha20-Poly1305）

AEAD 同时做「加密 + 认证」。`SEAL(key, ad, pt) -> ct` 用 key 加密明文 pt，并把 ad（附加数据）纳入认证（ad 不被加密，但被认证）。`OPEN` 是其逆运算，认证失败则整体拒绝。WireGuard 握手里 AEAD 的 nonce 恒为零（见 `ZERO_NONCE`），因为每把 key 只用一次。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/wireguard/handshake/noise.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs) | 本讲主角。预计算常量、密码学宏、`shared_secret`，以及 `create/consume_initiation`、`create/consume_response` 四个函数。 |
| [src/wireguard/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs) | 定义握手产物 `Key` / `KeyPair`（Drop 时清零）。 |
| [src/wireguard/handshake/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs) | `HandshakeError` 枚举、握手返回类型 `Output`、`Psk`。 |
| [src/wireguard/handshake/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs) | `State` 枚举（保存 InitiationSent 期间的 C/H/临时私钥），其 Drop 清零转录。 |
| [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs) | `KeyState`（本端静态密钥对）与 `begin/process` 调用入口，是本讲四个函数的调用方。 |

## 4. 核心概念与源码讲解

### 4.1 预计算常量与密码学原语宏

#### 4.1.1 概念说明

Noise 要求握手一开始把协议名和标识符「哈希」进初始状态。这两步的输入是固定字符串，输出也是固定的 32 字节，所以 wireguard-rs 把它们**预计算成常量**，避免每次握手都重算。

- `INITIAL_CK = Hash("Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s")`，即初始链密钥 C。
- `INITIAL_HS = Hash(INIAL_CK || "WireGuard v1 zx2c4 Jason@zx2c4.com")`，即初始哈希转录 H。这个标识符字符串是 WireGuard 特有的「prologue」之外的二次绑定。

随后整个握手就是反复调用六个宏：`HASH`（纯哈希）、`HMAC`、`KDF1/2/3`（基于 HMAC 的 HKDF）、`SEAL/OPEN`（AEAD 加解密）。

#### 4.1.2 核心流程

预计算 + 宏的整体关系：

1. 握手函数进入时，`ck = INITIAL_CK`，`hs = INITIAL_HS`。
2. 每一步用 `KDF*` 把 DH 结果揉进 `ck`（推进链密钥）。
3. 每一步用 `HASH` 把发出的/收到的字节揉进 `hs`（推进转录）。
4. 需要加密时用 `SEAL`，把当时的 `hs` 当作 AD；解密对称地用 `OPEN`。
5. 握手末尾用一次 `KDF2(ck, [])` 拆出收发密钥。

#### 4.1.3 源码精读

预计算常量直接写成字节字面量。注意 `C := Hash(Construction)`、`H := Hash(C || Identifier)` 这两行注释正是 Noise 规范的原始记号：

[src/wireguard/handshake/noise.rs:47-59](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L47-L59) —— 预计算初始链密钥 `INITIAL_CK`、初始转录 `INITIAL_HS` 与全零 nonce。

文件末尾的测试用 `CONSTRUCTION` / `IDENTIFIER` 两个原始字符串重新算一遍，验证常量确实等于规范值：

[src/wireguard/handshake/noise.rs:140-150](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L140-L150) —— `precomputed_chain_key` 与 `precomputed_hash` 两个健全性测试。

`HASH!` 宏就是对任意多段输入做 BLAKE2s 串联哈希：

[src/wireguard/handshake/noise.rs:61-70](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L61-L70) —— `HASH!` 把所有输入依次 `update` 后 `finalize`。

`HMAC!` 用 `Blake2s` 构造 HMAC（`HMACBlake2s = Hmac<Blake2s>`，[第 35 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L35)）：

[src/wireguard/handshake/noise.rs:72-81](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L72-L81) —— `HMAC!` 以 `key` 为密钥对输入做 HMAC。

三个 KDF 宏把第 2 节的公式原样翻译。`KDF1` 只返回 \(t_1\) 并清零中间量 `t0`（= prk）：

[src/wireguard/handshake/noise.rs:83-111](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L83-L111) —— `KDF1/KDF2/KDF3`。注意 `t0.clear()` 在返回前清掉 prk，是密钥卫生习惯。

`SEAL!`/`OPEN!` 包装 ChaCha20-Poly1305，固定用 `ZERO_NONCE`，解密失败统一映射为 `HandshakeError::DecryptionFailure`：

[src/wireguard/handshake/noise.rs:113-129](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L113-L129) —— `SEAL!` 把明文加密后拷进 `$ct` 缓冲；`OPEN!` 反向解密进 `$pt`。

> 提示：`SEAL!`/`OPEN!` 的第 4 个参数是「密文输出/输入缓冲」。调用方事先在消息结构里留好「明文长度 + 16 字节 Poly1305 标签」的空间（见 u4-l1 的 `f_static = [u8; 32+16]`），就地写入，零分配。

#### 4.1.4 代码实践

1. **目标**：验证预计算常量与 KDF 实现的正确性。
2. **操作**：运行 noise 模块自带的三个测试。
   ```bash
   cargo test --lib handshake::noise::tests
   ```
3. **现象**：编译并执行 `precomputed_chain_key`、`precomputed_hash`、`hkdf` 三个用例。
4. **预期结果**：三个测试全部通过。其中 `hkdf` 用例（[第 156-212 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L156-L212)）用 WireGuard-Go 生成的测试向量逐字节核对了 `KDF1/2/3` 的输出，通过即说明 HKDF 实现与参考实现一致。
5. 若环境无法编译（缺 nightly 等），此项「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `INITIAL_CK` 可以写成常量，而 `eph_sk`（临时私钥）不能？

**答案**：`INITIAL_CK` 的输入 `Construction` 字符串是固定的，输出确定，故可预计算成常量省去每次哈希。`eph_sk` 是每次握手用 RNG 现场生成的随机数，必须动态产生，否则重用临时密钥会破坏前向安全性。

**练习 2**：`KDF2!(ck, input)` 返回 `(t1, t2)`。在握手代码里，`t1` 和 `t2` 一般分别扮演什么角色？

**答案**：`t1` 作为新的链密钥 C（继续往下推进），`t2` 作为本次 AEAD 加密用的密钥 k。

---

### 4.2 DH 共享密钥与零值检查（`shared_secret`）

#### 4.2.1 概念说明

X25519 有一个数学性质：如果对端公钥是一个「低阶点」（small subgroup 里的特殊点），`diffie_hellman` 会算出全零共享密钥。Noise 规范对此有一套「fallback」处理（用上一把链密钥顶替），而 WireGuard 内核实现选择**直接拒绝**全零共享密钥。wireguard-rs 为了「与内核绝对等价」（注释原话：`strive for absolute equivalent behavior`），也显式拒绝零共享密钥。

#### 4.2.2 核心流程

```
ss = sk.diffie_hellman(pk)
if ss 常时间等于 [0;32]:
    返回 InvalidSharedSecret
else:
    返回 ss
```

关键是「常时间比较」：用 `subtle::ConstantTimeEq`（`ct_eq`），避免普通比较 `==` 在字节不同时提前短路而泄漏时序信息。

#### 4.2.3 源码精读

[src/wireguard/handshake/noise.rs:220-228](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L220-L228) —— `shared_secret` 包装 dalek 的 DH，加一道零值检查。

注意 `create_initiation` 进入时还有**第二道**零值检查——针对**预计算**的静态-静态共享密钥 `peer.ss`：

[src/wireguard/handshake/noise.rs:240-243](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L240-L243) —— 若 `peer.ss` 为全零（本端还没设私钥，或对端公钥异常），直接拒绝发起握手。

这两道检查互补：`shared_secret` 守的是临时 DH 的结果；`peer.ss` 检查守的是静态-静态 DH（在 `Device::set_sk/add` 时由 `update_ss` 预计算，见 u4-l2）。

#### 4.2.4 代码实践

1. **目标**：理解零值检查的触发条件。
2. **操作**：阅读 [device.rs 的 `update_ss`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L106-L127)。当设备没有私钥（`keyst = None`）时，`peer.ss` 会被清成什么值？
3. **现象/预期**：`update_ss` 在 `keyst = None` 分支执行 `peer.ss.clear()`（全零）。于是后续 `create_initiation` 进入时的 `peer.ss.ct_eq(&[0u8;32])` 必然为真，握手在第一道关卡就被 `InvalidSharedSecret` 拒绝。
4. 结论：**没有私钥就无法发起握手**，零值检查既是抗小子群攻击，也顺带做了「未配置」的兜底。

#### 4.2.5 小练习与答案

**练习**：为什么零值比较必须用 `ct_eq`（常时间），而不能写 `ss.as_bytes() == &[0u8;32]`？

**答案**：普通 `==` 在第一个不同的字节就返回，耗时与「前缀相同长度」相关，可能让攻击者通过时序推测密钥内容。`ct_eq` 对所有字节都做完整的逻辑运算，耗时与输入无关，不泄漏时序。

---

### 4.3 创建与消费 Initiation（消息 1）

#### 4.3.1 概念说明

握手第一报文 `Initiation` 由**发起者**用 `create_initiation` 生成，由**响应者**用 `consume_initiation` 解析。两者必须算出**完全相同**的 `(C, H)` 中间状态——因为响应者随后要用它继续算 response。

`Initiation` 里携带：
- 临时公钥 `f_ephemeral`（明文）。
- 发起者静态公钥 `f_static`（用 es 派生的 key 加密）。
- 时间戳 `f_timestamp`（用 ss 派生的 key 加密），用于防重放（u4-l5）。

> 噪音模式 `IK` 在此体现：发起者预先知道响应者静态公钥 `pk`，所以第一报文就能做 `DH(E_priv, S_pub_resp)`（es）和 `DH(S_priv, S_pub_resp)`（ss，已预计算为 `peer.ss`）。

#### 4.3.2 核心流程（create_initiation）

```
C = INITIAL_CK
H = Hash(INITIAL_HS || S_pub_resp)          # 混入响应者公钥
(E_priv, E_pub) = DH-Generate()             # 生成临时密钥
C = KDF1(C, E_pub)                          # 揉入临时公钥
msg.ephemeral = E_pub
H = Hash(H || msg.ephemeral)
(C, k) = KDF2(C, DH(E_priv, S_pub_resp))    # es
msg.static = AEAD(k, H, S_pub_init)         # 加密发起者静态公钥
H = Hash(H || msg.static)
(C, k) = KDF2(C, peer.ss)                   # ss（预计算）
msg.timestamp = AEAD(k, H, TAI64N)          # 加密时间戳
H = Hash(H || msg.timestamp)
保存 State::InitiationSent{ H, C, E_priv, local }   # 供 consume_response 续算
```

`consume_initiation` 在响应者侧**对称地**重放每一步：用收到的 `msg.ephemeral` 做 KDF1，用本端静态私钥 `keyst.sk` 做 es 的 DH，`OPEN` 出发起者静态公钥并查 peer，再用 `peer.ss` 做 ss，`OPEN` 出时间戳并校验。最终把 `(sender_id, 发起者临时公钥, H, C)` 打包返回，交给 `create_response`。

#### 4.3.3 源码精读

`create_initiation` 全文（注意整个函数体被包在 `clear_stack_on_return_fnone(CLEAR_PAGES, || { ... })` 里，见 4.5）：

[src/wireguard/handshake/noise.rs:230-317](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L230-L317) —— 发起者构造 Initiation。

几个关键步骤的逐行对应：

- [第 247-250 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L247-L250)：初始化 `ck = INITIAL_CK`，`hs = Hash(INITIAL_HS || pk)`（`pk` 是对端/响应者静态公钥）。
- [第 255-262 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L255-L262)：生成临时密钥对，`ck = KDF1(ck, eph_pub)`。
- [第 272-274 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L272-L274)：es —— `(ck, key) = KDF2(ck, DH(eph_sk, pk))`。
- [第 276-283 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L276-L283)：`SEAL` 把发起者静态公钥加密进 `msg.f_static`，AD 是 `hs`。
- [第 289-291 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L289-L291)：ss —— `(ck, key) = KDF2(ck, peer.ss)`，`peer.ss` 是预计算好的静态-静态 DH。
- [第 293-300 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L293-L300)：`SEAL` 加密时间戳进 `msg.f_timestamp`。
- [第 306-313 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L306-L313)：把 `(hs, ck, eph_sk, local)` 存进 `State::InitiationSent`。

`State` 枚举定义在 peer.rs，注意 `InitiationSent` 变体正好保存这四个续算量：

[src/wireguard/handshake/peer.rs:41-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L41-L49) —— `State::Reset` 与 `State::InitiationSent{ local, eph_sk, hs, ck }`。

`consume_initiation` 在响应者侧重放：

[src/wireguard/handshake/noise.rs:319-404](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L319-L404) —— 响应者消费 Initiation。

值得对比的两点：

- 发起者用 `eph_sk`（自己刚生成的临时私钥）做 es；响应者用 `keyst.sk`（本端静态私钥）做 es（[第 344 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L344)）。两边 DH 输入互补，乘积相同。
- [第 357 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L357)：`OPEN` 出发起者静态公钥后，用 `device.lookup_pk` 反查 peer。这是响应者「发现」这次握手来自哪个已知 peer 的关键时刻。
- [第 367 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L367)：把 peer 状态重置为 `State::Reset`（丢弃该 peer 上一次未完成的发起状态）。
- [第 390 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L390)：`peer.check_replay_flood` 校验时间戳（u4-l5）。
- [第 398-402 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L398-L402)：返回 `TemporaryState = (f_sender, 发起者临时公钥, hs, ck)`。`f_sender` 是发起者在 `msg.f_sender` 里填的本地 receiver id，响应者要在 response 里回填进 `f_receiver`。

`TemporaryState` 的类型定义（`(u32, PublicKey, GenericArray<u8,U32>, GenericArray<u8,U32>)`，[第 39 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L39)）正是 `(receiver_id, eph_pub, hs, ck)`，是 `consume_initiation` → `create_response` 之间的接力棒。

#### 4.3.4 代码实践

1. **目标**：把 `create_initiation` 的每一步对应到 Noise 规范符号。
2. **操作**：在本地副本（**不要改源码**）或笔记里，按下表给 `create_initiation` 的每段代码加注释。参考答案见 4.3.5。
3. **现象**：你会发现 `create_initiation` 与 `consume_initiation` 的 KDF/HASH 步骤几乎逐行对称，只有 DH 私钥来源不同。
4. **预期结果**：能画出一条「C 和 H 一路推进」的时间线，标注每一步的输入输出。

#### 4.3.5 小练习与答案

**练习**：`create_initiation` 在最后把 `eph_sk`（临时私钥）存进了 `State::InitiationSent`。这个临时私钥要等到什么时候才再次被用到？

**答案**：等到本端收到对端的 response，`consume_response` 把它取出来再做两次 DH（ee 和 se，见 4.4）。在此之前它必须留在内存里，所以 `State::Drop`（[peer.rs:51-59](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L51-L59)）负责在状态被替换时清零 `hs`/`ck`（`eph_sk` 由 dalek 自己的 Drop 清零）。

---

### 4.4 创建与消费 Response（消息 2）与密钥派生

#### 4.4.1 概念说明

第二报文 `Response` 由**响应者**用 `create_response` 生成，由**发起者**用 `consume_response` 解析。这一步要做三次 DH（ee、se，加上 psk2 的 PSK 混合），最后从链密钥拆出收发密钥，构造 `KeyPair`。

- `create_response` 接收 `consume_initiation` 传来的 `TemporaryState`，续算 C/H。
- `consume_response` 则从 `State::InitiationSent` 取出之前保存的 C/H/eph_sk 续算。

两边算出的最终 `ck` 必须相同，于是各自 `KDF2(ck, [])` 拆出的密钥也相同——只是**收发方向相反**。

#### 4.4.2 核心流程

```
# 在第一条消息结束时的 C, H 基础上继续
(E_priv, E_pub) = DH-Generate()             # 响应者临时密钥
C = KDF1(C, E_pub)
msg.ephemeral = E_pub
H = Hash(H || msg.ephemeral)
C = KDF1(C, DH(E_priv, E_pub_init))         # ee：响应者临时 × 发起者临时
C = KDF1(C, DH(E_priv, S_pub_init))         # se：响应者临时 × 发起者静态
(C, tau, k) = KDF3(C, psk)                  # psk2：混入预共享密钥
H = Hash(H || tau)
msg.empty = AEAD(k, H, [])                  # 空密文，用于确认密钥派生正确
# 派生收发密钥
(key_recv, key_send) = KDF2(C, [])           # 响应者视角
```

#### 4.4.3 源码精读

`create_response` 全文：

[src/wireguard/handshake/noise.rs:406-488](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L406-L488) —— 响应者构造 Response 并派生密钥。

关键点：

- [第 418 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L418)：解包 `state = (receiver, eph_r_pk, hs, ck)`，`eph_r_pk` 是发起者的临时公钥。
- [第 441-447 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L441-L447)：ee 与 se 两次 DH。注意 se 用的是 `pk`（发起者静态公钥，由 `consume_initiation` 经 `OPEN` 得到并通过 `device.rs::process` 传入）。
- [第 451-455 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L451-L455)：`psk2`——`KDF3` 把 PSK 揉进链密钥，`tau` 进转录，`key` 用于加密空报文。
- [第 471 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L471)：`let (key_recv, key_send) = KDF2!(&ck, &[]);` 派生收发密钥。

**密钥方向是本讲最容易绕晕的地方**，仔细看 `create_response`（响应者）与 `consume_response`（发起者）的最后几行：

- `create_response`（响应者）：`(key_recv, key_send) = KDF2(ck, [])` → `key_recv = t1`，`key_send = t2`。
- `consume_response`（发起者）：`(key_send, key_recv) = KDF2(ck, [])`（[第 551 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L551)）→ `key_send = t1`，`key_recv = t2`。

两边故意**交换了** `t1`/`t2` 的收发归属，于是：

\[ \text{发起者.send} = t_1 = \text{响应者.recv},\qquad \text{发起者.recv} = t_2 = \text{响应者.send} \]

方向正好对上，双方各自有一把发密钥、一把收密钥。

派生出的密钥连同 receiver id 组装成 `KeyPair`（[第 475-486 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L475-L486)）。`KeyPair` 定义在 `src/wireguard/types.rs`：

[src/wireguard/types.rs:31-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L31-L37) —— `KeyPair { birth, initiator, send: Key, recv: Key }`。

其中每个 `Key` 在 Drop 时清零，避免密钥材料在释放后残留在堆上：

[src/wireguard/types.rs:5-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L5-L16) —— `Key { key, id }` 与 `impl Drop for Key`（`self.key.clear()`）。

`create_response` 返回的 `KeyPair` 设了 `initiator: false`，而 `consume_response`（[第 573-585 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L573-L585)）返回 `initiator: true`。这里的 `initiator` 字段（注释写作「has the key-pair been confirmed?」）实际语义是：**只有发起者收到合法 response 后，得到的密钥才被视为「已确认」**，因为能解开 response 的空 AEAD 就证明响应者也正确派生了密钥；而响应者创建的密钥是「未确认」的，要等收到第一个传输报文才算数。这个标志在路由器的 KeyWheel 里决定密钥是直接转正还是进 `next` 槽位（见 u5-l6）。

`consume_response` 还有一个值得注意的并发设计——它在查找 peer 后**先释放锁**，做完耗时的密码学运算再**重新加锁提交**：

[src/wireguard/handshake/noise.rs:490-512](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L490-L512) —— 函数顶部的注释解释：释放锁是为了允许并发处理对潜在伪造 response 的响应，更好地缓解 DoS。

提交时再用「临时私钥是否仍匹配」判断在此期间有没有人发起新握手（[第 555-561 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L555-L561)），若已被替换则拒绝（`InvalidState`），避免重放旧 response。

#### 4.4.4 代码实践

1. **目标**：确认收发密钥方向的一致性。
2. **操作**：阅读 `create_response`（[第 471 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L471)）与 `consume_response`（[第 551 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L551)）两处 `KDF2`，写下两边 `send.key` / `recv.key` 分别等于 `t1` 还是 `t2`。
3. **预期结果**：
   - 发起者（consume）：`send = t1`，`recv = t2`。
   - 响应者（create）：`send = t2`，`recv = t1`。
   - 因此发起者 `send` 等于响应者 `recv`（都是 `t1`），方向匹配。
4. 进一步观察 `send.id` / `recv.id`：发起者的 `send.id = remote`（响应者的 sender id），`recv.id = local`（自己的 receiver id）。这与 u4-l2 讲的 receiver id 在线路上的角色一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `msg.empty`（`f_empty`）加密的是空明文？它有什么用？

**答案**：它不是为了传数据，而是用最终派生的 `key` 做一次 AEAD，把整个转录 `H` 绑定进认证标签。发起者 `consume_response` 时用 `OPEN` 解密它——**只要 OPEN 成功，就证明响应者持有的所有 DH 私钥和 PSK 都正确**，从而确认双方派生出了相同的收发密钥。这是 Noise 的「transport confirmation」。

**练习 2**：`psk2` 表示 PSK 在第二条消息后混入。若两个 peer 没配 PSK（`psk = [0u8;32]`），握手还能正常工作吗？

**答案**：能。PSK 全零时，`KDF3(C, [0u8;32])` 仍然会推进链密钥（只是揉进的是「已知常量」），后续密钥派生照常进行。PSK 全零等价于「没有额外的对称口令加固」，但 IK 握手本身的安全性不依赖 PSK。

---

### 4.5 栈清零：`clear_stack_on_return_fnone`

#### 4.5.1 概念说明

四个握手函数的函数体都被包在一层 `clear_stack_on_return_fnone(CLEAR_PAGES, || { ... })` 里。这个来自 `clear_on_drop` crate 的工具做一件事：**在闭包返回后，把它用过的栈内存清零**。

#### 4.5.2 为什么需要它

握手函数的栈上短暂存在大量敏感中间量：

- 临时私钥 `eph_sk`。
- 每次 DH 的共享密钥（`shared_secret` 的返回值）。
- KDF 派生出的 AEAD 密钥 `key`。
- 中间链密钥 `ck`、转录 `hs`、prk（`t0`）。

Rust 默认不会擦除函数返回后留下的栈帧。这些字节会一直留在栈上，直到被后续函数调用覆盖。在此期间，任何能读到进程内存的手段——core dump、swap 换页、内存泄漏漏洞——都可能让攻击者拿到这些密钥材料，进而解密流量。

`clear_stack_on_return_fnone(CLEAR_PAGES=1, closure)` 在闭包返回时把大约 1 页（通常 4 KiB）栈区域写零，正好覆盖这些函数的栈用量，让密钥「用过即焚」。

> 「fnone」指该工具针对 `FnOnce` 闭包的变体；它先执行闭包、再清零其栈帧。它与 `Key::Drop`（清堆上密钥）、`State::Drop`（清保留下来的 hs/ck）分工互补：Drop 管「活到函数之外」的密钥，`clear_stack_on_return_fnone` 管「只在本次调用里出现」的栈上临时量。

#### 4.5.3 源码精读

常量定义与四处调用：

[src/wireguard/handshake/noise.rs:44-45](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L44-L45) —— `CLEAR_PAGES = 1`。

四个函数体都被包裹（以 `create_initiation` 为例）：

[src/wireguard/handshake/noise.rs:245-316](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L245-L316) —— 整个密码学计算在 `clear_stack_on_return_fnone` 闭包内完成，闭包返回前栈被清零。

另外三处：[consume_initiation 第 326 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L326)、[create_response 第 415 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L415)、[consume_response 第 500 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L500)。

## 5. 综合实践：把 `create_initiation` 翻译成 Noise 规范

本实践把本讲所有最小模块串起来，完成规格里要求的两件事：（a）把 `create_initiation` 每一步对应到 Noise 符号；（b）解释 `clear_stack_on_return_fnone` 的必要性。

### 步骤 1：跑通基线测试

```bash
cargo test --lib handshake::noise::tests
```

确认 `precomputed_chain_key`、`precomputed_hash`、`hkdf` 通过。这是后续注释「站得住脚」的前提——常量与 KDF 实现已被测试向量钉死。（若环境无法编译，标注「待本地验证」。）

### 步骤 2：在笔记里给 `create_initiation` 加 Noise 符号注释

对照 [src/wireguard/handshake/noise.rs:230-317](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L230-L317)，按下表逐行标注（**参考答案**）：

| 代码位置（行） | Noise 规范符号 | 中文说明 |
|---|---|---|
| 248 | `C` ← `Hash(Construction)` | 链密钥初始化（预计算常量 `INITIAL_CK`） |
| 249-250 | `H` ← `Hash(H ‖ S_pub_resp)` | 把响应者静态公钥 `pk` 混入转录（`H` 初值为 `INITIAL_HS`） |
| 255-258 | `(e_priv, e_pub) ← DH-Generate()` | 生成发起者临时密钥对 `E` |
| 260-262 | `C ← KDF1(C, e_pub)` | 临时公钥揉进链密钥 |
| 264-266 | `msg.ephemeral ← e_pub` | 明文写入临时公钥 |
| 268-270 | `H ← Hash(H ‖ msg.ephemeral)` | 临时公钥录入转录 |
| 272-274 | `(C, k) ← KDF2(C, DH(e_priv, S_pub_resp))` | **es**：临时私钥 × 响应者静态公钥 |
| 276-283 | `msg.static ← AEAD(k, 0, S_pub_init, H)` | 用 `k` 加密发起者静态公钥，AD 为 `H` |
| 285-287 | `H ← Hash(H ‖ msg.static)` | 密文录入转录 |
| 289-291 | `(C, k) ← KDF2(C, DH(S_priv, S_pub_resp))` | **ss**：静态-静态 DH（预计算为 `peer.ss`） |
| 293-300 | `msg.timestamp ← AEAD(k, 0, TAI64N, H)` | 用新 `k` 加密时间戳 |
| 302-304 | `H ← Hash(H ‖ msg.timestamp)` | 时间戳密文录入转录 |
| 306-313 | 保存 `(H, C, e_priv, local)` | 存进 `State::InitiationSent`，待 `consume_response` 续算 |

标注完成后，你应该能看到一条清晰的时间线：**每次 DH 都推进 C，每段发出/收到的字节都推进 H**，而 AEAD 始终用「当前 H」作 AD——这就是 Noise 的「转录绑定」。

### 步骤 3：解释 `clear_stack_on_return_fnone`

请用一段话回答：上表中第 255-291 行在栈上产生了哪些敏感量？如果函数返回后不清零，会怎样？

**参考答案**：栈上至少出现 `eph_sk`（临时私钥）、`shared_secret` 的返回值（es 的 DH 共享密钥）、两次 `KDF2` 派生的 `key`（AEAD 密钥）、以及中间的 `ck`/`hs`/prk。这些值若残留在栈上，可能被 core dump、swap 或内存泄漏读取，导致会话密钥泄露、流量被解密。`clear_stack_on_return_fnone(1, …)` 在闭包返回时清零约 1 页栈，把这些临时量擦除。它与 `Key::Drop`（清最终密钥）和 `State::Drop`（清保留的 `ck`/`hs`）一起构成 wireguard-rs 的密钥卫生三层防线。

## 6. 本讲小结

- WireGuard 握手即 `Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s`，全程由两个状态量驱动：链密钥 C 与转录 H，每步 DH 推进 C、每段数据推进 H。
- `INITIAL_CK`/`INITIAL_HS` 是协议名与标识符的预计算哈希常量；`HASH/HMAC/KDF1-3/SEAL/OPEN` 六个宏把 Noise 公式原样落地，HKDF 测试向量钉死了正确性。
- `shared_secret` 用常时间比较 `ct_eq` 拒绝零共享密钥，既抗小子群攻击也与内核行为对齐；`peer.ss` 的零值检查顺带兜底「未配置私钥」。
- `create_initiation` 与 `consume_initiation` 对称地完成第一条消息：es（临时×静态）+ ss（静态×静态，预计算），并加密静态公钥和时间戳；中间状态经 `State::InitiationSent` / `TemporaryState` 传递。
- `create_response` 与 `consume_response` 完成 ee + se + psk2，最后 `KDF2(ck, [])` 拆出收发密钥；两边**故意交换 t1/t2 归属**使收发方向匹配，组装成 Drop 清零的 `KeyPair`。
- `initiator` 标志区分「已确认」（发起者，收到合法 response 即确认）与「未确认」（响应者，待首个传输报文确认）密钥，驱动后续 KeyWheel。
- 四个函数体都被 `clear_stack_on_return_fnone(1, …)` 包裹，连同 `Key::Drop`、`State::Drop` 构成密钥材料三层清零。

## 7. 下一步学习建议

- 顺「握手调度」回看 u4-l2 的 `Device::begin/process`，体会它们如何调用本讲的四个函数、并把返回的 `KeyPair`/`Output` 上交。
- 学 **u4-l4**（MAC/Cookie 与速率限制）：本讲产生的裸报文在外层还要包上 `MacsFooter`（mac1/mac2）并过 `RateLimiter`，才能上线。
- 学 **u4-l5**（时间戳与重放）：本讲 `create_initiation` 加密的时间戳如何被 `check_replay_flood` 校验。
- 学 **u5-l2/u5-l3/u5-l6**：本讲派生的 `KeyPair` 进入路由器后，如何被 KeyWheel（next/current/previous）管理与轮转，并用于 ChaCha20-Poly1305 传输报文的加解密。
