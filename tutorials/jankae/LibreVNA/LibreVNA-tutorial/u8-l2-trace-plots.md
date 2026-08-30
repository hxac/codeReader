# 绘图体系：Smith 图、XY 图与瀑布图

## 1. 本讲目标

上一讲（u8-l1）我们搞清楚了「数据在哪里」：Trace 按 X 升序存放「x + 线性复数 y」，TraceModel 管理所有 Trace 并把测量数据分发进去。本讲回答「数据怎么变成屏幕上的图」：

1. 理解 `TracePlot` 基类用「模板方法」模式提供的公共骨架：重绘节流、事件分发（拖拽/缩放/平移/marker 拖动）、右键菜单、拖放分裂图窗、JSON 持久化。
2. 掌握三种坐标变换链：Smith/极坐标图的「复平面 → 圆盘像素」、XY 图的「复数样本 → 显示量 → 笛卡尔像素」、瀑布图的「显示量 → 颜色」。
3. 对比 Smith 图、极坐标图、XY 图、瀑布图、眼图各自适合表达什么数据、各自 `supported()` 拒绝什么数据。
4. 解释多个图共享同一条 Trace 时的更新机制：谁订阅、谁节流、谁触发重绘。

学完本讲，你应当能把任何一个测量点「手算」到屏幕像素，并能读懂任意一种图的核心绘制循环。

## 2. 前置知识

### 2.1 复数 S 参数与反射系数（回顾）

VNA 测出的每个点是复数 \( S \)（线性幅度 + 相位）。对反射参数 S11，它就是反射系数：

\[
\Gamma = \frac{Z - Z_0}{Z + Z_0}, \qquad Z = Z_0\,\frac{1+\Gamma}{1-\Gamma}
\]

其中 \( Z_0 \) 是参考阻抗（通常 50 Ω）。Smith 图就是把 \( \Gamma \) 复平面（永远满足 \( |\Gamma| \le 1 \)，无源时）直接画成一个单位圆盘，再叠加等阻抗网格。

### 2.2 Qt 绘图三件套

- **`QWidget::paintEvent`**：Qt 里所有自绘控件的重绘入口。系统触发重绘时不直接调用它，而是调用 `update()` 把一次重绘请求排入事件循环，多个 `update()` 会被合并成一次 `paintEvent`——这是 GUI 侧的第一层节流。
- **`QPainter`**：画笔。`drawLine`、`drawArc`、`fillRect`、`drawPixmap` 等都是它提供的原语。
- **坐标系**：Qt 窗口坐标 **y 轴向下**，原点在左上角。而 Smith 图虚轴向上、XY 图数值大的在上面——所以所有变换链里都会出现一次「取负号」或「高低互换」。

### 2.3 观察者模式：一份数据、多方订阅

Qt 的信号槽就是观察者模式。一条 `Trace` 发出一次 `dataChanged` 信号，可以有任意多个 `TracePlot` 连接它——这正是「多个图共享同一条 Trace」的机制基础。连接关系是动态建立的（图启用该 Trace 时 connect，禁用时 disconnect），不是静态注册。

### 2.4 模板方法模式（Template Method）

基类实现「整体流程」（如 `paintEvent` 先画标题、Trace 名标签、marker 数据框，最后才调 `draw(p)`），把「可变的一步」声明为纯虚函数留给子类。子类只填空，不重写流程本身。这是读懂 `TracePlot` 家族的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Traces/traceplot.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp) | 所有图的抽象基类：类型枚举、工厂、事件分发、重绘节流、拖放 |
| [Traces/tracepolar.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolar.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolar.cpp) | 「复平面圆盘」中间层：`dataToPixel`/`pixelToData`、圆内裁剪、平移缩放 |
| [Traces/tracesmithchart.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp) | Smith 图：阻抗网格、等 VSWR/R/X/Q 常量线、Z0 耦合 |
| [Traces/tracepolarchart.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolarchart.cpp) | 极坐标图：同心圆 + 放射线网格，接受任意频域迹线 |
| [Traces/tracexyplot.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp) | XY 图：双 Y 轴、自动量程、限位线、扫描指示 |
| [Traces/traceaxis.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp) | 轴抽象：`sampleToCoordinate`（复数→显示量）与 `transform`（数值→像素） |
| [Traces/tracewaterfall.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp) | 瀑布图：逐扫描热图，颜色编码幅度 |
| [Traces/eyediagramplot.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp) | 眼图：后台线程把 S21 频域数据经 TDR 仿真成数字眼图 |
| [Util/util.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h) / [Util/util.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp) | `Util::Scale` 通用线性/对数映射、S 参数换算、强度配色 |

继承关系一张图：

```
TracePlot (抽象: draw / supported / markerToPixel / nearestTracePoint / markerVisible)
 ├── TracePolar (复平面圆盘: dataToPixel / pixelToData / constrainLineToCircle)
 │    ├── TraceSmithChart   (Smith 图)
 │    └── TracePolarChart   (极坐标图)
 ├── TraceXYPlot            (双 Y 轴笛卡尔图)
 ├── TraceWaterfall         (逐扫描热图)
 └── EyeDiagramPlot         (数字眼图)
```

## 4. 核心概念与源码讲解

### 4.1 TracePlot 基类：一块图的公共骨架

#### 4.1.1 概念说明

GUI 里每种图（Smith、极坐标、XY、瀑布、眼图）看起来完全不同，但它们有大量共同行为：标题、Trace 名标签、marker 数据框、右键菜单、鼠标拖动 marker、滚轮缩放、中键自动量程、把一条 Trace 拖进图里、把自己嵌进 TileWidget 网格、保存/加载配置。`TracePlot` 把这些全部收编，只把「真正画数据」这一步留给子类：

