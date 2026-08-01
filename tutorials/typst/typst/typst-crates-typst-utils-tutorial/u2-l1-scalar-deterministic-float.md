# Scalar：可哈希可排序的确定性浮点

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么原生的 `f64` 不能放进 `HashSet`/`HashMap` 当键、不能放进 `BTreeMap`，以及 `NaN` 在其中扮演的「破坏者」角色。
- 解释 `Scalar` 是如何用一个 newtype 包装 + 构造期「NaN 规约」策略，让浮点变得可哈希、可排序的。
- 看懂 `Scalar` 的运算符重载（包括 `Scalar` 与 `f64` 的混合运算）和通过 `assign_impl!` 宏批量派生赋值运算符的写法。
- 理解 `Numeric` trait 如何用 supertrait 约束「一组统一的数值能力」，以及 `Scalar` 是如何满足它的。
- 读懂从 LLVM 移植过来的 `powi`（整数幂）算法——为什么 Typst 不直接用标准库，而是手写一段快速幂。

本讲承接 [u1-l2 扩展 trait 与辅助函数](u1-l2-extension-traits-and-helpers.md)：那里提到 `Numeric` trait 的一个实现留待本讲，正是这里的 `Scalar`。

## 2. 前置知识

本讲假设你已经了解：

- **newtype 模式**：用 `struct Scalar(f64);` 把一个类型包一层，从而为它量身定制新的 trait 实现，而不污染原始类型。这是 Rust 里给「外部类型」加行为的常用手段。
- **运算符重载**：在 Rust 里，`+ - * /` 等运算符都对应一个 trait（`Add`、`Sub`、`Mul`、`Div`…），实现了哪个 trait 就能用哪个运算符。`+=` 一类赋值运算符对应 `AddAssign` 等。
- **`Hash` / `Eq` / `Ord` 三个 trait**：`HashSet`、`HashMap` 的键要求 `Hash + Eq`；`BTreeMap`、`BTreeSet` 要求 `Ord`。这三个 trait 之间还有一条「一致性契约」：若 `a == b`，则 `hash(a)` 必须等于 `hash(b)`。
- **IEEE-754 浮点的两个坑**：
  - `NaN != NaN`（`f64::NAN == f64::NAN` 为 `false`），违反了 `Eq` 要求的「自反性」。
  - 浮点只有「偏序」(partial order)，`NaN` 与任何值都无法比较大小，没有全序，因此 `f64` 只实现了 `PartialOrd` 而非 `Ord`。

> 一句话总结：正因为上面这两点，标准库**没有**给 `f64` 实现 `Eq` / `Ord` / `Hash`。本讲的全部工作，就是造一个「修补了这些缺陷」的浮点类型。

## 3. 本讲源码地图

本讲涉及三个文件：

| 文件 | 作用 |
| --- | --- |
| [src/scalar.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs) | `Scalar` 类型的全部实现：包装、NaN 规约、`Eq/Ord/Hash`、运算符重载、`powi`。 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | 定义 `Numeric` / `NumericLength` trait（`Scalar` 实现的就是它）。 |
| [src/macros.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs) | `assign_impl!` 宏，用来从普通运算符批量派生赋值运算符。 |

> 提醒：`Scalar` 通过 [lib.rs:27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L27) 的 `pub use self::scalar::Scalar;` 重导出到 crate 根，所以外部直接写 `typst_utils::Scalar`（见 [u1-l1](u1-l1-project-overview-and-build.md)）。

---

## 4. 核心概念与源码讲解

### 4.1 Scalar 包装与 NaN 规约

#### 4.1.1 概念说明

Typst 是一个排版引擎，排版过程里充斥着浮点运算：长度、坐标、缩放比例……这些值经常需要被放进哈希表（比如缓存排版结果）、需要排序（比如按坐标排序元素）。但原生 `f64` 既不能哈希也不能排序，怎么办？

`Scalar` 的答案是：**用一个 newtype 把 `f64` 包起来，并在「进入」这个类型的那一刻，把所有 `NaN` 统一改写成 `0.0`**。

