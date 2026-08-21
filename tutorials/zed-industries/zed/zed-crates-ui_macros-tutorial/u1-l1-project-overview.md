# ui_macros 是什么：Zed 的 UI 过程宏工具箱

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `ui_macros` 这个 crate 在 Zed 仓库中的定位：它是一个 **过程宏（proc-macro）类型的库**，只对外暴露两个宏——函数式宏 `derive_dynamic_spacing!` 和派生宏 `RegisterComponent`。
- 说清楚两个宏各自服务谁：`derive_dynamic_spacing!` 为 `ui` crate 生成密度感知的间距枚举 `DynamicSpacing`；`RegisterComponent` 让实现了 `Component` trait 的组件自动登记进组件注册表。
- 读懂它的 `Cargo.toml`：理解 `[lib] path`、`proc-macro = true`、`[dependencies]`（syn、quote）与 `[dev-dependencies]`（component、ui）各自的含义。
- 成功构建这个 crate、查看它的依赖树，并用 `cargo doc` 找到两个宏的文档页面，理解「doc 注释就是文档」的生成机制。

本讲是整套手册的第一讲，不要求你已经读过任何 Zed 源码。

## 2. 前置知识

### 2.1 Rust 的宏：写「生成代码的代码」

如果你写过 `println!("hello")` 或 `vec![1, 2, 3]`，你已经在用宏了。宏是一种**在编译期生成代码**的手段，Rust 的宏分两大类：

| 类别 | 代表 | 拓展方式 | 适合场景 |
| --- | --- | --- | --- |
| 声明式宏 | `vec![]` | 按模式匹配替换 token | 简单重复代码 |
| 过程宏（procedural macro） | 本讲的两个宏 | 拿到 token 流，用 Rust 代码任意加工后输出新 token 流 | 复杂代码生成 |

过程宏又分三种形式，`ui_macros` 用到了其中两种：

1. **函数式过程宏**：形如 `my_macro!(...)`，用 `#[proc_macro]` 声明。本讲的 `derive_dynamic_spacing!` 属于这种。
2. **派生宏（derive macro）**：挂在 `#[derive(...)]` 里，用 `#[proc_macro_derive(...)]` 声明，为已有的类型自动「派生」出新代码。本讲的 `RegisterComponent` 属于这种。
3. **属性宏**：形如 `#[my_attribute]`，用 `#[proc_macro_attribute]` 声明。本 crate 没有使用。

### 2.2 TokenStream：过程宏的输入和输出

过程宏的输入输出都是 `TokenStream`（token 流）——可以把它理解为「编译器眼中的代码文本」：一串带语法类别的记号（标识符、字面量、标点……）。过程宏函数接收调用处的 token，返回生成代码的 token，编译器再把返回值当作正常代码继续编译。

### 2.3 Cargo workspace

Zed 是一个巨大的仓库，根目录的 `Cargo.toml` 定义了一个 **workspace**，把 `crates/` 下上百个 crate 统一管理：依赖版本集中声明、统一构建。`crates/ui_macros` 就是其中一个小 crate。

### 2.4 proc-macro crate 的特殊性

普通 Rust 库编译出来是运行时用的代码；而 `proc-macro = true` 的 crate 编译出来是**编译期运行的可执行逻辑**——它本身是个「编译器插件」。这带来一条重要推论，后面会反复出现：

> 过程宏 crate 的 `[dependencies]`（如 syn、quote）只在**写宏**时可用；宏**生成**的代码里引用的路径（如 `component::`、`::theme::`）必须由**调用宏的一方**提供。

## 3. 本讲源码地图

本讲涉及的核心文件都在 `crates/ui_macros/` 下，外加两个「消费者」文件用于理解它被谁使用：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/ui_macros/Cargo.toml` | crate 的构建配置 | `[lib] path`、`proc-macro = true`、依赖划分 |
| `crates/ui_macros/src/ui_macros.rs` | crate 入口，声明两个对外宏并转发到内部模块 | 两个宏的声明方式、doc 注释 |
| `crates/ui_macros/src/dynamic_spacing.rs` | `derive_dynamic_spacing!` 的实现 | 只看入口函数，细节留给单元二 |
| `crates/ui_macros/src/derive_register_component.rs` | `RegisterComponent` 的实现 | 只看入口函数，细节留给单元四 |
| `crates/ui/src/styles/spacing.rs` | `ui` crate 中调用 `derive_dynamic_spacing!` 的地方 | 宏的真实输入长什么样 |
| `crates/ui/src/components/tooltip.rs` | 使用 `#[derive(RegisterComponent)]` 的组件示例 | 派生宏的真实用法 |
| 根目录 `Cargo.toml` | workspace 级配置 | syn/quote 的版本集中声明 |

