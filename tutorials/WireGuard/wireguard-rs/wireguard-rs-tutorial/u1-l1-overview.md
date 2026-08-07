# 项目定位：WireGuard 与 wireguard-rs

> 本讲是整本学习手册的第一篇。目标只有一个：让你在「不写一行代码」的情况下，搞清楚这个项目到底实现了什么、和官方内核版 WireGuard 是什么关系、它在运行时和外部世界靠哪几个接口打交道。后续每一篇讲义都会以本讲建立的「全局视角」为地基。

## 1. 本讲目标

学完本讲，你应当能够：

- 用自己的话说出 WireGuard 的核心思想：**握手（密钥协商）与数据面（报文加解密/路由）分离**，二者都跑在 **UDP 隧道** 之上，握手协议是 **Noise IK**。
- 说清 `wireguard-rs` 这个仓库的定位：它是 WireGuard 的**纯 Rust 用户态实现**，对标官方内核模块，README 明确建议「Linux 上请用内核模块，不要跑这个」。
- 看懂 README 中 `Usage` / `Building` / `Architecture` 三段，并理解它如何与 `wg(8)`、`ip(8)` 这些既有命令配合工作。
- 在源码层面指出 `src/main.rs` 顶层声明的三大模块 `configuration`、`platform`、`wireguard`，并说清它们各自的职责与依赖方向。

## 2. 前置知识

本讲面向「零项目背景」的读者，但下面几个通用概念会帮你更顺地理解：

- **VPN 隧道**：把一种网络报文（比如 WireGuard 自己的密文报文）封装在另一种报文（比如 UDP）里传输，对端解封装还原。WireGuard 走的就是 UDP 隧道。
- **对称加密 vs 非对称加密**：握手阶段用非对称密码学（这里指基于 Curve25519 的 DH）协商出一个「会话密钥」；之后真正的数据报文用这个会话密钥做对称加密（ChaCha20-Poly1305）。本讲只要理解「先握手换密钥，再用密钥加解密」这个分工即可。
- **TUN 设备**：操作系统提供的一种「虚拟网卡」。程序可以像读写文件一样从 TUN 读出「内核想发出去的 IP 包」，也可以把「收到的 IP 包」写回 TUN 让内核以为是从网卡收到的。它是 WireGuard 接入操作系统网络栈的入口。
- **守护进程（daemon）**：在后台运行的进程。本项目会默认 fork 到后台（除非加 `--foreground`），并在「创建完需要特权的 TUN/UAPI 资源后」放弃 root 权限。
- **Rust 的 `mod`**：Rust 用 `mod foo;` 声明一个子模块。本讲我们只看顶层 `mod` 声明来建立模块地图，不涉及 Rust 语法细节。

> 你不需要已经懂 Noise 协议、ioctl、netlink、zerocopy——这些都是后续讲义的主题。本讲只要求你建立「大局观」。

## 3. 本讲源码地图

本讲只涉及两个文件，外加一张架构图，但它们是理解整个项目的「索引」：

| 文件 / 资源 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| [README.md](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md) | 项目的使用说明、平台支持、构建方式、架构概述 | 重点读 `Usage`、`Platforms`、`Architecture` 三段 |
| [architecture.svg](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/architecture.svg) | README 里引用的架构图（draw.io 导出） | 读懂其中「握手模块 / 路由器模块 / 密钥材料 / 定时器 / TUN / Internet(UDP)」的箭头 |
| [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) | 程序入口，把三大模块拼装起来并启动工作线程 | 用顶层 `mod` 声明和 `main()` 的步骤建立「三大模块」映射 |

一句话的依赖方向（后续讲义会反复用到）：

```
configuration  ──使用──▶  wireguard  ──使用──▶  platform
   (配置/UAPI)            (纯协议核心)        (OS 抽象：TUN/UDP/UAPI)
```

即：`configuration` 在最上层、负责对外配置接口；`wireguard` 是不依赖任何具体操作系统的纯协议核心；`platform` 在最底层、把协议核心和真实的 Linux（或测试用 dummy）IO 绑定起来。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 WireGuard 协议核心思想与 wireguard-rs 的定位**——回答「它实现了什么、为什么是这样设计的、和内核版什么关系」。
- **4.2 运行方式与三大外部接口**——回答「它怎么跑起来、靠哪几个接口和外界打交道」。

### 4.1 WireGuard 协议核心思想与 wireguard-rs 的定位

