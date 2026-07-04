# no_std、feature 与模块组织

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `#![no_std]` 到底改变了什么，以及它为什么不会让本 crate 「真的」脱离 `std`。
- 看懂 `Cargo.toml` 里 `[features]` 的 `default = ["std"]`、`std = ["crossbeam-utils/std"]` 是如何把 feature 在「本 crate → 所有内部模块 → 上游依赖」三层之间传递的。
- 逐行解释 `src/lib.rs` 里那一长串 `#[cfg(feature = "std")]` 的作用，并说明为什么注释里写明「禁用 `std` 尚不支持」。
- 理解 `internal` 这个 `#[doc(hidden)]` 隐藏模块存在的意义：它如何既不污染用户文档，又能被 `select!` 宏在展开后跨 crate 调用。
- 把 `#[macro_export]`、`$crate`、`pub use` 三者拼成一条完整的「宏 → internal → select 算法 → flavor」的代码路径。

本讲是「架构地图」类的讲义：我们不读任何 flavor 的内部算法，只看 `crossbeam-channel` 这整个 crate 是如何被「装配」起来的。它承接 u2-l1 建立的「公共类型壳 + 六 flavor」总览，并为后续阅读任何具体源码文件提供「这个文件是怎么被编译进来、又怎么暴露给外部」的坐标。

## 2. 前置知识

在开始前，请确认你理解下面几个概念（不熟悉也没关系，本讲会顺带复习）：

- **`no_std`**：Rust crate 的一种编译模式。一个 `#![no_std]` 的 crate 默认**只能**使用 `core`（与平台无关的最小基础库），不能直接用 `std`（包含线程、文件、网络、`Mutex`、`Instant` 等「需要操作系统」的东西）。它的价值是让 crate 能被用在内核、固件、`#![no_std]` 嵌入式环境里。
- **feature（特性）**：Cargo 的条件编译开关。`Cargo.toml` 里的 `[features]` 定义开关，源码里用 `#[cfg(feature = "xxx")]` 控制某段代码是否参与编译。
- **`extern crate`**：在 Rust 2018 之前，要用某个外部 crate 必须显式 `extern crate foo;`；2018 之后对 `std` 通常可省略。但在 `#![no_std]` crate 里要把 `alloc` / `std` 「重新接回来」，仍然需要显式 `extern crate`，本讲的 `lib.rs` 就是这么做的。
- **`#[macro_export]`**：把一个宏「导出到 crate 根」。即使宏定义在深层模块里，`#[macro_export]` 也会让它出现在 crate 根路径上，从而能被外部 crate 用 `crate_name::macro_name!()` 调用。
- **`$crate`**：宏内部的特殊标识符，在宏展开时会被替换成「定义该宏的那个 crate 的路径」。它是让宏在跨 crate 调用时仍能正确引用「自己 crate 内部东西」的关键。

如果你对 `select!` 宏的展开结果还没印象，可以先翻一眼 u2-l9（使用层）和 u3-l3（展开机制）。本讲会用到「宏展开后会调用 `$crate::internal::select(...)`」这一结论。

## 3. 本讲源码地图

本讲只读两个文件，外加对 `select_macro.rs` / `select.rs` 的一处指针式引用：

