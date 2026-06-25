# 上手第一例：local socket 回显通信

## 1. 本讲目标

本讲是 interprocess 的「动手第一课」。我们将逐行精读仓库自带的 local socket 同步示例（一个回显 server / client 对），把它真正跑起来，并理解每一段代码背后的设计意图。

学完本讲，你应当能够：

- 读懂 `prelude` 的导入方式，并解释它为什么是推荐用法；
- 看懂服务端如何用 `ListenerOptions` 创建监听器、用 `incoming()` 驱动主循环；
- 看懂客户端如何用 `Stream::connect` 建立连接；
- 理解在 `BufReader` 包装下用 `get_mut()` 写回的模式；
- 说清楚为什么同步示例要刻意「一端先发、一端先收」，否则会死锁。

本讲只把 local socket 当作**可运行的工具**来用，名称系统的完整内部机制、enum dispatch、平台后端实现都留到后续单元。

## 2. 前置知识

在进入源码之前，先用通俗语言把几个会反复出现的概念讲清楚。

**进程与 IPC。** 一个运行中的程序就是一个进程，每个进程有自己独立的内存。两个进程不能直接读写对方的内存，所以需要操作系统提供「进程间通信（IPC）」机制来交换字节。

**local socket（本地套接字）。** 这不是某个操作系统自带的单一原语，而是 interprocess 自己定义的**抽象**：客户端通过一个「名字」（文件系统路径，或某个命名空间里的标识符）找到服务端，建立一条**点对点、私密**的连接。它的 API 像网络套接字，但只在本机内通信，因此更快、更安全。在 Windows 上它底层由 named pipe 实现，在 Unix 上底层由 Unix domain socket 实现（这点在 [u1-l1](u1-l1-project-overview.md) 已讲过）。

**流（stream）与半双工。** 一条 local socket 连接是一个双向字节流：两端既能读也能写。但在**单线程同步**代码里，一次只能做一件事——要么在读，要么在写。如果两端同时尝试写、谁都不读，写缓冲区被填满后双方都会卡住，这就是下文反复强调的「死锁」。

**builder（构建器）模式。** Rust 里常见的一种 API 风格：先 `XxxOptions::new()` 创建一个默认配置，再用一连串链式方法（每个方法消耗并返回 `self`）逐项设置，最后调用一个 `create_*` / `connect_*` 方法真正产出对象。本讲的 `ListenerOptions` 就是这种风格。

**`BufReader`。** 标准库 `std::io::BufReader` 给一个「裸读写」的对象套一层缓冲，从而能方便地按行读取（`read_line`）。代价是：`BufReader` 只实现了 `Read`，**没有**实现 `Write`，所以想往里写东西时要用 `get_mut()` 拿到内部对象再写。这是本讲一个关键的小技巧。

**trait 方法为什么需要「导入」。** 在 Rust 里，即便一个类型实现了某个 trait，你也得把这个 trait 引入当前作用域，才能调用它的方法。interprocess 用 `prelude` 帮你一次性把这些 trait 以「匿名导入（`as _`）」的方式带入作用域——只借方法、不污染命名空间。

## 3. 本讲源码地图

本讲围绕两个示例文件展开，并辅以它们用到的库内定义：

| 文件 | 作用 |
| --- | --- |
| [examples/local_socket/sync/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs) | 同步服务端示例：创建监听器、循环接收连接、先收后发回显。注册名 `local_socket_sync_server`。 |
| [examples/local_socket/sync/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs) | 同步客户端示例：构造名字、连接、先发后收。注册名 `local_socket_sync_client`。 |
| [src/local_socket.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs) | local_socket 模块根，定义 `prelude`、re-export `ListenerOptions` / `Stream` 等。 |
| [src/local_socket/listener/options.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs) | `ListenerOptions` 构建器：`new` / `name` / `create_sync` 等。 |
| [src/local_socket/listener/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs) | `Listener` / `ListenerExt` trait 与 `incoming()` 主循环迭代器。 |
| [src/local_socket/stream/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs) | `Stream` trait，定义 `connect`、读写、拆分等接口。 |
| [src/local_socket/name/type.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs) | 名称类型系统：`NameType`、`GenericNamespaced`、`GenericFilePath`。 |

