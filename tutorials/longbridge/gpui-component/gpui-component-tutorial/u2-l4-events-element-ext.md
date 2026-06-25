# 事件、元素扩展与焦点陷阱

## 1. 本讲目标

本讲讲解 gpui-component 中三块「给 GPUI 元素加挂通用能力」的基础设施。学完后你应该能够：

- 用 `InteractiveElementExt` 给元素挂上 GPUI 原生没有提供的事件（例如双击）。
- 用 `ElementExt` 的 `on_prepaint` 在元素绘制后拿到它的像素矩形，并据此实现定位类交互（tooltip / popover / 拖拽手柄等）。
- 用 `FocusTrapElement` 的 `focus_trap` 把一个容器变成「Tab 焦点循环」区域，理解模态对话框为什么需要它。
- 看懂贯穿三者的同一个 Rust 设计手法：**扩展 trait + blanket impl**。

这三个工具本身都很短小（核心实现加起来不到 250 行），但它们是整个库里 Dialog、Sheet、Popover、Tooltip、Slider、Table、Tiles 等组件交互能力的底座，掌握它们能让你看懂一大半组件源码。

## 2. 前置知识

本讲默认你已经学完：

- **u1-l4 应用入口、init 初始化与 Root**：知道每个窗口的第一层视图必须是 `Root`，它是窗口级状态（Sheet / Dialog / Notification / 键盘导航）的管理壳。
- **u2-l2 样式系统：Styled 与尺寸 Sizable**：知道 gpui-component 大量使用「扩展 trait + blanket impl」来给已有 trait 加方法（例如 `StyledExt` 给所有 `Styled` 元素加 `h_flex` / `v_flex`）。本讲的三块基础设施用的是**完全相同的手法**。

在进入源码前，先用三句话建立 GPUI 元素的直觉（这些是 GPUI 框架的概念，不是 gpui-component 自创的）：

- **元素（Element）**：GPUI 中一棵可绘制的树节点，类似浏览器里的 DOM 节点。`div()` 就是最常见的元素。
- **`InteractiveElement`**：带「交互能力」的元素，能挂监听器，如 `on_click`、`on_hover`、`on_mouse_down` 等。
- **`Stateful<E>`（状态化元素）**：给元素加上一个稳定 `id` 后得到的状态化版本。**只有状态化元素才能挂监听器**，因为 GPUI 需要用 id 在不同帧之间跟踪「这个元素是否被悬停、是否被点击」。

> 一个关键点：`div().id("foo")` 得到的就是 `Stateful<Div>`。绝大多数监听器都要求你先 `.id(...)`。

接下来三个小节的核心手法是同一个模式，先在这里讲透，后面不再重复：

```text
// 1) 定义一个「扩展 trait」，里面写你想新增的方法
pub trait XxxExt: SomeBase {
    fn new_method(...) { ... 默认实现 ... }
}

// 2) 用 blanket impl，让所有满足条件的类型「自动获得」这些方法
impl<T: SomeBase> XxxExt for T {}
```

这种写法的妙处在于：**你不需要修改 GPUI 的源码**，就能让 `div()` 这类元素直接拥有 `on_double_click`、`on_prepaint`、`focus_trap` 这些新方法。Rust 社区把这种模式叫 *extension trait*（扩展 trait）。后面三节你会看到它的三种具体形态。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [event.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/event.rs) | 定义 `InteractiveElementExt`，给状态化元素加双击事件 `on_double_click`。 |
| [element_ext.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/element_ext.rs) | 定义 `ElementExt`，核心是 `on_prepaint`，在元素绘制后回调出像素矩形 `Bounds`。 |
| [focus_trap.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/focus_trap.rs) | 定义 `FocusTrapElement` 的 `focus_trap`、全局 `FocusTrapManager` 与包装元素 `FocusTrapContainer`。 |
| [root.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs) | `Root` 拦截 `Tab` / `Shift-Tab` 动作，借助 `FocusTrapManager` 实现真正的焦点循环。 |
| [tooltip.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs) | tooltip 实现里用 `on_prepaint` + `on_hover` 组合，是 `ElementExt` 的典型用法。 |
| [lib.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs) | 在 `init(cx)` 里注册 `FocusTrapManager`，并把三个扩展 trait 通过 `pub use` 导出。 |

