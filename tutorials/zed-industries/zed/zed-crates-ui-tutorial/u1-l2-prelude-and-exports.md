# prelude：导入约定与公共 API 地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `use ui::prelude::*` 这一行为什么几乎是 Zed 所有 UI 代码的第一行，它背后依赖 Rust 的哪个语言机制。
2. 拿到 [src/prelude.rs](src/prelude.rs) 后，能按「gpui 转发 / 显式 gpui 类型 / 能力 trait / 设计令牌 / 组件与布局函数」五层读懂整个文件，知道每个条目大概从哪来、用来做什么。
3. 区分 `ui::prelude` 与 `ui::component_prelude` 两套导入的适用场景，理解为什么 `button.rs` 这类组件文件会同时导入两者。
4. 理解 ui crate 的扁平化 re-export 结构：`ui::Button`、`ui::prelude::Button`、`crate::components::button::Button` 指向同一个真实定义，以及这种结构对下游使用体验和 crate 内部重构自由的影响。

## 2. 前置知识

本讲建立在 u1-l1（ui crate 的定位与整体结构）之上，另外需要理解三个 Rust 基础概念：

- **glob 导入（glob import）**：`use some::module::*;` 把目标模块里所有 `pub` 条目一次性引入当前作用域。prelude 的本质就是一次精心挑选过的 glob 导入。
- **trait 方法的作用域规则**：Rust 中调用一个由 trait 提供的方法（比如 `.on_click(...)`），即使类型本身已经在作用域里，**该 trait 也必须处于作用域中**，否则编译器报「找不到方法」。这就是为什么 UI 代码需要大量导入 trait——而逐个导入太痛苦，prelude 把它们一网打尽。
- **re-export（再导出）**：`pub use other::Item;` 让 `Item` 可以通过当前模块路径访问。u1-l1 已经见过 `pub use components::*` 这种「模块私有、条目公有」的倾倒式再导出，本讲会看到它与 prelude 之间形成的双向引用。

一个不熟悉 Rust 的读者也只需记住一句话：**prelude 是组件库递给你的「工具箱」，一个星号导入，常用的类型、trait、函数全部就位。**

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/prelude.rs` | 本讲主角。定义 `ui::prelude`，聚合 gpui 原语、能力 trait、设计令牌与常用组件，共 35 行 |
| `src/component_prelude.rs` | 组件预览体系的专用 prelude，仅 6 行，额外的 `ComponentId`、`ComponentStatus`、`Documented` |
| `src/ui.rs` | crate 根（lib 入口）。声明 6 个模块并把 `components`、`prelude`、`styles`、`animation_ext` 的内容倾倒到 crate 根 |
| `../gpui/src/prelude.rs` | 被转发的上游 prelude，提供 `IntoElement`、`Styled`、`Render` 等核心 trait |
| `src/traits.rs` 与 `src/traits/*.rs` | 能力 trait 的声明处，prelude 逐个 glob 转发它们 |
| `src/styles.rs` | 设计令牌模块根，prelude 从中挑拣再导出 |
| `src/components/stack.rs` | `h_flex` / `v_flex` 布局快捷函数的定义处，prelude 尾部的函数来自这里 |
| `src/components/button/button.rs`、`src/components/facepile.rs` | 真实组件文件，展示「双 prelude 同时导入」的实际写法与 doc 测试 |
| `../component/src/component.rs` | `Component` trait、`ComponentStatus`、`ComponentScope` 的定义处，component_prelude 转发的就是这些 |

> 说明：本讲引用的 gpui 与 component 文件在 `crates/gpui`、`crates/component` 下，永久链接会带上完整相对路径。

## 4. 核心概念与源码讲解

### 4.1 为什么需要 prelude：一个星号解决「trait 不在作用域」问题

#### 4.1.1 概念说明

设想你在 Zed 里写一行最普通的按钮代码：

```rust
Button::new("save", "保存").on_click(|_, _, _| { /* ... */ })
```

这行代码看似只用到 `Button` 一个类型，实际上它至少触及四个名字：

- `Button`：组件类型，来自 ui crate；
- `on_click`：**不是 `Button` 的固有方法**，而是 `Clickable` trait 提供的方法；
- 闭包参数隐含的 `ClickEvent`、`Window`、`App` 类型（由 trait 方法签名约定）；
- 如果接着写 `.h_flex()`、`.gap_2()`，又是 `StyledExt` / `Styled` 两个 trait 的方法。

Rust 规定 trait 必须在作用域内，其方法才能被调用。如果逐个导入，每个 UI 文件开头都要写十几行 `use`。prelude 把「写 Zed UI 几乎必然用到」的名字打包成一个模块，于是全仓库才能统一用一行 `use ui::prelude::*;` 开头——按 `^use ui::prelude::*;` 精确匹配统计，当前仓库 `crates/` 下至少有 42 个 `.rs` 文件以这一行开头（例如 `crates/picker/src/head.rs`、`crates/settings_ui/src/components/number_field.rs`），这还不算各种变体写法。

#### 4.1.2 核心流程

prelude 的工作方式可以概括为一条「名字注入链」：

```text
定义处（如 traits/clickable.rs 里的 Clickable）
   │  pub use（模块根 traits.rs / components.rs / styles.rs 聚合）
   ▼
