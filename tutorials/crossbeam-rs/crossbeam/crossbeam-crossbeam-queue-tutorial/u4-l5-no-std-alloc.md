# no_std / alloc 支持与自定义全局分配器

## 1. 本讲目标

本讲是专家层最后一篇，把目光从「队列算法本身」移到「这些算法赖以运行的底层平台」上。`crossbeam-queue` 是一个 `no_std` 友好的 crate：它能在没有标准库、只有 `core` + `alloc` 的环境（嵌入式、内核、bootloader）里工作。学完本讲你应当能够：

- 说清楚 `#![no_std]` + `extern crate alloc` + `feature = "alloc"` 三者如何配合，让 crate 同时支持「std / alloc-only / 空壳」三种编译形态。
- 解释 `target_has_atomic = "ptr"` 守卫为何是无锁队列的「硬性前提」，以及它在哪些目标上会失败。
- 读懂 `alloc_helper.rs` 里的 `Global` 类型——它是对（尚未稳定的）`alloc::alloc::Global` 的一份最小化 polyfill，提供 `allocate` / `allocate_zeroed` / `deallocate` 三个原语。
- 论证 `SegQueue::Block::new` 为什么能用 `allocate_zeroed` 一次性把整块内存（含 31 个 `MaybeUninit<T>`）零初始化、且这一步是安全的；并理解 0.3.12 版本为何要为「大元素」做这个修复。

本讲与 [u1-l2](u1-l2-crate-root-and-features.md) 是承接关系：u1-l2 在入门层给出了 feature 开关与 cfg 守卫的「全景图」，本讲则从工程实现角度**逐行**解释这些开关背后的机制，并深入到分配原语与堆分配安全性的细节。

## 2. 前置知识

本讲假设你已经读过 u1-l2（crate 入口与 feature），并了解以下概念。为避免初学者卡壳，这里用最短篇幅复习：

- **`core` / `alloc` / `std` 三层标准库**：`core` 是不依赖操作系统的最底层（基本类型、原子、`MaybeUninit` 等）；`alloc` 在 `core` 之上提供堆类型（`Box`、`Vec`、`Arc`、`String`）；`std` 在 `alloc` 之上提供操作系统相关功能（线程、文件、网络、锁）。`#![no_std]` 的含义是「不自动链接 `std`」，但 `core` 永远可用，`alloc` 需要显式 `extern crate alloc;` 引入。
- **Cargo feature 与 cfg 守卫**：`#[cfg(feature = "alloc")]` 控制某段代码是否参与编译；`#[cfg(target_has_atomic = "ptr")]` 则由编译器根据当前**编译目标**（target）自动设置。
- **全局分配器（global allocator）**：Rust 的堆 API（`Box::new` 等）最终调用一个进程级的全局分配器。普通 `std` 程序使用系统默认分配器（malloc 等）；`no_std + alloc` 程序必须用 `#[global_allocator]` 注册一个自己选择的分配器（如 `linked_list_allocator`、`buddy_system_allocator`）。本讲标题中的「自定义全局分配器」指的就是这一点。
- **`MaybeUninit<T>`**：一块「可能尚未初始化」的内存，对底层字节没有任何有效性要求——全零字节对它而言是完全合法的状态。（这一点在 4.4 节的健全性论证中是关键。）

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `crossbeam-queue/` 下，除特别说明外）：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs) | crate 根。`#![no_std]` 声明、条件化的 `extern crate alloc/std`、模块守卫、`pub use` 导出。本讲的「平台开关」几乎全部集中在这里。 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml) | `[features]` 段定义 `default`/`std`/`alloc` 三个 feature 及其依赖透传关系。 |
| [src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs) | 私有的分配原语模块。定义 `Global` 类型与 `allocate` / `allocate_zeroed` / `deallocate` 三个方法。 |
| [../crossbeam-utils/src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/alloc_helper.rs) | `crossbeam-utils` 里**逐字相同**的一份 `alloc_helper`。它是 `pub(crate)` 私有、不对外导出的，所以 `crossbeam-queue` 必须自己「vendor」（内嵌）一份。 |
| [src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) | `SegQueue` 的实现。`Block::new`（L74-L90）是 `allocate_zeroed` 的唯一调用点，也是本讲「安全性论证」的主角。 |

> 小提示：可以先把 `src/lib.rs` 与 `Cargo.toml` 的 `[features]` 段并排打开，对照阅读「声明」与「开关定义」两边的 token，能最快建立直觉。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1** no_std + alloc 编译模式；**4.2** `target_has_atomic` 守卫；**4.3** `alloc_helper` 的 `Global` 分配原语；**4.4** `allocate_zeroed`、零大小布局与 `Block::new` 的健全性。

### 4.1 no_std + alloc 编译模式

#### 4.1.1 概念说明

`crossbeam-queue` 想同时服务两类用户：

