# 语义颜色系统：让组件与主题解耦

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `ui::Color` 这份「语义调色板」的设计动机：为什么组件只说「我要表示错误」，而不说「我要红色」。
2. 按场景正确选择 `Default`、`Muted`、`Error`、`Warning`、`Success`、`Disabled` 等常用变体，并知道去哪里查每个变体的官方文档注释。
3. 跟踪完整的解析链路：组件在渲染期调用 `Color::color(cx)`，经 `theme::ActiveTheme` 拿到当前全局主题，再从 `ThemeColors` / `StatusColors` / `PlayerColors` 三类色表中查出真正的 `Hsla` 颜色值。
4. 理解 `Color::Custom(Hsla)` 为什么被源码「强烈、强烈地」不推荐，以及 `Color::Player(u32)` 如何为协作参与者稳定分配颜色。

## 2. 前置知识

本讲只依赖两个概念，我们都用通俗语言过一遍：

- **HSLA 颜色模型**。`gpui::Hsla` 是一个用四个数描述颜色的结构：色相 \(h\)（角度制，绕色环一圈的角度）、饱和度 \(s\)（灰到纯彩，0 到 1）、亮度 \(l\)（黑到白，0 到 1）、不透明度 \(a\)（透明到不透明，0 到 1）。它比 RGB 更贴近人对颜色的直觉：「暗一点、淡一点」就是调 \(l\) 和 \(s\)。主题里的每个颜色最终都是一个 `Hsla`。
- **主题（Theme）与 gpui 全局状态**。Zed 允许用户在设置里切换主题（One Light、One Dark、自定义主题族等）。当前生效的主题被存放在一个 gpui 全局（`GlobalTheme`）里，任何能拿到 `App` 上下文的地方都能读到它。上一讲（u1-l2）我们已经在 prelude 里见过跨 crate 转发的 `theme::ActiveTheme`——正是它让 `cx.theme()` 这个调用成立。

另外需要一点 Rust 基础：`Color` 是带数据的枚举（`Custom(Hsla)`、`Player(u32)`），变体可以携带载荷，这在后文会反复出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/ui/src/styles/color.rs` | 本讲主角。定义 `Color` 枚举、`color(cx)` 解析方法、`From<Hsla>` 转换，以及供组件预览体系使用的 `Component` 实现 |
| `crates/theme/src/theme.rs` | 定义 `ActiveTheme` trait、`GlobalTheme` 全局存储和 `Theme` 结构（`colors()` / `status()` / `players()` 三个访问器） |
| `crates/theme/src/styles/status.rs` | `StatusColors`：每个状态色的「前景 / 背景 / 边框」三件套 |
| `crates/theme/src/styles/players.rs` | `PlayerColors`：协作参与者色表与 `color_for_participant` 的取模分配算法 |
| `crates/ui/src/components/label/label_like.rs` | 消费方示例：`LabelCommon::color(Color)` 如何存下语义色，`render` 时才解析成 `Hsla` |

一个先记住的结论：`Color` 定义在 ui crate，颜色值定义在 theme crate。**ui 只负责「语义」，theme 只负责「外观」**，两者在渲染期才汇合。

## 4. 核心概念与源码讲解

### 4.1 Color 枚举：一份按意图命名的调色板

#### 4.1.1 概念说明

`Color` 是一个普通 Rust 枚举，它的每个变体都不是颜色，而是**意图**：`Error` 表示「出错或用户做不到某事」，`Modified` 表示「条目被修改过」，`Ignored` 表示「被版本控制忽略」。文件开头的文档注释一句话点明了设计目标：

> Sets a color that has a consistent meaning across all themes.（设定一个在所有主题中含义一致的颜色。）

见 [src/styles/color.rs:L6](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L6)。

为什么这很重要？假设组件代码里直接写「错误提示用 `hsl(0, 70%, 50%)`」：

1. 用户切到深色主题后，这个为浅色背景调的红色可能对比度不足，看不清；
2. 某个主题族想把自己的错误色统一调成偏橙，就得改几十个组件；
3. 读者看到一串数字，完全不知道它代表什么。

而写成 `Color::Error`，颜色交给主题决定，组件只声明语义。主题换、颜色变、组件代码一行不动。

#### 4.1.2 核心流程

`Color` 的生命周期分两段：

```text
构建期（无上下文）                 渲染期（有 cx）
──────────────────                ────────────────────────
Label::new("保存失败")             render(self, window, cx)
    .color(Color::Error)  ──────►     self.color.color(cx)
    只是存下枚举值                    查全局主题 → 得到 Hsla
                                     → 写入文本样式
