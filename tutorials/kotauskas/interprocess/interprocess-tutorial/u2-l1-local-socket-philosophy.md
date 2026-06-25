# Local Socket 的设计哲学

## 1. 本讲目标

本讲是理解整个 interprocess 库的「钥匙」。local socket（本地套接字）是 interprocess 最核心的抽象，而它背后那套「trait 定义接口 + enum 做派发」的设计模式，会反复出现在库的每一个角落。

学完本讲，你应该能够：

- 说清楚 **local socket 不是操作系统自带的通信原语**，而是 interprocess 在更底层原语之上「拼」出来的一个抽象。
- 看懂 **trait 与 enum 的双层分工**：`traits` 模块里的 trait 定义「能做什么」，模块顶层的 `Listener`/`Stream` 等枚举类型负责「把调用转发给当前平台的真实实现」。
- 解释 **prelude 为什么是官方推荐的写法**，以及它用 `as _` 匿名导入解决了什么问题。
- 独立说明 **为什么 interprocess 选择 enum 派发而不是 trait object**，并指出当前每个平台只有一个后端这一事实。

本讲只讲「设计哲学」，不展开具体选项、读写细节和平台实现——那些留给后续单元。

## 2. 前置知识

在进入本讲前，你需要已经具备以下认知（来自前置讲义 u1-l1、u1-l2）：

- **什么是 IPC**：进程间通信（Inter-Process Communication），让两个独立进程互相传递数据。
- **interprocess 提供了哪些原语**：跨平台的 local socket、两端皆有的 unnamed pipe、Unix 专有的 FIFO 文件、Windows 专有的 named pipe。
- **src 的分层**：跨平台公共模块（`local_socket`、`unnamed_pipe`、`error`、`bound_util`）在 `lib.rs` 直接声明；平台私有后端放在 `os::unix` / `os::windows` 下，用 `#[cfg]` 互斥编译，同一份构建里只会有一个后端存在。

下面补充几个本讲会用到的 Rust 概念，初学者可能不熟：

- **trait（特征）**：Rust 里定义「一组方法签名」的接口，类似其他语言的 interface。类型实现（implement）一个 trait，就承诺提供这些方法。
- **enum（枚举）**：Rust 的 enum 远比 C 的 enum 强大，每个变体（variant）可以携带不同类型的数据。例如 `enum Stream { NamedPipe(NamedPipeImpl), UdSocket(UdSocketImpl) }`。
- **trait object（特征对象，`dyn Trait`）**：把「实现了某 trait 的任意类型」藏在一个指针后面，运行时通过虚表（vtable）找到真正的方法，属于**动态派发**。
- **`as _` 导入**：`use Foo as _;` 只把 trait 的**方法**带进作用域，却不引入 trait 的**名字**，从而既能调用方法又不会污染命名空间。
- **对象安全（object safety）**：决定一个 trait 能否被做成 `dyn Trait`。含有「返回 `Self`」「按值接收 `self`」「带关联类型被返回」等特性的 trait 不满足对象安全，无法用 trait object。

## 3. 本讲源码地图

本讲涉及的源码很少，但每一处都是设计关键：

| 文件 | 作用 |
|------|------|
| `src/lib.rs` | crate 根，声明 `local_socket` 公共模块、定义 `os` 模块的 `#[cfg]` 互斥编译。 |
| `src/local_socket.rs` | local socket 模块本体。顶部的**模块文档注释**是理解整个设计哲学的最佳读物；同时还定义了 `traits` 子模块和 `prelude` 子模块。 |
| `src/local_socket/stream/trait.rs` | 定义 `traits::Stream`、`StreamCommon` 等 trait——接口层。 |
| `src/local_socket/listener/trait.rs` | 定义 `traits::Listener`——接口层。 |
| `src/local_socket/enumdef.rs` | 提供 `mkenum` 宏（生成平台枚举）和 `dispatch!` 宏（转发方法调用）——派发机制的发动机。 |
| `src/local_socket/stream/enum.rs` | 用 `mkenum` 生成 `Stream` 枚举，并为它实现 `traits::Stream`，把每个方法真正派发到平台后端——双层设计的连接点。 |

> 提示：`src/local_socket.rs` 里大量出现 `r#enum`、`r#trait` 这样的写法（`r#` 是 Rust 的原始标识符转义），只是为了能拿 `enum`、`trait` 这种关键字当模块名，理解时直接忽略 `r#` 即可。

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：

