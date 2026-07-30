# `#[func]`（二）：函数数据与包装闭包的代码生成

## 1. 本讲目标

上一讲（u3-l1）我们只做了「读懂输入」——把 `#[func(..)]` 元数据和 `syn::ItemFn` 翻译成中间结构 `Func`。本讲进入流水线的另一半：**生成（create）**。

读完本讲，你应当能够：

1. 说出 `create` 如何把一个普通 Rust 函数改造成「原函数 + 影子类型（或 `_data` 函数）+ `NativeFuncData` 字面量」三件套。
2. 解释「影子 enum」与 `{ident}_data()` 两种生成模式的取舍，并指出触发条件是**是否有 parent**。
3. 读懂包装闭包（wrapper closure）如何按参数属性在 `args.all / named / eat / expect` 之间做出选择、如何按默认值兜底，以及 `args.take().finish()?` 在何时被插入。
4. 理解 `create_param_info` 如何派生 `positional / required / settable` 等标志，以及 `LazyLock` 延迟初始化与 `DefSite / def_site_key` 的定位作用。

---

## 2. 前置知识

本讲默认你已经掌握 u3-l1 的全部内容。下面复习三个关键点，后文会直接使用这些术语，不再重复解释。

- **`Func` 中间结构**：`func::parse` 的产物，字段包括 `ident`（Rust 函数名）、`name/title`（Typst 暴露名）、`parent`（父类型，`Option<syn::Type>`）、`returns`（返回类型）、`special`（`SpecialParams`，含 `self_` 与 engine/context/args/span 四个布尔开关）、`params`（`Vec<Param>`）等。详见 [src/func.rs:20-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L20-L51)。
- **参数的事实只记录两个原始量**：`named`（是否有 `#[named]`）和 `default`（是否有 `#[default(..)]`）。`positional`、`required` 是在代码生成阶段**派生**出来的，而不是解析阶段记录的。
- **运行时契约**：本宏最终生成的代码必须满足运行时 trait 契约——为某个类型 `impl NativeFunc`，其 `data()` 返回 `&'static NativeFuncData`。`NativeFuncData` 是一个结构体，字段包括函数指针 `function`、`name`、`docs`、`def_site`、`scope`、`params`、`returns` 等。（该结构体定义在 `typst-library`，不在本 crate 内，故不附永久链接。）

一个贯穿全讲的核心直觉：

> **过程宏不「执行」你的函数，它「包装」你的函数。** 原函数原样保留并照常参与编译，宏在它旁边生成一个描述符（`NativeFuncData`）和一段「解析参数 → 调用原函数 → 把结果转成 `Value`」的胶水闭包。运行时 Typst 通过描述符找到胶水闭包，胶水闭包再调用你写的 Rust 逻辑。

---

## 3. 本讲源码地图

本讲几乎全部集中在 `src/func.rs`，辅以 `src/util.rs` 的少量工具与 `src/lib.rs` 的入口。

| 文件 | 作用 |
|---|---|
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) | `#[func]` 的 `#[proc_macro_attribute]` 入口，把 `item` 解析成 `syn::ItemFn` 后交给 `func::func`。 |
| [src/func.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs) | 本讲主战场：`create`（总调度）、`create_func_data`（数据字面量）、`create_wrapper_closure`（包装闭包）、`create_param_parser`（逐参解析）、`create_param_info`（参数元信息）、`create_func_ty`（影子类型）。 |
| [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs) | `oneliner`（首句抽取）、`quote_option`、`foundations`（路径简写）。 |

---

## 4. 核心概念与源码讲解

### 4.1 `create` 与 `create_func_data`：总调度与函数数据字面量

#### 4.1.1 概念说明

`create` 是「生成」阶段的总入口。它的职责只有一件：把 `Func` 中间结构 + 原函数项，拼装成一段完整的 `TokenStream` 发回给编译器。

这段输出包含**三个部分**：

1. **改写后的原函数**：去掉 `#[named]` / `#[default]` / `#[external]` 等仅供宏消费的属性，并删除 `#[external]` 参数，得到一个能正常编译的干净 Rust 函数。
2. **影子类型（可选）**：一个与函数同名的空 `enum`，专门用来挂 `impl NativeFunc`。
3. **创建者（creator）**：要么是 `impl NativeFunc for 影子类型`，要么是一个 `{ident}_data()` 函数——二选一，取决于是否有 parent。

