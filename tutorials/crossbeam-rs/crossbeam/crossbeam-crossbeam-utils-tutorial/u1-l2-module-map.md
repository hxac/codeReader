# 目录结构与模块地图

## 1. 本讲目标

本讲紧接 [u1-l1 项目概览与定位](u1-l1-project-overview.md)，从「这个 crate 有哪些功能」推进到「这些功能在源码里究竟长什么样、由谁打开、对谁可见」。

学完后你应当能够：

- 画出 `crossbeam-utils` 的模块依赖与导出关系图。
- 说清楚 `feature`（`std` / `atomic`）和 `cfg`（`crossbeam_loom`、`target_has_atomic`）如何控制每个模块是否被编译、是否对外可见。
- 区分三类模块：对外公开模块（`pub mod`）、对外公开类型（`mod` + `pub use` 重导出）、纯内部辅助模块（`pub(crate)` 或私有 `mod`）。
- 给定任意一个 feature 组合，能预测 `AtomicCell`、`AtomicConsume`、`Parker`、`WaitGroup`、`ShardedLock`、`Backoff`、`CachePadded`、`scope` 中哪些可用、哪些不可用。

本讲**只看「门面」**：`lib.rs` 与两个 `mod.rs`。各个类型的内部实现（`AtomicCell` 的无锁路径、`Parker` 的三态机等）留给后续讲义。

## 2. 前置知识

### 2.1 Rust 的模块系统三件事

阅读本讲前，你需要先理解 Rust 模块系统的三个动作，它们是理解本讲的钥匙：

1. **声明模块**：`mod foo;` 告诉编译器「有一个子模块叫 `foo`」，编译器会去找 `foo.rs` 或 `foo/mod.rs`。**一个 `.rs` 文件只有被某处的 `mod` 声明引用，才会被编译进 crate**——这是后面会反复用到的事实。
2. **控制可见性**：`mod foo;` 是私有模块，只有同 crate 内部能访问；`pub mod foo;` 是公开模块，crate 外部也能访问。
3. **重导出（re-export）**：`pub use crate::foo::Bar;` 把 `Bar` 这个名字在当前路径上重新暴露一次。常见用法是「模块保持私有，但把它内部的某个类型重导出到更顺手的位置」。

### 2.2 条件编译 cfg

`#[cfg(条件)]` 标注的代码，只有在条件成立时才会被编译。本讲涉及的条件有：

| 条件 | 含义 | 由谁决定 |
|------|------|----------|
| `feature = "std"` | 启用了 `std` 特性 | 用户在 `Cargo.toml` 里开启 |
| `feature = "atomic"` | 启用了 `atomic` 特性 | 用户在 `Cargo.toml` 里开启 |
| `crossbeam_loom` | 正在用 loom 做并发交错测试 | 测试时通过 `--cfg` 开启 |
| `target_has_atomic = "ptr"` | 目标平台支持指针宽度的原子操作 | 由编译器根据目标平台判定 |

> 名词解释：**loom** 是 Rust 的一个并发测试工具，它会穷举线程交错来暴露数据竞争。`crossbeam-utils` 为了能在 loom 下测试，把标准库的 `Arc`/`Mutex`/`Condvar`/原子类型抽成了一层内部别名，loom 模式下换成 loom 自己的实现。这层抽象是下一节会看到的 `primitive` 模块。

### 2.3 feature 与默认 feature

`Cargo.toml` 里的 `[features]` 定义了特性开关。本 crate 有：

- `default = ["std"]`：不显式指定时，默认开启 `std`。
- `std = []`：开启后，依赖 `std`、`alloc` 的模块（`sync`、`thread`）才可见。
- `atomic = ["atomic-maybe-uninit"]`：开启后，`atomic` 模块才可见；它会顺带拉入 `atomic-maybe-uninit` 依赖，且**需要 Rust 1.74**。

把 2.1 的模块系统、2.2 的 cfg、2.3 的 feature 组合起来，就是本讲的全部技术基础。

## 3. 本讲源码地图

本讲只读 4 个文件，它们恰好构成 crate 的「骨架」：

| 文件 | 角色 | 本讲关注点 |
|------|------|------------|
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs) | crate 根 | `#![no_std]`、`primitive` 内部抽象层、四个功能模块的声明与门控 |
| [src/atomic/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs) | atomic 子模块根 | `AtomicCell` / `AtomicConsume` 的声明与门控 |
| [src/sync/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs) | sync 子模块根 | `Parker` / `WaitGroup` / `ShardedLock` 的声明与门控 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml) | 包元信息 | `[features]` 段 |

