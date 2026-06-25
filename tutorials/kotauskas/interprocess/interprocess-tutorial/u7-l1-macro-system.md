# 宏系统全景：forwarding 与 derive 宏

## 1. 本讲目标

本讲是「内部基础设施」单元的第一篇，专门拆解 interprocess 用来**消灭样板代码**的私有宏系统。

interprocess 有大量形如这样的「newtype 壳」类型：

```rust
pub struct Sender(pub(crate) SenderImpl);
```

一个两行的结构体，最终却同时具备 `Write`、`Debug`、`AsHandle`/`AsFd`、`From<OwnedHandle>`、`AsRawHandle`/`FromRawHandle`/`IntoRawHandle`…… 等十几项 trait 实现。**这些 trait 没有一个是手写的**，全部由本讲的宏自动生成。

学完本讲，你应当能够：

1. 说出 `multimacro!` 如何把一串宏「批量」套到同一个类型上，以及它展开后的等价形态。
2. 区分两类宏的本质差异：
   - **`forward_*`（转发宏）**：内部字段 `.0` 已经实现了目标 trait，宏只是生成一个「委托给 `self.0`」的实现。
   - **`derive_*`（派生宏）**：内部字段**没有**目标 trait，但有一个**相关的基础能力**，宏基于这个基础「拼装」出目标 trait。
3. 读懂 `forward_iorw`、`forward_handle_and_fd`、`derive_raw`、`derive_mut_iorw`、`derive_trivconv` 等核心宏的定义与展开。
4. 理解 `tag_enum`、`builder_setters` 这两个「类型生成器」宏在标记类型与构建器中的作用。
5. 拿到一个 `multimacro!` 调用，能逐项列出它为该类型缝上了哪些 trait。

---

## 2. 前置知识

在进入源码前，先把几个底层概念用大白话讲清楚。

### 2.1 newtype 模式与「壳/芯」分层

interprocess 的公共类型几乎都是 **newtype**：一个只包着一个字段 `pub(crate)` 的元组结构体，例如 `pub struct Sender(pub(crate) SenderImpl);`。公共类型是「壳」，被包住的 `SenderImpl` 是「芯」。u2-l3、u5-l1 已建立这套视角：壳不直接做系统调用，所有能力都转发给芯。本讲的宏，就是「把芯的能力搬到壳上」的自动化工具。

### 2.2 trait 转发：为什么需要宏

假设芯 `SenderImpl` 已经实现了 `Write`，你希望壳 `Sender` 也实现 `Write`。手写是这样的：

```rust
impl Write for Sender {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> { self.0.write(buf) }
    fn flush(&mut self) -> io::Result<()> { self.0.flush() }
    // ... write_vectored ...
}
```

每个方法都只是 `self.0.xxx()`。这种「纯委托」代码叫 **样板（boilerplate）**。Rust 的 `#[derive]` 不能帮你转发任意 trait，于是 interprocess 用 `macro_rules!` 自己造了一套。本讲就是讲这套自造工具。

### 2.3 `macro_rules!` 与文本作用域

本讲的所有宏都是 **声明式宏**（`macro_rules!`），在编译期做**文本替换**（更准确地说是 token 树展开）。关键点：

- 宏不是函数，它「展开」成代码，没有运行时开销。
- `#[macro_use] mod macros;`（[src/lib.rs:16-17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L16-L17)）让 `macros` 模块及其子模块里定义的宏，以**文本作用域**方式对整个 crate 后续代码可见。所以你在 `src/unnamed_pipe.rs` 里直接写 `multimacro! { ... }` 不需要 `use`，它是文本层面「在前面声明过」的。
- 宏可以带「分臂」（多个匹配规则，按顺序匹配）和「内部递归」（一个宏调用自己来分解参数）。

### 2.4 Rust I/O 安全与三类句柄 trait（1.63+）

`derive_raw` 宏生成的实现建立在标准库的 I/O 安全体系上，这里只做最小回顾（u5-l2 有详讲）：

| 类别 | Windows trait | Unix trait | 语义 |
|------|---------------|------------|------|
| **安全·借用** | `AsHandle`（`as_handle`） | `AsFd`（`as_fd`） | 借出句柄，不交出所有权 |
| **安全·拥有** | `From<OwnedHandle>` / `From<T> for OwnedHandle` | `From<OwnedFd>` / `From<T> for OwnedFd` | 所有权干净转移 |
| **原始·裸数值** | `AsRawHandle` / `IntoRawHandle` / `FromRawHandle` | `AsRawFd` / `IntoRawFd` / `FromRawFd` | 吐出/吃进一个整数，`FromRaw*` 是 `unsafe` |

记住一条核心线索：interprocess 的「原始数值」能力是**建立在「安全借用」之上**的——先 `as_handle()` 拿到安全借用句柄，再取它的 raw 值。这正是 `derive_raw` 的派生方向（见 4.3）。

### 2.5 `Sealed` 封印

`src/misc.rs` 里有一个 crate 私有的空 trait：`pub(crate) trait Sealed {}`（[src/misc.rs:21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L21)）。许多公共类型会写一句 `impl Sealed for Sender {}`，把它纳入封印体系，防止下游自行实现某些 trait。本讲不展开它的封印用途，只需知道它是 newtype 旁边常见的一行。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/lib.rs` | crate 根，以 `#[macro_use] mod macros;`（[L16-L17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L16-L17)）把宏引入文本作用域 |
| `src/macros.rs` | **宏系统总装**：`multimacro!`、`forward_rbv`、`pinproj_for_unpin`、`builder_setters`、`tag_enum`、`make_macro_modules!`，并挂载所有子模块 |
| `src/macros/forward_iorw.rs` | `forward_sync_read/write`、`forward_sync_ref_read/write`、`forward_tokio_*` —— 为 newtype 转发 `Read`/`Write`（及异步版） |
| `src/macros/forward_handle_and_fd.rs` | `forward_as_handle`/`forward_into_handle`/`forward_from_handle`/`forward_handle`/`forward_try_handle` —— 转发安全句柄转换 |
| `src/macros/forward_fmt.rs` | `forward_debug` —— 转发 `Debug`，可自定义类型名 |
| `src/macros/forward_try_clone.rs` | `forward_try_clone` —— 转发 crate 自定义的 `TryClone` |
| `src/macros/forward_as_ref.rs` | `forward_as_ref`/`forward_as_mut` —— 转发 `AsRef`/`AsMut` |
| `src/macros/forward_to_self.rs` | `forward_to_self` —— 把 trait 方法转发到**同名固有方法** |
| `src/macros/derive_raw.rs` | `derive_asraw`/`derive_intoraw`/`derive_fromraw`/`derive_raw` —— 基于安全句柄**派生**原始句柄 trait |
| `src/macros/derive_mut_iorw.rs` | `derive_sync_mut_read/write` 等 —— 基于 `&Self: Read` **派生** `Self: Read` |
| `src/macros/derive_trivconv.rs` | `derive_trivial_from/into/conv` —— newtype 与其内部字段之间的平凡 `From` 转换 |
| `src/unnamed_pipe.rs` | **实践对象**：公共 `Sender`/`Recver` 的 `multimacro!` 调用（[L62-L101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L62-L101)） |
| `src/os/unix/unnamed_pipe.rs` | Unix 后端 `Sender`/`Recver`，展示了「转发 + 派生」混用的完整链（[L98-L119](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L98-L119)） |

