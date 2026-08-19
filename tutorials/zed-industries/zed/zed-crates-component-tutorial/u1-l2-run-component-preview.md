# 把组件预览跑起来：example 二进制与 workspace 入口

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `cargo run -p component_preview --example component_preview` 独立启动 Zed 的组件预览应用。
2. 在 Zed 编辑器（开发版）里通过命令面板触发 `workspace: open component preview` 动作打开同一个面板。
3. 准确说出 `component::init()` 在两条启动路径中各自被调用的位置，并解释它为什么必须发生在任何组件预览界面被构造之前。
4. 理解「example 二进制」在这个仓库里的角色：它是一个手工组装的最小宿主应用，把库 crate `component_preview` 变成可以双击运行的东西。

## 2. 前置知识

- **crate 与 example 目标**：Rust 的一个 package 可以同时包含库（lib）和若干可执行目标。`[[example]]` 是 Cargo 里显式声明示例二进制的段落，声明后可以用 `cargo run -p <包名> --example <名称>` 单独运行它。本讲会看到 `component_preview` 包就是「库 + 一个 example」的结构。
- **动作（Action）**：GPUI 里的动作是一个普通的 Rust 类型（通常由 `actions!` 宏生成），代表用户的一次意图（如「打开组件预览」）。动作可以通过键绑定或命令面板触发，再由某处注册的处理器（handler）消费。动作名写作 `命名空间: 小写名称`，例如 `workspace: open component preview`。
- **inventory 的两阶段注册**（上一讲已建立，这里只需要结论）：`derive(RegisterComponent)` 生成的代码在**链接期**把一个注册函数登记进 `inventory` 的分布式切片；`component::init()` 在**运行期**遍历这个切片并逐个执行注册函数，把组件元数据填进全局注册表。链接期只是「登记」，运行期才「搬家」。
- **幂等（idempotent）**：同一个操作执行多次和执行一次效果相同。本讲会看到 `component::init()` 被调用两次也不会出错，原因就是注册表的写入方式是幂等的。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/component_preview/examples/component_preview.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs) | example 二进制，组件预览的独立宿主应用 | `main` 的完整初始化链、`component::init()` 的调用位置、窗口与条目的创建 |
| [crates/component_preview/Cargo.toml](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml) | 包定义 | `[[example]]` 显式声明，以及 example 依赖的 `dev-dependencies` |
| [crates/component/src/component.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs) | Component trait 与注册表 | `init()`、`components()`、`register_component()`、`COMPONENT_DATA` |
| [crates/workspace/src/workspace.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs) | workspace 框架 | `workspace::init` 第一行的 `component::init()`；`OpenComponentPreview` 动作定义 |
| [crates/component_preview/src/component_preview.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs) | 预览应用本体（库） | `init()` 里的动作处理器注册；`ComponentPreview::new` 读取注册表快照的时机 |
| [crates/zed/src/main.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/zed/src/main.rs) | Zed 主程序 | 无条件调用 `component_preview::init()` 完成接线 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**component::init**（注册表何时被填充）、**example 的 main**（一条独立的启动路径）、**workspace::init**（Zed 编辑器内的启动路径与动作入口）。

### 4.1 component::init：从链接期登记到运行期注册表

#### 4.1.1 概念说明

上一讲我们知道，`derive(RegisterComponent)` 只是在链接期把「注册函数」登记进了 `inventory` 的分布式切片。此时全局注册表 `COMPONENT_DATA` 仍然是空的——没有任何组件。

`component::init()` 就是那个「按下开关」的函数：它遍历登记好的所有注册函数并逐个执行，把每个组件的元数据写进全局注册表。在它执行之前，任何向注册表查询组件的代码都只能看到一个空表。

要理解本讲后面的实验，还需要知道一个关键性质：**这个填充操作是幂等的**。注册表的写入是以组件 `id` 为键的哈希表插入，同一个组件注册两次只是覆盖成相同的值。因此 `component::init()` 被调用两次、三次都不会产生重复条目或报错——这解释了为什么两条启动路径可以「各自保险地」调用它。