> 本讲**不展开** `src/backoff.rs`、`src/cache_padded.rs`、`src/thread.rs`、`src/atomic/atomic_cell.rs`、`src/sync/parker.rs` 等实现文件的内部逻辑——它们的实现是后续讲义的主题。本讲只关心「它们在哪、由谁打开、对谁可见」。

## 4. 核心概念与源码讲解

### 4.1 lib.rs：crate 的总入口与模块导出

#### 4.1.1 概念说明

`lib.rs` 是整个 crate 的根：编译器从这里开始读，所有模块最终都（直接或间接）挂在这棵树上。`crossbeam-utils` 的 `lib.rs` 做了三件事：

1. **声明 no_std**：用 `#![no_std]`（`!` 表示作用于整个 crate）告诉编译器默认不链接标准库，使本 crate 能在裸机 / 内核等无操作系统环境里使用。
2. **定义内部抽象层 `primitive`**：把「标准库 or loom」的差异藏在一个 `pub(crate)` 模块里，让上层代码只依赖 `crate::primitive::sync::atomic` 这样的别名。
3. **声明并门控四个功能模块**：`atomic`、`cache_padded`、`backoff`、`sync`、`thread`，每个模块按需挂上 `#[cfg(...)]`。

关键设计取舍：**可见性是 README 的 no_std 标记与 lib.rs 的 cfg 一一对应的**。回看 [u1-l1](u1-l1-project-overview.md) 里 README 的标注——`AtomicCell`/`AtomicConsume`/`Backoff`/`CachePadded` 标了 <sup>(no_std)</sup>，而 `Parker`/`ShardedLock`/`WaitGroup`/`scope` 没标。本讲会在源码里找到这条对应关系的「证据」。

#### 4.1.2 核心流程

`lib.rs` 的组织可以用下面的伪代码表示（顺序与源码一致）：

```
#![no_std]                              // 1. 不默认链接 std

extern crate alloc;   // 仅 feature="std" 且 非 loom
extern crate std;     // 仅 feature="std"

mod primitive { ... } // 2. 内部抽象层：loom 分支 vs 标准库分支

pub mod atomic;       // 3. 仅 feature="atomic"
mod cache_padded; pub use cache_padded::CachePadded;   // 4. 始终可用
mod backoff;     pub use backoff::Backoff;             // 5. 始终可用
pub mod sync;         // 6. 仅 feature="std"
pub mod thread;       // 7. 仅 feature="std" 且 非 loom
```

判断「某个类型在某 feature 组合下是否可见」的决策树：

```
该类型所属的顶层模块是？
├─ atomic   → 需要 feature="atomic"（且部分类型还要求 target_has_atomic="ptr"）
├─ sync     → 需要 feature="std"
├─ thread   → 需要 feature="std" 且 非 crossbeam_loom
└─ 顶层重导出（Backoff / CachePadded）→ 始终可见，无任何 feature 要求
```

#### 4.1.3 源码精读

**no_std 声明与 std 引入。** 第 27 行的 `#![no_std]` 让 crate 默认不依赖标准库；而第 41–45 行在开启 `std` 特性时再显式把 `alloc` 和 `std` 引入——这正是「no_std 友好但可选支持 std」的标准写法：

[src/lib.rs:27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L27) — 整个 crate 标记为 no_std。

[src/lib.rs:41-45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L41-L45) — 仅在 `feature="std"` 时引入 `alloc`（且排除 loom）与 `std`。

**内部抽象层 `primitive`。** 第 47–83 行定义了 `mod primitive`，它有两个互斥分支：`#[cfg(crossbeam_loom)]` 分支（第 47–69 行）把 `hint`/`sync::atomic`/`Arc`/`Mutex`/`Condvar` 全部指向 `loom` 的实现；`#[cfg(not(crossbeam_loom))]` 分支（第 70–83 行）指向标准库 / `alloc` / `core`。注意它是 **`pub(crate)`** 的——只对本 crate 内部可见，外部用户看不到这层抽象：

[src/lib.rs:47-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83) — `primitive` 内部抽象层（loom 分支与标准库分支二选一）。

**四个功能模块的声明与门控。** 这是本讲最关键的一段：

[src/lib.rs:85-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L100) — `atomic` / `cache_padded` / `backoff` / `sync` / `thread` 五个模块的声明与门控。

逐行拆解这一段：

