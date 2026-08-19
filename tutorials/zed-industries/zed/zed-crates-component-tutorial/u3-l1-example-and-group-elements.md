# u3-l1 ComponentExample 与 ComponentExampleGroup 布局元素

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `RenderOnce` 元素与 `#[derive(IntoElement)]` 派生宏是如何配合，把一个普通结构体变成可以塞进 `div().child(...)` 的 UI 元素的。
2. 逐行读懂 `ComponentExample`（单张示例卡片）的 `render` 实现，并能预测它在界面上的渲染结构：变体名、可选描述、带斜纹背景与圆角边框的展示区。
3. 逐行读懂 `ComponentExampleGroup`（分组容器）的 `render` 实现，说清楚 `with_title` 分支比无标题分支多渲染了什么。
4. 理解 `pattern_slash` 斜纹背景函数和 theme 颜色令牌（`text`、`text_muted`、`border`、`surface_background`）的使用方式。
5. 识破一个关键真相：`width` 构建器真实生效，而 `grow` 与 `vertical` 两个构建器选项在当前 HEAD 的 `render` 实现里根本没有被读取——这是历史遗留，也是本教「批判性读源码」的最佳素材。

本讲承接 u2-l1 的结论：`Component::preview()` 返回一个 `AnyElement`。那么这个 `AnyElement` 里面装的通常是什么？答案就是本讲的两个布局元素以及下一讲（u3-l2）的四个辅助函数。

## 2. 前置知识

### 2.1 Render 与 RenderOnce 的区别

在 GPUI 里有两种「把状态变成元素树」的方式：

- **`Render`**：给「有持久状态的实体视图」用的。`Entity<T>` 里的 `T` 实现 `Render`，框架在每次重绘时通过 `&mut self` 调用它。比如组件预览应用里的 `ComponentPreview` 本身。
- **`RenderOnce`**：给「纯数据组装的元素」用的。它没有实体状态，构造完成后被消费一次就丢弃。`render(self, ...)` 拿走所有权（`self` 不是引用），所以它天然适合链式构建器：构造 → 当作 child 挂到别的元素上 → 布局阶段调用一次 `render` 产出真正的 `div` 树。

本讲的两个元素 `ComponentExample` 和 `ComponentExampleGroup` 都是 `RenderOnce`。它们是「描述卡片长什么样」的数据包，不是有状态的视图。

### 2.2 FluentBuilder：读懂 render 代码的语法前提

