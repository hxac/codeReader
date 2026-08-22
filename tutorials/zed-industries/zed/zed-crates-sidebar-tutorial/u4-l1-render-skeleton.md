# 渲染主骨架：Render for Sidebar

## 1. 本讲目标

学完本讲，你应该能够：

1. 把 `Sidebar::render` 产出的 UI 树按「头部 → 列表主体 → 导入横幅 → 底部栏」四层分区说清楚，并知道每层由哪个辅助函数负责。
2. 解释 `no_open_projects` 与 `no_search_results` 这两个布尔量分别控制哪个替代视图，以及它们的数据来源。
3. 区分 `SidebarView::ThreadList` 与 `SidebarView::Archive` 两种视图的渲染分支。
4. 理解客户端窗口装饰（`Decorations::Client`）下侧边栏的绝对定位、圆角与「1px 外扩 + 补偿 padding」技巧，以及 `WindowBackgroundAppearance` 如何影响行标签的渲染方式。
5. 读懂 `render_sidebar_header` 与 `render_sidebar_bottom_bar` 这两个局部渲染函数的结构。

## 2. 前置知识

本讲假设你已学完单元三（尤其是 u3-l2 的重建管线）。需要用到的概念：

- **Render trait 与重渲染**：在 gpui 中，`Entity<Sidebar>` 之所以能显示在窗口里，是因为 `Sidebar` 实现了 `Render` trait。每当 `cx.notify()` 把实体标记为脏，gpui 就会再次调用 `render(&mut self, window, cx)`，把当前状态**投影**成一棵新的元素树。回忆 u3-l2：`update_entries` 每次收尾都会 `cx.notify()`，所以「全量重推导数据 → 全量重投影 UI」是同一哲学在数据侧和渲染侧的两面。
- **flexbox 与 `v_flex`/`h_flex`**：gpui 的布局是 flexbox（类似 Web）。`v_flex()` 是竖向排列子元素的容器，`h_flex()` 是横向的。样式方法名借鉴 Tailwind CSS：`p_1` 是 padding、`border_r_1` 是右侧 1px 边框、`flex_1` 是「占据剩余空间」。
- **FluentBuilder 链式写法**：渲染代码是一长串链式调用，其中三个方法负责条件分支：
  - `.when(条件, |el| ...)`：条件为真时应用闭包；
  - `.when_some(Option, |el, 值| ...)`：`Option` 有值时应用闭包；
  - `.map(|el| ...)`：无条件变换，常用来做 `match`。

  本讲的实践任务就是把这条长链「展开」成带缩进的树。
