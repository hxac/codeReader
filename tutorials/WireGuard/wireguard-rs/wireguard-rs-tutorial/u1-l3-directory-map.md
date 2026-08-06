# 目录结构与模块地图

## 1. 本讲目标

本讲带你建立 `src/` 目录到功能模块的映射。读完本讲后，你应该能够：

1. 说出 `src/` 下每个目录（`configuration/`、`platform/`、`wireguard/`）的职责。
2. 区分「平台相关代码」（`platform/linux`、`platform/dummy`）与「纯协议代码」（`wireguard/`）。
3. 识别三个顶层模块之间的依赖方向，并理解为什么协议核心不依赖具体操作系统。
4. 看懂 `main.rs` 顶部 `mod` 声明，以及 `plt`（平台别名）是怎么被引入的。

本讲承接 u1-l1 的「三大顶层模块」与 u1-l2 的构建方式，为后续逐层深入（u1-l4 入口生命周期、第 2 单元平台抽象、第 4/5 单元握手与路由器）画出一张可以随时回查的地图。

## 2. 前置知识

- **Rust 模块系统基础**：`mod foo;` 声明一个子模块（编译器会去找 `foo.rs` 或 `foo/mod.rs`）；`pub mod` 表示对外可见，`mod`（不带 `pub`）表示模块私有；`use` 把路径里的名字引入当前作用域；`pub use` 再导出（re-export）。
- **条件编译**：`#[cfg(target_os = "linux")]` 表示只在 Linux 编译；`#[cfg(test)]` 表示只在测试构建里编译。这套机制在本项目里大量用于「同一份协议核心，对接不同平台」。
- **模块即「目录」的直觉**：在 Rust 里，一个文件夹通常对应一个功能域。本项目的 `src/` 正是把 WireGuard 的功能按域拆成了三个顶层目录。

> 如果你对上述概念还陌生，可以先把本讲当成「目录 + 一句话说明」的速查表，等后续讲义逐个用到时再回头细看。

## 3. 本讲源码地图

本讲只看「模块声明」层面，即每个 `mod.rs` / `main.rs` 里声明了哪些子模块。下面这些文件是本讲的精读对象：

| 文件 | 作用 |
| --- | --- |
| `src/main.rs` | 程序入口，声明三个顶层模块 + `util`，并解析命令行、绑定 UAPI、创建 TUN、起线程。 |
| `src/platform/mod.rs` | 平台抽象层入口：导出 TUN/UDP/UAPI/Endpoint 的 trait，并按 `cfg` 引入 `linux`（生产）或 `dummy`（测试）实现，再用 `plt` 别名统一对外。 |
| `src/configuration/mod.rs` | 配置接口层入口：导出 `Configuration` trait、`WireGuardConfig` 包装器、`ConfigError`，以及 `uapi` 子模块。 |
| `src/wireguard/mod.rs` | 协议核心层入口：声明 handshake/router 等子模块，导出 `WireGuard`，并说明本层是「不依赖具体 IO 的纯协议实现」。 |

此外会顺带提到两个「实现入口」：`src/platform/linux/mod.rs`（生产实现）与 `src/platform/dummy/mod.rs`（测试实现），它们是理解「平台可替换」的关键。

## 4. 核心概念与源码讲解

### 4.1 顶层入口与 mod 声明

#### 4.1.1 概念说明

Rust 程序的入口是 `main.rs`。一个 crate 的「顶层模块」就是 `main.rs`（二进制）或 `lib.rs`（库）里用 `mod` 声明的那些名字。wireguard-rs 是一个二进制 crate，它的顶层模块声明决定了整个 `src/` 目录的骨架。

理解顶层声明的价值在于：**只要看 `main.rs` 头部十多行，就能知道这个项目分成几大块、谁是谁的入口**，而不必一开始就陷入细节。

#### 4.1.2 核心流程

`main.rs` 顶部的声明流程是：

