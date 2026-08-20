# 过程宏基础：syn、quote 与三类宏入口

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确区分 Rust 过程宏的三种形式——派生宏（derive）、属性宏（attribute）、函数式宏（function-like），并能说出它们在 `gpui_macros.rs` 中分别用 `#[proc_macro_derive]`、`#[proc_macro_attribute]`、`#[proc_macro]` 哪种方式声明。
2. 理解 `proc-macro2` / `syn` / `quote` 三个依赖各自负责什么，掌握「解析 → 语法树 → 模板拼接 → 交还编译器」这条过程宏标准工作流。
3. 学会安装和使用 `cargo expand`，亲眼看到 `#[derive(Render)]` 展开后生成的真实代码，并能把它与 `derive_render.rs` 中的 `quote!` 模板逐行对照。

本讲全程以 crate 中代码量最小的 `derive_render.rs`（21 行）作为解剖标本——它麻雀虽小，却包含了一个派生宏需要的全部要素。

## 2. 前置知识

### 2.1 宏是「编译期函数」

普通函数在程序运行时被调用；宏在程序**编译时**被 rustc 调用。过程宏（procedural macro）接收的是「源代码的 token 流」，返回的也是「token 流」——rustc 拿到你返回的 token 流，继续编译。所以可以把过程宏理解为：

> 输入一段代码字符串（的 token 化表示），输出另一段代码的「编译期函数」。

这也解释了上一讲（u1-l1）的一个结论：`gpui_macros` 被编译成动态库，由 rustc 在编译**其他 crate** 时加载，它自己不参与任何运行时。

### 2.2 token 与 TokenStream

编译器读源码的第一步是词法分析：把 `impl Foo for Bar { }` 切成 `impl`、`Foo`、`for`、`Bar`、`{`、`}` 这样的 token。`TokenStream` 就是一串 token 的容器。过程宏的输入输出都是它。

### 2.3 声明式宏与过程宏

Rust 有两套宏系统：

- **声明式宏**（`macro_rules!`）：模式匹配式展开，能力有限。GPUI 中的 `actions!` 宏（定义在 gpui crate 内部）就属于这类。
- **过程宏**：能拿到完整语法树，用 Rust 代码任意加工。`gpui_macros` 里**全部 19 个宏入口都是过程宏**。

### 2.4 三个运行依赖的分工

回顾 `Cargo.toml`（u1-l1 已讲过），本 crate 运行依赖只有 4 个，其中与「写宏」直接相关的三个是：

| crate | 分工 | 通俗理解 |
| --- | --- | --- |
| `proc-macro2` | 提供稳定的 `TokenStream` 类型（编译器自带的那套不能脱离 rustc 使用） | 地基 |
| `syn` | 把 token 流解析成 Rust 语法树（如 `DeriveInput`），也提供反向解析 | 解析器 |
| `quote` | 提供 `quote! { ... }` 宏，把模板代码加上 `#变量` 插值变成 token 流 | 代码生成器 |

（第四个 `heck` 只做命名格式转换，如驼峰转蛇骨，本讲不涉及。）

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `crates/gpui_macros/src/gpui_macros.rs` | 库入口：声明全部 19 个宏并转发到实现模块 | 观察三类过程宏的**声明语法** |
| `crates/gpui_macros/src/derive_render.rs` | `#[derive(Render)]` 的实现，全 crate 最小的实现文件 | 逐行精读 syn 解析 + quote 生成 |
| `crates/gpui_macros/tests/render_test.rs` | 7 行集成测试，验证 Render 派生可编译 | 作为 `cargo expand` 的观察对象 |
| `crates/gpui_macros/Cargo.toml` | `[lib] path` 与 `proc-macro = true` 配置 | 呼应 u1-l1，不重复展开 |

永久链接 base 为 `https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/`，下文所有链接均基于该 HEAD。

## 4. 核心概念与源码讲解

### 4.1 三类过程宏与 gpui_macros 的 19 个入口

#### 4.1.1 概念说明

Rust 过程宏恰好有三种形式，它们的差别在于「怎么被使用」和「函数签名长什么样」：

