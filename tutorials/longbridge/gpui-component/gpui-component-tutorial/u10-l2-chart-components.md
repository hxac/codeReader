# Chart 图表组件

> 本讲对应 HEAD：`be4c5d30`。本次相对上一版（`a0ae3a37`）的增量是两个小范围修复，正好落在图表的「鼠标悬停交互」上：水平柱状图悬停高亮条带溢出值轴（PR #2513）、虚线十字线颜色过淡（PR #2513 / #2507）。本讲会在讲解柱状图与十字线时专门拆解这两处。

## 1. 本讲目标

学完本讲你应该能够：

- 说出 `chart` 模块下五类开箱即用图表（折线 / 柱状 / 面积 / 饼图 / K 线）的定位与最小用法。
- 解释所有图表都只是实现了同一个 `Plot` trait 的「绘制器」，并通过 `IntoPlot` 派生宏自动获得 `IntoElement` 能力与「鼠标悬停 tooltip」交互。
- 独立为一张图表配置坐标轴、网格、系列名与悬浮提示。
- 讲清楚两个最新修复：水平柱状图的悬停高亮条带为什么必须用 `h_span` 而不是 `span`；虚线十字线的颜色为什么从 `border` 改成 `border.mix(foreground, 0.8)`。

## 2. 前置知识

本讲是「图表与二次开发扩展」单元的第二篇，默认你已学完 [u10-l1 Plot 绘图系统与 IntoPlot 派生宏](u10-l1-plot-and-into-plot.md)，理解 `Plot` trait、`scale`（`ScaleBand` / `ScalePoint` / `ScaleLinear`）、`shape`（`Bar` / `Line` / `Area` / `Arc` / `Pie`）这套底层积木。本讲不再重复这些底层原理，而是站在它们之上，看 `chart` 模块如何把它们组装成「拿来就能用」的成品图表。

此外请回顾两个公共基础：

- **主题色 `cx.theme()`**（见 [u2-l1](u2-l1-theme-system.md)）：图表的轴线、网格、默认系列色 `chart_1` … `chart_5`、K 线涨跌色 `chart_bullish` / `chart_bearish` 全部取自主题。
- **`Colorize` trait 的颜色运算**（见 [u2-l1](u2-l1-theme-system.md)）：本讲会用到的 `opacity(alpha)`（叠加透明度）和 `mix(other, factor)`（在 HSL 空间按权重混合两色）都来自它。

几个本讲会反复用到的术语，先一句话定义：

| 术语 | 含义 |
| --- | --- |
| **band 轴（带状轴）** | 把每个类别放进一个等宽「带」里，柱状图 / K 线用它做分类轴。 |
| **point 轴（点轴）** | 把每个数据点放在等距位置上，折线 / 面积图用它做 x 轴。 |
| **value 轴（数值轴）** | 映射数值大小的轴，柱状图里垂直方向（默认）或水平方向（Left/Right 对齐时）。 |
| **十字线 crosshair** | 鼠标悬停时穿过数据点的参考线，分虚线「发丝」与实色「高亮条带」两种形态。 |

## 3. 本讲源码地图

本讲涉及的源码集中在两个目录：

| 文件 | 作用 |
| --- | --- |
| [crates/ui/src/chart/mod.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/mod.rs) | `chart` 模块入口：声明五个子模块、导出五个图表类型，并提供两个公用的坐标轴标签构造助手。 |
| [crates/ui/src/chart/line_chart.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/line_chart.rs) | 折线图 `LineChart`。 |
| [crates/ui/src/chart/area_chart.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/area_chart.rs) | 面积图 `AreaChart`（支持多系列堆叠）。 |
| [crates/ui/src/chart/bar_chart.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/bar_chart.rs) | 柱状图 `BarChart`，本讲的「修复主角」之一。 |
| [crates/ui/src/chart/pie_chart.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/pie_chart.rs) | 饼图 `PieChart`（含环形、引线标签）。 |
| [crates/ui/src/chart/candlestick_chart.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/candlestick_chart.rs) | K 线图 `CandlestickChart`。 |
| [crates/ui/src/plot/tooltip.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs) | 悬浮提示 `Tooltip` 与十字线 `CrossLine`，本讲另一处「修复主角」。 |
| [crates/ui/src/plot/mod.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/mod.rs) | `Plot` trait 定义，所有图表都实现它。 |
| [crates/macros/src/derive_into_plot.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/macros/src/derive_into_plot.rs) | `#[derive(IntoPlot)]` 派生宏，是图表「变成可交互元素」的关键。 |
| [crates/story/src/stories/chart_story/chart_story.rs](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/chart_story/chart_story.rs) | Story Gallery 里的「Chart」演示页，本讲实践的主要观察对象。 |

