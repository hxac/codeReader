# #[func]（一）：参数与元数据解析

## 1. 本讲目标

`#[func]` 是 typst-macros 里**最常用**的属性宏：它把一个普通的 Rust 函数注册成 Typst 运行时可以调用的「原生函数」。本讲是 `#[func]` 的第一部分，**只关注「解析」阶段**——也就是宏如何把用户写的函数签名读进来、整理成结构化的中间数据。

学完本讲，你应该能够：

- 说清 `func::parse` 如何从 `syn::ItemFn` 提取出一个 `Func` 中间结构。
- 区分四类**普通参数属性**（`#[named]` / `#[default]` / `#[variadic]` / `#[external]`）各自改写了什么字段。
- 解释 `engine` / `context` / `args` / `span` 四个**运行时特殊参数**是如何**按名字**被识别的，以及 `self` 接收者为何必须有 `parent`。
- 理解 `Binding`（`Owned` / `Ref` / `RefMut`）和 `parent` 的作用。
- 推导「positional 且 required」这一默认行为背后的判定逻辑。

> 下一讲（u3-l2）才会讲 `create`——也就是用这些中间数据生成 `NativeFuncData` 字面量和包装闭包。本讲专注「读懂输入」。

## 2. 前置知识

本讲承接 u1-l3（共享工具层 `util.rs`）。如果你还没读过，至少要了解以下概念：

- **属性宏 stream/item 约定**：`#[func]` 是属性宏，签名是 `fn func(stream, item)`，其中 `stream` 是 `#[func(..)]` 括号里的内容（元数据），`item` 是被装饰的那个 `fn`。
- **「取出 → 解析 → 校验」属性三步法**：`has_attr` / `take_attr` 取出属性，`parse_attr` 解析属性的值，`validate_attrs` 兜底校验未被消费的属性。
- **键值解析的正交矩阵**：`parse_flag` / `parse_key_value` / `parse_string` / `parse_string_array` 配合 `kw::xxx` 自定义关键字，构成「键 × 值类型」的解析工具。
- **`bail!` 宏**：构造带 span 的 `syn::Error`，错误最终经 `to_compile_error` 回传给编译器，全程不 panic。
- **`determine_name_and_title`**：借助 `heck` 把 Rust 标识符推导成 Typst 的 kebab-case `name` 与 Title Case `title`，并对冗余配置报错。
- 基本的 `syn` 概念：`ItemFn`、`FnArg`（分为 `Receiver` 和 `Typed`）、`Pat::Ident`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | `#[func]` 属性宏的对外入口，解析 `item` 为 `ItemFn` 后调用 `func::func`。入口的文档注释是 `#[func]` 用法的权威说明。 |
| `src/func.rs` | 本讲主战场。`func` / `parse` / `parse_param` 负责解析；`Func` / `Param` / `SpecialParams` / `Binding` / `Meta` 是中间数据结构。 |
| `src/util.rs` | 解析阶段反复调用的共享工具：`determine_name_and_title`、`documentation`、`has_attr` / `parse_attr` / `validate_attrs`、各种 `parse_*` helper、`kw` 关键字。 |

> 源码精读中只引用这三个文件。库里的真实用法（`str.rs`、`state.rs`）仅作为「现象」佐证，不属于本 crate 源码。

## 4. 核心概念与源码讲解

### 4.1 中间数据模型：`Func` / `Param` / `SpecialParams` / `Binding` / `Meta`

#### 4.1.1 概念说明

过程宏的解析阶段，本质是把一棵 `syn` 语法树「翻译」成**自己定义的、更贴合业务语义的中间结构**，然后代码生成阶段再基于这个中间结构 `quote!` 出 Token 流。`#[func]` 的中间结构分两层：

- **`Meta`**：来自 `#[func(..)]` 括号里的元数据（函数级别的配置，如 `name`、`since`、`scope`、`constructor`、`parent`）。
- **`Func`**：把 `Meta` 与函数项本身合并后的「完整画像」，包含名字、文档、可见性、所有参数、返回类型。

注意 Typst 把函数参数分成两类，这点贯穿整个解析逻辑：