## 4. 核心概念与源码讲解

### 4.1 prelude 导入与名称构造

#### 4.1.1 概念说明

要使用 local socket，第一步永远是两件事：**导入能力**和**构造名字**。

- **导入能力**：interprocess 推荐用 `use interprocess::local_socket::prelude::*;`。`prelude` 把若干 trait 以匿名方式（`as _`）带入作用域，让枚举类型上的方法（`connect`、`incoming`、`to_ns_name` 等）可以被调用；同时把具体枚举类型以带 `LocalSocket` 前缀的名字导出，避免与别的同名类型冲突。
- **构造名字**：客户端和服务端必须就「在哪个名字上通信」达成一致。本例用 `"example.sock".to_ns_name::<GenericNamespaced>()?` 把一个字符串转换成跨平台的 `Name`。`GenericNamespaced` 是一个「不可实例化的标记类型（uninhabited tag type）」，它只用来在类型层面指定「我想要哪种命名方案」，本身不占用任何值。

`GenericNamespaced` 的跨平台映射规则是理解名字系统的钥匙：

| 平台 | `GenericNamespaced` 映射到 |
| --- | --- |
| Windows | 在名字前加 `\\.\pipe\`，变成一个本地命名管道名 |
| Linux | 使用内核的「抽象命名空间」（abstract namespace），最多 107 字节 |
| 其它 Unix | 在名字前加 `/tmp/`，变成文件系统路径 |

这意味着：**同一份 `"example.sock"` 字符串，在不同平台上会落到完全不同的底层地址，但 API 完全一致。** 这正是 local socket 抽象的价值。

#### 4.1.2 核心流程

客户端构造名字的流程（见 [stream.rs:9-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L9-L13)）：

```text
判断 GenericNamespaced::is_supported()
  ├─ 支持  → "example.sock".to_ns_name::<GenericNamespaced>()
  └─ 不支持 → "/tmp/example.sock".to_fs_name::<GenericFilePath>()
```

服务端更简单，直接用命名空间名字（见 [listener.rs:9-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L9-L10)）。

#### 4.1.3 源码精读

先看两个示例的导入：

[examples/local_socket/sync/listener.rs:4-7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L4-L7) 引入了 `prelude::*`、`GenericNamespaced`、`ListenerOptions`，以及标准库的 `BufReader`。

[examples/local_socket/sync/stream.rs:4-7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L4-L7) 在此基础上多了 `GenericFilePath` 和 `Stream`。

`prelude` 到底导出了什么？看库内定义：

[src/local_socket.rs:116-122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L116-L122) 说明 `prelude` 把 `ToFsName`、`ToNsName`、`Listener`、`ListenerExt`、`Stream`、`StreamCommon` 这些 trait 以 `as _` 匿名导入，并把枚举类型 `Listener`、`Stream` 以 `LocalSocketListener` / `LocalSocketStream` 的名字导出。**`as _` 是精髓**：它只把方法引入作用域，不引入类型名，从而既能调用方法、又不会和示例里从 `interprocess::local_socket::Stream` 直接导入的 `Stream` 冲突。

名称构造用到的标记类型定义在：

[src/local_socket/name/type.rs:93-114](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L93-L114) 定义 `GenericNamespaced`，并注明它在各平台的映射规则（Windows 加 `\\.\pipe\`、Linux 用抽象命名空间、其它 Unix 加 `/tmp/`）。

[src/local_socket/name/type.rs:35-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L35-L41) 定义 `NameType` trait 的 `is_supported()` 方法，客户端正是用它来做平台能力判断。

> 提示：目前 `GenericNamespaced` 和 `GenericFilePath` 的 `is_supported()` 都恒为 `true`（见 [type.rs:79-81](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L79-L81) 与 [type.rs:112-114](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L112-L114)），所以示例里 `if GenericNamespaced::is_supported()` 这个分支当前总是走「是」的那条。这是一种防御式写法，演示了「优雅回退到文件路径名」的可移植模式。

#### 4.1.4 代码实践

**目标**：亲手感受 `to_ns_name` 的平台映射。

1. 在示例目录下写一个最小程序（标记为「示例代码」，非仓库原有）：

   ```rust
   use interprocess::local_socket::{prelude::*, GenericNamespaced};
   fn main() -> std::io::Result<()> {
       let name = "example.sock".to_ns_name::<GenericNamespaced>()?;
       println!("namespaced? {} path? {}", name.is_namespaced(), name.is_path());
       Ok(())
   }
   ```

2. 分别想象（或在条件允许时实际）在 Linux、其它 Unix、Windows 上运行。
3. **需要观察的现象**：`is_namespaced()` / `is_path()` 的输出会因平台而异。
4. **预期结果**：Linux 上 `is_namespaced()` 为真、`is_path()` 为假（抽象命名空间）；其它 Unix 上两者都可能为真（`/tmp/` 路径既是路径也算命名空间名）；Windows 上两者都为真（`\\.\pipe\` 既是命名空间也是路径）。若无法本地验证多平台，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prelude` 里要用 `as _` 而不是直接 `pub use ... Listener`？
**答案**：`as _` 只把 trait 的方法引入作用域、不引入类型名，这样既能调用方法，又避免和用户自己 `use` 进来的同名类型（比如直接导入的 `Stream`）发生命名冲突。

