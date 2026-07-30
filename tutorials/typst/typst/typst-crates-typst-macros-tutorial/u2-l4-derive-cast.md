# #[derive(Cast)]：枚举的字符串转换

## 1. 本讲目标

学完本讲，你应当能够：

- 说出**派生宏** `#[derive(Cast)]` 与**函数式宏** `cast!` 在签名、参数、以及对原代码项处理方式上的三点差异，并解释为什么 `derive_cast` 把真正的转换逻辑「外包」给 `cast!`。
- 描述 `derive_cast` 如何遍历枚举变体、把每个变体名转成 kebab-case 字符串（可用 `#[string]` 覆盖），并把结果存进中间结构 `Variant`。
- 手写 `derive_cast` 为一个具体枚举最终展开出的**等价 `cast!` 代码**，指出其中 `strs_to_variants`（字符串→变体）与 `variants_to_strs`（变体→字符串）两个方向各自的去向。
- 解释为什么带显式 discriminant（如 `Nice = 1`）的变体会被 `bail!` 拒绝。

本讲是「类型与值转换」这条线的收尾：u2-l3 讲了底层原语 `cast!`，本讲告诉你 `#[derive(Cast)]` 并没有重新实现一套转换逻辑，而是**自动生成一段 `cast!` 调用**，把工作交回去给 `cast!`——你会看到 `cast!` 作为更底层原语被复用的典型范式。

## 2. 前置知识

### 2.1 先回忆 cast! 的三段语法

本讲完全建立在 u2-l3 之上。`cast!` 的输入分三段：

```rust ignore
cast! {
    目标类型,                       // ①（可带 type 前缀）
    self => 转出表达式,             // ② 生成 IntoValue（可省）
    "字符串字面量" => Self::Variant // ③ 若干转入臂，生成 FromValue / Reflect（可省）
    v: 类型 => 表达式,
}
```

其中第三段里以字符串字面量开头的臂，对应 u2-l3 讲过的 [`Pattern::Str`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L188-L192)：它在 `castable` / `from_value` 里会被收进一个针对 `Value::Str` 的 `match`，在 `input` 里会变成一条 `CastInfo::Value(那个字符串, 文档)` 候选。

`#[derive(Cast)]` 的全部产物，就是**自动拼出上面这种以 `Pattern::Str` 为主的 `cast!` 调用**。理解了这一点，本讲的源码就读完了一大半。

### 2.2 派生宏 vs 函数式宏：一个直觉

这是本讲要建立的第一个新认知。先给结论：

| 维度 | `cast!`（函数式宏） | `#[derive(Cast)]`（派生宏） |
|------|--------------------|-----------------------------|
| 标注 | `#[proc_macro]` | `#[proc_macro_derive(Cast, attributes(string))]` |
| 参数 | 单个 `stream`（宏体） | 单个 `item`（被派生的那个项，解析成 `DeriveInput`） |
| 语法 | 完全自定义的 DSL | 标准 Rust 的 `enum` 定义 |
| 原项去留 | 宏体被**消费**，整体替换为展开结果 | 原项**原样保留**，宏只在其后**追加** impl |

最后一行最关键：**派生宏永远不删你写的那个枚举**，它只是在枚举后面「贴」上若干 `impl`。这意味着你在枚举上挂的其它属性（如 `#[derive(Debug)]`、`#[default]`、`#[serde(...)]`）都不会被 `derive_cast` 吃掉，它会原封不动地留在原地。

### 2.3 一个真实场景

Typst 里大量「字符串开关型」类型都用 `#[derive(Cast)]`。例如字体样式：

```rust ignore
/// The style of a font.
#[derive(Debug, Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
#[derive(Cast, Serialize, Deserialize)]
pub enum FontStyle {
    /// The default, typically upright style.
    #[default]
    Normal,
    /// A cursive style with custom letterform.
    Italic,
    /// Just a slanted version of the normal style.
    Oblique,
}
```

见真实代码 [variant.rs 的 FontStyle](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L36-L47)。加了 `#[derive(Cast)]` 后，Typst 用户就能写 `font(style: "italic")`——字符串 `"italic"` 会被转成 `FontStyle::Italic`，反过来 `FontStyle::Italic` 也会显示成 `"italic"`。本讲讲的就是这条字符串↔变体的双向映射是怎么生成的。

## 3. 本讲源码地图

本讲围绕 `cast.rs` 顶部的 `derive_cast` 函数（它与 `cast!` 同处一个文件），辅以 `lib.rs` 入口与 `util.rs` 工具：

