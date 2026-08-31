# Marker 系统：读数、搜索与编组

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清一个 Marker 从创建、附着到 Trace、定位、取数、刷新的完整生命周期，以及它沿哪些 Qt 信号传播变化。
2. 找到并解释「从数据点到 marker 显示值」的插值代码：当 marker 落在两个采样点之间时，GUI 如何用线性插值算出显示值。
3. 列举 Marker 的 13 种类型（Manual、Maximum、Peak Table、Bandpass、TOI……），并读懂 `update()` 分发器与 Trace 侧的峰值/极值搜索算法。
4. 理解 helper marker（辅助游标）机制：为什么一个「带通」marker 会自带三个子游标。
5. 解释 MarkerGroup 如何让多条 Trace 上的游标同步移动，以及它如何防止信号级联回环。

## 2. 前置知识

**Marker（游标）是什么。** 如果你用过示波器或频谱仪的 cursor 功能，就已经见过 marker：它是贴在曲线上的一枚「读数探头」，指着一个 X 位置（频率/时间/功率），把该位置的 Y 值按选定格式（dB、阻抗、VSWR……）读给你。LibreVNA 把这个概念做得很完整——marker 不只能手动放置，还能自动搜索峰值、测滤波器带宽、算三阶交调，甚至跨多条 Trace 编组联动。

**线性插值。** Trace 内部存的是离散采样点（见 u8-l1：按 X 升序排列的 `(x, 复数y)` 数组）。当 marker 的位置 \(x\) 落在两个采样点 \(x_{low}\) 与 \(x_{high}\) 之间时，需要估算该处的值。最简单的办法是线性插值：

\[
\alpha = \frac{x - x_{low}}{x_{high} - x_{low}}, \qquad y = y_{low}\,(1-\alpha) + y_{high}\,\alpha
\]

对复数数据，实部和虚部分别做同样的插值——几何上就是在复平面上沿两点的连线（弦）取点。本讲会精确指出这行代码在哪里。

**Qt 树形模型/视图。** `MarkerModel` 继承 `QAbstractItemModel`，把 marker 列表呈现给 `QTableView`。顶层行是普通 marker，子行是 helper marker（树形结构）。表格里的可编辑控件（下拉框、SI 单位输入框）由 Delegate（委托）按需创建——这是标准 Qt Model/View 编程，只需知道「模型提供数据、委托提供编辑器」即可。

**信号与槽的同步直连。** Qt 的 `connect` 默认是直连：发出信号时，槽函数**立即**执行完才返回。这带来一个隐患——如果槽里又触发了同一个信号，就会递归。MarkerGroup 用一个布尔护栏解决这个问题，是本讲的一个亮点。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Traces/Marker/marker.h` / `marker.cpp` | 单个游标：状态、类型、定位、取数、自动搜索、持久化。本讲主战场 |
| `Traces/Marker/markermodel.h` / `markermodel.cpp` | 游标集合：树形表格模型 + Marker 容器 + MarkerGroup 的创建与回收 |
| `Traces/Marker/markergroup.h` / `markergroup.cpp` | 编组：让多个同域可移动游标共享一个位置 |
| `Traces/trace.cpp` | 被 Marker 调用的搜索算法：`findExtremum`、`findPeakFrequencies`、`interpolatedSample` |
| `Traces/Math/tracemath.cpp` | 插值的真正实现 `getInterpolatedSample`（u8-l1 讲过：Trace 的读取一律委托给数学链末端 `lastMath`） |
| `Traces/traceplot.cpp` | 图形交互：双击创建 marker、拖动 marker |
| `VNA/vna.cpp`、`SpectrumAnalyzer/spectrumanalyzer.cpp` | 各模式持有 MarkerModel，扫描完成时批量刷新 |

## 4. 核心概念与源码讲解

### 4.1 Marker 模型：附着、定位与读数

#### 4.1.1 概念说明

一个 `Marker` 对象的核心状态只有两个数：

- `position`（double）——X 轴位置。**单位随 Trace 的域（domain）变化**：频域是 Hz、时域是秒、功率域是 dBm。marker 自己不存单位，需要时通过 `getDomain()` 向父 Trace 查询。
- `data`（`std::complex<double>`）——该位置的 Y 值（频域是 S 参数，时域是冲激响应）。

除此之外，一个 marker 还携带：编号 `number`（图上三角形符号里画的数字）、可见性 `visible`、类型 `type`、可选的位置限制区间（`minPosition`/`maxPosition`/`restrictPosition`）、读数格式（表格一种 `formatTable`，图上可多种 `formatGraph`）。类型枚举共 13 种（`marker.h` L96–L112）：Manual、Maximum、Minimum、Delta、PeakTable、NegativePeakTable、Lowpass、Highpass、Bandpass、TOI、PhaseNoise、P1dB、Flatness。

Marker 不复制 Trace 的数据，它只是「指针 + 位置」，每次 Trace 数据变化时重新取一次插值样本。这意味着 marker 永远读到最新数据，也意味着空 Trace 上的 marker 读数是 NaN。

#### 4.1.2 核心流程

一个 marker 的完整数据流：

```text
创建（图表双击 或 表格添加）
  → assignTrace(t)          挂到某条 Trace，连接其 dataChanged 信号
  → constrainPosition()     位置夹取到 [minX, maxX]，可选吸附到最近采样点
  → traceDataChanged()      取插值样本 → data 更新
  → update()                按类型执行自动搜索（手动型则无事可做）
  → emit dataChanged        表格与所有图表刷新

