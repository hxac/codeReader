# 标准库类型的扩展 trait 与辅助函数

## 1. 本讲目标

上一篇我们建立了 `typst-utils` 的全局认知：它是最底层的工具库、采用「一文件一主题」结构、`build.rs` 注入版本变量、`lib.rs` 用「私有 mod + 选择性 pub use」对外公开 API。本讲我们扎进 `lib.rs` **内联**写在文件主体里的那一批代码——它们没有单独成文件，却构成了整个 crate 最常被调用的「便利层」。

读完本讲你应当能够：

1. 说清「用 trait 给外部（标准库）类型添加方法」这种 Rust 扩展模式的来龙去脉与前提（孤儿规则）。
2. 熟练使用 `SliceExt` 的 `trim_start_matches` / `trim_end_matches` / `group_by_key` / `split_prefix_suffix` 四个切片操作。
3. 读懂 `GroupByKey` 惰性迭代器与 `MaybeReverseIter::rev_if` 这个「用 `Option` 把两种迭代器统一成一个具体类型」的小技巧。
4. 理解 `debug()` / `display()` 如何把一个闭包变成 `impl Debug` / `impl Display`，以及 `Static` 为什么是「按指针相等、廉价克隆」的包装类型。

## 2. 前置知识

本讲假设你已读过 [u1-l1 项目概览](u1-l1-project-overview-and-build.md)，了解 `lib.rs` 的整体导出结构。此外需要以下 Rust 基础（用通俗的话复习）：

- **trait（特征）**：类似其他语言里的接口，定义一组方法签名。实现（`impl Trait for Type`）后，该类型就拥有这些方法。
- **孤儿规则（orphan rule）**：Rust 规定「只能为自己的 crate 或自己的类型实现 trait」。这意味着我们**不能**直接给标准库的 `Option<T>` 再加一个标准库里没有的 trait 方法——除非这个 trait 是我们自己定义的。这正是本讲大量「自定义 trait + 为标准库类型实现」写法的根本原因。
- **`Deref` / `Copy`**：`Deref` 让一个包装类型可以用 `*` 解引用、自动获得对内部方法的调用；`Copy` 表示按位复制即可（无需 `clone`）。
- **胖指针（fat pointer）**：`&str`、`&[T]`、`dyn Trait` 这类引用除了数据地址外还带一份额外信息（长度或 vtable），所以叫「胖」指针。
- **迭代器适配器**：像 `.map()`、`.rev()` 这样把一个迭代器变换成另一个迭代器的方法。

> 小提示：本讲引用的代码全部在 [`src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L1-L516) 一个文件里。建议把它打开对照阅读。

## 3. 本讲源码地图

本讲只涉及一个文件，但其中包含四个相互独立的小主题：

| 文件 | 作用 | 本讲涉及范围 |
| --- | --- | --- |
| [`src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L1-L516) | crate 根模块，内联了本讲的全部扩展 trait 与辅助函数 | 行 42–380 为主体 |

为方便对照，下面给出本讲四个最小模块在 `lib.rs` 中的行号锚点（HEAD 为 `32fd4cc`）：

| 最小模块 | 关键符号 | 行号 |
| --- | --- | --- |
| NonZeroExt / OptionExt / Numeric 概览 | `NonZeroExt`、`OptionExt`、`Numeric` | [lib.rs:80-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L80-L113)、[lib.rs:355-380](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L355-L380) |
| SliceExt 切片扩展 | `SliceExt` | [lib.rs:115-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L115-L191) |
| GroupByKey 与 MaybeReverseIter | `GroupByKey`、`MaybeReverseIter` | [lib.rs:228-276](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L228-L276) |
| debug/display/option_eq/Static/Get | `debug`、`display`、`option_eq`、`Static`、`Get` | [lib.rs:42-78](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L42-L78)、[lib.rs:279-353](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L279-L353) |

> 顺带一提：`lib.rs` 里还有一个 `Rdedup` trait（[lib.rs:193-225](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L193-L225)），它与切片去重关系紧密，但其「保留后值」的去重语义将在 [u2-l5 ListSet 与分组去重](u2-l5-listset-and-dedup.md) 详讲，本讲只做路标。

