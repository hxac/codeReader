# 菜单、系统通知与跳转列表：app_menu 契约的三方实现

## 1. 本讲目标

本讲是第 6 单元「macOS 与 Windows 平台实现」的第三讲，聚焦 Platform trait 中**应用与操作系统外壳对话**的三组能力：

1. 应用菜单：`Menu`/`MenuItem`/`OwnedMenu` 数据模型，`set_menus`/`get_menus` 契约，以及 Keymap 快捷键如何绑定到菜单项。
2. 系统通知：`SystemNotification` 模型与 `show_system_notification`/`dismiss_system_notification`/`on_system_notification_response` 三件套在 macOS、Windows、Linux 上的三份实现。
3. 应用身份与跳转列表：`set_app_identity` 与 Windows 专属的 AppUserModelID（AUMID）、`destination_list` 跳转列表机制。

学完本讲，你应该能：

- 说出 `Menu` 与 `OwnedMenu` 两套并存的原因，以及 `OsAction`、`SystemMenu` 这类「OS 感知」扩展点的用途。
- 解释为什么 macOS 能把菜单变成原生 NSMenu，而 Windows/Linux 只能存储菜单供应用内渲染。
- 对比三个平台在「tag 替换通知」「响应回调回主线程」上的不同落地方式。
- 说明 Windows 上一个非打包（unpackaged）应用为什么必须先 `set_app_identity` 才能弹 toast，以及跳转列表如何反过来把「用户手动移除的条目」报告回应用。

## 2. 前置知识

阅读本讲前，你应当已经理解（u2-l1、u5-l1、u6-l1）：

- **Platform trait 是契约，平台 crate 是实现**：gpui 在 `platform.rs` 里定义平台无关接口，`gpui_macos`/`gpui_windows`/`gpui_linux`/`gpui_web` 各自实现；`gpui_platform` 只是门面。Linux 侧还有一层 `LinuxPlatform` 外壳 → `LinuxClient` 后端的二次分发。
- **方法的默认实现代表「允许不支持」**：默认 no-op 的方法（如 `show_system_notification`）在不支持的平台就是静默空操作；无默认值的必需方法（如 `set_menus`）则每个实现方都必须给出答案。
- **控制流倒置与回调注册**：平台事件（菜单点击、通知点击）发生在平台线程，需要经 `on_app_menu_action` 这类注册的回调送回 gpui；u4 系列讲过「唤醒即再投递」与主线程约束。
- **Action 体系**：菜单项携带 `Box<dyn Action>`，最终经 `App::dispatch_action` 分发，与键盘快捷键触发的是同一条通路（u3-l3）。

本讲新引入的背景知识：

- **AppUserModelID（AUMID）**：Windows 用一个字符串（如 `Zed Industries.Zed`）标识「同一个应用」。任务栏分组、跳转列表、toast 通知全都以 AUMID 为锚点。MSIX 打包应用自动获得 AUMID；直接 `cargo run` 出来的裸进程没有，必须显式设置。
- **XDG 通知协议**：Linux 桌面的通知走 D-Bus 会话总线上的 `org.freedesktop.Notifications` 接口，由通知守护进程（如 GNOME Shell、mako）负责显示。它没有「按 tag 替换」的标准做法，动作按钮用任意字符串 key 标识。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `gpui/src/platform/app_menu.rs` | 菜单数据模型（`Menu`/`MenuItem`/`OwnedMenu`/`OsAction`）与 `init_app_menus` 回调桥接 |
| `gpui/src/platform.rs` | 契约层：`set_menus`、`show_system_notification`、`set_app_identity` 等方法签名与 `SystemNotification` 模型 |
| `gpui/src/app.rs` | 应用层包装：`App::set_menus`（注入 Keymap）、`App::show_system_notification` 等 |
| `gpui_macos/src/platform.rs` | macOS：用 Keymap 把菜单编译成原生 NSMenu；菜单点击/校验的 ObjC 回调 |
| `gpui_macos/src/system_notifications.rs` | macOS：`UNUserNotificationCenter` 封装（懒初始化、category、tag 替换） |
| `gpui_windows/src/platform.rs` | Windows：菜单存储、`set_app_identity`（AUMID）、通知与跳转列表的装配 |
| `gpui_windows/src/system_notifications.rs` | Windows：toast 通知（XML 文档、tag 哈希、手动替换） |
| `gpui_windows/src/destination_list.rs` | Windows：跳转列表（最近工作区 + 任务类别）的 COM 实现 |
| `gpui_linux/src/linux/platform.rs` | Linux 外壳：委托给 `SystemNotificationState`、菜单仅存储 |
| `gpui_linux/src/linux/system_notifications.rs` | Linux：notify-rust 走 XDG D-Bus 通知 |
| `title_bar/src/application_menu.rs` | （消费方）非 macOS 平台上用 `get_menus` 渲染应用内菜单栏 |

## 4. 核心概念与源码讲解

### 4.1 app_menu 数据模型：Menu、MenuItem、OwnedMenu 与 OsAction

#### 4.1.1 概念说明

「应用菜单」指的是窗口顶部那一排 File/Edit/View…（macOS 是屏幕顶部的系统菜单栏）。GPUI 对它的建模面临一个张力：

- 菜单要**跨平台、跨窗口系统**传递，而各平台菜单能力差异极大（macOS 有原生菜单栏，Windows/Linux 没有）；
- 菜单项要携带 **Rust 的 `Box<dyn Action>`**，而 `Box<dyn Action>` 不可 `Clone`（除非逐个 `boxed_clone`）。

于是有了两套并存的模型：

- **`Menu`/`MenuItem`**：构建期类型，用 builder 风格 API 组装，`name` 用 `SharedString`；
- **`OwnedMenu`/`OwnedMenuItem`**：可 `Clone` 的「定稿」版本，供 `get_menus` 随时取回一份快照（Windows/Linux 的应用内菜单栏渲染、以及 UI 层重新展示菜单时都要克隆它）。

`MenuItem::Action` 里还挂了两个「OS 感知」扩展点：

