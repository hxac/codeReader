# 宏清单速览：宏与它们在 gpui 中的用武之地

## 1. 本讲目标

前两讲我们已经知道 gpui_macros 是一个什么样的 crate、过程宏的三种入口形式长什么样。本讲要完成一张「全景地图」：

1. 列出 gpui_macros 导出的**全部 19 个宏**，并按派生宏 / 函数式宏 / 属性宏分类。
2. 为每个宏建立一条四段式索引：**宏名 → 入口函数（gpui_macros.rs 中的行号）→ 实现模块 → gpui 侧真实使用处**。
3. 掌握用 grep（ripgrep）检索宏用法的方法，今后遇到任何宏都能自己定位它的定义与消费方。

学完本讲，你在阅读 Zed 业务代码看到 `#[derive(Action)]`、`.mt_2()`、`#[gpui::test]` 时，应该能立刻说出它们各自来自哪个宏、在哪个文件生成、又被 gpui 的哪段代码消费。

## 2. 前置知识

本讲不需要写过过程宏，但需要理解三个前置概念（u1-l2 已详细讲过，这里只做一句话回顾）：

- **派生宏（derive macro）**：写在 `#[derive(...)]` 里，给一个类型追加 trait 实现，原类型保留。入口标注 `#[proc_macro_derive(Name, attributes(helper))]`。
- **函数式宏（function-like macro）**：形如 `foo!(...)`，接收 token 流并展开成新代码。入口标注 `#[proc_macro]`。
- **属性宏（attribute macro）**：形如 `#[foo(args)]`，接收「参数 + 被标注的条目」两段 token 流，输出替换原条目。入口标注 `#[proc_macro_attribute]`。

另外两个本讲会反复用到的概念：

- **再导出（re-export）**：gpui 用 `pub use gpui_macros::{...}` 把宏重新挂到自己的命名空间下，这样业务代码只需依赖 `gpui` 一个 crate，写 `#[derive(IntoElement)]`（即 `gpui::IntoElement`）即可，不必直接依赖 `gpui_macros`。
- **声明宏调用过程宏**：`macro_rules!` 声明宏展开出的代码里可以包含 `#[derive(...)]`。gpui 的 `actions!` 就是典型——它展开出一批带 `#[derive(gpui::Action)]` 的单元结构体。这是「低层过程宏 + 高层语法糖」的两层架构。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_macros/src/gpui_macros.rs` | 全部 19 个宏的入口声明，薄入口转发到各实现模块 |
| `crates/gpui/src/action.rs` | `Action` trait 定义、`actions!` 声明宏、action 注册表；`Action` 派生宏的再导出与消费方 |
| `crates/gpui/src/styled.rs` | `Styled` trait；8 个样式宏 + `style_helpers!` 的调用点，`derive_inspector_reflection` 的唯一 gpui 侧使用点 |
| `crates/gpui/src/gpui.rs` | gpui 库根：批量再导出 8 个宏，`bench_group!`/`bench_main!` 配套声明宏 |
| `crates/gpui/src/inspector.rs` | `inspector_reflection` 模块：`derive_inspector_reflection` 生成代码所依赖的运行时类型 `FunctionReflection` |
| `crates/gpui/tests/action_macros.rs` | 集成测试：同时演示 `actions!`、`#[derive(Action)]`、`register_action!` 三层用法 |
| `crates/gpui_macros/tests/derive_context.rs`、`render_test.rs` | 本 crate 自己的测试：`#[derive(AppContext, VisualContext)]`、`#[derive(Render)]` 的最小用法 |

## 4. 核心概念与源码讲解

### 4.1 宏清单与分类：19 个入口的完整盘点

#### 4.1.1 概念说明

gpui_macros.rs 是「薄入口 + 厚实现」结构（u1-l1 已建立这个认知）：文件本身几乎不含逻辑，只负责用 `#[proc_macro_derive]` / `#[proc_macro]` / `#[proc_macro_attribute]` 三种标注把 19 个公开名字注册给 rustc，然后一对一转发给 11 个实现模块。

盘点宏清单时，除了名字，还要关注三件事：

1. **helper 属性**：派生宏声明的 `attributes(...)` 决定用户能在字段上写什么属性（如 `#[action(...)]`、`#[app]`、`#[window]`）。
2. **`#[doc(hidden)]` 与条件编译**：有些宏是内部实现细节，不希望被直接使用。
3. **模块级 cfg**：`derive_inspector_reflection` 只在 `inspector` feature 或 debug 构建下存在，release 构建里根本没有这个宏。

#### 4.1.2 核心流程

按类别清点 19 个宏的推导过程：

```text
gpui_macros.rs 入口声明
├── #[proc_macro_derive] 派生宏 × 5
│   Action / IntoElement / Render / AppContext / VisualContext
├── #[proc_macro] 函数式宏 × 10
│   register_action
│   style_helpers（doc hidden）
│   8 个 *_style_methods（visibility/margin/padding/position/overflow/cursor/border/box_shadow）
└── #[proc_macro_attribute] 属性宏 × 4
    test / bench / property_test / derive_inspector_reflection（cfg 门控）

5 + 10 + 4 = 19
```

