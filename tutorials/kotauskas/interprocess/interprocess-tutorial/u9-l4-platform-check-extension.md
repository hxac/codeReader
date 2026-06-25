# 平台探测与二次开发扩展点

## 1. 本讲目标

interprocess 的全部价值都建立在一句承诺上：**「同一套公共接口，在不同操作系统上落到不同的原生 IPC 原语」**。要兑现这句承诺，库必须能在三个时间点上回答「我在什么平台上、能做什么」：

- **编译期**：这个目标平台到底支不支持？支持的话走哪一套后端代码？
- **运行期**：在真实机器上，某个具体行为（比如 socket 文件的权限位、`fchmod` 的生效时机）究竟是怎样的？
- **演进期**：以后要新增一个平台后端、或新增一种名字类型，需要改动哪些层、有哪些「封印」挡在外面？

本讲是专家层的收官篇，带你从「使用 interprocess」翻到「理解并改造 interprocess 的骨架」。学完后你应该能够：

- 说清 `platform_check.rs` 如何用 `compile_error!` 在编译期把不支持的目标平台挡在门外；
- 知道 `inspect-platform` 这个独立二进制的作用，能运行它并读懂它的输出；
- 理解 `Sealed` 这个私有 trait 如何形成「外部 crate 无法实现本库 trait」的扩展边界；
- 画出 `os` 模块的条件编译结构，说清 `impmod!`/`mkenum`/`dispatch!` 三把宏构成的「壳/芯」接缝；
- **设想新增一个平台后端或一种名字类型时，能列出需要新增/修改的文件与 trait 实现，并说明 enum dispatch 如何把它接入。**

## 2. 前置知识

本讲承接 u1-l2（目录结构与模块地图），你应已经知道 interprocess 的「公共层为壳、平台后端为芯」分层，以及 `os::unix` / `os::windows` 用 `#[cfg]` 互斥编译的事实。下面补充几个本讲反复出现的概念。

- **条件编译（`#[cfg]`）**：Rust 在编译期根据 `cfg(...)` 谓词决定某段代码是否参与编译。`cfg(unix)`、`cfg(windows)`、`cfg(target_os = "linux")`、`cfg(target_pointer_width = "64")` 等都是编译期常量，不成立的分支根本不会进入最终的二进制。interprocess 几乎所有跨平台行为都靠它实现。
- **`compile_error!`**：一个在编译期触发编译错误的宏。它本身不依赖 `cfg`，但通常与 `#[cfg]` 配合：当某组 `cfg` 条件成立时才展开这条宏，从而「针对不支持的配置给出清晰错误，而不是放任后续代码报一堆看不懂的错」。
- **封印 trait（sealed trait）模式**：Rust 没有语言级的「只能在本 crate 实现」关键字。社区惯用法是给一个 `pub` trait 加一个**私有**（`pub(crate)`）的 supertrait，由于下游 crate 无法命名这个私有 trait，也就无法为它提供实现，于是 `pub` trait 实际上被「封印」。interprocess 把它做到了全库统一。
- **enum dispatch（枚举派发）**：用 `enum` 把多个后端类型包成一个公共类型，方法体里 `match` 出具体变体再转发调用。它区别于 `dyn Trait`（动态派发、有虚表开销）：当枚举在编译期实际只有一个变体存活时（这正是 interprocess「每个平台只有一个后端」的现状），`match` 被优化器完全消解，派发零开销。
- **`[[bin]]` 目标**：Cargo.toml 里声明一个**独立的可执行二进制**（区别于库本身和 `[[example]]`）。`inspect-platform` 就是一个 `[[bin]]`，不参与库的常规构建产物。

> 一句话定位：`platform_check` 是**编译期的门卫**，`inspect-platform` 是**运行期的探测器**，`Sealed` 是**类型系统的围墙**，`os` + 三把宏是**接芯的插座**。本讲逐个拆开它们。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | crate 根：挂载 `platform_check`、声明 `pub mod os` 并用 `#[cfg]` 选 unix/windows |
| `src/platform_check.rs` | 编译期门卫：两条 `compile_error!`，挡掉不支持的目标平台 |
| `src/misc.rs` | 定义 `pub(crate) trait Sealed {}`——全库封印的根基 |
| `inspect-platform/main.rs` | 独立 `[[bin]]` 的入口：打印平台信息，在 Unix 上调用 `unix::main()` 做实地实验 |
| `inspect-platform/unix.rs` | Unix 上的运行期探测：socket 文件权限、`fchmod` 生效时机等实验 |
| `inspect-platform/util.rs` | 探测器的格式化与错误报告工具（`bitwidths!`、`ResultExt`） |
| `src/os/unix.rs` / `src/os/windows.rs` | 两个平台后端的总装模块，声明各自的子原语 |
| `src/os/unix/cfg_doc_templates.rs` | 一组**被注释掉的** `doc(cfg(...))` 模板，供文档徽章参考 |
| `src/local_socket.rs` | local socket 公共层：traits、prelude、tokio 子模块 |
| `src/local_socket/enumdef.rs` | `mkenum!`/`dispatch!` 宏：生成派发枚举并转发方法 |
| `src/local_socket/stream/enum.rs`、`listener/enum.rs` | 用宏装配出公共 `Stream`/`Listener` 枚举，注入后端 |
| `src/local_socket/name/type.rs` | 名字类型系统：`NameType` 等 trait（含 `Sealed`）与不可居标记类型 |
| `src/os/{unix,windows}/local_socket/dispatch_sync.rs` | 平台路由层：把公共创建请求转发到具体后端 |

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：`platform_check`、`inspect-platform`、`Sealed`、`os`。前三者是「探测与边界」，最后一个 `os` 模块顺带把扩展点（enum dispatch 接缝）讲透。

