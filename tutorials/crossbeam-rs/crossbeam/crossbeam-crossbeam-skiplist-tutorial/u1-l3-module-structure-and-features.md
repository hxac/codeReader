# 目录结构与 feature 门控：base→map→set 分层

## 1. 本讲目标

前两讲我们解决了「它是什么」（[u1-l1](u1-l1-project-overview.md)）和「它怎么用」（[u1-l2](u1-l2-quick-start-usage.md)）。本讲把镜头拉远到**工程结构**层面：当你打开 `crossbeam-skiplist` 这个 crate 的源码时，文件是怎么组织的？为什么要把代码切成 `base` / `map` / `set` 三层？那些 `cfg(feature = ...)` 又在门控什么？

读完本讲你应该能够：

1. 画出 `src/` 下六个模块（`base`、`map`、`set`、`comparator`、`equivalent`、`alloc_helper`）的**依赖方向图**，并说清楚谁依赖谁。
2. 看懂 `Cargo.toml` 里 `default` / `std` / `alloc` **三档 feature** 的级联关系，以及关掉默认 feature 后哪些类型会「消失」。
3. 理解 `#![no_std]` 顶层声明与 `target_has_atomic = "ptr"` 门控的意义——为什么这个库能跑在没有标准库的嵌入式环境里。
4. 用一句话解释**三层包装**：`base::SkipList`（底层无锁原语）→ `SkipMap`（高层封装）→ `SkipSet`（`SkipMap<T, ()>` 的特化）。

本讲几乎不涉及并发算法（那是第二、三单元的事），重点是把「项目骨架」看清楚。骨架清楚了，后面读 `base.rs` 里上千行无锁代码时，你才知道每一块拼在哪里。

## 2. 前置知识

本讲是结构梳理型的，门槛不高，但需要几个 Rust 工程基础概念。

**什么是 crate 与模块（module）？**

- 一个 Rust 库就是一个 crate，根入口是 `src/lib.rs`。
- 用 `mod foo;` 声明一个子模块，对应 `src/foo.rs` 或 `src/foo/mod.rs` 文件。
- 用 `pub mod foo;` 让它对外可见；用 `pub use foo::Bar;` 把 `Bar` 重新导出到上层，方便用户写 `crossbeam_skiplist::Bar` 而不是 `crossbeam_skiplist::foo::Bar`。

**什么是 Cargo feature？**

- 在 `Cargo.toml` 的 `[features]` 段里定义「可选的编译开关」，比如 `std = ["alloc"]`。
- feature 是**累加（additive）**的：开得越多，能用的 API 越多；关掉某些 feature 只会让对应的代码不参与编译，不会破坏其它功能。
- 一个 feature 可以带上「依赖项的 feature」，例如 `std = ["crossbeam-epoch/std"]` 表示「当我开 `std` 时，也帮依赖 `crossbeam-epoch` 打开它的 `std`」。

**`no_std` 是什么？**

- Rust 标准库分两层：最底层的 `core`（不依赖操作系统，到处都能用）、中间的 `alloc`（提供堆分配 `Box`/`Vec`）、最上层的 `std`（操作系统相关：线程、文件、网络……）。
- 在 `lib.rs` 顶部写 `#![no_std]`，表示这个 crate **默认不链接 `std`**，只用 `core`。需要堆分配时再按需引入 `alloc`。
- 这样做的好处：这个库不仅能跑在普通服务器上，还能跑在单片机、内核等没有操作系统的 `no_std` 环境里。

**术语速查**

- **cfg（配置属性）**：`#[cfg(condition)]` 表示「仅当条件成立时才编译这段代码」，是条件编译的核心机制。
- **epoch（基于时代的内存回收）**：本 crate 依赖的并发内存回收机制，来自 `crossbeam-epoch` crate；它需要目标平台支持原子指针操作。
- **`target_has_atomic = "ptr"`**：一个内置 cfg，当编译目标支持「指针大小」的原子操作时为真。`crossbeam-epoch` 离不开它。

## 3. 本讲源码地图

本讲围绕两个文件展开，关注的是它们的「声明」与「配置」，而非算法实现：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/lib.rs` | crate 根模块 | 第 231 行 `#![no_std]`、第 244–269 行的 `extern crate` / `mod` / `pub use` 声明与 `cfg` 门控 |
| `Cargo.toml` | 包配置 | 第 27–38 行的 `[features]` 段（`default` / `std` / `alloc` 三档） |

