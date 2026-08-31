# Trace 与 TraceModel：测量数据的容器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Trace` 类内部的数据存储结构（`x + 复数 y` 的有序向量）、四种数据来源（Live/File/Math/Calibration）与三种 live 更新策略（Overwrite/MaxHold/MinHold）。
2. 解释 `Trace` 的更新通知机制：一次 `addData()` 从写入向量到最终触发图表重绘，中间经过哪几个 Qt 信号、被谁转发。
3. 理解 `TraceModel` 的双重身份——它既是界面右侧 Trace 列表对应的 `QAbstractTableModel`，又是所有 Trace 的容器和测量数据的分发中枢。
4. 区分 X 轴的五种类型（频率/时间/距离/功率/零扫宽时间）与 Y 轴的类型矩阵，理解「数据源（VNA/SA）× X 轴类型」如何约束一张图能显示什么。
5. 独立画出一条 Trace 从创建到删除的完整生命周期图，并标注每一步触发的信号与槽。

本讲是单元八的第一篇：Trace 体系是 GUI 侧一切可视化、Marker、数学运算、导出功能的共同地基。

## 2. 前置知识

阅读本讲前，你需要（对应前置讲义 u7-l1）：

- **Qt 信号与槽**：Qt 的观察者模式实现。`emit someSignal(args)` 会依次调用所有连接到该信号的槽函数。本讲的整个刷新机制就是一条信号链。
- **QAbstractTableModel**：Qt Model/View 框架中表格模型的基类。实现 `rowCount()`、`columnCount()`、`data()` 三个虚函数，即可让 `QTableView` 自动显示内容；增删行前后必须调用 `beginInsertRows()`/`endInsertRows()` 等函数通知视图。
- **S 参数与线性复数**：`S11`、`S21` 等测量值在本项目中一律以**线性幅度复数** `std::complex<double>` 存储（不是 dB）。dB 只是绘图时的换算结果。
- **VNA 测量数据入口**：u7-l1 讲过，VNA 模式在 `NewDatapoint` 槽中完成平均与校准后，把数据交给 `TraceModel::addVNAData()`。本讲就从这里向下钻。

如果对「数据从驱动信号到界面曲线」的完整路径已经模糊，建议先复习 u7-l1 的数据上行链路一节。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h` | `TraceMath` 基类：定义 `Data` 样本结构、`DataType` 域枚举、`outputSamplesChanged` 等核心信号。`Trace` 继承它，数学运算节点也继承它 |
| `Software/PC_Application/LibreVNA-GUI/Traces/trace.h` | `Trace` 类声明：数据来源、live 策略、信号列表、私有成员 |
| `Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp` | `Trace` 实现：构造/析构、`addData` 的有序插入算法、live 参数切换、hash 持久化 |
| `Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.h` | `TraceModel` 声明：表格列枚举、数据源枚举、信号与槽 |
| `Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp` | `TraceModel` 实现：增删 Trace、`addVNAData`/`addSAData` 分发、激励端口反查 |
| `Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h` | `Axis`/`XAxis`/`YAxis` 声明：轴类型枚举与坐标变换接口 |
| `Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp` | 轴实现：样本→坐标换算、可用 Y 类型矩阵、刻度生成 |
| `Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp` | （辅助阅读）`TracePlot` 图基类：展示 Trace 的信号最终如何被图表消费 |

另外会引用两处调用方代码用于印证调用链：`VNA/vna.cpp` 与 `Util/util.h`。

## 4. 核心概念与源码讲解

### 4.1 Trace 数据与信号

#### 4.1.1 概念说明

一条 **Trace 就是一条曲线**：它有自己的名字、颜色、数据来源，以及一个按 X 坐标升序排列的样本向量。GUI 界面上你在图例里看到的每一条线、Smith 图上的每一圈、瀑布图的每一行，背后都是一个 `Trace` 对象。

理解 `Trace` 要抓住三个要点：

1. **样本是「X + 复数 Y」**。X 是频率（Hz）、时间（s）、功率（dBm）等自变量；Y 是线性复数测量值。dB、相位、阻抗等都是绘图时从复数换算出来的「视图」，不落盘存储。
2. **Trace 有四种数据来源**（`Source` 枚举）：Live（设备实时数据）、File（Touchstone/CSV 导入）、Math（由其他 Trace 按表达式计算得出）、Calibration（校准测量专用）。同一份存储结构服务四种来源。
3. **Trace 同时是数学链的一环**。`Trace` 继承自 `TraceMath`，并把「自己」作为数学运算链的第 0 级——没有启用数学时，链的末端就是 Trace 本身；启用了 TDR、表达式等运算后，链末端变成最后一个运算节点，外界读到的「这条 Trace 的数据」其实是链末端的输出。这是下一讲（u8-l5）的伏笔，本讲只需记住：**外界对 Trace 的读取一律委托给 `lastMath`**。

#### 4.1.2 核心流程

一条 Live Trace 收到一个新数据点的流程：

```text
设备驱动测量信号
  → VNA 模式（校准/平均之后）调用 TraceModel::addVNAData(d, datatype, deembedded)
  → TraceModel 遍历所有 Live 且未暂停的 Trace
      按 liveParameter（如 "S11"）在测量 map 中查值，查不到则跳过
  → Trace::addData(d, domain, Z0, index)
      ① 若 domain 与当前不同：先 clear()，再 emit typeChanged
      ② 加锁，按 x 用 lower_bound 定位插入/替换位置
      ③ 三种情况：追加到末尾 / 同 x 替换（按 Overwrite|MaxHold|MinHold）/ 中间插入
      ④ emit outputSamplesChanged(index, index+1)
  → （数学链末端）outputSamplesChanged 被转发为 Trace::dataChanged
  → 图表（TracePlot::triggerReplot）重绘
