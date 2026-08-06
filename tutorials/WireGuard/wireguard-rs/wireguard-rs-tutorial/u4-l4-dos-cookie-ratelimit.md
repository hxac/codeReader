# 抗拒绝服务：MAC/Cookie 与速率限制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 WireGuard 握手为什么是拒绝服务（DoS）的理想攻击面，以及它用什么总体思路来防御。
- 区分 `mac1`（基于公钥的第一层廉价认证）和 `mac2`（基于 cookie 的第二层源地址认证）各自的作用与触发时机。
- 复述 CookieReply 在「under-load（过载）」时如何让对端拿到 cookie、再回填 `mac2` 的完整往返。
- 读懂 `macs.rs` 中 `Validator`（接收侧）与 `Generator`（发送侧）的对称设计，以及 `ratelimiter.rs` 中按源 IP 的令牌桶与 GC 线程。
- 解释 `THRESHOLD_UNDER_LOAD`、`DURATION_UNDER_LOAD` 等常量如何驱动整套机制的开与关。

本讲承接 u4-l2（握手 `Device` 与 peer 管理）。u4-l2 讲了 `Device::process` 如何按消息类型分用，本讲则专门拆开 `process` 里 `check_mac1` / `check_mac2` / `create_cookie_reply` / `limiter.allow` 这一段「地址校验与 DoS 缓解」逻辑。

## 2. 前置知识

- **握手报文很贵，传输报文很便宜**：处理一条 Initiation/Response 要做 Curve25519 标量乘（DH）等公钥运算（见 u4-l3），而传输报文只做对称 AEAD（ChaCha20-Poly1305）。攻击者只要往一个 UDP 端口狂发「看起来像 Initiation」的随机字节，就能逼服务端做海量昂贵运算。
- **UDP 无连接，无法像 TCP 那样握手**：服务端在收到第一条报文时，还不知道对端是谁、地址是否可回达，因此不能直接做「三次握手」式的防伪造。
- **MAC（消息认证码）**：用一把密钥对一段数据算出一个固定长度的标签，持有同一密钥的人能验证标签、攻击者伪造不出。本讲用的是 Blake2s 的 128 位 MAC（`macs.rs` 内部）和 XChaCha20-Poly1305 AEAD（加密 cookie）。
- **关键术语回顾**：`receiver id`（u4-l2）、握手报文的线路格式与 `MacsFooter`（u4-l1）、`udp_worker` 的消息分用与 `wg.pending` 原子计数（u3-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/wireguard/handshake/macs.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs) | 本讲主战场：`Validator`（接收侧校验/发 cookie）、`Generator`（发送侧算 mac1/mac2、收 cookie），以及 `MAC!`/`XSEAL!`/`XOPEN!` 原语宏。 |
| [src/wireguard/handshake/ratelimiter.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/ratelimiter.rs) | 按源 IP 的令牌桶 `RateLimiter`，含懒启动的 GC 线程。 |
| [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs) | `Device::process` 中调用 mac1/mac2/cookie/ratelimiter 的整合点。 |
| [src/wireguard/constants.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs) | `MAX_QUEUED_INCOMING_HANDSHAKES`、`THRESHOLD_UNDER_LOAD`、`DURATION_UNDER_LOAD` 等 under-load 阈值常量。 |
| [src/wireguard/workers.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs) | `handshake_worker` 中判定 under-load、决定是否把源地址传给 `process`。 |
| [src/wireguard/handshake/messages.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs) | `MacsFooter`（mac1/mac2 脚注）与 `CookieReply` 报文布局。 |

## 4. 核心概念与源码讲解

### 4.1 为什么握手需要 DoS 防御：两层 MAC 模型总览

#### 4.1.1 概念说明

WireGuard 把「确认对端身份」和「付出昂贵代价」分离：真正的身份认证发生在 Noise 握手里（u4-l3，要做 DH），但服务端在动用公钥运算之前，先用两层**廉价**的 MAC 给报文做快速过滤：

- **`mac1`（第一层，始终启用）**：用「从响应方公钥派生的密钥」对内层报文算的 MAC。它能证明**发送方知道响应方的公钥**——把攻击门槛从「往随机端口喷随机 UDP」抬高到「至少知道目标公钥」。它是 Blake2s MAC，验证极其便宜。
- **`mac2`（第二层，仅过载时启用）**：用一个 **cookie** 对（内层报文 + mac1）算的 MAC。cookie 只发给「服务端能回达的源地址」，因此有效的 mac2 能证明**发送方能收到发往该源地址的报文**（源地址未被伪造）。无 cookie 时 mac2 填全零并被忽略。

关键直觉：`mac1` 不做密码学身份认证（公钥本就是公开的），它只是个**廉价的垃圾过滤器**；`mac2` 才是通过 cookie 证明「源地址可回达」的反伪造手段。服务端轻松时只查 mac1；被压垮时再加查 mac2，查不过就回一条 CookieReply 而不做昂贵运算。