此后两条触发路径：
① Trace::dataChanged → traceDataChanged → data 变了才 update
② 用户拖动/输入 → setPosition → constrainPosition → traceDataChanged → ...
③ 模式层扫描完成 → MarkerModel::updateMarkers → 每个 marker 调 update
```

注意一个设计上的自洽：自动类型（如 Maximum）在 `update()` 里调用 `setPosition()`，而 `setPosition` 又会经过 `constrainPosition → traceDataChanged → update`。这构成一次受控递归——搜索收敛后位置不再变化、`data` 不再变化，递归自然终止；而 Manual/Delta 分支在 `update()` 里是空操作（「nothing to do」），保证递归有底。

#### 4.1.3 源码精读

**类骨架与核心状态。** Marker 同时继承 `QObject`（为了信号槽）与 `Savable`（为了 JSON 持久化，见 u2-l3）：

- [marker.h:L16-L20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.h#L16-L20)：类声明，构造参数里带可选的 `parent`（helper marker 的宿主）和 `descr`。
- [marker.h:L207-L245](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.h#L207-L245)：全部成员变量。留意 `position`、`data`、`delta`（Delta 型的参照游标）、`helperMarkers`（子游标）、`parent`、`group`（编组）几个关键字段。
- [marker.cpp:L23-L51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L23-L51)：构造函数设定默认值——初始位置 1 GHz、类型 Manual、截止幅度 −3 dB、峰值门限 −40 dB、相位噪声偏移 10 kHz、表格格式 dB+angle。

**附着到 Trace。** `assignTrace` 是 marker 与数据源建立联系的唯一入口：

- [marker.cpp:L62-L89](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L62-L89)：先从旧 Trace 上拆线，再连接四路信号——`Trace::deleted`（Trace 被删时自毁）、`Trace::dataChanged`（数据变了重取读数）、`Trace::colorChanged`（重画符号）、`Trace::typeChanged`（域可能变了）。若新 Trace 不支持当前类型则退回 Manual。最后 `constrainPosition()` + `update()` + 发 `traceChanged`。
- [marker.cpp:L89-L91](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L89-L91)：helper marker 会跟着宿主一起换 Trace。

**定位与夹取。**

- [marker.cpp:L789-L794](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L789-L794)：`setPosition` 三步走——赋值、夹取、发 `positionChanged`（编组联动就靠这个信号）。
- [marker.cpp:L1092-L1116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1092-L1116)：`constrainPosition` 依次做三件事：① 若启用了限制区间，先夹到 `[minPosition, maxPosition]`；② 再夹到 Trace 的 `[minX(), maxX()]`；③ **若偏好设置 `Marker.interpolatePoints` 为 false（默认！），把位置吸附到最近采样点的 x**。这第三步是理解本讲实践任务的关键——默认配置下 marker 永远坐在真实采样点上，插值分支根本不会被触发。

**取数：插值读数发生的地方。**

- [marker.cpp:L810-L833](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L810-L833)：`traceDataChanged` 是 marker 的「心跳」。空 Trace 或位置越界时把 `data` 置为 NaN；否则第 825 行 `newdata = parentTrace->interpolatedSample(position).y` 取插值样本。**只有当新值 != 旧值时**才更新并触发 `update()` 与 `rawDataChanged`——这是一个朴素但有效的去抖：数据没变的刷新不会向下传播。
- [trace.cpp:L1699-L1703](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1699-L1703)：`Trace::interpolatedSample` 只有一行——委托给 `lastMath->getInterpolatedSample(x)`。这正是 u8-l1 讲过的「Trace 对外读取一律委托数学链末端」原则：如果这条 Trace 挂了 TDR 等数学运算，marker 读到的是**运算后**的数据。
- [tracemath.cpp:L142-L166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L142-L166)：**插值的真正实现**。用 `lower_bound` 二分找到第一个 \(x_{high} \ge x\) 的样本；精确命中直接返回；否则取前一个样本作 \(x_{low}\)，第 160–161 行完成线性插值：
  ```cpp
  double alpha = (x - low.x) / (high.x - low.x);
  ret.y = low.y * (1 - alpha) + high.y * alpha;
  ```
  `ret.x` 设为请求的 \(x\)（而不是某个采样点）。复数的加法与标量乘法让实部、虚部各自线性——几何上是复平面弦上的点。
- [preferences.h:L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L177) 与 [preferences.h:L383](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L383)：`Marker.interpolatePoints` 的声明与默认值 `false`（即默认吸附采样点，不插值）。

**读数格式化。** marker 表格里的 Data 列和图上标注，都来自一个 600 行的大函数 `readableData`：

- [marker.cpp:L400-L509](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L400-L509)：先按域分派（时域/频域/功率/零扫宽），再按格式枚举（[marker.h:L25-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.h#L25-L58) 共 20 多种）换算成字符串。Delta 型在此对参照游标做差（dB 差、相位差折叠到 ±360°）。
- 一个有代表性的分支：[marker.cpp:L508-L515](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L508-L515) 的 VSWR——当 \(|S| \ge 1\)（无源器件不可能）时显示 NaN 而不是算出负数，体现「宁可报错不可说谎」的工程习惯。

**域查询。** [marker.cpp:L2117-L2123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L2117-L2123)：`getDomain()` 直接返回 `parentTrace->outputType()`——marker 没有自己的域，Trace 挂上 TDR 数学运算后，marker 就自动「变成」了时域游标。

**图形符号。** [marker.cpp:L835-L887](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L835-L887)：`updateSymbol` 用 QPainter 画一个 15×15 的三角形，填充色取自所属 Trace 的颜色，中间写编号加后缀（helper marker 的后缀如 `1a`）。三种样式（数字在三角内/三角上方实心/空心）由偏好设置选择；helper marker 与隐藏的 marker 只得到 1×1 的空位图。

**MarkerModel：容器、表格与树。**

- [markermodel.cpp:L16-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L16-L23)：构造函数把自己注册回 TraceModel（`model.setMarkerModel(this)`）——图表双击创建 marker 时就是从 TraceModel 找到它的。
- [markermodel.cpp:L64-L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L64-L83)：`createDefaultMarker` 找一个最小空闲编号，把新 marker 挂到**第一条** Trace 并放在扫描范围中点。
- [markermodel.cpp:L85-L109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L85-L109)：`addMarker` 先做 `beginInsertRows`/`endInsertRows`（让视图平滑增行），再把 marker 的 `dataChanged`/`typeChanged`/`traceChanged`/`deleted` 等信号接到模型刷新上。
- [markermodel.cpp:L30-L62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L30-L62)：`index`/`parent` 实现树形结构——顶层行的父是虚拟 root，helper marker 是顶层行的子行（`rowCount` 见 [markermodel.cpp:L213-L220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L213-L220)）。
- [markermodel.cpp:L227-L257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L227-L257)：`data()` 给各列供数——编号列显示 `number + suffix`，Settings 列来自 `readableSettings()`，Data 列来自 `readableData()`。表头定义在 [markermodel.cpp:L259-L295](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L259-L295)，Group 列的提示文字（L284）说明了编组的操作方式：Ctrl 点选多行、右键编组。
- [markermodel.cpp:L432-L513](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L432-L513)：四个 Delegate 分别为 Trace 列、Type 列、Settings 列、Restrict 列创建编辑器，编辑器再回调 marker 的 `getTraceEditor`/`getTypeEditor`/`getSettingsEditor`/`getRestrictEditor`。

**图表交互入口。**

- [traceplot.cpp:L610-L615](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L610-L615)：在图上双击（靠近哪条 Trace）就调 `createDefaultMarker()` 并 `setPosition` 到点击处的 X 坐标。
- [traceplot.cpp:L485-L492](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L485-L492)：按住符号拖动时，每次鼠标移动调用 `setPosition(nearestTracePoint(...))`——把像素坐标反算回数据域 X。注意这条路径每次移动都会走完整的「夹取 → 取数 → 刷新」链。

**模式层的批量刷新。** [vna.cpp:L1073-L1076](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1073-L1076)：VNA 模式只在**每个扫描的最后一个点**到达时调 `markerModel->updateMarkers()`（频谱模式对应 [spectrumanalyzer.cpp:L585](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L585)）。也就是说：峰值表、带宽这类重搜索不是每个数据点都跑，而是攒满一整次扫描才重算一次——性能与实时性的折中。

#### 4.1.4 代码实践

**实践目标：** 在导入的示例 Trace 上创建 marker、启用峰值搜索，并亲自定位「采样点 → 显示值」的插值代码，解释两点之间的取值规则。

**操作步骤：**

1. 启动 GUI（无硬件即可，见 u1-l3），菜单 `File → Import` 导入 `Documentation/Measurements/Mini-circuits_VAT-10+.s2p`（一只 10 dB 衰减器的实测 S2P），并把它拖到一张 XY 图上。
2. 在图上**双击**靠近视在 S21 的位置——出现编号 1 的三角形 marker（这条路径就是 `traceplot.cpp:L610-L615`）。
3. 在 marker 表格中把该行的 Type 改为 `Maximum`，marker 立即跳到曲线最高点（对应 `update()` 的 `Type::Maximum` 分支）。
4. 打开 `Edit → Preferences`，在 Marker 组里找到「interpolate points」选项，切换开/关，然后在 Settings 列输入一个**不落在采样点上**的频率（例如两个刻度之间），观察 Data 列读数。
5. 对照源码确认：`Marker::traceDataChanged`（marker.cpp L825）调用 `Trace::interpolatedSample` → `TraceMath::getInterpolatedSample`（tracemath.cpp L150–L162）。
6. 手工验证一次插值：从导入的 `.s2p` 文本里挑两个相邻频率点 \(x_{low}, x_{high}\)，代入
   \[ y = y_{low}\,(1-\alpha) + y_{high}\,\alpha,\quad \alpha=\frac{x-x_{low}}{x_{high}-x_{low}} \]
   算出某个中间位置的 dB 值，与 GUI 读数对比（dB 域需先对插值结果取 \(20\lg|y|\)，因为插值发生在复数域，不是 dB 域）。

**需要观察的现象：**

- 偏好设置关闭时（默认），无论输入什么频率，marker 的 Settings 列会被**吸附**到最近的采样点频率——这就是 `constrainPosition` 第 1109–1112 行的吸附逻辑。
- 偏好设置打开后，marker 可以停在任意频率上，读数是插值出来的。
- 拖动 marker 时它沿曲线滑动，编号符号颜色与 Trace 同色。

**预期结果：** 你能指出插值发生在 `tracemath.cpp:L160-L161`，且插值对复数实虚部分别进行；手算值与 GUI 显示一致（误差在显示精度内）。若手算不一致，优先检查是否把插值误放到了 dB 域。运行效果待本地验证。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `traceDataChanged` 里要判断 `newdata != data` 才更新？如果去掉这个判断会怎样？

**答案：** 这是个去抖优化。Trace 每来一批数据都发 `dataChanged`，但 marker 位置的插值结果未必变（例如该处数据没更新）。去掉判断后每次都会执行 `update()` 并发 `rawDataChanged`/`dataChanged`——对自动类型这会重复搜索，对 Delta 型参照链会触发一串无意义的级联刷新，扫描数据密集时造成可观的额外开销。注意该比较对 NaN 不严格成立（NaN != NaN 为真），所以空数据时也会持续走更新路径，但 `update()` 开头对空 Trace 有早退保护。

**练习 2：** 把一条频域 Trace 挂上 TDR 数学运算（见 u8-l6 预告）后，原本显示频率的 marker 会发生什么？

**答案：** marker 的域来自 `getDomain()` → `parentTrace->outputType()`，TDR 运算使输出类型变为 Time，于是 marker 自动变成时域游标：Settings 列单位换成秒、可用格式收缩为 dB/实虚/阻抗（`applicableFormats` 的时域分支），且若当前类型（如 Bandpass）不被时域支持，`getSupportedTypes` 会使其退回 Manual。marker 代码全程无需感知「Trace 上发生了什么」。

**练习 3：** 图上 marker 符号里的编号是怎么画上去的？为什么 helper marker 的符号是 `1a`、`1b` 这样的？

**答案：** `updateSymbol`（marker.cpp L835–L887）用 QPainter 在三角形上绘制 `QString::number(number) + suffix`；顶层 marker 的 `suffix` 为空串，helper marker 创建时被赋予后缀——PeakTable 的第 i 个 helper 后缀是 `QChar('a' + i)`（marker.cpp L1853），滤波器类 helper 后缀是 `l`/`h`/`c`（setType 中 L1230–L1243 的描述表）。

### 4.2 自动搜索规则：update() 分发器与 Trace 侧算法

#### 4.2.1 概念说明

手动 marker 只回答「这里是多少」；自动 marker 回答「最值在哪」「峰有几个」「滤波器的带宽是多少」。LibreVNA 把所有自动搜索集中在一个入口 `Marker::update()` 里，按 `type` 分发。理解本模块要抓住三件事：

1. **搜索算法在 Trace 侧**（`findExtremum`、`findPeakFrequencies`），marker 只负责调用和摆放结果——算法可被任何 Trace 复用。
2. **复杂类型靠 helper marker 表达结果。** 一个 Bandpass marker 自带 3 个子游标（下截止、上截止、中心），一个 TOI marker 自带 4 个（双峰与两个交调产物）。主游标显示汇总读数，子游标在图上标出各个位置。
3. **类型可用性受「闸门」约束。** `getSupportedTypes()` 根据域、是否反射测量、数据来源（Live 且 SA 参数才有 TOI/相位噪声）动态决定下拉框里有哪些类型可选。

#### 4.2.2 核心流程

`update()` 的分发结构（伪代码）：

```text
update():
    若 Trace 为空：返回
    计算搜索窗口 [xmin, xmax]（restrictPosition ? 限制区间 : 全范围）
    switch type:
        Manual/Delta:  无操作
        Maximum:       position ← findExtremum(max=true,  窗口)
        Minimum:       position ← findExtremum(max=false, 窗口)
        PeakTable:     peaks ← findPeakFrequencies(最多100个, 门限, 3dB谷深, 窗口)
                       动态增删 helper（第 i 个放 peaks[i]，后缀 'a'+i）
        Lowpass/Highpass: 主游标放峰值；从峰值向一个方向走，
                       直到幅度 ≤ 峰值 + cutoffAmplitude → helper[0] 放截止点
        Bandpass:      从峰值向两侧各走一步搜索 → helper[0/1] 截止点，
                       helper[2] 中心 = 两截止点中点
        TOI:           找两个峰 → helper[0/1] 放双峰，
                       helper[2/3] 放 f1−Δ 与 f2+Δ（Δ 为双峰间距）
        PhaseNoise:    主游标放载波峰，helper[0] 放 载波+offset
        P1dB:          从最大点向高功率方向走，直到增益压缩 1 dB
        Flatness:      在两个 helper 限定区间内，求曲线相对两端点连线的
                       最大正/负偏差（maxDeltaPos/maxDeltaNeg）
    发 dataChanged