---

## 4. 核心概念与源码讲解

### 4.1 NonZeroExt 与 OptionExt（含 Numeric 概览）

#### 4.1.1 概念说明

Rust 标准库提供了 `NonZeroUsize`、`NonZeroU32` 等「保证不为 0」的整数类型，以及 `Option<T>`。我们经常需要给它们补一两个标准库没提供的小方法或常量。由于孤儿规则，不能直接往标准库类型上挂标准库没有的方法，于是 typst-utils 的做法是：

1. **自己定义一个 trait**（如 `NonZeroExt`、`OptionExt`）。
2. **为标准库类型实现这个 trait**（`impl NonZeroExt for NonZeroUsize`）。
3. 调用方写 `use typst_utils::NonZeroExt;` 把 trait 引入作用域，即可像调用原生方法一样使用 `NonZeroUsize::ONE`。

这种模式叫 **extension trait（扩展特征）**，是 Rust 里给外部类型加便利方法的标准手法，也是本讲几乎所有代码的共同骨架。

#### 4.1.2 核心流程

扩展 trait 的使用流程是固定的三步：

```
定义 trait（声明想要的常量/方法）
        │
        ▼
为若干标准库类型 impl 该 trait
        │
        ▼
调用方 `use MyExt;`  →  即可用「方法调用语法」访问
```

- `NonZeroExt` 只加一个常量 `ONE`，让 `NonZeroUsize` / `NonZeroU32` 能直接写出「数字 1」而不必每次 `NonZeroUsize::new(1).unwrap()`。
- `OptionExt` 加一个 `map_or_default`：把 `Some(x)` 用函数映射、`None` 则返回默认值——相当于标准库 `map_or(U::default(), f)` 的更清晰写法。

#### 4.1.3 源码精读

`NonZeroExt` 为两种非零整数提供常量 `ONE`，内部仍用 `Self::new(1).unwrap()`，但这个 `unwrap` 永不 panic（1 显然非 0）：

[lib.rs:80-92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L80-L92) —— 定义 `NonZeroExt` 并为 `NonZeroUsize`、`NonZeroU32` 实现 `ONE` 常量。

`OptionExt::map_or_default` 用一个 `match` 把「有值就变换、无值就取默认」说得很直白：

[lib.rs:94-113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L94-L113) —— `OptionExt` 与其 `Option<T>` 实现；`Some(x) => f(x)`，`None => U::default()`。

**Numeric 概览**：稍微往下看，[lib.rs:355-380](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L355-L380) 定义了一个用 supertrait 列表约束的 `Numeric` trait。它要求实现类型同时满足 `Copy + Debug + PartialEq`，以及和 `f64` 的乘除、和自身的加减、取负，并提供 `zero()` / `is_zero()` / `is_finite()`。这是一个「把各种数值类型（长度、角度、比率等）统一抽象」的接口，便于上层写泛型代码。它的具体实现（如 `Scalar`）将在 [u2-l1 Scalar](u2-l1-scalar-deterministic-float.md) 详讲，这里只需知道它和 `NonZeroExt`/`OptionExt` 一样，是「以 trait 抽象一批类型」的同一思路。

#### 4.1.4 代码实践

**实践目标**：体会扩展 trait 必须先 `use` 才能用。

**操作步骤**（在一个依赖了 `typst-utils` 的 Rust 项目里）：

```rust
// 示例代码
use std::num::NonZeroUsize;
use typst_utils::{NonZeroExt, OptionExt};

fn main() {
    // 不写 NonZeroUsize::new(1).unwrap()
    let one: NonZeroUsize = NonZeroUsize::ONE;

    // 把 Option<String> 映射成大写，None 时返回空字符串
    let some = Some("hi".to_string());
    let none: Option<String> = None;
    assert_eq!(some.map_or_default(|s| s.to_uppercase()), "HI");
    assert_eq!(none.map_or_default(|s| s.to_uppercase()), "");
    println!("{one:?}");
}
```

