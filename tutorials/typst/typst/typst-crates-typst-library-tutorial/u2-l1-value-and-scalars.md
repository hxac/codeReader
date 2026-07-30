# Value 枚举与标量类型

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `Value` 枚举为什么是 Typst 运行时的「万能值类型」，并把它每一个变体对应到背后的 Rust 类型。
- 解释 `#[default]` 为什么落在 `None` 上，以及 `none` 与 `auto` 这两种「空」在语义上的本质区别。
- 看懂 `primitive!` 宏如何用 `Reflect` / `IntoValue` / `FromValue` 三个 trait 把 Rust 类型与 `Value` 变体桥接起来。
- 理解 `Value` 上的 `==`、`<`、`+` 等运算最终都派发到 `ops.rs` 里的自由函数。

本讲是整个「值与类型基础」单元的起点：之后讲容器类型（Array/Dict）、类型转换系统（cast/Type/Module）都要建立在对 `Value` 的理解之上。

## 2. 前置知识

在阅读本讲前，你需要知道：

- **Typst 是一门脚本化的排版语言**：用户在「代码模式」里写的每一个字面量（`1`、`"hi"`、`12pt`、`none`）和表达式的结果，最终都要变成一个 Rust 里的值。
- **枚举（enum）作为带标签的联合体**：Rust 的 `enum` 每个变体可以携带不同类型的数据，这正是表示「任意类型值」的天然工具。
- **trait 与泛型**：本讲会用到 `Reflect`、`IntoValue`、`FromValue`、`Repr` 等 trait，你可以把它们理解为「类型与 `Value` 之间的转换契约」。
- 你应当已经读过 [u1-l3 标准库的装配](u1-l3-library-assembly.md)，知道 `Library` 是标准库的配置对象；本讲则进入这个配置对象里流转的最基本单位——「值」。

> 直觉提示：你可以把 `Value` 想成一只「俄罗斯套娃的最外层盒子」。无论里面装的是整数、字符串还是整段文档内容，对外都统一表现为 `Value`。求值器（`typst-eval`）只需要认识这一种盒子，至于拆盒子的细节，则交给本讲的转换机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/foundations/value.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs) | 定义 `Value` 枚举本体，以及 `Dynamic` 动态值、`primitive!` 宏、序列化/反序列化、`Debug`/`Repr`/`PartialEq`/`Hash` 等 trait 实现。 |
| [src/foundations/none.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/none.rs) | 定义 `NoneValue`（即 `none`），以及它与 Rust `Option<T>`、`()` 的转换。 |
| [src/foundations/auto.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs) | 定义 `AutoValue`（即 `auto`）与 `Smart<T>` 枚举。 |
| [src/foundations/int.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs) | 把 Rust 的 `i64` 注册为 Typst 的 `int` 类型（常量、构造器、位运算）。 |
| [src/foundations/float.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/float.rs) | 把 Rust 的 `f64` 注册为 Typst 的 `float` 类型。 |
| [src/foundations/str.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs) | 定义 `Str(EcoString)` 新类型，即 Typst 的 `str`。 |
| [src/foundations/ops.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs) | 对值进行运算的「自由函数库」：`add`/`sub`/`mul`/`div`/`neg`/`join`/`equal`/`compare`。 |

## 4. 核心概念与源码讲解

### 4.1 Value 枚举总览：Typst 运行时的万能值类型

#### 4.1.1 概念说明

Typst 的求值器需要一种「能装下任何类型」的统一类型，好让所有表达式、函数参数、字典取值都用同一种 Rust 类型传递。这个类型就是 `Value`——一个带标签的联合体（tagged union）。它的设计目标有两点：

1. **类型擦除**：调用方拿到的是 `Value`，不需要在编译期知道里面是整数还是字符串。
2. **可区分**：通过 `match` 仍然能在运行期取出真实数据。

`Value` 用 `#[derive(Default, Clone)]` 标注，意味着它有默认值、且克隆廉价（多数变体内部是引用计数或 `Copy`）。

#### 4.1.2 核心流程

一个 `Value` 的「生命周期」大致是：

