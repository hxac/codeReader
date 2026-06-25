# 表单：Form 与 Field 校验

> 所属单元：u4 表单与基础输入 · 进阶层
> 依赖讲义：[u3-l1 按钮家族](u3-l1-button-family.md)、[u4-l2 开关选择](u4-l2-switch-checkbox-radio.md)

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Form` 和 `Field` 各自的职责：`Form` 负责**整体布局**，`Field` 负责**单行字段的标签 / 输入 / 说明**排版。
- 理解 `Form` 如何用一个 `FieldProps` 把「布局方向、列数、标签宽度、尺寸」统一下发给每一个 `Field`。
- 掌握 `Field` 的「标签、必填星号、描述、列跨度」等配置项的用法。
- 明白 gpui-component 的表单**没有内置校验状态字段**，校验是「状态外置 + 描述反馈」的约定，能据此把红色错误提示画到字段下方。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前序讲义）：

- **无状态 `RenderOnce` 组件范式**（[u2-l2](u2-l2-styled-and-sizable.md)）：`Form`/`Field` 都是 `#[derive(IntoElement)]` + `RenderOnce` 的无状态组件，跨帧状态由外层 View 持有。
- **`Styled` / `Sizable`**（[u2-l2](u2-l2-styled-and-sizable.md)）：`Form` 实现了这两个 trait，可用链式样式与 `xs/sm/md/lg` 尺寸。
- **`Input` 与 `InputState`**（[u4-l4](u4-l4-input-and-inputstate.md)）：表单里最常见的子控件就是 `Input`，它的值通过 `InputState` 持有。
- **状态外置 + 双向同步**（[u3-l4](u3-l4-collapsible-accordion.md)）：折叠/分组组件已经演示过「状态存在 View、组件只负责展示」的库约定，本讲的校验反馈遵循同一约定。

几个术语先对齐：

- **Flex / Grid**：GPUI 的 `Styled` 支持 flexbox 与 CSS grid 两种布局。`Form` 用 **CSS grid** 来实现多列排版，每个 `Field` 通过 `col_span` 决定占几列。
- **`FieldProps`**：表单级的「共享参数包」，`Form` 持有它的一份，渲染时分发给每个 `Field`，保证整张表的风格一致。
- **`danger` 色**：主题里的危险/错误色（红色系），`cx.theme().danger`，用于必填星号和错误提示。

## 3. 本讲源码地图

本讲只涉及 `crates/ui/src/form/` 这一个目录，共三个文件：

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/form/mod.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/mod.rs) | 模块入口，再导出 `Form`/`Field`，并提供 `v_form()`、`h_form()`、`field()` 三个构造快捷函数。 |
| [crates/ui/src/form/form.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs) | `Form` 容器：持有字段列表与 `FieldProps`，渲染成一个 CSS grid 容器。 |
| [crates/ui/src/form/field.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs) | `Field` 字段：负责「标签 + 输入 + 描述」的排版；内含 `FieldProps` 与 `FieldBuilder`。 |

此外，最接近真实用法的范例是 Story Gallery 里的：

- [crates/story/src/stories/form_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/form_story.rs) —— 一个完整可跑的表单示例，串联 `Input`、`Select`、`Switch`、`Checkbox`、`DatePicker`、`ColorPicker` 等控件。

---

## 4. 核心概念与源码讲解

### 4.1 Form：表单容器与栅格布局

#### 4.1.1 概念说明

`Form` 解决的问题是：**「一行字段」之间如何对齐、多列如何排布、整张表如何统一风格**。

如果你直接用一堆 `div().flex()` 手摆表单，会遇到这些重复劳动：

- 每个标签的宽度要手动设；
- 水平布局下「标签 + 输入」要一行行对齐；
- 想从一列变两列，每个字段都要改；
- 字段之间的间距、字号要一处一处调。

`Form` 把这些「表单级」的配置集中成一个 `FieldProps`，自己渲染成一个 CSS grid 容器，再把每个 `Field` 当作一个 grid 单元摆进去。你只需要告诉 `Form`：方向（水平/垂直）、列数、标签宽度、尺寸，剩下交给它。

#### 4.1.2 核心流程

`Form` 的渲染可以归纳为下面这个流程：

