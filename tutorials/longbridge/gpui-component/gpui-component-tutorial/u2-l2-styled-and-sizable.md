# 样式系统：Styled 与尺寸 Sizable

## 1. 本讲目标

本讲是「组件开发公共基础」的第二讲。上一讲我们学会了用 `cx.theme()` 取主题色，但一个组件光有颜色还不够——它还要有布局、间距、圆角、阴影、字号，以及统一的「大中小」尺寸。

学完本讲，你应当能够：

- 用 `div().flex().gap_2().size_full()` 这类**链式样式 API** 把一个容器「写」出来，并理解它背后来自哪里。
- 理解 gpui-component 的 `StyledExt` 扩展 trait 是如何给所有 GPUI 元素增加便捷样式方法的。
- 理解 `Size` 枚举与 `Sizable` trait 如何让 60+ 组件**统一支持 xs/sm/md/lg 四档尺寸**。
- 区分「无状态 `RenderOnce` 组件」与「有状态 `Render` 视图」两种设计，知道什么时候该用哪一种。

本讲覆盖三个最小模块：**Styled、Sizable、RenderOnce**。

## 2. 前置知识

在进入源码前，先用三个小概念打底：

- **元素（Element）与样式（Style）**。在 GPUI 里，屏幕上一切可见的东西都是「元素」。每个元素都附带一份「样式」，描述它的大小、颜色、间距、布局方式。gpui-component 沿用 GPUI 的做法，用类 CSS 的链式方法来设置样式。
- **链式构建（Builder / Fluent API）**。你会大量看到形如 `div().flex().gap_2().bg(red)` 的写法。每个方法都返回 `Self`（被修改后的自己），所以可以一路「点」下去。这在后面会反复出现。
- **trait + blanket impl（特征 + 全局实现）**。gpui-component 给 GPUI 的 `Styled` trait 写了一个扩展 trait `StyledExt`，并用 `impl<E: Styled> StyledExt for E {}` 一行代码让「凡是实现了 `Styled` 的类型，都自动获得 `StyledExt` 的方法」。这是本讲理解「为什么任何 `div()` 都能调用 `h_flex()`」的关键。

如果你已经读过 [u1-l4](u1-l4-entry-init-and-root.md)（入口与 Root）和 [u2-l1](u2-l1-theme-system.md)（主题系统），本讲会非常顺。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/ui/src/styled.rs` | 样式系统的核心：`StyledExt` 扩展 trait、`h_flex`/`v_flex` 便捷函数、`box_shadow` 构造器、`Size` 枚举、`Sizable`/`StyleSized` 等 trait。本讲最关键的文件。 |
| `crates/ui/src/element_ext.rs` | 元素扩展 trait `ElementExt`（如 `on_prepaint` 获取元素绘制后的边界），以及 `AnyChildElement` 这类子元素能力。 |
| `crates/ui/src/badge.rs` | 一个典型的**无状态 `RenderOnce` 组件**，同时实现了 `Sizable`，是「样式 + 尺寸 + RenderOnce」三者结合的最佳范例。 |
| `crates/ui/src/button/button.rs` | `Button` 同样是 `RenderOnce` + `Sizable` + `Styled`，结构更复杂，用于对比。 |
| `examples/hello_world/src/main.rs` | 一个典型的**有状态 `Render` 视图**，用于和无状态组件做对比。 |

> 这些 trait 都在 [crates/ui/src/lib.rs:94](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L94) 通过 `pub use styled::*;` 对外导出，所以你可以直接 `use gpui_component::{Sizable, Size, StyledExt, h_flex};`。

---

## 4. 核心概念与源码讲解

### 4.1 Styled：类 CSS 的链式样式 API

#### 4.1.1 概念说明

在网页里我们用 CSS 写样式；在 gpui-component 里，我们用 **Rust 方法链** 写样式。最基础的写法是从 `div()` 出发：

```rust
div()
    .flex()           // 弹性布局
    .gap_2()          // 子元素间距
    .size_full()      // 宽高都撑满父容器
    .items_center()   // 子元素交叉轴居中
    .bg(cx.theme().background)  // 背景色取自主题