### 4.1 编译期门卫（最小模块：platform_check）

#### 4.1.1 概念说明

`platform_check` 是整个 crate **最早**、也**最便宜**的平台探测点。它不产生任何运行时代码，只在编译期下两道判决：

1. **目标操作系统必须属于 interprocess 支持的集合**——目前即「Windows 或 Unix（不含 emscripten）」。
2. **指针宽度必须是 32 位或 64 位**——这是库内不少 `as usize` / `as isize` 转换与 `impl_subsize!` 宏的安全前提。

把这两条检查放在 crate 的最前面（`src/lib.rs` 的 `mod platform_check;` 紧跟文档与 lint 之后、早于一切业务模块），目的是**尽早失败、给出人话错误**。否则下游的 `#[cfg(windows)] use windows_sys::...`、`#[cfg(unix)] use libc::...` 等会在不支持的平台上爆出一堆「找不到类型」的次生错误，让人无从下手。

#### 4.1.2 核心流程

`platform_check` 的判罚逻辑可以画成两条独立的 `#[cfg]` 闸门：

```text
编译开始
  │
  ├─ 若 cfg(not(any(windows, unix))) 或 cfg(target_os = "emscripten")
  │      └─ 展开 compile_error!("...not supported by interprocess...") → 编译终止
  │
  ├─ 若 cfg(not(any(target_pointer_width = "32", target_pointer_width = "64")))
  │      └─ 展开 compile_error!("...exotic pointer widths...") → 编译终止
  │
  └─ 否则：两道闸门都不触发，platform_check 模块体为空，编译继续
```

注意它**只挡、不选**：真正「选哪个后端」的工作由 `os` 模块和各 `#[cfg]` 完成（见 4.4）。`platform_check` 只保证「剩下的一定是个被支持的平台」，让后续 `#[cfg]` 可以放心地假设自己处在 windows 或 unix 之一。

#### 4.1.3 源码精读

