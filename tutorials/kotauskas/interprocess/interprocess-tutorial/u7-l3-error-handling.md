# 错误处理体系

## 1. 本讲目标

interprocess 的错误处理不是「失败就抛一个字符串」，而是围绕一个核心问题设计的：**很多转换会消费掉一个对象的所有权，转换失败时，能不能把原来的对象还给调用方？**

读完本讲，你应当能够：

- 说清 `ConversionError` 为什么同时携带 `details`、`cause`、`source` 三个字段，三者各自代表什么。
- 理解「归还输入所有权」这一设计动机，以及为什么 `source` 字段是 `Option`（库不保证一定归还）。
- 区分两个名字相近但含义不同的东西：`source` **字段**（被归还的输入对象）与 `Error::source()` 方法（链式错误来源，实际返回的是 `cause`）。
- 掌握把 `ConversionError` 转成标准库 `io::Error` 的两条路径（`to_io_error()` 与 `From`），以及它们对 `details` 类型的约束。
- 理解 `ReuniteError` 在拆分/重聚失败时如何原样归还两半所有权，以及 `convert_halves` 在 enum 派发层的作用。

本讲是专家层「内部基础设施」单元的一环，承接 u3-l3（`split`/`reunite` 的使用层）与 u7-l2（句柄/FD 所有权），把视线从「怎么用」收回到「失败时所有权如何流动」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**(1) 所有权与「消费型转换」。** interprocess 的许多类型与标准库、操作系统句柄（Handle）/文件描述符（fd）之间可互转。这类转换往往是**消费型的**：它拿走输入对象的所有权，返回一个新形态的对象。一旦失败，输入对象已经不在调用方手里了。Rust 没有 GC，丢掉一个 `OwnedHandle` 意味着一个操作系统句柄被关闭，调用方再也无法挽回。因此，一个「有良心的」错误类型应当尽量把原对象塞回错误里还回去。

**(2) `io::Error` 是整个 crate 的「最大公约数」。** I/O 操作的最终错误类型是标准库的 `std::io::Error`。interprocess 的专有错误最终大多会被转成 `io::Error` 向上传播（例如 `from_options` 返回 `io::Result<Self>`）。所以专有错误类型必须能优雅地「降级」成 `io::Error`。

**(3) 细节字段（details）是「失败发生在哪一步」。** 有些转换分多个阶段，失败时光给一个 OS 错误码不够，还要说明「这个 OS 错误是在哪一步发生的」。`details` 就是承载这层「上下文标签」的字段。

理解了这三点，再去看 `error.rs` 的结构，就能体会到每一个字段都不是多余的。

## 3. 本讲源码地图

本讲主要涉及以下文件：

| 文件 | 作用 |
|------|------|
| [src/error.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs) | 错误类型的「总装车间」：定义 `ConversionError`、`NoDetails`、`ReuniteError` 及平台别名 `FromHandleError`/`FromFdError`。 |
| [src/local_socket/stream/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs) | `Stream` trait 契约，声明 `reunite` 的签名并定义本层的 `ReuniteResult` 别名。 |
| [src/os/windows/named_pipe/stream/error.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/error.rs) | Windows named pipe 后端的真实用例：定义 details 枚举 `FromHandleErrorKind` 与别名 `FromHandleError`。 |
| [src/os/windows/named_pipe/stream/impl/handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs) | 展示 `ConversionError` 三字段如何被真实填充（`is_server_check_failed_error` 与 `NoMessageBoundaries` 分支）。 |
| [src/local_socket/stream/enum.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs) | 展示 `convert_halves` 如何把后端 `ReuniteError` 桥接成公共 `ReuniteError`。 |
| [src/os/unix/uds_local_socket/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs) | Unix 后端 `reunite` 的实现，展示 `ReuniteError { rh, sh }` 的直接构造。 |

> 说明：本讲引用的所有行号均基于当前 HEAD `ecb9daf`。

## 4. 核心概念与源码讲解

### 4.1 ConversionError：可归还所有权的转换错误

#### 4.1.1 概念说明

`ConversionError<S, E>` 是 interprocess 用于「消费型转换失败」的通用错误类型。它有两个泛型参数：

