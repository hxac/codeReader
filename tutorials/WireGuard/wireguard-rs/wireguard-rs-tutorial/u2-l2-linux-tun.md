# Linux TUN 设备实现

## 1. 本讲目标

本讲是「平台抽象层」的第二篇。在上一篇（u2-l1）里，我们已经看完了 `platform::tun` 定义的一组 trait：`Tun`、`PlatformTun`、`Reader`、`Writer`、`Status`。这些 trait 只描述了「我需要一张什么样的虚拟网卡」，而没有说「这张网卡在 Linux 上到底是怎么造出来的」。

本讲就来回答这个问题。我们将精读 `src/platform/linux/tun.rs`，把上面这五个 trait 在 Linux 上的具体实现 `LinuxTun / LinuxTunReader / LinuxTunWriter / LinuxTunStatus` 全部拆开。学完后你应该能够：

- 说清楚用 `open("/dev/net/tun")` + `ioctl(TUNSETIFF)` 创建一张 TUN 网卡的完整流程，以及 `Ifreq` 结构体在内核与用户态之间传递参数的作用。
- 理解为什么用一个 `NETLINK_ROUTE` 套接字就能「被动收到」本接口的 Up/Down 事件，以及如何从 `RTM_NEWLINK` 报文里解析出 `IfInfomsg`。
- 看懂 `get_mtu` 如何借助 `SIOCGIFMTU` ioctl 查询当前 MTU。
- 把 `LinuxTunReader::read` / `LinuxTunWriter::write` 与上一篇 trait 里 `offset` 参数的设计意图对应起来。

本讲只读两个文件，但会反复回到上一篇的 trait 定义，请把 u2-l1 的结论放在手边。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫三个 Linux 内核概念。它们都是 TUN 设备能工作的前提。

### 2.1 TUN 设备与 `/dev/net/tun` 克隆设备

TUN（network **TUN**nel）是一张**虚拟网卡**。它一头挂在 Linux 网络协议栈上，和 `eth0` 一样有 IP、有路由、能被 `ip link` 管理；另一头却不连任何物理网线，而是连到一个**用户态进程**——进程从这张网卡「读」到的，是协议栈要发出去的 IP 包；进程往这张网卡「写」的，会被协议栈当成收到的 IP 包。

但 Linux 并不让你直接 `open("wg0")` 来创建一张 TUN 网卡。所有 TUN 设备都通过同一个**克隆设备** `/dev/net/tun` 来创建：你打开它得到一个文件描述符，再用一个 `ioctl(TUNSETIFF)` 告诉内核「请给我新建（或关联）一张名叫 `wg0` 的 TUN 设备」，内核才真正把这张网卡和这个 fd 绑定。之后对这个 fd 的 `read`/`write` 就等价于收发这张网卡的 IP 包。

### 2.2 ioctl：用户态调用内核的「万能开关」

`ioctl`（I/O control）是 Unix 里一个**大杂烩系统调用**，专门用来做 `read`/`write` 表达不了的事：设置设备参数、查询状态、切换模式等。它的调用形式是 `ioctl(fd, 请求码, 参数)`，其中：

- `fd` 是某个设备或套接字的文件描述符；
- **请求码**（如 `TUNSETIFF`、`SIOCGIFMTU`）是一个由内核约定的魔法数字，告诉内核「我要做哪件事」；
- **参数**通常指向一个 C 结构体，内核从中读入参数、或往里写结果。

本讲里你会见到三个 ioctl 请求码：`TUNSETIFF`（创建/关联 TUN 设备）、`SIOCGIFMTU`（查询网卡 MTU）。它们各自配一个不同的结构体。

### 2.3 netlink：内核主动推送消息的通道

普通系统调用都是**用户态主动问、内核答**。但「网卡被 `ip link set wg0 up` 了」这种事件是内核先发生的，用户态怎么及时知道？

答案是 **netlink**——一个专门用于内核 ↔ 用户态通信的**套接字协议族**（`AF_NETLINK`）。你创建一个 netlink 套接字、`bind` 到某个**组**（group），之后只要内核里有对应事件，就会**主动把消息推送**到你的套接字上，你用普通的 `recv` 就能读出来。

本讲用到的 `NETLINK_ROUTE` 协议负责网络配置变更，订阅了 `RTNLGRP_LINK` 等组之后，每当任何网卡链路状态变化（Up/Down、MTU 改变等），内核都会送来一条 `RTM_NEWLINK` 消息。`LinuxTunStatus::event` 就是靠这个来感知 `wg0` 的 Up/Down 的。

> 初学者术语速查：**fd**（file descriptor，文件描述符，一个整数句柄）；**MTU**（Maximum Transmission Unit，最大传输单元，网卡一次能承载的有效载荷上限字节数）；**iff** 是 `interface` 的缩写，所以 `ifi_index`/`ifi_flags` 都是「网卡」相关的字段。

