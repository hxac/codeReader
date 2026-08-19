# Component trait 逐方法精读

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐个说出 `Component` trait 七个方法（`id`、`scope`、`status`、`name`、`sort_name`、`description`、`preview`）的签名、默认实现和覆写时机。
2. 解释「`name()` 默认取 `std::any::type_name::<Self>()`、`id()` 默认取 `name()`」这条默认值派生链，以及它在预览界面上的呈现效果。
3. 理解 `preview` 返回 `AnyElement` 意味着可以返回任意元素树，以及它为什么以函数指针的形式被「冷冻」在注册表里。
4. 写出一个只实现 `description` 与 `preview` 的最小组件，并准确推断其余五个方法各自会返回什么。

## 2. 前置知识

本讲只需要几块很小的 Rust 基础，我们先把术语讲清楚：

- **关联函数（associated function）**：`impl` 块里不带 `self` 参数的函数，通过类型名调用（如 `Divider::name()`），不需要先创建实例。`Component` trait 的七个方法全部是关联函数，这是理解它一切设计的钥匙。
- **trait 默认方法**：Rust 允许 trait 方法带默认实现，实现者不写时就用默认值。`Component` 的七个方法中有五个带默认实现，只有 `description` 和 `preview` 必须自己写。
- **`&'static str`**：编译期就确定、整个程序生命周期内有效的字符串字面量。注意它不是 `String`，没有堆分配、不能运行时拼接——这决定了组件的静态描述必须「写死」。
- **函数指针 `fn(...)`**：Rust 里每个 `fn` 定义既是一个函数项（function item），也可以被强转为裸函数指针 `fn(A, B) -> R` 存进结构体字段。注册表存的就是 `preview` 的函数指针。
- **`std::any::type_name::<T>()`**：标准库提供的编译期反射，返回类型 `T` 的完整路径字符串（如 `"ui::components::divider::Divider"`）。它只保证唯一性，不保证格式跨编译器稳定。
- **类型擦除与 `AnyElement`**：GPUI 中的元素有几十种具体类型，`AnyElement` 是把它们装箱抹平后的统一类型（类比 `Box<dyn Any>`）。`into_any_element()` 把任意元素转成它，从而能用同一个返回类型装下任意元素树。
- **newtype 模式**：`ComponentId(pub &'static str)` 这种单字段元组结构体就是 newtype，用全新类型包住底层类型，避免把「裸字符串」到处传。

承接上一单元：你已经知道 component crate 只有 `component.rs`（机制）和 `component_layout.rs`（展示）两个文件，外界通过 `ui::prelude` 使用 `Component`、`RegisterComponent` 等符号。本讲专门解剖 `component.rs` 里的 trait 本身；注册表数据结构（`ComponentRegistry`、`ComponentMetadata` 的完整 API）是下一讲 u2-l2 的主题，这里只涉及两者交接的部分。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/component/src/component.rs` | Component trait、注册表、scope/status 枚举 | **主战场**：trait 定义（L170-L258）、`ComponentId`（L106-L107）、`register_component`（L47-L61） |
| `crates/ui_macros/src/ui_macros.rs` | `RegisterComponent` 派生宏入口 | 顶部 doc 注释里的 `MyComponent` 最小示例（L19-L43），是官方给出的「最少要写什么」 |
| `crates/ui/src/components/divider.rs` | 分隔线组件 | 一个真实、完整的 `impl Component for Divider`（L162-L239） |
| `crates/ui/src/components/button/button.rs`、`icon_button.rs` | 按钮组件 | `sort_name()` 覆写的真实案例（`"ButtonA"`、`"ButtonB"`） |
| `crates/ui/src/prelude.rs` | ui crate 的符号出口 | 确认 `Component`、`RegisterComponent` 等如何到达业务代码（L10-L13） |
| `crates/component_preview/src/component_preview.rs` | 预览应用 | 各方法返回值在界面上的消费点（排序、标题、过滤） |

永久链接统一基于当前 HEAD `28c0f4aef8d298a08176977d9839671827f5528`。

## 4. 核心概念与源码讲解

### 4.1 Component trait 的形状：一份「类型级契约」

#### 4.1.1 概念说明

初学者看到 trait 的第一反应往往是「定义对象的行为接口」——比如 `Render` 就是对某个实例调用 `render(&mut self, ...)`。`Component` 不是这种 trait。它的七个方法**全部没有 `self`**，都是关联函数：调用它们不需要任何组件实例，只需要类型本身。

为什么这样设计？因为注册表关心的不是「某个按钮实例长什么样」，而是「`Divider` 这个**类型**有哪些静态属性、以及如何凭空造出它的演示」。看 trait 顶部的 doc 注释：

> Implement this trait to define a UI component. This will allow you to derive `RegisterComponent` on it, in turn allowing you to preview the contents of the preview fn in `workspace: open component preview`.（实现这个 trait 来定义 UI 组件，之后就可以在它上面 derive `RegisterComponent`，从而在组件预览面板里预览 `preview` 函数的内容。）

也就是说，`Component` 是一份「类型级契约」：类型用七个关联函数交代自己的身份证（`id`）、归属（`scope`）、状态（`status`）、名字（`name`/`sort_name`）、说明（`description`）和演示（`preview`）。注册机制在启动时把这些答案一次性「抄录」进注册表，之后预览界面只跟注册表打交道，不再需要类型本身。

这个设计还有一个直接后果：`dyn Component`（trait 对象）不可用——Rust 规定没有接收者（receiver）的方法会让 trait 失去对象安全性。这不是缺陷而是意图：动态分发被刻意排除，替代品是下一讲要精读的 `ComponentMetadata`（把信息拆成 `SharedString` 和函数指针存起来）。

#### 4.1.2 核心流程

trait 方法何时被调用？整条链路是（链接期部分在 u2-l3 详讲，这里只看运行期）：

1. **链接期**：`#[derive(RegisterComponent)]` 生成的代码通过 `inventory` 把一个「注册函数」登记进分布式切片——此时什么都不执行。
2. **运行期 init**：`component::init()` 遍历切片，逐个调用注册函数。
3. **注册函数**调用 `register_component::<T>()`，**就在这一刻**，`T` 的七个关联函数被逐一调用，返回值被抄录进 `ComponentMetadata`。
4. `ComponentMetadata` 以 `T::id()` 为键插入全局注册表 `COMPONENT_DATA`。
5. 此后预览界面读取注册表；渲染某个组件时才调用 `metadata` 里存的 `preview` 函数指针。

