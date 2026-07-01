# AtomicConsume 与序列锁

## 1. 本讲目标

本讲承接 [u2-l3 AtomicCell](u2-l3-atomic-cell.md)。在那里我们见到了 `AtomicCell<T>` 的两条实现路径：能命中原生原子类型的走 CPU 原子指令，否则退化为一组「全局序列锁（SeqLock）」兜底。本讲聚焦那一段旅程里被一带而过的两个「细节」，它们恰恰是 crossbeam-utils 精细工程的缩影：

1. **读时的内存序**：为什么读取一个被并发写入的原子值，需要 `Acquire`？有没有比 `Acquire` 更便宜的「按需排序」？`AtomicConsume` trait 正是为此而生。
2. **序列锁的计数回绕**：序列锁靠一个不断 `+2` 的戳记（stamp）来判断「读期间有没有被写过」。当这个戳记是 32 位（甚至 16 位）整数时，它会回绕（wrap around）——而 `seq_lock_wide.rs` 就是用来挡住这个隐患的。

学完本讲你应当能够：

- 说清 `consume` 内存序相对 `acquire` 的语义差异，以及为什么它在弱内存模型架构上更便宜；
- 读懂 `AtomicConsume` trait 与 `impl_consume!` 宏的两条分支，解释 ARM/AArch64 上「`Relaxed` 载入 + `compiler_fence(Acquire)`」为何等价于一次 consume 载入；
- 解释序列锁戳记为什么会回绕、回绕为什么会撕裂读，以及 `seq_lock_wide.rs` 用 `(state_hi, state_lo)` 双计数器如何挡住它。

## 2. 前置知识

### 2.1 内存序（memory ordering）速记

现代 CPU 为了快，会乱序执行指令、缓存数据；编译器也会重排代码。内存序就是程序员写给硬件/编译器的「契约」，规定「这一次原子操作前后的普通读写，能不能被挪到它的另一侧」。Rust 标准库 `core::sync::atomic::Ordering` 提供了 `Relaxed / Release / Acquire / AcqRel / SeqCst` 这几种，**唯独没有 `Consume`**。本讲的全部动机，都来自「std 缺了这个序，crossbeam 来补」。

- **`Release`（写端）**：本次 store 之前的所有读写，对拿到这个值的线程都可见。
- **`Acquire`（读端）**：本次 load 之后的所有读写，都不会被重排到 load 前面；它和 `Release` 配对，构成「发布—获取」同步。
- **`Consume`（读端，本讲主角）**：比 `Acquire` 更弱也更便宜——它只对「**依赖**这次载入结果」的操作排序。

> 关键直觉：`Acquire` 是一面「挡住后面所有读写」的墙；`Consume` 只挡住「顺着这次载入的值往下走」的那条依赖链，墙外的读写可以自由穿越，因此省掉了 fence 指令。

### 2.2 「依赖」是什么意思

```text
let p = atomic.load(Consume);   // 读出一个指针
let x = (*p).field;             // 这一句「依赖」p —— 没拿到 p 就没法解引用
```

`(*p).field` 用到了 `p` 的值，硬件层面这是一条**地址依赖（address dependency）**。几乎所有 CPU 都会老老实实地「先算出地址再访问内存」，不会把 `(*p).field` 提前到 `load` 之前——因为不提前 load 根本不知道地址。所以这种依赖链**天然有序**，不需要额外的 fence。`Consume` 就是利用这一点，把 `Acquire` 那条昂贵的 fence 省掉。

> ⚠️ 理论上「依赖」可以包含控制依赖，但 C++/Rust 标准对 consume 的定义长期有争议（即所谓的 "consume 问题"），所以 Rust 至今没有稳定 `Ordering::Consume`。crossbeam 用工程手段给出了一个「在真实硬件上等价、在 Linux 内核等大量软件中久经验证」的实现。

### 2.3 序列锁戳记（回顾 u2-l3）

`SeqLock` 把锁状态编码进一个整数 `state`：最低位是「写锁位」（`1` 表示被占），其余位是「戳记」。每完成一次写，戳记 `wrapping_add(2)`——`+2` 是因为要跳过锁位，保持戳记永远是偶数。读者用「乐观读」：先记下戳记、读数据、再校验戳记没变。戳记变了，说明读期间有人写过，重试即可。

