# Linux UDP 绑定与 sticky socket

## 1. 本讲目标

本讲在 u2-l1（平台 trait）和 u2-l2（Linux TUN）的基础上，进入 Linux 上 UDP 这一侧的实现。读完本讲，你应当能够：

- 说清为什么 wireguard-rs 在 Linux 上要**分别**绑定一个 IPv4 和一个 IPv6 套接字，并让二者**复用同一个端口**。
- 理解 `IP_PKTINFO` / `IPV6_PKTINFO` 这段辅助数据（ancillary data）如何实现 WireGuard 协议要求的 **sticky socket（源地址粘连）** 行为。
- 看懂 `LinuxEndpoint` 如何把「对端目的地址」与「本端 sticky 源地址」打包在一起，并能解释 `from_address` / `into_address` / `clear_src` 三个方法各自的作用。
- 解释 `write4` / `write6` 在 `errno == EINVAL` 时为什么要**清空辅助数据再重发**，以及这对应 WireGuard 的哪一种失效场景。
- 掌握 `LinuxOwner::set_fwmark` 如何用 `SO_MARK` 给外出报文打标记。

本讲是「平台可替换」设计在 Linux 上的第二块拼图：TUN 负责「与内核交换明文 IP 包」，UDP 负责「与对端交换密文报文」。二者合在一起，协议核心就拿到了完整的 IO 能力。

## 2. 前置知识

阅读本讲前，建议你已掌握以下概念（不熟悉的术语会在出现时简要解释）：

- **UDP 套接字基础**：`socket` / `bind` / `sendto` / `recvfrom` 的基本用法，知道 `SOCK_DGRAM` 是什么。
- **`sendmsg` / `recvmsg` 与辅助数据（ancillary data / control message）**：普通 `sendto` 只能发「数据」，而 `sendmsg` 可以附带一段「控制信息」（`cmsghdr`），内核会据此改变发送行为。本讲的核心 `IP_PKTINFO` 就是通过这段控制信息传递的。
- **`sockaddr_in` / `sockaddr_in6`**：IPv4 / IPv6 地址在 C 层的结构表示，注意端口、地址都是**网络字节序（大端）**。
- **`u2-l1` 中的 trait 契约**：`UDP`、`Reader`、`Writer`、`Owner`、`PlatformUDP`、`Endpoint` 这几个 trait 各自定义了什么能力。
- **WireGuard 的「sticky socket」概念**：回复报文的源地址，要「粘」在对对端报文到达时所用的本端接口地址上，而不是由路由表临时决定。

如果你对 trait 层还不熟，强烈建议先读 u2-l1，本讲不再重复 trait 定义本身，只讲它们的 Linux 落地。

## 3. 本讲源码地图

本讲主要涉及三个文件：

| 文件 | 作用 |
| --- | --- |
| [src/platform/linux/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs) | Linux 上 `PlatformUDP` 的全部实现：`bind4` / `bind6`、读 / 写路径、`LinuxEndpoint`、`LinuxOwner`。本讲绝对主力。 |
| [src/platform/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs) | 平台无关的 trait 定义：`Reader` / `Writer` / `UDP` / `Owner` / `PlatformUDP`。 |
| [src/platform/endpoint.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs) | `Endpoint` trait，只有三个方法：`from_address` / `into_address` / `clear_src`。 |

另外会少量引用配置层，用来交代 `bind` 的产物如何被挂到协议核心上：

| 文件 | 作用 |
| --- | --- |
| [src/configuration/config.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs) | `start_listener` 调用 `B::bind`，拿到 reader/writer/owner 后装配进 `WireGuard`。 |

> 一句话关系：`PlatformUDP::bind` 产出 `(readers, writer, owner)` 三元组——readers 喂给 `udp_worker` 线程（入站）、writer 给路由器发出站报文、owner 掌握 socket 生命周期与 fwmark。

---

## 4. 核心概念与源码讲解

### 4.1 双栈绑定：bind4 / bind6 / PlatformUDP::bind

#### 4.1.1 概念说明

WireGuard 的 UDP 监听有一个看起来「奇怪」的设计：它**不是**开一个双栈 IPv6 套接字同时收 v4/v6 报文，而是**开两个独立的套接字**——一个 `AF_INET`、一个 `AF_INET6`——并让它们绑定**同一个端口**。

这样做的理由有三点：

1. **简化 `IP_PKTINFO` 处理**：v4 用 `in_pktinfo` / `IP_PKTINFO`，v6 用 `in6_pktinfo` / `IPV6_PKTINFO`，二者的辅助数据结构不同。分开套接字后，每个套接字只处理一种辅助数据，代码路径清晰。
2. **天然并行**：两个套接字 = 两个 `Reader` = 两个 `udp_worker` 线程，v4 和 v6 入站互不阻塞。
3. **`IPV6_V6ONLY`**：IPv6 套接字显式设置 `IPV6_V6ONLY=1`，禁止它接收 IPv4-mapped 地址的报文，避免和专门的 v4 套接字冲突。

