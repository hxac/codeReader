# #[derive(Action)]（下）：生成的 trait 实现逐行拆解

## 1. 本讲目标

上一讲（u2-l1）我们拆完了 `derive_action.rs` 的属性解析阶段：`#[action(...)]` 的六个参数与文档注释被解析成七个状态变量。本讲从 [derive_action.rs:L119](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L119) 开始，逐行拆解**代码生成阶段**——这些状态变量如何变成一个完整的 `impl gpui::Action`。

学完本讲，你应该能够：

1. 说出 `Action` trait 的九个方法各自的用途，以及哪些方法由宏无条件生成、哪些按分支生成。
2. 读懂 `build` 的三种分支（`no_json` / 单元结构体 / 带字段结构体）与 `action_json_schema` 的两种分支。
3. 理解 `partial_eq` 中「向下转型」的比较技巧，以及它为什么依赖用户自己派生的 `PartialEq`。
4. 解释生成代码里 `gpui::private::serde_json` 这类「奇怪路径」存在的原因。
5. 理解 gpui 运行时如何通过这些生成的方法，从 keymap JSON 反序列化出一个 action 实例。

## 2. 前置知识

本讲会用到几个 Rust 中稍微进阶的概念，先用通俗语言过一遍：

- **trait 对象（`dyn Action`）与类型擦除**：`Box<dyn Action>` 是一个「不知道具体类型，但保证能调用 Action 的对象安全方法」的盒子。GPUI 在派发按键时拿着的就是它——按键处理代码不可能知道几百种 action 的具体类型。
- **`Any` 与向下转型（downcast）**：标准库的 `std::any::Any` trait 为每个类型提供了 `TypeId`，`downcast_ref::<T>()` 可以在运行时检查「这个 trait 对象底层是不是 T」，是则给出 `&T`，否则给出 `None`。这是类型擦除之后「找回」具体类型的唯一通道。
- **对象安全（object safety）与 `where Self: Sized`**：带 `Self: Sized` 约束的关联函数**不能**通过 `dyn Action` 调用，只能写出 `Foo::build(...)` 这种具体类型调用。本讲的 `build`、`name_for_type` 等都属于这一类，这个约束直接塑造了宏生成的代码形态。
- **serde_json 与 schemars**：`serde::Deserialize` 让结构体能从 `serde_json::Value`（一个运行时 JSON 值）反序列化出来；`schemars::JsonSchema` 则为结构体生成 JSON Schema 描述（keymap 编辑器的自动补全就靠它）。
- **编译期与运行期的分界**：宏在编译期把 `&'static str` 名称、函数指针等「烧」进生成的代码；运行期只是查表调用。本讲会反复在这两个期间穿梭。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/gpui_macros/src/derive_action.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L119-L211) | 本讲主角：L119-L211 是代码生成阶段 |
| [crates/gpui/src/action.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L117-L173) | `Action` trait 定义（宏要实现的「合同」）与 `ActionRegistry` 运行时（生成代码的消费者） |
| [crates/gpui_macros/src/register_action.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/register_action.rs#L15-L47) | 生成代码末尾 `#registration` 片段的来源，下一讲（u2-l3）的主角，本讲只带过 |
| [crates/gpui/src/gpui.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L74-L82) | `gpui::private` 模块：生成代码所依赖的 crate 重导出 |
| [crates/gpui/src/app.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L2267-L2279) | `App::build_action` / `App::all_action_names`：运行期从 JSON 构建 action 的公开入口 |

## 4. 核心概念与源码讲解

### 4.1 Action trait 方法清单：宏要填的九个空

#### 4.1.1 概念说明

派生宏的本质是「替你写枯燥的 impl 块」。要读懂生成的代码，先要看清合同——`Action` trait 一共要求/提供了什么。这份合同定义在 [action.rs:L117-L173](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L117-L173)，它的方法可以分成两类：

1. **对象安全方法**（没有 `where Self: Sized`）：可以通过 `&dyn Action` 动态分发。运行期按键派发时调用的就是它们。
2. **`where Self: Sized` 的关联函数**：只能用具体类型调用（`Foo::build(..)`）。trait 为其中五个提供了默认实现，但派生宏**全部**显式覆盖，把值在编译期固化。

#### 4.1.2 核心流程

宏生成阶段的总流程：

```text
属性解析产出（u2-l1）
  full_name / is_unit_struct / no_json / no_register
  deprecated_aliases / deprecated / doc_str
        │
        ▼
为「随分支变化」的方法体先算出 quote 片段
  build_fn_body / json_schema_fn_body
  deprecated_aliases_fn_body / deprecation_fn_body / documentation_fn_body
        │
        ▼
拼进一个固定模板 quote! { #registration impl gpui::Action for #struct_name { ... } }
        │
        ▼
TokenStream 追加在用户结构体后面（派生宏不删除原条目）
```

注意一个重要的 quote 惯用法：**变化的部分先单独算成小片段，再插进大模板**。`#build_fn_body` 这种插值出现在方法体位置时，会原样嵌入预先选好的分支代码。这比在大模板里写 `#if` 式的分支可读得多。

#### 4.1.3 源码精读

先看合同。trait 定义与九个方法（[action.rs:L117-L173](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L117-L173)）：

| trait 方法 | 行号 | 类别 | 宏生成时 |
| --- | --- | --- | --- |
| `boxed_clone(&self) -> Box<dyn Action>` | [L118-L119](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L118-L119) | 对象安全 | 固定模板 |
| `partial_eq(&self, &dyn Action) -> bool` | [L121-L122](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L121-L122) | 对象安全 | 固定模板 |
| `name(&self) -> &'static str` | [L124-L125](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L124-L125) | 对象安全 | 固定模板（插值 `#full_name`） |
| `name_for_type() -> &'static str` | [L127-L130](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L127-L130) | `Self: Sized` | 固定模板 |
| `build(serde_json::Value) -> Result<Box<dyn Action>>` | [L132-L137](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L132-L137) | `Self: Sized` | **三选一分支** |
| `action_json_schema(&mut SchemaGenerator) -> Option<Schema>` | [L138-L144](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L138-L144) | `Self: Sized`（有默认 `None`） | **二选一分支** |
| `deprecated_aliases() -> &'static [&'static str]` | [L146-L154](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L146-L154) | `Self: Sized`（默认 `&[]`） | 二选一分支 |
| `deprecation_message() -> Option<&'static str>` | [L156-L163](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L156-L163) | `Self: Sized`（默认 `None`） | 二选一分支 |
| `documentation() -> Option<&'static str>` | [L165-L172](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L165-L172) | `Self: Sized`（默认 `None`） | 二选一分支 |

再看宏这边对应的大模板（[derive_action.rs:L162-L210](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L162-L210)）：`#registration` 排在最前（下一讲的主角），随后 `impl gpui::Action for #struct_name` 里九个方法与上表一一对应，其中五个方法体是插值变量。整段没有 `split_for_impl()` 泛型处理——因为 `Action` 的派生目标约定为非泛型结构体（`actions!` 宏与文档示例都是如此），这是与 `IntoElement` 派生（见 u2-l4）的一个显著差异。

一个容易忽略的细节：宏写的是 `impl gpui::Action for #struct_name`，即生成代码假定**调用方命名空间里 `gpui` 这个名字可用**。在 gpui crate 内部使用时，靠的是 [action.rs:L426](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L426) 的 `use crate as gpui;` 别名把自身引入为 `gpui`；在业务 crate 里则天然满足（业务代码都依赖 gpui）。

#### 4.1.4 代码实践

**实践目标**：建立「trait 合同 ↔ 宏模板」的一一对应感。

**操作步骤**：

1. 打开 [derive_action.rs:L162-L210](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L162-L210)，从上到下数出九个方法。
2. 对照上面的表格，把每个方法标记为「固定模板」或「插值分支」。
3. 对每个插值分支，向上找到它对应的 `let xxx_fn_body = if ... { ... }` 语句（L121-L154）。

**需要观察的现象**：模板里方法体只有一行 `#xxx_fn_body` 的，才是真正「随 `#[action(...)]` 参数变化」的部分；其余代码对任何 action 都一字不差。

**预期结果**：五个插值变量（`build_fn_body`、`json_schema_fn_body`、`deprecated_aliases_fn_body`、`deprecation_fn_body`、`documentation_fn_body`）全部能在 L121-L154 找到定义。本实践为纯阅读型，**待本地验证**的只有你自己的标注是否齐全。

#### 4.1.5 小练习与答案

**练习 1**：`name()` 和 `name_for_type()` 返回同样的字符串，为什么要有两个方法？

**答案**：`name(&self)` 是对象安全方法，运行期通过 `Box<dyn Action>` 调用（例如 `Debug for dyn Action` 在 [action.rs:L175-L181](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L175-L181) 就用它打印）；`name_for_type()` 带 `where Self: Sized`，允许在**没有实例**的情况下用类型名调用（如 `Foo::name_for_type()`），下一讲的注册代码 `<#type_name as gpui::Action>::name_for_type()` 正是这样用的——注册发生在 `main` 之前，手里只有类型没有值。

**练习 2**：既然 trait 已经给 `deprecated_aliases` 等五个方法提供了默认实现，宏为什么还要显式生成它们？

**答案**：默认实现的返回值是空（`&[]` / `None`），对绝大多数 action 是对的；但宏需要在编译期把 `#[action(...)]` 参数与文档注释「烧」成 `&'static` 数据，所以只要用户提供了参数就必须覆盖默认值。宏采用无条件覆盖（空参数时生成与默认实现等价的 `&[]` / `None`），逻辑上更简单，也让注册端（u2-l3 的 `MacroActionData`）可以统一调用这几个关联函数而不必关心是否有默认值。

**练习 3**：生成代码为什么不需要 `split_for_impl()`？

**答案**：`split_for_impl()` 用于把泛型参数拆成「impl 头 / 类型 / where 子句」三元组，只有泛型类型才需要。`#[derive(Action)]` 面向的是非泛型 action 结构体（`actions!` 宏生成的单元结构体、文档中的 `SelectNext` 等都是），`#struct_name` 直接可用。对比 u2-l4 将讲的 `IntoElement` 派生，它面向任意 `RenderOnce` 类型（可能带泛型和生命周期），就必须处理。

### 4.2 partial_eq 的向下转型与 boxed_clone

#### 4.2.1 概念说明

这两个是 `dyn Action` 世界里最常用的对象安全方法。问题背景：GPUI 在匹配按键绑定时需要回答「刚派发出来的这个 action 和我注册监听的那个 action 是不是同一个？」——两边都是 `&dyn Action`，编译期类型已被擦除，`==` 无法直接使用。解法是**先向下转型回具体类型，再用用户派生的 `PartialEq` 比较**。

`boxed_clone` 解决的是另一个问题：action 派发时可能被多个监听者接收、缓存，需要在不知道具体类型的情况下克隆一份。

#### 4.2.2 核心流程

`partial_eq` 的执行流程：

```text
self.partial_eq(action: &dyn Action)
  │
  ├─ action.as_any()            // &dyn Action → &dyn Any（固有方法）
  │
  ├─ .downcast_ref::<Self>()    // 运行期 TypeId 比较
  │      │
  │      ├─ 底层就是 Self  ──► Some(&Self) ──► self == a（用户的 PartialEq）
  │      └─ 底层是别的类型 ──► None        ──► false
```

#### 4.2.3 源码精读

宏生成的两个方法（[derive_action.rs:L177-L186](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L177-L186)）：

```rust
fn partial_eq(&self, action: &dyn gpui::Action) -> bool {
    action
        .as_any()
        .downcast_ref::<Self>()
        .map_or(false, |a| self == a)
}

fn boxed_clone(&self) -> Box<dyn gpui::Action> {
    Box::new(self.clone())
}
```

这段代码做了三件事：

1. `action.as_any()` 调用的是定义在 [action.rs:L183-L188](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L183-L188) 的**固有方法**（inherent method on `dyn Action`）：`pub fn as_any(&self) -> &dyn Any { self as &dyn Any }`。虽然 `Action: Any`（trait 声明 `pub trait Action: Any + Send`，[action.rs:L117](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L117)），但 `Any` 的 `downcast_ref` 只存在于 `dyn Any` 上，不能隔着 `dyn Action` 调用——所以需要一个固有方法先做一次「再擦除」，把 `&dyn Action` 变成 `&dyn Any`。
2. `downcast_ref::<Self>()` 在运行期比较 `TypeId`：不同类型的 action 比较直接得 `false`（`map_or(false, ..)` 分支），不会 panic。
3. `self == a` 与 `self.clone()` 分别依赖用户为结构体派生的 `PartialEq` 和 `Clone`——这正是 [action.rs:L68-L70](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L68-L70) 文档强调「derive 宏要求类型实现 `Clone` 和 `PartialEq`」的原因：宏生成的代码里**出现了这两个 trait 的方法调用**，缺了就在展开处编译失败。

顺带一提，`dyn Action` 的 `Debug` 实现（[action.rs:L175-L181](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L175-L181)）只打印 `name()`，不需要每个 action 都是 `Debug`——但 `actions!` 宏仍然统一派生了 `Debug`（[action.rs:L27](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L27)），方便业务侧直接打印具体类型。

#### 4.2.4 代码实践

**实践目标**：验证「类型不匹配返回 false」与「克隆保持具体类型」。

**操作步骤**（示例代码，需在本地临时创建文件运行，本讲义不修改仓库）：

1. 在 `crates/gpui_macros/tests/` 下新建临时文件 `partial_eq_probe.rs`（`gpui` 已是 gpui_macros 的 dev-dependency，见 [Cargo.toml:L26-L27](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/Cargo.toml#L26-L27)）；
2. 写入以下内容：

```rust
// 示例代码：crates/gpui_macros/tests/partial_eq_probe.rs（实践后可删除）
use gpui_macros::Action;

#[derive(Clone, PartialEq, Action)]
#[action(namespace = test)]
struct ActionA;

#[derive(Clone, PartialEq, Action)]
#[action(namespace = test)]
struct ActionB;

#[test]
fn partial_eq_across_types() {
    let a = ActionA;
    let b = ActionB;
    let dyn_b: &dyn gpui::Action = &b;
    // 类型不同：downcast_ref::<ActionA>() 返回 None → false
    assert!(!a.partial_eq(dyn_b));
    // 类型相同：走 self == a 分支 → true
    let dyn_a: &dyn gpui::Action = &a;
    assert!(a.partial_eq(dyn_a));
    // boxed_clone 后仍是具体类型
    let cloned: Box<dyn gpui::Action> = a.boxed_clone();
    assert!(cloned.as_any().is::<ActionA>());
}
```

3. 运行 `cargo test -p gpui_macros --test partial_eq_probe`。

**需要观察的现象**：三条断言全部通过；特别留意第三条——`Box<dyn Action>` 经过 `as_any()` 仍能 `is::<ActionA>()`，说明 `boxed_clone` 克隆出了具体类型而非某种空壳。

**预期结果**：测试通过。注意文件顶部没有 `use gpui::Action`（那会与 `gpui_macros::Action` 冲突），`dyn gpui::Action` 路径可以直接使用，因为 gpui 在 [action.rs:L3](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L3) 再导出了派生宏。本实践**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `map_or(false, |a| self == a)` 改成 `unwrap()`，会发生什么？

**答案**：当两个 action 类型不同（例如按键绑定匹配时遍历到别的 action）时，`downcast_ref` 返回 `None`，`unwrap()` 会让进程 panic。类型不同的比较是完全正常的场景，所以必须用「None 即 false」的语义。

**练习 2**：`boxed_clone` 为什么不写成 `self.clone()` 直接返回？

**答案**：`Self::clone(&self) -> Self` 返回的是具体类型，而方法签名要求 `Box<dyn Action>`。`Box::new(self.clone())` 先克隆具体类型再装箱擦除。这也解释了为什么 `Clone` 是 derive `Action` 的硬性前置条件。

### 4.3 build 与 action_json_schema：三种分支与运行时反序列化链路

#### 4.3.1 概念说明

`build` 是整个 Action 体系里**唯一的「从数据到对象」通道**：Zed 的 keymap 是 JSON 文件，里面写的是 `"bindings": { "up": "editor::MoveUp" }` 这类字符串；运行期需要把名字 + JSON 参数变成真正的 action 实例，靠的就是每个类型生成的 `build` 函数。宏根据两个编译期事实选择分支：用户是否声明了 `no_json`，以及结构体是否为单元结构体（`is_unit_struct`，[derive_action.rs:L119](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L119)，判定 `Data::Struct` 且无字段）。

`action_json_schema` 是 `build` 的「文档伴生」：告诉编辑器这类 action 的 JSON 参数长什么样，keymap 的自动补全与校验由此而来。

#### 4.3.2 核心流程

编译期的分支选择（[derive_action.rs:L121-L134](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L121-L134)）：

```text
build_fn_body:
  no_json ?        ──► Err(anyhow!("X cannot be built from JSON"))
  │ 否
  单元结构体 ?      ──► Ok(Box::new(Self))                      # 不经过 serde
  │ 否（带字段）
                    ──► Ok(Box::new(serde_json::from_value::<Self>(_value)?))

json_schema_fn_body:
  no_json 或单元 ?  ──► None
  │ 否
                    ──► Some(<Self as schemars::JsonSchema>::json_schema(_generator))
```

运行期的消费链路（自上而下）：

```text
keymap JSON 加载
  └─ App::build_action(name, params)              app.rs L2267
      └─ ActionRegistry::build_action             action.rs L351
          ├─ by_name 查不到 ──► Err(ActionBuildError::NotFound)
          └─ 取出 ActionData.build 函数指针
              └─ (build_action)(params.unwrap_or(json!({})))
                  └─ 生成的 build 分支执行，返回 Box<dyn Action>
                      └─ 反序列化失败 ──► Err(ActionBuildError::BuildError)
```

#### 4.3.3 源码精读

**编译期：分支片段的拼装**（[derive_action.rs:L119-L134](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L119-L134)）：

```rust
let is_unit_struct = matches!(&input.data, Data::Struct(data) if data.fields.is_empty());

let build_fn_body = if no_json {
    let error_msg = format!("{} cannot be built from JSON", full_name);
    quote! { Err(gpui::private::anyhow::anyhow!(#error_msg)) }
} else if is_unit_struct {
    quote! { Ok(Box::new(Self)) }
} else {
    quote! { Ok(Box::new(gpui::private::serde_json::from_value::<Self>(_value)?) }
};
```

三个分支各自的意义：

- **`no_json` 分支**：错误消息在宏展开时用 `format!` 拼好（注意这是**宏代码里的** `format!`，编译期执行），作为字符串字面量烧进生成代码。适合「逻辑上不可能从 JSON 构造」的 action。
- **单元结构体分支**：`Ok(Box::new(Self))`——没有字段，JSON 里无信息可提取，直接构造。**这就是 `actions!` 宏生成的所有 action 不需要派生 `Deserialize`/`JsonSchema` 的根本原因**（对照 [action.rs:L27](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L27) 的 derive 列表，确实没有它们）。
- **带字段分支**：`gpui::private::serde_json::from_value::<Self>(_value)?` 要求用户派生 `serde::Deserialize`（文档示例见 [action.rs:L61-L66](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L61-L66) 的 `SelectNext`）。`?` 把 `serde_json::Error` 转成 `anyhow::Error`——`build` 返回的 `gpui::Result` 就是 `anyhow::Result` 的再导出（[gpui.rs:L94](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L94) `pub use anyhow::Result;`），anyhow 对任意 `E: std::error::Error` 提供了 `From` 实现，所以这里可以直接 `?`。

`action_json_schema` 与 `build` 共用判定但分支更少（[derive_action.rs:L130-L134](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L130-L134)）：`no_json` **或**单元结构体都返回 `None`（单元 action 的参数恒为 `{}`，无需 schema）；否则调用用户派生的 `schemars::JsonSchema::json_schema`。注意写法 `<Self as gpui::private::schemars::JsonSchema>::json_schema(_generator)` 用了显式 trait 限定调用，因为要调的是 trait 方法而字段名可能恰好叫 `json_schema`（可能性极低，但显式写法零歧义）。

**运行期：函数指针的桥接**。`build` 带 `where Self: Sized`，不能通过 `dyn Action` 调用——那注册表怎么在运行期构建 action？答案是编译期把关联函数**提升为函数指针**存进注册表（[action.rs:L231](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L231)）：

```rust
type ActionBuilder = fn(json: serde_json::Value) -> anyhow::Result<Box<dyn Action>>;
```

`ActionData`（[action.rs:L259-L262](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L259-L262)）持有 `build` 与 `json_schema` 两个函数指针；谁来填？是下一讲的 `generate_register_action`，它写出 `<#type_name as gpui::Action>::build` 把关联函数 coerce 成 `fn` 指针（[register_action.rs:L33-L34](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/register_action.rs#L33-L34)）。这是「trait 对象 + Sized 关联函数」组合的标准桥接技巧，值得记住。

运行期的查找与调用在 [action.rs:L351-L369](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L351-L369)：按名字从 `by_name` 取出函数指针，参数缺省时补 `json!({})`（所以单元 action 的 `{}` 也会走一遍 `build`，只是分支直接构造 `Self`），失败统一包装成 `ActionBuildError::NotFound` 或 `BuildError`。公开入口是 [app.rs:L2267-L2273](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L2267-L2273) 的 `App::build_action`。

**一个现成的带字段例子**：gpui 内部就有一个带字段的注册 action——`Unbind`（[action.rs:L445-L447](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L445-L447)）：

```rust
#[derive(Clone, Debug, PartialEq, Deserialize, JsonSchema, gpui::Action)]
#[action(namespace = zed)]
pub struct Unbind(pub gpui::SharedString);
```

它派生了 `Deserialize` + `JsonSchema`，走「带字段」分支；同文件的 `NoAction` 由 `actions!(zed, [NoAction])` 生成（[action.rs:L430-L437](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L430-L437)），走「单元」分支。综合实践会用到它们。

#### 4.3.4 代码实践

**实践目标**：亲眼看三个分支生成的代码差异。

**操作步骤**：

1. 安装 cargo-expand（若未安装）：`cargo install cargo-expand`（需要 nightly 工具链）。
2. 展开 gpui 的 action 模块（其中同时含单元 action `NoAction` 与带字段 action `Unbind`）：

   ```bash
   cargo expand -p gpui action > /tmp/action_expanded.rs
   ```

3. 在输出中搜索 `impl gpui::Action for NoAction` 与 `impl gpui::Action for Unbind`，摘出两者的 `build` 与 `action_json_schema` 方法体。
4. 对照 [derive_action.rs:L121-L134](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L121-L134) 标注每个方法体来自哪个分支。

**需要观察的现象**：

- `NoAction::build` 的方法体是 `Ok(Box::new(Self))`，`NoAction::action_json_schema` 是 `None`；
- `Unbind::build` 的方法体含 `gpui::private::serde_json::from_value::<Unbind>`，`action_json_schema` 返回 `Some(...)`。

**预期结果**：两张方法体对照表与源码分支一一对应。展开输出较长（gpui 的 action 模块还包含 `ActionRegistry` 等运行时代码），建议用 `grep -A 5 'fn build'` 过滤。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一个带字段结构体忘了派生 `serde::Deserialize`，什么时候报错？报在哪？

**答案**：宏本身不检查（derive_action.rs 里没有任何 `Deserialize` 探测代码），错误发生在宏**展开之后**的普通类型检查阶段：生成代码里的 `serde_json::from_value::<Self>(_value)` 需要 `Self: Deserialize`，于是报 E0277（trait 未实现），错误位置指向展开后的那一行。这就是宏生成代码「借用用户 impl」的典型失败模式。

**练习 2**：为什么 `no_json` 的错误消息能精确说出 action 全名？

**答案**：宏在编译期已拼出 `full_name`（u2-l1 讲过的 `namespace::Name` 规则），[derive_action.rs:L122](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L122) 用宏内的 `format!` 把它嵌进 `anyhow!` 的字符串字面量，编译期固化，运行期零开销。

**练习 3**：`build` 为什么不能通过 `&dyn Action` 调用，而运行期注册表照样能构建 action？

**答案**：`build` 带 `where Self: Sized` 约束（trait 对象没有编译期已知的尺寸），被排除在动态分发之外。注册表的做法是在编译期把 `<T as Action>::build` 提升为 `ActionBuilder` 类型的裸函数指针存进 `ActionData`（[action.rs:L259-L262](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L259-L262)），运行期按名字查表后以 `(build)(json)` 形式调用。函数指针不关心 `Sized`，因为它在编译期就绑定到了具体类型。

### 4.4 元数据方法与 gpui::private 重导出路径

#### 4.4.1 概念说明

剩下三个方法（`deprecated_aliases`、`deprecation_message`、`documentation`）不参与构建逻辑，只输出「关于这个 action 的描述性数据」，供 Zed 的 keymap 校验、迁移警告和文档生成使用。它们的共同点是：**把编译期收集的字符串列表/字符串变成 `&'static` 返回值**。

而贯穿所有生成代码的 `gpui::private::serde_json`、`gpui::private::anyhow`、`gpui::private::schemars` 这类路径，回答的是过程宏的一个经典问题：**生成代码在别人的 crate 里展开，怎么保证它引用的依赖一定能解析？**

#### 4.4.2 核心流程

quote 的重复插值语法 `#(#aliases),*` 把编译期的 `Vec<String>` 展开成逗号分隔的字面量序列：

```text
编译期 deprecated_aliases = ["editor::OldName", "editor::AncientName"]
        │  quote! { &[#(#aliases),*] }
        ▼
生成代码  &["editor::OldName", "editor::AncientName"]   （'static 数组字面量）
```

`gpui::private` 路径解析的保障链：

```text
用户 crate（如 editor）──依赖──► gpui ──re-export──► serde_json / anyhow / schemars / inventory
        │
        ▼
生成代码写 gpui::private::serde_json::Value
  ⇒ 只要用户能编译出 gpui，这条路径必然可解析
  ⇒ 用户 crate 无需直接依赖 serde_json
```

#### 4.4.3 源码精读

三个元数据片段（[derive_action.rs:L136-L154](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L136-L154)）：

```rust
let deprecated_aliases_fn_body = if deprecated_aliases.is_empty() {
    quote! { &[] }
} else {
    let aliases = deprecated_aliases.iter();
    quote! { &[#(#aliases),*] }
};

let deprecation_fn_body = if let Some(message) = deprecated {
    quote! { Some(#message) }
} else {
    quote! { None }
};

let documentation_fn_body = if let Some(doc) = doc_str {
    let doc = doc.trim();
    quote! { Some(#doc) }
} else {
    quote! { None }
};
```

要点：

- `#(#aliases),*` 是 quote 的**重复插值**：`#aliases` 绑定到迭代器（`Vec<String>::iter()`），`$(...),*` 语义是「对每个元素展开一次，用逗号连接」。字符串插进 `&[...]` 后成为数组字面量，自动 `'static`。
- `#message` / `#doc` 是 `String` 插值成字符串字面量——`ToTokens` 对 `String` 的实现就是转成带引号的 str 字面量。
- 这些值的最终去向是注册数据结构 `MacroActionData`（[action.rs:L269-L280](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L269-L280)，字段 `deprecated_aliases` / `deprecation_message` / `documentation`），由下一讲的 `generate_register_action` 填充，运行期汇入 `ActionRegistry` 的三张表（[action.rs:L237-L239](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L237-L239)）。本讲只需记住「元数据方法的总线在 `MacroActionData`」。

**`gpui::private` 重导出模块**（[gpui.rs:L74-L82](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L74-L82)）：

```rust
/// Do not touch, here be dragons for use by gpui_macros and such.
#[doc(hidden)]
pub mod private {
    pub use anyhow;
    pub use inventory;
    pub use schemars;
    pub use serde;
    pub use serde_json;
}
```

为什么必须这样？生成代码在**用户 crate** 里展开。假如宏直接生成 `serde_json::Value`，就要求每个使用 `#[derive(Action)]` 的 crate 都在自己的 `Cargo.toml` 里声明 `serde_json` 依赖——对 Zed 仓库里上百个 crate 是灾难性的样板。通过 `pub use` 把这些 crate 挂进 gpui 的命名空间后，生成代码统一走 `gpui::private::xxx`，唯一前提「用户 crate 依赖 gpui」天然成立。`#[doc(hidden)]` 与那句 "here be dragons" 注释则明确告诉人类读者：这是宏的私有通道，不要手动使用。

顺带完成本讲最后一块拼图：生成代码的**最外层结构**（[derive_action.rs:L156-L163](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L156-L163)）：

```rust
let registration = if no_register {
    quote! {}
} else {
    generate_register_action(struct_name)
};

TokenStream::from(quote! {
    #registration

    impl gpui::Action for #struct_name {
        ...
    }
})
```

`no_register` 为真时只生成 trait 实现不生成注册代码（适用于「实现 Action 但不支持按名调用/JSON 构建」的场景，见 [action.rs:L81-L83](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L81-L83)）；否则在 impl 之前先插入一段隐藏的注册代码——它长什么样、如何借助 `inventory` 在 `main` 之前生效，正是下一讲 u2-l3 的主题。

#### 4.4.4 代码实践

**实践目标**：数一数生成代码里 `gpui::private::` 的出现点，理解它的覆盖面。

**操作步骤**：

1. 在 4.3 实践产出的 `/tmp/action_expanded.rs` 中执行：

   ```bash
   grep -n 'gpui::private::' /tmp/action_expanded.rs | head -30
   ```

2. 对每个命中，判断它出现在哪个方法/哪个片段里。

**需要观察的现象**：命中集中在 `build`（serde_json、anyhow）与 `action_json_schema`（schemars），以及注册代码里的 `gpui::private::inventory::submit!`。

**预期结果**：`gpui::private` 是生成代码引用「宏作者选定的工具 crate」的唯一通道；trait 定义本身（[action.rs:L134](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L134)）写的是普通 `serde_json::Value`——因为 action.rs 在 gpui crate **内部**，那里 serde_json 是真实依赖。两处写法不同但指同一个类型。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`quote! { &[#(#aliases),*] }` 中的 `#(#aliases),*` 分别是什么意思？

**答案**：外层 `#(...)*` 是重复插值，对绑定的迭代器逐元素展开模板；尾部的 `,` 是分隔符，表示元素之间用逗号连接（相当于宏规则里的 `$(...),*`）。`#aliases` 绑定到 `deprecated_aliases.iter()`，每个 `String` 被 `ToTokens` 转成字符串字面量。

**练习 2**：如果把生成代码里的 `gpui::private::serde_json` 改成 `serde_json`，对 Zed 仓库会发生什么？

**答案**：所有派生了 `Action`（带字段）却没把 `serde_json` 列进自己 `Cargo.toml` 的业务 crate 都会报「unresolved import / can't find crate」编译错误。要么每个 crate 补依赖，要么统一经由 gpui 重导出——后者正是现状的做法。同理 `inventory`（注册宏用到）与 `schemars` 也走这条通道。

**练习 3**：`no_register` 和 `no_json` 有什么本质区别？

**答案**：`no_json` 只关掉「从 JSON 构建」的能力（`build` 恒错、schema 为 `None`），action 仍会注册、仍可按名引用；`no_register` 关掉的是**注册本身**（[derive_action.rs:L156-L160](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L156-L160)），生成的只有 `impl Action`，运行期注册表里查不到这个名字，适合纯程序化构造、不需要出现在 keymap 里的 action。

## 5. 综合实践

把本讲四个模块串起来：**先用 cargo expand 看「宏生成了什么」，再写测试验证「运行期真的能这么用」**。

### 第一部分：展开并标注（对应 4.1/4.3/4.4）

1. 执行 `cargo expand -p gpui action > /tmp/action_expanded.rs`。
2. 在输出中找到 `NoAction`（`actions!` 宏生成）与 `Unbind`（手写 derive）的 `impl gpui::Action` 块。
3. 制作一张四列对照表：**方法 | NoAction 的展开体 | Unbind 的展开体 | 对应 derive_action.rs 的分支行号**，至少覆盖 `build`、`action_json_schema`、`partial_eq`、`boxed_clone` 四行。前两项应体现出分支差异，后两项应完全相同（固定模板）。

如果你想展开自己写的 `actions!(test, [SampleAction])`，可在任意依赖 gpui 的临时 crate 里写 `gpui::actions!(test, [SampleAction]);` 再 `cargo expand`，效果等同（`actions!` 只是声明宏包装，最终都落到同一个派生宏上，见 u1-l3）。

### 第二部分：运行期从 JSON 构建 action（对应 4.2/4.3）

在 `crates/gpui_macros/tests/` 下临时创建 `build_action_from_json.rs`（示例代码，实践后删除；`gpui` 已是 dev-dependency）：

```rust
// 示例代码：crates/gpui_macros/tests/build_action_from_json.rs
// 复用 gpui 内置的两个注册 action：
//   zed::NoAction —— 单元结构体（actions! 生成，build 走 Ok(Box::new(Self)) 分支）
//   zed::Unbind   —— 带字段（build 走 serde_json::from_value 分支）

#[gpui::test]
fn build_actions_from_json(app: &mut gpui::App) {
    // 1) 单元 action：不传参数 → 运行期补 json!({}) → 直接构造 Self
    let no_action = app
        .build_action("zed::NoAction", None)
        .expect("zed::NoAction is registered");
    assert_eq!(no_action.name(), "zed::NoAction");
    assert!(gpui::is_no_action(no_action.as_ref())); // 确认具体类型

    // 2) 带字段 action：JSON 字符串 → serde 反序列化出 SharedString 字段
    let unbind = app
        .build_action(
            "zed::Unbind",
            Some(gpui::private::serde_json::Value::String("editor::NewLine".into())),
        )
        .expect("zed::Unbind is registered");
    assert_eq!(unbind.name(), "zed::Unbind");
    let unbind = unbind
        .as_any()
        .downcast_ref::<gpui::Unbind>()
        .expect("downcast to concrete Unbind");
    assert_eq!(unbind.0.as_ref(), "editor::NewLine");

    // 3) 未注册的名字 → NotFound
    let err = app.build_action("no_such::Action", None).unwrap_err();
    assert!(matches!(err, gpui::ActionBuildError::NotFound { .. }));
}
```

运行：`cargo test -p gpui_macros --test build_action_from_json`。

**需要观察的现象**：

- 步骤 2 里 `as_any().downcast_ref::<gpui::Unbind>()` 成功——这正是 4.2 讲的向下转型在真实 API 上的用法（`is_no_action` 内部同样基于 `as_any().is::<NoAction>()`，见 [action.rs:L450-L452](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L450-L452)）；
- JSON 值经由 `gpui::private::serde_json::Value` 构造——呼应 4.4：测试 crate 没有直接依赖 serde_json，仍能使用该类型。

**预期结果**：测试通过，四列对照表与 [derive_action.rs:L121-L154](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_action.rs#L121-L154) 的分支一一对应。`#[gpui::test]` 的同步形式（参数 `&mut App`）在下一单元 u4-l1/u4-l2 会详细拆解，这里把它当作「能拿到 `&mut App` 的测试」用即可。以上均**待本地验证**。

## 6. 本讲小结

- `Action` trait 共九个方法：三个对象安全方法（`name`/`partial_eq`/`boxed_clone`）由固定模板生成；六个 `Self: Sized` 关联函数中，`name_for_type` 固定生成，其余五个（`build`、`action_json_schema`、三个元数据方法）按编译期分支生成片段再插值进模板。
- `partial_eq` 用 `as_any().downcast_ref::<Self>()` 先找回具体类型再用用户的 `PartialEq` 比较，类型不匹配返回 `false`；`boxed_clone` 依赖用户的 `Clone`。这两个 derive 是 `#[derive(Action)]` 的硬性前提。
- `build` 有三分支：`no_json` → 恒错；单元结构体 → `Ok(Box::new(Self))`（不经 serde，这就是 `actions!` 生成的 action 无需 `Deserialize` 的原因）；带字段 → `serde_json::from_value`。`action_json_schema` 在 `no_json` 或单元时返回 `None`，否则用 schemars 生成。
- 运行期链路：`App::build_action` → `ActionRegistry::build_action` 按名查函数指针 → 调生成的 `build`；`Self: Sized` 方法通过「编译期提升为 `ActionBuilder` 函数指针」跨越了 trait 对象不能动态调用 Sized 关联函数的鸿沟。
- 生成代码统一以 `gpui::private::serde_json` 这类全路径引用工具 crate，靠 gpui 的重导出保证在任意用户 crate 可解析，免去「每个业务 crate 都要声明 serde_json/schemars/anyhow/inventory 依赖」的样板。

## 7. 下一步学习建议

本讲结束时，`impl gpui::Action` 已经完整，但它只出现在类型上——运行期注册表还不知道这个 action 的存在。衔接两者的正是生成代码最前面的 `#registration` 片段。下一讲 **u2-l3《register_action 与 inventory：action 如何自动注册》** 将拆解 [register_action.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/register_action.rs#L15-L47)：隐藏的 `__autogenerated` 函数、`MacroActionData` 的组装、`inventory::submit!` 如何在 `main` 执行前收集所有 action，以及 `ActionRegistry::load_actions`（[action.rs:L286-L291](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/action.rs#L286-L291)）如何消费它们。

在继续之前，建议先完成本讲综合实践的第一部分（cargo expand 标注），因为展开输出里紧挨着 `impl gpui::Action` 的那段 `__gpui_actions_builder_*` 代码，就是下一讲的入场券。