**练习 2**：`GenericNamespaced` 是一个能被实例化的普通结构体吗？为什么用它做泛型参数？
**答案**：不是。它是「不可实例化的标记类型（uninhabited tag type）」，只用于在类型层面指定命名方案。编译器靠它的类型身份来选择对应的 `map` 实现，运行时不需要、也无法构造它的值。

---

### 4.2 ListenerOptions 与服务端监听

#### 4.2.1 概念说明

服务端的任务是：在一个名字上「守候」，每当有客户端连过来，就 `accept` 出一条新的连接。interprocess 用 **builder 模式** 来创建监听器：

- `ListenerOptions::new()` 产出一组默认配置；
- `.name(name)` 设置监听的名字；
- `.create_sync()` 真正创建并绑定监听器，返回 `Listener` 枚举。

拿到 `Listener` 后，最常见的主循环写法是 `for conn in listener.incoming() { ... }`：`incoming()` 返回一个**无限迭代器**，每次 `next()` 都阻塞地 `accept` 一条新连接。

#### 4.2.2 核心流程

服务端启动流程：

```text
ListenerOptions::new()
   └─ .name(name)          // 设置名字
        └─ .create_sync()  // -> io::Result<Listener>
             └─ 匹配 AddrInUse：打印提示并退出 / 否则解包得到 listener
                  └─ for conn in listener.incoming() { 处理每条连接 }
```

`incoming()` 的迭代器语义：它内部每次调用都执行 `listener.accept()`，所以 `for` 循环天然就是一个「无限接受连接」的服务器主循环。

#### 4.2.3 源码精读

服务端创建监听器的关键一行：

[examples/local_socket/sync/listener.rs:12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L12) 调用 `ListenerOptions::new().name(name).create_sync()`，并用 `match` 专门捕获 `AddrInUse` 错误。

`AddrInUse` 的处理逻辑见 [listener.rs:13-33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L13-L33)：注释解释了当一个使用「文件型」名字的服务异常退出且没清理 socket 文件时，会留下一个「僵尸 socket」，既连不上也无法被新监听器复用。注释还指出真实程序应改用 `.try_overwrite(true)` 选项来自动替换它（本讲只是给用户打印提示）。

`ListenerOptions` 的构建器定义：

[src/local_socket/listener/options.rs:63-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L63-L78) 是 `new()`，默认开启 `reclaim_name`（drop 监听器时回收名字）。

[src/local_socket/listener/options.rs:82-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L82-L85) 是 `.name()` setter，由 `builder_setters!` 宏生成（宏的细节留到宏系统单元）。

