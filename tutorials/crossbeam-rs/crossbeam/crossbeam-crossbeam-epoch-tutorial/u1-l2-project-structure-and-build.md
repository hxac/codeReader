# 项目结构、构建配置与特性开关

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 crossbeam-epoch 仓库的目录布局，以及 `src/` 下每个源文件负责什么职责。
- 读懂 `Cargo.toml` 中的三大特性开关（`std` / `alloc` / `loom`），并解释它们之间的依赖与互斥关系，尤其是「为什么同时关闭 `std` 和 `alloc` 不被支持」。
- 理解 `src/lib.rs` 顶部如何用 `cfg(...)` 条件编译，根据特性开关与目标平台「裁剪」出不同的模块集合，以及为何要为 loom 单独维护一份 `primitive` 抽象层。
- 读懂 `build.rs` 如何探测线程消毒器，并在编译期「捏出」`crossbeam_sanitize_thread` 这个 cfg 标志。

本讲是 u1-l1（EBR 思想）的延续：上一讲建立了「为什么需要延迟回收」的心智模型，本讲把这套机制对应的**代码物理形态**摆出来——它由哪些文件组成、怎么编译、在哪些平台上能跑。

## 2. 前置知识

阅读本讲前，建议你已经：

- 了解了上一讲 u1-l1 的核心结论：无锁数据结构里「逻辑移除」与「物理释放」必须分离，crossbeam-epoch 用 epoch（纪元）来做延迟回收。
- 大致知道 Rust 的 crate 是什么、`Cargo.toml` 是什么。
- 听说过 `feature`（特性开关）、`cfg`（条件编译）这两个 Cargo/rustc 概念。如果没有也没关系，下面会用通俗的方式解释。

几个本讲会反复出现的术语，先做个最简解释：

| 术语 | 一句话解释 |
| --- | --- |
| **feature（特性开关）** | 在 `Cargo.toml` 的 `[features]` 里声明的「可开关的编译选项」，编译时打开或关闭它，会启用或裁剪一部分代码。 |
| **`cfg(...)`** | Rust 的条件编译语法，形如 `#[cfg(feature = "std")]` 表示「仅当启用了 std 这个 feature 时才编译这段代码」。 |
| **`no_std`** | 表示这个 crate 不依赖 Rust 标准库 `std`，只能用更底层的 `core`（和可选的 `alloc`）。适合嵌入式、内核等没有操作系统的环境。 |
| **`alloc`** | 一个提供堆分配（`Box`、`Arc`、`Vec` 等）的 crate。比 `std` 小，比 `core` 大。 |
| **loom** | 一个用于对并发代码做「有限状态模型检验」的工具，能在单线程里枚举所有可能的线程交错，专门用来找数据竞争。 |
| **build script（build.rs）** | Cargo 在编译 crate 之前先编译并运行的一段小程序，常用来探测环境、生成代码或向下游传递 cfg 标志。 |

## 3. 本讲源码地图

本讲围绕下面几个文件展开，它们共同回答「这个 crate 长什么样、怎么编译」：

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml` | 声明 crate 名、版本、特性开关（`std`/`alloc`/`loom`）、依赖与目标平台依赖。这是「编译配置的总入口」。 |
| `build.rs` | 编译期探测脚本，唯一职责是探测是否启用了线程消毒器，并向下游发出 `crossbeam_sanitize_thread` cfg。 |
| `src/lib.rs` | crate 根模块。顶部是 crate 级文档；之后是 `#![no_std]` 声明、`extern crate`、`primitive` 抽象层、`const_fn!` 宏，以及所有子模块的 `mod` 声明与 `pub use` 重导出。它是「模块拼装的总装车间」。 |
| `src/sync/mod.rs` | `sync` 子模块的入口，只声明了 `list` 和 `queue` 两个内部子模块。本讲用它说明「子模块如何再往下分」。 |

> 说明：`src/` 下还有 `atomic.rs`、`collector.rs`、`default.rs`、`deferred.rs`、`epoch.rs`、`guard.rs`、`internal.rs`、`alloc_helper.rs` 等文件，它们属于后续讲义的主题（指针、Guard、Collector、回收链路等）。本讲只关心它们**如何被 `lib.rs` 在条件编译下整体启用或裁剪**，而不深入各自实现。

## 4. 核心概念与源码讲解

### 4.1 目录结构与源文件清单

#### 4.1.1 概念说明