| 文件 | 作用 |
|------|------|
| `src/cast.rs` | `derive_cast`（遍历枚举、生成等价 `cast!`）、`Variant` 中间结构、`strs_to_variants` / `variants_to_strs` 两个方向的生成。`cast!` 本身也在这个文件里（被生成的代码所调用）。 |
| `src/lib.rs` | `#[proc_macro_derive(Cast, attributes(string))] pub fn derive_cast` 入口，以及库文档里的 `Niceness` 范例。 |
| `src/util.rs` | `documentation()` 抽取变体文档、`foundations` 路径简写、`bail!` 宏。 |

本讲按三个最小模块展开：

1. **4.1** `derive_cast` 入口：派生宏特性与「只支持枚举」的硬约束
2. **4.2** `Variant` 中间结构：变体↔字符串映射与校验（kebab-case、`#[string]` 覆盖、拒绝显式 discriminant）
3. **4.3** 双向生成：`strs_to_variants` / `variants_to_strs` 与委托 `cast!`

---

## 4. 核心概念与源码讲解

### 4.1 derive_cast 入口：派生宏特性与「只支持枚举」

#### 4.1.1 概念说明

`#[derive(Cast)]` 的入口签名与 u2-l3 的 `cast!` 截然不同。它是一个**派生宏**，标注见 [lib.rs 的 derive_cast 入口](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L322-L328)：

```rust ignore
#[proc_macro_derive(Cast, attributes(string))]
pub fn derive_cast(item: BoundaryStream) -> BoundaryStream {
    let item = syn::parse_macro_input!(item as DeriveInput);
    cast::derive_cast(item)
        .unwrap_or_else(|err| err.to_compile_error())
        .into()
}
```

注意三点：

1. **`#[proc_macro_derive(Cast, attributes(string))]`**：这是派生宏专属标注。`Cast` 是派生出的「派生名」（写在 `#[derive(Cast)]` 里）；`attributes(string)` 声明了 `string` 是一个**派生辅助属性**（derive helper attribute）。没有这一句，编译器会在 `#[string("nfc")]` 上报「unknown attribute」错误——所以 `attributes(string)` 是允许用户在变体上写 `#[string(...)]` 的「许可证」。
2. **单参数 `item`**：派生宏只接收被装饰的那个项，没有 `stream`。入口把它解析成 [`syn::DeriveInput`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L15)——一个能表示 struct / enum / union 的通用结构。
3. **错误回传**：与其它六个宏完全一致，`unwrap_or_else(|err| err.to_compile_error())`，绝不 panic。这条骨架已在 u1-l2 建立。

#### 4.1.2 核心流程

`derive_cast` 的流程很直白：

```
DeriveInput
   │  若 data 不是 Enum → bail!("only enums are supported")
   ▼
遍历 data.variants，逐个构造 Variant { ident, string, docs }
   │  若某变体有显式 discriminant → bail!("explicit discriminant is not allowed")
   ▼
用同一批 Variant，分别生成两个方向的代码片段：
   strs_to_variants   （字符串 => 变体，喂给 from_value）
   variants_to_strs   （变体 => 字符串，喂给 into_value 的 match）
   ▼
quote! 出一个 cast! { … } 调用 ← 真正干活的是它
```

最关键的认识：`derive_cast` **自己不生成 `Reflect` / `IntoValue` / `FromValue` 任何一个 impl**。它只生成一段 `cast!` 调用，然后把活儿交给 u2-l3 讲的 `cast!`。这是一次「宏生成宏调用」的嵌套展开。

#### 4.1.3 源码精读

`derive_cast` 的开头就拒绝了非枚举：[src/cast.rs:L11-L16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L11-L16)。

```rust
pub fn derive_cast(item: DeriveInput) -> Result<TokenStream> {
    let ty = &item.ident;

    let syn::Data::Enum(data) = &item.data else {
        bail!(item, "only enums are supported");
    };
    ...
}
```

`let syn::Data::Enum(data) = &item.data else { bail!(...) }` 是 Rust 的 let-else 模式：只有当 `item.data` 是 `Enum` 时才绑定成功；若是 `Struct` 或 `Union`，立即 `bail!` 报错并提前返回。

`ty = &item.ident` 就是枚举的名字（如 `FontStyle`），后面会作为 `cast!` 的目标类型。

