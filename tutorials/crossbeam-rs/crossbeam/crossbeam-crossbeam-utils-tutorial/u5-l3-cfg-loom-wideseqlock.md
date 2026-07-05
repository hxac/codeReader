# 跨平台 cfg、loom 抽象与宽 SeqLock

## 1. 本讲目标

本讲是专家层（advanced）的第三篇，目标不再是「学习某个并发原语怎么用」，而是回答三个「工程化」问题：

1. **可测试性**：crossbeam-utils 用了什么手段，让同一份并发源码既能在真实多线程下跑，又能在 `loom` 模型检查器下穷举线程交错？——`primitive` 抽象层。
2. **可移植性**：面对「有的目标平台根本没有原子指令」「有的目标开了 sanitizer 不允许内联汇编」这些差异，crate 如何在**编译期**就选好 `AtomicCell` 的实现路径？——`build.rs` 发射的三条 `cfg`。
3. **正确性边界**：在 16/32 位指针宽度的「窄架构」上，`SeqLock` 的版本印戳（stamp）会过快回绕（wrap），带来读到撕裂值的风险。crate 如何用「宽计数」对策来化解？——`seq_lock_wide.rs`。

学完后你应该能够：读懂 `primitive` 模块在 `crossbeam_loom` 下与标准库之间的二选一；解释 `crossbeam_no_atomic` / `crossbeam_sanitize_thread` / `crossbeam_atomic_cell_force_fallback` 三条 `cfg` 各自由谁、在何时发射，又被谁消费；并用数学说明窄架构下 stamp 的 wrap 风险，以及双计数器对策把回绕周期放大了多少。

## 2. 前置知识

- **`cfg` 属性**：Rust 的条件编译。`#[cfg(条件)]` 标注的项只在条件成立时才参与编译。`cfg!()` 宏则把条件求值成运行期布尔量。
- **build script（`build.rs`）**：Cargo 在编译 crate **之前**先编译并运行的小程序，通过 `cargo:rustc-cfg=NAME` 指令向 crate 注入 `cfg`，从而让源码能「按目标平台裁剪自己」。
- **loom**：Tokio 团队出品的并发**模型检查器**。它用自己的原子类型替换标准库原子类型，记录每一次内存访问，从而可以**穷举**线程交错，发现数据竞争。代价是只能跑极小的模型程序。
- **sanitizer**：编译器附带的运行期检查工具。本讲关心的是 `thread` sanitizer（TSan，检测数据竞争）。
- **指针宽度（`target_pointer_width`）**：`usize` 的位数，常见为 64，嵌入式与老架构可能是 32 甚至 16。`AtomicUsize` 的有效位数随之变化。
- **整数回绕（wrap-around）**：无符号整数运算溢出时取模回绕。本讲关心的是版本计数器加到上限后「回到起点」，导致新旧版本号撞车。
- **SeqLock 印戳机制**：本讲假定你已读过 [u2-l3](u2-l3-atomiccell-global-lock-seqlock.md)，知道 `AtomicCell` 回退路径用单个 `AtomicUsize` 同时编码「锁位（LSB）+ 版本号」，每完成一次写让版本号 +2，读者用「读前读后印戳相等」来判断读到的快照是否一致。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83) | 定义内部 `primitive` 抽象层：在 `crossbeam_loom` 下重导出 `loom::sync::*`，否则重导出标准库，供全 crate 统一引用。 |
| [build.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L18-L50) | 编译期读 `TARGET` / `CARGO_CFG_SANITIZE`，发射 `crossbeam_no_atomic` / `crossbeam_sanitize_thread` / `crossbeam_atomic_cell_force_fallback` 三条 `cfg`。 |
| [no_atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/no_atomic.rs#L4-L13) | 一份「不支持原子操作的 target triple 黑名单」，被 `build.rs` `include!` 进来用于查表。 |
| [src/atomic/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L6-L19) | 用 `cfg_attr(path = ...)` 在 16/32 位指针宽度下把 `seq_lock` 模块的源文件换成 `seq_lock_wide.rs`。 |
| [src/atomic/seq_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L9-L15) | 标准 `SeqLock`：单个 `AtomicUsize` 当印戳，64 位指针宽度的默认实现。 |
| [src/atomic/seq_lock_wide.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L12-L21) | 宽 `SeqLock`：拆成 `state_hi` / `state_lo` 两个 `AtomicUsize`，用于 16/32 位指针宽度，防印戳过快回绕。 |
| [src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L348-L373) | `atomic!` 宏在此消费 `miri` / `crossbeam_loom` / `crossbeam_atomic_cell_force_fallback`，决定是否删去无锁候选、强制走全局锁回退。 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L34-L46) | 声明 `atomic` feature 依赖 `atomic-maybe-uninit`；声明 loom 为 `cfg(crossbeam_loom)` 下的可选依赖。 |

