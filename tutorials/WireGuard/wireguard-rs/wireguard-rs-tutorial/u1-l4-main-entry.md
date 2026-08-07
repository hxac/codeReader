# 程序入口 main.rs 与运行生命周期

## 1. 本讲目标

学完本讲，你应当能够：

- 按顺序讲清 `main.rs` 从进程启动到最终阻塞（`wg.wait()`）的每一步。
- 说清 `plt::UAPI`、`plt::Tun`、`plt::UDP` 这一组平台别名的作用，以及它们为什么能让协议核心与具体操作系统解耦。
- 看懂 TUN 事件（`Up`/`Down`）如何驱动 `cfg.up(mtu)` / `cfg.down()`，以及一条 UAPI 连接如何被分发到 `configuration::uapi::handle`。
- 理解 `WireGuard::new` / `add_tun_reader` 在背后启动了哪些工作线程，以及 `wg.wait()` 靠什么条件解除阻塞。
- 能够动手给参数解析增加一个 `--version/-V` 选项，并为它写一个单元测试。

## 2. 前置知识

在进入源码之前，先建立几个直觉。如果你学过 **u1-l1** 到 **u1-l3**，这些概念应该不陌生，这里只做最小回顾。

- **WireGuard 的三大外部接口**：TUN（与内核交换**明文** IP 包）、UDP（在对端之间交换**密文**报文）、UAPI（一个**文本**控制协议，对接 `wg(8)` 工具）。本讲的 `main.rs` 就是把这三个接口接到协议核心上的“装配车间”。
- **平台别名 `plt`**：项目用一组 trait 描述“平台能力”（怎么建 TUN、怎么绑 UDP、怎么开 UAPI 套接字），不同操作系统提供不同实现。`src/platform/mod.rs` 用 `pub use linux as plt;` 把当前平台的实现统一起一个别名 `plt`，于是 `main.rs` 里写 `plt::Tun` 就等于写“当前平台的 TUN 实现”。这是上一讲（u1-l3）讲过的“核心只依赖 trait”的关键落点。
- **特权与降权**：建 TUN、绑 UAPI 套接字都需要 root；而协议核心本身不需要 root。所以 `main.rs` 会**先**做完两件需要 root 的事，**再**调用 `util::drop_privileges()` 把权限降为 `nobody`。降权的细节是下一讲（u1-l5）的主题，本讲只关注它在生命周期中的**位置**。
- **守护进程化（daemonize）**：默认情况下进程会通过双 fork 进入后台；用 `-f/--foreground` 可以让它停留前台。本讲把它当作生命周期中的一个步骤来理解。

> 一句话总结本讲的视角：`main.rs` 是一个**装配脚本**——它按固定顺序把“需要 root 的 IO 资源”准备好，降权后构造协议核心对象，再起几个常驻线程驱动它，最后主线程阻塞，直到所有 TUN reader 线程退出。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) | 程序唯一入口，装配整个设备并启动服务线程。 |
| [src/platform/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs) | 用 `cfg` 选择平台实现，并定义 `plt` 别名。 |
| [src/platform/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs) | 定义 `TunEvent`、`Status`、`PlatformTun::create` 等 trait。 |
| [src/platform/uapi.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs) | 定义 `BindUAPI::connect`、`PlatformUAPI::bind` trait。 |
| [src/wireguard/wireguard.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs) | 协议核心胶水层，`WireGuard::new`、`add_tun_reader`、`wait` 的实现。 |
| [src/configuration/config.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs) | `WireGuardConfig::new`，把核心对象包成配置接口。 |
| [src/configuration/uapi/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/uapi/mod.rs) | UAPI 文本协议的入口函数 `handle`。 |

## 4. 核心概念与源码讲解

### 4.1 命令行参数解析与启动约定

#### 4.1.1 概念说明

`wireguard-rs` 被设计成一个命令行可执行程序，调用方式形如：

```text
wireguard-rs wg0
```

它的命令行约定非常朴素（没有用 `clap`/`structopt` 这类参数解析库），而是直接遍历 `env::args()`：

- 第一个非选项参数 = 设备名（如 `wg0`），**必填**。
- `--foreground` / `-f` = 不进入后台（前台运行）。
- `--disable-drop-privileges` = 不降权（调试时用）。

之所以手写解析，是因为参数极少，引入一个解析库并不划算；也正因如此，后面我们才有机会把它“重构”成一个可单测的函数。

