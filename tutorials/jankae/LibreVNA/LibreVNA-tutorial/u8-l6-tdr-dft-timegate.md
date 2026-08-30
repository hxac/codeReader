# 时频变换实战：TDR、DFT 与时间门

## 1. 本讲目标

学完本讲，你应该能够：

1. **推导**从频域 S 参数到时域冲激响应的 IFFT 关系：为什么「测一片频谱」能等价于「向电缆发一个冲激脉冲」。
2. **解释**窗类型（Rectangular/Hamming/Hann/Blackman/Gaussian）对 TDR 时域分辨率与旁瓣（振铃）的影响，并能在源码中找到加窗与补零的确切位置。
3. **走通**时间门的完整回路：TDR（频→时）→ TimeGate（时域截断）→ DFT（时→频），理解仓库把这三级打包成 `TimeDomainGating` 复合运算的设计。
4. **读懂**底层 FFT 引擎（Nayuki 库）的分派策略：2 的幂走 radix-2，任意长度走 Bluestein，以及「逆变换不做缩放」这一影响上层代码的契约。

## 2. 前置知识

### 2.1 时域反射计（TDR）的物理直觉

TDR（Time Domain Reflectometry，时域反射计）好比「电缆雷达」：向被测电缆发一个极窄的脉冲，若线上有阻抗突变（连接器、开路、短路），脉冲会在那里反射回来。**反射回来的时间对应故障距离，反射的幅度与极性对应阻抗变化的性质**（开路反射为正、短路为负）。

传统 TDR 是一台独立的仪器（发窄脉冲 + 采样示波器）。LibreVNA 的做法是「算出来的 TDR」：VNA 先在频域测得一片 S 参数，再对它做逆离散傅里叶变换（IDFT），就得到等效的时域冲激响应。本讲 4.1 会推导这中间的数学条件。

两个工程要点先记住：

- 反射传播是**往返**的：时域图上 1 ns 的峰，对应单程距离 \( d = v \cdot t / 2 \)（\(v\) 是线上波速，典型同轴电缆约 \(2\times10^{8}\,\mathrm{m/s}\)）。
- **冲激响应**积分一次就是**阶跃响应**——阶跃响应的样子更接近传统 TDR 示波器波形，LibreVNA 两种都能显示。

### 2.2 离散傅里叶变换的对偶关系

设频域有 \(M\) 个等间隔样本、间隔 \(\Delta f\)，则逆变换得到的时域序列满足：

\[
\Delta t = \frac{1}{M \cdot \Delta f}, \qquad T_{\text{span}} = M \cdot \Delta t = \frac{1}{\Delta f}
\]

- \(\Delta t\)：时域采样间隔（时间分辨率的上限之一）；
- \(T_{\text{span}}\)：时域记录总长——超过 \(1/\Delta f\) 的反射会「绕回」混叠。

而时域分辨率还受**总带宽**限制：频谱只覆盖到 \(f_{\max}\)，相当于时域信号被带宽截断，矩形截断的冲激核是 sinc 形，第一零点在 \(1/(2f_{\max})\)。这就是「扫得越宽，看得越清」的定量表述。

### 2.3 窗函数：用主瓣换旁瓣

在有限带宽上截断频谱，等价于乘了矩形窗，sinc 核的旁瓣会让时域图上出现「振铃」，小反射可能被旁瓣淹没。换成两端渐变的窗（Hann、Hamming、Blackman……），旁瓣大幅下降，代价是主瓣变宽（分辨率变差）。这是贯穿本讲的核心权衡，4.2 会给出对照表。

### 2.4 承接上一讲（u8-l5）

本讲的所有节点都是 `TraceMath` 子类，沿用上一讲建立的认知：

- 运算节点经 `assignInput()` 串成链，对外读取一律委托链上最后一个启用节点 `lastMath`；
- 每个节点用 `outputType(inputType)` 声明输入域→输出域的映射，域失配则 `Invalid`：**TDR 是 Frequency→Time，TimeGate 是 Time→Time，DFT 是 Time→Frequency**——三者恰好能首尾相接；
- 数据变更靠 `outputSamplesChanged` 信号接力，计算线程与 GUI 线程之间用 `dataMutex` 保护；
- 工厂 `createMath()` 可以一次返回多个节点（复合运算）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Traces/Math/tdr.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp) | TDR 节点：频→时变换的全部算法，含 lowpass/bandpass 两种模式、DC 外推、加窗、补零、阶跃响应触发；计算在专用后台线程 `TDRThread` 中 |
| [Traces/Math/dft.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp) | DFT 节点：时→频变换，是 TDR 的逆运算；负责剥离 TDR 补的零、可选撤销 TDR 的窗 |
| [Traces/Math/timegate.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp) | 时间门节点：用时域逐点乘法实现带通/陷波门；含可拖拽门沿的编辑图 `TimeGateGraph` |
| [Traces/Math/windowfunction.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.cpp) / [.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.h) | 可复用的窗函数组件（非 TraceMath），被 TDR、DFT、TimeGate 三个节点各持一份；提供 `apply`/`reverse` 与编辑 UI |
| [Traces/fftcomplex.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp) / [.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h) | 底层 FFT 引擎：第三方 Nayuki 库（MIT），任意长度复数 FFT 与 `fftshift` |
| [Traces/Math/tracemath.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp) | 工厂 `createMath()`：`TimeDomainGating` 在此被展开为 TDR+TimeGate+DFT 三节点链；阶跃响应的累加积分也在这里 |
| [LibreVNA-Test/ffttests.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp) | FFT 引擎的单元测试，用 5 点（非 2 的幂）数据验证 Bluestein 路径与缩放契约 |

