# impmod 与平台后端注入

## 1. 本讲目标

本讲承接 u2-l1（local socket 的「trait + enum」双层设计）与 u2-l2（`mkenum`/`dispatch!` 宏），把目光从 **local socket 的 enum 派发** 转到 **unnamed pipe（匿名管道）的类型注入**。

二者解决的是同一个问题：公共层代码必须平台无关，而真正干活的实现只能存在于 `os::unix` 或 `os::windows` 之下。local socket 用「枚举 + dispatch 宏」做派发；unnamed pipe 用更朴素的方式——**用一个宏 `impmod!` 把平台后端的类型和函数以统一别名「注射」进公共模块**，再用公共 newtype 把它包起来。

学完本讲你应该能够：

- 看懂 `impmod!` 宏的两条 `match` 臂，并能手写它展开后的 `#[cfg]` `use` 语句；
- 画出从公共 `unnamed_pipe::pipe()` 到平台 `pipe_impl()` 的完整调用链，说清楚每一层包装了什么；
- 理解公共 `Sender`/`Recver` 是「壳」，平台后端类型是「芯」，以及为什么壳要靠 `multimacro!` 批量转发 trait；
- 区分 `impmod!`（注入实现）和 `multimacro!`（批量转发 trait）这两把宏各自的职责，并看清同一套机制如何在同步与 Tokio 异步两套公共层上复用。

## 2. 前置知识

在进入源码前，先澄清几个本讲反复用到的概念。

- **newtype（新类型）模式**。Rust 里写 `struct Recver(Inner)`（一个字段的元组结构体）会把 `Inner` 包成全新的类型 `Recver`，二者在类型系统里不互通，但内存布局一致、零开销。interprocess 用它给平台私有类型套一层公共外壳：外面是稳定的公共 API，里面是会随平台变化的实现。本讲大量出现的 `self.0` 就是指「newtype 的第 0 个字段」，也就是被包住的那个内部类型。

- **`#[cfg]` 条件编译**。`#[cfg(unix)]` / `#[cfg(windows)]` 让编译器在编译期按目标平台保留或丢弃代码。在任一次编译里，`cfg(unix)` 与 `cfg(windows)` **恰有一个为真**。这是「单后端、零运行时派发」的物理基础：另一平台的代码根本不存在于最终的二进制里。

- **`use ... as` 别名导入**。`use foo::Bar as Baz` 把路径 `foo::Bar` 在当前模块里改名为 `Baz`。`impmod!` 的核心技巧就是：对 Unix 和 Windows 各写一条 `use`，但都改名为**同一个别名**（如 `RecverImpl`），于是下游代码只要写 `RecverImpl`，编译器会按当前平台解析到正确的后端类型。

- **转发宏（forwarding macro）**。一类「为 newtype 生成 trait 实现、内部转调 `self.0` 同名方法」的宏，例如 `forward_sync_read!(Recver)` 会生成 `impl Read for Recver { fn read(...) { self.0.read(...) } }`。u2-l2 里 local socket 的枚举用的是 `dispatch_read!`，本讲 unnamed pipe 用的是 `forward_*` 系列，二者目的一致、手法不同。

- **`macro_rules!` 的文本展开**。声明式宏在编译期做「文本到文本」的替换，不做类型检查（类型检查在展开后由编译器统一进行）。理解 `impmod!` 只需要把它当成「按模板生成两条 `use` 语句」即可。

一句话概括本讲的分层观：**公共层是壳，平台后端是芯，`impmod!` 是把芯塞进壳的注射器，`multimacro!` 是给壳批量贴上 trait 标签的流水线。**

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 本讲看点 |
|---|---|---|
| [src/macros.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs) | 宏的总装车间 | 定义 `impmod!`、`multimacro!` 等贯穿全库的私有宏 |
| [src/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs) | **公共**同步管道模块 | 用 `impmod!` 注入后端，定义公共 `Sender`/`Recver` newtype 与 `pipe()` |
| [src/unnamed_pipe/tokio.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs) | **公共**异步管道模块 | 同样模式，但转发的是 Tokio 的 `AsyncRead`/`AsyncWrite` |
| [src/os/unix/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs) | Unix 后端 | 提供 `Recver`/`Sender`/`pipe_impl`，基于 `libc::pipe` |
| [src/os/windows/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs) | Windows 后端 | 提供 `Recver`/`Sender`/`pipe_impl`，基于 `CreatePipe` |
| [src/macros/forward_iorw.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs) | 转发宏子模块 | `forward_sync_read`/`forward_tokio_read` 等定义 |
| [src/macros/forward_handle_and_fd.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs) | 转发宏子模块 | `forward_handle`/`forward_try_handle` 等定义 |
| [src/macros/derive_raw.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs) | 派生宏子模块 | `derive_raw`/`derive_asraw` 定义 |

