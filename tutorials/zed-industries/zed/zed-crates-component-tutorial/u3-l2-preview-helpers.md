# 布局辅助函数与高质量 preview 的写法

> 本讲是「组件预览体系」第 9 讲（u3-l2）。上一讲（u3-l1）我们已经拆开了 `ComponentExample`（示例卡片）和 `ComponentExampleGroup`（分组容器）这两个 `RenderOnce` 元素的渲染实现；本讲把视角切回「写 preview 的人」，学习把这两个元素组装成高质量预览页的四个辅助函数，并以 `Divider` 的真实 preview 为范本，总结一套可复用的组织范式。

## 1. 本讲目标

学完本讲，你应该能够：

1. 熟练使用 `single_example`、`empty_example`、`example_group`、`example_group_with_title` 四个辅助函数，产出结构化的 `preview()`。
2. 掌握「变体分组 + Example Usage」的 preview 组织范式：分组对应组件的一个维度，卡片对应该维度的一个取值，最后一组展示真实用法。
3. 理解 `empty_example` 表达「合法的空渲染」的设计意图，知道它和斜纹画布如何配合。
4. 独立为自己的练习组件写出一个包含两个带标题分组和一处空态示例的完整 preview，并跑通组件预览验证。

## 2. 前置知识

本讲默认你已经理解以下内容（前几讲已建立，这里只做一句话复习）：