阅读建议：先读 `src/macros.rs` 看总装，再按「转发家族 → 派生家族 → 类型生成」的顺序读子模块，最后回到 `src/unnamed_pipe.rs` 看宏在真实类型上的落地。

---

## 4. 核心概念与源码讲解

### 4.1 宏系统总装：`mod macros` 与 `multimacro!` 调度器

#### 4.1.1 概念说明

interprocess 的宏系统是一棵两层的小树：

- **顶层 `src/macros.rs`**：既定义了若干「顶层宏」（`multimacro!`、`builder_setters!`、`tag_enum!`、`forward_rbv!`、`pinproj_for_unpin!`），又用 `make_macro_modules!` 把九个子模块挂进来。
- **九个子模块**（`src/macros/*.rs`）：按功能聚类，每个文件一组同族宏。

真正让这套系统「好用」的是 `multimacro!` 这个**调度器宏**：它接收一个类型名和一串「宏名」（可带各自参数），然后把同一个类型名依次喂给列表里每个宏。一句话：**`multimacro!` 是「宏的 for 循环」**。

#### 4.1.2 核心流程

整个宏系统的装配与使用流程：

1. **挂载子模块**：`make_macro_modules!` 对每个子模块生成 `#[macro_use] mod X; pub(crate) use X::*;`，既把子模块的宏纳入文本作用域，又 `pub(crate)` 重导出（便于路径引用）。
2. **crate 根引入**：`#[macro_use] mod macros;` 让 `macros` 内部所有宏对整个 crate 可见。
3. **类型作者写一行**：在某个 newtype 旁边写 `multimacro! { MyType, macro_a, macro_b(args), macro_c, }`。
4. **展开**：`multimacro!` 把它展开成等价的顺序调用：

   ```rust
   macro_a!(MyType);
   macro_b!(MyType, args);
   macro_c!(MyType);
   ```

5. **各宏各自展开**：每个被调用的宏再各自展开成一段 `impl Trait for MyType { ... }`。

用伪代码表示 `multimacro!` 的展开规则：

```
multimacro! { T, M1, M2(arg), M3 }
        │  展开（macro_rules 重复捕获 $macro $(($arg))?）
        ▼
M1!(T);
M2!(T, arg);
M3!(T);
```

#### 4.1.3 源码精读

先看顶层 `src/macros.rs` 如何挂载九个子模块（[src/macros.rs:113-125](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L113-L125)）：

```rust
/// Generates this module's macro submodules.
macro_rules! make_macro_modules {
    ($($modname:ident),+ $(,)?) => {$(
        #[macro_use] mod $modname;
        pub(crate) use $modname::*;
    )+};
}

make_macro_modules! {
    derive_raw, derive_mut_iorw, derive_trivconv,
    forward_as_ref, forward_fmt, forward_handle_and_fd, forward_iorw, forward_to_self,
    forward_try_clone,
}
```

说明：`make_macro_modules!` 用 `$(...)+` 重复捕获一组模块名，对每个 `$modname` 生成两行——`#[macro_use] mod $modname;`（声明子模块并让其宏进入文本作用域）和 `pub(crate) use $modname::*;`（重导出）。这正是「宏的宏」：用一个宏去批量声明一堆模块。

再看调度器本体（[src/macros.rs:28-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L28-L46)）：

```rust
/// Calls multiple macros, passing the same identifier or type as well as optional per-macro
/// parameters.
macro_rules! multimacro {
    ($pre:tt $ty:ident, $($macro:ident $(($($arg:tt)+))?),+ $(,)?) => {$(
        $macro!($pre $ty $(, $($arg)+)?);
    )+};
    // ... 另外三个分臂：带 $pre 的 $ty:ty、不带 $pre 的 $ty:ident、不带 $pre 的 $ty:ty ...
    ($ty:ident, $($macro:ident $(($($arg:tt)+))?),+ $(,)?) => {$(
        $macro!($ty $(, $($arg)+)?);
    )+};
}
```

逐 token 拆解最常用的分臂（最后一臂）：

- `$ty:ident`：捕获类型名（如 `Sender`）。
- `$($macro:ident $(($($arg:tt)+))?),+`：捕获**一个或多个**「宏名」，每个宏名后面**可选**地跟一对括号里的参数（如 `forward_debug("local_socket::RecvHalf")`）。
- `$($macro!($ty $(, $($arg)+)?);)+`：对每个捕获到的宏，生成一行 `宏名!(类型, 参数...);`。

四个分臂只是为「类型是 `ident` 还是 `ty`」「前面有没有 `$pre` 前缀 token」提供不同匹配。真实调用点用的都是最简形式 `multimacro! { Type, m1, m2(args), ... }`。

> 小提示：`multimacro!` 自身**不做任何 `impl`**，它只是把调用扇出到列表里的宏。所有真正的代码生成都发生在被调用的那些宏内部。

#### 4.1.4 代码实践

**实践目标**：以公共 `unnamed_pipe::Sender` 为对象，列出 `multimacro!` 为它展开了哪些宏，并说明每个宏贡献了哪个 trait 实现。这正是本讲规格指定的实践任务。

**操作步骤**：

1. 打开 [src/unnamed_pipe.rs:93-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L93-L101)，找到 `Sender` 的定义与 `multimacro!`：

   ```rust
   pub struct Sender(pub(crate) SenderImpl);
   impl Sealed for Sender {}
   multimacro! {
       Sender,
       forward_sync_write,
       forward_handle,
       forward_debug,
       derive_raw,
   }
   ```

