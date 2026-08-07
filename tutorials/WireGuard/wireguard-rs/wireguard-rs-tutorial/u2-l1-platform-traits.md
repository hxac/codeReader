# 平台抽象 trait 设计

## 1. 本讲目标

本讲打开 `src/platform/` 这一层「接口契约」。读完本讲，你应该能够：

1. 说出 `Tun`、`udp::UDP`、`uapi::PlatformUAPI`、`Endpoint` 四个 trait 各自描述了什么平台能力，以及它们的关联类型分别对应什么。
2. 解释 `PlatformTun::create` 与 `PlatformUDP::bind` 返回的 `(readers, writer, status/owner)` 三元组里每一项的语义。
3. 说清楚为什么协议核心 `WireGuard<T: Tun, B: UDP>` 要用「泛型 + 关联类型」来对接 IO，而不是用运行时的 trait 对象（`Box<dyn Tun>`）。
4. 理解 `#[cfg]` 条件编译如何把当前平台的具体实现别名化为 `plt`，让 `main.rs` 一行代码就能切换平台。

本讲是第 2 单元（平台抽象层）的总纲。后续 u2-l2 / u2-l3 会进入 Linux 的具体实现，u2-l4 会看 dummy 测试实现。本讲只聚焦「trait 契约本身」。

## 2. 前置知识

在进入本讲前，你需要先建立以下直觉（在 u1-l3 已讲过，这里简要回顾）：

- **WireGuard 的协议核心是「纯」的**：握手（Noise IK）和路由器（加解密 + cryptokey 路由）这两套逻辑本身不读文件、不建 socket、不碰操作系统。它们只关心字节和密码学。
- 但协议核心终究要和真实世界交互，交互点只有三个外部接口：
  - **TUN**：一张虚拟网卡，核心从它读「明文 IP 包」（要发出去加密的），往它写「解密后的明文 IP 包」。
  - **UDP**：核心通过它收发「密文报文」（握手报文 + 加密后的传输报文）。
  - **UAPI**：一个文本控制协议，`wg(8)` 工具通过它来配置本设备（设私钥、加 peer、设 allowed-ips 等）。
- 这三个外部接口在不同操作系统上长得很不一样：Linux 用 `/dev/net/tun` + ioctl 建网卡、用 `IP_PKTINFO` 做 sticky socket；macOS 用 `utun`；Windows 用 Wintun。如果协议核心直接调用 Linux 的 syscall，它就再也没法在 macOS 上跑、也没法在单元测试里跑了。

所以需要一个**平台抽象层**：用一组 trait 把「我需要什么样的 IO 能力」描述清楚，协议核心只依赖这些 trait，由具体的平台实现去填空。本讲要逐个读懂这组 trait。

> 术语提示：
> - **trait**：Rust 中描述「某个类型能做什么」的接口，类似 Java/Go 的 interface，但可带关联类型。
> - **关联类型（associated type）**：trait 内部声明的「占位类型」，由实现者指定，调用方通过 `Self::Writer` 这样的路径引用。
> - **泛型约束 `T: Tun`**：表示「类型参数 T 必须实现了 Tun 这个 trait」。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [src/platform/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs) | 平台层的总入口。声明子模块、重导出 `Endpoint` trait，并用 `#[cfg]` 把当前平台的实现别名化为 `plt`。 |
| [src/platform/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs) | 定义 TUN 侧的全部 trait：`TunEvent`、`Status`、`Writer`、`Reader`、`Tun`、`PlatformTun`。 |
| [src/platform/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs) | 定义 UDP 侧的全部 trait：`Reader`、`Writer`、`UDP`、`Owner`、`PlatformUDP`。 |
| [src/platform/uapi.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs) | 定义 UAPI 侧的全部 trait：`BindUAPI`、`PlatformUAPI`。 |
| [src/platform/endpoint.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs) | 定义 `Endpoint` trait：抽象「一个对端地址 + 它的 sticky 源地址信息」。 |
| [src/wireguard/wireguard.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs) | 协议核心。`WireGuard<T: Tun, B: UDP>` 是这组 trait 的「消费者」，印证抽象设计。 |

## 4. 核心概念与源码讲解

### 4.1 设计动机：用泛型 + 关联类型把 IO 解耦

#### 4.1.1 概念说明

platform 层要解决的核心问题是：**协议核心如何在不认识任何具体操作系统的前提下，仍然能收发报文？**

答案是把「需要的 IO 能力」抽象成 trait，让核心成为 trait 的消费者。这里有一个关键的设计抉择：是用**运行时多态（trait 对象 `Box<dyn Writer>`）**，还是用**编译期多态（泛型 `T: Writer`）**？

wireguard-rs 选择了后者——全部用泛型 + 关联类型。原因有二：

1. **性能**：数据面（每个报文都要 `read`/`write`）是热路径。trait 对象的每次方法调用都要经过一次动态分派（vtable 查表），而泛型会被单态化（monomorphization），编译器为每个具体类型生成一份直接的、可内联的代码。对一个追求线速的 VPN，这很重要。
2. **零成本抽象 + 类型安全**：关联类型让「这个 `Tun` 实现自带的 `Reader` 和 `Writer`」在编译期就绑定成一对配套类型，核心代码里 `T::Reader`、`T::Writer` 都是具体类型，没有运行时擦除，也就没有「类型不匹配」的运行时风险。

