# DynamicSpacing 与 UI 密度

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `DynamicSpacing` 解决什么问题：为什么 Zed 不允许组件把间距写死成像素值。
2. 读懂 `src/styles/spacing.rs` 里那张「间距表」，并掌握 `BaseXX` 命名与像素值、rem 之间的换算关系。
3. 理解 `derive_dynamic_spacing!` 过程宏如何从一张表生成整个枚举及其解析方法。
4. 分清 `rems(cx)` 与 `px(cx)` 两个出口的用途，以及它们都会随 UI 字体大小缩放这一事实。
5. 掌握 `ui_density()` 的正确使用边界：只用于「展示或非间距逻辑」，间距一律走 `DynamicSpacing`。
6. 把一段用 `px()` 硬编码 gap/padding 的布局改写为密度感知的写法。

## 2. 前置知识

### 2.1 什么是「UI 密度」

现代编辑器通常允许用户在「同样一块屏幕里塞多少内容」之间做选择。Zed 提供了三档实验性设置 `ui_density`：

| 档位 | 语义 | 设置文件中的取值 |
| --- | --- | --- |
| Compact | 更紧凑，间距更小，元素更密 | `"compact"` |
| Default | 默认密度 | `"default"` |
| Comfortable | 更宽松，间距更大 | `"comfortable"` |

对组件作者来说，这带来一个直接要求：**间距不能是编译期定死的像素值**。`.gap(px(4.))` 写出来的间距永远 4px，用户切到 Comfortable 也纹丝不动。`DynamicSpacing` 就是为此而生的「密度感知间距令牌」。

### 2.2 与前两讲的衔接

本讲是「语义键 + 渲染期解析」模式（见 u2-l1 语义颜色）的又一次出现：

- `Color` 枚举：组件构建期只存 `Color::Error` 这样的**语义键**，渲染期调用 `Color::color(cx)` 查主题得到真实 `Hsla`。
- `DynamicSpacing` 枚举：组件构建期只存 `DynamicSpacing::Base04` 这样的**尺寸语义键**，渲染期调用 `.rems(cx)` / `.px(cx)` 查当前设置得到真实尺寸。

同时要 recall u2-l2 的 rem 机制：Zed 窗口的 rem 基准被设为 UI 字体大小（`ui_font_size`），所以一切以 rem 表达的尺寸都会随用户调大 UI 字体而整体放大。本讲的间距也不例外。

### 2.3 一个容易混淆的点：`px()` 函数 vs `.px()` 方法

- `px(12.)` 是 gpui 的**函数**，构造一个固定 12 像素的 `Pixels` 值（经 prelude 导入，见 u1-l2）。
- `.px(x)` 是 `Styled` trait 的**样式方法**，设置水平内边距，参数是 `impl Into<Length>`。

`DynamicSpacing::Base12.rems(cx)` 返回的 `Rems` 能直接喂给 `.px(...)`、`.gap(...)` 等样式方法——这是本讲改写实践的核心操作。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/styles/spacing.rs` | 本讲主角（仅 55 行）：一张间距表 + `ui_density()` 辅助函数。整个 `DynamicSpacing` 枚举由这里的宏调用生成 |
| `crates/ui_macros/src/dynamic_spacing.rs` | `derive_dynamic_spacing!` 过程宏的实现：解析输入表，生成枚举、文档注释和解析方法 |
| `crates/ui_macros/src/ui_macros.rs` | ui_macros crate 的宏入口，转发到上面的实现 |
| `crates/theme/src/ui_density.rs` | `UiDensity` 枚举定义（Compact/Default/Comfortable），属于 theme crate |
| `crates/theme/src/theme_settings_provider.rs` | `ThemeSettingsProvider` trait：theme crate 读取用户设置的桥，`theme_settings(cx)` 全局访问点 |
| `crates/theme_settings/src/settings.rs` | 用户设置中的 `ui_density` 字段定义与解析 |
| `src/styles/units.rs` | `BASE_REM_SIZE_IN_PX = 16.0` 常量与 `rems_from_px()`（u2-l2 已讲，本讲作对照） |
| `src/components/button/button_like.rs`、`src/components/modal.rs`、`src/components/tab.rs`、`src/components/icon.rs` | `DynamicSpacing` 在 ui crate 内的真实使用现场 |

`DynamicSpacing` 已进入 prelude（[src/prelude.rs:L15](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L15) 显式导出 `pub use crate::DynamicSpacing;`），所以下游只需 `use ui::prelude::*` 即可直接写 `DynamicSpacing::Base04`。

## 4. 核心概念与源码讲解

### 4.1 UI 密度从哪里来：UiDensity 与 theme_settings

#### 4.1.1 概念说明

`DynamicSpacing` 在渲染期要回答「当前是哪档密度」，答案来自用户设置。但这里有一条精心的分层：

- **theme crate** 定义密度「是什么」（`UiDensity` 枚举），但不知道用户的设置存在哪。
- **theme_settings crate** 拥有具体设置（settings.json），在应用启动时把一个实现了 `ThemeSettingsProvider` 的对象注册为 gpui 全局。
- **ui crate** 只通过 `theme::theme_settings(cx)` 这个全局入口取值，与具体设置基础设施完全解耦。

这正是 u1-l1 讲过的「theme 供主题令牌」分工的延伸：ui 不依赖任何业务设置代码。

#### 4.1.2 核心流程

从 settings.json 到组件拿到密度值的完整链路：

```text
settings.json 里 "ui_density": "comfortable"
        │
        ▼
