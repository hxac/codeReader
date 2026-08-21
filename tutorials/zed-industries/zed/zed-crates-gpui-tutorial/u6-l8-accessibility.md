# 无障碍访问：AccessKit 集成

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 GPUI 与 AccessKit 的集成架构：无障碍树如何随每帧绘制同步构建、何时发送给系统、屏幕阅读器的动作请求如何回到你的代码。
2. 熟练使用 `.id()` + `.role()` + `.aria_*()` 链式 API，为元素标注无障碍语义（角色、名称、数值、状态），并理解 `text!` 宏的 ID 陷阱。
3. 用 `.on_a11y_action()` 响应辅助技术发起的动作（如语音控制的 Increment/Decrement），并知道 Click/Focus 有内置回退。
4. 用 `A11ySubtreeBuilder` 为自定义 `Element` 注入"合成子节点"，让一个元素在无障碍树中呈现为多个节点。
5. 会用 `Window::debug_a11y_tree_json()` 与真实屏幕阅读器验证树结构。

## 2. 前置知识

### 2.1 什么是"程序化无障碍"

"无障碍"（accessibility，常缩写为 a11y）指应用能被所有用户使用，包括视障、听障、行动不便等用户。其中一部分靠视觉设计（足够对比度、可关闭动画），另一部分靠**程序化无障碍**：让屏幕阅读器、盲文显示器、语音控制等辅助技术（assistive technology，AT）能**读取和操纵**你的界面。本讲只关注后者。

程序化无障碍依赖两个方向的能力：

- **上报**：把当前 UI 的结构（这里有个按钮、它叫什么、值是多少）告诉系统。
- **响应**：接收系统转发的用户意图（"请点击那个按钮"、"请把滑块加一"）并执行。

这与 GPUI 已有的两套机制天然同构：上报挂在绘制管线上，响应挂在事件派发上——这正是源码的实际做法。

### 2.2 AccessKit：跨平台无障碍工具包

每个操作系统都有自己的无障碍 API（macOS 的 AX、Windows 的 UIA、Linux 的 AT-SPI/dbus）。[AccessKit](https://accesskit.dev/) 是一个 Rust 编写的跨平台抽象层：你只描述一棵"无障碍树"，各平台的适配器（adapter）负责把它翻译成系统调用。GPUI 没有自己造轮子，而是把元素树"投影"成 AccessKit 的 `TreeUpdate`。

### 2.3 WAI-ARIA 术语

GPUI 的 API 模仿 Web 的 ARIA 术语，先建立词汇表：

| 术语 | 含义 | GPUI 对应 |
| --- | --- | --- |
| role（角色） | 这是什么类型的节点：按钮、标题、列表… | `.role(Role::Button)` |
| label（名称） | 节点的名字，屏幕阅读器会朗读 | `.aria_label("重置")` |
| value（值） | 节点当前值，如滑块位置 | `.aria_numeric_value(3.0)` |
| state（状态） | 选中/展开/开关等 | `.aria_toggled(Toggled::True)` |
| action（动作) | 节点支持的操作 | `.on_a11y_action(AccessibleAction::Click, …)` |

### 2.4 本讲需要的既有知识

- **`GlobalElementId`**（u4-l1）：元素 id 栈的路径快照，形如 `["outer-id", "inner-id"]`，是跨帧元素状态的键。本讲它会再次成为主角——AccessKit 的 `NodeId` 就是从它哈希出来的。
- **`Interactivity` 与 `Stateful<Div>`**（u5-l2）：div 的全部交互配置汇聚于 `Interactivity` 结构体，`.id()` 返回 `Stateful<E>` 才解锁有状态方法。`.role()` 等 a11y 方法同样定义在 `StatefulInteractiveElement` 上。
- **`text!` 宏**（u3-l5）：按源码位置自动生成 ID 的文本元素。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/_accessibility.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/_accessibility.rs) | 官方用户指南，仅 `#[cfg(doc)]` 时编译进 crate 文档，不参与正常构建 |
| [src/window/a11y.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs) | 核心实现：每窗口的 `A11y` 状态、`A11yNodeBuilder` 树构建器、`A11ySubtreeBuilder` |
| [src/window/a11y/debug.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y/debug.rs) | 保留最近一帧 `TreeUpdate`，支撑 `debug_a11y_tree_json` 调试输出 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs) | 接线层：窗口创建时初始化适配器、`draw_roots` 的帧生命周期、动作派发 |
| [src/element.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs) | `Element` trait 的三个 a11y 钩子；`Drawable::prepaint` 中真正建节点的代码 |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs) | `role`/`aria_*`/`on_a11y_action` 链式 API 与 `Interactivity` 的登记逻辑 |
| [src/elements/text.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs) | `Text` 元素默认以 `Label` 角色进树；`new_inaccessible` 逃逸口 |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs) | `A11yCallbacks` 回调包与 `PlatformWindow::a11y_init` 平台钩子 |
| [src/app.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs) | `Application::new_inaccessible`：整体关闭 a11y 的开关 |
| [src/gpui.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs) | 重导出 `Role`/`Toggled`/`Orientation` 与 `AccessibleAction` |
| [examples/a11y.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs) | 官方演示：标题、SpinButton、按钮、开关、列表的完整标注范例 |

## 4. 核心概念与源码讲解

### 4.1 AccessKit 集成：每帧构建无障碍树

#### 4.1.1 概念说明

这个模块解决的问题是：**如何把一棵每帧都重建的立即模式元素树，变成系统屏幕阅读器眼中一棵"稳定"的树？**

回忆 u3-l1 的心智模型：元素树是立即模式的，每帧从根视图 `render` 重建；应用状态（实体）是保留模式的。无障碍树必须是保留语义的——屏幕阅读器需要知道"这个按钮还是上一帧那个按钮"，否则每帧都当成"删了一个按钮、又加了一个按钮"，朗读会完全混乱。

GPUI 的解法与元素跨帧状态（u4-l1）同源：**用 `GlobalElementId` 做跨帧身份**。每帧为无障碍树收集所有"有 id 且有 role"的元素，打包成一个 AccessKit `TreeUpdate` 发给平台适配器；适配器与上一帧做 diff，只把"有意义的变化"（新增/删除/更新节点）翻译成系统调用。节点 ID 稳定，diff 就小；ID 变了，就等价于旧节点销毁、新节点诞生。

