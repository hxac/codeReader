# cast! 函数式宏：值的双向转换

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `cast!` 宏的三段输入语法（类型名、`self =>` 转出、若干 `pattern => expr` 转入臂），并能区分 `Pattern::Str`（字面量字符串）与 `Pattern::Ty`（`绑定: 类型`）两种转入模式。
- 解释 `type` 前缀（`dynamic` 标志）如何**同时**决定 `Reflect`、`IntoValue`、`FromValue` 三个 impl 是否生成。
- 看懂 `castable` / `input` / `output` / `from_value` / `into_value` 五个方法体的生成逻辑与判断先后顺序，并能手写一个 `cast!` 后预测它的展开结果。

本讲是「类型与值转换」这条线的核心：`#[ty]`（u2-l2）会自动给类型补一个最简的 `cast!`，而 `cast!` 本身才是把 Rust 值与 Typst `Value` 真正连起来的桥梁。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么需要「双向转换」

Typst 运行时里所有数据都表现为一个统一的 `Value` 枚举（整数、字符串、内容、动态类型……）。而 Rust 侧代码使用的是强类型（`i64`、`Color`、`Stroke`……）。当 Typst 调用一个原生函数、或解析一个元素的字段时，必须把 `Value` 转成具体的 Rust 类型；反过来，函数返回、字段读出时，又要把 Rust 类型转回 `Value`。

这套双向转换由三个运行时 trait 定义（位于 `typst-library`，不是本 crate）：

| trait | 方向 | 职责 |
|-------|------|------|
| `IntoValue` | Rust 类型 → `Value` | `fn into_value(self) -> Value` |
| `FromValue` | `Value` → Rust 类型 | `fn from_value(value: Value) -> HintedStrResult<Self>` |
| `Reflect` | 元信息 | 描述「能从哪些值转来 / 能转成什么 / 某个值能不能转」，服务于文档、错误提示与自动补全 |

它们的定义见：
- [Reflect trait 定义](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L33-L58)（`input` / `output` / `castable` / `error` 四个方法）。
- [IntoValue trait 定义](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L175-L178)。
- [FromValue trait 定义](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L252-L255)。

`cast!` 宏的全部工作，就是**根据用户写的转换规则，自动生成这三个 trait 的 impl**。

### 2.2 函数式过程宏

`cast!` 是一个**函数式过程宏**（function-like macro），由 `#[proc_macro]` 标注。回顾 u1-l2：它只有一个 `stream` 参数（宏调用的花括号体），没有 `item`。整段宏体由 `cast!` 自己解析，所以 `cast!` 的「语法」完全是宏自定义的——它既不是 Rust 语法，也不需要先有一个被装饰的真实 Rust 项。

入口见 [lib.rs 中 `#[proc_macro] pub fn cast`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L296-L301)：与其它六个宏一样，走 `cast::cast(stream.into()).unwrap_or_else(|err| err.to_compile_error()).into()`，出错绝不 panic。

### 2.3 共享工具回顾

本讲会用到 u1-l3 讲过的两个 util 工具：

