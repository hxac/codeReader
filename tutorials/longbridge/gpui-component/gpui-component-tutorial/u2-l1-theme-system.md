# 主题系统：Theme、ThemeColor 与明暗模式

## 1. 本讲目标

本讲带你走进 gpui-component 的「主题系统」——所有组件配色、明暗模式、圆角、字体的统一来源。学完本讲你应该能够：

- 理解 `Theme` 全局单例的结构：它包含 `colors`（配色）、`tokens`（解析后的绘制值）、`highlight_theme`（语法高亮配色）、字体、圆角等。
- 掌握 `cx.theme()` 这个访问入口背后的 `ActiveTheme` trait 与 GPUI 的 Global 机制。
- 看懂 `ThemeColor` 这个「语义化配色字典」，并能通过 `Theme` 的 `Deref` 直接写 `cx.theme().background`。
- 掌握 `ThemeMode`（Light/Dark）切换，以及 `Theme::change` 如何把一套 JSON 配置变成运行时颜色。
- 理解 `apply_config` 末尾的 `clamp_alpha` 如何把 `list_active`/`table_active`/`selection` 的透明度钳到上限，避免「双重透明度衰减」导致选中高亮不可见。

本讲是 u2 单元（组件开发公共基础）的第一篇，承接 u1-l4 讲过的 `gpui_component::init(cx)` 与 `Root` 启动骨架。

## 2. 前置知识

阅读本讲前，建议你已经：

- 大致了解 GPUI 的「全局状态（Global）」概念：GPUI 允许把一个值注册到 `App` 上，整个应用任意位置都能读取它。主题正是这样一种全局值。
- 知道 Rust 的 `Deref` / `DerefMut`：它让一个结构体可以「伪装」成内部某个字段，从而 `cx.theme().background` 实际访问的是 `cx.theme().colors.background`。
- 了解颜色可以用 HSLA 表示：色相（Hue）、饱和度（Saturation）、亮度（Lightness）、透明度（Alpha）。gpui-component 内部颜色统一用 `gpui::Hsla`。

> 名词速查
> - **HSLA**：一种颜色模型，用 \((h, s, l, a)\) 四个分量描述颜色。本库中 \(h,s,l,a\) 都归一化到 \(0..1\)（其中 \(h=1.0\) 等价于 \(360^\circ\)）。
> - **Global**：GPUI 的应用级单例机制，类似其它框架的「全局 Store」。
> - **语义化颜色**：颜色不叫 `#1D4ED8`，而叫 `primary`（主色）、`background`（背景）、`border`（边框）。组件按语义取色，换肤只改语义对应的值。
> - **透明度（alpha）钳制**：把颜色透明度压到一个固定上限。注意区分两种写法——`Hsla::alpha(target)` 把 alpha **直接设为** target；而 `Colorize::opacity(factor)`（以及 `Background::opacity`）是 **相乘**（`a × factor`）。把「目标值」误当「因子」传给后者，会让原本就半透明的颜色再衰减一次，这正是本讲 4.5 节那个 bug 的根源。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `crates/ui/src/theme/` 目录下：

| 文件 | 作用 |
| --- | --- |
| [mod.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs) | 定义 `Theme` 全局单例、`ActiveTheme` trait、`ThemeMode`、`Theme::change` 等核心入口 |
| [theme_color.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/theme_color.rs) | 定义 `ThemeColor`（约 120 个语义化颜色字段）与 `ThemeToken`/`ThemeTokens` |
| [color.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs) | 颜色工具：`Colorize` trait（透明、提亮、混合）、颜色字符串解析 |
| [schema.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs) | 主题 JSON 的数据模型 `ThemeConfig` 与配置合并逻辑 `apply_config` |
| [registry.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/registry.rs) | `ThemeRegistry`：管理可用主题、解析默认主题 JSON |
| [default-theme.json](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/default-theme.json) | 内置默认主题（Default Light / Default Dark）的配色定义 |

---

## 4. 核心概念与源码讲解

### 4.1 Theme 全局单例与 ActiveTheme 访问入口

#### 4.1.1 概念说明

「主题」听起来很玄，其实就是一个被全应用共享的结构体 `Theme`。gpui-component 的所有组件（按钮、输入框、表格……）在绘制时都会去读这个结构体里的颜色、圆角、字体，而不是各自硬编码颜色。这样只需要改一处，整个应用就换了一套皮。

要做到「任意位置都能读」，`Theme` 用了 GPUI 的 **Global** 机制：它被注册到 `App` 上成为全局单例。`init(cx)` 时注册，之后任何持有 `&App`（或能 `Deref` 到 `&App`，比如 `Context`、`Window`）的地方都能拿到它。

而 `cx.theme()` 这个最常用的写法，背后是一个叫 `ActiveTheme` 的扩展 trait——它给 `App` 加了一个 `theme()` 方法，内部调用 `Theme::global(cx)` 取出全局主题。

#### 4.1.2 核心流程