另一个关键设计是**懒激活**：无障碍树构建有开销（每帧哈希、构造节点），而绝大多数用户并不连接屏幕阅读器。所以 AccessKit 适配器只在系统真正有 AT 连接时才回调"激活"，GPUI 读到一个原子布尔标志，从下一帧才开始建树；AT 断开则自动停建。这就是为什么相关代码处处可见 `if window.a11y.is_active()` 守卫。

#### 4.1.2 核心流程

一帧中无障碍树的构建流程（伪代码）：

```text
平台帧回调 on_request_frame
  └─ Window::draw_roots
       ├─ a11y.sync_active_flag()          # 读原子标志，锁定"本帧是否建树"
       ├─ if a11y.is_active():
       │    a11y.begin_frame()             # 清空上帧的 focus/bounds/listeners，
       │                                   # 压入根节点 (Role::Window, NodeId(0), 窗口标题)
       ├─ 整棵元素树 request_layout → prepaint → paint
       │    # prepaint 阶段，每个「有 id 且有 role」的元素：
       │    #   NodeId = hash(GlobalElementId)
       │    #   node   = Node::new(role) + 物理像素 bounds + write_a11y_info
       │    #   nodes.push(node_id, node)  # 压栈；父节点自动记录 child
       │    #   （元素 prepaint 完成后）a11y_synthetic_children → nodes.pop()
       ├─ a11y.sync_active_flag()          # 帧中可能被激活/去激活
       └─ if 本帧始终活跃:
            tree_update = a11y.end_frame() # 弹出所有节点、解析焦点、修复树
            platform_window.a11y_tree_update(tree_update)  # 交给适配器 diff
```

激活/去激活与动作回流的流程：

```text
屏幕阅读器连接 ──▶ 适配器调用 activation 回调
                    ├─ active_flag.store(true)     # 下一帧开始建树
                    └─ 经 channel 通知前台任务 window.refresh()  # 强制重绘一帧
屏幕阅读器发起动作 ──▶ 适配器调用 action 回调 ──▶ channel ──▶ Window::handle_a11y_action
```

#### 4.1.3 源码精读

**架构总览**。模块文档用一张图说明分层：GPUI 只面对 AccessKit，由各平台适配器对接系统 API（Linux 走 dbus/AT-SPI）。并明确两大职责与"节点 ID 必须跨帧稳定，ID 派生自 `GlobalElementId`，没有全局 ID 的元素不会出现在无障碍树中"这一核心规则：[src/window/a11y.rs:L1-L65](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L1-L65)。文档还点明这一切发生在 `Drawable::prepaint` 中，构建器内部维护一个节点栈来计算父子关系。

**每窗口状态 `A11y`**：[src/window/a11y.rs:L122-L164](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L122-L164)。这段代码定义了窗口级无障碍状态，关键字段：

- `force_disabled`：由 `Application::new_inaccessible` 设置的总开关；
- `active_flag`：`Arc<AtomicBool>`，AccessKit 适配器在激活/去激活回调里写它，可在帧中途变化；
- `active_this_frame`：帧开始时对 `active_flag` 的快照。文档解释了为什么必须快照——构建器维护节点栈，每个节点必须恰好压栈/弹栈一次，帧中途翻转活跃状态会破坏栈平衡；
- `nodes: A11yNodeBuilder`：树构建器本体；
- `focus_ids` / `node_bounds` / `action_listeners`：三个 `NodeId → 数据` 的映射，分别服务焦点派发、Click 回退的坐标合成、动作监听器查找，每帧 `begin_frame` 清空重建。

**帧边界**。`begin_frame` 清空三张表并让构建器压入根节点：[src/window/a11y.rs:L270-L276](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L270-L276)；根节点固定为 `NodeId(0)`、角色 `Window`、携带窗口标题作为 label：[src/window/a11y.rs:L115-L116](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L115-L116) 与 [src/window/a11y.rs:L469-L486](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L469-L486)。`end_frame` 收尾产出 `TreeUpdate` 并让调试层留档：[src/window/a11y.rs:L279-L291](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L279-L291)。

**树构建器 `A11yNodeBuilder`**：[src/window/a11y.rs:L367-L384](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L367-L384)。它的数据结构是两条平行栈（`ids_stack` 与 `nodes_stack`，`SmallVec` 内联 16 层）加一个结果列表 `all_nodes`。这套设计与 u4-l1 的元素状态栈如出一辙：

- `push`：查重后压栈，同时把 id 追加为栈顶（父）节点的 child：[src/window/a11y.rs:L421-L436](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L421-L436)。**父子关系不需要显式声明——prepaint 的递归顺序天然保证孩子的 push 发生在父亲还在栈上时**；
- `can_push`：用 `seen_ids` 集合查重，debug 构建下重复 ID 直接 `debug_assert` 失败，并明确警告"release 构建中该节点会被静默丢弃"：[src/window/a11y.rs:L406-L419](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L406-L419)——这就是官方指南反复强调"同一帧内 `GlobalElementId` 必须唯一"的物理后果；
- `pop`：弹栈并把节点定稿进 `all_nodes`：[src/window/a11y.rs:L461-L467](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L461-L467)；
- `finalize`：断言栈只剩根、弹出全部节点、解析焦点（含 active descendant 覆盖，详见 4.4），组装 `TreeUpdate { nodes, tree, tree_id, focus }`：[src/window/a11y.rs:L540-L583](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L540-L583)；
- `repair_tree_update`：防御性修复——AccessKit 对非法 `TreeUpdate` 会 panic，这里主动校验"焦点必须指向存在的节点""child 引用必须存在"，违规时打日志并修复而非崩溃：[src/window/a11y.rs:L585-L629](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L585-L629)。这是"框架兜住应用层 bug"的典型样例。