1. 普通应用开发者——运行在 Linux/Windows/macOS 上，有完整 `std`。
2. 系统/嵌入式开发者——写内核模块、bootloader、固件，**没有 `std`**，但堆（`alloc`）可用（比如已经接好了一个页帧分配器）。

Rust 的 feature 机制让一个 crate 用同一份源码同时满足两者。核心手段是：

- crate 顶部写 `#![no_std]`，表示「默认不依赖 `std`」。
- 用 `extern crate alloc;` 在「需要堆」时**显式**把 `alloc` crate 拉进来（注意：即使在 2021 edition，`no_std` 下也必须显式写这一行，`alloc` 不像 `core` 那样自动可用）。
- 用 Cargo feature `alloc` / `std` 控制这段 `extern crate` 与各模块的开关。

为什么 `no_std` 下要写 `extern crate alloc;`？因为 `alloc` 不是 `core` 的一部分——它依赖一个全局分配器，因此被单独放在 `extern crate`。在 `std` 环境下 `std` 内部已经 re-export 了 `alloc`，所以平时用 `Box` 不用写 `extern crate`；但 `no_std` 下你必须自己引入。

#### 4.1.2 核心流程

三种典型编译配置（与 u1-l2 对齐，本节给出执行视图）：

```
配置 A（默认）: cargo build
  default = ["std"]  →  std = ["alloc", "crossbeam-utils/std"]
  结果: 链接 std，导出 ArrayQueue + SegQueue

配置 B（纯 alloc）: cargo build --no-default-features --features alloc
  alloc = []  （不拉 std）
  结果: 不链接 std，但 extern crate alloc 生效，导出 ArrayQueue + SegQueue
  前提: 宿主 crate 必须用 #[global_allocator] 注册一个分配器

配置 C（空壳）: cargo build --no-default-features
  两个 feature 都关
  结果: 所有模块都被 cfg 过滤掉，crate 编译为一个「无导出」的空壳
  （Cargo.toml 明确标注：禁用 std 和 alloc "is not supported yet"）
```

`extern crate` 的条件化是关键：它在编译期决定 `alloc` 这个外部 crate 是否被链接进来，进而决定 `Box`、`Vec`、`Arc` 等类型是否可用。

#### 4.1.3 源码精读

crate 根的 `#![no_std]` 是整个故事的起点：

[src/lib.rs:8-L8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L8-L8) —— 声明 `#![no_std]`：从此 crate 默认只依赖 `core`，不再隐式链接 `std`。

紧接着，`alloc` 与 `std` 的引入被两道不同的条件门把守：

[src/lib.rs:21-L24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L21-L24) —— `extern crate alloc;` 在「`alloc` feature 开启 **且** 目标支持指针原子」时引入；`extern crate std;` 仅在 `std` feature 开启时引入。注意两者的守卫不同：`alloc` 多了一道 `target_has_atomic`，原因见 4.2。

模块声明与 `pub use` 共用同一道**组合守卫**：

[src/lib.rs:26-L34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L26-L34) —— `mod alloc_helper; mod array_queue; mod seg_queue;` 以及 `pub use crate::{array_queue::ArrayQueue, seg_queue::SegQueue};` 全部带 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]`。这就是配置 C「空壳」的来源：关掉 `alloc` 后，这三个模块与两个公开类型都不复存在，crate 仍能编译，只是对外什么都没导出。

feature 的定义在 Cargo.toml：

[Cargo.toml:26-L37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L26-L37) —— `default = ["std"]`；`std = ["alloc", "crossbeam-utils/std"]`（开 std 自动开 alloc，并连带把 `crossbeam-utils` 的 std 也打开）；`alloc = []`（纯占位 feature）。注释里那句 `NOTE: Disabling both std and alloc features is not supported yet.` 解释了配置 C 为何「能编译但不可用」。

依赖侧也做了 feature 透传：

[Cargo.toml:39-L40](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L39-L40) —— `crossbeam-utils` 以 `default-features = false` 引入，避免它默认把 `std` 拉进来；`std` feature 再按需打开 `crossbeam-utils/std`。这样在纯 alloc 配置下，整条依赖链都不会偷偷引入 `std`。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认三种配置下 crate 的导出差异。
2. **操作步骤**：
   - 在仓库根目录运行：
     - `cargo build -p crossbeam-queue`（配置 A）
     - `cargo build -p crossbeam-queue --no-default-features --features alloc`（配置 B）
     - `cargo build -p crossbeam-queue --no-default-features`（配置 C）
   - 再分别加 `--features std` / 观察报错。
3. **需要观察的现象**：A、B 均应编译成功；C 也应编译成功（空壳），但若你写一个下游 crate 在 C 配置下 `use crossbeam_queue::SegQueue;`，会得到「`SegQueue` not found」的错误——因为它根本没被导出。
4. **预期结果**：三条 `cargo build` 都返回 0；用 `cargo doc -p crossbeam-queue --no-default-features` 生成的文档里看不到任何公开类型，可佐证「空壳」语义。
5. 若你的环境因依赖编译耗时较长难以快速复现，**待本地验证**时间，可仅用 `cargo check` 替代以加快速度。

#### 4.1.5 小练习与答案

**练习 1**：如果只写 `#![no_std]` 而不写 `extern crate alloc;`，`src/seg_queue.rs` 里 `use alloc::boxed::Box;` 会发生什么？

