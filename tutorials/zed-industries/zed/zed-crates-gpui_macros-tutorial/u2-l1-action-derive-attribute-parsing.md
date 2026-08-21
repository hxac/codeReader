# #[derive(Action)]（上）：#[action(...)] 属性解析

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `derive_action.rs` 中「属性解析阶段」的每一行代码：如何遍历 `input.attrs`、如何用 `attr.parse_nested_meta` 逐个消费 `#[action(...)]` 里的参数。
- 准确说出 `name`、`namespace`、`no_json`、`no_register`、`deprecated_aliases`、`deprecated` 六个参数各自的写法、解析成的类型、语义与「重复指定即报错」的校验规则。
- 理解 `///` 文档注释如何被当作 `#[doc = "..."]` 属性提取出来，最终变成 `Action::documentation()` 的返回值。
- 理解 action 名称的拼接规则（`namespace::Name`）以及为什么 `name` 中禁止出现 `::`。

本讲只讲「解析」，即从 `#[derive(Action)]` 标注的 TokenStream 中提取出所有配置信息；这些信息如何被用来生成 `impl gpui::Action` 的九个方法，属于下一讲 u2-l2 的内容。

## 2. 前置知识

在开始前，请回顾前两讲已建立的概念（本讲直接使用，不再重复解释）：

- **派生宏与 helper 属性**（u1-l2）：`#[proc_macro_derive(Action, attributes(action))]` 中的 `attributes(action)` 把 `#[action]` 注册为该派生宏的专属 helper 属性，否则用户写 `#[action(...)]` 会直接编译报错。
- **TokenStream 输入输出**（u1-l2）：派生宏拿到的输入是整个条目（struct 定义 + 它身上的所有属性）的 TokenStream，用 `parse_macro_input!` 解析成 `syn::DeriveInput`。
- **两层架构**（u1-l3）：`actions!` 声明宏展开出带 `#[derive(gpui::Action)]` 的单元结构体，也就是说 `actions!` 的所有能力最终都落到本讲分析的 `#[action(...)]` 参数上。

本讲新需要的基础概念：

