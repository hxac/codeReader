# Value 类型系统：从 none 到 dict

## 1. 本讲目标

上一讲（u3-l2）我们看到 `Library` 如何装配出全局作用域，作用域里每个绑定最终都是一个 `Value`。本讲就打开这个 `Value` 本身——它是 Typst 脚本世界中「一切值的统一 Rust 表示」。学完本讲，你应当能够：

- 列举 `Value` 枚举的全部 30 个变体，并说出每个变体背后的 Rust 类型；
- 解释「类型标识」机制：`Value::ty()` 如何把 Rust 变体映射为脚本层的 `Type`，`Type` 的短名（`int`）与长名（`integer`）分别用在哪里；
- 跟踪一次 `+` 运算从 AST 到 `ops::add` 的派发路径，理解「按 `(lhs, rhs)` 变体对派发」的 match 矩阵与统一报错的 `mismatch!` 宏；
- 说明 `str` / `array` / `dict` 的方法如何通过 `#[ty(scope)]` + `#[scope]` + `#[func]` 三件套注册进类型作用域，再被脚本里的 `.len()` 调用；
- 在 Rust 单元测试中手工构造 `Value`，执行加法/拼接并核对 `repr` 输出与错误消息。

## 2. 前置知识

- **Rust 枚举与模式匹配**：`Value` 是一个大型枚举，运算符语义全部写在 `match (lhs, rhs)` 的二元模式里。看懂 `match` 就能看懂本讲一半的源码。
- **trait（特征）**：`Repr`（调试表示）、`NativeType`（原生类型元数据）、`IntoValue` / `FromValue`（与 `Value` 的双向转换，u3-l5 会深入 cast 体系）。
- **`EcoString` / `EcoVec` / `Arc`**：来自 [ecow](https://github.com/typst/ecow) 与标准库的三个「克隆很便宜」的容器。`Str(EcoString)`、`Array(EcoVec<Value>)`、`Dict(Arc<...>)` 的克隆都是 O(1) 级别的指针复制或引用计数递增。
- **解释器为什么需要统一值类型**：Typst 求值器是树遍历解释器（不是先编译成字节码的虚拟机），AST 的每个表达式节点求值后都要返回「同一种东西」，这个东西就是 `Value`。
- **为什么所有变体都要可哈希**：u3-l1 讲过编译结果靠 comemo 记忆化缓存，缓存键要用到哈希，因此 `Value` 必须实现 `Hash`。这也解释了 `Dict` 内部用 `Arc<IndexMap>` 共享存储——既便宜克隆，又可哈希。
- **承接语法层**：u2 讲过 `1 + 2` 会被解析成 `SyntaxNode`/AST 的二元运算节点；本讲回答「求值这个节点时发生什么」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/foundations/value.rs` | `Value` 枚举本体、`ty()`/`display()` 等固有方法、`Dynamic` 逃生舱、`primitive!` 宏、单元测试 |
| `crates/typst-library/src/foundations/ops.rs` | 全部运算符（join/pos/neg/add/sub/mul/div/not/and/or/eq/比较/in）的按类型派发与报错 |
| `crates/typst-library/src/foundations/str.rs` | 字符串类型 `Str`：存储、`#[scope]` 方法表、`Repr`、正则 `Regex` |
| `crates/typst-library/src/foundations/array.rs` | 数组类型 `Array`：存储、方法表（len/first/at/push/insert…）、`Repr` |
| `crates/typst-library/src/foundations/dict.rs` | 字典类型 `Dict`：存储、方法表（at/insert…）、`Repr` |
| `crates/typst-library/src/foundations/repr.rs` | `Repr` trait、脚本函数 `#repr(...)`、各种格式化工具（进制、浮点、逗号列表） |
| `crates/typst-library/src/foundations/ty.rs` | `Type` 与 `NativeType`：类型的名字、构造函数、作用域 |
| `crates/typst-library/src/diag.rs` | （节选）`HintedStrResult` / `HintedString`——ops 报错的错误类型 |
| `crates/typst-eval/src/ops.rs`、`crates/typst-eval/src/code.rs` | 求值器如何把 AST 运算符接到上面的 ops 函数上 |

> 同目录下还有 `int.rs`、`float.rs`、`bytes.rs`、`decimal.rs`、`duration.rs` 等兄弟文件，分别是数值族各类型的独立实现，本讲只在与 `Value` 交界处提及，建议学完本讲后自行浏览。

## 4. 核心概念与源码讲解

### 4.1 Value 枚举与类型标识

#### 4.1.1 概念说明

脚本里能写出的每一种值——`none`、`auto`、`true`、`12`、`3.24`、`"hello"`、`12pt`、`#f79143ff`、`(1, "hi")`、`(a: 1)`、`upper`（函数）……——在 Rust 侧都对应 `Value` 枚举的一个变体。设计动机有两个：

1. **同构传递**：求值器、`Content` 字段、函数参数（`Args` 里装的就是 `Value` 流）都只认一种类型，避免泛型到处扩散。
2. **动态类型语义**：Typst 是动态类型语言，`1 + "x"` 合不合法要到运行时才知道，因此值的「身份」必须随值携带。

`Value` 只有 `Default` 与 `Clone` 两个派生，等值、哈希、序列化等语义都在文件后半部分手动实现；同时它被刻意控制在 \( \mathrm{size\_of}\langle\text{Value}\rangle \le 32 \,\text{字节} \)，这样克隆和传递都很廉价。

#### 4.1.2 核心流程

一个 `Value` 的生命周期大致是：

```text
字面量/函数返回值
      │  求值器(typst-eval)构造
      ▼
   Value ──ty()──► Type（脚本层 type(12) 的来源）
      │
      ├──ops::add/join/...──► 新的 Value（或 HintedString 错误）
      ├──repr()────────────► Str（调试表示，如 "(1, 2)"）
      └──display()─────────► Content（插入文档时的展示形态，衔接排版）
```

关键点：`Value` 自己几乎不含行为，所有「语义」都被拆到 `ops.rs`（运算）、各类型的 `#[scope]`（方法）和 `display()`（展示）里。

#### 4.1.3 源码精读

**枚举本体**：30 个变体按「空值 → 数值族 → 视觉族 → 文本/集合 → 可执行/元」排列，[value.rs:L26-L88](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L26-L88)。节选：

```rust
pub enum Value {
    /// The value that indicates the absence of a meaningful value.
    #[default]
    None,
    /// A value that indicates some smart default behaviour.
    Auto,
    /// An integer: `120`.
    Int(i64),
    /// A floating-point number: `1.2`, `10e-4`.
    Float(f64),
    /// A length: `12pt`, `3cm`, `1.5em`, `1em - 2pt`.
    Length(Length),
    ...
    /// A string: `"string"`.
    Str(Str),
    ...
    /// An array of values: `(1, "hi", 12cm)`.
    Array(Array),
    /// A dictionary value: `(a: 1, b: "hi")`.
    Dict(Dict),
    /// An executable function.
    Func(Func),
    ...
    /// A dynamic value.
    Dyn(Dynamic),
}
```

要点：`#[default]` 挂在 `None` 上，说明「没有值」是全系统的默认态；`Int(i64)` 与 `Float(f64)` 是两个独立变体，整数不悄悄变浮点。

**类型标识 `ty()`**：[value.rs:L116-L149](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L116-L149) 是一张「变体 → Type」的对照表，每个臂一行：

```rust
pub fn ty(&self) -> Type {
    match self {
        Self::None => Type::of::<NoneValue>(),
        Self::Auto => Type::of::<AutoValue>(),
        Self::Bool(_) => Type::of::<bool>(),
        Self::Int(_) => Type::of::<i64>(),
        ...
        Self::Dyn(v) => v.ty(),
    }
}
```

`Type` 定义在 [ty.rs:L66](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L66)：`pub struct Type(Static<NativeTypeData>)`，一个 `Copy` 的静态句柄；`Type::of::<T>()`（[ty.rs:L70-L72](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L70-L72)）向实现了 `NativeType`（[ty.rs:L198-L210](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L198-L210)）的 Rust 类型索要元数据。`Type` 同时携带短名与长名：短名 `short_name`（[ty.rs:L75-L77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/ty.rs#L75-L77)）用于代码（如 `int`），长名 `long_name` 用于诊断（如 `integer`）；`ty.rs` 中 `impl Display for Type` 打印的是长名（约 L179-L183，行号待确认），这一点马上会在 4.2 的错误消息里兑现。

**`Dynamic` 逃生舱**：不是所有类型都值得进枚举。[value.rs:L495](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L495) 的 `pub struct Dynamic(Arc<dyn Bounds>)` 用 trait 对象装「实现了 `Debug + Repr + NativeType + PartialEq + Hash` 的任意 Rust 类型」。例如对齐方式 `Alignment` 就走这条路——4.2 里 `pos()` 的源码会对它 `downcast::<Alignment>()`。

**`primitive!` 宏**：[value.rs:L575-L616](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L575-L616) 定义、[value.rs:L617-L661](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L617-L661) 逐类型实例化，一行声明同时生成 `Reflect` / `IntoValue` / `FromValue` 三个实现：

```rust
primitive! { bool: "boolean", Bool }
primitive! { i64: "integer", Int }
primitive! { f64: "float", Float, Int(v) => v as f64 }
```

注意第三个实例的尾参 `Int(v) => v as f64`：把 `FromValue` 写成「整数可以转成浮点」，这就是脚本里 `float` 参数能吃 `3` 的地方。更有趣的还有 `Str` 实例（[value.rs:L634-L638](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L634-L638)）允许 `Symbol` 转字符串，以及 `Func` 实例（[value.rs:L653-L658](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L653-L658)）允许「类型」和「符号」直接当函数用——`table` 这种类型名能出现在函数位置，根源就在这。

**体积约束**：单元测试 [value.rs:L721-L723](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/value.rs#L721-L723) 明确断言 `size_of::<Value>() <= 32`。