伪代码表示第 3、4 步：

```text
register_component::<T>():
    metadata = ComponentMetadata {
        id:          T::id(),          // 关联函数此刻求值
        description: T::description(), // &'static str 包装成 SharedString
        name:        T::name(),
        preview:     T::preview,       // 注意：存的是函数本身，不是调用结果
        scope:       T::scope(),
        sort_name:   T::sort_name(),
        status:      T::status(),
    }
    COMPONENT_DATA.write().components.insert(id, metadata)
```

关键点：**七个方法只在注册时各调用一次**（`preview` 则连一次都不调用，只取函数地址）。它们必须 Pure 且便宜——事实上它们也只能返回编译期常量或函数指针。

#### 4.1.3 源码精读

先看 trait 声明与 doc 注释给出的总体建议：

[src/component.rs:L160-L170](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L160-L170)

```rust
/// Generally you will want to implement at least `scope` and `preview`
/// from this trait, so you can preview the component, and it will show up
/// in a section that makes sense.
pub trait Component {
```

官方建议很直白：至少实现 `scope` 和 `preview`。再加上语法上必须实现的 `description`，一个「合格」组件通常写三个方法。

再看抄录现场 `register_component`，它就是上一节伪代码的原文：

[src/component.rs:L47-L61](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L47-L61)

```rust
pub fn register_component<T: Component>() {
    let id = T::id();
    let metadata = ComponentMetadata {
        id: id.clone(),
        description: SharedString::new_static(T::description()),
        name: SharedString::new_static(T::name()),
        preview: T::preview,          // 存函数指针，不调用
        scope: T::scope(),
        sort_name: SharedString::new_static(T::sort_name()),
        status: T::status(),
    };

    let mut data = COMPONENT_DATA.write();
    data.components.insert(id, metadata);
}
```

注意 `SharedString::new_static`：它把 `&'static str` 零拷贝地包成引用计数的 `SharedString`，让元数据可克隆的同时不产生堆分配。`preview: T::preview` 一行没有括号——取的是函数项，随后强转为字段声明的函数指针类型。

最后确认这些方法在 `ComponentMetadata` 里落地后的类型：

[src/component.rs:L109-L118](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L109-L118)

```rust
#[derive(Clone)]
pub struct ComponentMetadata {
    id: ComponentId,
    description: SharedString,
    name: SharedString,
    preview: fn(&mut Window, &mut App) -> AnyElement,
    scope: ComponentScope,
    sort_name: SharedString,
    status: ComponentStatus,
}
```

#### 4.1.4 代码实践

**实践目标**：亲眼确认「七个方法只在 `register_component` 里被调用一次、`preview` 只存指针」这个论断，建立对调用时机的直觉。

**操作步骤**：

1. 在仓库根目录执行 `git grep -n "register_component" -- crates/ui/src`，观察哪些文件出现了这个函数名（它们是派生宏生成的注册函数的调用点）。
2. 执行 `git grep -n "T::preview\|\.preview()" -- crates/component/src/component.rs`，对比 `register_component` 里的 `preview: T::preview`（取地址）与 `ComponentMetadata::preview(&self)` 访问器（返回函数指针）两处代码。
3. 在 [src/component.rs:L76-L80](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L76-L80) 的 `sorted_previews` 旁停下想一想：排序为什么按 `name()` 而不是 `sort_name()`？（这个问题在 4.3 的练习里回答。）

**需要观察的现象**：`register_component` 是整个 crate 里唯一一处集中调用七个关联函数的地方。