**答案**：编译失败，提示找不到 `alloc` crate。`no_std` 下 `alloc` 不会自动进入作用域，必须显式 `extern crate alloc;`。

**练习 2**：为什么 `extern crate std;` 不需要 `target_has_atomic` 守卫，而 `extern crate alloc;` 需要？

**答案**：因为 `alloc` 守卫要和「模块守卫」保持一致——无锁队列算法依赖指针原子，没有原子的目标上模块本身就不该编译；`std` 守卫仅表示「要不要额外链接 std」，与原子能力无关（且开了 std 通常也意味着目标是支持原子的桌面/服务器平台）。

### 4.2 target_has_atomic 守卫

#### 4.2.1 概念说明

`target_has_atomic = "ptr"` 是一个由编译器自动设置的 `cfg` 谓词，含义是「当前编译目标支持对**指针宽度**的整数（即 `usize`/`isize`/`*const T`/`*mut T`）做原子操作」。

这对 `crossbeam-queue` 是**硬性前提**，因为两条队列的核心游标 `head` / `tail`（ArrayQueue）与 `Position::index`（SegQueue）都是 `AtomicUsize`，而无锁算法的灵魂就是对这些游标的 `compare_exchange`。若目标不支持指针原子，`AtomicUsize::compare_exchange` 在这些平台上要么不可用，要么会被编译成对**软件模拟的原子库**（`__atomic_*` 库函数）的调用——这在裸机/无操作系统环境往往无法链接，且性能与正确性都难以保证。

> 哪些目标会 `target_has_atomic = "ptr"` 为假？典型是部分 8/16 位单片机与精简指令集的「无原子扩展」子集，例如 AVR（`avr-unknown-gnu-atmega328`）、MSP430、以及不带 `a` 扩展的 RISC-V（`riscv32i-...`）。在这些目标上，整型原子操作不被硬件直接支持。

#### 4.2.2 核心流程

```
编译开始
  ├─ 编译器探测目标能力，设置 target_has_atomic = {"8","16","32","64","ptr","128"} 各项
  ├─ 求值 lib.rs 中每个 cfg(all(feature = "alloc", target_has_atomic = "ptr"))
  │     ├─ alloc 关 且 ptr 原子可用  → 模块不编译（空壳）
  │     ├─ alloc 开 且 ptr 原子可用  → 模块正常编译，导出队列
  │     └─ alloc 开 但 ptr 原子不可用 → 模块不编译（静默降级为空壳）
  └─ 结果：crate 永远能编译，但在「无 ptr 原子」目标上自动隐藏全部功能
```

关键设计哲学是**静默降级**：与其在无原子的目标上报「`AtomicUsize` not available」的硬错误，不如让 crate 编译成一个空壳，由下游决定是否使用。这样 `crossbeam-utils` 等共享依赖可以无副作用地在这些目标上被引用。

#### 4.2.3 源码精读

`target_has_atomic` 与 `feature = "alloc"` 在本 crate 里**永远成对出现**，用 `cfg(all(...))` 组合：

