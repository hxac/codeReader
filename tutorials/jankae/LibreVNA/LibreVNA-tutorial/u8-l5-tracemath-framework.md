# u8-l5 Trace 数学运算框架

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 TraceMath 运算链的有向图结构：Trace 本身是链的第 0 级，任意多个运算节点串联，所有对外读取都委托给链上最后一个启用的节点（lastMath）。
2. 说清数据沿链流动的完整机制：输入信号 `outputSamplesChanged` → 槽 `inputSamplesChanged` → 计算并写入自身 `data` → 再发出自己的 `outputSamplesChanged`，逐级接力直到 `Trace::dataChanged` 触发绘图刷新。
3. 独立实现一个新的数学运算节点（本讲以「乘以常数 Multiply」为例），走完注册的全部触点：Type 枚举、工厂 `createMath()`、信息表 `getInfo()`、`.pro` 工程文件，并理解「可选列表是自动生成的」这一设计。
4. 理解时间域/频域互转约束：`outputType()` 如何决定节点输出域、`DataType::Invalid` 如何沿链传播为红色错误状态、以及 TimeDomainGating 复合运算如何用 TDR→TimeGate→DFT 完成一次「频→时→频」的往返。

## 2. 前置知识

本讲需要以下概念（均在前序讲义建立过，这里做最小回顾）：

- **S 参数与线性复数**：Trace 的每个样本是「X 值（频率等）+ 线性复数 Y 值」。乘以复常数 c 相当于幅度乘 |c|、相位加 arg(c)；若 c 为正实数，则 dB 值整体平移 20·lg(c)。
- **信号与槽（Qt）**：Qt 的观察者机制。一个对象发出信号（signal），连接到它的槽（slot）会被调用。本讲中运算链的「数据变了」通知完全靠 `outputSamplesChanged`/`inputSamplesChanged` 这一对信号与槽接力。
- **lastMath 委托**（u8-l1）：Trace 对外提供的 `getSample()`、`size()` 等读取接口不读自己的原始数据，而是转发给链上最后一个启用的运算节点，因此图表、Marker、导出看到的永远是「链末端」的数据。
- **Savable 与 nlohmann::json**（u2-l3）：所有可持久化对象实现 `toJSON()`/`fromJSON()` 两个函数，JSON 是统一中间表示。运算节点的参数也靠这一机制存进 `.setup` 文件。
- **两种「数学」不要混淆**：GUI 中还有一个「Math 来源的 Trace」（`Trace::Source::Math`，整条曲线由表达式从其他 Trace 计算，见 [trace.cpp:589-640](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L589-L640)）。本讲讲的是另一种：**附着在某条 Trace 上的运算链**（`mathOps`），两者是不同机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Traces/Math/tracemath.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h) | `TraceMath` 抽象基类：数据结构、DataType/Status/Type 枚举、纯虚接口、信号槽。文件头 37 行注释是官方的「新增运算指南」 |
| [Traces/Math/tracemath.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp) | 基类实现：工厂 `createMath()`、信息表 `getInfo()`、输入接驳 `assignInput()`/`removeInput()`、域变化级联 `inputTypeChanged()`、状态管理 |
| [Traces/Math/medianfilter.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.h) / [medianfilter.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp) | 最简单的完整节点范例：中值滤波器。综合实践将参照它实现 Multiply |
| [Traces/trace.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.h) / [trace.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp) | `Trace` 继承 `TraceMath` 并兼任链的第 0 级；`mathOps` 容器、`addMathOperations()`、`enableMathOperation()`、`updateLastMath()` 与链的 JSON 持久化都在这里 |
| [Traces/traceeditdialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.cpp) | 「添加运算」对话框：可选列表由 Type 枚举**自动生成**；MathModel 把链渲染成三列表格（状态/描述/输出域） |
| Traces/Math/tdr.cpp、dft.cpp、timegate.cpp、expression.cpp | 各内置节点的 `outputType()`，用于讲解域约束 |

## 4. 核心概念与源码讲解

### 4.1 TraceMath 基类与运算链

#### 4.1.1 概念说明

一条 Trace 拿到的原始测量数据（频域 S 参数）往往不能直接回答用户的问题：「这个接头的时域反射波形什么样？」「把毛刺滤掉什么样？」「只保留 2 米处的那个反射点又什么样？」。LibreVNA 的答案是把「数据」和「对数据做什么」解耦：

- **Trace** 只负责存储与生命周期，它本身**就是**一个运算节点（链的第 0 级，恒为启用）；
- 每个数学问题封装成一个 **TraceMath 节点**，节点不存输入数据，只持有一个指向上游的 `input` 指针和自己的输出缓冲 `data`；
- 节点串成一条**有向链**（实际上是有向路径）：`Trace → 节点1 → 节点2 → …`，箭头方向即数据流向。链上任意位置可以插入/删除/禁用节点，剩余部分自动重新接驳。