**预期结果**：`git grep` 会显示 `register_component` 出现在 `component.rs`（定义）以及各组件文件中由宏生成的 `__component_registry_internal_register_*` 函数体内（如 `crates/ui/src/components/divider.rs`）。派生宏如何生成这些函数是 u2-l4 的内容，本讲只需确认调用点存在。待本地验证（grep 结果随代码演进变化）。

#### 4.1.5 小练习与答案

**练习 1**：既然 `Component` 的方法都没有 `self`，那 `Component` 和「带默认实现的普通 trait」相比，丢了什么能力？

**答案**：丢了「按实例定制」的能力——无法让同一个类型的两个实例注册出不同的描述或预览。但组件目录本来就要的是「每个类型一条目录项」，静态化反而换来了 `&'static str`、函数指针这些可廉价存储的表示，并使 `dyn Component`（及其运行期开销）变得不必要。

**练习 2**：如果给 `Component` 增加一个 `fn version() -> u32` 默认返回 `1` 的方法，`register_component` 需要改吗？

**答案**：需要。`register_component` 是唯一把 trait 答案抄录进 `ComponentMetadata` 的地方，新方法若想被预览界面看到，必须同时扩展 `ComponentMetadata` 结构体、抄录一行 `version: T::version()`，并考虑旧注册数据的兼容性。这也是「加一个 trait 方法」在这套架构里的真实成本。

### 4.2 ComponentId 与 name 的默认值派生链

#### 4.2.1 概念说明

`ComponentId` 是注册表里每个组件的「主键」，而它的默认值来自一条两级派生链：

```text
std::any::type_name::<Self>()        （name 的默认值，完整类型路径）
        ↓ 原样包装
ComponentId(Self::name())            （id 的默认值）
        ↓ 用作
COMPONENT_DATA 中 HashMap 的键
```

为什么不直接用 `String` 或裸 `&str` 当键？newtype `ComponentId(pub &'static str)` 有两个好处：一是类型系统能防止把随便一个字符串误当组件 id 传入；二是它派生了 `Debug, Clone, PartialEq, Eq, Hash`，正好满足 `HashMap` 键的全部约束。

类型路径（如 `ui::components::divider::Divider`）天然全局唯一，所以默认情况下不需要任何人操心 id 冲突。而界面上显示时又要用短名——这就是 `ComponentMetadata::scopeless_name()` 存在的原因：按 `::` 切开取最后一段。

#### 4.2.2 核心流程

一个默认配置组件的身份信息流：

1. `Divider::name()` 调 `std::any::type_name::<Divider>()`，得到完整路径字符串。
2. `Divider::id()` 返回 `ComponentId(该字符串)`。
3. 注册时该字符串成为 `HashMap<ComponentId, ComponentMetadata>` 的键。
4. 预览应用用 `id()` 做**页面跳转与持久化**——`PreviewPage::Component(component.id())` 记录「当前在看哪个组件」。
5. 预览应用用 `scopeless_name()` 做**标题显示与过滤高亮**——侧栏和详情页标题只显示最后一段 `Divider`，过滤器也按它匹配。

分工可以记成一句话：**`id` 给机器用（全路径、求唯一），`scopeless_name` 给人看（短名、求可读）**。

#### 4.2.3 源码精读

`ComponentId` 的定义——一个极简 newtype：

[src/component.rs:L106-L107](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L106-L107)

```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ComponentId(pub &'static str);
```

派生链的两级默认值：

[src/component.rs:L171-L177](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L171-L177)

```rust
    /// The component's unique identifier.
    ///
    /// Used to access previews, or state for more
    /// complex, stateful components.
    fn id() -> ComponentId {
        ComponentId(Self::name())
    }
```

[src/component.rs:L196-L202](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L196-L202)

```rust
    /// The name of the component.
    ///
    /// This name is used to identify the component
    /// and is usually derived from the component's type.
    fn name() -> &'static str {
        std::any::type_name::<Self>()
    }
```

注意 `id()` 的 doc：除了访问预览，还能用于「更复杂的有状态组件」存取状态——预览应用正是用 `id` 作为 SQLite 里找回上次浏览组件的依据（u4-l3 详讲）。

展示端的去前缀逻辑：

[src/component.rs:L145-L153](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L145-L153)

```rust
    pub fn scopeless_name(&self) -> SharedString {
        self.name
            .clone()
            .split("::")
            .last()
            .unwrap_or(&self.name)
            .to_string()
            .into()
    }
```

消费点在预览应用里（标题与过滤用短名、页面身份用 id）：

[component_preview.rs:L442-L445](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L442-L445)（侧栏条目取名）

```rust
        let name = component.scopeless_name();
```

[component_preview.rs:L962-L968](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L962-L968)（详情页标题用 `Headline`，描述用小号 `Label`）

```rust
                                Headline::new(self.component.scopeless_name())
            .child(Label::new(self.component.description()).size(LabelSize::Small))
```

[component_preview.rs:L342-L347](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L342-L347)（页面身份比较用 `id()`）

