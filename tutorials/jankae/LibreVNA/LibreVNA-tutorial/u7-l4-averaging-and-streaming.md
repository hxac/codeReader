# 平均、数据分级与流式输出

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `Averaging` 类的通用平均器实现，理解 Mean（均值）与 Median（中值）两种策略的数学含义与适用场景。
2. 说出 VNA 模式与 SA 模式各自在哪里接入平均器，并列出**所有**会触发一次 `average.reset()` 的场景。
3. 解释 `AppWindow::addStreamingData` 的数据分级设计：同一个测量点为什么会以 Raw / Calibrated / Deembedded（VNA）和 Raw / Normalized（SA）多个版本对外分发。
4. 用 Python socket 连接 `StreamingServer`，接收逐点的 JSON 流并转换成可绘制的幅度数据。

本讲是单元 7 的收尾：前两讲（u7-l1、u7-l2）讲到测量点从驱动进入模式层后"依次做平均、校准、（去嵌入/归一化）再写入 TraceModel"，本讲就把这条管线上的两个"旁路出口"彻底拆开——**平均器**是管线中的第一站处理，**流式输出**是管线沿途的三个（VNA）/两个（SA）取样龙头。

## 2. 前置知识

- **为什么要平均**：每一次扫描的每个测量点都叠加了随机噪声。被测的 S 参数（或功率）本身在扫间是稳定的，而噪声的相位与幅度是随机的。把连续 N 次扫描的**线性复数值**相加取平均，信号部分相干叠加（增长 N 倍），噪声部分非相干叠加（只增长 \(\sqrt{N}\) 倍），于是信噪比提升 \(\sqrt{N}\)，折合 \(10\lg N\) dB。N=100 时约 20 dB。
- **为什么必须在线性域平均**：若先把每点换算成 dB 再平均，等于对对数取均值——一个被噪声打到 −60 dB 的点会把平均值严重拉低。所以代码里 VNA 平均的是 `std::complex<double>`（线性复数），SA 平均的是线性电压（1.0 对应 0 dBm，dBm 值 = \(20\lg v\)）。这是 u3-l1 与 u7-l2 已建立的约定，本讲的 `Averaging` 正是建立在该约定之上。
- **均值 vs 中值**：均值对高斯噪声最优，但一个突发干扰（如 EMI 尖峰）会被原样摊进结果；中值对离群点完全免疫（只要尖峰不超过窗口的一半），代价是对高斯噪声的抑制略差。GUI 通过偏好项 `Acquisition.useMedianAveraging` 二选一。
- **滑动窗口（moving window）**：平均器不是"攒满 N 次就停"，而是每个点维护一个最多容纳 N 次历史的队列，新扫描进来、最老的出去，因此**稳态下每次扫描都输出一个由最近 N 次组成的平均**。
- **`std::map` 的有序性**：`VNAMeasurement::measurements` / `SAMeasurement::measurements` 是以字符串为键的 `std::map`，遍历顺序恒为键的字典序。平均器正是依赖"取出顺序 = 写回顺序"来完成值与键的对应。
- **推送（push）与轮询（poll）**：SCPI 远程控制（u10 详述）是"你问一句、我答一句"的轮询；流式输出则是服务器主动把每个点推给所有已连接的客户端，适合连续采集。本讲的 `StreamingServer` 属于后者。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/PC_Application/LibreVNA-GUI/averaging.h` | `Averaging` 类声明：Mode 枚举、public 接口、内部窗口容器 |
| `Software/PC_Application/LibreVNA-GUI/averaging.cpp` | 平均器实现：VNA/SA 两个 `process` 重载、Mean/Median 计算、级别统计 |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp` | VNA 模式：平均器接入点、三个流式取样点、全部 reset 场景 |
| `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp` | SA 模式：平均器接入点、两个流式取样点、reset 场景 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.h` / `appwindow.cpp` | `VNADataType`/`SADataType` 枚举、五个 `StreamingServer*` 指针、`addStreamingData` 分发 |
| `Software/PC_Application/LibreVNA-GUI/streamingserver.h` / `streamingserver.cpp` | TCP 流式服务器：多客户端管理、JSON 行序列化 |
| `Software/PC_Application/LibreVNA-GUI/preferences.h` | 五个流式通道的默认开关与端口（19000/19001/19002/19100/19101） |
| `Documentation/UserManual/SCPI_Examples/capture_live_data.py` | 官方流式采集示例：`add_live_callback(19000, ...)` |
| `Documentation/UserManual/SCPI_Examples/libreVNA.py` | 官方 Python 控制库：流式连接线程与 JSON 复数重组逻辑 |

## 4. 核心概念与源码讲解

### 4.1 averaging 模板：一个类吃下 VNA 与 SA 两种测量

#### 4.1.1 概念说明

`Averaging` 是一个**不依赖 Qt、不依赖硬件**的纯数据类，位于 GUI 顶层目录（不属于任何模式）。它解决的问题是：VNA 模式的测量值是"每点一组复数 S 参数"，SA 模式的测量值是"每点一组线性电压"，两者都需要"逐点、跨扫描、滑动窗口"式的平均，且都要回答三个状态问题——

- 已经平均了多少圈？（`getLevel`，驱动状态栏上 `3/10` 的分子）
- 当前正在跑第几圈？（`currentSweep`，校准/归一化测量要等它到 N 才取数）
- 是否已攒满？（`settled`，单次扫描模式的停止条件、`setOperationPending` 的解除条件）

它的设计要点是**类型归一化**：两个 public `process` 重载各自把 `std::map` 里的值抽成 `std::vector<std::complex<double>>`，复用同一个私有 `process(pointNum, data)` 完成窗口维护与统计，最后再按各自类型写回（VNA 写回复数，SA 只取 `.real()`）。

#### 4.1.2 核心流程

内部数据结构是一层"点号 → 历史"的嵌套容器：

```
avg : vector< deque< vector<complex<double>> > >
       ↑点号0..N-1   ↑最近averages圈  ↑每圈该点的全部测量值