#### 4.1.2 核心流程

```text
读取 env::args()
  ├── 跳过 argv[0]（程序自身路径）
  └── 逐个 token 匹配：
        "--foreground" / "-f"        → foreground = true
        "--disable-drop-privileges"  → drop_privileges = false
        其它                          → 视为设备名 name
若 name 为 None → 报错并 exit(-1)
```

注意三个布尔量 `name`、`drop_privileges`、`foreground` 的默认值：`drop_privileges` 默认为 `true`（安全优先），`foreground` 默认为 `false`（默认后台）。

#### 4.1.3 源码精读

`main()` 一开头就把这三个状态变量初始化好，然后用一个 `for` 循环完成全部解析（[src/main.rs:55-74](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L55-L74)）：

```rust
fn main() {
    // parse command line arguments
    let mut name = None;
    let mut drop_privileges = true;
    let mut foreground = false;
    let mut args = env::args();

    // skip path (argv[0])
    args.next();
    for arg in args {
        match arg.as_str() {
            "--foreground" | "-f" => { foreground = true; }
            "--disable-drop-privileges" => { drop_privileges = false; }
            dev => name = Some(dev.to_owned()),
        }
    }
    ...
```

解析完后，对设备名做“存在性校验”，缺失就直接 `exit(-1)`（[src/main.rs:76-83](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L76-L83)）。

> 关于退出码：本文件用负数退出码（`-1`、`-2` …）区分不同的致命错误来源，方便运维脚本定位是哪一步失败了。完整映射见 4.2.3 末尾的表格。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在没有 IDE 跳转的前提下，凭肉眼判断参数行为。

1. 打开 [src/main.rs:55-74](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L55-L74)。
2. 回答：若用户输入 `wireguard-rs --foreground wg0 --disable-drop-privileges`，最终 `name`、`foreground`、`drop_privileges` 分别是什么？
3. **预期结果**：因为 `for` 循环对顺序不敏感、只看 token 内容，三者分别为 `Some("wg0")`、`true`、`false`。这说明选项与位置参数可以交错出现。
4. **待本地验证**：你可以编译后用 `./target/release/wireguard-rs`（不带任何参数）运行，观察是否打印 `No device name supplied` 并退出。

#### 4.1.5 小练习与答案

**练习 1**：当前实现里，如果用户误写 `--forground`（拼错），会发生什么？
**答案**：它不会匹配任何分支，会落入 `dev => name = Some(...)`，被当作“设备名”。于是后续 `plt::Tun::create("--forground")` 会尝试创建一张名为 `--forground` 的网卡并失败。这说明手写解析**没有未知选项报错**，是它的一个弱点。

**练习 2**：为什么 `args.next();` 这一行不可或缺？
**答案**：`env::args()` 的第一个元素是程序自身路径（`argv[0]`），若不跳过，它会被当作设备名。

---

### 4.2 平台别名 plt 与 UAPI/TUN 的特权创建

#### 4.2.1 概念说明

参数解析完之后，`main.rs` 要做两件**需要 root 权限**的事：

1. **绑定 UAPI 套接字**（在 Linux 上是 `/var/run/wireguard/<name>.sock` 一个 Unix 域套接字），用来接收 `wg(8)` 下发的配置。
2. **创建 TUN 网卡**（在 Linux 上是打开 `/dev/net/tun` 并 `ioctl(TUNSETIFF)`）。

这两步之所以必须在降权**之前**完成，是因为它们都需要 root。一旦完成，就立刻放弃 root，符合“最小权限”原则。

这两步分别通过 `plt::UAPI::bind(...)` 和 `plt::Tun::create(...)` 完成。这里的 `plt` 是平台别名——它在编译期根据目标操作系统被绑定到具体实现。

#### 4.2.2 核心流程

```text
plt::UAPI::bind(name)        →  返回 UAPI listener（用来 accept 连接）
plt::Tun::create(name)       →  返回 (readers, writer, status) 三元组
                                 ├── readers: Vec<Reader>  （多队列/多协议栈读端）
                                 ├── writer:  Writer        （写端，注入核心）
                                 └── status:  Status        （产生 Up/Down 事件）
```

`Tun::create` 返回**三元组**而不是单个对象，是因为 WireGuard 的数据面需要：若干个并发 reader（多队列）、一个共享 writer、以及一个独立的状态事件源。这套“三元组”语义是平台 trait 的统一约定（详见 u2-l1）。

