# 项目概览：interprocess 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向「完全没接触过 interprocess」的读者。读完本讲，你应当能够：

- 说清楚 **IPC（进程间通信）** 到底是什么，以及 interprocess 这个 crate 想解决什么问题。
- 列出 interprocess 提供的四种通信原语：**local socket（本地套接字）**、**unnamed pipe（匿名管道）**、**Windows named pipe（Windows 命名管道）**、**Unix FIFO 文件**，并说出它们各自出现在哪些平台上。
- 读懂 interprocess 的整体模块组织，知道「跨平台公共 API」和「平台私有实现」分别放在哪里。
- 理解 Cargo 的三个 feature gate——`async`、`tokio`、`doc_cfg`——各自控制什么，以及为什么 `tokio` 默认是关闭的。

本讲不要求你已经写过 Rust 异步代码或系统调用，只需要对 Rust 的基本语法和 `Cargo.toml` 有印象即可。

## 2. 前置知识

在进入源码之前，先用最朴素的方式理解几个概念。

### 2.1 什么是「进程」

操作系统里，**进程（process）** 是一个正在运行的程序实例。每个进程有自己独立的内存空间：进程 A 不能直接读写进程 B 的内存。这是一种保护机制——一个程序崩溃了，通常不会拖垮另一个。

### 2.2 什么是「进程间通信（IPC）」

正因为进程之间内存隔离，当它们需要交换数据时（比如一个 GUI 程序要和一个后台守护进程通信），就必须借助操作系统提供的某种「通道」。这类机制统称为 **进程间通信（Inter-Process Communication，简称 IPC）**。

常见的 IPC 机制包括：管道（pipe）、套接字（socket）、共享内存、信号量、消息队列等。不同操作系统提供的具体 API 不同，这正是跨平台库要解决的核心矛盾。

### 2.3 为什么要专门做一个 crate

`std::os::unix::net` 给了 Unix domain socket，Windows 有自己的 named pipe API，但二者在「命名方式、连接握手、消息边界、超时」上都存在大量细微差异。interprocess 的价值在于：把这些差异藏在一层**统一的、平台无关的接口**后面，同时**尽量不丢失**平台原生的能力。

### 2.4 什么是 feature gate

Cargo 允许在 `Cargo.toml` 里声明可选功能（feature）。开启某个 feature 时，crate 内部用 `#[cfg(feature = "...")]` 包起来的代码才会被编译。这样可以做到：默认编译产物尽可能小、依赖尽可能少；需要高级功能（比如异步）时再显式开启。理解这一点，是读懂本讲后半部分 Cargo.toml 的前提。

## 3. 本讲源码地图

本讲只从「鸟瞰」角度引用几个最关键的文件，帮你建立全局印象，不深入实现细节（那是后续讲义的任务）。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「自我介绍」：定位、通信原语清单、平台支持等级、feature 说明、许可证。 |
| `Cargo.toml` | 构建清单：版本、依赖、feature 定义、example 与 binary 列表、lint 策略。 |
| `src/lib.rs` | crate 的根入口，声明所有顶层模块（`local_socket`、`unnamed_pipe`、`os` 等）。 |
| `src/os/unix/fifo_file.rs` | Unix-only 的 FIFO 文件创建（`create_fifo`，底层是 `mkfifo`）。 |
| `src/os/windows/named_pipe.rs` | Windows-only 的 named pipe 模块入口，并声明了用 named pipe 实现 local socket 的子模块。 |

> 提示：本讲引用的代码行号都基于当前 HEAD `ecb9daf`，每个引用都附有指向 GitHub 的永久链接，你可以点进去对照阅读。

## 4. 核心概念与源码讲解

### 4.1 interprocess 是什么：定位与设计目标

#### 4.1.1 概念说明

`interprocess` 是一个用 Rust 编写的进程间通信工具包（toolkit）。它的核心定位可以用 README 开头一句话概括：在**尽量暴露平台特性**的同时，**保持跨平台一致的接口**，并**鼓励写出可移植、正确的代码**。

这句话里有两个看似矛盾的目标，理解它们的张力，就理解了整个库的设计哲学：

- 「暴露平台特性」：意味着不把 Windows named pipe 独有的消息模式、安全描述符等功能砍掉。
- 「跨平台一致接口」：意味着你写一次 server/client 代码，在 Windows 和 Linux 上都能跑，且行为可预期。