> 为什么只支持枚举？因为字符串↔变体的映射只有在「有若干离散变体」时才有意义。struct 的字段是「数据」而非「选项」，没有对应的字符串表示；union 更不适用。所以这套转换语义天然只对 enum 成立。

#### 4.1.4 代码实践

**目标**：亲手验证「派生宏保留原项、只追加 impl」这一特性。

**操作步骤**：

1. 打开真实代码 [FontStyle](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L36-L47)，它同时挂了 `#[derive(Debug, Default, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]`、`#[derive(Serialize, Deserialize)]`、`#[serde(rename_all = "kebab-case")]`，以及变体上的 `#[default]`。
2. 想象 `#[derive(Cast)]` 展开后：原枚举和这些属性**全部保留**，只是在文件作用域里多出几个 `impl FontStyle { ... }`（由生成的 `cast!` 再展开而来）。

**需要观察的现象（预期结果）**：`#[default]`（属于 `derive(Default)`）和 `#[serde(...)]` 依然各自工作，不受 `Cast` 影响。它们能共存，正是因为派生宏互不干扰、且 `derive_cast` 对 `string` 以外的属性**视而不见**（它既不消费也不校验它们，只读取 `string`）。

> 待本地验证：若装了 `cargo-expand`，对 `variant.rs` 所在 crate 运行 `cargo expand FontStyle`，可看到 `#[derive(Cast)]` 最终「贴」上的 `impl Reflect / IntoValue / FromValue for FontStyle`，而原 `enum FontStyle` 与其它 derive 的产物原样在列。

#### 4.1.5 小练习与答案

**练习 1**：如果在一个 struct 上写 `#[derive(Cast)]`，会发生什么？错误指向哪里？
**答案**：编译期报错 `typst: only enums are supported`。错误 span 指向整个 `item`（即那个 struct 的定义处），因为代码用的是 `bail!(item, ...)`，而 `item` 是整个 `DeriveInput`。

**练习 2**：为什么入口签名必须写成 `#[proc_macro_derive(Cast, attributes(string))]`，把 `attributes(string)` 也带上？
**答案**：`attributes(string)` 把 `string` 注册为该派生宏的辅助属性。否则编译器在解析 `#[string("nfc")]` 时会认为它是一个未知的非法属性而报错，宏根本拿不到执行机会。

**练习 3**：`derive_cast` 入口把 `item` 解析成 `DeriveInput`，而 `cast!` 入口把 `stream` 用 `syn::parse2::<CastInput>` 解析。这两种「解析目标」反映了什么差异？
**答案**：`DeriveInput` 是 syn 对**标准 Rust 项**的通用表示——派生宏只能用在合法的 Rust 项上，所以解析目标也是标准的。`CastInput` 则是 `cast!` **自定义的 DSL 语法树**——函数式宏的宏体不需要是合法 Rust，所以解析目标也是宏私有的。一个「服从 Rust 语法」，一个「发明自己的语法」。

---

### 4.2 Variant 中间结构：变体↔字符串映射与校验

#### 4.2.1 概念说明

`derive_cast` 的核心工作是把每个枚举变体映射成一个字符串。映射规则很简单：

- **默认**：变体名转成 kebab-case。如 `NotNice` → `"not-nice"`，`Normal` → `"normal"`。
- **覆盖**：变体上写 `#[string("xxx")]`，则用 `xxx` 作为该变体的字符串。如 `#[string("nfc")] Nfc` → `"nfc"`。

这条规则体现了 Typst 一贯的命名约定：用户侧看到的字符串是 kebab-case（小写、连字符分隔），与 Rust 侧的 PascalCase 变体名对应。`#[string]` 则用于那些不符合 kebab 规则、或有特殊拼写的情况（如 Unicode 形态名 `"nfc"`、纯符号 `"❌"`）。

转换的同时，每个变体还要带上它的**文档注释**——这些 doc 会变成该字符串候选在自动补全 / 错误提示里的说明文字。

