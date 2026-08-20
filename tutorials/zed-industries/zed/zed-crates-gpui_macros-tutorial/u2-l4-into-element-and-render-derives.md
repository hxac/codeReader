# u2-l4 最小的两个派生宏：IntoElement 与 Render

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `RenderOnce`（协议）、`IntoElement`（入场景）、`ViewElement`（桥）三者各自的职责，以及 `#[derive(IntoElement)]` 在其中补上的那一块拼图。
2. 逐行读懂 `derive_into_element.rs`（24 行）与 `derive_render.rs`（21 行）——这两个文件是全 crate 代码量最少的派生宏，也是理解「派生宏 = 固定样板生成器」的最佳标本。
3. 解释 `ast.generics.split_for_impl()` 为什么是泛型正确性的关键，并预测「不拆分会生成什么错误代码」。
4. 说出 `#[derive(Render)]` 生成了什么、为什么它被标 `#[doc(hidden)]`。
5. 在 `#[gpui::test]` 中亲手写一个 `RenderOnce` 组件，用派生宏把它挂进 `div().child(...)` 渲染树，并用 `cargo expand` 核对生成代码。

## 2. 前置知识

本讲是单元二里最轻松的一讲：前几讲的 `#[derive(Action)]` 有六个属性参数、多个编译期分支和 inventory 注册；本讲的两个宏**没有任何属性、没有任何分支**，是把「三行样板」自动写好的最小派生宏。你已经从 u1-l2 掌握了派生宏的四步套路：

```
parse_macro_input!(input as DeriveInput)   // 1. 解析
ast.generics.split_for_impl()              // 2. 拆泛型
quote! { impl ... for ... { ... } }        // 3. 生成代码
r#gen.into()                               // 4. 转回 TokenStream
```

还需要用通俗语言铺垫几个 gpui 侧的概念（下一节会对照源码）：

- **元素（Element）**：GPUI 真正布局和绘制的东西。每帧从视图状态构建元素树 → Taffy 布局 → 绘制 → 丢弃，下一帧重来。
- **视图（View）**：GPUI 中「能把自己变成元素树」的东西。有两种：有状态视图（`Entity<T>` 且 `T: Render`，靠实体系统响应 `cx.notify()`）和无状态视图（任何 `RenderOnce` 类型，构造完即用完，没有身份）。
- **组件（component）**：官方推荐的高层用法——一个普通结构体，字段就是参数，实现 `RenderOnce` 后即可像内置元素一样 `.child(...)` 地使用，保住流式构造器的链式写法。
- **`#[track_caller]`**：一个普通 Rust 属性，让函数拿到「调用处的源码位置」而不是宏内部的位置，报错和调试信息因此指向你写的那一行。

一个容易混淆的点先澄清：`#[derive(IntoElement)]` 里的 `IntoElement`（宏命名空间）与 trait `IntoElement`（类型命名空间）同名但不同物，这是 u1-l3 讲过的基本功。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `crates/gpui_macros/src/derive_into_element.rs` | `#[derive(IntoElement)]` 的全部实现，仅 24 行 | 主角之一：为 `RenderOnce` 类型生成 `ViewElement` 包装 |
| `crates/gpui_macros/src/derive_render.rs` | `#[derive(Render)]` 的全部实现，仅 21 行 | 主角之二：生成返回 `gpui::Empty` 的默认渲染 |
| `crates/gpui_macros/src/gpui_macros.rs` | 两个宏的入口声明与文档 | 看 `#[doc(hidden)]` 加在谁身上 |
| `crates/gpui_macros/tests/render_test.rs` | `#[derive(Render)]` 的编译冒烟测试 | 理解「编译通过即测试通过」 |
| `crates/gpui/src/element.rs` | `IntoElement` / `Render` / `RenderOnce` trait 与 `Empty` 元素的定义 | 协议侧：宏生成代码要满足的契约 |
| `crates/gpui/src/view.rs` | `View` trait、`ViewElement` 及三条 blanket impl | 桥侧：把视图接入布局/绘制管线 |
| `crates/gpui/src/elements/div.rs` | `Div` 元素；手写 `impl IntoElement for Div` | 对照组：内置元素为什么不用派生宏 |
| `crates/gpui/examples/view_example/view_example_main.rs` | `CursorReadout` 组件：`#[derive(IntoElement)]` + `RenderOnce` 的官方示例 | 实践任务的模板 |
| `crates/gpui/examples/view_example/example_tests.rs` | 示例的 `#[gpui::test]` 测试 | 实践任务落点 |

