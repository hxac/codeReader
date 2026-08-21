# 运行与预览：通过组件预览系统看到 InputField

## 1. 本讲目标

上一讲（u1-l1）我们只读了代码，知道了 `InputField` 是「外壳 + `Arc<dyn ErasedEditor>` 内芯」的表单组件。本讲要解决一个更朴素的问题：**怎么亲眼看到它长什么样？**

Zed 为此内置了一套「组件预览系统」：任何实现了 `Component` trait 的 UI 组件，都会自动出现在一个可视化面板里，供设计师和工程师查看、调试、文档化。学完本讲，你应该能够：

1. 使用 `workspace: open component preview` 命令（或独立 example）打开组件预览面板，找到 `InputField` 的实际渲染效果。
2. 说明 `Component` trait 的 `preview` 方法与 `ComponentScope::Input` 分类各自的作用。
3. 理解 `example_group` / `single_example` 布局助手如何组织预览内容（屏幕上每个「示例卡片」对应哪行代码）。
4. 了解 `component::init` 与 `inventory` 分布式注册的基本流程：为什么 `InputField` 没有被任何人显式 import，却能出现在面板里。

## 2. 前置知识

本讲是 beginner 级别，只需要上一讲的概念加上下面几个新名词：

- **trait 的静态方法（关联函数）**：Rust 的 trait 方法可以没有 `self` 参数，例如 `fn preview(window: &mut Window, cx: &mut App) -> AnyElement`。调用时写作 `InputField::preview(window, cx)`，不需要先有实例。`Component` trait 的全部方法都是这种静态方法，因为「登记组件信息」是类型层面的知识，不依赖某个具体实例。
- **`AnyElement`**：GPUI 中「擦除了具体类型的元素」。`preview` 要返回各种五花八门的元素树（按钮组、输入框、图标墙……），但函数签名只有一个，于是统一装进 `AnyElement` 这个盒子。这与上一讲 `Arc<dyn ErasedEditor>` 擦除 `Editor` 类型是同一思想。
- **`Entity<T>` 与 `cx.new`**：`Entity<T>` 是 GPUI 对状态 `T` 的句柄（上一讲提过）；`cx.new(|cx| ...)` 用来在应用上下文里创建一个新实体。若 `T` 实现了 `Render`，则 `Entity<T>` 可以直接作为元素的 child 渲染。
- **derive 宏**：`#[derive(X)]` 会让编译器在编译期为类型额外生成代码。本讲的 `#[derive(RegisterComponent)]` 就会生成「把组件登记进全局注册表」的代码。
- **`inventory` crate**：一个实现「分布式注册」的 Rust 库。它借助链接器的特殊 section，把散落在各个 crate 里的注册项收集起来，运行时可以用 `inventory::iter::<T>()` 一次性遍历。你可以把它理解成「编译期自动汇总的全局清单」，不需要任何人手工维护一份中心列表。
- **Zed 的 action 与命令面板**：Zed 把用户可触发的操作定义成 action（如 `OpenComponentPreview`），用户通过命令面板搜索触发。命令面板默认快捷键是 `cmd-shift-p`（macOS，见 [assets/keymaps/default-macos.json:L747](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/assets/keymaps/default-macos.json#L747)）或 `ctrl-shift-p` / `f1`（Windows/Linux）。

## 3. 本讲源码地图

本讲涉及的文件分三层：被预览的组件、预览基础设施、预览面板（消费者）。

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/input_field.rs`（ui_input crate） | 被预览的组件 | `impl Component for InputField`：`scope` / `description` / `preview` |
| `../component/src/component.rs` | 预览基础设施 | `Component` trait、`ComponentRegistry`、`component::init`、`ComponentScope` |
| `../component/src/component_layout.rs` | 预览基础设施 | `single_example` / `example_group` 布局助手 |
| `../ui_macros/src/derive_register_component.rs` | 预览基础设施 | `#[derive(RegisterComponent)]` 生成的注册代码 |
| `../component_preview/src/component_preview.rs` | 预览面板 | 面板实体、打开命令、按 scope 分组、调用 `preview()` |
| `../component_preview/examples/component_preview.rs` | 独立预览入口 | 不启动完整 Zed 也能打开面板的最小程序 |
| `../workspace/src/workspace.rs` | 上游 | `OpenComponentPreview` action 定义；`workspace::init` 调用 `component::init` |

阅读建议：先读 4.1（组件怎么「自报家门」），再读 4.2（家门信息怎么进注册表），然后 4.3（预览内容怎么排版），最后 4.4（面板怎么消费这一切）。

## 4. 核心概念与源码讲解

### 4.1 Component trait 与 preview 方法

#### 4.1.1 概念说明

`Component` trait 是 component crate 定义的组件「身份证」：一个 UI 类型实现了它，就等于声明「我愿意出现在组件预览面板里，我的长这样、属于这个分区、处于这个状态」。

trait 的方法分成两类：

| 方法 | 是否必须实现 | 作用 |
| --- | --- | --- |
| `id()` | 默认 | 组件唯一标识，默认等于 `name()` |
| `scope()` | 默认（`ComponentScope::None`） | 所属分区，决定在面板中如何分组展示 |
| `status()` | 默认（`ComponentStatus::Live`） | 就绪状态：`Live` / `WorkInProgress` / `EngineeringReady` / `Deprecated` |
| `name()` | 默认 | 组件名，默认取类型的完整路径（`std::any::type_name`） |
| `sort_name()` | 默认 | 排序用的名字，用于把相关组件排在一起 |
| `description()` | **必须** | 一句话描述，显示在预览页头部 |
| `preview()` | **必须** | 返回一个 `AnyElement`，即组件的演示内容 |

注意 `preview` 的签名：`fn preview(window: &mut Window, cx: &mut App) -> AnyElement`——没有 `self`。面板并不需要组件实例；它拿到的是一个「如何现场搭出演示」的函数，等到渲染那一刻才调用。

#### 4.1.2 核心流程

一个组件从「实现 trait」到「出现在面板」的静态结构是这样的：

```text
impl Component for InputField
        │  提供 scope()/description()/preview()
        ▼
#[derive(RegisterComponent)]          ← 编译期生成注册代码（4.2 详述）
        │
        ▼
ComponentMetadata { id, name, scope, status, description,
                    preview: fn(&mut Window, &mut App) -> AnyElement }
        │  metadata 里只存一个函数指针，不存任何实例
        ▼
面板渲染时调用 (metadata.preview())(window, cx) → AnyElement → 嵌入面板元素树
```

#### 4.1.3 源码精读

先看 trait 本体。`Component` trait 的文档注释直白地说明了它的用途——用于可视化测试与调试，并建议至少实现 `scope` 和 `preview`：

- [../component/src/component.rs:L170-L258](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L170-L258)：`Component` trait 的完整定义。前五个方法（`id`/`scope`/`status`/`name`/`sort_name`）都有默认实现，最后两个（`description`/`preview`）没有函数体，是必须实现的。
- [../component/src/component.rs:L243-L257](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L243-L257)：`preview` 方法。文档明确指出「这里可以返回任意元素」，并推荐使用 `single_example` / `component_group` 等助手。

再看 `InputField` 的实现——只覆盖了 `scope`、`description`、`preview` 三项，其余全部吃默认值：

- [src/input_field.rs:L237-L240](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L237-L240)：`InputField` 把 `scope()` 覆盖为 `ComponentScope::Input`。
- [src/input_field.rs:L242-L246](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L242-L246)：`description()` 返回静态字符串，描述这是一个支持标签、占位符、前导图标和掩码的单行输入框。

`ComponentScope::Input` 为什么在屏幕上显示为 "Forms & Input"？因为枚举用了 `strum` 的 `serialize` 属性做展示名映射：

- [../component/src/component.rs:L298-L325](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L298-L325)：`ComponentScope` 枚举全集（Agent、Editor、Layout……）。
- [../component/src/component.rs:L306-L307](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L306-L307)：`Input` 变体被标注为 `#[strum(serialize = "Forms & Input")]`，调用 `scope().to_string()` 时得到的就是 "Forms & Input"。

另外注意结构体上的派生：

- [src/input_field.rs:L20-L21](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L20-L21)：`#[derive(RegisterComponent)]` 标在 `pub struct InputField` 上。这个 derive 由 `ui::prelude` 转发出来（[../ui/src/prelude.rs:L13](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui/src/prelude.rs#L13)），所以 `input_field.rs` 顶部 `use ui::prelude::*` 之后无需额外 import。它的展开结果是 4.2 的主角。

#### 4.1.4 代码实践

**实践目标**：用纯阅读的方式，确认 `InputField` 对 `Component` trait 的实现是「最小实现」。

**操作步骤**：

1. 打开 [../component/src/component.rs:L170-L258](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L170-L258)，把 7 个方法抄成两列清单：默认实现 / 必须实现。
2. 打开 [src/input_field.rs:L237-L271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L237-L271)，对照清单标出 `InputField` 只覆盖了哪三个。
3. 在仓库根目录执行下面的命令，看看还有哪些组件同样声明在 "Forms & Input" 分区：

```bash
git grep -n "ComponentScope::Input" -- 'crates/**/src/**/*.rs'
```

**需要观察的现象**：第 3 步的输出里，除了 `ui_input` 的 `input_field.rs`，还会命中若干其他 crate（例如 ui crate 下的一些表单类组件）。

**预期结果**：你会得到一张「同分区组件清单」，并且确认 `InputField` 没有覆盖 `name()`，因此它在面板中显示的名字来自 `std::any::type_name::<InputField>()`，即形如 `ui_input::InputField` 的完整路径；面板侧栏实际只显示 `::` 之后的部分（`InputField`），这一点在 4.4 的 `scopeless_name` 处兑现。

#### 4.1.5 小练习与答案

**练习 1**：`Component` trait 的方法为什么都不带 `self`？如果带 `self` 会有什么问题？

**参考答案**：因为这些信息（名字、分区、描述、如何渲染演示）是类型层面的静态知识，注册表需要在「没有任何实例」的情况下也能列出全部组件。若方法带 `self`，注册表就必须先构造出每个组件的实例才能登记，而很多组件的构造需要 `Window`/`App` 上下文甚至更重的依赖，注册阶段（应用启动早期）未必具备。

**练习 2**：`InputField` 没有实现 `name()`，面板上它的名字是什么？由哪行代码决定？

**参考答案**：默认实现取 `std::any::type_name::<Self>()`，得到 `ui_input::InputField` 这样的完整路径，见 [../component/src/component.rs:L196-L202](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L196-L202)。面板显示时再用 `scopeless_name()` 截取 `::` 之后的部分，得到 `InputField`。

**练习 3**：如果设计师说「InputField 还在改版，别让人在应用里用它」，应该改 trait 的哪个方法？

**参考答案**：覆盖 `status()` 返回 `ComponentStatus::WorkInProgress`。面板会在组件名旁渲染一个状态徽章（`Live` 之外的状态才显示），鼠标悬停能看到解释文案，见 [../component/src/component.rs:L268-L296](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L268-L296) 中四种状态的含义定义。

### 4.2 ComponentRegistry 与注册流程

#### 4.2.1 概念说明

预览面板想列出全仓库上百个组件，但它不可能 `use` 每一个 crate——那会把依赖图搅成一团。解法是经典的**注册表模式（registry pattern）**：

- 每个组件「自己把自己」登记进一张全局表（`ComponentRegistry`）；
- 面板只依赖这张表，不依赖任何具体组件。

而「自己登记自己」的动作由 `inventory` 完成分布式收集：`#[derive(RegisterComponent)]` 在每个组件所在的编译单元里悄悄生成一段注册代码，链接器把它们汇总，运行时 `inventory::iter::<ComponentFn>()` 能一次性遍历所有注册函数。**没有任何中心化的清单文件需要维护**——新增一个组件，只要 derive 一下，它就自动出现在面板里。

#### 4.2.2 核心流程

完整链路按时序排列：

```text
编译期（每个组件一份）:
  #[derive(RegisterComponent)]
    ├─ 生成 fn __component_registry_internal_register_InputField()
    │    └─ 调用 component::register_component::<InputField>()
    ├─ 生成 const 断言: InputField 必须实现 Component trait（否则编译失败）
    └─ inventory::submit! { ComponentFn::new(上述函数) }   ← 进入链接器特殊 section

运行期（应用启动）:
  zed 启动 → workspace::init() → component::init()
    └─ for f in inventory::iter::<ComponentFn>() { (f.0)(); }
         └─ 每个注册函数调用 register_component::<T>()
              └─ 构造 ComponentMetadata { id, name, scope, status,
                    description, preview: T::preview (函数指针) }
              └─ COMPONENT_DATA.write().components.insert(id, metadata)

运行期（用户打开面板）:
  ComponentPreview::new() → components() 克隆注册表 → 分组渲染
```

#### 4.2.3 源码精读

第一步看 derive 宏生成了什么。整个宏不到 30 行：

- [../ui_macros/src/derive_register_component.rs:L13-L27](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_macros/src/derive_register_component.rs#L13-L27)：derive 的全部展开。三个关键片段：
  - L14-L17：一个 `const _: ()` 块里的 `AssertComponent<T: Component>` 幽灵结构体——如果被 derive 的类型没有实现 `Component` trait，这里直接编译报错，把错误提前到编译期；
  - L19-L22：生成形如 `__component_registry_internal_register_InputField` 的注册函数，内部调用 `component::register_component::<InputField>()`；
  - L24-L26：`inventory::submit!` 把这个函数包成 `ComponentFn` 提交给分布式注册表。注意它写的是 `component::__private::inventory`——component crate 把 `inventory` 藏在 `#[doc(hidden)]` 的私有模块里转发给宏使用（[../component/src/component.rs:L41-L45](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L41-L45)），这样使用 derive 的 crate 不必把 `inventory` 声明为自己的依赖。

第二步看运行期谁收集这些注册项：

- [../component/src/component.rs:L31-L39](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L31-L39)：`ComponentFn` 是一个包裹 `fn()` 的新类型，`inventory::collect!(ComponentFn)` 声明「本程序里所有 `submit!` 的 `ComponentFn` 都可以被迭代到」。
- [../component/src/component.rs:L25-L29](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L25-L29)：`component::init()` 只有一件事——遍历 `inventory::iter::<ComponentFn>()` 并逐个调用。这就是「扫描全仓库登记项」的时刻。

第三步看单个组件如何入库：

- [../component/src/component.rs:L47-L61](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L47-L61)：`register_component::<T>()` 把 trait 的静态方法「拍平」成一份 `ComponentMetadata`。注意 L53 的 `preview: T::preview`——存的是**函数指针**，不是调用结果。演示内容是等面板渲染那一刻才现场生成的。
- [../component/src/component.rs:L63-L64](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L63-L64)：全局存储 `COMPONENT_DATA` 是 `LazyLock<RwLock<ComponentRegistry>>`，首次访问时懒初始化，之后读写锁保护并发访问。
- [../component/src/component.rs:L66-L104](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L66-L104)：`ComponentRegistry` 对外只暴露查询接口（`components()` / `sorted_components()` / `get(id)` / `len()`），字段 `components: HashMap<ComponentId, ComponentMetadata>` 是私有的——外界只能读，不能绕过 `register_component` 乱写。

最后看 `component::init()` 的调用时机：

- [../workspace/src/workspace.rs:L785-L786](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/workspace.rs#L785-L786)：`workspace::init` 的第一行就是 `component::init()`。Zed 主程序启动时会初始化 workspace，因此注册表在打开任何窗口之前就已填满。

#### 4.2.4 代码实践

**实践目标**：体感验证「分布式注册」——统计全仓库有多少组件登记了自己，并确认 `ui_input` 没有出现在任何中心化清单里。

**操作步骤**：

1. 在仓库根目录执行：

```bash
git grep -l "#\[derive(RegisterComponent)\]" -- 'crates/**/src/**/*.rs' | wc -l
git grep -l "#\[derive(RegisterComponent)\]" -- 'crates/**/src/**/*.rs' | head -20
```

2. 再执行下面两条，对比结果：

```bash
git grep -n "register_component::<" -- 'crates/**/*.rs' | grep -v ui_macros | grep -v "crates/component/src/component.rs"
git grep -n "InputField" -- crates/component/src/component.rs
```

3. 打开 [../workspace/src/workspace.rs:L785-L786](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/workspace.rs#L785-L786)，确认 `component::init()` 的调用位置。

**需要观察的现象**：第 1 步会命中几十个文件（ui、ui_input、workspace 等许多 crate 都有组件）；第 2 步第一条几乎没有输出（除了宏定义与注册表实现本身，没有人手工调用 `register_component::<...>()`），第二条（在 component crate 里搜 `InputField`）零命中。

**预期结果**：`InputField` 的注册只发生在它自己的文件里（derive 生成），component crate 对它的存在一无所知——这正是分布式注册的含义：依赖方向是「组件 → 注册表」，注册表永远不反向 import 组件。

#### 4.2.5 小练习与答案

**练习 1**：如果给一个没有实现 `Component` 的结构体标上 `#[derive(RegisterComponent)]`，会发生什么？在哪一行被拦下？

**参考答案**：编译失败。derive 生成的 `AssertComponent<T: component::Component>` 幽灵类型会在类型检查时要求 `T: Component`，见 [../ui_macros/src/derive_register_component.rs:L14-L17](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_macros/src/derive_register_component.rs#L14-L17)。错误被挡在编译期而不是运行期。

**练习 2**：`ComponentMetadata` 里存的 `preview` 为什么是函数指针 `fn(&mut Window, &mut App) -> AnyElement`，而不是直接存一个 `AnyElement`？

**参考答案**：`AnyElement` 是已经构建好的元素实例，包含状态；而注册发生在应用启动早期，那时既没有窗口，也不应该为上百个组件预先构建演示。存函数指针等于存一张「图纸」，面板渲染到谁才现场构建谁的演示，既省资源也保证每次拿到新鲜状态。

**练习 3**：新写一个组件 crate，忘了在启动链路上做任何登记，组件会出现在面板吗？需要额外在某处 `use` 它吗？

**参考答案**：只要该 crate 被最终二进制链接进来（在依赖树上），`inventory::submit!` 的注册项就会被链接器保留，`component::init()` 自然能迭代到它，不需要任何中心化 `use`。反之，如果 crate 没被链接（不在依赖树上），链接器会丢弃它的注册项，组件不会出现。

### 4.3 example_group / single_example 布局助手

#### 4.3.1 概念说明

`preview` 可以返回任意元素，但如果每个组件都自由发挥，面板会乱成一团。component crate 在 `component_layout.rs` 里提供了统一的「展示语言」：

- **`single_example`（单示例卡片）**：对应 `ComponentExample` 类型。一张卡片 = 变体名（如 "Small Label (Default)"）+ 可选描述 + 一个带边框、斜线纹理背景的展示框，组件演示就放在框里。
- **`example_group`（示例分组）**：对应 `ComponentExampleGroup` 类型。把若干张卡片纵向排成一列，可选地加一个全大写的分组标题与分隔线。

两者都是 `RenderOnce` 组件（用 `#[derive(IntoElement)]` 派生）：它们只是为了「构造完立刻转成元素」而存在的纯展示类型，没有自己的实体状态，也不需要 `Context<Self>`。这与 `InputField`（有状态、实现 `Render`、由 `Entity` 持有）是两种不同的组件形态。

#### 4.3.2 核心流程

`InputField::preview` 内部的构建流程：

```text
preview(window, cx) 被面板调用
  ├─ cx.new(|cx| InputField::new(window, cx, "placeholder").label("Small Label"))
  │     → 创建第一个 Entity<InputField>（演示实例 1：小号标签）
  ├─ cx.new(|cx| InputField::new(...).label("Regular Label").label_size(LabelSize::Default))
  │     → 创建第二个 Entity<InputField>（演示实例 2：常规标签）
  ├─ single_example("Small Label (Default)", div().child(实例1).into_any_element())
  │     → 包成卡片 A（卡片标题 = 第一个参数）
  ├─ single_example("Regular Label", div().child(实例2).into_any_element())
  │     → 包成卡片 B
  ├─ example_group(vec![卡片 A, 卡片 B])
  │     → 两张卡片排成一列
  └─ v_flex().gap_6().children(...).into_any_element()
        → 外层留出间距，整体擦除类型后返回
```

#### 4.3.3 源码精读

先看被预览方——`InputField::preview` 的完整实现：

- [src/input_field.rs:L248-L271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L248-L271)：`preview` 方法。两个 `cx.new` 创建两个演示实体（L249-L256）；两次 `single_example` 分别命名为 "Small Label (Default)" 与 "Regular Label"（L261-L268）；`example_group` 把它们归为一组（L260-L269）。文件第 1 行就 import 了这两个助手：[src/input_field.rs:L1](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L1)。

再看助手方——卡片和分组各自怎么渲染：

- [../component/src/component_layout.rs:L7-L14](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L7-L14)：`ComponentExample` 的字段：`variant_name`（卡片标题）、`description`（可选副标题）、`element`（演示本体）、`width`（可选固定宽度）。
- [../component/src/component_layout.rs:L30-L47](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L30-L47)：卡片上半部分——`variant_name` 用正文色、1rem 字号渲染在顶部，`description` 存在时以 0.875rem 弱化色跟在下面。**你在面板上看到的 "Small Label (Default)" 字样就是这里的 `self.variant_name`。**
- [../component/src/component_layout.rs:L49-L66](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L49-L66)：卡片下半部分——展示框：最小高度 100px、内边距 `p_8()`、圆角 `rounded_xl()`、1px 边框（半透明），背景是 `pattern_slash(...)` 生成的斜线纹理。演示元素（`self.element`）居中放在框内。斜线底纹是刻意设计：让「组件之外的区域」一眼可辨。
- [../component/src/component_layout.rs:L92-L100](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L92-L100)：`ComponentExampleGroup` 的字段：`title`（可选）、`examples`（卡片列表）、`width`、`grow`、`vertical`。
- [../component/src/component_layout.rs:L102-L150](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L102-L150)：分组的渲染。有 `title` 时渲染一行「大写标题 + 横向分隔线」（L115-L137）；卡片列表纵向排列、间距 `gap_6()`（L138-L147）。
- [../component/src/component_layout.rs:L185-L205](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L185-L205)：三个快捷函数 `single_example` / `example_group` / `example_group_with_title`，就是把构造函数包装成更好读的自由函数。`InputField::preview` 用的正是前两个。

#### 4.3.4 代码实践

**实践目标**：不运行程序，纯靠读码画出 `InputField::preview` 产出的元素树，并锁定「卡片标题文字」的渲染位置。

**操作步骤**：

1. 读 [src/input_field.rs:L248-L271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L248-L271)，在纸上画出如下树并补全 `?` 处的节点：

```text
v_flex (gap_6)
└── ComponentExampleGroup            ← example_group(vec![...])
    └── div (flex_col, gap_6)
        ├── ComponentExample         ← single_example("Small Label (Default)", ...)
        │   ├── div "Small Label (Default)"   ← 卡片标题（哪个文件哪一行渲染它？）
        │   └── div 展示框（min_h 100px, 斜线背景）
        │       └── ?
        └── ComponentExample         ← single_example("Regular Label", ...)
            └── ...
```

2. 回答：第二张卡片里 `div().child(input_regular)` 中的 `input_regular` 是什么类型？GPUI 为什么允许一个 `Entity<InputField>` 直接当 child？
3. 把 [../component/src/component_layout.rs:L60-L64](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L60-L64) 中 `pattern_slash` 的两个 `12.0` 参数改成 `24.0`（仅本地实验，不要提交），重新构建后观察斜线密度变化。

**需要观察的现象**：第 3 步改参后，展示框背景的斜线间距变大（纹理变稀疏）。

**预期结果**：你能准确说出——卡片标题由 `ComponentExample::render` 渲染（[../component/src/component_layout.rs:L35-L38](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L35-L38)）；`input_regular` 是 `Entity<InputField>`，因为 `InputField` 实现了 `Render`，GPUI 为 `Entity<T: Render>` 提供了 `IntoElement` 实现。第 3 步的视觉效果为「待本地验证」（本环境无法运行 GUI）。

#### 4.3.5 小练习与答案

**练习 1**：`single_example` 的第一个参数在屏幕上有几处消费？分别是什么？

**参考答案**：一处——作为 `ComponentExample::variant_name` 渲染在卡片顶部（[../component/src/component_layout.rs:L35-L38](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component_layout.rs#L35-L38)）。它不参与面板搜索；面板搜索匹配的是组件名、scope 和 description（见 4.4）。

**练习 2**：`ComponentExample` 与 `InputField` 都叫「组件」，它们的实现形态有何本质不同？

**参考答案**：`ComponentExample` 实现 `RenderOnce`：构造即用、无实体状态、`render` 拿所有权并接收 `&mut App`；`InputField` 实现 `Render`：由 `Entity<InputField>` 持有状态、可被 `cx.notify()` 触发重渲染、`render` 接收 `&mut Context<Self>`。前者适合纯排版（卡片、分组），后者适合有交互状态的表单控件。

**练习 3**：如果想给 `InputField` 的预览再加一张「带前导图标」的卡片，最少要动哪几行代码？

**参考答案**：在 `preview` 里再 `cx.new` 一个实例并链上 `.start_icon(IconName::Magnifier)` 之类的 builder 方法，再在 `example_group` 的 vec 里追加一个 `single_example("With Start Icon", div().child(新实例).into_any_element())`，全部改动都在 [src/input_field.rs:L248-L271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L248-L271) 内。（`start_icon` builder 的细节是下一讲 u2-l1 的内容。）

### 4.4 component_preview 入口

#### 4.4.1 概念说明

`component_preview` crate 是整套系统的「门面」：它提供一个 workspace 面板（一个标签页形式的 `Item`），布局为：

- **左侧窄栏（231px）**：组件导航，按 `ComponentScope` 分组（"Forms & Input"、"Layout & Structure"……组名按字母序排列），组内按 `sort_name` 排序；
- **右侧内容区**：两种页面——"All Components" 长列表（每组的每个组件内联展示完整预览），或单个组件的详情页（大标题 + scope + 状态徽章 + 描述 + 预览）；
- **顶部过滤框**：输入文字按「组件名 / scope / 描述」过滤。

有个漂亮的细节：**这个过滤框本身就是一个 `Entity<InputField>`**——组件预览系统自己就是 `InputField` 的消费者之一。这也意味着，面板能打开的前提是 `ERASED_EDITOR_FACTORY` 已被 `editor::init` 注入（上一讲 u1-l1 的知识点在这里闭环）。

#### 4.4.2 核心流程

从用户敲命令到看到 `InputField` 的完整时序：

```text
应用启动:
  zed/src/main.rs → workspace::init → component::init()     ← 注册表填满
  zed/src/main.rs → component_preview::init()               ← 注册打开面板的 action

用户操作:
  cmd/ctrl-shift-p 打开命令面板 → 输入 "component"
  → 选择 "workspace: open component preview"（action: workspace::OpenComponentPreview）
  → observe_new 回调里注册的处理器执行
  → ComponentPreview::new(...)
       ├─ components() 克隆全局注册表
       ├─ sorted_components() 按名字排序
       ├─ cx.new(|cx| InputField::new(window, cx, "Find components or usages…"))
       │    ← 顶部过滤框：InputField 的真实消费者！
       └─ scope_ordered_entries() 按 scope 分组、按 sort_name 组内排序

渲染:
  详情页: ComponentPreviewPage::render_preview
       └─ (component.preview())(window, cx)   ← 现场调用 4.1 的静态方法
            └─ 进入 InputField::preview → 4.3 的 example_group/single_example
```

#### 4.4.3 源码精读

- [../workspace/src/workspace.rs:L307-L308](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/workspace.rs#L307-L308)：`OpenComponentPreview` action 在 workspace crate 的 `actions!` 宏列表中定义，文档注释 "Opens the component preview." 会作为命令描述显示在命令面板里。action 名 `OpenComponentPreview` 属于 `workspace` 命名空间，因此命令面板里显示为 **`workspace: open component preview`**。
- [../component_preview/src/component_preview.rs:L25-L65](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L25-L65)：`component_preview::init`。它用 `cx.observe_new` 监听每个新建的 `Workspace`，在 workspace 上 `register_action` 注册 `OpenComponentPreview` 的处理器：创建 `ComponentPreview` 实体并 `add_item_to_active_pane` 加进当前面板（作为标签页）。
- [../zed/src/main.rs:L985](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/zed/src/main.rs#L985)：Zed 主程序启动时调用 `component_preview::init(app_state.clone(), cx)`——这就是上面 action 生效的入口。
- [../component_preview/src/component_preview.rs:L93-L111](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L93-L111)：`ComponentPreview` 结构体。注意 L101 的 `filter_editor: Entity<InputField>`——顶部过滤框的类型；以及 `component_map`、`components`、`entries` 等注册表快照字段。文件第 19 行 `use ui_input::InputField;` 直接依赖了本教程的主角 crate。
- [../component_preview/src/component_preview.rs:L124-L128](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L124-L128)：`ComponentPreview::new` 的开头——`components()` 克隆注册表、`sorted_components()` 排序，然后创建过滤用的 `InputField`（占位文案 "Find components or usages…"）。
- [../component_preview/src/component_preview.rs:L213-L323](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L213-L323)：`scope_ordered_entries`——把组件按 `scope` 分桶。L274-L277 组内按 `sort_name` 排序；L284-L290 把各 scope 按显示名（`to_string()`）字母序排列；L292-L305 依次压入 `Separator` + `SectionHeader(scope.to_string())` + 各组件条目。**你在侧栏看到的 "Forms & Input" 标题，就是 L297 把 `ComponentScope::Input.to_string()`（由 strum 映射，见 4.1）塞进 `SectionHeader` 的结果。**
- [../component_preview/src/component_preview.rs:L436-L482](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L436-L482)：All Components 页中单个组件的内联渲染：名称 + scope + 描述的头部框，下方 L480 调用 `(component.preview())(window, cx)` 现场构建演示。
- [../component_preview/src/component_preview.rs:L941-L980](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L941-L980)：单组件详情页。L954-L957 把 scope（"Forms & Input"）以小号弱化标签显示在名字上方；L962-L965 用大标题显示 `scopeless_name()`（即 "InputField"）；L968 显示描述；L979 再次调用 `(self.component.preview())(window, cx)` 渲染演示本体。
- [../component_preview/src/component_preview.rs:L192-L211](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L192-L211)：过滤逻辑——大小写不敏感地匹配组件名、scope 名或描述，三者命中任一即保留。在过滤框里输入 `input` 试试，`InputField` 会因名字命中而保留。

最后是独立运行入口（不想启动完整 Zed 时用它）：

- [../component_preview/examples/component_preview.rs:L1-L3](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/examples/component_preview.rs#L1-L3)：文件头文档注释写明了运行命令：`cargo run -p component_preview --example component_preview`。
- [../component_preview/examples/component_preview.rs:L31-L32](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/examples/component_preview.rs#L31-L32)：程序一进来（GPUI application 的 `run` 回调第一行）就调用 `component::init()` 填注册表。
- [../component_preview/examples/component_preview.rs:L79-L81](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/examples/component_preview.rs#L79-L81)：随后 `workspace::init` → `editor::init(cx)` → `init(app_state.clone(), cx)`。注意 `editor::init` 在创建任何 `InputField` **之前**执行——它负责注入 `ERASED_EDITOR_FACTORY`（注入点在 [../editor/src/editor.rs:L394-L397](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/editor/src/editor.rs#L394-L397)，工厂产出 `Editor::single_line` 的擦除版本）。这是上一讲「工厂未初始化则 `InputField::new` panic」结论的现场佐证。
- [../component_preview/examples/component_preview.rs:L125-L137](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/examples/component_preview.rs#L125-L137)：example 直接 new 出 `ComponentPreview` 并加进面板——绕过了命令面板，打开即见面板。

#### 4.4.4 代码实践

**实践目标**：用两种方式之一把组件预览面板跑起来，确认 `InputField` 在 "Forms & Input" 分区中可见。

**操作步骤**：

方式 A（独立 example，最快）：

```bash
cd <zed 仓库根目录>
cargo run -p component_preview --example component_preview
```

方式 B（完整 Zed）：

```bash
cargo run            # 在仓库根目录；首次构建耗时较长
```

然后在打开的 Zed 窗口里按 `cmd-shift-p`（macOS）或 `ctrl-shift-p` / `f1`（Windows/Linux）打开命令面板，输入 `component`，选择 **workspace: open component preview**。

**需要观察的现象**：

1. 面板打开后，左侧导航按字母序出现多个分区，其中有一组叫 **Forms & Input**；
2. 该组下有一项 **InputField**（也可能有 4.1 实践里发现的其他表单类组件）；
3. 点击 `InputField`，右侧详情页顶部依次显示：小字 "Forms & Input"、大标题 "InputField"、一段描述（就是 `description()` 返回的那句话），下方是两张演示卡片；
4. 卡片一标题为 "Small Label (Default)"，内含一个小号标签的输入框；卡片二标题为 "Regular Label"，标签字号更大；
5. 顶部过滤框输入 `input`，列表收窄到名字/分区/描述含 "input" 的组件。

**预期结果**：以上 5 点均应观察到；两张卡片可以点击聚焦并输入文字（它们是活的 `Entity<InputField>`，不是截图）。具体的像素级外观（间距、斜线纹理密度）随版本可能略有变化，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：面板侧栏的 "Forms & Input" 这个字符串，追到源头一共经过几站？

**参考答案**：三站。(1) `ComponentScope::Input` 变体上的 `#[strum(serialize = "Forms & Input")]`（[../component/src/component.rs:L306-L307](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L306-L307)）；(2) `InputField::scope()` 返回该变体（[src/input_field.rs:L238-L240](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L238-L240)）；(3) 面板 `scope_ordered_entries` 里 `scope.to_string()` 塞进 `SectionHeader`（[../component_preview/src/component_preview.rs:L292-L305](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L292-L305)）。

**练习 2**：为什么 `component_preview::init` 用 `cx.observe_new(...)` 注册 action，而不是在 `main` 里直接注册？

**参考答案**：`OpenComponentPreview` 是 workspace 级 action，需要挂在每个 `Workspace` 实例上（处理器要用该 workspace 的 project、app_state 等创建面板并加进它的活动 pane）。`observe_new` 保证将来新建的每个 workspace 窗口（多窗口场景）都自动带上这个命令，无需 `main` 逐一处理。

**练习 3**：如果运行独立 example 时把 `editor::init(cx)`（examples 第 80 行）注释掉，会发生什么？

**参考答案**：`ERASED_EDITOR_FACTORY` 不会被注入。`ComponentPreview::new` 创建过滤框 `InputField::new` 时，`ERASED_EDITOR_FACTORY.get()` 返回 `None`，`expect("ErasedEditorFactory to be initialized")` 直接 panic（见 [src/input_field.rs:L57-L59](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L57-L59)）——窗口刚打开就会崩溃。这验证了 u1-l1 的结论：**任何要用 `InputField` 的环境必须先执行 `editor::init`**。

## 5. 综合实践

把本讲四个模块串成一个任务：**从打开面板到逐行对应**。

### 5.1 实践目标

在真实运行的组件预览面板中找到 `InputField`，把屏幕上看到的每一个元素反向映射回源码行号，从而证明「预览系统 = Component trait + 注册表 + 布局助手 + 面板」四层结构。

### 5.2 操作步骤

1. **构建并运行**（二选一）：
   - `cargo run -p component_preview --example component_preview`（轻量）；
   - 或 `cargo run` 后用命令面板执行 `workspace: open component preview`。
2. **定位组件**：在左侧导航找到 **Forms & Input** 分区 → 点击 **InputField** 进入详情页。
3. **记录示例**：截图或文字记录两张演示卡片——"Small Label (Default)" 与 "Regular Label"——各自的标题文字、标签字号差异、输入框内的占位文本。
4. **逐项映射**：对照下表，把屏幕元素填到源码位置上：

| 屏幕上看到的 | 由哪个函数渲染 | 源码位置 |
| --- | --- | --- |
| 侧栏分组标题 "Forms & Input" | `scope_ordered_entries` 塞入 `SectionHeader` | component_preview.rs L292-L305 |
| 详情页小字 "Forms & Input" | `ComponentPreviewPage::render_header` | component_preview.rs L954-L957 |
| 大标题 "InputField" | `render_header` 的 `scopeless_name()` | component_preview.rs L962-L965 |
| 标题下的一段描述 | `InputField::description()` | input_field.rs L242-L246 |
| 卡片标题 "Small Label (Default)" | `single_example` 第一个参数 → `variant_name` | input_field.rs L261-L264 → component_layout.rs L35-L38 |
| 斜线纹理展示框 | `ComponentExample::render` | component_layout.rs L49-L66 |
| 框内可输入的输入框 | `cx.new(...InputField...)` 创建的实体 | input_field.rs L249-L250 |
| 卡片标题 "Regular Label" | 第二次 `single_example` 调用 | input_field.rs L265-L268 |
| 两张卡片纵向排列 | `example_group(vec![...])` | input_field.rs L260-L269 |
| 顶部搜索框 | `filter_editor: Entity<InputField>` | component_preview.rs L124-L128 |

5. **加分项**：在过滤框输入 `input`，观察 `InputField` 仍然在列（名字命中）；再输入一个不存在的词如 `zzz`，内容区出现 "No components matching 'zzz'." 的提示（对应 [../component_preview/src/component_preview.rs:L491-L498](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L491-L498)）。

### 5.3 预期结果

完成映射表后，你应该能不看资料回答：屏幕上任何一个像素区域，分别来自 `input_field.rs`、`component.rs`、`component_layout.rs`、`component_preview.rs` 中的哪一层。运行效果相关的观察点（截图、字号差异、过滤行为）在本地环境执行，**待本地验证**；若暂时无法构建 GUI 程序，可退化为「源码走查」：按 4.2 的时序图从 `component::init()` 一路读到 `(component.preview())(window, cx)`，同样能完成上表的映射。

## 6. 本讲小结

- `Component` trait 是组件的「身份证」：7 个方法全是静态方法，`description()` 与 `preview()` 必须实现，`InputField` 只额外覆盖了 `scope()`（`ComponentScope::Input`，经 strum 映射显示为 "Forms & Input"）。
- 注册走的是分布式路线：`#[derive(RegisterComponent)]` 生成 `inventory::submit!` 登记项 → `component::init()` 遍历调用 → `register_component::<T>()` 把静态方法（含 `preview` 函数指针）拍平成 `ComponentMetadata` 存入全局 `COMPONENT_DATA`。
- `preview` 返回的元素树用 `single_example`（标题 + 描述 + 斜线纹理展示框的卡片）与 `example_group`（卡片纵排 + 可选分组标题）组织成统一视觉语言；两者都是 `RenderOnce` 纯排版组件。
- 面板（`component_preview` crate）通过 `workspace: open component preview` 命令打开，按 scope 分组导航，渲染时才调用 `(metadata.preview())(window, cx)` 现场构建演示。
- 有趣的自举：面板顶部的过滤框本身就是一个 `Entity<InputField>`，因此打开面板隐式依赖 `editor::init` 已注入 `ERASED_EDITOR_FACTORY`——u1-l1 的工厂机制在此闭环。
- 两种运行方式：完整 Zed（命令面板）或 `cargo run -p component_preview --example component_preview`（独立窗口，example 中 `component::init` → `editor::init` → `component_preview::init` 的顺序不可颠倒）。

## 7. 下一步学习建议

本讲只把 `InputField` 当作「预览系统里的一个展品」看了外观，还没拆它的内部。下一讲 **u2-l1《InputField 的数据模型与 Builder API》**将逐字段分析结构体（`label`、`placeholder`、`start_icon`、`masked`……），讲解 `InputField::new` 如何通过 `ERASED_EDITOR_FACTORY` 创建内芯，以及 `text()`/`set_text()`/`is_empty()` 这些委托方法的惯用法。

在继续之前，建议自行阅读两段源码巩固本讲：

1. [../component_preview/src/component_preview.rs:L213-L323](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L213-L323) 的 `scope_ordered_entries`——体会「分组 → 组内排序 → 分区字母序」的三级排序逻辑；
2. [../component/src/component.rs:L47-L61](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component/src/component.rs#L47-L61) 的 `register_component`——注意「trait 静态方法 → 元数据」的拍平手法，第 3 单元讲 `ErasedEditor` 的类型擦除时会再次遇到类似思想。