这就是一个经典的**装饰器/流水线混合模式**：每个节点实现同一套接口（自己也是 TraceMath），所以「原始数据」和「加工后的数据」对下游完全同构——第 2 个节点的输入就是第 1 个节点的输出，图表读链末端和读原始 Trace 用的是同一套函数。

#### 4.1.2 核心流程

数据从设备到达屏幕的接力过程：

```text
DeviceDriver 测量信号
  → Trace::addData() 写入原始样本，emit outputSamplesChanged(i, i+1)
      → [节点1 槽] inputSamplesChanged(begin, end)
          读取 input->getData()，计算，写入自己的 data
          emit outputSamplesChanged(begin', end')；调用 success()/warning()/error()
      → [节点2 槽] inputSamplesChanged(...)  ……逐级接力……
      → [链末端 lastMath] outputSamplesChanged
          →（Trace 在 updateLastMath 中预先连接）→ Trace::dataChanged
              → 各图表 triggerReplot（u8-l2 讲过的重绘节流在此生效）
```

三条不变式贯穿全链：

1. **通知粒度是样本区间**：`outputSamplesChanged(begin, end)` 只声明「begin 到 end-1 这些输出样本变了」，下游只需重算受影响的区间。中值滤波等需要「看到邻域」的节点则自行把区间向两侧扩半个核宽（见 4.1.3 第 5 段）。
2. **线程安全靠 dataMutex**：每节点一把互斥锁，读自己 `data` 前加锁（`getSample`/`getData`/`numSamples` 都做了）。
3. **状态自报**：每个节点计算结束必须调用 `success()`/`warning()`/`error()` 之一，编辑对话框里那列红绿灯图标就是它（见 4.2.3）。

#### 4.1.3 源码精读

**① 基类的身份与数据单元**。[tracemath.h:50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L50) 声明 `TraceMath` 同时继承 `QObject`（要信号槽）和 `Savable`（要 JSON 持久化）；[tracemath.h:57-62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L57-L62) 定义全链统一的数据单元：一个 double 的 X（频率/时间/功率）加一个 `std::complex<double>` 的 Y。注意 Y 是**线性复数**，不是 dB——所有运算都在线性域做，dB 只在显示层换算（u7-l4、u8-l2 讲过换算位置）。

**② 必须实现的契约**。[tracemath.h:107-109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L107-L109) 列出三个核心纯虚函数：`outputType()`（声明输出域，见 4.3）、`description()`（编辑对话框里的一行描述）、以及 `getType()`（[tracemath.h:120](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L120)）；[tracemath.h:109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L109) 的 `edit()` 是可选的参数编辑对话框入口。

**③ 信号槽接口**。[tracemath.h:131-135](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L131-L135) 是槽：`inputSamplesChanged(begin, end)` 收到「上游部分样本变化」的通知（基类给了一个空默认实现，纯透传节点可以不覆写）；[tracemath.h:137-141](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L137-L141) 是信号：`outputSamplesChanged` 向下游广播自己的输出变化，`outputTypeChanged` 广播输出域变化。

**④ 输入接驳**。[tracemath.cpp:217-226](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L217-L226) 的 `assignInput()` 是链的「接线柱」：先把 `outputTypeChanged → inputTypeChanged` 这条域监视连接挂上，再立即用上游当前域调一次 `inputTypeChanged()` 完成初始化；[tracemath.cpp:203-215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L203-L215) 的 `removeInput()` 则断开与上游的一切连接、清空输出并把域置 Invalid。

**⑤ 一个真实节点的计算槽**。中值滤波 [medianfilter.cpp:81-159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L81-L159) 是最佳教材，其骨架任何节点都通用：

- [L82-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L82-L90)：先把上游数据整份拷到本地 `inputData`，输出缓冲 `data` 尺寸与上游对齐（头注释提醒过「输入长度可能变，先检查再访问」）；
- [L92-100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L92-L100)：把受影响区间 `[begin, end)` 向两侧各扩 `(kernelSize-1)/2` 并夹取到合法范围——因为一个输出样本依赖邻域输入，邻域变了它就得重算；
- [L114-151](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L114-L151)：滑动窗内排序取中值（首个窗口整体排序，之后每滑动一格只删一个旧样本、插一个新样本，`lower_bound`/`upper_bound` 保持有序，避免每点重排）；
- [L153-158](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L153-L158)：**收尾三件事**——`emit outputSamplesChanged(start, stop)` 通知下游、`success()` 上报状态、无输入数据时改报 `warning("No input data")`。

**⑥ Trace 如何成为第 0 级并委托末端**。[trace.cpp:45-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L45-L47) 在构造函数里把 `this` 作为第一个元素压入 `mathOps`；[trace.cpp:1230-1251](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1230-L1251) 的 `updateLastMath()` 从链尾向前找**第一个启用**的节点定为 `lastMath`，并把它的 `outputSamplesChanged` 中继为 `Trace::dataChanged`。这就是 u8-l1 说过的「对外读取一律委托 lastMath」的落点，例如 [trace.cpp:1443-1446](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1443-L1446)：`Trace::size()` 直接返回 `lastMath->numSamples()`——TDR 之类的节点输出点数与输入不同，这个委托保证了语义正确。