- `interprocess::local_socket`（local socket 这个抽象本身）
- `interprocess::local_socket::traits`（接口层）
- `interprocess::local_socket::prelude`（推荐用法）

### 4.1 local socket：操作系统里并不存在的原语

#### 4.1.1 概念说明

很多人第一次看到 local socket 会以为它是某种「系统调用」或「内核提供的套接字类型」。这是最大的误解。

interprocess 的模块文档开宗明义地澄清：**local socket 不是操作系统实现的真正 IPC 原语，而是 interprocess 自己构造的一个概念**，它在底层是借助「该平台已有的某个 IPC 原语」来实现的。

- 在 **Windows** 上，local socket 用 **named pipe（命名管道）** 实现。
- 在 **Unix**（Linux、macOS、FreeBSD 等）上，local socket 用 **Unix domain socket（UDS，Unix 域套接字）** 实现。

换句话说，「local socket」是 interprocess 给这两类底层原语起的一个**统一的名字**，让上层代码不必关心自己到底跑在哪个平台。它对外暴露「客户端通过一个名字（文件路径或命名空间里的标识）连到服务端，每个客户端拿到一条私有连接」这样一套语义，至于这条连接底层是 named pipe 还是 UDS，被藏起来了。

为什么要造这层抽象？因为这两类原语在不同平台上能力不同、限制不同，但它们都能提供「本机上、按名字寻址、点对点私密连接」这同一类服务。interprocess 把这一类服务抽象成 local socket，于是你写一份代码就能在两个平台上跑。

#### 4.1.2 核心流程

从「你写的 API 调用」到「底层系统调用」，中间经过的层次可以这样理解：

```text
你的代码
  │  用名字连接 / 监听
  ▼
interprocess::local_socket（抽象层）
  │  Listener / Stream 等枚举类型
  ▼
平台后端（实现层，被 #[cfg] 互斥选择）
  │  Windows: named pipe       Unix: Unix domain socket
  ▼
操作系统系统调用（CreateFile/Connect / connect/bind/accept …）
```

关键点在于：抽象层不自己发系统调用，它只是「壳」；真正干活的是被 `#[cfg]` 选中的那一个平台后端，它是「芯」。

#### 4.1.3 源码精读

模块文档的第一段就定义了 local socket 的语义——客户端通过文件路径或命名空间里的标识访问服务端，每个客户端拿到私有连接：

[src/local_socket.rs:1-4](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L1-L4) — 模块头注释，给出 local socket 的基本语义。

真正点明「非 OS 原语」的是 `## Implementations and dispatch` 这一节：

[src/local_socket.rs:6-7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L6-L7) — 明确写道 local socket 不是 OS 实现的真正原语，而是 interprocess 借助底层原语构造出来的。

接着文档说明，模块里的 `Listener`、`Stream`、`RecvHalf`、`SendHalf` 这些类型其实是枚举：

[src/local_socket.rs:9-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L9-L14) — 这些类型本质上是 `enum_dispatch` 风格的枚举，含所有实现的变体；被派发的类型通过 `traits` 模块里对应的 trait 来「对话」。

文档还强调了一个对你写跨平台程序极其重要的**稳定性承诺**：

[src/local_socket.rs:29-38](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L29-L38) — 选哪种底层原语**只取决于当前平台和所用的 name type**；interprocess 绝不偷偷插入自己的消息分帧或元数据，**你写进去的字节就是对端原封不动收到的字节**。

这条承诺的意义在于：local socket 的可移植 API 可以用来和**不使用 interprocess、甚至不是 Rust 写的**程序通信——例如一个用 C 写的、直接操作 named pipe 或 UDS 的程序。你只要为每个平台选对 name type，字节流就是完全透明的。

#### 4.1.4 代码实践

**实践目标**：亲手验证「local socket 在不同平台由不同底层原语实现」这一结论。

**操作步骤**：

1. 打开 [src/local_socket.rs:5-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L5-L23)，把 `## Implementations and dispatch` 这一节完整读一遍。
2. 对照 `mkenum` 宏生成的枚举变体，确认「Windows = NamedPipe，Unix = UdSocket」：见 [src/local_socket/enumdef.rs:23-33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L23-L33)。
3. 再看 `os` 模块如何用 `#[cfg]` 互斥选择后端：[src/lib.rs:33-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L33-L40)。