为了说清楚「谁依赖谁」「谁包了谁」，我们还会**点到为止**地引用下面四个文件的「头部声明」（不读实现）：

| 文件 | 关注点 |
| --- | --- |
| `src/base.rs` | 顶层 `use` 揭示 `base` 依赖 `alloc_helper` 与 `comparator`；引入 `crossbeam_epoch::Collector` |
| `src/map.rs` | `SkipMap` 结构体定义：`inner: base::SkipList<K, V, C>` |
| `src/set.rs` | `SkipSet` 结构体定义：`inner: map::SkipMap<T, (), C>` |
| `src/alloc_helper.rs` | 自实现的 `Global` 分配器（替代尚未稳定的 `alloc::alloc::Global`） |

`comparator.rs` 和 `equivalent.rs` 是「无门控、随时可见」的工具模块，本讲只看它们的依赖关系，用法留到 [u2-l7](u2-l7-comparator-and-equivalent.md)。

## 4. 核心概念与源码讲解

### 4.1 顶层 `#![no_std]` 与三层标准库的 `extern crate` 门控

#### 4.1.1 概念说明

`crossbeam-skiplist` 的第一个非同寻常的设计是：它在 [src/lib.rs:231](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L231) 顶部声明了 `#![no_std]`。

这意味着整个 crate **默认只依赖最底层的 `core`**，而不是 `std`。但这并不等于「完全不能用堆、不能用标准库」——它通过 Cargo feature 按**三档**逐层引入：

- **第 0 档（仅 `core`）**：连堆分配都没有。本 crate 目前不支持这一档（见 4.2）。
- **第 1 档（`core` + `alloc`）**：能堆分配，能用 `Box`/`Vec`，但仍没有线程、没有操作系统服务。这一档下 `base::SkipList` 可用。
- **第 2 档（`core` + `alloc` + `std`）**：完整标准库。这一档下 `SkipMap` / `SkipSet` 才可用。

「按需引入」靠的就是 `extern crate` 加 `cfg` 门控。

#### 4.1.2 核心流程

`#![no_std]` 声明之后，引入标准库的流程是：

```text
#![no_std]                              # 默认只有 core
   │
   ├─ #[cfg(all(feature="alloc", target_has_atomic="ptr"))]
   │  extern crate alloc;               # 开 alloc feature 且目标支持原子指针 → 引入 alloc
   │
   └─ #[cfg(feature = "std")]
      extern crate std;                 # 开 std feature → 引入 std
```

关键点：

1. `extern crate alloc;` 不是无条件编译的，它被 `feature = "alloc"` 门控。
2. 它还附加了 `target_has_atomic = "ptr"` 条件——因为底层的跳表节点靠原子指针维护，目标平台若不支持原子指针，整个核心就编译不出来（详见 4.3）。
3. `extern crate std;` 只被 `feature = "std"` 门控（隐含 `alloc`，因为 `std` 依赖 `alloc`，见 4.2 的级联）。

为了让「`no_std` 纪律」不被无意破坏，`lib.rs` 还打开了三条 clippy lint，强制代码**只能从最底层开始 import**：

```text
clippy::alloc_instead_of_core    # 要用堆类型时，优先从 core 引入而非 alloc
clippy::std_instead_of_alloc     # 要用 alloc 类型时，优先从 alloc 引入而非 std
clippy::std_instead_of_core      # 要用 core 类型时，禁止从 std 引入
```

这等于把「分层」变成了**编译期硬约束**：在 `base.rs` 这种 `alloc` 档代码里写 `use std::...` 会被 clippy 直接拒绝。

#### 4.1.3 源码精读

顶层 `#![no_std]` 声明位于 [src/lib.rs:231](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L231)：

> `#![no_std]`

紧随其后的 lint 块（[src/lib.rs:236-242](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L236-L242)）就是上面提到的三条 clippy 规则——它们是「分层」的守门员。

真正引入标准库的两行在 [src/lib.rs:244-247](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L244-L247)，作用是「按 feature 条件性地把 `alloc` 与 `std` 拉进编译单元」：

- `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))] extern crate alloc;`：只有开 `alloc` 且目标支持原子指针时才链接 `alloc`。
- `#[cfg(feature = "std")] extern crate std;`：只有开 `std` 时才链接 `std`。

注意一个细节：在 `#![no_std]` crate 里仍可以写 `extern crate std;`——`no_std` 只是「默认不链接 std」，显式 `extern crate` 仍能把它请回来，这正是「条件性升级到 std」的标准手法。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是直观感受「分层 import」。