#### 4.1.2 核心流程

```text
程序编译链接阶段
  └─ derive(RegisterComponent) 生成的 inventory::submit!(...)
     把 ComponentFn(fn 指针) 登记进分布式切片   ← 只是登记，未执行

程序运行阶段（任何 UI 出现之前）
  └─ component::init()
       ├─ inventory::iter::<ComponentFn>()   遍历所有登记项
       ├─ 对每一项执行 (f.0)()               即调用 register_component::<T>()
       │    └─ 取 T 的静态信息（id/name/scope/status/preview 指针…）
       │       写入 COMPONENT_DATA（HashMap，键为 ComponentId）← 幂等
       └─ 此后 components() 才能查到组件
```

#### 4.1.3 源码精读

先看 `init()` 本体，它只有三行有效代码：

[crates/component/src/component.rs:L25-L29](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L29) —— `init()` 遍历 `inventory` 收集到的所有 `ComponentFn` 并逐个执行其中的函数指针。这就是「链接期登记、运行期执行」的第二阶段。

被遍历的类型和登记入口在同一文件里：

[crates/component/src/component.rs:L31-L39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L31-L39) —— `ComponentFn` 是一个包裹 `fn()` 指针的新类型，`inventory::collect!` 声明「本类型可以被分布式登记」，是整个机制的锚点。

每个被登记的函数最终都落到 `register_component`：

[crates/component/src/component.rs:L47-L61](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L47-L61) —— `register_component` 把 `Component` trait 的静态信息固化成一份 `ComponentMetadata`（注意 `preview: T::preview` 存的是函数指针），然后以 `id` 为键 `insert` 进全局表。**因为键是 `id`，重复注册同一个组件只是覆盖，所以 `init()` 多跑几次是安全的**——这是本讲实验结论的根源。

全局表本身是一个惰性初始化的读写锁：

[crates/component/src/component.rs:L63-L69](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L63-L69) —— `COMPONENT_DATA` 是 `LazyLock<RwLock<ComponentRegistry>>`，内部就是一个 `HashMap<ComponentId, ComponentMetadata>`。

而查询入口 `components()` 返回的是注册表的**克隆快照**：

[crates/component/src/component.rs:L21-L23](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L21-L23) —— `components()` 读一次锁并把整个注册表克隆出来返回。消费方拿到的是拍照那一刻的静态副本，之后即使再有组件注册，这份副本也不会更新——这决定了「必须先 init 再构造界面」的时序要求。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：不看运行结果，仅凭静态阅读确认「谁在运行期执行注册函数」以及「注册了多少东西」。
2. **操作步骤**：
   - 在仓库根目录执行 `rg "inventory::iter" crates/`，确认遍历 `ComponentFn` 的调用点只有 `component::init()` 一处。
   - 执行 `rg "derive\(.*RegisterComponent" crates/ --count-matches`，得到使用派生宏的文件数与出现次数（作为登记数量的下限；个别泛型组件走手动注册，不在此列）。
3. **需要观察的现象**：命令输出的文件清单横跨哪些 crate（如 `ui`、`settings_ui`、`project_panel` 等）。
4. **预期结果**：`inventory::iter` 的命中集中在 `crates/component/src/component.rs`；派生宏的使用分布在多个 crate。具体数字随 HEAD 变化，以你本地输出为准（待本地验证）。
5. 之所以把它设计成阅读型实践，是因为注册数量本身在持续增长，写死数字反而误导；重要的是建立「登记点分散、执行点唯一」的印象。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `component::init()` 的循环体改成执行两遍（例如外面再套一层循环），注册表会出问题吗？

**答案**：不会。`register_component` 以 `id` 为键做 `HashMap::insert`，重复执行只是用相同内容覆盖相同键，注册表条目数不变。这正是两条启动路径可以都调用 `init()` 而不出错的原因。