> 说明：后缀为 `forward_*.rs`、`derive_*.rs` 的文件是 `macros` 模块用 `make_macro_modules!` 自动挂载的子模块（见 [src/macros.rs:114-125](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L114-L125)）。本讲只用到其中少数几个，宏系统全景留待 u7-l1。

---

## 4. 核心概念与源码讲解

### 4.1 `impmod!`：平台后端的别名注入（macros 模块）

#### 4.1.1 概念说明

`impmod!` 是 interprocess 全库的「平台后端注射器」。它解决一个反复出现的痛点：

> 公共模块（如 `src/unnamed_pipe.rs`）想用平台后端的类型 `Recver`、`Sender` 和函数 `pipe_impl`，但这些符号在 Unix 下属于 `crate::os::unix::unnamed_pipe`，在 Windows 下属于 `crate::os::windows::unnamed_pipe`。公共模块不想、也不该出现平台判断。

`impmod!` 的做法是：**一次性生成两条带 `#[cfg]` 的 `use`，把两个平台的同名符号都改名为统一别名**。由于 `cfg(unix)` 与 `cfg(windows)` 互斥，编译期只会留下一条，别名因此唯一地解析到当前平台的后端。

它和 u2-l2 里 local socket 用的 `impmod!` 是**同一个宏**——u2-l2 里它注入的是 `dispatch_sync::connect` 这类后端派发函数；本讲 unnamed pipe 用它注入的是**后端类型 + 创建函数**。`impmod!` 是全库「壳/芯」分层共用的那把注射器。

#### 4.1.2 核心流程

`impmod!` 的展开逻辑可以用一句话描述：

```
输入：模块路径 + 一组「原名 as 别名」（别名可省略）
   │
   ├─ 生成 #[cfg(unix)]    use crate::os::unix::<路径>::{ <原名 as 别名>, ... };
   └─ 生成 #[cfg(windows)] use crate::os::windows::<路径>::{ <原名 as 别名>, ... };
```

编译期，两条 `use` 中恰好一条被保留，别名（如 `RecverImpl`、`SenderImpl`、`pipe_impl`）就被绑定到当前平台的真实符号。

原理上，设平台选择函数 \(\sigma(\text{target}) \in \{\text{unix},\text{windows}\}\) 在一次编译中取唯一值，则别名 `RecverImpl` 解析到的路径唯一：

\[
\texttt{RecverImpl} \;\equiv\; \texttt{crate::os::}\,\sigma(\text{target})\texttt{::unnamed\_pipe::Recver}
\]

没有运行时分支、没有 `dyn`、没有 match——派发的代价在编译期就被 `cfg` 消化了。

#### 4.1.3 源码精读

`impmod!` 定义在 [src/macros.rs:3-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L3-L14)：

```rust
/// Dispatches to a symmetrically named submodule in the target OS module.
macro_rules! impmod {
    ($($osmod:ident)::+ $(as $into:ident)?) => {
        impmod!($($osmod)::+, self $(as $into)?);
    };
    ($($osmod:ident)::+, $($orig:ident $(as $into:ident)?),* $(,)?) => {
        #[cfg(unix)]
        use $crate::os::unix::$($osmod)::+::{$($orig $(as $into)?,)*};
        #[cfg(windows)]
        use $crate::os::windows::$($osmod)::+::{$($orig $(as $into)?,)*};
    };
}
```

两条臂各司其职（[src/macros.rs:5-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L5-L13)）：

- **第一条臂（行 5-7）**：只给一个模块路径（可选 `as`），它递归调用第二条臂、把整个子模块本身（`self`）当作要导入的符号。用于「把平台子模块整体改名引入」的简单场景（local socket 的 `dispatch_sync` 就是这样整体引入的，见 u2-l2）。
- **第二条臂（行 8-13）**：核心臂。`$($osmod:ident)::+` 匹配模块路径（如 `unnamed_pipe` 或 `unnamed_pipe::tokio`），`$($orig:ident $(as $into:ident)?),*` 匹配一串「原名 + 可选别名」。展开后同时吐出 `#[cfg(unix)]` 和 `#[cfg(windows)]` 两条 `use`，路径前缀分别是 `os::unix` 与 `os::windows`，其余完全对称。

> 注意 `$crate`：它会被展开成当前 crate 的名字（这里是 `interprocess`），保证宏即便被其它 crate 引用也能定位到正确的模块。这是声明式宏里保证路径绝对性的标准写法。

实际调用在公共管道模块 [src/unnamed_pipe.rs:26-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L26-L30)：

```rust
impmod! {unnamed_pipe,
    Recver as RecverImpl,
    Sender as SenderImpl,
    pipe_impl,
}
```

它命中的是第二条臂（逗号分隔的符号列表）。手写展开后等于：

```rust
#[cfg(unix)]
use crate::os::unix::unnamed_pipe::{
    Recver as RecverImpl,   // 公共层里的「芯」
    Sender as SenderImpl,
    pipe_impl,              // 公共 pipe() 要调用的后端创建函数
};
#[cfg(windows)]
use crate::os::windows::unnamed_pipe::{
    Recver as RecverImpl,
    Sender as SenderImpl,
    pipe_impl,
};
```