阅读一个 Rust crate，第一件事通常是看清它的物理布局：根目录有什么、`src/` 下有什么、有没有测试、基准、示例和构建脚本。这能让你在后续读源码时迅速「对号入座」——知道某个函数大概率住在哪个文件里。

crossbeam-epoch 是 crossbeam 工作区（workspace）下的一个成员 crate，目录在仓库的 `crossbeam-epoch/` 子路径下。它的整体结构是一个典型的「库 crate + benches + examples + tests + build.rs」布局。

#### 4.1.2 核心流程

一个 Rust 库 crate 的典型编译流程是：

1. Cargo 读取 `Cargo.toml`，解析特性开关、依赖、目标平台。
2. 若存在 `build.rs`，Cargo 先编译并运行它；build.rs 可以通过 `cargo:rustc-cfg=...` 之类的指令向后续编译传递配置标志。
3. Cargo 编译 `src/lib.rs`（库 crate 的根），并按其中的 `mod` 声明递归纳入其他源文件。
4. `benches/`、`examples/`、`tests/` 下的代码只在对应场景（`cargo bench` / 运行示例 / `cargo test`）下编译。

#### 4.1.3 源码精读

先用一个表格把仓库实际跟踪的源文件清单列出来（来自 `git ls-files`），并标注职责：

| 路径 | 类别 | 职责（本讲视角） |
| --- | --- | --- |
| `Cargo.toml` | 构建配置 | 特性开关与依赖声明 |
| `build.rs` | 构建脚本 | 探测线程消毒器，发 cfg |
| `README.md` / `CHANGELOG.md` | 文档 | 说明与变更记录 |
| `src/lib.rs` | crate 根 | 模块拼装与重导出 |
| `src/atomic.rs` | 子模块 | `Atomic`/`Owned`/`Shared`/`Pointable` 等指针类型 |
| `src/collector.rs` | 子模块 | `Collector`、`LocalHandle` |
| `src/default.rs` | 子模块 | 默认收集器、`pin()` 等（仅 `std`） |
| `src/deferred.rs` | 子模块 | `Deferred`/`Bag` 延迟任务存储 |
| `src/epoch.rs` | 子模块 | epoch 数值表示与 `AtomicEpoch` |
| `src/guard.rs` | 子模块 | `Guard`、`unprotected` |
| `src/internal.rs` | 子模块 | `Global`/`Local` 等内部实现 |
| `src/alloc_helper.rs` | 子模块 | 自定义分配相关的辅助 |
| `src/sync/mod.rs` | 子模块入口 | 声明 `list`、`queue` |
| `src/sync/list.rs` | 子模块 | 无锁侵入式链表 |
| `src/sync/queue.rs` | 子模块 | Michael-Scott 无锁队列 |
| `tests/loom.rs` | 集成测试 | loom 模型检验 |
| `benches/pin.rs`、`benches/defer.rs`、`benches/flush.rs` | 基准 | pin/defer/flush 性能测量 |
| `examples/sanitize.rs` | 示例 | 多线程压测（配合 thread sanitizer） |

注意 `src/sync/mod.rs` 非常短，它本身不带实现，只把两个子模块「挂」出来：