这个「戳记」是一个**单调递增（取模回绕）的计数器**，而它会不会回绕得太快，正是本讲下半场的核心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crossbeam-utils/src/atomic/consume.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs) | 定义 `AtomicConsume` trait 与 `load_consume`，用 `impl_consume!` 宏分两条分支实现，再用 `impl_atomic!` 宏批量给所有原生原子类型实现。 |
| [crossbeam-utils/src/atomic/seq_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs) | 基础序列锁：单个 `AtomicUsize` 既当戳记又当锁位。64 位平台上用它。 |
| [crossbeam-utils/src/atomic/seq_lock_wide.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs) | 宽序列锁：用 `state_hi` + `state_lo` 两个 `AtomicUsize` 拼成更宽的计数器，防止 16/32 位平台戳记回绕。 |
| [crossbeam-utils/src/atomic/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs) | 用 `cfg_attr(path = ...)` 在指针宽度 ≤32 时把 `seq_lock` 模块替换成 `seq_lock_wide.rs`。 |
| [crossbeam-utils/src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) | 序列锁的真实消费者：`atomic_load` 的乐观读路径。 |

> 提示：`seq_lock.rs` 与 `seq_lock_wide.rs` 都定义了一个**同名** `pub(crate) struct SeqLock`，且对外接口（`optimistic_read / validate_read / write / abort`）逐一对齐。这正是 `mod.rs` 能用 `cfg_attr(path=...)` 透明替换的前提——下游 `atomic_cell.rs` 只认 `seq_lock::SeqLock` 这个名字，不关心它来自哪个文件。

## 4. 核心概念与源码讲解

### 4.1 consume 内存序：为什么需要比 acquire 更轻的读

#### 4.1.1 概念说明

在无锁数据结构里，最常见的一种读模式是：

```text
let p = head.load(Acquire);   // 读出一个指针
let next = (*p).next;         // 顺着指针读下一个节点
```

为了安全，读 `*p` 之前必须先「获取」发布者写入的那个指针及其指向的内容，所以传统写法用 `Acquire`。但在 ARM、AArch64 这类弱内存模型上，`Acquire` 载入往往要插一条 `dmb`/`ldar` 之类的屏障或特殊版式的载入指令，代价不低。

而上面这段代码里，`(*p).next` **天然依赖** `p` 的值（地址依赖）。硬件本来就保证「拿到地址之后才访问该地址」，所以这条依赖链是**自动有序**的——我们真正需要的，只是阻止**编译器**把依赖读提前或优化掉。换句话说：

> 我们不需要硬件 fence，只需要 compiler fence。

这正是 `consume` 的语义：**只对依赖这次载入的操作排序**。它比 `acquire` 弱（不挡无关读写），但在「指针→解引用」这类典型 RCU（Read-Copy-Update）读路径上语义恰好够用，且在弱模型架构上能省掉 fence。

#### 4.1.2 核心流程

`consume` 与 `acquire` 的区别可以用一张对比图概括：

```text
acquire load：┃──── 一面挡住「之后所有读写」的墙 ────（需要硬件 fence）
                ↑ 其后的任何读写都不能越过

consume load：  p = load ──► (*p).x ──► (*p).x.y   ← 只沿依赖链排序
              （依赖链外的读写可自由穿越，无需硬件 fence）
```

形式化地，对一个原子值 `A`：

- `A.store(v, Release)` 之后，任何**携带依赖**到 `A.load(Consume)` 结果的操作，都能看到 store 之前的写；
- 不携带依赖的操作，不保证顺序。

#### 4.1.3 源码精读

`consume.rs` 顶部的文档注释把这套动机讲得非常清楚，是本讲最重要的「官方解释」：

[consume.rs:L9-L24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L9-L24) — 注释说明 `consume` 类似 `acquire`，但只对「依赖载入结果」的操作排序；在弱内存模型架构上通常快得多，因为不需要内存 fence；并诚实地点明「依赖」的定义有点模糊，但实践中（尤其 Linux 内核）久经验证。

而 `AtomicConsume` trait 本身极简，只有一个关联类型和一个方法：

[consume.rs:L5-L25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L5-L25) — 定义 `pub trait AtomicConsume` 与 `fn load_consume(&self) -> Self::Val`。这是 crossbeam 给 std 补的「缺失的 consume 序」入口。

#### 4.1.4 代码实践

**实践目标**：建立对 `consume` vs `acquire` 语义差异的直觉。

**操作步骤**：

1. 读上面 [consume.rs:L9-L24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L9-L24) 的文档注释原文。
2. 找一张纸，画出下面两段伪代码在「弱内存模型 CPU」上，`acquire` 与 `consume` 各自需要的屏障：

   ```text
   // 版本 A：acquire
   let p = head.load(Acquire);
   let x = (*p).value;

   // 版本 B：consume
   let p = head.load_consume();
   let x = (*p).value;
   ```