`create_func_data` 则负责构造那个大字面量 `NativeFuncData { ... }`，它是运行时识别这个函数的全部元数据。

#### 4.1.2 核心流程

`create` 的伪代码：

```
create(func, item):
  item'   = rewrite_fn_item(item)      // 剥离宏私有属性、删除 external 参数
  ty?     = create_func_ty(func)        // 无 parent → Some(影子 enum)；有 parent → None
  data    = create_func_data(func)      // 构造 NativeFuncData 字面量
  oneliner = oneliner(func.docs)

  // creator 二选一：
  if ty.is_some():                      // 没有 parent：自由函数
      creator = impl NativeFunc for ident {
          fn data() -> &'static NativeFuncData {
              static DATA: NativeFuncData = data;   // 静态量，满足 &'static
              &DATA
          }
      }
  else:                                 // 有 parent：作用域内的方法
      creator = #[doc(hidden)] fn {ident}_data() -> &'static NativeFuncData {
          static DATA: NativeFuncData = data;
          &DATA
      }

  输出:  #[doc = oneliner]  item'   // 原函数
         #[doc(hidden)]    ty?      // 影子类型（若有）
         creator                    // impl 或 _data 函数
```

`create_func_data` 的核心是逐字段填充 `NativeFuncData`，其中三处需要特别理解：

- **`def_site_key`**：用于精确定位「这个函数定义在源码哪里」。没有 parent 时就是函数名字符串；有 parent（且 parent 是简单路径）时是 `"Parent::func"`。
- **`name`**：构造函数（`constructor`）时直接复用父类型的名字 `<#parent as NativeType>::NAME`；否则用推导出的 `name`。
- **`scope` / `params` / `returns`**：这三个字段都用 `LazyLock::new(&|| ...)` 延迟求值（详见 4.1.4 的实践与 4.3）。

#### 4.1.3 源码精读

入口在 [src/lib.rs:103-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L103-L109)，把 `item` 解析为 `syn::ItemFn` 后交给 `func::func`；后者只有两行——先 `parse` 再 `create`（[src/func.rs:14-17](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L14-L17)）。

`create` 的完整逻辑在 [src/func.rs:252-290](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L252-L290)。其中 creator 的二选一分支：

```rust
let creator = if ty.is_some() {
    quote! {
        impl #foundations::NativeFunc for #ident {
            fn data() -> &'static #foundations::NativeFuncData {
                static DATA: #foundations::NativeFuncData = #data;
                &DATA
            }
        }
    }
} else {
    let ident_data = quote::format_ident!("{ident}_data");
    quote! {
        #[doc(hidden)]
        #[expect(non_snake_case)]
        #vis fn #ident_data() -> &'static #foundations::NativeFuncData {
            static DATA: #foundations::NativeFuncData = #data;
            &DATA
        }
    }
};
```

这段代码（[src/func.rs:259-278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L259-L278)）说明了关键取舍：`ty.is_some()`（即 `create_func_ty` 返回了影子 enum，也就是没有 parent）时走 `impl NativeFunc`；否则走 `{ident}_data()` 函数。`data()` 用局部 `static DATA` 来满足 `&'static` 返回要求——这是过程宏里极常见的「把字面量塞进 static 再返回引用」模式。

> 为什么需要两种模式？因为「影子类型」只能定义在模块顶层。自由函数 `#[func] fn double(...)` 在模块顶层，宏可以在它旁边生成 `enum double {}` 并 `impl NativeFunc for double`；但当一个 `#[func]` 出现在 `#[scope] impl Foo { ... }` 块里时（此时它带 `parent = Foo`），**impl 块内部不允许定义新类型**，父类型也早已实现 `NativeType`，于是宏退而求其次，生成一个隐藏函数 `foo_data()`，由 `#[scope]` 宏在组装作用域时调用它来注册函数（详见下一讲 u3-l3）。这正是 lib.rs 顶部注释所讲的故事（[src/lib.rs:17-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L17-L27)）。

`create_func_data` 在 [src/func.rs:293-358](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L293-L358)。`def_site_key` 的推导：

```rust
let def_site_key = if let Some(syn::Type::Path(path)) = parent
    && let Some(parent) = path.path.get_ident()
{
    format!("{parent}::{ident}")
} else {
    ident.to_string()
};
```

[src/func.rs:309-315](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L309-L315)：有简单 parent 路径就拼成 `Parent::func`，否则只用函数名。最终的字面量：

