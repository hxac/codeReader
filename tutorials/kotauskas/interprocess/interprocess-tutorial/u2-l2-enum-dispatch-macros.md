# enum dispatch：mkenum 与 dispatch 宏

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 interprocess 如何用一个声明式宏 `mkenum!` 一次性生成「枚举本体 + `From` 转换 + `Debug` + `Sealed` 封印」这一整套派发所需的脚手架。
- 理解 `dispatch!` 宏如何把一行 `match` 表达式变成对当前平台后端的调用，并看懂它内部那个看起来奇怪的「重新借用」小把戏。
- 读懂 `local_socket::stream::enum` 模块如何把 `mkenum!`、`dispatch!`、`dispatch_read!`/`dispatch_write!`、`multimacro!` 组合起来，产出对外可见的 `Stream`/`Listener`/`RecvHalf`/`SendHalf` 四个枚举类型。
- 自己写出一个最小化的双后端派发示例，并用 `cargo expand` 观察宏展开效果。

## 2. 前置知识

本讲在 [u2-l1 Local Socket 的设计哲学](u2-l1-local-socket-philosophy.md) 的基础上继续。u2-l1 已经确立了核心结论：**local socket 的跨平台抽象采用「trait 定义接口 + enum 做派发」的双层设计**，当前每个平台只有一个后端（Windows 用 named pipe、Unix 用 Unix domain socket），因此单变体枚举的 `match` 会被编译器优化掉，派发**零开销**。

本讲要回答的问题是：这套「enum 派发」在代码层面到底是**怎么**用宏自动生成的？为什么作者不手写每个枚举？

需要你了解的几个 Rust 概念：

- **声明式宏（`macro_rules!`）**：一种用模式匹配做代码模板替换的机制。本讲涉及的宏都是这种。
- **`#[cfg(windows)]` / `#[cfg(unix)]`**：编译期条件编译。同一个枚举，在 Windows 上只保留 `NamedPipe` 变体，在 Unix 上只保留 `UdSocket` 变体——所以运行时只有一条 `match` 分支。
- **`#[macro_use]`**：让某个模块里定义的宏对其**后续**声明的兄弟模块可见（Rust 2018 之前的文本作用域宏可见性机制）。interprocess 正是用它把 `enumdef` 里的宏暴露给 `stream`/`listener`。
- **match ergonomics（匹配工效学）**：当你对一个引用（如 `&Enum`）做 `match` 时，Rust 会自动把绑定模式设为 `ref`/`ref mut`，省得你手写 `&`。理解 `dispatch!` 时会用到。
- **newtype 与枚举的区别**：newtype（如 `struct Foo(Inner)`）只有一种内部类型，转发用 `self.0` 即可；枚举有多种变体，转发必须先 `match` 出来。这两种结构需要**不同**的转发宏，本讲会点明这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/local_socket/enumdef.rs`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs) | **本讲核心**。定义 `mkenum!`（生成枚举及配套实现）和 `dispatch!`（转发方法调用）两个宏。 |
| [`src/local_socket/stream/enum.rs`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs) | **本讲核心**。用上面两个宏装配出 `Stream`、`RecvHalf`、`SendHalf` 三个枚举，并定义 `dispatch_read!`/`dispatch_write!` 为枚举实现 `Read`/`Write`。 |
| [`src/macros.rs`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs) | crate 级宏工具箱，本讲用到其中的 `impmod!`（按平台注入后端模块别名）和 `multimacro!`（把同一个类型一次性喂给多个宏）。 |
| [`src/local_socket/listener/enum.rs`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs) | 对照样例：用同样的宏装配出 `Listener` 枚举，佐证这套机制是通用的。 |
| [`src/local_socket.rs:73-74`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L73-L74) | `#[macro_use] mod enumdef;`——正是这一行让 `enumdef` 里的宏对 `stream`/`listener` 子模块可见。 |
| [`src/misc.rs:19-21`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L19-L21) | `pub(crate) trait Sealed {}`——`mkenum!` 为每个生成的枚举实现的「封印」trait。 |
| [`src/os/unix/local_socket/dispatch_sync.rs`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs) 与 [`src/os/windows/local_socket/dispatch_sync.rs`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs) | 平台后端的「入口函数」`listen()`/`connect()`，负责构造具体的平台类型并**包回**成枚举。 |

## 4. 核心概念与源码讲解

### 4.1 `local_socket::enumdef` 模块：mkenum! 与 dispatch! 宏

#### 4.1.1 概念说明

如果手写一个跨平台枚举，你需要为每个类型（`Listener`、`Stream`、`RecvHalf`、`SendHalf`）重复写：

