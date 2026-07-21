# squirrel.yaml 配置文件结构

## 1. 本讲目标

上一篇（u3-l1）我们读了 `SquirrelConfig` 这个 Swift 门面，知道了前端是如何用 `getString/getBool/getDouble/getColor/getAppOptions` 五个接口去读 librime 配置的。但「读配置」只是手段，真正被读的那份文件长什么样、每个键到底控制什么行为，本讲就要把这张「配置地图」铺开。

本讲学完后你应该能够：

- 说出 `data/squirrel.yaml` 的顶层结构（`config_version` / `keyboard_layout` / `chord_duration` / `show_notifications_when` / `status_icon` / `style` / `preset_color_schemes` / `app_options`）各自的作用。
- 区分「全局行为开关」「面板样式」「配色方案」「应用级特化」四类配置的边界。
- 知道每个 YAML 键在前端代码里被谁、在哪一行读取，从而具备「改一行配置 → 定位到代码」的能力。
- 学会通过 `app_options` 为某个具体应用（按 bundle ID）定制输入行为。

## 2. 前置知识

在进入配置文件之前，先确认几个上一讲（u3-l1）和第二单元已经建立的概念：

- **base config 与 schema config**：`squirrel.yaml` 是「基础配置」（base config），描述的是前端自己的全局行为与外观；而输入方案（schema，如朙月拼音）描述的是「按键如何转成汉字」的引擎规则。`SquirrelConfig` 在 schema config 查不到某项时会回退到 base config。
- **配置部署（deploy）**：用户编辑 `~/Library/Rime/squirrel.yaml` 后，需要让 librime 重新读取。Squirrel 用 `config_version` 这个键作为「版本号哨兵」，借助 `deploy_config_file("squirrel.yaml", "config_version")` 判断是否需要重新部署。
- **librime 的 option 机制**：`set_option(session, name, value)` 会往引擎里写一个「运行时开关」，引擎和前端都可以用 `get_option` 读回。`app_options` 的本质就是「把一组布尔开关按应用批量 `set_option`」。
- **bundle ID**：macOS 上每个应用都有一个形如 `com.apple.Terminal`、`com.microsoft.VSCode` 的唯一标识，`IMKTextInput.bundleIdentifier()` 在会话创建时取到它，作为 `app_options` 的查找键。

> 提示：`squirrel.yaml` 只是「签入仓库的默认值」。真正生效的通常是用户目录 `~/Library/Rime/squirrel.yaml`（u2-l2 里 `user_data_dir` 指向该目录），两者内容同构，本讲一律以仓库里的 `data/squirrel.yaml` 为准。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml) | 本讲主角：Squirrel 前端的默认配置，共四个顶层节 + 若干全局项。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 读取 `config_version` / `show_notifications_when` / `status_icon/show`，并在 `loadSettings` 中部署配置。 |
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 读取 `keyboard_layout` / `chord_duration`，以及消费 `app_options`（`updateAppOptions`）并执行 `ascii_mode` / `no_inline` / `inline` / `vim_mode` 的运行时逻辑。 |
| [sources/SquirrelConfig.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift) | `getAppOptions` 用迭代器把 `app_options/<bundleID>` 下的布尔项读成字典。 |

主题加载（`color_scheme` / `style` 下的大量外观项）的详细机制留给 u3-l3（`SquirrelTheme.load`）与 u3-l4（亮/暗与 schema 特化），本讲只讲「这些键代表什么」。

## 4. 核心概念与源码讲解

`squirrel.yaml` 自上而下可以划分成四个最小模块：

1. 顶层全局项（`keyboard_layout` / `chord_duration` / `show_notifications_when` / `status_icon` / `config_version`）。
2. `style` 全局样式节（布局、字体、几何、颜色策略、候选格式）。
3. `preset_color_schemes` 预设配色方案仓库。
4. `app_options` 应用级选项。

下面逐一拆解。

### 4.1 顶层全局项

#### 4.1.1 概念说明