```

这套 API 的底层是 GPUI 提供的 [`Styled`](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L1-L6) trait（从 `gpui` 引入），它定义了 `flex`、`gap`、`size`、`bg`、`text_color`、各种 `px_*`/`py_*`/`size_*` 间距方法等。

gpui-component 在此之上又加了一层自己的扩展，叫 `StyledExt`，提供更贴近业务的高级便捷方法（如 `h_flex`、`paddings`、`popover_style`、`debug_red` 等）。

#### 4.1.2 核心流程

样式应用的核心流程是一个**「构造 → 修改 StyleRefinement → 返回 self」**的循环：

1. `div()` 创建一个 `Div` 元素，内部持有一份 `StyleRefinement`（样式描述，初始为空）。
2. 你调用 `.flex()`、`.gap_2()` 等方法时，方法内部读取 `self.style()` 拿到这份描述、写入对应字段，再返回 `self`。
3. 元素被渲染时，GPUI 把这份 `StyleRefinement` 解析成实际的布局参数并绘制。

关键在于：**所有方法都返回 `Self`，所以可以无限「点」下去**。这正是「链式样式 API」的全部秘密——没有黑魔法，就是 builder 模式。

gpui-component 的扩展通过「扩展 trait + 全局实现（blanket impl）」融入这个体系，因此你不需要 import 任何额外的东西，任何实现了 `Styled` 的元素就自动拥有扩展方法。

#### 4.1.3 源码精读

**便捷函数 `h_flex` / `v_flex`**：这是 gpui-component 里出现频率最高的两个助手，分别返回「水平弹性布局」和「垂直弹性布局」的 `Div`：

[crates/ui/src/styled.rs:8-18](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L8-L18) —— `h_flex()` / `v_flex()` 两个自由函数，内部就是 `div().h_flex()`。

它们的真正实现是 `StyledExt` 上的方法：

[crates/ui/src/styled.rs:66-76](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L66-L76) —— `h_flex` 等价于「`flex` + 横向排列 + 子元素垂直居中」，`v_flex` 等价于「`flex` + 纵向排列」。注意 `h_flex` 额外调了 `items_center()`，所以水平排列时子元素默认垂直居中，这符合大多数 UI 场景的直觉。

**`StyledExt` 的定义与全局实现**：这是理解本模块的钥匙：

[crates/ui/src/styled.rs:54-59](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L54-L59) —— `StyledExt` 只对「`Styled + Sized`」的类型开放。
[crates/ui/src/styled.rs:196](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L196) —— `impl<E: Styled> StyledExt for E {}`。这一行就是 blanket impl：**凡是实现了 `Styled` 的类型 `E`，都自动实现 `StyledExt`**。正因为有它，你的 `div()` 才能直接调用 `h_flex()`、`paddings(...)`、`debug_red()`。

**`paddings` / `margins`：四边一起设**：网页里 `padding: 8px 12px;` 可以一次设四边，这里对应的是 `paddings` / `margins` 方法：

[crates/ui/src/styled.rs:78-100](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L78-L100) —— 接收一个 `Edges<L>`（含 top/bottom/left/right），拆成 `pt`/`pb`/`pl`/`pr` 四个方法调用。这就是「把一个语义化输入翻译成一组基础 `Styled` 方法」的典型写法。

**`popover_style`：把一组样式打包复用**：很多浮层组件（Popover、Tooltip、DropdownMenu）外观几乎一致（有背景、边框、圆角、阴影）。gpui-component 把这套外观提成一个方法：

[crates/ui/src/styled.rs:176-185](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L176-L185) —— 一次性应用背景、前景色、1px 边框、阴影、主题圆角。后面写浮层组件时你会反复用到它。

**`box_shadow` 构造器**：GPUI 的阴影结构略繁琐，这里提供了一个类 CSS 的构造函数：

[crates/ui/src/styled.rs:20-42](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L20-L42) —— 对照 CSS `box-shadow: x y blur spread color;` 的顺序传参，降低心智负担。

**`debug_*` 系列与 `font_weight!` 宏**：源码里还有一组调试用的红/蓝/黄/绿/粉描边方法（`debug_red` 等，仅 debug 构建生效），以及用 `macro_rules!` 批量生成的字重方法：

[crates/ui/src/styled.rs:44-52](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L44-L52) —— `font_weight!` 宏为每个字重常量生成一个方法（`font_thin`、`font_bold`…），避免手写重复代码。
[crates/ui/src/styled.rs:166-174](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L166-L174) —— 生成出的九个字重方法。

#### 4.1.4 代码实践

**实践目标**：亲手感受链式样式 API 与 `StyledExt` 的扩展方法。

**操作步骤**：

1. 打开 `examples/hello_world/src/main.rs`，找到 `Example::render` 里这段：

```rust
div()
    .v_flex()
    .gap_2()
    .size_full()
    .items_center()
    .justify_center()