| 行 | 代码 | 模式 | 门控 |
|----|------|------|------|
| 85–87 | `pub mod atomic;` | 公开模块 | `feature="atomic"` |
| 89–90 | `mod cache_padded;` + `pub use ...CachePadded;` | 私有模块 + 顶层重导出 | 无（始终可用） |
| 92–93 | `mod backoff;` + `pub use ...Backoff;` | 私有模块 + 顶层重导出 | 无（始终可用） |
| 95–96 | `pub mod sync;` | 公开模块 | `feature="std"` |
| 98–100 | `pub mod thread;` | 公开模块 | `feature="std"` 且 非 `crossbeam_loom` |

这里出现了两种截然不同的导出风格，务必分清：

- **`pub mod atomic;` / `pub mod sync;` / `pub mod thread;`**：整个子模块对外公开。用户写 `crossbeam_utils::sync::Parker`、`crossbeam_utils::thread::scope`，能直接进入子模块路径。
- **`mod cache_padded;` + `pub use crate::cache_padded::CachePadded;`**：子模块本身是**私有**的（用户不能写 `crossbeam_utils::cache_padded::CachePadded`），但把里面的 `CachePadded` 类型重导出到 crate 根。于是用户写 `crossbeam_utils::CachePadded` 即可。`Backoff` 同理。

> 设计意图：`Backoff` 和 `CachePadded` 是「叶子类型」，没必要暴露一个公开命名空间；而 `sync`、`atomic`、`thread` 各自包含多个相关类型，值得用一个公开模块把它们组织起来。

把这张表和 README 的 no_std 标注对照：`cache_padded`、`backoff` 没有任何 `feature` 门控 → 始终可见 → README 标 <sup>(no_std)</sup>；`sync`、`thread` 需要 `feature="std"` → 不可见 on no_std → README 不标。证据闭环。

#### 4.1.4 代码实践

**实践目标**：用一个最小 binary 验证 feature 门控的真实效果，亲手看到「开/关 feature 会改变哪些类型存在」。

**操作步骤**：

1. 在 `crossbeam-utils` 目录**之外**新建一个 binary crate（避免循环依赖）：

   ```bash
   cargo new --bin utils_probe
   cd utils_probe
   ```

2. 把 `utils_probe/Cargo.toml` 的依赖改成指向本地路径（把 `<绝对路径>` 换成你机器上 `crossbeam-utils` 的实际路径）：

   ```toml
   [dependencies]
   crossbeam-utils = { path = "<绝对路径>/crossbeam-utils" }
   ```

3. 在 `utils_probe/src/main.rs` 里写一段引用多种类型的代码：

   ```rust
   // 示例代码：用于探测不同 feature 下哪些类型可见
   use crossbeam_utils::Backoff;          // 顶层重导出
   use crossbeam_utils::CachePadded;      // 顶层重导出
   use crossbeam_utils::atomic::AtomicCell; // atomic 模块
   use crossbeam_utils::sync::WaitGroup;  // sync 模块
   use crossbeam_utils::thread;           // thread 模块

   fn main() {
       let _b = Backoff::new();
       let _c = CachePadded::new(0u32);
       let _a = AtomicCell::new(0u32);
       let _w = WaitGroup::new();
       let _ = thread::scope(|s| { s.spawn(|_| ()).unwrap(); () });
   }
   ```

4. 分别用四种 feature 组合编译，每次只记录「能否通过编译」：

   ```bash
   cargo build                                   # 默认 = std
   cargo build --no-default-features             # 啥都不开
   cargo build --no-default-features --features atomic
   cargo build --features atomic                 # std + atomic
   ```

**需要观察的现象**：每次编译失败时，`rustc` 会报 `unresolved import` 或 `could not find`，告诉你哪个模块/类型不存在。

**预期结果**（依据 [src/lib.rs:85-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L100) 的 cfg 逻辑推断；`AtomicCell` 在典型 64 位目标上额外满足 `target_has_atomic="ptr"`）：

| feature 组合 | Backoff/CachePadded | atomic::AtomicCell | sync::WaitGroup | thread::scope |
|--------------|---------------------|--------------------|-----------------|---------------|
| 默认 (std) | ✅ | ❌（缺 atomic） | ✅ | ✅ |
| --no-default-features | ✅ | ❌ | ❌ | ❌ |
| --no-default-features --features atomic | ✅ | ✅ | ❌（缺 std） | ❌ |
| --features atomic (= std + atomic) | ✅ | ✅ | ✅ | ✅ |

> 待本地验证：上表中 `AtomicCell` 那一列还依赖目标平台是否满足 `target_has_atomic="ptr"`。在 x86-64 / aarch64 等 64 位目标上为真；若你在某些特殊目标上编译，请以本地实际结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `lib.rs` 用 `mod backoff;` + `pub use` 而不是直接 `pub mod backoff;`？