- **虚拟列表 `list` 与 `ListState`**：gpui 的 [list(state, render_item)](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/elements/list.rs#L24-L34) 只为视口内的行调用 `render_item` 构建元素，滚动位置和每行的实测高度都缓存在 `ListState` 里。u3-l3 讲过 `apply_list_state_diff` 如何保护这些测量值，本讲只看它在渲染树里的位置。注意这里传的不是普通闭包而是 `cx.processor(Self::render_list_entry)`——[Context::processor](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/app/context.rs#L264-L272) 会把「按需回调」包成「先 update 实体再回调」的处理器，gpui 由此保证渲染任何一行时借到的都是 `&mut Sidebar`。
- **窗口装饰（Decorations）**：窗口的标题栏、圆角、边框由谁画？[Decorations::Server](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L522-L531) 表示操作系统（窗口管理器）负责，应用只画内容区；`Decorations::Client { tiling }` 表示应用自己画（Windows、Linux 上常见），此时窗口四角是圆的，贴着角的 UI 必须自己配合画圆角。`Tiling` 结构体的四个布尔进一步表示窗口某条边是否平铺贴住屏幕边缘（[crates/gpui/src/platform.rs:L697-L708](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L697-L708)）——贴边的边是直角、无 1px 窗口边框。
- **1px 样式方法**：`pt_px()`、`pb_px()`、`mt_px()` 这类「`_px` 后缀」方法的值恒为 `px(1.)`。它们由宏生成：前缀（`pt`/`pb`/`pl`…）在 [crates/gpui_macros/src/styles.rs:L765-L793](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_macros/src/styles.rs#L765-L793) 定义指向 padding 的哪一侧，后缀 `px` 在 [styles.rs:L1083-L1087](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui_macros/src/styles.rs#L1083-L1087) 定义为 `px(1.)`（文档串就是 "1px"）。理解这点，4.2 节的「补偿 padding」就一目了然。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/sidebar/src/sidebar.rs` | 本讲主战场：`impl Render for Sidebar`（第 7760 行起）、`render_sidebar_header`、`render_sidebar_bottom_bar`、`render_sticky_header`、空态/无结果视图、导入横幅，全部在这个文件里 |
| `crates/theme/src/theme.rs` | 定义 `CLIENT_SIDE_DECORATION_ROUNDING`（客户端装饰圆角半径 10px）与 `ClientDecorationsExt` 辅助 trait |
| `crates/ui/src/utils/constants.rs` | `platform_title_bar_height`（平台标题栏高度）与 `TRAFFIC_LIGHT_PADDING`（macOS 红绿灯按钮左侧留白） |

归档视图实体 `ThreadsArchiveView` 来自 `agent_ui` crate，本讲只看它被嵌入渲染树的位置，内部留到 u8-l1。

## 4. 核心概念与源码讲解

### 4.1 `Render::render`：一棵 UI 树的四层分区

#### 4.1.1 概念说明

`render` 是侧边栏的「总装车间」：它自己几乎不画细节，而是把工作分派给一组 `render_*` 辅助函数，自己只决定**整体结构**——根容器的样式与定位、四种互斥/叠加的内容分支、子元素的排列顺序。理解本函数的最好方式不是逐行读链式调用，而是先把这棵树画出来。

整棵树自上而下是四层：

1. **头部**（`render_sidebar_header`）：标题栏高度的搜索区，混着窗口控件。
2. **列表主体**：三种可能——空态视图（没有任何打开的项目）、虚拟列表（正常情况，可能叠加「无结果」覆盖层与粘性分组头）、或整个换成归档视图实体。
3. **导入横幅**（可选）：ACP 外部代理导入、跨通道导入，各自独立判定。
4. **底部栏**（`render_sidebar_bottom_bar`）：收起按钮、历史切换按钮、最近项目按钮。

#### 4.1.2 核心流程

把 [render()](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7761-L7902) 的链式调用展开，得到这样一棵树：

```text
v_flex#workspace-sidebar                     ← 根：竖向 flex 列
│  .key_context(dispatch_context)            ← 键位上下文（u5-l1 详讲）
│  .track_focus(&focus_handle)               ← 可聚焦元素
│  .on_action(...) × 25                      ← 动作注册（u5-l1 详讲）
│  [.map 装饰分支]                            ← Server: h_full + w(width)
│                                            ← Client: absolute + 1px 外扩 + 圆角（4.2 详讲）
│  .bg(混合背景)  [.when 边框]                ← 左侧 → border_r_1；右侧 → border_l_1
│
├─ [.map 视图分支] match self.view
│   ├─ SidebarView::ThreadList:
│   │   ├─ render_sidebar_header(no_open_projects)          ← 第 1 层
│   │   └─ [.map]
│   │       ├─ if no_open_projects → render_empty_state      ← 替代视图 A
│   │       └─ else → v_flex.relative.flex_1.overflow_hidden
│   │           ├─ list(list_state, render_list_entry)       ← 虚拟列表
│   │           ├─ .when(no_search_results) → render_no_results   ← 替代视图 B
│   │           ├─ .when_some(sticky_header) → 粘性分组头
│   │           └─ custom_scrollbars(...)
│   └─ SidebarView::Archive(view) → view.clone()             ← 整体替换为归档视图
│
├─ [.map 横幅层]
│   ├─ .when(show_acp)           → render_acp_import_onboarding(verbose)
│   └─ .when(show_cross_channel) → render_cross_channel_import_onboarding(verbose)
│
└─ render_sidebar_bottom_bar                                  ← 第 4 层
```

两个关键布尔量在函数开头一次性算好：

- `no_open_projects = !self.contents.has_open_projects`：**世界里有没有任何打开的项目**。它决定第 2 层是显示「空态引导」（打开/克隆项目）还是显示列表。数据来自 `SidebarContents.has_open_projects`（u2-l1 讲过，rebuild 阶段维护）。
- `no_search_results = self.contents.entries.is_empty()`：**当前重建结果是否一行都没有**。它只是在列表容器**之上叠加**一个居中的「无结果/还没有线程」提示，列表本身仍然存在（只是没有行）。注意两者的层级差别：空态是**替换**列表区域，无结果是**覆盖**在列表容器上。

还要注意：粘性头部 `sticky_header` 是在构建树**之前**单独算好的（第 7764 行），它返回 `Option<AnyElement>`——不满足滚动条件时是 `None`，`.when_some` 直接跳过。

#### 4.1.3 源码精读

准备阶段——字体、粘性头、混合背景、两个开关：

[crates/sidebar/src/sidebar.rs:L7761-L7772](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7761-L7772) 中：先取 UI 字体并预先计算粘性头部；随后 `bg = title_bar_background.blend(panel_background.opacity(0.25))`——这就是讲义标题里说的「标题栏混合背景」。gpui 的 `blend` 语义是逐通道线性插值（见 [crates/gpui/src/color.rs:L57-L71](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/color.rs#L57-L71)）：

\[ c = (1-\alpha)\cdot c_{\text{title\_bar}} + \alpha\cdot c_{\text{panel}},\qquad \alpha = 0.25 \]

即整个侧边栏底色 = 75% 标题栏色 + 25% 面板色，让侧边栏与标题栏同源又略偏面板。第 7762 行的 `_titlebar_height` 变量名以下划线开头——它在 `render` 里算了但没使用，头部函数内部会自己再算一次。

根容器与动作注册（本讲只看结构，动作体系留到 u5-l1）：

[crates/sidebar/src/sidebar.rs:L7774-L7805](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7774-L7805) 用 `v_flex().id("workspace-sidebar")` 建根，挂上键位上下文、焦点追踪，并连续注册 25 个 `.on_action` 处理器：24 个是 `cx.listener(Self::某个方法)` 形态（选择移动、确认、折叠、归档、重命名、切换器……），最后 1 个是针对 `OpenRecent` 动作的闭包，用来开关「最近项目」弹出层。注册完 `.font(ui_font)` 统一字体。

视图分支——`SidebarView` 是个只有两个变体的枚举（[crates/sidebar/src/sidebar.rs:L131-L135](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L131-L135)）：

[crates/sidebar/src/sidebar.rs:L7852-L7886](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7852-L7886) 按 `self.view` 分流。`ThreadList` 分支先挂头部，再按 `no_open_projects` 二选一：空态走 `render_empty_state`；正常走一个 `relative + flex_1 + overflow_hidden` 的容器，依次放入 `list(self.list_state.clone(), cx.processor(Self::render_list_entry))` 虚拟列表、`no_search_results` 时的覆盖层、`when_some(sticky_header)` 的粘性头、以及 `custom_scrollbars`（滚动条把手绑定到 `list_state`）。`Archive` 分支最简单：`this.child(archive_view.clone())`——归档视图是一个独立的 `Entity<ThreadsArchiveView>`，直接把自己 `render` 出的元素树挂进来（实体 clone 只是句柄复制，不复制状态）。

横幅层与底栏收尾：

[crates/sidebar/src/sidebar.rs:L7887-L7902](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7887-L7902) 先算两个 `should_render_*` 布尔，再依 `.when(show_acp)` / `.when(show_cross_channel)` 各挂一条横幅，最后 `.child(self.render_sidebar_bottom_bar(cx))`。注意顺序：横幅是加在列表主体**之后**、底栏**之前**的子元素，所以在竖向 flex 列里恰好夹在两者中间。

#### 4.1.4 代码实践

**实践目标**：把 `render()` 的链式调用彻底「去语法糖」，练成一眼能看出分支结构的能力。

**操作步骤**：

1. 打开 [crates/sidebar/src/sidebar.rs:L7774-L7902](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7774-L7902)，准备好纸或本地文本文件。
2. 从 `v_flex()` 开始，每遇到 `.child(...)` 就向下一层缩进，每遇到 `.when(cond, ...)` / `.when_some(opt, ...)` / `.map(...)` 就写一行 `── when <条件> →` / `── map <这个 map 在 match 什么>` 的标注。
3. 对每个条件，追问两件事：条件的**数据来源字段**是什么（如 `self.contents.has_open_projects`）？它**何时变化**（哪个函数写入它）？
4. 对照 4.1.2 的树检查：你的树是否漏掉了 `.when(!tiling.top, ...)` 这类藏在装饰分支内部的二级条件（4.2 会展开）。

**需要观察的现象**：展开后你会发现 `render()` 里没有任何循环和复杂计算——所有「内容」都来自 `self.contents`（u3-l4 的重建结果）与各 `render_*` 函数；`render` 只做**结构决策**。

**预期结果**：得到一棵类似 4.1.2 的伪代码树（你的版本应更完整，含装饰分支内部的条件）。本实践为本地练习，不必提交。

#### 4.1.5 小练习与答案

**练习 1**：`no_open_projects` 与 `no_search_results` 能否同时为真？此时界面显示什么？

答案：能。关闭所有项目后 `has_open_projects` 为假，且 `entries` 必然为空。但此时只有**空态视图**生效——`render_no_results` 位于 `else` 分支的列表容器里，空态分支根本不会构建那个容器。也就是说空态优先级更高，二者是替换与覆盖的关系，不是平级选择。

**练习 2**：粘性头部为什么在构建树之前（第 7764 行）单独计算，而不是在列表容器的闭包里现算？

答案：因为它需要返回 `Option`——`render_sticky_header` 依据滚动位置判断「当前是否需要粘性头」，不需要时返回 `None`，`render` 里用 `.when_some(sticky_header, ...)` 处理。先算好后传入，也让树构建代码保持线性，避免在 `.when_some` 闭包里再嵌套一次「计算 + 使用」。

**练习 3**：`SidebarView::Archive` 分支为什么只有一行 `this.child(archive_view.clone())`？

答案：归档视图是独立实体 `Entity<ThreadsArchiveView>`（由 `agent_ui` 提供）。实体句柄的 `clone` 是廉价的引用复制；把它作为 child 挂入时 gpui 会调用它自己的 `Render` 实现。侧边栏因此不必关心归档视图的内部结构——这是「组合优于实现」的分层：sidebar 管切换，agent_ui 管内容。

### 4.2 客户端窗口装饰：Decorations、圆角与 1px 外扩

#### 4.2.1 概念说明

`WindowBackgroundAppearance` 与 `Decorations` 回答的是同一个问题的两面：**窗口长什么样，应用要配合做什么？**

- `Decorations` 决定**几何**：标题栏和窗口边框由谁绘制。`Server` 时侧边栏只是一个普通的全高块；`Client` 时窗口本身有 10px 圆角和 1px 边框，贴边的侧边栏必须自己画圆角、并处理与窗口边框的接缝。
- `WindowBackgroundAppearance` 决定**底色透明度**：Zed 允许透明/半透明窗口背景。侧边栏在非不透明窗口上要避免使用渐变淡出效果——渐变叠在透明背景上会渲染成一块可见的色斑。

本模块讲三个点：`Decorations` 匹配分支的几何技巧、圆角常量的来源、以及 `WindowBackgroundAppearance` 在行渲染里的一个应用。

#### 4.2.2 核心流程

客户端装饰下的定位算法（伪代码）：

```text
若 Decorations::Server:
    侧边栏 = 普通块，高 = 窗口高，宽 = self.width
若 Decorations::Client { tiling }:
    侧边栏 = 绝对定位，铺满自己这一侧
    对每条「未平铺」的边（top/bottom/靠窗的 left 或 right）:
        向外扩 1px（偏移 -1px）盖住窗口边框
        同时在该侧加 1px padding，把内容推回安全区
    对每个「两条邻边都未平铺」的角:
        画 10px 圆角（与窗口圆角对齐）
```

为什么必须外扩 1px？窗口在客户端装饰下自带 1px 描边。如果侧边栏老老实实从窗口内容区 (0, 0) 开始，它的圆角背景与窗口圆角轮廓之间会露出一圈透明缝。把背景矩形向未平铺的边各扩 1px，让背景「压」在窗口描边之下，圆角才能严丝合缝；扩出去的 1px 用 padding 补偿，避免内容（文字、边框线）挪位。

#### 4.2.3 源码精读

装饰匹配的主分支：

[crates/sidebar/src/sidebar.rs:L7806-L7847](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7806-L7847)。`Decorations::Server` 一行带过：`el.h_full().w(self.width)`（宽度即 `Sidebar` 的 `width` 字段，构造与恢复时被钳制在 200–800px 之间，[sidebar.rs:L104-L106](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L104-L106) 定义常量、[L7683](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7683) 执行钳制）。`Decorations::Client { tiling, .. }` 分支前的注释（7810-7816 行）原文解释了动机：侧边栏拥有其所在一侧的窗口圆角，要像标题栏、状态栏那样画圆角；在未平铺的边上向外拉伸 1px（带补偿 padding），使圆角背景与窗口形状精确对齐、避免圆角处出现透明缝。代码逐项执行：`.absolute()` 脱离文档流，`top`/`bottom` 在未平铺时取 `px(-1.)`（7819-7820 行），配合 `.pt_px()`/`.pb_px()`（7821-7822 行）补偿；左右方向按 `on_left` 二选一（7823-7833 行）：靠窗的那条边同样 `-1px` + `pl(px(1.))` 或 `pr(px(1.))`；最后四个 `.when` 只在「角的两条邻边都未平铺」时给对应角加圆角（7834-7845 行）。

圆角常量与现成的辅助 trait：

[crates/theme/src/theme.rs:L52-L77](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/theme/src/theme.rs#L52-L77) 定义 `CLIENT_SIDE_DECORATION_ROUNDING = px(10.0)`，并提供 `ClientDecorationsExt::rounded_client_corners(tiling)`——「两条邻边都未平铺才圆这个角」的通用实现。对比之下会发现 `render` **没有**复用这个 helper，而是手写四个 `.when`：因为该 helper 面向占满整窗的元素（一次处理全部四角），而侧边栏只拥有**自己一侧**的两个角，还要先知道 `on_left` 还是 `on_right`。这是「读源码时注意现成抽象为何没被复用」的好例子。

`WindowBackgroundAppearance` 的实际用武之地（预告 u4-l2）：

[crates/sidebar/src/sidebar.rs:L2292-L2306](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L2292-L2306) 在 `render_project_header` 里先判定 `opaque_window = window_background_appearance() == WindowBackgroundAppearance::Opaque`（枚举定义见 [crates/gpui/src/platform.rs:L2101-L2121](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/gpui/src/platform.rs#L2101-L2121)，含 Opaque/Transparent/Blurred/MicaBackdrop 等变体）；注释说明：文字的渐变淡出效果在透明窗口上会渲染成可见色斑，所以非不透明窗口改用 `truncate()` 截断标签。窗口外观设置由此一路影响到一行文字的展示方式。

顺带一提，`render_sidebar_header` 里也有一个装饰匹配（[crates/sidebar/src/sidebar.rs:L7215-L7218](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7215-L7218)）：客户端装饰时头部 `mt(px(-1.))` 向上缩 1px、服务端装饰时 `mt_px().pb_px()`——这是同一个 1px 接缝问题在头部的镜像处理（父容器顶部扩了 1px，头部要缩回去对齐标题栏）。

#### 4.2.4 代码实践

**实践目标**：把「1px 外扩 + 补偿 padding」的几何关系在纸面上算清楚。

**操作步骤**：

1. 画出侧边栏贴在窗口左侧、窗口**未平铺**时的横截面示意：窗口边框 1px、侧边栏背景从 `left = -1px` 开始、内容区因 `pl(px(1.))` 从 0 开始。标出背景左边缘与窗口外边缘重合的位置。
2. 再画窗口**左缘平铺**（`tiling.left = true`）时的示意图：此时 `left = px(0.)`、无补偿 padding、左上/左下角不画圆角。
3. 阅读 [crates/theme/src/theme.rs:L58-L74](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/theme/src/theme.rs#L58-L74) 的 `rounded_client_corners`，与 [sidebar.rs:L7834-L7845](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7834-L7845) 的四个 `.when` 逐条对照，圈出二者处理的角集合差异。

**需要观察的现象**：两份示意图里，「背景覆盖的区域」与「内容可见的区域」应当恰好相差 1px——这就是补偿 padding 存在的意义。

**预期结果**：能口头回答「为什么 `top = -1px` 时必须 `pt_px()`，而去掉任何一个都会出什么视觉问题（前者露透明缝，后者内容上移 1px）」。

#### 4.2.5 小练习与答案

**练习 1**：侧边栏配置在右侧（`SidebarSide::Right`），窗口顶部平铺、其余边未平铺。哪几个角会被画圆角？

答案：先把四个条件全部代值再下结论。侧边栏在右，`on_left` 为假，所以 `rounded_tl` / `rounded_bl` 两个分支直接短路；再看另外两个：`rounded_tr` 要求 `!(tiling.top || tiling.right)`，`tiling.top = true` 使其整体为假，**右上角不圆**；`rounded_br` 要求 `!(tiling.bottom || tiling.right)`，两条边都未平铺，条件成立，**右下角画圆角**。最终只有右下角是圆角——平铺的边保持直角。这道题的陷阱在于直觉容易答成「右上、右下两个角」，务必代回布尔表达式。

**练习 2**：为什么不把圆角半径写死在 sidebar 里，而要从 theme crate 引常量？

答案：客户端装饰的圆角半径是**窗口级**约定：标题栏、状态栏、侧边栏都必须用同一半径，各自的圆角才能拼出完整的窗口圆角。集中定义在 [theme.rs:L53](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/theme/src/theme.rs#L53) 一处，改主题时全体同步。

### 4.3 `render_sidebar_header`：标题栏混合背景与搜索区

#### 4.3.1 概念说明

头部是侧边栏与窗口标题栏的「共享区域」：它的高度必须等于平台标题栏高度，这样才能与旁边的窗口控件对齐；在 macOS 上红绿灯按钮（关闭/最小化/全屏）就叠在这条区域里，在 Windows/Linux 上最小化/最大化/关闭按钮画在头部内。与此同时它还承载搜索过滤框。所有平台差异（`cfg!(target_os = "macos")`、全屏状态、侧边栏在左还是在右、有无项目）都收敛在这个函数的一串布尔里。

#### 4.3.2 核心流程

```text
计算 3 个布局布尔（都要求非全屏）:
    traffic_lights        = macOS 且 在左
    left_window_controls  = 非 macOS 且 在左
    right_window_controls = 非 macOS 且 在右
构建 h_flex:
    高度 = platform_title_bar_height(window)
    装饰补偿: Client → mt(-1px)；Server → mt(1px) + pb(1px)
    [when left_window_controls]  → 嵌入左侧窗口控件
    左侧内边距: traffic_lights → 71 或 78px；否则 1.5rem
    [when !right_window_controls] → 右侧内边距 1.5rem
    [when !no_open_projects]:
        底边框 + （macOS 时）分隔线
        放大镜图标 + 过滤输入框
        [when 有选中且过滤框未聚焦] → FocusSidebarFilter 键位提示
        [when 有查询词] → 清空按钮
    [when right_window_controls] → 嵌入右侧窗口控件
```

#### 4.3.3 源码精读

布局布尔与高度：

[crates/sidebar/src/sidebar.rs:L7203-L7211](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7203-L7211) 一次性算出 `traffic_lights` / `left_window_controls` / `right_window_controls` 三个互斥布尔（都要求非全屏），并取 `platform_title_bar_height(window)` 作为头部高度。该函数在 [crates/ui/src/utils/constants.rs:L12-L25](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/utils/constants.rs#L12-L25)：非 Windows 平台为 `max(1.75 × rem, 34px)`（随 UI 缩放），Windows 固定 32px。

容器骨架与装饰补偿：

[crates/sidebar/src/sidebar.rs:L7213-L7231](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7213-L7231) 建 `h_flex().h(header_height)`，按装饰类型做 4.2 讲过的 1px 补偿；随后视情况嵌入窗口控件、设置左右内边距。macOS 红绿灯的左内边距用 `ui::utils::TRAFFIC_LIGHT_PADDING`（[constants.rs:L5-L10](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/utils/constants.rs#L5-L10)：值为 71px，若以 macOS SDK 26 或更高版本编译则为 78px；注释解释了为何用像素而非 rem——红绿灯按钮尺寸固定、不随 UI 缩放，且左侧多出的 1px 正是为窗口 1px 边框预留）。窗口控件的实际绘制委托给 `platform_title_bar` 模块的 [render_left_window_controls / render_right_window_controls](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7273-L7287)。

搜索区只在有项目时存在：

[crates/sidebar/src/sidebar.rs:L7233-L7267](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7233-L7267) 的 `.when(!no_open_projects, ...)` 块是头部的「增值部分」：加底边框、macOS 时在红绿灯后补一条分隔线、放放大镜图标和 `render_filter_input` 过滤输入框；当 `self.selection.is_some()` 且过滤框未聚焦时显示 `FocusSidebarFilter` 的键位提示（帮用户发现「按哪个键跳到搜索框」）；查询非空时显示清空按钮，点击后 `reset_filter_editor_text` 并手动 `update_entries`（过滤逻辑 u5-l3 详讲）。反过来读：当 `no_open_projects` 为真，头部只剩窗口控件和内边距——没有搜索框、没有底边框，这与 4.1 的空态分支互相呼应。

#### 4.3.4 代码实践

**实践目标**：理解头部高度的平台差异，以及 `no_open_projects` 对头部的「裁剪」效果。

**操作步骤**：

1. 阅读 [constants.rs:L12-L25](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/utils/constants.rs#L12-L25)，计算默认 UI 缩放（1rem = 16px）下非 Windows 平台的头部高度（应为 `max(28, 34) = 34px`），再算 UI 放大到 125%（1rem = 20px）时的高度（`max(35, 34) = 35px`）。
2. 在 [sidebar.rs:L7213-L7271](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7213-L7271) 中数一数：`no_open_projects = true` 时，最终元素树里还剩几个 child？（提示：`left_window_controls` 与 `right_window_controls` 互斥，且都要求非 macOS 才为真。）
3. 不修改代码，仅推演：macOS 全屏时 `traffic_lights` 和两个 `window_controls` 全为假，此时头部还剩什么？

**需要观察的现象**：三种平台情形（macOS 普通 / 非 macOS 在左 / 非 macOS 在右）下头部左端的内容完全不同，但高度和装饰补偿逻辑完全一致。

**预期结果**：第 2 步答案——`no_open_projects` 时最多只有 1 个 child（左侧或右侧窗口控件），macOS 上是 0 个；第 3 步答案——只剩内边距撑起的空头部（若同时无项目，搜索区也被裁掉）。全部为源码推演，待本地运行验证（可用 `cargo run -p zed` 切换全屏对比）。

#### 4.3.5 小练习与答案

**练习 1**：`render` 根容器的背景混合用 `opacity(0.25)`，粘性头部（`render_sticky_header`）的背景混合用 `opacity(0.2)`（[sidebar.rs:L3209-L3212](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3209-L3212)）。为什么要差这 0.05？

答案：粘性头部是**悬浮在列表之上**的覆盖层（`absolute` 定位、带 `shadow_sm` 投影），混得更「实」一点（panel 占比从 25% 降到 20%）配合阴影，让它在视觉上与下层滚动内容区分开。这是一个主观视觉调参，读源码时记住「两处混合同源、比例不同」即可。

**练习 2**：为什么 `platform_title_bar_height` 在 Windows 上直接返回 32px 而不用 rem？

答案：见 [constants.rs:L21-L25](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/ui/src/utils/constants.rs#L21-L25) 的 `todo(windows)` 注释：理想做法是向 Windows 平台 API 查询实际标题栏高度，当前先用固定值。这是「读注释识别已知技术债」的例子。

### 4.4 `render_sidebar_bottom_bar` 与导入横幅

#### 4.4.1 概念说明

底部栏是侧边栏的「全局操作区」，与头部不同，它**不随 `no_open_projects` 或视图切换而变化**——列表视图和归档视图共用同一条底栏。三个控件从左到右（在右侧时从右到左）是：收起侧边栏按钮（带侧别切换菜单）、线程历史（归档视图）切换按钮、弹性空隙、最近项目按钮。导入横幅则位于底栏与列表主体之间，是两条**独立判定、可同时出现**的引导条，教用户把外部代理（ACP）或其他渠道的历史线程导入 Zed。

#### 4.4.2 核心流程

```text
底栏:
    is_archive = 当前是否归档视图
    on_right   = 侧边栏在右侧
    h_flex: p_1 + gap_1 + 上边框
    [when on_right] → flex_row_reverse（控件顺序镜像）
    child 1: 收起按钮（popover 菜单触发器，点击调 multi_workspace.close_sidebar）
    child 2: 历史按钮（Clock 图标，toggle_state = is_archive，
             点击 → toggle_archive 动作处理器）
    child 3: div().flex_1()（弹性空隙）
    child 4: 最近项目按钮

横幅层（在 render 的倒数第二个 map 中）:
    show_acp           = 有外部代理 且 ACP 引导未 dismiss
    show_cross_channel = 有待导入渠道 且 未 dismiss
    verbose = import_banners_use_verbose_labels.get_or_insert(show_acp && show_cross_channel)
    [when show_acp]           → ACP 横幅（verbose ? 长/短按钮文案）
    [when show_cross_channel] → 跨通道横幅
```

#### 4.4.3 源码精读

底栏主体：

[crates/sidebar/src/sidebar.rs:L7343-L7372](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7343-L7372)。`is_archive` 来自 `matches!(self.view, SidebarView::Archive(..))`；`on_right` 时 `flex_row_reverse()` 把整行镜像，保证「收起按钮永远靠窗边」。历史按钮用 `toggle_state(is_archive)` 呈现按下态，tooltip 文案随状态在 "Show/Hide Thread History" 间切换，点击转发给 `Self::toggle_archive`——这正是切换 `ThreadList`/`Archive` 两种视图的入口之一（完整切换逻辑 u8-l1 讲）。收起按钮的实现在 [render_sidebar_toggle_button（L7289-L7341）](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7289-L7341)：一个锚定式 popover 菜单触发器，tooltip 里同时列出 Toggle/Focus 两个动作的键位，点击本体则通过 `window.root::<MultiWorkspace>()` 找到宿主并调用 `close_sidebar`。

横幅判定的「文案锁存」：

[crates/sidebar/src/sidebar.rs:L7887-L7901](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7887-L7901)。两条横幅的按钮文案默认都是 "Import Threads"；当**首次同时出现**时（`show_acp && show_cross_channel`），`import_banners_use_verbose_labels.get_or_insert(...)` 把 `verbose` 锁存为 `true`，此后两条横幅分别改用更长的 "Import Threads from External Agents" / "Import Threads from Other Channels" 加以区分。`import_banners_use_verbose_labels: Option<bool>` 是 `Sidebar` 的字段（初值 `None`，见 [sidebar.rs:L783-L788](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L783-L788) 的文档注释与 [L921](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L921) 的初始化）——`get_or_insert` 只在 `None` 时写入，所以「曾经同时出现」这一事实被记住，避免用户先看到短文案、随后文案突然变长的抖动。判定函数本身在 [should_render_acp_import_onboarding（L7427-L7441）](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7427-L7441) 与 [should_render_cross_channel_import_onboarding（L7467-L7470）](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7467-L7470)，都与各自的「已 dismiss」持久化设置联动。

两条横幅的内容与公共画法：

[render_acp_import_onboarding（L7443-L7465）](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7443-L7465) 的导入按钮会先 `show_archive`（切到归档视图，导入的历史线程落在那里）再弹导入模态框；[render_cross_channel_import_onboarding（L7472-L7517）](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7472-L7517) 会把渠道名拼进描述文案。两者都落到公共的自由函数 [render_import_onboarding_banner（L7612-L7675）](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7612-L7675)：`v_flex` + 顶边框 + 从强调色渐隐的 `linear_gradient` 背景 + 关闭按钮 + 全宽描边导入按钮。

#### 4.4.4 代码实践

**实践目标**：验证 `import_banners_use_verbose_labels` 的锁存语义——这是本模块最容易被误读为「每帧重算」的逻辑。

**操作步骤**：

1. 在 [sidebar.rs:L7888-L7893](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7888-L7893) 处推演四个场景下 `verbose` 的值与字段变化：
   - 场景 A：首帧只有 ACP 横幅（`show_acp=true, show_cross_channel=false`）；
   - 场景 B：首帧两条同时出现；
   - 场景 C：首帧只有一条，后来另一条也出现；
   - 场景 D：始终各只有一条交替出现。
2. 对每个场景写出：`get_or_insert` 的参数、字段最终值、两条横幅各自显示的按钮文案（对照 [L7456-L7460](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7456-L7460) 与 [L7508-L7512](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7508-L7512) 的三元表达式）。
3. 推演完成后，在 `sidebar_tests.rs` 中搜索 `import` 相关测试（若有）对照你的结论；若没有覆盖测试，记为「待本地验证」。

**需要观察的现象**：`Option<bool>` 字段一旦从 `None` 变为 `Some(false)` 或 `Some(true)`，后续任何帧都无法再改变它——`get_or_insert` 不覆盖已有值。

**预期结果**：场景 A 锁存 `Some(false)`，此后即使场景 C 两条同时出现，文案仍是短的；只有场景 B（首帧即同时出现）才锁存 `Some(true)` 用长文案。场景 D 始终短文案。以上为源码推演，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：底栏为什么用 `flex_row_reverse` 而不是把 child 顺序倒过来写？

答案：代码只写一份「收起 → 历史 → 弹性空隙 → 最近项目」的语义顺序，用 `flex_row_reverse`（[sidebar.rs:L7350](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7350)）在右侧布局时整体镜像。这样「哪些控件是一组、空隙在哪」的意图保持清晰，也不会因为维护两份顺序而出现不一致。（反过来手写两份 child 顺序也能实现，属于可读性取舍。）

**练习 2**：底栏在 `no_open_projects` 时会消失吗？归档视图时会消失吗？

答案：都不会。对照 [render 的收尾（L7902）](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7902)：`.child(self.render_sidebar_bottom_bar(cx))` 在所有条件分支之外无条件执行。底栏是跨视图的常驻操作区——哪怕空态也要允许用户收起侧边栏、打开最近项目。

**练习 3**：ACP 横幅的「Import」按钮为什么先 `show_archive` 再弹导入模态框？

答案：见 [sidebar.rs:L7448-L7451](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7448-L7451)：从外部代理导入的线程是**已完成的历史线程**，落在归档（线程历史）视图里。先切换视图，用户导入完成后立即能在当前视图看到结果，避免「导入成功却在列表里找不到」的困惑。

## 5. 综合实践

**任务：产出一份完整的 `render()` 条件树，并在真实 Zed 中逐格验证。**

1. **画树（纸面）**：把 [sidebar.rs:L7774-L7902](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L7774-L7902) 的整条链式调用展开成一棵带条件标注的完整伪代码树。要求覆盖**全部** `.when` / `.when_some` / `.map` 分支，包括 4.2 的装饰分支内部（`tiling` 四个方向、`on_left` 两个方向、四个圆角条件）与 4.4 的横幅分支。每个条件旁注明数据来源字段。
2. **标注两个开关（纸面）**：在你的树上用两种颜色（或记号）分别标出 `no_open_projects` 控制的子树（空态替换 + 头部搜索区裁剪，共两处消费点）与 `no_search_results` 控制的子树（列表容器内的覆盖层，一处消费点）。写一句话回答：为什么前者出现在两个地方、后者只有一个？
   - 参考答案：`no_open_projects` 描述「世界里没有项目」，头部搜索框对无项目毫无意义，所以头部与主体都要裁；`no_search_results` 只关心「本次重建结果为空」，是列表区域的局部状态。
3. **运行验证（本机，可选）**：`cargo run -p zed` 启动 Zed，依次制造四个状态并对照你的树：关闭所有项目（空态 + 头部只剩窗口控件）、在空项目里搜索不存在的词（无结果覆盖层）、点击底栏时钟按钮（Archive 分支替换主体）、把窗口贴到屏幕左缘（Linux 客户端装饰下圆角消失）。每项在树上打勾。若在某平台无法触发（如 macOS 服务端装饰），在树旁注明「该分支在本平台不可达」。此步骤的具体视觉表现**待本地验证**。

## 6. 本讲小结

- `render()` 是纯结构代码：四层分区（头部 → 列表主体 → 导入横幅 → 底部栏）由一组 `render_*` 辅助函数填充，内容全部来自 `self.contents`（u3-l4 的重建结果）。
- `no_open_projects` **替换**主体为空态并**裁剪**头部搜索区（两处消费）；`no_search_results` 只在列表容器上**叠加**覆盖层（一处消费）。二者数据来源分别是 `has_open_projects` 与 `entries.is_empty()`。
- `SidebarView::ThreadList` / `Archive(Entity<ThreadsArchiveView>)` 决定主体是自绘列表还是整体嵌入归档视图实体；底栏跨视图常驻。
- 客户端装饰下侧边栏绝对定位、向未平铺边外扩 1px 并用 1px padding 补偿、按「两邻边未平铺」规则画 `CLIENT_SIDE_DECORATION_ROUNDING`（10px）圆角；`WindowBackgroundAppearance` 非不透明时行标签弃用渐变淡出改用截断。
- 头部高度 = `platform_title_bar_height`（非 Windows 为 `max(1.75rem, 34px)`），三平台控件布尔互斥；混合背景 = 标题栏色 75% + 面板色 25%。
- 导入横幅的按钮文案由 `import_banners_use_verbose_labels` 的 `get_or_insert` **首帧锁存**，防止同时出现时文案抖动。

## 7. 下一步学习建议

- 下一讲 **u4-l2（项目分组头与粘性头部渲染）**：本讲只给了 `render_sticky_header` 一个「位置」（`when_some` 挂载点），下一讲深入 `render_project_header` 与粘性头部的 `top_offset` 推导（你已预习过 [sidebar.rs:L3142-L3227](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/sidebar/src/sidebar.rs#L3142-L3227) 的背景混合）。
- 之后 **u4-l3** 讲 `render_list_entry` 分流出的线程行/终端行渲染，补全列表主体的最后一层。
- 键位分发相关的 `dispatch_context` 与 25 个 `on_action` 留到 **u5-l1**；归档视图内部与 `toggle_archive` 的完整链路留到 **u8-l1**。
- 延伸阅读：[crates/theme/src/theme.rs](https://github.com/zed-industries/zed/blob/fd82517a115d97a07835b52f0512b22b38e38ccf/crates/theme/src/theme.rs#L52-L77) 的 `ClientDecorationsExt`，以及 Zed 标题栏如何用同一常量画窗口圆角——多处对比能加深对「窗口级几何约定」的理解。
