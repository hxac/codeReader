# 校准求解器：从测量到误差模型

## 1. 本讲目标

上一讲（u9-l1）解决了「标准件的理论 S 参数从哪里来」——校准件模型给出了 Open/Short/Load/Through 各自的 `toS11()`/`toSparam()`。本讲解决另一半问题：**拿着标准件的实测数据和理论值，如何解出仪器自身的误差，再用误差去修正日常测量**。

学完本讲，你应该能够：

1. 说出 12 项误差模型中每一项的物理来源（方向性、反射跟踪、源匹配、接收机匹配、传输跟踪、隔离度），以及它们在 `Calibration::Point` 中的存放方式。
2. 跟踪「用户点击 Measure → 设备扫描 → 数据流入校准测量 → 全部点收齐 → 逐频率点求解」的完整调度链路。
3. 手动推导 SOL 三步测量对应的三个方程，读懂 `computeOSL()` 的闭式解，并理解 `correctMeasurement()` 如何用误差项反解真实 S 参数。
4. 解释当测量数量不足或质量不佳时，求解器的各级降级策略：SOLTwithoutRxMatch、ThroughNormalization、滑动负载替代、隔离度省略。

## 2. 前置知识

**误差模型（error box model）是什么？** 矢网并非理想仪器。你测量一个负载读到的 `S11m`（m = measured），其实是「仪器误差网络 + 被测件」级联后的结果。校准的任务就是把这个误差网络当作一个固定的「黑盒二端口」测出来（这一步叫求解误差项），之后每次测量都先在数学上把黑盒「除掉」。因为误差网络画出来像一个盒子套在仪器端口外面，所以常被称为 error box。

**为什么要「按频率点」求解？** 误差项（电缆损耗、耦合器方向性、匹配）都随频率变化。所以校准不是解出一组数，而是对扫描范围内**每一个频率点**各解一组误差项。LibreVNA 用 `std::vector<Point> points` 存放这张「误差项 vs 频率」的表格。

**信号流图与波量（power wave）。** S 参数描述的是反射波 b 与入射波 a 之比：\( S = b/a \)。仪器的各项误差都可以理解为信号在激励路径、耦合器、接收路径上的「走错路」：该走的路有损耗（跟踪项）、不该走的路有泄漏（方向性、隔离度）、端口自身有反射（匹配项）。本讲会用一个单端口方程把这三类误差串起来，不需要更深的流图代数。

**线性代数。** 多端口修正会用到复数矩阵的乘法与求逆（Eigen 库的 `MatrixXcd`），读者只需理解「矩阵乘法 = 级联、求逆 = 反解」即可。

**与本讲相关的既有结论**（来自前面单元）：

- `DeviceDriver::VNAMeasurement` 以 `"S21"` 这样的字符串为键存放线性复数测量值（u3-l1）。
- VNA 模式的数据上行链路是 `NewDatapoint → 平均 → 校准 → TraceModel`（u7-l1），本讲将把其中的「校准」环节展开。
- 校准件模型：理想 Short 的反射系数是 −1，理想 Open 是 +1，理想 Load 是 0（u9-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h) | `Calibration` 类声明：校准类型枚举、误差项 `Point` 结构、compute/canCompute/correctMeasurement 等接口 |
| [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp) | 本讲主战场：误差项求解（computeXXX）、修正应用（correctMeasurement）、可行性检查（canCompute）、频率栅格推断（hasFrequencyOverlap） |
| [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h) | 校准测量的类层次：`Base → OnePort（Open/Short/Load/…）`、`Base → TwoPort（Through/Line）`、`Isolation` |
| [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp) | 测量对象如何从 `VNAMeasurement` 里「抽取」自己需要的数据，以及理论值（getActual）与实测值（getMeasured）的插值取数 |
| [Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp) | 调度侧：`StartCalibrationMeasurements` 启动测量、`NewDatapoint` 中采数与调用 `correctMeasurement` |
| [Software/PC_Application/LibreVNA-Test/calibrationtests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp) | 无硬件即可运行的校准单元测试（频率栅格检测），本讲综合实践的样板 |

## 4. 核心概念与源码讲解

### 4.1 误差模型：把仪器的不完美写成 6 类系数

#### 4.1.1 概念说明

把矢量网络分析仪的每个端口想象成一个「有缺陷的测量装置」，它的缺陷可以拆成三类：

- **方向性 D（Directivity）**：激励信号没经过被测件、直接从耦合器泄漏进接收机的部分。接一个理想匹配负载时读到的残余 `S11` 就是 D。
- **反射跟踪 R（Reflection Tracking）**：激励路径损耗与接收路径增益的乘积。即使 DUT 是全反射的，读数幅度也会偏离 1、相位也会旋转，这部分统一由 R 描述。
- **源匹配 S（Source Match）**：从 DUT 看进仪器端口并非理想 50Ω，DUT 的反射波被端口再次反射回去，形成多次往返。这项误差与 DUT 的反射**相互作用**，所以它在方程的分母里。