1. 声明条件依赖（仅在 `profiler` feature 开启时引入 `cpuprofiler`）。
2. 用三个 `mod` 声明三大顶层模块：`configuration`、`platform`、`wireguard`。
3. 再声明一个工具模块 `util`（守护进程化与降权）。
4. 用 `use` 把三个模块里的关键类型引入 `main` 函数作用域，其中 `use platform::*;` 会把 `plt` 这个平台别名一并引入。

随后 `main()` 函数才开始解析命令行参数、绑定 UAPI、创建 TUN、降权、起线程。

#### 4.1.3 源码精读

模块声明部分（注意这里没有 `pub`，因为二进制 crate 的顶层模块不需要对外暴露）：

[src/main.rs:11-15](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L11-L15) — 三个顶层模块 + util 的声明。这一段就是 `src/` 的「目录骨架」。

[src/main.rs:21-27](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L21-L27) — 把三个模块里的关键名字引入作用域。重点看最后一行 `use platform::*;`。

`use platform::*;` 之所以重要，是因为 `platform/mod.rs` 里有一行 `pub use linux as plt;`（仅 Linux 目标）。于是 `main` 函数里写 `plt::UAPI`、`plt::Tun`、`plt::UDP` 时，`plt` 实际指向 Linux 的具体实现。例如：

[src/main.rs:86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L86) — `plt::UAPI::bind(...)` 创建 UAPI 监听套接字（实际是 `LinuxUAPI`）。

[src/main.rs:92](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L92) — `plt::Tun::create(...)` 创建 TUN 设备（实际是 `LinuxTun`）。

[src/main.rs:131](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L131) — `WireGuard<plt::Tun, plt::UDP>`，把 Linux 的 TUN 与 UDP 类型作为泛型参数喂给协议核心。

> **直觉**：`main.rs` 自己不写任何「Linux 特定」的代码，它只通过 `plt` 这个别名接触平台。换一个平台（例如将来的 macOS），只要让 `platform/mac/mod.rs` 也被别名为 `plt`，`main.rs` 几乎不用改。这就是「平台可替换」的入口设计。

#### 4.1.4 代码实践

**实践目标**：搞清楚 `plt` 这个别名是怎么从 `platform::*` 流进 `main` 函数的。

**操作步骤**：

1. 打开 `src/main.rs`，找到第 25 行 `use platform::*;`。
2. 打开 `src/platform/mod.rs`，找到 `pub use linux as plt;`（第 16 行）。
3. 打开 `src/platform/linux/mod.rs`，找到 `pub use tun::LinuxTun as Tun;`、`pub use uapi::LinuxUAPI as UAPI;`、`pub use udp::LinuxUDP as UDP;`（第 5-7 行）。

**需要观察的现象**：沿着三步跳转，你能把 `main.rs` 里的 `plt::Tun::create` 一路还原成 `LinuxTun::create`。

**预期结果**：在纸上（或注释里）写出一行等价替换：`plt::Tun` → `LinuxTun`，`plt::UAPI` → `LinuxUAPI`，`plt::UDP` → `LinuxUDP`。

#### 4.1.5 小练习与答案

**练习 1**：`main.rs` 顶部声明了几个不带 `pub` 的 `mod`？分别对应 `src/` 下哪些路径？

**参考答案**：4 个——`configuration`（→`src/configuration/mod.rs`）、`platform`（→`src/platform/mod.rs`）、`wireguard`（→`src/wireguard/mod.rs`）、`util`（→`src/util.rs`）。

**练习 2**：如果将来要支持 macOS，按现有设计，`platform/mod.rs` 里应该新增哪一行？

**参考答案**：新增形如 `#[cfg(target_os = "macos")] pub use mac as plt;` 的别名（对应一个新建的 `platform/mac/` 实现），并保留 Linux 分支互斥。`main.rs` 因只依赖 `plt` 而基本无需改动。

---

### 4.2 platform/ 平台抽象层

#### 4.2.1 概念说明

`platform/` 是「与操作系统打交道」的那一层。它做两件事：

1. **定义 trait（抽象）**：用一组 trait 描述「WireGuard 需要平台提供哪些能力」——读/写 TUN 网卡、收/发 UDP 报文、监听 UAPI 控制连接、表示一个对端网络端点。这些 trait 里没有任何操作系统调用。
2. **提供具体实现**：针对每个平台给出 trait 的实现。目前有 `linux/`（生产用）和 `dummy/`（测试用，纯软件、无副作用）。

