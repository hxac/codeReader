# 密度感知间距：spacing_ratio、rems 与 px

## 1. 本讲目标

上一讲（u3-l1）我们弄清了 `derive_dynamic_spacing!` 在**编译期**生成的两样产物：`BaseXX` 变体名和 doc 注释。本讲把视角切到**运行时**，回答三个问题：

1. 用户在设置里选择的 UI 密度（Compact / Default / Comfortable）是如何一步步传递到 `DynamicSpacing` 的？
2. 生成的三个方法 `spacing_ratio` / `rems` / `px` 各自做什么？`BASE_REM_SIZE_IN_PX = 16.0` 和 `ui_font_size` 在换算中扮演什么角色？
3. 真实组件（按钮、标签页）什么时候用 `.rems(cx)`、什么时候用 `.px(cx)`？

学完本讲，你应该能手算任意 `DynamicSpacing` 变体在任意「密度 × UI 字号」组合下的最终像素值，并知道如何把一处硬编码间距替换成密度感知间距。

## 2. 前置知识

### 2.1 像素与 rem：两种长度单位

- **`Pixels`**：绝对长度，屏幕上的物理（逻辑）像素。gpui 里的构造函数是 `px(16.)`。
- **`Rems`**：相对长度，含义是「多少个 rem 单位」。CSS 里 `1rem` 等于根元素字号；Zed 沿用了这个思想——**1 个 rem 单位等于当前 UI 字号**。gpui 里的构造函数是 `rems(1.5)`。

关键在于：`Rems` 本身不是像素，它要到**布局阶段**才被换算成像素，换算基准是 `window.rem_size()`。窗口的 rem 基准被设置为 UI 字号（后面 4.3.3 会看到证据），所以：

\[ \text{最终像素} = \text{rem 数值} \times \text{UI 字号（像素）} \]

这也解释了变体文档里 `@16px/rem` 的含义：**当 UI 字号为 16px 时**，`Base16` 的三档值正好是 14px / 16px / 18px；UI 字号变了，间距按比例跟着变。

### 2.2 设置如何进入代码：settings → 全局 → `theme_settings(cx)`

Zed 把用户设置（settings.json）解析成 `ThemeSettings` 并注册为全局状态。`theme` crate 定义了一个查询入口 trait `ThemeSettingsProvider`，具体实现由 `theme_settings` crate 在应用启动时注册。任何持有 `cx: &App` 的代码都能通过 `theme::theme_settings(cx)` 拿到当前 UI 字号和 UI 密度。本讲会完整走一遍这条链路。

### 2.3 `cx: &App`

gpui 的 `App` 是根上下文（见仓库 CLAUDE.md 的 GPUI 一节），读取全局状态、读取实体都要经过它。`DynamicSpacing` 的三个方法都以 `cx: &App` 为参数，因为它们要**在运行时**查询用户设置。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/ui_macros/src/dynamic_spacing.rs` | 宏模板：生成 `spacing_ratio` / `rems` / `px` 三个方法的地方 |
| `crates/ui/src/styles/spacing.rs` | 宏的唯一调用点；另有 `ui_density(cx)` 辅助函数 |
| `crates/theme/src/ui_density.rs` | `UiDensity` 枚举定义（Compact / Default / Comfortable） |
| `crates/theme/src/theme_settings_provider.rs` | `ThemeSettingsProvider` trait 与 `theme_settings(cx)` 查询入口 |
| `crates/theme_settings/src/theme_settings.rs` | trait 的具体实现（读全局 `ThemeSettings`） |
| `crates/theme_settings/src/settings.rs` | `ThemeSettings` 字段定义、`setup_ui_font`（把 UI 字号设为窗口 rem 基准） |
| `crates/gpui/src/geometry.rs` | `Rems` / `Pixels` 类型与 `rems()` / `px()` 构造函数 |
| `crates/gpui/src/window.rs` | `window.rem_size()` 与 `set_rem_size` |
| `crates/gpui/src/elements/div.rs` | 布局期把 rem 换算成像素的地方 |
| `crates/ui/src/components/button/button.rs`、`.../button_like.rs`、`.../tab.rs` | 三个真实使用方 |

## 4. 核心概念与源码讲解

### 4.1 UiDensity：三档密度设置

#### 4.1.1 概念说明

「UI 密度」是 Zed 的一个**实验性**用户设置：同样的界面，紧凑模式下按钮、列表的留白更小，宽松模式下留白更大。它只有三档：

- `Compact`：更紧凑
- `Default`：默认
- `Comfortable`：更宽松

`DynamicSpacing` 的每个变体都为这三档各自准备了一个像素值（上一讲讲过：Single 输入按 \(n-4 \mid n \mid n+4\) 推导，Tuple 输入直接给三个值），运行时按当前档位取用。

#### 4.1.2 核心流程

设置值在用户配置里是字符串，进入代码后变成枚举：

```
settings.json 里的 "unstable.ui_density": "comfortable"
        │ serde 反序列化（rename_all = "snake_case"）
        ▼
