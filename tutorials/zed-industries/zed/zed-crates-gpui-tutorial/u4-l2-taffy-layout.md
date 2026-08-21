# Taffy 布局引擎集成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 GPUI 为什么把布局外包给 Taffy，以及 `TaffyLayoutEngine` 在一帧里如何被「建树 → 计算 → 查询 → 清空」。
2. 对照 `Style::to_taffy` 的映射代码，说出 GPUI 的 `Style` 字段如何逐项变成 `taffy::style::Style`，特别是 grid 相关字段的翻译。
3. 解释 `AvailableSpace` 三态（Definite / MinContent / MaxContent）在约束传递中的作用，以及它和 `grid_cols_min_content` 这类方法的关系。
4. 排查三类常见「布局不生效」问题：`display` 没设对、`Display::None` 与可见性混淆、最小尺寸约束把元素压扁。

本讲承接 u4-l1 的三阶段生命周期：`request_layout` 阶段到底发生了什么，正是上一讲留下的钩子，本讲正面回答。

## 2. 前置知识

### 2.1 什么是 Taffy

[Taffy](https://github.com/DioxusLabs/taffy) 是一个纯 Rust 实现的布局引擎，把 CSS 规范中的三大布局算法——**flexbox（弹性盒）、CSS Grid（网格）、block（块布局）**——做成了独立库。它只做布局计算，不做绘制：你喂给它一棵带样式的节点树和一块可用空间，它吐出每个节点的位置和尺寸。

浏览器里这套算法由引擎内核（Blink/Gecko）实现；GPUI 没有内核，所以直接引入 Taffy。在 gpui 的 [Cargo.toml:93](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/Cargo.toml#L93) 中可以看到依赖被钉死为精确版本 `taffy = "=0.13.0"`——布局语义对版本极其敏感，哪怕补丁版本改变了舍入行为都可能引起 UI 像素级漂移，所以连 `^0.13` 都不允许。

### 2.2 布局即约束求解

把布局想象成一场「讨价还价」：

- 父元素告诉子元素：「我这里有这么多空间可用」（**约束，AvailableSpace**）；
- 子元素回答：「在这种约束下，我想要这么大」（**期望尺寸**）；
- 父元素汇总所有子元素的期望，敲定各自的位置（**求解**）。

flexbox 的 `flex_grow`、grid 的 `1fr`、`min_content`/`max_content` 都是这场谈判中的报价策略。理解了这一点，`AvailableSpace` 的三个变体就好懂了：

- `Definite(px)`：「空间就是这么大，直接照此报价」；
- `MinContent`：「空间不设限，但请按『不换行会溢出的最小宽度』报价」（想象一个词的宽度）；
- `MaxContent`：「空间不设限，请按『全部摊开、绝不换行』报价」。

### 2.3 承接上一讲的术语

u4-l1 讲过：元素每帧走 `request_layout → prepaint → paint` 三阶段，且 `request_layout` 阶段「只申报、不计算」。本讲揭晓「申报」去了哪里（Taffy 树）、「计算」发生在何时（根节点 prepaint 之前一次性触发）、结果如何取回（`layout_bounds`）。u3-l3 讲过 `StyleRefinement` 如何叠加成最终 `Style`，本讲追踪这个 `Style` 离开 GPUI 之后的旅程。

## 3. 本讲源码地图

| 文件 | 行数规模 | 作用 |
| --- | --- | --- |
| [src/taffy.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs) | 约 780 行 | `TaffyLayoutEngine`：对 Taffy 树的封装、Style 映射、`AvailableSpace` 定义 |
| [src/style.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs) | 约 1500 行 | `Style` 结构（含 grid 字段）、`Display`/`Overflow`/`Position` 枚举、`GridTemplate` |
| [src/styled.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/styled.rs) | 约 900 行 | 链式样式 API：`grid()`、`grid_cols()`、`col_span()` 等方法写入 `StyleRefinement` |
| [src/geometry.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/geometry.rs) | 约 3900 行 | `GridLocation`/`GridPlacement` 及其到 taffy 的转换 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs) | 约 7400 行 | `Window::request_layout`/`compute_layout`/`layout_bounds` 桥接方法、每帧建树与清空 |
| [src/element.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs) | 约 1100 行 | `Drawable` 状态机：`layout_as_root` 触发计算、prepaint 取回 bounds |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs) | 约 3400 行 | `Div::request_layout` 如何递归申报并组装 Taffy 节点 |
| [examples/grid_layout.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs) | 90 行 | 「圣杯布局」示例：grid 与 flex 双形态切换 |

## 4. 核心概念与源码讲解

### 4.1 TaffyLayoutEngine：每帧重建的布局树

#### 4.1.1 概念说明

GPUI 是立即模式框架：元素树每帧从根视图重建（u3-l1）。这带来一个关键推论——**布局树不需要跨帧存活**。Taffy 树与元素树同生共死：每帧 `request_layout` 阶段从头建一棵，帧尾整棵丢弃。

`TaffyLayoutEngine` 就是这棵短命树的持有者，它是对 `taffy::TaffyTree` 的薄封装，外加三个辅助缓存表。注意它**不是** trait——GPUI 目前只有一种布局引擎实现，直接以具体类型挂在 `Window` 上（[src/window.rs:1134](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1134) 声明字段，[src/window.rs:1821](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L1821) 在窗口构造时创建）。

#### 4.1.2 核心流程

一帧之内，布局树的完整生命周期：