theme_settings crate: ThemeSettingsContent 解析（serde snake_case，
                      别名 "compact"/"default"/"comfortable"）
        │  ui_density_from_settings() 转换
        ▼
ThemeSettings::get_global(cx).ui_density        （theme_settings 内部）
        │  应用启动时经 set_theme_settings_provider() 注册为全局 provider
        ▼
theme::theme_settings(cx) -> &dyn ThemeSettingsProvider
        │  .ui_density(cx)
        ▼
UiDensity::Comfortable
        │
        ├──► ui::ui_density(cx)          （给「非间距」逻辑用）
        └──► DynamicSpacing 的解析方法内部 （给间距用）
```

#### 4.1.3 源码精读

**密度枚举本体**。[crates/theme/src/ui_density.rs:L21-L32](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/ui_density.rs#L21-L32) 定义了三档密度，`Default` 变体带 `#[default]`，serde 用 snake_case 序列化并给每个变体配了字符串别名：

```rust
#[serde(rename_all = "snake_case")]
pub enum UiDensity {
    /// A denser UI with tighter spacing and smaller elements.
    #[serde(alias = "compact")]
    Compact,
    #[default]
    #[serde(alias = "default")]
    /// The default UI density.
    Default,
    #[serde(alias = "comfortable")]
    /// A looser UI with more spacing and larger elements.
    Comfortable,
}
```

注意源码注释标明这是**实验性设置**，跟踪 issue 为 zed-industries/zed#18078。