- `os_action: Option<OsAction>`：声明这个菜单项对应系统的剪切/复制/粘贴/全选/撤销/重做，让 macOS 能把它接到原生 selector（后文 4.2.3）；
- `MenuItem::SystemMenu(OsMenu)`：整块子菜单由**操作系统填充**——目前唯一的变体是 macOS 的 Services 菜单，内容由系统根据注册了哪些服务决定，应用只负责占位。

#### 4.1.2 核心流程

构建一组菜单的典型流程：

```text
Menu::new("File")
    .items(vec![
        MenuItem::action("New File", NewFile),                       // 普通动作项
        MenuItem::os_action("Copy", Copy, OsAction::Copy),           // 带 OS 语义的动作项
        MenuItem::separator(),                                       // 分隔线
        MenuItem::submenu(Menu::new("Open Recent").items(...)),      // 子菜单（可嵌套）
        MenuItem::os_submenu("Services", SystemMenuType::Services),  // 系统填充的子菜单
    ])
    .disabled(false)
```

- 每个 `Action` 项可用 `.checked(bool)` 渲染为勾选态、`.disabled(bool)` 置灰；
- `Menu::owned()` / `MenuItem::owned()` 把构建期模型转换为可克隆的 `OwnedMenu`，转换中 `action` 原样搬移（`Box<dyn Action>` 移动所有权），克隆 `OwnedMenuItem` 时才用 `boxed_clone`。

`OsAction` 枚举与 `SystemMenuType` 是典型的「**能力下探**」设计：GPUI 不理解「复制」是什么，但它把语义标签带在模型里，交给能理解的平台去兑现，其余平台自然忽略。

#### 4.1.3 源码精读