```text
帧开始（draw）
  │
  ├─ 1. 建树（自底向上）
  │     根元素 request_layout
  │       └─ div：先递归每个 child.request_layout 拿到 LayoutId
  │            └─ window.request_layout(style, child_ids)
  │                 └─ TaffyLayoutEngine::request_layout
  │                      └─ style.to_taffy() 后 new_leaf / new_with_children
  │
  ├─ 2. 根节点特殊处理
  │     stretch_auto_size_to_fill(root, viewport_size)
  │     （auto 尺寸的根元素拉伸到填满窗口，模拟 web 根元素）
  │
  ├─ 3. 一次性求解
  │     prepaint_as_root → layout_as_root
  │       └─ window.compute_layout(root_id, viewport_size.into())
  │            └─ taffy.compute_layout_with_measure(...)   ← 整棵树算完
  │
  ├─ 4. prepaint 递归取结果
  │     每个元素 window.layout_bounds(id) → 递归累加绝对原点 + 像素吸附 + 缓存
  │
  └─ 5. 帧尾清空
        layout_engine.clear() —— Taffy 树与缓存全部丢弃
```

「申报」与「计算」因此是两个分离的时刻：所有元素先申报完（第 1 步），求解只发生一次（第 3 步），随后 prepaint 按需查询（第 4 步）。这正是 u4-l1 中「request_layout 只申报不计算」的准确含义。

#### 4.1.3 源码精读

先看引擎的结构与初始化：

[taffy.rs:34-56](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L34-L56) 定义了 `TaffyLayoutEngine`：核心字段是 `taffy: TaffyTree<NodeContext>`（布局树本体），其余三个成员是帧内缓存——`absolute_layout_bounds`（已换算的绝对边界）、`absolute_outer_origins`（未取整的绝对外框原点）、`computed_layouts`（本轮已计算过的子树集合）。构造函数里一句 `taffy.disable_rounding()` 值得注意：Taffy 默认会对布局结果取整，GPUI 把取整关掉、改由自己按设备像素吸附（见 4.3.3）。

[taffy.rs:58-63](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L58-L63) 的 `clear()` 把树和三张缓存表一并清空，它在每帧绘制结束时被调用（[window.rs:2902](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L2902)，紧跟在 `draw_roots` 之后）。这行代码就是「布局树每帧重建」的直接证据。

接着看申报入口。`Window` 提供的门面方法：

[window.rs:4615-4640](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4615-L4640) 是 `Window::request_layout`：接收合成好的 `Style` 与孩子们 的 `LayoutId`，取出 `rem_size` 与 `scale_factor`，转交给引擎。注意它先断言当前处于 prepaint 系阶段（`debug_assert_prepaint`），然后把孩子 id 收进复用缓冲 `cx.layout_id_buffer`，避免每次分配。

引擎侧的实现：

[taffy.rs:65-86](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L65-L86) 是 `TaffyLayoutEngine::request_layout`：第一行 `style.to_taffy(rem_size, scale_factor)` 完成样式翻译（4.2 详述）；随后分两种情况——无孩子则 `new_leaf`（叶子节点），有孩子则 `new_with_children`。后者传入的孩子切片通过 [taffy.rs:392-397](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L392-L397) 的 `to_taffy_slice` 做零拷贝转换：`LayoutId` 被声明为 `#[repr(transparent)]` 包装 `taffy::NodeId`（[taffy.rs:387-390](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L387-L390)），内存布局完全一致，直接 `transmute` 切片类型，省一次遍历拷贝。

谁在调用这条链？以 `Div` 为例：

[div.rs:1819-1853](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1819-L1853) 是 `Div::request_layout` 的实现。注意闭包内的顺序：**先**遍历 `self.children` 逐个调 `child.request_layout(window, cx)` 收集 `child_layout_ids`，**后**调 `window.request_layout(style, child_layout_ids, cx)` 为自己建节点。这个「孩子先、父亲后」的顺序就是建树自底向上的原因——为父亲挂孩子时，孩子的节点必须已经存在。

最后看引擎的另一种申报形态：

[taffy.rs:88-112](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L88-L112) 是 `request_measured_layout`：不传孩子，而是传一个 `measure` 回调，存进节点的 `NodeContext`。Taffy 在求解遇到这种「带测量函数的叶子」时会回调它，由元素自己用任意逻辑（比如文本测量、uniform_list 的行高）决定尺寸。这是文本系统能参与 flexbox/grid 布局的关键通道（u6-l5 会用到）。对应的 `Window` 门面在 [window.rs:4650-4663](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4650-L4663)。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「布局树每帧重建」与「申报顺序自底向上」。

**操作步骤**：

1. 打开 [src/taffy.rs:58-63](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L58-L63) 的 `clear()`，在函数体第一行临时加一条 `println!("taffy tree cleared");`（阅读实验，结束记得还原）。
2. 在仓库根目录运行：

   ```bash
   cargo run -p gpui --example grid_layout
   ```

3. 拖动改变窗口尺寸，观察终端输出频率。
4. 再在 [div.rs:1840](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1840) 附近的 `window.with_text_style(...)` 闭包里临时加 `println!("div requesting layout");`，重复运行。

**需要观察的现象**：窗口静止不动时终端安静；每次 resize（触发重绘）都打印一轮 `div requesting layout`（自孩子向父亲成批出现）和一次 `taffy tree cleared`。

**预期结果**：布局申报与清空严格按帧成组出现，证明 Taffy 树没有任何跨帧状态。若给某个 div 加 `.hidden()`（见 4.2.3），该分支的申报日志会随子树一起消失。

