# AtomicCell 的无锁路径

## 1. 本讲目标

上一讲（u2-l1）我们看清了 `AtomicCell` 对外暴露的「契约」：它的结构是 `#[repr(transparent)]` 包裹的 `UnsafeCell<MaybeUninit<T>>`，对外提供 `load`/`store`/`swap`/`compare_exchange` 等方法，且内存序固定为 `load=Acquire`、`store=Release`、读改写=`AcqRel`。

但上一讲刻意回避了一个关键问题：**这些方法到底是怎么执行的？** 是直接用一条 CPU 原子指令，还是退化成「拿一把全局锁」再操作？这正是本讲要回答的问题。

学完本讲，你应当能够：

1. 理解 `AtomicCell` 在**编译期**就决定了对类型 `T` 走「无锁原子指令」还是「全局锁回退」，并能解释为什么是编译期决定。
2. 读懂 `can_transmute` 这个 `const fn` 的判定逻辑：为什么要求 `size` 相等、`align` 满足 `>=`。
3. 读懂 `atomic!` 宏如何像一台「编译期分发器」一样，依次尝试 `AtomicUnit`、各宽度的 `AtomicMaybeUninit<uN>`，最后才回退。
4. 理解 `()`（零大小类型）为什么永远是无锁的——靠的是对 `AtomicUnit` 的特判，以及 `is_lock_free()` 的真实含义。

> 本讲只讲「无锁路径是怎么被选中的」，**不展开**回退路径里 `SeqLock` 的印戳机制（那是下一讲 u2-l3 的内容），但会指出回退发生在何处。

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念说清楚。

### 2.1 什么是「无锁（lock-free）」

「无锁」在这里是一个非常具体的含义：**这一步操作能不能用单条 CPU 原子指令完成**（比如 `LOCK XADD`、`LDXR/STXR`）。

- `std::sync::atomic::AtomicUsize` 之所以能在多线程下安全地 `fetch_add`，是因为底层有一条原子指令保证「读-改-写」不可被打断。
- 但 CPU 的原子指令**只支持固定的几种位宽**：通常 8/16/32/64 位，少数平台有 128 位。一个 1000 字节的类型，CPU 没有任何一条指令能原子地读写它。
- 对那些「装不进任何原生原子类型」的 `T`，`AtomicCell` 的兜底办法是：**拿一把全局锁**，把整个操作串行化。这就不算无锁了。

所以「无锁 vs 回退」本质上是：**`T` 能不能被 reinterpret 成某个原生原子类型来操作。**

### 2.2 `size_of` 与 `align_of`

Rust 中每个类型有两个编译期就确定的布局属性：

- `size_of::<T>()`：`T` 占多少字节。
- `align_of::<T>()`：`T` 的对齐要求（字节数）。`T` 的每个实例都必须存放在「对齐值的整数倍」的地址上。

关键性质：**Rust 中所有类型的 `align_of` 都是 2 的幂**。这意味着若 `align_of::<A>() >= align_of::<B>()`，那么任何「A 对齐的地址」必然也满足 B 的对齐（因为大对齐是小米对齐的整数倍）。本讲的 `align >=` 判据就依赖这一条。

### 2.3 `transmute` 与 `transmute_copy`

- `mem::transmute_copy(&x)` 会把 `x` 的原始字节按位复制成另一个类型 `U` 的值（要求 `size_of::<T>() >= size_of::<U>()`）。
- 它是「按比特重新解释」，不调用任何转换函数。`AtomicCell` 正是用它把「用户类型 `T` 的字节」和「原子类型 `AtomicUsize` 的字节」相互转换。

### 2.4 `const fn` 与编译期求值

`const fn` 是「可以在编译期求值的函数」。当一个 `const fn` 的所有参数在编译期都已知（比如 `size_of`、`align_of` 这种本身就是编译期常量），编译器就能把整个调用折叠成一个常量。本讲的 `can_transmute` 被刻意写成 `const fn`，正是为了让宏里那一长串 `if can_transmute::<...>()` 在编译期被优化掉绝大部分分支。

## 3. 本讲源码地图

本讲集中在两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) | `AtomicCell` 的全部实现，包括本讲的 `can_transmute`、`atomic!` 宏、`AtomicUnit`、`atomic_is_lock_free`，以及 `atomic_load`/`atomic_store`/`atomic_swap`/`atomic_compare_exchange_weak` 四个使用宏的自由函数。 |
| [src/atomic/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/mod.rs) | 模块门面，负责按 `cfg` 把 `AtomicCell` 与 `seq_lock` 暴露出来。 |