**需要观察的现象**：版本 A 在 ARM/AArch64 上 `load(Acquire)` 会落到一条带 acquire 语义的载入指令（如 `ldar`）；版本 B 只是普通载入 + 编译器栅栏，机器码里**没有**硬件 fence。

**预期结果**：能用自己的话说出「依赖链天然有序，所以 consume 能省 fence」。

**待本地验证**：若你有 ARM/AArch64 设备，可写一段含 `load_consume` 的小程序，`cargo rustc --release -- --emit asm` 查看汇编，确认没有 `dmb` 指令（相对 `load(Acquire)` 版本）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `let x = (*p).value;` 改成 `let x = GLOBAL;`（与 `p` 无关的全局变量），consume 还能保证 `x` 读到「发布者写入之后」的值吗？

**答案**：不能。`GLOBAL` 不携带对 `p` 的依赖，consume 不对依赖链外的读排序，因此 `x` 可能看到旧值。这正是 consume 弱于 acquire 的地方——acquire 会挡住其后所有读，consume 只挡依赖读。

**练习 2**：为什么 Rust 标准库 `Ordering` 没有 `Consume` 变体，crossbeam 却要自己提供？

**答案**：C++/Rust 对 consume 的形式化定义长期有争议（"consume 问题"），Rust 故意没有稳定它。而真实硬件（尤其 ARM 系）对地址依赖有明确保证，Linux 内核等大量软件长期依赖此行为。crossbeam 用 `Relaxed + compiler_fence` 的工程实现补上了这个「标准库缺失但实践中需要」的能力。

---

### 4.2 AtomicConsume trait 与 load_consume 的批量实现

#### 4.2.1 概念说明

有了 trait（4.1），还要给它「装」到所有原生原子类型上：`AtomicBool / AtomicUsize / AtomicPtr<T>` 等等。手写一遍不难，但太啰嗦。crossbeam 用一个宏 `impl_atomic!` 批量实现，核心逻辑（怎么读）则抽到另一个宏 `impl_consume!` 里——后者有两份定义，按编译目标二选一。

#### 4.2.2 核心流程

```text
impl_atomic!(AtomicU64, u64)
   └─► impl AtomicConsume for AtomicU64 { type Val = u64; impl_consume!(); }
                                                              │
                       ┌──────────────────────────────────────┴───────────┐
                       ▼（cfg 选其一）                                     ▼
            ARM/AArch64（非 miri/loom/tsan）                       其它所有平台
            load(Relaxed) + compiler_fence(Acquire)              load(Acquire)
```

#### 4.2.3 源码精读

`impl_atomic!` 宏负责「对每个原子类型 + 对应值类型」实现 trait，并为 loom 模型额外实现一份：

[consume.rs:L64-L77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L64-L77) — 对 `core::sync::atomic::$atomic` 实现 `AtomicConsume`（带 `crossbeam_no_atomic` 门控），并在 `crossbeam_loom` 下对 `loom::sync::atomic::$atomic` 也实现一份。`impl_consume!()` 在此处展开成方法体。

随后是一长串 `impl_atomic!` 调用，覆盖所有定宽数数类型，注意大位宽类型还带了 `target_has_atomic` 门控（呼应 u1-l2 讲过的「原子能力档位」）：

[consume.rs:L79-L99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L79-L99) — 批量给 `AtomicBool/AtomicUsize/.../AtomicU64/AtomicI64` 实现 consume。`AtomicU32` 等带 `#[cfg(any(target_has_atomic = "32", not(target_pointer_width = "16")))]`，意味着在连 32 位原子都没有的目标上不提供。

`AtomicPtr<T>` 单独实现（因为它带泛型 `T`，没法塞进 `impl_atomic!` 的定参宏里）：

[consume.rs:L101-L105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L101-L105) — 对 `AtomicPtr<T>` 实现 `AtomicConsume`，`type Val = *mut T`。

> 这也是本讲实践任务里「为自定义 `AtomicPtr` 包装实现 consume 读」的依据：标准库的 `AtomicPtr` 已经实现了 `AtomicConsume`，我们只需 `use crossbeam_utils::AtomicConsume` 就能调 `.load_consume()`。

#### 4.2.4 代码实践

**实践目标**：验证 `AtomicPtr` 开箱即得 `load_consume`，并对比 `Acquire` 读取。

**操作步骤**：

1. 在一个依赖 `crossbeam-utils` 的 crate 里写：

   ```rust
   // 示例代码（非项目原有代码）
   use crossbeam_utils::atomic::AtomicConsume;
   use std::sync::atomic::AtomicPtr;

   let a: AtomicPtr<u8> = AtomicPtr::new(core::ptr::null_mut());
   let _via_consume = a.load_consume();           // 走 AtomicConsume
   let _via_acquire = a.load(std::sync::atomic::Ordering::Acquire); // 走 std
   ```