```

其中「同 x 替换」的三种策略语义：

- **Overwrite**：新值无条件覆盖旧值（普通扫描，同一频点每圈刷新）。
- **MaxHold**：仅当新幅度 \( |y_{new}| > |y_{old}| \) 时覆盖——频谱仪常用的峰值保持。
- **MinHold**：仅当 \( |y_{new}| < |y_{old}| \) 时覆盖——最小值保持。

#### 4.1.3 源码精读

**样本结构与域枚举**（定义在基类 `TraceMath` 中，`Trace` 通过 `using Data = TraceMath::Data` 直接复用）：

[Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h:L57-L70](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L57-L70)

这段定义了 `Data`（`double x` + `std::complex<double> y`）和 `DataType`（Frequency/Time/Power/TimeZeroSpan/Invalid）。注意 `DataType` 描述的是 **X 轴的物理量**，而不是 Y 的内容——这是初学时最容易混淆的一点。

**数据真正存放的位置**是 `TraceMath` 的 protected 成员：

[Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h:L148-L156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L148-L156)

`std::vector<Data> data` 由 `QMutex dataMutex` 保护——因为绘制可能发生在数据仍在被写入时（后面讲并发时会更明显）。`Trace` 自己额外还有一份 `deembeddingData`（见 [trace.h:L290-L293](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.h#L290-L293)），用于同时保存去嵌入前后的两套数据，供用户一键切换。

**数据来源枚举与信号清单**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.h:L29-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.h#L29-L35)

四种来源：`Live`/`File`/`Math`/`Calibration`。

[Software/PC_Application/LibreVNA-GUI/Traces/trace.h:L204-L217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.h#L204-L217)

这是 `Trace` 的全部信号。重点记三个：`typeChanged`（域/结构变了，图表要检查是否还支持这条 Trace）、`dataChanged(begin, end)`（样本变了，重绘）、`deleted`（析构时发出，订阅者必须立刻断开与它的关系）。注意 `dataChanged` 是 `Trace` 自己声明的信号，而 `outputSamplesChanged` 是从 `TraceMath` 继承的——两者通过下面的转发机制衔接。

**构造函数——把自己装进数学链的第 0 级**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L21-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L21-L68)

构造函数做了四件关键的事：默认来源设为 `Live`、参数默认 `S11`；把 `{this, enabled=true}` 压入 `mathOps` 作为链首（第 45-47 行）；末尾在 `updateLastMath` 中建立信号转发；再挂两个自连接——`typeChanged` 时同步 `dataType` 并广播 `outputTypeChanged`，`outputSamplesChanged` 时把缓存的展开相位截断丢弃（相位展开缓存失效，见 [trace.cpp:L61-L67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L61-L67)）。

**析构函数**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L70-L77](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L70-L77)

析构第一行就 `emit deleted(this)`——这是一条「讣告」，让所有还持有该指针的对象（图表、Marker、以它为源的数学 Trace）先做清理；随后逐个删除数学链上的附加运算节点（第 0 级是自己，不能 delete）。

**addData 的有序插入算法（本模块最核心的 60 行）**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L94-L156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L94-L156)

逐段看：

- L95-L99：若新数据的 `domain` 与 Trace 当前域不同（例如从频率域切到零扫宽时间域），先 `clear()` 清空旧数据再换域，并 `emit typeChanged`——图表收到后会检查自己是否还支持这条 Trace。
- L103-L108：调用方给了 `index >= 0` 时按下标直接写（必要时 `resize` 扩容）。零扫宽/时间域数据按点号写入走这条路。
- L113-L117：未给 index 时，用 `lower_bound` 按 `x` 二分查找定位，保持向量严格按 X 升序。注释特意提醒：先算出 index 再插入，因为插入可能导致迭代器失效。
- L118-L144：三分支——`x` 比所有现存样本都大则 `push_back`（最常见、最快的扫描前进方向）；`x` 已存在则按 live 策略替换（L122-L140 的 switch，即 MaxHold/MinHold 判据 `abs(d.y) > abs(lower->y)`）；否则中间 `insert`（乱序到达的点）。
- L151-L155：若参考阻抗变化则再补一个 `typeChanged`；最后 `emit outputSamplesChanged(index, index+1)` 通知「第 index 到 index+1 之间的样本变了」（左闭右开区间）。

为什么必须保持有序？因为后续的 Marker 插值（`getInterpolatedSample`）、峰值搜索、`index(x)` 二分定位全都依赖「x 单调递增」这一不变量；而设备端数据并不总按序到达（分段扫描、多机同步、暂停恢复），所以插入算法必须自己维护顺序。

**SA 版重载与静态填充工具**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L158-L168](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L158-L168)

SA 数据的 addData 重载：记录 SA 设置（供 `getNoise` 换算噪声密度用），并在 `freqStart == freqStop` 时自动把域判为零扫宽时间。

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L334-L368](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L334-L368)

静态函数 `fillFromDatapoints`：把一批 `VNAMeasurement`（`map<QString, complex>` 以参数名为键）按名字分发到一组 Trace。这是「一条测量 → 多条 Trace」的批量入口。

**live 参数与判定函数**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L370-L382](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L370-L382)

`fromLivedata`：设置 live 策略与参数名；若参数形如 `S11`/`S22`（两位数字相同，即反射测量）则标记 `reflection = true`——这个标志后面会被「距离轴」用来把往返时间除以 2。

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L807-L823](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L807-L823)

两个静态判定：`isSAParameter` 匹配 `PORTx`（5 个字符），`isVNAParameter` 匹配 `Sxy`（3 个字符）或 `RawPort` 前缀（原始接收机读数）。字符串即协议——这正是 u3-l1 讲过的「以字符串为键的 map 适配任意端口数」在消费端的体现。

**读取一律委托 lastMath**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L1443-L1446](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1443-L1446)

`size()` 不是返回 `data.size()`，而是 `lastMath->numSamples()`。当链上挂了 TDR 这类会改变样本数的运算时，二者并不相等。同理 `minX()/maxX()/sample()` 也都先问 `lastMath`（[trace.cpp:L1492-L1508](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1492-L1508)、[trace.cpp:L1593-L1601](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1593-L1601)）。去嵌入激活时 `getSample/numSamples/getData` 会改读 `deembeddingData`（[trace.cpp:L1603-L1619](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1603-L1619)）。

**信号转发的关键一行**：

[Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp:L1230-L1251](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1230-L1251)

`updateLastMath` 在数学链末端变化时执行 `connect(lastMath, &TraceMath::outputSamplesChanged, this, &Trace::dataChanged)`（L1246）：**链末端（可能是 Trace 自己）的 `outputSamplesChanged` 一律被转发为 `Trace::dataChanged`**。这就把「原始数据写入」和「数学运算输出」统一成了同一个对外通知口径——图表只需要监听 `dataChanged`，不必关心链上有几个运算节点。

#### 4.1.4 代码实践

**实践：跟踪一条数据的信号链（源码阅读型，无需硬件）**

1. **实践目标**：亲手验证「`addData` → `outputSamplesChanged` → `dataChanged` → 重绘」这条链上的每一环，确认转发发生在哪一行。
2. **操作步骤**：
   - 在 [trace.cpp:L155](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L155) 处确认 `addData` 末尾发出的是 `outputSamplesChanged`（基类信号），不是 `dataChanged`。
   - 打开 [trace.cpp:L1246](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1246)，确认构造函数经 `updateLastMath` 建立的转发连接。
   - 打开 [traceplot.cpp:L70-L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L70-L83)，确认图表把 `Trace::dataChanged` 连到 `TracePlot::triggerReplot`。
   - （可选，需编译环境）在 `addData` 的 L155 前加一行 `qDebug() << "addData" << d.x;`，在 `TracePlot::triggerReplot` 里加 `qDebug() << "replot";`，编译运行并导入示例测量，观察日志中两者的交替。
3. **需要观察的现象**（若执行了可选项）：每来一个数据点，先出现一条 `addData` 日志，紧接着一次或多次 `replot`；`replot` 次数明显少于 `addData` 次数（图表有 `MinUpdateInterval` 限流，见 [traceplot.cpp:L792-L802](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L792-L802)）。
4. **预期结果**：三个源码位置连成一条完整链路；纯走读也可完成，日志部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把一条 live 类型为 `MaxHold` 的 S11 Trace 连续喂两个同频点数据 \( y_1 = 0.1 \)、\( y_2 = 0.5 \)，再喂 \( y_3 = 0.2 \)。最终该频点的值是多少？

**答案**：0.5。第一次写入 0.1；第二次 \( |0.5| > |0.1| \) 覆盖为 0.5；第三次 \( |0.2| < |0.5| \) 不覆盖。判据在 [trace.cpp:L127-L132](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L127-L132)。

**练习 2**：为什么 `addData` 用 `lower_bound` 维持有序，而不是简单 `push_back`？给出两个必须依赖有序性的后续功能。

**答案**：设备端数据不保证按 X 升序到达（分段扫描各段独立、多台设备同步拼装、暂停恢复后重扫部分点），而 Marker 插值 `getInterpolatedSample` 的线性插值与 `Trace::index(x)` 的二分查找都要求向量按 X 单调递增才能正确工作；此外 `findPeakFrequencies` 顺序扫描也隐含依赖有序输入。

**练习 3**：`Trace::size()` 为什么写成 `lastMath->numSamples()` 而不是 `data.size()`？

**答案**：Trace 是数学运算链的第 0 级，链末端（`lastMath`）才是对外呈现的数据。挂上 TDR/DFT 等运算后，输出样本数（例如补零后的时域点数）与原始频域 `data.size()` 不同；去嵌入激活时末端读到的还会是 `deembeddingData` 的大小。统一委托 `lastMath` 保证对外口径一致。

### 4.2 TraceModel 容器

#### 4.2.1 概念说明

`TraceModel` 是所有 Trace 的「户籍管理局 + 数据分发台」，它同时扮演两个角色：

1. **表格模型**：继承 `QAbstractTableModel`，界面右侧的 Trace 列表（可见性、暂停、去嵌入、数学、名字五列）直接绑定它。勾一个眼睛图标、按一次暂停，都是对它的调用。
2. **容器与分发中枢**：持有 `std::vector<Trace*>`；VNA/SA 两种模式收到驱动数据后都调用它的 `addVNAData`/`addSAData`，由它把一个测量点按参数名分发给所有感兴趣的 Live Trace。

它还有第三个隐藏角色——**反向控制通道**：新增或暂停一条 Live Trace 会改变「哪些端口需要被激励」，`TraceModel` 通过 `requiredExcitation` 信号通知 VNA 模式重新配置扫描（这正是 u7-l1 提过的 `excitedPorts` 由 TraceModel 反向推导的实现位置）。

每个测量模式（VNA、SA）各持有一个自己的 `TraceModel` 实例，所以切换模式就是切换整套 Trace 集合。

#### 4.2.2 核心流程

**数据分发（VNA）**：

```text
addVNAData(d, datatype, deembedded):
  source = VNA; 记录接收时间
  for 每条 Trace:
    if 来源 != Live 或 已暂停: 跳过
    按 datatype 选 X:
      Frequency   → x = d.frequency
      Power       → x = d.dBm
      TimeZeroSpan→ x = d.us / 1e6, 且 index = d.pointNum
    若 d.measurements 里没有该 Trace 的 liveParameter: 跳过
    deembedded ? addDeembeddingData(...) : addData(td, datatype, d.Z0, index)
