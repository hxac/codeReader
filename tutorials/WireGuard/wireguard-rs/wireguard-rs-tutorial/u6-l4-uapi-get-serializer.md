# UAPI get 状态序列化

## 1. 本讲目标

本讲承接 u6-l2 讲过的 UAPI 协议处理框架。在 `handle()` 里，`get=1` 操作没有状态机、不需要逐行解析，它只是把设备的当前状态「拍一张快照」并按固定格式吐成文本。本讲就精读这张快照的生成器：`src/configuration/uapi/get.rs` 里的 `serialize()`。

学完后你应该能够：

- 说出 `serialize()` 输出的字段顺序与每一行的格式（`key=value\n`）。
- 解释为什么 `last_handshake_time` 在线路上的一个逻辑值会被拆成 `last_handshake_time_sec` 和 `last_handshake_time_nsec` 两行。
- 说明 `allowed_ip` 是如何由 `(IpAddr, u32)` 序列化成 `ip/cidr` 形式的。
- 看懂这些待序列化的值是从哪里来的——也就是 `config.rs` 的 `get_peers()` 如何把散落在各处的 per-peer 运行态聚合成无泛型的 `PeerState`。
- 动手写一个用「内存替身」驱动 `serialize()` 的单元测试。

## 2. 前置知识

- **UAPI 文本协议**（u6-l2）：`wg(8)` 通过一个 Unix 域套接字与守护进程通信，协议是按行、`key=value`、以空行结束事务的纯文本。`get=1` 请求设备状态。
- **`Configuration` trait**（u6-l1）：一个**不含 IO 泛型**的配置接口，把复杂的协议核心类型对宿主应用隐藏起来。`serialize()` 只依赖这个 trait，而不依赖任何具体平台类型——这是它能在内存流上做单元测试的关键。
- **`PeerState` 快照**：一个普通结构体，集中保存某个 peer 的「当前状态」（收发字节、最近握手时间、端点、allowed-ips……），是协议核心动态状态在某一时刻的只读拷贝。
- **`Option` 与「省略字段」**：UAPI 中很多字段是可选的（私钥未设、未握过手、未设端点）。Rust 侧用 `Option<T>` 表达，序列化时用 `if let Some` / `map` 决定「写还是不写这一行」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/configuration/uapi/get.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs) | 本讲主角。`serialize()` 把设备与各 peer 状态写成 UAPI 文本。 |
| [src/configuration/config.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs) | 定义 `PeerState` 结构体、`Configuration` trait，以及 `get_peers()` 如何把核心里的动态状态聚合成快照。 |
| [src/configuration/uapi/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs) | `handle()` 在收到 `get=1` 时把流传给 `serialize()`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. `serialize()` 的整体骨架与 `write` 闭包。
2. 接口级字段序列化（`private_key` / `listen_port` / `fwmark`）。
3. peer 级字段序列化（`rx/tx_bytes` / `last_handshake_time` / `endpoint` / `allowed_ip` 等）。
4. 序列化的数据来源：`PeerState` 快照与 `get_peers()`。

### 4.1 `serialize()` 的整体骨架与 `write` 闭包

#### 4.1.1 概念说明

`serialize()` 做的事情本质上很简单：**遍历一份状态，按固定顺序把每个字段格式化成 `key=value\n` 写进输出流**。它本身不做任何协议计算，纯粹是一个「结构体 → 文本」的翻译器。

有两个设计要点让它显得「整洁」：

- **`write` 闭包**：每个字段都要重复「写 key、写 `=`、写 value、写 `\n`」四步，很容易写错。作者用一个捕获了 `writer` 的闭包把这四步封装起来，调用点就退化成 `write("rx_bytes", n.to_string())`，清爽且统一。
- **泛型在「源头」就被切断**：签名是 `serialize<C: Configuration, W: io::Write>(writer: &mut W, config: &C)`。`C` 只要求实现 `Configuration`（一个无泛型的 trait），`W` 只要求能写字节。所以 `serialize()` 既不知道也不关心背后是 `WireGuardConfig<LinuxTun, LinuxUDP>` 还是别的什么——它只看到一组 `get_*` 方法。这也是我们后面能用一个「内存替身」来测它的原因。

#### 4.1.2 核心流程