## 3. 本讲源码地图

本讲涉及两个文件：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [src/platform/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs) | 定义 `Tun`/`PlatformTun`/`Reader`/`Writer`/`Status` 这些 trait 与 `TunEvent` 枚举 | 契约，复习自 u2-l1 |
| [src/platform/linux/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs) | 上述 trait 在 Linux 上的具体实现 | 本讲主角 |

`src/platform/linux/tun.rs` 内部又分成几块职责：

- `LinuxTun`（空结构体）：仅作为 `Tun`/`PlatformTun` trait 的「类型标签」，真正干活的是它的关联类型。
- `PlatformTun::create`：创建网卡，返回 `(readers, writer, status)` 三元组（呼应 u1-l4 里 `plt::Tun::create` 的返回值）。
- `LinuxTunReader` / `LinuxTunWriter`：收发 IP 包的最薄封装，直接调 `libc::read`/`libc::write`。
- `LinuxTunStatus`：持有 netlink 套接字，靠 `event()` 把 Up/Down 事件翻译成 `TunEvent`。
- `get_ifindex` / `get_mtu`：两个工具函数，分别用 `if_nametoindex` 和 `SIOCGIFMTU` 查接口编号和 MTU。

依赖方向上，本文件 `use super::super::tun::*`（即引用上一层的 trait 定义），实现这些 trait；上层 `platform/mod.rs` 用 `cfg` 把它别名化成 `plt::Tun`。这正是 u2-l1 所述「核心只依赖 trait」的落地。

## 4. 核心概念与源码讲解

### 4.1 LinuxTun::create：打开克隆设备并执行 TUNSETIFF

#### 4.1.1 概念说明

`create` 是整个 TUN 实现的入口，它要把「给一个名字 `wg0`」变成「拿到一张可读写的虚拟网卡」。这件事在 Linux 上分三步：

1. 打开克隆设备 `/dev/net/tun`，拿到一个 fd。
2. 用 `ioctl(TUNSETIFF)` 把这个名字告诉内核，让内核新建/关联一张 TUN 设备。
3. 把这一个 fd 分别包装成 `Reader`、`Writer`，再单独建一个 netlink 套接字做 `Status`，三者一起返回。

关键认知：**TUN 设备的读端和写端是同一个 fd**。对它 `read` 就是收包，对它 `write` 就是发包，内核靠调用方向区分。所以你会在源码里看到 `LinuxTunReader { fd }` 和 `LinuxTunWriter { fd }` 持有**同一个 fd 的拷贝**。

#### 4.1.2 核心流程

```
create(name)
 ├─ 构造 Ifreq { name = "wg0", flags = IFF_TUN | IFF_NO_PI }
 ├─ 名字长度校验（≤ IFNAMSIZ-1，即 15 字节）
 ├─ fd = open("/dev/net/tun", O_RDWR)        // 克隆设备
 ├─ ioctl(fd, TUNSETIFF, &Ifreq)             // 让内核造网卡
 ├─ LinuxTunStatus::new(req.name)            // 单独建 netlink 套接字
 └─ 返回 (vec![Reader{fd}], Writer{fd}, Status)
```

两个标志位的含义：

- `IFF_TUN`：要的是 **TUN** 设备（三层，传 IP 包），而不是 **TAP** 设备（二层，传以太网帧）。WireGuard 工作在三层，所以选 TUN。
- `IFF_NO_PI`：**不要** prefix 信息。默认情况下 TUN 会在每个包前加 4 字节的「packet information」（标识该包的协议等），WireGuard 不需要这个前缀，加 `IFF_NO_PI` 关掉它。这一点和上一篇 trait 里 `Reader::read` 的 `offset` 参数紧密相关——后面 4.4 会展开。

#### 4.1.3 源码精读

先看两个常量与 `Ifreq` 结构体的定义。`TUNSETIFF` 是内核约定的请求码（`0x4004_54ca`），`CLONE_DEVICE_PATH` 是克隆设备路径（末尾带 `\0` 方便直接当 C 字符串传给 `open`）：

[src/platform/linux/tun.rs:9-17](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L9-L17) —— 定义 `TUNSETIFF` 请求码、克隆设备路径，以及用于在用户态与内核间传递参数的 `Ifreq` 结构体（`name` + `flags` + 占位 `_pad`）。

`Ifreq` 用 `#[repr(C)]` 保证内存布局和 C 完全一致，这样 `ioctl` 拿到它的指针时，内核按 C 结构体的方式读写才不会错位。`_pad: [u8; 64]` 是因为内核里 `struct ifreq` 是个 union，体积比 `name + flags` 大，这里用 pad 补齐到内核期望的大小，避免 `ioctl` 越界读写。

接着看 `create` 主体：

