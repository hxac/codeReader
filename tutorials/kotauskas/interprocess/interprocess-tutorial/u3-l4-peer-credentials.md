# 对端凭据 PeerCreds

## 1. 本讲目标

本讲聚焦 local socket 连接的「身份信息」。学完后你应当能够：

- 说清 `PeerCreds` 是什么、为什么它的每个字段都返回 `Option`。
- 对照四类平台（Windows、`ucred` 系、`xucred` 系、NetBSD）说出 `pid`/`euid`/`egid`/`groups` 各自的可用性。
- 解释 `stream.peer_creds()` 在「连接/监听/bind 时刻取值」的快照语义。
- 识别用 PID 做访问控制的竞态风险（TOCTOU），并知道如何正确使用凭据。

本讲承接 [u3-l3](u3-l3-stream-read-write-split.md)：在那里我们已见过 `StreamCommon` 这个 trait，并提过 `peer_creds()` 留待本讲详讲。现在补上这块拼图。

## 2. 前置知识

在进入源码前，先用通俗语言把几个基础概念过一遍。

- **进程与 PID**：操作系统里每个正在运行的程序就是一个进程，操作系统给它分配一个整数编号，即 PID（process ID）。进程退出后，这个编号可能被回收、日后分配给别的进程——这就是后面要讲的「PID 复用」竞态的根源。
- **用户与 UID/GID**：Unix 系统中每个用户有一个数字编号 UID（user ID），每个用户组有一个 GID（group ID）。一个进程也有自己的 UID/GID。
- **有效 UID/GID（effective, euid/egid）**：进程做权限检查（比如能不能读某文件）时，内核看的是「有效」身份而非「真实」身份。setuid 程序运行时真实身份不变、有效身份变成文件属主。所以鉴权场景里看 `euid`/`egid` 更有意义。
- **附加组（supplementary groups）**：用户除了主组之外还可以属于多个附加组，这些组成一个列表。
- **local socket 与 `StreamCommon`**：local socket 是 interprocess 在 named pipe（Windows）/ Unix domain socket（Unix）之上的统一抽象（见 [u2-l1](u2-l1-local-socket-philosophy.md)）；`StreamCommon` 是同步流与异步流共有的 trait（见 [u3-l3](u3-l3-stream-read-write-split.md)），`peer_creds()` 就定义在这里。
- **「壳/芯」注入模式**：公共类型不直接做系统调用，而是用 `impmod!` 宏把平台后端的实现类型以统一别名注入进来（见 [u2-l3](u2-l3-impmod-backend-injection.md)）。`PeerCreds` 也是这个套路。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| `src/local_socket/peer_creds.rs` | 公共 `PeerCreds` 外壳类型与四个 getter 方法 |
| `src/local_socket/stream/trait.rs` | `StreamCommon` trait，定义 `peer_creds()` 接口与取值时刻语义 |
| `src/os/unix/local_socket/peer_creds.rs` | Unix 后端：用 `getsockopt` 取 `ucred`/`xucred`/`unpcbid` |
| `src/os/windows/local_socket/peer_creds.rs` | Windows 后端：仅持有 `pid: u32` |
| `src/os/unix/uds_local_socket/stream.rs` | Unix 流上 `peer_creds()` 的落地（调用 `for_socket`） |
| `src/os/windows/named_pipe/local_socket/stream.rs` | Windows 流上 `peer_creds()` 的落地（调用 `peer_process_id`） |
| `tests/local_socket/stream.rs` | 参考测试 `check_peer_creds`，展示标准用法 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：`PeerCreds` 容器本身、获取它的入口 `StreamCommon::peer_creds`、以及安全使用它时的竞态风险。

### 4.1 PeerCreds：跨平台的对端凭据容器

#### 4.1.1 概念说明