```
serialize(writer, config):
    定义闭包 write(key, value):  # 统一的 key=value\n 写出
        断言 value 与 key 都是 ASCII
        写 key; 写 '='; 写 value; 写 '\n'

    # —— 接口级字段（可选）——
    若 get_private_key()   有值 → 写 private_key
    若 get_listen_port()   有值 → 写 listen_port
    若 get_fwmark()        有值 → 写 fwmark

    # —— peer 级字段（逐个 peer）——
    peers = get_peers()
    while peers 还有 peer p:
        写 public_key、preshared_key、rx_bytes、tx_bytes、persistent_keepalive_interval
        若 last_handshake_time 有值 → 写 sec、nsec 两行
        若 endpoint 有值           → 写 endpoint
        对每个 allowed_ip          → 写 allowed_ip=ip/cidr
```

顺序上**先接口、后 peers**；peer 内部则按固定的字段顺序输出。需要特别留意：**字段顺序是 `serialize()` 硬编码的，不随内核 UAPI 规范的字段顺序变化**（一个典型差异见 4.3）。

#### 4.1.3 源码精读

`serialize()` 的整体骨架与 `write` 闭包（[src/configuration/uapi/get.rs:5-14](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L5-L14)）：

```rust
pub fn serialize<C: Configuration, W: io::Write>(writer: &mut W, config: &C) -> io::Result<()> {
    let mut write = |key: &'static str, value: String| {
        debug_assert!(value.is_ascii());
        debug_assert!(key.is_ascii());
        log::trace!("UAPI: return : {}={}", key, value);
        writer.write_all(key.as_ref())?;
        writer.write_all(b"=")?;
        writer.write_all(value.as_ref())?;
        writer.write_all(b"\n")
    };
    // …… 后续用 write("xxx", ...) 逐字段输出 ……
```

要点逐条说明：

- `write` 是个 **`FnMut` 闭包**，`key` 限定为 `&'static str`（键都是编译期常量，零分配），`value` 是 `String`（值需要格式化，因此由调用方构造好再传进来）。
- 两处 `debug_assert!(...is_ascii())` 是廉价的健全性检查：UAPI 协议的所有键值都应是 ASCII，若混入非 ASCII 字节（例如 `IpAddr`/`SocketAddr` 的 `to_string()` 在正常情况下不会，但理论上 IP 字面量可能含奇形怪状字符），debug 构建会 panic，release 构建则跳过。
- `writer.write_all(...)` 四连写：`key`、`=`、`value`、`\n`。注意行尾是单个 `\n`（LF），与 UAPI 行格式一致。
- 闭包内部用 `?` 传播 `io::Error`，所以 `write` 本身返回 `io::Result<()>`。

`get=1` 是在哪里调用 `serialize()` 的：[src/configuration/uapi/mod.rs:47-50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L47-L50)。`serialize(stream, config)` 把状态直接写回给客户端的 Unix 流，IO 错误被映射成 `ConfigError::IOError`。

#### 4.1.4 代码实践

**实践目标**：亲手驱动 `serialize()`，观察它的输出。

**操作步骤**：

1. 阅读本讲的「4.4 综合小练习」给出的完整替身测试，理解它如何用一个不依赖真实网络的 `MockConfig` 实现 `Configuration`。
2. 把那段测试放进 `src/configuration/uapi/get.rs` 末尾的 `#[cfg(test)] mod tests { ... }`。
3. 运行 `cargo test --lib serialize_dumps_expected_lines`（或你给测试取的名字）。

**需要观察的现象**：

- 测试通过，说明 `serialize()` 在没有任何 socket、TUN、UDP 的情况下也能跑起来——它只依赖 `Configuration` trait 与一个可写字节流。
- 把断言里的 `assert!` 临时改成 `println!("{}", out)`，可以肉眼看到完整的 `key=value\n` 列表。

**预期结果**：`out` 是一段 ASCII 文本，每个字段一行，以 `\n` 分隔；接口级字段在前，peer 字段在后。

**待本地验证**：若你的工具链对 `debug_assert!` 有特殊设置，确认 release 构建下非 ASCII 检查被跳过（不会 panic）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `write` 闭包的 `key` 参数类型是 `&'static str` 而不是 `&str`？

> **参考答案**：因为所有键名（`"private_key"`、`"rx_bytes"` 等）都是编译期已知的固定字符串字面量，生命周期为 `'static`。这样约束既能在编译期杜绝「临时拼接出的 key」误入，又避免了为每次调用分配一个 `String` 来存键名——零开销。

**练习 2**：`serialize()` 为什么只用泛型 `C: Configuration` 而不用 `&dyn Configuration`（trait 对象）？