**⑦ 中间插拔的自动接驳**。[trace.cpp:1405-1441](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1405-L1441) 的 `enableMathOperation()` 展示了链的自愈能力：禁用某节点时，把它后面的节点重新接到它前面最后一个启用节点上，再对该节点 `removeInput()`；启用时反向操作。索引 0（Trace 自身）受保护不可动（[L1407](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1407) 的 `index < 1` 守卫）。

**⑧ 谁触发第一棒**。[trace.cpp:94-156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L94-L156) 是 `Trace::addData()`：把一个新样本按 X 升序插入 `data`，最后一行 [trace.cpp:155](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L155) `emit outputSamplesChanged(index, index + 1)` 点燃整条链。而这条信号之所以能到下一个节点，是因为 `inputTypeChanged()` 在 [tracemath.cpp:240](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L240) 用 `Qt::UniqueConnection` 把上游的 `outputSamplesChanged` 连到了自己的 `inputSamplesChanged`（Unique 防止重复连接导致一次变化触发多次计算）。

#### 4.1.4 代码实践：跟踪一次接力（源码阅读型）

1. **实践目标**：不借助调试器，仅凭静态阅读，写出「一个新的 S11 数据点到达 → Smith 图重绘」途经的每一个函数。
2. **操作步骤**：
   - 从 [trace.cpp:155](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L155) 出发；
   - 查 [tracemath.cpp:228-247](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L228-L247) 确认这条信号连接到谁的哪个槽；
   - 若链上有节点，进入该节点的 `inputSamplesChanged`（如 [medianfilter.cpp:81](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L81)），找到它的 `emit outputSamplesChanged`；
   - 最后经 [trace.cpp:1246](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1246) 到达 `Trace::dataChanged`，再结合 u8-l2 讲过的 `enableTrace` 时建立的 `dataChanged → triggerReplot` 连接收尾。
3. **需要观察的现象**：调用链每一跳的「文件:行号」与你推断的信号/槽连接代码一一对应。
4. **预期结果**：得到一条类似
   `addData → outputSamplesChanged → MedianFilter::inputSamplesChanged → outputSamplesChanged →（lastMath 中继）→ Trace::dataChanged → TraceSmithChart::triggerReplot`
   的序列（无运算节点时第一跳直达中继）。链上每多一个节点，序列中就多一对 `inputSamplesChanged/outputSamplesChanged`。
5. 本任务为纯源码阅读，结论可直接与代码比对，无需本地运行验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MedianFilter` 的输出缓冲要自己维护一份 `data`，而不是直接返回「滤波后的上游数据」？
**答案**：TraceMath 的契约是每个节点提供统一的读取接口（`getSample`/`getData`/`numSamples`），且输出长度可与输入不同、输出域可与输入不同。缓存一份输出让下游不必知道上游细节；配合 `dataMutex` 还保证了读取与计算并发安全（实时测量时上游随时可能再来新样本）。

**练习 2**：链上第 1 个节点报了 `error()`，第 2 个节点会收到通知吗？它的数据会怎样？
**答案**：`error()` 只改本节点状态并发出 `statusChanged`，**不会**停止数据流——第 2 个节点照常收到 `outputSamplesChanged` 并计算。真正断流的是域失效路径：若第 1 个节点输出 `DataType::Invalid`，`inputTypeChanged` 会在 [tracemath.cpp:237](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L237) 主动断开数据连接，第 2 个节点才停止接收。

**练习 3**：`Trace::root()`（[tracemath.cpp:324-331](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L324-L331)）为什么能 `static_cast<Trace*>`？
**答案**：链上只有第 0 级的 `input` 为空（其余节点的 `input` 都指向前面某级），`root()` 沿 `input` 指针走到头必然落在 Trace 自身；而 Trace **是一个** TraceMath（公有继承），上行转型安全。

### 4.2 运算节点注册：工厂、Type 枚举与「自动生成的可选列表」

#### 4.2.1 概念说明

新增一种运算，GUI 要在四处「看见」它：能被创建（工厂）、能在添加对话框里列出（信息表）、能在链表格里显示（description/状态/域）、能被存取（toJSON/fromJSON 按名字重建）。LibreVNA 没有引入独立的插件系统，而是用最朴素的**枚举 + switch 工厂**完成注册。妙处在于：添加对话框的可选列表不是手写清单，而是**遍历 Type 枚举自动生成**——所以只要枚举、工厂、信息表三处同步扩展，UI 侧一行代码都不用改。

值得先澄清一个容易误解的点：任务描述里的「newtracemathdialog」在仓库中**不是**一个 C++ 类，只是一个 UI 布局文件 [Traces/Math/newtracemathdialog.ui](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/newtracemathdialog.ui)（一个 QListWidget 加一个 QStackedWidget）；填充列表的逻辑写在其使用者 [traceeditdialog.cpp:250-264](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.cpp#L250-L264) 里。

#### 4.2.2 核心流程

添加一个运算节点的运行时流程：

```text
用户：Trace 属性对话框 → Math 页 → "添加" 按钮
  → traceeditdialog.cpp bAdd 槽
      for i in 0 .. Type::Last-1:
          info = TraceMath::getInfo(i)        // 名称 + 说明页
          列表.addItem(info.name)；说明栈.addWidget(info.explanationWidget)
  → 用户双击某项 → accepted
      ops = TraceMath::createMath(选中类型)   // 可能返回多个节点（复合运算）
      model->addOperations(ops) → Trace::addMathOperations
          逐个 assignInput(前一级) 并压入 mathOps
      ops[0]->edit()                          // 立刻弹参数对话框