#### 4.1.2 核心流程

正常（未过载）路径：

1. 发送方算 mac1，mac2 置零，发出 Initiation。
2. 响应方校验 mac1 通过 → mac2 是零、忽略 → 直接做 Noise 握手。

过载路径（响应方 under-load）：

1. 发送方算 mac1，mac2 置零，发出 Initiation。
2. 响应方校验 mac1 通过 → 校验 mac2 失败（为零）→ **回 CookieReply，提前返回，不做 DH**。
3. 发送方收到 CookieReply，解出 cookie 存下。
4. 发送方重发 Initiation，这次 mac2 = MAC(cookie, 内层, mac1)。
5. 响应方校验 mac1、mac2 都过 → 再过一道按 IP 的令牌桶 → 才做 Noise 握手。

#### 4.1.3 源码精读

`mac1`/`mac2` 两个字段就住在每条 Initiation/Response 的 `MacsFooter` 脚注里，各 16 字节，紧贴在 Noise 内层报文之后（详见 u4-l1）：

- [messages.rs:63-68](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L63-L68)：`MacsFooter { f_mac1, f_mac2 }`，这就是本讲所有运算的落脚点。

而 `macs.rs` 顶部的两个标签常量是理解全篇的钥匙——所有派生密钥都用固定 ASCII 标签 + 公钥做 Blake2s 哈希得到：

- [macs.rs:22-30](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L22-L30)：`LABEL_MAC1 = b"mac1----"`、`LABEL_COOKIE = b"cookie--"`，以及尺寸常量 `SIZE_COOKIE=16`、`SIZE_MAC=16`、`SIZE_TAG=16`、`COOKIE_UPDATE_INTERVAL=120s`。标签补齐到 8 字节是 Noise 协议族的惯例。

#### 4.1.4 代码实践

**目标**：在阅读源码前，先用一句话写出 mac1 与 mac2 各自「证明什么」，避免混淆。

**步骤**：

1. 打开本讲，遮住答案，写下「mac1 证明 ___；mac2 证明 ___」。
2. 对照 4.1.1 自查：mac1 → 发送方知道响应方公钥；mac2 → 源地址可回达（cookie 来自该地址）。

**预期结果**：能区分「知道目标是谁」与「我真的是这个源地址」是两件不同的事。

#### 4.1.5 小练习与答案

- **练习**：既然公钥是公开的，任何人都能算 mac1，那 mac1 还有什么防御价值？
- **答案**：它过滤掉的是「盲扫」流量——不知道目标公钥的随机 UDP 喷射连 mac1 都凑不出；同时它把更贵的 DH 运算挡在一层极廉价的 Blake2s 校验之后。

---

### 4.2 mac1：基于公钥的第一层廉价认证

#### 4.2.1 概念说明

`mac1` 的密钥由 `mac1_key = Blake2s("mac1----" || PK)` 派生。这里有个精妙的对称设计：

- **发送方（`Generator`）**：用**对端公钥**派生 mac1_key（它要发给对端，让对端能验）。
- **接收方（`Validator`）**：用**自己的设备公钥**派生 mac1_key。

由于「对端公钥」（发送方视角）就是「响应方自己的公钥」（接收方视角），两边派生出**同一把密钥**，于是发送方算的 mac1 能被接收方验证。`Generator` 是 per-peer 的（存在 `Peer.macs`），`Validator` 是 per-device 的（存在 `KeyState.macs`）。

#### 4.2.2 核心流程

发送侧 `Generator::generate`：

```
mac1_key = H(LABEL_MAC1 || 对端PK)
f_mac1   = Blake2s128(mac1_key, 内层报文)
```

接收侧 `Validator::check_mac1`：

```
重算 expected = Blake2s128(mac1_key, 内层报文)
用常时间比较 ct_eq(expected, f_mac1)  // 防计时侧信道
相等 → Ok(())；不等 → Err(InvalidMac1)
```

#### 4.2.3 源码精读

派生密钥与构造（发送侧）——`Generator::new` 用对端公钥算出 mac1_key 与 cookie_key：

- [macs.rs:122-129](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L122-L129)：`mac1_key: HASH!(LABEL_MAC1, pk.as_bytes())`。

生成 mac1（发送侧）——`Generator::generate` 第一行就用 `MAC!` 宏算 f_mac1，并顺手把这次 mac1 存为 `last_mac1`（4.4 节会用到）：

- [macs.rs:165-179](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L165-L179)：`macs.f_mac1 = MAC!(&self.mac1_key, inner);` 以及 `self.last_mac1 = Some(macs.f_mac1);`。

校验 mac1（接收侧）——`Validator::check_mac1` 用 `subtle::ConstantTimeEq` 做常时间比较，避免「比较提前返回」泄露 MAC 前缀：

- [macs.rs:264-271](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L264-L271)：`MAC!(&self.mac1_key, inner).ct_eq(&macs.f_mac1).into()`，失败返回 `HandshakeError::InvalidMac1`。