这样设计的好处是：**协议核心 `wireguard/` 只依赖 trait，不依赖任何具体平台**。同一份协议代码，配 `linux` 跑生产，配 `dummy` 跑单元测试。

#### 4.2.2 核心流程

`platform/mod.rs` 的组织流程：

1. 声明三个对外可见的 trait 模块：`pub mod tun;`、`pub mod uapi;`、`pub mod udp;`。
2. 私有声明 `mod endpoint;` 并用 `pub use endpoint::Endpoint;` 只导出类型名。
3. 按目标平台条件编译具体实现：Linux 编进 `linux`，测试编进 `dummy`。
4. 用 `pub use linux as plt;` 把当前平台实现统一别名为 `plt`，供 `main.rs` 使用。

trait 与实现的对应关系如下表：

| 平台能力 | trait 文件 | 主要 trait | Linux 实现 | dummy 实现 |
| --- | --- | --- | --- | --- |
| TUN 网卡 | `tun.rs` | `Tun`、`PlatformTun`、`Writer`、`Reader`、`Status` | `linux/tun.rs` (`LinuxTun`) | `dummy/tun/` (`TunTest`) |
| UDP 套接字 | `udp.rs` | `UDP`、`PlatformUDP`、`Owner` | `linux/udp.rs` (`LinuxUDP`) | `dummy/udp.rs` (`PairBind`) |
| UAPI 控制 | `uapi.rs` | `PlatformUAPI`、`BindUAPI` | `linux/uapi.rs` (`LinuxUAPI`) | （测试中由配置层模拟） |
| 对端端点 | `endpoint.rs` | `Endpoint` | `LinuxEndpoint`（在 `linux/udp.rs` 内） | `UnitEndpoint` |

#### 4.2.3 源码精读