[src/local_socket/listener/options.rs:202-207](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L202-L207) 是 `create_sync()` 与 `create_sync_as()`：前者返回默认的 `Listener` 枚举，后者允许你指定具体的监听器类型。二者最终都调用 `L::from_options(self)`，把构建器派发到平台后端（派发机制留到 [u2](u2-l1-local-socket-philosophy.md) 单元）。

主循环迭代器：

[examples/local_socket/sync/listener.rs:41-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L41-L45) 是服务端主循环：对 `listener.incoming()` 做 `filter_map`（打印失败的连接并跳过）和 `map(BufReader::new)`（给每条连接套上缓冲读）。

[src/local_socket/listener/trait.rs:100-107](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L100-L107) 定义 `ListenerExt::incoming()`。

[src/local_socket/listener/trait.rs:120-125](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L120-L125) 是 `Incoming` 迭代器的 `next()`——每次都返回 `Some(self.listener.accept())`，所以它确实是无限的。

[src/local_socket/listener/trait.rs:23-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L23-L37) 是 `accept()` 的文档，特别提醒：**在 Windows 上，长时间不调用 `accept` 会导致新客户端无法连接**（因为 named pipe「连接即就绪」）。这一点到 [u4-l2/u4-l3](u4-l3-windows-raw-named-pipe.md) 会深入讲解，此处先留个印象。

#### 4.2.4 代码实践

**目标**：运行官方示例，观察 `AddrInUse` 的真实表现。

1. 在终端 A 运行服务端：`cargo run --example local_socket_sync_server`。注意示例名是 `local_socket_sync_server`（在 [Cargo.toml:120-122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L120-L122) 中注册，指向 `listener.rs`），不是文件名。
2. **不要关闭**终端 A，在终端 B 再启动一次同样的服务端。
3. **需要观察的现象**：第二次启动会命中 `AddrInUse` 分支。
4. **预期结果**：终端 B 打印 `Error: could not start server because the socket file is occupied...`，并以错误码退出。这是因为上一个监听器还占着同一个名字（Unix 上是文件系统里的 socket 文件）。
5. 若本地无法运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`incoming()` 返回的迭代器会在什么时候自然结束？
**答案**：永远不会。它的 `next()` 每次都返回 `Some(accept() 的结果)`，没有 `None` 终止条件，所以 `for` 循环就是一个一直运行的服务主循环。

**练习 2**：示例为什么对 `create_sync()` 的返回值用 `match` 而不是直接 `?`？
**答案**：为了专门识别 `AddrInUse` 这一种错误并给出友好的中文/人类可读提示，其它错误才用 `x => x?` 统一向上抛。直接用 `?` 会丢失这种区分。

**练习 3**：默认创建的 `ListenerOptions` 是否开启了 `reclaim_name`？
**答案**：是。`new()` 默认设置了 `reclaim_name` 位（见 options.rs 第 69 行 `flags: 1 << SHFT_RECLAIM_NAME`），drop 时会回收名字。

---

### 4.3 Stream 连接与客户端

#### 4.3.1 概念说明

客户端的任务简单得多：用同一个名字，调用 `Stream::connect(name)` 建立连接，拿到一条 `Stream`（双向字节流），然后读写即可。

注意示例里 `Stream::connect` 是 **`Stream` trait 上的方法**，所以必须把 `Stream` trait 引入作用域——这正是 `prelude` 帮你做的事。`connect` 本质上是 `ConnectOptions::new().name(name).connect_sync_as::<Self>()` 的简写（见 [stream/trait.rs:31-34](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L31-L34)），所以客户端也有对应的 builder 版本，只是简单场景下用 `connect` 更顺手。

#### 4.3.2 核心流程

```text
Stream::connect(name)          // 阻塞连接，失败立即返回 Err
   └─ 得到 Stream
        └─ BufReader::new(...)  // 套缓冲读
             └─ conn.get_mut().write_all(...)  // 先发
                  └─ conn.read_line(...)        // 后收
```

注释里有一句关键提醒：`Stream::connect` 在服务端尚未启动时会**立即失败**（见 [stream.rs:17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L17) 上方注释 "Will fail immediately if the server hasn't started yet."）。所以一定要先启动服务端，再启动客户端。