`MAC!` 宏本身是基于带密钥的 Blake2s（`VarBlake2s::new_keyed`），输出截断为 16 字节：

- [macs.rs:43-55](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L43-L55)：可见 MAC 即 keyed-Blake2s-128。

`Validator` 自身存在 `Device` 的 `KeyState` 里，在 `set_sk` 设私钥时用**设备公钥**初始化：

- [device.rs:29-33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L29-L33)：`KeyState { sk, pk, macs: macs::Validator }`。
- [device.rs:134-140](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L134-L140)：`let macs = macs::Validator::new(pk);`（pk 为设备公钥）。

`Generator` 则 per-peer，存在握手 `Peer` 里：

- [peer.rs:34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L34)：`pub macs: Mutex<macs::Generator>`（用 `spin::Mutex` 保护可变状态）。

#### 4.2.4 代码实践

**目标**：确认「发送侧用对端 PK、接收侧用本机 PK」能得到同一把 mac1_key。

**步骤**：

1. 阅读 [macs.rs:122-129](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L122-L129) 与 [macs.rs:194-203](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L194-L203)。
2. 在笔记里画一张表：`Generator::new(对端PK)` vs `Validator::new(本机PK)`，标注当「对端 = 本机响应方」时两者输入相同。

**预期结果**：理解 mac1 之所以能跨网络验证，根因是双方用了**同一公钥**派生同一密钥。

#### 4.2.5 小练习与答案

- **练习**：为什么 `check_mac1` 用 `ct_eq`（常时间比较）而不是普通的 `==`？
- **答案**：普通比较在首个不一致字节处提前返回，会让攻击者通过测量响应时间逐字节猜出合法 MAC（计时侧信道）。`ct_eq` 总是遍历全部字节、耗时与输入无关。
- **练习**：`mac1_key` 用的是哪段输入的哈希？
- **答案**：`Blake2s("mac1----" || 公钥)`，其中公钥对发送方是对端公钥、对接收方是本机设备公钥。

---

### 4.3 mac2 与 Cookie：under-load 时的源地址验证

#### 4.3.1 概念说明

`mac2` 解决的是「源地址是否可回达」。响应方维护一个**设备级随机秘密** `secret`（32 字节，每 120 秒轮换一次）。对某个源地址 `src`，cookie 定义为：

\[
\text{cookie} = \text{Blake2s128}(\text{secret},\ \text{src\_bytes})
\]

注意 cookie 是**按源地址**算的（同一个 secret 对不同源地址给出不同 cookie），但 secret 是**全设备共享**的——所以响应方无需为每个源地址存 cookie，只要记住当前 secret，就能为任意源地址现算。发送方则把 cookie 当作一次性秘密，计算：

\[
\text{mac2} = \text{Blake2s128}(\text{cookie},\ \text{内层报文},\ \text{mac1})
\]

响应方校验时：先用 `src` 和自己的 secret 现算 cookie，再算 mac2 与报文里的 f_mac2 比较。**发送方没有 cookie 时，mac2 填全零**；响应方在非过载时直接忽略 mac2。

cookie 机制本质是「让攻击者先证明他能收到发往该源地址的报文」——因为 cookie 是通过 CookieReply 发回源地址的，伪造源地址的攻击者收不到 CookieReply，也就算不出 mac2。

#### 4.3.2 核心流程

接收侧 `Validator`（双检锁管理 secret）：

```
get_tau(src):                          // 只读路径（用于 check_mac2）
  若 secret 未过期(≤120s): 返回 MAC(secret, src)
  否则: 返回 None（视为 mac2 无效）

get_set_tau(src):                      // 写路径（用于 create_cookie_reply）
  读锁查 secret 是否过期；未过期直接返回 MAC(secret, src)
  过期则取写锁、再查一次（防并发重复轮换）
  用 rng 重置 secret，返回 MAC(secret, src)
```

`check_mac2(inner, src, macs)`：调 `get_tau(src)`，若 None 返回 false；否则比较 `MAC(tau, inner, mac1)` 与 `f_mac2`。

#### 4.3.3 源码精读

`Validator` 的字段与「过期初始值」技巧——构造时把 `birth` 设为 `now - 86400s`，使首个 secret 一出生就「已过期」，从而第一次 `create_cookie_reply` 必然触发轮换：

- [macs.rs:187-203](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L187-L203)：`Validator { mac1_key, cookie_key, secret: RwLock<Secret> }`，`birth: Instant::now() - Duration::new(86400, 0)`。

只读的 `get_tau`（check_mac2 用）——过期就返回 None：

- [macs.rs:205-212](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L205-L212)：`if secret.birth.elapsed() < COOKIE_UPDATE_INTERVAL { Some(MAC!(...)) } else { None }`。

