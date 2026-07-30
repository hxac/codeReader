# #[scope] 作用域组装

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `#[scope]` 把一个 `impl` 块「编译」成什么：一段改写后的 impl + 一个 `NativeScope` 实现。
- 解释四类成员（`const` / `fn` / `type` / `elem`）分别走哪条分发分支，最终生成哪条 `scope.define*` 调用。
- 说明 `#[func]` 方法是如何被 `#[scope]` 注入 `parent = Self` 的，以及 `constructor` 方法为何不进 `definitions`、而是成为 `NativeScope::constructor()` 的返回值。
- 理解 `#[scope(ext)]` 模式为何必须把 inherent impl 改写成「trait + trait impl」，以及 `rewrite_primitive_base` 具体做了什么。
- 看懂 `#[deprecated]` 属性如何被翻译成 `.deprecated(...)` 链式调用。

本讲承接 u3-l2：上讲我们让 `#[func]` 在「有 parent」时生成 `{ident}_data()` 函数作为注册件，本讲就来看 `#[scope]` 是如何收集这些注册件、把它们组装进一个 `Scope` 的。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：作用域（Scope）是一张「名字 → 值」的表。** 在 Typst 运行时里，`Scope` 是一个映射，存放常量、函数、类型、元素等。类型本身（比如 `str`、`array`）可以挂一个「附属作用域」，里面装着它的方法（如 `str.len`、`array.push`）。`#[scope]` 的职责，就是帮某个类型自动生成这张表。

**直觉二：`impl` 块是「挂载点」。** 在 Rust 里，类型的附属方法都写在 `impl Foo { ... }` 里。Typst 复用这一约定：你想给 Typst 类型 `Foo` 加方法，就写一个 `impl Foo`，并在上面标 `#[scope]`。宏会遍历这个 impl 的每一项，决定它对应 Scope 里的哪条记录。

**直觉三：过程宏不执行代码，只改写代码。** `#[scope]` 看到的是一个语法树（AST），它做的事是：读 AST → 改写 AST（给每个 `#[func]` 注入 parent）→ 生成一段新的 AST（`NativeScope` 实现）。它本身不创建任何运行时对象，运行时的 `Scope::deduplicating()` 才在「真正调用 `scope()` 时」才执行。

**为什么需要 parent 注入？** 回顾 u3-l2：`#[func]` 在「没有 parent」时会生成一个与函数同名的空 `enum`（因为 `#[scope]` impl 块内不能定义新类型）；在「有 parent」时改为生成 `{ident}_data()` 函数。那么 parent 从哪来？正是 `#[scope]` 在遍历时自动注入的——用户自己写 `#[func]` 时根本不知道 parent 是谁。这就是两讲之间的契约接口。

下表是本讲会反复用到的运行时 trait 与方法（定义在 `typst-library` 里，`#[scope]` 生成的代码必须满足它们）：

| 运行时契约 | 位置 | 作用 |
|---|---|---|
| `NativeScope` trait | typst-library | 声明 `constructor()` 与 `scope()` 两个方法 |
| `Scope::define(name, value)` | typst-library | 注册常量 |
| `Scope::define_func_with_data(data)` | typst-library | 用裸数据注册函数 |
| `Scope::define_type::<T>()` | typst-library | 注册类型 |
| `Scope::define_elem::<T>()` | typst-library | 注册元素 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/scope.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs) | 本讲主角，全部逻辑都在这一个文件里（约 228 行）。 |
| [src/func.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs) | 提供 `func::Meta`（解析 `#[func(constructor)]` 等参数）与 `{ident}_data` 的生成方式，是 `#[scope]` 注入 parent 的下游消费者。 |
| [src/util.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs) | 提供 `foundations` 路径简写、`kw::ext` 关键字、`parse_flag`、`BareType`（解析裸 `type Name;`）等共享工具。 |
| typst-library/.../foundations/scope.rs | 运行时的 `Scope` 与 `NativeScope` 定义，宏生成的代码最终调用这里的 API。 |

---

## 4. 核心概念与源码讲解

### 4.1 入口与分发：scope::scope 与四类成员

#### 4.1.1 概念说明

`#[scope]` 是一个属性宏，它装饰一个 `impl` 块。它的整体任务可以用一句话概括：**把 impl 块里的成员翻译成一串 `scope.define*` 语句，再外面包一个 `NativeScope` 实现。**