> **参考答案**：这是本项目的统一风格（见 u2-l1 的「泛型 + 关联类型优于 trait 对象」）。用具体泛型 `C` 让编译期单态化、零成本分发；更重要的是 `Configuration` 的实现类型 `WireGuardConfig<T, B>` 自身带 IO 泛型，若在 trait 对象后面还要擦除这些泛型会非常别扭。对 `serialize()` 而言，泛型版本还方便测试时换上自制的替身类型。

### 4.2 接口级字段序列化

#### 4.2.1 概念说明

接口级字段描述的是「这台 WireGuard 设备本身」的状态，而非某个 peer。UAPI 的 `get` 会先输出它们（如果有值），再输出各 peer。

三个字段都是**可选**的：

| 字段 | 含义 | 没有设置时 |
| --- | --- | --- |
| `private_key` | 设备私钥（32 字节，十六进制 64 位） | 省略该行 |
| `listen_port` | UDP 监听端口 | 省略该行 |
| `fwmark` | 防火墙标记（Linux 的 `SO_MARK`） | 省略该行 |

「省略该行」是 UAPI 的约定：客户端（`wg(8)`）见不到某行，就理解为「该项未设置」。这与 `set` 方向的「用全零表示清除」是对称的两种表达。

#### 4.2.2 核心流程

```
get_private_key() → Option<StaticSecret>   → Some(sk) 则写 private_key = hex(sk.to_bytes())
get_listen_port() → Option<u16>            → Some(p)  则写 listen_port = p
get_fwmark()      → Option<u32>            → Some(m)  则写 fwmark = m
```

三个字段都用 `.map(|x| write(...))` 模式：有值就写、没值就什么都不做。

#### 4.2.3 源码精读

接口级字段序列化（[src/configuration/uapi/get.rs:17-27](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L17-L27)）：

```rust
    // serialize interface
    config
        .get_private_key()
        .map(|sk| write("private_key", hex::encode(sk.to_bytes())));

    config
        .get_listen_port()
        .map(|port| write("listen_port", port.to_string()));

    config
        .get_fwmark()
        .map(|fwmark| write("fwmark", fwmark.to_string()));
```

说明：

- `sk.to_bytes()` 返回 `[u8; 32]`，`hex::encode` 把它变成 64 个小写十六进制字符。`hex` 来自 `Cargo.toml` 的依赖 `hex = "0.4"`。
- `port.to_string()` / `fwmark.to_string()` 把数值转成十进制字符串。
- `.map(|x| write(...))` 返回 `Option<io::Result<()>>`，而这三段都是**语句**（以 `;` 结尾），其值被丢弃——也就是说，**接口级字段的 IO 错误被静默吞掉**。这与 peer 级字段用 `?` 显式传播错误（见 4.3）形成对比。实践中接口级字段很少，且通常写向内存缓冲或健康的 Unix 流，所以这个差异一般不构成问题，但值得知道。

#### 4.2.4 代码实践

**实践目标**：观察「值存在 vs 值缺失」时接口级行的差异。

**操作步骤**：

1. 在 4.1 的替身测试里，先把 `sk_bytes` 设为 `None`、`listen_port` 设为 `None`、`fwmark` 设为 `None`，运行测试，打印 `out`。
2. 再把三者都设上具体值（私钥用某个 `[u8; 32]`、端口用 `51820`、fwmark 用 `0x51820` 之类的数），再次打印 `out`。

**需要观察的现象**：第一次输出里**完全没有** `private_key=` / `listen_port=` / `fwmark=` 这三行；第二次则按顺序出现这三行。

**预期结果**：验证了 `Option::map` 模式正确实现了「无值即省略」。

**待本地验证**：确认私钥行确实输出了 64 个十六进制字符。

#### 4.2.5 小练习与答案

**练习**：`listen_port` 与 `fwmark` 的值都用 `to_string()` 直接转字符串，而 `private_key` 却要先 `to_bytes()` 再 `hex::encode`。为什么私钥要绕这一步？

> **参考答案**：端口和 fwmark 是普通整数，其十进制字面量就是 UAPI 期望的格式；私钥则是一串原始字节，不能直接 `to_string()`（那样会得到无意义的乱码或编译错误）。UAPI 规定私钥/公钥/PSK 这类密钥材料统一用**小写十六进制**传输，所以必须先取字节 `sk.to_bytes()` 再 `hex::encode`。

### 4.3 peer 级字段序列化

