# 目录结构与模块地图

## 1. 本讲目标

上一讲（u1-l1）我们已经知道 interprocess 是一个 Rust 进程间通信（IPC）库，它用「统一的跨平台 API + 平台原生后端」的方式工作。本讲我们要打开 `src/` 目录，看清楚这套抽象在文件和模块层面是怎么摆放的。

学完本讲，你应该能够：

- 画出 interprocess `src/` 目录的顶层模块树。
- 区分三类模块：**跨平台公共 API**、**平台私有后端实现**、**crate 内部基础设施**。
- 理解 `#[cfg(unix)]` / `#[cfg(windows)]` 条件编译如何让 `os::unix` 与 `os::windows` 在同一次编译中只有一个被编译进来。
- 读懂 `lib.rs` 作为 crate 根模块，是如何把上面这些零件「总装」成一个完整 crate 的。

> 承接上一讲：上一讲我们提到「公共模块在 `lib.rs` 直接声明，平台私有后端置于 `os::unix` / `os::windows`，用 `#[cfg]` 互斥编译」。本讲就是要把这句话落实到具体文件、具体行号上。

## 2. 前置知识

- **Rust 模块系统**：crate（一个编译单元）、`mod`（声明子模块）、`pub`（导出给外部使用）、`pub(crate)`（只在本 crate 内可见）、`use`（引入名字）。
- **条件编译**：`#[cfg(条件)]` 让某段代码只在满足条件时参与编译。例如 `#[cfg(unix)]` 表示「目标平台是 Unix 系（Linux、macOS、FreeBSD 等）时才编译」。
- **路径属性**：`#[path = "..."]` 可以显式指定一个 `mod` 对应的源码文件，覆盖 Rust 默认的「目录同名」规则。
- **`include_str!`**：编译期把一个文本文件的内容作为字符串常量嵌入进来。

如果你对其中某些概念不熟，不用担心，本讲会在用到的地方顺便解释。

## 3. 本讲源码地图

本讲只看「骨架」，不深入各原语的实现细节。下表是本讲涉及的关键文件及其职责：

| 文件 | 职责 | 平台 |
| --- | --- | --- |
| `src/lib.rs` | crate 根模块：声明所有顶层模块、定义 `ConnectWaitMode`、配置 lint、挂载测试 | 跨平台 |
| `src/local_socket.rs`（及其 `src/local_socket/` 子目录） | 跨平台 local socket 公共 API | 跨平台 |
| `src/unnamed_pipe.rs`（含 `unnamed_pipe/tokio.rs`） | 跨平台匿名管道公共 API | 跨平台 |
| `src/error.rs` | 通用错误类型（`ConversionError` 等） | 跨平台 |
| `src/bound_util.rs` | 把 `&T: Trait` 编码进类型系统的工具（`RefRead`/`RefWrite` 等） | 跨平台 |
| `src/os.rs`（内联在 `lib.rs` 的 `os` 块里） | 平台私有实现的总入口，按 cfg 暴露 `unix` 或 `windows` | 条件 |
| `src/os/unix.rs` | Unix 后端总入口 | 仅 `cfg(unix)` |
| `src/os/windows.rs` | Windows 后端总入口 | 仅 `cfg(windows)` |
| `src/platform_check.rs` | 编译期检查目标平台是否被支持 | 跨平台 |

完整的文件清单可以用一行命令获取（这是真实可运行的命令）：

```bash
git ls-files 'src/**/*.rs' | sort
```

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：先看 crate 根 `lib.rs` 的总装，再看跨平台公共原语（`local_socket` / `unnamed_pipe`），接着看跨平台支撑模块（`error` / `bound_util`），最后看 `os` 模块及其平台条件编译。

### 4.1 lib.rs：crate 根模块与总装入口

#### 4.1.1 概念说明

在 Rust 里，crate 的「根源文件」就是 `lib.rs`（库 crate）或 `main.rs`（二进制 crate）。interprocess 是一个库，所以 `src/lib.rs` 是它一切模块的总装入口。理解一个 crate 的结构，第一件事就是读它的 `lib.rs`：所有顶层模块都在这里被声明（`mod`）或导出（`pub use`）。