[src/lib.rs:26-L31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L26-L31) —— `mod alloc_helper; mod array_queue; mod seg_queue;` 三行共用 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]`。可以看到，作者刻意把 `target_has_atomic` 和 `alloc` 绑成「一个不可分割的能力包」，因为：没有 `alloc`，`SegQueue` 无法分配块；没有指针原子，两条队列的 CAS 主循环都无法工作。任何一个缺失，整个 crate 就没有意义。

`pub use` 同样受这道组合守卫约束：

[src/lib.rs:33-L34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L33-L34) —— 仅在能力齐全时导出 `ArrayQueue` 与 `SegQueue`。

注意：`feature` 是「用户/Cargo 可控」的开关，而 `target_has_atomic` 是「编译器根据目标自动判定」的开关——两者性质不同，但在本 crate 中被组合成同一道闸门。

#### 4.2.4 代码实践

1. **实践目标**：直观感受「无 ptr 原子的目标」会让 crate 退化为空壳。
2. **操作步骤**（源码阅读型 + 可选编译）：
   - 阅读 `src/lib.rs` 全部 5 处 `cfg(all(...))`，确认它们一致。
   - 可选：若有 AVR 工具链，运行 `cargo check -p crossbeam-queue --target avr-unknown-gnu-atmega328 --no-default-features --features alloc`，再用一段下游代码尝试 `use crossbeam_queue::SegQueue;`。
3. **需要观察的现象**：crate 本体 check 通过；但下游 `use` 语句报「未解析的导入」，因为 `SegQueue` 没被导出。
4. **预期结果**：在 AVR 这类目标上，`crossbeam-queue` 编译为空壳，体现「静默降级」。
5. 若本地无 AVR 工具链，此项**待本地验证**；可改为阅读 [rustc target features 文档](https://doc.rust-lang.org/reference/conditional-compilation.html) 中关于 `target_has_atomic` 的说明来理解。

#### 4.2.5 小练习与答案

**练习 1**：假设某新目标支持 `target_has_atomic = "32"` 但**不**支持 `"ptr"`，而 `usize` 在该目标上恰好是 32 位。`AtomicUsize` 在该目标上可用吗？

**答案**：可用。`AtomicUsize` 选择原子能力的依据是 `usize` 的位宽；若 `usize` 为 32 位且目标支持 32 位原子，则 `AtomicUsize` 可用。但 `crossbeam-queue` 用的是 `target_has_atomic = "ptr"` 这一谓词来判断，它等价于检查 `usize` 对应的原子能力——因此在实践中两者一致。这个练习的关键是理解 `ptr` 谓词与 `usize` 位宽的对应关系。

**练习 2**：为什么作者选择「静默降级为空壳」而不是直接让 `AtomicUsize` 触发编译错误？

**答案**：为了让 `crossbeam-queue`（以及引用它的 `crossbeam-utils` 等）能在 Cargo 依赖图里被「无害地」包含进面向这些目标的工程——上游 workspace 可以统一声明依赖而不必为每个目标做条件依赖剔除；功能缺失由「类型不存在」这一更温和的方式表达。

### 4.3 alloc_helper Global 分配原语

#### 4.3.1 概念说明

`SegQueue` 在运行期需要不断分配 `Block`（每个块 31 个槽），`ArrayQueue` 在构造时需要一次性分配 `cap` 个槽的缓冲区。这些分配都需要「堆」。Rust 标准库提供了两层堆 API：

- **稳定 API（自由函数）**：`alloc::alloc::alloc(layout)`、`alloc::alloc::alloc_zeroed(layout)`、`alloc::alloc::dealloc(ptr, layout)`。它们直接调用进程的全局分配器。
- **不稳定 API（`Allocator` trait）**：`alloc::alloc::Global` 是一个实现了 `Allocator` 的类型，提供 `allocate` / `allocate_zeroed` 等方法，返回 `Result<NonNull<[u8]>, AllocError>`，并能与 `Box`/`Vec` 的 `new_in` 系列 API 联动。

`crossbeam-queue` 想要「`Allocator` 风格的方法签名」（更安全、更易用），但又必须保持 **MSRV = 1.60**（见 [Cargo.toml:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L10-L10)），而稳定的 `Allocator` API 远在 1.60 之后才可用。**折中方案**就是 `alloc_helper.rs`：手写一个 `Global` 类型，包装稳定的自由函数，对外暴露 `allocate` / `allocate_zeroed` / `deallocate` 三个方法。

> 为什么 `crossbeam-queue` 不复用 `crossbeam-utils` 里那份一模一样的 `alloc_helper`？因为那里的 `Global` 是 `pub(crate)`——对 `crossbeam-utils` 之外的 crate 不可见（`crossbeam-utils/src/lib.rs` 只导出了 `atomic`、`CachePadded`、`Backoff`、`sync`、`thread`，没有 `alloc_helper`）。因此 `crossbeam-queue` 只能 vendor（内嵌）一份逐字相同的副本。两份文件你可以 `diff` 一下，内容完全一致。

#### 4.3.2 核心流程

`Global` 的分配流程：

```
Global.allocate_zeroed(layout) / allocate(layout)
  └─ alloc_impl(layout, zeroed)
       ├─ 若 layout.size() == 0:
       │     返回 dangling 指针（地址 = layout.align()，无 provenance，不真正分配）
       │     —— 这符合 Allocator 语义：零大小布局不需要真内存
       └─ 若 layout.size() > 0:
             ├─ zeroed? → alloc::alloc::alloc_zeroed(layout)   // 全零内存
             └─ 否则   → alloc::alloc::alloc(layout)            // 未初始化内存
             再用 NonNull::new 把 *mut u8 包成 Option<NonNull<u8>>
             None 表示分配失败（OOM），交给调用方决定