---

## 4. 核心概念与源码讲解

### 4.1 InteractiveElementExt：补齐 GPUI 缺少的事件

#### 4.1.1 概念说明

GPUI 的 `InteractiveElement` 已经提供了 `on_click`，但并没有直接提供「双击」事件。然而很多桌面交互天然需要双击，例如标题栏双击最大化窗口、文件双击打开。

`InteractiveElementExt` 这个扩展 trait 就是来补这个缺口的：它给所有状态化元素 `Stateful<E>` 加一个 `on_double_click` 方法。底层实现非常巧妙——**它复用 GPUI 已有的 `on_click`，只是在回调里多加一次「点击次数是否为 2」的判断**。

#### 4.1.2 核心流程

```text
on_double_click(listener)
   │
   ├─ 调用 self.interactivity().on_click(内部回调)   // 复用 GPUI 的单击监听
   │
   └─ 内部回调每次点击都收到 ClickEvent
            │
            └─ if event.click_count() == 2 {
                   listener(event, ...)               // 只有双击才真正触发用户逻辑
               }
```

GPUI 的 `ClickEvent` 会累计连续点击次数（短时间内连点 N 次，`click_count()` 就是 N）。所以「双击」本质上就是「`click_count() == 2` 的那次单击」。

#### 4.1.3 源码精读

trait 与方法定义、以及 blanket impl 集中在一个文件里：