ui crate 内部模块
   │  prelude.rs 挑拣 + glob 再导出
   ▼
ui::prelude
   │  下游文件 use ui::prelude::*
   ▼
下游文件作用域 → Button / .on_click / .h_flex 全部可用
```

注意这条链是「单向消费」的：下游文件永远只认 `ui::prelude` 这个稳定入口，至于 `Clickable` 住在大模块还是小模块、文件叫什么名字，下游完全不关心。

#### 4.1.3 源码精读

prelude 文件第一行的文档注释直接点明了它的定位：

- [src/prelude.rs:L1-L3](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L1-L3)：文档注释写着「The prelude of this crate. When building UI in Zed you almost always want to import this.」（本 crate 的 prelude，在 Zed 中写 UI 时几乎总是要导入它），第一句代码就是 `pub use gpui::prelude::*;` —— 把 gpui 框架的 prelude 原样转发，这是整个链条的起点。

`Clickable` trait 的定义极小，却是 prelude 存在意义的最佳例证：

- [src/traits/clickable.rs:L4-L9](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits/clickable.rs#L4-L9)：定义 `Clickable` trait，只有两个方法——`on_click`（设置点击回调）与 `cursor_style`（设置悬停光标样式）。任何元素想获得 `.on_click()`，这个 trait 就得在作用域里。

Button 组件文件里的 doc 测试就是「只用 prelude 写按钮」的官方示范：

- [src/components/button/button.rs:L26-L33](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L26-L33)：doc 示例只有两行——`use ui::prelude::*;` 之后直接 `Button::new("button_id", "Click me!").on_click(...)`。注意 doc 测试是真实会被编译执行的代码（后文实践环节会用到这一点），它证明仅凭 prelude 就足以写出带点击事件的按钮。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「trait 不在作用域就调不了方法」这一机制。
2. **操作步骤**：
   - 打开任一下游文件，例如 `crates/picker/src/head.rs`，找到第 4 行的 `use ui::prelude::*;`。
   - 在自己的本地克隆中，临时注释掉这一行，运行 `cargo check -p picker`。
   - 阅读编译错误：哪些方法「not found」？报错里提到的 trait 名是否都能在 `src/prelude.rs` 里找到？
   - 恢复该行，再次 `cargo check -p picker` 确认通过。
3. **需要观察的现象**：`on_click`、样式方法等会集体报错，且错误信息会提示「an import is missing」或列出候选 trait。
4. **预期结果**：注释掉 prelude 后编译失败，恢复后成功；失败清单与 prelude 条目高度重合。（命令运行结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 Zed 不把所有 trait 方法直接做成各组件的固有（inherent）方法，从而省掉 prelude？

**参考答案**：固有方法无法泛化。`Clickable` 这类能力 trait 要同时服务于 `Button`、`ButtonLike`、`IconButton` 等多种类型（后一讲会看到它们各自 `impl Clickable`），用 trait 才能让「凡是实现了 Clickable 的东西都有 `.on_click()`」这一契约被泛型代码（如 `fn wire_up<T: Clickable>(el: T)`）引用。代价是 trait 必须进作用域，于是用 prelude 一次性解决。

**练习 2**：`use ui::prelude::*` 与 `use ui::prelude::Button` 有什么本质区别？

**参考答案**：前者是 glob 导入，把 prelude 里全部 `pub` 名字引入作用域（可能带来重名冲突，但省事，是 Zed 的约定）；后者是精确导入，只引入 `Button` 一个名字，但会导致 trait 方法（如 `Clickable::on_click`）仍不在作用域，还得补导 trait。所以 UI 代码几乎总是选前者。

### 4.2 逐行精读 prelude.rs：五层来源构成的一张 API 地图

#### 4.2.1 概念说明

[src/prelude.rs](src/prelude.rs) 只有 35 行，但它的条目来自五个不同层次。把它当作「ui crate 公共 API 的地图」来读，后续所有讲义提到的类型都能在这张图上对号入座：

| 层 | prelude.rs 行号 | 代表条目 | 解决什么问题 |
| --- | --- | --- | --- |
| ① gpui prelude 转发 | L3 | `IntoElement`、`Styled`、`Render`、`RenderOnce`、`ParentElement`、`StatefulInteractiveElement`、`FluentBuilder` 等 | GPUI 框架的核心 trait，写任何元素都离不开 |
| ② gpui 显式类型与函数 | L4-L8 | `div`、`px`、`rems`、`relative`、`SharedString`、`Div`、`AnyElement`、`App`、`Context`、`Window`、`ElementId`、`Pixels`/`Rems` 等 | gpui prelude 里没有、但 UI 代码高频使用的具体类型与构造函数 |
| ③ 能力 trait | L20-L25 | `Clickable`、`Disableable`、`Toggleable`、`FixedWidth`、`StyledExt`、`VisibleOnHover` | 让不同组件暴露一致的交互方法（u3-l4 详讲） |
| ④ 设计令牌与样式工具 | L15-L19 | `DynamicSpacing`、`DefaultAnimations`、`TextSize`、`Severity`、`PlatformStyle`、`StyledTypography`、`rems_from_px`、`vw`/`vh` | 颜色/字号/间距/动画等与主题联动的样式语言（u2 单元详讲） |
| ⑤ 组件与布局函数 | L10-L13、L26-L34 | `Button` 家族、`Icon` 家族、`Label`/`Headline` 家族、`h_flex`/`v_flex`、`h_group_*`/`v_group_*`，以及组件预览体系的 `Component`、`example_group` 等 | 最常用的成品组件与快捷布局函数 |

最后一行 L35 还有一项容易忽略的转发：`pub use theme::ActiveTheme;`。`ActiveTheme` 是 theme crate 提供的 trait，`cx.theme().colors().xxx` 这类取主题色的调用全靠它在作用域里——prelude 顺手把它也包了，下游 thus 不必再单独 `use theme::ActiveTheme`。

#### 4.2.2 核心流程

五层条目汇入 prelude 后，下游一行导入即可覆盖一条典型渲染链路的全部依赖：

```text
use ui::prelude::*;
   │
   ├─ ① Styled / ParentElement      → div().flex().gap_2() 有地方调用
   ├─ ② div / SharedString          → 容器与字符串类型就位
   ├─ ③ StyledExt                   → .h_flex() 可用
   ├─ ⑤ Label / Button              → 成品组件就位
   ├─ ③ Clickable                   → Button 的 .on_click() 可解析
   └─ ⑤ + L35 ActiveTheme           → 组件内部取主题色（cx.theme()）