可以看到这个 crate 非常小：三个源码文件加一个 `Cargo.toml`，就构成了 Zed UI 体系里一个关键的「代码生成工具箱」。

## 4. 核心概念与源码讲解

### 4.1 ui_macros crate 定位：两个宏，两个使命

#### 4.1.1 概念说明

`ui_macros` 解决的问题是：Zed 的 UI 代码里存在**大量模式化、重复、且需要跟运行时配置联动**的代码，手写既容易错也难维护。这个 crate 把两类模式固化成宏：

1. **`derive_dynamic_spacing!`（函数式宏）**——Zed 允许用户在设置里选择 UI 密度（Compact / Default / Comfortable），每种间距在不同密度下应对应不同像素值。如果在每个组件里手写这套「三档切换 + 单位换算」，会重复几百遍。这个宏在编译期根据一份间距清单，一次性生成完整的 `DynamicSpacing` 枚举及其换算方法。
2. **`RegisterComponent`（派生宏）**——Zed 有一个组件预览体系（`component_preview`），要求每个 UI 组件把自己登记进全局注册表。在结构体上标注一行 `#[derive(RegisterComponent)]`，就能自动生成登记代码，并在忘记实现 `Component` trait 时直接产生编译错误。

一句话总结定位：**`ui_macros` 是「Zed UI 体系的编译期脚手架工厂」，产品只有两件——间距枚举与组件注册。**

#### 4.1.2 核心流程

整个过程宏的工作链路是：

```text
编译 ui crate 时
    │
    ├─ 遇到 spacing.rs 里的 derive_dynamic_spacing![ 24, 32, ... ]
    │       │
    │       ▼
    │  ui_macros（作为编译器插件被调用）
    │       │  syn  解析输入 token → 结构化数据
    │       │  quote 把数据拼成新代码 token
    │       ▼
    │  编译器把生成的 `enum DynamicSpacing {...}` 当作 spacing.rs 里的正常代码编译
    │
    └─ 遇到 tooltip.rs 里的 #[derive(RegisterComponent)]
            │
            ▼
       ui_macros 生成「编译期 trait 检查 + 注册函数」代码
            │
            ▼
       编译期：inventory 收集注册项；运行时：组件进入 ComponentRegistry
```

注意时序：**宏的执行发生在编译期**，`DynamicSpacing` 枚举在 `ui` crate 编译完成前就已经「真实存在」于源码中等价的位置了；而组件注册发生在**程序运行时**的初始化阶段（由宏生成的代码驱动）。

#### 4.1.3 源码精读

先看 crate 入口文件，它只有 43 行：

[ui_macros.rs:L1-L4](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L1-L4)

```rust
mod derive_register_component;
mod dynamic_spacing;

use proc_macro::TokenStream;
```

前两行声明两个**内部实现模块**——`ui_macros.rs` 只是「门面」，真正干活的代码在 `dynamic_spacing.rs` 和 `derive_register_component.rs` 里。`use proc_macro::TokenStream` 引入的 `proc_macro` 是 Rust 编译器内置的 crate，任何过程宏 crate 都能直接使用，无需在 `Cargo.toml` 里声明。

接着是第一个宏——函数式宏的声明：

[ui_macros.rs:L6-L10](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L6-L10)

```rust
/// Generates the DynamicSpacing enum used for density-aware spacing in the UI.
#[proc_macro]
pub fn derive_dynamic_spacing(input: TokenStream) -> TokenStream {
    dynamic_spacing::derive_spacing(input)
}
```

三个要点：

- `#[proc_macro]` 把这个函数标记为**函数式过程宏**，调用形式是 `derive_dynamic_spacing!(...)`；
- 它的签名固定为「吃进 `TokenStream`、吐出 `TokenStream`」；
- 函数体只有一行转发——解析与生成的重活全部在 `dynamic_spacing::derive_spacing` 里（入口见 [dynamic_spacing.rs:L49-L50](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L49-L50)，细节单元二精读）。