它接受一个可选的括号参数 `ext`（形如 `#[scope(ext)]`），用来开启「扩展 trait 模式」，我们放在 4.5 讲。本模块先讲主流程，并默认 `ext = false`。

#### 4.1.2 核心流程

```
#[scope] impl Foo { 四类成员 }
        │
        ▼
1. 解析括号参数 → Meta { ext }
2. 校验被装饰项必须是 Item::Impl
3. 计算 self_ty_expr（ext 模式下会变成 <Foo as FooExt>）
4. for 每个成员:
       const  ──► handle_const      ──► scope.define(name, Foo::C)
       fn     ──► handle_fn         ──► scope.define_func_with_data(Foo::f_data())
                                      或（constructor）→ 赋给 constructor 变量
       type   ──► handle_type_or_elem ──► scope.define_type::<T>()
       elem   ──► handle_type_or_elem ──► scope.define_elem::<T>()
       其它   ──► bail!
       （顺带处理每项的 #[deprecated]）
5. 从 AST 中删除 Verbatim 项（裸 type 声明已被消费）
6. 生成 base：原 impl 块（成员里的 #[func] 已被改写）
7. 生成 impl NativeScope for Foo { constructor(); scope() }
```

关键点：`constructor` 与 `definitions` 是两个独立的累加器。普通方法进 `definitions`（在 `scope()` 里逐条注册），constructor 方法不进 `definitions`，而是进 `constructor` 变量（在 `constructor()` 里返回）。

#### 4.1.3 源码精读

入口签名（属性宏标准双参 `stream` + `item`）：

[src/scope.rs:11-15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L11-L15) —— 先把括号参数解析成 `Meta`，再用 `let ... else` 强制 `item` 必须是 `Impl`，否则 `bail!` 报「expected module or impl item」。

[src/scope.rs:33-34](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L33-L34) —— 准备两个累加器：`definitions`（一个 `TokenStream` 列表）与 `constructor`（初值是 `quote! { None }`，即「没有构造器」）。

[src/scope.rs:35-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L35-L56) —— 核心分发循环，对 `item.items` 里每一项 `match`，四种合法成员各走一个 helper，其它直接 `bail!(child, "unexpected item in scope")`。注意 `fn` 分支内部又 `match` 了 `FnKind`：constructor 会 `continue` 跳过 `definitions.push`。

[src/scope.rs:100-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L100-L115) —— 最终输出。先原样吐出 `#base`（改写后的 impl），再生成 `impl NativeScope for #self_ty`：`constructor()` 直接返回累加好的 `#constructor`；`scope()` 里 `let mut scope = Scope::deduplicating();` 后用 `#(#definitions;)*` 逐条展开注册语句，最后 `return scope`。`#[expect(deprecated)]` 是为了在 4.4 讲的弃用处理里 suppression 编译器对弃用项的告警。

`Meta` 的解析极其简单——只有一个 `ext` 标志位：

[src/scope.rs:118-129](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L118-L129) —— `Meta` 只有 `ext: bool` 一个字段，由 u1-l3 讲过的 `parse_flag::<kw::ext>` 解析（`kw::ext` 是 util.rs 里的自定义关键字）。

#### 4.1.4 代码实践

**实践目标**：确认入口的「分流」行为。

**操作步骤**：

1. 打开 [src/scope.rs:55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L55)，看 `_ => bail!(child, "unexpected item in scope")` 这条兜底分支。
2. 想象在 `#[scope] impl Foo { static X: u32 = 0; }` 里塞了一个 `static` 项（`ImplItem` 里有 `Macro`、`Verbatim` 等，但没有合法的静态项变体——静态项本就不允许出现在 impl 里）。

**需要观察的现象**：由于 `static`/`macro!` 等不属于四个被处理的分支，编译时会触发 `bail!`，得到一条 `typst: unexpected item in scope` 的编译错误。

**预期结果**：理解 `#[scope]` 对 impl 块的内容有严格白名单（const/fn/type/elem），其余一律拒绝。这是「快速失败」设计。

#### 4.1.5 小练习与答案

**练习 1**：如果用户写 `#[scope] struct Foo { }`（标在了 struct 而非 impl 上），会发生什么？

**答案**：在 [src/scope.rs:13-15](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L13-L15) 的 `let ... else` 处，`item` 不是 `Item::Impl`，于是 `bail!(item, "expected module or impl item")`，得到编译错误。

**练习 2**：`scope()` 方法体里第一行创建的 `Scope` 为什么用 `deduplicating()` 而不是普通 `new()`？