**帧接线**。窗口绘制入口 `draw_roots` 在 prepaint 前同步活跃标志并开帧：[src/window.rs:L3085-L3092](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L3085-L3092)；帧尾再次同步（处理帧中途激活/去激活），只有"帧首帧尾都活跃"才把 `TreeUpdate` 发给平台适配器：[src/window.rs:L3174-L3198](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L3174-L3198)。注意帧首不活跃时连 `end_frame` 都不调（只在活跃时清理构建器），保证栈不会残留半帧数据。

**激活与动作通道**。窗口创建时（非 wasm 且未被强制禁用），GPUI 构造一个初始 `TreeUpdate`（只含根窗口节点），打包三个回调交给 `platform_window.a11y_init`：[src/window.rs:L1439-L1480](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L1439-L1480)。回调包 `A11yCallbacks` 定义在平台层，activation 返回初始树、action 转发 `ActionRequest`、deactivation 通知停用：[src/platform.rs:L727-L735](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L727-L735)；`a11y_init` 默认空实现，由各平台窗口覆盖（测试平台即用默认空实现）：[src/platform.rs:L969](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L969)。激活/去激活回调里各起一个前台任务，收到通知就 `window.refresh()` 强制重绘——文档注释解释了原因：a11y 可能在任意时刻激活，而按需计算 `TreeUpdate` 来不及，干脆强制一帧让完整树走正常管线：[src/window.rs:L1482-L1520](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L1482-L1520)。