1. **运行时特殊参数**（`self` / `engine` / `context` / `args` / `span`）：它们**不是** Typst 用户能传的参数，而是运行时注入的「上下文」。它们被收进 `SpecialParams`，不进入 `params` 列表。
2. **普通参数**：真正暴露给 Typst 用户的参数（如 `values`、`default`），收进 `params: Vec<Param>`。

#### 4.1.2 核心流程

整个解析就是把「两类输入」填进「一张 Func 画像」：

```
#[func(meta...)]  ──parse2──▶  Meta（函数级配置）
                                   │
syn::ItemFn  ──determine_name────▶ name / title
            ──documentation──────▶ docs
            ──遍历 sig.inputs────▶ parse_param × N
                                   │
                              ┌────┴─────┐
                          SpecialParams   Vec<Param>
                          (self/特殊参数)  (普通用户参数)
                                   │
                            合并 + 返回类型
                                   ▼
                                 Func
```

#### 4.1.3 源码精读

**入口** `func::func` 极简，严格遵循「parse → create」骨架（[src/func.rs:L13-L17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L13-L17)）：

```rust
pub fn func(stream: TokenStream, item: &syn::ItemFn) -> Result<TokenStream> {
    let func = parse(stream, item)?;
    Ok(create(&func, item))
}
```

入口在 [src/lib.rs:L103-L109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L103-L109)，用 `parse_macro_input!` 把 `item` 解析成 `syn::ItemFn`，再 `.into()` 转换 Token 流边界，出错走 `to_compile_error`。

**`Func` 画像**（[src/func.rs:L19-L51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L19-L51)）聚合了函数级的全部信息。其中 `special: SpecialParams` 与 `params: Vec<Param>` 分别盛放两类参数，`parent: Option<syn::Type>` 标记本函数是否是某个类型的**方法**（由 `#[scope]` 注入）。

**`SpecialParams`**（[src/func.rs:L53-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L53-L66)）值得细看：`self_` 是一个**完整的 `Param`**（因为 `self` 也要生成参数信息），而 `engine` / `context` / `args` / `span` 只是四个布尔标记——宏只需知道「有没有」，具体转发细节交给代码生成阶段。

```rust
struct SpecialParams {
    self_: Option<Param>,   // self 接收者，也是完整 Param
    engine: bool,           // 名为 engine 的参数是否出现
    context: bool,
    args: bool,
    span: bool,
}
```

**`Param`**（[src/func.rs:L68-L89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L68-L89)）与 **`Binding`**（[src/func.rs:L91-L99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L91-L99)）描述单个参数。`Binding` 决定调用时如何传递该参数（按值 / `&` / `&mut`），普通参数永远是 `Owned`，只有 `self` 会是 `Ref` 或 `RefMut`。

**`Meta`**（[src/func.rs:L101-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L101-L136)）是对 `#[func(..)]` 的解析。它的 `Parse` 实现把 u1-l3 讲过的「键 × 值类型」正交矩阵用了一遍：

```rust
impl Parse for Meta {
    fn parse(input: ParseStream) -> Result<Self> {
        Ok(Self {
            scope: parse_flag::<kw::scope>(input)?,            // 标志
            contextual: parse_flag::<kw::contextual>(input)?,
            name: parse_string::<kw::name>(input)?,            // 键 = 字符串
            title: parse_string::<kw::title>(input)?,
            constructor: parse_flag::<kw::constructor>(input)?,
            since: parse_key_value::<kw::since, Since>(input)?,// 键 = 任意类型
            keywords: parse_string_array::<kw::keywords>(input)?,
            parent: parse_key_value::<kw::parent, _>(input)?,
        })
    }
}
```

- `parse_flag::<kw::scope>`（[src/util.rs:L152-L160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L152-L160)）处理 `scope` 这种纯标志。
- `parse_string::<kw::name>`（[src/util.rs:L135-L140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L135-L140)）底层是 `parse_key_value`，要求值是字符串字面量。
- `parse_key_value::<kw::parent, _>`（[src/util.rs:L113-L126](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L113-L126)）则把值当作任意 `syn::Type` 解析——这正是 `#[scope]` 注入 `parent = SomeType` 的入口。
- 各 `kw::xxx` 自定义关键字统一定义在 [src/util.rs:L270-L282](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L270-L282)。