**待本地验证**：打印条数与元素数量的精确对应关系（GPUI 帧调度可能合并连续 resize）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LayoutId` 可以只靠 `repr(transparent)` + `transmute` 就转成 `taffy::NodeId` 切片？如果去掉 `repr(transparent)` 会怎样？

答案：`repr(transparent)` 保证单字段新类型与内部字段内存布局完全一致（大小、对齐都相同），所以 `&[LayoutId]` 与 `&[taffy::NodeId]` 在内存中逐字节相同，转型是安全的。去掉后 Rust 不保证布局，两个类型的大小/对齐可能不同，`transmute` 就是未定义行为。

**练习 2**：`request_measured_layout` 的 measure 回调为什么需要 `&mut Window` 和 `&mut App` 参数？这和 Taffy 是纯计算库矛盾吗？

答案：不矛盾。Taffy 只负责在合适的时机调用回调，回调是 GPUI 注入的。文本测量需要访问字体系统（挂在 `App` 上），滚动偏移、元素状态查询需要 `Window`，所以签名把两个上下文递了进来。Taffy 通过泛型 `NodeContext` 间接持有它，保持自身对 GPUI 类型零依赖。

**练习 3**：既然树每帧重建，为什么 `absolute_layout_bounds` 还要做帧内缓存（先查缓存再计算）？

答案：一帧之内同一个节点会被多次查询 bounds——`Drawable::prepaint` 查一次，子元素递归查父原点时也会查（[taffy.rs:360-371](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L360-L371) 里父节点递归本身就依赖「父原点已缓存」的断言）。缓存把递归累加 + 吸附的成本从 O(查询次数 × 树深) 降到 O(节点数)。

### 4.2 Style 映射：从 GPUI 样式到 Taffy 样式

#### 4.2.1 概念说明

GPUI 的 `Style`（u3-l3 讲过它由 `StyleRefinement` 逐层合成）是一份「面向作者的样式」：字段里既有布局属性（display、flex、grid），也有纯视觉属性（background、box_shadow、opacity）。Taffy 只关心前者。映射层做的就是一次**字段级的翻译 + 单位换算**：

- 布局字段逐项搬过去；
- 视觉字段直接丢弃；
- `Pixels` 逻辑像素按 `scale_factor` 吸附成设备像素浮点值（Taffy 内部以 f32 计算物理像素）。

理解这层映射的实际收益是排错：当你怀疑「样式写了没生效」，先问自己——这个字段根本不是布局字段（Taffy 感知不到），还是映射时被换算改变了？

#### 4.2.2 核心流程

```text
StyleRefinement（链式 API 写入的补丁）
  │  compute_style_internal 按 base/focus/hover/active 叠加（u3-l3）
  ▼
Style（全字段有确定值）
  │  to_taffy(rem_size, scale_factor)
  ▼
taffy::style::Style（只有布局字段，长度已吸附到设备像素）
  │  new_leaf / new_with_children
  ▼
