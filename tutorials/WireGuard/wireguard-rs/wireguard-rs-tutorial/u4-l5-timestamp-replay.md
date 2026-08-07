# 时间戳、重放与洪泛防护

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 WireGuard 用的 **TAI64N 时间戳**是怎么构造的，以及它和 Unix 时间的关系。
- 解释 `Peer<O>` 在握手过程中保存的可变状态（`state` / `timestamp` / `last_initiation_consumption`）与常量状态（`ss` / `psk`）各自的作用。
- 描述 `State::Reset` 与 `State::InitiationSent` 两个状态分别保存什么、何时切换。
- 读懂 `check_replay_flood` 如何同时防「时间戳重放」与「initiation 洪泛」，以及 `reset_state` 在密钥变更、握手中止时如何回收 receiver id。
- 自己为 `timestamp::compare` 编写边界测试，并解释「相等的时间戳也被当作旧」的原因。

## 2. 前置知识

在进入本讲前，你需要已经建立以下认知（来自 u4-l1 ~ u4-l4）：

- **握手报文线路格式**：Initiation 报文里有一个加密字段 `f_timestamp`，它是「12 字节明文时间戳 + 16 字节 Poly1305 标签」（见 u4-l1 的 `SIZE_TIMESTAMP + SIZE_TAG`）。时间戳经过 AEAD 加密，攻击者无法在不知密钥的情况下篡改它。
- **Noise IK 握手**（u4-l3）：发起方在 `create_initiation` 里把「当前时间戳」加密塞进报文；响应方在 `consume_initiation` 里解密出来。时间戳是握手报文的一部分，会揉进转录哈希 H。
- **receiver id**（u4-l2）：发起握手时由 `Device::allocate` 分配一个 4 字节的临时 local id，写进 `f_sender`，并登记进 `id_map`。握手结束后这个 id 要被 `release` 回收。
- **DH 共享密钥 `ss`**（u4-l2）：`ss = DH(本端静态私钥, 对端静态公钥)`，可在握手前预计算，存在每个 `Peer` 里。

本讲要回答的问题是：**当响应方收到一个 Initiation 时，它怎么判断「这是一个合理的、应该处理的新握手」，而不是被人录下来重放的旧报文，或者被人用高频 initiation 轰炸的洪泛？** 答案分两层——一层是「时间戳新鲜度」（防重放），一层是「initiation 频率」（防洪泛），都集中在 `check_replay_flood` 这一个函数里。先看它用的时间戳是怎么表示的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/wireguard/handshake/timestamp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/timestamp.rs) | 定义 `TAI64N` 类型、`now()`（取当前时间并编码）、`compare()`（比较两个时间戳的新鲜度）。本讲第一个最小模块。 |
| [src/wireguard/handshake/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs) | 定义 `Peer<O>` 结构体、`State` 枚举、`reset_state` 与 `check_replay_flood`。本讲的核心。 |
| [src/wireguard/handshake/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs) | 定义 `HandshakeError`，其中 `OldTimestamp` 与 `InitiationFlood` 两个变体就是本讲两道防线失败时的返回值。 |
| [src/wireguard/handshake/noise.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs) | 在 `create_initiation` 里 `SEAL` 时间戳、在 `consume_initiation` 里 `OPEN` 出时间戳并调用 `check_replay_flood`。是本讲逻辑的调用方。 |
| [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs) | `Device::release` 回收 receiver id；`update_ss` 在改私钥时调用 `reset_state` 中止进行中的握手。 |

## 4. 核心概念与源码讲解

### 4.1 TAI64N 时间戳：timestamp.rs

#### 4.1.1 概念说明