**练习 2**：`components()` 返回克隆快照意味着什么？假如某个组件在 `ComponentPreview` 构造之后才被注册，这个预览界面能看到它吗？

**答案**：看不到。`ComponentPreview::new` 在构造时调用一次 `components()` 并把结果存进字段，之后不再重新读取。快照之后的注册对已存在的实例不可见，所以 `init()` 必须发生在构造预览界面之前。

**练习 3**：`ComponentFn` 里存的是 `fn()`（无参数、无返回值的函数指针），为什么这样就够了？

**答案**：因为注册需要的全部信息都能从 trait 的关联函数静态取得——`register_component::<T>()` 里调用的是 `T::id()`、`T::name()`、`T::preview` 等无状态项。注册阶段不需要任何运行期上下文（没有 `cx`、没有 `window`），一个裸函数指针即可携带「去哪里取元数据」的全部信息。

### 4.2 component_preview example 的 main：一条独立的启动路径

#### 4.2.1 概念说明

`component_preview` 包的库部分（`src/component_preview.rs`）只是定义了 `ComponentPreview` 这个 workspace 条目和它的 `init()` 接线函数，它自己不会变成进程。要「看到」组件预览，需要有人准备好字体、主题、设置系统、HTTP 客户端、文件系统、会话、workspace 框架……最后开一个窗口。

example 二进制就是这份「有人」的手工版本：`examples/component_preview.rs` 用大约 150 行代码，把 Zed 主程序里通常由几千行初始化代码搭建的环境最小化地拼了一遍。它存在的意义是让组件预览可以脱离完整的 Zed 编辑器独立运行——改一个组件的样式，跑这个小例子就能看效果，不必启动整个编辑器。

Cargo.toml 里能看到这个 example 是被显式声明的：

[crates/component_preview/Cargo.toml:L48-L50](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L48-L50) —— `[[example]]` 段显式声明了名为 `component_preview` 的示例目标，这就是运行命令里 `--example component_preview` 的来源。

顺带注意 `dev-dependencies`（同文件 L42-L46）：字体资源（`assets`）、日志（`env_logger`）、平台后端（`gpui_platform`）等只有 example 需要的依赖被放在这里，库本身保持轻量。

#### 4.2.2 核心流程

`main` 的初始化链可以分成五段：

```text
main()
 ├─ ① env_logger 初始化（日志过滤到 Warn）
 ├─ ② gpui 应用启动，run 闭包内第一行：component::init()   ← 注册表填充
 ├─ ③ 基础设施：字体、HTTP 客户端、文件系统、settings、主题
 ├─ ④ 组装 AppState → workspace::init / editor::init / component_preview::init
 │      （workspace::init 内部又会调用一次 component::init()）
 └─ ⑤ cx.open_window(...)：
        ├─ 新建本地 Project 与 Workspace
        ├─ cx.new(|cx| ComponentPreview::new(...))   ← 此处读取注册表快照
        └─ workspace.add_item_to_active_pane(...)    ← 预览作为标签页加入
```

关键时序：⑤ 里 `ComponentPreview::new` 是第一次有人消费注册表，而它在②和④两次 `component::init()` 之后才执行——所以快照一定是满的。

#### 4.2.3 源码精读

运行命令写在文件顶部的模块文档里：

[crates/component_preview/examples/component_preview.rs:L1-L3](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L1-L3) —— 模块文档直接给出启动命令 `cargo run -p component_preview --example component_preview`。

进入 `run` 闭包后的第一件事就是填充注册表：

[crates/component_preview/examples/component_preview.rs:L31-L32](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L31-L32) —— 启动 GPUI 应用（挂载内嵌资源 `Assets`），闭包第一行调用 `component::init()`，先于字体、主题、窗口等一切后续步骤。

中段是基础设施装配，挑两行代表性的：