2. 对照 [consume.rs:L101-L105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L101-L105) 确认 `AtomicPtr` 确实有 `load_consume`。

**需要观察的现象**：两种读法都能编译；`load_consume` 返回 `*mut u8`，与 `load(Acquire)` 类型一致。

**预期结果**：理解「`AtomicConsume` 是 std 原子类型的一个 extension trait，`use` 进来即可用」。

**待本地验证**：编译运行确认无 warning。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AtomicPtr<T>` 不能像 `AtomicU64` 那样用 `impl_atomic!(AtomicPtr, *mut T)` 一行搞定？

**答案**：`AtomicPtr<T>` 带泛型参数 `T`，而 `impl_atomic!` 是为定参类型设计的非泛型宏。泛型 impl 需要写 `impl<T> AtomicConsume for AtomicPtr<T>`，宏形式不便表达，所以单独手写。

**练习 2**：`impl_atomic!` 里为什么同时给 `loom::sync::atomic::$atomic` 实现一份？

**答案**：loom 是并发模型检查器（见 u7-l3），它用自己的原子类型替代 std 的。为了让被测代码在 loom 下也能调 `load_consume`，必须对 loom 的原子类型同样实现 `AtomicConsume`。

---

### 4.3 ARM/AArch64 上的优化实现与 cfg 分发

#### 4.3.1 概念说明

`load_consume` 真正的「魔法」不在 trait，而在 `impl_consume!` 宏的**两条分支**：在 ARM/AArch64 上用「`Relaxed` 载入 + `compiler_fence(Acquire)`」省掉硬件 fence；在其它架构（包括 x86）退化为普通 `load(Acquire)`。

为什么只在这两个架构省 fence？因为只有它们的硬件对地址/数据依赖有 crossbeam 所需的保证，且 LLVM 在 `compiler_fence(Acquire)` 时**不会**把它降级成硬件 fence。注释里诚实地说：在 PowerPC、MIPS 等架构上，LLVM 会把 `compiler_fence(Acquire)` 生成成等价于硬件 `fence(Acquire)` 的指令（参见注释里的 godbolt 链接），那就没便宜可占了，不如直接用 `Acquire`。

#### 4.3.2 核心流程

`impl_consume!` 的选择由一个层层叠加的 `cfg` 门控：

```text
是否 ARM 或 AArch64？
   且 不是 miri / crossbeam_loom / crossbeam_sanitize_thread？
        ├─ 是 ─► 优化分支：Relaxed + compiler_fence(Acquire)
        └─ 否 ─► 回退分支：load(Acquire)