TaffyTree 中的节点
```

grid 相关字段的翻译要复杂一步，因为 GPUI 把 CSS 的 `grid-template-columns` 简化成了一个「重复模板」模型：`GridTemplate { repeat, min_size }` 只能表达「N 条同样的轨道」，翻译时展开成 `repeat(N, minmax(min, 1fr))` 这样的 taffy 组件。

#### 4.2.3 源码精读

先看翻译主体：

[taffy.rs:444-526](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L444-L526) 是 `impl ToTaffy<taffy::style::Style> for Style`。结构就是一个巨大的结构体字面量：`display`、`overflow`、`position`、`size`、`margin`、`padding`、`border`、对齐四件套、`gap`、flex 五件套、grid 四件套逐字段 `into()`/`to_taffy()`。注意 `background`、`border_color`、`box_shadow`、`opacity`、`text` 等**视觉字段根本没出现**——它们不进 Taffy，留给 paint 阶段消费。结尾的 `..Default::default()` 兜住 taffy 有而 GPUI 未映射的字段。

grid 模板的展开逻辑在同文件：

[taffy.rs:457-486](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L457-L486) 的 `to_grid_repeat` 把 `GridTemplate` 翻成 taffy 的轨道组件，三种 `min_size` 对应三种 CSS 写法（注释里写得很清楚）：

| GridTemplateMinSize | 等价 CSS | 效果 |
| --- | --- | --- |
| `Zero`（默认） | `repeat(N, minmax(0, 1fr))` | 轨道可压到 0，剩余空间均分 |
| `MinContent` | `repeat(N, minmax(min-content, 1fr))` | 轨道不小于内容最小宽度 |
| `MaxContent` | `repeat(N, minmax(0, max-content))` | 轨道按内容最大宽度撑开 |

`GridTemplate` 与 `GridTemplateMinSize` 定义在 [style.rs:141-175](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L141-L175)，`Style` 上的三个 grid 字段在 [style.rs:304-313](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L304-L313)。

grid 位置（item 放在哪）走另一条小链路。链式 API 写入 `grid_location`：

[styled.rs:835-847](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/styled.rs#L835-L847) 中 `col_span(span)` 把 `grid_location.column` 设为 `Span(span)..Span(span)`，`col_span_full()` 设为 `Line(1)..Line(-1)`（CSS 语法 `1 / -1`，即从第一条线到最后一条线）。`GridLocation`/`GridPlacement` 定义在 [geometry.rs:3791-3810](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/geometry.rs#L3791-L3810)，`Line(i16)` 支持负数（从末尾倒数，与 CSS 一致）。最终由 [geometry.rs:3812-3820](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/geometry.rs#L3812-L3820) 的 `From` 实现翻译成 `taffy::GridPlacement::from_line_index` / `from_span` / `Auto`，回填到 [taffy.rs:511-522](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L511-L522) 的 `grid_row`/`grid_column` 字段。

长度字段的换算以 `AbsoluteLength` 为例：

[taffy.rs:528-532](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L528-L532) 把 rem 归一的绝对长度先 `to_pixels(rem_size)` 转成逻辑像素，再 `round_to_device_pixel` 按 scale_factor 吸附成设备像素浮点数。这是「预布局吸附」：所有作者写的绝对长度（边框、内边距、间距、显式尺寸）在进 Taffy 前就取整，从源头减少模糊边。taffy.rs [275-348 行](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L275-L348) 的大段注释详细讨论了吸附的两阶段策略与「边缘闭合 vs 平移稳定」的取舍，值得通读。边框宽度有专门的 stroke 吸附（[taffy.rs:421-438](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L421-L438)，配套单测在 [taffy.rs:750-781](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L750-L781)）。

`Display` 的语义需要特别澄清。定义在 [style.rs:1126-1141](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L1126-L1141)：

- `Flex`（默认，[style.rs:799](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L799) 中 `flex_direction` 默认 `Row`）——所以裸 div 就是「横向 flex」，这与 CSS 裸 div 是 block 不同（u3-l2 讲过 `.flex()` 是冗余调用）；
- `Grid` —— 切换到网格算法，此时 `grid_cols`/`grid_rows` 模板才生效；
- `Block` —— 块布局；
- `None` —— **不参与布局**，孩子也不会被布局（相当于 CSS `display: none`）。

对应链式 API [styled.rs:36-62](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/styled.rs#L36-L62)：`block()`/`flex()`/`grid()`/`hidden()`，其中 `hidden()` 写入的就是 `Display::None`。

`Display::None` 的另一半语义在 div 里以硬编码实现：

[div.rs:1918-1922](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1918-L1922) 与 [div.rs:1974-1978](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1974-L1978)：prepaint 和 paint 两个闭包开头都检查 `style.display == Display::None`，是则直接返回——**不仅自己不画，孩子整棵子树的 prepaint/paint 都被跳过**。这解释了一个常见困惑：「`hidden()` 之后为什么后面的兄弟元素挤上来了？」因为 Taffy 侧 `display: none` 的节点不占轨道，grid/flex 自动排布会重排，如同该元素不存在。

顺带区分 `Position`（[style.rs:1225-1247](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L1225-L1247)）：GPUI 默认 `Relative`（与 CSS 相反），`Absolute` 让元素脱流。但注释里有个重要警告：想彻底退出布局必须用 `Display::None`，`Position::Absolute` 仍会参与绝对布局计算。

最后看一个把上述机制全部用起来的示例：

[grid_layout.rs:16-61](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L16-L61) 是「圣杯布局」（header / 目录 / 正文 / 广告 / footer）示例。窗口宽于 400px 时走 grid 分支（[grid_layout.rs:50-59](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L50-L59)）：容器 `.grid().grid_cols(5).grid_rows(5)` 建 5×5 网格；header `.row_span(1).col_span_full()` 横贯全行，正文 `.col_span(3).row_span(3)` 占 3×3，广告条 `.col_span(1).row_span(3)` 占 1×3。窄于 400px 时同一组色块改走 flex 纵向堆叠（[grid_layout.rs:41-48](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L41-L48)）。两种形态的选择由 `container_query` 完成——它是一个带测量回调的元素（4.1.3 的 `request_measured_layout` 的典型用户），把容器实测宽度递给闭包。拖动窗口即可看到两套布局实时切换。

#### 4.2.4 代码实践

**实践目标**：验证「视觉字段不进 Taffy」与「Display::None 影响排布」。

**操作步骤**：

1. 运行圣杯示例：

   ```bash
   cargo run -p gpui --example grid_layout
   ```

2. 把 [grid_layout.rs:38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L38) 容器上的 `.shadow_lg()` 删掉再运行——观察布局是否变化。
3. 恢复 shadow，再给 [grid_layout.rs:57](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L57) 的广告块 `ad` 追加 `.hidden()`（即 `ad.col_span(1).row_span(3).hidden()`）再运行。
4. 把 `.hidden()` 换成 `.overflow_hidden()` 对比一次。

**需要观察的现象**：

- 步骤 2：只有投影消失，五个色块的位置尺寸纹丝不动；
- 步骤 3：广告块整个消失，footer 上移、正文右侧空出一列但网格线不变（轨道仍由 `grid_cols(5)` 决定）；
- 步骤 4：广告块还在原位，只是内容被裁剪。

**预期结果**：`box_shadow` 是视觉字段不参与 Taffy 计算；`hidden()`（Display::None）让节点退出布局、兄弟重排；`overflow_hidden()` 只改绘制行为不改布局排布。

#### 4.2.5 小练习与答案

**练习 1**：`.grid_cols(3)` 与 `.grid_cols_min_content(3)` 在什么场景下必须选后者？

答案：当网格容器可能在 `AvailableSpace::MinContent` 约束下被测量时（比如它本身是另一个 flex 容器里 `flex_none` 的孩子、或被 `container_query`/文本测量间接测量）。`Zero` 模板的轨道最小是 0，在 MinContent 约束下整行会被压扁到 0 宽；`MinContent` 模板保证轨道至少容纳内容的最小宽度。[styled.rs:760-777](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/styled.rs#L760-L777) 的文档注释明确写了这一点。

**练习 2**：为什么 GPUI 的 `GridTemplate` 只有 `repeat + min_size` 两个字段，而不支持像 CSS 那样 `grid-template-columns: 200px 1fr 2fr` 逐轨定义？

答案：这是有意的 API 简化（[style.rs:155](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L155) 注释自称 "A simplified representation"）。Zed 的实际 UI 几乎只用等分网格，逐轨模板会膨胀序列化/主题系统成本。需要非均分时，惯用做法是给个别孩子显式 `col_span`/尺寸，或嵌套多个容器。

**练习 3**：`Display::None` 的节点还会调用它孩子的 `request_layout` 吗？

答案：会（对 `Div` 而言）。`Div::request_layout`（[div.rs:1819-1853](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1819-L1853)）里没有 display 检查，孩子照常申报、Taffy 节点照常建立；跳过发生在 prepaint/paint（[div.rs:1920](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1920)、[div.rs:1976](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1976)），而 Taffy 侧 `display:none` 节点本身在求解时被忽略。所以「不显示」是布局引擎和绘制阶段双重的跳过，申报阶段并未短路。

### 4.3 LayoutId 的申请与计算流程

#### 4.3.1 概念说明

`LayoutId` 是元素世界与布局世界的握手凭证：元素在 `request_layout` 阶段拿到它，在 prepaint 阶段凭它取回边界。它是 `taffy::NodeId` 的透明包装（u3-l4 讲过 newtype 惯用法，这里是又一个实例）。

计算流程里最值得建立的心智模型是「**一次求解、按需查询**」：不是每个元素各自触发一次布局计算，而是根节点在 prepaint 前对整棵树调用一次 `compute_layout`，之后所有元素在 prepaint 里用 `layout_bounds` 查询已算好的结果。

#### 4.3.2 核心流程

```text
Window::draw
  └─ draw_roots（window.rs:3061）
       ├─ root_element.request_layout          # 建树（4.1）
       ├─ stretch_auto_size_to_fill(root, viewport)
       └─ root_element.prepaint_as_root(origin, viewport.into())
            ├─ layout_as_root（element.rs:654）
            │    └─ window.compute_layout(root_id, available_space)
            │         └─ TaffyLayoutEngine::compute_layout
            │              ├─ 失效检查：本帧已算过且子树没变 → 跳过
            │              ├─ 逻辑像素 → 物理像素（乘 scale_factor）
            │              └─ taffy.compute_layout_with_measure(...)
            │                   └─ 遇到带 measure 的节点回调 NodeContext
            └─ 递归 prepaint：每个元素 window.layout_bounds(id)
                 └─ 递归累加绝对原点 → 设备像素吸附 → 除回 scale_factor → 缓存