- [traceplot.h:15-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.h#L15-L25)：`class TracePlot : public QWidget, public Savable`，五种图类型枚举 `Type { SmithChart, XYPlot, Waterfall, PolarChart, EyeDiagram }`。注意它同时继承 `Savable`——每种图都能进 `.setup` 工作区文件（承接 u2-l3）。
- 五个**纯虚函数**构成子类必须实现的「填空题」：[traceplot.h:72-74](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.h#L72-L74) 的 `draw()`（画数据）与 `supported()`（这个图能不能显示这条 Trace），以及 [traceplot.h:83-84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.h#L83-L84) 的 `markerToPixel()`、`nearestTracePoint()`、`markerVisible()`（marker 交互三件套，瀑布图和眼图返回空值即等于声明「本图无 marker」）。
- 另一批**带默认实现的虚函数**是「选做题」：`move()`/`zoom()`/`setAuto()`（默认什么都不做）、`configureForTrace()`（默认返回 false）、`mouseText()`（默认空字符串）等，见 [traceplot.h:63-103](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.h#L63-L103)。

#### 4.1.2 核心流程

**① 生命周期与订阅**

```
构造 TracePlot(model)
  → 注册进静态集合 plots（全 GUI 图登记簿）
  → 子类构造函数末尾调用 initializeTraceInfo()
       → 对模型里已有 Trace 逐个 newTraceAvailable()（初始都未启用）
       → connect(model, traceAdded, newTraceAvailable)   ← 以后新增的 Trace 也会进列表
用户勾选某条 Trace / 拖入
  → enableTrace(t, true)
       → connect(t, dataChanged,        triggerReplot)   ← 数据更新通知
       → connect(t, visibilityChanged,  triggerReplot)
       → connect(t, markerAdded/Removed, markerAdded/Removed)
       → connect(t, typeChanged/deembeddingChanged, checkIfStillSupported)
       → 重建右键菜单，replot()
```

**② 数据到达后的重绘（两级节流）**

```
Trace::dataChanged（一次，可能每秒几十次）
  → 每个启用了该 Trace 的图各自收到 triggerReplot()
       → 距上次重绘 ≥ 100ms（MinUpdateInterval）？立即 replot() = update()
       → 否则重启 100ms 单发定时器（后续事件自然合并）
paintEvent()
  → 画公共装饰：标题、Trace 名标签、marker 数据框
  → p.setViewport/setWindow 裁出数据区
  → 调子类 draw(p)                      ← 唯一的子类填空点
  → replotTimer.start(2000ms)            ← 兜底：即使无数据事件也至少 2 秒刷一次
```

**③ 交互事件分发**（全部在基类实现，子类只实现 `move/zoom/setAuto/nearestTracePoint` 等语义函数）

| 输入 | 基类处理 | 委托给子类 |
|---|---|---|
| 左键按下 | 命中 marker（20px 内）则选中；否则进入平移模式 | `positionWithinGraphArea()` 判定 |
| 左键拖动 | 拖 marker 或平移 | `nearestTracePoint()` / `move()` |
| 滚轮 | 以光标为锚缩放，Shift/Ctrl 限定单方向 | `zoom()` |
| 中键 | 恢复自动量程 | `setAuto()` |
| 右键 | marker 菜单或图菜单 | `updateContextMenu()` |
| 拖入 Trace | 计算落点五个分区 | `traceDropped()` / `supported()` |

#### 4.1.3 源码精读

**订阅与退订——多图共享一条 Trace 的机制核心**。[traceplot.cpp:70-98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L70-L98)：`enableTrace` 在启用时连接 8 个 Trace 信号，禁用时逐一断开：

```cpp
void TracePlot::enableTrace(Trace *t, bool enabled)
{
    if(traces[t] != enabled) {
        traces[t] = enabled;
        if(enabled) {
            // connect signals
            connect(t, &Trace::dataChanged, this, &TracePlot::triggerReplot);
            connect(t, &Trace::visibilityChanged, this, &TracePlot::triggerReplot);
            ...
```

要点：连接是「每图一份」。同一条 Trace 被 Smith 图和 XY 图同时启用时，`dataChanged` 一次发射，两个图各自触发各自的 `triggerReplot`，各自的 100ms 节流互不影响。`traces` 是 `std::map<Trace*, bool>`（[traceplot.h:75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.h#L75)），value 为 false 表示「图知道这条 Trace 存在但没显示它」——所以右键菜单能列出全部候选 Trace。

**重绘节流**。[traceplot.cpp:792-802](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L792-L802)：

```cpp
void TracePlot::triggerReplot()
{
    auto now = QTime::currentTime();
    if (lastUpdate.msecsTo(now) >= MinUpdateInterval // last update was a sufficiently long time ago
            || lastUpdate.msecsTo(now) < 0) { // or the time rolled over at midnight
        lastUpdate = now;
        replot();
    } else {
        replotTimer.start(MinUpdateInterval);
    }
}
```

两个常量在 [traceplot.h:55-56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.h#L55-L56)：`MinUpdateInterval = 100`、`MaxUpdateInterval = 2000`。注意午夜回绕的 `msecsTo(now) < 0` 分支——`QTime` 只有 24 小时，跨零点时间差为负，此时强制重绘。

**paintEvent：模板方法的「模板」部分**。[traceplot.cpp:339-351](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L339-L351)：

```cpp
    unsigned int l = marginLeft;
    unsigned int t = marginTop;
    unsigned int w = width() - marginLeft - marginRight;
    unsigned int h = height() - marginTop - marginBottom;

    if(hasMarkerData) {
        w -= marginMarkerData;
    }

    p.setViewport(l, t, w, h);
    p.setWindow(0, 0, w, h);

    draw(p);
```

基类先用 `setViewport/setWindow` 把 painter 的逻辑坐标系裁到「标题、Trace 标签、marker 数据框都让开之后」的数据区，且逻辑坐标 == 像素坐标（窗口与视口同尺寸）。子类 `draw(p)` 拿到的 `p.window()` 就是纯数据区。函数末尾 [traceplot.cpp:392](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L392) 的 `replotTimer.start(MaxUpdateInterval)` 保证静止画面也会周期性重绘（例如扫描指示三角需要随扫描位置移动）。

**工厂与默认图选择**。[traceplot.cpp:134-145](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L134-L145) 的 `createFromType` 按类型 switch 到五个具体类；[traceplot.cpp:762-773](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L762-L773) 的 `createDefaultPlotForTrace` 则按「反射/传输」查偏好设置选默认图类型——VNA 模式为每个 S 参数自动建图时（[vna.cpp:1795-1808](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1795-L1808)）走的就是这条路径。

**marker 命中与拖动**。[traceplot.cpp:541-582](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L541-L582) 的 `markerAtPosition` 遍历所有启用 Trace 的 marker，调子类 `markerToPixel(m)` 拿屏幕位置，取距离平方最小者；[traceplot.cpp:577](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L577) 的 `closestDistance <= 400` 即命中半径 20 像素（\( \sqrt{400} = 20 \)）。拖动时 [traceplot.cpp:485-492](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L485-L492) 用子类 `nearestTracePoint` 把像素反算回「Trace 上最近的 x 位置」再写回 marker——坐标变换的逆方向。

**拖放分裂图窗**。把一条 Trace 拖到图上时，[traceplot.cpp:622-637](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L622-L637) 接受 MIME 类型为 `trace/pointer` 的拖动（数据就是 Trace 指针的序列化值）；[traceplot.cpp:639-672](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L639-L672) 按相对位置把落点分成「上方/下方/左侧/右侧/图内」五区；[traceplot.cpp:674-718](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L674-L718) 在 `dropEvent` 里：落在图内就交给子类 `traceDropped`，落在边缘就调 `parentTile->splitVertically/splitHorizontally` 把当前图窗一分为二，再为新格子里 `createDefaultPlotForTrace` 建一张默认图并启用该 Trace。

**域变更的善后**。当某条 Trace 的输出类型变了（例如挂上 TDR 数学节点，频域变时域），`checkIfStillSupported`（[traceplot.cpp:804-842](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L804-L842)）按偏好设置三选一：直接移除、只在它是唯一 Trace 时尝试调整图、或总是先尝试 `configureForTrace` 调图、失败再移除。这解释了你在 GUI 里见过的「拖入时频 Trace 到频域图会弹确认框」行为。

#### 4.1.4 代码实践

**实践目标**：不写一行代码，靠阅读回答「一次扫描的一个新数据点到达后，屏幕上到底发生了什么」；有条件编译的话再验证。

**操作步骤**：

1. 打开 GUI，`File → Import` 导入 `Documentation/Measurements/Mini-circuits_VAT-10+.s2p`（无需硬件）。
2. 手工把 S11 同时显示在两张图上：在 Trace 列表里把 S11 拖进一个 Smith 图，再拖进一个 XY 图。此时全 GUI 有两个 `TracePlot` 实例共享同一条 S11 `Trace`。
3. 源码走读，沿下面这条链标注「谁调用谁」：

```
Trace::dataChanged
  → TracePlot::triggerReplot        (Smith 图那份连接)
  → TracePlot::triggerReplot        (XY 图那份连接)
  → 各自判断 100ms 节流 → update() → paintEvent() → draw()
```

4. （可选，示例代码）在 [traceplot.cpp:792](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L792) `triggerReplot` 函数体第一行临时加 `qDebug() << "replot requested by" << (QObject*)sender();`，重新编译运行，观察两条 Trace 更新时是否打印两行。

**需要观察的现象**：导入的是静态文件，不会触发 `dataChanged`；只有连接真设备开始扫描，或修改 Trace（如改颜色）才能看到触发。走读版实践则以「能复述调用链」为完成标准。

**预期结果**：能说出「一次 `dataChanged` 会让 N 个启用了该 Trace 的图各自收到一次通知，每个图独立节流」；加日志版应看到同一时刻打印两行（Smith + XY）。日志实验需本地编译验证，打印条数以实际连接的图数为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TracePlot` 把 `draw()` 和 `supported()` 设为纯虚，而 `move()`/`zoom()` 用带空默认实现的虚函数？

**答案**：任何图都必须画数据、必须回答「能否显示某 Trace」，没有合理默认，故纯虚强迫子类实现；而瀑布图、眼图这类不适合平移缩放的图可以不覆写 `move/zoom`，基类空实现让它们「自然不支持」，右键中键/滚轮操作变成无害空操作（且 `TraceWaterfall` 里被注释掉的 `move/zoom` 源码就是佐证，见 [tracewaterfall.cpp:65-95](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L65-L95)）。

**练习 2**：`MinUpdateInterval = 100`、`MaxUpdateInterval = 2000` 各自防什么？

**答案**：前者是**上限节流**——扫描时每秒可能来几十次 `dataChanged`，把重绘压到最高 10 次/秒，防止 GUI 线程被绘图打满；后者是**兜底定时**——`paintEvent` 末尾启动 2 秒单发定时器，保证即使没有任何数据事件（例如暂停的 Trace），周期性视觉元素（扫描指示线/三角）仍能刷新。

**练习 3**：`traces` 这个 map 里 value 为 false 的条目有什么用？

**答案**：表示「图已知该 Trace 但未显示」。`newTraceAvailable`（[traceplot.cpp:775-782](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L775-L782)）把模型中每条 Trace 都登记为 false，右键菜单据此列出全部可勾选的 Trace；勾选即 `enableTrace(t, true)` 把 false 翻 true 并建立信号连接。

### 4.2 Smith 图与极坐标图：复平面到屏幕圆盘

#### 4.2.1 概念说明

Smith 图和极坐标图本质上是同一件事：**把复数反射系数/传输系数当成复平面上的点，画进一个圆盘**。区别只在网格：

- **Smith 图**：叠加等电阻/等电抗网格，坐标可以读成阻抗，只接受**反射测量**（S11、S22…）且 Trace 的参考阻抗必须等于图的 Z0。
- **极坐标图**：只有同心圆（等 |Γ|）和放射线（等相位），接受任意频域迹线（S21 也行），读数只有幅度/相位。

两者共享中间层 `TracePolar`，它实现复平面几何的公共部分：`dataToPixel`/`pixelToData` 双向变换、平移（`offset`）、缩放（`edgeReflection`）、把线段裁剪进可见圆（`constrainLineToCircle`）。

三个关键状态量（[tracepolar.h:60-67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolar.h#L60-L67)）：

- `edgeReflection`：圆盘边缘对应的 \( |\Gamma| \)。默认 1.0（完整单位圆）；设小即放大（看圆心细节），设大即缩小（连 \( |\Gamma|>1 \) 的有源区也能看）。
- `offset`：视场平移量，网格和数据同时加它，效果是「数据点 \(-\)offset 落在屏幕中心」。
- `transform`：每次 `draw` 前算好的 `QTransform`，复平面坐标到像素的一次性映射。

#### 4.2.2 核心流程

Smith 图 `draw()`（[tracesmithchart.cpp:238-399](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L238-L399)）固定三步：

```
① 建立坐标变换
   p.translate(w/2, h/2)                  // 圆心移到数据区中心
   p.scale(s, s), s = min(w,h)/(2·4096)   // 4096 = polarCoordMax，内部大整数刻度
   transform = p.transform()              // 存下来供 dataToPixel 用

② 画网格（全部用「圆」这一种原语）
   外圆：圆心(0,0) 半径 edgeReflection
   主网格：22 个过点 (1,0) 的圆（圆心 1∓i/6，半径 i/6，i=1..11）
   中心水平线 + 上下各 5 个等电抗圆（x ∈ {0.2, 0.5, 1, 2, 5}）
   用户自定义常量线（VSWR / R / X / Q）
   每个圆先 constrainToCircle 裁到可见范围再画

③ 画迹线
   for 相邻两点 (i-1, i):
      频率窗过滤 → NaN 检查 → 扫描指示隐藏
      last/now = dataAddOffset(样本)        // 加平移
      p1/p2  = dataToPixel(last/now)        // 复数→像素
      超出 edgeReflection 时 constrainLineToCircle 裁剪
      p.drawLine(p1, p2)
   再画各 marker 符号
```

坐标变换链的数学。`dataToPixel`（[tracepolar.cpp:131-134](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolar.cpp#L131-L134)）先把复数乘固定刻度 4096 再除以缩放系数，随后过 `transform`（平移 + 统一缩放）。两步合并：

\[
x_{\text{px}} = \frac{w}{2} + \frac{\min(w,h)}{2}\cdot\frac{\operatorname{Re}(\Gamma)}{E},\qquad
y_{\text{px}} = \frac{h}{2} - \frac{\min(w,h)}{2}\cdot\frac{\operatorname{Im}(\Gamma)}{E}
\]

其中 \( E \) 为 `edgeReflection`，w/h 是数据区宽高。虚部取负正是「Qt 的 y 向下、史密斯图虚轴向上」的一次性修正。逆变换 `pixelToData`（[tracepolar.cpp:159-163](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolar.cpp#L159-L163)）用 `transform.inverted()` 完成反行程。

平移与缩放的锚点数学（[tracepolar.cpp:83-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolar.cpp#L83-L93)）：

```cpp
auto pos = pixelToData(center);   // 光标处的数据坐标
auto shift = QPointF(pos.real(), pos.imag());
offset -= shift;
edgeReflection *= factor;
offset += shift * factor;
```

即先假设绕原点缩放（offset 先减去锚点把锚点搬到原点），改 \( E \)，再把锚点按新刻度搬回去——标准的「以光标为锚的缩放」。

#### 4.2.3 源码精读

**变换的建立**。[tracesmithchart.cpp:241-249](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L241-L249)：

```cpp
    // translate coordinate system so that the smith chart sits in the origin and has a size of 1
    auto w = p.window();
    p.save();
    p.translate(w.width()/2, w.height()/2);
    auto scale = qMin(w.height(), w.width()) / (2.0 * polarCoordMax);
    p.scale(scale, scale);

    transform = p.transform();
    p.restore();
```

注意技巧：临时把 painter 坐标系摆好，**抄下矩阵立即 `restore`**。之后的绘制都走 `dataToPixel` 显式变换，而不是依赖 painter 状态——这样线宽、字体不会被巨大缩放系数（1/4096）影响。

**主网格 = 一族过 (1,0) 的圆**。[tracesmithchart.cpp:266-288](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L266-L288)：

```cpp
    constexpr int Circles = 6;
    ...
    for(int i=1;i<Circles * 2;i++) {
        auto radius = (double) i / Circles;
        drawArc(SmithChartArc(QPointF(1.0 - radius+offset.x(), 0.0+offset.y()), radius, 0, 2*M_PI));
        drawArc(SmithChartArc(QPointF(1.0 + radius+offset.x(), 0.0+offset.y()), radius, 0, 2*M_PI));
    }
    ...
    const std::array<double, 5> impedanceLines = {Z0*0.2, Z0*0.5, Z0, Z0*2, Z0*5};
    for(auto z : impedanceLines) {
        z /= Z0;
        auto radius = 1.0/z;
        drawArc(SmithChartArc(QPointF(1.0+offset.x(), radius+offset.y()), radius, 0, 2*M_PI));
        drawArc(SmithChartArc(QPointF(1.0+offset.x(), -radius+offset.y()), radius, 0, 2*M_PI));
    }
```

第一族（圆心在实轴上、全部内切于点 (1,0)）中半径 ≤ 1 的那些正是**等电阻圆**：等归一化电阻 \( r \) 的圆是圆心 \( \bigl(\tfrac{r}{1+r}, 0\bigr) \)、半径 \( \tfrac{1}{1+r} \)，恒有「圆心 + 半径 = 1」即过点 (1,0)。代入 \( i = 3 \)：半径 0.5、圆心 0.5，对应 \( r = 1 \)（50 Ω 线）；\( i = 1,2,4,5 \) 分别对应 \( r = 5, 2, 0.5, 0.2 \)。第二族（圆心在虚轴方向、切实轴于 (1,0)）是**等电抗圆**：归一化电抗 \( x \) 的圆是圆心 \( (1, \tfrac{1}{x}) \)、半径 \( |\tfrac{1}{x}| \)，代码取 \( x \in \{0.2, 0.5, 1, 2, 5\} \)。

**自定义常量线的解析公式**。[tracesmithchart.cpp:557-589](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L557-L589) `SmithChartConstantLine::getArcs` 把四类常量线全部折算成圆：

| 类型 | 公式 | 代码 |
|---|---|---|
| 等驻波比 | 圆心原点，半径 \( \frac{\text{VSWR}-1}{\text{VSWR}+1} \) | [L561-563](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L561-L563) |
| 等电阻 \( R \) | \( c_l = \frac{R/Z_0-1}{R/Z_0+1} \)，圆心 \( \frac{c_l+1}{2} \)、半径 \( \frac{1-c_l}{2} \) | [L564-568](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L564-L568) |
| 等电抗 \( X \) | 圆心 \( (1, \pm Z_0/X) \)、半径 \( \|Z_0/X\| \) | [L569-577](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L569-L577) |
| 等 Q 值 | 圆心 \( (0, \pm 1/Q) \)、半径 \( \sqrt{1/Q^2 + 1} \) | [L578-584](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L578-L584) |

等电阻公式与上面的网格推导互为印证。这些常量线经 `SmithChartConstantLine`（Savable 子类）存进 `.setup` 文件。

**圆内裁剪**。放大后很多网格圆/迹线段超出可见圆盘，`drawArc` lambda 先调 `a.constrainToCircle(QPointF(0,0), edgeReflection)`（[tracesmithchart.cpp:251-258](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L251-L258)），按圆-圆相交几何把弧的起角/跨角裁进可见范围（[tracesmithchart.cpp:485-548](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L485-L548)）；迹线段则用 `TracePolar::constrainLineToCircle`（[tracepolar.cpp:330-398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolar.cpp#L330-L398)）按圆-线相交公式裁剪线段两端（[tracesmithchart.cpp:342-348](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L342-L348)）。裁剪是「除法」而非「遮挡」——被裁掉的几何根本不送去光栅化。

**数据闸门：谁能上 Smith 图**。[tracesmithchart.cpp:416-429](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L416-L429) 的 `dropSupported` 要求 `isReflection()` 且输出类型为 Frequency/Power/TimeZeroSpan；[tracesmithchart.cpp:468-474](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L468-L474) 的 `supported` 在此之上再加一道 **Z0 匹配**：`t->getReferenceImpedance() != Z0` 即拒绝。`configureForTrace`（[L223-236](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L223-L236)）在收留新 Trace 时把图的 Z0 改成 Trace 的参考阻抗，并逐出 Z0 不符的旧 Trace——这就是拖入不同参考阻抗的 Trace 时弹确认框（[tracesmithchart.cpp:401-414](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L401-L414)）的来龙去脉。

**极坐标图的差异**。`TracePolarChart::draw`（[tracepolarchart.cpp:80-224](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolarchart.cpp#L80-L224)）网格换成「半径 \( i/6 \) 的同心圆（圆心即 offset）+ 每 30° 一条放射线」（[L108-128](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolarchart.cpp#L108-L128)）；`dropSupported` 只查频域，不要求反射（[tracepolarchart.cpp:226-234](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolarchart.cpp#L226-L234)）；光标读数只报 \( |\Gamma|\angle\varphi \)（[tracepolarchart.cpp:241-263](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracepolarchart.cpp#L241-L263)），而 Smith 图的 `mouseText` 可按用户选的格式显示 dB/实虚/VSWR/串联 R/Q/阻抗（[tracesmithchart.cpp:431-460](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L431-L460)）。

#### 4.2.4 代码实践

**实践目标**：验证 Smith 图坐标变换公式——手算一个反射系数的屏幕位置，再与 GUI 光标读数对表。

**操作步骤**：

1. 启动 GUI（无需硬件），导入 `Documentation/Measurements/Mini-circuits_VAT-10+.s2p`。
2. 新建一个 Smith 图并把 S11 拖进去。右键 `Setup...` 确认 `Zoom factor = 1`（即 `edgeReflection = 1`）、两个 offset 为 0，Z0 = 50 Ω。
3. 取一个便于手算的复数，例如 \( \Gamma = 0.5 + 0.2\mathrm{j} \)。量一下图区尺寸（近似即可，比如把窗口调成正方形 600×600，则数据区约 w = h = 560）。
4. 套公式计算：

\[
x_{\text{px}} = \frac{w}{2} + \frac{\min(w,h)}{2}\times 0.5 = 280 + 280\times 0.5 = 420,\qquad
y_{\text{px}} = \frac{h}{2} - \frac{\min(w,h)}{2}\times 0.2 = 280 - 56 = 224
\]

5. 对照：把鼠标移到你算出的点上，观察 `mouseText` 浮标显示的实部/虚部（右键 `Cursor format → Real + Imagj`）是否约为 `0.5+0.2j`。
6. 再做一次反向验证：在 Smith 图上放一个 marker，把它拖到某处，从 marker 数据框读出该点 S11 的实部/虚部，用公式反推像素位置，看与 marker 符号的实际位置是否一致（±2 像素内）。

**需要观察的现象**：鼠标位置与读数之间的换算是可逆的；虚部增大时点向上（屏幕 y 减小）。

**预期结果**：步骤 5 的浮标读数应与手算值一致（允许边缘留白导致的几个像素偏差——`min(w,h)/2` 之外图区还有余量）。本实践的像素级数值**待本地验证**：数据区实际尺寸取决于窗口与字体边距，重点检验「线性关系与方向」，不是绝对像素。

#### 4.2.5 小练习与答案

**练习 1**：推导等电阻圆公式：为什么 \( r \) 为常数的轨迹是圆心 \( \bigl(\tfrac{r}{1+r},0\bigr) \)、半径 \( \tfrac{1}{1+r} \) 的圆？

**答案**：令 \( \Gamma = u + \mathrm{j}v \)，由 \( \Gamma = \tfrac{z-1}{z+1} \) 反解 \( z = \tfrac{1+\Gamma}{1-\Gamma} \)。取 \( z = r + \mathrm{j}x \)，虚部为零的条件给 \( v^2 + (u - \tfrac{r}{1+r})^2 = \bigl(\tfrac{1}{1+r}\bigr)^2 \)：分子分母同乘 \( (1-u)^2 + v^2 \) 后整理即得标准圆方程，圆心 \( \bigl(\tfrac{r}{1+r}, 0\bigr) \)、半径 \( \tfrac{1}{1+r} \)。圆心 + 半径恒等于 1，故全族内切于 (1,0)——与代码 [tracesmithchart.cpp:270-274](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L270-L274) 画的那族圆完全一致。

**练习 2**：把 `edgeReflection` 从 1.0 改成 0.2，屏幕内容如何变化？哪些几何会被裁掉？

**答案**：相当于放大 5 倍：只有 \( |\Gamma| \le 0.2 \) 的圆盘占满绘图区，圆心附近细节展开；所有超出该半径的网格圆被 `SmithChartArc::constrainToCircle` 裁弧、迹线段被 `constrainLineToCircle` 裁段，完全在圆外的段被丢弃（`spanAngle = 0` / 返回 false）。GUI 里对应 `Setup...` 对话框的 `Zoom factor = 5`（[tracesmithchart.cpp:128-135](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L128-L135)，zoomFactor 与 edgeReflection 互为倒数）。

**练习 3**：Smith 图 `supported()` 为什么要比较 Z0？不比较会发生什么？

**答案**：Smith 图网格上的阻抗刻度（等电阻/等电抗线、常量线）都以图的 Z0 为基准绘制；若显示参考阻抗不同的 Trace，读出的阻抗就是错的。所以 `supported` 直接拒绝（[tracesmithchart.cpp:468-474](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp#L468-L474)），并给出 `configureForTrace` 这条「改图 Z0 并逐出冲突 Trace」的出路。极坐标图只读 \( |\Gamma| \) 和相位，与 Z0 无关，故无此约束。

### 4.3 XY 图：笛卡尔坐标与双 Y 轴

#### 4.3.1 概念说明

XY 图是「万能图」：横轴 X（频率/时间/距离/功率/零扫宽时间），纵轴两条独立的 Y 轴（左右各一条，例如左边幅度 dB、右边相位°），每条 Trace 可同时挂在一根或两根轴上。它与上讲 u8-l1 的「轴类型矩阵」直接对接：X 轴类型决定数据域闸门（`domainMatch`），Y 轴类型决定复数如何变成显示量。

`TraceXYPlot` 在基类之上新增的核心状态（[tracexyplot.h:118-129](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.h#L118-L129)）：

- `XAxis xAxis` 与 `YAxis yAxis[2]`：三根轴对象，各自带类型/范围/刻度。
- `std::set<Trace*> tracesAxis[2]`：每根 Y 轴各自显示哪些 Trace（基类的 `traces` map 是两者的并集视图）。
- `XAxisMode`：X 轴范围的三种来历——`UseSpan`（跟随设备扫描范围）、`FitTraces`（包络所有迹线）、`Manual`（手动）。

#### 4.3.2 核心流程

XY 图的绘制是**三层瀑布式变换**，每层职责单一：

```
第 1 层（数据 → 显示量，复数 → double）
   traceToCoordinate(t, i, yaxis)
     = ( xAxis.sampleToCoordinate(样本), yAxis.sampleToCoordinate(样本) )
   例：Magnitude 轴 → 20·log10|S|；Phase 轴 → arg(S)·180/π

第 2 层（显示量 → 像素，仿射映射）
   plotValueToPixel(QPointF, axis)
     x_px = x_L + (x − x_min)/(x_max − x_min) · W
     y_px = y_B + (y − y_min)/(y_max − y_min) · (y_T − y_B)   // y_T < y_B，数值大在上

第 3 层（逐段画线）
   相邻两点 → 各种过滤（NaN/限位/扫描指示/出框） → p.drawLine
```

第 2 层的 `Axis::transform` 委托给通用映射 `Util::Scale`（[util.h:15-28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L15-L28)）：线性时 \( \text{norm} = \tfrac{v - f_{\min}}{f_{\max} - f_{\min}} \) 再插到目标区间；对数时先取 \( \log_{10} \) 归一再反对数插值——一根函数同时服务所有轴、所有图（连瀑布图的颜色标定也用它）。

自动量程在每次重绘前由 `updateAxisTicks`（[tracexyplot.cpp:876-996](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L876-L996)）完成：

```
X 轴（非 Manual 模式）：
   UseSpan    → [sweep_fmin, sweep_fmax]（来自 TraceModel::SpanChanged → updateSpan）
   FitTraces  → 扫描所有可见 Trace 的 minX/maxX 取包络
Y 轴（autorange）：
   遍历本轴 Trace 的全部样本 → sampleToCoordinate → 取 min/max
   → 线性轴：两端各外扩 5%；对数轴：按对数比例外扩 5%，并处理跨零
   → 退化保护：所有值相同（如理想 Touchstone）时给 ±5% 或 ±1
```

#### 4.3.3 源码精读

**默认轴配置**。[tracexyplot.cpp:23-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L23-L36)：构造时左轴默认 Magnitude、右轴默认 Phase、X 轴 Frequency + UseSpan——正是你新建 XY 图时看到的样子。第 28 行 `yAxis[1].setTickMaster(yAxis[0])` 让右轴刻度线与左轴对齐（可选偏好）。

**第 1 层变换**。[traceaxis.cpp:103-167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L103-L167) `YAxis::sampleToCoordinate` 是「同一个复数、二十种读法」的巨型 switch，摘两例：

```cpp
    case YAxis::Type::Magnitude:
        return Util::SparamTodB(data.y);          // 20·log10|S|
    case YAxis::Type::Phase:
        return Util::SparamToDegree(data.y);      // arg(S)·180/π
```

X 轴版本（[traceaxis.cpp:502-514](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L502-L514)）只有一种特殊情况：`Distance` 轴要把时间经 `t->timeToDistance`（乘光速半程）换算，其余轴直接取样本 x。

**第 2 层变换**。[tracexyplot.cpp:1099-1113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1099-L1113)：

```cpp
QPointF TraceXYPlot::traceToCoordinate(Trace *t, unsigned int sample, YAxis &yaxis)
{
    QPointF ret = QPointF(numeric_limits<double>::quiet_NaN(), numeric_limits<double>::quiet_NaN());
    ret.setX(xAxis.sampleToCoordinate(t->sample(sample), t, sample));
    ret.setY(yaxis.sampleToCoordinate(t->sample(sample), t, sample));
    return ret;
}

QPoint TraceXYPlot::plotValueToPixel(QPointF plotValue, int Yaxis)
{
    QPoint p;
    p.setX(round(xAxis.transform(plotValue.x(), plotAreaLeft, plotAreaLeft + plotAreaWidth)));
    p.setY(round(yAxis[Yaxis].transform(plotValue.y(), plotAreaBottom, plotAreaTop)));
    return p;
}
```

注意 `transform` 的目标区间方向：X 是 `[左, 左+宽]`（正向），Y 是 `[底, 顶]`（数值上顶 < 底，即反向）——方向反转在参数顺序里完成，不需要显式取负。逆变换 `pixelToPlotValue`（[L1115-1121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1115-L1121)）供光标读数 `mouseText`（[L1264-1284](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1264-L1284)）使用。

**绘制主循环**。[tracexyplot.cpp:547-603](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L547-L603)，逐段过滤再画线：

```cpp
        p.setClipRect(QRect(plotRect.x()+1, plotRect.y()+1, plotRect.width()-2, plotRect.height()-2));
        for(auto t : tracesAxis[i]) {
            ...
            if(i == 1) {
                pen.setStyle(Qt::DotLine);        // 次轴迹线画虚线
            }
            ...
            for(unsigned int j=1;j<nPoints;j++) {
                auto last = traceToCoordinate(t, j-1, yAxis[i]);
                auto now = traceToCoordinate(t, j, yAxis[i]);
                // checking limits
                for(auto limit : constantLines) { ... if(!limit->pass(now)) limitPassing = false; }
                if(isnan(last.y()) || isnan(now.y()) || isinf(last.y()) || isinf(now.y())) continue;
                ... // 扫描指示隐藏判断
                auto p1 = plotValueToPixel(last, i);
                auto p2 = plotValueToPixel(now, i);
                if(!plotRect.contains(p1) && !plotRect.contains(p2)) continue;  // 整段出框
                p.drawLine(p1, p2);
            }
```

三个细节值得注意：右轴迹线用**虚线**区分；每段先做**限位线判定**（`XYPlotConstantLine::pass`，失败则全图标记 FAIL，见 [L753-788](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L753-L788) 的 PASS/FAIL 文本与红色覆盖两种呈现）；**两端都出框才跳过**，一端出框的段交给 `setClipRect` 裁——比 Smith 图的手动圆裁剪简单，因为矩形裁剪 QPainter 原生支持。marker 只画在左轴（[L604-644](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L604-L644)），且绘制 marker 时临时 `setClipping(false)` 让符号不被图框切掉。

**数据闸门**。[tracexyplot.cpp:1055-1097](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1055-L1097)：`domainMatch` 把 X 轴类型与 Trace 输出类型对齐（Frequency↔Frequency、Time/Distance↔Time、Power↔Power…）；`supported(t, type)` 再按 Y 轴类型过滤——VSWR/串联 R/电抗/电容/电感/Q/阻抗这些「由反射参数导出」的轴类型要求 `t->isReflection()`。`supported(t)`（[L417-426](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L417-L426)）是「两根轴任一支持即可」。

**marker 像素位置与插值**。[tracexyplot.cpp:1123-1159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1123-L1159)：marker 的 x 未必落在采样点上，`markerToPixel` 用相邻两采样点线性插值显示位置（`markerPoint = l0 + (l1 - l0) * t0`）——这就是你在屏幕上看到 marker「平滑落在两点之间」的原因。`nearestTracePoint`（[L1161-1215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1161-L1215)）则把反方向（像素→x）也做了一遍点到线段的投影。

**拖放的三个落区**。双轴都启用时，把 Trace 拖到 XY 图上会显示「左 1/3 加到主轴 / 中 1/3 加到两轴 / 右 1/3 加到次轴」三个区域（[tracexyplot.cpp:810-873](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L810-L873)），`traceDropped`（[L1226-1262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1226-L1262)）按 `dropOnLeftAxis/dropOnRightAxis` 标志分别启用对应轴；域不匹配时先问用户是否重配图（`configureForTrace`，[L303-340](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L303-L340)，按 Trace 输出类型换 X 轴并给 Y 轴挑默认类型）。

#### 4.3.4 代码实践

**实践目标**：沿三层变换手算一个 Touchstone 样本的像素位置与 dB 值，验证与 GUI 显示一致。

**操作步骤**：

1. 导入 `Documentation/Measurements/Mini-circuits_VAT-6+.s2p`，把 S11 显示在一个 XY 图（左轴 Magnitude、X 轴 Frequency）。
2. 从 Touchstone 文件里挑一行（文件是文本，直接用编辑器打开），记下频率 \( f \) 与线性幅度 \( |S_{11}| \)。例如某行若为 \( |S_{11}| = 0.5 \)，则 dB 值：

\[
20\log_{10}(0.5) = -6.02\ \text{dB}
\]

3. 在 GUI 里放一个 marker 到同一频率，读 marker 数据框的 dB 值，与手算对表（这验证第 1 层 `sampleToCoordinate`）。
4. 从图上读出当前轴范围（把光标移到左右下边框直接读刻度）：设 X 轴 \( [f_{\min}, f_{\max}] \)、Y 轴 \( [y_{\min}, y_{\max}] \)，再量图区左边距 \( x_L \)、宽 \( W \)、底 \( y_B \)、高 \( H \)，代入：

\[
x_{\text{px}} = x_L + \frac{f - f_{\min}}{f_{\max} - f_{\min}}\cdot W,\qquad
y_{\text{px}} = y_B - \frac{-6.02 - y_{\min}}{y_{\max} - y_{\min}}\cdot H
\]

5. 把 marker 拖到你算出的像素处，看 marker 报告的频率是否回到 \( f \)（这同时验证了第 2 层正变换与 `nearestTracePoint` 逆变换）。

**需要观察的现象**：dB 手算值与 marker 读数完全一致（Touchstone 的线性值换算是精确的）；像素位置因边距测量误差允许 ±5 像素。

**预期结果**：第 3 步应当严格吻合；第 5 步方向正确（x 大→频率高，y 小→dB 高）。若你的 Touchstone 行是 dB 格式（`.s2p` 选项行注明 MA/DB/RI），先按格式换算成线性再算。

#### 4.3.5 小练习与答案

**练习 1**：为什么 XY 图能显示任何 Trace，Smith 图却不能？

**答案**：XY 图的 `supported` 是「两根 Y 轴任一支持即可」，而轴类型多达二十余种，总能找到一种兼容的读法（最差情况换 X 轴域，`configureForTrace` 还能整图重配）；Smith 图坐标本身就是 \( \Gamma \) 复平面，几何上只对反射参数有意义，`dropSupported` 便硬性要求 `isReflection()` 加 Z0 匹配。

**练习 2**：`updateAxisTicks` 里 `range == 0.0` 的分支防什么？什么时候触发？

**答案**：当某轴上所有样本的显示量完全相同（例如导入只有少数几个点的理想文件，或 Trace 只有一个点），max − min 为 0，直接除会产生 NaN/Inf。代码区分两种情况：全零时给 [−1, 1]，非零常值时给 ±5%（[tracexyplot.cpp:962-979](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L962-L979)），保证图永远可读。正常扫描几乎不会触发，导入文件时常见。

**练习 3**：一条 Trace 同时挂在两根 Y 轴上（幅度 + 相位），`enableTrace(t, true)` 走了什么路径？基类信号连接建了几份？

**答案**：`TraceXYPlot::enableTrace`（[tracexyplot.cpp:79-84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L79-L84)）对两根轴各调一次 `enableTraceAxis`；`enableTraceAxis`（[L1019-1053](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracexyplot.cpp#L1019-L1053)）只在「至少一根轴需要启用」时才调 `TracePlot::enableTrace(t, true)`，且禁用时只有当另一根轴也不显示它才真正断开基类连接。因此**基类的 dataChanged 连接只建一份**（Qt 同一信号槽重复连接默认也会去重，但这里逻辑上就只调一次），两根轴共用一次重绘通知，绘图循环再按轴分别变换。

### 4.4 瀑布图与眼图：把「历史」与「仿真」画进颜色

#### 4.4.1 概念说明

前三种图回答「现在的扫描长什么样」，另两种图回答别的问题：

- **瀑布图（TraceWaterfall）**：把**每一次扫描**压成一行像素，用颜色编码幅度，历史记录向下（或向上）滚动——适合观察漂移、间歇干扰、温漂。它**同一时刻只显示一条 Trace**，且**没有 marker**。
- **眼图（EyeDiagramPlot）**：输入是频域 S21，软件先用 TDR（时域反射/传输变换，下一单元 u8-l6 的数学节点）求出冲激响应，再用可配置的数据率、上升/下降时间、噪声、抖动**仿真**一段数字信号，把每个比特周期折叠到横轴 2 个码元宽度上叠画——评估信道对数字信号的影响。计算量大，放在专用后台线程 `EyeThread` 里做。

#### 4.4.2 核心流程

**瀑布图的数据累积**（[tracewaterfall.cpp:544-593](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L544-L593)）：

```
Trace::dataChanged(begin, end)
  → 若 X 轴范围变了：resetWaterfall()（历史作废，网格对不齐）
  → 若 begin == 0（新扫描开始）：data.push_back(空行)，超长时 pop_front（FIFO）
  → 把 [begin, end) 的样本拷进最新一行 data.back()
  → Y autorange：在线更新 min/max
draw()
  → 对每行（每次扫描）：
       对行内每个样本 s：
          x 区间 = 与左右邻居的中点（矩形无缝拼接）
          颜色   = getIntensityGradeColor( yAxis.transform(y, 0, 1) )
          fillRect(x 区间, 行的 y 区间, 颜色)
  → 行高 = pixelsPerLine；方向 TopToBottom / BottomToTop
  → 画不满的旧行按 keepDataBeyondPlotSize 决定保留或丢弃
```

颜色映射是唯一「坐标变换」：显示量先归一化到 [0,1]，再映射色相（[util.cpp:204-215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp#L204-L215)）：

\[
\text{hue} = 240° \times (1 - \text{intensity})
\]

强度 0 → 蓝色（240°），强度 1 → 红色（0°），饱和度与明度拉满；越界值钳到黑/白。

**眼图的计算-显示流水线**（[eyediagramplot.cpp:767-812](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp#L767-L812) 起）：

```
数据/参数变化 → semphr.release()（信号量唤醒）
EyeThread::run()
  → 取 calcMutex，清空多余信号量（合并多次触发）
  → 合法性检查（数据率/抖动范围）
  → 由 TDR 冲激响应 + 伪随机码型 + 边沿/噪声/抖动 → 计算各码元波形
  → 写入 calcData 缓冲
draw()
  → 取 bufferSwitchMutex，从 displayData 读
  → 把每段波形经 plotValueToPixel 映射成像素线段（Bresenham 直线）
  → 累加到「命中计数位图」，再按命中数上色（密度分级）
```

双缓冲（`data[2]` + `displayData/calcData` 指针交换 + 两把互斥锁）保证后台计算与前台绘制互不阻塞——这是 GUI 中少见的「生产者-消费者」结构，与 u7-l4 的平均器一样属于「重计算不卡界面」的范式。

#### 4.4.3 源码精读

**瀑布图的单 Trace 约束**。[tracewaterfall.cpp:35-58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L35-L58)：`enableTrace(t, true)` 先把已启用的其他 Trace 全部禁用（热图无法叠加两条曲线），再建立自己的 `dataChanged → traceDataChanged` 连接——注意这是**瀑布图私有的额外连接**，因为基类的 `triggerReplot` 只知道重画，不知道要把数据拷进 `deque`。

**「无 marker」的声明方式**。[tracewaterfall.h:41-42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.h#L41-L42) 的 `markerToPixel` 内联返回空 `QPoint()`，配合恒 false 的 `markerVisible`（[tracewaterfall.cpp:537-542](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L537-L542)）和返回 0 的 `nearestTracePoint`（[tracewaterfall.cpp:516-524](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L516-L524)）——三个纯虚函数分别给出「没有位置、不可见、拖不动」的答案，基类的 marker 交互自然全部失效。这是「用返回值声明能力缺失」的惯用法。

**填色主循环**。[tracewaterfall.cpp:394-421](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L394-L421)：

```cpp
        for(i=data.size() - 1;i>=0;i--) {
            auto sweep = data[i];
            for(unsigned int s=0;s<sweep.size();s++) {
                auto x = xAxis.sampleToCoordinate(sweep[s], trace);
                ...
                if(s == 0) { x_start = x; }
                else       { x_start = (prev_x + x) / 2.0; }   // 与左邻居的中点
                ...
                if(s == sweep.size() - 1) { x_stop = x; }
                else                      { x_stop = (next_x + x) / 2.0; }
                ...
                auto y = yAxis.sampleToCoordinate(sweep[s]);
                auto color = Util::getIntensityGradeColor(yAxis.transform(y, 0.0, 1.0));
                auto rect = QRect(round(x_start), ytop, round(x_stop - x_start) + 1, ybottom - ytop + 1);
                p.fillRect(rect, QBrush(color));
```

采样点稀疏时矩形用「邻居中点」决定边界，保证相邻矩形无缝且等宽——比逐点画竖线更接近真实频谱占用。Y 轴自动量程横跨**全部历史行**重算（`updateYAxis`，[L595-618](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L595-L618)），因为颜色是绝对标定，范围一变整幅历史都要重上色。左侧配色条（颜色标尺）画在 [L260-291](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L260-L291)，且瀑布图特意复用 `TraceXYPlot::sideMargin`（[L251-252](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L251-L252)，对齐选项 Alignment）让它能和 XY 图上下拼成一列、左右边缘对齐。

**眼图的数据闸门与坐标**。[eyediagramplot.cpp:693-704](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp#L693-L704)：`supported` 要求频域且**非反射**（S11 不能做眼图——眼图关心的是信号经过信道传输后的形状）。横轴固定为「2 个码元宽度」：`calculatedTime() = 2.0 / datarate`（[L750-753](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp#L750-L753)）；纵轴电压范围由仿真的高/低电平外扩 20%（[L755-765](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp#L755-L765)）。像素变换 `plotValueToPixel`（[L722-728](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp#L722-L728)）与 XY 图同构（复用 Axis::transform）。绘制时把每条仿真波形光栅化进命中计数位图（Bresenham 直线 + 可选模糊核，[L570-634](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp#L570-L634)），再按命中密度上色——与瀑布图同为「颜色编码」，但编码的是**波形叠加密度**而非幅度。

#### 4.4.4 代码实践

**实践目标**：手算瀑布图两个幅度对应的颜色，验证配色映射；走读一次「扫描 → 新行」的时机。

**操作步骤**：

1. **纯手算部分**（无需运行 GUI）：设瀑布图 Y 轴为 Magnitude、范围 [−40, +20] dB（默认量程近似）。取两个显示量：\( y_1 = -34\ \text{dB} \)、\( y_2 = +8\ \text{dB} \)。归一化（`yAxis.transform(y, 0.0, 1.0)` 即线性归一到 0..1）：

\[
i_1 = \frac{-34 - (-40)}{20 - (-40)} = 0.1 \Rightarrow \text{hue} = 240° \times 0.9 = 216°\ (\text{偏蓝})
\]
\[
i_2 = \frac{8 - (-40)}{60} = 0.8 \Rightarrow \text{hue} = 240° \times 0.2 = 48°\ (\text{偏黄/橙})
\]

对照代码 [util.cpp:204-215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp#L204-L215)：intensity < 0 → 黑（比下限还低）、> 1 → 白（超上限）、其余蓝→红渐变。

2. **GUI 验证部分**（需设备或仿真数据）：连接设备后把 SA 模式某条 Trace 放进瀑布图，观察每次扫描新增一行、旧行滚动；打开 `Setup...` 改 `pixelsPerLine`（1→4）与 `max lines`（500→50），观察行变厚、历史变短。
3. **走读部分**：确认「新行开始」的判据——[tracewaterfall.cpp:556-567](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L556-L567) 只在 `begin == 0 || data.size() == 0` 时 `push_back` 新行。据此回答：分段扫描（u7-l1 讲过的分段突破 4501 点上限）时，每段的第一点都会触发一次 `begin == 0`，一次逻辑扫描会占多行还是一行？

**需要观察的现象**（步骤 2）：行高、行数随参数即时变化；低幅度区偏蓝、高幅度区偏红/白。

**预期结果**：步骤 1 的两个色相值即答案（216°、48°）；步骤 3 的推论是**一次逻辑扫描会占多行**（每个分段的首点都被当成「新扫描开始」），这是分段扫描与瀑布图组合时值得注意的行为——具体表现**待本地验证**（可接设备或用文件回放观察）。

#### 4.4.5 小练习与答案

**练习 1**：瀑布图为什么要把 `data` 存成 `std::deque`（双端队列）而不是 `std::vector`？

**答案**：历史行是先进先出的滑动窗口：新扫描 `push_back`，超长 `pop_front`（[tracewaterfall.cpp:562-566](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewaterfall.cpp#L562-L566)）。`deque` 两端操作都是 O(1)；`vector` 的头删是 O(n)，长时间运行每秒多次搬移整段历史不可接受。

**练习 2**：眼图为什么禁用 S11，且把计算放到独立线程？

**答案**：禁 S11 是物理原因——眼图模拟「信号经过信道传到接收端」的波形，需要的是传输参数的冲激响应；反射参数对应的是源端看到的反射，折叠成眼图没有意义（[eyediagramplot.cpp:699-703](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/eyediagramplot.cpp#L699-L703)）。独立线程是因为计算包含冲激响应提取 + 多码元卷积仿真，耗时远超一帧绘图的预算；放在 GUI 线程会冻住整个界面。线程用信号量触发、合并多余唤醒（`semphr.tryAcquire(available)`），并用双缓冲避免「画到一半的数据」。

**练习 3**：瀑布图和 XY 图都用 `Axis::transform`，但用途有何不同？

**答案**：XY 图用它把显示量映射成**像素坐标**（`plotValueToPixel`，输出 int 像素）；瀑布图只用它做**归一化**（`yAxis.transform(y, 0.0, 1.0)`，输出 0..1 的强度）再转颜色，Y 方向的像素位置由「第几行扫描」决定而非数值大小。同一个仿射函数，一处当坐标用、一处当色标用。

## 5. 综合实践

**任务：一条 S11，三种视图，两条手推坐标链。**

1. **准备**（无需硬件）：编译并启动 GUI，导入 `Documentation/Measurements/Mini-circuits_VAT-10+.s2p`（2 端口衰减器的实测数据）。
2. **建图**：右键图区 `Add tile...` 分裂出三个格子，分别放入 Smith 图、XY 图（左轴 Magnitude + 右轴 Phase）、瀑布图，三张图全部显示 S11。
3. **读同一个点**：在 Smith 图上创建 marker 并移到 1 GHz 附近；用 `Window → Add marker` 或右键在另外两图上观察同一 Trace 的 marker 数据。记录三个图各自报出的 S11 信息（Smith：阻抗/实虚；XY：dB 与角度；瀑布：无 marker——验证它确实不可交互）。
4. **手推两条坐标链**：
   - Smith 链：由 marker 读出的 \( \Gamma = a + b\mathrm{j} \)，用第 4.2 节公式算像素位置，与 marker 符号实际位置对照。
   - XY 链：由 Touchstone 原始线性值算 dB（第 4.3 节步骤 2），再由图上轴范围算像素，与 marker 实际位置对照。
5. **验证共享更新**：修改 S11 的显示颜色（Trace 属性），观察三张图是否同时变色——对应基类 `enableTrace` 连接的 `colorChanged → triggerReplot`（[traceplot.cpp:83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L83)）。
6. **收尾**：`File → Save setup`，用文本编辑器打开 `.setup` 文件，找到三张图的 JSON（`Smith Chart` / `XY Plot` / `Waterfall` 类型字符串来自 `TypeToString`，[traceplot.cpp:111-121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L111-L121)），确认轴范围、Z0、pixelsPerLine 等参数的落点，把整条「数据 → 坐标 → 像素 → 持久化」链路闭环。

完成标准：两条手推链的误差在厘米级像素测量误差内（±5 px）；能口述每个图各自的 `supported()` 会拒绝什么 Trace、为什么。

## 6. 本讲小结

- `TracePlot` 是模板方法基类：公共骨架（装饰绘制、重绘节流 100ms/2s、事件分发、拖放分裂、JSON 持久化）全在基类，子类只需实现 `draw/supported` 与 marker 三件套；「不支持某能力」通过空默认实现或空返回值声明。
- 多图共享一条 Trace 靠「每图一份信号连接」：`enableTrace` 时 connect `dataChanged → triggerReplot`，各图独立节流、独立重绘；域变更由 `checkIfStillSupported` 按偏好善后。
- Smith 图与极坐标图共享 `TracePolar` 的复平面几何：像素 = 中心 + 半径 × (Re/E, −Im/E)；主网格是一族过点 (1,0) 的等电阻圆加等电抗圆，自定义常量线（VSWR/R/X/Q）各有解析圆公式；Smith 图额外要求反射参数与 Z0 匹配。
- XY 图是三层瀑布：`sampleToCoordinate`（复数→显示量，如 20log10|S|）→ `Axis::transform`（显示量→像素，`Util::Scale` 支持线性/对数）→ 逐段过滤画线；双 Y 轴各挂各的 Trace 集，次轴虚线，限位线给出 PASS/FAIL。
- 瀑布图把每次扫描压成一行颜色（强度→色相 240°→0°），单 Trace、无 marker、历史存 `deque`；眼图把 S21 经 TDR 仿真成数字眼图，独立线程 + 双缓冲 + 命中计数位图。
- 五种图对数据的闸门各不相同：Smith 最严（反射 + 频/功率/零扫宽 + Z0），极坐标次之（频域即可），XY 最宽（靠轴类型矩阵兜底），瀑布按 X 域过滤且单迹线，眼图只要频域非反射。

## 7. 下一步学习建议

- **下一讲 u8-l3（Marker 系统）**：本讲反复出现的 `markerToPixel/nearestTracePoint/markerVisible` 正是 marker 与绘图体系的接口，下一讲进入 Marker 内部——插值、峰值搜索、编组联动。
- **u8-l5（Trace 数学运算框架）**与 **u8-l6（TDR/DFT/时间门）**：眼图依赖的 `Math::TDR` 就在那里；理解数学节点如何改变 `outputType` 从而触发本讲的 `checkIfStillSupported` 流程。
- 若想继续读绘图体系的周边：`CustomWidgets/tilewidget.cpp`（图窗如何分裂/嵌套）、`xyplotaxisdialog.cpp`（轴设置界面如何驱动 `setXAxis/setYAxis`）、`screenshot.cpp`（右键导出图片）。
- 动手方向：给 Smith 图的 `mouseText` 加一种自定义读数格式，或给瀑布图配色换一条色带（只改 `getIntensityGradeColor`），是两处改动极小、效果立竿见影的练习。