1. **实践目标**：验证 `base.rs` 只从 `core` / `alloc` 引入，而 `map.rs` / `set.rs` 可以从 `std` 引入。
2. **操作步骤**：
   - 打开 [src/base.rs:3-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L3-L16)，观察它的 `use core::{...}` 与 `use alloc::alloc::handle_alloc_error;`，确认它**只碰 `core` 和 `alloc`**。
   - 打开 [src/map.rs:3-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L3-L15)，注意它同样写的是 `use core::{...}`。
3. **需要观察的现象**：`base.rs` 顶部没有任何 `use std::...`，因为它要在 `alloc` 档下编译；如果它偷用了 `std`，4.1.2 里的 clippy lint 会报错。
4. **预期结果**：`base` 模块自给自足于 `core` + `alloc`，不依赖操作系统——这就是它能进 `no_std` 环境的根因。
5. 本步骤为静态阅读，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果把 [src/lib.rs:231](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L231) 的 `#![no_std]` 删掉，会发生什么？

**参考答案**：crate 会默认链接 `std`，于是失去 `no_std` / `alloc` 档的支持能力——在嵌入式目标上无法编译。此外，那三条 `clippy::*_instead_of_core` lint 的意义也会被削弱（因为 `std` 默认就在）。所以这一行是整个「三档」设计的基石，不能删。

**练习 2**：为什么 `extern crate alloc;` 上要同时挂 `feature = "alloc"` **和** `target_has_atomic = "ptr"` 两个条件？

**参考答案**：`feature = "alloc"` 是用户的「我想要堆分配」意愿；`target_has_atomic = "ptr"` 是平台的「我支持原子指针」客观能力。底层的跳表节点靠原子指针维护并发，两者缺一不可，所以用 `all(...)` 取交集。

---

### 4.2 Cargo.toml 的 feature 定义与依赖传递

#### 4.2.1 概念说明

「三档标准库」对应到 Cargo 里就是三个 feature：`default`、`std`、`alloc`。它们不是平级的，而是**级联（cascade）**关系：开高级别会自动开低级别。

这是 Rust 生态里非常常见的「洋葱式」feature 设计：最外层（`std`）包含中间层（`alloc`），中间层包含核心（`core`，但 `core` 不需要 feature 因为它总是可用）。

#### 4.2.2 核心流程

feature 的级联关系如下：

```text
default = ["std"]                 # 默认开 std（开箱即用，最常见场景）
        │
        ▼
std    = ["alloc",                # 开 std 自动开 alloc
         "crossbeam-epoch/std",   # 同时帮依赖 crossbeam-epoch 打开它的 std
         "crossbeam-utils/std"]   # 同时帮依赖 crossbeam-utils 打开它的 std
        │
        ▼
alloc  = ["crossbeam-epoch/alloc"] # 开 alloc 自动帮 crossbeam-epoch 打开 alloc
```

几个要点：

1. **`default = ["std"]`**：用户不指定 feature 时，默认拿到完整的 `std` 档。绝大多数应用都在这一档。
2. **`std` 隐含 `alloc`**：写了 `std = ["alloc", ...]`，所以开 `std` 必然也开 `alloc`，不会出现「有 `std` 没 `alloc`」的奇怪状态。
3. **跨 crate 传递**：`std` 里带 `crossbeam-epoch/std`，意思是「我开 `std` 时，也请 `crossbeam-epoch` 开它的 `std`」。这让依赖链上的 feature 自动对齐，不用用户手动管。
4. **底部注释**：`Cargo.toml` 明确写着「同时关掉 `std` 和 `alloc` 尚不支持」——即「第 0 档（纯 `core`）」目前不可用。

#### 4.2.3 源码精读

feature 的全部定义集中在 [Cargo.toml:27-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L27-L38)。逐行解读：

- `default = ["std"]`（第 28 行）：默认开启 `std` 档。
- `std = ["alloc", "crossbeam-epoch/std", "crossbeam-utils/std"]`（第 32 行）：开 `std` 级联开 `alloc`，并传递 feature 给两个依赖。
- `alloc = ["crossbeam-epoch/alloc"]`（第 38 行）：开 `alloc` 时传递给 `crossbeam-epoch`。
- 第 37 行的注释 `NOTE: Disabling both std and alloc features is not supported yet.`：明确「第 0 档不支持」。