> 注意与 FPGA 侧区分：单元 6 讲过 FPGA 里也有一个 `DFT.vhd`（片上 96 bin 频谱分析）和带 Kaiser 窗的 `Windowing.vhd`。本讲全部是 **PC 端 GUI 的数学节点**，两者只是同名亲戚，不要混淆。

## 4. 核心概念与源码讲解

### 4.1 TDR 变换：从 S 参数到时域冲激响应

#### 4.1.1 概念说明

TDR 节点解决的问题：**把 VNA 在频域测得的 S 参数「翻译」成时域波形，从而看出每个反射发生在多远、有多强**。这在排查连接器、转接头、夹具缺陷时比频域纹波直观得多。

翻译不是无条件的，数学上要求频点构成「从直流开始的等间隔谐波栅格」。这引出两种工作模式：

- **Lowpass 模式**：频点为 \( f_k = k f_1,\ k=1..N \)（起止频率成整数比、起点接近直流）。此时频谱可做**共轭对称延拓**并外推出直流点，IFFT 结果是**实数**冲激响应，还能积分出阶跃响应——最接近传统 TDR。
- **Bandpass 模式**：频点是任意一段窄带（如波导频段、远离直流的测量）。无法构造共轭延拓，直接对测量带做 IFFT，得到**复数**冲激响应：反射位置和幅度仍有效，但直流信息缺失，**不支持阶跃响应**。

#### 4.1.2 核心流程

TDR 的计算流程（发生在专用后台线程里）：

```text
输入频域数据 (≥2 点)
   │
   ├─ lowpass: 校验谐波栅格 → 共轭对称延拓成 2N+1 点 → 外推/手填直流点
   ├─ bandpass: 直接取 N 个测量值
   │
   ├─ 频域加窗 (window.apply)
   ├─ 记录 unpaddedInputSize
   ├─ 可选补零 (padding/2 个零插在两端)
   ├─ Fft::shift(true)   ── 把直流分量转到下标 0
   ├─ Fft::transform(逆) ── IFFT
   ├─ Fft::shift(false)  ── 把 t=0 转到序列中央
   │
   ├─ 写输出: x = Δt·(i−M/2), y = 结果/M   （Δt = 1/(M·Δf)）
   └─ lowpass 模式触发阶跃响应（对实部做累加积分）
```

**数学推导（lowpass 模式）**：设测量满足 \( f_k = k f_1 \)。构造长度 \( M = 2N+1 \) 的频谱：

\[
X[k] = \begin{cases} S(k f_1), & 1 \le k \le N \\ \overline{S((M-k) f_1)}, & -N \le k \le -1 \\ S(0), & k = 0 \end{cases}
\]

由共轭对称性 \( X[-k]=\overline{X[k]} \)，IFFT 结果必为实序列：

\[
h[n] = \frac{1}{M} \sum_{k=-N}^{N} X[k]\, e^{j 2\pi k n / M}
\]

（代码中体现为：逆变换后再整体除以 \(M\)，见 4.1.3。）

三个关键量：

| 量 | 公式 | 含义 |
|---|---|---|
| 时域采样间隔 | \( \Delta t = \frac{1}{M f_1} \) | 时域波形的时间粒度 |
| 最大不模糊时域范围 | \( \frac{1}{f_1} \) | 更远的反射混叠绕回 |
| 距离分辨率（矩形窗） | \( \approx \frac{1}{2 f_{\max}} \)（第一零点） | \(f_{\max} = N f_1\)，两个更近的反射会粘连 |

**直流点的意义**：\(X[0]\) 就是反射系数的直流值。lowpass 模式下 VNA 测不到真正的直流，代码用最低两个频点做线性外推：

\[
|X[0]| = 2|S(f_1)| - |S(f_2)|, \qquad \angle X[0] = 2\angle S(f_1) - \angle S(f_2)
\]

这个值直接决定**阶跃响应的终值**（阶跃响应 = 冲激响应的积分，终值即 \(S(0)\)），所以外推质量差时阶跃响应的「终点高度」会失真——代码也允许手动填 DC 幅度/相位。

**补零的作用**：在频谱两端（即 \(|f|>f_{\max}\) 处）补零使 \(M\) 变大，\(\Delta t = 1/(M f_1)\) 变小——这是**时域插值**，让时域波形更平滑好读，但**不增加任何信息**（带宽没变，分辨率不变）。

**加窗的作用**：窗乘在**频域**上，等价于时域与窗的变换核做**卷积**（平滑），压制截断带来的 sinc 旁瓣（振铃），代价是主瓣加宽、相邻反射更易粘连。具体对照表见 4.2.2。

#### 4.1.3 源码精读

**线程模型**：TDR 计算量大（每次扫描全量 IFFT），因此节点构造时就启动最低优先级专用线程，并用信号量通知；GUI 侧每次数据到达只 `release()` 信号量，线程把积压的信号量一次清空（合并多次更新为一次计算）——[tdr.cpp:212-225](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L212-L225) 是触发侧（不足 2 个样本则清空输出并告警 "Not enough input samples"），[tdr.cpp:270-272](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L270-L272) 是线程侧的信号量合并。窗参数变化经 [tdr.cpp:31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L31) 的信号连接同样触发重算。

**域声明**：[tdr.cpp:43-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L43-L50) 声明 Frequency→Time，其余输入一律 Invalid——这就是 u8-l5 讲过的域闸门。

**lowpass 分支**（核心算法在 [tdr.cpp:293-321](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L293-L321)）：

```cpp
if(firstStep * steps != inputData.back().x) {
    // data is not available with correct frequency spacing, calculate required steps
    steps = inputData.back().x / firstStep;
    stepSize = firstStep;
}
frequencyDomain.resize(2 * steps + 1);
// copy frequencies, use the flipped conjugate for negative part
for(unsigned int i = 1;i<=steps;i++) {
    auto S = TraceMath::interpolatedSample(inputData, stepSize * i);
    frequencyDomain[steps - i] = conj(S);
    frequencyDomain[steps + i] = S;
}
```

