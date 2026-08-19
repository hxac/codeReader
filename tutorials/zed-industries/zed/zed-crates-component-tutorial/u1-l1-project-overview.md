# component crate 是什么：Zed 组件预览体系的入口

> 本讲是 `zed-crates-component` 学习手册的第一讲（u1-l1，入门篇）。没有任何前置讲义，你只需要一个能在本地打开的 Zed 仓库。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `component` crate 的三大职责：**Component trait**、**组件注册表**、**预览布局**。
2. 画出（或默写出）`component`、`ui_macros`、`ui`、`component_preview`、`workspace` 五个 crate 之间的依赖与协作关系。
3. 理解 Zed 为什么需要一套「组件预览与注册机制」——它解决的是设计系统治理和 UI 可视化调试问题，而不是普通的运行时功能。

本讲不要求你读懂每一行代码。我们只做一件事：把这个不足 600 行的小 crate 放进它在整个 Zed 仓库中的位置，建立一张「地图」。后续所有讲义都会在这张地图上展开。

## 2. 前置知识

本讲面向初学者，但有几个 Rust 和 Zed 的术语最好先混个脸熟。不需要精通，知道它们「大概是干什么」即可。

| 术语 | 通俗解释 |
| --- | --- |
| crate | Rust 的编译单元，类似于「一个库或一个包」。Zed 仓库的 `crates/` 目录下有上百个 crate。 |
| trait | Rust 中定义「一组能力」的方式。一个类型实现（`impl`）了某个 trait，就等于声明「我具备这些能力」。类似其他语言的 interface。 |
| 派生宏（derive macro） | 写在类型上方的一种编译期注解，例如 `#[derive(RegisterComponent)]`，编译器会据此自动生成一段附加代码。 |
| 过程宏 crate | 专门用来「生成代码的代码」的 crate，`ui_macros` 就是。它自己不参与运行，只在编译期干活。 |
| gpui | Zed 自研的 UI 框架，同时提供状态管理和并发原语。`AnyElement`、`Window`、`App` 都来自它。 |
| inventory | 一个第三方 crate，提供「分布式切片」：把散落在各处的注册项在**链接期**自动收集起来，不需要一个集中清单。第 2 单元会精读。 |
| workspace（双义） | 小写的 `workspace` crate 是 Zed 里「一个编辑器工作区」的核心抽象（窗口、标签页、面板都挂在它上面）；注意与 Rust 自身的 Cargo workspace 概念区分。 |
| action | Zed 里的用户动作（通常由快捷键触发），例如 `workspace: open component preview`。 |

一个值得先建立的直觉：**`component` crate 不渲染任何业务界面**。Zed 的按钮、图标、输入框都在 `ui` crate 里；`component` crate 提供的是一套「让这些 UI 组件可以被登记、被分类、被预览」的**元层设施**——你可以把它理解成 UI 组件世界的「户籍管理系统 + 展览馆布展工具」。

## 3. 本讲源码地图

本讲涉及的关键文件（行号均对应当前 HEAD `28c0f4ae`）：