于是公共模块里的 `RecverImpl`、`SenderImpl`、`pipe_impl` 三个名字就有了确切定义，且**对 Unix/Windows 而言指向各自的后端**。`pipe_impl` 没写 `as`，所以原名不变。

异步版同理，路径多了一段 `::tokio`，见 [src/unnamed_pipe/tokio.rs:8-12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L8-L12)：它注入的是 `crate::os::unix::unnamed_pipe::tokio::{...}` 与 Windows 对应路径。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `impmod!` 的展开结果，建立「别名 = 当前平台后端」的直觉。

**操作步骤**：

1. 打开 [src/unnamed_pipe.rs:26-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L26-L30) 的 `impmod!` 调用。
2. 在纸上按本讲 4.1.3 的模板，手写它展开后的两条 `use`。
3. 核对：你在 Unix 上的 `RecverImpl` 应该解析到 [src/os/unix/unnamed_pipe.rs:91](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L91) 的 `pub(crate) struct Recver(FdOps);`；在 Windows 上应解析到 [src/os/windows/unnamed_pipe.rs:104](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L104) 的 `pub(crate) struct Recver(AdvOwnedHandle);`。
4. （可选）运行 `cargo expand -p interprocess`（需安装 `cargo-expand`）查看 `unnamed_pipe` 模块展开后的真实 `use`。

**需要观察的现象**：展开结果里只有一条 `use` 被保留（取决于你的目标平台），另一条 `#[cfg]` 不满足的 `use` 消失；`RecverImpl` 是个指向后端类型的别名。

**预期结果**：别名在编译期唯一绑定到当前平台后端，没有任何运行时判断。

> 若本地未安装 `cargo-expand`，步骤 4 标注为「待本地验证」；前三步是纯源码阅读，不依赖任何工具即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `impmod!` 调用里的 `Recver as RecverImpl` 改成不写别名（即 `Recver,`），公共模块会出现什么问题？

**参考答案**：那么注入进来的名字就叫 `Recver`，会和公共模块自己在 [src/unnamed_pipe.rs:62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L62) 定义的公共 newtype `struct Recver(pub(crate) RecverImpl)` **同名冲突**，编译报「重复定义」。`as RecverImpl` 的别名正是为了避免这种「壳和芯同名」的碰撞——后端类型改名成 `*Impl`，公共类型保留原名。

**练习 2**：为什么 `impmod!` 生成的两条 `use` 不会触发「未使用导入」告警？

**参考答案**：两条 `use` 各自带 `#[cfg(unix)]` / `#[cfg(windows)]`，在任一目标上只有一条被编译，另一条根本不存在于编译产物中；而留下来的那条里每个别名都会被公共层的 newtype（`RecverImpl`/`SenderImpl`）或 `pipe()`（`pipe_impl`）用到，所以既无多余、也无缺失。

---

### 4.2 公共 `Sender`/`Recver` 如何包装平台实现（unnamed_pipe 模块）

#### 4.2.1 概念说明

`impmod!` 只负责「把名字引进来」。引进来的 `RecverImpl` 是平台私有类型（Unix 是 `Recver(FdOps)`，Windows 是 `Recver(AdvOwnedHandle)`），不能直接对外暴露——否则公共 API 就和平台绑死了。

所以公共层用 **newtype** 再包一层：`pub struct Recver(pub(crate) RecverImpl);`（[src/unnamed_pipe.rs:62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L62)）。注意字段是 `pub(crate)`：**crate 内部（尤其是平台后端）能构造它，但外部用户拿不到 `RecverImpl`**，从而壳的内部细节对外不可见。

于是形成两层包装（以 Unix 为例）：

```
公共 Recver ──.0──► 后端 Recver ──.0──► FdOps ──.0──► OwnedFd (原始 fd)
（壳）            （芯）              （fd 操作封装）   （std 拥有的 fd）
```

公共 API 的所有读写、句柄操作，最终都顺着 `.0` 一路转发到底层系统调用。

#### 4.2.2 核心流程

管道的**创建**沿调用链「自上而下」展开，而**读写**则反向「自上而下」转发。两条链都串过 `impmod!` 注入的名字。

创建链（`pipe()` 构造对象）：

```
公共 pipe()                  [unnamed_pipe.rs:50]   fn pipe() { pipe_impl() }
   │  调用注入的 pipe_impl()
   ▼
后端 pipe_impl()             [os/unix:88] / [os/windows:100]
   │  Unix: pipe(false)        Windows: CreationOptions::default().build()
   ▼
后端 pipe() / create()       执行系统调用 libc::pipe2 / CreatePipe
   │  得到 OwnedFd / OwnedHandle
   ▼
包装：FdOps(fd) → 后端 Recver/Sender → 公共 PubRecver/PubSender
   ▼
返回 io::Result<(Sender, Recver)>   ← 直接是「公共类型」
```