这三项描述「端口对自己测量反射」的误差。测量传输（S21）时还有另外三项，作用在**每一对**端口上：

- **接收机匹配 L（Receiver Match）**：接收端口自身的失配。
- **传输跟踪 T（Transmission Tracking）**：正向传输路径的总损耗/增益。
- **隔离度 I（Isolation）**：信号不经 DUT、直接从激励端口串到接收端口的泄漏。

数一数：双端口时，反射类 3 项 × 2 个端口 = 6 项，传输类 3 项 × 2 个方向 = 6 项，**合计 12 项**——这就是经典的「12 项误差模型」。传统文献按「正向/反向」分成两组（EDF、ESF、ERF、ELF、ETF、EXF / EDR、ESR、ERR、ELR、ETR、EXR）；LibreVNA 的存放方式与此等价，但按「激励端口」组织，天然支持任意端口数。

#### 4.1.2 核心流程

单端口测量的信号流图可以浓缩成一个分式线性（Möbius）方程——实测值 \( S_m \)、真实反射系数 \( \Gamma \) 与三个误差项的关系：

\[
S_m \;=\; D \;+\; \frac{R \cdot \Gamma}{1 - S \cdot \Gamma}
\]

直觉解读：

- \(\Gamma = 0\)（理想负载）时 \( S_m = D \)——**负载测量直接暴露方向性**；
- \( S = 0 \)（理想源匹配）时 \( S_m = D + R\Gamma \)——误差退化为「平移 + 缩放」，一条直线；
- \( S \neq 0 \) 时分母 \( 1 - S\Gamma \) 让关系变成曲线——这就是为什么校准要解方程组而不能简单做除法。

反过来，已知误差项后，从测量值反解真实值只需把上式移项：

\[
\Gamma \;=\; \frac{S_m - D}{R + S\,(S_m - D)}
\]

多端口情形下，`correctMeasurement()` 用矩阵形式完成同样的反解：把每个端口的入射波排成矩阵 a、出射波排成矩阵 b，则真实 S 参数矩阵 \( \mathbf{S} = \mathbf{b}\,\mathbf{a}^{-1} \)。代码注释标明公式出处为论文 *"Multi-Port Calibration Techniques for Differential Parameter Measurements with Network Analyzers"*。

#### 4.1.3 源码精读

误差项的数据结构是内嵌类 `Calibration::Point`：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h:151-162](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L151-L162)：`Point` 持有一个频率点上的全部误差项——`D/R/S` 是长度为端口数的向量（每端口 3 个反射项），`L/T/I` 是端口数×端口数的矩阵（每对「激励端口 i → 接收端口 j」3 个传输项）；整个校准就是 `std::vector<Point> points` 这张按频率排列的表格。

求解前的「初始化误差盒」值得关注——默认值本身就是一套**恒等误差**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:748-763](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L748-L763)：`createInitializedPoint()` 把 `D=0、R=1、S=0、L=0、T=1、I=0` 填入新点。代入 4.1.2 的公式会发现这套系数使 \( S_m = \Gamma \)——即「不修正」。各种降级校准类型正是靠**保留部分默认值、只求解部分项**来实现的。

校准类型枚举定义了「能解到什么程度」的阶梯：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h:21-29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L21-L29)：`Type` 枚举列出 None / OSL / SOLT / SOLTwithoutRxMatch / ThroughNormalization / TRL。从 SOLT 到 ThroughNormalization 是一条「解的项数递减」的降级链，4.3 节详细展开。

对外还提供按项查询的接口，供 UI 与 SCPI 判断「当前校准含哪些项」：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h:104-110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L104-L110)：`hasDirectivity(port)`、`hasTransmissionTracking(src, rcv)` 等六个查询函数。

#### 4.1.4 代码实践

**实践目标**：验证「恒等误差盒」确实不改变数据，并理解 12 项的存放布局。

**操作步骤**：

1. 打开 `calibration.cpp:748-763`，抄下六个字段的初值。
2. 把这些初值代入 4.1.2 的反解公式 \( \Gamma = (S_m - D)/(R + S(S_m-D)) \)，手工化简（应得到 \(\Gamma = S_m\)）。
3. 数一数双端口 SOLT 时 `Point` 里实际存放多少个复数：`D/R/S` 各 2 个 + `L/T/I` 各 2×2 个 = 18 个存储位。思考：为什么是 18 个位置而不是 12 项？（提示：`L/T/I` 矩阵的对角线元素 `i==j` 从不参与计算，见 `computeSOLT()` 中 `if(i == j) continue`，有效项恰为 6 + 12 − 6 = 12。）

**需要观察的现象**：纯代数推导，无需运行程序。

**预期结果**：恒等初值代入反解公式后形式不变；存储布局是「3×N + 3×N²」个复数，双端口时有效误差项 12 个。

#### 4.1.5 小练习与答案

**练习 1**：接一个理想匹配负载（Γ=0）时，仪器读数由哪些误差项决定？