[crossbeam-epoch/src/sync/mod.rs:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/mod.rs#L1-L3) —— 这两行声明了 `list` 和 `queue` 两个 `pub(crate)` 子模块，真正的实现分别在 `sync/list.rs` 与 `sync/queue.rs`。`pub(crate)` 表示这两个模块对 crate 内可见、但不对外暴露。

> 这就是一个常见的 Rust 模块组织手法：把一组相关功能放进一个目录（`sync/`），目录里放一个 `mod.rs` 作为入口，再用 `mod xxx;` 把同目录下的 `xxx.rs` 纳入。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手把目录结构和「`mod` → 文件」的映射跑通。

1. **实践目标**：验证「`lib.rs` 里每一个 `mod xxx;` 声明都对应 `src/` 下一个真实文件」。
2. **操作步骤**：
   - 在仓库的 `crossbeam-epoch/` 目录下，列出 `src/` 的全部 `.rs` 文件。
   - 打开 `src/lib.rs`，找到所有形如 `mod xxx;` 的声明。
   - 把两份清单对齐：每个 `mod xxx;` 应当能在 `src/` 里找到 `xxx.rs`（或 `xxx/mod.rs`）。
3. **需要观察的现象**：`mod atomic;` 对应 `src/atomic.rs`；`mod sync;` 对应 `src/sync/mod.rs`（注意这里是目录形式）；而 `mod default;`、`mod guard;` 等都一一对应。
4. **预期结果**：`lib.rs` 中所有「裸 `mod` 声明」都能在文件系统中找到对应文件，没有「悬空」声明。例外是 `primitive`（见 4.3，它是一个内联模块，声明和实现都写在 `lib.rs` 里，没有独立文件）和 `sealed`（同理内联）。
5. 如果某条 `mod` 找不到文件，先检查它是否被 `#[cfg(...)]` 包住——条件编译不成立时该声明根本不参与编译。

#### 4.1.5 小练习与答案

**练习 1**：`src/sync/` 目录下有 `mod.rs`、`list.rs`、`queue.rs` 三个文件。如果改成「单文件」组织，应该怎么合并？为什么本项目选择目录形式？

> **参考答案**：可以把 `list` 和 `queue` 的内容直接搬进 `sync.rs`。本项目选择目录形式是因为 `list` 与 `queue` 各自代码量较大、相对独立，分文件更易维护；这也是 Rust 社区对「较大子模块」的常见约定。

**练习 2**：`benches/` 和 `tests/` 下的文件会出现在「正常 `cargo build`」的编译产物里吗？

> **参考答案**：不会。`benches/` 只在 `cargo bench`（或被 bench 目标引用）时编译；`tests/` 下的集成测试只在 `cargo test` 时编译。普通 `cargo build` 只编译库本身与它的依赖。

### 4.2 三大特性开关：std / alloc / loom

#### 4.2.1 概念说明

crossbeam-epoch 是一个 `#![no_std]` crate（见 4.3），意味着它默认不依赖标准库。但「内存回收」这种功能在不同运行环境下能用的基础设施不同：

- **`std` 环境**：有线程（`std::thread`）、线程局部存储（`thread_local!`）等。默认收集器需要「每个线程一个本地参与者」，这依赖 `thread_local`，所以**只有开了 `std` 才能用最便捷的 `pin()`**。
- **`no_std` + `alloc` 环境**：没有线程局部存储，但仍然有堆（`Box`/`Arc`）。此时除「默认收集器」外的几乎所有 API（指针、Guard、Collector）都可用——你可以**自己**管理参与者。
- **`loom`**：不是给生产用的，而是给**并发测试**用的。开启后，crate 内部的原子操作、`UnsafeCell`、`Arc` 等会被替换成 loom 的版本，从而能枚举线程交错、发现数据竞争。

这三种「能力」在 `Cargo.toml` 里被建模成三个 feature。

#### 4.2.2 核心流程

特性之间的依赖关系可以画成一张小图（箭头表示「开启它会自动连带开启」）：

```
default ──► std ──► alloc
                     │
              (alloc 本身也可单独开)
loom ──► crossbeam-utils/loom  （且需要 cfg(crossbeam_loom) 才真正引入 loom-crate）
```

关键不变量：

1. **`std` 隐含 `alloc`**：开 `std` 自动开 `alloc`（因为 `std = ["alloc", ...]`）。所以「有 std 没 alloc」是不可能的。
2. **`alloc` 和 `std` 至少要开一个**：注释明确写了「同时关闭 `std` 和 `alloc` 不被支持」。原因见 4.2.4 的实践。
3. **`loom` 是测试专用、且不受 semver 保障**：它的注释里特别声明这一点，意味着跨小版本都可能改。

#### 4.2.3 源码精读

先看 `Cargo.toml` 的 `[features]` 段：

[crossbeam-epoch/Cargo.toml:26-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L26-L43) —— 这里集中声明了三大特性开关。逐条解读：

- `default = ["std"]`：默认开启 `std`。所以你 `cargo add crossbeam-epoch` 后，开箱即用就是 std 模式。
- `std = ["alloc", "crossbeam-utils/std"]`：开 `std` 会自动开 `alloc`（连带本 crate 的 `alloc` feature），并要求依赖 `crossbeam-utils` 也开它的 `std`。
- `alloc = []`：本身不带额外依赖，只是一个「标记位」，用来在 `lib.rs` 里用 `#[cfg(feature = "alloc")]` 启用相关模块。
- 注释里那句「Disabling both `std` *and* `alloc` features is not supported yet.」在 [Cargo.toml:33-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L33-L37)。
- `loom = ["loom-crate", "crossbeam-utils/loom"]`：开启 loom 测试支持，并要求 `crossbeam-utils` 也开 loom。

再看依赖声明部分，注意 `loom-crate` 是「目标平台条件 + 可选」依赖：

[crossbeam-epoch/Cargo.toml:45-53](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L45-L53) —— 几个要点：

- `crossbeam-utils` 用了 `path = "../crossbeam-utils"`，因为它们同属一个 workspace，开发期直接走路径依赖；同时 `default-features = false, features = ["atomic"]`，只启用它需要的 `atomic` 能力。
- `[target.'cfg(crossbeam_loom)'.dependencies]` 这一段很特别：只有当编译环境设置了 `cfg(crossbeam_loom)` 时，`loom-crate` 才会被引入，而且它是 `optional = true`（受 `loom` feature 控制）。换句话说，**真正用到 loom 需要同时满足「开了 `loom` feature」和「设置了 `cfg(crossbeam_loom)`」**。这是 loom 测试通常通过 `RUSTFLAGS="--cfg crossbeam_loom" cargo test ...` 来跑的原因。

> 顺带一提，`[lints] workspace = true`（[Cargo.toml:58-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L58-L59)）表示 lint 配置由 workspace 根 `Cargo.toml` 统一管理，本 crate 不单独声明。

#### 4.2.4 代码实践

这是本讲的主实践，对应规格里的实践任务。

1. **实践目标**：亲手验证三个特性组合的编译行为，并用源码解释「为什么同时关 `std` 和 `alloc` 不被支持」。
2. **操作步骤**：
   - 新建一个实验 crate（`cargo new epoch-f features` 之类），把 crossbeam-epoch 配成路径依赖：
     ```toml
     [dependencies]
     crossbeam-epoch = { path = "../<你的路径>/crossbeam-epoch", default-features = false }
     ```
   - 分别尝试以下三种 `features` 配置并 `cargo check`：
     - (a) `features = ["std"]`（默认）
     - (b) `default-features = false, features = ["alloc"]`（纯 no_std + alloc）
     - (c) `default-features = false`（两者都不开）
   - 对 (c)，在你的 `main.rs` / `lib.rs` 里尝试 `use crossbeam_epoch::Atomic;`，观察编译器报什么。
3. **需要观察的现象**：
   - (a) 正常通过，`Atomic`、`pin` 等都可用。
   - (b) 应能通过，`Atomic`、`Guard`、`Collector` 可用，但 `pin` / `default_collector` / `is_pinned` 这些**不可用**（它们在 `default.rs` 里，受 `#[cfg(feature = "std")]` 保护）。
   - (c) `use crossbeam_epoch::Atomic;` 会报「cannot find type `Atomic` in crate root」之类的错误——因为整个 `atomic` 模块都没有被编译进来。
4. **预期结果 + 原因解释**：在配置 (c) 下，`feature = "alloc"` 为假。回到 [src/lib.rs:153-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L153-L168)，你会看到 `atomic`、`collector`、`guard` 等所有核心模块都被 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 守卫；`alloc` 关闭时这些模块**全部消失**，`pub use`（[src/lib.rs:170-177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L170-L177)）也就重导不出任何东西。crate 变成「空壳」，自然不可用——这就是「不支持同时关闭 std 和 alloc」的真正含义：它不是编译报错，而是**功能被裁剪殆尽**。
5. 现象属于「待本地验证」：上面 (a)/(b)/(c) 的具体报错文案随 rustc 版本变化，请以你本机实际输出为准，重点确认 (c) 下 `Atomic` 确实不存在。

#### 4.2.5 小练习与答案

**练习 1**：假设有用户写 `features = ["std"]` 但同时又 `default-features = false`，`alloc` 会被启用吗？

> **参考答案**：会。因为 `std = ["alloc", ...]`，开启 `std` 这个 feature 会自动连带开启 `alloc`，与 `default-features` 是否为 false 无关。

**练习 2**：为什么 `loom` feature 的注释要专门强调它「不在 semver 保障范围内」？

> **参考答案**：loom 用于内部并发测试，它的 API 和 loom 模型本身演进较快，且只在开发期使用。声明不受 semver 约束，是为了让维护者能随时调整 loom 相关代码，而不必把它当成对下游稳定承诺的公共 API。

### 4.3 cfg(target_has_atomic) 与模块的条件编译

#### 4.3.1 概念说明

除了「特性开关」这种**用户可选**的裁剪维度，crossbeam-epoch 还要面对一个**由目标平台决定**的维度：**这个平台到底支不支持原子操作？**

epoch 回收的根基是「用原子变量读写 epoch、做 CAS」。但并非所有目标平台都有指针宽度的原子指令（一些嵌入式/特殊架构没有）。Rust 用 `cfg(target_has_atomic = "ptr")` 来表达「目标平台支持指针宽度的原子操作」；`cfg(target_has_atomic = "64")` 则表达「支持 64 位原子」。

因此 crossbeam-epoch 的核心模块只在「平台支持原子 + 开了 alloc」时才存在。这二者缺一不可。

另一个关键是 **loom 抽象层**。为了让同一份业务代码既能跑在真实硬件上、又能跑在 loom 模型检验里，crate 在 `lib.rs` 顶部维护了一个叫 `primitive` 的内部模块，根据是否处于 loom 环境，把 `cell::UnsafeCell`、`sync::atomic::*`、`sync::Arc`、`thread_local` 这些「原语」指向不同的实现：

- **loom 模式**：指向 `loom::cell::UnsafeCell`、`loom::sync::atomic::*`、`loom::sync::Arc`、`loom::thread_local`。
- **真实模式**：指向 `core::cell::UnsafeCell`、`core::sync::atomic`、`alloc::sync::Arc`、`std::thread_local`，外加一个手写的 `UnsafeCell` wrapper 把它的 API 对齐到 loom 的形状。

这样，业务代码只 `use crate::primitive::...`，就能在两种环境下无缝切换。

#### 4.3.2 核心流程

`lib.rs` 的「条件拼装」逻辑可以总结为四步：

1. 声明 `#![no_std]`（[src/lib.rs:51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L51)），表示默认不依赖标准库。
2. 按需 `extern crate`：loom 模式下引入 `loom_crate` 别名为 `loom`；std 模式下显式引入 `std`；alloc 模式下引入 `alloc`。
3. 选择并定义 `primitive` 抽象层（两套，由 cfg 二选一）。
4. 用 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 守卫，统一启用核心模块，并 `pub use` 重导出公共 API。

#### 4.3.3 源码精读

先看 crate 根的 `#![no_std]` 与 lint 声明：

[crossbeam-epoch/src/lib.rs:51-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L51-L67) —— 关键点：

- `#![no_std]` 让 crate 默认只链接 `core`。
- `#[cfg(crossbeam_loom)] extern crate loom_crate as loom;`：只有设置了 `cfg(crossbeam_loom)` 才把 loom crate 引入并起别名。
- `#[cfg(feature = "std")] extern crate std;`：在 `#![no_std]` 的 crate 里，要用 `std` 就得显式 `extern crate`，并且它受 `std` feature 控制。

接着是 loom 版的 `primitive`（注意它把 `AtomicU64` 又额外用 `target_has_atomic = "64"` 守卫）：

[crossbeam-epoch/src/lib.rs:69-91](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L69-L91) —— 这一段把 `UnsafeCell`、`AtomicPtr/AtomicUsize/AtomicU64`、`Ordering`、`fence`、`Arc`、`thread_local` 全部指向 loom 实现。注意第 81-86 行的注释：loom 暂不支持 `compiler_fence`，这里用 `fence` 临时顶替（更强，可能漏报一些竞争），这是已知权宜之计。

再看真实环境的 `primitive`：

[crossbeam-epoch/src/lib.rs:92-131](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L92-L131) —— 这里的亮点是手写的 `UnsafeCell` wrapper（第 99-121 行）。注释解释了原因：loom 的 `UnsafeCell` API 和标准库的不完全一样，为了让业务代码对「是否在 loom 下」无感，这里把标准库 `core::cell::UnsafeCell` 包了一层，提供和 loom 一致的 `with` / `with_mut` 方法（[src/lib.rs:112-120](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L112-L120)）。另外 `Arc` 受 `feature = "alloc"` 守卫（[src/lib.rs:124-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L124-L125)），`thread_local` 受 `feature = "std"` 守卫（[src/lib.rs:129-130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L129-L130)）——这正对应 4.2 里说的「`thread_local` 只有 std 才有」。

> 顺带一提 `extern crate alloc` 也在条件编译内：[src/lib.rs:133-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L133-L134) 表示只在 `alloc + 原子` 同时满足时才引入 `alloc` crate。

最关键的「核心模块统一守卫」在这里：

[crossbeam-epoch/src/lib.rs:153-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L153-L168) —— 每一个核心模块（`alloc_helper`、`atomic`、`collector`、`deferred`、`epoch`、`guard`、`internal`、`sync`）都套了完全相同的 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]`。这等价于说：「只要开了 alloc 且平台支持指针原子，整套核心就启用」。这也是 4.2.4 实践里 (c) 配置会「空壳」的根因。

公共 API 的重导出：

[crossbeam-epoch/src/lib.rs:170-177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L170-L177) —— 把 `Atomic`、`Owned`、`Shared`、`Pointable`、`Pointer`、`Collector`、`LocalHandle`、`Guard`、`unprotected` 以及 CAS 相关的两个错误类型对外暴露。注意它们也戴着同一个 cfg 守卫，所以「关 alloc」时这条 `pub use` 也会消失。

最后，「默认收集器」相关只受 `std` 控制：

[crossbeam-epoch/src/lib.rs:184-187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187) —— `mod default;` 与 `pub use crate::default::{default_collector, is_pinned, pin};` 都只戴 `#[cfg(feature = "std")]`。这解释了 4.2.4 里配置 (b) 为什么「有 Atomic 但没有 `pin()`」：核心模块在 `alloc` 下就启用，而便捷的 `pin()` 要等 `std`。