```

#### 4.3.3 源码精读

帧的起点在 [window.rs:3088-3098](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3088-L3098)：`draw_roots` 先对根元素 `request_layout` 建树，然后调用 `stretch_auto_size_to_fill(root_layout_id, root_size, scale_factor)`，最后 `prepaint_as_root(Point::default(), root_size.into(), ...)`——注意 `root_size.into()`，这是 `Size<Pixels>` 到 `Size<AvailableSpace>` 的转换（[taffy.rs:741-748](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L741-L748)），即视口尺寸成为根节点的 Definite 约束。

`stretch_auto_size_to_fill` 的实现在 [taffy.rs:120-142](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L120-L142)：若根节点样式的宽或高是 `auto`，就把它改写成「取整后的窗口物理尺寸」。注释说明了动机——模拟 web 上根元素撑满视口（initial containing block）的行为，显式尺寸则保留。这就是为什么你给根 div 写 `.size_full()` 不是必须的：auto 根本来就会填满窗口。

求解的触发链：`prepaint_as_root`（[element.rs:652-663](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L652-L663)）先调 `layout_as_root` 再 prepaint。`layout_as_root`（[element.rs:499-549](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L499-L549)）是 `Drawable` 状态机的一环：首次进入时调用 `window.compute_layout(layout_id, available_space, cx)`；若已处于 `LayoutComputed` 阶段且 `available_space` 没变，则跳过重算（[element.rs:526-544](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L526-L544)）——这服务于同一子树被多个父节点测量/复用的场景。`window.compute_layout`（[window.rs:4670-4681](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4670-L4681)）只是把引擎从 `Window` 里 `take` 出来再放回，规避双重借用。

引擎侧的 `compute_layout` 是本讲最核心的函数：

[taffy.rs:191-273](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L191-L273)。分三段读：

1. **失效处理**（[taffy.rs:210-224](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L210-L224)）：`computed_layouts` 是本帧已计算集合。若当前 id 首次插入成功，说明没算过，继续；若已存在（说明同帧内该子树曾被以不同可用空间计算过），就把该子树的所有缓存边界（`absolute_layout_bounds`、`absolute_outer_origins`）沿孩子链清掉，保证后续查询强制重算——**同一个元素在同一帧拿到两个不同布局时，旧结果必须作废**。
2. **单位换算**（[taffy.rs:226-238](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L226-L238)）：传入的 `available_space` 以逻辑像素表达，这里统一乘 `scale_factor` 变成物理像素再交给 Taffy（Definite 分支才乘，MinContent/MaxContent 是无量纲约束原样透传）。
3. **求解与测量回调**（[taffy.rs:240-272](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L240-L272)）：`compute_layout_with_measure` 是 Taffy 的入口。闭包里先做反向换算——Taffy 递来的 `known_dimensions`/`available_space` 是物理像素，除回 `scale_factor` 还原成逻辑像素再调用 GPUI 侧的 measure 函数（文本测量等逻辑都以逻辑像素思考）；返回值经 `snap_measured_size_to_device_pixels`（[taffy.rs:417-419](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L417-L419)，`ceil` 向上吸附）转回物理像素。没有 `NodeContext` 的节点直接返回默认尺寸。

结果查询走 `layout_bounds`：

[taffy.rs:350-384](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L350-L384)。Taffy 给出的 `layout.location` 是**相对父节点**的偏移，GPUI 需要**相对窗口**的绝对边界。算法：先查缓存；未命中则递归确保父节点的绝对外框原点已算出（`parent_origin + layout_location` 累加）；把外框两个角（origin 与 origin+size）在物理像素空间用 `round_half_toward_zero` 吸附（「后布局吸附」，保证相邻元素共享边缘闭合）；最后除以 `scale_factor` 回到逻辑像素存缓存。`Window::layout_bounds`（[window.rs:4683-4700](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L4683-L4700)）再叠加当前元素偏移（滚动偏移等）后返回给 `Drawable::prepaint`（[element.rs:364](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/element.rs#L364)），成为传给每个元素 `prepaint(bounds, ...)` 的那个 bounds——u4-l1 里「prepaint 拿到最终 Bounds」的来源就在这里。

#### 4.3.4 代码实践

**实践目标**：用 `.debug()` 描边直观看到 Taffy 算出的每格边界。

**操作步骤**：

1. 再次打开 grid_layout 示例，把 [grid_layout.rs:30](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L30) 的 header 一行改为：

   ```rust
   let header = block(gpui::white()).debug().child(format!("Header — {}", container_size.width));
   ```

   `.debug()` 来自 `Styled` trait（[styled.rs:893](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/styled.rs#L893)），在 debug 构建下给元素画红色描边，直接可视化 `layout_bounds` 的结果。
2. 重新运行，观察 header 四周的红框。
3. 拖拽窗口到 400px 以下再拖回来，红框随布局形态切换而变化。

**需要观察的现象**：红框紧贴色块外缘；grid 形态下红框横贯全宽（`col_span_full`），flex 形态下红框是全宽的窄条（`h_12`）。

**预期结果**：红框即吸附后的设备像素边界，验证「Taffy 相对坐标 → 绝对坐标 → 吸附」整条链路的产物与视觉一致。

**待本地验证**：非整数缩放（如 125%、150%）下红框是否始终落在整数物理像素上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `compute_layout` 里要把可用空间乘 `scale_factor`，measure 回调里又要除回去？

答案：Taffy 的布局精度单位是物理像素（配合设备像素吸附策略），而 GPUI 上层 API（Style、measure 回调、返回的 bounds）一律以逻辑像素表达。进 Taffy 前乘、出 Taffy 后除，Taffy 内部保持物理像素一致性，边界处完成两套单位的翻译。

**练习 2**：`stretch_auto_size_to_fill` 为什么只处理根节点？普通 div 的 `size_full()` 是怎么生效的？

答案：根节点之上没有父容器给它约束，必须有个人替它把「auto」解释成「视口大小」，这个角色由 `draw_roots` 显式承担。普通 div 的 `size_full()`（百分比尺寸）在 Taffy 求解时相对父节点解析，不需要特殊处理。

**练习 3**：`layout_bounds` 的递归要求父节点原点「should be cached」（[taffy.rs:364-367](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L364-L367)），这个前提为什么总是成立？

答案：递归入口第一行就是 `self.layout_bounds(parent_id, scale_factor)`，父节点会先于自己完成计算并写入 `absolute_outer_origins`；整条祖先链最终止于无父的根节点（`None => Point::from(layout_location)`）。prepaint 的遍历顺序（父元素 prepaint 先于孩子）也与之吻合。

### 4.4 AvailableSpace：约束如何传递

#### 4.4.1 概念说明

`AvailableSpace` 是布局约束的类型化表达，回答「这个节点可以在多大的空间里报价」。三态语义（2.2 节已述）：`Definite(Pixels)` 确定值、`MinContent` 最小内容约束、`MaxContent` 最大内容约束，默认 `MinContent`。

它出现的三个位置构成完整闭环：

1. **入口**：根节点拿到视口的 Definite 约束；
2. **传递**：Taffy 求解时沿树下发，遇到带 measure 的节点时把约束递给回调；
3. **出口**：measure 回调按约束返回尺寸（如文本在 MinContent 下按最长单词断行）。

#### 4.4.2 核心流程

```text
viewport_size: Size<Pixels>
  │  Size<Pixels> → Size<AvailableSpace>（两个维度都变 Definite）
  ▼
