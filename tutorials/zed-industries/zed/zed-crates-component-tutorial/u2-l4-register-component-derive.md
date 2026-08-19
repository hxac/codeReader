# RegisterComponent 派生宏的展开结果

## 1. 本讲目标

学完本讲，你应该能够：

1. 不借助工具，在纸上写出 `#[derive(RegisterComponent)]` 作用在任意结构体上会生成哪三段代码。
2. 解释 `const _: () = { ... }` 这段「编译期 trait 断言」为什么能在编译期报错、且运行期零开销。
3. 说出注册函数名 `__component_registry_internal_register_*` 为什么必须带组件名前缀。
4. 讲清楚泛型组件 `ToggleButtonGroup<T, COLS, ROWS>` 为什么不能走派生宏，以及手动注册是如何做到与派生完全等价的。

本讲是第 2 单元「注册机制」的第四讲，聚焦宏的**展开结果**——上一讲（u2-l3）讲了 inventory 的登记/执行两阶段模型，本讲把「登记」这个动作拆开，看看它是被谁、以什么形状的代码生产出来的。

## 2. 前置知识

### 2.1 过程宏（proc-macro）是什么

Rust 的宏分两类：

- **声明宏**（`macro_rules!`）：按模式匹配做文本级替换，能力有限。
- **过程宏**（proc-macro）：以函数形式运行的编译器插件。输入是 `TokenStream`（一串 token），输出也是 `TokenStream`，编译器把输出当作真实代码继续编译。

过程宏又分三种，本讲涉及的是**派生宏**（derive macro）：写在 `#[derive(...)]` 里，挂在结构体或枚举上。它有一个关键特性：

> 派生宏**不能修改**被派生的类型本身，只能**追加**新的代码条目。编译器会把宏返回的 token 拼接在原条目之后。

### 2.2 syn 与 quote：拆 token、拼 token

过程宏几乎总是靠两个 crate 干活：

- `syn`：把输入的 token 流解析成语法树，例如 `DeriveInput`（一个结构体/枚举定义的完整描述：名字、泛型参数、字段……）。
- `quote`：提供 `quote! { ... }` 宏，把语法树片段拼回 token 流。`quote!` 里的 `#变量` 是插值语法，会把变量的内容嵌进去。

本讲的主角 `derive_register_component.rs` 总共只有 29 行，就是对这两步的组合：**用 syn 读出类型名，再用 quote 拼出三段代码**。

### 2.3 宏卫生（hygiene）：一个会影响理解展开结果的概念

`quote!` 生成的标识符默认使用 `Span::call_site()`，这种标识符是**非卫生的**——它可以和使用者代码、以及同一文件里其他宏展开生成的同名符号发生冲突。记住这一点，第 4.2 节解释「函数名为什么带前缀」时会用到。

### 2.4 承接前几讲的概念

- **u2-l1**：`Component` trait 的七个方法全是无 `self` 的关联函数，注册时被 `register_component` 逐个求值一次、抄进 `ComponentMetadata` 存入全局注册表。
- **u2-l3**：注册分两阶段——`inventory::submit!` 在**链接期**把 `ComponentFn`（包装 `fn()` 的 newtype）登记进分布式集合；`component::init()` 在**运行期**遍历集合并调用函数指针。`component::__private` 模块把 `inventory` 重导出，是为了解决宏展开的依赖可见性与版本同一性问题。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `crates/ui_macros/src/ui_macros.rs` | 过程宏 crate 的入口，声明 `#[proc_macro_derive(RegisterComponent)]` | 派生入口与文档示例 |
| `crates/ui_macros/src/derive_register_component.rs` | 派生宏的真正实现（29 行） | 本讲主角：三段生成物 |
| `crates/ui_macros/Cargo.toml` | 过程宏 crate 的依赖声明 | 证明宏 crate 不依赖 component |
| `crates/ui/src/components/button/toggle_button.rs` | 泛型组件 ToggleButtonGroup 及其手动注册 | 泛型绕开派生的案例 |
| `crates/ui/src/components/divider.rs` | 一个普通的派生注册组件 | 手工推演展开结果的素材 |
| `crates/component/src/component.rs` | `register_component`、`ComponentFn`、`init()` | 生成代码的调用目标 |