**答案**：由 \( S_m = D + R\Gamma/(1-S\Gamma) \) 可知 Γ=0 时 \( S_m = D \)，只有方向性项。这正是 Isolation 测量之外最「干净」的一项——也是为什么 Load 测量能直接标定 D。

**练习 2**：源匹配 S 在物理上是什么？为什么它出现在方程分母里？

**答案**：S 是从 DUT 看入仪器端口的等效反射系数。DUT 的反射波到达仪器端口后被部分反射回去、再次被 DUT 反射，形成 \( 1 + S\Gamma + (S\Gamma)^2 + \cdots = 1/(1-S\Gamma) \) 的多次往返级数，所以它以分母形式与 DUT 自身的 Γ 耦合，无法用简单的减法/除法消除。

**练习 3**：LibreVNA 按激励端口组织误差项，相比传统「正向/反向」12 项写法有什么好处？

**答案**：端口数不再写死为 2。`caltype.usedPorts` 是一个端口列表，D/R/S 按端口索引、L/T/I 按端口对索引，同一套数据结构与求解代码可直接支持 1/2/3/4 端口（配合 CompoundDriver 的虚拟多端口设备，u3-l3）。

### 4.2 测量调度：从「按下测量键」到「数据收齐」

#### 4.2.1 概念说明

求解之前必须先有数据。校准测量与普通测量的区别在于：**仪器测的还是 S 参数，但被测的对象换成了标准件**。调度系统要回答四个问题：

1. **一次测什么？** 用户勾选若干「校准测量」对象（如 Port1 的 Open、Short、Load）。若它们占用不同物理端口，可以一次扫描同时完成——例如 Port1 接 Open 的同时 Port2 接 Short，一次扫描采两组数。
2. **怎么知道测完了？** 扫描必须完整跑满一个 sweep 且达到设定的平均次数，中途的点不能混入旧数据。
3. **每个测量对象存什么？** 单端口测量只关心 `Spp` 一个数；Through 关心四个 S 参数；Isolation 要存全部端口的传输泄漏矩阵。
4. **这些测量够解方程吗？** `canCompute()` 用一张「必需测量清单」逐一核对，并推断出求解将使用的频率栅格。

#### 4.2.2 核心流程

```
用户点击 Take Measurement（或 SCPI CAL:MEASure）
   │
   ▼
Calibration::startMeasurements 信号 ──► VNA::StartCalibrationMeasurements (vna.cpp)
   │  停扫 → clearMeasurements(删除旧数据) → 重配设备 → 回调中 calMeasuring = true
   ▼
设备逐点上报 → VNA::NewDatapoint
   │  若 calMeasuring 且已是最后一个平均圈：
   │      Calibration::addMeasurements → 各测量对象 addPoint(m) 自取所需数据
   │      收到末点 (pointNum == npoints-1)：calMeasuring = false，measurementsComplete()
   ▼
用户点 Activate（或 SCPI CAL:ACTivate）
   │
   ▼
Calibration::canCompute：清单核对 + 频率交集推断（起点/止点/点数/线性或对数）
   │
   ▼
Calibration::compute：逐频率点调用 computeOSL/computeSOLT/… 生成 points 表
```

`canMeasureSimultaneously()` 的规则很简单：任何两个被选测量不得占用同一个物理端口；Isolation 测量要用到所有端口，因此不能与其他测量同时进行。

#### 4.2.3 源码精读

**测量对象的类层次与「自取数据」**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h:80-142](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h#L80-L142)：`OnePort` 基类承载 Open/Short/Load/SlidingLoad/Reflect 五种单端口测量，持有 `port` 与 `vector<Point>{frequency, S}`。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h:200-271](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h#L200-L271)：`TwoPort` 承载 Through/Line，额外有 `reverseStandard` 标志（标准件定义端口顺序与测量方向相反时使用）。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h:283-354](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h#L283-L354)：`Isolation` 直接从 `Base` 派生，`Point::S` 是可变大小的二维向量，能容纳任意端口数的泄漏矩阵。

三种 `addPoint` 展示了「各取所需」：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp:269-279](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L269-L279)：`OnePort::addPoint` 只摘取 `"S"+port+port` 这一个键（如 Port1 的 S11），其余测量一律忽略。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp:428-435](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L428-L435)：`TwoPort::addPoint` 先 `m.toSparam()` 把字符串键的测量拼成 S 参数矩阵，再 `reduceTo({port1, port2})` 裁剪出自己那两个端口——这使 Through 测量在多端口设备上也能正确取数。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp:622-644](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L622-L644)：`Isolation::addPoint` 遍历全部测量键，把 `"S21"` 之类的名字解析回接收/激励端口号，动态扩容二维矩阵后逐格存放。