[crates/component_preview/examples/component_preview.rs:L50-L51](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L50-L51) —— 初始化设置系统和基础主题。组件预览界面的颜色、字体都依赖这套机制先就位。

[crates/component_preview/examples/component_preview.rs:L67-L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L67-L81) —— 组装 `AppState`（语言注册表、客户端、用户存储、文件系统、会话等）设为全局，随后依次调用 `workspace::init`、`editor::init` 和组件预览自己的 `init`。注意 `workspace::init` 内部也会调用 `component::init()`（见 4.3），所以到这一行为止注册函数已经被执行过两次——幂等性使这完全无害。

最后开窗口并把预览条目放进去：

[crates/component_preview/examples/component_preview.rs:L125-L137](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L125-L137) —— 在窗口回调里构造 `ComponentPreview`。它的构造函数第一步就是调用 `components()` 拿注册表快照（库侧对应代码见 4.3.3 最后一处引用）。

[crates/component_preview/examples/component_preview.rs:L139-L145](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L139-L145) —— `add_item_to_active_pane` 把预览作为条目加进活动窗格，它随即以标签页形式显示。example 走的是「直接构造并添加」，而不是派发 `OpenComponentPreview` 动作——动作这条路留给真正的 Zed 编辑器（见 4.3）。

#### 4.2.4 代码实践

**实践 A：运行并观察作用域分组**

1. **实践目标**：亲眼看到组件预览界面，并确认组件按 `ComponentScope` 分组展示。
2. **操作步骤**：
   - 在仓库根目录执行 `cargo run -p component_preview --example component_preview`（Linux 上需要图形环境；首次编译较久）。
   - 窗口打开后，在左侧导航找到分组 **Forms & Input**（对应 `ComponentScope::Input`）或 **Layout & Structure**（对应 `ComponentScope::Layout`）。
   - 数一数该分组下的组件条目数，截图保存。
3. **需要观察的现象**：左侧是带过滤输入框的分组列表，点进一个组件后内容区展示它的多个变体示例。
4. **预期结果**：Forms & Input 分组下应能看到 Button、IconButton、ToggleButton、Switch、DropdownMenu、NumberField 等条目。可用 grep 交叉验证：`rg "ComponentScope::Input" crates/ --glob '!**/*-tutorial/**'`，在当前 HEAD 下源码命中约 12 处（含 `ui/src/components/toggle.rs` 中 3 处，对应多个组件）；Layout & Structure 约 3 处。**grep 数与界面计数不一定严格相等**（个别组件可能未覆写 `scope()` 或经由别名注册），以你本地界面为准（待本地验证）。

**实践 B：注释掉 example 里的 `component::init()`**

1. **实践目标**：验证 4.1 的论断——`workspace::init` 内部还有一次 `component::init()`，且注册幂等，因此单独注释掉 example 第 32 行**不会**破坏预览。
2. **操作步骤**：
   - 把 [component_preview.rs:L32](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L32) 的 `component::init();` 注释掉，重新运行。
   - 恢复该行后，再做一个更彻底的实验：同时注释掉 [workspace.rs:L785](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L785) 的 `component::init();`（这需要改动 workspace 源码，请只在你自己的本地克隆里做，不要提交），重新运行 example。
3. **需要观察的现象**：第一组实验界面应与原来完全一致；第二组实验左侧导航应当几乎为空、无法进入任何组件页。
4. **预期结果**：
   - 实验 1：一切正常。原因是 example 第 79 行的 `workspace::init` 第一行就是 `component::init()`，它在窗口打开（第 86 行）之前执行，注册表照样被填满。example 第 32 行的显式调用是「提前 + 表意」性质的冗余。
   - 实验 2：注册表始终为空，`ComponentPreview::new` 拿到的快照里没有任何组件，导航列表为空。
   - 两组预期均基于静态推演，运行现象待本地验证。