| 文件 | 作用 |
| --- | --- |
| [`src/lib.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs) | crate 的唯一入口与「装配清单」：crate 级属性（`#![no_std]`、lint）、`extern crate`、所有子模块声明、`internal` 隐藏模块、对外 `pub use`。本讲的「主角」。 |
| [`Cargo.toml`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml) | crate 的「配置面板」：版本、MSRV、`[features]`（`default`/`std`）、对 `crossbeam-utils` 的依赖配置。 |
| [`src/select_macro.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs) | `select!` / `select_biased!` / `crossbeam_channel_internal!` 三个 `#[macro_export]` 宏。本讲只看它们如何引用 `$crate::internal`。 |
| [`src/select.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | `internal` 模块 re-export 的真正来源（`SelectHandle`、`select`、`try_select` 等）。本讲只确认这些符号确实定义在此。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. `#![no_std]` 头部属性与 no_std 纪律
2. `std` feature 门控：从 Cargo 到模块的三层传递
3. 模块声明与 re-export 组织（含 `#[macro_export]`）
4. `internal` 隐藏模块：`select!` 宏的后门

### 4.1 `#![no_std]` 头部属性与 no_std 纪律

#### 4.1.1 概念说明

一个 Rust crate 默认是「链接到 `std`」的。如果在文件最顶部写上 `#![no_std]`（注意是 `!`，表示「作用于整个 crate」的内部属性），编译器就不再自动把 `std` 拉进来，crate 默认只能看到 `core`。

`crossbeam-channel` 选择 `#![no_std]` 的动机是**前瞻性兼容**：它希望自己有朝一日能在嵌入式 / 内核等无 `std` 环境里被使用。通道算法本身（原子操作、无锁队列）很多都不需要操作系统，理论上是可以脱离 `std` 的。所以 crate 的「骨架」按 `no_std` 来搭。

但「骨架按 `no_std` 搭」并不等于「现在就能在没有 `std` 的环境里跑」。事实上本 crate 当前的所有实现（线程阻塞、`Mutex`、`Instant` 等）都依赖 `std`，于是出现了一个有意思的局面：crate 顶部声明 `no_std`，却又立刻把 `std` 接回来——见 4.2。

为了不让「`no_std` 骨架」被后面的代码悄悄破坏，`lib.rs` 顶部还挂了三条专用的 clippy lint 来强制纪律（见 4.1.3）。

#### 4.1.2 核心流程

crate 级属性的处理顺序可以理解为：

```text
#![no_std]                      # 1. 关掉默认的 std 链接，只剩 core
#![doc(test(...))]              # 2. 配置文档测试行为（不影响运行时）
#![warn(... clippy::*_instead_of_* ...)]  # 3. 立法：能用 core/alloc 就不许用 std

#[cfg(feature = "std")]
extern crate alloc;             # 4. 需要堆分配？显式把 alloc 接回来
#[cfg(feature = "std")]
extern crate std;               # 5. 需要操作系统？显式把 std 接回来
```

关键点：在 `#![no_std]` 的 crate 里，`alloc` 和 `std` **不会自动可用**，必须用 `extern crate` 显式引入。这也是为什么 `extern crate` 这种「老写法」在本 crate 里依然存在——它不是怀旧，而是 `no_std` 的必需品。

#### 4.1.3 源码精读

crate 顶部的属性块在 [`src/lib.rs:328-344`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L328-L344)：

```rust
#![no_std]
#![doc(test(
    no_crate_inject,
    attr(allow(dead_code, unused_assignments, unused_variables))
))]
#![warn(
    missing_docs,
    unsafe_op_in_unsafe_fn,
    clippy::alloc_instead_of_core,
    clippy::std_instead_of_alloc,
    clippy::std_instead_of_core
)]

#[cfg(feature = "std")]
extern crate alloc;
#[cfg(feature = "std")]
extern crate std;
```

逐段说明：

- `#![no_std]`（第 328 行）：关掉默认 `std` 链接。这是整个 no_std 设计的总开关。
- `#![doc(test(no_crate_inject, attr(...)))]`（第 329-332 行）：配置文档里的 doctest。`no_crate_inject` 让 doctest 不自动注入 `extern crate crossbeam_channel;`（2018 之后 doctest 本就用路径，这里只是显式声明）；`attr(...)` 允许 doctest 里的样例代码有未使用的变量/赋值，避免文档示例触发 lint 噪音。
- `#![warn(...)]`（第 333-339 行）里的三条 clippy lint 是 no_std 纪律的「执法者」：
  - `clippy::std_instead_of_core`：你写的代码明明只用 `core` 就够了（比如 `core::sync::atomic`），却写了 `std::sync::atomic` → 报警。
  - `clippy::std_instead_of_alloc`：只用 `alloc` 就够了却用了 `std` → 报警。
  - `clippy::alloc_instead_of_core`：只用 `core` 就够了却用了 `alloc` → 报警。
  
  这三条合在一起，强制开发者按「能用 `core` 就别用 `alloc`，能用 `alloc` 就别用 `std`」的优先级选类型，从而把对 `std` 的真实依赖降到最低，为将来真正支持 `no_std` 铺路。
- `#[cfg(feature = "std")] extern crate alloc;` / `extern crate std;`（第 341-344 行）：把 `alloc` 和 `std` 显式接回来，但**仅在 `std` feature 开启时**。下一节会看到这个 feature 默认是开的。

#### 4.1.4 代码实践

**实践目标**：理解三条 clippy lint 各自把关什么，并体会 `extern crate` 在 `no_std` 下的必要性。

**操作步骤**（纯阅读 + 在你自己的临时项目里验证，**不要修改本仓库源码**）：

1. 打开 [`src/lib.rs:333-339`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L333-L339)，把三条 `clippy::*_instead_of_*` lint 抄下来。
2. 在你自己另建的一个临时 crate（`cargo new --lib scratch && cd scratch`）的 `lib.rs` 顶部加上 `#![no_std]` 和这三条 `#![warn(...)]`。
3. 在 `lib.rs` 里写一行 `pub fn f() { let _ = std::sync::atomic::AtomicUsize::new(0); }`，运行 `cargo clippy`。
4. 把它改成 `core::sync::atomic::AtomicUsize`，再跑一次 `cargo clippy`。

**需要观察的现象**：

- 第 3 步应触发 `clippy::std_instead_of_core` 警告，提示你 `std::sync::atomic` 在 `no_std` 下应改用 `core::sync::atomic`。
- 第 3 步还可能直接编译失败，因为 `#![no_std]` 下没有 `std` 这个路径——这恰好印证了「`std` 不会自动可用」。

**预期结果**：你会切身感受到这三条 lint 是怎么在「编译期」就把「不必要的 std 依赖」拦下来的。本 crate 正是靠它们维持 no_std 边界。

> 本实践需在你自己的临时 crate 中运行；不要在本仓库里改源码。如未在本地实际运行，可只做阅读理解部分。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `extern crate std;` 在普通的（非 `no_std`）crate 里通常不需要写，但在本 crate 里必须写？

**参考答案**：普通 crate 默认链接 `std`，编译器会自动把 `std` 注入作用域；而本 crate 顶部有 `#![no_std]`，默认不链接 `std`，所以必须用 `extern crate std;`（且仅在 `std` feature 开启时）显式把它接回来。

**练习 2**：`clippy::std_instead_of_alloc` 和 `clippy::alloc_instead_of_core` 各自阻止的是哪一种「过度依赖」？

**参考答案**：前者阻止「其实只需要堆分配（`alloc`）却用了 `std`」，后者阻止「其实连堆都不需要、只用 `core` 就够却用了 `alloc`」。二者共同把依赖按 `core < alloc < std` 的最小化原则收敛。

---

### 4.2 `std` feature 门控：从 Cargo 到模块的三层传递

#### 4.2.1 概念说明

上一节看到 crate 是 `#![no_std]` 的，但本 crate 现在的实现又确实需要 `std`。怎么调和？答案是 **feature 门控（feature gating）**：定义一个名为 `std` 的 feature，让它默认开启，并用 `#[cfg(feature = "std")]` 把所有需要 `std` 的代码包起来。

`crossbeam-channel` 把 feature 门控做到了「极致」：**几乎整个 crate**（所有子模块、`internal`、对外 `pub use`）都被 `#[cfg(feature = "std")]` 包裹。换句话说，关掉 `std` 之后，这个 crate 就剩一个空壳。这正是 Cargo.toml 里注释「Disabling `std` feature is not supported yet」（禁用 `std` 尚不支持）的含义。

feature 还会**跨 crate 传递**：本 crate 的 `std` feature 同时会打开上游依赖 `crossbeam-utils` 的 `std` feature，这通过 `std = ["crossbeam-utils/std"]` 这一行声明。

#### 4.2.2 核心流程

feature 的传递链可以画成三层：

```text
        ┌─ Cargo.toml: default = ["std"]   ─→ 用户默认就拿到 std
        │
本 crate├─ Cargo.toml: std = ["crossbeam-utils/std"]   ─→ ② 向上游依赖传递
        │
        └─ src/lib.rs: #[cfg(feature = "std")] mod xxx;  ─→ ③ 向下门控每个模块
                                   │
上游依赖└─ crossbeam-utils 也打开自己的 std feature（提供 Instant 等）
```

三层分别是：

1. **入口层**：`default = ["std"]` 保证普通用户 `cargo add crossbeam-channel` 时，`std` 默认开启，无需手动配置。
2. **传递层**：`std = ["crossbeam-utils/std"]` 表示「开启本 crate 的 `std`，也顺带开启 `crossbeam-utils` 的 `std`」。这是 Cargo 的 feature 联动机制。
3. **门控层**：`src/lib.rs` 里每个 `mod xxx;` 前都加 `#[cfg(feature = "std")]`，只有 `std` 开启时该模块才参与编译。

#### 4.2.3 源码精读

`Cargo.toml` 的 `[features]` 段在 [`Cargo.toml:26-33`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml#L26-L33)：

```toml
[features]
default = ["std"]

# Enable to use APIs that require `std`.
# This is enabled by default.
#
# NOTE: Disabling `std` feature is not supported yet.
std = ["crossbeam-utils/std"]
```

- 第 27 行 `default = ["std"]`：默认 feature 集合包含 `std`。
- 第 33 行 `std = ["crossbeam-utils/std"]`：`std` 这个 feature 不是空壳，它会激活依赖 `crossbeam-utils` 的同名 `std` feature。
- 第 32 行注释明确写了「禁用 `std` 尚不支持」。

对 `crossbeam-utils` 的依赖在 [`Cargo.toml:35-36`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml#L35-L36)：

```toml
[dependencies]
crossbeam-utils = { version = "0.8.18", path = "../crossbeam-utils", default-features = false, features = ["atomic"] }
```

两个细节值得注意：

- `default-features = false`：故意关掉 `crossbeam-utils` 的默认 feature，避免「默认就把 `std` 拉进来」。这样只有当本 crate 的 `std` 被显式开启时，才通过 `std = ["crossbeam-utils/std"]` 把它补回去——这是 no_std 友好的依赖写法。
- `features = ["atomic"]`：始终开启 `crossbeam-utils` 的 `atomic` feature，因为本 crate 的无锁实现离不开 `AtomicCell`、`CachePadded`、`Backoff`（这些在 `core` 可用，不需要 `std`）。

然后在 `src/lib.rs` 里，**全部十个内部模块**都被同一个门控包裹，见 [`src/lib.rs:346-366`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L346-L366)：

```rust
#[cfg(feature = "std")]
mod alloc_helper;

#[cfg(feature = "std")]
mod channel;
#[cfg(feature = "std")]
mod context;
#[cfg(feature = "std")]
mod counter;
#[cfg(feature = "std")]
mod err;
#[cfg(feature = "std")]
mod flavors;
#[cfg(feature = "std")]
mod select;
#[cfg(feature = "std")]
mod select_macro;
#[cfg(feature = "std")]
mod utils;
#[cfg(feature = "std")]
mod waker;
```

这十个模块各司其职（在 u1-l1 已有总览）：`channel`（对外壳 `Sender`/`Receiver`）、`context`/`waker`（阻塞唤醒）、`counter`（引用计数）、`err`（错误类型）、`flavors`（六种通道实现）、`select`/`select_macro`（选择机制）、`utils`（非毒 Mutex、shuffle、sleep_until）、`alloc_helper`（指向 `crossbeam-utils` 的分配器封装软链接）。

#### 4.2.4 代码实践

**实践目标**：列出所有受 `std` 门控的模块，并解释「为什么禁用 `std` 尚不支持」。

**操作步骤**：

1. 在仓库根目录（`crossbeam-channel/`）运行 `grep -n 'cfg(feature = "std")' src/lib.rs`，数一数一共有多少处门控。
2. 对照上面 4.2.3 的列表，把它们分成三类：(a) `extern crate`、(b) `mod` 模块声明、(c) `pub mod internal` 与 `pub use`。
3. 思考：如果把 `default-features = false`（关掉 `std`）去编译，crate 根上还会剩下哪些 `pub` 项？

**需要观察的现象**：

- `src/lib.rs` 里 `cfg(feature = "std")` 出现约 13 处（2 处 `extern crate`、10 处 `mod`、1 处 `pub mod internal`、1 处 `pub use`）。
- 关掉 `std` 后，crate 根上**没有任何** `pub` 项——连 `Sender`、`unbounded` 都消失了。

**预期结果**：你会得出结论——禁用 `std` 时 crate 虽然能编译（因为所有代码都被 cfg 掉了，只剩一个空的 `#![no_std]` crate），但它不再提供任何功能，对用户毫无用处。所以 Cargo.toml 才标注「not supported yet」：结构已为未来 `no_std` 实现留好位置，但当前实现尚未移植。这正是本讲 practice_task 要回答的核心问题。

> 该 grep 命令为只读操作，不修改源码。如未在本地运行，可根据本节列出的行号直接核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `crossbeam-utils` 的依赖要写 `default-features = false`，再单独开 `features = ["atomic"]`？

**参考答案**：为了让 `crossbeam-utils` 默认不把 `std` 拉进来（保持 no_std 友好），同时显式声明本 crate 真正常用的只有 `atomic` 这一个 feature（提供 `AtomicCell`/`CachePadded`/`Backoff`，不依赖 `std`）。只有当本 crate 的 `std` 被开启时，才通过 `std = ["crossbeam-utils/std"]` 把上游的 `std` 补上。

**练习 2**：假设有人把 `default = ["std"]` 改成 `default = []`，普通用户 `cargo add crossbeam-channel` 后会发生什么？

**参考答案**：默认不再开启 `std`，于是 crate 里所有 `#[cfg(feature = "std")]` 的模块都不会编译，用户拿到的将是一个空壳 crate——`Sender`、`unbounded` 等统统不存在，写任何通道代码都会编译失败。用户必须手动在 `Cargo.toml` 写 `features = ["std"]` 才能恢复功能。

---

### 4.3 模块声明与 re-export 组织（含 `#[macro_export]`）

#### 4.3.1 概念说明

一个 crate 对外暴露什么，不完全由「哪些东西是 `pub`」决定，还由「crate 根上的 `pub use`」决定。`crossbeam-channel` 采用的是经典的「内部按功能分模块、外部集中 re-export」策略：

- 内部：实现细节分散在 `channel`、`err`、`select` 等十几个模块里。
- 外部：在 `lib.rs` 末尾用一个大 `pub use` 把要给用户用的东西一次性摆到 crate 根，让用户写 `use crossbeam_channel::{unbounded, Sender};` 而不是 `use crossbeam_channel::channel::unbounded;`。

宏则是个例外。`select!`、`select_biased!` 用的是 `#[macro_export]`，它们不经过 `pub use` 也能自动出现在 crate 根。本节把这两套机制讲清楚。

#### 4.3.2 核心流程

对外暴露的两条路径：

```text
路径 A（普通类型/函数/错误）：
  定义在 channel:: / err:: / select::
      │
      └─ lib.rs 末尾 #[cfg(feature="std")] pub use crate::{...};
                              │
                              └─ 出现在 crate 根

路径 B（宏）：
  定义在 select_macro::
      │
      └─ #[macro_export] macro_rules! select { ... }
                              │
                              └─ 自动出现在 crate 根（无需 pub use）
```

注意：宏虽然在 `select_macro` 模块里定义，但 `#[macro_export]` 会无视模块路径，把它「提升」到 crate 根。所以 `lib.rs` 里看不到 `pub use select_macro::select;` 这样的语句——不需要。

#### 4.3.3 源码精读

`lib.rs` 末尾的对外 re-export 在 [`src/lib.rs:377-387`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L377-L387)：

```rust
#[cfg(feature = "std")]
pub use crate::{
    channel::{
        IntoIter, Iter, Receiver, Sender, TryIter, after, at, bounded, never, tick, unbounded,
    },
    err::{
        ReadyTimeoutError, RecvError, RecvTimeoutError, SelectTimeoutError, SendError,
        SendTimeoutError, TryReadyError, TryRecvError, TrySelectError, TrySendError,
    },
    select::{Select, SelectedOperation},
};
```

这一段一次性把对外 API 全摆到 crate 根，分三组：

- 来自 `channel`：核心类型 `Sender`/`Receiver` 及三个迭代器 `Iter`/`TryIter`/`IntoIter`，外加 6 个构造函数 `unbounded`/`bounded`/`after`/`at`/`tick`/`never`。
- 来自 `err`：10 个错误类型（u2-l3 已成体系讲解）。
- 来自 `select`：动态 API 的 `Select` 与 `SelectedOperation`（u2-l10）。

注意整个 `pub use` 也被 `#[cfg(feature = "std")]` 包着——这和 4.2 的结论一致：关掉 `std` 时，这些符号全部消失。

宏的「自动到根」机制则在 `select_macro.rs` 里。三个宏都带 `#[macro_export]`：

- [`src/select_macro.rs:22-24`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L22-L24)：内部宏 `crossbeam_channel_internal!`（`#[doc(hidden)]` 隐藏，仅供 `select!` 内部调用）。
- [`src/select_macro.rs:1135-1136`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1135-L1136)：用户入口 `select!`。
- [`src/select_macro.rs:1156-1157`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1156-L1157)：有偏入口 `select_biased!`。

看一眼 `select!` 的定义，注意它如何委托给内部宏，并在 [`src/select_macro.rs:1136-1146`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1136-L1146) 里植入 `_IS_BIASED`：

```rust
#[macro_export]
macro_rules! select {
    ($($tokens:tt)*) => {
        {
            const _IS_BIASED: bool = false;

            $crate::crossbeam_channel_internal!(
                $($tokens)*
            )
        }
    };
}
```

`$crate::crossbeam_channel_internal!(...)` 这一行很关键：`$crate` 在展开后会变成 `crossbeam_channel`（定义该宏的 crate），从而无论用户把自己的 crate 叫什么、无论宏在哪个模块里被调用，它都能正确引用到本 crate 的内部宏。`select_biased!` 唯一的差别就是 `const _IS_BIASED: bool = true;`（见 [`src/select_macro.rs:1157-1167`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L1157-L1167)）。

#### 4.3.4 代码实践

**实践目标**：验证「普通符号走 `pub use`、宏走 `#[macro_export]`」两条路径，并理解 `$crate` 的作用。

**操作步骤**：

1. 在 `crossbeam-channel/` 下运行 `cargo doc --no-deps --open`（或仅生成文档目录），观察文档里 crate 根下出现了哪些项。
2. 用 `grep -n 'pub use' src/lib.rs` 确认 re-export 的来源模块（`channel` / `err` / `select`）。
3. 用 `grep -n 'macro_export' src/select_macro.rs` 确认三个宏都标注了 `#[macro_export]`，但 `lib.rs` 里**没有** `pub use select_macro::select;`。
4. （可选）安装 `cargo-expand`（`cargo install cargo-expand`），在一个用到 `select!` 的临时项目里运行 `cargo expand`，在展开结果里搜索 `::internal::select` 与 `::crossbeam_channel_internal`，观察 `$crate` 被替换成了什么。

**需要观察的现象**：

- crate 根文档里既有 `Sender`/`unbounded` 等普通项，也有 `select!`/`select_biased!` 宏。
- `select!` 并不出现在 `lib.rs` 的 `pub use` 列表里，却依然在 crate 根可见——因为它靠 `#[macro_export]`。
- 展开结果里 `$crate` 被替换成 `::crossbeam_channel`（或等价路径），所以宏能在用户 crate 里正确找到本 crate 的 `internal` 和 `crossbeam_channel_internal!`。

**预期结果**：你会清晰看到「普通符号靠 `pub use`、宏靠 `#[macro_export]`」两套并行的对外暴露机制，并理解 `$crate` 是让宏跨 crate 仍能「回家」的关键。

> `cargo doc` 与 `cargo expand` 均为只读/本地实验，不修改本仓库源码。如本地未安装相关工具，步骤 1-3 的 grep 部分仍可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lib.rs` 的 `pub use` 列表里没有 `select`，但用户却能用 `use crossbeam_channel::select;`（或直接 `crossbeam_channel::select!`）？

**参考答案**：因为 `select!` 是宏，靠 `#[macro_export]` 自动提升到 crate 根，不需要、也不能通过 `pub use` 导出。`#[macro_export]` 会把宏挂到 crate 根路径上，无视它在哪个模块里定义。

**练习 2**：把 `select!` 宏体里的 `$crate::crossbeam_channel_internal!(...)` 改成直接写 `crossbeam_channel_internal!(...)`（去掉 `$crate::`），会出什么问题？

**参考答案**：在**本 crate 内部**调用可能还能工作，但当外部 crate 使用 `select!` 时，展开后的 `crossbeam_channel_internal!` 会在该外部 crate 的路径下找不到这个宏（因为它实际定义在 `crossbeam_channel` 里），导致编译失败。`$crate` 的作用就是确保展开后引用的是「定义宏的那个 crate」，从而跨 crate 可用。

---

### 4.4 `internal` 隐藏模块：`select!` 宏的后门

#### 4.4.1 概念说明

`select!` 宏展开后会生成一堆「调用本 crate 内部函数」的代码（比如 `internal::select(...)`、`internal::try_select(...)`）。这些函数原本是 `pub(crate)` 的内部实现，但宏被外部 crate 调用时，它生成的代码运行在**用户 crate** 里——`pub(crate)` 的东西用户 crate 访问不到。

这就产生一个矛盾：

- 这些函数**必须**对外可见（否则宏展开后的代码编译不过）。
- 但它们又**不应该**出现在用户文档里（它们是实现细节，不是给人直接调用的 API）。

`crossbeam-channel` 的解决方案就是 `internal` 模块：用 `pub mod`（对外可见）+ `#[doc(hidden)]`（文档里隐藏）的组合，做出一个「编译器看得到、文档读者看不到」的后门。

#### 4.4.2 核心流程

`internal` 后门的完整链路：

```text
用户写：  select! { recv(r) -> msg => ... }
            │  宏展开（在用户 crate 里）
            ▼
展开为：  $crate::internal::select(&mut _sel, _IS_BIASED)
            │  $crate → crossbeam_channel
            ▼
定位到：  crossbeam_channel::internal  （pub mod，外部可见）
            │
            ▼
re-export：pub use crate::select::{select, try_select, select_timeout,
                                   sender_addr, receiver_addr, SelectHandle};
            │
            ▼
真正定义在： src/select.rs
```

关键在于 `internal` 模块同时满足两个条件：

1. 它是 `pub mod`，所以从 crate 根路径可达，外部 crate 能引用。
2. 它带 `#[doc(hidden)]`，所以 docs.rs 生成的文档里看不到它，普通用户不会被它误导。

#### 4.4.3 源码精读

`internal` 模块定义在 [`src/lib.rs:368-375`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375)：

```rust
/// Crate internals used by the `select!` macro.
#[doc(hidden)]
#[cfg(feature = "std")]
pub mod internal {
    pub use crate::select::{
        SelectHandle, receiver_addr, select, select_timeout, sender_addr, try_select,
    };
}
```

逐行说明：

- `pub mod internal`：声明一个**对外公开**的模块。注意它本身是 `pub` 的，不是 `pub(crate)`——这正是让外部 crate 能访问的前提。
- `#[doc(hidden)]`：告诉 rustdoc「不要把这个模块写进文档」。于是普通用户翻 `crossbeam_channel` 的 API 文档时，根本看不到 `internal` 的存在。
- `#[cfg(feature = "std")]`：和所有其他模块一样受 `std` 门控。
- 模块内的 `pub use crate::select::{...}`：把 6 个符号从 `select` 模块 re-export 到 `internal` 名下。它们是：
  - `SelectHandle`（trait，定义在 [`src/select.rs:99`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99)）。
  - `select` / `try_select` / `select_timeout`（三个 select 内核入口函数，分别定义在 [`src/select.rs:474`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L474)、[`src/select.rs:456`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L456)、[`src/select.rs:494`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L494)）。
  - `sender_addr` / `receiver_addr`（取通道端地址用于身份校验，分别定义在 [`src/select.rs:524`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L524) 与 [`src/select.rs:528`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L528)）。

宏展开后对它们的调用可以在 `select_macro.rs` 的代码生成阶段看到，例如 [`src/select_macro.rs:763`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L763)（阻塞 select 分支）：

```rust
let _oper = $crate::internal::select(&mut $sel, _IS_BIASED);
```

以及非阻塞 / 超时分支在 [`src/select_macro.rs:785`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L785) 与 [`src/select_macro.rs:815`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L815)：

```rust
let _oper = $crate::internal::try_select(&mut $sel, _IS_BIASED);
...
let _oper = $crate::internal::select_timeout(&mut $sel, $timeout, _IS_BIASED);
```

这就形成了一个完整的闭环：宏（`#[macro_export]`，在 crate 根）→ `$crate::internal::xxx`（`pub mod` + `#[doc(hidden)]`）→ `crate::select::{xxx}`（真正实现）。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 `internal` 模块「对编译器可见、对文档隐藏」的双重属性，并追踪 `select!` 到 `internal` 的调用链。

**操作步骤**：

1. 运行 `cargo doc --no-deps`，然后在 `target/doc/crossbeam_channel/` 生成的文档里搜索 `internal`——你应该**找不到**它（因为 `#[doc(hidden)]`）。
2. 但用 `grep -rn '::internal::' src/select_macro.rs`，你能看到宏展开模板里到处引用 `$crate::internal::select` / `try_select` / `select_timeout`——证明它在编译期是被真实调用的。
3. 对照 [`src/lib.rs:371-374`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L371-L374) 的 `pub use` 列表与 [`src/select.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) 里这些符号的定义行号（4.4.3 已列出），确认 `internal` 里 re-export 的每一项都能在 `select.rs` 找到定义。
4. 思考：如果把这 6 个符号声明为 `pub(crate)` 而不是放进 `pub mod internal`，`select!` 宏在**外部 crate** 里还能用吗？

**需要观察的现象**：

- 文档里没有 `internal`，但源码里宏模板大量引用它。
- `internal` 里 re-export 的 6 个符号在 `select.rs` 都有对应的 `pub` 定义。

**预期结果**：你会理解 `#[doc(hidden)] pub mod internal` 是一种标准的「给宏用的后门」模式——既满足宏跨 crate 调用所需的可见性，又不污染公共 API 文档。练习第 4 步的答案是：不能。`pub(crate)` 的东西外部 crate 访问不到，`select!` 展开后的代码会编译失败；这正是 `internal` 必须是 `pub mod` 的原因。

> 本实践为只读（`cargo doc` / `grep`），不修改源码。

#### 4.4.5 小练习与答案

**练习 1**：`internal` 模块同时挂着 `pub mod` 和 `#[doc(hidden)]`。如果把 `#[doc(hidden)]` 去掉，对用户会有什么影响？对功能有影响吗？

**参考答案**：功能完全不受影响（可见性没变）。但 `internal` 及其内部函数会出现在 `crossbeam_channel` 的公共 API 文档里，让普通用户看到一批「本不该直接调用」的实现细节函数（`select`、`sender_addr` 等），造成文档噪音和误用风险。`#[doc(hidden)]` 就是为了避免这一点。

**练习 2**：为什么 `internal` 里 re-export 的函数不直接定义在 `internal` 模块里，而是从 `crate::select` re-export？

**参考答案**：因为这些函数（`select`/`try_select`/`SelectHandle` 等）同时也是 `Select` 动态 API 和 select 内核的实现，逻辑上属于 `select` 模块。把定义集中在 `select.rs` 里、再用 `internal` 做一个「面向宏的窗口」去 re-export，可以避免代码重复，同时把「给宏用的入口」和「给运行时 API 用的实现」在物理上解耦——`internal` 只是 `select` 的一扇窗户。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个「装配图」任务。

**任务**：为 `crossbeam-channel` 画一张完整的「编译装配图」，并回答三个问题。

**步骤**：

1. **特征与依赖层**。读 [`Cargo.toml:26-36`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml#L26-L36)，在图上标出：`default = ["std"]`、`std = ["crossbeam-utils/std"]`、`crossbeam-utils` 的 `default-features = false, features = ["atomic"]`。用箭头表示 feature 的传递方向。

2. **crate 属性层**。读 [`src/lib.rs:328-344`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L328-L344)，标出 `#![no_std]`、三条 clippy lint、`extern crate alloc/std` 的位置与作用。

3. **模块门控层**。读 [`src/lib.rs:346-366`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L346-L366)，把十个模块画成都被 `#[cfg(feature = "std")]` 「罩住」的方框。

4. **对外暴露层**。读 [`src/lib.rs:368-387`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L387)，分两条路径标注：`internal`（`pub mod` + `#[doc(hidden)]`，给 `select!` 宏用）和 `pub use`（给普通用户用）。再单独标出 `select!` / `select_biased!` / `crossbeam_channel_internal!` 走 `#[macro_export]` 自动到根。

5. **回答三个问题**：
   - (a) 如果把 `Cargo.toml` 改成 `default = []`（关闭默认 `std`），crate 根还剩哪些 `pub` 项？
   - (b) `internal` 模块为什么必须是 `pub mod` 而不是 `pub(crate)`？
   - (c) 用户写 `select! { recv(r) -> m => ... }`，从敲下代码到最终调用 `src/select.rs` 里的 `select` 函数，中间经过哪几跳？

**预期结果**：

- (a) 一个 `pub` 项都不剩——所有 `pub use` 与 `pub mod internal` 都被 `#[cfg(feature = "std")]` 门控，crate 变成空壳。
- (b) 因为 `select!` 宏在外部 crate 里展开后会生成 `$crate::internal::select(...)`，若 `internal` 是 `pub(crate)`，外部 crate 无法访问，编译失败。
- (c) 四跳：`select!`（`#[macro_export]` 在 crate 根）→ `$crate::crossbeam_channel_internal!`（内部宏，代码生成）→ `$crate::internal::select`（`#[doc(hidden)] pub mod` 后门）→ `crate::select::select`（真正实现，`src/select.rs:474`）。

完成这张图后，你就把本讲的全部知识点连成了一条从「Cargo 配置」到「宏展开」到「真实函数」的完整路径。

## 6. 本讲小结

- `crossbeam-channel` 顶部声明 `#![no_std]`，是一种前瞻性的 no_std 友好骨架；当前实现仍需 `std`，靠 `#[cfg(feature = "std")] extern crate alloc/std;` 在开启 `std` 时把它接回来。
- 三条 clippy lint（`std_instead_of_core` / `std_instead_of_alloc` / `alloc_instead_of_core`）在编译期强制「能用 `core` 就别用 `alloc`/`std`」的最小依赖纪律。
- `Cargo.toml` 用 `default = ["std"]` 让普通用户开箱即用，用 `std = ["crossbeam-utils/std"]` 把 feature 向上游传递，对 `crossbeam-utils` 则 `default-features = false, features = ["atomic"]` 保持 no_std 友好。
- `src/lib.rs` 里**全部**十个模块、`internal`、`pub use` 都被 `#[cfg(feature = "std")]` 门控；关掉 `std` 后 crate 编译成空壳，故 Cargo.toml 注明「禁用 `std` 尚不支持」。
- 对外暴露走两条路：普通类型/函数/错误经 `lib.rs` 末尾的 `pub use` 集中到 crate 根；三个宏经 `#[macro_export]` 自动到根，无需 `pub use`。
- `internal` 是 `#[doc(hidden)] pub mod` 后门：对编译器可见（让 `select!` 宏跨 crate 调用 `$crate::internal::select` 等），对文档隐藏（不污染用户 API），它 re-export 自 `crate::select` 的 6 个符号。

## 7. 下一步学习建议

本讲解的是「crate 怎么装配」。接下来建议：

- **回到 select 内核**：顺着 `internal` 这扇窗，去读 u3-l1（`run_select` 核心算法）和 u3-l2（`SelectHandle` trait 与各 flavor 对接），看 `src/select.rs` 里这些被 re-export 的函数到底怎么工作。
- **看宏展开细节**：读 u3-l3（`select!` 宏展开机制），对照本讲的 `internal` 后门，理解宏从语法到 `$crate::internal::xxx` 调用的完整代码生成过程。
- **对比 no_std 实践**：如果你对 `no_std` 感兴趣，可以对比 `crossbeam-utils`（它是真正能在 `no_std` 下工作的依赖，本 crate 依赖它的 `atomic` feature），体会「声明 no_std 骨架」与「真正实现 no_std」之间的差距——这正好解释了本 crate 「禁用 std 尚不支持」的现状。
- **工具模块**：若想了解 `alloc_helper` 这个软链接模块、以及 `utils.rs` 的非毒 `Mutex`/shuffle 如何同样受 `std` 门控，可读 u3-l5。
