# 渲染主骨架：Render for Sidebar

## 1. 本讲目标

学完本讲，你应该能够：

1. 把 `Sidebar::render` 产出的 UI 树按「头部 → 列表主体 → 导入横幅 → 底部栏」四层分区说清楚，并说出每层对应的辅助函数。
2. 解释 `no_open_projects` 与 `no_search_results` 两个布尔量分别控制哪个替代视图，以及它们各自的数据来源。
3. 区分 `SidebarView::ThreadList` 与 `SidebarView::Archive` 两种视图的渲染分支，知道哪些区域是两种视图共享的。
4. 理解客户端窗口装饰（`Decorations::Client`）下侧边栏的绝对定位、圆角与「1px 外扩 + 补偿 padding」技巧，以及 `WindowBackgroundAppearance` 在本 crate 中的用武之地。
5. 读懂 `render_sidebar_header` 与 `render_sidebar_bottom_bar` 这两个局部渲染函数的结构。

## 2. 前置知识

本讲假设你已学完单元三（尤其是 u3-l2 的重建管线与 u3-l3 的列表测量保留）。需要用到的概念：

- **Render trait 与重渲染**：在 gpui 中，`Entity<Sidebar>` 之所以能显示在窗口里，是因为 `Sidebar` 实现了 `Render` trait。每当 `cx.notify()` 把实体标记为脏，gpui 就会再次调用 `render(&mut self, window, cx)`，把当前状态**投影**成一棵新的元素树。回忆 u3-l2：`update_entries` 每次收尾都会 `cx.notify()`，所以「全量重推导数据 → 全量重投影 UI」是同一哲学在数据侧和渲染侧的两面。
- **flexbox 与 `v_flex` / `h_flex`**：gpui 的布局是 flexbox（类似 Web）。`v_flex()` 是竖向排列子元素的容器，`h_flex()` 是横向的。样式方法名借鉴 Tailwind CSS：`p_1` 是 padding、`border_r_1` 是右侧 1px 边框、`flex_1` 是「占据剩余空间」。
- **FluentBuilder 链式写法**：渲染代码是一长串链式调用，其中三个方法负责条件分支：
  - `.when(条件, |el| ...)`：条件为真时应用闭包；
  - `.when_some(Option, |el, 值| ...)`：`Option` 有值时应用闭包；
  - `.map(|el| ...)`：无条件变换，常用来嵌一个 `match`。

  本讲的实践任务就是把这条长链「展开」成带缩进的树。