其中 8 个样式宏共享同一个实现文件 `styles.rs`（各自有独立入口函数），`derive_inspector_reflection` 有独立的 cfg 门控模块，因此实现模块数 = 11 而不是 19。

#### 4.1.3 源码精读

先看五个派生宏的入口声明。[src/gpui_macros.rs:L18-L22](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L18-L22) 声明 `Action` 派生宏，注意 `attributes(action)` 注册了 `#[action(...)]` 这个 helper 属性——没有它，用户在结构体上写 `#[action(namespace = editor)]` 会直接编译报错：

```rust
#[proc_macro_derive(Action, attributes(action))]
pub fn derive_action(input: TokenStream) -> TokenStream {
    derive_action::derive_action(input)
}
```

[src/gpui_macros.rs:L32-L43](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L32-L43) 声明了两个最小的派生宏：`IntoElement`（为 `RenderOnce` 类型生成元素包装）和 `Render`（`#[doc(hidden)]`，生成空渲染）。`Render` 上多出的 `#[doc(hidden)]` 说明它是内部宏，不鼓励直接使用：

```rust
#[proc_macro_derive(IntoElement)]
pub fn derive_into_element(input: TokenStream) -> TokenStream { ... }

#[proc_macro_derive(Render)]
#[doc(hidden)]
pub fn derive_render(input: TokenStream) -> TokenStream { ... }
```

[src/gpui_macros.rs:L45-L61](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L45-L61) 与 [src/gpui_macros.rs:L91-L94](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L91-L94) 是两个上下文派生宏。注意 helper 属性的差异：`AppContext` 只要 `attributes(app)`，`VisualContext` 要 `attributes(window, app)` 两个——这正对应「VisualContext = App 能力 + Window 能力」的叠加关系：

```rust
#[proc_macro_derive(AppContext, attributes(app))]
pub fn derive_app_context(input: TokenStream) -> TokenStream { ... }

#[proc_macro_derive(VisualContext, attributes(window, app))]
pub fn derive_visual_context(input: TokenStream) -> TokenStream { ... }
```

十个函数式宏中，`register_action` 是低层逃生通道（[src/gpui_macros.rs:L24-L30](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L24-L30)），供手动实现 `Action` trait 的类型完成启动前注册；其余九个全部是样式宏。[src/gpui_macros.rs:L96-L149](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L96-L149) 连续声明了它们，每个入口两三行，全部转发到 `styles.rs` 中同名（或对应）的函数：

```rust
/// Used by GPUI to generate the style helpers.
#[proc_macro]
#[doc(hidden)]
pub fn style_helpers(input: TokenStream) -> TokenStream {
    styles::style_helpers(input)
}

/// Generates methods for margin styles.
#[proc_macro]
pub fn margin_style_methods(input: TokenStream) -> TokenStream {
    styles::margin_style_methods(input)
}
// ……visibility/padding/position/overflow/cursor/border/box_shadow 同构，共 8 个
```