双检锁的 `get_set_tau`（create_cookie_reply 用）——经典的「读锁探路 → 写锁复查 → 真正轮换」，避免每个并发请求都重新生成 secret：

- [macs.rs:214-235](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L214-L235)：`rng.fill_bytes(&mut secret.value); secret.birth = Instant::now();`。

`check_mac2`——把源地址 `SocketAddr` 折成字节、算 tau、再常时间比较：

- [macs.rs:273-279](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L273-L279)：`match self.get_tau(&src) { Some(tau) => MAC!(...).ct_eq(...).into(), None => false }`。

源地址到字节的折算——把 IP octets + 端口（小端）拼成字节串，作为 cookie 的输入，使 cookie 与「IP+端口」绑定：

- [macs.rs:95-110](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L95-L110)：`addr_to_mac_bytes` 分别处理 v4/v6。

#### 4.3.4 代码实践

**目标**：理解「无 cookie → mac2 全零 → check_mac2 返回 false」这一步如何触发后续 CookieReply。

**步骤**：

1. 阅读 `Generator::generate` 中 `mac2` 的 `None` 分支：[macs.rs:165-179](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L165-L179)，确认无 cookie 时 `macs.f_mac2 = [0u8; SIZE_MAC]`。
2. 阅读接收侧 `check_mac2`：当 secret 有效时，`MAC!(tau, inner, mac1)` 与全零比较必为 false。

**预期结果**：能说清「mac2 全零」是「我还没有 cookie」的合法表达，而不是错误。

#### 4.3.5 小练习与答案

- **练习**：cookie 是按 peer 存的还是按设备存的？
- **答案**：按设备存一个共享 secret，cookie 由 `MAC(secret, src)` 现算；发送侧则 per-peer 缓存最近一次收到的 cookie。响应方无需维护「每个源地址的 cookie」表。
- **练习**：为什么构造 `Validator` 时把 `birth` 倒拨 86400 秒？
- **答案**：让初始 secret 立即处于「已过期」状态，保证第一次需要发 cookie 时一定走 `get_set_tau` 的轮换分支、用上新鲜随机 secret，而不是停留在全零的初始 value。

---

### 4.4 CookieReply 的加密封装与 Generator/Validator 协作

#### 4.4.1 概念说明

cookie 不能明文回传——否则任何监听者都能拿到 cookie 去伪装源地址。CookieReply 用 **XChaCha20-Poly1305 AEAD** 把 cookie 加密发回，密钥同样由公钥派生：`cookie_key = H("cookie--" || PK)`，与 4.2 的 mac1_key 派生方式同构。附加认证数据（AD）用的是**触发这条回复的报文的 mac1**——只有持有合法 mac1（即知道响应方公钥）的一方才能解密。

于是 CookieReply 字段为：`f_type | f_receiver | f_nonce(24B) | f_cookie(16B cookie + 16B tag)`（u4-l1）。`f_receiver` 让发送方按 receiver id 找到对应 peer（即对应 `Generator`）。

#### 4.4.2 核心流程

响应方 `Validator::create_cookie_reply`：

```
nonce = 随机 24 字节
cookie = get_set_tau(src)                      // 现算/轮换后的 cookie
f_cookie = XChaCha20Poly1305.Seal(
    key   = cookie_key,                        // H("cookie--" || 本机PK)
    nonce = nonce,
    ad    = 报文的 mac1,
    pt    = cookie
)
填入 f_type=TYPE_COOKIE_REPLY, f_receiver, f_nonce, f_cookie
```

发送方 `Generator::process(CookieReply)`：

```
要求 self.last_mac1 存在（即本方刚发过触发回复的报文）
cookie = XChaCha20Poly1305.Open(
    key   = cookie_key,                        // H("cookie--" || 对端PK)
    nonce = reply.f_nonce,
    ad    = last_mac1,                         // 与响应方 Seal 时的 mac1 一致
    ct    = reply.f_cookie
)
存下 cookie（带时间戳 birth，120s 后失效）
```

发送侧下次 `generate` 时：若 cookie 未过期，`mac2 = MAC(cookie, inner, mac1)`；若已过期则清空 cookie、mac2 回到全零。

#### 4.4.3 源码精读

`create_cookie_reply`——把源地址折字节、随机 nonce、用 mac1 作 AD 封装 cookie：

- [macs.rs:237-256](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L237-L256)：`XSEAL!(&self.cookie_key, &msg.f_nonce, &macs.f_mac1, &self.get_set_tau(rng, &src), &mut msg.f_cookie)`。

`Generator::process`——用 `last_mac1` 作 AD 解密，失败（过期/畸形）返回 `HandshakeError`：

- [macs.rs:141-157](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L141-L157)：`XOPEN!(&self.cookie_key, &reply.f_nonce, &mac1, &mut tau, &reply.f_cookie)?`，成功后存 `Cookie { birth, value: tau }`。

`generate` 中 cookie 过期清理与 mac2 计算：