这些信息被收进一个中间结构 `Variant`：[src/cast.rs:L64-L68](https://github.com/typst/typst/blob/146a58329a30f6cf38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L64-L68)。

```rust
struct Variant {
    ident: Ident,      // 变体名，如 NotNice
    string: String,    // 对应字符串，如 "not-nice"
    docs: String,      // 该变体的文档注释
}
```

`Variant` 是 `derive_cast` 私有的中间表示（注意它没有被 `pub`）。收集完所有变体后，再用它分别生成两个方向的代码。这样「遍历一次、生成两次」，避免重复解析。

#### 4.2.2 核心流程

遍历变体的伪代码：

```
for variant in data.variants:
    若 variant.discriminant 存在（即写了 = 数字）→ bail!(expr, "explicit discriminant ...")

    string = 若变体上有 #[string] 属性:
                 解析其参数为 LitStr，取 .value()
             否则:
                 variant.ident.to_string().to_kebab_case()

    docs = documentation(&variant.attrs)   // 拼接变体的 /// 文档

    variants.push(Variant { ident, string, docs })
```

两个值得注意的细节：

- **`#[string]` 用 `find` 而非 `take_attr`**：只查找、不移除。这无所谓，因为派生宏本来就不把原项吐回去修改，原变体连同其属性都由编译器保留。
- **kebab-case 用 `heck` 的 `ToKebabCase`**：`NotNice` → `not-nice`。这与 u1-l3 讲过的 `determine_name_and_title` 用的是同一个 `heck` crate，只是这里直接调 `to_kebab_case()` 而非走 `determine_name_and_title`（因为变体不需要 title、也不需要冗余校验）。

#### 4.2.3 源码精读

遍历与构造 `Variant` 的完整逻辑：[src/cast.rs:L18-L37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L18-L37)。

```rust
let mut variants = vec![];
for variant in &data.variants {
    if let Some((_, expr)) = &variant.discriminant {
        bail!(expr, "explicit discriminant is not allowed");
    }

    let string = if let Some(attr) =
        variant.attrs.iter().find(|attr| attr.path().is_ident("string"))
    {
        attr.parse_args::<syn::LitStr>()?.value()
    } else {
        variant.ident.to_string().to_kebab_case()
    };

    variants.push(Variant {
        ident: variant.ident.clone(),
        string,
        docs: documentation(&variant.attrs),
    });
}
```

逐句看：

- **`variant.discriminant`**：syn 里枚举变体的 `discriminant` 字段是 `Option<(Eq, Expr)>`。写成 `Nice = 1` 时它就是 `Some`。`derive_cast` 用 `if let Some((_, expr))` 捕获到那个表达式 `expr`，并以 `expr` 为 span 报错——这样错误光标会精确指在 `1` 上，而不是整个变体。
- **`#[string]` 查找**：`variant.attrs.iter().find(|attr| attr.path().is_ident("string"))` 在变体的属性里找名为 `string` 的属性。找到则 `attr.parse_args::<syn::LitStr>()?` 把括号里的内容解析成一个**字符串字面量**（`LitStr`）并取 `.value()`。这要求 `#[string(...)]` 里必须是一个字符串字面量——写 `#[string(123)]` 会在这里解析失败。
- **默认 kebab-case**：找不到 `#[string]` 时，`variant.ident.to_string().to_kebab_case()`。`to_string()` 把 `Ident` 变成字符串 `"NotNice"`，`to_kebab_case()` 转成 `"not-nice"`。
- **文档**：`documentation(&variant.attrs)` 复用 u1-l3 / u2-l3 讲过的 [util.rs documentation()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L26-L59)，把变体的 `///` 注释拼成一段字符串。

#### 4.2.4 代码实践

**目标**：用真实枚举验证 kebab-case 与 `#[string]` 覆盖两条规则。

**操作步骤**：

1. 打开 [str.rs 的 UnicodeNormalForm](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L900-L917)，它四个变体都挂了 `#[string("nfc")]` / `#[string("nfd")]` / `#[string("nfkc")]` / `#[string("nfkd")`。
2. 对每个变体套用 4.2.2 的规则，预测 `Variant.string`。

**预期结果**：

| 变体 ident | 是否有 `#[string]` | `string` 取值 |
|-----------|-------------------|--------------|
| `Nfc`  | 有 `"nfc"`  | `"nfc"` |
| `Nfd`  | 有 `"nfd"`  | `"nfd"` |
| `Nfkc` | 有 `"nfkc"` | `"nfkc"` |
| `Nfkd` | 有 `"nfkd"` | `"nfkd"` |

注意：即使不写 `#[string]`，`Nfc.to_kebab_case()` 恰好也是 `"nfc"`。所以这里写 `#[string]` 更多是**显式声明意图**，避免依赖 kebab 规则的隐式行为（也方便读者一眼看出对外的字符串名）。

> 对比 [Case 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/case.rs#L60-L67)：`Lower` / `Upper` 都没写 `#[string]`，于是走默认分支，`string` 分别为 `"lower"` / `"upper"`。

#### 4.2.5 小练习与答案

**练习 1**：变体名 `SuperScript`（无 `#[string]`）会得到什么字符串？
**答案**：`"super-script"`。`"SuperScript".to_kebab_case()` 在大写边界处插入连字符并转小写，得到 `super-script`。

**练习 2**：为什么 `#[string]` 的参数被解析成 `syn::LitStr`（字符串字面量），而不是通用的 `syn::Expr`？
**答案**：因为变体对应的对外字符串必须是一个编译期已知的确定字面量——它要直接出现在生成的 `match` 臂和 `CastInfo::Value` 候选里。如果允许任意表达式，就无法在编译期确定匹配模式，也无法生成静态的文档候选。所以用 `LitStr` 在解析阶段就强制约束为字符串字面量，写错立即报错。

**练习 3**：`derive_cast` 既不调用 `validate_attrs` 也不调用 `take_attr`。如果用户在变体上写了一个拼错的属性 `#[strnig("x")]`，会怎样？
**答案**：`derive_cast` 不会报错——它只 `find` 名为 `string` 的属性，找不到就当默认 kebab 处理。拼错的 `#[strnig]` 既不被本宏识别（不影响转换），也不被消费。但由于它是写在变体上的「裸」属性，编译器多半会另行报「cannot find attribute `strnig` in this scope」之类的错误。这与 u1-l3 讲的 `validate_attrs` 主动校验残留属性是不同策略——`derive_cast` 选择了「不校验、只读 `string`」。

---

### 4.3 双向生成：strs_to_variants / variants_to_strs 与委托 cast!

#### 4.3.1 概念说明

收集完 `variants` 后，`derive_cast` 用**同一批 `Variant`** 生成两个方向的代码片段，再拼进一个 `cast!` 调用：

- **`strs_to_variants`**（字符串 → 变体）：每条形如 `"nice" => Self::Nice`，带 `#[doc]`。这些会成为 `cast!` 的**转入臂**（`Pattern::Str`），驱动 `FromValue`（把 `Value::Str("nice")` 转成 `Self::Nice`）和 `Reflect::input`（自动补全里列出 `"nice"` 候选）。
- **`variants_to_strs`**（变体 → 字符串）：每条形如 `Niceness::Nice => "nice"`。这些会成为 `cast!` 的 **`self =>` 转出表达式**里 `match self` 的臂，驱动 `IntoValue`（把 `Self::Nice` 转成 `Value::Str("nice")`）。

两个方向共用一份映射表（`variants`），保证「正向和逆向一致」——能从 `"nice"` 解析进来的变体，`into_value` 时一定也吐出 `"nice"`。这是双向转换正确性的根本保证。

#### 4.3.2 核心流程

最终的 `quote!` 模板（简化）：

```
::typst_library::foundations::cast! {
    <枚举名>,
    self => ::typst_library::foundations::IntoValue::into_value(match self {
        <变体1 => 字符串1>,   // variants_to_strs，逗号分隔
        <变体2 => 字符串2>,
        ...
    }),
    <#[doc> 字符串1 => Self::变体1>  // strs_to_variants
    <#[doc> 字符串2 => Self::变体2>
    ...
}
```

要点：

- 转出臂 `self => ...` 里，`match self` 把枚举值匹配到一个 `&'static str`（字面量字符串），再交给 [`IntoValue::into_value`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L100-L106)（来自运行时）转成 `Value::Str`。这正好对应 u2-l3 讲的 `create_into_value_body` 走「有 `self =>`」分支。
- 转入臂都是 `Pattern::Str`（字符串字面量开头），所以 u2-l3 讲的 `create_castable_body` / `create_from_value_body` 都会把它们收进针对 `Value::Str` 的 `match`，`create_input_body` 会把每条变成 `CastInfo::Value(字符串, 文档)`。
- 每条转入臂上的 `#[doc = #docs]` 来自该变体的文档，最终成为自动补全里那个字符串候选的说明。

#### 4.3.3 源码精读

**生成 `strs_to_variants`（转入臂）**：[src/cast.rs:L39-L44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L39-L44)。

```rust
let strs_to_variants = variants.iter().map(|Variant { ident, string, docs }| {
    quote! {
        #[doc = #docs]
        #string => Self::#ident
    }
});
```

每条是 `#[doc = "..."]  "nice" => Self::Nice`。这正是 `cast!` 转入臂 `Pattern::Str` 的形态（u2-l3 的 `Cast::parse` 会先 `parse_outer` 读到这个 `#[doc]`，再解析字符串字面量为 `Pattern::Str`）。

**生成 `variants_to_strs（转出臂里的 match）`**：[src/cast.rs:L46-L50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L46-L50)。

```rust
let variants_to_strs = variants.iter().map(|Variant { ident, string, .. }| {
    quote! {
        #ty::#ident => #string
    }
});
```

每条是 `Niceness::Nice => "nice"`，是 `match self` 的一个臂。注意这里用 `#ty::#ident`（完整路径 `枚举名::变体名`），因为转出臂里 `self` 的类型就是该枚举；而转入臂里用 `Self::#ident`（因为 `cast!` 生成的 `impl` 里 `Self` 就是该枚举）。

**最终拼装**：[src/cast.rs:L52-L60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L52-L60)。

```rust
Ok(quote! {
    #foundations::cast! {
        #ty,
        self => #foundations::IntoValue::into_value(match self {
            #(#variants_to_strs),*
        }),
        #(#strs_to_variants)*
    }
})
```

- `#foundations::cast!`：`foundations` 是 u1-l3 讲过的路径简写，展开为 `::typst_library::foundations`。所以这里写的是绝对路径 `::typst_library::foundations::cast!`，避免与当前作用域里可能存在的其它 `cast!` 冲突。
- `#(#variants_to_strs),*`：用逗号连接所有转出臂，作为 `match self` 的臂序列。
- `#(#strs_to_variants)*`：转入臂之间**没有分隔符**（`*` 而非 `, *`），因为 `cast!` 的转入臂本身以逗号结尾时由 `Punctuated::parse_terminated` 处理——实际上每条臂是一个独立的 `pattern => expr`，`cast!` 解析时用 `Punctuated::parse_terminated` 读到末尾即可。这里靠的是 `cast!` 那套 `CastInput::parse` 的容错。

最后这个 `quote!` 返回的 `TokenStream` 就是一个 `cast! { … }` 调用。编译器会再次调用 `cast!` 宏来展开它——于是 `Reflect` / `IntoValue` / `FromValue` 三个 impl 就由 `cast!` 按部就班地生成出来了。

> 这就是「委托」的实现：`derive_cast` 不写任何 `impl`，只写一句 `cast! { … }`。维护上，`cast!` 的逻辑（u2-l3 那 5 个 `create_*_body`）改一次，`#[derive(Cast)]` 自动跟着受益，零重复。

#### 4.3.4 代码实践

**目标**：把 [lib.rs 文档里的 Niceness 范例](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L309-L321) 完整地展开成等价 `cast!`。

**操作步骤**：

1. 阅读该范例：

   ```rust ignore
   /// A stringy enum of options.
   #[derive(Cast)]
   enum Niceness {
       /// Clearly nice (parses from `"nice"`).
       Nice,
       /// Not so nice (parses from `"not-nice"`).
       NotNice,
       /// Very much not nice (parses from `"❌"`).
       #[string("❌")]
       Unnice,
   }
   ```

2. 对每个变体算出 `Variant { ident, string, docs }`，再套用 4.3.2 的模板拼出 `cast!`。

**预期结果（等价 `cast!`，示意）**：

```rust ignore
::typst_library::foundations::cast! {
    Niceness,
    self => ::typst_library::foundations::IntoValue::into_value(match self {
        Niceness::Nice => "nice",
        Niceness::NotNice => "not-nice",
        Niceness::Unnice => "❌",
    }),
    #[doc = "Clearly nice (parses from `\"nice\"`)."]
    "nice" => Self::Nice
    #[doc = "Not so nice (parses from `\"not-nice\"`)."]
    "not-nice" => Self::NotNice
    #[doc = "Very much not nice (parses from `\"❌\"`)."]
    "❌" => Self::Unnice
}
```

注意三处与变体一一对应：

- `Nice` 无 `#[string]` → `"nice"`（`"Nice".to_kebab_case()`）。
- `NotNice` 无 `#[string]` → `"not-nice"`（`"NotNice".to_kebab_case()`，大写边界插连字符）。
- `Unnice` 有 `#[string("❌")]` → `"❌"`（覆盖默认的 `"unnice"`）。