```

调用方（`Block::new`）在拿到 `None` 时的策略是调用 `handle_alloc_error` 直接终止进程——见 4.4。这里有一个重要约定：**零大小布局返回一个「悬垂但已对齐」的指针**，并且 `deallocate` 对零大小布局是 no-op。这与稳定的 `Allocator` trait 语义一致。

#### 4.3.3 源码精读

`Global` 是一个零大小类型，所有方法都以 `&self` 接收（仅起「命名空间 + 未来可替换为真实 Allocator」的作用）：

[src/alloc_helper.rs:7-L9](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L7-L9) —— `pub(crate) struct Global;`，注释说明它「基于尚不稳定的 `alloc::alloc::Global`」，且**返回 `NonNull<u8>` 而非 `NonNull<[u8]>`**（后者是不稳定 API 的签名）。

核心分发函数 `alloc_impl`：

[src/alloc_helper.rs:12-L34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L12-L34) —— `match layout.size()` 是关键：
- 分支 `0 => Some(dangling(layout))`：零大小布局返回悬垂指针，不调用全局分配器。
- 分支 `_size => unsafe { ... }`：非零大小才真正调用 `alloc::alloc::alloc_zeroed(layout)` 或 `alloc::alloc::alloc(layout)`，并用 `NonNull::new(raw_ptr)` 把可能的空指针转成 `Option`。

`#[cfg_attr(miri, track_caller)]` 这类属性出现于多个方法，是为了在 Miri 报错时给出更友好的调用栈，与方法逻辑无关。

`dangling` 与 `without_provenance_mut`：

[src/alloc_helper.rs:14-L19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L14-L19) —— `dangling` 用 `without_provenance_mut::<u8>(layout.align())` 构造一个「地址等于对齐值、但无 provenance」的指针。这就是 Rust 里表示「零大小分配的有效指针」的惯用法：它指向一个合法对齐的地址，但你绝不能解引用它去读写真实内存。

[src/alloc_helper.rs:68-L85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L68-L85) —— `without_provenance_mut` 在 Miri 与 CHERI 下用 `transmute`，否则用 `addr as *mut T`。注释解释了 CHERI（带 provenance 的指针架构）需要特殊处理的原因。

三个对外方法：

[src/alloc_helper.rs:38-L46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L38-L46) —— `allocate(layout)` 与 `allocate_zeroed(layout)` 都是 `alloc_impl` 的薄封装，区别仅是 `zeroed` 布尔参数。

[src/alloc_helper.rs:50-L65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L50-L65) —— `deallocate` 是 `unsafe fn`，内部先判断 `layout.size() != 0` 再调用 `alloc::alloc::dealloc`。零大小布局直接返回（no-op），与 `allocate` 的零大小分支对称。SAFETY 注释列出了三条契约（非零大小、layout 与分配时一致、其余由调用方保证）。

#### 4.3.4 代码实践

1. **实践目标**：理解 `Global` 与全局分配器的关系，以及零大小布局的行为。
2. **操作步骤**（源码阅读 + 小实验）：
   - 通读 `src/alloc_helper.rs` 全 86 行，确认它没有定义 `#[global_allocator]`——它**使用**全局分配器，而非**定义**一个。
   - 在一个临时的 `std` 程序里写：`let l0 = Layout::from_size_align(0, 1).unwrap();`，观察标准库 `alloc::alloc::alloc(l0)` 返回的指针地址，与 `alloc_helper::dangling`（地址 = align）对照。
3. **需要观察的现象**：零大小「分配」并不会真正占用堆，返回的指针地址等于对齐值（对齐为 1 时地址就是 1）。
4. **预期结果**：你能口头解释「为什么 `deallocate` 对零大小布局必须是 no-op」——因为根本没有真内存可释放。
5. 涉及私有 API（`Global` 是 `pub(crate)`）无法在 crate 外直接调用，相关现象**待本地验证**或改为阅读 `alloc::alloc` 模块文档理解。

#### 4.3.5 小练习与答案

**练习 1**：`Global::allocate` 返回 `Option<NonNull<u8>>` 而非 `Result<NonNull<[u8]>, AllocError>`，为什么？

**答案**：因为稳定的自由函数 `alloc::alloc::alloc` 在失败时返回空指针，用 `NonNull::new` 把它转成 `Option` 是最自然、最稳定的方式；`NonNull<[u8]>` 与 `AllocError` 属于尚不稳定的 `Allocator` API，不能在 MSRV 1.60 上使用。

**练习 2**：为什么 `Global` 用 `&self` 而不是关联函数（`fn allocate(layout)`）？

**答案**：签名上模拟未来的 `Allocator` trait（其方法以 `&self` 接收 `&Self`），便于将来稳定后直接迁移；`#[allow(clippy::unused_self)]` 也印证了「`self` 当前未被使用，仅为签名对齐」。

### 4.4 allocate_zeroed 与零大小布局：Block::new 的健全性

#### 4.4.1 概念说明

4.3 讲了「分配原语长什么样」，本节讲「它怎么被用、为什么这样用是安全的」。唯一（也是最重要的）调用点是 `SegQueue::Block::new`。