- **虚拟列表 `list` 与 `ListState`**：gpui 的 [list(state, render_item)](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/elements/list.rs#L23-L34) 只为视口内的行调用 `render_item` 构建元素，滚动位置和每行的实测高度都缓存在 `ListState` 里。u3-l3 讲过 `apply_list_state_diff` 如何保护这些测量值，本讲只看它在渲染树里的位置。注意这里传的不是普通闭包而是 `cx.processor(Self::render_list_entry)`——[Context::processor](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/app/context.rs#L262-L272) 会把「按需回调」包成「先 update 实体再回调」的处理器，gpui 由此保证渲染任何一行时借到的都是 `&mut Sidebar`。
- **窗口装饰（Decorations）**：窗口的标题栏、圆角、边框由谁画？[Decorations::Server](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/platform.rs#L520-L531) 表示操作系统（窗口管理器）负责，应用只画内容区；`Decorations::Client { tiling }` 表示应用自己画（Windows、部分 Linux 桌面常见），此时窗口四角是圆的，贴着角的 UI 必须自己配合画圆角。`Tiling` 结构体的四个布尔进一步表示窗口某条边是否平铺贴住屏幕边缘（[platform.rs:L697-L708](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/platform.rs#L697-L708)）——贴边的边是直角、没有 1px 窗口边框。
- **`_px` 后缀样式方法**：`pt_px()`、`pb_px()`、`mt_px()` 这类方法的值恒为 `px(1.)`。它们由宏生成：`Styled` trait 体里展开的 `gpui_macros::style_helpers!()`（[styled.rs:L22-L34](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/styled.rs#L22-L34)）按「前缀 + 后缀」拼方法名，后缀 `px` 的定义就是 `px(1.)`（[styles.rs:L1083-L1087](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui_macros/src/styles.rs#L1083-L1087)，文档串写作 "1px"）。理解这点，4.2 节的「补偿 padding」就一目了然。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/sidebar/src/sidebar.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7760-L7903) | 本讲主战场：`impl Render for Sidebar`（7760 行起）以及 `render_sidebar_header`、`render_sidebar_bottom_bar`、`render_no_results`、`render_empty_state`、`render_sticky_header` 等局部渲染函数 |
| [crates/gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/platform.rs#L520-L531) | `Decorations` 枚举与 `Tiling` 结构体定义 |
| [crates/gpui/src/elements/list.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/elements/list.rs#L23-L34) | `list()` 虚拟列表元素构造函数 |
| [crates/gpui/src/app/context.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/gpui/src/app/context.rs#L262-L272) | `Context::processor`：把实体方法包装成列表回调 |
| [crates/theme/src/theme.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/theme/src/theme.rs#L52-L74) | `CLIENT_SIDE_DECORATION_ROUNDING`（10px）常量与 `ClientDecorationsExt` 圆角助手 |
| [crates/ui/src/utils/constants.rs](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/utils/constants.rs#L12-L25) | `platform_title_bar_height`：平台标题栏高度 |

## 4. 核心概念与源码讲解

### 4.1 Render::render：一棵四层竖排的树

#### 4.1.1 概念说明

`render()` 是侧边栏的「总装车间」：它自己几乎不画任何具体内容，而是把四层区域组装成一棵竖向元素树——

1. **头部**（`render_sidebar_header`）：平台标题栏 + 搜索过滤行；
2. **列表主体**：虚拟列表、空态视图或归档视图三者择一；
3. **导入横幅**：ACP 外部 Agent 导入与跨通道导入两条引导横幅（条件出现）；
4. **底部栏**（`render_sidebar_bottom_bar`）：折叠按钮、历史按钮、最近项目按钮。

同时它在本讲要厘清三组「开关」：

- **两个视图**：`SidebarView::ThreadList`（默认）与 `SidebarView::Archive(archive_view)`（历史归档），决定主体区域渲染什么；
- **两个布尔**：`no_open_projects` 与 `no_search_results`，决定线程列表视图下用哪个替代视图顶替列表；
- **两种装饰**：`Decorations::Server` 与 `Decorations::Client`，决定根容器的尺寸与定位方式（4.2 节专门讲）。

#### 4.1.2 核心流程

把 [render()](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7760-L7903) 的链式调用展开，得到这样一棵树（✓ 表示该分支的触发条件）：

```text
v_flex("workspace-sidebar")                        # 竖排根容器
├─ key_context(dispatch_context)                   # 键位上下文（u5-l1 详讲）
├─ track_focus(focus_handle)                       # 让侧边栏可聚焦
├─ on_action × 24                                  # 动作注册（u5-l1 详讲）
├─ font(ui_font)                                   # UI 字体
├─ map: match window.window_decorations()          # ✓ 装饰模式
│   ├─ Server   → h_full().w(self.width)           #   常规流式布局
│   └─ Client   → absolute + 1px 外扩 + 圆角 + 补偿 padding
├─ bg(title_bar_background ⊕ panel_background·25%) # 混合背景
├─ when(在左侧) → border_r_1                        # ✓ side(cx) == Left
│  when(在右侧) → border_l_1                        # ✓ side(cx) == Right
├─ map: match &self.view                           # ✓ 视图分支
│   ├─ ThreadList
│   │   ├─ render_sidebar_header(no_open_projects)
│   │   └─ map: if no_open_projects                #   ✓ 没有任何打开的项目
│   │   │       └─ render_empty_state()            #     空态：打开项目/克隆仓库
│   │   │     else
│   │   │       └─ v_flex(relative, flex_1, overflow_hidden)
│   │   │           ├─ list(list_state, render_list_entry)   # 虚拟列表
│   │   │           ├─ when(no_search_results) → render_no_results()  # ✓ entries 为空
│   │   │           ├─ when_some(sticky_header) → 粘性项目头  # ✓ 滚动越过某个分组头
│   │   │           └─ custom_scrollbars(Vertical) # 绑定 list_state 的滚动条
│   └─ Archive(archive_view)
│       └─ archive_view.clone()                    #   归档子实体整体接管主体
├─ when(show_acp)           → ACP 导入横幅          # ✓ 检测到外部 Agent 且未关闭
├─ when(show_cross_channel) → 跨通道导入横幅        # ✓ 发现其他通道线程且未关闭
└─ render_sidebar_bottom_bar()                     # 底部栏（两种视图都渲染）
```

两个布尔的分工：

| 布尔 | 定义处 | 数据来源 | 控制的替代视图 |
| --- | --- | --- | --- |
| `no_open_projects` | `!self.contents.has_open_projects` | u2-l1 讲过的 `SidebarContents.has_open_projects`，由 `rebuild_contents` 维护 | `render_empty_state()`：整个列表主体被 `ProjectEmptyState` 替换，提供「打开项目 / 克隆仓库」入口；同时让头部退化成纯标题栏（见 4.3） |
| `no_search_results` | `self.contents.entries.is_empty()` | 重建后的可见行序列（含分组头） | `render_no_results()`：覆盖在列表位置上的提示，文案随是否在搜索细分两种 |

注意一个细节：u3-l4 讲过搜索过滤发生在分组头压入之前，整组无命中时连分组头一并丢弃——所以「过滤无命中」和「有项目但一个线程都没有」最终都表现为 `entries` 为空，靠 `render_no_results` 里是否查询非空来区分文案。

#### 4.1.3 源码精读

**（1）序幕：三个准备值与两个布尔**

[sidebar.rs:L7760-L7772](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7760-L7772)：先算 UI 字体和粘性头部（粘性头部是个 `Option`，此时只是「预演」，后面用 `when_some` 决定挂不挂），再把标题栏底色与 25% 不透明度的面板底色混合，作为整个侧边栏的底色——让它和标题栏视觉上连成一体。最后派生本讲的两个关键布尔：

```rust
let no_open_projects = !self.contents.has_open_projects;
let no_search_results = self.contents.entries.is_empty();
```

顺带一提，第 7762 行的 `let _titlebar_height = ...` 是一个带下划线前缀的**未使用**绑定——标题栏高度在头部函数内部另算（见 4.3），这里只是遗留，也提醒我们不必把源码当圣物。

**（2）根容器：身份、焦点与动作注册**

[sidebar.rs:L7774-L7805](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7774-L7805)：`v_flex().id("workspace-sidebar")` 建立带状态的根容器，`.key_context(...)` 挂上键位上下文（`ThreadsSidebar` + `menu`，u5-l1 展开），`.track_focus(...)` 接入焦点系统，随后是 23 个 `cx.listener(...)` 加 1 个内联 `OpenRecent` 共 24 个 `on_action` 注册——键盘交互的入口全部集中在这里。这些注册与布局无关，所以本讲只数个数、不逐个展开。

**（3）视图分支：ThreadList 与 Archive**

[sidebar.rs:L7852-L7886](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7852-L7886) 是主体区域的 `match &self.view`。`SidebarView` 本身只有两个变体（[sidebar.rs:L130-L135](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L130-L135)）：

```rust
enum SidebarView {
    #[default]
    ThreadList,
    Archive(Entity<ThreadsArchiveView>),
}
```

- `ThreadList` 分支先挂头部，再按 `no_open_projects` 二选一：空态视图，或「`relative` + `flex_1` + `overflow_hidden`」的主体容器。主体容器里有四样东西：虚拟列表、无结果提示、粘性项目头、滚动条。列表回调传的是 `cx.processor(Self::render_list_entry)`，行分发逻辑（分组头/线程/终端）在 [render_list_entry（sidebar.rs:L2164-L2221）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2164-L2221)，具体行的画法留给 u4-l2、u4-l3。
- `Archive` 分支只有一行：`.child(archive_view.clone())`——归档视图是 `agent_ui` 里的 `ThreadsArchiveView` 实体，整体接管主体区域（它有自己的头部和搜索，u8-l1 详讲）。注意头部、横幅、底部栏在这个 `match` 之外，所以**底部栏和横幅两种视图共享，而 `render_sidebar_header` 只在线程列表视图渲染**。

粘性头部由 [render_sticky_header（sidebar.rs:L3142-L3227）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L3142-L3227) 现算：从 `list_state.logical_scroll_top()` 找到「最后一个已滚出视口顶部的分组头」，若它确实被滚过（或正被滚出一半）就返回一个 `absolute` 定位、叠在列表顶部的分组头元素，还会根据下一个分组头滚入的位置计算 `top_offset` 让它被「推走」。u3-l3 讲过为什么必须保护 `ListState` 的测量值——`bounds_for_item` 有值，这里的推进动画才不会闪跳。

**（4）两个替代视图**

无结果提示在 [render_no_results（sidebar.rs:L7151-L7170）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7151-L7170)：一个居中的 `Label`，文案由 `has_filter_query` 决定——有查询时 "No threads match your search."，否则 "No threads yet"。空态在 [render_empty_state（sidebar.rs:L7172-L7195）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7172-L7195)：`ProjectEmptyState` 组件带「打开项目」（派发 `workspace::Open` 动作）与「克隆仓库」（派发 `git::Clone`）两个回调，还打了遥测事件。

**（5）横幅与底栏收尾**

[sidebar.rs:L7887-L7902](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7887-L7902)：两个 `.when` 条件挂横幅（判定函数在 [sidebar.rs:L7427-L7441](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7427-L7441) 与 [L7467-L7470](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7467-L7470)，横幅本体是自由函数 [render_import_onboarding_banner（sidebar.rs:L7612-L7675）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7612-L7675)）。一个有意思的细节：两条横幅同时出现时会用更长的按钮文案，且这个「详细模式」经 `get_or_insert` 记进 `import_banners_use_verbose_labels` 字段后**不再回退**——避免其中一条被关闭后文案突然变短造成跳动。最后 `.child(self.render_sidebar_bottom_bar(cx))` 无条件挂底栏。

#### 4.1.4 代码实践

**实践目标**：把 `render()` 的链式调用亲手展开成伪代码树，并能回答两个布尔各自切换哪个替代视图。

**操作步骤**（本地练习，不必提交）：

1. 打开 [sidebar.rs:L7760-L7903](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7760-L7903)，**先遮住 4.1.2 节的树**，从上到下逐个链式节点抄写成带缩进的树，给每个 `.when` / `.when_some` / `.map` 标注触发条件。
2. 展开完后与 4.1.2 的树对照，找出你漏掉的分支（最常见的遗漏：`no_open_projects` 时头部仍会渲染、横幅在两种视图下都可能出现）。
3. 追一下数据来源：`has_open_projects` 在哪被赋值（提示：`rebuild_contents` 内，u3-l4 讲过）；`entries` 何时会为空。

**需要观察的现象**：树里同一条链上 `.map` 出现了四次（装饰、视图、横幅、以及头部内的 match），每层 `map` 都是一个「结构性二选一」。

**预期结果**：

- `no_open_projects = true`（窗口里没有任何打开的项目）→ 主体变成 `ProjectEmptyState` 空态，头部的搜索行也被 `.when(!no_open_projects, ...)` 摘掉；
- `no_search_results = true` 且有项目打开 → 列表位置出现居中提示，有查询词时说「无匹配」，无查询词时说「还没有线程」。

改代码验证（可选）：本地把 [L7771](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7771) 改成 `let no_open_projects = true;` 后 `cargo run -p zed`，应看到空态视图常驻（**待本地验证**，实验后记得还原）。

#### 4.1.5 小练习与答案

**练习 1**：归档视图下按 `Escape`（`cancel` 动作）仍能被侧边栏处理吗？依据是什么？

答案：能。24 个 `on_action` 注册在根容器上（[L7778-L7804](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7778-L7804)），而视图 `match` 只替换主体子树，根容器及其动作注册在任何视图下都在。

**练习 2**：`render_sticky_header` 在 `render()` 开头就被调用（[L7764](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7764)），但在空态视图下它的结果去哪了？

答案：被丢弃。它返回 `Option<AnyElement>`，只有线程列表分支里的 `.when_some(sticky_header, ...)`（[L7875](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7875)）会消费它；空态分支不接这个变量，值直接被扔掉（而且空态下列表无内容，`render_sticky_header` 也会因找不到分组头返回 `None`）。

**练习 3**：为什么 `no_search_results` 的判定用 `entries.is_empty()` 而不是「线程数为 0」？

答案：`entries` 是**可见行**序列，包含分组头。u3-l4 讲过过滤发生在压入分组头之前，整组无命中连头一起丢；所以 `entries` 为空准确表达了「没有任何可见行」——无论是因为过滤无命中，还是因为真的没有线程。若用「线程数为 0」，分组头还在时会误判。

### 4.2 客户端窗口装饰：绝对定位、圆角与 1px 外扩

#### 4.2.1 概念说明

服务端装饰下窗口是方的，应用只管画内容；客户端装饰下（Windows、部分 Linux），**窗口的圆角和边框由应用自己画**。侧边栏贴着窗口左缘或右缘，一旦它占据了窗口的一侧，那一侧的两个窗口圆角就落在侧边栏的「辖区」里——所以侧边栏必须把自己的对应角也画成圆角，否则圆角窗口后面会露出一块方形的背景色。

这带来两个工程问题：

1. **对齐**：客户端装饰的窗口四周有一圈 1px 的窗口边框。窗口内容区若老老实实从 0 开始画，圆角背景和窗口形状会差 1px，圆角处露出透明缝。
2. **贴边（tiling）**：窗口被平铺到屏幕边缘时，那条边没有圆角也没有 1px 边框，处理方式必须区分。

`WindowBackgroundAppearance` 则是另一个平台维度：窗口背景可以是不透明 / 半透明 / 模糊。本 crate 只在一处直接用到它——判断窗口是否不透明（见 4.2.3 末尾），影响项目分组头行内渐变标签的渲染方式。

#### 4.2.2 核心流程

客户端装饰分支的规则可以总结成一张表（对上下左右每条边独立适用）：

| 该边状态 | 根容器该边 | 效果 |
| --- | --- | --- |
| 贴边（`tiling.top/left/right/bottom == true`） | 定位到 `px(0.)` | 与窗口齐平，直角 |
| 未贴边 | 定位到 `px(-1.)`（向外扩 1px）并加 1px 补偿 padding | 背景盖住 1px 窗口边框，内容又被 padding 推回原位 |

圆角规则：某个角要圆，当且仅当**相邻两条边都未贴边**。圆角半径恒为

\[ r = \text{CLIENT\_SIDE\_DECORATION\_ROUNDING} = 10\,\text{px} \]

侧边栏在左侧时管左上/左下角，在右侧时管右上/右下角。

#### 4.2.3 源码精读

装饰分派在 [sidebar.rs:L7806-L7847](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7806-L7847)。`Server` 分支只有一行：`el.h_full().w(self.width)`，即常规流式布局、按用户拖的宽度占位。

`Client` 分支前面有一段注释（[L7810-L7816](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7810-L7816)），翻译过来就是 4.2.1 的两个工程问题：客户端装饰下侧边栏拥有所在一侧的窗口圆角，所以要像标题栏、状态栏那样画圆角；侧边栏在未贴边的边上向外多画 1px（配合补偿 padding），让圆角背景与窗口形状精确对齐，避免圆角处出现透明缝。代码逐层做三件事：

1. **绝对定位 + 上下边**（[L7817-L7822](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7817-L7822)）：`.absolute()` 脱离常规布局，`.top(...)`/`.bottom(...)` 按贴边与否取 `0` 或 `-1`，未贴边再加 `.pt_px()`/`.pb_px()` 把内容推回 1px。
2. **左右边**（[L7823-L7833](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7823-L7833)）：靠窗口内侧的一端永远对齐 `0`；靠外的一端按贴边与否取 `0` 或 `-1` 加补偿 `pl/pr(px(1.))`。
3. **四角圆角**（[L7834-L7845](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7834-L7845)）：`on_left` 时对左上/左下角、`on_right` 时对右上/右下角，条件统一是「该角的两个邻边都未贴边」。

半径常量来自 theme crate：[theme.rs:L52-L53](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/theme/src/theme.rs#L52-L53) 定义 `CLIENT_SIDE_DECORATION_ROUNDING = px(10.0)`，紧随其后的 [ClientDecorationsExt::rounded_client_corners（theme.rs:L57-L74）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/theme/src/theme.rs#L57-L74) 把同样的「两邻边都不贴边才圆角」规则封装成任意 `Styled` 元素可用的助手。侧边栏没有直接用它，因为除了圆角它还要同时处理 1px 外扩与补偿 padding——这三件事必须配套，分开写反而容易漏。

`WindowBackgroundAppearance` 的用法在 [sidebar.rs:L2292-L2295](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L2292-L2295)（位于 `render_project_header` 内）：只有窗口背景**不透明**时才允许给分组标签加渐变淡出（fade gradient），半透明/模糊窗口上渐变会渲染成一块可见的色斑，所以改为直接截断文本。这是「渲染决策依赖平台外观设置」的一个典型小样本。

#### 4.2.4 代码实践

**实践目标**：给定 `tiling` 与侧边栏位置，推出哪些圆角类会被应用，验证你读懂了规则。

**操作步骤**：

1. 阅读规则函数 [ClientDecorationsExt::rounded_client_corners（theme.rs:L57-L74）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/theme/src/theme.rs#L57-L74)，它把四角的判定写得最紧凑。
2. 对下面五个场景，先自己写出 sidebar 根容器（左侧布局）会应用哪些 `rounded_*` 与哪些 `-1px` 外扩：
   - a. 自由窗口（四边都不贴）；
   - b. 窗口贴住屏幕左缘（`tiling.left = true`，其余 false）；
   - c. 窗口贴住屏幕顶缘（`tiling.top = true`，其余 false）；
   - d. 全屏平铺（四边都 true，实际上此时窗口通常无装饰）；
   - e. 布局切到右侧、自由窗口。
3. 对照 [sidebar.rs:L7817-L7845](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7817-L7845) 逐条核对。

**需要观察的现象**：外扩（`-1px`）与圆角是两个独立维度——场景 b（贴左缘）不外扩左边但仍然不圆左上/左下角？请用代码条件 `!(tiling.top || tiling.left)` 自己验证这个推断。

**预期结果**：

- a：左上、左下都圆，四边都外扩 1px；
- b：左边对齐 0 且 `pl(px(1.))` 不加；左上/左下角因 `tiling.left` 为真**不圆**；
- c：顶边不外扩、不加 `pt_px()`；左上角不圆，左下角仍圆；
- d：全部直角、全部对齐 0；
- e：右上、右下圆（`rounded_tr`/`rounded_br`）。

**待本地验证**：在 Linux/Windows 上 `cargo run -p zed`，把窗口贴到屏幕边缘再拖离，观察侧边栏圆角的出现与消失；macOS 默认服务端装饰，此分支不会走到。

#### 4.2.5 小练习与答案

**练习 1**：为什么要「外扩 1px 再用 padding 补回来」这么绕，而不是直接让内容区从 0 开始？

答案：窗口形状（含圆角）覆盖到 1px 边框外侧，而内容区起点在边框内侧。若背景只画内容区，圆角处窗口形状与背景之间会差 1px，露出透明缝隙。向外多画 1px 让背景与窗口形状重合，再用 padding 把实际内容推回原位，视觉内容不位移。

**练习 2**：`Decorations::Server` 分支为什么不需要任何圆角处理？

答案：服务端装饰由操作系统画窗口边框和形状，窗口对应用呈现的就是一个矩形内容区，侧边栏画什么都填不满也出不了界，直接 `h_full().w(self.width)` 即可（[L7809](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7809)）。

**练习 3**：贴边（tiling）状态下窗口为什么「没有 1px 边框」？这从代码哪里能反推出来？

答案：代码对贴边边取 `px(0.)` 且不加补偿 padding（[L7819-L7822](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7819-L7822)），即窗口形状与内容区在那条边上直接重合——若那里仍有 1px 边框，这种对齐就会露出缝隙，与未贴边分支的处理方式相同。圆角判定同样把贴边边视为直角（`rounded_client_corners` 的条件），三者互相印证。

### 4.3 render_sidebar_header：标题栏、窗口控件与过滤行

#### 4.3.1 概念说明

头部是一行高度固定为平台标题栏高度的 `h_flex`，它要同时扮演两个角色：

1. **窗口标题栏的一部分**：客户端装饰/非 macOS 平台上，窗口控制按钮（关闭/最小化/最大化，Linux/Windows）或红绿灯（macOS）可能落在侧边栏区域，头部要给它们腾位置；
2. **搜索过滤行**：放大镜图标 + 过滤输入框 + 清除按钮，仅当窗口里有打开的项目时才有意义。

这两件事靠一组平台相关的布尔量协调，而 `no_open_projects` 为真时过滤行整个被摘掉，头部退化成纯标题栏。

#### 4.3.2 核心流程

头部开头的六个布尔（[L7203-L7211](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7203-L7211)）可以列成一张推导表：

| 布尔 | 定义 | 含义 |
| --- | --- | --- |
| `sidebar_on_left` / `sidebar_on_right` | `self.side(cx) == ...`（设置项，[L7695-L7697](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7695-L7697)） | 侧边栏靠哪侧 |
| `not_fullscreen` | 非全屏且非简单全屏 | 全屏时窗口控制按钮消失 |
| `traffic_lights` | macOS ∧ 非全屏 ∧ 在左侧 | 红绿灯画在左侧侧边栏头部 |
| `left_window_controls` | 非 macOS ∧ 非全屏 ∧ 在左侧 | Linux/Windows 窗口控制按钮画在左 |
| `right_window_controls` | 非 macOS ∧ 非全屏 ∧ 在右侧 | 同上，画在右 |

随后按「装饰模式 → 窗口控件 → 左右留白 → 过滤行 → 右侧控件」的顺序装配。

#### 4.3.3 源码精读

**（1）高度与装饰微调**

[sidebar.rs:L7211-L7218](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7211-L7218)：头部高度取 `platform_title_bar_height(window)`——非 Windows 平台为 `1.75 × rem` 且不低于 34px，Windows 固定 32px（[constants.rs:L12-L25](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/ui/src/utils/constants.rs#L12-L25)）。然后按装饰模式微调：

```rust
.map(|header| match window.window_decorations() {
    Decorations::Client { .. } => header.mt(px(-1.)),
    Decorations::Server => header.mt_px().pb_px(),
})
```

这两行是 4.2 的「补偿 padding」在头部的对偶：客户端装饰下根容器加了 `pt_px()`（顶部补偿），头部用 `mt(px(-1.))` 把自己**顶回去** 1px，占据那圈 padding——因为标题栏内容本应贴着窗口顶端；服务端装饰下（macOS 的 1px 窗口边框）反而加 1px 上边距避让边框，再加 1px 下内边距保持总高度不变。

**（2）窗口控件与留白**

[sidebar.rs:L7219-L7231](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7219-L7231)：左侧有窗口控件时插入控件组（macOS 红绿灯则加 `TRAFFIC_LIGHT_PADDING` 左内边距，那是给 1px 窗口边框留的余量）；两侧都不是控件区时给 `pl_1p5`/`pr_1p5` 的常规留白。控件组本身委托给 [render_left/right_window_controls（L7273-L7287）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7273-L7287)，再转交给 `platform_title_bar` crate 的同名函数——侧边栏只决定「放不放」，不关心按钮长什么样。

**（3）过滤行**

[sidebar.rs:L7233-L7267](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7233-L7267)：`.when(!no_open_projects, ...)` 包住整段——底边框、红绿灯右侧的分隔线、放大镜图标、过滤输入框（`render_filter_input`，内部是 `filter_editor` 实体并捕获回车动作用于确认选中，[L6553-L6563](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6553-L6563)）、以及两个条件小部件：键盘有选中且焦点不在过滤框时显示「聚焦过滤框」的键位提示；有查询词时显示清除按钮（点击后重置文本并手动触发一次 `update_entries`）。搜索交互链的细节属于 u5-l3。

#### 4.3.4 代码实践

**实践目标**：写出头部布尔的取值矩阵，解释「同一份头部代码如何适应三种平台 × 两种侧 × 全屏与否」。

**操作步骤**：

1. 阅读 [sidebar.rs:L7203-L7211](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7203-L7211) 的六个布尔定义。
2. 画一张 6 列表格，行是这些场景：macOS 左侧、macOS 右侧、Linux 左侧、Linux 右侧、Linux 左侧全屏、macOS 左侧全屏。逐格填 `traffic_lights` / `left_window_controls` / `right_window_controls` 的值。
3. 对每行推断头部的可见内容（控件在哪边、过滤行是否存在）。

**需要观察的现象**：`no_open_projects` 与平台布尔是正交的——空态视图下 macOS 左侧的头部仍然要给红绿灯腾位子。

**预期结果**：macOS 左侧 → 红绿灯 + 无过滤行（若空态）；Linux 右侧 → 控件在右（`right_window_controls`），头部左端直接是留白/过滤行；全屏 → 三者全 false，头部只剩留白和（可选的）过滤行。可在 Zed 设置里切换 `agent_panel` 的 sidebar_side 并观察头部变化（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：客户端装饰下头部为什么用 `mt(px(-1.))` 而 `Server` 下却要 `mt_px()`（方向相反）？

答案：`Client` 分支根容器为了盖住 1px 窗口边框加了 `pt_px()`，头部作为第一个子元素用负 margin 顶回去，让标题内容贴住窗口顶；`Server` 分支（macOS）窗口自带 1px 边框且在内容区外侧，头部加 1px 正 margin 避让，再补 1px 底部 padding 保持行高。

**练习 2**：清除搜索按钮的 `on_click` 里为什么要手动调 `this.update_entries(cx)`？u3-l2 不是说变化会自动触发重建吗？

答案：u3-l1/u3-l2 讲过，自动重建依赖事件订阅——过滤框的 `BufferEdited` 订阅在**用户键入**时触发；程序化调用 `reset_filter_editor_text` 设置文本不会产生用户的编辑事件，所以这里手动补一次 `update_entries` 让列表立刻反映清空后的状态（[L7260-L7263](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7260-L7263)）。

**练习 3**：头部的 `.when(!no_open_projects, ...)` 里包含底边框。空态视图下头部没有底边框，视觉上如何区分头部和空态？

答案：由空态组件自己负责——`ProjectEmptyState` 是垂直居中的大块内容，与头部之间自然留白；头部无过滤行时只剩留白与窗口控件，两者无需边框也能区分。这是一个「结构性区分让位于组件自带视觉」的小取舍。

### 4.4 render_sidebar_bottom_bar：三个按钮与镜像布局

#### 4.4.1 概念说明

底部栏是一行带顶边框的 `h_flex`，固定三个成员：折叠/展开侧边栏的开关按钮、切换线程历史（归档视图）的时钟按钮、以及被 `flex_1` 空隙推到另一端的「最近项目」按钮。它是两种视图共享的导航区，也是用户在归档视图与列表视图之间切换的入口之一。

#### 4.4.2 核心流程

```text
h_flex (p_1, gap_1, border_t_1)
├─ when(在右侧) → flex_row_reverse        # ✓ 镜像布局
├─ 侧边栏开关按钮（贴边角锚定的上下文菜单触发器）
├─ 历史按钮（toggle_state 高亮表示当前在归档视图）
├─ div().flex_1()                          # 弹性空隙
└─ 最近项目按钮（弹出最近项目列表）
```

关键点是 `flex_row_reverse`：侧边栏在右侧时整行镜像，开关按钮始终贴着**窗口外侧边缘**，最近项目按钮靠内侧——与左侧布局形成对称。

#### 4.4.3 源码精读

[sidebar.rs:L7343-L7372](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7343-L7372)：开头取两个布尔——`is_archive`（当前是否在归档视图，决定时钟按钮的高亮态与提示文案「显示/隐藏线程历史」）和 `on_right`。容器 `.when(on_right, |this| this.flex_row_reverse())` 镜像，然后依次挂：

- **开关按钮**：由 [render_sidebar_toggle_button（L7289-L7341）](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7289-L7341) 构造。它是一个 `sidebar_side_context_menu`（锚定在贴边角的弹出菜单）包着的 `IconButton`，图标按左/右布局选择，点击后向窗口根部的 `MultiWorkspace` 请求 `close_sidebar`；提示气泡里同时列出「切换侧边栏」和「聚焦侧边栏」两个动作的键位。
- **历史按钮**：`IconButton` + `toggle_state(is_archive)`，点击走 `toggle_archive(&ToggleThreadHistory, ...)`——这正是 render 根上注册的动作之一，按钮点击与键位殊途同归（u8-l1 详讲切换的副作用）。
- **空隙 + 最近项目按钮**：`div().flex_1()` 把后面的内容推到行尾；`render_recent_projects_button`（[L6565 起](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L6565)）是个 `PopoverMenu`，弹出 `SidebarRecentProjects` 列表，弹出句柄存在 `recent_projects_popover_handle` 上——render 根上那个内联的 `OpenRecent` 动作注册（[L7802-L7804](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7802-L7804)）就是从外部（比如命令面板）toggle 这个弹窗。

#### 4.4.4 代码实践

**实践目标**：验证「镜像布局」的实际效果，并跟踪一次按钮点击的完整去向。

**操作步骤**：

1. 阅读 [sidebar.rs:L7333-L7339](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7333-L7339)：开关按钮的 `on_click` 里 `window.root::<MultiWorkspace>()` 是在找谁？为什么侧边栏不自己改自己的可见性？（提示：回忆 u1-l1——侧边栏由宿主 `MultiWorkspace` 持有并注册。）
2. 写下三个按钮各自的「点击 → 最终调用」链：开关按钮、历史按钮、最近项目按钮。
3. 本地把 `.when(on_right, |this| this.flex_row_reverse())` 临时注释掉，`cargo check -p sidebar` 通过后 `cargo run -p zed`，把侧边栏设为右侧，观察按钮顺序（**待本地验证**，观察后还原）。

**需要观察的现象**：右侧布局下若去掉 `flex_row_reverse`，开关按钮会跑到靠窗口内侧的一端，「最近项目」贴住窗口边缘——与左侧布局不再对称。

**预期结果**：三条链分别是——开关按钮 → `multi_workspace.close_sidebar(window, cx)`（可见性归宿主管）；历史按钮 → `toggle_archive` → 切换 `SidebarView` 并序列化（u8-l1）；最近项目按钮 → 打开 `SidebarRecentProjects` 弹出菜单。

#### 4.4.5 小练习与答案

**练习 1**：历史按钮的 `toggle_state(is_archive)` 在视觉上表达什么？

答案：按钮呈「激活/按下」样式，表示当前正处于归档（线程历史）视图——底栏在两种视图下都渲染，这个高亮是用户判断「我在哪个视图」的即时线索。

**练习 2**：为什么最近项目按钮的弹出句柄要存成字段 `recent_projects_popover_handle`，而不是像其他按钮那样在渲染时临时创建？

答案：`OpenRecent` 动作（命令面板/键位可触发）需要在渲染循环之外 toggle 这个弹窗（[L7802-L7804](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7802-L7804)）；临时创建的句柄出不了渲染函数，字段让「按钮内部」和「外部动作」共享同一个弹出状态。

**练习 3**：底部栏在归档视图下会渲染吗？依据？

答案：会。`.child(self.render_sidebar_bottom_bar(cx))` 在视图 `match` 之外（[L7902](https://github.com/zed-industries/zed/blob/7eec89207ccfbef7ba366da22fc885079a5c0296/crates/sidebar/src/sidebar.rs#L7902)），只被装饰/背景等根级属性包裹。

## 5. 综合实践

设计一张「状态 → UI」真值表，把本讲所有分支串起来：

1. 列出四个输入维度：视图（ThreadList / Archive）、`no_open_projects`（true/false）、`no_search_results`（true/false）、装饰模式（Server / Client）。
2. 对下面六个组合，逐一写出渲染树中**每个区域**（头部、主体、横幅、底栏）分别渲染什么，并给出依据的源码行号：
   - 自由窗口 + ThreadList + 有项目 + 有线程（常态）；
   - 自由窗口 + ThreadList + 有项目 + 过滤无命中；
   - 自由窗口 + ThreadList + 无项目（刚打开一个空窗口）；
   - 贴左缘平铺 + ThreadList + 常态（哪些角不圆？哪些边不外扩？）；
   - 任意装饰 + Archive 视图（头部还是搜索行吗？底栏还在吗？）；
   - 自由窗口 + 常态 + 同时出现两条导入横幅（按钮文案用哪个版本？之后关掉一条呢？）。
3. 全部写完后，用 `cargo run -p zed` 实际制造其中至少三种状态（清空搜索、切到线程历史、贴边窗口）核对推断（**待本地验证**）。

这张表完成后，你就拥有了一份 `render()` 的「调试速查表」——以后侧边栏显示异常时，先定位异常属于哪个区域、哪个分支，再顺着行号进源码。

## 6. 本讲小结

- `render()` 是纯投影：四层竖排结构（头部 → 主体 → 横幅 → 底栏），自身不画具体行，行渲染经 `cx.processor(Self::render_list_entry)` 交给虚拟列表按需调用。
- 两个布尔各管一个替代视图：`no_open_projects`（来自 `contents.has_open_projects`）切换 `ProjectEmptyState` 空态并顺带摘掉头部过滤行；`no_search_results`（来自 `contents.entries.is_empty()`）挂出居中提示，文案由是否有查询词决定。
- 视图 `match` 只替换主体：`Archive` 分支让 `ThreadsArchiveView` 子实体整体接管，而底栏和导入横幅在两种视图下共享。
- 客户端装饰分支做三件配套的事：未贴边各边向外扩 1px 盖住窗口边框、加 1px 补偿 padding 推回内容、对「两邻边都不贴边」的角应用 `CLIENT_SIDE_DECORATION_ROUNDING`（10px）圆角；头部再用 `mt(px(-1.))` 顶回补偿 padding。
- `WindowBackgroundAppearance` 在本 crate 仅一处直接使用：非不透明窗口上禁用分组标签的渐变淡出、改为截断。
- 底部栏靠 `flex_row_reverse` 实现左右镜像，开关/历史/最近项目三个按钮分别通向宿主的 `close_sidebar`、`toggle_archive` 与最近项目弹出菜单。

## 7. 下一步学习建议

本讲只拆了「骨架」——每个区域内部怎么画还没展开。建议按顺序继续：

1. **u4-l2（项目分组头与粘性头部渲染）**：`render_project_header` / `render_sticky_header` 的内部——分组标签、状态徽标、计数、折叠交互，以及粘性头部的推进动画细节。
2. **u4-l3（线程行与终端行渲染）**：`render_thread` / `render_terminal` 与 `ThreadItem` 组件的拼装、图标前缀拆分与搜索高亮。
3. **u4-l4（菜单、工作区标签与默认分支预取）**：头部省略号菜单与 `DefaultBranchCache` 预取。
4. 想先补渲染前置机制的，可以回看 **u3-l3**（`EntryShape` 与测量保留）——本讲的虚拟列表之所以能稳定地只重排变化区间，靠的就是那一讲讲的差异应用算法。