```text
Form {
    fields: Vec<Field>,      // 你塞进来的字段
    props:  FieldProps,      // 布局方向 / 列数 / 标签宽度 / 尺寸
}
        │ render
        ▼
div()                              // 外层包装，解决偶尔出现的宽度不满问题
  └─ v_flex().grid()               // 一个 CSS grid 容器
       .grid_cols(columns)         // 列数
       .gap_x(gap*3).gap_y(gap)
       .children(                  // 逐个字段
           fields.enumerate().map(|(ix, f)|
               f.props(ix, props)  // ★ 把表单级 props 下发给字段
           )
       )
```

关键点：

1. **`Form` 是 CSS grid 容器**，列数由 `columns` 决定。
2. **props 下发**：`Form` 不是直接渲染字段，而是先调用 `field.props(ix, props)` 把「我这份表单级配置」注入到每个 `Field`，再渲染。这是 `Form` 与 `Field` 的耦合点。
3. **间距由尺寸推导**：`gap` 根据 `size`（小=6px、默认=8px、大=12px）计算，横向间距是纵向的 3 倍。

#### 4.1.3 源码精读

**`Form` 的结构**：它持有字段列表与一份 `FieldProps`，本身是一个无状态组件。

[crates/ui/src/form/form.rs:13-18](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L13-L18) —— `Form` 的三个字段：`style`（自定义样式）、`fields`（字段列表）、`props`（表单级共享参数）。

```rust
#[derive(IntoElement)]
pub struct Form {
    style: StyleRefinement,
    fields: Vec<Field>,
    props: FieldProps,
}
```

**构造与配置 API**：`Form` 用两个静态方法区分方向，配置方法都是 builder 风格（消费 `self` 返回 `self`）。

[crates/ui/src/form/form.rs:29-43](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L29-L43) —— `horizontal()` / `vertical()` 只是 `new().layout(...)` 的快捷方式，默认是 `Axis::Vertical`。

```rust
pub fn horizontal() -> Self { Self::new().layout(Axis::Horizontal) }
pub fn vertical() -> Self { Self::new().layout(Axis::Vertical) }
pub fn layout(mut self, layout: Axis) -> Self { self.props.layout = layout; self }
```

[crates/ui/src/form/form.rs:45-55](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L45-L55) —— 标签宽度（水平布局下生效，默认 100px）与标签字号。注意它们改的是 `self.props`，不是某个具体字段。

[crates/ui/src/form/form.rs:69-75](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L69-L75) —— 列数，默认 1。

**`child` 接收的是 `Field`**：

[crates/ui/src/form/form.rs:57-67](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L57-L67) —— `child` 的签名是 `impl Into<Field>`。源码里没有任何「把别的东西转成 `Field`」的 `From` 实现，所以实际能传进来的只有 `Field` 本身（恒等转换 `From<T> for T`）。这保证了 `Form` 的直接子节点一定是 `Field`。

```rust
pub fn child(mut self, field: impl Into<Field>) -> Self {
    self.fields.push(field.into());
    self
}
```

**`Sizable` 决定整体间距**：

[crates/ui/src/form/form.rs:84-89](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L84-L89) —— `with_size` 把尺寸写进 `props.size`，`Field` 也会继承它。注意它**只存档位**，真正的「档位→像素」翻译在 `render` 里。

**`render`：CSS grid + props 下发**（这是 `Form` 的核心）：

[crates/ui/src/form/form.rs:91-117](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L91-L117) —— 重点看 `gap` 的尺寸推导、`.grid().grid_cols(...)`，以及 `.map(|(ix, field)| field.props(ix, props))` 这一行 props 下发。

```rust
let gap = match props.size {
    Size::XSmall | Size::Small => px(6.),
    Size::Large => px(12.),
    _ => px(8.),
};
div().child(
    v_flex()
        .w_full()
        .gap_x(gap * 3.)
        .gap_y(gap)
        .grid()
        .grid_cols(props.columns as u16)
        .children(
            self.fields
                .into_iter()
                .enumerate()
                .map(|(ix, field)| field.props(ix, props)),  // ★ 下发
        ),
)
```

> 小提示：最外层套了一个 `div()`，注释说是「避免偶尔出现的宽度不满问题」——这是 GPUI flex 子元素宽度计算的一个经验性兜底。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `Form` 的「列数」与「props 下发」两个机制。

**操作步骤**（源码阅读型实践，无需改源码）：