5. 这个实验是本讲最重要的一课：**「必须先 init」约束的真正含义是「在第一个消费者读快照之前 init」，而不是「main 的第一行必须写 init」**。例子里有两道保险，都满足这个约束。

#### 4.2.5 小练习与答案

**练习 1**：example 为什么需要 `editor::init(cx)`？组件预览又不是编辑器。

**答案**：因为 `ComponentPreview` 的部分组件预览会渲染 `Editor`（例如输入类组件的示例），而 `Editor` 作为 workspace 条目需要在应用层初始化它的动作、设置等。初始化链是按「窗口里可能出现什么」来准备的，不是按「这个应用叫什么」。

**练习 2**：example 里 `NodeRuntime::unavailable()`、`Project::local(...)` 传了 `init_worktree_trust: false`，这些「降级」说明了什么设计取向？

**答案**：example 只需要 workspace 框架的容器能力（窗格、条目、动作分发），不需要真实的网络、节点运行时或受信工作区。把依赖降到最低可以让示例启动快、行为确定，也说明组件预览体系对宿主环境的真实要求其实很薄。

**练习 3**：如果我想让 example 窗口默认更大，改哪里？

**答案**：改 [component_preview.rs:L83-L85](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L83-L85) 的 `size(px(1200.), px(800.))` 与 `Bounds::centered`，这是纯 example 侧的展示参数，不影响库。

### 4.3 workspace::init：Zed 编辑器内的入口与动作接线

#### 4.3.1 概念说明

第二条启动路径属于真正的 Zed 编辑器。回顾上一讲的依赖反转：`workspace` crate 定义了 `OpenComponentPreview` 动作但不依赖 `component_preview`；`component_preview` 依赖 `workspace`，它的 `init()` 负责对每个新建的 workspace 注册这个动作的处理器；Zed 主程序在启动时调用 `component_preview::init()` 完成最后接线。

在这条路径上，`component::init()` 的调用点藏在 `workspace::init` 的**第一行**。这是一个很讲究的位置：任何把 `workspace` 纳入依赖的应用，只要调了 `workspace::init`，注册表就自动被填充——不需要每个宿主应用都记得自己调 `component::init()`。example 第 32 行的显式调用因此只是双保险。

还有一个实用细节：`OpenComponentPreview` 在默认键位表（`assets/keymaps/`）里**没有绑定快捷键**（在当前 HEAD 下搜索无结果），所以在 Zed 里打开组件预览的方式是命令面板里输入动作名 `workspace: open component preview`。

#### 4.3.2 核心流程

```text
Zed 主程序启动
 └─ zed/src/main.rs: component_preview::init(app_state, cx)
      ├─ workspace::register_serializable_item::<ComponentPreview>(cx)   ← 支持会话恢复
      └─ cx.observe_new(每个新建的 Workspace):
           └─ workspace.register_action(OpenComponentPreview 处理器)

用户按下命令面板 → 输入 workspace: open component preview
 └─ 处理器执行：
      ├─ cx.new(|cx| ComponentPreview::new(...))   ← 读注册表快照
      └─ workspace.add_item_to_active_pane(...)    ← 预览成为标签页

与此同时（更早发生）：
  workspace::init(app_state, cx)
   └─ 第一行：component::init()                    ← 注册表填充先于一切窗口
```

#### 4.3.3 源码精读

动作定义在 workspace 的动作列表里，只有一行文档加一个标识符：

[crates/workspace/src/workspace.rs:L306-L307](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L306-L307) —— `OpenComponentPreview` 动作的定义。`actions!` 宏会把它注册进 `workspace` 命名空间，命令面板里显示为 `workspace: open component preview`。

本讲的标题级代码——`workspace::init` 的开头：

[crates/workspace/src/workspace.rs:L784-L788](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L784-L788) —— `workspace::init` 的前几行，第一行就是 `component::init()`，随后是主题预览、toast 层、历史管理等其他初始化。把它放在第一行保证了「只要走了 workspace 初始化，注册表就已就绪」。