#### 4.3.3 源码精读

客户端连接与名字构造：

[examples/local_socket/sync/stream.rs:9-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L9-L13) 用 `GenericNamespaced::is_supported()` 选择命名空间名或文件路径名。

[examples/local_socket/sync/stream.rs:18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L18) 用 `BufReader::new(Stream::connect(name)?)` 一步完成「连接 + 套缓冲」。

`Stream` trait 的接口定义：

[src/local_socket/stream/trait.rs:22-34](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L22-L34) 声明 `Stream: Read + RefRead + Write + RefWrite + StreamCommon`，并定义 `connect()`。注意它同时要求 `&Self` 也能读写（`RefRead` / `RefWrite`），这意味着把流放进 `Arc` 共享也能读写——这是后续异步单元会用到的重要特性。

[src/local_socket/stream/trait.rs:78-96](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L78-L96) 是 `StreamCommon`，提供 `take_error()` 和 `peer_creds()`，本讲的同步示例没有用到它们，但它们是真实程序里诊断连接问题、做对端鉴权时会用到的接口。

#### 4.3.4 代码实践

**目标**：验证「先服务端后客户端」的启动顺序要求。

1. 终端 A 启动服务端：`cargo run --example local_socket_sync_server`。
2. 终端 B 启动客户端：`cargo run --example local_socket_sync_client`（在 [Cargo.toml:123-125](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L123-L125) 中注册，指向 `stream.rs`）。
3. 反过来：**先**单独启动客户端（不启动服务端）。
4. **需要观察的现象**：先启动客户端时连接立即失败。
5. **预期结果**：正常顺序下，终端 A 打印 `Client answered: Hello from client!`，终端 B 打印 `Server answered: Hello from server!`；反向启动时客户端因找不到服务端而立刻报错退出。
6. 若本地无法运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么客户端示例要在 `Stream::connect` 外面再套一层 `BufReader::new`？
**答案**：因为后面要用 `read_line` 按行读取服务端的回复，`read_line` 来自 `BufRead`，需要 `BufReader` 提供缓冲。裸 `Stream` 只实现了 `Read`/`Write`，没有 `BufRead`。

**练习 2**：`Stream::connect(name)` 和 `ConnectOptions` 是什么关系？
**答案**：`connect` 是便捷方法，等价于 `ConnectOptions::new().name(name).connect_sync_as::<Self>()`（见 trait.rs 第 32-34 行）。需要设置更多连接选项（如等待模式）时才直接用 `ConnectOptions`，相关内容在 [u3-l2](u3-l2-connect-options.md) 讲解。

---

### 4.4 BufReader 读写与「先收后发」的死锁规避

#### 4.4.1 概念说明

这是本讲最重要的一节。同步单线程代码里，**收和发不能同时进行**。如果客户端和服务端都「先写后读」，双方的写操作都会把数据塞进操作系统缓冲区；当缓冲区满了，写操作就会阻塞，等待对端把数据读走腾出空间——可对端也在写、也在等，于是双双卡死，这就是死锁。

interprocess 的示例用一个非常简单的约定规避它：**一端先发、另一端先收**。具体地，客户端先 `write_all` 发送、然后 `read_line` 接收；服务端先 `read_line` 接收、然后才 `write_all` 回复。这样在任何时刻都只有一方在写、另一方在读，缓冲区不会同时被两边填满。

另一个贯穿读写的小技巧：`BufReader` 不实现 `Write`，所以写回时要用 `conn.get_mut()` 拿到内部的 `Stream` 再调用 `write_all`。

还有一个细节：local socket **无法跨平台地优雅关闭（shutdown）**，所以示例把换行符 `\n` 当作「一条消息结束」的标志，用 `read_line` 来切分消息边界（见 [stream.rs:23-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L23-L27) 的注释）。

#### 4.4.2 核心流程

时序对齐如下（关键：收发交错，避免同时写）：