背景：在 0.3.12 之前，`Block` 的创建走的是「先在栈上构造 `Block { next, slots }`，再 `Box::new` 移到堆上」的常规路径。问题在于 `slots: [Slot<T>; 31]`——当 `T` 很大时（比如 `T = [u8; 32768]`），单个 `Block` 在栈上的体积是 \(31 \times 32768 \approx 1\text{ MiB}\)，足以**撑爆线程栈**。CHANGELOG 记录了这个修复：

[CHANGELOG.md:1-L3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L1-L3) —— 0.3.12 修复「向 `SegQueue` push 大元素时的栈溢出」。

修复思路：**绕过栈，直接在堆上分配一块全零内存**，然后用 `Box::from_raw` 把它重新解释成一个 `Block`。这正是 `allocate_zeroed` 的用武之地——它一次系统调用就把整块（含 31 个槽）全置零，既避免了栈临时量，又省去了逐字段初始化。

为什么不直接用 `Box::new_zeroed`？源码注释给出答案：

[src/seg_queue.rs:76-L76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L76-L76) —— `// unsafe { Box::new_zeroed().assume_init() } requires Rust 1.92`。也就是说，stdlib 提供的更简洁写法要到 1.92 才稳定，而 crate 的 MSRV 是 1.60，所以必须用 `Global::allocate_zeroed` 手动实现等价语义。

#### 4.4.2 核心流程

```
Block::<T>::new()
  ├─ Global.allocate_zeroed(Self::LAYOUT)
  │     └─ alloc_impl(LAYOUT, zeroed=true)
  │           └─ layout.size() 必然 > 0（LAYOUT 有断言保证）
  │                 → alloc::alloc::alloc_zeroed(LAYOUT)  // 全零堆内存
  ├─ Some(ptr):
  │     unsafe { Box::from_raw(ptr.as_ptr().cast()) }   // 把零内存「解释」为 Box<Block<T>>
  └─ None:
        handle_alloc_error(Self::LAYOUT)                // 分配失败 → 终止进程，永不返回
```

健全性的关键问题：**全零字节构成的 `Block<T>` 是不是合法的？** 这要求 `Block` 的每个字段都「容忍零初始化」：

- `next: AtomicPtr<Block<T>>` —— 零 = 空指针 `null()`，合法（表示「没有下一块」）。
- `slots[i].state: AtomicUsize` —— 零 = `WRITE|READ|DESTROY` 三个比特都没置位，合法（表示「这个槽既没写过也没读过」）。
- `slots[i].value: UnsafeCell<MaybeUninit<T>>` —— `MaybeUninit` **对底层字节没有任何有效性要求**，全零字节是完全合法的「未初始化」状态，合法。

三个字段全部容忍零初始化，因此整块零内存就是一个合法的 `Block`。

`LAYOUT` 常量还做了编译期断言：

[src/seg_queue.rs:65-L72](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L65-L72) —— `const LAYOUT: Layout` 用 `assert!(layout.size() != 0, ...)` 保证「`Block` 永远非零大小」——因为它至少含一个 `AtomicPtr` 字段。这个断言的意义是：让 4.3 节的「零大小布局分支」永远不会在 `Block::new` 里触发，从而 `allocate_zeroed` 必走真正的 `alloc_zeroed` 路径。

#### 4.4.3 源码精读

`seg_queue.rs` 顶部的导入已经把 `alloc` 的两个符号拉进来：

[src/seg_queue.rs:1-L1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L1-L1) —— `use alloc::{alloc::handle_alloc_error, boxed::Box};`。`Box` 来自 `alloc`，`handle_alloc_error` 来自 `alloc::alloc`。这两者只有在 `extern crate alloc;` 生效时才可解析——回扣 4.1 的 `cfg` 守卫。

`Block::new` 的完整实现：

[src/seg_queue.rs:74-L90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L74-L90) ——
- L77 `Global.allocate_zeroed(Self::LAYOUT)`：在堆上分配全零内存。
- L78-L86 的 `Some(ptr)` 分支：`unsafe { Box::from_raw(ptr.as_ptr().cast()) }`，把裸指针转成 `Box<Block<T>>`。L79-L84 的 SAFETY 注释把「为什么全零是合法的」按字段拆成 [1]–[4] 四条理由（与 4.4.2 的论证一一对应）。
- L88 `None => handle_alloc_error(Self::LAYOUT)`：分配失败时不返回错误，而是调用 `handle_alloc_error`——这个函数**永不返回**（默认实现是 abort 进程），与 Rust 全局分配器「OOM 即终止」的默认策略一致。

关于 `handle_alloc_error` 的设计意图：`crossbeam-queue` 的公共 API（`push` 等）返回值里**没有**「分配失败」这一种情况——`SegQueue::push` 返回 `()`。为了让「无 OOM 错误路径」这一 API 契约成立，唯一的办法就是在分配失败时直接终止进程，而不是 unwind 或返回 `Result`。