- `S`：**被消费的输入对象的类型**（例如 `OwnedHandle`、`OwnedFd`，或更一般的任何拥有所有权的值）。失败时，原始对象以 `Option<S>` 的形式被塞进错误还回去。
- `E`：**细节字段的类型**，描述失败发生在哪一步。默认是 `NoDetails`（无细节）。

它的核心思想浓缩在源码顶部那段注释里：转换「consume ownership of one object and return ownership of its new form」（消费一个对象的所有权，返回其新形态的所有权），失败时把原对象还回去会非常有用。但库**保留不还的自由**——这正是 `source` 字段是 `Option<S>` 而非 `S` 的原因。

源码注释甚至坦诚地写明了这个设计妥协的来由：Tokio 的异步类型没有 `.try_clone()`，且只返回 `io::Error`，于是某些转换失败时根本拿不回原对象，只能丢弃。详见 [src/error.rs:9-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L9-L29)。

#### 4.1.2 核心流程

一个 `ConversionError` 的结构可以这样概括：

\[ \text{ConversionError}\langle S, E \rangle = \underbrace{E}_{\text{details: 失败在哪步}} \;\times\; \underbrace{\text{Option}<\text{io::Error}>}_{\text{cause: OS 层原因}} \;\times\; \underbrace{\text{Option}<S>}_{\text{source: 归还的输入对象}} \]

三者的关系是**正交**的，可以任意组合：

- 只有 OS 原因，没有细节，也无法归还对象（如 Tokio 路径）。
- 有细节和被归还的对象，但没有 OS 原因（如「类型不匹配」这种纯语义失败，根本没碰系统调用）。
- 三者齐全（系统调用在某个特定阶段失败，且能归还输入）。

构造时，按「是否需要自定义 details」「是否带 OS cause」「是否归还对象」三个维度选择构造器。`map_source` / `try_map_source` 则用于在错误向上传递的过程中**变换被归还对象的类型**（典型场景：后端把 `OwnedHandle` 还回来，公共层需要把它包成公共类型再还）。

#### 4.1.3 源码精读

结构体定义，三个字段全部 `pub`，调用方可以直接读取/构造：

[src/error.rs:30-38](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L30-L38) —— 定义 `details`、`cause`、`source` 三字段；`E` 默认 `NoDetails`，`cause` 与 `source` 均为 `Option`。

当 `E: Default` 时，提供一组「细节取默认值」的便捷构造器：

[src/error.rs:39-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L39-L55) —— `from_source`（只还对象）、`from_cause`（只有 OS 原因，不还对象）、`from_source_and_cause`（二者皆有），细节一律 `Default::default()`。

当 `E` 任意时，提供需要显式给出 details 的构造器：

[src/error.rs:56-64](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L56-L64) —— `from_source_and_details`、`from_cause_and_details`。

变换被归还对象类型的两个工具方法：

[src/error.rs:65-81](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L65-L81) —— `map_source`（用闭包 `FnOnce(S)->Sb` 转换 `source` 类型，保留 details 与 cause）；`try_map_source`（闭包返回 `Option<Sb>`，返回 `None` 时**丢弃**被归还对象的所有权）。

下表汇总构造与变换的入口：

| 需求 | 用法 | 约束 |
|------|------|------|
| 失败时归还对象，无细节 | `from_source(s)` | `E: Default` |
| 只有 OS 原因，不还对象 | `from_cause(e)` | `E: Default` |
| 还对象 + OS 原因，无细节 | `from_source_and_cause(s, e)` | `E: Default` |
| 自定义细节 + 还对象 | `from_source_and_details(s, d)` | 任意 `E` |
| 自定义细节 + OS 原因 | `from_cause_and_details(e, d)` | 任意 `E` |
| 改变被归还对象类型 | `map_source(\|s\| ...)` | 任意 `E` |
| 改变类型，可能丢弃所有权 | `try_map_source(\|s\| ...)` | 任意 `E` |

> 提示：`map_source` / `try_map_source` 在文档中被标注为「mostly used in the crate's internals」（主要用于内部）。它们是面向消费者与内部 wrapper 的公共工具，负责在错误跨越层边界时同步变换被归还对象的类型。