> **答案**：因为 `Backoff` 是单个叶子类型，作者不希望对外暴露一个公开的 `backoff` 命名空间。私有 `mod` + 顶层 `pub use` 让用户直接写 `crossbeam_utils::Backoff`，既隐藏了模块内部细节，又得到了最短的访问路径。`CachePadded` 出于同样理由采用相同写法。

**练习 2**：`thread` 模块除了 `feature="std"` 之外，还多了一个 `#[cfg(not(crossbeam_loom))]`。请猜测原因。

> **答案**：`thread::scope` 的实现里用到了 `std::thread::spawn`、`catch_unwind`、`JoinHandle` 等与真实线程强相关的机制，而 loom 有自己线程模型，无法直接替换这些 `std::thread` 调用，因此 loom 模式下干脆不编译 `thread` 模块。（这与 `atomic_cell` 在 loom 下不可用是同类取舍。）

**练习 3**：如果用户既不开启 `std` 也不开启 `atomic`（即 `--no-default-features`），这个 crate 还剩什么对外公开的东西？

> **答案**：只剩 `Backoff` 和 `CachePadded` 两个顶层重导出类型。`atomic`、`sync`、`thread` 三个模块全部被 cfg 排除。这正是 README 里只有这两个 Utility 类型在「最朴素」的 no_std 环境也一定能用的体现。

---

### 4.2 atomic/mod.rs：原子子模块的内部组织

#### 4.2.1 概念说明

`atomic` 是个**公开模块**（`pub mod atomic;`，门控在 `feature="atomic"`），它自己又是一个「子模块根」，由 `src/atomic/mod.rs` 描述它包含哪些更深的子模块、哪些类型对外导出。

`atomic/mod.rs` 把工作分成两块：

1. **公开类型**：`AtomicCell`（线程安全可变内存位置）、`AtomicConsume`（用 consume 序读取原生原子类型的 trait）。
2. **内部支持模块**：`seq_lock`（`AtomicCell` 在无法原子化时的全局锁回退所需的 SeqLock，后续 [u2-l3](u2-l3-atomiccell-global-lock-seqlock.md) 详讲）。

这里会出现第二层 cfg 门控——不是 feature，而是**目标平台能力**（`target_has_atomic="ptr"`）和**测试模式**（`crossbeam_loom`）。

#### 4.2.2 核心流程

`atomic/mod.rs` 的决策流程：

```
进入 atomic 模块（前提：feature="atomic" 已开启）

对于 consume 子模块：
  → 始终声明 mod consume; 并 pub use AtomicConsume;   （无额外门控）

对于 atomic_cell 子模块（AtomicCell）：
  if target_has_atomic="ptr" 且 非 crossbeam_loom:
      → 声明 mod atomic_cell; 并 pub use AtomicCell;
  else:
      → 整个 AtomicCell 不存在

对于 seq_lock 子模块（内部支持）：
  if target_has_atomic="ptr" 且 非 crossbeam_loom:
      → 声明 mod seq_lock;（私有，仅内部用）
      其中若 target_pointer_width 为 16 或 32，改用 seq_lock_wide.rs
```

> 注意一个细节：`AtomicConsume` 是个 trait，作用在**原生原子类型**（如 `core::sync::atomic::AtomicUsize`）上，不需要 `AtomicCell` 那套 SeqLock 回退，因此它的门控比 `AtomicCell` 宽松——只要进了 `atomic` 模块就存在。

#### 4.2.3 源码精读

**SeqLock 内部支持模块（私有）。** 第 6–19 行声明了 `mod seq_lock;`，但它带了两层 cfg（`target_has_atomic="ptr"` 且非 loom），并且用 `cfg_attr(... path = "seq_lock_wide.rs")` 在 16/32 位指针宽度下把文件路径切到「宽版本」。它是**私有 `mod`**，外部完全不可见：

[src/atomic/mod.rs:6-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L6-L19) — 私有的 `seq_lock` 子模块，按目标指针宽度在 `seq_lock.rs` 与 `seq_lock_wide.rs` 间切换。

**AtomicCell（公开类型）。** 第 21–29 行先声明私有子模块 `mod atomic_cell;`，再用 `pub use self::atomic_cell::AtomicCell;` 把类型导出到 `atomic` 命名空间下。两层 cfg 与 `seq_lock` 相同：

[src/atomic/mod.rs:21-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L21-L29) — `AtomicCell` 的声明与重导出，门控 `target_has_atomic="ptr"` 且非 loom。