`lib.rs` 在 interprocess 里主要做四件事：

1. 配置 crate 级别的文档与 lint。
2. 声明所有顶层模块（公共的 + 私有的）。
3. 定义少数几个不属于任何子模块的顶层类型（例如 `ConnectWaitMode`）。
4. 挂载集成测试。

#### 4.1.2 核心流程

读 `lib.rs` 时，可以按「从上到下、由外到内」的顺序理解：

```
1. crate 文档与 lint 配置   ← 给整个 crate 定基调
2. 私有基础设施模块          ← platform_check / macros / try_clone / atomic_enum / misc
3. 公共跨平台模块            ← bound_util / error / local_socket / unnamed_pipe
4. os 平台模块块             ← 按 cfg 暴露 unix 或 windows
5. 顶层类型 ConnectWaitMode  ← 不归属任何子模块的公共类型
6. 集成测试挂载              ← #[cfg(test)] #[path = "../tests/index.rs"]
```

这六块合起来，就把整个 crate 的所有源码「总装」完毕。

#### 4.1.3 源码精读

**① crate 文档与 lint 配置**。第一行把 `README.md` 当作文档首页，紧接着是一组 `warn` 级别的 lint。这些 lint 是给整个 crate（不含 examples）定的代码规范：

[src/lib.rs:1-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L1-L10) — README 作为 crate 文档，并启用 `missing_docs`、`panic_in_result_fn` 等 lint。

**② 私有基础设施模块**。这几个模块没有 `pub`，所以 crate 外部用户看不到，只服务于 crate 内部：

[src/lib.rs:12-17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L12-L17) — `platform_check`（编译期平台检查）、`macros`（`#[macro_use]` 让宏在声明顺序之后的全 crate 可用）。

**③ 公共跨平台模块**。这四个是面向用户的跨平台 API，全部 `pub`：

[src/lib.rs:19-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L19-L22) — `bound_util`、`error`、`local_socket`、`unnamed_pipe` 四个公共模块。

**④ os 平台模块块**。这是本讲的重头戏（详见 4.4）。`os` 模块本身总存在，但它的两个子模块用 cfg 互斥：

[src/lib.rs:33-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L33-L40) — `pub mod os { ... }`，内部用 `#[cfg(unix)]` / `#[cfg(windows)]` 暴露对应子模块。

**⑤ 顶层类型 `ConnectWaitMode`**。它描述客户端连接服务端时的等待方式，不属于任何子模块，所以直接定义在 `lib.rs` 根上：

[src/lib.rs:42-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L42-L66) — `ConnectWaitMode` 枚举（`Deferred` / `Timeout` / `Unbounded`）及其内部辅助方法。

**⑥ 集成测试挂载**。测试代码并不放在 `src/` 下，而是放在仓库根的 `tests/` 目录，通过 `#[path]` 显式指过去：

[src/lib.rs:75-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L75-L78) — `#[cfg(test)]` 且 `#[path = "../tests/index.rs"]`，把外部 `tests/` 当作内联测试模块编译。

> 小提示：`mod try_clone;` 后面紧跟 `pub use try_clone::*;`（[src/lib.rs:68-69](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L68-L69)）是一种常见模式——模块本身私有（`mod`），但其内容整体导出（`pub use *`），这样既能扁平化导出，又隐藏了模块这一层路径。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `lib.rs` 里声明的所有 `mod`。

**操作步骤**：

1. 打开 `src/lib.rs`。
2. 用编辑器搜索所有 `mod `（注意 `mod` 后有一个空格）出现的行。
3. 把它们分成三类：`pub mod`（公共）、`mod`（私有）、`#[macro_use] mod`（宏）。

**需要观察的现象**：你会发现 `pub mod` 只有 4 个顶层跨平台模块，加上 `os` 块里的 2 个平台子模块；其余都是私有模块。

**预期结果**：公共模块 = `bound_util`、`error`、`local_socket`、`unnamed_pipe`、`os::unix`/`os::windows`。私有模块 = `platform_check`、`macros`、`try_clone`、`atomic_enum`、`misc`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `macros` 模块要用 `#[macro_use]` 而不是 `pub use`？