#### 4.2.3 源码精读

先看 `plt` 别名是怎么来的。`src/platform/mod.rs` 用条件编译选择平台实现（[src/platform/mod.rs:9-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs#L9-L16)）：

```rust
#[cfg(target_os = "linux")]
pub mod linux;

#[cfg(test)]
pub mod dummy;

#[cfg(target_os = "linux")]
pub use linux as plt;
```

也就是说：在 Linux 上 `plt = platform::linux`；在测试构建里另有 `dummy` 平台（u2-l4 会讲）。因此 `main.rs` 里的 `plt::UAPI`、`plt::Tun`、`plt::UDP`，编译到 Linux 时就分别对应 `linux::LinuxUAPI`、`linux::LinuxTun`、`linux::LinuxUDP`。

> 注意：`main.rs` 顶部 `use platform::*;`（[src/main.rs:25](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L25)）把 `plt` 这个别名引入了当前作用域，所以下面能直接写 `plt::Tun`。

接着看 `main.rs` 里这两步的真实代码。先绑 UAPI（[src/main.rs:85-89](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L85-L89)）：

```rust
// create UAPI socket
let uapi = plt::UAPI::bind(name.as_str()).unwrap_or_else(|e| {
    eprintln!("Failed to create UAPI listener: {}", e);
    exit(-2);
});
```

`plt::UAPI::bind` 对应的 trait 方法签名在 [src/platform/uapi.rs:11-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs#L11-L16)，它返回一个实现了 `BindUAPI` 的对象，后者提供 `connect()` 用来接收一条连接（[src/platform/uapi.rs:4-9](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/uapi.rs#L4-L9)）。

再建 TUN（[src/main.rs:91-95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L91-L95)）：

```rust
// create TUN device
let (mut readers, writer, status) = plt::Tun::create(name.as_str()).unwrap_or_else(|e| {
    eprintln!("Failed to create TUN device: {}", e);
    exit(-3);
});
```

`plt::Tun::create` 对应 trait 方法 `PlatformTun::create`，它的返回类型就是上文说的三元组（[src/platform/tun.rs:58-63](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L58-L63)）：

```rust
fn create(name: &str) -> Result<(Vec<Self::Reader>, Self::Writer, Self::Status), Self::Error>;
```

随后是降权与守护进程化（[src/main.rs:97-117](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L97-L117)），细节留给 u1-l5，这里只强调**顺序**：UAPI/TUN 创建 → 降权 → daemonize。日志初始化发生在它们之后（[src/main.rs:119-124](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L119-L124)）。

**致命错误退出码汇总**：

| 退出码 | 触发条件 | 源码位置 |
| --- | --- | --- |
| `-1` | 未提供设备名 | [src/main.rs:80](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L80) |
| `-2` | UAPI 监听套接字创建失败 | [src/main.rs:88](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L88) |
| `-3` | TUN 设备创建失败 | [src/main.rs:94](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L94) |
| `-4` | 降权失败 | [src/main.rs:103](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L103) |
| `-5` | daemonize 失败 | [src/main.rs:114](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L114) |

#### 4.2.4 代码实践（源码阅读型）

**目标**：体会“平台别名”如何让 `main.rs` 对操作系统无感。

1. 读 [src/platform/mod.rs:9-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs#L9-L16)。
2. 设想你要把项目移植到 macOS：你会新增一个 `platform/macos/` 模块，并在 `mod.rs` 里加 `#[cfg(target_os = "macos")] pub use macos as plt;`。
3. **预期结果**：只要 macOS 模块正确实现了 `PlatformUAPI`/`PlatformTun`/`PlatformUDP` 这几个 trait，`main.rs` 的代码**一行都不用改**就能在 macOS 上跑起来——这正是“核心只依赖 trait”设计带来的红利。

#### 4.2.5 小练习与答案

**练习 1**：为什么 UAPI 和 TUN 的创建必须排在 `drop_privileges()` **之前**，而日志初始化可以排在**之后**？
**答案**：前者需要 root（绑定 `/var/run/wireguard` 套接字、打开 `/dev/net/tun`）；后者只是初始化日志后端，普通用户权限即可。所以把特权操作前置、降权后再做无特权工作。

**练习 2**：`plt::Tun::create` 返回的是 `Vec<Reader>` 而不是单个 `Reader`，为什么？
**答案**：为了支持多队列（multi-queue）TUN 和分离的 IPv4/IPv6 读端，让多个 worker 线程并行从同一张网卡读包，提高吞吐。

---

### 4.3 创建 WireGuard 设备与注册 TUN reader

#### 4.3.1 概念说明

资源就绪后，`main.rs` 构造协议核心对象 `WireGuard<plt::Tun, plt::UDP>`。这个对象是整个项目的“胶水层”（u3-l1 会深入），它内部组合了两个纯引擎：握手模块（协商密钥）和路由器（加解密 + 路由）。

构造它需要两步：

1. `WireGuard::new(writer)`：传入 TUN 的 writer（出站明文包要写回网卡），构造核心对象，**同时**在内部 spawn 一组握手工作线程。
2. 对每个 TUN reader 调用 `add_tun_reader(reader)`：为它 spawn 一个 `tun_worker` 线程，负责从网卡读包、送进加密管道。

注意 `WireGuard<T,B>` 实现了 `Clone`，但克隆只是增加内部 `Arc` 的引用计数，**不会**复制设备状态——这一点对后续把 `wg` 共享给多个线程很关键。

#### 4.3.2 核心流程

```text
WireGuard::new(writer)
  ├── 取 CPU 物理核数 cpus = num_cpus::get()
  ├── 创建握手队列 ParallelQueue（cpus 个接收端、容量 128）
  ├── 创建 router::Device（cpus 个工作线程）
  ├── 组装 WireguardInner（enabled=false, mtu=0, …）
  └── 为每个接收端 spawn 一个 handshake_worker 线程

wg.add_tun_reader(reader)   （对每个 reader 调用）
  ├── tun_readers.increase()      （计数 +1）
  └── spawn tun_worker 线程
        └── 线程退出时 tun_readers.decrease()
```

#### 4.3.3 源码精读

`main.rs` 中的两段（[src/main.rs:130-139](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L130-L139)）：

```rust
// create WireGuard device
let wg: WireGuard<plt::Tun, plt::UDP> = WireGuard::new(writer);

// add all Tun readers
while let Some(reader) = readers.pop() {
    wg.add_tun_reader(reader);
}

// wrap in configuration interface
let cfg = configuration::WireGuardConfig::new(wg.clone());
```

注意第三步：`wg.clone()` 把核心对象包进了 `WireGuardConfig`。`WireGuardConfig` 的作用是**隐藏 IO 泛型**，对外暴露一个干净的配置接口（[src/configuration/config.rs:47-55](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L47-L55)）。此后 TUN 事件线程和 UAPI 线程都只持有 `cfg`，不再直接碰复杂的 `WireGuard<plt::Tun, plt::UDP>` 类型。

再看 `WireGuard::new` 内部（[src/wireguard/wireguard.rs:268-302](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L268-L302)），关键点：

- `let cpus = num_cpus::get();`（[:270](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L270)）：线程数 = 物理核数。
- `ParallelQueue::new(cpus, 128)`（[:273](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L273)）：构造一个多生产者多消费者的握手任务队列，容量 128。
- `router::Device::new(num_cpus::get(), writer)`（[:276-277](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L276-L277)）：把 writer 注入路由器。
- 最后的 `while let Some(rx) = rxs.pop()` 循环（[:296-299](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L296-L299)）：为队列的每个接收端 spawn 一个 `handshake_worker`。

`add_tun_reader` 则非常短（[src/wireguard/wireguard.rs:251-262](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L251-L262)）：

```rust
pub fn add_tun_reader(&self, reader: T::Reader) {
    let wg = self.clone();
    wg.tun_readers.increase();                 // 计数 +1
    thread::spawn(move || {
        tun_worker(&wg, reader);
        wg.tun_readers.decrease();             // 线程退出时计数 -1
    });
}
```

这里的 `increase/decrease` 直接决定了后面 `wg.wait()` 何时解除阻塞（见 4.5）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：数清楚 `WireGuard::new` 一共 spawn 了多少个线程。

1. 读 [src/wireguard/wireguard.rs:268-302](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L268-L302)。
2. 假设机器有 4 个物理核，TUN 创建返回 1 个 reader。
3. **预期结果**：`new` 内部 spawn 了 **4** 个 `handshake_worker`；`add_tun_reader` 再 spawn **1** 个 `tun_worker`；`router::Device::new` 内部还会 spawn 若干 router 工作线程（u5-l1 会讲，同样是 4 个）。注意此时 **UDP reader 还没注册**——UDP 绑定要等到 UAPI 下发 `listen_port` 后，由 `cfg` 触发 `start_listener` 才进行（见 u6-l1）。

#### 4.3.5 小练习与答案

**练习 1**：`WireGuard` 实现 `Clone`，为什么克隆它是安全的、不会产生两个独立设备？
**答案**：因为 `WireGuard` 内部只有一个 `Arc<WireguardInner>` 字段（[src/wireguard/wireguard.rs:63-65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L63-L65)），`clone` 只增加引用计数。所有克隆体共享同一份设备状态。

**练习 2**：为什么把 `wg` 包进 `WireGuardConfig` 之后，TUN/UAPI 线程就拿的是 `cfg` 而不是 `wg`？
**答案**：`WireGuardConfig` 隐藏了 `T`、`B` 这两个 IO 泛型，提供统一的不带泛型的配置接口，避免把复杂类型暴露给控制面代码。

---

### 4.4 两条服务线程：Tun 事件线程与 UAPI 服务线程

#### 4.4.1 概念说明

设备对象构造完后，`main.rs` 还要起两个**常驻线程**，分别处理两类外部事件：

- **Tun 事件线程**：通过 `status.event()` 阻塞等待网卡的 Up/Down 事件（以及 MTU 变化）。一旦网卡被 `ip link set wg0 up`，这里会收到 `TunEvent::Up(mtu)`，进而调用 `cfg.up(mtu)` 把整个协议核心“开”起来。
- **UAPI 服务线程**：在一个循环里 `uapi.connect()` 接收 `wg(8)` 的连接，**每条连接再 spawn 一个新线程**去跑 `configuration::uapi::handle`，解析文本协议并下发配置。

注意两者的线程模型不同：Tun 事件是**单线程**串行处理（事件之间有先后）；UAPI 是**连接级并发**（每条连接一个线程，互不阻塞）。

#### 4.4.2 核心流程

```text
Tun 事件线程（1 个）
  loop {
    match status.event() {
      Err         → 记日志，profiler_stop()，exit(0)   // 网卡没了，正常退出
      Ok(Up(mtu)) → cfg.up(mtu)                        // 开启核心 + 启动各 peer 定时器
      Ok(Down)    → cfg.down()                         // 关停核心 + 停止定时器
    }
  }

UAPI 服务线程（1 个 + 每连接 1 个）
  loop {
    stream = uapi.connect()
    thread::spawn → configuration::uapi::handle(&mut stream, &cfg)
  }
```

#### 4.4.3 源码精读

**Tun 事件线程**（[src/main.rs:141-162](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L141-L162)）：

```rust
thread::spawn(move || loop {
    match status.event() {
        Err(e) => {
            log::info!("Tun device error {}", e);
            profiler_stop();
            exit(0);
        }
        Ok(tun::TunEvent::Up(mtu)) => {
            log::info!("Tun up (mtu = {})", mtu);
            let _ = cfg.up(mtu); // TODO: handle
        }
        Ok(tun::TunEvent::Down) => {
            log::info!("Tun down");
            cfg.down();
        }
    }
});
```

`TunEvent` 只有两种取值（[src/platform/tun.rs:3-6](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L3-L6)）：`Up(usize)`（带 MTU）和 `Down`。`status.event()` 在没有新事件时会**阻塞**（[src/platform/tun.rs:8-14](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L8-L14)），所以这个线程不会空转。

`cfg.up(mtu)` 最终会调用 `WireGuard::up`（[src/wireguard/wireguard.rs:153-175](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L153-L175)）：设置 MTU、开启路由器发送、为每个 peer 重启定时器、把 `enabled` 置 `true`。`cfg.down()` 对称地反向操作（[:127-149](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L127-L149)）。

> 一个细节：`Ok(tun::TunEvent::Up(mtu)) =>` 这里用的是全路径 `tun::TunEvent`，因为 `main.rs` 顶部 `use platform::tun::{PlatformTun, Status};`（[src/main.rs:23](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L23)）只引入了 trait，没引入 `TunEvent` 枚举本身。

**UAPI 服务线程**（[src/main.rs:164-180](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L164-L180)）：

```rust
thread::spawn(move || loop {
    match uapi.connect() {
        Ok(mut stream) => {
            let cfg = cfg.clone();
            thread::spawn(move || {
                configuration::uapi::handle(&mut stream, &cfg);
            });
        }
        Err(err) => {
            log::info!("UAPI connection error: {}", err);
            profiler_stop();
            exit(-1);
        }
    }
});
```

每收到一条连接，就 `cfg.clone()`（增加 `Arc` 引用）后 spawn 一个新线程，把流通入 `configuration::uapi::handle`。`handle` 会按行读取 UAPI 文本协议（`get=1` / `set=1` 等），调用 `cfg` 上的配置方法（详见 u6-l2）。`uapi.connect()` 同样会阻塞等待新连接。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理清一次“把网卡拉起”的事件传播链。

1. 读 [src/main.rs:141-162](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L141-L162) 与 [src/wireguard/wireguard.rs:153-175](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L153-L175)。
2. 画出调用链：`ip link set wg0 up`（内核）→ netlink 通知（Linux 实现，u2-l2）→ `status.event()` 返回 `Up(1500)` → `cfg.up(1500)` → `WireGuard::up` → `router.up()` + 各 `peer.up()` / `peer.start_timers()`。
3. **预期结果**：你会看到 `enabled` 从 `false` 变 `true`，MTU 从 0 变为 1500，各 peer 的定时器开始运转。整条链路解释了“控制面事件如何驱动数据面”。

#### 4.4.5 小练习与答案

**练习 1**：为什么 UAPI 线程要为**每条连接** spawn 一个新线程，而不是串行处理？
**答案**：`wg(8)` 可能同时发起多个连接（例如一边 `wg set` 一边 `wg show`），串行处理会让后到的连接等待。每连接一线程可以并发响应，且 `handle` 只持有 `cfg` 的 `Arc` 引用，线程安全。

**练习 2**：Tun 事件线程在收到 `Err` 时调用 `exit(0)`（退出码 0），而不是负数，为什么？
**答案**：`status.event()` 返回 `Err` 通常意味着 TUN 设备被关闭/销毁（正常关机场景），属于预期内的优雅退出，所以用 0 表示“正常结束”。

---

### 4.5 wg.wait() 阻塞与 WaitCounter 优雅退出

#### 4.5.1 概念说明

两条服务线程起完后，`main.rs` 的最后一件事是 `wg.wait()`——主线程在这里阻塞，直到所有 `tun_worker` 线程退出。这是一个“反向生命周期管理”的小机制：

- `add_tun_reader` 时 `tun_readers.increase()`（计数 +1）。
- 对应的 `tun_worker` 线程退出时 `tun_readers.decrease()`（计数 -1）。
- `wait()` 在计数归零前一直阻塞；一旦归零就返回，主线程随后 `profiler_stop()` 并从 `main` 返回，进程结束。

换句话说：**只要还有 TUN reader 在工作，进程就活着**；所有 reader 都停了，进程就干净地退出。

#### 4.5.2 核心流程

```text
wg.wait()
  └── tun_readers.wait()
        └── 取锁 nread
            while nread > 0: nread = condvar.wait(nread)   // 挂起，直到被 notify_all
```

`WaitCounter` 本质是一个 `(Mutex<usize>, Condvar)` 组合（[src/wireguard/wireguard.rs:67](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L67)）：用 `Mutex` 保护计数，用 `Condvar` 唤醒等待者。

#### 4.5.3 源码精读

`main.rs` 的收尾（[src/main.rs:182-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L182-L185)）：

```rust
// block until all tun readers closed
wg.wait();
profiler_stop();
```

`wait` 的实现（[src/wireguard/wireguard.rs:264-266](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L264-L266)）只是转发给 `tun_readers.wait()`。完整的 `WaitCounter` 三件套在 [:91-115](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L91-L115)：

```rust
pub fn wait(&self) {
    let mut nread = self.0.lock().unwrap();
    while *nread > 0 {
        nread = self.1.wait(nread).unwrap();
    }
}
fn decrease(&self) {
    let mut nread = self.0.lock().unwrap();
    assert!(*nread > 0);
    *nread -= 1;
    if *nread == 0 {
        self.1.notify_all();      // 归零时唤醒 wait()
    }
}
```

注意 `decrease` 里的 `assert!(*nread > 0)`：它防止“减得比加得多”这种逻辑错误，是内部不变量检查。`wait()` 用 `while` 而不是 `if`，这是条件变量的标准用法——用来抵御**虚假唤醒**（spurious wakeup）：即使没人 `notify`，`wait` 也可能自己返回，必须重新检查条件。

> 小提示：本项目里 `Mutex`/`RwLock` 来自 `spin` crate（自旋锁，[:29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L29)），但 `WaitCounter` 故意用的是标准库的 `std::sync::Mutex` + `Condvar`（[:21-22](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L21-L22)），因为自旋锁没法配合 `Condvar` 做真正的“挂起等待”。

#### 4.5.4 代码实践（源码阅读型）

**目标**：找出“进程何时自然退出”。

1. 读 [src/wireguard/wireguard.rs:251-262](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L251-L262) 与 [:91-115](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L91-L115)。
2. 推理：`tun_worker` 在什么情况下会退出？它的退出会让 `tun_readers` 归零，从而唤醒 `main` 里的 `wg.wait()`。
3. **预期结果**：当 TUN reader 的 `read` 返回错误（例如网卡被删除、`status` 报错导致上层关闭 reader）时，`tun_worker` 循环结束，触发 `decrease`。所有 reader 都如此之后，`wait()` 返回，进程结束。
4. **待本地验证**：运行中的进程，若用 `ip link delete wg0` 删掉网卡，理论上会触发上述退出路径。

#### 4.5.5 小练习与答案

**练习 1**：`wg.wait()` 只等待 TUN reader，**不**等待 handshake worker / UAPI 线程 / router worker。这是否意味着进程退出时那些线程会被“遗弃”？
**答案**：是的。`main` 返回后进程退出，那些常驻线程会被强制结束。这是有意的：TUN reader 全部关闭意味着设备已不可用，没必要再等待其它线程；它们大多是共享 `Arc` 的，进程退出时资源随进程一起回收。

**练习 2**：为什么 `wait()` 用 `while *nread > 0` 循环而不是 `if *nread > 0`？
**答案**：为了处理条件变量的虚假唤醒——`Condvar::wait` 可能在未被 `notify` 的情况下返回，必须循环重新检查条件才能保证正确性。

---

## 5. 综合实践

把本讲的三条主线（参数解析、平台装配、线程模型）串起来，完成下面这个**修改 + 测试**任务：

> **任务**：在 `main.rs` 的参数解析处增加一个 `--version` / `-V` 选项，打印 `Cargo.toml` 中的 `version` 字段后退出；并为解析逻辑补充一个单元测试。

### 背景知识

Rust 在编译期会把 `Cargo.toml` 的包信息注入为若干环境变量，其中 `CARGO_PKG_VERSION` 就是 `[package]` 里的 `version`（当前是 `0.1.4`，见 [Cargo.toml:6](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L6)）。用 `env!("CARGO_PKG_VERSION")` 宏即可读取它。

### 操作步骤（示例代码）

当前 `main()` 把解析与执行混在一起，难以单测。推荐做法是**先把解析抽成一个纯函数**，再在 `main` 里调用它。

第一步，抽出可测试的解析函数（**示例代码**，需你自行添加到 `src/main.rs`）：

```rust
// 示例代码：把参数解析抽成纯函数，便于单元测试
struct Args {
    name: Option<String>,
    drop_privileges: bool,
    foreground: bool,
    version: bool,
}

impl Default for Args {
    fn default() -> Self {
        Args {
            name: None,
            drop_privileges: true,   // 安全优先
            foreground: false,       // 默认后台
            version: false,
        }
    }
}

/// 从参数迭代器解析出启动选项；跳过 argv[0]。
fn parse_args<I: Iterator<Item = String>>(mut args: I) -> Args {
    let mut result = Args::default();
    args.next(); // skip program path (argv[0])
    for arg in args {
        match arg.as_str() {
            "--foreground" | "-f" => result.foreground = true,
            "--disable-drop-privileges" => result.drop_privileges = false,
            "--version" | "-V" => result.version = true,
            dev => result.name = Some(dev.to_owned()),
        }
    }
    result
}
```

第二步，改写 `main()` 使用它（**示例代码**）：

```rust
fn main() {
    let args = parse_args(env::args());

    // 处理 --version：打印版本后立即退出
    if args.version {
        println!("wireguard-rs {}", env!("CARGO_PKG_VERSION"));
        return;
    }

    // unwrap device name
    let name = match args.name {
        None => {
            eprintln!("No device name supplied");
            exit(-1);
        }
        Some(name) => name,
    };
    // …后续保持原逻辑：用 args.foreground / args.drop_privileges…
}
```

第三步，补单元测试（**示例代码**，放在 `src/main.rs` 末尾）：

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_version_short_flag() {
        let argv = vec!["wireguard-rs".to_string(), "-V".to_string()];
        let parsed = parse_args(argv.into_iter());
        assert!(parsed.version);
        assert!(parsed.name.is_none());
    }

    #[test]
    fn test_version_long_flag_with_device() {
        // -V 与设备名同时出现时，仍以 version 优先（main 里最先判断）
        let argv = vec![
            "wireguard-rs".to_string(),
            "wg0".to_string(),
            "--version".to_string(),
        ];
        let parsed = parse_args(argv.into_iter());
        assert!(parsed.version);
        assert_eq!(parsed.name.as_deref(), Some("wg0"));
    }

    #[test]
    fn test_defaults_preserved() {
        let argv = vec!["wireguard-rs".to_string(), "wg0".to_string()];
        let parsed = parse_args(argv.into_iter());
        assert!(!parsed.version);
        assert!(!parsed.foreground);
        assert!(parsed.drop_privileges);
        assert_eq!(parsed.name.as_deref(), Some("wg0"));
    }
}
```

### 需要观察的现象与预期结果

1. 运行 `cargo test`，三个新测试应当全部通过。
2. 运行 `cargo build --release` 后执行 `./target/release/wireguard-rs -V`，应当打印 `wireguard-rs 0.1.4` 并立即退出，**不会**尝试创建任何 TUN/UAPI 资源（因为 `return` 发生在它们之前）。
3. 运行 `./target/release/wireguard-rs wg0`（不带 root）行为应与改动前一致：仍会走到 UAPI 绑定并因权限失败。

### 注意事项

- 这是给读者的练习；**本讲义不会修改源码**，请你自己动手编辑 `src/main.rs`。
- `env!` 宏在编译期求值，因此版本号是编译时固化的，改了 `Cargo.toml` 后需要重新编译才会生效。
- 若你希望未知选项（如拼错的 `--forground`）报错而不是被当成设备名，可在 `parse_args` 的 `dev =>` 分支里增加一个“以 `-` 开头即视为未知选项”的判断——可作为进阶练习。

## 6. 本讲小结

- `main.rs` 是一个**装配脚本**：参数解析 → 绑 UAPI → 建 TUN（这三件需 root）→ 降权 → daemonize → 建日志 → 建核心 → 起两条服务线程 → `wg.wait()` 阻塞。
- `plt::UAPI` / `plt::Tun` / `plt::UDP` 是平台别名，由 [src/platform/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs) 用 `cfg` 绑定到具体 OS 实现，使 `main.rs` 不含任何平台特化代码。
- `plt::Tun::create` 返回 `(readers, writer, status)` 三元组，对应“多队列读 / 共享写 / 独立事件源”的数据面需求。
- `WireGuard::new` 内部已 spawn 了 `num_cpus` 个 `handshake_worker`，`add_tun_reader` 为每个 reader spawn 一个 `tun_worker`；此时 UDP reader 尚未注册（要等 UAPI 下发 `listen_port`）。
- Tun 事件线程把 `Up(mtu)`/`Down` 翻译成 `cfg.up/down`，驱动整个核心与各 peer 定时器的启停；UAPI 线程为每条 `wg(8)` 连接 spawn 一个线程跑 `configuration::uapi::handle`。
- `wg.wait()` 通过 `WaitCounter`（`Mutex<usize>` + `Condvar`）等待所有 `tun_worker` 退出，是进程优雅退出的唯一解除条件。

## 7. 下一步学习建议

- **下一讲（u1-l5）** 会展开本讲里被当成“黑盒”的两步——`util::drop_privileges()`（chroot + 切 nobody）与 `util::daemonize()`（双 fork + setsid），理解 Unix 守护进程化的细节。
- 想搞清 `status.event()` 是怎么从内核 netlink 收到 Up/Down 的，进入 **u2 单元**（平台抽象层），尤其是 u2-l2（Linux TUN）与 u2-l3（Linux UDP）。
- 想搞清 `WireGuard::new` 背后的握手队列、`tun_worker`/`udp_worker`/`handshake_worker` 各自做什么，进入 **u3 单元**（WireGuard 核心与 IO 工作线程），从 u3-l1 开始。
- 想搞清 `configuration::uapi::handle` 如何解析 `wg(8)` 下发的文本协议，进入 **u6 单元**（配置接口与 UAPI 协议）。