- [`documentation()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L26-L59)：从属性列表里拼出文档字符串。`cast!` 把它用在每条转入臂的 `#[doc]` 上，生成 `CastInfo` 的说明文字。
- [`foundations` 路径简写](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L217-L225)：展开为 `::typst_library::foundations`，让生成代码里所有 `Reflect` / `FromValue` / `IntoValue` / `CastInfo` 都带绝对路径前缀，避免命名冲突。

## 3. 本讲源码地图

本讲几乎全部围绕 `cast.rs`，辅以 `util.rs` 的工具与 `lib.rs` 的入口：

| 文件 | 作用 |
|------|------|
| `src/cast.rs` | `cast!` 的解析（`CastInput` / `Cast` / `Pattern`）与五个方法体生成函数（`create_castable_body` / `create_input_body` / `create_output_body` / `create_into_value_body` / `create_from_value_body`）。 |
| `src/util.rs` | 提供 `documentation()`、`foundations` 简写、`bail!` 宏。 |
| `src/lib.rs` | `#[proc_macro] pub fn cast` 入口，以及库内文档里的 `CoolInt` 范例。 |

本讲按四个最小模块展开：

1. **4.1** `cast!` 的输入语法与中间结构（`CastInput` / `Cast` / `Pattern`）
2. **4.2** 三个 impl 的条件生成（`cast::cast` 入口的调度与 `dynamic` 开关）
3. **4.3** `castable` 与 `from_value`：判断与转换
4. **4.4** `input` / `output` / `into_value`：元信息与转出

---

## 4. 核心概念与源码讲解

### 4.1 cast! 的输入语法与中间结构（CastInput / Cast / Pattern）

#### 4.1.1 概念说明

`cast!` 是一种声明式 DSL。一个完整的 `cast!` 调用长这样（这是 `lib.rs` 文档里的官方范例）：

```rust ignore
struct CoolInt(u8);

cast! {
    CoolInt,                              // ① 目标类型
    self => self.0.into_value(),          // ② 转出（IntoValue）
    v: bool => Self(v as u8),             // ③ 转入臂（FromValue）
    v: i64 => if matches!(v, 0..=13) {    // ③ 转入臂
        Self(v as u8)
    } else {
        bail!("integer is not nice :/")
    },
}
```

完整范例见 [lib.rs 的 CoolInt 文档示例](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L276-L295)。

三部分含义：

- **① 目标类型**：要被实现转换 trait 的 Rust 类型。可加可选的 `type` 前缀，表示这是一个「动态类型」（dynamic），见 4.2。
- **② `self => expr`**：转出规则，`expr` 的类型须为 `Value`，生成 `IntoValue::into_value` 的方法体。这一段**可省略**。
- **③ 若干 `pattern => expr` 转入臂**：描述「哪些形态的 `Value` 能转成本类型，以及怎么转」。生成 `FromValue::from_value` 与 `Reflect` 相关方法体。这一段也可省略（配合 `type` 时常见）。

转入臂的 `pattern` 有两种写法，对应 `Pattern` 枚举的两个变体：

| 写法 | `Pattern` 变体 | 含义 | 例子 |
|------|---------------|------|------|
| `"字符串字面量"` | `Pattern::Str(LitStr)` | 按字符串内容匹配 | `"ascender" => Self::Ascender` |
| `绑定: 类型` | `Pattern::Ty(Pat, Type)` | 按类型匹配，把值绑定到名字 | `v: i64 => Self::Int(v)` |

#### 4.1.2 核心流程

`cast!` 的解析走标准的 `syn` 路线（u1-l2 已建立）：

```
TokenStream(宏体)
   │  syn::parse2::<CastInput>(stream)
   ▼
CastInput { ty, dynamic, into_value: Option<Expr>, from_value: Punctuated<Cast> }
                                  │
                                  ├── into_value：来自 ② self => expr（最多一条）
                                  └── from_value：来自 ③ 各条转入臂（零到多条）
```

注意三个字段是**正交**的：你可以只写 ①、只写 ②、只写 ③，或任意组合。这些「有没有」会直接决定 4.2 里哪些 impl 被生成。

#### 4.1.3 源码精读

**目标结构 `CastInput`**：[src/cast.rs:L125-L131](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L125-L131)。

```rust
struct CastInput {
    ty: syn::Type,
    dynamic: bool,
    into_value: Option<syn::Expr>,
    from_value: Punctuated<Cast, Token![,]>,
}
```

`dynamic` 就是那个 `type` 前缀；`into_value` 至多一条（`self =>`）；`from_value` 是逗号分隔的转入臂列表。

**解析 `CastInput`**：[src/cast.rs:L133-L155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L133-L155)。

```rust
let mut dynamic = false;
if input.peek(syn::Token![type]) {       // ① 可选的 type 前缀
    let _: syn::Token![type] = input.parse()?;
    dynamic = true;
}
let ty = input.parse()?;                 // ① 目标类型
let _: syn::Token![,] = input.parse()?;  // 类型后的逗号

let mut to_value = None;
if input.peek(syn::Token![self]) {       // ② 可选的 self => expr
    let _: syn::Token![self] = input.parse()?;
    let _: syn::Token![=>] = input.parse()?;
    to_value = Some(input.parse()?);
    let _: syn::Token![,] = input.parse()?;
}

let from_value = Punctuated::parse_terminated(input)?; // ③ 剩余的转入臂
```

关键点：① 的顺序是固定的——`type`（若有）→ 类型 → 逗号 → `self =>`（若有）→ 逗号 → 若干转入臂。`self =>` 段如果出现，后面必须跟逗号，因为它和转入臂都用逗号分隔。

**解析单条转入臂 `Cast`**：[src/cast.rs:L157-L165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L157-L165)。

```rust
let attrs = input.call(syn::Attribute::parse_outer)?; // 允许臂上写 #[doc] 等
let pattern = input.parse()?;                          // Pattern::Str 或 Pattern::Ty
let _: syn::Token![=>] = input.parse()?;
let expr = input.parse()?;                             // 转换表达式
```

注意 `attrs`：每条转入臂可以挂文档注释，这些注释会通过 `documentation()` 变成 `CastInfo` 的说明文字（见 4.4）。

**解析 `Pattern`**：[src/cast.rs:L167-L178](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L167-L178)。

```rust
if input.peek(syn::LitStr) {                 // 字符串字面量 → Pattern::Str
    Ok(Pattern::Str(input.parse()?))
} else {
    let pat = syn::Pat::parse_single(input)?; // 绑定名，如 v
    let _: syn::Token![:] = input.parse()?;
    let ty = input.parse()?;                  // 类型，如 i64
    Ok(Pattern::Ty(pat, ty))
}
```

判定方法很干脆：**先看下一个 token 是不是字符串字面量**。是 → 整段按字符串匹配；否 → 按 `绑定: 类型` 解析。

`Pattern` 与 `Cast` 的定义见 [src/cast.rs:L180-L192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L180-L192)：

```rust
struct Cast { attrs: Vec<syn::Attribute>, pattern: Pattern, expr: syn::Expr }

enum Pattern {
    Str(syn::LitStr),
    Ty(syn::Pat, syn::Type),
}
```

#### 4.1.4 代码实践

**目标**：把 4.1.1 的 CoolInt 范例拆解成 `CastInput` 的字段值。

**操作步骤**：

1. 阅读上面引用的 [lib.rs CoolInt 示例](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L276-L295)。
2. 逐段对照 [CastInput](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L125-L131) 的四个字段，填出它们的取值。

**需要观察的现象（预期结果）**：

```
ty         = CoolInt
dynamic    = false            （没有 type 前缀）
into_value = Some(self.0.into_value())
from_value = [
    Cast { pattern: Ty(v, bool), expr: Self(v as u8) },
    Cast { pattern: Ty(v, i64),  expr: if matches!(v, 0..=13) { ... } else { bail!(...) } },
]
```

两条转入臂都是 `Pattern::Ty`（没有用字符串字面量），所以 `from_value` 里**不会**出现 `Pattern::Str`。

#### 4.1.5 小练习与答案

**练习 1**：下面这条臂属于哪种 `Pattern`？
```rust ignore
"start" => Self::Start
```
**答案**：`Pattern::Str`，因为模式是一个字符串字面量 `"start"`。

**练习 2**：如果用户把 `self =>` 段和某条转入臂写反了顺序（先写转入臂再写 `self =>`），会发生什么？
**答案**：解析失败。`CastInput::parse` 在读完类型与逗号后，**先**尝试解析 `self =>`（`input.peek(syn::Token![self])`），再解析转入臂；且 `self =>` 必须以逗号结尾。若先出现转入臂，`Punctuated::parse_terminated` 会把后面的 `self => ...` 当成非法的转入臂语法而报错。

**练习 3**：为什么 `Pattern` 枚举上有一个 `#[expect(clippy::large_enum_variant)]`？
**答案**：两个变体 `Str(LitStr)` 与 `Ty(Pat, Type)` 体积差异较大（`syn::Type` 通常比 `LitStr` 大），clippy 默认会建议把大变体装箱（`Box`）。这里作者判断 `Pattern` 只是临时解析中间结构、生命周期短，刻意不装箱，所以显式压下该 lint。

---

### 4.2 三个 impl 的条件生成（cast::cast 入口与 dynamic 开关）

#### 4.2.1 概念说明

`cast!` 最多生成三个 impl：`Reflect`、`IntoValue`、`FromValue`。但它们**不是无条件全部生成**的——生成与否取决于 `CastInput` 的两个状态：

- 是否有转入臂（`from_value` 非空）。
- 是否写了 `type` 前缀（`dynamic == true`）。
- 是否写了 `self =>`（`into_value.is_some()`）。

`dynamic` 是这里的关键开关：当你在类型名前写 `type`（如 `cast! { type Stroke, ... }`），表示「这个类型自己也是一种可以被装进 `Value::Dyn` 的动态类型」。此时即使没有写 `self =>` 或转入臂，三个 impl 也会被**强制**生成，并补上针对 `Value::Dyn` 的判断逻辑。

#### 4.2.2 核心流程

`cast::cast` 的调度逻辑非常对称，可以用一张表概括三个 impl 的生成条件：

| 生成的 impl | 条件（满足其一即生成） | 不生成时意味着 |
|------------|----------------------|--------------|
| `Reflect`  | `!from_value.is_empty() \|\| dynamic` | 该类型不对外暴露「能转成什么」的元信息 |
| `IntoValue` | `into_value.is_some() \|\| dynamic` | 该类型没有「Rust→Value」的转出能力 |
| `FromValue` | `!from_value.is_empty() \|\| dynamic` | 该类型不能从 `Value` 转入 |

注意 `Reflect` 与 `FromValue` 共用同一条件，而 `IntoValue` 用的是「是否有 `self =>`」。这就是 `dynamic` 同时影响「四个方法」（更准确说是三个 impl 里若干方法）的体现：一旦 `dynamic == true`，三者全开。

#### 4.2.3 源码精读

**入口 `cast::cast`**：[src/cast.rs:L71-L123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L71-L123)。

先解析、再分别生成五个方法体：

```rust
pub fn cast(stream: TokenStream) -> Result<TokenStream> {
    let input: CastInput = syn::parse2(stream)?;
    let ty = &input.ty;
    let castable_body   = create_castable_body(&input);   // Reflect::castable
    let input_body      = create_input_body(&input);       // Reflect::input
    let output_body     = create_output_body(&input);      // Reflect::output
    let into_value_body = create_into_value_body(&input);  // IntoValue::into_value
    let from_value_body = create_from_value_body(&input);  // FromValue::from_value
    ...
}
```

然后三个 `.then(|| quote! { ... })` 各自带条件：

- **`reflect`**：[src/cast.rs:L80-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L80-L96)，条件 `(!input.from_value.is_empty() || input.dynamic)`，生成含 `input` / `output` / `castable` 三方法的 `impl Reflect`。
- **`into_value`**：[src/cast.rs:L98-L106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L98-L106)，条件 `(input.into_value.is_some() || input.dynamic)`，生成 `impl IntoValue`。
- **`from_value`**：[src/cast.rs:L108-L116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L108-L116)，条件 `(!input.from_value.is_empty() || input.dynamic)`，生成 `impl FromValue`。

最后把三者拼在一起返回：

```rust
Ok(quote! { #reflect #into_value #from_value })
```

**`dynamic` 在方法体里的作用**（先在这里点明，细节在 4.3 / 4.4）：当 `dynamic == true` 时，`create_castable_body` 与 `create_from_value_body` 会在最前面插入一段针对 `Value::Dyn` 的判断，`create_input_body` / `create_output_body` 会追加 `CastInfo::Type(Type::of::<Self>())`，`create_into_value_body` 在没有 `self =>` 时退化为 `Value::dynamic(self)`。这些都在后续模块逐一看。

#### 4.2.4 代码实践

**目标**：给定四种 `cast!` 写法，预测各自生成哪些 impl。

**操作步骤**：阅读下面四种写法，逐一判断 `ty` / `dynamic` / `into_value` / `from_value` 的取值，再套用 4.2.2 的条件表。

```rust ignore
// (a)
cast! { CoolInt, }

// (b)
cast! { CoolInt, self => self.0.into_value(), }

// (c)
cast! { type Stroke, thickness: Length => Self { .. }, }

// (d)
cast! { type Foo, }
```

**预期结果**：

| 写法 | dynamic | into_value | from_value | 生成的 impl |
|------|---------|-----------|-----------|-----------|
| (a) | false | None | 空 | **都不生成**（注意：这只是占位，实际无意义） |
| (b) | false | Some | 空 | 仅 `IntoValue` |
| (c) | true | None | 非空 | `Reflect` + `IntoValue` + `FromValue`（全开） |
| (d) | true | None | 空 | `Reflect` + `IntoValue` + `FromValue`（dynamic 全开） |

其中 (c) 正是 Typst 真实代码 [stroke.rs 中 `cast! { type Stroke, ... }`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/stroke.rs#L397-L414) 的形态——`Stroke` 是一个动态类型，既可从 `Length` / `Color` / `Gradient` 等转入，也能装回 `Value::Dyn`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Reflect` 与 `FromValue` 用同一个条件，而 `IntoValue` 用不同条件？
**答案**：`Reflect` 描述的「能从哪些值转来」与 `FromValue` 的「实际转换」是同一批信息来源（转入臂），所以二者绑定。而 `IntoValue` 是「转出」，信息来自 `self =>`，是独立的另一条线，故用 `into_value.is_some()`。

**练习 2**：一个类型如果只有 `IntoValue` 而没有 `Reflect` / `FromValue`，会有什么后果？
**答案**：它只能被转成 `Value`（例如作为函数返回值），但不能作为函数参数或字段类型被解析（因为缺少 `FromValue`），运行时的文档与错误提示里也不会出现它的「可接受输入」描述（因为缺少 `Reflect::input`）。

**练习 3**：`#[ty]` 宏（u2-l2）在不写 `cast` 标志时会自动补一句 `cast! { type #ident, }`。对照本节，它生成了哪些 impl？
**答案**：`cast! { type Foo, }` 即上面的 (d)，因为 `dynamic == true`，所以三个 impl 全部生成，且各方法体都走 `dynamic` 分支（`castable` 只判断 `Value::Dyn` 是否装着自己，`into_value` 退化为 `Value::dynamic(self)` 等）。这正是把一个纯 Rust 类型「注册」为动态类型的最小写法。

---

### 4.3 castable 与 from_value：判断与转换

#### 4.3.1 概念说明

`castable` 与 `from_value` 是一对容易混淆但分工明确的方法：

- **`castable(value: &Value) -> bool`**：只回答「**能不能**转」，返回布尔值，不真正转换。它存在主要是为了**性能**——见运行时注释 [Reflect::castable 的说明](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L40-L45)：比起走 `CastInfo` 做堆分配 + 动态判断，直接为每个类型生成优化过的机器码要快得多。
- **`from_value(value: Value) -> HintedStrResult<Self>`**：真正把 `Value` 转成本类型，失败则返回错误。

两者的**判断顺序完全一致**，这样能保证 `castable` 返回 `true` 的值，`from_value` 一定能成功转换。生成顺序是：

```
① dynamic 检查（Value::Dyn 装着自己）
② 字符串检查（所有 Pattern::Str 收集到一个 match）
③ 类型检查（各 Pattern::Ty 按声明顺序逐个判断）
④ 兜底（castable → false；from_value → Err）
```

#### 4.3.2 核心流程

伪代码描述两个方法体的骨架（以非 dynamic 为例）：

```
fn castable(value) -> bool:
    [若 dynamic] 若 value 是 Dyn 且装着 Self → return true
    [若有字符串臂] 若 value 是 Str 且匹配某个字符串字面量 → return true
    [对每个 Ty 臂，按声明顺序] 若 <Ty as Reflect>::castable(value) → return true
    return false

fn from_value(value) -> Result<Self>:
    [若 dynamic] 若 value 是 Dyn 且能 downcast 成 Self → return Ok(clone)
    [若有字符串臂] 若 value 是 Str，match 各字符串 → return Ok(对应 expr)
    [对每个 Ty 臂，按声明顺序] 若 <Ty>::castable(value):
        let 绑定 = <Ty as FromValue>::from_value(value)?;
        return Ok(对应 expr)
    return Err(<Self as Reflect>::error(&value))
```

关键观察：
- **类型臂的顺序 = 声明顺序**，所以两条 Ty 臂若能匹配同一种值，写在前面的会「赢」。
- **字符串臂被合并进一个 `match`**，所以字符串之间的顺序不影响（字面量互不相同）。
- `from_value` 里类型臂的转换用 `<Ty as FromValue>::from_value(value)?`，意味着转入臂的类型本身也可以是另一个 `cast!` 类型——**转换规则可递归组合**。

#### 4.3.3 源码精读

**`create_castable_body`**：[src/cast.rs:L194-L240](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L194-L240)。

```rust
for cast in &input.from_value {
    match &cast.pattern {
        Pattern::Str(lit) => {
            strings.push(quote! { #lit => return true });   // 收集字符串
        }
        Pattern::Ty(_, ty) => {
            casts.push(quote! {                              // 收集类型判断
                if <#ty as #foundations::Reflect>::castable(value) { return true; }
            });
        }
    }
}

let dynamic_check = input.dynamic.then(|| quote! { ... });    // Value::Dyn 判断
let str_check     = (!strings.is_empty()).then(|| quote! { ... }); // 字符串 match

quote! { #dynamic_check #str_check #(#casts)* false }
```

输出顺序就是 `dynamic → str → 各 casts → false`。

**`create_from_value_body`**：[src/cast.rs:L289-L337](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L289-L337)。

```rust
for cast in &input.from_value {
    let expr = &cast.expr;
    match &cast.pattern {
        Pattern::Str(lit) => {
            string_arms.push(quote! { #lit => return Ok(#expr) });
        }
        Pattern::Ty(binding, ty) => {
            cast_checks.push(quote! {
                if <#ty as #foundations::Reflect>::castable(&value) {
                    let #binding = <#ty as #foundations::FromValue>::from_value(value)?;
                    return Ok(#expr);
                }
            });
        }
    }
}
// 同样的 dynamic_check 与 str_check，顺序也是 dynamic → str → casts → Err
quote! {
    #dynamic_check
    #str_check
    #(#cast_checks)*
    Err(<Self as #foundations::Reflect>::error(&value))
}
```

注意类型臂里 `let #binding = <#ty as FromValue>::from_value(value)?;` —— 绑定名（如 `v`）就是把 `Pattern::Ty(pat, ty)` 里的 `pat` 喂给用户的 `expr`。这正是「`v: i64 => Self(v as u8)`」里 `v` 的来源。

两个方法体的「判断先后顺序」**完全相同**（dynamic → str → 按声明顺序的 ty → 兜底），这是设计上的刻意对齐。

#### 4.3.4 代码实践

**目标**：为 CoolInt 的 `from_value` 列出判断先后顺序（这是本讲主实践的第三问，先在这里聚焦）。

**操作步骤**：回到 4.1.1 的 CoolInt 范例，它有两条 `Pattern::Ty` 臂（`bool`、`i64`），无字符串臂、非 dynamic。套用 4.3.2 的骨架。

**预期结果（from_value 方法体的判断顺序）**：

```
1. [dynamic 检查] 跳过（dynamic == false）
2. [字符串检查]  跳过（没有 Pattern::Str）
3. [类型臂 1]    if <bool as Reflect>::castable(&value) {
                     let v = <bool as FromValue>::from_value(value)?;
                     return Ok(Self(v as u8));
                 }
4. [类型臂 2]    if <i64 as Reflect>::castable(&value) {
                     let v = <i64 as FromValue>::from_value(value)?;
                     return Ok(if matches!(v, 0..=13) { Self(v as u8) }
                               else { bail!("integer is not nice :/") });
                 }
5. [兜底]        Err(<CoolInt as Reflect>::error(&value))
```

所以一个传入的 `Value::Bool(true)` 会在第 3 步命中，`Value::Int(5)` 会在第 4 步命中，`Value::Str("x")` 则一路落到第 5 步报错。`castable` 方法体的顺序与之相同，只是每个分支换成 `return true`、兜底换成 `false`。

> 待本地验证：如需亲眼确认展开结果，可在 typst-library 下对包含该 `cast!` 的文件运行 `cargo expand`（需安装 `cargo-expand`）查看生成的 `impl FromValue`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 CoolInt 的两条臂对调成 `v: i64` 在前、`v: bool` 在后，对转换结果有影响吗？
**答案**：没有。`Value::Bool` 与 `Value::Int` 是不同的变体，`<bool as Reflect>::castable` 与 `<i64 as Reflect>::castable` 互斥，所以无论顺序如何，布尔值只命中 bool 臂、整数只命中 i64 臂。**只有当两条 Ty 臂能匹配同一种值时，顺序才决定谁赢。**

**练习 2**：`from_value` 里类型臂为什么写成 `let #binding = <#ty as FromValue>::from_value(value)?;` 而不是直接用 `value`？
**答案**：因为转入臂的类型本身可能又是另一个 `cast!` 类型，需要调用那个类型的 `FromValue` 实现，递归地把 `value` 转成 `#ty`，再把结果绑定给用户表达式里的 `#binding`。`?` 则把转换失败冒泡成 `HintedStrResult` 的错误。

**练习 3**：`castable` 与 `from_value` 都把兜底分别写成 `false` 与 `Err(...)`。如果二者顺序不一致会发生什么？
**答案**：会出现「`castable` 说能转、`from_value` 却转失败」或相反的不一致，违反 `Reflect::castable` 作为 `FromValue` 廉价预判的契约。所以宏刻意让两者用同一套 `dynamic → str → ty → 兜底` 的顺序生成。

---

### 4.4 input / output / into_value：元信息与转出

#### 4.4.1 概念说明

剩下的三个方法各司其职：

- **`Reflect::input() -> CastInfo`**：描述「**哪些值能转成本类型**」，用于自动补全、文档、错误信息（如「expected ..., found ...」）。每个转入臂贡献一条 `CastInfo`。
- **`Reflect::output() -> CastInfo`**：描述「**本类型能转成什么**」。非 dynamic 时它直接复用 `input()`。
- **`IntoValue::into_value(self) -> Value`**：转出。有 `self =>` 用用户表达式；dynamic 且无 `self =>` 时退化为 `Value::dynamic(self)`（把自己装进动态值）。

`CastInfo` 是运行时的「类型说明」枚举，`cast!` 里只用到它的两个变体：`CastInfo::Value(value, docs)`（一个具体的字面量候选 + 文档）和 `CastInfo::Type(Type::of::<T>())`（一个类型候选）。

#### 4.4.2 核心流程

```
input()  = 各转入臂的 CastInfo 串联   + [若 dynamic] CastInfo::Type(Self)
output() = [若 dynamic] CastInfo::Type(Self)   否则  <Self as Reflect>::input()
into_value(self) = [若有 self =>] 用户 expr    否则  Value::dynamic(self)
```

注意 `output` 的「否则」分支是 `<Self as Reflect>::input()`——也就是 **output 直接复用 input 的描述**。为什么可以？因为大多数非 dynamic 类型的「能转成什么」和「能从什么转来」是同一组候选（比如 `i64` 的 input 和 output 都是「integer」）。

#### 4.4.3 源码精读

**`create_input_body`**：[src/cast.rs:L242-L271](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L242-L271)。

```rust
for cast in &input.from_value {
    let docs = documentation(&cast.attrs);          // 臂上的 #[doc]
    infos.push(match &cast.pattern {
        Pattern::Str(lit) => quote! {
            #foundations::CastInfo::Value(
                #foundations::IntoValue::into_value(#lit),
                #docs,
            )
        }
        Pattern::Ty(_, ty) => quote! { <#ty as #foundations::Reflect>::input() },
    });
}
if input.dynamic {
    infos.push(quote! { #foundations::CastInfo::Type(#foundations::Type::of::<Self>()) });
}
quote! { #(#infos)+* }
```

两种臂的差异很清楚：字符串臂生成 `CastInfo::Value`（候选值就是那个字符串本身，附文档），类型臂则**委托**给目标类型的 `Reflect::input`（递归）。最后的 `#(#infos)+*` 把多个 `CastInfo` 用 `+` 连接——`CastInfo` 支持 `Add`，会把多个候选合并成一个集合描述。

**`create_output_body`**：[src/cast.rs:L273-L279](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L273-L279)。

```rust
if input.dynamic {
    quote! { #foundations::CastInfo::Type(#foundations::Type::of::<Self>()) }
} else {
    quote! { <Self as #foundations::Reflect>::input() }   // 复用 input
}
```

**`create_into_value_body`**：[src/cast.rs:L281-L287](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L281-L287)。

```rust
if let Some(expr) = &input.into_value {
    quote! { #expr }                              // 用 self => expr
} else {
    quote! { #foundations::Value::dynamic(self) } // dynamic 退化
}
```

注意：当没有 `self =>` 但 `dynamic == true` 时，`IntoValue` 仍会被生成（见 4.2），但方法体走这个 `else`，把 `self` 装进 `Value::Dyn`。

#### 4.4.4 代码实践

**目标**：阅读真实代码 [stroke.rs 的 `cast! { type Stroke, ... }`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/stroke.rs#L397-L414)，预测它的 `input()` 会生成哪些 `CastInfo`。

**操作步骤**：

1. 打开上面的 stroke.rs 链接，它有四条 `Pattern::Ty` 臂（`Length`、`Color`、`Gradient`、`Tiling`）以及后面一条 `mut dict: Dict` 臂，并且是 `dynamic`。
2. 套用 `create_input_body` 的逻辑：每条 Ty 臂 → `<Ty as Reflect>::input()`，再因 dynamic 追加一条 `CastInfo::Type(Type::of::<Stroke>())`。

**预期结果（input 方法体）**：

```rust ignore
<Length as Reflect>::input()
+ <Color as Reflect>::input()
+ <Gradient as Reflect>::input()
+ <Tiling as Reflect>::input()
+ <Dict as Reflect>::input()
+ CastInfo::Type(Type::of::<Self>())
```

所以在自动补全里，`stroke` 字段会提示「length、color、gradient、tiling、dictionary、stroke 类型」这一组候选。这就是 `cast!` 的转入臂如何反过来变成用户可见的文档/补全信息。

#### 4.4.5 小练习与答案

**练习 1**：`create_output_body` 在非 dynamic 时返回 `<Self as Reflect>::input()`。如果某类型的 input 与 output 其实不同，这个简化会出错吗？
**答案**：会损失精度（output 描述不够准确），但不会编译失败。`cast!` 选择「绝大多数情况下 input ≈ output」这一假设来简化生成；少数需要精确 output 的类型会通过其它途径（或被设计成 dynamic）处理。这是「常见情况优化、罕见情况另行处理」的典型取舍。

**练习 2**：字符串臂在 `input` 里为什么用 `IntoValue::into_value(#lit)` 而不是直接写 `Value::Str(#lit)`？
**答案**：写 `IntoValue::into_value(#lit)` 让生成代码与运行时的「字符串→Value」约定解耦——`&str` / `String` / `Str` 的 `into_value` 实现可能不同，统一走 trait 方法更稳健，也保持与 `cast!` 其它分支一致的风格。

**练习 3**：CoolInt 的 `into_value` 方法体是什么？
**答案**：CoolInt 写了 `self => self.0.into_value()`，所以 `create_into_value_body` 走 `if let Some(expr)` 分支，方法体就是 `self.0.into_value()`——即取出内部的 `u8`，再由 `u8` 的 `IntoValue` 转成 `Value::Int`。

---

## 5. 综合实践

把本讲主实践完整做一遍。这是检验你是否真正掌握 `cast!` 的综合任务。

### 5.1 任务

给定类型：

```rust ignore
/// 一个 0 到 13 之间的整数。
struct CoolInt(u8);
```

请完成三件事：

1. **写一个 `cast!` 调用**，要求包含：
   - 一条转出臂 `self => self.0.into_value()`；
   - 一条 `v: i64 => …` 转入臂（自行决定如何把 `i64` 变成 `CoolInt`）；
   - 一条 `v: bool => …` 转入臂。

2. **预测宏会生成哪些 impl**（哪些 trait、哪些方法）。

3. **列出 `FromValue::from_value` 方法体里的判断先后顺序**。

### 5.2 参考答案

**第 1 问**（一种可行写法，与官方文档示例等价）：

```rust ignore
cast! {
    CoolInt,
    self => self.0.into_value(),
    v: i64 => if matches!(v, 0..=13) {
        Self(v as u8)
    } else {
        bail!("integer is not nice :/")
    },
    v: bool => Self(v as u8),
}
```

> 这里故意把 `i64` 臂写在 `bool` 前，用来观察顺序的影响。

**第 2 问（生成的 impl）**：

- `dynamic == false`（无 `type` 前缀），`into_value.is_some() == true`（有 `self =>`），`from_value` 非空。
- 套用 4.2 的条件表：
  - `Reflect`：生成（from_value 非空），含 `input` / `output` / `castable`。
  - `IntoValue`：生成（有 `self =>`），`into_value` 方法体为 `self.0.into_value()`。
  - `FromValue`：生成（from_value 非空），方法体见第 3 问。

**第 3 问（from_value 判断顺序）**：

```
1. [dynamic]  跳过
2. [字符串]   跳过（无 Pattern::Str）
3. [Ty 臂: i64]  if <i64 as Reflect>::castable(&value) {
                     let v = <i64 as FromValue>::from_value(value)?;
                     return Ok(if matches!(v, 0..=13) { Self(v as u8) }
                               else { bail!("integer is not nice :/") });
                 }
4. [Ty 臂: bool] if <bool as Reflect>::castable(&value) {
                     let v = <bool as FromValue>::from_value(value)?;
                     return Ok(Self(v as u8));
                 }
5. [兜底]     Err(<CoolInt as Reflect>::error(&value))
```

因为 `i64` 写在 `bool` 前，所以对 `Value::Int(7)` 会先在第 3 步命中；对 `Value::Bool(true)`，第 3 步 `<i64>::castable` 为 false，落到第 4 步命中。两者互斥，故顺序不影响最终归属，但**判断的先后**严格遵循声明顺序。

### 5.3 验证方法（源码阅读型）

本 crate 自身没有针对 `cast!` 展开结果的单元测试，验证依赖下游 `typst-library` 的使用。你可以：

1. 在 `typst-library` 中找一个用了 `cast!` 的类型（如 `visualize/stroke.rs` 的 `Stroke`、`visualize/color.rs` 的 `Color`），阅读它如何被当作函数参数或字段类型使用，确认转换确实按上述顺序发生。
2. 若本地装了 `cargo-expand`，对包含 `cast!` 的目标运行 `cargo expand`，肉眼比对生成的 `impl Reflect / IntoValue / FromValue` 与本讲的预测是否一致。

---

## 6. 本讲小结

- `cast!` 是函数式过程宏，输入分三段：目标类型（可带 `type` 前缀）、可选的 `self =>` 转出、若干 `pattern => expr` 转入臂；解析成 [`CastInput`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L125-L131)。
- 转入臂有两种模式：[`Pattern::Str`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L188-L192)（字符串字面量匹配）与 `Pattern::Ty`（`绑定: 类型` 匹配，递归委托）。
- `type` 前缀置 `dynamic = true`，**同时**打开 `Reflect` / `IntoValue` / `FromValue` 三个 impl 的生成开关（见 [`cast::cast`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L71-L123) 的三个 `.then` 条件）。
- `castable`（廉价布尔判断）与 `from_value`（真正转换）共用同一套判断顺序：`dynamic → 字符串 → 各类型臂（按声明顺序）→ 兜底`。
- `input` 把每条转入臂翻译成 `CastInfo` 候选（字符串→`CastInfo::Value`，类型→委托 `Reflect::input`），从而驱动自动补全与错误信息；`output` 在非 dynamic 时直接复用 `input`。
- `#[ty]`（u2-l2）自动补的 `cast! { type #ident, }` 正是本讲 (d) 形态，是「把纯 Rust 类型注册为动态类型」的最小写法。

## 7. 下一步学习建议

- **继续本单元**：阅读 [u2-l4 `#[derive(Cast)]`](u2-l4-derive-cast.md)，看 `derive_cast` 如何只支持枚举、把变体名转成 kebab-case 字符串，再**委托给本讲的 `cast!`** 生成——你会看到 `cast!` 作为更底层原语被复用。
- **跨单元**：进入 u3-l1 `#[func]`（参数解析）时，注意函数参数的类型必须实现 `FromValue`，而函数返回类型必须实现 `IntoResult`/`IntoValue`——本讲正是这些约束的来源。
- **源码延伸**：在 `typst-library` 里挑一个使用了 `cast! { type ..., }` 的类型（推荐 [stroke.rs 的 `Stroke`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/visualize/stroke.rs#L397-L414)、`color.rs` 的 `Color`），把它的 `cast!` 当作练习题，逐臂预测它生成的 `castable` / `from_value` / `input` 方法体，再与 `cargo expand` 的结果对照。