#### 4.1.4 代码实践

**实践目标**：亲手核对 `Meta::parse` 的字段与 `#[func(..)]` 写法的对应关系。

**操作步骤**：

1. 打开 [src/func.rs:L123-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L123-L136)，对照 `lib.rs` 里的官方文档示例（[src/lib.rs:L74-L88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L74-L88)）的 `#[func(title = "Minimum")]`。
2. 对 `#[func(title = "Minimum")]`，逐字段判断 `Meta` 的 8 个字段分别是什么值。
3. 在 Typst 库里找一个真实用法 `#[func(contextual, since = "0.11.0")]`（见 [introspection/state.rs:L256](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L256)），同样逐字段填表。

**需要观察的现象**：`contextual` 走 `parse_flag`（无需 `=`），`since = "0.11.0"` 走 `parse_key_value::<kw::since, Since>`；`Since` 的解析见 [src/util.rs:L320-L331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L320-L331)，会把字符串 `"0.11.0"` 解析成 `Version([0, 11, 0])`。

**预期结果**：你能解释为什么 `title` 必须写成 `title = "Minimum"`（字符串值）而 `scope` 只能写成裸 `scope`（无值标志）。

#### 4.1.5 小练习与答案

**练习 1**：`SpecialParams` 里，为什么 `self_` 用 `Option<Param>` 而 `engine` 只用 `bool`？

> **答案**：`self` 在生成参数信息（`NativeParamInfo`）时需要和普通参数一样的完整元数据（名字 `self`、文档、类型、定位 key），所以存成完整的 `Param`；而 `engine` / `context` / `args` / `span` 不暴露给 Typst 用户，代码生成阶段只需知道「有没有这个参数」来决定是否把它转发进调用，所以一个布尔标记就够。

**练习 2**：写出 `#[func(contextual, since = "forever", keywords = ["str", "text"])]` 解析出的 `Meta` 关键字段。

> **答案**：`contextual: true`、`since: Some(Since::Forever)`（`"forever"` 是 `Since` 的特殊取值，见 [src/util.rs:L294-L297](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L294-L297)）、`keywords: vec!["str".into(), "text".into()]`，其余字段为默认值（`scope: false`、`name/title/parent: None`、`constructor: false`）。

---

### 4.2 `func::parse`：函数项的整体解析

#### 4.2.1 概念说明

`func::parse`（[src/func.rs:L138-L177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L138-L177)）是解析阶段的「调度中枢」。它负责把 `Meta` 和 `syn::ItemFn` 拼成 `Func`，具体做四件事：

1. 解析 `Meta`，推导 `name` / `title`。
2. 提取文档。
3. 遍历 `item.sig.inputs`，逐个交给 `parse_param`。
4. 推导返回类型，做一条 `parent` 与 `scope` 互斥的校验。

#### 4.2.2 核心流程

```
func::parse(stream, item):
  1. meta = syn::parse2::<Meta>(stream)         # 解析 #[func(..)]
  2. (name, title) = determine_name_and_title(   # kebab / title 推导
       meta.name, meta.title, &item.sig.ident, None)
  3. docs = documentation(&item.attrs)           # 拼接 doc 注释
  4. for input in item.sig.inputs:               # 遍历每个参数
        parse_param(&mut special, &mut params, meta.parent, input)
  5. returns = item.sig.output 对应的类型（无返回值则 ()）
  6. if meta.parent.is_some() && meta.scope:
        bail!("scoped function cannot have a scope")
  7. 组装 Func 返回
```

#### 4.2.3 源码精读

`name` / `title` 推导复用 u1-l3 的 `determine_name_and_title`，第四个参数 `None` 表示不裁剪标识符（[src/func.rs:L141-L142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L141-L142)）。`documentation`（[src/util.rs:L25-L59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L25-L59)）把 `#[doc=...]` 注释拼成一段字符串。

参数遍历是关键（[src/func.rs:L146-L150](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L146-L150)）：