对应的实现入口分散在 `styles.rs` 中（行号可在 [src/styles.rs:L40-L384](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/styles.rs#L40-L384) 之间找到）：`style_helpers`（L40）、`visibility_style_methods`（L50）、`margin_style_methods`（L72）、`padding_style_methods`（L86）、`position_style_methods`（L100）、`overflow_style_methods`（L129）、`cursor_style_methods`（L159）、`border_style_methods`（L335）、`box_shadow_style_methods`（L384）。注意 `border_style_methods` 与 `cursor_style_methods` 之间相隔约 170 行——因为每个入口函数内部都有一张自己的「前缀×后缀」生成表，这正是 u3-l3 的主题，本讲只需知道入口分布。

四个属性宏里，最常用的是 `test`（[src/gpui_macros.rs:L188-L191](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L188-L191)）；`bench`（[L203-L206](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L203-L206)）与 `property_test`（[L276-L279](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L276-L279)）结构相同。最特殊的是 `derive_inspector_reflection`（[L297-L301](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L297-L301)），它同时被 `#[cfg(any(feature = "inspector", debug_assertions))]` 门控——release 构建下这个宏不存在，使用它的代码必须同样用 cfg 保护（4.2 节会看到 gpui 如何做到这一点）：

```rust
#[cfg(any(feature = "inspector", debug_assertions))]
#[proc_macro_attribute]
pub fn derive_inspector_reflection(_args: TokenStream, input: TokenStream) -> TokenStream {
    derive_inspector_reflection::derive_inspector_reflection(_args, input)
}
```

汇总成一张总索引表（「gpui 侧使用处」列的出处见 4.2 节逐一精读）：

| 宏 | 类别 | 入口（gpui_macros.rs） | 实现模块 | gpui 侧使用处 |
| --- | --- | --- | --- | --- |
| `Action` | 派生 | L19-L22 | derive_action.rs | action.rs:3 再导出；actions! 展开产物 |
| `IntoElement` | 派生 | L34-L37 | derive_into_element.rs | gpui.rs:109-111 再导出；ui crate 大量使用 |
| `Render` | 派生（doc hidden） | L39-L43 | derive_render.rs | gpui.rs:109-111 再导出；本 crate tests/render_test.rs:5 |
| `AppContext` | 派生 | L58-L61 | derive_app_context.rs | gpui.rs:109-111 再导出；tests/derive_context.rs:6 |
| `VisualContext` | 派生 | L91-L94 | derive_visual_context.rs | 同上 |
| `register_action` | 函数式 | L27-L30 | register_action.rs | gpui.rs:109-111 再导出；gpui/tests/action_macros.rs:26 |
| `style_helpers` | 函数式（doc hidden） | L97-L101 | styles.rs:40 | styled.rs:26（trait 体内调用） |
| `visibility_style_methods` | 函数式 | L104-L107 | styles.rs:50 | styled.rs:27 |
| `margin_style_methods` | 函数式 | L110-L113 | styles.rs:72 | styled.rs:28 |
| `padding_style_methods` | 函数式 | L116-L119 | styles.rs:86 | styled.rs:29 |
| `position_style_methods` | 函数式 | L122-L125 | styles.rs:100 | styled.rs:30 |
| `overflow_style_methods` | 函数式 | L128-L131 | styles.rs:129 | styled.rs:31 |
| `cursor_style_methods` | 函数式 | L134-L137 | styles.rs:159 | styled.rs:32 |
| `border_style_methods` | 函数式 | L140-L143 | styles.rs:335 | styled.rs:33 |
| `box_shadow_style_methods` | 函数式 | L146-L149 | styles.rs:384 | styled.rs:34 |
| `test` | 属性 | L188-L191 | test.rs | gpui.rs:109-111 再导出；gpui/src 内 72 处 |
| `bench` | 属性 | L203-L206 | bench.rs | gpui.rs:109-111 再导出；配套 gpui.rs:120-136 |
| `property_test` | 属性 | L276-L279 | property_test.rs | gpui.rs:109-111 再导出 |
| `derive_inspector_reflection` | 属性（cfg 门控） | L297-L301 | derive_inspector_reflection.rs | styled.rs:18-21（cfg_attr 修饰 trait） |

#### 4.1.4 代码实践

**实践目标**：不看本讲表格，独立从 `gpui_macros.rs` 提取全部宏入口并分类。

**操作步骤**：

1. 在 zed 仓库根目录执行：
   ```bash
   rg -n '#\[proc_macro' crates/gpui_macros/src/gpui_macros.rs
   ```
2. 观察输出：三种标注 `#[proc_macro_derive(...)]`、`#[proc_macro]`、`#[proc_macro_attribute]` 各出现多少次，下一个非空行就是对应的 `pub fn` 入口名。
3. 再执行 `rg -n '^mod |^#\[cfg' crates/gpui_macros/src/gpui_macros.rs`，对照文件头部的模块声明，统计实现模块数量。
4. 用 `rg -n 'attributes\(' crates/gpui_macros/src/gpui_macros.rs` 找出所有注册了 helper 属性的派生宏。

**需要观察的现象**：`#[proc_macro` 开头的匹配恰好 19 行；模块声明 11 个（其中 `derive_inspector_reflection` 带 cfg 门控）；`attributes(...)` 出现在 Action（`action`）、AppContext（`app`）、VisualContext（`window, app`）三处。

**预期结果**：你自己数出的分类数（5 派生 + 10 函数式 + 4 属性）与本讲 4.1.2 的推导一致。行号会随仓库版本漂移，以你本地 HEAD 的实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`#[doc(hidden)]` 分别出现在哪两个宏上？为什么要隐藏它们？

**参考答案**：出现在 `Render` 派生宏（gpui_macros.rs L40）和 `style_helpers`（L98）上。它们是给 gpui 内部（框架自身的 trait 定义）用的实现细节，不属于面向业务的公开 API；隐藏后文档与 IDE 补全不会引导用户直接调用，降低误用面。

**练习 2**：为什么 `VisualContext` 的 helper 属性是 `attributes(window, app)` 两个，而 `AppContext` 只有一个 `attributes(app)`？

**参考答案**：`AppContext` 派生只需要在结构体里找到持有 `&mut App` 的字段（用 `#[app]` 标记）；`VisualContext` 则是在 AppContext 之上叠加 Window 能力，必须同时定位 `#[app]` 与 `#[window]` 两个字段才能生成委托代码。缺少任一属性时宏会输出 `compile_error!`（u3-l1、u3-l2 会精读这两个宏）。

**练习 3**：在 release 构建（无 `inspector` feature、`debug_assertions` 关闭）下，`derive_inspector_reflection` 宏还存在吗？使用它的 gpui 代码为什么不会编译失败？

**参考答案**：不存在，入口与实现模块都被 `#[cfg(any(feature = "inspector", debug_assertions))]` 编译掉了。gpui 侧不是直接写 `#[derive_inspector_reflection]`，而是通过 `#[cfg_attr(条件, gpui_macros::derive_inspector_reflection)]` 条件施加属性（见 4.2.3），条件两端一致，因此任何构建配置下都不会引用一个不存在的宏。

### 4.2 gpui 侧使用点：再导出链与关键调用点

#### 4.2.1 概念说明

宏的「定义」在 gpui_macros，但宏的「价值」在消费方。gpui 消费这些宏有三种典型姿势，理解这三种姿势比记住具体行号更重要：

1. **再导出**：把宏挂到 gpui 命名空间，让业务代码只依赖 gpui。
2. **直接调用**：在 gpui 自身的类型/trait 定义处展开宏（样式宏、`derive_inspector_reflection`）。
3. **声明宏包装**：用 `macro_rules!` 写一层语法糖，糖衣里藏着过程宏（`actions!` 包住 `#[derive(Action)]`）。

#### 4.2.2 核心流程

```text
gpui_macros（19 个宏）
   │  pub use
   ▼
gpui（action.rs:3 / gpui.rs:109-111 / styled.rs:8-12）
   │  业务代码 use gpui::...
   ▼
zed 各业务 crate（ui、editor、workspace……）

特殊链路：actions!（macro_rules，action.rs:23-40）
   展开为 #[derive(..., gpui::Action)] + #[action(namespace = ...)]
   即：声明宏 → 派生宏 → register_action（inventory 注册）
```

#### 4.2.3 源码精读

**（1）Action 家族：一条「三层结构」的完整链路。**

[crates/gpui/src/action.rs:L1-L5](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L1-L5) 第一处再导出：`pub use gpui_macros::Action;`——从此业务代码写 `#[derive(Action)]`（导入自 gpui）即可：

```rust
pub use gpui_macros::Action;
```

[crates/gpui/src/action.rs:L11-L40](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L11-L40) 是 `actions!` 声明宏的完整定义。看第一个 arm：它为列表中的每个名字生成一个单元结构体，derive 列表里赫然有 `gpui::Action`，并且自动加上 `#[action(namespace = $namespace)]`：

```rust
#[macro_export]
macro_rules! actions {
    ($namespace:path, [ $( $(#[$attr:meta])* $name:ident),* $(,)? ]) => {
        $(
            #[derive(::std::clone::Clone, ::std::cmp::PartialEq, ::std::default::Default,
                     ::std::fmt::Debug, gpui::Action)]
            #[action(namespace = $namespace)]
            $(#[$attr])*
            pub struct $name;
        )*
    };
    ([ ... ]) => { /* 第二个 arm 省略 namespace，其余相同 */ };
}
```

这就是「声明宏调用过程宏」的实证：`actions!(editor, [MoveUp])` 展开后，`MoveUp` 是一个普通单元结构体，真正干活的是它身上的 `#[derive(gpui::Action)]`。第二个 arm 允许省略 namespace（文档注明 Zed 的 action 必须带 namespace，这是给测试等场景用的）。

gpui 自己也消费这条链路。[crates/gpui/src/action.rs:L425-L447](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L425-L447) 的内联模块 `no_action` 里，内置的 `NoAction` 用 `actions!(zed, [...])` 定义，`Unbind` 则因为带字段而直接用派生宏加 `#[action(namespace = zed)]`：

```rust
mod no_action {
    ...
    actions!(
        zed,
        [ /// Action with special handling which unbinds ... 
          NoAction ]
    );

    #[derive(Clone, Debug, PartialEq, Deserialize, JsonSchema, gpui::Action)]
    #[action(namespace = zed)]
    pub struct Unbind(pub gpui::SharedString);
```

注册的收集端在 [crates/gpui/src/action.rs:L415-L423](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L415-L423)：`generate_list_of_all_registered_actions` 通过 `inventory::iter` 遍历所有提交的 `MacroActionBuilder`——这解释了为什么派生宏生成的代码里会藏一段 `inventory::submit!`（u2-l3 专题展开）：

```rust
pub fn generate_list_of_all_registered_actions() -> impl Iterator<Item = MacroActionData> {
    inventory::iter::<MacroActionBuilder>
        .into_iter()
        .map(|builder| builder.0())
}
```

三层用法（声明宏 / 派生宏 / 手动实现 + `register_action!`）有一个现成的对照标本：[crates/gpui/tests/action_macros.rs:L6-L55](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/tests/action_macros.rs#L6-L55)。这个测试同时演示了 `actions!(test_only, [...])`（L8-L15）、`#[derive(..., Action)] #[action(namespace = test_only)]`（L17-L20）与 `register_action!(RegisterableAction)` + 手写 `impl gpui::Action`（L26-L54）三种方式，是理解 Action 宏分层最好的单文件教材。

**（2）Styled trait：9 个样式宏的集中调用点。**

[crates/gpui/src/styled.rs:L8-L12](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs#L8-L12) 把 8 个样式宏再导出到 gpui 命名空间，使下游 crate 能在自己的 trait（如 ui crate 的 `StyledExt`，见 [crates/ui/src/traits/styled_ext.rs:L26](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/ui/src/traits/styled_ext.rs#L26)）里复用同一套生成器：

```rust
pub use gpui_macros::{
    border_style_methods, box_shadow_style_methods, cursor_style_methods, margin_style_methods,
    overflow_style_methods, padding_style_methods, position_style_methods,
    visibility_style_methods,
};
```

真正的调用发生在 trait 体内。[crates/gpui/src/styled.rs:L17-L34](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs#L17-L34)：

```rust
// gate on rust-analyzer so rust-analyzer never needs to expand this macro,
// it takes up to 10 seconds to expand due to inefficiencies in rust-analyzers proc-macro srv
#[cfg_attr(
    all(any(feature = "inspector", debug_assertions), not(rust_analyzer)),
    gpui_macros::derive_inspector_reflection
)]
pub trait Styled: Sized {
    /// Returns a reference to the style memory of this element.
    fn style(&mut self) -> &mut StyleRefinement;

    gpui_macros::style_helpers!();
    gpui_macros::visibility_style_methods!();
    gpui_macros::margin_style_methods!();
    gpui_macros::padding_style_methods!();
    gpui_macros::position_style_methods!();
    gpui_macros::overflow_style_methods!();
    gpui_macros::cursor_style_methods!();
    gpui_macros::border_style_methods!();
    gpui_macros::box_shadow_style_methods!();
    ...
```

这段不足 20 行的代码蕴含四个关键信息：

1. **函数式宏在 trait 体内展开**：`margin_style_methods!()` 展开出的不是独立函数，而是 `Styled` trait 的**带默认实现的方法**（如 `fn mt_2(mut self) -> Self { ... }`）。任何实现 `Styled` 的类型（`div()`、`img()`……）立刻获得全部数百个链式样式方法。
2. **8 个宏按「样式维度」切分**：可见性、外边距、内边距、定位、溢出、光标、边框、阴影各由一个宏负责，`styles.rs` 里每个入口函数维护自己的前缀表（`m`/`p`/`w`/`h`……）× 后缀表（尺寸档位）。拆成 8 个而非合并成 1 个，让每个宏的展开体量与维护边界都可控——每张表独立演进，互不干扰。
3. **还有第 9 个隐藏宏 `style_helpers!()`**（L26，doc hidden）：它生成不属于上述八个维度的通用助手方法，与 8 个显式宏共同构成完整方法集。
4. **宏展开开销是真实约束**：L17 的注释明说——`derive_inspector_reflection` 在 rust-analyzer 的 proc-macro 服务里展开「最多要花 10 秒」，所以用 `not(rust_analyzer)` 把它挡在 IDE 之外。这从侧面解释了样式宏为什么要按维度拆分：单次展开越小，IDE 与增量编译的代价越低。同时注意 `cfg_attr` 的条件与宏本身的 cfg 门控（`inspector` 或 `debug_assertions`）保持一致，两端同步开关。

`derive_inspector_reflection` 生成的代码依赖 gpui 侧的运行时类型。[crates/gpui/src/inspector.rs:L225-L253](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/inspector.rs#L225-L253) 定义了 `FunctionReflection<T>`：名字、函数指针、文档三件套，`invoke` 用 `Box<dyn Any>` 完成类型擦除后按名调用——这是「宏在编译期收集、类型在运行时调用」的另一半（u4-l5 专题展开）：

```rust
/// Provides definitions used by `#[derive_inspector_reflection]`.
#[cfg(any(feature = "inspector", debug_assertions))]
pub mod inspector_reflection {
    pub struct FunctionReflection<T> {
        pub name: &'static str,
        pub function: fn(Box<dyn Any>) -> Box<dyn Any>,
        pub documentation: Option<&'static str>,
        pub _type: std::marker::PhantomData<T>,
    }
```

**（3）批量再导出与 `#[gpui::test]`。**

[crates/gpui/src/gpui.rs:L109-L111](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L109-L111) 一次性再导出 8 个宏——5 个派生宏中的 4 个（`Render` 也在其中）加上 `bench`、`property_test`、`register_action`、`test` 四个属性/函数式宏：

```rust
pub use gpui_macros::{
    AppContext, IntoElement, Render, VisualContext, bench, property_test, register_action, test,
};
```

`#[gpui::test]` 是整个 gpui 代码库里使用频率最高的宏：在 `crates/gpui/src` 下有 72 处调用、分布在 14 个文件（本讲用 ripgrep 实测统计）。典型样板见 [crates/gpui/src/window.rs:L6958-L6959](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6958-L6959)——被标注函数按类型注入 `TestAppContext`，这正是 u4-l1/u4-l2 的主题：

```rust
#[gpui::test]
fn test_frame_waker_fires_on_frame_demand(cx: &mut TestAppContext) {
```

`bench` 宏有配套的声明宏。[crates/gpui/src/gpui.rs:L114-L136](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L114-L136) 定义 `bench_group!` / `bench_main!`，它们只是 `criterion::criterion_group!` / `criterion_main!` 的镜像，让 GPUI 基准文件保持与普通 Criterion 相同的形状。

最后看两个「本 crate 自产自销」的使用点：[tests/derive_context.rs:L6-L12](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/tests/derive_context.rs#L6-L12) 同时派生 `AppContext` 与 `VisualContext`（`#[app]`/`#[window]` 标记字段）；[tests/render_test.rs:L5-L6](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/tests/render_test.rs#L5-L6) 派生 `Render`。值得注意的是 `IntoElement`：在 `crates/gpui/src` 内 grep 不到任何 `#[derive(IntoElement)]` 的真实调用（gpui 的内置元素都是手写 `impl IntoElement`，仅 [crates/gpui/src/element.rs:L30](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L30) 的模块文档提到它），它的主战场在下游组件层——例如 [crates/ui/src/components/button/button.rs:L78](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/ui/src/components/button/button.rs#L78) 的 `#[derive(IntoElement, Documented, RegisterComponent)]`。这个事实本身就是一条架构信息：**gpui 提供宏给框架层用，业务组件 crate 才是派生宏的主要消费者**。

#### 4.2.4 代码实践

**实践目标**：亲手验证「再导出链」——证明业务代码不必直接依赖 gpui_macros。

**操作步骤**：

1. 执行 `rg -n 'pub use gpui_macros' crates/gpui/src/`，列出 gpui 的全部再导出点。
2. 执行 `rg -ln 'gpui_macros' crates/ui/src/ | head`，检查 ui crate 是否直接引用 gpui_macros。
3. 任选 ui crate 的一个组件文件（如 `button.rs`），阅读其 `use gpui::...` 导入列表，确认 `IntoElement` 来自 gpui 而非 gpui_macros。

**需要观察的现象**：步骤 1 至少命中 action.rs:3、styled.rs:8、gpui.rs:109 三处；步骤 2 几乎没有命中（ui crate 通过 `gpui::` 路径使用宏）。

**预期结果**：得到「gpui_macros → gpui（3 个再导出点）→ ui/业务 crate」的依赖链证据。若步骤 2 出现个别命中，观察它引用的是什么（可能是 dev-dependency 下的测试代码），并思考为什么（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`actions!(editor, [MoveUp])` 与手写 `#[derive(Clone, PartialEq, Default, Debug, Action)] #[action(namespace = editor)] pub struct MoveUp;` 是什么关系？

**参考答案**：完全等价。`actions!` 是 macro_rules 语法糖，第二个 arm 还能省略 namespace。糖的价值是让「批量定义一组无字段 action」压缩到一行，并强制统一 namespace 与四个基础 derive。

**练习 2**：为什么 gpui 内置元素（如 div）不用 `#[derive(IntoElement)]`？

**参考答案**：`IntoElement` 派生宏的生成策略是「把 `RenderOnce` 类型包进 `ViewElement`」，面向的是「构造完就转成元素」的组件类型；而 div 这类框架内置元素有定制的元素生命周期实现，直接手写 `impl IntoElement` 更精确。派生宏服务的是下游组件层（ui crate 的 Button、IconButton 等都是 `#[derive(IntoElement)]`）。

**练习 3**：`Styled` trait 的 `derive_inspector_reflection` 为什么用 `#[cfg_attr(...)]` 而不是直接写 `#[gpui_macros::derive_inspector_reflection]`？

**参考答案**：这个宏只在 `inspector` feature 或 `debug_assertions` 下存在（gpui_macros.rs L297 的 cfg）。若直接标注，release 构建会因「宏不存在」而失败。`cfg_attr(条件, 属性)` 让属性仅在条件成立时施加，与宏的 cfg 门控两端对齐；同时还叠加了 `not(rust_analyzer)` 以规避 IDE 展开该宏的巨大开销。

### 4.3 grep 检索宏用法的方法

#### 4.3.1 概念说明

宏调用在源码里有三种「藏身之处」：真实调用、文档注释里的示例（doc comment / doctest）、以及字符串。检索时若不加以区分，会把文档示例误认为使用点——例如 `action.rs:115` 的 `register_action!(Paste);` 就写在 `///` 文档注释里，是示例而非真实调用。本模块给出一套可复用的检索流程。

#### 4.3.2 核心流程

```text
定位一个宏的完整链路（以 X 为例）：
1. rg -n 'pub fn X|#\[proc_macro' crates/gpui_macros/src/gpui_macros.rs
      → 找到入口声明与转发的实现模块
2. rg -n 'X!' crates/gpui/src/            → 找 gpui 侧调用点（注意区分 doc 注释）
3. rg -n '#\[derive\(.*X\]' crates/      → 派生宏要换一种模式
4. rg -c 统计规模，rg -l 列出文件
5. 用 cargo expand 或 IDE「转到定义」交叉验证一处
```

#### 4.3.3 源码精读

没有新源码，这里给出一组**经过实测的检索模式与结果**（基于当前 HEAD `fe9556a11e`，均可用 ripgrep 复现）：

| 检索目标 | rg 模式 | 实测结果（crates/gpui/src 范围） |
| --- | --- | --- |
| 样式宏调用 | `gpui_macros::` | 12 处：styled.rs 的 9 次宏调用 + action.rs:3 + styled.rs:8 + gpui.rs:109 的再导出 |
| `style_helpers!` | `style_helpers!` | 1 处：styled.rs:26 |
| action 声明宏 | `actions!\(` | action.rs:430（no_action 模块内）等；全仓库更多 |
| 测试宏 | `#\[gpui::test\]` | 72 处 / 14 个文件（window.rs 6 处、interactive.rs 2 处……） |
| 反射宏 | `derive_inspector_reflection` | 2 处：styled.rs:20（cfg_attr 使用点）、inspector.rs:225（文档提及） |
| IntoElement 派生 | `derive\(.*IntoElement` | gpui/src 内 0 处真实调用（仅 element.rs:30 文档提及）；ui crate 内大量命中 |
| register_action | `register_action!` | gpui/src 内仅 action.rs:115（文档示例）；真实调用在 gpui/tests/action_macros.rs:26 |

三个实用技巧：

1. **转义正则元字符**：`#[gpui::test]` 里的 `[` `]` 必须写成 `#\[gpui::test\]`；`derive(...)` 的括号同理。
2. **区分文档与代码**：给结果加 `-n` 看行号后打开文件确认上下文；doc 注释行以 `///` 或 `//!` 开头。上表 `register_action!` 一行就是典型陷阱。
3. **规模感**：用 `rg -c`（每文件计数）代替逐条列举，快速判断「这是内部宏（1~2 处）还是基础设施宏（数十处）」——`style_helpers!` 只有 1 处调用 vs `#[gpui::test]` 有 72 处，两者的定位一目了然。

#### 4.3.4 代码实践

**实践目标**：完成本讲规格指定的检索任务——制作「宏 → 调用文件:行号」对照表，并回答「Styled trait 中连续调用 8 个样式宏的原因」。

**操作步骤**：

1. 在 zed 仓库根目录依次执行（均在 `crates/gpui/src` 范围内）：
   ```bash
   rg -n 'style_helpers!' crates/gpui/src/
   rg -n 'actions!\(' crates/gpui/src/
   rg -n '#\[gpui::test\]' crates/gpui/src/
   rg -n 'derive_inspector_reflection' crates/gpui/src/
   ```
2. 把四组结果整理成一张表：宏名 | 调用文件 | 行号 | 该处代码做了什么（一句话）。
3. 打开 `crates/gpui/src/styled.rs` 第 22-34 行，逐行阅读 `Styled` trait 体内的 9 个宏调用，结合 4.2.3 的分析写下「为什么是 8 个连续调用」。

**需要观察的现象**：

- `style_helpers!` 只出现在 styled.rs:26；
- `actions!(` 在 action.rs:430 附近命中（no_action 模块）；
- `#[gpui::test]` 大量命中，集中在 window.rs、app.rs、test.rs 等文件的 `#[cfg(test)]` 模块内；
- `derive_inspector_reflection` 仅 2 处命中，其一在 styled.rs:20 的 `cfg_attr` 里，其二在 inspector.rs:225 的文档注释里。

**预期结果**：对照表与 4.3.3 的实测表一致（行号以你本地 HEAD 为准）；对「8 个样式宏」的回答应覆盖三点——(a) 每个宏负责一个独立样式维度，展开成 trait 默认方法；(b) 按维度拆分使 `styles.rs` 中每张前缀×后缀表独立维护、单次展开体量可控（styled.rs:17 的注释证明展开开销是真实约束）；(c) 它们与第 9 个隐藏宏 `style_helpers!()` 共同构成 `Styled` 的完整方法集。若你的统计与本表不符，先检查是否漏掉了 doc 注释中的示例（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如何用一条 rg 命令统计 `#[gpui::test]` 在整个 zed 仓库（crates/ 目录）的使用规模？

**参考答案**：`rg -c '#\[gpui::test\]' crates/ | awk -F: '{s+=$2} END {print s}'`，或直接 `rg '#\[gpui::test\]' crates/ | wc -l`。前者按文件计数后汇总，后者逐行计数；注意转义方括号。

**练习 2**：你在 `crates/gpui/src/action.rs` grep `register_action!` 只命中 L115，能否据此断言「gpui 从不使用这个宏」？

**参考答案**：不能。L115 位于 `///` 文档注释中，只是 doctest 示例；真实调用点在集成测试 `crates/gpui/tests/action_macros.rs:26`（`register_action!(RegisterableAction);`）。检索宏用法时必须同时覆盖 `src/` 与 `tests/`，并区分文档示例与真实调用。

**练习 3**：想找出「哪些类型派生了 `VisualContext`」，应该用什么模式？

**参考答案**：派生宏不能按 `X!` 检索，要用 `rg -n 'derive\([^\)]*VisualContext\)' crates/`（容忍同一 derive 里并列的其他 trait）。本仓库已知命中是 gpui_macros 自己的 tests/derive_context.rs:6；若在更大范围（如 Zed 主仓库历史版本）检索，还可能命中各 crate 自定义的窗口上下文包装类型。

## 5. 综合实践

**任务：制作你自己的《gpui_macros 宏速查表》。**

把 4.1.3 的 19 行索引表扩展成一份属于你的速查表，要求：

1. **四列索引**：宏名 | 类别与入口行号（gpui_macros.rs） | 实现模块 | 至少一个真实使用处（文件:行号）。样式宏 9 个可以合并为一行，但需注明「入口 L97-L149，实现 styles.rs，调用 styled.rs:26-34」。
2. **检索留痕**：每个使用处都必须来自你自己执行的 rg 命令，把命令记在表格下方（参考 4.3 的模式库）。
3. **交叉验证一处**：任选 `#[gpui::test]` 的一个调用点（如 window.rs 内的某个测试），在编辑器中对 `gpui::test` 执行「转到定义」，确认落脚点是 gpui_macros.rs 的 L188-L191 入口——验证「再导出链」的最后一环。
4. **一问一答**：在表末用三五行回答：如果 Zed 团队明天新增第十个样式维度（如 `filter`），按照现有架构需要改动哪几个文件？

预期答案要点（第 4 问）：在 `styles.rs` 新增一个入口函数（带 `filter` 的前缀表）；在 `gpui_macros.rs` 声明 `#[proc_macro] pub fn filter_style_methods` 并转发；在 `styled.rs` 的 `Styled` trait 体内追加一次 `gpui_macros::filter_style_methods!();` 并视需要加入 styled.rs:8-12 的再导出。三处改动、零业务代码修改——这正是「宏集中生成方法」架构的意义。

本实践无需写 Rust 代码，全部为源码阅读与检索操作；若条件允许，第 3 步的「转到定义」也可用 `cargo doc --open -p gpui` 在文档中点击宏名替代（待本地验证）。

## 6. 本讲小结

- gpui_macros 共 **19 个宏入口**：5 派生 + 10 函数式 + 4 属性，对应 11 个实现模块；8 个样式宏共享 `styles.rs`，`derive_inspector_reflection` 受 `inspector` feature / `debug_assertions` 双重门控。
- gpui 通过三处再导出（action.rs:3、styled.rs:8-12、gpui.rs:109-111）把宏挂进自己的命名空间，业务 crate 只需依赖 `gpui`；`IntoElement` 等派生宏的主要消费者是下游组件层（ui crate），gpui 内置元素反而手写实现。
- `actions!` 是「声明宏包过程宏」的典型：macro_rules 展开出带 `#[derive(gpui::Action)]` 与 `#[action(namespace = ...)]` 的单元结构体；`register_action!` 是手动实现 trait 时的低层注册通道，两者最终都汇入 inventory 收集的 `generate_list_of_all_registered_actions`。
- `Styled` trait 体内连续 9 次宏调用（8 个显式样式宏 + 隐藏的 `style_helpers!`）把数百个 Tailwind 风格方法变成 trait 默认实现；按维度拆分让每张前缀×后缀表独立维护，且单次展开体量可控——styled.rs:17 的 rust-analyzer 注释证明宏展开开销是真实约束。
- 检索宏用法的三个要点：正则转义 `[]()`、区分 doc 注释示例与真实调用、用 `rg -c` 建立规模感（`style_helpers!` 1 处 vs `#[gpui::test]` 72 处）。

## 7. 下一步学习建议

本讲建立了全景地图，下一讲起进入单元二，从最复杂的 `#[derive(Action)]` 开始逐个精读实现：

- **u2-l1**：`derive_action.rs` 的属性解析——`parse_nested_meta` 如何识别 `name`/`namespace`/`no_json`/`no_register`/`deprecated_aliases`/`deprecated` 六个参数。建议先重读 [crates/gpui/src/action.rs:L55-L116](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L55-L116) 的 trait 文档，那里逐条列出了六个参数的语义，是派生宏行为的「需求说明书」。
- **u2-l4**：两个最小的派生宏 `IntoElement` 与 `Render`，适合作为读懂 `quote!` 模板的热身。
- 若你想先巩固本讲的检索能力，可自行统计 `actions!` 在 `crates/editor/src` 中的出现次数，并与 `#[derive(Action)]` 的直接使用次数对比，验证「简单 action 走声明宏、带字段 action 走派生宏」的分工直觉。
