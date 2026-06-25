# 名称系统：Name 与 NameType

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `local_socket::name` 模块存在的理由：为什么不能直接用字符串当 local socket 名字。
- 画出从「一个普通字符串」到「一个可用的 `Name`」的完整构造流水线。
- 区分两种维度的名称：**文件系统路径名**（filesystem path）与**命名空间名**（namespace），并理解 `is_namespaced()` 和 `is_path()` 为何可以同时为真。
- 掌握 `NameType` / `PathNameType` / `NamespacedNameType` 三个 trait 的分工，以及 `GenericFilePath` / `GenericNamespaced` 这两个「总是受支持」的标记类型。
- 知道 `ToFsName` / `ToNsName` 是面向使用者的构造入口，以及它们覆盖了哪些字符串类型。

本讲承接 [u2-l1 Local Socket 的设计哲学](u2-l1-local-socket-philosophy.md)：你已经知道 local socket 是 interprocess 在底层原语之上构造的抽象，本讲专门拆解「按名字寻址」这一步的内部机制。

## 2. 前置知识

在进入源码前，先用通俗语言澄清三个概念。

**操作系统怎么命名一个 local socket？** 同一个抽象概念，在不同平台上的「名字」长得完全不一样：

| 平台 | 底层原语 | 典型名字形式 |
|---|---|---|
| Windows | named pipe | `\\.\pipe\example` |
| Unix（通用） | Unix domain socket（UDS） | `/tmp/example.sock`（一个真实存在于文件系统里的文件） |
| Linux / Android | UDS 的抽象命名空间 | 一段字节，不属于文件系统，最长 107 字节 |

**问题来了**：如果公共 API 直接接收一个 `String`，库就无法判断它到底该被当成 Windows 管道名、Unix 文件路径，还是 Linux 抽象命名空间。强行猜测会破坏稳定性（参见 u2-l1 讲过的「字节流透明、可与非 interprocess 程序互通」这一承诺）。

**解决方案**：引入一个 `Name` 类型承载「已经解释清楚含义的名字」，并要求调用方在构造时**显式选择一条映射规则**（用 tag 类型作泛型参数）。这样平台差异被收敛进类型系统，下游代码就能保持平台无关。

**两个相关术语**：

- **Cow（Clone-on-Write）**：`std::borrow::Cow<'a, T>` 要么借用一段数据（`Borrowed`），要么拥有它（`Owned`）。`Name` 用它来在「名字来自临时字符串、只需借用」和「名字需要长期存活、必须拥有副本」两种场景间复用同一套代码。
- **uninhabited type（不可居类型）**：没有值的类型，例如 `enum Foo {}`。它无法被实例化，只能作为「类型层面的标记」出现在泛型参数里。本讲的 tag 类型就是这种用法。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/local_socket/name.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name.rs) | 名称子模块的入口：声明子模块、导出公共类型、定义对外可见的 `Name` 结构及其方法。 |
| [src/local_socket/name/inner.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/inner.rs) | 平台私有的 `NameInner` 枚举——真正存储名字字节、按平台/变体分派的内核。 |
| [src/local_socket/name/type.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs) | `NameType` / `PathNameType` / `NamespacedNameType` trait 定义，以及 `GenericFilePath`、`GenericNamespaced` 两个跨平台标记类型。 |
| [src/local_socket/name/to_name.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/to_name.rs) | `ToFsName` / `ToNsName` 两个面向使用者的转换 trait，以及为 `&str`/`String`/`Path`/`OsStr`/`CStr` 等类型批量实现的入口。 |
| [src/os/unix/local_socket/name_type.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs) | Unix 平台的 name type 实现：`FilesystemUdSocket`、`SpecialDirUdSocket`、`AbstractNsUdSocket`，以及 `Generic*` 在 Unix 上的具体落地。 |
| [src/os/windows/local_socket/name_type.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/name_type.rs) | Windows 平台的 name type 实现：`NamedPipe`，以及 `Generic*` 在 Windows 上的落地与 `\\.\pipe\` 前缀校验。 |
| [src/macros.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs) | `tag_enum!`（生成不可居标记枚举）与 `impmod!`（按 cfg 注入平台后端别名）两个宏。 |

## 4. 核心概念与源码讲解

### 4.1 Name 与 NameInner：统一容器与平台变体

#### 4.1.1 概念说明

`Name` 是 local socket 名字的「统一容器」。它的文档注释开门见山地点明了它存在的理由：

> 不同平台给 local socket 命名的方式差异巨大，需要一个类型在统一存储与处理这些名字的同时，**保留平台特有性质**。`Name` 的职责就是在「可移植性」与「正确性」之间架桥，尽量减少下游程序里的平台相关代码。

注意文档里的两个关键约束：

- 不能从不**支持**的值构造 `Name`（例如在不支持抽象命名空间的平台上构造抽象命名空间名会直接报错）。
- 但可以从**无效**的值构造（例如一个不存在的文件路径），因为有效性要到真正绑定/连接时才能由 OS 判定。

#### 4.1.2 核心流程

`Name` 本身只是一个薄壳，真正的数据藏在私有的 `NameInner` 枚举里。整体结构是：

```
Name<'s>  (公共、对外可见)
  └── NameInner<'s>  (pub(crate)，平台私有)
        ├── NamedPipe(Cow<U16CStr>)        [cfg(windows)]
        ├── UdSocketPath(Cow<OsStr>)       [cfg(unix)]
        ├── UdSocketPseudoNs(Cow<OsStr>)   [cfg(unix)]
        └── UdSocketNs(Cow<[u8]>)          [cfg(linux/android)]