| 文件 | 作用 | 本讲怎么看它 |
| --- | --- | --- |
| [crates/component/src/component.rs:L1-L8](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L1-L8) | crate 唯一的两个源文件之一：模块文档 + Component trait + 注册表 + 元数据 + 作用域/状态枚举 | 只读顶部模块文档和文件整体结构 |
| [crates/component/src/component_layout.rs:L1-L14](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L1-L14) | 另一个源文件：预览页的布局元素（示例卡片、分组容器）与辅助函数 | 本讲只确认它存在，第 3 单元精读 |
| [crates/component/Cargo.toml:L1-L26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L1-L26) | `component` crate 的依赖声明 | 逐个依赖弄清用途 |
| [crates/component_preview/Cargo.toml:L18-L50](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L18-L50) | 预览应用的依赖声明与 example 目标 | 从依赖反推五 crate 关系 |
| [crates/ui/Cargo.toml:L17-L31](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/Cargo.toml#L17-L31) | UI 组件库的依赖声明 | 证明 `ui` 依赖 `component` 与 `ui_macros` |
| [crates/workspace/src/workspace.rs:L784-L786](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L784-L786) | `workspace::init` 中调用 `component::init()` 的位置 | 证明 workspace 与 component 的调用关系 |
| [crates/component_preview/src/component_preview.rs:L25-L34](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L25-L34) | 预览应用的 `init`：注册可序列化条目 + 订阅 `OpenComponentPreview` 动作 | 理解「依赖反转」的协作方式 |

## 4. 核心概念与源码讲解

### 4.1 模块文档里的三大职责：component.rs 的自我介绍

#### 4.1.1 概念说明

打开一个陌生 crate 的正确姿势是先读它的**模块文档**（文件顶部 `//!` 开头的注释）。`component.rs` 的模块文档只有 8 行，但它恰好把这个 crate 的三大职责说得一清二楚：

1. **Component trait** —— 定义「什么算一个可预览的 UI 组件」；
2. **预览布局**（layouts for rendering component examples and example groups）—— 预览页上每个示例卡片、每组示例的排版元素；
3. **分布式注册机制**（the distributed slice mechanism for registering components）—— 让散落在各个 crate 里的组件自动登记进全局注册表，而不需要维护一份集中清单。

这正好对应学习目标里的「三大职责」。后面三个单元的讲义也基本按这个顺序展开：trait 与数据结构（第 2 单元）→ 预览布局（第 3 单元）→ 注册机制与预览应用（第 2、4 单元）。

#### 4.1.2 核心流程

先从 30 米高空看一遍「一个 UI 组件从被定义到出现在预览面板里」的全程，本讲只需记住骨架：

```text
① 定义组件的类型（通常在 ui 或业务 crate 中）
        │  #[derive(RegisterComponent)]     ← ui_macros 在编译期生成注册代码
        ▼
② 链接期：inventory 把所有「注册函数」收集成一个分布式切片
        │
        ▼
③ 运行期：component::init() 遍历切片，逐个执行注册函数
        │  每个注册函数调用 register_component::<T>()
        ▼
④ 组件的元信息（名字、描述、scope、status、preview 函数指针）
   被写入全局注册表 COMPONENT_DATA
        │
        ▼
⑤ 用户打开组件预览 → component_preview 读取注册表，
   按 scope 分组渲染，逐个调用 preview 函数展示组件
```

本讲的三个模块分别对应：①②⑤的「是什么」（4.1）、这个 crate 需要哪些积木（4.2）、⑤所在的预览应用如何与其他 crate 接线（4.3）。③④的细节留给第 2 单元。

#### 4.1.3 源码精读

先看模块文档原文：

> [crates/component/src/component.rs:L1-L8](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L1-L8)
>
> ```rust
> //! # Component
> //!
> //! This module provides the Component trait, which is used to define
> //! components for visual testing and debugging.
> //!
> //! Additionally, it includes layouts for rendering component examples
> //! and example groups, as well as the distributed slice mechanism for
> //! registering components.
> ```

这段文档逐句翻译过来就是三大职责：第一句对应 **Component trait**（用于可视测试与调试）；第二句对应**预览布局**（渲染组件示例与示例分组）；第三句对应**分布式注册机制**。

紧接着两行是文件的模块组织：

> [crates/component/src/component.rs:L10-L14](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L10-L14)
>
> ```rust
> mod component_layout;
> ...
> pub use component_layout::*;
> ```
>
> 私有子模块 `component_layout`（预览布局的实现），再通过 `pub use *` 把它的内容扁平地暴露出去。所以使用者写 `component::single_example(...)` 时，其实调用的是 `component_layout.rs` 里的函数。

整个 crate 就这两个文件：`component.rs` 约 326 行，`component_layout.rs` 约 206 行。为了给后续讲义埋好锚点，这里给一张 `component.rs` 的内部地图（本讲不深入，混个脸熟即可）：

| 行号范围 | 内容 | 对应职责 | 精读讲义 |
| --- | --- | --- | --- |
| [L21-L45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L21-L45) | `components()`、`init()`、`ComponentFn`、`inventory::collect!`、`__private` | 注册机制 | u2-l3 |
| [L47-L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L47-L64) | `register_component::<T>()`、全局注册表 `COMPONENT_DATA` | 注册机制 | u2-l2 |
| [L66-L107](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L66-L107) | `ComponentRegistry` 查询/排序 API、`ComponentId` | 注册表 | u2-l2 |
| [L109-L158](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L109-L158) | `ComponentMetadata`（把 trait 信息固化成可存储的值） | 注册表 | u2-l2 |
| [L160-L258](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L160-L258) | `Component` trait 本体（七个方法） | Component trait | u2-l1 |
| [L268-L325](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L268-L325) | `ComponentStatus`（生命周期）与 `ComponentScope`（15 个分类） | 治理语义 | u2-l5 |

最后看一处能直接回答「为什么需要这套机制」的文档——`Component` trait 的说明：

> [crates/component/src/component.rs:L160-L165](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L160-L165)
>
> ```rust
> /// Implement this trait to define a UI component. This will allow you to
> /// derive `RegisterComponent` on it, in turn allowing you to preview the
> /// contents of the preview fn in `workspace: open component preview`.
> ///
> /// This can be useful for visual debugging and testing, documenting UI
> /// patterns, or simply showing all the variants of a component.
> ```
>
> 这段文档给出了三个官方用途：**可视调试与测试**（visual debugging and testing）、**给 UI 模式做文档**（documenting UI patterns）、**展示一个组件的所有变体**（showing all the variants）。这三个用途就是「为什么需要它」的答案——Zed 的设计系统有上百个组件、每个组件又有多种状态和尺寸，没有一套集中预览设施，设计师和工程师很难对齐「这个组件现在长什么样、哪些能用、哪些已弃用」。

#### 4.1.4 代码实践

**实践：用 grep 给 component.rs 画一张「pub 项地图」**

1. **实践目标**：不逐行读代码，用一条命令掌握 `component.rs` 暴露了哪些顶层 API，验证它与三大职责的对应关系。
2. **操作步骤**：在 Zed 仓库根目录运行：

   ```bash
   grep -nE '^pub (fn|struct|enum|trait|static|mod)' crates/component/src/component.rs
   ```

3. **需要观察的现象**：输出的每一行形如 `21:pub fn components() ...`，带行号、带种类。
4. **预期结果**：应得到约 12 个顶层 pub 项——`fn` 3 个（`components`、`init`、`register_component`）、`struct` 4 个（`ComponentFn`、`ComponentRegistry`、`ComponentId`、`ComponentMetadata`）、`enum` 2 个（`ComponentStatus`、`ComponentScope`）、`trait` 1 个（`Component`）、`static` 1 个（`COMPONENT_DATA`）、`mod` 1 个（`__private`）。把它们与 4.1.3 的行号表对照，应当完全吻合。（这正是本讲义撰写时实际运行的结果。）
5. 若你的仓库 HEAD 不同导致行号有出入，以你自己 grep 到的行号为准。

#### 4.1.5 小练习与答案

**练习 1**：模块文档说这个 crate 有三大职责，请分别说出对应哪个源码文件或哪段代码。
**参考答案**：① Component trait → `component.rs` 的 [L160-L258](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L160-L258)；② 预览布局 → `component_layout.rs`（经 [L14](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L14) 的 `pub use` 导出）；③ 分布式注册机制 → `component.rs` 的 [L25-L45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L45)（`init`、`ComponentFn`、`inventory::collect!`）配合 [L47-L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L47-L64)（`register_component` 与 `COMPONENT_DATA`）。

**练习 2**：为什么 Zed 不直接在 `ui` crate 里写一个「所有组件的列表」数组，而要搞分布式注册？
**参考答案**：集中列表意味着每新增一个组件都要改同一处代码，跨 crate 时更麻烦——`ui_input`、`git_ui`、`agent_ui` 等crate的组件也得回来改 `ui` 的清单，造成依赖倒挂与合并冲突。分布式注册让每个组件「在自己家门口登记」，新增组件零中心化改动；代价是注册发生在链接期、排查问题时不如集中列表直观（第 2 单元会展开这个取舍）。

### 4.2 极简 crate 的自我介绍：Cargo.toml 依赖声明

#### 4.2.1 概念说明

一个 crate 的 `Cargo.toml` 是它的「名片」：依赖越少、越底层，说明它越接近地基。`component` crate 只有 6 个直接依赖，全部是 workspace 内部 crate 或基础设施库，没有任何业务依赖——这正是它能被 `ui`、`workspace` 等上层 crate 依赖而不引起循环依赖的前提。

#### 4.2.2 核心流程

阅读 `Cargo.toml` 的顺序建议：

```text
① [package]      → 我叫什么、版本、许可证
② [lib] path     → 库根文件在哪（本 crate 用了非默认命名）
③ [dependencies] → 我需要哪些积木（本讲重点）
④ [dev-dependencies] / [features] → 只在测试/特性开关下需要的积木
```

#### 4.2.3 源码精读

> [crates/component/Cargo.toml:L11-L12](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L11-L12)
>
> ```toml
> [lib]
> path = "src/component.rs"
> ```
>
> 库根不是默认的 `src/lib.rs`，而是 `src/component.rs`。这是 Zed 仓库的统一约定（见仓库 CLAUDE.md：「prefer specifying the library root path in Cargo.toml... to maintain consistent and descriptive naming」），目的是让文件名与 crate 名一致，`grep` 时更容易定位。

依赖部分逐个看：

> [crates/component/Cargo.toml:L14-L20](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L14-L20)
>
> ```toml
> [dependencies]
> collections.workspace = true
> gpui.workspace = true
> inventory.workspace = true
> parking_lot.workspace = true
> strum.workspace = true
> theme.workspace = true
> ```

| 依赖 | 在这个 crate 里干什么 | 证据位置 |
| --- | --- | --- |
| `collections` | Zed 内部的集合类型封装，注册表里的 `HashMap` 来自它 | [component.rs:L16](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L16)、[L68](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L68) |
| `gpui` | UI 框架：`AnyElement`、`App`、`Window`、`SharedString`（preview 函数的签名离不开它们） | [component.rs:L17](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L17) |
| `inventory` | 分布式注册的引擎：`collect!`/`submit!` 宏 | [component.rs:L39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L39) |
| `parking_lot` | 高性能同步锁，`RwLock` 用来保护全局注册表 | [component.rs:L18](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L18)、[L63](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L63) |
| `strum` | 让枚举在「Display 展示名」和「字符串解析」之间自动转换，`ComponentScope`/`ComponentStatus` 靠它 | [component.rs:L19](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L19)、[L268](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L268) |
| `theme` | 主题令牌（颜色等），预览布局元素用它取色 | [component_layout.rs:L5](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L5) |

再看「谁依赖了它」。以 `ui` crate 为例：

> [crates/ui/Cargo.toml:L17](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/Cargo.toml#L17) 与 [L31](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/Cargo.toml#L31)
>
> ```toml
> component.workspace = true
> ...
> ui_macros.workspace = true
> ```
>
> `ui` 同时依赖 `component`（运行时的 trait 与注册表）和 `ui_macros`（编译期的派生宏）。这两个依赖性质完全不同：前者是普通库依赖，后者是过程宏依赖。

一个容易混淆的点：`ui_macros` 自己**并不依赖** `component`。派生宏生成的代码里写的是 `component::register_component::<T>()` 这样的**路径**，这些路径是在「使用宏的那个 crate」里解析的，所以只要使用者（如 `ui`）同时依赖两者即可。证据在派生宏的实现里：

> [crates/ui_macros/src/derive_register_component.rs:L20-L26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L20-L26)
>
> ```rust
> #[allow(non_snake_case)]
> fn #register_fn_name() {
>     component::register_component::<#name>();
> }
>
> component::__private::inventory::submit! {
>     component::ComponentFn::new(#register_fn_name)
> }
> ```
>
> 生成的代码直接引用 `component::` 前缀（第 2 单元 u2-l4 会逐行展开这段 `quote!`）。

#### 4.2.4 代码实践

**实践：用 cargo tree 验证依赖名片**

1. **实践目标**：亲手验证 `component` 的直接依赖确实只有 6 个，并观察 `ui` 如何同时挂上 `component` 与 `ui_macros`。
2. **操作步骤**：在 Zed 仓库根目录运行：

   ```bash
   cargo tree -p component --depth 1
   cargo tree -p ui --depth 1 | grep -E 'component|ui_macros'
   ```

3. **需要观察的现象**：第一条命令输出 `component v0.1.0` 及其直接依赖列表；第二条命令应在 `ui` 的依赖里看到 `component` 和 `ui_macros` 两行。
4. **预期结果**：第一条的依赖集合与 [Cargo.toml:L14-L20](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L14-L20) 完全一致（版本号以你本地的 workspace 继承为准）。注意：`cargo tree` 会解析整个 workspace，首次运行可能需要下载/编译依赖，耗时较长属正常现象。
5. 具体输出格式与版本号**待本地验证**（不同 HEAD 与平台可能有差异），但依赖集合不应有出入。

#### 4.2.5 小练习与答案

**练习 1**：`component` 的依赖里为什么有 `theme`？它和「注册组件」这件事看起来无关。
**参考答案**：因为 crate 的第二大职责是「预览布局」——`component_layout.rs` 里的 `ComponentExample`/`ComponentExampleGroup` 是真正的渲染元素，需要用 `cx.theme().colors()` 取边框色、文字色、斜纹背景色（见 [component_layout.rs:L38-L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L38-L64)）。这也说明这个 crate 不只是「登记处」，还自带布展工具。

**练习 2**：如果把 `Component` trait 从 `component.rs` 挪到 `ui` crate，会发生什么？
**参考答案**：会形成依赖矛盾。`ui_macros` 生成的代码引用 `component::Component`（编译期断言，见 [derive_register_component.rs:L15](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L15)），而 `git_ui`、`agent_ui` 等**不依赖 `ui`** 的 crate 也想注册组件；trait 若进了 `ui`，这些 crate 就被迫依赖庞大的 `ui`。放在只有 6 个依赖的 `component` 里，任何 crate 都能低成本接入——这是「元层设施要放在依赖图底部」的典型设计。

### 4.3 五个 crate 如何协作：component_preview 的依赖关系

#### 4.3.1 概念说明

`component_preview` 是把注册表「变成应用」的地方：它读取 `component::components()` 返回的注册表，按作用域分组渲染成左侧导航 + 右侧预览的界面。它是我们观察五个 crate 关系的最佳窗口，因为它几乎站在依赖图的顶端。

先给出本讲最重要的图——五个 crate 的依赖与协作关系（每条边都已在源码中验证）：

```text
        ┌───────────────────────────────────────────────────────┐
        │ zed 主程序 (crates/zed)                                │
        │ 启动时调用 component_preview::init()                    │
        └───────────────────────┬───────────────────────────────┘
                                │ Cargo 依赖 + init 调用
                                ▼
┌───────────────────────────────────────────────────────────────┐
│ component_preview（预览应用，本讲 4.3 主角）                    │
│ Cargo 依赖：component、ui、workspace、db、settings …           │
│ 在 workspace 上注册 OpenComponentPreview 动作的处理器           │
└──────┬────────────────────┬──────────────────────┬────────────┘
       │                    │                      │
       ▼                    ▼                      ▼
┌─────────────┐    ┌──────────────┐    ┌─────────────────────────┐
│  component  │◄───│      ui      │    │       workspace          │
│  (注册核心)  │    │  (组件库)     │    │ Cargo 依赖 component、ui │
│             │◄───┼──────────────┘    │ workspace::init() 中调用 │
└─────────────┘    │                   │ component::init()        │
       ▲           │ ui 还依赖 ui_macros│ 定义 OpenComponentPreview │
       │           ▼                   │ 动作（但不依赖            │
       │    ┌──────────────┐           │ component_preview）      │
       │    │  ui_macros   │           └─────────────────────────┘
       └────┤ (过程宏 crate) │
   生成代码   │  无 Cargo 依赖 │
   引用它的   └──────────────┘
   路径
```

用边列表总结：

| 关系 | 类型 | 证据 |
| --- | --- | --- |
| `ui` → `component` | Cargo 依赖 | [crates/ui/Cargo.toml:L17](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/Cargo.toml#L17) |
| `ui` → `ui_macros` | Cargo 依赖（过程宏） | [crates/ui/Cargo.toml:L31](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/Cargo.toml#L31) |
| `ui_macros` → `component` | **无** Cargo 依赖，仅生成代码引用其路径 | [derive_register_component.rs:L15-L26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L15-L26) |
| `component_preview` → `component` / `ui` / `workspace` / `db` | Cargo 依赖 | [component_preview/Cargo.toml:L22](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L22)、[L37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L37)、[L40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L40)、[L23](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L23) |
| `workspace` → `component` | Cargo 依赖 | [crates/workspace/Cargo.toml:L37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/Cargo.toml#L37) |
| `workspace` → `component_preview` | **无**依赖（方向相反！） | `workspace/Cargo.toml` 中无 `component_preview` 条目 |
| `zed` → `component_preview` | Cargo 依赖 + 启动调用 | [crates/zed/src/main.rs:L978](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/zed/src/main.rs#L978) |

#### 4.3.2 核心流程

注意一个反直觉的点：**`workspace` 并不依赖 `component_preview`，反而是 `component_preview` 依赖 `workspace`**。那用户在 Zed 里触发 `workspace: open component preview` 时，workspace 怎么知道要打开预览？答案是 Zed 常用的「动作反转」模式：

```text
① workspace crate 定义动作 OpenComponentPreview（只是一个数据类型，不含实现）
        │
        ② zed 主程序启动时调用 component_preview::init()
        │
        ③ init 在每个新 workspace 上注册该动作的处理器
        │   （component_preview 依赖 workspace，所以它能引用这个动作类型）
        ▼
④ 用户触发动作 → 处理器创建 ComponentPreview 条目并加进 workspace
```

这样 `workspace` 保持了对具体面板的无知，依赖图保持无环。

#### 4.3.3 源码精读

先看 `component_preview` 的依赖声明（节选关键行）：

> [crates/component_preview/Cargo.toml:L18-L40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L18-L40)
>
> ```toml
> [dependencies]
> anyhow.workspace = true
> client.workspace = true
> collections.workspace = true
> component.workspace = true      # ← 注册表在这里
> db.workspace = true             # ← 状态持久化（SQLite）
> ...
> ui.workspace = true             # ← 预览界面本身用 ui 组件搭建
> ...
> workspace.workspace = true      # ← 作为 workspace 的条目（Item）嵌入
> ```
>
> 它同时依赖底层的 `component`（读注册表）、中间的 `ui`（搭界面）和上层的 `workspace`（把自己作为面板条目挂进去），还引入 `db` 做状态持久化——第 4 单元 u4-l3 会讲后者。

再看 example 声明：

> [crates/component_preview/Cargo.toml:L48-L50](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L48-L50)
>
> ```toml
> [[example]]
> name = "component_preview"
> path = "examples/component_preview.rs"
> ```
>
> 这就是 `cargo run -p component_preview --example component_preview` 能独立启动预览的原因（下一讲 u1-l2 实操）。

然后验证 4.3.2 的「动作反转」：

> [crates/component_preview/src/component_preview.rs:L25-L34](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L25-L34)
>
> ```rust
> pub fn init(app_state: Arc<AppState>, cx: &mut App) {
>     workspace::register_serializable_item::<ComponentPreview>(cx);
>
>     cx.observe_new(move |workspace: &mut Workspace, _window, cx| {
>         ...
>         workspace.register_action(
>             move |workspace, _: &workspace::OpenComponentPreview, window, cx| {
> ```
>
> `component_preview::init` 做两件事：把 `ComponentPreview` 注册为可序列化条目；对每个新建的 workspace 注册 `OpenComponentPreview` 动作的处理器。注意动作类型来自 `workspace::`——依赖方向是「我依赖你，所以我认识你的动作」。

> [crates/workspace/src/workspace.rs:L307](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L307)
>
> ```rust
> OpenComponentPreview,
> ```
>
> 这是 `workspace` crate 里 `actions!` 宏清单中的一行：动作在 `workspace` 中**定义**。`workspace` 对谁来实现它一无所知。

最后两条接线：

> [crates/workspace/src/workspace.rs:L784-L786](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L784-L786)
>
> ```rust
> pub fn init(app_state: Arc<AppState>, cx: &mut App) {
>     component::init();
> ```
>
> `workspace::init` 的第一行就是 `component::init()`——遍历链接期收集的注册函数、填充全局注册表。这就是为什么 `workspace` 必须直接依赖 `component`。

> [crates/zed/src/main.rs:L978](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/zed/src/main.rs#L978)
>
> ```rust
> component_preview::init(app_state.clone(), cx);
> ```
>
> Zed 主程序启动时安装预览面板的动作处理器。没有这一行，编辑器里触发 `workspace: open component preview` 不会有任何反应（但独立 example 仍然可用，因为它自己调用了同样的初始化链）。

#### 4.3.4 代码实践

**实践：全仓库普查——谁在使用这套注册体系？**

1. **实践目标**：用一条 grep 命令统计 `derive(RegisterComponent)` 在 `crates/` 下的真实分布，直观感受这套体系的覆盖面。
2. **操作步骤**：在 Zed 仓库根目录依次运行：

   ```bash
   # 有多少个源码文件用到了 RegisterComponent 派生
   grep -rlE 'derive\(.*RegisterComponent' crates/ --include='*.rs' | wc -l

   # 分布在哪些 crate 里
   grep -rlE 'derive\(.*RegisterComponent' crates/ --include='*.rs' | cut -d/ -f2 | sort | uniq -c | sort -rn
   ```

3. **需要观察的现象**：第一条输出一个数字；第二条输出「次数 + crate 名」的排行榜。
4. **预期结果**：本讲义撰写时（HEAD `28c0f4ae`）实测：**62 个 `.rs` 文件**命中，分布在 **11 个 crate**——`ui` 占绝对多数，其余按多少依次还有 `agent_ui`、`git_ui` 等，完整名单是：`ui`、`ui_input`、`settings_ui`、`workspace`、`git_ui`、`agent_ui`、`extensions_ui`、`notifications`、`onboarding`、`ai_onboarding`、`language_models`（另有 `ui_macros` 文档示例 1 处，不算真实使用）。你的数字会随 HEAD 漂移，量级应在几十个文件、十个左右 crate。
5. 注意加上 `--include='*.rs'`，否则教程类文件（如本讲义所在目录的 json/md）会混进统计。

#### 4.3.5 小练习与答案

**练习 1**：`workspace` crate 里能 `use component_preview::...` 吗？为什么？
**参考答案**：不能。Cargo 依赖是单向的：`component_preview` 依赖 `workspace`（见 [component_preview/Cargo.toml:L40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L40)），若 `workspace` 再依赖 `component_preview` 就成了循环依赖，Cargo 会直接拒绝。所以采用「workspace 定义动作、component_preview 注册处理器」的反转模式。

**练习 2**：假如把 `component_preview::init()` 从 `zed/src/main.rs` 中删掉，独立 example `cargo run -p component_preview --example component_preview` 还能正常显示组件吗？
**参考答案**：能。example 有自己的 `main`，会自己完成 settings/theme/workspace 及 `component_preview::init()` 的初始化链，不经过 `zed` 主程序；受影响的只是「在完整 Zed 编辑器里触发动作打开预览」这条路径（处理器没人注册了）。此推断基于两者的初始化结构，具体现象**待本地验证**。

**练习 3**：对照 4.3.1 的图，说出 `component::init()` 与 `component_preview::init()` 各自的调用者和职责差异。
**参考答案**：`component::init()` 由 `workspace::init` 调用（[workspace.rs:L785](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L785)），职责是**填充注册表**（执行链接期收集到的注册函数，见 [component.rs:L25-L29](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L29)）；`component_preview::init()` 由 `zed` 主程序调用（[main.rs:L978](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/zed/src/main.rs#L978)），职责是**安装 UI 接线**（注册可序列化条目与动作处理器）。前者提供数据，后者提供入口。

## 5. 综合实践

把本讲三个模块串成一份小调研笔记（建议动手做完，下一讲会用到其中的直观感受）：

**任务：给 component crate 写一份 5 句以内的「定位总结」**

1. 通读 [crates/component/src/component.rs:L1-L8](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L1-L8) 的模块文档和 [L160-L169](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L160-L169) 的 trait 文档。
2. 完成 4.3.4 的 grep 普查，记录你统计到的文件数和 crate 名单（至少列出 3 个）。
3. 对照 4.3.1 的依赖图，在纸上凭记忆重画一遍五个 crate 的关系（重点别画反 `workspace` 与 `component_preview` 的箭头）。
4. 用自己的话写下不超过 5 句的定位总结。一份合格的总结应当涵盖：它是一个「元层」crate 而非业务 UI 库；三大职责；它被谁依赖、依赖谁；它的注册机制为什么能让跨 crate 的组件自动进目录。

**参考范文（先自己写再看）**：

> `component` 是 Zed 设计系统的注册中枢：它定义 `Component` trait 来描述「可预览的 UI 组件」，提供全局注册表存放每个组件的元数据，并附带预览页的布局元素。组件通过 `#[derive(RegisterComponent)]` 在编译期自动登记，运行期由 `component::init()` 统一填充注册表，因此新增组件无需修改任何中心清单。它只依赖 gpui、theme 等基础 crate，站在依赖图底部，所以 `ui`、`git_ui`、`agent_ui` 等十来个 crate 都能低成本接入。预览界面由依赖它的 `component_preview` 呈现，后者反过来依赖 `workspace` 并注册 `OpenComponentPreview` 动作处理器。这套体系服务三件事：可视调试、UI 模式文档、组件变体与生命周期展示。

## 6. 本讲小结

- `component` crate 只有约 530 行、两个源文件，三大职责来自模块文档的原话：**Component trait**（定义可预览组件）、**预览布局**（`component_layout.rs` 的示例卡片与分组）、**分布式注册机制**（inventory + 全局注册表 `COMPONENT_DATA`）。
- 官方给出的存在理由是三个：可视调试与测试、给 UI 模式做文档、展示组件的全部变体——本质是设计系统治理设施，不是运行时业务功能。
- 依赖图（验证过）：`ui` 依赖 `component` 与 `ui_macros`；`ui_macros` 不依赖 `component`，只生成引用 `component::` 路径的代码；`component_preview` 依赖 `component`、`ui`、`workspace`；`workspace` 依赖 `component` 但**不**依赖 `component_preview`。
- 两个关键接线点：`workspace::init` 第一行调用 `component::init()` 填充注册表（[workspace.rs:L785](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L785)）；`zed` 主程序调用 `component_preview::init()` 安装动作处理器（[main.rs:L978](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/zed/src/main.rs#L978)）。
- 实测覆盖率：HEAD `28c0f4ae` 下 62 个 `.rs` 文件、11 个 crate 在使用 `derive(RegisterComponent)`，`ui` 之外还有 `git_ui`、`agent_ui`、`settings_ui` 等，证明它是跨 crate 的公共设施。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：《把组件预览跑起来》——用 `cargo run -p component_preview --example component_preview` 亲眼看看本讲描述的注册表长什么样，并实验删掉 `component::init()` 会发生什么。
- 如果想立刻深入本讲埋下的线头，推荐按顺序预读：[component.rs:L25-L45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L45)（init 与 inventory 的三行核心）、[component_preview/examples/component_preview.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs)（独立启动预览的最小初始化链）。
- 第 2 单元将逐方法精读 `Component` trait 的七个方法，并解剖 `ComponentRegistry` / `ComponentMetadata` 数据结构——本讲的「pub 项地图」就是那两讲的预习提纲。