[src/platform/linux/tun.rs:318-356](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L318-L356) —— `PlatformTun::create` 的实现：构造带 `IFF_TUN | IFF_NO_PI` 的 `Ifreq`，校验名字长度，打开克隆设备，执行 `TUNSETIFF`，最终返回 `(readers, writer, status)` 三元组。

几个要点对照源码：

- 名字长度校验 `bs.len() > libc::IFNAMSIZ - 1`（[L331-L334](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L331-L334)）：`IFNAMSIZ` 是 16，去掉结尾 `\0` 最多 15 个字符，这就是为什么 WireGuard 接口名不能太长。
- `open` 失败返回 `FailedToOpenCloneDevice`，`ioctl` 失败返回 `SetIFFIoctlFailed`（[L338-L347](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L338-L347)）。后者的错误信息特意提示 `insufficient permissions?`，因为创建 TUN 设备需要 `CAP_NET_ADMIN`——这正对应 u1-l4/u1-l5 强调的「建 TUN 必须在降权之前」。
- 返回值里 `vec![LinuxTunReader { fd }]` 带 TODO 注释 `use multi-queue for Linux`（[L351](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L351)）：目前只给了一个 reader，将来可改成多队列以提升吞吐。这也解释了为什么 trait 里 readers 是 `Vec<Self::Reader>`——为多队列预留。
- 注意 `req.name` 在 `ioctl` 之后被原样传给 `LinuxTunStatus::new`（[L353](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L353)）：内核可能把实际生效的设备名回填进 `Ifreq`，所以用 ioctl 之后的 `req.name` 而不是入参 `name`。

#### 4.1.4 代码实践

**目标**：用最小的系统调用复现 `create` 的核心两步，验证「open + TUNSETIFF」确实能造出一张网卡。

**操作步骤**（需 Linux + root；以下为示例代码，**不是项目原有代码**）：

1. 在你自己的练习目录里写一个小程序 `mk_tun.rs`，用 `libc` 打开 `/dev/net/tun` 并执行 `TUNSETIFF`。
2. 用 `cargo run`（需 root）运行，再开一个终端执行 `ip link show type tun`。

```rust
// 示例代码：仅用于理解 create 的核心两步，不是 wireguard-rs 的一部分
const TUNSETIFF: u64 = 0x4004_54ca;

#[repr(C)]
struct Ifreq {
    name: [u8; 16],
    flags: std::os::raw::c_short,
    _pad: [u8; 64],
}

fn main() {
    let mut req = Ifreq {
        name: *b"wg0\0\0\0\0\0\0\0\0\0\0\0\0\0",
        flags: (libc::IFF_TUN | libc::IFF_NO_PI) as _,
        _pad: [0; 64],
    };
    let fd = unsafe {
        libc::open(b"/dev/net/tun\0".as_ptr() as _, libc::O_RDWR)
    };
    assert!(fd >= 0, "open failed (need root?)");
    let r = unsafe { libc::ioctl(fd, TUNSETIFF as _, &mut req) };
    assert!(r >= 0, "TUNSETIFF failed");
    println!("created, fd = {}, sleeping 30s; run: ip link show wg0", fd);
    std::thread::sleep(std::time::Duration::from_secs(30));
}
```

**需要观察的现象**：在程序睡眠的 30 秒内，另一个终端 `ip link show wg0` 能看到一张名为 `wg0` 的接口；程序退出后接口消失。

**预期结果**：成功看到 `wg0: <>` 之类输出，证明「open 克隆设备 + TUNSETIFF」即可造卡。若报 `Operation not permitted`，说明没有 root/CAP_NET_ADMIN——这恰好验证了 `SetIFFIoctlFailed` 的错误提示。

**待本地验证**：上述行为依赖你的内核与权限环境，请在本地实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Ifreq` 必须加 `#[repr(C)]`？去掉会有什么风险？

> **参考答案**：`ioctl` 把结构体指针交给内核，内核按 C 的 `struct ifreq` 布局读写。Rust 默认布局允许编译器重排字段、加填充，若不加 `#[repr(C)]`，字段位置可能与内核不一致，导致名字写错位置、标志位读不到，甚至越界。`#[repr(C)` 强制与 C 一致，是跨 FFI 的硬要求。

**练习 2**：`_pad: [u8; 64]` 看似没用，去掉它程序可能仍能跑通。它存在的根本原因是什么？

> **参考答案**：内核的 `struct ifreq` 实际是一个 union，体积为 40 字节（含 16 字节 `ifr_name` + 24 字节 union）。Rust 这边只用到 `name + flags`，体积小于内核结构体。用 `_pad` 补齐到不小于内核期望的大小，是为了避免 `ioctl` 在读取/写入越界字节时触发未定义行为。即使侥幸跑通，也是「恰好」未踩雷。

---

### 4.2 LinuxTunStatus::event：netlink 监听与 IfInfomsg 解析

