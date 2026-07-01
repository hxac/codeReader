# AtomicCell 任意类型原子单元

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `AtomicCell<T>` 为什么对**任意**类型 `T` 都能提供 `load / store / swap / compare_exchange`，而不像 `std::sync::atomic::AtomicUsize` 那样只能锁死在固定几种位宽上。
- 区分 `AtomicCell` 的**两条实现路径**：能「位转换」成原生原子类型时走真正的原子指令（lock-free），否则退化为**全局序列锁数组**兜底。
- 读懂序列锁（seqlock）的「乐观读 / 写校验」协议，并解释 stamp 为什么每次 `+2`。
- 理解 `UnsafeCell` + `MaybeUninit` 这个布局组合解决了什么安全隐患，以及它给 `Drop` 带来的额外责任。

本讲是 u2-l1（Backoff）与 u2-l2（CachePadded）的综合应用：序列锁在争用时用 `Backoff::snooze()` 退避，而那 67 把全局锁每一把都被 `CachePadded` 包裹以消除伪共享。

## 2. 前置知识

在进入源码前，先用通俗语言对齐三个概念。

### 2.1 `Cell<T>` 与内部可变性

标准库的 `Cell<T>` 允许你通过一个**共享引用** `&Cell<T>` 改里面的值。Rust 默认「共享即不可变」，`Cell` 打破这条规则靠的是 `UnsafeCell<T>`——它是 Rust 中**唯一**被编译器认可的「内部可变性」原语。只要类型里有 `UnsafeCell`，编译器就知道这块内存可能被 `&self` 改写，从而抑制那些会破坏并发安全的优化。`AtomicCell` 同样以 `UnsafeCell` 为地基。

### 2.2 原子指令的局限

`std::sync::atomic` 只提供了 `AtomicU8/16/32/64`（以及平台相关的 128 位）、`AtomicBool`、`AtomicPtr` 等固定形状的类型。如果你想对一个 `(u64, u64)`、一个 `[u8; 1000]`、或一个自定义结构体做「整块原子读写」，标准库无能为力。`AtomicCell<T>` 的目标就是填补这个空白：对**任意** `T` 都给出统一的 `load/store/swap/CAS` 接口。

### 2.3 序列锁（seqlock）直觉

序列锁是一种经典的「写者优先、读者乐观」的锁，核心是给数据配一个单调递增的**邮戳（stamp）**：

- **读者**：先记下当前 stamp → 读数据 → 再看 stamp 变没变。没变说明读的期间没有写者闯入，数据有效；变了就重读。全程不阻塞写者。
- **写者**：进入临界区前先把 stamp「弄脏」（标记正在写），写完再把 stamp 推进一步，让同时正在乐观读的读者随后校验失败。

这是一种「赌大多数时候没有写者」的策略，读路径极快。后面你会看到，`AtomicCell` 的非原子分支正是用它实现的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crossbeam-utils/src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) | `AtomicCell<T>` 的全部定义：公共 API、`atomic!` 宏分发、四个底层原子函数、`lock()` 选锁逻辑。 |
| [crossbeam-utils/src/atomic/seq_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs) | 序列锁本体：`SeqLock`、`optimistic_read / validate_read / write`、`SeqLockWriteGuard`。 |
| [crossbeam-utils/src/atomic/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs) | 模块门面：根据指针宽度选择普通 `seq_lock` 还是 `seq_lock_wide`，并导出 `AtomicCell`。 |
| [crossbeam-utils/tests/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs) | 契约测试：`is_lock_free`、Drop 计数、`issue_833`（驱动 `MaybeUninit` 引入的原因）等。 |

> 说明：当指针宽度 ≤ 32（见 `target_pointer_width = "16" / "32"`）时，`mod.rs` 会改用 `seq_lock_wide.rs`，把 stamp 拆成高位/低位两个 `AtomicUsize` 以防回绕。本讲以常见的 64 位平台（即 `seq_lock.rs`）为主线，wide 版本属于 u2-l4 的延伸。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **内存布局**：`repr(transparent)` + `UnsafeCell` + `MaybeUninit`；
2. **路径分发**：`can_transmute` 判定 + `atomic!` 宏；
3. **序列锁兜底**：67 把 `CachePadded<SeqLock>` + 乐观读校验。

