# 构建与运行

## 1. 本讲目标

本讲承接 [u1-l1 项目定位](u1-l1-overview.md)：你已经知道 wireguard-rs 是「用户态纯 Rust 的 WireGuard 实现」，协议核心 `src/wireguard/` 与具体 IO 解耦。这一讲我们要把它**从源码变成一个能跑起来的进程**。

学完本讲你应当能够：

1. 读懂 `Cargo.toml` 的 `[package]`、`[dependencies]`、`[features]` 三段，并能按用途把依赖分类（密码学 / 并发 / 网络 / 平台）。
2. 掌握从 `git clone` 到 `cargo build --release` 的完整构建流程，说清 nightly 与 stable 的关系。
3. 说清 `profiler` 与 `start_up` 两个可选 feature 各自打开什么、用在哪里。
4. 理解运行 `wireguard-rs wg0` 时需要的两类权限：创建 TUN 网卡、绑定 `/var/run/wireguard` 控制套接字，以及为什么这些事必须**在降权之前**完成。

---

## 2. 前置知识

本讲假设你了解以下 Rust / Cargo 基础。不熟悉的术语会在用到时再解释一次。

- **Cargo**：Rust 的构建工具与包管理器。一个项目（crate）的元信息写在根目录的 `Cargo.toml` 里，锁定的具体版本写在 `Cargo.lock` 里。
- **`Cargo.toml` 的几个段落**：
  - `[package]`：本 crate 的「身份证」——名字、版本、作者、license。
  - `[dependencies]`：运行时依赖的第三方 crate。
  - `[dev-dependencies]`：仅在测试与 bench 时才用到的依赖。
  - `[features]`：可选功能开关，编译期决定要不要把某段代码编进去。
- **条件编译 `#[cfg(...)]`**：Rust 编译期指令，按条件（如某个 feature 是否打开、目标操作系统）选择性地编译某段代码。本讲的两个 feature 就是靠它实现的。
- **TUN 设备**：内核提供的「虚拟网卡」，进出的是裸 IP 包。wireguard-rs 通过它与内核协议栈交换**明文** IP 包，加密后从 UDP 发出。创建 TUN 需要 root。
- **UAPI**：WireGuard 的文本控制协议，`wg(8)` 命令通过它与实现通信。在 wireguard-rs 里它是一颗 Unix domain socket，默认在 `/var/run/wireguard/` 下。

---

## 3. 本讲源码地图

本讲主要看「项目门口」的几个文件，全部是配置与入口：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [Cargo.toml](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml) | crate 元信息、依赖、feature 开关 | `[package]` / `[dependencies]` / `[features]` 三段 |
| [README.md](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md) | 用法、平台、构建说明 | Usage 段与 Building 段 |
| [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) | 二进制入口 | `profiler` feature 的两段 `#[cfg]` 代码、降权/守护进程化时机 |
| [src/platform/linux/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs) | Linux TUN 实现 | `start_up` feature 注入的假 `Up` 事件 |
| [src/platform/linux/uapi.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/uapi.rs) | Linux UAPI 套接字 | 控制套接字目录常量 |

> 说明：`main.rs`、`tun.rs`、`uapi.rs` 在本讲只是「被 feature 和权限流程引用」，它们的完整逐行讲解分别放在 [u1-l4 程序入口](u1-l4-main-entry.md)、[u2-l2 Linux TUN](u2-l2-linux-tun.md)、[u6-l2 UAPI 协议](u6-l2-uapi-protocol.md)。本讲只挑与「构建、运行、权限」直接相关的片段。

---

## 4. 核心概念与源码讲解

### 4.1 `[package]` 段：项目的身份卡

#### 4.1.1 概念说明

`[package]` 段告诉 Cargo「这个 crate 叫什么、版本号多少、谁写的、按什么协议开源」。它不涉及任何编译行为，但决定了：

- **产物名**：因为本 crate 含 `src/main.rs`，它是一个**二进制 crate**，编译产物的名字就是 package 名 `wireguard-rs`——这正是 README 里 `wireguard-rs wg0` 那条命令的可执行文件名。
- **版本号**：`0.1.4`，本讲后面如果要写「打印版本号」的练习会用到它。

#### 4.1.2 核心流程

`[package]` → 生成可执行文件 `wireguard-rs`（release 模式下在 `target/release/wireguard-rs`）→ 用户以 `wireguard-rs <接口名>` 启动。

#### 4.1.3 源码精读