```

处理一个新到测量点的流程：

1. 若测量的**个数**变了（例如激励端口变化导致 S 参数集合变化）→ 自动 `reset`，窗口清零。
2. 把 `measurements` 按 map 顺序抽成 `vector<complex>`。
3. 按 `pointNum` 找到对应 deque：`push_back` 新样本；若长度超过 `averages` 则 `pop_front` 最老样本（滑动窗口）。
4. 按模式统计：
   - **Mean**：\[\bar{S}_i=\frac{1}{W}\sum_{k=1}^{W} S_i[k]\]，W 为当前窗口长度（未攒满时 W < N，即"部分平均"）。
   - **Median**：对每个测量分量，按**模值** \(|S|\) 插入排序；W 为奇数取第 \((W{+}1)/2\) 个，偶数取中间两个的平均 \(\frac{v_{W/2-1}+v_{W/2}}{2}\)。
5. 把统计结果按原顺序写回 `measurements`。

两个级别的计数有一个精巧的不对称，理解它就能读懂校准流程的"取数时机"：

- `currentSweep()` 返回 `avg.front().size()`——**第 0 点**的队列长度。新一圈开始时第 0 点最先收到数据，所以它最先 +1，表示"正在跑第几圈"。
- `getLevel()` 返回 `avg.back().size()`——**最后一点**的队列长度。只有当这一圈跑到最后一个点，它才 +1，表示"完整攒下的圈数"。

#### 4.1.3 源码精读

类声明与核心成员——`Mode` 枚举只有两个值，`avg` 就是上面那张嵌套容器图：

[Software/PC_Application/LibreVNA-GUI/averaging.h:L10-L41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.h#L10-L41)
`reset(points)` 按点数重建容器；`getLevel`/`currentSweep`/`settled` 提供窗口状态查询。

`reset` 与 `setAverages`——后者在调小平均值时从 `pop_front()` 丢弃**最老**的样本，保留最新历史：

[Software/PC_Application/LibreVNA-GUI/averaging.cpp:L12-L29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L12-L29)

VNA 版 `process`：测量个数变化即自动重置，抽取→统计→写回复数：

[Software/PC_Application/LibreVNA-GUI/averaging.cpp:L31-L48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L31-L48)

SA 版 `process`：结构完全相同，仅写回时取 `.real()`（SA 测量本就是实数电压，复数化只是为了复用私有实现）：

[Software/PC_Application/LibreVNA-GUI/averaging.cpp:L50-L67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L50-L67)

级别统计的"首尾不对称"：

[Software/PC_Application/LibreVNA-GUI/averaging.cpp:L69-L90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L69-L90)
`getLevel` 看 `back()`（最后一点），`currentSweep` 看 `front()`（第 0 点），`settled` 即"完整圈数 == 目标圈数"。

私有 `process` 的窗口维护：点号等于容器长度时自动扩容（支持点数比初始 `reset` 更多的扫描），随后压入新样本并把窗口裁剪到 `averages`：

[Software/PC_Application/LibreVNA-GUI/averaging.cpp:L102-L124](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L102-L124)

Mean 分支：对窗口内每个测量分量求和后除以**当前窗口长度**（注意不是除以 N，未攒满时输出的是部分平均）：

[Software/PC_Application/LibreVNA-GUI/averaging.cpp:L126-L141](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L126-L141)

Median 分支：比较器按模值排序，`upper_bound` 插入保持有序；奇偶窗口分别取中位或中间两个的均值：

[Software/PC_Application/LibreVNA-GUI/averaging.cpp:L142-L169](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L142-L169)

**模式层的接入点。** VNA 模式在 `NewDatapoint` 中，先做单圈停止判断（`getLevel() == averages` 时 `Stop()`），再送入平均器：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L982-L1007](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L982-L1007)

SA 模式的 `NewDatapoint` 同样有三重守卫（活动检查、设置变更中丢点、单圈停止）后进入 `average.process`：

[Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp:L513-L532](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L513-L532)

`settled()` 的一个直接用途：平均未攒满时状态栏显示"操作进行中"，攒满即解除：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L1038-L1040](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1038-L1040)

`currentSweep()` 的关键用途：校准测量只在**最后一圈**取数，保证进入误差项的测量已经是满窗平均：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L1042-L1056](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1042-L1056)（SA 的归一化测量在 [spectrumanalyzer.cpp:L546-L569](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L546-L569) 用完全相同的写法）

工具栏上的 Averaging 控件与 **Reset 按钮**（VNA 侧）——这是第一种人为触发的重置：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L480-L494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L480-L494)（SA 侧对应 [spectrumanalyzer.cpp:L209-L224](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L209-L224)）

`SetAveraging` 只调 `setAverages`（裁剪窗口），**不**清空历史：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L1352-L1361](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1352-L1361)

VNA 的 `ResetLiveTraces` 是统一的重置入口（清平均 + 清 live 迹线）：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L2074-L2081](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L2074-L2081)

#### 4.1.4 代码实践：找出所有 "reset averaging" 场景（无硬件，纯源码走读）

1. **实践目标**：把 VNA 与 SA 两个模式中所有会导致平均窗口清零的代码路径找全，整理成一张表。这能检验你对"设置变更 → 设备重配 → 数据重置"整条链路的理解。
2. **操作步骤**：
   - 在 `VNA/vna.cpp` 中搜索 `average.` 与 `ResetLiveTraces`，逐个打开每一处调用上下文，回答"谁在什么时机调用它"。
   - 在 `SpectrumAnalyzer/spectrumanalyzer.cpp` 中做同样的事。
   - 额外检查 `averaging.cpp` 内部的**自动**重置（不经过任何模式代码）。
3. **需要观察的现象**：每个调用点周围是否有 `resetTraces` 标志、`changingSettings` 标志或设备回调包裹。
4. **预期结果**（参考答案，可对照你自己的发现）：

   | # | 模式 | 触发场景 | 代码位置 |
   |---|---|---|---|
   | 1 | VNA | 工具栏 Reset 按钮 | `vna.cpp:490-493` |
   | 2 | VNA | `SettingsChanged(resetTraces=true)` 立即清一次（用户改设置的第一时间让画面归零） | `vna.cpp:1110-1112` |
   | 3 | VNA | 防抖定时器到期后 `ConfigureDevice(resetTraces)` 发送新配置前再清一次 | `vna.cpp:1978-1980` |
   | 4 | VNA | 设备 `setVNA` 回调中（注释 "device received command, reset traces now"）——确认设备已切换后再清，丢弃在途旧点 | `vna.cpp:2043-2046` |
   | 5 | VNA/SA | 平均器内部：测量个数变化（如激励端口数变化） | `averaging.cpp:33-36`、`52-55` |
   | 6 | SA | 工具栏 Reset 按钮 | `spectrumanalyzer.cpp:219-223` |
   | 7 | SA | `SettingsChanged()` 每次都调 `ResetLiveTraces`（SA 无 `resetTraces` 参数，改任何设置都清） | `spectrumanalyzer.cpp:607`、`943-948` |
   | 8 | SA | `ConfigureDevice` 启动/重启扫描（`setSA` 分支）末尾无条件 `average.reset(SApoints())` | `spectrumanalyzer.cpp:924` |

   一个值得咀嚼的差异：VNA 在设置变更期间**不丢弃**在途数据点（依赖场景 4 的回调后重置兜底），SA 则在 `NewDatapoint` 开头用 `changingSettings` 守卫直接丢弃旧设置的点（`spectrumanalyzer.cpp:519-522`），两种策略殊途同归。
5. 本实践为纯源码走读，结论可直接从代码验证，无需"待本地验证"标注。

#### 4.1.5 小练习与答案

**练习 1**：把 Averaging 从 10 改成 3，窗口里原来攒了 10 圈，`setAverages` 之后剩哪 3 圈？为什么从 `pop_front()` 而不是 `pop_back()` 丢弃？

**答案**：剩下最近的 3 圈（第 8、9、10 圈）。`pop_front()` 弹出的是**最老**的样本，保留最新历史，这样平均结果立即反映最近的测量；若丢最新保最旧，输出会"卡在过去"。见 [averaging.cpp:L20-L29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L20-L29)。

**练习 2**：`averages = 1` 时这个类退化成什么？`settled()` 何时为真？

**答案**：窗口长度为 1，每个新样本立刻覆盖旧样本，Mean/Median 都等于原样透传（除以 1 / 取唯一元素）。第 0 点一到，`getLevel()`（`back().size()`）在扫到末点后即为 1 == averages，故第一圈扫完就 `settled()`。

**练习 3**：为什么 Median 分支的比较器用 `abs(a) < abs(b)`（按模值排序）而不是对复数做全序比较？

**答案**：`std::complex` 没有定义 `<`（复数域无全序），必须自给比较器；按模值排序后"取中位"的物理含义是"取幅度居中的那次测量"，离群的大幅度尖峰会被排到队尾而进不了中位，这正是中值滤波抗尖峰的机制。见 [averaging.cpp:L147-L156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L147-L156)。

### 4.2 addStreamingData 数据分级：一根管线上的多个取样龙头

#### 4.2.1 概念说明

回顾 u7-l1 的数据上行管线：原始点 →（可选偏移）→ **平均** → X 轴定型 → **校准修正** → 写入 TraceModel →（可选）**去嵌入**再写一次。同一个测量点在管线上不同位置的"身份"不同：

| 分级 | 经过的处理 | 语义 |
|---|---|---|
| `VNADataType::Raw` | 已平均，**未校准** | 设备视角的原始 S 参数（注意：Raw 指"未修正"，不是"未平均"） |
| `VNADataType::Calibrated` | 已平均 + SOLT 校准 | 修正了仪器自身系统误差的 S 参数 |
| `VNADataType::Deembedded` | 以上全部 + 去嵌入 | 再扣除了夹具/端口延伸效应的 DUT 真实参数 |
| `SADataType::Raw` | 已平均，未归一化 | 端口上的绝对功率（线性电压） |
| `SADataType::Normalized` | 已归一化 | 除以参考测量并缩放到目标电平后的相对显示值 |

不同用户要的不同：有人想拿原始数据自己写后处理，有人只想要最终可记录的结果。GUI 的解法不是提供"选项"，而是**每一级都开一个取样龙头**——五个独立的 TCP 端口，同时对外推送，客户端各取所需。这就是 `AppWindow::addStreamingData` 存在的意义：它是一个纯粹的**分发器**，把某一级的数据路由到对应的 `StreamingServer`。

#### 4.2.2 核心流程

```
VNA::NewDatapoint                          SA::NewDatapoint
  m_avg = average.process(m)                 m_avg = average.process(m)
  addStreamingData(m_avg, Raw)        →      addStreamingData(m_avg, Raw)
  cal.correctMeasurement(m_avg)               if(normalize.active){
  if(校准激活)                                 　 除以参考 × 电平系数
    addStreamingData(m_avg, Calibrated)  →      addStreamingData(m_avg, Normalized)
  traceModel.addVNAData(...)                 }
  if(去嵌入激活){                            traceModel.addSAData(...)
    deembedding.Deembed(m_avg)
    addStreamingData(m_avg, Deembedded)
  }