- [tdr.cpp:302-306](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L302-L306) 校验「频点从 \(f_1\) 出发等间隔直达终点」的谐波条件；不满足时改用 \( f_1 \) 作步长重新构造栅格，随后逐点用 `TraceMath::interpolatedSample`（复数线性插值，定义在 [tracemath.cpp:276-297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L276-L297)）从测量数据里取值。
- [tdr.cpp:309-313](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L309-L313) 做共轭对称延拓（对应公式 \(X[-k]=\overline{X[k]}\)）；若起点为 0 Hz 会跳过该点（[tdr.cpp:297-301](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L297-L301)，直流由下一段负责）。
- [tdr.cpp:314-321](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L314-L321) 是直流外推（自动）或手动 DC 值，正是 4.1.2 里的两个外推公式。

**bandpass 分支**（[tdr.cpp:322-329](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L322-L329)）：直接把测量值依次填入频谱数组，无延拓、无直流。

**加窗、补零与变换**（[tdr.cpp:331-343](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L331-L343)）：

```cpp
tdr.window.apply(frequencyDomain);          // ① 频域加窗
tdr.unpaddedInputSize = frequencyDomain.size();
if(tdr.padding > 0) {                        // ② 两端对称补零
    frequencyDomain.insert(frequencyDomain.begin(), tdr.padding/2, 0);
    frequencyDomain.insert(frequencyDomain.end(), tdr.padding/2, 0);
}
Fft::shift(frequencyDomain, true);           // ③ 直流移到下标 0
int fft_bins = frequencyDomain.size();
const double fs = 1.0 / (stepSize * fft_bins); // ④ Δt = 1/(M·Δf)
Fft::transform(frequencyDomain, true);       // ⑤ IFFT（任意长度）
Fft::shift(frequencyDomain, false);          // ⑥ t=0 移到中央
```

这段是本讲实践任务要找的「补零与窗处理」原点：①加窗压制旁瓣、②补零做时域插值。`unpaddedInputSize` 记下补零前的长度，供 DFT 节点日后剥掉（见 4.3）。