读写链（对已构造的对象调用 `read`/`write`）：

```
调用 pub_recver.read(buf)
   │  forward_sync_read! 生成：self.0.read(buf)
   ▼
后端 Recver 的 Read 实现
   │  Unix: 经 derive_sync_mut_read → refwd → FdOps → libc::read
   │  Windows: impl Read [os/windows:106] → c_wrappers::read → ReadFile
   ▼
操作系统
```

关键洞察：**公共 `Sender`/`Recver` 这两个 newtype 并不是在公共模块里构造的**。因为后端 `pipe_impl()` 的返回类型就是 `(PubSender, PubRecver)`（即公共类型），真正调用系统调用并完成层层包装的，是后端的 `pipe()`/`create()`。公共 `pipe()` 只是个 `#[inline]` 的转发壳。

#### 4.2.3 源码精读

先看公共层。公共 `pipe()` 直接转发给注入的 `pipe_impl`（[src/unnamed_pipe.rs:49-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L49-L50)）：

```rust
#[inline]
pub fn pipe() -> io::Result<(Sender, Recver)> { pipe_impl() }
```

两个公共 newtype 把后端类型包起来（[src/unnamed_pipe.rs:61-63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L61-L63) 与 [src/unnamed_pipe.rs:93-94](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L93-L94)）：

```rust
// field is pub(crate) to allow platform builders to create the public-facing pipe types
pub struct Recver(pub(crate) RecverImpl);
impl Sealed for Recver {}
// ...
pub struct Sender(pub(crate) SenderImpl);
impl Sealed for Sender {}
```

注释点明了 `pub(crate)` 的用意：让平台后端能构造公共类型。后端通过 `use crate::unnamed_pipe::{Recver as PubRecver, Sender as PubSender}` 反向引用公共类型来构造它（见 [src/os/unix/unnamed_pipe.rs:7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L7) 与 [src/os/windows/unnamed_pipe.rs:11](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L11)）。

再看后端。Unix 后端把系统调用得到的 fd 层层包装成公共类型（[src/os/unix/unnamed_pipe.rs:64-81](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L64-L81)）：

```rust
let w = PubSender(Sender(FdOps(w)));   // OwnedFd → FdOps → 后端 Sender → 公共 PubSender
let r = PubRecver(Recver(FdOps(r)));   // 同理
```

而 `pipe_impl` 是公共 `pipe()` 真正调用的那个函数，注释自嘲了它名字的由来（[src/os/unix/unnamed_pipe.rs:87-89](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L87-L89)）：

```rust
// This is imported by a macro, hence the confusing name.
#[inline]
pub(crate) fn pipe_impl() -> io::Result<(PubSender, PubRecver)> { pipe(false) }
```

Windows 后端结构对称：`pipe_impl` 委托给构建器（[src/os/windows/unnamed_pipe.rs:100-102](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L100-L102)），`create()` 内做 `CreatePipe` 并包装（[src/os/windows/unnamed_pipe.rs:84-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L84-L85)）：

```rust
let w = PubSender(Sender { io: ManuallyDrop::new(w.into()), needs_flush: false });
let r = PubRecver(Recver(r.into()));
```

> 两个后端的 `Recver`/`Sender` 内部结构差异很大（Unix 是 `FdOps`，Windows 的 `Sender` 还多了 `needs_flush` 字段、`ManuallyDrop` 和 `Drop` 里的 `linger_pool` 延迟刷新——见 [src/os/windows/unnamed_pipe.rs:151-158](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L151-L158)）。但因为有公共 newtype 这层壳，并且二者都实现了相同的 trait（`Read`/`Write`、句柄互转），外部用户看到的 `Sender`/`Recver` 行为是一致的。这种「内部各异、对外统一」正是壳/芯分层的价值。

#### 4.2.4 代码实践（本讲的主实践任务）

**实践目标**：追踪 `unnamed_pipe::pipe()` 的调用链，画出从公共 `pipe()` 到平台 `pipe_impl()` 的注入路径，标注 `impmod!` 的作用点。

**操作步骤**：

1. 从公共入口 [src/unnamed_pipe.rs:50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L50) 的 `pub fn pipe()` 出发，它调用了 `pipe_impl()`。
2. 回到同一文件 [src/unnamed_pipe.rs:26-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L26-L30) 的 `impmod!` 调用，确认 `pipe_impl` 是被注入的别名——**这是第一个 `impmod!` 作用点**。
3. 跳到当前平台后端的 `pipe_impl`：
   - Unix：[src/os/unix/unnamed_pipe.rs:88-89](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L88-L89) → 委托 [src/os/unix/unnamed_pipe.rs:49](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L49) 的 `pipe(false)` → `libc::pipe2`（[src/os/unix/unnamed_pipe.rs:53-62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L53-L62)）。
   - Windows：[src/os/windows/unnamed_pipe.rs:100-102](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L100-L102) → `CreationOptions::default().build()` → `create()`（[src/os/windows/unnamed_pipe.rs:64-90](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L64-L90)）→ `CreatePipe`。
