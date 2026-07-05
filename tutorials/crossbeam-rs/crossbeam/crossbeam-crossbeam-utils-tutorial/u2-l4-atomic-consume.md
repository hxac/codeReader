# AtomicConsume 与 consume 内存序

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「consume 内存序」是什么、它和 acquire 有什么区别、为什么在弱内存模型 CPU 上可能更快。
- 看懂 `consume.rs` 中 `AtomicConsume` trait 的定义，以及它为什么是一个独立于标准库 `Ordering` 的手工实现。
- 准确说出 `impl_consume!` 宏的两个分支各在什么 `cfg` 条件下被选中，并能解释为什么只在 ARM/AArch64 上才走「真正的 consume 路径」。
- 读懂 `impl_atomic!` 宏如何为各种整数原子类型批量生成实现，以及为什么 32/64 位类型的实现要额外加 `cfg`。
- 解释为什么在 Miri / Loom / ThreadSanitizer 下，以及 x86-64 上，consume 都会退化成普通的 `load(Acquire)`。

本讲承接 [u2-l1](u2-l1-atomiccell-api.md)：那里讲了 `AtomicCell` 的对外契约与固定的 load=Acquire / store=Release 排序约定。本讲换一个角度，讲 crossbeam 如何为**标准库原生原子类型**提供一种标准库本身没有的、更轻的读内存序。

## 2. 前置知识

### 2.1 内存序（memory ordering）回顾

在现代多核 CPU 上，一个线程发出的「读/写」并不一定按代码顺序被其它核心观察到。Rust 的 `core::sync::atomic::Ordering` 用来约束原子操作**之间**以及它们与**周围普通内存操作**之间的可见性顺序。最常用的几种：

- `Relaxed`：只保证操作本身的原子性，不约束与其它内存操作的顺序。
- `Release`（写）/ `Acquire`（读）：一对配套。Acquire 读之后的**所有**后续读写，都保证能看到匹配 Release 写之前的所有写入。
- `AcqRel` / `SeqCst`：更强的组合，本讲用不到。

### 2.2 consume 比 acquire「弱」在哪里

`Acquire` 读会约束它**之后的所有**内存操作（无论是否和读到的值有关）。而 `consume` 只约束那些**依赖于读到的值**的后续操作（data dependency / address dependency）。

举一个最典型的例子——读取一个指向共享数据的指针，再解引用它：

```text
let p = atomic_ptr.load_consume();   // 读指针
let data = (*p).field;                // 解引用，依赖于 p
```

「解引用」在数据上**依赖**于读到的指针 `p`。consume 语义保证：当你读到指针 `p` 时，`p` 所指对象的内容一定已经写好了。但 consume **不**保证与 `p` 无关的其它读写在顺序上被约束——这恰恰是它能更快的原因。

### 2.3 为什么 consume 在弱内存模型上可能免 fence

- 在弱内存架构（ARM、AArch64、PowerPC、MIPS 等）上，硬件本身会**沿数据/地址依赖**保持顺序。也就是说，只要后续操作「依赖」读到的值，CPU 不会把它们重排到读之前——天然如此。
- 因此一个 consume 读只需要一道**编译器栅栏**（阻止编译器把依赖优化掉、或把后续代码提前），**不需要**硬件 fence 指令。
- 而一个 `Acquire` 读在弱模型上往往需要更强的指令（如 AArch64 的 `ldar`，或更糟的 fence）。

> 直觉记忆：**acquire 挡住一切后续访问；consume 只挡住「顺着读到的值往下用」的那条依赖链，所以更便宜。**

### 2.4 一个关键背景：Rust 标准库没有 Consume

Rust 的 `core::sync::atomic::Ordering` 枚举里**没有** `Consume` 变体（早期曾规划，后被移除）。因为正确实现 consume 要求编译器全程跟踪「数据依赖」，工程上极难做对（C++ 也因此把 `memory_order_consume` 实际降级成 acquire）。

所以 crossbeam 没有「现成的 consume」可用——它必须手工拼出来。这正是本讲主角 `AtomicConsume` 存在的理由。

## 3. 本讲源码地图

本讲只涉及两个文件，且都很短：