转出臂里 `Niceness::Unnice => "❌"` 与转入臂里 `"❌" => Self::Unnice` 用的是同一个字符串 `"❌"`——这就是「同一份 `variants` 表生成两个方向」带来的正向/逆向一致性。

> 待本地验证：`docs` 字段是 `documentation()` 拼接并 `trim()` 后的结果（u1-l3），上面 `#[doc = ...]` 里的具体文本以本地展开为准。

#### 4.3.5 小练习与答案

**练习 1**：转出臂用 `#ty::#ident`（如 `Niceness::Nice`），转入臂用 `Self::#ident`。为什么一个用具体类型名、一个用 `Self`？
**答案**：转出臂出现在 `self => match self { ... }` 里，此时 `match` 的 scrutinee `self` 的类型就是该枚举，臂写 `Niceness::Nice` 是完整路径、最直白；而转入臂 `"nice" => Self::Nice` 出现在 `cast!` 生成的 `impl FromValue for Niceness` / `Reflect` 方法体内，那里 `Self` 就是 `Niceness`，用 `Self::Nice` 更通用、且与 `cast!` 手写范例（u2-l3 的 `Self(v as u8)`）风格一致。两种写法在这里都正确，只是各自贴合适用的上下文。

**练习 2**：`#(#strs_to_variants)*` 没有分隔符，`#(#variants_to_strs),*` 有逗号。为什么？
**答案**：`variants_to_strs` 是 `match self { … }` 的臂序列，Rust 语法要求 match 臂之间用逗号分隔，所以用 `,#(#…),*` 生成逗号分隔。`strs_to_variants` 是 `cast!` 的顶层转入臂，由 `cast!` 自己的 `CastInput::parse` 经 `Punctuated::parse_terminated` 解析——它靠的是 `cast!` 的解析规则，不需要在外层 quote 里显式插逗号（`Punctuated::parse_terminated` 会按需消费分隔符）。两处分别服从各自「宿主语法」的分隔要求。