回退路径涉及的 [src/atomic/seq_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs) 本讲只引用、不展开，留给 u2-l3。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`can_transmute`**：编译期判定「`T` 能否被 reinterpret 成某个原子类型」。
2. **`atomic!` 宏**：在 `can_transmute` 之上做编译期 + 运行期混合分发。
3. **`AtomicUnit` 与 `atomic_is_lock_free`**：零大小类型 `()` 的特判，以及 `is_lock_free()` 的真实含义。

### 4.1 为什么需要两条路径

#### 4.1.1 概念说明

`AtomicCell<T>` 的类型文档里有一句很关键的话：

> Operations on `AtomicCell`s use atomic instructions whenever possible, and synchronize using global locks otherwise.

翻译过来就是：**能上原子指令就上原子指令，不行就用全局锁。** 这句话直接定义了本讲的全部主题。

为什么要这么设计？因为 `AtomicCell<T>` 是泛型的，`T` 可以是任何 `Send` 类型：

- 当 `T = usize` 时，我们希望它和 `AtomicUsize` 一样快——一条原子指令搞定。
- 当 `T = [u8; 1000]` 时，没有任何原子指令能一次处理 1000 字节，只能退而求其次用锁。

`AtomicCell` 的巧妙之处在于：**这种选择是自动的、对用户透明的。** 用户只管写 `AtomicCell::<T>::new(...)`，库自己判断该走哪条路。而判断的依据，就是「`T` 能不能 transmute 成某个原生原子类型」。

#### 4.1.2 核心流程

可以把整个分发流程画成一张「漏斗」：

```
       对类型 T 的某次原子操作（如 load）
                    │
            进入 atomic! 宏的 loop
                    │
        ┌───────────▼───────────┐
        │ T 能 transmute 成      │── 是 ──► 用原生原子类型执行（无锁路径）
        │ 某个原生原子类型？      │
        └───────────┬───────────┘
                    │ 否
        ┌───────────▼───────────┐
        │  走全局锁（SeqLock 锁池） │  （回退路径，u2-l3 详讲）
        └───────────────────────┘
```

而「能不能 transmute」由 `can_transmute` 判定，「如何根据判定结果选路」由 `atomic!` 宏完成。下面逐个拆开。

### 4.2 `can_transmute`：编译期可转换判定

#### 4.2.1 概念说明

`can_transmute<A, B>()` 回答一个非常具体的问题：**类型 `A` 的值，能不能安全地按比特 reinterpret 成类型 `B`？** 在本讲的语境里，`A` 是用户类型 `T`，`B` 是某个原生原子类型（如 `AtomicMaybeUninit<u64>`）。

为什么需要这个判定？因为 `AtomicCell` 内部存储的是 `T` 的字节（`repr(transparent)` 保证了布局等同 `T`），而无锁路径要做的就是把指向这些字节的指针强转成 `*const AtomicU64` 之类，再用原生原子类型的方法去操作。这个强转只有在 `T` 和原子类型「布局兼容」时才是合法的（sound）。

#### 4.2.2 核心流程

`can_transmute` 的判定只有两个条件，用按位与 `&` 串起来：

\[ \texttt{can\_transmute}(A, B) = (\,\text{size\_of}(A) = \text{size\_of}(B)\,) \;\wedge\; (\,\text{align\_of}(A) \geq \text{align\_of}(B)\,) \]

两个条件各自的含义：

1. **`size_of::<A>() == size_of::<B>()`（大小必须相等）**
   原子指令只读取/写入「原子类型那么大」的字节。如果原子类型比 `T` 小，会漏读 `T` 的高位字节；如果比 `T` 大，会越界读到相邻内存。所以必须**精确相等**，保证一次原子操作恰好覆盖 `T` 的全部字节。

2. **`align_of::<A>() >= align_of::<B>()`（A 的对齐必须不低于 B）**
   这是为了让「把 `*mut T` 强转成 `*const AtomicU64`」不违反对齐要求。`T` 实例存放在「`T` 对齐的地址」上；只有当这个地址也满足原子类型的对齐时，强转后的引用才合法。由于 Rust 的对齐都是 2 的幂，`align(A) >= align(B)` 就能保证「`T` 对齐的地址必然也是 `B` 对齐的」。

   举一个反例体会一下：`#[repr(C)] struct Two(u32, u32)` 的大小是 8、对齐是 4。它**不能**用 `AtomicU64`（对齐 8）：因为一个 4 对齐的地址（比如 `0x...4`）未必是 8 对齐的，强转就会产生未定义行为。

> 注意 `can_transmute` 用的是 `&`（按位与）而非 `&&`（逻辑与）。因为它是 `const fn`，而在旧版本 Rust 的 `const` 上下文里，`&&` 的短路语义曾受限；用 `&` 配合 `bool` 语义等价且兼容性更好。这是一个有历史原因的写法。

#### 4.2.3 源码精读