1. **派生宏**：写在 `#[derive(...)]` 里，附着在 struct/enum 上。输入是**整个类型定义**，输出会被**追加**在原条目之后——也就是说，原 struct 仍然存在，你生成的 `impl` 块只是额外拼上去。
2. **属性宏**：形如 `#[gpui::test]`，可以标注函数、trait 等条目。签名多一个参数：`(args, item)` 分别是括号里的参数和被标注的条目。它的输出会**完全替换**原条目——如果想保留原函数，必须自己把 `#item` 重新放进输出。
3. **函数式宏**：像函数一样被调用，如 `style_helpers!(...)`。输入是调用时括号里的全部内容。

#### 4.1.2 核心流程

入口文件的模式是「薄入口 + 厚实现」（u1-l1 已建立此认知）：

```text
rustc 调用入口函数（#[proc_macro_*] 标注的那个）
        │
        ▼
一行转发：mod_name::fn_name(input)
        │
        ▼
实现模块（derive_action.rs / styles.rs / ...）做解析与生成
```

本讲只需看懂第一层——入口如何声明；实现层是后续几讲的主角。

#### 4.1.3 源码精读

先看文件开头的模块声明与导入：

[gpui_macros.rs:L1-L16](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L1-L16)

这里声明了 10 个实现模块，外加一个条件编译的反射模块（L12-13，仅在 `inspector` feature 或 `debug_assertions` 下编译），并导入编译器提供的 `TokenStream` 和 syn 的 `DeriveInput`、`Ident`。

**派生宏的声明**，以 Action 为例：

[gpui_macros.rs:L19-L22](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L19-L22)

```rust
#[proc_macro_derive(Action, attributes(action))]
pub fn derive_action(input: TokenStream) -> TokenStream {
    derive_action::derive_action(input)
}
```

要点：

- `#[proc_macro_derive(Action, ...)]` 的第一个参数是**用户侧宏名**，不必与函数名一致。
- 第二个参数 `attributes(action)` 声明「helper 属性」：允许用户在被派生类型的字段上写 `#[action(...)]`。没有这个声明，字段上的 `#[action]` 会直接被编译器拒绝。后续讲 Action 宏（u2-l1）时你会看到实现正是靠它定位字段的。
- 派生宏签名只有一个 `TokenStream` 参数，返回一个。

另外 4 个派生宏入口：`IntoElement`（L34-37）、`Render`（L39-43）、`AppContext`（带 `attributes(app)`，L58-61）、`VisualContext`（带 `attributes(window, app)`，L91-94）。

**函数式宏的声明**，以 `style_helpers` 为例：

[gpui_macros.rs:L97-L101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L97-L101)

```rust
#[proc_macro]
#[doc(hidden)]
pub fn style_helpers(input: TokenStream) -> TokenStream {
    styles::style_helpers(input)
}
```

签名与派生宏相同（一进一出），区别只在使用方式：`style_helpers!(...)` 是被「调用」的。`#[doc(hidden)]` 表示不出现在文档里——这类宏是给 gpui 内部用的。函数式宏共 **10 个**：`register_action`（L27-30）与 9 个样式宏（`style_helpers`、`visibility_style_methods`、`margin_style_methods` 等），其中 9 个样式入口全部转发到 `styles.rs`。

**属性宏的声明**，以 `test` 为例：

[gpui_macros.rs:L188-L191](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L188-L191)

```rust
#[proc_macro_attribute]
pub fn test(args: TokenStream, function: TokenStream) -> TokenStream {
    test::test(args, function)
}
```

注意**两个**输入参数：`args` 是 `#[gpui::test(seed = 10)]` 括号里的内容，`function` 是整个被标注的测试函数。返回值**替换**原函数——u4-l1 会讲到它如何把原函数改名为 `__` 前缀的内层函数再包一层新的 `#[test]` 函数。其余属性宏：`bench`（L203-206）、`property_test`（L276-279）、条件编译的 `derive_inspector_reflection`（L297-301）。

汇总成表：

| 类别 | 声明属性 | 函数签名 | 语义 | 本 crate 的入口 |
| --- | --- | --- | --- | --- |
| 派生宏 | `#[proc_macro_derive(Name, attributes(...))]` | 1 进 1 出 | 输出**追加**在原条目后 | Action、IntoElement、Render、AppContext、VisualContext（共 5） |
| 函数式宏 | `#[proc_macro]` | 1 进 1 出 | 按调用展开 | register_action + 9 个样式宏（共 10） |
| 属性宏 | `#[proc_macro_attribute]` | 2 进（args + item）1 出 | 输出**替换**原条目 | test、bench、property_test、derive_inspector_reflection（共 4） |

#### 4.1.4 代码实践