```

**增删 Trace 的模型/视图协议**：

```text
addTrace(t):
  beginInsertRows → connect(nameChanged/pauseChanged/deembeddingChanged → 刷新表格)
  → push_back → endInsertRows → t->setModel(this)
  → emit traceAdded(t)        // 图表基类监听它来认识新 Trace
  → emit requiredExcitation() // VNA 模式监听它来重算激励端口

removeTrace(index):
  beginRemoveRows → delete trace（析构 emit deleted(讣告)）→ erase → endRemoveRows
  → emit traceRemoved(trace)  // 注意：此时指针已失效，仅用于比对清理
  → emit requiredExcitation()
```

**激励端口反查**：`PortExcitationRequired(port)` 遍历所有「Live 且未暂停」的 Trace，取出 live 参数 `Sxy` 的第二位数字 `y`（激励源端口），与查询端口比较——`requiredExcitation` 到设备重配的闭环就此完成。

#### 4.2.3 源码精读

**类声明与列定义**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.h:L13-L33](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.h#L13-L33)

`TraceModel : public QAbstractTableModel, public Savable`——一个是表格模型身份，一个是 u2-l3 讲过的 JSON 持久化接口。`ColIndex` 枚举定义了列表的五列（可见/暂停/去嵌入/数学/名字）；`DataSource` 枚举（VNA/SA/Unknown）记录这套 Trace 当前由哪种模式喂数据——这个标志在下一节轴类型里会再次出现，用于约束可用轴类型。

**信号与槽**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.h:L69-L79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.h#L69-L79)

六个信号里 `traceAdded`/`traceRemoved` 面向图表，`requiredExcitation` 面向模式层，`SpanChanged` 面向图表的 X 轴范围；三个槽 `clearLiveData`/`addVNAData`/`addSAData` 是模式层的数据入口。

**addTrace——注册与双重通知**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L27-L45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L27-L45)

先按 QAbstractTableModel 协议包裹 `beginInsertRows`/`endInsertRows`；接着为 `nameChanged`/`pauseChanged`/`deembeddingChanged` 三个 Trace 信号各挂一个刷新表格的 lambda（注意 lambda 里发的是 `dataChanged(createIndex(0,0), createIndex(traces.size()-1, ColIndexLast-1))`——全表刷新，简单但不精细）；最后 `setModel(this)` 建立反向指针，`traceAdded` 与 `requiredExcitation` 先后发出。

**removeTrace——删除顺序的讲究**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L47-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L47-L58)

注意时序：L52 `delete trace`（此刻 Trace 析构函数已 `emit deleted`，图表已自行摘除）→ L53 从容器 erase → L55 才 `emit traceRemoved(trace)`。**`traceRemoved` 携带的是一个已析构对象的指针**，接收方只能拿它做指针比对和清理，绝不能解引用。析构函数（[tracemodel.cpp:L19-L25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L19-L25)）逐条 delete，保证容器析构时无泄漏。

**表格 data() 的角色分发**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L138-L211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L138-L211)

`data()` 按列返回：前四列在 `Qt::DecorationRole` 下返回图标（可见/暂停/去嵌入/数学开关），且各自带能力门槛——`canBePaused()` 为假（File 来源）不显示暂停图标、`deembeddingAvailable()` 为假不显示去嵌入图标、`hasMathOperations()` 为假不显示数学图标；名字列在 `DisplayRole` 下返回文本、`ForegroundRole` 下返回 **Trace 的颜色**——列表里的名字直接用曲线同色显示，一眼对应。

**getLiveTraces / PortExcitationRequired——反向控制通道**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L213-L242](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L213-L242)

`PortExcitationRequired` 对每条「Live 且未暂停」的 Trace 取 `param[2]`（`Sxy` 的第二位，即激励源端口编号）转成整数与查询端口比较。两点值得注意：该解析只适用于端口号为一位数的 `S` 参数（这是为双端口设备写的简化代码）；对 `RawPort` 类参数，`param[2]` 不是数字、`toInt()` 返回 0，不会匹配任何 ≥1 的端口。

调用方闭环在 VNA 模式：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L1363-L1376](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1363-L1376)

`VNA::ExcitationRequired`（由 [vna.cpp:L90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L90) 连接到 `requiredExcitation`）逐端口比对「Trace 需要的激励」与「当前扫描设置里的 `excitedPorts`」，不一致则触发 `SettingsChanged()` 走一遍完整的防抖重配。于是「在图上多勾一条 S22」最终会导致设备端改用端口 2 激励——数据链路完全反转了一次方向。

**addVNAData——分发主体**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L286-L323](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L286-L323)

对照 4.2.2 的伪代码逐行可读。三个细节：`DataType` 不在三种已知域之列时直接 `return`（整包丢弃）；`lastSweepPosition = td.x` 记录扫频光标位置；`d.measurements.count(t->liveParameter())` 查不到就 `continue`——一次双端口测量携带 S11/S21/S12/S22 四个值，S11 Trace 只取自己的那份，其余跳过。调用点在 [vna.cpp:L1064-L1068](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1064-L1068)：先喂原始数据，若去嵌入可用再喂一份 `deembedded=true` 的数据。

**addSAData 与扫频光标**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L325-L350](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L325-L350)

SA 版结构相同，区别只在 X 的取法：零扫宽（`freqStart == freqStop`）按点号写入时间，否则按频率写入；且调用的是 SA 版 `addData(td, settings, index)`（会记录 SA 设置供噪声换算）。

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L362-L371](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L362-L371)

`getSweepPosition()`：距上次收数据超过 1000ms 就返回 NaN。图表用它画「当前扫到哪了」的竖线光标（[traceplot.cpp:L216-L226](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L216-L226)），数据流一停光标就自动消失，不会留一条误导性的冻结线。

**持久化**：

[Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L253-L274](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L253-L274)

`fromJSON` 先清空旧 Trace，再逐条 `new Trace()` + `fromJSON` + `addTrace`；全部创建完后再统一 `resolveMathSourceHashes()`——因为 Math 来源的 Trace 引用的是**其他 Trace 的哈希**而非指针，而那个被引用的 Trace 可能在本条之后才被创建（保存 setup 时指针无意义，故用哈希标识，见 [trace.h:L147-L154](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.h#L147-L154) 的注释；哈希由 JSON 序列化文本经 `std::hash` 得到，见 [trace.cpp:L1081-L1093](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1081-L1093)）。

#### 4.2.4 代码实践

**实践：读懂一次分发，画出调用链（源码阅读型）**

1. **实践目标**：以一条 S11 Trace 为例，写出一个双端口测量点到达后它的取值路径，并验证「不相关参数被跳过」。
2. **操作步骤**：
   - 假设驱动上报 `d.measurements = {"S11": a, "S21": b, "S12": c, "S22": e}`，`d.frequency = 1e9`，`datatype = Frequency`。
   - 对照 [tracemodel.cpp:L290-L322](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L290-L322)，写下 S11 Trace 得到的 `td.x`、`td.y`、`index` 三个值分别是多少。
   - 再假设该 Trace 的 liveParameter 被改成 `PORT1`（SA 参数）：同样这包 VNA 数据里会发生什么？
   - 检查 [vna.cpp:L1064](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1064) 的调用者上下文，确认 `addVNAData` 的 `datatype` 参数从哪里来（提示：沿 `NewDatapoint` 槽内的分支查）。
3. **需要观察的现象**：第二问中，由于 VNA 测量的 map 里没有 `PORT1` 键，`d.measurements.count(...)` 为 0，该 Trace 被 `continue` 跳过、一个字节都不会写入。
4. **预期结果**：得到一张「测量点 → 各 Trace 取值」的小表；S11 Trace 得到 `x=1e9, y=a, index=-1`（频率域不指定下标）。最后一小问**待本地验证**（需要沿 vna.cpp 的 `NewDatapoint` 阅读约 30 行）。

#### 4.2.5 小练习与答案

**练习 1**：`addTrace` 末尾为什么要 `emit requiredExcitation()`？请说出这个信号的两类触发场景。

**答案**：新增（或删除/暂停/恢复）Live Trace 会改变「哪些端口必须被激励」才能喂饱所有可见曲线。两类场景：用户在 Trace 列表新增/删除 Live Trace（`addTrace`/`removeTrace`）；用户切换某条 Trace 的暂停状态（`togglePause`，[tracemodel.cpp:L91-L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L91-L102)）。两种情况最终都由 `VNA::ExcitationRequired` 比对后按需触发 `SettingsChanged()`。

**练习 2**：`traceRemoved(Trace*)` 信号的接收方能否在槽里调用 `t->name()`？为什么？

**答案**：不能。`removeTrace` 先 `delete trace` 再 `emit traceRemoved(trace)`，信号携带的是悬空指针；接收方只能做指针比对/从自己的容器中摘除，不能解引用。对象在析构前已通过 `Trace::deleted` 信号完成「活性」清理。

**练习 3**：为什么 `getSweepPosition()` 里要判断 `lastReceivedData.msecsTo(t) > 1000` 就返回 NaN？

**答案**：扫频光标表示「数据此刻流到哪」。设备停止上报（暂停、断连、扫描结束）后 `lastSweepPosition` 不再更新，若照常返回旧值，图表会画一条静止的光标线，让用户误以为扫描仍在进行。超时返回 NaN 后图表自然不再绘制光标。

### 4.3 TraceAxis 轴类型

#### 4.3.1 概念说明

Trace 存的是「X + 复数 Y」，但屏幕上的图需要「横轴一个物理量、纵轴一个物理量」。`traceaxis.h/cpp` 就是这层翻译：

- **`Axis` 基类**：负责轴的通用属性——线性/对数、是否自动量程、范围、分割数、刻度列表，以及把数据坐标线性映射到屏幕像素的 `transform()`。
- **`XAxis`**：五种类型 `Frequency / Time / Distance / Power / TimeZeroSpan`。前四种直接对应 `TraceMath::DataType`，`Distance` 是特例——它不是数据自带的域，而是把时间乘以传播速度（含速率因子）换算出来的「派生轴」。
- **`YAxis`**：二十来种类型，全部是「复数样本 → 一个显示标量」的换算：dB、相位、VSWR、阻抗、电容、群时延、TDR 冲激/阶跃响应……同一份 S11 数据配上不同 Y 轴就是完全不同的图。

关键的约束关系是：**不是任意 X、Y 组合都有意义**。`getSupported(X类型, 数据源)` 返回该组合下允许的 Y 类型集合——例如频率轴可以配 dB、相位、阻抗，但不能配「阶跃响应」（那是时域专属）；SA 数据源只有 dBm 和 dBuV 两种 Y 可选。图表在 Trace 的 `typeChanged` 之后会调用这套判定决定「还支持这条 Trace 吗」（u8-l2 展开）。

#### 4.3.2 核心流程

**一次绘图中某个样本的坐标换算**：

```text
样本 (x, y) + (XAxis, YAxis)
  ① XAxis::sampleToCoordinate(data, t) → 数据横坐标 cx
       Frequency/Time/Power/TimeZeroSpan: cx = data.x（直通）
       Distance: cx = t->timeToDistance(data.x)（乘 c·v，反射再除 2）
  ② YAxis::sampleToCoordinate(data, t, sample) → 数据纵坐标 cy
       Magnitude: cy = 20·log10|y|；Phase: cy = arg(y)·180/π；……
  ③ 屏幕映射 Axis::transform(v, to_low, to_high)
       线性:  p = to_low + (v - rangeMin)/(rangeMax - rangeMin) · (to_high - to_low)
       对数:  先对 v 取对数再作同样线性映射