**练习 3**：如果枚举只有一个变体，`#[derive(Cast)]` 还能正常工作吗？
**答案**：能。`variants` 只有一个元素时，`variants_to_strs` 只有一条 match 臂（`match self { Foo::Only => "only" }`，单臂 match 合法），`strs_to_variants` 也只有一条转入臂。生成的 `cast!` 完全有效，转换在「唯一变体」与 `"only"` 之间双向进行。代码里没有任何要求变体数 ≥ 2 的假设。

---

## 5. 综合实践

把本讲主实践完整做一遍，检验你是否真正掌握 `#[derive(Cast)]` 的展开。

### 5.1 任务

给定枚举（即本讲主实践题）：

```rust ignore
/// 一个字符串枚举。
#[derive(Cast)]
enum Niceness {
    /// Clearly nice.
    Nice,
    /// Not so nice.
    NotNice,
    /// Very much not nice.
    #[string("❌")]
    Unnice,
}
```

请完成两件事：

1. **写出 `derive(Cast)` 最终展开出的等价 `cast!` 代码**（含 `self =>` 转出臂与各字符串转入臂，并标注 `#[doc]`）。
2. **说明为何带显式 discriminant 的变体（如 `Nice = 1`）会被 `bail!` 拒绝**，并指出错误光标会落在哪里。