1. **实践目标**：不看资料，凭代码说出每个入口属于哪一类。
2. **操作步骤**：打开 `crates/gpui_macros/src/gpui_macros.rs`，从上到下扫描所有 `#[proc_macro_derive]` / `#[proc_macro]` / `#[proc_macro_attribute]` 标注，数出每类的数量。
3. **需要观察的现象**：每个入口函数体都只有一行转发；`attributes(...)` 只出现在派生宏上。
4. **预期结果**：5 个派生、10 个函数式、4 个属性宏，合计 19 个入口；与 u1-l1 建立的索引一致。

#### 4.1.5 小练习与答案

**练习 1**：`#[proc_macro_derive(AppContext, attributes(app))]` 中的 `attributes(app)` 起什么作用？去掉会怎样？

**答案**：它向编译器注册名为 `app` 的 helper 属性，允许用户在结构体字段上写 `#[app]`。去掉后，用户再写 `#[app] app: &mut App` 会被编译器以「无法识别的属性」拒绝——`derive_app_context` 也就找不到该字段并会输出 `compile_error!`（u3-l1 详述）。

**练习 2**：为什么属性宏 `test` 的函数有两个参数，而派生宏 `derive_render` 只有一个？

**答案**：属性宏必须同时拿到「括号里的参数」（seed、iterations 等）和「被标注的整个函数」，并且其输出会替换原条目；派生宏只接收被派生的类型定义本身，输出只是附加的 `impl` 块，原类型不动。

**练习 3**：`#[gpui::test(iterations = 5)]` 里的 `iterations = 5` 这串 token 从哪里进入宏？

**答案**：作为第一个参数 `args: TokenStream` 传入 `gpui_macros.rs:L189` 的 `test` 函数，再转发给 `test::test(args, function)`，由 `src/test.rs` 里的 `Args` 解析（u4-l1 详述）。

### 4.2 syn 解析与 quote 生成：以 derive_render 为例

#### 4.2.1 概念说明

`derive_render.rs` 只有 21 行，是全 crate 最小的实现文件，却完整展示了过程宏「三步走」套路：

1. **解析**（syn）：把 `proc_macro::TokenStream` 解析成 `DeriveInput` 语法树——里面有类型名 `ident`、泛型 `generics`、字段 `data` 等。
2. **提取**：从语法树里取出生成代码需要的片段（这里是类型名和泛型）。
3. **生成**（quote）：用 `quote! { ... }` 模板拼出新的 Rust 代码，再转换回 `proc_macro::TokenStream` 交还编译器。

`#[derive(Render)]` 的用途：为一个类型生成「渲染为空元素」的默认 `Render` 实现。gpui 内部一些不需要真正绘图的类型用它省去手写样板。

#### 4.2.2 核心流程

```text
proc_macro::TokenStream          ← 编译器递来的原始 token（struct _Element; 及其属性）
        │  parse_macro_input!(input as DeriveInput)
        ▼
syn::DeriveInput                 ← 语法树：ident = "_Element"，generics 为空，data = Struct
        │  &ast.ident            → type_name
        │  ast.generics.split_for_impl() → (impl_generics, type_generics, where_clause)
        ▼
quote! { impl #impl_generics gpui::Render for #type_name #type_generics #where_clause { ... } }
        │                       ← #var 是插值语法，把语法树片段嵌进模板
        ▼
proc_macro2::TokenStream         ← quote! 的返回类型
        │  .into()
        ▼
proc_macro::TokenStream          ← 交还编译器，追加到原条目之后
```

#### 4.2.3 源码精读

完整实现（全文仅 21 行）：

[derive_render.rs:L1-L21](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L1-L21)

逐段拆开看。

**导入与入口签名**：

[derive_render.rs:L1-L5](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L1-L5)

```rust
use proc_macro::TokenStream;
use quote::quote;
use syn::{DeriveInput, parse_macro_input};

pub fn derive_render(input: TokenStream) -> TokenStream {
```

注意实现函数的签名与入口函数完全一致——入口只是同名转发，所以这里再次接收 `proc_macro::TokenStream`。

**解析与提取**：

[derive_render.rs:L6-L8](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L6-L8)

```rust
let ast = parse_macro_input!(input as DeriveInput);
let type_name = &ast.ident;
let (impl_generics, type_generics, where_clause) = ast.generics.split_for_impl();
```