| 文件 | 作用 |
|------|------|
| `src/atomic/consume.rs` | 定义 `AtomicConsume` trait、两个分平台的 `impl_consume!` 宏、批量生成的 `impl_atomic!` 宏，以及 `AtomicPtr` 的实现。 |
| `src/atomic/mod.rs` | 声明 `mod consume;` 并 `pub use` 导出 `AtomicConsume`。注意：`consume` 模块**没有** `target_has_atomic` / `not(crossbeam_loom)` 门控。 |

另外会引用 [u1-l2](u2-l1-atomiccell-api.md) 已建立的认知：`lib.rs` 里的 `primitive` 抽象层决定了 `compiler_fence` 在 loom 下其实是 `loom::sync::atomic::fence` 的替身。

## 4. 核心概念与源码讲解

### 4.1 AtomicConsume trait：consume 内存序的对外契约

#### 4.1.1 概念说明

`AtomicConsume` 是一个很小的 trait，只声明一个方法 `load_consume`。它的作用是给**标准库原生原子类型**（`AtomicUsize`、`AtomicPtr` 等）加上一个「按 consume 序读取」的能力。注意它操作的不是 crossbeam 自家的 `AtomicCell`，而是 `core::sync::atomic` 里那些类型——这是它与 [u2-l1](u2-l1-atomiccell-api.md) 的 `AtomicCell` 的本质区别。

trait 文档原文说得很清楚：consume「类似 acquire，只不过只对那些『依赖于读结果』的操作保证顺序」，并且「在有弱内存模型的架构上通常比 acquire 快得多，因为不需要内存 fence 指令」。

#### 4.1.2 核心流程

`load_consume` 在概念上只有两种实现，二选一：

```text
# 路径 A：真正的 consume（仅 ARM/AArch64、且非 sanitizer）
result = self.load(Relaxed)        # 不加硬件 fence 的普通读
compiler_fence(Acquire)            # 只挡编译器重排，不生成硬件 fence
return result

# 路径 B：退化（其它所有情况）
return self.load(Acquire)          # 直接用标准库的 acquire 读
```

两条路径由编译期 `cfg` 选定，对调用方完全透明——`load_consume` 的签名和行为契约在两种实现下都一致：返回值本身永远正确，差异只在于「对周围内存访问的约束强度」与「生成的机器指令开销」。

#### 4.1.3 源码精读

trait 定义本身没有任何 `cfg` 门控，永远会被编译（即便在 `crossbeam_no_atomic` 平台下，trait 仍存在，只是没人实现它）：

[consume.rs:5-25 — AtomicConsume trait 与 load_consume 方法声明](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L5-L25)

注意几点：

- 关联类型 `type Val` 表示 `load_consume` 的返回类型（如 `AtomicUsize` 对应 `usize`、`AtomicPtr<T>` 对应 `*mut T`）。
- `load_consume` 是**必须实现**的方法（无默认实现），所以具体走哪条路径由 `impl` 端的宏决定。
- 文档注释明确点出：「目前只在 ARM 和 AArch64 上实现（可以避免 fence），其它架构退化为 `load(Ordering::Acquire)`」——这是本讲 4.2 节要展开的核心。

文件顶部那一行 `use core::sync::atomic::Ordering;` 被 `#[cfg(not(crossbeam_no_atomic))]` 门控（[consume.rs:1-2](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L1-L2)），因为只有在「目标平台支持原子」时才用得到 `Ordering`。

#### 4.1.4 代码实践

**目标**：用 `AtomicConsume` 读一个 `AtomicUsize`，验证它就是一个普通的「读当前值」。

**操作步骤**：

1. 在一个依赖了 `crossbeam-utils`（开启 `atomic` feature）的 crate 里写：
   ```rust
   use crossbeam_utils::atomic::AtomicConsume;

   let a = std::sync::atomic::AtomicUsize::new(42);
   // 注意：调用的不是 AtomicUsize 自带的方法，而是 AtomicConsume trait 的扩展方法
   let v: usize = a.load_consume();
   assert_eq!(v, 42);
   ```
2. 编译运行，确认打印 / 断言通过。

**需要观察的现象**：`load_consume` 作为一个 trait 方法，必须把 `AtomicConsume` 引入作用域才能调用。

**预期结果**：断言通过，读到的就是当前值 42。从结果上看不出和 `a.load(Acquire)` 的区别——差异只在底层指令层面（见 4.2.4）。

#### 4.1.5 小练习与答案

**练习 1**：`AtomicConsume` 是给 `AtomicCell` 用的，还是给标准库原子类型用的？为什么？