**理论值与实测值的统一取数接口**（求解方程的左右两边）：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp:354-369](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L354-L369)：`getMeasured(f)` 对采样序列做插值取出任意频率处的实测值（越界返回 NaN）；`getActual(f)` 则委托给上一讲的校准件模型 `toS11(f)`。求解器只面对这两个对称的接口，完全不关心标准件是「系数描述」还是「Touchstone 文件描述」。
- [Software/PC_Application/LibreVNA-GUI/Util/util.h:98-119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L98-L119)：通用插值模板 `Util::interpolate`，靠 `Point` 上重载的 `operator*`/`operator+` 完成线性插值（复数 S 参数按实虚部线性内插）。

**同时测量的端口冲突检查**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp:177-218](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L177-L218)：`canMeasureSimultaneously()` 用一个 `set<int> usedPorts` 检查端口占用冲突；Isolation 分支直接要求集合大小为 1。

**VNA 模式侧的采数时机**（调度链的执行端）：

- [Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:1406-1435](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1406-L1435)：`StartCalibrationMeasurements` 先停扫、`clearMeasurements` 删除这组测量的旧数据、置 `calWaitFirst`，然后重配设备并在配置完成回调里才置 `calMeasuring = true`——注释解释了原因：避免把配置切换期间仍在处理的旧数据采进来。
- [Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:1042-1056](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1042-L1056)：`NewDatapoint` 中只在 `average.currentSweep() == averages`（最后一个平均圈，u7-l4 的「末圈取数」）时调用 `cal.addMeasurements`；收到末点时关掉 `calMeasuring` 并发出 `measurementsComplete`，同时向进度对话框汇报百分比。