`PeerCreds` 是「连接对端（the other side）的身份凭据」的容器。它的设计灵感来自标准库的 [`std::fs::Metadata`](https://doc.rust-lang.org/std/fs/struct.Metadata.html)：一个小巧、`Copy`、按值传递的只读结构体，装着「关于对端的一些元数据」。文档注释里明确写了这一点：

[src/local_socket/peer_creds.rs:9-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/peer_creds.rs#L9-L16) —— 说明本类型灵感来自 `Metadata`，并警告用于安全决策可能存在竞态。

它对外暴露四个 getter：

| 方法 | 含义 |
|---|---|
| `pid()` | 对端进程号 |
| `euid()` | 对端有效用户 ID |
| `egid()` | 对端有效组 ID |
| `groups()` | 对端附加组 ID 列表（返回切片引用） |

**关键设计：每个 getter 都返回 `Option`。** 原因是这些字段在不同平台上「有的有、有的没有」。与其为每个平台设计不同的结构体，interprocess 选择保留统一的方法签名，用 `Some`/`None` 表达「这个平台提不提供」。

更细致地看，平台差异有**两层**：

1. **Windows vs Unix —— 编译期差异**：`euid()`、`egid()`、`groups()` 这三个方法带 `#[cfg(any(doc, unix))]`，在 Windows 上**根本不存在**（编译期就被排除）。只有 `pid()` 是无条件存在的。
2. **Unix 内部（ucred / xucred / NetBSD）—— 运行期差异**：在 Unix 上这些方法都存在，但具体某个平台可能返回 `None`。比如 `groups()` 只在 `xucred` 系平台返回 `Some`。

也就是说，「方法是否存在」由 `cfg` 在编译期决定，「返回值有没有」由后端在运行期决定。`Option` 主要服务于第二层。

#### 4.1.2 核心流程

`PeerCreds` 是一个 newtype 外壳，内部包着一个平台相关的 `Inner` 类型：

```
公共 PeerCreds  ──包着──▶  Inner（由 impmod! 注入的平台 PeerCreds）
                              │
            ┌─────────────────┼──────────────────┐
            ▼                 ▼                  ▼
       Unix 后端          Windows 后端        （其它未来后端）
   PeerCreds(ucred…)    PeerCreds { pid: u32 }
```

四个 getter 的可用性矩阵（以**后端实际实现**为准，`✓`=返回 `Some`，`✗`=返回 `None`，`—`=方法在编译期不存在）：

| 平台类别 | 代表系统 | `pid()` | `euid()` | `egid()` | `groups()` |
|---|---|---|---|---|---|
| Windows | Windows | ✓ | — | — | — |
| `ucred` 系 | Linux/Android、OpenBSD、Fuchsia、Redox | ✓ | ✓ | ✓ | ✗ |
| `xucred` 系（FreeBSD） | FreeBSD | ✓ | ✓ | ✗ | ✓ |
| `xucred` 系（Darwin/DragonFly） | macOS/iOS/…、DragonFly | ✗ | ✓ | ✗ | ✓ |
| NetBSD | NetBSD | ✓ | ✓ | ✓ | ✗ |

> 说明：公共 rustdoc 的「Available on」清单（见下方源码）措辞比上表保守——例如它把 `pid()` 列为「Windows、ucred 系、FreeBSD」，未提 NetBSD，但后端代码确实为 NetBSD 返回 `Some`。以代码实现为准即可。

#### 4.1.3 源码精读

公共外壳用 `impmod!` 把平台后端的 `PeerCreds` 以别名 `Inner` 注入，并引入 `Pid` 类型别名：

[src/local_socket/peer_creds.rs:3-7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/peer_creds.rs#L3-L7) —— `impmod!` 注入后端的 `PeerCreds as Inner` 与 `Pid`。

外壳本体极其简单，是个带 `#[derive(Copy, Clone)]` 的元组结构体：

[src/local_socket/peer_creds.rs:33-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/peer_creds.rs#L33-L42) —— `pub struct PeerCreds(Inner)`，实现 `Debug`（转发给内部）与 `From<Inner>`（让后端能把构造好的平台类型包回公共外壳）。

四个 getter 都只是把调用转发给 `self.0`（内部后端类型），并各自标注了平台可用性。注意 `pid()` 没有 `#[cfg]`（永远存在），而另外三个带 `#[cfg(any(doc, unix))]`：

[src/local_socket/peer_creds.rs:54-93](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/peer_creds.rs#L54-L93) —— `pid()` 永远可用；`euid`/`egid`/`groups` 仅 Unix（或文档构建）存在，`doc_cfg` feature 还会在文档里标出 `cfg(unix)`。

文件末尾对 `uid_t`/`gid_t` 做了平台别名处理：

[src/local_socket/peer_creds.rs:96-100](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/peer_creds.rs#L96-L100) —— Unix 用 `libc::{gid_t, uid_t}`；非 Unix（Windows）把它们别名成 `u32`，仅为让文件能通过解析（方法本身已被 `cfg` 排除）。

再看两个后端。**Windows 后端**只持有一个 `pid` 字段，因此也只实现 `pid()`：

[src/os/windows/local_socket/peer_creds.rs:1-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/peer_creds.rs#L1-L10) —— `pub struct PeerCreds { pub(crate) pid: u32 }`，`pid()` 恒返回 `Some`，`pub type Pid = u32`。

**Unix 后端**才是字段差异的来源。它内部 `Inner` 是 libc 的凭据结构体，按 `target_os` 选取不同的类型与 socket 选项：

[src/os/unix/local_socket/peer_creds.rs:137-165](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/peer_creds.rs#L137-L165) —— 选项级别/名称与 `Inner` 类型的映射：Linux 系用 `SOL_SOCKET`/`SO_PEERCRED`/`ucred`；OpenBSD 用 `sockpeercred`；FreeBSD/Darwin/DragonFly 用 `SOL_LOCAL`/`LOCAL_PEERCRED`/`xucred`；NetBSD 用 `LOCAL_PEEREID`/`unpcbid`。

每个 getter 内部用一连串互斥的 `#[cfg(...)]` 分支返回对应字段，最后以 `#[allow(unreachable_code)] None` 兜底（因为同一编译只会命中一个分支，其余分支对编译器而言「不可达」）：

[src/os/unix/local_socket/peer_creds.rs:67-134](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/peer_creds.rs#L67-L134) —— `pid`/`euid`/`egid`/`groups` 各自按平台返回字段或 `None`。注意 FreeBSD 的 `cr_pid` 要走 union（`cr_pid__c_anonymous_union.cr_pid`），`groups` 切片长度由 `cr_ngroups` 决定。

`Pid` 在 Unix 上是 `pid_t`（有符号 `i32`），与 Windows 的 `u32` 不同——这就是测试里出现 `pid as u32` 配 `#[allow(clippy::cast_sign_loss)]` 的原因：

[src/os/unix/local_socket/peer_creds.rs:6](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/peer_creds.rs#L6) —— `pub type Pid = pid_t;`

#### 4.1.4 代码实践

**实践目标**：亲手取到一条连接的对端凭据，观察自己平台上哪些字段是 `Some`。

**操作步骤**（接续 [u1-l4](u1-l4-first-local-socket-example.md) 的回显示例，在服务端 `accept` 之后插入几行）：

```rust
// 示例代码：在 accept 得到 conn (Stream) 之后
let creds = conn.peer_creds().expect("peer_creds");
if let Some(pid) = creds.pid() {
    println!("对端 pid = {pid}");
}
#[cfg(unix)]
{
    if let Some(euid) = creds.euid() { println!("对端 euid = {euid}"); }
    if let Some(egid) = creds.egid() { println!("对端 egid = {egid}"); }
    if let Some(groups) = creds.groups() {
        println!("对端附加组 = {:?}", groups);
    }
}
```

**需要观察的现象**：

- Windows 上只有 `pid` 一行输出，`euid`/`egid`/`groups` 调用根本编译不过（必须用 `#[cfg(unix)]` 包住）。
- Linux 上能看到 `pid`/`euid`/`egid`，但 `groups()` 返回 `None`。
- macOS 上 `pid()` 返回 `None`（Darwin 的 `xucred` 没有 `cr_pid`），`euid`/`groups` 有值。

**预期结果**：与本节可用性矩阵一致。各字段的具体数值**待本地验证**（取决于运行账号与系统）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PeerCreds` 设计成 `Copy`，而 `Name`（见 [u2-l4](u2-l4-name-system.md)）不是？

> **答案**：`PeerCreds` 内部是值类型的凭据快照（几个整数/一个小数组），拷贝廉价，且语义上「只读副本」很自然；`Name` 内部可能持有 `Cow` 借用或堆分配的字符串，拷贝有成本且涉及生命周期，故不 `Copy`。

**练习 2**：在 Windows 上调用 `creds.euid()` 会发生什么？

> **答案**：编译错误。`euid()` 带 `#[cfg(any(doc, unix))]`，在 Windows 上该方法不存在，必须用 `#[cfg(unix)]` 守卫相关代码。

### 4.2 获取凭据：StreamCommon::peer_creds

#### 4.2.1 概念说明

`PeerCreds` 本身只是个数据容器，真正「去问操作系统要凭据」的入口是 `StreamCommon::peer_creds(&self) -> io::Result<PeerCreds>`。它定义在 `StreamCommon` trait 上（同步流与异步流都实现它，见 [u3-l3](u3-l3-stream-read-write-split.md)），所以无论是 `local_socket::Stream` 还是它的 Tokio 版本，都能调用。

返回 `io::Result` 是因为「向内核查询」这一步本身可能失败（系统调用返回错误）。

#### 4.2.2 核心流程

理解 `peer_creds()` 最重要的一点是它的**取值时刻语义**：凭据是「连接建立那一刻的快照」，而不是实时查询。trait 文档说得很清楚：

- **Unix**：客户端的凭据取自 `connect` 时刻；服务端的凭据取自 `listen` 时刻。在 OpenBSD 和 NetBSD 上，服务端凭据改为取自 `bind` 时刻。
- **Windows**：调用 `GetNamedPipeClientProcessId`（或对应的服务端版本），方向感知——服务端拿到客户端 PID，客户端拿到服务端 PID。

把这条规则画成时间线：

```
服务端                          客户端
  │                               │
  ├─ bind() ─────────────────┐    │   (OpenBSD/NetBSD 服务端凭据取自这里)
  ├─ listen() ───────┐       │    │   (其它 Unix 服务端凭据取自这里)
  │                  │       │    │
  │                  │       ├─ connect()  (客户端凭据取自这里)
  ├─ accept() ◀──────┴───────┴────┤
  │                               │
  ├─ peer_creds() ◀ 快照(上述时刻) │
```

派发链沿用 [u3-l1/u3-l2](u3-l1-listener-options.md) 的套路：公共 `Stream` 枚举 →（`dispatch!`）→ 后端 `Stream` → 后端 `StreamCommon::peer_creds` → 真正的系统调用。

#### 4.2.3 源码精读

接口定义与取值时刻语义写在 trait 上：

[src/local_socket/stream/trait.rs:78-95](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L78-L95) —— `StreamCommon::peer_creds`，文档说明 Unix 的 connect/listen（OpenBSD/NetBSD 为 bind）取值时刻。

**Unix 后端**把 fd 交给 `PeerCredsInner::for_socket`，再用 `From` 包成公共外壳：

[src/os/unix/uds_local_socket/stream.rs:84-91](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L84-L91) —— `peer_creds` 调用 `PeerCredsInner::for_socket(self.as_fd())`。

`for_socket` 内部用 `getsockopt` 取凭据结构体，并做两项平台特有的健全性检查：

[src/os/unix/local_socket/peer_creds.rs:26-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/peer_creds.rs#L26-L66) —— `for_socket`：先 `getsockopt` 拿到 `MaybeUninit<Inner>`；FreeBSD/Darwin 系检查 `xucred` 的 `cr_version`（版本不匹配返回 `InvalidData`）；Linux 系检查 `pid == 0` 这个「零初始化哨兵」（返回 `ConnectionReset`）。

**Windows 后端**则调用 named pipe 流的 `peer_process_id()`：

[src/os/windows/named_pipe/local_socket/stream.rs:69-76](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L69-L76) —— `peer_creds` 构造 `PeerCredsInner { pid: self.0.peer_process_id()? }`。

`peer_process_id` 是「方向感知」的：服务端取客户端 PID、客户端取服务端 PID：

[src/os/windows/named_pipe/stream/impl.rs:76-90](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl.rs#L76-L90) —— `select_dir(if_srv, if_clt)` 按是否服务端分流；`peer_process_id = select_dir(client_process_id, server_process_id)`，最终落到 `GetNamedPipeClientProcessId` / `GetNamedPipeServerProcessId`。

#### 4.2.4 代码实践

**实践目标**：验证「对端凭据 = 自己的进程/用户」这一自洽性，复刻官方测试的做法。

**操作步骤**：参考集成测试 `check_peer_creds`，在服务端 `accept` 后和客户端 `connect` 后各调用一次 `peer_creds()`，把对端的 `pid` 跟 `std::process::id()`、（Unix 下）`euid`/`egid` 跟 `libc::geteuid()`/`getegid()` 比对：

[tests/local_socket/stream.rs:27-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/local_socket/stream.rs#L27-L43) —— 官方测试 `check_peer_creds`：断言对端 `pid` 等于本进程 id、Unix 下 `euid`/`egid` 等于 `geteuid()`/`getegid()`。

**需要观察的现象**：因为服务端和客户端跑在同一个进程（同一线程作用域内），对端的 `pid` 就是自己的 `std::process::id()`，`euid`/`egid` 也是自己的。

**预期结果**：断言全部通过。跨进程运行时对端 pid 会是另一个进程的 pid——**待本地验证**具体数值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 trait 文档要特别区分「connect 时刻」和「listen/bind 时刻」？如果凭据是实时的，还需要这样区分吗？

> **答案**：因为凭据是一次性快照，取值时刻决定了「捕获到的是哪一刻的身份」。若凭据是实时的，就不存在「取自何时」的问题，自然无需区分。这也提示读者：进程随后即使 `setuid` 改变了身份，已建立的连接上 `peer_creds()` 仍返回旧值。

**练习 2**：在 Windows 上，一个由客户端 `connect` 得到的流调用 `peer_creds()`，返回的 pid 是谁的？

> **答案**：是服务端的 pid。Windows 后端方向感知：客户端流调用 `server_process_id`（即 `GetNamedPipeServerProcessId`），取的是「对端（服务端）」的 PID。

### 4.3 安全决策：竞态风险与正确用法

#### 4.3.1 概念说明

`peer_creds()` 的典型用途是**鉴权**——服务端判断「连进来的客户端是不是我信任的用户/进程」，据此决定是否提供服务。本模块讲清这件事的陷阱。

核心风险叫 **TOCTOU（time-of-check to time-of-use）竞态**，具体到本场景就是 **PID 复用**：

- 你拿到对端 `pid = 1234`。
- 你用这个 pid 去查 `/proc/1234` 或 `getpwuid` 之类的「外部身份表」，确认它是可信用户。
- 但就在你「查」和「据此放行」之间，原始对端进程退出了，操作系统把 1234 这个编号**回收并分配给了一个攻击者进程**。
- 于是你放行的是攻击者，而你核对的是早已退出的老进程。

文档在两处都反复强调：用于安全决策时务必保证「你用来鉴权的标识符不会被失效并重用」。

#### 4.3.2 核心流程

竞态的产生链条：

```
accept() → peer_creds() 拿到 pid=1234
              │
              ▼
   用 pid 查外部身份表（/proc、ps、用户名映射…）  ◀── 此刻 1234 可能已被回收
              │
              ▼
   依据查询结果决定放行/拒绝                        ◀── 判定的可能已是另一个进程
```

关键认知：

- **`peer_creds()` 本身不引入竞态**：Unix 上凭据由内核在 `getsockopt` 时直接填入结构体（`ucred`/`xucred`），不经过「先拿 pid 再查表」这一步；Windows 的 `GetNamedPipeClientProcessId` 也是内核直接给。所以凭据结构体里的 `euid`/`egid` 是可信快照。
- **竞态来自「拿 pid 再去外部查」**：只有当你**用 pid 作为钥匙去查另一张表**时，pid 复用才会咬人。
- **更糟的前提**：要让竞态真正有害，需要「对端进程已退出但连接还在」。正常情况下对端退出会关闭连接；但如果对端的连接 fd 被泄露（被子进程继承、或经 `SCM_RIGHTS` 发给了别的进程），连接就不会随原进程退出而关闭，pid 就有窗口被复用。interprocess 自身不会造成这种泄露，但与其它库混用时可能出现。

缓解办法（文档建议）：**进行多次查询**，交叉验证；或尽量用 `euid`/`egid`（内核直填、不依赖外部查表）而非 `pid` 做鉴权。

#### 4.3.3 源码精读

类型级的安全警告：

[src/local_socket/peer_creds.rs:13-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/peer_creds.rs#L13-L16) —— 提醒用于安全决策可能受竞态影响，要求所用标识符不可在无管理员干预下被失效与重用。

`pid()` 方法上的详细警告，点明了泄露来源（子进程继承、`SCM_RIGHTS`）与缓解手段（多次查询）：

[src/local_socket/peer_creds.rs:44-52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/peer_creds.rs#L44-L52) —— `pid()` 文档：按 pid 查身份受竞态影响；泄露连接 fd 会让原进程退出后连接仍开；interprocess 不制造此竞态，但与其它库交互可能需要多次查询来缓解。

`peer_creds()` 方法本身也重申了同一警告：

[src/local_socket/stream/trait.rs:84-89](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L84-L89) —— `peer_creds()` 文档：凭据性质 OS 相关，用于安全决策可能有竞态。

#### 4.3.4 代码实践

**实践目标**：设计一个「相对安全」的访问控制，并指出不安全写法。

**操作步骤**：

1. 不安全写法（仅作反面教材，**不要**用于生产）：

   ```rust
   // 示例代码（不安全）：用 pid 去外部查身份
   let creds = conn.peer_creds()?;
   if let Some(pid) = creds.pid() {
       let user = lookup_user_by_pid(pid); // 查 /proc 或 ps —— 存在 PID 复用窗口
       if user.is_trusted() { grant(); }
   }
   ```

2. 更稳妥的写法：优先用内核直填的 `euid`/`egid`，避免「pid → 外部表」的跳转：

   ```rust
   // 示例代码（较稳妥）：直接用凭据里的身份字段
   let creds = conn.peer_creds()?;
   #[cfg(unix)]
   if creds.euid() == Some(TRUSTED_UID) {
       grant();
   }
   ```

**需要观察的现象 / 思考**：对比两种写法，第二种不经过「pid 查表」这一跳，因而不受 pid 复用影响（前提是你的信任判定只依赖凭据结构体里的字段）。

**预期结果**：这是一个**源码阅读 + 设计型实践**，无需运行即可得出结论；如需验证行为，可在本地构造一个会泄露 fd 的客户端，观察 pid 是否在原进程退出后仍可被查到——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么说「竞态不在 `peer_creds()` 本身，而在用它返回的 pid 去查外部表」？

> **答案**：`peer_creds()` 返回的凭据（尤其 `euid`/`egid`）由内核在系统调用时直接填入结构体，是一次性快照，不经过可被替换的中间查表步骤；只有当你拿其中的 `pid` 再去查 `/proc`、用户名映射等外部表时，才暴露在「pid 被回收复用」的窗口里。

**练习 2**：如果对端进程正常退出，连接会怎样？这跟竞态有什么关系？

> **答案**：正常退出会关闭连接，服务端能感知到，此时 pid 复用不构成威胁（连接已断）。竞态只在「进程退出但连接因 fd 泄露仍然存活」时才有害——这也是文档强调泄露来源（子进程继承、`SCM_RIGHTS`）的原因。

## 5. 综合实践

把本讲三部分串起来：写一个带「轻量访问控制」的 local socket 回显服务。

要求：

1. 服务端用 `ListenerOptions` 建监听器（参考 [u3-l1](u3-l1-listener-options.md)），`accept` 后调用 `peer_creds()`。
2. 打印对端的 `pid`；Unix 下额外打印 `euid`/`egid`，并据此判断：仅当 `euid` 等于服务端启动者的 euid 时才回显，否则返回一行 `denied`。
3. 客户端 `connect` 后也调用 `peer_creds()` 打印服务端凭据，验证方向感知。
4. 在代码注释里写明：为什么你选择用 `euid` 而非 `pid` 做判断（呼应 4.3 的竞态讨论）。

验收要点：同一用户运行客户端时收到回显；切换到别的用户运行客户端时（Unix）收到 `denied`。跨用户行为的实际效果**待本地验证**。

## 6. 本讲小结

- `PeerCreds` 是连接对端的身份凭据容器，灵感来自 `std::fs::Metadata`，`Copy` 且按值传递。
- 四个 getter（`pid`/`euid`/`egid`/`groups`）都返回 `Option`，因为字段可用性因平台而异；其中 `euid`/`egid`/`groups` 在 Windows 上连方法都不存在（编译期 `cfg`）。
- 平台分四类：Windows（仅 pid）、`ucred` 系（pid/euid/egid）、`xucred` 系（euid/groups，部分有 pid）、NetBSD（pid/euid/egid）。
- `peer_creds()` 返回的是**连接建立时刻的快照**：Unix 取自 `connect`/`listen`（OpenBSD/NetBSD 取自 `bind`），Windows 方向感知地取对端进程 PID。
- 凭据结构体本身由内核直填、可信；用其中的 `pid` 再去外部查表会引入 PID 复用竞态（TOCTOU），鉴权应优先用 `euid`/`egid` 或多次交叉查询。

## 7. 下一步学习建议

- 继续向平台后端深入：阅读 [u4-l1（Unix UDS 后端）](u4-l1-unix-uds-backend.md) 与 [u4-l2（Windows named pipe 后端）](u4-l2-windows-named-pipe-local-socket.md)，看 `for_socket`/`peer_process_id` 所在的整条系统调用封装链。
- 若对底层 `getsockopt` 与 Windows API 的 `unsafe` 封装感兴趣，可预习 [u9-l1（FFI 封装层）](u9-l1-ffi-wrappers.md)，了解 `c_wrappers` 如何把系统调用转成 `io::Result`。
- 想看凭据如何在异步路径上工作，可对照 [u6-l2（异步 Listener 与 Stream）](u6-l2-async-listener-stream.md)——`StreamCommon` 同样适用于 Tokio 流，`peer_creds()` 的语义不变。