```

峰值判定的状态机（`findPeakFrequencies`）核心是「**起了峰、又回落够了，才算峰**」：维护当前候选峰顶电平 `max_dbm` 与其后最深谷 `min_dbm`；一个候选被确认的条件是它比门限高、且曲线从峰顶回落了至少 `minValley`（本调用中固定 3 dB）。这避免了把噪声毛刺都当成峰。

滤波器截止搜索的数学很直白：设峰顶幅度为 \(P_{peak}\)（dB），截止幅度参数为 \(c\)（默认 −3 dB），则从峰顶出发沿 X 方向逐点检查，第一个满足

\[
P(x) \le P_{peak} + c
\]

的采样点即截止点。Lowpass/Highpass 只向一个方向走（`inc = ±1`），Bandpass 双向各走一遍。

TOI（三阶交调点）读数由四个 helper 的电平算出。设两个基波峰平均电平为 \(\bar{P}_{fund}\)、两个交调产物平均电平为 \(\bar{P}_{IM}\)，则

\[
\text{TOI} = \frac{3\,\bar{P}_{fund} - \bar{P}_{IM}}{2}
\]

直观推导：基波功率随输入以 1:1 斜率增长、三阶产物以 3:1 斜率增长，把两条斜线外推到相等处即交调点。代码在 `readableData` 的 `Format::TOI` 分支（marker.cpp L523–L528）。

#### 4.2.3 源码精读

**类型闸门。** [marker.cpp:L1037-L1090](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1037-L1090)：`getSupportedTypes` 按域给出可用类型——时域只有 Manual/Delta（L1042–L1046）；频域最全，但**反射测量（S11/S22）不含 Lowpass/Highpass/Bandpass**（L1056–L1060，这些计算需要传输测量）；TOI 与 PhaseNoise 只对「Live 来源且 SA 参数」的 Trace 开放（L1061–L1069）；功率域额外支持 P1dB（L1078–L1084）。这个集合同时驱动 Type 下拉框（`getTypeEditor` L1522 遍历它）与右键菜单。

**update() 分发器。**

- [marker.cpp:L1826-L1845](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1826-L1845)：函数开头处理空 Trace 早退、组装搜索窗口，Maximum/Minimum 两个分支各一行——把 `findExtremum` 的结果直接交给 `setPosition`。
- [marker.cpp:L1846-L1872](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1846-L1872)：PeakTable 分支——调 `findPeakFrequencies(100, peakThreshold, 3.0, ...)`，随后是 helper 的**动态增删**：峰变多就补建子游标（L1850–L1858），峰变少就整批删除多余的（L1862–L1870，包在 `beginRemoveHelperMarkers`/`endRemoveHelperMarkers` 信号里让表格模型同步删行）。`NegativePeakTable` 通过最后一个布尔参数复用同一算法找谷（例如通带内的凹陷）。
- [marker.cpp:L1873-L1907](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1873-L1907)：Lowpass/Highpass 的截止搜索——先放主游标到峰（即插入损耗参考点），再从峰索引出发按 `inc = ±1` 逐点走，幅度跌破 `peak + cutoffAmplitude` 即停（L1894），helper[0] 放该点。
- [marker.cpp:L1908-L1960](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1908-L1960)：Bandpass 与上一分支同构，只是双向各走一遍（L1922–L1938 向低、L1940–L1956 向高），中心游标取两截止点中点（L1958）。
- [marker.cpp:L1961-L1975](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1961-L1975)：TOI——找两个峰（门限 −100 dB、谷深 3 dB），双峰间距 \(\Delta = f_2 - f_1\)，两个交调 helper 放在 \(f_1 - \Delta\) 与 \(f_2 + \Delta\)（正好是三阶产物 \(2f_1 - f_2\) 与 \(2f_2 - f_1\)）。
- [marker.cpp:L1977-L1999](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1977-L1999)：PhaseNoise 与 P1dB。P1dB 从最大点出发向高功率走，第一个满足压缩量 ≥ 1 dB 的点即 P1dB（L1993）；若走到扫描上限仍未压缩，显示「> 上限」（见 `readableData` L583–L589）。
- [marker.cpp:L2001-L2044](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L2001-L2044)：Flatness——用两个 helper 指定区间的两端，`Util::Scale` 线性内插出「理想直线」，逐点求曲线与直线的偏差，记录最大正/负偏差并生成三条附加线段（`lines`，由图表负责绘制）。**Flatness 是唯一 helper 可由用户拖动的类型**（见 `isMovable` L2067–L2087 的特判），拖动任一端点会把主游标重新放在两端点中点（setType 中 L1256–L1263 的连接）。

**helper marker 的创建。** [marker.cpp:L1209-L1269](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1209-L1269)：`setType` 先删光旧 helper，再按一张「后缀 + 描述 + 类型」表（L1214–L1246）重建所需数量：Bandpass 三个（`l`/`h`/`c`）、TOI 四个、PhaseNoise 一个（`o`）、Flatness 两个（`l`/`u`）。helper 与主游标同编号、不同后缀，默认不可编辑（`isEditable` L2089–L2100 只放行 Flatness 的 helper）。

**Trace 侧：极值搜索。** [trace.cpp:L1510-L1526](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1510-L1526)：`findExtremum` 就是一次线性扫描——窗口外的点跳过，按 \(|y|\)（复数模）比较保留最大/最小的 x。注意比较的是**幅度**不是 dB，平顶时返回最后一个最大点。

**Trace 侧：峰值状态机。** [trace.cpp:L1528-L1591](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1528-L1591)：`findPeakFrequencies` 单遍扫描维护三个状态量：候选峰频率 `frequency`、峰顶电平 `max_dbm`、谷底电平 `min_dbm`。两处关键判断：

- L1553：`dbm >= max_dbm && min_dbm <= dbm - minValley`——只有当前电平超过此前谷底至少一个谷深时，新的高点才被接纳为候选峰（防止把同一个缓坡上的抖动当成多个峰）。
- L1561：`dbm <= max_dbm - minValley && max_dbm >= minLevel && frequency`——从峰顶回落够深、峰顶也够高（超过 `minLevel` 门限），峰正式确认。
- L1573–L1585：峰太多时先按电平降序截断保留 `maxPeaks` 个，再按频率升序排回——所以 Peak Table 的 helper 顺序总是从低频到高频。
- L1534–L1536 与 L1550–L1552：`negativePeaks` 模式把电平取负再搜索，等价于找「谷」（S21 通带里的陷波点）。

**汇总读数。** [marker.cpp:L523-L528](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L523-L528) 是 TOI 公式的落点；[marker.cpp:L562-L581](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L562-L581) 是「中心频率 + 带宽 + 插入损耗」的拼装——若曲线从未跌到截止电平以下（截止点没找到），中心显示 `?`、带宽显示 `>`，宁可标「不确定」也不给假数（L572–L575）。

#### 4.2.4 代码实践

**实践目标：** 用 Peak Table 的门限参数直观感受峰值判定规则，并手推一遍状态机。

**操作步骤：**

1. 承接 4.1.4 的会话，再导入 `Documentation/Measurements/Murata_RF1419D.s2p`（一只滤波器的实测数据，S21 有明显的通带与止带），拖到图上。
2. 新建 marker，Type 选 `Peak Table`。表格中出现主行 + 若干子行（`1a`、`1b`…），每个子行是一个找到的峰。
3. 把 Settings 列的门限从默认 −40 dB 逐步抬高（−30、−20、−10 dB），观察子行数量变化。
4. 勾选 Restrict，把搜索窗口缩到只有止带，再观察。
5. 手推状态机：打开 `.s2p` 文本，取一小段频率（约 10 个点），把每个点的 S21 换算成 dB，逐点模拟 `findPeakFrequencies` 的三个状态量（`frequency`/`max_dbm`/`min_dbm`），写下你认为会被确认的峰，与 GUI 的 Peak Table 对照。

**需要观察的现象：** 门限抬高后，低于门限的峰整行消失（不是读数变差而是不被承认）；限制窗口后窗口外的峰不再参与。相邻很近的两个隆起若之间的回落不足 3 dB，只会被算作一个峰。

**预期结果：** 手推结果与 GUI 一致；能说出「门限管高度、谷深管分隔」这两把尺子分别对应 `minLevel` 与 `minValley` 参数。运行效果待本地验证。

#### 4.2.5 小练习与答案

**练习 1：** Peak Table 最多能显示多少个峰？超出时保留哪些？

**答案：** 调用处写死上限 100（marker.cpp L1848 的第一个参数）。超出时 `findPeakFrequencies` 把候选峰按电平降序排序、截断到 100 个、再按频率升序排回（trace.cpp L1573–L1585）——保留的是**最高的**那些峰，显示时仍按频率从低到高排列。

**练习 2：** 为什么 Lowpass/Highpass/Bandpass 对 S11（反射测量）不可用？

**答案：** `getSupportedTypes` 在 L1056–L1060 显式排除了反射 Trace；`update()` 的对应分支（L1875–L1877、L1909–L1911）也有双保险直接 break。物理原因：滤波器截止/带宽的定义建立在「传输幅度跌落」之上，S11 的起伏形态与传输响应不是一回事（通带内 S11 低、止带内 S11 高，方向甚至相反），用它算带宽会得到错误结论。`readableData` 的 Cutoff/CenterBandwidth 分支（L540–L545、L562–L565）还准备了「反射测量无法计算」的友好文案兜底。

**练习 3：** Bandpass 的中心频率 helper 是精确找出来的还是算出来的？

**答案：** 算出来的——`helperMarkers[2]->setPosition((helperMarkers[0]->position + helperMarkers[1]->position) / 2)`（marker.cpp L1958），即两个截止点的中点，不做任何峰值重找。所以对非对称滤波器，「中心」可能不落在通带峰值上；若需要真正的峰值中心，应另加一个 Maximum marker。

### 4.3 MarkerGroup 联动：跨 Trace 同步移动

#### 4.3.1 概念说明

测一条滤波器时你常想同时看 S11 和 S21 在**同一频率**的读数。逐个拖两个 marker 既慢又对不齐。MarkerGroup 解决这个问题：把若干 marker 编成一组，拖动其中任何一个，整组的 X 位置一起变。本质上，MarkerGroup 是「共享一个 position」的广播器。

编组有两条硬规则（`applicable`）：

1. 成员必须**可移动**——只有 Manual/Delta 型（自动搜索型自身位置都不由用户控制，编组没有意义）；
2. 成员必须**同域**——频域与功率域的 marker 混在一组没有共同语言。

违规（例如某成员后来被改成 Maximum 型）不会让程序报错，而是被 `checkMarker` **自动请出组**。组空了以后自动销毁，不留下僵尸对象。

#### 4.3.2 核心流程

```text
编组（表格 Ctrl 多选 + 右键，或 marker 右键菜单 "Add to linked group"）
  → MarkerGroup::add(m)
      applicable? 不可则拒绝
      若 m 已在别的组：先从旧组移出（一个 marker 至多属一组）
      连接 m 的 positionChanged/typeChanged/domainChanged/deleted
      若组内已有成员：把 m 的位置吸附到组成员当前位置
      记录组域
  之后任一成员被拖动
      → positionChanged → markerMoved(newpos)
          若 adjustingMarkers 为真：直接返回（防重入）
          置 adjustingMarkers = true
          对组内每个成员 setPosition(newpos)
          置回 false