```

为什么测试工具（miri/loom/tsan）要强制走回退分支？注释解释了三件事：

- **Miri** 和 **Loom** 不支持 consume 语义；
- **ThreadSanitizer** 不把 `load(Relaxed) + compiler_fence(Acquire)` 当作 consume，会误报数据竞争；
- 所以在这些工具下统一退回 `Acquire`，保证检测结果可信。

#### 4.3.3 源码精读

优化分支——本讲的高光时刻：

[consume.rs:L27-L48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L27-L48) — `cfg` 门控 + `impl_consume!` 宏。条件是 `any(target_arch = "arm", target_arch = "aarch64")` 且 `not(any(miri, crossbeam_loom, crossbeam_sanitize_thread))`。宏展开后的方法体是：

```rust
let result = self.load(Ordering::Relaxed);
compiler_fence(Ordering::Acquire);
result
```

注释（L27-L37）解释了为何在 PowerPC/MIPS 等架构上 `compiler_fence(Acquire)` 会被 LLVM 降级成硬件 fence，所以这个优化**实际只在 ARM/AArch64 上生效**。

> 注意 `compiler_fence` 来自 `crate::primitive::sync::atomic::compiler_fence`——`primitive` 是 crossbeam 内部为 loom 等场景抽象出的「原子原语」门面（见 u7-l3 的 loom 抽象）。

回退分支——覆盖所有「不能省 fence」的情况：

[consume.rs:L50-L62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L50-L62) — 在不满足上面 `cfg` 的所有目标（x86、PowerPC、MIPS，以及任何架构下的 miri/loom/tsan）上，`load_consume` 直接 `self.load(Ordering::Acquire)`。这是「宁可慢一点，也要正确」的保守选择。

#### 4.3.4 代码实践

**实践目标**：亲手写一个「自定义 AtomicPtr 包装 + consume 读」，并在注释里对比 acquire 与 consume 的语义差异。

**操作步骤**：

1. 新建一个依赖 `crossbeam-utils = { version = "0.8", default-features = false }` 的小 crate（注意关掉 default-features 才能在 no_std 下用，呼应 u1-l2）。
2. 写如下包装（**示例代码，非项目原有代码**）：

   ```rust
   use crossbeam_utils::atomic::AtomicConsume;
   use std::sync::atomic::{AtomicPtr, Ordering};

   /// 一个发布 / 订阅风格的指针槽：写端 Release 发布，读端 Consume 获取。
   pub struct PubPtr<T> {
       inner: AtomicPtr<T>,
   }

   impl<T> PubPtr<T> {
       pub fn new(p: *mut T) -> Self {
           Self { inner: AtomicPtr::new(p) }
       }

       /// 发布一个新指针（写端）。
       pub fn publish(&self, p: *mut T) {
           // Release：保证构造 *p 的所有写 在 这条 store 之前完成。
           self.inner.store(p, Ordering::Release);
       }

       /// 读出当前指针（读端）。
       /// - 用 consume：(*p).field 这类「依赖读」天然有序，弱模型架构上省 fence。
       /// - 若改用 load(Acquire)：会挡住其后所有读，更安全但更贵。
       pub fn read(&self) -> *mut T {
           self.inner.load_consume()
       }
   }
   ```

3. 在 `main` 里：线程 A 构造一个 `Box` 并 `publish`，线程 B 循环 `read()` 直到非空，再解引用读取其字段。

**需要观察的现象**：在 x86 上 `read()` 与 `load(Acquire)` 行为一致（x86 的 TSO 本身就强）；在 ARM/AArch64 上 `read()` 的汇编里没有硬件 fence。

**预期结果**：能说清——`publish` 的 `Release` 与 `read` 的 `Consume` 配对，保证 B 看到 `*p` 的内容；区别仅在于 consume 不挡依赖链外的读。

**待本地验证**：消费方读到非空后解引用是否一定安全，取决于你是否保证「发布后 `*p` 不被并发释放」——本示例只演示读序，**不**演示内存回收（那是 u5 epoch 的主题）。完整安全需要 epoch 保护。

#### 4.3.5 小练习与答案

**练习 1**：把上面的 `read()` 从 `load_consume()` 换成 `inner.load(Ordering::Relaxed)`，会发生什么？

**答案**：会出问题。`Relaxed` 完全不排序，编译器可能把 `(*p).field` 这种依赖读重排或优化掉（比如把 `p` 当成不变的常量缓存），即便硬件层面地址依赖天然存在，编译期重排也足以破坏正确性。这正是为什么 `impl_consume!` 在 `load(Relaxed)` 之后必须紧跟一句 `compiler_fence(Acquire)`。

**练习 2**：为什么 `impl_consume!` 要写成两个互斥的宏定义，而不是一个带 `#[cfg]` 的函数？

**答案**：宏在调用点（`impl_atomic!` 内）文本展开，`cfg` 决定「展开成哪段方法体」。若写成一个函数，内部的 `cfg` 只能选择某几行编译，而这里整段方法体（`Relaxed + compiler_fence` vs `Acquire`）都要整体替换，用两个宏定义 + 互斥 `cfg` 最清晰，也便于在 loom 下复用同一套宏。

---

### 4.4 序列锁的计数回绕与 seq_lock_wide 宽计数器防护

#### 4.4.1 概念说明

现在转到本讲下半场：序列锁的戳记。

[u2-l3](u2-l3-atomic-cell.md) 讲过，乐观读靠「载入前记戳记 → 读数据 → 校验戳记是否变化」来判断读期间有没有被写。这套机制**绝对依赖一个前提**：在我读数据这段时间里，戳记要么不变（没被写），要么变成一个**不同的偶数**（被写过）。可一旦戳记是个有限位宽的整数，它就会 `wrapping_add(2)` 地一圈圈转——万一在我「记戳记 → 读数据 → 校验」的间隙里，它整整转了 \(2^{k-1}\) 圈回到原值，校验就会**误判为「没被写」**，于是读者吞下一个撕裂的（torn）值。

这就是**计数回绕（counter wraparound）**问题。

#### 4.4.2 核心流程

先把戳记位宽与回绕阈值算清楚。`SeqLock::state` 是一个 `AtomicUsize`，戳记占除最低位外的所有位，每次写 `+2`，所以戳记取遍所有偶数值，回绕周期为：

\[
\text{回绕所需写次数} = 2^{\,k-1}, \quad k = \text{usize 的位宽}
\]