---

## 4. 核心概念与源码讲解

### 4.1 primitive 抽象层与 loom 分支

#### 4.1.1 概念说明

并发代码的正确性极难用普通单元测试保证：一次跑通了，可能只是「这次恰好没踩到那个交错」。`loom` 用一种激进的办法解决这个问题——它**不让你的代码用真正的 CPU 原子指令**，而是替换成 `loom` 自己的原子类型。这些类型每次被访问，都会把这次访问记录进一棵「执行图」，然后 `loom` 反复回放、穷举所有可能的线程交错，只要某一种交错下出现数据竞争，`loom` 就报错。

要做到「同一份源码既能跑真线程、又能跑 loom」，最朴素的办法是在每一处原子操作上写 `#[cfg(crossbeam_loom)]` 二选一。但这会让业务代码遍布条件编译，可读性极差。crossbeam-utils 的做法是引入一层**内部抽象** `mod primitive`：把「原子类型」「`Mutex`/`Condvar`/`Arc`」「`spin_loop` 提示」这些可替换的设施统一收口到 `crate::primitive::*` 路径下，全 crate 只引用这一层。于是一处 `cfg` 切换，全部跟着切。

> 小知识：为什么是 `loom` 而不是 `miri`？两者互补——`miri` 按真实内存模型解释单次执行，能抓「这次执行里的 UB」；`loom` 则穷举交错，能抓「某种你测不到的交错下的竞争」。本讲聚焦 `loom`，`miri` 在 [u5-l4](u5-l4-testing-and-benchmarks.md) 详述。

#### 4.1.2 核心流程

`primitive` 模块有两个互斥的 `cfg` 版本，编译期只活一个：

```text
                   ┌── #![cfg(crossbeam_loom)]  ──▶ 重导出 loom::sync::*
mod primitive  ────┤
                   └── #![cfg(not(crossbeam_loom))] ──▶ 重导出 core/std 的对应类型
```

loom 分支提供的「原子类型」其实是 `loom::sync::atomic::*`，`Mutex`/`Condvar`/`Arc` 是 `loom::sync::*`，`spin_loop` 是 `loom::hint::spin_loop`。非 loom 分支则重导出 `core::sync::atomic`、`std::sync::{Mutex, Condvar}`、`alloc::sync::Arc`、`core::hint::spin_loop`。

调用方（如 `parker.rs`、`wait_group.rs`、`atomic_cell.rs`、`consume.rs`、`backoff.rs`）一律写 `use crate::primitive::sync::{...}`，对底下是 loom 还是标准库**完全无感**。这样：跑 `cargo test` 时用真原子、跑 `RUSTFLAGS="--cfg crossbeam_loom"` 时自动换成 loom 模型，零业务代码改动。

需要特别说明一个**覆盖盲区**：`SeqLock` 与 `AtomicCell` 在 loom 下**不可用**。原因是 `AtomicCell` 依赖 `#[repr(transparent)]` 把内部布局「按比特重解释」成原生原子类型，而 loom 的原子类型**内存表示与底层类型不同**，无法 transmute。因此这两个模块干脆被 `#[cfg(not(crossbeam_loom))]` 关掉（见 `atomic/mod.rs`）。也就是说，loom 能覆盖 Parker / WaitGroup / Backoff / consume，但**覆盖不到** `AtomicCell`——这是它已知的、被代码注释承认的限制。

#### 4.1.3 源码精读

`primitive` 的两个版本定义在 [src/lib.rs:47-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83)：loom 版重导出 `loom` 的原子与同步原语，非 loom 版重导出标准库。注意 loom 版还做了一处**替身**处理。

[src/lib.rs:60-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L60-L65) 把 `loom::sync::atomic::fence` 别名为 `compiler_fence` 导出。原因写在注释里：**loom 至今不支持 `compiler_fence`**（追踪 issue tokio-rs/loom#117）。`compiler_fence` 只约束编译器重排、不插入硬件栅栏；loom 没法建模它，于是这里用更强的全功能 `fence` 顶替。代价是：`fence` 比 `compiler_fence` 严格，可能「多报」一些实际不会发生的竞争——这是务实折中。

`SeqLock` 不能用 loom 的原子类型，所以它**绕开** `primitive`，直接 `use core::sync::atomic`，并用 `#[cfg(not(crossbeam_loom))]` 把整个模块关掉（见 [src/atomic/mod.rs:6-7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L6-L7)）。换句话说：`primitive` 抽象服务于「需要在 loom 下也跑」的代码；`SeqLock` 既然永远不在 loom 下编译，也就不必走抽象层。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `crossbeam_loom` 这个 `cfg` 是 `primitive` 抽象层的「总开关」，并观察它会让哪些模块消失。