```rust
let mut special = SpecialParams::default();
let mut params = vec![];
for input in &item.sig.inputs {
    parse_param(&mut special, &mut params, meta.parent.as_ref(), input)?;
}
```

注意 `meta.parent.as_ref()` 被传进每一个 `parse_param`——这正是 `self` 接收者能拿到类型的依据（见 4.3）。

返回类型的处理（[src/func.rs:L152-L155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L152-L155)）有个细节：函数若没有显式返回类型（`->` 缺省），`returns` 会被填成 `()`。

最后是一条互斥校验（[src/func.rs:L157-L159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L157-L159)）：

```rust
if meta.parent.is_some() && meta.scope {
    bail!(item, "scoped function cannot have a scope");
}
```

含义：当一个函数已经身处某个类型的 `impl` 块里（`parent` 被注入），它就不能再自带 `#[func(scope)]`。因为 `scope` 标志会让代码生成阶段调用 `<#ident as NativeScope>::scope()` 来取子作用域，而带 `parent` 的函数根本不会生成同名影子类型 `#ident`（见 [src/func.rs:L361-L364](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L361-L364)），二者无法共存。

#### 4.2.4 代码实践

**实践目标**：验证「无返回值」函数会被填上 `returns = ()`。

**操作步骤**：

1. 阅读 [src/func.rs:L152-L155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L152-L155) 的 `match`。
2. 想象用户写了 `#[func] fn log(msg: Str) { println!("{msg}"); }`（没有 `->`）。
3. 判断这个函数的 `Func.returns` 最终是什么。

**需要观察的现象**：`syn::ReturnType::Default` 分支返回 `parse_quote! { () }`。

**预期结果**：`returns` 为 `()`（unit 类型）。这会在 u3-l2 里影响 `NativeFuncData.returns` 字段（它用 `<#returns as Reflect>::output()` 推导返回值描述）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `meta.parent` 会在遍历参数之前就要先解析出来，而不是等到生成阶段？

> **答案**：因为 `parse_param` 处理 `self` 接收者时需要立刻知道 `parent` 的类型来填充 `Param.ty`（[src/func.rs:L200-L203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L200-L203)）。`parent` 是函数级元数据，必须在解析参数之前就位。

**练习 2**：如果用户写了 `#[func(scope, parent = Foo)]`，会发生什么？

> **答案**：`parse` 在 [src/func.rs:L157-L159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L157-L159) 触发 `bail!`，报错 `"scoped function cannot have a scope"`。`parent` 通常不应由用户手写，而是由 `#[scope]` 自动注入（见 4.3 与 u3-l3）。

---

### 4.3 `parse_param`（上）：接收者 `self` 与运行时特殊参数

#### 4.3.1 概念说明

`parse_param`（[src/func.rs:L179-L249](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L179-L249)）逐个处理 `item.sig.inputs`。`syn::FnArg` 只有两个变体：

- `FnArg::Receiver`：即 `self` / `&self` / `&mut self`。
- `FnArg::Typed`：普通带类型的参数 `name: Type`。

本节讲两件容易被忽略的事：

1. `self` 接收者**必须有 `parent`**，否则直接报错。
2. 四个特殊参数 `engine` / `context` / `args` / `span` 是**纯按名字识别**的——只要参数名匹配，就被当成运行时注入的参数，**不会**进入 `params` 列表。

#### 4.3.2 核心流程

```
parse_param(input):
  match input:
    Receiver(recv):                       # self 接收者
      binding = 按引用/可变 选 Owned/Ref/RefMut
      special.self_ = Some(Param {
        ty: parent.clone() 或 bail("explicit parent type required")
        ident: "self_", name: "self", ...
      })
      return                              # 提前返回，不进 params

    Typed(typed):                         # name: Type
      要求 pat 是简单标识符（否则 bail "expected identifier"）
      match ident 名字:
        "engine"  => special.engine  = true
        "context" => special.context = true
        "args"    => special.args    = true
        "span"    => special.span    = true
        _         => 走「普通参数」分支（见 4.4）
```

#### 4.3.3 源码精读