依赖声明在 [Cargo.toml:40-42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L40-L42)，注意 `crossbeam-epoch` 与 `crossbeam-utils` 都用了 `default-features = false`——也就是说**默认不带入它们的 feature**，全靠上面的级联规则按需打开。这是 `no_std` 友好的关键：避免把整个 `std` 拖进来。

另外，[Cargo.toml:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L10) 写明 `rust-version = "1.74"`，即 **MSRV（最低支持 Rust 版本）为 1.74**，后续编译实践需要工具链不低于此版本。

#### 4.2.4 代码实践

这是一个**配置修改 + 编译观察型实践**。

1. **实践目标**：直观验证「关掉默认 feature、只开 `alloc`」时，crate 仍能编译。
2. **操作步骤**：在 `crossbeam-skiplist` 目录下执行（不影响源码，只改编译开关）：

   ```bash
   cargo build --no-default-features --features alloc
   ```

3. **需要观察的现象**：编译应当成功，因为 `base` 模块只需要 `alloc` + 原子指针。
4. **预期结果**：`cargo build` 返回码 0，无错误（可能有少量 warning）。此时 `base::SkipList` 可用，但 `SkipMap` / `SkipSet` 不可用（原因见 4.3 / 4.4）。
5. 若想进一步确认「同时关掉两者」确实不支持，可执行 `cargo build --no-default-features`，预期会因缺少 `alloc` 而报错或产出空 crate。**待本地验证**具体报错文本。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `crossbeam-epoch` 和 `crossbeam-utils` 在依赖声明里都要加 `default-features = false`？

**参考答案**：因为它们默认会开启 `std`。如果这里不关掉默认 feature，那么即使本 crate 选了 `alloc` 档，依赖也会把 `std` 拖进来，破坏 `no_std` 支持。所以必须先关掉默认值，再用 feature 级联规则「按需」打开。

**练习 2**：用户写 `features = ["std"]` 时，`crossbeam-epoch` 最终会带上哪些 feature？

**参考答案**：会带上 `std` 和 `alloc`。因为本 crate 的 `std` 级联开 `alloc`，而 `alloc` 又会传递 `crossbeam-epoch/alloc`，`std` 会传递 `crossbeam-epoch/std`——两条规则叠加，`crossbeam-epoch` 同时拿到 `alloc` 与 `std`。

---

### 4.3 src 模块声明、可见性与 `target_has_atomic` 门控

#### 4.3.1 概念说明

有了 feature，下一步就是把 `src/` 下的文件「挂」到模块树上，并按 feature 决定每个模块的**可见性**。本 crate 一共有六个子模块，它们的门控规则分三类：

| 模块 | 门控条件 | 何时可见 |
| --- | --- | --- |
| `alloc_helper` | `all(feature="alloc", target_has_atomic="ptr")` | 仅 `alloc`/`std` 档，且目标支持原子指针；私有（`mod`，非 `pub`） |
| `base` | `all(feature="alloc", target_has_atomic="ptr")` | 同上；公开（`pub mod`） |
| `map` | `feature = "std"` | 仅 `std` 档；公开 |
| `set` | `feature = "std"` | 仅 `std` 档；公开 |
| `comparator` | 无门控 | 任何档都可见；公开 |
| `equivalent` | 无门控 | 任何档都可见；公开 |

注意 `alloc_helper` 是**私有**的（`mod` 没有 `pub`），它只是 `base` 的内部工具；而 `comparator` / `equivalent` 是纯 trait 定义，不依赖堆也不依赖原子，所以**无条件可见**——哪怕在「第 0 档」也能用（虽然那一档目前整体不支持）。

为了让用户写 `crossbeam_skiplist::SkipMap` 而不必写 `crossbeam_skiplist::map::SkipMap`，`lib.rs` 还用 `pub use` 把三个主类型**重新导出**到 crate 根。

#### 4.3.2 核心流程

模块挂载与重导出的流程：

```text
#[cfg(alloc + target_has_atomic)]            # ---- alloc 档可见 ----
   mod alloc_helper;                         #   私有分配器工具
   pub mod base;                             #   底层无锁跳表
   #[doc(inline)] pub use base::SkipList;    #   重导出到根：crossbeam_skiplist::SkipList

#[cfg(std)]                                  # ---- std 档额外可见 ----
   pub mod map;                              #   SkipMap 封装
   pub mod set;                              #   SkipSet 封装
   #[doc(inline)] pub use {map::SkipMap, set::SkipSet};  # 重导出到根

（无条件）                                      # ---- 任何档都可见 ----
   pub mod comparator;
   pub mod equivalent;
```