**操作步骤**（源码阅读型，可在本机或仅阅读理解）：

1. 在 `crossbeam-utils` 目录下，普通编译一次，确认 `AtomicCell` 可用：

   ```bash
   cargo build --features atomic
   ```

2. 设置 `crossbeam_loom` 再编译，观察报错位置：

   ```bash
   RUSTFLAGS="--cfg crossbeam_loom" cargo build --features atomic,loom
   ```

3. 试着在 `examples/` 或一个临时测试里写 `use crossbeam_utils::atomic::AtomicCell;`，分别在两种配置下编译。

**需要观察的现象**：

- 在 `crossbeam_loom` 配置下，`crossbeam_utils::atomic::AtomicCell` **不存在**（模块被 `#[cfg(not(crossbeam_loom))]` 关掉），引用会报「cannot find type `AtomicCell`」。
- 同样，`crossbeam_utils::sync::ShardedLock` 与 `crossbeam_utils::thread::scope` 也会消失（见 [src/lib.rs:98-100](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L98-L100)）。
- 而 `crossbeam_utils::sync::Parker`、`WaitGroup`、`Backoff` 仍然可用——因为它们走 `primitive` 抽象、能在 loom 下存活。

**预期结果**：你将直观看到「loom 覆盖盲区」——哪些类型为了能被 loom 测试而牺牲了（`AtomicCell`/`ShardedLock`/`scope` 反而不能被 loom 测），哪些类型设计成 loom 友好（`Parker`/`WaitGroup`/`Backoff`/`consume`）。

> 待本地验证：具体编译器报错措辞与能否成功链接 `loom` 取决于本机 toolchain；若不便运行，至少完成「在源码里用 `#[cfg(not(crossbeam_loom))]` 过一遍 `src/lib.rs` 与 `src/atomic/mod.rs`，列出哪些 `pub` 项在 loom 下消失」的阅读任务。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `atomic_cell.rs` 写 `use crate::primitive::sync::atomic::{self, Ordering}`，而 `seq_lock.rs` 却直接写 `use core::sync::atomic`？

> **参考答案**：`atomic_cell.rs` 与 loom 共用同一套排序语义，走 `primitive` 以保持一致性；但 `AtomicCell` 整体被 `#[cfg(not(crossbeam_loom))]` 关掉，实际上 loom 下它根本不参与编译。`seq_lock.rs` 同样只在非 loom 下编译，且它需要 `read_volatile` 等底层操作，直接用 `core` 更直白——两条路殊途同归：都保证了「loom 下不存在」。

**练习 2**：如果某天 loom 修复了对 `compiler_fence` 的支持，`primitive` 的 loom 分支要怎么改？

> **参考答案**：把 [src/lib.rs:65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L65) 那行 `pub(crate) use self::fence as compiler_fence;` 替换为直接 `pub(crate) use loom::sync::atomic::compiler_fence;`（前提是 loom 暴露了它），从而去掉「用更强的 `fence` 顶替」这个会多报竞争的折中。

---

### 4.2 build.rs 发射的 cfg

#### 4.2.1 概念说明

`AtomicCell` 的某次操作（如 `load`）到底走「单条 CPU 原子指令」还是「全局 SeqLock 回退」？这个选择不是运行期 `if`，而是**编译期裁剪**。选择的依据有两个维度：

1. **目标平台有没有原子指令**：MSP430、ARMv4T 这类老/嵌入式目标根本没有原子操作，`AtomicUsize` 都不存在。
2. **目标平台有没有内联汇编**：开 sanitizer（尤其是 TSan）时，`AtomicCell` 用的某些内联汇编优化路径会干扰检测器，必须强制走更「朴素」的全局锁回退。

这两个维度都不能写进 `Cargo` 的 `features`（feature 不随 target 变），而要靠 `build.rs` 在编译期读 `TARGET` 环境变量、查表后用 `cargo:rustc-cfg=NAME` 发 `cfg` 出去，让 `src` 里的 `#[cfg(NAME)]` 据此裁剪。

本讲涉及三条由 `build.rs` 发射的 `cfg`：

| cfg 名 | 含义 | 是否公开 API |
| --- | --- | --- |
| `crossbeam_no_atomic` | 目标平台**不支持任何原子操作** | 是（build.rs 头部注释声明公开，但 unstable） |
| `crossbeam_sanitize_thread` | 目标启用了 **thread sanitizer** | 否（内部） |
| `crossbeam_atomic_cell_force_fallback` | **任意 sanitizer** 激活，强制 `AtomicCell` 走全局锁回退 | 否（内部） |

#### 4.2.2 核心流程

`build.rs` 的 `main()` 做三件事：