```rust
quote! {
    #foundations::NativeFuncData {
        function: #foundations::NativeFuncPtr(&#closure),
        name: #name,
        title: #title,
        since: #since,
        docs: #docs,
        def_site: Some(::typst_utils::DefSite { path: file!(), key: #def_site_key }),
        keywords: &[#(#keywords),*],
        contextual: #contextual,
        scope: ::std::sync::LazyLock::new(&|| #scope),
        params: ::std::sync::LazyLock::new(&|| ::std::vec![#(#params),*]),
        returns: ::std::sync::LazyLock::new(&|| <#returns as #foundations::Reflect>::output()),
    }
}
```

[src/func.rs:343-357](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L343-L357)。几个要点：

- `function: NativeFuncPtr(&#closure)` 把包装闭包以 `&'static` 引用形式存进函数指针 newtype。能拿到 `&'static` 是因为闭包**不捕获任何外部变量**（所有数据都硬编码在闭包体内或来自参数），Rust 会把它提升（promote）为静态值。
- `def_site` 用 `file!()`（编译期当前文件路径）+ `def_site_key` 组成定位信息，便于 IDE 跳转与热重载时定位定义点。
- `scope / params / returns` 全部用 `LazyLock::new(&|| ...)`：这些值依赖其他类型（如 `Reflect::output()`、`NativeScope::scope()`），延迟到运行时首次访问才求值，规避「类型间互相引用导致初始化顺序无解」的问题。

`scope` 字段的两种来源（[src/func.rs:317-321](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L317-L321)）：声明了 `scope` 标志的函数用 `<#ident as NativeScope>::scope()`（由 `#[scope]` 宏生成），否则用空作用域 `Scope::new()`。`name` 字段同理，构造函数复用父类型名（[src/func.rs:331-335](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L331-L335)）。

#### 4.1.4 代码实践

**实践目标**：亲手追踪 `create` 对一个自由函数的展开，验证「原函数 + 影子 enum + impl」三件套。

**操作步骤**：

1. 想象输入是 lib.rs 注释里的示例（[src/lib.rs:29-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L29-L35)）：
   ```rust
   /// Doubles an integer.
   #[func]
   fn double(x: i64) -> i64 { 2 * x }
   ```
2. 因为没有 parent，`create_func_ty` 返回 `Some`（影子 enum）。
3. 据此写出 `create` 顶层 `quote!` 块（[src/func.rs:280-289](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L280-L289)）展开后的等价形式（**示例代码，非项目原码**）：
   ```rust
   #[doc = "Doubles an integer."]          // oneliner 抽取的首句
   #[expect(dead_code)]
   #[expect(rustdoc::broken_intra_doc_links)]
   fn double(x: i64) -> i64 { 2 * x }      // rewrite_fn_item 处理后的干净原函数

   #[doc(hidden)]
   #[expect(non_camel_case_types)]
   pub enum double {}                       // 影子类型：同名空 enum

   impl ::typst_library::foundations::NativeFunc for double {
       fn data() -> &'static ::typst_library::foundations::NativeFuncData {
           static DATA: ::typst_library::foundations::NativeFuncData =
               /* create_func_data 生成的字面量，见 4.3 实践 */;
           &DATA
       }
   }
   ```

**需要观察的现象 / 预期结果**：

- 原函数 `double` 与影子类型 `double` 同名共存——这合法，因为 Rust 的**值命名空间**（函数）与**类型命名空间**（enum）互不冲突。
- `#[expect(dead_code)]`：原函数现在不再被 Rust 代码直接调用（改由闭包内部调用），加这条抑制「未使用」警告。

> 待本地验证：若你想真的看到展开结果，可用 `cargo expand`（需要 `cargo install cargo-expand`）。但注意 typst-macros 是过程宏 crate 本身，需在一个依赖它的二进制 crate 里使用，不能直接对本 crate 展开。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 `#[func]` 带 `parent`（即位于 `#[scope]` impl 块内），`create` 不会再生成影子 enum。那么运行时怎么拿到它的 `NativeFuncData`？

**答案**：`create` 会生成一个 `#[doc(hidden)] fn {ident}_data() -> &'static NativeFuncData`（[src/func.rs:268-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L268-L277)）。`#[scope]` 宏在组装作用域时会调用这个函数，把数据注册进 scope（详见 u3-l3）。

**练习 2**：`NativeFuncData::data()` 为什么用一个局部 `static DATA` 而不是直接返回字面量的引用？

