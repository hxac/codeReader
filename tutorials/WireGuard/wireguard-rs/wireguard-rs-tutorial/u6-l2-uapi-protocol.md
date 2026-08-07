# UAPI 协议处理框架

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 WireGuard 的 **UAPI 文本协议**长什么样：它是「按行组织、`key=value`、以空行结束事务」的纯文本协议，只有 `get=1` 与 `set=1` 两大操作。
- 逐段读懂 `configuration::uapi::handle` 的主流程：如何按字节读一行、如何把一行拆成 `(key, value)`、如何根据操作行分用到 `get` 序列化或 `set` 解析。
- 理解 `handle` 在**任何一条操作结束后都会统一回写 `errno=<n>\n\n`**，以及 `ConfigError` 如何映射成数字 errno。
- 看懂 Linux 上 UAPI 的平台绑定 `LinuxUAPI`：用 `UnixListener` 在 `/var/run/wireguard/<接口名>.sock` 上监听，`connect` 就是 `accept`。

本讲承接 [u6-l1 配置抽象](u6-l1-config-interface.md)：那里我们讲了 `Configuration` trait 如何隐藏 IO 泛型；本讲把镜头拉到「文本协议如何进出这个 trait」。

## 2. 前置知识

### 2.1 什么是 UAPI

WireGuard 不像传统 VPN 那样自带命令行配置工具。它的内核模块（以及本仓库这个用户态实现）只暴露一个**文本控制接口**，叫做 **UAPI**（Userspace API）。官方的 `wg(8)` 命令（如 `wg setconf`、`wg show`）并不直接改内核，而是去连接这个接口，把人类的命令翻译成一串文本写进去。

在 Linux 上，这个接口通常是一个 **Unix 域套接字**（Unix domain socket），路径形如 `/var/run/wireguard/wg0.sock`。谁拥有这个 socket，谁就能配置这台 WireGuard 设备。README 也说明了这一点：

> When an interface is running, you may use `wg(8)` to configure it, as well as the usual `ip(8)` and `ifconfig(8)` commands.

### 2.2 文本协议的极简心智模型

你可以把 UAPI 想象成一个「非常刻板的问答机器人」：

1. 客户端（`wg(8)`）连进来，先发**一行操作声明**：`get=1`（我要读状态）或 `set=1`（我要写配置）。
2. 如果是 `set=1`，紧接着发若干行 `key=value`，最后用一个**空行**表示「我说完了」。
3. 服务端（wireguard-rs）处理完，**永远**回写一行 `errno=0`（成功）或 `errno=<错误码>`（失败），再跟一个空行表示「我也说完了」。

整个协议是「行」为单位、用 `\n` 分隔、用空行（连续两个 `\n`）分隔事务。没有二进制、没有长度前缀、没有 JSON。

### 2.3 为什么用文本协议

文本协议的好处是：可以用 `nc`、`socat` 甚至 `cat` 手工调试，`wg(8)` 跨平台实现简单，且与内核模块的行为完全一致（用户态实现必须和内核模块「说一样的话」，否则 `wg(8)` 无法通用）。代价是解析时要逐字节读、要防超长行。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/configuration/uapi/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs) | **本讲主角**。`handle` 函数：读行、拆 key=value、分用 get/set、回写 errno。 |
| [src/configuration/uapi/set.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs) | `set=1` 的逐行解析器 `LineParser`（详细拆解见 [u6-l3](u6-l3-uapi-set-parser.md)，本讲只看它与 `handle` 的衔接）。 |
| [src/configuration/uapi/get.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs) | `get=1` 的序列化器 `serialize`（详细拆解见 [u6-l4](u6-l4-uapi-get-serializer.md)，本讲只看它与 `handle` 的衔接）。 |
| [src/configuration/error.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/error.rs) | `ConfigError` 枚举及其 `errno()` 映射，决定回写的数字错误码。 |
| [src/platform/uapi.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs) | 平台无关的 UAPI trait：`PlatformUAPI` / `BindUAPI`。 |
| [src/platform/linux/uapi.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/uapi.rs) | Linux 实现 `LinuxUAPI`：`UnixListener` 绑定 `/var/run/wireguard/`。 |
| [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) | 在 UAPI 服务线程里 `accept` 连接、每连接 spawn 一线程跑 `handle`。 |

## 4. 核心概念与源码讲解