```
gpui_component::init(cx)
      │
      ▼
theme::init(cx)        ── registry::init(cx) + Theme::change(Light, ...) 设置初始主题
      │
      ▼
cx.set_global::<Theme>(...)   ── Theme 成为全局单例
      │
      ▼
任意组件渲染时：cx.theme()  ──ActiveTheme──▶  Theme::global(cx)  ──▶  &Theme
```

关键点：`init(cx)` 必须最先调用（u1-l4 已讲过），因为它正是把 `Theme` 注册成全局、并加载默认 Light 主题的地方。

#### 4.1.3 源码精读

先看 `Theme` 结构体本身，它把「主题相关的一切」都装在一起：

- [crates/ui/src/theme/mod.rs:45-89](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L45-L89) —— `Theme` 结构体定义。可以看到它包含 `colors: ThemeColor`（配色）、`tokens: ThemeTokens`（解析后可直接绘制的值，比如渐变）、`highlight_theme`（Tree-sitter 语法高亮配色）、`mode`（明/暗）、字体族与字号、`radius`/`radius_lg`（圆角）、`shadow`、滚动条行为 `scrollbar_show` 等。

再看它如何成为全局单例：

- [crates/ui/src/theme/mod.rs:111](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L111) —— `impl Global for Theme {}`。这一行就是「注册成 GPUI 全局单例」的标记，有了它才能用 `cx.global::<Theme>()` 取值。
- [crates/ui/src/theme/mod.rs:115-124](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L115-L124) —— `Theme::global` / `global_mut` 两个方法，分别返回全局主题的只读与可变引用。

然后是访问入口 `ActiveTheme`：

- [crates/ui/src/theme/mod.rs:32-41](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L32-L41) —— `ActiveTheme` trait 与 `impl ActiveTheme for App`。`cx.theme()` 就等价于 `Theme::global(self)`。因为 `Context<T>` 会 `Deref` 到 `App`，所以在视图的 `render` 里写 `cx.theme()` 同样成立。

最后看 `init` 究竟做了什么：

- [crates/ui/src/theme/mod.rs:24-30](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L24-L30) —— `theme::init(cx)`：先初始化 `ThemeRegistry`，再调用 `Theme::change(ThemeMode::Light, None, cx)` 把默认 Light 主题注册成全局，并同步滚动条行为。这就是为什么「没调用 init 就用不了主题」。

一个真实用法来自 hello_world 示例：