**答案**：`NativeFuncData` 含 `LazyLock`、`&'static str`、函数指针等字段，是一个完整的结构体值，不是 `const`。Rust 不允许直接对临时字面量取 `&'static`，但可以把它绑定到一个 `static` 项上再返回其引用。这是「字面量进 static、返回 static 引用」的标准过程宏套路。

---

### 4.2 `create_wrapper_closure` 与 `create_param_parser`：运行时包装闭包

#### 4.2.1 概念说明

`NativeFuncData.function` 指向的运行时函数有一个固定签名（trait object）：

```rust
dyn Fn(&mut Engine, Tracked<Context>, &mut Args) -> SourceResult<Value> + Send + Sync
```

但你写的 Rust 函数签名各不相同——参数有 positional / named / variadic / default 之分，还可能带 `&self`、`engine`、`context`、`args`、`span` 等运行时特殊参数。**包装闭包**就是这两者之间的适配器：它接收固定的 `(engine, context, args)`，按你声明的参数属性从 `args` 里把值一个个解析出来，再转发给你的真实函数，最后把返回值转成 `Value`。

`create_wrapper_closure` 生成这个闭包的骨架，`create_param_parser` 为每个参数生成一句「解析并绑定」的 `let` 语句。

#### 4.2.2 核心流程

包装闭包的整体结构（[src/func.rs:414-422](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L414-L422)）：

```
|engine, context, args| {
    let __typst_func = parent::ident;   // 指向真实 Rust 函数（自由函数则无 parent:: 前缀）

    // ① 逐参解析（self 优先，然后每个非 external 参数）
    <self_handler>                        // 若有 self：let mut self_ = ...;
    <param_handlers...>                   // 每个参数：let mut x: T = ...;

    // ② 收尾校验（仅当函数未声明自己的 args 参数）
    args.take().finish()?;                // 检查是否有多余/未识别的参数

    // ③ 调用真实函数
    let output = __typst_func(self?, engine?, context?, args?, args.span?, x, y, ...);

    // ④ 把返回值（T / StrResult<T> / SourceResult<T>）统一转成 SourceResult<Value>
    IntoResult::into_result(output, args.span)
}
```

`create_param_parser` 对单个参数的取值决策是一个四选一的优先级矩阵：

| 条件（按优先级从上到下） | 取值方式 | 含义 |
|---|---|---|
| `variadic` | `args.all(name)?` | 取走剩余所有位置参数，打包成 `Vec` |
| `named` | `args.named(name)?` | 取一个**具名**参数，返回 `Option` |
| 有 `default`（非 named 非 variadic） | `args.eat()?` | 吃掉一个**可选**位置参数，返回 `Option` |
| 其余（positional + required） | `args.expect(name)?` | 取一个**必需**位置参数，缺失即报错 |

若参数带 `default`，取值后再用 `.unwrap_or_else(\|\| default)` 兜底（[src/func.rs:476-478](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L476-L478)）。

#### 4.2.3 源码精读

`create_wrapper_closure` 全文在 [src/func.rs:375-423](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L375-L423)。三段分别对应上面流程的 ①②③：

**① handlers**：把 self（若有）和每个非 external 参数各送进 `create_param_parser`：

```rust
let handlers = {
    let func_handlers = func.params.iter()
        .filter(|param| !param.external)
        .map(create_param_parser);
    let self_handler = func.special.self_.as_ref().map(create_param_parser);
    quote! { #self_handler #(#func_handlers)* }
};
```

[src/func.rs:377-388](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L377-L388)。注意 `external` 参数只出现在文档与 `NativeParamInfo` 里，运行时根本不解析、也不转发——这就是 `#[external]`「只在文档里有、行为上忽略」的实现。

**② finish**：

```rust
let finish = (!func.special.args).then(|| quote! { args.take().finish()?; });
```

[src/func.rs:391](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L391)。**只有当函数没有声明自己的 `args: &mut Args` 特殊参数时**，才会插入这一行。`args.take()` 取走所有尚未被消费的参数，`.finish()` 检查其中有没有「意外/多余」的参数，有就报错。这保证了 `#[func] fn double(x: i64)` 在被 `double(1, 2)` 调用时会因多了一个参数而报错。当函数声明了 `args`，意味着作者要亲自接管剩余参数的处理，宏就不再插足。

**③ call**：按 special 开关按需拼上 `self / engine / context / args / span`，再追加所有非 external 参数：