1. 打开 [crates/story/src/stories/form_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/form_story.rs)。
2. 找到 `v_form()` 的调用（约第 186 行），观察它链式调用了 `.layout(self.layout)`、`.with_size(self.size)`、`.columns(self.columns)`、`.label_width(...)` 四个配置。
3. 顺着 `field.props(ix, props)` 这一行（[form.rs:113](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/form.rs#L113)）跳到 [field.rs:176-183](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L176-L183)，确认 `Field` 把 `Form` 传来的 `props` 直接覆盖了自己的 `self.props`，并顺手把 `id` 设成下标 `ix`。

**需要观察的现象 / 预期结果**：

- 在 Gallery 里打开 **Form** 这个 story，把右上角 **Multi Columns** 开关切到「多列」，你会看到字段从单列重排成两列——这正是 `.columns(self.columns)` 改变了 `grid_cols`。
- 这两个配置你**只在 `Form` 上设了一次**，但所有字段都跟着变了——这就是 props 下发的效果。

> 行为为待本地验证（需要能运行 Gallery：`cargo run`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Form` 的 `child` 签名是 `impl Into<Field>`，而不是直接 `Field`？

**参考答案**：用 `impl Into<Field>` 是 Rust builder API 的常见写法，保留了「将来允许别的类型转换成 `Field`」的扩展空间，同时也不会拒绝直接传 `Field`（恒等转换）。当前源码里并没有别的 `From<T> for Field`，所以实际只能传 `Field`，签名只是预留了灵活性。

**练习 2**：把表单设成 `.columns(3)` 后，某个字段想横跨整行，该调用哪个方法？在哪个组件上调用？

**参考答案**：在 **`Field`** 上调用 `.col_span(3)`（[field.rs:203-209](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L203-L209)）。`Form` 只决定 grid 有几列，每个字段占几列由 `Field` 自己声明。

---

### 4.2 Field：表单字段与校验反馈

#### 4.2.1 概念说明

如果说 `Form` 是「表格」，`Field` 就是「一行」——它负责把**标签（Label）、输入控件（Input/Switch…）、描述（Description）**这三部分按方向对齐排版，并提供「必填」「隐藏」「列跨度」等单字段配置。

关于**校验**，有一个非常重要、容易踩坑的事实：

> gpui-component 的 `Field` **没有内置的 `error` / `invalid` 字段或方法**。源码里翻遍 `field.rs` 也找不到 `.error(...)`。

那么「校验失败显示红色错误」是怎么做到的？靠的是库统一的约定：

1. **必填标记**：`Field::required(true)` 在标签后画一个红色的 `*`。
2. **错误文案**：把错误信息当成「描述」塞进去——用 `description_fn` 渲染一段红色文字（颜色取 `cx.theme().danger`）。
3. **校验状态外置**：是否报错、报什么错，由外层 View 持有（`Option<String>`），每帧根据状态决定要不要画这段描述。

这和 [u3-l4](u3-l4-collapsible-accordion.md) 讲过的「状态外置 + 双向同步」完全一致：组件只负责展示，状态归 View。

#### 4.2.2 核心流程

`Field` 的 `render` 把内容拆成上下两块（都用方向感知的 `wrap_div`）：

```text
Field
  │ render（layout 来自 Form 下发的 props）
  ▼
v_flex()                            // 整列容器（参与 grid，col_span 在这里设）
  ├─ wrap_div(layout)               // 第一块：Label + 输入
  │    ├─ [标签]  wrap_label(label_width)
  │    │      .text_sm().font_medium()
  │    │      .child(标签内容)
  │    │      .required → 红色 "*"          // ★ 必填星号
  │    └─ [输入]  div().w_full().children(self.children)
  │
  └─ wrap_div(layout)               // 第二块：描述（错位对齐）
       ├─ [水平布局时] 空的占位 label_width   // 让描述和输入对齐
       └─ description / description_fn        // 小号 muted 文字
```

几个要点：

- **方向感知**：`wrap_div` 在垂直布局下是 `v_flex()`，水平布局下是 `h_flex()`。所以同一份 `Field` 在两种表单方向下都能正确排版。
- **标签宽度只在水平布局生效**：垂直布局下 `label_width` 为 `None`。
- **描述的对齐占位**：水平布局时，为了让「描述」和「输入」左对齐，会先塞一个宽度等于 `label_width` 的空 `div`（[field.rs:334-339](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L334-L339)）。

#### 4.2.3 源码精读

**`FieldProps`：表单级与字段级的共享参数包**。

[crates/ui/src/form/field.rs:11-19](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L11-L19) —— 注意它是 `pub(super)`，外部用不到，只在 `form` 模块内流转；`Field` 自己也有一个 `props`，默认会被 `Form` 覆盖。

```rust
#[derive(Clone, Copy)]
pub(super) struct FieldProps {
    pub(super) size: Size,
    pub(super) layout: Axis,
    pub(super) columns: usize,
    pub(super) label_width: Option<Pixels>,
    pub(super) label_text_size: Option<Rems>,
}
```

[crates/ui/src/form/field.rs:21-31](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L21-L31) —— 默认值：垂直布局、默认尺寸、1 列、标签宽 140px。

**`FieldBuilder`：标签/描述既能是文字也能是任意元素**。

[crates/ui/src/form/field.rs:33-59](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L33-L59) —— 标签和描述都接受 `FieldBuilder`，它有三种形态：纯字符串（`String`/`&str`/`SharedString`）、闭包生成的元素（`Element`）、现成视图（`AnyView`）。这就是为什么 `label("Name")` 和 `label_fn(|_,_| div()...))` 都能用。

```rust
pub enum FieldBuilder {
    String(SharedString),
    Element(Rc<dyn Fn(&mut Window, &mut App) -> AnyElement>),
    View(AnyView),
}
```

**`Field` 结构：单字段的所有配置**。

[crates/ui/src/form/field.rs:80-97](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L80-L97) —— 逐字段含义见注释：`label`、`description`、`children`（真正的输入控件）、`visible`、`required`、`align_items`、`col_span`/`col_start`/`col_end`。**没有任何 `error` 字段**——这正是「校验靠约定」的物证。

**props 同步（Form 的注入点）**：

[crates/ui/src/form/field.rs:176-183](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L176-L183) —— `pub(super)`，只给 `Form` 调用：把下标变成 `id`，用表单级 props 覆盖字段自己的 props。

```rust
pub(super) fn props(mut self, ix: usize, props: FieldProps) -> Self {
    self.id = ix.into();
    self.props = props;
    self
}
```

**必填星号（红色 `*`）**：

[crates/ui/src/form/field.rs:293-321](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L293-L321) —— 标签渲染逻辑。标签文字外层 `overflow_x_hidden` 防溢出；当 `required` 为真，在标签后追加一个颜色为 `cx.theme().danger` 的 `*`。

```rust
.when(self.required, |this| {
    this.child(
        div().text_color(cx.theme().danger).child("*"),
    )
})
```

**描述渲染（校验错误就挂在这里）**：

[crates/ui/src/form/field.rs:330-348](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L330-L348) —— 描述固定用 `text_xs()` + `muted_foreground` 色，渲染在输入下方（错位对齐）。校验错误时，我们就**不走 `description`，改走 `description_fn`，自己指定红色**，从而得到「红色错误提示」的效果。

```rust
.when_some(self.description, |this, builder| {
    this.child(
        div()
            .text_xs()
            .text_color(cx.theme().muted_foreground)  // 普通描述是灰色
            .child(builder.render(window, cx)),
    )
})
```

**列跨度**：

[crates/ui/src/form/field.rs:203-221](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L203-L221) —— `col_span`（占几列）、`col_start`/`col_end`（定位到第几列），直接映射到 CSS grid 的同名属性。

#### 4.2.4 代码实践

**实践目标**：验证「`description_fn` + `danger` 色 = 红色错误提示」这条约定。

**操作步骤**（源码阅读型实践）：

1. 在 [form_story.rs:219-227](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/form_story.rs#L219-L227) 看 Bio 字段如何用 `description_fn` 渲染自定义内容：

   ```rust
   field()
       .label("Bio")
       .child(Input::new(&self.bio_input))
       .description_fn(|_, _| {
           div().child("Use at most 100 words to describe yourself.")
       }),
   ```

2. 对照 [field.rs:340-347](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L340-L347) 的默认描述是灰色（`muted_foreground`）。

**需要观察的现象 / 预期结果**：

- 如果你想让这段描述变红，只需在闭包里给 `div()` 加 `.text_color(cx.theme().danger)`。因为 `description_fn` 的闭包返回的是任意 `IntoElement`，颜色完全由你控制——这就是「错误提示复用描述槽位」的原理。

> 完整的红色错误提示实现见第 5 节综合实践。

#### 4.2.5 小练习与答案

**练习 1**：在水平布局下，为什么描述文字会自动和输入框左对齐，而不是顶到最左边？

**参考答案**：因为 [field.rs:334-339](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L334-L339) 在水平布局时，会先塞一个宽度等于 `label_width` 的空 `div` 当占位，把描述顶到和输入框相同的起始位置。

**练习 2**：`Field` 实现了 `ParentElement`（见 [field.rs:224-228](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L224-L228)）。`.child(Input::new(...))` 塞进去的东西最终渲染在哪？

**参考答案**：渲染在第一块的「输入区」，即 [field.rs:322-328](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L322-L328) 那个 `div().w_full().flex_1().children(self.children)`——它是标签右侧（水平）或下方（垂直）的主体控件区域。

**练习 3**：如果既不设 `label` 也不设 `label_indent(false)`，水平布局下输入框会出现在什么位置？

**参考答案**：`label_indent` 默认是 `true`（[field.rs:110](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/form/field.rs#L110)）。所以即便没设 `label`，输入框也会被预留出一段 `label_width` 的缩进，与其它有标签的字段对齐。想让输入框顶到最左，就调用 `.label_indent(false)`（见 [form_story.rs:230](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/form_story.rs#L230) 的全宽字段示例）。

---

## 5. 综合实践：带校验的登录表单

把 `Form` 的布局、`Field` 的标签/必填/描述、以及「校验状态外置 + `description_fn` 红色错误」串起来，做一个登录表单。

**实践目标**：

- 用 `v_form()` + `field()` 组织「用户名 / 密码」两个字段。
- 用户名为必填，校验「非空」；密码校验「至少 6 位」。
- 校验失败时，在对应字段下方用 `cx.theme().danger` 显示红色错误。

**设计要点**：

1. 校验状态 `username_error: Option<String>` / `password_error: Option<String>` 由 View 持有（状态外置）。
2. `Field` 无内置错误字段，错误文案通过 `description_fn` 渲染，颜色自定义为 `danger`。
3. 用 `.when_some(err, |field, e| field.description_fn(...))` 实现「有错才画错误、没错不画任何描述」。
4. 字段值从 `InputState` 读取：`self.username.read(cx).value()`（该方法见 [input/state.rs:1158](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/input/state.rs#L1158)）。

**示例代码**（这是一个可放进 `examples/` 独立示例的最小程序，遵循 hello_world 的启动骨架，仅供阅读与参考）：

```rust
// 示例代码：login_form（非仓库原有代码，供学习参考）
use gpui::{
    App, Application, Context, IntoElement, ParentElement, Render, Styled, Window, WindowOptions,
    px,
};
use gpui_component::{
    ActiveTheme, Root, Sizable,
    button::Button,
    form::{field, v_form},
    h_flex, v_flex,
    input::{Input, InputState},
};