### 4.1 AtomicCell 的内存布局：repr(transparent) + UnsafeCell + MaybeUninit

#### 4.1.1 概念说明

`AtomicCell<T>` 对外表现得像一个「线程安全的 `Cell<T>`」：你拿到的是 `&AtomicCell<T>`，却能改写其中的值。要做到这点，它在内部放了 `UnsafeCell`（拿到内部可变性）；又为了让 `&AtomicCell<T>` 与 `&T` 的指针可以安全互转，用了 `#[repr(transparent)]`（保证 `AtomicCell<T>` 与它唯一的字段、进而与 `T` 布局完全一致）。

最微妙的是它**不**直接放 `UnsafeCell<T>`，而是放 `UnsafeCell<MaybeUninit<T>>`。原因不是「值可能未初始化」——`AtomicCell` 的 API 保证存进去的都是合法 `T`——而是要阻止编译器基于「这块内存一定是合法 `T`」做优化。在并发读时，我们可能读到正在被另一线程覆写的「半成品」字节，如果编译器假设它是合法 `T`（例如假设 `NonZeroU128` 永不为 0），就会触发未定义行为（UB）。用 `MaybeUninit` 显式声明「这些字节当前不一定是合法 `T`」，把「是否合法」的决定权交还给程序员，正是 issue #833 的修复手段。

#### 4.1.2 核心流程

布局带来的连锁后果可以画成这样一张依赖图：

```
AtomicCell<T>
  └─ repr(transparent) ──► 与 T 布局相同 ──► 可把 *mut T 当作 *const AtomicUsize 等
  └─ UnsafeCell<MaybeUninit<T>>
        ├─ UnsafeCell  ──► 内部可变性（&self 可写）+ 抑制危险优化
        └─ MaybeUninit ──► 编译器不假定字节是合法 T
              ├─ 读端：read_volatile → 校验通过才 assume_init
              └─ 写端：值由 API 保证合法，但 MaybeUninit 会屏蔽自动 Drop
                    └─ 因此 AtomicCell 必须 hand-write Drop（needs_drop 时 drop_in_place）
```

#### 4.1.3 源码精读

结构体与字段定义，[atomic_cell.rs:32-48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L32-L48) —— 注意字段注释里直接点明了两条路径（transmute 成原子类型 / 否则用全局锁），以及 `MaybeUninit` 防止观察到「部分初始化状态」的用意：

```rust
#[repr(transparent)]
pub struct AtomicCell<T> {
    value: UnsafeCell<MaybeUninit<T>>,
}
```

`Send`/`Sync` 的实现要求 `T: Send`（注意是 `Send` 而非 `Clone`），[atomic_cell.rs:50-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L50-L51)。原因：`AtomicCell` 从不交出 `&T`，只通过值把 `T` 移入移出，因此只要 `T` 能跨线程转移所有权（`Send`）即可安全共享。

`MaybeUninit` 会屏蔽 `T` 的自动析构，所以 `AtomicCell` 必须亲自负责 drop，[atomic_cell.rs:317-329](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L317-L329)：

```rust
impl<T> Drop for AtomicCell<T> {
    fn drop(&mut self) {
        if mem::needs_drop::<T>() {
            unsafe { self.as_ptr().drop_in_place(); }
        }
    }
}
```

同理，`store` 在覆盖旧值时若 `T` 需要析构，就走 `swap`（换出旧值并丢弃它）而非直接覆写，[atomic_cell.rs:177-185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L177-L185)：

```rust
pub fn store(&self, val: T) {
    if mem::needs_drop::<T>() {
        drop(self.swap(val));   // 旧值被换出并在此 drop
    } else {
        unsafe { atomic_store(self.as_ptr(), val); }
    }
}
```

> 这就是为什么 `Drop` 计数测试里，反复 `store`/`swap` 一个带 `Drop` 的类型，全局对象计数始终稳定在 1：旧值每次都被正确回收，而 `MaybeUninit` 又保证不会双重释放。

#### 4.1.4 代码实践

**目标**：用一个 `Drop` 计数器验证 `AtomicCell` 对非 `Copy` 类型也能正确管理生命周期，亲眼看一遍 `MaybeUninit` + 手写 `Drop` 的协作。

**操作步骤**（这是一个可独立运行的 `#[test]`，照搬自 [tests/atomic_cell.rs:76-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L76-L116) 的思路）：