然后是第二个宏——派生宏的声明，先看它的文档注释（这段文档很重要，是本讲实践的观察对象之一）：

[ui_macros.rs:L12-L39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L12-L39)

```rust
/// Registers components that implement the `Component` trait.
///
/// This proc macro is used to automatically register structs that implement
/// the `Component` trait with the [`component::ComponentRegistry`].
///
/// If the component trait is not implemented, it will generate a compile-time error.
///
/// # Example
///
/// ```
/// use ui::{AnyElement, App, Component, div, IntoElement, Window};
/// use ui_macros::RegisterComponent;
///
/// #[derive(RegisterComponent)]
/// struct MyComponent;
///
/// impl Component for MyComponent {
///     fn description() -> &'static str {
///         "My component description"
///     }
///
///      fn preview(_window: &mut Window, cx: &mut App) -> AnyElement {
///         div().into_any_element()
///     }
/// }
/// ```
///
/// This example will add MyComponent to the ComponentRegistry.
```

这段注释里藏着一个 **doctest**：`# Example` 下面用 ```` ``` ```` 围起来的代码块会被 `cargo test` 当作测试编译运行——它演示了「派生 + 实现 `Component` trait」的完整用法，也正是 `Cargo.toml` 里 `[dev-dependencies]` 存在的原因（4.3 节展开）。

最后是派生宏本体：

[ui_macros.rs:L40-L43](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L40-L43)

```rust
#[proc_macro_derive(RegisterComponent)]
pub fn derive_register_component(input: TokenStream) -> TokenStream {
    derive_register_component::derive_register_component(input)
}
```

