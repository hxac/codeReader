# Configuration 抽象与 WireGuardConfig

## 1. 本讲目标

本讲打开 `src/configuration/` 模块，理解 wireguard-rs 如何用一个「配置接口层」把复杂的协议核心类型对宿主应用隐藏起来。学完后你应该能够：

- 说清 `Configuration` trait 存在的理由，以及它把哪些「实现细节」对调用方隐藏。
- 画出 `WireGuardConfig` 用 `Arc<Mutex<Inner>>` 包装 `WireGuard<T,B>` 的结构，并解释为什么这样能隐藏 IO 泛型。
- 复述 `start_listener` 的完整步骤：`B::bind` → `set_fwmark` → `set_writer` → `add_udp_reader` → 存 `Owner`。
- 讲清 `up` / `down` / `set_listen_port` / `set_fwmark` 四个操作各自的副作用，尤其是「设备已绑定时改端口要释放旧监听器、重建新监听器」的流程。
- 看懂 `get_peers` 如何遍历各 peer 把运行态聚合成 `PeerState` 快照。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些概念（它们来自前置讲义，本讲不会重复）：

- **三大顶层模块与依赖方向**：`configuration → wireguard → (handshake, router) → platform(trait)`（见 u1-l3）。本讲的 `configuration` 正是位于最上层、向宿主暴露配置能力的层。
- **平台别名与 trait**：`WireGuard<T: Tun, B: UDP>` 用泛型 + 关联类型对接平台 IO（见 u2-l1）。`T`、`B` 这两个泛型参数会一路向上传染到配置层，是本讲要「隐藏」的核心对象。
- **`PlatformUDP::bind` 的三元组**：`bind(port) -> (Vec<Reader>, Writer, Owner)`（见 u2-l1 / u2-l3）。其中 `Owner` 在被 `drop` 时会关闭 UDP 套接字，这是本讲「释放旧 bind」机制的物理基础。
- **`WireGuard` 设备状态**：`WireguardInner` 持有 `peers`（握手 device）、`router`（数据面）以及 `up/down`、`set_writer`、`add_udp_reader` 等方法（见 u3-l1）。
- **UAPI 文本协议**：`wg(8)` 通过 `/var/run/wireguard/<iface>/sock` 下发 `get=1` / `set=1` 文本指令（见 u1-l4 / u2-l1）。本讲的 `Configuration` trait 就是这些指令最终落到的那一层接口。

一个直觉：协议核心 `WireGuard<T,B>` 是「带两个泛型参数的复杂机器」，而宿主应用（main.rs、UAPI 解析器）只想要几个简单的旋钮——开机、关机、设端口、加 peer。`Configuration` 就是这台复杂机器的「控制面板」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/configuration/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/mod.rs) | 模块入口，声明子模块并把 `Configuration`、`WireGuardConfig`、`ConfigError` 重新导出（re-export）给上层。 |
| [src/configuration/config.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs) | 本讲主角：定义 `PeerState`、`WireGuardConfig`/`Inner`、`Configuration` trait、`start_listener` 以及 trait 的完整实现。 |
| [src/configuration/error.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/error.rs) | 定义 `ConfigError` 枚举，并把它映射成 Unix `errno`（供 UAPI 协议回写）。 |
| [src/platform/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs) | `Owner` 与 `PlatformUDP::bind` 的 trait 契约，是 `start_listener` 调用的底层能力。 |
| [src/wireguard/wireguard.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs) | `WireGuard` 的 `up`/`down`/`set_writer`/`add_udp_reader` 等方法，是配置层调用的协议核心入口。 |
| [src/platform/linux/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs) | `LinuxOwner` 的 `get_port`/`set_fwmark`/`Drop`，让本讲的「释放旧 bind」落到真实的 socket 关闭行为上。 |

依赖方向小结：本讲的 `WireGuardConfig` 持有一个 `WireGuard<T,B>`，对上以 `Configuration` trait（无泛型出现在方法签名里）暴露，对下调用 `WireGuard` 与 `B::bind`/`B::Owner`。

## 4. 核心概念与源码讲解

### 4.1 设计目标：为什么需要 Configuration 抽象层

#### 4.1.1 概念说明

协议核心 `WireGuard<T: Tun, B: UDP>` 是高度参数化的类型——`T`、`B` 两个泛型里还嵌套着各自的关联类型（`T::Reader`、`B::Writer`、`B::Owner`、`B::Endpoint` …）。任何一个想嵌入 wireguard-rs 的宿主应用，如果直接拿着 `WireGuard<plt::Tun, plt::UDP>` 写代码，就会被这些泛型「传染」：宿主自己的结构体也得带上 `<T, B>` 参数。

`Configuration` trait 的存在就是为了切断这种传染。源码顶部的注释把目标说得非常直白：

```rust
/// The goal of the configuration interface is, among others,
/// to hide the IO implementations (over which the WG device is generic),
/// from the configuration and UAPI code.
///
/// Furthermore it forms the simpler interface for embedding WireGuard in other applications,
/// and hides the complex types of the implementation from the host application.
```

参见 [src/configuration/config.rs:12-17](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L12-L17)。它有两层意图：

1. **隐藏 IO 实现**：把 `T`/`B` 泛型以及它们的关联类型从配置与 UAPI 代码里彻底抹掉。
2. **提供更简单的嵌入接口**：宿主应用只面对一个 trait 上的几十个语义化方法（`up`、`set_listen_port`、`add_peer`…），不需要知道路由器、握手、KeyWheel 等内部类型。

#### 4.1.2 核心流程

抽象层在调用链中的位置：

```
wg(8) 文本指令
   │  (get=1 / set=1)
   ▼
configuration::uapi::handle        ← 文本协议解析（u6-l2~u6-l4）
   │  调用
   ▼
Configuration trait                ← 本讲的「控制面板」
   │  实现
   ▼
WireGuardConfig<Inner>             ← Arc<Mutex<>> 包装
   │  转发
   ▼
WireGuard<T, B>  /  B::bind / B::Owner   ← 协议核心 + 平台 IO
```

关键点：上层（UAPI 解析器）只面向 `&dyn Configuration` 或泛型 `impl Configuration`，看不到 `T`、`B`；泛型只在 `WireGuardConfig` 这一层被「吸收」。

#### 4.1.3 源码精读

`Configuration` trait 的方法签名里**完全不含泛型参数**，这是「隐藏」的直接证据。例如：