**可行性检查与频率栅格推断**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:1780-1856](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1780-L1856)：`canCompute()` 按校准类型组装 `required` 清单（SOLT 走 `[[fallthrough]]` 复用 OSL 的清单构造），逐项 `findMeasurement` 核对存在性，再检查 `readyForCalculation()`（校准件已指定且有数据，见 [calibrationmeasurement.h:96-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.h#L96-L97)），最后把所有通过的测量交给 `hasFrequencyOverlap`。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:1804-1810](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1804-L1810)：一个重要的运行时分支——某端口滑动负载测量数 ≥3 时用滑动负载，否则退回普通 Load。这是「数据多寡改变求解路径」的第一处降级。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:2039-2110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L2039-L2110)：`hasFrequencyOverlap` 计算所有测量频率范围的**交集**（start 取各测量最小频率的最大值、stop 取各测量最大频率的最小值）、用最细的频率分辨率推算点数，并通过比较「相邻点频率差的波动」与「相邻点频率比的波动」投票判定这组测量是线性扫还是对数扫——这个判定直接决定 `compute()` 里误差项表格的频率取法。

#### 4.2.4 代码实践

**实践目标**：跑通仓库自带的校准单元测试，验证线型/对数栅格检测逻辑。

**操作步骤**：

1. 进入 `Software/PC_Application/LibreVNA-Test` 目录，用 qmake6 + make 编译测试工程（依赖与 GUI 相同，见 u1-l3；`.pro` 已把整个 GUI 编进测试）。
2. 运行生成的 `LibreVNA-Test`（或 `./LibreVNA-Test CalibrationTests` 只跑校准测试类）。
3. 对照 [Software/PC_Application/LibreVNA-Test/calibrationtests.cpp:7-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L7-L47) 阅读 `LinearDetection`：它手工构造 1001 个线性分布的频率点、`S11=0` 的假测量，断言 `hasFrequencyOverlap` 检出正确的起止频率、点数和「非对数」标志。
4. 再看 [第 91-136 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L91-L136) 的 `MixedDetection`：一个测量线性、两个对数时，按「投票多数」判为对数。

**需要观察的现象**：测试输出中 `LinearDetection`、`LogDetection`、`MixedDetection` 三项均 PASS。

**预期结果**：全部通过（这三条用例在仓库中随 CI 维护）。若你修改了 `hasFrequencyOverlap` 的判定逻辑，这三条用例是最快的回归手段。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `StartCalibrationMeasurements` 要先 `clearMeasurements` 再启动扫描？

**答案**：同一校准测量可能重复执行（上次失败、换了标准件重测）。旧数据与新扫描的频率范围或标准件可能不一致，混在一起会污染求解。删除后由 `calWaitFirst` 保证从新扫描的第 0 点开始重新积累。

**练习 2**：`OnePort::addPoint` 里 `if(m.measurements.count(measurementName) > 0)` 的守卫有什么作用？

**答案**：设备上报的 `VNAMeasurement` 不一定包含所有 S 参数（例如单激励配置下可能只有被激励端口的参数）。守卫确保只有当本次数据里确实有 `Spp` 时才记录，避免把空数据压进序列。

**练习 3**：如果 Open 测量扫了 1–6 GHz、Short 测量只扫了 1–3 GHz，`hasFrequencyOverlap` 会给出什么结果？校准的有效范围是？

**答案**：交集为 1–3 GHz，校准只在这个范围内有效（超出范围的测量点被舍弃）。这也解释了该函数名的含义——取所有测量频率范围的「重叠」部分。

### 4.3 求解与应用：解方程、存系数、修数据

#### 4.3.1 概念说明

**求解（compute）**：对栅格上的每个频率点，把「实测值（getMeasured）」与「理论值（getActual）」代入误差模型方程，解出该点的全部误差项，填入 `points` 表。不同校准类型对应不同的方程组：

| 类型 | 求解内容 | 所需测量 |
| --- | --- | --- |
| OSL | 每端口 D/R/S | 每端口 Open+Short+Load（或 ≥3 次滑动负载） |
| SOLT | OSL 全部 + 每对端口 L/T/I | OSL + 每对端口 Through（Isolation 可选） |
| SOLTwithoutRxMatch | 同 SOLT 但 L 强制置 0 | 同 SOLT |
| ThroughNormalization | 仅 T = S21m/S21ideal，其余保持理想值 | 每对端口 Through |
| TRL | 通过 Through+Line+Reflect 自校准 | 每端口 Reflect + 每对端口 Through+Line |

**应用（correctMeasurement）**：日常测量时，对每个到来的 `VNAMeasurement`，先按频率从 `points` 表取出（必要时插值出）误差项，再用 \( \mathbf{S} = \mathbf{b}\,\mathbf{a}^{-1} \) 反解真实 S 参数，原地写回测量对象。

**存系数的一个反直觉设计**：`.cal` 文件里**不存误差项**，只存「原始测量 + 校准件 + 校准类型」；加载时（`fromJSON`）重新调用 `compute()` 把系数算回来。好处是：校准件定义日后被修正（比如给 Open 补上了边缘电容系数）后，重新加载旧测量文件即可得到更准的系数，历史数据永不「固化」。

#### 4.3.2 核心流程

**SOL 三步测量的三个方程**（单端口，对每个频率点）：

把标准件的真实反射系数记为 \( o_c, s_c, l_c \)（来自校准件模型），实测值记为 \( o_m, s_m, l_m \)，代入 4.1.2 的误差模型得到三个方程：

\[
o_m = D + \frac{R\,o_c}{1-S\,o_c},\qquad
s_m = D + \frac{R\,s_c}{1-S\,s_c},\qquad
l_m = D + \frac{R\,l_c}{1-S\,l_c}
\]

三个方程、三个未知数 \(D, R, S\)。代码采用闭式解（而非数值迭代），并借助中间量 \(\Delta = DS - R\) 使三个未知数的表达式共享同一个分母 `denom`。理想校准件（\(l_c=0, o_c=+1, s_c=-1\)）时方程大幅简化，可用于手工验证（见 4.3.5 练习 1）。

**SOLT 的传输项**：拿到 D/R/S 后，接 Through 标准件再测一组四个 S 参数。测量值与理论值（Through 的 S 参数矩阵 \(S_{ideal}\)，含其 \(\Delta_S = S_{11}S_{22}-S_{21}S_{12}\)）之间的差异恰好足以解出 L 和 T；隔离度 I 直接取 Isolation 测量值（没测就置 0）。

**compute 的总控流程**：

```
compute(type)
  ├─ canCompute(type, &start, &stop, &numPoints, &isLog)  ← 失败直接返回 false
  ├─ caltype = type
  ├─ 清空 points
  ├─ for i in 0..numPoints-1:
  │     f = 线性或对数栅格上的第 i 个频率
  │     p = computeOSL/SOLT/SOLTwithoutRxMatch/ThroughNormalization/TRL (f)
  │     points.push_back(p)
  └─ emit activated(caltype)          ← VNA 收到后刷新状态栏
```

**correctMeasurement 的反解流程**：

```
correctMeasurement(&d)
  ├─ caltype == None → 直接返回
  ├─ 按频率在 points 表中定位；区间内则幅相插值出一个临时 Point
  ├─ 组装实测 S 矩阵：S(j,i) = d["S_rcv,src"]，非对角先减去隔离度 I
  ├─ 组装波量矩阵：
  │     i == j（激励口）: a(j,i) = 1 + S·(S_m−D)/R,  b(j,i) = (S_m−D)/R
  │     i != j（接收口）: a(j,i) = L·S_m/T,          b(j,i) = S_m/T
  ├─ S_corrected = b · a⁻¹
  └─ 把修正值逐项写回 d.measurements
```

#### 4.3.3 源码精读

**总控与频率栅格**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:1858-1902](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1858-L1902)：`compute()` 先用 `canCompute` 拿到栅格参数，再按 `isLog` 选择线性（1877 行）或对数（1879 行）插值出每个频率，switch 分发到五个求解函数；任何频率点抛出异常都会清空 `points` 并清空 `usedPorts`（1895-1898 行），保证半成品系数不会被误用。