**调试出口**。`Window::is_a11y_active()` 是公开查询（渲染期可用来跳过只为 a11y 服务的昂贵计算，激活时会强制重绘所以不会漏）：[src/window.rs:L6070-L6082](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6070-L6082)；`debug_a11y_tree_json()` 返回最近一帧树的 JSON 快照（把 64 位 NodeId 换成 `a`、`b`… 短名便于阅读）：[src/window.rs:L6084-L6087](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6084-L6087)，其实现见 [src/window/a11y/debug.rs:L99-L101](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y/debug.rs#L99-L101)。注意它只在 a11y 活跃（有 AT 连接）的帧才有内容。

#### 4.1.4 代码实践

**实践目标**：亲手跑通官方 a11y 示例，观察懒激活日志。

1. 阅读示例顶部文档注释了解界面结构：[examples/a11y.rs:L1-L32](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L1-L32)。
2. 运行（Linux 需显式启用平台 feature，见示例文档）：

   ```bash
   cargo run -p gpui --example a11y
   # Linux:
   cargo run -p gpui --features gpui_platform/wayland,gpui_platform/x11 --example a11y
   ```

3. 示例的 `main` 把 `gpui` 模块日志级别调到 `Info`（[examples/a11y.rs:L252-L259](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L252-L259)），因此激活时会打印 `Accessibility activated`（[src/window.rs:L1463](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L1463)）。

**需要观察的现象**：

- 不开屏幕阅读器时，界面正常但无 a11y 日志；
- 启动屏幕阅读器（Linux 上如 Orca；macOS 开启 VoiceOver；Windows 用 NVDA）后，终端应出现 `Accessibility activated`，随后是每帧的 `Sending a11y tree update: N nodes` 调试日志（该日志为 `debug` 级，可将示例的 filter_module 调到 `Debug` 观察）；
- 关闭屏幕阅读器应出现 `Accessibility deactivated`。

**预期结果**：验证"懒激活 + 强制重绘"机制真实生效。本机无屏幕阅读器/无显示环境时此实践**待本地验证**；替代方案是纯源码阅读：从 `a11y_init` 的 activation 闭包出发，沿 `active_flag → sync_active_flag → begin_frame` 走一遍 4.1.2 的伪代码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `A11y` 要同时保存 `active_flag` 和 `active_this_frame` 两个标志，而不是每次直接读原子布尔？

**答案**：`active_flag` 由适配器回调写入，可能在帧执行中途变化；而树构建器依赖节点栈的严格平衡（每个节点恰好 push/pop 一次），如果帧中途标志翻转，可能出现"压了栈但帧尾不再建树"或反之的栈失衡。所以帧首快照到 `active_this_frame` 并以它驱动整帧，帧尾再同步一次并要求"帧首帧尾都活跃"才发送更新（[src/window/a11y.rs:L136-L147](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L136-L147)、[src/window.rs:L3174-L3198](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L3174-L3198)）。

**练习 2**：一个没有 `.id()` 的 div，即使设置了 role 也不会出现在无障碍树里。从源码找出判定链路。

**答案**：`Drawable::prepaint` 中建节点的条件是三层嵌套的 `if`：`window.a11y.is_active()` → `global_id.is_some()` → `a11y_role()` 返回 `Some`（[src/element.rs:L365-L401](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L365-L401)）。没有 id 就没有 `GlobalElementId`，也就没有 `accesskit_node_id()` 可言（[src/window/a11y.rs:L55-L59](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L55-L59)）。

### 4.2 role 标注与 aria_* 属性

#### 4.2.1 概念说明

这个模块解决的问题是：**应用开发者如何声明"这个元素在无障碍树里是什么、叫什么、值是多少"？**

GPUI 的答案是把 ARIA 概念全部做成 `StatefulInteractiveElement` 上的链式方法，存储进 `Interactivity` 的 `aria` 字段组——与 u5-l2 讲过的样式补丁、监听器完全同一模式。三条使用规则：

1. **id + role 双条件**：`.role()` 定义在 `StatefulInteractiveElement` 上，必须先 `.id()`。id 提供跨帧身份，role 提供语义类型，缺一不可。
2. **role 决定"报不报"**：`Div` 的 `a11y_role` 实现只返回 `override_role`（即 `.role()` 设置的值）并过滤 `GenericContainer`（[src/elements/div.rs:L1797-L1803](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1797-L1803)）——不设 role 的 div 根本不进树，相当于 HTML 里没有 role 的 `<div>`。想"有节点但无特殊语义"应显式用 `Role::GenericContainer` 以外的通用角色（`role()` 会对 `GenericContainer` 做 debug 断言提醒，见 [src/elements/div.rs:L1250-L1257](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1250-L1257)）。
3. **ID 稳定性 = 语义稳定性**：跨帧同 ID 视为同一节点（内容更新），换 ID 视为删旧增新。`text!` 宏的 ID 来自**调用处的源码位置**，在同一处循环调用会产生多个同 ID 节点——release 下被静默丢弃。两种修法：`text!(id = index, todo)` / `text!(todo).with_id(index)`，或包一层 `div().id(index)`（全局 ID 包含祖先路径，父不同则全局不同）。官方指南用 todo 列表完整演示了这个陷阱：[src/_accessibility.rs:L105-L183](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/_accessibility.rs#L105-L183)。

#### 4.2.2 核心流程

`role`/`aria_*` 从链式调用到无障碍节点的数据流：

```text
div().id("x").role(Role::Button).aria_label("保存")
  │ 构建期：写入 Interactivity.override_role 与 Interactivity.aria.*（Option 字段）
  ▼
帧内 prepaint（Drawable::prepaint）
  ├─ NodeId = GlobalElementId("…","x").accesskit_node_id()   # DefaultHasher 哈希
  ├─ node = accesskit::Node::new(Role::Button)
  ├─ node.set_bounds(物理像素矩形)                # 逻辑像素 × scale_factor
  ├─ element.write_a11y_info(&mut node)          # 把 aria 字段搬运到 node
  └─ a11y.nodes.push(node_id, node)
```

焦点元素还有一步额外登记：带 `track_focus`/`focusable` 的元素在 `Interactivity` 的 prepaint 里调用 `set_focusable(node_id, focus_handle.id)` 建立 `NodeId ↔ FocusId` 映射；若该元素正持有焦点，再调 `set_focus(node_id)` 把它报告为本帧焦点。

#### 4.2.3 源码精读

**公开 API 层**。`.role()` 把角色存入 `override_role` 并对 `GenericContainer` 做 debug 断言：[src/elements/div.rs:L1246-L1257](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1246-L1257)。`.aria_label()` 写入 `aria.label`：[src/elements/div.rs:L1271-L1275](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1271-L1275)。整个 aria 家族都是同一形状的一行 setter：

- `aria_description`（补充描述，朗读顺序在名称/角色/值之后）、`aria_keyshortcuts`（告知快捷键，不创建键位绑定）：[src/elements/div.rs:L1277-L1294](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1277-L1294)；
- 状态与数值族：`aria_selected`/`aria_expanded`/`aria_toggled`/`aria_numeric_value`/`aria_min_numeric_value`/`aria_max_numeric_value`/`aria_numeric_value_step`/`aria_value`/`aria_placeholder`/`aria_orientation`：[src/elements/div.rs:L1333-L1434](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1333-L1434)；
- 集合与表格族：`aria_level`（标题层级）、`aria_position_in_set`/`aria_size_of_set`（列表项序号）、`aria_row_index`/`aria_column_index`/`aria_row_count`/`aria_column_count`：[src/elements/div.rs:L1397-L1434](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1397-L1434)；
- `aria_active_descendant`：实现"焦点容器 + 活动后代"模式（列表容器持焦点、选中项被朗读为焦点），设在后代元素上且仅当Focused祖先存在时生效，详见 4.4.3：[src/elements/div.rs:L1296-L1314](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1296-L1314)；
- `accessibility_id`：作者自定义的外部可见 ID（映射到 UIA `AutomationId`/macOS `AXIdentifier` 等），与内部 `ElementId` 是两回事：[src/elements/div.rs:L1259-L1269](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1259-L1269)。

**`Element` trait 钩子**。三个可选方法构成自定义元素的 a11y 契约：`a11y_role`（返回 `None` 则不进树）、`write_a11y_info`（仅在 role 为 `Some` 时被调）、`a11y_synthetic_children`（见 4.4）：[src/element.rs:L106-L136](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L106-L136)。

**节点构造现场**。`Drawable::prepaint` 是 role 真正变成节点的地方：取 `GlobalElementId` 哈希出 `NodeId`，用 role 新建节点、写入乘以缩放系数后的物理像素 bounds、调用 `write_a11y_info`、登记 `node_bounds`、压入构建器栈：[src/element.rs:L364-L401](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L364-L401)。`accesskit_node_id` 就是对整个全局 ID 路径做一次 `DefaultHasher`：[src/element.rs:L227-L234](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L227-L234)。

**aria 字段搬运**。`Interactivity::write_a11y_info` 逐字段把 `Option` 值搬进 `accesskit::Node`，末尾还根据已有配置追加动作声明：有 click 监听器则 `add_action(Click)`，可聚焦则 `add_action(Focus)`，显式注册的 a11y 动作也一并声明：[src/elements/div.rs:L3392-L3465](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L3392-L3465)。注意这个方法既是 `Div` 的 `Element::write_a11y_info` 实现入口（[src/elements/div.rs:L1805-L1807](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1805-L1807)），也被单测直接调用（[src/elements/div.rs:L4865-L4877](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L4865-L4877)）。

**焦点登记**。`Interactivity` 的 prepaint 中，被 track 的焦点句柄映射到节点：`set_focusable` 建 `NodeId → FocusId` 表；当前持焦则 `set_focus`；有焦点句柄却无元素 id 时调用 `note_focus_without_node` 打出指引日志（"给它同时配 `.id(...)` 和 `.role(...)`"），因为此时屏幕阅读器只能退化为朗读整个窗口：[src/elements/div.rs:L2216-L2235](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L2216-L2235)。这个日志的去重逻辑在 [src/window/a11y.rs:L188-L202](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L188-L202)。`report_active_descendant_focus` 的对应处理紧随其后：[src/elements/div.rs:L2237-L2243](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L2237-L2243)。

**文本元素**。`Text` 有 id 就以 `Label` 角色进树，值即文本内容——这是纯文本默认可读的原因：[src/elements/text.rs:L189-L199](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L189-L199)。`InteractiveText` 同样固定 `Label`：[src/elements/text.rs:L1071-L1077](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L1071-L1077)。自定义组件内想避免文本与父容器 label 重复朗读时，用 `Text::new_inaccessible` 制造无 id（因而不进树）的文本：[src/elements/text.rs:L82-L94](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L82-L94)，`with_id` 则用于补救循环中的同 ID 问题：[src/elements/text.rs:L102-L106](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L102-L106)。

**重导出**。`Role`/`Toggled`/`Orientation` 与 `AccessibleAction`（即 `accesskit::Action` 的别名）都从 crate 根导出，业务代码无需直接依赖 accesskit：[src/gpui.rs:L90-L92](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L90-L92)。

**示例范本**。a11y 示例的根节点是 `Role::Application` + `aria_label`：[examples/a11y.rs:L61-L74](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L61-L74)；标题用 `Role::Heading` + `aria_level(1)`：[examples/a11y.rs:L76-L85](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L76-L85)；列表用 `Role::List`，列表项 `Role::ListItem` + `aria_position_in_set`/`aria_size_of_set`，且项的 id 带索引 `("task", i)` 保证唯一（注释明确解释了为何内层 `text!` 无需再配 id——父 div 的 id 已区分全局 ID 路径）：[examples/a11y.rs:L195-L223](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L195-L223)。

#### 4.2.4 代码实践

**实践目标**：把"id + role + aria"三件套用到自己的界面上，并制造一次"屏幕阅读器身份断裂"来理解 ID 稳定性的意义。

1. 以 hello_world 或 a11y 示例为底，新建一个自己的示例（复制 `examples/a11y.rs` 到 `examples/my_a11y.rs` 并在 `Cargo.toml` 的 `[[example]]` 区块登记，模仿 [Cargo.toml:L253](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/Cargo.toml#L253) 附近的写法；若不想改构建配置，直接改 a11y 示例源码也可以）。
2. 渲染一个计数器，每秒（`cx.spawn` + `timer`）让计数 +1，节点 id 固定为 `"counter"`、role 为 `Role::Button`、`aria_label(format!("计数 {n}"))`、`aria_numeric_value(n as f64)`——观察值更新走"节点内容更新"路径。
3. 再故意把 id 改成 `format!("counter-{n}")`（每帧变 id）：用屏幕阅读器听朗读行为的变化（每次都像"新按钮出现"），或对比 `debug_a11y_tree_json` 输出。

**需要观察的现象 / 预期结果**：固定 id 时 AT 平静地播报值变化；变化 id 时 AT 每秒重新宣告新节点。无 AT 环境时**待本地验证**，可退化为阅读 [src/_accessibility.rs:L69-L103](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/_accessibility.rs#L69-L103) 的 frame_1/frame_2 例子并用自己的话复述"为什么屏幕阅读器无法知道两个 div 是同一个"。

#### 4.2.5 小练习与答案

**练习 1**：`.aria_keyshortcuts("ctrl-s")` 会让 Ctrl+S 触发保存吗？

**答案**：不会。文档明确说它"只是告知辅助技术键位是什么"，不创建任何键位绑定（[src/elements/div.rs:L1286-L1294](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1286-L1294)）。真正的绑定要走 u5-l4 的 Keymap/`bind_keys` + action。

**练习 2**：`div().children((0..3).map(|i| text!("第{i}项")))` 在无障碍树上会发生什么？如何修复？

**答案**：三次 `text!` 写在同一处源码位置，ID 相同、祖先相同，三个节点全局 ID 撞车；debug 构建触发 `can_push` 的断言，release 构建下后两个节点被静默丢弃（[src/window/a11y.rs:L406-L419](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L406-L419)）。修复：`text!(id = i, format!("第{i}项"))` 或包一层 `div().id(i)`（[src/_accessibility.rs:L154-L183](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/_accessibility.rs#L154-L183)）。

**练习 3**：为什么 `role()` 定义在 `StatefulInteractiveElement` 而不是 `InteractiveElement` 上？

**答案**：无障碍节点以 `GlobalElementId` 为身份，而 id 只有 `.id()` 之后才存在（产生 `Stateful<E>`，u5-l2）；没有 id 的元素根本无法生成 `NodeId`。把 `role()` 放在 Stateful 侧让"忘配 id"在编译期就失败，而不是运行时静默不进树。

### 4.3 响应辅助技术动作：on_a11y_action

#### 4.3.1 概念说明

这个模块解决的问题是：**语音控制用户说"把计数加一"，这个意图如何变成你代码里的一次状态更新？**

首先要分清两个"Action"：GPUI 的 `Action` trait（u5-l3，服务于键位/菜单）与 AccessKit 的 `accesskit::Action`（服务于辅助技术）**完全无关**。后者在 GPUI 中重导出为 `AccessibleAction`（[src/gpui.rs:L91](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L91)），值包括 `Click`、`Focus`、`Blur`、`Increment`、`Decrement`、`SetValue` 等。

动作派发目标是**特定节点**。平台适配器发来 `ActionRequest { target_node, action, data }`，GPUI 查 `action_listeners: FxHashMap<NodeId, Vec<(Action, Listener)>>` 找到该节点注册的监听器执行。找不到时有一层**内置回退**：

- `Click`：用该节点的 `node_bounds` 算出中心点，合成一对 MouseDown/MouseUp 走常规事件派发——所以"只配了 `.on_click()` 的按钮天然可被语音点击"；
- `Focus`/`Blur`：经 `focus_ids` 表换算成 `FocusId`，调用窗口焦点 API。

这与 u5-l2 讲过的"监听器在 paint 阶段注册、每帧重申报"模式完全一致：a11y 动作监听器也是每帧清空、paint 期重新登记，绝无陈旧引用。

#### 4.3.2 核心流程

```text
屏幕阅读器/语音控制发起动作
  └─ 平台适配器 → A11yCallbacks.action 闭包
       └─ async_channel 发送 ActionRequest
            └─ 前台任务收到 → window.handle_a11y_action(request, cx)
                 ├─ 查 a11y.action_listeners[request.target_node]
                 │    逐个匹配 action 字段并调用（先取出再放回，避免借用冲突）
                 ├─ 命中 → 结束
                 └─ 未命中 → 内置回退：
                      Click  → 合成中心点鼠标按下/抬起 → dispatch_event
                      Focus  → focus_ids 查 FocusId → window.focus(handle)
                      Blur   → window.blur()
```

节点声明"我支持哪些动作"发生在 prepaint 的 `write_a11y_info` 里（`node.add_action(...)`），实际监听器登记发生在 paint 里——声明与实现分离但同帧完成。

#### 4.3.3 源码精读

**链式入口**。`.on_a11y_action(action, listener)` 把 `(Action, 盒装闭包)` push 进 `Interactivity.a11y_action_listeners`：[src/elements/div.rs:L1438-L1452](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1438-L1452)。存储字段与合成子节点闭包一起定义在 `Interactivity` 上：[src/elements/div.rs:L2083-L2087](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L2083-L2087)。

**paint 期登记**。`Interactivity::paint` 里，a11y 活跃且元素有全局 id 时，把构造期攒的监听器整批转交给 `window.on_a11y_action(node_id, …)`：[src/elements/div.rs:L2478-L2491](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L2478-L2491)。`Window::on_a11y_action` 是按 `NodeId` 分桶的哈希表插入：[src/window.rs:L6089-L6105](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6089-L6105)。模块文档明确描述了这条"public API → Interactivity::paint → Window::on_a11y_action"的调用链与"帧首清空、paint 期重填"的生命周期：[src/window/a11y.rs:L85-L97](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L85-L97)。

**派发与回退**。`handle_a11y_action` 先把目标节点的监听器桶临时取出（让闭包可以可变借用 Window）、逐个匹配 action 执行、再放回；未命中则进入内置回退分支——Click 合成中心点的一对鼠标事件、Focus/Blur 走焦点表：[src/window.rs:L6107-L6168](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6107-L6168)。动作请求经 `async_channel` 从适配器线程回到 GPUI 前台的接收循环：[src/window.rs:L1509-L1520](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L1509-L1520)。

**动作声明**。`Interactivity::write_a11y_info` 末尾按配置 `add_action`：有 click 监听器声明 `Click`，可聚焦声明 `Focus`，显式 a11y 动作逐一声明：[src/elements/div.rs:L3456-L3464](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L3456-L3464)。

**示例范本**。计数器以 `Role::SpinButton` 呈现，注册 `Increment`/`Decrement` 两个 a11y 动作（闭包捕获实体的弱句柄回写状态），同时保留 `.on_click`——文档注释点明"Click 也可用，经由内置处理器"：[examples/a11y.rs:L86-L133](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L86-L133)。数值语义由 `aria_numeric_value`/`aria_min_numeric_value` 补全（Decrement 回调里 `.max(0)` 与最小值声明呼应）。

#### 4.3.4 代码实践

**实践目标**：为一个自定义"滑块"补齐语音控制能力。

1. 参照 a11y 示例的 SpinButton 写法，实现一个 0–100 的滑块：`div().id("volume").role(Role::Slider).aria_label("音量").aria_numeric_value(v).aria_min_numeric_value(0.).aria_max_numeric_value(100.)`。
2. 注册 `AccessibleAction::Increment` 与 `Decrement`，回调里 `v = (v + 5).min(100)` / `(v - 5).max(0)` 后 `cx.notify()`；再加 `SetValue`（若携带 `ActionData`，从 `Option<&accesskit::ActionData>` 中读取目标值——具体数据形态可查 accesskit 文档，属**待确认**细节）。
3. 验证：连接屏幕阅读器后，用其"递增/递减"命令（NVDA/Orca 的对象导航 + 操作）驱动滑块；同时点击滑块本体验证 `on_click` 仍工作。

**需要观察的现象 / 预期结果**：语音/读屏递增命令改变数值且界面刷新；鼠标点击路径不受影响。无 AT 环境**待本地验证**；纯阅读替代：对照 [examples/a11y.rs:L103-L122](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L103-L122) 画出 Increment 从适配器到 `this.count += 1` 的完整六跳调用链。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `handle_a11y_action` 要先把监听器桶 `remove` 出来执行完再 `insert` 回去？

**答案**：监听器签名是 `FnMut(Option<&ActionData>, &mut Window, &mut App)`，执行时需要可变借用 Window；而 `action_listeners` 表本身存在 `Window.a11y` 里，直接遍历会构成对 Window 的双重可变借用。先取出来、执行、再放回，是标准的借用冲突解法（[src/window.rs:L6108-L6126](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6108-L6126)）。

**练习 2**：一个只写了 `.id("ok").role(Role::Button).on_click(...)` 的按钮，没注册任何 `on_a11y_action`，语音控制说"点击 OK"能生效吗？

**答案**：能。未命中显式监听器时，`Click` 走内置回退：用 `node_bounds` 的中心合成 MouseDown+MouseUp 派发（[src/window.rs:L6129-L6149](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6129-L6149)）；且 `write_a11y_info` 因存在 click 监听器已替它声明了 `Click` 动作（[src/elements/div.rs:L3456-L3458](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L3456-L3458)）。

### 4.4 A11ySubtreeBuilder 与合成子节点

#### 4.4.1 概念说明

这个模块解决的问题是：**一个自定义 `Element` 想在无障碍树里"一个变多个"怎么办？**

常规规则是"一个有 id 有 role 的元素 = 一个节点"。但设想一个自定义文本编辑器元素：它整体应是 `Role::TextInput`，同时想把文本内容按行/按 run 暴露为孩子节点（`Role::TextRun`），还要报告光标位置（`TextPosition`/`TextSelection`）。这些"孩子"在元素树里并不存在——没有对应的 div 或元素。

GPUI 的答案是**合成子节点**（synthetic children）：`Element` trait 提供钩子 `a11y_synthetic_children(&mut self, prepaint, builder)`，在元素被 prepaint 之后（因此能利用 prepaint 状态，比如"哪些行在屏幕上可见"）调用；你通过 `A11ySubtreeBuilder` 做三件事：

1. `synthetic_node_id(key)`：为孩子派生 ID——由父 NodeId 与 key 联合哈希，所以 key 只需在**同一次调用内**唯一，跨元素可重名；
2. `push_child(id, node)`：把孩子作为叶子挂到当前元素节点下（等价于 push 后立即 pop）；
3. `parent_node()`：拿到父节点（当前元素自身）的可变引用，补写 `set_text_selection` 之类只有"知道孩子存在"之后才能写的属性。

div 侧还提供了免实现的便捷入口：`.a11y_synthetic_children(|builder| ...)` 直接传闭包（[src/elements/div.rs:L1316-L1331](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1316-L1331)）。

#### 4.4.2 核心流程

```text
Drawable::prepaint(某元素)
  ├─ 建父节点并 push（见 4.2）
  ├─ 执行元素自身 prepaint（产出 PrepaintState）
  ├─ if 本元素成功 push 了节点:
  │    builder = A11ySubtreeBuilder::new(父 NodeId, &mut a11y.nodes)
  │    element.a11y_synthetic_children(&mut prepaint, &mut builder)
  │         ├─ builder.synthetic_node_id(key) → 孩子ID
  │         ├─ builder.push_child(孩子ID, accesskit::Node::new(TextRun) + 值/字符长度)
  │         └─ builder.parent_node().set_text_selection(...)   # 反手改父节点
  └─ a11y.nodes.pop()   # 父节点出栈定稿
```

时机是关键：合成发生在"父节点已在栈上、元素 prepaint 已完成"的窗口内，所以既能用 prepaint 信息决定合成什么，又不会破坏栈平衡——pop 由框架统一执行。

#### 4.4.3 源码精读

**调用现场**。`Drawable::prepaint` 中，元素自身 prepaint 完成后：若该元素成功 push 了节点，就用其 NodeId 构造 `A11ySubtreeBuilder` 并调用 `a11y_synthetic_children`，最后统一 `nodes.pop()`：[src/element.rs:L414-L438](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L414-L438)。模块文档解释了"为何放在 prepaint 之后"——合成可能需要 prepaint 才知道的信息（如可见性）：[src/window/a11y.rs:L67-L83](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L67-L83)。

**`Element` 钩子签名**（默认空实现）：[src/element.rs:L122-L136](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L122-L136)。

**`A11ySubtreeBuilder`**：持有父 NodeId 与构建器的可变引用：[src/window/a11y.rs:L298-L307](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L298-L307)。三个方法：

- `synthetic_node_id`：`DefaultHasher` 混合父 ID 与 key，文档强调 key 只需调用内唯一：[src/window/a11y.rs:L325-L336](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L325-L336)；
- `push_child`：委托 `push_leaf`（挂为孩子但不入栈——合成节点不能再有元素孩子），ID 重复时返回 `false` 并丢弃；debug 构建下记录 `synthetic: true` 的溯源信息：[src/window/a11y.rs:L338-L357](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L338-L357)，`push_leaf` 本体在 [src/window/a11y.rs:L443-L453](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L443-L453)；
- `parent_node`：返回栈顶（即当前元素节点）的可变引用，不存在则 expect 失败（builder 只在其元素的节点在栈上时存在）：[src/window/a11y.rs:L359-L365](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L359-L365)。

**官方指南范例**。`MyCustomTextField` 的完整示例：建 `TextRun` 孩子并 `set_value`、`set_character_lengths`（UTF-8 每字符字节数，供读屏按字符导航），再反手在父节点 `set_text_selection` 报告光标：[src/_accessibility.rs:L230-L272](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/_accessibility.rs#L230-L272)。

**active descendant：合成思路的姊妹机制**。`finalize` 解析焦点时，若本帧有人调过 `set_active_descendant` 且该节点在树中，则用它覆盖真实焦点——支撑"容器持焦点、选中项被朗读"的复合控件模式：[src/window/a11y.rs:L560-L572](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L560-L572)。门禁在 `A11y::set_active_descendant`：声明者必须是焦点节点的后代（`focus_is_ancestor_of_current` 检查当前栈的非栈顶部分），否则拒绝；同名节点重复声明在 debug 下 panic：[src/window/a11y.rs:L254-L268](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L254-L268)。这套门禁配套一组单测（深后代也认、无焦点/异子树则忽略、兄弟双声明 panic 等）：[src/window/a11y.rs:L657-L746](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L657-L746)，是理解语义的最佳测试集。

#### 4.4.4 代码实践

**实践目标**：写一个最小自定义元素，把一段文本暴露为多个合成 `TextRun` 孩子。

1. 仿照 u4-l1 的 `FixedBox` 实现一个最小 `Element`（`request_layout` 报固定尺寸、`prepaint` 记 bounds、`paint` 画底色即可，可参照 [src/elements/canvas.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/canvas.rs) 的三阶段骨架）。
2. 为它实现：

   ```rust
   // 示例代码：最小合成子节点实现（节选自官方指南思路）
   fn a11y_role(&self) -> Option<accesskit::Role> {
       Some(accesskit::Role::TextInput)
   }

   fn a11y_synthetic_children(
       &mut self,
       _prepaint: &mut Self::PrepaintState,
       builder: &mut gpui::A11ySubtreeBuilder,
   ) {
       let mut run = accesskit::Node::new(accesskit::Role::TextRun);
       run.set_value(self.text.to_string());
       let run_id = builder.synthetic_node_id(0);
       builder.push_child(run_id, run);
   }
   ```

3. 把该元素放进 `div().id("host")`（宿主自身不必再设 role，合成由元素自身钩子完成），连接屏幕阅读器或 dump `debug_a11y_tree_json` 检查 `TextInput` 节点下出现 `TextRun` 孩子。

**需要观察的现象 / 预期结果**：树中宿主节点拥有一个带文本值的 `TextRun` 子节点；把 `push_child` 的 key 从 0 改成固定值并在循环里建多个 run 时，key 重复的节点被丢弃（返回 `false`）。无 AT 环境**待本地验证**；纯阅读替代：跑 `cargo test -p gpui window::a11y` 相关单测并阅读 [src/window/a11y.rs:L657-L746](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L657-L746) 的断言。

#### 4.4.5 小练习与答案

**练习 1**：`push_child` 挂的合成节点为什么不能有自己的元素孩子？

**答案**：它走 `push_leaf`——追加为孩子但不入栈（[src/window/a11y.rs:L443-L453](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L443-L453)），语义上等价于 push 后立刻 pop。元素孩子是在其父元素 prepaint 期间压栈产生的；合成回调运行在元素 prepaint 之后，若允许合成节点再有元素孩子，栈的父子推断就会被破坏。

**练习 2**：`.aria_active_descendant()` 为什么"可以无条件设置在选中项上"而不用担心出错？

**答案**：声明的生效有门禁——仅当焦点节点是声明者的严格祖先时才被采纳（`focus_is_ancestor_of_current`，[src/window/a11y.rs:L498-L507](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L498-L507)）；容器未持焦点时声明被直接忽略。div 侧文档也强调了这一与 Web 不同的安全设计（[src/elements/div.rs:L1296-L1314](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1296-L1314)）。

## 5. 综合实践

**任务：给一个表单界面补全无障碍，并用调试工具验证树结构完整。**（对应本讲规格中的实践任务，综合 4.1–4.4 全部知识。）

从零构建一个"用户资料表单"窗口（可复制 examples/a11y.rs 的 `run_example` 骨架，[examples/a11y.rs:L227-L250](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L227-L250)），要求：

1. **根容器**：`.id("form").role(Role::Form)`（或 `Application`）+ `aria_label("用户资料")` + `track_focus`，并像示例那样绑定 Tab/Shift-Tab 动作走 `focus_next`/`focus_prev`（[examples/a11y.rs:L39-L67](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L39-L67)）。
2. **两个"输入框"**：GPUI 没有内置 input 元素，用 `div().id("name").role(Role::TextInput).aria_label("用户名").focusable().track_focus(focus_handle)` 模拟；旁边配 `text!` 的可见标签。给其中一个输入框试用 `.a11y_synthetic_children` 闭包注入一个 `TextRun` 合成孩子承载当前值。
3. **一个开关**：仿照示例的 `Role::Switch` + `aria_toggled`（[examples/a11y.rs:L160-L192](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/a11y.rs#L160-L192)）。
4. **提交按钮**：`Role::Button` + `aria_label` + `.on_click`；另注册 `AccessibleAction::Click` 的显式处理器打一条日志，验证与内置回退并存时显式监听器优先（参考 [src/window.rs:L6111-L6126](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L6111-L6126) 的命中即返回逻辑）。
5. **验证一（工具）**：绑定一个调试动作（如 `f12`），处理器里调用 `window.debug_a11y_tree_json()` 并 `log::info!` 输出；连接屏幕阅读器触发激活后按 F12，检查 JSON 中：根 Window 之下有 Form，Form 下有 TextInput（带 label）、Switch（带 toggled 状态）、Button（带 Click 动作），TextInput 下有合成 TextRun。节点 id 显示为 `a`、`b`、`c` 等短名。
6. **验证二（人工）**：用真实屏幕阅读器（Linux: Orca；macOS: VoiceOver；Windows: NVDA）Tab 遍历：每个控件应被正确称呼（label + 角色 + 状态），开关切换后被播报，提交按钮可被"执行默认操作"触发。
7. **对照实验**：故意去掉某个输入框的 `.role(...)`，观察日志出现"a11y: focused element … has no accessibility node"（[src/window/a11y.rs:L193-L202](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L193-L202)），读屏退化为朗读整窗。

运行命令与平台 feature 要求同 4.1.4。屏幕阅读器相关的现象**待本地验证**；JSON dump 依赖 a11y 处于活跃状态，无 AT 时返回 `None`，这一点本身就是一个值得写进笔记的框架行为。

## 6. 本讲小结

- GPUI 的无障碍基于 **AccessKit**：每帧把"有 `GlobalElementId` 且 `a11y_role()` 非 `None`"的元素投影成 `TreeUpdate`，平台适配器负责 diff 并调用系统 API；**节点的跨帧身份 = 全局 ID 的哈希**，改 ID 就等于"删旧增新"。
- 树构建发生在 **prepaint 阶段**（`Drawable::prepaint`），靠构建器内部的**节点栈**天然推导父子关系；同帧重复全局 ID 在 debug 下断言、release 下静默丢弃节点。
- a11y 是**懒激活**的：适配器激活回调置原子标志 + 强制 `refresh`，帧首快照到 `active_this_frame` 保证整帧一致性；`Application::new_inaccessible` 可整体关闭。
- 应用侧 API 是 `.id()` + `.role()` + `.aria_*()`（label、数值、状态、集合序号等约二十个 setter），`text!` 的 ID 来自源码位置，循环中必须 `with_id` 或包一层有 id 的 div。
- 辅助技术动作经 `AccessKit::ActionRequest` → channel → `handle_a11y_action`；显式 `on_a11y_action` 优先，未命中时 **Click 合成中心点鼠标事件、Focus/Blur 走焦点表**的内置回退，因此普通 `on_click` 按钮天然可被语音控制。
- **合成子节点**让一个元素呈现为多个节点：`a11y_synthetic_children` 在元素 prepaint 后调用，`A11ySubtreeBuilder` 提供 `synthetic_node_id`/`push_child`/`parent_node`；姊妹机制 `aria_active_descendant` 用焦点门禁支撑复合控件。

## 7. 下一步学习建议

本讲是 u6 高级 UI 模式的收尾。接下来进入第 7 单元（advanced）：

- **u7-l1（Platform 抽象）**：本讲出现的 `PlatformWindow::a11y_init`、`a11y_tree_update` 正是 `PlatformWindow` trait 的成员，下一讲会完整盘点平台层如何隔离 macOS/Windows/Linux 差异（Linux 的 AT-SPI/dbus 适配就在兄弟 crate gpui_linux 中）。
- **u7-l4（测试）**：`A11yNodeBuilder` 的单测（[src/window/a11y.rs:L632-L888](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window/a11y.rs#L632-L888)）展示了不走窗口、直接驱动构建器断言 `TreeUpdate` 的测法，可与 `#[gpui::test]` 基础设施对照学习。
- **延伸阅读**（指南推荐）：[AccessKit 官网](https://accesskit.dev/)、[MDN WAI-ARIA 基础](https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/Accessibility/WAI-ARIA_basics)、[W3C ARIA Authoring Practices Guide](https://www.w3.org/WAI/ARIA/apg/)；同时记住指南末尾的提醒——GPUI 模仿 Web API 但行为不与浏览器完全一致。
- 若你在做真实应用，建议直接通读 Zed 编辑器内对 `role`/`aria_active_descendant` 的实际使用（如在编辑器列表与菜单组件中），体会"焦点容器 + 活动后代"模式的工程化用法。