`#[doc(inline)]` 的作用是：在生成的 rustdoc 里，重导出的类型**像直接定义在当前模块一样**显示，而不是显示成一个跳转链接。这让 API 文档更干净。

关于 `target_has_atomic = "ptr"`：这是 Rust 内置的 cfg，当**编译目标支持「指针大小」的原子操作**（即 `AtomicUsize` / `AtomicPtr` 等）时为真。绝大多数常见平台（x86、ARM、RISC-V……）都满足；只有少数极小的嵌入式目标不满足。`crossbeam-epoch` 的整套 epoch 回收机制建立在这些原子操作之上，所以核心模块必须挂这个条件。

#### 4.3.3 源码精读

完整的模块声明集中在 [src/lib.rs:249-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L249-L269)。逐块对应：

- [src/lib.rs:249-250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L249-L250)：`#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))] mod alloc_helper;`——私有分配器模块，仅 `alloc` 档可见。
- [src/lib.rs:252-257](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L252-L257)：`pub mod base;` 与 `#[doc(inline)] pub use crate::base::SkipList;`，两者都挂同一个 `cfg`——这意味着在 `alloc` 档下，`crossbeam_skiplist::SkipList` 直接可用。
- [src/lib.rs:259-266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L259-L266)：`pub mod map;` / `pub mod set;` 与 `#[doc(inline)] pub use crate::{map::SkipMap, set::SkipSet};`，只挂 `feature = "std"`——所以 `SkipMap` / `SkipSet` **只在 `std` 档可见**。
- [src/lib.rs:268-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L268-L269)：`pub mod comparator;` / `pub mod equivalent;`，**没有任何 cfg**——无条件可见。

把 [src/lib.rs:244-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L244-L269) 作为一个整体看，你会发现它的排列极有规律：**自底向上、按 feature 分层**——`alloc` 档的东西先出现，`std` 档的后出现，无门控的最后出现。这和 4.1 的「三档标准库」是一一对应的。

#### 4.3.4 代码实践

这是一个**编译观察型实践**，验证「关掉 `std` 后高层类型消失」。

1. **实践目标**：确认只开 `alloc` 时 `SkipList` 可用、`SkipMap` / `SkipSet` 不可用。
2. **操作步骤**：在 `crossbeam-skiplist` 目录下，写一个临时示例 `examples/no_std_probe.rs`（示例代码，仅供本实践，不要提交）：

   ```rust
   // 示例代码：探测各类型可见性
   use crossbeam_skiplist::SkipList;        // 期望：alloc 档可见
   // use crossbeam_skiplist::SkipMap;      // 期望：alloc 档不可见（被 cfg 屏蔽）
   fn main() {
       let _ = core::any::TypeId::of::<SkipList<u64, u64>>();
   }
   ```

   然后执行：

   ```bash
   cargo run --example no_std_probe --no-default-features --features alloc
   ```

3. **需要观察的现象**：
   - 只导入 `SkipList` 时，编译运行成功。
   - 若把注释行 `use crossbeam_skiplist::SkipMap;` 取消注释，应出现「`SkipMap` 未找到 / 未导出」之类的编译错误，因为 `map` 模块被 `cfg(feature = "std")` 屏蔽。
4. **预期结果**：`SkipList` 在 `alloc` 档可见；`SkipMap` / `SkipSet` 仅在 `std` 档可见。这印证了 [src/lib.rs:255-266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L255-L266) 的门控。
5. 完成后请删除临时示例文件，避免污染源码树。**待本地验证**确切报错文案。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `comparator` 和 `equivalent` 模块不加任何 `cfg`？

**参考答案**：它们只定义 trait 与少量 blanket 实现，内部只用到 `core`（`core::cmp::Ordering`、`core::borrow::Borrow`），既不需要堆分配，也不需要原子操作，所以在任何 feature 组合下（甚至纯 `core` 档）都能编译，因此无需门控。

**练习 2**：`#[doc(inline)]` 如果去掉，用户体验会有什么变化？

**参考答案**：rustdoc 会把 `pub use base::SkipList;` 显示成一个「Re-export」链接，用户需要多点一次才能跳到 `SkipList` 的文档页；加上 `#[doc(inline)]` 后，`SkipList` 像直接定义在 crate 根一样展示，文档更扁平、更友好。

---

### 4.4 三层包装：`base::SkipList` → `SkipMap` → `SkipSet`