**AtomicConsume（公开类型）。** 第 31–32 行是最简单的一段：没有任何额外 cfg，只要 `atomic` 模块被编译（即 `feature="atomic"`），`consume` 子模块和 `AtomicConsume` trait 就一定存在：

[src/atomic/mod.rs:31-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L31-L32) — `consume` 子模块与 `AtomicConsume` 的重导出，无额外门控。

把这三段汇总成 `atomic` 模块内部的一张可见性表：

| 名字 | 物理文件 | 对 crate 外 | 额外 cfg |
|------|----------|-------------|----------|
| `AtomicCell` | `src/atomic/atomic_cell.rs` | 可见（`atomic::AtomicCell`） | `target_has_atomic="ptr"` 且 非 loom |
| `AtomicConsume` | `src/atomic/consume.rs` | 可见（`atomic::AtomicConsume`） | 无 |
| `seq_lock`（SeqLock） | `src/atomic/seq_lock.rs` 或 `seq_lock_wide.rs` | 不可见（私有 `mod`） | `target_has_atomic="ptr"` 且 非 loom |

#### 4.2.4 代码实践

**实践目标**：亲手验证 `AtomicCell` 与 `AtomicConsume` 的门控差异，并理解「同一公开模块下的两个类型，门控可以不同」。

**操作步骤**：

1. 复用 4.1.4 的 `utils_probe` 工程，把 `main.rs` 改成只引用 atomic 模块的两个类型：

   ```rust
   // 示例代码
   use crossbeam_utils::atomic::AtomicCell;
   use crossbeam_utils::atomic::AtomicConsume;
   use core::sync::atomic::AtomicUsize;

   fn main() {
       let a = AtomicCell::new(7_u32);
       let _ = a.load();

       let b = AtomicUsize::new(3);
       // AtomicConsume 是 trait，load_consume 通过 trait 方法调用
       let _ = b.load_consume();
   }
   ```

2. 关掉默认特性、只开 atomic，在普通 64 位目标上编译：

   ```bash
   cargo build --no-default-features --features atomic
   ```

**需要观察的现象**：编译通过。这说明在 `--features atomic` 下，即便没有 `std`，`AtomicCell` 与 `AtomicConsume` 都可用——对应 README 里它们都标了 <sup>(no_std)</sup>。

**预期结果**：

- `--no-default-features --features atomic`：`AtomicCell` ✅、`AtomicConsume` ✅。
- `--no-default-features`（不开 atomic）：两者都 ❌（`atomic` 模块整体不存在，会报 `could not find 'atomic'`）。

> 待本地验证：若你的目标平台不满足 `target_has_atomic="ptr"`，则即便开了 `atomic` 特性，`AtomicCell` 仍不可用（但 `AtomicConsume` 仍可用）。这正好印证了 4.2.3 表格中两者 cfg 不同的事实。

#### 4.2.5 小练习与答案

**练习 1**：`seq_lock` 是 `atomic` 模块下的子模块，为什么用户不能写 `crossbeam_utils::atomic::seq_lock::SeqLock`？

> **答案**：因为它是用 `mod seq_lock;`（私有）声明的，不是 `pub mod`。它只是 `AtomicCell` 的内部实现细节（全局锁回退用的 SeqLock），作者刻意不对外暴露。只有 `AtomicCell` 被 `pub use` 导出。

**练习 2**：`AtomicConsume` 比 `AtomicCell` 少了 `target_has_atomic="ptr"` 这层门控。结合它在 `src/atomic/consume.rs:5` 的定义（`pub trait AtomicConsume`），解释为什么它不需要这层门控。

> **答案**：`AtomicConsume` 是一个作用在**原生原子类型**（如 `AtomicUsize`）上的 trait，它只是给已有原子类型增加一个 `load_consume` 方法。只要平台有原生原子类型可用，trait 本身就能定义；它不需要 `AtomicCell` 那套「SeqLock 全局锁回退」机制，因此不依赖 `target_has_atomic="ptr"` 这个用于回退实现的条件。

**练习 3**：第 15–18 行用 `cfg_attr(any(target_pointer_width = "16", target_pointer_width = "32"), path = "seq_lock_wide.rs")` 切换文件。这句话的意思是？

> **答案**：当目标指针宽度是 16 位或 32 位时，`mod seq_lock;` 不再加载默认的 `seq_lock.rs`，而是加载 `seq_lock_wide.rs`。这是因为在窄架构上 SeqLock 的印戳计数器位数少、容易「回绕」，需要宽版本对策。具体机制留到 [u5-l3](u5-l3-cfg-loom-wideseqlock.md) 详讲。

---

### 4.3 sync/mod.rs：同步原语子模块的内部组织