```

关键点：builder 方法 `.color(...)` 不需要任何上下文，所以可以在任何地方链式调用；真正需要知道「当前主题是什么」的解析动作被推迟到 `render`，那一刻手里一定有 `cx`。

#### 4.1.3 源码精读

先看枚举全貌，派生宏我们稍后解释：

[Color 枚举的定义与派生](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L7-L19)——`Color` 通过 `#[derive(...)]` 获得了 `Default`（默认变体是 `Default`，见 L20-L25 的 `#[default]` 标注）、`RegisterComponent`（接入组件预览体系，u8-l5 详讲）以及 `Documented` / `DocumentedFields` / `DocumentedVariants`（把每个变体的文档注释暴露给预览系统显示）。

[枚举主体](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L19-L86) 共 24 个变体，按用途分成四组：

| 分组 | 变体 | 典型场景 |
| --- | --- | --- |
| 文本强调层级 | `Default`、`Muted`、`Hidden`、`Disabled`、`Placeholder`、`Accent`、`Selected` | 正文 / 次要说明 / 隐藏文件 / 禁用按钮 / 输入框占位符 / 链接高亮 / 选中项 |
| 通用状态 | `Success`、`Warning`、`Error`、`Info`、`Hint`、`Conflict`、`Created`、`Modified`、`Deleted`、`Ignored` | 状态栏、诊断提示、文件树状态角标 |
| 版本控制专用 | `VersionControlAdded` / `Conflict` / `Deleted` / `Ignored` / `Modified` | 行内 git 标记、diff 着色，主题可为 vcs 单独配色 |
| 特殊 | `Debugger`、`Player(u32)`、`Custom(Hsla)` | 调试器 UI、协作者光标、绕过语义体系逃生门 |

两个值得注意的细节：

- 每个变体都有认真的文档注释，且相互推荐替代品。例如 `Default` 的文档说「想要更弱强调请考虑 `Color::Muted` 或 `Color::Hidden`」，见 [src/styles/color.rs:L20-L25](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L20-L25)。选色时的第一参考就是这些注释。
- `Selected` 和 `Accent` 当前解析到同一个主题色 `text_accent`（见 [src/styles/color.rs:L105](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L105) 与 [L108](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L108)）。两个语义名、恰好同色——这正是解耦的收益：某天某主题想把「选中」换成独立颜色，只改主题定义即可，所有组件自动跟随。

#### 4.1.4 代码实践

**实践目标**：亲手把六个常用变体写成可编译的 doc 示例，并对照官方预览验证自己的理解。

**操作步骤**：

