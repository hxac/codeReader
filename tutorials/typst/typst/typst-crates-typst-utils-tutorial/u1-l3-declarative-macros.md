# 声明式辅助宏：singleton / sub_impl / assign_impl / display

## 1. 本讲目标

typst-utils 把一些「写起来很啰嗦、但又反复出现」的样板代码抽象成了几个 `macro_rules!` 宏。学完本讲，你应当能够：

- 说清楚 `singleton!` 是如何用 `LazyLock` 产生「惰性初始化、按调用点全局唯一」的 `&'static` 引用的，以及它为什么只返回**共享引用**。
- 理解 `sub_impl!` / `assign_impl!` 这一类「基于已有运算符派生复合运算符」的宏模式，能说出它们的代数前提和泛型约束（`Copy`）。
- 学会 `display!`（把格式化字符串包成 `impl Display`）与 `display_possible_values!`（为 clap 的 `ValueEnum` 批量实现 `Display`）的区别与用法。
- 掌握阅读 `macro_rules!` 声明式宏的基本能力：片段分类符（`ty`/`expr`/`ident`/`tt`）、重复（`$()`）、`#[macro_export]` 与 `$crate`。

本讲是入门单元的第三篇，承接 u1-l2 讲过的「扩展 trait」思路——上一讲用 trait 给外部类型加方法，本讲用宏给外部类型**批量生成 impl**，二者都是「消除样板」的常见手段。

## 2. 前置知识

### 2.1 什么是声明式宏 `macro_rules!`

Rust 的宏在**编译期**展开，比函数更早运行。`macro_rules!` 是「声明式宏」，它本质是一组**模式匹配规则**：你写好「长这样的输入 → 展开成这样的代码」，编译器在调用处做文本级的模式匹配和替换。

一个最小例子：

```rust
macro_rules! say {
    ($name:expr) => { println!("hello, {}", $name) };
}

say!("world"); // 编译期展开为 println!("hello, {}", "world");
```

阅读宏时要抓住三个要素：

| 要素 | 写法 | 含义 |
| --- | --- | --- |
| 片段分类符 | `$x:ty` `$x:expr` `$x:ident` `$x:tt` | 捕获「一个类型 / 一个表达式 / 一个标识符 / 一棵 token 树」 |
| 重复 | `$($x:expr),*` | 捕获「零个或多个，逗号分隔」 |
| 转录 | `=> { ... }` | 在花括号里用 `$x` 把捕获的内容拼回去 |

本讲用到的分类符主要是：

- `:ty` —— 一个完整类型，如 `Vec<String>`、`Scalar`、`Mutex<Vec<String>>`。
- `:expr` —— 一个表达式，如 `Vec::new()`、`self + -other`。
- `:ident` —— 一个标识符（类型名也算），如 `Scalar`、`Abs`、`Em`。

### 2.2 为什么需要「派生运算符」的宏

Rust 的算术运算符是 trait：`+` 对应 `Add`，`-` 对应 `Sub`，`+=` 对应 `AddAssign`，等等。这些 trait **彼此独立**，编译器不会因为你实现了 `Add` 就自动送你一个 `AddAssign`。于是对一个数值包装类型（比如 `struct Meters(f64)`），你往往要手写一大片高度相似的 impl：

```rust
// 手写一遍 += ：本质上就是 self = self + other
impl AddAssign for Meters {
    fn add_assign(&mut self, other: Meters) { *self = *self + other; }
}
```

这种「五个性质几乎一样的 impl」正是宏的用武之地。`sub_impl!` / `assign_impl!` 就是为此而生。

### 2.3 为什么需要 `singleton!`

Rust 里要拿到一个「全局唯一的 `&'static` 引用」并不麻烦，但写起来略啰嗦：声明一个 `static`，用 `LazyLock` 包起来，再 `&*` 解引用返回。`singleton!` 把这三步压成一行，并保证**每个调用点只初始化一次、之后永远返回同一个引用**。这在 Typst 里极其常见（例如「全局唯一的空 `Content`」「全局唯一的字体回退列表」），所以值得做成宏。

> 前置知识提醒：`std::sync::LazyLock` 自 Rust 1.80 起进入标准库；typst 工作区当前钉在更高的 `rust-version = "1.92"`，所以本讲的 `singleton!` 在工作区内可直接使用。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/macros.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs) | 定义 `singleton!`、`sub_impl!`、`assign_impl!`、`display!` 四个宏 |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | 用 `#[macro_use] mod macros;` 把宏引入 crate 根；定义 `display()` 函数（被 `display!` 转发）和 `display_possible_values!` 宏 |