> **参考答案**：`#[macro_use]` 让该模块里定义的宏在声明点之后对整个 crate 可见（按文本顺序）。这些是给 crate 内部使用的内部宏，不需要通过 `pub use` 导出给外部用户。

**练习 2**：`ConnectWaitMode` 的三个变体里，哪个是 `#[default]`？

> **参考答案**：`Unbounded`。它带 `#[default]`，所以 `ConnectWaitMode::default()` 返回 `Unbounded`（见 [src/lib.rs:53-56](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L53-L56)）。

---

### 4.2 跨平台公共原语：local_socket 与 unnamed_pipe

#### 4.2.1 概念说明

`local_socket` 和 `unnamed_pipe` 是 interprocess 暴露给用户的两类核心通信原语。它们都位于 `src/` 顶层，并且**自身就是跨平台的**——用户写一份代码，在 Windows 和 Unix 上都能编译运行。

关键点在于：这两个模块是「公共 API 层」，它们**不直接**包含某平台的具体系统调用代码。具体实现藏在 `os::unix` / `os::windows` 里，公共层通过宏（`impmod!`、`mkenum` 等）把对应平台的实现「注入」进来。本讲只需记住这种分层；注入机制本身是 u2-l2、u2-l3 的主题。

- **`local_socket`**：local socket 是 interprocess 自造的抽象（不是 OS 原语），在 Unix 上由 Unix domain socket 实现、Windows 上由 named pipe 实现。它对外暴露 `Listener`、`Stream`、`RecvHalf`、`SendHalf` 等枚举类型。
- **`unnamed_pipe`**：匿名管道，两端都通过句柄访问，适合与子进程通信。对外暴露 `pipe()`、`Sender`、`Recver`。

#### 4.2.2 核心流程

两个公共模块的共性流程：

```
用户调用公共 API（如 local_socket::Stream::connect / unnamed_pipe::pipe()）
        │
        ▼
公共模块（src/local_socket.rs、src/unnamed_pipe.rs）
   ├─ 定义对外类型（多为 enum dispatch）
   └─ 用 impmod! / mkenum 把「平台实现类型」以统一别名引入
        │
        ▼
平台后端（os::unix/* 或 os::windows/*）做真正的系统调用
```

也就是说，公共模块是「壳」，平台后端是「芯」。`src/local_socket/` 和 `src/unnamed_pipe/` 目录里放的就是这个「壳」的各种零件（name、listener、stream、options、tokio 异步变体等）。

#### 4.2.3 源码精读

**① `local_socket` 模块文档**。模块开头的文档明确说出 local socket 的本质和 dispatch 设计：

[src/local_socket.rs:1-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L1-L23) — 说明 local socket「不是真实 OS 原语」，而是用 `enum_dispatch` 风格的枚举在多个底层实现之间派发；并指出目前每个平台只有一个后端，因此派发是零开销的。

**② `unnamed_pipe` 模块文档与入口**。文档点出匿名管道「只能通过句柄访问」、句柄默认可继承的特性：

[src/unnamed_pipe.rs:1-20](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L1-L20) — 匿名管道的定位与句柄可继承性说明。

**③ `impmod!` 注入点**。这就是公共层「对接平台后端」的桥梁（细节留到 u2-l3）：

[src/unnamed_pipe.rs:26-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L26-L30) — `impmod! { unnamed_pipe, Recver as RecverImpl, Sender as SenderImpl, pipe_impl }` 按当前平台把后端类型以 `RecverImpl`/`SenderImpl`/`pipe_impl` 别名注入。

**④ 公共 `pipe()` 函数**。它只是对注入进来的 `pipe_impl()` 的一层转发：

[src/unnamed_pipe.rs:49-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L49-L50) — `pub fn pipe() -> io::Result<(Sender, Recver)> { pipe_impl() }`。

**⑤ 子目录组织**。这两个公共模块各自有子目录（`src/local_socket/`、`src/unnamed_pipe/`），把 listener、stream、name、options、tokio 变体等拆成独立文件。例如 `unnamed_pipe` 还有一个受 feature 门控的 Tokio 子模块：