零大小布局的对称处理：`Block` 永远非零大小，所以 `Block::new` 不会走到 `dangling` 分支；但 `Global` 的零大小分支（4.3）与 `deallocate` 的 no-op（4.3）依然重要——它们保证「未来若有零大小类型走这套 API」时不会误调 `dealloc(null, layout)`。

#### 4.4.4 代码实践

1. **实践目标**：复现 0.3.12 修复所针对的场景，理解「堆上零初始化」为何能避免栈溢出。
2. **操作步骤**：
   - 阅读 [tests/seg_queue.rs:237-L250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L237-L250) 的 `stack_overflow` 测试：它定义 `struct BigStruct { _data: [u8; 32768] }`，向 `SegQueue` push 一个 `BigStruct`，再迭代消费。
   - 思考：若 `Block` 仍在栈上构造，单个 `Block<BigStruct>` 体积约 \(31 \times 32768 = 1{,}015{,}808\) 字节（约 0.97 MiB），而测试线程默认栈仅 2 MiB，多次 push 极易溢出。改用 `allocate_zeroed` 后，`Block` 直接在堆上诞生，栈上不出现大临时量。
   - 运行：`cargo test -p crossbeam-queue --test seg_queue stack_overflow`。
3. **需要观察的现象**：测试通过（无栈溢出、无内存错误）；用 `cargo test -p crossbeam-queue --test seg_queue stack_overflow -- --nocapture` 可看到正常完成。
4. **预期结果**：测试通过；若你能拿到 0.3.11 的源码对比，可看到旧的 `Block::new` 走 `Box::new(Block { ... })` 路径，会因栈上构造大数组而触发溢出。
5. 如需用 Miri 验证「零初始化内存的访问安全性」，可运行 `cargo +nightly miri test -p crossbeam-queue --test seg_queue stack_overflow`（注意 Miri 下规模会自动缩小）；Miri 环境是否可用**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果 `T` 是一个「零字节不合法」的类型（比如某个自指针结构体要求某字段非零），`allocate_zeroed` 创建的 `Block<T>` 还健全吗？

**答案**：依然健全。因为这些「零字节不合法」的字段都位于 `Slot::value: UnsafeCell<MaybeUninit<T>>` 之内，而 `MaybeUninit<T>` **本身**对底层字节无任何有效性约束——零字节只是「未初始化」的一种特例。只有当生产者后续用 `slot.value.get().write(...)`（见 [src/seg_queue.rs:280-L280](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L280-L280)）写入一个合法 `T` 之后，该槽的值才被「承诺」为合法；消费者在 `wait_write`（确保 `WRITE` 位置位）之后才读取，因此绝不会读到「不合法的零」。

**练习 2**：`handle_alloc_error` 与「返回 `Result`」相比，对 `SegQueue` 的公共 API 有什么影响？

**答案**：它让 `SegQueue::push` 能保持 `()` 返回值——调用方无需处理 OOM。代价是分配失败会终止整个进程（不可恢复）。这是一个明确的 API 设计取舍：把「无界队列」的失败模式简化为「要么成功，要么进程结束」。

## 5. 综合实践

把本讲的四条线索串起来：在一个 **no_std + alloc** 的最小目标上运行 `SegQueue::new().push(1)`。这个任务会逼你同时用到 4.1（feature 配置）、4.2（目标能力）、4.3（全局分配器）、4.4（堆分配安全）。

**任务步骤（以宿主 x86_64 Linux、纯 alloc 配置为例）**：

1. 新建一个 binary crate `host`，在它的 `Cargo.toml` 里：
   ```toml
   [dependencies]
   crossbeam-queue = { path = "<repo>/crossbeam-queue", default-features = false, features = ["alloc"] }
   ```
2. 在 `host/src/main.rs` 写一个**最小可运行的 no_std 程序**（示例代码，非项目原有代码）：
   ```rust
   #![no_std]
   #![no_main]

   extern crate alloc;

   use core::panic::PanicInfo;
   use crossbeam_queue::SegQueue;

   // 必须注册一个全局分配器；这里用 linked_list_allocator 仅作示意。
   // 实际需自行管理一段堆区，具体取决于你的裸机环境。
   // #[global_allocator]
   // static ALLOC: SomeAllocator = SomeAllocator::new();

   #[panic_handler]
   fn panic(_: &PanicInfo) -> ! { loop {} }

   #[no_mangle]
   pub extern "C" fn _start() -> ! {
       let q = SegQueue::new();
       q.push(1);
       let _ = q.pop();
       loop {}
   }
   ```
   > 注意：真正「可运行」的 no_std 二进制需要一个入口（`_start`）、一个 panic handler，以及一个已接好堆区的 `#[global_allocator]`。在宿主 Linux 上更现实的做法是写一个 **no_std 但仍用 std 入口** 的库测试——即只验证 `--no-default-features --features alloc` 能编译并通过 `SegQueue::new().push(1)`。下面给出更易落地的替代方案。