`#[proc_macro_derive(RegisterComponent)]` 声明这是一个名为 `RegisterComponent` 的派生宏，同样一行转发到 [derive_register_component.rs:L5-L6](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/derive_register_component.rs#L5-L6)。对比两个宏的声明属性：`#[proc_macro]` 对应 `xxx!(...)` 调用语法，`#[proc_macro_derive(Name)]` 对应 `#[derive(Name)]` 语法。

#### 4.1.4 代码实践：亲眼看到两个宏被调用的地方

1. **实践目标**：在真实代码里找到两个宏的调用点，建立「宏定义 ↔ 宏使用」的对应关系。
2. **操作步骤**：
   - 在 Zed 仓库根目录执行 `grep -rn "derive_dynamic_spacing!" crates/ --include="*.rs"`（或用编辑器全局搜索）。
   - 再执行 `grep -rn "derive(RegisterComponent)" crates/ --include="*.rs" | head -20`。
   - 打开搜到的 `crates/ui/src/styles/spacing.rs`，重点看第 29–44 行的宏调用；再打开 `crates/ui/src/components/tooltip.rs` 看第 8 行。
3. **需要观察的现象**：
   - `derive_dynamic_spacing!` 在整个仓库中**只有一个调用点**——它更像「一次性生成一整套类型」的批处理；
   - `#[derive(RegisterComponent)]` 有很多调用点，每个 UI 组件一份。
4. **预期结果**：`spacing.rs` 里能看到这样的调用（节选）：

   [spacing.rs:L29-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44)

   ```rust
   derive_dynamic_spacing![
       (0, 0, 0),
       (1, 1, 2),
       (1, 2, 4),
       // …中间省略…
       24,
       32,
       40,
       48
   ];
   ```

   这份清单就是宏的输入：括号三元组 `(1, 2, 4)` 表示「Compact 1px / Default 2px / Comfortable 4px」，单个数字 `24` 则由宏按公式 \( (n-4,\ n,\ n+4) \) 推导三档。而 `tooltip.rs` 里只需一行：

   [tooltip.rs:L4-L13](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tooltip.rs#L4-L13)

   ```rust
   use crate::prelude::*;

   #[derive(RegisterComponent)]
   pub struct Tooltip {
       title: Title,
       meta: Option<SharedString>,
   }
   ```

   `RegisterComponent` 是经由 `ui` crate 的 prelude 引入的，组件本身对注册过程「零感知」。
5. 本实践的搜索结果数量取决于仓库当前状态，属「待本地验证」项；但两个调用文件的位置是确定的。

#### 4.1.5 小练习与答案

**练习 1**：`derive_dynamic_spacing` 是函数式宏还是派生宏？从源码的哪一行可以判断？

答案：函数式宏。依据是 [ui_macros.rs:L7](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L7) 上的 `#[proc_macro]` 属性；若是派生宏则会写成 `#[proc_macro_derive(...)]`。

**练习 2**：`ui_macros.rs` 里两个 `pub fn` 的函数体都只有一行转发，为什么要把实现拆到另外两个文件？

答案：入口文件负责「声明宏 + 文档」，实现文件负责「解析 + 生成」。这样 `ui_macros.rs` 成为一张清晰的目录页：读者扫一眼就知道 crate 对外提供什么，再按需深入对应模块。这也是 Zed 编码规范「优先在既有文件实现，除非是新逻辑组件」的一个体现——解析逻辑确实是独立组件，值得独立成文件。

**练习 3**：如果删掉 `ui_macros.rs` 中 `RegisterComponent` 文档注释里的 ` ``` ` 代码块，会影响编译吗？

答案：不影响编译，但会带来两个变化——`cargo doc` 生成的文档页面失去示例；`cargo test -p ui_macros` 失去唯一的 doctest（该 crate 没有其他测试），自动验证能力随之消失。

### 4.2 Zed workspace 与 crates 组织：ui_macros 的位置

#### 4.2.1 概念说明

Zed 仓库根部的 `Cargo.toml` 用 `[workspace]` 管理 `crates/` 下的所有 crate：公共依赖的版本只在根部声明一次（`[workspace.dependencies]`），成员 crate 用 `xxx.workspace = true` 继承。`ui_macros` 在这张大图里是一个非常「底层」的小节点：全仓库只有 `ui` 一个 crate 直接依赖它，而 `ui` 是几乎所有 UI 组件的宿主，因此两个宏的影响面实际覆盖整个编辑器界面。

#### 4.2.2 核心流程

依赖与调用关系可以用这张图概括：

```text
workspace 根 Cargo.toml（集中声明 syn/quote 版本）
        │  workspace = true 继承
        ▼
┌─────────────┐  生成代码引用 ::theme:: / component::     ┌───────────┐
│ ui_macros   │ ──────────────────────────────────────▶  │ ui crate  │
│ (proc-macro)│      （要求调用方自己提供这些依赖）         │ 间距/组件  │
└─────────────┘                                         └─────┬─────┘
      ▲  唯一消费者：ui/Cargo.toml 中的 ui_macros.workspace = true
      │                                                    │ 依赖
      │                                                    ▼
      │                                          component / theme / gpui …
┌──────────────┐   dev-dependencies（仅供 doctest 使用）
│ component +  │ ◀────────────────────────────────── ui_macros
│ ui           │
└──────────────┘
```

#### 4.2.3 源码精读

先看「谁依赖 ui_macros」——全仓库只有一处：

[ui/Cargo.toml:L31](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/Cargo.toml#L31)

```toml
ui_macros.workspace = true
```

再看 `ui` crate 为什么能「喂饱」宏生成的代码。生成的间距代码引用了 `::theme::theme_settings` 和 `::theme::UiDensity`，生成的注册代码引用了 `component::Component`——这些路径必须在宏展开处可解析：

[ui/Cargo.toml:L17](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/Cargo.toml#L17)

```toml
component.workspace = true
```

[ui/Cargo.toml:L30](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/Cargo.toml#L30)

```toml
theme.workspace = true
```

也就是说：`ui_macros` 自己的 `[dependencies]` 里**没有** theme 和 component，它只是「代笔」，写出的代码里点名要这两个路径；真正提供它们的是调用方 `ui`。`spacing.rs` 里的直接引用也印证了这一点：

[spacing.rs:L1-L3](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L1-L3)

```rust
use gpui::{App, Pixels, Rems, px, rems};
use theme::UiDensity;
use ui_macros::derive_dynamic_spacing;
```

最后看 workspace 根部对 syn/quote 的集中管理：

[根 Cargo.toml:L756](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/Cargo.toml#L756)

```toml
quote = "1.0.9"
```

[根 Cargo.toml:L814](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/Cargo.toml#L814)

```toml
syn = { version = "2.0.101", features = ["full", "extra-traits", "visit-mut"] }
```

还有一个有意思的细节——workspace 甚至为这两个「编译期依赖」单独开了编译优化：

[根 Cargo.toml:L1008-L1009](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/Cargo.toml#L1008-L1009)

```toml
quote = { opt-level = 3 }
syn = { opt-level = 3 }
```

因为过程宏在**每次编译时都要运行**，把它跑快一点能直接改善全仓库的构建速度。

#### 4.2.4 代码实践：验证依赖关系

1. **实践目标**：亲手验证「只有 ui 依赖 ui_macros」这个论断。
2. **操作步骤**：在仓库根目录运行 `grep -rln "^ui_macros" crates/*/Cargo.toml`；再运行 `cargo tree -p ui_macros --invert --depth 1`（`--invert` 反向查询谁依赖它）。
3. **需要观察的现象**：grep 应只命中 `crates/ui/Cargo.toml`；`cargo tree --invert` 会显示 `ui` 出现在依赖链上。
4. **预期结果**：与 4.2.3 的分析一致。`cargo tree --invert` 的完整输出依赖本地workspace 状态，属「待本地验证」。
5. 完成后记录到你的笔记里，后续讲义（u1-l3）会把这张关系图扩展到 theme 与 component。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ui_macros` 的 `Cargo.toml` 里不直接依赖 `theme`，生成代码却能用 `::theme::`？

答案：过程宏生成的代码是在**调用方**（`ui` crate）里编译的，路径解析发生在调用方的依赖环境中。`ui` 依赖了 `theme`，所以生成的 `::theme::...` 能解析。这也意味着：任何想调用这两个宏的 crate，都必须自己具备相应依赖。

**练习 2**：workspace 根部把 `syn` 的版本声明为 `2.0.101` 并开启多个 feature，这对 `ui_macros` 意味着什么？

答案：`ui_macros` 用 `syn.workspace = true` 继承同一份配置，无需自己写版本号。全仓库所有过程宏 crate 共用同一个 syn 版本与 feature 集，避免版本碎片化和重复编译。

**练习 3**：如果 Zed 新增一个 `crates/rich_ui` 也想用 `derive_dynamic_spacing!`，它需要做哪些准备？

答案：至少三件事——在 `Cargo.toml` 加 `ui_macros.workspace = true`；加 `theme.workspace = true`（生成代码引用了 `::theme::`）；生成的枚举还用到 `App`、`Rems`、`Pixels`、`rems`、`px` 等 gpui 类型且期望它们在调用处可直接使用（`ui` 里是 `use gpui::...` 显式导入），因此还需依赖 gpui 并导入这些名字。这也解释了为什么目前只有 `ui` 一个消费者。

### 4.3 Cargo.toml 构建配置：proc-macro = true 的含义

#### 4.3.1 概念说明

这个 crate 的 `Cargo.toml` 一共只有 21 行，但每一节都有讲究：

- **`[lib] path = "src/ui_macros.rs"`**：指定库入口文件。Cargo 默认找 `src/lib.rs`，而 Zed 的编码规范要求「用描述性命名代替默认的 `lib.rs`」，所以这里显式指向与 crate 同名的 `ui_macros.rs`。
- **`proc-macro = true`**：声明这是一个过程宏 crate。它改变 Cargo 的产物类型——编译出的不是普通 `rlib`，而是供编译器加载的宏实现；同时**禁止** crate 里出现普通公开项（函数、结构体都不能直接 `pub use` 给别人），只能导出宏。
- **`[dependencies]`：`syn` + `quote`**——过程宏开发的两大标准库。syn 负责「token → 结构化语法树」的解析，quote 负责「数据 → token」的生成。注意它们只在**宏内部**可用（见 2.4 节的推论）。
- **`[dev-dependencies]`：`component` + `ui`**——只在测试/示例场景可用。它们正是为 `ui_macros.rs` 里那个 doctest 服务的：doctest 写了 `use ui::{AnyElement, App, Component, ...}`，没有这两个依赖，doctest 连编译都过不了。这形成了一个精巧的循环：`ui` 依赖 `ui_macros`（正常依赖），`ui_macros` 又在 dev 依赖里借 `ui` 来测试自己——Cargo 允许这种「开发期反向依赖」，因为它不进入正式构建图。

#### 4.3.2 核心流程

从 `cargo build` 的视角看这个 crate 的编译时序：

```text
cargo build -p ui_macros
    │
    ├─ 1. 编译 syn、quote（普通库，供宏实现使用）
    │
    ├─ 2. 编译 ui_macros 本体 → 产出「可被编译器加载的宏插件」
    │
    └─ 完成。（此时还没有任何宏被真正执行）

之后 cargo build -p ui
    │
    ├─ 3. 加载 ui_macros 插件
    │
    ├─ 4. 展开 spacing.rs 的 derive_dynamic_spacing! → 得到 DynamicSpacing 枚举源码
    ├─ 5. 展开 tooltip.rs 的 #[derive(RegisterComponent)] → 得到注册代码
    │
    └─ 6. 把展开结果与普通代码一起编译
```

理解这个时序就能明白：改 `ui_macros` 的源码后，所有用到它的 crate 都要重新编译——这也是 Zed 把 syn/quote 设为 `opt-level = 3` 的原因之一。

#### 4.3.3 源码精读

完整看一遍这份 `Cargo.toml`：

[Cargo.toml:L1-L6](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L1-L6)

```toml
[package]
name = "ui_macros"
version = "0.1.0"
edition.workspace = true
publish.workspace = true
license = "GPL-3.0-or-later"
```

包名、版本之外，`edition` / `publish` 都继承 workspace 统一配置。

[Cargo.toml:L8-L13](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L8-L13)

```toml
[lints]
workspace = true

[lib]
path = "src/ui_macros.rs"
proc-macro = true
```

`[lints] workspace = true` 让全仓库统一的 clippy 规则也约束这个小编件；`[lib]` 两行则是本节的主角：入口文件名 + 过程宏开关。

[Cargo.toml:L15-L21](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/Cargo.toml#L15-L21)

```toml
[dependencies]
quote.workspace = true
syn.workspace = true

[dev-dependencies]
component.workspace = true
ui.workspace = true
```

注意这里的对称美：**正式依赖只有「写宏的工具」（syn/quote），测试依赖是「用宏的成品」（component/ui）**。前者是因，后者是果——若未来 doctest 删除，`component` 这条 dev 依赖也可以移除。

#### 4.3.4 代码实践：构建、依赖树与文档（本讲主实践）

1. **实践目标**：亲手构建 `ui_macros`，摸清它的依赖，并通过 `cargo doc` 理解「doc 注释如何变成文档」。

2. **操作步骤**（在 Zed 仓库根目录执行）：

   ```bash
   # 第一步：单独构建这个 crate
   cargo build -p ui_macros

   # 第二步：查看直接依赖
   cargo tree -p ui_macros --depth 1

   # 第三步：生成它自己的文档（不构建依赖的文档，加快速度）
   cargo doc -p ui_macros --no-deps --open
   ```

3. **需要观察的现象**：

   - 第一步应顺利通过——这个 crate 只有两个小源文件，构建很快；
   - 第二步的输出中，`ui_macros` 节点下应能看到 `quote` 与 `syn` 两个直接依赖（proc-macro crate 还会隐式关联编译器内置的 `proc-macro`，具体显示项「待本地验证」）；
   - 第三步打开的文档页面里应有 **两个宏** 的条目：
     - `macro derive_dynamic_spacing`——文档只有一句话，即 [ui_macros.rs:L6](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L6) 那行 `/// Generates the DynamicSpacing enum...`；
     - `derive macro RegisterComponent`——文档包含完整说明加一个可展开的 `# Example` 代码块，内容正是 [ui_macros.rs:L12-L39](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs#L12-L39) 中的 doctest。

4. **预期结果**：你会直观看到「rustdoc 只是忠实地渲染 doc 注释」——两个宏的文档详略差异，完全由源码里 `///` 注释的长度决定，没有任何额外配置。这也提示我们：**写过程宏时，doc 注释就是使用手册**。

5. **延伸（可选）**：把 `cargo doc` 输出目录（`target/doc/ui_macros/`）里的 HTML 用浏览器打开，对比源码注释与渲染结果的逐段对应关系。

6. 说明：以上命令的精确输出与本地工具链版本有关，若与预期不符，请以实际输出为准（「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `[lib]` 里的 `proc-macro = true` 删掉，会发生什么？

答案：crate 会变成普通库，但 `use proc_macro::TokenStream` 与 `#[proc_macro]` 等将无法使用（`proc_macro` crate 只对过程宏 crate 可见），编译直接报错。反之，保留 `proc-macro = true` 时，你不能在这个 crate 里导出普通的公共函数——过程宏 crate 只能导出宏。

**练习 2**：`[dev-dependencies]` 里的 `ui` 会不会让 `ui_macros` 产生循环依赖，导致构建失败？

答案：不会。Cargo 对 dev-dependencies 的处理是：它们只在编译测试、示例、doctest 时引入，不参与「被别人正常依赖」时的构建图。`ui → ui_macros` 是正式依赖边，`ui_macros → ui`（dev）是开发期边，二者不构成正式循环。

**练习 3**：为什么 `cargo doc -p ui_macros --no-deps` 要加 `--no-deps`？

答案：不加的话 rustdoc 会连带为所有依赖（syn、quote，以及它们的一串传递依赖）生成文档，耗时且占空间。加 `--no-deps` 只生成本 crate 文档——我们只关心两个宏的说明页，正合适。

## 5. 综合实践

**任务：给 `ui_macros` 建立一份「crate 身份档案」。**

把本讲三个模块的观察合成一张卡片，包含以下字段（全部基于你本地跑出的真实结果，而非本讲义的文字）：

1. **基本信息**：crate 名、入口文件路径（依据 `[lib] path`）、crate 类型（proc-macro）。
2. **对外产品**：两个宏的名字、类型（函数式/派生）、各自一行职责描述（抄自 `cargo doc` 渲染出的文档第一句）。
3. **依赖档案**：`cargo tree -p ui_macros --depth 1` 的输出；标注哪些是「写宏用的」、哪些（dev）是「测宏用的」。
4. **客户档案**：用 grep / `cargo tree --invert` 找出的消费者清单，以及消费者为满足宏生成代码而必备的依赖（theme、component）。
5. **一句话定位**：用你自己的话总结这个 crate 为什么存在（提示：如果不存在它，`spacing.rs` 和每个组件要多写什么）。

完成后，把这份档案保存在你的学习笔记里——下一讲（u1-l2）我们会动手写一个最小的过程宏 crate，那将用到本档案里的所有概念。

## 6. 本讲小结

- `ui_macros` 是 Zed workspace 中一个极小的 **proc-macro crate**：三个源码文件，对外只提供 `derive_dynamic_spacing!`（函数式宏，`#[proc_macro]` 声明）和 `RegisterComponent`（派生宏，`#[proc_macro_derive]` 声明）两个宏。
- 前者为 `ui` crate 的 `spacing.rs` 编译期生成密度感知的 `DynamicSpacing` 枚举；后者让 UI 组件一行 `#[derive(RegisterComponent)]` 即可自动注册，并在漏实现 `Component` trait 时直接编译报错。
- crate 入口 `ui_macros.rs` 只做「声明 + 文档 + 转发」，实现在 `dynamic_spacing.rs` 与 `derive_register_component.rs` 两个模块中。
- `Cargo.toml` 的关键配置：`[lib] path` 指定描述性入口名；`proc-macro = true` 决定产物是编译器插件；正式依赖只有 syn/quote（写宏的工具），`ui`/`component` 只是服务 doctest 的 dev 依赖。
- 宏生成的代码引用 `::theme::`、`component::` 等路径，由**调用方** `ui` crate 的依赖负责解析——这是理解过程宏依赖关系的关键。
- rustdoc 文档完全来自源码 `///` 注释；`RegisterComponent` 的 doctest 同时是本 crate 唯一的自动化测试。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：我们将拆解过程宏 crate 的骨架——`TokenStream` 如何进出、函数式宏与派生宏在声明与调用上的差异，并动手创建一个最小的 proc-macro crate。建议先复习本讲 2.1–2.4 节的概念。
- **再下一讲（u1-l3）**：全景梳理两个宏与 `ui`、`component`、`theme` 等 crate 的关系，届时可以拿本讲「综合实践」产出的身份档案对照。
- **源码预读**（可选）：通读 [ui_macros.rs](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/ui_macros.rs) 全文（仅 43 行），并浏览 [spacing.rs:L5-L28](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L5-L28) 的注释——它是 `derive_dynamic_spacing!` 输入格式最好的官方说明。