「复用同一端口」靠一个小技巧实现：先 `bind6`，若调用方传入 `port=0`（表示「随便选一个」），内核会分配一个端口；把这个**实际分配到的端口**记下来，再用它去 `bind4`，从而保证两侧端口一致。任意一侧失败都没关系——只要至少一侧成功，就还能工作（例如禁用了 IPv6 的系统）。

#### 4.1.2 核心流程

`PlatformUDP::bind` 的流程：

```
bind(port):
    1. bind6(port)            # 尝试 IPv6，拿回实际端口 new_port
       若成功 → port = new_port
    2. bind4(port)            # 用同一端口尝试 IPv4
       若成功 → port = new_port
    3. 若两者都失败 → 返回错误
    4. 把两个 fd 包成 Arc<FD>
    5. 组装 (readers, writer, owner)
       - readers: 每个 socket family 一个 LinuxUDPReader
       - writer:  同时持有 sock4 和 sock6（缺的用 FD(-1) 占位）
       - owner:   持有 sock4/sock6 的权威引用，Drop 时 shutdown
```

`bind4` / `bind6` 各自的步骤高度对称：

```
bind4(port):
    1. socket(AF_INET, SOCK_DGRAM, 0)
    2. setsockopt: SO_REUSEADDR=1, IP_PKTINFO=1   # 关键：开启 pktinfo 上报
    3. bind(INADDR_ANY : port)
    4. getsockname → 读回内核实际分配的端口（处理 port=0）
    5. 断言 family / 端口一致性，返回 (new_port, fd)
```

#### 4.1.3 源码精读

**`bind6` 的 socket + 三个 setsockopt**：

[src/platform/linux/udp.rs:519-545](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L519-L545) —— 创建 `AF_INET6` 套接字，然后设置三个选项。注意第三行的 `IPV6_V6ONLY=1`，它把 IPv6 套接字限定为「只收 v6」，是双栈共存的前提。

第二行 `IPV6_RECVPKTINFO=1` 是 sticky socket 的「总开关」：开了它，内核才会在 `recvmsg` 时把入站报文到达的本地地址/接口填进辅助数据里返回。

**`bind4` 对应的一段**：

[src/platform/linux/udp.rs:598-622](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L598-L622) —— 结构完全对称，只是 `IPPROTO_IPV6`+`IPV6_RECVPKTINFO` 换成了 `IPPROTO_IP`+`IP_PKTINFO`，并且没有 `V6ONLY` 这一项。

**`getsockname` 读回实际端口**（这是「同端口」的关键）：

[src/platform/linux/udp.rs:562-585](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L562-L585) —— `bind` 之后立刻 `getsockname`，把内核分配的真实端口取出来返回。当调用方传 `port=0` 时，这里拿到的才是可用端口。

**`PlatformUDP::bind` 的装配**：

[src/platform/linux/udp.rs:671-719](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L671-L719) —— 先 `bind6` 再 `bind4`，注意第 676-678 行：`bind6` 成功后把 `port` 更新为真实端口，再喂给 `bind4`。第 687-690 行处理「两边都失败」。第 704-709 行按 family 把 socket 推进 `readers`，第 713-716 行构造 writer 时，缺失的那一侧用 `FD(-1)` 占位（`FD(-1)` 在 Drop 时不会调用 `close`，见下文）。

**`FD` 与它的 `Drop`**：

[src/platform/linux/udp.rs:12-23](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L12-L23) —— 把裸 `RawFd` 包成 RAII 类型，`fd != -1` 时才 `close`，这就是 writer 里能用 `FD(-1)` 表示「该 family 未绑定」的原因。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**（运行真实绑定需要网络与权限）。

1. **目标**：验证「同端口」逻辑确实成立。
2. **步骤**：
   - 打开 [src/platform/linux/udp.rs:671-719](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L671-L719)。
   - 假设调用 `LinuxUDP::bind(0)`：追踪 `port` 这个局部变量的取值变化。
3. **需要观察的现象**：`bind6` 内核分配了某端口（如 51820），随后 `bind4(51820)` 也用 51820。
4. **预期结果**：`LinuxOwner.port` 最终等于 51820，两个 reader（v6 + v4）共用该端口。
5. **待本地验证**：若你在一台禁用 IPv6 的机器上跑，`bind6` 会失败，但 `bind4` 仍成功，`readers` 里将只有一个 v4 reader——请用 `strace -e socket,bind,setsockopt ./wireguard-rs wg0` 观察实际系统调用序列来印证。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `bind6` 里的 `setsockopt(IPV6_V6ONLY, 1)`，会发生什么？