#### 4.2.1 概念说明

网卡造出来只是第一步，WireGuard 还需要知道这张网卡什么时候被 `up`、什么时候被 `down`——因为接口状态决定了 `mtu`，进而影响加密前的报文填充（见 u3-l2 的 `padding()`）。这个职责由 `LinuxTunStatus` 承担。

它的核心是一个 `event()` 方法，**每次调用阻塞直到拿到一个 `TunEvent`（Up 或 Down）再返回**。它不靠轮询，而是靠 2.3 节介绍的 netlink：订阅了链路事件组之后，`recv` 会在内核推送时立刻返回一条 `RTM_NEWLINK` 消息，里面带着目标网卡的标志位。

#### 4.2.2 核心流程

`event()` 的逻辑可以拆成两层循环：

```
event() 外层循环:
  ├─ 若 self.events 缓冲区里还有事件 → 弹出并返回     # 先消化缓冲
  └─ 否则 recv(netlink fd) 阻塞等内核推送
       ├─ recv 失败 → 返回 NetlinkFailure
       └─ 收到一坨字节，进入内层循环逐条解析:
            对每条 nlmsghdr:
              ├─ NLMSG_DONE / NLMSG_ERROR → 跳出内层
              ├─ RTM_NEWLINK → 解析 IfInfomsg
              │    ├─ ifi_index != 本接口 index → 忽略（别人的网卡）
              │    ├─ ifi_flags 含 IFF_UP → 读 mtu，缓冲 TunEvent::Up(mtu)
              │    └─ 否则 → 缓冲 TunEvent::Down
              └─ 内层继续下一条（按 nlmsg_len 步进）
       # 内层结束，回到外层：缓冲里现在有事件，下一轮弹出返回
```

为什么需要 `self.events` 这个**内部缓冲**？因为内核一次 `recv` 可能把多条 netlink 消息打成一个批次送来，而 `event()` 契约规定每次只返回**一个** `TunEvent`。所以多余的事件先存进 `Vec`，下次调用先pop缓冲、再 `recv`。

#### 4.2.3 源码精读

先看 `IfInfomsg` 和 `LinuxTunStatus` 的字段定义：

[src/platform/linux/tun.rs:21-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L21-L29) —— `IfInfomsg`，对应 netlink `RTM_NEWLINK` 报文里的「接口信息」，字段含义：`ifi_family`（地址族）、`ifi_type`（接口 ARP 类型）、`ifi_index`（接口编号，唯一标识一张网卡）、`ifi_flags`（状态标志位，如 `IFF_UP`）、`ifi_change`（变更掩码）。

[src/platform/linux/tun.rs:41-46](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L41-L46) —— `LinuxTunStatus` 持有四样东西：`events`（多余的 `TunEvent` 缓冲）、`index`（本接口编号，用于过滤）、`name`（接口名，给 `get_mtu` 用）、`fd`（netlink 套接字）。

再看 netlink 套接字是在哪里建好的——`LinuxTunStatus::new`：

[src/platform/linux/tun.rs:265-310](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L265-L310) —— 用 `socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE)` 建 netlink 套接字，`bind` 到三个组（`RTNLGRP_LINK`、`RTNLGRP_IPV4_IFADDR`、`RTNLGRP_IPV6_IFADDR`），并调用 `get_ifindex` 记下本接口编号。

注意 `groups` 的计算用 `1 << (组号 - 1)` 把组号转成位掩码（[L278-L280](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L278-L280)）。订阅 `RTNLGRP_LINK`（组号 1）后，任何网卡的链路状态变化都会推到这个套接字。

特别留意 `start_up` feature 的影响（[L300-L303](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L300-L303)）：当启用该 feature 时，`events` 初始就塞一个 `TunEvent::Up(1500)`。这呼应 u1-l2 讲过的——`start_up` 用于没有真实 TUN 事件源的环境，让程序一启动就当作「网卡已 up、MTU=1500」。

最关键的 `event()` 主体：

[src/platform/linux/tun.rs:176-262](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L176-L262) —— `Status::event` 实现：先消化 `events` 缓冲，否则 `recv` 阻塞；收到字节后按 `nlmsghdr` 逐条切分，对 `RTM_NEWLINK` 解析 `IfInfomsg`，仅当 `ifi_index == self.index` 时，依 `IFF_UP` 位缓冲 `Up(get_mtu)` 或 `Down`。

重点段落拆解：