| 平台 `usize` 位宽 \(k\) | 回绕阈值 \(2^{k-1}\) | 直观感受（按每秒 \(10^8\) 次写） |
| --- | --- | --- |
| 16 位 | \(2^{15} = 32768\) | 极易回绕 |
| 32 位 | \(2^{31} \approx 2.1\times10^9\) | 约 21 秒回绕一次 |
| 64 位 | \(2^{63} \approx 9.2\times10^{18}\) | 约 292 年，实质永不回绕 |

> 注意：乐观读窗口通常只有几条指令、纳秒级，而回绕需要戳记在**同一个窗口内**整整转一圈才会误判。所以回绕危害 = 「写频率 × 窗口长度」是否逼近回绕周期。64 位周期 \(2^{63}\) 实际不可达；32 位 \(2^{31}\) 在高频写入的长跑服务里**有真实风险**。

`seq_lock_wide.rs` 的对策：用 **两个** `AtomicUsize`——`state_lo` 仍是原来的「戳记 + 锁位」（行为与 `seq_lock.rs` 完全一致），`state_hi` 专门记录 `state_lo` 回绕的次数。乐观读返回元组 `(hi, lo)`，校验时比较整个元组，把有效位宽从 \(k\) 拓宽到 \(2k\)。

#### 4.4.3 源码精读

先看 `mod.rs` 怎么在编译期把两个文件「换皮」：

[mod.rs:L6-L19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L6-L19) — 注释说明「指针宽度 ≤32 时用 wide 版序列锁防止计数回绕」；并用 `#[cfg_attr(any(target_pointer_width = "16", target_pointer_width = "32"), path = "seq_lock_wide.rs")] mod seq_lock;`。即：16/32 位目标时，`mod seq_lock` 的源来自 `seq_lock_wide.rs`；否则来自 `seq_lock.rs`。两者都导出同名 `SeqLock`，下游无感。注释还诚实地说：16 位目标上即便 wide 版，`state_lo` 仍是 16 位，仍有回绕风险，但「这种原始硬件上计数不会增长那么快」，属于务实的折中。

再看基础版（64 位平台用）的戳记，理解回绕从何而来：

[seq_lock.rs:L9-L15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L9-L15) — `SeqLock` 只有一个 `state: AtomicUsize`，注释说明「除最低位外都是戳记；锁定时 state == 1，不含合法戳记」。

它的释放逻辑就是回绕的源头：

[seq_lock.rs:L85-L93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L85-L93) — `Drop` 里 `store(self.state.wrapping_add(2), Release)`。`wrapping_add` 意味着到了 \(2^{k-1}\) 次写之后会从最大偶数绕回 `0`，戳记重复。

现在看 wide 版的结构：

[seq_lock_wide.rs:L12-L21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L12-L21) — `SeqLock` 拆成 `state_hi`（高位）与 `state_lo`（低位，含锁位），各是一个 `AtomicUsize`。

它的乐观读返回元组：

[seq_lock_wide.rs:L34-L49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L34-L49) — 返回 `Option<(usize, usize)>`。注释解释 acquire 载入两个分量，与 `Drop` 的 release store 同步，从而保证「能读到临界区之前的所有写」。

校验逻辑是 wide 版最精巧的地方，它要处理「lo 恰好相同但其实是回绕」的情况：

[seq_lock_wide.rs:L55-L77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L55-L77) — 先 `fence(Acquire)`，再读 `state_lo`（Acquire）和 `state_hi`（Relaxed），最后比较 `(state_hi, state_lo) == stamp`。注释分两种情形：(1) `state_lo` 没变且确实没被写过；(2) `state_lo` 绕回来了——此时 acquire 保证能看到新的 `state_hi`，只要 `state_hi` 与记下的不同，就能识别出「这是回绕而非静止」，返回 `false` 让读者重试。只有当 `hi` 与 `lo` **同时**绕回（需要写 \(2^{2k-2}\) 次）才会误判，阈值平方级放大，32 位上变为 \(2^{62}\)，实质安全。

最后看「回绕时如何累加高位」：

[seq_lock_wide.rs:L120-L140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L120-L140) — `Drop`：先 `state_lo = self.state_lo.wrapping_add(2)`；**当 `state_lo == 0`（即低位刚好绕回）时**，把 `state_hi` 加 1（Release）。这保证了「每绕回一圈，高位 +1」，从而校验阶段的「`state_hi` 不同 ⇒ 发生过回绕」推理成立。

#### 4.4.4 代码实践

**实践目标**：阅读 `seq_lock_wide.rs`，回答「为什么指针宽度 ≤32 需要 wide 版本」。