判定函数本身非常短：

[`can_transmute` 常量函数，size 等号 + align 大于等于号](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L949-L953)

```rust
/// Returns `true` if values of type `A` can be transmuted into values of type `B`.
const fn can_transmute<A, B>() -> bool {
    // Sizes must be equal, but alignment of `A` must be greater or equal than that of `B`.
    (mem::size_of::<A>() == mem::size_of::<B>()) & (mem::align_of::<A>() >= mem::align_of::<B>())
}
```

两点要特别注意：

- 它是 **`const fn`**。这是后面宏能在编译期折叠分支的前提。
- 它没有判断「`A`、`B` 的位模式是否语义兼容」（比如把 `f64` 的字节当成 `u64` 操作是否合理）。`AtomicCell` 的无锁路径只关心**布局兼容**，语义正确性由更大的设计（`repr(transparent)`、固定的内存序、以及 `T: Copy` 约束）来兜底。

#### 4.2.4 代码实践

**实践目标**：用 `size_of` / `align_of` 自己复刻 `can_transmute` 的逻辑，预测若干类型能否被 `u64` 原子类型无锁化，体会「大小相等 + 对齐足够」两个条件。

**操作步骤**（在 u1-l1 创建的 binary crate 中，记得 `cargo add crossbeam-utils --features atomic`，因为 `AtomicCell` 需要 `atomic` feature）：

```rust
// 示例代码：复刻 can_transmute 判定，预测 T 能否 transmute 成 u64 原子类型
fn can_be_u64_atomic<T>() -> bool {
    // AtomicMaybeUninit<u64> 的大小是 8、对齐是 8（典型 64 位平台）
    (std::mem::size_of::<T>() == 8) & (std::mem::align_of::<T>() >= 8)
}

#[repr(C)]
struct SingleU64(u64);          // size 8, align 8

#[repr(C)]
struct TwoU32(u32, u32);        // size 8, align 4  ← 关键反例

fn main() {
    println!("u64        -> {}", can_be_u64_atomic::<u64>());        // 预测 true
    println!("SingleU64  -> {}", can_be_u64_atomic::<SingleU64>()); // 预测 true
    println!("TwoU32     -> {}", can_be_u64_atomic::<TwoU32>());    // 预测 false（对齐不足）
}
```

**需要观察的现象**：`TwoU32` 虽然「大小恰好 8 字节」，但因为对齐只有 4，不满足 `align >= 8`，所以**不能**用 `u64` 原子类型。

**预期结果**：`true`、`true`、`false`。这意味着即便 `TwoU32` 是 8 字节，它的 `AtomicCell` 也无法走 `u64` 无锁路径——要么退化到更窄的原子（但 `size` 又不匹配），要么直接走全局锁。

#### 4.2.5 小练习与答案

**练习 1**：`#[repr(C)] struct Mixed(u8, u32)` 的 `size_of` 和 `align_of` 各是多少？它能否 transmute 成 `AtomicMaybeUninit<u32>`？

**参考答案**：`repr(C)` 下 `Mixed` 布局为 `u8` + 3 字节填充 + `u32`，`size_of = 8`、`align_of = 4`。要 transmute 成 `AtomicMaybeUninit<u32>`（size 4）：`size_of` 不等（8 ≠ 4），所以**不能**。它也无法用 `u64`（align 4 < 8 不满足）。最终 `AtomicCell<Mixed>` 会走全局锁回退。

**练习 2**：为什么 `can_transmute` 的对齐判据是 `align_of::<A>() >= align_of::<B>()`，而不是反过来 `<=`？

**参考答案**：因为我们手上的指针来自 `T`（即 `A`），它指向一个「`A` 对齐的地址」。要把这个指针当作 `&B` 来用，这个地址就必须**也**满足 `B` 的对齐。`A` 的对齐越严格（越大），地址越「规整」，越能满足 `B` 的（较松的）对齐要求。所以是 `A` 的对齐 `>=` `B` 的对齐。

---

### 4.3 `atomic!` 宏：编译期分发器

#### 4.3.1 概念说明

光有 `can_transmute` 还不够——我们还要在「能 transmute 的若干候选原子类型里挑一个」和「全都不行就回退」之间做出选择。这件事由 `atomic!` 宏完成。

`atomic!` 宏是本讲最精巧的部分。它**不是**纯运行期的 `if/else` 链，而是「编译期 cfg 裁剪 + 运行期 `const fn` 折叠」的混合体：

- **编译期**：根据目标平台支持哪些原子位宽（`cfg_has_atomic_8/16/32/64/128`），把不支持的候选直接从代码里删掉；在 `miri`/`loom`/`force_fallback` 环境下，干脆删掉所有候选，强制回退。
- **运行期**（或经优化器折叠后）：依次用 `can_transmute` 测试每个候选，第一个匹配的就 `break` 出去执行无锁代码；都不匹配则执行回退分支。