> 提示：前四个文件在 `gpui_macros` crate，后面的在 `gpui` crate。宏生成的代码必须能在**用户 crate** 里解析，所以生成的路径都写全（`gpui::IntoElements` 之类的 `gpui::` 前缀），这是 u1-l2 讲过的规则。

## 4. 核心概念与源码讲解

### 4.1 三层关系：RenderOnce 是协议，IntoElement 是入场景，ViewElement 是桥

#### 4.1.1 概念说明

写一个 GPUI 组件时，你要做两件事，它们**默认互不相干**：

1. 实现 `RenderOnce`——约定「给我 `self`、窗口和应用上下文，我还你一棵元素树」。这是**协议**：说明组件如何渲染自己。
2. 让组件能被塞进 `.child(...)`——`child` 的参数是 `impl IntoElement`。这是**入场景**：说明组件能出现在哪里。

只做第 1 件事时，组件还进不了元素树，因为 `child` 要的不是 `RenderOnce` 而是 `IntoElement`。手写第 2 件事每次都是同样的三行样板，于是 `#[derive(IntoElement)]` 把这三行自动生成。

生成物里的关键是 `ViewElement<Self>`——一个**桥**：它实现 `Element`，内部持有你的组件，在布局阶段调用组件的 `render`，把「视图世界」接进「元素世界」。

#### 4.1.2 核心流程

从「我写了一个组件」到「它被画出来」的完整链路：

```
div().child(MyComponent { ... })          // child 接收 impl IntoElement
  └─ MyComponent::into_element(self)      // 派生宏生成的方法
       └─ ViewElement::new(self)          // 包装成桥；类型是 IntoElement::Element
            └─ (布局阶段) Element::request_layout for ViewElement
                 └─ self.view.render(window, cx)   // 调用你手写的 RenderOnce
                      └─ 返回真正的元素树，继续正常布局绘制
```

而 `ViewElement<V>` 有个泛型约束 `V: View`。你的组件满足它靠的是一条 blanket impl：任何 `RenderOnce` 类型自动是 `View`（无身份视图）。这是 2026 年 7 月 `Unify Render and RenderOnce into View (#58087)` 重构的成果——此前组件包装类型叫 `gpui::Component<Self>`，重构后与有状态视图统一走 `ViewElement`。

#### 4.1.3 源码精读

先看协议侧。`IntoElement` trait 本体只有两个必需项：

[crates/gpui/src/element.rs:L144-L157](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L144-L157)——定义「可转换为元素」的能力：关联类型 `Element` 指明转成哪个元素类型，`into_element` 执行转换，`into_any_element` 提供擦除类型的默认实现（调 `into_element().into_any()`）。

`RenderOnce` 则是组件协议：

[crates/gpui/src/element.rs:L174-L184](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L174-L184)——文档明确写了「你可以在任何实现此 trait 的类型上派生 `IntoElement`」，并强调 `render` 拿的是 `self` 所有权（对比 `Render::render` 拿 `&mut self`，见 L161-L166）。所有权语义正是「无状态、用完即弃」组件的签名。

再看桥侧。`View` 是统一抽象：

[crates/gpui/src/view.rs:L182-L195](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L182-L195)——`View` 只要求两件事：报告自己的 `entity_id`（可以没有）和按值 `render`。

[crates/gpui/src/view.rs:L197-L207](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L197-L207)——**本讲最重要的 blanket impl**：任何 `T: RenderOnce` 自动实现 `View`，`entity_id` 恒为 `None`（无身份）。你的组件之所以能被 `ViewElement` 接受，全靠这两行。

[crates/gpui/src/view.rs:L209-L221](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L209-L221)——对照组：有状态视图 `Entity<T>`（`T: Render`）也实现 `View`，但带真实 `entity_id`，渲染时先进实体更新。同一个桥，两种过法。

`ViewElement` 本体：