1. 一个带 `#[cfg(windows)]`/`#[cfg(unix)]` 变体的枚举；
2. 从平台具体类型到枚举的 `From` 实现（方便后端把构造好的对象包回去）；
3. 一个 `Debug` 实现（要能看到内部内容）；
4. 一个 `Sealed` 实现（封印 trait，防止外部实现接口 trait）。

四个枚举 × 四套样板 = 大量重复、且容易抄错。`mkenum!` 就是把这些样板**收敛成一个声明**：你只写一句 `mkenum!(...文档注释... Stream);`，它就替你吐出上面四样东西。

而 `dispatch!` 解决的是另一个问题：枚举的每个方法都要写一个 `match` 把调用转给内部的具体类型。这些 `match` 长得几乎一样，于是 `dispatch!` 把它模板化成一行调用。

> 直觉总结：**`mkenum!` 负责「生成类型」，`dispatch!` 负责「转发调用」**。二者配合，构成 enum dispatch 的全部机巧。

#### 4.1.2 核心流程

`mkenum!(<文档> $nm)` 展开成以下几部分（以 `Stream` 为例）：

```
mkenum!(... Stream);
        │
        ├─► pub enum Stream {
        │       #[cfg(windows)] NamedPipe(np_impl::Stream),
        │       #[cfg(unix)]    UdSocket(uds_impl::Stream),
        │   }
        ├─► impl Sealed for Stream {}          // 封印，禁止外部实现接口 trait
        ├─► impl From<np_impl::Stream> for Stream {...}   // (仅 windows)
        │   impl From<uds_impl::Stream> for Stream {...}  // (仅 unix)
        └─► impl Debug for Stream { ... 用 dispatch! 取内部值 ... }
```

`dispatch!($ty: $nm in $var => $e)` 的展开（以 `Stream`、Windows 为例）：

```
dispatch!(Self: x in self => x.read(buf))
   │
   └─► match self {
           Stream::NamedPipe(arm) => {          // 只剩这一个变体
               let mut _arm2 = arm;             // 重新借用小把戏
               let x = &mut _arm2;
               x.read(buf)                      // 实际调用后端方法
           }
       }
```

由于另一个变体是 `#[cfg(unix)]`，在 Windows 编译时根本不存在，所以这个 `match` 实际上只有一个分支，编译器会把它彻底消解为直接调用——这就是 u2-l1 所说的「零开销」。

#### 4.1.3 源码精读

先看 [`dispatch!` 宏本体（enumdef.rs:1-16）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L1-L16)：

```rust
/// Only dispatches with `&self` and `&mut self`.
macro_rules! dispatch {
    (@$arm:ident $nm:ident $e:expr) => {{
        let mut _arm2 = $arm;
        let $nm = &mut _arm2;
        $e
    }};
    ($ty:ident: $nm:ident in $var:expr => $e:expr) => {{
        match $var {
            #[cfg(windows)]
            $ty::NamedPipe(arm) => dispatch!(@arm $nm $e),
            #[cfg(unix)]
            $ty::UdSocket(arm) => dispatch!(@arm $nm $e),
        }
    }};
}
```

- **主规则**（[enumdef.rs:8-15](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L8-L15)）写法是 `$ty: $nm in $var => $e`，读起来像英语「在 `$var` 里，对类型 `$ty` 做匹配，把内部值绑定为 `$nm`，然后求值 `$e`」。它生成一个 `match`，两个臂分别用 `#[cfg]` 门控，互斥存在。
- **`@` 辅助规则**（[enumdef.rs:3-7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L3-L7)）是那个「重新借用小把戏」。它把 `match` 解构出的 `arm` 先放进临时变量 `_arm2`，再以 `&mut` 借给名字 `$nm`。**为什么多此一举？** 因为这样 `$e`（如 `x.read(buf)` 或 `x.set_nonblocking(v)`）无论外层 `self` 是 `&self` 还是 `&mut self`，都能统一地通过 `$nm` 调用方法，并且避免了部分移动（partial move）等所有权问题。顶部的注释点明了它的适用范围：「只支持 `&self` 和 `&mut self`」。

再看 [`mkenum!` 宏本体（enumdef.rs:18-61）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L18-L61)，核心是枚举本体（[enumdef.rs:21-34](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L21-L34)）：

```rust
pub enum $nm {
    #[cfg(windows)]
    NamedPipe(np_impl::$nm),
    #[cfg(unix)]
    UdSocket(uds_impl::$nm),
}
```

注意内部类型写成 `np_impl::$nm`、`uds_impl::$nm`——它复用宏参数 `$nm`（如 `Stream`）去取后端模块里的同名类型。这要求使用 `mkenum!` 的文件先用 `use ... as np_impl;` / `use ... as uds_impl;` 把后端模块别名引进来（见 4.2.3）。