4. 在终点处确认：后端构造的 `PubSender` / `PubRecver` 就是公共类型 `crate::unnamed_pipe::{Sender, Recver}`——**这是后端反向引用公共 newtype 的作用点**。
5. 画出注入路径图（参考 4.2.2 的创建链），用箭头标出 `impmod!` 在「公共 `pipe()` → 后端 `pipe_impl()`」这一跳上扮演的桥接角色。

**需要观察的现象**：调用链穿过「公共模块」与「平台后端」两个模块的边界，而这条边界完全由 `impmod!` 注入的 `pipe_impl` 名字缝合；公共 `pipe()` 函数体里没有任何 `cfg`、没有任何 `if`。

**预期结果**：你得到的图里，`impmod!` 是唯一的「跨边界跳板」，`pipe_impl` 是桥上的那块板。

> 这是「源码阅读型实践」，不依赖编译运行；若想顺手验证，可在两个后端的 `pipe_impl` 各加一行 `eprintln!("pipe_impl on unix|windows");`（仅本地实验，勿提交），分别在 Linux 与 Windows 上 `cargo build --examples` 后运行示例，观察哪一行输出出现。跨平台行为标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么公共 `pipe()` 的函数体只有一句 `pipe_impl()`，而不直接把系统调用写在公共模块里？

**参考答案**：因为系统调用是平台特定的（Unix 用 `libc::pipe2`、Windows 用 `CreatePipe`），写在公共模块里就必须塞满 `#[cfg]` 分支，破坏「公共层平台无关」的分层。把实现下沉到后端、再用 `impmod!` 注入 `pipe_impl` 别名，公共层就只需转发一行，保持干净。

**练习 2**：后端 `pipe_impl` 的返回类型是 `(PubSender, PubRecver)`（公共类型），而不是后端私有类型 `(Sender, Recver)`。这说明了什么？

**参考答案**：说明「壳」是在后端里构造的，不是在公共模块里。后端拿到原始句柄后，一路包装成公共 newtype 再返回；公共 `pipe()` 拿到的已经是成品。这也解释了为什么公共 newtype 的字段必须是 `pub(crate)`——后端需要它来构造壳（见 [src/unnamed_pipe.rs:61](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L61) 的注释）。

**练习 3**：把后端 `Recver` 改成对外 `pub` 会带来什么后果？

**参考答案**：会泄露平台私有实现细节（`FdOps`、`AdvOwnedHandle` 等内部类型），公共 API 就和平台绑死，跨平台兼容性丧失，未来重构后端也会变成破坏性变更。这正是 interprocess 坚持「公共 newtype 套壳、字段 `pub(crate)`」的原因。

---

### 4.3 `multimacro!` 批量转发与 tokio 变体（macros + unnamed_pipe::tokio）

#### 4.3.1 概念说明

壳包好芯之后，还得让壳「继承」芯的各种 trait：`Read`、`Write`、`Debug`、句柄互转、raw 句柄派生……每样都对应一个转发宏。如果逐个手写 `forward_xxx!(Recver);`，既啰嗦又容易漏。

`multimacro!` 就是用来批量调用这些宏的「流水线」：**给出一个类型名，再列出一串宏名，它就依次把每个宏套到这个类型上**。它本身不实现任何 trait，只是「宏的宏」——把样板代码压成一张清单。

`multimacro!` 与 `impmod!` 分工明确：

| 宏 | 职责 | 作用时机 |
|---|---|---|
| `impmod!` | 把平台后端的**类型/函数**以别名注入公共模块 | 解决「芯从哪来」 |
| `multimacro!` | 给公共 newtype **批量挂上转发 trait** | 解决「壳怎么继承芯的能力」 |

本节同时回答一个关键问题：**同一套 `impmod!` + newtype + `multimacro!` 机制，如何在 Tokio 异步公共层 `unnamed_pipe::tokio` 上原样复用？**

#### 4.3.2 核心流程

`multimacro!` 的展开规则很直白：把首个 token（类型名）依次喂给列表里的每个宏，必要时把每个宏**括号里的额外参数**拼接进去。

```
multimacro! { Recver, M1, M2(Arg), M3 }
        │
        ▼
M1!(Recver);
M2!(Recver, Arg);   // 括号里的 Arg 被拼到类型名之后
M3!(Recver);
```

之所以需要「带参数」的形式，是因为有的转发宏需要额外信息：例如 `forward_tokio_read!(Recver, pinproj)` 需要知道用哪个方法做 pin 投影，`forward_try_handle!(Recver, io::Error)` 需要知道错误类型。`multimacro!` 的括号语法正是为这种「每宏自带参数」而设计。

异步公共层与同步公共层是「同一模板的两个实例」，差别只有三处：