**SOL 闭式解**（本讲最核心的一段代码）：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:765-808](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L765-L808)：`computeOSL()` 对每个端口取出 Short/Open/Load 的实测值（774-775 行 `getMeasured`）与理论值（776-777 行 `getActual`）；779-800 行处理滑动负载分支——≥3 次滑动负载测量时用 `Util::findCenterOfCircle` 拟合测量圆心当作「理想负载测量值」（滑动负载反射系数恒为 1、只有相位转动，多次测量的轨迹是圆，圆心即真实负载响应），否则用普通 Load；801-805 行就是三个方程的闭式解：公共分母 `denom`、方向性 `D`、源匹配 `S`、中间量 `delta`，最后 `R = D*S − delta`。
- [Software/PC_Application/LibreVNA-GUI/Util/util.cpp:104](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp#L104)：`findCenterOfCircle` 的代数圆拟合实现（Kåsa 法风格的最小二乘）。

**SOLT 的传输项求解**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:810-851](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L810-L851)：`computeSOLT()` 第 812 行先调用 `computeOSL(f)` 拿到反射项；对每一对端口（815-817 行跳过对角），824-837 行取 Through 测量——正向 Through 缺失时自动用反向 Through 并 `swapPorts(1,2)`（这是「测量方向不对称也能求解」的降级）；838-842 行取 Isolation，没有该测量则 isolation 置 0（省略隔离度修正）；844-847 行代入闭式公式解出接收机匹配 L、传输跟踪 T，并把隔离度原样存入 I。

**两级降级类型**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:853-862](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L853-L862)：`computeSOLTwithoutRxMatch()` 完整求解 SOLT 后把全部 L 置 0。注释说明动机：Through 标准件插损很大时，L 的解会被噪声淹没，与其引入噪声不如假设接收机理想匹配。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:864-903](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L864-L903)：`computeThroughNormalization()` 是最粗的修正：反射项直接保留 `createInitializedPoint` 的理想值（D=0、S=0、R=1，869-874 行），唯一求解的传输项是 898 行的 `T = S21实测/S21理论`——纯粹的「归一化」，等价于把 Through 当作已知衰减器除掉。

**TRL 求解器**（选读）：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:905-1067](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L905-L1067)：`computeTRL()` 不需要已知量值的标准件——Through/Line/Reflect 只要求「同属一种反射标准」。流程：1010-1012 行把 Through 和 Line 测量转成 T 参数（级联参数）并相除分离出误差盒；1015 行解一元二次方程（`Util::solveQuadratic`）得到误差盒参数的两个根，1018-1020 行按「模值小者为 b」的规则选根；1038-1042 行用 Reflect 测量的符号（开路/短路）给 a 定符号——TRL 数学较深，代码逐行标注了 UIUC 讲义页码（1008 行的注释），建议对照原文献阅读。求解结果最后从 T 参数转回 S 参数填入 Point（1051-1061 行），TRL 无隔离度测量可用，I 恒为 0。

**应用修正**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:325-399](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L325-L399)：`correctMeasurement()`。339-353 行定位频率：低于表头/高于表尾直接取端点（外推退化为端值），区间内用 `lower_bound` 找到包围点并插值；365-372 行按 `"S"+rcv+src` 命名约定抓取测量组成 S 矩阵，非对角元素先减去隔离度 I（367-369 行）——隔离度修正在这里、而不是在求解端完成；374-387 行按 4.3.2 的公式组装 a/b 矩阵；388 行 `S = b * a.inverse()` 一行完成反解；390-398 行把结果写回 `d.measurements`。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:2191-2229](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L2191-L2229)：`Point::interpolate()` 对每个误差项调用 `Util::interpolateMagPhase`。
- [Software/PC_Application/LibreVNA-GUI/Util/util.cpp:241-257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp#L241-L257)：`interpolateMagPhase` 先做**相位解卷绕**再对幅度和相位分别线性插值——若直接对复数实虚部插值，两个频率点上相位跨过 ±π 的误差项会被插出一个错误的中间值。

**调用点与插值状态提示**：

- [Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:1058-1062](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1058-L1062)：`NewDatapoint` 中 `cal.correctMeasurement(m_avg)` 一行触发修正；修正前后的数据分别以 Raw 与 Calibrated 级别送入流式输出（u7-l4）。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:1074-1106](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1074-L1106)：`getInterpolation()` 判断当前扫描设置与系数表的匹配程度，返回 Unchanged/Exact/Interpolate/Extrapolate/NoCalibration 五态，VNA 据此在状态栏提示「校准插值中/外推中」等告警。

**系数的可视化与持久化**：

- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:1108-1169](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1108-L1169)：`getErrorTermTraces()` 把 `points` 表中的每个误差项导出成一条 Trace（如 `Directivity_Port1`），可直接在 GUI 里画图检视——这是判断校准质量的最直观工具。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:1462-1485](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1462-L1485)：`toJSON()` 只序列化测量、校准件、类型与端口——**不含误差项**。
- [Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp:1506-1529](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1506-L1529)：`fromJSON()` 的 format 3 分支重建全部测量后，只要类型非 None 就重新 `compute(ct)` 把系数算回来。

#### 4.3.4 代码实践

**实践目标**：写出「读取校准测量 → 组建方程组 → 求解误差项 → 存系数」四步在源码中的精确位置，并手工推导 SOL 方程组的简化解。