- [macs.rs:165-179](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L165-L179)：`if cookie.birth.elapsed() > COOKIE_UPDATE_INTERVAL { self.cookie = None; [0u8;SIZE_MAC] } else { MAC!(&cookie.value, inner, macs.f_mac1) }`。

`XSEAL!`/`XOPEN!` 宏——薄封装 XChaCha20-Poly1305，断言密文长度 = 明文 + 16 字节 tag：

- [macs.rs:57-81](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L57-L81)：`XOPEN` 解密失败统一映射为 `HandshakeError::DecryptionFailure`。

`CookieReply` 报文布局（u4-l1 已讲字段，这里聚焦与本讲相关的 `f_nonce` 与 `f_cookie`）：

- [messages.rs:52-59](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L52-L59)：`f_nonce: [u8; 24]`、`f_cookie: [u8; 16+16]`。

`device.rs` 里接收侧处理 CookieReply——按 `f_receiver` 查 peer，调 `Generator::process`，注意它**不做密码学身份认证**，只是更新 cookie：

- [device.rs:416-428](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L416-L428)：`peer.macs.lock().process(&msg)?; Ok((None, None, None))`，注释明确「DOES NOT cryptographically verify the peer」。

#### 4.4.4 代码实践

**目标**：复用 `macs.rs` 测试里的 `new_validator_generator`，跑通 mac1 → cookie reply → mac2 的完整往返，并断言「无 cookie 时 mac2 全零」。这是本讲的主实践，对应规格中的 practice_task。

**步骤**（这是「源码阅读 + 复用现有测试夹具」型实践，不修改源码）：

1. 打开 [macs.rs:289-325](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L289-L325)，阅读 `new_validator_generator`（用同一公钥同时造 `Validator` 和 `Generator`，模拟收发双方共享公钥）与 `test_cookie_reply`。
2. 对照 `test_cookie_reply` 写出四步：
   - `generator.generate(inner1, &mut macs)`：断言 `macs.f_mac1 != 0`、`macs.f_mac2 == [0;16]`（无 cookie）。
   - `validator.check_mac1(inner1, &macs)` 通过；`validator.check_mac2(inner1, &src, &macs)` 此时返回 **false**（mac2 全零）。
   - `validator.create_cookie_reply(...)` 造出 CookieReply；`generator.process(&msg)` 解出 cookie。
   - 再 `generator.generate(inner2, &mut macs)`：此时 `macs.f_mac2 != 0`；`validator.check_mac2(inner2, &src, &macs)` 返回 **true**。
3. 想运行可执行 `cargo test --release test_cookie_reply`（该测试由 `proptest!` 包裹，会随机生成 `inner1/inner2/receiver` 多轮）。

**需要观察的现象**：第一次 `generate` 后 mac2 为全零；`process` 完 CookieReply 后第二次 `generate` 的 mac2 非零且能被 `check_mac2` 验过。

**预期结果**：测试通过；并能解释 mac2 从全零变为非零的转折点正是 `Generator::process` 成功解出 cookie。

> 本地运行结果：待本地验证（`cargo test` 是否通过取决于本机依赖与 nightly/stable 环境，参见 u1-l2）。

#### 4.4.5 小练习与答案

- **练习**：为什么 CookieReply 用 mac1 作 AD，而不用整条报文？
- **答案**：mac1 已绑定「内层报文 + 响应方公钥」，足以作为该次交互的上下文指纹；只有算得出 mac1（即知道响应方公钥）的合法对端才能解密 cookie，把窃听者挡在外面。
- **练习**：`Generator::process` 为什么要求 `self.last_mac1` 必须存在？
- **答案**：解密 cookie 需要和加密时相同的 AD（即触发回复那条报文的 mac1）；`generate` 每次都把当次 mac1 存进 `last_mac1`，`process` 据此重现 AD。若没有发过报文就收到 CookieReply，说明状态不一致，返回 `InvalidState`。

---

### 4.5 令牌桶速率限制 RateLimiter

#### 4.5.1 概念说明

即便 mac1、mac2 都通过，响应方还会再过一道**按源 IP 的令牌桶**，给每个 IP 的握手处理速率封顶。令牌桶用一个「以纳秒为单位的余额」表达：时间流逝按墙钟自动「充值」（每过 1 纳秒余额 +1，直到桶满），每处理一个报文扣固定「成本」。这样长期平均速率被限制，同时允许小幅突发。