#### 4.4.1 概念说明

现在把镜头对准**类型层次**。本 crate 的三个主类型不是平级的，而是层层包装：

```text
base::SkipList<K, V, C>          # 底层：真正的无锁跳表算法（在 base.rs）
        ▲
        │ inner: base::SkipList<K, V, C>
SkipMap<K, V, C = BasicComparator> # 高层：易用的 map 封装（在 map.rs）
        ▲
        │ inner: map::SkipMap<T, (), C>
SkipSet<T, C = BasicComparator>    # 高层：set 是 map 的 value=() 特化（在 set.rs）
```

为什么这样切？因为不同层次解决不同的问题：

- **`base::SkipList`**：只管「把无锁跳表算法做对」。它接受一个**外部传入的 `Collector`**（epoch 回收器）作为构造参数，因此自己不依赖全局状态，能在 `alloc` 档下运行。它的 API 偏底层，需要用户手动管理 `Guard` 生命周期。
- **`SkipMap`**：在 `base` 之上包一层「人体工学」API——自动用全局默认 collector、把 `RefEntry` 转成更顺手的 `Entry`、隐藏 epoch 细节。代价是它依赖 `epoch::default_collector()`，而后者需要 `std`，所以 `SkipMap` 只在 `std` 档可见。
- **`SkipSet`**：复用 `SkipMap` 的全部逻辑，只是把 value 类型固定为 `()`（空元组，零开销），并提供 set 风格的 API（`contains`、`value()` 返回元素本身）。

这就是「**底层尽量通用、高层尽量好用**」的经典分层。

#### 4.4.2 核心流程

依赖方向（谁 `use` 谁）严格自底向上，**没有环**：

```text
equivalent  ◀── comparator   # comparator.rs:5  use crate::equivalent::{...}
     ▲
     │
alloc_helper ──▶ base        # base.rs:18-21  use crate::{alloc_helper::Global, comparator::{...}}
                  ▲
                  │
                  map         # map.rs:12-15  use crate::{base::{self, try_pin_loop}, comparator::{...}}
                  ▲
                  │
                  set         # set.rs:8-11   use crate::{comparator::{...}, map}
```

要点：

1. `equivalent` 是叶子，不依赖任何内部模块。
2. `comparator` 依赖 `equivalent`。
3. `base` 依赖 `alloc_helper`（分配器）和 `comparator`（键比较）。
4. `map` 依赖 `base` 和 `comparator`。
5. `set` 依赖 `map` 和 `comparator`。

注意 `comparator` 被三层（`base` / `map` / `set`）共用——所以它才被设计成「无门控、随时可见」（见 4.3）。

#### 4.4.3 源码精读

**第一层包装：`SkipMap` 内嵌一个 `base::SkipList`。** 见 [src/map.rs:28-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L28-L30)：

```rust
pub struct SkipMap<K, V, C = BasicComparator> {
    inner: base::SkipList<K, V, C>,
}
```

`SkipMap` 本身只有一个字段 `inner`，所有方法都是委托给 `inner`（即 `base::SkipList`）。`C = BasicComparator` 是默认的比较器类型参数，对应「按 key 的自然 `Ord` 排序」。

`SkipMap::new` 的构造（[src/map.rs:42-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L42-L46)）揭示了它为什么需要 `std`：

```rust
pub fn new() -> Self {
    Self {
        inner: base::SkipList::new(epoch::default_collector().clone()),
    }
}
```