#### 4.3.1 概念说明

`sync` 也是一个**公开模块**（`pub mod sync;`，门控在 `feature="std"`），它包含三类同步原语：

- `Parker`（线程挂起）、`Unparker`、`UnparkReason`——来自 `parker.rs`。
- `WaitGroup`（引用计数式同步）——来自 `wait_group.rs`。
- `ShardedLock`（分片读写锁）及其读/写 guard——来自 `sharded_lock.rs`。

此外还有一个**纯内部**的 `once_lock` 模块，它只在 loom 之外编译，且只对 crate 内部可见——`ShardedLock` 用它来惰性建立「线程 → 分片索引」注册表（详见 [u3-l3](u3-l3-shardedlock.md) 与 [u3-l4](u3-l4-oncelock.md)）。

本小节的重点是：观察 `sync/mod.rs` 如何把「公开类型」和「内部模块」混在一起声明，以及 `crossbeam_loom` 如何在这里造成不对称（`ShardedLock` 在 loom 下消失，`Parker`/`WaitGroup` 却保留）。

#### 4.3.2 核心流程

`sync/mod.rs` 的组织流程：

```
进入 sync 模块（前提：feature="std" 已开启）

声明内部子模块：
  mod once_lock;     仅 非 loom（私有，ShardedLock 专用）
  mod parker;        无额外门控（私有模块）
  mod sharded_lock;  仅 非 loom（私有模块）
  mod wait_group;    无额外门控（私有模块）

对外重导出公开类型：
  if 非 loom:
      pub use sharded_lock::{ShardedLock, ShardedLockReadGuard, ShardedLockWriteGuard};
  pub use parker::{Parker, Unparker, UnparkReason};
  pub use wait_group::WaitGroup;
```

注意所有公开类型都走「私有 `mod` + `pub use`」的统一模式——和 `Backoff`/`CachePadded` 一样，子模块不对外暴露路径，只把类型名导出到 `sync` 命名空间下（用户写 `crossbeam_utils::sync::Parker`，而不是 `crossbeam_utils::sync::parker::Parker`）。

#### 4.3.3 源码精读

**子模块声明。** 第 7–12 行声明了四个私有子模块，其中 `once_lock` 和 `sharded_lock` 带 `#[cfg(not(crossbeam_loom))]`，`parker` 和 `wait_group` 不带：