[src/unnamed_pipe.rs:22-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L22-L24) — `#[cfg(feature = "tokio")] pub mod tokio;`，只有在启用 `tokio` feature 时才编译。

#### 4.2.4 代码实践

**实践目标**：感受「公共层是壳、平台后端是芯」的分层。

**操作步骤**：

1. 打开 `src/unnamed_pipe.rs`，找到 `impmod!` 调用（4.2.3 ③）。
2. 记下它注入的别名：`RecverImpl`、`SenderImpl`、`pipe_impl`。
3. 在 `src/os/unix/unnamed_pipe.rs`（如果你在 Unix 上）或 `src/os/windows/unnamed_pipe.rs`（如果你在 Windows 上）里搜索这些名字对应的真实定义。

**需要观察的现象**：公共模块里出现的 `Recver`/`Sender` 只是包装，真正的类型定义在平台后端文件里。

**预期结果**：你会看到公共 `Sender` 是对 `SenderImpl`（平台类型）的薄封装，验证了「壳 + 芯」的分层。如果不确定运行环境，可以标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：local socket 是不是操作系统提供的原语？

> **参考答案**：不是。它是 interprocess 的抽象，底层在 Unix 用 Unix domain socket、Windows 用 named pipe 实现（见 [src/local_socket.rs:5-8](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L5-L8)）。

**练习 2**：`unnamed_pipe` 的句柄默认是否可被子进程继承？为什么这一点对「与子进程通信」很重要？

> **参考答案**：默认可继承（见 [src/unnamed_pipe.rs:7-8](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L7-L8)）。正因为可继承，父进程创建的管道端点才能在子进程启动时被继承，从而实现父子进程间的匿名管道通信。

---

### 4.3 跨平台支撑模块：error 与 bound_util

#### 4.3.1 概念说明

除了两个「通信原语」模块，`lib.rs` 还声明了两个「支撑性」公共模块：

- **`error`**：定义全 crate 通用的错误类型。interprocess 经常需要做「句柄/FD 与标准库类型之间的转换」，这些转换很多是可失败的（语义不是 1:1），所以需要一个能同时承载「细节」「OS 原因」「原始所有权」的错误类型。
- **`bound_util`**：trait bound 工具。它用 GAT（generic associated types）把「`&T` 实现了某 trait」这件事编码进 Rust 类型系统，让流对象既能按值、又能按引用读写。

这两个模块本身不涉及任何平台系统调用，是纯 Rust 的类型抽象，因此天然跨平台。

#### 4.3.2 核心流程

**`error` 模块**核心是 `ConversionError<S, E>`：

```
ConversionError {
    details: E,        // 转换在哪一阶段失败（可选）
    cause: Option<io::Error>,  // 底层 OS 错误（可选）
    source: Option<S>, // 归还输入对象的所有权（可选）
}
```

设计要点：转换失败时，把原始对象的所有权「还给」调用方（`source` 字段），这样调用方可以拿它另作他用。

**`bound_util` 模块**核心是 `bound_util!` 宏，它生成形如 `RefRead`/`RefWrite` 的 trait，把「`&Self: Read`」表达为可在泛型约束里使用的形式。这是 u6-l3 的深入主题，这里只需知道它在 `src/bound_util.rs`。

#### 4.3.3 源码精读

**① `error` 模块文档**。文档解释了为什么需要这种「能归还所有权」的错误类型：

[src/error.rs:1-8](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L1-L8) — 通用错误类型说明。

**② `ConversionError` 结构**。三个字段的设计：

[src/error.rs:30-38](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L30-L38) — `ConversionError<S, E = NoDetails>`，含 `details`、`cause`、`source` 三段。

**③ `bound_util` 模块文档与宏**。`bound_util!` 宏为指定 trait 生成「按引用实现」的包装 trait：