紧接着它生成 [`Sealed` 实现（enumdef.rs:35）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L35)——`Sealed` 这个 crate 内部 trait 定义在 [`misc.rs:19-21`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L19-L21)，注释写得很清楚：「用作父 trait 时，可阻止其他 crate 实现该 trait」。这是实现「接口 trait 对外封印」的关键。

然后是两个 [`From` 实现（enumdef.rs:36-49）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L36-L49)——这一步至关重要，它让后端的「构造函数」可以把构造好的平台具体类型**直接 `.into()` / `From::from` 包回成枚举**（4.2.3 会看到实际调用点）。

最后是 [`Debug` 实现（enumdef.rs:50-56）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L50-L56)，它本身就在用 `dispatch!` 取出内部值再 `field` 到调试输出里——这是 `dispatch!` 最早的「吃自己的狗粮」用例。

最后两行（[enumdef.rs:58-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L58-L60)）是重载形式：`mkenum!($nm)` 会以空前缀转发给带 `$pref:literal` 的主形式，前缀只影响 `Debug` 输出的字符串（如 `"local_socket::Stream"`）。

#### 4.1.4 代码实践

**实践目标**：用 `dispatch!` 思路写一个最小化的双后端派发示例，亲手感受「一行宏 → 一段 match」的展开。

**操作步骤**：

1. 新建一个独立的 cargo 项目：`cargo new enum-dispatch-demo && cd enum-dispatch-demo`。
2. 把下面的**示例代码**完整粘贴进 `src/main.rs`（注意：`dispatch!` 在 interprocess 里是 crate 私有宏、未对外导出，所以这里我们**复刻**一份简化版到自己的文件里）。
3. 用 cargo feature 模拟两个平台（这样你在同一台机器上就能切换观察两种后端，而不必真的换操作系统）。先编辑 `Cargo.toml`，在末尾加：

   ```toml
   [features]
   mock_unix = []
   mock_windows = []
   ```

   为避免两个 feature 同时启用导致 `match` 有两个分支，建议默认一个也不开，下面用命令行显式指定。

```rust
// 示例代码：最小化 enum dispatch 演示（独立 crate，可直接运行）
// 复刻 interprocess 的 dispatch! 思路：用 cfg 互斥的两个 mock 后端 + match 转发

// --- 复刻 dispatch! 宏（与 interprocess 内部等价的简化版）---
macro_rules! dispatch {
    (@$arm:ident $nm:ident $e:expr) => {{
        let mut _arm2 = $arm;
        let $nm = &mut _arm2;
        $e
    }};
    ($ty:ident: $nm:ident in $var:expr => $e:expr) => {{
        match $var {
            #[cfg(feature = "mock_windows")]
            $ty::NamedPipe(arm) => dispatch!(@arm $nm $e),
            #[cfg(feature = "mock_unix")]
            $ty::UdSocket(arm) => dispatch!(@arm $nm $e),
        }
    }};
}

// --- 两个 mock 后端 ---
#[cfg(feature = "mock_windows")]
pub mod np_impl {
    pub struct Stream;
    impl Stream {
        pub fn describe(&self) -> &'static str { "Windows named pipe 后端" }
    }
}
#[cfg(feature = "mock_unix")]
pub mod uds_impl {
    pub struct Stream;
    impl Stream {
        pub fn describe(&self) -> &'static str { "Unix domain socket 后端" }
    }
}

// --- 用宏生成跨平台枚举（手工简化版的 mkenum）---
pub enum Stream {
    #[cfg(feature = "mock_windows")]
    NamedPipe(np_impl::Stream),
    #[cfg(feature = "mock_unix")]
    UdSocket(uds_impl::Stream),
}

impl Stream {
    pub fn describe(&self) -> &'static str {
        // 关键：一行 dispatch! 把调用转发给当前启用后端的 describe
        dispatch!(Stream: x in self => x.describe())
    }
}

fn main() {
    #[cfg(feature = "mock_windows")]
    let s = Stream::NamedPipe(np_impl::Stream);
    #[cfg(feature = "mock_unix")]
    let s = Stream::UdSocket(uds_impl::Stream);
    println!("派发结果：{}", s.describe());
}
```

4. 运行两种后端，分别观察输出：

   ```bash
   cargo run --features mock_windows
   cargo run --features mock_unix
   ```

**需要观察的现象**：两次运行分别打印「Windows named pipe 后端」和「Unix domain socket 后端」。这说明同一个 `s.describe()` 调用，在不同 `cfg` 下被派发到了不同的内部实现。

**（可选）观察宏展开**：如果你装了 `cargo-expand`（`cargo install cargo-expand`），运行