```rust
let call = {
    let self_ = func.special.self_.as_ref().map(bind).map(|t| quote! { #t, });
    let engine_ = func.special.engine.then(|| quote! { engine, });
    let context_ = func.special.context.then(|| quote! { context, });
    let args_ = func.special.args.then(|| quote! { args, });
    let span_ = func.special.span.then(|| quote! { args.span, });
    let forwarded = func.params.iter().filter(|p| !p.external).map(bind);
    quote! { __typst_func(#self_ #engine_ #context_ #args_ #span_ #(#forwarded,)*) }
};
```

[src/func.rs:394-409](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L394-L409)。每个运行时特殊参数都用 `.then(...)` 按需出现，因此你的函数签名里**写不写** `engine`、`context`、`args`、`span`，决定了闭包是否把它们转发进去——这解释了 u3-l1 里「engine/context/args/span 纯按名字识别」为何如此设计：名字即开关。`bind` 函数（[src/func.rs:484-491](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L484-L491)）负责把 `self` 的三种绑定形式（Owned / `&self` / `&mut self`）翻译成 `self_` / `&self_` / `&mut self_`。

`create_param_parser` 全文（[src/func.rs:463-481](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L463-L481)）：

```rust
let mut value = if param.variadic {
    quote! { args.all()? }
} else if param.named {
    quote! { args.named(#name)? }
} else if param.default.is_some() {
    quote! { args.eat()? }
} else {
    quote! { args.expect(#name)? }
};
if let Some(default) = &param.default {
    value = quote! { #value.unwrap_or_else(|| #default) }
}
quote! { let mut #ident: #ty = #value; }
```

这就是 4.2.2 那张矩阵的直接翻译。最后一句强调变量声明为 `let mut`——因为 `bind` 对 `&mut self` 要取可变借用。

#### 4.2.4 代码实践

**实践目标**：为 `#[func] fn double(x: i64) -> i64` 手写包装闭包，并解释 `args.take().finish()?` 的插入条件。

**操作步骤**：

1. `double` 无 parent、无 self、无 engine/context/args/span、`x` 是 positional required。
2. `create_param_parser(x)`：`x` 非 variadic、非 named、无 default → 落到 `args.expect("x")?`，无兜底。
3. `finish`：`double` 未声明 `args` → 插入 `args.take().finish()?;`。
4. `call`：无任何 special，只转发 `x` → `__typst_func(x,)`。

**预期结果**（**示例代码，非项目原码**）：

```rust
|engine, context, args| {
    let __typst_func = double;                          // 无 parent，直接指函数
    let mut x: i64 = args.expect("x")?;                 // ① handlers
    args.take().finish()?;                              // ② finish（未声明 args）
    let output = __typst_func(x,);                      // ③ call
    ::typst_library::foundations::IntoResult::into_result(output, args.span)  // ④
}
```

把这个闭包代入 `NativeFuncData.function = NativeFuncPtr(&上述闭包)`，就得到了 4.1.4 里 `static DATA` 的 `function` 字段。

**`args.take().finish()?` 的插入条件**：当且仅当 `!func.special.args` 为真，即**该函数没有声明 `args: &mut Args` 参数**。它的作用是把所有「没被任何 `all/named/eat/expect` 消费掉」的剩余参数取出并校验，遇多余参数即报错。一旦作者写了 `args`，意味着要手工处理剩余参数，宏便不再自动收尾——否则会与作者自己的 `args.take()` 冲突。

> 待本地验证：可以构造一个故意多传参数的 Typst 调用 `#double(1, 2)`，观察报错信息是否来自「unexpected argument」。这需要在一个集成了 typst 引擎的项目里运行，本 crate 内无法直接运行。

#### 4.2.5 小练习与答案

**练习 1**：对 `#[variadic] values: Vec<i64>`，`create_param_parser` 生成哪一句？为何不需要 default 兜底？

**答案**：`let mut values: Vec<i64> = args.all()?;`（[src/func.rs:466-467](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L466-L467)）。variadic 取走所有剩余位置参数，`args.all()` 在零个参数时返回空 `Vec` 而非 `None`，所以天然有「空集」语义，不需要 default 兜底。

**练习 2**：`engine`、`context`、`args`、`span` 这四个特殊参数为什么用 `.then(...)` 生成转发代码，而不是固定转发？

