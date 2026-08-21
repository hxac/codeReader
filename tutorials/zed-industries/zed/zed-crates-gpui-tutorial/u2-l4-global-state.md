# u2-l4 Global 全局状态：把单例挂到 App 上

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 GPUI 里「全局状态」到底存在哪里、以什么形式存（`globals_by_type: TypeIdHashMap<Box<dyn Any>>`）。
2. 自己定义一个 `Global`，完成初始化（`set_global`）、读取（`global` / `try_global` / `ReadGlobal::global`）、修改（`global_mut` / `update_global`）。
3. 解释 `update_global` 为什么能在同一个闭包里同时给出 `&mut G` 和 `&mut cx`——这就是全局租约（`lease_global`）机制。
4. 用 `cx.observe_global` + `cx.notify()` 打通「全局变了 → 界面刷新」的链路，并知道修改全局本身**不会**自动触发重绘。
5. 能判断一个状态该做成 `Global` 还是独立 `Entity`。

## 2. 前置知识

- **单例（singleton）**：整个进程里只有一份的状态。比如应用主题、全局配置、服务注册表。GPUI 为每种类型各保留一个单例槽位。
- **`TypeId` 与 `Any`（Rust 标准库）**：`TypeId::of::<T>()` 给每个类型生成一个进程内唯一的编号，可以当哈希表的键用；`dyn Any` 是「类型被擦除的值」，配合 `downcast_ref::<T>()` 能把值还原回具体类型。u2-l2 里实体在 `EntityMap` 中以类型擦除方式存储，用的是同一套手法——全局状态只是把同样的思路搬到了一张以 `TypeId` 为键的哈希映射上。
- **借用规则**：同一时间，一个值要么有多个不可变引用（`&T`），要么只有一个可变引用（`&mut T`）。本讲 4.3 节的全部精妙设计都源于这条规则。
- **本单元已建立的模型**（默认你已完成 u2-l1 ~ u2-l3）：
  - `App` 是唯一的全局状态容器，前台代码都跑在单一前台线程上，`AppCell` 是 `RefCell<App>`（u2-l1）。
  - 实体的更新走「租约：把状态搬出表 → 修改 → 放回」（u2-l2 的 `lease` / `end_lease`）。
  - 副作用（`Notify`、`Emit` 等）先进入 `Effect` 队列，在最外层更新结束后的 `flush_effects` 中统一派发（u2-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/global.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/global.rs) | 仅 76 行的核心文件：定义 `Global`（标记 trait）、`ReadGlobal`、`UpdateGlobal` 三个 trait |
| [src/app.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs) | 存储本体（`globals_by_type`、`global_observers`）与全部固有读写方法、租约、`Effect` 处理 |
| [src/gpui.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs) | crate 根：`BorrowAppContext` trait（`update_global` 的统一入口）与 `AppContext::read_global` |
| [src/app/context.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs) | `Context<T>` 上的 `observe_global`（视图刷新的关键） |
| [src/app/async_context.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs) | `AsyncApp` 的全局读写变体（短暂借用模式） |
| [src/colors.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/colors.rs) | 真实全局示例一：`GlobalColors`（GPUI 默认配色） |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/elements/div.rs) | 真实全局示例二：`GroupHitboxes`（SVG 分组命中盒，`default_global` 懒初始化） |
| [examples/set_menus.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/set_menus.rs) | 可运行示例：用全局 `AppState` 驱动应用菜单 |
| [examples/text.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/text.rs) | 可运行示例：`GlobalTextContext` 演示 newtype + `Arc` 共享的全局封装模式 |
| [src/subscription.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/subscription.rs) | 全局观察者的单元测试（理解注销语义的最佳材料） |

## 4. 核心概念与源码讲解

### 4.1 Global trait：一个类型，一个全局单例

#### 4.1.1 概念说明

实体（u2-l2）适合存「有身份、可多实例」的状态：三个输入框就是三个实体。但应用里还有另一类状态——**整个进程只有一份**：当前主题色、应用级配置、字体加载器……如果为它们各造一个实体，就得先把 `Entity<T>` 句柄层层传递给所有使用者，非常繁琐。

GPUI 的答案是 `Global`：**每种类型在 `App` 上保留一个全局槽位**，任何代码拿到任意上下文（`App`、`Context<T>`、`AsyncApp`）都能直接读写，不需要传递句柄。

它的实现简单得出奇——`Global` 只是个空的标记 trait（marker trait，没有任何方法），作用是把「可以放进全局槽位」这件事变成编译期检查：没实现 `Global` 的类型调用 `cx.set_global(x)` 直接编译报错。

#### 4.1.2 核心流程

存取一个全局的完整流程：