**答案**：`deduplicating` 让该 Scope 在 debug 构建下对重复名字做断言（见 typst-library 的 `Scope::define`，`#[cfg(debug_assertions)]` 时若名字已存在则 panic）。这是为了在编译期尽早发现「同一个 impl 里把两个方法注册成了同一个 Typst 名字」的笔误。

---

### 4.2 常量、类型、元素：handle_const 与 handle_type_or_elem

#### 4.2.1 概念说明

这是两类最简单的成员：

- **常量（const）**：直接以它的值注册进 Scope，名字是 Rust 标识符的 kebab-case。比如 `const INF: f64 = ...;` 在 Typst 里就是 `float.inf`。
- **类型/元素（type/elem）**：这两者在 impl 块里都写成「裸类型声明」`type Name;`（由 util.rs 的 `BareType` 解析，见 u2-l2）。区别只在于有没有 `#[elem]` 属性：有就走 `define_elem`，没有就走 `define_type`。

#### 4.2.2 核心流程

```
const C: T = v;          →  handle_const  →  scope.define("c", Foo::C)
type Bar;                →  handle_type_or_elem（无 #[elem]）→  scope.define_type::<Bar>()
#[elem] type NiceElem;   →  handle_type_or_elem（有 #[elem]）→  scope.define_elem::<NiceElem>()
```

注意常量注册用的是 `self_ty_expr`（不是 `self_ty`），这是为了 ext 模式下能通过 trait 路径访问；非 ext 模式下两者相同，详见 4.5。

#### 4.2.3 源码精读

`handle_const` 极短：

[src/scope.rs:131-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L131-L136) —— 把标识符转 kebab-case 作为名字（`ToKebabCase`，如 `VAL` → `"val"`、`INFINITY` → `"infinity"`），值通过 `#self_ty::#ident` 引用。

`handle_type_or_elem` 同样很短，靠 `#[elem]` 属性分流：

[src/scope.rs:138-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L138-L147) —— 用 `item.attrs.iter().any(|attr| attr.path().is_ident("elem"))` 判断是不是元素，决定调用 `define_elem` 还是 `define_type`。注意它接收的是 `&BareType`（裸类型声明），`#ident` 就是类型名。

这两条生成语句最终会被注入到 `scope()` 里（参考运行时 API）：

- [typst-library/.../scope.rs:176-185](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L176-L185) `Scope::define`（常量）
- [typst-library/.../scope.rs:153-163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L153-L163) `define_type` / `define_elem`（都是泛型方法，靠 trait `NativeType` / `NativeElement` 取数据）

#### 4.2.4 代码实践

**实践目标**：把真实项目里的常量注册和源码对应起来。

**操作步骤**：