1. 先读一遍官方自己的展示代码：`Color` 为组件预览体系实现了 `Component::preview`，用 `Label` + `.color(...)` 把变体逐个渲染出来，入口在 [src/styles/color.rs:L136-L172](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L136-L172)（Text Colors 一组）。注意它连说明文字都直接取自变体文档：`.description(Color::Default.get_variant_docs())`。
2. 参照 `Label::color` 已有的 doc 示例写法（[src/components/label/label.rs:L181-L186](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label.rs#L181-L186)），在你自己的 fork 里为六个变体各写一行场景化构造（示例代码）：

   ```rust
   /// ```no_run
   /// # use ui::{Color, Label, LabelCommon};
   /// let body       = Label::new("常规正文");           // Color::Default
   /// let hint       = Label::new("次要说明").color(Color::Muted);
   /// let ok         = Label::new("已同步").color(Color::Success);
   /// let warn       = Label::new("磁盘即将写满").color(Color::Warning);
   /// let err        = Label::new("保存失败").color(Color::Error);
   /// let unavailable = Label::new("此项不可用").color(Color::Disabled);
   /// ```
   ```

3. 运行 `cargo test -p ui --doc` 验证示例能通过编译。

**需要观察的现象**：doc 示例只构造、不渲染，因此不需要窗口或 GPUI 应用上下文即可编译——这印证了 4.1.2 的「解析被推迟到渲染期」。

**预期结果**：doc 测试通过编译（`no_run` 使其不实际执行渲染）。具体测试输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：文件树里有一个新建且未提交的文件，应该用 `Created` 还是 `VersionControlAdded`？两者什么关系？

**答案**：按源码文档，`Created` 是通用的「新建条目」语义（比如新文件出现在磁盘上），`VersionControlAdded` 是版本控制语境下的「新增文件/内容」。文件树的未提交新文件通常用 `Created`（`Color::color` 里它映射到 `cx.theme().status().created`）；如果这个 UI 明确属于 git diff 着色，就用 `VersionControlAdded`。两者的区别是主题可以分别为通用状态和 vcs 场景配不同的颜色。

**练习 2**：为什么 `Selected` 与 `Accent` 当前同色，源码还要保留两个变体？

**答案**：因为 `Color` 表达的是语义而非外观。保留两个名字，主题就保留了「把选中色和强调色配成不同值」的自由；如果合并成一个变体，这层区分就永久丢失了。组件作者按「这是选中态」还是「这是强调/链接」选词，而不是按「现在看起来是什么颜色」选词。

**练习 3**：`#[default]` 标注在 `Default` 变体上（[L20](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L20)），这带来了什么便利？

**答案**：整个 `Color` 枚举实现了 `Default`，缺省值就是 `Color::Default`（默认文本色）。于是组件结构体可以直接 `#[derive(Default)]` 或把 `Color` 字段初始化为 `Color::Default`，不必显式写颜色——「不指定就是正常文本」这一约定由类型系统兜底。

### 4.2 Color::color(cx)：渲染期的主题解析

#### 4.2.1 概念说明

`Color` 自己不存任何 `Hsla`（`Custom` 除外），它只是一个「查表键」。查的表就是当前主题。把键变成值的唯一入口是：

```rust
pub fn color(&self, cx: &App) -> Hsla
```

这个方法接收 `&App`——GPUI 的根上下文类型（回顾 u1-l2：各种上下文最终都能到达 `App`）。也就是说，**语义色只有在「看得见全局状态」的时刻才能变成真颜色**，这也是它必须推迟到渲染期调用的原因。

而 `cx.theme()` 之所以可用，是因为 theme crate 给 `App` 实现了 `ActiveTheme` trait。这就是 u1-l2 里 prelude 跨 crate 转发 `theme::ActiveTheme` 的意义：不转发，你就得在每个文件里手写 `use theme::ActiveTheme`。

#### 4.2.2 核心流程

完整解析链路：

```text
Color::Error
  │ color(cx)
  ▼
cx.theme()                      ── ActiveTheme trait 方法（theme crate 实现）
  │  返回 &Arc<Theme>（全局主题）
  ▼
cx.theme().status().error       ── Theme::status() 返回 &StatusColors
  │
  ▼
Hsla ──► 写入文本样式 / 边框 / 背景
```

主题在背后分三张表：

- `colors()` → `ThemeColors`：界面基础色（文本、面板、边框、调试器强调色、版本控制五色等）；
- `status()` → `StatusColors`：状态三件套（见下）；
- `players()` → `PlayerColors`：协作者色表。

#### 4.2.3 源码精读

**第一站：`color()` 方法本体**。[src/styles/color.rs:L88-L119](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L88-L119) 是一个巨大的 `match`，把 24 个变体逐一映射到主题字段。节选关键几行（[L92-L94](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L92-L94)）：

```rust
Color::Default => cx.theme().colors().text,
Color::Muted => cx.theme().colors().text_muted,
Color::Created => cx.theme().status().created,
```

注意模式：文本类走 `colors()`，状态类走 `status()`，协作者走 `styles.player`（[L106](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L106)），`Custom` 原样返回（[L116](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L116)）。

**第二站：`ActiveTheme` 与全局主题存储**。[crates/theme/src/theme.rs:L146-L155](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/theme.rs#L146-L155)：

```rust
pub trait ActiveTheme {
    /// Returns the active theme.
    fn theme(&self) -> &Arc<Theme>;
}

impl ActiveTheme for App {
    fn theme(&self) -> &Arc<Theme> {
        GlobalTheme::theme(self)
    }
}
```

`GlobalTheme` 是一个 gpui 全局（`impl Global for GlobalTheme {}`，见 [crates/theme/src/theme.rs:L319-L324](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/theme.rs#L319-L324)），里面装着 `Arc<Theme>`，用户切主题时通过 `GlobalTheme::update_theme` 整体替换（[L332-L335](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/theme.rs#L332-L335)）。所以 `cx.theme()` 读到的永远是「此刻」的主题，切换主题后下一帧渲染自动用新颜色。

**第三站：`Theme` 的三张表**。[Theme 结构体](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/theme.rs#L233-L244) 由 `id`、`name`、`appearance`（明/暗）和 `styles: ThemeStyles` 组成；三个访问器 [`players()` / `colors()` / `status()`](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/theme.rs#L259-L281) 都只是取出 `styles` 里的对应子结构。

**第四站：为什么状态色要三件套**。[StatusColors 的字段](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/styles/status.rs#L10-L44) 里每个状态都是 `xxx` / `xxx_background` / `xxx_border` 三个颜色，例如 `error`、`error_background`、`error_border`。`Color::Error` 只取前景 `error`；需要带背景的警示横幅（如 `Callout`、`Banner`）则直接查主题拿另外两件——这就是为什么 `Color` 枚举里没有 `ErrorBackground` 这种变体：背景色不是「文本语义」，由需要的组件自己向主题查询。

**第五站：消费方**。以 `Label` 为例：[LabelCommon::color 的签名](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L44-L45) 只是把 `Color` 存进字段；真正解析发生在 [`LabelLike::render`](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/label/label_like.rs#L233-L238)：

```rust
fn render(self, _window: &mut Window, cx: &mut App) -> impl IntoElement {
    let mut color = self.color.color(cx);   // ← 语义 → Hsla 的那一刻
    if let Some(alpha) = self.alpha {
        color.fade_out(1.0 - alpha);
    }
    ...
```

`fade_out` 的存在还说明：拿到 `Hsla` 之后仍可以做数值层面的二次加工（透明度），但那是「外观微调」，不是「换语义」。

#### 4.2.4 代码实践

**实践目标**：完整走一遍「组件 → Color → 主题 → Hsla」调用链，证明你能在真实源码里独立导航。

**操作步骤**：

1. 从 [src/styles/color.rs:L107](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L107) 的 `Color::Error` 分支出发，写下它查到的字段：`cx.theme().status().error`。
2. 用 `Grep` 在 `crates/ui/src` 内搜索 `\.color(Color::Error)`，任选一个命中组件，打开其 `render` 实现，确认它最终把解析结果交给了文本样式、图标色还是边框色。
3. 再用 `Grep` 在 `crates/theme/src` 搜索 `error:`，找到默认主题给 `StatusColors::error` 赋的 `Hsla` 构造处，观察它是怎么由基础色推导出来的。
4. 画出你自己的调用链图（组件 → `LabelCommon::color` → `LabelLike::render` → `Color::color` → `ActiveTheme::theme` → `StatusColors`）。

**需要观察的现象**：解析链条上每一环都只做一件事：组件存语义、`color()` 查表、theme 存值。没有任何一环写死具体颜色。

**预期结果**：得到一张五节点的调用链图，且每一节点的源码位置都有行号可指。步骤 3 中默认主题的具体色值待本地验证（不同主题族赋值不同）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Color::color` 的参数是 `&App`，而不是让 `Color` 自己实现一个 `Color::hex()` 之类的无上下文方法？

**答案**：因为除了 `Custom`，变体的值完全取决于运行时当前生效的主题，而主题存在 gpui 全局状态里，只有通过 `App` 上下文才能读到。无上下文方法意味着把某个主题的颜色写死成「唯一答案」，这正是语义色体系要避免的。

**练习 2**：`Color` 同时被 `colors()` 系和 `status()` 系字段满足。假如未来新增一个 `Color::RenamePending`，按现有模式应加在哪张表？

**答案**：它表达的是一种状态（重命名等待确认），对应 `StatusColors` 新增 `rename_pending` 三件套，然后在 `Color::color` 的 `match` 里加一行 `Color::RenamePending => cx.theme().status().rename_pending`。`ThemeColors` 保留给「界面基础外观」（文本、面板、边框），`StatusColors` 保留给「事件/状态语义」。

**练习 3**：用户在运行中切换主题，为什么所有已显示的 `Label` 不需要重新构造就能换颜色？

**答案**：`Label` 存的是 `Color::Error` 这个键，不是解析后的 `Hsla`。每次 `render` 都重新执行 `self.color.color(cx)`，读到的又是 `GlobalTheme` 里的最新主题；主题切换会触发受影响视图重绘，下一帧解析自然得到新颜色。这就是「推迟解析」换来的响应式更新能力。

### 4.3 Color::Custom 与 Color::Player：两个特殊变体

#### 4.3.1 概念说明

枚举里有两个「不走主题」的变体，地位特殊：

- `Color::Custom(Hsla)`：直接携带一个颜色值，渲染时原样返回。它是逃生门，但源码在文档里用双重强调劝退使用者。
- `Color::Player(u32)`：携带参与者编号，渲染时到协作者色表里按编号取色，保证「同一个参与者在整个 UI 里颜色一致」——光标、头像、署名都是它。

#### 4.3.2 核心流程

`Custom` 没有流程可言：进什么、出什么（[L116](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L116)：`Color::Custom(color) => *color`）。

`Player` 的分配是一个取模映射。设色表长度为 \(n\)（含槽位 0），参与者编号为 \(i\)，则：

\[ \text{slot}(i) = (i \bmod (n-1)) + 1 \]

即编号先对 \(n-1\) 取模，再整体右移一格。槽位 0 被跳过——它保留给本地用户（`local()`），参与者只在其余槽位中循环。效果：任意大的编号都有颜色、同一编号颜色稳定、且永远不会与本地用户的颜色冲突。

#### 4.3.3 源码精读

**Custom 的警告原文**，[src/styles/color.rs:L33-L37](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L33-L37)：

```rust
/// It is highly, HIGHLY recommended not to use this! Using this color
/// means detaching it from any semantic meaning across themes.
///
/// A custom color specified by an HSLA value.
Custom(Hsla),
```

「强烈、强烈地建议不要使用！」——连用两个 HIGHLY。理由写在第二句：使用它就意味着把这个颜色从所有主题的语义体系中剥离。后果具体是：换主题时它纹丝不动（可能在新背景上对比度失效）、明暗外观不适配、语义审查工具无法归类它。

配套还有一个易被忽略的转换实现，[src/styles/color.rs:L121-L125](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L121-L125)：

```rust
impl From<Hsla> for Color {
    fn from(color: Hsla) -> Self {
        Color::Custom(color)
    }
}
```

任何 `Hsla` 都能 `.into()` 成 `Color`——写 `.color(some_hsla.into())` 会静默降级为 `Custom`。这是把「显式逃生门」重新变成「隐式后门」的风险点，代码评审时应格外留意。

**Player 的定义与取色**。[src/styles/color.rs:L68-L69](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L68-L69) 定义变体；解析在 [L106](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L106)：

```rust
Color::Player(i) => cx.theme().styles.player.color_for_participant(*i).cursor,
```

注意它取的是 `PlayerColor` 三元组（`cursor` / `background` / `selection`）中的 `cursor`——`Color::Player` 语义上是「这位参与者的标识色」。

取模算法在 [crates/theme/src/styles/players.rs:L147-L150](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/styles/players.rs#L147-L150)：

```rust
pub fn color_for_participant(&self, participant_index: u32) -> PlayerColor {
    let len = self.0.len() - 1;
    self.0[(participant_index as usize % len) + 1]
}
```

同文件上方的 [PlayerColors 其余方法](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/styles/players.rs#L125-L146) 交代了槽位语义：`local()` 取第一个槽（本地用户）、`agent()` / `absent()` 取最后一个槽、`read_only()` 把本地色转灰度。色表本身是一列按色相排布的 `PlayerColor`（红、琥珀、玉绿……，见 [L97-L121](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/styles/players.rs#L97-L121) 的构造片段），保证相邻编号拿到的是可区分的颜色。

#### 4.3.4 代码实践

**实践目标**：亲手验证 Player 取模分配，并识别 Custom 的合法与非法用法。

**操作步骤**：

1. **手工演算**：设默认色表长度 \(n = 9\)（含本地槽）。分别计算参与者 0、1、8、9、17 的槽位（按公式 \((i \bmod 8) + 1\)），确认：槽位永远落在 1..=8、编号 0 与 8 同色（周期为 8）、任何编号都不会拿到槽位 0。
2. **用法审查**：在 `crates/` 下用 `Grep` 搜索 `Color::Custom`，统计命中数量，并抽查两三处上下文，判断它们是否属于「数据驱动染色」的合理场景（例如把语法高亮主题色、用户自选颜色映射到 UI），还是本应用语义变体的偷懒写法。
3. 写下你的结论：哪几处合理、哪几处可疑，理由各一句话。

**需要观察的现象**：命中数应当远小于 `Color::Error` 之类的常用变体（待本地验证具体数字）；合理用法通常出现在「颜色来自外部数据」的边界代码里，而不是通用组件内部。

**预期结果**：一张槽位分配表（步骤 1 有确定答案：槽位分别为 1、2、1、2、3）加一份 `Custom` 用法审查清单。

#### 4.3.5 小练习与答案

**练习 1**：既然 `Custom` 如此不堪，为什么不全删了它？

**答案**：因为总有些颜色天生不属于任何预定义语义：语法高亮主题里的 token 色、用户在设置里自选的高亮色、从图片取的平均色。这些「数据驱动的颜色」没有稳定语义可言，`Custom` 是它们的正确容器。要删的不是变体，而是「本该用语义变体却顺手写了 `Custom`」的用法。

**练习 2**：`Color::Player(3)` 和 `Color::Player(11)` 在长度为 9 的色表下会是什么关系？

**答案**：两者槽位都是 \((i \bmod 8) + 1 = 4\)（3 % 8 = 3，11 % 8 = 3），颜色相同。周期性撞色是该算法接受的取舍：色表有限而参与者编号无界，保证「稳定且与本地用户不撞车」比「全局唯一」更实际；会话内活跃参与者通常远少于色表长度。

**练习 3**：`From<Hsla> for Color` 这个实现是便利还是隐患？如果你来设计，会怎么权衡？

**答案**：两面性。便利在于边界代码（接外部颜色数据）少写一层 `Color::Custom(...)` 包装；隐患在于 `.into()` 会让人在不知不觉中绕开语义体系。参考答案的权衡：保留实现但在组件公共 API 文档中显著提示，或者仅在 `theme` / 数据边界模块引入该转换、通用组件 API 只收 `Color` 并对 `Custom` 打点审查——重点是让「绕过语义」永远是一个显式、可见的决定。

## 5. 综合实践

**任务：为一页「项目同步面板」选定全部语义色，并写出可直接编译的构造代码。**

面板需求如下，请为每一项选择正确的 `Color` 变体并写出 `Label`（或图标着色）的构造调用：

1. 面板标题（常规正文）；
2. 「最近同步：3 分钟前」的次要说明；
3. 「同步成功」状态文案；
4. 「有 2 个文件冲突」警告；
5. 「无法连接服务器」错误；
6. 「回滚」按钮（当前不可用）；
7. 输入框里的占位符「输入分支名…」；
8. 文件列表中新建文件的文件名；
9. 文件列表中被 .gitignore 忽略的文件名；
10. 一位远端协作者（编号 5）的署名。

要求：

- 每一项先写「我选 X，因为语义是……」，再对照 [src/styles/color.rs:L19-L86](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L19-L86) 的变体文档自查；
- 把 10 行构造代码写成一个 doc 示例（参考 4.1.4 的格式），跑 `cargo test -p ui --doc` 验证编译；
- 最后用一段文字回答本讲的核心问题：**为什么源码「强烈、强烈地」不建议使用 `Color::Custom`？**（提示：从换主题、明暗外观、对比度、语义可审查四个角度组织答案。）

参考答案速查：1 `Default`；2 `Muted`；3 `Success`；4 `Warning`（或冲突文件名用 `Conflict`）；5 `Error`；6 `Disabled`；7 `Placeholder`；8 `Created`；9 `Ignored`；10 `Player(5)`。注意第 7 项：输入框占位符不是「弱化的正文」，主题为它单独配了 `text_placeholder`，不要用 `Muted` 凑合。

## 6. 本讲小结

- `ui::Color` 是一份**按意图命名**的调色板：24 个变体各表一个跨主题稳定的语义，颜色值全部延迟到渲染期向主题查询。
- 解析链是 `Color::color(cx)` → `ActiveTheme::theme()`（gpui 全局 `GlobalTheme`）→ `Theme` 的三张表：`colors()`（界面基础色）、`status()`（状态三件套）、`players()`（协作者色表）。
- `StatusColors` 每个状态提供前景/背景/边框三件套，`Color` 只取前景；需要背景色的组件直接查主题。
- `Selected` 与 `Accent` 当前同色但语义分离，是「语义与实现解耦」最直观的证据。
- `Color::Custom(Hsla)` 是被双重强调劝退的逃生门，`From<Hsla>` 使它可能被 `.into()` 静默触发，评审时要盯紧；`Color::Player(u32)` 用取模映射为参与者分配稳定颜色，槽位 0 永远保留给本地用户。
- 组件（如 `Label`）在构建期只存 `Color` 键，`render` 时才解析——这让主题切换后无需重建组件即可整体换色。

## 7. 下一步学习建议

下一讲（u2-l2）我们把语义色放进真正的排版场景：精读 `Label` / `LabelLike` 与 `LabelCommon` trait 的完整 API（尺寸、字重、截断），以及 `TextSize` 与 `text_ui*` 方法如何像颜色一样把「排版档位」也令牌化。届时你会发现同一套「语义键 + 渲染期解析」的模式再次出现。

在此之前，建议做两个热身阅读：

1. [src/styles/color.rs:L136-L241](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/color.rs#L136-L241) 的 `Component::preview`——官方如何用组件预览体系把 `Color` 的所有变体可视化（也为 u8-l5 埋下伏笔）；
2. `crates/theme/src/styles/status.rs` 全文——看看还有哪些状态拥有三件套，这些颜色未来如何被 `Callout`、`Banner` 类组件（u4-l3）消费。
