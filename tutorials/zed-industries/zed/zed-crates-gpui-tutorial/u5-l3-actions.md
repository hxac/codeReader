# Action 体系：把按键变成逻辑操作

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 Action（动作）在 GPUI 中解决什么问题：为什么按键不直接调用函数，而要先变成一个「命名意图」。
2. 会用 `actions!` 宏批量定义无参数动作，会用 `#[derive(Action)]` 定义带数据的动作，并说出生成的代码里都包含什么。
3. 理解 `ActionRegistry` 与 `inventory` 的静态自动注册机制：动作在你的 `main` 执行之前就已经被收集进全局清单，`App` 启动时一次装载。
4. 掌握 `on_action` 的标准写法（`on_action(cx.listener(...))`），并能说出动作派发的四个阶段（全局捕获 → 路径捕获 → 路径冒泡 → 全局冒泡）以及「冒泡阶段默认停止传播」这条规则。
5. 通过一个绘图面板实践，验证同一段逻辑可以被键盘和代码两条路径触发。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先快速回顾：

- **事件 vs 动作**（u5-l1）：`MouseDownEvent`、`KeyDownEvent` 这类是**框架预定义的事件**，类型固定、由平台产生；而 Action 是**你自己定义的逻辑操作**，类型由你声明。官方文档 [docs/key_dispatch.md](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/docs/key_dispatch.md#L1-L9) 开篇即点明：GPUI 是键盘优先（keyboard-first）的框架，鼠标功能靠按钮暴露，键盘功能靠「在键位上下文中绑定动作」暴露。
- **监听器活一帧**（u5-l2）：鼠标/键盘监听是在元素 `paint` 阶段注册进窗口的，只在下一帧有效，每帧重新申报。动作监听同样如此——本讲会再次看到这个模式。
- **`cx.listener`**（u2-l3）：把一个「只能拿到 `&mut App` 的回调」适配成「能安全更新某个实体状态的回调」，是动作处理器接入视图状态的标准桥梁。
- **实体与视图**（u2-l2、u3-l1）：动作处理器通常要更新某个实体的状态并 `cx.notify()` 触发重绘。
- **命令模式**（软件工程常识）：把「请求」封装成对象，使请求的发出者（按键）与执行者（处理器）解耦。Action 就是 GPUI 版的命令对象。

一个直觉类比：如果把按键直接绑到函数上，就像把电器的开关焊死在墙上；Action 体系则是「电器插上插座（注册处理器），开关贴在哪面墙（键位绑定）随便你挪」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/action.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L1-L458) | 本讲主战场：`actions!` 宏、`Action` trait、`ActionRegistry` 全部在此 |
| `crates/gpui_macros/src/derive_action.rs` | `#[derive(Action)]` 过程宏，解析 `#[action(...)]` 属性并生成 trait 实现 |
| `crates/gpui_macros/src/register_action.rs` | 生成 `inventory::submit!` 静态注册代码的辅助函数 |
| [src/app.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L679-L830) | `App` 持有 `ActionRegistry`；提供全局 `on_action`、`bind_keys`、`build_action` 等方法 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L2183-L2196) | 动作派发的核心：`dispatch_action` 与四阶段派发循环 |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L426-L441) | `on_action` 链式 API 与监听器在 paint 阶段的落盘 |
| [src/key_dispatch.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L333-L344) | `DispatchTree`：动作监听器的树形存储（u5-l4 深入） |
| [examples/set_menus.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/set_menus.rs#L1-L128) | 菜单 + 全局动作处理器的完整小示例 |
| [examples/input.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L16-L34) | `actions!` + `on_action(cx.listener(...))` + `bind_keys` 的标准三件套写法 |
| [src/platform/app_menu.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform/app_menu.rs#L125-L134) | `MenuItem::action`：菜单项携带一个动作实例 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**actions! 宏（含 derive 宏）**、**ActionRegistry 与 inventory 注册**、**on_action 注册与派发**。

### 4.1 模块一：actions! 宏与 Action trait——动作的定义

#### 4.1.1 概念说明

一个 Action 就是一个实现了 `Action` trait 的结构体，它代表一个**命名的、类型安全的用户意图**，例如「光标左移」「保存文件」「增大笔刷」。

为什么不直接把按键绑到函数上？因为「按键 → 逻辑」若直接耦合，三样东西会被焊死在一起：

1. **输入来源**：同一个逻辑可能来自键盘、菜单、命令面板、别的代码。
2. **键位配置**：用户要能改键，键位必须以数据（JSON 键位表）而非代码存在。
3. **处理位置**：处理逻辑应挂在元素树的某一层，并允许父层拦截或放行。

引入 Action 后，三方解耦：**按键字符串 →（键位表）→ 动作名 →（注册表构建）→ 动作实例 →（元素树派发）→ 处理器**。动作既是键位表里的一个条目（`"editor::MoveUp"`），又是 Rust 里一个可 `downcast` 的具体类型，两副面孔靠下面的宏与注册表粘合。

#### 4.1.2 核心流程

`actions!` 宏接受一个命名空间和一列标识符，为每个标识符生成一个单元结构体并派生 `Action`：

```text
actions!(editor, [MoveUp, MoveDown]);
        ↓ 展开为
#[derive(Clone, PartialEq, Default, Debug, gpui::Action)]
#[action(namespace = editor)]
pub struct MoveUp;          // MoveDown 同理

// gpui::Action 派生再生成：
impl gpui::Action for MoveUp {
    fn name(&self) -> &'static str { "editor::MoveUp" }   // 命名空间::类型名
    fn build(..) -> .. { Ok(Box::new(Self)) }             // 单元结构体直接构造
    ...                                                    // 以及 inventory 注册代码（模块二）
}
```

带数据的动作则手写结构体 + `#[derive(Action)]`，要求额外派生 `serde::Deserialize` 与 `schemars::JsonSchema`（用于从键位表 JSON 反序列化参数）。

#### 4.1.3 源码精读

**宏定义**：[src/action.rs:L23-L40](https://github.com/zed-industries-zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L23-L40) 定义了 `actions!` 的两种形态：带命名空间（`$namespace:path`）与不带。两种形态都只为每个名字生成一条 `#[derive(..., gpui::Action)]` 的单元结构体声明——宏本身极薄，真正的工作在派生宏里。注意 derive 列表里的 `Default` 与 `PartialEq` 是 `Action` trait 的前置要求。

**Action trait**：[src/action.rs:L117-L173](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L117-L173) 定义了动作的完整契约，关键方法如下：

| 方法 | 作用 |
| --- | --- |
| `name(&self) -> &'static str` | 动作全名（含命名空间），键位表、菜单里用的字符串 |
| `name_for_type() -> &'static str` | 同上，但无需实例（静态上下文使用） |
| `boxed_clone(&self) -> Box<dyn Action>` | 类型擦除后的克隆 |
| `partial_eq(&self, &dyn Action) -> bool` | 跨具体类型的相等比较（downcast 后比较） |
| `build(json) -> Result<Box<dyn Action>>` | **从 JSON 构建动作实例**，键位表加载时用 |
| `action_json_schema(..)` | 参数的 JSON Schema，供键位表校验/补全 |
| `deprecated_aliases()` / `deprecation_message()` | 旧名字迁移与弃用提示 |
| `documentation()` | 动作文档，派生宏会从结构体的 doc 注释自动提取 |

trait 上方的文档注释 [src/action.rs:L42-L116](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L42-L116) 列出了 `#[action(...)]` 支持的全部属性参数，值得记住的五个：

| 属性 | 含义 |
| --- | --- |
| `namespace = foo` | 名字前缀，Zed 中必填 |
| `name = "Bar"` | 覆盖类型名作为动作名（不能含 `::`） |
| `no_json` | 不支持从 JSON 构建（`build` 恒报错），可免去 serde/schemars 约束 |
| `no_register` | 不注册进 ActionRegistry（仅手动实现 trait 时用） |
| `deprecated_aliases = ["foo::Old"]` | 旧名仍可触发本动作 |

**派生宏展开**：[derive_action.rs:L104-L134](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macros/src/derive_action.rs#L104-L134) 是几个关键决策点：全名由 `namespace::name` 拼出（L113-L117）；`is_unit_struct` 判定（L119）决定 `build` 的函数体——单元结构体直接 `Ok(Box::new(Self))`，带字段结构体则走 `serde_json::from_value` 反序列化（L121-L128）。最终在 [derive_action.rs:L162-L210](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macros/src/derive_action.rs#L162-L210) 生成完整的 `impl Action` 块，L156-L160 决定是否附带注册代码（`no_register` 则跳过）。

**真实用例**：[examples/input.rs:L16-L34](https://github.com/zed-industries-zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L16-L34) 用一条 `actions!(text_input, [Backspace, Delete, ...])` 声明了文本框示例的全部 14 个动作，命名空间是 `text_input`，因此键位表里它们的名字形如 `text_input::Backspace`。

另有一个内建的「元动作」值得知道：[src/action.rs:L425-L458](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L425-L458) 定义了 `NoAction`（表示「此键位不绑定任何动作」，用于屏蔽默认键）和 `Unbind`（解除某个键位绑定），它们自己也通过 `actions!` / derive 定义，是「用自己验证自己」的好例子。

#### 4.1.4 代码实践

**实践：数一数并追踪一个动作名**

1. 实践目标：确认 `actions!` 的一条声明确实展开成一组独立结构体，并理解命名规则。
2. 操作步骤：
   - 打开 [examples/input.rs:L16-L34](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L16-L34)，数出动作个数。
   - 在 `src/` 下用 `grep -rn "fn backspace" examples/input.rs` 找到处理器（约 L49），观察其第二个参数类型 `&Left` 正是宏生成的结构体。
   - （可选，需本地安装 cargo-expand）运行 `cargo expand -p gpui example input 2>/dev/null | grep -A5 "impl gpui::Action for Left"`，直接看到展开后的 `impl` 块。
3. 需要观察的现象：`grep` 能在示例内找到 `&Left`、`&Backspace` 等类型出现在处理器签名中。
4. 预期结果：14 个动作声明，每个都能在文件内找到对应的处理器方法；`Left` 的完整动作名是 `text_input::Left`。expand 步骤待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`actions!(editor, [MoveUp])` 和手写 `#[derive(Action)] #[action(namespace = editor)] pub struct MoveUp;` 等价吗？

答案：等价。`actions!` 宏展开后就是后者（外加 `Clone/PartialEq/Default/Debug` 派生），见 [src/action.rs:L25-L32](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L25-L32)。宏只是省去重复书写。

**练习 2**：如果要定义一个带参数的动作 `SetBrush { size: u32 }`，需要哪些 derive？为什么比单元结构体多？

答案：需要 `#[derive(Clone, PartialEq, serde::Deserialize, schemars::JsonSchema, Action)]`。多出来的两个是因为带参数动作必须支持从键位表 JSON 反序列化（`build` 走 `serde_json::from_value`），并为键位表提供参数 Schema。参考 [src/action.rs:L55-L70](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L55-L70) 的文档。

**练习 3**：`Action` trait 为什么需要 `partial_eq(&self, &dyn Action)` 这种「和一个类型擦除对象比较」的方法，而不是直接用 `PartialEq`？

答案：派发链路上动作以 `Box<dyn Action>` / `&dyn Action` 形式流动，两个 `dyn Action` 是否表示同一逻辑无法用 `PartialEq`（要求 `Self: Sized`）判断；`partial_eq` 内部先 `downcast_ref::<Self>()` 再比较，让类型擦除后仍可判等。

### 4.2 模块二：ActionRegistry 与 inventory 自动注册

#### 4.2.1 概念说明

定义动作类型只是第一步。键位表是 JSON，里面写的是字符串 `"editor::MoveUp"`；菜单、命令面板也需要「按名字列出/构建动作」。因此运行时需要一份**名字 → 动作构建函数**的全局清单，这就是 `ActionRegistry`。

关键难题是：注册发生在什么时候？如果靠程序员在 `main` 里手写注册调用，几千个动作（Zed 主程序注册了数千个）会是一场灾难。GPUI 的答案是 **`inventory` crate 的链接期静态收集**：派生宏在每个动作类型上生成一段 `inventory::submit!` 代码，这段代码作为静态初始化物被链接进二进制；`inventory` 在任何用户代码运行前把它们收集到一个全局迭代器里。`App` 创建时遍历一次，全部装进注册表。

（准确地说，`inventory` 依赖链接器把 `linkme` 风格的静态段保住——这就是为什么用 GPUI 的动作体系时二进制不能被过度裁剪。）

#### 4.2.2 核心流程

```text
编译期   #[derive(Action)]
              └→ 生成 inventory::submit!(MacroActionBuilder(fn))   （静态注册物）
链接期   inventory 收集所有 MacroActionBuilder
运行期   App::new()
              └→ ActionRegistry::default()
                    └→ load_actions()
                          └→ for builder in inventory::iter::<MacroActionBuilder>
                                └→ insert_action(data)   → by_name / names_by_type_id / all_names
之后     cx.build_action("editor::MoveUp", Some(json))   → 注册表按名构建实例
```

`MacroActionData` 里带着 `type_id`（`TypeId` → 名字的反向映射也存一份）、构建函数、JSON Schema、弃用别名与文档。

#### 4.2.3 源码精读

**静态注册的生成**：[register_action.rs:L15-L47](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macros/src/register_action.rs#L15-L47) 生成的代码在动作类型上挂了一个隐藏函数，返回装满元数据的 `MacroActionData`，并通过 `inventory::submit!` 静态提交 `MacroActionBuilder`。这就是上一节派生宏里 `registration` 变量（[derive_action.rs:L156-L160](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_macros/src/derive_action.rs#L156-L160)）的真身。

**收集端**：[src/action.rs:L264-L282](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L264-L282) 定义了 `MacroActionBuilder`/`MacroActionData` 两个公开（但标记 `#[doc(hidden)]`）的类型——必须公开是因为其他 crate 的宏生成的代码要引用它们——并在 L282 用 `inventory::collect!(MacroActionBuilder)` 声明收集。

**注册表构建**：[src/action.rs:L242-L291](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L242-L291) 中 `ActionRegistry::default()` 调用 `load_actions`，后者遍历 `inventory::iter::<MacroActionBuilder>` 逐个 `insert_action`。注册表本体 [src/action.rs:L233-L240](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L233-L240) 维护四张表：`by_name`（名字 → 构建函数）、`names_by_type_id`（类型 → 名字）、`all_names`、以及弃用别名/文档表。

**重名防护**：[src/action.rs:L293-L333](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L293-L333) 的 `insert_action` 在名字已存在时直接 `panic!`——这就是 trait 文档中「同名注册会在 `App` 创建时 panic」的出处。弃用别名也占名字空间，同样受查重保护（L308-L323）。

**挂载进 App**：[src/app.rs:L687](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L687) 是 `App` 结构上的 `actions: Rc<ActionRegistry>` 字段；[src/app.rs:L809](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L809) 显示它在 `App::new` 时通过 `ActionRegistry::default()` 一次性构建——也就是在这一刻，链接期收集的全部动作被装载。

**对外查询接口**：[src/app.rs:L2266-L2279](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L2266-L2279) 把注册表能力透出为 `cx.build_action(name, json)` 与 `cx.all_action_names()`；底层实现是 [src/action.rs:L336-L369](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L336-L369) 的 `build_action_type`（按 `TypeId` 查名字再构建，供 UI 查询「这个元素能触发哪些动作」）与 `build_action`（按名字 + 可选 JSON 参数构建，供键位表加载）。找不到名字时返回 `ActionBuildError::NotFound`（[src/action.rs:L190-L229](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L190-L229)）。

#### 4.2.4 代码实践

**实践：打印本二进制的动作清单**

1. 实践目标：亲眼验证 inventory 静态注册——你没有写过任何注册调用，但注册表里有名字。
2. 操作步骤：
   - 复制 `examples/set_menus.rs` 为 `examples/list_actions.rs`（放在 `examples/` 下的新 `.rs` 文件默认会被 cargo 自动发现；若未被发现，可在 `Cargo.toml` 仿照 [Cargo.toml:L167-L169](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/Cargo.toml#L167-L169) 补一段 `[[example]]`）。
   - 在 `run_example` 的 `application().run(|cx| { ... })` 回调开头插入：

     ```rust
     // 示例代码：打印注册表中的动作名
     for name in cx.all_action_names() {
         println!("{name}");
     }
     ```

   - 运行 `cargo run -p gpui --example list_actions 2>&1 | head -40`。
3. 需要观察的现象：输出包含 `set_menus::Quit`、`set_menus::ToggleCheck`（来自 set_menus 示例，若该示例也在同一 crate 编译单元中则不一定出现——**注意**：每个 example 是独立二进制，只有本二进制内静态收集的动作才会出现，所以你应当主要看到 `gpui` crate 自身定义的动作，如 `zed::NoAction`、`zed::Unbind`，以及你新示例里定义的动作）。
4. 预期结果：至少能看到 `zed::NoAction`、`zed::Unbind`（定义于 [src/action.rs:L425-L458](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L425-L458)）以及 `list_actions` 二进制内其他被链接进来的动作名；清单非空即证明静态注册生效。具体条目数待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：两个不同 crate 里都写了 `actions!(editor, [Save])`，会发生什么？什么时候发现？

答案：`App::new` → `ActionRegistry::default()` → `insert_action` 时第二个 `editor::Save` 触发 `panic!("Action with name 'editor::Save' already registered...")`（[src/action.rs:L294-L300](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L294-L300)）。这是运行期启动时失败，不是编译错误——这正是命名空间存在的原因：用自己的 crate 名做命名空间可避免撞名。

**练习 2**：`ActionRegistry` 是每个窗口一份还是每个 App 一份？依据是什么？

答案：每个 `App` 一份。它是 `App` 结构的字段（[src/app.rs:L687](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L687)），在 `App::new` 时创建一次（[src/app.rs:L809](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L809)），所有窗口共享。

**练习 3**：为什么 `build_action` 的参数是 `Option<serde_json::Value>`，缺省时传 `{}`？

答案：键位表里无参数动作只写名字字符串，有参数动作写 `["名字", {参数对象}]`（见 [docs/key_dispatch.md:L88-L99](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/docs/key_dispatch.md#L88-L99)）。[src/action.rs:L363](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/action.rs#L363) 用 `params.unwrap_or_else(|| json!({}))` 把「没给参数」统一成空对象，单元结构体的 `build` 忽略它直接 `Box::new(Self)`，带字段结构体则把空对象交给 serde 按各字段默认规则处理。

### 4.3 模块三：on_action——监听注册与派发链路

#### 4.3.1 概念说明

定义并注册了动作之后，还需要两件事才能形成闭环：**谁处理它**（注册监听器）与**它何时被触发**（派发）。

GPUI 提供三个层次的 `on_action`：

| 层次 | 写法 | 适用场景 |
| --- | --- | --- |
| 元素级（最常用） | `div().on_action(cx.listener(Self::on_move_up))` | 挂在元素树上，随焦点与上下文生效 |
| 实体级（低层） | `cx.on_action(TypeId::of::<A>(), window, ..)` | 自定义元素 paint 阶段注册（[src/app/context.rs:L734-L749](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app/context.rs#L734-L749)） |
| 全局级（兜底） | `cx.on_action(|a: &Quit, cx| ...)`（App 方法） | 无窗口焦点归属的兜底，如菜单触发的 Quit |

触发来源也有三类，最终全部汇入同一条派发链路：

1. **键盘**：按键 → 键位表匹配出动作（u5-l4 详解匹配算法）。
2. **代码**：`window.dispatch_action(Box::new(SomeAction), cx)` 或 `focus_handle.dispatch_action(&SomeAction, window, cx)`。
3. **菜单**：`MenuItem::action("名字", SomeAction)` → 平台菜单选择回调 → 派发到聚焦窗口。

#### 4.3.2 核心流程

**注册链路**（元素级，自上而下）：

```text
.on_action(cx.listener(Self::handler))          ① 链式 API（InteractiveElement）
    └→ Interactivity::on_action                  ② 存入 action_listeners: Vec<(TypeId, 监听器)>
paint 阶段 paint_keyboard_listeners             ③ mem::take 取走监听器
    └→ window.on_action(type_id, 监听器)          ④ 注册进下一帧的 dispatch_tree
        └→ DispatchTree::on_action               ⑤ 挂到当前活动节点（每帧重建、只活一帧）
```

**派发链路**（以键盘为例）：

```text
平台 KeyDown
  └→ Window::dispatch_key_event                 焦点节点 → dispatch_path（根到焦点的祖先链）
        └→ DispatchTree::dispatch_key           键位表匹配 → 得到 KeyBinding → build_action 构建实例
              └→ Window::dispatch_action_on_node
                    ① 全局监听器 Capture 正序
                    ② 路径节点 Capture 正序（根→焦点）
                    ③ 路径节点 Bubble 逆序（焦点→根）★ 默认 stop_propagation
                    ④ 全局监听器 Bubble 逆序（兜底）
```

与鼠标事件的关键差异：**动作（以及键盘事件）的派发路径由焦点决定，而不是由命中测试决定**；鼠标事件靠 hitbox 与光标位置，键盘与动作靠 dispatch_path。

#### 4.3.3 源码精读

**① 链式 API 与底层注册**：[src/elements/div.rs:L1042-L1053](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L1042-L1053) 是 `InteractiveElement::on_action`——注意它对任何 `Styled` 元素可用，**不要求 `.id()`**（区别于 `on_click` 等有状态交互）。它转发到 [src/elements/div.rs:L426-L441](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L426-L441) 的 `Interactivity::on_action`：把 `(TypeId::of::<A>(), 闭包)` 压入 `action_listeners`，闭包内先 `downcast_ref::<A>()` 再只在 `DispatchPhase::Bubble` 时调用。捕获阶段的孪生兄弟 `capture_action` 在 [src/elements/div.rs:L405-L424](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L405-L424)，唯一区别是匹配 `DispatchPhase::Capture`。

**② paint 阶段落盘**：[src/elements/div.rs:L3138-L3167](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L3138-L3167) 的 `paint_keyboard_listeners` 用 `mem::take` 取走 `Interactivity` 上积攒的键盘/动作监听器，逐个调 `window.on_action` 注册。这再次印证 u5-l2 的结论：**监听器只活一帧，每帧重申报**——副作用是「处理逻辑要不要存在」本身也是渲染的一部分，条件分支里的 `.when(cond, |el| el.on_action(...))` 可以精确控制某帧是否响应动作。

**③ 窗口与派发树**：[src/window.rs:L5965-L5983](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5965-L5983) 的 `Window::on_action` 带着注释「只能在元素 paint 阶段调用」（L5972 有 `debug_assert_paint` 检查），它把监听器写进 `next_frame.dispatch_tree`——即**渲染帧的派发树**，键盘事件到来时用的是上一帧渲染好的树（`rendered_frame`）。[src/key_dispatch.rs:L333-L344](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L333-L344) 的 `DispatchTree::on_action` 把 `(action_type, listener)` 挂到**当前活动节点**（构建 dispatch tree 时栈顶对应的元素节点）——树形结构怎么来的、如何复用，是 u5-l4 的主题。

**④ 派发四阶段**：核心在 [src/window.rs:L5573-L5686](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5573-L5686) 的 `dispatch_action_on_node_inner`，依次执行：

1. L5581-L5606：**全局监听器 Capture**（正序）。注意它先把 `global_action_listeners` 从 `cx` 上 `remove` 出来再放回——这样监听器内部修改全局监听器集合也不会迭代失效。
2. L5612-L5633：**路径 Capture**，沿 `dispatch_path`（根→焦点）正序。
3. L5635-L5657：**路径 Bubble**，逆序（焦点→根）。L5645 是本模块最重要的一行注释与代码：`cx.propagate_event = false; // Actions stop propagation by default during the bubble phase`——**动作冒泡默认停止传播**（与鼠标事件默认传播相反！），一个元素处理后父层不会再收到，除非处理器显式 `cx.propagate()`。
4. L5659-L5685：**全局监听器 Bubble**（逆序），兜底执行。

任一阶段把 `propagate_event` 置 false 且当前阶段结束即整体返回。

**⑤ 键盘如何变成动作**：[src/window.rs:L5239-L5313](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5239-L5313) 的 `dispatch_key_event` 先确定焦点节点与派发路径（L5244-L5245），再调用 [src/key_dispatch.rs:L483-L519](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L483-L519) 的 `dispatch_key` 匹配键位（支持多键序列 pending 状态），命中后由注册表构建动作实例并进入上面的四阶段。本讲只需记住这条链的入口与出口，中间的匹配算法留给 u5-l4。

**⑥ 代码派发入口**：[src/window.rs:L2183-L2196](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L2183-L2196) 的 `Window::dispatch_action` 在**当前焦点元素**上派发（`cx.defer` 推迟到效果冲刷时机）；[src/window.rs:L626-L635](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L626-L635) 的 `FocusHandle::dispatch_action` 则可指定任意焦点句柄对应的节点。两者都汇入同一个 `dispatch_action_on_node`。

**⑦ 全局监听**：[src/app.rs:L2231-L2248](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L2231-L2248) 的 `App::on_action` 按 `TypeId` 注册到 `global_action_listeners`，文档注释明确：全局处理器在冒泡阶段**末尾**执行，只有元素层没人处理（或处理器调用了 `cx.propagate()`）才会轮到它。

**⑧ 标准三件套（必读范例）**：[examples/input.rs:L589-L604](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L589-L604) 展示了元素级注册的标准写法——`.key_context("TextInput")` 声明键位上下文（u5-l4 详讲）、`.track_focus(&self.focus_handle(cx))` 让此元素可聚焦（决定派发路径起点）、十几个 `.on_action(cx.listener(Self::xxx))` 绑定处理器。`cx.listener` 的定义在 [src/app/context.rs:L247-L260](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app/context.rs#L247-L260)：捕获实体的弱句柄，回调触发时 `view.update(cx, |view, cx| f(view, e, window, cx))`——这正是 u2-l3 学过的桥接模式在动作体系的应用。键位绑定在 [examples/input.rs:L700-L713](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L700-L713)（`cx.bind_keys` + `KeyBinding::new("按键串", 动作, 上下文)`，实现见 [src/app.rs:L2214-L2218](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L2214-L2218)，绑定会追加一个刷新窗口的效果使新键位立即生效）；[examples/input.rs:L763-L764](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/input.rs#L763-L764) 则是全局兜底 + 键位的两行写法。

**⑨ 菜单路径**：[examples/set_menus.rs:L25-L40](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/set_menus.rs#L25-L40) 中，`cx.on_action(quit)` 注册全局处理器（L33-L34），`set_app_menus` 构建菜单；菜单项由 [src/platform/app_menu.rs:L125-L134](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform/app_menu.rs#L125-L134) 的 `MenuItem::action` 创建——它把一个**动作实例**装进菜单项。平台层通过 `Platform::set_menus` 接收菜单、通过 [src/platform.rs:L246](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform.rs#L246) 的 `on_app_menu_action` 回调把用户点击的菜单动作交回框架，最终汇入聚焦窗口的同一条 `dispatch_action` 链路——set_menus 示例没有任何元素级处理器，动作一路冒泡到全局处理器 `quit`/`toggle_check`（[examples/set_menus.rs:L119-L128](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/set_menus.rs#L119-L128)）并被处理，这就是「全局兜底」的实证。

#### 4.3.4 代码实践

**实践：观察捕获与冒泡两个阶段**

1. 实践目标：用一个全局监听器亲眼看到同一个动作的两个阶段各触发一次，并验证冒泡默认停止传播。
2. 操作步骤：
   - 复制 `examples/set_menus.rs` 为 `examples/phases_demo.rs`。
   - 把 `cx.on_action(toggle_check)` 替换为（示例代码）：

     ```rust
     cx.on_action(|action: &ToggleCheck, cx| {
         println!("ToggleCheck 到达全局处理器");
     });
     ```

   - 再为 `Quit` 追加一个会打印两个阶段的版本：由于 `App::on_action` 的闭包签名只拿到 `&A` 和 `&mut App`，观察 Capture/Bubble 需要用元素级 API。改在 `SetMenus` 的 `render` 里加：

     ```rust
     // 示例代码：同时观察捕获与冒泡
     div()
         .capture_action(|_a: &ToggleCheck, _window, cx| {
             println!("capture 阶段");
             cx.propagate(); // 捕获阶段默认继续传播，此处显式声明
         })
         .on_action(cx.listener(|this: &mut SetMenus, _a: &ToggleCheck, _window, _cx| {
             println!("bubble 阶段，处理动作");
             // 不调用 cx.propagate()，观察全局处理器是否还会触发
         }))
         // ...原有样式与 child
     ```

   - 运行示例（菜单路径在 macOS 上可见原生菜单栏；其他平台菜单栏是否显示取决于平台实现，待本地验证），触发 `ToggleCheck`。
3. 需要观察的现象：终端先打印 `capture 阶段`，再打印 `bubble 阶段，处理动作`；而「到达全局处理器」**不会**打印——因为元素处理器没有调用 `cx.propagate()`，冒泡在元素层就被截断。
4. 预期结果：与上述一致。若在元素处理器末尾补一句 `cx.propagate();`，则全局处理器也会触发，可对照验证 [src/window.rs:L5645](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5645) 的默认规则。

#### 4.3.5 小练习与答案

**练习 1**：`.on_action()` 挂在 div 上后，这个监听器能活多久？如果下一帧这个 div 没有被渲染（条件分支关掉了），按键还有反应吗？

答案：只活到当前帧渲染出的派发树被替换。监听器在 paint 阶段经 `paint_keyboard_listeners`（[src/elements/div.rs:L3138-L3167](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L3138-L3167)）写入 `next_frame.dispatch_tree`；键盘事件在 `rendered_frame.dispatch_tree` 上派发。div 不渲染就没有节点，动作无人认领（除非冒泡到全局兜底或被祖先处理）。

**练习 2**：为什么 `on_action` 不要求 `.id()`，而 `on_click` 要求？

答案：`on_click` 依赖跨帧的 hover/active 状态与点击配对（pending_mouse_down），状态以 `(GlobalElementId, TypeId)` 为键存续，需要身份（u5-l2）。`on_action` 的监听器本身每帧重建、不存跨帧状态，`TypeId` 就足以路由（[src/elements/div.rs:L426-L441](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L426-L441)），因此 `InteractiveElement`（无状态）即可提供。

**练习 3**：菜单点击 `Quit` 时窗口里没有任何元素注册 `on_action`，为什么 `cx.on_action(quit)` 还是收到了？

答案：菜单动作最终通过平台的 `on_app_menu_action` 回调汇入派发链路，沿「全局 Capture → 路径 Capture → 路径 Bubble → 全局 Bubble」四阶段走完；路径上无人处理后，最后一步全局 Bubble 兜底执行（[src/window.rs:L5659-L5685](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5659-L5685)，且其文档注释 [src/app.rs:L2231-L2233](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L2231-L2233) 明确「只在无人处理或它们调用了 propagate 时触发」）。

## 5. 综合实践

**任务：绘图面板的笔刷调节——一个动作，两条触发路径**

综合运用本讲三个模块：定义动作（4.1）→ 让动作进入注册表（4.2）→ 键盘与代码两条路径派发到同一处理器（4.3）。

下面是参考实现（**示例代码**，非项目原有文件，建议保存为 `examples/brush_panel.rs`）：

```rust
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    App, Context, KeyBinding, Menu, MenuItem, Window, WindowOptions, actions,
    div, prelude::*, px, rgb,
};
use gpui_platform::application;

// 模块一：定义动作。命名空间 brush 下两个无参数动作
actions!(brush, [IncreaseBrush, DecreaseBrush]);

struct BrushPanel {
    brush_size: usize,
}

impl BrushPanel {
    fn increase(&mut self, _: &IncreaseBrush, _window: &mut Window, cx: &mut Context<Self>) {
        self.brush_size += 1;
        cx.notify();
    }

    fn decrease(&mut self, _: &DecreaseBrush, _window: &mut Window, cx: &mut Context<Self>) {
        self.brush_size = self.brush_size.saturating_sub(1);
        cx.notify();
    }
}

impl Render for BrushPanel {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .size_full()
            .flex_col()
            .gap_4()
            .p_6()
            .bg(rgb(0x1e1e2e))
            .text_color(rgb(0xcdd6f4))
            // 模块三：元素级注册。on_action 不需要 .id()；
            // cx.listener 把回调桥接到本实体的可变状态
            .on_action(cx.listener(Self::increase))
            .on_action(cx.listener(Self::decrease))
            .child(div().text_xl().child("Brush Panel"))
            // 状态栏：显示当前笔刷大小
            .child(
                div()
                    .id("status-bar")
                    .text_xl()
                    .child(format!("笔刷大小: {}", self.brush_size)),
            )
            // 代码派发路径：按钮点击 → window.dispatch_action → 同一处理器
            .child(
                div()
                    .id("btn-increase")
                    .px_4()
                    .py_2()
                    .bg(rgb(0x45475a))
                    .hover(|s| s.bg(rgb(0x585b70)))
                    .child("用代码派发 IncreaseBrush")
                    .on_mouse_down(
                        gpui::MouseButton::Left,
                        // 闭包参数依次是：实体引用、事件、window、Context<BrushPanel>
                        // （Context 可自动 Deref 到 &mut App，满足 dispatch_action 的签名）
                        cx.listener(|_this, _event, window, cx| {
                            window.dispatch_action(Box::new(IncreaseBrush), cx);
                        }),
                    ),
            )
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        // 键盘路径的第一步：把按键绑到动作。
        // "+"/"-" 都是合法按键串（Zed 自带键位表也在用，如 assets/keymaps/vim.json）
        cx.bind_keys([
            KeyBinding::new("+", IncreaseBrush, None),
            KeyBinding::new("-", DecreaseBrush, None),
        ]);

        // 菜单路径：菜单项携带动作实例，点击后汇入同一派发链路
        cx.set_menus([Menu::new("brush_panel").items([
            MenuItem::action("Increase Brush", IncreaseBrush),
            MenuItem::action("Decrease Brush", DecreaseBrush),
        ])]);

        cx.open_window(WindowOptions::default(), |_, cx| {
            cx.new(|_| BrushPanel { brush_size: 4 })
        })
        .unwrap();
    });
}

#[cfg(not(target_family = "wasm"))]
fn main() {
    run_example();
}
```

（两点说明：① `cx.listener` 要求你写的闭包签名是 `|&mut 实体, &事件, &mut Window, &mut Context<实体>|`，它内部帮你 downgrade/upgrade 桥接到实体状态，如 [src/app/context.rs:L247-L260](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app/context.rs#L247-L260) 所示；② 闭包里的 `cx` 是 `&mut Context<BrushPanel>`，因 `Context` 可 `Deref` 到 `App`，可直接传给需要 `&mut App` 的 `window.dispatch_action`。若编译报参数不匹配，请以本地编译器提示为准微调参数名。）

**操作步骤**：

1. 将代码保存为 `crates/gpui/examples/brush_panel.rs`（新文件默认被 cargo 自动发现）。
2. 运行：`cargo run -p gpui --example brush_panel`。
3. **键盘路径**：依次按 `+`、`-`，观察状态栏数字。
4. **代码路径**：点击「用代码派发 IncreaseBrush」按钮，观察状态栏数字。
5. **菜单路径**：若平台显示应用菜单栏（macOS 原生支持；其他平台待本地验证），点菜单项 Increase Brush。

**需要观察的现象与预期结果**：

- 键盘按 `+` 一次，数字 +1；按 `-` 一次，数字 −1（减到 0 不再下降，`saturating_sub` 的效果）。
- 点击按钮与按 `+` 效果完全一致——两条路径触发的是**同一个** `Self::increase` 处理器，这正是 Action 解耦的价值：处理器不知道也不关心动作从哪来。
- 菜单点击同样使数字 +1（若菜单可用）。
- 进阶实验 A：把 `bind_keys` 中 `"+"` 改成 `"ctrl-+"`，重启后直接按 `+` 应无反应——证明键盘路径必须先经过键位表。
- 进阶实验 B：删去 render 中两行 `on_action`，改在 `run_example` 里加 `cx.on_action(|_: &IncreaseBrush, cx| println!("全局兜底: {}", std::any::type_name::<App>()))`——键盘按 `+` 不再改变数字，说明元素层监听才是常规归宿、全局层是兜底。
- 本实践在无显示环境无法运行 GUI，涉及运行结果的条目请本地验证。

## 6. 本讲小结

- **Action 是命名的用户意图**：一个实现 `Action` trait 的结构体。它把按键、菜单、代码调用三种触发源与处理逻辑解耦——按键绑定键位表、处理挂在元素树、意图本身是类型。
- **`actions!` 宏是语法糖**：展开为带 `#[action(namespace = ...)]` 的单元结构体；带数据的动作用 `#[derive(Action)]`，需额外实现 `Deserialize + JsonSchema` 以支持从键位表 JSON 构建。
- **注册靠 inventory 静态收集**：派生宏生成 `inventory::submit!`，链接期收集，`App::new` 时由 `ActionRegistry::default()` 一次装载；重名动作在此时 panic，命名空间是防撞名手段。
- **注册表的双重索引**：`by_name`（字符串 → 构建函数，键位表/菜单用）与 `names_by_type_id`（`TypeId` → 名字，UI 查询可用动作用）。
- **`on_action` 有三层**：元素级（链式 + `cx.listener`，标准写法）、实体级（低层 paint 注册）、全局级（`App::on_action` 冒泡兜底）；监听器在 paint 阶段写入下一帧的 `DispatchTree`，只活一帧。
- **派发四阶段 + 默认截断**：全局 Capture → 路径 Capture → 路径 Bubble（**默认 `stop_propagation`**，与鼠标事件相反，显式 `cx.propagate()` 才放行）→ 全局 Bubble 兜底。

## 7. 下一步学习建议

本讲刻意绕过了派发链路中最有意思的一半：**键位表如何从一串按键匹配出动作**。下一讲 u5-l4（键位派发链路：Keymap、KeyContext 与 DispatchTree）将深入：

- [src/keymap.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs) 与 [src/keymap/context.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs)：`KeyBinding` 的优先级与 `KeyContext`（本讲 `key_context("TextInput")` 那行终于要兑现）。
- [src/key_dispatch.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs)：`DispatchTree` 如何随帧重建、多键序列（pending）与重放（replay）机制。

继续阅读源码时，建议带着两个问题：①「按键没反应」时该沿哪条链排查（键位表没绑 → 上下文不匹配 → 路径上没注册 → 被子层截断）；② Zed 主程序的 `assets/keymaps/*.json` 是 GPUI 动作体系最大规模的真实用例，拿本讲的注册表知识去读它会非常顺畅。