```rust
fn up(&self, mtu: usize) -> Result<(), ConfigError>;
fn set_listen_port(&self, port: u16) -> Result<(), ConfigError>;
fn add_peer(&self, peer: &PublicKey) -> bool;
fn get_peers(&self) -> Vec<PeerState>;
```

参见 [src/configuration/config.rs:64-193](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L64-L193)。注意参数类型都是「平台无关」的：`PublicKey`、`IpAddr`、`SocketAddr`、`u16`、`u32`，没有任何 `T::Reader` 或 `B::Endpoint` 露出来。把一个具体端点地址 `SocketAddr` 传进来后，由实现内部再把它转成 `B::Endpoint`（见 4.5 节）。

模块入口的 re-export 也佐证了「只暴露 trait 与配置类型、不暴露泛型核心」的意图：

```rust
pub use error::ConfigError;
pub use config::Configuration;
pub use config::WireGuardConfig;
```

参见 [src/configuration/mod.rs:9-12](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/mod.rs#L9-L12)。注意 `WireGuard<T,B>` 本身并没有从这里 re-export——它被封装在 `WireGuardConfig` 内部。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「隐藏泛型」的效果。

**操作步骤**：

1. 打开 `src/configuration/config.rs`，定位 `pub trait Configuration`（约 64 行起）。
2. 浏览全部方法签名，记录每个方法参数/返回值用到的所有类型。
3. 打开 `src/wireguard/wireguard.rs`，对比 `WireGuard<T, B>` 上那些带泛型约束的方法（如 `set_writer(&self, writer: B::Writer)`）。

**需要观察的现象**：`Configuration` trait 的方法签名里出现的类型应当全部是标准库或本 crate 的「普通类型」（`PublicKey`、`IpAddr`、`SocketAddr`、`StaticSecret`、`PeerState`、`ConfigError`、各种整数），不出现 `T::`、`B::` 或 `plt::`。

**预期结果**：你能得出结论——「任何调用方只要面向 `Configuration` trait 写代码，就不需要在自己的类型上携带 `<T, B>` 泛型」。这正是 trait 抽象的回报。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Configuration` trait 不能用「返回 `WireGuard<T,B>` 的方法」来暴露内部状态？
**答案**：因为 `WireGuard<T,B>` 自带 `T`、`B` 泛型，一旦出现在 trait 方法签名里，泛型就会传染给所有 `impl Configuration` 的调用方，与「隐藏 IO 实现」的目标直接冲突。所以 trait 只用 `PeerState`、`ConfigError` 这类不含泛型的类型。

**练习 2**：`Configuration` 的哪些方法返回 `Result<_, ConfigError>`，哪些不返回？请归类并猜测原因。
**答案**：返回 `Result` 的有 `up`、`set_listen_port`、`set_fwmark`——它们都涉及绑定 socket 或设 fwmark，可能失败。不返回 `Result` 的（如 `add_peer`、`set_endpoint`、`set_preshared_key`）要么是「已存在则 no-op」的幂等操作，要么其失败（如 peer 不存在）被静默忽略。见 4.5 节实现。

---

### 4.2 WireGuardConfig 与 Inner：用 Arc<Mutex> 隐藏 IO 泛型

#### 4.2.1 概念说明

`Configuration` 是接口，`WireGuardConfig` 是它的唯一实现。`WireGuardConfig` 做两件事：

1. **持有协议核心** `WireGuard<T,B>` 以及与 UDP 监听相关的配置（端口、当前 `Owner`、fwmark）。
2. **提供线程安全的共享**：它内部是 `Arc<Mutex<Inner>>`，可以廉价克隆进多个工作线程，并用一把互斥锁串行化所有配置变更。

`Inner` 是真正存放状态的私有结构；`WireGuardConfig` 只是包了一层 `Arc<Mutex<>>` 的句柄。

#### 4.2.2 核心流程

类型包装层次：

```
WireGuardConfig<T, B>            ← 对外句柄，可 Clone
   = Arc<Mutex<Inner<T, B>>>
              │
              └─ Inner<T, B>
                    ├─ wireguard: WireGuard<T, B>   ← 协议核心
                    ├─ port: u16                    ← 配置端口
                    ├─ bind: Option<B::Owner>       ← 当前监听器（None=未绑定）
                    └─ fwmark: Option<u32>          ← 防火墙标记
```

- `port` 是「期望监听的端口」，由 `set_listen_port` 写入，由 `start_listener` 实际生效。
- `bind` 是「当前真正持有的监听器」。`Some` 表示已绑定（设备通常已 up），`None` 表示未绑定。它同时是「释放旧 bind」的抓手——把它置 `None` 就会 drop 掉 `Owner`，从而关闭 socket。
- `fwmark` 是与端口并列的平台标记，在新监听器建立时一并应用。

#### 4.2.3 源码精读

`WireGuardConfig` 与 `Inner` 的定义：

```rust
pub struct WireGuardConfig<T: tun::Tun, B: udp::PlatformUDP>(Arc<Mutex<Inner<T, B>>>);

struct Inner<T: tun::Tun, B: udp::PlatformUDP> {
    wireguard: WireGuard<T, B>,
    port: u16,
    bind: Option<B::Owner>,
    fwmark: Option<u32>,
}
```

参见 [src/configuration/config.rs:31-38](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L31-L38)。注意 `WireGuardConfig` 是个**元组结构体**，唯一的字段就是 `Arc<Mutex<Inner>>`——泛型 `T`、`B` 仍然存在，但被锁在了这一层之内。

`new` 把一个已构造好的 `WireGuard` 核心装进去，初始未绑定：

```rust
pub fn new(wg: WireGuard<T, B>) -> WireGuardConfig<T, B> {
    WireGuardConfig(Arc::new(Mutex::new(Inner {
        wireguard: wg,
        port: 0,
        bind: None,
        fwmark: None,
    })))
}
```

参见 [src/configuration/config.rs:47-54](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L47-L54)。`port: 0` 表示「让内核随机分配端口」（见 u2-l3 中 bind6 取回真实端口的逻辑）；`bind: None` 表示启动时尚未绑定 UDP。

所有配置操作都先经过统一的 `lock()` 拿到 `MutexGuard`：

```rust
fn lock(&self) -> MutexGuard<Inner<T, B>> {
    self.0.lock().unwrap()
}
```

参见 [src/configuration/config.rs:41-43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L41-L43)。这把锁保证任意时刻只有一个线程能修改 `Inner`（端口、bind、fwmark），杜绝并发改配置时的竞态。`.unwrap()` 在这里意味着「持有锁的线程 panic 也不会 poison」这一假设由上层兜底。

`Clone` 只克隆 `Arc`，因此多线程共享同一份 `Inner`：

```rust
impl<T: tun::Tun, B: udp::PlatformUDP> Clone for WireGuardConfig<T, B> {
    fn clone(&self) -> Self {
        WireGuardConfig(self.0.clone())
    }
}
```

参见 [src/configuration/config.rs:57-61](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L57-L61)。

#### 4.2.4 代码实践

**实践目标**：理解「锁的粒度」——一次配置调用持有锁的整个范围。

**操作步骤**：

1. 阅读 `up` 的实现（4.5 节会详述）：它先 `self.lock()`，再依次调用 `cfg.wireguard.up(mtu)` 与 `start_listener(cfg)`。
2. 注意 `start_listener` 接收的是 `MutexGuard<Inner>`（按值传入），意味着锁在整个绑定过程中都不释放。
3. 阅读握手 worker 的并发模型（u4-l6），了解握手线程池在 `lock()` 期间仍在消费队列。

**需要观察的现象**：配置层的 `Mutex` 只保护 `Inner` 这一份状态，**不**保护协议核心内部的并发结构（`WireguardInner` 用的是自己的 `spin::RwLock`/`DashMap`/原子量，见 u3-l1）。

**预期结果**：你应能解释——为什么改端口时要持锁：防止两个 `set_listen_port` 同时重建监听器；为什么持锁期间不会卡死数据面：数据面用的是协议核心内部自己的锁，配置锁与数据面锁是两套。

#### 4.2.5 小练习与答案

**练习 1**：`Inner` 的 `bind` 字段类型是 `Option<B::Owner>`。为什么是 `Option`？
**答案**：设备可能处于「未绑定」状态（启动后、`up` 之前，或 `down` 之后）。`None` 表示当前没有 UDP 监听器；`Some(owner)` 表示已绑定。`Owner` 被 drop 即关闭 socket，所以 `Option` 还兼任「释放」开关。

**练习 2**：`WireGuardConfig` 用的是 `std::sync::Mutex`，而协议核心 `WireguardInner` 大量用 `spin::Mutex`/`spin::RwLock`。两者为何不同？
**答案**：配置操作是低频、可阻塞的（绑定 socket、解析配置），用标准 `Mutex` 持锁几十微秒无妨；数据面是高频热路径，需要极短临界区，故用自旋锁减少线程挂起开销。这是「控制面 vs 数据面」的典型锁策略分野。

---

### 4.3 Configuration trait：设备级与 peer 级配置接口

#### 4.3.1 概念说明

`Configuration` trait 把全部配置能力分成两类：

- **设备级（interface-level）**：影响整个 WireGuard 设备的操作，如 `up`/`down`、`set_private_key`、`set_listen_port`、`set_fwmark`、`replace_peers`、`get_peers`。
- **peer 级（peer-level）**：针对单个 peer（用 `&PublicKey` 标识）的操作，如 `add_peer`/`remove_peer`、`set_endpoint`、`set_preshared_key`、`set_persistent_keepalive_interval`、`add_allowed_ip`/`replace_allowed_ips`。

这两类对应 UAPI 协议里「接口行」与「peer 行」的区分（u6-l2~u6-l4 会展开）。

#### 4.3.2 核心流程

trait 方法的语义分组：

| 分组 | 方法 | 备注 |
|------|------|------|
| 设备启停 | `up(mtu)`、`down()` | up 绑定 UDP；down 解绑 |
| 设备密钥 | `set_private_key`、`get_private_key`、`get_protocol_version` | 协议版本恒为 1 |
| 网络绑定 | `set_listen_port`、`get_listen_port`、`set_fwmark`、`get_fwmark` | 涉及 socket |
| peer 增删 | `replace_peers`、`remove_peer`、`add_peer` | 幂等 |
| peer 属性 | `set_preshared_key`、`set_endpoint`、`set_persistent_keepalive_interval`、`replace_allowed_ips`、`add_allowed_ip` | peer 不存在则静默忽略 |
| 状态查询 | `get_peers` | 返回 `Vec<PeerState>` |

#### 4.3.3 源码精读

trait 定义里，peer 级方法都以 `&PublicKey` 起头，体现「按公钥寻址 peer」：

```rust
fn add_peer(&self, peer: &PublicKey) -> bool;
fn set_preshared_key(&self, peer: &PublicKey, psk: [u8; 32]);
fn set_endpoint(&self, peer: &PublicKey, addr: SocketAddr);
fn set_persistent_keepalive_interval(&self, peer: &PublicKey, secs: u64);
fn add_allowed_ip(&self, peer: &PublicKey, ip: IpAddr, masklen: u32);
```

参见 [src/configuration/config.rs:118-181](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L118-L181)。

实现里，peer 级方法都遵循同一个模式——「读 peer map → 命中则委托给 peer 的方法，否则 no-op」：

```rust
fn set_endpoint(&self, peer: &PublicKey, addr: SocketAddr) {
    if let Some(peer) = self.lock().wireguard.peers.read().get(peer) {
        peer.set_endpoint(B::Endpoint::from_address(addr));
    }
}
```

参见 [src/configuration/config.rs:311-315](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L311-L315)。这里 `peers.read().get(peer)` 返回握手 device 中该 peer 对应的**路由器 peer 句柄**（即 `handshake::Device` 的 opaque 类型 `O`，见 u3-l1），其上的 `set_endpoint`、`add_allowed_ip`、`list_allowed_ips` 等方法在路由器 peer 上实现（见 [src/wireguard/router/peer.rs:363-380](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L363-L380)）。

关键细节：`addr: SocketAddr`（平台无关）被 `B::Endpoint::from_address(addr)` 转成平台相关的 `B::Endpoint`（见 [src/platform/endpoint.rs:3-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs#L3-L7)）。这就是「trait 暴露普通类型、实现内部转换成泛型类型」的典型边界。

`set_persistent_keepalive_interval` 多走一步 `.opaque()`，因为 keepalive 间隔存在路由器 peer 暴露出的 opaque 对象（`PeerInner`，持有 `timers`）上：

```rust
fn set_persistent_keepalive_interval(&self, peer: &PublicKey, secs: u64) {
    if let Some(peer) = self.lock().wireguard.peers.read().get(peer) {
        peer.opaque().set_persistent_keepalive_interval(secs);
    }
}
```

参见 [src/configuration/config.rs:317-321](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L317-L321)。

设备级方法里，`set_private_key` 直接转发给协议核心：

```rust
fn set_private_key(&self, sk: Option<StaticSecret>) {
    log::info!("configuration, set private key");
    self.lock().wireguard.set_key(sk)
}
```

参见 [src/configuration/config.rs:243-245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L243-L245)。`WireGuard::set_key` 会重算每个 peer 的 DH 共享密钥并清空发送密钥（见 u3-l1 / u4-l2）。

#### 4.3.4 代码实践

**实践目标**：追踪一个 peer 属性写入是「no-op 还是真的生效」。

**操作步骤**：

1. 阅读 `set_preshared_key`（[src/configuration/config.rs:307-309](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L307-L309)），注意它**没有** `if let Some(peer)` 守卫，而是直接 `self.lock().wireguard.set_psk(*peer, psk)`。
2. 对比 `set_endpoint`，它有守卫。
3. 阅读 `WireGuard::set_psk`（[src/wireguard/wireguard.rs:198-200](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L198-L200)）：`self.peers.write().set_psk(pk, psk).is_ok()`，握手层的 `set_psk` 在 peer 不存在时返回 `Err`，被 `.is_ok()` 吞掉。

**需要观察的现象**：`set_preshared_key` 与 `set_endpoint` 用了不同的「peer 不存在」处理方式——前者靠下层返回值静默，后者靠上层守卫短路。

**预期结果**：你能总结出「peer 不存在时所有 peer 级写入都是安全的 no-op」，只是实现位置不同。

#### 4.3.5 小练习与答案

**练习 1**：`get_protocol_version` 永远返回 `1`。为什么这个方法存在？
**答案**：UAPI 协议里有 `protocol_version` 字段，`wg(8)` 可能查询它。WireGuard 协议目前只有版本 1，所以硬编码返回。它属于「接口级查询」的一员，让 trait 表达能力完整。

**练习 2**：`add_peer` 返回 `bool`，而 `set_endpoint` 返回 `()`。为什么 `add_peer` 要返回布尔值？
**答案**：UAPI `set` 协议在添加一个已存在的 peer 时需要知道是否真的新增了（用于决定后续是否走「新 peer 初始化」路径）。`add_peer` 返回 `bool` 表示「是否真的添加」（已存在则返回 `false`）。`set_endpoint` 对不存在的 peer 无副作用，无需返回值。

---

### 4.4 start_listener：UDP 监听器的绑定与装配

#### 4.4.1 概念说明

`start_listener` 是配置层的「装配车间」：它把一个端口号变成一组正在运行的 UDP 收发资源，并把它们安装到协议核心里。它是 `up` 与「改端口/改 fwmark后重建」共同调用的底层函数。

它完成四件事：

1. 调 `B::bind(port)` 拿到新的 `(readers, writer, owner)` 三元组。
2. 在新 `owner` 上设置 fwmark。
3. 把 `writer` 装进协议核心（路由器的出站发送器）。
4. 为每个 `reader` 启动一个 `udp_worker` 线程。

#### 4.4.2 核心流程

```
start_listener(cfg):
  cfg.bind = None                  ← 先丢弃旧 Owner（若有），drop → 关闭旧 socket
  (readers, writer, owner) = B::bind(cfg.port)
        失败 → 返回 FailedToBind
  owner.set_fwmark(cfg.fwmark)     ← 应用防火墙标记（忽略错误）
  cfg.wireguard.set_writer(writer) ← 装出站发送器
  for reader in readers:
      cfg.wireguard.add_udp_reader(reader)   ← 每路 reader 起一个 udp_worker
  cfg.bind = Some(owner)           ← 保存新 Owner（drop 时关 socket）
```

注意第一步 `cfg.bind = None` 是「释放旧 bind」的关键：把旧 `Owner` 置空会触发其 `Drop`，从而 `shutdown` 旧 socket。这对 Linux 而言会解除 `udp_worker` 线程在 `reader.read` 上的阻塞（读返回错误 → 线程退出），实现旧监听器的干净拆除（见 u2-l3 与 [src/platform/linux/udp.rs:483-494](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L483-L494)）。

#### 4.4.3 源码精读

`start_listener` 接收 `MutexGuard<Inner>`（不是 `&self`），因此它复用调用方已持有的锁：

```rust
fn start_listener<T: tun::Tun, B: udp::PlatformUDP>(
    mut cfg: MutexGuard<Inner<T, B>>,
) -> Result<(), ConfigError> {
    cfg.bind = None;

    // create new listener
    let (mut readers, writer, mut owner) = match B::bind(cfg.port) {
        Ok(r) => r,
        Err(_) => {
            return Err(ConfigError::FailedToBind);
        }
    };

    // set fwmark
    let _ = owner.set_fwmark(cfg.fwmark); // TODO: handle

    // set writer on WireGuard
    cfg.wireguard.set_writer(writer);

    // add readers
    while let Some(reader) = readers.pop() {
        cfg.wireguard.add_udp_reader(reader);
    }

    // create new UDP state
    cfg.bind = Some(owner);
    Ok(())
}
```

参见 [src/configuration/config.rs:195-222](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L195-L222)。逐行解读：

- **第 198 行 `cfg.bind = None`**：丢弃旧 `Owner`。即使调用方已清空（如 `set_listen_port` 用 `mem::replace` 取走），这里再清一次也无害，且让 `up` 路径（没预先清）也安全。
- **第 201 行 `B::bind(cfg.port)`**：平台无关的绑定入口。Linux 上会创建 IPv4/IPv6 双栈 socket 并复用端口（见 u2-l3）。`cfg.port == 0` 时由内核分配端口。
- **第 209 行 `owner.set_fwmark(cfg.fwmark)`**：`let _ =` 故意忽略错误（源码标注了 `// TODO: handle`），意味着设 fwmark 失败不会阻止监听器建立。
- **第 212 行 `cfg.wireguard.set_writer(writer)`**：把出站 writer 装进路由器。`WireGuard::set_writer` 实现为 `self.router.set_outbound_writer(writer)`（见 [src/wireguard/wireguard.rs:247-249](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L247-L249)）。
- **第 215-217 行 `while let Some(reader) = readers.pop()`**：`readers` 是 `Vec`，每个 reader 起一个 `udp_worker`。`add_udp_reader` 内部 `thread::spawn(move || udp_worker(&wg, reader))`（见 [src/wireguard/wireguard.rs:240-245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L240-L245)）。Linux 双栈通常有 2 个 reader（v4/v6）。
- **第 220 行 `cfg.bind = Some(owner)`**：保存新 `Owner`，它持有 socket 的引用计数，drop 时关闭。

底层 `B::bind` 与 `Owner` 的契约在 trait 层定义：

```rust
pub trait PlatformUDP: UDP {
    type Owner: Owner;
    fn bind(port: u16) -> Result<(Vec<Self::Reader>, Self::Writer, Self::Owner), Self::Error>;
}

pub trait Owner: Send {
    fn get_port(&self) -> u16;
    fn set_fwmark(&mut self, value: Option<u32>) -> Result<(), Self::Error>;
}
```

参见 [src/platform/udp.rs:28-46](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L28-L46)。注释明确写道：`Owner` 在 `drop` 时关闭 UDP socket，并能配置 fwmark。这正是 `start_listener` 第 198 行与第 220 行所依赖的语义。

错误映射：`B::bind` 失败时返回 `ConfigError::FailedToBind`，它在 `errno()` 里映射为 `EPERM`（见 [src/configuration/error.rs:45-46](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/error.rs#L45-L46)）——这会被 UAPI 协议回写给 `wg(8)`。

#### 4.4.4 代码实践

**实践目标**：用注释把 `start_listener` 每一步对应到「它改了哪一份状态、起了哪个副作用」。

**操作步骤**（源码阅读型实践）：

1. 在 [src/configuration/config.rs:198](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L198) 上方加一行中文注释：`// 释放旧 Owner：drop → LinuxOwner::Drop → shutdown(sock4/sock6) → 旧 udp_worker 的 read 报错退出`。
2. 在 [src/platform/linux/udp.rs:483-494](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L483-L494) 的 `Drop for LinuxOwner` 处确认：它对 v4、v6 两个 fd 各调用一次 `libc::shutdown(fd, SHUT_RDWR)`。
3. 在 [src/wireguard/wireguard.rs:240-245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L240-L245) 的 `add_udp_reader` 处确认：每个 reader 起一个 `udp_worker` 线程。

**需要观察的现象**：`start_listener` 改的状态分三处——`Inner.bind`（本层）、`router` 的出站 writer（核心层）、新建的 udp_worker 线程（系统层）。

**预期结果**：你能完整复述一次 `start_listener` 调用的全部副作用链，并指出「为什么旧 socket 必须先关掉再 bind 新的」（否则同端口复用阶段可能出现两个监听器并存）。

#### 4.4.5 小练习与答案

**练习 1**：`start_listener` 为什么接收 `MutexGuard<Inner>` 而不是 `&self`？
**答案**：它要修改 `Inner.bind`（要写 `cfg.bind = None` 和 `cfg.bind = Some(owner)`），并且整个绑定过程必须在同一把锁内完成，避免 `set_listen_port` 与 `up` 并发时互相踩踏。接收已持有的 `MutexGuard` 让调用方决定锁的范围，避免函数内部重复加锁（`std::sync::Mutex` 不可重入）。

**练习 2**：`owner.set_fwmark(cfg.fwmark)` 的返回值被 `let _ =` 丢弃。这会带来什么潜在问题？
**答案**：设 fwmark 失败（例如权限不足、平台不支持）会被静默忽略，监听器照常建立，但 fwmark 未生效——可能导致策略路由场景下回包走错路由表。源码已用 `// TODO: handle` 标注这一遗留问题。

---

### 4.5 关键操作：up/down/set_listen_port/set_fwmark 的重新绑定

#### 4.5.1 概念说明

本节是本讲的重心，尤其是 `set_listen_port` 的「条件性重建」逻辑。四个操作合起来回答一个问题：**配置层的变更如何同步到正在运行的 UDP 监听器与协议核心**。

核心规则：
- `up`：先唤醒协议核心，再绑定 UDP。
- `down`：先让协议核心休眠，再丢弃监听器。
- `set_listen_port`：只更新 `port`；**若当前已绑定**，则顺便用新端口重建监听器；若未绑定，则只记账、等 `up` 时再生效。
- `set_fwmark`：若已绑定，直接在现有 `Owner` 上设；若未绑定，只记账、等下次 `start_listener` 时生效。

#### 4.5.2 核心流程

`set_listen_port` 的决策流（本讲指定的实践任务重点）：

```
set_listen_port(port):
  lock cfg
  old = mem::replace(&mut cfg.bind, None)   ← 取出旧 Owner（drop → 关旧 socket）
  cfg.port = port                            ← 记下新端口
  bound = old.is_some()                      ← 之前是否在监听？
  ┌─ bound == true  → start_listener(cfg)    ← 用新端口重建监听器
  └─ bound == false → Ok(())                 ← 未监听，仅记账，等 up()
```

关键设计：**是否重建取决于「当前是否在监听」，而不是「设备是否 up」**。这两个状态在 `Inner` 里被合并为 `bind.is_some()` 这一个布尔量。

#### 4.5.3 源码精读

`up` —— 唤醒核心 + 绑定：

```rust
fn up(&self, mtu: usize) -> Result<(), ConfigError> {
    log::info!("configuration, set device up");
    let cfg = self.lock();
    cfg.wireguard.up(mtu);
    start_listener(cfg)
}
```

参见 [src/configuration/config.rs:225-230](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L225-L230)。顺序很重要：先 `wireguard.up(mtu)`（设 MTU、启路由器、起各 peer 定时器，见 [src/wireguard/wireguard.rs:153-175](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L153-L175)），再 `start_listener(cfg)` 让 UDP 收发上线。注意 `cfg`（`MutexGuard`）被 move 进 `start_listener`，锁一路持有到绑定结束。

`down` —— 休眠核心 + 解绑：

```rust
fn down(&self) {
    log::info!("configuration, set device down");
    let mut cfg = self.lock();
    cfg.wireguard.down();
    cfg.bind = None;
}
```

参见 [src/configuration/config.rs:232-237](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L232-L237)。`wireguard.down()` 设 `mtu=0`、停路由器、停各 peer 定时器（见 [src/wireguard/wireguard.rs:127-149](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L127-L149)）；随后 `cfg.bind = None` drop 掉 `Owner`，关闭 socket 并让 udp_worker 退出。设备状态被保留（peer 列表、密钥），只是停止收发。

`set_listen_port` —— 本讲的实践重点：

```rust
fn set_listen_port(&self, port: u16) -> Result<(), ConfigError> {
    log::trace!("Config, Set listen port: {:?}", port);

    // update port and take old bind
    let mut cfg = self.lock();
    let bound: bool = {
        let old = mem::replace(&mut cfg.bind, None);
        cfg.port = port;
        old.is_some()
    };

    // restart listener if bound
    if bound {
        start_listener(cfg)
    } else {
        Ok(())
    }
}
```

参见 [src/configuration/config.rs:262-279](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L262-L279)。逐步拆解「设备已绑定时改端口」的流程：

1. **取出旧 bind**：`mem::replace(&mut cfg.bind, None)` 把旧的 `Some(owner)` 移出来赋给局部 `old`，`cfg.bind` 变 `None`。此时旧 `Owner` 尚未 drop（还在 `old` 里）。
2. **更新端口**：`cfg.port = port`，记下新端口。
3. **记录是否曾绑定**：内层块结束时 `old` 被 drop → 旧 `Owner` 析构 → `shutdown` 旧 socket → 旧 udp_worker 报错退出。`bound = old.is_some()` 在 drop 之前已取出。
4. **条件重建**：若 `bound`（之前在监听），调 `start_listener(cfg)`，它用**新** `cfg.port` 执行 `B::bind`，重新装 writer、起新 udp_worker、存新 `Owner`。若未绑定，直接 `Ok(())`——端口已记在 `cfg.port`，等下次 `up()` 时 `start_listener` 自然读取它。

这就是「释放旧 bind 并用新端口重建 listener」的完整实现：用 `mem::replace` 取旧、用 `start_listener` 建新，二者通过 `cfg.bind` 这一个 `Option` 衔接。

`set_fwmark` —— 仅在已绑定时立即生效：

```rust
fn set_fwmark(&self, mark: Option<u32>) -> Result<(), ConfigError> {
    log::trace!("Config, Set fwmark: {:?}", mark);
    match self.lock().bind.as_mut() {
        Some(bind) => {
            if bind.set_fwmark(mark).is_err() {
                Err(ConfigError::IOError)
            } else {
                Ok(())
            }
        }
        None => Ok(()),
    }
}
```

参见 [src/configuration/config.rs:281-293](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L281-L293)。注意与 `set_listen_port` 的差异：`set_fwmark` **不修改 `cfg.fwmark` 字段**，只在现有 `Owner` 上即时设置。这意味着：若调用 `set_fwmark` 时未绑定，fwmark **不会**被记住，下次 `start_listener` 仍用 `cfg.fwmark`（初始 `None`）。这是一个值得注意的不对称——`port` 会被记账而 `fwmark` 在此方法里不会。

`get_listen_port` 从当前 `Owner` 读取**真实**端口：

```rust
fn get_listen_port(&self) -> Option<u16> {
    let st = self.lock();
    log::trace!("Config, Get listen port, bound: {}", st.bind.is_some());
    st.bind.as_ref().map(|bind| bind.get_port())
}
```

参见 [src/configuration/config.rs:256-260](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L256-L260)。它不返回 `cfg.port`（期望端口），而是返回 `Owner::get_port()`（实际绑定端口，当 `cfg.port==0` 时是内核分配的真实端口）。这也解释了为什么用 `Option<u16>`：未绑定时没有「真实端口」可报。

#### 4.5.4 代码实践（本讲指定任务）

**实践目标**：为 `Configuration::set_listen_port` 写一份调用流程说明，讲清「设备已绑定时如何释放旧 bind 并用新端口重建 listener」。

**操作步骤**：

1. 打开 [src/configuration/config.rs:262-279](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L262-L279)，对照上面的逐步拆解。
2. 在该函数上方用中文写一段文档注释，覆盖以下要点（这是你产出的「说明」，不要改函数逻辑，只加注释）：
   - 进入时设备处于已绑定状态（`cfg.bind == Some`），假设旧端口 `P0`、新端口 `P1`。
   - `mem::replace` 取出旧 `Owner`，`cfg.port` 改为 `P1`，`bound = true`。
   - 内层块结束，旧 `Owner` drop → 触发 [src/platform/linux/udp.rs:483](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/udp.rs#L483) 的 `shutdown(sock4)` 与 `shutdown(sock6)` → 旧 `udp_worker` 的 `reader.read` 返回错误并退出。
   - `start_listener(cfg)` 用 `P1` 调 `B::bind`，得到新 `(readers, writer, owner)`；设 fwmark；`set_writer` 装新出站器；每个 reader 起新 `udp_worker`；存新 `Owner`。
3. 追踪一个反例：若进入时 `cfg.bind == None`（设备未 up），流程在哪一步提前返回？为什么此时改端口不会失败？

**需要观察的现象**：注释写完后，你应该能一眼看出「重建」与「仅记账」的分叉点就是 `if bound`。

**预期结果**：你产出一段不依赖运行、纯源码可验证的流程说明，并指出两个边界——(a) `cfg.port == 0` 时由内核选端口，`get_listen_port` 经 `Owner::get_port` 返回真实值；(b) `set_fwmark` 在未绑定时不会记账，与 `set_listen_port` 不对称。

**待本地验证**：若你想观察真实行为，可在测试中用 `dummy::PairBind`（见 u2-l4）装配一个 `WireGuardConfig`，先 `up(1420)` 再 `set_listen_port(51821)`，但注意 dummy 的 `PlatformUDP::bind` 留空返回错误（见 u2-l4），故 dummy 下 `start_listener` 会返回 `FailedToBind`——这本身验证了「重建会调 bind」的事实。

#### 4.5.5 小练习与答案

**练习 1**：假设设备已 up（`bind == Some`），依次执行 `set_listen_port(51821)`、`down()`、`set_listen_port(51822)`、`up(1420)`。最终监听端口是多少？`cfg.port` 经历了哪些值？
**答案**：`set_listen_port(51821)` 重建为 51821（`cfg.port=51821`）；`down()` 把 `bind=None`；`set_listen_port(51822)` 因未绑定只记账（`cfg.port=51822`，不重建）；`up(1420)` 调 `start_listener` 用 `cfg.port=51822` 绑定。最终监听端口 51822。`cfg.port` 经历 `初始0 → 51821 → 51822`。

**练习 2**：为什么 `set_listen_port` 在已绑定时要**先关旧 socket 再 bind 新的**，而不是先 bind 新的再关旧的？
**答案**：若先 bind 新的，新 socket 与旧 socket 在同端口（或同地址）上会短暂并存，可能触发 `EADDRINUSE`（端口复用未生效时），且旧 udp_worker 与新 udp_worker 会同时消费报文造成混乱。先关旧的（`Owner::drop` → `shutdown`）能干净拆除旧 worker，再 bind 新的避免冲突。

**练习 3**：`set_fwmark` 没有写 `cfg.fwmark`。如果你希望「未绑定时设 fwmark 也能被记住、等 up 时生效」，应该怎么改？
**答案**：在 `set_fwmark` 里先 `cfg.fwmark = mark` 记账，再 `if let Some(bind) = cfg.bind.as_mut() { bind.set_fwmark(mark)... }` 立即生效。这样 `start_listener` 里的 `owner.set_fwmark(cfg.fwmark)` 就能在重建时应用记住的值。当前实现漏了记账这一步（见 4.5.3 的不对称说明）。

---

### 4.6 PeerState 与 get_peers：peer 状态快照聚合

#### 4.6.1 概念说明

`get_peers` 是设备级查询的核心：它把协议核心里散落在各处的 per-peer 运行态（字节计数、上次握手时间、端点、keepalive、allowed-ips、PSK）聚合成一个**纯数据的快照** `PeerState`，交给上层（UAPI `get` 序列化器）。

`PeerState` 的设计要点：它不含任何泛型、不含任何锁、不持有任何对协议核心的引用——它是一份「拷贝出来的照片」，调用方可以自由地序列化它而无需持锁。

#### 4.6.2 核心流程

```
get_peers():
  lock cfg
  peers = cfg.wireguard.peers.read()           ← 握手 device 的读锁
  state = Vec::with_capacity(peers.len())
  for (pk, p) in peers.iter():
      last_handshake_time = p.walltime_last_handshake → 换算成 (secs, nano)
      psk = cfg.wireguard.get_psk(&pk)
      if psk 存在:
          state.push(PeerState {
              preshared_key, endpoint, rx_bytes, tx_bytes,
              persistent_keepalive_interval, allowed_ips,
              last_handshake_time, public_key: pk
          })
  state
```

注意：`get_peers` 持有 `Inner` 的 `Mutex` 与 `peers` 的 `RwLock` 读锁**双重锁**，因此它执行期间会阻塞其他配置写操作——但只读，不阻塞数据面（数据面用的是协议核心内部锁）。

#### 4.6.3 源码精读

`PeerState` 是一个普通的结构体，所有字段都是可拷贝的值类型：

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
}
```

参见 [src/configuration/config.rs:20-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L20-L29)。`last_handshake_time` 是 `Option<(u64, u64)>`——`(秒, 纳秒)`，这正是 UAPI `get` 协议里 `last_handshake_time_sec` / `last_handshake_time_nsec` 两行的来源（u6-l4 会展开）。`preshared_key` 的注释点明：全零 `0^32` 是「未设置」的默认值，但代码里被当作普通 psk 处理。

`get_peers` 的实现：

```rust
fn get_peers(&self) -> Vec<PeerState> {
    let cfg = self.lock();
    let peers = cfg.wireguard.peers.read();
    let mut state = Vec::with_capacity(peers.len());

    for (pk, p) in peers.iter() {
        // convert the system time to (secs, nano) since epoch
        let last_handshake_time = (*p.walltime_last_handshake.lock()).map(|t| {
            let duration = t
                .duration_since(SystemTime::UNIX_EPOCH)
                .unwrap_or_else(|_| Duration::from_secs(0));
            (duration.as_secs(), duration.subsec_nanos() as u64)
        });

        if let Some(psk) = cfg.wireguard.get_psk(&pk) {
            // extract state into PeerState
            state.push(PeerState {
                preshared_key: psk,
                endpoint: p.get_endpoint(),
                rx_bytes: p.rx_bytes.load(Ordering::Relaxed),
                tx_bytes: p.tx_bytes.load(Ordering::Relaxed),
                persistent_keepalive_interval: p.get_keepalive_interval(),
                allowed_ips: p.list_allowed_ips(),
                last_handshake_time,
                public_key: pk,
            })
        }
    }
    state
}
```

参见 [src/configuration/config.rs:354-383](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L354-L383)。逐项解读数据来源：

- **`rx_bytes` / `tx_bytes`**：`p.rx_bytes.load(Ordering::Relaxed)`，从 `PeerInner` 的原子计数器读（见 [src/wireguard/peer.rs:34-35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs#L34-L35)）。`Relaxed` 排序够用，因为这只是统计值，不需要与其他内存操作建立 happens-before。
- **`walltime_last_handshake`**：墙钟时间（`SystemTime`），用于 UAPI 状态展示（区别于单调时钟 `Instant`，后者用于定时器）。换算成自 Unix epoch 起的 `(secs, nanos)`；若 `duration_since` 失败（时钟早于 epoch 的极端情况）退化为 0。
- **`endpoint`**：`p.get_endpoint()` 返回 `Option<SocketAddr>`，由路由器 peer 把内部的 `B::Endpoint` 转回平台无关地址（见 [src/wireguard/router/peer.rs:377-380](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L377-L380)），注意它「不携带 sticky socket 信息」。
- **`persistent_keepalive_interval`**：`p.get_keepalive_interval()`，经 `Deref` 链读到 `Timers` 上的值（见 [src/wireguard/timers.rs:42](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L42)）。
- **`allowed_ips`**：`p.list_allowed_ips()`，路由器 peer 的 cryptokey 路由表里该 peer 的所有前缀（见 [src/wireguard/router/peer.rs:531](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L531)）。
- **`preshared_key`**：`cfg.wireguard.get_psk(&pk)`，从握手 device 查（见 [src/wireguard/wireguard.rs:201-203](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L201-L203)）。

一个值得注意的过滤：只有 `get_psk` 返回 `Some` 的 peer 才会被加入 `state`。由于 psk 默认全零也算「有值」，正常添加的 peer 都会有 psk，因此这个过滤主要在「peer 处于半删除状态」时起保护作用。

#### 4.6.4 代码实践

**实践目标**：理解快照「无锁、无泛型」的设计收益。

**操作步骤**（源码阅读型实践）：

1. 阅读 `PeerState` 的所有字段类型（[src/configuration/config.rs:20-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L20-L29)），确认没有任何 `Arc`、`Mutex`、`RwLock`、`T::`、`B::`。
2. 阅读 UAPI `get` 序列化器（u6-l4 / `src/configuration/uapi/get.rs`）会如何消费 `&[PeerState]`：它只是遍历字段写文本，完全不需要任何锁。
3. 对比 `get_peers` 里取值时必须持锁，与 `PeerState` 返回后可以无锁使用，体会「快照」的意义。

**需要观察的现象**：`get_peers` 返回后，调用方拿到的 `Vec<PeerState>` 是一份独立拷贝，协议核心后续的收发（继续累加 `rx_bytes`）不会改变这份拷贝。

**预期结果**：你能解释「为什么 UAPI `get` 用快照而非实时读取」——避免在序列化数十个 peer、数百条 allowed-ip 的过程中长时间持锁，把锁的持有时间压缩到 `get_peers` 内部。

#### 4.6.5 小练习与答案

**练习 1**：`rx_bytes` 用 `Ordering::Relaxed` 读取，而 `walltime_last_handshake` 用了 `.lock()`。为什么统计字节可以放松内存序，而握手时间却要加锁？
**答案**：`rx_bytes`/`tx_bytes` 是 `AtomicU64`，原子读本身保证读到某个完整值，统计值不需要与其他字段保持一致性顺序，`Relaxed` 足够且最快。`walltime_last_handshake` 是 `Mutex<Option<SystemTime>>`——`SystemTime` 不是 `Copy` 且体积大，无法用单个原子量表示，必须用互斥锁保护其读改写。

**练习 2**：`last_handshake_time` 用墙钟 `SystemTime`，而定时器里用单调时钟 `Instant`（见 u7-l1）。为什么展示给用户用 `SystemTime`？
**答案**：用户看到的「上次握手时间」需要是可理解的日历时间（对应 UAPI 输出的 sec/nsec 自 epoch），单调时钟 `Instant` 没有绝对含义、不能跨进程比较。定时器内部判断「距上次握手多久」只需要相对时长，故用 `Instant`（且不受系统时间回拨影响）。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「**配置层调用链走查 + 状态推断**」任务。

**场景**：假设一个刚 `WireGuardConfig::new(wg)` 出来的设备（`port=0`、`bind=None`、`fwmark=None`），宿主通过 UAPI 依次下发如下操作（你只需在源码层面推断，不必运行）：

1. `set private_key=<某私钥>`
2. `set listen_port=51820`
3. `set fwmark=0x200`
4. `up`（MTU 来自 TUN 事件，假设 1420）
5. `set listen_port=51821`
6. `get`

**任务**：

- 对每一步，写出它调用了 `Configuration` trait 的哪个方法、修改了 `Inner` 的哪些字段、是否触发了 `start_listener`、是否 spawn 了新线程。
- 重点回答：第 2 步后设备在监听吗？第 4 步发生了什么？第 5 步「释放旧 bind + 重建」具体经过哪些代码行？第 6 步 `get_listen_port` 返回什么？
- 指出第 3 步 `set fwmark` 的一个隐患（提示：与 `cfg.fwmark` 字段的关系，见 4.5.3 / 4.5.5 练习 3）。

**参考结论**：

1. `set_private_key` → `cfg.wireguard.set_key`，重算各 peer 共享密钥；此时还没有 peer，无实际副作用。`Inner` 字段不变。
2. `set_listen_port(51820)` → `cfg.port=51820`；`bind` 仍为 `None`，`bound=false`，**不**重建。设备**未**监听。
3. `set_fwmark(Some(0x200))` → `bind` 为 `None`，走 `None => Ok(())` 分支；**注意 `cfg.fwmark` 没被写**，仍是 `None`。隐患：此后 `up` 时 `start_listener` 用 `cfg.fwmark=None`，fwmark 0x200 **丢失**。
4. `up(1420)` → `wireguard.up(1420)` 唤醒核心 + `start_listener`：`B::bind(51820)` 得 (readers, writer, owner)；`set_fwmark(None)`（因 `cfg.fwmark` 还是 None）；`set_writer`；每个 reader 起一个 udp_worker；`bind=Some(owner)`。spawn 了 2 个（双栈）udp_worker 线程。
5. `set_listen_port(51821)` → `mem::replace` 取出旧 owner（drop → shutdown 旧 socket → 旧 2 个 udp_worker 退出）；`cfg.port=51821`；`bound=true` → `start_listener`：`B::bind(51821)`、`set_writer`、起新 2 个 udp_worker、存新 owner。关键代码行 [src/configuration/config.rs:268-278](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L268-L278)。
6. `get` → `get_listen_port` 返回 `Owner::get_port() == 51821`（真实端口）；`get_peers` 返回空 `Vec`（无 peer）。

**待本地验证**：上述线程数（2 个 udp_worker）基于 Linux 双栈 socket 的假设；在 dummy 平台下 `start_listener` 会返回 `FailedToBind`（见 u2-l4），可作为验证「重建会调 bind」的反向证据。

## 6. 本讲小结

- `Configuration` trait 的核心价值是**隐藏 IO 泛型**——方法签名里只出现平台无关类型，让宿主应用不必携带 `<T, B>` 参数。
- `WireGuardConfig` 用 `Arc<Mutex<Inner>>` 包装 `WireGuard<T,B>`，`Inner` 持有协议核心与三份配置状态（`port`、`bind: Option<B::Owner>`、`fwmark`）；`bind` 字段兼任「释放旧监听器」的开关。
- `start_listener` 是 UDP 监听器的装配车间：`B::bind` → `set_fwmark` → `set_writer` → `add_udp_reader` → 存 `Owner`；进入时先把 `bind` 置 `None` 以 drop 旧 `Owner`、关闭旧 socket。
- `up`/`down` 是「唤醒/休眠核心 + 绑定/解绑」的对称对；`set_listen_port` 在已绑定时**释放旧 bind 并用新端口重建**（经 `mem::replace` 取旧、`start_listener` 建新），未绑定时仅记账。
- `set_fwmark` 与 `set_listen_port` 存在**不对称**：前者未绑定时不会记账到 `cfg.fwmark`，会导致后续 `up` 时 fwmark 丢失（源码遗留）。
- `get_peers` 把散落各处的 per-peer 运行态聚合成无锁、无泛型的 `PeerState` 快照，供 UAPI `get` 序列化使用；`last_handshake_time` 用墙钟 `(secs, nanos)` 表达。

## 7. 下一步学习建议

本讲建立了「配置层如何驱动协议核心与 UDP 监听器」的认知。建议接下来：

- **u6-l2 UAPI 协议处理框架**：看 `configuration::uapi::handle` 如何把 `wg(8)` 的文本指令解析后调用本讲的 `Configuration` trait 方法——这是 trait 的主要消费者。
- **u6-l3 / u6-l4**：分别看 `set` 配置解析器如何累积成一次 `Configuration` 调用，以及 `get` 序列化器如何消费 `PeerState` 快照。
- **回顾 u3-l1**：重新对照 `WireGuard::up/down/set_writer/add_udp_reader`，确认本讲配置层调用的协议核心入口的内部行为。
- **延伸阅读**：`src/configuration/error.rs` 的 `ConfigError::errno()` 映射（[src/configuration/error.rs:40-67](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/error.rs#L40-L67)），理解每个失败如何变成 UAPI 回写的 errno。