**需要观察的现象**：

- `mkenum` 生成的枚举里，`NamedPipe` 变体带 `#[cfg(windows)]`，`UdSocket` 变体带 `#[cfg(unix)]`，二者互斥。
- `os` 模块下 `unix` 和 `windows` 两个子模块同样互斥。

**预期结果**：你会清楚地看到，在任意一次编译里，`Stream` 枚举只会有一个变体存在——这正是后面「当前零开销」说法的根源。

#### 4.1.5 小练习与答案

**练习 1**：如果 interprocess 偷偷在字节流里插入了长度前缀做消息分帧，会破坏什么承诺？

> **答案**：会破坏文档第 29-38 行的稳定性承诺——「你写进去的字节就是对端原封不动收到的字节」。一旦插入元数据，对端如果是用原生 named pipe / UDS 的非 interprocess 程序，就会读到额外的、它无法理解的字节，互通就失败了。

**练习 2**：为什么 interprocess 要把抽象层和平台实现层分开，而不是直接在每个平台模块里各写一套 API？

> **答案**：为了让上层代码（你的业务逻辑）写一份就能在多平台运行。抽象层提供统一的名字和语义，平台差异被封装在底层；如果每平台各写一套 API，使用方就得为每个平台写一份代码，或自己再造一层抽象。

### 4.2 trait 与 enum 的双层设计

#### 4.2.1 概念说明

local socket 要跨平台，意味着同一种东西（比如「流」）在不同平台有不同的具体类型。interprocess 用了一种非常优雅的**双层设计**来解决它：

- **第一层——trait（接口层）**：`traits` 模块里定义了一组 trait，例如 `traits::Stream`、`traits::Listener`。它们只声明「一个 local socket 流应该能做什么」（连接、设置非阻塞、读写、拆分……），**不关心**具体类型是谁。每个平台的真实实现类型都会实现这些 trait。
- **第二层——enum（派发层）**：模块顶层的 `Stream`、`Listener` 是**枚举类型**，它的变体就是各平台的实现类型。这个枚举自己也实现了对应的 trait——实现方式是：收到方法调用后，用一个 `match` 判断自己是哪个变体，再把调用**转发**给内部真正的实现。

文档把这种关系讲得很直白：「被派发的那些类型，是通过 `traits` 模块里对应的 trait 来对话的」。而且文档还特意点了一句：**这种派发当前在所有平台上都是零开销的**，因为每个平台目前只有一个后端（Windows 只有 named pipe、Unix 只有 UDS），单变体枚举的 `match` 会被编译器完全优化掉。

这里有一个容易忽略的前瞻性细节：文档第 18-21 行提到，将来 Windows 可能会引入对 Unix domain socket 的支持（AF_UNIX 登陆 Windows），届时一个平台可能出现多个后端。enum 派发设计之所以「提前」存在，正是为了这一天到来时，公开 API 完全不必改动。

为什么文档把这种 enum 称作「a trait object of sorts（某种意义上的 trait object）」？因为它和 trait object 一样能「在多个实现之间抽象」，区别在于：enum 是**静态派发**（编译期就确定调用哪个实现），trait object 是**动态派发**（运行时查虚表）。这一点会在本讲综合实践里深入展开。

#### 4.2.2 核心流程

以「在一个 `Stream` 上调用 `set_nonblocking(true)`」为例，双层设计的执行路径是：

```text
你的调用：stream.set_nonblocking(true)
  │
  ▼  stream 的类型是枚举 Stream（派发层）
dispatch! 宏展开成 match：
  │   match stream {
  │       Stream::NamedPipe(s) => s.set_nonblocking(true),  // Windows
  │       Stream::UdSocket(s)   => s.set_nonblocking(true),  // Unix
  │   }
  ▼  转发到当前平台后端（实现层，已实现 traits::Stream）
平台后端的 set_nonblocking 真正发起系统调用
```

也就是说，trait 负责**定义** `set_nonblocking` 这个方法签名，enum 负责**派发**这个调用，后端负责**真正执行**。三者各司其职。

派发开销可以用一句直觉概括：当前每平台只有一个变体，`match` 等价于一个「永远只走一支」的分支，编译器会把它连同那一层 enum 包装一起消除，于是你为统一 API 付出的代价是 0。