#### 4.3.4 代码实践

1. **实践目标**：通过「读 cfg」反推「在给定 feature 组合下，crate 根会 `pub` 出哪些符号」。
2. **操作步骤**：
   - 准备一张表格，列分别是：符号（`Atomic`、`Guard`、`pin`、`default_collector`）。
   - 对三种配置 (a) std、(b) alloc-only、(c) 都关，逐个判断该符号是否存在。
   - 判断依据：查 `src/lib.rs` 中该符号所在 `mod` / `pub use` 行的 `#[cfg(...)]` 守卫条件，对照配置里 `feature = "alloc"`、`feature = "std"` 的真假。
3. **需要观察的现象**：你应该得到类似下表（✓=存在，✗=不存在）：

   | 符号 | (a) std | (b) alloc-only | (c) 都关 |
   | --- | --- | --- | --- |
   | `Atomic` | ✓ | ✓ | ✗ |
   | `Guard` | ✓ | ✓ | ✗ |
   | `Collector` | ✓ | ✓ | ✗ |
   | `pin` / `default_collector` / `is_pinned` | ✓ | ✗ | ✗ |

4. **预期结果**：上表应与「`atomic` 等受 `alloc` 守卫，`default` 受 `std` 守卫」的源码事实一致。
5. 「待本地验证」：如果你在 (b) 下 `use crossbeam_epoch::pin;`，编译器应当报「cannot find function `pin`」；请以本机输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`#[cfg(target_has_atomic = "ptr")]` 和 `#[cfg(target_has_atomic = "64")]` 有什么区别？为什么 loom 版 `primitive` 里对 `AtomicU64` 单独用后者？