这背后的核心思想是——「让 `NaN` 根本无法存在于 `Scalar` 内部」。既然内部永远不会有 `NaN`，那么 `Eq`、`Ord`、`Hash` 这些原本被 `NaN` 破坏的 trait，就都可以安全地实现了。

为什么选 `0.0` 而不是别的值？因为 `0.0` 是「中性、确定、跨平台一致」的值，规约到 `0.0` 意味着「缺失/非法的浮点被当作零」，对排版这种「容错优于崩溃」的场景是合理的。

#### 4.1.2 核心流程

```
外部任意 f64（可能是 NaN）
        │
        ▼
   Scalar::new(x)   ← 唯一的「入口」
   ┌──────────────────────────┐
   │  if x.is_nan() { 0.0 }   │
   │  else            { x   } │
   └──────────────────────────┘
        │
        ▼
   Scalar(一个保证非 NaN 的 f64)
```

关键设计：**所有公开的构造路径都经过 `new`**——包括 `From<f64>`、所有算术运算、`sqrt`、`powi`、`Sum`。它们在返回前都会调用 `Self::new(...)`，从而保证「任何运算结果产生的 `NaN`（如 `∞ - ∞`）也会被立刻规约」。这就形成了一道闭环：只要数据进了 `Scalar`，就再也变不出 `NaN`。

> 注意：文件里的几个常量 `ZERO / ONE / INFINITY` 用的是私有的元组构造器 `Self(...)` 而非 `Self::new(...)`，但它们都是已知合法（非 `NaN`）的值，所以可以直接构造。

#### 4.1.3 源码精读

类型定义本身极其朴素，只是一个单字段元组结构体：

[src/scalar.rs:9-15](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L9-L15) —— 注释说明了这个类型的全部「承诺」：实现 `Eq/Ord/Hash`，且所有运算跨平台确定。

```rust
/// A 64-bit float that implements `Eq`, `Ord` and `Hash`.
/// Panics if it's `NaN` during any of those operations.
/// All operations implemented for this type are cross-platform deterministic.
#[derive(Default, Copy, Clone)]
pub struct Scalar(f64);
```

构造函数 `new` 是整个类型的「守门人」，用 `const fn` 实现，可以在常量上下文里使用：

[src/scalar.rs:27-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L27-L32) —— 把 `NaN` 规约为 `0.0`。

```rust
pub const fn new(x: f64) -> Self {
    Self(if x.is_nan() { 0.0 } else { x })
}
```

配套的三个常量与取值方法：

[src/scalar.rs:18-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L18-L37) —— `ZERO / ONE / INFINITY` 常量与 `get()`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`NaN` 在构造期被吃掉，变成 `0.0`」。

**操作步骤**（在你为 [u1-l1](u1-l1-project-overview-and-build.md) 建好的那个依赖了 `typst-utils` 的临时项目里继续即可）：

```rust
// 示例代码（非 typst 原有代码）
use typst_utils::Scalar;

fn main() {
    let from_nan = Scalar::new(f64::NAN);
    let zero     = Scalar::new(0.0);

    println!("nan -> {:?}, get = {}", from_nan, from_nan.get());
    println!("equal? {}", from_nan == zero); // 期望 true
}
```

**需要观察的现象**：`from_nan.get()` 打印出 `0`，且 `from_nan == zero` 为 `true`。

**预期结果**：`NaN` 经 `new` 后变成了 `0.0`，所以两者相等。**待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：既然 `new` 会把 `NaN` 规约为 `0.0`，那为什么后面 `PartialEq`、`Ord` 里还要写 `assert!(…is_nan())`、`.expect("float is NaN")` 这些检查？岂不是多此一举？

> **参考答案**：这是「纵深防御」(defense in depth)。正常情况下，由于所有公开构造路径都经过 `new`，`Scalar` 内部确实不可能存进 `NaN`，这些断言不会触发。但它们是安全网——一旦未来有人不小心绕过 `new`（例如直接用私有元组构造器，或通过 `unsafe`），断言能尽早暴露问题，而不是让错误的哈希/排序结果静默扩散。