- `parse_macro_input!` 是 syn 提供的惯用宏：尝试把 input 解析为 `DeriveInput`；**失败时它会把 `syn::Error` 转成输出 `compile_error!` 的 TokenStream 并从当前函数提前 return**——这就是过程宏报告编译错误的标准方式之一（另一种在 `test.rs` 里见到，u4-l1 详述）。
- `split_for_impl()` 是写派生宏必背的惯用法：把泛型拆成三份，分别放在 `impl<...>` 尖括号处（`impl_generics`）、类型名后的 `<...>` 处（`type_generics`）和 `where` 子句处（`where_clause`）。对 `struct _Element;` 这种无泛型类型，三者都展开为空；但一旦用户给 `struct Foo<T: Clone>` 用这个派生，缺了它们生成的代码就无法编译。

**模板生成**：

[derive_render.rs:L10-L18](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L10-L18)

```rust
let r#gen = quote! {
    impl #impl_generics gpui::Render for #type_name #type_generics
    #where_clause
    {
        fn render(&mut self, _window: &mut gpui::Window, _cx: &mut gpui::Context<Self>) -> impl gpui::Element {
            gpui::Empty
        }
    }
};
```

四个关键点：

1. `quote!` 里的 `#var` 是插值语法（相当于字符串模板的 `${}`，但操作的是 token）。`##var`（重复插值）本文件没用到，`styles.rs` 里会大量出现（u3-l3）。
2. 模板里写的是**带路径的限定名** `gpui::Render`、`gpui::Window`、`gpui::Empty`，而不是裸的 `Render`——因为生成代码会被拼进**用户所在的 crate**，必须保证在那里的路径可解析。这要求使用 `#[derive(Render)]` 的 crate 依赖 gpui（事实上该宏 `#[doc(hidden)]`，本就是 gpui 内部使用的，见 [gpui_macros.rs:L39-L43](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L39-L43)）。
3. `r#gen` 是原始标识符（raw identifier）：zed 工作区使用 Rust 2024 edition（仓库根 `Cargo.toml` 中 `edition = "2024"`），而 `gen` 在 2024 edition 是保留关键字，必须加 `r#` 前缀才能当变量名。
4. 生成的 `render` 方法返回 `impl gpui::Element` 且函数体是 `gpui::Empty`——「渲染成空元素」的默认实现，参数加了 `_` 前缀表示未使用。

**类型转换收尾**：

[derive_render.rs:L20](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L20)

```rust
r#gen.into()
```

`quote!` 返回的是 `proc_macro2::TokenStream`（可以脱离编译器存在、便于测试的那套），而函数必须返回编译器的 `proc_macro::TokenStream`，`.into()` 完成这次跨越。这就是 2.4 节表格里「proc-macro2 是地基」的具体含义。

**测试侧**：