1. `impmod!` 的路径多一段 `::tokio`；
2. `multimacro!` 挂的宏换成异步版（`forward_tokio_read` 代替 `forward_sync_read`）；
3. 注入进来的「芯」换成异步实现（Unix 异步后端的 `RecverImpl` 是 `AsyncFd<FdOps>`，而非裸 `FdOps`）。

#### 4.3.3 源码精读

`multimacro!` 定义在 [src/macros.rs:33-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L33-L46)，文档说明在 [src/macros.rs:28-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L28-L32)。本讲实际命中的是「无前缀 token」的臂（[src/macros.rs:40-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L40-L42)）：

```rust
($ty:ident, $($macro:ident $(($($arg:tt)+))?),+ $(,)?) => {$(
    $macro!($ty $(, $($arg)+)?);
)+};
```

`$ty` 是类型名，`$($macro:ident $(($($arg:tt)+))?),+` 匹配一串「宏名 + 可选括号参数」。外层 `$(...)+` 循环对每个宏生成一次调用，内层 `$(, $($arg)+)?` 在有参数时把参数拼到 `$ty` 之后。

公共同步 `Recver` 的实际清单在 [src/unnamed_pipe.rs:64-70](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L64-L70)：

```rust
multimacro! {
    Recver,
    forward_sync_read,
    forward_handle,
    forward_debug,
    derive_raw,
}
```

展开等价于：

```rust
forward_sync_read!(Recver);
forward_handle!(Recver);
forward_debug!(Recver);
derive_raw!(Recver);
```

每个宏为公共 `Recver` 贡献的能力如下（均已在前述源码中核实）：

| 宏 | 生成的能力 | 实现要点 |
|---|---|---|
| `forward_sync_read` | `impl Read for Recver` | `read`/`read_vectored` 转调 `self.0.*`，见 [forward_iorw.rs:4-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L4-L22) |
| `forward_handle` | `AsHandle`/`AsFd` + `From`/`Into` 句柄互转（双 `cfg`） | 转发到 `self.0`，见 [forward_handle_and_fd.rs:85-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L85-L98) |
| `forward_debug` | `impl Debug for Recver` | 转发到 `self.0` 的 `Debug`（定义于 `forward_fmt.rs`） |
| `derive_raw` | `AsRawHandle`/`AsRawFd`、`IntoRaw*`、`FromRaw*` | 基于「安全的 `As*`」派生「raw」版，见 [derive_raw.rs:124-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L124-L137) |

**异步变体**的清单更长，且用到了「带参数」形式，见 [src/unnamed_pipe/tokio.rs:45-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L45-L53)：

```rust
multimacro! {
    Recver,
    pinproj_for_unpin(RecverImpl),   // 带参数：为 RecverImpl 生成 pin 投影方法
    forward_tokio_read,              // AsyncRead（依赖上面的 pinproj）
    forward_as_handle,
    forward_try_handle(io::Error),   // 带参数：错误类型
    forward_debug,
    derive_asraw,
}
```

这里 `pinproj_for_unpin(RecverImpl)` 展开为 `pinproj_for_unpin!(Recver, RecverImpl)`，它为公共 `Recver` 生成一个把 `self.0`（即 `RecverImpl`）投影成 `Pin<&mut RecverImpl>` 的方法（见 [src/macros.rs:17-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L17-L26)）。随后 `forward_tokio_read!(Recver)` 正是靠它把 `poll_read` 转发给芯（见 [forward_iorw.rs:115-135](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L115-L135)，其中 [forward_iorw.rs:127](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L127) 调用 `self.pinproj()`）。这就是同步 `Read` 用普通 `&mut self`、而异步 `AsyncRead` 必须多一个 `pinproj_for_unpin` 的原因。