**练习 2**：`Scalar::new(f64::INFINITY) - Scalar::new(f64::INFINITY)` 的结果是什么？为什么不会得到 `NaN`？

> **参考答案**：`∞ - ∞` 在 IEEE-754 里是 `NaN`，但 [src/scalar.rs:171-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L171-L177) 的 `Sub` 实现里写的是 `Self::new(self.0 - rhs.0)`，结果 `NaN` 又被 `new` 规约成了 `0.0`。所以最终得到 `Scalar(0.0)`，而不是一个携带 `NaN` 的 `Scalar`。

---

### 4.2 Eq / Ord / Hash 实现

#### 4.2.1 概念说明

要让 `Scalar` 能当 `HashSet` 的键、能进 `BTreeMap`，就必须实现 `Eq`、`Ord`、`Hash`。但前面说过，`f64` 本身是不实现这三者的。

`Scalar` 的策略分两层：

1. **比较层**：因为内部保证没有 `NaN`，所以可以直接用 `f64` 的 `==` 和 `partial_cmp`——但为了双保险，比较前先 `assert`/`expect` 一下「确实不是 `NaN`」。
2. **哈希层**：`f64` 没法直接 `.hash()`，但它的位模式（`to_bits()`，一个 `u64`）是可以哈希的。于是 `Hash` 实现就哈希 `self.0.to_bits()`。

> 一个关键概念：`to_bits()` 把浮点的 64 位内存表示原样当作 `u64` 取出。相同位模式 ⇒ 相同哈希 ⇒ 同一个桶。这是浮点做哈希的标准手法。

#### 4.2.2 核心流程

| trait | 做法 | 遇到 `NaN` 的行为 |
| --- | --- | --- |
| `PartialEq` | `assert!(!is_nan())` 后用 `==` 比较 | **panic** |
| `Eq` | 空实现 `impl Eq for Scalar {}`（标记 trait，承诺「相等是自反/传递/对称的」） | —— |
| `Ord` | `partial_cmp(...).expect("float is NaN")` | **panic** |
| `Hash` | `debug_assert!(!is_nan())` 后哈希 `to_bits()` | debug 构建下 panic，release 下跳过断言 |

注意三者的「严重程度」不同：`PartialEq`/`Ord` 用的是会 panic 的 `assert!`/`expect!`（release 也生效），而 `Hash` 用的是 `debug_assert!`（release 下被编译掉）。但如 4.1 所述，正常路径根本走不到这些检查。

#### 4.2.3 源码精读

[src/scalar.rs:93-100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L93-L100) —— `Eq` 是空的标记实现，`PartialEq` 先断言非 `NaN` 再比较。

```rust
impl Eq for Scalar {}

impl PartialEq for Scalar {
    fn eq(&self, other: &Self) -> bool {
        assert!(!self.0.is_nan() && !other.0.is_nan(), "float is NaN");
        self.0 == other.0
    }
}
```

[src/scalar.rs:108-118](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L108-L118) —— `Ord` 借助 `partial_cmp`，并在 `None`（即遇到 `NaN`）时 panic；`PartialOrd` 直接转调 `Ord::cmp`，让两者保持一致。

[src/scalar.rs:120-125](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L120-L125) —— `Hash` 哈希位模式。

```rust
impl Hash for Scalar {
    fn hash<H: Hasher>(&self, state: &mut H) {
        debug_assert!(!self.0.is_nan(), "float is NaN");
        self.0.to_bits().hash(state);
    }
}
```

此外还有一个跨类型比较的实现：[src/scalar.rs:102-106](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L102-L106) 的 `PartialEq<f64>`，让你能写 `scalar == 3.0_f64`。

#### 4.2.4 代码实践

**实践目标**：本讲规格里要求的核心实践——用 `HashSet<Scalar>` 验证 `NaN` 被规约后与 `0.0` 视为同一个键。

**操作步骤**：