四个常量（均在 [ratelimiter.rs:8-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/ratelimiter.rs#L8-L13)）：

- `PACKETS_PER_SECOND = 20`：稳态每秒允许的报文数。
- `PACKETS_BURSTABLE = 5`：突发量（桶大小以「报文数」计）。
- `PACKET_COST = 1e9 / PACKETS_PER_SECOND = 50_000_000`（ns）：单个报文成本。
- `MAX_TOKENS = PACKET_COST * PACKETS_BURSTABLE = 250_000_000`（ns）：桶容量。

#### 4.5.2 核心流程

每个 `Entry { last_time, tokens }` 的更新规则：

\[
\text{tokens} \leftarrow \min(\text{MAX\_TOKENS},\ \text{tokens} + \Delta_{\text{ns}}),\quad \Delta_{\text{ns}} = \text{距上次的纳秒数}
\]

\[
\text{放行} \iff \text{tokens} > \text{PACKET\_COST},\ \text{放行后}\ \text{tokens} \mathrel{-}= \text{PACKET\_COST}
\]

新 IP 首次出现时直接插入一条 `tokens = MAX_TOKENS - PACKET_COST` 的记录并放行（即第一个报文立即通过、且桶近乎满）。长期来看，每个 IP 的握手速率被锁在 ≤ `PACKETS_PER_SECOND`。

为避免表无限增长，有一条**懒启动的 GC 线程**：第一个调用 `allow` 的线程负责 spawn 它，它每约 1 秒 (`GC_INTERVAL`) 扫一遍表，删除 `last_time.elapsed() > 1s` 的条目；表空了就自行退出并把 `gc_running` 置 false，下次有新 IP 时再重启。`RateLimiter` 被 Drop 时通过 condvar 唤醒 GC 线程优雅退出。

#### 4.5.3 源码精读

`allow` 主逻辑——命中已有条目走只读 + 内层互斥锁更新；未命中走写锁插入新条目：

- [ratelimiter.rs:48-79](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/ratelimiter.rs#L48-L79)：余额更新 `entry.tokens = MAX_TOKENS.min(entry.tokens + u64::from(entry.last_time.elapsed().subsec_nanos()));`，注意它取 `subsec_nanos()`（不足 1 秒的部分，ns），扣费判断 `if entry.tokens > PACKET_COST { … return true } else { return false }`。

新条目插入——预扣一个成本，使首包直接放行：

- [ratelimiter.rs:71-78](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/ratelimiter.rs#L71-L78)：`tokens: MAX_TOKENS - PACKET_COST`，函数返回 `true`。

懒启动 GC 线程——用 `gc_running` 的 `swap(true)` 保证只起一次：

- [ratelimiter.rs:82-105](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/ratelimiter.rs#L82-L105)：`tw.retain(|_, entry| entry.lock().last_time.elapsed() <= GC_INTERVAL)`；表空则 `gc_running.store(false)` 并 return；否则 `cvar.wait_timeout(GC_INTERVAL)` 周期性回收。

Drop 与并发结构——表是 `spin::RwLock<HashMap<IpAddr, spin::Mutex<Entry>>>`，外层读写锁、内层逐条目互斥：

- [ratelimiter.rs:15-36](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/ratelimiter.rs#L15-L36)：`Entry`、`RateLimiterInner { gc_running, gc_dropped, table }`，`Drop` 置 `dropped=true` 并 `notify_all`。

Device 持有限速器（全设备共享一个）：

- [device.rs:38-43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L38-L43)：`limiter: Mutex<RateLimiter>`。
- [device.rs:97-104](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L97-L104)：`limiter: Mutex::new(RateLimiter::new())`。

#### 4.5.4 代码实践

**目标**：通过自带的 `test_ratelimiter` 观察令牌桶的「突发 → 拒绝 → 充值后再放行」节奏。

**步骤**：

1. 阅读 [ratelimiter.rs:122-198](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/ratelimiter.rs#L122-L198)：它对 14 个 IP 按一个预期序列（初始突发全放行 → 突破后拒绝 → 睡 `PACKET_COST` 纳秒后放行一个 → 再睡 `2*PACKET_COST` 放行两个）逐一断言。
2. 运行 `cargo test --release test_ratelimiter`。

**需要观察的现象**：刚注入的新 IP 连续多次 `allow` 返回 true（突发），随后返回 false；等待对应纳秒后又能放行。

**预期结果**：测试通过；稳态速率 ≈ 20 包/秒/IP。

> 本地运行结果：待本地验证。注意初始突发能放行的确切次数取决于「插入时预扣一个成本 + 严格大于 `>` 判断」与各次调用间的实际墙钟纳秒增量，建议在日志里打印每次 `allow` 前后的 `tokens` 实测。

#### 4.5.5 小练习与答案

- **练习**：令牌桶为什么用 `subsec_nanos()`（不足 1 秒的纳秒）来充值？
- **答案**：因为单次报文间隔通常远小于 1 秒（握手突发在毫秒级），用秒级精度会丢光增量；取纳秒级 `subsec_nanos` 才能精确累加微秒/毫秒级的间隔。
- **练习**：GC 线程为什么是「懒启动 + 表空即退」而不是常驻？
- **答案**：设备大部分时间并不在收握手，常驻 GC 线程是纯浪费；有新 IP 进来才需要回收，表空后立刻退出，下次再按需重启，用 `gc_running` 原子标志保证唯一性。

---

### 4.6 under-load 触发与 device.process 的整合

#### 4.6.1 概念说明

前面三层防御（mac1、mac2+cookie、ratelimiter）的开关并不是随时全开——那样会拖慢正常握手。它们由一个「设备是否 under-load」的判定统一调度：

- **非过载**：只查 mac1，**不查 mac2、不限速**，直接做昂贵的 Noise 握手（用户体验优先）。
- **过载**：先查 mac1，再查 mac2；mac2 不过就回 CookieReply 提前返回（不做 DH）；mac2 过了再过令牌桶；都过才做握手。

`device.process` 用一个 `src: Option<SocketAddr>` 参数承载这个判定：`Some(src)` 表示过载（需要带上源地址去查 mac2/限速），`None` 表示非过载。是否传 `Some` 由 `handshake_worker` 决定。

under-load 判定有两路（[workers.rs:156-176](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L156-L176)）：

1. **阈值路**：当前 `pending`（待处理握手数）`> THRESHOLD_UNDER_LOAD` 时立即判过载，并刷新 `last_under_load = now`。
2. **迟滞路**：即便此刻未超阈值，只要距上次 `last_under_load` 不超过 `DURATION_UNDER_LOAD`（1 秒），仍视为过载——避免在阈值附近反复抖动。

相关常量（[constants.rs:17-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L17-L29)）：

- `MAX_QUEUED_INCOMING_HANDSHAKES = 4096`：设计上的最大排队握手数。
- `THRESHOLD_UNDER_LOAD = 4096 / 8 = 512`：触发过载的排队阈值。
- `DURATION_UNDER_LOAD = 1s`：过载后的保持时长（迟滞）。

> **源码阅读提示（待本地验证）**：握手队列实际由 `ParallelQueue::new(cpus, 128)` 创建（见 [wireguard.rs:273](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L273)、[queue.rs:15-27](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs#L15-L27)），容量为 **128**，小于 `THRESHOLD_UNDER_LOAD`（512）。由于 `udp_worker` 是先 `pending.fetch_add(1)` 再入队，`pending`（入队数 − 出队数）通常被这个 128 容量上界压住，难以触达 512 阈值；这意味着默认配置下「阈值路」较少触发，过载缓解机制主要在排队量真正接近上界时才介入。这是阅读本段源码时值得核实的细节，而不是规范要求——请在本地用日志打印 `pending` 实测确认。

#### 4.6.2 核心流程

`handshake_worker` 每取一个 job：

```
pending = wg.pending.fetch_sub(1)              // 返回的是「扣除前」的旧值
under_load = false
if pending > THRESHOLD_UNDER_LOAD:             // 阈值路
    last_under_load = now; under_load = true
if not under_load and (DURATION_UNDER_LOAD >= elapsed(last_under_load)):  // 迟滞路
    under_load = true

device.process(msg, src = under_load ? Some(src) : None)
```

`device.process`（以 TYPE_INITIATION 为例，TYPE_RESPONSE 结构相同）：

```
check_mac1(...)                                // 始终执行；失败 → InvalidMac1
if let Some(src) = src {                       // 仅过载
    if not check_mac2(..., src, ...):          // mac2 不过
        create_cookie_reply(...)               // 造 CookieReply
        return (None, Some(cookie_reply), None)  // 提前返回，不做 DH
    if not limiter.allow(src.ip()):            // 限速不过
        return Err(RateLimited)
}
consume_initiation(...) / create_response(...) // 昂贵的 Noise 运算
```

#### 4.6.3 源码精读

under-load 判定（阈值路 + 迟滞路）：

- [workers.rs:156-176](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L156-L176)：`let pending = wg.pending.fetch_sub(1, Ordering::SeqCst);`（注意 `fetch_sub` 返回旧值），随后两段判定。

把 src 传给 process（三元运算决定 Some/None）：

- [workers.rs:183-191](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L183-L191)：`if under_load { Some(src.into_address()) } else { None }`。

`process` 的 TYPE_INITIATION 分支——mac1 必查、mac2/限速仅 `Some(src)` 时查：

- [device.rs:330-356](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L330-L356)：`check_mac1` → `if let Some(src) = src { if !check_mac2 { create_cookie_reply; return (None, Some(reply), None) } if !limiter.allow { return Err(RateLimited) } }`。TYPE_RESPONSE 分支同构：[device.rs:386-411](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L386-L411)。

under-load 常量与语义注释：

- [constants.rs:16-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L16-L29)：`THRESHOLD_UNDER_LOAD = MAX_QUEUED_INCOMING_HANDSHAKES / 8`，`DURATION_UNDER_LOAD = 1s`，注释说明语义。

#### 4.6.4 代码实践

**目标**：跟踪一条「过载 Initiation」在 `process` 内的提前返回路径，确认它没有走到 `consume_initiation`。

**步骤**：

1. 在 [device.rs:330-356](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L330-L356) 中标出三处 `return`：mac1 失败（错误返回）、mac2 失败（CookieReply 提前返回）、限速失败（错误返回）。
2. 对照 4.4 的实践，说明只有在「mac2 通过 + 限速通过」后，代码才会继续到第 359 行的 `consume_initiation`（即真正花 CPU 做 DH）。

**预期结果**：能画出「过载时一次失败的握手最多只算一次 Blake2s MAC + 一次 XChaCha 封装 cookie，绝不做 DH」的结论——这正是 DoS 防御的精髓：把昂贵运算挡在廉价校验之后。

#### 4.6.5 小练习与答案

- **练习**：为什么需要「迟滞路」（过载后保持 1 秒）？
- **答案**：防止排队量在阈值附近抖动导致 under-load 状态频繁开关，使正在进行的 cookie 往返不至于因为状态忽变而失效；同时也给限速器一个稳定的生效窗口。
- **练习**：`process` 的 `src` 参数为 `None` 时会发生什么？
- **答案**：跳过 mac2 校验与令牌桶限速，mac1 通过后直接进入 Noise 握手。这是「设备轻松时优先吞吐」的设计。

---

## 5. 综合实践

把本讲四块内容串成一条完整的「过载握手」时序。请用纸笔或文本画出下面这一往返，并在每一步标注**调用的是 macs.rs / ratelimiter.rs / device.rs / workers.rs 的哪个函数**：

1. 客户端 `Generator::generate`：算 mac1，mac2 = 0，发出 Initiation。
2. 服务端 `udp_worker` 把报文入队、`pending` +1（[workers.rs:130-133](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L130-L133)）。
3. 服务端 `handshake_worker` 判定 under-load = true，以 `Some(src)` 调 `process`（[workers.rs:183-191](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L183-L191)）。
4. `process`：`check_mac1` 过 → `check_mac2` 失败（mac2=0）→ `create_cookie_reply` → 返回 `(None, Some(reply), None)`（[device.rs:335-350](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L335-L350)）。
5. 客户端 `Generator::process` 解出 cookie（[macs.rs:141-157](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L141-L157)）。
6. 客户端重发 Initiation，`Generator::generate` 这次 mac2 ≠ 0（[macs.rs:165-179](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L165-L179)）。
7. 服务端 `process`：`check_mac1` 过 → `check_mac2` 过 → `limiter.allow` 过 → 进入 `consume_initiation` 做 DH（[device.rs:335-359](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L335-L359)）。

**交付物**：一张时序图 + 一段说明，指出在第 4 步服务端**没有做任何公钥运算**，只在第 7 步才做——这就是本讲所有机制存在的意义。

## 6. 本讲小结

- WireGuard 用「两层 MAC + 令牌桶」把昂贵的 DH 握手挡在廉价校验之后：`mac1` 证明发送方知道响应方公钥（垃圾过滤），`mac2` 通过 cookie 证明源地址可回达（反伪造），令牌桶按 IP 限速。
- `mac1_key` 与 `cookie_key` 都由 `Blake2s(标签 || 公钥)` 派生；发送侧（`Generator`）用对端公钥、接收侧（`Validator`）用本机公钥，因「对端 = 响应方自己」而得到同一密钥。
- cookie 由「设备级随机 secret + 源地址」现算，secret 每 120 秒轮换、用双检锁管理；CookieReply 用 XChaCha20-Poly1305 以 mac1 为 AD 加密回传。
- `Validator`（per-device，在 `KeyState`）负责校验与发 cookie；`Generator`（per-peer，在 `Peer.macs`）负责算 mac1/mac2 与收 cookie——两者对称协作。
- under-load 由 `handshake_worker` 用「阈值 + 1 秒迟滞」判定，结果以 `src: Option<SocketAddr>` 传给 `process`；非过载只查 mac1，过载才叠加 mac2 与令牌桶。
- 默认握手队列容量为 128（`ParallelQueue::new(cpus, 128)`），与 `THRESHOLD_UNDER_LOAD=512` 的关系值得本地实测；但 cookie/ratelimiter 机制本身在 `process` 中已完整接线。

## 7. 下一步学习建议

- **u4-l5（时间戳、重放与洪泛防护）**：本讲的 ratelimiter 是「按 IP 限速」，u4-l5 讲另一维度的防护——`check_replay_flood` 用 TAI64N 时间戳防握手洪泛与重放，两者互补。
- **u4-l6（握手工作线程：驱动状态机）**：本讲只走到 `process` 返回，u4-l6 讲 `handshake_worker` 如何处理 `process` 的返回值（更新端点、字节计数、定时器回调、新增 keypair）。
- **延伸阅读**：对照 WireGuard 白皮书的「Denial of Service Mitigation」一节，把本讲的 `mac1`/`mac2`/cookie 与规范符号（`LC`、`mac1`、`mac2`、`cookie`）一一对应；并可阅读 SCTP/DTLS 的 cookie 机制作为对照。