```text
1. 读 TARGET 环境变量；若是自定义 linux 目标，把 vendor 规范化成 "unknown"
2. 若 TARGET ∈ NO_ATOMIC 黑名单 ──▶ 发 crossbeam_no_atomic
3. 读 CARGO_CFG_SANITIZE：
       含 "thread"            ──▶ 发 crossbeam_sanitize_thread
       （只要 sanitize 非空） ──▶ 发 crossbeam_atomic_cell_force_fallback
```

几个设计要点：

- **用 `no_` 否定式命名**（注释 [build.rs:36-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L36-L38) 明说）：当 build script **没跑**（非 cargo 构建系统）时，没有任何 `cfg` 被发射，crate 就**乐观地默认「支持原子」**继续编译。这是为了兼容性——宁可少检测、也不要因为 build script 缺席就把 crate 编译成「什么原子都不支持」的废铁。只有黑名单命中时才显式关掉。
- **`force_fallback` 对所有 sanitizer 一视同仁**：不止 TSan，任何 sanitizer 激活都会触发它。原因是 sanitizer 与内联汇编路径常常不兼容，而全局锁回退路径是纯标准库原子、与 sanitizer 完美兼容。
- **消费侧在 `atomic!` 宏**：见 4.2.3，`force_fallback` 直接删去宏里所有「无锁候选」，让任何 `AtomicCell` 操作都只能落到全局锁回退。

#### 4.2.3 源码精读