> **答案**：给标准库原子类型用的。trait 在 `consume.rs` 里只为 `core::sync::atomic::$atomic` 和 `AtomicPtr<T>` 做了实现（见 4.3），并没有为 crossbeam 自家的 `AtomicCell` 实现。`AtomicCell` 自己内部固定用 Acquire/Release，不暴露内存序参数（见 [u2-l1](u2-l1-atomiccell-api.md)）。

**练习 2**：为什么 `load_consume` 在 trait 里没有默认实现？

> **答案**：因为两条实现路径要用不同的 `cfg` 选定，而 trait 的默认实现无法按 `target_arch` / sanitizer 分叉。把「选哪条路径」下放到 `impl` 端的 `impl_consume!` 宏里（4.2），由宏的 `cfg` 决定展开成哪段代码，是最干净的做法。

---

### 4.2 impl_consume 分平台宏：为什么只在 ARM/AArch64 上「真 consume」

#### 4.2.1 概念说明

`impl_consume!` 宏不接收参数，它在展开时直接变成 `load_consume` 的方法体。文件里**定义了两个同名宏**，靠互斥的 `cfg` 让编译器只选中其中一个：

- 一个展开成「`load(Relaxed)` + `compiler_fence(Acquire)`」——真正的 consume 路径。
- 另一个展开成「`load(Acquire)`」——退化路径。

这是 Rust 里常见的「用 `cfg` 选择宏体」的技巧：同名宏写两遍，各自的 `cfg` 互为否定，保证任意配置下有且仅有一个生效。

#### 4.2.2 核心流程

选中「真 consume 路径」必须**同时**满足：

```text
target_arch 是 "arm" 或 "aarch64"
且 不是 miri
且 不是 crossbeam_loom
且 不是 crossbeam_sanitize_thread
且 不是 crossbeam_no_atomic
```

只要其中任一条件不满足（比如在 x86-64、PowerPC 上，或开了 ThreadSanitizer，或在 Miri/Loom 下），就落到退化路径 `load(Acquire)`。

这套排除规则的直觉是：

- **只挑 ARM/AArch64**：consume 的收益来自「硬件沿依赖保持顺序 + compiler_fence 不生成硬件指令」。但在 PowerPC、MIPS 等架构上，LLVM 会把 `compiler_fence(Acquire)` 编译成等价于硬件 fence 的指令（见源码注释里的 godbolt 链接与 rust-lang/rust#62256），收益消失，不如直接用 `load(Acquire)`。
- **排除 miri/loom**：Miri 和 Loom 不建模 consume 语义，无法验证这条路径。
- **排除 sanitize_thread**：ThreadSanitizer 不会把 `load(Relaxed) + compiler_fence(Acquire)` 识别成 consume 读，会让它误报数据竞争；退化成 Acquire 才能让 TSan 正确理解。

#### 4.2.3 源码精读

真正的 consume 路径——注意它门控在 `arm`/`aarch64` 且排除三类 sanitizer/test 工具：

[consume.rs:27-48 — 真 consume 路径：load(Relaxed) + compiler_fence(Acquire)](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L27-L48)

读这段代码要抓住三点：

1. `self.load(Ordering::Relaxed)`——读取本身不加任何运行时屏障。
2. `compiler_fence(Ordering::Acquire)` 来自 `crate::primitive::sync::atomic::compiler_fence`（见 [lib.rs 的 primitive 层](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83)）。在非 loom 下它就是 `core::sync::atomic::compiler_fence`——**只阻止编译器重排，不生成硬件指令**（这正是该路径能省 fence 的关键）。在 loom 下它其实是 `loom` 的 `fence`（[lib.rs:60-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L60-L65)），但本路径已被 `not(crossbeam_loom)` 排除，所以 loom 下根本走不到这里。
3. 顶部那段注释（[consume.rs:28-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L28-L33)）是理解整个设计的钥匙，明确写出了 Miri/Loom/TSan 的限制以及「只在 ARM/AArch64 上 compiler_fence 才不变成硬件 fence」。

退化路径——`cfg` 恰为上面那段宏条件的**整体取反**，保证两者互斥：

[consume.rs:50-62 — 退化路径：直接 load(Acquire)](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L50-L62)

注意它外层还有一道 `#[cfg(not(crossbeam_no_atomic))]`——在完全没有原子的目标上，两个宏都不展开，trait 也就无人实现。