注意它调用了 `epoch::default_collector()`——**全局默认 collector**。这个函数由 `crossbeam-epoch` 提供，依赖线程局部的惰性初始化，需要 `std`。这就是 `map` / `set` 模块被 `feature = "std"` 门控的根本原因。而 `base::SkipList::new` 接受一个**显式传入的 `Collector``（见 [src/base.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L15) 引入了 `Collector` 类型），所以 `base` 不需要默认 collector、不需要 `std`。

**第二层包装：`SkipSet` 内嵌一个 `map::SkipMap<T, ()>`。** 见 [src/set.rs:24-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L24-L26)：

```rust
pub struct SkipSet<T, C = BasicComparator> {
    inner: map::SkipMap<T, (), C>,
}
```

`SkipSet` 把 value 类型写死成 `()`（零大小），于是 set 在内存上几乎不比 map 多花成本——这就是「set 复用 map」的零开销特化。`SkipSet::new`（[src/set.rs:38-42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L38-L42)）直接构造一个 `map::SkipMap::new()`，把所有工作都甩给 `SkipMap`。

**依赖方向的证据**：把三个文件的 `use` 声明放在一起看，结论很干净：

- [src/base.rs:18-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L18-L21)：`base` 用 `crate::{alloc_helper::Global, comparator::{BasicComparator, Comparator}}`。
- [src/map.rs:12-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L12-L15)：`map` 用 `crate::{base::{self, try_pin_loop}, comparator::{...}}`。
- [src/set.rs:8-11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L8-L11)：`set` 用 `crate::{comparator::{...}, map}`。
- [src/comparator.rs:5](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L5)：`comparator` 用 `crate::equivalent::{Comparable, Equivalent}`。

**关于 `alloc_helper`**：[src/alloc_helper.rs:3-7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L3-L7) 注明它是「基于尚未稳定的 `alloc::alloc::Global`」自实现的分配器。因为稳定版 `Global` 还没进标准库，作者只好在 crate 内手写一份，供 `base.rs` 分配变长跳表节点使用。这块细节留到 [u5-l16](u5-l16-no-std-alloc.md) 详讲。

#### 4.4.4 代码实践

这是一个**调用链追踪型实践**。

1. **实践目标**：跟踪一次 `SkipSet::insert` 是如何层层下落到 `base::SkipList` 的。
2. **操作步骤**：
   - 在 [src/set.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs) 中找到 `SkipSet::insert`，确认它调用了 `self.inner.insert(...)`，即委托给 `map::SkipMap`。
   - 在 [src/map.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs) 中找到 `SkipMap::insert`，确认它又委托给 `self.inner.insert(...)`，即 `base::SkipList`。
   - 至此调用进入 `base.rs` 的核心算法（`insert_internal`），那是 [u3-l10](u3-l10-insert-path.md) 的内容。
3. **需要观察的现象**：`set` 与 `map` 的方法体几乎都是「一行委托」，真正的算法全在 `base`。
4. **预期结果**：你会清晰地看到 `SkipSet → SkipMap → base::SkipList → insert_internal` 的调用栈，印证「三层包装、算法下沉」的设计。
5. 本步骤为静态阅读，无需运行命令。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SkipSet<T>` 复用 `SkipMap<T, ()>` 而不是直接复用 `base::SkipList<T, ()>`？

**参考答案**：因为 `SkipMap` 已经做好了「人体工学」封装——`Entry` 句柄、默认 collector、隐藏 `Guard`。`SkipSet` 想白嫖这些封装，所以复用 `SkipMap` 而不是重新去包 `base`。直接复用 `base::SkipList` 等于把 set 的所有易用性工作再做一遍。

**练习 2**：`SkipMap` 需要 `std` 的「罪魁祸首」是哪一行代码？

**参考答案**：是 [src/map.rs:44](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L44) 的 `epoch::default_collector().clone()`。`default_collector` 依赖 `crossbeam-epoch` 的 `std` feature（线程局部惰性初始化），所以 `map` / `set` 只能在 `std` 档编译。相比之下，`base::SkipList::new` 接受外部 `Collector`，不碰全局默认 collector，所以 `base` 能在 `alloc` 档运行。

**练习 3**：如果想在 `no_std + alloc` 环境里用跳表，应该用哪个类型？

**参考答案**：用 `base::SkipList`。你需要自己提供一个 `crossbeam_epoch::Collector`（而不是依赖 `default_collector()`），并手动管理 `Guard` 生命周期。这正是 `base` 模块被设计成「接受显式 Collector」的原因——为 `no_std` 场景留出可用路径。

---

## 5. 综合实践

本讲的综合实践把四个模块的知识串起来：**画一张模块依赖关系图，并用 `cargo doc` 验证可见性**。

### 5.1 实践目标

1. 用一张图把 `src/` 六个模块的依赖方向画清楚。
2. 用 rustdoc 验证：在默认（`std`）档下 `SkipList` / `SkipMap` / `SkipSet` 三个类型都在 crate 根可见。
3. 用 rustdoc 验证：在 `alloc` 档下只有 `SkipList` 可见，`SkipMap` / `SkipSet` 消失。

### 5.2 操作步骤

**第一步：画出依赖关系图。** 参考 4.4.2 的结论，自己手画或在文本里画出下面这张图（箭头表示「依赖 / 内嵌」）：

```text
        equivalent
            ▲
            │ use
        comparator ◀──────────────┐
            ▲                     │
            │ use                 │ use
   alloc_helper ──▶ base ◀─ map ◀─ set
                       ▲       ▲     │
                       │inner  │inner│
                   SkipList  SkipMap  SkipSet
                            (根重导出)
```