[build.rs:18-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L18-L50) 是 `main()` 主体。它先 `include!("no_atomic.rs")` 把黑名单常量 `NO_ATOMIC` 引入（[no_atomic.rs:4-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/no_atomic.rs#L4-L13)），再用 `NO_ATOMIC.contains(&&*target)` 判断当前 target 是否在册。

[build.rs:39-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L39-L49) 是发射逻辑：黑名单命中发 `crossbeam_no_atomic`；`CARGO_CFG_SANITIZE` 含 `"thread"` 发 `crossbeam_sanitize_thread`；只要 sanitizer 非空就额外发 `crossbeam_atomic_cell_force_fallback`。注意 [build.rs:20-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L20-L22) 用 `rustc-check-cfg` 预先向编译器声明这三条 `cfg` 合法，避免触发「unknown cfg」lint。

这三条 `cfg` 被 `atomic_cell.rs` 的 `atomic!` 宏消费。[src/atomic/atomic_cell.rs:348-373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L348-L373) 是关键：

```rust
// Always use fallback for now on environments that do not support inline assembly.
#[cfg(not(any(
    miri,
    crossbeam_loom,
    crossbeam_atomic_cell_force_fallback,
)))]
atomic_maybe_uninit::cfg_has_atomic_cas! {
    // ……逐宽度尝试 AtomicMaybeUninit<u8/u16/u32/u64/u128> 的无锁候选……
}
break $fallback_op;
```

这段宏逻辑是：只要 `miri`、`crossbeam_loom`、`crossbeam_atomic_cell_force_fallback` 三者有其一成立，**整段「按宽度试无锁候选」的代码就被 `cfg` 删掉**，于是 `break $fallback_op` 直接执行——也就是全局 SeqLock 回退。这就是「sanitizer 强制回退」在源码里的落点。

此外，`atomic` feature 本身还引入了 [Cargo.toml:36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L36) 的 `atomic-maybe-uninit` 依赖（[Cargo.toml:38-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/Cargo.toml#L38-L39)）。它提供 `cfg_has_atomic_8/16/32/64/128!` 这类宏，用来在编译期判断「当前 target 有没有 N 位原子」，从而决定 `AtomicMaybeUninit<uN>` 候选是否可见——这是宏里 `#[cfg(not(...))]` 之外、第二层「按 target 原子宽度」的裁剪。

#### 4.2.4 代码实践

**实践目标**：直观看到「同一份 `AtomicCell` 源码，在不同 target / sanitizer 下被裁剪成不同实现路径」。

**操作步骤**（建议用 `cargo rustc -- --pretty=expanded` 不便时退化为「读 `build.rs` 输出 + grep 源码」）：

1. 为一个无原子 target 交叉编译，观察 `crossbeam_no_atomic` 是否被发射：

   ```bash
   cargo build --target msp430-none-elf --features atomic 2>&1 | head
   ```

   （若没有该 target 已安装，可改为 `cargo build --target thumbv6m-none-eabi`，并阅读 `no_atomic.rs` 确认黑名单成员。）

2. 在 sanitizer 下编译，观察 `force_fallback`：

   ```bash
   RUSTFLAGS="-Zsanitizer=thread" cargo +nightly build --features atomic 2>&1 | head
   ```

3. 不便运行时，做静态阅读：在仓库里用 `grep` 找 `crossbeam_atomic_cell_force_fallback` 的所有出现，画出「build.rs 发射 → atomic! 宏消费」的数据流。

**需要观察的现象**：

- 对黑名单 target，整个 `atomic` 模块会因为 `target_has_atomic="ptr"` 不成立而被裁掉（`AtomicCell` 都不存在）。
- 对 sanitizer 构建，`AtomicCell::<usize>::is_lock_free()` 在文档测试里被 [src/atomic/atomic_cell.rs:138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L138) 那行 `cfg!(any(miri, crossbeam_loom, crossbeam_atomic_cell_force_fallback))` 提前 `return`，绕过断言——因为 sanitizer 下所有操作都被强制走全局锁，`is_lock_free` 会返回 `false`，与文档里的 `true` 断言冲突。

**预期结果**：理解 build script 的输出（`cfg`）如何穿透到源码的 `#[cfg]`，最终改变 `AtomicCell` 的实现路径——这就是「编译期裁剪」的完整闭环。

> 待本地验证：交叉编译与 sanitizer 构建需要对应 target/nightly toolchain；若环境不具备，请至少完成步骤 3 的静态数据流阅读。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `build.rs` 用 `crossbeam_no_atomic`（否定式）而不是 `crossbeam_has_atomic`（肯定式）？

> **参考答案**：为了让「build script 没跑」时行为正确。非 cargo 构建系统不跑 build script，也就不会发射任何 `cfg`。用否定式，缺省（无 `cfg`）意味着「支持原子」，crate 照常工作；若用肯定式，缺省会变成「不支持原子」，把所有原子代码裁光，crate 直接不可用。否定式把「不确定」安全地降级为「乐观支持」。

**练习 2**：`crossbeam_sanitize_thread` 和 `crossbeam_atomic_cell_force_fallback` 的关系是什么？为什么前者是后者的子集？

> **参考答案**：`build.rs` 里只要 `CARGO_CFG_SANITIZE` 含 `"thread"` 就发 `crossbeam_sanitize_thread`；而只要 `sanitize` 非空（任意 sanitizer）就发 `force_fallback`。所以 `force_fallback` 涵盖更广（任何 sanitizer 都触发回退），`crossbeam_sanitize_thread` 是更窄的标记，供需要精确区分「是不是 TSan」的代码使用。两者是「宽松触发」与「精确标记」的分工。

---

### 4.3 seq_lock_wide 宽计数

#### 4.3.1 概念说明

回到 [u2-l3](u2-l3-atomiccell-global-lock-seqlock.md) 讲过的 SeqLock：读者用「读前拿一个 stamp、读完再比对 stamp 是否变化」来判断读到的快照是否一致。stamp 由写者每完成一次写就 +2 来推进。问题是——**stamp 是有限的整数**，加到上限会回绕（wrap）回 0，再继续加。

设想一个极端：读者在 `optimistic_read` 时拿到 stamp = `s`，开始读；这期间写者疯狂写入，恰好完成了「半数个 stamp 周期」次写，stamp 又回到了 `s`；读者读完做 `validate_read`，发现 stamp 没变，误判「读到的是一致快照」——但它读到的其实是写者写到一半的撕裂值。这就是 **stamp wrap 风险**。

在 64 位 `usize` 上，stamp 周期约为 \(2^{63}\)，现实里永远跑不满，风险可忽略。但在 16/32 位指针宽度的窄架构上（`usize` 只有 16 或 32 位），周期分别只有 \(2^{15}=32768\) 或 \(2^{31}\approx 2.1\times10^{9}\)，前者几乎立刻就会撞上 wrap。对策：**把单个计数器拆成高低两段**，让完整回绕周期平方级放大。

#### 4.3.2 核心流程与数学

**标准 SeqLock**（`seq_lock.rs`，64 位默认）：单个 `AtomicUsize`，LSB 是锁位，其余位是 stamp，每次写 `wrapping_add(2)`。设 `usize` 为 N 位，则 stamp 的取值数（也即「写多少次会回到同一个 stamp」）为：

\[ W_{\text{standard}} = \frac{2^{N}}{2} = 2^{N-1} \]

- N=64：\(W = 2^{63} \approx 9.2\times10^{18}\)，永不回绕。
- N=32：\(W = 2^{31} \approx 2.1\times10^{9}\)，紧循环下数秒可能撞上。
- N=16：\(W = 2^{15} = 32768\)，几乎立刻回绕，不可接受。

读者误判的**充分条件**：在一次读的 `optimistic_read` 与 `validate_read` 之间，恰好完成 \(k\cdot W_{\text{standard}}\)（k 为正整数）次写。在窄架构上，k=1 就触手可及。

**宽 SeqLock**（`seq_lock_wide.rs`，16/32 位启用）：把 stamp 拆成两个 N 位 `AtomicUsize`：

- `state_lo`：与标准版同构（LSB 锁位 + 其余位），每次写 `wrapping_add(2)`；当它回绕到 0 时，触发 `state_hi` 自增 1。
- `state_hi`：完整的 N 位计数器，记录 `state_lo` 回绕了多少次。

读者的 stamp 变成元组 `(hi, lo)`，`validate_read` 同时校验两段。完整回绕要求 `state_lo` 与 `state_hi` **同时**回到原值，周期为：

\[ W_{\text{wide}} = \underbrace{2^{N-1}}_{\text{state_lo 一轮}} \times \underbrace{2^{N}}_{\text{state_hi 满量程}} = 2^{2N-1} \]

- N=32：\(W = 2^{63}\)，安全。
- N=16：\(W = 2^{31} \approx 2.1\times10^{9}\)——源码注释 [src/atomic/mod.rs:11-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L11-L13) 判断：在 16 位这种「原始硬件」上写操作不会那么频繁，这个量级「mostly okay」。

选择哪个实现，由 `mod.rs` 里一处优雅的 `cfg_attr(path = ...)` 完成（见 4.3.3）。

#### 4.3.3 源码精读

**切换器**：[src/atomic/mod.rs:6-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L6-L19)。`mod seq_lock;` 的源文件路径本应是默认的 `seq_lock.rs`，但被一层 `cfg_attr` 改写：

```rust
#[cfg_attr(
    any(target_pointer_width = "16", target_pointer_width = "32"),
    path = "seq_lock_wide.rs"
)]
mod seq_lock;
```

含义：模块名恒为 `seq_lock`（消费方 `atomic_cell.rs` 写 `use super::seq_lock::SeqLock;` 即可），但当指针宽度为 16 或 32 时，模块的**源文件**换成 `seq_lock_wide.rs`。这是 Rust 的惯用法——同名模块、不同实现文件，靠 `cfg` 选其一。注意两个文件对外暴露的 API（`SeqLock`、`optimistic_read` / `validate_read` / `write` / `abort`）完全一致，只是 `optimistic_read` 的 stamp 类型一个是 `usize`、一个是 `(usize, usize)`——但因为 stamp 只在 `atomic_cell.rs` 内部传递、从不跨文件，这点差异不影响调用方。

**标准版结构**：[src/atomic/seq_lock.rs:9-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L9-L15)——单个 `state: AtomicUsize`。读路径 [src/atomic/seq_lock.rs:28-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L28-L41) 中，`optimistic_read` 返回当前 state、`validate_read` 直接比对 `state == stamp`。Drop 时 [src/atomic/seq_lock.rs:85-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L85-L93) 用 `state.wrapping_add(2)` 解锁并推进版本号——wrap 就发生在这条 `wrapping_add` 上。

**宽版结构**：[src/atomic/seq_lock_wide.rs:12-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L12-L21)——拆成 `state_hi` / `state_lo` 两个 `AtomicUsize`。核心差异在两处：

1. **`optimistic_read` 返回元组**：[src/atomic/seq_lock_wide.rs:35-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L35-L49) 用 acquire 序同时读 hi 与 lo，注释详细论证了「lo 为偶数 ⇒ 临界区内所有写对当前可见」。
2. **`validate_read` 处理 lo 单独回绕**：[src/atomic/seq_lock_wide.rs:56-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L56-L77)。逻辑很精细——如果只读 `state_lo` 且发现它等于 `stamp.1`，这有两种可能：①没有新写发生（正常）；②`state_lo` 回绕了一圈恰好相等。靠再读 `state_hi` 来区分：若 hi 也相等，那就是「hi 和 lo 都回绕」的极端情形，此时 `validate_read` **放弃判定**（保守地认为读无效），最终判定式 `(state_hi, state_lo) == stamp`。
3. **Drop 时联动进位**：[src/atomic/seq_lock_wide.rs:120-140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L120-L140)。先算 `state_lo = self.state_lo.wrapping_add(2)`；若结果为 0（说明 lo 回绕），就把 `state_hi` 自增 1（release 序）；最后才 store 新的 lo。这正是把单计数器的 `wrapping_add(2)` 拆成「低位自增 + 逢 0 进位」的实现。

对比两文件，**状态类型**（`AtomicUsize` vs 双 `AtomicUsize`）、**stamp 类型**（`usize` vs `(usize, usize)`）、**`validate_read` 的判定复杂度**（等值 vs 处理 lo 回绕）、**Drop 的解锁**（单 store vs 逢 0 进位双 store）——这些都是为窄架构「续命」付出的代价。

#### 4.3.4 代码实践

**实践目标**：对照阅读两个文件，亲手找出它们在「状态类型」「stamp 运算」「validate 判定」上的全部差异，并解释窄架构为何必须用宽版本。

**操作步骤**（源码阅读型实践）：

1. 同时打开 [src/atomic/seq_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs) 与 [src/atomic/seq_lock_wide.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs)。
2. 填一张对照表，逐行比较：结构体字段、`new`、`optimistic_read`（返回类型 + 读的序）、`validate_read`（判定逻辑 + 处理 wrap）、`write`（抢哪个字段）、`abort`、`Drop`。
3. 结合 [src/atomic/mod.rs:15-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L15-L18) 的 `cfg_attr(path = ...)`，回答：为什么 16/32 位指针宽度「必须」用宽版本？用 4.3.2 的公式量化回答。

**需要观察的现象（对照表要点）**：

| 维度 | seq_lock.rs（标准） | seq_lock_wide.rs（宽） |
| --- | --- | --- |
| 字段 | `state: AtomicUsize` | `state_hi` + `state_lo` 两个 `AtomicUsize` |
| stamp 类型 | `usize` | `(usize, usize)` |
| `optimistic_read` 读序 | Acquire（单个 load） | Acquire（先 hi 后 lo 两个 load） |
| `validate_read` 判定 | `state == stamp`（一次 load） | 两次 load + 处理 lo 回绕的注释论证 |
| `write` 抢锁 | `state.swap(1, Acquire)` | `state_lo.swap(1, Acquire)`（hi 不参与抢锁） |
| `Drop` 解锁 | `state.store(prev+2)` | `state_lo+2`，逢 0 进位 `state_hi` |

**预期结果**：你能清晰说出「窄架构上单计数器周期 \(2^{N-1}\) 太短、宽版本周期 \(2^{2N-1}\) 足够长」，并能指出代价是「每次写多一次条件 store、每次读多一次 load + 更复杂的 validate」。

**完整代码实践（可选运行）**：两个文件末尾各有一个 `#[cfg(test)] mod tests` 的 `test_abort` 单元测试（[seq_lock.rs:99-110](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L99-L110) 与 [seq_lock_wide.rs:146-157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L146-L157)），断言「aborted write 不更新 stamp」。运行：

```bash
cargo test --features atomic seq_lock
```

可验证当前 target（64 位）编译的是标准版。若想验证宽版，需要交叉编译到 32 位 target（如 `i686-unknown-linux-gnu`）后跑测试——

> 待本地验证：32 位 target 测试需要安装对应 target 与能跑 32 位二进制的环境；若不具备，阅读两个 `test_abort` 实现并确认它们结构一致即可。

#### 4.3.5 小练习与答案

**练习 1**：在宽版本里，为什么 `write()` 只抢 `state_lo`，完全不碰 `state_hi`？

> **参考答案**：锁位在 `state_lo` 的 LSB。抢锁只需把 `state_lo` 原子地置 1，写者之间、写者与乐观读者之间的互斥都靠 `state_lo` 即可。`state_hi` 只是「`state_lo` 回绕了多少次」的辅助计数，写者持有锁期间它的值不会变化（其他写者进不来），所以抢锁阶段无需触碰它，进位只在 `Drop` 里发生。

**练习 2**：宽版本的 `validate_read` 注释说「除 `state_hi` 和 `state_lo` 都回绕的情况外，判定有效」。请用周期公式说明这个「放弃判定」有多罕见。

> **参考答案**：完整回绕周期是 \(W_{\text{wide}} = 2^{2N-1}\)。对 N=32 是 \(2^{63}\)，对 N=16 是 \(2^{31}\)。即在一次读的时间窗内要发生 \(2^{2N-1}\) 次完整写才会撞上「双回绕」，在窄架构的原始硬件上几乎不可能，故源码选择「保守放弃判定、让读重试」而非「冒险相信结果」。

**练习 3**：如果未来要在 `AtomicCell` 内部复用 loom 测试，宽 SeqLock 的双计数设计会不会带来额外麻烦？

> **参考答案**：会。宽版本靠「lo 回绕触发 hi 进位」的精细时序来保证正确性，而 loom 穷举交错正是要检验这种时序在所有重排下都成立。但更根本的障碍是 4.1 节说的：`AtomicCell` 整体因 `repr(transparent)` 与 loom 原子类型内存表示不兼容而被 `#[cfg(not(crossbeam_loom))]` 关闭，所以宽 SeqLock 目前根本进不了 loom 测试——这是 `atomic/mod.rs` 里那条注释「TODO: latest loom supports fences, so fallback using seqlock may be available」指向的未来工作。

---

## 5. 综合实践

**任务**：给定一次 `AtomicCell::<[u8; 9]>::load()` 调用（`[u8; 9]` 无法 transmute 成任何原生原子类型，必走全局锁回退），请画出它在三种「编译配置」下分别走哪条路径、用到哪个 SeqLock 实现。

三种配置：

- **A**：`x86_64-unknown-linux-gnu`，无 sanitizer，无 loom。
- **B**：`x86_64-unknown-linux-gnu`，开启 `-Zsanitizer=thread`。
- **C**：某个 32 位嵌入式 target（指针宽度 32），无 sanitizer，无 loom。

要求你为每种配置回答：

1. `build.rs` 发射了哪些 `cfg`？
2. `atomic!` 宏里「按宽度试无锁候选」那段代码是否被保留？（参考 [atomic_cell.rs:348-353](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L348-L353)）
3. `[u8; 9]` 最终落到 `atomic_load` 的哪个分支？（提示：无论候选是否保留，它都过不了 `can_transmute`，所以一定走 fallback。）
4. fallback 用到的是 `seq_lock.rs` 还是 `seq_lock_wide.rs`？stamp 是 `usize` 还是 `(usize, usize)`？

**参考结论**：

- **A**：不发任何 `cfg`；候选段保留但对 `[u8;9]` 无效 → 走 fallback；指针宽度 64 → 用标准 `seq_lock.rs`，stamp 为 `usize`。
- **B**：发 `crossbeam_sanitize_thread` 与 `crossbeam_atomic_cell_force_fallback`；候选段被 `#[cfg(not(any(...)))]` **删掉** → 直接走 fallback；仍用标准 `seq_lock.rs`。
- **C**：不发 sanitizer 类 `cfg`，候选段保留但无效 → 走 fallback；指针宽度 32 → 经 `cfg_attr(path=...)` 用 **`seq_lock_wide.rs`**，stamp 为 `(usize, usize)`。

这个练习把本讲三个最小模块——`primitive`/loom 抽象、`build.rs` 的 cfg、宽 SeqLock——串成一条「同一段源码、三种编译产物」的完整链路，帮助你建立「crossbeam-utils 的可移植性与可测试性是如何同时被设计出来」的整体观。

## 6. 本讲小结

- crossbeam-utils 用内部 `mod primitive` 抽象层统一收口原子/Mutex/Condvar/Arc/提示，靠 `#[cfg(crossbeam_loom)]` 在 `loom` 与标准库之间二选一，使并发原语可被 loom 穷举交错测试。
- loom 抽象有覆盖盲区：`AtomicCell`/`ShardedLock`/`thread::scope` 因内存表示或 `std::sync::Once` 不可建模，在 loom 下被整体关闭；`loom` 不支持 `compiler_fence`，临时用更强的 `fence` 顶替。
- `build.rs` 在编译期读 `TARGET`/`CARGO_CFG_SANITIZE`，发射三条 `cfg`：`crossbeam_no_atomic`（无原子 target，公开但 unstable）、`crossbeam_sanitize_thread`（TSan）、`crossbeam_atomic_cell_force_fallback`（任意 sanitizer 强制 `AtomicCell` 走全局锁回退）。
- 否定式命名（`no_atomic`）让「build script 没跑」时乐观默认「支持原子」，兼容非 cargo 构建系统；三条 `cfg` 被 `atomic!` 宏消费，直接删去无锁候选段。
- 窄架构（16/32 位指针宽度）上 SeqLock 的单计数器 stamp 周期 \(2^{N-1}\) 过短，有回绕导致读到撕裂值的风险；宽 SeqLock 拆成 `state_hi`/`state_lo` 双 `AtomicUsize`，周期放大到 \(2^{2N-1}\)，并在 `validate_read` 与 `Drop` 里精细处理「lo 回绕进位」。
- `mod.rs` 用 `#[cfg_attr(target_pointer_width="16"|"32", path="seq_lock_wide.rs")] mod seq_lock;` 在编译期切换同名模块的源文件，对调用方完全透明。

## 7. 下一步学习建议

- 阅读 [u5-l4 并发测试策略与基准](u5-l4-testing-and-benchmarks.md)，看 `tests/` 与 `benches/` 如何把本讲的 `loom`/`miri`/`TSan` 三套验证工具组织进实际测试矩阵，并理解 `benches/atomic_cell.rs` 如何度量「无锁路径 vs 全局锁回退」的吞吐差距。
- 顺着 `atomic!` 宏再读一遍 [u5-l2 算术运算的宏生成](u5-l2-atomiccell-arithmetic-macros.md)，体会「编译期 cfg 裁剪 + 运行期 const fn 折叠」这套混合分发如何与 build.rs 发射的 cfg 协同。
- 若想深入 loom 本身，建议阅读 loom 实现并对照 [src/lib.rs:47-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83) 的 `primitive` 抽象，理解「为什么 loom 的原子类型有不同内存表示」这一根本限制。
- 想挑战自己的话：尝试回答 u5-l3 综合实践里的「配置 C 在 loom 下会怎样」——答案是 32 位 target 上的 `AtomicCell` 同样会被 loom 关闭，因为盲区与指针宽度无关，而源于 `repr(transparent)` 与 loom 原子表示的不兼容。