compute_layout(root, Definite(viewport))
  │  Taffy 树内下发：
  │    flex/grid 算法决定给每个孩子的约束
  │      （如 flex_basis 已定 → Definite；测最小宽度 → MinContent）
  ▼
NodeContext.measure(known_dimensions, available_space, window, cx)
  │  元素按约束返回 Size<Pixels>（逻辑像素）
  ▼
snap_measured_size_to_device_pixels → 物理像素 → 继续求解
```

#### 4.4.3 源码精读

定义与转换集中在一个小节里：

[taffy.rs:681-691](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L681-L691) 是 `AvailableSpace` 枚举本体——注意它定义在 gpui 而非引用 taffy 的类型，与 `Display`/`Overflow` 等一样是「taffy 类型的本地镜像」（为了派生 `JsonSchema`/序列化并控制公开 API 表面）。[taffy.rs:715-733](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L715-L733) 的两个 `From` 实现完成与 `taffy::style::AvailableSpace` 的互转（`Definite` 里拆出 `f32` 装回 `Pixels`）。[taffy.rs:707-713](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L707-L713) 的 `min_size()` 是便捷构造，两维都是 `MinContent`——活跃拖拽（active drag）的元素就用它布局（[window.rs:3121](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3121)）：拖拽预览不应受窗口尺寸约束，按内容自适应。[taffy.rs:735-748](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L735-L748) 提供 `From<Pixels>` 与 `From<Size<Pixels>>`，让 `root_size.into()` 这样的调用自然发生。

约束与 grid 模板的联动在 4.2 的表格里已经埋了伏笔，这里点透：当带 `grid_cols(3)`（Zero 模板）的容器在 `MinContent` 约束下被测量时，`minmax(0, 1fr)` 允许轨道收缩到 0，整个网格报价宽度为 0，内容被压没；换成 `grid_cols_min_content(3)` 则轨道下限变为 min-content，容器报价至少等于最窄可行布局。`styled.rs` 里 [760-768 行](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/styled.rs#L760-L768) 与 [788-796 行](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/styled.rs#L788-L796) 的文档注释直接说明了这个差异。

约束还通过另一条隐蔽路径影响布局——`Overflow` 的「自动最小尺寸」效应（[style.rs:1193-1223](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L1193-L1223)）：flex/grid 项的**自动最小尺寸**默认基于内容（`Visible`/`Clip`），而 `Hidden`/`Scroll` 下为 0。这就是「给长文本容器加 `overflow_hidden` 后它终于肯收缩了」的原因——不是裁剪改变了布局，是自动最小尺寸的规则变了。

#### 4.4.4 代码实践

**实践目标**：观察「同样内容，不同约束，不同报价」。

**操作步骤**：

1. 阅读 [grid_layout.rs:18](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L18) 的 `container_query` 用法：闭包签名 `(container_size, _window, _cx)` 里的 `container_size` 正是测量回调在各种约束下算出的尺寸。
2. 把 [grid_layout.rs:40](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs#L40) 的阈值 `px(400.)` 改成 `px(1000.)`，运行。
3. 再改成 `px(100.)`，运行。
4. 运行时不拖窗口，直接观察初始 500×500 窗口下走哪条分支。

**需要观察的现象**：阈值 1000 时 500px 宽的容器显示为纵向堆叠（flex 分支）；阈值 100 时显示为三栏网格（grid 分支）。

**预期结果**：`container_size.width` 是测量阶段按约束算出的实际宽度（约等于视口宽减内边距），与阈值比较后决定布局形态。`container_query` 本质就是把 `request_measured_layout` 的回调结果暴露给业务代码。

#### 4.4.5 小练习与答案

**练习 1**：`AvailableSpace::min_size()` 为什么适合拖拽预览元素？

答案：拖拽预览浮在所有内容之上，不应被视口或父容器的 Definite 空间约束（否则拖到窗口边缘会被挤压），MinContent 让它按内容最小需求自适应尺寸，同时语义上比 MaxContent 更克制（不会因为长文本无限展开）。

**练习 2**：一个 `flex_grow(1)` 的子元素在 `Definite(500px)` 约束下的最终宽度由什么决定？

答案：由 Taffy 的 flex 算法在求解时决定：父节点扣除自身 padding/border/gap 及其他孩子的 flex_basis 与固定尺寸后，把剩余空间按 `flex_grow` 权重分给它。`flex_grow` 是「参与分配的权重」，不是最终尺寸；约束（500px）是分配的总盘子。

**练习 3**：为什么 `AvailableSpace` 定义在 gpui 而不直接复用 `taffy::style::AvailableSpace`？

答案：与 `Display`、`Overflow`、`Position` 同理（style.rs 中这些枚举注释均标明 "Copy of taffy::style type of the same name"）：gpui 的公开 API 需要派生 `Serialize`/`Deserialize`/`JsonSchema`（供主题与设置系统使用），并且不希望 taffy 类型泄漏到公共 API 表面——将来更换或升级布局引擎时上层代码不受影响。

## 5. 综合实践

把本讲三个模块串起来：**用 grid 实现 3×3 九宫格，一格跨两列，再隐藏一格观察 Taffy 重排**。

**步骤 1：新建示例文件**。在 `examples/` 下新建 `grid_nine.rs`（示例代码，仿照 [grid_layout.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/grid_layout.rs) 的三段式骨架）：

```rust
// examples/grid_nine.rs（示例代码）
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    App, Bounds, Context, Window, WindowBounds, WindowOptions, div, prelude::*, px, rgb, size,
};
use gpui_platform::application;