**答案**：因为这四个参数是**可选**的——作者只在需要时才把它们写进签名。`.then(func.special.xxx)` 按声明与否决定是否拼进调用列表（[src/func.rs:401-404](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L401-L404)）。这样真实函数签名里没有 `engine` 时，闭包也不会尝试给它传 `engine`，签名匹配才成立。

---

### 4.3 `create_param_info` 与 `create_func_ty`：参数元信息与影子类型

#### 4.3.1 概念说明

`NativeFuncData.params` 是一个 `Vec<NativeParamInfo>`，每一条描述一个参数的名字、文档、接受什么类型（`input: CastInfo`）、是否 positional/named/variadic/required、有没有默认值等。这些信息**不参与调用**，只用于文档生成、自动补全和错误提示。`create_param_info` 把每个 `Param` 翻译成这样一条元信息。

注意一个关键事实：**`settable` 字段恒为 `false`**。函数参数永远不会是「可 set」的——那是元素（element）字段才有的能力。宏在这里写死 `settable: false`，是函数与元素的明确分界。

`create_func_ty` 则负责生成那个影子 enum（已在 4.1 讨论过它的使用时机，这里补全它的实现）。它也间接定义了 `def_site_key` 的「逐级拼接」规则——`create_param_info` 会把函数级 key 再拼上参数名。

#### 4.3.2 核心流程

`create_param_info` 对单个参数派生各项的规则：

```
create_param_info(param, parent_key):
  key  = parent_key + "::" + param.ident        // 如 "double::x"
  positional = !named
  required   = !named && default.is_none()
  ty = if variadic 或 (named 且 无 default):
           <param.ty as Container>::Inner       // 解包外层 Vec / Option，暴露内层类型
       else:
           param.ty
  default = param.default.map(\_| 闭包: || { let v: ty = default; IntoValue::into_value(v) })

  生成: NativeParamInfo { name, docs, def_site, input: <ty as Reflect>::input(),
                          default, positional, named, variadic, required, settable: false }
```

两个派生式最值得记住：

- `positional = !named`
- `required = !named && default.is_none()`

这正是 u3-l1 强调的「解析阶段只记 `named` 与 `default` 两个事实，`positional/required` 在生成阶段派生」的落点（[src/func.rs:431-432](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L431-L432)）。

`ty` 的「解包」逻辑（[src/func.rs:433-437](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L433-L437)）值得专门理解：当参数是 variadic（`Vec<T>`）或 named 且必填（`Option<T>`）时，向文档/补全暴露的「真实类型」是内层 `T`，而不是 `Vec<T>` / `Option<T>`。所以用 `<ty as Container>::Inner` 取出内层类型。其他情况（positional optional、named 带默认值）直接用声明类型。

`create_func_ty` 的实现极简（[src/func.rs:361-372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L361-L372)）：有 parent 返回 `None`；否则返回一个同名空 `enum`。它的返回值被 `create` 用来判断走哪种 creator 分支（见 4.1）。

#### 4.3.3 源码精读

`create_param_info` 全文（[src/func.rs:426-460](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L426-L460)），关键片段：

```rust
let def_site_key = format!("{parent_def_site_key}::{ident}");
let positional = !named;
let required = !named && default.is_none();
let ty = if *variadic || (*named && default.is_none()) {
    quote! { <#ty as #foundations::Container>::Inner }
} else {
    quote! { #ty }
};
let default = quote_option(&default.as_ref().map(|_default| {
    quote! {
        || {
            let typed: #ty = #default;
            #foundations::IntoValue::into_value(typed)
        }
    }
}));
```

- `def_site_key` 逐级拼接（[src/func.rs:430](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L430)）：函数级 key（来自 `create_func_data`）再拼 `::参数名`，于是自由函数 `double` 的参数 `x` 得到 `"double::x"`，作用域方法 `Foo::bar` 的参数 `y` 得到 `"Foo::bar::y"`。这套 key 规则让 IDE 能精确定位到「哪个函数的哪个参数」。
- `default` 是一个**返回 `Value` 的闭包**（[src/func.rs:438-445](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L438-L445)）：运行时需要默认值时调用它，先按声明类型求值，再 `IntoValue` 转成 `Value`。注意闭包内的 `#ty` 是**解包后的类型**（上一步算出的内层类型），保证 `let typed: #ty = #default` 类型一致。`quote_option`（[src/util.rs:105-111](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L105-L111)）把 `Some(x)` / `None` 字面化为 `Some(...)` / `None`。