1. 写入：把值装箱成 `Box<dyn Any>`（类型擦除），以 `TypeId::of::<G>()` 为键插入哈希映射。
2. 读取：用同一个 `TypeId` 查表，拿到 `&dyn Any` 后 `downcast_ref::<G>()` 还原成具体类型。
3. 因为键是 `TypeId`，**每个类型最多只有一份**——再 `set_global` 一次是覆盖，不是新增。
4. 想存两个形状相同的东西，必须用 newtype 包装成两个不同类型（见 4.2.3 的 `GlobalTextContext` 模式）。

#### 4.1.3 源码精读

先看 trait 本体：

- [src/global.rs:L22-L27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/global.rs#L22-L27) —— `Global` 是故意留空的标记 trait：只有 `G: Global` 的类型才能走全局存取方法，这就是全部的「机制」；功能由后面的扩展 trait 补充。
- [src/global.rs:L12-L21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/global.rs#L12-L21) —— 文档注释给出的进阶用法：把实现 `Global` 的结构设为私有，再用 newtype + 自定义访问器只暴露想要的操作（用 Rust 可见性限制全局的读写权限）。

再看存储本体，它就是 `App` 结构体上的一个普通字段：

- [src/app.rs:L716-L720](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L716-L720) —— `globals_by_type: TypeIdHashMap<Box<dyn Any>>`：以类型编号为键、类型擦除的装箱值为值的哈希映射（`TypeIdHashMap` 来自 Zed 的 `collections` crate）。字段上方的注释说明它被刻意放在字段列表最后声明，保证 `App` 析构时全局状态最后 drop。
- [src/app.rs:L705](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L705) —— `global_observers: SubscriberSet<TypeId, Handler>`：每种全局的观察者回调集合，4.4 节的主角。

读取侧的三个固有方法（都在 `impl App` 上，不需要导入任何 trait）：

- [src/app.rs:L1953-L1955](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1953-L1955) —— `has_global::<G>()`：只查键是否存在。
- [src/app.rs:L1957-L1964](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1957-L1964) —— `global::<G>()`：查表 + `downcast_ref` 还原；没设置过就 panic，错误信息里带类型名（`#[track_caller]` 让 panic 指向调用处而不是框架内部）。
- [src/app.rs:L1966-L1971](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1966-L1971) —— `try_global::<G>()`：返回 `Option<&G>`，全局可能不存在时的安全版本。

GPUI 内部真实的全局有哪些？grep `impl Global for` 可以看到：

- [src/colors.rs:L78-L89](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/colors.rs#L78-L89) —— `GlobalColors(pub Arc<Colors>)`：GPUI 的默认配色。初始化入口在 [src/app.rs:L2706-L2711](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2706-L2711) 的 `App::init_colors`，它就是一句 `set_global`；示例 [examples/text.rs:L366](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/text.rs#L366) 会调用它。
- [src/app.rs:L364-L385](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L364-L385) —— `SystemWindowTabController`：GPUI 自己管理的系统级窗口标签页控制器，带一个 `init(cx)` 帮手函数，内部同样是一句 `set_global`。
- [src/elements/div.rs:L3838-L3857](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/elements/div.rs#L3838-L3857) —— `GroupHitboxes`：SVG 分组命中盒注册表，用 `default_global`（不存在则插入 `Default` 值）实现懒初始化，是「框架内部悄悄用全局」的例子。

> **勘误（以源码为准）**：本讲规划时曾把 `FocusMap`（焦点表）也当成全局单例的例子。实际核查后它**不是** `Global`——它是 [src/app.rs:L689](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L689) 上 `focus_handles: Arc<FocusMap>` 这个普通字段（`FocusMap` 定义在 [src/window.rs:L472](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/window.rs#L472)）。这提醒我们：判断一个状态存在哪里，别靠名字猜，去 `App` 结构体里看字段。

#### 4.1.4 代码实践

1. **实践目标**：跑通一个「全局状态驱动界面」的真实示例，确认全局的读与写分别长什么样。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo run -p gpui --example set_menus`（Linux 上需确保已启用 wayland/x11 平台特性，这是 gpui 的 default features，通常无需额外操作）。
   - 打开应用菜单，在 `Mode` 子菜单里切换 `List` / `Grid`，观察菜单项前面的勾选标记变化。
   - 对照源码 [examples/set_menus.rs:L79-L91](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/set_menus.rs#L79-L91)（`AppState` + `impl Global for AppState {}`）、[L93-L113](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/set_menus.rs#L93-L113)（`set_app_menus` 用 `cx.global::<AppState>()` 读状态决定勾选）、[L124-L128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/set_menus.rs#L124-L128)（`toggle_check` 用 `cx.global_mut::<AppState>()` 改状态后重建菜单）。
3. **需要观察的现象**：点击菜单项 → 菜单重新弹出时勾选位置变了；没有任何视图持有 `AppState` 的句柄，菜单读写它全靠 `cx`。
4. **预期结果**：状态唯一存放在全局槽位中；如果愿意，可在 `toggle_check` 里临时加一行 `eprintln!("view_mode -> {:?}", app_state.view_mode);` 再跑一次验证（学习性修改，验证完请还原，不要提交）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Global` trait 一个方法都没有也能起作用？
**答案**：它是标记 trait，作用是给类型打上「可以作为全局」的标签。所有存取方法都以 `G: Global` 为泛型约束（如 [src/app.rs:L1959](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1959)），没实现 `Global` 的类型在编译期就被挡住，避免任意类型误入全局命名空间。

**练习 2**：`TypeIdHashMap<Box<dyn Any>>` 里，`Box<dyn Any>` 和 `TypeId` 各解决什么问题？
**答案**：`Box<dyn Any>` 解决「值的类型各不相同但要放进同一张表」——装箱后类型擦除，读出时再 `downcast_ref::<G>()` 还原；`TypeId` 解决「用什么当键」——同一类型每次 `TypeId::of::<G>()` 结果相同且进程内唯一，天然是单例槽位的键。

**练习 3**：如果我想存两份「相同字段结构」的全局配置，直接 `set_global` 两次行吗？
**答案**：不行。键是 `TypeId`，同类型第二次 `set_global` 会覆盖第一次。正确做法是定义两个 newtype（比如 `struct ProdConfig(Config); struct DevConfig(Config);`）分别实现 `Global`，让它们成为两个不同类型、占两个槽位。

### 4.2 读写接口：ReadGlobal、UpdateGlobal 与 BorrowAppContext

#### 4.2.1 概念说明

GPUI 给全局状态准备了两套外观不同的 API：

| 风格 | 写法 | 需要导入 |
| --- | --- | --- |
| 上下文固有方法 | `cx.global::<T>()`、`cx.set_global(x)`、`cx.global_mut::<T>()` | 无（`App` 的固有方法） |
| 类型侧扩展 trait | `T::global(cx)`、`T::set_global(x, cx)`、`T::update_global(cx, \|t, cx\| ...)` | `ReadGlobal` / `UpdateGlobal`（不在 prelude 中，需显式 `use`） |

第二种风格的好处是读代码时主语是类型（`ThemeConfig::update_global(...)` 一眼看出在改哪个全局），被 Zed 大量使用。

这里出现了两个新术语：

- **blanket implementation（覆盖实现）**：`impl<T: Global> ReadGlobal for T` 表示「为所有实现了 `Global` 的类型自动实现 `ReadGlobal`」——你只需写 `impl Global for MyType {}` 一行，整套读写方法就都有了。
- **`BorrowAppContext`**：一个中间层 trait，让 `App`、`Context<T>` 等不同上下文都能用同一份 `set_global` / `update_global` 实现。

#### 4.2.2 核心流程

调用 `ThemeConfig::update_global(cx, f)` 时的解析路径：

1. `UpdateGlobal` 是对 `T: Global` 的 blanket impl，所以 `ThemeConfig` 天然获得该方法。
2. 方法体转调 `cx.update_global(update)`，这里的 `cx` 泛型约束是 `C: BorrowAppContext`。
3. `BorrowAppContext` 又是对所有 `C: BorrowMut<App>` 的 blanket impl——标准库自带 `impl BorrowMut<T> for T`，所以 `App` 天然满足；`Context<T>` 则由 GPUI 手动实现（见下）。
4. 于是同一份实现覆盖了 `App` 和 `Context<T>` 两种最常见的上下文；`AsyncApp` 因为只持弱引用、不能长期借用 `App`，单独提供了一套「短暂借用」的变体方法。

#### 4.2.3 源码精读

- [src/global.rs:L30-L41](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/global.rs#L30-L41) —— `ReadGlobal`：`fn global(cx: &App) -> &Self`，blanket impl 直接转调 `cx.global::<T>()`（未初始化同样 panic）。
- [src/global.rs:L44-L75](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/global.rs#L44-L75) —— `UpdateGlobal`：`update_global` 的闭包**同时拿到 `&mut Self` 和 `&mut C`**（这是 4.3 节租约机制存在的理由）；`set_global` 覆盖旧值。
- [src/gpui.rs:L300-L311](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L300-L311) —— `BorrowAppContext` trait 定义：`set_global` / `update_global` / `update_default_global` 三个方法。
- [src/gpui.rs:L313-L339](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L313-L339) —— 对 `C: BorrowMut<App>` 的 blanket impl。`update_global` 的三行实现（租出 → 执行闭包 → 归还）就是 4.3 节的全部内容。
- [src/app/context.rs:L873-L882](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L873-L882) —— `Context<T>` 手动实现 `Borrow<App>` / `BorrowMut<App>`，从而使上面的 blanket impl 覆盖到实体更新闭包里的 `cx`。
- [src/prelude.rs:L5-L9](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/prelude.rs#L5-L9) —— prelude 里包含了 `BorrowAppContext`（所以 `cx.update_global(...)` 直接可用），但**没有** `ReadGlobal` / `UpdateGlobal`——用类型侧风格要自己 `use gpui::{ReadGlobal, UpdateGlobal};`。
- [src/gpui.rs:L241-L244](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L241-L244) 与 [src/app.rs:L2829-L2835](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2829-L2835) —— `AppContext` trait 的 `read_global`：以闭包借出全局的只读视图，所有上下文（含异步）都实现。
- [src/app/async_context.rs:L221-L262](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L221-L262) —— `AsyncApp` 的一组变体：`read_global` / `try_read_global`（不 panic）/ `read_default_global` / `update_global`。注意 [L258-L262](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L258-L262) 的实现：短暂 `borrow_mut` 借出 `App`，转调同步版本后立刻归还——这正是 u2-l3 讲过的「异步上下文每次调用短暂借用」模式。

一个值得学的封装模式来自 text 示例：

- [examples/text.rs:L40-L57](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/text.rs#L40-L57) —— `GlobalTextContext(pub Arc<TextContext>)`：全局里存的不是裸数据而是 `Arc`，再配合 `Deref`/`DerefMut`，让使用侧 `cx.global::<GlobalTextContext>().0` 拿到的是可廉价克隆的共享句柄。当全局状态需要被许多地方**共享只读引用**时，这个模式能避免每次读取都碰全局表。
- [examples/text.rs:L366-L367](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/text.rs#L366-L367) —— 初始化位置：`run_example` 的 `run` 回调里、开窗口之前，连续调用 `cx.init_colors()`（设置 `GlobalColors`）和 `cx.set_global(GlobalTextContext(...))`。

#### 4.2.4 代码实践

1. **实践目标**：亲手完成「定义全局 → 初始化 → 读取」的最小闭环。
2. **操作步骤**（以下为**示例代码**，基于 [examples/hello_world.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs) 改写）：

   ```rust
   use gpui::{App, Global, ReadGlobal, rgb};

   struct ThemeConfig {
       background: gpui::Rgba,
   }

   impl Global for ThemeConfig {}

   fn init(cx: &mut App) {
       cx.set_global(ThemeConfig { background: rgb(0x2e3440) });
   }

   // 读取侧：类型侧风格（需要导入 ReadGlobal）
   fn current_background(cx: &App) -> gpui::Rgba {
       ThemeConfig::global(cx).background
   }

   // 读取侧：上下文风格（不需要任何 trait 导入）
   fn current_background_2(cx: &App) -> gpui::Rgba {
       cx.global::<ThemeConfig>().background
   }
   ```

   把 `init` 放进 `application().run` 回调的开头（开窗口之前），再让 `HelloWorld::render` 里的 `.bg(...)` 用 `current_background(_cx)` 的返回值（render 的第三个参数就是 `&mut Context<Self>`，可当 `&App` 用，见 u2-l3 的 Deref 关系）。
3. **需要观察的现象**：窗口背景变成你设置的颜色。
4. **预期结果**：两种读取风格拿到的值一致。如果注释掉 `init` 里的 `set_global`，运行时会 panic 并提示 `no state of type ThemeConfig exists`——这正是 4.1.3 中 `global()` 的 panic 分支。

#### 4.2.5 小练习与答案

**练习 1**：`UpdateGlobal::update_global` 的闭包签名是 `FnOnce(&mut Self, &mut C) -> R`，为什么必须把 `cx` 也递进闭包？
**答案**：修改全局时经常需要顺带做别的事（读另一个全局、spawn 任务、通知实体）。如果只给 `&mut Self`，闭包里就拿不到任何上下文。而同时给出 `&mut G` 和 `&mut C` 在借用规则下本不可能（G 住在 C 里），所以框架需要 4.3 节的租约机制来实现它。

**练习 2**：`BorrowAppContext` 为什么要存在？直接在 `App` 上写 `update_global` 不行吗？
**答案**：行，但那样 `Context<T>` 就得各自再写一遍（或靠 Deref 转发，返回值和闭包参数类型会对不上）。用一个以 `BorrowMut<App>` 为约束的 blanket trait，一份实现同时覆盖 `App`、`Context<T>` 等所有能可变借出 `App` 的上下文，是 Rust 里典型的能力组合手法。

**练习 3**：在 `AsyncApp` 里能用 `ThemeConfig::global(cx)` 吗？
**答案**：不能直接用——`ReadGlobal::global` 需要 `&App`，而 `AsyncApp` 只持弱引用。应改用 `AsyncApp` 自己的 `read_global` / `try_read_global`（闭包内短暂借用），见 [src/app/async_context.rs:L221-L239](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/async_context.rs#L221-L239)。

### 4.3 全局租约 lease_global：update_global 如何不违反借用规则

#### 4.3.1 概念说明

Rust 规定：同一个值不能同时存在两个可变引用。而 `update_global` 想同时给出：

- `&mut G`——你要修改的全局；
- `&mut C`（最终是 `&mut App`）——上下文。

问题是 `G` 就装在 `App` 内部的 `globals_by_type` 里，从同一条所有权链上同时借出两个 `&mut`，编译器必然拒绝。

GPUI 的解法与 u2-l2 实体更新用的租约完全同构：**把值搬出去，用完再放回来**。调用 `update_global` 时，全局的 `Box` 被从哈希表里 `remove` 出来，搬到栈上的 `GlobalLease` 包装里；此刻表里不再有这个全局，`&mut lease`（Deref 到 `&mut G`）和 `&mut App` 就互不冲突了。闭包结束后，`Box` 被 `insert` 回表里，并顺手登记一条「通知观察者」的副作用。

#### 4.3.2 核心流程

```
update_global::<G>(cx, f):
    lease = cx.globals_by_type.remove(TypeId::of::<G>())   # 租出：值离开全局表
    result = f(&mut lease, cx)                             # 表里暂时没有 G，
                                                           # 所以 &mut G 与 &mut cx 不冲突
    cx.globals_by_type.insert(TypeId::of::<G>(), lease)    # 归还：值回到全局表
    push_effect(NotifyGlobalObservers { G })               # 登记通知（4.4 节）
    return result
```

一个直接推论：**租约存续期间，全局表里查不到 `G`**。如果在 `f` 里再写 `cx.global::<G>()` 读自己，会命中 `global()` 的 panic 分支。闭包参数 `&mut G` 是这段时间内访问该全局的唯一途径。

#### 4.3.3 源码精读

- [src/gpui.rs:L322-L330](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L322-L330) —— `BorrowAppContext::update_global` 的实现，三步与上面的伪代码一一对应：`lease_global` → `f(&mut global, self)` → `end_global_lease`。
- [src/app.rs:L2037-L2046](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2037-L2046) —— `App::lease_global`：从表里 `remove` 出 `Box<dyn Any>`，不存在则 panic；包成 `GlobalLease` 返回。
- [src/app.rs:L2048-L2054](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2048-L2054) —— `App::end_global_lease`：`insert` 回表，并 `push_effect(Effect::NotifyGlobalObservers)` 登记通知。
- [src/app.rs:L2877-L2904](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2877-L2904) —— `GlobalLease<G>` 的定义：内部就是 `Box<dyn Any>` 加一个 `PhantomData<G>`（让包装类型带上 `G` 的类型信息），`Deref` / `DerefMut` 里做 `downcast_ref` / `downcast_mut`。所以 `f(&mut lease, ...)` 里的 `&mut G` 其实是 Deref 的产物。
- 对照组：[src/app.rs:L1973-L1982](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1973-L1982) —— `global_mut` **不使用租约**，值留在表里直接 `get_mut`，因此只返回 `&mut G`、拿不到 `cx`，但同样会 `push_effect` 登记通知。什么时候用哪个？不需要碰 `cx` 就用 `global_mut`（更便宜），需要同时操作上下文就用 `update_global`。
- 另外两个写入口：[src/app.rs:L1984-L1994](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1984-L1994) `default_global`（无则插入 `Default`，框架内部的 `GroupHitboxes` 靠它懒初始化）；[src/app.rs:L2009-L2019](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2009-L2019) `remove_global`（取出并返回值，同样登记通知）。

#### 4.3.4 代码实践

1. **实践目标**：用两个小实验验证租约机制的边界。
2. **操作步骤**（**示例代码**，接 4.2.4 的 `ThemeConfig`）：

   实验一——在 `update_global` 闭包里同时使用 `cx` 与 `&mut G`：

   ```rust
   use gpui::UpdateGlobal;

   ThemeConfig::update_global(cx, |theme, cx| {
       theme.background = rgb(0x111111);
       // 同时读另一个全局（或 cx.spawn / 其他上下文操作）都能编译通过：
       let colors = cx.default_colors().clone();
       println!(" GPUI 默认背景色是 {:?}", colors.background);
   });
   ```

   实验二——在闭包里读自己（预期 panic，理解机制后删除）：

   ```rust
   ThemeConfig::update_global(cx, |theme, cx| {
       theme.background = rgb(0x111111);
       // 租约期间值不在全局表里，下面这行会 panic:
       let _ = cx.global::<ThemeConfig>();
   });
   ```
3. **需要观察的现象**：实验一正常打印且修改生效；实验二在运行时 panic，信息形如 `no state of type ThemeConfig exists`。
4. **预期结果**：实验二的行为待本地验证（结论来自 [src/app.rs:L2039-L2045](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2039-L2045) 的 `remove` + `unwrap` 路径推得；跑一次确认即可）。验证后删除实验二。

#### 4.3.5 小练习与答案

**练习 1**：`global_mut` 和 `update_global` 都能改全局，核心差异是什么？
**答案**：`global_mut` 值不出表、只返回 `&mut G`，拿不到 `cx`；`update_global` 通过租约把值搬出表，闭包同时拿到 `&mut G` 和 `&mut C`。两者都会登记 `NotifyGlobalObservers` 副作用。

**练习 2**：`GlobalLease` 为什么需要 `PhantomData<G>`？
**答案**：内部存储是 `Box<dyn Any>`，本身不携带 `G` 的类型信息；`PhantomData<G>` 让包装类型的签名带上 `G`，这样 `Deref::Target = G` 的 downcast 才类型安全，外部也无法把它当成别的类型的租约使用。

**练习 3**：把本节的租约与 u2-l2 实体更新的租约做个对比，说出一个相同点和一个不同点。
**答案**：相同点——都是「把状态搬出容器 → 修改 → 放回」以规避同时可变借用，嵌套/重入都会出问题。不同点——实体租约的目的一是允许构造期自引用、二是让更新闭包拿到 `Context<T>`；全局租约的目的单纯是让 `&mut G` 与 `&mut App` 共存，且归还时才登记通知副作用。

### 4.4 observe_global 与通知效果：改完全局，谁来刷新

#### 4.4.1 概念说明

最容易被初学者误解的一点：**修改全局不会自动触发任何重绘**。`set_global` / `global_mut` / `update_global` 只做两件事——改值、登记一条「通知全局观察者」的副作用。真正让界面动起来，需要有人注册了观察者，并在观察者回调里调用 `cx.notify()`。

这延续了 u2-l3 的效果队列模型：副作用不在写入点立即执行，而是排队等待最外层更新结束后的 `flush_effects` 统一派发。好处有两个：

1. **去重**：同一次更新里把一个全局改了 N 次，观察者只在周期末收到一次通知；
2. **一致性**：观察者被调用时，所有状态修改已完成，不会看到「改了一半」的中间态。

#### 4.4.2 核心流程

```
写入点（set_global / global_mut / end_global_lease / remove_global）
    │ push_effect(NotifyGlobalObservers { TypeId })
    │   └─ 若该 TypeId 已在 pending_global_notifications 中 → 直接丢弃（去重）
    ▼
最外层 App::update 结束 → flush_effects()
    │ 逐条弹出 Effect，循环直到队列清空
    ▼
apply_notify_global_observers_effect(TypeId)
    │ 从去重集合移除该 TypeId
    │ 遍历 global_observers[TypeId] 中的每个回调
    ▼
观察者回调执行（视图的回调里应调用 cx.notify() 触发重绘）
```

#### 4.4.3 源码精读

- [src/app.rs:L2838-L2860](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2838-L2860) —— `Effect` 枚举：全局通知是其中第 4 个变体 `NotifyGlobalObservers { global_type: TypeId }`，与实体通知 `Notify` 平级。
- [src/app.rs:L1606-L1622](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1606-L1622) —— `push_effect` 的去重：`NotifyGlobalObservers` 先查 `pending_global_notifications`（`FxHashSet<TypeId>`），已在集合中就直接 return——同一更新周期内重复通知被吞掉，观察者只触发一次。
- [src/app.rs:L1624-L1661](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1624-L1661) —— `flush_effects`：循环弹出并派发各类效果，效果可以再产生效果，直到队列清空（注释明说了这一点）。
- [src/app.rs:L1759-L1764](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1759-L1764) —— `apply_notify_global_observers_effect`：清掉去重标记，然后对该 `TypeId` 的观察者集合逐个调用。`retain` + 回调返回布尔值的写法意味着：回调返回 `false` 即自动注销该观察者。
- [src/app.rs:L2021-L2035](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L2021-L2035) —— `App::observe_global`：注册观察者。注意最后的 `self.defer(...)` 激活——订阅先登记、延迟到当前更新结束才生效，避免注册当刻就被已排队的通知触发。
- [src/app/context.rs:L175-L190](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L175-L190) —— **视图刷新的标准写法**：`Context<T>::observe_global` 把弱实体句柄捕获进回调；全局变化时升级句柄、`update` 对应实体并调用你给的 `f(&mut T, &mut Context<T>)`。实体已被释放时 `upgrade` 失败，闭包返回 `false`，订阅自动注销（这正是 `retain` 布尔返回值的用途）。**你的 `f` 里必须调用 `cx.notify()`，否则视图不会重绘**——这是初学者最常踩的坑。
- [src/window.rs:L5664-L5682](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/window.rs#L5664-L5682) —— `Window::observe_global`：第三个变体，全局变化时更新整个窗口（拿到 `&mut Window`），适合需要重算窗口级状态的场景。
- [src/subscription.rs:L207-L256](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/subscription.rs#L207-L256) —— 单元测试 `test_unsubscribe_during_callback_with_insert`：观察者 A 在回调里 drop 自己并注册新观察者，断言之后同一全局再变化时 A、B 都不再触发。这个测试完整演示了订阅的生命周期语义（返回的 `Subscription` 被 drop 即注销，`detach()` 可延长生命）。

**Global 还是 Entity？** 选型参考：

| 维度 | 用 `Global` | 用 `Entity` |
| --- | --- | --- |
| 实例数量 | 进程内唯一（每类型一份） | 任意多份，各有 `EntityId` |
| 访问方式 | 任意上下文直接读写，无需传句柄 | 需要 `Entity<T>` 句柄（或全局里存句柄） |
| 生命周期 | 随 `App` 存亡 | 随引用计数，可动态创建销毁 |
| 变更通知 | `observe_global`（无载荷，按类型） | `observe`（按实体）+ `emit`/`subscribe`（带事件载荷） |
| 典型用途 | 主题、配置、服务注册表、字体加载器 | 编辑器缓冲区、面板、列表项 |

经验法则：**先问「会不会有多份」**。只有一份且被四面八方使用 → Global；现在或将来有多份 → Entity。两者常组合使用：全局里存 `Arc` 共享数据（`GlobalTextContext` 模式）或存关键实体的句柄。

#### 4.4.4 代码实践

1. **实践目标**：打通「全局变化 → 视图自动重绘」链路，并读懂订阅注销语义。
2. **操作步骤**：
   - 在 4.2.4 的 hello_world 改造中，给 `HelloWorld` 增加构造期订阅（**示例代码**）：

     ```rust
     // HelloWorld::new / cx.new 闭包内（_subscriptions: Vec<Subscription> 存到结构体字段里）
     cx.observe_global::<ThemeConfig>(|_this, cx| {
         println!("主题变了，请求重绘");
         cx.notify();
     })
     .detach();
     ```

     （学习示例中可以 `detach()`；正式代码建议存入 `_subscriptions` 字段，随实体销毁自动注销。）
   - 给窗口加一个点击区域，`on_click` 里执行 `ThemeConfig::update_global(cx, |theme, _| { theme.background = ...; })` 切换颜色。
   - 运行 `cargo test -p gpui test_unsubscribe_during_callback_with_insert`，阅读 [src/subscription.rs:L207-L256](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/subscription.rs#L207-L256) 对照断言理解。
3. **需要观察的现象**：点击后终端打印「主题变了」且背景色立刻更新；连续快速点击多次（同一更新周期内的多次修改会被去重）观察者也不应看到中间态。
4. **预期结果**：删除 `cx.notify()` 这行后再点击——终端仍会打印（观察者被调用了），但背景色**不再更新**（没有重绘请求）。这个对照实验是理解「通知」与「重绘」两阶段的最好方式。

#### 4.4.5 小练习与答案

**练习 1**：同一次 `App::update` 里调用三次 `update_global::<G>`，观察者会被调用几次？为什么？
**答案**：一次。每次归还租约都 `push_effect(NotifyGlobalObservers)`，但 [push_effect 的去重逻辑](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app.rs#L1613-L1617) 发现该 `TypeId` 已在 `pending_global_notifications` 集合中就直接丢弃，周期末只派发一次。

**练习 2**：为什么 `Context<T>::observe_global` 的观察者在实体释放后不会变成悬垂回调？
**答案**：回调捕获的是 `WeakEntity<T>`，触发时先 `upgrade`；实体已释放则升级失败，闭包返回 `false`，`SubscriberSet::retain` 随即将其自动注销（见 [src/app/context.rs:L184-L187](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/context.rs#L184-L187)）。

**练习 3**：一个视图只在 `render` 里读 `ThemeConfig::global(cx)` 而不注册任何观察者，主题切换后它会更新吗？
**答案**：不会。修改全局只登记观察者通知，不产生重绘请求；没有观察者（或观察者不调 `cx.notify()`），这个视图的 `render` 永远不会被再次调用，读到的仍是旧值。

## 5. 综合实践

把本讲全部知识点串成一个可运行的主题切换器。以下是完整指导（基于 [examples/hello_world.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/hello_world.rs) 的骨架，**全部为主题相关代码为示例代码**）：

**任务**：窗口背景与文字颜色由一个全局 `ThemeConfig` 决定；点击按钮在深色 / 浅色两套主题间切换，界面即时刷新，并在界面某处显示当前主题名。

**步骤**：

1. 复制 `examples/hello_world.rs` 为 `examples/theme_switch.rs`，并在 [Cargo.toml](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml) 的 examples 段照抄一个条目（本仓库示例是显式声明的，格式见 [Cargo.toml:L177-L180](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L177-L180)）。也可以直接在 `hello_world.rs` 上改（学习用途，勿提交）。
2. 定义全局（4.1 / 4.2）：

   ```rust
   use gpui::{App, Global, ReadGlobal, UpdateGlobal, cx_aware? /* 无此宏，仅示意 */, ...};

   #[derive(Clone, Copy, PartialEq)]
   enum Theme {
       Dark,
       Light,
   }

   struct ThemeConfig {
       theme: Theme,
   }

   impl ThemeConfig {
       fn background(&self) -> gpui::Rgba {
           match self.theme {
               Theme::Dark => rgb(0x2e3440),
               Theme::Light => rgb(0xf4f5f5),
           }
       }
       fn text_color(&self) -> gpui::Rgba {
           match self.theme {
               Theme::Dark => rgb(0xffffff),
               Theme::Light => rgb(0x252525),
           }
       }
   }

   impl Global for ThemeConfig {}
   ```

3. 在 `application().run` 回调里、`open_window` 之前初始化：`cx.set_global(ThemeConfig { theme: Theme::Dark });`（初始化时机参照 [examples/text.rs:L366-L367](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/text.rs#L366-L367)）。
4. 在根视图的 `render` 里用 `ThemeConfig::global(cx)` 读取颜色（`Context<Self>` 可 Deref 成 `&App`），替换 hello_world 原来的硬编码 `rgb(...)`，并加一个 `.child(...)` 显示 `format!("theme: {:?}", ThemeConfig::global(cx).theme)`。
5. 在视图构造处注册观察者并请求重绘（4.4）：

   ```rust
   cx.observe_global::<ThemeConfig>(|_, cx| cx.notify()).detach();
   ```

6. 加一个可点击元素切换主题（`on_click` 需要 `InteractiveElement`，且元素要有 `.id()` 才能用 `on_click`，这是 u5 的内容，照抄即可）：

   ```rust
   div()
       .id("toggle-theme")
       .on_click(cx.listener(|_, _, _, cx| {
           ThemeConfig::update_global(cx, |config, _| {
               config.theme = match config.theme {
                   Theme::Dark => Theme::Light,
                   Theme::Light => Theme::Dark,
               };
           });
       }))
       // ... 样式
   ```

7. 从仓库根目录运行：`cargo run -p gpui --example theme_switch`。

**需要观察的现象**：点击按钮 → 背景 / 文字颜色立刻互换，主题名同步变化；如果第 5 步被注释掉，点击后毫无视觉变化。

**预期结果**：完整链路 `on_click → update_global（租约修改）→ push_effect（去重）→ flush_effects → observe_global 回调 → cx.notify() → 下一帧 render 重新读取全局`。若你把 4.3.4 的 eprintln 实验加进 `update_global` 闭包，还能观察到通知发生在租约归还之后。

**常见故障排查**：

- 启动即 panic `no state of type ThemeConfig exists` → 忘了第 3 步的 `set_global` 初始化；
- 点击无反应但终端有打印 → 观察者里忘了 `cx.notify()`（对照 4.4.4 实验二）；
- 点击直接 panic → 你可能在 `update_global` 闭包里又用 `cx.global::<ThemeConfig>()` 读了自己（租约期间不在表里，4.3.4 实验二）。

## 6. 本讲小结

- `Global` 是空的标记 trait；全局状态存在 `App.globals_by_type: TypeIdHashMap<Box<dyn Any>>`，每种类型一个槽位，键是 `TypeId`，值装箱类型擦除。
- 读有三层：`cx.global`（panic）/ `cx.try_global`（Option）/ `ReadGlobal::global(cx)`（类型侧风格）；写有 `set_global`、`global_mut`、`update_global`、`default_global`、`remove_global`。
- `update_global` 靠**全局租约**同时给出 `&mut G` 与 `&mut cx`：`lease_global` 把 `Box` 移出表 → 闭包执行 → `end_global_lease` 放回并登记通知副作用；租约期间表里查不到该全局。
- 修改全局**不会自动重绘**：变化以 `NotifyGlobalObservers` 效果进入队列（按 `TypeId` 去重），在最外层更新的 `flush_effects` 中派发给 `global_observers`；视图要刷新必须 `observe_global` + `cx.notify()`。
- `AsyncApp` 不实现 `BorrowAppContext`，要用它自己的 `read_global` / `try_read_global` / `update_global`（短暂借用模式）。
- 选型：进程唯一、处处使用的状态用 `Global`；多实例、有生命周期、需要事件载荷的状态用 `Entity`；全局里存 `Arc` 或实体句柄是常见的组合手法。

## 7. 下一步学习建议

- 下一讲 **u2-l5（GPUI 并发模型：executor 与 Task）**：本讲的 `Effect` 队列由前台执行器驱动，`cx.spawn` 的异步闭包正是效果队列的重要生产者，两讲合起来才是完整的状态更新闭环。
- 继续阅读源码：
  - [src/subscription.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/subscription.rs) 里剩余几个全局观察者测试（L258 起），覆盖「回调注销回调」等边界。
  - [src/app/test_app.rs:L109-L118](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/app/test_app.rs#L109-L118) 的 `TestApp::update`——它会自动 `run_until_parked`，等于替你执行了 `flush_effects`，是写全局状态测试的利器（u7-l4 展开）。
  - 到 Zed 主程序仓库里搜索 `impl Global for`，观察真实应用如何用全局组织主题、设置与服务（如 `ThemeRegistry`、`SettingsStore`）。
- 带着问题前进：如果一个全局要在多个窗口间共享，观察者注册在哪个窗口会怎样？（提示：`Window::observe_global` 的窗口句柄捕获，以及 u7-l2 的多窗口实体模型。）
