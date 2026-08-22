# 键盘输入的跨平台抽象：布局、映射与 Keystroke

## 1. 本讲目标

上一讲（u3-l2）我们拆完了 `PlatformWindow` 的窗口控制面；本讲转向同一契约层的**输入面**——键盘。为什么「按了一个键」这件小事需要一整套平台抽象？因为同样按下物理键盘上的一个键，Linux 的 Wayland 合成器给你一段 keymap 文件描述符，macOS 给你一个 `NSEvent`，浏览器给你一个已经按布局解析好的 `KeyboardEvent`。GPUI 必须把它们统一成同一个模型，键位绑定（keymap）才能一份配置全平台通用。学完本讲你应该能：

1. 解释 `Keystroke` / `KeybindingKeystroke` / `Modifiers` 三个数据结构的分工，以及 `key` 与 `key_char` 两个字段为什么必须分开。
2. 说出 `PlatformKeyboardLayout` 与 `PlatformKeyboardMapper` 两个契约 trait 各自回答什么问题，以及 gpui 的 `App` 在什么时机向平台要这两样东西。
3. 走读 Linux 侧的 xkbcommon 三件套（`Context`/`Keymap`/`State` 与 `Keycode`/`Keysym`），理解 `keystroke_from_xkb` 如何把一个 XKB 按键状态翻译成 GPUI 的 `Keystroke`。
4. 对照 macOS（Carbon/HIToolbox 的 TIS + AppKit 事件）与 Web（浏览器 `KeyboardEvent`）两套实现，看同一契约的完全不同落地方式，并理解 macOS 特有的 key equivalents 大表为什么存在。
5. 描述一次按键从操作系统事件到 GPUI 动作（Action）分发的完整链路。

## 2. 前置知识

- **键盘布局（keyboard layout）**：物理按键（键帽位置）到字符的映射表。同一颗物理键在美式 QWERTY 上打出 `;`，在德式 QWERTZ 上打出 `ö`。布局可以运行中切换（输入法、`setxkbmap`、系统设置）。
- **键码（keycode / scancode）与键符号（keysym）**：操作系统报告的是「第几号键被按下」（keycode，纯数字、与布局无关）；keysym 则是 X11 世界发明的「键的符号名」（如 `XK_a`、`XK_Return`），已经过了一层布局翻译。浏览器更进一步：`event.key` 直接给你布局翻译后的字符。
- **修饰键**：ctrl / alt / shift / fn，加上平台键（macOS 的 cmd、Windows 的 win、Linux 的 super）。GPUI 把平台键统一命名为 `platform`（见 `Modifiers` 结构体）。
- **keymap（键位绑定表）与 action**：GPUI 应用把 `"ctrl-s"` 这样的字符串绑定到某个 action（如 `Save`）；按键到达时按当前焦点上下文匹配绑定并分发动作。本讲只追到「Keystroke 参与匹配」为止，keymap 本身的优先级规则不在本讲范围。
- **compose / 死键（dead key）**：先按 `´` 再按 `e` 得到 `é` 的输入方式。第一下按键不产生字符，只进入「组合中」状态，这会直接影响 `key_char` 的取值。
- **xkbcommon**：Linux 桌面事实标准的键盘布局库（X Keyboard Extension 的独立实现），Wayland 合成器与 X11 服务器都用它描述 keymap；GPUI 在客户端用它把按键事件反翻译回字符。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [../gpui/src/platform/keystroke.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs) | 数据模型：`Keystroke`、`KeybindingKeystroke`、`Modifiers`、解析/匹配逻辑 |
| [../gpui/src/platform/keyboard.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keyboard.rs) | 契约层：`PlatformKeyboardLayout`、`PlatformKeyboardMapper` 两个 trait 与 `DummyKeyboardMapper` |
| [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform.rs) | `Platform` trait 的三个键盘方法（L338-L340） |
| [../gpui_linux/src/linux/keyboard.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/keyboard.rs) | Linux 布局实现：`LinuxKeyboardLayout`（一个字符串壳） |
| [../gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs) | Linux 侧 xkb 转换函数集中地：`keystroke_from_xkb`、`modifiers_from_xkb` 等 |
| [../gpui_macos/src/keyboard.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/keyboard.rs) | macOS 布局与映射器：TIS 查询 + key equivalents 大表 |
| [../gpui_macos/src/events.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/events.rs) | macOS 事件桥接：`parse_keystroke` 把 `NSEvent` 翻译成 `Keystroke` |
| [../gpui_web/src/keyboard.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/keyboard.rs) 与 [../gpui_web/src/events.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs) | Web 侧：硬编码布局 + 浏览器事件翻译 |

辅助出镜（正文少量引用）：[../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/app.rs)（契约的消费端）、[../gpui/src/keymap/binding.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/keymap/binding.rs)（mapper 的使用端）、[../gpui_linux/src/linux/wayland/client.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs) 与 [../gpui_linux/src/linux/x11/client.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs)（xkb 的两个调用方）。

## 4. 核心概念与源码讲解

### 4.1 Keystroke 模型：一次按键的平台无关描述

#### 4.1.1 概念说明

`Keystroke` 是 GPUI 对「一次按键」的统一描述，由**平台层构造**、被**上层消费**。它只有三个字段，但每个字段都藏着跨布局的苦心：

- `modifiers`：按下该键时修饰键的状态（`Modifiers` 五个布尔位）。
- `key`：**键帽语义**的键名——美式键盘上印什么就是什么。例如泰语布局上没有 ASCII 字母键，物理 Q 键位置印着 `ๆ`，但 `key` 仍取 ASCII 等价物 `q`。
- `key_char`：**这次按键本来会打出什么字符**。`option-s` 在 macOS 美式布局上是 `ß`；而 `cmd-s` 不产生字符，是 `None`。

`key` 用于匹配快捷键（布局无关），`key_char` 用于文字输入（布局相关）。两者分离后，`ctrl-s` 的绑定在任何布局下都能命中，而文本编辑器仍能拿到正确的插入字符。

另一个容易混淆的角色是 `KeybindingKeystroke`：它是**写在键位绑定表里的**按键描述（从 JSON/字符串解析而来），与运行时产生的 `Keystroke` 匹配。它在 Windows 上额外携带 `display_modifiers`/`display_key`——因为 Windows 的 VK（虚拟键）体系里「按下的键」和「显示给用户的键」经常不一致（`ctrl-@` 实际是按 `ctrl-shift-2`）。

#### 4.1.2 核心流程

一次按键的完整旅程：