#### 4.1.1 概念说明

**WireGuard 是什么？**

WireGuard 是一种现代 VPN 协议（也是一个项目名）。它的设计哲学可以用三个词概括：**简单、快速、安全**。和 IPSec/OpenVPN 相比，它的代码量极小、协议状态机极简，但却用到了很现代的密码学。

理解 WireGuard，关键在于抓住一个核心思想——**握手与数据面分离**：

- **握手（Handshake）**：两个对端在开始通信前，先用非对称密码学（Noise IK 模式）互相验证身份，并协商出一组对称的「会话密钥」。握手是低频的——大约每隔几分钟才做一次，或者密钥快用尽时才重新握手。
- **数据面 / 报文保护器（Packet Protector）**：握手产出的会话密钥，被交给「路由器（Router）」模块，用于对每一个真正的传输报文（即 IP 数据包）做对称加解密和路由选择。数据面是高频的——每一个进出的 IP 包都要走这里。

为什么要把它们分开？因为这两件事的**性能特征完全不同**：握手计算重但调用少，数据面计算相对轻但调用极频繁。分开后，可以让数据面跑在多线程并行管道里榨干 CPU，而握手用单独的工作线程驱动状态机。README 的 Architecture 段正是这样描述的：

> separating the handshake code from the packet protector. The handshake module implements an authenticated key-exchange (NoiseIK), which provides key-material, which is then consumed by the router module (packet protector) ...

**wireguard-rs 这个仓库的定位**

`wireguard-rs` 是 WireGuard 的**纯 Rust 实现**，运行在**用户态（userspace）**。它对标的是官方的、跑在内核里的 WireGuard 模块。但要注意 README 里一个非常明确的警告（这一句决定了它的适用场景）：

> This will run on Linux; however YOU SHOULD NOT RUN THIS ON LINUX. Instead use the kernel module

也就是说：在 Linux 上，官方推荐用内核模块（性能更好、更成熟）；`wireguard-rs` 的真正价值在于那些**没有内核模块的平台**（README 列出 Windows / FreeBSD / OpenBSD 为 "Coming soon"），以及作为**可被第三方程序当作库嵌入**的纯协议核心。事实上，本仓库的协议核心 `wireguard` 模块被刻意写成了「不依赖任何具体 IO 实现」的形态，这样它既能在生产里配上 `platform::linux` 跑，也能在单元测试里配上 `platform::dummy` 端到端跑通——这点我们会在后面很多讲义里反复用到。

#### 4.1.2 核心流程

从「全局视角」看，一个 WireGuard 实例的生命周期可以抽象成下面这条流水线（本讲只看大局，细节都是后续讲义）：

```
┌──────────────── 控制面（低频）────────────────┐
│  握手模块(Noise IK) ──产出会话密钥──▶ 路由器     │
└──────────────────────────────────────────────┘
          ▲                                  │
          │ 定时器：密钥过期/重传               │ 用密钥加解密
          │                                  ▼
┌──────────────── 数据面（高频）────────────────┐
│  路由器：对传输报文 加密/解密 + 按路由表选 peer  │
└──────────────────────────────────────────────┘
```

对应 README 引用的 `architecture.svg`，里面画了这样几个组件和箭头（这里把 SVG 里的文字标签整理成文字描述，你可以在仓库根目录打开这张图对照）：

- 顶部两个 `Internet` 方框代表 UDP 报文的出入口（`Read UDP Datagram` / `Write UDP Datagram`）。
- 中间一个 `Packet Demultiplexer`（报文分用器）：把进来的 UDP 报文按类型分流——握手报文（`Hanshake Messages`，原图拼写如此）送给左侧 `Handshake Module`；传输报文（`Transport Messages`）送给右侧 `Router Module`。
- 左侧 `Handshake Module` 与右侧 `Router Module` 之间有一条 `Key Material`（密钥材料）箭头——这正是「握手产出密钥，喂给数据面」。
- 底部 `Timers`：路由器的收发事件（`Send / Recv Events`）会驱动定时器；定时器在适当时机要求握手模块重新握手（`Request New Handshake`）。
- 右下角 `TUN Device`：`Write IP Packet` / `Read IP Packet`，对应解密后写回内核的 IP 包，以及从内核读出待加密的 IP 包。

这一整块（分用器 + 握手 + 路由器 + 定时器）就是被 `architecture.svg` 标注为 `WireGuard Module` 的大背景框，也就是源码里的 `src/wireguard/` 这棵子树。