```

`adjustingMarkers` 这个布尔护栏是本模块最值得学习的细节：Qt 直连信号是同步的，`setPosition` 内部又发 `positionChanged`，广播循环里每个 `setPosition` 都会再次进入 `markerMoved`。没有护栏就是无限递归；有了护栏，重入的调用在第一行就被挡掉，一次拖动恰好广播一轮。

#### 4.3.3 源码精读

**组的全部状态。** [markergroup.h:L8-L39](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markergroup.h#L8-L39)：一个组只有四个字段——防重入护栏 `adjustingMarkers`、组域 `domain`、成员集合 `std::set<Marker*> markers`、组编号 `number`。它不是 Savable（组本身不进 JSON，靠成员各自记住组号，见下）。

**入组。** [markergroup.cpp:L10-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markergroup.cpp#L10-L35)：`add` 先做 `applicable` 检查；再处理「一脚两船」——marker 若已在别的组，先从旧组移出（L17–L20）；然后连四路信号（位置、类型、域、销毁）；**L27–L29 把新成员位置吸附到组内现有位置**——这是「编组即对齐」的交互承诺；最后 `setGroup` 回写反向指针并插入集合。

**出组与销毁。** [markergroup.cpp:L37-L59](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markergroup.cpp#L37-L59)：`remove` 拆掉三路信号连接（`deleted` 那一路由对象销毁自动断开）、清反向指针、从集合删除；集合清空时发 `emptied` 信号。析构函数（L3–L8）逐个移出成员，保证销毁组不会留下悬空指针。

**资格检查。** [markergroup.cpp:L61-L78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markergroup.cpp#L61-L78)：第一个成员入组时把组的域定为其域；之后任何与组域不同的 marker 都被拒绝。L63 的 `isMovable()` 调用把所有自动类型挡在门外。

**防重入广播。** [markergroup.cpp:L80-L92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markergroup.cpp#L80-L92)：`markerMoved` 就是上面流程图里的实现——四行核心代码，注释明说「移动其他成员会再次触发本槽，用护栏变量检查」。注意它同步地一个一个 `setPosition`，因此整组在一次鼠标事件内完成对齐，不会出现半对齐的中间帧。

**自动清退。** [markergroup.cpp:L94-L99](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markergroup.cpp#L94-L99)：`checkMarker` 监听成员的 `typeChanged` 与 `domainChanged`——一旦某成员变成自动类型（不可移动）或换了域，立即被移出组。这就是「违规不入库、入库后违规自动退」的完整闭环。

**组的生命周期管理在 MarkerModel。**

- [markermodel.cpp:L144-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L144-L152)：`groupEmptied` 从集合删除空组并销毁，再让所有 marker 重建右键菜单（菜单里的可选组列表变了）。
- [markermodel.cpp:L159-L206](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L159-L206)：`createMarkerGroup`（自动找最小空闲编号）与 `addToGroupCreateIfNotExisting`（按编号找组，不存在则建）。
- 组的持久化走成员自身：[marker.cpp:L1347-L1349](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1347-L1349) 在 marker 的 JSON 里只存组编号；加载时 [marker.cpp:L1448-L1451](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L1448-L1451) 经 `addToGroupCreateIfNotExisting` 按编号重组——组对象不进 setup 文件，加载时按需重建。

**UI 入口两处。** 表格 Group 列（Ctrl 多选后右键编组，提示见 [markermodel.cpp:L284](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/markermodel.cpp#L284)），以及 marker 右键菜单的 "Add to linked group" / "Remove from linked group"——菜单构建代码在 [marker.cpp:L977-L1022](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Marker/marker.cpp#L977-L1022)，它遍历 `model->getGroups()` 过滤出 `applicable` 的组。

#### 4.3.4 代码实践

**实践目标：** 亲手编一组联动 marker，再用源码推理「拆掉护栏会怎样」。

**操作步骤：**

1. 承接前一会话：确认图上有两条 Trace（例如 VAT-10+ 的 S11 与 S21，或滤波器的 S11/S21）。
2. 在两条 Trace 上各建一个 Manual marker（编号 1、2）。
3. 在 marker 表格的 Group 列，Ctrl 点选两行后右键编组（或分别右键 marker 符号 → "Add to linked group" 选同一编号）。观察：编组瞬间两个 marker 跳到同一 X 位置（`add` 的吸附逻辑）。
4. 拖动其中一个符号——两个 marker 一起滑动，两行 Data 列同步刷新。
5. 把其中一个 marker 的 Type 改成 Maximum——它立刻**被请出组**（`checkMarker` 触发），此后拖另一个不再带动它。
6. 保存 setup（`.setup` 文件）再用文本编辑器打开，找到两个 marker 的 JSON，确认各有一个相同的 `"group"` 数字字段。
7. 纯代码推理：假设删掉 `markerMoved` 里的 `adjustingMarkers` 护栏，写出拖动一次会发生什么。

**需要观察的现象：** 编组即对齐；拖动任意一个全组跟随；改成自动类型即退组；setup 里组以「成员各自记编号」的形式存在，没有独立的组对象。

**预期结果：** 第 7 步的推理结论应是：`markerMoved` 广播的第一个 `setPosition` 同步发 `positionChanged`，再次进入 `markerMoved`，无限递归直至栈溢出（Qt 直连没有排队缓冲）。护栏把广播压成恰好一轮。GUI 操作效果待本地验证。

#### 4.3.5 小练习与答案

**练习 1：** 一个 marker 能同时属于两个组吗？

**答案：** 不能。`add` 的 L17–L20 保证入组前先从旧组移出；且 marker 身上只有一个 `group` 反向指针（marker.h L245）、JSON 里只有一个组号字段。若想「换组」，直接加入新组即可，旧的隶属关系自动解除。

**练习 2：** 组里的 Delta marker 被拖动时，它的「参照 marker」（不在组内）会跟着动吗？

**答案：** 不会。组广播只调成员的 `setPosition`，而 Delta 的读数依赖参照 marker 的 `rawDataChanged` 信号触发本 marker 的 `update()`（marker.cpp L1190）。参照 marker 自己不动、数据不变，就不发信号；Delta marker 自身位置变化经 `constrainPosition → traceDataChanged` 链路更新自己的 `data` 与显示。两组机制（编组联动、Delta 参照）作用在信号的不同环节，互不干扰。

**练习 3：** 为什么 `MarkerGroup` 不像 Marker 一样实现 `Savable`？

**答案：** 因为组的存在可以完全由成员推导：每个成员的 JSON 记了组编号，加载时 `addToGroupCreateIfNotExisting`（markermodel.cpp L191–L206）按编号现查现建。给组单独做持久化反而要处理「成员与组的保存顺序」「组号冲突」等额外问题。这是「能推导的状态不落盘」的典型取舍。

## 5. 综合实践

**任务：为 VAT-10+ 衰减器测量搭建一个「读数工作台」，用三类 marker 加一个编组覆盖本讲全部知识。**

1. **准备**：无硬件启动 GUI，导入 `Documentation/Measurements/Mini-circuits_VAT-10+.s2p`，把 S21 与 S11 拖到同一张 XY 图（纵轴 dB）。
2. **自动搜索**：在 S21 上建 marker，Type 依次试 `Maximum`、`Peak Table`、`Flatness`，体会「单值搜索 / 多值列表 / 区间统计」三种形态；Flatness 的两个 helper 拖到扫描两端，读出衰减器的平坦度（max Δ+ / max Δ−）。
3. **Delta**：再建一个 Delta marker，参照选第一个 marker，验证 dB 差读数与两行读数之差一致。
4. **编组**：在 S11 与 S21 上各放一个 Manual marker 编成一组，拖到任一频点，读出该频率下 S11（回波损耗）与 S21（插损）——一组拖动同时回答「反射多少、透过多少」。
5. **插值验证**：打开偏好设置的 marker 插值选项，把组拖到非采样点位置，按 4.1.4 的方法手算一次插值并核对。
6. **收尾**：把以上全部保存进 `.setup` 文件，用文本编辑器检查 marker 与 group 的 JSON 字段，再重新加载验证恢复。

**验收标准：** 能不看资料说出——插值代码位于 `tracemath.cpp:getInterpolatedSample`；峰值判定靠「门限 + 3 dB 谷深」两把尺子；编组靠 `positionChanged` 广播加 `adjustingMarkers` 护栏。

## 6. 本讲小结

- Marker 的本质是「Trace 指针 + X 位置」，读数永远现算：`Trace::dataChanged → traceDataChanged → interpolatedSample`，且只在值变化时向下传播刷新。
- 「marker 落在两采样点之间怎么读」的答案是 `TraceMath::getInterpolatedSample`（tracemath.cpp L142–L166）：二分定位相邻两点后做**复数域线性插值**；而默认偏好 `Marker.interpolatePoints=false` 会先把位置吸附到最近采样点，插值分支默认并不生效。
- 13 种 marker 类型共享一个 `update()` 分发器；重活（极值、峰值）在 Trace 侧的 `findExtremum`/`findPeakFrequencies`，复杂类型用 helper marker 呈现多点结果，helper 数量随搜索结果动态增删。
- 峰值判定是「高度过门限 + 回落超 3 dB 谷深」的状态机；TOI 由四个 helper 电平按 \((3\bar{P}_{fund}-\bar{P}_{IM})/2\) 外推；类型可用性受域/反射/Live-SA 三重闸门约束。
- MarkerModel 身兼三职：树形表格模型（helper 为子行）、marker 容器、组的工厂与回收站；模式层只在每次扫描完成时批量 `updateMarkers()`。
- MarkerGroup 用 `positionChanged` 广播实现跨 Trace 同步，靠 `adjustingMarkers` 布尔护栏阻断同步信号的递归回环，成员违规（变自动型/换域）时自动清退，空组自动销毁，持久化则完全由成员的组号字段推导。

## 7. 下一步学习建议

- **下一讲 u8-l4（Touchstone 与 CSV 导入导出）**：本讲多次导入 `.s2p` 文件，下一讲讲清这类文件本身的格式与解析代码。
- 之后 **u8-l5（Trace 数学运算框架）** 将解释 `lastMath` 这条数学链的构造——本讲 marker 读到的正是链末端的输出，学完后你能完整回答「marker 读到的数据到底经历了哪些加工」。
- 想继续挖掘本讲细节，推荐三处延伸阅读：`Trace::getNoise` 与 `getGroupDelay`（trace.cpp L1715–L1768，Noise/群延迟两种 marker 格式的数据来源，群延迟用相位解卷绕加线性回归求导）；`Marker::fromJSON` 与 `MarkerModel::fromJSON` 的两遍加载（markermodel.cpp L390–L430，理解为什么 Delta 参照必须第二遍才能接上）；以及 `applicableFormats`（marker.cpp L170–L319）这张「域 × 类型 → 可用格式」的完整闸门矩阵。