---

## 4. 核心概念与源码讲解

### 4.1 chart 模块总览：五类图表如何复用底层 Plot 系统

#### 4.1.1 概念说明

底层 `plot` 模块提供的是「零件」（坐标轴 `PlotAxis`、网格 `Grid`、比例尺 `scale`、形状 `shape`）。`chart` 模块则把这些零件按「一类图表」组装成成品，提供五类：

- **`LineChart`** 折线图 —— point 轴 + 线形 `Line`。
- **`AreaChart`** 面积图 —— point 轴 + 面形 `Area`，支持多系列堆叠。
- **`BarChart`** 柱状图 —— band 轴 + 柱形 `Bar`，可竖可横。
- **`PieChart`** 饼图 —— 角度比例 + 扇形 `Arc` / `Pie`。
- **`CandlestickChart`** K 线图 —— band 轴 + 自绘 OHLC 矩形与影线。

它们之间最关键的共同点是：**都实现同一个 `Plot` trait**，并且都标注了 `#[derive(IntoPlot)]`。`Plot` trait 只规定「怎么画」（`paint`）与「悬停时显示什么」（`id` / `tooltip_state` / `tooltip`），完全不关心交互事件本身；交互事件由派生宏生成的 `Element` 实现统一接管。这就让「加一张新图表」=「写一个实现 `Plot` 的绘制器」，而不必重写任何鼠标处理逻辑。

#### 4.1.2 核心流程

一张图表从被创建到可交互，经过这样几步：

1. **构造**：`BarChart::new(data).band(..).value(..)` 等 builder 链设置数据映射与外观。
2. **派生宏接管**：`#[derive(IntoPlot)]` 生成 `IntoElement` + `Element` 两个实现，使图表对象本身就是一个 GPUI 元素。
3. **布局**：生成的 `request_layout` 让图表占据父容器全部空间（`Size::full()`）。
4. **绘制**：`Element::paint` 调用 `Plot::paint` 画出轴线、网格与图形。
5. **交互（可选）**：若 `Plot::id()` 返回 `Some`（即你调用了 `.id(..)`），生成的 `Element` 还会注册鼠标移动监听，把光标位置存进元素局部状态，下一帧由 `tooltip_state` 命中数据点、`tooltip` 渲染浮层。

关键在于：**「要不要交互」完全由 `.id(..)` 一个方法决定**。不调用 `.id`，图表就是纯静态绘制，和改造前完全一样。

#### 4.1.3 源码精读

模块入口导出五类图表，并定义两个公用的坐标轴标签助手：