[crates/gpui/src/view.rs:L237-L260](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L237-L260)——`ViewElement<V: View>` 持有视图、可选的实体 id 和缓存样式；`new` 用 `#[track_caller]` 记录调用位置（`debug_assertions` 下存进 `source` 字段，L244-L245），供调试器回答「这个元素是哪行代码放进树的」。

[crates/gpui/src/view.rs:L277-L283](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L277-L283)——`ViewElement` 自己也实现 `IntoElement`（`type Element = Self`），所以桥可以嵌套使用。

最后看桥在布局期如何回调你的组件：

[crates/gpui/src/view.rs:L314-L337](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L314-L337)——`Element for ViewElement` 的 `request_layout`：有 `entity_id` 走有状态路径（创建响应式边界），否则 `self.view.take().unwrap().render(window, cx)`——**这里就是你的 `RenderOnce::render` 被调用的地方**。

官方真实示例（不是示例代码，是仓库自带的）：

[crates/gpui/examples/view_example/view_example_main.rs:L38-L59](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/view_example/view_example_main.rs#L38-L59)——`CursorReadout` 组件：`#[derive(IntoElement)]` 加结构体、`impl RenderOnce` 写渲染逻辑，字段 `editor: Entity<Editor>` 就是组件参数。

[crates/gpui/examples/view_example/view_example_main.rs:L96-L104](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/view_example/view_example_main.rs#L96-L104)——使用点：`.child(CursorReadout::new(owned))` 与 `.child(Input::editor(...))` 并排出现，一个手写组件和一个内置元素以完全相同的方式入树。

#### 4.1.4 代码实践

**实践目标**：不用写任何代码，只靠删注释验证「`RenderOnce` 是协议、`IntoElement` 是入场景」的分工。

**操作步骤**：

1. 打开 [view_example_main.rs:L40](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/view_example/view_example_main.rs#L40)，把 `#[derive(IntoElement)]` 这一行注释掉。
2. 运行 `cargo check -p gpui --example view_example`。

**需要观察的现象**：编译错误不是「缺少 RenderOnce」，而是出现在两处 `.child(CursorReadout::new(...))` 上（L102、L119），错误信息形如 `the trait bound CursorReadout: IntoElement is not satisfied`。

**预期结果**：`impl gpui::RenderOnce for CursorReadout`（L51-L59）仍然完整存在——协议没坏，但组件进不了 `child`。恢复注释后编译通过。这证明派生宏只负责「入场景」这一层。（本实践只改注释、不改逻辑，验证后记得还原。）

#### 4.1.5 小练习与答案

**练习 1**：`ViewElement<CursorReadout>` 能编译通过，靠的是哪条 impl？为什么 `Entity<Editor>` 也能被 `.child()` 接受？

**答案**：靠 [view.rs:L197-L207](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L197-L207) 的 `impl<T: RenderOnce> View for T`——`ViewElement<V: View>` 的约束由此满足。`Entity<Editor>` 走的是另一条路：[view.rs:L95-L101](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L95-L101) 直接为 `impl<V: Render> IntoElement for Entity<V>` 手写了实现，`Element` 同样是 `ViewElement<Entity<V>>`。

**练习 2**：`RenderOnce::render` 拿 `self`，`Render::render` 拿 `&mut self`，这个差异为什么和「有无状态」对应？

**答案**：`RenderOnce` 组件是无状态的一锤子买卖：字段就是参数，渲染时消费 `self`（按值读一遍字段就够），用完即弃。`Render` 的宿主是长活的实体，`cx.notify()` 之后会被反复重渲染，所以只能借 `&mut self`，且能借到 `Context<Self>` 与实体系统交互（发事件、订阅等）。

### 4.2 `#[derive(IntoElement)]` 源码精读：三行样板的自动化

#### 4.2.1 概念说明

`derive_into_element.rs` 是「最小派生宏模板」的标准 specimen：无属性、无分支、无错误处理。它生成的 impl 手写出来是这样（以 4.1 的 `CursorReadout` 为例）：

```rust
// 示例代码：以下是 #[derive(IntoElement)] 为 CursorReadout 生成的等价代码
impl gpui::IntoElement for CursorReadout {
    type Element = gpui::ViewElement<Self>;

    #[track_caller]
    fn into_element(self) -> Self::Element {
        gpui::ViewElement::new(self)
    }
}
```

值得注意的两个细节：

- 宏**没有**给生成的 impl 添加 `Self: RenderOnce` 之类的显式约束。约束检查发生在类型检查阶段：如果用户派生了 `IntoElement` 却没实现 `RenderOnce`，错误会由 `ViewElement<Self>` 的 `V: View` 约束触发（`the trait bound ... : View is not satisfied`）。宏只要原样转发泛型即可。
- `#[track_caller]` 让 `ViewElement::new`（自身也标了 `#[track_caller]`，[view.rs:L250](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L250)）拿到你写 `.child(...)` 的那一行，而不是宏内部的机器位置。

#### 4.2.2 核心流程

```
TokenStream（结构体定义）
  → parse_macro_input! 解析为 DeriveInput
  → 取 ast.ident 作为类型名
  → ast.generics.split_for_impl() 拆出泛型三元组
  → quote! 填模板
  → .into() 转回 TokenStream，追加在原结构体后面
```

对照 `Div` 的手写版本可以看清「谁需要派生宏」：

- `Div` **本身就是元素**，它的 `IntoElement` 实现是恒等转换（[div.rs:L1989-L1995](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1989-L1995)：`type Element = Self`）。
- 组件**不是元素**，必须借 `ViewElement` 这座桥，所以样板是「包装」而非「恒等」——这正是派生宏的价值所在。

#### 4.2.3 源码精读

宏入口声明（在 crate 唯一库入口里）：

[crates/gpui_macros/src/gpui_macros.rs:L32-L37](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L32-L37)——`#[proc_macro_derive(IntoElement)]` 声明派生宏入口（注意没有 `attributes(...)` 注册任何 helper 属性），转发到实现模块。doc 注释两行就是全部文档：「为任何 `RenderOnce` 类型生成 `IntoElement` impl，包成 `ViewElement` 以便作 child 使用」。

实现全文（24 行，一屏读完）：

[crates/gpui_macros/src/derive_into_element.rs:L5-L24](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L5-L24)——完整的派生函数：解析、取类型名、拆泛型、`quote!` 生成 `impl #impl_generics gpui::IntoElement for #type_name #type_generics #where_clause`，关联类型指向 `gpui::ViewElement<Self>`，`into_element` 调 `gpui::ViewElement::new(self)`。

其中 [derive_into_element.rs:L8](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L8) 的 `split_for_impl()` 是 4.3 节的主角；[derive_into_element.rs:L10-L21](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L10-L21) 的 `quote!` 模板里三个 `#` 插值点分别填入 impl 泛型、类型泛型和 where 子句。

运行端再确认一次链路（与 4.1.2 呼应）：

[crates/gpui/src/element.rs:L193-L199](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L193-L199)——`ParentElement::child` 的签名 `child(self, child: impl IntoElement)`：这就是组件最终要满足的入场景；内部 `child.into_element().into_any()` 完成转换。

#### 4.2.4 代码实践

见第 5 节综合实践（本讲的主实践按规格要求设计在 `#[gpui::test]` 中完成，涵盖本模块与 4.3、4.4 的验证点）。这里先做一个零成本的编译冒烟：

1. **实践目标**：确认 `#[derive(IntoElement)]` 生成的代码在真实 crate（`gpui`）中可用。
2. **操作步骤**：在 zed 仓库根目录执行 `cargo check -p gpui --example view_example`。
3. **需要观察的现象**：编译通过，无警告；`CursorReadout` 上方的 `#[derive(IntoElement)]` 生效。
4. **预期结果**：`Cargo.toml` 里 `gpui` 并不直接依赖 `gpui_macros` 的所有宏——`gpui` 通过 [gpui.rs:L109-L111](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L109-L111) 的 `pub use gpui_macros::{AppContext, IntoElement, Render, ...}` 再导出，所以 example 里 `use gpui::IntoElement` 拿到的正是本讲的宏。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉生成代码里的 `#[track_caller]`（[derive_into_element.rs:L16](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L16)），功能上会坏什么？

**答案**：编译与运行都不受影响；丢失的是调试信息——`ViewElement` 在 `debug_assertions` 下记录的 `source` 位置（[view.rs:L244-L245](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L244-L245)）会指向派生宏生成的代码位置（无意义），而不是你写 `.child(...)` 的那一行。

**练习 2**：为什么宏生成的是 `gpui::ViewElement<Self>` 而不是直接 `type Element = Self`（像 `Div` 那样）？

**答案**：`IntoElement::Element` 有关联类型约束 `type Element: Element`（[element.rs:L148](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L148)）。`Div` 自己实现 `Element` 所以能恒等；你的组件只实现了 `RenderOnce`，不是元素，必须由实现 `Element` 的 `ViewElement` 代为参与布局绘制（在 `request_layout` 里回调 `view.render`，见 4.1.3）。

### 4.3 `split_for_impl`：泛型正确性的关键一行

#### 4.3.1 概念说明

`Generics::split_for_impl()` 是 syn 提供的派生宏惯用法，返回三元组，分别用于生成 impl 的三个语法位置：

| 组成部分 | 内容（以 `struct Panel<'a, T: Clone + 'static>` 为例） | 用在哪儿 |
| --- | --- | --- |
| `impl_generics` | `<'a, T: Clone + 'static>`（**带**约束） | `impl #impl_generics Trait for ...` |
| `type_generics` | `<'a, T>`（**不带**约束） | 类型位置 `... for Panel<'a, T>` |
| `where_clause` | `where ...`（若有） | impl 头之后、花括号之前 |

为什么必须拆？Rust 语法里，impl 处的泛型参数**要**带约束（`impl<T: Clone>`），而类型名后面跟的泛型参数**不能**带约束（`Panel<T: Clone>` 是语法错误）。同一份「泛型信息」在两个位置长得不一样，所以 syn 把一份声明拆成两种形态。

#### 4.3.2 核心流程

三种输入下 `split_for_impl` 的效果：

```
struct Badge;                                  // 零泛型
  → impl gpui::IntoElement for Badge

struct Panel<'a, T: Clone + 'static> { ... }   // 生命周期 + 类型参数
  → impl<'a, T: Clone + 'static> gpui::IntoElement for Panel<'a, T>

struct Row<T> where T: Styled { ... }          // where 子句
  → impl<T> gpui::IntoElement for Row<T> where T: Styled
```

如果不拆、直接把 `ast.generics.to_token_stream()` 填进两处，第二种输入会生成 `impl<T: Clone> ... for Panel<T: Clone>`——类型位置带约束，直接编译错误。**本 crate 的两个宏都正确处理了泛型**，对照 `derive_action.rs`（u2-l1）你会发现它也调了同一个方法——这是所有为泛型结构体工作的派生宏的必经之路。

#### 4.3.3 源码精读

[crates/gpui_macros/src/derive_into_element.rs:L8](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L8)——`let (impl_generics, type_generics, where_clause) = ast.generics.split_for_impl();` 一行解构出三元组。

[crates/gpui_macros/src/derive_into_element.rs:L11-L13](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L11-L13)——三个插值点各就各位：`impl #impl_generics gpui::IntoElement for #type_name #type_generics #where_clause`。

[crates/gpui_macros/src/derive_render.rs:L8](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L8)——`derive_render` 的同一行，证明这是两个宏共享的固定骨架。

注意边界：`split_for_impl` 处理的是**结构体自己声明的**泛型与约束。它不知道「`ViewElement<Self>` 需要额外约束」这类语义——若用户的泛型参数不满足下游要求（例如缺 `'static`，而 `RenderOnce: 'static`），错误同样推迟到类型检查阶段报出。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到泛型组件经过派生宏后生成正确的 impl 头。
2. **操作步骤**：把综合实践（第 5 节）里的组件改成带泛型的版本：

   ```rust
   // 示例代码：泛型组件，验证 split_for_impl
   #[derive(IntoElement)]
   struct Repeated<T: 'static + Clone> {
       label: T,
       times: usize,
   }

   impl<T: 'static + Clone> RenderOnce for Repeated<T> {
       fn render(self, _window: &mut Window, _cx: &mut App) -> impl IntoElement {
           gpui::div().children((0..self.times).map(|i| gpui::div().child(format!("{}", i))))
       }
   }
   ```

   然后运行 `cargo expand -p gpui --example view_example`（若实践落在 example 中），在输出中搜索 `impl.*IntoElement for Repeated`。
3. **需要观察的现象**：展开结果里 impl 头是 `impl<T: 'static + Clone> gpui::IntoElement for Repeated<T>`——约束只在 impl 处，类型位置是裸的 `<T>`。
4. **预期结果**：与 4.3.2 表格第二种形态一致。展开输出较大，可配合 `| grep -A4 'IntoElement for Repeated'` 使用（待本地验证：具体输出形态取决于 cargo expand 版本）。

#### 4.3.5 小练习与答案

**练习 1**：`struct Row<T> where T: Styled` 派生后，`where T: Styled` 出现在生成代码的哪个位置？为什么不能出现在 `for Row<T>` 之前？

**答案**：在 impl 头之后、`{` 之前（`#where_clause` 插值点，[derive_into_element.rs:L12](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L12)）。`for Row<T>` 处只能出现裸泛型参数列表，where 子句在 Rust 语法中只能位于 impl 头尾部（或函数签名尾部）。

**练习 2**：本 crate 里还有哪些派生宏必须做同样的拆分？

**答案**：全部为结构体生成 impl 的派生宏都要：`derive_action.rs`、`derive_app_context.rs`、`derive_visual_context.rs` 以及本讲的两个。用 `grep -n split_for_impl src/*.rs` 可确认（待本地验证：各文件行号）。

### 4.4 `#[derive(Render)]`：空渲染与 `#[doc(hidden)]`

#### 4.4.1 概念说明

`Render` 是**有状态视图**的渲染协议（`render(&mut self, window, cx) -> impl IntoElement`）。`#[derive(Render)]` 为任意类型生成一个**什么都不画**的默认实现——返回 `gpui::Empty` 元素。

它的用途是快速造桩：当你需要一个满足 `T: Render` 约束的类型（比如放进 `Entity<T>` 当测试视图），但不需要它画任何东西时，派生一下就行，省去手写空 impl。注意它与 `impl Render for Empty`（[element.rs:L168-L172](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L168-L172)）是两个方向：前者是「给你的类型一个空渲染」，后者是「给 Empty 类型一个渲染（返回它自己）」。

`#[doc(hidden)]` 的含义：宏入口被 `gpui` 再导出（[gpui.rs:L109-L111](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/gpui.rs#L109-L111)），但不出现在 rustdoc 文档里——它不是官方鼓励使用的 API。git 考据：这个属性是 2024-01-09 的提交 `42cbd103fb`（"Even more docs"）与 `style_helpers` 一起被隐藏的；用 `rg 'derive\(Render\)' crates/` 全库检索，除 `tests/render_test.rs` 自身外没有任何调用方。它是「存在但边缘」的宏。

#### 4.4.2 核心流程

```
#[derive(Render)] struct _Element;
  → impl gpui::Render for _Element {
        fn render(&mut self, _window, _cx) -> impl gpui::Element {
            gpui::Empty      // 什么都不画
        }
    }
```

`Empty` 自己实现了完整的 `Element`（布局返回默认样式、绘制为空），所以它可以安全地充当任何「需要返回点什么」的位置的占位符。

#### 4.4.3 源码精读

实现全文（21 行，全 crate 最小）：

[crates/gpui_macros/src/derive_render.rs:L5-L21](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L5-L21)——与 `derive_into_element` 完全同构的骨架（解析 → 拆泛型 → quote → into），唯一区别是生成的 trait 与方法体：`render` 返回 `impl gpui::Element`，落点是 `gpui::Empty`。

[crates/gpui_macros/src/derive_render.rs:L14-L16](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L14-L16)——方法体：参数名带 `_` 前缀（未使用），直接返回 `gpui::Empty`。

入口声明与隐藏标记：

[crates/gpui_macros/src/gpui_macros.rs:L39-L43](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/gpui_macros.rs#L39-L43)——`#[proc_macro_derive(Render)]` 之下的 `#[doc(hidden)]`（L40）把这个入口从文档中隐藏；对比 L32-L34 的 `IntoElement` 入口没有这个标记且有说明文档。

协议侧的 `Render` 与 `Empty`：

[crates/gpui/src/element.rs:L161-L166](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L161-L166)——`Render` trait 定义：`'static + Sized`，`render` 借 `&mut self` 并拿到 `Context<Self>`（对比 `RenderOnce` 拿所有权与 `&mut App`）。

[crates/gpui/src/element.rs:L727-L736](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L727-L736)——`Empty` 是空元素：单元结构体，`IntoElement` 恒等转换。其后 L738 起的 `impl Element for Empty` 用默认样式参与布局、不绘制任何内容。

冒烟测试：

[crates/gpui_macros/tests/render_test.rs:L1-L7](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/tests/render_test.rs#L1-L7)——整个测试只有一个函数体内的 `#[derive(Render)] struct _Element;`：**编译通过即测试通过**。没有断言、没有运行时行为，验证的只是「宏生成的代码合法且满足 trait 签名」。

#### 4.4.4 代码实践

1. **实践目标**：运行 `#[derive(Render)]` 的冒烟测试，并用 `cargo expand` 对照生成代码。
2. **操作步骤**：在 zed 仓库根目录执行：

   ```bash
   cargo test -p gpui_macros --test render_test
   cargo expand -p gpui_macros --test render_test
   ```

3. **需要观察的现象**：第一条命令输出 `test test_derive_render ... ok`；第二条命令在展开结果中能看到 `impl gpui::Render for _Element` 及方法体里的 `gpui::Empty`。
4. **预期结果**：展开代码与 [derive_render.rs:L10-L18](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs#L10-L18) 的 `quote!` 模板逐行对应（`_Element` 零泛型，故 impl 头无泛型段）。这延续了 u1-l2 的「模板对照」练习法。（待本地验证：cargo expand 的具体输出格式。）

#### 4.4.5 小练习与答案

**练习 1**：`#[derive(Render)]` 和手写 `impl Render for T { fn render(...) -> impl IntoElement { gpui::Empty } }` 效果一样吗？什么时候你会选手写？

**答案**：派生版本只能返回 `Empty`（什么都不画）。想要任何非空渲染、或需要用 `_window`/`_cx`（如 `window.use_state`、`cx.notify`）时，必须手写——仓库里所有真实视图都是手写的，派生版本只适合测试桩。

**练习 2**：`gpui::Empty`（element.rs）和 `EmptyView`（[view.rs:L501](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs#L501)）是一回事吗？

**答案**：不是。`Empty` 是**元素**（参与布局但零内容，本讲派生宏的返回值）；`EmptyView` 是**视图**（一个实现了 `Render` 的现成空视图类型，用于「需要一个视图但不画东西」的场合，如 [element.rs:L168-L172](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L168-L172) 让它渲染出 `Empty` 元素）。名字相近，层次不同。

## 5. 综合实践

把本讲全部内容串起来：亲手写一个组件，走完「协议 → 派生 → 入树 → 渲染 → 展开验证」五步。

**任务**：在 `crates/gpui/examples/view_example/` 的测试模块中，新增一个 `RenderOnce` 组件 `LabeledCount`，用 `#[derive(IntoElement)]` 让它进入 `div()` 渲染树，在 `#[gpui::test]` 中真实绘制一帧，最后用 `cargo expand` 核对宏生成物。

**操作步骤**：

1. 打开 [example_tests.rs](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/view_example/example_tests.rs)，在 `mod tests` 内（`use` 块之后）追加以下**示例代码**：

   ```rust
   use gpui::{App, IntoElement, RenderOnce, SharedString, div, px};

   /// 一个最小的无状态组件：字段即参数。
   #[derive(IntoElement)]
   struct LabeledCount {
       label: SharedString,
       count: usize,
   }

   impl RenderOnce for LabeledCount {
       fn render(self, _window: &mut Window, _cx: &mut App) -> impl IntoElement {
           div()
               .flex()
               .gap(px(8.))
               .child(self.label)
               .child(SharedString::from(format!("×{}", self.count)))
       }
   }

   #[gpui::test]
   fn labeled_count_composes_into_div(cx: &mut TestAppContext) {
       // draw 会真正执行 layout + prepaint + paint（见 test_context.rs 的 VisualTestContext::draw）
       cx.draw(gpui::point(px(0.), px(0.)), gpui::size(px(200.), px(40.)), |_, _| {
           div()
               .flex()
               .p(px(4.))
               .child(LabeledCount { label: "items".into(), count: 3 })
               .into_any()
       });
   }
   ```

   `cx.draw(origin, space, closure)` 的用法取自仓库真实测试 [list.rs:L1770-L1772](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/list.rs#L1770-L1772)。

2. 运行测试（该命令写在此文件头部的文档注释里，[example_tests.rs:L1-L5](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/view_example/example_tests.rs#L1-L5)）：

   ```bash
   cargo test -p gpui --example view_example --features test-support
   ```

3. 观察编译期证据：

   ```bash
   cargo expand -p gpui --example view_example | grep -B2 -A8 'IntoElement for LabeledCount'
   ```

4. 对照验证（呼应 4.2.4 的删注释实验）：注释掉 `#[derive(IntoElement)]` 那一行再跑第 2 步命令，读编译错误；恢复后重跑。

**需要观察的现象**：

- 第 2 步：`labeled_count_composes_into_div ... ok`——组件进入了真实渲染管线且未 panic。
- 第 3 步：展开结果里有 `impl gpui::IntoElement for LabeledCount`、`type Element = gpui::ViewElement<LabeledCount>` 与 `gpui::ViewElement::new(self)`，与 [derive_into_element.rs:L10-L21](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs#L10-L21) 的模板逐行对应。
- 第 4 步：错误出现在 `.child(LabeledCount { ... })` 处，提示 `LabeledCount: IntoElement` 不满足——证明缺的只是「入场景」。

**预期结果**：编译与测试均通过；你能不假思索地回答「宏生成了什么、缺了它会怎样、`ViewElement` 在哪个阶段回调 `RenderOnce::render`（答：布局阶段的 `request_layout`，见 4.1.3）」。（待本地验证：`cargo expand` 输出的具体排版。）

## 6. 本讲小结

- `RenderOnce` 是协议（组件如何渲染自己），`IntoElement` 是入场景（能进 `.child(...)`），`ViewElement` 是桥（实现 `Element`，在布局阶段回调 `view.render`）；`#[derive(IntoElement)]` 只补「入场景」那一块。
- [derive_into_element.rs](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_into_element.rs)（24 行）与 [derive_render.rs](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui_macros/src/derive_render.rs)（21 行）是「最小派生宏模板」：解析 → `split_for_impl` → `quote!` → `into()`，无属性、无分支。
- `split_for_impl()` 把泛型声明拆成「impl 处带约束 / 类型处不带约束 / where 子句」三种形态，是所有支持泛型结构体的派生宏的必经一行。
- 组件能被 `ViewElement<V: View>` 接受，靠 `impl<T: RenderOnce> View for T`（无身份 blanket impl）；有状态视图 `Entity<T>` 走另一条带 `entity_id` 的路——两条路都汇入 `ViewElement`。
- `#[derive(Render)]` 生成返回 `gpui::Empty` 的空渲染，适合测试桩；它被 `#[doc(hidden)]` 隐藏，全仓库除自身测试外无调用方。
- 内置元素（如 `Div`）自己就是 `Element`，手写恒等 `IntoElement`；组件不是元素，必须借桥——这就是「谁需要派生宏」的分界线。

## 7. 下一步学习建议

- **下一讲 u3-l1（`#[derive(AppContext)]`）**：本讲的两个宏「无属性、无分支」，下一讲开始进入带 helper 属性（`#[app]`）与 `compile_error!` 错误处理的派生宏，复杂度上一个台阶。
- **延伸阅读 1**：[view.rs](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/view.rs) 的 `Element for ViewElement` 完整实现（L298 起），看有状态路径如何创建响应式边界、`cached_style` 如何跳过子树渲染——理解桥的全部工作。
- **延伸阅读 2**：[element.rs:L28-L32](https://github.com/zed-industries-zed/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/element.rs#L28-L32) 的模块文档，官方对「什么时候自定义 Element、什么时候用组件」的建议。
- **延伸阅读 3**：跑一遍 `cargo run -p gpui --example view_example`，在窗口里找到 `CursorReadout` 渲染出的 cursor 读数，把源码里的 `#[derive(IntoElement)]`、`RenderOnce`、`.child(...)` 与屏幕像素对应起来。