> **参考答案**：前者表示「支持指针宽度（usize 宽）的原子」，后者表示「支持 64 位原子」。指针宽度不一定等于 64 位（如某些 32 位平台）。loom 版 `primitive` 里 `AtomicU64` 只在 64 位原子可用时才从 loom 导入，这与真实模式下 `epoch.rs` 用 `AtomicU64`（64 位平台）或退化为 `AtomicUsize` 的取舍保持一致。

**练习 2**：为什么真实模式下要给标准库 `UnsafeCell` 写一个 wrapper，而不是直接 `use core::cell::UnsafeCell`？

> **参考答案**：因为 loom 的 `UnsafeCell` 暴露的是 `with` / `with_mut`（接受闭包）这套 API，而标准库是 `get()`（返回 `*mut T`）。为了让业务代码写一份就能在 loom 与真实环境间切换，这里把标准库版本包一层，提供和 loom 同形的 API。这正是 loom 文档推荐的「处理 API 差异」做法。

### 4.4 build.rs 与 crossbeam_sanitize_thread cfg

#### 4.4.1 概念说明

有些「编译期配置」不是用户在 `Cargo.toml` 里选的，而是要靠一个**编译期探测脚本**（build script，文件名固定为 `build.rs`）去「问环境」。crossbeam-epoch 用 build.rs 来探测一件事：**当前编译是否启用了 ThreadSanitizer（线程消毒器）？**