**操作步骤**：

1. 读 [seq_lock.rs:L85-L93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L85-L93) 的 `wrapping_add(2)`，确认戳记会回绕。
2. 读 [seq_lock_wide.rs:L120-L140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs#L120-L140)，在草稿纸上模拟：设 `state_lo` 为 4 位（仅示意，实际是 usize），戳记序列为 `0,2,4,6,8,10,12,14,0,2,...`，标注每一次回到 `0` 时 `state_hi` 自增。
3. 用 [mod.rs:L6-L19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L6-L19) 的注释佐证你的结论。

**需要观察的现象**：单计数器在 32 位下 \(2^{31}\) 次写回绕；双计数器把误判阈值抬到约 \(2^{62}\)。

**预期结果**：能写出一段话——「32 位 `usize` 上，单戳记回绕周期只有约 21 亿次写（按每秒 1 亿次写约 21 秒），乐观读窗口若与回绕重叠会撕裂读；wide 版用 `state_hi` 记录回绕次数，把误判周期平方到约 \(2^{62}\)，实质消除该风险。16 位虽然 wide 版 `state_lo` 仍只有 16 位，但 16 位硬件写频率极低，回绕实际不会发生，故注释判定『mostly okay』。」

**待本地验证**：可写一个 32 位目标（如 `i686-unknown-linux-gnu`）的压测，多线程狂写一个 `AtomicCell<u64>`（触发序列锁路径）数十秒，用断言检查读到的值是否始终合法（此为**待本地验证**的压力实验，本讲义未实际运行）。

#### 4.4.5 小练习与答案

**练习 1**：`seq_lock_wide.rs` 的 `optimistic_read` 里，`state_hi` 和 `state_lo` 都用 `Acquire` 载入；而 `validate_read` 里 `state_hi` 却用 `Relaxed`。为什么 `state_hi` 在校验阶段可以放宽？

**答案**：`validate_read` 的 `state_lo` 用 `Acquire`，其同步效果已足以让我们在「lo 绕回」时看到新的 `state_hi`（因为 `state_hi` 的 release store 发生在 `state_lo` 绕回那次 `Drop` 里，先于 `state_lo` 的 release store，而 acquire 读 `state_lo` 会一并看到之前的 `state_hi` 写）。所以再读 `state_hi` 时用 `Relaxed` 即可，省一次屏障。

**练习 2**：假设有人提议「干脆所有平台都用 `seq_lock_wide`，省得维护两份」。这个提议有什么缺点？

**答案**：`seq_lock_wide` 每次乐观读要载入**两个**原子量、校验一个二元组，开销大于单计数器版。64 位平台单戳记回绕周期 \(2^{63}\) 实质永不回绕，完全没有 wide 的必要，用它只会白白增加读延迟。因此 crossbeam 选择「按指针宽度编译期分流」，各取其优。

**练习 3**：`seq_lock.rs` 与 `seq_lock_wide.rs` 的 `abort` 方法都做了 `mem::forget(self)`，为什么？

**答案**：`abort` 表示「放弃这次写，不推进戳记」。它先把 `state`（或 `state_lo`）恢复成加锁前的旧值，再用 `mem::forget` 阻止 `Drop` 运行——因为 `Drop` 会 `wrapping_add(2)` 推进戳记。`forget` 是「跳过析构」的标准手法，呼应 u2-l3 里「abort 不推进 stamp，避免连累其它读者」的设计。

---

## 5. 综合实践

把本讲的两条线串起来：**用 consume 读一个指针槽，而该指针槽内部值的并发一致性由序列锁守护**。

**任务**：实现一个极简的「带版本号的双字段快照」`VersionedPair`，体现本讲两个主题。

```rust
// 示例代码（非项目原有代码），用于串联本讲概念
use crossbeam_utils::atomic::AtomicConsume;
use std::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};

/// 一个可被并发更新的 (u64, u64) 快照，附带一个版本号。
/// - 版本号用序列锁戳记思想：写端 +2，读端乐观读校验。
/// - 指针槽用 consume 读：读到指针后顺依赖解引用。
pub struct VersionedPair {
    // 真实工程里这两字段的并发安全会落到 SeqLock；
    // 这里用原子版本号示意「戳记 +2」与「乐观读校验」的思想。
    version: AtomicUsize,
    a: AtomicUsize,
    b: AtomicUsize,
}

impl VersionedPair {
    pub fn new(x: u64, y: u64) -> Self {
        Self {
            version: AtomicUsize::new(0),
            a: AtomicUsize::new(x as usize),
            b: AtomicUsize::new(y as usize),
        }
    }

    /// 写端：进入临界区（version 置奇=加锁）→ 写两字段 → 退出（version +1 变偶，等价于 +2 的简化）。
    pub fn write(&self, x: u64, y: u64) {
        // 简化演示：用 swap 占锁，退出时 +2（与 seq_lock.rs 的 wrapping_add(2) 同构）。
        let _g = self.version.fetch_add(1, Ordering::Acquire); // 进入：变奇数
        std::sync::atomic::fence(Ordering::Release);
        self.a.store(x as usize, Ordering::Relaxed);
        self.b.store(y as usize, Ordering::Relaxed);
        self.version.fetch_add(1, Ordering::Release); // 退出：再 +1 变偶数
    }

    /// 读端：乐观读——记版本 → 读字段 → 校验版本。
    pub fn read(&self) -> Option<(u64, u64)> {
        let v1 = self.version.load(Ordering::Acquire);
        if v1 & 1 == 1 {
            return None; // 正被写
        }
        let a = self.a.load(Ordering::Relaxed);
        let b = self.b.load(Ordering::Relaxed);
        std::sync::atomic::fence(Ordering::Acquire);
        let v2 = self.version.load(Ordering::Acquire);
        if v1 == v2 {
            Some((a as u64, b as u64))
        } else {
            None // 读期间被写过，重试
        }
    }
}
```

**要求**：

1. 对照 [seq_lock.rs:L27-L41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L27-L41) 的 `optimistic_read / validate_read`，说明上面 `read()` 是序列锁乐观读的等价骨架。
2. 思考：若把 `VersionedPair` 改成持有一个 `AtomicPtr<Snapshot>`，写端 `Release` 发布、读端用 `load_consume` 读指针，能省掉哪一道 fence？在哪种架构上省？
3. 进阶：把版本号字段想象成 32 位，解释为什么本综合实践的「乐观读」在长期高频写下也需要类似 `seq_lock_wide` 的宽计数器保护。

**预期结果**：能讲清楚 consume（指针槽读序）与 SeqLock（多字段一致性）各自负责什么，并指出宽计数器是后者的安全补丁。

**待本地验证**：多线程压测 `write`/`read` 是否会出现 `read()` 永远返回 `None`（饥饿）或返回不一致 `(a,b)`——本讲义未实际运行。

## 6. 本讲小结

- Rust 标准库的 `Ordering` **没有 `Consume`**；`AtomicConsume` trait 及 `load_consume` 是 crossbeam 给 std 补的「按依赖链排序」的轻量读序。
- `consume` 比 `acquire` 弱：它只对「携带依赖」到载入结果的操作排序，因此弱内存模型架构上可省硬件 fence；典型场景是 RCU 式的「读指针→解引用」。
- `impl_consume!` 宏有两条 `cfg` 分支：ARM/AArch64（且非 miri/loom/tsan）用 `load(Relaxed) + compiler_fence(Acquire)`；其余目标退化为 `load(Acquire)`。miri/loom/tsan 不认 consume，必须走回退分支。
- 序列锁靠一个 `wrapping_add(2)` 的戳记判断「读期间是否被写」。戳记位宽有限会**回绕**，回绕若发生在乐观读窗口内，会让读者误判、吞下撕裂值。
- 64 位平台戳记周期 \(2^{63}\)，实质不回绕，用单计数器 `seq_lock.rs`；16/32 位平台用 `seq_lock_wide.rs` 的 `(state_hi, state_lo)` 双计数器，把误判阈值平方，并由 `mod.rs` 的 `cfg_attr(path=...)` 在编译期透明换皮。

## 7. 下一步学习建议

- 本讲的 consume 序在真实无锁结构里大量使用。接下来进入 [u2-l5 Parker](u2-l5-parker.md)，看「自旋退避 + 阻塞唤醒」如何组合成线程停放原语，那是 u2-l1（Backoff）三段式等待的最后一段「阻塞」的落地。
- 若你想直接看 consume 在数据结构里的实战，可以跳到 [u7-l2 跳表操作与 epoch 集成](u7-l2-skiplist-ops.md)：`crossbeam-skiplist/src/base.rs` 的 `search_bound` 大量调用 `load_consume`（见 `base.rs` 第 795、804、869… 行），是无锁遍历依赖链的典范——但需要先学 [u5 crossbeam-epoch](u5-l1-epoch-overview.md) 才能读懂其安全性。
- 想验证本讲的并发正确性，留到 [u7-l3 测试、loom 与并发正确性](u7-l3-testing-concurrency-correctness.md)：用 miri/tsan 检测 consume 与序列锁的实现是否存在数据竞争与 UB。
