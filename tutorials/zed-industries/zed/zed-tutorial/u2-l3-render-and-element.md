# Element 与 Render:声明式 UI 渲染

## 1. 本讲目标

上一讲(u2-l2)我们解决了「状态存哪、怎么改、怎么通知」:可变状态住在 `Entity<T>` 里,通过 `cx.update` 租借修改,通过 `cx.notify()` 发出变化信号。这一讲回答下一个自然的问题:**屏幕上的像素是从哪里来的?**

学完本讲,你应该能够:

1. 写出一个实现 `Render` trait 的自定义视图,并把它的 `Entity` 作为窗口根视图显示出来。
2. 区分 `Render` 与 `RenderOnce` 两种渲染方式,知道什么时候该用哪一个。
3. 使用 `div()`、`.child()`、`.children()` 与 Tailwind 风格样式方法组装元素树。
4. 完整描述从 `cx.notify()` 到屏幕刷新的重绘链路,理解 GPUI「按需渲染 + 子树缓存」的设计。

## 2. 前置知识

本讲默认你已学完 u2-l1(GPUI 总览)和 u2-l2(Entity 模型)。在此基础上补充几个新概念:

- **声明式 UI vs 命令式 UI**:命令式 UI 要求你手动操作界面元素(`label.setText(...)`、`button.setColor(...)`);声明式 UI 只要求你描述「界面在当前状态下应该长什么样」,框架负责把描述变成像素。GPUI 是声明式的:你的 `render` 方法返回一棵元素树,这就是「描述」。
- **元素(Element)**:一棵短命的对象树,描述「这里有一个容器,它的背景色是 X,里面有一个文本子节点」。它由 `render` 每帧重建,绘制完就被丢弃。
- **视图(View)**:`Entity<T>` 中 `T` 实现了 `Render` 的实体俗称视图。视图是长寿的(状态),元素是短命的(描述)。
- **渲染三阶段**:元素树先 `request_layout`(向 taffy 布局引擎登记,算出每个节点的位置和大小),再 `prepaint`(提交边界、生成命中测试盒等),最后 `paint`(真正向场景(scene)输出绘制指令)。本讲只需要知道这个顺序,细节在 u5-l4 编辑器渲染管线中再深入。
- **flexbox**:浏览器同款的弹性盒子布局模型,GPUI 用 [taffy](https://github.com/DioxusLabs/taffy) 实现。`.flex()`、`.gap_3()` 这些方法就是在设置 flexbox 属性,细节留到 u2-l4。

一句话承上启下:**Entity 管「是什么状态」,Element 管「长什么样」,重绘循环管「什么时候把新状态变成新像素」。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/gpui/src/element.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs) | 定义 `Element`、`IntoElement`、`Render`、`RenderOnce`、`ParentElement` 五个核心 trait,以及模块级文档对元素生命周期的权威说明 |
| [crates/gpui/src/view.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/view.rs) | `View` trait 统一 `Render` 与 `RenderOnce`;`ViewElement` 是把视图挂进布局/绘制管线的适配器,也是子树缓存的实现处 |
| [crates/gpui/src/elements/div.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/elements/div.rs) | `div()` 函数、`Div` 容器与 `Interactivity`(交互与样式载体),GPUI 中使用最频繁的元素 |
| [crates/gpui/src/window.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs) | 帧循环:`WindowInvalidator` 脏标记、`draw`/`draw_roots` 的布局-预绘-绘制三阶段 |
| [crates/gpui/src/app.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs) 与 [crates/gpui/src/app/context.rs](https://github.com/zed-industries-zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs) | `cx.notify()` 的入口实现 |
| [crates/gpui/examples/hello_world.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/hello_world.rs) | 官方最小示例,本讲实践的样板 |
| [crates/settings_ui/src/components/input_field.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/settings_ui/src/components/input_field.rs) | 真实代码库中的 `RenderOnce` 组件案例 |

## 4. 核心概念与源码讲解

### 4.1 Render trait:状态如何变成元素树

#### 4.1.1 概念说明

`Render` 是「这个实体自己会画自己」的凭证。任何一个 `Entity<T>`,只要 `T` 实现了 `Render`,就可以作为窗口的根视图或作为子视图嵌入其他视图——这就是上一讲提到的「视图」的严格定义。

理解 GPUI 渲染模型的关键一句话写在 element.rs 的模块文档里:**元素树由 `Render::render()` 从当前状态递归构建,经 taffy 布局、按各自 `Element::paint()` 绘制上屏,并且在下一帧开始前整棵元素树连同其注册的回调一起被丢弃,然后过程重来。** 这是「立即模式 + 保留模式」的混合:状态长期保留在 Entity 中(保留模式的好处),元素树每帧重建(立即模式的好处——render 逻辑永远是「从状态到 UI」的纯函数式推导,不会残留过期 UI)。

#### 4.1.2 核心流程

一个视图从状态到像素的完整生命周期:

```text
Entity<T: Render>(长寿,持有状态)
        │  每帧由 ViewElement 适配器调用
        ▼
T::render(&mut self, window, cx) ──► 元素树(短命,只是描述)
        │
        ▼
request_layout ──► taffy 计算,得到每个节点的 LayoutId
        │
        ▼
prepaint ──► 布局结果落地为 Bounds,生成 Hitbox 等
        │
        ▼
paint ──► 向 scene 输出绘制指令,GPU 上屏
        │
        ▼
本帧结束,元素树被丢弃;下次重绘时从 Entity 重新 render
```

注意 `render` 拿的是 `&mut self` 而非 `self`:实体长期存活,每帧只是被「借用」一下来产出描述。

#### 4.1.3 源码精读

**Render trait 的定义**只有三行,它是整个体系最窄的入口:

> [crates/gpui/src/element.rs:L161-L166](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs#L161-L166)
> 这段代码定义了 `Render` trait:任何 `'static + Sized` 的类型实现 `render(&mut self, &mut Window, &mut Context<Self>) -> impl IntoElement`,就成为一个可渲染的视图。返回值只需要「能转换成元素」,这让你可以按需返回 `div()`、`Empty` 或任何自定义元素。

**元素每帧被丢弃的官方说明**:

> [crates/gpui/src/element.rs:L8-L14](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs#L8-L14)
> 模块文档明确写道:元素由窗口根视图的 `Render::render()` 递归构建,由 Taffy 布局,按各自 `Element::paint()` 绘制;下一帧开始前,整棵元素树及其回调都被 drop,过程重复。这是理解「为什么 render 里可以放心写任何临时计算」的依据。

**一个最小的 Render 实现**(官方示例):

> [crates/gpui/examples/hello_world.rs:L9-L28](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/hello_world.rs#L9-L28)
> `HelloWorld` 结构体只有一个 `text: SharedString` 字段;它的 `render` 方法返回一个 `div()`:设置弹性布局、背景色、边框、文字颜色,然后 `.child(format!("Hello, {}!", self.text))` 把问候语作为子元素挂进去。这就是「从状态到 UI」的完整推导——状态变了,下次 render 自然产出不同的树。

**视图如何成为窗口内容**:

> [crates/gpui/examples/hello_world.rs:L92-L109](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/hello_world.rs#L92-L109)
> `run_example` 里,`application().run(...)` 启动应用;`cx.open_window(...)` 的第二个参数是一个闭包,它调用 `cx.new(|_| HelloWorld { text: "World".into() })` 创建实体并返回——**open_window 的闭包返回的就是窗口根视图的 Entity**。之后每一帧,GPUI 都会调用这个实体的 `render` 来构建整棵元素树。

顺带一提,`Entity<T: Render>` 天然实现了 `IntoElement`,所以视图也可以直接作为别的视图的 child 嵌入(见 [crates/gpui/src/view.rs:L95-L101](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/view.rs#L95-L101),它把 `Entity<V>` 转成 `ViewElement<Entity<V>>`)。

#### 4.1.4 代码实践:跑起来并扰动它

1. **实践目标**:亲手验证「render 是每帧从状态重新推导的」。
2. **操作步骤**:
   1. 在仓库根目录执行 `cargo run -p gpui --example hello_world`(这是官方推荐的示例运行方式,见 [crates/gpui/examples/README.md:L3-L7](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/README.md#L3-L7)),看到一个 500×500 的窗口。
   2. 把 `hello_world.rs` 中 `HelloWorld` 结构体的 `text` 字段初始值从 `"World".into()` 改成你的名字,重新运行,观察问候语变化——你只改了状态,没碰任何 UI 代码。
   3. 在 `render` 方法的第一行加一句 `eprintln!("render called");`,重新运行,然后:静止不动几秒 → 用鼠标在窗口上悬停、点击 → 拖拽窗口边缘改变大小,分别观察终端输出。
3. **需要观察的现象**:修改状态即修改界面;终端的 `render called` 打印在窗口静止时会停下来,鼠标交互和缩放窗口时会成批出现。
4. **预期结果**:你会直观感受到 GPUI 是**按需渲染**的——没有变化就没有帧;而悬停、缩放这类事件会触发重绘,每次重绘都重新执行一遍 `render`。(悬停为什么能触发重绘,答案在 4.4 节。)
5. 本实践需要本地能编译 gpui(依赖安装见 u1-l2),若暂无环境,可先完成 4.1.5 的源码阅读练习,标注「待本地验证」。

#### 4.1.5 小练习与答案

1. **练习**:为什么 `Render::render` 接收 `&mut self`,而不是拿走 `self`?
   **答案**:实现 `Render` 的实体长期存活在 EntityMap 中,每帧被反复渲染,只能以借用方式访问;拿走所有权就等于每帧克隆一份状态,既昂贵也违背「Entity 是状态唯一所有者」的模型。
2. **练习**:`hello_world.rs` 里 `open_window` 的闭包返回值是什么类型?它和窗口是什么关系?
   **答案**:返回 `cx.new(...)` 创建的 `Entity<HelloWorld>`。它是这个窗口的根视图,GPUI 每帧从它开始调用 `render` 构建元素树。
3. **练习**:元素树每帧都被丢弃,那「某个 div 当前是否被悬停」这类状态存在哪里?
   **答案**:不存在元素里。悬停等跨帧状态由 `Interactivity` 配合窗口的命中测试(hitbox)机制在框架层跟踪,业务状态则存在 Entity 中。元素树永远只是「当前这一帧的纯描述」。

### 4.2 RenderOnce 与 derive(IntoElement):无状态组件

#### 4.2.1 概念说明

很多 UI 片段不需要自己的长期状态,只是「把一组参数组装成一段元素树」的配方:一张卡片、一个列表项、一个标签。为它们建 Entity 既浪费又啰嗦。GPUI 为此提供 `RenderOnce`:**为组装元素而构造的普通值,渲染时消费自身**。

`Render` 与 `RenderOnce` 的关键区别:

| 维度 | `Render` | `RenderOnce` |
| --- | --- | --- |
| 谁实现它 | 有状态的业务实体 | 纯数据组件(常配 `#[derive(IntoElement)]`) |
| render 签名 | `&mut self`,`&mut Context<Self>` | `self`,`&mut App` |
| 生命周期 | 长期存活于 EntityMap | 构建后通常立刻被渲染并丢弃 |
| 身份(entity_id) | 有(`Some(EntityId)`) | 无(`None`) |
| 能否 `cx.notify()` 驱动重绘 | 能 | 不能,须由外层实体驱动 |
| 典型用途 | 编辑器、面板、页面 | 按钮、卡片、标签等展示组件 |

两者在内部被一个更新的抽象统一:`View` trait。它把「参与 GPUI 响应式图的渲染体」抽象成两类:带实体身份的(`Entity<T: Render>`)和不带的(`T: RenderOnce`)。

#### 4.2.2 核心流程

```text
你的代码: Card::new("标题", "正文")          ← 普通 Rust 值
                │ #[derive(IntoElement)] 生成 into_element()
                ▼
        ViewElement<Card>(entity_id = None)   ← 无身份,走"无状态路径"
                │ 布局时调用 View::render → RenderOnce::render(self, ...)
                ▼
        div().child(标题).child(正文)          ← 组装出的真实元素树

对照: Entity<CardList>(entity_id = Some(id))  ← 有身份,走"有状态路径"
        每帧检查缓存:该 id 自上次渲染后是否被 notify 过?
        干净 → 复用上一帧的整棵子树
        脏   → 重新调用 Render::render
```

#### 4.2.3 源码精读

**RenderOnce 的定义与定位**:

> [crates/gpui/src/element.rs:L174-L184](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs#L174-L184)
> 文档注释说得很清楚:`RenderOnce` 用来「从纯数据构造可复用的组件」,把组件看作「某种元素模式的配方」,调用它不会打断元素 API 的链式构建风格。注意 `render(self, ...)` 拿所有权——组件值被就地消费,字段无需克隆。

**View trait 如何统一两种渲染**:

> [crates/gpui/src/view.rs:L171-L195](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/view.rs#L171-L195)
> `View` 是 `Render` 与 `RenderOnce` 背后的统一模型。`entity_id()` 返回 `Some` 时,这个 id 成为视图身份:它获得独立的元素 id 空间,并且对该实体 `cx.notify()` 只重渲染这一个子树;返回 `None` 则表现为无状态组件。文档还提醒:很少需要手写 `View`,只有「既要父级传参、又要实体身份」的组件才手动实现。

**两条 blanket impl 把两个世界接进 View**:

> [crates/gpui/src/view.rs:L197-L221](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/view.rs#L197-L221)
> 第一段:任何 `T: RenderOnce` 自动是 `View`,`entity_id()` 恒为 `None`,`render` 直接转发给 `RenderOnce::render`。第二段:`Entity<T: Render>` 自动是 `View`,`entity_id()` 返回自己的实体 id,`render` 则通过 `self.update(cx, ...)` 租借实体再调用 `Render::render`——这正是上一讲「访问实体必须出示上下文」的体现。

**derive(IntoElement) 生成了什么**:

> [crates/gpui_macros/src/derive_into_element.rs:L5-L21](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_macros/src/derive_into_element.rs#L5-L21)
> 这个过程宏为标注的类型生成 `impl IntoElement`,内容是 `type Element = ViewElement<Self>` 并把 `self` 包成 `ViewElement::new(self)`。所以每个 `#[derive(IntoElement)]` 组件本质上就是一个无身份的 `ViewElement`,能直接作为 `.child()` 的参数。

**真实代码库中的例子**:

> [crates/settings_ui/src/components/input_field.rs:L13-L27](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/settings_ui/src/components/input_field.rs#L13-L27) 与 [crates/settings_ui/src/components/input_field.rs:L130-L131](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/settings_ui/src/components/input_field.rs#L130-L131)
> 设置界面的输入框组件 `SettingsInputField` 就是标准写法:`#[derive(IntoElement)]` 标注纯数据结构体,构造函数 `new(id)` 加一串 `with_xxx` builder 方法收集参数,最后在 `impl RenderOnce for SettingsInputField` 的 `render(self, ...)` 里一次性消费这些参数组装出带 `Editor` 的元素树。注意它内部持有的 `id: ElementId` 字段——无状态组件自己没有身份,但可以用字段为内部的**有状态**子元素提供跨帧 id。

#### 4.2.4 代码实践:数一数身边的 RenderOnce

1. **实践目标**:建立「组件库几乎全是 RenderOnce」的直觉。
2. **操作步骤**:
   1. 在仓库根目录执行 `grep -rl "impl RenderOnce for" crates/ui/src | head -20`,再执行 `grep -rl "impl Render for" crates/editor/src | head -5`。
   2. 打开上面找到的任意一个 `RenderOnce` 组件(建议选个头小的),对照 4.2.3 的四步结构阅读:数据字段 → builder 方法 → `render(self, ...)` → 返回的元素树。
   3. 思考:如果给这个组件加一个「需要随时间变化的计数器」,改造方向是什么?
3. **需要观察的现象**:`ui` 这类纯组件 crate 里 `RenderOnce` 占绝对多数;`editor` 这类业务 crate 里 `Render` 占多数。
4. **预期结果**:你会得出结论——**组件(展示)用 RenderOnce,业务(状态)用 Render**,需要两者兼备时才手写 `View`(在组件里放一个 `Entity` 字段并返回其 id)。第 3 步的答案:把计数器放进一个 `Entity<Counter>`,组件持有该 Entity 作为字段并在 render 中嵌入它,由 Counter 自己 notify 驱动刷新。
5. grep 命令不依赖编译环境,可直接完成。

#### 4.2.5 小练习与答案

1. **练习**:什么时候「必须」用 `Render` 而不能用 `RenderOnce`?
   **答案**:当这片 UI 需要(1) 跨帧的可变业务状态,(2) 被 `cx.observe`/`cx.subscribe` 观察,(3) 主动调用 `cx.notify()` 触发重绘,三者任一时。`RenderOnce` 没有实体身份,这三件事都无从谈起。
2. **练习**:为什么 `RenderOnce` 组件的 builder 方法(如 `with_placeholder`)都返回 `Self`?
   **答案**:为了保持元素 API 的链式调用风格(`Card::new(..).highlighted().compact()`),这也正是 element.rs 文档说 RenderOnce 「不打断 fluent builder pattern」的含义;消费发生在最后的 `render(self)`。
3. **练习**:`#[derive(IntoElement)]` 和手写 `impl IntoElement` 效果一样吗?
   **答案**:一样。宏只是替你写了 `type Element = ViewElement<Self>; fn into_element(self) -> Self::Element { ViewElement::new(self) }` 这段样板(见 4.2.3 第三个引用)。

### 4.3 div() 与元素树组装:child、children 与样式

#### 4.3.1 概念说明

`div()` 是 GPUI 里使用频率最高的函数,返回一个 `Div` 元素——文档称它为「构建复杂 UI 的一体化元素」。它同时实现三个 trait,分别对应三件事:

- **`Styled`**:提供 Tailwind 风格的样式方法链(`bg`、`flex`、`gap_3`、`p_4`……),写入样式精修(StyleRefinement)。
- **`ParentElement`**:提供 `.child()` 与 `.children()`,把别的元素挂进来,形成树。
- **`InteractiveElement`**(下一讲 u2-l5 的主角):提供 `.on_click()`、`.id()`、`.hoverable()` 等交互能力。

组装发生在**构建期**:调用 `div().flex().gap_3().child(...)` 时只是在收集样式与子元素列表,没有任何布局或绘制发生;真正的处理在布局阶段的 `request_layout` 里递归展开。另外注意 `Div` 内部持有一个 `Interactivity` 结构,样式与交互数据其实都存在它身上——`.id()` 给它一个 `ElementId` 是启用有状态交互(如 `on_click`)的前提。

#### 4.3.2 核心流程

```text
构建期(你的 render 方法里):
div()
  .flex().flex_col().gap_3()      ← Styled:写入 interactivity.base_style
  .bg(rgb(0x505050))
  .child(文本)                     ← ParentElement:push 进 children: SmallVec
  .child(div().size_8()...)        ← 子 div 又有自己的 children → 树

布局期(框架调用):
Div::request_layout
  ├─ 逐个调用 child.request_layout(...)   ← 递归向下,子先登记
  ├─ window.request_layout(style, child_layout_ids)  ← 把自己与孩子们的
  │                                        LayoutId 关系交给 taffy
  └─ 返回自己的 LayoutId
(随后 prepaint / paint 同样递归,完成整棵树的三阶段)
```

#### 4.3.3 源码精读

**div() 函数与 Div 结构体**:

> [crates/gpui/src/elements/div.rs:L1687-L1706](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/elements/div.rs#L1687-L1706)
> `div()` 构造一个空 `Div`:内部是一个新建的 `Interactivity` 和空的 `children: SmallVec`。`Div` 的全部字段就是样式/交互载体加子元素列表——它真的只是一个「描述容器」,不持有任何业务状态。

**Div 实现三大 trait**:

> [crates/gpui/src/elements/div.rs:L1766-L1783](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/elements/div.rs#L1766-L1783)
> 这三段分别展示:`impl Styled for Div` 把 `style()` 指向 `interactivity.base_style`(所有样式方法最终写入处);`impl ParentElement for Div` 实现 `extend`,把子元素包成 `StackSafe` 追加进 `children`。`child`/`children` 的默认实现就在 `ParentElement` trait 里,见 [crates/gpui/src/element.rs:L188-L209](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs#L188-L209):`.child(x)` 就是 `extend(once(x.into_element().into_any()))`——任何 `IntoElement` 都能当孩子,统一擦除成 `AnyElement`。

**递归布局**:

> [crates/gpui/src/elements/div.rs:L1819-L1853](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/elements/div.rs#L1819-L1853)
> `Div::request_layout` 先遍历 `self.children` 逐个调用 `child.request_layout(window, cx)` 收集 `child_layout_ids`,再调用 `window.request_layout(style, child_layout_ids, ...)` 把「我的样式 + 我的孩子们」登记给 taffy。整棵元素树的布局就是这样自顶向下递归、自底向上汇总的。

**样式方法从哪来**:

> [crates/gpui/src/styled.rs:L22-L29](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/styled.rs#L22-L29)
> `Styled` trait 只要求实现一个 `style(&mut self) -> &mut StyleRefinement`;文件顶部通过宏批量生成 Tailwind 风格方法(如 `gpui_macros::padding_style_methods!()` 生成 `p_1`…`p_32`、`px_2` 等一族,`w`/`size`/`gap` 前缀同理,见 [crates/gpui_macros/src/styles.rs:L840-L870](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui_macros/src/styles.rs#L840-L870))。所以你调 `.p_4()` 时,本质是往 `style().padding` 四个方向写入长度值。

**Interactivity:样式与交互的合租屋**:

> [crates/gpui/src/elements/div.rs:L2025-L2045](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/elements/div.rs#L2025-L2045)
> `Interactivity` 的字段表揭示了 Div 的全部秘密:`element_id`(有状态交互的前提)、`active`/`hovered`(跨帧交互状态)、`base_style`(基础样式)以及后面一长串 `hover_style`、`click_listeners` 等。本讲只用它的样式面,交互面留给 u2-l5。

最后补充两个实用组合子(细节在 u2-l4):`FluentBuilder` 提供的 `.when(cond, |this| ...)` 与 `.when_some(opt, |this, v| ...)` 定义在 [crates/gpui/src/util.rs:L11-L52](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/util.rs#L11-L52),让条件样式不必拆成 if/else。

#### 4.3.4 代码实践:把链式调用画成一棵树

1. **实践目标**:把「方法链」在脑中固化为「元素树」。
2. **操作步骤**:
   1. 重新阅读 [crates/gpui/examples/hello_world.rs:L13-L89](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/hello_world.rs#L13-L89),数一数一共出现了几个 `div()`。
   2. 在纸上画出这棵树:根 div(flex 纵排)→ 文本子节点 + 第二个 div(flex 横排)→ 六个彩色小方块。
   3. 把六个 `.child(div().size_8()...)` 中任意三个改写成一次 `.children([...])` 调用(签名见 [crates/gpui/src/element.rs:L202-L208](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs#L202-L208)),重新运行 `cargo run -p gpui --example hello_world`。
3. **需要观察的现象**:界面与改写前完全一致。
4. **预期结果**:`.child(a).child(b).child(c)` 与 `.children([a, b, c])` 产出同一棵树,后者在循环生成子元素时更简洁(div.rs 内部就有这样的用法,见 [crates/gpui/src/elements/div.rs:L5169](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/elements/div.rs#L5169) 附近用 `.children(widths.into_iter()...)` 生成一排子元素)。若暂无编译环境,画出树形图并标注「待本地验证」。
5. **加分项**:把根 div 的 `.flex_col()` 换成 `.flex_row()`(或删掉 `.flex_col()`),预测六个方块会怎么排,再运行验证你的预测。

#### 4.3.5 小练习与答案

1. **练习**:`.child()` 能接收哪些类型的参数?
   **答案**:一切实现了 `IntoElement` 的类型——`Div`、其他自定义元素、`Entity<V: Render>`(视图)、`#[derive(IntoElement)]` 的 `RenderOnce` 组件、`Empty`、`AnyElement` 等。`child` 的默认实现会把它转换并擦除为 `AnyElement` 存入 children。
2. **练习**:为什么 `Div` 的 children 用 `SmallVec<[StackSafe<AnyElement>; 2]>` 而不是 `Vec`?
   **答案**:绝大多数容器的子元素不超过两个,`SmallVec` 把前两个内联在栈上,避免一次堆分配;`AnyElement` 完成类型擦除,让同一容器能混装不同类型的子元素。这是「热点数据结构按常见情况优化」的典型取舍。
3. **练习**:连续调用两次 `.bg(red()).bg(blue())` 会怎样?
   **答案**:后写覆盖先写。样式方法是对 `StyleRefinement` 对应字段的一次赋值(`Some(...)` 覆盖),同名属性只保留最后一次设置——这也是为什么条件样式可以用 `.when()` 组合而不会互相叠加出意外结果。

### 4.4 重绘触发:从 cx.notify() 到屏幕刷新

#### 4.4.1 概念说明

u2-l2 讲过 `cx.notify()` 「只做去重入队,观察者派发发生在最外层 update 结束后」。本讲补全它的另一半身份:**notify 同时是重绘的发令枪**。但它不直接画任何东西——它只做三件小事:把实体记入窗口的脏集合、把窗口标记为脏、唤醒平台的帧请求源。真正的绘制要等平台下一次「请求帧」的回调到来。

这套机制的精妙之处在于两级节流:

1. **窗口级**:`invalidator.is_dirty()` 为假时,帧回调直接跳过绘制(这就是 4.1.4 实验里「静止时 render 不再打印」的原因——GPUI 不做无意义的连续渲染)。
2. **子树级**:`ViewElement` 会缓存每个有身份视图上一帧渲染出的子树,只要该实体没被 notify 过、且边界/文本样式没变,整棵子树直接复用,连 `render` 都不会调用。这使得「窗口里 100 个视图,只有 1 个变了」时,99 个视图的 render 不执行。

#### 4.4.2 核心流程

从 `cx.notify()` 到像素更新的完整链路(六步):

```text
1. cx.notify()                      ← Context<T> 转发给 App::notify(实体id)
2. App::notify                      ← 找出正在显示该实体的窗口的 invalidator
   └─ invalidator.invalidate_view   ← 记入 dirty_views、置 dirty=true、
                                      唤醒平台帧源(waker)
3. 平台帧回调 on_request_frame      ← 到点醒来
   └─ if invalidator.is_dirty()     ← 窗口级节流:不脏不画
        └─ window.draw()
4. draw
   ├─ invalidate_entities           ← 取出 dirty_views,把每个脏视图
   │    └─ mark_view_dirty             连同其祖先视图一起标进 window.dirty_views
   └─ draw_roots                    ← 从根元素开始三阶段:
        ├─ request_layout
        ├─ prepaint  ──► ViewElement 在这里逐个检查:
        │      缓存命中(实体不脏 && 边界没变) → 复用上一帧子树
        │      缓存未命中 → 重新调用 Render::render,重建子树
        └─ paint
5. present                          ← 场景交给 GPU 上屏
6. dirty_views.clear()              ← 收尾,等待下一次 notify
```

另一个入口是 `window.refresh()`:它设置 `refreshing = true` 并强制置脏,会让所有 `ViewElement` 缓存失效、全量重绘(比如主题切换时用)。

#### 4.4.3 源码精读

**第 1 步:notify 的入口**:

> [crates/gpui/src/app/context.rs:L228-L231](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app/context.rs#L228-L231)
> `Context<T>::notify` 只有一行:把自己的实体 id 交给 `App::notify`。注意此刻既没有渲染也没有事件派发——它只是「挂号」。

**第 2 步:路由到正在显示该实体的窗口**:

> [crates/gpui/src/app.rs:L2614-L2649](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/app.rs#L2614-L2649)
> `App::notify` 用 `window_invalidators_by_entity` 查出与该实体关联的窗口失效器,并用 `tracked_entities` 过滤,只通知「当前真的在渲染这个实体」的窗口;对每个活跃窗口调用 `invalidator.invalidate_view(entity_id, cx)`。若一个窗口都没有(实体没被显示),则退化为普通的观察者通知效果——这正是 u2-l2 讲过的那条路径,两条路在这里汇合又分工。

**第 2 步(续):脏标记与唤醒**:

> [crates/gpui/src/window.rs:L160-L179](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L160-L179)
> `invalidate_view` 把实体插入 `dirty_views`,把 `dirty` 置真;若当前不在绘制阶段(`DrawPhase::None`),还通过 `platform_waker` 唤醒平台的帧请求源——这一步保证「空闲窗口不空转,一旦有变化又能立刻醒来」。

**第 3 步:帧回调里的窗口级节流**:

> [crates/gpui/src/window.rs:L1614-L1627](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L1614-L1627)
> 平台每次请求帧时,回调先判断 `invalidator.is_dirty() || force_render`:不脏就只在必要时呈现(present)上一帧,脏了才执行 `window.draw(cx)` + `window.present()`。这是「按需渲染」的第一道闸门。

**第 4 步:draw 的开场与散场**:

> [crates/gpui/src/window.rs:L2836-L2852](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L2836-L2852)
> `draw` 一进来就调用 `invalidate_entities`(把失效器里的脏视图迁入 window 的 `dirty_views` 集合)并立刻 `set_dirty(false)`——脏状态消费掉,之后到来的 notify 会重新置脏、再排队一帧。
> [crates/gpui/src/window.rs:L2884-L2884](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L2884)
> 绘制完成后 `self.dirty_views.clear()`,一轮重绘闭环。
> [crates/gpui/src/window.rs:L2989-L2995](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L2989-L2995) 与 [crates/gpui/src/window.rs:L1922-L1934](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L1922-L1934)
> `invalidate_entities` 取出脏视图后逐个 `mark_view_dirty`;`mark_view_dirty` 沿着上一帧的调度树把该视图的**祖先链**也全部标脏——因为祖先的缓存子树里包含了这个视图,不标脏祖先就会复用出过期画面。

**第 4 步(续):三阶段从根展开**:

> [crates/gpui/src/window.rs:L3088-L3132](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L3088-L3132)
> `draw_roots` 把根视图转成元素后依次执行 `request_layout`(并让根像网页根元素一样撑满视口)、`prepaint_as_root`,最后在注释 `// Now actually paint the elements.` 之后调用 `root_element.paint(self, cx)`。整棵树的三阶段在此完成。

**子树级缓存:ViewElement 的命中判断**:

> [crates/gpui/src/view.rs:L380-L401](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/view.rs#L380-L401)
> `ViewElement::prepaint` 的有状态路径里,这段条件判断是性能核心:若上一帧缓存的 `cache_key`(bounds + content_mask + text_style)与当前一致、实体不在 `window.dirty_views` 中、且窗口没有整体 refresh,就直接 `reuse_prepaint` 复用上一帧的子树,**完全跳过 render**;否则才重新 `render` + 布局 + 预绘,并记下本轮访问过的实体集合。4.1.4 实验中「悬停会触发重绘」的原因也在这里:交互样式变化会以失效/刷新的形式让相关视图走重新渲染路径。

**强制全量重绘的入口**:

> [crates/gpui/src/window.rs:L1999-L2004](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/window.rs#L1999-L2004)
> `Window::refresh` 只置两个标志:`refreshing = true` + 置脏。下帧 prepaint 时上面那段缓存判断里的 `!window.refreshing` 条件必然失败,于是所有视图子树重建。主题切换、GPU 设备恢复等场景靠它兜底。

#### 4.4.4 代码实践:让 notify 驱动一次真实的界面变化

1. **实践目标**:亲手走通「改状态 → notify → 重绘」的完整闭环(也就是本讲所有知识的串联)。
2. **操作步骤**(示例代码,基于 hello_world.rs 改造):
   1. 复制 `crates/gpui/examples/hello_world.rs` 为 `crates/gpui/examples/blink.rs`(examples 目录下的每个 `.rs` 都会被 cargo 自动识别为示例,`gpui_platform` 已在 gpui 的 dev-dependencies 中,见 [crates/gpui/Cargo.toml:L147-L152](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/Cargo.toml#L147-L152))。
   2. 给 `HelloWorld` 加一个 `count: u32` 字段。
   3. 在 `render` 里把问候语子节点改成 `format!("Hello, {}! (render {})", self.text, self.count)`,并在 `render` 开头 `self.count += 1;`(借 render 的 `&mut self` 修改状态;随后紧跟一句 `cx.notify();`)。
   4. 运行 `cargo run -p gpui --example blink`,然后用鼠标悬停到窗口边框上产生交互、或拖拽窗口大小。
3. **需要观察的现象**:每次真正的重绘,计数才 +1;窗口静止时数字不动——render 没有被执行。
4. **预期结果**:屏幕上的数字与「render 实际执行次数」一致,证明:(1) render 只在需要时执行;(2) 在 render 中修改状态并 notify 是合法但「每一帧都排队下一帧」的写法(交互一旦发生就会连续刷新)。更规范的定时刷新写法要用 `cx.spawn` + 定时器,那是 u2-l6 的内容。
5. 若暂无编译环境,请通读上面六步链路并在 4.4.2 的流程图上标出每一步对应的源码行号,标注「待本地验证」。

#### 4.4.5 小练习与答案

1. **练习**:调用 `cx.notify()` 的瞬间屏幕更新了吗?
   **答案**:没有。notify 只做脏标记 + 唤醒帧源;真正的 draw 要等平台帧回调到来且 `invalidator.is_dirty()` 为真。这保证了同一批多次 notify 会被合并到一帧里。
2. **练习**:为什么 `mark_view_dirty` 要把祖先视图一起标脏,而不是只标这个视图?
   **答案**:`ViewElement` 的缓存单位是「视图的整棵子树」,祖先的缓存区间内嵌着后代的绘制结果。只标后代,祖先的缓存判断(不检查后代是否脏)就会命中,复用过期画面;沿祖先链全标才能让包含它的最小缓存单位失效。
3. **练习**:一个 `RenderOnce` 组件能自己触发重绘吗?
   **答案**:不能。它没有实体身份,`cx.notify()` 无从发起;它的刷新由外层持有它的实体驱动——「谁持有状态,谁驱动重绘」。
4. **练习**:`cx.notify()` 与 `window.refresh()` 的区别?
   **答案**:notify 精确失效:只有被通知实体(及其祖先链)的子树重建,其余视图复用缓存;refresh 设 `refreshing = true`,下一帧所有视图缓存全部失效,整窗全量重绘,代价高一个量级,用于主题切换等全局变化。

## 5. 综合实践

**任务:实现 `Card` 组件并在窗口中渲染三张卡片**——把本讲的三个模块(Render/RenderOnce 对比、元素树组装、重绘触发)串成一次完整的动手。

1. **实践目标**:写出一个 `#[derive(IntoElement)]` + `impl RenderOnce` 的无状态组件,由一个 `impl Render` 的有状态实体承载,并验证「改外层状态 → notify → 组件内容刷新」。

2. **操作步骤**:

   1. 新建文件 `crates/gpui/examples/cards.rs`,写入以下内容(**示例代码**,由本讲义提供,API 与当前仓库的 hello_world.rs 对齐):

   ```rust
   #![cfg_attr(target_family = "wasm", no_main)]

   use gpui::{
       div, prelude::*, px, rgb, size, App, Bounds, Context, Render, RenderOnce, SharedString,
       Window, WindowBounds, WindowOptions,
   };
   use gpui_platform::application;

   /// 无状态卡片组件:只负责把「标题 + 正文」组装成一段元素树。
   /// 每帧被构造、渲染、丢弃,自身不保存任何可变状态。
   #[derive(IntoElement)]
   struct Card {
       title: SharedString,
       body: SharedString,
   }

   impl Card {
       fn new(title: impl Into<SharedString>, body: impl Into<SharedString>) -> Self {
           Self {
               title: title.into(),
               body: body.into(),
           }
       }
   }

   impl RenderOnce for Card {
       fn render(self, _window: &mut Window, _cx: &mut App) -> impl IntoElement {
           div()
               .flex()
               .flex_col()
               .gap_2()
               .p_4()
               .w(px(240.0))
               .bg(rgb(0x2e2e2e))
               .rounded_lg()
               .border_1()
               .border_color(rgb(0x505050))
               .text_color(rgb(0xffffff))
               .child(div().text_xl().child(self.title)) // 标题
               .child(div().text_sm().child(self.body))  // 正文
       }
   }

   /// 有状态的根视图:持有数据,负责驱动重绘。
   struct CardList {
       highlight: usize, // 当前高亮第几张卡
   }

   impl Render for CardList {
       fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
           let cards = [
               ("第一张", "RenderOnce 组件每帧被构建、消费、丢弃。"),
               ("第二张", "卡片自己不保存任何可变状态。"),
               ("第三张", "想变,就改外层实体的数据再 notify。"),
           ];
           div()
               .flex()
               .gap_4()
               .p_8()
               .size_full()
               .bg(rgb(0x1e1e1e))
               .items_start()
               .children(cards.iter().enumerate().map(|(index, (title, body))| {
                   Card::new(*title, body)
                       .when(index == self.highlight, |card| card.border_color(rgb(0xffaa00)))
               }))
       }
   }

   fn run_example() {
       application().run(|cx: &mut App| {
           let bounds = Bounds::centered(None, size(px(960.), px(360.0)), cx);
           cx.open_window(
               WindowOptions {
                   window_bounds: Some(WindowBounds::Windowed(bounds)),
                   ..Default::default()
               },
               |_, cx| cx.new(|_| CardList { highlight: 0 }),
           )
           .unwrap();
           cx.activate(true);
       });
   }

   #[cfg(not(target_family = "wasm"))]
   fn main() {
       run_example();
   }
   ```

   3. 运行:`cargo run -p gpui --example cards`。
   4. 修改 `CardList { highlight: 0 }` 中的数字为 `1`,再次运行。

3. **需要观察的现象**:第一次运行,窗口里横向排开三张深灰卡片,第一张带橙色边框;改 `highlight` 后重新运行,橙框移到第二张——你没有改 `Card` 的任何代码。

4. **预期结果**:验证了本讲的分工模型:`Card`(RenderOnce)是纯函数式的「数据 → 元素」配方,`CardList`(Render)持有状态;`.children(...)` 配 `.when(...)`(来自 4.3.3 提到的 `FluentBuilder`)按数据批量生成并按条件精修子元素。若编译报错,优先检查 `prelude::*` 是否已导入(`Render`、`RenderOnce`、`FluentBuilder` 的方法都由它带入)。

5. **进阶(选做)**:把 `CardList` 的字段改成 `Vec<(SharedString, SharedString)>`,体会「数据结构即界面结构」——列表数据变了,render 出的卡片自然跟着变。

## 6. 本讲小结

- **Render 是状态到 UI 的推导公式**:`Entity<T: Render>` 每帧被借用来重建元素树,树经 request_layout → prepaint → paint 三阶段上屏后整棵丢弃;状态长存于实体,元素只是每帧的瞬时描述。
- **RenderOnce 是无状态组件**:`render(self, ...)` 消费自身,配 `#[derive(IntoElement)]`(生成 `ViewElement<Self>`)即可直接作为 child;两者由 `View` trait 统一,区别在于有没有实体身份(`entity_id`)。
- **div() 是一体化容器**:同时实现 `Styled`(样式写入 `interactivity.base_style`)、`ParentElement`(`.child()`/`.children()` 收集子元素)与 `InteractiveElement`;构建期只攒数据,布局期递归展开。
- **notify 不画屏,只挂号**:`cx.notify()` → 窗口失效器记脏并唤醒帧源 → 帧回调发现脏才 `draw`;两级节流(窗口级 is_dirty、子树级 ViewElement 缓存)让 GPUI 按需渲染,静止界面零开销。
- **谁持有状态,谁驱动重绘**:RenderOnce 组件无法自己 notify,刷新由外层实体驱动;`window.refresh()` 则是绕过一切缓存的全量重绘兜底。

## 7. 下一步学习建议

本讲你已能「画出来」,下一讲 u2-l4《布局与样式:Tailwind 风格的 API》将深入你一直在调用却未细究的东西:`Styled` 方法链背后的 `Style`/`StyleRefinement` 结构、taffy 布局引擎如何解释 `.flex()`/`.gap_3()`,以及 `.when()`/`.when_some()` 的组合技巧。之后再进入 u2-l5《事件、Action 与键位分发》,补上 `Interactivity` 的交互面(`.on_click()`、`cx.listener`、actions!)。

继续阅读源码的建议顺序:

1. [crates/gpui/src/element.rs](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/src/element.rs) 的模块文档(前 32 行)——官方对元素生命周期最权威的一段话,值得逐句读。
2. [crates/gpui/examples/README.md](https://github.com/zed-industries/zed/blob/05473ed83d2bada157e89f181e15c205a3b22163/crates/gpui/examples/README.md) 按主题把示例分了类,挑 `text`、`opacity` 两个布局/样式类示例热身,为 u2-l4 做准备。
3. 在真实业务里找一个 `impl Render`(如 `crates/workspace/src/notifications.rs` 中第 304 行的 `LanguageServerPrompt`),对照本讲的分工模型分析:哪些部分本可以抽成 RenderOnce 组件?