> **答案**：该 IPv6 套接字会变成双栈，能接收 IPv4-mapped 报文，于是 `bind4` 会因为端口已被（以 `INADDR_ANY` 等价形式）占用而失败或行为错乱；同时 v4 报文会同时被两个套接字处理，破坏「每 family 一条清晰路径」的设计。`V6ONLY` 正是为了让两个套接字和平共存。

**练习 2**：`bind(0)` 时，为什么必须先 `bind6` 再 `bind4`，而不是反过来？

> **答案**：因为 `port=0` 的实际端口要由内核分配，第一侧绑定后才能拿到真实端口传给第二侧。先做 v6 是惯例——若先 v4 拿到端口再 v6，逻辑也成立，但代码固定了「v6 先、v4 后」，并要求两侧共用第一侧分配的端口。

---

### 4.2 LinuxEndpoint：承载 sticky source 的数据结构

#### 4.2.1 概念说明

这是本讲的「中枢」数据结构。一个 `LinuxEndpoint` 同时承载两份信息：

- **`dst`**：报文的**目的地**（对端的地址 + 端口），来自 `recvmsg` 的 `msg_name`，即「这份报文是从谁那里来的」。
- **`info`**：报文的 **sticky 源信息**（本端该用哪个地址 / 接口回发），来自 `recvmsg` 的辅助数据 `in_pktinfo` / `in6_pktinfo`。

为何要把它们打包在一起？因为 WireGuard 的「端点」不只是「对方的地址」，还包含「我该从哪个本地地址/接口发回去」。这两者在同一次 `recvmsg` 中被一起捕获，并在后续 `sendmsg` 中被一起使用，构成 sticky socket 的闭环。

`in_pktinfo` 有三个字段，本讲重点关注前两个：

| 字段 | 含义 | 在本讲中的角色 |
| --- | --- | --- |
| `ipi_ifindex` | 报文到达的接口索引 | 0 表示「交给路由表决定」；非 0 表示粘连到某接口 |
| `ipi_spec_dst` | 报文到达的**本地地址**（本端视角的「目的地址」） | **回复报文的源地址**就钉在这个值上 |
| `ipi_addr` | 报文原始目的地址（头部里的） | 本讲未直接使用 |

关键直觉：**对端发来的报文到达本端的那个本地地址，就是我们回复时应当使用的源地址。** 这就是「sticky（粘连）」——源地址粘在入站路径上。