```

保存/加载流程（`.setup` 文件）：`toJSON` 把每个节点写成 `{operation: 名称, enabled: bool, settings: 节点自己的 JSON}`；`fromJSON` 按名称在 `getInfo` 表中反查 Type，`createMath` 重建实例，再 `fromJSON` 恢复参数，最后逐个 `assignInput` 重新接链。

#### 4.2.3 源码精读

**① 官方新增指南**。[tracemath.h:10-46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L10-L46) 的注释就是权威步骤清单（放进 `Math` 命名空间、实现哪些虚函数、何时发信号/报状态、扩展 Type 枚举、扩展工厂、加说明页、扩展 getInfo）。

**② Type 枚举**。[tracemath.h:78-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L78-L87)：`MedianFilter / TDR / DFT / Expression / TimeGate / TimeDomainGating / Last`，注释明确要求「新条目加在 Last 之前、不要显式赋值」——因为 UI 靠 `0..Last-1` 遍历，`Last` 是哨兵。`Trace::getType()` 返回 [trace.h:145](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.h#L145) 的 `Type::Last` 作为「Trace 自身」的保留标记，序列化时靠它跳过第 0 级（[trace.cpp:929-932](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L929-L932)）。

**③ 工厂 createMath**。[tracemath.cpp:20-48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L20-L48)：每种类型 new 一个实例。注意 [L39-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L39-L43) 的 **TimeDomainGating 是复合运算**：一次返回 `TDR + TimeGate + DFT` 三个节点，这就是函数返回 `std::vector<TraceMath*>` 而非单个指针的原因——「时间门控」本质是一条 频→时→时（加工）→频 的三节点小链。

**④ 信息表 getInfo**。[tracemath.cpp:50-88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L50-L88)：每种类型给出用户可见名称与说明页控件。各节点的说明页由静态函数创建（如 [medianfilter.cpp:56-65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L56-L65) 从 `medianexplanationwidget.ui` 生成）。

**⑤ 自动生成的可选列表**。[traceeditdialog.cpp:257-264](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.cpp#L257-L264)：

```cpp
for(int i = 0; i < (int) TraceMath::Type::Last;i++) {
    auto info = TraceMath::getInfo(static_cast<TraceMath::Type>(i));
    ui->list->addItem(info.name);
    if(!info.explanationWidget) {
        info.explanationWidget = new QWidget();   // 空说明页兜底
    }
    ui->stack->addWidget(info.explanationWidget);
}
```

选中行号直接当作 Type 用（[traceeditdialog.cpp:270-272](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.cpp#L270-L276)），所以**枚举顺序即列表顺序**。即使不给说明页，兜底代码也会放一个空白 QWidget，列表照样出现。

**⑥ 链表格的状态列**。[traceeditdialog.cpp:447-496](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.cpp#L447-L496) 的 `MathModel::data()` 把每个节点渲染成一行三列（列枚举见 [traceeditdialog.h:19-23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.h#L19-L23)）：状态图标（Ok 对勾 / Warning 感叹 / Error 叉，鼠标悬停显示 `getStatusDescription()`）、description() 文本、输出域名。这就是 4.1 中 `success()/warning()/error()` 的最终去向。

**⑦ 持久化往返**。存：[trace.cpp:927-941](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L927-L941) 用 `getInfo(...).name` 作为运算的**身份标识**写入 JSON；读：[trace.cpp:1050-1074](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1050-L1074) 按名称反查、`createMath` 重建、`fromJSON` 恢复参数、`assignInput(lastMath)` 重新接链。含义：**名称改了旧 setup 就认不出该运算**（只会得到一条 `Unable to create math operation` 警告并跳过，[trace.cpp:1059-1063](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1059-L1063)），自定义运算命名要稳定。

**⑧ 编译注册**。qmake 工程是「活的文件索引」（u1-l3）：新文件必须登记进 `LibreVNA-GUI.pro`——参照中值滤波的三行：HEADERS 段 [LibreVNA-GUI.pro:66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L66)、SOURCES 段 [LibreVNA-GUI.pro:237](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L237)、FORMS 段（若有 .ui）[LibreVNA-GUI.pro:393-394](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L393-L394)。

#### 4.2.4 代码实践：编写你的「新增运算触点清单」（源码阅读型）

1. **实践目标**：把「新增一种数学运算」需要改动的每一处整理成可勾选的清单，为 4.2/综合实践做准备。
2. **操作步骤**：对照 [tracemath.h:10-46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L10-L46) 的官方指南，逐条在源码中找到落点：新建类文件（哪些虚函数必须实现、哪些可选）、Type 枚举（哪一行前插入）、`createMath`（哪个 case）、`getInfo`（名称与说明页）、`.pro`（哪三段）。再确认 **不需要** 改动的文件：`traceeditdialog.cpp`（列表自动生成）、`trace.cpp`（链管理对具体运算类型无感知）。
3. **需要观察的现象**：清单中每一项都能指出具体文件与行号；能说出「列表顺序由什么决定」（枚举顺序）。
4. **预期结果**：约 6-7 项的改动清单 + 2 项「无需改动」的结论。综合实践将逐项执行这份清单。
5. 纯源码阅读，可直接验证。

#### 4.2.5 小练习与答案

**练习 1**：如果不写 `createExplanationWidget`、`getInfo` 里说明页留空，会发生什么？
**答案**：[traceeditdialog.cpp:260-262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.cpp#L260-L262) 的兜底会创建一个空白 QWidget 放进说明栈，列表项照常显示名称，功能完全正常，只是没有说明文字。

**练习 2**：把自定义运算的 `getInfo` 名称从 "Multiply" 改成 "Gain"，对已保存的 `.setup` 文件有何影响？
**答案**：旧文件里 `operation: "Multiply"` 在 `getInfo` 表中反查失败，走 [trace.cpp:1059-1063](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1059-L1063) 的警告分支被跳过，该节点连同其效果一起丢失（Trace 回退为直接使用原始数据）。名称即持久化身份，定了就不要改。

**练习 3**：为什么 `createMath` 返回 `std::vector<TraceMath*>` 而不是单指针？
**答案**：为复合运算预留的能力：TimeDomainGating 一个「逻辑运算」对应 TDR+TimeGate+DFT **三个**物理节点（[tracemath.cpp:39-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L39-L43)）。调用方（`Trace::addMathOperations`）本就按「一段节点序列」接链，单个节点只是 vector 长度为 1 的特例。

### 4.3 时间域与频域互转约束

#### 4.3.1 概念说明

每个节点的输出有一个**域**（DataType）：Frequency、Time、Power、TimeZeroSpan，或 Invalid。链的语义约束由每个节点自己声明：`outputType(inputType)` 是一个纯函数「给定输入域，我输出什么域」。它承担两件事：

1. **可行性判断**：TDR 只能吃频域——输入若是时域，返回 `Invalid` 表示「这步做不了」；
2. **级联传播**：某节点输出域变化时，`Invalid` 或新域会沿着链向下游逐级重算，每个后继节点重新回答「那我还能做吗」。

这样，链在任何插拔/禁用/参数变化后都保持域自洽，用户在编辑对话框「输出域」列看到的就是每个节点当前的 `getDataType()`。而 X 轴类型（频率轴/时间轴/功率轴）能否显示某条 Trace，由 u8-l1 讲过的「数据源×X 类型」闸门判断——根子上的依据正是链末端的这个域。

#### 4.3.2 核心流程

域变化的级联（由 `assignInput` 挂上的 `outputTypeChanged → inputTypeChanged` 监视触发）：

```text
某节点输出域改变 → emit outputTypeChanged(新域)
  → 下游节点 inputTypeChanged(新域)
      myType = this->outputType(新域)     // 各自声明
      dataType = myType；清空输出 data
      若 myType == Invalid:
          error("Invalid input data")     // 状态列变红
          断开与上游的数据信号连接         // 数据流在此截断
      否则:
          （重）连接 outputSamplesChanged → inputSamplesChanged
          inputSamplesChanged(0, 输入长度) // 全量重算
      emit outputTypeChanged(dataType)    // 继续向下游级联