整个模块只有两条 `compile_error!`，先看第一条——操作系统门卫：[src/platform_check.rs:1-6](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/platform_check.rs#L1-L6)。

```rust
#[cfg(any(not(any(windows, unix)), target_os = "emscripten"))]
compile_error!(
    "Your target operating system is not supported by interprocess – check if yours is in the list \
of supported systems, and if not, please open an issue on the GitHub repository if you think that \
it should be included"
);
```

谓词 `any(not(any(windows, unix)), target_os = "emscripten")` 读作：「既不是 Windows 也不是 Unix，**或**，目标是 emscripten」。emscripten 名义上算 Unix（`cfg(unix)` 在它上面为真），但 interprocess 明确不支持它，所以单独排除。

再看第二条——指针宽度门卫：[src/platform_check.rs:8-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/platform_check.rs#L8-L13)。

```rust
#[cfg(not(any(target_pointer_width = "32", target_pointer_width = "64")))]
compile_error!(
    "Platforms with exotic pointer widths (neither 32-bit nor 64-bit) are not supported by \
interprocess – if you think that your specific case needs to be accounted for, please open an \
issue on the GitHub repository if you think that it should be included"
);
```

这条挡掉 16 位（以及将来可能出现的 128 位以外的异类宽度）。库内的 `impl_subsize!` 宏（见 `src/misc.rs` 的注释「we don't run on 16-bit platforms」）依赖这一前提。

这两条都在 `src/lib.rs:12` 处被最早挂载——[src/lib.rs:12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L12)：

```rust
mod platform_check;
```

它排在 `pub mod bound_util;` 等业务模块（[src/lib.rs:19-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L19-L22)）之前，确保门卫先于一切被求值。

#### 4.1.4 代码实践

**实践目标**：亲眼看到编译期门卫生效，确认它给出的是「人话错误」而非一堆次生报错。

**操作步骤**：

1. 在仓库根目录执行一次正常检查，确认本平台通过门卫：
   ```bash
   cargo check
   ```
2. 选一个 interprocess 不支持的目标尝试编译（若你已通过 `rustup target add wasm32-unknown-unknown` 安装该 target）：
   ```bash
   cargo check --target wasm32-unknown-unknown
   ```

**需要观察的现象**：

- 第 1 步应顺利通过（不输出 platform_check 相关错误）。
- 第 2 步应在编译**很早**的阶段就停下，并打印第一条 `compile_error!` 的原文（"Your target operating system is not supported by interprocess…"）。

**预期结果**：wasm32 既非 windows 也非 unix，命中第一条门卫；错误信息清晰指向「平台不支持」，而不是后续的「找不到 `windows_sys` / `libc`」之类噪音。

> 待本地验证：若未安装 `wasm32-unknown-unknown` target，第 2 步会先报「target not found」，请先 `rustup target add` 再试。

#### 4.1.5 小练习与答案

**练习 1**：为什么 emscripten 需要被单独排除，而不是天然地被 `not(any(windows, unix))` 挡掉？

**参考答案**：因为 emscripten 在 Rust 的 `cfg` 里**算 unix**（`cfg(unix)` 为真），所以 `not(any(windows, unix))` 对它为假、挡不住。interprocess 不支持它，必须用 `target_os = "emscripten"` 显式补一刀。

**练习 2**：若有人要把 interprocess 移植到一个全新的、既非 windows 也非 unix 的操作系统，`platform_check` 这一行需要怎么改？

**参考答案**：需要把第一条谓词从 `not(any(windows, unix))` 放宽，例如改成 `not(any(windows, unix, target_os = "newos"))`，否则新平台会直接被门卫挡死，根本进不了后续的 `#[cfg]` 选择逻辑。

---

### 4.2 运行期探测器（最小模块：inspect-platform）

#### 4.2.1 概念说明

`platform_check` 只能回答「这个平台理论上支不支持」，但 interprocess 依赖大量**真实平台行为**——而这些行为往往没有写在标准里、因内核版本而异。例如：

- `bind()` 创建的 socket 文件，权限位由什么决定？`umask` 还是 `fchmod`？
- `bind()` 之后再 `fchmod` 改权限，对**已经连上的**客户端鉴权还有没有效？
- `sockaddr_un`、`socklen_t` 等类型在当前目标上到底是几个字节？

这些问题只能靠「在真实机器上跑一遍、打印结果」来回答。`inspect-platform` 就是为此而生的**独立诊断二进制**：它不属于库的常规 API，只在开发者需要排查平台行为时手动运行。

它被声明为一个 `[[bin]]` 目标：[Cargo.toml:109-112](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L109-L112)。

```toml
[[bin]]
name = "inspect-platform"
path = "inspect-platform/main.rs"
test = false
```

注意 `path` 指向仓库根下的 `inspect-platform/` 目录（与 `src/` 平级），`test = false` 表示它不参与 `cargo test`。

#### 4.2.2 核心流程

探测器的主流程很直白：先打印**通用**信息（OS、ARCH、若干整型的位宽），再按平台分发到具体实验：

```text
main()
  ├─ print_common_intro()            # OS/ARCH + usize/c_char/... 的位宽
  ├─ #[cfg(unix)] unix::main()       # Unix 实地实验
  └─ #[cfg(not(unix))] 打印 "no further information"
```

Unix 实验部分（`inspect-platform/unix.rs`）围绕「监听一个 socket 文件、stat 它的权限、尝试连接、再 fchmod 改权限看鉴权是否变化」展开，通过枚举不同 `umask`/`fchmod` 组合，**实证**地记录当前内核的行为。

#### 4.2.3 源码精读

入口在 [inspect-platform/main.rs:16-28](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/inspect-platform/main.rs#L16-L28)：

```rust
fn print_common_intro() {
    use std::env::consts::*;
    println!("==== interprocess inspect-platform on {} {} ====", OS, ARCH);
    print_bitwidths(&bitwidths!(usize, c_char, c_short, c_int, c_long, c_longlong));
}

fn main() {
    print_common_intro();
    #[cfg(unix)]
    unix::main();
    #[cfg(not(unix))]
    println!("Not a Unix system, no further information will be gathered.");
}
```

通用信息靠 `std::env::consts::{OS, ARCH}`（编译期常量字符串，如 `"linux"`/`"x86_64"`）和 `bitwidths!` 宏（在 `inspect-platform/util.rs` 里把一组类型的 `BITS` 打成表格）。随后用 `#[cfg(unix)]` / `#[cfg(not(unix))]` 二选一：Unix 上深入实验，非 Unix 上只到此为止。

Unix 实验的核心是 [inspect-platform/unix.rs:21-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/inspect-platform/unix.rs#L21-L43)：先打印一组 Unix 专有类型（`socklen_t`、`mode_t`、`pid_t` 等）的位宽与 `sockaddr_storage`/`sockaddr_un` 的大小；若以 root 运行，主动 `seteuid(1)` 降权以模拟普通用户；然后在临时目录里反复 `bind` 监听器、`stat`、尝试连接，观察权限与鉴权行为。关键的一段是 [inspect-platform/unix.rs:45-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/inspect-platform/unix.rs#L45-L66) 的 `requires_tmpdir`：它用一张「`mask` × `pre_fchmod_mode` × `has_post_fchmod`」的参数表逐个调用 `try_listener_c`，相当于对内核做一组对照实验。

> 旁注：`src/os/unix/cfg_doc_templates.rs` 是一个**整文件被注释掉**的参考库（[src/os/unix/cfg_doc_templates.rs:1-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/cfg_doc_templates.rs#L1-L79)）。它收藏了一堆 `#[cfg_attr(feature = "doc_cfg", doc(cfg(any(target_os = …, …))))]` 模板（按 `ucred`/`xucred`/`sockcred` 等凭据机制分类），供在源码各处需要标注「这段代码在哪些 `target_os` 上可用」时复制粘贴。它当前不参与编译，纯做备忘——这也印证了 interprocess 的文档徽章（`doc_cfg`）本身就是一套平台探测结果的展示。

#### 4.2.4 代码实践

**实践目标**：在你的机器上运行探测器，看清它输出的平台事实。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   cargo run --bin inspect-platform
   ```
2. （可选）若在 Linux 上且临时目录可写，观察它打印的 socket 文件权限实验结果。

**需要观察的现象**：

- 第一行形如 `==== interprocess inspect-platform on linux x86_64 ====`。
- 紧跟一张整型位宽表（`usize`、`c_int`、`c_long` …）。
- Unix 上再打印 `sockaddr_un`、`socklen_t` 等的大小，以及一组 `Listener fstat` / `Listener stat` 的 `[mode …]` 行，反映 `bind` 出来的 socket 文件权限。
- 可能出现 `[Caution]` 开头的行——那是探测器对「权限位看似无效」之类异常行为的警告。

**预期结果**：你拿到一份当前内核对 socket 文件权限/鉴权行为的实证快照。这正是 `inspect-platform` 的价值——它把「文档里说不清、得跑一遍才知道」的平台行为变成可读输出。

> 待本地验证：不同发行版/内核版本输出会不同；root 与普通用户运行也会不同（探测器对 root 会主动降权）。

#### 4.2.5 小练习与答案

**练习 1**：`inspect-platform` 为什么是 `[[bin]]` 而不是 `[[example]]`？

**参考答案**：`[[example]]` 的语义是「展示如何**使用**这个库」，而 `inspect-platform` 的目的是**诊断平台本身**、给库的**开发者**看内核行为，不教用户怎么调 API。用独立 `[[bin]]` 并 `test = false`，把它和库的公共演示清晰隔开。

**练习 2**：探测器在 root 下为什么要 `seteuid(1)` 降权？

**参考答案**：root 能无视文件权限位读写任何文件，会让「socket 文件权限是否真的拦住了非授权连接」这个实验失真。降权到普通用户（euid=1，即 daemon）才能测出权限位的真实鉴权效果。

---

### 4.3 封印与扩展边界（最小模块：Sealed）

#### 4.3.1 概念说明

`Sealed` 是 interprocess 全库统一的**封印标记 trait**。它本身没有任何方法，唯一作用是充当一个 `pub(crate)`（私有）的 supertrait。看它的定义：[src/misc.rs:19-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L19-L21)。

```rust
/// Utility trait that, if used as a supertrait, prevents other crates from implementing the
/// trait.
pub(crate) trait Sealed {}
```

`pub(crate)` 是关键：**外部 crate 无法命名这个 trait**，于是也无法为它提供实现。而 Rust 规定「要实现某 trait，必须同时满足该 trait 的所有 supertrait」——既然外部 crate 无法 `impl Sealed`，它也就无法 `impl` 任何把 `Sealed` 列为 supertrait 的公共 trait。

这就形成了 interprocess 最硬的扩展边界：库对外暴露 `pub trait Listener: … + Sealed`、`pub trait Stream: … + Sealed`、`pub trait NameType: … + Sealed` 等，**用户只能用库自带的类型去满足这些 trait，不能自己造一个类型冒充后端**。用集合论写就是：

\[
\forall T \notin \text{crate},\quad \nexists\, \text{impl } \textit{PublicTrait} \text{ for } T
\]

因为要写出该 `impl`，必须先写出 `impl Sealed for T`，而 `Sealed` 不可在 crate 外命名，故实现不成立。

#### 4.3.2 核心流程

封印的「闭环」由三处协同：

1. **定义根基**：`src/misc.rs` 里的 `pub(crate) trait Sealed {}`，并通过 `lib.rs` 的 `pub(crate) use {… misc::*}`（[src/lib.rs:71-73](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L71-L73)）把 `Sealed` 暴露为 crate 内可见。
2. **挂为 supertrait**：公共 trait 把 `Sealed` 写进 bounds，例如 `NameType`：[src/local_socket/name/type.rs:35-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L35-L41)。
3. **库内类型补实现**：库自带的类型显式 `impl Sealed`，例如派发枚举由 `mkenum!` 自动 `impl $crate::Sealed for $nm`（[src/local_socket/enumdef.rs:35](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L35)），不可居标记类型由 `tag_enum!` 补（[src/macros.rs:102-111](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L102-L111)）。

外部 crate 卡在第 1 步——它连 `Sealed` 的名字都写不出来。

#### 4.3.3 源码精读

先看公共 trait 如何挂上封印。`NameType` 是名字类型的总接口：[src/local_socket/name/type.rs:35-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L35-L41)。

```rust
#[allow(private_bounds)]
pub trait NameType: Copy + std::fmt::Debug + Eq + Send + Sync + Unpin + Sealed {
    fn is_supported() -> bool;
}
```

注意 `Sealed` 出现在 supertrait 列表里，且整个 trait 标了 `#[allow(private_bounds)]`——这正是「公共 trait 引用了私有 trait」的信号，编译器本来会警告，这里显式放行。子接口 `PathNameType`/`NamespacedNameType` 又继承 `NameType`：[src/local_socket/name/type.rs:46-62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L46-L62)，于是封印自动传染到它们。

再看库内类型如何「拿到通行证」。`mkenum!` 宏在生成派发枚举时顺手补上封印：[src/local_socket/enumdef.rs:35](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L35)。

```rust
impl $crate::Sealed for $nm {}
```

于是 `Stream`/`Listener`/`RecvHalf`/`SendHalf` 这些公共枚举都能满足 `Sealed`，进而可以 `impl traits::Stream for Stream`。`tag_enum!` 宏对不可居标记类型（如 `GenericFilePath`）做同样的事：[src/macros.rs:102-111](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L102-L111)。

```rust
macro_rules! tag_enum {
    ($($(#[$attr:meta])* $tag:ident),+ $(,)?) => {$(
        $( #[$attr] )*
        #[derive(Copy, Clone, Debug, PartialEq, Eq)]
        pub enum $tag {}            // 无人居的标记枚举
        #[allow(deprecated)]
        impl $crate::Sealed for $tag {}
    )+};
}
```

`pub enum $tag {}` 是一个**没有变体的枚举**（uninhabited），永远构造不出值，纯粹在类型层面充当「标签」。`NameType` 就实现在这些标签上，例如：[src/local_socket/name/type.rs:64-81](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L64-L81)——`GenericFilePath` 由 `tag_enum!` 生成并 `impl Sealed`，随后 `impl NameType for GenericFilePath` 才合法。

> 一个常被忽略的细节：`local_socket.rs` 的模块文档明确说，`NameType` 的映射「**It is a breaking change for a mapping to meaningfully change.**」（[src/local_socket/name/type.rs:29-33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/name/type.rs#L29-L33)）。封印 + 这条稳定性承诺合起来，把「名字类型 → 底层原语」的映射锁死成跨进程协议的一部分。

#### 4.3.4 代码实践

**实践目标**：亲手验证「封印 = 外部 crate 无法实现」，而不是停留在概念。

**操作步骤**（源码阅读型 + 示例代码）：

1. 全仓搜索 `Sealed`，确认它的定义是且仅是 `src/misc.rs:21` 的 `pub(crate) trait Sealed {}`，且所有公共 trait 都把它列为 supertrait。
2. 在**另一个**（下游）crate 里写如下「示例代码」，尝试自己实现 `NameType`：
   ```rust
   // 示例代码：预期无法编译
   use interprocess::local_socket::NameType;

   enum MyNameType {}   // 我想自己造一个名字类型
   impl NameType for MyNameType {
       fn is_supported() -> bool { true }
   }
   ```

**需要观察的现象**：

- 第 2 步编译失败，错误信息指向 `NameType` 的 supertrait `Sealed` 是私有的（something like "trait `Sealed` is private" / "not visible outside the crate"）。

**预期结果**：你无法在 interprocess 之外实现 `NameType`（以及 `Listener`/`Stream` 等）。这就是「封印」把你挡在边界外的实证。要扩展，只能在 interprocess **内部**改源码（见 4.4 与综合实践）。

> 待本地验证：不同 Rust 版本错误措辞略有差异，但结论一致——封印成立。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `pub(crate) trait Sealed {}` 改成 `pub trait Sealed {}`，封印还能成立吗？

**参考答案**：不能。`Sealed` 一旦 `pub`，外部 crate 就能命名并 `impl Sealed for MyType`，从而绕过封印实现任意公共 trait。封印的全部魔力就在 `pub(crate)` 这一个可见性修饰符上。

**练习 2**：`tag_enum!` 生成的 `pub enum $tag {}` 为什么要是**空枚举**（无变体）？

**参考答案**：这些类型只在**类型层面**当标签用（作为泛型参数、作为 `impl` 的目标），永远不需要构造出值。空枚举（uninhabited）保证「没法 new 出实例」，既省内存又能在类型层面表达「这只是个标记」。`is_supported()` 等方法都是关联函数（无 `self`），不需要实例。

---

### 4.4 os 模块与 enum dispatch 接缝（最小模块：os）

#### 4.4.1 概念说明

前三个模块回答了「探测与边界」，这最后一个模块回答本讲的核心命题——**扩展点在哪里**。interprocess 的扩展性全部凝结在「壳/芯」接缝上，而这条接缝由三把宏拧成：

- **`impmod!`**——平台后端**注射器**。按 `cfg(unix)`/`cfg(windows)` 生成两条对称的 `use`，把后端类型/函数以统一别名注入公共层。
- **`mkenum!`**——派发枚举**生成器**。吐出带 `#[cfg]` 互斥变体的公共枚举，并自动补 `Sealed`/`From`/`Debug`。
- **`dispatch!`**——方法**转发器**。把一行调用展开成按 `cfg` 互斥的单分支 `match`。

三者协作的模式是：公共层用 `mkenum!` 造出壳，用 `impmod!` 把芯（后端模块）引过来，用 `dispatch!` 把壳上的方法转发给芯。`os` 模块则是「芯」的栖息地。

看 `os` 模块本身：[src/lib.rs:33-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L33-L40)。

```rust
pub mod os {
    #[cfg(unix)]
    #[cfg_attr(feature = "doc_cfg", doc(cfg(unix)))]
    pub mod unix;
    #[cfg(windows)]
    #[cfg_attr(feature = "doc_cfg", doc(cfg(windows)))]
    pub mod windows;
}
```

`unix` 与 `windows` 两个子模块用 `#[cfg]` 互斥，**任何一次编译只有一个存在**。它们各自的总装见 [src/os/unix.rs:20-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix.rs#L20-L23) 与 [src/os/windows.rs:4-7](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows.rs#L4-L7)：Unix 暴露 `fifo_file`/`local_socket`/`uds_local_socket`/`unnamed_pipe`，Windows 暴露 `local_socket`/`named_pipe`/`security_descriptor`/`unnamed_pipe`——同名的是对称原语，各自还带平台专有项。

#### 4.4.2 核心流程：接缝如何把「芯」接上「壳」

以 local socket 的 `Stream` 为例，接缝的完整装配链是：

```text
公共壳                        接缝（宏）                      芯（后端）
─────────────────────────────────────────────────────────────────────
src/local_socket/
  stream/enum.rs
    use uds_impl / np_impl  ──► impmod!/cfg 选后端模块别名 ──► os::unix::uds_local_socket
                                                                 os::windows::named_pipe::local_socket
    mkenum!(Stream)         ──► 生成 enum Stream { UdSocket(..), NamedPipe(..) }
                                  + impl Sealed + From + Debug
    impl Stream::from_options ─► dispatch_sync::connect(opts) ─► os::<plat>/local_socket/dispatch_sync.rs
                                  dispatch!(...) 转发              pub fn connect() { connect_sync_as::<后端>() }
```

每一层都「不碰系统调用」——公共壳只派发，接缝只路由，真正调用 `socket()`/`CreateNamedPipeW()` 的是芯。这种分层让「换一个后端」成为一件边界清晰的事。

#### 4.4.3 源码精读

**接缝之一：`impmod!` 注射后端别名。** 看 `stream/enum.rs` 顶部：[src/local_socket/stream/enum.rs:1-17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L1-L17)。

```rust
#[cfg(unix)]
use crate::os::unix::uds_local_socket as uds_impl;
#[cfg(windows)]
use crate::os::windows::named_pipe::local_socket as np_impl;
…
impmod! {local_socket::dispatch_sync}
```

`impmod!` 的定义在 [src/macros.rs:4-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L4-L14)：

```rust
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

一句话：它把 `os::unix::X` 与 `os::windows::X` 两条对称路径折叠成同一个名字 `X` 注入当前作用域，因为两 `cfg` 互斥，运行时零开销。

**接缝之二：`mkenum!` 生成壳 + `dispatch!` 转发。** `Stream` 枚举由一句 `mkenum!(… Stream)` 生成：[src/local_socket/stream/enum.rs:66-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L66-L78)。宏本体 [src/local_socket/enumdef.rs:18-57](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L18-L57) 会吐出两个 `#[cfg]` 互斥的变体（`NamedPipe(np_impl::Stream)` 与 `UdSocket(uds_impl::Stream)`）。随后方法体用 `dispatch!` 转发，例如 `from_options`：[src/local_socket/stream/enum.rs:80-88](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L80-L88)。

```rust
fn from_options(options: &ConnectOptions<'_>) -> io::Result<Self> {
    dispatch_sync::connect(options)
}
```

`dispatch!` 宏本体（[src/local_socket/enumdef.rs:2-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L2-L16)）展开成一个按 `cfg` 互斥的 `match`，由于编译时只剩一个变体，分支被优化器消解。

**接缝之三：路由层 `dispatch_sync`。** 公共层的 `from_options` 不直接构造后端，而是调 `dispatch_sync::connect`，后者在**平台后端目录**里：[src/os/unix/local_socket/dispatch_sync.rs:7-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs#L7-L14) 与 [src/os/windows/local_socket/dispatch_sync.rs:7-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L7-L14)。两份文件几乎逐字对称：

```rust
pub fn connect(options: &ConnectOptions<'_>) -> io::Result<Stream> {
    options.connect_sync_as::<uds_impl::Stream>().map(Stream::from)   // Unix
    // np_impl::Stream::from_options(options).map(Stream::from)       // Windows
}
```

监听器侧同理：`listener/enum.rs` 用 `impmod! {local_socket::dispatch_sync as dispatch}`（[src/local_socket/listener/enum.rs:11](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L11)）注入路由，`from_options` 调 `dispatch::listen`（[src/local_socket/listener/enum.rs:57-67](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L57-L67)）。Tokio 异步层是完全镜像的另一套，接缝换成 `impmod! {local_socket::dispatch_tokio as dispatch}`（[src/local_socket/tokio/listener/enum.rs:11](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/listener/enum.rs#L11)）。

> **关键认知（决定扩展难度）**：当前 `mkenum!`/`dispatch!`/`impmod!` 都是**两臂**（windows/unix）的，反映了库的现状——「每个平台恰好一个后端」。所以「新增一个全新的平台后端」意味着要**泛化这三把宏**（加第三臂/第三变体），这是一次触及骨架的改动；而「在已有平台内新增一种名字类型」则**不必动宏**，只需加一个 `tag_enum!` 标记 + `impl NameType` + 一个后端 `map_*` 函数，是局部得多、也更被鼓励的扩展。详见综合实践。

#### 4.4.4 代码实践

**实践目标**：跟踪一条完整的「壳 → 接缝 → 芯」调用链，亲手确认每一层都不越界。

**操作步骤**（源码阅读型）：

1. 从客户端连接 `Stream::connect(name)` 出发，按顺序打开并阅读：
   - `src/local_socket/stream/trait.rs` 中 `Stream::connect` 的默认实现（它等价于 `ConnectOptions::new().name(name).connect_sync_as::<Self>()`）；
   - `src/local_socket/stream/enum.rs:80-88` 的 `from_options` → `dispatch_sync::connect`；
   - 你当前平台的 `src/os/<plat>/local_socket/dispatch_sync.rs`（`connect_sync_as::<后端>`）；
   - 后端 `Stream::from_options`（`src/os/unix/uds_local_socket/stream.rs` 或 `src/os/windows/named_pipe/local_socket/stream.rs`），真正落到 `socket()`/`CreateFileW()`。
2. 在纸上画出这条链，标注「壳 / 接缝（宏名）/ 芯」三色。

**需要观察的现象**：

- 每一层都只做「转发」或「路由」，没有任何一层同时承担两件事；
- `dispatch!` 与 `impmod!` 出现的位置就是接缝，跨过它们就从公共层进入平台私有层。

**预期结果**：你能指出「要换一个后端，只需替换芯 + 改路由层；要支持新平台，还需动宏」。这为综合实践打好地图。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dispatch_sync` 要放在**平台后端目录**（`os/<plat>/local_socket/`）里，而不是公共层？

**参考答案**：因为它必须 `use` 具体后端类型（`uds_impl::Stream`/`np_impl::Stream`），而后端类型本身是平台私有的。把路由层放在平台目录里，它就能合法引用同目录的后端；公共层则只通过 `impmod!` 拿到一个统一的 `dispatch_sync` 名字，对后端类型保持无知。这正是「壳不知道芯的具体类型」的体现。

**练习 2**：既然每次编译只有一个平台后端存活，公共枚举 `Stream` 实际上只有一个变体。那为什么还要做成 `enum` 而不是直接 `pub use` 后端类型？

**参考答案**：为了**统一类型名**与**为将来留余地**。公共层需要一个跨平台稳定的名字 `local_socket::Stream`（直接 `pub use` 后端会让类型名/路径随平台变化）；同时模块文档（[src/local_socket.rs:14-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L14-L21)）指出，未来 Windows 可能引入基于 AF_UNIX 的第二后端，届时枚举会有两个变体而公共 API 不变。单变体时 match 零开销，多变体时自动派发——enum 是兼顾「现在零开销」与「将来可扩展」的选择。

---

## 5. 综合实践

**任务**：设想为 interprocess **新增一个假想平台后端**（例如一个名为 `myos` 的虚构操作系统，其 local socket 由某种 `MyPipe` 原语实现）。不必真正编译通过，但要求列出完整的改动清单，并说明 enum dispatch 如何把它接入。这个任务把本讲四个模块全部串起来。

**要求产出的清单**（请逐条对照源码写出「改哪个文件、改什么」）：

1. **门卫放行**：`src/platform_check.rs` 第一条谓词要怎么放宽，才能让 `myos` 通过编译期检查？（提示：在 `any(windows, unix, …)` 里加上 `myos` 的 `cfg`。）
2. **新建芯目录**：参照 `src/os/unix/` 的结构，新建 `src/os/myos/`，至少包含：
   - `local_socket.rs`（总装，声明 `dispatch_sync`/`name_type`/`peer_creds` 等子模块）；
   - `local_socket/dispatch_sync.rs`（实现 `listen`/`connect` 两个路由函数，内部 `create_sync_as::<MyListener>()` / `connect_sync_as::<MyStream>()`）；
   - `local_socket/dispatch_tokio.rs`（若启用 tokio，对称实现）；
   - `local_socket/name_type.rs`（实现 `map_generic_path_osstr` 等被 `name/type.rs` 经 `impmod!` 调用的 `n_impl::map_*` 函数）；
   - 后端类型模块（如 `my_pipe_local_socket.rs`），其中的 `Listener`/`Stream` 实现 `local_socket::traits::{Listener, Stream, StreamCommon, RecvHalf, SendHalf}`——**注意这些 trait 都被封印**，所以这些 `impl` 必须写在 interprocess 内部，且类型要 `impl Sealed`。
3. **挂到 `os`**：`src/lib.rs` 的 `pub mod os` 里加一条 `#[cfg(myos)] pub mod myos;`（与 unix/windows 并列）。
4. **泛化接缝宏（最难的一步）**：当前 `mkenum!`/`dispatch!`/`impmod!` 都是两臂（windows/unix）。要让 `myos` 作为**第三**个平台接入，需要：
   - `mkenum!`（[src/local_socket/enumdef.rs:18-57](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L18-L57)）增加第三个变体 `MyPipe(my_impl::$nm)` 及其 `From`/`Sealed` 实现；
   - `dispatch!`（[src/local_socket/enumdef.rs:2-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/enumdef.rs#L2-L16)）的 `match` 增加第三个 `#[cfg(myos)]` 臂；
   - `impmod!`（[src/macros.rs:4-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L4-L14)）增加第三条 `#[cfg(myos)] use …`；
   - `stream/enum.rs`、`listener/enum.rs`（及 tokio 版）顶部的 `use … as uds_impl/np_impl` 加上 `#[cfg(myos)] use … as my_impl;`。

**对比思考**：如果改成「**在已有平台内新增一种名字类型**」（例如 Linux 上新增一个特殊的抽象命名空间映射），清单会短得多——只需在 `name/type.rs` 里加一个 `tag_enum!(MyNameType)` + `impl NameType/PathNameType for MyNameType` + 后端 `name_type.rs` 里加一个 `map_*` 函数，**完全不必动三把接缝宏**。请说明为什么这条路径不碰宏、也不碰 `platform_check`。

**预期产出**：一份分层清晰的改动清单（门卫 / 芯目录 / os 挂载 / 接缝宏），以及一句对 enum dispatch 接入机制的总结：**「新后端 = 新芯 + 新路由 + 把宏加一臂；公共 API 名字不变。」**

> 这是一个设计型实践，重在把骨架看清楚，不要求实际编译。完成它意味着你已经能在脑子里「换芯」了。

## 6. 本讲小结

- `platform_check.rs` 是编译期门卫，用两条带 `#[cfg]` 的 `compile_error!` 在最早期挡掉不支持的目标操作系统与异类指针宽度，给出人话错误。
- `inspect-platform` 是一个独立 `[[bin]]` 诊断工具，在运行期实证地探测真实平台的 socket 行为（权限位、`fchmod` 生效时机等），供库开发者排查平台差异。
- `Sealed`（`pub(crate) trait Sealed {}`）是全库封印根基：公共 trait 把它列为 supertrait，外部 crate 因无法命名它而不能实现这些 trait，从而形成硬扩展边界。
- `os` 模块用 `#[cfg(unix)]`/`#[cfg(windows)]` 互斥编译两个后端；`impmod!`/`mkenum!`/`dispatch!` 三把宏构成「壳/芯」接缝，让公共层零开销派发到平台私有后端。
- 扩展分两条难度天差地别的路径：**新增平台后端**要泛化三把宏（动骨架）；**新增名字类型**只加 `tag_enum!` + `impl NameType` + 后端 `map_*`（局部改动）。
- 全库扩展性都遵循一条约定：后端类型必须在 interprocess **内部** `impl Sealed` 并实现封印 trait，外部无法冒充后端。

## 7. 下一步学习建议

- 本讲是 9 个单元的收官。若你想真正动手「换芯」，建议回看 **u2-l2（enum dispatch 宏）** 与 **u2-l3（impmod 后端注入）**，它们是本讲接缝机制的详细展开；动手前先在 **u4-l1/u4-l2（Unix/Windows 后端实现）** 里照抄一个现有后端的目录结构作为模板。
- 想理解「为什么 local socket 要做成 enum 而非 `pub use`」，可重读 **u2-l1（Local Socket 设计哲学）** 与 `src/local_socket.rs:5-27` 的模块文档（里面明确提到未来 Windows 可能引入第二后端）。
- 若你的兴趣在「名字类型 → 底层原语」的映射规则，**u2-l4（名称系统）** 是直接前驱；本讲 4.4 的「新增名字类型」路径正是建立在它的 `NameType`/`tag_enum!` 之上。
- 进阶挑战：照综合实践的清单，**真**实现一个最小的 mock 后端（哪怕只支持 `connect` 返回一个内存管道），体会「加一臂宏」的全过程——这是检验你是否真正看懂 interprocess 骨架的最好方式。