```rust
// 示例代码（非 typst 原有代码）
use std::collections::HashSet;
use typst_utils::Scalar;

fn main() {
    let mut set: HashSet<Scalar> = HashSet::new();
    set.insert(Scalar::new(f64::NAN)); // 规约为 0.0
    set.insert(Scalar::new(0.0));      // 与上面的 0.0 落到同一个桶
    set.insert(Scalar::new(1.5));
    set.insert(Scalar::new(2.0));

    println!("set size = {}", set.len());              // 期望 3（NaN 与 0.0 合并）
    println!("contains 0.0? {}", set.contains(&Scalar::new(0.0))); // 期望 true
}
```

**需要观察的现象**：插入的 4 个元素最终只剩 3 个，因为 `NaN` 与 `0.0` 哈希相同、比较也相等，被去重为一个。

**预期结果**：`set size = 3`，`contains 0.0? = true`。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`Scalar::new(0.0)` 和 `Scalar::new(-0.0)` 用 `==` 比较结果是什么？如果把它们各自放进一个 `HashSet`，再互相 `contains` 查找，结果又如何？这说明了什么？

> **参考答案**：`0.0 == -0.0` 在 IEEE-754 里为 `true`，所以 `Scalar(0.0) == Scalar(-0.0)` 通过 [src/scalar.rs:95-100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L95-L100) 判断为相等。但 `to_bits(0.0) != to_bits(-0.0)`（符号位不同），所以两者**哈希不同**，会被放进不同的桶。于是 `contains` 时因为哈希不同根本不会触发相等比较，互相查不到——这在技术上**违反了「相等 ⇒ 哈希相等」的契约**，是「用 `to_bits` 做哈希」的一个已知小瑕疵。在 Typst 的实际使用中，`±0.0` 几乎不会作为缓存键出现，所以可以接受。**可本地验证。**

**练习 2**：为什么 `Hash` 里用的是 `debug_assert!`，而 `PartialEq` 里用的是 `assert!`？

> **参考答案**：`assert!` 在 release 构建里依然生效，会在运行时检查并 panic；`debug_assert!` 只在 debug 构建里生效，release 下被完全移除。`PartialEq` 用 `assert!` 意味着「即便发布版也绝不容忍 `NaN` 相等比较」；而 `Hash` 路径可能非常高频，用 `debug_assert!` 可以在 release 下省掉这次 `is_nan()` 调用的开销。两处都依赖 4.1 的不变量（内部无 `NaN`）兜底。

---

### 4.3 运算符重载与 Numeric trait

#### 4.3.1 概念说明

光能哈希还不够，`Scalar` 还得能像普通数字一样做四则运算。在 Rust 里，这就是为它实现 `Add`、`Sub`、`Mul`、`Div`、`Rem`、`Neg` 这些运算符 trait。

`Scalar` 的运算符重载有两个值得学习的设计点：

1. **混合运算**：不但实现了 `Scalar + Scalar`，还实现了 `Scalar + f64` 和 `f64 + Scalar`（减/乘/除/余同理）。这让 `Scalar` 和裸 `f64` 能自然混用，写起来不啰嗦。
2. **每个运算都用 `Self::new(...)` 包装结果**：再次保证运算产生的任何 `NaN` 都被立刻规约，闭环不变。

而在更高一层，`Numeric` trait 把「一个数值类型应具备的最小能力」抽象出来：

> 一个类型只要满足 `Numeric`，就承诺它有 `zero()`、能判断 `is_finite()`，并且支持 `Neg/Add/Sub/Mul<f64>/Div<f64>` 这一整套运算。

这样 Typst 里其它泛型代码就可以写 `fn f<T: Numeric>(...)`，对任何满足这套约束的数值类型一视同仁（比如长度 `Abs`、相对长度 `Em`，以及本讲的 `Scalar`）。

#### 4.3.2 核心流程