```rust
                .any(|component| component.id() == *component_id);
                    self.set_active_page(PreviewPage::Component(first_component.id()), cx);
```

#### 4.2.4 代码实践

**实践目标**：验证「`id()` 在整个 Zed 代码库里从不被覆写」以及「`type_name` 输出完整路径」两个事实。

**操作步骤**：

1. 在仓库根目录执行 `git grep -n "fn id() -> ComponentId" -- crates`。
2. 再执行 `git grep -n "ComponentId(" -- crates | grep -v component/src`，看有没有业务代码手工构造 `ComponentId`。
3. 写一个三行的临时 Rust 程序（或在任何已有测试里临时加一行 `println!("{}", std::any::type_name::<ui::Divider>());`）观察输出。

**需要观察的现象**：第 1 步只应命中 `component.rs` 里的默认实现一处；第 3 步输出形如 `ui::components::divider::Divider` 的完整路径。

**预期结果**：全仓库无人覆写 `id()`——完整类型路径已经保证唯一，覆写它反而是制造冲突风险的机会。第 3 步的具体字符串待本地验证（`type_name` 的输出格式不承诺跨编译器稳定，但同一工具链内确定）。

#### 4.2.5 小练习与答案

**练习 1**：假如两个不同 crate 里各有一个都叫 `components::Divider` 的类型，它们的默认 `id` 会冲突吗？

**答案**：不会。`std::any::type_name` 输出的是**以 crate 名开头的完整路径**（如 `ui::components::divider::Divider` 与 `other_crate::components::Divider`），两个不同 crate 的完整路径必然不同。真正会冲突的场景是：同一个类型用泛型参数区分（`Foo<A>` 与 `Foo<B>` 的 `type_name` 不同，天然不冲突）或者有人手工覆写 `id()`/`name()` 返回了相同字符串——此时 `register_component` 里 `HashMap::insert` 会以后注册者覆盖先注册者。

**练习 2**：为什么 `scopeless_name()` 里需要 `unwrap_or(&self.name)`？`split("::").last()` 什么时候会失败到需要它？

**答案**：`split` 迭代器对任何输入至少产生一个片段，`last()` 理论上总返回 `Some`；`unwrap_or` 是防御式写法，保证未来即使重构（比如改用其他切分方式）也不会 panic——这符合仓库 CLAUDE.md「避免可能 panic 的操作」的准则。另外注意它对 `Foo::Bar` 取 `Bar`，但如果名字不含 `::`（有人覆写了 `name()` 返回短名），就原样返回。

### 4.3 七个方法逐方法精读：默认实现与覆写时机

#### 4.3.1 概念说明

把七个方法放进一张表（签名省略 `fn` 前缀）：

| # | 方法 | 返回类型 | 默认实现 | 必须实现吗 | 典型覆写时机 |
| --- | --- | --- | --- | --- | --- |
| 1 | `id()` | `ComponentId` | `ComponentId(Self::name())` | 否 | 几乎从不（全仓库零覆写） |
| 2 | `scope()` | `ComponentScope` | `ComponentScope::None` | 否 | **强烈建议**：决定左侧导航分组；不写则落入「Unsorted」尾部分组 |
| 3 | `status()` | `ComponentStatus` | `ComponentStatus::Live` | 否 | 设计治理标记：还在设计中 / 可开工 / 已废弃 |
| 4 | `name()` | `&'static str` | `std::any::type_name::<Self>()` | 否 | 想让显示名与类型路径解耦（如去掉泛型参数的噪音） |
| 5 | `sort_name()` | `&'static str` | `Self::name()` | 否 | 家族组件聚组排序（Button/IconButton 排在一起） |
| 6 | `description()` | `&'static str` | **无** | **是** | 总是要写；可配合 `Documented` 派生直接取 doc 注释 |
| 7 | `preview(window, cx)` | `AnyElement` | **无** | **是** | 总是要写；返回组件的演示元素树 |

记忆口诀：**「两必须（description、preview），一强烈（scope），四随意（id、status、name、sort_name）」**。

#### 4.3.2 核心流程

为组件选方法时的决策顺序：

1. 写 `impl Component for X`，编译器立刻要求补上 `description` 和 `preview`——这是语法强制的最小集。
2. 问「这个组件属于哪个分区？」→ 覆写 `scope()`，从 15 个枚举值里选。
3. 问「它处于设计生命周期的哪一站？」→ 大多数保持默认 `Live`，不成熟时标 `WorkInProgress`。
4. 问「排序时它该跟谁做邻居？」→ 若按类型名排序已经合适就什么都不做；若想把家族排在一起，覆写 `sort_name()` 加前缀。
5. `id()` 与 `name()` 仅在特殊需求（如自定义稳定标识）下才动。

注意排序的真实生效点在预览应用里，两个排序入口都用 `sort_name()`：

[component_preview.rs:L276-L276](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L276-L276)

```rust
            components.sort_by_key(|(c, _)| c.sort_name());
```