**需要观察的现象**：若把顶部 `use typst_utils::NonZeroExt;` 注释掉，`NonZeroUsize::ONE` 会编译报错（`ONE` 找不到）——这验证了扩展 trait 必须显式引入。

**预期结果**：编译通过，打印 `1`；注释掉 `use` 后编译失败。

#### 4.1.5 小练习与答案

1. **练习**：为什么 typst-utils 不直接把 `ONE` 加到标准库的 `NonZeroUsize` 上？
   **答案**：因为孤儿规则禁止我们为外部类型实现外部 trait；`NonZeroUsize` 和（假设的）提供 `ONE` 的 trait 都不属于 typst-utils，所以只能自建 `NonZeroExt` 这个「内部 trait」再 impl。
2. **练习**：`map_or_default` 对 `None` 分支返回的值从何而来？
   **答案**：返回 `U::default()`，因此目标类型 `U` 必须实现 `Default`（这正是签名里 `U: Default` 的约束）。

---

### 4.2 SliceExt 切片扩展

#### 4.2.1 概念说明

`[T]`（切片）是 Rust 里最常见的数据载体之一。typst-utils 给它加了四个高频操作：从头/尾剔除连续满足条件的元素、按键连续分组、把切片拆成「前缀—中间—后缀」三段。这些操作在 Typst 的排版（layout）代码里几乎无处不在，例如按行列分组、剔除空白边距等。

#### 4.2.2 核心流程

四个方法的语义（注意 `trim` 系列处理的是**元素**而非字符，因为 `T` 是任意类型）：

- `trim_start_matches(f)`：从下标 0 起向后扫描，跳过所有 `f` 为真的元素，返回剩下的切片。
- `trim_end_matches(f)`：从末尾向前扫描，跳过所有 `f` 为真的元素，返回前面的切片。
- `group_by_key(f)`：返回一个惰性迭代器 `GroupByKey`，每次产出「连续相同键」的一段（见 4.3）。
- `split_prefix_suffix(f)`：返回 `(start, end)` 两个下标，把切片切成三段：
  - 前缀 `[0..start]`：全部满足 `f`；
  - 中间 `[start..end]`：夹杂不满足 `f` 的「内核」；
  - 后缀 `[end..len]`：全部满足 `f`，且与前缀不重叠。

`split_prefix_suffix` 的下标计算逻辑（伪代码）：

```
start = 第一个「不满足 f」的元素下标；若全都满足，start = len
end   = 从 start 起，最后一个「不满足 f」的元素下标 + 1；若从 start 起全满足，end = start
```

#### 4.2.3 源码精读

trait 声明，四个方法签名（注意 `group_by_key` 的键只需 `PartialEq`，不必排序）：

[lib.rs:115-149](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L115-L149) —— `SliceExt` trait，声明 `trim_start_matches` / `trim_end_matches` / `group_by_key` / `split_prefix_suffix`。

两个 `trim` 实现就是朴素的 while 循环，分别从前/后推进下标：

[lib.rs:152-173](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L152-L173) —— `trim_start_matches` 与 `trim_end_matches` 实现。

`split_prefix_suffix` 用 `iter().position(...)` 找首个不满足者、`skip(start).rposition(...)` 找最后一个不满足者：

[lib.rs:179-190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L179-L190) —— `split_prefix_suffix` 实现；`start` 用 `position(|v| !f(v)).unwrap_or(self.len())`，`end` 用 `rposition` 的相对下标换算回绝对下标。

> 旁证：这些方法在 Typst 排版代码里被大量使用，例如 `crates/typst-layout/src/math/scripts.rs` 一处就引用了 10 次（来自工作区检索），说明它们是真实高频工具，而非摆设。

#### 4.2.4 代码实践

**实践目标**：用 `split_prefix_suffix` 验证「三段切分」语义，并直观看到下标含义。

**操作步骤**：

```rust
// 示例代码
use typst_utils::SliceExt;

fn main() {
    // true 表示「空白」，false 表示「内容」
    let row = [true, true, false, false, true, true];
    let (start, end) = row.split_prefix_suffix(|&blank| blank);
    println!("prefix = {:?}", &row[..start]);   // [true, true]
    println!("inner   = {:?}", &row[start..end]); // [false, false]
    println!("suffix  = {:?}", &row[end..]);      // [true, true]

    // trim 系列：剔除首尾连续的 true
    let trimmed = row.trim_start_matches(|&b| b);
    println!("trimmed = {:?}", trimmed); // [false, false, true, true]
}
```