#### 4.1.2 核心流程

协议核心与平台实现之间的装配流程可以概括为：

```text
        ┌──────────────────────────────────────────┐
        │  协议核心  WireGuard<T: Tun, B: UDP>      │
        │  只认 trait，不认 syscall                  │
        └──────────────┬───────────────────────────┘
                       │ 编译期：T / B 的具体类型由调用方填入
        ┌──────────────┼───────────────────────────┐
        ▼              ▼                           ▼
   T::Reader       T::Writer                    T::Status
   (读明文包)     (写明文包)                   (感知 Up/Down)
   B::Reader       B::Writer          B::Endpoint   B::Owner
   (读密文)       (写密文)           (对端地址)    (持有 socket)
                       │
                       ▼ 运行期由具体实现提供
            Linux:  /dev/net/tun, UDP socket, AF_UNIX
            dummy:  内存通道（测试用）
            macOS:  utun, UDP socket  （假想，本讲练习）
```

关键点：`T` 和 `B` 是「容器型」trait，它们本身没有方法，只用关联类型把一组配套的子类型打包在一起。真正干活的方法在 `Reader`/`Writer`/`Status` 这些子 trait 上。

#### 4.1.3 源码精读

先看 platform 模块入口，理解子模块如何被组织和选择：

[src/platform/mod.rs:1-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs#L1-L16) —— 声明四个子模块并重导出 `Endpoint`；最后用 `#[cfg(target_os = "linux")]` 把 `linux` 模块别名化为 `plt`。

关键几行：

- `pub use endpoint::Endpoint;`（第 7 行）：把 `Endpoint` trait 提到 `platform::Endpoint` 这一公开路径，供 `udp` 等子模块引用。
- `#[cfg(target_os = "linux")] pub mod linux;`（第 9-10 行）：只在 Linux 编译时才编译 `linux` 子模块。
- `#[cfg(test)] pub mod dummy;`（第 12-13 行）：只在测试编译时才编译 `dummy` 子模块。
- `#[cfg(target_os = "linux")] pub use linux as plt;`（第 15-16 行）：**本讲最关键的一行**。它给当前平台实现起了一个统一别名 `plt`。于是 `main.rs` 里写 `plt::Tun`、`plt::UAPI`、`plt::UDP`，编译到 Linux 就解析成 `linux::Tun` 等，换平台只需改这里的 `cfg`，`main.rs` 一行不动。

再看协议核心如何消费这组 trait。`WireGuard` 整个结构体被参数化为 `<T: Tun, B: UDP>`：

[src/wireguard/wireguard.rs:32-61](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L32-L61) —— `WireguardInner<T: Tun, B: UDP>` 的字段。注意第 51 行 `peers` 字段和第 55 行 `router` 字段里大量出现的 `B::Endpoint`、`T::Writer`、`B::Writer`——这正是关联类型被消费的地方：核心把「对端地址类型」「TUN 写句柄」「UDP 写句柄」当作具体类型层层传递下去，全程零动态分派。

这些关联类型的「实物」是在构造期注入的：

[src/wireguard/wireguard.rs:251-262](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L251-L262) —— `add_tun_reader(reader: T::Reader)` 把一个具体的 TUN reader 交给核心，并 spawn 一个 `tun_worker` 线程去驱动它。`add_udp_reader(reader: B::Reader)` 同理。可以看到，核心拿到的 `reader` 已经是具体类型，不再有 `dyn`。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读型实践」亲眼确认「核心只依赖 trait、平台实现通过 `plt` 注入」这一论断。

**操作步骤**：

1. 打开 [src/wireguard/wireguard.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs)，在文件内搜索 `linux`、`libc`、`RawFd`、`socket` 等关键字。
2. 打开 [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs)，搜索 `plt::`，记录每个出现点用了 `plt` 的哪个关联名字。
3. 打开 [src/platform/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs)，确认 `plt` 是怎么被别名化出来的。

**需要观察的现象**：

- `wireguard/` 子树里**完全搜不到** `linux`/`libc` 等平台符号——它只 `use` `tun::Tun`、`udp::UDP`、`Endpoint` 这些 trait。
- `main.rs` 里所有平台相关操作都通过 `plt::Tun`、`plt::UAPI`、`plt::UDP` 这三个名字访问。

**预期结果**：你会清楚看到「协议核心」与「平台实现」之间唯一的粘合点就是 `main.rs` 里对 `plt::*` 的调用，以及 `WireGuard::<plt::Tun, plt::UDP>` 的类型实例化。这正是平台可替换的根基。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `WireGuard` 改成持有 `Box<dyn Tun>`，会损失什么？至少说出两点。

> **参考答案**：(1) 数据面每次 `read`/`write` 都要动态分派，无法内联，热路径性能下降；(2) `dyn Tun` 没法暴露关联类型的具体类型（`Reader`/`Writer` 只能也变成 `Box<dyn ...>`），导致 reader/writer 之间「配套」的静态关系丢失，且核心要把它们装箱来传递，增加堆分配与运行时开销。

**练习 2**：`plt` 这个别名是在哪一行、靠什么机制建立起来的？为什么放在 `platform/mod.rs` 而不是 `main.rs`？

> **参考答案**：在 [src/platform/mod.rs:15-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs#L15-L16)，用 `#[cfg(target_os = "linux")] pub use linux as plt;` 建立。放在 `platform/mod.rs` 是因为「当前平台是谁」是 platform 层的职责，集中在这里一处 `cfg` 切换，`main.rs` 和核心层都不必感知具体平台，职责更清晰。

---

### 4.2 TUN 抽象：Tun / Writer / Reader / Status / PlatformTun

#### 4.2.1 概念说明

TUN 是一张虚拟网卡。从协议核心的视角，它需要四种能力：

- **读**：从网卡读出一个明文 IP 包（这是用户要发出去、待加密的包）。
- **写**：往网卡写一个明文 IP 包（这是收到的、已解密的包）。
- **状态**：感知网卡被 `ip link set up/down` 以及 MTU 变化。
- **创建**：用设备名（如 `wg0`）创建出这张网卡，并返回上述读/写/状态句柄。

`platform/tun.rs` 用五个 trait 描述这四种能力：`Reader`、`Writer`、`Status` 是「能力」trait，`Tun` 是把它们打包的「容器」trait，`PlatformTun` 额外提供 `create` 构造方法。

#### 4.2.2 核心流程

TUN 侧的装配与数据流：

```text
PlatformTun::create("wg0")
        │
        ▼
 返回三元组 (Vec<Reader>, Writer, Status)
        │           │           │
        ▼           ▼           ▼
 每个 Reader   Writer 交给     Status 交给一个
 spawn 一个    router 做        专用线程，循环调
 tun_worker    出站写           event() 拿 Up/Down
 （读明文包                    事件，驱动 cfg.up/down
  去加密发送）
```

注意 `Reader` 返回的是 `Vec`（多个 reader），这是为了支持**多队列**以及 Linux 上「IPv4/IPv6 分开」的常见实现——每个 reader 一个线程，并行读。

#### 4.2.3 源码精读

[src/platform/tun.rs:3-6](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L3-L6) —— `TunEvent` 枚举。`Up(usize)` 携带 MTU，`Down` 不带数据。这是 `Status::event` 的返回值，也是 `main.rs` 里 Tun 事件线程翻译成 `cfg.up(mtu)` / `cfg.down()` 的输入。

[src/platform/tun.rs:8-14](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L8-L14) —— `Status` trait。`event(&mut self)` 在「状态未变化时阻塞」，只有网卡状态真正改变（up/down/MTU 变化）才返回。这把「阻塞等待」的责任下放给平台实现（Linux 用 netlink 阻塞 recv）。

[src/platform/tun.rs:16-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L16-L29) —— `Writer` trait。`write(&self, src: &[u8])` 把一个已解密的明文 IP 包写回网卡。注意它要求 `Send + Sync + 'static`：`Sync` 是因为出站解密后有多个 worker 线程可能并发地往同一个 writer 写。

[src/platform/tun.rs:31-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L31-L49) —— `Reader` trait。重点看 `read(&self, buf: &mut [u8], offset: usize)` 的 `offset` 参数及其文档注释：它要求调用方在缓冲区**预留一段前缀空间**。原因是「某些平台的包前面会带一个协议头」，而且这段前缀稍后会被**就地**用来构造传输消息头（零拷贝）。这是 platform trait 与核心紧密协作的一个细节。

[src/platform/tun.rs:51-55](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L51-L55) —— `Tun` 容器 trait。它**没有任何方法**，只声明三个关联类型 `Writer`、`Reader`、`Error`。这就是前面说的「打包」作用：让核心用 `T::Reader`、`T::Writer` 引用一对配套的句柄。

[src/platform/tun.rs:57-63](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L57-L63) —— `PlatformTun: Tun`。它继承 `Tun`，加上关联类型 `Status` 和构造方法 `create(name: &str) -> Result<(Vec<Self::Reader>, Self::Writer, Self::Status), Self::Error>`。这就是「三元组」的来源：**多个 reader（多队列读）+ 一个 writer（写）+ 一个 status（状态感知）**。`#[allow(clippy::type_complexity)]` 是因为这个返回类型很长，作者主动关掉了 clippy 的「类型太复杂」告警——这是一个合理的复杂度。

#### 4.2.4 代码实践

**实践目标**：理解 `Reader::read` 的 `offset` 参数为何而设。

**操作步骤**：

1. 阅读 [src/platform/tun.rs:36-44](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L36-L44) 的文档注释。
2. 在 `src/wireguard/workers.rs` 的 `tun_worker` 中找到调用 `reader.read(buf, SIZE_MESSAGE_PREFIX)` 的地方，观察 `offset` 取的是什么值。
3. 在 macOS 等「包带协议族前缀」的平台上，这个 offset 还要额外预留前缀，体会「为不同平台的头部预留空间」的设计。

**需要观察的现象**：`offset` 不是 0，而是 `SIZE_MESSAGE_PREFIX`，调用方刻意把 IP 包读到缓冲区中间，留出前缀。

**预期结果**：你会理解 `offset` 是 platform trait 与核心之间关于「就地构造报文、零拷贝」的契约。具体运行行为「待本地验证」（需结合 workers 章节的实践）。

#### 4.2.5 小练习与答案

**练习 1**：`PlatformTun::create` 的返回值里，为什么 `Reader` 是 `Vec` 而 `Writer` 只有一个？

> **参考答案**：TUN 设备通常可以有多个读端（多队列、或 IPv4/IPv6 各一个 socket），每个 reader 配一个线程并行读以提升吞吐；但写端通常共享同一个 fd（Linux 上写 TUN 字符设备是并发安全的），所以只需一个 `Writer`，并靠 `Writer: Send + Sync` 让多个线程共享。

**练习 2**：`Tun` 这个 trait 没有任何方法，只有关联类型。删掉它、把 `Reader/Writer` 直接作为两个独立约束写在 `WireGuard<R: Reader, W: Writer, ...>` 上，会有什么坏处？

> **参考答案**：会丢失「Reader 和 Writer 必须来自同一个平台实现」这一约束。用独立泛型参数，理论上允许把 Linux 的 Reader 和 dummy 的 Writer 拼在一起，类型系统能通过但运行时错误。`Tun` 容器 trait 把配套的 Reader/Writer 绑成一个类型参数 `T`，从根上排除这种错配——这正是关联类型的核心价值。

---

### 4.3 UDP 抽象：UDP / Reader / Writer / Owner / PlatformUDP

#### 4.3.1 概念说明

UDP 套接字是 WireGuard 收发密文的通道。它的抽象比 TUN 多一层微妙：WireGuard 在 Linux 上使用「sticky socket」技术——发送时把报文绑定到「当初收到对端报文时的那个源地址/网卡」，从而配合策略路由。这意味着「对端地址」不能只是个简单的 `SocketAddr`，它还要携带「源地址/网卡」等辅助信息。这就是为什么 UDP 抽象需要 `B::Endpoint` 这个关联类型，而不是直接用 `std::net::SocketAddr`。

UDP 侧需要的能力：

- **读**：读一个密文报文，并返回「它是从哪个对端地址来的」（用 `B::Endpoint` 表达）。
- **写**：把一个密文报文发往指定对端地址（同样是 `B::Endpoint`）。
- **拥有/管理**：持有 socket、读取绑定端口、设置 fwmark、并在 Drop 时关闭 socket。
- **绑定**：绑定一个端口，返回上述读/写/管理句柄。

`platform/udp.rs` 用 `Reader<E>`、`Writer<E>`、`Owner`、`UDP`、`PlatformUDP` 五个 trait 描述。

#### 4.3.2 核心流程

```text
PlatformUDP::bind(port)
        │
        ▼
 返回三元组 (Vec<Reader>, Writer, Owner)
        │              │            │
        ▼              ▼            ▼
 每个 Reader      Writer 交给    Owner 留在配置层
 spawn udp_worker  router 做      （可改 fwmark、
 （读密文 +        出站加密发送   get_port；Drop 时
 对端地址分流）                    关闭 socket）
```

注意 `Reader::read` 返回 `(usize, E)`——读到的字节数**和**对端 endpoint 一起返回，因为处理报文必须知道它来自哪个 peer。

#### 4.3.3 源码精读

[src/platform/udp.rs:4-8](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L4-L8) —— `Reader<E: Endpoint>`。`read(&self, buf) -> Result<(usize, E), _>`，把「收到的对端地址」以 `E` 类型返回。`E` 由 `UDP::Endpoint` 指定。

[src/platform/udp.rs:10-14](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L10-L14) —— `Writer<E: Endpoint>`。`write(&self, buf, dst: &mut E)` 把密文发往 `dst`。注意 `dst` 是 `&mut`——因为发送时可能会**就地修改** endpoint 内的 sticky 源信息（例如 Linux 在 sendmsg 失败、源失效时会清空源重发，详见 u2-l3）。

[src/platform/udp.rs:16-23](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L16-L23) —— `UDP` 容器 trait。除了 `Error`，它声明 `type Endpoint: Endpoint` 以及配套的 `Writer`/`Reader`。重点看第 20 行的注释：

```rust
/* Until Rust gets type equality constraints these have to be generic */
type Writer: Writer<Self::Endpoint>;
type Reader: Reader<Self::Endpoint>;
```

意思是：理想情况下我们想约束「`Writer` 实现了 `Writer<Self::Endpoint>`」（类型相等约束），但 Rust 当年还没有稳定的 type equality constraint，所以 `Writer`/`Reader` 这两个子 trait 本身被设计成**带泛型参数 `E`** 的（`Writer<E>`），这里再用 `Self::Endpoint` 去实例化它们。这是一个值得品味的、受语言能力限制而产生的写法。

[src/platform/udp.rs:28-34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L28-L34) —— `Owner` trait。它代表「对这个 UDP 绑定的所有权」：`get_port` 查端口、`set_fwmark` 设 fwmark。**最关键的是它的生命周期语义**：平台实现会在 `Owner` 被 Drop 时关闭底层 socket（Linux 实现见 [src/platform/linux/udp.rs:12-23](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L12-L23) 的 `FD` 包装器，其 `Drop` 调用 `libc::close`）。于是「重新绑定端口」只需 drop 旧 Owner 再 `bind` 一次。

[src/platform/udp.rs:38-46](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L38-L46) —— `PlatformUDP: UDP`。`bind(port) -> Result<(Vec<Self::Reader>, Self::Writer, Self::Owner), _>`，同样是三元组：**多个 reader + 一个 writer + 一个 owner**。注释说明 owner「在 drop 时关闭 UDP socket」。

#### 4.3.4 代码实践

**实践目标**：理解 `Writer::write` 为什么接收 `&mut E` 而不是 `&E`。

**操作步骤**：

1. 阅读 [src/platform/udp.rs:10-14](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L10-L14)。
2. 打开 [src/platform/linux/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs)，找到 `write4`/`write6`，观察它们在 `sendmsg` 返回 `EINVAL` 时对 endpoint 做了什么（提示：调用 `clear_src` 然后重发）。
3. 结合 [src/platform/endpoint.rs:3-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs#L3-L7) 的 `clear_src(&mut self)`，串起「发送失败 → 清空 sticky 源 → 重发」的逻辑。

**需要观察的现象**：`write` 内部会在特定 errno 下就地修改传入的 endpoint（清空其源地址信息）。

**预期结果**：你会明白 `&mut E` 不是笔误，而是 sticky socket「失效后自愈」所需的写权限。完整实现细节在 u2-l3 展开。

#### 4.3.5 小练习与答案

**练习 1**：UDP 抽象的 `Owner` 和 TUN 抽象的 `Status` 在「三元组」里位置类似，但职责不同。各用一句话说明。

> **参考答案**：`Status` 负责「感知网卡 up/down 与 MTU 变化」（只读的事件源）；`Owner` 负责「持有并管理 socket 的生命周期与配置（端口、fwmark），Drop 时关闭」（可写的资源句柄）。

**练习 2**：为什么 UDP 的 `Reader`/`Writer` 都带泛型参数 `E: Endpoint`，而 TUN 的 `Reader`/`Writer` 不带任何泛型参数？

> **参考答案**：因为 UDP 收发必须携带「对端地址」信息，而对端地址的类型 `E` 是平台相关的（Linux 的 endpoint 还要存 sticky 源信息），所以子 trait 必须泛型化在 `E` 上；TUN 读写的对象只是「无地址的 IP 字节流」（地址信息在 IP 包头里，不靠 socket 辅助数据），所以不需要地址泛型。

---

### 4.4 UAPI 抽象：PlatformUAPI / BindUAPI

#### 4.4.1 概念说明

UAPI 是 WireGuard 自定义的文本控制协议，`wg(8)` 工具用它来查询和修改设备配置。它的传输层是平台相关的：Linux 上是一个绑定在 `/var/run/wireguard/<name>.sock` 的 Unix 域套接字。但协议核心不关心这些细节，它只需要「能拿到一个可读可写的字节流，按行解析文本」。

所以 UAPI 抽象非常薄，只描述「绑定监听端点」和「接受一条连接得到一个字节流」两件事。

#### 4.4.2 核心流程

```text
PlatformUAPI::bind("wg0")
        │
        ▼  返回 Bind（监听端点，如 Linux 的 UnixListener）
   BindUAPI::connect()   // 实际语义是「接受一条新连接」，见下方说明
        │
        ▼  返回 Stream: Read + Write
   把 Stream 交给 configuration::uapi::handle 按行解析
```

#### 4.4.3 源码精读

[src/platform/uapi.rs:4-9](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs#L4-L9) —— `BindUAPI` trait。`type Stream: Read + Write` 是关联类型，约束为标准库的 `Read + Write`。`connect(&self) -> Result<Self::Stream, _>` 返回一条新的字节流。

> 命名提示：方法叫 `connect`，但在 Linux 实现里它内部做的是 `UnixListener::accept()`（接受 `wg(8)` 发起的连接）。名字偏向「得到一条可读写流」的语义，而非字面的「主动连出」。

[src/platform/uapi.rs:11-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs#L11-L16) —— `PlatformUAPI` trait。`bind(name: &str) -> Result<Self::Bind, _>` 用设备名构造监听端点（Linux 实现里会把 `name` 拼进 `/var/run/wireguard/<name>.sock` 路径）。`type Bind: BindUAPI` 把监听器类型暴露出来。

注意与 TUN/UDP 的对比：UAPI 抽象**没有** `Reader/Writer/Owner` 三件套，只有一个 `Bind + Stream`。因为它只是个文本管道，不涉及多队列、不涉及 sticky socket，所以模型最简。

#### 4.4.4 代码实践

**实践目标**：搞清 `PlatformUAPI` 的 `bind` 与 `BindUAPI::connect` 在 Linux 上的真实身份。

**操作步骤**：

1. 阅读 [src/platform/uapi.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs) 全文（很短）。
2. 打开 [src/platform/linux/uapi.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/uapi.rs)，找到 `impl PlatformUAPI for LinuxUAPI` 的 `bind`（确认它绑定 Unix socket 路径），以及 `impl BindUAPI for ...` 的 `connect`（确认它内部是 `accept`）。
3. 在 [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) 中找到 UAPI 服务线程：它循环调用 `connect()`，每拿到一个 `Stream` 就 spawn 一个线程跑 `configuration::uapi::handle`。

**需要观察的现象**：`bind` 一次性创建监听端点；`connect` 每次返回一条新连接对应的流。

**预期结果**：你会看到 UAPI 的「监听-接受-按行解析」三段式如何映射到这三个 trait 方法上。

#### 4.4.5 小练习与答案

**练习 1**：`BindUAPI::Stream` 的约束为什么是 `Read + Write`，而不是某个自定义 trait？

> **参考答案**：因为 UAPI 协议核心（`configuration::uapi::handle`）只把它当成一个「能按行读、能写回文本」的字节流来用，标准库的 `std::io::{Read, Write}` 正好够用且通用。用标准库 trait 可以让任何实现了 `Read + Write` 的类型（包括内存流）都能被当作 UAPI 流，便于测试。

**练习 2**：UAPI 抽象为什么不像 UDP 那样返回「多个 reader」？

> **参考答案**：UAPI 是控制面，并发量极低（一次几条 `wg` 命令），不像数据面要追求吞吐，因此单线程 accept + 每连接 spawn 一个处理线程就足够，无需多队列。

---

### 4.5 Endpoint 抽象：from_address / into_address / clear_src

#### 4.5.1 概念说明

`Endpoint` 是「对端地址」的抽象。它必须能：

- 从一个标准 `SocketAddr` 构造（`from_address`）。
- 转回一个标准 `SocketAddr`（`into_address`）。
- 清空其携带的「sticky 源地址信息」（`clear_src`）。

为什么 `UDP::Endpoint` 不直接用 `std::net::SocketAddr`？因为 Linux 上，endpoint 除了目的地址还要缓存 `IP_PKTINFO` 带来的「源地址 + 网卡索引」，用于 sticky 发送。`SocketAddr` 装不下这些额外信息，所以需要一个平台自定义类型，但核心代码又需要在某些场合把它转回 `SocketAddr`（例如记日志、回填到 UAPI 的 `endpoint` 字段），于是有了这三个转换方法。

#### 4.5.2 核心流程

```text
收报文：recvmsg 拿到 SocketAddr + pktinfo
        │  Endpoint::from_address(...)
        ▼
    B::Endpoint（携带目的地址 + sticky 源信息）
        │  存进 peer 的端点字段，用于后续 sendmsg
        ▼
发报文：Writer::write(buf, &mut endpoint)
        │  若 sendmsg 因源失效返回 EINVAL：
        │  endpoint.clear_src() → 重发
        ▼
需要展示给外部（UAPI get endpoint）：
        into_address() → SocketAddr
```

#### 4.5.3 源码精读

[src/platform/endpoint.rs:1-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs#L1-L7) —— `Endpoint` trait 全文。只有三个方法，约束为 `Send + 'static`。

[src/platform/dummy/endpoint.rs:1-18](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/endpoint.rs#L1-L18) —— dummy 平台的 `UnitEndpoint` 实现。它**不携带任何信息**（`struct UnitEndpoint {}`）：`from_address` 直接丢弃入参，`into_address` 永远返回写死的 `127.0.0.1:8080`，`clear_src` 啥也不做。这个「退化实现」恰好说明了 Endpoint trait 的本质——在纯软件测试里不需要 sticky socket，于是把它实现成空壳，让协议核心照样能跑通。这是一个用最小实现「证明 trait 约束恰如其分」的好例子。

对比之下，Linux 的 `EndpointV4`（[src/platform/linux/udp.rs:37-40](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L37-L40)）同时存了 `dst: sockaddr_in`（目的地址）和 `info: in_pktinfo`（源地址 + 网卡索引），这才是 `clear_src` 有活可干的实现。两种实现的差异，正是 `Endpoint` trait 存在的意义：把「地址」这件事平台化。

#### 4.5.4 代码实践

**实践目标**：对比「最简实现」与「真实实现」，体会 trait 抽象的边界。

**操作步骤**：

1. 阅读 [src/platform/endpoint.rs:1-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs#L1-L7)。
2. 阅读 dummy 实现 [src/platform/dummy/endpoint.rs:5-18](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/endpoint.rs#L5-L18)。
3. 阅读 Linux 实现的字段定义 [src/platform/linux/udp.rs:37-40](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L37-L40)，并猜想 `clear_src` 在 Linux 上会清掉 `info` 里的哪个字段。

**需要观察的现象**：dummy 实现三个方法都是「空操作」，却完全合法地满足了 trait。

**预期结果**：你会理解 trait 只规定「能做什么」，不规定「要做多少实质工作」；当某个平台不需要某项能力时，可以用退化实现把它「空转」掉。

#### 4.5.5 小练习与答案

**练习 1**：`UnitEndpoint::into_address` 返回一个写死的 `127.0.0.1:8080`。这在真实场景里会出问题吗？为什么 dummy 平台可以接受？

> **参考答案**：在真实部署里会（会汇报错误的 endpoint）。但 dummy 平台只用于单元测试，测试不关心 endpoint 的真实地址，只要协议核心能跑通即可。这体现了「抽象允许退化实现」带来的测试便利。

**练习 2**：如果某个新平台完全没有「sticky 源」概念（如某些 NAT 穿透受限的环境），它的 `Endpoint` 实现可以怎么写？

> **参考答案**：可以仿照 `UnitEndpoint`：内部只存一个 `SocketAddr`，`from_address/into_address` 直接存取它，`clear_src` 留空。协议核心依然能正常工作，只是失去了 sticky 发送的优化（报文可能从默认网卡发出）。

---

## 5. 综合实践

**任务**：为一个假想的 macOS 平台，写出 `PlatformTun` 与 `PlatformUDP` 的空实现骨架（所有方法用 `todo!()` 占位），并为每个关联类型写一行注释说明它对应 macOS 上的什么概念。

这个任务把本讲四个最小模块的知识串起来：你要为每个 trait 选定关联类型、把握三元组语义、并体会 macOS 与 Linux 的能力差异（macOS 没有 fwmark、sticky 机制不同）。

**示例代码**（这是为讲解而写的骨架，**不是项目原有代码**）：

```rust
// 示例代码：假想的 macOS TUN 实现骨架（放入 src/platform/macos/tun.rs）
use super::super::tun::*; // 引入 Tun, Reader, Writer, Status, PlatformTun, TunEvent

pub struct MacTun;        // 对应 Tun：一张 utun 虚拟网卡
pub struct MacTunReader;  // 对应 Reader：utun 的读端（每条队列一个）
pub struct MacTunWriter;  // 对应 Writer：utun 的写端（共享）
pub struct MacTunStatus;  // 对应 Status：通过 AF_ROUTE 的 RTM_IFINFO 感知 up/down

impl Reader for MacTunReader {
    type Error = std::io::Error;
    fn read(&self, buf: &mut [u8], offset: usize) -> Result<usize, Self::Error> {
        // macOS 的 utun 每个包前面带 1 字节协议族（PF_INET/PF_INET6），
        // 因此 offset 至少要预留这 1 字节。
        todo!("从 utun fd 读一个 IP 包到 buf[offset:]")
    }
}

impl Writer for MacTunWriter {
    type Error = std::io::Error;
    fn write(&self, src: &[u8]) -> Result<(), Self::Error> {
        todo!("向 utun fd 写一个 IP 包，首字节需补协议族前缀")
    }
}

impl Status for MacTunStatus {
    type Error = std::io::Error;
    fn event(&mut self) -> Result<TunEvent, Self::Error> {
        todo!("阻塞监听 AF_ROUTE 路由套接字，返回 Up(mtu)/Down")
    }
}

impl Tun for MacTun {
    type Writer = MacTunWriter;
    type Reader = MacTunReader;
    type Error = std::io::Error;
}

impl PlatformTun for MacTun {
    type Status = MacTunStatus;
    fn create(name: &str) -> Result<(Vec<Self::Reader>, Self::Writer, Self::Status), Self::Error> {
        // macOS 用 socket(AF_SYSTEM, SYSPROTO_CONTROL) + ctl 命令打开 utunN，
        // name 参数在 macOS 上通常被忽略（utun 的编号由内核分配）。
        todo!("打开 utun 设备，返回 (readers, writer, status) 三元组")
    }
}
```

```rust
// 示例代码：假想的 macOS UDP 实现骨架（放入 src/platform/macos/udp.rs）
use super::super::udp::*;   // 引入 UDP, Reader, Writer, Owner, PlatformUDP
use super::super::Endpoint; // 引入 Endpoint trait
use std::net::SocketAddr;

pub struct MacUDP;                  // 对应 UDP
pub struct MacUDPReader;            // 对应 Reader
pub struct MacUDPWriter;            // 对应 Writer
pub struct MacOwner;                // 对应 Owner：持有 socket，Drop 时关闭
pub struct MacEndpoint(SocketAddr); // 对应 Endpoint：macOS 无 sticky，只存目的地址

impl Endpoint for MacEndpoint {
    fn from_address(addr: SocketAddr) -> Self { MacEndpoint(addr) }
    fn into_address(&self) -> SocketAddr { self.0 }
    fn clear_src(&mut self) {} // macOS 不维护 sticky 源，空操作
}

impl Reader<MacEndpoint> for MacUDPReader {
    type Error = std::io::Error;
    fn read(&self, buf: &mut [u8]) -> Result<(usize, MacEndpoint), Self::Error> {
        // macOS 用 IP_RECVDSTADDR（而非 Linux 的 IP_PKTINFO）获取目的地址辅助信息。
        todo!("recvfrom 读一个密文报文，返回 (长度, 对端地址)")
    }
}

impl Writer<MacEndpoint> for MacUDPWriter {
    type Error = std::io::Error;
    fn write(&self, buf: &[u8], dst: &mut MacEndpoint) -> Result<(), Self::Error> {
        todo!("sendto 把密文发往 dst")
    }
}

impl Owner for MacOwner {
    type Error = std::io::Error;
    fn get_port(&self) -> u16 { todo!("返回绑定的本地端口") }
    fn set_fwmark(&mut self, _value: Option<u32>) -> Result<(), Self::Error> {
        // ⚠️ macOS 没有 Linux 的 SO_MARK/fwmark 概念，
        // 这里要么恒定 Ok、要么返回「不支持」错误——这是平台能力差异的真实体现。
        todo!("macOS 无 fwmark，需决定如何处理")
    }
}

impl UDP for MacUDP {
    type Error = std::io::Error;
    type Endpoint = MacEndpoint;
    type Writer = MacUDPWriter;
    type Reader = MacUDPReader;
}

impl PlatformUDP for MacUDP {
    type Owner = MacOwner;
    fn bind(port: u16) -> Result<(Vec<Self::Reader>, Self::Writer, Self::Owner), Self::Error> {
        // 分别 bind 一个 IPv4 和一个 IPv6 UDP 套接字，复用同一端口。
        todo!("bind UDP 套接字，返回 (readers, writer, owner) 三元组")
    }
}
```

**操作步骤**：

1. 在本地建一个 `src/platform/macos/` 目录，把上面两段骨架分别放入 `tun.rs` 和 `udp.rs`，并补一个 `mod.rs`（参照 [src/platform/linux/mod.rs:1-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/mod.rs#L1-L7) 的写法）。
2. 在 [src/platform/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs) 增加一条 `#[cfg(target_os = "macos")] pub mod macos;` 和对应的 `pub use macos as plt;`。
3. 运行 `cargo check --target x86_64-apple-darwin`（若本地无该 target，则「待本地验证」，改为人工核对每个 trait 方法都被覆盖）。

**需要观察的现象**：编译器会逐个提示你哪些 trait 方法还没实现、哪些关联类型还没指定——这正是 trait 契约在「强迫」你把平台能力交代清楚。

**预期结果**：你得到一个能在 macOS 上 `cargo check` 通过的骨架（方法体为 `todo!()`）。重点不是让它跑起来，而是通过填空的过程，把「TUN/UDP/Endpoint 各需要哪些能力、macOS 上对应什么、与 Linux 有何差异」彻底理清。`set_fwmark` 这一处尤其值得思考：它暴露了「抽象统一接口、但平台能力并不对等」的现实。

> 说明：本实践仅用于学习 trait 设计，**不要**把 `macos` 模块提交进真实仓库，也不要修改 `src/platform/mod.rs` 之外的项目源码。本讲禁止修改源码。

## 6. 本讲小结

- platform 层用一组 trait 把协议核心与操作系统 IO 解耦：`Tun`、`udp::UDP`、`uapi::PlatformUAPI`、`Endpoint`，核心 `WireGuard<T: Tun, B: UDP>` 只依赖这些 trait。
- 选择「泛型 + 关联类型」而非 trait 对象，是为了数据面热路径的零成本抽象，并让配套的 `Reader/Writer` 在编译期绑定、杜绝错配。
- TUN 侧 `PlatformTun::create` 返回 `(Vec<Reader>, Writer, Status)`：多队列读 + 单写 + 状态感知；`Reader::read` 的 `offset` 参数为就地构造报文预留前缀。
- UDP 侧 `PlatformUDP::bind` 返回 `(Vec<Reader>, Writer, Owner)`：多队列读 + 单写 + socket 所有权（Drop 关闭、可设 fwmark）；`Writer::write` 接收 `&mut E` 以支持 sticky 源失效后清源重发。
- UAPI 侧最简：`PlatformUAPI::bind` 返回 `Bind`，`BindUAPI::connect` 返回一条 `Read + Write` 的字节流。
- `Endpoint` trait 把「对端地址」平台化，容纳 sticky 源信息；dummy 的 `UnitEndpoint` 用退化实现证明 trait 边界恰到好处。
- `#[cfg]` + `pub use ... as plt` 把当前平台实现统一别名化，`main.rs` 通过 `plt::Tun/UAPI/UDP` 访问，平台切换只改 `platform/mod.rs` 一处。

## 7. 下一步学习建议

本讲只读了「契约」，没读「履约」。建议接着：

- **u2-l2 Linux TUN 设备实现**：看 `LinuxTun` 如何用 `/dev/net/tun` + ioctl 与 netlink 兑现 `PlatformTun` 和 `Status::event` 的承诺。
- **u2-l3 Linux UDP 绑定与 sticky socket**：看 `LinuxUDP` 如何用 `IP_PKTINFO` 兑现 sticky 发送与 `clear_src` 重发。
- **u2-l4 Dummy 纯测试平台**：看 dummy 如何用内存通道兑现全部 trait，使协议核心可在单元测试里端到端跑通。

读完这三篇，再回过头看本讲的 trait 定义，你会对每一个方法签名背后的现实约束有更具体的体会。