```rust
// 示例代码：放在你自己的 crate 里运行
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
use crossbeam_utils::atomic::AtomicCell;

static CNT: AtomicUsize = AtomicUsize::new(0);

struct Foo(usize);
impl Foo {
    fn new(v: usize) -> Self { CNT.fetch_add(1, SeqCst); Self(v) }
}
impl Drop for Foo {
    fn drop(&mut self) { CNT.fetch_sub(1, SeqCst); }
}

#[test]
fn atomic_cell_drops_correctly() {
    let a = AtomicCell::new(Foo::new(5));
    assert_eq!(CNT.load(SeqCst), 1);      // 存了 1 个
    a.store(Foo::new(6));                  // 旧值(5)被 drop，新值(6)进来
    assert_eq!(CNT.load(SeqCst), 1);      // 仍然只有 1 个，没有泄漏、没有双释放
    drop(a);                               // AtomicCell 自身的 Drop 回收内部值
    assert_eq!(CNT.load(SeqCst), 0);
}
```

**需要观察的现象**：每次 `store` 后 `CNT` 恒为 1，`drop(a)` 后归 0。

**预期结果**：测试通过。它反向印证了 `MaybeUninit` 没有把析构责任丢掉——`AtomicCell::drop` 与 `store` 里的 `swap` 路径共同接管了 `T` 的生命周期。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `AtomicCell<T>` 的字段改成裸的 `UnsafeCell<T>`（不要 `MaybeUninit`），在单线程下功能是否还正常？为什么 crossbeam 仍坚持用 `MaybeUninit`？

> **答案**：单线程功能正常。坚持用 `MaybeUninit` 是为了并发读时读到「半成品字节」时不触发 UB——编译器不再能假设这块内存一定是合法 `T`。这是 issue #833 的核心修复，详见 `issue_833` 测试 [tests/atomic_cell.rs:360-404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L360-L404)。

**练习 2**：`AtomicCell` 的 `unsafe impl Sync` 只要求 `T: Send`，而不要求 `T: Sync`。请用「`AtomicCell` 如何交出 `T`」解释这一点。

> **答案**：`Sync` 是「`&T` 能跨线程共享」。`AtomicCell` 从不交出 `&T`，只通过 `load/swap` 等把 `T` **按值**搬出，跨线程传递的是所有权而非引用，因此只需 `T: Send`。要求 `T: Sync` 会过度收紧可用范围（例如 `Cell<u32>` 不是 `Sync` 但 `u32` 是 `Send`）。

### 4.2 两条实现路径：can_transmute 判定与 atomic! 宏分发

#### 4.2.1 概念说明

`AtomicCell<T>` 的每一个操作（`load/store/swap/CAS`）都要在编译期决定走哪条路：

- **快路径（lock-free）**：如果 `T` 的尺寸与对齐恰好能「位转换」（bit-transmute）成某种原生原子类型（如 `AtomicU64`），就把 `*mut T` 重解释成 `*const AtomicU64`，直接用 CPU 原子指令。
- **慢路径（全局锁）**：否则，调用 `lock(addr)` 取一把全局 `SeqLock`，在临界区内做普通读写。

判定的关键是 `can_transmute<A, B>`：`A` 能否安全地按位重解释成 `B`。而 `atomic!` 宏则把「依次尝试各档原子类型，命中则跳出，全部落空则走兜底」这套流程封装成一段在每次操作里复用的样板。

#### 4.2.2 核心流程

`atomic!` 宏展开后的运行时控制流（它被包在一个 `loop {}` 里，靠 `break` 退出）：

```
进入 atomic! { T, a, <原子分支>, <兜底分支> }
  │
  ├─ 试 AtomicUnit          （专门给 () 的空操作）
  │     can_transmute::<T, AtomicUnit>()?  命中 → break 原子分支
  │
  ├─ （仅当平台有 CAS 原子时）依次试
  │     AtomicMaybeUninit<u8>   /  <u16>  /  <u32>  /  <u64>  /  <u128>
  │     每个都用 can_transmute 判定；命中 → break 原子分支
  │
  └─ 全部不命中 → break 兜底分支（走全局 SeqLock）
```

> 注意：宏里的「尝试」是运行时的 `if can_transmute::<...>()`，但 `can_transmute` 是 `const fn`，编译器能在单态化后把绝大多数分支常量折叠掉，最终基本没有运行时开销。