**接收者分支**（[src/func.rs:L186-L212](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L186-L212)）先根据 `recv.reference` 和 `recv.mutability` 选 `Binding`：

```rust
let mut binding = Binding::Owned;
if recv.reference.is_some() {
    if recv.mutability.is_some() {
        binding = Binding::RefMut;   // &mut self
    } else {
        binding = Binding::Ref;      // &self
    }
}
// 否则保持 Owned（裸 self）
```

然后构造 `Param`，这里有两个关键点（[src/func.rs:L197-L210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L197-L210)）：

- `ident` 被设成字符串 `"self_"`（带下划线），为的是避开 Rust 关键字 `self`，同时 `name`（暴露给 Typst 的名字）是 `"self"`。
- `ty` 取自 `parent`，**没有 `parent` 就 `bail!`**：

```rust
ty: match parent {
    Some(ty) => ty.clone(),
    None => bail!(recv, "explicit parent type required"),
},
```

这条约束的逻辑是：`self` 只有在方法（某个 `impl` 块内的函数）里才有意义，而方法的「宿主类型」正是 `parent`。顶层函数写 `self` 没有意义，所以报错。

**特殊参数按名字识别**（[src/func.rs:L222-L226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L222-L226)）非常直接：

```rust
match ident.to_string().as_str() {
    "engine" => special.engine = true,
    "context" => special.context = true,
    "args" => special.args = true,
    "span" => special.span = true,
    _ => { /* 普通参数 */ }
}
```

注意这里的隐患：识别**完全靠名字字符串**，不看类型，也不消费属性、不做 `validate_attrs`。也就是说，如果你把一个想暴露给用户的参数取名叫 `engine`，它会被悄悄吞掉，变成运行时引擎参数。

一个真实例子，`state.get`（[introspection/state.rs:L255-L265](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L255-L265)）：

```rust
#[func(contextual, since = "0.11.0")]
pub fn get(
    &self,                       // Receiver -> special.self_，parent = State
    engine: &mut Engine,         // special.engine = true
    context: Tracked<Context>,   // special.context = true
    span: Span,                  // special.span = true
) -> SourceResult<Value> { ... }
```

它没有普通参数，所以 `params` 是空的；`engine` / `context` / `span` 全进了 `SpecialParams`。注意它也没有 `args`——是否需要「剩余参数」由用户自己决定。

#### 4.3.4 代码实践

**实践目标**：用一个真实库函数验证「按名字识别」会吞掉同名参数。

**操作步骤**：