[render_test.rs:L1-L7](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/tests/render_test.rs#L1-L7)

```rust
#[test]
fn test_derive_render() {
    use gpui_macros::Render;

    #[derive(Render)]
    struct _Element;
}
```

这个测试只断言了一件事：**`#[derive(Render)]` 能通过编译**。派生宏的正确性常常就用「能否编译」来测试——生成的 `impl` 块如果格式非法，这里立刻报错。注意派生宏可以作用在函数体内定义的局部 struct 上，宏操作的是语法树，不关心作用域嵌套；`_Element` 的下划线前缀用于规避未使用告警。

#### 4.2.4 代码实践

1. **实践目标**：不运行任何东西，徒手预测 `#[derive(Render)] struct _Element;` 的展开结果。
2. **操作步骤**：
   - 读 [render_test.rs:L5-L6](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/tests/render_test.rs#L5-L6)，确认 `_Element` 无泛型、无 where 子句。
   - 对照 [derive_render.rs:L10-L18](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L10-L18) 的模板，把每个 `#xxx` 替换为「空」或 `_Element`。
3. **需要观察的现象**：三个泛型插值点全部消失，只留下具体类型名。
4. **预期结果**（示例代码，即 4.3 节 cargo expand 应看到的核心片段）：

   ```rust
   impl gpui::Render for _Element {
       fn render(&mut self, _window: &mut gpui::Window, _cx: &mut gpui::Context<Self>) -> impl gpui::Element {
           gpui::Empty
       }
   }
   ```

5. 下一节的 `cargo expand` 实践会验证这份手写预测。

#### 4.2.5 小练习与答案

**练习 1**：`split_for_impl()` 返回的三元组分别放到生成代码的什么位置？为什么必须拆开？

**答案**：`impl_generics` 放在 `impl` 后的尖括号（可含泛型参数及其约束），`type_generics` 放在类型名后（只放参数名），`where_clause` 放在 `{` 之前的 where 子句位置。Rust 要求这两处尖括号内容不同（impl 处可写约束、类型名处只写名字），所以不能整体复用 `ast.generics`。

**练习 2**：如果把模板里的 `gpui::Render` 改成 `Render`，会发生什么？

**答案**：生成代码被拼进用户 crate，`Render` 能否解析取决于用户作用域里是否恰好 `use` 了该 trait；没有导入就编译失败。写全路径 `gpui::Render` 是让宏「在任何依赖 gpui 的 crate 里都能用」的标准做法。

**练习 3**：`parse_macro_input!` 解析失败时函数会怎样结束？

**答案**：它展开为一段「解析成功则绑定变量、失败则直接 `return` 编译错误 TokenStream」的代码，因此失败时 `derive_render` 不会执行到 `quote!`，用户在编译输出里看到的是 syn 生成的 `compile_error!` 指出的语法错误位置。

### 4.3 cargo expand：亲眼看到宏展开的结果

#### 4.3.1 概念说明

过程宏是「编译期黑盒」，出了问题很难调试——你写的代码和编译器看到的代码之间隔着一层。`cargo-expand` 工具把编译器**展开所有宏之后**的源码打印出来，是阅读、调试过程宏的第一利器。对学习而言，它是验证「我在 `quote!` 模板里读到的东西就是编译器拿到的东西」这一信念的最直接手段。

#### 4.3.2 核心流程

```text
cargo expand -p gpui_macros --test render_test
        │
        ▼
对 render_test.rs 这个测试目标做完整编译前端展开
（展开 use、派生宏、macro_rules! 等）
        │
        ▼
终端打印展开后的全部源码
        │
        ▼
在其中搜索 "impl gpui::Render for _Element" 定位我们关心的片段
```

两个环境注意点：

- `cargo expand` 内部使用编译器的不稳定选项（`-Zunpretty=expanded`），因此通常需要 nightly 工具链；若默认工具链不是 nightly，用 `cargo +nightly expand ...` 调用（具体表现待本地验证）。
- 展开测试目标会连带构建其依赖；`gpui_macros` 的 dev-dependencies 包含 gpui（inspector feature），首次构建耗时较长。

#### 4.3.3 源码精读

本节没有新源码，观察对象仍是 [render_test.rs:L1-L7](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/tests/render_test.rs#L1-L7)。展开后你应当能在输出中同时看到：

- 原样的 `struct _Element;`——再次印证派生宏**不删除**原条目；
- 追加的 `impl gpui::Render for _Element { ... }` 块——即 [derive_render.rs:L10-L18](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L10-L18) 模板实例化的结果。

此外输出里还会有大量与本讲无关的内容（std prelude 导入、测试属性展开等），属正常现象，用搜索定位即可。

#### 4.3.4 代码实践

1. **实践目标**：得到 `#[derive(Render)]` 的真实展开结果，确认 4.2.4 的手写预测。
2. **操作步骤**：
   ```bash
   cargo install cargo-expand          # 若未安装
   cd <zed 仓库根目录>
   cargo expand -p gpui_macros --test render_test | less
   # 在 less 中输入 /impl gpui::Render 回车搜索
   ```
3. **需要观察的现象**：`struct _Element;` 与 `impl gpui::Render for _Element` 同时出现在输出中。
4. **预期结果**：`impl` 块的每一行都能在 `quote!` 模板里找到出处（见第 5 节综合实践的对照表）。若你的环境缺少 nightly 或构建失败，精确输出为「待本地验证」，可退回 4.2.4 的纯阅读实践。

#### 4.3.5 小练习与答案

**练习 1**：`cargo expand -p gpui_macros --test render_test` 与 `cargo expand -p gpui_macros --lib` 展开的分别是哪个目标？

**答案**：前者是 `tests/render_test.rs` 这个集成测试目标（链接 gpui 宏依赖后编译），后者是过程宏库本身——展开库目标意义不大，因为宏定义不会被「展开」，我们关心的是宏的使用处，所以这里选 `--test`。

**练习 2**：为什么输出里 `struct _Element;` 还在？

**答案**：派生宏的语义是「在原条目之后追加生成代码」，rustc 总是保留被派生的类型定义本身；对比属性宏（替换原条目），这是两类宏的关键差异。

**练习 3**：如果只想给自己新写的类型做展开实验，怎么做？

**答案**：在自己的分支/克隆里仿照 `render_test.rs` 新建一个集成测试文件，定义 `#[derive(Render)] struct MyThing;`，再执行 `cargo expand -p gpui_macros --test <文件名>`（本讲义不修改仓库文件，此操作由读者在自己的工作副本完成）。

## 5. 综合实践

**任务：把 `quote!` 模板与展开结果逐行对上。**

1. 在 zed 仓库根目录执行：

   ```bash
   cargo expand -p gpui_macros --test render_test
   ```

2. 从输出中摘抄出 `impl gpui::Render for _Element { ... }` 整块代码。

3. 打开 [derive_render.rs:L10-L18](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L10-L18)，完成下面这张对照表（「展开后」一列已按无泛型情况预先填好，请用实际输出核验）：

   | `quote!` 模板片段 | 展开后（无泛型的 `_Element`） | 说明 |
   | --- | --- | --- |
   | `impl #impl_generics gpui::Render for #type_name #type_generics` | `impl gpui::Render for _Element` | 两个泛型插值点为空，`#type_name` ← `ast.ident` |
   | `#where_clause` | （不产生任何内容） | 无 where 子句时插值为空 |
   | `fn render(&mut self, _window: &mut gpui::Window, _cx: &mut gpui::Context<Self>) -> impl gpui::Element` | 原样出现 | 模板中不含插值的字面 token 原样输出 |
   | `gpui::Empty` | `gpui::Empty` | 函数体表达式，原样输出 |
   | `r#gen.into()`（L20） | —— | 只是 proc_macro2 → proc_macro 的类型转换，不生成代码 |

4. 进阶一步（可选）：把 `struct _Element;` 换成 `struct _Pair<A, B> where A: Clone { _a: A, _b: B }`（在你自己的工作副本中改 `tests/render_test.rs`），重新展开，观察 `impl<A, B> gpui::Render for _Pair<A, B> where A: Clone,` 这一行如何由三个插值点拼出来——这正是 `split_for_impl()` 存在的意义。

5. 若环境无法运行 cargo expand：完成 4.2.4 的手写预测并通读 [derive_render.rs:L1-L21](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L1-L21)，标注「待本地验证」。

## 6. 本讲小结

- 过程宏分三类：派生宏（`#[proc_macro_derive]`，输出**追加**）、属性宏（`#[proc_macro_attribute]`，双参数，输出**替换**）、函数式宏（`#[proc_macro]`，按调用展开）；`gpui_macros` 的 19 个入口 = 5 派生 + 10 函数式 + 4 属性。
- 派生宏的 `attributes(...)` 参数注册 helper 属性（如 `#[action]`、`#[app]`、`#[window]`），是实现「在字段上打标记」机制的前提。
- 标准工作流四步：`parse_macro_input!` 解析为 `DeriveInput` → 提取 `ident` 与 `split_for_impl()` 三元组 → `quote!` 模板插值 → `.into()` 转回编译器 TokenStream；`derive_render.rs` 用 21 行完整示范了这条流水线。
- 生成代码必须写全路径（`gpui::Render`）以在用户 crate 中可解析；`r#gen` 的 `r#` 是因为 `gen` 在 2024 edition 是保留关键字。
- `cargo expand` 是验证宏行为的第一工具；派生宏不删除原条目，展开结果中原始 struct 与生成的 `impl` 块并存。
- 派生宏的「能否编译」本身就是有效测试——`render_test.rs` 只有三行主体，全篇唯一断言就是编译通过。

## 7. 下一步学习建议

- 下一讲（u1-l3「宏清单速览」）会把本讲建立的「三类入口」视角扩展到 gpui crate 侧的真实调用点（`Styled` trait、`actions!` 宏等），形成「宏 → 入口 → 实现 → 使用处」的完整索引。
- 之后进入单元二前，建议亲手用 syn/quote 写一个玩具派生宏（如为 struct 生成 `fn type_name() -> &'static str`），巩固本讲的流水线。
- 阅读预告：`derive_action.rs`（u2-l1/u2-l2）将展示带属性解析、多分支生成的「完整版」派生宏；`test.rs`（u4-l1）将展示属性宏如何改写并重新拼装整个函数。