[src/sync/mod.rs:7-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs#L7-L12) — 四个私有子模块的声明，`once_lock` / `sharded_lock` 排除 loom。

**公开类型重导出。** 第 14–19 行把类型导出到 `sync` 路径下，其中 `ShardedLock` 那条带 `#[cfg(not(crossbeam_loom))]`，而 `Parker`/`Unparker`/`UnparkReason`/`WaitGroup` 不带：

[src/sync/mod.rs:14-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs#L14-L19) — 公开类型重导出，`ShardedLock` 排除 loom。

汇总成 `sync` 模块的可见性表：

| 名字 | 物理文件 | 对 crate 外 | 额外 cfg |
|------|----------|-------------|----------|
| `Parker` / `Unparker` / `UnparkReason` | `src/sync/parker.rs` | 可见（`sync::Parker` 等） | 无 |
| `WaitGroup` | `src/sync/wait_group.rs` | 可见（`sync::WaitGroup`） | 无 |
| `ShardedLock` / `ShardedLockReadGuard` / `ShardedLockWriteGuard` | `src/sync/sharded_lock.rs` | 可见（`sync::ShardedLock` 等） | 非 loom |
| `once_lock`（内部 `OnceLock`） | `src/sync/once_lock.rs` | 不可见（私有 `mod`） | 非 loom |

> 一个值得注意的不对称：loom 模式下 `ShardedLock` 整个消失（它依赖内部的 `once_lock`，而 `once_lock` 也在 loom 下消失），但 `Parker` 和 `WaitGroup` 仍然保留。这说明 loom 能测试 `Parker`/`WaitGroup` 的并发正确性，却无法覆盖 `ShardedLock`——这是阅读测试时需要留意的覆盖盲区。

#### 4.3.4 代码实践

**实践目标**：通过「代码阅读 + 编译探测」确认 `once_lock` 是纯内部模块、`ShardedLock` 是公开类型，并理解它们之间的依赖。

**操作步骤（源码阅读型）**：

1. 打开 [src/sync/once_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/once_lock.rs)，找到 `OnceLock` 结构体定义。注意它的可见性是 `pub(crate)`——对本 crate 可见，但 crate 外部用不到。

2. 用搜索工具确认 `OnceLock` 被谁使用（在 `crossbeam-utils` 目录内搜索 `OnceLock`）。预期会发现它被 `sharded_lock.rs` 引用，用于为每个线程惰性分配一个分片索引。

3. 在 `utils_probe` 里尝试写一条注定失败的外部引用，亲眼看到「内部模块对外不可见」：

   ```rust
   // 示例代码：这条语句应当编译失败
   use crossbeam_utils::sync::once_lock::OnceLock;
   ```

   然后编译（开启 `std`）：

   ```bash
   cargo build
   ```

**需要观察的现象**：第 3 步应报错，提示 `once_lock` 是私有模块 / `OnceLock` 是 `pub(crate)` 不可在外部使用。而把 `use` 换成 `use crossbeam_utils::sync::ShardedLock;` 则能正常通过。

**预期结果**：

- `use crossbeam_utils::sync::once_lock::OnceLock;` → ❌ 编译失败（私有）。
- `use crossbeam_utils::sync::ShardedLock;` → ✅ 编译通过（公开重导出）。
- `use crossbeam_utils::sync::WaitGroup;` → ✅ 编译通过。

这说明：**「文件存在于 `src/sync/` 下」不等于「对外可见」**。可见性由 `mod.rs` 里的 `pub` / `pub use` / `pub(crate)` 决定。

#### 4.3.5 小练习与答案

**练习 1**：`sync/mod.rs` 里所有子模块都是 `mod xxx;`（私有），没有一个 `pub mod`。那用户是怎么用到 `Parker` 的？

> **答案**：通过第 16–19 行的 `pub use self::{parker::{Parker, UnparkReason, Unparker}, wait_group::WaitGroup};`。子模块路径 `sync::parker` 虽然私有，但里面的类型被重导出到了 `sync` 命名空间下，所以用户写 `crossbeam_utils::sync::Parker` 即可。

**练习 2**：为什么 `once_lock` 和 `sharded_lock` 都带 `#[cfg(not(crossbeam_loom))]`，而 `parker` 和 `wait_group` 不带？

> **答案**：`ShardedLock` 的实现依赖内部 `OnceLock` 建立线程索引注册表，这套机制与真实 `std::thread::ThreadId` 强绑定，loom 无法良好模拟，因此在 loom 模式下整体禁用（`once_lock` 和 `sharded_lock` 一起消失）。`Parker` 和 `WaitGroup` 则通过 `crate::primitive` 抽象层使用了 loom 可替换的 `Mutex`/`Condvar`/`Arc`，能在 loom 下正常运行，故不需要排除 loom。

**练习 3**：`ShardedLockReadGuard` 和 `ShardedLockWriteGuard` 这两个 guard 类型，用户需要单独 `use` 吗？它们从哪里导出？

> **答案**：需要单独 `use`（如果要在类型位置写它们的名字）。它们和 `ShardedLock` 一起在第 15 行被 `pub use self::sharded_lock::{ShardedLock, ShardedLockReadGuard, ShardedLockWriteGuard};` 导出，路径是 `crossbeam_utils::sync::ShardedLockReadGuard` 等。同样受 `#[cfg(not(crossbeam_loom))]` 门控。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一份**模块树文档**——这是理解任何 Rust crate 结构的标准产物。

**任务**：产出一份 Markdown 表格（或树形图），覆盖 `crossbeam-utils` 所有公开类型，并标注三列关键信息：**物理源文件路径**、**所属 feature**、**是否对 crate 外可见**。

**操作步骤**：

1. 以本讲的 [src/lib.rs:85-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L85-L100)、[src/atomic/mod.rs:21-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L21-L32)、[src/sync/mod.rs:14-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs#L14-L19) 为唯一事实来源。

2. 整理出下面这张「公开类型总表」（请你自己填写最右两列，再对照下方的参考答案）：

   | 公开类型 | 物理文件 | 所属 feature | 对 crate 外可见？ |
   |----------|----------|--------------|-------------------|
   | `AtomicCell` | `src/atomic/atomic_cell.rs` | ? | ? |
   | `AtomicConsume` | `src/atomic/consume.rs` | ? | ? |
   | `Backoff` | `src/backoff.rs` | ? | ? |
   | `CachePadded` | `src/cache_padded.rs` | ? | ? |
   | `Parker` / `Unparker` / `UnparkReason` | `src/sync/parker.rs` | ? | ? |
   | `WaitGroup` | `src/sync/wait_group.rs` | ? | ? |
   | `ShardedLock`（+ 两个 guard） | `src/sync/sharded_lock.rs` | ? | ? |
   | `scope` / `Scope` / `ScopedThreadBuilder` / `ScopedJoinHandle` | `src/thread.rs` | ? | ? |

3. 再补一张「纯内部模块表」，列出：`primitive`（在 `lib.rs` 内联）、`seq_lock` / `seq_lock_wide`、`once_lock`，以及那个**没有被任何 `mod` 声明、因此根本不参与编译**的孤儿文件 `src/alloc_helper.rs`（可在 `crossbeam-utils` 目录内搜索 `mod alloc_helper` 验证它确实没有声明）。

4. 用一行命令把整棵模块树打印出来，直观感受 crate 骨架（仅列出 `src/` 下文件）：

   ```bash
   git ls-files src/ | sort
   ```

**参考答案（公开类型总表）**：

| 公开类型 | 物理文件 | 所属 feature | 对 crate 外可见？ |
|----------|----------|--------------|-------------------|
| `AtomicCell` | `src/atomic/atomic_cell.rs` | `atomic` | ✅ |
| `AtomicConsume` | `src/atomic/consume.rs` | `atomic` | ✅ |
| `Backoff` | `src/backoff.rs` | 无（始终可用） | ✅ |
| `CachePadded` | `src/cache_padded.rs` | 无（始终可用） | ✅ |
| `Parker` / `Unparker` / `UnparkReason` | `src/sync/parker.rs` | `std` | ✅ |
| `WaitGroup` | `src/sync/wait_group.rs` | `std` | ✅ |
| `ShardedLock`（+ 两个 guard） | `src/sync/sharded_lock.rs` | `std` | ✅ |
| `scope` / `Scope` / `ScopedThreadBuilder` / `ScopedJoinHandle` | `src/thread.rs` | `std` | ✅ |

> 这张表就是本讲的「最终产物」。把它和 README 的分类（Atomics / Thread synchronization / Utilities）一一对照，你会发现：Atomics = `atomic` feature；Thread synchronization = `std` feature；Utilities 里 `Backoff`/`CachePadded` 无 feature、`scope` 走 `std`。全部对得上。

## 6. 本讲小结

- `lib.rs` 是 crate 根，三件事：`#![no_std]`、内部抽象层 `primitive`、四个功能模块（`atomic` / `cache_padded`+`backoff` / `sync` / `thread`）的声明与门控。
- 存在两种导出风格：`pub mod`（公开整个子模块，如 `sync`）vs 私有 `mod` + `pub use`（只重导出类型到更短路径，如 `Backoff`、`CachePadded`、`sync` 下的所有类型）。
- feature 门控的硬规则：`atomic` 模块需 `feature="atomic"`；`sync`、`thread` 需 `feature="std"`；`Backoff`、`CachePadded` 无任何 feature 要求，这正好对应 README 的 <sup>(no_std)</sup> 标记。
- 在 feature 之外，还有平台/测试维度的 cfg：`AtomicCell` 与 `seq_lock` 需 `target_has_atomic="ptr"` 且非 loom；`ShardedLock` / `once_lock` / `thread` 需非 loom。
- 「文件存在」≠「被编译」≠「对外可见」。`src/alloc_helper.rs` 没有被任何 `mod` 声明，根本不参与编译；`once_lock`、`seq_lock`、`primitive` 虽被编译但是 `pub(crate)` / 私有，外部不可见。
- 至此你已建立完整的「骨架地图」：知道每个公开类型住在哪里、由哪把 feature 钥匙打开。

## 7. 下一步学习建议

本讲只看了「门面」，没有进入任何类型的实现。接下来的学习路径按依赖关系推荐：

1. **先打基础（u1-l3）**：学习 [build.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs) 与 [no_atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/no_atomic.rs) 如何在编译期检测目标平台、发出 `crossbeam_no_atomic` 等 cfg——这是本讲反复提到的那些 `cfg` 的「来源」。
2. **进入 atomic 模块（u2 系列）**：从 [src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) 开始，按 u2-l1（公共 API）→ u2-l2（无锁路径）→ u2-l3（全局锁回退）的顺序精读 `AtomicCell`。
3. **进入 sync 模块（u3 系列）**：精读 `Parker`、`WaitGroup`、`ShardedLock`，并理解 `OnceLock` 如何被 `ShardedLock` 使用。
4. **作用域线程（u4 系列）**：`thread::scope` 依赖 `WaitGroup`，放在 u3 之后学习。

阅读建议：边读边对照本讲的「公开类型总表」，每打开一个实现文件，先确认它在表里的位置（属于哪个 feature、是公开还是内部），再进入实现细节——这能避免在 no_std / loom / 目标平台等条件上迷失方向。