#### 4.1.4 代码实践

这是本讲的主实践（对应学习任务）：手写构造一个带 `source` 与 `cause` 的 `ConversionError`，用 `map_source` 改变被归还对象的类型，再把它降级成 `io::Error` 并打印。

1. **实践目标**：亲身体会「三字段正交」「`map_source` 变换 source 类型」「`Display` 降级为 `io::Error`」三件事。
2. **操作步骤**：在一个新 cargo 工程里添加 `interprocess` 依赖，写入下面的「示例代码」并运行。

```rust
// 示例代码（非仓库原有代码）
use interprocess::error::{ConversionError, NoDetails};
use std::io;

fn main() {
    // 1) 构造一个带「被归还对象」与「OS 原因」的错误。
    //    这里用 String 模拟「被消费的输入对象」，用 raw os error 2 模拟一个 OS 原因。
    let err: ConversionError<String, NoDetails> = ConversionError {
        details: NoDetails,
        cause: Some(io::Error::from_raw_os_error(2)),
        source: Some("原始输入".to_string()),
    };

    // 2) 读出被归还的对象，确认所有权确实回到了我们手里。
    if let Some(returned) = err.source.as_deref() {
        println!("被归还的输入对象: {returned:?}");
    }

    // 3) 用 map_source 改变被归还对象的类型：String -> usize（取长度）。
    let err: ConversionError<usize, NoDetails> = err.map_source(|s| s.len());

    // 4) 降级为标准库 io::Error 并打印最终消息。
    let io_err: io::Error = err.into();
    println!("最终 io::Error: {io_err}");
}
```

3. **需要观察的现象**：
   - `被归还的输入对象` 应打印出 `"原始输入"`，证明 `source` 字段确实承载了原对象的所有权。
   - 第 3 步后 `err` 的类型从 `ConversionError<String, _>` 变为 `ConversionError<usize, _>`，编译期类型变化印证了 `map_source` 的作用。
4. **预期结果**：`最终 io::Error` 一行的消息主体应来自 `cause`，即 raw os error 2 的描述（如 `No such file or directory (os error 2)`），具体措辞以本地运行结果为准——**待本地验证**。注意：因为 `NoDetails` 的 `Display` 输出为空，消息中不会出现前导的 `: `。
5. 若想确认 `map_source` 真的改变了类型，可故意把第 3 步的类型注解写错（如仍写成 `String`），观察编译器报错。

#### 4.1.5 小练习与答案

**练习 1**：`source` 字段为什么是 `Option<S>` 而不是 `S`？

> **答案**：因为库**保留不归还输入所有权的自由**。绝大多数同步转换会还，但异步（Tokio）路径因为 Tokio 没有 `.try_clone()` 且只返回 `io::Error`，失败时可能拿不回原对象，只能丢弃，故用 `Option` 表达「可能还不回来」。这正是源码注释 [src/error.rs:20-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L20-L23) 坦承的设计妥协。

**练习 2**：`map_source` 与 `try_map_source` 的闭包签名分别是什么？后者比前者多了什么能力？

> **答案**：`map_source` 的闭包是 `FnOnce(S) -> Sb`（必定产出新对象）；`try_map_source` 的闭包是 `FnOnce(S) -> Option<Sb>`（可返回 `None`）。后者在返回 `None` 时**主动丢弃**被归还对象的所有权，用于「变换后该对象不再有意义」的场景。见 [src/error.rs:68](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L68) 与 [src/error.rs:75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L75)。

---

### 4.2 NoDetails、FromHandleError 与 FromFdError：平台别名

#### 4.2.1 概念说明

`ConversionError<S, E>` 的 `E` 默认是 `NoDetails`——一个**无人居住的标记类型**（unit struct），表示「这次失败没有额外的步骤标签」。它派生了全部常用 trait，但 `Display` 实现什么也不写。

真正有用的细节类型由各后端自定义。例如 Windows named pipe 后端定义了 `FromHandleErrorKind` 枚举作为 `E`，区分「无法判断是否服务端」「管道不保留消息边界」两种失败原因。基于此，后端用类型别名把 `ConversionError` 特化为「从句柄/FD 转换失败」的错误：