### 4.1 UAPI 文本协议格式

#### 4.1.1 概念说明

UAPI 协议只有两种操作，由**第一条非空行**声明：

- `get=1`：读取设备当前状态（私钥、监听端口、各 peer 的 rx/tx 字节、上次握手时间、allowed-ips 等）。
- `set=1`：修改配置（设私钥、加/删 peer、设 allowed-ips、设 endpoint 等）。

每条消息（无论请求还是响应）都以**一个空行**作为终止符。也就是说，连续两个 `\n`（`\n\n`）就是「事务边界」。

#### 4.1.2 核心流程

一次完整的 `set` 交互（客户端 → 服务端）：

```text
客户端发送:
  set=1\n
  private_key=<64位十六进制>\n
  public_key=<64位十六进制>\n
  allowed_ip=10.0.0.1/24\n
  \n                         ← 空行：set 请求结束

服务端回写:
  errno=0\n\n                ← errno 行 + 空行：响应结束
```

一次完整的 `get` 交互：

```text
客户端发送:
  get=1\n

服务端回写:
  private_key=<hex>\n        ← 仅当存在该字段时才写
  listen_port=51820\n
  public_key=<hex>\n         ← 每个 peer 一组
  rx_bytes=...\n
  tx_bytes=...\n
  allowed_ip=...\n
  errno=0\n\n                ← 永远以 errno + 空行收尾
```

关键约定可以总结为一个不等式——任意一行（不含 `\n`）的长度都被限制：

\[
\mathrm{len}(\text{line}) \le \text{MAX\_LINE\_LENGTH} = 256
\]

超过即报错（见 4.2）。

#### 4.1.3 源码精读

协议中那一行的最大长度上限定义在 [src/configuration/uapi/mod.rs:11](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L11)：

```rust
const MAX_LINE_LENGTH: usize = 256;
```

这正好容纳 UAPI 里最长的字段（如 32 字节私钥的 64 位十六进制 + `private_key=` 前缀 + 余量）。