#### 4.2.3 源码精读

`can_transmute` 的判定只有一行，[atomic_cell.rs:949-953](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L949-L953)：

```rust
const fn can_transmute<A, B>() -> bool {
    // 尺寸必须相等，且 A 的对齐要 >= B 的对齐
    (mem::size_of::<A>() == mem::size_of::<B>())
        & (mem::align_of::<A>() >= mem::align_of::<B>())
}
```

这里 `A` 是 `T`，`B` 是原子类型底层值（如 `u64`）。对齐条件解释了一个反直觉现象：在 32 位 x86（i686）上，`u64` 的对齐是 4，而 `AtomicU64` 的对齐是 8，于是 `AtomicCell::<u64>::is_lock_free()` 在那里返回 `false`——见契约测试 [tests/atomic_cell.rs:51-57](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L51-L57)（链接同目录的测试文件）。同一个测试还表明：用 `#[repr(align(8))]` 把 `u64` 包一层，对齐补到 8 后就重新变成 lock-free。

`atomic!` 宏主体，[atomic_cell.rs:331-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L331-L375)。其 `@check` 内部规则负责「判定 + 声明变量 + 跳出」：

```rust
(@check, $t:ty, $atomic:ty, $a:ident, $atomic_op:expr) => {
    if can_transmute::<$t, $atomic>() {
        let $a: &$atomic;
        break $atomic_op;          // 命中，跳出 loop，执行原子分支
    }
};
```

主规则把 `AtomicUnit` 和 5 档 `AtomicMaybeUninit` 依次喂给 `@check`，最后兜底，[atomic_cell.rs:344-374](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L344-L374)。其中 `cfg(not(any(miri, crossbeam_loom, crossbeam_atomic_cell_force_fallback)))` 这一长串很重要：在 miri / loom / 或显式开启 `crossbeam_atomic_cell_force_fallback` 时，跳过所有原子分支、**强制走全局锁**——这是测试序列锁路径的开关。

一个最直观的「双分支」例子是 `fetch_add`，[atomic_cell.rs:395-417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L395-L417)：

```rust
atomic! {
    $t, _a,
    { /* 原子分支：a.fetch_add(val, AcqRel) */ },
    {
        let _guard = lock(self.as_ptr() as usize).write();  // 慢路径：取全局锁
        let value = unsafe { &mut *(self.as_ptr()) };
        let old = *value;
        *value = value.wrapping_add(val);
        old
    }
}
```

`AtomicUnit` 是给 `()` 准备的「占位原子」，所有操作都是 no-op，[atomic_cell.rs:1000-1022](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1000-L1022)。它的存在让 `AtomicCell::<()>` 不必特判就能走宏的统一流程。

最后，`is_lock_free` 就是「能否命中任何一条原子分支」的常量查询，[atomic_cell.rs:1025-1027](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1025-L1027)：`atomic! { T, _a, true, false }`——命中原子分支返回 `true`，落到兜底返回 `false`。

#### 4.2.4 代码实践

**目标**：用 `is_lock_free()` 在真实平台上观察路径分发，并验证你对 `can_transmute` 的理解。

**操作步骤**（示例代码，独立运行）：

```rust
use crossbeam_utils::atomic::AtomicCell;

fn main() {
    struct UsizeWrap(usize);                 // 尺寸=指针宽，对齐=指针宽
    #[repr(align(8))]
    struct U64Align8(u64);                   // 对齐被抬到 8
    struct Tuple(u64, u64);                  // 尺寸 16，对齐 8

    println!("usize   : {}", AtomicCell::<usize>::is_lock_free());     // 多数平台 true
    println!("bool    : {}", AtomicCell::<bool>::is_lock_free());      // true
    println!("()      : {}", AtomicCell::<()>::is_lock_free());        // true（AtomicUnit）
    println!("UsizeWrap: {}", AtomicCell::<UsizeWrap>::is_lock_free());// true
    println!("U64Align8:{}", AtomicCell::<U64Align8>::is_lock_free()); // 有 atomic=64 时 true
    println!("(u64,u64): {}", AtomicCell::<Tuple>::is_lock_free());    // 关键：多数平台 false
    println!("[u8;1000]: {}", AtomicCell::<[u8; 1000]>::is_lock_free());// false
}
```

