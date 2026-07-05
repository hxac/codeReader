# AtomicCell 算术运算的宏生成

## 1. 本讲目标

在前面的讲义里，我们已经从两个角度看过 `AtomicCell<T>`：

- u2-l1 讲清了它的公共 API，并提到 `fetch_add`/`fetch_sub` 这些「读改写（read-modify-write, RMW）」方法只对**整数类型**存在。
- u2-l2 讲清了 `atomic!` 宏如何在编译期于「无锁路径」与「全局锁回退」之间二选一。
- u5-l1 则把 `atomic_load`/`atomic_compare_exchange_weak` 等自由函数里的 `unsafe` 逐一做了安全性论证。

但有一个问题一直被搁置：**`fetch_add`/`fetch_sub`/`fetch_and`/`fetch_or`/`fetch_max`/`fetch_min`…… 这一大堆整数方法，到底是怎么写出来的？** 它们显然不能为每一种整数类型（`u8`/`i8`/`u16`/`i16`/…/`usize`/`isize`，共 12 种）各手写一遍——那会是 12 × 8 = 96 个几乎雷同的方法。

本讲要回答：**这些方法是用一个名叫 `impl_arithmetic!` 的声明宏批量生成的。** 读完本讲，你应当能够：

1. 读懂 `impl_arithmetic!` 宏的**模板签名**，说出它的三个参数 `$t` / `$target_has_atomic` / `$atomic` / `$example` 各自的作用，并解释它如何把「一份模板」展开成「12 个类型的完整 `impl` 块」。
2. 画出**任意一个** `fetch_*` 方法（例如 `fetch_max`）的**三层回退阶梯**：原生原子路径、`fetch_update` CAS 回退、全局 SeqLock 回退，并精确指出每一层由哪条 `cfg` 控制、在什么目标上才会被编译进二进制、在什么目标上才会真正执行。
3. 解释为什么 `AtomicCell<bool>` 的逻辑运算（`fetch_and`/`fetch_or`/`fetch_xor`/`fetch_nand`）**不走 `impl_arithmetic!` 宏**，而是单独手写一个 `impl` 块，以及它与整数版 `fetch_*` 的三点关键差别。

本讲**不再重复** `atomic!` 宏与 `can_transmute` 的判定细节（那是 u2-l2 的内容），也不重讲 `SeqLock` 印戳机制（u2-l3/u5-l1），而是把它们当作已知工具，专注在**宏的批量生成机制**与**三层路径的 cfg 编排**上。

## 2. 前置知识

本讲假设你已读过 u2-l1 与 u2-l2。下面只做最小回顾。

- **声明宏（`macro_rules!`）**：Rust 的宏在编译期做「模式匹配 + 文本替换」。`macro_rules!` 定义的宏接收若干「片段（fragment）」，按 `($a:ty, $b:ident, ...)` 这样的「臂（arm）」模式匹配，再把模板里的 `$a`/`$b` 替换成实际传入的片段后展开成代码。`impl_arithmetic!` 正是一个为「多个类型批量生成 `impl` 块」而设计的声明宏。
- **`target_has_atomic = "N"`**：rustc 内置的编译期 cfg。当目标平台支持 N 位原子操作（`AtomicU8` 对应 `"8"`，`AtomicUsize` 对应 `"ptr"`）时为真。注意它与「是否有原子 CAS」是**两个维度**——`target_has_atomic = "N"` 关注的是「该宽度的原子类型存在」，而真正决定能否做原子「读改写」的是 `atomic_maybe_uninit::cfg_has_atomic_cas!`。
- **`cfg_has_atomic_cas!` / `cfg_has_atomic_N!`**：来自依赖 `atomic-maybe-uninit` 的两个宏。前者在「目标支持原子 CAS」时保留内部代码，后者在「目标支持 N 位原子」时保留内部代码。它们与 `target_has_atomic` 来自同一份 LLVM 目标能力描述，在 stable rustc 上**对齐**。
- **`fetch_update`**：定义在 `impl<T: Copy + Eq> AtomicCell<T>` 上的通用 CAS 循环（[src/atomic/atomic_cell.rs:299-312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L299-L312)）。它反复 `load` → 调用闭包算 `next` → `compare_exchange(prev, next)`，成功才返回。闭包可能被调用多次，但「写入」只生效一次。
- **`wrapping_add` / `wrapping_sub`**：整数「回绕（wrap-around）」算术。溢出不 panic，而是按位回绕。`fetch_add` 的语义是 *“The addition wraps on overflow”*，故用 `wrapping_add` 而非 `+`。
- **`atomic!` 宏**（u2-l2）：`atomic!{$t, $a, $atomic_op, $fallback_op}` 是一个 `loop { ... }`，依次尝试 `AtomicUnit` 与各宽度 `AtomicMaybeUninit<uN>` 候选；命中 `can_transmute` 就 `break $atomic_op`（无锁分支），全部不中就 `break $fallback_op`（全局锁分支）。本讲的每个 `fetch_*` 方法体都是对这个宏的一次调用。

> 关键直觉：`AtomicCell` 的整数 RMW 方法不是「一个函数里写 if-else 切换三条路径」，而是「**宏展开 + 编译期 cfg 裁剪**」共同把三条路径编织进**同一段源码模板**。最终编译进二进制的，往往只是其中一条。理解这一点，就读懂了 `impl_arithmetic!` 的全部精巧。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| `src/atomic/atomic_cell.rs` | 唯一主角。本讲的 `impl_arithmetic!` 宏定义、12 个调用点、`AtomicCell<bool>` 单独 `impl` 块，以及它们依赖的 `atomic!` 宏、`fetch_update`、`lock()` 全部在此文件。 |
| `src/atomic/seq_lock.rs` | 提供全局锁回退路径用到的 `SeqLock::write()` / `SeqLockWriteGuard`。本讲不重讲印戳机制，只引用它作为「第三层路径」的落点。 |
| `src/atomic/mod.rs` | 提供 `target_has_atomic = "ptr"` 这一**模块级门控**——它决定了「整个 `atomic_cell.rs` 文件是否参与编译」，是理解三条路径可达性的前提。 |