```text
字面量/表达式  ──求值──▶  构造出某个 Value::变体(数据)
                                   │
                ┌──────────────────┼──────────────────┐
                ▼                  ▼                  ▼
          ty() 查询类型        cast() 转回具体类型    参与运算(ops.rs)
        (Type::of::<T>())     (FromValue::from_value)  (add/equal/…)
```

- **构造**：由 Rust 类型 `T` 通过 `IntoValue::into_value` 包成 `Value::$variant(self)`。
- **查询类型**：`Value::ty()` 把变体映射回一个 `Type` 对象（用于 `type(x)`、错误提示）。
- **转换回去**：`Value::cast::<T>()` 委托给 `FromValue`，把 `Value` 还原成具体 Rust 类型 `T`。
- **运算**：`==` / `<` / `+` 等，由 `PartialEq` / `PartialOrd` 实现派发到 `ops.rs`。

#### 4.1.3 源码精读

`Value` 枚举本体，含约 30 个变体，覆盖了 Typst 的全部内置值类型：[src/foundations/value.rs:26-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L26-L88)。每个变体的文档注释直接说明了它对应的 Typst 字面量写法。

`#[default]` 标注落在 `None` 上，因此 `Value::default()` 等价于 `Value::None`：[src/foundations/value.rs:27-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L27-L29)。这是一个关键设计决策——后面会讲为什么「默认值」是 `none` 而不是 `auto`。

`ty()` 方法把每个变体映射到其 `Type`（动态值 `Dyn` 例外，它委托给内部对象）：[src/foundations/value.rs:116-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L116-L149)。注意它用 `Type::of::<T>()` 而不是字符串，类型身份由 Rust 的类型系统保证。

`cast()` 是把 `Value` 还原为具体类型的统一入口，内部直接委托给 `FromValue`：[src/foundations/value.rs:152-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L152-L154)。

`display()` 决定一个值「插入文档时」如何呈现——比如整数渲染成文本、`none` 渲染为空：[src/foundations/value.rs:193-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L193-L209)。

为了控制内存占用，源码用测试断言 `Value` 的大小不超过 32 字节：[src/foundations/value.rs:723-725](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L723-L725)。这正是它能廉价克隆、到处传递的前提。

#### 4.1.4 代码实践

**实践目标**：亲手建立「变体 → Rust 类型」的映射，固化对枚举结构的记忆。

操作步骤：

1. 打开 [src/foundations/value.rs:26-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L26-L88)。
2. 逐行阅读每个变体，按下表填写背后的 Rust 类型（参考文件顶部 `use crate::foundations::{...}` 的导入）。

需要观察的现象：变体名与 Rust 类型名并不总是一致（例如 `Int(i64)`、`Str(Str)`、`Bool(bool)`），且有些变体携带的数据本身就是本项目自定义类型（如 `Content`、`Module`）。

预期结果：你能得到一张类似下面的映射表（节选）：

| `Value` 变体 | Rust 类型 | 含义 |
| --- | --- | --- |
| `None` | `NoneValue`（单元结构） | 无值 |
| `Auto` | `AutoValue`（单元结构） | 智能默认 |
| `Bool(bool)` | `bool` | 布尔 |
| `Int(i64)` | `i64` | 64 位有符号整数 |
| `Float(f64)` | `f64` | 64 位浮点 |
| `Str(Str)` | `Str`（包装 `EcoString`） | 字符串 |
| `Dyn(Dynamic)` | `Arc<dyn Bounds>` | 不在枚举里的动态值 |

> 完整 30 个变体的映射表见本讲「综合实践」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Value` 用 `#[derive(Default)]` 且默认值是 `None`，而不是为每个变体单独提供构造函数就够了？

**参考答案**：`Default` 让很多泛型容器（如 `Option`、缓存槽、字段默认值）可以统一获得一个「零值」。选 `None` 是因为 `none` 在 Typst 语义里表示「没有任何值」，是最安全、最不会产生副作用的占位符；而 `auto` 带有「让系统自行决定」的语义，作为默认值会意外触发智能行为。