3. **更易落地的替代验证**（宿主上即可）：在 `host` 的 `[dev-dependencies]` 引入同样的 alloc-only 配置，写一个 `#[test]`：
   ```rust
   // host/tests/alloc_only.rs（示例代码）
   #[test]
   fn segqueue_alloc_only() {
       let q = crossbeam_queue::SegQueue::new();
       q.push(1);
       assert_eq!(q.pop(), Some(1));
   }
   ```
   运行 `cargo test --no-default-features --features alloc`。
4. **观察并回答**：
   - 若忘记注册 `#[global_allocator]`（在真正 no_std 二进制里），链接期会报「`alloc` 找不到分配器」类错误——回扣 4.3「`Global` 使用而非定义全局分配器」。
   - 思考并写下：`Block::new` 在 `push(1)` 触发首次块分配时，`allocate_zeroed` 把整块内存置零；这段内存的 `next` 为 null、所有 `state` 为 0、所有 `MaybeUninit<i32>` 为全零。请逐字段论证这三者都是合法初值（参考 4.4.2）。
   - 解释为什么即便 `i32` 本身「零字节是合法值」，本设计的健全性也**不依赖**这一点（参考练习 4.4.5-1）。
5. **预期结果**：测试通过；你能用一段话讲清「从 `push(1)` 到 `Block::new` 到 `allocate_zeroed` 到全局分配器」的完整调用链，并解释为什么全零内存就是一个合法的 `Block`。

## 6. 本讲小结

- `crossbeam-queue` 用 `#![no_std]` + 条件 `extern crate alloc/std` + Cargo feature（`default`/`std`/`alloc`）三种手段，从同一份源码编译出「std / alloc-only / 空壳」三种形态。
- `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 是贯穿全部模块与导出的统一守卫：`alloc` 保证堆可用，`target_has_atomic = "ptr"` 保证无锁算法所依赖的 `AtomicUsize` 可用；任一缺失，crate 静默降级为空壳。
- `alloc_helper.rs` 的 `Global` 是对（MSRV 不允许使用的）`alloc::alloc::Global` 的最小 polyfill，包装稳定的 `alloc`/`alloc_zeroed`/`dealloc` 自由函数，暴露 `allocate` / `allocate_zeroed` / `deallocate`，并正确处理零大小布局（返回悬垂指针、`dealloc` 为 no-op）。
- `Global` 在 `crossbeam-utils` 里是 `pub(crate)` 私有的，所以 `crossbeam-queue` 内嵌了一份逐字相同的副本——这是「跨 crate 复用私有工具模块」的典型代价。
- `SegQueue::Block::new` 用 `Global::allocate_zeroed` 直接在堆上分配全零内存，绕过栈临时量，修复了 0.3.12 的大元素栈溢出问题；健全性依据是 `next`(null)、`state`(0)、`MaybeUninit`（全零合法）三类字段都容忍零初始化。
- 分配失败时调用 `handle_alloc_error` 永不返回，使 `SegQueue::push` 能维持 `()` 返回值——把 OOM 转化为进程终止，是明确的 API 取舍。

## 7. 下一步学习建议

本讲是 `crossbeam-queue` 学习手册的最后一篇，至此你已经把两条队列的算法、原子序、性能原语、unsafe 安全性、测试方法与底层平台支持全部走完。建议的后续方向：

- **回到全图复盘**：重读 [u1-l1](u1-l1-project-overview.md) 的选型建议与 [u3-l2](u3-l2-segqueue-push-pop.md) 的 push/pop 主链路，验证你现在能从「最底层的分配原语」一路解释到「最顶层的公共 API」。
- **横向对比 `crossbeam-utils`**：阅读 `crossbeam-utils/src/alloc_helper.rs` 与本讲的 `crossbeam-queue/src/alloc_helper.rs`，体会「同一份私有工具如何在多个 crate 里 vendor」的工程模式；并扩展阅读 `crossbeam-utils` 的 `CachePadded`/`Backoff` 源码（对应 [u4-l2](u4-l2-cachepadded-backoff.md)）。
- **深入 `Allocator` API**：关注 Rust nightly 上 `alloc::alloc::Global` 与 `Box::new_zeroed` 的稳定化进展，思考一旦它们稳定、MSRV 提升，`alloc_helper` 是否可以被整段删除。
- **动手扩展**：尝试为 `crossbeam-queue` 写一个真正可运行的 `no_std` + `alloc` 二进制（注册 `linked_list_allocator` 作为 `#[global_allocator]`，在 QEMU 上跑通 `SegQueue::new().push(1)`），把本讲的「综合实践」做成可提交的 demo。