- Windows：`FromHandleError<E = NoDetails> = ConversionError<OwnedHandle, E>`
- Unix：`FromFdError<E = NoDetails> = ConversionError<OwnedFd, E>`

两者用 `#[cfg]` 互斥，保证每个平台只编译自己那个。这与 u1-l2 讲的「平台后端互斥编译」是同一套机制。

#### 4.2.2 核心流程

类型别名的派生关系如下（`E` 可省略，默认 `NoDetails`）：

\[ \text{FromHandleError}\langle E \rangle \;\equiv\; \text{ConversionError}\langle \text{OwnedHandle},\; E \rangle \quad (\text{仅 Windows}) \]
\[ \text{FromFdError}\langle E \rangle \;\equiv\; \text{ConversionError}\langle \text{OwnedFd},\; E \rangle \quad (\text{仅 Unix}) \]

也就是说，别名只是「把 `S` 钉死成平台句柄类型、把 `E` 留给调用方选」的简写。当某个 `TryFrom<OwnedHandle>` 实现需要返回带细节的错误时，它把 `E` 选成自己的 details 枚举。

#### 4.2.3 源码精读

`NoDetails` 的定义与空 `Display`：

[src/error.rs:135-143](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L135-L143) —— 标记类型，`Display::fmt` 直接返回 `Ok(())`，什么都不输出。

平台别名，注意 `cfg` 门控：

[src/error.rs:145-153](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L145-L153) —— `FromHandleError`（Windows）与 `FromFdError`（Unix），`E` 默认 `NoDetails`。

一个**真实的 details 枚举**——Windows named pipe 后端的 `FromHandleErrorKind`：