```
运算符 trait 体系
─────────────────────────────────────────
Neg        →  -a          Add<Self>  → a + b
Add<f64>   →  a + 1.5     Add<Scalar> for f64 → 1.5 + a
（Sub / Mul / Div / Rem 同理，各 3 套实现）
Sum        →  iterator.sum()
─────────────────────────────────────────
assign_impl! 宏（见 macros.rs）
─────────────────────────────────────────
从普通运算符派生赋值运算符：+=  -=  *=  /=  %=
（展开为  *self = *self op other ）
─────────────────────────────────────────
Numeric trait（见 lib.rs）用 supertrait 把以上能力收口
```

注意 `Numeric` 的 supertrait 里，乘除要求的是 `Mul<f64>` / `Div<f64>` 而非 `Mul<Self>`——这是有意为之：对很多「带单位的长度」类型来说，「乘以一个无单位的比例因子」才是最常见的操作。

#### 4.3.3 源码精读

先看一组代表性的运算符实现。加法有三套：[src/scalar.rs:147-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L147-L169)（`Scalar+Scalar`、`Scalar+f64`、`f64+Scalar`）：

```rust
impl Add<Self> for Scalar {
    type Output = Self;
    fn add(self, rhs: Self) -> Self::Output { Self::new(self.0 + rhs.0) }
}

impl Add<Scalar> for f64 {
    type Output = Scalar;
    fn add(self, rhs: Scalar) -> Self::Output { Scalar::new(self + rhs.0) }
}
```

每个实现体都调用 `Self::new` / `Scalar::new`，是 4.1 所述闭环的直接体现。`Sub/Mul/Div/Rem` 在 [src/scalar.rs:171-265](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L171-L265) 完全对称，`Sum` 在 [src/scalar.rs:267-277](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L267-L277)。

再看赋值运算符。文件末尾连续 10 行宏调用，一口气派生了 `+= -= *= /= %=`（每种各两个，对应 `Scalar` 与 `f64` 两个右操作数类型）：

[src/scalar.rs:279-288](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L279-L288)

```rust
assign_impl!(Scalar += Scalar);
assign_impl!(Scalar += f64);
// … -= *= /= %= 各两组 …
```

这些宏的展开体在 [src/macros.rs:25-66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L25-L66)，核心就一句 `*self = *self + other;`（详见 [u1-l3 声明式宏](u1-l3-declarative-macros.md)）。它能成立的前提是 `Scalar: Copy`，这恰好满足（[src/scalar.rs:14](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L14) 派生了 `Copy`）。

最后看 `Numeric` trait 的定义：[src/lib.rs:355-377](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L355-L377)。

```rust
pub trait Numeric:
    Sized + Debug + Copy + PartialEq
    + Neg<Output = Self> + Add<Output = Self> + Sub<Output = Self>
    + Mul<f64, Output = Self> + Div<f64, Output = Self>
{
    fn zero() -> Self;
    fn is_zero(self) -> bool { self == Self::zero() }   // 默认实现
    fn is_finite(self) -> bool;
}
```

`Scalar` 对它的实现在 [src/scalar.rs:71-79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L71-L79)，只补了 `zero()`（返回 `Self(0.0)`）和 `is_finite()`（转调 `f64::is_finite`），其余能力都由上面的运算符实现和 supertrait 自动满足。旁边还有一个空的标记 trait `NumericLength`（[src/lib.rs:379-380](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L379-L380)），用来标注「能当长度用的数值类型」。

#### 4.3.4 代码实践

**实践目标**：体验 `Scalar` 的混合运算、`+=` 赋值，以及作为 `Numeric` 的 `sum()`。

**操作步骤**：

```rust
// 示例代码（非 typst 原有代码）
use typst_utils::{Numeric, Scalar}; // 注意：用 += 前要保证 trait 在作用域里

fn main() {
    let mut a = Scalar::new(1.0);
    a += 2.5_f64;                 // Scalar += f64
    a += Scalar::new(0.5);        // Scalar += Scalar
    println!("a = {a:?}");        // 期望 4.0

    let mixed = Scalar::new(3.0) * 2.0_f64; // Scalar * f64
    println!("mixed = {mixed:?}");          // 期望 6.0

    // Numeric 带来的 sum（需要对迭代器用 .sum::<Scalar>()）
    let total: Scalar = [Scalar::new(1.0), Scalar::new(2.0), Scalar::new(3.0)]
        .into_iter().sum();
    println!("total = {total:?}");          // 期望 6.0
}
```

