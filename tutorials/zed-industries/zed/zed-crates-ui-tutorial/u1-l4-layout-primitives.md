# 布局原语：h_flex、v_flex 与 Group

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `h_flex()` / `v_flex()` 各自展开成哪几个 GPUI 样式调用，以及两者在 `items_center()` 上的不对称设计。
- 会用 `h_group_sm` / `h_group` / `h_group_lg` / `h_group_xl`（及纵向对应版本）组织「成组控件」，并记住四档间距与像素的换算。
- 理解 ui crate 在 GPUI flexbox 之上的三层封装习惯：gpui `Styled` 原子方法 → ui `StyledExt` 组合方法 → ui 快捷函数，以及「函数与方法并存」的原因。
- 能独立搭出一个工具栏骨架：左侧图标按钮组、中间分隔线、右侧弹性留白加主按钮。

## 2. 前置知识

本讲不需要你已经写过 GPUI 界面，但需要以下概念（前两讲已建立，这里快速回顾）：

- **flexbox 直觉**：一个容器把 `display` 设为 `flex` 后，它的直接子元素会沿「主轴」排列。`flex-direction: row` 表示主轴为水平（子元素从左到右），`column` 表示主轴为垂直（从上到下）。与主轴垂直的方向叫「交叉轴」，`align-items` 控制子元素在交叉轴上如何对齐（`center` 即居中）。GPUI 实现的就是这套 CSS flexbox 布局的等价物。
- **`div()` 与 `Div`**：`gpui::div()` 返回一个 `Div` 元素，它实现了 `Styled`（可以链式设置样式）和 `ParentElement`（可以用 `.child()` 添加子元素）。这是 u1-l3 讲过的「无状态元素」的最底层形态。
- **Tailwind 风格原子方法**：`Styled` trait 提供大量形如 `gap_1()`、`h_8()`、`w_full()` 的方法，命名模仿 Tailwind CSS。它们大多不是手写的，而是由 `gpui_macros` 里的宏批量生成（本讲 4.2 会看到生成表）。
- **rem**：GPUI 中所有尺寸的基准单位。默认 `1rem = 16px`，用户调整 `ui_scale` 时 rem 的像素值整体变化，组件等比缩放（u1-l3 提过「尺寸统一以 rem 表达」）。本讲的间距换算都以 16px/rem 为前提。
- **builder 链式调用**：样式方法按值消费 `self` 并返回 `Self`，因此可以无限 `.a().b().c()` 串下去；`when()` / `when_some()` 等条件组合来自 gpui 的 `FluentBuilder`。

## 3. 本讲源码地图

| 文件 | 规模与角色 | 本讲关注点 |
| --- | --- | --- |
| `src/components/stack.rs` | 15 行，两个快捷函数 | `h_flex()` / `v_flex()` 的定义与 `#[track_caller]` |
| `src/components/group.rs` | 57 行，八个分组函数 | `h_group_*` / `v_group_*` 四档间距 |
| `src/traits/styled_ext.rs` | trait `StyledExt` | `h_flex` / `v_flex` 方法本体、blanket impl、`debug_bg_*` |
| `crates/gpui/src/styled.rs`（辅助） | gpui 的 `Styled` trait | `flex()` / `flex_row()` / `flex_col()` / `items_center()` / `flex_1()` 各改了哪个字段 |
| `crates/gpui_macros/src/styles.rs`（辅助） | 样式方法生成器 | `gap_N` 数字后缀与 rem 的换算表 |
| `src/components/button/button.rs`（用例） | Button 组件 | `Button::render` 中 `h_flex` 的嵌套用法 |
| `crates/git_ui/src/unstaged_diff.rs`（用例） | 下游业务 crate | `h_group_sm()` 包 IconButton + Divider 的真实工具栏 |

