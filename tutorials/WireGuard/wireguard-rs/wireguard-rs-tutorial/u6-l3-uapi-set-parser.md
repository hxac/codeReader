# UAPI set 配置解析器

## 1. 本讲目标

本讲精读 `src/configuration/uapi/set.rs`，搞清楚 WireGuard 用户态实现是如何把 `wg(8)` 发来的一串 `set=1` 文本「逐行翻译」成对协议核心的配置调用。

学完后你应该能够：

- 画出 `LineParser` 在 `Interface` 与 `Peer` 两个状态之间的切换状态机。
- 说清 `private_key`/`listen_port`/`fwmark`/`public_key`/`allowed_ip` 等 key 各自如何被解析（十六进制还是数值、类型如何推断）。
- 解释 `replace_peers`、`update_only`、`remove`、`replace_allowed_ips` 这几个控制字段的语义，以及它们「先攒批、再提交」的设计。
- 看懂 `flush_peer` 如何把一个累积好的 `ParsedPeer` 按固定顺序批量下发到 `Configuration` trait。
- 独立扩展解析器，加入对未知（实验性）key 的宽容处理，并为它写一个解析用例。

## 2. 前置知识

本讲承接 u6-l2，那里我们建立了 UAPI 协议的全景：

- UAPI 是纯文本、按行、`key=value`、以空行结束事务的控制协议，只有 `get=1`（读状态）与 `set=1`（写配置）两种操作。
- 协议入口是 `handle<S: Read + Write, C: Configuration>`（[src/configuration/uapi/mod.rs:13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L13)）。
- 遇到 `set=1` 时，`handle` 会创建一个 `LineParser`，循环 `readline` → `keypair` → `parse_line`，最后再用 `parse_line("", "")` 强制提交最后一个 peer。

本讲只关注 `set` 这一支，主角是 `LineParser`。在阅读前，请确认你理解下面两个概念：

- **状态机（state machine）**：解析器需要记住「我现在正在配置接口，还是在配置某个 peer」，因为同一个 key（比如 `public_key`）在两种语境下含义不同。这种「记忆」就是状态。
- **trait 作为解析目标**：`LineParser` 并不直接操作协议核心，而是把解析结果通过 `Configuration` trait 的方法下发。这让解析逻辑与具体实现解耦，也使「用内存里的假实现测试解析器」成为可能。

此外需要一个直觉：WireGuard 的配置是**声明式 + 攒批（batch）**的。`wg set wg0 ...` 可以一次性写多个字段、多个 peer，解析器先把它们收进一个临时结构，等到事务结束（空行）再统一提交。这与「逐字段立即生效」相对，好处是减少对协议核心的锁竞争、保证一次 set 是原子的整体。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/configuration/uapi/set.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs) | 本讲主角。`LineParser`、`ParserState`、`ParsedPeer`、`parse_line`、`flush_peer` 全部在此。 |
| [src/configuration/uapi/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs) | `handle` 在此驱动 `set` 分支的循环，是 `LineParser` 的唯一调用方。 |
| [src/configuration/config.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs) | 定义 `Configuration` trait（解析器的下发目标）与 `get_protocol_version`（恒为 1）。 |
| [src/configuration/error.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/error.rs) | `ConfigError` 枚举与 `errno()` 映射，决定每种解析失败最终回写给 `wg(8)` 的 errno。 |

依赖方向：`mod.rs::handle` → `set.rs::LineParser` → `config.rs::Configuration` trait。`LineParser` 只依赖 trait，不知道背后是真实设备还是测试替身。

---

## 4. 核心概念与源码讲解

### 4.1 LineParser 与 ParserState 状态机

#### 4.1.1 概念说明

UAPI 的 `set` 配置是这样组织的：先写若干**接口级**字段（如 `private_key=...`、`listen_port=...`），接着每出现一个 `public_key=...` 就开始描述一个**新的 peer**，随后属于该 peer 的字段（`endpoint`、`allowed_ip` 等）紧随其后，直到下一个 `public_key` 或空行。

这就要求解析器有「上下文」：读到 `allowed_ip=...` 时，它必须知道这是在给当前 peer 加 allowed-ip，而不是在配置接口。`LineParser` 用一个两态状态机来表达这个上下文。

#### 4.1.2 核心流程

状态机只有两个状态，外加明确的迁移规则：

```
            ┌─────────────┐  遇到 public_key=   ┌──────────────────┐
  启动 ───▶ │  Interface  │ ───────────────────▶ │ Peer(ParsedPeer) │
            │  接口级配置  │ ◀─────────────────── │  攒批当前 peer   │
            └─────────────┘   (Peer 内再遇       └──────────────────┘
              ^               public_key：先          │
              │               flush 旧 peer,           │ 空行 / 事务结束
              │               再开新 peer)             ▼
              └──────────── flush_peer 提交 ◀─────────┘
```

要点：