最终生成的 `NativeParamInfo` 字面量（[src/func.rs:446-459](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L446-L459)）里，`input: <#ty as Reflect>::input()` 复用 u2-l3 `cast!` 引入的 `Reflect::input()` 生成 `CastInfo`（接受类型描述），驱动自动补全与错误提示——这是 `#[func]` 与 `cast!` 两条线的交汇点。`settable: false` 写死。

`create_func_data` 中 `params` 字段的拼装（[src/func.rs:324-329](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L324-L329)）把 self 与所有用户参数（含 external！）串起来逐一 `create_param_info`：

```rust
let params = func.special.self_
    .iter()
    .chain(&func.params)
    .map(|param| create_param_info(param, &def_site_key));
```

注意这里**包含 external 参数**——external 参数运行时不解析、不转发（4.2 已说明），但它的 `NativeParamInfo` 照样生成，出现在文档里。这正是 `#[external]`「只在文档里有、行为上忽略」的另一半体现：解析端忽略，文档端保留。

`create_func_ty` 全文（[src/func.rs:361-372](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L361-L372)）：

```rust
pub fn create_func_ty(func: &Func) -> Option<TokenStream> {
    if func.parent.is_some() {
        return None;
    }
    let Func { vis, ident, .. } = func;
    Some(quote! {
        #[doc(hidden)]
        #[expect(non_camel_case_types)]
        #vis enum #ident {}
    })
}
```

`#[expect(non_camel_case_types)]` 抑制「类型名应为大驼峰」的警告——因为影子类型与函数同名，而函数名通常是蛇形。`#[doc(hidden)]` 让它对用户不可见。

#### 4.3.4 代码实践

**实践目标**：为 `double` 的参数 `x` 手写 `create_param_info` 的输出，并补全 4.1.4 里 `static DATA` 的 `params` 与 `returns` 字段。

**操作步骤**：

1. `x`：`name="x"`、`named=false`、`variadic=false`、`default=None`、`ty=i64`。
2. 派生：`positional=true`、`required=true`。
3. `ty` 解包判断：非 variadic、非（named 且无 default）→ 直接用 `i64`。
4. `default = None`（无默认值）。
5. `def_site_key = "double::x"`。

**预期结果**（**示例代码，非项目原码**）：

```rust
// params 字段（create_func_data 的 vec![...] 里只有这一条）
::typst_library::foundations::NativeParamInfo {
    name: "x",
    docs: "",
    def_site: Some(::typst_utils::DefSite { path: file!(), key: "double::x" }),
    input: <i64 as ::typst_library::foundations::Reflect>::input(),
    default: None,
    positional: true,
    named: false,
    variadic: false,
    required: true,
    settable: false,
}

// returns 字段（直接由返回类型 i64 的 Reflect::output() 生成）
::std::sync::LazyLock::new(&|| <i64 as ::typst_library::foundations::Reflect>::output())
```

把它们填进 4.1.4 的 `static DATA` 字面量对应位置，`double` 的完整 `NativeFuncData` 就齐了。

> 待本地验证：在 IDE（如 typst 的语言服务器）里对一个 `#[func]` 函数触发自动补全，能看到参数名 `x` 与其类型描述——这正是 `input: <i64 as Reflect>::input()` 在起作用。本 crate 内无法直接验证，需在集成 typst 的环境中观察。

#### 4.3.5 小练习与答案

**练习 1**：对一个 `#[named] default: Option<i64>` 参数（named 且无 `#[default]`），`create_param_info` 里 `ty` 字段会是什么？为什么？

**答案**：是 `<Option<i64> as Container>::Inner`，即暴露内层 `i64`（[src/func.rs:433-434](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L433-L434)）。因为 named 且必填（`default.is_none()`）时，外层 `Option` 只是「可缺失」的容器，文档/补全应告诉用户「这里填一个整数」，而不是「填一个 Option」。

**练习 2**：为什么 `NativeParamInfo.settable` 恒为 `false`？

**答案**：`settable`（能否用 `set` 规则设置）是**元素字段**独有的属性，函数参数没有这条能力。宏在 [src/func.rs:457](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L457) 写死 `settable: false`，是函数与元素的明确分界线——这一点在 u4 `#[elem]` 系列会形成鲜明对比。

---

## 5. 综合实践

把本讲三个模块串起来，**完整还原一个稍复杂函数的宏展开**。

**输入**（参照 lib.rs 的 `min` 示例，[src/lib.rs:74-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L74-L88)，但简化为自由函数、去掉 `title`）：