`get` 操作的输出格式由序列化器 [src/configuration/uapi/get.rs:5-56](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/get.rs#L5-L56) 逐字段写出，每行都是 `key=value\n`；接口级字段（`private_key`/`listen_port`/`fwmark`）仅在 `Option::is_some()` 时才输出。

`set` 操作的输入由解析器 [src/configuration/uapi/set.rs:55](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L55) 的 `parse_line` 按行处理。本讲只关心 `handle` 如何驱动它们，字段级细节留给 u6-l3 / u6-l4。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把「协议是纯文本」这件事坐实。

1. 打开 `src/configuration/uapi/get.rs`，数一下 `write("xxx", ...)` 调用一共输出哪些 key。
2. 打开 `src/configuration/uapi/set.rs`，在 `parse_line` 的两个 `match` 分支里找出所有被识别的 key（如 `private_key`、`public_key`、`allowed_ip`、`endpoint` 等）。
3. **需要观察的现象**：`get` 输出的 key 集合与 `set` 接受的 key 集合**并不完全相同**——例如 `get` 会输出 `rx_bytes`/`tx_bytes`/`last_handshake_time_sec`（这些是只读统计量），而 `set` 不接受它们。
4. **预期结果**：`get` 多输出「运行态」字段，`set` 只接受「配置态」字段。

#### 4.1.5 小练习与答案

**练习 1**：为什么 UAPI 要用纯文本而不是二进制？
> **参考答案**：便于用 `nc`/`socat` 手工调试、`wg(8)` 跨平台实现简单，且必须与内核模块说同一种语言才能复用官方工具。代价是需要逐字节解析和防超长行。

**练习 2**：`get` 的响应里，如果设备没设置私钥，`private_key=` 这一行会出现吗？
> **参考答案**：不会。`get.rs` 用 `config.get_private_key().map(|sk| write(...))`，`Option::None` 时整行被跳过。

---

### 4.2 handle 主流程：readline / keypair / operation

#### 4.2.1 概念说明

`handle` 是整个 UAPI 协议的**唯一入口**。它的签名极度朴素——只吃一个「能读又能写」的字节流 `S` 和一个配置接口 `C`：

[src/configuration/uapi/mod.rs:13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L13)

```rust
pub fn handle<S: Read + Write, C: Configuration>(stream: &mut S, config: &C)
```

`S: Read + Write` 既负责读客户端请求、又负责写响应——对 `handle` 而言，Unix 套接字、内存流、测试桩都只是一个「双工字节流」。这正是它能在单元测试里用内存流喂入文本的根本原因（见第 5 节综合实践）。

`handle` 内部用了三个嵌套的私有函数来组织逻辑：`operation`（执行一次操作并返回 `Result`）、`readline`（读一行）、`keypair`（拆 `key=value`）。

#### 4.2.2 核心流程

```text
handle(stream, config)
  │
  ├─ res = operation(stream, config)        ← 内层函数，可能返回 Err(ConfigError)
  │     │
  │     ├─ ln = readline(stream)            ← 读「操作行」
  │     │
  │     ├─ match ln:
  │     │     "get=1"  → serialize(stream, config)        ← 直接序列化状态
  │     │     "set=1"  → LineParser 逐行解析，直到空行    ← 循环读行
  │     │     _        → Err(InvalidOperation)
  │
  ├─ 写回 "errno=" + (错误码 或 "0")
  └─ 写回 "\n\n"                            ← 空行终止响应
```

`readline` 的逐字节读循环是这个框架的基石：

[src/configuration/uapi/mod.rs:19-34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L19-L34)

```rust
fn readline<R: Read>(reader: &mut R) -> Result<String, ConfigError> {
    let mut m: [u8; 1] = [0u8];
    let mut l: String = String::with_capacity(MAX_LINE_LENGTH);
    while reader.read_exact(&mut m).is_ok() {
        let c = m[0] as char;
        if c == '\n' {
            log::trace!("UAPI, line: {}", l);
            return Ok(l);          // 返回不含 '\n' 的行内容
        };
        l.push(c);
        if l.len() > MAX_LINE_LENGTH {
            return Err(ConfigError::LineTooLong);   // 超长保护
        }
    }
    Err(ConfigError::IOError)       // 读到 EOF 仍未遇到 '\n'
}
```

要点：

- **逐字节读**（`read_exact(&mut [u8;1])`），遇 `\n` 即止，返回的字符串**不含** `\n`。源码注释 `(why is this not in std?)` 是在吐槽：标准库 `BufRead::read_line` 既不限制长度、又保留尾部 `\n`，都不符合这里的需求。
- **超长保护**：一旦累计长度超过 `MAX_LINE_LENGTH`(256) 立即报 `LineTooLong`，防止恶意/异常客户端用超长行耗尽内存。
- **空行语义**：如果流里就是单独一个 `\n`，循环第一次就命中 `c == '\n'`、`l` 仍为空字符串 `""`，于是 `readline` 返回 `Ok("")`。这正是 `set` 事务用「空行」结束的机制。

`keypair` 把一行拆成 `(key, value)`：

[src/configuration/uapi/mod.rs:37-43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L37-L43)

```rust
fn keypair(ln: &str) -> Result<(&str, &str), ConfigError> {
    let mut split = ln.splitn(2, '=');
    match (split.next(), split.next()) {
        (Some(key), Some(value)) => Ok((key, value)),
        _ => Err(ConfigError::LineTooLong),   // 没有 '=' 时
    }
}
```

> ⚠️ **小坑（代码阅读要点）**：当一行里**没有 `=`** 时，`splitn(2,'=')` 只产出一个段，于是 `keypair` 返回 `Err(LineTooLong)`。这里的错误变体名是 **`LineTooLong`，但语义其实是「格式不对、缺少 `=`」**，属于历史遗留的命名复用。阅读时不要被名字误导。

#### 4.2.3 源码精读

操作行的分用逻辑在 [src/configuration/uapi/mod.rs:46-65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L46-L65)：

```rust
// read operation line
match readline(stream)?.as_str() {
    "get=1" => {
        log::debug!("UAPI, Get operation");
        serialize(stream, config).map_err(|_| ConfigError::IOError)
    }
    "set=1" => {
        log::debug!("UAPI, Set operation");
        let mut parser = LineParser::new(config);
        loop {
            let ln = readline(stream)?;
            if ln == "" { break; }              // 空行结束 set 请求
            let (k, v) = keypair(ln.as_str())?;
            parser.parse_line(k, v)?;
        }
        parser.parse_line("", "")              // 用空 key 触发最后一个 peer 的 flush
    }
    _ => Err(ConfigError::InvalidOperation),
}
```

读这段时注意三个细节：

1. `get` 分支**不循环**：读完操作行后直接调 `serialize` 把状态一次性吐出。
2. `set` 分支**循环读行直到空行**，每行交给 `LineParser::parse_line`。
3. 循环退出后还有一句 `parser.parse_line("", "")`：它用「空 key」通知解析器「事务结束」，触发最后一个待提交 peer 的 `flush_peer`（见 set.rs 第 248-252 行的 `"" => flush_peer(...)` 分支）。少了这一句，最后一段 peer 配置会丢失。

#### 4.2.4 代码实践（源码阅读型）

**目标**：追踪一次 `set` 调用在 `handle` 内部的控制流。

1. 假设客户端发来 `set=1\npublic_key=<hex>\nallowed_ip=10.0.0.1/24\n\n`。
2. 在纸上逐步标注：第 1 次 `readline` 返回什么？进入哪个 `match` 分支？循环里 `readline` 几次、每次返回什么？哪一行触发 `break`？最后 `parse_line("","")` 干了什么？
3. **需要观察的现象**：`public_key` 这一行会让 `LineParser` 的状态从 `Interface` 切到 `Peer`（见 set.rs 的 `"public_key" => self.state = Self::new_peer(value)`）。
4. **预期结果**：循环体执行 2 次（读 `public_key=...` 和 `allowed_ip=...`），第 3 次读到空行 `""` 触发 `break`，随后 `parse_line("","")` 把这个 peer 真正提交给 `config`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `readline` 要逐字节读，而不是用 `BufReader::read_line`？
> **参考答案**：标准库的 `read_line` 不限制行长（可能被超长行耗尽内存）且保留尾部 `\n`。这里需要「带最大长度保护、不含 `\n`」的行，所以自己实现。

**练习 2**：`handle` 的参数为什么是 `&mut S` 而不是 `(reader, writer)` 两个参数？
> **参考答案**：因为底层连接（Unix 套接字、内存流）是「同一对象既能读又能写」的双工流，用单个 `S: Read + Write` 更贴切，也方便在测试里用一个内存双工对象同时扮演两端。

---

### 4.3 get=1 与 set=1 的分用：set 的事务模型

#### 4.3.1 概念说明

`get` 与 `set` 的处理结构**不对称**：

- `get` 是**无状态的单次查询**：调用 `serialize(stream, config)`，把 `Configuration` trait 暴露的状态（私钥、端口、各 peer 快照）原样序列化成文本。它不需要「记着上一行」。
- `set` 是**有状态的事务**：多行配置可能共同描述同一个 peer（先 `public_key=`，再若干 `allowed_ip=`），所以需要一个 `LineParser` 维护「当前正在配置哪个 peer」的状态机，直到事务结束（空行）才统一提交（`flush_peer`）。

`LineParser` 把对 `Configuration` 的多次小修改**攒成一批**，是因为 UAPI 协议里一个 peer 的属性是分散在多行的，必须等收齐再下发。

#### 4.3.2 核心流程

`set` 事务的状态机（简化）：

```text
            ┌──────────────┐
  开始 ───► │  Interface   │  ← 解析 private_key / listen_port / fwmark / replace_peers
            └──────┬───────┘
                   │ 遇到 "public_key=<hex>"
                   ▼
            ┌──────────────┐
            │  Peer(A)     │  ← 解析 allowed_ip / endpoint / preshared_key / ...
            └──────┬───────┘
                   │ 又遇到 "public_key=<hex>" → flush_peer(A)，切到 Peer(B)
                   ▼
            ┌──────────────┐
            │  Peer(B)     │
            └──────┬───────┘
                   │ 空行 / parse_line("","")  → flush_peer(B)
                   ▼
                 提交完成
```

`flush_peer`（set.rs 第 64-104 行）负责把一个 `ParsedPeer` 真正下发：若 `remove` 则删 peer，否则按需 `add_peer`、加 `allowed_ip`、设 `preshared_key`、设 `endpoint` 等。

#### 4.3.3 源码精读

`set` 主循环已在 4.2.3 引用（mod.rs 第 51-63 行）。提交逻辑在 [src/configuration/uapi/set.rs:64-104](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L64-L104)，节选关键开头：

```rust
fn flush_peer<C: Configuration>(config: &C, peer: &ParsedPeer) -> Option<ConfigError> {
    if peer.remove {
        config.remove_peer(&peer.public_key);
        return None;
    }
    if !peer.update_only {
        config.add_peer(&peer.public_key);     // 新 peer 先 add
    }
    for (ip, cidr) in &peer.allowed_ips {
        config.add_allowed_ip(&peer.public_key, *ip, *cidr);
    }
    // ... preshared_key / keepalive / endpoint ...
    None
}
```

注意 `flush_peer` 返回的是 `Option<ConfigError>`（`Some` 表示出错的那个错误，`None` 表示成功），只有 `protocol_version` 校验这条路径会用 `Some(...)` 上报错误。这些 `Configuration` trait 方法（`add_peer`/`add_allowed_ip`/`set_endpoint`…）的内部实现正是 u6-l1 讲的 `WireGuardConfig`。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解「一个 peer 的属性为什么必须攒着一起提交」。

1. 读 set.rs 中 `ParserState::Peer` 分支，确认：在一个 peer 的若干 `allowed_ip=` 行之间，**没有任何**对 `config.add_allowed_ip` 的直接调用。
2. 追到 `flush_peer`，确认所有 `add_allowed_ip` 都集中在提交时一次性调用。
3. **需要观察的现象**：如果在每读一行时就立即下发，那么「peer 尚未 add，却先 add_allowed_ip」会出错。攒批避免了这种乱序。
4. **预期结果**：`flush_peer` 先 `add_peer`（若非 `update_only`），再加 allowed-ips，顺序固定，保证 peer 先存在。

#### 4.3.5 小练习与答案

**练习**：`update_only=true` 的 peer 在 `flush_peer` 时会有什么不同？
> **参考答案**：跳过 `config.add_peer`（即「只更新已存在的 peer，不存在也不新建」），其余字段照常下发。用于只改属性而不新增 peer 的场景。

---

### 4.4 errno 统一回写与错误码映射

#### 4.4.1 概念说明

无论 `get`/`set` 成功还是失败，`handle` 在结束时**必定**向流里写：

```text
errno=<n>\n\n
```

成功时 `<n>` 是 `0`，失败时是 `ConfigError::errno()` 返回的数字。这是一种「统一收尾」设计：客户端永远可以在响应末尾找到一行 `errno=...` 来判定成败，不需要根据有没有数据来判断。

> 设计动机：把「业务结果」与「协议收尾」解耦。`operation` 内层函数用 `Result<(), ConfigError>` 表达业务成败，外层 `handle` 只负责把它翻译成协议规定的文本尾巴。

#### 4.4.2 核心流程

```text
let res = operation(stream, config);     // Result<(), ConfigError>

stream.write("errno=".as_ref());
stream.write( match res {
    Err(e) => e.errno().to_string(),     // 失败：错误码（如 "22"）
    Ok(()) => "0".to_owned(),            // 成功："0"
}.as_ref() );
stream.write("\n\n".as_ref());           // errno 行 + 空行
```

注意三处 `write` 的返回值都被 `let _ =` 丢弃——即便回写本身失败（比如客户端已断开），`handle` 也不报错，因为一次 UAPI 事务到此就结束了。

#### 4.4.3 源码精读

errno 回写代码在 [src/configuration/uapi/mod.rs:68-82](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs#L68-L82)：

```rust
// process operation
let res = operation(stream, config);
log::debug!("UAPI, Result of operation: {:?}", res);

// return errno
let _ = stream.write("errno=".as_ref());
let _ = stream.write(
    match res {
        Err(e) => e.errno().to_string(),
        Ok(()) => "0".to_owned(),
    }
    .as_ref(),
);
let _ = stream.write("\n\n".as_ref());
```

数字错误码由 `ConfigError::errno()` 给出，定义在 [src/configuration/error.rs:42-66](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/error.rs#L42-L66)，按类别映射到标准 libc errno（注意源码里有一句 `// TODO: obtain the correct errno values`，表示这些映射尚未最终校准）：

| `ConfigError` 变体 | errno（Linux libc 常量） | 数值 | 含义 |
|---|---|---|---|
| `FailedToBind` | `EPERM` | 1 | 绑定失败（权限不足） |
| `InvalidHexValue`/`InvalidPortNumber`/`InvalidFwmark`/`InvalidSocketAddr`/`InvalidKeepaliveInterval`/`InvalidAllowedIp`/`InvalidOperation`/`UnsupportedValue` | `EINVAL` | 22 | 值解析失败 |
| `LineTooLong`/`InvalidKey`/`UnsupportedProtocolVersion` | `EPROTO` | 71 | 协议格式错误 |
| `IOError` | `EIO` | 5 | IO 错误 |

`errno()` 方法带有 `#[cfg(unix)]`，只在 Unix 平台编译（因为依赖 `libc::EPERM` 等常量）。

> 注：源码里有一句 `// TODO: obtain the correct errno values`（error.rs 第 43 行），说明这些 errno 数值尚未和内核实现完全对齐，阅读时心里有数即可。

#### 4.4.4 代码实践（源码阅读型）

**目标**：把「业务错误」到「协议错误码」的链路走一遍。

1. 构造一个非法输入：`set=1\nlisten_port=abc\n\n`（端口不是数字）。
2. 追踪：`readline` 返回 `"listen_port=abc"` → `keypair` → `parse_line` 的 `"listen_port"` 分支 → `value.parse::<u16>()` 失败 → `Err(ConfigError::InvalidPortNumber)`。
3. 该错误一路 `?` 上抛到 `operation`，再到 `handle`，经 `errno()` 映射成 `EINVAL`(22)。
4. **需要观察的现象**：客户端最终收到 `errno=22\n\n`。
5. **预期结果**：非法端口号 → `errno=22`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `handle` 末尾三处 `write` 都用 `let _ =` 忽略返回值？
> **参考答案**：因为这是事务的最后一步，即使回写失败（客户端已断开）也无法挽回，且 `handle` 本身返回 `()`，没有渠道上报错误，故忽略。

**练习 2**：如果 `operation` 返回 `Ok(())`，客户端会收到什么尾巴？
> **参考答案**：`errno=0\n\n`。

---

### 4.5 平台绑定 LinuxUAPI：UnixListener 与 /var/run/wireguard

#### 4.5.1 概念说明

`handle` 只跟「双工字节流」打交道，至于这个流是 Unix 套接字还是别的，由**平台层**决定。平台抽象用两个 trait 描述：

- `PlatformUAPI`：`bind(name)` → 创建一个**监听器**（服务端）。
- `BindUAPI`：`connect()` → 从监听器接受一条**连接**，返回一个 `Read + Write` 流。

在 Linux 上，监听器是 `UnixListener`，流是 `UnixStream`，套接字文件放在 `/var/run/wireguard/<接口名>.sock`。

#### 4.5.2 核心流程

```text
main 启动:
  uapi = plt::UAPI::bind("wg0")
    → UnixListener::bind("/var/run/wireguard/wg0.sock")
       （先 create_dir_all，再 remove_file 清掉旧 socket）

UAPI 服务线程（常驻循环）:
  loop {
      stream = uapi.connect()       → UnixListener::accept()  → UnixStream
      thread::spawn → handle(&mut stream, &cfg)   ← 每连接一线程
  }
```

`connect` 这个名字有点反直觉：它不是「主动连出去」，而是「接受一个连进来的连接」（`accept`）。命名是从「服务端视角拿到一条可用连接」来理解的。

#### 4.5.3 源码精读

平台无关 trait 在 [src/platform/uapi.rs:4-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs#L4-L16)：

```rust
pub trait BindUAPI {
    type Stream: Read + Write;        // ← handle 需要的「双工流」就是它
    type Error: Error;
    fn connect(&self) -> Result<Self::Stream, Self::Error>;
}

pub trait PlatformUAPI {
    type Error: Error;
    type Bind: BindUAPI;
    fn bind(name: &str) -> Result<Self::Bind, Self::Error>;
}
```

注意 `BindUAPI::Stream: Read + Write` 这条约束——它正是 `handle<S: Read + Write, ...>` 里 `S` 的来源，把平台层与协议层严丝合缝地对接。

Linux 实现在 [src/platform/linux/uapi.rs:1-31](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/uapi.rs#L1-L31)：

```rust
const SOCK_DIR: &str = "/var/run/wireguard/";

impl PlatformUAPI for LinuxUAPI {
    type Error = io::Error;
    type Bind = UnixListener;

    fn bind(name: &str) -> Result<UnixListener, io::Error> {
        let socket_path = format!("{}{}.sock", SOCK_DIR, name);
        let _ = fs::create_dir_all(SOCK_DIR);     // 确保目录存在
        let _ = fs::remove_file(&socket_path);    // 清掉残留的旧 socket 文件
        UnixListener::bind(socket_path)
    }
}

impl BindUAPI for UnixListener {
    type Stream = UnixStream;
    fn connect(&self) -> Result<UnixStream, io::Error> {
        let (stream, _) = self.accept()?;         // accept 一条连接
        Ok(stream)
    }
}
```

两个 `let _ =` 同样忽略错误：目录已存在、旧 socket 不存在都是正常情况，不必区分。

`main.rs` 的 UAPI 服务线程在 [src/main.rs:164-180](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L164-L180)，每接受一条连接就 `thread::spawn` 一个新线程跑 `handle`——所以**多个 `wg(8)` 进程可以并发配置同一台设备**，每条连接独立处理、互不阻塞。

#### 4.5.4 代码实践（源码阅读型）

**目标**：把「平台绑定」与「协议入口」串起来。

1. 读 `src/platform/mod.rs` 第 15-16 行，确认 `pub use linux as plt;`（Linux 下 `plt` 别名指向 `linux` 模块）。
2. 读 `src/main.rs` 第 86 行 `plt::UAPI::bind(...)`，确认 `plt::UAPI` 就是 `LinuxUAPI`。
3. 顺着 `uapi.connect()` → `UnixListener::accept` → `UnixStream` → `handle(&mut stream, &cfg)` 走完整条链。
4. **需要观察的现象**：平台层的 `UnixStream`（`Read + Write`）就是 `handle` 泛型 `S` 的具体填充。
5. **预期结果**：理解「`handle` 与具体平台解耦，靠 `S: Read + Write` 这一个约束」。

#### 4.5.5 小练习与答案

**练习 1**：`BindUAPI::connect` 为什么内部用的是 `accept`？
> **参考答案**：UAPI 是服务端，等待 `wg(8)` 主动连入；`connect` 在这里是「服务端接受一条连接、获得可读写流」的语义，而非客户端的「主动拨号」。

**练习 2**：`bind` 时为什么要先 `remove_file(&socket_path)`？
> **参考答案**：上一次进程异常退出会残留 socket 文件，`UnixListener::bind` 遇到已存在的路径会失败，所以先清理再绑定。

---

## 5. 综合实践

**目标**：不依赖真实操作系统与 root，用**内存流**把一段 `set` 文本喂给 `handle`，并捕获它回写的 `errno`，断言为 `0`。这是验证「UAPI 协议处理框架」最直接的方法，也复现了项目自身用 dummy 平台做端到端测试的思路（参见 [u7-l4 测试策略](u7-l4-testing-strategy.md)）。

### 5.1 思路

`handle<S: Read + Write, C: Configuration>` 需要两样东西：

1. 一个**既可读又可写**的内存双工流：读端提供 `set` 文本，写端捕获 `errno` 响应。
2. 一个实现了 `Configuration` 的对象：用 `WireGuardConfig` 包装一个基于 dummy 平台的 `WireGuard` 设备。

### 5.2 操作步骤（示例代码）

> ⚠️ 本测试必须放在 crate **内部**（例如 `src/configuration/uapi/mod.rs` 的 `#[cfg(test)]` 子模块），因为 `handle`、`WireGuardConfig`、`dummy` 平台（`pub mod dummy` 被 `#[cfg(test)]` 限定，见 `src/platform/mod.rs` 第 12-13 行）都不是对外公开的。以下为**示例代码**，未实际在本环境运行。

```rust
// 示例代码：放在 src/configuration/uapi/mod.rs 的 #[cfg(test)] mod tests 内
use std::io::{self, Read, Write};

use crate::platform::dummy;
use crate::wireguard::WireGuard;
use super::handle;
use crate::configuration::WireGuardConfig;

// 一个极简的内存双工流：读端从 input 读，写端累积到 output
struct MemStream {
    input: io::Cursor<Vec<u8>>,
    output: Vec<u8>,
}
impl MemStream {
    fn new(input: Vec<u8>) -> Self {
        MemStream { input: io::Cursor::new(input), output: Vec::new() }
    }
}
impl Read for MemStream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> { self.input.read(buf) }
}
impl Write for MemStream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.output.extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> { Ok(()) }
}

#[test]
fn uapi_set_returns_errno_zero() {
    // (1) 用 dummy 平台搭一个 WireGuard 设备（与 test_pure_wireguard 同款装配）
    let (_, tun_reader, tun_writer, _) = dummy::TunTest::create(true);
    let wg: WireGuard<dummy::TunTest, dummy::PairBind> = WireGuard::new(tun_writer);
    wg.add_tun_reader(tun_reader);
    let cfg = WireGuardConfig::new(wg);

    // (2) 构造一段合法的 set 文本：
    //     private_key(64 hex) + public_key(64 hex, 进入 Peer 状态) + allowed_ip，空行收尾
    let pk = "a".repeat(64);   // 任意非全零的 64 位十六进制（示例占位密钥）
    let request = format!(
        "set=1\nprivate_key={pk}\npublic_key={pk}\nallowed_ip=10.0.0.1/24\n\n"
    );

    // (3) 喂给 handle
    let mut stream = MemStream::new(request.into_bytes());
    handle(&mut stream, &cfg);

    // (4) 断言回写的 errno 为 0
    let resp = String::from_utf8(stream.output).unwrap();
    assert!(resp.ends_with("errno=0\n\n"), "unexpected response: {resp}");
}
```

### 5.3 需要观察的现象与预期结果

- **预期结果**：`stream.output` 末尾为 `errno=0\n\n`。
- **为什么是 0**：这段 `set` 文本语法合法——`private_key` 是有效十六进制且非全零（不会被当作「清除私钥」）；`public_key` 让解析器进入 `Peer` 状态；`allowed_ip=10.0.0.1/24` 合法；末尾空行触发 `flush_peer`，依次 `add_peer` + `add_allowed_ip`。这些 `Configuration` 方法在本设备上都不会失败（无需 `up`，因为没有涉及 `listen_port` 绑定 UDP），故 `operation` 返回 `Ok(())`，回写 `errno=0`。
- **可改写的对照实验**：把 `allowed_ip=10.0.0.1/24` 改成 `listen_port=abc`，预期响应变成 `errno=22\n\n`（`InvalidPortNumber` → `EINVAL`）。
- **若运行不通过**：请核对 `dummy` 模块与 `WireGuardConfig` 的可见性（测试须在 crate 内）、以及 `private_key` 是否为 64 位十六进制。该断言结果为**待本地验证**。

## 6. 本讲小结

- UAPI 是一个**纯文本、按行、`key=value`、以空行结束事务**的控制协议，只有 `get=1` 与 `set=1` 两大操作，客户端是官方 `wg(8)`。
- `handle<S: Read + Write, C: Configuration>` 是协议唯一入口：用 `readline` 逐字节读行（带 256 字节超长保护），用 `keypair` 按 `=` 拆键值，按操作行分用。
- `get` 是无状态单次查询（直接 `serialize`）；`set` 是有状态事务（`LineParser` 攒批，空行结束，`parse_line("","")` 触发最后一个 peer 的 `flush_peer`）。
- 无论成败，`handle` 末尾**统一回写** `errno=<n>\n\n`；`ConfigError::errno()` 把业务错误映射成 libc 数字（成功为 0）。
- 平台层用 `PlatformUAPI`/`BindUAPI` 两个 trait 把「双工流」抽象出来；Linux 上是 `/var/run/wireguard/<name>.sock` 的 `UnixListener`，`connect` 即 `accept`，`main.rs` 每连接 spawn 一线程跑 `handle`。
- `handle` 之所以能用内存流测试，关键在于 `S: Read + Write` 这一约束与平台层 `BindUAPI::Stream: Read + Write` 完全对齐——协议层与平台层解耦。

## 7. 下一步学习建议

- **[u6-l3 UAPI set 配置解析器](u6-l3-uapi-set-parser.md)**：深入 `LineParser` 的 `Interface`/`Peer` 状态机与 `flush_peer` 的逐字段解析。
- **[u6-l4 UAPI get 状态序列化](u6-l4-uapi-get-serializer.md)**：深入 `serialize` 如何把 `PeerState` 快照逐行写出。
- **[u7-l4 测试策略与纯软件回归](u7-l4-testing-strategy.md)**：看 `test_pure_wireguard` 如何用 dummy 平台把两个 WG 实例背靠背对接，做端到端握手与收发回归。