ThreadSanitizer（TSan）是编译器自带的工具，能在运行时检测数据竞争。crossbeam-epoch 的 `examples/sanitize.rs` 就是一个配合 TSan 跑的多线程压测。为了让 crate 内部代码「知道自己正被 TSan 检查」（从而可能调整某些与 TSan 不友好的 hack，例如 4.x 后续讲到的 x86 屏障 hack），需要在编译期生成一个 cfg 标志：`crossbeam_sanitize_thread`。

> 重要：build.rs 顶部的注释明确写了「The rustc-cfg emitted by the build script are *not* public API.」——也就是说 `crossbeam_sanitize_thread` 不是给下游用户依赖的稳定接口，维护者可以随时改。

#### 4.4.2 核心流程

build.rs 的逻辑极其简单，伪代码如下：

```
读取环境变量 CARGO_CFG_SANITIZE（可能不存在，默认空串）
如果其中包含 "thread"：
    输出 cargo:rustc-cfg=crossbeam_sanitize_thread
否则：
    什么都不做
```

其中 `CARGO_CFG_SANITIZE` 是 Cargo 在运行 build.rs 时注入的环境变量，它的值来自 `--cfg sanitize="..."` 之类的设置（注意 `cfg(sanitize = "..")` 在 rustc 里尚未稳定，所以走环境变量而非直接 `cfg!`）。

`cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)` 这一行则是告诉 rustc「请把 `crossbeam_sanitize_thread` 当成一个『已知的、可能为真也可能为假』的 cfg」，这样 `check-cfg`（检查未知 cfg）就不会对它报警。

#### 4.4.3 源码精读

整个 build.rs 只有十几行：

[crossbeam-epoch/build.rs:5-14](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/build.rs#L5-L14) —— 解读：

- 第 6 行 `cargo:rerun-if-changed=build.rs`：告诉 Cargo「只有 build.rs 自己变化时才需要重跑这个脚本」，避免每次都跑。
- 第 7 行 `cargo:rustc-check-cfg=cfg(crossbeam_sanitize_thread)`：登记这个 cfg，避免 `unexpected_cfgs` 警告（[build.rs:7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/build.rs#L7)）。
- 第 10 行读取 `CARGO_CFG_SANITIZE`，`unwrap_or_default()` 在变量不存在时返回空字符串（[build.rs:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/build.rs#L10)）。
- 第 11-13 行：若该值包含 `"thread"`，则输出 `cargo:rustc-cfg=crossbeam_sanitize_thread`，于是 crate 内部代码就可以用 `#[cfg(crossbeam_sanitize_thread)]` 来写「TSan 专用分支」。

#### 4.4.4 代码实践

1. **实践目标**：观察 build.rs 如何响应 TSan 设置，并确认 `crossbeam_sanitize_thread` 真的被注入。
2. **操作步骤**：
   - 在 `crossbeam-epoch/` 下，先用普通方式 `cargo build`，再观察 `target/` 下 build 脚本的 `output`（通常在 `target/debug/build/crossbeam-epoch-<hash>/output`）。
   - 然后用 TSan 重跑，例如：
     ```bash
     RUSTFLAGS="-Z sanitizer=thread" cargo +nightly build
     ```
     （`sanitize="thread"` 设置目前需要 nightly，且仅支持部分目标如 `x86_64-unknown-linux-gnu`。）
   - 再次查看 build 脚本的 `output` 文件，对比是否多出一行 `cargo:rustc-cfg=crossbeam_sanitize_thread`。
3. **需要观察的现象**：普通构建的 `output` 里没有 `crossbeam_sanitize_thread`；TSan 构建的 `output` 里应当出现这一行。
4. **预期结果**：与 build.rs 第 11-13 行的逻辑一致——只有 `CARGO_CFG_SANITIZE` 含 `"thread"` 时才注入该 cfg。
5. 「待本地验证」：具体能否启用 TSan 取决于你的工具链（nightly）与目标平台；若环境不具备，可改为只读 `output` 文件确认普通构建下该 cfg **不**被注入即可。务必不要修改 `build.rs` 或源码。

#### 4.4.5 小练习与答案

**练习 1**：如果不写第 7 行 `cargo:rustc-check-cfg=...`，会怎样？

> **参考答案**：新版 rustc 会启用 `unexpected_cfgs` 检查，对 `cfg(crossbeam_sanitize_thread)` 这种「未被任何 feature 或内置定义覆盖」的 cfg 发出警告。第 7 行的作用就是预先登记它，告诉 rustc「我知道这个 cfg，它可能为真也可能为假」，从而消除警告。

**练习 2**：为什么 build.rs 用 `env::var("CARGO_CFG_SANITIZE")` 而不是直接写 `#[cfg(sanitize = "thread")]`？

> **参考答案**：因为 `cfg(sanitize = "..")` 在 rustc 里尚未稳定，不能直接在稳定的 `cfg(...)` 表达式里用。Cargo 会在运行 build.rs 时把 `sanitize` 配置以环境变量 `CARGO_CFG_SANITIZE` 的形式传进来，所以在 build.rs 里读环境变量、再转发成自定义的 `crossbeam_sanitize_thread` cfg，是一种兼容稳定工具链的做法。

## 5. 综合实践

把本讲的四个模块串起来，完成一个「**给 crossbeam-epoch 画一张编译期裁剪决策图**」的小任务：

1. 在实验 crate 里把 crossbeam-epoch 配为路径依赖（见 4.2.4）。
2. 准备一张「决策表」，自变量为：`feature=std`（真/假）、`feature=alloc`（真/假）、`target_has_atomic="ptr"`（真/假）、`cfg(crossbeam_loom)`（真/假）、`CARGO_CFG_SANITIZE` 含 thread（真/假）。
3. 选定一组取值后，依据本讲讲过的 cfg 守卫，预测以下问题的答案：
   - `Atomic` 是否可用？
   - `pin()` 是否可用？
   - `primitive::cell::UnsafeCell` 指向 loom 版还是标准库版？
   - `crossbeam_sanitize_thread` 是否被注入？
4. 用 `cargo check`（必要时带 `--cfg crossbeam_loom` 或 TSan 的 `RUSTFLAGS`）验证你的预测。
5. 把预测与实际结果整理成一页笔记，重点写清楚「同时关 std 和 alloc 时 crate 为何变空壳」这一条。

完成这个任务后，你应当能在不打开任何子模块实现的前提下，仅凭 `Cargo.toml` + `build.rs` + `src/lib.rs` 顶部，就推断出任意环境下 crate 会暴露什么。

## 6. 本讲小结

- crossbeam-epoch 是一个典型库 crate，目录由 `src/`（核心实现 + `sync/` 子目录）、`tests/`、`benches/`、`examples/`、`build.rs`、`Cargo.toml` 组成；`src/lib.rs` 是模块「总装车间」。
- 三大特性开关：`default = ["std"]`；`std` 隐含 `alloc`；`alloc` 是标记位；`loom` 用于并发测试且不受 semver 保障。同时关 `std` 和 `alloc` 会让核心模块全部被裁剪，crate 变空壳，因此不被支持。
- 核心模块统一受 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 守卫；`pin`/`default_collector`/`is_pinned` 额外需要 `feature = "std"`。
- crate 在 `lib.rs` 顶部用 `primitive` 抽象层屏蔽 loom 与真实环境的差异（`UnsafeCell`/原子/`Arc`/`thread_local`），并为标准库 `UnsafeCell` 写了一个对齐 loom API 的 wrapper。
- `build.rs` 通过读 `CARGO_CFG_SANITIZE` 探测线程消毒器，按需注入 `crossbeam_sanitize_thread` cfg（非公共 API），并用 `rustc-check-cfg` 登记。
- 仓库还通过 workspace（`[lints] workspace = true`）统一 lint、用 `crossbeam-utils` 的路径依赖（`default-features = false, features = ["atomic"]`）共享底层能力。

## 7. 下一步学习建议

本讲解决的是「crate 长什么样、怎么编译」。接下来建议进入第 2 单元，从**指针三剑客**开始读真正的实现：

- 先读 `src/lib.rs` 顶部关于 `Atomic`/`Shared`/`pin` 的 crate 级文档（[src/lib.rs:1-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L1-L49)），这是官方对「指针 / pin / 垃圾」三段式心智模型的精炼总结。
- 然后进入 u1-l3，亲手写一个最小可运行的 `pin()` + `Atomic` 例子，把本讲的「std 模式」跑起来。
- 之后按大纲顺序：u2-l4（`Pointable`）→ u2-l5（`Atomic` 与 tag）→ …，逐步深入 `src/atomic.rs`。

如果你对 loom 抽象层感兴趣，可以把 u6-l22（no_std/loom 可移植性）作为延伸阅读，它会更细地讲 `primitive`、`alloc_helper` 与 strict-provenance 处理。