WireGuard 不用 Unix 时间戳（自 1970-01-01 起的秒数），而用 **TAI64N**。TAI64N 是 [TAI64](https://cr.yp.to/libtai/tai64.html) 的一种扩展，设计目的是有一个单调、不受闰秒影响的「国际原子时」刻度。它由两部分组成：

- 8 字节秒数（big-endian，大端）。
- 4 字节纳秒（big-endian，大端）。

合计 12 字节，所以源码里直接用一个定长数组表示：

```rust
pub type TAI64N = [u8; 12];
```

为什么不用 Unix 时间？因为 WireGuard 只关心「时间戳是否更新」，需要一个全局单调递增的刻度来比较新旧。TAI64N 的 12 字节定长也正好适配握手报文里 `f_timestamp` 的明文长度 `SIZE_TIMESTAMP = 12`（见 [src/wireguard/handshake/messages.rs:20](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L20)）。

TAI64N 的秒数零点与 Unix 时间零点之间差一个常量偏移：

\[ \text{TAI64 秒} = \text{Unix 秒} + \text{TAI64\_EPOCH} \]

其中 `TAI64_EPOCH = 0x400000000000000a`（约 \(4.6 \times 10^{18}\)），这个偏移对应「1970-01-01 UTC」与「TAI64 原点（1992-06-02 00:00:00 TAI 前推 2^62 秒）」之间的换算，并包含历史累积的 10 秒闰秒差（末尾的 `0xa`）。我们不需要记住它的来历，只需记住：**把 Unix 秒加上这个常量，就得到 TAI64 秒**。

#### 4.1.2 核心流程

timestamp 模块只做两件事：

1. **`now()`**：读系统时钟 → 算出 `(TAI64 秒, 纳秒)` → 拼成 12 字节 big-endian 数组。
2. **`compare(old, new)`**：判断 `new` 是否比 `old`「更新」（严格更大）。返回 `true` 表示 `new` 更新，`false` 表示 `new` 不比 `old` 新（相等或更旧）。

`now()` 的编码流程（伪代码）：

```
delta = 当前时间距 UNIX_EPOCH 的 Duration
tai64_secs = delta.secs + TAI64_EPOCH
tai64_nano = delta.subsec_nanos()
res[0..8]  = tai64_secs  的 big-endian 字节
res[8..12] = tai64_nano  的 big-endian 字节
return res
```

注意秒数和纳数都用 `to_be_bytes()` 写入，所以整个 12 字节是「大端序」的——最高位字节在下标 0。这一点对后面的 `compare` 至关重要。

#### 4.1.3 源码精读

先看常量与类型定义：

[src/wireguard/handshake/timestamp.rs:3-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/timestamp.rs#L3-L7) — 定义 12 字节的 `TAI64N`、TAI64 与 Unix 时代的偏移常量 `TAI64_EPOCH`，以及一个全零时间戳 `ZERO`（`consume_initiation` 用它作解密缓冲初值）。

再看 `now()`：

[src/wireguard/handshake/timestamp.rs:9-23](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/timestamp.rs#L9-L23) — 取 `SystemTime::now()` 距 `UNIX_EPOCH` 的 `Duration`，把秒数加上 `TAI64_EPOCH` 得到 TAI64 秒，再分别把 8 字节秒、4 字节纳秒按大端写入 `res`。

> 说明：`unwrap()` 假设系统时钟不会早于 Unix 元年；在正常主机上成立。`create_initiation` 在 [src/wireguard/handshake/noise.rs:293-300](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L293-L300) 处把这个 12 字节时间戳作为明文，用 `SEAL!` 加密进 `msg.f_timestamp`。

最后是本讲最需要细看的 `compare()`：

[src/wireguard/handshake/timestamp.rs:25-32](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/timestamp.rs#L25-L32) — 从高位字节（下标 0）向低位扫描，只要发现某个字节位 `new[i] > old[i]`，就判定 `new` 更新并返回 `true`；扫描完整 12 字节都没发现这样的位，则返回 `false`。

理解这段代码的关键有三点：

1. **从高位扫到低位**：因为 12 字节是大端序，下标 0 是最高位字节，最先比较的位最关键——这和「先比年、再比月、再比日」是一个道理。
2. **「相等」落在 `false`**：如果两个时间戳完全相同，循环里 `new[i] > old[i]` 永远不成立，最终返回 `false`。也就是说「相等」被当作「不更新」。这正是防重放想要的——同一个时间戳不能被接受第二次。
3. **对合法的单调时间戳足够**：现实里两个 TAI64N 只会随时间增长。当秒数增加时，必然在秒字段的某个字节上出现 `new > old`（进位链停在的那一位），循环会在到达纳秒字段前就返回 `true`。所以对真实时钟产生的时间戳，它的行为等价于「严格大于」。

> ⚠️ 一个值得注意的细节：这段实现**没有**在 `new[i] < old[i]` 时提前返回 `false`。严格意义上的字典序「大于」比较应当是 `>` 返回 true、`<` 返回 false、相等则继续。当前实现只在能证明「`new` 在每个字节都不超过 `old`」时返回 false。对于经 AEAD 认证、且随时间单调增长的真实时间戳，这与字典序「大于」效果一致；但若有人构造「高位更小、低位更大」的字节串，结果会偏离字典序。由于 `f_timestamp` 是加密认证的、攻击者无法随意改写，这一差异在实践中不会成为可利用的漏洞。本讲末尾的小练习会让你用测试亲自验证它的真实行为。

#### 4.1.4 代码实践

**实践目标**：为 `timestamp::compare` 编写单元测试，覆盖「新时间戳更大返回 true、相等或更小返回 false」三类边界，并解释「相等也返回 false」的防重放含义。

**操作步骤**：

1. `timestamp` 模块目前没有自带测试。在 [src/wireguard/handshake/timestamp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/timestamp.rs) 末尾追加一个 `#[cfg(test)]` 模块（这是示例代码，仅用于教学，不会真的去改源码仓库）：

   ```rust
   // 示例代码（教学用），追加到 timestamp.rs 末尾
   #[cfg(test)]
   mod tests {
       use super::*;

       // 工具：把 (secs, nanos) 编码成 TAI64N，便于构造确定性时间戳
       fn mk(secs: u64, nanos: u32) -> TAI64N {
           let mut res = [0u8; 12];
           res[..8].copy_from_slice(&(secs + TAI64_EPOCH).to_be_bytes()[..]);
           res[8..].copy_from_slice(&nanos.to_be_bytes()[..]);
           res
       }

       #[test]
       fn compare_newer_returns_true() {
         // 仅纳秒增加 -> 在纳秒字节的某一位出现 new > old
         let old = mk(1_000, 0);
         let new = mk(1_000, 5);
         assert!(compare(&old, &new)); // new 更新
       }

       #[test]
       fn compare_seconds_rollover_returns_true() {
         // 秒 +1、纳秒回绕到小值：真实时钟最常见情形
         let old = mk(1_000, 999_999_999);
         let new = mk(1_001, 1);
         assert!(compare(&old, &new));
       }

       #[test]
       fn compare_equal_returns_false() {
         // 完全相等 -> 重放，必须拒绝
         let t = mk(1_000, 7);
         assert!(!compare(&t, &t));
       }

       #[test]
       fn compare_older_returns_false() {
         // 各字节都不超过 old -> 旧时间戳
         let old = mk(2_000, 9);
         let new = mk(1_000, 0);
         assert!(!compare(&old, &new));
       }
   }
   ```

   注意 `mk` 里需要用 `secs + TAI64_EPOCH` 才能与 `now()` 的编码口径一致，因为 `TAI64_EPOCH` 是私有的，测试只能写在同文件内才能访问（这正是把测试放进 `timestamp.rs` 而不是别处的原因）。

2. 运行测试（在仓库根目录）：

   ```bash
   cargo test --lib handshake::timestamp
   ```

**需要观察的现象 / 预期结果**：

- `compare_newer_returns_true`、`compare_seconds_rollover_returns_true` 通过 → 证明「更晚的真实时间戳」会被判为更新。
- `compare_equal_returns_false` 通过 → 证明「完全相同的时间戳」被判为不更新。
- `compare_older_returns_false` 通过 → 证明「更早的时间戳」被拒绝。

> 若你对上面提到的「高位更小、低位更大」的边界好奇，可再加一个 `mk_high_low` 用例：例如 `old = mk(0, 0)` 手工把高位字节改大、低位字节改小后再比较，观察 `compare` 的返回，亲手感受它与严格字典序的差异。该用例的具体取值与预期「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TAI64N` 要用 big-endian（大端）存储，而不是 little-endian？

> **参考答案**：大端序让「最高位字节排在前面的低地址」，于是从下标 0 开始逐字节比较就等价于从最高有效位开始比较——`compare` 才能靠一次顺序扫描判断大小。若用小端序，最低位字节反而在下标 0，逐字节比较就没有「先高位后低位」的语义了。

**练习 2**：`now()` 里为什么是 `delta.as_secs() + TAI64_EPOCH`，而不是直接用 `delta.as_secs()`？

> **参考答案**：`delta` 是相对 Unix 元元（1970）的秒数，而 TAI64 秒用的是另一套原点。`TAI64_EPOCH` 就是这两个原点之间的固定换算偏移（含历史闰秒差），加上它才能把 Unix 秒换算成 TAI64 秒。

---

### 4.2 Peer 状态机：State 枚举与字段

#### 4.2.1 概念说明

`Peer<O>` 是握手模块里「对端」的全部状态。回忆 u4-l2：`Device<O>` 用 `pk_map`（公钥 → `Peer`）和 `id_map`（receiver id → 公钥）管理所有 peer。本讲聚焦单个 `Peer` 内部的字段。

一个 `Peer` 的字段分两类：

- **常量状态**（握手期间不变）：`opaque`（上层路由器句柄）、`ss`（预计算的静态 DH 共享密钥）、`psk`（预共享密钥）、`macs`（per-peer 的 MAC 生成器，见 u4-l4）。
- **可变状态**（随握手推进而变，需加锁）：`state`、`timestamp`、`last_initiation_consumption`。

其中 `state` 是一个两态的有限状态机：

- `State::Reset`：空闲态，当前没有进行中的、由本端发起的握手。
- `State::InitiationSent { local, eph_sk, hs, ck }`：本端已经发出一个 Initiation、正在等对端 Response 的「半完成」态。这里保存的四个字段是「等收到 Response 时继续算 Noise 所需的全部上下文」。

为什么要专门存这四个字段？因为 Noise IK 是两步握手：`create_initiation` 算到一半，把中间的链密钥 `ck`、转录哈希 `hs`、临时私钥 `eph_sk` 和分配到的 local id 暂存；等 `consume_response` 回来时，要用同一份 `ck/hs/eph_sk` 继续 DH 与 HKDF，才能和对端算出一致的会话密钥。所以这些中间量必须「挂」在 peer 上跨报文保存。

#### 4.2.2 核心流程

`State` 的状态转移（仅与发起侧相关）：

```
                 begin() / create_initiation()
   State::Reset  ───────────────────────────────►  State::InitiationSent{local, eph_sk, hs, ck}
       ▲                                                     │
       │                                                     │ 收到 Response 并完成 consume_response
       │                                                     ▼
       └────────────  reset_state() / check_replay_flood() ──┘
```

要点：

- **进入 `InitiationSent`**：`create_initiation` 在算完 Initiation 后，把 `hs/ck/eph_sk/local` 打包写进 `peer.state`（见下文源码精读）。
- **离开 `InitiationSent`**：有三条路径都会把 state 重置为 `Reset` 并回收 `local` id：
  1. `consume_response` 正常完成（握手成功）。
  2. `check_replay_flood` 在接受一个更新时间戳时，重置本端发起侧状态（见 4.3）。
  3. `reset_state()` 主动重置（被 `Device::update_ss` 在改私钥时调用，中止所有进行中的握手）。

#### 4.2.3 源码精读

先看 `Peer<O>` 的字段：

[src/wireguard/handshake/peer.rs:24-39](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L24-L39) — 这是单个 peer 的全部状态。注释把它清楚地分成「mutable state」「DoS mitigation」「constant state」三组：

- `state: Mutex<State>` — 上面讲的两态状态机。
- `timestamp: Mutex<Option<TAI64N>>` — **本 peer 见过的最新时间戳**，用于防重放（4.3）。`None` 表示还没收到过任何 Initiation。
- `last_initiation_consumption: Mutex<Option<Instant>>` — **本 peer 上一次「消费」一个 Initiation 的本地单调时刻**，用于防洪泛（4.3）。注意它用的是 `Instant`（单调钟）而非 `TAI64N`，因为洪泛判定只关心「两次接受之间隔了多久」，与绝对时间无关。
- `macs: Mutex<macs::Generator>` — per-peer 的 mac1/mac2 生成器（u4-l4）。
- `ss: [u8; 32]` — 预计算 DH 共享密钥（`DH(static, static)`），常量。
- `psk: Psk` — 预共享密钥，常量。

再看 `State` 枚举与它的 `Drop`：

[src/wireguard/handshake/peer.rs:41-59](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L41-L59) — `State::InitiationSent` 里存了 `local`（分配到的 receiver id）、`eph_sk`（临时私钥）、`hs` 与 `ck`（Noise 中间量）。`Drop` 实现保证状态被丢弃时把敏感的 `hs`、`ck` 清零（`eph_sk` 由 x25519-dalek 自己的 `Drop` 清），这是 u7-l2 会展开的密钥安全主题。

接着看 `create_initiation` 是如何把 state 置为 `InitiationSent` 的：

[src/wireguard/handshake/noise.rs:308-313](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L308-L313) — Initiation 算完后，把链密钥 `hs`、`ck`、临时私钥 `eph_sk`、local id 打包写入 `peer.state`，进入「已发出、等响应」态。

而 `consume_initiation`（响应方处理对端发来的 Initiation）会先把发起侧状态清空：

[src/wireguard/handshake/noise.rs:365-367](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L365-L367) — 响应方在确认对端公钥后，立即把 `peer.state` 重置为 `Reset`，丢弃任何旧的发起侧中间量（因为即将由对端驱动新一轮握手）。

最后看主动重置入口 `reset_state`：

[src/wireguard/handshake/peer.rs:74-79](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L74-L79) — 用 `mem::replace` 把 state 原子地换成 `Reset`，如果之前是 `InitiationSent` 就返回其中的 `local` id，调用方据此回收 id。它的一个调用点在 `Device::update_ss`：

[src/wireguard/handshake/device.rs:121-123](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L121-L123) — 改设备私钥时，遍历每个 peer 重算 `ss`，并调用 `reset_state()` 收集所有被中止握手的 id，随后由 `set_sk` 统一 `release`（见 [src/wireguard/handshake/device.rs:146-148](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L146-L148)）。这保证换私钥后，旧的、可能已泄露的握手中间量不会残留。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，把「`State::InitiationSent` 的四个字段」与「`consume_response` 继续计算时需要什么」一一对应起来，理解为什么必须跨报文保存它们。

**操作步骤**：

1. 打开 [src/wireguard/handshake/noise.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs)，找到 `create_initiation`（约 L235 起）和 `consume_response`。
2. 在 `create_initiation` 中标记四个写入点：`eph_sk`（L257）、`ck` 演进、`hs` 演进、`local`（来自参数），以及最终写 state 的 L308-313。
3. 在 `consume_response` 中找出它从 `State::InitiationSent` 取回 `eph_sk`、`hs`、`ck`、`local` 的位置，确认这四者被继续用于 `DH(eph_sk, ee)`、HKDF 与 nonce/sender 构造。

**需要观察的现象 / 预期结果**：

- 你应当看到 `consume_response` 完全依赖 `create_initiation` 存下的 `ck/hs/eph_sk` 才能算出与对端一致的密钥——这正是 `State::InitiationSent` 存在的理由。
- 任意一处中间量丢失或被错误重置，握手必然失败（解密/密钥不一致）。

> 结论性观察「待本地验证」：可尝试在脑中（不要真改源码）模拟「若 `reset_state` 在 `consume_response` 之前被误触发会发生什么」——答案是对端 Response 来到时 state 已是 `Reset`，`consume_response` 取不到中间量而失败，握手中止。这印证了 state 生命周期的严格性。

#### 4.2.5 小练习与答案

**练习 1**：`timestamp` 和 `last_initiation_consumption` 都用 `Mutex<Option<...>>`，但一个装 `TAI64N`、一个装 `Instant`。为什么类型不同？

> **参考答案**：`timestamp` 存的是报文里携带的、跨主机比较的绝对时间（TAI64N），用于防重放——必须与对端时钟口径一致。`last_initiation_consumption` 只用来本地判断「两次接受之间隔了多久」，不需要跨主机，所以用单调钟 `Instant`，且不受系统时钟回拨影响，更适合做频率判定。

**练习 2**：`State::InitiationSent` 里的 `local` 字段除了参与 Noise 计算，还有什么用途？

> **参考答案**：它是 `Device::allocate` 分配给这次发起握手的 receiver id（写进 `f_sender`，并登记进 `id_map`）。当握手结束或被中止时，`reset_state` 把它返回，调用方据此 `Device::release(local)` 把 id 从 `id_map` 移除，避免 id 泄漏。

---

### 4.3 防重放与防洪泛：check_replay_flood

#### 4.3.1 概念说明

响应方收到一个 Initiation 后，在投入昂贵的 Noise 计算之前，已经过了 mac1/mac2 与限速器（u4-l4）的廉价过滤。但即便报文通过了 MAC 校验、解出了真实时间戳，仍有两个风险：

- **重放（replay）**：攻击者录下一条合法的、带较新时间戳的 Initiation，反复发给响应方。如果不加防御，响应方会一次次为同一时间戳跑完整握手、派生密钥、触发上层回调，浪费 CPU 并扰乱密钥轮转。
- **洪泛（flood）**：即便每条 Initiation 时间戳都更新（攻击者控制了合法私钥，或伪造了大量通过 mac1 的报文），只要响应方处理得够快，攻击者就能用高频 initiation 耗尽 CPU。

`check_replay_flood` 用两道检查分别应对：

1. **防重放**：每个 peer 记住「见过的最新时间戳」。新到的 `timestamp_new` 必须严格更新（`compare` 返回 true），否则直接 `Err(OldTimestamp)`。相等也算旧——所以同一条 Initiation 重放第二次必被拒。
2. **防洪泛**：每个 peer 记住「上一次接受 Initiation 的本地时刻」。若距上次接受不到 `TIME_BETWEEN_INITIATIONS`（20 毫秒），返回 `Err(InitiationFlood)`。

注意两道检查的对象不同：重放看的是**对端时钟产生的时间戳**（TAI64N），洪泛看的是**本端单调钟**（Instant）。

#### 4.3.2 核心流程

`check_replay_flood` 在持有三把锁（`state` / `timestamp` / `last_initiation_consumption`）的前提下顺序执行：

```
锁住 state, timestamp, last_initiation_consumption

1) 防重放：
   if 存在 timestamp_old 且 compare(timestamp_old, timestamp_new) == false:
       return Err(OldTimestamp)        # 新时间戳不比旧的新（含相等）→ 拒绝

2) 防洪泛：
   if 存在 last 且 last.elapsed() < 20ms:
       return Err(InitiationFlood)     # 距上次接受太近 → 拒绝

3) 重置发起侧状态、回收 id：
   if state == InitiationSent{ local, .. }:
       device.release(local)           # 把旧的 local id 还回 id_map

4) 更新两本「账」：
   state = Reset
   timestamp = Some(timestamp_new)     # 记下这次见到的最新时间戳
   last_initiation_consumption = Some(Instant::now())  # 记下本次接受时刻
   return Ok(())