#### 4.2.4 代码实践

**目标**：用 `load_consume` 读取 `AtomicPtr` 并解引用，结合 `consume.rs` 注释解释 x86-64 与 aarch64 上的差异。

**操作步骤**：

1. 写一个 RCU 风格的最小示例（这是 consume 最经典的用途——读指针、解引用所指对象）：
   ```rust
   use crossbeam_utils::atomic::AtomicConsume;
   use std::sync::atomic::AtomicPtr;

   // 示例代码：一个写者把数据挂到 AtomicPtr 上，读者用 load_consume 读指针再解引用
   static SHARED: AtomicPtr<Data> = AtomicPtr::new(core::ptr::null_mut());

   #[derive(Debug)]
   struct Data { value: u64 }

   fn main() {
       // 写者（示例中简化为单线程顺序执行）
       let d = Box::new(Data { value: 7 });
       SHARED.store(Box::into_raw(d), std::sync::atomic::Ordering::Release);

       // 读者：consume 读
       let ptr: *mut Data = SHARED.load_consume();
       // 解引用依赖 ptr，consume 语义保证我们看到的是一致的 Data
       let value = unsafe { (*ptr).value };
       println!("read value = {}", value);
   }
   ```
2. 在 **x86-64** 上用 `cargo rustc --release -- --emit asm`（或 `cargo asm`）查看 `load_consume` 对应的机器指令。
3. （可选）在 **aarch64** 目标上交叉编译查看：`cargo rustc --release --target aarch64-unknown-linux-gnu -- --emit asm`。

**需要观察的现象与解释**：

- **x86-64 上**：`load_consume` 会落到退化路径 `load(Acquire)`。因为 x86 是 TSO（强内存模型），`Acquire` 读就是一条普通 `mov`，本来就不需要 fence。所以「在 x86-64 上 consume 实际仍表现为 Acquire」——而且这个 Acquire 是免费的，并没有额外开销。
- **aarch64 上**：若未开任何 sanitizer，`load_consume` 走真 consume 路径，生成的是普通 `ldr` + 编译器栅栏（无硬件 fence）；相比之下 `load(Acquire)` 会用更重的 `ldar`。这就是「在 aarch64 上可能省略 fence / 用更轻指令」的含义。
- **开了 ThreadSanitizer 时**：即便在 aarch64 上，因为 `crossbeam_sanitize_thread` 命中，也会退化成 `load(Acquire)`，让 TSan 能正确建模。

**预期结果**：x86-64 下看到的就是一条普通 `mov`；aarch64 下看到 `ldr`（而非 `ldar`）。汇编的具体形式随 rustc/LLVM 版本变化，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：在 `miri` 下运行 consume 示例时，`load_consume` 走哪条路径？为什么？

> **答案**：走退化路径 `load(Acquire)`。因为真 consume 路径的 `cfg` 含 `not(miri)`（[consume.rs:36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L36)）。注释解释：Miri 不支持 consume 序。

**练习 2**：为什么作者没有把 PowerPC、MIPS 也加进 `target_arch` 名单，让它们也走「真 consume」？

> **答案**：因为在这些架构上，LLVM 会把 `compiler_fence(Acquire)` 编译成等价于硬件 fence 的指令（见 [consume.rs:30-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L30-L31) 的注释与 godbolt 链接），省 fence 的收益消失，反而比直接 `load(Acquire)` 更复杂。所以保守地只让 ARM/AArch64 走该路径。

---

### 4.3 impl_atomic 类型列表：批量实现与平台门控

#### 4.3.1 概念说明

trait 定义好之后，得为每一种标准库原子类型实现它。手写一遍遍重复，所以用一个 `impl_atomic!` 宏批量生成。同时，不同位宽的原子类型并不是在所有平台都存在——比如 16 位指针宽度的目标上不一定有原生 32/64 位原子——所以 32/64 位那一组实现要额外加 `cfg`。

#### 4.3.2 核心流程

`impl_atomic!` 宏对每一个「原子类型 + 值类型」对生成**两份**实现：

```text
#[cfg(not(crossbeam_no_atomic))]
impl AtomicConsume for core::sync::atomic::$atomic { ... }   # 标准库版本

#[cfg(crossbeam_loom)]
impl AtomicConsume for loom::sync::atomic::$atomic { ... }   # loom 版本
```