- **缓冲优先**（[L186-L188](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L186-L188)）：每次进循环先 `self.events.pop()`，有就直接返回。
- **逐条切分 netlink 报文**（[L203-L260](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L203-L260)）：netlink 一段字节里可能含多条消息，每条以 `nlmsghdr` 开头，用 `hdr.nlmsg_len` 步进 `remain = &remain[msg_len..]`。
- **本接口过滤**（[L243](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L243)）：`if info.ifi_index == self.index`——netlink 推送的是**全系统所有网卡**的链路事件（因为订阅了组），这里用接口编号过滤，只关心自己的 `wg0`。这正是本讲实践任务要写注释的地方。
- **Up/Down 判定**（[L245-L252](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L245-L252)）：用 `ifi_flags & IFF_UP` 判断是否 up；up 时立刻调 `get_mtu(&self.name)` 读取当前 MTU，把它带进 `TunEvent::Up(mtu)`。这意味着用户每次 `ip link set wg0 up`（甚至改 MTU 后重新 up），WireGuard 都能拿到最新 MTU。

最后看消费端，确认这个 `event()` 是怎么被驱动的：

[src/main.rs:145-161](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L145-L161) —— main 里 spawn 的 Tun 事件线程在一个 `loop` 里反复调 `status.event()`，把 `TunEvent::Up(mtu)` 翻译成 `cfg.up(mtu)`、`Down` 翻译成 `cfg.down()`，出错则退出进程。这就是 u1-l4 提到的那条常驻线程。

#### 4.2.4 代码实践

**目标**：完成本讲指定的实践——给 `event()` 增加对 `ifi_flags` 其它位的日志，并解释为什么只关心 `ifi_index` 匹配的事件。

> 注意：worker 规则要求不修改项目源码，因此以下是你**在自己 fork/练习副本里**要做的改动，不是对仓库的提交。

**操作步骤**（在你的练习副本中修改 `src/platform/linux/tun.rs`）：

1. 在 [L243 的 `if info.ifi_index == self.index` 分支内](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L243)，紧挨着现有 `log::trace!` 之后，加一行输出 `IFF_RUNNING` 位：

   ```rust
   // 示例代码：你需要在练习副本中加入
   if info.ifi_flags & (libc::IFF_RUNNING as u32) != 0 {
       log::trace!("netlink, IFF_RUNNING set (carrier/layer-1 active)");
   }
   ```

2. 在 `if info.ifi_index == self.index {` 上方加一段注释，解释过滤原因（答案见下文练习）。
3. 以 `RUST_LOG=netlink=trace,wireguard_rs=trace cargo run -- wg0`（具体 env 名以你的日志配置为准）运行，在另一个终端反复 `ip link set wg0 down && ip link set wg0 up`。

**需要观察的现象**：日志里能看到 `netlink, up event, mtu = ...` 与 `netlink, down event` 交替，并能观察到 `IFF_RUNNING` 是否随 Up/Down 变化。

**预期结果**：Up 时同时出现 `IFF_UP` 与 `IFF_RUNNING` 相关日志；Down 时只有 down 日志。同时你应能解释「为什么即使没有 `wg0` 的事件，`event()` 也会被频繁唤醒」——因为 netlink 组推送的是全系统链路事件，靠 `ifi_index` 过滤掉别家网卡。

**待本地验证**：netlink 事件的具体时序与 `IFF_RUNNING` 的取值依赖内核版本与链路状态，请以本地观测为准。

#### 4.2.5 小练习与答案

**练习 1**：netlink 套接字订阅了全系统的链路事件。为什么 `event()` 不会把 `eth0` 的 Up/Down 当成自己的事件上报？

> **参考答案**：因为 `IfInfomsg.ifi_index` 唯一标识一张网卡，而 `LinuxTunStatus` 在 `new()` 里用 `get_ifindex(&name)` 记下了 `wg0` 自己的 `index`。`event()` 里 `if info.ifi_index == self.index` 这一行做了过滤，只有本接口的事件才会被翻译成 `TunEvent`，其它网卡的事件被静默丢弃（但仍消耗一次 `recv`）。

**练习 2**：`event()` 用了 `self.events` 这个 `Vec<TunEvent>` 做缓冲。如果不缓冲、直接收到一条就 `return`，会出现什么问题？

> **参考答案**：内核一次 `recv` 返回的字节里可能打包了多条 `RTM_NEWLINK` 消息（比如一次 `ip link` 操作触发多个事件）。若收到第一条就 `return`，内层 `while` 循环里 `remain` 中尚未解析的消息会被丢弃，造成事件丢失。缓冲把它们都 push 进 `Vec`，下次 `event()` 进来先 `pop`，保证不漏。

---

### 4.3 get_mtu：用 SIOCGIFMTU 查询 MTU

#### 4.3.1 概念说明

`get_mtu` 是个**工具函数**，被 `event()` 在判定 Up 时调用，用来把「网卡 up 了」翻译成「网卡 up 了，且当前 MTU 是多少」。为什么需要单独查 MTU？因为 `IfInfomsg` 里**不带 MTU 字段**——netlink 的链路事件只告诉你 flags，不告诉你 MTU。要拿 MTU，得再用一次 ioctl：`SIOCGIFMTU`。