- [examples/hello_world/src/main.rs:34](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/examples/hello_world/src/main.rs#L34) —— `Root::new(view, window, cx).bg(cx.theme().background)`：用当前主题的背景色给根视图上色。`cx.theme()` 此处 `cx` 是 `&mut App`，直接命中 `ActiveTheme`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `cx.theme()` 能拿到主题颜色，并观察改一个语义色的影响。

**操作步骤**：

1. 打开 [examples/hello_world/src/main.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/examples/hello_world/src/main.rs)。
2. 运行 `cargo run -p hello_world`（**待本地验证**：本讲不替你运行命令）。
3. 把第 34 行的 `.bg(cx.theme().background)` 改成 `.bg(cx.theme().primary)`，重新运行。

**需要观察的现象**：窗口背景从默认浅色（`background` = white）变成主色（`primary` = 接近黑的中性色）。

**预期结果**：说明 `cx.theme().primary` 取到的是 `ThemeColor` 里 `primary` 字段的值——你只改了「取哪个语义色」，组件并未感知颜色具体值，这正是主题系统的意义。

#### 4.1.5 小练习与答案

**练习 1**：为什么在视图的 `render(&mut self, _, cx: &mut Context<Self>)` 里能直接写 `cx.theme()`，而 `ActiveTheme` 只给 `App` 实现了？

> **参考答案**：因为 `Context<T>` 实现了 `Deref<Target = App>`，方法解析会自动透传到 `App` 上的 `ActiveTheme::theme`。

**练习 2**：如果不调用 `gpui_component::init(cx)`，直接在某处用 `cx.theme()` 会发生什么？

> **参考答案**：`Theme` 没有被 `set_global` 注册，`Theme::global(cx)`（内部 `cx.global::<Theme>()`）会 panic，提示全局不存在。所以 `init` 必须先调用。

---

### 4.2 ThemeColor：语义化配色字典

#### 4.2.1 概念说明

`ThemeColor` 是主题系统真正的「颜色仓库」——一个扁平的结构体，里面有大约 120 个 `Hsla` 字段，每个字段都是一个**语义化名字**：`background`、`foreground`、`primary`、`border`、`button`、`button_hover`、`danger`、`muted`、`tab`、`table_head`……组件代码里写 `cx.theme().danger` 而不是 `#EF4444`，这样换肤时只要改这些语义值即可。

为了让访问更顺手，`Theme` 对 `colors` 字段实现了 `Deref`/`DerefMut`，所以 `cx.theme().background` 等价于 `cx.theme().colors.background`——少写一层。

此外还有 `ThemeToken` / `ThemeTokens`：有些颜色值不只是纯色，还可能是渐变（`linear-gradient(...)`），于是需要一个同时保存「代表纯色」和「实际绘制背景」的结构。`ThemeToken` 就是这样的双值结构。

#### 4.2.2 核心流程

```
ThemeColor（纯 Hsla 字典）
      │  From<&ThemeColor>          （每个字段 → ThemeToken）
      ▼
ThemeTokens（每字段含 color + 可绘制 background，如渐变）
      │
存储在 Theme.tokens / 供高级渲染使用

Theme ──Deref──▶ ThemeColor   （所以 cx.theme().primary 直接命中 colors.primary）
```

颜色之间还能用 `Colorize` trait 做运算，例如透明度、提亮、变暗、OkLab 空间混合——这些被 `apply_config` 用来在配置没给某颜色时**自动派生**合理值。

几个关键公式（本库实现，`factor` ∈ \([0,1]\)）：

- 透明度：\(a' = a \cdot \text{factor}\)
- 提亮：\(l' = l \cdot (1 + \text{factor})\)
- 变暗：\(l' = l \cdot (1 - \text{factor})\)

#### 4.2.3 源码精读

- [crates/ui/src/theme/theme_color.rs:59-345](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/theme_color.rs#L59-L345) —— `ThemeColor` 结构体。每个字段都有中文/英文注释说明用途，例如 `pub background: Hsla`（默认背景）、`pub primary: Hsla`（主色）、`pub button_hover: Hsla`（按钮悬停）。这一大段就是「语义化配色字典」的全部条目。

- [crates/ui/src/theme/mod.rs:97-109](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L97-L109) —— `impl Deref/DerefMut for Theme`，把 `Theme` 透明地转发到 `colors: ThemeColor`。这就是 `cx.theme().background` 能直接写的原因。

- [crates/ui/src/theme/theme_color.rs:9-49](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/theme_color.rs#L9-L49) —— `ThemeToken`：同时持有 `color: Hsla`（代表性纯色）和 `background: Background`（实际可绘制背景，可能是渐变）。注意它同样 `Deref` 到 `Hsla`，并提供到 `Hsla`/`Background`/`Fill` 的转换。

- [crates/ui/src/theme/color.rs:20-61](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs#L20-L61) —— `Colorize` trait 声明：`opacity`、`divide`、`invert`、`lighten`、`darken`、`mix`、`mix_oklab`、`hue`、`saturation`、`lightness`、`to_hex`、`parse_hex` 等。

- [crates/ui/src/theme/color.rs:139-180](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs#L139-L180) —— `impl Colorize for Hsla` 中 `opacity`/`lighten`/`darken` 的实现，正好对应上面三个公式。

- [crates/ui/src/theme/theme_color.rs:515-525](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/theme_color.rs#L515-L525) —— `ThemeColor::light()` / `dark()`：从预解析的 `DEFAULT_THEME_COLORS` 取出内置的亮/暗配色（`Arc` 引用，零拷贝）。

#### 4.2.4 代码实践

**实践目标**：理解颜色字符串能写成哪些形式（这是主题 JSON 里颜色的写法）。

**操作步骤**：

1. 阅读测试 [crates/ui/src/theme/color.rs:1072-1093](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs#L1072-L1093)（`test_try_parse_color`）。
2. 对照解析函数 [crates/ui/src/theme/color.rs:677-741](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs#L677-L741)，弄清 `try_parse_color` 接受哪些格式。

**需要观察的现象**：测试断言列出了多种合法写法。

**预期结果**：你应能总结出合法格式有：HEX `#RRGGBB` / `#RRGGBBAA`、颜色名 `red`、带刻度 `blue-600`、带透明度 `pink/33`、带刻度+透明度 `orange-300/66`。这些写法都能直接用在主题 JSON 里。

#### 4.2.5 小练习与答案

**练习 1**：`cx.theme().primary` 和 `cx.theme().colors.primary` 有区别吗？为什么两者都可用？

> **参考答案**：运行时取到的是同一个值。`colors.primary` 是字段直访；`cx.theme().primary` 走的是 `Theme` 对 `ThemeColor` 的 `Deref`，由编译器自动转发。两者等价，`Deref` 版本更简洁。

**练习 2**：`ThemeColor` 里有 `button` 但没有 `button_hover` 的「硬编码默认」——它在配置缺失时由谁算出来？

> **参考答案**：由 `apply_config` 里的 fallback 逻辑用 `Colorize` 派生（例如 `button_hover` fallback 为 `self.input.mix_oklab(transparent, 0.5)`）。这就是 `Colorize` 的实际用途之一。

---

### 4.3 主题配置的加载：从 JSON 到运行时

#### 4.3.1 概念说明

`ThemeColor` 的默认值并不是手写在 Rust 里的，而是来自一份 JSON：[default-theme.json](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/default-theme.json)。这份 JSON 描述了一个「主题集」`ThemeSet`，里面包含 Default Light 和 Default Dark 两套配置（`ThemeConfig`）。

启动时，`ThemeRegistry`（主题注册表）会解析这份 JSON，把两套配置登记成 `default_themes`，并预解析出 `DEFAULT_THEME_COLORS`。当需要应用某套主题时，`apply_config` 会把 JSON 配置「合并」进 `ThemeColor`——配置里给了的字段就用配置值，没给的字段就按 fallback 规则派生（用 `Colorize` 算）。

这套设计的好处是：换肤 = 换一份 JSON，运行时甚至能监听主题目录热重载。

#### 4.3.2 核心流程

```
default-theme.json  ──include_str!──▶ 编译进二进制
        │
        ▼ serde_json 解析
ThemeSet { themes: Vec<ThemeConfig> }
        │
        ├──▶ registry::init_default_themes  ──▶ default_themes[Light/Dark]
        └──▶ DEFAULT_THEME_COLORS（预解析 ThemeColor + HighlightTheme）
        │
当 Theme::change(mode) 被调用
        ▼
Theme::apply_config(config)  ──▶ ThemeColor::apply_config(config, default)
        │  字段逐个合并：有配置用配置，否则 fallback 派生
        ▼
更新 Theme.colors / tokens / 字体 / 圆角 / highlight_theme
```

#### 4.3.3 源码精读

- [crates/ui/src/theme/default-theme.json:1-12](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/default-theme.json#L1-L12) —— 默认主题集开头：`name: "Default"`、`author: "shadcn"`，`themes` 数组里第一个是 `"name": "Default Light"`、`"mode": "light"`，`colors` 用语义键（如 `"background": "white"`、`"primary.background": "neutral-900"`）。注意键名是带点的字符串，对应 `ThemeConfigColors` 的 `#[serde(rename = "...")]`。

- [crates/ui/src/theme/registry.rs:12-39](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/registry.rs#L12-L39) —— `DEFAULT_THEME_COLORS`：一个 `LazyLock` 静态量，首次访问时解析 `default-theme.json`，为每个 `ThemeMode` 预生成一份 `(Arc<ThemeColor>, Arc<HighlightTheme>)`。

- [crates/ui/src/theme/registry.rs:41-73](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/registry.rs#L41-L73) —— `registry::init(cx)`：注册 `ThemeRegistry` 全局、调用 `init_default_themes`，并用 `observe_global::<ThemeRegistry>` 监听变化——一旦注册表里的主题变了，就重新 `Theme::change` 并刷新所有窗口。这就是「热重载主题」的原理。

- [crates/ui/src/theme/registry.rs:163-183](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/registry.rs#L163-L183) —— `init_default_themes`：把 JSON 里的主题按 `mode` 分桶放进 `default_themes`，并复制一份到 `themes` 供查询。

- [crates/ui/src/theme/schema.rs:36-78](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L36-L78) —— `ThemeConfig`：一份主题配置的数据模型，含 `name`、`mode`、字体、圆角、`shadow`、`colors: ThemeConfigColors`，以及兼容 Zed 的 `highlight`（语法高亮样式）。

- [crates/ui/src/theme/schema.rs:513-891](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L513-L891) —— `ThemeColor::apply_config`：核心合并逻辑。它用两个宏 `apply_color!`/`apply_background_color!` 逐字段处理：JSON 给了值就解析（失败回退到默认），没给就用 `fallback` 表达式（大量调用 `Colorize` 的 `mix_oklab`/`blend`/`opacity`/`darken` 派生）。例如 [schema.rs:760-779](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L760-L779) 里 `danger` 系列颜色都从 `red` 派生。函数末尾还会对 `list_active`/`table_active`/`selection` 三类交互态高亮做 alpha 钳制（详见 4.5 节）。

- [crates/ui/src/theme/schema.rs:896-941](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L896-L941) —— `Theme::apply_config`：把 `ThemeConfig` 应用到 `Theme`——设置字体/圆角/高亮，并调用上面的 `ThemeColor::apply_config` 重算 `colors` 与 `tokens`。

> 💡 一个易错点：内置默认主题是用 `include_str!` **编译期**嵌入的，所以直接改 `default-theme.json` 后必须重新 `cargo build` 才会生效（它不是运行时读盘的文件）。运行时读盘的是 `ThemeRegistry::watch_dir` 监听的 `themes/` 目录，那是给「用户自定义主题」用的。

#### 4.3.4 代码实践

**实践目标**：通过一个单元测试理解 `apply_config` 的「渐变 + fallback」行为。

**操作步骤**：

1. 阅读 [crates/ui/src/theme/schema.rs:951-991](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L951-L991)（`test_apply_config_preserves_gradient_background_and_solid_color_fallback`）。
2. 关注两点：① `primary.background` 写成 `linear-gradient(...)` 后，`theme.primary`（纯色）取的是渐变**起始色**，而 `theme.tokens.primary.background` 保留**完整渐变**；② 没有显式给的 `button_primary` 会 fallback 到 `tokens.primary`，因此也带上了渐变。

**需要观察的现象**：断言 `theme.tokens.button_primary.background == theme.tokens.primary.background` 成立。

**预期结果**：说明 fallback 不只是「取纯色」，而是把 token（含可绘制背景）整体复用，渐变得以传播。**待本地验证**：可执行 `cargo test -p gpui-component test_apply_config_preserves_gradient` 观察通过。

#### 4.3.5 小练习与答案

**练习 1**：为什么改了 `default-theme.json` 后直接 `cargo run` 不一定看到变化？

> **参考答案**：因为 `registry.rs` 用 `include_str!("./default-theme.json")` 在编译期把内容嵌入二进制。修改后必须重新编译（`cargo build`/`cargo run` 会触发）才会生效。

**练习 2**：`apply_config` 中很多颜色带 `fallback =`，这个 fallback 是相对什么计算的？

> **参考答案**：相对「已经解析出的其它颜色」和当前 `mode`（亮/暗）计算。例如 hover 色常用 `self.background.blend(self.primary.opacity(0.9))`，active 色常用 `self.primary.darken(active_darken)`，其中 `active_darken` 在暗色模式取 `0.2`、亮色取 `0.1`（见 [schema.rs:618](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L618)）。

---

### 4.4 明暗模式切换：ThemeMode 与 Theme::change

#### 4.4.1 概念说明

明暗模式用一个简单的枚举 `ThemeMode { Light, Dark }` 表示。`Theme` 里同时保存了 `light_theme` 和 `dark_theme` 两套 `ThemeConfig`，当前用哪套由 `Theme.mode` 决定。

切换模式的统一入口是 `Theme::change(mode, window, cx)`：它设置 `mode`，然后根据模式把对应的 `ThemeConfig` 应用到 `Theme`（重算所有颜色），最后刷新窗口让界面重绘。

此外还有 `sync_system_appearance`：跟随操作系统的明暗外观自动切换。

#### 4.4.2 核心流程

```
读取当前 mode: cx.theme().mode.is_dark()
        │ 取反得到目标 mode
        ▼
Theme::change(mode, window, cx)
        │ 1. 若全局 Theme 尚未初始化，用默认 light/dark 配置创建
        │ 2. theme.mode = mode
        │ 3. apply_config(theme.light_theme 或 theme.dark_theme)  ── 重算颜色
        │ 4. 若传入 window，调用 window.refresh()
        ▼
（通常再调用 cx.refresh_windows() 让所有窗口重绘）
```

> 注意：`Theme::change` 的第二参数 `window` 传 `None` 时不会自动刷新窗口，所以调用方一般要补一句 `cx.refresh_windows()`（见下方真实代码）。

#### 4.4.3 源码精读

- [crates/ui/src/theme/mod.rs:243-262](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L243-L262) —— `ThemeMode` 枚举，默认 `Light`，`#[serde(rename_all = "snake_case")]` 让 JSON 里写 `"light"`/`"dark"`。

- [crates/ui/src/theme/mod.rs:264-277](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L264-L277) —— `ThemeMode::is_dark()` 与 `name()`（返回 `"light"`/`"dark"`）。

- [crates/ui/src/theme/mod.rs:279-286](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L279-L286) —— `From<WindowAppearance> for ThemeMode`：把 GPUI 的系统外观（`Dark`/`VibrantDark`/`Light`/`VibrantLight`）映射成本库的 `ThemeMode`。这是「跟随系统」的基础。

- [crates/ui/src/theme/mod.rs:163-183](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L163-L183) —— `Theme::change`：切换主入口。注意第 165-170 行，若全局主题还没初始化，会先用 `ThemeRegistry` 的默认 light/dark 配置建好 `Theme`；随后按 `mode` 选择 `dark_theme` 或 `light_theme` 调用 `apply_config`。

- [crates/ui/src/theme/mod.rs:142-151](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/mod.rs#L142-L151) —— `sync_system_appearance`：读取 `window.appearance()`（或 `cx.window_appearance()`）并调用 `change`，实现跟随系统。注释提到在 Linux 上优先用 `window.appearance()` 以避开一个已知问题。

一个生产环境的真实用法（设置页的暗色开关）：

- [crates/story/src/stories/settings_story.rs:147-156](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/settings_story.rs#L147-L156) —— 读取 `cx.theme().mode.is_dark()` 作为开关当前值；开关变化时算出目标 `ThemeMode`，先 `Theme::global_mut(cx).mode = mode`，再 `Theme::change(mode, None, cx)` 重新应用配色。这是本讲综合实践要模仿的范式。

#### 4.4.4 代码实践

见第 5 节「综合实践」，那里把 `Theme::change` 与 `cx.theme().background` 完整串起来。

#### 4.4.5 小练习与答案

**练习 1**：调用 `Theme::change(ThemeMode::Dark, None, cx)` 后界面没立刻变暗，可能漏了什么？

> **参考答案**：第二参数传了 `None`，`change` 不会自动刷新窗口。需要补一句 `cx.refresh_windows()`，或在有窗口句柄时把 `Some(window)` 传进去触发 `window.refresh()`。

**练习 2**：`Theme` 同时持有 `light_theme` 和 `dark_theme` 两份配置，这样做的好处是什么？

> **参考答案**：切换明暗模式时不需要重新解析 JSON 或重新加载主题，直接从内存里的另一份配置 `apply_config` 即可，切换瞬时完成；同时也方便「亮/暗分别配置不同主题名」（见 `registry::init` 里对 `light_theme`/`dark_theme` name 的观察逻辑）。

---

### 4.5 交互态高亮的 alpha 钳制：clamp_alpha

#### 4.5.1 概念说明

`apply_config` 在逐字段合并完颜色后，还会在最后为三类**交互态高亮色**做一次「透明度钳制」（clamp alpha）：

- `list_active`：列表项被选中/激活的背景。
- `table_active`：表格行被选中/激活的背景。
- `selection`：文本选区高亮。

这三类颜色都要求「半透明」——太浓会盖住前景文字，太淡又看不出选中态。库的统一策略是把它们的 alpha 压到一个上限：`list_active`/`table_active` 上限 `0.2`，`selection` 上限 `0.3`。

但在 [PR #2512](https://github.com/longbridge/gpui-component/pull/2512) 之前，这里的实现藏着一个 bug。关键在于：`Background::opacity(factor)` 是**乘法**语义，它用「现有 alpha」乘以 `factor`；而旧代码却把「目标绝对 alpha」（比如 `0.2`）直接当成 `factor` 传了进去。于是当颜色本身已经带 `0.2` 透明度时，最终 alpha 变成 \(0.2 \times 0.2 = 0.04\)，**双重衰减**，选中行的高亮几乎看不见：

\[ a_{\text{旧}} = a_{\text{base}} \times \text{target} \quad (\text{当 } a_{\text{base}} = \text{target} = 0.2 \Rightarrow 0.04) \]

这个 bug 之所以在表格里暴露、在列表里「侥幸」没被发现，是因为两者读取的字段不同：表格组件读的是 `tokens.table_active.background`（可绘制背景，走了错误的乘法路径）；而列表组件读的是纯 `Hsla`（`self.list_active`，那条路径恰好用 `Hsla::alpha(target)` 直接赋值，没有双重衰减）。`selection` 之所以「正确只是运气好」，是因为它的默认 base alpha 恰好是 `1.0`。

#### 4.5.2 核心流程

修复后的做法是用一个共享闭包 `clamp_alpha`，把三种颜色用同一套逻辑处理：

```
对每个交互态色，给定上限 max（list_active/table_active=0.2，selection=0.3）：
        │
        ▼
base   = color.a                  当前 alpha
target = min(base, max)           钳到上限
        │
        ├── 纯色 color：color.alpha(target)        直接把 alpha 设为 target
        │
        └── 可绘制 background（可能是渐变）：
              ├── 有原始 JSON 字符串 raw 时：
              │     try_parse_background_clamped(raw, max)
              │       逐个渐变 stop 独立把 alpha 压到 max
              └── 只有已解析的 Background 时：
                    factor = target / base     把「绝对目标」换算成「乘法因子」
                    background.opacity(factor)  最终 alpha = base × factor = target
```

两处关键修正：

1. **把「绝对目标」换算成「因子」**：调用 `Background::opacity` 时传 `factor = target / base`，这样 \( \text{base} \times \text{factor} = \text{target} \)，最终 alpha 精准落在 `target`，与 base 初始 alpha 无关，根除双重衰减。当 `base == 0`（完全透明）时因子无意义，取 `1.0` 保持原样。

2. **渐变要逐 stop 钳制**：`try_parse_background_clamped` 不像 `Background::opacity` 用单一因子缩放全部 stop，而是对**每个 stop 独立**执行 `alpha(min(max))`。这样渐变中某个高亮 stop（例如完全不透明的 `to` stop）也不会突破上限，同时低于上限的 stop 仍保留其原有深浅。

#### 4.5.3 源码精读

- [crates/ui/src/theme/schema.rs:857-888](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L857-L888) —— `apply_config` 末尾的 `clamp_alpha` 闭包与三处调用。第 865 行 `let factor = if base > 0. { target / base } else { 1. };` 就是「把绝对 alpha 换算成乘法因子」的关键；第 863 行在能拿到原始 JSON 字符串时优先走 `try_parse_background_clamped` 逐 stop 钳制。三处调用分别给 `list_active`/`table_active` 传 `0.2`、给 `selection` 传 `0.3`。

- [crates/ui/src/theme/color.rs:756-775](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs#L756-L775) —— `try_parse_background_clamped`：纯色分支 `color.alpha(color.a.min(max))` 直接钳制；渐变分支对 `from`/`to` 两个 stop 各自 `alpha(min(max))`，互不影响。注释明确指出它和 `Background::opacity`「单一因子缩放」的区别。

- [crates/ui/src/theme/default-theme.json:41](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/default-theme.json#L41) —— 内置亮色主题的 `"list.active.background": "#bfdbfe33"`。末尾两位 `33` 是 alpha，\(0x33 / 0xff \approx 0.2\)，正是上面「双重衰减」复现时用到的 base alpha；`table.active.background` 同样是 `#bfdbfe33`（见第 84 行）。

- [crates/ui/src/theme/schema.rs:1033-1081](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L1033-L1081) —— 随该修复新增的回归测试 `test_apply_config_clamps_highlight_alpha_per_gradient_stop`，覆盖三种情形：纯色被钳到 `0.2`、含不透明 `to` stop 的渐变被逐 stop 钳制、含透明 `from` stop 的渐变仍能把不透明 `to` stop 钳住（这正是旧代码 `base == 0` 分支漏掉的情形）。

> 💡 为什么要分「有原始字符串」和「无原始字符串」两条路径？因为走到 `clamp_alpha` 时，纯色 `color` 已经解析好，可以直接重设 alpha；但可绘制 `background`（可能是渐变）此时只剩一个 `Background` 值，已丢失「每个 stop 的字符串」。要逐 stop 钳制，就得从 `config.colors` 里的原始 JSON 字符串（如 `colors.list_active.as_deref()`）重新解析一次——这正是闭包要把原始串一并传进来的原因。

#### 4.5.4 代码实践

**实践目标**：亲手跑通新增的回归测试，理解「双重衰减」与「逐 stop 钳制」的区别。

**操作步骤**：

1. 阅读 [crates/ui/src/theme/schema.rs:1033-1081](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L1033-L1081) 的测试，对照注释弄清它构造的三种颜色配置（纯色、含不透明 `to` 的渐变、含透明 `from` 的渐变）。
2. 执行 `cargo test -p gpui-component test_apply_config_clamps_highlight_alpha_per_gradient_stop`（**待本地验证**：本讲不替你运行命令）。
3. （可选）把 [schema.rs:865](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs#L865) 的 `target / base` 临时改回旧逻辑 `target`（即直接当因子用），再跑该测试，观察断言如何失败。

**需要观察的现象**：步骤 2 测试通过；步骤 3 改回旧逻辑后，`theme.tokens.table_active.background` 的 `to` stop alpha 不再是 `0.2`（被乘成了更小的值），断言失败。

**预期结果**：说明新逻辑既能避免纯色路径的双重衰减，也能保证渐变每个 stop 都不突破上限——这正是交互态高亮始终清晰可见的原因。

#### 4.5.5 小练习与答案

**练习 1**：为什么旧代码下 `list_active` 没有像 `table_active` 那样「变看不见」？

> **参考答案**：因为列表组件读取的是 `ThemeColor` 里的纯 `Hsla`（`self.list_active`），旧代码对它用 `Hsla::alpha(target)` 直接赋值，本身没问题；而表格组件读取的是 `tokens.table_active.background`（可绘制背景），旧代码错误地用 `background.opacity(target)` 做乘法，才产生双重衰减。两者读的字段不同，bug 只在后一条路径上显现。

**练习 2**：若把一个交互态色配置成「`from` 半透明（alpha 0.2）、`to` 完全不透明」的渐变，用旧的单一因子 `Background::opacity(0.2)` 缩放，结果会怎样？新逻辑又是如何修正的？

> **参考答案**：单因子会把两个 stop 都乘以 `0.2`：`from` 从 `0.2` 变成 `0.04`（被二次衰减，几乎消失），`to` 从 `1.0` 变成 `0.2`。新逻辑 `try_parse_background_clamped` 改为对每个 stop 独立执行 `min(a, max)`：`from` 取 `min(0.2, 0.2) = 0.2`（不被进一步压低），`to` 取 `min(1.0, 0.2) = 0.2`（被钳到上限）。这样既封住上限，又不会让本就偏淡的 stop 被再压一次。

---

## 5. 综合实践

**任务**：编写一个最小应用，窗口里有一个按钮，点击后在亮/暗模式之间切换；容器背景用 `cx.theme().background` 上色，切换后背景色立刻跟着变化，以此验证主题确实生效。

下面是参考实现（**示例代码**，基于 [examples/hello_world/src/main.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/examples/hello_world/src/main.rs) 改写，非项目原有文件）：

```rust
// 示例代码：theme_toggle 示例
use gpui::*;
use gpui_component::{button::*, Theme, ThemeMode, Root, *};

pub struct Example;

impl Render for Example {
    fn render(&mut self, _: &mut Window, _: &mut Context<Self>) -> impl IntoElement {
        div()
            .v_flex()
            .gap_2()
            .size_full()
            .items_center()
            .justify_center()
            // 关键点 1：用当前主题的背景色上色，切换主题后这里会立即变化
            .bg(cx.theme().background)
            .child(div().text_color(cx.theme().foreground).child("Theme Toggle Demo"))
            .child(
                Button::new("toggle")
                    .primary()
                    .label("切换明暗模式")
                    // 关键点 2：on_click 第三参数是 &mut App，可直接当 cx 用
                    .on_click(|_, _, cx| {
                        // 取当前模式并取反
                        let mode = if cx.theme().mode.is_dark() {
                            ThemeMode::Light
                        } else {
                            ThemeMode::Dark
                        };
                        // 应用新模式并刷新所有窗口
                        Theme::change(mode, None, cx);
                        cx.refresh_windows();
                    }),
            )
    }
}

fn main() {
    gpui_platform::application().run(move |cx| {
        // 必须最先调用，它会把 Theme 注册成全局单例
        gpui_component::init(cx);

        cx.spawn(async move |cx| {
            cx.open_window(WindowOptions::default(), |window, cx| {
                let view = cx.new(|_| Example);
                // 窗口第一层视图必须是 Root
                cx.new(|cx| Root::new(view, window, cx).bg(cx.theme().background))
            })
            .expect("Failed to open window");
        })
        .detach();
    });
}
```

**操作步骤**：

1. 在 `examples/` 下新建一个示例 crate（可参照 `examples/hello_world` 的 `Cargo.toml` 结构），把上面的代码放入 `src/main.rs`。
2. 运行 `cargo run -p <你的包名>`（**待本地验证**：具体包名依你创建的 crate 而定）。

**需要观察的现象**：

- 初始为亮色模式，窗口背景接近白色，按钮为深色主色。
- 每点击一次按钮：背景在白色（`background` = white）与深色（`background` = 暗色主题值）之间切换；文字颜色（`foreground`）也随之反转；按钮主色（`primary`）也跟着变。

**预期结果**：证明三件事——① `cx.theme().background` 取到的是当前激活主题的语义色；② `Theme::change` 成功重算了整套颜色；③ `cx.refresh_windows()` 触发了重绘，使变化可见。

> 进阶：把 `Theme::change(mode, None, cx)` 换成 `Theme::sync_system_appearance(Some(window), cx)`，观察应用是否会跟随操作系统的明暗设置变化（需要你能切换系统外观）。

## 6. 本讲小结

- `Theme` 是注册到 `App` 的 **Global 全局单例**，装着配色、字体、圆角、语法高亮主题等一切主题相关状态；`init(cx)` 负责注册并加载默认 Light 主题。
- 访问入口是 `ActiveTheme` trait 提供的 `cx.theme()`，内部等价于 `Theme::global(cx)`；因 `Context` 可 `Deref` 到 `App`，在 `render` 里也能直接用。
- `ThemeColor` 是约 120 个字段的**语义化配色字典**；`Theme` 对它做 `Deref`，所以 `cx.theme().primary` 直达 `colors.primary`。
- 颜色用 `Hsla`，配合 `Colorize` trait 可做透明/提亮/变暗/OkLab 混合，`apply_config` 大量用这些规则在配置缺失时**派生**合理颜色。
- 交互态高亮（`list_active`/`table_active`/`selection`）的透明度由 `apply_config` 末尾的 `clamp_alpha` 统一钳制：纯色直接设目标 alpha，可绘制背景（含渐变）则按 `target / base` 因子缩放或用 `try_parse_background_clamped` 逐 stop 钳制，杜绝「双重衰减」让选中行变看不见（PR #2512）。
- 默认主题来自编译期嵌入的 `default-theme.json`，经 `ThemeRegistry` 解析、`apply_config` 合并成运行时颜色；用户自定义主题则可经 `watch_dir` 热重载。
- 明暗模式用 `ThemeMode { Light, Dark }` 表达，统一通过 `Theme::change(mode, window, cx)` 切换；`sync_system_appearance` 可跟随系统外观。

## 7. 下一步学习建议

- **本单元后续**：下一讲 u2-l2「样式系统：Styled 与尺寸 Sizable」会讲 `div().flex().gap_2()` 这类链式样式 API 和 `Sizable`（xs/sm/md/lg），它们与主题色一起决定了组件外观。
- **深入主题定制**：若想自定义整套配色，可阅读 [crates/ui/src/theme/schema.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/schema.rs) 的 `ThemeConfig` 与 `apply_config`，再参照仓库根的 `themes/` 目录（如 `aurora.json`）写一份自己的主题 JSON。
- **语法高亮主题**：`Theme.highlight_theme` 关联到 u9-l2 的 Tree-sitter Highlighter，届时你会看到主题颜色如何作用到代码高亮上。
- **运行时观察**：在 Story Gallery（`cargo run`）的设置页里有现成的暗色开关与主题选择器，可对照 [crates/story/src/stories/settings_story.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/settings_story.rs) 实时体验切换效果。