#### 4.3.1 概念说明

接口级字段之后，`serialize()` 遍历 `get_peers()` 返回的所有 peer，逐个输出一整组字段。每个 peer 之间的「边界」靠 `public_key` 行隐式标记——`wg(8)` 看到 `public_key=` 就知道这是一个新 peer 的开始。

本模块要重点理解三个细节：

1. **字段顺序**：peer 内部字段的输出顺序由 `serialize()` 写死，与内核 UAPI 规范的顺序**略有不同**（见下）。
2. **`last_handshake_time` 拆成两行**：一个时间值拆成 `_sec` 与 `_nsec` 两行，且仅当确实握过手时才输出。
3. **`allowed_ip` 是一个循环**：一个 peer 可能有任意多条 allowed-ip，每条独占一行，格式 `ip/cidr`。

#### 4.3.2 核心流程

```
peers = get_peers()                      # Vec<PeerState>
while peers 还有 peer p:                  # 注意是从尾部 pop
    写 public_key          = hex(p.public_key.as_bytes())
    写 preshared_key       = hex(p.preshared_key)        # 即使全零也输出
    写 rx_bytes            = p.rx_bytes
    写 tx_bytes            = p.tx_bytes
    写 persistent_keepalive_interval = p.persistent_keepalive_interval
    if let Some((secs, nsecs)) = p.last_handshake_time:
        写 last_handshake_time_sec  = secs
        写 last_handshake_time_nsec = nsecs
    if let Some(endpoint) = p.endpoint:
        写 endpoint        = endpoint.to_string()
    for (ip, cidr) in p.allowed_ips:
        写 allowed_ip      = ip/cidr
```

两个要点：

- **pop 顺序**：`peers.pop()` 从 `Vec` 尾部取，所以 peer 的输出顺序与 `get_peers()` 返回顺序**相反**。而 `get_peers()` 本身是按 DashMap 迭代顺序遍历的，**顺序本就不确定**。因此 peer 在输出中的相对顺序没有意义——客户端按 `public_key` 区分，不依赖顺序。
- **字段顺序差异**：本实现把 `persistent_keepalive_interval` 放在 `last_handshake_time` **之前**，而内核 UAPI 规范里这两者顺序相反。这只影响文本的肉眼阅读，不影响 `wg(8)` 解析（客户端按 key 名读取，不依赖行序）。

#### 4.3.3 源码精读

peer 级字段序列化（[src/configuration/uapi/get.rs:30-53](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L30-L53)）：

```rust
    // serialize all peers
    let mut peers = config.get_peers();
    while let Some(p) = peers.pop() {
        write("public_key", hex::encode(p.public_key.as_bytes()))?;
        write("preshared_key", hex::encode(p.preshared_key))?;
        write("rx_bytes", p.rx_bytes.to_string())?;
        write("tx_bytes", p.tx_bytes.to_string())?;
        write(
            "persistent_keepalive_interval",
            p.persistent_keepalive_interval.to_string(),
        )?;

        if let Some((secs, nsecs)) = p.last_handshake_time {
            write("last_handshake_time_sec", secs.to_string())?;
            write("last_handshake_time_nsec", nsecs.to_string())?;
        }

        if let Some(endpoint) = p.endpoint {
            write("endpoint", endpoint.to_string())?;
        }

        for (ip, cidr) in p.allowed_ips {
            write("allowed_ip", ip.to_string() + "/" + &cidr.to_string())?;
        }
    }
```

逐条说明：

- **`public_key`**（[L32](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L32)）：`p.public_key.as_bytes()` 返回 `&[u8; 32]`，hex 编码成 64 个字符。这是每个 peer 块的「标题行」。
- **`preshared_key`**（[L33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L33)）：PSK 是 `[u8; 32]`。注意：**即使 PSK 全零（即「未设置」）也会输出一行全零的 `preshared_key=`**。这和 `set` 方向「全零表示清除」是一致的——这里的「默认值」就是全零并被当作普通 PSK 对待（见 `PeerState` 字段注释）。
- **`rx_bytes` / `tx_bytes`**（[L34-L35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L34-L35)）：`u64` 字节计数器，直接 `to_string()`。这些值来源于路由器的原子计数器（见 4.4）。
- **`persistent_keepalive_interval`**（[L36-L39](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L36-L39)）：`u64`，0 表示「禁用 keepalive」，但仍然会输出 `persistent_keepalive_interval=0`。
- **`last_handshake_time`**（[L41-L44](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L41-L44)）：这是个 `Option<(u64, u64)>`。**有值时拆成两行** `last_handshake_time_sec` 和 `last_handshake_time_nsec`；**没握过手（`None`）时两行都不输出**。
- **`endpoint`**（[L46-L48](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L46-L48)）：`Option<SocketAddr>`，`to_string()` 形如 `203.0.113.5:51820`。没学到端点（`None`）时不输出。
- **`allowed_ip`**（[L50-L52](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L50-L52)）：`Vec<(IpAddr, u32)>`，每条拼成 `ip.to_string() + "/" + &cidr.to_string()`，例如 `10.0.0.0/24`。这是一个循环，所以一个 peer 可以有 0 到多条 `allowed_ip` 行。