[src/os/windows/named_pipe/stream/error.rs:13-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/error.rs#L13-L21) —— `IsServerCheckFailed`（无法判断服务端/客户端）与 `NoMessageBoundaries`（管道不保留消息边界）两个变体。

基于它特化出后端自己的别名：

[src/os/windows/named_pipe/stream/error.rs:38-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/error.rs#L38-L41) —— `FromHandleError = ConversionError<OwnedHandle, FromHandleErrorKind>`，把 details 钉死成具体枚举。

最关键的真实用例——`handle.rs` 中如何把三个字段填满：

[src/os/windows/named_pipe/stream/impl/handle.rs:20-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L20-L26) —— `is_server_check_failed_error` 构造一个三字段齐全的错误：`details = IsServerCheckFailed`、`cause = Some(e)`、`source = Some(handle)`。

另一个分支则展示「有 details、有 source、但无 OS 原因」的组合：

[src/os/windows/named_pipe/stream/impl/handle.rs:77-83](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L77-L83) —— `NoMessageBoundaries` 分支：`cause: None`（这是纯语义失败，没有系统调用错误），但仍 `source: Some(handle)` 归还句柄。

#### 4.2.4 代码实践

1. **实践目标**：确认别名在你当前平台的形态，并理解 details 字段的取值。
2. **操作步骤**：阅读 [src/error.rs:145-153](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L145-L153)，判断你所处平台（Linux/Mac 为 Unix、Windows 为 Windows）哪一个别名会编译。然后在本地工程里写一行：
   ```rust
   // 示例代码（非仓库原有代码）—— Unix 平台
   use interprocess::error::FromFdError;
   let _: FromFdError = interprocess::error::ConversionError {
       details: interprocess::error::NoDetails,
       cause: None,
       source: Some(std::os::unix::io::OwnedFd::from(std::fs::File::open("/dev/null").unwrap())),
   };
   ```
   （Windows 平台则把 `FromFdError`/`OwnedFd` 换成 `FromHandleError`/`OwnedHandle`。）
3. **需要观察的现象**：把别名或 `OwnedFd`/`OwnedHandle` 写成当前平台不支持的那个，编译器会直接报「未定义」错误，印证 `cfg` 门控。
4. **预期结果**：本平台别名可编译，跨平台别名不可编译——**待本地验证**。
5. 进阶：参考 `FromHandleErrorKind`，思考如果 Unix 的 `TryFrom<OwnedFd>` 也想区分多种失败原因，你会如何定义一个 `FromFdErrorKind` 枚举并放进 `FromFdError<FromFdErrorKind>`。

#### 4.2.5 小练习与答案

**练习 1**：`FromHandleError` 与 `FromFdError` 的默认 `E` 是什么？它们为什么用 `cfg` 门控？

> **答案**：默认 `E = NoDetails`。门控是因为 `OwnedHandle` 只存在于 `std::os::windows::io`、`OwnedFd` 只存在于 `std::os::unix::io`，二者所在的模块本身就被平台条件编译。见 [src/error.rs:146-153](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L146-L153)。

**练习 2**：`NoMessageBoundaries` 分支为什么 `cause: None`？这说明 `ConversionError` 的三个字段是什么关系？

> **答案**：「管道不保留消息边界」是纯语义判定（比较 `flags & PIPE_TYPE_MESSAGE`），没有发生系统调用失败，所以没有 OS 原因，`cause` 为 `None`。这说明 `details`/`cause`/`source` 三字段**正交**，可任意组合。见 [src/os/windows/named_pipe/stream/impl/handle.rs:77-83](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream/impl/handle.rs#L77-L83)。

---

### 4.3 Display / Error / to_io_error：如何降级为 io::Error

#### 4.3.1 概念说明

专有错误最终要降级成 `io::Error` 才能融入标准库的 `io::Result` 体系。interprocess 提供两条路径：

- `to_io_error(&self) -> io::Error`：借用 self，把它 `to_string()` 后包进 `io::Error::other(...)`。
- `From<ConversionError<S, E>> for io::Error`：按值消费，内部就是调 `to_io_error`（文档注释提到会丢弃被保留的 fd）。

二者都依赖 `E: Display`。关键在于 `Display` 如何把 `details` 与 `cause` 拼成一句可读消息，这里有一个精巧的细节：当 `details` 为空（如 `NoDetails`）时，**不能**在开头输出一个孤立的 `": "`。interprocess 用一个 `FormatSnooper`（格式化嗅探器）来探测「前面是否已经写过内容」，从而决定是否插入分隔符。

还有一个**极易混淆**的点：`Error` trait 的 `source()` 方法返回的是 `cause` 字段（OS 原因），而**不是**名字相同的 `source` 字段（被归还的输入对象）。这两个「source」含义完全不同。

#### 4.3.2 核心流程

`Display` 的拼接逻辑可表示为：

\[ \text{display}(e) = \begin{cases} \text{details} & \text{cause 为 None} \\ \text{details} \;\|\; \text{": "}\;\|\;\text{cause} & \text{cause 为 Some 且 details 非空} \\ \text{cause} & \text{cause 为 Some 且 details 为空} \end{cases} \]

实现上，先借 `FormatSnooper` 把 `details` 渲染进 formatter，过程中记录「是否写过非空内容」；若写过且存在 `cause`，则在 `cause` 前补 `": "`。

`Error::source()` 的语义则是：

\[ \text{ConversionError::source}() = \text{cause} \quad (\text{而非 source 字段}) \]

#### 4.3.3 源码精读

`to_io_error` 与 `From` 转换，注意二者都只要 `E: Display`：

[src/error.rs:83-90](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L83-L90) —— `to_io_error` 用 `io::Error::other(self.to_string())`；`From` 实现直接转发到它（注释点明会丢弃被保留的 fd）。

`Display` 与 `FormatSnooper` 的配合：

[src/error.rs:94-106](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L94-L106) —— 先用嗅探器写 `details`；若有 `cause` 且已写过内容，插入 `": "` 再写 `cause`。

[src/error.rs:114-133](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L114-L133) —— `FormatSnooper` 包装 formatter，实现 `Write`，只要写过非空串就把 `anything_written` 置真。

`Error` trait 的 `source()`——返回 `cause`：

[src/error.rs:107-110](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L107-L110) —— 注意 `source()` 映射到 `self.cause`，约束为 `S: Debug, E: Error + 'static`。

> 重要陷阱：要让 `ConversionError` 实现 `std::error::Error`，`E` 必须满足 `Error + 'static`。而 `NoDetails` 与前述 `FromHandleErrorKind` 都**没有**实现 `std::error::Error`（它们只实现了 `Display`）。因此在实际代码里，这些错误更常通过 `Display` 降级成 `io::Error`，而不是直接当作 `dyn Error` 使用。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 `FormatSnooper` 的 `": "` 插入行为——有细节和无细节时，`io::Error` 的消息差异。
2. **操作步骤**：在本地工程里构造两个错误，一个 `E = NoDetails`，一个 `E` 用自定义的 `Display` 类型，分别转成 `io::Error` 并打印：

```rust
// 示例代码（非仓库原有代码）
use interprocess::error::{ConversionError, NoDetails};
use std::{fmt, io};

#[derive(Default)]
struct Step; // 自定义 details 类型
impl fmt::Display for Step {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("打开管道实例失败")
    }
}

fn main() {
    let cause = io::Error::from_raw_os_error(13); // EACCES

    // A: 无细节
    let a: ConversionError<String, NoDetails> =
        ConversionError { details: NoDetails, cause: Some(cause), source: Some("in".into()) };
    // B: 有细节
    let b: ConversionError<String, Step> =
        ConversionError { details: Step, cause: Some(cause), source: Some("in".into()) };

    let a: io::Error = a.into();
    let b: io::Error = b.into();
    println!("A (无细节): {a}");
    println!("B (有细节): {b}");
}
```

3. **需要观察的现象**：A 的消息只有 OS 原因（无前导 `: `）；B 的消息是 `打开管道实例失败: <OS 原因>`，中间出现了 `": "`。
4. **预期结果**：A 行不含 `": "`，B 行含 `": "`。具体 OS 原因措辞以本地为准——**待本地验证**。
5. 思考题：把 `Step` 的 `Display` 改成输出空串，B 的消息会退化成什么样？（应与 A 一致，因为嗅探器认为「没写过内容」。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Error::source()` 返回 `cause` 而不是 `source` 字段？这会造成什么混淆？

> **答案**：`Error::source()` 的语义是「链式错误的下层原因」，即 OS 层失败，对应 `cause`。而 `source` **字段**承载的是「被归还的输入对象」，与错误链无关。二者只是恰好同名。混淆它们会误以为「归还的对象能从错误链里取到」。见 [src/error.rs:107-110](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L107-L110)。

**练习 2**：`From<ConversionError<S, E>> for io::Error` 要求 `E` 满足什么约束？`NoDetails` 满足吗？

> **答案**：要求 `E: Display`。`NoDetails` 实现了 `Display`（输出空串），所以满足。注意这比实现 `std::error::Error`（要求 `E: Error + 'static`）要弱。见 [src/error.rs:83-90](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L83-L90)。

**练习 3**：`FormatSnooper` 解决了什么具体问题？

> **答案**：避免在 `details` 为空时，消息以一个孤立的 `": "` 开头（即 `": <cause>"`）。它通过记录「是否已写过非空内容」来决定是否插入分隔符。见 [src/error.rs:94-133](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L94-L133)。

---

### 4.4 ReuniteError：归还两半所有权

#### 4.4.1 概念说明

`split()` 把一个流拆成收/发两半（`RecvHalf`/`SendHalf`），`reunite()` 试图把它们合回原来的流。但若两半**来自不同的流**，合并不成立。此时面临一个所有权难题：`reunite` 已经按值拿走了这两半，失败时不能简单丢弃——否则调用方永久失去它们。

`ReuniteError<R, S>` 就是为此设计的：它不携带任何「原因解释」，而是**把两半原样作为公开字段还回去**：

```rust
pub struct ReuniteError<R, S> { pub rh: R, pub sh: S }
```

调用方可以从错误里取回 `rh`（receive half）和 `sh`（send half），再作他用。这是「失败归还所有权」思想在拆分/重聚场景的具体化。

#### 4.4.2 核心流程

`reunite` 的整体流程：

1. 取出 `rh` 与 `sh` 的所有权。
2. 判定二者是否同源（如 Unix 用 `Arc::ptr_eq`）。
3. 若同源：销毁冗余引用（drop `rh`），从 `sh` 取回内部流，返回 `Ok(stream)`。
4. 若不同源：返回 `Err(ReuniteError { rh, sh })`，两半原样归还。

在 enum 派发层（公共 `Stream`），后端返回的是「后端类型」的 `ReuniteError`（如 `ReuniteError<RecvPipeStream, SendPipeStream>`），需要桥接成公共类型的 `ReuniteError`（`ReuniteError<RecvHalf, SendHalf>`）。这正是 `convert_halves` 的用武之地：它用 `From` 实现把后端半边转成公共半边。

#### 4.4.3 源码精读

`ReuniteError` 的结构与 `map_halves`/`convert_halves`：

[src/error.rs:155-182](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L155-L182) —— 两字段 `pub rh`/`pub sh`；`map_halves` 用闭包映射；`convert_halves` 直接用 `From::from`，专为「包装流类型」设计。

`Display` 与 `Error`：

[src/error.rs:183-188](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L183-L188) —— 固定文案「attempt to reunite stream halves that come from different streams」。

`ReuniteResult` 别名：

[src/error.rs:190-191](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L190-L191) —— `Result<T, ReuniteError<R, S>>`；`Stream` trait 又在此基础上定义本层别名 [src/local_socket/stream/trait.rs:126-128](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L126-L128)。

Unix 后端 `reunite` 的直接构造（无 unsafe，靠 `Arc`）：

[src/os/unix/uds_local_socket/stream.rs:75-82](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L75-L82) —— `Arc::ptr_eq` 判同源，不同源则 `Err(ReuniteError { rh, sh })`。

公共 enum 层用 `convert_halves` 桥接后端错误：

[src/local_socket/stream/enum.rs:117-130](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L117-L130) —— 对匹配的后端变体调用 `e.convert_halves()`，把 `ReuniteError<后端半边>` 转成 `ReuniteError<公共半边>`；不匹配的变体（如一端是 NamedPipe、另一端是 UdSocket，因 cfg 互斥实际不会发生）则直接 `Err(ReuniteError { rh, sh })`。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：理清「后端 `ReuniteError` → 公共 `ReuniteError`」的桥接链路，体会 `convert_halves` 的作用。
2. **操作步骤**：
   - 读 [src/os/unix/uds_local_socket/stream.rs:75-82](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L75-L82)：后端 `reunite` 失败时构造的 `ReuniteError`，其 `R`/`S` 是什么类型？（答：后端自己的 `RecvHalf`/`SendHalf`，即 `RecvPipeStream`/`SendPipeStream` 之类。）
   - 读 [src/local_socket/stream/enum.rs:117-130](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L117-L130)：公共 `reunite` 用 `.map_err(|e| e.convert_halves())`。问：`convert_halves` 依赖什么 trait 把后端半边变成公共半边？
3. **需要观察的现象**：`convert_halves` 内部调用 `From::from`（见 [src/error.rs:179-181](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L179-L181)），因此后端半边必须对公共半边实现 `From`。
4. **预期结果**：你应能用一句话说出：「公共 `reunite` 先派发到后端，后端失败返回 `ReuniteError<后端半边>`，公共层用 `convert_halves`（借助 `From`）把它升格为 `ReuniteError<公共半边>`，从而把两半以公共类型归还给调用方。」
5. 进阶：对照 u3-l3，回忆「故意合并来自不同流的半边会得到 `ReuniteError`」——现在你能指出那条错误里 `rh`/`sh` 的所有权是如何一路无损归还到调用方手里的吗？

#### 4.4.5 小练习与答案

**练习 1**：`ReuniteError` 为什么把 `rh`/`sh` 设为 `pub` 字段，而不是提供 getter？

> **答案**：因为 `reunite` 已经按值消费了两半，失败时必须把所有权**完整**还给调用方。`pub` 字段让调用方能直接解构取回 `rh`/`sh`，没有任何中间损耗，符合「失败归还所有权」的设计目标。见 [src/error.rs:158-163](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L158-L163)。

**练习 2**：`convert_halves` 与 `map_halves` 有何区别？enum 派发层为什么选 `convert_halves`？

> **答案**：`map_halves` 接受两个闭包自由映射；`convert_halves` 是它的特化，固定用 `From::from`。enum 派发层需要把后端半边「转换」成公共半边，这恰好是 `From` 的语义，故用 `convert_halves` 最贴切。见 [src/error.rs:166-181](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L166-L181)。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「错误传播观察台」小任务：

**任务**：写一个程序，模拟「从原始对象转换失败」的完整错误传播路径，并观察每一步的所有权流动。

要求：

1. 定义一个自定义 details 类型（实现 `Display`），描述一个两阶段转换的失败阶段。
2. 用结构体字面量构造一个 `ConversionError`，三字段都填上：details 取你的自定义类型、`cause` 取一个 `io::Error`、`source` 取一个 `String`（模拟被消费的输入）。
3. 用 `map_source` 把被归还对象从 `String` 变成 `Vec<u8>`（例如 `s.into_bytes()`）。
4. 把它 `into()` 成 `io::Error`，打印消息，验证 details 与 cause 被 `": "` 连接。
5. 再构造一个**同源但 source 为 `None`** 的错误（模拟 Tokio 路径不归还对象），同样转成 `io::Error` 打印，对比消息里是否仍能体现 cause。

**验收点**：

- 能解释第 3 步后类型如何变化。
- 能说出第 4 步消息里 `": "` 出现的原因（`FormatSnooper`）。
- 能解释第 5 步为何「不归还对象」不影响 `io::Error` 的消息内容（因为消息只依赖 `details`/`cause`，与 `source` 字段无关）。
- 全程不调用任何 `unsafe`，所有权靠类型系统保证。

> 这是「源码阅读 + 动手实验」型综合实践；运行结果中 OS 错误的具体措辞**待本地验证**，但类型变化与消息拼接规则是确定的。

## 6. 本讲小结

- `ConversionError<S, E>` 用 `details`（失败在哪步）、`cause`（OS 原因）、`source`（被归还的输入对象）三字段正交组合，核心动机是「消费型转换失败时把输入所有权还回去」。
- `source` 字段是 `Option<S>`，因为库保留不归还的自由——Tokio 路径拿不回原对象时只能丢弃。
- `NoDetails` 是默认的空 details 标记；`FromHandleError`（Windows）与 `FromFdError`（Unix）是把它钉到平台句柄类型的别名，真实细节由后端自定义枚举（如 `FromHandleErrorKind`）提供。
- 降级为 `io::Error` 有 `to_io_error()` 与 `From` 两条路径，都只要求 `E: Display`；`FormatSnooper` 负责在 details 为空时不输出孤立的 `": "`。
- **易混点**：`Error::source()` 返回的是 `cause`，而非同名的 `source` 字段；要让 `ConversionError` 实现 `std::error::Error`，`E` 须满足 `Error + 'static`，常见 details 类型并不满足，故实践中多以 `Display` 降级。
- `ReuniteError<R, S>` 用 `pub rh`/`pub sh` 原样归还拆分失败的两半；enum 派发层用 `convert_halves`（借助 `From`）把后端半边桥接成公共半边。

## 7. 下一步学习建议

- 沿着 `FromHandleError`/`FromFdError` 往句柄方向走，建议复习 u7-l2（`TryClone`、`OwnedHandle`/`OwnedFd`、`ShareHandle` 的所有权模型），体会「错误归还的 `S`」与「句柄所有权抽象」如何咬合。
- 若想看异步路径上「不归还对象」的真实表现，可阅读 `src/os/windows/named_pipe/tokio/` 与 `src/os/unix/uds_local_socket/tokio/` 下各 `TryFrom` 实现，观察其 `Error` 类型为何退化为 `io::Error` 而非 `ConversionError`（承接 u6-l1 的「Tokio 镜像」结论）。
- 继续专家层单元：u9-l1（unsafe FFI 封装层）会解释 `cause` 字段里的 `io::Error` 是如何由 `OrErrno`/`RawOsErrorExt` 从系统调用返回值翻译来的，与本讲的 `cause` 字段首尾相接。