**需要观察的现象**：`a` 经过两次 `+=` 后是 `4.0`；`mixed` 是 `6.0`；`total` 是 `6.0`。

**预期结果**：三行打印依次为 `4.0`、`6.0`、`6.0`。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：`assign_impl!` 展开后是 `*self = *self + other`。为什么这个写法要求 `Scalar` 必须是 `Copy` 的？如果 `Scalar` 不是 `Copy` 会怎样？

> **参考答案**：等号左边 `*self` 出现了两次——一次作为加法左操作数被「读取」，一次作为赋值目标被「写入」。对于非 `Copy` 类型，`*self` 作为右值会发生「move 出借用内容」，编译器会报「cannot move out of `*self`」的错误。`Copy` 类型则是按位复制，读取不会导致 move，因此合法。这正是 `assign_impl!` 天然只适合像 `Scalar` 这样轻量、`Copy` 的数值包装类型。

**练习 2**：`Numeric` 的 supertrait 里乘法是 `Mul<f64, Output = Self>` 而不是 `Mul<Self, Output = Self>`。结合「带单位的长度」类型（如 `Abs` 表示绝对长度），猜猜为什么这样设计？

> **参考答案**：长度乘以无单位的比例因子是有意义的（`5pt * 2.0 = 10pt`），而「长度乘长度得到长度」在量纲上不成立（`5pt * 5pt` 不该还是 `pt`）。所以 `Numeric` 把「乘/除以 `f64`」作为通用约束，更贴合数值类型在排版里的真实用法。`Scalar` 虽然没有单位、连 `Mul<Self>` 也实现了，但它只是「恰好更宽」，依旧满足 `Numeric` 的最低约束。

---

### 4.4 powi 整数幂

#### 4.4.1 概念说明

`powi` 计算「一个数的整数次幂」，比如 \( 2^{10} = 1024 \)。听起来简单，但 `Scalar` 选择**手写实现**，而不是直接调标准库，原因写在类型注释里：**「All operations implemented for this type are cross-platform deterministic（本类型所有运算都跨平台确定）」**。

跨平台确定性对 Typst 至关重要：同一份 Typst 文档，无论在 x86、ARM、Windows 还是 Linux 上编译，都应当得到**逐位相同**的输出。基本的 `+ - * /` 由 IEEE-754 保证一致，但「整数幂」这类操作如果依赖平台/编译器内置实现（不同的数学库、不同的内置指令），结果可能存在末位差异。为了保证一致，`Scalar` 把整数幂的算法「固化」在自己代码里——这段代码移植自 LLVM 的 `compiler-rt` 里的 `powidf2.c`（见源码注释里的链接）。

#### 4.4.2 核心流程

算法是经典的**平方求幂**（exponentiation by squaring，又叫快速幂）。把指数 \( b \) 写成二进制：

\[
b = b_0 + b_1\cdot 2 + b_2\cdot 2^2 + \cdots
\]

那么：

\[
a^b = a^{b_0}\cdot a^{b_1\cdot 2}\cdot a^{b_2\cdot 2^2}\cdots = \prod_{b_i=1} a^{2^i}
\]

其中每个 \( a^{2^i} \) 都可以由前一项**自乘**得到（\( a^{2^{i+1}} = a^{2^i}\cdot a^{2^i} \)）。于是只要一边扫描指数的二进制位，一边不断自乘底数，遇到该位为 1 就把当前的累乘进结果即可：

```
r = 1, a = 底数, b = |指数|
while b != 0:
    if b 的最低位为 1:  r *= a      # 该位贡献 a 的当前幂
    b = b >> 1                       # 处理下一位（源码用 b /= 2）
    if b != 0:  a *= a               # 底数自乘，准备下一位（a^{2^i} -> a^{2^{i+1}}）
若指数原本为负:  r = 1 / r
```