### 5.2 参考答案

**第 1 问（等价 `cast!`，示意）**：

```rust ignore
::typst_library::foundations::cast! {
    Niceness,
    self => ::typst_library::foundations::IntoValue::into_value(match self {
        Niceness::Nice => "nice",
        Niceness::NotNice => "not-nice",
        Niceness::Unnice => "❌",
    }),
    #[doc = "Clearly nice."]
    "nice" => Self::Nice
    #[doc = "Not so nice."]
    "not-nice" => Self::NotNice
    #[doc = "Very much not nice."]
    "❌" => Self::Unnice
}
```

逐变体核对：

- `Nice` → 默认 kebab → `"nice"`。
- `NotNice` → 默认 kebab → `"not-nice"`（大写边界插连字符）。
- `Unnice` → `#[string("❌")]` 覆盖 → `"❌"`（否则会是 `"unnice"`）。

这段 `cast!` 再由 u2-l3 的逻辑展开为：

- `impl IntoValue`：方法体 `IntoValue::into_value(match self { … })`，把变体变成 `Value::Str`。
- `impl FromValue`：方法体先判 `Value::Str`，`match` 各字符串 → `return Ok(Self::Xxx)`，兜底 `Err(...)`。
- `impl Reflect`：`castable` 判 `Value::Str` 是否匹配某字符串；`input` 把每条变成 `CastInfo::Value(字符串, 文档)`，`output` 复用 `input`（非 dynamic）。