- **Component trait 的 preview 方法**（u2-l1）：`fn preview(window: &mut Window, cx: &mut App) -> AnyElement`，是关联函数（没有 `self`），注册时只存裸函数指针，渲染组件页时才执行，返回的 `AnyElement` 是类型擦除后的任意元素树。
- **ComponentExample 卡片与 ComponentExampleGroup 分组**（u3-l1）：卡片 = 变体名 + 可选描述 + 斜纹画布展示区（`min_h(100px)`、圆角边框、`pattern_slash` 底纹）；分组 = 可选大写标题 + 通栏细线 + 一列卡片。两者都实现 `RenderOnce`，经 `#[derive(IntoElement)]` 挂入元素树。
- **符号来源**（u1-l3）：写组件时 `use ui::prelude::*`（ui crate 内部写作 `use crate::prelude::*`）即可拿到 `Component`、`ComponentScope`、`single_example`、`example_group` 系列；但注意 `empty_example` **不在**任何 prelude 里，需要写全路径 `component::empty_example`。
- **inventory 链接期注册铁律**（u2-l3）：只有被链接进二进制的 crate 才贡献注册节点。练习组件放在 ui crate 内部就一定能在组件预览里出现，因为 component_preview 链接了 ui。
- **`v_flex`**：ui crate 提供的竖向 flex 容器快捷函数，定义在 [stack.rs:L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/stack.rs#L13)，是几乎所有 preview 的顶层容器。

如果对以上任何一条感到陌生，建议先回看对应讲义再继续。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/component/src/component_layout.rs` | 预览布局元素与辅助函数 | 四个辅助函数的定义（L185-L205） |
| `crates/ui/src/components/divider.rs` | Divider（分隔线）组件 | 真实的三分组 preview（L172-L238），本讲的范本 |
| `crates/ui/src/components/toggle.rs` | Switch / Toggle / Checkbox 等开关组件 | Switch::preview（L993 起），「按维度命名分组」的另一个范例 |
| `crates/ui/src/components/stack.rs` | `v_flex` / `h_flex` 等容器快捷函数 | preview 顶层容器的来源 |
| `crates/ui/src/components.rs` | ui 组件的模块声明清单 | 综合实践中挂载练习组件 `mod` 声明的位置 |
| `crates/ui/src/prelude.rs` | ui 内部 prelude | 确认哪些辅助函数被重导出（L10-L11） |

## 4. 核心概念与源码讲解

### 4.1 词汇表：single_example 与 example_group 系列助手

#### 4.1.1 概念说明

上一讲我们看到，直接构造布局元素要写 `ComponentExample::new("Default", elem)` 和 `ComponentExampleGroup::with_title("States", vec![...])`，名字长、嵌套深，一个稍大的 preview 会被构造器噪音淹没。

`component_layout.rs` 在文件末尾提供了四个自由函数，把构造器包装成一套「预览专用词汇」：

| 函数 | 产出 | 对应构造器 |
| --- | --- | --- |
| `single_example(name, element)` | 一张示例卡片 | `ComponentExample::new` |
| `empty_example(name)` | 一张「空渲染」占位卡片 | `ComponentExample::new` + 固定占位元素 |
| `example_group(examples)` | 一个无标题分组 | `ComponentExampleGroup::new` |
| `example_group_with_title(title, examples)` | 一个带标题分组 | `ComponentExampleGroup::with_title` |

有了这套词汇，preview 代码读起来就像一份目录：「分组标题 → 变体名 → 元素」，声明什么该被展示，而不是怎么构造它。这是一种典型的**领域专用语言（DSL）**手法：底层机制（两个 `RenderOnce` 元素）不变，只在上面铺一层贴近使用场景的薄语法。

#### 4.1.2 核心流程

一个 preview 的组装管线如下：

```text
v_flex()                                  ← 顶层竖向容器（gap_6 控制分组间距）
  .children(vec![
      example_group_with_title(           ← 分组一：维度 A
          "标题 A",
          vec![
              single_example("取值 1", 元素 1.into_any_element()),
              single_example("取值 2", 元素 2.into_any_element()),
          ],
      ),
      example_group_with_title(...),      ← 分组二：维度 B
  ])
  .into_any_element()                     ← 类型擦除，作为 AnyElement 返回
```

对应的元素树结构：

```text
AnyElement
└── v_flex（竖向容器，gap_6 = 分组间距）
    ├── ComponentExampleGroup（分组一，带大写标题 + 通栏细线）
    │   ├── ComponentExample（卡片：变体名 + 斜纹画布）
    │   └── ComponentExample
    └── ComponentExampleGroup（分组二）
        └── ...
```

渲染时逐层调用 `RenderOnce::render`（上一讲精读过每一层的样式细节），最终铺成组件预览页面的内容区。

注意每个 `single_example` 的第二个参数都要以 `.into_any_element()` 结尾——因为卡片构造器的字段类型固定为 `AnyElement`，类型擦除发生在调用方手里，而不是辅助函数内部做泛型转换。

#### 4.1.3 源码精读

四个辅助函数集中在 component_layout.rs 的最后 21 行，逐个看：

**`single_example`：一行转调。**

[component_layout.rs:L185-L190](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L185-L190)

```rust
pub fn single_example(
    variant_name: impl Into<SharedString>,
    example: AnyElement,
) -> ComponentExample {
    ComponentExample::new(variant_name, example)
}
```

这就是 `ComponentExample::new` 的别名。参数用 `impl Into<SharedString>`，所以 `"Default"` 这样的 `&'static str` 字面量可以直接传入并被零拷贝包裹（`SharedString` 既可以是 `&'static str` 也可以是 `Arc<str>`，u2-l2 讲过）。

**`example_group` 与 `example_group_with_title`：同样是一行转调。**

[component_layout.rs:L196-L205](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L196-L205)

```rust
pub fn example_group(examples: Vec<ComponentExample>) -> ComponentExampleGroup {
    ComponentExampleGroup::new(examples)
}

pub fn example_group_with_title(
    title: impl Into<SharedString>,
    examples: Vec<ComponentExample>,
) -> ComponentExampleGroup {
    ComponentExampleGroup::with_title(title, examples)
}
```

四个函数里三个是纯语法糖，编译结果与直接调用构造器完全一致——选哪个纯粹是可读性考量。它们的价值在于把「预览怎么写」统一成一种风格：整个 Zed 代码库（ui、workspace、notifications、extensions_ui 等 crate）里，带标题分组 `example_group_with_title` 被调用约 134 次（50 个文件），无标题 `example_group` 约 59 次（40 个文件）——带标题版本是主流，因为组件通常有多个维度需要分节展示。

**`empty_example` 有自己的默认元素**，留到 4.2 单独讲。

**导入路径提醒**：ui 的 prelude 只重导出其中三个。

[prelude.rs:L10-L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/prelude.rs#L10-L11)

```rust
pub use component::{
    Component, ComponentScope, example_group, example_group_with_title, single_example,
```

`empty_example` 不在列表里，写 `use ui::prelude::*` 之后它仍然不可见，必须写 `component::empty_example(...)`（ui 已依赖 component crate，路径可直接使用）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认四个辅助函数的调用面与分布，验证「带标题分组是主流」的论断。
2. **操作步骤**：
   - 在仓库根目录执行 `rg -c 'example_group_with_title\(' crates/ | wc -l` 与 `rg -c 'example_group\(' crates/ | wc -l`，再对比两者总次数（可加 `--no-filename | wc -l`）。
   - 挑一个调用次数多的文件（如 `crates/ui/src/components/toggle.rs`、`crates/ui/src/components/button/icon_button.rs`），通读其中一个组件的 `preview()`，数一数它有几个分组、每组几张卡片。
3. **需要观察的现象**：分组标题几乎总是「维度名」（States、Colors、Disabled、Sizes…），而卡片名几乎总是「取值名」（Default、On、Off、Dashed…）。
4. **预期结果**：你会看到所有 preview 的骨架高度一致——`v_flex().gap_6().children(vec![example_group_with_title(...), ...]).into_any_element()`。差异只在分组怎么切、卡片放什么。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `single_example` 的第二个参数是具体的 `AnyElement`，而不是泛型 `E: IntoElement`？

**参考答案**：因为 `ComponentExample` 的 `element` 字段类型就是 `AnyElement`（u3-l1 讲过），而一个分组要装进 `Vec<ComponentExample>`，元素类型必须统一，类型擦除不可避免。把擦除点放在调用方（`.into_any_element()`），辅助函数签名保持最简，也避免了为每个元素类型单态化出一份泛型实例。

**练习 2**：`example_group`（无标题）什么时候比 `example_group_with_title` 更合适？

**参考答案**：当分组语义自明时——比如整个 preview 只有一组卡片、或分组标题会与组件页头信息重复。树内无标题版本约 59 处使用，多为单组小预览；多维度组件一律用带标题版本。

**练习 3**：`variant_name` 最终显示在卡片的什么位置、什么样式？

**参考答案**：卡片顶部，`text_size(rems(1.0))`、颜色为 `cx.theme().colors().text`（对应 component_layout.rs L35-L39 的渲染逻辑，u3-l1 已精读）。

### 4.2 empty_example：「合法的空渲染」的语义

#### 4.2.1 概念说明

很多组件存在「应当什么都不画」的状态：列表无数据、图标插槽暂时隐藏、条件不满足时整块区域退场。这类状态在组件目录里有个展示难题：

- 如果放一个真的空 `div`，观众分不清「组件正确地渲染了空」和「预览坏了 / 漏写了」；
- 如果干脆不展示，「空」这个重要行为就从组件文档里消失了。

`empty_example` 的解法是把「空」变成一个**被明确命名的变体**：卡片标题写上状态名（如 `"Empty"`），画布里放一行刻意调暗的说明文字，声明「这块留白是故意的，它表示该场景下应当不渲染任何内容」。它回答的问题不是「这个组件长什么样」，而是「这个组件在这个场景下**不**长什么样」——这是空态文档化（documenting the void）的思路。

#### 4.2.2 核心流程

`empty_example(name)` 构造的卡片仍然复用上一讲的标准卡片骨架（斜纹画布、圆角边框、100px 最小高度），只是画布里的孩子换成了固定占位元素：

```text
ComponentExample
├── variant_name: 调用方起的状态名（如 "Empty"）
└── 画布（斜纹 pattern_slash 背景）
    └── div（w_full + text_center + text_xs + opacity(0.4)）
        └── "This space is intentionally left blank. ..."
```

斜纹画布在这里发挥了关键作用（u3-l1 埋过伏笔）：文案浮在斜纹上，观众能**同时**看到「画布在」和「组件没画东西」两件事——画布边界证明预览本身是好的，画布内的暗色文案证明空是组件的行为。

#### 4.2.3 源码精读

[component_layout.rs:L192-L194](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component_layout.rs#L192-L194)

```rust
pub fn empty_example(variant_name: impl Into<SharedString>) -> ComponentExample {
    ComponentExample::new(variant_name, div().w_full().text_center().items_center().text_xs().opacity(0.4).child("This space is intentionally left blank. It indicates a case that should render nothing.").into_any_element())
}
```

只接收一个参数——被展示的「元素」就是那行固定文案，不需要调用方提供。样式链值得逐个看：

- `text_xs` + `opacity(0.4)`：说明文字必须退居次要，不能抢真组件的视觉焦点。`opacity` 作用在整个占位 div 上，是乘法语义（u3-l1 讲过 `Hsla::opacity`）。
- `w_full` + `text_center`：文案横跨画布居中，避免看起来像放错位置的真实内容。
- 文案内容本身就是语义说明："This space is intentionally left blank. It indicates a case that should render nothing."（这块留白是故意的，表示该场景应当不渲染任何内容。）

**两个诚实的观察**（截至当前 HEAD `28c0f4ae`，用 `rg 'empty_example' crates/` 可复核）：

1. `empty_example` 在整个 crates 树内**目前没有真实调用者**——只有定义，没有任何组件的 preview 用到它。它是一份「语义储备」：预定义了表达空渲染的标准方式，等待需要展示空态的组件采用。
2. 同样地，卡片的 `.description()` 构建器（component_layout.rs L81-L84）在树内也几乎没有消费者——搜索命中的 `callout.rs` 里的 `.description(...)` 是 `Callout` 组件自己的构建器，不是卡片的。

这不影响学习：新组件的 preview 完全可以（本讲的综合实践就会）用它们，机制是完备的；只是别在现成代码里找范例。

#### 4.2.4 代码实践（参数观察型）

1. **实践目标**：体感理解「次要信息要降低不透明度」的设计，以及占位文案在画布中的视觉层级。
2. **操作步骤**：在本地克隆中临时把 L193 的 `.opacity(0.4)` 改成 `.opacity(1.0)`（或不透明度 0.9），保存后运行组件预览（见综合实践的运行命令），找一个你能触发 `empty_example` 的入口观察（可以先跳过，等做完综合实践、自己的 preview 里有了 `empty_example` 后再回来做这一步，观察最直观）。
3. **需要观察的现象**：占位文案变得刺眼，开始和真组件争夺注意力，卡片之间的视觉权重被拉平。
4. **预期结果**：高不透明度下「这是一条元说明」的感觉消失，读起来更像卡片内容本身。**待本地验证**（视觉结论需实际运行确认）。
5. 改完记得还原源码（这是观察实验，不是要提交的修改）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `empty_example` 只需要一个参数，而 `single_example` 需要两个？

**参考答案**：`single_example` 展示的是调用方提供的任意元素，`empty_example` 展示的内容是固定的占位说明文字，唯一的变量是状态名（卡片标题），所以只留一个参数。

**练习 2**：假设一个组件在「无数据」状态下渲染结果为空，用 `empty_example("No Data")` 和用 `single_example("No Data", EmptyState::new().into_any_element())`（假设真有一个渲染为空的组件实例）有什么取舍？

**参考答案**：前者语义明确、自带解释文案，适合「就是什么都不画」的空态；后者展示的是组件真实的空输出，适合「空态下仍渲染占位 UI」的情况。关键判断：组件在该状态下**真的什么都不画**用 `empty_example`；**画了东西但看起来是空的**用 `single_example` 装真实实例。

**练习 3**：占位文案为什么是写死的英文，而不走本地化？

**参考答案**：component crate 不依赖任何本地化设施，组件预览是面向开发者的工具而非用户界面，英文写死最简单且与代码注释同一语言层。

### 4.3 Divider::preview 精读与「变体分组 + Example Usage」范式

#### 4.3.1 概念说明

好 preview 的标准是：观众在 30 秒内看全组件的所有维度。但真实组件往往维度过多。以 Divider 为例：

- 方向：Horizontal / Vertical（2 种）
- 线型：Solid / Dashed（2 种）
- 颜色：Border / BorderFaded / BorderVariant（3 种，见 [divider.rs:L19-L24](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L19-L24)）
- inset：是 / 否（2 种）

全组合有 \( 2 \times 2 \times 3 \times 2 = 24 \) 种卡片——组合爆炸，没人看得下去。`Divider::preview` 的策略是**策展而非穷举**：

1. 按最重要的维度（方向）切成两个分组；
2. 每组内挑代表性取值（Default、Border Color、Inset、Dashed）各展示一张；
3. 最后加一个「Example Usage」分组，展示组件在真实布局中的用法。

这套「**变体分组 + Example Usage**」结构就是本讲要提炼的范式。

#### 4.3.2 核心流程

Divider preview 的三分组结构（对应代码 [divider.rs:L172-L238](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L238)）：

```text
v_flex().gap_6()
├── "Horizontal Dividers"（水平方向组，4 张卡）
│   ├── Default        ← Divider::horizontal()
│   ├── Border Color   ← horizontal() + color(Border)
│   ├── Inset          ← horizontal() + inset()
│   └── Dashed         ← horizontal_dashed()
├── "Vertical Dividers"（垂直方向组，4 张卡）
│   ├── Default        ← div().h_16().child(Divider::vertical())
│   ├── Border Color   ← 同上 + color(Border)
│   ├── Inset          ← 同上 + inset()
│   └── Dashed         ← div().h_16().child(vertical_dashed())
└── "Example Usage"（真实用法组，1 张卡）
    └── Between Content ← 三个 Label 与两条 Divider 交替的 v_flex
```

两个值得注意的通用技巧藏在细节里：

- **给无尺寸组件提供布局上下文**：水平分隔线自己是 `h_px().w_full()`，宽度来自父容器，放进卡片就能撑满；但垂直分隔线是 `w_px().h_4()`（[divider.rs:L146-L150](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L146-L150)），孤立展示太短小，所以每张垂直卡的例子都包了一层 `div().h_16()` 给出高度上下文——真实使用中分隔线的尺寸同样来自布局，包装反而更接近真相。
- **Example Usage 里的组件用默认参数**：`Divider::horizontal()`、`Divider::horizontal_dashed()` 都不额外调颜色或 inset，文档优先展示最常见的调用方式。

#### 4.3.3 源码精读

先看整体骨架（含前两个 `impl Component` 方法，preview 是第三个）：

[divider.rs:L162-L175](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L162-L175)

```rust
impl Component for Divider {
    fn scope() -> ComponentScope {
        ComponentScope::Layout
    }

    fn description() -> &'static str {
        "Visual separator used to create divisions between groups of content \
        or sections in a layout."
    }

    fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement {
        v_flex()
            .gap_6()
            .children(vec![
```

`scope()` 决定它在预览侧栏落入哪个分组（`Layout` 的显示名是 `Layout & Structure`，u2-l5 讲过）；`description()` 进入搜索索引；然后就是标准骨架 `v_flex().gap_6().children(vec![...])`。

**第一组：水平分隔线。**

[divider.rs:L176-L189](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L176-L189)

```rust
example_group_with_title(
    "Horizontal Dividers",
    vec![
        single_example("Default", Divider::horizontal().into_any_element()),
        single_example(
            "Border Color",
            Divider::horizontal()
                .color(DividerColor::Border)
                .into_any_element(),
        ),
        single_example("Inset", Divider::horizontal().inset().into_any_element()),
        single_example("Dashed", Divider::horizontal_dashed().into_any_element()),
    ],
),
```

每组四个取值，卡片名简短、首字母大写、风格统一（"Default"、"Border Color"、"Inset"、"Dashed"）。注意每个例子的构造起点都是 `Divider::horizontal()` 这类具名构造函数——**preview 里的例子应该用组件的公开 API 构造**，这样预览同时充当 API 用法示例。

**第二组：垂直分隔线，注意 `div().h_16()` 包装。**

[divider.rs:L193-L217](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L193-L217)

```rust
single_example(
    "Default",
    div().h_16().child(Divider::vertical()).into_any_element(),
),
single_example(
    "Border Color",
    div()
        .h_16()
        .child(Divider::vertical().color(DividerColor::Border))
        .into_any_element(),
),
```

四个垂直例子全部包在 `div().h_16()` 里，把「分隔线需要布局上下文才有意义」这个事实直接写进了文档。

**第三组：Example Usage。**

[divider.rs:L220-L235](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L220-L235)

```rust
example_group_with_title(
    "Example Usage",
    vec![single_example(
        "Between Content",
        v_flex()
            .w_full()
            .gap_4()
            .px_4()
            .child(Label::new("Section One"))
            .child(Divider::horizontal())
            .child(Label::new("Section Two"))
            .child(Divider::horizontal_dashed())
            .child(Label::new("Section Three"))
            .into_any_element(),
    )],
),
```

这张卡不再展示孤立变体，而是「三个 Label 与两条 Divider 交替」的迷你场景——观众由此知道：实线适合同级分节，虚线适合弱化分节。前两组回答「有哪些变体」，这一组回答「什么时候用哪个」。

**范式交叉验证：Switch 的 preview 同构。**

[toggle.rs:L993-L1031](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/toggle.rs#L993-L1031) 里 Switch::preview 的前两个分组叫「States」和「Colors」：

```rust
example_group_with_title(
    "States",
    vec![
        single_example("Off", Switch::new("switch_off", ToggleState::Unselected)...),
        single_example("On", Switch::new("switch_on", ToggleState::Selected)...),
    ],
),
example_group_with_title(
    "Colors",
    vec![...],
),
```

规律完全一致：**分组名 = 维度名（States/Colors/Disabled/With Label），卡片名 = 取值名（Off/On/Accent/Custom）**。Divider 按方向分组、Switch 按状态分组，只是维度的选择不同。

最后提一句渲染细节的承接：分组标题在界面上会被 `to_uppercase()` 转成大写（component_layout.rs L127，u3-l1 已讲），所以代码里写 `"Horizontal Dividers"`，界面显示 `HORIZONTAL DIVIDERS`。

#### 4.3.4 代码实践（纸面设计型）

1. **实践目标**：在写代码之前，用「维度 → 分组」的方法论为组件设计 preview 结构。
2. **操作步骤**：任选一个你熟悉的组件（可以就用 Divider），列出它的全部维度及每个维度的取值；然后回答三个问题：(a) 哪个维度最重要、应该作为分组轴？(b) 每组内挑哪些代表性取值（而非全部取值）？(c) Example Usage 该展示什么场景？把答案画成 4.3.2 那样的树。
3. **需要观察的现象**：设计过程中你会被迫做取舍——这正是策展的意义。
4. **预期结果**：以 Divider 为例，你的设计应与 [divider.rs:L172-L238](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L172-L238) 的真实实现高度相似；如果选了其他组件，可以与它的真实 preview 对照，看 Zed 的作者怎么切维度。

#### 4.3.5 小练习与答案

**练习 1**：为什么垂直分隔线的例子要包一层 `div().h_16()`，而水平的不用？

**参考答案**：水平分隔线是 `h_px().w_full()`，宽度天然来自父容器（卡片画布），能自己撑满；垂直分隔线是 `w_px().h_4()`，固定高度只有 `h_4`，孤立放在画布中太短小且不反映真实使用（真实高度来自布局），所以用一个固定高度容器给出布局上下文。

**练习 2**：如果给 Divider 新增「粗细」维度（如 1px/2px），preview 应该怎么改？

**参考答案**：两种方案——若粗细是低频维度，在现有两组里各加一张 `single_example("2px", ...)` 卡片；若粗细是用户高频关心的维度，新增一个「Thickness」分组（或把「方向」分组降级、按粗细分组）。判断标准仍是「哪个维度值得占用分组标题的位置」。

**练习 3**：Divider::preview 为什么不把 24 种参数组合全部展示？

**参考答案**：组合爆炸会让页面失去可读性，预览是**策展**而非穷举——分组轴选一个维度、组内挑代表性取值、Example Usage 展示组合语义，三者配合已经能覆盖组件的全部信息点。

## 5. 综合实践

把四个辅助函数和三分组范式全部用一遍：为自己的练习组件写一个完整 preview，包含**两个 `example_group_with_title`（各含至少 2 个 `single_example`）和至少一处 `empty_example`**，并跑通组件预览验证。

下面的步骤以「在 ui crate 内新建一个练习组件」为载体（组件放进 ui crate 一定能被 inventory 收集到——链接期注册铁律，u2-l3）。

### 第 1 步：新建练习组件文件

在 `crates/ui/src/components/` 下新建 `practice_badge.rs`（**示例代码，仅用于练习，不建议合入上游**），内容如下：

```rust
// 示例代码：仅用于本讲练习
use component::empty_example; // 不在任何 prelude 里，必须显式引入（见 4.1.3）
use gpui::{AnyElement, App, IntoElement, RenderOnce, SharedString, Window, div};

use crate::prelude::*;

#[derive(IntoElement, RegisterComponent)]
pub struct PracticeBadge {
    label: SharedString,
    muted: bool,
}

impl PracticeBadge {
    pub fn new(label: impl Into<SharedString>) -> Self {
        Self {
            label: label.into(),
            muted: false,
        }
    }

    pub fn muted(mut self) -> Self {
        self.muted = true;
        self
    }
}

impl RenderOnce for PracticeBadge {
    fn render(self, _: &mut Window, cx: &mut App) -> impl IntoElement {
        let colors = cx.theme().colors();
        div()
            .px_2()
            .py_1()
            .rounded_sm()
            .border_1()
            .map(|this| {
                if self.muted {
                    this.border_color(colors.border.opacity(0.5))
                        .text_color(colors.text_muted)
                } else {
                    this.border_color(colors.border).text_color(colors.text)
                }
            })
            .text_sm()
            .child(self.label.clone())
    }
}

impl Component for PracticeBadge {
    fn scope() -> ComponentScope {
        ComponentScope::Layout
    }

    fn description() -> &'static str {
        "A small practice badge used to learn the preview helpers."
    }

    fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement {
        v_flex()
            .gap_6()
            .children(vec![
                example_group_with_title(
                    "Tones",
                    vec![
                        single_example(
                            "Default",
                            PracticeBadge::new("Draft").into_any_element(),
                        ),
                        single_example(
                            "Muted",
                            PracticeBadge::new("Archived").muted().into_any_element(),
                        ),
                    ],
                ),
                example_group_with_title(
                    "Edge Cases",
                    vec![
                        empty_example("Hidden"), // 注意：来自 component crate，不在 prelude 里
                        single_example(
                            "Long Label",
                            PracticeBadge::new("A Very Long Badge Label That May Wrap")
                                .into_any_element(),
                        ),
                    ],
                ),
            ])
            .into_any_element()
    }
}
```

说明三点：

- `empty_example` 不在任何 prelude 里（见 4.1.3），所以代码顶部必须显式写 `use component::empty_example;`，否则编译报错「找不到函数」。`use crate::prelude::*;` 只带进 `single_example` / `example_group_with_title` / `v_flex` / `Component` / `ComponentScope` / `RegisterComponent`。
- 练习组件放在 `crates/ui/src/components/` 目录下、并在 `components.rs` 里挂 `mod` 声明——`components` 模块本身在 [ui.rs:L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/ui.rs#L11) 声明为私有 `mod components;`，模块间互相引用走 `crate::components::...` 路径。
- 颜色只用了 u3-l1 已验证的 theme 令牌（`text`、`text_muted`、`border`），不引入新的颜色 API。

### 第 2 步：挂载模块声明

打开 `crates/ui/src/components.rs`，在 [components.rs:L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components.rs#L13) 的 `mod divider;` 旁边加一行：

```rust
mod practice_badge;
```

### 第 3 步：运行组件预览

```bash
cargo run -p component_preview --example component_preview
```

（启动方式与初始化链路见 u1-l2；`component::init()` 由 `workspace::init()` 保证在读取注册表之前执行。）

### 第 4 步：观察清单

在预览窗口左侧导航的 `Layout & Structure` 分组（`scope()` 返回 `ComponentScope::Layout` 决定，u2-l5）里找到 `PracticeBadge`，点开检查：

1. **分组标题**：`TONES`、`EDGE CASES` 是否大写显示（渲染期 `to_uppercase()`）。
2. **卡片**：每张是否有约 100px 高的斜纹画布、圆角边框，`Default` 与 `Muted` 的边框/文字颜色差异是否可见。
3. **empty_example**：`Hidden` 卡片是否显示那行 40% 不透明度的英文占位文案。
4. **长文本**：`Long Label` 卡片里超长标签如何换行/溢出。
5. **排序**：`PracticeBadge` 在 `Layout & Structure` 分组内的位置由 `sort_name()` 决定（默认取 `name()`，u2-l1）；如果不满意，给 `impl Component` 补一个 `sort_name()` 覆写再观察位置变化。

**预期结果**：以上五点全部可见、可交互。**待本地验证**——本讲义写作环境未运行 GUI，具体视觉效果（尤其斜纹密度、长标签换行行为）需你实际跑一遍确认。

### 第 5 步（可选进阶）

把 `scope()` 换成 `ComponentScope::Input`（显示名 `Forms & Input`），重新运行，观察组件从 `Layout & Structure` 分组「搬家」到 `Forms & Input` 分组——这一步把 u2-l5 的作用域分类与本讲的 preview 组织串了起来。

## 6. 本讲小结

- 四个辅助函数是 preview 的「词汇表」：`single_example` 造卡片、`example_group(_with_title)` 造分组，其中三个是对构造器的纯转调语法糖；`empty_example` 自带固定占位元素。
- `empty_example` 表达「合法的空渲染」：斜纹画布证明预览正常，暗色占位文案声明「该场景应渲染为空」；截至当前 HEAD 树内尚无真实调用者，属语义储备。
- 「变体分组 + Example Usage」范式：分组名 = 维度名，卡片名 = 取值名，策展而非穷举组合，最后一组展示真实用法场景。
- 两个通用技巧：孤立状态下无尺寸语义的组件（如垂直分隔线）要在 preview 里给布局上下文；Example Usage 中的组件用默认参数，优先展示最常见调用方式。
- 符号路径注意：`use ui::prelude::*`（或 ui 内部 `use crate::prelude::*`）拿不到 `empty_example`，必须写 `component::empty_example`。
- 综合实践完整走了一遍「新建组件 → 挂 mod → 写 preview → 跑预览验证」的闭环，为下一讲的真实组件接入做了热身。

## 7. 下一步学习建议

- **下一讲（u3-l3）「实战：为一个真实 UI 组件接入注册体系」**：以 `Divider`（ui crate 内）与 `NumberField<usize>`（settings_ui，跨 crate 注册）为例完整走一遍接入流程，本讲的 practice_badge 就是它的预演——建议保留你的练习文件，下一讲会再次用到这套三步法。
- 若对 `#[derive(RegisterComponent)]` 背后的展开还有模糊，回看 u2-l4：为什么派生宏生成的注册函数名带组件名前缀、泛型组件为何绕开派生手动注册。
- 想看更多高质量 preview 范例，推荐通读 `crates/ui/src/components/toggle.rs`（Switch / Toggle / Checkbox 三个组件的 preview，分组按维度命名的教科书式示例）和 `crates/ui/src/components/button/icon_button.rs`（5 个分组的大型 preview）。