它和 4.1 的 `TUNSETIFF` 是同一套 ioctl 机制，只是请求码和参数结构体不同。

#### 4.3.2 核心流程

```
get_mtu(name)
 ├─ fd = socket(AF_INET, SOCK_DGRAM, 0)   // 任意 UDP 套接字即可，只为拿 ioctl 句柄
 ├─ 构造 arg { name, mtu: 0 }
 ├─ ioctl(fd, SIOCGIFMTU, &arg)            // 内核按 name 查到 mtu，回填 arg.mtu
 ├─ close(fd)
 └─ 返回 arg.mtu
```

一个反直觉点：`SIOCGIFMTU` 虽然是「查网卡」，但它的 fd **不需要是那张网卡的 fd**——任意一个 `AF_INET` 数据报套接字都行，内核只靠 ioctl 参数里的 `name` 去找网卡。这个套接字纯粹是 ioctl 的「门票」。

#### 4.3.3 源码精读

[src/platform/linux/tun.rs:132-171](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L132-L171) —— `get_mtu`：开一个临时 `AF_INET/SOCK_DGRAM` 套接字作为 ioctl 句柄，把接口名塞进 `arg`，用 `SIOCGIFMTU` 让内核回填 MTU，再关闭套接字并返回。

要点：

- 参数结构体 `arg` 是函数内部的 `#[repr(C)]` 结构体（[L133-L137](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L133-L137)）：`name: [u8; IFNAMSIZ]` + `mtu: u32`。这其实是 C 里 `struct ifreq` union 的 `ifr_mtu` 视图——前 16 字节是名字，紧跟一个 32 位 MTU 字段。
- 失败处理（[L165-L167](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L165-L167)）：ioctl 返回非 0 报 `GetMTUIoctlFailed`。
- 注意 `?` 的传播（[L246](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L246)）：`event()` 里 `let mtu = get_mtu(&self.name)?;`，查 MTU 失败会让整个 `event()` 返回 `NetlinkFailure`/`GetMTUIoctlFailed`，进而被 main 里的事件线程当成致命错误退出进程（见 4.2.3 末尾引用的 main.rs）。

`get_ifindex` 是同类工具函数，用 `if_nametoindex` 把名字转成编号，供 `LinuxTunStatus::new` 记下 `self.index`：

[src/platform/linux/tun.rs:117-130](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L117-L130) —— 把以 `\0` 结尾的接口名转成内核全局唯一的接口编号 `ifi_index`。

#### 4.3.4 代码实践

**目标**：用命令行复现 `SIOCGIFMTU` 的效果，建立「ioctl 查 MTU」的直觉。

**操作步骤**：

1. 选一张已存在的网卡（如 `lo` 或 `eth0`）。
2. 执行 `cat /sys/class/net/<网卡名>/mtu`。
3. 改一下：`sudo ip link set <网卡名> mtu 1400`（对 `eth0` 等真实网卡需谨慎，建议用 `lo` 或自建 dummy）。

**需要观察的现象**：`/sys/class/net/.../mtu` 的值随 `ip link set ... mtu` 改变而改变。

**预期结果**：这个 sysfs 文件读到的值，就是 `get_mtu` 通过 `SIOCGIFMTU` 查到的同一个数——两者都是问内核「这张网卡当前 MTU」。从而建立「`get_mtu` 不过是用 ioctl 问内核」的直观对应。

**待本地验证**：`ip link set` 在不同网卡/容器里权限不同，请以本地可改的网卡为准。

#### 4.3.5 小练习与答案

**练习 1**：`get_mtu` 里为什么要 `socket(AF_INET, SOCK_DGRAM, 0)`？能不能改成对 TUN 设备 fd 直接 `ioctl`？

> **参考答案**：`SIOCGIFMTU` 的 ioctl 句柄不必是目标网卡的 fd，任意 `AF_INET` 数据报套接字即可，内核只认 ioctl 参数里的接口名。改成对 TUN fd 直接 ioctl 在某些请求码上可行，但代码选择「开一个临时通用套接字」是更通用、更不容易和具体 fd 类型耦合的写法；同时这个临时套接字用完即 `close`，不占用资源。

**练习 2**：`get_mtu` 的失败会让 `event()` 直接返回错误，进而导致 main 退出进程。这种「查 MTU 失败就退出」的策略合理吗？

> **参考答案**：合理但激进。MTU 是发送侧填充对齐（`padding`）的关键参数，拿不到 MTU 就无法正确发包，继续运行没有意义；而 Up 事件期间查 MTU 失败通常意味着接口已异常。因此把它当致命错误、让 main 的 Tun 事件线程 `exit(0)` 是可接受的。若要更宽容，也可改成「失败时用默认 MTU 并告警」，但当前实现选择了简单与安全。

---

### 4.4 LinuxTunReader / LinuxTunWriter：最朴素的 read/write