把它和 [src/lib.rs:244-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L244-L269) 的 `cfg` 注释对照检查：`alloc_helper` / `base` 挂 `alloc` 档，`map` / `set` 挂 `std` 档，`comparator` / `equivalent` 无门控。

**第二步：生成默认档文档并验证可见性。**

```bash
cd crossbeam-skiplist
cargo doc --no-deps --open
```

在打开的文档里确认：

- `crossbeam_skiplist` 根下能看到 `SkipList`、`SkipMap`、`SkipSet` 三个类型（因为 `#[doc(inline)] pub use`）。
- `base`、`map`、`set`、`comparator`、`equivalent` 五个模块都在（`alloc_helper` 是私有的，不应出现在文档里）。

**第三步：生成 `alloc` 档文档并对比。**

```bash
cargo doc --no-deps --features alloc --no-default-features
```

预期观察：根下只剩 `SkipList`（与 `comparator` / `equivalent` 模块），`SkipMap` / `SkipSet` 以及 `map` / `set` 模块都消失。

### 5.3 预期结果

- 依赖图与 4.4.2 完全一致，无环、自底向上。
- 默认档文档里三个主类型都在根可见；`alloc_helper` 不可见。
- `alloc` 档文档里只剩 `SkipList`。

### 5.4 思考延伸

如果你在自己的 `no_std + alloc` 项目里要用这个库，你会怎么写 `Cargo.toml` 的依赖？答案是：

```toml
# 示例代码：在 no_std + alloc 项目中依赖本 crate
[dependencies.crossbeam-skiplist]
version = "0.1"
default-features = false
features = ["alloc"]
```

然后只能用 `crossbeam_skiplist::base::SkipList`，并用不上 `SkipMap` / `SkipSet`。这一思考能帮你把「feature 门控」和「实际选型」真正挂钩。**待本地验证**你在目标嵌入式工具链上能否顺利编译。

## 6. 本讲小结

- 本 crate 顶部声明 `#![no_std]`，靠 `extern crate` + feature 按 **`core` → `alloc` → `std`** 三档逐层引入标准库（[src/lib.rs:231-247](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L231-L247)）。
- `Cargo.toml` 定义三档级联 feature：`default = ["std"]`、`std` 隐含 `alloc` 并把 feature 传递给 `crossbeam-epoch` / `crossbeam-utils`；同时关掉 `std` 和 `alloc` 目前不支持（[Cargo.toml:27-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L27-L38)）。
- 六个模块按 feature 分三类门控：`base` / `alloc_helper` 挂 `alloc + target_has_atomic`；`map` / `set` 挂 `std`；`comparator` / `equivalent` 无门控（[src/lib.rs:249-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L249-L269)）。
- `target_has_atomic = "ptr"` 保证目标平台支持原子指针操作——`crossbeam-epoch` 的 epoch 回收离不开它。
- 类型上是**三层包装**：`base::SkipList` → `SkipMap` → `SkipSet`，依赖方向严格自底向上、无环（[src/map.rs:28-30](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L28-L30)、[src/set.rs:24-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/set.rs#L24-L26)）。
- 分层的根因：`base` 接受显式 `Collector`，能在 `alloc` 档跑；`SkipMap` 用了 `epoch::default_collector()`，所以必须 `std`（[src/map.rs:42-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L42-L46)）。

## 7. 下一步学习建议

本讲建立了「项目骨架」，接下来可以选两条路：

- **想先会用再深入**：进入第二单元，从 [u2-l5（Node 与 Tower 内存布局）](u2-l5-node-and-tower-layout.md) 开始读 `base.rs` 的核心数据结构；这是理解所有算法的前置。
- **想先把工程细节补全**：先读 [u1-l4（构建、测试与基准对比）](u1-l4-build-test-bench.md)，学会怎么跑测试和基准，再进入第二单元。

无论选哪条路，建议同时打开 [src/base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) 作为参考——后续讲义会频繁进入这个文件，而你现在已经知道它在模块树里的位置（`alloc` 档、被 `map` 包装、依赖 `alloc_helper` 与 `comparator`）。

如果你对 `no_std` 支持特别感兴趣，可以直接跳到 [u5-l16（no_std 与 alloc 支持）](u5-l16-no-std-alloc.md)，那里会详讲 `alloc_helper::Global` 的实现。