**需要观察的现象**：当所有元素都满足 `f` 时，`(start, end)` 会变成 `(len, len)`，前缀吞掉整段、中间和后缀都为空。

**预期结果**：打印出 `prefix=[true,true]`、`inner=[false,false]`、`suffix=[true,true]`、`trimmed=[false,false,true,true]`。

#### 4.2.5 小练习与答案

1. **练习**：把上例的 `row` 改成全 `true`（`[true, true, true]`），`split_prefix_suffix` 返回什么？
   **答案**：`start = 3`（`position` 找不到不满足者，`unwrap_or(self.len())` 给 3），`end = start = 3`；于是前缀 `[0..3]` 是全部，中间和后缀为空。这与文档「全部匹配时 prefix 变成 self、suffix 为空」一致。
2. **练习**：`group_by_key` 要求切片**预先排序**吗？
   **答案**：不要求。它只把**连续相同键**的元素合并成一组；若相同键在切片中不连续，会被拆成多个组。这正是「连续分组」的语义，也提醒调用方：想要按键整体归并时，需先排序。

---

### 4.3 GroupByKey 与 MaybeReverseIter

#### 4.3.1 概念说明

这两个是「迭代器适配」主题：

- `GroupByKey` 是 `group_by_key` 返回的惰性迭代器。**惰性**意味着它不会一次性把所有分组算出来，而是每次 `next` 只消费切片里「连续相同键」的那一段。
- `MaybeReverseIter::rev_if(cond)` 解决一个尴尬问题：我们想「根据条件决定是否 `.rev()`」，但 `.rev()` 会改变迭代器的**具体类型**（多包一层 `Rev<I>`），导致 `if/else` 两个分支类型不一致、无法直接返回。`rev_if` 用一个巧妙办法把它们统一成一个具体类型，从而避免 `Box<dyn Iterator>` 的动态分发开销。

#### 4.3.2 核心流程

`GroupByKey::next` 的推进过程：

```
取出当前剩余切片的第 1 个元素 → 用 f 算出它的 key
        │
        ▼
从第 2 个起向后数，只要 f(元素) == key 就继续（take_while + count）
        │
        ▼
把这段 [0..count] 作为 (key, &slice) 产出；剩余切片前移 count 位
```

`rev_if` 的「Option 统一类型」技巧：

```
若 condition == true ：把 self.rev() 放进 Some，另一边放 None
若 condition == false：把 self       放进 Some，另一边放 None
        │
        ▼
两边都 .into_iter().flatten() 再 .chain()
        │
        ▼
因为必有一边是 None（flatten 后为空），实际只产出未反转 / 已反转的一路
```

这样返回类型是固定的 `Chain<Flatten<...>, Flatten<...>>`，编译期完全确定，无虚函数表。

#### 4.3.3 源码精读

`GroupByKey` 是个仅持有「剩余切片 + 键函数」的小结构体：

[lib.rs:228-231](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L228-L231) —— `GroupByKey` 结构体，保存 `slice: &'a [T]` 与闭包 `f`。

`next` 里 `1 + iter.take_while(...).count()` 这一行是关键：先消费第 1 个算 key，再数连续相等的个数：

[lib.rs:240-247](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L240-L247) —— `GroupByKey::next`；`take_while` 边比较边消费，`split_at(count)` 切出本组，`self.slice = tail` 前移游标。

`MaybeReverseIter` 把迭代器或其反转版本塞进 `Option`，再 flatten + chain：

[lib.rs:260-276](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L260-L276) —— `rev_if` 实现；注意关联类型 `RevIfIter` 是 `Chain<Flatten<IntoIter<Option<I>>>, Flatten<IntoIter<Rev<I>>>>`，正是「双 Option 扁平化」的结果。

#### 4.3.4 代码实践