补充：`atomic_cell.rs` 整个模块被 `#[cfg(target_has_atomic = "ptr")]` 与 `#[cfg(not(crossbeam_loom))]` 双重门控（见 [src/atomic/mod.rs:21-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs#L21-L26)）。也就是说，**只要目标连指针宽度的原子都没有，`AtomicCell` 直接不存在**——这是后续讨论「三条路径在何种目标上出现」的硬前提。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应 spec 要求：

- **4.1 `impl_arithmetic!` 宏定义**：解释这个声明宏的模板签名、参数含义，以及它如何用一份模板批量生成 12 个类型的 `impl` 块。
- **4.2 单个 `fetch_*` 的三条路径**：以 `fetch_max` 为例，剖析每个方法内部「原生原子 / `fetch_update` / 全局锁」三层回退的 cfg 编排，并诚实指出各层的真实可达性。
- **4.3 `AtomicCell<bool>` 的逻辑运算特例**：解释 bool 为何另起一个 `impl` 块、它与整数算术宏的三点差别。

### 4.1 `impl_arithmetic!` 宏定义

#### 4.1.1 概念说明

`AtomicCell<T>` 对整数类型提供了 8 个 RMW 方法：

| 方法 | 语义 | 对应整数运算 |
| --- | --- | --- |
| `fetch_add` | 加，回绕 | `wrapping_add` |
| `fetch_sub` | 减，回绕 | `wrapping_sub` |
| `fetch_and` | 按位与 | `&` |
| `fetch_nand` | 按位与非 | `!(a & b)` |
| `fetch_or` | 按位或 | `\|` |
| `fetch_xor` | 按位异或 | `^` |
| `fetch_max` | 取最大 | `cmp::max` |
| `fetch_min` | 取最小 | `cmp::min` |

这些方法对 **12 种整数类型**（`u8`/`i8`/`u16`/`i16`/`u32`/`i32`/`u64`/`i64`/`u128`/`i128`/`usize`/`isize`）都适用，且**方法体除了「具体原子类型」与「doc 示例」外完全相同**。

如果手写，需要 12 × 8 = 96 个几乎一模一样的方法——典型的「复制粘贴地狱」，既难维护（改一处要改 96 处）又容易抄错。`impl_arithmetic!` 宏的目的是**用一份模板消除这 96 份重复**：把「随类型变化的部分」抽成宏参数，把「不变的方法骨架」写成模板。

#### 4.1.2 核心流程

`impl_arithmetic!` 的展开流程：

1. 宏接收四个片段：类型 `$t:ty`、一段带 cfg 的原子类型名 `#[cfg($target_has_atomic:meta)] $atomic:ident`、文档示例字符串 `$example:tt`。
2. 宏把 `$target_has_atomic` 作为 `:meta` 片段捕获——它能装下 `target_has_atomic = "8"` / `"16"` / … / `"ptr"` 这类完整的 cfg 谓词，之后在模板里用 `#[cfg($target_has_atomic)]` / `#[cfg(not($target_has_atomic))]` 复用它。
3. 宏体是 `impl AtomicCell<$t> { ... }`，里面定义全部 8 个方法。每个方法体里的「原生原子类型」写 `$atomic`、文档示例写 `$example`、整数运算写该类型对应的运算符。
4. 每调用一次 `impl_arithmetic!(...)`，就为**一个**具体类型生成一个完整的 8 方法 `impl` 块。
5. 文件末尾连续调用 12 次，得到 12 个类型的全部整数 RMW 方法。

伪代码：

```
macro impl_arithmetic($t, cfg($target_has_atomic), $atomic, $example) {
    impl AtomicCell<$t> {
        fn fetch_add(val) {
            atomic! { $t, _a,
                若 cfg($target_has_atomic):  a.fetch_add(...)        // 第一层
                若 not cfg($target_has_atomic): fetch_update(...)   // 第二层
            ,
                全局 SeqLock 读改写                                  // 第三层
            }
        }
        // ... fetch_sub / fetch_and / ... / fetch_min 同构 ...
    }
}

impl_arithmetic!(u8,     target_has_atomic="8",   AtomicU8,   "...");
impl_arithmetic!(i8,     target_has_atomic="8",   AtomicI8,   "...");
// ... 一直到 isize ...
```

#### 4.1.3 源码精读

先看宏的**签名臂**（[src/atomic/atomic_cell.rs:377-379](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L377-L379)）：

```rust
macro_rules! impl_arithmetic {
    ($t:ty, #[cfg($target_has_atomic:meta)] $atomic:ident, $example:tt) => {
        impl AtomicCell<$t> { /* 8 个方法 */ }
    };
}
```

要点：

- `$t:ty` 是目标整数类型（如 `u8`）。
- `#[cfg($target_has_atomic:meta)]` 这个写法很巧妙——它**把调用点上的整个 `#[cfg(...)]` 属性**当成一个 `:meta` 片段捕获。于是调用 `impl_arithmetic!(u8, #[cfg(target_has_atomic = "8")] AtomicU8, ...)` 时，`$target_has_atomic` 就是 `target_has_atomic = "8"`。模板里再写 `#[cfg($target_has_atomic)]` 就能逐字复用它。
- `$atomic:ident` 是原生原子类型名（`AtomicU8`）。
- `$example:tt` 是一个 token 树，这里是一段字符串字面量，用于注入到 doc 注释里（见下文）。

再看**文档注入**技巧（[src/atomic/atomic_cell.rs:386-393](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L386-L393)）：

```rust
/// ```
/// use crossbeam_utils::atomic::AtomicCell;
///
#[doc = $example]
///
/// assert_eq!(a.fetch_add(3), 7);
/// assert_eq!(a.load(), 10);
/// ```
```

`#[doc = $example]` 把调用点传入的字符串（如 `"let a = AtomicCell::new(7u8);"`）拼进 doc 测试。这样每个类型的文档示例都用**正确类型的字面量**（`7u8` / `7u16` / …），doc 测试能独立通过。这是宏比「复制粘贴」更高明的地方：连文档里的类型字面量都参数化了。

最后看**调用点**（[src/atomic/atomic_cell.rs:685-761](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L685-L761)），共 12 次。例如：

```rust
impl_arithmetic!(
    u8,
    #[cfg(target_has_atomic = "8")]
    AtomicU8,
    "let a = AtomicCell::new(7u8);"
);
// ... i8 / u16 / i16 / u32 / i32 / u64 / i64 ...
impl_arithmetic!(
    u128,
    #[cfg(any(/* always false */))]
    AtomicU128,
    "let a = AtomicCell::new(7u128);"
);
// ... i128 同上 ...
impl_arithmetic!(
    usize,
    #[cfg(target_has_atomic = "ptr")]
    AtomicUsize,
    "let a = AtomicCell::new(7usize);"
);
```

注意 `u128`/`i128` 两个调用点（[src/atomic/atomic_cell.rs:736-748](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L736-L748)）的 cfg 是 `#[cfg(any(/* always false */))]`——即**永远为假**。源码上方有注释 *“TODO: core::sync::atomic::AtomicU128 is unstable”*：因为标准库的 `AtomicU128` 尚未稳定，作者干脆把这条「原生原子路径」用恒假 cfg 关掉，使得 `AtomicCell<u128>` 的算术方法**永远不走第一层**，只能落到 `fetch_update` 或全局锁。这是一个用宏参数「条件性禁用某层路径」的巧妙手法。

#### 4.1.4 代码实践

**实践目标**：亲眼看到宏展开后的真实代码，确认「一次调用 → 一个完整 `impl` 块」。

**操作步骤**：

1. 在 `crossbeam-utils` 目录下，用 nightly 工具链的 `cargo-expand` 展开宏。
   ```bash
   cargo +nightly rustc --features atomic -p crossbeam-utils -- -Zunstable-options --pretty=expanded 2>/dev/null \
     | grep -A 25 "impl AtomicCell<u8>"
   ```
   若没有 nightly，可安装：`cargo +nightly install cargo-expand`，再 `cargo +nightly expand --features atomic | grep -A 25 "impl AtomicCell<u8>"`。
2. 在展开结果里找到 `impl AtomicCell<u8>` 块，确认它包含 `fetch_add`/`fetch_sub`/`fetch_and`/`fetch_nand`/`fetch_or`/`fetch_xor`/`fetch_max`/`fetch_min` 共 8 个方法。
3. 再找 `impl AtomicCell<usize>`，确认它也存在且结构相同，只是 `$atomic` 从 `AtomicU8` 换成了 `AtomicUsize`。

**需要观察的现象**：

- 展开后会出现 12 个 `impl AtomicCell<$t>` 块，每个都有 8 个方法——即约 96 个方法定义。
- `AtomicCell<u128>` 的 `fetch_add` 里，`#[cfg(any(/* always false */))]` 那一支被裁掉，只剩下 `not(...)` 分支（`fetch_update`）与全局锁分支。

**预期结果**：宏确实把一份模板展开成了 12 份几乎雷同的 `impl` 块。

**如果无法确定运行结果**：`cargo-expand` 依赖 nightly，若环境无 nightly，此项标注为「待本地验证」——可直接阅读宏源码 [src/atomic/atomic_cell.rs:377-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L377-L683) 与调用点 [src/atomic/atomic_cell.rs:685-761](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L685-L761) 推导展开结果。

#### 4.1.5 小练习与答案

**练习 1**：`impl_arithmetic!` 的第二个参数写成 `#[cfg($target_has_atomic:meta)]`，为什么不直接写 `$target_has_atomic:meta`（去掉前面的 `#[cfg(...)]`）？

> **参考答案**：写成 `#[cfg($target_has_atomic:meta)]` 是为了让调用点形如 `impl_arithmetic!(u8, #[cfg(target_has_atomic = "8")] AtomicU8, ...)`，把整个属性连同 `#[cfg(...)]` 外壳一起传入，使调用点的写法和普通字段上的 `#[cfg]` 属性完全一致、更自然。若去掉外壳只收 `$target_has_atomic:meta`，调用点就要写成裸的 `target_has_atomic = "8"`，读起来像「悬空谓词」，不如带 `#[cfg(...)]` 直观。两种写法功能等价，这里是**风格选择**。

**练习 2**：为什么 `u128`/`i128` 的调用点用 `#[cfg(any(/* always false */))]` 而不是直接删掉这两个调用点？

> **参考答案**：删掉调用点会导致 `AtomicCell<u128>` **完全没有** `fetch_add` 等方法，类型支持会出现「缺口」。用恒假 cfg 保留调用点，能让宏照常生成 `impl AtomicCell<u128>` 块、照常提供全部 8 个方法，只是把「原生原子层」编译期关掉，运行时自动落到 `fetch_update`/全局锁。这样 `AtomicCell<u128>` 仍可用（只是不无锁），并且一旦将来 `AtomicU128` 稳定，把 cfg 改回 `target_has_atomic = "128"` 即可一键启用，改动最小。

### 4.2 单个 `fetch_*` 的三条路径

#### 4.2.1 概念说明

每个由 `impl_arithmetic!` 生成的 `fetch_*` 方法，方法体都是**同一段模板**：一次 `atomic!` 宏调用，内含三层回退。这三层是「成本从低到高」的阶梯：

| 层 | 名称 | 机制 | 成本 |
| --- | --- | --- | --- |
| 第一层 | 原生原子 RMW | 直接调用原生原子类型的 `fetch_*`（如 `AtomicUsize::fetch_max`），编译成单条 CPU RMW 指令 | 最低：一条指令 |
| 第二层 | `fetch_update` CAS 回退 | 用 `fetch_update(|old| Some(op(old, val)))` 跑 CAS 循环，依赖 `compare_exchange` | 中：可能多次重试 |
| 第三层 | 全局 SeqLock 回退 | `lock(addr).write()` 取一把全局锁，普通读改写后释放 | 最高：加锁、可能自旋 |

关键在于：**这三层不是运行时 if-else，而是编译期 cfg 裁剪**。最终编译进二进制的，取决于「目标平台的能力」与「宏调用点传入的 cfg」。下面以 `fetch_max` 为例精确剖析。

#### 4.2.2 核心流程

一个 `fetch_*` 方法的执行决策分两道关：

**第一道关（外层 `atomic!` 宏）**：决定走「无锁分支 `$atomic_op`」还是「全局锁分支 `$fallback_op`」。

- `atomic!` 依次检查 `AtomicUnit` 与各宽度 `AtomicMaybeUninit<uN>` 候选，看 `can_transmute::<$t, 候选>()` 是否成立（要求 size 相等、align 不小于）。
- 若处于 `miri`/`crossbeam_loom`/`crossbeam_atomic_cell_force_fallback`，或目标不支持原子 CAS（`cfg_has_atomic_cas!` 为假），则**全部候选被 cfg 裁掉**，直接落到全局锁分支。
- 否则若某候选匹配，走无锁分支。

**第二道关（内层 `#[cfg($target_has_atomic)]`）**：仅在「无锁分支」被选中时才有意义，它再细分出两条：

- `#[cfg($target_has_atomic)]` 为真：直接调用原生原子 `a.fetch_max(val, Ordering::AcqRel)`。
- `#[cfg(not($target_has_atomic))]` 为真：调用 `self.fetch_update(|old| Some(cmp::max(old, val))).unwrap()`。

> ⚠️ **诚实补充（专家级细节）**：对于 `impl_arithmetic!` 覆盖的整数类型，第二道关里的 `fetch_update` 分支在当前实现下**实际是不可达的**。原因是：`$target_has_atomic`（如 `target_has_atomic = "8"`）和 `atomic!` 内部用来裁候选的 `cfg_has_atomic_N!` / `cfg_has_atomic_cas!` **来自同一份目标能力描述**——当 `$target_has_atomic` 为假时，对应的原子候选也被裁掉，于是 `atomic!` 在第一道关就已落到全局锁，根本进不到内层。这个 `fetch_update` 分支是一层**防御性声明（defensive tier）**：它表达了「若某目标拥有原子 CAS 但缺少该具体宽度的 RMW 内建函数」时的正确回退，并保证「即便两个 cfg 检测将来出现分歧」也不会编译出错误代码。把它理解成「设计意图上的第二层、当前对整数类型被第三层抢先」即可。对**用户直接调用** `fetch_update`（非宏生成）而言，它仍是一条真实且常用的 CAS 回退路径。

两层关口的伪代码：

```
fn fetch_max(val) {
    atomic! {  // 第一道关
        $t, _a,
        {  // 无锁分支（仅当存在匹配的原子候选）
            if cfg($target_has_atomic) {            // 第二道关
                a.fetch_max(val, AcqRel)             // ← 第一层
            } else {  // not cfg($target_has_atomic)
                fetch_update(|old| Some(max(old,val))).unwrap()  // ← 第二层（防御性）
            }
        },
        {  // 全局锁分支
            let _guard = lock(addr).write();        // ← 第三层
            let old = *value;
            *value = max(old, val);
            old
        }
    }
}
```

#### 4.2.3 源码精读

以 `fetch_max` 模板为例（[src/atomic/atomic_cell.rs:619-642](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L619-L642)）：

```rust
#[inline]
pub fn fetch_max(&self, val: $t) -> $t {
    atomic! {
        $t, _a,
        {
            #[cfg($target_has_atomic)]
            {
                let a = unsafe { &*(self.as_ptr() as *const atomic::$atomic) };
                a.fetch_max(val, Ordering::AcqRel)
            }
            #[cfg(not($target_has_atomic))]
            {
                self.fetch_update(|old| Some(cmp::max(old, val))).unwrap()
            }
        },
        {
            let _guard = lock(self.as_ptr() as usize).write();
            let value = unsafe { &mut *(self.as_ptr()) };
            let old = *value;
            *value = cmp::max(old, val);
            old
        }
    }
}
```

逐层拆解：

- **第一层（原生原子）**：`let a = unsafe { &*(self.as_ptr() as *const atomic::$atomic) };`——把 `AtomicCell<$t>` 的裸指针**重解释**为原生原子类型 `$atomic`（如 `AtomicUsize`）的引用。这次 `transmute` 的安全性来自 `repr(transparent)` 布局保证（u2-l1/u5-l1）。随后 `a.fetch_max(val, Ordering::AcqRel)` 直接调用原生原子的 RMW，编译成单条指令（如 x86 的 `lock cmpxchg` 配合循环，或直接 `cmpxchg8b`/`lr/sc` 序列）。内存序为 `AcqRel`（读 acquire + 写 release），与 `AtomicCell` 对外承诺的排序一致（u2-l1）。
- **第二层（`fetch_update`）**：`self.fetch_update(|old| Some(cmp::max(old, val))).unwrap()`——`fetch_update` 是 `impl<T: Copy + Eq>` 上的 CAS 循环（[src/atomic/atomic_cell.rs:299-312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L299-L312)）。闭包恒返回 `Some`，故循环必以 `Ok` 结束，`.unwrap()` 不会 panic。它内部反复 `compare_exchange`，每次失败都用返回的最新值重试。
- **第三层（全局锁）**：`lock(self.as_ptr() as usize).write()` 从 67 把锁的静态池里按地址取模选一把（[src/atomic/atomic_cell.rs:963-995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L963-L995)，详见 u2-l3），拿到写 guard 后做普通的 `*value = cmp::max(old, val)`，guard 离开作用域时 `Drop` 释放锁并推进印戳（[src/atomic/seq_lock.rs:85-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L85-L93)）。

注意外层 `atomic!` 宏本体（[src/atomic/atomic_cell.rs:331-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L331-L375)）里有一段决定「候选是否存在」的 cfg：

```rust
#[cfg(not(any(
    miri,
    crossbeam_loom,
    crossbeam_atomic_cell_force_fallback,
)))]
atomic_maybe_uninit::cfg_has_atomic_cas! {
    atomic_maybe_uninit::cfg_has_atomic_8! { atomic!(@check, $t, ...u8..., $a, $atomic_op); }
    // ... 16 / 32 / 64 / 128 ...
}
break $fallback_op;
```

这段是「第一道关」的实质：只要处于 `miri`/`loom`/`force_fallback`，或目标不支持原子 CAS，整个候选清单被裁掉，`atomic!` 直接 `break $fallback_op`——**这时连内层的两个分支都不会被编译**，全局锁是唯一存活路径。

对照看 `fetch_add`（[src/atomic/atomic_cell.rs:394-417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L394-L417)），骨架与 `fetch_max` 完全一致，只是把 `cmp::max` 换成 `wrapping_add`、原生调用换成 `a.fetch_add`：

```rust
#[cfg($target_has_atomic)]
{ /* a.fetch_add(val, Ordering::AcqRel) */ }
#[cfg(not($target_has_atomic))]
{ /* self.fetch_update(|old| Some(old.wrapping_add(val))).unwrap() */ }
// fallback:
// *value = value.wrapping_add(val);
```

其余 `fetch_sub`/`fetch_and`/`fetch_nand`/`fetch_or`/`fetch_xor`/`fetch_min` 全部同构（[src/atomic/atomic_cell.rs:433-680](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L433-L680)），只是中间运算不同。

#### 4.2.4 代码实践

**实践目标**：选取 `fetch_max`，分别说明在三种目标配置下它**实际执行了哪段代码**。

**操作步骤**：

1. **场景一：常规目标（如 x86-64），`target_has_atomic = "ptr"` 成立、支持原子 CAS、未开 sanitizer**。
   - 以 `AtomicCell::<usize>::fetch_max` 为例。
   - 阅读 [src/atomic/atomic_cell.rs:750-755](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L750-L755)，调用点 cfg 是 `target_has_atomic = "ptr"`，在本场景为真。
   - 阅读 [src/atomic/atomic_cell.rs:619-642](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L619-L642)。
   - 写一个最小程序验证：
     ```rust
     // 依赖：crossbeam-utils = { version = "0.8", features = ["atomic"] }
     use crossbeam_utils::atomic::AtomicCell;
     fn main() {
         let a = AtomicCell::new(7usize);
         let old = a.fetch_max(9);
         assert_eq!(old, 7);
         assert_eq!(a.load(), 9);
     }
     ```
2. **场景二：`crossbeam_atomic_cell_force_fallback`（强制全局锁）**。
   - 用 `RUSTFLAGS="--cfg crossbeam_atomic_cell_force_fallback"` 编译并运行上面的程序。
   - 阅读外层 `atomic!` 的 cfg（[src/atomic/atomic_cell.rs:348-353](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L348-L353)），确认此时候选清单被整体裁掉。
3. **场景三：`AtomicCell::<u128>::fetch_max`（原生层恒关）**。
   - 把上面程序的类型改成 `u128`。
   - 阅读 [src/atomic/atomic_cell.rs:736-742](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L736-L742)，确认调用点 cfg 是恒假。

**需要观察的现象 / 预期结果**：

| 场景 | 第一道关（`atomic!`）选谁 | 内层 cfg 存活情况 | 实际执行 |
| --- | --- | --- | --- |
| 场景一（x86-64，usize） | 无锁分支（usize 候选命中） | `target_has_atomic="ptr"` 为真 | **第一层**：`AtomicUsize::fetch_max(val, AcqRel)`，单条 RMW |
| 场景二（force_fallback） | 全局锁分支（候选全裁） | 内层两分支都不编译 | **第三层**：`lock(addr).write()` 取锁 → `*value = max(...)` → 释放 |
| 场景三（u128，任意常规目标） | 无锁分支（u128 候选命中，因 64 位目标上有 `cfg_has_atomic_128` 或更宽……**见下方说明**） | `target_has_atomic="128"` 写成恒假 → 只有 `not(...)` 存活 | **第二层**：`fetch_update(\|old\| Some(max(old,val)))` CAS 循环 |

> 场景三的精确说明：`u128` 在 64 位目标上，`atomic!` 宏确实可能命中 `AtomicMaybeUninit<u128>` 候选（若目标有 128 位原子，如 x86-64 的 `cmpxchg16b`），从而选中无锁分支；但调用点把 `$target_has_atomic` 设为恒假，于是无锁分支里**只有 `not(...)` 即 `fetch_update` 那一支被编译**——这就是「有原子 CAS、但禁用了原生 RMW 内建」时 `fetch_update` 分支**真正可达**的实例。这恰好印证了 4.2.2 里关于「第二层是设计上的 CAS 回退」的说法。若目标连 128 位候选都没有，则 `atomic!` 第一道关就落到全局锁（第三层）。

**如果无法确定运行结果**：场景二的 `force_fallback` 与场景三的 `u128` 行为标注为「待本地验证」——可在本地分别用对应 `RUSTFLAGS` / 类型编译运行上述最小程序，断言结果不变（语义与场景一一致，只是路径不同）。

#### 4.2.5 小练习与答案

**练习 1**：`fetch_update` 闭包恒返回 `Some`，却还跟着 `.unwrap()`。这个 `.unwrap()` 会不会 panic？

> **参考答案**：不会。`fetch_update` 的返回类型是 `Result<T, T>`（[src/atomic/atomic_cell.rs:300-312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L300-L312)）：闭包一旦返回 `Some(next)`，循环就持续尝试 `compare_exchange` 直到成功并以 `Ok(prev)` 返回；只有闭包返回 `None` 才会以 `Err(prev)` 返回。这里闭包**恒返回 `Some`**，故循环必以 `Ok` 结束，`.unwrap()` 永不 panic。它存在的意义只是把 `Result<T,T>` 拆回 `T` 以匹配 `fetch_*` 的签名 `-> $t`。

**练习 2**：为什么全局锁分支里用的是 `lock(self.as_ptr() as usize)`，而不是「一把全局大锁」？

> **参考答案**：`lock()` 返回的是 67 把 `CachePadded<SeqLock>` 组成的静态锁池里**按地址取模**选出的某一把（[src/atomic/atomic_cell.rs:963-995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L963-L995)）。用多把锁而非单锁，是为了**降低争用**：不同地址的 `AtomicCell` 会落到不同锁上，从而可以真正并行。`LEN=67` 取素数是为了避免「地址按 2 的幂对齐 → 取模后只用到一半锁」的退化（u2-l3）。

**练习 3**：把 `fetch_max` 的内存序从 `AcqRel` 改成 `Relaxed`，会破坏 `AtomicCell` 的对外契约吗？

> **参考答案**：会。`AtomicCell` 的对外承诺是 *“loads use Acquire, stores use Release”*（u2-l1）。`fetch_max` 是读改写，既要读（需 acquire 语义）又要写（需 release 语义），故必须 `AcqRel`。改成 `Relaxed` 会丢失 happens-before 边，破坏「写者 release → 读者 acquire」的可见性保证，属于 unsound 的改动——这也是为什么宏模板里把 `Ordering::AcqRel` 写死、不暴露给用户。

### 4.3 `AtomicCell<bool>` 的逻辑运算特例

#### 4.3.1 概念说明

打开 [src/atomic/atomic_cell.rs:763-926](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L763-L926)，你会发现 `AtomicCell<bool>` 拥有一个**单独手写的 `impl` 块**，提供 `fetch_and`/`fetch_nand`/`fetch_or`/`fetch_xor` 四个逻辑方法。它**没有**走 `impl_arithmetic!` 宏。原因有三：

1. **bool 不是整数类型**：`impl_arithmetic!` 的模板里大量出现 `wrapping_add`、`cmp::max` 这类**整数独有**的运算。bool 没有这些运算——对 bool 谈「加法」「取最大」没有意义。bool 只需要 4 个**逻辑**运算（与/或/异或/与非），方法集合与整数不同。
2. **原生原子类型不同**：整数对应 `AtomicU8`/`AtomicIsize` 等，bool 对应 `AtomicBool`。`AtomicBool` 是一个独立类型，不能塞进 `impl_arithmetic!` 那个「整数原子类型」的参数槽里而不破坏模板里其它整数方法的假设。
3. **运算符与字面量不同**：整数 `fetch_and` 用 `old & val`（位与），bool 也用 `old & val`（逻辑与，因 bool 重载了 `&`），但 doc 示例、cfg 宽度（bool 是 1 字节，恒用 `target_has_atomic = "8"`）都和整数不同。单独写更清晰。

简言之：`bool` 与整数的「运算语义」「方法集合」「原生原子类型」三方面都不同，强行塞进 `impl_arithmetic!` 会让模板充满 `bool` 特判，反而比单独写一个 `impl` 块更复杂。

#### 4.3.2 核心流程

`AtomicCell<bool>` 的 `impl` 块结构与整数版**同构**，也是每个方法一个 `atomic!` 调用、内含三层：

1. **第一层（原生）**：`#[cfg(target_has_atomic = "8")]` 时，把指针重解释为 `AtomicBool`，调用 `a.fetch_and(val, Ordering::AcqRel)`。注意 cfg 是**直接写死** `target_has_atomic = "8"`，而不是从宏参数来——因为 `bool` 永远是 1 字节，对应 8 位原子。
2. **第二层（`fetch_update`）**：`#[cfg(not(target_has_atomic = "8"))]` 时，`self.fetch_update(|old| Some(old & val)).unwrap()`。
3. **第三层（全局锁）**：与整数版完全一致，`lock(addr).write()` 后做 `*value &= val`。

差别只在「运算符」与「原生类型」。

#### 4.3.3 源码精读

以 bool 版 `fetch_and` 为例（[src/atomic/atomic_cell.rs:779-802](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L779-L802)）：

```rust
#[inline]
pub fn fetch_and(&self, val: bool) -> bool {
    atomic! {
        bool, _a,
        {
            #[cfg(target_has_atomic = "8")]
            {
                let a = unsafe { &*(self.as_ptr() as *const atomic::AtomicBool) };
                a.fetch_and(val, Ordering::AcqRel)
            }
            #[cfg(not(target_has_atomic = "8"))]
            {
                self.fetch_update(|old| Some(old & val)).unwrap()
            }
        },
        {
            let _guard = lock(self.as_ptr() as usize).write();
            let value = unsafe { &mut *(self.as_ptr()) };
            let old = *value;
            *value &= val;
            old
        }
    }
}
```

与整数版 `fetch_and`（[src/atomic/atomic_cell.rs:470-493](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L470-L493)）逐点对比：

| 对比项 | 整数版 `AtomicCell<u8>::fetch_and` | bool 版 `AtomicCell<bool>::fetch_and` |
| --- | --- | --- |
| 来源 | `impl_arithmetic!` 宏生成 | 单独手写 `impl` 块 |
| 类型参数 `$t` | `u8` | `bool` |
| 原生原子类型 | `atomic::AtomicU8`（来自 `$atomic`） | `atomic::AtomicBool`（写死） |
| cfg 宽度 | `$target_has_atomic` = `target_has_atomic = "8"`（宏参数） | `target_has_atomic = "8"`（写死） |
| 运算 | `old & val`（位与） / `*value &= val` | `old & val`（逻辑与） / `*value &= val` |
| 方法集合 | 8 个（含 `fetch_add`/`fetch_sub`/`fetch_max`/`fetch_min`） | 仅 4 个（无算术、无 max/min） |

最后一行是本质差别：bool 版**没有** `fetch_add`/`fetch_sub`/`fetch_max`/`fetch_min`，只有 4 个逻辑运算——因为对 bool 做加法或取最大没有定义良好的语义。这也是它无法并入 `impl_arithmetic!`（那个宏为每个类型都生成全部 8 个方法）的根本原因。

另一点值得注意：bool 版的 `fetch_xor`（[src/atomic/atomic_cell.rs:902-925](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L902-L925)）运算 `old ^ val` 恰好就是「翻转特定位」的语义，这在实现「一次性开关」场景下很实用。

#### 4.3.4 代码实践

**实践目标**：对比 `AtomicCell<bool>` 与 `AtomicCell<u8>` 的方法集合，直观感受 bool 特例。

**操作步骤**：

1. 写一个最小程序，尝试对两种类型调用同名方法：
   ```rust
   use crossbeam_utils::atomic::AtomicCell;
   fn main() {
       let b = AtomicCell::new(true);
       b.fetch_and(false);      // OK：bool 有
       b.fetch_or(true);        // OK：bool 有

       let u = AtomicCell::new(7u8);
       u.fetch_and(3);          // OK：u8 有
       u.fetch_add(1);          // OK：u8 有

       // 取消下面两行注释，观察编译错误：
       // b.fetch_add(true);    // 期望：方法不存在
       // b.fetch_max(false);   // 期望：方法不存在
   }
   ```
2. 编译：`cargo build`。

**需要观察的现象**：

- `AtomicCell<bool>` 调用 `fetch_and`/`fetch_or` 编译通过。
- `AtomicCell<bool>` 调用 `fetch_add`/`fetch_max` 时，编译器报「no method named `fetch_add`/`fetch_max`」错误——因为 bool 的 `impl` 块里没有这两个方法，而 `impl_arithmetic!` 也没有为 bool 生成。
- `AtomicCell<u8>` 调用 `fetch_add` 编译通过——证实 u8 走的是宏生成的 8 方法集合。

**预期结果**：bool 类型只有 4 个逻辑运算方法，整数类型有 8 个算术/位/极值方法。这印证了「bool 必须单独 `impl`」。

#### 4.3.5 小练习与答案

**练习 1**：`bool` 也是 1 字节、对齐 1，理论上能 `can_transmute` 到 `AtomicMaybeUninit<u8>`。为什么 bool 版没有像整数那样「先试 `AtomicMaybeUninit<u8>` 候选」，而是直接用 `AtomicBool`？

> **参考答案**：bool 版**同样会经过 `atomic!` 宏的候选筛选**——`atomic!` 仍会试 `AtomicUnit` 与各 `AtomicMaybeUninit<uN>` 候选（`can_transmute::<bool, AtomicMaybeUninit<u8>>()` 在 8 位原子可用时成立）。命中后进入无锁分支，在那里才用 `as *const atomic::AtomicBool` 重解释。之所以用 `AtomicBool` 而非 `AtomicU8`，是因为 `AtomicBool` 语义上就是「布尔原子」，它的 `fetch_and`/`fetch_or` 接收 `bool` 参数、返回 `bool`，类型更精确；而 `AtomicU8::fetch_and` 接收 `u8`。两者底层是同一条 CPU 指令（bool 与 u8 布局相同），但 `AtomicBool` 让 `unsafe` 重解释的两端类型对齐为「bool ↔ bool」，更安全、更可读。

**练习 2**：假如想把 `AtomicCell<bool>` 也并入 `impl_arithmetic!`，至少要给宏增加哪些「特判」？

> **参考答案**：至少需要：(a) 让宏对 `bool` 跳过 `fetch_add`/`fetch_sub`/`fetch_max`/`fetch_min` 四个方法（bool 无这些运算）；(b) 把原生原子类型参数从「整数原子」泛化为「也可能是 `AtomicBool`」；(c) 把 doc 示例里的 `7u8` 之类换成 `true`/`false`；(d) `fetch_and` 等方法的中间运算从「整数位运算」泛化为「也能是 bool 逻辑运算」。这些特判会让原本「一份干净模板」变成「充满 if-bool 分支的模板」，可读性和可维护性反而下降——这正是作者选择单独 `impl` 的理由。

## 5. 综合实践

把本讲三块知识串起来，做一个**「宏展开 + 路径标注」**的小任务：

1. **选取类型**：任选 `AtomicCell<u32>`。
2. **展开宏（纸面）**：根据 `impl_arithmetic!` 模板（[src/atomic/atomic_cell.rs:377-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L377-L683)）与 u32 调用点（[src/atomic/atomic_cell.rs:710-715](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L710-L715)），在纸上写出 `AtomicCell<u32>::fetch_max` 展开后的完整方法体（把 `$t`→`u32`、`$atomic`→`AtomicU32`、`$target_has_atomic`→`target_has_atomic = "32"`、`cmp::max` 不变）。
3. **标注三层路径**：在你写出的方法体上，用三种颜色（或注释）标出：
   - 第一层（原生）：`AtomicU32::fetch_max(val, AcqRel)`，由 `#[cfg(target_has_atomic = "32")]` 控制。
   - 第二层（`fetch_update`）：`self.fetch_update(|old| Some(cmp::max(old, val))).unwrap()`，由 `#[cfg(not(target_has_atomic = "32"))]` 控制。
   - 第三层（全局锁）：`lock(addr).write()` 后 `*value = cmp::max(old, val)`，由外层 `atomic!` 在无候选时选中。
4. **判定可达性**：在你常用的目标（如 x86-64）上，`target_has_atomic = "32"` 为真 → 实际走第一层。再回答：若改用 `RUSTFLAGS="--cfg crossbeam_atomic_cell_force_fallback"`，会走哪一层？（答：第三层，因为 `atomic!` 在该 cfg 下裁掉所有候选，连内层都不编译。）
5. **（可选）运行验证**：写一个多线程程序，若干线程并发对同一个 `AtomicCell<u32>` 调用 `fetch_max`，最后 `load()` 应等于所有线程传入值的最大值——验证无论走哪层，语义一致。此项在 `force_fallback` 下的表现标注为「待本地验证」。

完成本任务后，你应当能对 `impl_arithmetic!` 覆盖的**任意**类型、任意 `fetch_*` 方法，迅速说出它的三层路径分别由哪条 cfg 控制、在你当前目标上实际执行哪一段代码。

## 6. 本讲小结

- `impl_arithmetic!` 是一个声明宏，用**一份模板**为 12 种整数类型（`u8`..`isize`）批量生成各 8 个 RMW 方法（`fetch_add`/`fetch_sub`/`fetch_and`/`fetch_nand`/`fetch_or`/`fetch_xor`/`fetch_max`/`fetch_min`），共消除约 96 份重复代码（[src/atomic/atomic_cell.rs:377-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L377-L683)）。
- 宏签名 `($t:ty, #[cfg($target_has_atomic:meta)] $atomic:ident, $example:tt)` 用 `:meta` 片段捕获整段 `#[cfg(...)]` 属性，用 `#[doc = $example]` 把类型化的 doc 示例注入文档（[src/atomic/atomic_cell.rs:377-379](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L377-L379)）。
- 每个生成的 `fetch_*` 方法体是一次 `atomic!` 调用，内含**三层回退阶梯**：原生原子 RMW（`#[cfg($target_has_atomic)]`）、`fetch_update` CAS 循环（`#[cfg(not($target_has_atomic))]`）、全局 SeqLock（外层 `atomic!` 在无候选时选中）。
- 三层由**编译期 cfg 裁剪**而非运行时分支决定；外层 `atomic!` 在 `miri`/`loom`/`force_fallback` 或无原子 CAS 时裁掉全部候选，使全局锁成为唯一存活路径（[src/atomic/atomic_cell.rs:348-374](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L348-L374)）。
- 对整数类型而言，第二层 `fetch_update` 在当前实现下通常被第三层抢先（同一份目标能力描述同时门控候选与内层 cfg），属**防御性声明**；但它对「禁用原生 RMW」的场景（如 `u128` 调用点的恒假 cfg）确实可达（[src/atomic/atomic_cell.rs:736-748](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L736-L748)）。
- `AtomicCell<bool>` **不走** `impl_arithmetic!`，因为它的方法集合（仅 4 个逻辑运算，无算术/极值）、原生原子类型（`AtomicBool`）、运算语义都与整数不同，单独手写 `impl` 块比往宏里塞特判更清晰（[src/atomic/atomic_cell.rs:763-926](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L763-L926)）。

## 7. 下一步学习建议

- **u5-l3 跨平台 cfg、loom 抽象与宽 SeqLock**：本讲反复提到的 `cfg_has_atomic_cas!` / `cfg_has_atomic_N!` / `crossbeam_atomic_cell_force_fallback` 等 cfg 从何而来、`build.rs` 如何发射它们、loom 如何替换 `primitive` 抽象层做交错测试——这些都在 u5-l3 系统讲解。读完它，你会彻底理解本讲「三层路径的 cfg 编排」背后的构建期机制。
- **u5-l4 并发测试策略与基准**：本讲的多个实践都涉及「验证语义一致」与「标注待本地验证」。u5-l4 讲解如何用 `tests/`、loom、Miri、TSan、`benches/` 为这些原子方法设计压力测试与基准，是把本讲知识落到工程验证的下一步。
- **回看 u2-l2**：若你对 `atomic!` 宏与 `can_transmute` 的判定仍有疑问，建议重读 u2-l2 的「`atomic!` 宏分发」一节——本讲的「第一道关」就是它。
- **延伸阅读**：标准库 [`core::sync::atomic`](https://doc.rust-lang.org/core/sync/atomic/) 文档，对照 `AtomicUsize::fetch_max` 与本讲第一层路径的调用，理解「`AtomicCell` 是对原生原子的透明包装 + 全局锁兜底」这一总体设计。