**需要观察的现象**：`AtomicCell<(u64, u64)>` 与 `[u8; 1000]` 都打印 `false`。

**预期结果**：`Tuple(u64, u64)` 尺寸为 16，唯一能匹配的原子是 128 位（`AtomicMaybeUninit<u128>`，底层 `u128` 对齐 16），但 `Tuple` 对齐只有 8 < 16，`can_transmute` 失败 → 退回全局锁。这正是下一节序列锁要兜的对象。**待本地验证**：在带 128 位原子且 `u128` 对齐为 16 的平台上，结论是否改变（可对照 [tests/atomic_cell.rs:65-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/atomic_cell.rs#L65-L73) 的 `u128` 断言）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `atomic!` 宏在 miri 下要刻意跳过原子分支、强制走兜底？

> **答案**：miri 不支持任意尺寸的内联汇编原子；更关键的是，序列锁路径依赖 `read_volatile` + stamp 校验，是真正需要被 miri 检查 UB 的部分。强制走兜底让这部分代码在 miri 下也能被覆盖到。开关见 [atomic_cell.rs:349-353](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L349-L353)。

**练习 2**：`can_transmute` 要求「`A` 的对齐 ≥ `B` 的对齐」。若改成 `==`，对 `AtomicCell<u64>`（i686 上对齐为 4）会带来什么后果？

> **答案**：若用 `==`，i686 上 `align_of::<u64>()==4` 而 `align_of::<AtomicU64>()==8` 不等，仍会判定不可转——结论不变；但更重要的是 `>=` 允许「`T` 对齐比原子更高」的情形（如 `#[repr(align(16))]` 的 8 字节结构体重解释为 `AtomicU64`）合法通过，更宽松且安全。改成 `==` 会错误拒绝这些本可 lock-free 的类型。

### 4.3 全局序列锁兜底：lock() 与乐观读校验

#### 4.3.1 概念说明

当 `T` 无法走原子路径时，`AtomicCell` 用一组**全局**序列锁来串行化所有写操作、并让读操作乐观并发。设计上有两个亮点：

1. **不是一把锁，而是 67 把**，按数据地址取模选一把。这样不同地址的 `AtomicCell` 落到不同锁，写彼此不干扰，争用被摊薄。
2. **每把锁都被 `CachePadded` 包裹**（u2-l2），避免多个锁变量挤在同一缓存行上引发伪共享。

读操作优先尝试**乐观读**：不拿锁，读出数据后再校验 stamp；只有乐观读失败（确有写者闯入）才退化为拿写锁读。这是一种「读多写少」场景下的加速。

#### 4.3.2 核心流程

**选锁**：`lock(addr) = LOCKS[addr % 67]`，其中 67 是质数。地址总是按某种 2 的幂对齐；若 `LEN` 取偶数，`addr % LEN` 也会偏到偶数下标，只用上一半锁；甚至 `#[repr(C)]` 结构体数组里的字段可能按 3 的倍数排布，若 `LEN` 能被 3 整除又会塌缩。选一个大质数能把这些「地址聚集」打散到全部 67 把锁上。

**SeqLock 状态机**（`state` 是一个 `AtomicUsize`）：

| state 值 | 含义 |
| --- | --- |
| 偶数（0, 2, 4, …） | 未锁定，stamp 就是这个偶数本身（最低位 0） |
| `1` | 正在被写者持有（脏值，读者应判定为无效） |

写者释放时把 stamp 推进 2（而非 1），保证最低位回到 0、并让 stamp 单调变化：

\[ \text{stamp}_{\text{new}} = (\text{stamp}_{\text{old}} + 2) \bmod 2^{W} \]

**乐观读协议**（`atomic_load` 的兜底分支）：

```
1. stamp = optimistic_read()        // 读 state(Acquire)；若==1 返回 None
2. data = read_volatile(src)        // 可能读到正在被写者改写的「半成品」字节（MaybeUninit）
3. if validate_read(stamp):         // fence(Acquire) 后再读 state，看是否仍 == stamp
        return data.assume_init()   // 期间无写者，数据有效
4. 否则：拿 write 锁 → 普通读 → abort（不推进 stamp，因为没改数据）
```

写者协议（`SeqLock::write` + guard `Drop`）：

```
write():  loop { swap(state, 1, Acquire) } 用 Backoff::snooze 退避，直到 previous != 1
          fence(Release)；返回 guard，记下 previous（即旧 stamp）
guard.drop():  store(state, old + 2, Release)   // 解锁并推进 stamp
guard.abort(): store(state, old, Release)        // 仅读者用：解锁但不推进 stamp
```

#### 4.3.3 源码精读

`lock()` 与那 67 把锁，[atomic_cell.rs:963-995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L963-L995)：

```rust
const LEN: usize = 67;                                  // 质数，打散地址
const L: CachePadded<SeqLock> = CachePadded::new(SeqLock::new());
static LOCKS: [CachePadded<SeqLock>; LEN] = [L; LEN];  // 每把锁独占缓存行

&LOCKS[addr % LEN]                                      // 选锁
```

注释 [atomic_cell.rs:966-987](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L966-L987) 详细解释了「为什么是质数」。`addr` 来自 `self.as_ptr() as usize`（见 4.2.3 的 `fetch_add` 兜底分支），所以同一个 `AtomicCell` 的所有操作总是命中**同一把锁**——这是正确性的前提。

序列锁结构与方法，[seq_lock.rs:9-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L9-L15)（`state: AtomicUsize`），乐观读 [seq_lock.rs:27-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L27-L31)：

```rust
pub(crate) fn optimistic_read(&self) -> Option<usize> {
    let state = self.state.load(Ordering::Acquire);
    if state == 1 { None } else { Some(state) }   // 锁住时返回 None
}
```

读后校验，[seq_lock.rs:37-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L37-L41)。注意先 `fence(Acquire)` 再 `Relaxed` 读：fence 保证「如果发生过写，写者的 `Release` store 对我可见」，随后的 `Relaxed` 读只是取最新值做比较：

```rust
pub(crate) fn validate_read(&self, stamp: usize) -> bool {
    atomic::fence(Ordering::Acquire);
    self.state.load(Ordering::Relaxed) == stamp
}
```

写者加锁，[seq_lock.rs:44-61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L44-L61)——用 `swap(1, Acquire)` 自旋抢锁，失败时调用 `Backoff::snooze()`（u2-l1 的退避器）让出时间片：

```rust
pub(crate) fn write(&'static self) -> SeqLockWriteGuard {
    let backoff = Backoff::new();
    loop {
        let previous = self.state.swap(1, Ordering::Acquire);
        if previous != 1 {
            atomic::fence(Ordering::Release);
            return SeqLockWriteGuard { lock: self, state: previous };
        }
        backoff.snooze();
    }
}
```

guard 的两种释放方式，[seq_lock.rs:73-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L73-L93)：

```rust
fn abort(self) {                               // 读者专用：解锁但不推进 stamp
    self.lock.state.store(self.state, Ordering::Release);
    mem::forget(self);                         // 阻止 drop 再 store 一次
}
fn drop(&mut self) {                           // 写者专用：解锁并推进 stamp
    self.lock.state.store(self.state.wrapping_add(2), Ordering::Release);
}
```

把这套协议拼起来的，是四个底层函数。最值得读的是 `atomic_load` 的兜底分支，[atomic_cell.rs:1043-1067](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1043-L1067)：

```rust
// 1) 乐观读
if let Some(stamp) = lock.optimistic_read() {
    let val = unsafe { ptr::read_volatile(src.cast::<MaybeUninit<T>>()) };
    if lock.validate_read(stamp) {
        return unsafe { val.assume_init() };   // 校验通过才信任数据
    }
}
// 2) 退化为写锁读，读完 abort（不推进 stamp）
let guard = lock.write();
let val = unsafe { ptr::read(src) };
guard.abort();
val
```

> 注释 [atomic_cell.rs:1049-1053](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1049-L1053) 坦承：理论上对非原子内存的并发读写「始终是 UB」，乐观读只是工程上最优的近似；彻底解决需要任意尺寸的稳定原子类型。这是 crossbeam 对自身 unsafe 边界的诚实标注。

对照看 `atomic_store` / `atomic_swap` 的兜底就很简单了——拿写锁、写、靠 guard `drop` 自动推进 stamp，[atomic_cell.rs:1083-1087](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1083-L1087) 与 [atomic_cell.rs:1103-1107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1103-L1107)。`atomic_compare_exchange_weak` 的兜底则在「比较失败」时调 `abort()`（没改数据，不该连累其他乐观读者），[atomic_cell.rs:1155-1166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1155-L1166)。

#### 4.3.4 代码实践

**目标**：亲手让一个「大尺寸 `T`」走序列锁路径，并算出它命中 67 把锁中的哪一把。

**操作步骤**（示例代码，独立运行）：

```rust
use crossbeam_utils::atomic::AtomicCell;

fn main() {
    // 尺寸 1000 > 任何原生原子 → 必然走全局锁
    let big: AtomicCell<[u64; 16]> = AtomicCell::new([0; 16]);
    println!("is_lock_free = {}", AtomicCell::<[u64; 16]>::is_lock_free()); // 期望 false

    // lock() 用的就是 (as_ptr() as usize) % 67
    let addr = big.as_ptr() as usize;
    println!("addr          = {addr:#x}");
    println!("命中锁下标    = {} (addr % 67)", addr % 67);

    // 再造一个相邻的 AtomicCell，观察是否落到不同锁
    let big2: AtomicCell<[u64; 16]> = AtomicCell::new([0; 16]);
    println!("big2 锁下标   = {}", (big2.as_ptr() as usize) % 67);
}
```

**阅读任务**：对照 [atomic_cell.rs:963-995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L963-L995) 的 `lock()`，确认你的手算下标与源码公式一致；再阅读 `atomic_load` 兜底分支 [atomic_cell.rs:1043-1067](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1043-L1067)，在纸上画出「乐观读 → 校验失败 → 写锁读 → abort」的状态流转。

**需要观察的现象**：`is_lock_free` 为 `false`；两个相邻的 `AtomicCell` 因地址相差 128 字节，`% 67` 后大概率落到不同下标。

**预期结果**：输出一个 0–66 之间的下标，且与源码公式吻合。地址与具体下标取决于运行时分配，**待本地验证**具体数值。

#### 4.3.5 小练习与答案

**练习 1**：stamp 为什么是 `+2` 而不是 `+1`？

> **答案**：state 的最低位是「锁位」：`1` 表示锁定，偶数表示未锁定且 stamp 即该偶数。`+2`（而非 `+1`）能保证释放后最低位回到 0（仍为偶数、仍未锁定），同时让 stamp 单调变化以使乐观读者校验失败。若 `+1`，释放后 state 变成奇数，会被误判成……其实奇数 ≠ 1 时 `optimistic_read` 仍返回 `Some`，但 stamp 与旧值混在同一边界上，语义混乱且无法区分「锁住」与「推进过」。见 [seq_lock.rs:85-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L85-L93)。

**练习 2**：乐观读失败后，`atomic_load` 为什么选择「拿写锁读、然后 `abort()`」，而不是直接重试乐观读？

> **答案**：持续重试乐观读可能被写者持续饿死（每次都撞上写）。拿写锁读能保证拿到一个一致快照，且因为读操作不改数据，用 `abort()` 释放**不推进 stamp**，避免无谓地让其他乐观读者集体失效。见 [atomic_cell.rs:1061-1066](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1061-L1066)。

**练习 3**：为什么 67 把锁每一把都要 `CachePadded`？

> **答案**：多线程会同时争用/读取相邻下标的锁变量；若它们挤在同一条缓存行，任意一把锁的 state 变更都会让整行在核间乒乓失效——这正是 u2-l2 讲的伪共享。`CachePadded` 让每把锁独占缓存行。见 [atomic_cell.rs:989-990](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L989-L990)。

## 5. 综合实践

把三条线索串起来：用**并发交换测试**对比「lock-free 路径」与「序列锁路径」的真实行为，并亲手画出大尺寸 `T` 写操作选锁的过程。

**任务 A：并发交换对比**

```rust
// 示例代码：需要 crossbeam-utils 与 crossbeam-utils 的 thread（scope）
use crossbeam_utils::atomic::AtomicCell;
use crossbeam_utils::thread;

fn stress_swap<T: Copy + Send + Default + PartialEq + std::fmt::Debug>(
    cell: &AtomicCell<T>, make: impl Fn(usize) -> T + Send + Sync, n: usize,
) {
    thread::scope(|s| {
        for t in 0..4 {
            let cell = cell;
            let make = &make;
            s.spawn(move |_| {
                for i in 0..n {
                    let v = make(t * 1_000_000 + i);
                    cell.swap(v);        // u64 走原子；(u64,u64) 走全局锁
                }
            });
        }
    }).unwrap();
}

fn main() {
    // 路径 1：lock-free（多数平台）
    let a: AtomicCell<u64> = AtomicCell::new(0);
    stress_swap(&a, |i| i as u64, 100_000);
    println!("u64        is_lock_free = {}", AtomicCell::<u64>::is_lock_free());

    // 路径 2：全局序列锁（(u64,u64) 对齐 8 < u128 对齐 16）
    let b: AtomicCell<(u64, u64)> = AtomicCell::new((0, 0));
    stress_swap(&b, |i| (i as u64, i as u64), 100_000);
    println!("(u64,u64)  is_lock_free = {}", AtomicCell::<(u64, u64)>::is_lock_free());
}
```

**任务 B：画出选锁链路**

针对上面那个 `AtomicCell<(u64, u64)>` 的一次 `swap`，在纸上画出完整调用链：

1. `swap` → [atomic_cell.rs:200-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L200-L202) 调 `atomic_swap`；
2. `atomic!` 宏因 `can_transmute::<(u64,u64), _>` 全部落空 → 走兜底分支 [atomic_cell.rs:1103-1107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1103-L1107)；
3. `lock(self.as_ptr() as usize)` → [atomic_cell.rs:990 & 994](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L990-L994) 取 `LOCKS[addr % 67]`；
4. `SeqLock::write()` 抢锁（`Backoff::snooze` 退避）→ `ptr::replace` 写值 → guard `drop` 推进 stamp。

**预期结果**：两个 cell 都能正确完成 40 万次并发 `swap` 不崩溃、不数据竞争（可用 `cargo +nightly -Zsanitizer=thread` 跑一遍验证，注意源码已坦承乐观读在严格意义上仍是 data race，thread-sanitizer 可能报警，这是已知工程取舍）。

**观察点**：在同等并发度下，`(u64, u64)` 路径因每次写都要抢全局锁、还要 `Backoff` 退避，吞吐应明显低于 `u64`。**待本地验证**具体数值。

## 6. 本讲小结

- `AtomicCell<T>` 用 `#[repr(transparent)]` + `UnsafeCell<MaybeUninit<T>>` 布局：与 `T` 同布局以便指针互转，`UnsafeCell` 提供内部可变性，`MaybeUninit` 防止编译器假定读到的一定是合法 `T`（issue #833）。
- 因为 `MaybeUninit` 屏蔽了自动 Drop，`AtomicCell` 必须手写 `Drop`，并在 `store` 非 `Copy` 类型时走 `swap` 以回收旧值。
- 每个操作经 `atomic!` 宏分发：`can_transmute`（尺寸等、对齐 `A≥B`）命中原生原子类型就走原子指令，否则走全局序列锁。
- 序列锁兜底用 **67 把 `CachePadded<SeqLock>`**，按 `addr % 67` 选锁（质数打散地址聚集），读路径用「乐观读 + stamp 校验」、写路径用 `swap(1)` 抢锁并 `Backoff::snooze` 退避。
- stamp 在写释放时 `wrapping_add(2)`（保最低位为 0、stamp 单调）；纯读退路用 `abort()` 释放以不推进 stamp，避免连累其他读者。
- 这套设计是 u2-l1（Backoff 退避）与 u2-l2（CachePadded 隔离）的直接组合应用。

## 7. 下一步学习建议

- **u2-l4（AtomicConsume 与序列锁）**：本讲的序列锁在 16/32 位平台上会换成 `seq_lock_wide.rs` 的双字计数器以防回绕，并配合 `AtomicConsume` 的 consume 内存序优化依赖链读取——那是序列锁故事的下半段。
- **u4-l1（ArrayQueue）**：你会再次见到「stamp + 缓存行对齐」的组合，只不过那里用 lap（代次）编码防 ABA，思路与本讲的 seqlock stamp 异曲同工。
- 继续阅读 [crossbeam-utils/src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) 的 `impl_arithmetic!` 宏（[atomic_cell.rs:377-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L377-L683)），它把同一套 `atomic!` 双分支套路批量生成给所有整数原语的 `fetch_add/sub/and/or/...`。