[crates/ui/src/chart/mod.rs:1-11](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/mod.rs#L1-L11) —— 声明五个子模块并再导出五个图表类型。

[crates/ui/src/chart/mod.rs:61-85](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/mod.rs#L61-L85) —— `build_band_labels`：为 band 轴（柱状图、K 线）生成居中的分类标签，tick 坐标取每个 band 的中心。

`Plot` trait 只规定四个方法，`id` 默认返回 `None`（关闭交互）：

[crates/ui/src/plot/mod.rs:23-68](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/mod.rs#L23-L68) —— `Plot` trait：`paint`（必填）、`id` / `tooltip_state` / `tooltip`（默认实现都关闭 tooltip）。

真正把「实现 Plot 的图表」变成「可交互 GPUI 元素」的是派生宏。以 `BarChart` 为例，它标注了 `#[derive(IntoPlot)]`：

[crates/ui/src/chart/bar_chart.rs:23-45](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/bar_chart.rs#L23-L45) —— `BarChart` 结构体与 `#[derive(IntoPlot)]`。

派生宏生成的 `Element` 实现是理解「悬停 tooltip 如何驱动」的钥匙：

[crates/macros/src/derive_into_plot.rs:41-45](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/macros/src/derive_into_plot.rs#L41-L45) —— `Element::id` 直接委托给 `Plot::id`，所以 `.id(..)` 是开启交互的唯一开关。

[crates/macros/src/derive_into_plot.rs:66-94](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/macros/src/derive_into_plot.rs#L66-L94) —— `prepaint`：从元素局部状态读出上一帧记录的光标位置，依次调用 `tooltip_state`（命中哪个数据点）与 `tooltip`（构造浮层），并把浮层 `deferred` 延迟绘制，使其盖在图表后续兄弟元素（如卡片底栏文字）之上。

[crates/macros/src/derive_into_plot.rs:108-128](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/macros/src/derive_into_plot.rs#L108-L128) —— `paint`：先画图表本体，再注册一个 `MouseMoveEvent` 处理器，把「光标相对图表原点的位置」写入元素局部状态，发生变化就 `window.refresh()` 触发下一帧。注意处理器只捕获 `Copy` 的 `bounds` 与状态 cell，不碰 `self`，以满足 `'static` 约束。

#### 4.1.4 代码实践

**目标**：验证「`.id(..)` 是交互开关」。

**步骤**：

1. 运行 Story Gallery（`cargo run`），在左侧找到「Chart」页面（也可用 `cargo run -- chart` 直接聚焦，名称按小写包含匹配）。
2. 在该页找到带 tooltip 的折线图（标题 `Line Chart - Tooltip`）与不带 tooltip 的（如 `Line Chart - Linear`）。
3. 分别把鼠标移到两条线上。

**观察**：

- 带 `.id` 的图，鼠标悬停会出现十字线 + 数据点圆点 + 浮动数值框。
- 不带 `.id` 的图，悬停毫无反应。

**预期结果**：交互与否完全由是否调用 `.id(..)` 决定，验证了 4.1.3 的派生宏逻辑。运行结果请以本地为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么把图表的鼠标移动监听写在派生宏里，而不是每个图表各自实现？

**参考答案**：因为所有图表的交互逻辑完全相同（记录光标 → 命中 → 渲染浮层），只有「命中哪个点」「浮层显示什么」因图而异。把公共的事件循环抽到宏里，让每个图表只需实现 `Plot` 的 `tooltip_state` / `tooltip` 两个纯函数，避免重复代码，也保证所有图表交互行为一致。

**练习 2**：`Plot::id` 默认返回 `None` 有什么好处？

**参考答案**：让「静态图表」与「可交互图表」共用同一套类型，调用方不加 `.id(..)` 时行为与改造前完全一致（零成本、零意外），是典型的「能力 opt-in」设计。

---

### 4.2 折线图 LineChart 与面积图 AreaChart

#### 4.2.1 概念说明

`LineChart` 与 `AreaChart` 都基于 **point 轴**（数据点等距排列在 x 方向），区别只在最终画的是线还是面。`AreaChart` 额外支持**多系列**：可以连续 `.y(..)` 多次，每次配一个 `.stroke(..)` / `.fill(..)` / `.name(..)`，悬停时会同时显示每个系列的数值与圆点。

两者的悬停十字线都是**虚线发丝**形态（见 4.5），因此本次「十字线颜色修复」主要影响的就是这两类图。

#### 4.2.2 核心流程

折线图绘制流程：

1. 用 `scales(bounds)` 同时构造 x 的 `ScalePoint` 与 y 的 `ScaleLinear`（y 域会链入一个零点，保证从 0 起算）。
2. 画 x 轴（`PlotAxis`）与水平网格（`Grid`，虚线 `[4,2]`）。
3. 用底层 `Line` 形状按 `stroke_style`（Natural 自然曲线 / Linear 折线 / StepAfter 阶梯）连线，可选带数据点圆点。

悬停流程：

1. `tooltip_state`：忽略 x 轴标签槽位（避免悬停标签也弹提示），用 `x.least_index` 找到最近的数据点，返回十字线点 + 一个数据点圆点。
2. `tooltip`：构造 `Tooltip`，十字线用 `CrossLine::new(..).height(..)` 限定在绘图区内（不穿过 x 轴），圆点用系列色填充、背景色描边。

#### 4.2.3 源码精读

[crates/ui/src/chart/line_chart.rs:160-216](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/line_chart.rs#L160-L216) —— `LineChart::paint`：构造比例尺 → 画轴与网格 → 用 `Line` 连线，默认系列色取 `cx.theme().chart_2`。

[crates/ui/src/chart/line_chart.rs:249-284](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/line_chart.rs#L249-L284) —— `LineChart::tooltip`：注意第 269-273 行的 `CrossLine::new(state.cross_line).height(..)`，没有调用 `.band(..)`，因此十字线保持默认的**虚线发丝**形态——这正是 4.5 会讲到的颜色修复所作用的分支。

面积图的多系列 tooltip 类似，只是每个系列各生成一行与一个圆点：

[crates/ui/src/chart/area_chart.rs:270-311](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/area_chart.rs#L270-L311) —— `AreaChart::tooltip`：为每个 `y` 系列追加一行（色块 + 名称 + 数值）与一个数据点圆点。

#### 4.2.4 代码实践

**目标**：对比三种描边风格与「带点 / 不带点」。

**步骤**：

1. 打开 Story「Chart」页，找到 `Line Chart - Tooltip`、`Line Chart - Linear`、`Line Chart - Step After`、`Line Chart - Dots` 四张。
2. 观察曲线形态差异：Natural 平滑、Linear 折线、StepAfter 阶梯；Dots 多了数据点圆点。
3. 鼠标悬停 `Line Chart - Tooltip`，观察十字线颜色（应为一条比网格线更明显、但比正文浅的灰色——这是 4.5 修复后的效果）。

**预期结果**：四种风格正确呈现；悬停时十字线清晰可辨。运行结果请以本地为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习**：`LineChart` 的 y 比例尺为什么要在数据域里 `chain(Some(Y::zero()))`？

**参考答案**：强制把 0 纳入值域，使 y 轴始终从 0 起，避免数据全为正时折线「悬浮」在半空、夸大波动幅度——这是数据可视化的常见正确做法。

---

### 4.3 柱状图 BarChart（含水平条带 `h_span` 修复）

#### 4.3.1 概念说明

`BarChart` 基于 **band 轴**：每个类别占一个等宽「带」，柱子落在带内。它的最大特色是方向可变——通过 `BarAlignment` 决定柱子朝哪边长：

- `Bottom`（默认）/ `Top`：**竖向**柱，band 轴在水平方向，value 轴在垂直方向。
- `Left` / `Right`：**横向**柱（条形图），band 轴在垂直方向，value 轴在水平方向。

[crates/ui/src/plot/shape/bar.rs:13-28](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/shape/bar.rs#L13-L28) —— `BarAlignment` 枚举与 `is_horizontal`（`Left` / `Right` 为水平）。

悬停时柱状图不用「发丝十字线」，而是用一条**与柱子等宽的半透明高亮条带**（`.band(..)`），更醒目地圈出当前柱。

#### 4.3.2 核心流程

绘制流程（关键在「方向感知」）：

1. `band_scale`：band 轴跨度随方向变化——水平柱时跨高度，竖向柱时跨宽度。
2. 计算 value 轴范围：竖向柱用固定 `AXIS_GAP` 留白；**水平柱时类别名可能很长，需用 `horizontal_gaps` 实测最长标签宽度**来精确留白。
3. 用 `ScaleLinear` 把数值映射到像素，调用底层 `Bar` 形状画柱（支持纯色 `fill` 与 `fill_gradient` 渐变，渐变会按 `BarAlignment` 自动定角度）。

悬停流程：

1. `tooltip_state`：用 band 轴的 `least_index` 命中鼠标所在的那根柱，返回柱中心的十字线点。
2. `tooltip`：把高亮条带限制在**绘图区**内，跳过悬停在坐标轴标签上的情况。

#### 4.3.3 源码精读（重点：水平条带的 `h_span` 修复）

柱状图的 tooltip 根据方向走两个分支：

[crates/ui/src/chart/bar_chart.rs:505-564](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/bar_chart.rs#L505-L564) —— `BarChart::tooltip` 全貌。

**水平柱分支（本次修复点）**：

[crates/ui/src/chart/bar_chart.rs:522-537](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/bar_chart.rs#L522-L537) —— 水平柱的高亮条带，关键在第 536 行的 `.h_span(start, length)`。

要理解这一处修复，必须先看 `CrossLine` 的两个 span 方法——它们分别约束**不同方向**的线：

[crates/ui/src/plot/tooltip.rs:93-103](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs#L93-L103) —— `span(start, length)` 写入的是 `self.vertical`（约束**竖向**线的 y 跨度）；`h_span(start, length)` 写入的是 `self.horizontal`（约束**横向**线的 x 跨度）。

水平柱图的十字线被设为 `.horizontal()`（一条横线），渲染时读的是 `self.horizontal` 字段。**修复前**用的是 `.span(start, length)`，它写入的是 `vertical` 字段，对一条横线毫无作用——横线的 `horizontal` 仍是默认的 `(0, None)`，即「从左到右铺满整宽」。结果就是高亮条带一路延伸到分类名标签所在的值轴区域之外，看起来「溢出」了图表的数值绘图区。

**修复后**改用 `.h_span(start, length)`，正确地把横线约束在 `[start, start+length]`。这里的 `start` 与 `length` 由 `horizontal_gaps` 实测得到，正好扣除 band 标签区（`band_gap`）与数值末端标签区（`value_end_gap`），让条带精确停留在数值绘图区内。

竖向柱分支则一直正确地用 `.span`（约束竖线），无需改动：

[crates/ui/src/chart/bar_chart.rs:538-553](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/bar_chart.rs#L538-L553) —— 竖向柱的高亮条带用 `.span(start, length)` 约束竖线，正确。

`horizontal_gaps` 是修复能成立的测量基础（条带长度依赖它）：

[crates/ui/src/chart/bar_chart.rs:243-270](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/bar_chart.rs#L243-L270) —— `horizontal_gaps`：实测最长分类名宽度与最长数值标签宽度，返回 `(band_gap, value_end_gap)`，供绘制与 tooltip 共享，保证柱子与高亮条带对齐。

#### 4.3.4 代码实践

**目标**：观察水平柱状图悬停条带被正确限制在数值绘图区内。

**步骤**：

1. 打开 Story「Chart」页，找到 `Bar Chart - Left aligned` 与 `Bar Chart - Right aligned` 两张水平柱状图（源码见 [chart_story.rs:303-326](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/chart_story/chart_story.rs#L303-L326)）。
2. 鼠标悬停某一根水平柱，观察高亮条带的左右边界。

**观察 / 预期结果**：

- 修复后：高亮条带**只覆盖数值绘图区**（从分类名右侧到数值末端），不会压到左侧的分类名标签，也不会越过数值标签。
- （若想对照「修复前」，可在本地 `git checkout a0ae3a37 -- crates/ui/src/chart/bar_chart.rs` 临时编译，会看到条带横穿到分类名上方——观察完务必 `git checkout` 还原，不要提交。）

运行结果请以本地为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么水平柱的 value 轴留白要用 `horizontal_gaps` 实测，而竖向柱能用固定常量？

**参考答案**：竖向柱的分类名沿 x 轴水平排列、可换行，固定 `AXIS_GAP` 足够；水平柱的分类名沿垂直的 value 轴排列、宽度可任意长（如很长的地名），必须实测最长标签宽度才能精确留白，否则要么浪费空间、要么标签与柱子重叠。

**练习 2**：把第 536 行的 `.h_span` 故意改回 `.span`，水平柱悬停时会出现什么？为什么？

**参考答案**：高亮条带会重新横穿整张图、盖到分类名标签上。因为 `.span` 改的是 `vertical` 字段，而横线读的是 `horizontal` 字段，约束没生效，横线回退到「铺满整宽」。

---

### 4.4 饼图 PieChart 与 K 线 CandlestickChart

#### 4.4.1 概念说明

这两类图表都不用「数值坐标轴」，因此与折线/柱状差异较大：

- **`PieChart`**：按各扇区 `value` 占比分配角度，用底层 `Arc` / `Pie` 画扇形。支持内半径（环形 `donut`）、外半径函数（可让每片大小递减）、间隔角 `pad_angle`，以及环外的「引线 + 文字」标签。
- **`CandlestickChart`**：K 线，每个数据点有 OHLC（开高低收）四个值。先画一根从 high 到 low 的影线，再画一个 open 到 close 的矩形实体；涨（收 > 开）用 `chart_bullish`（默认 `green-600`），跌用 `chart_bearish`（默认 `red-600`）。

注意：**这两类目前都没有实现 `tooltip_state` / `tooltip`**，即不覆盖 `Plot` trait 的默认实现，因此默认不可交互（无悬停提示）。

#### 4.4.2 核心流程

饼图标签防重叠是它的难点：扇区在环边较密时，相邻标签会撞在一起。`pie_chart.rs` 用三遍扫描解决——

1. 第一遍：按「环边中心在左还是右」把标签分两组，记录每个标签的目标 y。
2. 第二遍：`spread_labels` 做双向松弛——自上而下把挤在一起的标签往下推，再自下而上（锚定底边）把溢出的往上拉，保证相邻标签至少隔一个字高，并钳制在 `[top, bottom]`。
3. 第三遍：先画引线（环边 → 标签锚点 → 水平拉到 ±label_radius），再画文字。

#### 4.4.3 源码精读

[crates/ui/src/chart/pie_chart.rs:152-276](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/pie_chart.rs#L152-L276) —— `PieChart::paint`：画扇区，再画带防重叠的引线标签。

[crates/ui/src/chart/pie_chart.rs:297-329](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/pie_chart.rs#L297-L329) —— `spread_labels`：双向松弛算法，解决级联重叠。

[crates/ui/src/chart/candlestick_chart.rs:166-236](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/candlestick_chart.rs#L166-L236) —— K 线逐点绘制：影线用 `PathBuilder` 画直线，实体用 `fill(quad)` 画矩形。

[crates/ui/src/chart/candlestick_chart.rs:198-204](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/chart/candlestick_chart.rs#L198-L204) —— 涨跌上色：`close > open` 取 `chart_bullish`，否则 `chart_bearish`。这两个语义色在主题里默认是 `green-600` / `red-600`（见 [default-theme.json:26-27](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/default-theme.json#L26-L27)）。

#### 4.4.4 代码实践

**目标**：通过阅读理解饼图标签防重叠，并观察 K 线涨跌色。

**步骤**：

1. 打开 Story「Chart」页，找到 `Pie Chart - Label`（带引线标签）与几张 `Candlestick Chart`。
2. 对饼图：把窗口拉窄，观察小角度扇区的标签是否仍互不重叠（验证 `spread_labels`）。
3. 对 K 线：确认收高于开的实体为绿、收低于开的为红。

**预期结果**：饼图标签在挤压下仍保持至少一字高间距且不出界；K 线涨绿跌红。运行结果请以本地为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习**：`spread_labels` 为什么需要「自上而下」与「自下而上」两遍，而不是一遍？

**参考答案**：单遍只能处理「前一个挤后一个」的局部重叠；当多个标签同时被往下推时，可能把最底下的标签顶出边界，引发级联溢出。先自上而下消除重叠，再自下而上（锚定 `bottom`）把溢出者逐个拉回，才能同时满足「互不重叠」与「不超界」。

---

### 4.5 悬浮提示与十字线：Tooltip / CrossLine（含十字线颜色 `mix` 修复）

#### 4.5.1 概念说明

`Tooltip` 是图表悬停浮层的统一容器，由两部分组成：

- **`CrossLine`**：穿过数据点的参考线。有两种形态——默认是 1px **虚线发丝**（dashed）；调用 `.band(thickness)` 后变成一条 `thickness` 宽的**半透明实色条带**。它还能用 `.span` / `.h_span` 把线限制在绘图区内，避免穿过坐标轴。
- **信息框**：跟随鼠标的圆角浮层，含标题行与若干「色块 + 名称 + 数值」行，靠近边缘时会自动翻向中心以免溢出。

柱状图用 `.band`（实色条带）+ `h_span/span`（限制范围）；折线 / 面积图用默认虚线发丝 + `height`（限制高度）。**本次颜色修复只影响虚线发丝形态**，即折线 / 面积图。

#### 4.5.2 核心流程

十字线的颜色与形态在 `CrossLine::line` 里决定：

- **虚线发丝**：用 0 宽度条带 + 1px 虚线边框画出，颜色取修复后的 `border.mix(foreground, 0.8)`。
- **实色条带**：用 `thickness` 宽填充，颜色取 `foreground.opacity(0.08)`（很淡的前景色），这条分支未改动。

信息框则由 `Tooltip::render` 布局：内容（标题 + 行）优先于自由子元素；框体绝对定位、跟随光标，并在靠近四条边时翻向中心。

#### 4.5.3 源码精读（重点：十字线颜色的 `mix` 修复）

先看 `CrossLine` 的结构，注意它把竖向、横向两条线的跨度分开存储：

[crates/ui/src/plot/tooltip.rs:30-44](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs#L30-L44) —— `CrossLine`：`vertical` 与 `horizontal` 各自带 `(start, Option<length>)`，`dashed` 默认 `true`。

[crates/ui/src/plot/tooltip.rs:61-65](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs#L61-L65) —— `.band(thickness)`：切到实色条带模式（`dashed = false`）。

**颜色修复所在**：

[crates/ui/src/plot/tooltip.rs:116-121](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs#L116-L121) —— `CrossLine::line` 的颜色计算：虚线分支由原来的 `cx.theme().border` 改为 `cx.theme().border.mix(cx.theme().foreground, 0.8)`；实色条带分支仍是 `cx.theme().foreground.opacity(0.08)`。

要准确理解这处改动的效果，必须看 `Colorize::mix` 的语义。其文档注释明说「`factor` 是**第一个颜色**的权重」（0.0..1.0）：

[crates/ui/src/theme/color.rs:44-45](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs#L44-L45) —— `mix` 文档：factor 属于第一个颜色（`self`）。

[crates/ui/src/theme/color.rs:193-209](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/theme/color.rs#L193-L209) —— `mix` 实现：对饱和度 / 亮度 / 透明度用 `self * factor + other * inv`（即 factor 是 self 权重），色相用最短路径插值。

把 `border.mix(foreground, 0.8)` 代入：factor = 0.8、inv = 0.2，于是饱和度 / 亮度 / 透明度都是 `border × 0.8 + foreground × 0.2`，即**保留 80% 的 border，掺入 20% 的 foreground**。border 在两种主题下都是较淡的灰色、foreground 是最强对比色（浅色主题近黑、深色主题近白），所以：

- 浅色主题：浅灰 border 掺 20% 近黑 → 变深一点的灰，比纯 border 更显眼。
- 深色主题：深灰 border 掺 20% 近白 → 变浅一点的灰，同样更显眼。

净效果：虚线十字线在两种主题下都比原来「更清晰可辨」，但又不像纯 `foreground` 那样刺眼——是一次克制的可见性打磨。这正是 PR 标题「Polish hover crosshair color」的意图。

`RenderOnce` 把竖向、横向两条线（按 `direction`）合到一个绝对定位容器里：

[crates/ui/src/plot/tooltip.rs:164-180](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs#L164-L180) —— `CrossLine::render`：按方向渲染竖线 / 横线 / 两者。

信息框的「靠边翻转」逻辑：

[crates/ui/src/plot/tooltip.rs:402-427](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs#L402-L427) —— `Tooltip::render`：框体跟随光标，当光标在左/上半时框体在右/下、反之在左/上，保证永不溢出近侧边界。

#### 4.5.4 代码实践

**目标**：直观对比十字线颜色修复前后的可见性。

**步骤**：

1. 打开 Story「Chart」页的 `Line Chart - Tooltip`，悬停观察十字线颜色（修复后）。
2. （可选对照）本地临时回退该颜色：把 [tooltip.rs:118](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/ui/src/plot/tooltip.rs#L118) 改回 `cx.theme().border`，重新运行，观察同一条十字线变得更淡；观察完务必还原，不要提交。
3. 切换主题明暗模式（若 Gallery 提供），分别在两种模式下观察十字线是否都清晰。

**预期结果**：修复后十字线在明、暗两种主题下都比纯 `border` 更醒目，但不刺眼。运行结果请以本地为准（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么柱状图的悬停高亮不受这次颜色修复影响？

**参考答案**：柱状图用 `.band(..)` 切到了实色条带模式（`dashed = false`），走的是 `foreground.opacity(0.08)` 那个分支；颜色修复只改了 `dashed` 分支（虚线发丝），所以柱状图的高亮颜色不变。

**练习 2**：若想让虚线十字线更突出，把 `0.8` 调小（如 `0.5`）会怎样？

**参考答案**：factor 是 border 的权重，调小意味着掺入更多 foreground，十字线会更接近 foreground、更醒目但也更「重」；调到 `0.0` 即完全等于 foreground。选择 0.8 是在「可见」与「克制」之间的取舍。

---

## 5. 综合实践

把本讲两处修复与三类图表串起来，完成下面这个验证任务。

**任务**：用 `bar_chart` 与 `line_chart` 各渲染一组示例数据并配置 tooltip，重点核对鼠标悬停时**十字线颜色**与**水平柱状条带**的正确显示。

**操作步骤**：

1. **运行 Gallery**：`cargo run`，进入「Chart」页（或 `cargo run -- chart` 直接聚焦）。

2. **观察折线图十字线颜色（修复 #2513）**：
   - 找到 `Line Chart - Tooltip`（源码 [chart_story.rs:487-496](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/chart_story/chart_story.rs#L487-L496)，链式 `.x(..).y(..).name(..).id(..)`）。
   - 悬停，确认十字线为一条比网格线明显、比正文浅的灰色（`border.mix(foreground, 0.8)`）。

3. **观察水平柱状条带范围（修复 #2513）**：
   - 找到 `Bar Chart - Left aligned` / `Bar Chart - Right aligned`（源码 [chart_story.rs:303-326](https://github.com/longbridge/gpui-component/blob/be4c5d30e0a51d5bfb2df93477a05050a50bf889/crates/story/src/stories/chart_story/chart_story.rs#L303-L326)，用 `.alignment(BarAlignment::Left)`）。
   - 悬停某根柱，确认半透明高亮条带只覆盖数值绘图区，不压到分类名、不越数值末端标签。

4. **（进阶）手写最小示例**：参考下面这段「示例代码」（非项目原有代码），在一个新 view 里同时放一张带 tooltip 的折线图与一张水平柱状图，亲自复现上述两点：

   ```rust
   // 示例代码：仅作示意，需自行放入一个实现 Render 的 view 的 render 方法中
   use gpui_component::{
       chart::{BarChart, LineChart},
       plot::shape::BarAlignment,
   };

   // data: Vec<(SharedString, f64)>，如 [("Jan", 30.), ("Feb", 80.), ...]
   LineChart::new(data.clone())
       .x(|d| d.0.clone())
       .y(|d| d.1)
       .name("Series A")
       .id("my-line"), // 开启 tooltip

   BarChart::new(data.clone())
       .band(|d| d.0.clone())
       .value(|d| d.1)
       .name("Series A")
       .alignment(BarAlignment::Left) // 水平柱，触发 h_span 分支
       .id("my-bar"),
   ```

**需要观察的现象与预期结果**：

- 折线图悬停：出现十字线 + 数据点圆点 + 数值框；十字线颜色清晰但不刺眼。
- 水平柱状图悬停：出现与柱等宽的半透明高亮条带，条带严格落在数值绘图区内。

若某项与预期不符，先回到 4.3.3 / 4.5.3 对照源码排查。运行结果请以本地为准（待本地验证）。

## 6. 本讲小结

- `chart` 模块把底层 `plot` 零件（轴 / 网格 / 比例尺 / 形状）组装成五类成品图表：折线、面积、柱状、饼图、K 线，**它们都实现同一个 `Plot` trait**。
- `#[derive(IntoPlot)]` 为图表生成 `IntoElement` + `Element`，统一接管鼠标事件循环；**`.id(..)` 是开启悬停 tooltip 的唯一开关**，不加即为纯静态图。
- 折线 / 面积用 point 轴，悬停十字线是**虚线发丝**；柱状用 band 轴，悬停高亮是**与柱等宽的半透明实色条带**。
- **修复一（柱状）**：水平柱的悬停条带必须用 `.h_span` 约束横线，旧代码误用 `.span`（约束竖线）导致条带横穿溢出数值绘图区。
- **修复二（十字线）**：虚线十字线颜色由 `border` 改为 `border.mix(foreground, 0.8)`——保留 80% border、掺入 20% foreground，使两种主题下都更清晰却不刺眼；实色条带（柱状）走另一分支，不受影响。
- 饼图与 K 线目前未实现 tooltip，默认不可交互；饼图用三遍扫描 + 双向松弛解决引线标签防重叠。

## 7. 下一步学习建议

- **回到底层**：若对 `CrossLine` 的 `.span/.h_span`、`Bar` 的方向感知、`Arc`/`Pie` 的角度计算还想深究，回到 [u10-l1](u10-l1-plot-and-into-plot.md) 精读 `plot/scale` 与 `plot/shape`。
- **自定义图表**：尝试实现一个自己的 `Plot`（如散点图 `ScatterChart`），只需写 `paint` 即得静态图，再加 `id`/`tooltip_state`/`tooltip` 三方法即可获得与官方图表一致的悬停体验——这正好印证 4.1 的「能力 opt-in」设计。
- **主题定制**：图表大量取色于主题（`chart_1..5`、`chart_bullish/bearish`、`border`、`foreground`）。结合 [u2-l1](u2-l1-theme-system.md) 自定义一套主题色，观察所有图表如何随之变化。
- **贡献实践**：参考 [u10-l3](u10-l3-global-state-actions-contributing.md) 的贡献规范，为饼图 / K 线补上 `tooltip_state` / `tooltip`，让它也支持悬停——这是一个大小合适、能完整走通 PR 流程的练手任务。