#### 4.2.3 源码精读

**第一层——trait 接口。** `traits` 模块只是把分散在子模块里的 trait 重新导出集中起来：

[src/local_socket.rs:89-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L89-L101) — `pub mod traits` 把 `Listener`、`ListenerExt`（来自 listener）、以及 stream 模块的所有 trait 集中导出；在启用 tokio 时还导出异步 trait 子模块。

`traits::Stream` 的定义体现了「接口只管能做什么」。注意它的超 trait（supertrait）约束和关联类型：

[src/local_socket/stream/trait.rs:16-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L16-L22) — `Stream` 要求实现者同时满足 `Read + RefRead + Write + RefWrite + StreamCommon`，并带有关联类型 `RecvHalf`、`SendHalf`。

文档注释里那句「makes it a trait object of sorts」就出现在这里——它点明了 enum 实现了 trait、因而可以像 trait object 一样被使用：

[src/local_socket/stream/trait.rs:18-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L18-L21) — 说明实现此 trait 的类型是 `Stream` 枚举的变体，而 enum 本身也实现了该 trait。

`Listener` trait 同样是接口层，注意它带 `Sealed`——这意味着外部 crate **无法**自行实现这些 trait（封印模式），派发边界完全由 interprocess 掌控：

[src/local_socket/listener/trait.rs:11-19](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L11-L19) — `Listener` 的定义，超 trait 含 `Sealed`。

**第二层——enum 派发。** `mkenum` 宏负责「按平台生成枚举」，这正是变体互斥的来源：

[src/local_socket/enumdef.rs:18-34](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L18-L34) — `mkenum` 生成一个含 `NamedPipe`（`cfg(windows)`）与 `UdSocket`（`cfg(unix)`）两个互斥变体的枚举，并为它实现 `Sealed` 与 `From<各后端>`。

真正把「接口」和「派发」连起来的，是 `dispatch!` 宏：它就是一个套着 `#[cfg]` 臂的 `match`：

[src/local_socket/enumdef.rs:2-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L2-L16) — `dispatch!` 宏：取出 `&mut` 引用后 match 变体，把表达式转发给内部实现。

最后看 `Stream` 枚举如何实现 `traits::Stream`——这就是双层设计的合龙之处。每个方法体都只是 `dispatch!(...)`：

[src/local_socket/stream/enum.rs:80-92](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L80-L92) — `impl Stream for Stream`：`from_options` 委托给 `dispatch_sync::connect`，`set_nonblocking`/`set_recv_timeout`/`set_send_timeout` 都用 `dispatch!` 转发。

注意 `from_options` 没有用 `dispatch!`，而是调了 `dispatch_sync::connect`——这是 `impmod!` 宏按平台注入进来的「连接派发函数」（关于 `impmod!` 的机制，详见下一讲 u2-l3）：

[src/local_socket/stream/enum.rs:17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L17) — `impmod! {local_socket::dispatch_sync}` 把平台后端的连接派发函数注入进来。

连 `Read`/`Write` 也是用 `multimacro!` 批量套用 `dispatch_read`/`dispatch_write` 实现的，每个方法同样只是一行 `dispatch!(...)`：

[src/local_socket/stream/enum.rs:145-149](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L145-L149) — 用 `multimacro!` 为 `Stream` 批量生成 `Read`/`Write`（含按值与按引用两套）实现。

#### 4.2.4 代码实践

**实践目标**：用一个最小程序，亲眼确认 `Stream` 是一个**具体的、Sized 的枚举类型**（而非 `dyn Stream` 这样的 trait object），并能调用 trait 方法。

**操作步骤**：

1. 新建一个二进制 crate（或在你已有的项目里），在 `Cargo.toml` 添加依赖：

   ```toml
   [dependencies]
   interprocess = "2"
   ```

2. 写入下面的 `src/main.rs`（**示例代码**，仅用于演示类型关系）：

   ```rust
   use interprocess::local_socket::prelude::*;

   // 参数类型是 LocalSocketStream——它是 prelude 带入的「枚举类型」，
   // 一个具体且 Sized 的类型，而不是 trait object。
   fn show_it_is_concrete(stream: &LocalSocketStream) {
       // take_error 是 StreamCommon 的方法，由 prelude 用 `as _` 匿名带入
       let _ = stream.take_error();
   }

   fn main() -> std::io::Result<()> {
       // to_ns_name 来自 ToNsName trait，同样由 prelude 用 `as _` 带入
       let name = "example.sock".to_ns_name();
       // connect 是 traits::Stream 的方法，作用在枚举类型 LocalSocketStream 上
       let stream = LocalSocketStream::connect(name)?;
       show_it_is_concrete(&stream);
       Ok(())
   }
   ```