如果用一句「公式」概括数据面吞吐，可以这样理解（仅示意密钥生命周期约束，不展开）：

\[ \text{单条隧道寿命} \approx \min(\text{密钥有效期},\; \text{最大报文数上限} / \text{报文速率}) \]

即一条会话密钥不能无限用下去，到点（时间或报文计数）就得让定时器触发重新握手——这正是 `Timers` 在图里的职责。细节会在「定时器」讲义里讲。

#### 4.1.3 源码精读

我们先不进 `src/wireguard/` 内部，只看 `main.rs` 顶层的模块声明，就能验证「握手与路由器分离」在源码层是真实存在的。

[src/main.rs:11-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L11-L13) 声明了整个程序的三个顶层模块——这是整本手册最重要的三行：

```rust
mod configuration;
mod platform;
mod wireguard;
```

- `configuration`：对外的配置接口（UAPI 文本协议、`WireGuardConfig`）。
- `platform`：平台抽象（Linux / dummy 的 TUN、UDP、UAPI 实现）。
- `wireguard`：纯 WireGuard 协议核心（本讲主角）。

[src/wireguard/mod.rs:1-6](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/mod.rs#L1-L6) 用文档注释直接点明了这个核心模块的「纯」属性，并说明了它的内部组成：

```rust
/// The wireguard sub-module represents a full, pure, WireGuard implementation:
///
/// The WireGuard device described here does not depend on particular IO implementations
/// or UAPI, and can be instantiated in unit-tests with the dummy IO implementation.
///
/// The code at this level serves to "glue" the handshake state-machine
/// and the crypto-key router code together,
```

注意最后一句「glue the handshake state-machine and the crypto-key router code together」——它和 README 的「separating the handshake from the packet protector」是完全一致的：协议核心内部就是「握手状态机 + 路由器」两块，再加上把它们粘起来的胶水层。本仓库的 `src/wireguard/` 目录下确实同时存在 `handshake/` 与 `router/` 两个子目录（你会在 4.1.4 自己去验证这一点）。

最后，关于「用户态实现、对标内核模块」这一点，README 的平台说明最直白：[README.md:20-21](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md#L20-L21) 明确写出 Linux 上「能跑但不建议跑，请改用内核模块」。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手验证「握手与数据面分离」不是一句空话。

1. **实践目标**：在源码目录里定位「握手模块」和「路由器模块」两个子树，并确认它们各自独立存在。
2. **操作步骤**：
   - 在仓库根目录执行（只读命令，不会改动任何文件）：
     ```bash
     git ls-files src/wireguard/handshake
     git ls-files src/wireguard/router
     ```
   - 观察这两个子目录分别包含哪些 `.rs` 文件。
3. **需要观察的现象**：
   - `handshake/` 下应能看到如 `noise.rs`（Noise 协议）、`device.rs`、`messages.rs`、`macs.rs`、`ratelimiter.rs`、`timestamp.rs` 等——对应「握手状态机 + 抗 DoS + 速率限制」。
   - `router/` 下应能看到如 `send.rs`、`receive.rs`、`route.rs`、`anti_replay.rs`、`queue.rs`、`ip.rs` 等——对应「加密/解密 + 路由表 + 防回放 + 有序队列」。
   - 两个子目录**没有循环地互相直接依赖**（握手产出密钥交给路由器，是单向的）。
4. **预期结果**：你能据此填出下表（第一行已示范）：

   | 子树 | 代表文件 | 解决的问题 |
   | --- | --- | --- |
   | `handshake/` | `noise.rs` | Noise IK 密钥协商 |
   | `router/` | `send.rs` / `receive.rs` | ?（请你填写） |

5. 如果你无法运行 `git ls-files`，可改为用文件浏览器查看 `src/wireguard/` 目录结构；结果一致。本步骤**待本地验证**你实际看到的文件列表。

#### 4.1.5 小练习与答案

**练习 1**：README 说握手模块产出「key-material」交给路由器。结合本讲，握手和数据面分别用的是哪种类型的密码学（对称 / 非对称）？

> **参考答案**：握手阶段以非对称密码学为主（Curve25519 DH 交换、签名/MAC 认证），目的是协商出对称的会话密钥；数据面则用这个对称会话密钥做对称加解密（ChaCha20-Poly1305）。简单记：「非对称握手换密钥，对称密钥加解密数据」。

**练习 2**：为什么 README 强烈建议 Linux 用户「不要跑 wireguard-rs，改用内核模块」？请从「实现形态」角度回答。

> **参考答案**：`wireguard-rs` 是**用户态**实现，需要把报文在内核与用户态之间来回拷贝（经过 TUN），上下文切换和数据拷贝开销大；内核模块直接在内核网络栈里加解密，性能更高、更成熟。`wireguard-rs` 的价值在于没有内核模块的平台，以及作为可嵌入的纯协议库。

### 4.2 运行方式与三大外部接口

#### 4.2.1 概念说明

`wireguard-rs` 不是一个「自己解析配置文件」的程序——它把**配置这件事完全交给了官方的 `wg(8)` 工具**，自己只提供一个叫做 **UAPI** 的文本控制接口。这是它「能与既有命令配合工作」的关键。

理解本节，要分清两类「接口」：

- **数据面接口**：承载真正的 VPN 流量。
  - **TUN**：与操作系统网络栈交换「明文 IP 包」。出站：内核把目标 IP 包送进 TUN，程序读出来准备加密；入站：程序把解密后的 IP 包写回 TUN，内核据此投递给上层应用。
  - **UDP**：与互联网交换「WireGuard 密文报文」。所有握手报文和加密后的传输报文都走 UDP（WireGuard 的 server/client 其实是对等的，都监听 UDP 端口）。
- **控制面接口**：
  - **UAPI**：一个**文本协议**（运行时通常绑定在 `/var/run/wireguard/<接口名>.sock` 这个 Unix 域套接字上）。`wg(8)` 就是往这个 socket 写入形如 `set=1\nprivate_key=...\n` 的文本来配置密钥、peer、allowed-ips 等，`wg show` 则通过它读取状态。本仓库的 `configuration::uapi::handle` 就是这个文本协议的解析/处理入口。

为什么这样设计？因为 WireGuard 把「配置协议」标准化成了 UAPI 文本，于是任何实现（内核模块也好、wireguard-rs 也好）只要实现 UAPI，就能直接复用 `wg(8)`、`ip(8)`、`ifconfig(8)` 等现成工具，用户无需学习新的配置语法。

#### 4.2.2 核心流程

把 4.2.1 的三个接口和 4.1 的协议核心叠在一起，运行时的大致流程是：

```
            控制面                            数据面
┌─────────────────────────┐      ┌──────────────────────────────┐
│ wg(8) ──文本协议──▶ UAPI │      │  明文 IP 包 ◀──▶ TUN          │
│        socket            │      │  密文 UDP 报文 ◀──▶ UDP socket │
└────────────┬─────────────┘      └────────────┬──────────────────┘
             │ 配置/查询                       │ 读 IP包 / 收发 UDP
             ▼                                 ▼
     ┌──────────────────────────────────────────────┐
     │             configuration  ──▶ wireguard     │
     │             (解析 UAPI, 下发配置)  (协议核心)   │
     │                              ──▶ platform     │
     │                                 (真实 TUN/UDP) │
     └──────────────────────────────────────────────┘
```

具体的启动顺序（`main()` 里）是：

1. 解析命令行参数，拿到**接口名**（如 `wg0`）。
2. **先绑定 UAPI socket**（占用 `/var/run/wireguard/wg0.sock`）。
3. **再创建 TUN 设备**（占用网卡名 `wg0`）。
4. 这两步都成功后，**放弃 root 权限**（因为已经拿到了特权资源）。
5. **默认 fork 到后台**成为守护进程（除非 `--foreground`）。
6. 创建 `WireGuard` 协议核心设备，把 TUN 的读端注册进去，再用 `configuration::WireGuardConfig` 包一层。
7. 起一个线程监听 TUN 的 Up/Down 事件；起一个线程接受 UAPI 连接并处理配置。
8. 主线程 `wg.wait()` 阻塞，直到所有 TUN reader 结束。

值得注意的一个细节（后面讲义会展开）：**`main.rs` 里并没有直接创建 UDP socket**。TUN 和 UAPI 是在启动时创建的；而 UDP 套接字是「延迟绑定」的——当 `wg(8)` 通过 UAPI 配置了 `listen_port` 之后，才由配置层调用 `start_listener` 真正去 `bind` UDP（见 4.2.3）。这一点经常被初学者忽略，但理解了它，你才明白为什么 `main.rs` 里只看到 UAPI 和 TUN 两个外部资源。

#### 4.2.3 源码精读

我们逐段对照 `main.rs`，把它和 4.2.2 的步骤一一对应。

**步骤 1：命令行解析**。[src/main.rs:56-83](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L56-L83) 手写解析（注意它没用 clap），只认三种参数：`--foreground`/`-f`、`--disable-drop-privileges`，以及「第一个非选项参数 = 接口名」。没有接口名就报错退出。

**步骤 2：绑定 UAPI socket**。[src/main.rs:85-89](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L85-L89)：

```rust
let uapi = plt::UAPI::bind(name.as_str()).unwrap_or_else(|e| {
    eprintln!("Failed to create UAPI listener: {}", e);
    exit(-2);
});
```

这里的 `plt::UAPI` 是平台别名——见 [src/platform/mod.rs:15-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs#L15-L16) 的 `pub use linux as plt;`（仅在 `target_os = "linux"` 下生效）。所以 `plt::UAPI` 实际就是 `platform::linux::UAPI`，它会把 Unix 域 listener 绑到 `/var/run/wireguard/<name>.sock`。这正好对应 README 里说的「删掉这个 socket 就能让 wireguard-rs 退出」。

**步骤 3：创建 TUN 设备**。[src/main.rs:91-95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L91-L95)：

```rust
let (mut readers, writer, status) = plt::Tun::create(name.as_str()).unwrap_or_else(|e| { ... });
```

`plt::Tun::create` 一次返回三件套：多个 reader（读 IP 包，按 CPU 核数开）、一个 writer（写回 IP 包）、一个 status（用来收 Up/Down 与 MTU 事件）。这套「(readers, writer, status) 三元组」语义是整个平台抽象的核心约定，后续「平台抽象层」单元会专门讲。

**步骤 6：创建协议核心 + 配置外壳**。[src/main.rs:131-139](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L131-L139)：

```rust
let wg: WireGuard<plt::Tun, plt::UDP> = WireGuard::new(writer);
while let Some(reader) = readers.pop() {
    wg.add_tun_reader(reader);
}
let cfg = configuration::WireGuardConfig::new(wg.clone());
```

注意类型标注 `WireGuard<plt::Tun, plt::UDP>`——这就是「把纯协议核心与平台 IO 绑定」的那一行：泛型参数把抽象的 `Tun`/`UDP` trait 指定为 Linux 的具体实现。`WireGuardConfig` 则包在外面，供 UAPI 调用。

**步骤 7：两个工作线程**。TUN 事件线程 [src/main.rs:141-162](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L141-L162) 监听 Up/Down，把它翻译成 `cfg.up(mtu)` / `cfg.down()`。UAPI 服务线程 [src/main.rs:164-180](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L164-L180) 每接受一个连接，就 `thread::spawn` 一个新线程调用 `configuration::uapi::handle(&mut stream, &cfg)` 来处理这一次 `wg(8)` 请求。

**UDP 的延迟绑定**（呼应 4.2.2 的细节）。`main.rs` 全文搜不到 `bind` UDP 的代码；UDP 是在配置层 `start_listener` 里，当设置 `listen_port` 时才绑定的：[src/configuration/config.rs:195-216](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L195-L216) 关键三步是 `B::bind(cfg.port)` → `cfg.wireguard.set_writer(writer)` → `cfg.wireguard.add_udp_reader(reader)`。

最后，README 的 `Usage` 段 [README.md:5-14](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md#L5-L14) 总结了用户视角的运行方式：`ip link add wg0 type wireguard`（内核版）换成 `wireguard-rs wg0`（本程序）；删接口用 `ip link del wg0`，或删除 `/var/run/wireguard/wg0.sock` 让程序自行退出；配置则照常用 `wg(8)`、`ip(8)`、`ifconfig(8)`。

#### 4.2.4 代码实践

这是一个**调用链跟踪型实践**，帮助你把「三个外部接口」和源码对应起来。

1. **实践目标**：不实际运行程序，仅靠阅读 `main.rs`，排出「UAPI / TUN / UDP」三个接口各自的创建位置和创建时机。
2. **操作步骤**：
   - 打开 [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs)。
   - 分别找到：UAPI 的绑定（`plt::UAPI::bind`）、TUN 的创建（`plt::Tun::create`）、以及「UDP 在哪里绑定」——你会发现 `main.rs` 里没有，需要跳到 [src/configuration/config.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs) 的 `start_listener`。
   - 记录每一步相对 `main()` 执行顺序的位置（是在 `WireGuard::new` 之前还是之后？在降权之前还是之后？）。
3. **需要观察的现象**：
   - UAPI 与 TUN 都在**降权之前**创建（因为需要 root）；UDP 则在**程序启动很久之后**、由 `wg(8)` 触发配置时才创建。
   - 降权（`util::drop_privileges`）发生在拿到 UAPI 与 TUN 之后、daemonize 之前。
4. **预期结果**：你能填出这张时序表（顺序从上到下）：

   | 顺序 | 接口/动作 | 源码位置 | 是否需要 root |
   | --- | --- | --- | --- |
   | 1 | 绑定 UAPI | `main.rs:86` | 是 |
   | 2 | 创建 TUN | `main.rs:92` | 是 |
   | 3 | ? | ? | ? |
   | 4 | ? | ?（延迟，由 UAPI 配置触发） | 否 |

5. 本实践为纯阅读型，**无需运行程序**；若想真正运行，请先看下一讲「构建与运行」。

#### 4.2.5 小练习与答案

**练习 1**：用户运行 `wg set wg0 private-key <file> listen-port 51820 peer <pubkey> ...` 后，这条命令是如何「到达」wireguard-rs 进程内部的？

> **参考答案**：`wg(8)` 把这条命令翻译成 UAPI 文本协议，连接到 `/var/run/wireguard/wg0.sock`，写入 `set=1\nprivate_key=...\nlisten_port=51820\n...`。wireguard-rs 的 UAPI 服务线程（`main.rs:164-180`）accept 这个连接，在新线程里调用 `configuration::uapi::handle` 逐行解析并下发配置。其中 `listen_port` 的设置会触发 `start_listener` 真正去 bind UDP。

**练习 2**：为什么 `main.rs` 在创建完 UAPI 和 TUN 之后要立即 `drop_privileges`，而不是在程序最后才降权？

> **参考答案**：因为创建 UAPI socket（绑定 `/var/run/wireguard/`）和 TUN 设备都需要 root 权限。一旦这两件需要特权的资源已经拿到，就应当**立刻**放弃 root，转成 `nobody` 用户运行，以缩小后续（处理网络报文、解析对端输入）时的攻击面——万一协议核心出现漏洞，攻击者拿到的是一个低权限进程。细节会在「特权降级与守护进程化」讲义展开。

**练习 3**：如果系统不支持 `ip link del wg0` 删除网卡，README 给的替代关闭方式是什么？它利用了哪个接口？

> **参考答案**：执行 `rm -f /var/run/wireguard/wg0.sock`。它删除的是 **UAPI** 的 Unix 域套接字文件；wireguard-rs 检测到 UAPI listener 失效后会自行关闭。这恰好说明 UAPI socket 不只是配置通道，还兼任了「进程存活锚点」的角色。

## 5. 综合实践

> 这是本讲的核心任务，综合 4.1 与 4.2 的全部内容。完成后，你就拥有了贯穿后续所有讲义的「全局地图」。

**任务**：阅读 [README.md](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md)（特别是 `Architecture` 段，[README.md:45-56](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md#L45-L56)）与 [architecture.svg](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/architecture.svg)，用一段文字（或一张文字图）画出 `wireguard-rs` 的**三大顶层模块**（`platform` / `configuration` / `wireguard`）之间的数据流向，并在图上**明确标注** `TUN`、`UDP`、`UAPI` 三个外部接口的位置。

**建议步骤**：

1. 先画三个大方框：`configuration`（上）、`wireguard`（中）、`platform`（下）。
2. 在 `platform` 边缘画三个「外部世界」出入口：`TUN`、`UDP`、`UAPI`，并标注它们各自交换的是「明文 IP 包 / 密文 UDP 报文 / 配置文本」。
3. 在 `wireguard` 内部，对照 `architecture.svg` 画出 `Packet Demultiplexer`、`Handshake`、`Router`、`Timers`，并用箭头标出：
   - `Handshake ──密钥──▶ Router`
   - `Demux ──握手报文──▶ Handshake`，`Demux ──传输报文──▶ Router`
   - `Router ──收发事件──▶ Timers ──请求重握──▶ Handshake`
4. 用箭头把三大模块和 `architecture.svg` 连起来：`configuration` 通过 UAPI 控制 `wireguard`；`wireguard` 的数据进出都经过 `platform` 的 TUN/UDP。
5. 在图下方写一段**不超过 6 行**的中文说明，解释一个「出站 IP 包」从内核到达对端的完整路径：`TUN读出 → wireguard加密 → UDP发出`；以及一个入站密文报文的反向路径。

**参考答案（文字版数据流向图）**：

```
                         ┌──────────────┐
        wg(8) ──文本──▶  │   UAPI 接口   │  (控制面, /var/run/wireguard/wg0.sock)
                         └──────┬───────┘
                                │ 配置/查询
                         ┌──────▼───────┐
                         │ configuration │  (解析 UAPI 文本, 下发配置, 延迟 bind UDP)
                         └──────┬───────┘
                                │ 使用
                ┌───────────────▼────────────────┐
   密钥流向▶   │  wireguard (纯协议核心)          │
                │  ┌──────────┐ Handshake ─密钥─▶ Router │
                │  │Packet Dem│ (Noise IK)        (加解密+路由)  │
                │  └────┬─────┘     ▲                 │          │
                │       │           └──重握手── Timers│          │
                └───────┼────────────────────────────-┘
                        │ 使用(平台IO)
                ┌───────▼───────────┐
                │     platform       │  (linux 实现 / dummy 测试实现)
                └───┬────────────┬───┘
   明文IP包 ◀──▶ TUN │            │ UDP ◀──▶ 密文UDP报文
        (与内核网络栈)            (与互联网/对端)
```

说明（出站）：应用发出的明文 IP 包经内核进入 **TUN** → `platform` 读出 → `wireguard` 的 Router 用会话密钥加密 → 经 **UDP** 发往对端。
说明（入站）：对端的密文 UDP 报文经 **UDP** → `platform` 读出 → `wireguard` 的 Demux 分流（握手报文给 Handshake 换密钥，传输报文给 Router 解密）→ 解密后的明文 IP 包经 **TUN** 写回内核。**UAPI** 全程不参与数据面，只负责 `wg(8)` 的配置与状态查询。

## 6. 本讲小结

- WireGuard 的核心思想是**握手与数据面分离**：握手模块用 Noise IK 协商会话密钥，路由器（packet protector）用该密钥加解密传输报文，二者都跑在 UDP 隧道之上。
- `wireguard-rs` 是 WireGuard 的**纯 Rust 用户态实现**，README 明确建议 Linux 用户改用内核模块；它的价值在没有内核模块的平台，以及作为可嵌入的纯协议库。
- 协议核心 `src/wireguard/` 被**刻意写成不依赖具体 IO**，内部即「握手 + 路由器 + 胶水层 + 定时器」，既能配 `platform::linux` 跑生产，也能配 `platform::dummy` 跑测试。
- `main.rs` 顶层声明了三大模块：`configuration`（上）→ `wireguard`（中）→ `platform`（下），依赖方向自上而下。
- 运行时存在三个外部接口：**UAPI**（控制面，绑 `/var/run/wireguard/<name>.sock`，对接 `wg(8)`）、**TUN**（数据面，明文 IP 包）、**UDP**（数据面，密文 WireGuard 报文，且是延迟绑定）。
- 启动顺序要点：先绑 UAPI、再建 TUN（均需 root）→ 立即降权 → 默认 daemonize → 创建协议核心与配置外壳 → 起 TUN 事件线程与 UAPI 服务线程 → `wg.wait()` 阻塞。

## 7. 下一步学习建议

有了本讲的「全局地图」，接下来的学习顺序是：

- **下一讲 u1-l2《构建与运行》**：动手 `cargo build --release`，搞懂 `Cargo.toml` 的依赖（哪些做密码学、哪些做并发）与 features，真正把程序跑起来。
- **u1-l3《目录结构与模块地图》**：从 `main.rs` 出发，系统地把 `src/` 每个目录映射到功能模块——这是后续所有源码讲义的索引。
- **u1-l4《程序入口 main.rs 与运行生命周期》**：逐段精读 `main()`，把本讲「时序表」里的每个步骤落实到源码行号。
- 建议同时收藏两个长期参考：[README.md 的 Architecture 段](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md#L45-L56) 与 [architecture.svg](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/architecture.svg)，等学到握手（u4）、路由器（u5）、定时器（u7）时再回头看，你会对这张图有「逐组件」的理解。