#### 4.3.2 核心流程

宏分两个「臂」（arm）。先看内部的 `@check` 臂——它是一个工具人，负责「如果 `T` 能 transmute 成 `$atomic`，就跳出循环执行 `$atomic_op`」：

```
@check(T, AtomicType, a, 原子操作):
    如果 can_transmute::<T, AtomicType>() 为真:
        声明 let a: &AtomicType;        ← 仅用于给 a 标注类型
        break 执行「原子操作」            ← 原子操作内部会给 a 赋值并使用它
```

再看主臂，它把所有候选按顺序串起来：

```
atomic!(T, a, 原子操作, 回退操作):
    loop {
        @check(T, AtomicUnit, a, 原子操作)        // ① 永远先试零大小的 AtomicUnit

        #[cfg(不是 miri/loom/force_fallback)]     // ② 这些环境强制回退，跳过候选
        cfg_has_atomic_cas! {                      //    且仅当平台支持原子 CAS
            cfg_has_atomic_8!  { @check(T, AtomicMaybeUninit<u8>,  ...) }
            cfg_has_atomic_16! { @check(T, AtomicMaybeUninit<u16>, ...) }
            cfg_has_atomic_32! { @check(T, AtomicMaybeUninit<u32>, ...) }
            cfg_has_atomic_64! { @check(T, AtomicMaybeUninit<u64>, ...) }
            cfg_has_atomic_128!{ @check(T, AtomicMaybeUninit<u128>,...) }
        }

        break 回退操作                              // ③ 全都不匹配，走全局锁
    }
```

三个要点：

1. **`AtomicUnit` 优先且无条件**：它的 `@check` 不被任何 `cfg` 包围，所以总是第一个被尝试。这是 `()` 能无锁的关键（见 4.4 节）。
2. **候选列表由平台决定**：`cfg_has_atomic_N!` 是 `atomic-maybe-uninit` crate 提供的宏，只有在目标平台「支持 N 位原子」时才展开成其内容，否则展开为空。这就是 `AtomicCell` 能适配 MIPS、ARM 等缺少某些位宽原子的平台的机制。
3. **回退是最后的兜底**：所有候选都不命中时，`break $fallback_op` 执行回退分支（拿全局锁）。

> 为什么用 `loop { ... break ... }` 这种写法？因为 Rust 宏里不能直接写「返回一个值」，但 `loop` 可以用 `break <expr>` 把一个表达式的值「交」出去。每个 `@check` 命中时就 `break` 出对应的原子操作，最后再 `break` 出回退操作。这是一种用控制流模拟「多路选择并求值」的常见宏技巧。

#### 4.3.3 源码精读

先看 `@check` 臂：

[`atomic!` 宏的 `@check` 臂：命中即 break](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L331-L339)

```rust
macro_rules! atomic {
    (@check, $t:ty, $atomic:ty, $a:ident, $atomic_op:expr) => {
        if can_transmute::<$t, $atomic>() {
            let $a: &$atomic;
            break $atomic_op;
        }
    };
```

注意 `let $a: &$atomic;` 这一行——它声明了一个**未初始化**的变量 `$a`，只为了给 `$a` 标注类型。真正给 `$a` 赋值的是后面的 `$atomic_op`（见下方 `atomic_load` 里 `a = unsafe { &*(...) }`）。这种「先声明类型，由后续块赋值」的写法，是为了让宏能在不同 `$atomic` 类型下复用同一段 `$atomic_op` 代码——每个候选分支都能通过类型检查，即便运行时只有一个会真正执行。

再看主臂：

[`atomic!` 宏主臂：依次尝试 AtomicUnit → 各宽度原子 → 回退](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L344-L374)

```rust
    ($t:ty, $a:ident, $atomic_op:expr, $fallback_op:expr) => {
        loop {
            atomic!(@check, $t, AtomicUnit, $a, $atomic_op);

            // Always use fallback for now on environments that do not support inline assembly.
            #[cfg(not(any(
                miri,
                crossbeam_loom,
                crossbeam_atomic_cell_force_fallback,
            )))]
            atomic_maybe_uninit::cfg_has_atomic_cas! {
                atomic_maybe_uninit::cfg_has_atomic_8! {
                    atomic!(@check, $t, atomic_maybe_uninit::AtomicMaybeUninit<u8>, $a, $atomic_op);
                }
                // ... 16 / 32 / 64 / 128 同理
            }

            break $fallback_op;
        }
    };
}
```