动作处理器的注册在 `component_preview` 库侧：

[crates/component_preview/src/component_preview.rs:L25-L65](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L25-L65) —— `component_preview::init` 做两件事：把 `ComponentPreview` 注册为可序列化条目（供会话恢复，细节留到第 4 单元）；通过 `cx.observe_new` 对每个新建的 workspace 调用 `register_action`，把 [L33-L34](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L33-L34) 处的处理器绑定到 `workspace::OpenComponentPreview` 上。处理器内容与 example 的窗口回调几乎相同：构造 `ComponentPreview` 并 `add_item_to_active_pane`。

Zed 主程序里的接线点：

[crates/zed/src/main.rs:L976-L978](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/zed/src/main.rs#L976-L978) —— 主程序在启动流程中调用 `component_preview::init`。这一行不在任何 `#[cfg(debug_assertions)]` 之类条件编译里，说明组件预览在正式发布的 Zed 里同样可用，是设计系统面向全团队的工具而非调试专用品。

最后是消费者一侧的时序证据：

[crates/component_preview/src/component_preview.rs:L114-L127](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L114-L127) —— `ComponentPreview::new` 的开头：第一行（L124）就调用 `components()` 克隆注册表快照，随后（L125）立即做排序。两条启动路径里，这个构造点都晚于各自的 `component::init()` 调用点——这就是「init 必须在组件可见之前」的具体含义。

#### 4.3.4 代码实践

1. **实践目标**：在 Zed 开发版里通过动作打开组件预览，并确认它与 example 是同一个 `ComponentPreview`。
2. **操作步骤**：
   - 构建并运行开发版 Zed（通常为 `cargo run -p zed`，具体平台差异见仓库 `docs/src/development.md`；待本地验证）。
   - 打开任意项目后，按出命令面板，输入 `component preview`，选择 **workspace: open component preview**。
   - 观察预览出现的形态（标签页 or 分割窗格），试着关掉它再打开。
3. **需要观察的现象**：预览以一个 workspace 条目的身份出现在活动窗格里；左侧导航的分组与 example 中一致。
4. **预期结果**：与 example 所见界面同源——因为两条路径最终都走到 `ComponentPreview::new` + `add_item_to_active_pane`，差别只在「谁触发构造」（example 直接构造 / Zed 由动作处理器构造）。此外可顺带在命令面板确认该动作无默认快捷键提示（待本地验证）。
5. **补充（源码阅读型）**：在 `crates/workspace/src/workspace.rs` 中搜索 `OpenComponentPreview`，确认它只出现两处相关位置——动作定义（L307）与其他动作并列；处理器注册则完全在 `component_preview` crate 里。这条搜索能亲手验证「依赖反转」不是一句空话。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `workspace::init` 要把 `component::init()` 放在第一行，而不是放在某个「打开窗口」的回调里？

**答案**：`workspace::init` 是所有宿主应用共用的初始化入口，把注册表填充放在它第一行，等于把时序约束下沉为框架保证：之后无论何时、以何种方式构造 `ComponentPreview`（动作触发、会话恢复反序列化），快照都已是满的。若放到回调里，就要保证回调先于所有消费者，时序更难推理。

**练习 2**：`component_preview::init` 里用的是 `cx.observe_new(...)` 加 `workspace.register_action(...)`，而不是在 `workspace::init` 里直接注册动作处理器。这样写的好处是什么？

**答案**：保持了依赖方向。`workspace` 不依赖 `component_preview`，所以不能在 `workspace::init` 里引用后者的类型；由依赖方（`component_preview`）观察新 workspace 的创建并自行注册处理器，动作类型（在 workspace 里定义）和处理逻辑（在 component_preview 里）得以分离部署。

**练习 3**：在 Zed 里关闭组件预览标签页后再用动作打开，会新建一个条目还是复用旧的？

**答案**：新建。动作处理器每次都 `cx.new(...)` 构造新的 `ComponentPreview` 实体再 `add_item_to_active_pane`。是否「聚焦已有同类条目」取决于 workspace 对 `add_item_to_active_pane` 的去重策略，对自定义条目类型默认不去重（待本地验证；会话恢复场景下则由第 4 单元将讲的持久化机制处理「找回上次的条目」）。

## 5. 综合实践

**任务：为两条启动路径各画一张时序图，并用一个实验证伪「删掉 example 的 init 也没事」的边界。**

1. 复现 4.2.4 实践 A：运行 example，记录 Forms & Input 与 Layout & Structure 两个分组的组件数，截图。
2. 执行 4.2.4 实践 B 的两个子实验，记录「只注释 example L32」与「同时注释 workspace.rs L785」两种情况下的界面差异。
3. 对照源码，在纸上或作图工具里画出两条路径的时序：
   - 路径一（example）：`component::init()`（L32）→ 基础设施 → `workspace::init`（内含第二次 `component::init()`）→ `component_preview::init` → `open_window` → `ComponentPreview::new`（读快照）。
   - 路径二（Zed）：`workspace::init`（内含 `component::init()`）→ …… → `component_preview::init`（注册动作）→ 用户触发动作 → `ComponentPreview::new`（读快照）。
4. 在图上用红笔标出「第一个读注册表的时刻」，检验两条路径中所有 `component::init()` 调用点是否都先于它。
5. 写一段 200 字以内的结论，回答：如果把 `component::init()` 从 `workspace::init` 里挪到 `workspace::open_window` 之类的更晚位置，哪些场景可能出问题？（提示：会话恢复时 `ComponentPreview` 的反序列化路径，见第 4 单元伏笔。）

预期成果：两张时序图 + 一份实验记录 + 一段结论。全程只改自己克隆里的代码，不改仓库。

## 6. 本讲小结

- 组件预览有两条启动路径：独立的 example 二进制（`cargo run -p component_preview --example component_preview`）和 Zed 编辑器内的 `workspace: open component preview` 动作；两者最终都走 `ComponentPreview::new` + `add_item_to_active_pane`。
- `component::init()` 遍历 `inventory` 登记的注册函数，把元数据以 `id` 为键写入全局 `COMPONENT_DATA`；写入幂等，调用多次无害。
- example 的 `main` 是一个手工组装的最小宿主：字体、设置、主题、HTTP、文件系统、`AppState`、`workspace::init`、`component_preview::init`，最后开窗口。
- `workspace::init` 的第一行就是 `component::init()`，把「注册表先就绪」变成框架级保证；example 第 32 行的显式调用是冗余的双保险——注释掉它预览照常工作。
- 真正的时序约束是：`component::init()` 必须早于第一个读注册表快照的消费者（`ComponentPreview::new` 的 `components()`），因为快照之后不会自动更新。
- `OpenComponentPreview` 无默认快捷键，在 Zed 中通过命令面板触发；主程序无条件调用 `component_preview::init`，组件预览不是调试专用品。

## 7. 下一步学习建议

下一讲（u1-l3）将转向 crate 的静态结构：`component` 只有 `component.rs` 与 `component_layout.rs` 两个源文件，`Cargo.toml` 用 `[lib] path` 指向非默认命名，`ui` crate 通过 `component_prelude` 与 `prelude` 把符号重导出给业务代码。建议先自己浏览 [crates/component/Cargo.toml](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml) 和 [crates/ui/src/component_prelude.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/component_prelude.rs)，带着「`use ui::prelude::*` 之后为什么就能写 `Component`」这个问题进入下一讲。若想提前深挖本讲的注册机制，可以直读 [crates/component/src/component.rs:L25-L61](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L61)，第 2 单元（u2-l3）会逐行拆解 `inventory` 的 collect/submit 细节。