3. 运行 `cargo check`。

**需要观察的现象**：

- `cargo check` 能通过类型检查，说明 `LocalSocketStream` 是一个可直接当函数参数类型、能调用 `.connect`/`.take_error` 的具体类型。
- 注意：`show_it_is_concrete` 的参数是**值类型** `&LocalSocketStream`，而不是 `&dyn Stream`。这正是 enum 派发区别于 trait object 的直观体现。

**预期结果**：编译通过。真正运行（`cargo run`）需要一个正在监听的服务端（见 u1-l4 的回显示例），否则 `connect` 会失败——本实践只需 `cargo check` 通过即可验证设计论断。若无法本地运行，请标注「待本地验证连接行为」。

#### 4.2.5 小练习与答案

**练习 1**：`traits::Stream` 这个 trait 和模块顶层的 `Stream` 这个枚举，二者是什么关系？

> **答案**：前者是**接口**，定义「流能做什么」；后者是**派发类型**，它的变体是各平台实现，且这个枚举自己也 `impl` 了 `traits::Stream`，方法体里用 `dispatch!` 把调用转发给内部实现。使用方拿到的是枚举类型，调用的方法签名来自 trait。

**练习 2**：文档说派发「当前零开销」，依据是什么？未来会不会不再零开销？

> **答案**：依据是当前每个平台只有一个后端，枚举只有一个变体，`match` 只有一支可达，编译器会消除掉这层包装，所以零开销。未来如果某平台出现多个后端（例如 Windows 同时支持 named pipe 和 AF_UNIX），`match` 就会有多支，派发会引入一次比较/跳转——但文档第 20-21 行指出，相比真正的系统调用开销，这点派发开销微不足道。

### 4.3 prelude：用 `as _` 消除命名冲突的推荐用法

#### 4.3.1 概念说明

读到这里你可能会困惑：既然有 `traits::Stream`（trait）和模块顶层 `Stream`（enum）两个同名 `Stream`，那 `Listener`、`ListenerExt`、`NameType`、`ToNsName` 等等又该怎么办？全部写全限定名太啰嗦，全部 `use` 进来又怕名字撞车。

interprocess 给出的官方答案是 `prelude` 子模块。模块文档明确写道：**`use interprocess::local_socket::prelude::*;` 是把 local socket 引入作用域的推荐方式**。

prelude 做了两件巧妙的事：

1. **把 trait 用 `as _` 匿名导入**——只把方法带进作用域，不带进 trait 的名字。这样你能直接写 `name.to_ns_name()`、`stream.take_error()`，却不会让 `ToNsName`、`StreamCommon` 这些名字占用你的命名空间。
2. **把枚举类型重命名带 `LocalSocket` 前缀导入**——`Listener → LocalSocketListener`、`Stream → LocalSocketStream`。这样无论你的项目里是否已有叫 `Listener`/`Stream` 的类型，都不会冲突。

`as _` 是理解 prelude 的核心。它利用了 Rust 的一个特性：trait 方法能否被调用，取决于「trait 是否在作用域内」，而不是「trait 的名字是否可见」。`as _` 让 trait 处于「在作用域内但不占名字」的状态，正好满足需求。

#### 4.3.2 核心流程

使用 prelude 时，作用域里实际多了什么，可以用一张表概括：

| 来源 | 导入方式 | 作用域里可见的名字 | 你能做什么 |
|------|----------|--------------------|------------|
| 各 trait（`Stream`、`Listener`、`NameType`…） | `as _` | **不可见**（只激活方法） | 直接调用 `.connect()`、`.accept()`、`.to_ns_name()` 等方法 |
| 枚举类型 `Listener`、`Stream` | 重命名为 `LocalSocketListener`、`LocalSocketStream` | `LocalSocketListener`、`LocalSocketStream` | 用它们当具体类型（函数参数、变量标注） |