再看异步公共层的 `impmod!`，路径多一段 `::tokio`，注入的是异步后端（[src/unnamed_pipe/tokio.rs:8-12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L8-L12)）；异步 `pipe()` 同样只转发 `pipe_impl`（[src/unnamed_pipe/tokio.rs:31-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L31-L32)）。而异步后端交上来的「芯」是 `AsyncFd<FdOps>`（[src/os/unix/unnamed_pipe/tokio.rs:18-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe/tokio.rs#L18-L24)）：它先用同步 `super::pipe(true)` 拿到非阻塞 fd，再包成 `AsyncFd`，最后塞进公共 newtype。

> 顺带注意一个命名细节：异步后端 [src/os/unix/unnamed_pipe/tokio.rs:18-19](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe/tokio.rs#L18-L19) 自己也声明了 `type RecverImpl = AsyncFd<FdOps>;`。它和公共层经 `impmod!` 注入的 `RecverImpl`（指向后端结构体 `Recver`）是**两个不同模块里的同名符号**，互不冲突。读源码时不要被同名迷惑——它们处于不同命名空间。

#### 4.3.4 代码实践

**实践目标**：并排对比同步与异步公共层，体会「同一套 `multimacro!` 机制」与「每宏参数」的拼接方式。

**操作步骤**：

1. 打开 [src/unnamed_pipe.rs:64-70](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L64-L70) 的同步 `Recver` 清单，按 4.3.3 的表格，逐行写下每个宏会为 `Recver` 生成哪个 trait。
2. 打开 [src/unnamed_pipe/tokio.rs:45-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L45-L53) 的异步 `Recver` 清单，手写展开，特别确认 `pinproj_for_unpin(RecverImpl)` → `pinproj_for_unpin!(Recver, RecverImpl)`、`forward_try_handle(io::Error)` → `forward_try_handle!(Recver, io::Error)` 这两条的参数拼接。
3. 记录同步/异步清单的三处差异：(a) `forward_sync_read` ↔ `forward_tokio_read`；(b) 异步版多了 `pinproj_for_unpin(RecverImpl)`；(c) `derive_raw` ↔ `derive_asraw`。
4. （可选）本地写一个临时实验：仿照 `multimacro!` 的写法，为一个自定义 newtype 列一张清单（含一个带参宏），`cargo check`（仅本地实验，勿提交）观察是否如期生成 trait。

**需要观察的现象**：清单里每多写一个宏名，公共类型就多一个 trait 实现；带括号的宏会把括号里的内容作为额外参数传给底层宏；同步与异步两个文件几乎是「同一模板的两个实例」。

**预期结果**：你能准确预测「在 `multimacro!` 清单里加一行 `forward_xxx`，公共类型就会多出对应的 trait」，并确信 `impmod!` + newtype + `multimacro!` 是一套与同步/异步无关的可复用骨架。

> 步骤 4 标注「待本地验证」；前三步为纯源码阅读，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：如果在公共同步 `Recver` 的 `multimacro!` 清单里删掉 `forward_sync_read`，会怎样？

**参考答案**：公共 `Recver` 将不再实现 `Read` trait，于是 `let n = recver.read(&mut buf)?;` 这类调用会编译失败（`no method named read`）。这正说明 `multimacro!` 的每一行都对应一项面向用户的能力。

**练习 2**：为什么异步清单里要带 `pinproj_for_unpin(RecverImpl)`，同步清单却不需要？

**参考答案**：因为 `AsyncRead`/`AsyncWrite` 的方法签名是 `self: Pin<&mut Self>`，需要把 `Pin<&mut Recver>` 投影到内部的 `Pin<&mut RecverImpl>` 才能转调芯的 `poll_read`。`pinproj_for_unpin` 生成这个投影方法（[src/macros.rs:17-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L17-L26)）。同步的 `Read`/`Write` 用普通 `&mut self`，直接 `self.0.read(...)` 即可，所以同步版没有它。

**练习 3**：异步后端的 `RecverImpl` 是 `AsyncFd<FdOps>`，同步后端的是裸 `FdOps`。公共 newtype 代码需要因此改动吗？

**参考答案**：不需要。公共层只写 `struct Recver(pub(crate) RecverImpl);`，`RecverImpl` 只是一个名字，它的具体类型由 `impmod!` 在编译期决定。同步/异步两个公共模块各自独立地 `impmod!`，各自得到自己的 `RecverImpl`，互不干扰。这正是别名注入的好处——公共层对「芯」的具体类型一无所知。

---

## 5. 综合实践

把本讲三块知识（`impmod!` 注入、newtype 包装、`multimacro!` 转发）串起来，完成下面这个「从用户视角到系统调用」的全程追踪任务。

**任务**：以 `interprocess::unnamed_pipe::pipe()` 创建一个管道、在单进程内主线程写、另一线程读、传一句字符串为场景，画一张**纵贯图**，要求同时体现「创建链」与「读写链」，并在图上标注：

1. `impmod!` 在哪一跳把后端的 `pipe_impl`/`RecverImpl`/`SenderImpl` 注入公共模块；
2. 公共 newtype `Recver(pub(crate) RecverImpl)` 与后端 `Recver(FdOps)` / `Recver(AdvOwnedHandle)` 的两层（或三层）包装关系；
3. `multimacro!` 为公共 `Recver` 挂上的 `Read`、句柄互转、`Debug` 等 trait 是如何经 `self.0` 转发到芯的；
4. 最终落到哪个系统调用（Unix：`libc::pipe2` + `read`；Windows：`CreatePipe` + `ReadFile`）。

**参考骨架代码**（示例代码，非项目原有代码）：

```rust
// 示例代码：仅用于说明调用链，非仓库内文件
use interprocess::unnamed_pipe::pipe;
use std::{io::{Read, Write}, thread};

fn main() -> std::io::Result<()> {
    let (mut tx, mut rx) = pipe()?;          // 创建链：pipe() -> pipe_impl() -> 系统调用
    let handle = thread::spawn(move || {      // 读写链：read 经 forward_sync_read 转发到 .0
        let mut buf = [0u8; 16];
        let n = rx.read(&mut buf)?;           // 公共 Recver 的 Read
        println!("received: {}", String::from_utf8_lossy(&buf[..n]));
        Ok::<(), std::io::Error>(())
    });
    tx.write_all(b"hello, pipe")?;            // 公共 Sender 的 Write -> .0 -> 系统调用
    handle.join().unwrap()?;
    Ok(())
}
```

**操作步骤**：

1. 先在纸上按本讲 4.2.2、4.3.3 的两幅链路图把骨架拼成一张纵贯图。
2. 把图上每个箭头都标上「位于哪个文件、第几行」（引用本讲给出的永久链接）。
3. 若本地已配置好 Rust 工具链，可在 `examples/` 之外新建一个临时二进制（仅本地实验，勿提交进仓库），启用 `interprocess` 依赖后运行上述骨架，确认能打印 `received: hello, pipe`。

**需要观察的现象**：从用户的一行 `pipe()?` 到操作系统内核，整条链路在源码里是连续可追踪的，且公共层始终不出现任何平台判断。

**预期结果**：纵贯图清晰展示「用户 API → 公共 newtype（壳）→ `impmod!` 注入的后端类型（芯）→ 系统调用」，三块知识各司其位。若把平台换成 Windows，整张图结构不变，只需把后端跳板换成 [src/os/windows/unnamed_pipe.rs:100-102](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L100-L102) 的 `pipe_impl()`（走 `CreationOptions::default().build()` → `CreatePipe`）。

> 步骤 3 的实际运行结果取决于本地平台与工具链，标注「待本地验证」；前两步为纯源码阅读，可在不编译的情况下完成。

## 6. 本讲小结

- `impmod!` 是全库的「平台后端注射器」：按 `cfg(unix)`/`cfg(windows)` 生成两条对称的 `use`，把后端的类型与函数以统一别名（如 `RecverImpl`、`pipe_impl`）注入公共模块；因两 `cfg` 互斥，编译期恰留一条，别名唯一绑定到当前平台，零运行时派发。
- unnamed pipe 采用「公共 newtype 套壳 + 后端做芯」的分层，与 local socket 的「enum 派发」不同；公共 `Sender`/`Recver` 字段为 `pub(crate)`，使得后端能构造壳、而外部用户碰不到芯。
- 公共 `pipe()` 只是转发壳，真正执行系统调用并完成层层包装（`OwnedFd`/`OwnedHandle` → `FdOps`/`ManuallyDrop` → 后端结构体 → 公共 newtype）的是后端的 `pipe_impl`，且它直接返回公共类型。
- `multimacro!` 是「宏的宏」，把一串转发宏压成一张清单批量套到公共 newtype 上；支持「带括号参数」的形式（如 `forward_try_handle(io::Error)`、`pinproj_for_unpin(RecverImpl)`）。
- 同步与异步（Tokio）两套公共层是「同一模板的两个实例」：同样用 `impmod!` + newtype + `multimacro!`，差别只在路径多 `::tokio`、宏换异步版、`RecverImpl` 换成 `AsyncFd<FdOps>`；异步版因 `Pin` 投影需要而多了一个 `pinproj_for_unpin`。
- `impmod!` 与 `multimacro!` 分工：前者解决「芯从哪来」，后者解决「壳怎么继承芯的 trait」；`unnamed_pipe`（newtype 包装）与 `local_socket`（enum 派发）两条路线都以 `impmod!` 为地基。

## 7. 下一步学习建议

- **顺着后端继续往下挖**：Unix 后端的 `FdOps`、`c_wrappers`，Windows 后端的 `linger_pool`、`needs_flush`/`Drop` 延迟刷新，分别属于 u4（平台后端剖析）与 u8（Windows 高级内部机制）。读完本讲再去看后端内部，会清楚每个后端类型是如何「成为公共 newtype 的芯」的。
- **对照 local socket 的 enum 派发**：回看 u2-l2 的 `mkenum`/`dispatch!`，对比「enum 派发」与「newtype + `impmod!` 注入」两种手法的取舍——前者为多后端预留空间，后者更轻量；二者都以 `impmod!` 为共同地基。
- **宏系统全景**：本讲只用到 `forward_*`/`derive_*` 里的少数几个；`make_macro_modules!`（[src/macros.rs:114-125](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L114-L125)）挂载的全部宏将在 u7-l1 系统讲解。
- **句柄所有权**：本讲提到的 `OwnedHandle`/`OwnedFd`、`pub(crate)` 字段、句柄互转，其所有权模型在 u7-l2（句柄/FD 抽象与所有权管理）展开。
- **建议阅读的源码顺序**：`src/unnamed_pipe.rs` → `src/unnamed_pipe/tokio.rs` → `src/os/unix/unnamed_pipe.rs` → `src/os/windows/unnamed_pipe.rs`，把「同一套壳/芯模式在同步与异步、Unix 与 Windows 上如何复用」看全。