```

对照真实调用：`h_flex().gap_2().child(Label::new("hi"))` 这一行就用到了 ①②③⑤ 四层。

#### 4.2.3 源码精读

逐段过一遍这 35 行：

- [src/prelude.rs:L3-L8](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L3-L8)：第 ③ 段的「① 转发 + ② 显式补充」。`pub use gpui::prelude::*` 之后，再从 gpui 精挑十余个具体条目——因为 gpui 的 prelude 偏重 trait，而 UI 代码还需要 `div()` 工厂函数、`px()`/`rems()` 单位函数、`SharedString` 字符串类型这些「非 trait」名字。
- [../gpui/src/prelude.rs:L5-L9](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/prelude.rs#L5-L9)：被转发的上游内容。注意两个写法特别的条目：`AppContext as _` 与 `TaskExt as _`——`as _` 表示「导入 trait 的方法实现但不绑定名字」，用于只关心方法、不关心类型名的 trait；`FluentBuilder` 则提供了后文常见的 `.when()` / `.when_some()`。
- [src/prelude.rs:L20-L25](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L20-L25)：六个能力 trait 模块逐一 glob 转发。这些模块在 [src/traits.rs:L1-L8](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits.rs#L1-L8) 中声明，其中 `clickable`、`disableable`、`fixed`、`styled_ext`、`toggleable`、`visible_on_hover` 六个进入 prelude，而 `animation_ext` 与 `transformable` **没有**进入（见 4.4 节的反例）。
- [src/prelude.rs:L26-L34](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L26-L34)：组件层。注意这里只挑了四个家族——Button（含 `ButtonStyle`、`ButtonSize`、`SelectableButton`）、Headline、Icon（含 `IconName`、`IconSize`、`IconPosition`）、Label（含 `LabelCommon`、`LabelSize`、`LineHeightStyle`、`LoadingLabel`），外加 `h_flex`/`v_flex` 与 `h_group_*`/`v_group_*` 布局函数。ContextMenu、Modal、Tab 等更大的组件**不在 prelude 里**，需要时走 `ui::Xxx` 路径显式导入。
- [src/components/stack.rs:L5-L15](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/stack.rs#L5-L15)：prelude 尾部那两个函数的真身。`h_flex()` 就是 `div().h_flex()` 的快捷方式，而 `div().h_flex()` 里的 `.h_flex()` 又来自 `StyledExt`：
- [src/traits/styled_ext.rs:L26-L39](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits/styled_ext.rs#L26-L39)：`StyledExt` trait 的默认方法 `h_flex` 等价于 `.flex().flex_row().items_center()`，`v_flex` 等价于 `.flex().flex_col()`。也就是说 prelude 同时给了你「函数入口」（`h_flex()`）和「方法入口」（`.h_flex()`）两种等价写法。
- [src/prelude.rs:L17-L19](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L17-L19)：从 `crate::styles` 中**挑拣**再导出的令牌（`PlatformStyle`、`Severity`、`StyledTypography`、`TextSize`、`rems_from_px`、`vw`、`vh`）。对比 [src/styles.rs:L11-L18](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/styles.rs#L11-L18)：styles 模块内部是全量 `pub use xxx::*` 倾倒，而 prelude 只取少数最常用的——这体现了 prelude 的「克制」：入口越精简，下游命名空间越干净。
- [src/prelude.rs:L35](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L35)：`pub use theme::ActiveTheme;`。跨 crate 转发兄弟 crate 的 trait，让下游少写一行导入，也把「组件取色必须经主题」这一设计约定固化进了导入习惯。

#### 4.2.4 代码实践

1. **实践目标**：建立「我要用 X，X 属于 prelude 五层中的哪一层」的检索直觉。
2. **操作步骤**：
   - 打开 [src/prelude.rs](src/prelude.rs)，对照 4.2.1 的表格，把 35 行逐行归层（自己画一张五列清单）。
   - 随机抽三个条目做「溯源」：例如 `Severity`（→ `src/styles/severity.rs`）、`IconName`（→ icons crate，经 `src/components/icon.rs`）、`FluentBuilder`（→ gpui 的 `util::FluentBuilder`）。用 IDE 跳转或 `grep -rn "pub enum Severity" src/` 验证你的归层是否正确。
   - 故意找反例：`ContextMenu` 在 prelude 里吗？`Color` 在吗（提示：L27）？`CommonAnimationExt` 在吗？
3. **需要观察的现象**：溯源结果与归层一致；`ContextMenu` 不在 prelude、`Color` 在 L27、`CommonAnimationExt` 不在。
4. **预期结果**：完成一张五层清单，并能说出「prelude 收录的是高频条目而非全部 API」。

#### 4.2.5 小练习与答案

**练习 1**：prelude 为什么要同时 `pub use gpui::prelude::*`（L3）和再显式列一批 gpui 条目（L4-L8）？只保留 L3 行不行？

**参考答案**：不行。gpui 的 prelude 以 trait 为主（见 gpui/src/prelude.rs），不含 `div`、`px`、`rems`、`SharedString`、`AnyElement` 这些具体类型与函数。L4-L8 是对 trait 清单的「非 trait 补充」，两者合起来才凑齐写 UI 的最小集合。

**练习 2**：`rems_from_px`、`vw`、`vh` 这三个函数为什么值得进 prelude，而 `DynamicSpacing` 的具体档位枚举不需要单独导入？

**参考答案**：前者是自由函数，调用形式是 `rems_from_px(16)`，名字必须进作用域才能用；后者 `DynamicSpacing` 是一个枚举类型（L15 已导出类型本身），其档位变体（如 `Base16`）通过路径 `DynamicSpacing::Base16` 访问，类型在作用域即可，无需逐变体导入。

**练习 3**：下游文件里 `cx.theme().colors().border` 能编译通过，至少依赖 prelude 的哪一行？

**参考答案**：L35 的 `pub use theme::ActiveTheme;`。`theme()` 是 `ActiveTheme` trait 提供的方法，trait 不在作用域则该方法无法解析。

### 4.3 component_prelude：组件预览体系的专用导入

#### 4.3.1 概念说明

ui crate 还有第二个 prelude：[src/component_prelude.rs](src/component_prelude.rs)，为「实现组件预览体系」这一特定场景准备。两套 prelude 的条目对比如下：

| 条目 | `ui::prelude` | `ui::component_prelude` |
| --- | --- | --- |
| `Component`、`ComponentScope`、`example_group`、`example_group_with_title`、`single_example` | ✅（L10-L12） | ✅（L2-L3） |
| `RegisterComponent`（派生宏） | ✅（L13） | ✅（L6） |
| `ComponentId`、`ComponentStatus` | ❌ | ✅（L2） |
| `Documented`（documented crate 的派生宏） | ❌ | ✅（L5） |
| gpui 原语、能力 trait、设计令牌、成品组件 | ✅ | ❌ |

可见 `component_prelude` = prelude 中「预览相关」那部分 + 三个额外条目，且**不含任何渲染所需的 gpui/组件条目**。这就是为什么真实组件文件总是两个一起导入：

```rust
use crate::component_prelude::*;  // 预览体系：Component、Documented、RegisterComponent...
use crate::prelude::*;            // 渲染所需：div、Styled、Button...
```

#### 4.3.2 核心流程

一个组件接入预览体系的典型流水线（u8-l5 会完整展开）：

```text
struct 上派生 #[derive(IntoElement, Documented, RegisterComponent)]
        │
        ├─ Documented       → 文档注释变为运行时可读的 Self::DOCS（描述文案来源）
        ├─ RegisterComponent→ 注册进 ComponentRegistry（workspace: open component preview 可查看）
        └─ impl Component   → 提供 scope()/description()/preview()
                                  │
                                  └─ preview 内部用 single_example / example_group 组织样例
