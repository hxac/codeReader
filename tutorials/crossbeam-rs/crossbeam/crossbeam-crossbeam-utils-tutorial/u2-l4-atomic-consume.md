# AtomicConsume 与 consume 内存序

## 1. 本讲目标

本讲聚焦 `crossbeam-utils` 的 `atomic` 模块里一个「小而精」的类型族：`AtomicConsume` trait 与它的 `load_consume` 方法。学完后你应当能够：

- 说清楚 **consume 内存序**与 acquire 内存序的差别，以及为什么在弱内存模型（ARM/AArch64）上 consume 可能更快。
- 读懂 `consume.rs` 中那两个互斥的 `impl_consume!` 宏，理解编译期 `cfg` 如何在「`load(Relaxed)` + `compiler_fence(Acquire)`」与「`load(Acquire)`」之间二选一。
- 解释为什么这个「快速路径」只对 `arm`/`aarch64` 开放，而在 `miri`、`loom`、`crossbeam_sanitize_thread` 下一律退化为 `load(Acquire)`。
- 看懂 `impl_atomic!` 宏如何为一批整数原子类型批量实现 trait，以及为什么 `AtomicPtr<T>` 要单独手写。

本讲**不**讨论 `AtomicCell` 的无锁/锁回退路径（那是 u2-l2、u2-l3 的内容），也**不**讨论底层 SeqLock。`AtomicConsume` 面向的是 `core::sync::atomic` 里的**原生**原子类型，而非 `AtomicCell`。

## 2. 前置知识

### 2.1 happens-before 与 acquire/release

在并发编程里，我们用 **happens-before** 关系来判断「线程 A 的写是否对线程 B 可见」。最常用的两种内存序是：

- **Release（写端）**：一次 `store(Release)` 之前的所有读写，对...
- **Acquire（读端）**：...同一个变量的 `load(Acquire)` 之后的读写可见。

也就是：`store(Release)` 与后来读到该值的 `load(Acquire)` 之间建立一条 synchronizes-with 边，于是写线程在 store 之前的全部操作，都对读线程在 load 之后可见。代价是：在很多弱内存架构上，acquire 读需要插入（或隐式带上）一条内存屏障指令。

### 2.2 强内存模型 vs 弱内存模型

- **强内存模型（如 x86-64）**：CPU 本身就保证「load 之后跟着的读写在 load 完成之前不会被提前执行」，所以 acquire 读几乎零开销，普通 `load` 就近似带 acquire 语义。
- **弱内存模型（如 ARM、AArch64、PowerPC、MIPS）**：CPU 可以乱序执行访存指令，acquire 读通常需要显式屏障。

### 2.3 数据依赖与 dependency ordering

考虑经典代码：

```rust
// 线程 A：先写数据，再发布指针
data.store(42, Ordering::Relaxed);
ptr.store(&data, Ordering::Release);

// 线程 B：读指针，再解引用
let p = ptr.load(Ordering::???);
println!(*p); // 这一步「依赖于」p 的值
```

线程 B 解引用 `*p` 时，CPU **必须**先知道 `p` 的值才能去取那个地址的内容——这叫 **数据依赖（data dependency）**。很多弱架构（包括 ARM/AArch64）保证：存在数据依赖的两次访存不会被重排。也就是说，**只要读指针的值之后跟着一个依赖它的操作，硬件天然就保证了顺序**，不需要额外屏障。

**consume 内存序**正是利用这一点：它只对「依赖于本次 load 结果」的后续操作建立顺序关系，因此比 acquire 更轻。Linux 内核大量依赖这个性质（`READ_ONCE` + `smp_read_barrier_depends` 的演化）。

### 2.4 Rust 标准库里没有 `Ordering::Consume`