1. 打开 [typst-library/.../float.rs:34-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/float.rs#L34-L39)，看到 `impl f64` 里有 `const INF: f64 = f64::INFINITY;` 和 `const NAN: f64 = f64::NAN;`。
2. 对照 `handle_const` 的逻辑，手算它们注册后的名字。

**预期结果**：`INF` → `"inf"`，`NAN` → `"nan"`。于是在 Typst 里可以写 `float.inf`、`float.nan`，对应宏生成的 `scope.define("inf", <f64 as f64Ext>::INF)`（注意这里是 ext 模式，所以走 trait 路径，4.5 讲会解释）。

#### 4.2.5 小练习与答案

**练习**：为什么类型和元素在 impl 块里都写成 `type Name;`（裸声明），而不是完整的 `struct Name { ... }`？

**答案**：因为真正的类型/元素定义在 impl 块**外面**（由 `#[ty]`/`#[elem]` 装饰的 struct 生成，见 u2-l2 和 u4）。impl 块里只需要一个「占位声明」告诉 `#[scope]`「请把外部那个 `Name` 注册到这个作用域」。这个占位声明用 util.rs 的 `BareType` 解析（[src/util.rs:251-268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L251-L268)），并在主循环末尾被 `item.items.retain(...)` 删除（[src/scope.rs:93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L93)），因为它只是给宏看的「指令」，不该出现在最终代码里。

---

### 4.3 函数：handle_fn、FnKind、parent 注入与 constructor 识别

#### 4.3.1 概念说明

函数是 `#[scope]` 处理起来最复杂的一类成员，因为它要把两件事串起来：

1. **给 `#[func]` 注入 parent**：用户在 impl 里写的是裸 `#[func]`，但 u3-l2 讲过，「有 parent」时 `#[func]` 才会生成 `{ident}_data()` 而非同名 enum。所以 `#[scope]` 必须把每个 `#[func]` 改写成 `#[func(parent = Self)]`。
2. **区分普通方法与构造器**：`#[func(constructor)]` 标注的方法是「类型的构造函数」（在 Typst 里就是 `Foo(...)` 这个调用本身），它不进 `definitions`，而是被 `constructor()` 返回。

`FnKind` 就是用来区分这两种归宿的中间结构。

#### 4.3.2 核心流程

```
fn 方法 + #[func]
   │
   ├─ 找不到 #[func]? → bail!("scope function is missing #[func] attribute")
   │
   ├─ #[func]（Path 形式）      → 改写为 #[func(parent = Self)]
   │                              → 返回 FnKind::Member(define_func_with_data 调用)
   │
   └─ #[func(...)]（List 形式） → 解析里面的 func::Meta
        ├─ 追加 ", parent = Self"
        ├─ 若 meta.constructor == true → 返回 FnKind::Constructor(Some(Self::f_data()))
        └─ 否则                         → 返回 FnKind::Member(define_func_with_data 调用)
```

主循环里收到 `FnKind::Constructor` 就 `continue`（不 push 进 definitions，改写 constructor 变量）；收到 `FnKind::Member` 就把它的 token 作为一条 definition。

#### 4.3.3 源码精读

`handle_fn` 的全貌：

[src/scope.rs:149-175](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L149-L175) —— 先用 `item.attrs.iter_mut().find(... "func")` 定位 `#[func]` 属性，找不到就 `bail!`；这一步保证「ext 模式下 impl 里每个 fn 都必须是 func」，是 4.5 讲 `rewrite_primitive_base` 能放心为每个 fn 生成 `_data` 声明（[src/scope.rs:201-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L201-L205)）的前提。

parent 注入分两种 `#[func]` 写法（[src/scope.rs:159-172](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L159-L172)）：

- **裸 `#[func]`**（`syn::Meta::Path`）：直接整体替换为 `#[func(parent = #self_ty)]`。
- **`#[func(...)]`**（`syn::Meta::List`）：先把括号里的 tokens 解析成 `crate::func::Meta`（[src/func.rs:102-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L102-L136)），再用 `list.tokens = quote! { #tokens, parent = #self_ty }` 在原参数末尾追加 `parent`。若解析出的 `meta.constructor` 为真，就 `return Ok(FnKind::Constructor(...))`。

constructor 的最终取值（[src/scope.rs:168](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L168)）：

```rust
FnKind::Constructor(quote! { Some(#self_ty::#ident_data()) })
```

其中 `ident_data = format_ident!("{}_data", item.sig.ident)`（[src/scope.rs:157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L157)）。也就是 `Some(Foo::construct_data())`。这个 `construct_data()` 正是 u3-l2 讲过的、`#[func]` 在「有 parent」时生成的数据函数（[src/func.rs:268-277](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L268-L277)）。

`FnKind` 与普通方法的注册（[src/scope.rs:174-180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L174-L180)）：

```rust
enum FnKind {
    Constructor(TokenStream),
    Member(TokenStream),
}
```

普通方法（Member）生成 `scope.define_func_with_data(#self_ty::#ident_data())`，交给运行时 [typst-library/.../scope.rs:144-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L144-L149) 的 `define_func_with_data` 注册。

**constructor 的名字特殊处理**：在 `#[func]` 侧（[src/func.rs:331-335](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L331-L335)），当 `constructor == true` 时，函数对外暴露的名字不是 Rust 方法名，而是 `<parent as NativeType>::NAME`（即类型自己的名字）。这就是为什么 `impl f64` 里的 `fn construct` 在 Typst 里表现为 `float(...)` 而不是 `float.construct(...)`——构造器「吞掉」了类型名。

#### 4.3.4 代码实践

**实践目标**：跟踪真实构造器的注入链路。

**操作步骤**：

1. 打开 [typst-library/.../float.rs:59-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/float.rs#L59-L65)，看到 `#[func(constructor, since = "forever")] pub fn construct(value: ToFloat) -> f64`。
2. 走一遍 `handle_fn`：这是 List 形式，解析出 `meta.constructor = true`，于是返回 `FnKind::Constructor(Some(<...>::construct_data()))`（`<...>` 是 ext 模式的 self_ty_expr）。
3. 主循环把它赋给 `constructor` 变量并 `continue`，所以 `scope()` 里**不会**出现 `construct` 的注册语句，它只作为 `constructor()` 的返回值存在。

**预期结果**：理解构造器是「特殊公民」——它不在作用域里作为普通方法出现，而是成为类型本身的调用入口。若你在此处把 `constructor` 误改成普通 `#[func]`，Typst 里 `float(...)` 将无法构造。

#### 4.3.5 小练习与答案

**练习 1**：用户写 `#[func]` 时并没有写 `parent`，这个字段在 `func::Meta::parse` 里如何被解析？

**答案**：在 [src/func.rs:133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L133)，`parent: parse_key_value::<kw::parent, _>(input)?`，缺省时就是 `None`。`#[scope]` 的职责正是在用户写完之后，把 `None` 补成 `Some(Self)`。

**练习 2**：为什么 `#[func]` 的 List 分支要先解析一遍 `func::Meta`，而不是直接在 tokens 后面拼 `parent`？

**答案**：因为需要读出 `meta.constructor` 来决定返回 `Constructor` 还是 `Member`（[src/scope.rs:165-169](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L165-L169)）。拼接本身确实只是 `quote! { #tokens, parent = #self_ty }`（[src/scope.rs:166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L166)），解析是为分流服务的。

---

### 4.4 #[deprecated] 属性处理

#### 4.4.1 概念说明

impl 块里的成员可以带 `#[deprecated]`，表示这个 Typst 函数/常量已被弃用。`#[scope]` 在生成每条 definition 后，会检查该项是否带这个 Rust 标准属性，若有，就给 definition 链式追加一个 `.deprecated(...)` 调用。这是把「Rust 的弃用标记」翻译成「Typst 运行时的弃用元数据」。

它支持两种 Rust `#[deprecated]` 写法：

| Rust 写法 | 翻译结果 |
|---|---|
| `#[deprecated = "msg"]`（NameValue） | `def.deprecated("msg")` |
| `#[deprecated(message = "...", until = "...")]`（List） | `def.deprecated(Deprecation::new().with_message(...).with_until(...))` |

#### 4.4.2 核心流程

```
for 每个成员生成 def 后：
    if 项带 #[deprecated]:
        match 属性形态:
            NameValue  → def = def.deprecated(#message)
            List       → 解析 message/until → 拼 Deprecation::new().with_*(...) → def = def.deprecated(#deprecation)
            其它       → 忽略（_ => {}）
    definitions.push(def)
```

#### 4.4.3 源码精读

[src/scope.rs:58-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L58-L88) —— 用 `attrs.iter().find(... "deprecated")` 定位属性，然后 `match &attr.meta` 分三种。

注意几个细节：

- [src/scope.rs:60-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L60-L63) NameValue 分支直接把整个 `#message`（一个 `Expr`）塞进 `deprecated(#message)`。
- [src/scope.rs:64-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L64-L85) List 分支用 `Punctuated::<MetaNameValue, Token![,]>::parse_separated_nonempty` 解析键值对，再分别 `find_map` 找 `message` 和 `until`，链式拼到 `Deprecation::new()` 上。`until` 之外的键会被静默忽略。
- 这段逻辑对 `const`/`fn`/`type`/`elem` **所有四类**成员都生效，因为它在分发 `match` 之后、`push` 之前统一执行（`attrs` 是从各分支一起带出来的）。

正因为生成代码里会用到被弃用的项，`scope()` 方法本身才带 `#[expect(deprecated)]`（[src/scope.rs:108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L108)），避免 Rust 编译器对「在宏生成代码中引用了弃用项」报警。

#### 4.4.4 代码实践

**实践目标**：理解弃用翻译的两种形态差异。

**操作步骤**：

1. 假设给某个 `#[func]` 方法同时加上 `#[deprecated(message = "use bar", until = "1.0.0")]`。
2. 对照 List 分支，写出它生成的链式调用。

**预期结果**：`scope.define_func_with_data(Foo::foo_data()).deprecated(crate::foundations::Deprecation::new().with_message("use bar").with_until("1.0.0"))`。注意 `Deprecation` 用的是 `crate::foundations::` 前缀（在 typst-library 内部），而 `scope` 等用的是 `#foundations`（即 `::typst_library::foundations`）简写——这是因为在 typst-library crate 内部，`crate::foundations` 更直接。

> 待本地验证：不同 crate 里 `Deprecation` 的实际路径前缀可能略有差异，可在 typst-library 中 `cargo expand` 一个带 `#[deprecated]` 的 `#[scope]` impl 来确认展开结果。

#### 4.4.5 小练习与答案

**练习**：为什么 NameValue 分支生成的是 `deprecated(#message)`，而 List 分支生成的是 `deprecated(#deprecation)`（一个 `Deprecation` 对象）？二者类型不一致会编译失败吗？

**答案**：不会失败。这说明运行时 `Binding`（或对应的 builder）上的 `deprecated` 方法被**重载**了：既能接受一个字符串字面量（NameValue 的简写形式），也能接受一个完整的 `Deprecation` 对象（List 的完整形式）。宏只是忠实地把两种 Rust 语法翻译成两种调用形态，由运行时方法签名兜底。

---

### 4.5 ext 扩展模式与 rewrite_primitive_base

#### 4.5.1 概念说明

这是本讲最有意思的设计。问题来源是 Rust 的一条硬规则：

> **不能对外部类型写 inherent impl（固有实现）。** 也就是你不能在自己 crate 里写 `impl f64 { ... }` 或 `impl str { ... }`，因为 `f64`/`str` 是标准库定义的，inherent impl 必须与类型定义在同一个 crate。

但 Typst 需要给原始类型 `f64`（Typst 的 `float`）挂方法和常量！怎么办？标准答案是用**扩展 trait（extension trait）**：

```rust
trait f64Ext { fn foo(self); }
impl f64Ext for f64 { fn foo(self) { ... } }
```

trait impl 不受「同 crate」限制（受的是更宽松的 orphan rule，而 `f64Ext` 是你自己定义的，所以满足）。这就是 `#[scope(ext)]` 的全部动机。

`#[scope(ext)]` 让宏自动完成「inherent impl → trait + trait impl」的改写，用户**写的还是熟悉的 `impl f64 { ... }`**，宏在背后把它变成 `trait f64Ext { ... } impl f64Ext for f64 { ... }`。

#### 4.5.2 核心流程

```
ext = true 且 self_ty 是单个标识符（如 f64）
   → primitive_ident_ext = Some(format_ident!("{}Ext", "f64")) = Some(f64Ext)
   → self_ty_expr = <f64 as f64Ext>   // 之后所有 handle_const / handle_fn 都用这个路径

改写 impl 块（rewrite_primitive_base）：
   trait f64Ext {
       方法签名;                        // 每个原方法一个
       fn construct_data() -> &'static NativeFuncData;   // 每个方法额外声明 _data
       const INF: f64 = ...;           // 常量直接搬进 trait
   }
   impl f64Ext for f64 {
       （原方法体，可见性改为私有，#[func] 仍待展开）
   }
```

随后 Rust 继续展开里面的 `#[func]`，由 u3-l2 的逻辑在每个 `impl f64Ext for f64` 块内生成对应的 `fn construct_data()` 实现——正好满足 trait 里声明的 `_data` 要求。

#### 4.5.3 源码精读

ext 标志的判定与命名（[src/scope.rs:19-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L19-L31)）：只有当 `meta.ext` 为真**且** `self_ty` 是单个路径标识符时，才生成 `{ident}Ext`。`self_ty_expr` 据此二选一——这是 `handle_const`（常量访问）和 `handle_fn`（`_data` 调用）所引用 `Self` 的统一入口。

`base` 的选择（[src/scope.rs:95-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L95-L98)）：非 ext 模式直接用原 impl；ext 模式调用 `rewrite_primitive_base`。

`rewrite_primitive_base` 全貌（[src/scope.rs:182-227](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L182-L227)），它遍历原 impl 的每一项，构造两个集合：

- `sigs`（trait 体）：
  - `ImplItem::Fn` → 推入原方法签名 `#sig;`（[src/scope.rs:202](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L202)），再推入 `_data` 声明 `fn #ident_data() -> &'static NativeFuncData;`（[src/scope.rs:203-205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L203-L205)）。注意签名构造时会清掉参数上的属性（[src/scope.rs:193-198](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L193-L198)），因为像 `#[named]` 这类属性不该出现在 trait 声明里。
  - `ImplItem::Const` → 整个 const 推入 trait（[src/scope.rs:208-210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L208-L210)）。
- `items`（impl 体）：
  - `ImplItem::Fn` → 把方法可见性改为 `Inherited`（私有，[src/scope.rs:189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L189)）后推入。`_data` 的实现**不在这里**，它由后续 `#[func]` 展开生成。

最终输出（[src/scope.rs:217-226](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L217-L226)）：

```rust
#[expect(non_camel_case_types)]
trait f64Ext {
    #(#sigs)*       // 方法签名 + _data 声明 + 常量
}
impl f64Ext for f64 {
    #(#items)*      // 原方法体（私有，带未展开的 #[func]）
}
```

`#[expect(non_camel_case_types)]` 是因为 trait 名 `f64Ext` 不符合 Rust 的 CamelCase 规范（以小写数字开头），需要显式抑制告警。

#### 4.5.4 代码实践

**实践目标**：对照真实代码理解 ext 改写的必要性。

**操作步骤**：

1. 打开 [typst-library/.../float.rs:29-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/float.rs#L29-L33)，看到用户写的是 `type f64;`（由 `#[ty]` 装饰）加上 `#[scope(ext)] impl f64 { ... }`。
2. 想象宏展开后的样子：`trait f64Ext { fn construct(...); fn construct_data() -> &'static NativeFuncData; const INF: f64 = ...; const NAN: f64 = ...; ... }` 和 `impl f64Ext for f64 { ... }`。
3. 回答：为什么作者必须写 `#[scope(ext)]` 而不是普通 `#[scope]`？

**需要观察的现象 / 预期结果**：因为 `f64` 是标准库类型，typst-library 不能对它写 `impl f64 { ... }`（inherent impl 的「同 crate」限制），Rust 编译器会直接报 `error[E0118]: no nominal type available for using inherent implementations`。ext 模式把它改写成 trait，才绕过这条限制。这就是本讲综合实践题第二问的答案。

> 待本地验证：可在 typst-library 里临时把 `#[scope(ext)]` 改成 `#[scope]`，运行 `cargo check`，应能看到上述 `E0118`（或类似）错误，从而亲眼确认 ext 模式的必要性（验证后请还原改动，不要提交）。

#### 4.5.5 小练习与答案

**练习 1**：ext 模式下，常量 `INF` 是如何被 `scope.define("inf", ...)` 访问到的？

**答案**：`handle_const` 用的是 `self_ty_expr`，在 ext 模式下等于 `<f64 as f64Ext>`（[src/scope.rs:28-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L28-L31)），所以生成 `<f64 as f64Ext>::INF`。因为 `INF` 现在是 trait 常量，必须用 `<Type as Trait>` 的完全限定语法才能消歧。这正是 `self_ty_expr` 存在的全部理由。

**练习 2**：为什么 `rewrite_primitive_base` 给每个方法都生成一个 `_data` 声明放进 trait？

**答案**：因为 ext 模式下 `#[func]` 生成的 `_data` 函数不再是「自由函数」而是要落在 `impl f64Ext for f64` 块内成为**关联函数**。trait 必须先声明这个关联函数（无默认实现），impl 才能提供它。这样 `handle_fn` 生成的 `Some(<f64 as f64Ext>::construct_data())` 才能通过 trait 路径解析到。

---

## 5. 综合实践

把本讲的知识串起来。给定如下 impl 块（非 ext 模式，假设 `Foo` 已是 `#[ty]` 类型）：

```rust
#[scope]
impl Foo {
    /// A simple constant.
    const VAL: u32 = 0;

    /// A function.
    #[func]
    fn foo() -> EcoString {
        "foo!".into()
    }

    /// A constructor.
    #[func(constructor)]
    fn make() -> Foo {
        Foo
    }
}
```

**任务一：写出 `scope()` 方法体里的 `definitions` 序列与 `constructor` 的最终取值。**

参考解答（这是宏展开后 `impl NativeScope for Foo` 的等价形态，标注为示例代码）：

```rust
// 示例代码：#[scope] 展开后的核心部分（省略 #[doc(hidden)] 等次要标注）
impl Foo {
    const VAL: u32 = 0;

    #[func(parent = Foo)]            // ← 裸 #[func] 被注入 parent
    fn foo() -> EcoString { "foo!".into() }

    #[func(constructor, parent = Foo)] // ← List 形式追加 ", parent = Foo"
    fn make() -> Foo { Foo }
}

impl ::typst_library::foundations::NativeScope for Foo {
    fn constructor() -> Option<&'static ::typst_library::foundations::NativeFuncData> {
        // constructor 变量的最终取值：构造器不进 definitions
        Some(Foo::make_data())
    }

    fn scope() -> ::typst_library::foundations::Scope {
        let mut scope = ::typst_library::foundations::Scope::deduplicating();
        scope.define("val", Foo::VAL);                       // ← handle_const（VAL→"val"）
        scope.define_func_with_data(Foo::foo_data());        // ← handle_fn（Member）
        // 注意：make 没有出现在这里，它是构造器
        scope
    }
}
```

要点核对：

1. `definitions` 序列有 **2 条**：`define("val", ...)` 与 `define_func_with_data(Foo::foo_data())`。顺序与源码中成员顺序一致。
2. `constructor` 取值为 **`Some(Foo::make_data())`**。`make` 因为是构造器被 `continue` 跳出，不进 `definitions`。
3. 两个 `#[func]` 都被注入了 `parent = Foo`，于是它们各自生成 `foo_data()` / `make_data()`（而非同名 enum），这正是 u3-l2 的契约。

**任务二：解释 ext 模式为何必须生成一个 `XxxExt` trait 而不能直接用 inherent impl。**

参考解答：因为 ext 模式专门用于**外部定义的类型**（如 `f64`、`str` 这类标准库原始类型）。Rust 规定 inherent impl（`impl SomeType { ... }`）必须与类型定义位于同一 crate（[RFC 2451](https://rust-lang.github.io/rfcs/2451-re-rebalancing-coherence.html) 中关于 inherent impl 的约束），否则报 `E0118`。由于 typst-library 无权修改 `f64` 的定义，就不能写 `impl f64`。改用扩展 trait `f64Ext` + `impl f64Ext for f64` 则满足 orphan rule（trait 本身是自己 crate 定义的），从而合法。`rewrite_primitive_base` 自动完成这一改写，让用户仍以熟悉的 `impl f64` 语法书写。

> 进阶观察（可选）：用 `cargo expand`（需安装 `cargo-expand`）在 typst-library 里展开 `float.rs`，对比 `#[scope(ext)]` 与普通 `#[scope]`（如 `decimal.rs` 的 `impl Decimal`）展开后的差异——前者会出现 `trait f64Ext`，后者直接是 `impl Decimal`。

## 6. 本讲小结

- `#[scope]` 把一个 `impl` 块编译成「改写后的 impl + `impl NativeScope for Self`」两部分；`NativeScope` 提供 `constructor()` 与 `scope()`。
- 四类成员各有分发分支：`const` → `handle_const` → `scope.define`；`fn` → `handle_fn`；`type`/`elem` → `handle_type_or_elem` → `define_type`/`define_elem`（靠 `#[elem]` 属性区分）。
- `handle_fn` 的核心动作是给每个 `#[func]` **注入 `parent = Self`**（裸 `#[func]` 整体替换、`#[func(...)]` 末尾追加），这是与 u3-l2 `#[func]` 生成 `_data` 的接口契约。
- 用 `FnKind` 区分普通方法与构造器：普通方法进 `definitions`（`define_func_with_data`），构造器进 `constructor` 变量（`Some(Self::f_data())`），不进作用域。
- `#[deprecated]` 在每条 definition 后统一链式追加 `.deprecated(...)`，支持字符串简写与 `Deprecation` 对象两种形态。
- `#[scope(ext)]` 用 `rewrite_primitive_base` 把对原始类型（如 `f64`）的 inherent impl 改写成 `trait XxxExt + impl XxxExt for T`，绕过 Rust「inherent impl 必须同 crate」的限制。

## 7. 下一步学习建议

- **进入 u4 元素宏系列**：`#[scope]` 已经能注册 `#[elem]` 类型（4.2 讲的 `define_elem`），但元素本身的字段模型、vtable、Construct/Set 远比函数复杂。建议接着读 u4-l1「#[elem]（一）：字段模型与属性校验」。
- **回看运行时侧**：本讲多次引用了 typst-library 的 `Scope`/`NativeScope`。如果你想从「宏生成代码」切到「运行时如何使用这些代码」，可以去读 `crates/typst-library/src/foundations/scope.rs` 的 `NativeScope` trait 和 `define*` 系列方法。
- **动手实验**：在 typst-library 挑一个简单的 `#[scope]` impl（如 `duration.rs`），用 `cargo expand` 展开它，逐行把展开结果与本讲的「分发 → 注入 → 组装」流程对应起来，这是巩固本讲最有效的方式。