2. 把 `multimacro!` 在脑中（或纸上）手工展开成四条顺序调用：

   ```rust
   forward_sync_write!(Sender);
   forward_handle!(Sender);
   forward_debug!(Sender);
   derive_raw!(Sender);
   ```

3. 逐个打开宏定义，填写下表（参考答案见下）。

**预期结果 / 参考答案**：

| 宏调用 | 展开位置 | 贡献的 trait 实现（以 Windows 为例；Unix 把 `*Handle` 换成 `*Fd`） |
|--------|----------|-------------------------------------------------------------------|
| `forward_sync_write!(Sender)` | [forward_iorw.rs:24-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L24-L46) | `impl Write for Sender`（`write`/`flush`/`write_vectored`，委托 `self.0`） |
| `forward_handle!(Sender)` | [forward_handle_and_fd.rs:85-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L85-L98) | 展开 `forward_asinto_handle` + `forward_from_handle`，共三组：<br>• `AsHandle`（[L4-L24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L4-L24)）<br>• `From<Sender> for OwnedHandle`（[L26-L46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L26-L46)）<br>• `From<OwnedHandle> for Sender`（[L48-L68](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L48-L68)） |
| `forward_debug!(Sender)` | [forward_fmt.rs:22-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_fmt.rs#L22-L30) | `impl Debug for Sender`（直接转给 `self.0`） |
| `derive_raw!(Sender)` | [derive_raw.rs:124-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L124-L137) | 展开 `derive_asintoraw` + `derive_fromraw`，共三组：<br>• `AsRawHandle`（[L4-L37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L4-L37)，基于安全 `as_handle()`）<br>• `IntoRawHandle`（[L39-L72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L39-L72)）<br>• `FromRawHandle`（[L89-L122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L89-L122)，`unsafe`） |

**需要观察的现象**：`Sender` 的结构体本体只有一行 `pub struct Sender(pub(crate) SenderImpl);` 加一句 `impl Sealed`，但经这一行 `multimacro!`，它获得了 `Write`、`Debug`、`AsHandle`、`From<OwnedHandle>`（双向）、`AsRawHandle`、`IntoRawHandle`、`FromRawHandle` 共约七组 trait 实现——零手写。

> 说明：以上结论是基于源码静态阅读得出的宏展开结果；若想用工具验证，可在本地用 `cargo expand`（需要 nightly）查看宏展开后的真实代码。本讲不假定你已运行该命令。

#### 4.1.5 小练习与答案

**Q1**：如果把 `multimacro!` 列表里的某个宏名拼错了（比如写成 `forward_write`），会在什么时候报错？
**A1**：在 `multimacro!` **展开后**报错。`multimacro!` 本身只是把 `forward_write!(Sender);` 原样吐出，它不校验宏名是否存在；直到这条 `forward_write!(...)` 被编译器当作宏调用解析时，才会因「找不到该宏」而报错。这也是声明式宏的典型特征——它是文本扇出，不做语义检查。

**Q2**：`multimacro!` 的列表项顺序重要吗？
**A2**：通常不重要，但有一个例外：`forward_rbv`（生成被引用的内部访问器 `refwd`）必须在 `forward_sync_ref_*` / `forward_tokio_ref_*`（这些宏的展开体里会调用 `self.refwd()`）**之前或同处**。事实上 `refwd` 是一个固有方法，只要在「使用它的 impl 被编译时」该方法已存在于该类型即可；在 `src/os/unix/unnamed_pipe.rs` 的调用里 `forward_rbv` 被刻意放在列表最前（见 4.2.3）。顺序对其它纯转发的宏彼此无依赖。

---

### 4.2 `forward_*` 转发宏家族

#### 4.2.1 概念说明

`forward_*` 家族的核心思想一句话：**内部字段 `.0` 已经实现了目标 trait，壳只需把方法原样转发过去**。这是最直接的「壳/芯」搬运。

转发宏的共同特征：

- 展开体里几乎每个方法体都是 `self.0.xxx(...)`（或借用 `self.0`）。
- 它们**不创造新能力**，只是把芯已有能力的「接口面」复制到壳上。
- 名字都以 `forward_` 开头。

典型成员：`forward_sync_read/write`、`forward_sync_ref_read/write`（为 `&T` 实现）、`forward_tokio_read/write`（异步版）、`forward_handle`（安全句柄转换）、`forward_debug`、`forward_try_clone`、`forward_as_ref/as_mut`、`forward_to_self`。

#### 4.2.2 核心流程

转发宏的展开模式高度一致。以 `forward_sync_write` 为例：

```
forward_sync_write!(Sender)
        │
        ▼
impl Write for Sender {
    fn write(&mut self, buf) { self.0.write(buf) }      // 委托 .0
    fn flush(&mut self)      { self.0.flush() }
    fn write_vectored(...)   { self.0.write_vectored(...) }
}
```

句柄家族稍微复杂，因为它是「双向 + 借用」三件套，用**分臂递归**逐层分解：

```
forward_handle!(Sender)
   ├─ forward_asinto_handle!(Sender)   // 「as + into」
   │     ├─ forward_as_handle!(Sender)      // AsHandle
   │     └─ forward_into_handle!(Sender)    // From<Sender> for OwnedHandle
   └─ forward_from_handle!(Sender)     // From<OwnedHandle> for Sender
```

每个「复合宏」（如 `forward_handle`）内部都是对「原子宏」（如 `forward_as_handle`）的调用，并通过 `windows` / `unix` 分臂分别生成平台特定 impl。

引用型 IO（`forward_sync_ref_*`）和异步型（`forward_tokio_*`）需要一个**访问器**把 `&self` 映射到「内部的可读/可写引用」。这个访问器由 `forward_rbv!` 宏生成（见 4.2.3 末尾）。

#### 4.2.3 源码精读

**① 同步 `Read`/`Write` 转发**（[src/macros/forward_iorw.rs:4-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L4-L46)）：

```rust
macro_rules! forward_sync_read {
    ($({$($lt:tt)*})? $ty:ty $(, #[$a1:meta] ...)? ) => {
        impl Read for $ty {
            fn read(&mut self, buf: &mut [u8]) -> Result<usize> { self.0.read(buf) }
            fn read_vectored(&mut self, bufs) -> Result<usize> { self.0.read_vectored(bufs) }
        }
    };
}
```

要点：
- 可选的 `{$($lt:tt)*}` 捕获泛型生命周期参数（用花括号包住，便于在带生命周期的类型上使用）。
- `self.0.read(buf)` 就是全部逻辑——纯委托。注释里写明「不包含 `read_to_end`，因为这个宏不该用在 Chain 之类的适配器上」。

**② 引用型 `Read`/`Write`（为 `&T` 实现）**（[src/macros/forward_iorw.rs:58-76](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L58-L76)）：

```rust
macro_rules! forward_sync_ref_read {
    ($({$($lt:tt)*})? $ty:ty ...) => {
        impl Read for &$ty {
            fn read(&mut self, buf) -> Result<usize> { self.refwd().read(buf) }
            ...
        }
    };
}
```

注意它实现的是 `Read for &$ty`（对**引用**实现），并且方法体是 `self.refwd().read(buf)`——这里调用的 `refwd()` 是由 `forward_rbv!` 生成的固有方法，而不是直接 `self.0`。为什么？因为 `self` 这里是 `&&T`（`&$ty` 的 `&mut self`），需要先解一层引用再拿到内部。`refwd()` 把这个「取内部引用」的细节封装起来。

**③ `refwd` 访问器与智能指针字段**（[src/macros.rs:54-67](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L54-L67)）：

```rust
macro_rules! forward_rbv {
    (@$slf:ident, &) => { &$slf.0 };          // 普通字段：直接借 .0
    (@$slf:ident, *) => { &&*$slf.0 };         // 智能指针字段（如 Arc）：先 Deref 再借
    ($ty:ty, $int:ty, $kind:tt) => {
        impl $ty {
            fn refwd(&self) -> &$int { forward_rbv!(@self, $kind) }
        }
    };
}
```

`$kind` 区分两种字段形态：
- `&`：字段就是普通值，`&self.0` 即可（如 Unix 后端 `Recver(FdOps)`）。
- `*`：字段是智能指针（如 `Arc<Stream>`），需要 `&&*self.0`——先 `*` 解引用 `Arc` 得到内部，再取引用。

> 这个 `refwd` 是 `pub(crate)` 之外的**私有固有方法**，只在宏生成的 impl 内部使用，对用户不可见。

**④ 安全句柄转发家族**（[src/macros/forward_handle_and_fd.rs:4-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L4-L98)）：

以 `forward_as_handle` 的「公共分臂」为例（[L20-L23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L20-L23)）：不指定平台时，它同时展开 `windows` 和 `unix` 两路。每路进入 `@impl` 分臂（[L5-L13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L5-L13)），用 `#[cfg($cfg)]` 给对应平台生成 impl：

```rust
#[cfg(windows)]
impl AsHandle for $ty {
    fn as_handle(&self) -> BorrowedHandle<'_> { AsHandle::as_handle(&self.0) }
}
```

整族结构（都是「委托 `.0`」）：
- `forward_as_handle` → `AsHandle`/`AsFd`（借用，[L4-L24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L4-L24)）
- `forward_into_handle` → `From<$ty> for OwnedHandle`（[L26-L46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L26-L46)）
- `forward_from_handle` → `From<OwnedHandle> for $ty`（[L48-L68](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L48-L68)）
- `forward_asinto_handle`、`forward_handle`（复合）、`forward_try_*`（fallible 版，带 `$ety` 错误类型，[L100-L159](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L100-L159)）。

**⑤ 其它转发宏**：
- `forward_debug`（[forward_fmt.rs:13-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_fmt.rs#L13-L30)）：两臂——带字符串字面量时用辅助函数 `debug_forward_with_custom_name` 打印自定义类型名（如 `"local_socket::RecvHalf"`），不带时直接 `Debug::fmt(&self.0, f)`。
- `forward_try_clone`（[forward_try_clone.rs:3-12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_try_clone.rs#L3-L12)）：转发 crate 自定义的 `TryClone`（[src/try_clone.rs:7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/try_clone.rs#L7)），方法体 `Ok(Self(TryClone::try_clone(&self.0)?))`。
- `forward_as_ref`/`forward_as_mut`（[forward_as_ref.rs:3-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_as_ref.rs#L3-L18)）：`AsRef`/`AsMut` 返回 `&self.0` / `&mut self.0`。
- `forward_to_self`（[forward_to_self.rs:2-39](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_to_self.rs#L2-L39)）：方向相反——把 **trait 方法**转发到**同名固有方法**，用于一个类型既实现 trait 又有同名 inherent 方法的情况。

**⑥ Tokio 异步转发**（[forward_iorw.rs:115-183](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L115-L183)）：异步版的 `poll_read`/`poll_write` 接收 `Pin<&mut Self>`，所以方法体是 `self.$pinproj().poll_read(...)`。这里的 `$pinproj` 默认是 `pinproj`——一个由 `pinproj_for_unpin!` 生成的固有方法。

**⑦ Pin 投影辅助**（[src/macros.rs:17-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L17-L26)）：

```rust
macro_rules! pinproj_for_unpin {
    ($src:ty, $dst:ty) => {
        impl $src {
            fn pinproj(&mut self) -> Pin<&mut $dst> { Pin::new(&mut self.0) }
        }
    };
}
```

因为 `self.0` 是 `Unpin` 的，所以可以安全地把 `&mut self.0` 包成 `Pin<&mut _>`，供 Tokio 的 `poll_*` 使用。注意它和 `forward_rbv` 是一对：`forward_rbv` 服务「按引用」读写，`pinproj_for_unpin` 服务「按 Pin 的可变引用」异步读写。

一个把这套全部用上的真实例子是 Tokio 版匿名管道读端（[src/unnamed_pipe/tokio.rs:45-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L45-L53)）：先 `pinproj_for_unpin(RecverImpl)` 生成投影方法，再 `forward_tokio_read` 使用它。

#### 4.2.4 代码实践

**实践目标**：阅读 Unix 后端 `Recver` 的 `multimacro!`，验证「`forward_rbv` + `forward_sync_ref_read` + `derive_sync_mut_read`」三者如何协作，让一个 newtype 同时拥有 `Read for &Recver` 和 `Read for Recver`。

**操作步骤**：

1. 打开 [src/os/unix/unnamed_pipe.rs:91-105](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L91-L105)，读到：

   ```rust
   pub(crate) struct Recver(FdOps);
   impl Sealed for Recver {}
   impl Debug for Recver { /* 手写：打印 fd */ }
   multimacro! {
       Recver,
       forward_rbv(FdOps, &),
       forward_sync_ref_read,
       forward_try_clone,
       forward_handle,
       derive_sync_mut_read,
   }
   ```

2. 逐项追踪这三者（忽略 `forward_try_clone`/`forward_handle`，它们与 4.1.4 同理）：
   - `forward_rbv(FdOps, &)` → 生成固有方法 `fn refwd(&self) -> &FdOps { &self.0 }`。
   - `forward_sync_ref_read` → 生成 `impl Read for &Recver`，方法体调 `self.refwd().read(buf)`，最终落到 `FdOps` 的 `Read`。
   - `derive_sync_mut_read` → 生成 `impl Read for Recver`，方法体 `(&*self).read(buf)`（见 4.3）——**复用** 上面那条 `&Recver` 的 impl。

3. 在纸上画出依赖链：`FdOps: Read`（芯）→（`forward_sync_ref_read`）→ `&Recver: Read` →（`derive_sync_mut_read`）→ `Recver: Read`。

**需要观察的现象**：壳 `Recver` 本身并不直接对 `self.0` 调 `read`，而是经 `refwd` 走引用 impl，再由派生宏补出按值 impl。这正是「转发」与「派生」**接力**的样板。

**预期结果**：你应能解释为什么这里 `forward_rbv` 必须排在 `forward_sync_ref_read` 之前——后者展开体依赖前者生成的 `refwd` 方法。若想运行验证，可在本地 `cargo expand`（待本地验证）。

#### 4.2.5 小练习与答案

**Q1**：`forward_sync_read!(T)` 和 `forward_sync_ref_read!(T)` 实现的 trait 分别作用在什么「self 类型」上？
**A1**：前者实现 `impl Read for T`（按值/可变借用 self），后者实现 `impl Read for &T`（对引用本身实现）。二者互补：前者让 `T` 可直接 `read`，后者让 `&T` 也能 `read`，这正是 u3-l3 讲过的「按值与按引用都能读写」的类型系统支撑。

**Q2**：为什么 `forward_debug` 有「带字符串字面量」和「不带」两个分臂？
**A2**：不带时，直接打印内部字段（`Debug::fmt(&self.0, f)`），输出形态与芯一致；带字面量时（如 `forward_debug("local_socket::RecvHalf")`），调用辅助函数 `debug_forward_with_custom_name`，把外壳打印成自定义名字——因为有些 newtype 是平台私有实现，用户在 `Debug` 输出里看到的应当是稳定的公共类型名，而非内部实现名。见 [src/os/windows/named_pipe/local_socket/stream.rs:150](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L150) 的真实用法。

---

### 4.3 `derive_*` 派生宏家族

#### 4.3.1 概念说明

`derive_*` 家族与 `forward_*` 形成本讲最关键的对照。它们的名字虽也叫「derive」，但**不是** Rust 内置的 `#[derive]`，而是 interprocess 自造的 `macro_rules!`。

核心区别：

| | `forward_*`（转发） | `derive_*`（派生） |
|---|---|---|
| 前提 | `.0` **已有**目标 trait | `.0` 没有，但有**相关基础能力** |
| 方法体 | `self.0.xxx()` | 基于基础能力**拼装**出新调用 |
| 方向 | 壳 ← 芯（搬运） | 从一个能力**构造**另一个能力 |

三个典型派生方向：

1. **从「安全句柄」派生「原始句柄」**（`derive_raw`）：`as_raw_handle` = `as_handle().as_raw_handle()`。
2. **从「`&T: Read`」派生「`T: Read`」**（`derive_mut_iorw`）：`read` = `(&*self).read()`。
3. **从「内部字段」派生「与 newtype 的平凡互转」**（`derive_trivconv`）：`from(src) = Self(src)` / `from(w) = w.0`。

#### 4.3.2 核心流程

**① `derive_raw` 的派生链**（[src/macros/derive_raw.rs:124-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L124-L137)）：

```
derive_raw!(Sender)
   ├─ derive_asintoraw!(Sender)
   │     ├─ derive_asraw!(Sender)     // AsRawHandle = as_handle().as_raw_handle()
   │     └─ derive_intoraw!(Sender)   // IntoRawHandle = OwnedHandle::from(self).into_raw_handle()
   └─ derive_fromraw!(Sender)         // FromRawHandle = unsafe { Self::from(FromRawHandle::from_raw_handle(fd)) }
```

关键洞察：`derive_asraw` 的实现体（[derive_raw.rs:10-17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L10-L17)）是：

```rust
fn as_raw_handle(&self) -> RawHandle {
    let h = AsHandle::as_handle(self);   // 先拿安全借用句柄
    AsRawHandle::as_raw_handle(&h)        // 再取其 raw 值
}
```

也就是说，「原始数值」能力**建立在安全 I/O 之上**——它要求类型先实现 `AsHandle`（由 `forward_handle` 提供），`derive_raw` 才能工作。这就是 4.1.4 里 `Sender` 同时挂 `forward_handle` 和 `derive_raw` 的原因：**前者是后者的地基**。

**② `derive_mut_iorw` 的派生方向**（[src/macros/derive_mut_iorw.rs:4-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_mut_iorw.rs#L4-L21)）：

```rust
macro_rules! derive_sync_mut_read {
    ($({$($lt:tt)*})? $ty:ty) => {
        impl Read for $ty {
            fn read(&mut self, buf: &mut [u8]) -> Result<usize> { (&*self).read(buf) }
            ...
        }
    };
}
```

方法体是 `(&*self).read(buf)`——它**不**碰 `self.0`，而是把 `self` 退化成 `&Self`，调用「为 `&Self` 实现的 `Read`」。前提：`&Self: Read` 已经存在（通常由 `forward_sync_ref_read` 提供）。模块注释点明了这一点：*「derive `Read`/`Write` on all `T` that satisfy `for<'a> &'a T: Trait`」*。

**③ `derive_trivconv` 的派生方向**（[src/macros/derive_trivconv.rs:4-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_trivconv.rs#L4-L27)）：

```rust
macro_rules! derive_trivial_from {  // From<Inner> for Wrapper = Self(src)
    ($dst:ty, $src:ty) => { impl From<$src> for $dst { fn from(src) -> Self { Self(src) } } };
}
macro_rules! derive_trivial_into { // From<Wrapper> for Inner = src.0
    ($src:ty, $dst:ty) => { impl From<$src> for $dst { fn from(src) -> Self { src.0 } } };
}
macro_rules! derive_trivial_conv { // 两个方向都生成
    ($ty1, $ty2) => { derive_trivial_from!($ty1, $ty2); derive_trivial_into!($ty1, $ty2); };
}
```

它派生的是「newtype 与其内部字段之间的平凡互转」。注意它需要类型字段可访问（`.0`），所以常用于「后端内部」的 newtype 包装。

#### 4.3.3 源码精读

**`derive_raw` 全景**（[src/macros/derive_raw.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs)）：

- `derive_asraw`（[L4-L37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L4-L37)）：`AsRaw*` 基于 `As*`。
- `derive_intoraw`（[L39-L72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L39-L72)）：`IntoRaw*` 先把 `self` 转成 `Owned*`（用 `From`），再调 `into_raw_*`。
- `derive_fromraw`（[L89-L122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L89-L122)）：`FromRaw*`，**整个方法是 `unsafe fn`**，内部用 `unsafe { FromRawHandle::from_raw_handle(fd) }` 得到 `OwnedHandle`，再用 `From::from` 包回 newtype。这与 u5-l2「子进程用裸数值重建 I/O 对象」直接相关。

每个宏都有 `windows` / `unix` / 不指定 三个分臂，结构完全对称（Windows 走 `Handle`，Unix 走 `Fd`）。

**真实派生用例 1**：UDS 后端的 `Stream` newtype（[src/os/unix/uds_local_socket/stream.rs:143-147](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L143-L147)）：

```rust
multimacro! {
    Stream,
    forward_asinto_handle(unix),
    derive_sync_mut_rw,
}
```

这里 `Stream` 是包着 `Arc` 的半流类型。`forward_asinto_handle(unix)` 提供 `AsFd` + `From<Stream> for OwnedFd`；`derive_sync_mut_rw` 则从「`&Stream: Read`」（由别处提供）派生出「`Stream: Read/Write`」。

**真实派生用例 2**：Windows local socket 后端 `Stream`（[src/os/windows/named_pipe/local_socket/stream.rs:126-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L126-L137)）：

```rust
multimacro! {
    Stream,
    forward_rbv(StreamImpl, &),
    forward_as_ref(StreamImpl),
    forward_as_mut(StreamImpl),
    forward_sync_read,
    forward_sync_ref_read,
    forward_as_handle,
    forward_try_clone,
    derive_sync_mut_write,
    derive_trivial_conv(StreamImpl),
}
```

注意它**只**派生了 `Write`（`derive_sync_mut_write`）而没有派生 `Read`——因为 `Read` 已由 `forward_sync_read`（基于 `self.0`）直接转发，而 `Write` 则走「先 `&Self: Write`、再派生 `Self: Write`」的路线（与该后端的引用共享写设计配套）。`derive_trivial_conv(StreamImpl)` 则补出 `Stream` 与 `StreamImpl` 之间的双向 `From`。

> 小结：同一个类型上，`forward_*` 与 `derive_*` 常常**混用**——转发负责「芯已有能力」，派生负责「在转发结果上再造一层」。这正是 interprocess 用极少手写代码撑起庞大 trait 面的秘诀。

#### 4.3.4 代码实践

**实践目标**：动手写一个最小 newtype，复刻 `derive_raw` 与 `derive_trivconv` 的派生思路（不依赖 interprocess 内部宏，用普通 Rust 验证「派生 = 在基础能力上拼装」）。

**操作步骤**：

1. 新建一个独立的小 crate 或在 `examples/` 旁写一个临时文件（**示例代码**，非项目原有代码）：

   ```rust
   // 示例代码：手动复刻 forward + derive 的关系
   use std::ops::Deref;

   // 「芯」：已实现基础能力
   struct Core;
   impl Core { fn value(&self) -> i32 { 42 } }

   // 「壳」：newtype
   struct Shell(Core);

   // ① forward 风格：直接委托 .0（等价 forward_* 的思路）
   impl Shell {
       fn value(&self) -> i32 { self.0.value() }   // self.0.xxx()
   }

   // ② derive 风格：基于已有能力「拼装」新能力（等价 derive_* 的思路）
   //    假设我们想要 Deref，但「芯」没有 Deref——我们基于 Shell.value() 自己造
   impl Deref for Shell {
       type Target = i32;
       fn deref(&self) -> &i32 {
           // 不能返回对临时值的引用，这里仅示意「拼装」方向：
           // 真实 derive_raw 是 self.as_handle().as_raw_handle() 这种链式拼装。
           unimplemented!("示意：derive = 在已有方法链上构造新 trait")
       }
   }
   ```

2. 对照本节定义，在注释里标注：`Shell::value` 属于 **forward**（委托 `.0`），而 `Deref` 属于 **derive**（在 `value()` 之上构造）。真实 `derive_raw` 的 `as_raw_handle` 就是 `self.as_handle().as_raw_handle()`——一条两步的方法链，而非单步委托。

3. 回到 [src/macros/derive_raw.rs:10-17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L10-L17)，确认 `derive_asraw` 的方法体确实是「先 `as_handle`、再 `as_raw_handle`」的两步链，符合「派生 = 拼装」的定义。

**需要观察的现象**：你能清楚说出每个宏生成的方法体是「一步委托」（forward）还是「多步拼装 / 借助另一 impl」（derive）。

**预期结果**：建立起判定准则——看到方法体形如 `self.0.f()` 是 forward；形如 `(&*self).f()`、`a().b()`、`Self(x.0)` 的是 derive。

> 说明：示例代码只用于理解概念，不保证编译通过（`Deref` 示意返回引用的方式不合法）。请勿把它加入项目。

#### 4.3.5 小练习与答案

**Q1**：`derive_fromraw` 生成的 `from_raw_handle` 为什么是 `unsafe fn`，而 `forward_from_handle` 生成的 `From<OwnedHandle>` 不是？
**A1**：`from_raw_handle` 接收的是一个**裸整数**，编译器无法保证它指向有效、未被重复占有的内核对象——调用者必须亲自担保「数值有效、所有权唯一、语义匹配」，故为 `unsafe`。而 `From<OwnedHandle>` 在两个**拥有所有权的安全类型**之间转移，所有权语义由类型系统保证，无需 `unsafe`。这正是 u5-l2「子进程重建」必须用 `unsafe` 的根因。

**Q2**：如果对一个类型只调用 `derive_raw!` 而不先调用 `forward_handle!`，会发生什么？
**A2**：会编译失败。`derive_asraw` 的展开体里调用了 `AsHandle::as_handle(self)`，要求该类型实现 `AsHandle`；而 `AsHandle` 是由 `forward_handle`（更精确地是其子宏 `forward_as_handle`）提供的。所以 `forward_handle` 是 `derive_raw` 的**必要前置**——「派生」必须建立在「基础能力」已存在的前提上。

---

### 4.4 类型生成辅助：`tag_enum` 与 `builder_setters`

#### 4.4.1 概念说明

除了「为 newtype 缝 trait」的 forward/derive 家族，宏系统还有两个「**生成类型本身**」的辅助宏：

- **`tag_enum!`**：生成一个**公开、封印、无人居住（无变体）的标记枚举**，并附带一批「无用但必需」的 trait 实现（`Copy`/`Clone`/`Debug`/`PartialEq`/`Eq`）。它用于 local socket 的 name type 标记类型（如 `GenericFilePath`、`GenericNamespaced`）。
- **`builder_setters!`**：为构建器结构体**批量生成「按值 setter」**——每个字段一个返回 `Self` 的链式方法，自动加上 `#[must_use]` 提醒。

这两个宏的共同点：它们生成的是**新的公开 API 表面**（类型定义、方法定义），而非 trait 实现。

#### 4.4.2 核心流程

**`tag_enum!` 流程**：

```
tag_enum!( GenericFilePath )   // 也可带文档注释、多个标签
        │
        ▼
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum GenericFilePath {}     // 空枚举：无实例
impl Sealed for GenericFilePath {}
```

空枚举（`enum X {}`）没有任何变体，因此**无法被实例化**，它的唯一用途是**当作类型层面的标签**（type-level tag）——出现在泛型参数里，用于在编译期区分不同的「名称类型」。给它派生 `Copy/Clone/Debug` 等 trait 是为了满足某些泛型约束（即便永远没有实例，trait 仍需存在）。

**`builder_setters!` 流程**：

```
impl<'n> ListenerOptions<'n> {
    builder_setters! {
        /// Sets the name the server will listen on.
        name: Name<'n>,
    }
}
        │
        ▼
impl<'n> ListenerOptions<'n> {
    /// Sets the name the server will listen on.
    #[must_use = "builder setters take the entire structure and return it with the corresponding field modified"]
    #[inline(always)]
    pub fn name(mut self, name: Name<'n>) -> Self {
        self.name = name.into();
        self
    }
}
```

每个字段展开成一个标准的「消费 self、改字段、返回 self」的 builder 方法，并自动：
- 用 `.into()` 赋值（允许参数类型到字段类型的 `Into` 转换）；
- 加 `#[must_use]`，提示别忘记链式接住返回值。

#### 4.4.3 源码精读

**`tag_enum!`**（[src/macros.rs:102-111](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L102-L111)）：

```rust
/// Creates a public sealed uninhabited type with a bunch of unnecessary trait implementations.
macro_rules! tag_enum {
    ($($(#[$attr:meta])* $tag:ident),+ $(,)?) => {$(
        $( #[$attr] )*
        #[derive(Copy, Clone, Debug, PartialEq, Eq)]
        pub enum $tag {}
        #[allow(deprecated)]
        impl $crate::Sealed for $tag {}
    )+};
}
```

注释里的 *「unnecessary trait implementations」* 点明了这些 derive 是「无用但必需」的——空枚举没有实例，但泛型边界可能要求这些 trait。`$(#[$attr:meta])*` 允许给每个标签附加文档或属性。真实用法见 [src/local_socket/name/type.rs:64-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L64-L78)（`GenericFilePath`）与 [L93](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L93)（`GenericNamespaced`），以及平台私有 `src/os/unix/local_socket/name_type.rs`、`src/os/windows/local_socket/name_type.rs`。

**`builder_setters!`**（[src/macros.rs:69-100](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L69-L100)）：三个分臂层层递进——最底层是「带文档的单字段」（[L76-L85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L76-L85)），中间层「无文档的单字段」会自动用 `concat!` 生成默认文档（[L86-L96](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L86-L96)），最顶层「多字段」对每个字段调用一次单字段版（[L97-L99](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L97-L99)）。配套的 `builder_must_use!`（[L69-L71](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L69-L71)）集中存放 `#[must_use]` 的提示文案，避免重复。

真实用法：[src/local_socket/listener/options.rs:80-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L80-L85) 为 `ListenerOptions` 生成 `name` setter；[src/local_socket/stream/options.rs:53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L53)、[src/os/windows/named_pipe/listener/options.rs:130](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/options.rs#L130) 同理。注意 `ListenerOptions` 里**不是所有** setter 都用宏——像 `nonblocking`（涉及位标志位运算）这种「改字段方式特殊」的就手写（[src/local_socket/listener/options.rs:86-94](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L86-L94)），只有「直接赋值」的简单字段才用 `builder_setters!`。这是一个很实用的取舍：**宏只覆盖最规整的样板，特殊逻辑仍手写**。

#### 4.4.4 代码实践

**实践目标**：对比 `ListenerOptions` 中「用宏生成」与「手写」的 setter，理解宏的适用边界。

**操作步骤**：

1. 打开 [src/local_socket/listener/options.rs:80-103](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L80-L103)。
2. 找到 `builder_setters! { name: Name<'n> }`（用宏），以及紧随其后手写的 `nonblocking` 和 `reclaim_name`。
3. 观察手写版的差异：
   - `name`：`self.name = name.into(); self;`（标准赋值，故能用宏）。
   - `nonblocking`：`self.flags = (self.flags & (ALL_BITS ^ NONBLOCKING_BITS)) | nonblocking as u8;`（位标志位运算，无法用宏）。
   - `reclaim_name`：`self.flags = set_bit(self.flags, SHFT_RECLAIM_NAME, reclaim_name);`（单 bit 操作，无法用宏）。
4. 在纸上把三个 setter 的「字段写入方式」列成表。

**需要观察的现象**：宏 `builder_setters!` 只能处理 `self.field = value.into()` 这种「直接赋值」；任何需要位运算、条件、跨字段副作用的 setter 都必须手写。

**预期结果**：你应能判断「这个 setter 能否用 `builder_setters!` 生成」——判据是它的字段写入是否为「单字段、直接赋值、带 `.into()`」。

#### 4.4.5 小练习与答案

**Q1**：`tag_enum!` 生成的枚举为什么是「空的」（无变体）？它有什么用？
**A1**：因为它只是**类型层面的标签**（type-level tag），不需要、也不应该有运行时实例。空枚举 `enum X {}` 无法被构造，零开销；它的价值是出现在泛型参数（如 `NameType` 的实现者）里，让编译器在类型层面区分「文件路径名」与「命名空间名」等不同类别（见 u2-l4）。给它派生 `Copy/Clone/Debug` 是为了满足泛型约束，而非运行时使用。

**Q2**：`builder_setters!` 生成的 setter 为什么都带 `#[must_use]`？
**A2**：因为这些 setter 是「按值消费 `self` 并返回修改后的 `Self`」（builder 模式）。如果调用者写了 `opts.name(x);` 却没接住返回值，由于 `self` 被消费、原 `opts` 已失效，这次设置就会**静默丢失**。`#[must_use]` 让编译器在这种情况下发出警告，避免「设了不生效」的隐蔽 bug。文案由 `builder_must_use!` 统一提供。

---

## 5. 综合实践

把本讲四块内容串起来，完成下面这个**追踪 + 归类**任务。

**任务**：选取 **Windows local socket 后端的 `Stream`**（[src/os/windows/named_pipe/local_socket/stream.rs:126-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L126-L137)），完成：

1. **展开 `multimacro!`**：把列表里的 8 个宏调用，手工写成 8 条独立的 `宏!(Stream, args);`。
2. **归类**：把每条宏归入下表三类之一，并写出它贡献的 trait / 方法。

   | 类别 | 宏 | 贡献 |
   |------|----|------|
   | 访问器辅助 | | |
   | forward（转发） | | |
   | derive（派生） | | |

3. **画依赖图**：标注哪些宏之间存在「地基—派生」或「访问器—使用者」的依赖。例如 `forward_rbv` 是 `forward_sync_ref_read` 的访问器提供者；`derive_sync_mut_write` 依赖 `&Stream: Write` 已存在。
4. **回答**：为什么这个 `Stream` 上有 `forward_sync_read`（转发读）却用 `derive_sync_mut_write`（派生写），而不是对称地都用 forward？

**参考要点**（先自己做完再看）：

- 访问器辅助：`forward_rbv(StreamImpl, &)` → 生成 `refwd`。
- forward：`forward_as_ref`/`forward_as_mut`（`AsRef`/`AsMut`）、`forward_sync_read`（`Read for Stream`，委托 `self.0`）、`forward_sync_ref_read`（`Read for &Stream`，经 `refwd`）、`forward_as_handle`（`AsHandle`）、`forward_try_clone`（`TryClone`）。
- derive：`derive_sync_mut_write`（从 `&Stream: Write` 派生 `Stream: Write`）、`derive_trivial_conv(StreamImpl)`（`Stream ↔ StreamImpl` 的双向 `From`）。
- 第 4 问的关键：该后端的「写」被设计成「多个 `&Stream` 共享同一底层」的引用共享写（配合 `MaybeArc`，见 u8-l2），所以写的入口是 `&Stream: Write`，再由 derive 补出 `Stream: Write`；而「读」直接转发 `self.0` 即可，故用 forward。这体现了 forward/derive 的选择**反映了类型的并发/共享设计**，而非随意。

> 这个综合实践把「总装调度（4.1）+ 转发家族（4.2）+ 派生家族（4.3）+ 类型生成边界（4.4）」全部用上。若想验证展开，可在本地对 Windows target 运行 `cargo expand`（待本地验证）。

---

## 6. 本讲小结

- interprocess 的宏系统是一棵两层小树：`src/macros.rs` 总装，九个 `src/macros/*.rs` 子模块按族聚类，经 `make_macro_modules!` 挂载、`#[macro_use] mod macros;` 进文本作用域。
- **`multimacro!` 是「宏的 for 循环」**：它把一个类型名 + 一串宏名（可带各自参数）扇开成顺序的宏调用，自身不生成任何 impl。
- **`forward_*`（转发）**：前提是 `.0` 已有目标 trait，方法体形如 `self.0.f()`；代表：`forward_sync_read/write`、`forward_sync_ref_*`、`forward_tokio_*`、`forward_handle`（含 `as/into/from` 三件套）、`forward_debug`、`forward_try_clone`、`forward_as_ref`。
- **`derive_*`（派生）**：`.0` 没有目标 trait，但有相关基础能力，方法体是「拼装 / 借助另一 impl」；代表：`derive_raw`（在安全 `As*` 之上派生 `AsRaw*/IntoRaw*/FromRaw*`）、`derive_mut_iorw`（在 `&T: Read` 之上派生 `T: Read`）、`derive_trivconv`（newtype ↔ 内部字段的平凡 `From`）。
- 两个**访问器辅助宏**支撑了转发家族：`forward_rbv!` 生成 `refwd`（服务引用型 IO）、`pinproj_for_unpin!` 生成 `pinproj`（服务 Tokio 的 `Pin<&mut Self>`）。
- 两个**类型生成宏**：`tag_enum!` 生成无人居住的封印标记枚举（name type 标签），`builder_setters!` 批量生成「按值 setter」——但只覆盖「直接赋值」的规整字段，位运算等特殊 setter 仍手写。
- forward 与 derive 常在同一类型上**混用**，且存在「地基—派生」依赖（如 `forward_handle` 是 `derive_raw` 的前提）；选择哪种往往反映类型的共享/并发设计。

---

## 7. 下一步学习建议

- **u7-l2（句柄/FD 抽象与所有权管理）**：本讲的 `forward_handle`/`derive_raw` 生成了整套句柄 trait，下一篇将深入 `try_clone`、Windows 的 `ShareHandle`/`adv_handle`，讲清「克隆句柄」背后的 `DuplicateHandle`/`dup` 系统调用与所有权模型。
- **u7-l3（错误处理体系）**：`forward_try_clone`/`forward_try_handle` 里出现的 `io::Error`、`derive_fromraw` 的 `unsafe` 边界，都与错误处理紧密相关；下一篇系统讲 `ConversionError`、`ReuniteError` 的所有权归还设计。
- **回看 u2-l2（enum dispatch）**：本讲聚焦 newtype 的 forward/derive；local socket 枚举用的是另一套 `mkenum!`/`dispatch!` 宏（在 `src/local_socket/enumdef.rs`），它们与 `multimacro!` 是互补关系——一个服务 enum 派发，一个服务 newtype 转发。建议对照阅读，看清「宏系统」的全貌。
- **动手加深印象**：在本地对 `src/unnamed_pipe.rs` 跑一次 `cargo expand`（nightly），亲眼看 `multimacro!` 展开成的一长串 `impl`，会比读讲义更直观。