最关键的一行注释：`Always use fallback for now on environments that do not support inline assembly.` 在 `miri`、`loom`、以及 `crossbeam_atomic_cell_force_fallback`（任意 sanitizer 时由 `build.rs` 发出，见 u1-l3）下，整个候选块被 `cfg` 删掉，于是只有 `AtomicUnit` 和回退两条路——这对除 `()` 以外的所有类型都意味着「强制走全局锁」。这样做的目的是：这些验证工具无法准确模拟原子指令的内存表示，干脆让它们只测回退路径。

那么这个宏被谁用？答案是四个自由函数 `atomic_load` / `atomic_store` / `atomic_swap` / `atomic_compare_exchange_weak`，而 `AtomicCell` 的公共方法（`load`/`store`/`swap`/`compare_exchange`）只是转调它们。以 `atomic_load` 为例：

[`atomic_load` 用 `atomic!` 在无锁路径与 SeqLock 乐观读之间二选一](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1033-L1069)

```rust
unsafe fn atomic_load<T>(src: *mut T) -> T
where T: Copy,
{
    atomic! {
        T, a,
        {
            // 无锁路径：把指针强转成原生原子类型，执行原子 load
            a = unsafe { &*(src as *const _ as *const _) };
            unsafe { mem::transmute_copy(&a.load(Ordering::Acquire)) }
        },
        {
            // 回退路径：SeqLock 乐观读（u2-l3 详讲）
            let lock = lock(src as usize);
            if let Some(stamp) = lock.optimistic_read() {
                let val = unsafe { ptr::read_volatile(src.cast::<MaybeUninit<T>>()) };
                if lock.validate_read(stamp) {
                    return unsafe { val.assume_init() };
                }
            }
            // ...退化为加锁读
        }
    }
}
```

可以清楚看到：宏的第一个参数是类型 `T`，第二、三、四个参数分别是「变量名 `a`」「无锁操作」「回退操作」。命中无锁路径时，`a` 被强转成具体的 `&AtomicU64`（或其它宽度），然后 `a.load(Ordering::Acquire)` 读出原子值，再 `transmute_copy` 还原成 `T`。`store`/`swap`/`compare_exchange_weak` 的套路完全一致，只是内存序换成 `Release` 或 `AcqRel`。

#### 4.3.4 代码实践

**实践目标**：用一个真实的 `AtomicCell` 操作，结合 `cargo expand` 直观看到宏在具体类型上「折叠」成了什么。

**操作步骤**：

1. 安装宏展开工具：`cargo install cargo-expand`（需要 nightly）。
2. 在你的 binary crate 里写：

   ```rust
   use crossbeam_utils::atomic::AtomicCell;

   fn main() {
       let a: AtomicCell<u64> = AtomicCell::new(7);
       let _ = a.load();          // 应走 u64 无锁路径
       let b: AtomicCell<[u8; 1000]> = AtomicCell::new([0u8; 1000]);
       let _ = b.load();          // 应走全局锁回退
   }
   ```

   注意 `AtomicCell<[u8;1000]>` 的 `load` 需要 `[u8;1000]: Copy`（成立），且需要 `atomic` feature。

3. 运行 `cargo +nightly expand --release --features atomic`，在输出里搜索 `atomic_load` 展开后的 `loop`。

**需要观察的现象**：

- 对 `AtomicCell<u64>`，展开后那一串 `if can_transmute::<u64, AtomicMaybeUninit<u8>>()` … 在 `u64` 那一条命中 `break`；release 模式下经优化，无关分支应被消除。
- 对 `AtomicCell<[u8; 1000]>>`，所有 `can_transmute` 都为假，最终落到 `break $fallback_op`（SeqLock 路径）。