> **为什么 `last_handshake_time` 要拆成两行？**
>
> UAPI 是纯文本、按行的协议，一行只能有一个值。而握手时间需要一个「秒 + 纳秒」的高精度时间戳（`wg show` 显示「latest handshake」时用到纳秒级）。两个数值无法塞进同一个 `key=value`，于是协议把它定义成 `last_handshake_time_sec` 和 `last_handshake_time_nsec` 两个独立 key。客户端把它们配对读出再拼成完整时间。这正是 4.3.1 提到的「一个逻辑值拆两行」。

注意：与接口级字段不同，peer 级字段的 `write(...)` 都带 `?`，**会向上传播 IO 错误**。

#### 4.3.4 代码实践

**实践目标**：验证 peer 级字段的输出格式与「条件输出」行为。

**操作步骤**：

1. 在替身测试的 `PeerState` 里，把 `last_handshake_time` 先设为 `None`、`endpoint` 设为 `None`、`allowed_ips` 设为空 `vec![]`，打印输出。
2. 再把三者都设上值（时间设 `Some((1700000000, 500_000_000))`、端点设一个 `SocketAddr`、allowed-ips 设两条），再次打印输出。

**需要观察的现象**：

- 第一次：`public_key` 之后只有 `preshared_key`、`rx_bytes`、`tx_bytes`、`persistent_keepalive_interval`，**没有**任何 `last_handshake_time_*` / `endpoint` / `allowed_ip` 行。
- 第二次：多出 `last_handshake_time_sec=1700000000`、`last_handshake_time_nsec=500000000`、一行 `endpoint=...`、**两行** `allowed_ip=...`。

**预期结果**：确认 `if let Some` 与 `for ... in` 正确实现了「有则输出、无则省略」与「多条 allowed_ip 多行」。

**待本地验证**：观察 `endpoint` 的 `to_string()` 在 IPv4 与 IPv6 两种 `SocketAddr` 下的不同字面量。

#### 4.3.5 小练习与答案

**练习 1**：如果某 peer 从未握手，`wg show` 还会显示 `rx_bytes` / `tx_bytes` 吗？

> **参考答案**：会。`rx_bytes` / `tx_bytes` 是**无条件**输出的（用 `?` 而非 `if let Some`），未握手时它们就是 `0`。只有 `last_handshake_time` / `endpoint` 是条件输出。

**练习 2**：`allowed_ip` 的拼接用 `ip.to_string() + "/" + &cidr.to_string()`。这里的 `&cidr.to_string()` 为什么要加 `&`？

> **参考答案**：`cidr.to_string()` 产生一个临时的 `String`。`String + &str`（左操作数 `ip.to_string()` 是 `String`，右操作数想加 `"/"` 这种 `&str`）合法；但 `cidr.to_string()` 的结果是 `String`，`String + String` 在 Rust 里**不直接合法**（`+` 的右操作数要求是 `&str`）。加 `&` 把它降级成 `&str`（ deref coercion：`&String → &str`），从而匹配 `Add<&str>` 实现。

### 4.4 序列化的数据来源：`PeerState` 快照与 `get_peers()`

#### 4.4.1 概念说明

`serialize()` 只做「翻译」，真正要吐出去的数据全来自 `config.get_*()` 方法。其中 `get_peers()` 是重头戏：它要从协议核心里那些**分散、带锁、带泛型**的动态状态里，收集出一份**集中、无锁、无泛型**的 `Vec<PeerState>`。

`PeerState` 就是这次「拍照」的产物——一个所有字段都是普通类型（`u64`、`Option`、`Vec`、数组）的结构体，可以安全地脱离协议核心的锁与泛型存在，交给 UAPI 层慢慢序列化。