1. 打开 [introspection/state.rs:L272-L284](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/state.rs#L272-L284) 的 `state.at`。
2. 数一数它的签名里有几个参数：`&self`、`engine`、`context`、`span`、`selector: LocatableSelector`。
3. 对照 [src/func.rs:L222-L226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L222-L226)，判断哪些进 `SpecialParams`、哪些进 `params`。

**需要观察的现象**：`selector` 是普通参数（名字不匹配任何特殊名），会被推入 `params`，其 `name` 是 `"selector"`（kebab-case 后仍为 `selector`）。

**预期结果**：`SpecialParams { self_: Some(..), engine: true, context: true, span: true, args: false }`，`params: [Param { name: "selector", .. }]`。

#### 4.3.5 小练习与答案

**练习 1**：一个顶层函数（不在任何 `impl` 里）写了 `fn foo(self) {}`，`#[func]` 会怎样？

> **答案**：`#[scope]` 没有注入 `parent`，所以 `meta.parent` 是 `None`。`parse_param` 在接收者分支命中 [src/func.rs:L200-L203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L200-L203) 的 `None` 分支，`bail!` 报 `"explicit parent type required"`。

**练习 2**：为什么 `&self` 对应 `Binding::Ref`、`&mut self` 对应 `Binding::RefMut`？这个 `Binding` 最终用在哪？

> **答案**：`Binding` 记录调用原函数时如何传递 `self`。`Ref` 会让代码生成阶段产生 `&self_`、`RefMut` 产生 `&mut self_`、`Owned` 产生 `self_`（见 [src/func.rs:L484-L491](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L484-L491) 的 `bind`），从而让生成的包装闭包以正确的借用方式调用原方法。

---

### 4.4 `parse_param`（下）：普通参数与四种属性

#### 4.4.1 概念说明

当参数名不是 `engine` / `context` / `args` / `span` 时，它进入 `_ =>` 分支，被当作**普通参数**处理。本节关注四个改变参数行为的属性：

- `#[named]`：参数变成「命名且可选」。
- `#[default]` / `#[default(expr)]`：给参数一个默认值。
- `#[variadic]`：参数吃掉「剩余的若干个位置参数」，类型须是 `Vec<_>`。
- `#[external]`：参数**只出现在文档里**，运行时被忽略。

这一分支还做两件事：把 Rust 标识符转成 Typst 的 kebab-case 名字，以及用 `validate_attrs` 兜底拒绝未识别的属性。

#### 4.4.2 核心流程

```
_ 分支（普通参数）:
  attrs = typed.attrs.clone()
  params.push(Param {
    binding:   Owned,
    ident:     ident.clone(),
    ty:        typed.ty.clone(),
    name:      ident.to_string().to_kebab_case(),     # heck 转换
    docs:      documentation(&attrs),
    named:     has_attr(&mut attrs, "named"),         # 取出 + 置布尔
    variadic:  has_attr(&mut attrs, "variadic"),
    external:  has_attr(&mut attrs, "external"),
    default:   parse_attr(&mut attrs, "default")?     # 取出 + 解析值
                 .map(|opt| opt.unwrap_or_default()), # flag => Default::default()
  })
  validate_attrs(&attrs)?                             # 剩余属性必须只剩 doc/derive
```

#### 4.4.3 源码精读

普通参数构造在 [src/func.rs:L227-L245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L227-L245)：

```rust
let mut attrs = typed.attrs.clone();
params.push(Param {
    binding: Binding::Owned,
    ident: ident.clone(),
    ty: (*typed.ty).clone(),
    name: ident.to_string().to_kebab_case(),
    docs: documentation(&attrs),
    named: has_attr(&mut attrs, "named"),
    variadic: has_attr(&mut attrs, "variadic"),
    external: has_attr(&mut attrs, "external"),
    default: parse_attr(&mut attrs, "default")?.map(|expr| {
        expr.unwrap_or_else(
            || parse_quote! { ::std::default::Default::default() },
        )
    }),
});
validate_attrs(&attrs)?;
```

几个要点：

**① 名字转换**：`name` 用 `heck` 的 `to_kebab_case()`。所以 Rust 里的 `default_value` 在 Typst 里就是 `default-value`（[src/func.rs:L233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L233)）。

**② `has_attr` 会「取出」属性**：`has_attr`（[src/util.rs:L61-L64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L61-L64)）内部调用 `take_attr`（[src/util.rs:L82-L91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L82-L91)）把属性**从列表里移除**。因此 `named` / `variadic` / `external` / `default` 这些自定义属性被逐个消费掉后，`validate_attrs`（[src/util.rs:L93-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L93-L102)）检查时，剩下的应该只有 `doc` 和 `derive`——否则就是拼写错误，报 `unrecognized attribute`。

**③ `default` 的三态**：`parse_attr`（[src/util.rs:L67-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L67-L80)）返回 `Option<Option<T>>`（外层「属性在不在」，内层「带不带值」），再被 `.map(|expr| expr.unwrap_or_else(|| Default::default()))` 规整成单一语义：

| 用户写法 | `parse_attr` 返回 | 最终 `default` 字段 |
| --- | --- | --- |
| 不写 `#[default]` | `None` | `None` |
| `#[default]`（裸标志） | `Some(None)` | `Some(::std::default::Default::default())` |
| `#[default(0)]`（带值） | `Some(Some(0))` | `Some(0)` |

**④ 「positional 且 required」的判定**：注意，解析阶段只**记录** `named` 和 `default` 这两个原始事实，并不直接判断 `positional` / `required`。这两个派生量在代码生成阶段才由 `create_param_info`（[src/func.rs:L426-L460](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L426-L460)）算出：

```rust
let positional = !named;
let required = !named && default.is_none();
```

也就是说，一个普通参数默认（什么属性都不加）就是「**positional 且 required**」：因为它 `named == false`（故 `positional == true`）且 `default == None`（故 `required == true`）。要让它变成可选，要么加 `#[named]`（变命名参数），要么给 `#[default]`（变带默认值）。

一个真实例子，`str` 的构造函数（[foundations/str.rs:L152-L160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L152-L160)）：

```rust
#[func(constructor, since = "forever")]
pub fn construct(
    value: ToStr,                                    # 普通参数：positional & required
    #[named]
    #[default(Spanned::detached(Base::Default))]
    base: Spanned<Base>,                             # 普通参数：named & 有默认值
) -> SourceResult<Str> { ... }
```

`value` 什么属性都没有 → positional + required；`base` 有 `#[named]` 且 `#[default(..)]` → 命名且可选。

#### 4.4.4 代码实践

**实践目标**：手动走一遍 `default` 的三态规整逻辑，确认你理解 `Option<Option<T>>`。

**操作步骤**：

1. 阅读 [src/func.rs:L238-L243](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L238-L243) 这段 `default` 赋值，再读 `parse_attr`（[src/util.rs:L67-L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L67-L80)）。
2. 对下面三种写法分别推算 `default` 字段最终值：
   - `x: i64`
   - `#[default] x: i64`
   - `#[default(7)] x: i64`
3. 再对照 `create_param_info` 的 `required = !named && default.is_none()`（[src/func.rs:L432](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L432)），判断三者各自是否 `required`。

**需要观察的现象**：第二种 `#[default]` 被规整成 `Some(Default::default())`（非 `None`），因此 `required == false`。

**预期结果**：
- `x: i64` → `default: None` → `required: true`
- `#[default] x: i64` → `default: Some(Default::default())` → `required: false`
- `#[default(7)] x: i64` → `default: Some(7)` → `required: false`

#### 4.4.5 小练习与答案

**练习 1**：为什么 `validate_attrs` 放在「构造完 Param」之后调用，而不是之前？

> **答案**：因为判断 `named` / `variadic` / `external` / `default` 需要先把这几个属性**从列表里取出来**（`has_attr` / `parse_attr` 都是消费型的）。取完之后，列表里剩下的才是「未被识别的残留属性」，这时调 `validate_attrs` 才能准确报出拼写错误。若在取之前校验，合法的自定义属性也会被误判为非法。

**练习 2**：`#[external] x: i64` 这个参数，解析后 `external: true`。它会不会进 `params`？会不会进 `SpecialParams`？

> **答案**：会进 `params`（它走的是 `_ =>` 普通参数分支，[src/func.rs:L227-L245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L227-L245)），但 `external: true`。它**不进** `SpecialParams`。`external` 的作用在代码生成阶段体现：包装闭包和参数转发都会 `filter(|param| !param.external)` 跳过它（见 [src/func.rs:L381-L382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L381-L382) 与 [src/func.rs:L405](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L405)），所以它只参与文档生成（`create_param_info` 不按 `external` 过滤，仍会生成参数说明）。

---

## 5. 综合实践

**任务**：给定下面这个函数签名（受 lib.rs 官方示例启发，本讲为练习改写），请**完整列出**解析阶段产出的 `SpecialParams` 与 `params`，并解释每个普通参数的 `positional` / `required` 取值。

```rust
#[func(title = "Minimum")]
fn min(
    &self,
    /// The values to extract the minimum from.
    #[variadic]
    values: Vec<i64>,
    /// A default value to return if there are no values.
    #[named]
    #[default(0)]
    default: i64,
) -> i64 {
    values.iter().min().unwrap_or(default)
}
```

> 注：这是「示例代码」，用于练习。`&self` 在这里假设 `parent` 已被 `#[scope]` 注入（例如 `parent = Min`）。

**操作步骤与参考答案**：

**① `Meta`（来自 `#[func(title = "Minimum")]`）**：`title: Some("Minimum")`，`name: None`（由 `min` 推导为 `"min"`），其余默认。`name` / `title` 经 `determine_name_and_title` 推导：`name = "min"`，`title` 因显式给了 `"Minimum"` 且不等于默认值 `"Min"`，故取 `"Minimum"`。

**② `SpecialParams`**：

| 字段 | 值 | 来源 |
| --- | --- | --- |
| `self_` | `Some(Param { binding: Ref, ident: "self_", ty: <parent>, name: "self", named:false, variadic:false, external:false, default:None })` | `&self` 是带引用的 Receiver，故 `Binding::Ref` |
| `engine` | `false` | 无名为 engine 的参数 |
| `context` | `false` | |
| `args` | `false` | |
| `span` | `false` | |

**③ `params`**（两条记录，按出现顺序）：

| ident | name（kebab） | ty | named | variadic | external | default | positional | required |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `values` | `values` | `Vec<i64>` | `false` | `true` | `false` | `None` | `true` | `true` |
| `default` | `default` | `i64` | `true` | `false` | `false` | `Some(0)` | `false` | `false` |

**④ 「positional 且 required」判定逻辑解释**：解析阶段只记录两个原始事实——`named`（是否有 `#[named]`）和 `default`（是否有 `#[default]` / `#[default(..)]`）。派生量在 `create_param_info` 算出（[src/func.rs:L431-L432](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L431-L432)）：

- `positional = !named`
- `required = !named && default.is_none()`

因此：

- `values`：`named=false` 且 `default=None` → positional **true**、required **true**。（`#[variadic]` 不影响这两个推导，它只在生成 `args.all()?` 时起作用。）
- `default`：`named=true` → positional **false**；又因 `named=true`，无论 `default` 是否为 `None`，`required = !named && ...` 都是 **false**。

可见一个参数「默认是 positional 且 required」，要打破这个默认，加 `#[named]`（改 named）或 `#[default]`（填 default）任一即可。

**待本地验证项**：如果你想确认 `name` / `title` 推导结果，可在本地用 `cargo expand`（若项目启用）查看 `#[func]` 展开后的 `NativeFuncData` 字面量里的 `name` 与 `title` 字段。

## 6. 本讲小结

- `#[func]` 严格遵循「parse → create」骨架：`func::parse` 把 `Meta` 与 `syn::ItemFn` 翻译成 `Func` 中间结构，代码生成（下一讲）再消费它。
- 参数被分成两类：运行时特殊参数（`self` / `engine` / `context` / `args` / `span`）进 `SpecialParams`，普通用户参数进 `params: Vec<Param>`。
- `engine` / `context` / `args` / `span` **纯按名字**识别，不看类型、不校验属性；`self` 接收者**必须有 `parent`**，否则报 `explicit parent type required`。
- `Binding`（`Owned` / `Ref` / `RefMut`）只对 `self` 有意义，普通参数恒为 `Owned`，它决定生成代码如何转发该参数。
- 四个普通参数属性各管一个布尔/字段：`#[named]`→`named`、`#[variadic]`→`variadic`、`#[external]`→`external`、`#[default(..)]`→`default`；属性被取出后再用 `validate_attrs` 兜底。
- 「positional 且 required」是默认行为，由 `positional = !named`、`required = !named && default.is_none()` 派生——这两个量在代码生成阶段才算出，解析阶段只负责记录 `named` 与 `default`。

## 7. 下一步学习建议

- **u3-l2（`#[func]`（二）：函数数据与包装闭包的代码生成）**：本讲整理出的 `Func` 将在那里被 `create` 消费，生成影子类型 / `_data` 函数、`NativeFuncData` 字面量、按参数属性选择 `args.all` / `named` / `eat` / `expect` 的包装闭包，以及 `NativeParamInfo`。建议带着本讲的 `SpecialParams` 与 `params` 表格去读，重点看 `create_wrapper_closure` 和 `create_param_info` 如何把 `named` / `default` / `variadic` / `external` 落地成真实调用代码。
- 课后可以再翻一遍 [src/lib.rs:L37-L102](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L37-L102) 的 `#[func]` 文档注释，它用文字总结了本讲用源码验证的全部属性语义，是最权威的速查表。