**预期结果**：肉眼可见「`u64` 命中某条 `@check`」，而 `[u8; 1000]` 落到回退。若 `cargo expand` 不便安装，跳过此步亦可——可改为在第 4.4 节用 `is_lock_free()` 间接验证路径选择。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AtomicUnit` 的 `@check` 写在所有 `cfg_has_atomic_N` 之前，且不被任何 `cfg` 包围？

**参考答案**：因为 `()`（零大小类型）在任何平台、任何环境下都应当被视为「无锁」——它根本没有任何字节需要原子地读写。把它无条件放在最前，保证 `AtomicCell<()>` 在 `miri`/`loom`/`force_fallback` 下依然命中无锁路径，而不是错误地走全局锁。

**练习 2**：在一个「没有 64 位原子」的嵌入式平台上，`AtomicCell<u64>` 会发生什么？

**参考答案**：`cfg_has_atomic_64!` 在该平台展开为空，所以 `@check(T, AtomicMaybeUninit<u64>, ...)` 这条候选根本不存在；`u64` 又匹配不了 8/16/32 位的候选（`size` 不等）。于是所有候选都不命中，落到 `break $fallback_op`，走全局锁。这正是 `AtomicCell` 跨平台自适应的体现。

---

### 4.4 `AtomicUnit` 与 `atomic_is_lock_free`：零大小类型的特判

#### 4.4.1 概念说明

至此还有一个边角没解释：**`()` 这种零大小类型怎么办？** 它的 `size_of` 是 0，没有任何一个 `AtomicMaybeUninit<uN>` 能和它 `size` 相等。但直觉上 `()` 根本不占内存，对它的「原子操作」应该是无锁的——因为什么都不用做。

`AtomicCell` 用一个专门的占位类型 `AtomicUnit` 来处理这种情况。`AtomicUnit` 是一个零大小的 marker 类型，它的所有「原子操作」都是空操作（no-op）。配合 `can_transmute`，`()` 恰好能 transmute 成 `AtomicUnit`（两者都是 size 0、align 1），于是命中第一条 `@check`，走「什么都不做」的无锁路径。

而对外，用户通过 `AtomicCell::<T>::is_lock_free()` 查询某类型是否走无锁路径。它的实现极其巧妙——**复用同一个 `atomic!` 宏**，只是把「原子操作」换成 `true`、把「回退操作」换成 `false`。这样 `is_lock_free` 返回的真假，就和实际操作走的路径**完全一致**，不会有「查询说无锁、实际却加锁」的脱节。

#### 4.4.2 核心流程

`is_lock_free` 的判定逻辑可以表示为：

\[ \texttt{is\_lock\_free}(T) = \begin{cases} \text{true} & \text{若 } T \text{ 能 transmute 成 AtomicUnit（即零大小）} \\ \text{true} & \text{若存在平台支持的原子宽度 } N \text{，使 } can\_transmute(T, \text{Atomic}\langle u_N\rangle) \\ \text{false} & \text{否则（走全局锁）} \end{cases} \]

并且这个结果**依赖编译环境**：在 `miri`/`loom`/`force_fallback` 下，中间那一整块候选被删掉，只有 `AtomicUnit`（零大小）为 `true`，其它一律 `false`。

`AtomicUnit` 自身则是一个「所有操作都是 no-op」的类型：

| 方法 | 行为 |
| --- | --- |
| `load(_order)` | 空操作，返回 `()` |
| `store(_val, _order)` | 空操作 |
| `swap(_val, _order)` | 空操作 |
| `compare_exchange_weak(...)` | 永远返回 `Ok(())` |

这完全正确：对 `()` 来说，读出来永远是 `()`，CAS 永远「相等且成功」，因为没有别的可能值。

#### 4.4.3 源码精读

先看 `AtomicUnit` 的定义和实现：

[`AtomicUnit`：零大小的占位原子类型，所有操作都是 no-op](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L997-L1022)

```rust
/// An atomic `()`.
///
/// All operations are noops.
struct AtomicUnit;

impl AtomicUnit {
    #[inline] fn load(&self, _order: Ordering) {}
    #[inline] fn store(&self, _val: (), _order: Ordering) {}
    #[inline] fn swap(&self, _val: (), _order: Ordering) {}
    #[inline]
    fn compare_exchange_weak(&self, _current: (), _new: (), _success: Ordering, _failure: Ordering) -> Result<(), ()> {
        Ok(())
    }
}
```

注意文件开头第 1-2 行有一句 `#![allow(clippy::unit_arg)]`——正是因为 `AtomicUnit` 的方法大量接收/返回 `()` 字面量，需要关掉 clippy 的告警。

再看 `atomic_is_lock_free` 如何复用宏：

[`atomic_is_lock_free`：用同一个 `atomic!` 宏，无锁分支返回 true、回退返回 false](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1024-L1027)

```rust
/// Returns `true` if operations on `AtomicCell<T>` are lock-free.
const fn atomic_is_lock_free<T>() -> bool {
    atomic! { T, _a, true, false }
}
```

妙处在于：变量名写成 `_a`（带下划线表示未使用），因为这里的「原子操作」就是常量 `true`，根本不需要 `a`。若宏命中任何无锁分支（`AtomicUnit` 或某个 `AtomicMaybeUninit<uN>`），就 `break true`；否则 `break false`。这样 `is_lock_free()` 与实际操作的路径选择由**同一个宏、同一套 `can_transmute` 判定**决定，二者天然一致，绝不会脱节。

公共 API 只是一层薄转发：

[`is_lock_free` 公共方法，转发到 `atomic_is_lock_free`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L160-L162)

```rust
pub const fn is_lock_free() -> bool {
    atomic_is_lock_free::<T>()
}
```

它是 `const fn`，所以 `AtomicCell::<usize>::is_lock_free()` 在编译期就能求值成 `true`。