1. 解析器**初始处于 `Interface`**。
2. 在 `Interface` 遇到 `public_key=` 即迁移到 `Peer`，并新建一个空的 `ParsedPeer`。
3. 在 `Peer` 遇到下一个 `public_key=`：先把当前 peer **提交（flush）**，再用新公钥开一个新 peer（始终停在 `Peer` 态）。
4. 空行 `""` 在 `Peer` 态触发提交；在 `Interface` 态被忽略（因为接口级字段大多是即时生效，无需攒批）。

#### 4.1.3 源码精读

状态用枚举表达，注意两个变体的顺序（`Peer` 在前只是书写顺序，与逻辑无关）：

[ParserState 枚举：src/configuration/uapi/set.rs:8-11](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L8-L11) —— 定义 `Peer(ParsedPeer)` 与 `Interface` 两个状态。`Peer` 变体把整个 `ParsedPeer` 带在身上，状态即数据。

解析器本身只持有一个配置引用和当前状态：

[LineParser 结构与生命周期：src/configuration/uapi/set.rs:25-28](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L25-L28) —— `LineParser<'a, C: Configuration>` 借用 `config: &'a C`，生命周期与这次 set 事务（一次 `wg` 连接）绑定，事务结束即销毁。

构造时固定从 `Interface` 态开始：

[new() 初始化：src/configuration/uapi/set.rs:31-36](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L31-L36)

而迁移到 `Peer` 态、并造出一个空 `ParsedPeer` 的动作封装在 `new_peer` 里：

[new_peer()：src/configuration/uapi/set.rs:38-53](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L38-L53) —— 把 64 位十六进制字符串解析成 32 字节公钥，构造一个全默认值的 `ParsedPeer`；解析失败返回 `ConfigError::InvalidHexValue`。注意它返回的是 `ParserState`，由调用方赋给 `self.state`，完成迁移。

驱动这个状态机的正是 `handle` 里的 set 循环：

[set=1 循环：src/configuration/uapi/mod.rs:51-63](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L51-L63) —— 逐行读，遇到空行 `break`，最后用 `parser.parse_line("", "")` 把「事务结束」信号送进去，从而触发最后一个 peer 的 flush。

#### 4.1.4 代码实践

**实践目标**：手工跟踪状态机的迁移，确认你理解了「`public_key` 既是接口态的出口、也是 peer 态内的切 peer 信号」这一双重身份。

**操作步骤**：

1. 假设有下面这段 UAPI set 文本（每个 `↵` 代表一个换行）：

   ```
   set=1↵
   private_key=<hex>↵
   public_key=<pkA>↵
   allowed_ip=10.0.0.1/32↵
   public_key=<pkB>↵
   endpoint=1.2.3.4:51820↵
   ↵
   ```

2. 模拟 `handle` 的循环，逐行记录 `parser.state` 的值。

**需要观察的现象**：每一行处理后 `state` 处于 `Interface` 还是 `Peer(谁)`，以及哪一行会触发 `flush_peer`。

**预期结果**：

| 输入行 | 处理后 state | 是否 flush |
| --- | --- | --- |
| `private_key=...` | `Interface` | 否（接口级即时下发） |
| `public_key=<pkA>` | `Peer(ParsedPeer{pk: A})` | 否 |
| `allowed_ip=10.0.0.1/32` | `Peer(A)` | 否（累积） |
| `public_key=<pkB>` | `Peer(ParsedPeer{pk: B})` | **是**：先 flush A，再开 B |
| `endpoint=1.2.3.4:51820` | `Peer(B)` | 否 |
| `""`（空行） | `Peer(B)` | **是**：flush B |