**练习 2**：`Value::ty()` 对 `Value::Dyn(v)` 分支为什么写得和其他分支不一样？

**参考答案**：其他变体的类型在编译期就确定了，可以直接用 `Type::of::<T>()`；而 `Dyn` 包装的是类型擦除的对象（`Arc<dyn Bounds>`），其真实类型只有运行期才知道，所以必须委托 `v.ty()`（最终调用 `Dynamic::ty` → `Bounds::dyn_ty` → `Type::of::<T>()`）。

---

### 4.2 primitive! 宏：标量类型与 Value 的桥接

#### 4.2.1 概念说明

`Value` 是类型擦除的盒子，但函数签名（例如一个 `#[func]` 想要 `i64` 参数）需要的是具体 Rust 类型。这就需要一个**双向转换机制**：

- `IntoValue`：Rust 类型 `T` → `Value`（装盒）。
- `FromValue`：`Value` → Rust 类型 `T`（拆盒，可能带类型转换）。
- `Reflect`：描述「这个类型能接受什么样的 `Value`」，用于错误提示和合法性检查。

为了避免为每个标量类型手写三份几乎相同的 impl，源码提供了 `primitive!` 宏批量生成。这个宏还支持「转换分支」——例如 `f64` 在拆盒时除了接受 `Float`，还接受 `Int` 自动提升。

#### 4.2.2 核心流程

`primitive!` 宏对每个声明的类型生成如下逻辑：

```text
声明:  primitive! { f64: "float", Float, Int(v) => v as f64 }
                 │       │        │        └─ 额外转换分支: 收到 Int 就转成 f64
                 │       │        └─ 主变体: Value::Float
                 │       └─ 类型显示名
                 └─ Rust 类型

生成:
  Reflect::castable(v) => matches!(v, Value::Float(_) | Value::Int(_))
  IntoValue::into_value(self) => Value::Float(self)
  FromValue::from_value(v)    => match v { Float(v)=>Ok(v), Int(v)=>Ok(v as f64), _=>Err(...) }
```

#### 4.2.3 源码精读

`primitive!` 宏的定义：[src/foundations/value.rs:578-617](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L578-L617)。它为 `$ty` 实现 `Reflect`（`castable` 用 `matches!` 列出可接受的变体）、`IntoValue`（`Value::$variant(self)`）、`FromValue`（`match` 主变体与额外分支，其余报错）。

宏的所有调用点，逐行列出了哪些 Rust 类型被注册为 `Value` 的标量变体：[src/foundations/value.rs:619-663](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619-L663)。注意几个带转换分支的有趣案例：

- `f64` 接受 `Int(v) => v as f64`（整数自动转浮点）。
- `Rel<Length>` 同时接受 `Length` 和 `Ratio`（两者都能升级为相对长度）。
- `Str` 接受 `Symbol(symbol) => symbol.get().into()`（符号可当字符串）。
- `Content` 接受 `None`（变空内容）、`Symbol`、`Str`（都升级为内容）。

#### 4.2.4 代码实践

**实践目标**：理解转换分支如何让 Typst 的「弱类型」体验成为可能。

操作步骤：

1. 阅读 [src/foundations/value.rs:619-663](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619-L663) 中 `primitive! { f64: ... }` 与 `primitive! { Rel<Length>: ... }` 两行。
2. 对照单元测试 [src/foundations/value.rs:727-754](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L727-L754)，找出体现「整数转浮点」「比例加长度变相对长度」的断言。

需要观察的现象：测试里 `test(3.24, "3.24")`、`test(Ratio::new(0.3) + Length::from(Abs::cm(2.0)), "30% + 56.69pt")`。

预期结果：你能解释「为什么 Typst 里 `1 + 1.0` 不会报类型错误」——因为 `FromValue for f64` 的转换分支默默把 `Int` 提升成了 `Float`。

#### 4.2.5 小练习与答案

**练习 1**：若一个 `#[func]` 的参数声明为 `f64`，传入 `Value::Int(5)` 会发生什么？

**参考答案**：`FromValue for f64` 的 `match` 命中 `Int(v) => Ok(v as f64)`，于是返回 `5.0`，不报错。这就是 Typst 中「期望浮点处可传整数」的底层来源。