```

几个关键设计：

- **三把锁一起拿**：把三处可变状态当作一个临界区，避免「检查通过、写入前被别处改掉」的 TOCTOU 竞态。`spin::Mutex` 在临界区很短的情况下比系统 Mutex 更轻。
- **先检查、通过后才更新账本**：只有通过两道检查，才会把 `timestamp_new` 写入 `timestamp`、把当前时刻写入 `last_initiation_consumption`。被拒绝的报文不会污染这两本账。
- **顺手回收 id**：一旦决定接受这个新时间戳，意味着上一轮发起侧握手（如果有）作废，于是调用 `device.release(local)` 把它占用的 receiver id 还回去，再 `state = Reset`。这与 4.2 讲的「离开 `InitiationSent`」三条路径中的第 2 条对应。

它的唯一调用点在 `consume_initiation`：

[src/wireguard/handshake/noise.rs:388-390](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L388-L390) — 解密出时间戳 `ts` 后，立刻调用 `peer.check_replay_flood(device, &ts)?`。注意它在「H := Hash(H || msg.timestamp)」（L392-394）**之前**返回——也就是说重放/洪泛的报文不会进入后续转录哈希，提前被丢弃。

`TIME_BETWEEN_INITIATIONS` 的定义：

[src/wireguard/handshake/peer.rs:19](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L19) — 洪泛判定阈值，20 毫秒。含义是：对同一个 peer，本端至少每 20ms 才会接受一次新的 initiation（即每秒至多约 50 次握手/peer）。

#### 4.3.3 源码精读

完整的 `check_replay_flood`：

[src/wireguard/handshake/peer.rs:87-120](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L87-L120) — 函数体严格按照上面「核心流程」的四步展开。重点逐段说明：

- **L97-101 防重放**：`if let Some(timestamp_old) = *timestamp` 表示「之前见过时间戳」。`!timestamp::compare(&timestamp_old, &timestamp_new)` 为真意味着「新时间戳不比旧的新」，返回 `OldTimestamp`。注意首次收到（`timestamp == None`）时跳过此检查——没有任何旧时间戳可比，天然接受。
- **L104-108 防洪泛**：`if let Some(last) = *last_initiation_consumption` 表示「之前接受过」。`last.elapsed() < TIME_BETWEEN_INITIATIONS` 为真则距上次接受不足 20ms，返回 `InitiationFlood`。同样，首次接受时跳过。
- **L111-113 回收 id**：若当前 state 是 `InitiationSent`，说明本端之前主动发起过一次握手还没收尾；现在对端反而发来了更新的 Initiation，那次发起作废，把 `local` 还给 `id_map`。
- **L116-118 更新账本**：通过检查后，把 state 置 `Reset`、记录新时间戳、记录本次接受时刻。

错误类型定义在 types.rs：

[src/wireguard/handshake/types.rs:37-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L37-L49) — `HandshakeError` 枚举里，`OldTimestamp`（L44）与 `InitiationFlood`（L48）正是这两道防线失败时的返回值。它们的 `Display` 文案见 [src/wireguard/handshake/types.rs:61](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L61)（"Timestamp is less/equal to the newest"）与 [src/wireguard/handshake/types.rs:65-67](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L65-L67)（"dropped because of initiation flood"）。

`Device::release` 的实现：

[src/wireguard/handshake/device.rs:268-271](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L268-L271) — 从 `id_map` 移除该 id，并用 `assert!` 保证被释放的 id 确实是先前分配过的（防御编程：若逻辑错误地释放了未分配的 id，程序会 panic 而非静默出错）。

把整条调用链串起来（响应方收到 Initiation 时）：

```
udp_worker 入队 → handshake_worker → device.process
  → consume_initiation
     → OPEN! 解密 f_timestamp 得 ts
     → peer.check_replay_flood(device, &ts)   ← 本讲的防线
        ├─ compare 防重放
        ├─ elapsed 防洪泛
        ├─ device.release(local) 回收旧 id
        └─ 更新 timestamp / last_initiation_consumption
     → H := Hash(H || msg.timestamp)