```

2. 把 `.v_flex()` 改成 `.h_flex()`，并在窗口根视图上加一个 `.debug_red()`（记得 `use gpui_component::StyledExt;`）。
3. 用 `cargo run -p hello_world` 运行（该示例是独立 workspace 成员包，需用 `-p` 而非 `--example`，见 [u1-l2](u1-l2-build-and-run.md)）。

**需要观察的现象**：

- 布局从「上下排列」变成「左右排列」——验证 `h_flex` / `v_flex` 的差异。
- debug 构建下，根容器出现一圈红色边框；用 `cargo run --release` 再跑一次，边框消失——验证 `debug_red` 的 `cfg!(debug_assertions)` 守卫。

**预期结果**：一行改动即可看到布局方向与边框颜色的变化。本机无 GUI 环境时记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `div().h_flex()` 能直接调用 `h_flex`，而不需要我们手动 `impl StyledExt for Div`？

**参考答案**：因为 [styled.rs:196](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L196) 的 blanket impl `impl<E: Styled> StyledExt for E {}` 让所有实现 `Styled` 的类型（包括 `Div`）自动获得 `StyledExt` 的方法。

**练习 2**：`popover_style` 内部依次调用了哪几个基础 `Styled` 方法？

**参考答案**：`bg`（背景）、`text_color`（前景）、`border_1`（边框宽度）、`border_color`（边框颜色）、`shadow_lg`（大阴影）、`rounded`（主题圆角），见 [styled.rs:176-185](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L176-L185)。

---

### 4.2 Sizable：统一的尺寸抽象

#### 4.2.1 概念说明

一个按钮可能要「小号」「中号」「大号」，一个输入框、一个列表项、一个表格单元格也都是如此。如果每个组件各自定义一套「小=多少像素」，代码会非常混乱。

gpui-component 的解法是：定义一个统一的 `Size` 枚举，再用一个 `Sizable` trait 让所有组件都「能被设置尺寸」。这样用户侧的 API 完全统一：

```rust
Button::new("ok").small()    // 小号按钮
Switch::new("s").large()     // 大号开关
Badge::new().xsmall()        // 超小徽标
```

每个组件在内部把 `Size` 翻译成适合自己的具体像素值（字号、内边距、高度等）。

#### 4.2.2 核心流程

尺寸系统的核心是「**语义档位 → 排序权重 → 具体像素**」的三段式映射：

1. **语义档位**：`Size` 枚举有 `XSmall / Small / Medium / Large` 四档（外加 `Size(Pixels)` 用于自定义精确像素值），默认是 `Medium`。
2. **排序权重**：`as_f32()` 把四档映射成 0/1/2/3，让 `Size` 可以比较大小（用于 `max`/`min` 等运算）。
3. **具体像素**：组件自己写 `match size { ... }`，或借助 `StyleSized` trait 提供的现成映射（`input_h`、`input_px`、`button_text_size` 等），把档位翻译成字号、高度、内边距。

`Sizable` trait 只负责「把外部传入的尺寸存进组件」，真正的「尺寸→像素」翻译发生在组件的 `render` 里。这是一个很干净的职责分离。

#### 4.2.3 源码精读

**`Size` 枚举与排序权重**：

[crates/ui/src/styled.rs:198-207](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L198-L207) —— 四档加自定义像素，`Medium` 是 `#[default]`。
[crates/ui/src/styled.rs:210-218](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L210-L218) —— `as_f32` 把四档映射为 0/1/2/3，这是后续 `max`/`min` 比较的依据。