**操作步骤**：

1. 制作一张四步对照表（参考答案见下方「预期结果」），每行写：步骤、函数、文件:行号。
2. 用理想校准件（\(o_c=+1, s_c=-1, l_c=0\)）手工解三个方程：
   - 由 \(\Gamma=0\) 的 Load 方程立即得 \(D = l_m\)；
   - 令 \(a \equiv o_m - l_m = \dfrac{R}{1-S}\)、\(b \equiv l_m - s_m = \dfrac{R}{1+S}\)；
   - 从两式消去 \(R\) 解出 \(S = \dfrac{a-b}{a+b}\)，再回代得 \(R = \dfrac{2ab}{a+b}\)。
3. 把第 2 步的结果与 `computeOSL()` 801-805 行的通用公式对照：将 \(o_c=1, s_c=-1, l_c=0\) 代入 `denom`、`D`、`S`、`delta` 四个表达式，验证逐项化简后与你的手工解一致（801 行的 `denom` 退化为 \(o_m - s_m = a+b\)）。

**需要观察的现象**：纯纸面推导；第 3 步代入时注意 `delta` 表达式中 \(l_c\) 与 \(s_c\) 乘在测量值前面的位置。

**预期结果**：四步对照表如下——

| 步骤 | 代码位置 |
| --- | --- |
| 读取校准测量 | `computeOSL()` 内 `getMeasured(f)`/`getActual(f)`：[calibration.cpp:774-777](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L774-L777)；数据积累入口 `addMeasurements`/`addPoint`：[calibration.cpp:1925-1934](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1925-L1934) |
| 组建方程组 | 三个方程隐含在「实测值 vs 理论值」的配对中：Short/Open/Load 各一对，[calibration.cpp:772-800](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L772-L800) |
| 求解误差项 | 闭式解四行：[calibration.cpp:801-805](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L801-L805)；逐点循环与分发：[calibration.cpp:1874-1894](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1874-L1894) |
| 存系数 | 内存：`points.push_back(p)`：[calibration.cpp:1893](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1893)；落盘时不存系数、加载时重算：[calibration.cpp:1462-1485](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1462-L1485) 与 [calibration.cpp:1525-1527](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1525-L1527) |

#### 4.3.5 小练习与答案

**练习 1**：接理想负载时读数 \(l_m\) 直接等于 D，那为什么还需要 Open 和 Short 两次测量？

**答案**：Load 方程只给出 D 一个方程。源匹配 S 与反射跟踪 R 藏在另外两个方程里：Open/Short 提供了两个**幅值接近 1 但符号相反**的已知反射，正是它们的差异让 \(S\)（分母项）和 \(R\)（分子项）能够分离。只有 Load 时方程组欠定。

**练习 2**：`computeSOLTwithoutRxMatch` 与 `computeThroughNormalization` 都是「少解几项」，两者的适用场景有何不同？

**答案**：SOLTwithoutRxMatch 保留完整 OSL 反射修正和传输跟踪，只放弃接收机匹配 L——适用于 Through 件插损大、L 项信噪比差的场景。ThroughNormalization 连反射误差都不修（D=0、R=1、S=0），只对传输做归一化——适用于没有 Open/Short/Load、或只需要粗略传输测量的场景。

**练习 3**：`correctMeasurement()` 里，隔离度修正为什么放在「抓测量」阶段（367-369 行减 I）而不是像 D/R 那样进 a/b 矩阵公式？

**答案**：隔离度是**加性**泄漏（接收机在无激励时也读到的串扰），与 DUT 无关，直接从传输测量里减掉即可；而 D/R/S/L/T 与 DUT 的反射相互作用（分母耦合），必须进入矩阵反解。加性误差先减、乘性误差后除，这也是 12 项模型里「泄漏类」与「跟踪/匹配类」的分野。

**练习 4**（进阶）：`compute()` 的 catch 块清空了 `points` 与 `usedPorts`，但没有重置 `caltype.type`。结合 `correctMeasurement()` 开头的守卫条件（328 行）思考：什么场景下这会构成隐患？

**答案**：`correctMeasurement` 只检查 `caltype.type == Type::None` 就访问 `points.front()`。若某次 compute 抛异常（如 TRL 在某频点找不到适用的 Line 标准），`points` 为空而 `type` 仍非 None，下一次测量数据到来时 `points.front()` 将解引用空向量。实践中 `canCompute` 前置过滤掉了绝大多数异常路径，且 VNA::ApplyCalibration 失败时会调用 `DisableCalibration()`，所以该路径不易触达——但这是一个值得在阅读时注意的健壮性细节（待本地验证具体可触发性）。

## 5. 综合实践

**任务：编写一个「正演—反演」闭环校准测试（无需任何硬件）。**

思路：`CalibrationTests` 是 `Calibration` 的友元类（[calibration.h:16-17](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L16-L17)），可以像仓库自带的 `LinearDetection` 一样直接访问其内部。我们选定一组「真值」误差项，用 4.1.2 的正演公式把理想标准件「测」出来，喂给 `Calibration` 求解，再检查解出来的误差项是否回到真值。