`Endpoint` trait 只暴露三个方法（[src/platform/endpoint.rs:3-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs#L3-L7)）：

- `from_address(SocketAddr) -> Self`：由一个纯地址构造端点（用于通过 UAPI **手动配置**对端 endpoint 时，此时还没有 sticky 源，`info` 全零）。
- `into_address() -> SocketAddr`：把端点的**目的地**还原成一个纯地址（用于 UAPI 上报 `endpoint=` 字段）。
- `clear_src(&mut self)`：清空 sticky 源（让后续发送退回「由路由表选源」）。

#### 4.2.2 核心流程

```
入站（recvmsg）:
    msg_name      → dst   （记下对端地址）
    ancillary pktinfo → info （记下本端 sticky 源）
    组装成 EndpointV4 { dst, info }

出站（sendmsg）:
    msg_name      ← dst   （发给对端）
    ancillary pktinfo ← info （指定源地址/接口，实现 sticky）

source 失效（handshake 重试 / new_handshake 定时器）:
    clear_src() → info 置零 → 下次发送让路由表重新选源
```

#### 4.2.3 源码精读

**两个 endpoint 结构**：

[src/platform/linux/udp.rs:37-45](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L37-L45) —— `dst`（目的）+ `info`（sticky 源）。注意注释把 `info` 标注为 "src & ifindex"。

**`from_address`**：

[src/platform/linux/udp.rs:133-166](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L133-L166) —— 把传入的 `SocketAddr` 拆成 `dst`（注意端口、地址都转成大端 `to_be()`），`info` 全部置零（`ipi_ifindex: 0`、`ipi_spec_dst: 0`）。注释明确写着 `ipi_spec_dst` 是 "src IP (dst of incoming packet)"——即：一旦收到对端报文，这里会被填成入站报文到达的本端地址。

**`into_address`**：

[src/platform/linux/udp.rs:168-183](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L168-L183) —— **只**把 `dst` 还原成 `SocketAddr`（端口 `from_be` 转回主机序），完全忽略 `info`。这符合直觉：对外暴露的「对端地址」就是 `dst`，sticky 源是本端的内部实现细节。

**`clear_src`**：

[src/platform/linux/udp.rs:185-196](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L185-L196) —— 把 `info` 的接口索引和源地址都清零，但**保留 `dst`**。也就是说「对端地址不变，只是放弃粘连的源，下次发送让内核按路由表选源」。

调用方在 [src/wireguard/timers.rs:295](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L295) 和 [src/wireguard/timers.rs:327](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L327) —— 握手重传与 `new_handshake` 定时器触发时调用 `peer.clear_src()`，主动丢弃可能已失效的 sticky 源。

#### 4.2.4 代码实践

1. **目标**：理解 `ipi_spec_dst` 的双重身份。
2. **步骤**：在 [src/platform/linux/udp.rs:144-148](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L144-L148) 的 `in_pktinfo` 初始化处补一段中文注释。
3. **建议注释内容**（示例代码，仅作参考，不是项目原有代码）：

   ```rust
   // ipi_spec_dst：本端「粘连源地址」。
   // - 收报文时：内核填入「报文到达本端所用的本地地址」；
   // - 发报文时：我们把同一个值塞回辅助数据，钉死回复报文的源地址，
   //   实现 WireGuard 的 sticky socket 行为。
   // - 这里在 from_address 中置零，表示「尚未学到 sticky 源，
   //   先让路由表选源，等首个入站报文到达后再被 read4 覆盖」。
   info: libc::in_pktinfo {
       ipi_ifindex: 0,
       ipi_spec_dst: libc::in_addr { s_addr: 0 },
       ipi_addr: libc::in_addr { s_addr: 0 },
   },
   ```

4. **需要观察的现象**：在 `from_address` 阶段 `info` 是零；真正被填充发生在 `read4`（见 4.3）。
5. **预期结果**：你能用自己的话解释「为何 `from_address` 时 `info` 必须为零」——因为此时还没有任何入站报文可学。

#### 4.2.5 小练习与答案

**练习 1**：`into_address` 为什么不返回 `info` 里的内容？

> **答案**：`info` 描述的是**本端**的源地址/接口，是对端不需要也不应知道的内部实现细节；`into_address` 的语义是「这个端点对应的对端地址」，所以只取 `dst`。

**练习 2**：`clear_src` 之后，下一次 `write4` 会以什么源地址发出？

> **答案**：因为辅助数据被清零（见 4.4 的发送路径），`write4` 仍会带上全零的 `in_pktinfo`。具体效果取决于内核：通常等价于「不指定源」，由路由表为目的地选一个合适的本端地址。之后下一次 `read4` 收到对端报文时，会被重新填上正确的 sticky 源。

---

### 4.3 接收路径：recvmsg + IP_PKTINFO（read4 / read6）

#### 4.3.1 概念说明

`LinuxUDPReader` 是 `udp_worker` 线程的「数据源」（u3-l3 会讲 worker）。它的 `read` 一次性完成两件事：

1. 把**报文负载**读进 `buf`（通过 `iovec`）。
2. 把**对端地址**读进 `src`（通过 `msghdr.msg_name`），把 **sticky 源信息**读进 `control`（通过 `msghdr.msg_control`，即辅助数据）。

这三种信息在一次 `recvmsg` 系统调用里一起拿到，是 `sendmsg` / `recvmsg` 相对 `recvfrom` 的核心优势——后者拿不到辅助数据。

辅助数据的载体是两个 `#[repr(C, align(1))]` 结构（[src/platform/linux/udp.rs:25-35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L25-L35)）：

```rust
struct ControlHeaderV4 { hdr: libc::cmsghdr, info: libc::in_pktinfo }
struct ControlHeaderV6 { hdr: libc::cmsghdr, info: libc::in6_pktinfo }
```

它们就是「一个控制消息头 + 一段 pktinfo 载荷」的内存布局，直接当作 `msg_control` 缓冲区使用，免去手写 `CMSG_FIRSTHDR` / `CMSG_DATA` 的指针运算。

#### 4.3.2 核心流程

`read4` 的流程：

```
read4(fd, buf):
    1. 准备 iovec 指向 buf
    2. 准备 src: sockaddr_in      （未初始化，由内核填写）
    3. 准备 control: ControlHeaderV4 （未初始化，由内核填写）
    4. 组装 msghdr { msg_name=src, msg_iov=iovec, msg_control=control, ... }
    5. recvmsg(fd, &msghdr, 0)
    6. len <= 0 → 返回 NotConnected 错误
    7. 返回 (len, EndpointV4 { info: control.info, dst: src })
       —— 把「内核填好的 sticky 源」和「对端地址」一起塞进端点
```

第 7 步是 sticky socket 的「学习」环节：每收一个报文，就用它到达时的本地地址**刷新**端点的 sticky 源。这就实现了 WireGuard 的端点漫游（roaming）——对端换了网络，源地址会自动跟着更新。

#### 4.3.3 源码精读

**`read4` 全貌**：

[src/platform/linux/udp.rs:254-305](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L254-L305) —— 注意第 268 行 `control` 用 `MaybeUninit::uninit().assume_init()` 声明，因为它的内容会被 `recvmsg` 完整覆盖（前提是 `msg_controllen` 足够大，第 279-282 行有断言保证）。

关键是第 298-304 行的返回值构造：

```rust
Ok((
    len.try_into().unwrap(),
    LinuxEndpoint::V4(EndpointV4 {
        info: control.info, // save pktinfo (sticky source)
        dst: src,           // our future destination is the source address
    }),
))
```

注释一语中的：`dst` 就是「对方刚才是从这儿发来的，所以我未来的目的地就是它」；`info` 是「报文到达的本端地址，我下次也从这儿发」。

**`Reader` trait 的派发**：

[src/platform/linux/udp.rs:308-317](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L308-L317) —— `read` 根据 `LinuxUDPReader` 是 `V4` 还是 `V6` 派发到 `read4` / `read6`。`read6`（[src/platform/linux/udp.rs:200-252](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L200-L252)）结构完全对称，只是把 `in_pktinfo` 换成 `in6_pktinfo`。

> 平台无关 trait 在 [src/platform/udp.rs:4-8](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L4-L8)：`fn read(&self, buf: &mut [u8]) -> Result<(usize, E), Self::Error>`——返回「读到的字节数 + 端点」。

#### 4.3.4 代码实践

1. **目标**：理解「一次 `recvmsg` 同时拿到负载、对端地址、sticky 源」。
2. **步骤**：对照 [src/platform/linux/udp.rs:269-284](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L269-L284) 的 `msghdr` 字段，画一张表，把 `msg_name` / `msg_iov` / `msg_control` 三个字段分别对应到「对端地址 / 负载 / sticky 源」。
3. **需要观察的现象**：三者由同一次系统调用填充。
4. **预期结果**：你能说清「为什么不能用 `recvfrom` 替代 `recvmsg`」——因为 `recvfrom` 拿不到 `msg_control` 里的 `in_pktinfo`，sticky socket 就无从实现。
5. **待本地验证**：可写一个最小 C 或 Rust 程序，`setsockopt(IP_PKTINFO)` 后 `recvmsg`，打印 `ipi_spec_dst`，对比本机 `ip addr`，验证它确实等于报文到达的本地接口地址。

#### 4.3.5 小练习与答案

**练习 1**：`read4` 中 `src` 和 `control` 都用 `MaybeUninit::uninit().assume_init()` 声明，安全吗？

> **答案**：在本场景下安全。因为它们随后会被 `recvmsg` 完整写入（`msg_name` 写满 `src`，`msg_control` 写满 `control`），且代码用 `debug_assert!` 保证 `msg_controllen` 足够容纳一个 `cmsghdr + in_pktinfo`。只要 `recvmsg` 成功返回正数，这两个结构就是「已初始化」状态。若 `recvmsg` 返回 `<=0`，代码直接报错返回，不会读取未初始化内存。

**练习 2**：为什么每收一个报文都要**重新**写 `info`，而不是只在第一次写？

> **答案**：因为对端可能漫游（更换网络/接口），它的新报文可能从本端不同的本地地址到达。每次刷新 `info`，sticky 源就能自动跟上对端的新路径——这正是 WireGuard 无缝漫游的底层机制。

---

### 4.4 发送路径：sendmsg + EINVAL 清源重试（write4 / write6）

#### 4.4.1 概念说明

`LinuxUDPWriter::write4` / `write6` 是 sticky socket 的「消费」环节：把端点里学到的 `info` 作为辅助数据附在 `sendmsg` 上发出，从而钉死源地址。

但有一个现实问题：**学到的 sticky 源可能会失效**。例如：

- 报文到达时所用的本地接口地址被删除了（`ip addr del`）。
- 接口被 down 掉了。
- 路由/地址变化导致原来的 `ipi_spec_dst` 不再是本机的合法地址。

这时如果还固执地用失效的 `info` 去发，内核会返回 `EINVAL`。WireGuard 的处理非常务实：

```
sendmsg 用 info 发送
  └─ 返回 EINVAL（源已失效）
       └─ 清空辅助数据（msg_control = null, msg_controllen = 0）
       └─ 清零端点的 info（dst.info = zeroed）
       └─ 不带辅助数据重发一次（让路由表重新选源）
            └─ 仍失败 → 上报 NotConnected
            └─ 成功  → ok（端点 info 已清，下次靠 read 重新学）
```

注意一个副作用：重试成功后 `dst.info` 已被清零，这与显式调用 `clear_src` 的效果一致。换句话说，`EINVAL` 分支隐式完成了一次「自动清源」。

#### 4.4.2 核心流程

辅助数据的构造需要遵循内核约定：控制消息头里的 `cmsg_len` 必须是按字长对齐后的真实长度。代码用两个 `const fn` 来算对齐：

\[ \texttt{CMSG\_ALIGN}(n) = (n + \text{size\_of}\langle u32\rangle - 1)\ \&\ !(\text{size\_of}\langle u32\rangle - 1) \]

\[ \texttt{CMSG\_LEN}(n) = \texttt{CMSG\_ALIGN}(n + \text{size\_of}\langle\text{cmsghdr}\rangle) \]

即：`CMSG_LEN(载荷长度)` = 把「头部 + 载荷」向上对齐到 4 字节。`write4` 里写的是 `CMSG_LEN(size_of::<in_pktinfo>())`，并用 `debug_assert_eq!(cmsg_len % size_of::<u32>(), 0)` 自检对齐。

#### 4.4.3 源码精读

**`CMSG_ALIGN` / `CMSG_LEN`**：

[src/platform/linux/udp.rs:117-125](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L117-L125) —— 对照上面两个公式理解。

**`write4` 构造辅助数据并 `sendmsg`**：

[src/platform/linux/udp.rs:385-424](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L385-L424) —— 第 393-400 行构造 `ControlHeaderV4`，`cmsg_level = IPPROTO_IP`、`cmsg_type = IP_PKTINFO`，载荷 `info = dst.info`（把学到的 sticky 源装回去）。第 414-422 行组装 `msghdr`，`msg_name` 指向 `dst.dst`（对端地址），`msg_control` 指向刚构造的 control。

**`EINVAL` 清源重试**（本模块核心）：

[src/platform/linux/udp.rs:426-445](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L426-L445) ——

```rust
if ret < 0 {
    if errno() == libc::EINVAL {
        log::trace!("clear source and retry");
        hdr.msg_control = ptr::null_mut();   // 摘掉辅助数据
        hdr.msg_controllen = 0;
        dst.info = unsafe { mem::zeroed() }; // 端点 info 清零（隐式 clear_src）
        return if unsafe { libc::sendmsg(fd, &hdr, 0) } < 0 {
            Err(/* NotConnected */)
        } else {
            Ok(())
        };
    }
    return Err(/* NotConnected */);
}
```

这段就是「sticky 源失效后自动退化为路由表选源」的实现。`write6` 的对应逻辑在 [src/platform/linux/udp.rs:361-380](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L361-L380)，完全对称。

**`Writer` trait 的派发**：

[src/platform/linux/udp.rs:451-460](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L451-L460) —— `write` 取 `&mut LinuxEndpoint`，按 `V4` / `V6` 派发。注意 trait 定义里 `dst: &mut E` 是**可变借用**（[src/platform/udp.rs:10-14](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L10-L14)）——正是因为 `EINVAL` 分支要改写 `dst.info`，所以必须 `&mut`。

#### 4.4.4 代码实践

1. **目标**：解释清楚 `EINVAL` 清源重试的来龙去脉。
2. **步骤**：在 [src/platform/linux/udp.rs:426-445](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L426-L445) 的 `if errno() == libc::EINVAL` 分支上方，补一段中文注释。
3. **建议注释内容**（示例代码，仅作参考）：

   ```rust
   // 触发 EINVAL 的典型场景：端点 info 里的 sticky 源（ipi_spec_dst）
   // 已经失效——例如对端报文到达时所用的本地接口地址被删除/接口被 down。
   // 此时若继续带这条辅助数据发送，内核拒绝并返回 EINVAL。
   // 处理：清空辅助数据并把 dst.info 置零（等价于 clear_src），
   //       不指定源地址重发一次，让内核按路由表重新选一个合法源地址。
   //       重试成功后，info 保持为零，直到下一次 read4 收到对端报文重新学习。
   if errno() == libc::EINVAL {
       ...
   }
   ```

4. **需要观察的现象**：把日志级别调到 `trace`，在人为删除对端所用本地接口地址后发送报文，应看到一次 `"clear source and retry"` 日志。
5. **预期结果**：单次 `write4` 内部最多出现两次 `sendmsg` 系统调用，第二次不带辅助数据。
6. **待本地验证**：上述场景需要真实网络环境与 root 权限才能复现日志，请在能联网的 Linux 容器中验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Writer::write` 的第二个参数是 `&mut E` 而不是 `&E`？

> **答案**：因为 `EINVAL` 分支要修改端点的 `info`（置零）。若用 `&E`，就无法在发送路径里完成「隐式清源」， sticky 源失效后就只能一直失败下去。

**练习 2**：第一次 `sendmsg` 返回 `EINVAL`、清源后第二次 `sendmsg` 成功——此时端点的 `dst`（对端地址）是否被修改？

> **答案**：没有。清源只动 `dst.info`（sticky 源），`dst.dst`（对端目的地址）保持不变。下次发送仍发给同一个对端，只是改用路由表选出的本端源地址。

---

### 4.5 fwmark 与 socket 所有权：LinuxOwner + SO_MARK

#### 4.5.1 概念说明

`bind` 返回的 `Owner` 承担两个职责：

1. **持有 socket 的「权威引用」**：reader 和 writer 都只是 `Arc<FD>` 的克隆，真正决定「这套 socket 何时关闭」的是 owner。`LinuxOwner` 在 `Drop` 时对两个 socket 执行 `shutdown(SHUT_RDWR)`，这会让阻塞在 `recvmsg` 的 reader 立刻返回错误，进而让 `udp_worker` 线程退出。这就是「换端口 / down 设备」时能干净拆除工作线程的机制。
2. **设置 fwmark**：通过 `SO_MARK` 给本套接字**产生的所有外出报文**打一个内核标记。这个标记会被策略路由（`ip rule` / `ip route` 里的 `fwmark`）使用，从而把 WireGuard 自己的加密 UDP 报文导到特定的路由表。

`SO_MARK` 的典型用途是「WireGuard over WireGuard」或多宿主场景：你希望隧道的外层报文走某条物理出口，而不是又被自己的 cryptokey 路由表捕获。

#### 4.5.2 核心流程

```
set_fwmark(value: Option<u32>):
    value = value.unwrap_or(0)          # None → 0（清除标记）
    set_mark(sock6, value)?             # SO_MARK on IPv6
    set_mark(sock4, value)              # SO_MARK on IPv4
    其中 set_mark: 若该 family 的 fd 不存在（None），直接 Ok(())

Drop for LinuxOwner:
    shutdown(sock4, SHUT_RDWR) if exists
    shutdown(sock6, SHUT_RDWR) if exists
    （FD 的真正 close 由 Arc<FD> 引用计数归零时触发）
```

#### 4.5.3 源码精读

**`Owner` trait**：

[src/platform/udp.rs:28-34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L28-L34) —— 只有 `get_port` 和 `set_fwmark` 两个方法，注释点明 owner 的两大用途：设 fwmark、Drop 时关 socket。

**`LinuxOwner::set_fwmark`**：

[src/platform/linux/udp.rs:469-480](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L469-L480) —— 内部函数 `set_mark` 对单个 fd 调 `setsockopt(SOL_SOCKET, SO_MARK, &value)`；若该 family 没有 socket（`fd` 为 `None`），直接返回 `Ok(())`。注意 `value.unwrap_or(0)`：UAPI 的 `fwmark=0` 表示「清除」，对应 [src/configuration/uapi/set.rs:136](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/set.rs#L136) 把 `fwmark==0` 转成 `None` 传入。

**`Drop for LinuxOwner`**：

[src/platform/linux/udp.rs:483-499](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L483-L499) —— 对存在的 socket 执行 `shutdown(SHUT_RDWR)`，注意它只 `shutdown` 不 `close`；真正的 `close` 由 `Arc<FD>` 的最后一个引用析构时完成（[src/platform/linux/udp.rs:14-22](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L14-L22)）。

**owner 的生命周期在配置层**：

[src/configuration/config.rs:195-222](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L195-L222) —— `start_listener` 把 `owner` 存进 `cfg.bind`。`down()`（[src/configuration/config.rs:232-237](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L232-L237)）把 `cfg.bind = None`，`owner` 析构 → `shutdown` → reader 报错 → worker 退出，整套 UDP 资源被干净拆除。重新 `up()` 时会再次 `B::bind` 新端口。

#### 4.5.4 代码实践

1. **目标**：把「owner 析构 → worker 退出」这条链子串起来。
2. **步骤**：沿以下顺序阅读三个点：
   - [src/configuration/config.rs:232-237](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L232-L237)（`down` 置 `bind = None`）
   - [src/platform/linux/udp.rs:483-499](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L483-L499)（`Drop` 调 `shutdown`）
   - [src/platform/linux/udp.rs:286-296](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L286-L296)（`read4` 在 `len <= 0` 时返回 `NotConnected`）
3. **需要观察的现象**：`shutdown` 会让阻塞中的 `recvmsg` 立刻返回 `<= 0`。
4. **预期结果**：你能用自己的话解释「为什么换监听端口时旧 worker 不会泄漏」——因为旧 owner 析构触发的 `shutdown` 会令旧 reader 的 `read` 报错，从而令 `udp_worker` 线程退出（worker 细节见 u3-l3）。
5. **待本地验证**：可在 `down/up` 之间用 `ss -lunp` 观察端口变化，印证旧 socket 已关闭。

#### 4.5.5 小练习与答案

**练习 1**：`set_fwmark(None)` 与 `set_fwmark(Some(0))` 效果是否相同？

> **答案**：相同。代码用 `value.unwrap_or(0)`，二者最终都向 `SO_MARK` 写入 `0`，即「清除 fwmark」。

**练习 2**：`LinuxOwner::drop` 只 `shutdown` 不 `close`，那 socket fd 何时真正关闭？

> **答案**：`sock4` / `sock6` 是 `Arc<FD>`，reader 和 writer 也各持一份克隆。只有当**所有**克隆（owner + writer + 各 reader）都析构后，`Arc` 引用计数归零，`FD::drop` 才执行 `libc::close`。`shutdown` 只是打断阻塞 IO，让 reader 尽快结束，从而让引用计数能够归零。

---

## 5. 综合实践

把本讲五个模块串起来，画一张「一个 sticky 源的完整生命周期」时序图，并配文字说明。要求覆盖以下阶段，每一步都标注对应的源码位置：

1. **绑定**：`PlatformUDP::bind` → `bind4`/`bind6` 开启 `IP_PKTINFO`，端口由 `getsockname` 确定。（[udp.rs:671-719](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L671-L719)）
2. **手动配置端点**：UAPI 设 `endpoint=` → `Endpoint::from_address`，此时 `info` 全零。（[udp.rs:133-166](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L133-L166)）
3. **首个入站报文**：`read4` 用 `recvmsg` 同时取 `dst` 与 `info`，sticky 源被**学到**。（[udp.rs:298-304](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L298-L304)）
4. **正常出站**：`write4` 把学到的 `info` 作为辅助数据 `sendmsg` 发出，源地址被钉死。（[udp.rs:393-424](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L393-L424)）
5. **源失效**：本地接口地址被删 → `sendmsg` 返回 `EINVAL` → 清空辅助数据 + 置零 `info` → 不带源重发成功。（[udp.rs:426-445](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L426-L445)）
6. **重新学习**：下一个入站报文到来，`read4` 再次刷新 `info`，闭环回到第 4 步。
7. **拆除**：`down()` → owner `Drop` → `shutdown` → reader 报错 → worker 退出。（[config.rs:232-237](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L232-L237)、[udp.rs:483-499](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L483-L499)）

> 完成后，你应当能脱稿解释：「sticky socket 不是 WireGuard 协议字段，而是靠 Linux 的 `IP_PKTINFO` 在收发两侧自动闭环的 IO 技巧；当粘连源失效时，靠 `EINVAL` 清源重试退化为路由表选源，保证可用性。」

---

## 6. 本讲小结

- wireguard-rs 在 Linux 上**分别**绑定 IPv4 / IPv6 两个套接字，靠 `IPV6_V6ONLY=1` 和「先 bind6 拿真实端口、再 bind4 同端口」实现双栈共端口。
- `IP_PKTINFO` / `IPV6_PKTINFO` 是 sticky socket 的核心：收报文时由内核填入「到达本端的本地地址」，发报文时把同一值塞回辅助数据钉死源地址。
- `LinuxEndpoint` 把「对端目的地址 `dst`」与「本端 sticky 源 `info`」打包；`from_address` 仅设 `dst`、`into_address` 仅取 `dst`、`clear_src` 仅清 `info`。
- `write4` / `write6` 在 `errno == EINVAL` 时会**清空辅助数据并把 `info` 置零后重发**，对应 sticky 源失效场景，退化为路由表选源——这是一次隐式的 `clear_src`。
- `Writer::write` 取 `&mut Endpoint` 正是为了在 `EINVAL` 分支里改写端点状态。
- `LinuxOwner` 负责 fwmark（`SO_MARK`，`None`/`0` 即清除）与 socket 生命周期（`Drop` 时 `shutdown`，由 `Arc<FD>` 引用计数最终 `close`），是「换端口 / down 设备」时干净拆除 worker 的关键。

## 7. 下一步学习建议

- **u2-l4（dummy 平台）**：看 `PairBind` / `UnitEndpoint` 如何在不碰真实 socket 的情况下实现 `Endpoint::clear_src`（[src/platform/dummy/endpoint.rs:17](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/endpoint.rs#L17)），你会更清楚 trait 与实现的边界。
- **u3-l3（udp_worker）**：本讲的 `LinuxUDPReader::read` 是 worker 的数据源，读完 worker 你会理解「`read` 报错如何导致线程退出」。
- **u6-l1（配置接口）**：看 `set_listen_port` / `set_fwmark` 如何触发重新 `bind`，把本讲的 owner/writer 生命周期放进配置层的大图。
- 进阶可阅读 WireGuard 白皮书第 6.1 节关于 sticky socket 与 Denial-of Service 缓解的论述，把本讲的工程实现对应回协议设计动机。