两份都调用 `impl_consume!()` 展开方法体。这也呼应了 [u1-l2](u2-l1-atomiccell-api.md) 的结论：`AtomicConsume` 在 loom 下仍然可用（与 `AtomicCell` 不同，后者被 `not(crossbeam_loom)` 排除）。

类型清单按位宽分三组：

| 组别 | 类型 | 额外 cfg |
|------|------|----------|
| 总是存在 | `AtomicBool`、`AtomicUsize`、`AtomicIsize`、`AtomicU8/I8`、`AtomicU16/I16` | 无 |
| 32 位 | `AtomicU32`、`AtomicI32` | `any(target_has_atomic="32", not(target_pointer_width="16"))` |
| 64 位 | `AtomicU64`、`AtomicI64` | `any(target_has_atomic="64", not(any(width="16", width="32")))` |
| 指针 | `AtomicPtr<T>` | 单独 impl，非宏生成 |

#### 4.3.3 源码精读

宏定义——每个 `$atomic` 同时生成标准库与 loom 两份实现：

[consume.rs:64-77 — impl_atomic! 宏：同时为 core 与 loom 的原子类型实现 AtomicConsume](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L64-L77)

宏调用清单——注意 32/64 位两组带了额外的 `cfg`：

[consume.rs:79-99 — 各整数原子类型的 impl_atomic! 调用与位宽门控](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L79-L99)

32 位那一组的 cfg `any(target_has_atomic = "32", not(target_pointer_width = "16"))` 读作：「目标显式声明有 32 位原子，或者它不是 16 位指针宽（即 32/64 位指针平台，天然有 32 位原子）」。这样在 16 位平台上若没有 32 位原子，就跳过这两个实现，避免引用不存在的 `AtomicU32`。

`AtomicPtr<T>` 因为带泛型 `T`，没法塞进上面的宏，所以单独写：

[consume.rs:101-111 — AtomicPtr<T> 的两份实现（core + loom）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L101-L111)

#### 4.3.4 代码实践

**目标**：理解「在 16 位指针宽度的目标上，哪些 `AtomicConsume` 实现仍然存在」。

**操作步骤**（源码阅读型实践，无需运行）：

1. 读 [consume.rs:79-99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L79-L99)。
2. 假设目标平台 `target_pointer_width = "16"` 且 `target_has_atomic` **不含** `"32"` 与 `"64"`。
3. 逐行判断每个 `impl_atomic!` 调用是否会被展开。

**需要观察的现象 / 预期结果**：

在该假设平台上，依然有实现的类型是：

- `AtomicBool`、`AtomicUsize`、`AtomicIsize`（usize/isize 跟随指针宽度，16 位平台上是 16 位原子）
- `AtomicU8`、`AtomicI8`、`AtomicU16`、`AtomicI16`
- `AtomicPtr<T>`

而 `AtomicU32/I32` 与 `AtomicU64/I64` 的 `impl_atomic!` 调用因 `cfg` 不满足而被跳过——若强行使用 `AtomicU32::load_consume` 会编译失败（因为类型本身可能不存在，或 trait 未实现）。这一规律对**所有**标准库原子 API 都成立，并非 consume 独有，但 consume 把它显式写进了宏调用上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AtomicPtr<T>` 不能也用 `impl_atomic!` 宏生成？

> **答案**：因为 `AtomicPtr<T>` 带泛型参数 `T`，而 `impl_atomic!` 接收的是「单标识符 + 固定值类型」（如 `AtomicUsize, usize`）。带泛型的 impl 需要写 `impl<T> AtomicConsume for AtomicPtr<T>`，宏模板不支持，所以单独写一份（[consume.rs:101-105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L101-L105)）。

**练习 2**：在 loom 模式下，`AtomicConsume` 的实现是给 `core::sync::atomic` 还是 `loom::sync::atomic` 的类型？

> **答案**：两份都有，但实际可用的是 `loom::sync::atomic` 那份（被 `#[cfg(crossbeam_loom)]` 门控，[consume.rs:71-75](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L71-L75)）。这呼应 [u1-l2](u2-l1-atomiccell-api.md) 关于 `primitive` 抽象层与 loom 覆盖范围的结论：consume 模块本身没有 `not(crossbeam_loom)` 门控，所以在 loom 下仍然编译，且 loom 下走的是 `load(Acquire)` 退化路径。