**实践目标**：用 `rev_if` 直观验证「条件反转」，并理解它返回的是一个普通可 `for` 的迭代器。

**操作步骤**：

```rust
// 示例代码
use typst_utils::MaybeReverseIter;

fn main() {
    let nums = vec![1, 2, 3, 4];

    print!("正序: ");
    for n in nums.clone().into_iter().rev_if(false) {
        print!("{n} ");
    } // 1 2 3 4

    print!("\n反序: ");
    for n in nums.into_iter().rev_if(true) {
        print!("{n} ");
    } // 4 3 2 1
    println!();
}
```

**需要观察的现象**：`rev_if(false)` 与 `rev_if(true)` 都能直接 `for`，无需 `Box`；说明类型已被统一。

**预期结果**：分别输出 `1 2 3 4` 与 `4 3 2 1`。**待本地验证**：若你用的 `typst-utils` 版本 API 有差异，以本地编译结果为准。

#### 4.3.5 小练习与答案

1. **练习**：为什么 `rev_if` 不直接写 `if cond { self.rev() } else { self }`？
   **答案**：`self.rev()` 的类型是 `Rev<I>`，与 `self`（类型 `I`）不同；`if/else` 两个分支必须同类型，直接写无法编译。用 `Option` 包一层后，两分支都变成「`Option<某迭代器>` 的扁平化链」，类型一致。
2. **练习**：对切片 `[("a",1),("a",2),("b",3),("a",4)]` 调用 `group_by_key(|(k,_)| *k)`，会产出几组？
   **答案**：三组：`("a", [1,2])`、`("b", [3])`、`("a", [4])`。因为最后的 `"a"` 与开头的 `"a"` 不连续，被拆成独立的一组。

---

### 4.4 debug / display / option_eq / Static / Get

#### 4.4.1 概念说明

这一组是「散装」便利工具，但各有巧妙：

- `debug(f)` / `display(f)`：把一个「往 `Formatter` 写东西」的闭包，包装成实现了 `Debug` / `Display` 的值。常见用途：临时给某个数据拼一段格式化输出，又不想专门定义一个新结构体。
- `option_eq(left, right)`：把 `Option<L>` 和一个裸值 `R` 比较——`Some(v)` 时比 `v == right`，`None` 时直接判否。省去手写 `left == Some(right)` 之类的样板。
- `Static<T>`：对一个 `&'static T` 的薄包装。它 `Copy`（克隆零成本），`Deref` 到 `T`，但 `PartialEq` / `Hash` **只看指针地址**而非内容。适合包装「全局唯一常量」，用地址当身份证。
- `Get<Index>`：一个泛型「按下标取组件」的 trait，提供 `get_ref` / `get_mut` 抽象，外加 `get` / `set` / `with` 三个默认方法（`with` 是链式 builder）。

#### 4.4.2 核心流程

`debug` / `display` 的统一手法（以 `debug` 为例）：

```
定义局部 struct Wrapper<F>(F)
        │
        ▼
为 Wrapper 实现 Debug：fmt 里调用 self.0(f) —— 即把格式化工作交还给闭包
        │
        ▼
返回 Wrapper(f)，它「是个 impl Debug」
```

`Static` 的相等判断流程：

```
Static(a).eq(&Static(b))
        │
        ▼
std::ptr::eq(self.0, other.0)   ← 只比较两个引用是否指向同一地址
        │
        ▼
内容相同但地址不同 → 判不相等；同一地址 → 判相等
```

`Get` 的默认方法 `with`（builder 风格）：

```
self.with(index, value)  →  内部调用 set(index, value)，再返回 self
```

#### 4.4.3 源码精读

`debug` 与 `display` 几乎是镜像，都是「闭包 → 局部 Wrapper → impl trait」：

[lib.rs:42-59](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L42-L59) —— `debug`：把 `Fn(&mut Formatter) -> Result` 的闭包变成 `impl Debug`。

[lib.rs:61-78](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L61-L78) —— `display`：同结构，实现的是 `Display`。

`option_eq` 一行核心：`left.is_some_and(|v| v == other)`：