对比之下，如果你**不**用 prelude 而是直接 `use interprocess::local_socket::*;`，你会同时拿到 trait 名和 enum 名，写 `Stream` 时容易指代不清，也可能和你自己的 `Stream` 类型撞名。prelude 正是为消除这些麻烦而生。

#### 4.3.3 源码精读

prelude 的定义就集中在模块底部，短短几行：

[src/local_socket.rs:114-122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L114-L122) — `pub mod prelude`：trait 一律 `as _` 匿名导入；`Listener`/`Stream` 重命名为 `LocalSocketListener`/`LocalSocketStream`。

它的文档注释把设计意图说得很清楚——「以不污染作用域的方式重新导出 trait，并给 enum 派发类型加上 `LocalSocket` 前缀」：

[src/local_socket.rs:114-115](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L114-L115) — prelude 的文档注释，点明「不污染作用域」与「`LocalSocket` 前缀」两个意图。

而模块文档正文里那句官方推荐语，就对应这里的实现：

[src/local_socket.rs:25-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L25-L27) — 明确推荐 `use interprocess::local_socket::prelude::*;` 作为引入 local socket 的方式。

#### 4.3.4 代码实践

**实践目标**：对比「用 prelude」与「不用 prelude」两种写法，体会 `as _` 带来的差别。

**操作步骤**：

写两份等价的 `src/main.rs`（**示例代码**），都用 `cargo check` 验证。

写法 A（推荐，用 prelude）：

```rust
use interprocess::local_socket::prelude::*;

fn main() -> std::io::Result<()> {
    let name = "example.sock".to_ns_name();          // 方法直接可用
    let _listener = LocalSocketListener::options()   // 枚举类型带前缀
        .name(name.clone())
        .create_sync()?;
    let _stream = LocalSocketStream::connect(name)?;
    Ok(())
}
```

写法 B（不用 prelude，手动导出）：

```rust
use interprocess::local_socket::{
    traits::{Listener as _, Stream as _, StreamCommon as _, NameType as _, ToNsName as _},
    ListenerOptions, Listener, Stream,
};
use interprocess::local_socket::name::GenericNamespaced;

fn main() -> std::io::Result<()> {
    let name = "example.sock".to_ns_name::<GenericNamespaced>()?;
    let _listener = Listener::options().name(name.clone()).create_sync()?;
    let _stream = Stream::connect(name)?;
    Ok(())
}
```

**需要观察的现象**：

- 写法 A 里你看不到任何 trait 名字，却能调用 `.to_ns_name()`、`.options()`、`.connect()`——因为 prelude 已经用 `as _` 激活了它们。
- 写法 B 你必须手动列出每个要激活的 trait，还要处理 `Listener`/`Stream` 与你项目里同名类型的潜在冲突。

**预期结果**：两份都能 `cargo check` 通过（运行需服务端配合）。你会直观感受到 prelude 大幅减少了样板代码。若本地无法编译运行，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 prelude 用 `NameType as _` 而不是 `use NameType;`？

> **答案**：`as _` 只激活 trait 的方法（让你能调 `.to_ns_name()` 等），却不把 `NameType` 这个名字带进作用域，避免和你自己的类型撞名。如果用普通 `use NameType;`，名字会占用作用域，且对「方法能否调用」没有额外好处。

**练习 2**：如果你在项目里已经有一个自己的 `struct Stream;`，用 prelude 会冲突吗？

> **答案**：不会。prelude 把枚举重命名成了 `LocalSocketStream`，不会直接引入名为 `Stream` 的项，因此你自己的 `Stream` 和 `LocalSocketStream` 可以共存。

## 5. 综合实践

本讲的综合实践是一个**写作任务**（与本讲「设计哲学」的定位一致），要求你把双层设计吃透后，用自己的话把核心取舍讲清楚。

**任务**：写一段 300 字左右的说明，回答两个问题：

1. **为什么 interprocess 选择用 enum（静态派发）而不是 trait object（动态派发）来做 local socket 的派发？** 请至少给出三条理由。
2. **当前每个平台各有几个 local socket 后端？** 这一事实对「派发开销」有什么影响？

**写作提示（先自己想，再对照下面的参考要点）**：