```

分发与生命周期：

1. AppWindow 构造时按 Preferences 为每个**已启用**的通道 `new StreamingServer(port)`。
2. 偏好设置变更时，lambda `updateStreamingServer` 统一处理三种迁移：开→关（删除）、关→开（创建）、改端口（重建）。
3. 模式层每处理一个点，调用 `addStreamingData(m, type, zerospan)`；函数体内 switch 把 `type` 映射到对应指针，指针非空才推送。

关键点：**通道未启用时指针为 nullptr，`if(server)` 直接跳过**，因此开关流式输出对测量管线零开销（只多一次空指针判断）。

#### 4.2.3 源码精读

数据类型枚举与两个分发函数声明——注意它们是 `AppWindow` 的 public 方法，模式层都持有一个 `window` 指针来调用：

[Software/PC_Application/LibreVNA-GUI/appwindow.h:L56-L69](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.h#L56-L69)
五个服务器指针在同文件 [appwindow.h:L169-L173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.h#L169-L173)。

构造函数里按偏好创建（此处位于 `setupUi` 之前——u2-l1 讲过的装配顺序：数据服务先于界面）：

[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L124-L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L124-L138)

偏好变更时的三态迁移 lambda 与五次调用：

[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L820-L836](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L820-L836)

VNA 分发函数——纯粹的枚举→指针映射加空指针守卫：

[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L874-L899](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L874-L899)

VNA 管线上的三个取样点，注意各自的条件（Raw 无条件；Calibrated 要求校准类型非 None；Deembedded 要求去嵌入激活）：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L1036-L1069](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1036-L1069)

SA 管线上的两个取样点（Raw 无条件；Normalized 在 `normalize.active` 分支内，且已先完成除以参考、乘以电平系数两个变换）：

[Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp:L544-L578](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L544-L578)

五个通道的默认端口（默认全部**关闭**；SCPI 服务器默认端口 19542 在紧邻行，可对照）：

[Software/PC_Application/LibreVNA-GUI/preferences.h:L387-L398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L387-L398)

#### 4.2.4 代码实践：核对"哪一级会被推送"（无硬件可做前半，有 GUI 可做后半）

1. **实践目标**：验证数据分级是**条件性**的——关闭校准时 Calibrated 通道静默；并熟悉端口配置入口。
2. **操作步骤**：
   - 源码走读（无硬件）：假设 VNA 模式未做任何校准、未启用去嵌入，回答"此刻 19001/19002 端口上有没有数据？"，并在 `vna.cpp:1036-1069` 找到判定依据。
   - GUI 操作（无硬件也可）：启动 GUI（无设备亦然），打开 Edit → Preferences，找到 Streaming Servers 组，勾选 `VNA raw data`（端口 19000），确认保存。
3. **需要观察的现象**：
   - 走读答案：`cal.getCaltype().type != Calibration::Type::None` 为假 → 19001 静默；`deembedding_active` 为假 → 19002 静默；只有 19000 始终有流。
   - 终端验证：`nc localhost 19000`，VNA 模式下无设备连接时应**无输出**（没有测量就没有推送），有设备则每点一行 JSON。
4. **预期结果**：偏好对话框中五个通道可独立开关、独立设端口；`nc` 能连上但无设备时保持安静。端口连通行为待本地验证（取决于本机是否运行 GUI 与有无设备）。

#### 4.2.5 小练习与答案

**练习 1**：为什么把"创建流式服务器"放在 `ui->setupUi(this)` 之前？换成放在后面会有什么问题？

**答案**：这是 u2-l1 讲过的装配顺序原则——无界面依赖的服务（SCPI TCP、流式输出）必须先于界面就绪，这样即使以 `--no-gui` 无头方式运行，远程客户端也能从启动第一刻起连接取数；若放在界面之后，无头模式下服务会晚启动甚至依赖界面逻辑。见 [appwindow.cpp:L124-L140](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L124-L140)。

**练习 2**：`addStreamingData` 为什么做成 `AppWindow` 的成员，而不是让 VNA/SA 模式直接持有五个 `StreamingServer*`？

**答案**：集中生命周期管理。五个服务器的创建/销毁/改端口由 AppWindow 统一负责（构造时创建、偏好变更时迁移、析构时回收），模式层只管在正确的管线位置"打卡"，通过 `window` 指针一行调用即可。若每个模式自持，会出现两份 VNA 管线（模式只有一个活动实例时虽不冲突，但 SA 与 VNA 各持一套指针会让偏好迁移逻辑重复五处）。

### 4.3 StreamingServer 输出：逐点 JSON 行协议

#### 4.3.1 概念说明

`StreamingServer` 是一个极简的 TCP 推送服务器：每个实例监听一个端口、代表一个数据分级通道；允许多个客户端同时连接（`std::set<QTcpSocket*>`）；每来一个测量点，就把该点序列化成**一行 JSON**（以 `\n` 结尾）广播给所有在线客户端。

它与 SCPI（u10 展开）构成 GUI 的两条远程数据出口，定位截然不同：

| | SCPI（TCP 19542） | StreamingServer（19000/19001/19002/19100/19101） |
|---|---|---|
| 模式 | 请求-应答（轮询） | 单向推送 |
| 内容 | 控制 + 查询（可取完整迹线） | 仅逐点测量数据 |
| 单位/格式 | 文本，多为工程单位 | JSON，**线性值** |
| 时机 | 客户端主动 | 每点产生即推 |

两个必须理解的格式细节：

1. **JSON 没有复数类型**。VNA 的 S 参数是复数，所以每个测量被拆成 `名字_real` 与 `名字_imag` 两个浮点键；SA 的测量本就是实数电压，直接一个键一个数。
2. **零扫宽（zerospan）切换字段**。零扫宽下点与点之间区分的是时间而非频率，因此 JSON 里用 `time`（秒，`us * 0.000001`）替代 `frequency`（VNA 同时还少了 `dBm` 激励电平字段）。

#### 4.3.2 核心流程

服务器的全部行为：

```
构造(port):
  server.listen(Any, port)
  新连接 → sockets.insert(socket)
  socket 断开(UnconnectedState) → sockets.erase + deleteLater