辅助参考（真实使用场景，便于体会宏的用途，**不在 typst-utils 目录内**）：

| 文件 | 看什么 |
| --- | --- |
| `crates/typst-utils/src/version.rs` | `version()` 用 `singleton!` 全局只解析一次版本号 |
| `crates/typst-library/src/layout/abs.rs` | `Abs` 类型：手写 `Neg`+`Add`，再用 `sub_impl!`/`assign_impl!` 派生 |
| `crates/typst-cli/src/args.rs` | `OutputFormat` 等 clap 枚举用 `display_possible_values!` 实现 `Display` |

## 4. 核心概念与源码讲解

### 4.1 `singleton!`：惰性全局唯一 `'static` 引用

#### 4.1.1 概念说明

`singleton!` 解决的问题是：**「我要一个值，它全局只有一份，第一次用到时才构造，之后所有人共享同一份。」** 这就是经典的「惰性单例（lazy singleton）」。

关键词解释：

- **惰性（lazy）**：值在**第一次被访问**时才真正构造，程序启动时不付这个代价。
- **全局唯一（globally unique）**：精确说是「**每个宏调用点**对应唯一一份」。同一段源码位置反复执行，拿到的是同一个值；不同源码位置各有一份。
- **`'static` 引用**：宏返回的是一个指向静态存储的引用 `&'static T`，因此引用本身可以随便复制、存进任意结构体，永远不会悬空。

#### 4.1.2 核心流程

宏展开后的逻辑等价于：

```text
singleton!(T, value)
   │
   ▼ 展开为
{
    static VALUE: LazyLock<T> = LazyLock::new(|| value);  // ① 声明静态变量，绑定闭包
    &*VALUE                                                // ② 解引用并借出 &T
}
```

- **第 ① 步**：`static VALUE` 是一个**函数体内的静态变量**。Rust 允许在函数/块内声明 `static`，它的生命周期是整个程序运行期，并且「定义在哪个调用点，就固定在那个调用点」。
- **`LazyLock::new(|| value)`**：注意 `value` 被包进闭包 `|| ...`，所以它**不会在声明时立刻求值**，而是推迟到第一次解引用时。
- **第 ② 步**：`&*VALUE` 先用 `*` 触发 `LazyLock` 的 `Deref`（必要时运行闭包完成初始化），再用 `&` 借出一个共享引用。
- **线程安全**：`LazyLock` 内部基于标准库的 once 机制，保证初始化闭包**恰好执行一次**，即便多线程同时首次访问也安全。

> 关键认识：宏返回的是**共享引用 `&T`，不是 `&mut T`**。所以你不能直接 `singleton!(Vec<i32>, vec![]).push(1)`——共享引用不允许修改。要做一个「可写的全局缓存」，必须把内部类型换成具备内部可可变性的容器，例如 `Mutex<Vec<i32>>`（见 4.1.4 的实践）。

#### 4.1.3 源码精读

宏定义只有寥寥几行（[src/macros.rs:1-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L1-L8)）：

```rust
/// Create a lazy initialized, globally unique `'static` reference to a value.
#[macro_export]
macro_rules! singleton {
    ($ty:ty, $value:expr) => {{
        static VALUE: ::std::sync::LazyLock<$ty> = ::std::sync::LazyLock::new(|| $value);
        &*VALUE
    }};
}
```

逐行解读：

- `#[macro_export]`：把这个宏放到 **crate 根**，外部 crate 可以用 `typst_utils::singleton!(...)` 调用。
- `$ty:ty`：捕获目标类型；`$value:expr`：捕获初始化表达式。
- 双花括号 `{{ ... }}`：把整块展开内容包成一个**块表达式**，其求值结果（`&*VALUE`，一个 `&'static $ty`）就是整个宏调用的值。
- `::std::sync::LazyLock`：用绝对路径 `::std::...` 而非 `std::...`，避免在宏被展开到别处时被局部 `use` 干扰——这是写宏时的良好习惯。