```

**刻度生成**（`Axis::updateTicks`）三分支：对数轴按十进制每倍频切 1/2/5 份；自动量程用 1-2-5 优美数字间隔并自动决定分割数；否则按用户给定 `divs` 均分。

#### 4.3.3 源码精读

**Axis 基类与两个子类的类型枚举**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h:L9-L33](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h#L9-L33)

`sampleToCoordinate` 是纯虚函数——每种轴必须说明「一个样本如何变成我这根轴上的坐标」；`transform/inverseTransform` 负责数据坐标与屏幕坐标的双向映射（后者供鼠标点击反查数据点使用）。

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h:L37-L44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h#L37-L44)

X 轴五种类型。注意它们与 `TraceMath::DataType`（Frequency/Time/Power/TimeZeroSpan）几乎一一对应，只多出 `Distance`。

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h:L64-L89](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.h#L64-L89)

Y 轴类型分四组：S 参数基本量（dB/相位/VSWR/实部/虚部）、派生量（阻抗/电阻/电抗/电容/电感/品质因数/群时延）、TDR 量（冲激实部/冲激幅度/阶跃/阻抗）。`Disabled` 表示该图不启用这条 Y 轴（双 Y 轴图中常见）。

**X 轴换算——Distance 的特殊性**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L502-L514](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L502-L514)

除 `Distance` 外全部直通 `data.x`；Distance 则调用 `t->timeToDistance(data.x)`——必须拿到 Trace 指针，因为换算依赖该 Trace 的速率因子和反射标志。换算公式在 [trace.cpp:L830-L838](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L830-L838)：

\[ d = \frac{t \cdot c \cdot v}{2} \quad (\text{反射测量，} v \text{ 为速率因子}) \]

除以 2 是因为反射测量的时间轴上是**往返**传播。

**X 轴 set() 的兜底**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L516-L553](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L516-L553)

若传入 `max <= min`（无效范围），按轴类型取设备能力兜底：频率轴取 `info.Limits.VNA.minFreq/maxFreq`（`DeviceDriver::getInfo`，即 u3-l1 讲的能力协商）、功率轴取功率上下限、时间/距离轴用固定默认。轴配置错误不会导致崩溃，只会回退到全量程。

**Y 轴换算——复数到标量的全部翻译**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L103-L167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L103-L167)

每个 case 一种换算，举三例（公式可在 `Util/util.h` 中核对，如 `SparamTodB = 20*log10(abs(d))`，见 [util.h:L30-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L30-L35)）：

- `Magnitude`：\( 20\log_{10}|y| \)（dB）；
- `Phase`：\( \arg(y) \cdot 180/\pi \)（度）；
- `AbsImpedance`：先 \( Z = Z_0\,\dfrac{1+y}{1-y} \)（[util.cpp:L64-L70](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp#L64-L70)），再取模。

三个 case 依赖 Trace 指针：`UnwrappedPhase`/`GroupDelay` 要查 Trace 的缓存/插值，`Step`/`Impedance` 要取阶跃响应（`t->sample(sample, true)`）。这是 `sampleToCoordinate` 签名里带 `Trace *t` 和 `unsigned int sample` 的原因。

**可用性矩阵——两道闸门**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L427-L440](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L427-L440)

第一道闸门（X 轴层面）：VNA 数据源允许全部五种 X 类型；**SA 数据源只允许 Frequency**；其余返回 false。

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L319-L364](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L319-L364)

第二道闸门（Y 类型层面），整理成矩阵：

| 数据源 | X 轴类型 | 允许的 Y 类型 |
|---|---|---|
| VNA | Frequency / Power / TimeZeroSpan | Magnitude、MagnitudeLinear、Phase、UnwrappedPhase、VSWR、Real、Imaginary、AbsImpedance、SeriesR、Reactance、Capacitance、Inductance、QualityFactor、GroupDelay |
| VNA | Time / Distance | ImpulseReal、ImpulseMag、Step、Impedance |
| SA | Frequency / TimeZeroSpan | Magnitude（dBm）、MagnitudedBuV |

物理直觉很清楚：频域复数才能谈阻抗/相位；时域 TDR 曲线只能谈冲激/阶跃/阻抗；SA 数据没有相位，只剩两种幅度单位（`Unit` 函数对 SA 源把 Magnitude 的单位写成 dBm 而非 dB，见 [traceaxis.cpp:L223-L257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L223-L257)）。

**屏幕映射**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L616-L624](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L616-L624)

`transform`/`inverseTransform` 都委托 `Util::Scale`（模板函数，实现见 [util.h:L15-L28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L15-L28)），线性映射：

\[ p = to_{low} + \frac{v - range_{min}}{range_{max} - range_{min}} \cdot (to_{high} - to_{low}) \]

**刻度生成**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L442-L460](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L442-L460)

`updateTicks()` 三分支：`log` 走十进制刻度、`autorange` 走 1-2-5 自动间隔、否则按用户分割数均分。此外 Y 轴还有一个可选的「刻度主从」机制（[traceaxis.cpp:L416-L425](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L416-L425)）：双 Y 轴图的右轴刻度可映射自主轴位置，让左右网格线对齐。

**反向换算（CSV 导入的入口之一）**：

[Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp:L366-L394](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L366-L394)

`reconstructValueFromYAxisType` 是换算的逆过程：CSV 文件里只有 Real/Imag 两列（或 Magnitude/Phase 两列）时，用它重建复数 `y`。`Trace::fillFromCSV`（[trace.cpp:L259-L332](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L259-L332)）按表头命名约定（`traceName_real`、`traceName_Magnitude` 等）定位列并调用它。

#### 4.3.4 代码实践

**实践：手工演算一个样本的完整坐标换算（纸笔即可）**

1. **实践目标**：给定一个样本和一套轴配置，算出它应绘制到屏幕的哪个像素，从而吃透 `sampleToCoordinate` + `transform` 两级换算。
2. **操作步骤**：
   - 设样本 \( x = 1\,\text{GHz} \)，\( y = 0.1 + 0.2j \)（线性复数）；X 轴 `Frequency`、线性、范围 0–2 GHz，绘图区宽 400 像素（`to_low=0, to_high=400`）；Y 轴 `Magnitude`、范围 −40 dB–0 dB，高 300 像素。
   - 按公式算 `cy = 20·log10|y|`（\(|y| = \sqrt{0.01+0.04} \approx 0.2236\)）。
   - 分别代入 4.3.2 的屏幕映射公式求像素坐标。
   - 对照源码核验每一步：dB 换算见 [util.h:L30-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L30-L35)，屏幕映射见 [traceaxis.cpp:L616-L624](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L616-L624)。
   - （可选）若 X 轴换成 `Distance`、Trace 速率因子 0.66 且为反射测量，重算横坐标。
3. **需要观察的现象**：无（纯纸笔演算）；若想验证，可在 u1-l3 构建出的 GUI 中导入 Touchstone，开一张 XY 图读取光标位置做粗略比对。
4. **预期结果**：\( cy = 20\log_{10} 0.2236 \approx -13.0\,\text{dB} \)；像素 \( px = 200 \)，\( py = \frac{-13-(-40)}{40} \cdot 300 = 202.5 \)（自上而下度量时注意 Y 方向通常取反）。换 Distance 时 \( d = t·c·0.66/2 \)。像素方向与作图原点细节**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一张图表的数据源为 SA，用户想把 X 轴设成 `Time`（TDR 时间轴），`XAxis::isSupported` 会怎么回答？为什么 SA 本该支持的「零扫宽时间轴」不算 `Time`？

**答案**：返回 false——SA 源下只有 `Frequency` 被允许（[traceaxis.cpp:L427-L440](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L427-L440)）。SA 的零扫宽走的是 `TimeZeroSpan`（按点号写入的实测时间序列），与 `Time`（对频域数据做 IFFT 得到的 TDR 时域）是两个概念；后者需要相位信息，SA 测量没有相位。

**练习 2**：为什么 `Distance` 轴的 `sampleToCoordinate` 必须拿到 `Trace *t`，而 `Frequency` 轴不需要？

**答案**：Distance 是派生量：时间→距离需要该 Trace 的速率因子 `vFactor`（不同介质不同），且反射测量要除以 2（往返），这两个信息都存在 Trace 对象里（`timeToDistance`，[trace.cpp:L830-L838](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L830-L838)）。Frequency 只是直通 `data.x`，与具体 Trace 无关。

**练习 3**：VNA 源 + `Time` 轴下，`getSupported` 允许哪些 Y 类型？其中 `Impedance` 类型的换算为什么还要再判断 `abs(step) < 1.0`？

**答案**：允许 ImpulseReal、ImpulseMag、Step、Impedance 四种（[traceaxis.cpp:L342-L348](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L342-L348)）。`Impedance` 由阶跃响应经 \( Z = Z_0(1+s)/(1-s) \) 换算，该公式只在 \( |s| < 1 \)（即反射系数不超过 1 的无源情形）下有意义，越界时落入 break 返回 0.0，避免除零/发散（[traceaxis.cpp:L151-L160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L151-L160)）。

## 5. 综合实践：Trace 生命周期图（本讲主打任务）

**任务**：写一份 Trace 生命周期图，覆盖「创建 → 添加到 TraceModel → 被图表订阅 → 接收数据 → 通知图 → 删除」六个阶段，**每一步标注触发的 Qt 信号与响应的槽函数名**。这是本讲的收官实践，完成后你就把 4.1、4.2 两条链缝合成了一张全景图。

**操作步骤**：

1. 白纸或绘图工具上画六个方框，按上述顺序排列。
2. 对照下列源码锚点逐步填写（先自己填，再核对）：

| 阶段 | 关键代码 | 发出的信号 | 谁在监听（槽） |
|---|---|---|---|
| ① 创建 | `new Trace("S11", color, "S11")`，[trace.cpp:L21-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L21-L68) | 构造末尾 `fromLivedata` 内 `emit typeChanged`（自连接同步 `dataType`，再 `outputTypeChanged`） | Trace 自身的 lambda（[trace.cpp:L57-L60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L57-L60)） |
| ② 添加 | `TraceModel::addTrace(t)`，[tracemodel.cpp:L27-L45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L27-L45) | `traceAdded(t)`、`requiredExcitation()` | `TracePlot::newTraceAvailable`；`VNA::ExcitationRequired`（[vna.cpp:L90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L90) 连接） |
| ③ 订阅 | `TracePlot::newTraceAvailable(t)`（[traceplot.cpp:L775-L782](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L775-L782)）与用户勾选后的 `enableTrace`（[traceplot.cpp:L70-L98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L70-L98)） | 无（只建立连接） | 注册 `deleted/nameChanged/typeChanged`；勾选后注册 `dataChanged` 等 → `triggerReplot` |
| ④ 接收数据 | `TraceModel::addVNAData` → `Trace::addData`（[tracemodel.cpp:L286-L323](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L286-L323)、[trace.cpp:L94-L156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L94-L156)） | `emit outputSamplesChanged(index, index+1)` | 经构造时 `updateLastMath` 的连接转发为 `Trace::dataChanged`（[trace.cpp:L1246](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1246)） |
| ⑤ 通知图 | 转发后的 `Trace::dataChanged(begin, end)` | （同上信号） | `TracePlot::triggerReplot` → 限流后 `replot()`（[traceplot.cpp:L792-L802](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp#L792-L802)）；同时以它为源的 Math Trace 也会被调度重算（[trace.cpp:L738-L760](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L738-L760)） |
| ⑥ 删除 | `TraceModel::removeTrace` → `delete trace`（[tracemodel.cpp:L47-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L47-L58)） | 析构内 `emit deleted(this)`（[trace.cpp:L70-L77](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L70-L77)）→ 随后 model 发 `traceRemoved(trace)`、`requiredExcitation()` | `TracePlot::traceDeleted`（摘除并重绘）；以它为源的 Math Trace 的 `mathSourceTraceDeleted` |

3. 在图上用另一种颜色标出**反向流**：`requiredExcitation` → `VNA::ExcitationRequired` → `PortExcitationRequired` → `SettingsChanged` → 设备重配（[vna.cpp:L1363-L1376](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1363-L1376)）。
4. **检验成果**：合上源码回答两个问题——(a) 删除阶段为什么 `traceRemoved` 里不能解引用指针？(b) 若这条 S11 是某条 Math Trace 的源，删除它时 Math Trace 靠哪个信号自愈？（答案分别在 4.2.5 练习 2 与上表第 ⑥ 行。）

**预期产出**：一张含六个阶段、两种颜色（数据流/控制流）的信号链图，以及两个自测问题的口头答案。全程无需硬件与编译。

## 6. 本讲小结

- **Trace = 有序样本向量 + 元信息**：每个样本是 `double x + complex y`，向量按 X 升序由 `addData` 的 `lower_bound` 插入算法维护；Overwrite/MaxHold/MinHold 三种 live 策略决定同频点新旧值的取舍。
- **Trace 有四种数据来源**（Live/File/Math/Calibration），对外读取一律委托数学链末端 `lastMath`——Trace 自己就是链的第 0 级，这是数学运算框架（u8-l5）的入口。
- **通知机制是一条三段信号链**：`outputSamplesChanged`（TraceMath 级）→ 构造时经 `updateLastMath` 转发为 `Trace::dataChanged` → `TracePlot::triggerReplot` 限流重绘；`typeChanged` 则让图表检查是否仍支持这条 Trace。
- **TraceModel 三位一体**：QAbstractTableModel（界面列表）、Trace 容器与数据分发台（`addVNAData`/`addSAData` 按参数名分发）、反向控制通道（`requiredExcitation` 反推激励端口，最终触发设备重配）。
- **删除有序**：先 `delete`（析构发 `deleted` 讣告，订阅者自清理），后发 `traceRemoved`（携带悬空指针，仅供比对）——Qt 信号链中的生命周期纪律。
- **轴是「数据 → 屏幕」的翻译层**：X 五类（Distance 是乘 \( c \cdot v / 2 \) 的派生轴）、Y 二十余类（全部是复数的标量换算）；「数据源 × X 类型」经 `isSupported`/`getSupported` 两道闸门约束组合，物理上无意义的搭配根本不会出现在菜单里。

## 7. 下一步学习建议

本讲把「数据放在哪、谁来通知谁」讲清了，下一讲 u8-l2《绘图体系：Smith 图、XY 图与瀑布图》将沿 ③⑤ 两步展开：`TracePlot` 继承体系如何订阅 TraceModel、如何用本讲的 `sampleToCoordinate`/`transform` 把样本画到五种不同坐标系上。之后再按顺序进入 Marker（u8-l3）、导入导出（u8-l4）与数学运算框架（u8-l5——届时回头重看本讲的 `lastMath` 转发机制会有全新体会）。

建议继续阅读的源码（按难度递增）：

1. [traceplot.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceplot.cpp) 的 `enableTrace`/`newTraceAvailable`/`traceDeleted`——本讲生命周期图的消费端。
2. [Math/tracemath.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h) 文件头 40 行注释——官方亲笔的「如何新增数学运算」指南。
3. [trace.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp) 的 `toJSON`/`fromJSON`/`toHash`——Trace 如何进入 u2-l3 讲过的 Setup 持久化体系。
