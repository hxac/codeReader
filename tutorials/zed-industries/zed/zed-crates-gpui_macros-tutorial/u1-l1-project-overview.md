# gpui_macros 是什么：GPUI 背后的宏引擎

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `gpui_macros` 在 Zed/GPUI 架构中扮演的角色：它是 GPUI 框架专属的**过程宏（proc-macro）crate**，为 `gpui` 提供 Action 派生、样式方法生成、测试与基准宏等能力。
2. 理解 `Cargo.toml` 中 `proc-macro = true`、`[lib] path = "src/gpui_macros.rs"`、`inspector` feature 各自的含义。
3. 看懂库入口 `gpui_macros.rs` 的模块组织方式：所有宏入口如何通过 `mod` 声明转发到各自的实现文件。
4. 掌握本 crate 的构建与测试命令，知道它的测试来自哪里。

本讲是整个学习手册的第一篇，不要求你已经读过任何 GPUI 代码。我们会从最基础的概念讲起。

## 2. 前置知识

### 2.1 宏是什么

Rust 的宏是一种「在编译期生成代码的代码」。写下一个宏调用，编译器会先把它展开成普通的 Rust 代码，再继续编译。宏分为两大类：

- **声明宏（macro_rules!）**：用模式匹配的方式做文本层面的展开，比如标准库的 `vec![]`。
- **过程宏（procedural macro）**：接收一段 Token 流（代码的词法单元序列），用普通 Rust 函数处理它，再输出新的 Token 流。过程宏功能更强，可以解析并理解代码结构。

`gpui_macros` 里的所有宏都是过程宏。过程宏又分三种形式，本 crate 三种都有：

| 形式 | 标注 | 例子 |
| --- | --- | --- |
| 派生宏（derive macro） | `#[proc_macro_derive(...)]` | `#[derive(Action)]` |
| 属性宏（attribute macro） | `#[proc_macro_attribute]` | `#[gpui::test]` |
| 函数式宏（function-like） | `#[proc_macro]` | `style_helpers!()` |

### 2.2 crate 与 Cargo.toml

一个 **crate** 是 Rust 的最小编译单元。每个 crate 根目录下的 `Cargo.toml` 描述它的名字、依赖、feature 开关等信息。Zed 仓库是一个 **workspace**（工作区），里面包含几百个 crate，`Cargo.toml` 中形如 `syn.workspace = true` 的写法表示「依赖版本由 workspace 根统一管理」。

### 2.3 feature 是什么

feature 是 Cargo 提供的可选编译开关。声明 `inspector = []` 后，其他 crate 可以用 `features = ["inspector"]` 或 `--features inspector` 打开它，源码里再用 `#[cfg(feature = "inspector")]` 控制哪些代码参与编译。

### 2.4 前置技能

- 会在命令行执行 `cargo build`、`cargo test`。
- 了解基本的 Rust 语法（结构体、trait、模块）即可，本讲不需要你写过过程宏。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_macros/Cargo.toml` | 声明这是一个 proc-macro crate，定义依赖与 `inspector` feature |
| `crates/gpui_macros/src/gpui_macros.rs` | **唯一的库入口**：声明所有 `mod`，并用 `#[proc_macro*]` 标注导出全部宏 |
| `crates/gpui_macros/src/*.rs` 其余 11 个文件 | 各个宏的具体实现（如 `derive_action.rs`、`styles.rs`、`test.rs`），本讲只建立索引不深入 |
| `crates/gpui_macros/tests/` | 三个集成测试文件，验证派生宏生成的代码能正确编译运行 |
| `crates/gpui/Cargo.toml` | 上游消费者：`gpui` 依赖 `gpui_macros`，并把 `inspector` feature 转发过来 |
| `crates/gpui/src/gpui.rs` | `gpui` 对本 crate 宏的再导出位置 |

## 4. 核心概念与源码讲解

### 4.1 crate 定位与 Cargo.toml 结构

#### 4.1.1 概念说明

GPUI 是 Zed 编辑器的 UI 框架，它把很多「重复的样板代码」交给宏自动生成。例如：