**设置读取桥**。[crates/theme/src/theme_settings_provider.rs:L9-L24](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/theme_settings_provider.rs#L9-L24) 的 trait 声明了 theme 需要的最小接口（UI 字体、buffer 字体、字号、密度），[L41-L43](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme/src/theme_settings_provider.rs#L41-L43) 的 `theme_settings(cx)` 从 gpui 全局取出 provider：

```rust
pub fn theme_settings(cx: &App) -> &dyn ThemeSettingsProvider {
    &*cx.global::<GlobalThemeSettingsProvider>().0
}
```

**设置侧的实现**。[crates/theme_settings/src/settings.rs:L94-L96](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme_settings/src/settings.rs#L94-L96) 声明设置字段 `pub ui_density: UiDensity`；[crates/theme_settings/src/theme_settings.rs:L62-L63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/theme_settings/src/theme_settings.rs#L62-L63) 的 provider 实现直接读全局设置：

```rust
fn ui_density(&self, cx: &App) -> UiDensity {
    ThemeSettings::get_global(cx).ui_density
}
```

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手走通「设置 → provider → ui」这条链，确认 ui crate 取密度时不接触任何 settings 类型。
2. **操作步骤**：
   - 从 `src/styles/spacing.rs` 的 `ui_density()` 出发，跳进 `theme::theme_settings`；
   - 再用 IDE 的「查找实现」找到 `ThemeSettingsProvider for ThemeSettings`（即 theme_settings.rs 第 62 行附近）；
   - 最后看 settings.rs 中 `ui_density` 字段如何从 settings.json 内容解析（`content.ui_density.unwrap_or_default()`）。
3. **需要观察的现象**：整条链上 ui crate 只出现 `theme::` 前缀，没有 `theme_settings::` 或 `settings::` 类型。
4. **预期结果**：画出 4.1.2 的链路图；结论是「换一套设置存储（例如远程配置）不影响 ui crate」。
5. 想在真实 Zed 里验证三档密度：在 `settings.json` 写 `"ui_density": "compact"` 后重启观察界面（实验性设置，入口可能随版本变化）——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`UiDensity` 为什么定义在 theme crate 而不是 ui crate？

**答案**：因为密度是「用户主题类设置」的一部分，由 theme_settings crate 持有并通过 `ThemeSettingsProvider` 暴露；theme crate 定义类型可以让 ui 与具体设置基础设施解耦（ui 只依赖 theme）。若放在 ui，theme 的 provider 接口就得反过来依赖 ui，造成不合理的依赖方向。

**练习 2**：`theme::theme_settings(cx)` 在没有注册 provider 时会发生什么？

**答案**：会 panic。其文档明确写着 "Panics if no provider has been registered"，因为它直接 `cx.global::<GlobalThemeSettingsProvider>()`。所以 provider 必须在应用初始化时由 `set_theme_settings_provider` 注册（theme_settings crate 负责这件事）。

---

### 4.2 `derive_dynamic_spacing!` 宏：一张表如何变成一个枚举

#### 4.2.1 概念说明

`src/styles/spacing.rs` 的核心只有一次宏调用——一张 14 行的「间距表」。`DynamicSpacing` 枚举的全部变体、每个变体的文档注释、以及按密度解析数值的方法，都由 `ui_macros` crate 的 `derive_dynamic_spacing!` **函数式过程宏**在编译期生成。

为什么用「一张表 + 宏」而不是手写枚举？

- **单一事实来源**：间距档位是设计决策，集中在一张表里维护，新增一档只需加一行。
- **闭合的取值集合**：生成的是枚举而非函数，编译器保证只能使用「被祝福过」的档位，杜绝随手写 `spacing(13)` 这类破坏节奏的值。
- **文档与代码同源**：每个变体的文档注释（三档像素值）由宏按表自动算出，永远不会与实现脱节。

注意命名陷阱：宏名以 `derive_` 开头，但它**不是 derive 宏**，而是**函数式（bang）过程宏**——它不依附于任何已有的结构体，而是凭空生成一个新枚举。

#### 4.2.2 核心流程

宏输入是一个逗号分隔的列表，每项两种形态：

- **三元组 `(a, b, c)`**：直接给出三档密度的像素值，即 `Compact: a px, Default: b px, Comfortable: c px`。
- **单值 `n`**：用标准公式推导三档：

\[
\text{Compact} = \max(n - 4,\ 0), \qquad \text{Default} = n, \qquad \text{Comfortable} = n + 4
\]

（间距.rs 源码注释里的例子：标准公式下 `24 => Compact: 20px, Default: 24px, Comfortable: 28px`。）

无论哪种形态，**变体名都取 Default 档的值**，格式为 `Base{:02}`（补零到两位）。宏为每个变体生成：

1. 枚举变体 `BaseXX`；
2. 文档注释，格式为 `` `Xpx`|`Ypx`|`Zpx (@16px/rem)` - Scales with the user's rem size. ``；
3. `spacing_ratio` 私有方法中对应的 match 分支：把三档像素值分别除以 16（`BASE_REM_SIZE_IN_PX`），得到以 rem 为单位的比率。

#### 4.2.3 源码精读

**那张表**。[src/styles/spacing.rs:L29-L44](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/spacing.rs#L29-L44) 是 `DynamicSpacing` 的唯一事实来源：

```rust
derive_dynamic_spacing![
    (0, 0, 0),
    (1, 1, 2),
    (1, 2, 4),
    (2, 3, 4),
    (2, 4, 6),
    (3, 6, 8),
    (4, 8, 10),
    (10, 12, 14),
    (14, 16, 18),
    (18, 20, 22),
    24,
    32,
    40,
    48
];
```

表前的注释块（[L5-L28](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/spacing.rs#L5-L28)）解释了两种输入形态与 `BaseXX` 命名含义：`XX = 默认 rem 尺寸、默认密度下的像素值`。

**宏入口**。[crates/ui_macros/src/ui_macros.rs:L6-L10](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/ui_macros.rs#L6-L10) 声明 `#[proc_macro] pub fn derive_dynamic_spacing`，转发给实现模块。

**输入解析**。[crates/ui_macros/src/dynamic_spacing.rs:L18-L46](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L18-L46) 用 syn 定义了两种输入形态：看到括号就解析成 `Tuple(a, b, c)`，否则是 `Single(n)`。

**代码生成**。[crates/ui_macros/src/dynamic_spacing.rs:L49-L89](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L49-L89) 是核心。变体名由 Default 档的值补零而来（[L56-L63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L56-L63)）：

```rust
let variant = match v {
    DynamicSpacingValue::Single(n) => {
        format_ident!("Base{:02}", n.base10_parse::<u32>().unwrap())
    }
    DynamicSpacingValue::Tuple(_, b, _) => {
        format_ident!("Base{:02}", b.base10_parse::<u32>().unwrap())
    }
};
```

单值形态套用标准公式（注意 `max(0.0)` 防止小值减出负数），三元组直接取值，两者最终都除以 16 得到 rem 比率（[L64-L87](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L64-L87)）：

```rust
DynamicSpacingValue::Single(n) => {
    let n = n.base10_parse::<f32>().unwrap();
    quote! {
        DynamicSpacing::#variant => match ::theme::theme_settings(cx).ui_density(cx) {
            ::theme::UiDensity::Compact => (#n - 4.0).max(0.0) / BASE_REM_SIZE_IN_PX,
            ::theme::UiDensity::Default => #n / BASE_REM_SIZE_IN_PX,
            ::theme::UiDensity::Comfortable => (#n + 4.0) / BASE_REM_SIZE_IN_PX,
        }
    }
}
```

**文档注释生成**。[L103-L122](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L103-L122) 按同一张表为每个变体算出三档像素值，拼成 `` `2px`|`4px`|`6px (@16px/rem)` `` 风格的 doc string——你在 IDE 里悬停 `DynamicSpacing::Base04` 看到的提示就是这里生成的。

**展开产物骨架**。[L127-L164](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L127-L164) 用 `quote!` 拼出最终代码：`pub enum DynamicSpacing { ... }` 加一个 `impl DynamicSpacing` 块，内含私有的 `spacing_ratio` 与公开的 `rems` / `px`（下一节精读）。一个小细节：宏在 `spacing_ratio` 内部**自带** `const BASE_REM_SIZE_IN_PX: f32 = 16.0;`（[L146-L148](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L146-L148)），与 [src/styles/units.rs:L3-L4](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/units.rs#L3-L4) 的同名常量数值一致但互不引用——因为 ui_macros 不能依赖 ui（会循环依赖），只能各自持有这份数值。

#### 4.2.4 代码实践（源码阅读型 + 手算）

1. **实践目标**：不运行任何代码，徒手「执行」一遍宏，验证你真的读懂了生成规则。
2. **操作步骤**：
   - 把 spacing.rs 表中 14 行逐行换算，填出下面的完整对照表（答案见 4.3.3 的表，先自己算再对答案）；
   - 回答：若在表里**新增**一行 `40`（单值），会发生什么？
3. **需要观察的现象**：小值区间（0~10px）全部用了手工三元组，单值公式只出现在 24 及以上。
4. **预期结果**：小值时 `n - 4` 要么补零要么差值失衡——例如 `2` 套公式会得到 `(0, 2, 6)`，Compact 与 Comfortable 相差 6px 太夸张；手工表把 `Base02` 定为 `(1, 2, 4)`，让小间距在密度切换时变化更克制。而 `24` 以上 `±4` 的绝对差在视觉上恰好合适，直接用公式。新增单值 `40` 会生成 `Base40`——与表中已有 `(36, 40, 44)` 生成的 `Base40` **重名，编译报错**（枚举变体重复定义）。
5. 本实践无需运行验证，结论可由宏源码直接推出。

#### 4.2.5 小练习与答案

**练习 1**：写出输入行 `(6, 10, 14)` 会生成的变体名和文档注释。

**答案**：变体名取 Default 档（中间值 10），补零两位 → `Base10`。文档注释为 `` `6px`|`10px`|`14px (@16px/rem)` - Scales with the user's rem size. ``

**练习 2**：`derive_dynamic_spacing!` 是 derive 宏吗？如何一眼区分？

**答案**：不是。它是函数式过程宏（`#[proc_macro]`，调用形如 `derive_dynamic_spacing![...]`），凭空生成枚举；derive 宏（`#[proc_macro_derive]`，如 `RegisterComponent`）必须附着在已有类型定义上为其追加 impl。

**练习 3**：为什么宏生成枚举而不是一个 `fn spacing(px: f32, cx: &App) -> Rems` 函数？

**答案**：枚举把可选间距收敛为闭合集合，编译期即拒绝任意值，保证全应用间距节奏一致；且枚举值 `Copy + Hash + Ord`，可作为配置存进组件字段、参与匹配。函数则接受任意浮点，等于没约束。

---

### 4.3 `DynamicSpacing::BaseXX`：命名、换算与 `rems()` / `px()` 两个出口

#### 4.3.1 概念说明

宏生成的完整变体列表（14 个）及其在三档密度下的像素值如下（按「默认 rem 基准 16px、默认密度」标注）：

| 变体 | 表输入 | Compact | Default | Comfortable |
| --- | --- | --- | --- | --- |
| `Base00` | `(0, 0, 0)` | 0px | 0px | 0px |
| `Base01` | `(1, 1, 2)` | 1px | 1px | 2px |
| `Base02` | `(1, 2, 4)` | 1px | 2px | 4px |
| `Base03` | `(2, 3, 4)` | 2px | 3px | 4px |
| `Base04` | `(2, 4, 6)` | 2px | 4px | 6px |
| `Base06` | `(3, 6, 8)` | 3px | 6px | 8px |
| `Base08` | `(4, 8, 10)` | 4px | 8px | 10px |
| `Base12` | `(10, 12, 14)` | 10px | 12px | 14px |
| `Base16` | `(14, 16, 18)` | 14px | 16px | 18px |
| `Base20` | `(18, 20, 22)` | 18px | 20px | 22px |
| `Base24` | `24` | 20px | 24px | 28px |
| `Base32` | `32` | 28px | 32px | 36px |
| `Base40` | `40` | 36px | 40px | 44px |
| `Base48` | `48` | 44px | 48px | 52px |

读表方法：**`BaseXX` 中的 XX 就是 Default 密度下的像素值**；切密度时，小档位按手工表跳变、24 以上按 \( n \pm 4 \) 跳变。

解析出的「比率」定义是：

\[
\text{ratio} = \frac{\text{px}_{\text{当前密度}}}{16}
\]

这个比率以 rem 为单位。两个公开出口：

- **`rems(cx) -> Rems`**：返回 `rems(ratio)`，喂给 `.gap()` / `.px()` / `.p()` 等样式方法，由 gpui 在布局期按**窗口 rem 基准**（即 `ui_font_size`）解析成像素。
- **`px(cx) -> Pixels`**：直接算出 \( \text{ui\_font\_size}(cx) \times \text{ratio} \)，得到具体的 `Pixels` 数值，用于算术或非样式场景。

**关键认知（初学者最易搞错的一点）**：`px(cx)` 返回的**不是固定像素**！它同样乘上了当前 UI 字体大小。所谓 "Base04 = 4px" 只在「密度 Default 且 ui_font_size = 16px」时成立；把 UI 字体调到 20px 后，`Base04.px(cx)` 在 Default 密度下是 \( 20 \times \tfrac{4}{16} = 5 \)px。两个出口随密度**和**字体双重缩放，只是「何时解析成像素」不同。

#### 4.3.2 核心流程

一次间距解析的时序：

```text
组件构建期（无 cx 也能存）:
    Button { label, .. }                     // 只存语义键，如需要间距时存 DynamicSpacing::Base04

渲染期 render(self, window, cx):
    DynamicSpacing::Base04.rems(cx)
        │  spacing_ratio(cx): 查 theme_settings(cx).ui_density(cx)
        │      Compact -> 2/16 = 0.125
        │      Default -> 4/16 = 0.25
        │      Comfortable -> 6/16 = 0.375
        ▼
    rems(0.25)  ──►  .gap(...) 样式方法
        │  布局期: Rems × 窗口 rem 基准(= ui_font_size)
        ▼
    16px × 0.25 = 4px   （ui_font_size = 16 时）
```

与 `Color::color(cx)`（u2-l1）完全同构：**构建期存键，渲染期查设置，布局期落地成具体值**。用户改密度或字号后，下一帧重新渲染即整体生效，组件无需重建。

#### 4.3.3 源码精读

**生成的方法体**。[crates/ui_macros/src/dynamic_spacing.rs:L144-L163](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui_macros/src/dynamic_spacing.rs#L144-L163) 是宏生成的 `impl DynamicSpacing` 全貌：

```rust
impl DynamicSpacing {
    /// Returns the spacing ratio, should only be used internally.
    fn spacing_ratio(&self, cx: &App) -> f32 {
        const BASE_REM_SIZE_IN_PX: f32 = 16.0;
        match self {
            // 每个变体一个分支，按密度返回 px / 16
        }
    }

    /// Returns the spacing value in rems.
    pub fn rems(&self, cx: &App) -> Rems {
        rems(self.spacing_ratio(cx))
    }

    /// Returns the spacing value in pixels.
    pub fn px(&self, cx: &App) -> Pixels {
        let ui_font_size_f32: f32 = ::theme::theme_settings(cx).ui_font_size(cx).into();
        px(ui_font_size_f32 * self.spacing_ratio(cx))
    }
}
```

三个方法层层递进：`spacing_ratio` 查密度得 rem 比率；`rems` 原样包装；`px` 显式乘上 `ui_font_size`——这就是 4.3.1 说的「`px()` 也随字体缩放」的直接证据。

**使用现场一：Modal 的内边距**。[src/components/modal.rs:L174-L177](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/modal.rs#L174-L177) 用三个不同档位组合出一组内边距与间距：

```rust
.px(DynamicSpacing::Base12.rems(cx))
.pt(DynamicSpacing::Base08.rems(cx))
.pb(DynamicSpacing::Base04.rems(cx))
.gap(DynamicSpacing::Base08.rems(cx))
```

**使用现场二：按钮按尺寸选间距**。[src/components/button/button_like.rs:L797-L804](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button_like.rs#L797-L804) 展示了「档位参与业务分支」——大/中按钮用 `Base08`，默认/紧凑按钮用 `Base04`：

```rust
.gap(DynamicSpacing::Base04.rems(cx))
.map(|this| match self.size {
    ButtonSize::Large | ButtonSize::Medium => this.px(DynamicSpacing::Base08.rems(cx)),
    ButtonSize::Default | ButtonSize::Compact => {
        this.px(DynamicSpacing::Base04.rems(cx))
    }
    ButtonSize::None => this.px_px(),
})
```

**使用现场三：`px()` 出口做像素算术**。[src/components/tab.rs:L79-L85](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/tab.rs#L79-L85) 需要的是「一个具体高度数值」而非样式，于是走 `px()` 并直接做减法：

```rust
pub fn content_height(cx: &App) -> Pixels {
    DynamicSpacing::Base32.px(cx) - px(1.)
}

pub fn container_height(cx: &App) -> Pixels {
    DynamicSpacing::Base32.px(cx)
}
```

**使用现场四：`rems()` 之间也能相加**。[src/components/modal.rs:L390](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/modal.rs#L390) 直接把两个 `Rems` 相加（`Rems` 实现了算术运算），等价于两倍 `Base06`：

```rust
this.px(DynamicSpacing::Base06.rems(cx) + DynamicSpacing::Base06.rems(cx))
```

**rem 落地为像素的证据**。[src/components/icon.rs:L86-L99](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/icon.rs#L86-L99) 中 `self.rems() * window.rem_size()` 一句显式展示了「Rems × 窗口 rem 基准 = Pixels」的解析动作；同一段还示范了用 `DynamicSpacing` 给图标方块加**内边距**（注意 `Custom` 分支留着 `TODO: Wire into dynamic spacing`，说明迁移仍在进行）：

```rust
let icon_size = self.rems() * window.rem_size();
let padding = match self {
    IconSize::Indicator => DynamicSpacing::Base00.px(cx),
    IconSize::XSmall => DynamicSpacing::Base02.px(cx),
    // ...
};
```

#### 4.3.4 代码实践：把硬编码间距改写为 DynamicSpacing

这是本讲的核心动手任务。

1. **实践目标**：把一段 `px()` 硬编码的布局改写成密度感知写法，并能在三档密度下说出每个间距的实际像素值。

2. **操作步骤**：

   先看这段「反面教材」（**示例代码**，风格模仿 modal.rs 改写前的样子）：

   ```rust
   // 示例代码：间距被写死，密度设置完全失效
   h_flex()
       .px(px(12.))
       .pt(px(8.))
       .pb(px(4.))
       .gap(px(8.))
       .child("项目名称")
   ```

   对照 spacing.rs 的表，逐个选出语义等价的档位（12→Base12、8→Base08、4→Base04），改写为：

   ```rust
   // 示例代码：改写后（需在能拿到 cx 的渲染上下文中，如 RenderOnce::render）
   h_flex()
       .px(DynamicSpacing::Base12.rems(cx))
       .pt(DynamicSpacing::Base08.rems(cx))
       .pb(DynamicSpacing::Base04.rems(cx))
       .gap(DynamicSpacing::Base08.rems(cx))
       .child("项目名称")
   ```

   验证编译：把改写后的片段临时放进任一组件的 `render` 方法或其 doc 示例中（学习时可随意，正式提交需遵循项目规范），在 `crates/ui` 目录运行：

   ```bash
   cargo check -p ui
   ```

3. **需要观察的现象 / 预期结果**：编译通过后，按 `ui_font_size = 16px` 填写对比表：

   | 调用 | Compact | Default | Comfortable |
   | --- | --- | --- | --- |
   | `px(12.)`（改写前，恒定） | 12px | 12px | 12px |
   | `Base12.rems(cx)` | 10px | 12px | 14px |
   | `Base08.rems(cx)` | 4px | 8px | 10px |
   | `Base04.rems(cx)` | 2px | 4px | 6px |

   再进一步推演：把 UI 字体调到 20px 且保持 Default 密度，`Base12` 变为 \( 20 \times \tfrac{12}{16} = 15 \)px，而 `px(12.)` 依旧是 12px——这正是「间距随字号整体缩放」的设计意图。

4. **运行观察（可选）**：在 Zed 的 `settings.json` 中分别设置 `"ui_density": "compact"` / `"default"` / `"comfortable"` 并重启，观察任一弹窗（Modal）内边距与按钮间距的变化——**待本地验证**（实验性设置，且本环境未运行 GUI）。

#### 4.3.5 小练习与答案

**练习 1**：`DynamicSpacing::Base16.rems(cx)` 在 Comfortable 密度、`ui_font_size = 16px` 下是多少像素？`ui_font_size = 20px` 下呢？

**答案**：Comfortable 档 Base16 = 18px，比率 \( 18/16 = 1.125 \) rem。`ui_font_size = 16px` 时即 18px；`ui_font_size = 20px` 时为 \( 20 \times 1.125 = 22.5 \)px。

**练习 2**：什么时候应该用 `.rems(cx)`，什么时候必须用 `.px(cx)`？

**答案**：设置元素样式（`gap`/`p`/`px`/`pt`/`w`/`h` 等接受 `impl Into<Length>` 的方法）优先 `rems(cx)`，让 gpui 在布局期按窗口 rem 基准解析；需要一个**具体 `Pixels` 数值**参与算术、比较或传给不接受 `Length` 的接口时用 `px(cx)`，如 `Tab::content_height` 里的 `Base32.px(cx) - px(1.)`。

**练习 3**：有人想表达「双倍 Base04 的间距」。直接写 `.gap(Base04.rems(cx) * 2.)` 行吗？应该怎么写？

**答案**：直接这么写**编译不过**。`Rems` 派生的 `Mul` 要求两侧都是 `Rems`（另有一个 `Rems * Pixels -> Pixels` 的实现），f32 字面量无法隐式转换。可行写法是参照 [src/components/modal.rs:L390](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/modal.rs#L390) 的 `Base04.rems(cx) + Base04.rems(cx)`（`Add` 已派生）。但更该做的是先查表：`Base04` 翻倍与 `Base08` 在 Compact/Default 档同为 4px/8px，**Comfortable 档却分别是 12px 与 10px**——两套写法在非默认密度下并不等价，能用现成档位（`Base08`）就优先用它，保持间距节奏统一。

---

### 4.4 `ui_density()` 的正确使用边界

#### 4.4.1 概念说明

`src/styles/spacing.rs` 除了宏调用，还导出一个看似「更灵活」的函数 `ui_density(cx)`——直接返回当前 `UiDensity`。但它的文档用连续三条注记**限死了用途**：

- 用它来「修改或展示 UI 中除间距以外的东西」；
- **不要**用它计算间距值；
- 间距**永远**用 `DynamicSpacing`。

为什么管得这么严？如果允许 `match ui_density(cx) { Compact => px(3.), _ => px(4.) }` 这类手算代码存在，密度逻辑就会散落各处、数值不受 spacing.rs 表约束，三档之间的视觉节奏很快失去控制。`ui_density()` 留给那些「密度影响的是结构而非间距」的场景，例如按密度换一套布局、增删一个装饰元素、切换行高所用的**既有样式令牌**。

另有一个历史包袱值得知道：`UiDensity` 上还有一个 `spacing_ratio()` 方法（返回 0.75 / 1.0 / 1.25），标注着 `TODO: Standardize usage throughout the app or remove`，目前在整个仓库中**没有任何调用者**——它是密度机制演进中的旧方案残留，勿与 `DynamicSpacing::spacing_ratio`（生成的私有方法，语义是「该档位的 rem 比率」）混淆。

#### 4.4.2 核心流程

决策规则一览：

```text
需要「间距」（gap / padding / margin / 宽高）？
 ├── 是 ──► DynamicSpacing::BaseXX.rems(cx) / .px(cx)
 └── 否 ──► ui_density(cx) 是否合适？
              ├── 是：按密度切换结构/装饰/既有令牌（如行高 h_5 vs h_7）
              └── 否：也许根本不该感知密度
```

#### 4.4.3 源码精读

**函数本体及其三条注记**。[src/styles/spacing.rs:L46-L54](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles/spacing.rs#L46-L54)：

```rust
/// Returns the current [`UiDensity`] setting. Use this to
/// modify or show something in the UI other than spacing.
///
/// Do not use this to calculate spacing values.
///
/// Always use [DynamicSpacing] for spacing values.
pub fn ui_density(cx: &mut App) -> UiDensity {
    theme::theme_settings(cx).ui_density(cx)
}
```

注意它**不在 prelude 里**（prelude 只导出了 `DynamicSpacing`），需要写全路径 `ui::ui_density(cx)`——导入面上的「不鼓励」也是一种态度。另外签名收 `&mut App`（而 provider 方法只需 `&App`），调用点需在可变上下文中。

**合规使用范例**。[src/components/list/list_header.rs:L110-L127](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/list/list_header.rs#L110-L127) 是 ui crate 内的标准示范：密度决定列表头的**行高所用的既有样式令牌**（`h_5()` / `h_7()`），而不是手算任何像素：

```rust
let ui_density = theme::theme_settings(cx).ui_density(cx);

h_flex()
    // ...
    .map(|this| match self.height {
        Some(height) => this.h(height),
        None => match ui_density {
            UiDensity::Comfortable => this.h_5(),
            _ => this.h_7(),
        },
    })
```

这里没有出现一个手写数字——密度只用来「选令牌」，数值仍来自 gpui 的样式方法体系。（顺带一提，`IconSize::rems` 仍用 `rems_from_px` 静态定义、`IconSize::Custom` 分支留着 TODO，说明全量迁移到动态间距是渐进过程，新代码应一律走 `DynamicSpacing`。）

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：学会判断「密度感知代码」是否合规。
2. **操作步骤**：
   - 在 `crates/ui/src` 全局搜索 `ui_density`，逐处分类：是「选结构/令牌」还是「算数值」；
   - 再搜索 `DynamicSpacing::Base`，统计 ui crate 内有哪些组件已经接入（button、modal、tab、toggle、icon、list、keybinding 等）；
   - 最后读 `crates/theme/src/ui_density.rs:L34-L44` 的 `spacing_ratio()`，用 Grep 确认它在整个 crates 目录下没有调用者。
3. **需要观察的现象**：`ui_density` 的使用点极少且都是「选令牌」型；`DynamicSpacing` 的使用点遍布主要组件。
4. **预期结果**：得出结论——密度影响的「数值」收口在 spacing.rs 一张表，密度影响的「结构」才散见各组件；旧 `spacing_ratio()` 无调用者，属待清理遗留。
5. 本实践为纯源码阅读，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：同事写了 `let pad = match ui_density(cx) { Compact => px(2.), Default => px(4.), Comfortable => px(6.) };` 并用于 `.px(pad)`，问题出在哪？

**答案**：违反了「不要用 `ui_density()` 计算间距」的边界——这正是 `DynamicSpacing::Base04` 的定义（`(2, 4, 6)`），且手写版本不随 UI 字体缩放（固定 `px()`），也不会出现在档位表中接受统一维护。应改为 `.px(DynamicSpacing::Base04.rems(cx))`。

**练习 2**：`ui::ui_density(cx)` 与 `DynamicSpacing` 内部调用的 `theme_settings(cx).ui_density(cx)` 是什么关系？

**答案**：同一数据源的两个门面。前者是 ui crate 暴露给「非间距逻辑」的公开函数（签名 `&mut App`）；后者是宏生成代码在解析间距时的内部调用（签名 `&App`）。两者最终都落到 theme crate 的全局 `ThemeSettingsProvider`。

**练习 3**：`theme::UiDensity::spacing_ratio()`（0.75/1.0/1.25）与 `DynamicSpacing` 的宏生成机制有什么本质区别？

**答案**：前者是「全局线性缩放系数」思路——所有间距乘同一系数；后者是「逐档位查表」思路——每个档位在三档密度下可以独立手工调整（小间距变化更克制，如 `Base02` 的 1/2/4 并非线性）。查表胜在视觉可控，这正是旧系数方案被 TODO 标记废弃、新机制另起炉灶的原因。

---

## 5. 综合实践

**任务：把一张「项目信息卡」从硬编码改造为密度感知，并产出三档密度对照报告。**

**示例代码**（改造前，间距全部硬编码）：

```rust
// 示例代码
fn project_card(cx: &App) -> impl IntoElement {
    v_flex()
        .p(px(16.))
        .gap(px(8.))
        .rounded_lg()
        .child(
            h_flex()
                .gap(px(4.))
                .child(Icon::new(IconName::Info))
                .child(Label::new("my-project").size(LabelSize::Default)),
        )
        .child(Label::new("main 分支 · 3 个协作者").color(Color::Muted))
}
```

改造要求：

1. 把五处 `px(...)` 全部替换为语义等价的 `DynamicSpacing::BaseXX.rems(cx)`（提示：16→Base16、8→Base08、4→Base04；`p_4`/`px_2` 这类静态样式方法同样属于「硬编码」，一并处理）。
2. 为卡片中每个间距填写三档密度（`ui_font_size = 16px`）下的像素值对照表。
3. 回答两个设计题：
   - 若产品要求这张卡片在 Comfortable 下更「透气」，应该改组件代码还是在 spacing.rs 表中调档？为什么？（答案方向：除非全局节奏都要变，否则不要动表——表是全局共享的；组件层面应换用更大的既有档位如 `Base20`。）
   - 图标与文字之间的 `gap` 选 `Base04` 还是 `Base02` 更合适？说出你会去查哪些现有组件做参照（如 `button_like.rs`、`keybinding.rs` 的同场景用法）。
4. **验证**：把改造后的函数放进任一组件 `render` 或 doc 示例，`cargo check -p ui` 通过；有条件的话在 Zed 中切换三档 `ui_density` 实际观察——**待本地验证**。

通过这个任务，你把本讲三个模块串成了一条线：从用户设置（`UiDensity` 从哪来）到令牌表（`BaseXX` 怎么定义）再到渲染出口（`rems`/`px` 怎么解析），最后落到「哪些场景允许直接感知密度」的工程边界。

## 6. 本讲小结

- `DynamicSpacing` 是密度感知的间距令牌：组件构建期只存 `BaseXX` 语义键，渲染期查当前 `UiDensity` 解析，与 u2-l1 的 `Color` 同属「语义键 + 渲染期解析」模式。
- 全部 14 个档位由 `src/styles/spacing.rs` 中一张表经 `derive_dynamic_spacing!` **函数式过程宏**生成：三元组 `(a, b, c)` 直接指定三档像素，单值 `n` 套公式 \( \max(n-4,0)\ /\ n\ /\ n+4 \)；变体名取 Default 档像素值补零两位。
- 两个出口都要会选：`rems(cx)` 喂样式方法、布局期按窗口 rem 基准解析；`px(cx)` 得到具体 `Pixels` 用于算术（如 `Tab::content_height`）。**两者都随 UI 字体缩放**，"Base04 = 4px" 仅在 16px rem 基准 + Default 密度下成立。
- `ui_density()` 有严格边界：只用于展示或切换结构/既有令牌（范例是 `list_header.rs` 按 Comfortable 选 `h_5()`），**绝不用于手算间距**；间距一律走 `DynamicSpacing`。
- theme 的 `UiDensity::spacing_ratio()`（0.75/1.0/1.25）是带 TODO 的旧方案，仓库内无调用者，勿与宏生成的私有 `spacing_ratio` 混淆。
- 实践要点：改写硬编码间距时先查表选语义等价档位，再考虑密度档下的行为分叉（乘法/加法组合在非 Default 档可能不等价）。

## 7. 下一步学习建议

本讲之后，你已集齐 ui crate 设计令牌的最后一块拼图（颜色 u2-l1、排版 u2-l2、间距 u2-l3）。建议：

1. **下一讲 u2-l4（Elevation 与外观判断）**：补齐表面层级与明暗主题判断，随后即可进入组件族学习。
2. **带着间距视角重读组件**：精读 `src/components/modal.rs` 与 `src/components/toggle.rs`（Switch 的 `w(Base32.rems) / h(Base20.rems)` 是尺寸走动态间距的好例子），观察真实组件如何混用多个档位组成节奏。
3. **对照延伸**：`src/styles/units.rs` 的 `rems_from_px` 与本讲宏的「除以 16」是同一换算的两处实现，阅读时注意区分「静态 rem（不随密度）」与「动态间距（随密度）」的适用场景。
4. 若你对宏的实现感兴趣，可把 `ui_macros/src/dynamic_spacing.rs` 当作入门 `syn` / `quote` 的最小案例——它只用了括号解析、整数解析和 `format_ident!` 三招。