```bash
cargo expand --features mock_unix | less
```

在展开结果里找到 `impl Stream` 的 `describe` 方法，你会看到 `dispatch!(...)` 已经变成了一段只有一个 `UdSocket(arm)` 分支的 `match`，且内部带着 `let mut _arm2 = arm; let x = &mut _arm2;` 那段借用小把戏——和本讲 4.1.2 的展开图完全一致。

> 若未安装 `cargo-expand`，不影响理解；上述运行结果已足够说明派发行为，宏展开属于加深印象的可选步骤。

#### 4.1.5 小练习与答案

**练习 1**：`dispatch!` 顶部的注释说「Only dispatches with `&self` and `&mut self`」。如果某个后端方法需要按值消费 `self`（接收 `self`），当前的 `dispatch!` 还能用吗？为什么？

**答案**：不能直接用。`@` 辅助规则里 `let $nm = &mut _arm2;` 总是给 `$nm` 一个引用，无法表达「把内部值整体 move 出来」。要支持按值消费，需要另写一个不借用、直接移动 `arm` 的变体。这也解释了为什么 interprocess 里**销毁性**操作（如 `split`，见 4.2.3）没有用 `dispatch!`，而是手写 `match`。

**练习 2**：`mkenum!` 生成的 `From<np_impl::Stream> for Stream` 实现有什么实际用途？如果没有它，哪一段代码会编译不过？