**练习 2**：`Content` 类型的 `primitive!` 声明里为什么要包含 `None => Content::empty()` 这个分支？

**参考答案**：为了让 `none` 能无缝出现在文档流里——`none` 表示「没有内容」，于是把它转成空内容 `Content::empty()`，拼接进文档时什么都不产生，符合 `none` 不可见的语义。

---

### 4.3 标量类型详解：bool、int、float、str

#### 4.3.1 概念说明

本节聚焦最基础的四种标量。它们在 Rust 侧的表示各不相同：

- **bool**：直接用 Rust 原生 `bool`，对应 `Value::Bool`。
- **int**：用 `i64`（64 位有符号整数），对应 `Value::Int`。
- **float**：用 `f64`（64 位 IEEE 754 浮点），对应 `Value::Float`。
- **str**：不是裸 `String`，而是新类型 `Str(EcoString)`，对应 `Value::Str`。

其中 `int`、`float`、`str` 还通过 `#[ty(...)]` 宏注册成了「一等类型」——即用户可以在 Typst 里写 `int.max`、`str("x")`、`float.inf` 这样的成员访问与构造调用。这是「标量」与「上一节的 `primitive!` 注册」的区别：`primitive!` 只解决值转换，`#[ty]` 额外把类型本身变成一个带作用域（常量、方法）的对象。

#### 4.3.2 核心流程

以 `int` 为例，类型注册的流程是：

```text
#[ty(scope, cast, name = "int", ...)]            ← 注册为 Typst 类型 "int"
type i64;                                        ← Rust 真实类型仍是 i64
   │
   ├── primitive! { i64: "integer", Int }        ← 同时参与 Value 转换（见 4.2）
   │
   └── #[scope(ext)] impl i64 {                  ← 给 int 挂上常量/方法
           const MAX / MIN
           fn construct(...)  // int(...) 构造器
           fn signum / bit-and / ... // 成员方法
       }
```

#### 4.3.3 源码精读

**整数** `int`：通过 `#[ty(scope, cast, name = "int", title = "Integer", since = "forever")]` 把 `i64` 注册为类型，文档注释详细说明了 64 位补码表示与取值范围：[src/foundations/int.rs:68-69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L68-L69)。

取值范围为 \(-2^{63}\) 到 \(2^{63}-1\)，对应常量 `int.min` 与 `int.max`：[src/foundations/int.rs:73-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L73-L79)。注意最小负整数 \(-2^{63}\) 无法直接写成字面量（因为 `-9223372036854775808` 会被解析成对 `9223372036854775808` 取负，而后者已溢出），所以源码注释强调要用 `int.min`。

整数构造器 `int(...)` 支持 `bool`/`float`/`decimal`/`str`（可指定进制 `base`）的转换：[src/foundations/int.rs:99-146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L99-L146)。整数的 `Repr` 实现很简单，直接用 `Debug` 格式化：[src/foundations/int.rs:430-434](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L430-L434)。