#### 4.4.4 代码实践

**实践目标**：对一组精心挑选的类型调用 `is_lock_free()`，并与你基于 `can_transmute` 的预测逐项对照，验证「分发逻辑」与「查询接口」一致。

**操作步骤**（`cargo add crossbeam-utils --features atomic`）：

```rust
// 示例代码：预测并验证各类型是否 lock-free（典型 x86_64 平台，支持 8/16/32/64 位原子）
use crossbeam_utils::atomic::AtomicCell;

#[repr(C)] struct SingleIsize(isize);   // size 8, align 8
#[repr(C)] struct TwoU32(u32, u32);      // size 8, align 4

fn main() {
    // 类型 → 预测 → 实际
    let cases: &[(&str, bool, bool)] = &[
        ("usize",             true,  AtomicCell::<usize>::is_lock_free()),
        ("isize",             true,  AtomicCell::<isize>::is_lock_free()),
        ("SingleIsize",       true,  AtomicCell::<SingleIsize>::is_lock_free()),
        ("()",                true,  AtomicCell::<()>::is_lock_free()),
        ("TwoU32(8B,align4)", false, AtomicCell::<TwoU32>::is_lock_free()),
        ("[u8; 1000]",        false, AtomicCell::<[u8; 1000]>::is_lock_free()),
    ];
    for (name, predicted, actual) in cases {
        println!("{:<20} 预测={:<5} 实际={:<5} 一致={}",
                 name, predicted, actual, predicted == actual);
    }
}
```

预测依据（在支持 8/16/32/64 位原子的 x86_64 上）：

- `usize`/`isize`（size 8, align 8）→ 命中 `AtomicMaybeUninit<u64>` → `true`
- `SingleIsize`（size 8, align 8）→ 同上 → `true`
- `()`（size 0）→ 命中 `AtomicUnit` → `true`
- `TwoU32`（size 8, align 4）→ 对 `u64` 对齐不足（4 < 8），对 `u32` 大小不等 → 无候选 → `false`
- `[u8; 1000]`（size 1000）→ 无任何原子位宽匹配 → `false`

**需要观察的现象**：每行的「一致=」都应当为 `true`，说明你对 `can_transmute` 的理解和库的实际分发一致。

**预期结果**：六行全部 `一致=true`。**待本地验证**：若你在 `miri` 或设置了 `crossbeam_atomic_cell_force_fallback` 的环境下运行，除 `()` 外其余都会变成 `false`（因为候选块被 `cfg` 删除）——这正好印证 4.3 节「这些环境强制回退」的设计。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接写一个 `match size_of::<T>()` 的函数来判断是否 lock-free，而要用 `atomic!` 宏？

**参考答案**：因为是否 lock-free **不仅取决于大小**，还取决于「平台是否支持该位宽的原子」（`cfg_has_atomic_N`）和「对齐是否足够」。`match size_of` 无法在编译期感知平台的原子能力；而 `atomic!` 宏通过 `cfg_has_atomic_N!` 在编译期就把平台不支持的候选删除，并通过 `can_transmute` 同时检查大小与对齐。复用宏还保证了 `is_lock_free()` 的返回值与实际操作路径**同源**，不会脱节。

**练习 2**：`AtomicUnit::compare_exchange_weak` 永远返回 `Ok(())`，这会不会导致 `AtomicCell<()>` 的 `compare_exchange` 行为不正确？

**参考答案**：不会。`()` 只有一个可能的值，所以「当前值是否等于 `current`」永远是 `true`，CAS 应当永远成功。返回 `Ok(())`（成功，旧值为 `()`）是唯一正确的语义。这与 4.3 节里 `atomic_compare_exchange_weak` 的「语义相等则成功」逻辑一致——对 `()`，任何值都与任何值「相等」。

---

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「路径预测器」小任务。

**任务**：写一个小程序，对一个类型 `T`，**独立预测**它的 `AtomicCell<T>` 会走无锁路径还是回退路径，再调用 `is_lock_free()` 验证。要求：

1. 自己实现一个预测函数 `predict_lock_free::<T>()`，逻辑为：
   - 若 `size_of::<T>() == 0`，返回 `true`（对应 `AtomicUnit`）。
   - 否则，遍历平台支持的原子位宽集合（你可以先固定为 `{1, 2, 4, 8}`，假设这些位宽可用），若存在某个 `N` 使得 `size_of::<T>() == N` **且** `align_of::<T>() >= N`，返回 `true`。
   - 否则返回 `false`。
2. 对 `u8`、`u32`、`u64`、`()`、`TwoU32(u32,u32)`、`[u16; 3]`、`[u8; 1000]` 这几个类型，分别打印 `predict_lock_free` 与 `AtomicCell::<T>::is_lock_free()`，并断言二者相等。