永久链接统一使用当前 HEAD `28c0f4aef8`。

## 4. 核心概念与源码讲解

### 4.1 RegisterComponent 派生入口

#### 4.1.1 概念说明

用户在结构体上写 `#[derive(RegisterComponent)]` 时，编译器会把**整个结构体条目的 token** 交给一个过程宏函数处理。本模块要回答三个问题：

1. 这个入口函数在哪里、长什么样？
2. 为什么宏 crate（ui_macros）自己不依赖 component crate？
3. 宏的文档注释里为什么能放一个可运行的完整示例？

#### 4.1.2 核心流程

```text
用户代码：#[derive(RegisterComponent)] struct Divider { ... }
   │
   │ 编译器把 struct Divider {...} 的 token 传给派生宏函数
   ▼
ui_macros::derive_register_component(input: TokenStream)
   │  委托给子模块 derive_register_component::derive_register_component
   ▼
生成的新 token 被拼接在原 struct 定义之后
   │  （本讲 4.2 的三段生成物）
   ▼
在使用者 crate（如 ui）里作为普通代码继续编译
```

#### 4.1.3 源码精读

派生宏的入口声明在 ui_macros.rs 的末尾：

[crates/ui_macros/src/ui_macros.rs:40-43](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/ui_macros.rs#L40-L43)

```rust
#[proc_macro_derive(RegisterComponent)]
pub fn derive_register_component(input: TokenStream) -> TokenStream {
    derive_register_component::derive_register_component(input)
}
```

这几行做了两件事：

- `#[proc_macro_derive(RegisterComponent)]` 属性决定了**用户可见的派生名**——用户写的是 `RegisterComponent`。注意：下面那个函数恰好也叫 `derive_register_component`、子模块的函数还与之同名，这只是命名约定，对外暴露的名字只由属性括号里的那个词决定。
- 入口函数只做一层转发，真正的展开逻辑在子模块里（也就是 4.2 节的主角）。

入口上方有一段长文档注释，里面是一个完整的 `MyComponent` 示例：

[crates/ui_macros/src/ui_macros.rs:12-39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/ui_macros.rs#L12-L39)

这段文档注释本身是一个 **doctest**（文档测试）：`rustdoc` 会把注释里的代码块当独立程序编译运行。它能在 ui_macros 这个「只做 token 处理」的 crate 里通过编译，靠的是 Cargo.toml 中的 dev-dependencies：

[crates/ui_macros/Cargo.toml:11-21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/Cargo.toml#L11-L21)

```toml
[lib]
path = "src/ui_macros.rs"
proc-macro = true

[dependencies]
quote.workspace = true
syn.workspace = true

[dev-dependencies]
component.workspace = true
ui.workspace = true
```

这张依赖表是理解整个机制的关键证据：

- `[dependencies]` 里**只有** `quote` 和 `syn`——过程宏在编译 ui_macros 自己时，完全不需要 component 的任何类型。
- `component` 和 `ui` 只出现在 `[dev-dependencies]`，仅供 doctest 使用。
- `proc-macro = true` 声明这是一个过程宏 crate，只能导出宏，不能导出普通函数或类型。

由此得出本模块最重要的结论：

> **过程宏 crate 对 component 一无所知。** 它只是拼接出以 `component::` 开头的路径**文本**，这些路径最终在使用派生宏的 crate（如 ui，它依赖 component）里解析。宏做的是 token 级操作，不做类型检查——这就是为什么一个不依赖 component 的 crate 能生成「调用 component」的代码。

#### 4.1.4 代码实践

**实践目标**：验证文档示例是可运行的 doctest，并确认宏 crate 的依赖构成。

**操作步骤**：

1. 在 Zed 仓库根目录执行：

   ```bash
   cargo test -p ui_macros --doc
   ```

2. 观察输出中与 `derive_register_component` 相关的 doctest 条目。
3. 打开 `crates/ui_macros/Cargo.toml`，对照上文的依赖表，确认 `[dependencies]` 中没有 `component`。

**需要观察的现象**：doctest 被编译并运行；输出的测试名通常形如 `src/ui_macros.rs - derive_register_component (line 21)`。

**预期结果**：该 doctest 通过，说明「派生 + 实现 Component 两个必须方法」这套最小接入确实能编译。具体测试名与数量以本机输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `component` 只出现在 ui_macros 的 `[dev-dependencies]`，而不能放进 `[dependencies]`？

**参考答案**：过程宏在编译 ui_macros 时只处理 token，不需要 component 的任何类型；把 component 加进正式依赖反而引入不必要的编译耦合。doctest 是独立的编译单元，运行在 ui_macros 的测试环境中，需要真实的 `component::Component`、`ui::div` 等类型，所以放进 dev-dependencies 即可。

**练习 2**：如果把属性写成 `#[proc_macro_derive(Widget)]`，用户代码要怎么写？入口函数名需要改吗？

**参考答案**：用户要写 `#[derive(Widget)]`；入口函数名不需要改——对外名字只由 `proc_macro_derive` 属性括号里的词决定，函数名只是 crate 内部的普通符号。

**练习 3**：derive 宏能否给被派生的 `Divider` 结构体**添加一个新字段**？

**参考答案**：不能。派生宏只能追加新条目，不能替换或修改原类型定义。所有能力都体现在「追加」上——本讲的三段生成物全部是新增条目。

### 4.2 derive_register_component 展开：三段生成物

#### 4.2.1 概念说明

这是本讲的核心模块。整个文件只有 29 行，但它精确地产出三段代码：

1. **编译期断言**：保证被派生的类型实现了 `Component`，否则在派生处报编译错误。
2. **注册函数**：一个把「注册这个组件」包装成 `fn()` 的薄函数。
3. **submit! 调用**：把注册函数作为 `ComponentFn` 值交给 inventory 登记（承接 u2-l3 的登记阶段）。

「心算展开」的能力来自记住这三段的形状和各自存在的理由。

#### 4.2.2 核心流程

```text
输入 TokenStream（struct Divider { ... }）
   │  syn 解析为 DeriveInput
   ▼
取 input.ident = "Divider"（注意：只取类型名，泛型参数被完全忽略）
   │  拼出注册函数名 __component_registry_internal_register_Divider
   ▼
quote! 生成三段：
   ① const _: () = { PhantomData 断言 }     ← 编译期检查 Divider: Component
   ② fn __component_registry_internal_register_Divider()
        { component::register_component::<Divider>(); }
   ③ component::__private::inventory::submit! {
        component::ComponentFn::new(②的函数名) }
   ▼
拼接在原 struct 之后，随使用者 crate 一起编译
   ▼
运行期：component::init() 遍历 inventory 集合，调用② → 执行
        register_component::<Divider>()，metadata 写入 COMPONENT_DATA
```

#### 4.2.3 源码精读

先看实现的前半段——解析输入、构造两个标识符：

[crates/ui_macros/src/derive_register_component.rs:5-12](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L5-L12)

```rust
pub fn derive_register_component(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as DeriveInput);

    let name = input.ident;
    let register_fn_name = syn::Ident::new(
        &format!("__component_registry_internal_register_{}", name),
        name.span(),
    );
```

- `input.ident` 只取出类型名标识符；`input.generics`（泛型参数）**完全没被读取**——这是 4.3 节泛型问题的根源，先埋下伏笔。
- `register_fn_name` 用 `format!` 拼出注册函数名，并通过 `syn::Ident::new(..., name.span())` 让新标识符**继承原类型名的 span**。span 是标识符在源码中的位置信息：继承它意味着 IDE 重命名 `Divider` 类型时，生成的函数名会联动更新；编译诊断也能指回类型名所在位置。

下面用 `Divider`（一个真实使用派生注册的组件）手工推演展开结果。以下为**示例代码（手工推演，非仓库文件原文）**，对照素材是 [crates/ui/src/components/divider.rs:36-43](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L36-L43) 上的 `#[derive(IntoElement, RegisterComponent)]`：

```rust
// ===== 派生宏为 Divider 生成的代码（手工推演）=====

// ① 编译期断言
const _: () = {
    struct AssertComponent<T: component::Component>(::std::marker::PhantomData<T>);
    let _ = AssertComponent::<Divider>(::std::marker::PhantomData);
};

// ② 注册函数
#[allow(non_snake_case)]
fn __component_registry_internal_register_Divider() {
    component::register_component::<Divider>();
}

// ③ inventory 登记
component::__private::inventory::submit! {
    component::ComponentFn::new(__component_registry_internal_register_Divider)
}
```

逐段对照 `quote!` 的原始模板：

[crates/ui_macros/src/derive_register_component.rs:13-27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L13-L27)

**第一段：编译期断言**（模板第 14-17 行）

```rust
const _: () = {
    struct AssertComponent<T: component::Component>(::std::marker::PhantomData<T>);
    let _ = AssertComponent::<#name>(::std::marker::PhantomData);
};
```

- `AssertComponent` 是一个零大小类型，唯一字段是 `PhantomData<T>`——它存在的意义只有一个：在类型参数上挂 `T: Component` 约束。
- `let _ = AssertComponent::<Divider>(PhantomData);` 在**值位置**构造这个类型，迫使编译器检查 `Divider: Component` 是否成立。不成立时在派生处直接报 E0277（trait bound 不满足），而不是等到运行期才发现组件没实现 trait。
- 为什么包在 `const _: () = { ... }` 里？两个原因：其一，`let` 语句不能出现在模块级，const 块提供了函数体式的上下文；其二，`const _` 是**匿名常量**，每次展开自成命名空间——同一个模块里多个组件各自派生，各自的 `AssertComponent` 互不冲突。整个断言在编译期类型检查阶段完成，不产生任何运行期代码。

**第二段：注册函数**（模板第 19-22 行）

```rust
#[allow(non_snake_case)]
fn #register_fn_name() {
    component::register_component::<#name>();
}
```

- 为什么要把一行调用包成函数？因为 inventory 收集的是**值**，而 `ComponentFn::new` 需要一个 `fn()` 函数指针（见 [crates/component/src/component.rs:31-37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L31-L37)）。「注册」这个动作必须先变成一个无参函数，它的地址才能作为静态值被登记。
- **函数名为什么带组件名前缀**？这是本讲的核心问题，答案分两层：
  1. `quote!` 生成的标识符是 `call_site` span（非卫生，见 2.3 节）。如果所有组件都生成同一个名字（比如就叫 `__register`），那么**同一个模块里只要有两个组件派生**，就会产生同名函数重定义的编译错误。把类型名嵌进函数名，天然保证同一模块内唯一。
  2. 双下划线开头的 `__component_registry_internal_` 前缀表明这是宏生成的内部符号，不是给人调用的 API；`#[allow(non_snake_case)]` 则是因为函数名里嵌入了 CamelCase 的类型名，会触发 snake_case 命名 lint。
- 函数体里的调用目标是注册的唯一入口：

  [crates/component/src/component.rs:47-61](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L47-L61)

  `register_component<T: Component>()` 逐项求值 `Component` 的关联函数、固化成 `ComponentMetadata`、以 `id` 为键写入 `COMPONENT_DATA`（细节见 u2-l2）。注意它的泛型约束 `<T: Component>`——手动注册路径靠这一约束获得与断言等价的编译期保证，4.3 节会用到。

**第三段：submit! 登记**（模板第 24-26 行）

```rust
component::__private::inventory::submit! {
    component::ComponentFn::new(#register_fn_name)
}
```

- `submit!` 在编译期展开为静态节点，程序启动、`main` 之前把 `ComponentFn` 值挂进由 [crates/component/src/component.rs:39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L39) 的 `inventory::collect!(ComponentFn)` 声明的分布式集合；随后 [crates/component/src/component.rs:25-29](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L29) 的 `init()` 运行期遍历集合并调用函数指针——这正是 u2-l3 建立的两阶段模型。
- 为什么写 `component::__private::inventory` 而不是直接 `inventory`？一句话回顾：使用者 crate（如 ui）**没有把 inventory 声明为直接依赖**（可查 `crates/ui/Cargo.toml`，其中只有 `component.workspace = true`、`ui_macros.workspace = true` 等，没有 inventory），宏展开若直接写 `inventory::submit!` 会因路径无法解析而编译失败；同时经由 component 重导出还能保证 `collect!` 与 `submit!` 用的是同一个 inventory 版本。完整论证见 u2-l3。

#### 4.2.4 代码实践

**实践目标**：亲眼看到三段生成物，验证手工推演与真实展开一致。

**操作步骤**：

1. 优先方案（cargo expand）：
   - 安装 cargo-expand 工具（部分环境需要 nightly 工具链，`rustup component add ...` 或参考其 README；若安装失败，转方案 2）。
   - 在 Zed 仓库根目录执行：

     ```bash
     cargo expand -p ui divider
     ```

   - 在输出中搜索 `__component_registry_internal_register_Divider`（输出较长时配合 `less` 或 `grep` 使用）。
2. 备用方案（手工推演，无需任何工具）：
   - 以 4.2.3 的「手工推演」代码块为底稿，对着 [crates/ui_macros/src/derive_register_component.rs:13-27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L13-L27) 的模板，把 `#name` 逐个替换成 `Divider`、把 `#register_fn_name` 替换成拼好的函数名。
3. 附加实验（编译期断言的威力）：
   - 在本地克隆里新建一个练习 crate，写一个只标 `#[derive(RegisterComponent)]` 但**不实现** `Component` 的结构体，执行 `cargo check`。

**需要观察的现象**：

- cargo expand 输出中，三段生成物紧跟在 `struct Divider` 定义之后（与 `#[derive(IntoElement)]` 的展开混排在一起，需要搜索定位）。
- 附加实验中，编译错误出现在派生处，错误类型为 trait bound 不满足（E0277），错误信息里包含 `AssertComponent` 相关字样。

**预期结果**：

- 展开代码与手工推演逐行对应（格式与宏展开的 span 信息可能略有差异，语义完全一致）。
- 附加实验产生编译错误，而非等到运行期才出问题。具体错误文案待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把断言从 `const _: () = { ... }` 里拿出来、直接放在模块级，会发生什么？

**参考答案**：无法编译。`let _ = AssertComponent::<Divider>(PhantomData);` 是语句，模块级只允许放条目（item），不允许放语句。此外，若把 `struct AssertComponent` 直接放模块级，同模块多次派生会重名。匿名 const 块同时解决了这两个问题：提供语句上下文 + 隔离命名空间。

**练习 2**：如果把生成函数名里的组件名后缀去掉（统一叫 `__component_registry_internal_register`），哪种场景会编译失败？

**参考答案**：同一个模块里有两个及以上组件都使用 `#[derive(RegisterComponent)]` 时。`quote!` 生成的标识符是 `call_site` span、非卫生，两个展开会生成同名函数，产生重定义错误。带类型名后缀后，同一模块内天然唯一。

**练习 3**：断言段和 `register_component<T: Component>()` 的泛型约束都在做「检查 T 实现 Component」，为什么两处都要有？

**参考答案**：断言让**派生宏的使用者**在派生处立刻看到清晰错误（指向写 `#[derive(...)]` 的那一行）；`register_component` 的约束是注册 API 自身的防线，覆盖包括手动注册在内的所有调用路径（4.3 节的手动注册正是靠它兜底）。一个服务于「使用派生宏的人」，一个服务于「注册入口的所有调用方」。

### 4.3 泛型组件手动注册：ToggleButtonGroup 案例

#### 4.3.1 概念说明

派生宏很好用，但 Zed 里有一个组件偏偏不能用——`ToggleButtonGroup`。它是一个**泛型组件**：类型参数 `T: ButtonBuilder` 加两个 const 泛型 `COLS`、`ROWS`。它绕开派生、手写注册代码，而这个「手写版本」恰好是理解派生展开的最佳教具：**手写的两段代码就是派生三段中②③的逐字复刻**。

泛型组件不能用派生的原因有三层，一层比一层本质：

1. **语法层**：派生只读 `input.ident`（4.2.3 已指出 `input.generics` 被完全忽略），展开结果会出现 `component::register_component::<ToggleButtonGroup>()`——在类型位置写泛型结构体而不给泛型实参，直接是编译错误（missing generics）。
2. **语义层**：即使宏被改成透传泛型参数，生成的模块级函数也无法引用只存在于类型定义作用域的类型参数；注册函数必须是非泛型的、指向唯一确定类型的函数。
3. **集合层**：`Component` 为**所有**满足 `T: ButtonBuilder` 的实例化都成立（每种 T × 每种 COLS × 每种 ROWS），这是一个无穷集合；而注册表以具体类型为单位存条目。必须由人挑一个「代表实例化」来注册——这是一个设计决策，宏无法替人做。

#### 4.3.2 核心流程

```text
泛型组件 ToggleButtonGroup<T, COLS, ROWS>
   │  不能 #[derive(RegisterComponent)]（三层原因）
   ▼
人肉挑一个代表实例化：ToggleButtonGroup<ToggleButtonSimple>
   │  （COLS/ROWS 用默认值 3/1）
   ▼
手写注册函数 register_toggle_button_group()
   │  函数体：component::register_component::<ToggleButtonGroup<ToggleButtonSimple>>();
   ▼
手写 submit! 登记一个 ComponentFn
   │  形状与派生第三段完全相同
   ▼
编译期保证来自 register_component<T: Component>() 的泛型约束
   （替代了派生第一段的 const 断言）
```

#### 4.3.3 源码精读

先看结构体定义，注意它 derive 了什么、没 derive 什么：

[crates/ui/src/components/button/toggle_button.rs:170-184](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L170-L184)

```rust
#[derive(IntoElement)]
pub struct ToggleButtonGroup<T, const COLS: usize = 3, const ROWS: usize = 1>
where
    T: ButtonBuilder,
{
    group_name: SharedString,
    rows: [[T; COLS]; ROWS],
    // ...其余字段
}
```

`#[derive(IntoElement)]` 在，`RegisterComponent` 不在——它走的是下面这段手动注册：

[crates/ui/src/components/button/toggle_button.rs:404-410](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L404-L410)

```rust
fn register_toggle_button_group() {
    component::register_component::<ToggleButtonGroup<ToggleButtonSimple>>();
}

component::__private::inventory::submit! {
    component::ComponentFn::new(register_toggle_button_group)
}
```

与 4.2.3 的派生展开逐段对照：

| 派生宏生成 | 手写版本 | 说明 |
| --- | --- | --- |
| ① `const _` 断言 | （无） | 编译期保证改由 `register_component<T: Component>()` 的泛型约束在调用点提供 |
| ② `__component_registry_internal_register_Divider` 函数 | `register_toggle_button_group` 函数 | 手写版可以用人类友好的 snake_case 名字，不需要 `#[allow(non_snake_case)]`，也不需要防碰撞前缀——它只有一个 |
| ③ `component::__private::inventory::submit!` | 完全相同的形状 | 同样经由 `component::__private`（ui 没有直接依赖 inventory，理由同 4.2.3） |

`ToggleButtonSimple` 定义在同一个文件（[toggle_button.rs:62](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L62) 起为结构体，第 95 行起实现 `ButtonBuilder`），它是最简单的 `ButtonBuilder` 实现，被选作「代表实例化」纯属就近取材。

再看 Component impl 的覆写，这里藏着两个与注册直接相关的决定：

[crates/ui/src/components/button/toggle_button.rs:412-431](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L412-L431)

```rust
impl<T: ButtonBuilder, const COLS: usize, const ROWS: usize> Component
    for ToggleButtonGroup<T, COLS, ROWS>
{
    fn name() -> &'static str {
        "ToggleButtonGroup"
    }

    fn scope() -> ComponentScope {
        ComponentScope::Input
    }

    fn sort_name() -> &'static str {
        "ButtonG"
    }
    // description、preview 略
}
```

- **为什么覆写 `name()`**：回忆 u2-l1——`name()` 的默认实现取 `std::any::type_name` 的完整类型路径。对泛型组件，那会得到一长串带 `ToggleButtonSimple` 的实例化路径，注册表键（`id()` 默认取自 `name()`）会随「挑了哪个代表实例化」而变。覆写成固定字符串 `"ToggleButtonGroup"` 后，**注册键与代表实例化解耦**——今天钉死 `ToggleButtonSimple`，明天换成别的 builder，注册表里的 id 不受影响。
- **为什么覆写 `sort_name()` 为 `"ButtonG"`**：让它在预览界面的分组内排序时进入 Button 家族（`sort_name` 管分组内排序，`name` 管显示与过滤，两者解耦，见 u2-l1）。

最后一个容易被误解的细节：注册时钉死了 `COLS = 3, ROWS = 1`，但 `preview()` 里展示的分组可以是任意列数——例如 [toggle_button.rs:440-447](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L440-L447) 处 `ToggleButtonGroup::single_row("single_row_test", [四个按钮])` 的数组长度会自行推断出新的 `COLS`。**注册的类型参数只决定「注册表里那条 metadata 由哪个具体类型求值」，不限制 preview 内部构造什么实例。** 因为 `Component` 的七个方法都是无 `self` 的关联函数，对整个泛型 impl 而言，无论从哪个实例化调用，`preview()` 执行的都是同一段函数体。

#### 4.3.4 代码实践

**实践目标**：亲手复现「泛型组件不能派生」的编译错误，并完成手写注册与派生展开的映射。

**操作步骤**：

1. 在自己的练习 crate（或本地仓库副本，**不要**改动上游源码）里写一个最小泛型组件：

   ```rust
   // 示例代码：用于复现编译错误
   #[derive(ui::RegisterComponent)]   // 路径按你的工程调整
   pub struct MyGroup<T: SomeTrait, const N: usize> {
       items: [T; N],
   }
   ```

2. 为它补全 `impl Component`（description、preview 两个必须方法），然后 `cargo check`。
3. 记录编译错误：预期第一段断言 `AssertComponent::<MyGroup>` 处就报「缺少泛型实参」类错误（E0107，具体文案待本地验证）。
4. 修复：去掉 derive，仿照 [toggle_button.rs:404-410](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L404-L410) 手写注册函数 + `component::__private::inventory::submit!`，钉死一个具体实例化（如 `MyGroup<SomeConcreteType, 2>`）。
5. 再次 `cargo check`，确认通过。

**需要观察的现象**：第 3 步的编译错误指向宏展开代码中的裸类型名 `MyGroup`；第 5 步通过编译，说明手动路径等价可行。

**预期结果**：泛型 + 派生 → 编译错误；手写注册 + 具体实例化 → 通过。若你的环境错误文案不同，记录实际输出即可。

#### 4.3.5 小练习与答案

**练习 1**：手动注册为什么偏偏选 `ToggleButtonSimple` 作为类型参数 `T`？换成别的 `ButtonBuilder` 实现会影响预览内容吗？

**参考答案**：`ToggleButtonSimple` 是同文件里最简单的 `ButtonBuilder` 实现，就近取材充当「代表实例化」。换掉它**不会**影响 name/description/preview 的值——`Component` impl 对所有实例化是同一份，关联函数执行的是同一段代码；唯一受影响的是未被覆写时的默认 `std::any::type_name`（但这里已覆写 `name()`，所以完全无感）。

**练习 2**：手动注册没有写 `const _` 断言，会不会因此失去「没实现 Component 就报错」的编译期保证？

**参考答案**：不会。`register_component<T: Component>()`（component.rs:47）的泛型约束在调用点强制检查 `ToggleButtonGroup<ToggleButtonSimple>: Component`，未实现同样在编译期报 E0277。断言段的额外价值只是把错误定位到「派生宏的使用处」，让写 `#[derive(...)]` 的人立刻看懂。

**练习 3**：想在预览里增加一个「4 列分组」的示例，需要再注册一个 `COLS = 4` 的实例化吗？

**参考答案**：不需要。`preview()` 内部调用 `ToggleButtonGroup::single_row(..., [四个元素])` 时，数组长度会推断出 `COLS = 4` 这个独立的具体类型；注册表条目只存一份 metadata（由代表实例化求值），preview 里可以自由构造任意实例化来展示。

## 5. 综合实践

综合实践 = 把本讲三块知识串成一份**带注释的展开笔记**（这也是本讲规格指定的实践任务）。任选一个真实的派生注册组件（如 `Divider`，或用 `grep -rn "derive(.*RegisterComponent" crates/` 再挑一个），完成以下四项产出：

1. **获取展开**：用 `cargo expand`（或按 4.2.3 的方法手工推演）得到该组件的派生展开代码。
2. **逐行对照表**：做一张三列表格——左列是 [crates/ui_macros/src/derive_register_component.rs:13-27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L13-L27) 模板的行号，中列是展开后的实际代码，右列写你的注释（这段在做什么、为什么需要）。
3. **回答关键问题**：用 3 到 5 句话解释生成的函数名 `__component_registry_internal_register_*` 为什么带组件名前缀（提示：`call_site` 非卫生标识符 + 同模块多组件碰撞 + span 继承）。
4. **泛型对照（选做）**：从第 1 步的对照表出发，再对照 [toggle_button.rs:404-410](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L404-L410) 的手写注册，写出「派生三段 ↔ 手写两段」的映射关系，并说明缺少的那段断言由什么补位。

**检查清单**：

- [ ] 能默写三段生成物的骨架（const 断言 / 注册函数 / submit!）。
- [ ] 对照表覆盖 `#name` 与 `#register_fn_name` 两个插值点的替换结果。
- [ ] 函数名前缀的解释提到了「非卫生标识符碰撞」。
- [ ] 泛型部分能说出「三层原因」中至少两层。

## 6. 本讲小结

- `#[derive(RegisterComponent)]` 生成且仅生成三段代码：`const _` 编译期断言、`__component_registry_internal_register_<类型名>` 注册函数、`component::__private::inventory::submit!` 登记调用。
- `const _: () = { struct AssertComponent<T: Component>(PhantomData<T>); let _ = ...; }` 是零运行期开销的编译期 trait 断言：匿名 const 提供语句上下文并隔离命名空间，PhantomData 结构在值位置被构造时触发约束检查。
- 注册函数名带组件名前缀，是因为 `quote!` 生成的 `call_site` 标识符非卫生——同一模块多次派生需要靠类型名后缀避免同名冲突；`name.span()` 的继承则让 IDE 重命名联动、错误定位准确。
- 过程宏 crate（ui_macros）不依赖 component，只生成 `component::` 开头的路径文本，由使用者 crate 解析——宏做 token 拼接，不做类型检查。
- 泛型组件绕开派生有三层原因（语法层丢泛型实参、语义层注册函数必须非泛型、集合层代表实例化需要人来挑）；`ToggleButtonGroup` 手写的「函数 + submit!」与派生②③段完全同构，缺少的断言由 `register_component<T: Component>()` 的泛型约束补位。
- 泛型组件覆写 `name()` 为固定字符串，使注册表键与「挑了哪个代表实例化」解耦。

## 7. 下一步学习建议

- **下一讲（u2-l5）**：`ComponentScope` 分类与 `ComponentStatus` 生命周期。本讲已多次见到 `scope()`（Divider 返回 `Layout`、ToggleButtonGroup 返回 `Input`），下一讲系统梳理 15 个作用域枚举与四种状态的设计系统治理语义，以及它们如何驱动 component_preview 的分组与徽章。
- **顺带阅读**：
  - `crates/component/src/component.rs` 第 25-61 行——把本讲的「生成代码调用目标」再通读一遍，巩固 u2-l2 的元数据固化流程。
  - inventory crate 的 README——理解 `collect!`/`submit!` 底层的构造器机制，加深 u2-l3 的登记阶段。
  - `crates/gpui_macros` 中的 `IntoElement` 派生宏——对比另一个为 UI 服务的派生宏在「解析 DeriveInput → quote! 拼接」上的同构性。