```

这条流水线上用到的名字（`Documented`、`RegisterComponent`、`Component`、`ComponentStatus`……）恰好就是 component_prelude 的全部内容——它是「为组件作者准备的工具包」，而 prelude 是「为所有 UI 构建者准备的工具包」。

#### 4.3.3 源码精读

- [src/component_prelude.rs:L1-L6](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/component_prelude.rs#L1-L6)：全文仅 6 行：从 component crate 转发 `Component`、`ComponentId`、`ComponentScope`、`ComponentStatus` 与三个样例组织函数，从 documented crate 转发 `Documented`，从 ui_macros 转发 `RegisterComponent`。注意它没有文档注释、也没有任何 gpui 条目——定位非常纯粹。
- [src/components/button/button.rs:L1-L9](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L1-L9)：Button 组件文件的开头。第 1 行导入 `crate::component_prelude::*`（为了实现预览），第 8 行又在同一组 `use` 里导入 `crate::prelude::*`（为了渲染）。这是「双 prelude」的标准写法，facepile 同样如此：
- [src/components/facepile.rs:L1-L2](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/facepile.rs#L1-L2)：Facepile 连续两行分别导入两个 prelude，并在 [L30](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/facepile.rs#L30) 一次性派生 `#[derive(IntoElement, Documented, RegisterComponent)]`——两个 prelude 各自供着派生宏之一（`IntoElement` 来自 gpui、经 prelude 生效；`Documented`/`RegisterComponent` 来自 component_prelude）。
- [../component/src/component.rs:L170-L258](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/component/src/component.rs#L170-L258)：`Component` trait 定义。文档注释说明了它的用途：实现该 trait 后即可派生 `RegisterComponent`，从而通过 `workspace: open component preview` 预览组件；其中 `status()`（L193）返回的正是 component_prelude 独有的 `ComponentStatus`。
- [../component/src/component.rs:L269-L325](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/component/src/component.rs#L269-L325)：`ComponentStatus`（WorkInProgress / EngineeringReady / Live / Deprecated）与 `ComponentScope`（Agent、DataDisplay、Input、Layout 等）两个枚举——预览界面的分组与状态标签就靠它们，这也是 `ComponentId`/`ComponentStatus` 只进 component_prelude 的原因：普通业务代码根本用不到。
- [../component/src/component_layout.rs:L185-L205](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/component/src/component_layout.rs#L185-L205)：`single_example`、`example_group`、`example_group_with_title` 三个样例组织函数的定义，两套 prelude 都转发它们（预览样例的搭建太常用了）。

#### 4.3.4 代码实践

1. **实践目标**：摸清 component_prelude 在 crate 内的实际使用范围。
2. **操作步骤**：
   - 在 `crates/ui` 下执行 `grep -rn "component_prelude" src/`。
   - 对每个命中文件，查看它导入了什么、文件里出现了哪些与预览相关的宏或 trait（找 `derive(`、`impl Component`）。
   - 再挑一个**没有**命中 component_prelude 的组件文件（例如 `src/components/divider.rs`），对比它的导入区少了哪些东西，思考：它为什么可以不用？
3. **需要观察的现象**：命中文件（当前为 `button/button.rs`、`facepile.rs`、`notification/alert_modal.rs`、`styles/color.rs` 等）都在实现 `Component`/派生相关内容；未命中文件通常只是不接入预览体系。
4. **预期结果**：得出结论「component_prelude 只在接入组件预览体系的文件里出现，属于可选工具包」。（grep 命中清单可能随版本演进，以本地结果为准。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 `ComponentId`、`ComponentStatus`、`Documented` 也塞进主 prelude，会有什么问题？

**参考答案**：会污染所有下游文件的命名空间。主 prelude 被 40+ 个文件无条件 glob 导入，其中绝大多数文件根本不实现组件预览；而预览相关条目只在极少数组件定义文件里使用。拆成两个 prelude 是「按场景收窄导入面」的常规做法——代价只是组件文件多写一行 `use`。

**练习 2**：一个只调用（而不实现）组件的下游文件，需要导入 component_prelude 吗？

**参考答案**：不需要。调用 `Button::new(...)` 只需要主 prelude；component_prelude 的条目服务于「定义可预览组件」这一侧（实现 `Component` trait、派生 `RegisterComponent`/`Documented`），纯消费方完全用不到。

### 4.4 扁平化 re-export：crate 根与 prelude 的双向引用

#### 4.4.1 概念说明

u1-l1 讲过 `src/ui.rs` 把私有模块 `components`、`styles` 的内容倾倒到 crate 根。本讲补上另一半：crate 根与 prelude 之间存在**双向引用**——

- 根 → prelude：`pub use prelude::*;` 把 prelude 的条目也提升到 `ui::` 根路径；
- prelude → 根：prelude 里的 `pub use crate::{Button, ...}` 又从根（经 `components::*` 得到）往回拿组件。

这不是循环定义，因为 Rust 的 glob 再导出最终都解析到**唯一的真实定义处**（例如 `Button` 真正定义在 `src/components/button/button.rs`）。结果是一个名字拥有多条等价路径：

```text
ui::Button
ui::prelude::Button
ui::components::button::Button   ← 仅 crate 内部可用（components 模块是私有的）
```

对下游的直接影响是「怎么写都对」：`use ui::Button` 与 `use ui::prelude::*` 拿到的是同一个类型；对维护者的直接影响是「重构自由」：`components` 模块怎么重排、文件怎么迁移，只要 `components.rs` 里的 `pub use` 跟着改，下游的 `ui::Xxx` 与 `ui::prelude::Xxx` 都不受影响。

#### 4.4.2 核心流程

名字解析的闭环比想象中更大一圈：

```text
src/components/stack.rs: pub fn h_flex()          ← 唯一真实定义
        ↑                        ↑
components.rs: pub use stack::*   prelude.rs L31: pub use crate::{h_flex, v_flex}
        ↑                        ↑
ui.rs L17: pub use components::*  ui.rs L18: pub use prelude::*
        └──────────┬─────────────┘
                   ▼
      对外可见：ui::h_flex 与 ui::prelude::h_flex（同一函数）
```

#### 4.4.3 源码精读

- [src/ui.rs:L10-L15](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/ui.rs#L10-L15)：六个顶层模块声明。注意只有 `component_prelude`、`prelude`、`utils` 是 `pub mod`，而 `components`、`styles`、`traits` 是私有 `mod`——外部无法走 `ui::components::xxx` 路径，只能从根或 prelude 拿，这正是扁平化的「围墙」。
- [src/ui.rs:L17-L20](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/ui.rs#L17-L20)：四行倾倒式再导出——`components::*`、`prelude::*`、`styles::*`、`traits::animation_ext::*`。前三者构成 4.4.1 所说的双向引用；第四行是个重要反例：
- [src/traits.rs:L1-L8](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits.rs#L1-L8) 中的 `animation_ext` 与 `transformable` 两个模块**没有**被 prelude 收录（见 prelude L20-L25 的六连 glob），只通过 [src/ui.rs:L20](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/ui.rs#L20) 挂在根上。因此要用 `.with_keyed_rotate_animation()`（u8-l2 会讲）就必须显式 `use ui::CommonAnimationExt;`——crate 内部同样如此，[src/components/button/button.rs:L5](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L5) 就是按完整路径单独导入 `CommonAnimationExt` 的。「在根上」不等于「在 prelude 里」，这是读代码时容易混淆的点。
- [src/prelude.rs:L26-L27](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L26-L27)：prelude 从 `crate::`（即 crate 根）往回拿 `Button`、`Color` 等条目——由于根上的这些名字本身来自 L17 的 `pub use components::*` / L19 的 `pub use styles::*`，两个方向的 glob 就形成了 4.4.2 图中的闭环。

#### 4.4.4 代码实践

1. **实践目标**：验证「多路径、同一物」，并找出「在根但不在 prelude」的条目。
2. **操作步骤**：
   - 书面追踪 `Label` 的四条路径：`ui::Label`、`ui::prelude::Label`、`ui::components::label::Label`（仅内部）、真实定义 `src/components/label/label.rs`，标出每一步经过的 `pub use`。
   - 对照 [src/ui.rs:L17-L20](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/ui.rs#L17-L20) 与 [src/prelude.rs:L15-L35](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L15-L35)，列出「根上有、prelude 没有」的清单（至少应包含 `CommonAnimationExt`、`Transformable`）。
   - 可选：在支持跳转的编辑器里分别从 `ui::Button` 与 `ui::prelude::Button` 跳转定义，确认落在同一处。
3. **需要观察的现象**：两条跳转路径终点相同；反例清单与预期一致。
4. **预期结果**：能画出 `Label` 的路径图，并说出「prelude 是根 API 的精选子集 + 少量跨 crate 转发」这一结论。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `components` 模块声明为私有 `mod` 而不是 `pub mod`？如果改成 `pub mod` 会发生什么？

**参考答案**：私有意味着外部只能通过 crate 根/prelude 这两个稳定入口访问组件，crate 内部随意调整 `components.rs` 的模块划分与文件位置都不会构成对外的破坏性变更。若改成 `pub mod`，`ui::components::button::Button` 就成了公开承诺的 API 路径，任何内部重组（拆文件、改目录）都等于改公共接口，维护成本大增。

**练习 2**：下游代码写 `use ui::prelude::*;` 之后又调用 `.with_keyed_rotate_animation(...)` 却编译失败，最可能的原因是什么？

**参考答案**：提供该方法的 `CommonAnimationExt` trait 只在 crate 根（`ui.rs` L20）再导出，**不在 prelude 里**（prelude L20-L25 的六个 trait glob 不含 `animation_ext`）。补一行 `use ui::CommonAnimationExt;` 即可。

## 5. 综合实践

**任务：写一个「prelude 自助游」函数——只允许一行 `use ui::prelude::*;`，组合 `div`、`h_flex`、`Label` 与 `Button`，返回 `impl IntoElement`，并用 `cargo check` 验证。**

1. **实践目标**：证明仅凭 prelude 就能搭出一个完整的元素树，从而把本讲的五层 API 地图「用一遍」。

2. **操作步骤**：

   - 第一步（先验证环境）：在仓库根目录运行 `cargo test -p ui --doc`。它会编译并执行 ui crate 里的所有 doc 测试，其中就包括 [button.rs:L26-L33](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L26-L33) 那些 `use ui::prelude::*;` 开头的官方示例——这是「prelude 足以写 UI」的现成证据。（编译 Zed 工作区耗时较长，结果待本地验证。）

   - 第二步（新示例文件，不改动任何现有源码）：在本地克隆中新建 `crates/ui/examples/prelude_tour.rs`（examples 目录当前不存在，创建即可），写入以下**示例代码**（非项目原有代码）：

     ```rust
     // 示例代码：prelude 自助游
     use ui::prelude::*;

     fn prelude_tour() -> impl IntoElement {
         h_flex()
             .gap_2()
             .p_2()
             .child(div().child(Label::new("只靠 prelude 写 UI")))
             .child(
                 Button::new("tour-button", "点我")
                     .on_click(|_event, _window, _cx| {}),
             )
     }

     fn main() {
         // 仅构造元素树，不启动窗口；防止未使用告警。
         let _ = prelude_tour;
     }
     ```

   - 第三步：运行 `cargo check -p ui --example prelude_tour`。

   - 第四步（对照实验）：把第一行 `use ui::prelude::*;` 临时注释掉，再跑一次 `cargo check -p ui --example prelude_tour`，记录报错清单；然后恢复。

   - 第五步：删除 `examples/prelude_tour.rs`（或用 `git clean` 还原），保持工作区干净。

3. **需要观察的现象**：
   - 第三步编译通过，说明 `h_flex`（函数）、`gap_2`/`p_2`（`Styled` trait 方法）、`div`（工厂函数）、`Label`/`Button`（组件）、`on_click`（`Clickable` trait 方法）全部只靠一行 prelude 就位。
   - 第四步失败，报错集中在你注释后缺失的那些名字与 trait 方法上，与 4.1 的分析互相印证。

4. **预期结果**：`cargo check` 通过；反例实验报「cannot find function/method」类错误。若 `Button::new` 的参数或闭包签名与示例有出入，以本地编译器提示与 [button.rs:L26-L33](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L26-L33) 的官方 doc 示例为准。（命令输出待本地验证。）

## 6. 本讲小结

- prelude 存在的根因是 Rust 的 trait 作用域规则：`on_click`、`h_flex` 这类方法都由 trait 提供，trait 必须进作用域，于是 [src/prelude.rs](src/prelude.rs) 把高频名字打包，让全仓库统一以 `use ui::prelude::*;` 开头（当前至少 42 个下游文件如此）。
- prelude 的 35 行来自五层：gpui prelude 转发、gpui 显式类型/函数、能力 trait、设计令牌、组件与布局函数，外加跨 crate 转发的 `theme::ActiveTheme`——它就是 ui crate 公共 API 的地图。
- `component_prelude` 是组件预览体系的专用工具包（多出 `ComponentId`、`ComponentStatus`、`Documented`），不含任何渲染条目；实现预览的组件文件（如 `button.rs`、`facepile.rs`）总是与主 prelude 一起双导入。
- crate 根与 prelude 通过 glob 再导出形成双向引用，同一个条目拥有 `ui::Xxx` 与 `ui::prelude::Xxx` 等价路径；`components`/`styles`/`traits` 模块私有，保证内部重构不破坏下游。
- 「在 crate 根上」不等于「在 prelude 里」：`CommonAnimationExt`、`Transformable` 只在根（ui.rs L20），使用时需显式导入。

## 7. 下一步学习建议

下一讲（u1-l3「RenderOnce：无状态组件的渲染模型」）将使用本讲的导入约定，深入 `Icon` 与 `Popover` 的源码，讲清 `#[derive(IntoElement)]` + `impl RenderOnce` 这一 ui crate 组件的标准形态——你在本讲看到的 `IntoElement`、`ParentElement`、`div` 正是那里的主角。

在此之前，建议做两个热身阅读：

- [src/components/stack.rs](src/components/stack.rs)（15 行）与 [src/traits/styled_ext.rs](src/traits/styled_ext.rs) 的 `h_flex`/`v_flex` 定义，体会「函数入口」与「方法入口」的等价性（u1-l4 布局原语的前菜）。
- 任一下游文件的导入区（如 `crates/picker/src/head.rs` 开头），观察它除了 `use ui::prelude::*;` 之外还补导了哪些 ui 条目——那些「补导名单」正是 prelude 精选策略的活标本。