```

#### 4.3.4 代码实践

**实践目标**：用一张时序图把「重放」与「洪泛」两种攻击下 `check_replay_flood` 的判定过程画出来，并对照源码标注每一步读/写了哪个字段。

**操作步骤**：

1. 阅读上面的「完整调用链」，确认 `check_replay_flood` 在 `consume_initiation` 中的位置。
2. 在纸上或笔记里画两张图：

   **图 A：重放攻击**
   ```
   攻击者录下 T=100 的合法 Initiation（首次被接受，timestamp: None→100, last: None→now）
   攻击者重放 T=100
     → compare(100, 100) == false（相等）
     → Err(OldTimestamp)，丢弃
   ```
   **图 B：洪泛攻击**（假设攻击者每次都构造了更新的时间戳 100,101,102,...）
   ```
   t=0ms   T=100 被接受（last: None→0ms）
   t=5ms   T=101 到达 → last.elapsed()=5ms < 20ms → Err(InitiationFlood)，丢弃
   t=8ms   T=102 到达 → last.elapsed()=8ms < 20ms → Err(InitiationFlood)，丢弃
   t=21ms  T=103 到达 → last.elapsed()=21ms ≥ 20ms → 通过，接受（last: →21ms）
   ```

3. 在每一步旁标注它读取或写入了 `Peer` 的哪个字段（`timestamp` / `last_initiation_consumption` / `state`）以及是否调用了 `device.release`。

**需要观察的现象 / 预期结果**：

- 图 A 中第二次到达的报文，因为时间戳相等，连 `last_initiation_consumption` 都不会被更新——账本只在通过检查后才写。
- 图 B 中被 `InitiationFlood` 拒绝的报文，同样不会更新 `timestamp`，所以也不会「刷高」重放基线。

> 这两项观察「待本地验证」：若你想在运行时确认，可在 `check_replay_flood` 的两个 `return Err(...` 前各加一行 `log::debug!`（需注意 u1-l5 提到的日志在降权后才初始化），然后用 dummy 平台（u2-l4）构造高频/重复 initiation，观察日志中两种错误的触发分布。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `check_replay_flood` 要同时锁住 `state`、`timestamp`、`last_initiation_consumption` 三把锁，而不是各自单独检查？

> **参考答案**：检查与更新必须是一个原子临界区。若分开加锁，可能出现「线程 A 检查时间戳通过、还没写入新时间戳；线程 B 也检查通过」的竞态，导致同一个重放报文被两个线程都接受。三把锁一起拿，保证「读旧值 → 判定 → 写新值」对并发握手是不可分割的。

**练习 2**：把 `TIME_BETWEEN_INITIATIONS` 调大（比如 1 秒）会更安全吗？会有什么副作用？

> **参考答案**：调大能更狠地压制洪泛，但会拖慢合法的快速重握手——比如丢包导致握手超时后想立即重试时，会被这个阈值挡住，延长隧道恢复时间。20ms 是 WireGuard 在「防洪泛」与「握手 responsiveness」之间权衡的取值。

**练习 3**：`check_replay_flood` 里回收 id 用的是 `device.release(local)`，而 `consume_response` 成功后也会回收 id。这两次回收会不会对同一个 id 重复释放（触发 `assert!`）？

> **参考答案**：不会。`check_replay_flood` 只在 `state == InitiationSent` 时才 release，并立即把 state 置为 `Reset`。`InitiationSent` 是「本端发起侧」的态，而 `consume_response` 处理的是「本端发起后对端的响应」——一旦 `check_replay_flood`（因收到对端更新的 initiation）把发起侧重置并 release 了 local，state 就是 `Reset`，后续不会再走 `consume_response` 对该 local 的回收路径。id 的所有权在任何时刻只属于一个状态，从而避免双重释放。

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「源码阅读 + 推理」任务：

**任务**：假设 peer A 与 peer B 已配置好彼此公钥。请按时间顺序描述下列事件中 `Peer<B>`（在 A 的视角下，A 是 B 的 peer；同理对称）的状态字段变化，并指出每一步是 `check_replay_flood` 通过、还是返回了某种 `HandshakeError`：

1. `T=0`：A 调用 `device.begin(B 的公钥)`，发起首次握手。
2. `T=5ms`：B 收到 A 的 Initiation（时间戳 TA=100）。
3. `T=8ms`：攻击者把同一条 Initiation（时间戳仍是 100）重放给 B。
4. `T=30ms`：A 因未收到 Response，再次 `begin`，发出时间戳 TA=101 的新 Initiation，B 在 `T=31ms` 收到。
5. `T=33ms`：攻击者把第 4 步那条（时间戳 101）再重放给 B。

要求：

- 对事件 2/3/4/5，分别写出 B 侧 `check_replay_flood` 走到哪个分支、`timestamp` 与 `last_initiation_consumption` 的新值、是否调用 `device.release`。
- 用一张表呈现结果。

**参考答案表**（以 B 处理 A 的报文为视角；B 上代表 A 的那个 `Peer` 记为 `P_A`）：

| 事件 | B 侧判定 | `P_A.timestamp` | `P_A.last_initiation_consumption` | `device.release`？ |
| --- | --- | --- | --- | --- |
| 2 (T=5ms, TA=100，首次) | 通过（无旧时间戳、无 last） | `Some(100)` | `Some(5ms)` | 否（state 本就是 Reset） |
| 3 (T=8ms, TA=100 重放) | `OldTimestamp`（compare(100,100)==false） | 不变 `Some(100)` | 不变 `Some(5ms)` | 否（提前返回） |
| 4 (T=31ms, TA=101) | 通过（compare(100,101)==true；elapsed≈26ms≥20ms） | `Some(101)` | `Some(31ms)` | 视 state 而定：若 B 之前对 A 主动发起过则 release，否则否 |
| 5 (T=33ms, TA=101 重放) | `InitiationFlood`（elapsed≈2ms<20ms）；即便先过了洪泛也会被 `OldTimestamp`（compare(101,101)==false）拦 | 不变 | 不变 | 否 |

> 表中事件 5 的两道检查顺序以源码为准：先查重放（`compare(101,101)==false` → `OldTimestamp`），实际在重放检查处就返回了，不会走到洪泛检查。即重放优先于洪泛判定。

## 6. 本讲小结

- WireGuard 用 12 字节的 **TAI64N**（8 字节大端秒 + 4 字节大端纳秒）作时间戳；`now()` 把 Unix 秒加上 `TAI64_EPOCH` 换算成 TAI64 秒再编码。
- `compare(old, new)` 从高位字节往低位扫描，发现任一字节 `new[i] > old[i]` 即判 `new` 更新；完全相等返回 `false`——这是「相同时间戳被视为旧」的防重放基础。
- `Peer<O>` 的可变状态有三处：`state`（两态握手状态机）、`timestamp`（见过的最新 TAI64N）、`last_initiation_consumption`（上次接受 initiation 的本地单调时刻）；常量状态有 `ss`、`psk`、`macs`、`opaque`。
- `State::InitiationSent{ local, eph_sk, hs, ck }` 保存发起侧握手跨报文所需的全部 Noise 中间量；`Drop` 会清零敏感的 `hs/ck`。
- `check_replay_flood` 在一个临界区内做两道检查：**时间戳新鲜度**（防重放，返回 `OldTimestamp`）与**接受频率**（防洪泛，`TIME_BETWEEN_INITIATIONS=20ms`，返回 `InitiationFlood`）；通过后才更新账本并回收旧 local id。
- `reset_state` 在改设备私钥（`update_ss`）、接受更新时间戳等场景下重置发起侧状态、归还 receiver id，避免握手中间量残留与 id 泄漏。

## 7. 下一步学习建议

- 本讲只讲了「响应方收到 Initiation」时的重放/洪泛防护。发起侧由谁驱动、握手成功后如何把新密钥交给路由器、失败如何重试，要看 **u4-l6 握手工作线程**，那里会把 `device.process` / `device.begin` 与 `handshake_worker` 串成完整时序。
- 密钥材料的清零细节（`State::Drop`、`Key::Drop`、`clear_stack_on_return_fnone`）在 **u7-l2 密钥材料的清零与安全** 中系统展开，可与本讲 4.2.3 的 `Drop for State` 对照阅读。
- 数据面报文的序号防回放（传输层的滑动位图，RFC 6479）与本讲的握手时间戳防重放是两套独立机制，前者在 **u5-l7 防回放窗口** 中讲解，建议对比「握手层用时间戳、传输层用计数器位图」的设计差异。