[lib.rs:279-284](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L279-L284) —— `option_eq`；`L: PartialEq<R>` 让 `Option<&str>` 能直接和 `&str` 比较等跨类型比较。

`Static` 的关键字段与判等/哈希：`PartialEq` 用 `std::ptr::eq`，`Hash` 用 `write_usize(self.0 as *const T as usize)`——**写的是地址，不是内容**：

[lib.rs:286-318](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L286-L318) —— `Static` 定义及其 `Deref` / `Copy` / `Clone` / `Eq` / `PartialEq` / `Hash` 实现；注意 `eq` 与 `hash` 都只关心指针地址。

`Get` trait 提供抽象的 `get_ref` / `get_mut`，并用默认方法给出 `get` / `set` / `with`：

[lib.rs:320-353](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs#L320-L353) —— `Get` trait；`get` 要求 `Component: Copy`，`with` 是 `Self: Sized` 的链式 setter。

#### 4.4.4 代码实践

**实践目标**：用 `debug()` 临时拼一段格式化输出，并验证 `Static` 「按指针相等」的真实表现。

**操作步骤**：

```rust
// 示例代码
use std::collections::HashSet;
use typst_utils::{debug, Static};

fn main() {
    // 1) debug(): 临时把闭包变成 impl Debug
    let point = (3, 4);
    let formatted = format!("{:?}", debug(|f| write!(f, "({}, {})", point.0, point.1)));
    assert_eq!(formatted, "(3, 4)");

    // 2) Static: 内容相同、地址不同 → 不相等
    let a: &'static str = Box::leak("typst".to_string().into_boxed_str());
    let b: &'static str = Box::leak("typst".to_string().into_boxed_str());
    assert_eq!(a, b);                  // 内容相等
    assert!(!Static(a).eq(&Static(b))); // 但 Static 按指针判 → 不等

    // 3) 同一地址放入 HashSet，按指针去重
    let mut set: HashSet<Static<str>> = HashSet::new();
    set.insert(Static(a));
    set.insert(Static(a));             // 同地址 → 视为已存在
    assert_eq!(set.len(), 1);
    println!("ok");
}
```

**需要观察的现象**：第 2 步若把 `Box::leak` 两次换成直接 `Static("typst")` 两次，结果可能因编译器是否合并相同字符串字面量而不同——这恰好说明 `Static` 的相等**完全取决于地址**，不应依赖内容。第 3 步说明 `Static` 可作为 `HashSet` / `HashMap` 的 key，且按地址去重。

**预期结果**：所有断言通过，打印 `ok`。**待本地验证**：字符串字面量是否被编译器合并取决于编译器实现，因此示例刻意用 `Box::leak` 制造两个确定不同的地址。

#### 4.4.5 小练习与答案

1. **练习**：若把 `Static` 的 `PartialEq` 改成「比较内容」（`self.0 == other.0`），会丢失什么能力？
   **答案**：会丢失「以地址作为唯一身份证」的能力——两个内容相同但来自不同全局定义处的常量会被判为相等，无法再用 `Static` 做「按定义点去重」。 Typst 用它正是想区分「同一处全局常量」与「恰好内容相同的另一处」。
2. **练习**：`debug(|f| write!(f, "..."))` 返回的值能放进 `Vec<Box<dyn Debug>>` 吗？
   **答案**：可以。它实现了 `Debug`，因此能作为 trait object 使用；只是返回类型是匿名的 `impl Debug`（具体类型是函数内部那个 `Wrapper<F>`），放进 `Box<dyn Debug>` 时需先装箱。
3. **练习**：`option_eq(Some(3), 3)` 与 `option_eq(None, 3)` 分别返回什么？
   **答案**：`true` 与 `false`。前者 `Some(3)` 中的 `3 == 3`；后者 `None` 直接判否。

---

## 5. 综合实践

把本讲两个最重要的工具——`group_by_key` 与 `Static`——串成一个完整任务。

**任务**：模拟一份「按部门连续记录的开支流水」，用 `SliceExt::group_by_key` 汇总每个连续段的金额；再用 `Static` 作为「部门枚举的全局身份证」，体会它按指针而非内容相等的特性。

**操作步骤**：

```rust
// 示例代码（综合实践）
use std::collections::HashSet;
use typst_utils::{SliceExt, Static};

// 用 'static 字符串充当「部门常量」的地址身份证
static SALES: &str = "sales";
static ENG: &str = "eng";

fn main() {
    // 1) 连续分组汇总（注意 "sales" 出现两次、不连续 → 两组）
    let ledger: &[(&'static str, i32)] = &[
        ("sales", 100),
        ("sales", 50),
        ("eng", 200),
        ("eng", 30),
        ("sales", 20),
    ];

    println!("== 连续分组汇总 ==");
    for (dept, rows) in ledger.group_by_key(|(d, _)| *d) {
        let sum: i32 = rows.iter().map(|(_, v)| v).sum();
        println!("{dept}: {sum}");
    }
    // 输出：
    //   sales: 150
    //   eng: 230
    //   sales: 20

    // 2) Static 按指针去重
    let mut depts: HashSet<Static<str>> = HashSet::new();
    depts.insert(Static(SALES));
    depts.insert(Static(SALES)); // 同地址 → 去重
    depts.insert(Static(ENG));
    println!("== 部门数量（按地址）==");
    println!("{}", depts.len()); // 2
}
```

**需要观察的现象**：
- 汇总输出有**三行**而非两行——`group_by_key` 只合并连续相同键，末尾的 `sales` 自成一组。这正是它和「整体归并」的区别。
- `depts.len()` 为 `2`：即便 `SALES` 与 `ENG` 内容都是普通字符串，`Static` 也只认地址。

**预期结果**：按注释中的输出打印。若你的数据顺序不同，分组数会随之变化——可尝试把末尾的 `("sales", 20)` 移到开头，观察分组如何改变。

---

## 6. 本讲小结

- typst-utils 通过 **extension trait** 模式（自建 trait + 为标准库类型 impl）绕过孤儿规则，给 `NonZeroUsize`、`Option`、`[T]`、迭代器等加上便利方法；调用前必须 `use` 该 trait。
- `SliceExt` 提供 `trim_start/end_matches`、`group_by_key`、`split_prefix_suffix` 四个高频切片操作；`group_by_key` 只合并**连续**相同键。
- `GroupByKey` 是惰性迭代器，每次 `next` 只消费一段；`MaybeReverseIter::rev_if` 用「双 Option 扁平化链」把「是否反转」两种迭代器统一成一个具体类型，避免动态分发。
- `debug()` / `display()` 用局部 `Wrapper` 把闭包变成 `impl Debug` / `impl Display`；`option_eq` 让 `Option<L>` 与裸值 `R` 跨类型比较。
- `Static<T>` 是 `&'static T` 的薄包装：`Copy` 且 `Deref` 到 `T`，但 `PartialEq` / `Hash` **只按指针地址**，适合做全局常量的「地址身份证」。
- `Get<Index>` 用泛型 trait 抽象「按下标取/设组件」，并提供 `get` / `set` / `with` 的默认方法；`Numeric` 则用一串 supertrait 统一各种数值类型（详见 u2-l1）。

## 7. 下一步学习建议

本讲的扩展 trait 是后续所有主题的「语法热身」。下一篇 [u1-l3 声明式辅助宏](u1-l3-declarative-macros.md) 将转入 `macro_rules!`，讲解 `singleton!`、`sub_impl!`、`assign_impl!`、`display!` 等用宏消除样板代码的写法。

进入第二单元后，建议优先读 [u2-l1 Scalar](u2-l1-scalar-deterministic-float.md)，看 `Numeric` trait 如何被一个真实的 `Scalar`（可哈希、可排序的确定性浮点）实现；之后再按 [u2-l5 ListSet 与分组去重](u2-l5-listset-and-dedup.md) 回过头来细读 `Rdedup` 与 `ListSet`，把「切片/集合」这条线补齐。

如果想立刻看到这些工具的真实用法，可以在仓库里检索 `group_by_key`、`split_prefix_suffix`、`rev_if` 在 `crates/typst-layout/` 下的调用点，跟踪一两条排版调用链。