（空行不把状态退回 `Interface`——一次 set 事务结束后，`LineParser` 本就被丢弃。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Peer` 变体要把整个 `ParsedPeer` 带在枚举里，而不是让 `LineParser` 另外持有一个 `Option<ParsedPeer>` 字段？

**答案**：把数据带在状态变体里，是 Rust 里典型的「状态即数据」写法：编译器强制你在 `match` 里同时处理「现在是什么状态」和「这个状态附带什么数据」，不可能出现「状态说在 Peer 但数据是 None」的非法组合，把一类 bug 在类型层面消灭。

**练习 2**：如果在 `Interface` 态连续收到两个 `private_key=...`，会发生什么？

**答案**：两个都会被即时下发（覆盖前者）。`private_key` 不走攒批，每行直接调用 `config.set_private_key`，后者覆盖前者。

---

### 4.2 ParsedPeer：累积字段容器

#### 4.2.1 概念说明

`ParsedPeer` 是一个 peer 在被提交之前的「草稿」。一个 peer 可以同时带十几个字段，如果在读到每个字段时都立即调用 `config.xxx`，就要反复给同一个 peer 加锁、查找。WireGuard 的做法是：先把这次 set 涉及该 peer 的所有字段收进 `ParsedPeer`，等事务（或该 peer 段）结束时，由 `flush_peer` 一次性、按固定顺序下发。

#### 4.2.2 核心流程

`ParsedPeer` 的字段可以分成三类：

- **标识**：`public_key`——这是「给哪个 peer 配置」的钥匙。
- **控制位（bool）**：`remove`、`update_only`、`replace_allowed_ips`——它们不直接对应一个配置值，而是改变 flush 的行为。
- **可空配置值（Option / Vec）**：`allowed_ips`、`preshared_key`、`persistent_keepalive_interval`、`protocol_version`、`endpoint`。用 `Option` 表示「这次 set 有没有提到这个字段」，`None` 即「保持不变」。

这种 `Option` 设计很关键：UAPI 协议里「没写就是不改」，所以解析器需要区分「显式设置」与「不触及」，`Option` 正好表达这一点。

#### 4.2.3 源码精读

[ParsedPeer 结构：src/configuration/uapi/set.rs:13-23](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L13-L23)

逐字段说明：

- `public_key: PublicKey`——`x25519_dalek` 的 Curve25519 公钥，peer 的唯一标识。
- `update_only: bool`——为真时，flush 只更新现有 peer，不调用 `add_peer`（peer 不存在则跳过创建）。
- `allowed_ips: Vec<(IpAddr, u32)>`——`(地址, 前缀长度)` 列表，如 `("10.0.0.0", 24)`。
- `remove: bool`——为真时，flush 调用 `remove_peer` 删除该 peer。
- `preshared_key: Option<[u8; 32]>`——`None` 表示本次未提及；`Some([0u8;32])` 也合法，表示显式清除 PSK。
- `replace_allowed_ips: bool`——见 4.4，标记是否要清空已有 allowed-ip（注意它有个微妙之处）。
- `persistent_keepalive_interval: Option<u64>`——`None` 未提及，`Some(0)` 表示关闭 keepalive。
- `protocol_version: Option<usize>`——协议版本，目前只能是 1。
- `endpoint: Option<SocketAddr>`——对端地址。

这些字段在 `new_peer` 里被初始化为全默认（[set.rs:40-50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L40-L50)）：布尔位一律 `false`，`Option` 一律 `None`，`allowed_ips` 为空 `vec![]`。

#### 4.2.4 代码实践

**实践目标**：理解 `Option` 字段如何编码「显式设置 vs 不触及」。

**操作步骤**：

1. 阅读下面两段等价的 UAPI 文本，预测 `ParsedPeer.preshared_key` 的值：

   ```
   # 情况 A：完全不提 psk
   public_key=<pk>↵
   allowed_ip=10.0.0.1/32↵
   ```

   ```
   # 情况 B：显式把 psk 写成全零
   public_key=<pk>↵
   preshared_key=0000000000000000000000000000000000000000000000000000000000000000↵
   ```

2. 回答：flush 时，A 和 B 分别会不会调用 `config.set_preshared_key`？

**需要观察的现象**：`flush_peer` 里对 `preshared_key` 的判断是 `if let Some(psk) = peer.preshared_key`。

**预期结果**：

- 情况 A：`preshared_key = None` → flush **不调用** `set_preshared_key`，对端 PSK 维持不变。
- 情况 B：`preshared_key = Some([0;32])` → flush **调用** `set_preshared_key(pk, [0;32])`，把对端 PSK 显式清成全零。

这就是「不写 ≠ 写成默认值」的体现，也是 `Option` 在协议解析中的价值。

#### 4.2.5 小练习与答案

**练习 1**：`persistent_keepalive_interval: Option<u64>` 里，`Some(0)` 和 `None` 有何区别？

**答案**：`None` 表示本次 set 未提及该字段，flush 时跳过、保持原值；`Some(0)` 表示显式设置为 0，flush 会调用 `set_persistent_keepalive_interval(pk, 0)`，即关闭 persistent keepalive。

**练习 2**：为什么 `allowed_ips` 用 `Vec` 而不是 `Option<Vec<...>>`？

**答案**：因为 allowed-ip 天然是「可追加多条」的列表（一个 peer 常有多条 allowed-ip 行），用空 `Vec` 表示「没追加任何条目」已经足够，不需要再套一层 `Option`。是否要「清空再设置」由独立的 `replace_allowed_ips` 控制位表达。

---

### 4.3 parse_line：Interface 分支与十六进制/数值解析

#### 4.3.1 概念说明

`parse_line` 是状态机的「引擎」：吃进一个 `(key, value)`，根据当前状态分发到不同的处理分支。本节只看 `Interface` 态，它处理接口级配置：`private_key`、`listen_port`、`fwmark`、`replace_peers`，以及通向 peer 配置的入口 `public_key`。

这里还涉及「如何把文本 value 解析成具体类型」——WireGuard 的 UAPI 对不同 key 用了不同协议：密钥是 64 位十六进制字符串（32 字节），端口号、fwmark 是十进制数值，endpoint 是 `IP:port` 字符串。解析器分别用 `hex::FromHex`、`str::parse` 等手段处理。

#### 4.3.2 核心流程

`Interface` 态的分支（简化伪代码）：

```
match key:
  "private_key"  → hex 解析 32 字节；若为全零则清空(None)，否则 Some(sk)；即时下发
  "listen_port"  → parse 成 u16；即时下发（? 传播错误）
  "fwmark"       → parse 成 u32；0 表示清除(None)；即时下发
  "replace_peers"→ 值必须为 "true"，删掉所有现有 peer
  "public_key"   → 迁移到 Peer 态
  ""             → 忽略（事务结束）
  _              → Err(InvalidKey)