#### 4.4.2 核心流程

`get_peers()` 的流程（[src/configuration/config.rs:354-383](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L354-L383)）：

```
锁住 config
拿到握手层 peers 表的读锁
对每个 (pk, p) in peers 表:
    把 walltime_last_handshake（SystemTime）换算成 (secs, nanos) since UNIX_EPOCH
    取该 peer 的 PSK
    若 PSK 存在，组装一个 PeerState 并 push:
        preshared_key          ← get_psk(pk)
        endpoint               ← p.get_endpoint()
        rx_bytes / tx_bytes    ← 原子计数器 load(Relaxed)
        persistent_keepalive   ← p.get_keepalive_interval()
        allowed_ips            ← p.list_allowed_ips()
        last_handshake_time    ← 上面的 (secs, nanos)
        public_key             ← pk
返回 Vec<PeerState>
```

关键点：

- **时间换算**：`walltime_last_handshake` 是 `Mutex<Option<SystemTime>>`。换算成「自 Unix 纪元起的秒 + 纳秒」二元组（[L361-L366](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L361-L366)）。若 `SystemTime` 出现在 UNIX_EPOCH **之前**（理论上极罕见），`duration_since` 会失败，此时用 `unwrap_or_else(|_| Duration::from_secs(0))` 兜底为 0。
- **原子计数器**：`rx_bytes` / `tx_bytes` 用 `Ordering::Relaxed` 读取，因为这里只关心「近似当前值」，不需要跨字段的同步顺序。
- **PSK 守卫**：只有 `get_psk(pk)` 返回 `Some` 的 peer 才会被收进快照——这是过滤掉「半成品 peer」的一道闸。

#### 4.4.3 源码精读

`PeerState` 结构体（[src/configuration/config.rs:20-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L20-L29)）：

```rust
pub struct PeerState {
    pub rx_bytes: u64,
    pub tx_bytes: u64,
    pub last_handshake_time: Option<(u64, u64)>,
    pub public_key: PublicKey,
    pub allowed_ips: Vec<(IpAddr, u32)>,
    pub endpoint: Option<SocketAddr>,
    pub persistent_keepalive_interval: u64,
    pub preshared_key: [u8; 32], // 0^32 is the "default value" (though treated like any other psk)
```

注意字段类型与 4.3 的序列化一一对应：`(u64, u64)` 对应拆开的 sec/nsec 两行；`Vec<(IpAddr, u32)>` 对应多条 `allowed_ip=ip/cidr`；`Option<SocketAddr>` 对应条件输出的 `endpoint`。

`get_peers()` 把动态状态聚合成快照（[src/configuration/config.rs:354-383](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L354-L383)），其中时间换算片段：

```rust
let last_handshake_time = (*p.walltime_last_handshake.lock()).map(|t| {
    let duration = t
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_secs(0));
    (duration.as_secs(), duration.subsec_nanos() as u64)
});
```