```

数学上，TDR 与 DFT 互为伴：TDR 把等间隔采样的频域 S(f) 经 IFFT 变到时域冲激响应 h(t)（分辨率 Δt = 1/(span)，最大时窗 = 1/Δf，这正是 u8-l6 要精读的内容）；DFT 把时域序列变回频域。所以「频→时→频」的往返在原理上是可逆的，TimeDomainGating 复合运算正是利用这一点：变到时域→截掉不要的时间段→再变回频域，实现「只保留某个距离范围的反射」。

#### 4.3.3 源码精读

**① 域枚举**。[tracemath.h:64-70](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L64-L70)：Frequency / Time / Power / TimeZeroSpan / Invalid，配套的字符串编解码在 [tracemath.cpp:176-201](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L176-L201)（持久化用）。

**② 各节点对 outputType 的回答**——一张「域约束表」：

| 运算 | 源码 | 输入域要求 | 输出域 |
| --- | --- | --- | --- |
| MedianFilter | [medianfilter.cpp:17-21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L17-L21) | 任意 | **同输入**（透传，注释明言 domain stays the same） |
| Expression | [expression.cpp:27-30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/expression.cpp#L27-L30) | 任意 | 同输入（透传） |
| TDR | [tdr.cpp:43-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L43-L50) | Frequency | **Time**，否则 Invalid |
| DFT | [dft.cpp:40-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L40-L47) | Time | **Frequency**，否则 Invalid |
| TimeGate | [timegate.cpp:31-38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L31-L38) | Time | Time（时域内加工），否则 Invalid |
| TimeDomainGating | 组合（见 4.2.3 ③） | Frequency | Frequency（往返） |

**③ 级联与断流**。[tracemath.cpp:228-247](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L228-L247) 是上面流程图的逐行实现，重点两行：Invalid 时 [L236-238](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L236-L238) 先 `error()` 再 `disconnect` 数据信号——错误状态与数据断流同时发生；合法时 [L240-245](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L240-L245) 建立数据连接并**全量**触发一次 `inputSamplesChanged(0, inputSize)`。基类构造函数 [tracemath.cpp:13-18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L13-L18) 把初始状态设为 `error("Invalid input")`——任何节点在接上输入之前天然是红的。

**④ Trace 自身也是一个域源**。[trace.cpp:1258-1262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1258-L1262) 中 Trace 的 `outputType()` 无视输入、直接返回自己的 `domain` 成员；Trace 的域来自测量设置（频域 S 参数为 Frequency，零扫宽时为 Time，见 u7-l1）。它经 [trace.cpp:57-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L57-L60) 的 `typeChanged → outputTypeChanged` 连接进入级联——这正是「设备改用零扫宽后整条链自动重算」的机制。

**⑤ 参数变化后的手工重算习语**。节点参数改变（edit() 里）后输入数据并没变，没有信号会来推它，所以要「装作输入全变了」主动调一次全量计算：TDR [tdr.cpp:208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L208) 与 [tdr.cpp:230](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L230)、DFT [dft.cpp:164](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L164)、Expression [expression.cpp:160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/expression.cpp#L160)、TimeGate [timegate.cpp:290-291](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L290-L291)（注释原话 "pretend that input samples have changed"）。对比之下 MedianFilter 的 `edit()` 只改成员不触发重算（[medianfilter.cpp:39-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/medianfilter.cpp#L39-L50)），改完核宽要等下一次输入变化才生效——写自定义节点时应采纳前者的习语。

**⑥ 阶跃响应缓存**（时域节点专用）。[tracemath.cpp:299-312](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L299-L312) 的 `updateStepResponse()` 对输出做前缀累积和（积分），把冲激响应变成阶跃响应缓存，读取接口 [tracemath.h:99-100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L99-L100) 支持任意下标/插值访问，TDR 的 lowpass+step 模式用它显示传统 TDR 曲线。

#### 4.3.4 代码实践：观察 Invalid 的传播（需本地 GUI，无硬件可用导入数据）

1. **实践目标**：亲眼看到「域不匹配 → 链路变红断流」，加深对 outputType 级联的理解。
2. **操作步骤**：
   - 按 u1-l3 的方法无硬件运行 GUI，导入一个 Touchstone 测量（Documentation/Measurements 下有示例），得到一条频域 Trace；
   - 打开该 Trace 的编辑对话框（双击 Trace 列表项），进入 Math 页，添加一个 **TDR** 运算：观察「输出域」列变为 Time，状态为绿；
   - 再在其**后面**添加一个 **TDR**（第二个 TDR 的输入是时域）：观察它输出 Invalid、状态变红，悬停状态图标应显示 Invalid input data；
   - 把第二个 TDR 换成 **DFT**：应恢复为绿，输出域回到 Frequency。
3. **需要观察的现象**：时域输入喂给 TDR 时红叉；换成 DFT 后变绿且域变回 Frequency。
4. **预期结果**：与 [tdr.cpp:45-49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L45-L49)（Frequency 之外返回 Invalid）与 [dft.cpp:42-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L42-L43)（Time→Frequency）的代码逐行对应。同时可留意：链末端是 Invalid 时，图表上该 Trace 无有效数据点。
5. 本实践需要本地编译运行 GUI；具体菜单措辞以实际界面为准，行为逻辑**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：TimeDomainGating 为什么是「TDR + TimeGate + DFT」三个节点而不是一个？
**答案**：TDR 只做频→时，TimeGate 只在时域截断，DFT 只做时→频——三个已有能力的组合恰好完成「频→时→截断→频」。复用现有节点既省代码，又让用户在链表格里能看到/微调每一级（例如单独编辑 TDR 的窗参数）。工厂一次返回三个节点（[tracemath.cpp:39-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L39-L43)）即可表达这种组合。

**练习 2**：一个输入为 Time 的链，末端的 Trace 能显示在频率轴的图上吗？依据是哪段代码？
**答案**：不能。图表支持的轴类型受「数据源×X 类型」闸门约束（u8-l1、u8-l2），而该闸门的上游依据是 `Trace::outputType()` 返回的 `lastMath->getDataType()`（[trace.h:69](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.h#L69)）——链末端是 Time，频率轴的图就不接受这条 Trace。想显示就得在链尾补一个 DFT 把域变回来。

**练习 3**：MedianFilter 与 Expression 的 `outputType` 都是「透传输入域」。这个选择意味着什么、又有什么代价？
**答案**：意味着它们是「域无关」的逐点（或邻域）运算，插在链上任何位置都不改变下游的域约束，组合自由度最高。代价是它们无法完成需要变换坐标轴含义的工作（如 TDR），且透传时若输入 Invalid 输出也 Invalid——错误依旧沿链传播，这其实是合理行为。

## 5. 综合实践：实现一个「Multiply」运算节点

本任务把三节内容串成闭环：参照 MedianFilter 实现把整条 Trace 乘以实常数的运算，注册进可选列表，编译验证，并检查持久化。全程不需要硬件（用导入的 Touchstone 数据验证即可）。

**数学预期**：线性复数乘以正实数 k，dB 值整体平移 20·lg(k)；k=2 时约为 +6.02 dB，相位不变。这给了一个非常干净的验证手段。

### 步骤 1：新建 multiply.h（示例代码）

放到 `Software/PC_Application/LibreVNA-GUI/Traces/Math/multiply.h`：

```cpp
#ifndef MULTIPLY_H
#define MULTIPLY_H