**浮点** `float`：同样把 `f64` 注册为类型，并提供 `float.inf`（正无穷）与 `float.nan` 两个常量：[src/foundations/float.rs:29-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/float.rs#L29-L39)。其构造器接收一个 `ToFloat`（可由 `bool`/`int`/`ratio`/`str` 转换而来）：[src/foundations/float.rs:59-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/float.rs#L59-L65)。

**字符串** `str`：定义为包装 `EcoString` 的新类型 `Str`，并派生了 `Default`/`Eq`/`Ord`/`Hash` 等常用 trait：[src/foundations/str.rs:74-78](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L74-L78)。选用 `EcoString`（来自 `ecow` crate）是为了让小字符串栈上存储、克隆是引用计数的廉价操作。文档注释强调字符串长度与下标都以 **UTF-8 字节** 计：[src/foundations/str.rs:36-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L36-L73)。

**布尔** `bool`：没有独立的 `.rs` 文件，而是直接通过 `primitive! { bool: "boolean", Bool }` 注册（见 [src/foundations/value.rs:619](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619)），因为 Rust 原生 `bool` 不需要额外挂常量或方法。

#### 4.3.4 代码实践

**实践目标**：体会四种标量在「类型注册」上的差异。

操作步骤：

1. 对比 [src/foundations/int.rs:68-69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L68-L69) 与 [src/foundations/value.rs:619](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619)。
2. 思考：`bool` 为什么不需要 `#[ty]` 与独立的 `impl` 块，而 `int`/`float`/`str` 需要？

需要观察的现象：`int`/`float`/`str` 都有可被用户调用的构造器（如 `int("ff", base: 16)`）和成员方法（如 `(5).signum()`、`"a".split()`），而 `bool` 没有这些。

预期结果：你能总结出——「当一个标量类型需要暴露常量、构造器或方法给 Typst 用户时，就要用 `#[ty(scope)]` 注册并配 `#[scope] impl`；否则只用 `primitive!` 做值转换即可」。

#### 4.3.5 小练习与答案

**练习 1**：在 Typst 里写 `#int("beef", base: 16)` 会得到什么？请根据源码说明依据。

**参考答案**：得到整数 `48879`（即 0xbeef）。依据是 [src/foundations/int.rs:115-143](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/int.rs#L115-L143)：当输入是字符串时，构造器用 `base.v.value()` 取得进制（此处 16），再调用 `i64::from_str_radix(s, radix)` 解析。

**练习 2**：为什么 `Str` 不直接用 `String`，而要包一层 `EcoString`？

**参考答案**：`EcoString` 是「引用计数的写时复制」字符串：短字符串内联在栈上，长字符串克隆只增加引用计数而不复制底层字节。由于 Typst 求值过程中值会被大量克隆、传递（`Value` 本身就是 `Clone`），用 `EcoString` 能显著降低字符串的克隆与内存开销。

---

### 4.4 none 与 auto：两种「空」的语义差异

#### 4.4.1 概念说明

Typst 有两种看起来都「空」的值，但语义截然不同：

- **`none`（`NoneValue`）**：表示「没有值」。类似其他语言里的 null / 空结果。插入文档时不产生任何内容；与其他值拼接时像「单位元」（`x + none == x`）。
- **`auto`（`AutoValue`）**：表示「请用智能默认」。它不是「没有值」，而是一个明确的信号——告诉接收方「按上下文自行决定」。例如 `text.dir` 设为 `auto` 时，方向由语言自动推断。

理解二者区别，是读懂 Typst 样式系统（`Smart<T>`、set 规则默认值）的前提。

#### 4.4.2 核心流程

两者在 Rust 侧都是单元结构体（无数据），但映射到不同的 Rust 桥接类型：

```text
Value::None  ←→  NoneValue (单元结构)  ←→  Rust Option<T> 的 None、以及单元 ()
Value::Auto  ←→  AutoValue (单元结构)  ←→  Rust Smart<T> 的 Auto 分支
```

- `none` 是整个 `Value` 枚举的 `#[default]`（见 4.1）。
- `auto` 多用于「可设属性」，对应 `Smart<T>`：`Smart::Auto` 表示「自动」，`Smart::Custom(v)` 表示「显式指定」。

#### 4.4.3 源码精读

**`NoneValue`** 定义为单元结构体，注释点明「表示任何其他值的缺失」，且与任意值拼接时返回对方：[src/foundations/none.rs:24-26](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/none.rs#L24-L26)。它的 `Repr` 实现固定输出 `"none"`：[src/foundations/none.rs:63-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/none.rs#L63-L67)。

`none` 还桥接 Rust 的 `Option<T>`：`None` 转成 `Value::None`，`Some(v)` 转成 `v.into_value()`：[src/foundations/none.rs:98-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/none.rs#L98-L115)。同时 `()`（Rust 单元类型）也映射到 `Value::None`：[src/foundations/none.rs:78-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/none.rs#L78-L82)。

**`AutoValue`** 同样是单元结构体，注释点明「表示一个智能默认」：[src/foundations/auto.rs:20-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L20-L22)。其 `Repr` 固定输出 `"auto"`：[src/foundations/auto.rs:59-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L59-L63)。

`auto` 的真正用武之地是 `Smart<T>` 枚举——它在 `auto` 与「显式值」之间二选一，并附带大量辅助方法（`is_auto`/`custom`/`map`/`unwrap_or` 等）：[src/foundations/auto.rs:66-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L66-L73)。`Smart<T>` 的 `FromValue` 实现：收到 `Value::Auto` 就返回 `Smart::Auto`，否则尝试解析为 `Smart::Custom(T)`：[src/foundations/auto.rs:253-261](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L253-L261)。

#### 4.4.4 代码实践

**实践目标**：用一个真实参数体会 `auto` 与 `none` 的差别。

操作步骤：

1. 在源码中搜索 `Smart<` 的使用（例如 `Grep` 工具搜 `Smart<` 在 `src/text/`、`src/model/` 下的出现），找一个用 `Smart<T>` 声明的字段。
2. 阅读该字段的文档注释，理解「设为 `auto`」与「不设/设为 `none`」分别意味着什么。

需要观察的现象：`Smart<T>` 字段接受 `auto` 表示「让 Typst 自己决定」；而一个 `Option<T>` 字段接受 `none` 表示「用户没提供」。

预期结果：你能用自己的话讲清——`none` 是「值的缺失」，`auto` 是「值的委托（交给系统）」。二者都不是某种具体数据。

> 说明：本实践属于「源码阅读型实践」，无需运行；具体某字段接受 `auto` 后系统的实际行为，待本地结合具体元素验证。

#### 4.4.5 小练习与答案

**练习 1**：`Value::default()` 得到的是 `Value::None` 还是 `Value::Auto`？为什么？

**参考答案**：得到 `Value::None`。因为 `Value` 枚举上 `#[default]` 标注在 `None` 变体（[src/foundations/value.rs:27-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L27-L29)）。`none` 表示「无值」，是比 `auto`（「智能默认」）更中立、更安全的零值。

**练习 2**：`none` 与任意值 `x` 用 `+` 拼接，结果是什么？

**参考答案**：结果是 `x`（即 `none` 是拼接的单位元）。依据是 [src/foundations/ops.rs:94-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L94-L95) 中 `add` 的 `(a, None) => a` 与 `(None, b) => b` 两个分支（`join` 中也有同样处理，见 [src/foundations/ops.rs:27-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L27-L28)）。

---

### 4.5 Value 如何参与运算：ops.rs 的派发

#### 4.5.1 概念说明

`Value` 上定义了 `PartialEq` 与 `PartialOrd`，但这俩 trait 的实现并没有自己写比较逻辑，而是**委托**给 `ops.rs` 里的自由函数 `ops::equal` 与 `ops::compare`。同样，加减乘除等运算也集中在 `ops.rs`（`add`/`sub`/`mul`/`div`/`neg`/`pos`/`join`）。求值器（`typst-eval`，属于另一个 crate）在处理 Typst 的 `+`、`==`、`<` 等运算符时，最终都落到这些函数上。

这样设计的好处是：运算时的「跨类型规则」（比如 `1 == 1.0` 为真、`Int + Float = Float`、`Length + Ratio = Relative`）集中在一处维护，避免散落在各类型里。

#### 4.5.2 核心流程

```text
Value 的 PartialEq  ──▶  ops::equal(lhs, rhs)   ──▶  match (lhs, rhs) { ... }
Value 的 PartialOrd ──▶  ops::compare(lhs, rhs)  ──▶  match (lhs, rhs) { ... }
Typst 的 + 运算      ──▶  ops::add(lhs, rhs)     ──▶  match (lhs, rhs) { ... }
Typst 的「拼接」     ──▶  ops::join(lhs, rhs)    ──▶  match (lhs, rhs) { ... }
```

`ops::equal` 与 `ops::compare` 都是大 `match`，先处理「同类型」比较，再处理少量「跨类型但可比」的情况，最后兜底。

#### 4.5.3 源码精读

`Value` 的 `PartialEq` 直接调用 `ops::equal`：[src/foundations/value.rs:295-299](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L295-L299)。`PartialOrd` 调用 `ops::compare`：[src/foundations/value.rs:301-305](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L301-L305)。

`ops::equal` 的实现：同类型逐对比较，并额外允许几组「跨类型相等」——`Int` 与 `Float`（`i as f64 == f`）、`Int` 与 `Decimal`、`Length` 与 `Relative`（当比例部分为零）、`Ratio` 与 `Relative`（当长度部分为零）：[src/foundations/ops.rs:422-468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L422-L468)。其余一律返回 `false`（而不是报错）。

`ops::compare` 结构类似，但跨类型比较附带条件守卫（如 `(Length(a), Relative(b)) if b.rel.is_zero()`），不满足条件就走到兜底的 `mismatch!` 报错：[src/foundations/ops.rs:471-502](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L471-L502)。

`ops::add` 展示了算术运算的单位元与跨类型提升：`None` 是单位元，`Int + Int` 用 `checked_add` 防溢出，`Int + Float` 提升为 `Float`：[src/foundations/ops.rs:91-130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L91-L130)。

`ops::join`（拼接，用于把多个值串起来）也把 `None` 当单位元，并对 `Str`/`Content`/`Array`/`Dict` 等做拼接：[src/foundations/ops.rs:24-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L24-L44)。

> 名词解释：`mismatch!` 是 `ops.rs` 里的一个内部宏，用于在运算无法进行时返回形如「cannot join {ty} with {ty}」的错误（见 [src/foundations/ops.rs:17-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L17-L21)）。

#### 4.5.4 代码实践

**实践目标**：验证「`==` 不报错而 `<` 可能报错」这一不对称设计。

操作步骤：

1. 阅读 [src/foundations/ops.rs:422-468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L422-L468)（`equal`）与 [src/foundations/ops.rs:471-502](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L471-L502)（`compare`）的兜底分支。
2. 注意 `equal` 的兜底是 `_ => false`，而 `compare` 的兜底是 `_ => mismatch!(...)`（报错）。

需要观察的现象：比较 `"a" == 1` 会得到 `false`（不报错），而 `"a" < 1` 会触发「cannot compare string and integer」错误。

预期结果：你能解释这一设计——相等性总是可以回答「真或假」，但大小比较只在可排序类型对之间才有意义，无法比较时应显式报错而非悄悄返回某个 `Ordering`。

> 说明：是否触发报错、以及求值器对 `<` 的确切派发细节，待本地用 Typst CLI 运行样例验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `Value` 的 `PartialEq` 不直接 `#[derive(PartialEq)]`，而要委托给 `ops::equal`？

**参考答案**：派生的 `PartialEq` 只会比较变体标签和内部数据的字面相等，无法表达 Typst 需要的「跨类型相等」语义（如 `1 == 1.0`、`10pt == 100% + 0pt` 当比例/长度另一部分为零时）。委托给 `ops::equal` 可以在一个集中处实现这些规则。

**练习 2**：`ops::add` 中 `(Int(a), Int(b)) => Int(a.checked_add(b).ok_or_else(too_large)?)`，为什么要用 `checked_add` 而不是直接 `a + b`？

**参考答案**：`i64` 的 `+` 在溢出时会回绕（wrap around）而非报错，会产生错误的静默结果。`checked_add` 在溢出时返回 `None`，配合 `ok_or_else(too_large)?` 转成 Typst 的「数值过大」错误，符合 Typst 对整数运算要在溢出时报错的语义（参见 `int.rs` 文档对 `int.max`/`int.min` 的说明）。

---

## 5. 综合实践

本任务贯穿本讲全部模块，帮你把 `Value` 枚举、标量注册与运算派发串起来。

**任务**：编制一份完整的「`Value` 变体 → Rust 类型 → 运算特性」对照手册，并回答三个问题。

操作步骤：

1. 打开 [src/foundations/value.rs:26-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L26-L88)，列出全部变体。
2. 对每个变体，填出三列：Rust 类型、是否经过 `primitive!` 注册（参考 [src/foundations/value.rs:619-663](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L619-L663)）、在 `ops::equal` / `ops::compare` / `ops::add` 中是否有专属分支（参考 [src/foundations/ops.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs)）。
3. 回答：
   - `Value::default()` 是什么？依据哪一行代码？
   - 为什么 `1 == 1.0` 为 `true`，而 `none == auto` 为 `false`？
   - 把 Rust 的 `Option<i64>` 与 `Smart<i64>` 分别 `into_value()`，会得到哪个 `Value` 变体？

预期结果（节选参考）：

| `Value` 变体 | Rust 类型 | `primitive!` 注册 | 在 ops.rs 中的运算分支 |
| --- | --- | --- | --- |
| `None` | `NoneValue` | 否（单独 impl） | `add`/`join` 单位元、`equal` 自反 |
| `Auto` | `AutoValue` | 否（单独 impl） | 无（不参与算术/比较） |
| `Bool(bool)` | `bool` | 是 | `equal`/`compare` |
| `Int(i64)` | `i64` | 是 | `add`/`sub`/`mul`/`div`/`neg`/`equal`/`compare`（含跨类型） |
| `Float(f64)` | `f64` | 是 | 同上（与 `Int` 互通） |
| `Str(Str)` | `Str` | 是 | `join`/`add` 拼接、`equal`/`compare` |

参考答案：

1. `Value::default() == Value::None`，依据 [src/foundations/value.rs:27-29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L27-L29) 的 `#[default] None`。
2. `1 == 1.0` 命中 `ops::equal` 的跨类型分支 `(&Int(i), &Float(f)) => i as f64 == f`（[src/foundations/ops.rs:455](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ops.rs#L455)）；而 `none == auto` 没有任何分支匹配（`None` 只与 `None` 自反，`Auto` 只与 `Auto` 自反），落到 `_ => false`。
3. `Option<i64>` 的 `None` → `Value::None`，`Some(5)` → `Value::Int(5)`（[src/foundations/none.rs:98-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/none.rs#L98-L105)）；`Smart<i64>` 的 `Auto` → `Value::Auto`，`Custom(5)` → `Value::Int(5)`（[src/foundations/auto.rs:244-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/auto.rs#L244-L251)）。

## 6. 本讲小结

- `Value` 是 Typst 运行时的万能值类型，一个约 30 个变体的枚举，体积被测试约束在 32 字节以内，默认值是 `Value::None`。
- `Value::ty()` 把变体映射回 `Type`，`Value::cast::<T>()` 委托 `FromValue` 把值还原为具体 Rust 类型。
- `primitive!` 宏批量为标量类型生成 `Reflect`/`IntoValue`/`FromValue`，并支持「转换分支」（如 `Int` 自动提升为 `Float`），这是 Typst 弱类型体验的底层来源。
- `int`(`i64`)、`float`(`f64`)、`str`(`Str`) 通过 `#[ty]` 注册为一等类型（带常量/构造器/方法）；`bool` 只用 `primitive!` 注册。
- `none`（`NoneValue`，桥接 `Option`/`()`）表示「无值」，是运算单位元；`auto`（`AutoValue`，桥接 `Smart<T>`）表示「智能默认」，二者语义不同。
- `Value` 的 `==`/`<` 委托给 `ops.rs` 的 `equal`/`compare`，算术运算集中在 `ops::add` 等自由函数，跨类型规则集中维护。

## 7. 下一步学习建议

- 下一讲 [u2-l2 容器类型 Array、Dict、Bytes 与 Label](u2-l2-containers-array-dict-bytes.md) 将进入 `Value` 的容器变体，看看 `Array`/`Dict` 为何选用 `EcoVec`/`IndexMap`，以及 `Bytes` 的引用计数设计。
- 如果你想提前理解 `cast!`/`Reflect`/`FromValue` 的完整模型，可以先跳读 [src/foundations/cast.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs)，它会在 [u2-l3 类型转换系统](u2-l3-cast-type-module-scope.md) 系统讲解。
- 想看 `Value` 在求值器中如何被实际构造与运算，可在 `typst-eval` crate 中搜索 `ops::add`、`ops::equal` 的调用点（本 crate 只提供「机制」，「调用」在行为 crate）。