**参考骨架**：

```rust
// 示例代码：综合实践骨架
use crossbeam_utils::atomic::AtomicCell;

fn predict_lock_free<T>() -> bool {
    let size = core::mem::size_of::<T>();
    let align = core::mem::align_of::<T>();
    if size == 0 {
        return true; // AtomicUnit 兜底
    }
    // 假设平台支持 1/2/4/8 位原子（典型 x86_64）
    [1usize, 2, 4, 8].iter().any(|&n| size == n && align >= n)
}

fn check<T: Default + Copy + 'static>(name: &str) {
    let predicted = predict_lock_free::<T>();
    let actual = AtomicCell::<T>::is_lock_free();
    println!("{:<14} 预测={:<5} 实际={:<5}", name, predicted, actual);
    assert_eq!(predicted, actual, "{} 预测与实际不一致", name);
}

#[repr(C)] struct TwoU32(u32, u32);

fn main() {
    check::<u8>("u8");
    check::<u32>("u32");
    check::<u64>("u64");
    check::<()>("()");
    check::<TwoU32>("TwoU32");
    check::<[u16; 3]>( "[u16;3]");
    check::<[u8; 1000]>("[u8;1000]");
    println!("全部一致！");
}
```

**思考题**：`[u16; 3]`（size 6, align 2）在你的预测里是什么结果？为什么没有「size 6」的原子能救它？（答：没有 6 字节宽的原子类型，且它也匹配不了 8 位宽——所以走全局锁。）

> 提示：本实践只验证了「路径选择」这一面。被选中的无锁路径在多线程下到底安不安全、回退路径的 `SeqLock` 如何保证读到一致快照，分别在 u5-l1（unsafe 安全性）和 u2-l3（SeqLock）中深入。

## 6. 本讲小结

- `AtomicCell<T>` 的每次操作都面临一个二选一：**用原生原子指令（无锁）**，还是**用全局锁（回退）**。选择由类型 `T` 的布局决定，对用户透明。
- [`can_transmute<A,B>()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L949-L953) 是判定的原子操作：要求 `size` 相等、`align(A) >= align(B)`，后者依赖「Rust 对齐都是 2 的幂」这一性质。
- [`atomic!` 宏](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L331-L375) 是「编译期 cfg 裁剪 + 运行期 `const fn` 折叠」的混合分发器：先试 `AtomicUnit`，再按 `cfg_has_atomic_N` 试各宽度 `AtomicMaybeUninit<uN>`，最后回退；`miri`/`loom`/`force_fallback` 会删掉所有候选强制回退。
- `AtomicCell` 的四个自由函数 [`atomic_load`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1033-L1069)/`atomic_store`/`atomic_swap`/`atomic_compare_exchange_weak` 都通过 `atomic!` 在「无锁」与「SeqLock 回退」之间二选一，公共方法只是转调它们。
- [`AtomicUnit`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L997-L1022) 是零大小类型 `()` 的占位原子，所有操作是 no-op；它让 `()` 在任何环境下都无锁。
- [`atomic_is_lock_free`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1024-L1027) 复用同一个 `atomic!` 宏（`true`/`false` 作分支），保证 `is_lock_free()` 的返回值与实际操作路径**同源且一致**。

## 7. 下一步学习建议

本讲讲清了「无锁路径如何被选中」，但留下了两块未展开的内容，正好是后续讲义的主题：

1. **下一讲 u2-l3《AtomicCell 的全局锁回退与 SeqLock》**：当 `atomic!` 宏落到 `break $fallback_op` 时会发生什么？答案是 [`lock()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L955-L995) 从 67 个 `CachePadded<SeqLock>` 组成的全局锁池里按地址取模选一把，读者用 `optimistic_read`/`validate_read` 配合 `read_volatile` 完成无锁乐观读。这是本讲的直接续篇，建议紧接着读。
2. **u5-l1《AtomicCell 的 unsafe 与内存安全深析》**：本讲多次出现 `unsafe { &*(src as *const _ as *const _) }` 这样的指针强转，它的安全性前提是什么？为什么对 `MaybeUninit` 用 `read_volatile` 是合法的？这些 `unsafe` 论证集中在进阶层讲。
3. **u5-l3《跨平台 cfg、loom 抽象与宽 SeqLock》**：本讲提到的 `cfg_has_atomic_N!`、`miri`/`force_fallback` 等 cfg 的来源（`build.rs`）、以及窄架构下的宽 SeqLock，会在进阶层系统讲解。

建议在进入 u2-l3 之前，先把本讲的「综合实践」跑一遍，确认你理解了 `can_transmute` 与 `is_lock_free` 的对应关系——这是理解回退路径「什么时候被触发」的基础。