[app_menu.rs:L4-L13](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform/app_menu.rs#L4-L13)——`Menu` 结构体：`name` + `items` + `disabled` 三个字段，是最朴素的三元组。

[app_menu.rs:L76-L104](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform/app_menu.rs#L76-L104)——`MenuItem` 枚举的四个变体：`Separator`、`Submenu(Menu)`（递归嵌套）、`SystemMenu(OsMenu)`、`Action { name, action, os_action, checked, disabled }`。注意 `action` 是 `Box<dyn Action>`——不可克隆，这正是需要 Owned 系列的原因之一。

[app_menu.rs:L307-L329](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform/app_menu.rs#L307-L329)——`OsAction` 枚举：Cut/Copy/Paste/SelectAll/Undo/Redo。注释里的 TODO 说明这是「全局选择重构」的过渡方案——目前它还把平台细节泄漏给了 GPUI 用户。

[app_menu.rs:L236-L247](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform/app_menu.rs#L236-L247) 与 [app_menu.rs:L280-L301](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform/app_menu.rs#L280-L301)——`OwnedMenu` 定义与 `OwnedMenuItem` 的手写 `Clone` 实现：`action: action.boxed_clone()`，name 从 `SharedString` 变为 `String`。

[app_menu.rs:L361-L425](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform/app_menu.rs#L361-L425)——同文件的单元测试：不依赖任何平台，直接断言 builder 产出的结构。这是纯数据模块可测试性的好范例。

#### 4.1.4 代码实践

1. **实践目标**：验证菜单模型是纯数据、不依赖平台。
2. **操作步骤**：在任意能编译 gpui 的 crate 里写一个测试（或临时 main），构造「带勾选项、带禁用子菜单、带 Services 系统菜单」的三级菜单，然后调用 `.owned()` 再 `.clone()`。
3. **需要观察的现象**：克隆后的 `OwnedMenuItem::Action` 与原对象字段一致；`checked(true)` 只对 `Action` 变体生效，对 `Separator` 调用会被静默忽略（`_ => {}` 分支）。
4. **预期结果**：编译通过、断言成立。也可以直接运行 gpui crate 里的 `cargo test -p gpui test_menu`（该测试位于上方源码精读链接中，纯逻辑、无需窗口）。
5. 本实践不依赖操作系统，预期可以确定性通过；若你的环境无法编译 gpui，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么需要 `OwnedMenu`？直接给 `Menu` 实现 `Clone` 不行吗？

**答案**：`MenuItem::Action` 持有 `Box<dyn Action>`，而 `dyn Action` 只提供 `boxed_clone` 不能自动 `Clone`；并且 `get_menus` 需要在任意时刻返回菜单快照（平台内部还要长期持有），必须有一个「定稿 + 可克隆」形态。`OwnedMenu` 把克隆语义集中到手写的 `Clone` 实现里，构建期的 `Menu` 则保持简单。

**练习 2**：`MenuItem::os_submenu("Services", SystemMenuType::Services)` 在 Linux 上会发生什么？

**答案**：Linux 的 `set_menus` 只做 `menu.owned()` 存储（见 4.2.3），`SystemMenu` 变体被原样保存进 `OwnedMenuItem::SystemMenu`，但应用内菜单栏渲染时会按普通（空）子菜单或忽略处理；只有 macOS 会在创建 NSMenu 时特判它（见 4.2.3 macOS 一节）。

**练习 3**：`checked` 与 `disabled` 这两个布尔状态由谁在什么时机更新？

**答案**：由应用层在每次菜单状态变化时**重新调用 `cx.set_menus(...)` 整体替换**（macOS 会重建整条 NSMenu），而不是原地修改。Zed 在动作可用性变化时就是这么做的；macOS 侧还有一个 `validate` 回调在菜单展开时实时校验灰化（见 4.2.3）。

### 4.2 Platform::set_menus 契约与三方落地：从 Keymap 到原生菜单

#### 4.2.1 概念说明

契约层只声明两个方法：

```rust
fn set_menus(&self, menus: Vec<Menu>, keymap: &Keymap);
fn get_menus(&self) -> Option<Vec<OwnedMenu>> { None }
```

`set_menus` 是**必需方法**（无默认实现），意味着每个平台都要表态；`get_menus` 默认返回 `None`（「本平台不保存菜单」）。

关键设计点：**菜单与快捷键的绑定不在菜单模型里**。`MenuItem::Action` 不携带任何按键信息，`set_menus` 额外接收 `&Keymap`，由平台在「编译菜单」时**反查**该 action 在 Keymap 中的绑定，把快捷键显示出来。这保证了一份菜单定义与一份键位表各自独立演化（用户改键位后重新 `set_menus` 即可刷新菜单显示）。

三方落地策略截然不同：

| 平台 | set_menus 行为 | get_menus | 菜单栏归属 |
|---|---|---|---|
| macOS | 用 Keymap 编译成原生 NSMenu，挂到 NSApplication | 返回存储的 Owned 快照 | 操作系统（屏幕顶部） |
| Windows | 仅存储 `OwnedMenu` | 返回存储 | 应用自绘（title_bar crate） |
| Linux | 仅存储 `OwnedMenu` | 返回存储 | 应用自绘；`set_dock_menu` 为 `todo(linux)` |
| Web | no-op | 默认 None | 无 |

#### 4.2.2 核心流程

macOS 上一次 `cx.set_menus(menus)` 的完整链路：

```text
App::set_menus(menus)
  └─ platform.set_menus(menus, &keymap)            # app.rs 注入 Keymap
       └─ MacPlatform::set_menus
            ├─ create_menu_bar(&menus, delegate, &mut menu_actions, keymap)
            │    └─ 对每个 Action 项：
            │         ├─ keymap.bindings_for_action(action) 反查按键
            │         ├─ OsAction → ObjC selector（cut:/copy:/paste:…）
            │         ├─ NSMenuItem initWithTitle:action:keyEquivalent:
            │         └─ item.setTag(index); menu_actions.push(action)   # 序号↔动作表
            ├─ app.setMainMenu_(menu)               # 挂上屏幕顶栏
            └─ state.menus = menus.owned()          # 存快照供 get_menus
```

用户点击菜单项时反向流动：

```text
NSMenuItem 点击 → selector handleGPUIMenuItem:
  → handle_menu_item：按 item.tag 取回 menu_actions[index]
  → menu_command 回调（即 init_app_menus 注册的 on_app_menu_action）
  → App::dispatch_action(action)                    # 与键盘快捷键同一条分发通路
```

Windows/Linux 上则没有这条反向链：应用内菜单栏（`title_bar` crate 的 `ApplicationMenu`）直接用 gpui 的 popover/context menu 渲染 `get_menus()` 的结果，点击项后由 UI 层直接 dispatch `OwnedMenuItem` 里携带的 action——平台层完全不经手点击事件。

#### 4.2.3 源码精读

[app.rs:L2409-L2412](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L2409-L2412)——`App::set_menus`：注意它把 `self.keymap.borrow()` 一并传入，这是 Keymap 进入菜单的唯一入口；`get_menus`/`set_dock_menu`/`perform_dock_menu_action` 在 [app.rs:L2415-L2427](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L2415-L2427) 同样是薄转发。

[app_menu.rs:L331-L359](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform/app_menu.rs#L331-L359)——`init_app_menus`：gpui 启动时向平台注册三个回调：菜单即将展开时清空 pending keystrokes（避免半截按键序列泄漏进绑定匹配）、菜单项校验（`is_action_available` 决定灰化）、菜单动作（`dispatch_action`）。这是「平台菜单点击 → gpui action 分发」的桥。

[platform.rs:L1041-L1051](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1041-L1051)——`MacPlatform::set_menus`：先持锁用 `create_menu_bar` 生成 NSMenu（借出 `menu_actions`），**构造完立刻 drop 锁**再 `setMainMenu_`，最后另取一次锁存 Owned 快照——锁的粒度被刻意切小，防止 AppKit 回调重入死锁（u6-l1 讲过这把大锁）。

[platform.rs:L329-L347](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L329-L347)——Keymap 反查：`keymap.bindings_for_action(action)` 找出该动作的所有绑定，再用一个默认的 Workspace/Pane/Editor 三层 `KeyContext` 过滤掉带谓词的上下文绑定，取第一个匹配绑定的 keystrokes。注释指出这里**故意取较早的绑定**（通常 later 优先显示），链接指向 Zed issue #23621 的讨论——一个有据可查的取舍。

[platform.rs:L349-L359](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L349-L359)——`OsAction` 到 ObjC selector 的映射：Cut/Copy/Paste/SelectAll 接原生 `cut:`/`copy:`/`paste:`/`selectAll:`，而 Undo/Redo **故意**回落到 `handleGPUIMenuItem:`——因为没有 NSTextView 可供系统执行撤销，注释写明系统菜单项会永远灰掉。

[platform.rs:L361-L399](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L361-L399)——构造 NSMenuItem：单键绑定转成 `keyEquivalent` + 修饰键掩码（`platform` 位映射为 Command 键，呼应 u3-l3 讲过的修饰键归一化）；多键绑定或无绑定时 keyEquivalent 留空（macOS 菜单不显示多键序列）。[platform.rs:L424-L426](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L424-L426) 把 `actions.len()` 写进 item tag 并把 action 压入 `menu_actions` 表。

[platform.rs:L1441-L1456](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1441-L1456) 与 [platform.rs:L1458-L1479](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/platform.rs#L1458-L1479)——点击与校验的 ObjC 回调：都按 `item.tag` 查 `menu_actions` 表；注意「take 回调 → drop 锁 → 调用 → 归还回调」的舞蹈，与 u5-l1 的 PlatformHandlers 同型，避免回调内部再碰平台状态时 RefCell/Mutex 重入。

[platform.rs:L738-L744](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L738-L744)——`WindowsPlatform::set_menus`：只做 `menus.owned()` 存储，`get_menus` 返回 `Some(clone)`。Windows 没有原生应用菜单栏，存储就是为了给应用内渲染供货。

[application_menu.rs:L52-L69](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/title_bar/src/application_menu.rs#L52-L69)——消费方证据：title_bar crate 的 `ApplicationMenu` 视图在构造时 `cx.get_menus().unwrap_or_default()`，把 OwnedMenu 渲染成 Zed 自绘菜单栏（非 macOS 平台你在 Zed 窗口里看到的那一排就是它）。

[platform.rs:L621-L633](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/platform.rs#L621-L633)——Linux 外壳：`set_menus` 存进 `LinuxCommon.menus`，`get_menus` 返回克隆；`set_dock_menu` 是显式的 `// todo(linux)` 空实现——契约是必需方法，但实现方可以选择先交白卷。

[platform.rs:L471-L473](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_web/src/platform.rs#L471-L473)——Web 平台：`set_menus`/`set_dock_menu` 均为空实现，浏览器没有应用菜单的概念。

#### 4.2.4 代码实践

1. **实践目标**：观察「同一份 `set_menus` 调用」在你当前平台上的不同归宿。
2. **操作步骤**：
   - 写一个最小 gpui 应用（参照 u1-l2 的 `application()` 模板），在 `run` 回调里调用：
     ```rust
     // 示例代码：需先在项目中定义自己的 action（此处借用 gpui::NoAction 演示结构）
     cx.set_menus(vec![gpui::Menu::new("File").items(vec![
         gpui::MenuItem::action("Do Nothing", gpui::NoAction),
         gpui::MenuItem::separator(),
         gpui::MenuItem::action("Quit", zed_actions::Quit), // 或任意可用的 action
     ])]);
     println!("{:?}", cx.get_menus().map(|m| m.len()));
     ```
   - 在 Linux 上运行：屏幕顶部不会有任何菜单出现。
   - 有条件的话在 macOS 上（打包成 .app 或直接 cargo run）运行同样代码。
3. **需要观察的现象**：Linux/Windows 上 `get_menus()` 返回 `Some(1)`，但操作系统 UI 无变化；macOS 上屏幕顶部出现 "File" 菜单。
4. **预期结果**：验证「Windows/Linux 只存储、macOS 编译成原生 NSMenu」的结论。macOS 分支「待本地验证」（本环境为 Linux）。
5. 记录你平台上 `set_menus` 最终落入哪个函数：Linux 是 `platform.rs:621`，Windows 是 `platform.rs:738`，macOS 是 `platform.rs:1041`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `set_menus` 要把 `Keymap` 作为参数传给平台，而不是让平台自己从某处取？

**答案**：Keymap 属于 gpui 应用层状态（`App` 里的 `keymap: RefCell<Keymap>`，用户加载键位文件后可重建）。把它作为参数传入，平台层就不必反向依赖应用状态；同时签名明确表达了「菜单编译需要键位表此刻的快照」这一事实，避免平台在菜单存活期间持有可能失效的引用。

**练习 2**：macOS 上用户点击 Edit → Copy，最终是 `cut:` 这类 selector 生效，还是 gpui 的 action 分发生效？

**答案**：走的是 `copy:` selector 直达系统文本栈（例如让 NSTextView 执行复制），**绕过了** gpui 的 action 分发；这正是 `OsAction::Copy` 存在的意义——把文本编辑原语交还给系统，剪贴板写人仍是系统。而 Undo/Redo/普通项走 `handleGPUIMenuItem:` → `menu_actions` 表 → `dispatch_action` 回到 gpui。

**练习 3**：在 Windows 上修改了菜单后，UI 上的菜单栏多久刷新？

**答案**：取决于应用层。`set_menus` 只更新平台存储；title_bar 的 `ApplicationMenu` 在构造时读一次 `get_menus()`，所以 Zed 的做法是在菜单变化时重建/通知该视图（配合 settings 订阅），而不是平台自动推送。平台层不提供菜单变更事件。

### 4.3 Platform::show_system_notification：三方通知实现

#### 4.3.1 概念说明

通知的数据模型非常小（契约层定义）：

- `SystemNotification { tag, title, body, actions }`——`tag` 是**稳定身份**：契约文档明确「用相同 tag 发新通知会在支持的平台上替换旧通知」，且响应回调会把 tag 带回来让应用对号入座；
- `SystemNotificationAction { id, label }`——通知上的按钮：`label` 给人看，`id` 给程序用；
- `SystemNotificationResponse { tag, action_id: Option }`——用户激活了通知；`action_id == None` 表示点击了**通知本体**而非某个按钮。

三个契约方法都是**默认 no-op**（`show_system_notification`/`dismiss_system_notification`/`on_system_notification_response`），所以 Web 平台与 headless 天然沉默；macOS、Windows、Linux 各有一份 `SystemNotificationState` 实现。三方在两个核心问题上的答案完全不同：

**问题一：怎么实现「同 tag 替换」？**

- macOS：把 tag 直接用作 `UNNotificationRequest` 的 identifier，系统原生支持「同 id 新请求顶掉旧请求」；
- Windows：toast 的 tag 有 64 字符上限且语义不同，平台**自己维护 `HashMap<tag, ToastNotification>`**，show 前先 `Hide` 掉同 tag 旧 toast——手动替换；
- Linux：XDG 通知协议没有按应用指定 id 替换的标准路径，**不支持替换**——每次 show 都是一条新通知。

**问题二：响应回调如何回到主线程？**

三方答案却高度一致（这是本讲最值得吸收的模式）：OS 回调都发生在平台线程（macOS 的 delegate 任意线程、Windows 的 toast 事件、Linux 的每通知专用线程），实现方一律用 `futures::mpsc::unbounded` 通道把 `SystemNotificationResponse` 发回，再在**前台执行器**上 spawn 一个常驻任务排空通道、调用用户回调——这正是 u4 系列讲的「唤醒即再投递」与主线程约束的应用。回调调用时还要「take 出来再归还」，防止回调内部重入平台（比如在回调里 dismiss 自己）造成借用冲突。

#### 4.3.2 核心流程

以「发一条带两个按钮、tag 为 "download" 的通知，用户点了第二个按钮」为例：

```text
cx.show_system_notification(SystemNotification {
    tag: "download", title: "下载完成", body: "zed.tar.gz",
    actions: vec![{id:"show", label:"显示"}, {id:"dismiss", label:"忽略"}],
})
  └─ 平台实现
       ├─ macOS: 注册 category（动作集合→类别 id）→ 构造 content →
       │         request(identifier=tag, trigger=None 立即投递)
       ├─ Windows: 构造 toast XML（<actions><action content=label arguments=id/>…）→
       │           tag 哈希成 16 位十六进制 → Hide 同 tag 旧 toast → Show
       └─ Linux: notify-rust builder（transport id "gpui-action-{i}" ←→ 动作 id 映射表）→
                 专用线程 show() + wait_for_action()

用户点击按钮
  ├─ macOS: delegate didReceiveNotificationResponse（任意线程）→ channel
  ├─ Windows: ToastActivated 事件携带 arguments=id → channel
  └─ Linux: wait_for_action 回调收到 transport id → 查映射表还原动作 id → channel
       ↓ 三方同型
前台任务排空 channel → on_system_notification_response 回调
  → App::on_system_notification_response 包装层 → 用户闭包(response, &mut App)
```

#### 4.3.3 源码精读

[platform.rs:L263-L291](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L263-L291)——契约三件套：三个方法全部默认 no-op，文档分别说明 tag 替换语义、dismiss 的 best-effort 性质（有些平台收不回已显示的通知）、以及「实现必须在主线程上调回调」的硬性要求。[platform.rs:L374-L409](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L374-L409) 是三个模型的定义。

[app.rs:L1512-L1538](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L1512-L1538)——应用层包装：`on_system_notification_response` 把平台层的裸回调包一层，用 `WeakEntity` 升级后给用户闭包追加 `&mut App` 参数——平台层只见 `FnMut(response)`，App 层才补上上下文。

**macOS**（[system_notifications.rs:L1-L6](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/system_notifications.rs#L1-L6) 模块注释开宗明义）：

- [L118-L139](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/system_notifications.rs#L118-L139)——**懒初始化守卫**：`UNUserNotificationCenter` 在进程不在 app bundle 里时（`cargo run` 的开发场景）会抛 `NSInternalInconsistencyException` **直接 abort 进程**，所以用 `NSBundle::mainBundle().bundleIdentifier().is_none()` 判断，不在 bundle 里就整块禁用通知。这解释了为什么开发构建的 Zed 调 `show_system_notification` 什么也不会发生。
- [L164-L195](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/system_notifications.rs#L164-L195)——`show`：先 `request_authorization`（只请求一次，Alert|Sound）；tag 直接作 request identifier、trigger 传 `None` 表示立即投递——注释点明「复用 tag 作为 identifier 让新通知替换旧通知」就是替换语义的全部实现。
- [L197-L231](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/system_notifications.rs#L197-L231)——`register_category`：macOS 的通知按钮属于「类别（category）」，且 `setNotificationCategories` 每次**整体替换**注册集；这里以动作集合（`Vec<SystemNotificationAction>` 作 HashMap 键）为粒度缓存已注册类别，每次新增都并集重设。
- [L255-L299](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/system_notifications.rs#L255-L299)——delegate 两个方法：`didReceiveNotificationResponse` 把默认激活（点本体）折叠为 `action_id: None`；`willPresentNotification` 强制返回 Banner|List——注释解释不加的话 macOS 会在应用前台时**吞掉横幅**，而「要不要在前台时弹」应该由应用决策而非平台。
- [L73-L100](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/system_notifications.rs#L73-L100)——`initialize`：建通道、spawn 前台排空任务，take/归还回调的舞蹈在这里（注释：回调可能重入平台或替换自身）。

**Windows**（[system_notifications.rs（windows）:L44-L91](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/system_notifications.rs#L44-L91)）：

- [L56-L63](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/system_notifications.rs#L56-L63)——tag 哈希：Windows toast tag 上限 64 字符（Creators Update 后），GPUI 的 tag 是任意长 `SharedString`，所以 `DefaultHasher` 压成 16 位十六进制再 `SetTag`。
- [L85-L89](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/system_notifications.rs#L85-L89)——手动替换：`active_toasts.remove(&tag)` 拿到旧 toast 先 `Hide` 再 `Show` 新的、重新插入表——对比 macOS 的一行原生替换，这里能看到「平台能力差异由实现层吸收」的实例。
- [L157-L188](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/system_notifications.rs#L157-L188)——`toast_document`：Windows toast 的内容是一段 XML（ToastGeneric 模板），按钮是 `<action content=label arguments=id/>`，`arguments` 就是激活时回传的 `action_id`；最后强制 `<audio silent="true"/>`——GPUI 的系统通知不发声。
- [L129-L154](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/system_notifications.rs#L129-L154)——`notifier` 的两条路：有包身份（MSIX）用 `CreateToastNotifier()`；否则要求已设置 app identity，用 `CreateToastNotifierWithId(AUMID)`，并在 [L191-L198](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/system_notifications.rs#L191-L198) 向注册表写 `HKCU\Software\Classes\AppUserModelId\{id}` 的 DisplayName，让无开始菜单快捷方式的裸进程也能正确显示 toast 归属。

**Linux**（[system_notifications.rs（linux）:L38-L87](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/system_notifications.rs#L38-L87)）：

- [L47-L52](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/system_notifications.rs#L47-L52)——**transport id 隔离**：XDG 动作 key 是任意字符串，且 `"default"`（点本体）和 notify-rust 的 `"__closed"`（未激活就关闭）是保留值；调用方的 `action.id` 可能恰好撞上它们，所以先映射成 `gpui-action-{index}` 发出去，回包时用 [L121-L130](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/system_notifications.rs#L121-L130) 的 `response_action_id` 反查还原；[L132-L154](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/system_notifications.rs#L132-L154) 的单元测试专门验证「调用方动作 id 与 transport id 不冲突」。
- [L57-L83](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/system_notifications.rs#L57-L83)——**每条通知一个线程**：注释说明 `show()` 要连 D-Bus 会话总线、`wait_for_action` 会**阻塞到通知关闭**，两者都不能在 UI 线程做，于是 `std::thread::Builder` 起名为 "system-notification" 的线程串起全程，结果经通道送回。
- [L89-L93](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/system_notifications.rs#L89-L93)——`dismiss` 空实现：协议只允许用服务端分配的 id 关闭**活句柄**，而那个 id 已被 `wait_for_action` 消费掉了；过期通知只能等它自己从通知中心滚出——契约文档说的「best-effort」在此具象化。
- [platform.rs:L569-L595](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/platform.rs#L569-L595)——Linux 外壳委托：`set_app_identity` 只留 `name`（D-Bus 通知的 appname）、三个通知方法全量转给 `common.system_notifications`——LinuxCommon 聚合公共状态的又一次体现（u5-l1）。

#### 4.3.4 代码实践

1. **实践目标**：在你当前平台发出一条真实系统通知并收到点击回调。
2. **操作步骤**：
   - 基于一个最小 gpui 应用（Linux 上直接 `cargo run` 即可，无需 bundle）：
     ```rust
     // 示例代码
     cx.show_system_notification(gpui::SystemNotification {
         tag: "demo".into(),
         title: "你好".into(),
         body: "点击我".into(),
         actions: vec![gpui::SystemNotificationAction {
             id: "greet".into(), label: "打招呼".into(),
         }],
     });
     cx.on_system_notification_response(|response, _cx| {
         println!("tag={} action_id={:?}", response.tag, response.action_id);
     });
     ```
   - 点击通知本体，再重新运行并点击按钮。
3. **需要观察的现象**：控制台分别打印 `action_id=None`（点本体，XDG 的 default 动作）与 `action_id=Some("greet")`（transport id 还原回调用方 id）；再快速连发两次同 tag 通知，观察 Linux 上**出现两条**而非一条。
4. **预期结果**：验证「Linux 不做 tag 替换」与「回调回主线程」。需要 D-Bus 会话与通知守护进程；无桌面环境（headless 服务器）时 notify-rust 连不上总线只会 `log::warn`，不 panic——这一点也值得观察。「待本地验证」：具体横幅样式取决于你的通知守护进程。
5. 对照源码记录路径：你的这次调用经过 [platform.rs:L574-L580](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/platform.rs#L574-L580)（外壳）→ [system_notifications.rs（linux）:L38](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/system_notifications.rs#L38)（show）。

#### 4.3.5 小练习与答案

**练习 1**：macOS 开发者 `cargo run` 一个 GPUI 应用调 `show_system_notification`，为什么毫无反应也不报错？

**答案**：`NotificationCenter::new` 检测 `bundleIdentifier()` 为 None（不在 .app bundle 中）时返回 None 并 `log::info!("system notifications disabled: not running from an app bundle")`——因为继续初始化 `UNUserNotificationCenter` 会触发 `NSInternalInconsistencyException` 直接 abort 进程。禁用比崩溃优雅。

**练习 2**：三方的 `on_response` 实现里都有同一段「`borrow_mut().take()` → 调用 → `get_or_insert` 归还」的代码，为什么？

**答案**：回调是 `Rc<RefCell<Option<...>>>` 共享的。若直接 `borrow_mut()` 持有引用再调用，回调内部一旦重入平台（例如在回调里调 `dismiss_system_notification`，它会再碰同一块状态）就会 double borrow panic。先 take 出来、调用完再放回，保证调用期间 RefCell 空置。这与 u5-l1 的 PlatformHandlers、macOS 菜单回调是同一个模式的三次重现。

**练习 3**：`SystemNotificationResponse.action_id` 为什么是 `Option<SharedString>` 而不是一个带 `Default` 变体的枚举？

**答案**：因为动作按钮的 id 由**应用**任意指定（`SystemNotificationAction.id`），平台无法枚举其取值；唯一能被平台识别的特殊情形就是「点了通知本体而非任何按钮」，用 `None` 表达恰好。tag 则必须原样带回，供应用把响应与请求配对。

### 4.4 set_app_identity 与 Windows 跳转列表：destination_list

#### 4.4.1 概念说明

`set_app_identity(identifier, name)` 是一个**默认 no-op** 的契约方法，文档要求「在启动早期、开窗和发通知之前调用一次」。它的三个落点：

- **Windows（真正的主角）**：为非打包进程显式设置 AUMID。Windows 的任务栏分组、跳转列表（右键任务栏图标出现的菜单）、toast 通知归属全靠 AUMID 认亲——没有它，任务栏会把你的窗口当成无名进程，toast 甚至弹不出来。
- **Linux**：只把 `name` 存下来当 D-Bus 通知的 appname（identifier 弃用）。
- **macOS**：不覆写（应用身份由 .app bundle 的 Info.plist 决定，无处可设）。

跳转列表（Jump List）是 Windows 专属的「任务栏图标右键菜单」：分「任务（Tasks）」「最近（Recent）」等类别。Zed 用它放两样东西：任务类别的 "New Window"，以及最近工作区列表。`destination_list.rs` 就是用 COM 接口 `ICustomDestinationList` 组装这份列表的模块。

跳转列表有一个独特的**双向数据流**：除了下行（应用 → 系统）的菜单项，`BeginList` 还会上行返回 `removed`——**用户曾在任务栏里手动移除过的条目**。应用拿到这份名单后应当把对应工作区从自己的历史里删掉（否则下次又写回去，用户移除等于白点）。

还有一个绕不开的鸿沟：**跳转列表项点击启动的是一个新进程**。列表项本质是 `IShellLinkW`（指向当前 exe 的快捷方式 + 命令行参数），用户点它 = 重新拉起 `zed.exe --dock-action 0` 或 `zed.exe "/path/to/workspace"`。任务类别的参数是 `--dock-action {index}`，新进程启动后经 `Platform::perform_dock_menu_action(index)` 走一条 PostMessage 链路把动作派发出去。

#### 4.4.2 核心流程

```text
应用: cx.update_jump_list(vec![MenuItem::action("New Window", NewWindow)], 最近工作区)
  └─ WindowsPlatform::update_jump_list（trait 实现）
       ├─ MenuItem → DockMenuItem（仅支持 Action 变体，其余 bail）
       ├─ 存入 state.jump_list（dock_menus + recent_workspaces）
       └─ background_spawn:
            update_jump_list(&recent, &dock)          # destination_list.rs
              ├─ create_destination_list
              │    ├─ CoCreateInstance(DestinationList)
              │    └─ BeginList → user_removed: IObjectArray   # 上行：用户移除的条目
              ├─ add_recent_folders：过滤掉 removed →
              │    每个 IShellLinkW(path=当前exe, args="路径A" "路径B",
              │                      icon=explorer.exe 模拟文件夹图标)
              │    → AppendCategory("Recent Folders")
              ├─ add_dock_menu：每项 IShellLinkW(args="--dock-action {i}")
              │    → AddUserTasks                              # 任务类别
              └─ CommitList
       └─ 返回 Task<Vec<工作区路径>>（removed）→ workspace 的 HistoryManager
            收到后把用户移除的工作区从本地历史中删除
```

`set_app_identity` 的前置地位：`show_system_notification` 在 Windows 上若进程无包身份且未设 AUMID，会打 warn 并**直接放弃**（返回 `Ok(())`，见 4.3.3 notifier 一节）——这就是「必须先 set_app_identity 才能发通知」的机制根源。

#### 4.4.3 源码精读

[platform.rs:L253-L261](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L253-L261)——契约：`set_app_identity` 默认 no-op，文档写明 identifier 用于 Windows AppUserModelID、name 用于 OS 向用户展示，且要求开窗/发通知前调用。[platform.rs:L239-L245](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform.rs#L239-L245)——`update_jump_list` 默认返回空 `Task`，Windows 之外的平台调用它等于空操作。

[platform.rs:L693-L709](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L693-L709)——Windows 的 `set_app_identity`：**有包身份（MSIX 打包）直接 return**——系统已自动授予 AUMID，人工设置反而多余；否则调 Win32 `SetCurrentProcessExplicitAppUserModelID` 并把 `(identifier, name)` 存进状态，供日后创建 toast notifier 使用（4.3.3 已见）。

[platform.rs:L947-L953](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L947-L953) 与 [platform.rs:L273-L296](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L273-L296)——trait 实现（签名带 `Task` 返回值）转调内部方法：转换菜单、更新 `jump_list` 状态、`background_spawn` 执行 COM 组装并 `detach`。跳转列表要碰 COM 与注册表，全程放后台线程。

[destination_list.rs:L64-L73](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/destination_list.rs#L64-L73)——组装主流程四步：建列表对象（并拿 removed）→ 加最近文件夹 → 加任务 → `CommitList` 提交。文件头注释标明参考了微软官方示例。

[destination_list.rs:L82-L112](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/destination_list.rs#L82-L112)——`create_destination_list`：`BeginList` 返回的 `IObjectArray` 是**用户手动移除过的条目**；每条还原成 `IShellLinkW`，用 `GetDescription` 读出创建时塞进去的、以 `\n` 连接的工作区路径串，再 split 回 `SmallVec<[PathBuf; 2]>`——**description 字段被当作结构化数据的载体**（每个工作区可含多个文件夹，对应 Zed 的多根工作区）。

[destination_list.rs:L133-L183](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/destination_list.rs#L133-L183)——`add_recent_folders`：先 `filter(|path| !removed.contains(path))` 尊重用户移除；图标用 `explorer.exe` **冒充文件夹图标**（注释链接到 VS Code 的同款技巧）；列表非空才 `AppendCategory`，避免出现空类别。

[destination_list.rs:L114-L131](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/destination_list.rs#L114-L131) 与 [destination_list.rs:L185-L207](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/destination_list.rs#L185-L207)——任务类别与 `create_shell_link`：每个快捷方式 `SetPath(当前exe)`、`SetArguments`，再经 `IPropertyStore` 写 `PKEY_TITLE` 控制显示名。任务项参数是 `--dock-action {index}`。

[platform.rs:L935-L945](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_windows/src/platform.rs#L935-L945)——`perform_dock_menu_action`：`PostMessageW(WM_GPUI_DOCK_MENU_ACTION)` 附验证号（validation number，防伪造，u4-l4 讲过）与索引，窗口过程收到后触发 dock 动作——新进程里执行「任务类别第 N 项」的完整闭环。

[history_manager.rs:L101-L115](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/workspace/src/history_manager.rs#L101-L115)——上行数据流的消费方：`HistoryManager` 每次历史变更都调 `cx.update_jump_list`，await 返回的 `user_removed`，把其中包含的工作区从本地历史删除——应用与系统视图最终一致。

#### 4.4.4 代码实践

1. **实践目标**：理解 `set_app_identity` 的跨平台差异与跳转列表的纯数据部分（无需 Windows 也能做）。
2. **操作步骤**：
   - 在 Linux 最小应用里调用 `cx.set_app_identity("dev.example.MyApp", "My App");`，随后发一条系统通知。
   - 用 `RUST_LOG=info cargo run` 观察：通知守护进程收到的 appname 是否为 "My App"（对照 [platform.rs:L569-L572](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/platform.rs#L569-L572)，identifier 被丢弃）。
   - 源码阅读部分：在 Windows 机器上（或纯读码）跟踪 `cx.update_jump_list(...)` 的调用链，列出它经过的四个函数。
3. **需要观察的现象**：Linux 通知的发送者名称从默认（notify-rust 通常用 "app" 或进程名）变为 "My App"；`update_jump_list` 在 Linux 上立即返回空 Task，无任何系统调用。
4. **预期结果**：Linux 侧 appname 生效可当场验证；Windows 跳转列表行为「待本地验证」（需要 Windows 与非打包进程实测任务栏右键菜单内容）。
5. 记录对照表：`set_app_identity` 在三个平台分别落入 `gpui_windows/src/platform.rs:693`、`gpui_linux/src/linux/platform.rs:569`、（macOS）trait 默认 no-op。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DockMenuItem::new` 对 `MenuItem::Submenu` 等非 Action 变体会 `bail!`？

**答案**：跳转列表的条目最终是 `IShellLinkW` 快捷方式——只有「一个可执行 + 一串参数」的表达能力，没有嵌套子菜单的概念；数据模型天然装不下 `Submenu`/`Separator`。`bail!` 让调用方（`set_dock_menus` 里 `log_err`）跳过不支持的项而不是整批失败。

**练习 2**：用户在任务栏把「最近」里的某个工作区移除了，一周后这个条目还会再出现吗？

**答案**：不会。下次 `update_jump_list` 时 `BeginList` 会再次报告 removed，`add_recent_folders` 会过滤它；并且 `HistoryManager` 在第一次收到 removed 上行时就已把该工作区从本地历史数据库删除——双保险保证用户的选择被尊重。

**练习 3**：任务类别里的 "New Window" 被点击后，动作发生在哪个进程里？

**答案**：在一个**新拉起的进程**里。快捷方式指向当前 exe、参数 `--dock-action 0`；新进程的 Zed 启动逻辑识别该参数后调用 `cx.perform_dock_menu_action(0)`，经 `PostMessageW(WM_GPUI_DOCK_MENU_ACTION)` 送进平台窗口过程，由已注册的 dock 动作回调执行 `NewWindow`。这与 macOS Dock 菜单（点选项在**当前**进程里经 `on_app_menu_action` 派发）是本质不同的两条路。

## 5. 综合实践

把本讲三组能力串成一个「桌面集成自检」小程序（Linux 上可完整运行；其他平台按注释替换预期）：

1. **实践目标**：注册一组带快捷键的应用菜单、发送带 tag 的系统通知并验证替换行为，产出一份「本机平台能力对照表」。
2. **操作步骤**：
   - 新建一个依赖 `gpui` 与 `gpui_platform` 的 crate（参照 u1-l2 模板）；
   - 定义两个 action（如 `Notify`、`Quit`），并加载一个最小 keymap 给 `Notify` 绑定 `ctrl-n`（或直接复用默认 keymap 机制）；
   - 在 `run` 回调里：
     ```rust
     // 示例代码
     cx.set_app_identity("dev.example.MenuDemo", "Menu Demo");
     cx.set_menus(vec![
         Menu::new("Demo").items(vec![
             MenuItem::action("Send Notification", Notify),
             MenuItem::separator(),
             MenuItem::action("Quit", Quit),
         ]),
     ]);
     cx.on_system_notification_response(|resp, cx| {
         println!("通知响应: tag={} action={:?}", resp.tag, resp.action_id);
     });
     ```
   - 在 `Notify` 的 action 处理器（用 `on_action` 或全局监听）里连续两次发送**相同 tag**、标题不同的通知（间隔 2 秒，可用 `cx.background_executor().timer`）；
   - 在通知上放一个按钮，验证点击后 `action_id` 有值。
3. **需要观察的现象**：
   - Linux：菜单不出现在任何系统位置（`get_menus` 却能取回）；两次同 tag 通知**并存**（不替换）；点击通知按钮，控制台在**主线程**打印响应；appname 显示为 "Menu Demo"；
   - Windows（若有条件）：菜单由你自己渲染才可见；同 tag 通知第二条出现时第一条消失（`Hide` 手动替换）；若没调 `set_app_identity` 会看到 warn 日志且通知不发；
   - macOS（若有条件，需 .app bundle）：屏幕顶部出现 Demo 菜单且 `ctrl-n` 显示为快捷键；同 tag 替换原生生效。
4. **预期结果**：完成下表（这正是本讲的考点浓缩）：

   | 能力 | macOS | Windows | Linux |
   |---|---|---|---|
   | 菜单归宿 | 原生 NSMenu | 平台存储 + 应用自绘 | 平台存储 + 应用自绘 |
   | tag 替换 | 原生（identifier） | 手动（Hide+Show） | 不支持 |
   | 响应回主线程 | 通道 + 前台任务 | 通道 + 前台任务 | 通道 + 前台任务 |
   | set_app_identity | no-op | AUMID（通知/跳转列表前置） | 仅存 appname |
   | 跳转列表 | 无 | COM ICustomDestinationList | 无 |

5. 无法在本机验证的格子标「待本地验证」，并在笔记里写上对应实现函数名与行号。

## 6. 本讲小结

- `Menu`/`MenuItem` 是构建期模型、`OwnedMenu` 是可克隆定稿；`OsAction` 与 `SystemMenu` 把「OS 感知语义」挂在纯数据模型上，交由有能力平台兑现。
- 快捷键不进菜单模型：`App::set_menus` 在调用点注入 `&Keymap`，macOS 用 `bindings_for_action` 反查并编译成 NSMenuItem 的 keyEquivalent；菜单点击经 `menu_actions` 表 + `on_app_menu_action` 回到与键盘相同的 `dispatch_action` 通路。
- Windows/Linux 的 `set_menus` 只做存储，`get_menus` 供货给 title_bar crate 自绘菜单栏；Linux 的 `set_dock_menu` 仍是显式 `todo`。
- 系统通知三方在「tag 替换」上分歧最大（macOS 原生替换 / Windows 手动 Hide+Show / Linux 不支持），在「响应回主线程」上完全同型（unbounded 通道 + 前台排空任务 + take/归还回调防重入）。
- `set_app_identity` 的核心消费者是 Windows：AUMID 是任务栏分组、toast、跳转列表的共同锚点；`destination_list.rs` 用 COM 组装跳转列表，且 `BeginList` 会把「用户手动移除的条目」上行回应用，`HistoryManager` 据此删除本地历史。
- 平台能力差异被实现层吸收而非泄漏进契约：通知的 authorize//category/transport id/AUMID 注册等细节对 GPUI 用户全部不可见。

## 7. 下一步学习建议

- 下一讲是第 7 单元第一讲 **u7-l1「WebPlatform 与 wasm 入口」****：离开桌面平台，看浏览器里这些桌面集成（菜单、通知、AUMID）如何全部退化成 no-op，以及 web 入口多出的 http client 注入。
- 若想继续深挖本讲主题：阅读 `gpui/src/platform/app_menu.rs` 的测试与 `title_bar` crate 的 `application_menu.rs` 全文，理解应用内菜单栏如何用 `ContextMenu`/`PopoverMenu` 渲染 `OwnedMenuItem`（含 checked/disabled 的渲染映射）。
- 延伸对照：Zed 应用层如何组织菜单定义——`zed/src/zed.rs` 中 `cx.set_menus(...)`/`cx.set_dock_menu(...)` 的调用点，以及菜单如何随窗口焦点（macOS）刷新 action 可用性。
- 回顾关联讲义：u2-l1（trait 方法分组与本讲方法在八组中的位置）、u4-l4（Windows 消息专用窗口与验证号——`WM_GPUI_DOCK_MENU_ACTION` 正是它的又一用户）、u6-l1（macOS 的大锁与 ObjC 委托模式——本讲的 notification delegate 与菜单回调同源）。