```text
时刻    客户端                       服务端
 t1     write_all("Hello...\n")      （阻塞在 read_line，等待）
 t2     （阻塞在 read_line，等待）    read_line 收到 "Hello...\n"
 t3     （继续等待）                  get_mut().write_all("Hello...\n")
 t4     read_line 收到 "Hello...\n"   （处理完毕，drop 连接）
```

若把服务端也改成「先写后读」，则 t1 时刻双方都在 `write_all`，缓冲区填满后双双阻塞 → 死锁。

#### 4.4.3 源码精读

服务端「先收后发」：

[examples/local_socket/sync/listener.rs:46-56](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L46-L56) 的注释明确解释了死锁成因：「因为我们的客户端示例先发，服务端应当先收一行、再发回应。否则，在没有线程或异步的情况下，收发无法同时进行，两端各自等待对方清空发送缓冲，就会死锁」。随后 `conn.read_line(&mut buffer)?` 先收，`conn.get_mut().write_all(b"Hello from server!\n")?` 再发。

[examples/local_socket/sync/listener.rs:58-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L58-L66) 在处理完一条连接后 `drop(conn)` 释放资源，打印收到的内容，并 `buffer.clear()` 清空缓冲——否则下一轮 `read_line` 会把新内容追加在旧内容后面。

客户端「先发后收」：

[examples/local_socket/sync/stream.rs:20-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L20-L27) 先 `conn.get_mut().write_all(...)` 发送，注释点明「`BufReader` 不透传 `Write`，所以用 `get_mut`」；再 `conn.read_line(&mut buffer)?` 接收，注释点明「由于 local socket 无法跨平台 shutdown，我们用换行符当作 EOF，并在读取时校验 UTF-8」。

#### 4.4.4 代码实践

**目标**：亲手制造一次死锁，理解收发顺序的重要性。

1. 复制服务端示例为一份新文件（不修改原示例），把主循环里的两行**对调**：先 `conn.get_mut().write_all(...)`，再 `conn.read_line(...)`。
2. 同时保持客户端仍是「先发后收」。
3. 分别启动改造后的服务端和原客户端。
4. **需要观察的现象**：两端都卡住不动（若缓冲区没满可能短暂看似正常，但数据量大或凑巧时必然卡死）。
5. **预期结果**：出现死锁——双方都阻塞在写或读上，程序不退出。把顺序改回「服务端先收后发」后恢复正常。本实验结果与缓冲区大小、消息长度有关，若未观察到明显卡死，可尝试发送更长的消息，或标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么写回时必须用 `conn.get_mut().write_all(...)` 而不能直接 `conn.write_all(...)`？
**答案**：`conn` 是 `BufReader<Stream>`，`BufReader` 只实现了 `Read`/`BufRead`，没有实现 `Write`。`get_mut()` 借用内部的 `Stream`，它才实现了 `Write`。

**练习 2**：服务端每轮循环结束为什么要 `buffer.clear()`？
**答案**：`read_line` 是把读到的一行**追加**进 `String`，不清空的话下一轮的新消息会拼在旧消息后面，导致显示和解析错误。

**练习 3**：异步版本（见后续 [u6](u6-l1-tokio-integration-overview.md)）为什么不需要这么小心翼翼地安排收发顺序？
**答案**：异步版本用 `try_join!` 让收和发**并发**进行（各自是独立的 future），一端在等读时另一端可以继续写，不会出现「双方同时阻塞在写」的局面，所以不必精心编排收发次序。

---

## 5. 综合实践

把本讲的知识串起来：基于现有回显示例，改造出一个「**客户端发送两个数字、服务端返回求和结果**」的 local socket 程序。你需要同时实现 server 和 client。

下面给出参考实现（**示例代码**，非仓库原有，需自行放入 `examples/` 并在 `Cargo.toml` 用 `[[example]]` 声明后才能用 `cargo run --example` 运行；本练习可先用临时 crate 验证）。

**server（示例代码）**：