```text
操作系统事件（NSEvent / wl_keyboard.key / XCB KeyPress / KeyboardEvent）
    │  平台实现各自翻译（本讲主角）
    ▼
Keystroke { modifiers, key, key_char }
    │  装进统一的输入事件
    ▼
PlatformInput::KeyDown(KeyDownEvent { keystroke, is_held, prefer_character_input })
    │  平台窗口 on_input 回调 → gpui Window
    ▼
dispatch_tree.dispatch_key(…)
    │  Keystroke::should_match 与绑定表中的 KeybindingKeystroke 逐个比对
    ▼
命中的 binding → dispatch_action(动作分发)
    │  未命中也总是通知
    ▼
App 的 keystroke observers（observe_keystrokes 订阅者，拿到 KeystrokeEvent）
```

绑定表那一侧则在**加载时**就要用到平台映射器：`KeyBinding::load` 把 `"ctrl-s"` 字符串先 `Keystroke::parse`，再经 `KeybindingKeystroke::new_with_mapper` 交给平台 mapper 矫正——这正是 macOS key equivalents 表的注入点（见 4.4）。

#### 4.1.3 源码精读

数据模型本体：

- [../gpui/src/platform/keystroke.rs:L17-L33](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs#L17-L33) —— `Keystroke` 三字段定义。注意 `key` 字段的文档注释专门举了泰语布局的例子：非 ASCII 布局上取 ASCII 等价字符，真实打出的字符放进 `key_char`。
- [../gpui/src/platform/keystroke.rs:L446-L471](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs#L446-L471) —— `Modifiers`：五个布尔位，`platform` 字段在 macOS 是 cmd、Windows 是 win 键、Linux 是 super 键；`secondary()` 方法（L479-L493）把「次修饰键」按平台解析成 cmd 或 ctrl。
- [../gpui/src/platform/keystroke.rs:L35-L46](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs#L35-L46) —— `KeybindingKeystroke`：包一个 `inner: Keystroke`，两个 `#[cfg(target_os = "windows")]` 的 display 字段只在 Windows 编译期存在——用条件编译把平台差异挡在结构体内部，访问器 [modifiers()/key()](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs#L309-L340) 在 Windows 上返回 display 版本，其余平台直接转发 `inner`。

匹配与解析这两个关键算法：

- [../gpui/src/platform/keystroke.rs:L74-L113](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs#L74-L113) —— `should_match`：运行时按键（`self`）对绑定表目标（`target`）的匹配。文档注释坦白了难点：捷克键盘上 `$` 要用 `alt-ç` 打出，巴西键盘上 `"` 是 `" space` 两键序列——所以只要 `key_char` 与目标的 `key` 相等且修饰键兼容，也算命中（L84-L98 的宽匹配分支）；Windows 分支（L100-L110）规则不同，单独 `#[cfg]` 出来。兜底是严格相等（L112）。
- [../gpui/src/platform/keystroke.rs:L115-L220](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs#L115-L220) —— `Keystroke::parse`：绑定字符串的语法是 `[secondary-][ctrl-][alt-][shift-][cmd-][fn-]key[->key_char]`；`secondary` 在 macOS 解析为 cmd、其他平台为 ctrl（L143-L150）；`cmd`/`super`/`win` 三个名字都归入 `platform`（L152-L159）；单个大写字母自动拆成 `shift + 小写`（L179-L187）。
- [../gpui/src/platform/keystroke.rs:L228-L236](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keystroke.rs#L228-L236) —— `is_ime_in_progress`：`key_char` 为 `None` 且是无修饰的可打印键，即视为输入法组合未完成——这就是「死键按下等第二个键」的状态判定。

消费端链路（ gpui 主 crate 内，平台无关）：

- [../gpui/src/keymap/binding.rs:L48-L75](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/keymap/binding.rs#L48-L75) —— `KeyBinding::load`：绑定表加载时逐段 `Keystroke::parse` + `KeybindingKeystroke::new_with_mapper`（L60-L64），mapper 与 `use_key_equivalents` 开关从上层传入。`KeyBinding::new`（L33-L45）则用 `DummyKeyboardMapper` 兜底。
- [../gpui/src/window.rs:L5292-L5313](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/window.rs#L5292-L5313) —— 事件到达 `Window` 后，先过 keystroke 拦截器，再调 `dispatch_tree.dispatch_key` 用当前 `Keystroke` 对绑定表做多键序列匹配（`pending` 是「等待后续键」的半匹配状态）。
- [../gpui/src/window.rs:L5373-L5403](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/window.rs#L5373-L5403) —— 匹配结果里有绑定则逐个 `dispatch_action_on_node` 分发动作；`prefer_character_input` 为真且焦点处接受文本输入时可跳过绑定（L5373-L5387），把按键让给文字输入——这是 IME 场景的关键让路逻辑。
- [../gpui/src/app.rs:L2986-L2997](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/app.rs#L2986-L2997) —— `KeystrokeEvent`：观察者拿到的封装（keystroke + 命中的 action + 上下文栈），无论是否命中绑定都会通知（window.rs L5426 的 `dispatch_keystroke_observers`）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用数据流验证 `key` 与 `key_char` 的分工，纯读代码即可完成。
2. **操作步骤**：
   - 读 `Keystroke` 的三个字段文档（keystroke.rs L17-L33），把 `option-s`（macOS 美式布局）、`s`、`cmd-s` 三种按键的 `{key, key_char}` 预测值填进下表；
   - 再读 `should_match`（L74-L113），回答：绑定表里写着 `cmd-$`，用户在捷克键盘上按 `cmd-alt-ç`（打出 `$` 需要的修饰组合）能否命中？
3. **需要观察的现象**：你的预测与代码逻辑是否一致；宽匹配分支（L84-L98）里 `ime_modifiers` 只保留了 control 和 platform 两个修饰位——想想为什么把 alt/shift 排除。
4. **预期结果**：`option-s` → `{key: "s", key_char: Some("ß")}`；`s` → `{key: "s", key_char: Some("s")}`；`cmd-s` → `{key: "s", key_char: None}`。捷克键盘的 `cmd-$` 能命中（走 key_char 宽匹配）。alt/shift 被排除是因为在这类布局里 alt+shift 正是「打出该字符所需的组合」本身，不能再算进快捷键修饰。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Keystroke` 不直接存 `String` 类型的修饰键列表，而要用五个布尔位的 `Modifiers` 结构体？

**参考答案**：修饰键需要参与集合运算与相等比较。`Modifiers` 实现了 `BitOr/BitAnd/BitXor`（keystroke.rs L608-L661）与 `is_subset_of`（L602-L605），匹配逻辑（如 `should_match` 的宽匹配）直接用位运算组合出「忽略 shift/alt 的修饰键视图」；结构体字段还让 `platform` 键的命名差异（cmd/win/super）在类型层面被抹平。

**练习 2**：`KeybindingKeystroke` 与 `Keystroke` 为什么是两个类型而不是一个？

**参考答案**：它们的来源与职责不同：`Keystroke` 是运行时事件（平台产生，可能带 `key_char`、可能处于 IME 中间态）；`KeybindingKeystroke` 是静态声明（从绑定字符串解析，需要经平台 mapper 矫正显示，Windows 上还要维护「实际按键」与「展示按键」两套字段）。分开后，匹配算法 `should_match(&self, target: &KeybindingKeystroke)` 的方向性一目了然，Windows 的 display 字段也不会污染其他平台。

### 4.2 PlatformKeyboardLayout 与 PlatformKeyboardMapper：两个小契约

#### 4.2.1 概念说明

整个键盘契约层惊人地小——两个 trait 加起来不到 25 行。它们回答两个问题：

- `PlatformKeyboardLayout`：「**现在是什么键盘布局**」——给上层一个布局身份（id 与显示名）。用途：诊断信息、按布局定制行为、检测布局切换。
- `PlatformKeyboardMapper`：「**绑定表里的按键名，在当前布局下应该改写成什么**」——只在加载键位绑定时使用，核心是 macOS 的 key equivalents（见 4.4）；没有此需求的平台用 `DummyKeyboardMapper` 原样透传。

它们通过 `Platform` trait 的三个方法暴露，注意这三个方法**都没有默认实现**，是平台实现的硬性作业（回顾 u2-l1 的「默认实现三种姿态」分类——这三个属于无默认体的一类）。

#### 4.2.2 核心流程

```text
App::new_app(platform)
    ├─ platform.keyboard_layout()   → 存入 App.keyboard_layout（布局身份快照）
    ├─ platform.keyboard_mapper()   → 存入 App.keyboard_mapper（绑定加载时的改写器）
    └─ platform.on_keyboard_layout_change(cb) → 注册布局变化回调

布局变化时（平台检测到）：
    cb() → App 重新拉取 keyboard_layout + keyboard_mapper
         → 通知 keyboard_layout_observers（cx.on_keyboard_layout_change 的订阅者）
```

#### 4.2.3 源码精读

契约本体（全文仅 42 行的文件）：

- [../gpui/src/platform/keyboard.rs:L5-L11](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keyboard.rs#L5-L11) —— `PlatformKeyboardLayout`：`id()` 要求布局内唯一，`name()` 是给用户看的名字。
- [../gpui/src/platform/keyboard.rs:L13-L24](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keyboard.rs#L13-L24) —— `PlatformKeyboardMapper`：`map_key_equivalent(keystroke, use_key_equivalents)` 吃一个 `Keystroke` 吐一个 `KeybindingKeystroke`；`get_key_equivalents` 文档直说「only used on macOS」。
- [../gpui/src/platform/keyboard.rs:L26-L41](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform/keyboard.rs#L26-L41) —— `DummyKeyboardMapper`：无映射需求平台的默认答案，`map_key_equivalent` 直接 `from_keystroke` 透传、`get_key_equivalents` 永远 `None`。

挂载点与消费端：

- [../gpui/src/platform.rs:L338-L340](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/platform.rs#L338-L340) —— `Platform` trait 的三个键盘方法，均以 `;` 结尾（必需方法）。
- [../gpui/src/app.rs:L796-L797](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/app.rs#L796-L797) —— `App::new_app` 启动时抽取两样东西存入应用状态（与执行器、文本系统同批，回顾 u1-l2 的「平台注入」）。
- [../gpui/src/app.rs:L880-L892](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/app.rs#L880-L892) —— 布局变化回调：重新拉取 layout 与 mapper，再逐个通知观察者。用 `Rc::downgrade` 持有 App，避免回调闭包形成引用环。
- [../gpui/src/app.rs:L987-L1011](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui/src/app.rs#L987-L1011) —— 对应用的公开 API：`keyboard_layout()`、`keyboard_mapper()`、`on_keyboard_layout_change()`（返回 `Subscription`，须保存否则立即注销——u2-l2 讲过 Subscription 语义）。

四平台第一印象对比（细节在后三节展开）：

| 平台 | `keyboard_layout()` 数据来源 | `keyboard_mapper()` |
| --- | --- | --- |
| Linux | XKB keymap 的 layout 名（Wayland/X11 各有获取途径） | 每次 `Rc::new(DummyKeyboardMapper)`（[platform.rs:L252-L254](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L252-L254)） |
| macOS | Carbon TIS 输入源查询 | 缓存在平台状态里的 `MacKeyboardMapper`（含 key equivalents 表） |
| Windows | `GetKeyboardLayoutNameW` + 注册表布局名 | `WindowsKeyboardMapper`（VKEY 映射表） |
| Web | 硬编码 `"us"` / `"US"` | trait 默认链之外的 Dummy（无自定义） |

#### 4.2.4 代码实践（阅读型 + 观察型）

1. **实践目标**：确认你所用平台上「布局身份」和「mapper 类型」分别是什么，并找到它们被创建的那行代码。
2. **操作步骤**：
   - 在四个平台 crate 中 grep `fn keyboard_layout` 与 `fn keyboard_mapper`，各找到一处实现；
   - Linux 用户额外看 [../gpui_linux/src/linux/keyboard.rs:L3-L22](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/keyboard.rs#L3-L22)：`LinuxKeyboardLayout` 里 `id()` 与 `name()` 返回**同一个字符串**——它只是 XKB layout 名的搬运工；
   - 运行第 5 节综合实践的程序，窗口里显示的布局名应与你系统设置一致（Linux 上 `setxkbmap -query` 的 `layout:` 字段）。
3. **需要观察的现象**：切换布局（Linux：`setxkbmap de` 再 `setxkbmap us`；macOS：输入法菜单或系统设置）后，`on_keyboard_layout_change` 订阅的回调是否触发、布局名是否变化。
4. **预期结果**：Linux 上布局名来自 XKB（如 `German`、`English (US)` 之类的 layout 名），切换后回调触发；macOS 上 id 形如 `com.apple.keylayout.ABC`（见 4.4 的 TIS 查询）。**待本地验证**（本讲义写作环境无图形会话）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `keyboard_layout()` 每次调用都返回新建的 `Box<dyn PlatformKeyboardLayout>`，而 `keyboard_mapper()` 返回 `Rc` 共享实例？

**参考答案**：布局是一个可查询的瞬时快照（macOS 实现每次现场调 TIS），值语义、随用随取；mapper 可能携带大状态（macOS 的 key equivalents 表、Windows 的 VKEY 表），且要保证「同一次加载的多条绑定用同一份映射」，用 `Rc` 共享同一实例既省内存又保证一致性。

**练习 2**：`on_keyboard_layout_change` 注册的回调触发后，为什么 `App` 要**重新**调用 `keyboard_mapper()`，而不是沿用旧的？

**参考答案**：mapper 是按布局构造的（macOS 的 `MacKeyboardMapper::new(layout_id)` 依赖布局 id 选择 key equivalents 表）。布局变了，映射表必须跟着换，否则绑定加载会用旧布局的改写规则——这正是 app.rs L885-L886 两行重新赋值的原因。

### 4.3 Linux：xkbcommon 的 State / Keycode / Keysym 三件套

#### 4.3.1 概念说明

Linux 上 GPUI 不自己解析键盘布局，而是复用整个桌面生态的标准库 **xkbcommon**。三个核心概念：

- **`xkb::Context`**：库上下文（持有 locale、日志等环境），一切的前提。
- **`xkb::Keymap`**：完整布局描述——每个 keycode 在什么修饰组合下产生什么 keysym/字符。**Wayland 与 X11 获取 keymap 的方式完全不同**：Wayland 由合成器把序列化的 keymap 文本通过 memfd 发给你；X11 则是你自己去 X 服务器的 XKB 扩展上拉。
- **`xkb::State`**：keymap 之上的**会话状态**——当前按着哪些修饰键、处于哪个 layout group、compose 组合进度。它是增量更新的：每来一个修饰键事件调 `update_mask`，查询时用 `key_get_one_sym`/`key_get_utf32` 等方法。

以及两个原始标识：**`Keycode`**（evdev 键码，xkb 世界要在 evdev 值上加 8：\( \text{keycode} = \text{evdev code} + 8 \)，因为 xkb 的最小键码是 8）与 **`Keysym`**（键符号，如 `XK_a`、`XK_Return`、`XK_dead_acute`）。

#### 4.3.2 核心流程

以 Wayland 一次按键为例（X11 流程相同，仅事件来源不同）：

```text
wl_keyboard.keymap 事件（fd + size）
    → new_xkb_context() + Keymap::new_from_fd() + State::new()   [初始化一次]
wl_keyboard.modifiers 事件
    → state.update_mask(...) → modifiers_from_xkb / capslock_from_xkb
    → group 变化？ → handle_keyboard_layout_change → 布局回调
wl_keyboard.key 事件（key = evdev 键码）
    → keycode = key + 8；keysym = state.key_get_one_sym(keycode)
    → keysym.is_modifier_key()? 纯修饰键不产生 Keystroke
    → keystroke_from_xkb(state, modifiers, keycode)   [核心翻译]
    → compose 状态机修正 key_char（死键/组合输入）
    → PlatformInput::KeyDown(KeyDownEvent { keystroke, .. }) → 窗口
```

#### 4.3.3 源码精读

**翻译核心 `keystroke_from_xkb`**（Linux 键盘处理的真正心脏）：

- [../gpui_linux/src/linux/platform.rs:L1016-L1139](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L1016-L1139) —— 函数签名与三次查询：`key_get_utf32`（字符码点）、`key_get_utf8`（字符文本）、`key_get_one_sym`（键符号），随后进入巨型 `match key_sym`。
- 命名键翻译（L1026-L1089）：`Return→"enter"`、`Prior→"pageup"`、`XF86_Back→"back"`、`space→"space"`……把 XKB 的符号名归一成 GPUI 的键名（与 4.5 的 Web 映射表、4.4 的 macOS 函数键表对照着看，三张表干的是同一件事）。
- 兜底分支的四级回退（[L1091-L1119](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L1091-L1119)）：①keysym 名小写、小键盘剥 `kp_` 前缀；②若 utf8 是单个 ASCII 可见字符，直接小写用作 key；③**ctrl 组合的妙手**：`ctrl-a` 时 XKB 会给出控制字符（码点 ≤ 0x1f），按 \( c + 0x40 \) 反算回字母（L1104-L1109），并特意排除 `ctrl-[` 这类不想被映射的场景；④非 ASCII 布局（如泰语）回退到 `guess_ascii(keycode, shift)` 用键码猜 ASCII 等价键。
- 两个收尾约定（[L1121-L1132](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L1121-L1132)）：shift 只对「有大小写之分的单字符键」保留（数字/符号键丢弃 shift，否则 `ctrl-shift-1` 会变成 `ctrl-shift-!` 而匹配不上）；`key_char` 过滤控制字符与 DEL（L1130-L1132）。

**修饰键与布局名**：

- [../gpui_linux/src/linux/platform.rs:L1200-L1213](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L1200-L1213) —— `modifiers_from_xkb`：用 XKB 的**修饰键名**（`MOD_NAME_SHIFT`/`ALT`/`CTRL`/`LOGO`）查询有效状态；注意 Linux 的 `platform` 位来自 `LOGO`（Super 键），`function` 恒为 false。紧随其后的 [capslock_from_xkb（L1215-L1219）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L1215-L1219) 单独跟踪大小写锁定。
- [../gpui_linux/src/linux/platform.rs:L840-L852](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L840-L852) —— `new_xkb_context` 与 `validate_xkb_context`：连裸指针判空都要做——xkbcommon 是 C 库绑定，创建失败只给空指针。
- [../gpui_linux/src/linux/platform.rs:L1145-L1199](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L1145-L1199) —— `keystroke_underlying_dead_key`：死键到「它代表的符号」的查表（`dead_acute→´`…），供 compose 状态机显示预编辑文本。

**Wayland 调用方**（keymap 从天而降）：

- [../gpui_linux/src/linux/wayland/client.rs:L1702-L1736](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1702-L1736) —— `wl_keyboard::Event::Keymap`：合成器发来 fd + size，客户端 `Keymap::new_from_fd` 编译（只接受 `XkbV1` 格式），建 `State`、建 compose 表（按 `LC_CTYPE` locale），随后立刻做一次布局上报。
- [../gpui_linux/src/linux/wayland/client.rs:L543-L570](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L543-L570) —— `handle_keyboard_layout_change`：`serialize_layout(STATE_LAYOUT_EFFECTIVE)` 拿当前生效 layout 的下标，`keymap.layout_get_name(idx)` 取名，与缓存不同则更新并触发 `common.callbacks.keyboard_layout_change`（即 4.2 里 App 注册的回调）。
- [../gpui_linux/src/linux/wayland/client.rs:L1764-L1793](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1764-L1793) —— `Modifiers` 事件：`update_mask` 增量更新 State，派生 `Modifiers`/`Capslock`，向窗口发 `PlatformInput::ModifiersChanged`；末尾 `group != old_layout` 时检测布局切换——**切换布局在 Wayland 上就是一次 group 变化的 modifiers 事件**。
- [../gpui_linux/src/linux/wayland/client.rs:L1795-L1817](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1795-L1817) —— `Key` 事件：`Keycode::from(key + MIN_KEYCODE)`（+8 换算），`key_get_one_sym` 后先滤掉纯修饰键，再进 `keystroke_from_xkb`。
- [../gpui_linux/src/linux/wayland/client.rs:L1818-L1902](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1818-L1902) —— compose 状态机（`Composing` 时清空 `key_char` 并上报预编辑文本；`Composed` 时用组合结果覆盖 `key_char` 与 `key`）；最终包成 `PlatformInput::KeyDown` 分发，并用 calloop `Timer` 实现按键重复（`is_held: true`）。

**X11 调用方**（keymap 自己去拉）：

- [../gpui_linux/src/linux/x11/client.rs:L394-L441](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L394-L441) —— 初始化：先 `xkb_use_extension` 协商版本，订阅 `STATE_NOTIFY | MAP_NOTIFY | NEW_KEYBOARD_NOTIFY` 三类事件，再 `keymap_new_from_device`/`state_new_from_device` 从 X 服务器**直接拉取** keymap 与状态，最后读出初始布局名。
- [../gpui_linux/src/linux/x11/client.rs:L995-L1013](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L995-L1013) —— 收到 `XkbNewKeyboardNotify`/`XkbMapNotify`（用户换了键盘或布局）时整体重建 keymap+State——X11 上换布局意味着服务器侧 keymap 变了。
- [../gpui_linux/src/linux/x11/client.rs:L1053-L1116](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1053-L1116) —— `KeyPress`：与 Wayland 同构——`keystroke_from_xkb` + compose + `PlatformInput::KeyDown`。区别仅在于修饰键状态直接来自 X 事件位掩码（`modifiers_from_state`）而非独立 modifiers 事件。

#### 4.3.4 代码实践（观察型实验）

1. **实践目标**：亲手制造一次「布局差异」，并定位产生差异的函数。
2. **操作步骤**（X11 或 Wayland 会话均可）：
   - 运行第 5 节的按键打印程序，按 `y` 键，记下输出；
   - 执行 `setxkbmap de` 切到德语 QWERTZ，再按**同一颗物理键**（现在键帽语义是 `z`），记下输出；
   - 执行 `setxkbmap us` 切回，重复对比；有死键的布局（如 `setxkbmap us -variant intl`）下按 `'` 再按 `e`，观察 compose 路径。
3. **需要观察的现象**：德语布局下同一颗键 `key` 变为 `z`、`key_char` 变为 `z`；intl 变体下按 `'` 的第一次按键 `key_char` 为 `None`（`is_ime_in_progress` 为真），第二次按 `e` 才产出 `é`；切换布局瞬间应看到布局变化回调打印。
4. **预期结果**：`key` 的差异由 [keystroke_from_xkb 的兜底分支](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/platform.rs#L1091-L1119) 产生（keysym 不同→分支不同）；`key_char` 的 `None` 由 [Wayland compose 的 Composing 分支](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L1821-L1830) 显式置空；布局回调由 [handle_keyboard_layout_change](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/wayland/client.rs#L543-L570) 或 [X11 的 XkbStateNotify 分支](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_linux/src/linux/x11/client.rs#L1049-L1051) 触发。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 evdev 键码要加 8 才是 XKB keycode？

**参考答案**：X11/XKB 历史上保留 0-7 作为「虚拟键码」区间（如系统的 Mode_switch 类特殊键），真实键码从 8 起编号；Linux 内核 evdev 不带这个偏移，所以客户端换算 `keycode = evdev + MIN_KEYCODE`（wayland/client.rs L1811 的 `Keycode::from(key + MIN_KEYCODE)`）。

**练习 2**：Wayland 与 X11 在「布局切换」的呈现方式上有何本质不同？

**参考答案**：Wayland 下布局是 keymap 里多个 layout group 之一，切换只是 `Modifiers` 事件里 `group` 字段变化（client.rs L1791-L1793 检测）；X11 下切布局通常意味着服务器整个 keymap 被替换，客户端会收到 `XkbNewKeyboardNotify`/`XkbMapNotify` 并整体重建 State（client.rs L995-L1013）。前者是轻量状态切换，后者是数据结构替换。

**练习 3**：`ctrl-a` 在 XKB 查询里 utf32 是控制字符 0x01，`keystroke_from_xkb` 如何把它变成 `"a"`？

**参考答案**：兜底分支检测 `key_utf32 <= 0x1f` 且 keysym 名不是数字开头时，用 \( 0x01 + 0x40 = 0x41 = \text{'A'} \) 反算再小写成 `"a"`（platform.rs L1104-L1109），这样 `ctrl-a` 的绑定匹配不会被控制字符破坏。

### 4.4 macOS：Carbon/HIToolbox 查布局 + AppKit 事件造 Keystroke

#### 4.4.1 概念说明

macOS 用两套古老 API 组合完成任务：

- **布局身份**：Carbon 时代的 Text Input Sources（TIS，HIToolbox 框架）——`TISCopyCurrentKeyboardLayoutInputSource` 拿当前输入源，`TISGetInputSourceProperty` 读它的 id 与本地化名。输入源（input source）比「布局」更广：它还涵盖输入法。
- **事件翻译**：AppKit 的 `NSEvent` 已经带 `charactersIgnoringModifiers`（忽略修饰键的字符），但 GPUI 仍要**反向查询布局**（`chars_for_modified_key`，底层是 `UCKeyTranslate`）来处理一批著名怪案：Dvorak+QWERTY 这类「按 cmd 时切回 QWERTY」的布局、乌克兰语 cmd-shift-s 只报 cmd-s、捷克数字行有 shift 字符等。

第三个 macOS 专属难题催生了 `MacKeyboardMapper` 的 key equivalents 大表：德语等布局不打 option 打不出全部 ASCII，macOS 的做法是把菜单快捷键**挪到够得着的键上**（如 `cmd-[` 挪到 `cmd-ö`）。系统没有公开查询接口，Zed 团队的办法是——真的渲染一遍菜单、逐个检查 UI 显示了什么（代码注释里记录了全过程）。

#### 4.4.2 核心流程

```text
NSEvent (NSKeyDown)
    → parse_keystroke(event)
        ├─ charactersIgnoringModifiers / modifierFlags
        ├─ 命名键（函数键 Unicode 区）→ "f1".."f35"/方向键等
        └─ 默认分支：chars_for_modified_key(keyCode, NO_MOD/SHIFT/CMD)
              （TIS + UCKeyTranslate 反查布局，处理 cmd 布局特例）
    → PlatformInput::KeyDown(KeyDownEvent { keystroke, is_held: isARepeat })

绑定加载（独立路径）：
    MacKeyboardLayout::new() → TIS 查布局 id
    MacKeyboardMapper::new(layout_id) → key equivalents 表
    KeyBinding::load 时 map_key_equivalent 改写单字符键名
```

#### 4.4.3 源码精读

- [../gpui_macos/src/events.rs:L120-L138](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/events.rs#L120-L138) —— `NSEvent` 类型分发：`NSFlagsChanged`（纯修饰键变化，对应 `ModifiersChanged`）、`NSKeyDown`/`NSKeyUp` 调 `parse_keystroke`；`is_held` 直接取 `isARepeat`。
- [../gpui_macos/src/events.rs:L338-L356](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/events.rs#L338-L356) —— `parse_keystroke` 开头：`charactersIgnoringModifiers` 与 `modifierFlags` 是原料；macOS 的 `function` 修饰位还要排除方向键等功能键区（L354-L356）。
- [../gpui_macos/src/events.rs:L359-L420](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/events.rs#L359-L420) —— 命名键表：用 NSEvent 字符串里的**函数键 Unicode 码点**（`NSUpArrowFunctionKey` 等）识别 `up`/`f1`…`f35`；`space`/`tab`/`enter` 顺手填好 `key_char`。
- [../gpui_macos/src/events.rs:L421-L482](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/events.rs#L421-L482) —— 全文件最精彩的默认分支：注释列出了 7 个必须人工验证的布局怪案（Armenian、Dvorak+QWERTY、Ukrainian、Czech、Norwegian、Russian、German QWERTZ，L422-L431）；`chars_for_modified_key(keyCode, ...)` 按 `NO_MOD`/`SHIFT_MOD`/`CMD_MOD` 组合反查布局（L433-L456）；无 ctrl/cmd/fn 时才计算 `key_char`（L458-L468）；最后按「shift 只留给 ASCII 小写字母」的约定取舍（L470-L481）——与 Linux 侧 L1121-L1128 的约定遥相呼应。
- [../gpui_macos/src/events.rs:L512-L535](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/events.rs#L512-L535) —— `chars_for_modified_key`：TIS 拿 `kTISPropertyUnicodeKeyLayoutData`，交 `UCKeyTranslate`（Carbon 的 Unicode 键翻译 API）按修饰组合反查——这就是「Carbon/HIToolbox 查布局」的落点。
- [../gpui_macos/src/keyboard.rs:L53-L77](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/keyboard.rs#L53-L77) —— `MacKeyboardLayout::new`：TIS 查 `kTISPropertyInputSourceID`（形如 `com.apple.keylayout.German`）与 `kTISPropertyLocalizedName`。
- [../gpui_macos/src/keyboard.rs:L32-L51](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/keyboard.rs#L32-L51) —— `MacKeyboardMapper::map_key_equivalent`：`use_key_equivalents` 为真且键是单字符时，查表改写 `keystroke.key`（`cmd-[` 的 `[` 被改成 `ö`）。
- [../gpui_macos/src/keyboard.rs:L87-L110](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/keyboard.rs#L87-L110) —— key equivalents 存在原因的完整注释（含 Apple 文档链接与菜单渲染抓取法）；表本体从 L111 延伸到 L1502，覆盖 AZERTY、QWERTZ、北欧、东欧等几十种布局，未知布局返回 `None`（[L1498-L1502](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/keyboard.rs#L1498-L1502)）。
- [../gpui_macos/src/platform.rs:L213-L214](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/platform.rs#L213-L214) 与 [L1355-L1369](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/platform.rs#L1355-L1369) —— mapper 在平台构造时按初始布局创建并缓存；Objective-C 委托方法 `onKeyboardLayoutChange:` 触发时**重建** layout 与 mapper 再回调——布局变化在 macOS 上是显式系统通知，而非像 Linux 那样从事件流里推断。

顺带一笔 Windows（本讲不展开，但表格完整性需要）：[../gpui_windows/src/keyboard.rs:L38-L91](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_windows/src/keyboard.rs#L38-L91) 的 `WindowsKeyboardMapper` 维护 `key↔VKEY↔shifted` 三张哈希表，`map_key_equivalent` 把 `ctrl-@` 这类绑定改写成「实际要按的 `ctrl-shift-2`」，并用 `KeybindingKeystroke::new` 同时设置 `inner`（匹配用）与 display 字段（显示用）——4.1 里 `KeybindingKeystroke` 的 Windows 分支就服务于此。布局身份来自 [GetKeyboardLayoutNameW + 注册表（L93-L103）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_windows/src/keyboard.rs#L93-L103)。

#### 4.4.4 代码实践（阅读型）

1. **实践目标**：把「布局怪案」从注释变成可复述的知识。
2. **操作步骤**：只读 [events.rs L422-L431](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_macos/src/events.rs#L422-L431) 的注释表格，为每种布局写一行「症状 → 代码对策」；再对照 L440-L456 找到对策所在分支。
3. **需要观察的现象**：`cmd` 按下时为什么用 `CMD_MOD` 反查而不是直接用事件字符？（提示：macOS 有一类「按 cmd 切回 QWERTY」的布局，事件自带的 `charactersIgnoringModifiers` 在 cmd 下已经是另一套布局的字符了。）
4. **预期结果**：例如 Ukrainian 行：症状「cmd-shift-s 系统只报 cmd-s」→ 对策 L452-L454（cmd 字符有大写形态时手动转大写补回 shift）；Russian 行：症状「7 键 shift 是 `.`」→ 对策 L440-L455（cmd 下改用 cmd 布局的字符）。完成下表即视为达成目标。

#### 4.4.5 小练习与答案

**练习 1**：Linux 的 `keystroke_from_xkb` 每次按键都要查一次 State，macOS 的 `parse_keystroke` 却要多次调 `chars_for_modified_key` 反查布局。两种风格的根源差异是什么？

**参考答案**：xkbcommon 的 `State` 是客户端持有的**会话状态**，布局翻译是本地查表（O(1) 内存操作）；macOS 没有把布局数据交给客户端，`NSEvent` 只给了部分翻译结果，客户端要拿到「同一键在其他修饰组合下的字符」只能每次穿过 TIS/UCKeyTranslate 系统调用反查。前者是「数据在我手里」，后者是「数据在系统手里、按次询问」。

**练习 2**：`MacKeyboardMapper::get_key_equivalents` 返回的表除了绑定加载，还有什么消费方？为什么 Linux 不需要等价物？

**参考答案**：Zed 的命令面板/UI 要把绑定表**显示**给用户（如 crates/zed/src/zed.rs 与 keymap 编辑器会拉取该表做方向改写）。Linux 桌面没有「菜单快捷键自动挪位」的系统级约定——XKB 布局里 ASCII 打不出来就是打不出来，用户自行配置，因此 `DummyKeyboardMapper` 足矣。

### 4.5 Web：浏览器已经替你做完布局翻译

#### 4.5.1 概念说明

浏览器把键盘抽象成了另一副模样：`KeyboardEvent.key` **已经是按当前布局翻译后的字符**（按德语布局的 `y` 键，`key` 就是 `"z"`），`KeyboardEvent.code` 才是物理键位（恒 `KeyY`）。GPUI 的 Web 实现因此**不需要任何 keymap**：没有 XKB、没有 TIS，`WebKeyboardLayout` 直接硬编码 `"us"`——浏览器不暴露真实布局，而且事件本身已经布局无关化了。

代价是失去精细控制：死键、AltGr、IME（`isComposing`）都要靠浏览器语义辨认，而不是自己驱动状态机。

#### 4.5.2 核心流程

```text
DOM keydown 事件
    → modifiers_from_keyboard_event（ctrl/alt/shift/meta → Modifiers）
    → dom_key_to_gpui_key（DOM key 名 → GPUI 键名）
    → 纯修饰键？丢弃
    → compute_key_char（推导 key_char）
    → Keystroke { modifiers, key, key_char }（内联构造，无翻译层）
    → dispatch_input(PlatformInput::KeyDown(...))
    → 未被拦截且会产生文本时：直接写入 input handler
```

#### 4.5.3 源码精读

- [../gpui_web/src/keyboard.rs:L3-L13](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/keyboard.rs#L3-L13) —— `WebKeyboardLayout` 全文：`id() = "us"`、`name() = "US"`，诚实的占位实现。
- [../gpui_web/src/events.rs:L629-L663](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs#L629-L663) —— `dom_key_to_gpui_key`：DOM 键名到 GPUI 键名的映射表（`"Enter"→"enter"`、`"ArrowLeft"→"left"`、`"Meta"→"platform"`），F 键用 `strip_prefix('F')` 解析数字（L652-L659），其余统一小写——与 Linux 的 Keysym 表、macOS 的函数键表对照，这是同一张归一化表的第三个版本。
- [../gpui_web/src/events.rs:L676-L684](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs#L676-L684) —— 修饰键直接读事件的四个布尔位，`meta_key` 映射到 `platform`；[capslock（L707-L711）](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs#L707-L711) 用 `get_modifier_state("CapsLock")`。
- [../gpui_web/src/events.rs:L366-L400](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs#L366-L400) —— `register_key_down` 主体：修饰键变化先发 `ModifiersChanged`，再归一化键名、滤纯修饰键（[is_modifier_only_key，L731-L736](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs#L731-L736)）、`compute_key_char` 算 `key_char`，`Keystroke` 三行内联构造（L390-L394）后立即分发。
- [../gpui_web/src/events.rs:L409-L425](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs#L409-L425) —— 两个浏览器特有善后：`is_composing`（IME 组合中）直接吞掉事件；`keystroke_inserts_text` 判定为「会产生文本」的按键直接写入 input handler 并 `prevent_default`，阻止浏览器默认行为（空格滚屏、快速查找等）。
- [../gpui_web/src/events.rs:L744-L750](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui_platform/../gpui_web/src/events.rs#L744-L750) —— `keystroke_inserts_text` 的 AltGr 陷阱：非 macOS 下浏览器把 AltGr 报成 `ctrl+alt`，所以「ctrl+alt」组合反而**要**产生文本——不认识这一点的代码会把欧洲用户的 AltGr 当快捷键吞掉。

#### 4.5.4 代码实践（观察型）

1. **实践目标**：验证「浏览器已完成布局翻译」这句话。
2. **操作步骤**：在浏览器控制台执行
   ```js
   addEventListener('keydown', e => console.log(e.key, e.code));
   ```
   切换系统输入法/布局后按同一颗物理键，对比 `key`（随布局变）与 `code`（不变）。
3. **需要观察的现象**：`key` 与 `code` 的分离恰好对应 `Keystroke.key_char` 与 `Keystroke.key` 的分工——但注意浏览器没有「键帽 ASCII 等价物」的概念，非 ASCII 布局下 `dom_key_to_gpui_key` 只能把本地化字符小写后直接当 `key`，此时靠 `should_match` 的 `key_char` 宽匹配兜底（4.1）。
4. **预期结果**：例如德语布局按 `y` 键位：`key: "z", code: "KeyY"`。此实验纯浏览器环境即可完成，无需构建 wasm。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `WebKeyboardLayout` 敢硬编码 `"us"` 而不给真实布局？

**参考答案**：浏览器既不暴露键盘布局 API，又已经在 `event.key` 里按真实布局翻译好了字符——Web 平台上客户端布局信息既**拿不到**也**不需要**。布局字段的用途（诊断、key equivalents）在浏览器场景里要么失效要么由 `should_match` 的宽匹配替代。

**练习 2**：Linux 用 xkb compose 状态机处理死键，Web 实现怎么处理？

**参考答案**：交给浏览器：死键组合进行中浏览器的 `keydown` 会带 `is_composing = true`（或 GPUI 自己维护的 `is_composing` 标志），events.rs L409-L412 直接 `prevent_default` 吞掉；组合完成的字符经 `compute_key_char` 出现在 `key_char` 里。也就是「状态机在浏览器里，GPUI 只认结果」。

## 5. 综合实践：键盘事件打印工具

把本讲内容串成一个可运行的小工具：打印每个按键的 `Keystroke` 关键字段、显示当前布局、监听布局切换，然后做 4.3.4 的布局对比实验。

**以下为示例代码（非项目原有代码）**，在 zed 仓库内新建一个独立 crate（如 `crates/key-logger`），或放进你自己的练习 workspace：

```toml
# Cargo.toml —— 若放在 zed 仓库内，可复用 workspace 依赖
[dependencies]
gpui = { path = "../gpui" }
gpui_platform = { path = "../gpui_platform", features = ["font-kit"] }

[package]
edition.workspace = true
```

```rust
use gpui::{
    Bounds, Context, IntoElement, ParentElement, Render, Styled, Window, WindowBounds,
    WindowOptions, div, px, size,
};
use gpui_platform::application;

struct KeyLogger {
    layout: String,
    // Subscription 一旦 Drop 即注销，必须存进实体保活（u2-l2 的 Subscription 语义）
    _subscriptions: Vec<gpui::Subscription>,
}

impl Render for KeyLogger {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .size_full()
            .p_4()
            .child(format!("layout: {}", self.layout))
            .child("看终端输出：每次按键打印一行 Keystroke")
    }
}

fn main() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(500.), px(300.)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |_window, cx| {
                let layout = format!(
                    "{} ({})",
                    cx.keyboard_layout().name(),
                    cx.keyboard_layout().id()
                );
                cx.new(|cx| {
                    // 每次按键（无论是否命中绑定）都会通知（app.rs observe_keystrokes）
                    let keystroke_sub = cx.observe_keystrokes(|event, _window, _cx| {
                        println!(
                            "key={:?} key_char={:?} modifiers={{ctrl:{},alt:{},shift:{},platform:{},fn:{}}} action_hit={}",
                            event.keystroke.key,
                            event.keystroke.key_char,
                            event.keystroke.modifiers.control,
                            event.keystroke.modifiers.alt,
                            event.keystroke.modifiers.shift,
                            event.keystroke.modifiers.platform,
                            event.keystroke.modifiers.function,
                            event.action.is_some(),
                        );
                    });
                    let layout_sub = cx.on_keyboard_layout_change(|cx| {
                        println!(
                            "layout changed -> {} ({})",
                            cx.keyboard_layout().name(),
                            cx.keyboard_layout().id()
                        );
                    });
                    KeyLogger {
                        layout: layout.clone(),
                        _subscriptions: vec![keystroke_sub, layout_sub],
                    }
                })
            },
        )
        .unwrap();
        cx.activate(true);
    });
}
```

操作步骤：

1. 构建运行（Linux 需要 Wayland/X11 会话；`cargo run -p key-logger`）。
2. 依次按 `a`、`ctrl-a`、`cmd/super-a`、`f1`、回车，记录每行的 `key` / `key_char` / 修饰位。
3. 切换布局（Linux：`setxkbmap de`；macOS：输入法菜单）后按同一颗物理键，对比输出；观察 `layout changed` 是否打印。
4. **定位差异源**：按 4.3.4 / 4.2.4 的结论，指出 `key` 变化由哪个函数产生（Linux：`keystroke_from_xkb`；macOS：`parse_keystroke`；Web：`dom_key_to_gpui_key` + 浏览器自身的布局翻译），布局回调由哪个函数触发（Linux：`handle_keyboard_layout_change`；macOS：`onKeyboardLayoutChange:` 委托）。
5. 无图形环境时的替代路径：把 `application()` 换成 `headless()`（u1-l2 讲过），配合 `cx.dispatch_keystroke(...)`（window.rs L4967-L4970，把合成 `PlatformInput::KeyDown` 派发进窗口）在 headless 下驱动观察者——但注意 headless 平台不会产生真实布局事件，`Keystroke::parse(...).with_simulated_ime()`（keystroke.rs L240-L263）可用于补出 `key_char`。

预期结果与观察要点：`f1` 的 `key_char` 为 `None`（非打印键）；`ctrl-a` 的 `key` 仍是 `"a"`（Linux 侧 +0x40 反算的成果）；德语布局下同一颗键 `key`/`key_char` 均随布局改变；macOS 上切到德语后，`on_keyboard_layout_change` 触发且新 mapper 生效。**待本地验证**（本讲义写作环境无图形会话，未实际运行）。

## 6. 本讲小结

- `Keystroke` 三字段是跨布局键盘抽象的基石：`key` 取键帽的 ASCII 等价语义（匹配绑定用），`key_char` 是实际将输入的字符（文本输入用），`Modifiers.platform` 抹平 cmd/win/super 的命名差异；`should_match` 的 key_char 宽匹配是非美式布局下快捷键仍可命中的关键。
- 契约层极小：`PlatformKeyboardLayout`（布局身份）+ `PlatformKeyboardMapper`（绑定加载时的键名改写），经 `Platform` 的三个必需方法暴露；`App` 启动时抽取、布局变化时刷新并广播给订阅者。
- Linux 复用 xkbcommon：Wayland 收合成器推来的 keymap fd、X11 从 XKB 扩展拉取，两端共用 `keystroke_from_xkb`（含 ctrl 控制字符 +0x40 反算、非 ASCII 布局的 `guess_ascii` 兜底、shift 只留给字母的约定）与 `modifiers_from_xkb`；compose/死键由 xkb compose 状态机驱动并显式置空 `key_char`。
- macOS 是「数据在系统手里」的风格：布局身份用 Carbon TIS 查询，按键翻译靠 `parse_keystroke` 多次经 `UCKeyTranslate` 反查布局以对付 cmd 布局特例；key equivalents 大表把 macOS「菜单快捷键挪位」的系统行为复刻到 GPUI 绑定层。
- Web 最简：浏览器已按布局翻译 `event.key`，无 keymap、布局硬编码 `"us"`，但需处理 `isComposing` 与 AltGr（浏览器把 AltGr 报成 ctrl+alt）两个浏览器特有语义。
- 统一出口是 `PlatformInput::KeyDown(KeyDownEvent)`：平台构造 `Keystroke` → 窗口经 `dispatch_key` 与绑定表匹配 → 命中则分发 action，未命中也总会通知 `observe_keystrokes` 的订阅者。

## 7. 下一步学习建议

- 下一讲（u3-l4）继续输入面：光标样式、窗口外观覆盖与外部拖放（`set_cursor_style`、`ExternalDragPayload`），与键盘同属 `Platform`/`PlatformWindow` 的输入相关方法族。
- 想深挖绑定匹配的同学推荐阅读 `crates/gpui/src/keymap/`（`Keymap`、`binding.rs` 的多键序列与上下文谓词）以及 `crates/gpui/src/window.rs` 的 `PendingInput`/多键序列等待逻辑（L5304-L5367 是本讲 4.1 链路的延伸）。
- 键盘与 IME（输入法）只在本文边缘出现：`PlatformWindow::set_input_handler`、`update_ime_position` 与 Linux 的 zwp_text_input_v3 处理（wayland/client.rs L1924 起）值得作为一个独立的源码阅读专题。