`render` 实现里大量出现 `.map(...)`、`.when(...)`、`.when_some(...)`，它们不是 `div` 独有的方法，而是 gpui 的 `FluentBuilder` trait 提供的通用流式工具（位于 [crates/gpui/src/util.rs:L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/util.rs#L11)）：

- `.map(f)`：把 `self` 传给闭包，返回闭包结果——用来在链中间做一次「条件分支再继续链」。
- `.when(cond, f)`：条件为真才执行闭包里的样式修改。
- `.when_some(option, f)`：`Option` 有值才执行，闭包拿到解包后的值。

看到 `.map(|this| if ... { this.w(width) } else { this.w_full() })` 就应立刻反应过来：「二选一设置宽度，然后继续链」。

### 2.3 Tailwind 风格的样式方法与单位

gpui 元素的样式方法命名借鉴 Tailwind CSS。本讲会用到：

| 方法 | Tailwind 含义 | 视觉效果 |
|---|---|---|
| `.pt_2()` | padding-top: 0.5rem | 顶部留 8px 内边距（1rem 按 16 逻辑像素计） |
| `.gap_3()` / `.gap_4()` / `.gap_6()` | gap: 0.75 / 1 / 1.5rem | 子元素间距 12 / 16 / 24px |
| `.p_8()` | padding: 2rem | 四周 32px 内边距 |
| `.min_h(px(100.))` | min-height: 100px | 最小高度 100 逻辑像素 |
| `.mt_4()` / `.mb_1()` | margin-top 1rem / bottom 0.25rem | 上外边距 16px / 下外边距 4px |
| `.rounded_xl()` | border-radius 大档 | 圆角矩形 |
| `.border_1()` | border-width: 1px | 1px 边框 |
| `.h_px()` | height: 1px | 1px 高的水平细线 |

字号单位有两种：`rems(1.0)` 是正常正文大小，`rems(0.875)` 是 87.5% 的缩小字号；分组标题则直接用 `px(10.)` 指定一个极小的绝对字号。

### 2.4 theme 颜色令牌

`cx.theme().colors()` 返回当前主题的调色板，本讲用到四个令牌（定义于 [crates/theme/src/styles/colors.rs:L17-L91](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/theme/src/colors.rs#L17-L91)）：

| 令牌 | 行号 | 语义 |
|---|---|---|
| `text` | L89 | 主要文字颜色 |
| `text_muted` | L91 | 弱化的次级文字颜色 |
| `border` | L17 | 边框颜色 |
| `surface_background` | L31 | 表面背景色 |

这些令牌都是 `Hsla`。注意 `Hsla::opacity(factor)` 是**乘法**而非赋值（[crates/gpui/src/color.rs:L637-L644](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/color.rs#L637-L644)）：`color.opacity(0.25)` 得到的新颜色满足 \( \alpha' = \alpha \times 0.25 \)，即「在原有透明度基础上再打个二五折」。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [crates/component/src/component_layout.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L1-L206) | **本讲主角**，全文件仅 206 行：两个 `RenderOnce` 元素 + 构建器 + 四个辅助函数 |
| [crates/gpui/src/element.rs:L174-L184](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/element.rs#L174-L184) | `RenderOnce` trait 的定义与文档 |
| [crates/gpui_macros/src/gpui_macros.rs:L32-L37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_macros/src/gpui_macros.rs#L32-L37) | `#[derive(IntoElement)]` 派生宏入口 |
| [crates/gpui/src/util.rs:L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/util.rs#L11) | `FluentBuilder`（`map` / `when` / `when_some`） |
| [crates/gpui/src/color.rs:L827-L838](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/color.rs#L827-L838) | `pattern_slash` 斜纹背景构造函数 |
| [crates/theme/src/styles/colors.rs:L17-L91](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/theme/src/styles/colors.rs#L17-L91) | 颜色令牌定义 |
| [crates/ui/src/components/divider.rs:L172-L238](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L238) | `Divider::preview`——两个布局元素的真实消费者，实践任务的对照样本 |

## 4. 核心概念与源码讲解

### 4.1 RenderOnce 实现：一次性元素与 `#[derive(IntoElement)]` 的配合

#### 4.1.1 概念说明

`ComponentExample` 是一个普通结构体：四个公有无状态的字段。它没有对应的 `Entity`，不需要 `cx.notify()`，也不会被二次渲染。它是「一次性元素」：构造 → 挂到父元素上 → 布局阶段 `render(self)` 被调用一次、拿走所有权、换成一棵 `div` 树，然后这个结构体本身就不复存在了。

这种模式解决的问题是：gpui 的 `.child()` 只接受实现了 `IntoElement` 的类型。如果每个「可复用的 UI 模式」都手写 `impl IntoElement`，代码会非常啰嗦。于是 gpui 提供了约定：**你实现 `RenderOnce`，派生宏替你实现 `IntoElement`**。

#### 4.1.2 核心流程

一个 `RenderOnce` 元素从构造到像素的完整生命周期：

```text
1. 构造阶段（业务代码，如 Divider::preview）
   single_example("Default", elem)
       └─> ComponentExample::new("Default", elem.into_any_element())   // 数据打包
2. 挂载阶段
   ComponentExampleGroup 的 render 里 .children(self.examples)
       └─> 对每个 ComponentExample 调 into_element()                  // 由派生宏生成
3. 渲染阶段（框架驱动，每帧最多一次）
   ComponentExample::render(self, window, cx)                          // self 被消费
       └─> 返回一棵 div 元素树（真正的布局节点）
4. 布局与绘制阶段
   框架对 div 树做 flexbox 布局、绘制
```

关键时序认知：**构造和渲染是分离的两个阶段**。你在 `preview()` 里写的只是「配方」，`render` 直到布局阶段才执行——这也呼应 u2-l1 讲过的「注册时只存函数指针、渲染时才执行」。

#### 4.1.3 源码精读

先看 `RenderOnce` trait 本体（gpui 侧）：

> [crates/gpui/src/element.rs:L174-L184](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/element.rs#L174-L184) — 官方文档写明：可以在任何 `RenderOnce` 类型上派生 `IntoElement`；`RenderOnce` 用于「用纯数据构造可复用组件」，让你调用这种模式时不破坏元素 API 的流式构建器风格；`render` 拿走 `self` 的所有权（对比 `Render::render` 只拿 `&mut self`）。

```rust
pub trait RenderOnce: 'static {
    fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement;
}
```

再看派生宏入口：

> [crates/gpui_macros/src/gpui_macros.rs:L32-L37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_macros/src/gpui_macros.rs#L32-L37) — `#[derive(IntoElement)]` 为任意 `RenderOnce` 类型生成 `IntoElement` 实现，把它包装成可以作 child 使用的元素。

最后看本讲两个结构体上的使用（component 侧）：

> [crates/component/src/component_layout.rs:L7-L14](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L7-L14) — `ComponentExample` 结构体定义，`#[derive(IntoElement)]` 挂在第一行；四个字段全部 `pub`：`variant_name`（变体名）、`description`（可选描述）、`element`（被展示的任意元素）、`width`（可选固定宽度）。

> [crates/component/src/component_layout.rs:L92-L100](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L92-L100) — `ComponentExampleGroup` 结构体定义，同样派生 `IntoElement`；字段为 `title`（可选分组标题）、`examples`（卡片列表）、`width`、`grow`、`vertical`。

注意两点：

1. 两个结构体都**没有**派生 `Clone`——因为 `RenderOnce` 的 `render` 拿走所有权，元素树里每个节点只需要存在一份。
2. `element: AnyElement` 是类型擦除后的任意元素（u2-l1 已建立该概念），所以卡片里可以放任何东西：一个按钮、一段布局、甚至空 `div`。

#### 4.1.4 代码实践：跟踪一次真实的调用链

1. **实践目标**：验证「构造 → 挂载 → 渲染」三阶段模型，确认 `single_example` 产出的卡片是如何最终被 `ComponentExample::render` 消费的。
2. **操作步骤**：
   - 打开 [crates/ui/src/components/divider.rs:L172-L238](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L238)，这是 `Divider::preview`。
   - 从 [L179](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L179) 的 `single_example("Default", Divider::horizontal().into_any_element())` 出发，跳转到 `single_example` 的定义（[component_layout.rs:L185-L190](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L185-L190)），确认它只是 `ComponentExample::new` 的别名。
   - 再看这个卡片被装进 `vec!` 传给 `example_group_with_title(...)`（[L176-L189](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L176-L189)），即 `ComponentExampleGroup::with_title`。
   - 最后在 `ComponentExampleGroup::render` 的 [L145](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L145) 找到 `.children(self.examples)`——卡片在这里被挂载，`IntoElement`（派生宏产物）在这里被调用。
3. **需要观察的现象**：整条链上没有任何一处直接调用 `ComponentExample::render`——它只由框架在布局阶段调用。
4. **预期结果**：你能画出这样的调用链：`single_example → ComponentExample::new →（存入 Vec）→ with_title → group.render 中 .children() → 框架布局阶段调 card.render`。
5. 本实践为纯源码阅读，无需运行（「待本地验证」的部分可选：在 `ComponentExample::render` 首行临时加一行 `println!` 再跑 u1-l2 的组件预览，观察每个卡片只打印一次；**不要提交这处源码修改**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ComponentExample` 不实现 `Render` 而实现 `RenderOnce`？

**答案**：`Render` 面向有持久状态的 `Entity` 视图，框架持有实体并可反复通过 `&mut self` 重绘；而示例卡片是无状态的纯数据描述，构造一次、渲染一次即可。`RenderOnce::render(self)` 拿走所有权也正好匹配「字段里的 `AnyElement` 直接移动进 div 树」的用法，无需克隆。

**练习 2**：删掉 `#[derive(IntoElement)]` 会发生什么？

**答案**：`ComponentExample` 仍实现着 `RenderOnce`，但不再实现 `IntoElement`，于是它不能再作为 `.children(self.examples)` 的子项传入（`children` 要求 `impl IntoElement`），`component_layout.rs` 与所有调用 `single_example` 的组件 preview 都会编译报错。`RenderOnce` 与 `IntoElement` 派生的配合缺一不可。

**练习 3**：`RenderOnce` trait 为什么要求 `'static`？

**答案**：产出的元素要能被元素树长期持有并在后续帧使用，不能携带可能提前失效的非静态借用；`ComponentExample` 的字段（`SharedString`、`AnyElement`、`Option<Pixels>`）都是拥有型数据，天然满足。

### 4.2 ComponentExample：单张示例卡片

#### 4.2.1 概念说明

组件预览页的主体是一张张「示例卡片」。每张卡片展示组件的**一个变体**：左上角是变体名（如 `Default`、`Dashed`），变体名下面可以有一行弱化的可选描述，卡片下方是一个大的展示区——展示区有最小高度、内容居中、四周圆角、1px 半透明边框，底色是**斜纹图案**。

斜纹背景不是装饰这么简单：它让「展示区本身的范围」在视觉上可辨——即使被展示的组件渲染为空（比如空状态），读者也能看清「这里有一块画布，组件确实没画东西」。下一讲的 `empty_example` 正是依赖这一点来表达「合法的空渲染」。

#### 4.2.2 核心流程

`ComponentExample::render` 产出的元素树（伪代码）：

```text
卡片根 div（纵向 flex，gap_3，pt_2，宽度 = 固定 width 或 100%）
├── 标题区 div（纵向 flex）
│   ├── 变体名 div（text_size = 1rem，颜色 = 令牌 text）
│   └── [仅当 description 为 Some] 描述 div（text_size = 0.875rem，颜色 = 令牌 text_muted）
└── 展示区 div（最小高 100px，宽 100%，内边距 32px，横向 flex，
     子项水平+垂直居中，圆角 xl，1px 边框 border×0.5 透明度，
     背景 = pattern_slash(surface_background×0.25, 12, 12)）
    └── element（被展示的任意元素）
```

两条分支逻辑都用 `FluentBuilder` 表达：宽度用 `.map` 二选一（[L20-L26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L20-L26)），描述用 `.when_some` 按需追加（[L40-L47](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L40-L47)）。

#### 4.2.3 源码精读

**结构体与构建器**：

> [crates/component/src/component_layout.rs:L71-L90](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L71-L90) — 三个关联函数：`new` 接收变体名与 `AnyElement`，`description` 与 `width` 是典型的消费式构建器（`mut self ... -> Self`），分别把 `Option` 字段从 `None` 填成 `Some`。

**render 第一段——卡片根节点**：

> [crates/component/src/component_layout.rs:L18-L29](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L18-L29) — 根 `div`：顶部内边距 `pt_2`；用 `.map` 在「固定宽度 `w(width)`」与「占满父容器 `w_full()`」之间二选一；随后设为纵向 flex、子项间距 `gap_3`。它的两个 child 就是标题区与展示区。

**render 第二段——标题区**：

> [crates/component/src/component_layout.rs:L30-L48](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L30-L48) — 第一个 child 是纵向 flex 的标题区。变体名行：`text_size(rems(1.0))`、颜色取令牌 `text`（正常正文强度）；随后 `.when_some(self.description, ...)` 仅在描述存在时追加第二个 div：`text_size(rems(0.875))`、颜色取令牌 `text_muted`（缩小一号、颜色弱化），形成主/次文本层级。

**render 第三段——展示区（本讲视觉核心）**：

> [crates/component/src/component_layout.rs:L49-L66](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L49-L66) — 展示区 div 依次设置：`min_h(px(100.))` 保证空组件也有画布高度；`w_full()` 横向占满；`p_8()` 四周 32px 留白；`flex()` + `items_center()` + `justify_center()` 让被展示元素水平垂直双居中；`rounded_xl()` 大圆角；`border_1()` + `border_color(...opacity(0.5))` 一条半透明细边框；`bg(pattern_slash(...opacity(0.25), 12.0, 12.0))` 斜纹图案底色；最后 `child(self.element)` 把真正的组件放进去。

其中两个细节值得单独展开：

> [crates/gpui/src/color.rs:L827-L838](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/color.rs#L827-L838) — `pattern_slash(color, width, interval)` 返回一个 `Background`（tag 为 `PatternSlash`）：把颜色与两个以设计像素为单位的几何参数（条纹宽度 12、间距 12）编码进一个背景值。它不是图片资源，是运行期按参数生成的图案背景——换主题时颜色自动跟随令牌变化。

边框透明度：`.opacity(0.5)` 作用于 `Hsla`，由 [crates/gpui/src/color.rs:L637-L644](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/color.rs#L637-L644) 可知是 \( \alpha' = \alpha \times \mathrm{clamp}(0.5, 0, 1) \)。同理展示区底色是 `surface_background` 的 25% 透明版本。用「令牌 × 透明度系数」而非新增令牌，是这套设计系统里非常常见的派生手法——既跟随主题，又不膨胀调色板。

#### 4.2.4 代码实践：纸面绘制布局层级图（本讲核心实践）

1. **实践目标**：不看任何图示，仅凭 [L16-L69](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L16-L69) 的 `render` 实现，在纸上（或任意画图工具）画出 `ComponentExample::new("Demo", elem).description("一个演示")` 的布局层级图，并把每个视觉部件标注到具体代码行。
2. **操作步骤**：
   - 画出根节点，标注它的四个布局属性来自 [L18-L29](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L18-L29)（`pt_2` / 宽度二选一 / `flex_col` / `gap_3`）。
   - 画两个子节点：标题区（[L30-L48](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L30-L48)）与展示区（[L49-L66](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L49-L66)）。
   - 在标题区下画两个叶子：变体名（对应 [L34-L39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L34-L39) 的 `self.variant_name`）与描述（对应 [L40-L47](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L40-L47) 的 `when_some` 分支），并标出两者字号（1rem vs 0.875rem）与颜色令牌（`text` vs `text_muted`）的差别。
   - 在展示区节点上标出五个视觉特征对应的行：最小高度 [L51](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L51)、居中 [L55-L56](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L55-L56)、圆角与边框 [L57-L59](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L57-L59)、斜纹背景 [L60-L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L60-L64)、被展示元素 [L65](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L65)。
3. **需要观察的现象**：绘图过程中你应当发现「描述是否存在」不改变树的形状骨架（只是少一个叶子），而「width 是否设置」会改变根节点宽度策略。
4. **预期结果**：与 4.2.2 的伪代码树一致——根（pt_2 / flex_col / gap_3 / w_full）→ 标题区（变体名 + 描述）+ 展示区（min_h 100 / p_8 / 双居中 / rounded_xl / border 0.5 / 斜纹 0.25 底 / element）。
5. 本实践为纸面推演，结论可由源码直接验证，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：变体名和描述为什么一个用 `text` 一个用 `text_muted`？如果反过来会怎样？

**答案**：变体名是卡片的主信息（读者按名字找变体），用主文字色；描述是补充说明，用弱化色 + 缩小字号形成视觉层级。反过来会让次要信息喧宾夺主，且两个同强度文本堆叠时失去层次。

**练习 2**：一个卡片如果不调用 `.description(...)`，render 产出的树里会少什么？

**答案**：`self.description` 为 `None`，[L40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L40) 的 `when_some` 直接原样返回 `this`，标题区只剩变体名一个叶子；卡片其余部分（包括 `gap_3` 的占位逻辑——flex 的 gap 只在真实子项之间生效）完全不变。

**练习 3**：把展示区背景里的 `opacity(0.25)` 改成 `opacity(1.0)`（纸面推演，不要改源码），界面会怎么变？

**答案**：斜纹会以 `surface_background` 的全强度绘制，图案明显加重，可能压过被展示组件本身的视觉重量；`0.25` 的取值是为了让画布可辨又不干扰主角。这体现了「背景必须比内容弱」的展示页设计原则。

### 4.3 ComponentExampleGroup：分组容器与构建器选项

#### 4.3.1 概念说明

`ComponentExampleGroup` 是卡片的容器：它把若干张 `ComponentExample` 纵向堆叠，并可选地在顶部渲染一个「大写小标题 + 通栏分隔线」的分组头。你在组件预览里看到的 `HORIZONTAL DIVIDERS`、`VERTICAL DIVIDERS`、`EXAMPLE USAGE` 这类小节标题，就是这个分组头。

它对外暴露五个构建入口：`new`（无标题）、`with_title`（带标题）、`width`（固定宽度）、`grow()`、`vertical()`。前三个真实影响渲染，后两个——这是本讲最重要的批判性发现——**在当前实现里没有任何效果**。

#### 4.3.2 核心流程

`ComponentExampleGroup::render` 产出的元素树：

```text
分组根 div（纵向 flex，text_sm，颜色 = text_muted，宽度 = 固定 width 或 100%）
├── [仅当 title 为 Some，由 when_some 追加]
│   ├── （同时给根 div 补一个 gap_4）
│   └── 分组头 div（横向 flex，items_center，gap_3，mt_4，mb_1）
│       ├── 标题 div（flex_none，字号 10px，title.to_uppercase()）
│       └── 尾部细线 div（h_px，w_full，flex_1，背景 = border）  ← 弹性占位成「通栏」
└── 卡片列表容器 div（flex，flex_col，items_start，w_full，gap_6）
    ├── ComponentExample 卡片 render 的产物
    ├── ComponentExample 卡片 render 的产物
    └── ...
```

注意 `flex_1()` 的妙用：标题文字 `flex_none` 不伸缩，尾部细线 `flex_1` 吃掉剩余全部宽度，两者拼出来才是「标题 + 延伸到行尾的横线」的通栏分隔线效果。

#### 4.3.3 源码精读

**结构体与两个构造函数**：

> [crates/component/src/component_layout.rs:L152-L170](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L152-L170) — `new` 与 `with_title` 都直接产出完整结构体：唯一区别是 `title` 字段填 `None` 还是 `Some(title.into())`；`width`/`grow`/`vertical` 一律先置默认（`None`/`false`/`false`）。

**render 第一段——根节点与宽度**：

> [crates/component/src/component_layout.rs:L104-L114](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L104-L114) — 根 `div`：纵向 flex、`text_sm`、默认文字色取 `text_muted`（分组标题与整体基调都是弱化色）；宽度同样用 `.map` 在固定 `w(width)` 与 `w_full()` 间二选一。**`width` 构建器真实生效于 [L109-L113](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L109-L113)。**

**render 第二段——with_title 分支（分组头）**：

> [crates/component/src/component_layout.rs:L115-L137](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L115-L137) — `.when_some(self.title, ...)` 有标题时做两件事：其一，给根 `div` 补 `.gap_4()`，让分组头与下方卡片列表之间有 16px 间距；其二，追加分组头 child——横向 flex、垂直居中、`gap_3`、`mt_4`（与上一组拉开距离）`mb_1`（与卡片列表贴近）；内部两个 child 分别是 10px 大写标题（`title.to_uppercase()`，`flex_none` 不收缩）和 1px 高的弹性细线（`w_full` + `flex_1`，颜色取令牌 `border`）。

对比无标题分支：`when_some` 条件不成立时整段跳过，根 `div` 不会获得 `gap_4`，也没有分组头 child。**所以 `with_title` 分支比无标题分支多渲染的东西一共是：根容器上的 `gap_4`、一个分组头行（含上边距 `mt_4` 下边距 `mb_1`）、行内的大写 10px 标题、以及延伸到行尾的 1px 通栏分隔线。**

**render 第三段——卡片列表容器**：

> [crates/component/src/component_layout.rs:L138-L147](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L138-L147) — 无条件渲染的卡片容器：`flex()` + `flex_col()` **硬编码纵向堆叠**、`items_start` 左对齐、`w_full` 占满、`gap_6` 卡片间距 24px，最后 `.children(self.examples)` 把每张卡片经 `IntoElement`（4.1 讲的派生宏产物）挂载进来。

**关键真相——`grow` 与 `vertical` 是无消费的字段**：

> [crates/component/src/component_layout.rs:L175-L182](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L175-L182) — `grow()` 与 `vertical()` 构建器只是把对应布尔字段置 `true`。

请回到 render 实现（[L103-L150](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L103-L150)）逐行检查：整个函数只读取了 `self.width`（L109）、`self.title`（L115）、`self.examples`（L145）三个字段，**没有任何一行读取 `self.grow` 或 `self.vertical`**。卡片容器在 [L141](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L141) 无条件 `flex_col()`，所以「是否纵向」根本不构成分支。

这不是最近的改动引入的疏漏。用 `git log -S 'self.vertical' -- src/component_layout.rs` 可以看到该文件自创建提交 `c5d8407df4`（component crate cleanup，2025-05）起，`grow`/`vertical` 就只被写入、从未被读取——它们是从更早的组件预览实现里搬家带过来的遗留字段。而调用侧并不知情：仓库里至少有数十处写着 `example_group(examples).vertical()`，例如 [crates/ui/src/components/button/copy_button.rs:L198](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/copy_button.rs#L198)、[crates/ui/src/components/chip.rs:L170](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/chip.rs#L170)、[crates/ui/src/components/callout.rs:L360-L361](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/callout.rs#L360-L361)、[crates/ui/src/components/label/spinner_label.rs:L208](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/label/spinner_label.rs#L208)——这些调用全部是**视觉上的空操作**（好在默认就是纵向堆叠，语义碰巧一致）。

> 阅读启示：**字段存在 + 构建器存在 ≠ 渲染生效**。判断一个构建器选项是否有效，唯一可靠的办法是在 `render` 里找到对该字段的读取。这个习惯在你阅读任何链式构建器风格的 UI 框架时都成立。

#### 4.3.4 代码实践：对比两个分支 + 识破空操作构建器

1. **实践目标**：亲手验证 `with_title` 分支与无标题分支的渲染差异；并用源码证据确认 `.grow()`/`.vertical()` 是空操作。
2. **操作步骤**：
   - **步骤 A**：对照 [L104-L147](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L104-L147) 写两棵伪代码树：`example_group(vec![card_a, card_b])` 与 `example_group_with_title("T", vec![card_a, card_b])`，逐行 diff。
   - **步骤 B**：在仓库根目录执行只读检索（示例命令，结果「待本地验证」）：
     ```bash
     # 统计调用 .vertical() 的位置（预期至少数十处）
     git grep -n '\.vertical()' -- 'crates/ui/src/components/**' | wc -l
     # 确认 component_layout.rs 的 render 从不读取 grow / vertical 字段
     git grep -n 'self\.grow\|self\.vertical' -- crates/component/src/component_layout.rs
     ```
   - **步骤 C（可选）**：按 u1-l2 的方式打开组件预览，进入 Callout 组件页——它的 preview（[callout.rs:L360-L361](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/callout.rs#L360-L361)）调用了 `.vertical()`——观察卡片是否纵向堆叠（是，但那是因为 `flex_col` 硬编码，而非 `.vertical()` 的功劳）。
3. **需要观察的现象**：步骤 B 的第二条命令预期**零匹配**（render 中不存在 `self.grow` / `self.vertical` 字样）；步骤 A 的 diff 恰好是 4.3.3 列出的那几项。
4. **预期结果**：两分支差异 = 根容器 `gap_4` + 分组头行（`mt_4`/`mb_1`、10px 大写标题、`flex_1` 通栏细线）；`.grow()`/`.vertical()` 无任何视觉后果。
5. 步骤 B 的检索结果待本地验证；步骤 A 的结论可由源码直接证明。

#### 4.3.5 小练习与答案

**练习 1**：`example_group_with_title("", vec![...])` 传入空字符串标题会渲染出什么？

**答案**：`title` 是 `Some("")`，`when_some` 条件成立，分组头**照常渲染**：一个 `mt_4 mb_1` 的行 + 通栏细线，只是标题文字为空（`"".to_uppercase()` 仍是空串）。于是你会看到一条「无字分隔线」。`Option` 表达的是「有没有标题」，空字符串是「有标题但内容为空」，两者语义不同。

**练习 2**：如果把 `.children(self.examples)` 容器（[L139-L146](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L139-L146)）的 `flex_col()` 改成 `flex_row()`（纸面推演），界面会怎样？这和 `vertical` 字段有什么关系？

**答案**：卡片会横向排成一行；由于卡片 `w_full()`（默认宽度分支），在 `flex_row` 里它们会挤分父容器宽度，视觉立刻崩坏。这说明「纵向」本该是个值得抽象的选项——历史版本可能确实分支过——但当前实现选择硬编码 `flex_col`，`vertical` 字段于是成了无消费的遗留。这正是 `vertical()` 调用遍布各处却无人发现无效的原因：默认行为碰巧就是纵向。

**练习 3**：分组根 `div` 的默认文字色为什么设为 `text_muted` 而不是 `text`？

**答案**：因为分组标题是「小节导航信息」，应当弱于卡片内容；而卡片（`ComponentExample`）内部自带更强的 `text` 用于变体名。父容器设基调色、子卡片按需覆写，是 flex 样式继承思路下的常见做法（`text_color` 影响后代未显式设置颜色的文本）。

## 5. 综合实践

**任务：为 `Divider::preview` 绘制一张完整的「三级渲染树标注图」并产出对照表。**

1. **实践目标**：把本讲三个知识块（RenderOnce 配合、卡片结构、分组结构）串成一张完整的图，做到「看到 preview 代码就能预言界面」。
2. **操作步骤**：
   - 通读 [crates/ui/src/components/divider.rs:L172-L238](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L238) 的 `Divider::preview`：最外层是 `v_flex().gap_6()`，下面挂着三个 `example_group_with_title(...)`（Horizontal Dividers / Vertical Dividers / Example Usage），每组 4/4/1 张 `single_example` 卡片。
   - 绘制三级树：**L1** `v_flex`（preview 自己的容器）→ **L2** 三个 `ComponentExampleGroup` 的 render 产物（每个都带 4.3.2 的分组头）→ **L3** 每组下的 `ComponentExample` 卡片（4.2.2 的结构）。
   - 为树上每个节点标注：来源代码行（divider.rs 的构造行 + component_layout.rs 的 render 行）、关键样式、使用的颜色令牌。
   - 产出一张三列对照表：**视觉部件 | 代码来源 | 颜色/尺寸令牌**。例如「分组头通栏细线 | group render L129-L135 | border」「卡片斜纹背景 | card render L60-L64 | surface_background × 0.25」。
3. **需要观察的现象**：绘图时会发现 Vertical Dividers 组的每张卡片里，元素外面还包了一层 `div().h_16()`（[divider.rs:L195](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L195)）——思考为什么：竖直分隔线自身没有宽度语义，需要外层容器给一个高度基准，否则展示区的居中布局无从谈起。
4. **预期结果**：一棵三级标注树 + 一张对照表；其中 L2 层每组都有「大写 10px 标题 + 通栏线」分组头，L3 层每张卡片都是「变体名 + 斜纹展示区」。
5. **可选验证（待本地验证）**：按 u1-l2 的 `cargo run -p component_preview --example component_preview` 启动预览，打开 Divider 页面与你的图逐项对照；确认所有 `.vertical()`（本 preview 未用）与 `grow` 相关疑问不影响结果。

## 6. 本讲小结

- `ComponentExample` 与 `ComponentExampleGroup` 是 `RenderOnce` 一次性元素：无实体状态，`render(self)` 拿走所有权，由 `#[derive(IntoElement)]` 补上 `IntoElement` 实现后才能挂进元素树；构造与渲染是分离的两个阶段。
- 卡片结构 = 根（`pt_2`/宽度二选一/`flex_col`/`gap_3`）→ 标题区（1rem 变体名 `text` + 可选 0.875rem 描述 `text_muted`）→ 展示区（`min_h 100`/`p_8`/双居中/`rounded_xl`/1px 边框×0.5/`pattern_slash` 斜纹底×0.25）。
- `pattern_slash` 是运行期按（颜色、条纹宽 12、间距 12）参数生成的图案背景，非图片资源；`opacity(f)` 是透明度乘法 \( \alpha' = \alpha f \)。
- 分组结构 = 根（`text_sm`/`text_muted`/宽度二选一）→ 可选分组头（`gap_4` + `mt_4 mb_1` 行 + 10px 大写标题 + `flex_1` 通栏细线）→ 卡片容器（硬编码 `flex_col`、`gap_6`、`.children(examples)`）。
- **批判性结论**：`width` 构建器真实生效；`grow()`/`vertical()` 只写字段、`render` 从不读取，是自文件创建提交起就存在的遗留空操作——「字段与构建器存在」不等于「渲染生效」，判断依据永远是在 render 里找到字段读取。
- 读链式构建器风格 UI 代码的两个语法工具：`FluentBuilder` 的 `map`/`when`/`when_some`，以及 Tailwind 风格样式方法与 theme 令牌的配合。

## 7. 下一步学习建议

下一讲 **u3-l2《布局辅助函数与高质量 preview 的写法》** 将精读本讲文件尾部的四个辅助函数——`single_example`、`empty_example`、`example_group`、`example_group_with_title`（[component_layout.rs:L185-L205](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L185-L205)），重点是你已经见过其结构的 `empty_example` 如何表达「合法的空渲染」，以及「变体分组 + Example Usage」的 preview 组织范式。

继续阅读源码的建议：

1. 重读 [crates/ui/src/components/divider.rs:L172-L238](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L238)，这次关注它如何用 `single_example` 命名变体（`Default`/`Border Color`/`Inset`/`Dashed`）——变体命名是 preview 可读性的关键。
2. 对比一两个更复杂的 preview（如 [crates/ui/src/components/callout.rs:L360-L361](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/callout.rs#L360-L361) 附近），观察它们与本讲标准结构的偏离之处。
3. 有兴趣可追溯 `git log --follow -p crates/component/src/component_layout.rs`，观察分组头样式（`pb_1` + 短横线 → `mt_4 mb_1` + 通栏线）与卡片阴影、斜纹透明度的演化——组件预览体系自身也在被持续设计。