负指数的情况，先按正指数算出 \( a^{|b|} \)，最后取倒数 \( a^{-b} = 1/a^{b} \)。

#### 4.4.3 源码精读

[src/scalar.rs:45-68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L45-L68) —— `powi` 的完整实现，注释里写明了移植来源。

```rust
pub fn powi(self, mut b: i32) -> Self {
    let mut a = self.get();
    let recip = b < 0;          // 负指数最后要取倒数
    let mut r = 1.0;
    loop {
        if (b & 1) != 0 { r *= a; }   // 当前位为 1，累乘
        b /= 2;                        // 移到下一位
        if b == 0 { break; }
        a *= a;                        // 底数自乘（平方）
    }
    if recip { r = 1.0 / r; }
    Self::new(r)
}
```

> 注意末尾 `Self::new(r)`：即便 `r` 因为取倒数（如 `1.0 / 0.0 = ∞`）或其它原因出现异常值，`new` 仍会把 `NaN` 规约掉，但 `∞` 会保留——这符合浮点的正常语义。

**手动追踪 `Scalar::new(2.0).powi(10)`**：

| 轮次 | `b` | `b & 1` | `r`（累乘后） | `a`（自乘后） |
| --- | --- | --- | --- | --- |
| 1 | 10 | 0 | 1 | 4 |
| 2 | 5  | 1 | 4 | 16 |
| 3 | 2  | 0 | 4 | 256 |
| 4 | 1  | 1 | 1024 | ——（`b` 变 0 后 break） |

最终 `r = 1024.0`，即 \( 2^{10} = 1024 \)。✓

#### 4.4.4 代码实践

**实践目标**：本讲规格里要求的第二个验证——用 `powi` 算 \( 2^{10} \)，并顺带验证负指数。

**操作步骤**：

```rust
// 示例代码（非 typst 原有代码）
use typst_utils::Scalar;

fn main() {
    let two = Scalar::new(2.0);
    println!("2^10  = {:?}", two.powi(10));   // 期望 1024
    println!("2^-2  = {:?}", two.powi(-2));   // 期望 0.25
    println!("2^0   = {:?}", two.powi(0));    // 期望 1
}
```

**需要观察的现象**：三次幂运算分别得到 `1024`、`0.25`、`1`，与上表的追踪一致。

**预期结果**：`2^10 = 1024`、`2^-2 = 0.25`、`2^0 = 1`。**待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：为什么 `powi` 的循环里，`a *= a`（底数自乘）必须放在「`if b == 0 { break; }`」之后？如果放到 `r *= a` 之后、`b /= 2` 之前会怎样？

> **参考答案**：放在 break 检查之后，可以避免「最后一次循环」里多余的一次自乘——当 `b` 已经归零、即将退出时，再算 `a *= a` 是浪费（结果不会再用到）。若提前自乘，算法结果仍然正确（多算了一次没人用的 `a`），但做了一次无用功。当前的顺序是「正确且最省」的写法。

**练习 2**：`Scalar::new(0.0).powi(-1)` 会得到什么？走一遍源码逻辑。

> **参考答案**：`b = -1`，`recip = true`，先把 `b` 当正数处理：最低位为 1，`r *= 0.0` 得 `r = 0.0`，然后 `b /= 2 = 0` break。因为 `recip` 为真，执行 `r = 1.0 / 0.0 = +∞`。所以结果是 `Scalar(+∞)`。注意 `∞` 不是 `NaN`，`new` 不会把它规约掉。

---

## 5. 综合实践

把本讲的四个模块串成一个完整的小任务。

**任务背景**：假设你在写一个小工具，接收一堆「可能含有 `NaN` 和 `±∞`」的原始 `f64`，需要：(1) 安全地去重并存入集合；(2) 求总和；(3) 对某个值做整数幂。要求全程跨平台确定、且 `NaN` 不会让程序崩溃。

**操作步骤**：