- 每一个用户可绑定的快捷键动作（Action）都要实现一个包含十几个方法的 trait——`#[derive(Action)]` 帮你生成。
- Tailwind 风格的样式方法（`mt_2()`、`px_4()`、`rounded_md()`……）有数百个——样式宏按「前缀 × 后缀」的表批量生成。
- GPUI 测试需要特殊的运行环境——`#[gpui::test]` 帮你生成标准 `#[test]` 骨架。

这些宏全部收拢在一个**独立的 crate** 里，而不是直接写在 `gpui` 内部。这是 Rust 生态的硬性要求：**过程宏必须放在单独的、标记了 `proc-macro = true` 的 crate 中**，不能和普通代码混在同一个 crate。

于是依赖方向是：

```text
        编译期（rustc 加载宏）
gpui_macros ──生成代码──▶ gpui（以及 Zed 其他 crate 通过 gpui 间接使用）
    │
    └─ 依赖：syn / quote / proc-macro2 / heck（只此四个）
```

一个值得注意的细节：`gpui_macros` 的 `[dev-dependencies]` 里出现了 `gpui` 本身。这并不构成循环依赖，因为 **dev-dependency 只在测试和示例中生效**，不影响正常构建的依赖方向——宏 crate 在编译期给 `gpui` 生成代码，`gpui` 在测试期反过来帮宏 crate 验证生成的代码可用。

#### 4.1.2 核心流程

理解这个 crate 定位的要点：

1. `gpui_macros` 是**纯编译期工具**：它编译成一个由 rustc 加载的动态库，自身不参与程序运行时。
2. 它的运行时依赖极少（4 个宏相关的库），其中 `heck`（用于把标识符转成 snake_case）只在反射宏里用到。
3. 上游 `gpui` 通过 feature 转发（`inspector = ["gpui_macros/inspector"]`）控制宏 crate 的行为分支。
4. `gpui` 再导出（re-export）了本 crate 的 8 个宏，所以业务代码通常写 `#[gpui::test]` 而不是直接依赖 `gpui_macros`。

#### 4.1.3 源码精读

先看完整的 `Cargo.toml`：