interprocess 的解决办法是：**公共层**提供一套统一的 trait/类型；**平台后端层**各自实现原生功能；中间用一套「派发」机制连接。这套机制是后续单元（核心抽象层）的主线，本讲只需建立直觉。

#### 4.1.2 核心流程

从「用户视角」看 interprocess 的定位，可以画成这样的分层关系：

```
            ┌─────────────────────────────────────────┐
   用户代码 │  调用公共 API（local_socket / pipe() …）  │
            └────────────────────┬────────────────────┘
                                 │  统一接口
            ┌────────────────────▼────────────────────┐
   公共层    │  trait 定义 + enum 派发                  │
            └──────┬──────────────────────────┬───────┘
                   │ cfg(unix)                │ cfg(windows)
        ┌──────────▼─────────┐      ┌─────────▼────────────┐
   后端 │  Unix domain socket │      │  Windows named pipe   │
        │  libc 系统调用      │      │  windows-sys 系统调用 │
        └────────────────────┘      └───────────────────────┘
```

也就是说：同一份用户代码，落到不同平台上，会派发到完全不同的操作系统原语。这是「本地套接字」能跨平台的关键。

#### 4.1.3 源码精读

README 开头对项目定位的完整描述：

[README.md:16-18](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/README.md#L16-L18>) —— 说明 interprocess 是一个「在暴露平台特性与保持统一接口之间取得平衡」的 Rust IPC 工具包，这正是它的设计目标。

Cargo.toml 的包元信息印证了它的定位与适用范围：

[Cargo.toml:1-13](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L1-L13>) —— 当前版本为 `2.4.2`，edition 2021，最低支持的 Rust 版本（MSRV）为 `1.75`；`categories` 横跨 `os::unix-apis`、`os::windows-apis`、`asynchronous`，关键词为 `ipc`、`pipe`；许可证是 `0BSD OR Apache-2.0`（极宽松）。

#### 4.1.4 代码实践

**实践目标**：用最直接的方式确认 interprocess 的「身份信息」。

1. 打开本仓库根目录的 `Cargo.toml`，找到 `[package]` 段。
2. 记录三个事实：`name`、`version`、`rust-version`。
3. 思考：`rust-version = "1.75"` 这个 MSRV 对依赖它的下游项目意味着什么？（提示：下游项目的工具链不能低于这个版本。）

**需要观察的现象**：`name` 必须是 `interprocess`（这是你在 `Cargo.toml` 依赖里要写的名字）；`edition` 是 2021；版本号会随发布更新。

**预期结果**：你能不查文档就回答「这个 crate 在 crates.io 上叫什么、当前是哪个版本、要求至少什么版本的 Rust」。

#### 4.1.5 小练习与答案

**练习 1**：interprocess 的两个核心设计目标看似矛盾，分别是什么？
**答案**：「尽量暴露平台原生特性」与「保持跨平台统一接口」。它通过公共层 trait + 平台后端 + 派发机制来调和。

**练习 2**：interprocess 当前要求的最低 Rust 版本是多少？
**答案**：`1.75`（见 `Cargo.toml` 的 `rust-version` 字段）。

---

### 4.2 通信原语总览与平台分布

#### 4.2.1 概念说明

这是本讲最重要的一节。interprocess 把通信原语按「平台分布」分成几类，理解这张地图，你才知道什么时候该用什么。

四种核心原语：

1. **Local socket（本地套接字）**——跨平台。行为类似 TCP 套接字，但不走网络协议栈，而是用文件系统路径或命名空间名来寻址，性能更高。
2. **Unnamed pipe（匿名管道）**——Unix 与 Windows 都有。匿名、类似文件的单向通道，最常用于父子进程通信。
3. **FIFO 文件**——Unix 专有。是一种「特殊文件」，行为类似匿名管道，但存在于文件系统上，常被叫作 Unix「命名管道」，但**和 Windows 的 named pipe 完全不是一回事**。
4. **Windows named pipe（Windows 命名管道）**——Windows 专有。更像 Unix domain socket，使用独立的命名空间（而非磁盘路径），支持多连接、可选的消息边界。

> 关键易错点：同样是「named pipe / 命名管道」这个词，在 Unix 上指 FIFO 文件，在 Windows 上指另一种东西。interprocess 为避免混淆，特地把 Unix 的版本叫作「FIFO files」。这一点源码里有明确注释。

#### 4.2.2 核心流程

把四种原语按平台画成一张表，最清晰：

| 原语 | 跨平台？ | Windows 实现 | Unix 实现 |
| --- | --- | --- | --- |
| Local socket | ✅ 跨平台 | 基于 **Windows named pipe** | 基于 **Unix domain socket**（标准库提供） |
| Unnamed pipe | ✅ 两端都有 | Windows 匿名管道 | Unix 匿名管道 |
| FIFO 文件 | ❌ Unix 专有 | — | `mkfifo` 创建的特殊文件 |
| Windows named pipe | ❌ Windows 专有 | `windows-sys` 的 named pipe API | — |

注意两个细节：

- **Local socket 不是操作系统原生概念**，而是 interprocess 在底层原语之上构造的一层抽象：在 Windows 上它落在 named pipe，在 Unix 上它落在 Unix domain socket。所以「local socket」其实是「抽象」，named pipe / UDS 才是「实现」。
- Unix domain socket 本身**不在 interprocess 里**——标准库 `std::os::unix::net` 已经提供了。interprocess 只是把它包装成 local socket 暴露出来。

#### 4.2.3 源码精读

README 的「Communication primitives」小节是这张地图的权威出处：

[README.md:24-28](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/README.md#L24-L28>) —— 明确说明 local socket「在 Windows 上用 named pipe、在 Unix 上用 Unix domain socket 实现」，且完全绕过网络栈。

[README.md:31-34](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/README.md#L31-L34>) —— 说明 unnamed pipe 是匿名、单向的，最常用于父子进程通信。

[README.md:36-41](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/README.md#L36-L41>) —— FIFO 文件是 Unix 专有；并特别说明 Unix domain socket 已由标准库提供，interprocess 不再重复提供，只作为 local socket 暴露。

[README.md:43-44](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/README.md#L43-L44>) —— Windows named pipe 类似 Unix domain socket，用独立命名空间寻址。

源码侧的两个平台模块文档，印证了「命名歧义」这个坑：

[src/os/windows/named_pipe.rs:1-9](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe.rs#L1-L9>) —— 模块开头就强调「Windows 的 named pipe 与 Unix 的 named pipe（FIFO）是完全不同的东西」，并点明 named pipe 正是 local socket 在 Windows 上的实现。

[src/os/unix/fifo_file.rs:1-20](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L1-L20>) —— 模块文档解释 FIFO 文件的特点：单向、无消息边界；额外的接收方会收不到任何数据、额外的发送方会让数据混乱，因此 FIFO 适合「两个应用通过一个已知路径连通」的场景。

创建 FIFO 的实际实现只有几行，底层直接调 `mkfifo`：

[src/os/unix/fifo_file.rs:38-45](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L38-L45>) —— `create_fifo` 把路径转成 C 字符串后，用 `unsafe` 调用 `libc::mkfifo`，再用 `.true_val_or_errno(())` 把「返回 -1 表示失败」的 C 风格返回值转成 `io::Result`。（这种错误转换模式会在后续 FFI 单元详细讲。）

#### 4.2.4 代码实践

**实践目标**：把本节的「平台分布表」内化为自己的理解。

1. 阅读上面引用的 README 片段和两个平台模块文档。
2. 闭卷（不看表）在纸上画出四种原语的平台分布。
3. 重点回答两个问题：
   - local socket 在 **Windows** 和 **Unix** 上分别由什么底层原语实现？
   - 为什么 interprocess 要把 Unix 的「named pipe」改名叫「FIFO 文件」？

**需要观察的现象**：你会发现自己容易把「Windows named pipe」和「Unix FIFO（也叫 named pipe）」搞混，这正是 interprocess 改名的用意。

**预期结果**：你能流利回答——local socket 在 Windows 上由 named pipe 实现、在 Unix 上由 Unix domain socket 实现；改名是为了避免「named pipe」一词在两个平台上指代完全不同的东西。

#### 4.2.5 小练习与答案

**练习 1**：以下哪个原语是 interprocess **跨平台**提供的？（A）FIFO 文件 （B）Windows named pipe （C）unnamed pipe （D）Unix domain socket
**答案**：C。FIFO 是 Unix 专有，named pipe 是 Windows 专有，Unix domain socket 在标准库里（interprocess 只把它包装成 local socket），只有 unnamed pipe 两端都有。

**练习 2**：Unix domain socket 是 interprocess 自己实现的吗？
**答案**：不是。它由标准库 `std::os::unix::net` 提供，interprocess 只是在其上构造了 local socket 这层抽象。

**练习 3**：为什么 FIFO 文件「不适合多个发送方同时写入」？
**答案**：因为 FIFO 不保留消息边界，多个发送方的数据会在流里无序混合，接收方无法区分，导致数据不可用（见 `fifo_file.rs` 模块文档）。

---

### 4.3 模块地图：从 src/lib.rs 看整体组织

#### 4.3.1 概念说明

知道了有哪些原语，下一步是搞清楚它们在源码里「住在哪」。`src/lib.rs` 是 crate 的根入口，它用 `pub mod` 声明把各个模块挂载上来。理解这一层，你就拿到了在整个仓库里导航的「目录」。

interprocess 的模块分为两类：

- **公共跨平台模块**：`local_socket`、`unnamed_pipe`、`error`、`bound_util`——无论你在哪个平台编译，它们都在，提供统一 API。
- **平台私有模块**：`os::unix` 和 `os::windows`——用 `#[cfg(unix)]` / `#[cfg(windows)]` 条件编译，**同一时刻只有一个可见**。平台原生能力（FIFO、named pipe 等）都在这里。

#### 4.3.2 核心流程

顶层模块的挂载关系：

```
interprocess (crate root, src/lib.rs)
├── pub mod bound_util        # 类型约束工具（后续异步单元讲）
├── pub mod error             # 错误类型
├── pub mod local_socket      # 跨平台：本地套接字
├── pub mod unnamed_pipe      # 跨平台：匿名管道
└── pub mod os
    ├── pub mod unix          # 仅 cfg(unix) 时存在
    │   ├── fifo_file         # FIFO 文件
    │   ├── uds_local_socket  # 用 UDS 实现 local socket
    │   └── unnamed_pipe      # Unix 匿名管道后端
    └── pub mod windows       # 仅 cfg(windows) 时存在
        ├── named_pipe        # Windows named pipe
        │   └── local_socket  # 用 named pipe 实现 local socket
        ├── mailslot          # Windows 邮槽
        └── unnamed_pipe      # Windows 匿名管道后端
```

一个关键事实：`os::unix` 和 `os::windows` 在文档站点上「同一时间只看得到一个」。interprocess 的策略是——平台特有的功能一律放在 `#[cfg]` 门后面，在不支持的平台上**直接编译失败**，而不是运行时才报错。这是一种「把平台差异前置到编译期」的设计。

#### 4.3.3 源码精读

lib.rs 顶部的模块声明就是这张地图的源头：

[src/lib.rs:19-40](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L19-L40>) —— 声明 `bound_util`、`error`、`local_socket`、`unnamed_pipe` 四个公共模块；并把 `os` 模块拆成 `unix`（`#[cfg(unix)]`）和 `windows`（`#[cfg(windows)]`）两个互斥的子模块。注意 `os` 模块上方的那段文档注释也解释了「为何文档里同一时间只有一个平台可见」。

在 Windows 平台模块内部，named_pipe 又进一步嵌套了「用 named pipe 实现 local socket」的子模块：

[src/os/windows/named_pipe.rs:28-32](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe.rs#L28-L32>) —— `os::windows::named_pipe::local_socket` 子模块，即「Windows 上 local socket 的后端实现」所在。

#### 4.3.4 代码实践

**实践目标**：亲手确认模块树，而不是只看上面的图。

1. 打开 `src/lib.rs`，找到所有 `pub mod` 和 `#[cfg(unix)]` / `#[cfg(windows)]`。
2. 进入 `src/os/unix/` 和 `src/os/windows/` 目录，对照列出两边各自实现了哪些后端（你可以用编辑器的文件树或 `ls`）。
3. 验证：`local_socket` 这个跨平台模块在 `os::unix/` 和 `os/windows/` 下都各有一个对应的「后端」目录吗？

**需要观察的现象**：你会看到 `src/os/unix/` 下有 `uds_local_socket/` 和 `local_socket.rs`，`src/os/windows/` 下有 `named_pipe/`（其内含 `local_socket` 子模块）——这正是「同一个抽象、两套实现」的物理体现。

**预期结果**：你能画出从 `local_socket`（公共）到 `os::unix::uds_local_socket` / `os::windows::named_pipe::local_socket`（后端）的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `os::unix` 和 `os::windows` 用 `#[cfg]` 而不是运行时判断？
**答案**：interprocess 的策略是「编译期门控」，在不支持的平台上直接编译失败，把平台差异前置到编译期，避免运行时才暴露问题。

**练习 2**：`error` 模块是平台私有的还是跨平台公共的？
**答案**：跨平台公共的。它在 `lib.rs` 里直接以 `pub mod error` 声明，没有任何 `cfg` 门。

---

### 4.4 Feature gate：async、tokio、doc_cfg

#### 4.4.1 概念说明

interprocess 默认只编译「同步」版本的 API。异步支持（目前只有 Tokio 运行时）是通过 feature gate 显式开启的。Cargo.toml 里定义了三个 feature：

- **`async`**：引入 `futures-core` 依赖，打开异步相关的基础设施。
- **`tokio`**：开启 Tokio 运行时下的异步变体（`local_socket::tokio`、`unnamed_pipe::tokio` 等）。它**隐式包含 `async`**。
- **`doc_cfg`**：这是给文档用的——开启后，平台特有、feature 特有的项会在文档里显示一个徽章，标明它的 `cfg(...)` 条件。通常只在 docs.rs 上构建时启用，普通用户不需要。

为什么 `tokio` 默认关闭？因为带异步就会带上 `tokio`、`futures-core` 这些依赖，而很多只需要同步 IPC 的项目并不想为它们付出编译时间和二进制体积的代价。Feature gate 让「按需付费」成为可能。

#### 4.4.2 核心流程

三个 feature 的依赖关系：

```
tokio ──► async ──► futures-core（外部依赖）
                （隐式启用）

doc_cfg（独立，仅影响文档展示）
```

也就是说，只要你在 `Cargo.toml` 里写 `features = ["tokio"]`，就会连带启用 `async`，并拉入 `tokio`、`futures-core` 两个依赖；源码里所有 `#[cfg(feature = "tokio")]` 标记的类型才会被编译进来。

#### 4.4.3 源码精读

feature 的定义在 Cargo.toml：

[Cargo.toml:25-29](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L25-L29>) —— `[features]` 段：`default = []`（默认不启用任何可选 feature）；`async = ["futures-core"]`；`tokio = ["dep:tokio", "async"]`（注意它包含了 `async`）；`doc_cfg = []`。

README 的 Feature gates 小节一句话总结了用户最关心的那条：

[README.md:119-121](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/README.md#L119-L121>) —— `tokio` 默认关闭，开启后提供各 IPC 原语的 Tokio 变体。

异步支持的范围（README 的 Asynchronous I/O 小节）：

[README.md:47-54](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/README.md#L47-L54>) —— 目前唯一支持的异步运行时是 Tokio；local socket 和 Windows named pipe 由 interprocess 提供 Tokio 变体；Unix domain socket 的异步支持在 Tokio 自身。

在源码里，feature 门控最直接的例子是 named pipe 的 tokio 子模块：

[src/os/windows/named_pipe.rs:54-60](<https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe.rs#L54-L60>) —— `pub mod tokio` 整个被 `#[cfg(feature = "tokio")]` 包住；不开启 tokio feature 时，这段代码根本不会被编译。模块上方的文档注释还提醒：这些类型**只能**在 Tokio 运行时上下文里使用，否则方法会 panic。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 feature 如何影响「能不能找到某个类型」。

1. 在一个临时 Rust 项目里，`Cargo.toml` 加上依赖 `interprocess = { version = "2.4.2" }`（**不**加 `features`）。
2. 尝试在代码里写 `use interprocess::local_socket::tokio;`，执行 `cargo check`。
3. 接着把依赖改成 `interprocess = { version = "2.4.2", features = ["tokio"] }`，再次 `cargo check`。
4. 对比两次结果。

**需要观察的现象**：第一次会编译失败（找不到 `tokio` 模块）；第二次编译通过。

**预期结果**：你直观体会到——不开 `tokio` feature，整个异步子模块就不存在；开了之后才被编译进来。

> 如果你暂时不方便建项目，也可以用「源码阅读型实践」替代：在仓库里全局搜索 `#[cfg(feature = "tokio")]`，数一数有多少处，体会「tokio feature 控制了一大批代码的编译与否」这件事。

#### 4.4.5 小练习与答案

**练习 1**：开启 `tokio` feature 后，`async` feature 是否也会自动启用？为什么？
**答案**：会。因为 Cargo.toml 里 `tokio = ["dep:tokio", "async"]` 显式把 `async` 列为 tokio 的依赖。

**练习 2**：`doc_cfg` feature 主要是给谁用的？
**答案**：主要是给文档构建（如 docs.rs）用的，让平台特有/feature 特有的项在文档里显示 `cfg(...)` 徽章，普通业务代码通常不需要开启。

**练习 3**：在非 Tokio 的异步运行时（如 `async-std`、`smol`）里能用 interprocess 的 tokio 类型吗？
**答案**：不能。相关类型的方法在 Tokio 运行时上下文之外会 panic（见 `named_pipe.rs` tokio 模块的文档说明）。

---

## 5. 综合实践

把本讲四节的内容串起来，完成下面这个「项目认知报告」小任务：

1. **克隆并定位**：确认你处于本仓库根目录，打开 `README.md` 与 `Cargo.toml`。
2. **原语清单**：用你自己的话，写出 interprocess 的四种通信原语，并各配一句「它解决什么问题」。
3. **平台映射**：写明 local socket 在 Windows 和 Unix 上分别由什么原语实现，并解释为什么 Unix 的「named pipe」要被改名叫「FIFO 文件」。
4. **模块导航**：列出 `src/lib.rs` 里声明了哪些跨平台公共模块，并指出 `os::unix` / `os::windows` 是如何用 `#[cfg]` 实现互斥的。
5. **feature 自检**：说明 `tokio` feature 控制了什么、默认是否开启、开启它会不会连带启用 `async`。

完成后，把这份报告写进自己的笔记里——它就是你后续阅读 interprocess 源码时的「导航地图」。这一步不需要运行任何命令，重在用自己的语言复述，确保你真的理解了。

## 6. 本讲小结

- **interprocess** 是一个 Rust IPC 工具包，设计目标是「尽量暴露平台原生特性」与「保持跨平台统一接口」的平衡。
- 它提供四种原语：**local socket**（跨平台，Windows 上由 named pipe 实现、Unix 上由 Unix domain socket 实现）、**unnamed pipe**（两端都有）、**FIFO 文件**（Unix 专有）、**Windows named pipe**（Windows 专有）。
- 源码组织上，跨平台公共模块（`local_socket`、`unnamed_pipe`、`error`、`bound_util`）在 `lib.rs` 直接声明；平台私有后端放在 `os::unix` / `os::windows`，用 `#[cfg]` 互斥，同一时刻只编译一个。
- 三个 feature：`async`（异步基础）、`tokio`（Tokio 运行时变体，隐含 `async`，默认关闭）、`doc_cfg`（仅影响文档展示）。
- 「named pipe」一词在两个平台上指代完全不同的东西，这是阅读本库源码时最大的易错点。
- Local socket 是 interprocess 的**抽象**，而非操作系统原语——这一点贯穿后续所有单元。

## 7. 下一步学习建议

本讲建立的是「鸟瞰图」。接下来建议：

1. **先看目录与构建**：进入 `u1-l2`（目录结构与模块地图）和 `u1-l3`（构建、Feature Gate 与示例运行），把模块树和「如何跑通一个 example」彻底弄熟。
2. **跑通第一个例子**：`u1-l4` 会带你逐行精读 local socket 同步 server/client 示例，这是从「读文档」到「写代码」的桥梁。
3. **再深入抽象机制**：第一单元结束后，进入第二单元（核心抽象：跨平台派发机制），你会看到 interprocess 如何用 enum dispatch 把公共接口和平台后端连起来——那是理解整个库内部实现的关键。

建议在进入 `u1-l4` 之前，先确保自己能独立回答本讲「综合实践」里的五点，再继续往下走。