#include "tracemath.h"

namespace Math {

class Multiply : public TraceMath
{
public:
    Multiply();

    virtual DataType outputType(DataType inputType) override;
    virtual QString description() override;
    virtual void edit() override;

    virtual nlohmann::json toJSON() override;
    virtual void fromJSON(nlohmann::json j) override;
    Type getType() override {return Type::Multiply;};

public slots:
    virtual void inputSamplesChanged(unsigned int begin, unsigned int end) override;

private:
    double factor;
};

}

#endif
```

对照 4.2.4 的清单：命名空间 `Math`（官方指南第 1 条）、必实现的三组虚函数、Savable 两函数、返回自己的 Type。

### 步骤 2：新建 multiply.cpp（示例代码）

放到 `Software/PC_Application/LibreVNA-GUI/Traces/Math/multiply.cpp`：

```cpp
#include "multiply.h"

#include <QInputDialog>
#include <QLabel>

using namespace Math;

Multiply::Multiply()
{
    factor = 1.0;
}

TraceMath::DataType Multiply::outputType(TraceMath::DataType inputType)
{
    // 乘以实常数不改变 X 轴含义，域保持不变（透传，同 MedianFilter）
    return inputType;
}

QString Multiply::description()
{
    return "Multiply by " + QString::number(factor);
}