档位与字符串、像素值的对照表（供你查阅）：

| `Size` | `as_str()` | `as_f32()` | `table_row_height()` | `input_px()` / `input_py()` |
| --- | --- | --- | --- | --- |
| `XSmall` | `"xs"` | 0 | 26px | 4 / 0 |
| `Small` | `"sm"` | 1 | 30px | 8 / 2 |
| `Medium`（默认） | `"md"` | 2 | 32px | 12 / 8 |
| `Large` | `"lg"` | 3 | 40px | 16 / 10 |
| `Size(p)` | `"custom"` | p | p | 8 / 2 |

（像素值取自 [styled.rs:251-360](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L251-L360)。）

**`Sizable` trait**：

[crates/ui/src/styled.rs:391-418](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L391-L418) —— 这是核心。`with_size` 是唯一需要组件自己实现的方法（接收 `impl Into<Size>`，所以既能传 `Size::Small` 也能传 `px(30.)`）；`xsmall`/`small`/`large` 是带默认实现的便捷方法，内部都只是调用 `with_size`。

> 注意：`Sizable` **没有** `medium()` 方法，因为 `Medium` 是默认值——组件的 `new()` 一开始就设成 `Size::Medium`。

**`StyleSized`：现成的「档位→像素」翻译**：

[crates/ui/src/styled.rs:420-437](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L420-L437) —— 定义了一批按 `Size` 设置样式的辅助方法（`input_size`、`input_h`、`button_text_size`、`table_cell_size` 等）。
[crates/ui/src/styled.rs:476-485](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L476-L485) —— 以 `input_h` 为例，`Large→h_11`、`Medium→h_8`、`Small→h_6`、`XSmall→h_5`。组件实现时直接 `self.input_h(size)` 即可，不必每个组件都重写一遍。

**真实组件如何使用**：`Badge` 在 `render` 里用 `match self.size` 决定徽标直径和字号：

[crates/ui/src/badge.rs:108-112](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L108-L112) —— `Large→(24px, 14px)`、`Medium→(16px, 10px)`、`Small/XSmall→(10px, 8px)`。这就是「组件自己定义尺寸语义」的典型写法。

而 `Button` 实现得非常简洁，只把尺寸存起来：