来看 typst-utils 内部如何用它。`version()` 函数只应在第一次调用时解析一次版本字符串，之后直接返回已构造好的 `TypstVersion`（[src/version.rs:18-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/version.rs#L18-L37)）：

```rust
pub fn version() -> TypstVersion {
    *crate::singleton!(TypstVersion, {
        let raw = env!("TYPST_VERSION");
        // ... 解析 semver，可能 panic ...
    })
}
```

注意这里的 `*crate::singleton!(...)`：`singleton!` 返回 `&'static TypstVersion`，前置 `*` 复制出 `TypstVersion`（它实现了 `Copy`）。由于 `singleton!` 永远写在 `version()` 函数体的同一个调用点，所有调用 `version()` 的代码共享同一份 `LazyLock`，闭包里的 `semver::Version::parse` **整个程序只跑一次**。

#### 4.1.4 代码实践

> **实践目标**：亲手验证 `singleton!` 的两个性质——(a) 同一调用点返回同一个引用；(b) 要做可写缓存必须借助内部可变性（`Mutex`）。

**操作步骤**：新建一个临时 crate，加入 `typst-utils` 依赖（需要联网拉取或在工作区内引用）：

```bash
cargo new singleton-demo && cd singleton-demo
cargo add typst-utils   # 或在工作区内用 path 依赖
```

把 `src/main.rs` 替换为下面的**示例代码**（非项目原有代码）：

```rust
use std::sync::Mutex;
use typst_utils::singleton;

// 用一个函数把 singleton! 固定在「同一调用点」，
// 这样多次调用 fallback_fonts() 共享同一份静态变量。
fn fallback_fonts() -> &'static Vec<&'static str> {
    singleton!(Vec<&'static str>, vec!["DejaVu Sans", "Noto Sans"])
}

// 可写的全局缓存：内部用 Mutex 提供可变性。
fn string_cache() -> &'static Mutex<Vec<String>> {
    singleton!(Mutex<Vec<String>>, Mutex::new(Vec::new()))
}

fn main() {
    // (a) 验证「同一调用点 → 同一引用」
    let a = fallback_fonts();
    let b = fallback_fonts();
    println!("同一引用? {}", std::ptr::eq(a, b)); // 期待 true
    println!("回退字体: {:?}", a);

    // (b) 验证「可写缓存」
    {
        let mut guard = string_cache().lock().unwrap();
        guard.push("hello".into());
        guard.push("world".into());
    }
    println!(
        "缓存内容: {:?}",
        string_cache().lock().unwrap()
    ); // 期待 ["hello", "world"]
}
```

**需要观察的现象**：

1. `std::ptr::eq(a, b)` 为 `true`，说明两次 `fallback_fonts()` 拿到的是同一份 `&'static Vec`。
2. 第二段打印出 `["hello", "world"]`，说明 `Mutex` 里的数据被跨调用点地保留了下来。
3. 如果把 (a) 的 `singleton!(Vec<&str>, ...)` 换成不带 `Mutex`、再尝试 `.push(...)`，编译器会直接拒绝（共享引用不可变）。

**预期结果**：程序正常编译运行，输出上述两行。`singleton!` 的惰性体现在：把 `fallback_fonts` 闭包里的 `vec![...]` 换成一个带 `println!` 副作用的构造，你会发现该打印**只在第一次调用时出现一次**。

> 待本地验证：具体输出取决于你的运行环境，请实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `singleton!` 写在两个**不同**的函数里各一次（类型相同），它们会共享同一份值吗？

> **参考答案**：不会。`singleton!` 的「唯一性」是**按调用点**计算的。两个不同的源码位置会展开成两个不同的 `static VALUE`，各自独立初始化。要跨函数共享，必须像 `fallback_fonts()` 那样把 `singleton!` 收拢到**同一个**函数里，让所有调用者经过同一个调用点。

**练习 2**：为什么 `singleton!` 返回 `&T` 而不是 `&mut T`？这对调用方意味着什么？

> **参考答案**：因为底层 `static VALUE` 是不可变的静态项，`LazyLock` 的 `Deref` 只提供共享访问。调用方拿不到 `&mut T`，想修改内部数据就必须把值本身设计成支持内部可变性（如 `Mutex`、`RwLock`、`AtomicXxx`）。

---

### 4.2 `sub_impl!`：从 `Neg`+`Add` 派生 `Sub`

#### 4.2.1 概念说明

数学上，减法可以由「取负 + 加法」定义：

\[
a - b \;=\; a + (-b)
\]

只要一个类型已经实现了「取负 `Neg`」和「加法 `Add`」，那么它的减法 `Sub` 就是**确定的、机械的**——无需让作者再手写一遍 `a.0 - b.0`。`sub_impl!` 把这个代数恒等式固化成宏，避免重复且易错的样板代码。

#### 4.2.2 核心流程

宏把 `sub_impl!(A - B -> C)` 展开为一个 `Sub` 实现：

```text
sub_impl!(A - B -> C)
   │
   ▼ 展开为
impl Sub<B> for A {
    type Output = C;
    fn sub(self, other: B) -> C {
        self + (-other)   // ← 复用已有的 Add 和 Neg
    }
}
```

展开后的 `self + (-other)` 依赖：

- `A: Add<B, Output = C>`（已实现），用于 `self + ...`；
- `B: Neg`，且其 `Neg::Output` 能被 `A` 的 `Add` 接受，用于 `-other`。

如果这两个前提不满足，宏本身能编译通过（它只是生成代码），但生成的 impl 会在类型检查时报错——这正是「宏只负责生成、类型检查照常进行」的体现。

#### 4.2.3 源码精读

宏定义在 [src/macros.rs:10-22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L10-L22)：

```rust
/// Implement the `Sub` trait based on existing `Neg` and `Add` impls.
#[macro_export]
macro_rules! sub_impl {
    ($a:ident - $b:ident -> $c:ident) => {
        impl ::core::ops::Sub<$b> for $a {
            type Output = $c;

            fn sub(self, other: $b) -> $c {
                self + -other
            }
        }
    };
}
```

要点：

- 模式 `$a:ident - $b:ident -> $c:ident` 用字面量的 `-` 和 `->` 做分隔，让调用写法贴近数学符号 `A - B -> C`，可读性很好。
- `::core::ops::Sub` 用绝对路径，避免命名冲突。
- `self + -other` 等价于 `self + (-other)`，先对 `other` 取负再相加。

真实使用见 typst-library 的 `Abs`（绝对长度）类型。它先手写了 `Neg`（[crates/typst-library/src/layout/abs.rs:161-167](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/abs.rs#L161-L167)）和 `Add`（[crates/typst-library/src/layout/abs.rs:169-175](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/abs.rs#L169-L175)），随后一行派生出 `Sub`（[crates/typst-library/src/layout/abs.rs:177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/abs.rs#L177)）：

```rust
typst_utils::sub_impl!(Abs - Abs -> Abs);
```

`Abs`、`Em`、`Angle`、`Length`、`Ratio`、`Fr`、`Point`、`Size` 等几乎全部数值类型都用同样的一行派生减法，这正是宏「消除重复」价值的最直观体现。

#### 4.2.4 代码实践

> **实践目标**：为一个自定义长度类型实现 `Neg` 与 `Add`，再用 `sub_impl!` 派生 `Sub`，验证 `a - b == a + (-b)`。

**操作步骤**：在上面的 `singleton-demo`（或新 crate）里加入：

```rust
use std::ops::{Add, Neg};
use typst_utils::sub_impl;

#[derive(Copy, Clone, Debug, PartialEq)]
struct Meters(f64);

impl Neg for Meters {
    type Output = Self;
    fn neg(self) -> Self { Meters(-self.0) }
}

impl Add for Meters {
    type Output = Self;
    fn add(self, other: Self) -> Self { Meters(self.0 + other.0) }
}

sub_impl!(Meters - Meters -> Meters); // 用 Add + Neg 派生 Sub

fn main() {
    let a = Meters(10.0);
    let b = Meters(3.0);
    assert_eq!(a - b, Meters(7.0));
    assert_eq!(a - b, a + (-b)); // 与定义一致
    println!("10m - 3m = {:?}", a - b);
}
```

**需要观察的现象**：删掉 `sub_impl!(...)` 这一行后，`a - b` 会编译失败（`Meters` 没有实现 `Sub`）；加回来即恢复。

**预期结果**：打印 `10m - 3m = Meters(7.0)`，两个断言通过。

#### 4.2.5 小练习与答案

**练习 1**：如果只实现了 `Add` 而没实现 `Neg`，调用 `sub_impl!` 会发生什么？

> **参考答案**：宏本身能展开（宏不做类型检查），但展开后的 `self + -other` 里 `-other` 找不到 `Neg` 实现，于是**类型检查阶段**报错 `cannot unary negate ...`。这说明宏只是代码生成器，所有类型约束仍由编译器照常校验。

**练习 2**：宏模式里为什么用 `:ident` 而不是 `:ty` 来捕获 `A`、`B`、`C`？

> **参考答案**：因为展开体里 `A`、`B`、`C` 出现在 `impl Sub<B> for A`、`fn sub(self, other: B) -> C` 等**需要标识符/类型名**的位置；用 `:ident` 能精确匹配像 `Abs`、`Meters` 这样的简单类型名，且让调用语法 `A - B -> C` 保持清爽。对带泛型参数的复杂类型，`:ident` 不够用，所以这些宏面向的是简单包装类型。

---

### 4.3 `assign_impl!`：从普通运算符派生赋值运算符

#### 4.3.1 概念说明

Rust 把 `+=`、`-=`、`*=`、`/=`、 `%=` 设计成独立的赋值运算符 trait（`AddAssign` 等），它们与对应的普通运算符（`Add` 等）**没有继承关系**。于是「`x += y` 等价于 `x = x + y`」这件事得作者自己写。

`assign_impl!` 把这条等价关系固化成 5 条规则，一条规则对应一个赋值运算符，调用语法直接模仿运算符本身：`assign_impl!(T += U)`。

#### 4.3.2 核心流程

宏的 5 个分支结构完全对称，以 `+=` 为例（[src/macros.rs:27-33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L27-L33)）：

```text
assign_impl!(A += B)
   │
   ▼ 展开为
impl AddAssign<B> for A {
    fn add_assign(&mut self, other: B) {
        *self = *self + other;   // ← 要求 A: Copy，且 A: Add<B>
    }
}
```

展开体 `*self = *self + other` 隐含两个约束：

- `A: Copy`：因为 `*self`（左值解引用）被当成了 `+` 的左操作数，又要在赋值后写回 `*self`，必须能够按位复制。
- `A: Add<B, Output = A>`：已有加法实现，且结果可以赋回 `A`。

> 为什么不写成 `self`？因为 `self` 是 `&mut A`，而 `Add::add` 接收 `A`（按值）。所以必须 `*self` 把 `&mut A` 解成 `A`，这要求 `A: Copy`。这是这类宏最重要的隐含前提。

#### 4.3.3 源码精读

5 条规则集中定义在 [src/macros.rs:24-66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L24-L66)：

```rust
#[macro_export]
macro_rules! assign_impl {
    ($a:ident += $b:ident) => {
        impl ::core::ops::AddAssign<$b> for $a {
            fn add_assign(&mut self, other: $b) {
                *self = *self + other;
            }
        }
    };

    ($a:ident -= $b:ident) => {
        impl ::core::ops::SubAssign<$b> for $a {
            fn sub_assign(&mut self, other: $b) {
                *self = *self - other;
            }
        }
    };
    // *= ... /= ... %= ... 同构，分别调用 * / %
}
```

`-=` 分支里用的是 `*self - other`，它会复用 `Sub`——而 `Sub` 往往正是上一节 `sub_impl!` 派生出来的。所以这三类宏构成一条「派生链」：

```text
手写 Add, Neg
   │
   ├── sub_impl!(A - B -> C)        派生 Sub
   │
   └── assign_impl!(A += B 等)      派生 AddAssign/SubAssign/...
```

真实使用仍以 `Abs` 为例（[crates/typst-library/src/layout/abs.rs:211-214](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/abs.rs#L211-L214)）：

```rust
typst_utils::assign_impl!(Abs += Abs);
typst_utils::assign_impl!(Abs -= Abs);
typst_utils::assign_impl!(Abs *= f64);
typst_utils::assign_impl!(Abs /= f64);
```

typst-utils 自己的 `Scalar`（见 u2-l1）也用了一整套（[src/scalar.rs:279-288](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L279-L288)）。注意第二操作数既可以是同类型（`Abs += Abs`），也可以是不同类型（`Abs *= f64`）——这正是宏参数里 `$b` 独立存在的意义。

#### 4.3.4 代码实践

> **实践目标**：在 4.2 已实现的 `Meters` 基础上，用 `assign_impl!` 派生 `+=` 与 `-=`，体会 `Copy` 前提。

**操作步骤**：

```rust
use std::ops::{Add, AddAssign};
use typst_utils::{sub_impl, assign_impl};

#[derive(Copy, Clone, Debug, PartialEq)]   // ← Copy 是 assign_impl! 的隐含前提
struct Meters(f64);

impl Neg for Meters { type Output = Self; fn neg(self) -> Self { Meters(-self.0) } }
impl Add for Meters { type Output = Self; fn add(self, o: Self) -> Self { Meters(self.0 + o.0) } }

sub_impl!(Meters - Meters -> Meters);
assign_impl!(Meters += Meters);             // 派生 AddAssign
assign_impl!(Meters -= Meters);             // 派生 SubAssign

fn main() {
    let mut m = Meters(10.0);
    m += Meters(5.0);
    assert_eq!(m, Meters(15.0));
    m -= Meters(2.0);
    assert_eq!(m, Meters(13.0));
    println!("结果: {:?}", m);

    // 验证 Copy 前提：去掉 #[derive(Copy, Clone)] 后，
    // assign_impl! 展开体里的 *self 会因「无法 move 出 &mut」而编译失败。
}
```

**需要观察的现象**：删掉 `#[derive(Copy, Clone, ...)]` 后重新编译，会得到类似 `cannot move out of a mutable reference` 的错误，定位到宏展开后的 `*self = *self + other;`。这直观印证了「`assign_impl!` 要求 `Copy`」。

**预期结果**：完整运行打印 `结果: Meters(13.0)`，两条断言通过。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `assign_impl!` 生成的代码要求类型是 `Copy`？如果类型不是 `Copy`（例如内部含 `String`），会怎样？

> **参考答案**：展开体 `*self = *self + other` 中，左边的 `*self` 既被「读取」（作为加法左操作数）又被「写入」（作为赋值目标）。非 `Copy` 类型无法在 `&mut` 引用里这样「先读后写」（会触发 move-out 错误）。所以 `assign_impl!` 天然只适合 `Copy` 的轻量数值包装类型——这正是 Typst 的数值类型（`Abs`/`Em`/`Scalar` 等）的共性。

**练习 2**：`assign_impl!(Abs *= f64)` 里，宏参数 `$b` 是 `f64`。展开后 `*self = *self * other` 要求什么前提？

> **参考答案**：要求 `Abs: Mul<f64, Output = Abs>` 已经存在。也就是说，必须先有对应的「普通」运算符 impl，赋值运算符才能派生。这与 `sub_impl!` 必须先有 `Neg`+`Add` 是同一种「依赖链」关系。

---

### 4.4 `display!` 与 `display_possible_values!`：转发格式化

这两个宏都和 `Display` 有关，但**用途完全不同**，初学时最容易混淆，务必区分清楚。

#### 4.4.1 概念说明

- **`display!`**：一个**值构造器**。它接收和 `format!` 一样的参数，返回一个**实现了 `Display` 的匿名值**。当你某个 API 要 `impl Display`、而你只想顺手用格式化字符串拼一段文字时使用。
- **`display_possible_values!`**：一个** impl 生成器**。它为某个**已经派生了 clap `ValueEnum` 的枚举类型**批量实现 `std::fmt::Display`，让命令行参数枚举能直接打印成其名字字符串。

#### 4.4.2 核心流程

**`display!`**（[src/macros.rs:68-75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L68-L75)）：

```text
display!("hello {}", x)
   │
   ▼ 展开为
$crate::display(|f| write!(f, "hello {}", x))
   │
   ▼ display() 把闭包包成 impl Display
返回一个 impl Display 的值
```

它转发的 `display()` 函数定义在 [src/lib.rs:62-78](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L62-L78)：把一个 `Fn(&mut Formatter) -> Result` 闭包塞进一个私有 `Wrapper` 结构体，并为它实现 `Display`。`display!` 宏只是「用 `write!` 现造一个这样的闭包」的快捷方式。

> 设计要点：宏体里写 `$crate::display(...)` 而非 `display(...)`。`$crate` 在宏展开时会自动解析为「定义该宏的 crate」（即 `typst_utils`），保证从外部 crate 调用时也能正确找到 `display` 函数，不会撞上调用方的同名函数。这是写「可被外部使用的宏」时的关键技巧。

**`display_possible_values!`**（[src/lib.rs:478-492](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L478-L492)）：

```text
display_possible_values!(MyEnum)
   │
   ▼ 展开为
impl std::fmt::Display for MyEnum {
    fn fmt(&self, f) -> Result {
        self.to_possible_value().expect("no values are skipped").get_name().fmt(f)
    }
}
```

它假设 `MyEnum` 已实现 clap 的 `ValueEnum`（提供 `to_possible_value()`）。`expect("no values are skipped")` 表明：如果你的枚举里有 `#[value(skip)]` 的变体，`to_possible_value()` 可能返回 `None` 而触发 panic——所以这个宏只适合「所有变体都可对外暴露」的枚举。

#### 4.4.3 源码精读

`display!` 宏（[src/macros.rs:70-75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L70-L75)）：

```rust
#[macro_export]
macro_rules! display {
    ($($arg:tt)*) => {
        $crate::display(|f| write!(f, $($arg)*))
    };
}
```

- `$($arg:tt)*`：用「零个或多个 token 树」捕获**任意**参数，原样转给 `write!`——因此 `display!` 的参数和 `format!`/`write!` 完全一致。
- `$crate::display(...)`：转发给前述函数，返回 `impl Display`。

> 注意 doc 注释里有一个原始拼写 `Accecpts`（[src/macros.rs:68](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs#L68)），阅读源码时不必在意，它不影响功能。

`display_possible_values!` 宏（[src/lib.rs:480-492](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L480-L492)）：

```rust
#[macro_export]
macro_rules! display_possible_values {
    ($ty:ty) => {
        impl std::fmt::Display for $ty {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                self.to_possible_value()
                    .expect("no values are skipped")
                    .get_name()
                    .fmt(f)
            }
        }
    };
}
```

真实使用：typst-cli 的命令行输出格式枚举（[crates/typst-cli/src/args.rs:590-606](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L590-L606)）：

```rust
#[derive(Debug, Copy, Clone, Eq, PartialEq, ValueEnum)]
pub enum OutputFormat { Pdf, Png, Svg, Html, Bundle }

display_possible_values!(OutputFormat);
```

`OutputFormat` 派生了 clap 的 `ValueEnum`，于是 `display_possible_values!` 一行就让 `format!("{}", OutputFormat::Pdf)` 得到 `"pdf"`。`display!` 的真实使用则集中在 typst 的 HTML 测试报告生成器里，例如 `.id(display!("r-{}", report.name))`，用一个格式化字符串现场造一个 `impl Display` 喂给 HTML 构建器。

#### 4.4.4 代码实践

> **实践目标**：用 `display!` 造一个临时 `impl Display` 值并打印；同时跟踪一个 `display_possible_values!` 的真实使用点。

**操作步骤（源码阅读型 + 动手型）**：

1. 动手验证 `display!`：

```rust
use typst_utils::display;

fn main() {
    let name = "typst";
    let version = 0.13;
    // display! 返回一个 impl Display，可直接用于任何需要 Display 的地方
    let label = display!("{} v{}", name, version);
    println!("{}", label); // 期待: typst v0.13
}
```

> 说明：这里直接调用 `typst_utils::display`（函数）。等价地，`typst_utils::display!("{} v{}", name, version)`（宏）会展开成几乎相同的代码——你可以两种写法都试，对比宏版省去了手写 `|f| write!(...)`。

2. 阅读型实践：打开 [crates/typst-cli/src/args.rs:606](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L606)，确认 `display_possible_values!(OutputFormat)` 上方确实有 `#[derive(... ValueEnum)]`。然后在仓库里搜索 `format!("{}", OutputFormat::Pdf)` 或在 CLI 帮助/错误输出代码中找到它被打印的位置，理解「派生 `ValueEnum` → 一行宏 → 自动可打印」的链路。

**需要观察的现象**：`display!` 产物可直接 `println!("{}", ...)`，与用 `format!` 得到的 `String` 在显示效果上一致，但**不会立即分配字符串**（它是个惰性的 `impl Display`，只有在真正格式化时才写入目标 `Formatter`）。

**预期结果**：第 1 步打印 `typst v0.13`；第 2 步能定位到 `ValueEnum` 派生与宏调用紧邻出现的位置。

#### 4.4.5 小练习与答案

**练习 1**：`display!` 与 `format!` 都能拼字符串，区别是什么？什么场景该用 `display!`？

> **参考答案**：`format!` 立刻返回一个**已分配的 `String`**；`display!` 返回的是**惰性的 `impl Display`**，只在被格式化时才写入，不预先分配。当某个 API 形参是 `impl Display`（而非 `String`）、且你只想用格式化语法拼一段文字时，用 `display!` 更轻量。typst 的 HTML 报告构建器大量采用这种「方法链里内联拼字符串」的写法。

**练习 2**：`display_possible_values!(E)` 对枚举 `E` 有什么前提？为什么带 `expect("no values are skipped")`？

> **参考答案**：前提是 `E` 实现了 clap 的 `ValueEnum`（提供 `to_possible_value()`）。`to_possible_value()` 对被 `#[value(skip)]` 跳过的变体可能返回 `None`；宏用 `expect` 明确表态：「本宏假设没有变体被跳过」，一旦违反就 panic，把问题尽早暴露在开发期而不是悄悄给出错误输出。

**练习 3**：为什么 `display!` 宏体用 `$crate::display(...)` 而不是直接 `display(...)`？

> **参考答案**：宏会被外部 crate 引用。`$crate` 是「定义该宏的 crate」的占位符，展开后自动解析为 `typst_utils`，确保总能调到正确的 `display` 函数，避免与调用方本地的同名 `display` 冲突。这是编写可复用宏的卫生性（hygiene）最佳实践。

## 5. 综合实践

把本讲四个宏串起来，做一个「**带单位的预算计算器**」微型库，要求：

1. 定义 `struct Euros(f64)`，派生 `Copy + Clone + Debug + PartialEq`。
2. 手写 `Neg`、`Add`；用 `sub_impl!` 派生 `Sub`；用 `assign_impl!` 派生 `+=`、`-=`、`*=`（乘以 `f64`）。
3. 用 `singleton!` 维护一个**全局可写的「交易备注」缓存**（`Mutex<Vec<String>>`），每次 `+=`/`-=` 时往里 push 一条 `display!` 生成的备注（如 `"+5.00 EUR"`），最后打印整份流水。
4. 写一个 `fn total()`，它内部用 `singleton!` 返回同一份累加器引用，验证多次调用 `total()` 拿到的是同一个累加器（`ptr::eq` 为真）。

参考骨架（**示例代码**，请自行补全并运行）：

```rust
use std::ops::{Add, Neg};
use std::sync::Mutex;
use typst_utils::{singleton, sub_impl, assign_impl, display};

#[derive(Copy, Clone, Debug, PartialEq)]
struct Euros(f64);

impl Neg for Euros { type Output = Self; fn neg(self) -> Self { Euros(-self.0) } }
impl Add for Euros { type Output = Self; fn add(self, o: Self) -> Self { Euros(self.0 + o.0) } }
sub_impl!(Euros - Euros -> Euros);
assign_impl!(Euros += Euros);
assign_impl!(Euros -= Euros);
assign_impl!(Euros *= f64);

// 全局交易备注缓存
fn ledger() -> &'static Mutex<Vec<String>> {
    singleton!(Mutex<Vec<String>>, Mutex::new(Vec::new()))
}

fn main() {
    let mut balance = Euros(100.0);
    balance += Euros(30.0);
    ledger().lock().unwrap().push(format!("{}", display!("+{:.2} EUR", 30.0)));
    balance -= Euros(10.0);
    ledger().lock().unwrap().push(format!("{}", display!("-{:.2} EUR", 10.0)));
    balance *= 1.1; // 涨 10%
    ledger().lock().unwrap().push(format!("{}", display!("*1.1 -> {:.2} EUR", balance.0)));

    println!("余额: {:?}", balance);
    println!("流水: {:?}", ledger().lock().unwrap());
}
```

完成后再回顾：`sub_impl!`/`assign_impl!` 为你省下了多少行高度相似的 impl？`singleton!` 让「全局缓存」的初始化代码收敛到了几行？`display!` 又是怎样让你在 `format!` 调用里顺手产出 `impl Display` 的？

## 6. 本讲小结

- typst-utils 用 `macro_rules!` 把高频样板代码压成了四个宏，定义在 [src/macros.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/macros.rs) 与 [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) 中，通过 `#[macro_use] mod macros;` 引入 crate 根，并用 `#[macro_export]` 暴露给外部。
- `singleton!(T, value)` 用 `LazyLock` 产生**按调用点全局唯一、惰性初始化**的 `&'static T`；它只返回共享引用，可写场景需搭配 `Mutex` 等内部可变性容器。
- `sub_impl!(A - B -> C)` 基于 \(a-b=a+(-b)\) 的恒等式，从已有 `Neg`+`Add` 派生 `Sub`。
- `assign_impl!(A op= B)` 从对应普通运算符派生赋值运算符（`+=`/`-=`/`*=`/`/=`/`%=`），隐含前提是类型 `Copy`。
- `display!` 把 `format!` 风格的参数包成惰性 `impl Display`；`display_possible_values!` 则给 clap `ValueEnum` 枚举一行生成 `Display` 实现——两者都借助 `$crate` 保证外部调用卫生。
- 阅读宏时抓住三件事：片段分类符（`:ty`/`:expr`/`:ident`/`:tt`）、重复语法（`$()*`）、以及 `#[macro_export]` + `$crate` 的可复用宏范式。

## 7. 下一步学习建议

本讲讲完了一类「消除样板的声明式宏」。接下来的进阶单元会进入 typst-utils 的数值与集合体系，届时你会再次遇到本讲的宏：

- **u2-l1 Scalar**：会看到 `assign_impl!(Scalar += Scalar)` 等 10 行派生（[src/scalar.rs:279-288](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/scalar.rs#L279-L288)）在真实数值类型上的完整应用，以及 `Numeric` trait 如何用 supertrait 统一各类数值。
- **u3-l4 版本信息与 DefSite**：会再次看到 `singleton!` 在 `version()` 里的用法，并理解 `DefSite` 为何用 `key` 而非行号来应对宏展开。
- 如果你想更系统地学习宏，建议阅读 Rust Reference 的 [Macros](https://doc.rust-lang.org/reference/macros-by-example.html) 与 [The Little Book of Rust Macros](https://danielkeep.github.io/tlborm/book/)，理解片段分类符的跟随集合（follow set）等更深入的主题。