C11 有 `memory_order_consume`，但 Rust 的 `core::sync::atomic::Ordering` **故意没有** `Consume` 变体，只有 `Relaxed / Release / Acquire / AcqRel / SeqCst`。原因是 C11 的 consume 语义被证明很难在编译器里正确实现（编译器很难精确追踪「依赖」在优化后是否还存在），多数编译器干脆把 consume 提升成 acquire。相关长期讨论见 `consume.rs` 注释里引用的 [rust-lang/rust#62256](https://github.com/rust-lang/rust/issues/62256)。

`crossbeam-utils` 的 `AtomicConsume` 就是一个**手写的、尽力而为的 consume 替代品**：它不依赖语言层面的 `Consume`，而是用 `load(Relaxed)` + `compiler_fence(Acquire)` 在 ARM/AArch64 上手动拼出 consume 行为。

### 2.5 `compiler_fence` 与 `fence` 的区别

- `core::sync::atomic::fence(Acquire)`：同时约束**编译器**和 **CPU**，会发出硬件屏障指令。
- `core::sync::atomic::compiler_fence(Acquire)`：**只**约束编译器（禁止它把后面的读读写写到 fence 前面），**不**发出任何 CPU 指令。

这个差别是本讲快速路径成立的关键：在 ARM/AArch64 上，硬件已经为我们保住了数据依赖顺序，我们只需要 `compiler_fence` 阻止编译器把依赖优化掉即可。

> ⚠️ 注意：`consume.rs` 注释明确指出，在 PowerPC/MIPS 等架构上，LLVM 会把 `compiler_fence(Acquire)` 编译成**等同硬件 fence** 的指令（见注释里的 godbolt 链接）。所以这条「省 fence」的捷径，现实里只在 ARM/AArch64 上真正成立。

## 3. 本讲源码地图

本讲只涉及两个文件，都很短：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `src/atomic/consume.rs` | 112 | 定义 `AtomicConsume` trait、两个 `impl_consume!` 宏、`impl_atomic!` 宏，以及所有原生原子类型的 trait 实现 |
| `src/atomic/mod.rs` | 33 | `atomic` 模块门面；在本讲里只关心它如何导出 `AtomicConsume` |

另外需要记住的上下文（来自前置讲义 u1-l2/u1-l3）：

- 整个 `atomic` 模块由 `feature = "atomic"` 门控（见 `src/lib.rs:85-87`）。
- 与 `AtomicCell` 不同，`AtomicConsume` **没有** `target_has_atomic = "ptr"` 门控，也**没有** `not(crossbeam_loom)` 门控——所以在 loom 模型测试下它仍然可用（loom 路径有独立的 impl）。

## 4. 核心概念与源码讲解

### 4.1 AtomicConsume trait：consume 内存序的对外契约

#### 4.1.1 概念说明

`AtomicConsume` 是一个 trait，给所有**原生**原子类型（`AtomicUsize`、`AtomicPtr<T>` 等）补一个统一方法 `load_consume`，让调用者能用「consume 语义」读取，而不必关心当前架构是否真的能省 fence。

它的语义可以用一条数学关系刻画：若一次 `load_consume` 读到了某次 release store 写入的值 `v`，则对**所有数据依赖于 `v`** 的后续操作 `D`，都有

\[
\text{store}(\text{Release}) \;\xrightarrow{\text{sw}}\; \text{load\_consume} \;\xrightarrow{\text{dep}}\; D
\]

也就是 release→consume 建立 synchronizes-with 边后，**依赖链**上的操作 `D` 能看到 store 之前的写入。注意它**不保证**与 `v` 无依赖的操作的顺序——这正是它比 acquire 轻的地方，也是它「可能更快」的根源。

关键提醒：`load_consume` 的具体实现是分平台的（见 4.2），但**对外签名只有一个**，调用者无需感知差异。

#### 4.1.2 核心流程

`AtomicConsume` trait 的核心流程极简：

1. 定义关联类型 `Val`（`load_consume` 的返回类型，例如 `AtomicUsize` 对应 `usize`）。
2. 定义唯一方法 `load_consume(&self) -> Self::Val`。
3. 由 `impl_atomic!` 宏为每种原生原子类型实现该 trait（4.3 详述）。

调用方流程：拿到一个原生原子引用 → 调 `.load_consume()` → 拿到值 → 后续对值有依赖的操作享受 consume 保证。

#### 4.1.3 源码精读

trait 定义本身带了一段很到位的文档注释，把「为什么需要 consume」和「为什么只在 ARM/AArch64 真正优化」都讲清楚了：

[src/atomic/consume.rs:5-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L5-L25) —— 定义 `pub trait AtomicConsume`，关联类型 `type Val` 与方法 `fn load_consume(&self) -> Self::Val`。注释里关键三句：consume 类似 acquire 但只对「依赖」该 load 结果的操作建立顺序；在弱内存模型上通常比 acquire 快，因为不需要内存屏障指令；当前只在 ARM/AArch64 上能省 fence，其他架构退化为 `load(Acquire)`。

文件顶部还有一行被 `#[cfg(not(crossbeam_no_atomic))]` 门控的 `use core::sync::atomic::Ordering;`：

[src/atomic/consume.rs:1-2](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L1-L2) —— 仅当目标平台**有**原子支持时才引入 `Ordering`。这是后续两个 `impl_consume!` 宏要用的。

trait 的对外导出在门面 `mod.rs` 里：

[src/atomic/mod.rs:31-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L31-L32) —— `mod consume;` 之后 `pub use self::consume::AtomicConsume;`。注意这两行**没有任何额外 cfg**（对比上方 `AtomicCell` 的导出同时被 `target_has_atomic = "ptr"` 和 `not(crossbeam_loom)` 门控）。这意味着：即便在 loom 下、即便没有 `ptr` 宽度原子，`AtomicConsume` 这个名字依然存在；只是在 `crossbeam_no_atomic` 下它没有任何实现者（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：用最少的代码跑通 `load_consume`，确认它就是一个普通的 trait 方法调用。

**操作步骤**（以下为示例代码，不是项目原有代码）：

```rust
// Cargo.toml: crossbeam-utils = { version = "0.8", features = ["atomic"] }
use crossbeam_utils::atomic::AtomicConsume;
use std::sync::atomic::AtomicUsize;

fn main() {
    let a = AtomicUsize::new(42);
    // 调用的是 AtomicConsume::load_consume，而非 AtomicUsize::load
    let v: usize = a.load_consume();
    assert_eq!(v, 42);
    println!("loaded = {v}");
}
```

**需要观察的现象**：程序正常编译并打印 `loaded = 42`。

**预期结果**：编译通过说明 `AtomicUsize` 确实实现了 `AtomicConsume`（由 4.3 的 `impl_atomic!` 生成）。运行行为在所有平台上**完全一致**——`load_consume` 的平台差异只体现在生成的机器码里（是否带屏障），不影响可观察的返回值。

> ⚠️ 如果忘了在 `Cargo.toml` 里开 `features = ["atomic"]`，会报「cannot find `AtomicConsume`」——这正对应 u1-l3 讲的「`atomic` 模块由 `feature="atomic"` 门控」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AtomicConsume` 的文档说它「类似 acquire」而不是「等价于 acquire」？

**参考答案**：acquire 对 load **之后的所有**读写都建立顺序保证；consume 只对**数据依赖于 load 结果**的后续操作建立保证。因此 consume 的约束更弱，但也因此可能在弱内存架构上省掉屏障。

**练习 2**：Rust 标准库的 `Ordering` 有 `Consume` 变体吗？如果没有，`AtomicConsume` 是怎么补上这个缺口的？

**参考答案**：没有。`AtomicConsume` 不依赖语言层面的 consume，而是用 `load(Relaxed) + compiler_fence(Acquire)` 在 ARM/AArch64 上手动拼出近似 consume 的行为，其余平台退化为 `load(Acquire)`（见 4.2）。

---

### 4.2 impl_consume! 分平台宏：编译期二选一

#### 4.2.1 概念说明

`load_consume` 的方法体不能写成「一个固定实现」——它的最优实现依赖目标架构。`consume.rs` 用了 Rust 里很经典的手法：**定义两个同名宏 `impl_consume!`，用互斥的 `cfg` 让它们不可能同时存在**，编译器在编译期根据目标自动选中其中一个展开。

两个版本分别是：

- **快速路径（ARM/AArch64）**：`let result = self.load(Relaxed); compiler_fence(Acquire); result`。`compiler_fence` 不发 CPU 指令，只挡编译器；硬件的数据依赖顺序保住了正确性。
- **回退路径（其它所有平台/工具环境）**：`self.load(Acquire)`。直接用标准库的 acquire 读，简单正确但可能多一条屏障。

#### 4.2.2 核心流程

快速路径的启用条件是一个复合 `cfg`：

```
all(
    any(target_arch = "arm", target_arch = "aarch64"),   // 仅这两个架构
    not(any(miri, crossbeam_loom, crossbeam_sanitize_thread)),  // 且不在 sanitizer/loom/miri 下
)
```

决策流程（伪代码）：

```
if crossbeam_no_atomic:
    // trait 存在，但没有 impl_consume 宏体（Ordering 都没引入）
elif arch in {arm, aarch64} and not (miri or loom or tsan):
    选中「快速路径」宏 → load(Relaxed) + compiler_fence(Acquire)
else:
    选中「回退路径」宏 → load(Acquire)
```

为什么要把 `miri`/`loom`/`crossbeam_sanitize_thread` 排除？注释里讲得很直白：

- **Miri** 不建模 consume 序，会把依赖顺序当成没有，从而误报数据竞争。
- **Loom** 目前不支持 `compiler_fence`（见 `src/lib.rs:60-65` 引用的 [tokio-rs/loom#117](https://github.com/tokio-rs/loom/issues/117)），临时用更强的 `fence` 顶替。
- **ThreadSanitizer** 不把 `load(Relaxed) + compiler_fence(Acquire)` 识别为一次 consume 读，会误报。

所以在这些验证工具下，统一退化为 `load(Acquire)`——慢一点，但工具给出的结果是可信的。这是一处典型的「为可测试性牺牲少量性能」的工程取舍。

#### 4.2.3 源码精读

快速路径宏（含上方那段重要注释）：

[src/atomic/consume.rs:27-48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L27-L48) —— `macro_rules! impl_consume` 的第一个定义。注释（28-33 行）解释了三件事：Miri/Loom 不支持 consume、TSan 不认这条模式、以及在 PowerPC/MIPS 等架构上 `compiler_fence(Acquire)` 实际会生成硬件 fence（godbolt 链接），所以「真能省 fence」的现实架构只有 ARM/AArch64。`cfg`（34-37 行）正是这个判断的代码化。宏体（40-46 行）就是 `load(Relaxed)` + `compiler_fence(Acquire)`，注意 `compiler_fence` 走的是 `crate::primitive::sync::atomic::compiler_fence`（在 loom 下会被替换，见 u1-l2 的 primitive 抽象层）。

回退路径宏：

[src/atomic/consume.rs:50-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L50-L62) —— 第二个 `macro_rules! impl_consume`，`cfg` 是第一个的**逻辑取反**，保证两选一。宏体只有一句 `self.load(Ordering::Acquire)`。

两个宏互斥的关键：它们的 `cfg` 条件互为补集（在 `not(crossbeam_no_atomic)` 大前提下），所以同一编译目标下永远只有一个 `impl_consume!` 存在；`impl_atomic!` 里写的 `impl_consume!()` 调用会展开成对应那一个。

#### 4.2.4 代码实践

**实践目标**：在不交叉编译的情况下，确认本机（很可能就是 x86-64）走的是**回退路径**，并理解为什么运行时无法直接观测到差异。

**操作步骤**：

1. 阅读上面两个宏的 `cfg`，判断你本机架构属于哪一支。
2. （可选）用 `cargo expand` 查看展开后的方法体：
   ```bash
   cargo install cargo-expand   # 若未安装
   cargo expand --features atomic atomic::AtomicConsume
   ```
3. 想观察「真正省 fence」的机器码差异，需要交叉到 aarch64（见综合实践 5）。

**需要观察的现象**：在 x86-64 上展开出的 `load_consume` 方法体应为 `self.load(Ordering::Acquire)`；在 aarch64 上应为 `load(Relaxed)` + `compiler_fence(Acquire)`。

**预期结果**：宏展开结果与 `cfg` 判断一致。**运行时行为**（返回值）在两种路径下完全相同，差异仅存在于生成的汇编里是否带屏障——所以「是否省了 fence」无法靠运行程序观察，只能靠看汇编。汇编层面的差异：**待本地验证**（需交叉编译到 aarch64 后用 `cargo asm` 或 godbolt 比对）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `impl_consume!` 要定义成两个互斥 `cfg` 的同名宏，而不是在一个宏里写 `#[cfg(...)]` 分支？

**参考答案**：因为 `cfg` 只能挂在**项（item）**上。宏体内的两条不同函数体无法各自挂 `cfg` 再同名共存；而把整个宏挂上 `cfg`，让两个 `macro_rules!` 在编译期只剩一个，是最干净的「编译期二选一」写法。

**练习 2**：假设你在 aarch64 上、且设置了 `RUSTFLAGS="--cfg miri"`，`load_consume` 会走哪条路径？为什么？

**参考答案**：走回退路径（`load(Acquire)`）。因为快速路径的 `cfg` 同时要求 `not(any(miri, crossbeam_loom, crossbeam_sanitize_thread))`，miri 下该条件不满足，落到取反分支。这是为了让 Miri 给出可信的结果。

---

### 4.3 impl_atomic! 类型列表：批量实现与 AtomicPtr 特例

#### 4.3.1 概念说明

`AtomicConsume` trait 本身只声明了方法签名，真正让它有用的是后面那一长串 `impl`。手写每个原子类型的实现既啰嗦又易错，所以 `consume.rs` 用 `impl_atomic!` 宏批量生成：传入「原子类型名」和「关联值类型」，宏就展开出完整的 `impl AtomicConsume` 块，方法体直接调用 4.2 的 `impl_consume!()`。

此外要处理两个细节：

- **双实现**：每个类型既要给 `core::sync::atomic::X` 实现（真实运行），也要给 `loom::sync::atomic::X` 实现（loom 模型测试），各自挂不同 `cfg`。
- **类型可用性**：`AtomicU32`/`AtomicI32`/`AtomicU64`/`AtomicI64` 不是所有平台都有，要按 `target_has_atomic` / `target_pointer_width` 门控。

#### 4.3.2 核心流程

`impl_atomic!` 宏展开逻辑（伪代码）：

```
impl_atomic!(AtomicUsize, usize) 展开为:
    #[cfg(not(crossbeam_no_atomic))]
    impl AtomicConsume for core::sync::atomic::AtomicUsize {
        type Val = usize;
        impl_consume!();   // 注入 4.2 选中的方法体
    }
    #[cfg(crossbeam_loom)]
    impl AtomicConsume for loom::sync::atomic::AtomicUsize {
        type Val = usize;
        impl_consume!();
    }
```

类型清单分三组：

| 组 | 类型 | 额外 cfg |
| --- | --- | --- |
| 无门控 | `AtomicBool`、`AtomicUsize`、`AtomicIsize`、`AtomicU8`、`AtomicI8`、`AtomicU16`、`AtomicI16` | 无 |
| 32 位 | `AtomicU32`、`AtomicI32` | `any(target_has_atomic = "32", not(target_pointer_width = "16"))` |
| 64 位 | `AtomicU64`、`AtomicI64` | `any(target_has_atomic = "64", not(any(target_pointer_width = "16", target_pointer_width = "32")))` |

`AtomicPtr<T>` 因为带泛型参数 `T`，塞不进 `impl_atomic!($atomic:ident, $val:ty)` 的固定模式，所以单独手写两个 impl（`core` 与 `loom` 各一个），但方法体同样调用 `impl_consume!()`。

#### 4.3.3 源码精读

`impl_atomic!` 宏定义：

[src/atomic/consume.rs:64-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L64-L77) —— 接收 `$atomic:ident`（如 `AtomicUsize`）与 `$val:ty`（如 `usize`）。宏体里同时给出 `core` 版（`#[cfg(not(crossbeam_no_atomic))]`）和 `loom` 版（`#[cfg(crossbeam_loom)]`）两个 `impl`，二者都把方法体委托给 `impl_consume!()`。

整数类型批量调用：

[src/atomic/consume.rs:79-99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L79-L99) —— 7 个无门控类型在 79-85 行连续列出；32 位（86-89 行）和 64 位（90-99 行）各自带平台 cfg。这套 cfg 与标准库「该平台是否原生支持该宽度原子」的判定一致，避免在没有 64 位原子的平台上引用不存在的 `AtomicU64`。

`AtomicPtr<T>` 的手写实现：

[src/atomic/consume.rs:101-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L101-L111) —— 因为 `AtomicPtr<T>` 是泛型，关联类型 `type Val = *mut T`，无法套用 `impl_atomic!` 的非泛型模式，所以单独写。注意它同样只有 `not(crossbeam_no_atomic)`（core 版）和 `crossbeam_loom`（loom 版）两道门，方法体仍是 `impl_consume!()`——一致性来自宏共享。

#### 4.3.4 代码实践

**实践目标**：用 `AtomicPtr` + `load_consume` 演示 consume 序最典型的场景——「发布一个指针并解引用」，并结合 `consume.rs` 注释解释 x86-64 与 aarch64 的行为差异。

**操作步骤**（以下为示例代码，不是项目原有代码）：

```rust
// Cargo.toml: crossbeam-utils = { version = "0.8", features = ["atomic"] }
use crossbeam_utils::atomic::AtomicConsume;
use std::sync::atomic::{AtomicPtr, Ordering};
use std::ptr;

static mut DATA: i32 = 0;

fn main() {
    let ptr = AtomicPtr::<i32>::new(ptr::null_mut());

    // 写线程：先写数据，再以 Release 发布指针
    std::thread::scope(|s| {
        s.spawn(|| {
            // SAFETY: 仅在本例的单线程写者内修改 DATA
            unsafe { DATA = 42; }
            ptr.store(&raw const DATA as *mut i32, Ordering::Release);
        });

        // 读线程：用 load_consume 读指针，再解引用（解引用「依赖」于读到的地址）
        s.spawn(|| {
            // 自旋等到指针非空
            let p: *mut i32 = loop {
                let p = ptr.load_consume(); // <- 本讲主角
                if !p.is_null() { break p; }
                std::hint::spin_loop();
            };
            // SAFETY: p 指向已初始化的 DATA，且 store(Release) 之前的写
            //        DATA=42 经 release->consume 边对这里可见。
            let v = unsafe { *p };
            assert_eq!(v, 42);
            println!("read *p = {v}");
        });
    });
}
```

**需要观察的现象**：程序打印 `read *p = 42`，且 `assert_eq!` 通过——说明 release/consume 边把 `DATA = 42` 的写入带到了读线程。

**结合注释解释行为差异**：参照 [src/atomic/consume.rs:28-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L28-L33) 的注释：

- 在 **x86-64** 上，`load_consume` 走的是回退路径 `load(Acquire)`。即便 x86 是强内存模型，这里也不会去走「Relaxed + compiler_fence」的捷径，因为该捷径的 `cfg` 只认 `arm`/`aarch64`。所以 x86-64 上它**实际表现为 Acquire**——但 x86 的 load 本身就近似 acquire，开销并不高。
- 在 **aarch64** 上（且未开 miri/tsan/loom），`load_consume` 走快速路径 `load(Relaxed) + compiler_fence(Acquire)`：CPU 不发屏障，靠硬件对数据依赖的保序保证 `*p` 不会读到旧指针、新内容这种「撕裂」组合。

**预期结果 / 待本地验证**：返回值与正确性在两种架构下完全一致；汇编层面 aarch64 应**少一条/弱化**屏障指令。要亲眼看到汇编差异，需交叉编译到 aarch64 后用 `cargo asm` 或 godbolt 比对 `load_consume` 与 `load(Acquire)` 的指令——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`AtomicPtr<T>` 为什么不放进 `impl_atomic!` 宏里一起批量生成？

**参考答案**：因为它带泛型参数 `T`，关联类型 `type Val = *mut T` 也依赖 `T`；而 `impl_atomic!($atomic:ident, $val:ty)` 假定类型名与值类型都是固定标识符/类型，无法表达泛型，所以单独手写。

**练习 2**：在 `crossbeam_no_atomic` 目标下，`AtomicConsume` trait 还能被调用吗？

**参考答案**：不能。trait 本身（5-25 行）没有 `cfg`，名字依然存在；但所有 `impl`（`impl_atomic!` 体里的 `#[cfg(not(crossbeam_no_atomic))]`、以及 `AtomicPtr` 的实现）都被门控掉，连 `Ordering` 都没引入。所以 trait 没有任何实现者，调用 `load_consume` 会直接编译失败。

**练习 3**：为什么 32 位整数组的实现用 `any(target_has_atomic = "32", not(target_pointer_width = "16"))` 而不是单纯 `target_has_atomic = "32"`？

**参考答案**：这是一个兼容性的「或」条件——即便目标没有显式声明 `target_has_atomic = "32"`，只要指针宽度不是 16 位（即至少 32 位平台），通常也提供 32 位原子，于是也允许该 impl 存在。这放宽了可用平台范围，避免在某些老/特殊目标上误判为不可用。

## 5. 综合实践

把本讲三个最小模块串起来，做一个「分平台行为对比」的小任务：

1. **写**：实现一个函数 `read_via_consume(p: &AtomicPtr<u64>) -> u64`，内部用 `load_consume` 读指针并解引用（参考 4.3.4 的示例代码）。
2. **读**：精读 `consume.rs` 全文，画一张「`cfg` 决策树」：从 `crossbeam_no_atomic`、`target_arch`、`miri`、`crossbeam_loom`、`crossbeam_sanitize_thread` 五个开关出发，标注每个组合下 `load_consume` 展开成哪段方法体。
3. **解释**：用你画的决策树回答——在「x86-64 + 普通构建」「aarch64 + 普通构建」「aarch64 + `--cfg crossbeam_sanitize_thread`」三种场景下，`read_via_consume` 分别走了哪条路径？为什么在第三种场景下即便架构对了也要退化？
4. **（进阶，待本地验证）**：交叉编译到 aarch64，用 `cargo asm` 比对 `load_consume` 与一个手写 `load(Acquire)` 的汇编，验证 aarch64 上前者确实没有额外的硬件屏障指令。

这个任务把「trait 契约（4.1）→ 宏二选一（4.2）→ 类型清单（4.3）」三步连成一条线：先会用，再看清编译期如何选实现，最后理解为什么这样选。

## 6. 本讲小结

- `AtomicConsume` 给所有原生原子类型补了一个统一的 `load_consume` 方法，语义近似 acquire，但只对**数据依赖**于 load 结果的后续操作建立顺序，因此在弱内存架构上可能更轻。
- Rust 标准库没有 `Ordering::Consume`，`crossbeam-utils` 用 `load(Relaxed) + compiler_fence(Acquire)` 在 ARM/AArch64 上手搓 consume；其它平台或 sanitizer 下退化为 `load(Acquire)`。
- 实现选择由两个互斥 `cfg` 的同名 `impl_consume!` 宏完成——编译期二选一，调用者无感。
- 快速路径只对 `arm`/`aarch64` 开放，并在 `miri`/`loom`/`crossbeam_sanitize_thread` 下被强制退化，以保证验证工具给出可信结果（注释明确指出 PowerPC/MIPS 上 `compiler_fence(Acquire)` 会生成硬件 fence）。
- `impl_atomic!` 宏为 `AtomicBool`/`AtomicUsize`/.../`AtomicU64` 批量生成 `core` 与 `loom` 双实现；`AtomicPtr<T>` 因泛型单独手写；32/64 位整数类型按 `target_has_atomic`/`target_pointer_width` 门控。
- 与 `AtomicCell` 不同，`AtomicConsume` 不受 `target_has_atomic="ptr"` 或 `not(crossbeam_loom)` 门控，所以在 loom 下仍可用（有独立的 loom impl）；但在 `crossbeam_no_atomic` 下 trait 没有任何实现者。

## 7. 下一步学习建议

- 想看「consume 真正在 crate 内部被怎么用」的读者，可以接着读 `src/sync/parker.rs`，对比它用普通 `compare_exchange(SeqCst, SeqCst)` 管理 token 的做法，体会为什么 Parker 没有使用 `load_consume`。
- 如果对「编译期 cfg 如何切换实现路径」感兴趣，下一讲 **u2-l5 Backoff** 会展示另一套 no_std 下的条件实现（`spin` vs `snooze`），与本讲的宏二选一思路遥相呼应。
- 想深入理解 `AtomicConsume` 的「依赖顺序」为何成立，建议额外阅读 Linux 内核文档 `Documentation/core-api/wrappers/memory-barriers.rst` 中关于 `smp_read_barrier_depends` / `READ_ONCE` 的章节，以及 `consume.rs` 注释引用的 [rust-lang/rust#62256](https://github.com/rust-lang/rust/issues/62256)。