struct GridNine {}

impl Render for GridNine {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let cell = |i: usize| {
            div()
                .flex()
                .items_center()
                .justify_center()
                .bg(rgb(0x4a4a4a))
                .border_1()
                .border_color(gpui::white())
                .text_color(gpui::white())
                .child(format!("{i}"))
        };

        div()
            .size_full()
            .p_4()
            .grid()          // Display::Grid：切换到网格算法
            .grid_cols(3)    // 3 条 1fr 轨道
            .grid_rows(3)
            .gap_2()
            .child(cell(1).col_span(2))   // 1 号格跨两列
            .child(cell(2))
            .child(cell(3))
            .child(cell(4))
            .child(cell(5))
            .child(cell(6))
            .child(cell(7))
            .child(cell(8))
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(400.), px(400.0)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |_, cx| cx.new(|_| GridNine {}),
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

共 8 个可见格子：1 号跨两列占第一行前两轨，2 号补第一行；第二行 3/4/5；第三行 6/7/8，恰好填满 3×3。

**步骤 2：运行**。在仓库根目录执行：

```bash
cargo run -p gpui --example grid_nine
```

若提示找不到 target，说明自动发现未覆盖新文件，仿照 [Cargo.toml:177-179](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/Cargo.toml#L177-L179) 的 `[[example]]` 段补一段 `name = "grid_nine"` / `path = "examples/grid_nine.rs"`。

**步骤 3：观察基准布局**。1 号格宽度约是其他格的两倍（减去 gap 差）；每格边界落在整数像素上（`.debug()` 可加在 cell 闭包里验证）。

**步骤 4：隐藏一格**。把 8 号格改为 `cell(8).hidden()`，重新运行。

**观察与思考**：

- 8 号格彻底消失（div 跳过其 prepaint/paint，[div.rs:1920](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1920)、[div.rs:1976](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1976)）；
- 剩余 7 格自动重排（grid 自动放置算法忽略 `display:none` 的项），第三行只剩 6/7 两格，右侧空出一轨——轨道本身仍由 `grid_cols(3)` 的模板决定，空轨道不会塌缩；
- 若想让第三行右侧被填满而不是留空，可给 7 号格追加 `.col_span(2)` 验证 span 与隐藏的组合行为。

**步骤 5：对照源码复盘**。回到 4.1.2 的流程图，指出这 8 个格子分别在哪一步进入系统：`render()` 里的 `.grid().grid_cols(3)` 经 `compute_style_internal` 合成进 `Style`（u3-l3）；`Div::request_layout` 自底向上申报（[div.rs:1819-1853](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/div.rs#L1819-L1853)）；`to_taffy` 把 `GridTemplate { repeat: 3, min_size: Zero }` 展开成 `repeat(3, minmax(0, 1fr))`（[taffy.rs:457-486](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L457-L486)）；`col_span(2)` 经 `GridPlacement::from_span` 进入 taffy（[geometry.rs:3812-3820](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/geometry.rs#L3812-L3820)）；窗口尺寸经 `root_size.into()` 变成 Definite 约束（[window.rs:3098](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L3098)）；帧尾 `layout_engine.clear()` 清场（[window.rs:2902](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/window.rs#L2902)）。

## 6. 本讲小结

- GPUI 不自己实现布局，而是钉死精确版本引入 Taffy（`=0.13.0`），`TaffyLayoutEngine` 是它的封装；布局树与元素树同生共死，每帧 `request_layout` 阶段自底向上重建、帧尾 `clear()` 整棵丢弃。
- `Style::to_taffy` 是字段级翻译：布局字段逐项搬移并按 `scale_factor` 做设备像素吸附，视觉字段（background、shadow、opacity）根本不进 Taffy；`GridTemplate { repeat, min_size }` 被展开为 `repeat(N, minmax(min, 1fr))` 型轨道模板。
- 布局是「一次求解、按需查询」：根节点在 prepaint 前经 `stretch_auto_size_to_fill` + `compute_layout` 对整棵树算一次，各元素 prepaint 时用 `layout_bounds` 取回递归累加并吸附后的绝对边界。
- `AvailableSpace` 三态（Definite/MinContent/MaxContent）描述约束；根节点拿到视口的 Definite，measure 回调按约束报价；`grid_cols` 的 Zero 模板在 MinContent 约束下会塌缩到 0，需要 `grid_cols_min_content` 兜底。
- 排错三件套：裸 div 默认 `Display::Flex`（不是 block）；`hidden()`（Display::None）让节点退出布局且兄弟重排、孩子不绘制；`overflow_hidden/scoll` 会把 flex/grid 项的自动最小尺寸变为 0，允许元素收缩。
- 单位纪律：GPUI 上层一律逻辑像素，Taffy 内部一律物理像素，进出边界处乘除 `scale_factor`，配合预布局与后布局两阶段吸附保证边缘锐利。

## 7. 下一步学习建议

布局算完只是准备工作，下一讲 [u4-l3 窗口绘制管线：从 refresh 到 draw](u4-l3-window-draw-pipeline.md) 将追踪 `draw_roots` 之后的故事：元素 arena 如何承接每帧重建、`cx.notify()` 如何调度下一帧、`refresh` 与常规重绘的区别。届时本讲的 `draw_roots` 入口会成为整条管线的起点。

想继续在布局方向深挖的读者可以：

- 通读 [taffy.rs:275-348](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/taffy.rs#L275-L348) 的像素吸附注释，理解「边缘闭合 vs 平移稳定」这对矛盾的设计取舍；
- 对照 CSS 规范的 flexbox 与 grid 章节，验证 Taffy 行为与浏览器的一致性边界；
- 预习 [src/elements/uniform_list.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/uniform_list.rs)（u6-l1），看虚拟化列表如何用 `request_measured_layout` 反向控制自己的滚动尺寸。