#### 4.4.1 概念说明

收发 IP 包的两个 trait 实现反而是本讲最简单的部分。它们就是把上一篇 trait 里 `Reader::read` / `Writer::write` 直接映射到 libc 的 `read` / `write` 系统调用，没有任何额外逻辑。

但这里有一个**承接 u2-l1 的关键设计**值得强调：`Reader::read` 的签名是 `read(&self, buf: &mut [u8], offset: usize)`。这个 `offset` 要求把 IP 包读进 `buf[offset..]`，前 `offset` 字节留空。为什么？因为后续加密管道会在那个前缀空间里**就地**构造 WireGuard 传输头与 nonce，避免一次内存拷贝。这是 u2-l1 强调的「为高效就地构造报文预留前缀」的落地。

#### 4.4.2 核心流程

```
read(buf, offset):
  n = libc::read(fd, buf[offset..].as_mut_ptr(), buf.len() - offset)
  n < 0 → Err(Closed)
  否则  → Ok(n)                  # n = 读到的 IP 包字节数（不含前缀）

write(src):
  libc::write(fd, src.as_ptr(), src.len())
  == -1 → Err(Closed)
  否则  → Ok(())
```

#### 4.4.3 源码精读

[src/platform/linux/tun.rs:85-104](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L85-L104) —— `Reader for LinuxTunReader`：对 TUN fd 调 `libc::read`，缓冲从 `buf[offset..]` 开始、长度 `buf.len() - offset`，留出前缀空间。读到负数视为设备关闭。

注意被注释掉的 `debug_assert!(offset < buf.len())`（[L89-L94](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L89-L94)）：作者保留了断言思路但禁用了它，因为 `buf.len() - offset` 在 `offset >= buf.len()` 时会下溢 panic，目前依赖调用方（u3-l2 的 `tun_worker`）保证传入合法 offset。

[src/platform/linux/tun.rs:106-115](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L106-L115) —— `Writer for LinuxTunWriter`：对同一个 fd 调 `libc::write`，把解密后的明文 IP 包写回协议栈。返回 -1 视为关闭。

把这两个实现和上一篇 trait 的文档注释对照着看会更有感觉：

[src/platform/tun.rs:31-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L31-L49) —— `Reader` trait 的文档清楚说明：`offset` 是为「就地构造 transport message」预留的前缀；`buf` 要装得下 MTU + 头部。这正是 4.4.1 提到的设计的契约来源。

[src/platform/tun.rs:16-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L16-L29) —— `Writer` trait：接收一个已被 cryptokey 路由过的明文 IP 包，写回隧道设备。

#### 4.4.4 代码实践

**目标**：用 `strace` 观察运行中的 wireguard-rs 进程，亲眼看 `read`/`write` 作用在 TUN fd 上，验证「收发包就是普通 read/write」。

**操作步骤**：

1. 编译 release：`cargo build --release`（u1-l2）。
2. 以 root 启动：`RUST_LOG=trace ./target/release/wireguard-rs wg0`，按 README 用 `wg(8)` 配置并 `ip link set wg0 up`。
3. 另开终端，`pgrep wireguard-rs` 拿到 PID，执行 `sudo strace -p <PID> -e read,write,recv,sendto -f`。
4. 在 `wg0` 上 `ping` 对端，制造流量。

**需要观察的现象**：strace 输出里能看到对某个 fd 的 `read(...)` 返回一段 IP 包字节（开头是 `45 00 ...` 这类 IPv4 头），紧随其后有 `sendmsg(...)` 把密文从 UDP 套接字发出去；反向类似。

**预期结果**：你会看到 TUN fd（较小编号）上的 `read`/`write` 与 UDP fd 上的 `sendmsg`/`recvmsg` 交替出现。这直接印证 4.4.1：TUN 收发就是最朴素的 `read`/`write`，没有任何额外封装。

**待本地验证**：strace 输出依赖你能否建立真实的 WireGuard 隧道（需要两个端点或 dummy 环境）。若无法搭建，可退而阅读 u3-l2 的 `tun_worker` 源码，跟踪 `read` 的返回值如何流向 `router.send`。

#### 4.4.5 小练习与答案

**练习 1**：`LinuxTunReader` 和 `LinuxTunWriter` 持有的是同一个 fd 吗？这样安全吗？

> **参考答案**：是同一个 fd 的两份拷贝（`create` 里 `LinuxTunReader { fd }` 和 `LinuxTunWriter { fd }` 用的是同一个值）。安全：fd 只是一个整数句柄，拷贝它不会复制内核里的文件描述符引用；真正的引用计数在内核。两个结构体分别只做 `read` 和 `write`，对 TUN 设备而言这两个方向互不干扰。注意这里没有 `Dup`，也没有人 `close` 它——fd 的生命周期由进程终止隐式回收（这是简化处理，生产实现里应显式管理）。