```rust
/// Determines the minimum of a sequence of values.
#[func]
fn min(
    /// The values to extract the minimum from.
    #[variadic] values: Vec<i64>,
    /// A default value to return if there are no values.
    #[named] #[default(0)] default: i64,
) -> i64 {
    values.iter().min().unwrap_or(default)
}
```

**任务**：请分别写出

1. `create_param_parser` 对 `values` 与 `default` 各生成的 `let` 语句；
2. 包装闭包中 `finish` 是否出现、`call` 的转发实参序列；
3. `values` 与 `default` 两条 `NativeParamInfo` 的 `positional / required / ty` 三个字段。

**参考答案**（**示例代码，非项目原码**）：

1. 解析语句：
   ```rust
   let mut values: Vec<i64> = args.all()?;                 // variadic
   let mut default: i64 = args.named("default")?.unwrap_or_else(|| 0);  // named + default 兜底
   ```
   注意 `default` 走 `args.named`（因 `named` 优先级高于「有 default」分支），再 `.unwrap_or_else(|| 0)` 兜底。

2. `finish`：`min` 未声明 `args` → **出现** `args.take().finish()?;`。`call` 转发实参：无 self/engine/context/args/span，只转发 `values, default,`，即 `__typst_func(values, default,)`。

3. 两条 `NativeParamInfo` 关键字段：
   - `values`：`positional=true`（!named）、`required=true`（!named 且无 default）、`ty = <Vec<i64> as Container>::Inner`（variadic 解包成 `i64`）。
   - `default`：`positional=false`（named）、`required=false`（!named 为假）、`ty = i64`（named 但**有** default，不满足「named 且无 default」，故不解包、直接用声明类型）。

   这里 `default` 的 `ty` 是个易错点：只有「named 且**无** default」才解包 `Option`；本例 `default` 带了 `#[default(0)]`，所以不解包，直接是 `i64`。

完成此题后，你应当能把任意 `#[func]` 函数的展开结果在脑中跑一遍：解析语句、闭包结构、参数元信息三处都能对号入座。

---

## 6. 本讲小结

- `create` 输出「改写后的原函数 + 影子类型（可选）+ creator」三件套；creator 在「无 parent → `impl NativeFunc`」与「有 parent → `{ident}_data()` 函数」之间二选一，根因是 Rust 不允许在 impl 块内定义新类型。
- `create_func_data` 生成 `NativeFuncData` 字面量，用局部 `static DATA` 满足 `&'static`；`scope/params/returns` 用 `LazyLock` 延迟求值以规避类型间初始化顺序问题。
- 包装闭包是「固定运行时签名 ↔ 多变用户签名」的适配器：`create_param_parser` 按优先级矩阵（variadic→named→有 default→positional required）选 `args.all/named/eat/expect`，有 default 再兜底。
- `args.take().finish()?` **仅当函数未声明 `args` 参数时**插入，用于报出多余参数；engine/context/args/span 用 `.then(...)` 按声明与否决定是否转发。
- `create_param_info` 在生成阶段派生 `positional = !named`、`required = !named && default.is_none()`，按需用 `Container::Inner` 解包 `Vec/Option`，`settable` 恒为 `false`（函数与元素的分界）。
- `def_site_key` 逐级拼接（函数名 / `Parent::func` / `Parent::func::param`）配合 `file!()`，支撑 IDE 跳转与热重载定位。

---

## 7. 下一步学习建议

本讲完成了 `#[func]` 的「解析（u3-l1）→ 生成（u3-l2）」闭环。建议：

1. **学习 u3-l3 `#[scope]`**：那里会用到本讲生成的 `{ident}_data()` 函数与 `<#ident as NativeScope>::scope()`，把多个 `#[func]`、`#[ty]`、`#[elem]`、常量、构造器组装进一个作用域。届时你会看到 `parent` 这条线如何闭环。
2. **预习 u4 `#[elem]`**：元素宏与本讲结构高度同构（都有 create / 字段 vtable / Construct-Set 解析），但字段模型更丰富、且有 `settable`。对比阅读能加深对「过程宏生成 + 运行时 trait 契约」这套架构的理解。
3. **延伸阅读**：在 typst-library 里读 `NativeFuncData`、`NativeFuncPtr`、`Args`（`all/named/eat/expect/take/finish`）、`IntoResult` 的实现，理解胶水闭包调用的那些运行时方法到底做了什么——这能让你从「宏视角」过渡到「运行时视角」。