[crates/ui/src/button/button.rs:405-410](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L405-L410) —— `Button` 的 `Sizable` 实现，仅 `self.size = size.into()`。真正的像素翻译在它的 `render`（[button.rs:437-438](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs#L437-L438)）里完成。

#### 4.2.4 代码实践

**实践目标**：通过阅读现成的单元测试，确认你对「档位→像素」映射的理解。

**操作步骤**：

1. 打开 [crates/ui/src/styled.rs:677-703](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L677-L703)，阅读 `test_table_row_height`、`test_size_from_str`、`test_size_as_str` 三个测试。
2. 对照上面的对照表，自己心算一遍 `Size::Large.table_row_height()` 应该是多少，再到 [styled.rs:679-683](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L679-L683) 看断言是否吻合。

**需要观察的现象**：测试断言 `Size::Large.table_row_height() == px(40.)`、`Size::Medium == px(32.)`，与表格一致。

**预期结果**：你能准确预测任意档位的 `table_row_height` 与 `as_str`。本仓库默认不运行测试（见 `CLAUDE.md`），所以这一步是「源码阅读型实践」，重点是建立映射直觉。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Sizable` 提供 `xsmall`/`small`/`large` 三个便捷方法，却**没有** `medium`？

**参考答案**：因为 `Size::Medium` 是默认值（`#[default]`），组件的 `new()` 已经把 `size` 初始化为 `Size::Medium`（见 [badge.rs:51](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L51)），所以不需要一个 `medium()` 方法去「切回默认」。

**练习 2**：`Size::Small.max(Size::Large)` 返回什么？为什么？

**参考答案**：返回 `Size::Small`。这里的 `max` 语义是「**更靠近 xs 的一档**」（即视觉上更小的那个），见 [styled.rs:317-325](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L317-L325)。它比较的是 `as_f32()`，Small=1 < Large=3，取较小者。这与日常「max=最大」的直觉相反，命名上需要注意——可以把它理解为「上限档位」。

---

### 4.3 RenderOnce：无状态组件设计

#### 4.3.1 概念说明

gpui-component 里有两种「写组件」的方式，初学者最容易混淆：

- **无状态组件（`RenderOnce`）**：像一个「函数」——你给它一堆参数，它返回一段 UI 描述，然后就完事了。它**每一帧都被重新构造一次**，不持有跨帧的可变状态。`Badge`、`Button`、`Switch` 都是这一类。这非常接近 React 的「函数组件」。
- **有状态视图（`Render`）**：像一个「对象」——它有持久身份（通过 `cx.new(|_| ...)` 创建成 View），内部状态在帧与帧之间保留，可以被观察、可以发起异步任务。`hello_world` 里的 `Example` 就是这一类。

gpui-component 的 60+ 组件**绝大多数是无状态 `RenderOnce` 组件**，因为它们只负责「把数据和回调翻译成 UI」，状态通常由外层的 View 持有。这是「Stateless design」原则的体现（见 `CLAUDE.md` 的组件设计原则）。

#### 4.3.2 核心流程

写一个无状态组件的标准三步：

1. **定义结构体**，派生 `#[derive(IntoElement)]`。`IntoElement` 让这个结构体能被当作子元素放进 `div().child(...)`。
2. **实现若干 trait**：至少 `impl RenderOnce`；按需 `impl Styled`（让它可被 `.bg().px()` 链式设置样式）、`impl Sizable`（让它支持 `.small()`）、`impl ParentElement`（让它能用 `.child()` 接收子元素）。
3. **在 `RenderOnce::render(self, window, cx)` 里返回 UI**。注意签名是 `self`（消费自身），不是 `&mut self`——这正体现了「用完即弃、每帧重建」。

与有状态视图的关键差异：

| 维度 | 无状态 `RenderOnce` 组件 | 有状态 `Render` 视图 |
| --- | --- | --- |
| 创建方式 | 每帧 `Foo::new(...)` 重新构造 | `cx.new(\|_\| Foo)`，只创建一次 |
| `render` 签名 | `render(self, window, cx)`（消费 self） | `render(&mut self, window, cx)`（可变借用） |
| 身份/状态 | 无持久身份，不持有跨帧状态 | 有持久身份（Entity/View），状态跨帧保留 |
| 派生宏 | `#[derive(IntoElement)]` | 由 `cx.new` 包装，实现 `Render` |
| 典型例子 | `Badge`、`Button`、`Switch` | `hello_world::Example`、`Root`、`DockArea` |

> **进阶补充**：说 `RenderOnce` 组件「无状态」是指它的**结构体本身**每帧重建。但它仍可以在渲染时读取外部状态（如 `cx.theme()`），甚至像 `Button` 那样通过 `window.use_keyed_state` 把小段状态（如焦点 handle）挂在 window 上跨帧存取。这属于「借位存状态」，不改变「结构体每帧重建」的本质。

#### 4.3.3 源码精读

**无状态组件范例：`Badge`**

[crates/ui/src/badge.rs:30-39](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L30-L39) —— 派生 `IntoElement`，字段都是「配置项」（style、count、variant、children、size）。没有任何跨帧的可变状态。

[crates/ui/src/badge.rs:101-102](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L101-L102) —— `impl RenderOnce for Badge`，`render(self, ...)` 消费自身。方法体里用 `self.style`、`self.children`、`self.size` 拼出一个 `div()`，然后 `self` 就被丢弃了。

注意 `Badge` 同时实现了 `Sizable`（[badge.rs:94-99](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L94-L99)）和 `ParentElement`（[badge.rs:88-92](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs#L88-L92)）——这正是 4.2 讲的尺寸能力与本讲的「组件 trait 组合」的结合。

**有状态视图范例：`Example`**

[examples/hello_world/src/main.rs:4-21](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs#L4-L21) —— `Example` 实现 `Render`，签名是 `render(&mut self, ...)`。它在 [main.rs:30](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/examples/hello_world/src/main.rs#L30) 通过 `cx.new(|_| Example)` 创建成 View，于是它有了持久身份，可以被放进 `Root` 当窗口主视图。

对比这两段，你就能直观看到：**组件（RenderOnce）是被「使用」的，视图（Render）是被「持有」的**。

#### 4.3.4 代码实践

**实践目标**：从源码层面确认两种 `render` 签名的差异。

**操作步骤**：

1. 打开 `crates/ui/src/badge.rs` 第 101 行，确认 `RenderOnce::render` 的第一个参数是 `self`（无 `&`、无 `mut`）。
2. 打开 `examples/hello_world/src/main.rs` 第 5 行，确认 `Render::render` 的第一个参数是 `&mut self`。

**需要观察的现象**：两处签名不同——一个消费所有权，一个只是可变借用。

**预期结果**：你能用自己的话解释「为什么 `Badge` 每帧重建而 `Example` 不会」。这是纯源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果我要做一个「带持久选中状态的复选框」，应该用 `RenderOnce` 还是 `Render`？

**参考答案**：**视状态归属而定**。gpui-component 的实际做法是：`Checkbox` 本身是无状态 `RenderOnce` 组件（与 `Badge` 同类），选中状态由「调用方」以 `.checked(bool)` 参数传入、用 `.on_change` 回调上交。真正的持久状态放在外层的 View（`Render`）里。这就是「无状态组件 + 有状态视图」的经典分工——组件只管渲染和上报，状态归 View 管。

**练习 2**：`#[derive(IntoElement)]` 对一个 `RenderOnce` 组件的作用是什么？

**参考答案**：它生成 `IntoElement` 的实现，使该结构体可以被当作子元素传给 `.child(...)` / `.children(...)`。没有这个派生，你的组件就无法被嵌进其它元素的子元素列表。

---

## 5. 综合实践

把本讲的三个模块（Styled、Sizable、RenderOnce）串起来，亲手写一个最小的 **Card** 无状态组件。

**需求**：

- 用 `Styled` 设置圆角、阴影、内边距、背景色。
- 通过 `Sizable` 支持「小（Small）」和「中（Medium，默认）」两档尺寸：小号内边距小、字号小；中号更大。
- 用 `RenderOnce` + `#[derive(IntoElement)]` 让它能作为子元素使用，并实现 `ParentElement` 以接收内容。

**参考实现（示例代码，非项目原有代码）**：

```rust
use gpui::{
    App, IntoElement, ParentElement, RenderOnce, SharedString, Styled, Window, div,
};
use gpui_component::{ActiveTheme, Size, Sizable, StyledExt};

// 1) 无状态组件：派生 IntoElement 才能作为子元素
#[derive(IntoElement)]
pub struct Card {
    title: Option<SharedString>,
    children: Vec<gpui::AnyElement>,
    size: Size,           // 2) 尺寸字段，默认 Medium
}

impl Card {
    pub fn new() -> Self {
        Self {
            title: None,
            children: Vec::new(),
            size: Size::default(),   // Medium
        }
    }

    pub fn title(mut self, title: impl Into<SharedString>) -> Self {
        self.title = Some(title.into());
        self
    }
}

// 让 Card 支持 .child(...)
impl ParentElement for Card {
    fn extend(&mut self, elements: impl IntoIterator<Item = gpui::AnyElement>) {
        self.children.extend(elements);
    }
}

// 3) 让 Card 支持 .small() / .large()
impl Sizable for Card {
    fn with_size(mut self, size: impl Into<Size>) -> Self {
        self.size = size.into();
        self
    }
}

impl RenderOnce for Card {
    fn render(self, _window: &mut Window, cx: &mut App) -> impl IntoElement {
        // 根据尺寸档位翻译成具体像素：4.2 讲的核心思想
        let (pad, title_size) = match self.size {
            Size::Small | Size::XSmall => (gpui::px(8.), gpui::px(12.)),
            _ => (gpui::px(16.), gpui::px(16.)),
        };

        // 4.1 讲的链式样式 API + StyledExt
        div()
            .v_flex()                           // 垂直弹性布局
            .gap_2()
            .p_(pad)                            // 内边距随尺寸变化
            .bg(cx.theme().background)          // 背景取自主题（u2-l1）
            .border_1()
            .border_color(cx.theme().border)
            .rounded(cx.theme().radius)         // 主题圆角
            .shadow_sm()                        // 阴影
            .when_some(self.title, |this, title| {
                this.child(div().text_size(title_size).font_semibold().child(title))
            })
            .children(self.children)
    }
}
```

**使用方式**：

```rust
// 在某个 View 的 render 里
Card::new()
    .title("用户信息")
    .small()                       // 切到小号
    .child("Alice")
```

**需要观察的现象**：

- `.small()` 与不调用时（默认 Medium）相比，Card 的内边距和标题字号明显变小——验证 `Sizable` + 自定义尺寸映射生效。
- 修改 `.shadow_sm()` 为 `.shadow_lg()`，阴影变浓；去掉 `.rounded(...)`，圆角消失——验证 `Styled` 链式 API 的逐项作用。

**预期结果**：你得到一个可复用、可设尺寸、外观随主题变化的卡片组件，且它本身是一个干净的无状态 `RenderOnce` 组件。无 GUI 环境时记为「待本地验证」，但代码能否通过 `cargo build` 的类型检查可以在本地确认。

## 6. 本讲小结

- **Styled**：GPUI 提供基础 `Styled` trait（`flex`/`gap`/`size`/`bg`…），gpui-component 通过 `StyledExt` 扩展 trait + blanket impl（[styled.rs:196](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/styled.rs#L196)）让任何 `div()` 都能调用 `h_flex()`、`paddings()`、`popover_style()`、`debug_red()` 等便捷方法。
- **Sizable**：统一的 `Size` 枚举（xs/sm/md/lg/自定义像素）+ `Sizable` trait（`with_size` + `xsmall`/`small`/`large`）让 60+ 组件共用一套尺寸 API；真正的「档位→像素」翻译在组件 `render` 里完成，可借助 `StyleSized` 复用。
- **RenderOnce**：无状态组件派生 `IntoElement` 并实现 `render(self, ...)`，每帧重建、不持有跨帧状态，是 gpui-component 组件的默认形态；有状态状态归 `Render` 视图持有。
- **三者关系**：`Badge`/`Button`/`Switch` 同时实现 `Styled`/`Sizable`/`RenderOnce`，是这三块公共基础结合的范例——写自己的组件时，按需实现这几个 trait 即可获得样式、尺寸与「可作子元素」的能力。

## 7. 下一步学习建议

- 下一讲 [u2-l3 图标系统：Icon 与 IconName](u2-l3-icon-system.md) 会讲解 `Icon` 元素——它同样是一个无状态组件，并和 `Size`/`Sizable` 紧密配合（图标大小随尺寸变化），正好巩固本讲。
- 之后 [u2-l4 事件、元素扩展与焦点陷阱](u2-l4-events-element-ext.md) 会回到 `element_ext.rs`，深入 `ElementExt` 与交互事件，补全组件交互能力。
- 想立刻看到「真实组件」如何组合这些 trait，可直接精读 [badge.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/badge.rs) 与 [button/button.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/button/button.rs)，它们是本讲内容的最佳实战参考。