**第 2 问（显式 discriminant 被拒）**：

`derive_cast` 在 [src/cast.rs:L20-L22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L20-L22) 检查每个变体的 `discriminant`：

```rust
if let Some((_, expr)) = &variant.discriminant {
    bail!(expr, "explicit discriminant is not allowed");
}
```

原因：`#[derive(Cast)]` 的转换语义是「变体名 ↔ 字符串」，**完全基于变体的名字和 `#[string]`**，与变体的数值 discriminant 毫无关系。若允许 `Nice = 1` 这样的写法，宏会**静默忽略** `= 1`——用户可能误以为 `1` 会参与转换（比如以为能从整数 `1` 解析进来），从而产生期望与实际不符的隐蔽 bug。因此宏选择**fail fast**：一旦看到 discriminant 就立即报错，把潜在的语义误解挡在编译期。

错误光标：因为 `bail!(expr, ...)` 用的是 discriminant 的表达式 `expr`（即那个 `1`），所以错误 span 精确落在 `= 1` 的 `1` 上，而非整个变体或整个枚举。

### 5.3 验证方法（源码阅读型）

本 crate 自身没有针对 `derive_cast` 展开结果的单元测试，验证依赖下游使用：

1. 在 `typst-library` 中挑一个 `#[derive(... Cast ...)]` 枚举（推荐 [FontStyle](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/variant.rs#L36-L47)、[Case](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/case.rs#L60-L67)、[UnicodeNormalForm](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L900-L917)），按本节方法手写它的等价 `cast!`，再对照该枚举在文档/自动补全里呈现的字符串选项。
2. 若本地装了 `cargo-expand`，对包含该枚举的 crate 运行 `cargo expand`，肉眼比对生成的 `impl IntoValue / FromValue / Reflect` 与本讲的预测是否一致。

---

## 6. 本讲小结

- `#[derive(Cast)]` 是**派生宏**（`#[proc_macro_derive(Cast, attributes(string))]`），入口接收单个 `DeriveInput`；原枚举项被编译器**原样保留**，宏只追加 impl——这与 `cast!`（函数式宏、消费宏体）在签名与对原项的处理上根本不同。
- `derive_cast` 只支持枚举，遇到 struct/union 立即 [`bail!(item, "only enums are supported")`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L14-L16)；遇到显式 discriminant 立即 [`bail!(expr, "explicit discriminant is not allowed")`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L20-L22)，因为转换只认名字、与数值无关，拒绝是为了 fail fast。
- 每个变体被映射成 [`Variant { ident, string, docs }`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L64-L68)：`string` 默认走 `to_kebab_case()`，可用 `#[string("...")]`（解析为 `LitStr`）覆盖；`docs` 由 `documentation()` 从变体注释拼出。
- 双向生成复用同一份 `variants` 表：[`strs_to_variants`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L39-L44) 产出字符串→变体的转入臂（`Pattern::Str`），[`variants_to_strs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L46-L50) 产出演→字符串的 `match` 臂，二者字符串一致保证双向正确。
- `derive_cast` 自己不写任何 `impl`，而是 [拼出一个 `cast! { … }` 调用](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L52-L60)（带 `#foundations::` 绝对路径前缀），把生成 `Reflect`/`IntoValue`/`FromValue` 的工作**委托**回 u2-l3 的 `cast!`——一次实现、两处复用。

## 7. 下一步学习建议

- **回顾闭环**：现在你已经看完了「类型与值转换」这条线的全部四个宏——`#[ty]`（u2-l2）自动补 `cast!`、`cast!`（u2-l3）是底层原语、`#[derive(Cast)]`（本讲）为枚举委托 `cast!`。建议回到 u2-l3 的 4.2 节条件表，确认你能解释「`#[derive(Cast)]` 生成的 `cast!` 一定是非 dynamic、有 `self =>`、有转入臂，所以三个 impl 全开」。
- **进入下一单元**：u3 单元讲 `#[func]` 与 `#[scope]`。届时你会看到函数参数的类型必须实现 `FromValue`——本讲生成的那些 `FromValue` impl 正是 `font(style: "italic")` 这类调用能解析的底层原因。带着这个联系去读 u3-l1 会顺畅很多。
- **源码延伸**：在 `typst-library` 里挑一个多变体的 `#[derive(Cast)]` 枚举（如 [model/reference.rs 的 RefForm](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/model/reference.rs#L416-L423)），手写它的等价 `cast!` 与三个 impl 方法体，作为本讲的自测题。