[src/platform/mod.rs:1-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/mod.rs#L1-L16) — 整个平台层的入口文件，全文仅 16 行，但已经把「trait + 两套实现 + 别名」讲清楚。

逐行解读：

- 第 1-4 行：`mod endpoint;`（私有）与三个 `pub mod`（trait 定义模块）。
- 第 7 行：`pub use endpoint::Endpoint;` 只把 `Endpoint` 类型名对外导出，隐藏模块内部。
- 第 9-10 行：`#[cfg(target_os = "linux")] pub mod linux;` 仅在 Linux 编译生产实现。
- 第 12-13 行：`#[cfg(test)] pub mod dummy;` 仅在测试构建编译 dummy 实现。
- 第 15-16 行：`#[cfg(target_os = "linux")] pub use linux as plt;` 生成 `plt` 别名。

[src/platform/tun.rs:51-63](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L51-L63) — `Tun` 与 `PlatformTun` trait，用关联类型把「读端、写端、状态端」绑成一个整体，`create` 返回三元组 `(Vec<Reader>, Writer, Status)`。这是 u1-l1 提到的「TUN 接口」在代码里的契约。

[src/platform/linux/mod.rs:1-7](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/mod.rs#L1-L7) — Linux 实现：把三个具体类型重命名为统一的 `Tun`/`UAPI`/`UDP`，使它们能被 `as plt` 别名后与 `main.rs` 对接。

[src/platform/dummy/mod.rs:1-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/mod.rs#L1-L13) — dummy 实现：注释明说它的用途是「让完整的 WireGuard、配置接口、UAPI 解析器都能在单元测试里端到端跑起来」。

> **直觉**：把 `platform/` 想成一排「插座（trait）」和两块「插头（linux/dummy）」。协议核心只认插座形状，插哪块插头都能通电。

#### 4.2.4 代码实践

**实践目标**：把 `platform/` 下的文件分成「trait 定义」与「具体实现」两类，并理解 `cfg` 门控。

**操作步骤**：

1. 列出 `src/platform/` 直接子项：`mod.rs`、`endpoint.rs`、`tun.rs`、`udp.rs`、`uapi.rs`、`linux/`、`dummy/`。
2. 判断每个属于哪一类：
   - trait 定义：`endpoint.rs`、`tun.rs`、`udp.rs`、`uapi.rs`。
   - 具体实现：`linux/`（生产）、`dummy/`（测试）。
3. 在 `platform/mod.rs` 里数一下 `#[cfg(...)]` 出现了几次，分别对应哪个实现。

**需要观察的现象**：`linux` 与 `dummy` 不会同时被编译进同一个产物——`linux` 要 `target_os = "linux"`，`dummy` 要 `cfg(test)`。

**预期结果**：在笔记里写出「trait 文件 4 个 + linux 3 个文件 + dummy 4 个文件（含 tun 子目录）」的分类清单。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mod endpoint;` 不带 `pub`，却还能让外部用到 `Endpoint`？

**参考答案**：因为第 7 行用了 `pub use endpoint::Endpoint;` 进行再导出。模块本身私有，但类型名对外可见，这是一种常见的「隐藏实现模块、只暴露类型」手法。

**练习 2**：`dummy` 平台在 `cargo build --release`（非测试）时会被编译吗？为什么？

**参考答案**：不会。它被 `#[cfg(test)]` 门控，只有在 `cargo test` 等测试构建里才会编入。生产二进制不含 dummy 代码。

**练习 3**：`plt` 这个别名在「测试构建 + Linux 目标」下会同时指向 `linux` 和 `dummy` 吗？

**参考答案**：不会。`pub use linux as plt;` 只在 `target_os = "linux"` 下定义；`dummy` 没有别名到 `plt`。测试中通常直接用完整路径 `platform::dummy::...` 来引用 dummy 类型，而不是通过 `plt`。

---

### 4.3 configuration/ 配置接口层

#### 4.3.1 概念说明

`configuration/` 是「最上层」。它面向宿主应用和 `wg(8)` 这类外部工具，提供两样东西：

1. **一个简单的配置接口**（`Configuration` trait + `WireGuardConfig` 包装器）：让上层不必关心协议核心那一堆复杂泛型，就能 up/down 接口、增删 peer、设私钥、设监听端口。
2. **UAPI 文本协议处理**（`uapi/` 子模块）：解析 `wg(8)` 发来的 `get=`/`set=` 文本命令，翻译成对 `Configuration` 的调用。

关键设计：`WireGuardConfig` 的注释明确说，它存在的目的之一就是**把协议核心的 IO 泛型（`T: Tun, B: UDP`）对配置/UAPI 代码隐藏起来**。

#### 4.3.2 核心流程

`configuration/mod.rs` 的组织流程：

1. 声明三个子模块：私有 `config`、私有 `error`、对外 `pub mod uapi`。
2. 用 `use` 引入下层依赖：`platform::{Endpoint, tun, udp}` 与 `wireguard::WireGuard`。
3. 用 `pub use` 对外导出三个名字：`ConfigError`、`Configuration`（trait）、`WireGuardConfig`（包装器）。

`WireGuardConfig` 内部持有一个 `WireGuard<T, B>` 实例并包在 `Arc<Mutex<Inner>>` 里（`Inner` 还持有端口、UDP owner、fwmark），从而把「协议核心 + 绑定状态」收拢成一个可克隆、可加锁的配置句柄。

依赖方向：`configuration` → `wireguard`（持有 `WireGuard`）+ `platform`（用到 `tun`/`udp` 的 trait 边界）。

#### 4.3.3 源码精读

[src/configuration/mod.rs:1-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/mod.rs#L1-L13) — 配置层入口，全文 13 行。注意第 5-7 行的三个 `use`，它说明了配置层依赖谁。

[src/configuration/config.rs:12-17](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L12-L17) — 模块注释，点明配置接口的目标：隐藏 IO 实现、为嵌入提供更简单接口、对宿主应用屏蔽复杂实现类型。

[src/configuration/config.rs:31-38](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/configuration/config.rs#L31-L38) — `WireGuardConfig<T, B>` 是一个元组结构体，包着 `Arc<Mutex<Inner<T, B>>>`；`Inner` 里同时持有 `wireguard: WireGuard<T, B>` 和绑定状态（`port`、`bind`、`fwmark`）。

`src/main.rs` 在创建协议设备后，正是把它包进配置接口：

[src/main.rs:139](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L139) — `configuration::WireGuardConfig::new(wg.clone())`，从此 TUN 事件线程和 UAPI 服务线程都通过 `cfg`（`WireGuardConfig` 的克隆）来操作设备。

UAPI 子模块（`configuration/uapi/`）的内部拆分（后续第 6 单元精讲）：

- `uapi/mod.rs`：`handle()` 按行读取文本、分用 `get=`/`set=`、回写 errno。
- `uapi/get.rs`：把设备/peer 状态序列化成 UAPI 文本。
- `uapi/set.rs`：解析 `set=` 配置行（一个 Interface/Peer 状态机）。

[src/main.rs:171](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/main.rs#L171) — UAPI 服务线程里调用 `configuration::uapi::handle(&mut stream, &cfg)`，把每一条外部连接交给配置层处理。

#### 4.3.4 代码实践

**实践目标**：跟踪 `WireGuardConfig` 如何把协议核心的泛型「藏起来」一层。

**操作步骤**：

1. 打开 `src/configuration/config.rs`，找到第 31 行 `WireGuardConfig<T: tun::Tun, B: udp::PlatformUDP>(...)`。
2. 对照第 33-38 行的 `Inner`，列出它持有的字段：`wireguard`、`port`、`bind`、`fwmark`。
3. 回到 `src/main.rs:139`，确认 `wg`（类型 `WireGuard<plt::Tun, plt::UDP>`）被传入 `WireGuardConfig::new`。

**需要观察的现象**：`main.rs` 之后所有的设备操作（`cfg.up(mtu)`、`cfg.down()`、UAPI handle）都只通过 `cfg` 这个包装器，不再直接碰 `WireGuard<plt::Tun, plt::UDP>` 这个又长又泛型的类型。

**预期结果**：写出一句话：「`WireGuardConfig` 把 `WireGuard<T,B>` 及其 UDP 绑定状态收拢进 `Arc<Mutex<Inner>>`，对上层屏蔽了 IO 泛型。」

#### 4.3.5 小练习与答案

**练习 1**：`configuration/mod.rs` 里哪个子模块是 `pub`，哪些是私有？为什么 `uapi` 要 `pub`？

**参考答案**：`pub mod uapi`（对外），`mod config`、`mod error`（私有）。`uapi` 要 `pub` 是因为 `main.rs` 第 171 行直接调用 `configuration::uapi::handle(...)`；而 `config`/`error` 里的类型通过 `pub use` 选择性导出（`Configuration`、`WireGuardConfig`、`ConfigError`），模块本身不必公开。

**练习 2**：`WireGuardConfig` 实现了 `Clone`（`config.rs:57-61`），它克隆时是真复制整个设备吗？

**参考答案**：不是。它内部是 `Arc<Mutex<Inner>>`，克隆只是增加一个 `Arc` 引用计数，多个 `cfg` 句柄共享同一个底层设备与绑定状态。

---

### 4.4 wireguard/ 协议核心层

#### 4.4.1 概念说明

`wireguard/` 是「纯协议核心」。它实现了 WireGuard 的全部协议逻辑，但**刻意不依赖任何具体 IO**——它对平台的依赖只有 `platform` 里那几个 trait（`Tun`、`UDP`、`Endpoint`），作为泛型边界出现。

模块顶部的一段文档注释把定位说得很清楚（见下方源码精读）：这一层「胶合」了两个纯引擎——**握手状态机（handshake）**和**加密路由器（router）**；每一个 WireGuard peer 由「一个握手 peer + 一个路由器 peer」组合而成。

`wireguard/` 内部又分两类：

- **胶水/编排代码**（本层直接文件）：`wireguard.rs`、`peer.rs`、`workers.rs`、`queue.rs`、`timers.rs`、`types.rs`、`constants.rs`。
- **两个纯引擎子树**：`handshake/`（Noise IK，第 4 单元）、`router/`（数据面加解密 + cryptokey 路由，第 5 单元）。

#### 4.4.2 核心流程

`wireguard/mod.rs` 的组织流程：

1. 用一段文档注释声明本层的定位（纯协议、可被 dummy 实例化、胶合 handshake 与 router）。
2. 声明本层子模块：`constants`、`handshake`、`peer`、`queue`、`router`、`timers`、`types`、`workers`，以及测试用 `tests` 和「同名模块」`wireguard`（用 `#[allow(clippy::module_inception)]` 抑制重名告警）。
3. `pub use wireguard::WireGuard;` 对外只暴露 `WireGuard` 类型。
4. 用 `use` 引入平台 trait 边界：`super::platform::{tun, udp, Endpoint}`。

数据流（高层、把 handshake/router 当黑盒）：

```
TUN(明文IP包) ──tun_worker──► router.send ──加密──► UDP(密文)
UDP(密文报文) ──udp_worker──► ┬─► router.recv ──解密──► TUN(明文IP包)
                              └─► handshake 队列 ──handshake_worker──► 协商会话密钥
```

#### 4.4.3 源码精读

[src/wireguard/mod.rs:1-31](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/mod.rs#L1-L31) — 协议核心层入口。重点看三处：

- 第 1-8 行的文档注释：明说本层是「full, pure, WireGuard implementation」，不依赖具体 IO/UAPI，可在单测里用 dummy 实例化；其作用是把 handshake 状态机和 crypto-key router「胶合」在一起。
- 第 9-16 行的 `mod` 列表：这是本层所有子模块的清单。
- 第 30 行 `use super::platform::{tun, udp, Endpoint};`：这是核心对平台的**唯一**依赖形式——只取 trait 模块，不取任何具体实现。

[src/wireguard/wireguard.rs:32-60](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L32-L60) — `WireguardInner<T: Tun, B: UDP>` 的字段定义，能直观看到「胶合」：它同时持有 `peers: handshake::Device<...>`（握手引擎）和 `router: router::Device<...>`（路由器引擎），外加定时器轮 `runner`、握手队列 `queue`、MTU、pending 计数等编排状态。

两个纯引擎子树的入口声明：

[src/wireguard/handshake/mod.rs:9-24](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/mod.rs#L9-L24) — 握手子树声明 `device/macs/messages/noise/peer/ratelimiter/timestamp/types`（+ tests），对外导出 `Device` 与消息类型常量。顶部注释指明实现的是 `Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s`。

[src/wireguard/router/mod.rs:1-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L1-L37) — 路由器子树声明 `anti_replay/device/ip/messages/peer/route/types` 与 `queue/receive/send/worker`（+ tests），对外导出 `DeviceHandle as Device`、`PeerHandle`、`Callbacks`，并定义 `SIZE_TAG`/`SIZE_MESSAGE_PREFIX` 等与传输报文尺寸相关的常量。

> **直觉**：`wireguard/` 是一颗三层树——根是胶水层（`wireguard.rs` 等），左右两个叶子子树是 `handshake/` 与 `router/`。三个子树都是「纯函数式」的协议代码，平台只是它们泛型上的一个边界。

#### 4.4.4 代码实践

**实践目标**：通览 `wireguard/` 子树，区分「胶水文件」与「两个引擎子树」。

**操作步骤**：

1. 浏览 `src/wireguard/` 直接子文件：`mod.rs`、`wireguard.rs`、`peer.rs`、`workers.rs`、`queue.rs`、`timers.rs`、`types.rs`、`constants.rs`、`tests.rs`。
2. 浏览 `src/wireguard/handshake/` 子目录的文件，对应一句话职责（如 `noise.rs`=Noise 加密核心、`macs.rs`=DoS 防御、`ratelimiter.rs`=令牌桶、`timestamp.rs`=TAI64N、`messages.rs`=握手消息布局）。
3. 浏览 `src/wireguard/router/` 子目录的文件，对应一句话职责（如 `send.rs`=发送加密、`receive.rs`=接收解密、`route.rs`=cryptokey 路由表、`ip.rs`=IP 头解析、`anti_replay.rs`=防回放、`queue.rs`=有序队列）。
4. 回到 `wireguard.rs:50-55`，确认胶水层同时持有 `handshake::Device` 与 `router::Device`。

**需要观察的现象**：`handshake/` 和 `router/` 这两个子目录里，没有出现 `linux`/`libc`/`/dev/net/tun` 之类的平台字样——它们是纯协议。

**预期结果**：产出一个三列表格：「文件 / 所属（胶水·握手·路由器）/ 一句话职责」。

#### 4.4.5 小练习与答案

**练习 1**：`wireguard/mod.rs` 里有一行 `#[allow(clippy::module_inception)] mod wireguard;`，这行在做什么？

**参考答案**：在 `wireguard/` 模块内部又声明了一个同名的子模块 `wireguard`（即 `src/wireguard/wireguard.rs`）。这会触发 clippy 的「模块同名」告警，所以用 `#[allow]` 抑制。真正的 `WireGuard` 结构体就在这个内层 `wireguard` 子模块里（`pub use wireguard::WireGuard;`）。

**练习 2**：协议核心对平台层的依赖，具体写在 `wireguard/mod.rs` 的哪一行？为什么说这保证了「平台可替换」？

**参考答案**：第 30 行 `use super::platform::{tun, udp, Endpoint};`。因为它只依赖 trait 模块，任何实现了这些 trait 的平台（linux、dummy、将来的 macos）都能接入，核心代码本身无需改动。

**练习 3**：`handshake` 和 `router` 这两个子树，哪一个负责「协商会话密钥」，哪一个负责「用密钥加解密数据报文」？

**参考答案**：`handshake/` 负责 Noise IK 握手、协商出对称会话密钥；`router/` 负责用该密钥对传输报文做 ChaCha20-Poly1305 加解密，并按 cryptokey 规则路由。这与 u1-l1 讲的「握手与数据面分离」一一对应。

---

## 5. 综合实践

把本讲全部内容串起来，完成下面这张「目录地图 + 依赖箭头」练习（这是本讲规格里要求的核心实践）。

### 实践目标

为 `src/` 下每个目录写一句中文说明，并画出三个顶层模块与两个引擎子树的依赖关系。

### 操作步骤

1. **目录一句话说明**：对照真实源码，为下列每个路径写一句不超过 20 字的中文说明。
   - `src/main.rs`
   - `src/util.rs`
   - `src/configuration/`
   - `src/configuration/uapi/`
   - `src/platform/`
   - `src/platform/linux/`
   - `src/platform/dummy/`
   - `src/wireguard/`
   - `src/wireguard/handshake/`
   - `src/wireguard/router/`

2. **画依赖箭头**：用文本箭头画出依赖方向。要求至少包含题目指定的主干：`configuration → wireguard → (handshake, router) → platform`，并补上 `main.rs` 与 `platform` 具体实现（linux/dummy）的位置。

### 参考输出（建议先自己写，再对照）

一句话说明示例：

- `src/main.rs`：程序入口，解析参数、起线程、把三大模块拼装起来。
- `src/util.rs`：守护进程化（daemonize）与降权（drop_privileges）。
- `src/configuration/`：配置接口层，封装 `WireGuard` 并处理 UAPI 文本协议。
- `src/configuration/uapi/`：UAPI 行协议的 get/set 解析与序列化。
- `src/platform/`：平台抽象层，定义 TUN/UDP/UAPI/Endpoint trait 与两套实现。
- `src/platform/linux/`：Linux 生产实现（ioctl/netlink/sticky socket）。
- `src/platform/dummy/`：纯软件测试实现，无副作用、可互连两实例。
- `src/wireguard/`：纯协议核心，胶合 handshake 与 router，不依赖具体 IO。
- `src/wireguard/handshake/`：Noise IK 握手引擎（协商会话密钥）。
- `src/wireguard/router/`：数据面路由器（加解密 + cryptokey 路由）。

依赖箭头示例：

```
                         ┌────────────────────────────────────────────┐
                         │                                            ▼
main.rs ──► configuration ──► wireguard ──┬─► handshake（纯协议）
                         │                │
                         │                ├─► router（纯协议）
                         │                │
                         └────────────────►└──► platform 的 trait (tun/udp/Endpoint)
                                                      ▲
                                                      │ 实现依赖 trait
                                          ┌───────────┴───────────┐
                                          │                       │
                                       linux/（生产）         dummy/（测试）
```

读图要点：

- `main.rs` 依赖全部三个顶层模块 + `util`。
- `configuration` 依赖 `wireguard`（持有 `WireGuard`）和 `platform`（用 tun/udp 边界）。
- `wireguard`（含 `handshake`、`router`）只依赖 `platform` 的 **trait**，不依赖任何具体实现——这是「平台可替换」的关键。
- `linux` 与 `dummy` 是 `platform` trait 的两套**实现**，依赖方向是「实现 → trait」（图里箭头向上）。

### 需要观察的现象

- 把 `configuration → wireguard → (handshake, router) → platform` 这条主干单独描一遍，发现它描述的是「上层调用下层、核心依赖抽象」的依赖方向。
- `linux`/`dummy` 与主干的关系是「实现抽象」，而非「被核心直接调用」。

### 预期结果

得到一张可保存的 `src/` 目录速查图。后续每一讲深入某个子模块时，都可以回到这张图定位「我现在在哪里」。

> 如果无法在本地运行项目（例如没有 root 权限创建 TUN），本实践作为「源码阅读型实践」同样成立——所有结论都能从 `mod.rs` 与 `use` 语句直接读出，无需运行。

## 6. 本讲小结

- `src/` 由三个顶层模块构成：`configuration/`（配置接口）、`wireguard/`（协议核心）、`platform/`（平台抽象），外加 `main.rs`（入口）与 `util.rs`（降权/守护进程化）。
- `main.rs` 通过 `use platform::*;` 拿到 `plt` 别名，从而用 `plt::Tun`/`plt::UAPI`/`plt::UDP` 间接调用当前平台的具体实现。
- `platform/` = 一组 trait（`tun.rs`/`udp.rs`/`uapi.rs`/`endpoint.rs`）+ 两套实现（生产 `linux/`、测试 `dummy/`），靠 `cfg` 选择编译哪一套。
- `configuration/` 用 `WireGuardConfig`（`Arc<Mutex<Inner>>`）把协议核心的 IO 泛型对上层隐藏，并承载 UAPI 文本协议处理（`uapi/` 子模块）。
- `wireguard/` 是纯协议核心：胶水层（`wireguard.rs`/`workers.rs`/`timers.rs` 等）组合了两个纯引擎子树 `handshake/`（协商密钥）与 `router/`（加解密 + 路由）。
- 依赖方向是「上层 → 下层、核心 → 抽象」：`configuration → wireguard → (handshake, router) → platform(trait)`，而 `linux`/`dummy` 实现 `platform` trait。

## 7. 下一步学习建议

- **横向接续本单元**：第 u1-l4 讲会逐段精读 `main()` 的运行生命周期（参数解析 → 绑定 UAPI → 创建 TUN → 降权 → 起线程 → `wg.wait()`），把你今天看到的 `mod` 声明真正「跑起来」。u1-l5 则深入 `util.rs` 的降权与守护进程化。
- **纵向深入平台层**：第 2 单元（u2-l1～u2-l4）逐个拆解 `platform/` 里的 trait 设计与 Linux/dummy 实现，是理解「平台可替换」的下一步。
- **纵向深入协议核心**：第 4 单元讲 `handshake/`（Noise IK），第 5 单元讲 `router/`（数据面）。阅读前建议先翻 `src/wireguard/wireguard.rs` 的字段，确认胶水层如何同时持有这两个引擎。
- **建议优先阅读的源码**：在进入后续讲义前，可以先把本讲列出的四个 `mod` 入口（`main.rs`、`platform/mod.rs`、`configuration/mod.rs`、`wireguard/mod.rs`）再读一遍——它们都很短，却是整张地图的骨架。