`walltime_last_handshake` 的定义在 [src/wireguard/peer.rs:29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs#L29)，注释明确写着「walltime for last handshake (for UAPI status)」——也就是说这个字段**专门**为 UAPI `get` 而存在。

`Configuration` trait 暴露的四个 getter（[src/configuration/config.rs:81](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L81)、[L183](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L183)、[L190](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L190)、[L192](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L192)）正是 `serialize()` 唯一调用的方法。trait 的完整签名见 [src/configuration/config.rs:64-193](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L64-L193)。

#### 4.4.4 代码实践

**实践目标**：写一个**不依赖真实网络**的单元测试，用一个自制 `MockConfig` 实现 `Configuration`，驱动 `serialize()` 并断言输出。

**操作步骤**：

1. 在 `src/configuration/uapi/get.rs` 末尾加入下面的测试模块（**示例代码**，可直接编译运行）。
2. 运行 `cargo test --lib serialize`。

```rust
// 示例代码：src/configuration/uapi/get.rs 末尾
#[cfg(test)]
mod tests {
    use super::*;                       // 引入 serialize、write
    use crate::configuration::{Configuration, PeerState, ConfigError};
    use std::cell::RefCell;
    use std::net::{IpAddr, Ipv4Addr, SocketAddr};
    use x25519_dalek::{PublicKey, StaticSecret};

    // 只实现 serialize 真正会调用的 get_* 方法；其余 setter 用 unimplemented!() 占位
    struct MockConfig {
        sk_bytes: Option<[u8; 32]>,
        listen_port: Option<u16>,
        fwmark: Option<u32>,
        // serialize 只调用一次 get_peers，所以用 drain 把 PeerState 移出，免去 Clone
        peers: RefCell<Vec<PeerState>>,
    }

    impl Configuration for MockConfig {
        fn up(&self, _: usize) -> Result<(), ConfigError> { unimplemented!() }
        fn down(&self) { unimplemented!() }
        fn set_private_key(&self, _: Option<StaticSecret>) { unimplemented!() }
        fn get_private_key(&self) -> Option<StaticSecret> { self.sk_bytes.map(StaticSecret::from) }
        fn get_protocol_version(&self) -> usize { unimplemented!() }
        fn set_listen_port(&self, _: u16) -> Result<(), ConfigError> { unimplemented!() }
        fn set_fwmark(&self, _: Option<u32>) -> Result<(), ConfigError> { unimplemented!() }
        fn replace_peers(&self) { unimplemented!() }
        fn remove_peer(&self, _: &PublicKey) { unimplemented!() }
        fn add_peer(&self, _: &PublicKey) -> bool { unimplemented!() }
        fn set_preshared_key(&self, _: &PublicKey, _: [u8; 32]) { unimplemented!() }
        fn set_endpoint(&self, _: &PublicKey, _: SocketAddr) { unimplemented!() }
        fn set_persistent_keepalive_interval(&self, _: &PublicKey, _: u64) { unimplemented!() }
        fn replace_allowed_ips(&self, _: &PublicKey) { unimplemented!() }
        fn add_allowed_ip(&self, _: &PublicKey, _: IpAddr, _: u32) { unimplemented!() }
        fn get_listen_port(&self) -> Option<u16> { self.listen_port }
        fn get_peers(&self) -> Vec<PeerState> { self.peers.borrow_mut().drain(..).collect() }
        fn get_fwmark(&self) -> Option<u32> { self.fwmark }
    }

    #[test]
    fn serialize_dumps_expected_lines() {
        // 构造 1 个 peer，公钥全零 → hex 为 64 个 '0'
        let peer = PeerState {
            rx_bytes: 1234,
            tx_bytes: 5678,
            last_handshake_time: Some((1_700_000_000, 500_000_000)),
            public_key: PublicKey::from([0u8; 32]),
            allowed_ips: vec![
                (IpAddr::V4(Ipv4Addr::new(10, 0, 0, 0)), 24),
                (IpAddr::V4(Ipv4Addr::new(192, 168, 1, 0)), 32),
            ],
            endpoint: Some(SocketAddr::from(([203, 0, 113, 5], 51820))),
            persistent_keepalive_interval: 25,
            preshared_key: [0u8; 32],
        };

        let cfg = MockConfig {
            sk_bytes: Some([0u8; 32]),
            listen_port: Some(51820),
            fwmark: Some(0x51820),
            peers: RefCell::new(vec![peer]),
        };

        let mut buf = Vec::<u8>::new();
        serialize(&mut buf, &cfg).unwrap();
        let out = String::from_utf8(buf).unwrap();

        // 接口级字段
        assert!(out.contains("private_key=0000000000000000000000000000000000000000000000000000000000000000"));
        assert!(out.contains("listen_port=51820"));
        assert!(out.contains(&format!("fwmark={}", 0x51820)));

        // peer 级字段
        assert!(out.contains("public_key=0000000000000000000000000000000000000000000000000000000000000000"));
        assert!(out.contains("rx_bytes=1234"));
        assert!(out.contains("tx_bytes=5678"));
        assert!(out.contains("persistent_keepalive_interval=25"));
        assert!(out.contains("last_handshake_time_sec=1700000000"));
        assert!(out.contains("last_handshake_time_nsec=500000000"));
        assert!(out.contains("endpoint=203.0.113.5:51820"));
        // 两条 allowed_ip 各占一行
        assert!(out.contains("allowed_ip=10.0.0.0/24\n"));
        assert!(out.contains("allowed_ip=192.168.1.0/32\n"));
    }
}
```

**需要观察的现象**：测试一次通过。`out` 完全由 `serialize()` 生成，期间没有任何 socket、TUN、UDP 被创建。

**预期结果**：所有断言成立，证明 `serialize()` 的字段格式、顺序与条件输出都符合预期。

**待本地验证**：`Configuration` trait 的方法签名若与当前 HEAD 有差异（例如未来增删方法），需相应补齐/删除替身里的占位实现。

#### 4.4.5 小练习与答案

**练习 1**：为什么替身里用 `RefCell<Vec<PeerState>>` + `drain(..)` 而不是 `Vec::clone`？

> **参考答案**：`PeerState` 没有派生 `Clone`（其字段虽多为可 `Clone` 类型，但结构体本身未标注 `#[derive(Clone)]`），所以 `Vec<PeerState>::clone` 编译不过。`get_peers(&self)` 只拿到 `&self`，要把 `PeerState` 移出又需要修改内部容器，于是用 `RefCell` 提供内部可变性，`drain(..)` 把元素**移出**而非克隆——正好 `serialize()` 只调用一次 `get_peers()`，移出一次就够。

**练习 2**：`get_peers()` 里读取 `rx_bytes` 用了 `Ordering::Relaxed`。为什么不用 `SeqCst`？

> **参考答案**：这里只需读取一个「近似当前值」用于状态展示，不依赖它与其他变量之间的跨线程先后顺序，也不基于读到的值做任何并发关键决策。`Relaxed` 开销最低、足够正确；用更强的排序只会徒增开销而无收益。

## 5. 综合实践

把本讲的知识串起来，完成下面这个「端到端」的小任务：

**任务**：模拟一次 `wg show <device>` 的状态输出。

1. 复用 4.4.4 的 `MockConfig`，但构造**两个** peer：peer A（全零公钥、有端点、有握手时间、两条 allowed-ip、rx/tx 非零）和 peer B（随机公钥、**未握手**（`last_handshake_time = None`）、**无端点**（`endpoint = None`）、一条 allowed-ip）。
2. 调用 `serialize()` 写入缓冲并转成字符串。
3. 断言：
   - 接口级三行（`private_key` / `listen_port` / `fwmark`）各出现一次。
   - `public_key=` 行恰好出现两次（每 peer 一次）。
   - peer A 的 `last_handshake_time_sec` 与 `endpoint=` 行**存在**；peer B 的这两个 key **不存在**。
   - `allowed_ip=` 行总共出现三次（A 两条 + B 一条）。
4. **进阶**：把缓冲内容按行 `split('\n')` 收集，验证每个 peer 块内的字段**相对顺序**与 4.3.2 描述的一致（例如每个 `public_key` 之后紧跟着 `preshared_key`）。

这个练习把「字段顺序」「条件输出」「多条 allowed_ip」「数据来源」四个要点一次性串起来。完成后，你就完整掌握了 UAPI `get` 方向的状态序列化。

## 6. 本讲小结

- `serialize()` 是一个纯粹的「结构体 → 文本」翻译器，通过统一的 `write(key, value)` 闭包把每个字段写成 `key=value\n`。
- 它只依赖无泛型的 `Configuration` trait 与一个可写字节流，因此可在内存流上做单元测试。
- 接口级字段（`private_key` / `listen_port` / `fwmark`）可选，用 `Option::map` 决定写不写；私钥走 `hex::encode`，端口/fwmark 走十进制。
- peer 级字段按固定顺序输出；`public_key` 标记新 peer 块；`rx/tx_bytes` 无条件输出，`last_handshake_time`（拆成 `_sec`/`_nsec` 两行）、`endpoint` 条件输出，`allowed_ip` 为循环每条一行。
- 这些值来源于 `get_peers()` 聚合出的 `PeerState` 快照——它把带锁带泛型的动态状态拍成一份无锁无泛型的只读拷贝；`last_handshake_time` 即来自 `peer.rs` 里专为 UAPI 存在的 `walltime_last_handshake`。

## 7. 下一步学习建议

- **u7-l4 测试策略与纯软件回归**：本讲的替身测试是「纯软件回归」的一个缩影。u7-l4 会展示项目里更完整的端到端测试（用 `dummy` 平台搭两个互连实例做真实握手+收发），可以把两者对照阅读。
- **u6-l3 UAPI set 配置解析器**：`get` 是「读」，`set` 是「写」。阅读 `set.rs` 的 `LineParser` 后，你会对 UAPI 这一文本协议的两个方向有完整认识。
- **深入数据来源**：若好奇 `rx_bytes` / `tx_bytes` 这些原子计数器在何处自增、`walltime_last_handshake` 在何处更新，可顺着 `src/wireguard/peer.rs` 与 `src/wireguard/router/` 的回调（`Callbacks::send` / `Callbacks::recv`）反向追踪，这会带你回到 u3/u5 的数据面与定时器章节。