addData(测量点, is_zerospan):        ← 模式层经 addStreamingData 间接触发
  组装 JSON:
    VNA: {pointNum, time | frequency+dBm, Z0,
          measurements:{"S11_real":.., "S11_imag":.., ...}}
    SA:  {pointNum, time | frequency,
          measurements:{"port1":线性电压, ...}}
  dump() + '\n'
  for 每个 open 的 socket: write(一行)
```

换算提醒（对接收端）：VNA 的线性复数转 dB 幅度用 \(20\lg|S|\)；SA 的线性电压转 dBm 用 \(20\lg v\)。

#### 4.3.3 源码精读

类声明：一个 `QTcpServer` 加一个 socket 集合，没有别的状态——足够说明这是一个"哑"广播器，不做任何协议解析：

[Software/PC_Application/LibreVNA-GUI/streamingserver.h:L10-L25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.h#L10-L25)

构造函数：监听、接纳新连接、断连时清理。客户端数量没有上限，也不做握手或鉴权：

[Software/PC_Application/LibreVNA-GUI/streamingserver.cpp:L5-L21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L5-L21)

VNA 版 `addData`：注意 `_real`/`_imag` 键的拼法（`QString(p.first+"_real")`）与零扫宽下的字段替换：

[Software/PC_Application/LibreVNA-GUI/streamingserver.cpp:L23-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L23-L46)

SA 版 `addData`：测量值是 double，直接写入；无 `Z0`、无 `dBm` 字段：

[Software/PC_Application/LibreVNA-GUI/streamingserver.cpp:L48-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L48-L68)

官方示例如何消费这个协议——`capture_live_data.py` 在启动扫描后注册 19000 端口的回调，收到第 500 点（501 点扫描的末点）即退出：

[Documentation/UserManual/SCPI_Examples/capture_live_data.py:L38-L53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/UserManual/SCPI_Examples/capture_live_data.py#L38-L53)

`libreVNA.py` 的实现细节：每端口一条线程 + 按行读取，收到后 `json.loads`，再把 `_real`/`_imag` 重组回 Python 的 `complex`——正是对上面拆分逻辑的逆操作：

[Documentation/UserManual/SCPI_Examples/libreVNA.py:L126-L141](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/UserManual/SCPI_Examples/libreVNA.py#L126-L141)、[libreVNA.py:L152-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/UserManual/SCPI_Examples/libreVNA.py#L152-L177)

#### 4.3.4 代码实践：用 Python socket 收一批点并绘制

1. **实践目标**：写一个不依赖 `libreVNA.py` 的最小流式客户端，连接 19000 端口收满一圈 501 点，把 |S11| 转成 dB 打印/绘制，验证你理解了 JSON 行协议。
2. **操作步骤**：

   保存以下脚本（示例代码，非仓库原有文件）：

   ```python
   #!/usr/bin/env python3
   # 示例代码：最小 LibreVNA 流式客户端
   import socket, json, sys

   HOST, PORT, NPOINTS = "localhost", 19000, 501

   def main():
       dry = "--dry-run" in sys.argv
       if dry:  # 无硬件/无 GUI：只打印将要发生的事
           print(f"[dry-run] 将连接 {HOST}:{PORT}，收满 {NPOINTS} 行 JSON")
           return
       s = socket.create_connection((HOST, PORT), timeout=5)
       buf, points = b"", []
       while len(points) < NPOINTS:
           chunk = s.recv(4096)
           if not chunk:
               break
           buf += chunk
           while b"\n" in buf:            # 行协议：按换行切帧
               line, buf = buf.split(b"\n", 1)
               if not line:
                   continue
               d = json.loads(line)
               s11 = complex(d["measurements"]["S11_real"],
                             d["measurements"]["S11_imag"])
               points.append((d["frequency"], s11))
       s.close()
       for f, v in points:
           print(f"{f:>12} Hz  |S11| = {20*__import__('math').log10(abs(v)):.2f} dB")

   if __name__ == "__main__":
       main()
   ```

   有设备时：先在 GUI 偏好里启用 `VNA raw data`（端口 19000），设置 501 点扫描并运行，再执行脚本。
   无设备时：加 `--dry-run` 参数运行，或直接阅读脚本对照 `streamingserver.cpp` 核对每个字段名。
3. **需要观察的现象**：
   - TCP 流上读到的是**连续字节流**，必须自己按 `\n` 切帧（`recv` 不保证一次恰好一行——这正是 u3-l2 DecodeBuffer 讲过的"粘包"问题在 TCP 上的重现）。
   - 每行 JSON 的 `pointNum` 从 0 递增到 500；`frequency` 单位是 Hz；`measurements` 里的值是**线性**复数。
4. **预期结果**：打印出 501 行按频率递增的 dB 值。若想画图，把 `points` 交给 matplotlib：`plt.plot([f for f,_ in points], [20*np.log10(abs(v)) for _,v in points])`。无硬件环境下的实际运行结果**待本地验证**；协议字段可与 [streamingserver.cpp:L23-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L23-L46) 逐键核对。
5. 进阶对照：跑通后阅读官方 `capture_live_data.py`，比较它与本脚本的差异（官方版还通过 SCPI 配置扫描、用线程常驻回调，而本脚本只做一次性采集）。

#### 4.3.5 小练习与答案

**练习 1**：两个客户端同时连到 19000 端口，第二个连上时会不会收到之前的历史数据？为什么？

**答案**：不会。`StreamingServer` 没有任何缓冲/回放机制，`addData` 只对"当下在 `sockets` 集合里且 isOpen 的连接"write；新连接从下一个产生的测量点开始接收。见 [streamingserver.cpp:L41-L45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L41-L45)。

**练习 2**：客户端如果读得慢（比如逐点做复杂计算），堆积的数据去哪了？

**答案**：服务器端对每个 socket 只调用 `QTcpSocket::write`，数据进入该 socket 的 Qt 写缓冲与操作系统发送缓冲；客户端不读，缓冲涨满后 TCP 流控（零窗口）会让服务器的 `write` 实际阻塞/缓存增长，极端情况下 GUI 线程可能被拖慢。这是"每点一推、无背压控制"设计的固有代价，也是为什么 u3-l2 强调过固件→USB 方向才需要反压处理，而这里的反压交给 TCP 自己。

**练习 3**：如何在不知道点数的情况下判断"一圈扫完了"？

**答案**：监控 `pointNum`——它从 0 单调递增，当再次出现 `pointNum == 0`（或观察到某个值之后突然回落）即是一圈的边界。官方示例则走了另一条路：先通过 SCPI 把点数设为已知（501），收到 `pointNum == 500` 即断定末点，见 [capture_live_data.py:L41-L44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/UserManual/SCPI_Examples/capture_live_data.py#L41-L44)。

## 5. 综合实践

**任务：给 LibreVNA 写一个"扫描质量监视器"。**

目标：把本讲三个模块串起来——理解平均器状态、选对数据分级、用流式接口取数。

1. **无硬件部分（代码走读）**：
   - 完成 4.1.4 的 reset 场景表。
   - 回答：如果要在远程监视"平均是否已攒满"（`settled`），你既可以通过 19000 端口观察数据，也可以通过 SCPI 查询。在 `VNA::SetupSCPI()` 注册的 `ACQuisition` 节点下找到对应命令（见 [vna.cpp:L1549-L1610](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1549-L1610)）：`AVG` 设置/查询平均次数、`AVGLEVel` 查询已平均圈数、`FINished` 查询是否攒满。
2. **有硬件/GUI 部分**：
   - 启用 19000 与 19001 两个通道，同时连接两个 socket；对一台已校准的设备分别收一圈，逐点比较两份数据的差异幅度——这个差异就是校准修正量。
   - 把 19000 的数据按 \(20\lg|S_{11}|\) 转成 dB，用 matplotlib 绘出 501 点曲线；再把 Averaging 从 1 改到 10，重复采集并叠加两条曲线，观察噪声底的变化是否接近 \(10\lg 10 = 10\) dB 的预期。
   - 无硬件时，上述两步全部改为"写出操作步骤 + 预期现象"的文字稿，并标注待本地验证。
3. **验收标准**：reset 场景表至少覆盖 4.1.4 参考答案中的 VNA 4 条 + SA 3 条；脚本能正确切帧、正确做 dB 换算；能解释 Raw 与 Calibrated 两份数据为何不同。

## 6. 本讲小结

- `Averaging` 是硬件无关的纯数据类：每个点号一个 deque 滑动窗口，VNA/SA 两个 `process` 重载把 `std::map` 归一化为 `vector<complex>` 后复用同一套 Mean/Median 统计。
- Mean 在线性复数域相干平均，SNR 提升 \(10\lg N\) dB；Median 按**模值**取中位，对突发尖峰免疫；两者由偏好 `Acquisition.useMedianAveraging` 选择。
- `currentSweep`（看第 0 点的窗口）与 `getLevel`（看末点的窗口）的首尾不对称，是校准/归一化"只在最后一圈取数"与单次扫描"攒满即停"两个机制的共同基础。
- reset 场景：VNA 有工具栏按钮、SettingsChanged、ConfigureDevice 前后共 4 处显式路径；SA 有按钮、SettingsChanged、扫描重启 3 处；两者还共享"测量个数变化即自动重置"的内部路径。`SetAveraging` 只裁剪不清空。
- `addStreamingData` 是数据分级分发器：VNA 在 Raw/Calibrated/Deembedded 三处、SA 在 Raw/Normalized 两处"打卡"，后四级是条件推送（校准/去嵌入/归一化激活才发），通道关闭时指针为空、零开销。
- `StreamingServer` 是一行一个 JSON 的 TCP 广播器：VNA 拆 `_real`/`_imag`（JSON 无复数），零扫宽以 `time` 换 `frequency`；默认五个端口（19000/19001/19002/19100/19101）全部关闭，需在 Preferences 中启用。

## 7. 下一步学习建议

- **单元 8（u8-l1）**：本讲结尾处的 `traceModel.addVNAData` / `addSAData` 是数据在 GUI 内部的最终归宿——Trace 与 TraceModel 如何存储、通知、成图，是下一单元的起点。
- **单元 10（u10-l3）**：本讲的流式输出与 `libreVNA.py` 只解决了"取数"；完整的自动化（配置扫描、触发校准、读状态）需要 SCPI，两章合起来才是 Python 控制 LibreVNA 的全貌，官方 `capture_live_data.py` 正是两条通道协同的范例。
- **延伸阅读**：对照 u3-l2 的 `DecodeBuffer`（设备字节流的拆帧）与本讲 4.3.4 的按行切帧，体会"TCP 流式传输需要应用层定界"这一通用问题在不同层的解法。