struct LoginForm {
    username: gpui::Entity<InputState>,
    password: gpui::Entity<InputState>,
    username_error: Option<String>,
    password_error: Option<String>,
}

impl LoginForm {
    fn new(window: &mut Window, cx: &mut App) -> Self {
        Self {
            username: cx.new(|cx| InputState::new(window, cx).placeholder("请输入用户名")),
            password: cx.new(|cx| InputState::new(window, cx).placeholder("请输入密码")),
            username_error: None,
            password_error: None,
        }
    }

    // 校验：结果写入 *error 字段，并 cx.notify() 触发重绘
    fn validate(&mut self, cx: &mut Context<Self>) -> bool {
        let name = self.username.read(cx).value().to_string();
        let pwd = self.password.read(cx).value().to_string();
        self.username_error = if name.is_empty() {
            Some("用户名不能为空".into())
        } else {
            None
        };
        self.password_error = if pwd.len() < 6 {
            Some("密码至少 6 位".into())
        } else {
            None
        };
        cx.notify();
        self.username_error.is_none() && self.password_error.is_none()
    }
}

impl Render for LoginForm {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        // 先把 danger 色读出来（Copy），供闭包捕获
        let danger = cx.theme().danger;

        v_flex()
            .size_full()
            .items_center()
            .justify_center()
            .child(
                v_flex()
                    .w(px(360.))
                    .gap_4()
                    .child(
                        v_form()
                            .label_width(px(72.))
                            .child(
                                field()
                                    .label("用户名")
                                    .required(true)
                                    .child(Input::new(&self.username))
                                    // 有错才画红色描述，没错则保持原样
                                    .when_some(self.username_error.clone(), |this, err| {
                                        this.description_fn(move |_, _| {
                                            div().text_color(danger).child(err.clone())
                                        })
                                    }),
                            )
                            .child(
                                field()
                                    .label("密码")
                                    .required(true)
                                    .child(Input::new(&self.password))
                                    .when_some(self.password_error.clone(), |this, err| {
                                        this.description_fn(move |_, _| {
                                            div().text_color(danger).child(err.clone())
                                        })
                                    }),
                            ),
                    )
                    .child(
                        // 提交按钮放在一个不缩进的字段里，与输入对齐
                        field().label_indent(false).child(
                            Button::new("submit")
                                .primary()
                                .w_full()
                                .child("登录")
                                .on_click(cx.listener(|this, _, _, cx| {
                                    if this.validate(cx) {
                                        println!("登录成功");
                                    }
                                })),
                        ),
                    ),
            )
    }
}