```

类型推断有个值得注意的点：`listen_port`、`fwmark` 这两处 `value.parse()` 都**没有写 turbofish**，类型完全靠下游 `config.set_listen_port(port: u16)` / `config.set_fwmark(Some(fwmark: u32))` 的形参类型反推。这是 Rust「通过期望类型推断」的典型用法。

#### 4.3.3 源码精读

先看一个细节：`parse_line` 开头有一段「看起来是调试日志」的代码：

[parse_line 开头：src/configuration/uapi/set.rs:55-61](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L55-L61)

这里有个值得警惕的点：它用了 `#[cfg(debug)]`。但当前 `Cargo.toml` 只声明了 `profiler` 与 `start_up` 两个 feature，并没有 `debug`；而 Rust 内建的 cfg 里也没有 `debug`（内建的是 `debug_assertions`）。因此除非外部构建脚本注入了同名 cfg，这段日志在常规构建下**不会被编译进来**——它目前相当于死代码。这是阅读源码时容易误以为「debug 构建会打印日志」的一个坑（待本地验证：可以用 `cargo build` 后观察是否产生该日志）。

下面是 Interface 分支的四个接口级字段：

[private_key 分支：src/configuration/uapi/set.rs:111-121](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L111-L121) —— 十六进制解析 32 字节私钥；用 `subtle::ConstantTimeEq`（`ct_eq`）做**常时间比较**判断是否为全零。全零是 UAPI 约定的「清除私钥」信号，转换为 `None` 下发。这里刻意用常时间比较，避免「私钥是否为全零」成为侧信道。`Choice.into()` 把常时间布尔转成普通 `bool`。

[listen_port 与 fwmark 分支：src/configuration/uapi/set.rs:124-140](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L124-L140) —— 两处 `value.parse()` 的目标类型分别由 `set_listen_port(port: u16)`、`set_fwmark(Some(fwmark): Option<u32>)` 反推。注意 `fwmark=0` 被解释为「清除 mark」（`None`），与私钥全零同理：UAPI 用「零值」表示清除。两处都用 `?` 把 `set_*` 返回的 `Result` 透传出去（例如端口绑定失败）。

[replace_peers 分支：src/configuration/uapi/set.rs:143-151](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L143-L151) —— 仅接受 `"true"`，其它值报 `UnsupportedValue`。它直接遍历 `config.get_peers()` 逐个 `remove_peer`，达到「清空所有 peer」的效果。

[public_key 迁移分支：src/configuration/uapi/set.rs:154-157](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L154-L157) —— 接口态的出口：调用 `new_peer(value)` 构造一个 `ParserState::Peer`，赋给 `self.state`，完成迁移。`?` 把非法十六进制转成 `InvalidHexValue`。

最后是空行与未知 key：

[空行与未知 key：src/configuration/uapi/set.rs:159-164](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L159-L164) —— Interface 态的空行被忽略；任何其它 key 一律 `ConfigError::InvalidKey`。

错误最终如何回给 `wg(8)`？`InvalidKey` 在 `errno()` 里映射到 `EPROTO`：