**步骤**：

1. 在 `Software/PC_Application/LibreVNA-Test/calibrationtests.cpp` 中仿照 [LinearDetection（第 7-47 行）](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/calibrationtests.cpp#L7-L47) 新增一个测试函数，并在 `calibrationtests.h` 的 private slots 中声明（示例代码，非仓库原有内容）：

   ```cpp
   void CalibrationTests::ForwardBackwardOSL()
   {
       const std::complex<double> D_true = {0.01, -0.02};   // 任选小误差
       const std::complex<double> R_true = {0.95, 0.03};
       const std::complex<double> S_true = {0.05, 0.01};
       Calibration cal;
       cal.getKit().setIdealDefault();   // 理想校准件: open=+1, short=-1, load=0
       // ...创建 Open/Short/Load 测量对象（照抄 LinearDetection 的 16-24 行）
       for (int i = 0; i < 101; i++) {
           double f = 1e6 + i * 1e6;
           // 用 S_m = D + R*Γ/(1 - S*Γ) 分别以 Γ=+1/-1/0 正演出 o_m/s_m/l_m
           // 填入 DeviceDriver::VNAMeasurement::measurements["S11"] 后 addPoint
       }
       Calibration::CalType ct{Calibration::Type::OSL, {1}};
       QVERIFY(cal.compute(ct));
       // friend 权限下直接检查 cal.points[k].D[0] 与 D_true 的误差 < 1e-9
   }
   ```

2. 编译并运行 `LibreVNA-Test CalibrationTests`。
3. 把某个真值误差改大（例如 D 取 0.2）重复实验，观察解出的系数是否仍精确回归。
4. （思考题）若正演时故意把 Load 的 Γ 从 0 改成 0.05（模拟「校准件模型与实物不符」），解出的 D 会怎样偏移？

**预期结果**：第 2 步中解出的 D/R/S 与真值之差在浮点精度量级（闭式解无迭代误差，唯一误差来自频率插值——测试栅格一致时为零）；第 4 步中 Load 的模型偏差会**全部**落入 D 的解里（因为理想 Load 方程 \(l_m = D\)），这正是 u9-l1 强调「校准件建模精度决定校准上限」的数值体现。若你尚未搭建 Qt 测试环境，第 1-2 步标注「待本地验证」，第 3-4 步的结论可由 4.3.5 练习 1 的公式直接推出。

## 6. 本讲小结

- LibreVNA 的误差模型按**激励端口**组织：每端口 D/R/S 三个反射误差项、每对端口 L/T/I 三个传输误差项，双端口合计 12 项，全部按频率点存放在 `std::vector<Calibration::Point>` 表中。
- 测量调度链：`startMeasurements` 信号 → VNA 停扫清数据重配 → 末平均圈逐点 `addPoint` 自取所需 S 参数 → 末点 `measurementsComplete`；`canCompute` 用必需清单 + 频率交集（含线性/对数栅格投票判定）判定可解性。
- `computeOSL` 用闭式解一次解出 D、S 与中间量 delta（R = D·S − delta）；`computeSOLT` 在此基础上借 Through 的实测/理论 S 参数解出 L/T，隔离度 I 直接取自 Isolation 测量（缺省为 0）。
- 应用端 `correctMeasurement` 以 \( \mathbf{S} = \mathbf{b}\,\mathbf{a}^{-1} \) 矩阵反解真实 S 参数，误差项按频率做**幅相插值**（先解卷绕相位）。
- 降级阶梯贯穿始终：滑动负载 ≥3 次走圆心拟合、Isolation 可省略、Through 可反向复用、SOLTwithoutRxMatch 弃 L、ThroughNormalization 只归一化 T——测量不足或质量不佳时求解器逐级放弃项数，而不是直接失败。
- `.cal` 文件不存误差项，只存「测量 + 校准件 + 类型」，加载时重新求解——修正校准件模型即可改善历史校准。

## 7. 下一步学习建议

- **u9-l3（LibreCAL：电子自动化校准件）**：本讲的测量调度要用户手动换标准件；下一讲看 LibreCAL 如何用电子开关把「换件」自动化，复用同一套 `startMeasurements` 调度。
- **u9-l4（去嵌入框架）**：校准把仪器误差修掉之后，夹具/电缆的残差交给 DeembeddingOption 插件继续修，两者在 `VNA::NewDatapoint` 中先后接力（本讲 vna.cpp:1058-1068 已露端倪）。
- **动手验证**：完成第 5 节综合实践后，可进一步把 `getErrorTermTraces()` 导出的 Directivity/SourceMatch Trace 画在 Smith 图上，直观感受「方向性是一小团泄漏、源匹配是端口反射」的物理图像；TRL 求解器（computeTRL）建议对照 [代码注释引用的 UIUC TRL 讲义](http://emlab.uiuc.edu/ece451/notes/new_TRL.pdf)（外部资料）阅读。