[Cargo.toml:L1-L6](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L1-L6) 定义了 crate 身份：

```toml
[package]
authors = ["Mathias Hall-Andersen <mathias@hall-andersen.dk>"]
edition = "2018"
license = "MIT"
name = "wireguard-rs"
version = "0.1.4"
```

- `edition = "2018"`：使用 Rust 2018 edition（与 Rust 语言的「版本」是两回事，仅影响一些语法与默认行为）。
- `license = "MIT"`：对应最近一次提交「Added MIT license」（见 `git log`）。

#### 4.1.4 代码实践（阅读型）

打开 `Cargo.toml`，把光标移到 `version = "0.1.4"` 这一行，记住这个值。后续在 [4.5](#45-运行流程与所需权限) 与综合实践里我们会让程序把这个版本号打印出来。这一步不需要运行命令，只需确认「二进制名 = `name` 字段」。

#### 4.1.5 小练习与答案

**练习**：如果把 `name` 改成 `mywg`，README 里的启动命令要怎么改？

**答案**：产物会变成 `target/release/mywg`，启动命令相应改为 `mywg wg0`。注意这只是练习设想，**不要真的修改源码**（本讲禁止改源码）。

---

### 4.2 `[dependencies]` 段：依赖清单与功能分组

#### 4.2.1 概念说明

wireguard-rs 不是从零造所有轮子。它把「经过审计的密码学原语」和「成熟的并发原语」交给第三方 crate，自己专注协议逻辑。理解依赖清单，等于读懂了项目「依赖什么能力」。

依赖大致分四类：

1. **密码学**：握手与数据面加解密。
2. **并发与同步**：多工作线程、锁、并发容器、定时器。
3. **网络与零拷贝解析**：路由表、报文视图。
4. **平台绑定与工具**：libc、日志、hex、随机数。

#### 4.2.2 核心流程

`Cargo.toml` 声明依赖 → `cargo build` 解析并下载 → 编入产物。`^` 前缀（如 `"^0.7"`）表示「兼容 0.7.x 的任意新版」（semver 兼容）。

#### 4.2.3 源码精读

主依赖块在 [Cargo.toml:L8-L31](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L8-L31)。几个要点：

- **密码学**（对应 Noise IK 握手与 ChaCha20-Poly1305 数据面）：
  - `ring = "0.16"`：Google 的密码学库绑定，**数据面**（router）的 ChaCha20-Poly1305 就用它。
  - `chacha20poly1305 = "^0.7"`：纯 Rust 的 AEAD 实现。
  - [Cargo.toml:L40-L41](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L40-L41) 的 `x25519-dalek = "^1.1"`：Curve25519 椭圆曲线 DH，握手的密钥交换核心。
  - `blake2 = "^0.9"`：BLAKE2s 哈希，Noise 协议的转录哈希。
  - `hmac`/`digest`/`aead`/`generic-array`：配套 trait 与定长数组。
  - [Cargo.toml:L43-L45](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L43-L45) 的 `subtle = "^2.4"`：常时间比较（防侧信道），其下注释掉的 `#features = ["nightly"]` 暗示了「可选 nightly」的历史。

- **并发与同步**：
  - `crossbeam-channel = "^0.5"`：跨线程通道（如握手任务队列）。
  - `parking_lot = "^0.11"`：高性能 `RwLock`/`Mutex`。
  - `dashmap = "^4.0"`：并发哈希表。这正是最近一次提交「Replace RwLock<HashMap> with DashMap in handshake」引入的（见 `git log`）。
  - `spin = "0.7"`：自旋锁。
  - `hjul = "0.2.2"`：时间轮定时器（驱动密钥过期、重握手）。
  - `num_cpus = "^1.10"`：探测 CPU 核数以决定工作线程数量。
  - `arraydeque = "0.4.5"`：定长双端队列（暂存报文、防回放等）。

- **网络与零拷贝**：
  - [Cargo.toml:L33-L35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L33-L35) 的 `treebitmap`（实际包名 `ip_network_table-deps-treebitmap`）：最长前缀匹配，cryptokey 路由表用它。
  - `zerocopy = "0.3"`：把字节切片安全「视图化」为结构体，避免拷贝。
  - `byteorder = "1.3"`：网络字节序读写。

- **平台绑定与工具**：
  - [Cargo.toml:L37-L38](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L37-L38) 的 `libc = "^0.2"`，带 `[target.'cfg(unix)'.dependencies]`，**只在 Unix 平台**编译——TUN、ioctl、netlink 都靠它。
  - `log` + `env_logger`：日志门面与实现。
  - `hex`：UAPI 里密钥的十六进制编解码。
  - `clear_on_drop = "0.2.3"`：密钥材料离开作用域时清零（安全相关，详见 [u7-l2](u7-l2-key-material-security.md)）。
  - `rand`/`rand_core`：随机数。

- **可选依赖**：[Cargo.toml:L15](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L15) 的 `cpuprofiler = {version = "*", optional = true}`——只在 `profiler` feature 打开时才拉取（见 4.3）。

- **测试依赖**：[Cargo.toml:L51-L54](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L51-L54) 的 `pnet`（构造测试 IP 报文）、`proptest`（属性测试）、`rand_chacha`（确定性 RNG），只在 `cargo test` 时生效。

#### 4.2.4 代码实践（运行型，本讲主实践）

1. **目标**：亲手编译一次，并把依赖按用途归类。
2. **步骤**：
   - 在项目根目录执行 `cargo build --release`。
   - 编译成功后，打开 `Cargo.toml` 的 `[dependencies]` 段，按下方「参考分类表」逐条核对每个 crate 属于哪一类。
3. **观察**：首次编译会下载大量依赖；命令最终应输出 `Finished release [optimized] target(s)`。产物位于 `target/release/wireguard-rs`。
4. **预期结果**：下表是参考分类（可直接对照源码核对，不依赖运行结果）：

   | 类别 | crate |
   | --- | --- |
   | 密码学 | `ring`、`chacha20poly1305`、`x25519-dalek`、`blake2`、`hmac`、`digest`、`aead`、`subtle`、`generic-array` |
   | 并发/同步 | `crossbeam-channel`、`parking_lot`、`dashmap`、`spin`、`hjul`、`num_cpus`、`arraydeque` |
   | 网络/零拷贝 | `treebitmap`、`zerocopy`、`byteorder` |
   | 平台/工具 | `libc`(unix)、`log`、`env_logger`、`hex`、`clear_on_drop`、`rand`、`rand_core` |
   | 仅测试 | `pnet`、`proptest`、`rand_chacha` |
   | 可选 | `cpuprofiler`（`profiler` feature） |

5. 编译报错或产物路径不同 → 「待本地验证」具体环境差异（如缺少 OpenSSL 等系统库；`ring` 一般不需要，但不同发行版可能要求安装编译工具链）。

> ⚠️ 不要假装已经运行过命令。如果你还没在本机执行，就把这一步当作「阅读 + 对照表」来完成，运行验证留到机器就绪时。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `libc` 要写在 `[target.'cfg(unix)'.dependencies]` 里，而不是普通 `[dependencies]`？

**答案**：wireguard-rs 的平台相关代码（创建 TUN、ioctl、netlink）只在 Unix 上有意义。用 target-specific 依赖可以让 crate 在非 Unix 平台编译时不去拉 `libc`，避免无谓的依赖。

**练习 2**：最近一次提交把握手模块的 `RwLock<HashMap>` 换成了 `dashmap`。从「多工作线程并发查 peer」的场景，说一个 DashMap 的好处。

**答案**：DashMap 内部分片（多个独立的锁），多个线程可以同时读/写不同的 key 而不争用同一把全局锁；而 `RwLock<HashMap>` 在写时整张表独占。握手模块会被多个 handshake 工作线程并发访问，分片能显著降低锁竞争。

---

### 4.3 `[features]` 段：profiler 与 start_up 两个可选开关

#### 4.3.1 概念说明

`[features]` 定义**编译期开关**。一个 feature 可以：（a）拉起若干可选依赖，（b）配合 `#[cfg(feature = "...")]` 让某些代码只在开关打开时才编译进去。默认**关闭**，用 `--features` 打开。

wireguard-rs 只有两个 feature：`profiler` 和 `start_up`，都很轻量。

#### 4.3.2 核心流程

[Cargo.toml:L47-L49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L47-L49)：

```toml
[features]
profiler = ["cpuprofiler"]
start_up = []
```

- `profiler = ["cpuprofiler"]`：打开 `profiler` 时，自动启用可选依赖 `cpuprofiler`，并让 `main.rs`/bench 里 `#[cfg(feature = "profiler")]` 的代码参与编译。
- `start_up = []`：不拉任何依赖，只是个纯开关，被 `src/platform/linux/tun.rs` 里的一行 `#[cfg]` 用到。

#### 4.3.3 源码精读

**profiler —— CPU 性能剖析**

入口处在 [src/main.rs:L5-L9](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L5-L9)：仅当 feature 打开才 `extern crate cpuprofiler` 并 `use` 它。`profiler_start` 在 [src/main.rs:L38-L53](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L38-L53) 寻找一个不存在的文件名（如 `wg0-0.profile`）开始采样；`profiler_stop` 停止并落盘。注意 [src/main.rs:L35-L36](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L35-L36) 提供了一个**空实现**版本，这样关闭 feature 时调用 `profiler_stop()` 仍能编译，零运行时开销。真正在 `main()` 里启动它的是 [src/main.rs:L126-L128](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L126-L128)。bench 里也有同样开关（`src/wireguard/router/tests/bench.rs`）。

**start_up —— 启动即「拉起」网卡**

在 [src/platform/linux/tun.rs:L300-L303](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs#L300-L303)，`LinuxTunStatus` 初始化事件队列时，只在 `start_up` 打开时往队首塞一个假的 `TunEvent::Up(1500)`：

```rust
events: vec![
    #[cfg(feature = "start_up")]
    TunEvent::Up(1500),
],
```

效果：Tun 事件线程第一次 `status.event()` 就会立刻收到 `Up(1500)`，于是 `main.rs` 里 [src/main.rs:L152-L155](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L152-L155) 调用 `cfg.up(1500)`，**无需用户再 `ip link set wg0 up`**。MTU 默认 1500。这个 feature 主要用于不方便手动 up 接口的测试/容器环境。

#### 4.3.4 代码实践（阅读 + 命令型）

1. **目标**：体会「feature 改变编译产物」。
2. **步骤**：
   - 先 `cargo build --release`（默认两 feature 都关）。
   - 再 `cargo build --release --features profiler`，观察编译日志里出现了 `cpuprofiler` 的编译（而第一次没有）。
3. **观察**：开启 `profiler` 后，构建脚本会额外编译 `cpuprofiler`；开启 `start_up` 不会拉新依赖，但产物里的那行 `#[cfg]` 代码会被编入。
4. **预期结果**：两次都应 `Finished`。`--features` 接受逗号分隔同时打开多个，如 `--features "profiler start_up"`。
5. 具体日志细节「待本地验证」。

#### 4.3.5 小练习与答案

**练习**：如果不传 `--features profiler`，`main.rs` 里调用 `profiler_start(name)` 的那行 [src/main.rs:L127-L128](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L127-L128) 会怎样？

**答案**：那两行本身也带 `#[cfg(feature = "profiler")]`，所以 feature 关闭时它们**不会出现在最终二进制里**——不是「调用了空函数」，而是「代码根本没编进去」。这是 Cargo feature 零开销的关键。

---

### 4.4 构建流程：从克隆到 `cargo build --release`

#### 4.4.1 概念说明

README 的 Building 段给出了官方构建步骤。注意它说项目「以当前 nightly 为目标，但应该也能用 stable 构建」。

#### 4.4.2 核心流程

[README.md:L35-L43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md#L35-L43) 的三步：

1. 通过 [rustup](https://rustup.rs/) 获取 nightly 的 `cargo`/`rustc`。
2. `git clone` 仓库。
3. 在仓库目录内执行 `cargo build --release`。

#### 4.4.3 源码精读

关于 nightly / stable，有两个**客观**线索，不要夸大 nightly 依赖：

- `src/main.rs:1` 写着 `#![cfg_attr(feature = "unstable", feature(test))]`——只有打开一个名为 `unstable` 的 feature 时才启用 nightly 专用的 `test` feature；但 [Cargo.toml](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml) 的 `[features]` 里**并没有定义 `unstable`**，所以这是一段「留好但默认不激活」的代码。
- `subtle` 下的 `#features = ["nightly"]` 被注释掉了。

结论：**默认构建路径不需要 nightly**，stable 应当可用（与 README 的「should also build with stable」一致）。`--release` 启用优化，发布部署用它。

#### 4.4.4 代码实践（命令型）

1. **目标**：跑通一次完整构建并确认产物。
2. **步骤**：
   ```
   rustup toolchain install stable      # 或 nightly
   cd <仓库根目录>
   cargo build --release
   ls -l target/release/wireguard-rs
   ```
3. **观察**：产物 `target/release/wireguard-rs` 存在且有可执行权限。
4. **预期结果**：`Finished release [optimized] target(s)`。
5. 不同发行版可能需要 `build-essential`/`pkg-config` 等系统包；缺包时的具体报错「待本地验证」。

#### 4.4.5 小练习与答案

**练习**：`cargo build` 与 `cargo build --release` 的产物分别在哪个目录？为什么部署要用后者？

**答案**：分别在 `target/debug/` 与 `target/release/`。debug 产物未优化、含运行时检查，体积大、速度慢；release 产物经过优化，适合部署。WireGuard 是高吞吐的数据面，必须用 release。

---

### 4.5 运行流程与所需权限

#### 4.5.1 概念说明

构建出 `wireguard-rs` 后，运行它需要**两类特权操作**，而这两件事都必须在程序**主动降权之前**完成（降权由 [u1-l5](u1-l5-privileges-daemon.md) 详讲）：

1. 绑定 UAPI 控制套接字（写 `/var/run/wireguard/`）。
2. 创建 TUN 网卡（`/dev/net/tun` + ioctl，需要 `CAP_NET_ADMIN`/root）。

之后程序会降权到 `nobody` 并 `chroot` 到 `/tmp`，再 daemonize 到后台，最后启动工作线程。

#### 4.5.2 核心流程（main 关键顺序）

读 [src/main.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs) 的 `main()`，顺序为：

1. 解析参数（接口名、`--foreground`、`--disable-drop-privileges`）。
2. **绑 UAPI**（需要写 `/var/run/wireguard`）。
3. **建 TUN**（需要 root）。
4. **降权**（`util::drop_privileges`）。
5. **daemonize**（除非 `--foreground`）。
6. 初始化日志、（可选）profiler。
7. 建 `WireGuard` 设备，加 Tun reader、起 Tun 事件线程、起 UAPI 服务线程。
8. `wg.wait()` 阻塞，直到所有 Tun reader 关闭。

#### 4.5.3 源码精读

UAPI 套接字目录在 [src/platform/linux/uapi.rs:L7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/uapi.rs#L7)：

```rust
const SOCK_DIR: &str = "/var/run/wireguard/";
```

接口名 `wg0` 会拼成 `/var/run/wireguard/wg0.sock`，这就是 `wg(8)` 与实现对话的控制端点。README 的 [Usage 段](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md#L3-L14) 也提到：删掉这个 socket 会让 wireguard-rs 关闭（作为无法 `ip link del` 时的退路）。

两件特权操作在 `main()` 里紧挨着出现在降权之前：绑 UAPI 在 [src/main.rs:L85-L89](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L85-L89)，建 TUN 在 [src/main.rs:L91-L95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L91-L95)，紧接着 [src/main.rs:L97-L106](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L97-L106) 调用 `util::drop_privileges()`。如果颠倒顺序——先降权再建 TUN——普通用户 `nobody` 没有 `CAP_NET_ADMIN`，建 TUN 会失败。这就是「先做特权事，再丢特权」的经典特权降级模式。

> 关于「在 Linux 上别用它」：README 的 [Platforms/Linux 段](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/README.md#L16-L21) 明确写「YOU SHOULD NOT RUN THIS ON LINUX. Instead use the kernel module」。wireguard-rs 的价值在无内核模块的平台与作为可嵌入协议库——见 [u1-l1](u1-l1-overview.md)。

#### 4.5.4 代码实践（运行型）

1. **目标**：以 `--foreground` 前台运行，观察日志，避免它 daemonize 进后台难以管理。
2. **步骤**（需 root）：
   ```
   # 编译
   cargo build --release
   # 前台运行，并禁用降权以便观察（仅调试环境！）
   sudo ./target/release/wireguard-rs wg0 --foreground
   # 另开终端，用 wg(8) 配置（需要已安装 wireguard-tools）
   sudo wg set wg0 private-key /path/to/privatekey listen-port 51820
   sudo ip address add 10.0.0.1/24 dev wg0
   sudo ip link set wg0 up
   ```
3. **观察**：前台终端应打印 `Starting wg0 WireGuard device.` 与 `Tun up (mtu = ...)`；`wg show wg0` 能看到接口。
4. **预期结果**：接口创建成功、`wg show` 可见。
5. 若 `wg(8)` 未安装、或当前内核已有 WireGuard 模块导致行为差异 → 「待本地验证」。**生产 Linux 请改用内核模块**，本步骤仅供学习。

> 提示：`--disable-drop-privileges` 只在你想用调试器附加进程时才用；正常运行应保留降权。

#### 4.5.5 小练习与答案

**练习**：为什么 README 说「删掉 `/var/run/wireguard/wg0.sock` 会让 wireguard-rs 关闭」？结合 `main()` 里的 UAPI 服务线程（[src/main.rs:L164-L180](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L164-L180)）说明。

**答案**：UAPI 服务线程在 `loop` 里 `uapi.connect()` 接受连接；当 socket 文件被删，后续 `connect()` 调用会持续报错，进入 `Err` 分支，那里会 `profiler_stop()` 然后 `exit(-1)`（见该段代码）。因此删 socket 等于强制结束进程，是「无法 `ip link del`」时的退路。

---

## 5. 综合实践

把本讲的「依赖分类」「feature」「权限顺序」三件事串起来，完成一个小任务：

**任务：生成一份「构建档案」**

1. 运行 `cargo build --release`，把终端里的 `Compiling ...` 行收集起来。
2. 对照 [4.2.4](#424-代码实践运行型本讲主实践) 的分类表，把编译日志里出现的每个依赖 crate 归入「密码学 / 并发 / 网络 / 平台工具 / 仅测试」五类，写成一张表。注意哪些是**间接依赖**（被你的直接依赖拉进来的，不出现在 `Cargo.toml` 里）。
3. 分别用默认、`--features profiler`、`--features "profiler start_up"` 三种方式各编译一次，记录：哪几次编译了 `cpuprofiler`？为什么 `start_up` 不会增加任何依赖编译？
4. 用 `--foreground` 启动 `wg0`，验证 [4.5](#45-运行流程与所需权限) 里「先 UAPI/TUN、后降权、最后 daemonize」的顺序：故意用非 root 运行，观察它在哪一步失败（预期：绑 UAPI 或建 TUN 时报权限错并 `exit`，给出对应负数退出码 `-2`/`-3`）。

**交付物**：一张依赖分类表 + 一段对三种 feature 组合编译差异的说明 + 一段对「非 root 运行在哪一步失败、退出码含义」的解释（退出码取值见 `main.rs` 各 `exit(-N)`）。

> 退出码速查（来自 `main.rs`）：`-1` 未给设备名；`-2` UAPI 绑定失败；`-3` TUN 创建失败；`-4` 降权失败；`-5` daemonize 失败。

---

## 6. 本讲小结

- `Cargo.toml` 的 `[package]` 决定产物名 `wireguard-rs` 与版本 `0.1.4`；`[dependencies]` 体现项目「密码学/并发/网络/平台」四大能力来源。
- 密码学依赖以 `ring`、`chacha20poly1305`、`x25519-dalek`、`blake2` 为代表；并发依赖以 `crossbeam-channel`、`parking_lot`、`dashmap`、`spin` 为代表。
- 两个 feature：`profiler`（拉起 `cpuprofiler`，CPU 剖析）与 `start_up`（注入假 `TunEvent::Up(1500)`，启动即拉起网卡），都靠 `#[cfg(feature = ...)]` 实现零默认开销。
- 构建用 `cargo build --release`；默认路径可用 stable，nightly 为「目标但非必需」。
- 运行 `wireguard-rs wg0` 需 root 完成「绑 UAPI（`/var/run/wireguard/`）+ 建 TUN」，**之后**才降权到 `nobody` 并 daemonize——顺序不能颠倒。
- 生产 Linux 应改用内核模块；wireguard-rs 适用于无内核模块的平台与作为协议库。

---

## 7. 下一步学习建议

- 想看清 `main()` 里建 UAPI、建 TUN、降权、daemonize 的逐行实现 → 下一讲 [u1-l3 目录结构与模块地图](u1-l3-directory-map.md)、[u1-l4 程序入口 main.rs 与运行生命周期](u1-l4-main-entry.md)。
- 降权与 daemonize 的双 fork/setsid/chroot 细节 → [u1-l5 特权降级与守护进程化](u1-l5-privileges-daemon.md)。
- 想理解 `start_up` 注入的 `TunEvent::Up` 如何被消费、`cfg.up(mtu)` 做了什么 → [u3-l1 WireGuard 胶水层](u3-l1-wireguard-glue.md)。
- 想理解 `cfg.up/down` 背后的配置接口 → [u6-l1 Configuration 抽象](u6-l1-config-interface.md)。

建议同时保存好本讲产出的「构建档案」，后续讲义会反复引用其中的依赖分类与 feature 语义。