而 `name()`（经 `scopeless_name`）决定侧栏与详情页显示的文字、并参与过滤匹配（[component_preview.rs:L201-L203](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L201-L203) 用 `name()` 与 `description()` 的小写形式做大小写不敏感匹配）。**一句话总结：`name` 管「显示成什么、搜不搜得到」，`sort_name` 管「排在哪」，两者互不干涉。**

#### 4.3.3 源码精读

**`scope()`——默认无分类**：

[src/component.rs:L178-L184](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L178-L184)

```rust
    /// Returns the scope of the component.
    ///
    /// This scope is used to determine how components and
    /// their previews are displayed and organized.
    fn scope() -> ComponentScope {
        ComponentScope::None
    }
```

`ComponentScope::None` 经 strum 序列化为 `"Unsorted"`（见 [src/component.rs:L314-L315](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L314-L315)），预览界面把它排在导航末尾——这是「没分类」的惩罚位，促使作者老实选 scope。15 个枚举值的语义在 u2-l5 专讲。

**`status()`——默认上线状态**：

[src/component.rs:L185-L195](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L185-L195)

```rust
    /// The ready status of this component.
    ///
    /// Use this to mark when components are:
    /// - `WorkInProgress`: Still being designed or are partially implemented.
    /// - `EngineeringReady`: Ready to be implemented.
    /// - `Deprecated`: No longer recommended for use.
    ///
    /// Defaults to [`Live`](ComponentStatus::Live).
    fn status() -> ComponentStatus {
        ComponentStatus::Live
    }
```

**`sort_name()`——最有故事的方法**。doc 注释直接给了命名方案：

[src/component.rs:L203-L218](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L203-L218)

```rust
    /// Returns a name that the component should be sorted by.
    ///
    /// Implement this if the component should be sorted in an alternate order than its name.
    ///
    /// Example:
    /// - Button      -> ButtonA
    /// - IconButton  -> ButtonBIcon
    /// - ToggleButton -> ButtonCToggle
    fn sort_name() -> &'static str {
        Self::name()
    }
```

真实代码里的落地（注意与 doc 略有出入，实际值更简洁）——`Button` 返回 `"ButtonA"`：