[errno 映射：src/configuration/error.rs:59-61](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/error.rs#L59-L61) —— `LineTooLong`、`InvalidKey`、`UnsupportedProtocolVersion` 都归为协议错误 `EPROTO`，而各种「值解析失败」归为 `EINVAL`。`handle` 末尾会把 errno 写回给客户端。

#### 4.3.4 代码实践

**实践目标**：验证「零值即清除」这一约定在 `private_key` 与 `fwmark` 上的对称性，并理解常时间比较的意义。

**操作步骤**：

1. 在 [src/configuration/uapi/set.rs:113](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L113) 的 `ct_eq` 调用处，写一条注释解释：为什么不用 `sk == [0u8;32]` 而要用 `ct_eq`。
2. 对照 `fwmark` 分支（[set.rs:133-140](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L133-L140)），指出两者「零值 → None」的共同点。

**需要观察的现象**：`private_key` 用常时间比较（安全敏感），`fwmark` 用普通相等判断 `fwmark == 0`（非敏感）。

**预期结果**：注释要点——私钥是秘密材料，「输入是否为全零」这一比较如果非常时间，可能因耗时差异泄露信息；fwmark 不是秘密，普通比较即可。这是「按敏感度选择比较方式」的好例子。

#### 4.3.5 小练习与答案

**练习 1**：`replace_peers=0` 会出现什么结果？

**答案**：`match value` 只接受 `"true"`，所以 `"0"` 落入 `_` 分支，返回 `Err(ConfigError::UnsupportedValue)`（errno = `EINVAL`）。

**练习 2**：为什么 `listen_port` 的 `value.parse()` 能推断出 `u16`？

**答案**：因为它的结果紧接着作为实参传给 `self.config.set_listen_port(port)`，而 trait 方法签名是 `fn set_listen_port(&self, port: u16)`。Rust 编译器用这个期望类型 `u16` 反推 `parse` 的泛型参数，等价于写 `value.parse::<u16>()`。

---

### 4.4 parse_line：Peer 分支与控制字段语义

#### 4.4.1 概念说明

进入 `Peer` 态后，`parse_line` 负责把该 peer 的字段逐行填进 `ParsedPeer`。这里有几类字段值得专门拎出来讲——它们不直接是「配置值」，而是改变解析或提交行为的**控制位**：

- `remove`：标记「删掉这个 peer」。
- `update_only`：标记「只更新、不创建」。
- `replace_allowed_ips`：标记「清空已有 allowed-ip 再加新的」。
- `public_key`（在 Peer 态再次出现）：切到下一个 peer——先 flush 当前的，再开新的。

理解这几个控制位是掌握 UAPI set 语义的关键。

#### 4.4.2 核心流程

Peer 态分支（简化伪代码）：

```
match key:
  "public_key"        → flush 当前 peer；new_peer(value) 切到新 peer
  "remove"            → peer.remove = true
  "update_only"       → peer.update_only = true
  "preshared_key"     → hex 解析 32 字节，存进 peer.preshared_key
  "endpoint"          → parse 成 SocketAddr，存进 peer.endpoint
  "persistent_keepalive_interval" → parse 成 u64，存进 peer.persistent_keepalive_interval
  "replace_allowed_ips" → peer.replace_allowed_ips = true；同时 peer.allowed_ips.clear()
  "allowed_ip"        → 按 "ip/cidr" 拆分，push 进 peer.allowed_ips
  "protocol_version"  → parse 成 usize，存进 peer.protocol_version
  ""                  → flush_peer（事务结束）
  _                   → Err(InvalidKey)
```

注意 `replace_allowed_ips` 的处理是**就地副作用**：除了置位，还立刻清空本地累积的 `allowed_ips`，因为「替换」意味着后面再出现的 `allowed_ip` 行要从零开始累积。

#### 4.4.3 源码精读

Peer 态开头是「切 peer」的逻辑：

[public_key（Peer 态）：src/configuration/uapi/set.rs:167-173](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L167-L173) —— 先 `flush_peer(self.config, &peer)` 提交当前 peer，再用新公钥造一个 `ParserState`。注意这里 flush 的返回值 `Option<ConfigError>` **被丢弃了**（见 4.5 关于 `protocol_version` 校验的讨论）。

控制位 `remove`、`update_only`：

[remove / update_only：src/configuration/uapi/set.rs:176-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L176-L185) —— 都是单行置位，不校验 value（UAPI 里它们的值恒为 `"true"`，但解析器并不检查）。

几个 `Option` 配置值的解析：

[preshared_key / endpoint / persistent_keepalive_interval：src/configuration/uapi/set.rs:188-212](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L188-L212) —— PSK 走十六进制（`<[u8;32]>::from_hex`），endpoint 与 keepalive 走 `value.parse()`，分别由后续赋值目标的字段类型（`[u8;32]`、`SocketAddr`、`u64`）推断。

`replace_allowed_ips` 与 `allowed_ip`：

[replace_allowed_ips：src/configuration/uapi/set.rs:215-219](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L215-L219) —— 置位并清空本地累积。

[allowed_ip：src/configuration/uapi/set.rs:222-233](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L222-L233) —— 用 `splitn(2, '/')` 把 `"10.0.0.0/24"` 拆成地址与前缀长度两段，分别 `parse()`（类型由元组 `(addr: IpAddr, cidr: u32)` 反推），失败报 `InvalidAllowedIp`。

`protocol_version`：

[protocol_version：src/configuration/uapi/set.rs:236-245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L236-L245) —— 这里显式写了 `let parse_res: Result<usize, _>`，因为 `protocol_version` 字段就是 `usize`，且没有下游方法能反推类型。

空行触发 flush：

[空行（Peer 态）：src/configuration/uapi/set.rs:248-252](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L248-L252) —— 事务结束，提交当前 peer。

最后是本讲实践任务要改造的「未知 key」分支：

[未知 key（Peer 态）：src/configuration/uapi/set.rs:254-256](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L254-L256) —— 任何无法识别的 key 一律 `Err(ConfigError::InvalidKey)`。Interface 态有一个完全对称的分支（[set.rs:162-164](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L162-L164)）。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：扩展 `LineParser`，使它在遇到**以 `x-` 开头的实验性未知 key** 时，记录一条 `warn!` 日志并正常跳过（返回 `Ok(())`），而不是直接报错；其它未知 key 仍报 `InvalidKey`。这种 `x-` 前缀是很多协议预留「实验/扩展」字段的习惯。

**操作步骤**：

1. 修改 Interface 态的未知 key 分支（[set.rs:162-164](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L162-L164)），把 `_ => Err(ConfigError::InvalidKey)` 改为：

   ```rust
   // 示例代码：本讲新增
   _ => {
       if key.starts_with("x-") {
           log::warn!("UAPI: 忽略实验性接口字段 {}={}", key, value);
           Ok(())
       } else {
           Err(ConfigError::InvalidKey)
       }
   }
   ```

2. 对 Peer 态的未知 key 分支（[set.rs:254-256](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L254-L256)）做同样改造（日志文案可改为「实验性 peer 字段」）。

3. 由于 `LineParser` 没有被 `pub use` 出 `configuration` 模块（`mod set;` 是私有的），外部 `tests/` 目录无法直接命名它。因此测试必须写在 `set.rs` 内部的 `#[cfg(test)]` 子模块里。在 `set.rs` 末尾追加（**示例代码，非项目原有**）：

   ```rust
   #[cfg(test)]
   mod tests {
       use super::*;
       use crate::configuration::config::PeerState;
       use std::net::{IpAddr, SocketAddr};
       use x25519_dalek::{PublicKey, StaticSecret};

       // 最小 Configuration 替身：绝大多数方法返回空操作（示例代码）
       struct MockConfig;
       impl Configuration for MockConfig {
           fn up(&self, _: usize) -> Result<(), ConfigError> { Ok(()) }
           fn down(&self) {}
           fn set_private_key(&self, _: Option<StaticSecret>) {}
           fn get_private_key(&self) -> Option<StaticSecret> { None }
           fn get_protocol_version(&self) -> usize { 1 }
           fn set_listen_port(&self, _: u16) -> Result<(), ConfigError> { Ok(()) }
           fn set_fwmark(&self, _: Option<u32>) -> Result<(), ConfigError> { Ok(()) }
           fn replace_peers(&self) {}
           fn remove_peer(&self, _: &PublicKey) {}
           fn add_peer(&self, _: &PublicKey) -> bool { true }
           fn set_preshared_key(&self, _: &PublicKey, _: [u8; 32]) {}
           fn set_endpoint(&self, _: &PublicKey, _: SocketAddr) {}
           fn set_persistent_keepalive_interval(&self, _: &PublicKey, _: u64) {}
           fn replace_allowed_ips(&self, _: &PublicKey) {}
           fn add_allowed_ip(&self, _: &PublicKey, _: IpAddr, _: u32) {}
           fn get_listen_port(&self) -> Option<u16> { None }
           fn get_peers(&self) -> Vec<PeerState> { vec![] }
           fn get_fwmark(&self) -> Option<u32> { None }
       }

       #[test]
       fn test_experimental_key_is_ignored() {
           let cfg = MockConfig;
           let mut parser = LineParser::new(&cfg);
           // 以 x- 开头的未知 key 应被忽略而非报错
           assert!(parser.parse_line("x-future-option", "42").is_ok());
           // 普通未知 key 仍应报错
           assert!(parser.parse_line("nonsense", "1").is_err());
       }
   }
   ```

   > 注：完整 trait 签名见 [Configuration trait：src/configuration/config.rs:64-193](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L64-L193)，上面的 `MockConfig` 需逐一对应实现。

**需要观察的现象**：`RUST_LOG=warn cargo test test_experimental_key_is_ignored` 运行时应打印一条 `UAPI: 忽略实验性接口字段 x-future-option=42` 的 warning；测试断言全绿。

**预期结果**：`x-` 前缀 key 返回 `Ok(())`、普通未知 key 返回 `Err(InvalidKey)`。

**待本地验证**：当前仓库 `Cargo.toml` 已包含 `log` 依赖（其它模块大量使用 `log::trace!`），但日志是否可见取决于运行时是否设置了 `RUST_LOG` 与某个日志后端；warning 是否真正输出需在本机验证（项目未显式初始化 `env_logger`，可临时在测试里 `let _ = env_logger::try_init();`）。断言部分（`is_ok`/`is_err`）不依赖日志后端，必然成立。

#### 4.4.5 小练习与答案

**练习 1**：`replace_allowed_ips` 这个控制位，除了置位还做了什么副作用？为什么？

**答案**：它还执行了 `peer.allowed_ips.clear()`（[set.rs:217](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L217)）。因为「替换」语义要求后面新出现的 `allowed_ip` 行从空集开始累积，而不是叠加到本次 set 早先累积的条目上。

**练习 2**：`remove` 和 `update_only` 的值如果不是 `"true"`（比如 `remove=false`），解析器会怎样？

**答案**：不会报错，因为这两处分支只看 key 不看 value，直接置 `peer.remove = true` / `peer.update_only = true`。严格来说这与「值必须为 true 才生效」的约定有出入——这是阅读时要注意的细节，真正的 UAPI 客户端 `wg(8)` 只会发 `=true`。

---

### 4.5 flush_peer：批量提交到 Configuration

#### 4.5.1 概念说明

`flush_peer` 是「攒批」的终点：把一个填好的 `ParsedPeer` 按固定顺序翻译成对 `Configuration` trait 的一串调用。它是一个定义在 `parse_line` 内部的嵌套函数（闭包性质的局部 fn），只在本文件内可见。

#### 4.5.2 核心流程

flush 的顺序（伪代码）：

```
fn flush_peer(config, peer):
  if peer.remove:           # 1. 删除优先
      config.remove_peer(pk); return
  if not peer.update_only:  # 2. 必要时先建 peer
      config.add_peer(pk)
  for (ip, cidr) in allowed_ips:   # 3. 加 allowed-ip
      config.add_allowed_ip(pk, ip, cidr)
  if let Some(psk):         # 4. 设 PSK
      config.set_preshared_key(pk, psk)
  if let Some(secs):        # 5. 设 keepalive
      config.set_persistent_keepalive_interval(pk, secs)
  if let Some(version):     # 6. 校验协议版本（不下发）
      if version == 0 or version > G: return Err(UnsupportedProtocolVersion)
  if let Some(endpoint):    # 7. 设 endpoint
      config.set_endpoint(pk, endpoint)
```

其中 `G = config.get_protocol_version()`，当前实现恒为 1（[config.rs:252-254](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L252-L254)）。因此协议版本通过的条件是：

\[
\text{version} \neq 0 \;\land\; \text{version} \leq G
\]

把 \(G=1\) 代入，唯一合法取值就是 \(\text{version}=1\)。

#### 4.5.3 源码精读

`flush_peer` 是 `parse_line` 里的嵌套函数：

[flush_peer 定义：src/configuration/uapi/set.rs:64-104](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L64-L104)

几个关键点：

- **返回 `Option<ConfigError>` 而非 `Result`**：目前唯一会返回 `Some(err)` 的路径是 `protocol_version` 校验失败。但调用处（[set.rs:170](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L170) 与 [set.rs:250](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L250)）都**丢弃了返回值**，所以即便协议版本非法，错误也不会冒泡到 `handle`、不会回写 errno。这是一个值得留意的实现细节（待确认：是否是有意为之）。
- **删除优先**（[set.rs:65-69](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L65-L69)）：`remove=true` 时只删不建，直接 `return`，跳过后续所有字段。
- **add_peer 受 update_only 门控**（[set.rs:71-74](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L71-L74)）：`update_only=true` 时跳过 `add_peer`，意味着「peer 不存在就不创建」，但其后的 `set_*` 仍会调用（对不存在的 peer 多为 no-op，见 [config.rs:311-333](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L311-L333) 的 `if let Some(peer)` 守卫）。
- **协议版本只校验、不下发**（[set.rs:91-96](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L91-L96)）：trait 里根本没有 `set_protocol_version` 方法，所以这里只能做合法性校验。

一个需要特别注意的「读到了但不消费」的字段：`replace_allowed_ips`。`flush_peer` 里**没有任何地方**调用 `config.replace_allowed_ips(pk)`（trait 方法存在，见 [config.rs:168](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L168)、实现见 [config.rs:323-327](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L323-L327)）。也就是说，`ParsedPeer.replace_allowed_ips` 这个字段被写入了（[set.rs:216](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L216)），却从不被读取。实际效果是：收到 `replace_allowed_ips=true` 时，只有本地累积器被清空，而设备上该 peer **已有的** allowed-ip 并不会被删除——这看起来是一处未完成/不一致（与内核 UAPI 的「替换」语义存在差距）。阅读源码时请把这个事实记下来。

#### 4.5.4 代码实践

**实践目标**：把 flush 的下发顺序内化为一张可预测的「调用序列表」，并验证上述 `replace_allowed_ips` 不被消费的现象。

**操作步骤**：

1. 给定一个 peer 段：

   ```
   public_key=<pk>↵
   update_only=true↵
   preshared_key=<hex 32>↵
   allowed_ip=10.0.0.0/24↵
   endpoint=1.2.3.4:51820↵
   ```

2. 预测 `flush_peer` 会按什么顺序调用哪些 `config.*` 方法，`add_peer` 是否会被调用。

3. 阅读 [config.rs:311-333](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L311-L333)，确认 `set_endpoint`/`set_persistent_keepalive_interval`/`add_allowed_ip` 在「peer 不存在」时是否安全（no-op）。

**需要观察的现象**：`update_only=true` 使 `add_peer` 被跳过；但因为没建 peer，后续 `add_allowed_ip`/`set_endpoint` 等方法内的 `if let Some(peer) = ...get(peer)` 守卫会落空，变成 no-op。

**预期结果**（调用序列）：

1. `add_peer` **不调用**（被 `update_only` 跳过）；
2. `add_allowed_ip(pk, 10.0.0.0, 24)`（若 peer 不存在则内部 no-op）；
3. `set_preshared_key(pk, psk)`；
4. `set_endpoint(pk, 1.2.3.4:51820)`（若 peer 不存在则内部 no-op）。

**待本地验证**：用 4.4.4 里的 `MockConfig` 改造为「记录每次调用」的版本（把 `bool`/计数器换成 `Mutex<Vec<String>>`），喂入上述文本，断言记录序列与预测一致。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `flush_peer` 把 `remove` 放在最前面、且删完就 `return`？

**答案**：因为「删除」与「配置字段」是互斥意图。一旦标记删除，后续的 endpoint、allowed-ip 等都没有意义（peer 都没了），所以删完立即返回，既省事也避免对已删除 peer 做无效操作。

**练习 2**：`protocol_version=2` 会导致什么？

**答案**：flush 时 `version=2 > G(=1)`，`flush_peer` 返回 `Some(ConfigError::UnsupportedProtocolVersion)`。但由于调用处丢弃返回值，这个错误不会冒泡——本次 set 仍按 errno=0 成功结束。这是阅读时要留意的「静默失败」。

---

## 5. 综合实践

把本讲全部内容串起来：手工模拟一次完整的 `wg setconf`，逐行预测解析器行为，再用一个「记录调用」的假 `Configuration` 验证。

**任务**：现有如下 UAPI set 事务（公钥以可读占位代替真实 hex）：

```
set=1
private_key=<sk_hex>
listen_port=51820
fwmark=0x100
replace_peers=true
public_key=<pkA>
allowed_ip=10.0.0.1/32
public_key=<pkB>
preshared_key=<psk_hex>
allowed_ip=10.0.0.2/32
endpoint=9.9.9.9:51820
remove=true

```

请完成：

1. **状态跟踪**：逐行写出 `parser.state` 的取值。
2. **调用序列预测**：写出最终对 `config.*` 的全部调用顺序（注意 `replace_peers` 会先删掉谁、`pkB` 的 `remove` 会让 flush 如何短路、`fwmark` 的十六进制是否能被 `value.parse::<u32>()` 接受）。
3. **陷阱排查**：指出至少两处「容易判断错」的地方。提示方向：
   - `fwmark=0x100` 是十六进制写法，Rust 的 `u32::FromStr` 默认**只接受十进制**，会发生什么？
   - `pkB` 段里既有 `preshared_key`/`allowed_ip`/`endpoint` 又有 `remove=true`，flush 会执行哪些、跳过哪些？

**验证方式**：在 4.4.4 的 `MockConfig` 基础上，把每个方法改成往 `Arc<Mutex<Vec<String>>>` 里 push 一条记录，构造 `LineParser` 后按行 `parse_line`（最后补一行 `parse_line("","")`），断言记录序列与你的预测一致。

**待本地验证**：`fwmark` 的解析行为、以及 `remove` 短路后 `preshared_key` 是否真的没被下发，都需要在本机用上面的记录型 mock 实测确认。

---

## 6. 本讲小结

- `LineParser` 是一个两态状态机（`Interface` / `Peer`），靠 `public_key=` 在两态间迁移，靠空行触发最后一个 peer 的提交。
- `ParsedPeer` 用 `Option`/`Vec` 字段精确表达「显式设置 vs 不触及」，用 `bool` 控制位表达 `remove`/`update_only`/`replace_allowed_ips`。
- Interface 分支即时下发接口级字段；密钥用十六进制 + 常时间比较，端口/mark 用数值 `parse()`，`fwmark=0`、私钥全零都表示「清除」。
- Peer 分支先攒批、由 `flush_peer` 按固定顺序（删 → 建 → allowed-ip → psk → keepalive → 版本校验 → endpoint）一次性下发。
- 未知 key 当前一律报 `InvalidKey`（errno=`EPROTO`）；本讲把它扩展为对 `x-` 实验前缀宽容。
- 两处值得记下的实现细节：`#[cfg(debug)]` 日志块在当前 features 下基本是死代码；`replace_allowed_ips` 字段被写入但 `flush_peer` 从不消费它（与内核「替换」语义有差距）。

## 7. 下一步学习建议

- **u6-l4（UAPI get 状态序列化）**：与本讲对称，讲 `get.rs` 如何把 `PeerState` 反向序列化成 UAPI 文本。两篇合起来就是 UAPI 协议的完整收发。
- **对照内核实现**：本讲指出的 `replace_allowed_ips` 未消费、`flush_peer` 返回值被丢弃、`#[cfg(debug)]` 死代码等细节，建议对照 WireGuard 内核驱动的 UAPI 处理（`net/wireguard/netlink.c`）看哪些是用户态实现的未完成项。
- **回看 u3-l1 / u6-l1**：`flush_peer` 下发的每个 `config.*` 最终落到 `WireGuardConfig`（u6-l1）对协议核心 `WireGuard`（u3-l1）的操作，可顺着 `add_peer`/`set_endpoint` 一路追到握手与路由器，把「配置如何驱动协议核心」补全。