**写输出与缩放**（[tdr.cpp:345-352](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L345-L352)）：`x = fs * (i - fft_bins/2)` 把时间轴居中，`y = frequencyDomain[i] / fft_bins` 手动除以 \(M\)——因为 Nayuki 库的逆变换**不做缩放**（契约写在 [fftcomplex.h:34-38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h#L34-L38) 的注释里）。

**阶跃响应**：[tdr.cpp:353-357](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L353-L357) 只在 lowpass 模式且勾选了 step response 时置为有效；真正的积分在基类 [tracemath.cpp:299-312](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L299-L312)——对冲激响应的**实部做累加求和**，即离散积分。

**限速**：[tdr.cpp:360-365](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L360-L365) 按 Preferences 中 `Acquisition.limitDFT/maxDFTrate` 限制重算频率，避免活测量时 CPU 被后台线程吃满。

#### 4.1.4 代码实践

**实践：纸面手算一次 TDR 的三个关键量，再到源码逐行对号**

1. **实践目标**：不依赖 GUI，用 4.1.2 的公式算出时域采样间隔、无模糊范围与分辨率，并确认每个数对应 tdr.cpp 的哪一行。
2. **操作步骤**：
   - 假设一次 lowpass 扫描：起点 10 MHz、终点 1 GHz、101 点。先验证它是谐波栅格：步长 = (1 GHz−10 MHz)/100 = 9.99 MHz，**不是** 10 MHz——按 [tdr.cpp:302-306](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L302-L306) 的逻辑，代码会改用 \(f_1=10\) MHz 作步长、steps = 100，在栅格上插值取数。
   - 计算：\(M = 2\times100+1 = 201\)，\(\Delta t = 1/(201 \times 10\,\mathrm{MHz}) \approx 497.5\,\mathrm{ps}\)；无模糊范围 \(1/f_1 = 100\,\mathrm{ns}\)（时间轴从 −50 ns 到 +50 ns）；矩形窗分辨率（第一零点）\(1/(2 f_{\max}) = 1/(2\times1\,\mathrm{GHz}) = 500\,\mathrm{ps}\)。
   - 换算成物理距离：500 ps 的往返时间，在线上波速 \(v = 2\times10^8\,\mathrm{m/s}\) 的电缆上对应单程 \( d = v t/2 = 5\,\mathrm{cm}\)。
   - 到源码对号：\(\Delta t\) ↔ [tdr.cpp:340](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L340) 的 `fs`；时间轴居中 ↔ [tdr.cpp:348](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L348)；除以 \(M\) ↔ [tdr.cpp:349](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L349)。
3. **需要观察的现象**：手算值与公式逐项吻合；注意 101 点里只有 100 个「步」，起点本身占一格。
4. **预期结果**：若把起点改为 5 MHz、终点 1 GHz、201 点，重复计算应得 \(M=401\)、\(\Delta t\approx498.8\) ps、无模糊范围 200 ns、分辨率仍 500 ps——**起止范围决定分辨率，点数决定混叠范围**。
5. 本实践的公式部分可完全纸面验证；GUI 对照部分**待本地验证**（需编译好的 GUI，见综合实践）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 bandpass 模式给不出阶跃响应？

**答案**：阶跃响应是冲激响应的积分，其终值等于 \(S(0)\)（直流反射系数）。bandpass 模式的频谱只是一段不含直流的窄带、也未做共轭延拓，直流信息根本不存在，积分出的「阶跃」没有物理意义。代码在 [tdr.cpp:353-357](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L353-L357) 用 `mode == TDR::Mode::Lowpass` 显式挡掉了这种情况，编辑对话框也相应禁用相关控件（[tdr.cpp:79-88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L79-L88)）。

**练习 2**：把 `padding` 从 0 改成 200，时域波形会怎么变？分辨率会变吗？

**答案**：补零使 \(M\) 增大 200，\(\Delta t = 1/(M f_1)\) 相应变小，波形在时间方向上被**插值加密**、显得更平滑，峰的位置读数更细。但带宽 \(f_{\max}\) 未变，第一零点分辨率 \(1/(2f_{\max})\) **不变**——两个粘连的反射不会因为补零而分开。对应代码 [tdr.cpp:333-336](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L333-L336)。

**练习 3**：TDR 为什么必须放在后台线程算，而 Marker 插值（u8-l3）可以同步算？

**答案**：TDR 是 \(O(M\log M)\) 的全量变换，活测量时每个扫描点到达都可能触发重算，同步执行会阻塞 GUI 事件循环；Marker 插值只涉及相邻两个样本的线性运算，开销可忽略。TDR 用「信号量 + 最低优先级线程 + 合并积压通知 + 偏好限速」（[tdr.cpp:28-29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L28-L29)、[270-272](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L270-L272)、[360-365](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L360-L365)）四层机制兜底。

### 4.2 DFT 节点、窗函数与 FFT 引擎

#### 4.2.1 概念说明

本模块讲三件互相配套的事：

1. **WindowFunction 组件**：一个可复用的「窗函数值对象」，不是 TraceMath 节点。TDR、DFT、TimeGate 各内嵌一份，编辑对话框里的「窗类型下拉框」就是它生成的。它提供 `apply`（乘窗）与 `reverse`（除窗）——后者是 DFT 节点「撤销 TDR 的窗」的关键。
2. **DFT 节点**（dft.cpp）：TDR 的镜像，Time→Frequency。单独用它的场景不多，它的主要使命是**接在时间门后面，把门控后的时域数据送回频域**（见 4.3）。为此它必须「原路退回」：剥掉 TDR 补的零、除掉 TDR 乘的窗。
3. **Fft 引擎**（fftcomplex.cpp）：全部变换的地基，来自 Project Nayuki 的开源实现。理解它的两条路径与「逆变换不缩放」契约，才能看懂上层为什么到处 `/fft_bins`。

#### 4.2.2 核心流程

**窗函数族**（公式见 4.2.3，均为周期窗形式，自变量 \(n \in [0,N)\)）。经典特性对照（数值为窗函数理论标准值，代码注释亦注明公式出处为维基百科窗函数条目）：

| 窗 | 主瓣宽度（第一零点，相对矩形） | 典型旁瓣电平 | TDR 里的体感 |
|---|---|---|---|
| Rectangular（矩形） | 1× | −13 dB | 最窄的峰，但振铃严重 |
| Hamming（默认） | ≈2× | −43 dB | 折中，代码默认值 |
| Hann | 2× | −31 dB | 振铃明显减小、峰略胖 |
| Blackman | 3× | −58 dB | 最干净，峰最胖 |
| Gaussian(σ) | 随 σ 变化 | 无旁瓣结构（高斯变换仍是高斯） | σ 小 → 逼近矩形 |

**DFT 节点流程**：

```text
输入时域数据 (≥2 点，来自 TDR 或 TimeGate)
   │
   ├─ 沿输入链回溯查找 TDR 节点（拿它的窗、补零长度、模式）
   ├─ 自动定直流频率：lowpass→0；bandpass→取原频带中点
   ├─ 拷贝 y 值 → 时域加窗（本节点自己的窗）
   ├─ Fft::shift(true) → 正 FFT → Fft::shift(false)
   ├─ 若上游有 TDR：
   │    ├─ 按 unpaddedInputSize 拆出 前置零段 | 数据段 | 后置零段
   │    ├─ 可选：reverse 撤销 TDR 的窗（只作用于数据段）
   │    └─ 可选：丢弃补零段
   └─ 写输出：f = (i − M/2)·binSpacing + DC；DC=0 时只输出正频率半轴
```

其中 \(\text{binSpacing} = \frac{1}{\Delta t \cdot M}\)——正是 TDR 里 \(\Delta t\) 公式的对偶。

**FFT 引擎分派**：

\[
\text{transform}(\vec{v}) = \begin{cases} \text{radix-2 Cooley-Tukey}, & |v| = 2^k \\ \text{Bluestein（chirp-z）}, & \text{其他长度} \end{cases}
\]

这一点对上层至关重要：TDR lowpass 模式的频谱长度是 \(2N+1\)（**奇数**），永远不是 2 的幂，所以 GUI 侧的 TDR 实际上总是走 Bluestein 路径（代价是多次 radix-2 变换实现的卷积）。

#### 4.2.3 源码精读

**WindowFunction**：

- 类型枚举与默认值：[windowfunction.h:14-24](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.h#L14-L24) 定义 `Rectangular/Gaussian/Hann/Hamming/Blackman`，构造默认 **Hamming**（[windowfunction.h:27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.h#L27)）——所以新建 TDR 不改设置时用的是 Hamming。Kaiser 与 Chebyshev 被注释掉（**FPGA 侧有 Kaiser，GUI 侧没有**，两处实现并不同步）。
- 乘窗与除窗：[windowfunction.cpp:35-49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.cpp#L35-L49)，`apply` 逐点乘 `getFactor(n,N)`，`reverse` 逐点除——除法是 DFT 节点撤销 TDR 窗的唯一手段。
- 窗公式：[windowfunction.cpp:161-195](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.cpp#L161-L195) 的 `getFactor`：

```cpp
case Type::Hamming:  return 25.0/46.0 - (21.0/46.0) * cos(2*M_PI*n / N);
case Type::Hann:     return pow(sin(M_PI*n / N), 2.0);
case Type::Blackman: return 0.42 - 0.5*cos(2*M_PI*n/N) + 0.08*cos(4*M_PI*n/N);
case Type::Gaussian: return exp(-0.5 * pow((n - (double)N/2) / (gaussian_sigma*N/2), 2));
```

四个公式分别对应 4.2.2 表中的四种窗；Gaussian 以序列中央为峰、σ 控制肩宽。编辑 UI（下拉框 + σ 参数框）在 [windowfunction.cpp:51-104](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.cpp#L51-L104)，`changed()` 信号让宿主节点自动重算。

**DFT 节点**：

- 域声明 Time→Frequency：[dft.cpp:40-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L40-L47)。与 TDR 一样拥有专用线程与信号量（[dft.cpp:24-28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L24-L28)），线程主循环 [dft.cpp:182-294](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L182-L294) 与 TDR 结构几乎逐行同构——读通一个就读通了两个。
- 回溯查找 TDR：[dft.cpp:208-220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L208-L220) 沿输入链找 `Type::TDR` 节点，找到后取它的窗与模式。**源码阅读发现**：循环体内写的是 `in = dft.input->getInput()` 而非 `in = in->getInput()`，每次赋的其实是同一个值——在出厂链条 DFT←TimeGate←TDR（恰好隔一层）下它能正确终止，但若在 TDR 与门之间再插一个别的节点，这个循环将原地踏步无法前进。读代码时留意这类「只对当前用法成立」的写法，二次开发加节点时这里是隐患。
- 自动直流：[dft.cpp:222-229](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L222-L229)，lowpass 取 0，bandpass 取 TDR 输入数据的中间频点。
- 时域加窗与正变换：[dft.cpp:237-241](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L237-L241)——注意这次窗乘在**时域**（控制的是「截取多长的时间记录去做 FFT」的泄漏），与 TDR 的频域加窗方向相反。
- 剥零与撤窗：[dft.cpp:245-267](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L245-L267)，按 `getUnpaddedInputSize()` 把结果拆成前置零/数据/后置零三段，`revertWindowFromTDR` 为真时对数据段调用 `tdr->getWindow().reverse(data)`，`removePaddingFromTDR` 为真时丢弃两个零段。这两个开关对应编辑对话框里的复选框（[dft.cpp:72-78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L72-L78)）。
- 频率轴重建：[dft.cpp:269-282](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L269-L282)。`DC > 0`（bandpass）输出全轴；`DC == 0`（lowpass）只输出正频率半轴——这样恰好还原出原始测量栅格 \(f_1 \ldots f_{\max}\)。

**Fft 引擎**：

- 版权与来源：[fftcomplex.cpp:1-22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp#L1-L22)（Project Nayuki，MIT 许可）——仓库「内置源码、不引第三方包」策略（u1-l3）的又一例证。
- 分派：[fftcomplex.cpp:43-51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp#L43-L51)，`(n & (n-1)) == 0` 判 2 的幂。
- radix-2：[fftcomplex.cpp:54-89](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp#L54-L89)，教科书式 Cooley-Tukey：旋转因子表 → 位反转重排 → 蝶形迭代。
- Bluestein：[fftcomplex.cpp:92-127](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp#L92-L127)，把任意长度 DFT 改写成卷积，再补零到 \(m \ge 2n+1\) 的 2 的幂用 radix-2 求解——TDR 的奇数长度全靠它。
- 契约「逆变换不缩放」：[fftcomplex.h:34-38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h#L34-L38) 注释明说 "The inverse transform does not perform scaling"；`convolve` 内部因此自己除以 \(n\)（[fftcomplex.cpp:145-146](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp#L145-L146)），上层 TDR/DFT 也各自手动除。
- `shift`：[fftcomplex.cpp:157-167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp#L157-L167)，等价 MATLAB 的 `fftshift/ifftshift`（奇数长度时两者旋转量差一），TDR/DFT/TimeGate 全靠它在「直流居中」与「直流在下标 0」两种排列间搬运。

#### 4.2.4 代码实践

**实践：用单元测试验证 FFT 引擎的两条路径与缩放契约**

1. **实践目标**：亲手验证 `Fft::transform` 对**非 2 的幂长度**也能工作（即 Bluestein 路径），并理解「逆变换后必须除以 N」的契约。
2. **操作步骤**：
   - 阅读测试 [ffttests.cpp:24-30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L24-L30)：输入是 5 个数 `{1,2,3,4,5}`——5 不是 2 的幂，所以这个测试**实际覆盖的是 Bluestein 分支**。先手算第一个期望值：DFT 的 0 号桶是所有样本之和 \(1+2+3+4+5=15\)，与 `expectedResult` 的第一个元素吻合。
   - 再看 [ffttests.cpp:32-42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-Test/ffttests.cpp#L32-L42) 的 `fftAndIfft`：正变换后立刻逆变换，然后**逐点除以 `data.size()`** 才与原数据比较——这条 `/size` 正是缩放契约的体现，与 tdr.cpp:349 的 `/fft_bins` 一脉相承。
   - 可选运行：按 u1-l3 同样的 qmake6/make 流程构建测试工程 `Software/PC_Application/LibreVNA-Test/LibreVNA-Test.pro` 并执行；若不便构建，用 Python 做等价验证：`import numpy as np; np.fft.fft([1,2,3,4,5])`，与测试中的期望值逐位对比。
   - 对照思考：TDR lowpass 的频谱长度 \(2N+1\) 是奇数，说明 GUI 的每次 TDR 都在走这个被测试覆盖的 Bluestein 路径。
3. **需要观察的现象**：手算的 15 与测试期望一致；numpy 结果与 `expectedResult` 各元素一致（数值精度内）。
4. **预期结果**：确认任意长度 FFT 正确性由 Bluestein 保证；确认忘写 `/size` 会让幅值放大 N 倍——这正是库把缩放责任交给调用方的原因与代价。
5. GUI 与测试工程运行部分**待本地验证**；numpy 对照部分可立即完成。

#### 4.2.5 小练习与答案

**练习 1**：DFT 节点的窗乘在时域、TDR 节点的窗乘在频域，两者目的有何不同？

**答案**：TDR 的频域加窗压制「带宽截断」引入的 sinc 旁瓣（时域振铃），改善时域可读性；DFT 的时域加窗压制「时间记录截断」引入的频谱泄漏（对有限时长信号做 FFT 时的能量外溢），改善回频域后的谱形。二者方向相反、目的同构：**在哪个域截断，就在哪个域加窗**。

**练习 2**：为什么 `WindowFunction` 要提供 `reverse`（除窗）？直接不加窗不就行了？

**答案**：因为链上的窗是**分两级**加的：TDR 在频域乘了一次窗，数据经门控回频域后，若不撤销，最终结果就是「测量数据 × TDR 的窗」，形状被污染。`reverse` 逐点除回窗因子（[windowfunction.cpp:43-49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.cpp#L43-L49)），由 DFT 节点在 [dft.cpp:255-257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L255-L257) 调用，且只作用于真实数据段（补零段无窗可除）。「加窗可逆、截断不可逆」——除窗能成立是因为窗因子处处非零，而矩形截断丢掉的信息无法恢复。

**练习 3**：若把 `Fft::transform` 的长度判断去掉、统一走 radix-2，会发生什么？

**答案**：radix-2 要求长度为 2 的幂（否则 [fftcomplex.cpp:60-61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.cpp#L60-L61) 抛 `domain_error`），而 TDR lowpass 的 \(2N+1\) 是奇数，程序会直接异常。要么强制把所有长度补到 2 的幂（改变栅格语义），要么保留 Bluestein——仓库选择后者，用通用性换计算量。

### 4.3 时间门：时域截断再回频域

#### 4.3.1 概念说明

时间门（Time Gate）解决一个典型问题：**被测件的连接器/夹具反射与被测件本身的响应在频域里搅在一起，形成纹波；但在时域里它们位于不同的时间位置，可以按位置切除**。

做法是一个往返：

\[
\underbrace{S(f)}_{\text{频域}} \xrightarrow{\ \text{TDR}\ } h(t) \xrightarrow{\ \times g(t)\ } h'(t) \xrightarrow{\ \text{DFT}\ } S'(f)
\]

其中 \(g(t)\) 是门函数。由卷积定理，时域相乘对应频域卷积：

\[
S'(f) = S(f) * G(f)
\]

这个对偶直接给出设计约束：

- 门越**窄**（切除越干净），\(G(f)\) 越**宽**——回频域后频谱被涂抹得越厉害，门内信息失真越大；
- 理想矩形门的 \(G(f)\) 是 sinc，拖尾衰减慢，会把纹波「抹」到整个频带；
- 把门沿做**软**（渐变过渡），\(G(f)\) 主瓣集中、拖尾衰减快——这正是窗函数知识的第二次登场。

仓库把这三步打包成一个复合运算 **TimeDomainGating**（时域门控），用户点一下菜单即可完成频→时→门→频的全程，见 [tracemath.cpp:39-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L39-L43)：

```cpp
case Type::TimeDomainGating:
    ret.push_back(new Math::TDR());
    ret.push_back(new Math::TimeGate());
    ret.push_back(new Math::DFT());
    break;
```

这正是 u8-l5 讲过「工厂返回 vector 以支持复合运算」的实例。

#### 4.3.2 核心流程

TimeGate 本体是 Time→Time 的逐点乘法：`data[i].y *= filter[i]`。全部巧妙之处在 `filter[]` 数组怎么造——用的是数字滤波器课上的**窗设计法**（window design method），只是把「理想频响的无限冲激响应」换成了「理想时域门的共轭域核」：

```text
updateFilter():
  1. 把门的 [start, stop]（秒）归一化到数据时域范围的 [0,1] → wc1, wc2
  2. 写出理想带通核（sinc 差）：
        h[n] = (sin(π·wc2·n) − sin(π·wc1·n)) / (π·n),  n 关于缓冲区中央对称
     陷波(notch)版 = 1 − 带通版
  3. 乘窗（软化门沿，压制 sinc 拖尾）
  4. Fft::shift(true) + 正 FFT —— 把核搬到数据轴上
  5. filter[i] = |结果前半| —— 一条平滑的 0/1 门曲线，下标与数据样本一一对应
  6. 通知重算：对每个数据样本 y[i] ×= filter[i]
```

理解第 4 步的诀窍：第 2 步写下的 sinc 差核，其实是「理想矩形门」在共轭域的解析表达；对它做一次 FFT，就得到了数据轴上真正的门形状。归一化 `Scale(center±span/2, minX, maxX, 0, 1)` 保证了 sinc 核的通带位置 \(w_{c1},w_{c2}\) 精确落在期望的秒数位置上。

门参数与模式：

- **bandpass**：门内保留、门外清零（看某个反射）；
- **notch**：门内清零、门外保留（切除某个干扰反射，正好是带通的「1 减」）；
- center/span 或 start/stop 两种等价表述，编辑图上可直接拖拽门沿。

#### 4.3.3 源码精读

**逐点乘法**：[timegate.cpp:219-243](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L219-L243)。输入样本数变化时重造 filter；否则只做 `data[i] = inputData[i]; data[i].y *= filter[i];`——TimeGate 是三个节点中唯一**没有后台线程**的，因为乘法开销可忽略。

**造门**：[timegate.cpp:245-292](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L245-L292)，对应上面流程的 1–6：

```cpp
auto wc1 = Util::Scale<double>(center - span / 2, minX, maxX, 0, 1);   // 步骤 1
auto wc2 = Util::Scale<double>(center + span / 2, minX, maxX, 0, 1);
for(unsigned int i=0;i<buf.size();i++) {                               // 步骤 2
    int n = i - buf.size() / 2;
    if(n == 0) {
        buf[i] = wc2 - wc1;
    } else {
        buf[i] = (sin(M_PI * wc2 * n) - sin(M_PI * wc1 * n)) / (n * M_PI);
    }
    if(!bandpass) {                                                    // 陷波 = 1 − 带通
        if(n == 0) { buf[i] = 1.0 - buf[i]; }
        else      { buf[i] = sin(M_PI * n) / M_PI - buf[i]; }
    }
}
window.apply(buf);                                                     // 步骤 3
Fft::shift(buf, true);
Fft::transform(buf, false);                                            // 步骤 4
filter.resize(buf.size() / 2);
for(unsigned int i=0;i<buf.size() / 2;i++) {
    filter[i] = abs(buf[i]);                                           // 步骤 5
}
```

细节值得咀嚼：

- `buf` 长度是数据长度的 **2 倍**（[timegate.cpp:253](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L253)），给门形留出过渡带空间，最后只取前半。
- 陷波分支里的 `sin(M_PI * n) / M_PI` 在整数 \(n\ne0\) 时恒为 0，写出来是为了表达「全通 − 带通 = 陷波」的对称形式；真正的意义在 \(n=0\) 项：\(1 - (w_{c2}-w_{c1})\)。
- `abs()` 取模是因为门形状本应是实数，数值误差会留下微小虚部。
- 造完门立刻「假装输入变了」触发一次全量重算（[timegate.cpp:291](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L291)），保证拖动门沿时输出实时刷新。

**交互编辑图**：[timegate.cpp:294-493](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L294-L493) 的 `TimeGateGraph`：绿色画时域迹线、红色画门形（dB 刻度，[timegate.cpp:416-438](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L416-L438)），鼠标按靠近原则抓取门沿拖动（[timegate.cpp:441-493](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L441-L493)），拖动经 `setStart/setStop`（[timegate.cpp:149-189](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L149-L189)）钳位到数据范围后重造门。`setStart` 中 `start < inputData.front().x` 时被钳到 `back().x` 一行（[timegate.cpp:155-157](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L155-L157)）读起来可疑（钳到最小值却赋了最大值），实际由后续 `if(start < stop)` 守卫兜住非法区间——又一处「读代码时值得停一秒」的地方。

**回程**：门控后的数据由链尾的 DFT 节点送回频域，其剥零/撤窗/频率轴重建逻辑已在 4.2.3 精读；`getUnpaddedInputSize()` 的值正是 TDR 在 [tdr.cpp:332](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L332) 记下的补零前长度——两个节点隔着 TimeGate 遥相呼应。

#### 4.3.4 代码实践

**实践：在示例测量上完成一次完整门控（无硬件）**

1. **实践目标**：体验频→时→门→频的完整回路，观察「切除一个时域反射」对频域迹线纹波的影响。
2. **操作步骤**：
   - 启动 GUI（构建方式见 u1-l3），菜单导入 Touchstone 测量文件，例如仓库自带的 `Documentation/Measurements/Prototype_Isolation_SOLT.s2p`（该目录下有多份示例测量，u1-l3 已验证可用）。
   - 在 Trace 列表中为 S11 创建 XY 图（X 轴频率）。
   - 给该 Trace 添加数学运算，选 **Time Domain Gating**（这一个操作等于同时挂上 TDR + TimeGate + DFT 三级，对应 [tracemath.cpp:39-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L39-L43)）。
   - 打开 TimeGate 的编辑对话框：绿色是时域冲激响应，红色是门形。把门拖到最靠近 t=0 的主反射峰上（bandpass 模式），确认应用。
3. **需要观察的现象**：
   - 对比门控前后的频域 S11：被门掉的反射造成的快速纹波应当显著减弱，曲线变光滑；
   - 把 span 拖得极窄：纹波会再次恶化——这就是 4.3.1 说的「门窄 → \(G(f)\) 宽 → 频域涂抹」；
   - 切换成 notch 模式：效果反转，只剩被切反射的贡献。
4. **预期结果**：门沿软硬（改 TimeGate 自己的窗类型：Rectangular vs Blackman）对过渡区纹波的影响方向，与 4.2.2 窗表中「旁瓣低则干净」一致。
5. 本实践依赖本地编译的 GUI，**待本地验证**；若无 GUI，可做纯源码替代：在 [timegate.cpp:260-287](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/timegate.cpp#L260-L287) 旁用 Python 复刻 sinc 差核 + Hann 窗 + FFT，画出门形曲线验证其中心与宽度由 wc1/wc2 决定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TimeGate 不需要后台线程，而它两边的 TDR 和 DFT 都需要？

**答案**：TimeGate 每次只做 N 次复数乘法（`y[i] *= filter[i]`），且 filter 只在门参数或样本数变化时才重算；TDR/DFT 每次都是全量 FFT（\(O(N\log N)\)）且活测量时随扫描频繁触发。开销差了数量级，线程化的收益不足以抵消复杂度。

**练习 2**：门控后回频域的结果，与「理想地把那个反射从测量中消去」完全等价吗？

**答案**：不等价。门控是时域相乘 = 频域与 \(G(f)\) 卷积，任何有限宽度的门都会让 \(G(f)\) 非理想（主瓣有限宽 + 旁瓣非零），因此门控会轻微改变门内目标的频响（幅度通常有百分之几量级的偏差，取决于门宽与窗）。门越宽、沿越软，失真越小但选择性越差——这是原理性权衡，不是实现缺陷。

**练习 3**：如果用户先手动挂了 TDR，再挂 TimeGate，再挂 DFT（而不是用 TimeDomainGating 一步到位），结果一样吗？

**答案**：数学结果一样（链条同构），但要注意两点：一是 DFT 查找上游 TDR 的循环（[dft.cpp:212-217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/dft.cpp#L212-L217)）只检查「隔一层」的前驱，链条再插入其他节点时它找不到 TDR，剥零/撤窗/自动直流都会失效；二是手动链的默认参数需要逐个设置，复合运算则由 [tracemath.cpp:39-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L39-L43) 保证了固定顺序。推荐使用复合入口。

## 5. 综合实践

**任务：导入示例测量 → S11 施加 TDR → 变窗观察 → 回源码解释**

这是本讲规格指定的核心实践，把三个模块串成一条线。

1. **准备**：按 u1-l3 构建 LibreVNA-GUI；无需硬件。准备示例测量 `Documentation/Measurements/Prototype_Isolation_SOLT.s2p`（或同目录任意 .s2p）。
2. **第一步（导入与建迹线）**：GUI 导入该 s2p，为 S11 建 Trace 并放到一个 XY 图上（频率轴）。
3. **第二步（施加 TDR）**：给 S11 添加数学运算 **TDR**。打开编辑对话框，确认模式为 lowpass；此时再给这条 TDR 迹线建一个 XY 图（X 轴应为时间）。若导入数据的频点不满足谐波栅格，对照 [tdr.cpp:302-306](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L302-L306) 想一想代码正在替你做插值重采样。
4. **第三步（变窗观察）**：在 TDR 对话框的窗下拉框里依次切换 Rectangular → Hamming（默认）→ Hann → Blackman，盯着时域图上的主反射峰，记录三件事：
   - 峰的**宽度**（半高宽）如何变宽；
   - 峰两侧的**振铃/台阶**如何减弱；
   - 小反射（若有）在 Rectangular 下是否被旁瓣淹没、在 Blackman 下是否显现。
   对照 4.2.2 的窗表，把观察到的趋势填进自己的表格。
5. **第四步（补零实验）**：把 padding 从 0 逐步加大（如 100、400），观察波形变平滑但峰**不分开**——验证 4.1.5 练习 2 的结论。
6. **第五步（回源码解释）**：打开 [tdr.cpp:331-343](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L331-L343)，用自己的话写 200 字：第三步看到的一切差异来自 `window.apply` 这一行乘上的不同 `getFactor`（[windowfunction.cpp:161-195](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/windowfunction.cpp#L161-L195)），第四步看到的平滑来自 `insert` 补零使 `fft_bins` 变大、`fs` 变小（[tdr.cpp:333-340](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tdr.cpp#L333-L340)）。
7. **产出**：一份观察记录表 + 一段源码解释。若最后有余力，把 TimeGate 也挂上（4.3.4），完成从频域纹波到时域定位再到门控净化的完整闭环。
8. **说明**：本实践所有 GUI 步骤**待本地验证**（依赖本机编译运行环境）；源码解释部分现在即可完成。

## 6. 本讲小结

- **TDR = 算出来的时域反射计**：对满足谐波栅格的频域 S 参数做共轭延拓（bandpass 则直接变换）+ IFFT，得到冲激响应；\(\Delta t = 1/(M\Delta f)\)、无模糊范围 \(1/\Delta f\)、分辨率 \(1/(2f_{\max})\)，阶跃响应由冲激响应实部累加积分而来，其终值取决于直流外推质量。
- **加窗与补零是两件不同的事**：窗乘在频域压制时域振铃（代价：主瓣变宽、分辨率下降）；补零只增大 \(M\) 做时域插值，不提升分辨率。TDR 的窗在 DFT 节点里可被 `reverse` 除回，补的零可被剥掉。
- **窗公式集中在一个可复用组件** `WindowFunction`（默认 Hamming；GUI 侧无 Kaiser），TDR/DFT/TimeGate 各持一份，`apply`/`reverse` 成对提供。
- **时间门是频→时→门→频的往返**：TimeGate 用「理想 sinc 核 + 加窗 + FFT 搬移」造出平滑门形做时域逐点乘法；门窄则频域涂抹重、沿软则拖尾小；仓库用 `TimeDomainGating` 复合运算把三级打包。
- **FFT 地基是 Nayuki 库**：2 的幂走 radix-2、任意长度走 Bluestein（TDR 的奇数长度全靠它）；「逆变换不缩放」的契约解释了上层到处出现的 `/fft_bins`。
- **工程细节**：TDR/DFT 用「专用低优先级线程 + 信号量合并 + 偏好限速」避免阻塞 GUI；DFT 回溯找 TDR 的循环只对「隔一层」的出厂链条成立，二次开发时是隐患。

## 7. 下一步学习建议

- **单元 9（校准与去嵌入）**：时间门是「软件修正测量」的一种手段，但它不能替代 SOLT 校准；学完 u9 后回头对比「先校准再门控」与「只门控」的差异，理解两者的职责边界（门控切反射，校准修误差）。
- **u8-l2（绘图体系）**：本讲的时域迹线要落到时间轴 XY 图上展示，可回看 `TracePlot` 与轴类型闸门如何配合 Time 域数据。
- **对照 FPGA 侧（u6-l4）**：FPGA 的 `Windowing.vhd/DFT.vhd` 是同一数学在硬件上的实时实现（有 Kaiser、定点相位累加器），对照 GUI 侧浮点实现，体会「同一算法、两种实现空间」的取舍。
- **源码延伸**：`Traces/Math/expression.cpp`（自定义表达式运算）与 `Traces/Math/medianfilter.cpp`（u8-l5 的注册示例）是数学链家族的其余成员；`LibreVNA-Test/ffttests.cpp` 与 `calibrationtests.cpp`（u11-l1 会精读）展示了如何为数值代码写「已知答案」的测试。