[button.rs:L515-L526](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/button.rs#L515-L526)

```rust
impl Component for Button {
    fn scope() -> ComponentScope {
        ComponentScope::Input
    }

    fn sort_name() -> &'static str {
        "ButtonA"
    }

    fn description() -> &'static str {
        "A button triggers an event or action."
    }
```

`IconButton` 返回 `"ButtonB"`（[icon_button.rs:L278-L280](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/icon_button.rs#L278-L280)），于是「Forms & Input」分组里三个按钮族组件按 `ButtonA < ButtonB < ButtonC…` 的字典序紧挨着排列。`toggle_button.rs`、`button_like.rs`、`onboarding/theme_preview.rs` 等处也有同样的覆写。

**`description()`——两个必须实现的方法之一**，doc 里还提示了用 `Documented` 派生把 doc 注释变成描述的捷径：

[src/component.rs:L219-L242](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L219-L242)

```rust
    /// An optional description of the component.
    ///
    /// This will be displayed in the component's preview. To show a
    /// component's doc comment as it's description, derive `Documented`.
    /// ...
    fn description() -> &'static str;
```

无默认实现、签名以分号结尾——不写就编译不过。`Documented`（来自 `documented` crate）为类型生成 `Self::DOCS` 常量，`ui::component_prelude` 已把它重导出给组件作者。

**`preview()` 的完整签名与 doc 建议**（返回值细节在 4.4 展开）：

[src/component.rs:L243-L257](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L243-L257)

```rust
    /// The component's preview.
    ///
    /// An element returned here will be shown in the component's preview.
    ///
    /// Useful component helpers:
    /// - [`component::single_example`]
    /// - [`component::component_group`]
    /// - [`component::component_group_with_title`]
    ///
    /// Note: Any arbitrary element can be returned here.
    /// ...
    fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement;
```

一个批判性阅读的发现：doc 里列的辅助函数名 `component::component_group` 与实际导出名不符——`component_layout.rs` 里真正的函数是 `example_group`（[component_layout.rs:L196-L200](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L196-L200)，`single_example` 与 `example_group_with_title` 则一致）。源码阅读时要养成「doc 注释只是路标，以真实导出为准」的习惯。

最后看一个「合格公民」的完整样貌——`Divider` 只覆写了三个方法：

[divider.rs:L162-L171](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L162-L171)

```rust
impl Component for Divider {
    fn scope() -> ComponentScope {
        ComponentScope::Layout
    }

    fn description() -> &'static str {
        "Visual separator used to create divisions between groups of content \
        or sections in a layout."
    }
```

#### 4.3.4 代码实践

**实践目标**：写出官方文档示例的完整版——一个仅实现 `description` 与 `preview` 的最小组件，并逐个标注其余五个方法走了什么默认实现、返回什么值。

官方最小示例出自派生宏的 doc 注释：

[ui_macros.rs:L19-L39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/ui_macros.rs#L19-L39)

```rust
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
```

**操作步骤**：

1. 在本地克隆中打开 `crates/component_preview/examples/component_preview.rs`，在 `fn main()` 之前粘贴以下**示例代码**（该 example 已依赖 `ui` 与 `component_preview`，而 `component::init()` 就在 main 里第 [L32](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L32) 行，注册链路天然打通）：

   ```rust
   // 示例代码：练习用，不要提交
   use ui::prelude::*; // Component, RegisterComponent, div, AnyElement, Window, App, IntoElement …

   #[derive(RegisterComponent)]
   struct MyComponent;

   impl Component for MyComponent {
       // 必须实现 #1：描述
       fn description() -> &'static str {
           "My component description"
       }

       // 必须实现 #2：预览
       fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement {
           div().into_any_element()
       }
   }
   ```

   `ui::prelude` 里确实重导出了全部所需符号（[prelude.rs:L10-L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/prelude.rs#L10-L13)）。
2. 写一张「默认值清单」 annotating 五个未覆写方法（答案见下方预期结果）。
3. 运行 `cargo run -p component_preview --example component_preview`（完整观察任务在综合实践中进行）。

**需要观察的现象**：编译通过（说明最小集确实只有两个方法）；`MyComponent` 出现在预览界面。

**预期结果**——默认值清单：

| 未覆写方法 | 实际返回值 | 推导路径 |
| --- | --- | --- |
| `id()` | `ComponentId("component_preview::MyComponent")` 之类 | `type_name` 前缀是定义处的 crate/模块路径（example 二进制内的确切前缀待本地验证） |
| `scope()` | `ComponentScope::None` | 默认实现 → 界面落入「Unsorted」尾部分组 |
| `status()` | `ComponentStatus::Live` | 默认实现 → 无特殊徽章 |
| `name()` | 与 `type_name::<MyComponent>()` 相同的完整路径 | 默认实现 |
| `sort_name()` | 等于 `name()` | 默认实现调 `Self::name()` |

#### 4.3.5 小练习与答案

**练习 1**：回答 4.1 实践里留下的问题——`sorted_previews`（[src/component.rs:L76-L80](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L76-L80)）按 `name()` 排序，而预览应用按 `sort_name()` 排序，这两处不一致是 bug 吗？

**答案**：不是 bug，而是两套排序服务不同用户。注册表的 `sorted_previews`/`sorted_components` 是通用 API，按真实名称排序合理；预览应用的 `sort_name()` 排序是**给设计师看目录**的专用策略（家族聚组）。副作用是：如果直接用注册表的 `sorted_components()` 渲染列表，`ButtonA` 这类排序名会失去聚组效果——所以预览应用自己重新排了一遍。这提醒我们 `sort_name` 是「表现层策略」而非「本质属性」。

**练习 2**：`Button` 的 `sort_name()` 是 `"ButtonA"`，它显示在界面上的名字还是 `Button` 吗？

**答案**：是。`sort_name` 只进 `sort_by_key`，从不参与显示；侧栏与标题渲染走 `scopeless_name()`（由 `name()` 派生）。所以排序键可以放心地变成 `"ButtonA"` 而不污染用户可见文字——这正是把两者拆成两个方法的价值。

**练习 3**：如果不实现 `scope()`，组件会怎样？

**答案**：`scope()` 返回 `None`，strum 显示为 `"Unsorted"`，预览导航把它归入末尾的 Uncategized/Unsorted 分组，与 `Utilities`、`Typography` 等正规分组并列但排最后。功能上无损失，但目录组织上是「未完成状态」。

### 4.4 preview 函数签名：为什么是 `fn(&mut Window, &mut App) -> AnyElement`

#### 4.4.1 概念说明

`preview` 是七个方法里唯一「有参数、返回复杂值」的，值得单独拆解：

```rust
fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement;
```

三个组成部分各有含义：

- **`_window: &mut Window`**：窗口句柄。构造某些元素需要窗口级信息（当前焦点、鼠标状态、屏幕缩放等）。大多数简单组件用不上，所以示例里参数名带下划线。
- **`_cx: &mut App`**：应用上下文。读取全局状态、主题、创建实体（`cx.new(...)`）都要经过它。有状态预览（如带开关的演示）会真正用到它。
- **返回 `AnyElement`**：类型擦除后的任意元素。这是本签名的灵魂——注册表不可能为每种返回类型（`Div`、`Flex`、`UniformList`……）各存一种字段，`AnyElement` 把它们统一成一个可存储、可传递的盒子。doc 也明说「Note: Any arbitrary element can be returned here」，你可以返回打开弹窗的按钮、悬停出提示的图标、图标总览网格，而不必是组件自身的类型。

还有一层容易忽略的时序含义：`preview` **注册时不执行**（只存函数指针），**每次渲染时执行**。这意味着它内部每次都可以拿到当时的 `window`/`cx` 重新构造元素树——有状态演示因此在切换页面再回来时能重建（配合预览应用的 `reset_key` 机制，见 u4-l1）。

#### 4.4.2 核心流程

`preview` 从定义到上屏的旅程：

1. 组件作者实现 `fn preview(window, cx) -> AnyElement`，内部通常用 `single_example`/`example_group_with_title` 组织若干变体，最后 `.into_any_element()`。
2. 注册时 `register_component` 把 `T::preview` 作为函数指针存进 `ComponentMetadata.preview` 字段（字段类型 `fn(&mut Window, &mut App) -> AnyElement`）。
3. 预览应用渲染组件页时，取出函数指针并**以当时的 window/cx 调用**，得到 `AnyElement`。
4. 元素树进入 GPUI 布局与绘制流程。

用一次延迟求值来理解：注册表像一张「菜单」，每道菜只登记了厨师（函数指针）；点单（渲染）时才真正下厨。

#### 4.4.3 源码精读

函数指针字段的声明与取回：

[src/component.rs:L114-L114](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L114-L114)

```rust
    preview: fn(&mut Window, &mut App) -> AnyElement,
```

[src/component.rs:L133-L135](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L133-L135)

```rust
    pub fn preview(&self) -> fn(&mut Window, &mut App) -> AnyElement {
        self.preview
    }
```

访问器返回的仍是函数本身——调用时机完全交给渲染方。

一个真实 `preview` 的骨架（`Divider`，节选）：

[divider.rs:L172-L190](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L190)

```rust
    fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement {
        v_flex()
            .gap_6()
            .children(vec![
                example_group_with_title(
                    "Horizontal Dividers",
                    vec![
                        single_example("Default", Divider::horizontal().into_any_element()),
                        // …Border Color / Inset / Dashed 三个变体
                    ],
                ),
                // …Vertical Dividers、Example Usage 两组
            ])
            .into_any_element()
    }
```

注意三个细节：参数带下划线（这个预览不需要窗口与应用状态）；内部**直接调用 `Divider::horizontal()` 等关联构造函数凭空造实例**（印证「无需 self」的设计）；最外层 `v_flex().into_any_element()` 收尾，把具体类型擦除成 `AnyElement`。`Divider` 完整的 preview 还有第三组「Example Usage」展示真实用法（[divider.rs:L220-L235](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L220-L235)）。

#### 4.4.4 代码实践

**实践目标**：纸面精读一个真实 `preview`，把签名里的三个组成部分与代码一一对应。

**操作步骤**：

1. 打开 [divider.rs:L172-L238](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L238)，在纸上画一棵元素树：根是 `v_flex`，三个子节点是三个 `example_group_with_title`，每个分组下是若干 `single_example` 卡片。
2. 标注：哪里体现了「参数未被使用」（两个 `_` 前缀）；哪里体现了「任意元素」（`into_any_element()` 出现了多少次、各自擦除了什么类型）。
3. 再对比一个**使用** `cx` 的预览：`git grep -n "fn preview(window" -- crates/ui/src`，任选一处（如 `Icon` 或含开关的组件），看它拿 `cx` 做了什么（通常是 `cx.new(|cx| ...)` 建状态实体）。

**需要观察的现象**：`Divider::preview` 里 `into_any_element()` 出现约 13 次——每个变体卡片都各自擦除一次，最外层再擦除一次。

**预期结果**：能口头回答「`preview` 的两个参数什么时候才真正需要」——当演示要感知窗口（焦点/悬停）或要创建可变状态（`cx.new`）时。第 3 步的具体命中文件待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `preview` 不设计成 `fn preview(&self, window, cx) -> AnyElement`？

**答案**：三个理由：① 注册发生在类型层面，没有实例可传；② 元数据里存的是**裸函数指针** `fn(&mut Window, &mut App) -> AnyElement`，带 `&self` 的方法无法这样存（需要连同接收者的闭包才行）；③ 预览演示本来就要求「凭空造出示例」，`Divider::horizontal()` 这类关联构造函数比实例字段更通用。

**练习 2**：`AnyElement` 和 `impl IntoElement` 作为返回类型，各自适合什么场景？

**答案**：`impl IntoElement` 适合「向直接调用方返回具体但不想写名的类型」（如 `Divider::render` 的返回值，调用方马上就用）；`AnyElement` 适合「异构集合与延迟存储」——注册表字段、`Vec` 里混装不同元素、跨时间传递。代价是擦除后失去具体类型信息，只能统一布局绘制。`preview` 恰好三种需求全占（存进字段、隔一段时间调用、每个组件返回的树形形色色），所以选 `AnyElement`。

**练习 3**：`Component` trait 的 doc 说 preview 可以返回「a button that opens a modal」——这暗示 preview 的元素具备什么能力？

**答案**：暗示返回的不只是静态展示，而是**完整可交互的元素**：元素可以注册事件处理器、持有实体状态、在用户操作时更新。因为 `preview` 每次渲染都拿最新的 `window`/`cx` 重新执行，交互闭环（点击 → 状态更新 → 重渲染）与普通 GPUI 视图无异——组件预览因此可以当「活的样板间」用。

## 5. 综合实践

把本讲全部知识串成一个实验：**让一个最小组件在预览界面里「搬家」两次**。

**任务设计**：

1. **第一阶段（默认形态）**：按 4.3.4 的步骤，把只实现 `description` 与 `preview` 的 `MyComponent` 放进 `crates/component_preview/examples/component_preview.rs`（本地实验，勿提交），`preview` 里放两个分组：

   ```rust
   // 示例代码：练习用
   fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement {
       v_flex()
           .gap_6()
           .child(example_group_with_title(
               "Variants",
               vec![
                   single_example("Simple", div().h_16().w_full().into_any_element()),
                   empty_example("Empty"),
               ],
           ))
           .into_any_element()
   }
   ```

   （`v_flex`、`example_group_with_title` 等同样来自 `ui::prelude`；`empty_example` 语义是「合法的空渲染」，详见 u3-l2。）
2. 运行 `cargo run -p component_preview --example component_preview`，记录三件事：它出现在哪个导航分组（应为「Unsorted」末尾）、详情页标题显示什么（应为 `MyComponent`，即 `scopeless_name`）、描述文字显示在哪（标题下方的小号 Label）。
3. **第二阶段（覆写 name 与 sort_name）**：为它追加两个方法并重跑：

   ```rust
   // 示例代码：练习用
   fn name() -> &'static str {
       "Practice::MyFirstWidget"
   }
   fn sort_name() -> &'static str {
   }
   ```

   先预测再验证：`name()` 改变后，侧栏标题、详情页 `Headline`、以及**页面身份 `id()`**（因为它默认取 `name()`）各会发生什么？过滤框里输入 `widget` 还能搜到它吗（注意过滤匹配的是小写化后的 name/scope/description，见 [component_preview.rs:L201-L203](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L201-L203)）？`scopeless_name` 会显示 `MyFirstWidget` 还是完整串？`sort_name()` 覆写后它在该分组内的排位如何变化（对照 `Button` 的 `"ButtonA"` 手法）？
4. **第三阶段（补上 scope）**：再覆写 `scope()` 返回 `ComponentScope::Layout`，确认它从「Unsorted」搬进「Layout & Structure」分组，且分组内排序仍由 `sort_name()` 决定。

**验收标准**：每个阶段先写下预测、再运行对照；最终你能不看笔记回答——`name` 影响侧栏文字/详情标题/过滤匹配/id 派生，`sort_name` 只影响分组内排序，`scope` 决定进哪个分组，`id` 默认等于 `name`，`status` 默认 `Live`。若某步现象与预期不符，回到 4.2/4.3 的源码引用处找原因。运行结果待本地验证。

## 6. 本讲小结

- `Component` 是**类型级契约**：七个方法全是无 `self` 的关联函数，注册时各求值一次后被「冷冻」进 `ComponentMetadata`，`preview` 更是只存函数指针、渲染时才调用。
- 默认值派生链：`name()` 默认 `std::any::type_name::<Self>()`（完整路径，保唯一），`id()` 默认 `ComponentId(Self::name())`；界面显示用 `scopeless_name()` 去掉路径前缀，页面身份与持久化用 `id()`。
- 七方法口诀「两必须（description、preview）、一强烈（scope）、四随意（id、status、name、sort_name）」；`id()` 在全仓库零覆写，`sort_name()` 的典型用法是 Button/IconButton 式家族聚组（`"ButtonA"`/`"ButtonB"`）。
- `name` 管「显示与搜索」，`sort_name` 管「排序位置」，两者解耦使排序键可以为了聚组而「变形」而不污染用户可见文字。
- `preview` 签名 `fn(&mut Window, &mut App) -> AnyElement`：两个参数服务有状态/感知窗口的演示，`AnyElement` 类型擦除让任意元素树可存进注册表字段；`Divider::preview` 展示了「变体分组 + 真实用法」的标准组织法。
- 源码阅读教训：trait doc 里列的 `component::component_group` 与实际导出 `example_group` 不符——doc 是路标，不是法律。

## 7. 下一步学习建议

- **下一讲 u2-l2（注册表与元数据）**：本讲反复出现的 `ComponentMetadata` 与 `COMPONENT_DATA` 将被完整解剖——`ComponentRegistry` 的查询/排序 API、`LazyLock<RwLock<...>>` 全局态的读写时机、`scopeless_name` 的去前缀技巧在 `component_map`/`get` 等 API 中的位置。
- 若你想先巩固本讲：重读 [src/component.rs:L160-L258](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L160-L258) 的 trait 定义并逐行核对 4.3 的表格；再挑 `crates/ui/src/components/` 下任意两个组件的 `impl Component` 块，比赛谁能更快说出「它覆写了哪几个方法、为什么」。
- 对 `ComponentScope` 15 个枚举与 `ComponentStatus` 四种状态的治理语义感兴趣，可跳读 u2-l5；对 `preview` 里 `single_example`/`example_group_with_title` 的布局细节感兴趣，u3-l1 与 u3-l2 是专门篇目。