```rust
use {
    interprocess::local_socket::{prelude::*, GenericNamespaced, ListenerOptions},
    std::io::{self, prelude::*, BufReader},
};

fn main() -> io::Result<()> {
    let name = "sum.sock".to_ns_name::<GenericNamespaced>()?;
    let listener = ListenerOptions::new().name(name).create_sync()?;
    let mut buf = String::with_capacity(64);

    for mut conn in listener.incoming().filter_map(|c| c.ok()).map(BufReader::new) {
        buf.clear();
        // 先收：客户端发来的 "a b\n"
        conn.read_line(&mut buf)?;

        // 解析两个整数并求和
        let sum: i64 = buf
            .split_whitespace()
            .filter_map(|t| t.parse::<i64>().ok())
            .sum();

        // 后发：把结果写回
        conn.get_mut().write_all(format!("{sum}\n").as_bytes())?;
        drop(conn);
    }
    Ok(())
}
```

**client（示例代码）**：

```rust
use {
    interprocess::local_socket::{prelude::*, GenericFilePath, GenericNamespaced, Stream},
    std::io::{self, prelude::*, BufReader},
};

fn main() -> io::Result<()> {
    let name = if GenericNamespaced::is_supported() {
        "sum.sock".to_ns_name::<GenericNamespaced>()?
    } else {
        "/tmp/sum.sock".to_fs_name::<GenericFilePath>()?
    };

    let mut buf = String::with_capacity(64);
    let mut conn = BufReader::new(Stream::connect(name)?);

    // 先发：两个数字
    conn.get_mut().write_all(b"3 4\n")?;

    // 后收：求和结果
    conn.read_line(&mut buf)?;
    print!("sum = {buf}");
    Ok(())
}
```

**验收要点**：

1. client 与 server 使用**同一个**名字（都叫 `sum.sock`）。
2. server 先 `read_line` 后 `write_all`，client 先 `write_all` 后 `read_line`——遵循本讲的收发顺序约定。
3. client 打印 `sum = 7`。
4. 思考：如果需求改成「服务端先返回结果、客户端再发送」，两端的角色如何对调？为什么仍然不能让两端都「先发」？

## 6. 本讲小结

- `use ...prelude::*;` 是使用 local socket 的推荐起点：它以 `as _` 匿名导入若干 trait 方法，并导出带 `LocalSocket` 前缀的枚举类型，既好用又不污染命名空间。
- 名字用 `"str".to_ns_name::<GenericNamespaced>()?` 构造，`GenericNamespaced` 是不可实例化的标记类型，决定跨平台映射（Windows 加 `\\.\pipe\`、Linux 用抽象命名空间、其它 Unix 加 `/tmp/`）。
- 服务端用 builder：`ListenerOptions::new().name(name).create_sync()` 创建监听器，`for conn in listener.incoming()` 驱动无限主循环；可专门捕获 `AddrInUse`。
- 客户端用 `Stream::connect(name)` 连接，它是 `Stream` trait 的方法，靠 prelude 引入作用域。
- `BufReader` 不实现 `Write`，写回要用 `get_mut()` 拿到内部 `Stream`。
- 同步单线程下必须「一端先发、一端先收」，否则双方同时写满缓冲区会死锁；local socket 无法跨平台 shutdown，故示例用 `\n` 当消息边界。

## 7. 下一步学习建议

你已经能跑通一个 local socket 程序，但对它「为什么能跨平台」还只停留在表面。接下来建议：

1. 学习 [u2-l1 Local Socket 的设计哲学](u2-l1-local-socket-philosophy.md)：理解 local socket 是抽象而非 OS 原语，以及 trait + enum dispatch 的双层设计。
2. 学习 [u2-l4 名称系统：Name 与 NameType](u2-l4-name-system.md)：深入 `Name`、`GenericFilePath`、`GenericNamespaced` 的内部结构与平台映射规则。
3. 若想先把「用」学扎实，可跳到 [u3 Local Socket 同步 API 实战](u3-l1-listener-options.md)，系统学习 `ListenerOptions` 全部选项、`ConnectOptions`、`Stream` 的拆分与重聚。
4. 继续阅读源码：精读 [src/local_socket/listener/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs) 与 [src/local_socket/stream/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs)，把本讲用到的方法在 trait 定义里逐一对照。