[src/bound_util.rs:1-9](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/bound_util.rs#L1-L9) — 模块定位与基础 `Is<T>` 标记 trait。

[src/bound_util.rs:10-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/bound_util.rs#L10-L41) — `bound_util!` 宏的定义（用 GAT 把 `&Self: Trait` 编码进类型系统）。

[src/bound_util.rs:43-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/bound_util.rs#L43-L45) — 实际生成的 `RefRead`（按引用 `Read`）等 trait 的起点。

#### 4.3.4 代码实践

**实践目标**：确认 `error` 和 `bound_util` 被哪些公共模块使用（印证它们是「支撑模块」）。

**操作步骤**：

1. 在仓库根目录运行 `grep -rn "use crate::error" src/`（或用编辑器全局搜索）。
2. 在仓库根目录运行 `grep -rn "bound_util" src/`。

**需要观察的现象**：你会看到 `error::ConversionError` 出现在 local_socket 的 stream、listener 等处；`bound_util` 的 `RefRead`/`RefWrite` 出现在 stream trait 处。

**预期结果**：印证这两个模块是「被多处引用的底层支撑」，而不是独立的通信原语。（`grep` 是只读操作，安全可运行；具体命中行号待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`ConversionError` 的 `source` 字段为什么是 `Option<S>`？

> **参考答案**：因为转换失败时，把原始输入对象的所有权还给调用方是有用的（可以拿去另作他用），但调用方（被调用者）也保留「不归还」的自由（例如 Tokio 的对象无法 `try_clone`，就只能不归还）。所以用 `Option` 表示「可能归还」。

**练习 2**：`bound_util` 里的 `Is<T>` 标记 trait 起什么作用？

> **参考答案**：它是一个辅助约束（`impl<T: ?Sized> Is<T> for T`），配合 GAT 用来在类型层面表达并约束「某个关联类型就是 `&'a Self` 且 `&'a Self` 实现了目标 trait」。

---

### 4.4 os 模块与平台条件编译（os::unix / os::windows）

#### 4.4.1 概念说明

`os` 模块是 interprocess 「跨平台抽象」的关键拼图：所有平台私有后端实现都收拢在这里。它在 `lib.rs` 里以一个内联块的形式出现，并按 `#[cfg(unix)]` / `#[cfg(windows)]` 决定暴露哪个子模块。

理解条件编译的核心事实：

- 一个编译目标（target）要么是 Unix 系，要么是 Windows，二者**互斥**。
- 因此 `#[cfg(unix)]` 的代码和 `#[cfg(windows)]` 的代码在同一次编译中**只有一个**会被编译。
- `os` 模块本身永远存在，但它的子模块 `unix`、`windows` 是「二选一」的。

这就解释了为什么 `os/unix.rs` 和 `os/windows.rs` 可以各自声明同名的东西（比如都叫 `local_socket`、`unnamed_pipe`）而不会冲突——它们从不同时参与编译。

#### 4.4.2 核心流程

条件编译的装配过程：

```
lib.rs: pub mod os {
    #[cfg(unix)]    pub mod unix;     ← Unix 目标时编译 src/os/unix.rs
    #[cfg(windows)] pub mod windows;  ← Windows 目标时编译 src/os/windows.rs
}
```

在 `src/os/unix.rs` 里（Unix 后端总入口）：

```
pub mod fifo_file;        ← Unix 专有：FIFO 文件
pub mod local_socket;     ← local socket 的 Unix 后端（基于 UDS）
pub mod uds_local_socket; ← UDS 实现细节
pub mod unnamed_pipe;     ← 匿名管道的 Unix 后端
+ 私有：c_wrappers / fdops / ud_addr / imports / unixprelude
```

在 `src/os/windows.rs` 里（Windows 后端总入口）：

```
pub mod named_pipe;           ← Windows 专有：named pipe（含 local socket 后端）
pub mod local_socket;         ← local socket 的 Windows 后端（基于 named pipe）
pub mod security_descriptor;  ← Windows 专有：安全描述符
pub mod unnamed_pipe;         ← 匿名管道的 Windows 后端
+ 私有：linger_pool / maybe_arc / needs_flush / share_handle / adv_handle / ...
```

注意两个后端「形似而神不同」：它们都有 `local_socket`、`unnamed_pipe`，但各自的专有原语不同（Unix 有 `fifo_file`，Windows 有 `named_pipe`、`security_descriptor`），私有基础设施也不同（Windows 多了一堆句柄/缓冲优化模块）。

#### 4.4.3 源码精读

**① `os` 模块的内联定义**。这就是条件编译的总开关：

[src/lib.rs:33-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L33-L40) — `pub mod os` 内联块，`unix`/`windows` 各带 `#[cfg]`；`doc_cfg` feature 还会在文档里给它们打上平台徽标。

**② Unix 后端总入口**。`os/unix.rs` 声明 Unix 平台的所有子模块：

[src/os/unix.rs:12-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix.rs#L12-L23) — 私有基础（`imports`、`c_wrappers`、`fdops`、`ud_addr`）与公共后端（`fifo_file`、`local_socket`、`uds_local_socket`、`unnamed_pipe`）。

[src/os/unix.rs:1-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix.rs#L1-L10) — Unix 模块文档，专门介绍了 FIFO 文件。

**③ Windows 后端总入口**。`os/windows.rs` 声明 Windows 平台的所有子模块：

[src/os/windows.rs:4-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows.rs#L4-L14) — 公共后端（`local_socket`、`named_pipe`、`security_descriptor`、`unnamed_pipe`）与私有支撑模块的再导出。

[src/os/windows.rs:16-25](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows.rs#L16-L25) — Windows 特有的一批私有内部模块（`linger_pool`、`maybe_arc`、`needs_flush`、`tokio_flusher` 等），其中 `tokio_flusher` 还受 `feature = "tokio"` 门控。

**④ 编译期平台检查**。`platform_check.rs` 在不支持的平台上直接 `compile_error!`，把「平台不支持」的失败提前到编译期：

[src/platform_check.rs:1-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/platform_check.rs#L1-L13) — 对「非 Unix 且非 Windows」「Emscripten」「非 32/64 位指针宽度」的目标直接报编译错误。

> 观察对照：把 ② 和 ③ 并排看，你会发现 `os/unix.rs` 和 `os/windows.rs` 的公共子模块名（`local_socket`、`unnamed_pipe`）是对称的——这正是公共层 `impmod!` 能「无差别注入」的前提。它们各自额外的专有模块（Unix 的 `fifo_file` vs Windows 的 `named_pipe`/`security_descriptor`）则体现了平台差异。

#### 4.4.4 代码实践

**实践目标**：亲眼看到「同一次编译只有一个平台后端」。

**操作步骤**：

1. 在你当前的平台上执行 `cargo build`（只读地编译，不会改源码）。
2. 观察编译器实际处理了 `src/os/unix.rs` 还是 `src/os/windows.rs`。一个简单办法是用 `touch src/os/unix.rs` 后再 `cargo build -v`，看它是否被重新编译——在你不在 Unix 上时，它根本不会进入编译图。
3. 或者在 `src/lib.rs` 的 `os` 块里**临时**给 `unix` 加一行注释、给 `windows` 加一行注释来验证（这只是观察手段，实践结束后请还原，本讲禁止修改源码，所以这一步可选）。

**需要观察的现象**：只有与你当前平台匹配的那一个 `os::*` 后端参与编译；另一个即使源码文件存在，也不会被编译。

**预期结果**：在 Linux/macOS 上只有 `os::unix` 被编译；在 Windows 上只有 `os::windows` 被编译。如果你无法方便地切换平台，可标注「待本地验证」，转而用第 3 步的阅读方式确认 cfg 互斥关系。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `os/unix.rs` 和 `os/windows.rs` 都能声明一个叫 `local_socket` 的子模块，却不会命名冲突？

> **参考答案**：因为它们分别带 `#[cfg(unix)]` 和 `#[cfg(windows)]`，而这两个条件互斥。任何一次编译中，只有一个 `os::local_socket` 会被编译进来，所以不会冲突。

**练习 2**：`os` 模块是用单独的 `src/os.rs` 文件实现的，还是内联在 `lib.rs` 里的？依据是什么？

> **参考答案**：内联在 `lib.rs` 里。依据是 [src/lib.rs:33-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L33-L40) 直接写了 `pub mod os { ... }` 带花括号的块形式，并且 `git ls-files` 里没有 `src/os.rs` 文件（只有 `src/os/unix.rs` 和 `src/os/windows.rs`）。

**练习 3**：Unix 后端有 `fifo_file`，Windows 后端没有；Windows 后端有 `named_pipe`、`security_descriptor`，Unix 后端没有。这说明了什么？

> **参考答案**：说明每个平台除了「对称的公共后端」（`local_socket`、`unnamed_pipe`）之外，还各自暴露了平台专有的原语。这也呼应了 u1-l1 讲过的「FIFO 文件是 Unix 专有、named pipe 是 Windows 专有」。

## 5. 综合实践

**任务**：对照 `src/lib.rs` 的 `mod` 声明，在纸上画出 interprocess 的顶层模块依赖图，并标出哪些模块只在 Unix 或 Windows 编译。

建议按下面的步骤完成：

1. **列出顶层零件**。读 `src/lib.rs`，把所有 `mod` 分成三类填入下表：

   | 类别 | 模块 |
   | --- | --- |
   | 公共跨平台（`pub mod`） | `bound_util`、`error`、`local_socket`、`unnamed_pipe` |
   | 平台私有（`os` 内、cfg 门控） | `os::unix`、`os::windows` |
   | 内部私有（无 `pub`） | `platform_check`、`macros`、`try_clone`、`atomic_enum`、`misc` |

2. **画依赖箭头**。从「用户」出发，画出大致的调用方向：

   ```
   用户
    │
    ▼
   local_socket / unnamed_pipe（公共 API）
    │  （通过 impmod! 注入）
    ▼
   os::unix/*   或   os::windows/*（平台后端，二选一）
    │
    ▼
   error / bound_util（支撑）   c_wrappers / libc / windows-sys（系统调用）
   ```

3. **标注平台**。在你的图上，给 `os::unix` 整棵子树标上「仅 Unix」，给 `os::windows` 整棵子树标上「仅 Windows」；其余标「跨平台」。

4. **用命令校验**。运行 `git ls-files 'src/**/*.rs' | sort`，把真实文件清单与你的图对照，补全 `os/unix/`、`os/windows/` 下各自的子模块文件。

**完成判据**：你能指着图上任意一个模块，说出它是公共的还是私有的、是跨平台的还是平台专有的，以及它在调用链上处于「壳」还是「芯」的位置。

## 6. 本讲小结

- `src/lib.rs` 是 crate 根，负责总装：声明模块、配置 lint、定义 `ConnectWaitMode`、用 `#[path]` 挂载 `tests/` 下的集成测试。
- 顶层公共跨平台模块有四个：`local_socket`、`unnamed_pipe`、`error`、`bound_util`，其中前两个是通信原语，后两个是支撑性抽象。
- 公共原语模块是「壳」，真正的系统调用在 `os::unix` / `os::windows` 平台后端里；公共层用 `impmod!` 等宏把后端注入进来（细节见 u2-l3）。
- `os` 模块内联在 `lib.rs`，其子模块 `unix`、`windows` 用 `#[cfg(unix)]` / `#[cfg(windows)]` 互斥编译，所以同一次构建只有一个平台后端存在。
- 两个平台后端「对称部分」（`local_socket`、`unnamed_pipe`）名字相同，但各自还带有专有原语（Unix 的 `fifo_file`，Windows 的 `named_pipe`、`security_descriptor`）。
- `platform_check.rs` 在编译期就用 `compile_error!` 拦下不支持的平台，把错误前置。

## 7. 下一步学习建议

本讲只看了「骨架」，还没讲公共 API 是如何把方法调用派发到平台后端的。建议：

- 继续学 **u2-l1（Local Socket 的设计哲学）**：理解 local socket 为什么用「trait + enum」双层设计。
- 再学 **u2-l2（enum dispatch：mkenum 与 dispatch 宏）** 和 **u2-l3（impmod 与平台后端注入）**：看清本讲提到的 `impmod!`、enum dispatch 具体怎么工作——这是贯穿全库的钥匙。
- 想先动手跑一个例子的读者，可以穿插看 **u1-l3（构建、Feature Gate 与示例运行）** 和 **u1-l4（上手第一例）**。