导出链回顾（u1-l1/u1-l2 已讲）：`stack`、`group` 在 [src/components.rs:17](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components.rs#L17) 和 [src/components.rs:36](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components.rs#L36) 声明为私有模块，再经 [src/components.rs:60](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components.rs#L60) 和 [src/components.rs:79](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components.rs#L79) 倾倒到 crate 根；同时 `StyledExt` 经 [src/prelude.rs:23](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L23) 进 prelude，`h_flex`/`v_flex` 经 [src/prelude.rs:31](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L31)、八个 group 函数经 [src/prelude.rs:32-L34](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/prelude.rs#L32-L34) 进 prelude。所以一行 `use ui::prelude::*;` 之后，本讲所有布局原语都直接可用。

## 4. 核心概念与源码讲解

### 4.1 h_flex 与 v_flex：方向确定的堆叠容器

#### 4.1.1 概念说明

任何 flex 布局的第一步都是三件事：把容器变成 flex、指定方向、决定交叉轴对齐。如果每处都手写 `div().flex().flex_row().items_center()`，一则冗长，二则整个仓库会出现「有的横排居中、有的横排顶对齐」的视觉不一致。

ui crate 的解法是把 Zed 的布局惯例固化为两个函数：

- `h_flex()`（horizontal flex）：横向堆叠子元素，并且**默认让子元素在垂直方向居中**——因为横向条最常见的组合是「图标 + 文字」这类高度不一的内容。
- `v_flex()`（vertical flex）：纵向堆叠子元素，**不**额外设置交叉轴对齐——纵向布局常见于表单和列表，子元素保持默认的拉伸（stretch）行为通常更合适。

这不是一个理论设计：在本仓库 `crates/` 目录下，`h_flex()` 的调用超过 1100 处、分布在 260 多个文件，是整个 Zed 代码库使用频率最高的布局入口。

#### 4.1.2 核心流程

调用链非常短，一层委托：

```text
h_flex()                          ← ui::components::stack 里的自由函数
  └─ div().h_flex()               ← div() 来自 gpui，.h_flex() 是 StyledExt 方法
       └─ StyledExt::h_flex(self)
            └─ self.flex()        → style().display = Display::Flex
            └─ .flex_row()        → style().flex_direction = FlexDirection::Row
            └─ .items_center()    → style().align_items = AlignItems::Center
```

每次链式调用都是在修改 `Div` 内部的 `StyleRefinement`（样式 refinement，即「与默认值的差量」），渲染时由 GPUI 布局器读取这些字段做 flexbox 排版。`v_flex()` 同理，但只有两步：`flex()` + `flex_col()`。

#### 4.1.3 源码精读

先看函数本体——整个文件只有 15 行：

- [src/components/stack.rs:1-L15](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/stack.rs#L1-L15)：定义 `h_flex()` 与 `v_flex()` 两个快捷函数，它们只是把 `div()` 和 `StyledExt` 上的同名方法拼起来，返回 `Div`。

```rust
#[track_caller]
pub fn h_flex() -> Div {
    div().h_flex()
}

#[track_caller]
pub fn v_flex() -> Div {
    div().v_flex()
}
```

两个细节值得注意：

1. `#[track_caller]`：这两个函数是薄封装，如果不用该属性，一旦链上后续代码 panic，栈回溯会指向 `stack.rs` 而不是真正的调用处；加上后 panic 位置指向写业务代码的那一行，便于排查。
2. 函数**不带任何参数**，也不需要 `cx`——设置布局样式不依赖应用状态，这与 `StyledExt::elevation_1(cx)` 这类需要查主题的方法形成对比。

真正的方法本体在 `StyledExt` trait 里：

- [src/traits/styled_ext.rs:26-L39](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits/styled_ext.rs#L26-L39)：`StyledExt` trait 的定义，以及 `h_flex` / `v_flex` 两个带默认实现的方法。这段代码是本讲的核心——`h_flex` 展开为 `flex().flex_row().items_center()` 三连，`v_flex` 展开为 `flex().flex_col()` 两连，文档注释明确写出了各自设置了什么。

```rust
pub trait StyledExt: Styled + Sized {
    /// Horizontally stacks elements.
    ///
    /// Sets `flex()`, `flex_row()`, `items_center()`
    fn h_flex(self) -> Self {
        self.flex().flex_row().items_center()
    }

    /// Vertically stacks elements.
    ///
    /// Sets `flex()`, `flex_col()`
    fn v_flex(self) -> Self {
        self.flex().flex_col()
    }
    // ……elevation、border、debug_bg 等方法见 4.3
}
```

这三连里的每一步都是 gpui `Styled` trait 的原子方法，各自只改一个样式字段：

- [crates/gpui/src/styled.rs:45-L48](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/styled.rs#L45-L48)：`flex()` 把 `display` 设为 `Display::Flex`，容器从此按 flexbox 排版。
- [crates/gpui/src/styled.rs:153-L170](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/styled.rs#L153-L170)：`flex_col()` / `flex_row()` 设置 `flex_direction` 为 `Column` / `Row`，决定主轴方向。
- [crates/gpui/src/styled.rs:301-L304](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/styled.rs#L301-L304)：`items_center()` 设置 `align_items = AlignItems::Center`，让子元素在交叉轴（横向容器里即垂直方向）居中。

注意这个**不对称**：`h_flex` 带 `items_center()`，`v_flex` 不带。这意味着 `v_flex()` 的子元素默认在水平方向拉伸填满容器宽度（flex 默认 `align-items: stretch`）；如果你想要纵向堆叠且水平居中，要自己补 `.items_center()`。

最后看一个真实组件里的用法——Button 内部就是用 `h_flex` 组织「图标 + 文字 + 快捷键」的：

- [src/components/button/button.rs:462-L494](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L462-L494)：`Button::render` 用外层 `h_flex()` 放图标和内容，内层再嵌一个 `h_flex()` 放文字与快捷键；并且当快捷键位置为 `Start` 时用 `.flex_row_reverse()` 在链中段把方向反转。这说明两件事：`h_flex` 可以嵌套；已有容器可以在任意位置用 `Styled` 原子方法覆盖此前的设置。

```rust
self.base.child(
    h_flex()
        .when(self.truncate, |this| this.min_w_0().overflow_hidden())
        .gap(DynamicSpacing::Base04.rems(cx))
        // …loading 图标或 start_icon…
        .child(
            h_flex()                       // ← 第二层嵌套
                .when(self.key_binding_position == KeybindingPosition::Start,
                    |this| this.flex_row_reverse())  // ← 中段反转方向
                .gap(DynamicSpacing::Base06.rems(cx))
                .justify_between()
                // …Label 与 KeyBinding…
```

#### 4.1.4 代码实践

**实践目标**：验证「快捷函数 = 完整原子链」这一等价关系，并亲手跑通第一个 ui 示例文件。

**操作步骤**：

1. 在本地克隆的 Zed 仓库中新建目录 `crates/ui/examples/`（ui crate 目前没有示例目录，cargo 会自动发现该目录下的 `*.rs` 作为 example 目标）。
2. 创建文件 `crates/ui/examples/layout_equivalence.rs`，写入以下内容（**示例代码**，非项目原有代码）：

   ```rust
   use ui::prelude::*;

   fn expanded_chain() -> Div {
       div().flex().flex_row().items_center()
   }

   fn shortcut() -> Div {
       h_flex()
   }

   fn vertical() -> Div {
       v_flex()
   }

   fn main() {
       let _ = (expanded_chain(), shortcut(), vertical());
   }
   ```

3. 在仓库根目录运行 `cargo check -p ui --example layout_equivalence`。

**需要观察的现象**：编译通过，没有任何类型错误——说明 `h_flex()` 的返回类型与手写三连链完全一致（都是 `Div`）。

**预期结果**：三条链都编译通过。如果你想进一步对比 `v_flex` 与 `h_flex` 的差异，可以在 `vertical()` 里补一行 `.items_center()`，同样应当编译通过。视觉效果（子元素对齐方式）需要真实窗口才能看到，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：不看源码，写出 `h_flex()` 展开后的完整链式调用。

答案：`div().flex().flex_row().items_center()`。其中 `flex()` 设置 `display`，`flex_row()` 设置主轴为水平，`items_center()` 设置交叉轴（垂直）居中。

**练习 2**：为什么 `v_flex()` 不像 `h_flex()` 那样自带 `items_center()`？如果确实需要「纵向堆叠且水平居中」怎么写？

答案：两者服务的典型场景不同。横向条里常见「图标 + 高低不平的文字」，垂直居中几乎总是想要的；纵向布局（表单、列表）里子元素通常希望拉伸填满宽度（flex 默认 stretch）。需要居中时显式写 `v_flex().items_center()` 即可——ui crate 的原则是「高频惯例进快捷方式，其余留给原子方法」。

**练习 3**：`stack.rs` 里两个函数上的 `#[track_caller]` 起什么作用？

答案：让 panic 报告的调用位置指向业务代码中调用 `h_flex()` / `v_flex()` 的那一行，而不是薄封装所在的 `stack.rs`，方便定位布局代码中的问题。

### 4.2 Group 家族：带语义间距的成组容器

#### 4.2.1 概念说明

界面上经常出现「语义上属于一组的几个小控件」：一对上/下一步箭头、一组字号切换按钮、撤销与重做。它们之间的间距应当**小而固定**，明显小于组与组之间的间距，用户才能一眼看出「这三个是一伙的」。

`group.rs` 提供的就是这种「成组容器」：一个 flex 容器加上一档预设的 `gap`（子元素间距）。它有四个尺寸档，命名对应语义尺寸 xs / s / m / l：

| 函数 | 展开的链 | gap | 16px/rem 下 |
| --- | --- | --- | --- |
| `h_group_sm()` | `div().flex().gap_0p5()` | 0.125rem | ~2px（xs） |
| `h_group()` | `div().flex().gap_1()` | 0.25rem | ~4px（s） |
| `h_group_lg()` | `div().flex().gap_1p5()` | 0.375rem | ~6px（m） |
| `h_group_xl()` | `div().flex().gap_2()` | 0.5rem | ~8px（l） |

纵向版本 `v_group_sm/lg/xl` 完全对称，只是多了 `.flex_col()`。

与 `h_flex` 的关键差异：group **不设置方向**（依赖 flex 默认的 `row`，纵向版本显式设 `col`），也**不设置 `items_center()`**——成组的多半是同尺寸按钮，无需居中；它比 `h_flex` 多出的恰恰是「语义化的固定间距」。

#### 4.2.2 核心流程

间距数字后缀与 rem 的换算关系（由 gpui 宏的生成表决定，见下节）：

\[ \text{长度}(N) = N \times 0.25\ \text{rem} = N \times 4\ \text{px} \quad (\text{当 } 1\,\text{rem} = 16\,\text{px}) \]

其中 \( N \) 是方法名里的数字后缀（`0p5` 表示 0.5，`1p5` 表示 1.5）。例如 `gap_2()` 即 \( 2 \times 4 = 8 \) px。使用成组容器的决策流程：

```text
要放一组语义相关的小控件？
  ├─ 是 → 选档位：极紧凑(2px)=h_group_sm / 紧凑(4px)=h_group / 常规(6px)=h_group_lg / 宽松(8px)=h_group_xl
  │        纵向排列则换成 v_group_*
  └─ 只是要普通堆叠 → h_flex() / v_flex()，间距自己用 .gap_N() 或 DynamicSpacing 指定
```

#### 4.2.3 源码精读

- [src/components/group.rs:6-L29](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/group.rs#L6-L29)：四个横向分组函数。每个都是一行 `div().flex().gap_N()`，文档注释标注了语义档位和像素参考值。

```rust
/// xs: ~2px @16px/rem
pub fn h_group_sm() -> Div {
    div().flex().gap_0p5()
}

/// s: ~4px @16px/rem
pub fn h_group() -> Div {
    div().flex().gap_1()
}

/// m: ~6px @16px/rem
pub fn h_group_lg() -> Div {
    div().flex().gap_1p5()
}

/// l: ~8px @16px/rem
pub fn h_group_xl() -> Div {
    div().flex().gap_2()
}
```

- [src/components/group.rs:34-L57](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/group.rs#L34-L57)：四个纵向分组函数，与横向版本唯一区别是多了 `.flex_col()`，例如 `v_group_sm()` 是 `div().flex().flex_col().gap_0p5()`。

`gap_0p5()` 这类方法从哪来？它们不是手写的，而是 `Styled` trait 内部调用宏批量生成：

- [crates/gpui/src/styled.rs:26](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/styled.rs#L26)：`gpui_macros::style_helpers!()` 在 `Styled` trait 体内展开出 `gap_*`、`p_*`、`m_*`、`w_*`、`h_*` 等成百上千个原子方法。
- [crates/gpui_macros/src/styles.rs:926-L952](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_macros/src/styles.rs#L926-L952)：数字后缀到长度值的对照表（`box_style_suffixes`）。本讲用到的四个档位都在这里：`"0p5" → rems(0.125)`（2px）、`"1" → rems(0.25)`（4px）、`"1p5" → rems(0.375)`（6px）、`"2" → rems(0.5)`（8px）。这就是 4.2.2 换算公式的出处。

最后看下游真实用法——git 面板工具栏里，`h_group_sm()` 把「上一个/下一个改动」两个箭头按钮拢成一组，组外用 `Divider::vertical()` 分隔：

- [crates/git_ui/src/unstaged_diff.rs:761-L790](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/git_ui/src/unstaged_diff.rs#L761-L790)：用 `h_group_sm()` 包住两个 `IconButton`（上/下一个 hunk），随后 `.child(Divider::vertical())` 再接第二个 `h_group_sm()`（Stage 相关按钮）。这正是本讲综合实践要复刻的结构。

```rust
h_group_sm()
    .child(IconButton::new("up", IconName::ArrowUp).icon_size(IconSize::Small) /* … */)
    .child(IconButton::new("down", IconName::ArrowDown).icon_size(IconSize::Small) /* … */)
// 外层继续：
.child(Divider::vertical())
.child(h_group_sm().child(Button::new("stage", "Stage") /* … */))
```

顺带一提，分隔线组件 `Divider` 也来自 ui crate（[src/components/divider.rs:46-L76](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/divider.rs#L46-L76) 提供 `horizontal` / `vertical` / 虚线四个方向构造器），它不在 prelude 里，需要 `use ui::Divider;`。

#### 4.2.4 代码实践

**实践目标**：直观感受四档 group 间距的差异，并验证档位换算。

**操作步骤**：

1. 继续使用 4.1.4 创建的 `crates/ui/examples/`，新建 `crates/ui/examples/group_spacing.rs`（**示例代码**）：

   ```rust
   use ui::prelude::*;

   fn main() {
       let tight = h_group_sm()
           .child(div().w_4().h_4().debug_bg_red())
           .child(div().w_4().h_4().debug_bg_red())
           .child(div().w_4().h_4().debug_bg_red());

       let loose = h_group_xl()
           .child(div().w_4().h_4().debug_bg_blue())
           .child(div().w_4().h_4().debug_bg_blue());

       let stack = v_flex().gap_2().child(tight).child(loose);
       let _ = stack;
   }
   ```

2. 运行 `cargo check -p ui --example group_spacing`。
3. 打开 [src/components/group.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/group.rs#L1-L57)，把四个函数的 `gap_N` 与 4.2.2 的换算表逐一对账。

**需要观察的现象**：编译通过；`debug_bg_red()` / `debug_bg_blue()`（来自 `StyledExt`，见 4.3）给占位块上色，为将来在真实窗口里目视对比做准备。

**预期结果**：按换算公式，`tight` 组内三个色块间距应为 2px，`loose` 组内为 8px，两组之间（`v_flex().gap_2()`）也是 8px。目视效果需真实窗口渲染，**待本地验证**；但换算关系可以直接从源码注释与宏对照表确认，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`h_group_sm()` 与 `h_flex().gap_2()` 在最终样式上有哪几处不同？

答案：三处——间距（2px vs 8px）、交叉轴对齐（`h_group_sm` 不设 `align-items`，子元素默认 stretch；`h_flex` 设了 `items_center()`）、语义（前者表达「这是一组控件」，后者只是普通横向堆叠）。

**练习 2**：`gap_3p5()` 对应多少 rem、多少像素（16px/rem）？

答案：\( 3.5 \times 0.25 = 0.875 \) rem，即 \( 3.5 \times 4 = 14 \) px。依据是宏对照表里 `"3p5" → rems(0.875)`。

**练习 3**：为什么这一族函数叫 group 而不是 `h_flex_sm`（「小间距的 h_flex」）？

答案：命名表达的是**用途**而非参数：它们存在的意义是「把语义相关的控件成组」，档位 xs/s/m/l 描述的是组的紧凑程度；普通堆叠容器 `h_flex` 与成组容器 `h_group_*` 是两个概念，间距只是 group 携带的默认值之一。这也是 ui crate 命名的一贯风格——名字回答「这是什么」。

### 4.3 封装习惯：ui 在 flexbox 之上的三层约定

#### 4.3.1 概念说明

把本讲三个文件放在一起，能看到 ui crate 对 GPUI 布局能力的分层封装：

| 层 | 位置 | 形态 | 例子 |
| --- | --- | --- | --- |
| 1. 原子方法 | gpui `Styled` trait（部分由宏生成） | 单一字段设置 | `flex()`、`flex_row()`、`items_center()`、`gap_1()` |
| 2. 组合方法 | ui `StyledExt` trait | 固化 Zed 惯例的方法 | `h_flex()`、`v_flex()`（以及 `elevation_*`、`debug_bg_*` 等） |
| 3. 快捷函数 | ui `components::stack` / `group` | 链起点工厂，返回 `Div` | `h_flex()`、`h_group_sm()` |

为什么第 2、3 层要**函数与方法并存**？因为两者的使用时机不同：

- **函数**是最常见的链起点：`h_flex().gap_2().child(…)` 直接得到一个 `Div`。
- **方法**用于链的中段：任何已经是 `Styled` 的元素（包括 gpui 的 `div()`、其他容器、甚至组件内部的元素）都能随时 `.h_flex()` 一下换上这套样式，比如 Button 在快捷键靠前时对已有内层容器调 `.flex_row_reverse()`（见 4.1.3 引用的 [src/components/button/button.rs:486-L494](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L486-L494)）。

#### 4.3.2 核心流程

写布局时的选型决策树：

```text
需要容器？
  ├─ 普通堆叠 → h_flex()/v_flex()（函数起步）
  │     └─ 中途要改方向/对齐 → 链上补 Styled 原子方法（.flex_row_reverse()、.items_start()）
  ├─ 一组相关小控件 → h_group_* / v_group_*（选 xs/s/m/l 档）
  ├─ 任何已有元素想换装 → .h_flex() / .v_flex()（StyledExt 方法）
  └─ 需要弹性留白/占位 → .flex_1()；固定不缩 → .flex_none()
```

其中 `flex_1()` 是「弹性留白」的关键：[crates/gpui/src/styled.rs:181-L186](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui/src/styled.rs#L181-L186) 显示它同时设置 `flex_grow = 1`、`flex_shrink = 1`、`flex_basis = 0`——即「忽略自身内容尺寸、吞掉所有剩余空间」，工具栏左右分区靠的就是它。

#### 4.3.3 源码精读

- [src/traits/styled_ext.rs:20-L26](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits/styled_ext.rs#L20-L26)：`StyledExt` 的 trait 声明，约束为 `Styled + Sized`，文档写明它是「为 gpui::Styled 扩展 Zed 特有样式方法」。其上的 `#[cfg_attr(… derive_inspector_reflection)]` 是为 UI 检查器生成反射信息，仅调试构建生效，初学可忽略。
- [src/traits/styled_ext.rs:94-L131](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits/styled_ext.rs#L94-L131)：同一 trait 还承载 `border_primary(cx)` / `border_muted(cx)` 与六个 `debug_bg_*` 调试方法。`debug_bg_red()` 等直接写死一个高饱和 `hsla` 颜色，专为排版时给块上色定位区域，是本讲实践里用它们做可视化的依据。
- [src/traits/styled_ext.rs:134](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/traits/styled_ext.rs#L134)：**blanket 实现** `impl<E: Styled> StyledExt for E {}`。这一行是整个机制的关键：任何实现了 gpui `Styled` 的类型（`Div`、`Stateful<Div>`、乃至你自定义的元素）自动获得全部 `StyledExt` 方法，无需逐类型实现。这也是 u1-l2 讲过的「能力 trait」模式在样式侧的重演。

```rust
impl<E: Styled> StyledExt for E {}
```

- [src/components/stack.rs:1-L3](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/stack.rs#L1-L3)：stack.rs 顶部只导入 `gpui::{Div, div}` 和 `crate::StyledExt`——快捷函数对 gpui 的依赖仅此而已，充分说明第 3 层是多么薄。

#### 4.3.4 代码实践

**实践目标**：体会 `h_flex()` 在真实代码库中的统治地位，找到「链中段使用方法」的实例。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   rg -c '\bh_flex\(\)' crates --glob '*.rs' | awk -F: '{s+=$2; n++} END {print n" files, "s" calls"}'
   ```

2. 再执行 `rg -n 'flex_row_reverse\(\)' crates/ui/src` ，找出在已有 `h_flex` 容器上中段反转方向的调用点。
3. 阅读其中一处（例如 [src/components/button/button.rs:486-L494](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/ui/src/components/button/button.rs#L486-L494) 的 `KeybindingPosition::Start` 分支），画出一层 Button 内部的两层 `h_flex` 嵌套草图。

**需要观察的现象**：第 1 步应输出约 260 个文件、1100 余次调用；第 2 步能看到 `.flex_row_reverse()` 这类「后设置的原子方法覆盖先设置的惯例」的写法。

**预期结果**：你会得出结论——`h_flex` 是 Zed 布局代码的事实标准入口，而 `StyledExt` 方法与 `Styled` 原子方法在链上的组合是微调手段。统计数字可能随版本浮动，量级不变。

#### 4.3.5 小练习与答案

**练习 1**：`StyledExt` 为什么用 blanket impl（`impl<E: Styled> StyledExt for E {}`），而不是只为 `Div` 实现？

答案：GPUI 里可样式化的元素类型很多（`Div`、`Stateful<Div>`、各种实现 `Styled` 的自定义元素）。blanket impl 让所有这些类型一次性获得 `h_flex()` 等方法，调用方无需关心具体元素类型，也让未来新增的元素类型自动受益——这是 Rust 里扩展第三方 trait 的标准手法。

**练习 2**：`debug_bg_red()` 这类方法为什么有资格放进 `StyledExt`？

答案：它是布局调试的工作流方法：排版阶段给不确定边界的块临时上色，完成后删掉。与 `h_flex` 一样属于「Zed 团队希望在所有元素上随手可用」的横切能力，放进同一个能力 trait 最顺手。

**练习 3**：已有 `h_flex().child(x)`，现在想改成纵向排列，有哪两种写法？

答案：改成 `v_flex().child(x)`（换链起点函数）；或保留原容器、在链上加 `.flex_col()` 覆盖 `flex_row()`（链中段用原子方法，后设覆盖先设）。两种等价，前者语义更清晰，后者适合条件分支（`.when(cond, |el| el.flex_col())`）。

## 5. 综合实践

**任务**：搭一个工具栏骨架——左侧一组两个图标占位块（成组，紧凑间距）、中间一条垂直分隔线、右侧弹性留白后接一个主按钮占位块。只用本讲原语加 `div()`，不借助任何业务组件。

创建 `crates/ui/examples/toolbar_skeleton.rs`（**示例代码**）：

```rust
use ui::prelude::*;
use ui::Divider;

/// 工具栏骨架：左（成组图标占位）| 分隔线 | 弹性留白 | 主按钮占位
fn toolbar_skeleton() -> Div {
    h_flex()
        .h_8()               // 工具栏高度：2rem = 32px
        .w_full()            // 占满父容器宽度
        .gap_2()             // 大区块之间 8px
        // 左侧：两个图标占位块，语义上是一组 → h_group_sm（2px 紧凑间距）
        .child(
            h_group_sm()
                .child(div().w_4().h_4().debug_bg_red())
                .child(div().w_4().h_4().debug_bg_red()),
        )
        // 中间：垂直分隔线
        .child(Divider::vertical())
        // 右侧：弹性留白（吃掉全部剩余宽度），把主按钮推到最右
        .child(div().flex_1())
        .child(div().h_7().w_10().rounded_sm().debug_bg_blue())
}

fn main() {
    let _ = toolbar_skeleton();
}
```

验证与观察：

1. 运行 `cargo check -p ui --example toolbar_skeleton`，应当无错误通过（类型层面即验证了所有方法存在、链式组合合法）。
2. 对照检查结构：`h_flex`（外层横排 + 垂直居中）→ `h_group_sm`（成组紧凑）→ `Divider::vertical()`（分隔）→ `flex_1()`（弹性留白）→ 主按钮占位。这五个角色与 git 面板真实工具栏（4.2.3 引用的 unstaged_diff.rs）的结构一一对应。
3. 想目视效果的话，把该函数挂进任何能打开 GPUI 窗口的示例或组件预览中渲染；色块布局效果**待本地验证**。观察点：左侧两个红块挨得近（2px），蓝块贴右边缘，中间剩余空间全被 `flex_1()` 占位块吞掉。
4. 迭代练习：把 `h_group_sm()` 换成 `h_group_xl()`，左侧间距应从 2px 变为 8px；把 `.gap_2()` 移到 `h_group_sm` 内部，观察语义变化（区块间距 vs 组内间距）。

## 6. 本讲小结

- `h_flex()` = `div().flex().flex_row().items_center()`，`v_flex()` = `div().flex().flex_col()`；两者是全仓库使用最广的布局入口（crates/ 下 1100+ 处调用）。
- 二者的不对称是刻意的：横向堆叠默认垂直居中，纵向堆叠保持默认拉伸，需要时用原子方法补 `.items_center()`。
- `h_group_sm/lg/xl`（及 `v_group_*`）是「成组控件」容器，只设 flex + 固定 gap（2/4/6/8px），不带 `items_center`；数字后缀换算满足 \( N \times 4 \) px。
- `gap_N` 等原子方法由 `gpui_macros::style_helpers!()` 按 [crates/gpui_macros/src/styles.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_macros/src/styles.rs#L926-L952) 的对照表生成，一切尺寸以 rem 表达、随 `ui_scale` 缩放。
- ui 的封装分三层：gpui `Styled` 原子方法 → `StyledExt` 组合方法（blanket impl 覆盖一切 `Styled` 元素）→ `stack.rs`/`group.rs` 快捷函数；函数做链起点、方法做链中段，`#[track_caller]` 保证薄封装不污染 panic 定位。
- 弹性留白用 `.flex_1()`（grow=shrink=1、basis=0），它是工具栏「左右分区」结构的支点。

## 7. 下一步学习建议

- 下一讲 **u2-l1（语义颜色系统）**：本讲的占位块用了 `debug_bg_*` 这种写死颜色，真实组件应改用 `Color::Muted` / `Color::Error` 等语义色查主题取值，学完你就能把骨架里的占位块换成主题化样式。
- 之后学 **u2-l3（DynamicSpacing 与 UI 密度）**：`gap_N` 是固定 rem，而 Button 源码里出现的 `DynamicSpacing::Base04.rems(cx)` 能随 Compact/Default/Comfortable 三档密度变化，是间距系统的进阶形态。
- 想看 `h_flex` 的最大用户，可直接跳读 **u3-l1（Button 全解）**，对照本讲 4.1.3 引用的 `Button::render` 理解两层嵌套如何组织图标、文字与快捷键。