[event.rs:3-21](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/event.rs#L3-L21) — `InteractiveElementExt` 定义了 `on_double_click`，并在末尾把它的能力授予所有 `Stateful<E>`。注意第 12 行复用的是 `self.interactivity().on_click(...)`，第 13 行用 `event.click_count() == 2` 做过滤；第 21 行的 blanket impl `impl<E: InteractiveElement> InteractiveElementExt for Stateful<E> {}` 是关键——**只有状态化元素才能拿到这个方法**，这和「监听器需要稳定 id」的要求一致。

一个真实使用例子在标题栏组件里。macOS / Windows 标题栏双击会触发窗口最大化或还原：

[title_bar.rs:275-278](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/title_bar.rs#L275-L278) — 给标题栏区域分别挂上「双击放大窗口」与「双击触发系统标题栏行为」两个回调。

#### 4.1.4 代码实践

**目标**：亲手用一次 `on_double_click`，验证它确实只在双击时触发。

**操作步骤**（这是一个源码阅读 + 最小改写型实践）：

1. 在 hello_world 示例 `examples/hello_world/src/main.rs` 里，把某个 `.id("...")` 的 `div` 上挂一个 `on_click` 和一个 `on_double_click`。
2. 两个回调里都 `println!` 一句话。

```rust
// 示例代码：对比单击与双击
use gpui_component::{InteractiveElementExt as _, StyledExt as _};

div()
    .id("click-box")
    .size(px(100.))
    .bg(cx.theme().primary)
    .rounded(cx.theme().radius_md)
    .on_click(|_, _, _| println!("单击触发"))
    .on_double_click(|_, _, _| println!("双击触发"))
```

**需要观察的现象**：慢速点一次只打印「单击触发」；快速连点两次会先打印两次「单击触发」，再打印一次「双击触发」。

**预期结果**：因为 `on_double_click` 复用了 `on_click`，第二次单击本身也会触发 `on_click`，所以你会看到单击日志和双击日志叠加——这正好印证了 4.1.2 的「双击即 click_count==2 的那次单击」。

> 运行效果依赖本地图形环境，若你无法运行桌面窗口，可仅做源码阅读：把 `event.rs` 的实现和本例对照，确认回调链路与 4.1.2 一致即可，标注「待本地验证」。

#### 4.1.5 小练习与答案

**Q1**：为什么 blanket impl 写成 `for Stateful<E>`，而不是 `for E: InteractiveElement`？

**参考答案**：因为 `on_double_click` 底层要调用 `self.interactivity().on_click(...)`，而 `on_click` 只在状态化元素（带 id）上可用。限制为 `Stateful<E>` 可以在编译期就阻止开发者对「没有 id 的普通 `div()`」调用双击，把错误前置到编译期。

**Q2**：如果想做一个「三击」事件，最小改动是什么？

**参考答案**：照葫芦画瓢再加一个方法，把判断条件从 `== 2` 改成 `== 3` 即可，整体结构不变。

---

### 4.2 ElementExt：用 on_prepaint 拿到元素的位置

#### 4.2.1 概念说明

很多交互都依赖「这个元素画在了屏幕的什么位置」：

- tooltip / popover 要紧贴触发元素的边缘弹出。
- Slider 的滑块、resizable 面板的拖拽手柄需要知道自身矩形来做命中测试。
- 表格、Dock 面板要记录自身 bounds 以便后续滚动、吸附计算。

GPUI 的布局与绘制分两个阶段：先 `request_layout` 算尺寸，再 `paint` 绘制。`ElementExt` 提供的 `on_prepaint` 就是在绘制完成后，把元素最终的像素矩形 `Bounds<Pixels>` 回调给你。

它的实现技巧同样很轻量——**它不是 GPUI 原生的钩子，而是往元素里塞一个「绝对定位、铺满、透明」的 `canvas` 子元素**，借 `canvas` 的绘制回调拿到外层 bounds。你可以把它理解成「在元素上贴一张隐形描图纸，画完后告诉你它的尺寸位置」。

#### 4.2.2 核心流程

```text
on_prepaint(f)
   │
   ├─ self.child( canvas(...) )            // 往元素里加一个 canvas 子节点
   │       .absolute()                      // 绝对定位
   │       .size_full()                     // 铺满父元素
   │
   └─ canvas 的布局回调拿到 bounds（== 父元素 bounds）
            │
            └─ 调用 f(bounds, window, cx)   // 把矩形交给用户
```

注意：`ElementExt` 的约束是 `ParentElement`（即「能装子元素的容器」），因为实现要点是「往里塞一个 canvas 子元素」。所以 `on_prepaint` 只能用在容器类元素上。

#### 4.2.3 源码精读

[element_ext.rs:34-54](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/element_ext.rs#L34-L54) — `ElementExt` 只有一个方法 `on_prepaint`。第 46-50 行用 `canvas(...).absolute().size_full()` 往元素里加一个铺满的画布子元素，画布的布局回调里拿到 `bounds` 并调用用户传入的 `f`；第 54 行的 blanket impl `impl<T: ParentElement> ElementExt for T {}` 让所有容器元素自动拥有它。

`on_prepaint` 在全库被大量使用，几乎凡是「需要记录自身位置」的组件都用它：

| 组件 | 用法 |
| --- | --- |
| [dock/mod.rs:1125](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dock/mod.rs#L1125) | DockArea 记录自身 bounds，用于面板拖拽命中测试 |
| [popover.rs:415](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs#L415) | popover 记录触发元素位置，以便浮层贴边弹出 |
| [table/state.rs:1455](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/table/state.rs#L1455) | 表格记录视口矩形，配合虚拟化渲染 |
| [slider.rs:693](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/slider.rs#L693) | 滑块记录轨道矩形，做拖拽命中 |

而 tooltip 是把 `on_prepaint` 和 GPUI 的 `on_hover` 组合起来的最佳范例：

[tooltip.rs:585-634](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L585-L634) — 这是 gpui-component 内部的 `ManagedTooltipExt`（`pub(crate)`，外部不直接用，但思路可直接借鉴）。先用 `Rc<Cell<Bounds>>` 做一个可被两个闭包共享的「位置盒子」（第 593 行），再用 `on_prepaint` 在每帧把最新 bounds 写进盒子（第 596-598 行），最后用 GPUI 的 `on_hover` 在鼠标进入时读出盒子里的 bounds 去定位 tooltip（第 599-623 行）。这正是本讲综合实践要复刻的「tooltip 式悬停」原型。

> 说明：`on_hover`、`on_mouse_down`、`track_focus`、`focus_next` 这些监听器都是 **GPUI 原生**提供的（来自 `StatefulInteractiveElement` / `Window`），不属于 gpui-component；本讲引用它们是因为扩展 trait 经常与它们组合使用。

#### 4.2.4 代码实践

**目标**：复刻 tooltip 的核心思路——用 `on_prepaint`（ElementExt）拿到元素矩形，用 `on_hover`（GPUI）感知鼠标进出，把位置打印出来。这是「tooltip 式悬停」的最小骨架（暂不接 Root 的 TooltipOverlay，只验证位置捕获）。

**操作步骤**：

1. 在一个带 `id` 的容器上同时挂 `on_prepaint` 和 `on_hover`。
2. 因为两个闭包都要访问同一个矩形，需要一个 `Rc<Cell<Bounds<Pixels>>>` 在它们之间共享（照搬 tooltip.rs 的做法）。

```rust
// 示例代码：on_prepaint + on_hover 组成 tooltip 式悬停（最小骨架）
use gpui::{Bounds, Pixels, Styled, prelude::FluentBuilder as _};
use gpui_component::{ElementExt as _, StyledExt as _, h_flex};
use std::{cell::Cell, rc::Rc};

let bounds_cell: Rc<Cell<Bounds<Pixels>>> = Rc::new(Cell::new(Bounds::default()));

h_flex()
    .id("hover-trigger")
    .w(px(160.))
    .h(px(48.))
    .bg(cx.theme().primary)
    .rounded(cx.theme().radius_md)
    // ① ElementExt：每帧把最新像素矩形写进共享盒子
    .on_prepaint({
        let bounds_cell = bounds_cell.clone();
        move |bounds, _, _| bounds_cell.set(bounds)
    })
    // ② GPUI on_hover：鼠标进出时读出矩形并打印
    .on_hover(move |hovered, _, _| {
        if *hovered {
            println!("鼠标进入，元素矩形 = {:?}", bounds_cell.get());
        } else {
            println!("鼠标离开");
        }
    })
```

**需要观察的现象**：鼠标移入色块时控制台打印一行带坐标的矩形；移出时打印「鼠标离开」。拖动或缩放窗口后再次悬停，矩形坐标会随之变化——证明 `on_prepaint` 是**每帧**更新位置，而非只算一次。

**预期结果**：你能拿到一个形如 `Bounds { origin: ..., size: ... }` 的像素矩形。这个矩形就是后续真正弹 tooltip / popover 时用来贴边定位的依据。

> 若无图形环境，可仅做源码阅读：把上面骨架与 [tooltip.rs:592-623](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L592-L623) 逐行对照，确认「共享盒子 + on_prepaint 写 + on_hover 读」三者一一对应即可，标注「待本地验证」。

#### 4.2.5 小练习与答案

**Q1**：为什么 `on_prepaint` 的实现要往元素里加一个 `canvas` 子元素，而不是直接改 GPUI 的绘制流程？

**参考答案**：gpui-component 是建立在 GPUI 之上的普通库，**不能也不应该改 GPUI 内部**。利用 GPUI 已有的「`canvas` 元素会在布局阶段拿到自身 bounds」这一能力，把它当作一个「隐形探针」塞进容器里，就能在不侵入框架的前提下拿到父元素矩形。这是典型的「用组合而非继承/侵入」的扩展思路。

**Q2**：`on_prepaint` 的 blanket impl 约束是 `ParentElement`，这对调用者意味着什么？

**参考答案**：意味着 `on_prepaint` 只能挂在「能装子元素」的容器上（如 `div`/`h_flex`）。如果你在一个叶子元素（不能有 child 的元素）上调用，会编译报错——这恰好把误用挡在了编译期。

---

### 4.3 FocusTrapElement：让 Tab 焦点循环在容器内

#### 4.3.1 概念说明

桌面应用里，模态对话框（Dialog）、抽屉（Sheet）有一个硬性可访问性要求：**按 Tab / Shift-Tab 时焦点不能逃逸到对话框背后的主界面**，而应在对话框内部的控件之间循环（按钮 A → 按钮 B → 按钮 C → 按钮 A ……）。这种行为叫**焦点陷阱（focus trap）**。

GPUI 原生的 Tab 导航是「在整个窗口里找下一个可聚焦元素」，并不天然支持「只在某个子树里循环」。`FocusTrapElement` 就是给容器加上这种能力的扩展 trait，它的方法叫 `focus_trap`。

它的实现分两层：

- **注册层**：`focus_trap` 把容器登记到全局 `FocusTrapManager`（一张「容器 id → 容器焦点句柄」的表）。
- **拦截层**：`Root` 拦截窗口级 `Tab` / `TabPrev` 动作，查表判断「当前焦点是否落在某个陷阱里」，若是则把焦点循环限制在该容器内。

#### 4.3.2 核心流程

整个机制是「注册 + 拦截」两部分协作，分属两个文件：

```text
【注册阶段 · focus_trap.rs】
focus_trap("trap1", &focus_handle)
   │
   ├─ 把容器包成 FocusTrapContainer（一个包装元素）
   │     └─ new() 里调用 base.track_focus(&focus_handle)  // 容器自身可被聚焦/包含
   │
   └─ 元素 request_layout 时
         └─ FocusTrapManager::register_trap(global_id, 容器句柄.downgrade(), cx)
               └─ 存进全局 HashMap，并 cleanup 已失效句柄

【拦截阶段 · root.rs】
用户按 Tab
   │
   └─ Root::on_action_tab
         ├─ FocusTrapManager::find_active_trap(window, cx)
         │     └─ 遍历所有陷阱，找「包含当前焦点的那个容器」
         │
         ├─ 若焦点在陷阱内：
         │     window.focus_next()
         │     若跳出了容器 → 继续向前找，直到回到容器内第一个可聚焦元素
         │
         └─ 若不在任何陷阱内：走 GPUI 默认的 window.focus_next()
```

循环的「回到起点」逻辑值得细看：它先正常 `focus_next`，再用 `container.contains_focused(...)` 判断焦点是否还在容器内；若已经跳出，就继续 `focus_next` 直到重新落回容器——等价于「跳过容器外面的元素，直接绕回容器开头」。为防止极端情况下死循环，代码用 `MAX_ATTEMPTS = 100` 兜底。

#### 4.3.3 源码精读

**注册层** —— 全部在 focus_trap.rs：

[focus_trap.rs:14-50](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/focus_trap.rs#L14-L50) — `FocusTrapElement` 只暴露一个方法 `focus_trap(id, focus_handle)`，它返回一个 `FocusTrapContainer` 包装元素。注意它的文档注释（第 19-26 行）清楚说明了三步工作原理：① 把元素登记为陷阱容器；② Tab/Shift-Tab 时由 Root 拦截；③ 焦点若要离开容器就绕回首/尾。第 50 行的 blanket impl `impl<T: InteractiveElement + Sized> FocusTrapElement for T {}` 让所有交互元素自动获得该方法。

[focus_trap.rs:53-101](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/focus_trap.rs#L53-L101) — 全局 `FocusTrapManager`，内部就是一张 `HashMap<GlobalElementId, WeakFocusHandle>`。`register_trap`（第 77 行）登记容器、顺带 `cleanup` 清理失效句柄；`find_active_trap`（第 84 行）遍历所有陷阱，用 `container.contains_focused(window, cx)` 找出当前焦点所在的那个容器。用 `WeakFocusHandle` 是为了「容器被销毁后能自动失效、不内存泄漏」。

[focus_trap.rs:119-127](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/focus_trap.rs#L119-L127) — `FocusTrapContainer::new` 在包装时调用 `child.track_focus(&focus_handle)`，让容器自身具备焦点身份。

[focus_trap.rs:176-187](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/focus_trap.rs#L176-L187) — 在元素的 `request_layout` 阶段（第 184 行）调用 `register_trap` 把自己登记进全局表。之所以放在 `request_layout` 而不是构造时，是因为此时才能拿到稳定的 `GlobalElementId`。

**拦截层** —— 在 root.rs：

[root.rs:21-33](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L21-L33) — `Root` 定义了两个窗口级动作 `Tab` / `TabPrev`，并在 `init` 里把 `tab` / `shift-tab` 绑定到这两个动作（限定在 `Root` 的 key context 内）。这就是「Tab 由 Root 统一接管」的入口。

[root.rs:453-486](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L453-L486) — `on_action_tab` 的完整循环逻辑：第 455 行先 `find_active_trap` 找当前陷阱；第 460 行 `focus_next` 试探；第 463 行若焦点跳出容器，则进入第 469-479 行的循环继续向前找，并以 `MAX_ATTEMPTS` 与「绕回起点」双重条件防止死循环。`on_action_tab_prev`（[root.rs:488-521](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L488-L521)）是对称的反向版本。

**初始化** —— `FocusTrapManager` 必须在启动时注册为全局：

[lib.rs:113](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/lib.rs#L113) — `init(cx)` 中调用 `focus_trap::init(cx)`，它执行 `cx.set_global(FocusTrapManager::new())`（见 [focus_trap.rs:9-11](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/focus_trap.rs#L9-L11)）。这正是 u1-l4 强调「必须先 `gpui_component::init(cx)`」的原因之一——漏调会让 `find_active_trap` 取不到全局而 panic。

**真实使用例子** —— Dialog 与 Sheet 都靠 `focus_trap` 实现模态循环：

[dialog/dialog.rs:522-526](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L522-L526) — Dialog 先 `track_focus(&self.focus_handle)` 再 `focus_trap(format!("dialog-{}", layer_ix), &self.focus_handle)`。注意多层 Dialog 嵌套时每个都用唯一的 `id`（带 `layer_ix`），保证 `FocusTrapManager` 能区分不同层。

#### 4.3.4 代码实践

**目标**：在一个容器上挂 `focus_trap`，按 Tab 观察焦点是否只在容器内循环、不逃逸到外部按钮。

**操作步骤**：

1. 准备一个 `FocusHandle`（用 `cx.focus_handle()` 创建）。
2. 渲染时：容器内放 2-3 个可聚焦按钮，容器用 `.id(...).track_focus(&handle).focus_trap("my-trap", &handle)` 包裹；容器**外**再放一个按钮作为「对照」。
3. 运行后用 Tab 键反复按下。

```rust
// 示例代码：focus_trap 最小验证（示意，需嵌入一个 Render 视图里）
use gpui::FocusHandle;
use gpui_component::{Button, FocusTrapElement as _, StyledExt as _, h_flex, v_flex};

// 在视图结构体里持有：container_handle: FocusHandle
// 构造时：container_handle: cx.focus_handle()

v_flex()
    .gap_4()
    // ── 对照：陷阱「外部」的按钮 ──
    .child(Button::new("outside").label("我在陷阱外面"))
    // ── 陷阱容器 ──
    .child(
        v_flex()
            .id("trap-area")
            .track_focus(&self.container_handle)
            .focus_trap("my-trap", &self.container_handle)
            .border_1()
            .p_4()
            .gap_2()
            .child(Button::new("b1").label("陷阱内 1"))
            .child(Button::new("b2").label("陷阱内 2"))
            .child(Button::new("b3").label("陷阱内 3")),
    )
```

**需要观察的现象**：先把焦点移进陷阱容器（点击 b1）。然后连续按 Tab，焦点应在 `b1 → b2 → b3 → b1` 之间循环；按 Shift-Tab 反向循环 `b1 → b3 → b2`。**焦点永远不会跳到容器外的「我在陷阱外面」按钮**。

**预期结果**：验证 4.3.2 的「循环 + 不逃逸」。若你把 `.focus_trap(...)` 这行注释掉再重跑，Tab 会正常跳到外部按钮——对比之下就能直观看到陷阱的作用。

> 若无图形环境无法观察键盘焦点，可改做源码阅读型验证：在 [root.rs:453-486](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/root.rs#L453-L486) 里追踪 `find_active_trap` → `contains_focused` → 循环 `focus_next` 的调用链，确认逻辑与上述现象一致即可，标注「待本地验证」。

#### 4.3.5 小练习与答案

**Q1**：`FocusTrapManager` 为什么存的是 `WeakFocusHandle` 而不是 `FocusHandle`？

**参考答案**：陷阱容器可能被销毁（例如 Dialog 关闭）。用弱引用 `WeakFocusHandle` 后，容器一旦销毁，`upgrade()` 就返回 `None`，`cleanup()` 会把它从表里清掉。若存强引用 `FocusHandle`，已关闭的对话框会一直留在表里，既泄漏内存，又可能让 `find_active_trap` 命中已不存在的容器。

**Q2**：为什么 Dialog 里要先 `track_focus` 再 `focus_trap`，而且两者传的是同一个 `focus_handle`？

**参考答案**：`focus_trap` 内部（`FocusTrapContainer::new`）会调用 `track_focus` 把容器注册成可聚焦；`find_active_trap` 判断「焦点是否在陷阱内」用的正是 `container.contains_focused(...)`，它依赖容器有一个焦点身份。Dialog 显式 `track_focus` 是为了把这个焦点句柄同时用于焦点恢复等其它逻辑，保证「登记陷阱」和「判断归属」用的是同一个句柄。

---

## 5. 综合实践

把本讲三块内容串起来：做一个带「悬停高亮 + tooltip 式位置打印 + 焦点循环」的小卡片容器。

**任务**：

1. 用 u1-l4 的最小应用骨架（`init(cx)` → `open_window` → `Root` 包裹视图）建一个视图。
2. 视图里渲染一个卡片容器，容器内放 2 个 `Button`，容器外放 1 个 `Button` 作对照。
3. 给卡片容器挂 `on_prepaint`（ElementExt）+ `on_hover`（GPUI），实现「鼠标进入时打印卡片矩形坐标」——这就是 tooltip 式悬停的最小骨架。
4. 给卡片容器挂 `.id("card").track_focus(&handle).focus_trap("card-trap", &handle)`，让 Tab 在卡片内两个按钮间循环、不逃逸到外部按钮。

**验收要点**：

- 鼠标悬停卡片：控制台打印带坐标的矩形；窗口缩放后坐标会变（验证 `on_prepaint` 每帧更新）。
- 焦点进入卡片后按 Tab：只在卡片内两个按钮循环，跳不出去。
- 注释掉 `focus_trap` 那一行重跑：Tab 会跳到外部按钮，形成对比。

**提示**：这一步把 4.2 的位置捕获与 4.3 的焦点循环合到了同一个容器上——这正是真实组件（如带操作的 Dialog 卡片）的常见组合。

> 完整可运行需要桌面图形环境与一个 `Render` 视图，若本地无法运行，请以源码阅读方式完成：把上述骨架与 [tooltip.rs:585-634](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs#L585-L634)、[dialog/dialog.rs:522-526](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs#L522-L526) 两处真实用法逐行对照，确认结构无误，标注「待本地验证」。

## 6. 本讲小结

- 三块基础设施（`InteractiveElementExt` / `ElementExt` / `FocusTrapElement`）用的是**同一个手法**：定义扩展 trait + blanket impl，在不改 GPUI 源码的前提下给元素加方法。
- `InteractiveElementExt` 的 `on_double_click` 复用 GPUI 的 `on_click`，用 `click_count() == 2` 过滤；只授予 `Stateful<E>`。
- `ElementExt` 的 `on_prepaint` 往容器里塞一个铺满的 `canvas` 子元素作「隐形探针」，在绘制后回调出像素矩形 `Bounds<Pixels>`；只授予 `ParentElement`。
- `FocusTrapElement` 的 `focus_trap` 把容器登记进全局 `FocusTrapManager`；真正的 Tab 循环逻辑在 `Root::on_action_tab` 里，靠 `find_active_trap` + `contains_focused` 实现「不逃逸、绕回首尾」。
- `FocusTrapManager` 必须由 `init(cx)` → `focus_trap::init(cx)` 注册为全局，这是 u1-l4 强调先调 `init` 的原因之一。
- 这些工具是 Dialog / Sheet / Popover / Tooltip / Slider / Table / Dock 等组件交互能力的底座，读懂它们等于拿到了大半组件的钥匙。

## 7. 下一步学习建议

- 下一讲 **u3-l1 按钮家族** 会用到本讲的 `on_click`（GPUI）与样式系统，建议结合本讲的「扩展 trait」视角去看 Button 是如何被组织成一个无状态 `RenderOnce` 组件的。
- 想深入「位置捕获」的真实应用，直接读 [popover.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/popover.rs) 与 [tooltip.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/tooltip.rs)，看 `on_prepaint` 拿到的 bounds 如何喂给 Root 的浮层 overlay。
- 想深入「焦点循环」的真实应用，读 [dialog/dialog.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/dialog/dialog.rs) 与 [sheet.rs](https://github.com/longbridge/gpui-component/blob/a0ae3a37b960b732801782dd8c27cb993ff57b59/crates/ui/src/sheet.rs)，看模态组件如何把 `focus_trap` 与动画、层级（layer_ix）结合起来——这部分会在 **u5-1 弹窗：Dialog 与 Alert** 详细展开。