文件最顶部是一组「散装的」全局键，不属于任何子节。它们控制的是「输入法作为一个整体」的行为：西文键盘布局回退、和弦打字节奏、通知是否弹出、菜单栏图标是否显示，以及配置自身的版本号。这一节的共同点是——**与具体输入方案、具体面板外观无关，纯粹是进程级开关**。

#### 4.1.2 核心流程

这些键的读取时机各不相同：

```text
启动期 loadSettings:
  config_version  →  deploy_config_file 用它判断要不要重新部署
  show_notifications_when  →  决定 enableNotifications 布尔
  status_icon/show  →  决定 showStatusIcon 布尔 + refreshStatusItem

会话创建 createSession:
  bundleIdentifier()  →  作为 app_options 的查找键

每次激活 activateServer:
  keyboard_layout  →  覆盖西文键盘布局

每次和弦按键 updateChord:
  chord_duration  →  作为和弦超时定时器间隔
```

也就是说，**同一个 YAML 文件里的键，会被三个不同的生命周期阶段、分散在两个文件里的代码分别读取**。这是 Squirrel 配置阅读的第一个难点。

#### 4.1.3 源码精读

先看文件本身的开头（顶层项都在这里）：

顶层选项区：[data/squirrel.yaml#L4-L25](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L4-L25) — 依次是 `config_version`、`keyboard_layout`、`chord_duration`、`show_notifications_when` 与 `status_icon` 子节。注意每个键上方都有 `# options: ...` 形式的可选值注释，这是 librime 配置常见的「自说明」写法。

配置部署哨兵：[sources/SquirrelApplicationDelegate.swift#L163-L166](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L163-L166) — `start_maintenance` 返回真时调用 `deploy_config_file("squirrel.yaml", "config_version")`。第二个参数 `"config_version"` 告诉 librime：用这个键的值（当前是 `'1.0'`）作为版本号，只有当用户目录里的版本号发生变化时才触发重新部署。这样既能在升级时刷新默认配置，又不会覆盖用户已经改过的本地副本。

通知开关：[sources/SquirrelApplicationDelegate.swift#L175-L176](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L175-L176) — 一行读 `show_notifications_when`（只要不等于 `"never"` 就启用通知），下一行读 `status_icon/show`（缺省时 `?? true` 仍显示图标）。注意 `status_icon` 是个子节，所以路径写成 `"status_icon/show"`，这正是 u3-l1 讲过的「用 `/` 分隔的 librime 路径」。

键盘布局回退：[sources/SquirrelInputController.swift#L169-L179](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L169-L179) — `activateServer` 里把 `keyboard_layout` 的三种取值翻译成系统键盘标识：

- `last` 或空字符串 → 不覆盖（保留上一次的西文布局）；
- `default` → `com.apple.keylayout.ABC`；
- 自定义（如 `USExtended`）→ 自动补全前缀成 `com.apple.keylayout.USExtended`。

最终通过 `client?.overrideKeyboard(withKeyboardNamed:)` 让目标应用在 ASCII（西文）模式下用这个布局。

和弦节奏：[sources/SquirrelInputController.swift#L335-L338](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L335-L338) — `chord_duration`（单位秒）只在「和弦打字」（`_chord_typing` 选项开启时）才有意义，它决定「同时按下多个键」的判定时间窗口。缺省 0.1 秒。

#### 4.1.4 代码实践

**实践目标**：把「YAML 键」与「代码读取点」一一对应起来，建立快速定位能力。

**操作步骤**：

1. 打开 [data/squirrel.yaml#L4-L25](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L4-L25)。
2. 把 `show_notifications_when` 的值从 `appropriate` 改成 `never`。
3. 用 `Squirrel --reload`（u5-l1 会详讲）触发已运行实例重新部署，或退出再登入输入法。
4. 切换输入方案，观察系统通知中心是否还会弹出「方案切换」提示。

**需要观察的现象**：改成 `never` 后，切方案时菜单栏/通知中心不再出现方案名提示；改回 `appropriate` 则恢复。状态栏「中 / Ａ」图标不受此键影响（它由 `status_icon/show` 单独控制）。

**预期结果**：通知静默，但候选面板与状态栏图标照常工作——证明 `show_notifications_when` 只管「通知弹窗」这一条通道。

> 本实践需要在本机安装 Squirrel 才能观察运行结果，CI 环境无法验证；如暂无 macOS 环境，标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `config_version` 要单独存在，而不是直接重新部署整个文件？
**参考答案**：`deploy_config_file` 用它做版本号哨兵——只有值变了才触发部署。这样升级时能刷新默认值，又不会在每次启动时都覆盖用户在 `~/Library/Rime/squirrel.yaml` 里手改的定制项。

**练习 2**：`keyboard_layout: last` 与 `keyboard_layout: default` 在代码里的实际效果有什么区别？
**参考答案**：`last` 把翻译后的 `keyboardLayout` 置为空串，跳过 `overrideKeyboard`，保留上次使用的西文布局；`default` 翻译成 `com.apple.keylayout.ABC`，强制切到 ABC 布局。

### 4.2 `style` 全局样式节

#### 4.2.1 概念说明

`style` 是整个文件里最大、也是与「面板长什么样」最相关的一节。它控制的不是输入逻辑，而是**候选词面板的布局、字体、几何尺寸、颜色策略与候选词排版格式**。这一节的键最终几乎都被 `SquirrelTheme.load`（u3-l3 详讲）读取，并写进 `SquirrelPanel` / `SquirrelView` 的属性，驱动第四单元的自绘 UI。

需要特别区分两类容易混淆的概念：

- **布局方向**：`candidate_list_layout`（候选词横向 `linear` 还是纵向 `stacked`）与 `text_orientation`（文字水平 `horizontal` 还是竖排 `vertical`），二者是独立的维度。
- **内嵌策略**：`inline_preedit`（编码是否内嵌在光标处）与 `inline_candidate`（选中的候选词是否内嵌）。这两个键在 u2-l7 已见过，它们会和 `app_options` 里的 `no_inline` / `inline` 联合判定。

#### 4.2.2 核心流程

`style` 的消费链可以概括为：

```text
squirrel.yaml 的 style 节
   │  （SquirrelTheme.load 读取，u3-l3）
   ▼
SquirrelTheme 的各属性（字体、颜色、几何、模板）
   │
   ├──▶ SquirrelPanel.update：拼候选富文本、测量尺寸、定位
   └──▶ SquirrelView.draw：用 Core Graphics 画背景/高亮/边框
```

其中几个关键映射：

| style 键 | 作用 | 主要消费者 |
| --- | --- | --- |
| `color_scheme` / `color_scheme_dark` | 选择哪套配色（亮/暗） | `SquirrelTheme.load`（u3-l3/u3-l4） |
| `candidate_list_layout` | 候选词排列：`stacked` / `linear` | `SquirrelPanel` |
| `text_orientation` | 文字朝向：`horizontal` / `vertical` | `SquirrelPanel`（垂直旋转） |
| `inline_preedit` / `inline_candidate` | 内嵌编码 / 内嵌候选 | `SquirrelInputController` 联合 `app_options` |
| `memorize_size` | 面板是否吸附屏幕边缘以减少跳动 | `SquirrelPanel.show` |
| `mutual_exclusive` | 透明色是否互斥叠加 | `SquirrelView.draw` |
| `translucency` | 是否启用半透明背景 | `SquirrelPanel` 背景视图 |
| `show_paging` | 是否显示翻页小箭头 | `SquirrelView` |
| `corner_radius` / `hilited_corner_radius` / `border_*` / `line_spacing` / `spacing` / `shadow_size` | 几何尺寸 | `SquirrelView` / `SquirrelPanel` |
| `candidate_format` | 候选行模板，含 `[label]/[candidate]/[comment]` | `SquirrelPanel.update` |
| `font_face` / `font_point` / `label_font_*` / `comment_font_*` | 字体族与字号 | `SquirrelTheme.decodeFonts` |

#### 4.2.3 源码精读

`style` 节本体：[data/squirrel.yaml#L27-L74](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L27-L74) — 注意其中两条「弃用注释」：

- 第 33 行：`horizontal` 自 0.36 起弃用，改用 `candidate_list_layout: stacked | linear`。
- 第 62-65 行：候选格式里的 `%@` 与 `%c` 自 1.0 起弃用，新模板用 `[label]` / `[candidate]` / `[comment]` 三个具名占位符（`%@` 旧含义是「候选词+注释」、`%c` 旧含义是「标签」）。

候选格式模板：[data/squirrel.yaml#L65](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L65) — 默认值 `'[label]. [candidate] [comment]'`，渲染出来类似「1. 你好 nǐ hǎo」。`SquirrelPanel.update` 会把 `[label]` / `[candidate]` / `[comment]` 替换为实际内容并分别套用不同文本属性（u4-l1 详讲）。

内嵌策略的联合判定：[sources/SquirrelInputController.swift#L450-L451](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L450-L451) — 这里能看到 `style` 里的 `inline_preedit`（经主题读到 `panel.inlinePreedit`）与 `app_options` 里的 `no_inline` / `inline` 如何联合：

```swift
inlinePreedit = (panel.inlinePreedit && !rimeAPI.get_option(session, "no_inline")) || rimeAPI.get_option(session, "inline")
inlineCandidate = panel.inlineCandidate && !rimeAPI.get_option(session, "no_inline")
```

也就是说，`style` 只提供「默认意愿」，`app_options` 可以在特定应用里否决（`no_inline`）或强制（`inline`）。这正是 4.4 节要讲的「应用级特化」与 `style` 的交汇点。

#### 4.2.4 代码实践

**实践目标**：理解布局与内嵌两个维度相互独立。

**操作步骤（源码阅读型）**：

1. 读 [data/squirrel.yaml#L34-L38](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L34-L38)，确认 `candidate_list_layout` 与 `text_orientation` 是两个不同的键。
2. 尝试在脑中枚举四种组合：`(stacked, horizontal)` / `(stacked, vertical)` / `(linear, horizontal)` / `(linear, vertical)`，并判断哪些组合是现实中常见的（例如竖排输入法通常是 `stacked + vertical`）。
3. 进一步把 `inline_preedit` 开关叠加上去，画出「编码显示在哪里」的二维表。

**需要观察的现象**：`candidate_list_layout` 决定候选词之间是换行还是并排；`text_orientation` 决定每个汉字本身是横躺还是正立；`inline_preedit` 决定编码是出现在光标处还是浮在面板顶部。

**预期结果**：四个组合各有意义，证明这三个键是正交维度，而非同义重复。

> 实际视觉效果待本地验证；若仅做源码阅读，重点在于确认「这三个键互不依赖」。

#### 4.2.5 小练习与答案

**练习 1**：`candidate_format: '%c %@'`（见 `clean_white` 配色，[L239](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L239)）和默认的 `'[label]. [candidate] [comment]'` 用的是同一套占位符吗？
**参考答案**：不是。`%c` / `%@` 是 1.0 之前的旧占位符（`%c`=标签、`%@`=候选词+注释），`[label]` / `[candidate]` / `[comment]` 是新占位符。新代码会兼容旧占位符，但新配置推荐用具名占位符。

**练习 2**：`translucency: true` 但背景色不透明时，半透明还有视觉效果吗？
**参考答案**：基本没有。YAML 注释（[L43-L44](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L43-L44)）明确说明：`translucency` 只在背景色透明（Alpha < 255）时才可见。

### 4.3 `preset_color_schemes` 预设配色

#### 4.3.1 概念说明

`preset_color_schemes` 是一个「配色方案仓库」，里面登记了二十多套有名字的配色（`aqua` / `azure` / `luna` / `ink` / `solarized_light` / `mojave_dark` …）。`style.color_scheme` 通过名字引用其中一套，把它叠在全局 `style` 之上。这样做的好处是：**外观可以「整包切换」而不必逐项改 `style`**。

每个配色方案本身又是一张「键 → 颜色值」的表。这里再次用到 u3-l1 讲过的 Rime 历史颜色约定：**字节序是 `0xAABBGGRR`**（从高位到低位：Alpha、Blue、Green、Red），与常见的 `0xAARRGGBB` 相反。

#### 4.3.2 核心流程

颜色值的两种写法与解析规则（与 `SquirrelConfig.color(from:inSpace:)` 的两条正则对应）：

- 8 位 `0xAABBGGRR`：完整含 Alpha。
- 6 位 `0xBBGGRR`：省略 Alpha，解析时默认 \( \alpha = 255 \)。

即对 6 位值 \( v = 0xBBGGRR \)，解析为

\[
(R, G, B, A) = (v\ \&\ 0xFF,\ (v \gg 8)\ \&\ 0xFF,\ (v \gg 16)\ \&\ 0xFF,\ 255)
\]

对 8 位值则再多取高字节作为 Alpha。

颜色还会受 `color_space` 键影响：默认按 `sRGB`，方案里写 `color_space: display_p3`（见 `solarized_light`）则按广色域 Display P3 构造 `NSColor`（u3-l1 的 `getColor(inSpace:)` 会把色空间一路传下去）。

一套配色方案可以覆盖哪些键？大致分四组：

| 分组 | 典型键 |
| --- | --- |
| 文本 | `text_color`、`candidate_text_color`、`comment_text_color`、`label_color` |
| 背景 | `back_color`、`border_color`、`candidate_back_color`、`preedit_back_color` |
| 高亮文本 | `hilited_text_color`、`hilited_candidate_text_color`、`hilited_candidate_label_color`、`hilited_comment_text_color` |
| 高亮背景 | `hilited_back_color`、`hilited_candidate_back_color` |

值得注意：**配色方案不只能改颜色**。它还可以覆盖 `style` 里的几何与布局项，例如 `clean_white`（[L235-L253](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L235-L253)）就自带 `candidate_list_layout: linear`、`candidate_format: '%c %@'`、`corner_radius: 6` 等。也就是说，配色方案是「一组外观约定的打包」，颜色只是其中一部分。这套叠加覆盖的精确规则在 u3-l3（`SquirrelTheme.load`）详讲。

#### 4.3.3 源码精读

`preset_color_schemes` 节起点：[data/squirrel.yaml#L76-L89](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L76-L89) — 第一套 `native` 很特殊：它只有 `name: 系統配色`，没有任何颜色键。这是「跟随系统外观」的占位方案，`style.color_scheme: native`（见 [L28](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L28)）时由 u3-l4 的亮/暗逻辑特殊处理。

一个「全键」范例：[data/squirrel.yaml#L299-L324](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L299-L324)（`mojave_dark`）—— 同时声明了布局（`linear` / `inline_preedit`）、几何（`corner_radius` / `border_*`）、字体（`font_face` / `font_point`）和全套颜色，是「配色方案即整套主题」的典型。

广色域示例：[data/squirrel.yaml#L326-L343](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L326-L343)（`solarized_light`）—— 第 329 行 `color_space: display_p3`，注释「Only available on macOS 10.12+」，说明广色域是较新的能力。

颜色解析的实际代码：[sources/SquirrelConfig.swift#L122-L131](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L122-L131) — 两条正则分别匹配 8 位与 6 位十六进制，捕获组顺序是 `alpha/blue/green/red`（与 `0xAABBGGRR` 的高低位一致），6 位时 Alpha 硬编码 255。这与上面给的公式完全对应。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`0xAABBGGRR` 字节序」这一历史约定。

**操作步骤**：

1. 打开 [data/squirrel.yaml#L299-L324](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L299-L324)（`mojave_dark`）。
2. 找到 `hilited_candidate_back_color: 0xcb5d00`（[L321](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L321)）。
3. 按本节公式手算：6 位值 \( 0xcb5d00 \) → \( B=0xcb=203, G=0x5d=93, R=0x00=0, A=255 \)，即 RGB(0, 93, 203)，是一个偏蓝的色块。
4. 对照 [sources/SquirrelConfig.swift#L126-L128](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L126-L128) 确认 6 位分支的捕获组顺序就是 `blue/green/red`，与你手算一致。

**需要观察的现象**：高亮候选词背景呈现蓝色调，而非按「直觉的 `0xRRGGBB`」算出来的橙色——证明字节序确实是 BGR 反过来的。

**预期结果**：手算 RGB(0, 93, 203) 与实际渲染颜色一致。若按 `0xRRGGBB` 误算会得到 RGB(203, 93, 0) 的橙色，二者明显不同。

#### 4.3.5 小练习与答案

**练习 1**：`back_color: 0xeeeceeee`（[L84](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L84)，`aqua`）的 Alpha 是多少？背景是接近透明还是不透明？
**参考答案**：8 位值，Alpha = `0xee` ≈ 233/255，约 93% 不透明，属于轻微半透明。

**练习 2**：为什么 `native` 配色方案可以一行颜色键都不写？
**参考答案**：`native` 是「跟随系统亮/暗外观」的占位方案，实际颜色不由配置项决定，而是由 u3-l4 的亮/暗逻辑结合窗口 appearance 推导，因此无需在 YAML 里写颜色。

### 4.4 `app_options` 应用级选项

#### 4.4.1 概念说明

`app_options` 是本讲最有「可玩性」的一节：它允许**按应用（bundle ID）定制输入行为**。例如「在终端里默认英文、且不要内嵌编码」「在 Chrome 里强制内嵌以规避某 bug」。其本质是：当一个会话的目标应用命中某个 bundle ID 时，前端把该应用名下登记的所有布尔项，逐条 `set_option` 写进 librime，从而让引擎和前端在该应用里改变行为。

`app_options` 下最常见的四个键：

| 键 | 作用 |
| --- | --- |
| `ascii_mode` | 会话进入该应用时初始为西文（ASCII）模式 |
| `no_inline` | 否决内嵌（既不内嵌编码，也不内嵌候选） |
| `inline` | 强制内嵌编码（即使 `style.inline_preedit` 为 false） |
| `vim_mode` | 在该应用里，按 Esc / Ctrl-[ / Ctrl-c 退出插入模式时自动切回 ASCII |

注意 `no_inline` 与 `inline` 的不对称——这已在 u2-l7 讲过：`no_inline` 同时否决编码内嵌与候选内嵌，而 `inline` 只强制开启编码内嵌。

#### 4.4.2 核心流程

`app_options` 的完整生命周期：

```text
createSession:
  bundleIdentifier() → currentApp
  updateAppOptions():  getAppOptions(currentApp) → for (k,v) in ... set_option(session, k, v)

handle (每次按键进门):
  if client.bundleIdentifier() != currentApp:
      currentApp = 新 bundleID
      updateAppOptions()      # 切到新应用，重灌一遍选项

rimeUpdate / processKey 中:
  get_option("no_inline") / get_option("inline") → 联合 style 算 inlinePreedit/inlineCandidate
  get_option("vim_mode") + 按键判定 → 自动切 ascii_mode
```

两个关键点：

1. **应用切换会重新应用**。`handle` 主循环每次进门都会比对 `bundleIdentifier()` 与缓存的 `currentApp`，一旦不同就重灌选项——这正是「切到终端就变英文、切回浏览器就变中文」的实现原理。
2. **`app_options` 只接受布尔值**。`getAppOptions` 用 `getBool` 解析每一项，所以本节里全是 `true`/`false`。

#### 4.4.3 源码精读

`app_options` 节本体：[data/squirrel.yaml#L391-L437](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L391-L437) — 一组以 bundle ID 为键、以布尔选项为值的映射。典型条目：

- 终端类（[L400-L405](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L400-L405)）：`com.apple.Terminal` 与 `com.googlecode.iterm2` 都是 `ascii_mode: true` + `no_inline: true`——终端默认英文且不内嵌（内嵌编码会破坏终端回显，u2-l7 讲过用全角空格 U+3000 占位）。
- Vim 用户（[L406-L409](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L406-L409)）：`org.vim.MacVim` 三项全开，`vim_mode: true` 让你按 Esc 回命令模式时自动切回英文，避免中文状态卡住 Vim。
- Bug 规避类（[L429-L437](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L429-L437)）：Chrome / Edge / Telegram 用 `inline: true`，每条都附 `# 规避 https://github.com/rime/squirrel/issues/...` 的 issue 链接——这是「用配置而非改代码来绕开宿主应用 bug」的典型用法。

读取字典：[sources/SquirrelConfig.swift#L102-L114](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L102-L114) — `getAppOptions` 用 librime 的 `config_begin_map` / `config_next` / `config_end` 三段式迭代器遍历 `app_options/<bundleID>` 下的每个键，逐个 `getBool` 解析，组装成 `[String: Bool]` 字典返回。这就是「YAML 子节 → Swift 字典」的转换层。

写入引擎：[sources/SquirrelInputController.swift#L366-L375](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L366-L375) — `updateAppOptions` 拿到字典后，`for (key, value) in appOptions { rimeAPI.set_option(session, key, value) }`。注意它还顺带处理了一个调试用的 `unsafe/report_bundleid` 开关（与本讲主题无关，但说明配置里存在「非 app_options 的隐藏键」）。

切应用重灌：[sources/SquirrelInputController.swift#L47-L51](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L47-L51) — `handle` 进门处的应用切换检测，`currentApp != app` 即调 `updateAppOptions()`。这是「按应用定制」能实时生效的根因。

`vim_mode` 的实际效果：[sources/SquirrelInputController.swift#L404-L408](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L404-L408) — 当引擎未处理（`!handled`）且按键是 Esc / Ctrl-c / Ctrl-[ 之一、且 `vim_mode` 开启、且当前非 ASCII 时，自动 `set_option(session, "ascii_mode", true)`。

#### 4.4.4 代码实践

**实践目标**：亲手为一个新应用添加两条应用级选项，验证「配置 → 行为」的闭环。

**操作步骤**：

1. 找到目标应用的 bundle ID。可以在「活动监视器」里对进程取样，或用 `mdls -name kMDItemCFBundleIdentifier /Applications/xxx.app`（待本地验证命令可用性）。
2. 复制 [data/squirrel.yaml#L391-L437](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L391-L437) 的结构，在 `app_options:` 下新增：

   ```yaml
   app_options:
     com.todesktop.some-editor:   # 替换为你查到的真实 bundle ID
       ascii_mode: true
       no_inline: true
   ```

   （示例代码：上面这段 YAML 是为讲解构造的，请替换 `com.todesktop.some-editor` 为真实 bundle ID。）

3. 把改动落到用户目录 `~/Library/Rime/squirrel.yaml`（仓库里的 `data/squirrel.yaml` 只是默认值）。
4. 运行 `Squirrel --reload` 触发重新部署。

**需要观察的现象**：切到该应用时，输入法初始处于英文（ASCII）模式（菜单栏图标显示「Ａ」）；即使全局 `style.inline_preedit: true`，在该应用里编码也不会内嵌到光标处（而是显示在浮起的面板里）。

**预期结果**：`ascii_mode: true` 让初始模式为西文；`no_inline: true` 否决内嵌——两条选项各自改变了「初始模式」与「编码显示位置」这两件不同的事。

> 改 `data/squirrel.yaml` 不会直接生效，必须同步到 `~/Library/Rime/squirrel.yaml` 并重新部署；运行效果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么终端类应用几乎都同时开 `ascii_mode: true` 和 `no_inline: true`，而 Chrome 只开 `inline: true`？
**参考答案**：终端默认需要英文命令、且内嵌编码会破坏其字符回显（终端不按 marked text 协议显示），所以既强制英文又否决内嵌；Chrome 的 `inline: true` 则是为了规避该应用上「面板定位/回显」的特定 bug（见 issue 链接），属于反向的强制内嵌。

**练习 2**：`app_options` 里能不能写 `vim_mode: false` 来在某应用里关闭 vim 行为？
**参考答案**：可以。`getAppOptions` 用 `getBool` 解析每一项，`false` 同样会 `set_option(session, "vim_mode", false)`。但默认情况下 `vim_mode` 本就是关的，显式写 `false` 通常只在「全局开了、某应用想关」时才有意义。

## 5. 综合实践

把本讲四个模块串起来，完成一次完整的「读配置 → 改配置 → 定位代码 → 验证行为」闭环。

**任务**：假设你常用一个基于 todesktop 打包的编辑器（bundle ID 形如 `com.todesktop.xxx`），它在编辑器里的中文输入回显有 bug。请：

1. **读**：通读 [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml)，判断该用哪一节解决问题（答案：`app_options`，参考 Chrome 的 `inline: true` 规避法，[L429-L434](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L429-L434)）。
2. **改**：在 `app_options` 下为该 bundle ID 添加 `ascii_mode: true`（默认英文，减少误触）与 `inline: true`（规避回显 bug）。
3. **定位**：用本讲的源码地图，说出这两条选项分别被哪几行代码消费——`ascii_mode` 经 [SquirrelInputController.swift#L182-L184](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L182-L184) 驱动状态栏图标、`inline` 经 [SquirrelInputController.swift#L450-L451](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L450-L451) 联合 `style` 计算内嵌策略；二者都由 [SquirrelInputController.swift#L366-L375](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L366-L375) 的 `updateAppOptions` 写入引擎。
4. **部署**：同步到 `~/Library/Rime/squirrel.yaml` 并 `Squirrel --reload`。
5. **验证**：切到该编辑器，确认初始为英文模式、且编码内嵌在光标处；再切回其它应用，确认行为恢复全局默认——以此证明「切应用重灌」（[SquirrelInputController.swift#L47-L51](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L47-L51)）确实在工作。

> 第 4、5 步需要本机安装 Squirrel，运行结果待本地验证；前三步（读/改/定位）可纯靠源码完成。

## 6. 本讲小结

- `squirrel.yaml` 由四块组成：顶层全局项、`style` 全局样式、`preset_color_schemes` 配色仓库、`app_options` 应用级选项；外加一个 `config_version` 部署哨兵。
- 顶层全局项被**分散在两个文件、三个生命周期阶段**读取：`config_version`/`show_notifications_when`/`status_icon` 在 `loadSettings`，`keyboard_layout` 在 `activateServer`，`chord_duration` 在和弦定时器。
- `style` 节描述面板外观，键值最终由 `SquirrelTheme.load`（u3-l3）读取；其中 `inline_preedit` / `inline_candidate` 会与 `app_options` 的 `no_inline` / `inline` 联合判定。
- 颜色用 Rime 历史字节序 `0xAABBGGRR`（6 位省略 Alpha 默认 255），由 `SquirrelConfig.color` 的两条正则解析；`native` 配色是跟随系统外观的占位方案。
- `app_options` 按 bundle ID 把一组布尔选项 `set_option` 写进引擎，`handle` 每次进门检测应用切换并重灌——这是「按应用定制」实时生效的根因。
- 配置改动需落到 `~/Library/Rime/squirrel.yaml` 并重新部署（`config_version` 哨兵或 `Squirrel --reload`）才生效。

## 7. 下一步学习建议

本讲只讲了「键代表什么」，刻意把「键如何被读成主题对象」留给后续：

- **u3-l3（SquirrelTheme 主题加载）**：精读 `SquirrelTheme.load`，看它如何先读全局 `style/*`、再叠加 `color_scheme` 指向的配色方案覆盖项，以及 `decodeFonts` 字体级联与 `candidate_format` 归一化。本讲的 4.2 / 4.3 节是它的前置词汇表。
- **u3-l4（亮/暗主题与 schema 特化）**：看 `loadSettings` 如何同时加载亮/暗两套主题、`native` 配色如何与窗口 appearance 联动，以及 `loadSettings(for schemaID:)` 如何在 schema 含 `style` 节时叠加特化样式。
- **横向回看 u2-l7**：本讲 4.2 / 4.4 多次引用的 `inlinePreedit` / `inlineCandidate` 联合判定，其完整语义在 u2-l7 已讲过，可对照复习。
- 如果对「配置如何部署」感兴趣，可回看 u2-l2 的 `setupRime → startRime → loadSettings` 与 `deploy_config_file` 三阶段，本讲的 `config_version` 哨兵正是其中一环。