```rust
// 示例代码（非 typst 原有代码）
use std::collections::HashSet;
use typst_utils::Scalar;

fn analyze(raw: &[f64]) {
    // (1) 转成 Scalar 后去重 —— NaN 会被规约为 0.0，不会 panic
    let mut set: HashSet<Scalar> = HashSet::new();
    for &x in raw {
        set.insert(Scalar::new(x));
    }
    println!("去重后元素数 = {}", set.len());

    // (2) 求和（Numeric 的 sum）
    let total: Scalar = set.iter().copied().sum();
    println!("总和 = {total:?}");

    // (3) 把总和自乘 3 次方
    println!("总和^3 = {:?}", total.powi(3));
}

fn main() {
    analyze(&[1.0, 1.0, f64::NAN, 0.0, 2.5, f64::INFINITY]);
}
```

**需要观察的现象与思考**：

1. `NaN` 与 `0.0` 合并，所以 `1.0, 1.0, NaN, 0.0, 2.5, ∞` 去重后应为 `{0.0, 1.0, 2.5, ∞}`，共 4 个元素——验证了 4.1 + 4.2 的「NaN 规约 + 可哈希」。
2. 总和 `0 + 1 + 2.5 + ∞ = ∞`，`∞` 的三次方仍是 `∞`——验证了 4.3 的运算符/`Sum` 与 4.4 的 `powi` 在边界值下的行为。
3. 全程没有 `panic`，即便输入里混入了 `NaN` 和 `∞`。

**预期结果**：`去重后元素数 = 4`，`总和 = inf`，`总和^3 = inf`。**待本地验证。**

> 进阶思考：如果把上面 `analyze` 的输入换成含 `-0.0` 和 `0.0` 的数组，去重结果会如 4.2.5 练习 1 所述出现「两者都被保留」的小瑕疵。试着构造这个输入并观察，加深对「`to_bits` 哈希」边界的理解。

## 6. 本讲小结

- `Scalar` 是 `f64` 的 newtype 包装，存在的意义是让浮点变得**可哈希、可排序**，从而能当 `HashSet`/`HashMap`/`BTreeMap` 的键。
- 核心策略是**构造期 NaN 规约**：`Scalar::new` 在入口处把所有 `NaN` 改写成 `0.0`，且所有公开构造路径（含 `From`、各运算符、`Sum`、`sqrt`、`powi`）都经过 `new`，形成「内部永无 `NaN`」的闭环。
- `Eq/PartialEq/Ord/Hash` 的实现分别用 `assert!`/`expect`/`partial_cmp`/`to_bits`，并带有「遇到 `NaN` 就 panic」的纵深防御断言。
- 运算符重载覆盖 `Scalar` 与 `Scalar`、`Scalar` 与 `f64` 双向混用，每个结果都用 `new` 再包一次；`+= -= *= /= %=` 通过 `assign_impl!` 宏批量派生（依赖 `Copy`）。
- `Numeric` trait 用 supertrait 把「数值类型的最小能力」收口，`Scalar` 是它的一个标准实现；`powi` 则是移植自 LLVM 的快速幂算法，目的是保证**跨平台逐位确定**。

## 7. 下一步学习建议

- 顺着「确定性数值」这条线，下一篇建议学习 [u2-l2 高精度数值舍入 round.rs](u2-l2-rounding-with-precision.md)：它会复用本讲的「跨平台确定」思想（用 `libm` 而非平台浮点库），并展示 `round_with_precision` 如何处理正/负精度与溢出。
- 想看 `Scalar` 的哈希思想如何被进一步泛化，可以提前跳读 [u2-l6 哈希体系](u2-l6-hashing.md)（`LazyHash`、`hash128`），理解 Typst 如何用稳定哈希保证跨架构一致。
- 若你对「为什么浮点运算需要这么小心」感兴趣，可补充阅读 IEEE-754 标准中关于 `NaN`、有符号零、舍入模式的章节，本讲的许多设计抉择都源于此。
- 在 Typst 仓库内搜索 `Scalar` 的实际使用点（如 `typst-layout`、`typst-realize` 等），观察它如何作为「确定性浮点」支撑排版缓存键——这是检验你是否真正理解本讲的最佳方式。