**练习 2**：为什么 `Reader::read` 要传 `offset`，而 `Writer::write` 没有 offset 参数？

> **参考答案**：方向不同。**读**（TUN → 用户态 → 加密）：内核给的是裸 IP 包，但下游加密管道要在 IP 包前面就地补一个传输头，所以读的时候就要把包放到 `buf[offset..]`，前缀留空。**写**（解密 → 用户态 → TUN）：解密后产物就是完整的明文 IP 包，直接整体写回 TUN 即可，不需要预留前缀，所以 `write(src)` 只接受一段连续字节。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「TUN 设备的一生」追踪任务。

**任务**：对照源码，画出 `wg0` 从「被创建」到「上报 Up 事件并开始收发」的完整时序，并在每个环节标注：调用了哪个系统调用、改动了 `LinuxTunStatus` 的哪个字段。

**建议步骤**：

1. 从 [main.rs 的 `plt::Tun::create(name)`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L92) 出发，依次画出：
   - `open("/dev/net/tun")` → 得到 fd；
   - `ioctl(TUNSETIFF)` → 网卡 `wg0` 诞生；
   - `LinuxTunStatus::new` → `socket(AF_NETLINK)` + `bind` → `self.index` 由 `get_ifindex` 填入；
   - 返回 `(readers, writer, status)`。
2. 接到 main.rs 的 Tun 事件线程，画出第一次 `status.event()` 的路径：`recv` 阻塞 → 用户 `ip link set wg0 up` → 内核推 `RTM_NEWLINK` → 解析 `IfInfomsg` → `ifi_index` 命中 → `get_mtu`（又一个 `socket`+`ioctl(SIOCGIFMTU)`）→ 缓冲 `TunEvent::Up(mtu)` → 返回 → `cfg.up(mtu)`。
3. 再画出数据面：`tun_worker` 调 `LinuxTunReader::read`（`libc::read`）拿到 IP 包 → 交给路由器；解密后 `LinuxTunWriter::write`（`libc::write`）把明文包写回。

**交付物**：一张包含「系统调用 / 字段变更 / 产出事件」三列的表格或时序图。这张图应该让你一眼看清：创建靠 `open+ioctl`、状态靠 netlink、MTU 靠又一次 ioctl、收发就是 `read/write`——四种机制各司其职。

**待本地验证**：若条件允许，用第 4.4.4 节的 strace 方法核对你画的时序与真实系统调用顺序是否一致。

## 6. 本讲小结

- `LinuxTun::create` 的核心是 `open("/dev/net/tun")` + `ioctl(TUNSETIFF)`，靠一个 `#[repr(C)]` 的 `Ifreq` 在用户态与内核间传「名字 + 标志（`IFF_TUN | IFF_NO_PI`）」。
- 网卡 Up/Down 不是轮询得来的，而是订阅 `NETLINK_ROUTE` 的 `RTNLGRP_LINK` 组后由内核**主动推送** `RTM_NEWLINK`，再解析其中的 `IfInfomsg`。
- `event()` 用 `self.index` 过滤掉别家网卡的事件，用 `self.events` 缓冲「一批次多条消息」以免丢事件；Up 时附带 `get_mtu` 读取当前 MTU。
- MTU 查询靠第二次 ioctl `SIOCGIFMTU`，fd 只是「门票」，内核按接口名查值回填。
- `LinuxTunReader`/`LinuxTunWriter` 把 trait 直接映射到 `libc::read`/`libc::write`；`read` 的 `offset` 参数为下游就地构造传输头预留前缀，是 u2-l1 零拷贝设计的落地。
- 三类机制（ioctl 造卡、netlink 听状态、read/write 收发）共同把上一篇的 `Tun`/`PlatformTun` trait 在 Linux 上补全，并通过 `platform/mod.rs` 别名化成 `plt::Tun` 供上层使用。

## 7. 下一步学习建议

下一篇 **u2-l3 Linux UDP 绑定与 sticky socket** 会用几乎同样的「ioctl/setsockopt + 收发」套路，但对象换成了 UDP 套接字，并引入更复杂的 `IP_PKTINFO` 辅助数据来实现 WireGuard 的 sticky source 行为。建议：

- 先复习 u2-l1 里 `UDP`/`PlatformUDP`/`Endpoint` 三个 trait，尤其是 `write(&mut Endpoint)` 为何要可变（为清源重发预留）。
- 带着本讲的两个直觉去读 u2-l3：netlink 的「内核推送」对应 UDP 里 `recvmsg` 的辅助数据；`get_mtu` 里「ioctl 参数结构体 = C union 视图」对应 `pktinfo` 的辅助数据布局。
- 若想先看「收到的 IP 包之后去了哪」，可跳到 u3-l2（TUN 工作线程），再回头读 u2-l3。