**答案**：它让后端的构造函数能用 `.map(Stream::from)` 或 `.into()` 把构造好的平台具体类型包回成跨平台枚举。例如 [`os/unix/local_socket/dispatch_sync.rs:12-14`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs#L12-L14) 的 `connect` 函数，返回值就是用 `.map(Stream::from)` 包装的。没有这个 `From`，那一行将无法编译。

### 4.2 `local_socket::stream::enum` 模块：装配 Stream/RecvHalf/SendHalf

#### 4.2.1 概念说明

`enumdef.rs` 给我们提供了两个「积木」（`mkenum!`、`dispatch!`）。`stream/enum.rs` 这个模块就是**用积木搭房子**的地方：它负责把对外可见的 `Stream`、`RecvHalf`、`SendHalf` 三个枚举真正定义出来，并为它们实现 `traits` 模块里声明的接口（`Stream`、`StreamCommon`、`RecvHalf`、`SendHalf` trait），以及标准库的 `Read`/`Write`/`TryClone`。

这里还出现了两个**新的本地宏** `dispatch_read!` 和 `dispatch_write!`。它们的存在是因为：标准库的 `Read`/`Write` 是 trait，其方法（`read`、`read_vectored`、`write`、`write_vectored`）不能简单地用 `dispatch!` 一次性转发——必须逐个方法地在 `impl Read`/`impl Write` 块里写。这两个宏把这层重复也消除了。

> 和 newtype 转发宏的对照：interprocess 在 [`src/macros/forward_iorw.rs`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs) 里还有一套 `forward_sync_read!` 等「newtype 转发宏」，它们用 `self.0.read(buf)` 直接访问内部字段。**枚举类型用 `dispatch_*`（要 `match`），newtype 包装用 `forward_*`（访问 `.0`）**，这是两套并行的转发体系，别混淆。

#### 4.2.2 核心流程

整个模块的装配顺序可以画成一条流水线：

```
1. 引入平台后端别名：use ... as uds_impl / np_impl;
2. impmod! {local_socket::dispatch_sync}     // 注入后端 connect 入口
3. 定义 dispatch_read! / dispatch_write!      // 为枚举实现 Read/Write 的本地宏
4. mkenum!(... Stream);                        // 生成 Stream 枚举
5. impl trait::Stream / StreamCommon / TryClone for Stream
       └─ 方法体里用 dispatch!(...) 转发；split/reunite 手写 match
6. multimacro! { Stream, dispatch_read, dispatch_write }  // 批量挂上 Read/Write
7. mkenum!(... RecvHalf);  dispatch_read!(RecvHalf);      // 收端半
8. mkenum!(... SendHalf);  dispatch_write!(SendHalf);     // 发端半
```

#### 4.2.3 源码精读

**步骤 1-2：后端别名与入口注入**。文件开头用 `cfg` 引入后端模块别名（[`stream/enum.rs:1-4`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L1-L4)）：

```rust
#[cfg(unix)]  use crate::os::unix::uds_local_socket as uds_impl;
#[cfg(windows)] use crate::os::windows::named_pipe::local_socket as np_impl;
```

这两个别名正是 `mkenum!` 里 `np_impl::$nm`、`uds_impl::$nm` 要找的名字。接着 [`impmod! {local_socket::dispatch_sync}`（stream/enum.rs:17）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L17) 把平台后端的 `dispatch_sync` 模块（含 `connect` 函数）注入进来，`impmod!` 的内部机制见 4.3.3。

**步骤 3：`dispatch_read!` / `dispatch_write!`**。以 [`dispatch_read!`（stream/enum.rs:19-38）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L19-L38) 为例：

```rust
macro_rules! dispatch_read {
    (@iw $ty:ident) => {
        #[inline] fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            dispatch!($ty: x in self => x.read(buf))
        }
        #[inline] fn read_vectored(&mut self, bufs: &mut [IoSliceMut<'_>) ...) { ... }
    };
    ($ty:ident) => {
        impl Read for &$ty { dispatch_read!(@iw $ty); }
        impl Read for $ty  { dispatch_read!(@iw $ty); }
    };
}
```

它对给定类型 `$ty` **同时**为 `&$ty` 和 `$ty` 实现 `Read`（这意味着 `Stream` 既可按值、也可按引用读写），每个方法体都是一句 `dispatch!(...)`。`dispatch_write!`（[`stream/enum.rs:39-64`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L39-L64)）同理，并额外把 `flush` 写成「永远是成功的空操作」。

**步骤 4：生成 `Stream` 枚举**。一句 [`mkenum!(... Stream)`（stream/enum.rs:66-78）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L66-L78) 就吐出了 4.1.2 描述的全部脚手架。

**步骤 5：为 `Stream` 实现接口 trait**。见 [`impl r#trait::Stream for Stream`（stream/enum.rs:80-131）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L80-L131)。大多数方法是一行 `dispatch!`，例如：

```rust
fn set_nonblocking(&self, nonblocking: bool) -> io::Result<()> {
    dispatch!(Self: x in self => x.set_nonblocking(nonblocking))
}
```

而 `from_options` 则把构造委托给后端入口函数（[`stream/enum.rs:84-87`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L84-L87)）：

```rust
fn from_options(options: &ConnectOptions<'_>) -> io::Result<Self> {
    dispatch_sync::connect(options)
}
```

注意这里形成了**闭环**：枚举的 `from_options` → 调用平台 `dispatch_sync::connect` → 后端构造具体平台类型 → 用 `mkenum!` 生成的 `From` 把它包回成枚举（见 [`os/windows/local_socket/dispatch_sync.rs:12-14`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L12-L14) 的 `.map(Stream::from)`）。

**为何 `split`/`reunite` 不用 `dispatch!`？** 因为它们要按值消费 `self` 并产生**两个**不同的枚举（`RecvHalf`、`SendHalf`），`dispatch!` 的「只转发 `&self`/`&mut self`」机制不够用，于是手写 `match`。见 [`split`（stream/enum.rs:103-116）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L103-L116) 与 [`reunite`（stream/enum.rs:117-130）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L117-L130)。`reunite` 里那条 `#[allow(unreachable_patterns)] (rh, sh) => Err(...)` 是兜底：当收/发两半来自不同后端时返回 `ReuniteError`（该兜底永远走不到，但模式检查需要它）。

**步骤 6：批量挂 `Read`/`Write`**。[`multimacro! { Stream, dispatch_read, dispatch_write }`（stream/enum.rs:145-149）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L145-L149) 一行展开成 `dispatch_read!(Stream); dispatch_write!(Stream);`——`multimacro!` 的细节见 4.3.3。

**步骤 7-8：半双工两半**。[`mkenum!(... RecvHalf)`（stream/enum.rs:151-155）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L151-L155) 后跟 [`dispatch_read!(RecvHalf)`（stream/enum.rs:165）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L165)，只挂读；`SendHalf`（[`stream/enum.rs:167-181`）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L167-L181) 只挂写。这正是「收端半只能读、发端半只能写」的类型层体现。

**对照样例：`Listener`**。`listener/enum.rs` 用**完全相同**的模式：[`impmod! {local_socket::dispatch_sync as dispatch}`（listener/enum.rs:11）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L11) 注入后端、[`mkenum!(... Listener)`（listener/enum.rs:13-55）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L13-L55) 生成枚举，`accept` 用 `dispatch!` 并 `.map(Stream::from)`（[`listener/enum.rs:64-67`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L64-L67)）。这说明 `mkenum!`/`dispatch!` 是一套**通用、可复用**的机制，不专属于 `Stream`。

#### 4.2.4 代码实践

**实践目标**：作为「源码阅读型实践」，跟踪一次真实调用的展开路径，确认你理解了 `dispatch_read!` 与 `dispatch!` 的配合。

**操作步骤**：

1. 打开 [`src/local_socket/stream/enum.rs:22-24`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L22-L24)（这是 `dispatch_read!` 的 `@iw` 臂里 `read` 方法的模板，`impl Read for Stream` 由它批量生成），其方法体是 `dispatch!($ty: x in self => x.read(buf))`。
2. 在脑中（或纸上）把 `dispatch!(Self: x in self => x.read(buf))` 按 4.1.2 的展开图手写一遍。
3. 回答下面的问题（见「需要观察的现象」）。

**需要观察的现象 / 需要回答**：

- 在 Windows 上，这段代码最终调用的「真实」方法是哪一个类型上的 `read`？
- `read` 方法接收 `&mut self`，而 `dispatch!` 的注释说支持 `&mut self`——请确认 `let $nm = &mut _arm2;` 这个绑定对 `x.read(buf)`（`read` 需要 `&mut self`）是否成立。

**预期结果**：Windows 上最终调用的是 `np_impl::Stream`（即 `os::windows::named_pipe::local_socket::Stream`）的 `read`。`x` 是 `&mut _arm2`，其中 `_arm2` 是 `match` 解构出的内部值，`x.read(buf)` 通过可变引用调用成立。**待本地验证**：可用 `cargo expand` 在启用 `tokio`/默认同步特性的编译中确认展开后的 `impl Read for Stream` 确实只剩一个 `NamedPipe` 分支。

#### 4.2.5 小练习与答案

**练习 1**：`dispatch_read!` 同时为 `&$ty` 和 `$ty` 实现 `Read`。这意味着对 `Stream` 而言，`stream.read(...)` 和 `(&stream).read(...)` 都能用。请结合 u2-l1 提到的「异步流通过引用共享读写」思考：为什么 interprocess 要特意为「引用」也实现 `Read`/`Write`？

**答案**：为了支持「多持有者共享同一个流」的场景（例如把一个 `&Stream` 交给多个任务/线程读写），而不必 `clone` 出独立的句柄。`&Stream` 实现 `Read`/`Write` 正是 `bound_util`（u6-l3）那套「把 `&T: Read` 编码进类型系统」机制的下游消费者。

**练习 2**：`split` 方法为什么不能写成 `dispatch!(Self: x in self => x.split())`？

**答案**：因为 `split` 需要按值消费 `self`，并把结果包装成 `(RecvHalf, SendHalf)` 这两个**不同**的枚举；`dispatch!` 只能转发 `&self`/`&mut self` 调用且结果类型单一。所以必须手写 `match`，在每个分支里调用后端的 `s.split()` 再逐个包成 `RecvHalf::NamedPipe(...)` / `SendHalf::NamedPipe(...)`。

### 4.3 `macros` 模块：impmod! 与 multimacro! 装配胶水

#### 4.3.1 概念说明

`macros.rs` 是 crate 级的宏工具箱。本模块（`stream::enum`）只用到了其中两个，但它们是 4.2 装配流水线里「后端注入」和「批量挂宏」这两步的幕后功臣：

- **`impmod!`**：把平台后端模块（`os::unix` 或 `os::windows` 下的某个路径）以**统一别名**引入，让公共层代码完全不必写 `#[cfg]`。
- **`multimacro!`**：把同一个类型一次性喂给多个宏，省掉重复写类型名。

这两个宏本身不属于 enum dispatch 的核心逻辑，但它们是让 4.2 的代码「看起来干净」的关键胶水。完整的宏系统全景留待 [u7-l1 宏系统全景](u7-l1-macro-system.md) 讲解。

#### 4.3.2 核心流程

`impmod!` 的思路是「一个名字，两个 `#[cfg]` 来源」：

```
impmod! {local_socket::dispatch_sync}
    │
    ├─► #[cfg(unix)]   use crate::os::unix::local_socket::dispatch_sync::{...};
    └─► #[cfg(windows)] use crate::os::windows::local_socket::dispatch_sync::{...};
```

`multimacro!` 的思路是「类型在前、宏名在后，循环展开」：

```
multimacro! { Stream, dispatch_read, dispatch_write }
    │
    ├─► dispatch_read!(Stream);
    └─► dispatch_write!(Stream);
```

#### 4.3.3 源码精读

**`impmod!`**（[`macros.rs:4-14`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L4-L14)）：

```rust
macro_rules! impmod {
    ($($osmod:ident)::+ $(as $into:ident)?) => {
        impmod!($($osmod)::+, self $(as $into)?);
    };
    ($($osmod:ident)::+, $($orig:ident $(as $into:ident)?),* $(,)?) => {
        #[cfg(unix)]   use $crate::os::unix::$($osmod)::+::{$($orig $(as $into)?,)*};
        #[cfg(windows)] use $crate::os::windows::$($osmod)::+::{$($orig $(as $into)?,)*};
    };
}
```

它的「主规则」用两个 `#[cfg]` 各导出一份 `use`，区别只在 `os::unix`/`os::windows` 这一段。`as $into` 允许重命名，例如 listener 那里写 `impmod! {local_socket::dispatch_sync as dispatch}`，就把 `dispatch_sync` 模块别名为 `dispatch`（[`listener/enum.rs:11`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L11)），于是后面能写 `dispatch::listen(options)`。`self` 这个名字表示「把整个模块引入」，对应 `use ...::{self}`。

> 衔接 u2-l3：`impmod!` 的完整用法（如何注入后端实现类型、如何与 `multimacro!` 配合做转发）会在 [u2-l3 impmod 与平台后端注入](u2-l3-impmod-backend-injection.md) 深入展开。本讲只关注它为 enum dispatch 提供的「入口注入」这一面。

**`multimacro!`**（[`macros.rs:33-46`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L33-L46)）：

```rust
macro_rules! multimacro {
    ($pre:tt $ty:ident, $($macro:ident $(($($arg:tt)+))?),+ $(,)?) => {$(
        $macro!($pre $ty $(, $($arg)+)?);
    )+};
    // ... 另外三个重载分别处理 $ty:ty、带/不带前缀的组合 ...
}
```

核心是 `$($macro:ident ...)+` 这个重复片段：每个 `macro` 名都被展开成一次 `$macro!($ty ...)`。`multimacro! { Stream, dispatch_read, dispatch_write }` 因此等价于分别调用两个宏。它还支持给个别宏传额外参数（用括号包裹），所以能统一调度 `forward_*`、`dispatch_*` 等需要不同参数的宏。

**`mkenum!`/`dispatch!` 如何对 `stream`/`listener` 可见？** 关键在 [`local_socket.rs:73-74`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L73-L74) 的 `#[macro_use] mod enumdef;`——`#[macro_use]` 让 `enumdef` 里定义的宏对该模块**之后**声明的子模块（`stream`、`listener`）在文本作用域内可见。这正是为什么 `stream/enum.rs` 能直接调用 `mkenum!` 和 `dispatch!` 而无需显式 `use`。

#### 4.3.4 代码实践

**实践目标**：用 `multimacro!` 的思路，体会「批量挂宏」如何减少重复。

**操作步骤**：

1. 在 4.1.4 的示例项目里，给你的 `dispatch!` 旁边再加一个只读、只写的 mock 方法（如 `describe_read_only` / `describe_write_only`），然后定义两个本地宏 `impl_read!(T)` 和 `impl_write!(T)`（内容可以只是 `impl T { fn marker_read(&self){} }` 之类）。
2. 仿照 interprocess，自己写一个简化版 `multimacro!`，把 `Stream` 一次性喂给这两个宏，确认只写一行就能挂上两个实现。

**需要观察的现象**：用 `cargo expand` 确认一行 `multimacro!` 展开成了两个 `impl` 块。

**预期结果**：展开结果里出现两个独立的 `impl Stream` 块，分别来自两个子宏。**待本地验证**（需 `cargo-expand`）。

#### 4.3.5 小练习与答案

**练习 1**：`impmod!` 用 `#[cfg(unix)]`/`#[cfg(windows)]` 生成两份 `use`。如果有人误把后端模块放到了 `os::linux` 下，这套机制还能工作吗？

**答案**：不能。`impmod!` 硬编码了 `os::unix` 和 `os::windows` 两个前缀，只认这两类平台。若要支持新平台（如纯 Linux 专有原语），需要同时修改 `impmod!`、`mkenum!`（增加新变体）和后端目录结构——这正是 [u9-l4 平台探测与二次开发扩展点](u9-l4-platform-check-extension.md) 会讨论的扩展成本。

**练习 2**：为什么 `mkenum!` 和 `dispatch!` 定义在 `enumdef.rs` 而不是 `macros.rs`？

**答案**：因为这两个宏**专属于 local socket 的双后端派发**（硬编码了 `NamedPipe`/`UdSocket` 变体名和 `np_impl`/`uds_impl` 别名），不是通用工具。放在 `local_socket` 模块内部（并通过 `#[macro_use]` 局部可见）既体现了它们的作用域，也避免污染 crate 级的通用宏工具箱 `macros.rs`。

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，完成一次「从用户调用到系统调用」的完整派发链追踪。

请准备一张纸，画出下面这条调用链上的每一步**宏展开**与**实际跳转**，并标注每一步发生在哪个文件：

```
用户代码：stream.read(&mut buf)?
        │
        ▼
① impl Read for Stream 的 read 方法        ← 由 dispatch_read! 生成（stream/enum.rs）
        │   方法体：dispatch!(Self: x in self => x.read(buf))
        ▼
② dispatch! 展开成 match（enumdef.rs）
        │   Windows 分支：Stream::NamedPipe(arm) => { let x = &mut arm; x.read(buf) }
        ▼
③ 调用 np_impl::Stream 的 read（os/windows/named_pipe/local_socket/stream.rs）
        │   （底层最终落到 named pipe 的系统调用）
```

**具体要求**：

1. 写出①处 `dispatch_read!(@iw Stream)` 展开后 `read` 方法体的精确文本。
2. 写出②处在 **Unix** 平台上 `match` 的唯一分支（提示：是 `UdSocket` 还是 `NamedPipe`？内部别名来自 `stream/enum.rs` 的哪一行？）。
3. 追踪一次**构造**的闭环：写出 `Stream::from_options(opts)` → `dispatch_sync::connect` → `Stream::from` 的三跳，指出每跳的源码位置。
4. 最后回答：整条链路在单平台编译后，`match` 的运行时开销是多少？为什么？

**预期结果**（自检）：

1. `#[inline] fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> { dispatch!(Self: x in self => x.read(buf)) }`（见 [`stream/enum.rs:22-24`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L22-L24)）。
2. Unix 上唯一分支是 `Stream::UdSocket(arm) => { let mut _arm2 = arm; let x = &mut _arm2; x.read(buf) }`，别名 `uds_impl` 来自 [`stream/enum.rs:1-2`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L1-L2)。
3. 构造闭环：[`stream/enum.rs:84-87`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L84-L87) 的 `from_options` → [`os/unix/local_socket/dispatch_sync.rs:12-14`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs#L12-L14) 的 `connect`（`.map(Stream::from)`）→ `mkenum!` 生成的 [`From` 实现（enumdef.rs:43-49）](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L43-L49)。
4. 零开销。因为另一个变体是 `#[cfg]` 排除的，`match` 只剩一个分支，编译器会消解掉分支判断，直接内联调用后端方法。

## 6. 本讲小结

- **`mkenum!`**（[`enumdef.rs:18-61`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L18-L61)）用一个声明生成「枚举本体 + `Sealed` + `From`（平台转换）+ `Debug`」全套脚手架，是 enum dispatch 的「类型生成器」。
- **`dispatch!`**（[`enumdef.rs:1-16`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L1-L16)）把一行调用展开成「按 `cfg` 互斥的单分支 `match` + 重新借用小把戏」，是「方法转发器」；它只支持 `&self`/`&mut self`，不支持按值消费（故 `split`/`reunite` 手写 `match`）。
- **`dispatch_read!`/`dispatch_write!`**（[`stream/enum.rs:19-64`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L19-L64)）在 `dispatch!` 之上多包一层，为枚举（及其引用）实现标准库 `Read`/`Write`，与 newtype 用的 `forward_*` 宏形成对照。
- **`stream/enum.rs`** 用 `mkenum!` + `dispatch!` + `dispatch_read!`/`dispatch_write!` + `multimacro!` 装配出 `Stream`/`RecvHalf`/`SendHalf`，构造链经由后端 `dispatch_sync::connect` + `mkenum!` 生成的 `From` 形成**闭环**。
- **`impmod!`**（[`macros.rs:4-14`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L4-L14)）和 **`multimacro!`**（[`macros.rs:33-46`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L33-L46)）是装配胶水；`#[macro_use] mod enumdef;`（`local_socket.rs:73-74`）是让 `mkenum!`/`dispatch!` 局部可见的关键。
- 单平台编译时单变体 `match` 被消解，enum dispatch 对运行时**零开销**——这把 u2-l1 的「设计哲学」落到了具体的宏实现上。

## 7. 下一步学习建议

- 想深入理解 `impmod!` 如何把**后端实现类型**（不只是入口函数）注入公共层、以及 `multimacro!` 如何批量挂载转发宏，请继续学习 [u2-l3 impmod 与平台后端注入](u2-l3-impmod-backend-injection.md)。
- 想看到这些枚举在真实接口（`Listener`、`Stream` trait）里如何被使用、以及 `split`/`reunite` 的所有权语义，进入 [u3 Local Socket 同步 API 实战](u3-l1-listener-options.md)。
- 想系统了解 crate 级宏工具箱（`forward_*`、`derive_*`、`tag_enum`、`builder_setters` 等全套样板消除宏），在专家层见 [u7-l1 宏系统全景：forwarding 与 derive 宏](u7-l1-macro-system.md)。
- 建议结合源码再读一遍 [`src/local_socket.rs:5-23`](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L5-L23) 的模块文档，它会用「enum_dispatch 风格」这句话印证本讲所讲的全部机制。