settings::UiDensity（settings schema 侧的枚举）
        │ ui_density_from_settings 逐一映射
        ▼
theme::UiDensity（本讲的主角）
        │ ThemeSettings::ui_density 字段（全局）
        ▼
theme_settings(cx).ui_density(cx) 查询
```

#### 4.1.3 源码精读

枚举本体在 theme crate：

- [crates/theme/src/ui_density.rs:21-L32](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/ui_density.rs#L21-L32)：定义 `UiDensity` 枚举，`Default` 变体带 `#[default]`，`#[serde(rename_all = "snake_case")]` 让 JSON 里的 `"compact"` 等小写蛇形字符串能直接反序列化。doc 注释标明这是实验性设置（跟踪 issue #18078）。
- [crates/theme/src/ui_density.rs:46-L55](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/ui_density.rs#L46-L55)：`From<String>` 实现，注意 `_ => Self::default()`——无法识别的字符串回落到 `Default` 档，而不是报错。
- [crates/theme/src/ui_density.rs:34-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/ui_density.rs#L34-L44)：`UiDensity` 自带一个 `spacing_ratio()` 方法（0.75 / 1.0 / 1.25）。**注意：生成的 `DynamicSpacing` 并不使用它**，而是对每个变体逐档 match（见 4.3.2）。源码里的 `TODO: Standardize usage throughout the app or remove` 说明这是两套并存、尚未统一的机制——读源码时容易在这里混淆，务必区分。
- [crates/settings_content/src/theme.rs:236-L238](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/settings_content/src/theme.rs#L236-L238)：设置 schema 侧，`#[serde(rename = "unstable.ui_density")]`——用户在 settings.json 里写的键名就是 `unstable.ui_density`，印证「实验性」定位。
- [crates/theme_settings/src/settings.rs:94-L96](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme_settings/src/settings.rs#L94-L96)：`ThemeSettings` 结构体上的 `ui_density: UiDensity` 字段，反序列化后的设置最终存放在这里（全局状态）。

#### 4.1.4 代码实践

1. **实践目标**：确认设置键名与回落行为。
2. **操作步骤**：
   - 阅读上面 4 处源码链接，画出「settings.json 字符串 → theme::UiDensity」的转换路径；
   - 在本地 Zed 的 settings.json 中加入 `"unstable.ui_density": "comfortable"` 和 `"ui_font_size": 20`，重启或等待热更新。
3. **需要观察的现象**：整体 UI 留白变大、文字与间距同时放大。
4. **预期结果**：与 `From<String>` 的语义一致；若写成非法值（如 `"cozy"`），按 `_ => Self::default()` 回落到 Default 档（该回落路径是否被 serde 反序列化提前拦截，**待本地验证**——serde 路径与 `From<String>` 路径是两条独立入口）。
5. 本实践需要图形界面，无法在无头环境运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`UiDensity::spacing_ratio()` 返回 0.75 / 1.0 / 1.25，这是不是 `DynamicSpacing::Base16` 在 Compact 档得到 14px 的原因？

**答案**：不是。`DynamicSpacing` 生成的代码逐变体 match 三档密度、各自除以 `BASE_REM_SIZE_IN_PX`（`14 / 16 = 0.875`），并不调用 `UiDensity::spacing_ratio()`。后者是另一套「全局比例缩放」机制，源码注释明确标注待统一（或移除）。

**练习 2**：为什么 `From<String>` 对未知字符串静默回落而不是返回 `Result`？

**答案**：这是设置解析的容错取舍：用户手写 settings.json 容易拼错，静默回落到默认值保证应用仍能启动。（代价是拼写错误不暴露——这也是双刃剑，读者可以思考如果是你会怎么选。）

---

### 4.2 theme_settings 查询：设置如何到达 DynamicSpacing

#### 4.2.1 概念说明

生成的代码里有一句 `::theme::theme_settings(cx).ui_density(cx)`。`theme_settings` 是 theme crate 提供的**全局查询函数**，背后是一个注册进 gpui 全局状态的对象。宏生成的代码之所以敢直接调用它，是因为名称解析发生在调用方（ui crate）——这是 u1-l3 讲过的「Cargo 依赖箭头 ≠ 宏展开箭头」在运行时的延续：查询到的值也完全由调用方所在应用的注册情况决定。

#### 4.2.2 核心流程

```
应用启动
  └─ theme_settings::init(...)
       └─ theme::set_theme_settings_provider(Box::new(ThemeSettingsProviderImpl), cx)
            └─ cx.set_global(GlobalThemeSettingsProvider(...))

任意代码运行时
  └─ theme::theme_settings(cx)            // cx.global::<GlobalThemeSettingsProvider>()
       └─ &dyn ThemeSettingsProvider
            ├─ .ui_density(cx)   → ThemeSettings::get_global(cx).ui_density
            └─ .ui_font_size(cx) → ThemeSettings::get_global(cx).ui_font_size(cx)
```

#### 4.2.3 源码精读

- [crates/theme/src/theme_settings_provider.rs:9-L24](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/theme_settings_provider.rs#L9-L24)：`ThemeSettingsProvider` trait，声明了 `ui_font`、`buffer_font`、`ui_font_size`、`buffer_font_size`、`ui_density` 五个查询方法。doc 注释说明它的意义：让 theme 相关查询**不耦合具体的设置基础设施**。
- [crates/theme/src/theme_settings_provider.rs:41-L43](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme/src/theme_settings_provider.rs#L41-L43)：`theme_settings(cx)` 的实现——从 gpui 全局状态取出装箱的 trait 对象。doc 注释明确警告：**若 provider 未注册则 panic**。
- [crates/theme_settings/src/theme_settings.rs:43-L65](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme_settings/src/theme_settings.rs#L43-L65)：具体实现 `ThemeSettingsProviderImpl`：`ui_density` 直接返回全局 `ThemeSettings` 的字段（第 62-64 行），`ui_font_size` 则经过 `ThemeSettings::ui_font_size(cx)`（第 54-56 行）——它还会叠加运行时调整（如 `UiFontSize` 全局覆盖，见 4.3.3）。
- [crates/theme_settings/src/theme_settings.rs:71-L75](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme_settings/src/theme_settings.rs#L71-L75)：`theme_settings::init` 在应用初始化时调用 `set_theme_settings_provider` 完成注册——这就是「未注册会 panic」的安全前提：真实应用总是先 init 再渲染 UI。
- [crates/theme_settings/src/settings.rs:383-L391](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme_settings/src/settings.rs#L383-L391)（`ui_font_size` 方法）与 [crates/theme_settings/src/settings.rs:40-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme_settings/src/settings.rs#L40-L44)（字段定义）：字段 doc 注释一锤定音——「UI 字号决定 UI 文字大小，**也决定一个 `gpui::Rems` 单位的大小**；改它会波及所有 UI 元素的尺寸」。

#### 4.2.4 代码实践

1. **实践目标**：亲手走通一条调用链，验证「注册 → 查询」闭环。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "set_theme_settings_provider" crates/ --include="*.rs"`，确认唯一的注册点在 `theme_settings::init`；
   - 再执行 `grep -rn "theme_settings::init" crates/zed/src --include="*.rs"`，找到应用入口的调用位置（**待确认**：具体行号请以本地搜索结果为准）；
   - 最后 `grep -rn "theme_settings(cx)" crates/ui/src/styles/spacing.rs`，确认宏生成代码里的查询路径。
3. **需要观察的现象**：三处 grep 分别命中「注册者」「初始化调用者」「查询者」。
4. **预期结果**：形成完整链路 `zed 启动 → theme_settings::init → set_theme_settings_provider → 渲染期 theme_settings(cx) 查询`。全程只读操作，可直接执行。

#### 4.2.5 小练习与答案

**练习 1**：`ui_macros` 的 Cargo.toml 并不依赖 `theme`，为什么生成代码里的 `::theme::theme_settings(cx)` 能编译并通过链接找到实现？

**答案**：宏只负责产出 token，名称解析与链接都发生在**调用方**（ui crate）。ui 依赖 theme，所以 `::theme::` 路径由 ui 解析；运行时取到的全局也是 ui 所链接的同一个应用里注册的那个。

**练习 2**：如果在没有调用过 `theme_settings::init` 的测试环境里渲染一个使用 `DynamicSpacing` 的组件，会发生什么？

**答案**：`theme_settings(cx)` 内部的 `cx.global::<GlobalThemeSettingsProvider>()` 找不到全局会 panic（trait 文档明确说明）。这也是相关测试需要先做主题/设置初始化的原因。

---

### 4.3 三个方法：spacing_ratio、rems 与 px

#### 4.3.1 概念说明

宏为 `DynamicSpacing` 生成了三个方法，构成一条两级流水线：

- `spacing_ratio(cx)`（**私有**）：按当前密度选出像素值，除以 `BASE_REM_SIZE_IN_PX`（16.0），得到 **rem 比例**。它是「密度」发生作用的地方。
- `rems(cx)`（公有）：把比例包成 `Rems`，**延迟**到布局期才换算成像素。
- `px(cx)`（公有）：立刻用 **UI 字号**把比例换算成 `Pixels`，**立即**得到确定像素值。

`BASE_REM_SIZE_IN_PX = 16.0` 的作用是**单位归一化**：设计稿里的像素值（以 16px 字号为基准标注）被转换成无量纲的 rem 比例，之后无论用户把字号调到多少，间距都按比例缩放。

#### 4.3.2 核心流程与数学

对变体 `BaseXX`，设三档像素值为 \(p_{\text{compact}}, p_{\text{default}}, p_{\text{comfortable}}\)（Single 输入时分别为 \(\max(n-4,0),\ n,\ n+4\)），则：

\[ \text{spacing\_ratio} = \frac{p_{\text{density}}}{16} \]

两条消费路径：

\[ \text{rems 路径（布局期）}: \quad \text{最终像素} = \text{spacing\_ratio} \times \underbrace{\text{window.rem\_size()}}_{=\ \text{UI 字号}} \]

\[ \text{px 路径（调用期）}: \quad \text{最终像素} = \text{spacing\_ratio} \times \text{ui\_font\_size} \]

两条公式在数学上等价（4.3.3 会证明 `window.rem_size()` 就是 UI 字号），区别只在**何时**换算、返回**什么类型**。

以 `Base16`（来自 Tuple `(14, 16, 18)`）为例：

| 密度 | 像素值 | rem 比例 | UI 字号 16px 时 | UI 字号 20px 时 |
| --- | --- | --- | --- | --- |
| Compact | 14 | \(14/16 = 0.875\) | 14px | 17.5px |
| Default | 16 | \(16/16 = 1.0\) | 16px | 20px |
| Comfortable | 18 | \(18/16 = 1.125\) | 18px | 22.5px |

伪代码：

```
fn rems(self, cx):
    ratio = spacing_ratio(self, cx)     # 选档 + 除以 16
    return Rems(ratio)                  # 换算推迟到布局

fn px(self, cx):
    ratio = spacing_ratio(self, cx)
    font = theme_settings(cx).ui_font_size(cx)
    return Pixels(font * ratio)         # 换算发生在当下
```

#### 4.3.3 源码精读

**（a）宏模板里的三个方法**（这是上一讲 quote! 模板的运行时部分）：

- [crates/ui_macros/src/dynamic_spacing.rs:144-L163](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L144-L163)：生成的 `impl DynamicSpacing` 块。
- [crates/ui_macros/src/dynamic_spacing.rs:146-L151](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L146-L151)：`spacing_ratio` 方法体。第 147 行声明 `const BASE_REM_SIZE_IN_PX: f32 = 16.0;`，第 148-150 行是逐变体 match。
- [crates/ui_macros/src/dynamic_spacing.rs:64-L86](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L64-L86)：match 分支的生成源头——Single 形态用 \((n-4).max(0) \mid n \mid (n+4)\)（第 69-71 行），Tuple 形态直接用 \(a \mid b \mid c\)（第 81-83 行），每档都除以 `BASE_REM_SIZE_IN_PX`。密度判断就是对 `::theme::theme_settings(cx).ui_density(cx)` 的 match。
- [crates/ui_macros/src/dynamic_spacing.rs:153-L156](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L153-L156)：`rems` 方法——调用 gpui 的 `rems()` 构造函数包一层 `Rems`。
- [crates/ui_macros/src/dynamic_spacing.rs:158-L162](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui_macros/src/dynamic_spacing.rs#L158-L162)：`px` 方法——第 160 行把 `theme_settings(cx).ui_font_size(cx)` 转成 `f32`，第 161 行乘上比例得 `Pixels`。

**（b）gpui 侧的类型与构造函数**：

- [crates/gpui/src/geometry.rs:3238-L3246](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/geometry.rs#L3238-L3246)：`Rems(pub f32)` 与 `to_pixels(rem_size)`——实现就是 `self * rem_size`，即乘法换算。
- [crates/gpui/src/geometry.rs:3723](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/geometry.rs#L3723) / [crates/gpui/src/geometry.rs:3736](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/geometry.rs#L3736)：`rems(f32) -> Rems` 与 `px(f32) -> Pixels` 两个构造函数——生成代码里裸名 `rems(...)`、`px(...)` 正是由调用方 spacing.rs 顶部的 `use gpui::{..., px, rems}` 导入（[crates/ui/src/styles/spacing.rs:1](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L1)）。
- [crates/gpui/src/geometry.rs:3496-L3503](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/geometry.rs#L3496-L3503)：`DefiniteLength::to_pixels`——rem 分支最终调 `rems.to_pixels(rem_size)`，这就是样式值在布局期的统一出口。

**（c）「UI 字号 = rem 基准」的证据链**：

- [crates/theme_settings/src/settings.rs:587-L596](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/theme_settings/src/settings.rs#L587-L596)：`setup_ui_font` 第 594 行调用 `window.set_rem_size(ui_font_size)`——**把 UI 字号设置为窗口的 rem 基准**，这是整条等价性的锚点。
- [crates/gpui/src/window.rs:2654-L2656](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L2654-L2656)：`set_rem_size` 的定义；[crates/gpui/src/window.rs:2645-L2650](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L2645-L2650)：`rem_size()` 读取（支持 `with_rem_size` 局部覆盖，用于子树内临时改变基准）。未设置时的兜底默认值是 `px(16.)`（[crates/gpui/src/window.rs:1839](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L1839)）。
- [crates/gpui/src/elements/div.rs:2341-L2342](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/elements/div.rs#L2341-L2342)：div 布局期 `let rem_size = window.rem_size(); let padding = style.padding.to_pixels(..., rem_size);`——`.px()` 样式里存的 rem 值正是在这里被换算成像素。

**（d）为什么 `px()` 用 `ui_font_size` 而不是 16？**

因为「16」只是**设计基准**（`BASE_REM_SIZE_IN_PX`，用于把设计像素折算成比例），而**运行基准**是 `window.rem_size()` = 用户当前 UI 字号。`px()` 的语义是「立刻给出与布局期 rem 换算一致的确切像素」，所以必须用运行基准：`ui_font_size × ratio`。若用 16，`px()` 的结果就永远固定，密度与字号缩放全部失效，还会与同一元素上 rem 路径算出的值互相矛盾。

顺带一提，spacing.rs 里还有一个语义相反的提醒：[crates/ui/src/styles/spacing.rs:46-L54](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L46-L54) 的 `ui_density(cx)` 辅助函数 doc 注释写着「**不要**用它计算间距值，间距一律用 `DynamicSpacing`」——它只服务于「按密度显示不同内容」的场景。

#### 4.3.4 代码实践

1. **实践目标**：不看代码手算 `DynamicSpacing` 的运行时值，并用 `Tab` 的真实代码交叉验证。
2. **操作步骤**：
   - 查调用清单（[crates/ui/src/styles/spacing.rs:29-L44](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/styles/spacing.rs#L29-L44)）确认 `Base32` 来自 Single 输入 `32`；
   - 手算：Compact 档 \(28/16 = 1.75\) rem、Default 档 \(32/16 = 2.0\) rem、Comfortable 档 \(36/16 = 2.25\) rem；
   - 分别在 UI 字号 16px 与 20px 下，按 `rems 路径`与 `px 路径`计算最终像素，验证两条路径结果一致；
   - 对照 [crates/ui/src/components/tab.rs:79-L85](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tab.rs#L79-L85)：`container_height` 返回 `DynamicSpacing::Base32.px(cx)`，即标签页高度在默认设置（16px 字号 + Default 密度）下应为 32px，`content_height` 为 \(32 - 1 = 31\)px（减 1px 给边框线）。
3. **需要观察的现象**：纸面计算与组件代码的语义互相印证。
4. **预期结果**：两张换算表数值一致；`rems` 与 `px` 两条路径对同一输入给出相同像素值。本实践为纯推导，可直接完成；若想看真实渲染数值，需在本地跑 GUI（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：UI 字号为 20px、密度为 Comfortable 时，`DynamicSpacing::Base32.rems(cx)` 最终是多少像素？

**答案**：Comfortable 档像素值 \(32 + 4 = 36\)，比例 \(36/16 = 2.25\) rem，最终 \(2.25 \times 20 = 45\)px。

**练习 2**：把 `BASE_REM_SIZE_IN_PX` 从 16.0 改成 8.0（仅思想实验），用户在默认设置下看到的间距会变吗？

**答案**：会变。换算基准（UI 字号，默认 16px）不受这个常数影响，而每个比例都会翻倍：以 `Base16` 为例，Default 档比例从 \(16/16 = 1.0\) 变成 \(16/8 = 2.0\)，最终像素从 \(1.0 \times 16 = 16\)px 变成 \(2.0 \times 16 = 32\)px——所有间距整体放大一倍。这说明 `BASE_REM_SIZE_IN_PX` 不是可以随便改的「内部常数」：它定义了「设计像素 ⇄ rem 比例」的折算率，必须与设计间距时假定的 16px 字号保持一致。

**练习 3**：为什么 `spacing_ratio` 是私有方法，而 `rems` / `px` 是公有的？

**答案**：`spacing_ratio` 返回的是无量纲比例，不是任何长度单位，直接暴露容易被人拿去乘错误的基准；公开 API 只保留两种带单位的形态，把「乘 16 还是乘字号」的决策封装在类型里（源码 doc 也标注 "should only be used internally"）。

---

### 4.4 组件中的典型用法：rems 与 px 的分工

#### 4.4.1 概念说明

真实组件里两条路径分工明确：

- **`.rems(cx)`：默认选择**。凡是能直接喂给样式方法（`.gap()`、`.px()`、`.py()` 等）的场合，传 `Rems` 即可——换算推迟到布局期，由 gpui 统一处理，还能享受 `with_rem_size` 局部覆盖等机制。
- **`.px(cx)`：需要确切像素的场合**。做算术（减 1px 边框）、返回 `Pixels` 类型的工具函数（如高度计算）、或传给只收像素的 API 时，必须先落成像素。

一个容易困惑的细节：div 的 `.px(...)` **样式方法**接受任何可转为 `DefiniteLength` 的值，所以 `.px(DynamicSpacing::Base08.rems(cx))`（传 rem）和 `.px(DynamicSpacing::Base04.px(cx))`（传像素）都能编译——前者延迟换算，后者立即换算，最终像素相同。这些样式方法由 `gpui_macros::padding_style_methods!` 生成，签名是 `fn px(mut self, length: impl Clone + Into<DefiniteLength>)`（见 [crates/gpui_macros/src/styles.rs:665-L697](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macros/src/styles.rs#L665-L697)，前缀 `px` 定义在 [crates/gpui_macros/src/styles.rs:776-L781](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macros/src/styles.rs#L776-L781)，挂载点在 [crates/gpui/src/styled.rs:26-L34](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/styled.rs#L26-L34)）。顺带澄清：`button_like.rs` 里的 `this.px_px()` **不是**「传像素」的版本，而是前缀 `px` + 后缀 `px` 生成的预定义快捷方式，语义是固定 1px 内边距（后缀表见 [crates/gpui_macros/src/styles.rs:1083-L1087](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macros/src/styles.rs#L1083-L1087)）。

#### 4.4.2 核心流程

```
组件 render(self, window, cx)
  ├─ 需要给样式方法喂长度 ──────────► DynamicSpacing::BaseXX.rems(cx)  → Rems → 布局期换算
  └─ 需要确切 Pixels（算术/工具函数） ► DynamicSpacing::BaseXX.px(cx)   → Pixels → 立即可用
```

#### 4.4.3 源码精读

- [crates/ui/src/components/button/button.rs:465](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button.rs#L465)：按钮图标与文字之间的 `.gap(DynamicSpacing::Base04.rems(cx))`——最典型的 rems 用法（Tuple `(2, 4, 6)`：紧凑 2px、默认 4px、宽松 6px）。
- [crates/ui/src/components/button/button.rs:493](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button.rs#L493)：带键位提示的按钮用 `.gap(DynamicSpacing::Base06.rems(cx))` 拉开更大间距。
- [crates/ui/src/components/button/button_like.rs:797-L804](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button_like.rs#L797-L804)：同一个元素上三种写法并列——第 797 行 `.gap(...rems(cx))`；第 798-802 行按按钮尺寸把 **rem 值**传给 `.px()` 样式方法（`Base08` / `Base04`）；第 803 行 `ButtonSize::None` 用 `px_px()`（固定 1px）。这一段是理解「`.px()` 方法 vs `px()` 函数」的最佳标本。
- [crates/ui/src/components/tab.rs:79-L85](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tab.rs#L79-L85)：`content_height` / `container_height` 两个工具函数返回 `Pixels`，且 `content_height` 要做减法 `Base32.px(cx) - px(1.)`——必须用 `px(cx)` 路径。
- [crates/ui/src/components/tab.rs:172-L174](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tab.rs#L172-L174)：标签内容区第 173 行 `.px(DynamicSpacing::Base04.px(cx))`（传像素）、第 174 行 `.gap(DynamicSpacing::Base04.rems(cx))`（传 rem）——同一处两种风格并存，效果等价。

#### 4.4.4 代码实践

1. **实践目标**：总结 `rems` / `px` 的分工规律，并亲手完成一次「硬编码 → 密度感知」替换。
2. **操作步骤**：
   - 阅读 [button.rs:465](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button.rs#L465)、[button_like.rs:797-L804](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/button/button_like.rs#L797-L804)、[tab.rs:79-L85](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tab.rs#L79-L85) 与 [tab.rs:172-L174](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tab.rs#L172-L174)，填出下面这张表：

     | 位置 | 用的是 rems 还是 px | 为什么 |
     | --- | --- | --- |
     | button.rs `.gap(Base04...)` |  |  |
     | button_like.rs `.px(Base08...)` |  |  |
     | tab.rs `content_height` |  |  |
     | tab.rs `.px(Base04...)` |  |  |

   - 然后在任意练习组件（或 component_preview 里的示例组件）中，找到一处硬编码间距如 `.gap(px(16.))`，替换为 `.gap(DynamicSpacing::Base16.rems(cx))`，运行 `cargo check -p ui` 确认编译通过；
   - 手算替换后的行为并记录：默认设置（16px + Default）仍是 16px，Compact 档变 14px，Comfortable 档变 18px；把 UI 字号调到 20px 后分别变 17.5 / 20 / 22.5px。
3. **需要观察的现象**：编译通过；本地运行 Zed 并切换 `"unstable.ui_density"` 与 `"ui_font_size"` 时，替换处的间距随之变化，而未替换的 `px(16.)` 处纹丝不动。
4. **预期结果**：表格规律——「喂样式方法用 rems，做算术/返回 Pixels 用 px」；替换处随设置缩放。渲染效果需 GUI，**待本地验证**；`cargo check -p ui` 可在本地直接验证编译。

#### 4.4.5 小练习与答案

**练习 1**：`tab.rs` 的 `content_height` 为什么不能用 `rems(cx)`？

**答案**：函数签名返回 `Pixels`，且函数体要做 `Base32.px(cx) - px(1.)` 的像素减法；`Rems` 既不能直接返回也不能与 `Pixels` 相减（除非先手动乘基准），用 `px(cx)` 一步到位。

**练习 2**：`.px(DynamicSpacing::Base04.rems(cx))` 和 `.px(DynamicSpacing::Base04.px(cx))` 最终渲染结果相同吗？

**答案**：相同。前者把 `Rems` 存进样式，布局期由 `window.rem_size()`（= UI 字号）换算；后者当场用 UI 字号换算成 `Pixels` 存进样式。两者换算基准一致，结果一致；差别只在换算时机与是否参与 `with_rem_size` 覆盖等布局期机制。

**练习 3**：如果某组件需要「间距不随字号缩放、只随密度变化」，`DynamicSpacing` 还适用吗？

**答案**：不适用。`DynamicSpacing` 的两条路径都以 UI 字号为缩放基准（这正是「随用户 rem 设置缩放」的设计目标）。这种需求需要另用 `ui_density(cx)` 判断档位后手动给固定像素——spacing.rs 第 46-54 行的 doc 注释恰好说明该辅助函数面向这类「非间距」场景。

## 5. 综合实践

**任务：给一个练习组件做「密度感知化」改造并验证换算。**

1. 在 `component_preview` 示例或你自己的练习组件中，写一个包含三个 `div` 的纵向布局，初始代码全部使用硬编码：`.gap(px(4.))`、`.px(px(8.))`、`.h(px(32.))`。
2. 改造一：把 `.gap(px(4.))` 换成 `.gap(DynamicSpacing::Base04.rems(cx))`，把 `.px(px(8.))` 换成 `.px(DynamicSpacing::Base08.rems(cx))`。
3. 改造二：高度无法直接用样式方法表达算术时，仿照 `Tab::container_height`（[tab.rs:83-L85](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/ui/src/components/tab.rs#L83-L85)）写一个 `fn container_height(cx: &App) -> Pixels { DynamicSpacing::Base32.px(cx) }` 并在 `.h(...)` 中使用。
4. 手算填表：三处间距在「密度三档 × 字号 16/20」共六种组合下的预期像素值（利用 \(\text{ratio} = p/16\)、\(\text{px} = \text{ratio} \times \text{字号}\)）。
5. 本地运行 Zed，切换 `"unstable.ui_density": "compact" | "default" | "comfortable"` 与 `"ui_font_size": 16 | 20`，逐格核对实测值与手算值。
6. （可选，纯本地实验）在 `.rems` 与 `.px` 两条路径各留一处等值间距，确认任何设置组合下两者都相同——验证 4.3 的等价性结论。实验后还原所有改动，不要提交。

渲染部分需图形界面，**待本地验证**；第 4 步手算与第 2、3 步的 `cargo check -p ui` 可离线完成。

## 6. 本讲小结

- `spacing_ratio`（私有）按 `theme::theme_settings(cx).ui_density(cx)` 在三档里选像素值，再除以 `BASE_REM_SIZE_IN_PX = 16.0` 得到 rem 比例——16 是**设计基准**，负责「设计像素 ⇄ 无量纲比例」的折算。
- `rems(cx)` 返回 `Rems`，换算推迟到布局期，基准是 `window.rem_size()`；`px(cx)` 返回 `Pixels`，当场乘 `ui_font_size`。两条路径数学等价，因为 `setup_ui_font` 把窗口 rem 基准设置成了 UI 字号。
- 分工经验：喂给样式方法（`.gap()` / `.px()` 等）用 `rems(cx)`；做像素算术或需要 `Pixels` 类型时用 `px(cx)`。
- div 的 `.px(...)` 样式方法接受 `impl Into<DefiniteLength>`，rem 与像素都能传；`px_px()` 则是「固定 1px」的预定义快捷方式，别被名字误导。
- `UiDensity::spacing_ratio()`（0.75/1.0/1.25）是另一套未统一的全局缩放机制，`DynamicSpacing` 并不使用它；查密度做非间距用途时才用 `spacing.rs` 的 `ui_density(cx)` 辅助函数。

## 7. 下一步学习建议

本讲完成了 `derive_dynamic_spacing!` 从编译期到运行时的完整闭环。下一讲进入第二座大山：**u4-l1 RegisterComponent 生成代码拆解**——看派生宏如何用 `const _: () = { ... }` + `PhantomData` 在编译期断言 trait 实现、如何生成注册函数。如果你想在 DynamicSpacing 这条线上再深入，建议：

- 阅读 [crates/gpui/src/window.rs:2691-L2710](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L2691-L2710) 的 `with_rem_size`，理解子树内临时改变 rem 基准的机制；
- 对比 `gpui_macros::padding_style_methods!` 生成的 `.px_4()` 等预定义 rem 快捷方式（[crates/gpui_macros/src/styles.rs:926-L992](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macros/src/styles.rs#L926-L992)）与 `DynamicSpacing` 的关系——前者是固定 rem 档位、不感知密度，后者密度感知，这是 Zed 间距体系的新旧两代。