void Multiply::edit()
{
    bool ok;
    double newval = QInputDialog::getDouble(nullptr, "Multiply",
                    "Factor:", factor, -1000000.0, 1000000.0, 6, &ok);
    if(ok && newval != factor) {
        factor = newval;
        // 参数变了但输入没变：按官方习语"装作输入全变了"，主动全量重算
        if(input) {
            inputSamplesChanged(0, input->numSamples());
        }
    }
}

nlohmann::json Multiply::toJSON()
{
    nlohmann::json j;
    j["factor"] = factor;
    return j;
}

void Multiply::fromJSON(nlohmann::json j)
{
    factor = j.value("factor", 1.0);
}

void Multiply::inputSamplesChanged(unsigned int begin, unsigned int end)
{
    std::vector<Data> inputData;
    if(input) {
        inputData = input->getData();
    }
    if(data.size() != inputData.size()) {
        dataMutex.lock();
        data.resize(inputData.size());
        dataMutex.unlock();
    }
    if(data.size() > 0) {
        if(end > inputData.size()) {
            end = inputData.size();
        }
        dataMutex.lock();
        for(unsigned int i=begin;i<end;i++) {
            data.at(i).x = inputData.at(i).x;
            data.at(i).y = inputData.at(i).y * factor;
        }
        dataMutex.unlock();
        emit outputSamplesChanged(begin, end);
        success();
    } else {
        warning("No input data");
    }
}
```

要点（都来自 4.1/4.3 的分析）：

- `inputSamplesChanged` 的骨架照抄 MedianFilter（拷输入、对齐尺寸、区间夹取、收尾三件事），只是核心运算换成逐点乘法——Multiply 是逐点运算，无需像中值滤波那样扩区间；
- `edit()` 用 `QInputDialog::getDouble`，**省掉一个 .ui 文件**（代价是没有说明页控件，见步骤 4 的兜底）；
- `edit()` 里参数变化后主动调 `inputSamplesChanged(0, ...)`，采纳 TDR/DFT/TimeGate 的重算习语（4.3.3 ⑤），避开 MedianFilter「改参数不立即生效」的坑。

### 步骤 3：注册（四处改动，全部在现有文件中）

1. **Type 枚举**：[tracemath.h:78-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.h#L78-L87) 中 `Last` 之前加一行 `Multiply,`；
2. **工厂**：[tracemath.cpp:20-48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L20-L48) 加 `case Type::Multiply: ret.push_back(new Math::Multiply()); break;`，并在文件头补 `#include "multiply.h"`；
3. **信息表**：[tracemath.cpp:50-88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L50-L88) 加 `case Type::Multiply: ret.name = "Multiply"; ret.explanationWidget = nullptr; break;`（说明页留空，添加对话框会兜底为空白页，见 4.2.3 ⑤）；
4. **工程文件**：`LibreVNA-GUI.pro` 的 HEADERS 段（参照 [L66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L66)）加 `Traces/Math/multiply.h \`，SOURCES 段（参照 [L237](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L237)）加 `Traces/Math/multiply.cpp \`。

**不需要改** traceeditdialog.cpp（列表自动生成）与 trace.cpp（链管理类型无关）——这是本框架设计的核心红利。

### 步骤 4：编译

按 u1-l3 的流程：在构建目录重新跑 `qmake6`（让 .pro 的新文件生效）再 `make`。若在 Qt Creator 中开发，右键项目执行 Run qmake 后构建。

### 步骤 5：功能验证（无硬件，导入数据）

1. **出现**：启动 GUI，导入 Touchstone 示例测量，打开 Trace 编辑对话框的 Math 页，点添加——列表里应出现 "Multiply"（位于枚举插入位置对应的行）；
2. **接线**：双击选中，弹出 Factor 输入框，填 2.0；链表格新增一行，状态绿、输出域与输入相同（透传）；
3. **数值**：为这条 Trace 建一个 dB 刻度的 XY 图（u8-l2），对比开关该运算（禁用/启用节点）：启用后整条曲线应整体抬高约 6.02 dB，形状与相位不变；更精确的办法是在同一位置放 Marker，用 delta 读数（u8-l3）确认差值；
4. **级联位置**：把 Multiply 拖到一个 TDR 前面/后面（若链上有 TDR），观察它在前（频域乘 2，TDR 后幅度同样 ×2）与在后（只作用于 TDR 输出）结果的异同——体会「链顺序即语义」；
5. **持久化**：SaveSetup 保存工作区，用文本编辑器打开 `.setup`，应能在该 Trace 的 `math` 数组里找到 `{"operation": "Multiply", "enabled": true, "settings": {"factor": 2.0}}`；重新 LoadSetup，节点与参数完整恢复（对应 4.2.3 ⑦ 的往返路径）。

**预期结果**：以上 5 项全部成立。其中第 3、4 项的数值结论（+6.02 dB、顺序语义）可由数学直接推出，界面层面的表现**待本地验证**。

## 6. 本讲小结

- **链式有向图**：Trace 继承 TraceMath 并充当链的第 0 级，任意多个运算节点经 `assignInput()` 串成一条路径；所有对外读取委托给最后一个启用节点 lastMath，`updateLastMath()` 在插拔/禁用后自动重接线并把末端信号中继为 `Trace::dataChanged`。
- **信号接力**：数据流靠 `outputSamplesChanged(begin,end) → inputSamplesChanged(begin,end)` 一对信号槽逐级传递，通知粒度是样本区间；节点计算完必须「发信号 + 报状态（success/warning/error）」。
- **注册即三处**：Type 枚举（Last 前）、工厂 `createMath()`、信息表 `getInfo()`，外加 .pro 登记；「添加运算」对话框的列表由枚举遍历**自动生成**，UI 代码零改动。
- **名称即持久化身份**：.setup 里运算以 `getInfo().name` 存取，改名会让旧配置认不出节点而静默跳过。
- **域约束与级联**：每个节点用纯函数 `outputType(inputType)` 声明输出域；域变化沿 `outputTypeChanged → inputTypeChanged` 级联，Invalid 使节点变红并断开数据连接；TDR（频→时）、DFT（时→频）、TimeGate（时→时）组合出 TimeDomainGating 的频→时→频往返。
- **参数变化的重算习语**：edit() 改参后应主动 `inputSamplesChanged(0, numSamples())`「装作输入全变了」（TDR/DFT/TimeGate 均如此；MedianFilter 是反例）。

## 7. 下一步学习建议

下一讲 **u8-l6 时频变换实战：TDR、DFT 与时间门** 将钻进本讲框架里最有价值的三个节点内部：tdr.cpp 的补零与窗处理、dft.cpp 的频率轴构造、timegate.cpp 的门函数卷积，以及底层 `fftcomplex.cpp` 的 FFT 实现——本讲的「域往返」在那里变成具体的 IFFT/FFT 数学。建议先完成本讲综合实践再进入 u8-l6：拥有一个自己写的节点后，再去读 TDR 会明显顺畅。若对「逐点运算」意犹未尽，可以对比阅读 `Traces/Math/expression.cpp`（基于 muparser 的表达式节点，`parser/` 目录是整个第三方解析器库），看它如何把用户公式套用到每个样本点上。