- **Action 是什么**：GPUI 用「action」实现键盘驱动的 UI。用户在 keymap JSON 里写 `"bindings": { "ctrl-t": "workspace::NewSearch" }`，框架按 *action 名称* 找到对应的类型，把 JSON 参数构造成该类型的实例，再分发给元素树上注册的监听器。因为名称是跨 crate 的全局键，所以「名称怎么拼出来」是件严肃的事——这正是本讲 4.3 节的主题。
- **syn 眼中的属性**：rustc 传给宏的每条属性最终都是 `syn::Attribute`，其 `meta` 字段是三种形态之一：`Meta::Path`（`#[foo]`）、`Meta::List`（`#[foo(...)]`）、`Meta::NameValue`（`#[foo = "bar"]`）。`#[action(namespace = editor, no_json)]` 属于 `Meta::List`，括号里那些逗号分隔的小项称为「嵌套 meta」。
- **doc 注释的本质**：`/// 文字` 不是注释，而是会被 rustc 改写成 `#[doc = " 文字"]` 属性挂在条目上。宏可以像处理普通属性一样处理它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/gpui_macros/src/gpui_macros.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L18-L22) | 派生宏入口：声明 `#[proc_macro_derive(Action, attributes(action))]` 并转发到实现模块 |
| [crates/gpui_macros/src/derive_action.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L7-L117) | 本讲主角。L7-L117 是解析与校验阶段，L119 之后是代码生成阶段（下一讲） |
| [crates/gpui/src/action.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L42-L173) | `Action` trait 定义、六个参数的官方文档（L72-L90）、`actions!` 声明宏（L24-L40） |
| [crates/workspace/src/workspace.rs](https://github.com/zed-industries-zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/workspace/src/workspace.rs#L478-L487) | 真实用例：`namespace + name + 带字段结构体` 三者组合 |
| [crates/vim/src/command.rs](https://github.com/zed-industries-zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/vim/src/command.rs#L56-L61) | 真实用例：`no_json + no_register` 组合 |
| [crates/vim/src/motion.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/vim/src/motion.rs#L342-L351) | 真实用例：`actions!` 内嵌 `#[action(deprecated_aliases = ...)]` 与 doc 注释共存 |

## 4. 核心概念与源码讲解

### 4.1 syn 属性解析 API：遍历 attrs 与 parse_nested_meta

#### 4.1.1 概念说明

一个被 `#[derive(Action)]` 标注的结构体身上可能同时挂着很多属性：`#[derive(...)]`、`#[serde(...)]`、`#[action(...)]`、若干 `#[doc = "..."]`。派生宏拿到的是**整个条目**，所以第一步不是「解析 #[action]」，而是**遍历所有属性，把属于自己的一部分挑出来**。

对 `#[action(namespace = editor, no_json)]` 这种 `Meta::List` 形态，syn 提供了 `parse_nested_meta` 方法：它把括号里的内容按逗号拆成一项一项，每项交给你的闭包处理。闭包签名中的 `meta` 提供两个关键接口：

- `meta.path`：当前项的名字（如 `namespace`、`no_json`）。
- `meta.input`：当前项名字**之后**剩余的 TokenStream。注意 `= editor` 并不会被自动帮你解析成值——等于号和值都需要你在闭包里手动 `parse`。

闭包返回 `Err(meta.error(...))` 会立刻中止整个解析，错误自带精确的源码 span。

#### 4.1.2 核心流程

`derive_action` 的整体骨架可以概括为：

```text
输入: DeriveInput
1. 初始化 7 个可变状态: name/namespace/no_json/no_register/
   deprecated_aliases/deprecated/doc_str
2. 遍历 input.attrs:
   a. path == "action" → parse_nested_meta 逐参数分派, 填充状态
   b. path == "doc"    → 模式匹配出字符串字面量, 追加到 doc_str
3. name 缺省时取结构体名
4. 校验 name 不含 "::"
5. 拼接 full_name = "namespace::name" 或 "name"
6. (下一讲) 用这些状态生成 impl gpui::Action
```

注意第 2 步的设计：解析结果不是某个自定义结构体，而是**一组散落的局部变量**。这是小型宏的常见做法——状态就几种，直接用变量比定义 `struct ActionAttrs` 更轻。

#### 4.1.3 源码精读

先看入口。helper 属性 `action` 在这里注册，实现转发到 `derive_action` 模块：

[crates/gpui_macros/src/gpui_macros.rs:L18-L22](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L18-L22)：声明 `#[proc_macro_derive(Action, attributes(action))]`，没有 `attributes(action)` 这一段，用户结构体上的 `#[action(...)]` 会被 rustc 当成未知属性直接拒绝——这是派生宏「申领」helper 属性的机制。

再看解析函数开头：七个状态变量的初始化，以及外层 `for attr in &input.attrs` 循环：

[crates/gpui_macros/src/derive_action.rs:L7-L28](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L7-L28)：`parse_macro_input!` 把输入解析为 `DeriveInput`；L26 开始遍历属性；L27 用 `attr.path().is_ident("action")` 判断这条属性是否属于本宏；L28 进入 `parse_nested_meta`，闭包将对括号内的每个参数各被调用一次。

以 `name` 参数为例看「手动消费 `=` 和值」的套路：

[crates/gpui_macros/src/derive_action.rs:L29-L35](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L29-L35)：先查重（`name_argument.is_some()` 则报错）；再 `meta.input.parse::<Token![=]>()?` 显式消费等号；然后 `parse::<LitStr>()` 解析出一个**字符串字面量**（所以 `name = CustomName` 不带引号是无法编译的）；最后 `lit.value()` 拿到 `String` 存入状态。这一段是所有「带值参数」的模板，`namespace`、`deprecated` 与之同构，只是值类型不同。

`doc` 属性的提取走的是另一条路——不用 `parse_nested_meta`，而是直接对 `attr.meta` 做模式匹配：

[crates/gpui_macros/src/derive_action.rs:L85-L101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L85-L101)：匹配 `Meta::NameValue` 且值是字符串字面量（也就是 `#[doc = " 文字"]` 的形态）；`doc.trim()` 去掉 doc 注释的固定前导空格；`push('\n')` 按行拼接；多行 doc 注释会多次命中这个分支，逐行累积。`doc_str.get_or_insert_default()` 是惰性初始化的惯用写法——没有 doc 注释时就保持 `None`，生成的 `documentation()` 返回 `None`。

顺带一提，源码 L19-L25 有一段注释在自问自答「结构体字段 `bar` 算不算 action 的属性」——答案是不算，`parse_nested_meta` 只看 `#[action(...)]` 括号里的内容，与结构体字段无关。这是阅读原始源码时能看到的设计笔记。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `#[action(...)]` 与 doc 注释在宏展开后「消失」，变成了生成代码里的常量。
2. **操作步骤**：
   - 前置：已按 u1-l2 安装 `cargo expand`（需要 nightly 工具链）。
   - 在 zed 仓库根目录执行 `cargo expand -p vim normal 2>/dev/null | less`，定位到 `GoToLine`（本讲 4.2 将用到的真实 action）。
3. **需要观察的现象**：展开结果中 `#[action(namespace = vim, no_json, no_register)]` 与 `/// Goes to the specified line...` 都不见了，取而代之的是 `impl gpui::Action for GoToLine { ... }`，其中 `documentation()` 方法体内嵌着那行英文，`name()` 返回 `"vim::GoToLine"`。
4. **预期结果**：确认「属性与 doc 注释是宏的输入、生成的方法是输出」这条数据流。若本机无 nightly，此步可跳过，改由 4.2 的编译实验覆盖。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`parse_nested_meta` 的闭包里，为什么必须写 `meta.input.parse::<Token![=]>()?`？不写会怎样？

**答案**：`meta.input` 是参数名之后的所有剩余 token。不消费等号，下一个 `parse::<LitStr>()` 会从 `= "CustomName"` 的等号开始解析，直接报「expected string literal」类错误。`parse_nested_meta` 只负责按逗号切分各项，不负责解析项内的 `键 = 值` 结构。

**练习 2**：一段两行的 doc 注释（`/// 第一行` / `/// 第二行`）最终会变成什么样的字符串？

**答案**：每行命中一次 L96-L99 的提取分支，得到 `"第一行\n第二行\n"`；生成代码前 L150 还有一次整体 `doc.trim()`，去掉首尾空白，最终 `documentation()` 返回 `Some("第一行\n第二行")`——中间的 `\n` 保留，仅首尾被清理。

**练习 3**：为什么 `#[action]` 分支用 `parse_nested_meta`，而 `#[doc]` 分支用模式匹配？

**答案**：`#[action(...)]` 是 `Meta::List`，内容是同构的逗号分隔参数表，适合逐项回调；`#[doc = "..."]` 是 `Meta::NameValue`，整条属性就是一个键值对，直接 destructure 即可，没有「多项」需要遍历。

### 4.2 #[action] 六个参数的语义与校验规则

#### 4.2.1 概念说明

六个参数就是 action 系统暴露给业务代码的全部「配置面」。它们的完整官方文档在 [crates/gpui/src/action.rs:L72-L90](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L72-L90)（trait 文档的一部分），解析实现则在 `derive_action.rs`。先给总表：

| 参数 | 写法 | 解析成的类型 | 语义 | 影响的生成方法 |
| --- | --- | --- | --- | --- |
| `name` | `name = "CustomName"` | `LitStr`（字符串字面量） | 覆盖默认的「结构体名」作为 action 名 | `name` / `name_for_type` |
| `namespace` | `namespace = editor`（裸标识符） | `Ident` | 命名空间，最终名拼为 `namespace::Name` | 同上 |
| `no_json` | `no_json`（标志，无值） | 无值，置 bool | 该 action 不支持从 JSON 构造，免实现 `Deserialize`/`JsonSchema` | `build` / `action_json_schema` |
| `no_register` | `no_register`（标志） | 无值，置 bool | 不注册进全局 action 表（不能按名调用） | 是否生成注册代码 |
| `deprecated_aliases` | `deprecated_aliases = ["a::B", "c::D"]` | 方括号内 `LitStr` 逗号列表 | 旧名称仍可触发本 action | `deprecated_aliases` |
| `deprecated` | `deprecated = "弃用说明"` | `LitStr` | 弃用提示，keymap 校验时展示为警告 | `deprecation_message` |

最后一列涉及生成阶段，下一讲才逐行拆解，这里只需建立「每个参数都在生成代码里有明确落点」的直觉。

#### 4.2.2 核心流程

每个参数的解析都遵循同一个模板：

```text
if meta.path 是某参数名:
    若已出现过 → Err(meta.error("specified multiple times"))   // 查重
    消费 '=' (仅带值参数)
    解析出值 (LitStr / Ident / 方括号列表 / 无)
    写入对应状态变量
else:
    Err(meta.error("argument not recognized, expected ..."))    // 兜底
```

校验分三层：

1. **查重**：六个参数都写了重复检测，报错文案统一是 `'xxx' argument specified multiple times`。
2. **值类型**：`name`/`deprecated` 必须是字符串字面量；`namespace` 必须是裸标识符；`deprecated_aliases` 必须是方括号包裹的字符串列表。
3. **兜底**：任何未识别的参数名进入 L75-L81 的 `else` 分支报错——这是参数名拼写错误（如 `namespace` 打成 `namespce`）的第一现场。

#### 4.2.3 源码精读

**namespace 分支**——值是 `Ident` 而非 `LitStr`，这是它与 `name` 的关键差异：

[crates/gpui_macros/src/derive_action.rs:L36-L42](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L36-L42)：`parse::<Ident>()` 决定了 `namespace = editor` 合法而 `namespace = "editor"` 编译失败（字符串字面量不是标识符）。这个设计与 `actions!` 宏对接：[crates/gpui/src/action.rs:L24-L32](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L24-L32) 中 `$namespace:path` 匹配到的就是一个裸路径，展开后原样填进 `#[action(namespace = $namespace)]`。注意解析目标只是单个 `Ident`，写成多段路径（`namespace = a::b`）会因剩余的 `::b` 无法被 `parse_nested_meta` 接受而报错；实践中命名空间都是单个标识符。

**no_json / no_register 分支**——「标志型」参数最简形态：

[crates/gpui_macros/src/derive_action.rs:L43-L52](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L43-L52)：没有等号没有值，命中即置 `true`，查重防 `no_json, no_json`。它们对生成阶段的影响：`no_json` 让 [L121-L134](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L121-L134) 的 `build` 直接返回错误、`action_json_schema` 返回 `None`；`no_register` 让 [L156-L160](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L156-L160) 跳过 `generate_register_action` 调用。

**deprecated_aliases 分支**——唯一的列表型参数，需要手工拆方括号：

[crates/gpui_macros/src/derive_action.rs:L53-L67](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L53-L67)：`syn::bracketed!` 宏消费 `[` `]` 并交出内部 token 流；`parse_terminated(LitStr, Token![,])` 解析逗号分隔的字符串字面量序列（尾逗号合法）；`extend` 而非赋值——但查重保证该分支只会命中一次。真实用例见 [crates/vim/src/motion.rs:L345](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/vim/src/motion.rs#L342-L351)（`WrappingLeft` 保留旧名 `vim::Backspace`）和 [crates/zed_actions/src/lib.rs:L284-L293](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/zed_actions/src/lib.rs#L284-L293)（`actions!` 的 `$(#[$attr:meta])*` 透传机制允许在列表内逐个附加 `#[action(...)]`）。

**deprecated 分支与未识别兜底**：

[crates/gpui_macros/src/derive_action.rs:L68-L84](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L68-L84)：`deprecated` 与 `name` 完全同构（L72-L74）。L75-L81 是兜底分支，`format!` 把未识别的路径嵌进报错。值得留意两个阅读源码时发现的细节：报错文案列举合法参数时**漏掉了 `name`**（L77-L78 的 expected 列表里只有五个），且 `'no_register` 后少了一个闭合引号——不影响功能，但如果你在报错信息里看到这份文案，不要被它误导以为 `name` 不存在。`deprecated` 参数目前在整个 zed 工作区内只有实现与文档、没有真实调用（已用 grep 验证），属于「能力已就绪、暂未使用」的状态。

**错误如何呈现给用户**——`panic!` 路径：

[crates/gpui_macros/src/derive_action.rs:L83-L84](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L83-L84)：`parse_nested_meta` 返回 `syn::Result`，代码用 `.unwrap_or_else(|e| panic!("in #[action] attribute: {}", e))` 收口。过程宏在编译期运行，其中的 `panic!` 会被 rustc 捕获并显示为编译错误（信息形如 `proc-macro panicked` + 引号内的消息），所以这是安全的——不会让编译器崩溃，只是错误定位会退化到整个 `#[derive(Action)]` 调用处，丢失 `meta.error()` 原本携带的精确 span。对比：`derive_app_context.rs` 用的是 `compile_error!` 输出（u3-l1 会讲），两种方案各有取舍。

**组合参数的真实范例**：

[crates/workspace/src/workspace.rs:L478-L487](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/workspace/src/workspace.rs#L478-L487)：`ToggleFileFinder` 同时使用 `namespace = file_finder` 与 `name = "Toggle"`（最终名 `file_finder::Toggle`），且是带字段结构体——doc 注释、serde 字段属性、action 属性在同一结构体上和平共处，因为解析阶段只挑 `action` 和 `doc` 两种属性，其余（如 `#[serde(deny_unknown_fields)]`）被循环直接跳过。

[crates/vim/src/command.rs:L56-L61](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/vim/src/command.rs#L56-L61)：`GoToLine` 使用 `namespace = vim, no_json, no_register`——vim 内部自己构造这个 action，不需要 keymap 按名引用，所以既不注册也不支持 JSON，同时省去了为私有字段 `CommandRange` 实现 `Deserialize`/`JsonSchema` 的负担。这演示了标志参数的典型动机。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：亲手验证六个参数中核心几个的解析结果与报错行为。

2. **操作步骤**：

   在 zed 仓库**之外**创建一个临时 crate（避免改动仓库）：

   ```bash
   cargo new --lib action-derive-lab
   cd action-derive-lab
   ```

   编辑 `Cargo.toml`（`path` 指向你本地的 zed 检出；gpui 的 `test` 模块无条件编译，无需额外 feature，见 [crates/gpui/src/gpui.rs:L60](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L60)）：

   ```toml
   [dependencies]
   gpui = { path = "/path/to/zed/crates/gpui" }
   serde = { version = "1", features = ["derive"] }
   schemars = "0.8"   # 版本需与 zed 工作区一致，见下方说明
   ```

   > 说明：带字段且支持 JSON 的 action 必须实现 `serde::Deserialize` 与 `schemars::JsonSchema`（官方要求见 [crates/gpui/src/action.rs:L68-L70](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L68-L70)），而派生宏生成的代码通过 `gpui::private::schemars` 引用 schemars，所以这里的版本要与 zed 的 `Cargo.lock` 一致（可在 zed 仓库根目录查 `Cargo.lock` 中 `schemars` 的版本）。若不想对齐版本，把下面 `SaveAll` 的 `serde::Deserialize, schemars::JsonSchema` 两个 derive 去掉并给 `#[action(...)]` 加上 `no_json` 即可，断言部分不受影响。

   在 `src/lib.rs` 写入（示例代码）：

   ```rust
   use gpui::Action;

   /// Demonstrates namespace plus name override.
   #[derive(Default, Clone, Copy, PartialEq, Debug, Action)]
   #[action(namespace = editor, name = "CustomName")]
   pub struct Ping;

   /// A fieldful action.
   /// Second doc line.
   #[derive(Clone, PartialEq, serde::Deserialize, schemars::JsonSchema, Action)]
   #[action(namespace = tutorial, no_register)]
   pub struct SaveAll {
       #[serde(default)]
       pub confirm: bool,
   }

   #[gpui::test]
   fn action_names(_cx: &mut gpui::TestAppContext) {
       assert_eq!(Ping::name_for_type(), "editor::CustomName");
       assert_eq!(SaveAll::name_for_type(), "tutorial::SaveAll");
       assert_eq!(
           SaveAll::documentation(),
           Some("A fieldful action.\nSecond doc line.")
       );
       assert_eq!(SaveAll::deprecated_aliases(), &[]);
   }

   #[test]
   fn no_context_needed() {
       // name_for_type 是静态关联函数，普通 #[test] 也能用
       assert_eq!(Ping::name_for_type(), "editor::CustomName");
   }
   ```

   然后 `cargo test`。

3. **需要观察的现象**：

   - 三个断言全部通过：`namespace + name` 拼出 `editor::CustomName`；缺省 `name` 落到结构体名 `tutorial::SaveAll`；两行 doc 注释合并为 `\n` 分隔的单个 `&str`（无首尾空白）。
   - 接着做**负向实验**：把 `Ping` 的属性改成 `#[action(namespace = editor, nam = "X")]` 后执行 `cargo build`，观察报错。预期错误消息包含 `in #[action] attribute: 'nam' argument not recognized, expected 'namespace', 'no_json', 'no_register, 'deprecated_aliases', or 'deprecated'`——它来自 [derive_action.rs:L76-L84](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L75-L84)，且以 proc-macro panic 的形式呈现。
   - 再试 `#[action(namespace = "editor")]`（字符串），预期在 [L41](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L40-L42) 报「expected identifier」类错误。待本地验证（确切措辞依 syn 版本而定）。

4. **预期结果**：正向断言全绿；负向实验各报一条编译错误且能指认其来源行。注意**不要在同一测试二进制里定义两个同名 action**——每个派生的 action 默认都会注册（`no_register` 除外），重名会在 `App` 创建时 panic（官方文档 [crates/gpui/src/action.rs:L53](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L53) 有明确警告）。

#### 4.2.5 小练习与答案

**练习 1**：`#[action(name = "A", name = "B")]` 会发生什么？

**答案**：`parse_nested_meta` 对每个参数各调用一次闭包，第二次进入 `name` 分支时 `name_argument.is_some()` 成立，返回 [L31](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L29-L32) 的 `Err(meta.error("'name' argument specified multiple times"))`，最终成为编译错误。

**练习 2**：`deprecated_aliases = ["a::B"]` 中的旧名 `a::B` 需要对应一个真实注册的 action 吗？

**答案**：不需要，恰恰相反——官方文档（[crates/gpui/src/action.rs:L84-L88](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L84-L88)）要求这些旧名**不**对应任何已注册 action，它们只是把旧键映射到新 action 上，供旧 keymap 继续使用并给出迁移警告。

**练习 3**：如果想让一个含私有字段、无法实现 `Deserialize` 的结构体当 action 用，最小配置是什么？

**答案**：`#[derive(Clone, PartialEq, Action)]` 加 `#[action(namespace = xxx, no_json)]`，参考 [crates/vim/src/command.rs:L56-L61](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/vim/src/command.rs#L56-L61) 的 `GoToLine`。`no_json` 让 `build` 恒返回错误、schema 为 `None`，`Deserialize`/`JsonSchema` 两个约束随之解除；若它也不需要被 keymap 按名引用，再加 `no_register`。

### 4.3 名称拼接与 `::` 禁止规则

#### 4.3.1 概念说明

action 名称是 keymap JSON 里的全局键：`"workspace::NewSearch"` 里的 `::` 是命名空间与动作名的分隔符。既然 `namespace` 参数已经承担了「命名空间」职责，`name` 里就不允许再出现 `::`——否则用户可以用 `name = "admin::DeleteEverything"` 伪造别人的命名空间，两个参数的职责边界也就形同虚设。

「名称」在生成代码里出现两次：`name()`（实例方法，用于 UI 展示）与 `name_for_type()`（静态方法，无实例时用，比如注册阶段）。两者返回同一个 `&'static str` 常量，其值就是本节拼出的 `full_name`。

#### 4.3.2 核心流程

```text
name       = name 参数 或 结构体名          # L104
校验: name 含 "::" → panic（引导改用 namespace）  # L106-L111
full_name  = "{namespace}::{name}" 或 name    # L113-L117
```

拼接用 `format!` 在**宏展开时（编译期）**完成，`full_name` 是宏进程里的一个普通 `String`，通过 `quote!` 的 `#full_name` 插值固化进生成代码——所以运行时调用 `name()` 只是返回一个早已存在的字符串字面量，零开销。

#### 4.3.3 源码精读

[crates/gpui_macros/src/derive_action.rs:L104-L117](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L104-L117)：L104 缺省回退到 `struct_name.to_string()`——这解释了为什么大多数 action 不写 `name`。L106-L111 是 `::` 禁止规则，报错文案直接给出修复建议 `also specify namespace instead`，是「报错即文档」的好例子。L113-L117 用 `if let Some(namespace)` 拼接最终名。紧随其后的 [L119](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L119) 判定 `is_unit_struct`，它不影响名称，但与 `no_json` 一起决定 `build` 的生成分支——那是下一讲的起点。

对照消费侧：`name_for_type` 的 trait 声明在 [crates/gpui/src/action.rs:L127-L130](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L127-L130)。而运行期按名查表构造 action 的入口是 [crates/gpui/src/action.rs:L351-L369](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L351-L369) 的 `build_action`：从注册表 `by_name` 里取出构建函数，传入 keymap 里的 JSON 参数。名称在编译期拼好、注册期登记、运行期查表——三个阶段由此串成一条链。

#### 4.3.4 代码实践

1. **实践目标**：验证 `::` 禁止规则与缺省名称回退。
2. **操作步骤**：继续使用 4.2 的 lab crate。先给 `Ping` 加一个别名 `#[action(namespace = editor, name = "a::b")]`，执行 `cargo build`；确认报错后还原，再新建一个不带 `name` 的 `actions!` 声明：

   ```rust
   gpui::actions!(my_ns, [DoThing]);
   ```

3. **需要观察的现象**：

   - `name = "a::b"` 触发编译错误，消息为 `` `name = "a::b"` must not contain `::`, also specify `namespace` instead ``，来源 [derive_action.rs:L106-L111](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L106-L111)。
   - `actions!` 版本展开后（可用 `cargo expand`）能看到 `#[action(namespace = my_ns)]`，`DoThing::name_for_type()` 返回 `"my_ns::DoThing"`——结构体名回退 + namespace 拼接的完整链路。
4. **预期结果**：两条均符合上述描述。`actions!` 的展开细节属于 u2-l3，此处只需确认名称值正确。

#### 4.3.5 小练习与答案

**练习 1**：`#[action(name = "editor::Foo")]`（不带 namespace）能通过编译吗？生成的 `name()` 会是 `"editor::Foo"` 吗？

**答案**：不能。`::` 校验发生在拼接**之前**（L106 在 L113 之前），无论是否提供 namespace，只要 `name` 含 `::` 就直接 panic，所以「借 name 伪造命名空间」这条路被彻底堵死。

**练习 2**：`actions!(editor, [MoveUp])` 生成的名称是什么？依赖了本讲的哪些逻辑？

**答案**：`"editor::MoveUp"`。`actions!` 展开出 `#[action(namespace = editor)]`（[crates/gpui/src/action.rs:L25-L31](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L25-L31)），`name` 缺省回退到结构体名 `MoveUp`（L104），再拼接成 `editor::MoveUp`（L113-L117）。

**练习 3**：`full_name` 是宏进程里的 `String`，它如何进入最终二进制？

**答案**：通过 `quote! { #full_name }` 插值——`quote` 会把 `String` 转成一个字符串字面量 token 写进生成代码（见 L162-L175 的 `#full_name`）。因此运行期的 `name()`/`name_for_type()` 返回的是编译期固化的 `&'static str`，不存在运行时拼接。

## 5. 综合实践

**任务：制作一张「参数 → 生成物」对照表。**

在 4.2 的 lab crate 中定义四个 action，把六个参数全部覆盖到（可组合）：

1. 一个 `actions!` 声明的、带 doc 注释与 `deprecated_aliases` 的单元 action（模仿 [crates/vim/src/motion.rs:L342-L351](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/vim/src/motion.rs#L342-L351) 的写法）；
2. 一个 `namespace + name` 组合的单元 action（模仿 `Ping`）；
3. 一个 `namespace + no_json + no_register` 的带字段 action（模仿 [crates/vim/src/command.rs:L56-L61](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/vim/src/command.rs#L56-L61)）；
4. 一个带 `deprecated = "..."` 与 doc 注释的带字段 action。

为每个 action 写断言：`name_for_type()`、`documentation()`、`deprecated_aliases()`、`deprecation_message()`（这些方法的语义见 [crates/gpui/src/action.rs:L117-L173](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L117-L173)）。然后执行 `cargo expand`，在展开结果中找到每个断言值出现的位置，填出这样的表：

| #[action] 参数 | 落在生成代码的哪个方法 | 你的断言值 |
| --- | --- | --- |
| `namespace = x, name = "Y"` | `name` / `name_for_type` | `"x::Y"` |
| doc 注释 | `documentation` | ... |
| ... | ... | ... |

这张表就是下一讲 u2-l2（生成阶段逐行拆解）的预习提纲：解析阶段收集的每个状态变量，都将在 `impl gpui::Action` 的某个方法里找到自己的落点。

## 6. 本讲小结

- 派生宏的第一步是遍历 `input.attrs`，只认领 `action` 与 `doc` 两种属性，其余（serde 等）一律跳过。
- `attr.parse_nested_meta` 按逗号逐项回调，`meta.path` 是参数名，`meta.input` 中的 `=` 和值需手动解析；每个参数都有「重复指定即报错」的查重。
- 六个参数的值类型各异：`name`/`deprecated` 是字符串字面量、`namespace` 是裸标识符、`deprecated_aliases` 是方括号字符串列表、`no_json`/`no_register` 是无值标志。
- `///` 注释即 `#[doc = "..."]` 属性，逐行 trim 后以 `\n` 拼接，最终经整体 `trim()` 成为 `documentation()`。
- 名称 = `name` 参数（缺省为结构体名），含 `::` 即报错；有 namespace 时拼为 `namespace::Name`，在编译期固化为 `&'static str`。
- 解析错误通过 `panic!` 收口，被 rustc 转成编译错误；兜底报错文案中漏列了 `name` 参数，阅读时需留意。

## 7. 下一步学习建议

本讲到 L117 为止，`derive_action` 手里已经攥着七个状态变量。下一讲 **u2-l2（#[derive(Action)]（下）：生成的 trait 实现逐行拆解）** 将从 [derive_action.rs:L119](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L119) 的 `is_unit_struct` 开始，逐行拆解 `build` 的三种分支（`no_json` / 单元结构体 / serde 反序列化）、`partial_eq` 的向下转型与 `boxed_clone`。学完 u2-l2 后，可以顺路进入 u2-l3 了解 `no_register` 分流到的 `generate_register_action` 与 inventory 注册机制。若想先看消费侧全景，可阅读 [crates/gpui/src/action.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L335-L423) 中 `ActionRegistry` 的构建与 `build_action` 查表逻辑。