fn main() {
    Application::new().run(move |cx| {
        gpui_component::init(cx); // 必须最先调用（见 u1-l4）
        cx.open_window(WindowOptions::default(), |window, cx| {
            let view = cx.new(|cx| LoginForm::new(window, cx));
            cx.new(|cx| Root::new(view, window, cx)) // 窗口顶层必须是 Root
        })
        .expect("Failed to open window");
    });
}
```

**操作步骤**：

1. 在 `examples/` 下新建一个独立 crate（参考 `examples/hello_world` 的 `Cargo.toml` 结构），把上面代码放进 `src/main.rs`，依赖加上 `gpui` 与 `gpui-component`。
2. `cargo run -p <你的包名>` 启动。
3. 直接点「登录」，观察两个字段下方是否各自出现红色错误。
4. 输入合法的用户名与 6 位以上密码再点「登录」，错误消失、控制台打印「登录成功」。

**需要观察的现象 / 预期结果**：

- 用户名为空时，其字段下方显示红色「用户名不能为空」，且标签后有红色 `*`（`required`）。
- 密码不足 6 位时，密码字段下方显示红色「密码至少 6 位」。
- 输入合法后，错误描述消失（因为 `username_error`/`password_error` 变 `None`，`when_some` 不再挂描述）。

> 运行结果待本地验证（本环境无法编译运行桌面窗口）。若 `value()` 返回类型或 `InputState` 构造方式与本示例有出入，以 [form_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/form_story.rs) 的真实用法为准。

---

## 6. 本讲小结

- `Form` 是无状态的 CSS grid 容器，负责**整张表的方向、列数、标签宽度、尺寸**；它把这一份配置打包成 `FieldProps`，在渲染时通过 `field.props(ix, props)` **统一下发**给每个字段。
- `Field` 是单行字段的排版单元，把「标签 + 输入 + 描述」按方向（垂直/水平）对齐；提供 `label`/`label_fn`、`required`、`description`/`description_fn`、`visible`、`col_span` 等配置。
- `label` 和 `description` 都接受 `FieldBuilder`，所以既能传字符串，也能传闭包生成的任意元素——这是「错误提示复用描述槽位」的基础。
- **`Field` 没有内置 `error` 字段**：必填靠 `required(true)` 画红色 `*`；校验错误靠「状态外置到 View + 用 `description_fn` 渲染 `cx.theme().danger` 红色文字」实现。
- `Form::child` 只接受 `Field`（`impl Into<Field>` 当前无其它实现），保证直接子节点都是字段。
- 选型：需要整表统一布局用 `Form`；单字段排版用 `Field`；多列用 `.columns(n)` + 字段 `.col_span(k)`。

## 7. 下一步学习建议

- **横向对比控件**：本讲的字段里用到了 `Input`（[u4-l4](u4-l4-input-and-inputstate.md)），建议继续阅读 [u4-l2 开关选择](u4-l2-switch-checkbox-radio.md)，把 `Switch`/`Checkbox`/`Radio` 也放进 `Field` 组合使用，体会「`Field` 是控件无关的排版容器」。
- **进阶布局**：`Form` 的多列机制依赖 CSS grid。后续学习 [u6 Dock 布局系统](u6-l1-dockarea-and-dockitem.md) 时会遇到更复杂的布局树，届时可对比 grid 与树形布局的取舍。
- **扩展阅读**：
  - 官方表单文档 [docs/docs/components/form.md](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/docs/docs/components/form.md)，含分组表单、条件字段等更多范例。
  - 真实示例 [crates/story/src/stories/form_story.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/story/src/stories/form_story.rs)，看它如何在一个表单里混排 `Select`、`DatePicker`、`ColorPicker` 等控件。