[crates/gpui_macros/Cargo.toml#L1-L27](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/Cargo.toml#L1-L27) —— 整个 crate 的构建配置，总共只有 27 行，逐段拆解如下。

**第一段：`[features]` 与 `[lib]`**

[crates/gpui_macros/Cargo.toml#L12-L18](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/Cargo.toml#L12-L18) —— 这 7 行是本 crate 最重要的配置：

```toml
[features]
inspector = []

[lib]
path = "src/gpui_macros.rs"
proc-macro = true
doctest = true
```

- `inspector = []`：定义一个空的 feature。空的 `[]` 表示它不启用任何额外依赖，只是一个编译期开关，供 `#[cfg(feature = "inspector")]` 使用。
- `path = "src/gpui_macros.rs"`：指定库入口文件。注意这里**不是**默认的 `src/lib.rs`，而是与 crate 同名的 `gpui_macros.rs`——这是 Zed 仓库的统一风格（见仓库 CLAUDE.md 的约定），好处是文件名即 crate 名，一眼可辨。
- `proc-macro = true`：**本 crate 的灵魂**。它告诉 Cargo「这个 crate 编译目标是过程宏动态库，只能导出 `#[proc_macro]` 系列标注的项，不能导出普通函数或类型」。
- `doctest = true`：开启文档测试。宏的文档注释里写的示例代码会被当作测试运行，这一点在 4.3 节展开。

**第二段：依赖**

[crates/gpui_macros/Cargo.toml#L20-L27](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/Cargo.toml#L20-L27) —— 全部依赖只有 4 + 1 个：

```toml
[dependencies]
heck.workspace = true
proc-macro2.workspace = true
quote.workspace = true
syn.workspace = true

[dev-dependencies]
gpui = { workspace = true, features = ["inspector"] }
```

- `syn`：把 Token 流**解析**成 Rust 语法树（「读」代码）。
- `quote`：把语法树**生成**回 Token 流（「写」代码）。
- `proc-macro2`：`syn`/`quote` 的底层基础设施，提供与编译器无关的 `TokenStream` 类型，让宏代码可以被普通单元测试。
- `heck`：命名风格转换库，仅在反射宏中把 trait 名转成 snake_case 模块名（使用点见 `src/derive_inspector_reflection.rs` 第 4 行的 `use heck::ToSnakeCase as _;`）。
- dev-dependency 的 `gpui` 带 `inspector` feature，供集成测试使用。

**上游视角：gpui 如何消费它**

[crates/gpui/Cargo.toml#L30](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/Cargo.toml#L30) —— `gpui` 的 `inspector` feature 直接转发给宏 crate：`inspector = ["gpui_macros/inspector"]`，这是 feature 穿透两个 crate 的接线方式。

[crates/gpui/Cargo.toml#L60](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/Cargo.toml#L60) —— 普通依赖声明 `gpui_macros.workspace = true`。

[crates/gpui/src/gpui.rs#L109-L111](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L109-L111) —— `gpui` 对外再导出的 8 个宏：`AppContext, IntoElement, Render, VisualContext, bench, property_test, register_action, test`。所以日常写 `#[gpui::test]` 时用的其实是这里转手出来的名字。样式宏和 `Action` 派生没有走这个再导出，而是被 `gpui` 内部直接使用——例如 [crates/gpui/src/styled.rs#L26-L34](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs#L26-L34) 在 `Styled` trait 里连续调用了全部 9 个样式宏。这个话题会在 u1-l3 详讲。

#### 4.1.4 代码实践

**实践一：验证依赖边界**

1. **实践目标**：确认 `gpui_macros` 的依赖列表确实只有 4 个，并理解 `gpui` 与它 feature 转发的关系。
2. **操作步骤**：
   - 在 Zed 仓库根目录执行 `cargo tree -p gpui_macros --depth 1`（只看一层依赖）。
   - 打开 `crates/gpui_macros/Cargo.toml` 与 `crates/gpui/Cargo.toml`，对照上面 4.1.3 的讲解。
3. **需要观察的现象**：`cargo tree` 输出中常规依赖只有 `heck`、`proc-macro2`、`quote`、`syn` 四项。
4. **预期结果**：依赖清单与 `Cargo.toml` 声明一致。（`cargo tree` 的具体输出格式随 Cargo 版本略有差异，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gpui` 不能把自己的宏写在 `gpui` crate 内部，必须拆出 `gpui_macros`？

**答案**：Rust 规定过程宏必须是 `proc-macro = true` 的独立 crate。这类 crate 编译成动态库供 rustc 在编译期加载，与普通库的编译产物完全不同，两者不能混装。这也是全生态「`xxx` + `xxx_macros/xxx_derive`」双 crate 模式的由来。

**练习 2**：`gpui_macros` 的 `[dev-dependencies]` 依赖 `gpui`，而 `gpui` 的 `[dependencies]` 依赖 `gpui_macros`，这是循环依赖吗？会报错吗？

**答案**：不是，不会报错。`[dependencies]`（`gpui` → `gpui_macros`）决定正常构建顺序；`[dev-dependencies]` 只在编译测试/示例时生效，不影响库本身的构建，因此 Cargo 允许这种「测试期反向依赖」。它的用途是让宏 crate 的集成测试能真实使用 `gpui` 运行时来验证宏生成的代码。

**练习 3**：如果把 `[lib]` 里的 `path = "src/gpui_macros.rs"` 删掉，会发生什么？

**答案**：Cargo 会去找默认入口 `src/lib.rs`，而这个文件不存在，构建直接失败。Zed 仓库刻意用与 crate 同名的 `gpui_macros.rs` 作为入口，所以 `path` 声明不可省略。

### 4.2 库入口 gpui_macros.rs 的模块组织

#### 4.2.1 概念说明

对 proc-macro crate 来说，**入口文件里的 `#[proc_macro*]` 标注项就是它全部的公共 API**。`gpui_macros.rs` 采用「薄入口 + 厚实现」的组织方式：

- 入口文件只做两件事：声明 `mod`，以及为每个宏写一个一行转发的入口函数。
- 真正的解析与代码生成逻辑全部放在 `src/` 下与宏同名的实现文件里。

这种结构的好处：入口文件天然成了一张「宏目录」，读一遍就能知道这个 crate 提供什么；实现文件彼此独立，可以单独阅读、单独测试。

#### 4.2.2 核心流程

一个宏从被使用到生效的路径：

```text
用户代码写 #[derive(Action)]（或 gpui::test! 等）
        │
        ▼
rustc 识别出宏，按名字找到 gpui_macros crate 里对应的 #[proc_macro*] 入口函数
        │
        ▼
入口函数一行转发：derive_action::derive_action(input)
        │
        ▼
实现模块（如 src/derive_action.rs）用 syn 解析、用 quote 生成
        │
        ▼
返回 TokenStream，替换用户代码中的宏调用，继续编译
```

注意其中的一个条件编译特例：`derive_inspector_reflection` 模块只在 `inspector` feature 或 `debug_assertions`（debug 构建）下才存在——这决定了它导出的宏在 release 构建中根本不存在。

#### 4.2.3 源码精读

**模块声明区**

[crates/gpui_macros/src/gpui_macros.rs#L1-L13](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L1-L13) —— 文件开头的 11 个 `mod` 声明对应 `src/` 下 11 个实现文件：

```rust
mod bench;
mod derive_action;
mod derive_app_context;
mod derive_into_element;
mod derive_render;
mod derive_visual_context;
mod property_test;
mod register_action;
mod styles;
mod test;

#[cfg(any(feature = "inspector", debug_assertions))]
mod derive_inspector_reflection;
```

前 10 个是无条件编译的；最后一个 `derive_inspector_reflection` 被 `#[cfg(any(feature = "inspector", debug_assertions))]` 包住——只有开启 inspector feature **或** debug 构建（`debug_assertions` 是 Cargo 在非 release 编译时自动打开的内置 cfg）时才编译。

**三类宏入口**

入口文件里每个宏都是「文档注释 + `#[proc_macro*]` 标注 + 一行转发」。各举一例：

[crates/gpui_macros/src/gpui_macros.rs#L18-L22](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L18-L22) —— **派生宏**的样板：`#[proc_macro_derive(Action, attributes(action))]` 导出 `#[derive(Action)]`，第二个参数 `attributes(action)` 声明「这个派生允许字段/变体上出现 `#[action(...)]` 辅助属性」，函数体只有一行转发。

[crates/gpui_macros/src/gpui_macros.rs#L188-L191](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L188-L191) —— **属性宏**的样板：`#[proc_macro_attribute] pub fn test(args, function)`，属性宏接收两个 Token 流（属性括号里的参数 + 被标注的整个函数）。`#[gpui::test]` 上方长达 30 余行的文档注释就是它的使用手册，也是 4.1.3 提到的 doctest 素材。

[crates/gpui_macros/src/gpui_macros.rs#L109-L113](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L109-L113) —— **函数式宏**的样板：`#[proc_macro] pub fn margin_style_methods(input)`，对应调用形式 `margin_style_methods!()`。

**样式宏家族（9 个入口共用一个实现模块）**

[crates/gpui_macros/src/gpui_macros.rs#L96-L149](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L96-L149) —— 从 `style_helpers` 到 `box_shadow_style_methods` 共 9 个函数式宏入口，全部转发到 `styles::` 下的同名函数。其中 `style_helpers` 标了 `#[doc(hidden)]`（内部使用，不对外文档化）。多个入口共享一个实现模块，是「一个模块服务一族相关宏」的组织范例。

**条件编译的反射宏**

[crates/gpui_macros/src/gpui_macros.rs#L297-L301](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L297-L301) —— `derive_inspector_reflection` 入口同样带着 `#[cfg(any(feature = "inspector", debug_assertions))]`，与第 13 行的 `mod` 声明配套。这意味着 release 构建里这个宏不存在，调用它的代码必须自己处理这种情况（上游 `gpui` 用 `#[cfg_attr(not(rust_analyzer), gpui_macros::derive_inspector_reflection)]` 这类条件属性来配合，见 `crates/gpui/src/styled.rs` 第 20 行附近）。

**共享的工具函数**

[crates/gpui_macros/src/gpui_macros.rs#L303-L313](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L303-L313) —— 入口文件末尾还有一个 `pub(crate)` 工具函数 `get_simple_attribute_field`：在结构体字段里找到带指定属性（如 `#[app]`）的字段并返回字段名，找不到或不是结构体就返回 `None`。它被 `AppContext`、`VisualContext` 等多个派生宏共用——放在入口文件里正好体现「公共辅助下沉、各自逻辑归各自模块」。

#### 4.2.4 代码实践

**实践二：画出「宏入口 → 实现文件」对应关系图**

1. **实践目标**：亲手整理一份索引表，之后阅读任何宏的实现时都能秒定位文件。
2. **操作步骤**：
   - 打开 [crates/gpui_macros/src/gpui_macros.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs)，从头到尾扫一遍所有 `#[proc_macro*]` 标注。
   - 为每个宏记录：宏名与调用形式、类别（派生/属性/函数式）、入口函数所在行号、转发的目标模块。
   - 整理成下表（可直接抄用，行号已按当前 HEAD 核对）：

   | 宏（调用形式） | 类别 | 入口行号 | 实现文件 |
   | --- | --- | --- | --- |
   | `#[derive(Action)]` | 派生 | L19-L22 | `src/derive_action.rs` |
   | `register_action!(...)` | 函数式 | L27-L30 | `src/register_action.rs` |
   | `#[derive(IntoElement)]` | 派生 | L34-L37 | `src/derive_into_element.rs` |
   | `#[derive(Render)]`（doc hidden） | 派生 | L39-L43 | `src/derive_render.rs` |
   | `#[derive(AppContext)]` | 派生 | L58-L61 | `src/derive_app_context.rs` |
   | `#[derive(VisualContext)]` | 派生 | L91-L94 | `src/derive_visual_context.rs` |
   | `style_helpers!`（doc hidden）及 8 个 `*_style_methods!` | 函数式 | L96-L149 | `src/styles.rs` |
   | `#[gpui::test]` | 属性 | L188-L191 | `src/test.rs` |
   | `#[gpui::bench]` | 属性 | L203-L206 | `src/bench.rs` |
   | `#[gpui::property_test]` | 属性 | L276-L279 | `src/property_test.rs` |
   | `#[derive_inspector_reflection]`（条件编译） | 属性 | L297-L301 | `src/derive_inspector_reflection.rs` |

3. **需要观察的现象**：19 个宏入口，只对应 11 个实现模块——`styles.rs` 一个模块承接了 9 个入口。
4. **预期结果**：表格如上，共 19 个入口（18 个无条件编译 + 1 个条件编译）。可以用 `grep -n "^#\[proc_macro" crates/gpui_macros/src/gpui_macros.rs` 快速自检，应输出 19 处标注。

#### 4.2.5 小练习与答案

**练习 1**：`#[proc_macro_derive(Action, attributes(action))]` 里的 `attributes(action)` 起什么作用？

**答案**：它向编译器注册 `#[action(...)]` 这个辅助属性。没有这一步，用户在结构体字段上写 `#[action(...)]` 会得到「找不到该属性」的编译错误，派生宏内部也无法读到它。类似地 `AppContext` 注册了 `attributes(app)`，`VisualContext` 注册了 `attributes(window, app)`。

**练习 2**：为什么 `#[derive(Render)]` 和 `style_helpers!` 要标 `#[doc(hidden)]`？

**答案**：`#[doc(hidden)]` 让该项不出现在 rustdoc 文档中。这两个宏是给 `gpui` 内部（或代码生成链）使用的，不是给最终用户的公共 API，隐藏文档可以避免使用者误用、保持公共文档整洁。

**练习 3**：如果在一个 release 构建（没有开 `inspector` feature）的代码里直接调用 `#[derive_inspector_reflection]`，会发生什么？

**答案**：编译错误「找不到宏」。因为该宏的入口函数被 `#[cfg(any(feature = "inspector", debug_assertions))]` 排除在 release 构建之外，宏根本不存在。上游因此用 `#[cfg_attr(...)]` 条件属性来按编译配置决定是否应用它。

### 4.3 构建与测试命令

#### 4.3.1 概念说明

proc-macro crate 的构建产物是**动态库**，在编译其他 crate 时被 rustc 加载执行。所以构建 `gpui_macros` 时你会注意到它编译得飞快——它总共不到两千行代码，且只依赖 4 个小库。

它的测试由三部分组成：

1. **集成测试**：`tests/` 目录下的三个文件。
2. **文档测试（doctest）**：入口文件里宏的文档注释中包含可运行的示例（包括故意失败的 `compile_fail` 示例），`doctest = true` 让它们也被执行。
3. **编译通过即测试**：宏 crate 的特殊性在于——很多验证隐含在「生成的代码能否通过编译」里，集成测试里每一个使用了派生宏的结构体都是一次验证。

#### 4.3.2 核心流程

```text
cargo test -p gpui_macros
    │
    ├─ 编译 gpui_macros（proc-macro 动态库）
    ├─ 编译 tests/ 下三个集成测试（此时宏被真实调用、展开）
    │      ├─ tests/render_test.rs        → test_derive_render
    │      ├─ tests/derive_context.rs     → test_derive_context
    │      └─ tests/derive_inspector_reflection.rs → test_derive_inspector_reflection
    ├─ 运行三个测试函数
    └─ 收集并运行 doctest（doctest = true 的效果）
```

#### 4.3.3 源码精读

[crates/gpui_macros/Cargo.toml#L18](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/Cargo.toml#L18) —— `doctest = true`：显式开启文档测试。入口文件中 `#[gpui::test]`、`#[gpui::property_test]` 等宏的文档注释里都有 ```` ``` ```` 包裹的示例代码，它们会被编译运行；`AppContext`/`VisualContext` 文档里的 `compile_fail` 示例（见 [crates/gpui_macros/src/gpui_macros.rs#L50-L57](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L50-L57)）则反过来验证「缺属性时确实报错」。

三个集成测试文件（函数名经源码核对）：

| 文件 | 测试函数 | 验证什么 |
| --- | --- | --- |
| `tests/render_test.rs` | `#[test] fn test_derive_render()` | `#[derive(Render)]` 生成的默认渲染可用 |
| `tests/derive_context.rs` | `#[test] fn test_derive_context()` | `AppContext`/`VisualContext` 派生生成的上下文委托可用 |
| `tests/derive_inspector_reflection.rs` | `#[test] fn test_derive_inspector_reflection()` | 反射宏生成的方法枚举/查找可用 |

#### 4.3.4 代码实践

**实践三：构建、测试并记录测试列表**

1. **实践目标**：跑通本 crate 的构建与测试，拿到一份真实的测试清单，验证上文对测试来源的描述。
2. **操作步骤**：
   - 在 Zed 仓库根目录执行：
     ```bash
     cargo build -p gpui_macros
     cargo test -p gpui_macros
     ```
   - 观察输出中「Running tests」与「Doc-tests」两个阶段。
   - 把输出里的测试名逐条抄录下来，与 4.3.3 的表格对照。
3. **需要观察的现象**：
   - `cargo build` 很快完成，产物是一个动态库而非普通 `.rlib`。
   - `cargo test` 先后出现 `tests/render_test`、`tests/derive_context`、`tests/derive_inspector_reflection` 三个测试目标，各含 1 个测试；随后是 Doc-tests 阶段。
4. **预期结果**：三个集成测试共 3 个测试函数全部通过；Doc-tests 阶段包含入口文件文档注释中的示例（具体数量取决于文档示例个数，待本地验证）。
5. 说明：本讲义没有替你执行过这些命令，以上「预期结果」中凡依赖实际运行的细节均标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`cargo test -p gpui_macros` 里的 `-p` 是什么意思？为什么必须在 Zed 仓库根目录（或其任意子目录）执行？

**答案**：`-p gpui_macros`（`--package`）限定只构建并测试 workspace 中名为 `gpui_macros` 的这一个包。它必须在 workspace 内执行，因为命令需要找到根 `Cargo.toml` 来解析 workspace 成员与统一的依赖版本。

**练习 2**：删除 `Cargo.toml` 中的 `doctest = true` 会损失什么测试？

**答案**：会损失文档注释里的所有示例测试。对宏 crate 来说这尤其可惜：`#[gpui::test]` 的用法示例、`compile_fail` 的负例验证都在文档里，doctest 是它们唯一的执行途径。（注：Cargo 默认对库 crate 开启 doctest，此处显式声明更像是明确意图。）

**练习 3**：为什么宏 crate 的集成测试「只要编译通过就已经完成了一半验证」？

**答案**：集成测试文件里每个使用 `#[derive(...)]` 的结构体、每个 `#[gpui::test]` 函数，都会在测试二进制编译阶段真实触发宏展开。如果宏生成了语法错误或类型不匹配的代码，测试根本编译不过。所以宏 crate 的测试 = 编译期验证（宏生成的代码合法且类型正确）+ 运行期断言（生成的代码行为正确）。

## 5. 综合实践

**任务：制作一份「宏 → 入口 → 实现 → gpui 使用点」四列索引**

把前面三个小实践串起来：

1. 执行 `cargo build -p gpui_macros && cargo test -p gpui_macros`，确认环境可用，抄录测试列表（见 4.3.4）。
2. 依据 4.2.4 的表格，为每一行补充第四列「gpui 侧使用点」。已核实的两个起点：
   - 再导出：[crates/gpui/src/gpui.rs#L109-L111](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L109-L111)
   - 样式宏调用：[crates/gpui/src/styled.rs#L26-L34](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs#L26-L34)
3. 对还没找到使用点的宏（如 `Action`、`register_action`），用 `grep` 在 `crates/gpui/src/` 下搜索宏名定位，例如：
   ```bash
   grep -rn "actions!" crates/gpui/src/action.rs | head
   ```
4. 最终产出一张四列表格，并在表下用三五行总结：哪些宏是面向最终用户的（被再导出），哪些是 gpui 内部基础设施（doc hidden / 仅内部调用）。

这张表就是你后续学习整本手册的「地图」，第二单元起的每一讲都会用到它。

## 6. 本讲小结

- `gpui_macros` 是 GPUI 框架专属的 proc-macro crate，编译成动态库供 rustc 在编译期调用，自身不参与运行时。
- `[lib]` 中 `proc-macro = true` 决定了它的 crate 类型；`path = "src/gpui_macros.rs"` 是 Zed 仓库「入口文件与 crate 同名」约定的体现。
- 依赖只有 `syn`（解析）、`quote`（生成）、`proc-macro2`（基础设施）、`heck`（命名转换）四个；`gpui` 作为 dev-dependency 反向出现在测试里，不构成循环依赖。
- 入口文件 `gpui_macros.rs` 是「薄入口 + 厚实现」：19 个宏入口（11 个实现模块），其中 9 个样式宏共享 `styles.rs`，反射宏受 `inspector`/`debug_assertions` 双条件编译控制。
- `gpui` 通过 feature 转发（`inspector = ["gpui_macros/inspector"]`）和再导出（`gpui.rs` L109-L111）把宏能力交给业务代码。
- 测试 = 3 个集成测试（每文件 1 个测试函数）+ doctest；`cargo build/test -p gpui_macros` 即可验证。

## 7. 下一步学习建议

- 下一篇 **u1-l2《过程宏基础：syn、quote 与三类宏入口》**：如果你还不熟悉 `syn`/`quote` 的配合方式，强烈建议先学它，再进入任何宏实现文件的精读；它会教你用 `cargo expand` 亲眼看到宏展开结果。
- 之后 **u1-l3《宏清单速览》** 会把本讲第 5 节的四列索引补完整，特别是 `Action` 派生与 `actions!` 声明宏的关系。
- 直接阅读建议：把 [crates/gpui_macros/src/gpui_macros.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs) 完整读一遍——它只有 313 行，是整个 crate 唯一的「目录页」，读完它你对这个 crate 的认知就立体了。