- 从「对象安全」角度想：`traits::Stream` 里有返回 `Self` 的方法（`connect`、`from_options`）和关联类型（`RecvHalf`/`SendHalf`）、按值接收 `self` 的 `split`。这些特性对 `dyn Stream` 意味着什么？
  - 可对照 [src/local_socket/stream/trait.rs:31-34](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L31-L34)（`connect` 返回 `Self`）与 [src/local_socket/stream/trait.rs:60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L60)（`split` 按值返回关联类型）。
- 从「开销」角度想：静态 match vs. 虚表间接调用。
- 从「值语义/堆分配」角度想：enum 内联持有实现 vs. `Box<dyn>` 必须放堆。
- 从「稳定性边界」角度想：文档第 40-71 行提到 enum 刻意**省略**了 raw handle/fd 的访问 trait，目的是什么？

**参考要点（你的说明应覆盖其中至少三条）**：

1. **对象安全限制**：`traits::Stream` 含返回 `Self` 的方法与按值 `self` 的 `split`、以及关联类型，根本**不满足对象安全**，无法构造 `dyn Stream`。trait object 这条路从接口设计上就被排除了，enum 是自然且唯一可行的静态抽象方式。
2. **当前零开销**：每个平台目前只有一个后端（Windows 仅 named pipe、Unix 仅 UDS），枚举单变体，`match` 被编译器消除，派发零开销（见 [src/local_socket.rs:15-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L15-L21)）。即便将来多后端，相比系统调用开销，一次 `match` 也微不足道。
3. **值语义、无堆分配**：enum 直接内联持有后端实现，无需 `Box`；trait object 必须借助指针放堆，多一次间接寻址与潜在分配。
4. **关联类型保真**：enum 让 `split()` 返回具体的 `RecvHalf`/`SendHalf` 枚举，`reunite` 能在类型层面校验两半同源；trait object 会丢失关联类型信息。
5. **稳定性边界**：enum 把「后端集合」收口在内部，变体是实现细节，公开 API 只暴露 trait 方法；刻意省略 raw handle/fd 访问，是为了将来新增后端时不破坏句柄 API（见 [src/local_socket.rs:40-44](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L40-L44)）。

> 提示：不要照抄以上要点，请结合你读过的源码行号，用自己的话组织。能说清「对象安全」和「当前每平台单后端」这两点，就抓住了本题的核心。

## 6. 本讲小结

- **local socket 不是 OS 原语**：它是 interprocess 在 named pipe（Windows）/ Unix domain socket（Unix）之上构造的统一抽象，对外提供「按名字寻址、点对点私密连接」的语义。
- **双层设计**：`traits` 模块的 trait 定义接口（能做什么），模块顶层的 `Listener`/`Stream` 枚举负责派发（把调用转发给当前平台的真实实现），二者由 `mkenum` + `dispatch!` 宏缝合。
- **当前零开销**：每平台只有一个后端，单变体枚举的 `match` 被编译器消除；enum 设计还前瞻性地为「未来某平台出现多后端」留好了不破坏 API 的余地。
- **稳定性承诺**：选哪种底层原语只取决于平台和 name type，interprocess 绝不插入消息分帧或元数据，字节流完全透明，可与非 interprocess 程序互通。
- **prelude 是推荐用法**：用 `as _` 匿名激活 trait 方法、用 `LocalSocket` 前缀导入枚举类型，既方便又不污染作用域。
- **派发边界受控**：trait 带 `Sealed`、enum 刻意省略 raw handle/fd 访问，确保「后端集合」是 interprocess 内部的实现细节。

## 7. 下一步学习建议

本讲建立了「trait 接口 + enum 派发」的心智模型，接下来的学习应顺着这条线索深入：

- **u2-l2 enum dispatch：mkenum 与 dispatch 宏**：动手精读 `enumdef.rs` 里 `mkenum`/`dispatch!` 的宏展开细节，以及 `stream/enum.rs` 如何为枚举实现 `Read`/`Write`。
- **u2-l3 impmod 与平台后端注入**：本讲提到 `from_options` 委托给了 `dispatch_sync::connect`，那正是 `impmod!` 宏的功劳——下一讲解开「公共层如何按平台注入后端」。
- **u2-l4 名称系统：Name 与 NameType**：本讲反复出现「name type」，第四讲专门讲清楚 local socket 的命名抽象。

在进入下一讲前，建议你回头再读一遍 [src/local_socket.rs:1-72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L1-L72) 的模块文档——它是整个库设计哲学的总纲，常读常新。