```

`Name` 提供两个查询方法来刻画「这个名字属于哪个维度」：

- `is_namespaced()`：名字是否指向一个**专用的 local socket 命名空间**。
- `is_path()`：名字是否以**文件系统路径**的形式存储。

二者并非互斥——这正是 Windows 管道名的特殊之处，详见 4.1.3。

#### 4.1.3 源码精读

先看 `Name` 结构本体与方法——它就是对 `NameInner` 的一层 newtype：

[src/local_socket/name.rs:27-28](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name.rs#L27-L28) — `Name` 只持有一个 `pub(crate)` 字段，外部无法直接构造，只能通过 `ToFsName`/`ToNsName` 入口构造。

[src/local_socket/name.rs:30-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name.rs#L30-L32) — `is_namespaced()` 直接委托给内部枚举的同名方法。

[src/local_socket/name.rs:34-47](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name.rs#L34-L47) — `is_path()` 的文档里藏着一个能说明问题的 doctest：`\\.\pipe\example` 这个 Windows 管道名**同时**满足 `is_namespaced()`（因为 `\\.\pipe\` 是一个命名空间）和 `is_path()`（因为整体是一条路径）。这正是「两个维度不互斥」的活例子。

[src/local_socket/name.rs:49-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name.rs#L49-L55) — `borrow()` 产生一个借用版 `Name<'_>`，`into_owned()` 把生命周期抬升到 `'static`（必要时克隆）。这两个方法让 `Name` 能在「临时借用」与「长期拥有」之间转换。

再看内核 `NameInner`，它是理解一切平台差异的钥匙：

[src/local_socket/name/inner.rs:9-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/inner.rs#L9-L18) — 四个变体，每个都用 `#[cfg]` 门控，因此**一次编译里只有当前平台对应的变体存在**。这正是 u2-l3 讲过的「壳/芯」分层在这里的体现。