**练习 3**：在 loom 下 `load_consume` 走的是 4.2 的哪条路径？

> **答案**：退化路径 `load(Acquire)`。因为真 consume 路径要求 `not(crossbeam_loom)`（[consume.rs:36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L36)）。这与 4.3.3 提到的「loom 下 `compiler_fence` 其实是 `fence`」并不冲突——因为该更强替身根本不会被用到。

---

## 5. 综合实践

把本讲三块内容串成一个完整的小任务：**用 `load_consume` 实现一个无锁的「发布 / 订阅」读取器，并通过阅读源码 + 反汇编说清它在不同平台上的真实开销。**

任务步骤：

1. **写代码**：用一个后台线程作为「发布者」，周期性地 `Box::new` 一份新数据并通过 `AtomicPtr::store(.., Release)` 发布；主线程作为「订阅者」，循环用 `load_consume` 读指针并解引用打印字段（注意：这是个演示 consume 语义的最小例子，**没有**安全地回收旧 `Box`，真实 RCU 还需引用计数或 epoch 回收——这正好引出后续学习建议）。
2. **静态分析**：对照 [consume.rs:27-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L27-L62)，写下在以下四种配置下 `load_consume` 各自展开成什么：
   - x86-64，普通构建；
   - aarch64，普通构建；
   - aarch64，开启 ThreadSanitizer；
   - 任意平台，Miri 下运行。
3. **反汇编验证**（**待本地验证**）：在 x86-64 上 `cargo rustc --release -- --emit asm` 找到 `load_consume`，确认它就是一条普通 `mov`（退化路径，但 TSO 下 Acquire 免费）。
4. **画一张时序图**：发布者 `store(Release)` → 订阅者 `load_consume` 读到指针 → 解引用。在图上标注「数据依赖」这条链，并用本讲的语言说明为什么订阅者不会读到只写了一半的对象。

完成此实践后，你应该能把「consume 的语义」「为什么只在 ARM/AArch64 上省 fence」「sanitizer 与 loom 下为何退化」这三件事用自己的话讲清楚。

## 6. 本讲小结

- `AtomicConsume` 是给**标准库原生原子类型**（不是 `AtomicCell`）加的「按 consume 序读取」能力，因为 Rust 标准库的 `Ordering` 没有 `Consume` 变体。
- consume 只约束「依赖于读结果的后续操作」，比 acquire 弱；在弱内存模型上可能因此省掉硬件 fence。
- 真正的 consume 路径 `load(Relaxed) + compiler_fence(Acquire)` 只在 **ARM/AArch64 且非 miri/loom/sanitize_thread** 时启用；其余情况一律退化成 `load(Acquire)`。
- 退化的原因各有不同：x86 本就 TSO、Acquire 免费；PowerPC/MIPS 的 compiler_fence 会变成硬件 fence，无收益；Miri/Loom 不建模 consume；TSan 无法识别该模式。
- `impl_atomic!` 宏为各整数类型批量生成 core 与 loom 两份实现；32/64 位类型因窄平台缺原子而加额外 `cfg`；`AtomicPtr<T>` 因泛型单独实现。
- `consume` 模块本身没有 `target_has_atomic` / `not(crossbeam_loom)` 门控，所以 `AtomicConsume` 在 loom 下仍可用（走退化路径）——这与 `AtomicCell` 不同。

## 7. 下一步学习建议

- **回到 `AtomicCell` 的内部机制**：本讲的 `AtomicConsume` 只读单个原生原子值；如果你想知道 crossbeam 如何对一个**任意大小**的类型做无锁/加锁读写，继续读 [u2-l2 AtomicCell 的无锁路径](u2-l2-atomiccell-lockfree.md) 与 [u2-l3 全局锁回退与 SeqLock](u2-l3-atomiccell-global-lock-seqlock.md)。
- **从 consume 走向更深的无锁回收**：本讲综合实践里刻意留下了「旧 `Box` 没有安全回收」的悬念。RCU/epoch-based reclamation 正是 consume 的天然搭档，可以接着读 crossbeam 主仓的 `crossbeam-epoch` crate。
- **想验证并发正确性**：本讲多次提到 Miri/Loom/TSan 的限制。下一阶段可读 [u5-l4 并发测试策略与基准](u5-l4-testing-and-benchmarks.md)，系统了解这三种工具如何互补地验证数据竞争。