[src/local_socket/name/inner.rs:50-75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/inner.rs#L50-L75) — `is_namespaced()` 与 `is_path()` 用 `match` 逐变体判定。注意结果并非简单二分：

| 变体 | `is_namespaced()` | `is_path()` | 含义 |
|---|---|---|---|
| `NamedPipe`（Windows） | `true` | `true` | 管道名既是命名空间也是路径 |
| `UdSocketPath`（Unix 文件系统） | `false` | `true` | 普通的文件系统路径 |
| `UdSocketPseudoNs`（Unix 伪命名空间） | `false` | `false` | 内部仍是路径，但向用户隐藏了具体路径 |
| `UdSocketNs`（Linux 抽象命名空间） | `true` | `false` | 纯字节序列，不落文件系统 |

[src/local_socket/name/inner.rs:34-47](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/inner.rs#L34-L47) — `map_cow!` 宏用同样的「按 cfg 互斥 match」模式，把 `borrow`/`into_owned` 统一应用到每个变体的 `Cow` 上，避免为四个变体各写一遍。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，能预测一个具体名字在给定平台上的 `is_namespaced()` / `is_path()` 结果。

**操作步骤**：

1. 打开 [src/local_socket/name/inner.rs:50-75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/inner.rs#L50-L75)。
2. 针对下表每一行，判断它最终会落到哪个 `NameInner` 变体，再查出对应的两个布尔值。

**需要观察的现象 / 预期结果**（待本地验证，因平台而异）：

| 场景 | 平台 | 落到的变体 | `is_namespaced()` | `is_path()` |
|---|---|---|---|---|
| `"example.sock".to_ns_name::<GenericNamespaced>()` | Linux | `UdSocketNs` | `true` | `false` |
| 同上 | macOS / 其它 Unix | `UdSocketPseudoNs` | `false` | `false` |
| 同上 | Windows | `NamedPipe` | `true` | `true` |
| `"/tmp/x.sock".to_fs_name::<GenericFilePath>()` | Unix | `UdSocketPath` | `false` | `true` |
| `r"\\.\pipe\foo".to_fs_name::<GenericFilePath>()` | Windows | `NamedPipe` | `true` | `true` |

注意第二行（macOS）是个反直觉点：用「命名空间名」构造出来的 `Name`，其 `is_namespaced()` 居然是 `false`。原因是 macOS 不支持真正的命名空间，interprocess 用「伪命名空间」（内部是一个 `/tmp` 下的隐藏路径）来兜底，于是它既不是真命名空间，也不对外暴露为路径。这一点在 4.3 会再展开。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Name` 的字段是 `pub(crate)` 而不是 `pub`？

> 参考答案：因为 `Name` 必须保证「只能从不支持的值构造失败、不能任意捏造内部变体」。若字段公开，外部就能直接拼出一个当前平台根本不存在的变体（比如在 Windows 上塞一个 `UdSocketPath`），破坏平台不变式。把构造入口收敛到 `ToFsName`/`ToNsName`，才能在构造时做平台校验。

**练习 2**：`is_namespaced()` 和 `is_path()` 会不会同时返回 `false`？

> 参考答案：会。`UdSocketPseudoNs` 变体（Unix 伪命名空间）两者都返回 `false`——它内部是文件系统路径，但 interprocess 向用户隐藏了这一点，因此既不算「真命名空间」也不算「用户可见的路径」。

### 4.2 NameType / PathNameType / NamespacedNameType：映射规则的 trait 接口

#### 4.2.1 概念说明

光有一个能存名字的 `Name` 还不够——我们需要一种方式来描述「**字符串 → 名字**」的映射规则，而且这条规则要能随平台变化。interprocess 的做法是：定义一组 trait，但**不实现它们的逻辑**，而是把它们实现在一批「标记类型（tag type）」上，让调用方通过泛型参数选择具体规则。

三个 trait 构成如下层级：

```
NameType                        （所有标记类型的公共接口：is_supported() + 一堆约束）
  ├── PathNameType<S>           （把「路径」映射成 Name）
  └── NamespacedNameType<S>     （把「字符串」映射成命名空间名 Name）
```

#### 4.2.2 核心流程

调用链是这样的（以 `to_ns_name::<GenericNamespaced>()` 为例）：

```
"foo".to_ns_name::<GenericNamespaced>()        (ToFsName/ToNsName 入口，见 4.4)
   └─> GenericNamespaced::map(Cow::Borrowed("foo"))   (NamespacedNameType::map)
         └─> n_impl::map_generic_namespaced_osstr(name)   (平台注入的函数)
               └─> 生成具体 NameInner 变体 → 包成 Name
```

其中 `n_impl` 是用 `impmod!` 宏按平台注入的别名（u2-l3 已讲过），指向 `os::unix::local_socket::name_type` 或 `os::windows::local_socket::name_type`。

`NameType` 还要求 `is_supported()`：它描述「这条映射规则在当前运行环境下是否可用」，可能需要向 OS 查询，OS 报错时返回 `false`。

#### 4.2.3 源码精读

[src/local_socket/name/type.rs:35-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L35-L41) — `NameType` 的定义。两个要点：

1. 它带一长串 supertrait 约束：`Copy + Debug + Eq + Send + Sync + Unpin`。这些都是标记类型理应满足的廉价约束（标记类型不可实例化，这些 trait 几乎零成本满足）。
2. 它以 `Sealed` 为超 trait。`Sealed` 定义在 [src/misc.rs:19-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L19-L21)，是一个 `pub(crate)` 的空 trait——**外部 crate 无法实现它**，因此也无法自己实现 `NameType`。这是 u2-l1 提到的「Sealed 封印」在名称系统里的应用：把「能定义哪些映射规则」牢牢攥在 interprocess 手里，保证映射稳定可预测。

[src/local_socket/name/type.rs:46-52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L46-L52) — `PathNameType<S>`：接收一个 `Cow<'_, S>`（S 通常是 `OsStr` 或 `CStr`），返回 `io::Result<Name<'_>>`。失败表示「该名字在当前平台不被支持」。

[src/local_socket/name/type.rs:56-62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L56-L62) — `NamespacedNameType<S>`：接口形态与 `PathNameType` 完全对称，区别只在语义（命名空间 vs 路径）。

`NameType` 文档里还有一句重要的稳定性承诺（[src/local_socket/name/type.rs:29-33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L29-L33)）：**映射规则的改变属于破坏性变更（breaking change）**——只要某个输入曾经能成功造出可用的名字，未来就必须继续把它映射到「OS 意义上的同一个名字」。这正是 local socket 能与非 interprocess 程序互通的根基。

[src/local_socket/name/type.rs:18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L18) — `impmod! {local_socket::name_type as n_impl}` 这一句，把平台后端的 `name_type` 模块以别名 `n_impl` 注入进来。展开后等价于：

```rust
#[cfg(unix)]   use crate::os::unix::local_socket::name_type as n_impl;
#[cfg(windows)] use crate::os::windows::local_socket::name_type as n_impl;
```

两个 `cfg` 互斥，所以运行时没有派发开销。宏本体见 [src/macros.rs:4-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L4-L14)。

#### 4.2.4 代码实践

**实践目标**：验证「Sealed 封印」确实阻止了外部实现 `NameType`。

**操作步骤**：

1. 在一个依赖 interprocess 的外部 crate 里，尝试写：
   ```rust
   // 示例代码（预期无法编译）
   use interprocess::local_socket::NameType;
   enum MyTag {}
   impl NameType for MyTag {
       fn is_supported() -> bool { true }
   }
   ```
2. 运行 `cargo check`。

**需要观察的现象**：编译器报错，提示 `MyTag` 没有实现 `Sealed`（或类似「trait `Sealed` is private」的信息）。

**预期结果**：无法编译。这证明 `NameType` 是封闭的，用户只能从 interprocess 提供的现成标记类型里选择。

#### 4.2.5 小练习与答案

**练习 1**：`NameType` 为什么要求 `Copy`？标记类型又没有值。

> 参考答案：标记类型不可实例化，`Copy` 对它几乎零成本，但加上这个约束后，泛型代码里就可以放心地「复制」一个标记类型的值（虽然实际不会用到），并把它当作纯编译期参数传递，简化 trait bound 的书写。

**练习 2**：`map()` 为什么返回 `io::Result` 而不是直接 `Name`？

> 参考答案：因为同一份字符串在某些平台上可能「不被支持」。例如 Windows 上用 `GenericFilePath` 传一个非 `\\.\pipe\` 开头的路径会失败（见 4.3）；又如抽象命名空间只在 Linux 存在。`Result` 让这些「平台不支持」的情况在构造期就被发现，而不是拖到运行时才崩溃。

### 4.3 标记类型：GenericFilePath / GenericNamespaced 与平台专有 tag

#### 4.3.1 概念说明

标记类型本身是「不可居枚举」（uninhabited enum），由 `tag_enum!` 宏生成。它们没有任何值，只作为 `to_fs_name::<T>` / `to_ns_name::<T>` 的泛型参数出现，用来在**类型层面**选定一条映射规则。

标记类型分两层：

- **跨平台「总是受支持」的便捷标记**：`GenericFilePath`、`GenericNamespaced`。它们的设计目标是「在任何平台上都能用」，具体怎么映射由库按平台决定。这是绝大多数程序该用的入口。
- **平台专有的精确标记**：Unix 的 `FilesystemUdSocket`、`SpecialDirUdSocket`、`AbstractNsUdSocket`，Windows 的 `NamedPipe`。需要精确控制底层原语时才用它们。

#### 4.3.2 核心流程

`tag_enum!` 宏的展开很简单，它把每个标记名变成一个空枚举，并为它实现 `Sealed`：

```
tag_enum!(GenericFilePath);
  └─展开─>  #[derive(Copy, Clone, Debug, PartialEq, Eq)]
            pub enum GenericFilePath {}
            impl Sealed for GenericFilePath {}
```

接着，每个标记类型分别 impl `NameType`（提供 `is_supported()`）以及 `PathNameType` 或 `NamespacedNameType`（提供 `map()`）。`map()` 的真正逻辑则委托给平台后端的 `n_impl::map_*` 函数。

两个 `Generic*` 标记的「总是受支持」承诺体现在：它们各自平台的 `map_*` 函数会按平台选用一个**确定可用**的底层变体。

#### 4.3.3 源码精读

先看宏本体：

[src/macros.rs:103-111](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L103-L111) — `tag_enum!` 为每个标记生成空枚举 + `Sealed` 实现。注意它接受紧贴标识符的文档注释（`$(#[$attr:meta])*`），所以每个标记类型都能带上详尽的平台行为说明。

再看两个跨平台标记的声明与 `is_supported()`：

[src/local_socket/name/type.rs:79-81](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L79-L81) — `GenericFilePath` 的 `is_supported()` 无条件返回 `true`。

[src/local_socket/name/type.rs:112-114](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L112-L114) — `GenericNamespaced` 同样无条件返回 `true`。

它们的 `map()` 则把工作转给平台后端：

[src/local_socket/name/type.rs:82-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L82-L85) — `GenericFilePath` 的 `PathNameType<OsStr>::map` 调 `n_impl::map_generic_path_osstr`。

[src/local_socket/name/type.rs:115-120](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L115-L120) — `GenericNamespaced` 的 `NamespacedNameType<OsStr>::map` 调 `n_impl::map_generic_namespaced_osstr`。

注意 [src/local_socket/name/type.rs:86-91](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L86-L91) 和 [src/local_socket/name/type.rs:121-128](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L121-L128)：基于 `CStr`（C FFI 字符串）的实现**只在 Unix 上存在**（`#[cfg(unix)]`），Windows 上没有——因为 Windows 管道名是 UTF-16，没有自然的 C 字符串表示。

现在进入平台后端，看 `Generic*` 究竟映射成什么。

**Unix 后端**：

[src/os/unix/local_socket/name_type.rs:111-140](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs#L111-L140) — `map_generic!` 宏生成四个函数。关键在 `namespaced` 分支：

[src/os/unix/local_socket/name_type.rs:117-129](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs#L117-L129) — `GenericNamespaced` 在 Linux/Android 上调 `AbstractNsUdSocket::map`（抽象命名空间），在其它 Unix 上调 `SpecialDirUdSocket::map`（伪命名空间）。这就是 4.1 表格里 macOS 落到 `UdSocketPseudoNs` 的原因。

[src/os/unix/local_socket/name_type.rs:94-104](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs#L94-L104) — `AbstractNsUdSocket::map` 直接把名字转成字节 `Cow<'_, [u8]>`，塞进 `NameInner::UdSocketNs`。Linux 抽象命名空间地址受 `sockaddr_un.sun_path` 长度限制，去掉首字节标记后可用长度为：

\[
L_{\text{max}} = |\texttt{sun\_path}| - 1 = 108 - 1 = 107
\]

这与文档注释里「maximum length of 107 bytes」一致（[src/local_socket/name/type.rs:104-106](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L104-L106)）。

[src/os/unix/local_socket/name_type.rs:28-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs#L28-L41) — `FilesystemUdSocket::map`（`GenericFilePath` 在 Unix 的落地）会先校验路径不含内部 NUL（因为 C 字符串以 NUL 结尾），再包成 `UdSocketPath`。

[src/os/unix/local_socket/name_type.rs:47-80](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs#L47-L80) — `SpecialDirUdSocket` 已被 `#[deprecated]`，文档建议直接用 `FilesystemUdSocket`。它是非 Linux Unix 上唯一的「命名空间」兜底方案。

**Windows 后端**：

[src/os/windows/local_socket/name_type.rs:9-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/name_type.rs#L9-L30) — `NamedPipe::map`（`GenericFilePath` 在 Windows 的落地）先校验路径确实是 NPFS 管道路径，再把 `OsStr` 编码成 UTF-16 宽字符串，包成 `NamedPipe` 变体。

[src/os/windows/local_socket/name_type.rs:38-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/name_type.rs#L38-L41) — `GenericNamespaced` 在 Windows 上的落地：把字符串编码成宽字符串存入 `NamedPipe` 变体，**但此刻并不补上 `\\.\pipe\` 前缀**。代码注释明说「The prepending currently happens at a later point」——前缀的拼接被推迟到 named pipe 后端真正绑定/连接时（这部分在 [u4-l2](u4-l2-windows-named-pipe-local-socket.md) / [u4-l3](u4-l3-windows-raw-named-pipe.md) 展开）。所以用 `to_ns_name::<GenericNamespaced>()` 在 Windows 上得到的 `Name`，其内部尚不含前缀。

[src/os/windows/local_socket/name_type.rs:43-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/name_type.rs#L43-L60) — `is_pipefs` 判定一个路径是否形如 `\\HOSTNAME\pipe\NAME`，靠前缀 `\\` 与中段 `\pipe\` 来识别。模块末尾的内联测试（[src/os/windows/local_socket/name_type.rs:72-95](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/name_type.rs#L72-L95)）给出了正反例，包括一条 `C:\Users\...\neovide.sock` 被正确判为「不是管道路径」。

#### 4.3.4 代码实践

**实践目标**：阅读平台后端，确认 `Generic*` 两个标记在三大平台族上的具体落地，并自己复述。

**操作步骤**：

1. 打开 Unix 后端 [src/os/unix/local_socket/name_type.rs:135-140](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs#L135-L140) 与 Windows 后端 [src/os/windows/local_socket/name_type.rs:32-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/name_type.rs#L32-L41)。
2. 在纸上把下表填满（答案见 4.1.4 的对照表）。

| 标记 | Windows | Linux/Android | 其它 Unix |
|---|---|---|---|
| `GenericFilePath` | `NamedPipe`（须 `\\.\pipe\`） | `UdSocketPath` | `UdSocketPath` |
| `GenericNamespaced` | `NamedPipe`（前缀延后补） | ? | ? |

**需要观察的现象 / 预期结果**：

- `GenericNamespaced` 在 Linux/Android → `UdSocketNs`（抽象命名空间）。
- `GenericNamespaced` 在其它 Unix → `UdSocketPseudoNs`（伪命名空间，`SpecialDirUdSocket`）。

**待本地验证**：在不同平台上分别构造并打印变体，确认上表。

#### 4.3.5 小练习与答案

**练习 1**：既然 `GenericNamespaced` 在所有平台都「受支持」，为什么它的 `map()` 仍然可能失败？

> 参考答案：`is_supported()` 描述的是「这条规则在当前平台是否存在」，而 `map()` 描述的是「这个具体输入是否能被这条规则接受」。例如 Linux 抽象命名空间对名字长度有 107 字节上限，超长的输入在 `map()` 时仍可能失败。规则存在 ≠ 任意输入都合法。

**练习 2**：为什么 `GenericNamespaced` 在非 Linux Unix 上要退化成「伪命名空间」而不是直接报错？

> 参考答案：因为 `GenericNamespaced` 的核心承诺是「在任何平台上都能用」。非 Linux Unix 没有真正的抽象命名空间，若直接报错就违背了这一承诺；于是 interprocess 用 `/tmp` 下的隐藏路径兜底（`SpecialDirUdSocket`），向用户隐藏具体路径，从而维持「给个名字就能用」的体验——代价是它既不是真命名空间也不暴露为用户路径（`is_namespaced()` 与 `is_path()` 都为 `false`）。

### 4.4 ToFsName / ToNsName：从字符串到 Name 的入口

#### 4.4.1 概念说明

到目前为止，我们有了容器（`Name`）和映射规则（标记类型 + trait）。但用户不会直接调 `GenericNamespaced::map(...)`——那要自己处理 `Cow`、自己选标记。`ToFsName` / `ToNsName` 这两个 trait 就是面向用户的**便利入口**：它们被实现在所有常见的字符串类型上，让你写出 `"foo.sock".to_ns_name::<GenericNamespaced>()` 这样简洁的代码。

#### 4.4.2 核心流程

入口 trait 的设计思路是「**适配 + 委托**」：

```
"foo".to_ns_name::<GenericNamespaced>()
   │  (&str 实现了 ToNsName)
   ├─ 先把 &str 适配成 &OsStr：OsStr::new(self)
   └─ 再委托给 OsStr 已有的实现 → NT::map(Cow::Borrowed(self))
```

对于「借用型」（`&str`、`&Path`、`&OsStr`、`&CStr`）输入，构造出的 `Name` 借用原数据（`Cow::Borrowed`），零拷贝；对于「拥有型」（`String`、`PathBuf`、`OsString`、`CString`）输入，则消费所有权转入 `Cow::Owned`。这样 `Name<'s>` 的生命周期 `'_` 与输入来源严格匹配。

#### 4.4.3 源码精读

[src/local_socket/name/to_name.rs:30-36](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/to_name.rs#L30-L36) — `ToFsName` 定义：一个带泛型的方法 `to_fs_name<NT: PathNameType<S>>`，把「选哪条路径映射规则」作为泛型参数交给调用方。

[src/local_socket/name/to_name.rs:38-44](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/to_name.rs#L38-L44) — `ToNsName` 定义，与 `ToFsName` 对称，泛型约束换成 `NamespacedNameType<S>`。

下面是一组「真正干活」的实现：

[src/local_socket/name/to_name.rs:49-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/to_name.rs#L49-L60) — `&Path` 与 `PathBuf` 的 `ToFsName` 实现：借用时 `Cow::Borrowed`，拥有时 `Cow::Owned`。

[src/local_socket/name/to_name.rs:15-28](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/to_name.rs#L15-L28) — `trivial_string_impl!` 宏批量生成「先把字符串适配成 `Path`/`OsStr`，再委托」的实现，避免为 `&str`、`String`、`&OsStr`、`OsString` 各写一遍样板。

[src/local_socket/name/to_name.rs:61-83](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/to_name.rs#L61-L83) — 这一段是上面宏展开后的结果：`&str`/`String` 走 `Path`/`PathBuf`（对 `ToFsName`）或 `OsStr`/`OsString`（对 `ToNsName`）。

[src/local_socket/name/to_name.rs:85-109](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/to_name.rs#L85-L109) — `CStr` / `CString` 的实现，供 Unix 上的 C FFI 场景使用。

实战中，这套入口最常见的用法就在官方示例里：

[examples/local_socket/sync/stream.rs:9-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/stream.rs#L9-L13) — 典型的「先探测、再分支构造」模式：`GenericNamespaced::is_supported()` 为真时用 `to_ns_name`，否则回退到 `to_fs_name`。这段代码是本讲综合实践的直接范本。

#### 4.4.4 代码实践

**实践目标**：体会「借用型 vs 拥有型」输入对 `Name` 生命周期的影响。

**操作步骤**：

1. 阅读下面两段等价写法，判断各自 `Name` 的生命周期来源：

```rust
// 示例代码
use interprocess::local_socket::{GenericNamespaced, ToNsName};

fn borrowed(literal: &str) {
    // literal 是 &'static，Name 借用它，零拷贝
    let _name = literal.to_ns_name::<GenericNamespaced>().unwrap();
}

fn owned() {
    let s = format!("example-{}.sock", 42); // String，非 'static
    // 消费 s 的所有权，Name 拥有其副本，之后 s 不可再用
    let _name = s.to_ns_name::<GenericNamespaced>().unwrap();
}
```

2. 尝试把 `borrowed` 的参数类型从 `&str` 改成 `String`（并在函数内 `&s`），观察签名变化与是否产生拷贝。

**需要观察的现象 / 预期结果**（待本地验证）：用 `&str` 调用时 `Name` 的生命周期绑定到该借用；用 `String` 调用时 `Name` 取得所有权、原 `String` 被消费。两者最终都能产生一个可用的 `Name`，但内存来源不同。

#### 4.4.5 小练习与答案

**练习 1**：`"foo".to_ns_name::<GenericNamespaced>()` 这一行里，`"foo"`、`to_ns_name`、`GenericNamespaced` 三者分别扮演什么角色？

> 参考答案：`"foo"` 是待转换的数据（它是 `&str`，`ToNsName` 的实现者）；`to_ns_name` 是 `ToNsName` trait 上的方法（入口）；`GenericNamespaced` 是泛型实参（标记类型，决定用哪条映射规则）。三者分别对应「数据 / 入口 / 规则」。

**练习 2**：为什么 `ToFsName` / `ToNsName` 不直接做成 `Name::from("foo")` 这种形式？

> 参考答案：因为「同一个字符串」可以映射成不同种类的名字（路径名 vs 命名空间名），且不同平台映射规则不同。若用单一的 `From`，编译器无法知道用户想要哪条规则。用泛型方法 `to_ns_name::<Tag>()` 把「规则选择」显式编码进调用点，既类型安全又自文档化。

## 5. 综合实践

把本讲所有最小模块串起来，完成下面这个「按平台自适应构造 `Name` 并自省」的小程序。这正是本讲规格里指定的实践任务。

**实践目标**：用 `GenericNamespaced::is_supported()` 分支，分别用命名空间名或文件路径名构造 `Name`，并打印它的 `is_namespaced()` / `is_path()`，从而亲眼看清不同平台的内部变体差异。

**操作步骤**：

1. 在 interprocess 仓库下新建一个临时示例（或在一个依赖 interprocess 的小 crate 里），写入：

```rust
// 示例代码
use interprocess::local_socket::{prelude::*, GenericFilePath, GenericNamespaced};

fn main() -> std::io::Result<()> {
    // 1) 先探测「命名空间名」规则是否受支持（当前 interprocess 下恒为 true，
    //    这里保留分支是为了演示 forward-compatible 的回退写法）。
    let name = if GenericNamespaced::is_supported() {
        println!("[选 GenericNamespaced] 用命名空间名构造");
        "example.sock".to_ns_name::<GenericNamespaced>()?
    } else {
        println!("[回退 GenericFilePath] 用文件路径名构造");
        "/tmp/example.sock".to_fs_name::<GenericFilePath>()?
    };

    // 2) 自省这个 Name 属于哪个维度。
    println!("is_namespaced() = {}", name.is_namespaced());
    println!("is_path()       = {}", name.is_path());

    Ok(())
}
```

2. 用 `cargo run` 运行。

**需要观察的现象 / 预期结果**（因平台而异，待本地验证）：

| 运行平台 | 命中分支 | 落到的 `NameInner` 变体 | `is_namespaced()` | `is_path()` |
|---|---|---|---|---|
| Linux / Android | `GenericNamespaced` | `UdSocketNs` | `true` | `false` |
| Windows | `GenericNamespaced` | `NamedPipe`（前缀延后补） | `true` | `true` |
| 其它 Unix（macOS 等） | `GenericNamespaced` | `UdSocketPseudoNs` | `false` | `false` |

**延伸思考**：当前 `GenericNamespaced::is_supported()` 在所有受支持平台上都返回 `true`，所以 `else` 分支实际不会被走到——它存在的意义是为未来「某个平台可能新增/移除命名空间支持」留出前向兼容的回退路径。如果你想强制走到 `else` 分支观察 `GenericFilePath`，可以把条件改成 `if false { ... } else { ... }`，此时各平台会分别落到 `NamedPipe`（Windows，且名字须为 `\\.\pipe\...`）或 `UdSocketPath`（Unix）。

## 6. 本讲小结

- `Name` 是 local socket 名字的统一容器，内部用 `pub(crate)` 的 `NameInner` 枚举按平台存储不同变体；外部只能通过 `ToFsName`/`ToNsName` 构造，保证平台不变式不被破坏。
- `NameInner` 有四个 `#[cfg]` 互斥的变体（`NamedPipe`/`UdSocketPath`/`UdSocketPseudoNs`/`UdSocketNs`）；`is_namespaced()` 与 `is_path()` 两个维度**不互斥**，Windows 管道名两者皆真，Unix 伪命名空间两者皆假。
- 映射规则由 `NameType` / `PathNameType` / `NamespacedNameType` 三个 trait 描述，并以 `Sealed` 封印防止外部自行实现，从而保证映射稳定可预测（映射变更属破坏性变更）。
- 规则的具体逻辑实现在「不可居标记类型」上：跨平台的 `GenericFilePath` / `GenericNamespaced` 总是受支持，平台专有的 `FilesystemUdSocket` / `AbstractNsUdSocket` / `SpecialDirUdSocket` / `NamedPipe` 提供精确控制。
- `Generic*` 标记把平台差异收敛进后端的 `map_generic_*` 函数：Linux 走抽象命名空间、其它 Unix 走伪命名空间、Windows 走 named pipe（`\\.\pipe\` 前缀延后到后端补）。
- `ToFsName` / `ToNsName` 是面向用户的便利入口，覆盖 `&str`/`String`/`Path`/`OsStr`/`CStr` 等类型，并用 `Cow` 让借用型输入零拷贝、拥有型输入转移所有权。

## 7. 下一步学习建议

本讲把「名字是怎么来的」讲透了。接下来：

- 想看「名字是怎么被用来创建监听器/连接的」，继续 [u3-l1 ListenerOptions 与服务端创建](u3-l1-listener-options.md) 和 [u3-l2 ConnectOptions 与客户端连接](u3-l2-connect-options.md)——它们正是 `Name` 的消费者。
- 想看「Windows 上 `\\.\pipe\` 前缀到底在哪里补上」，进入 [u4-l2 Windows 后端：named pipe local socket](u4-l2-windows-named-pipe-local-socket.md) 与 [u4-l3 Windows 原生 named pipe API](u4-l3-windows-raw-named-pipe.md)。
- 想从整体上复习「壳/芯」分层与 `impmod!` 注入机制，可回看 [u2-l3 impmod 与平台后端注入](u2-l3-impmod-backend-injection.md)——本讲的 `n_impl` 就是它的一个实例。
