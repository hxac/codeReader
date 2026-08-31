# 去嵌入实战：端口延伸、阻抗与 2x-Thru

## 1. 本讲目标

上一讲（u9-l4）我们看清了去嵌入框架的骨架：`DeembeddingOption` 三个纯虚函数构成契约，`Deembedding::Deembed()` 沿选项列表逐个调用 `transformDatapoint()`，对「已校准」的数据就地修正。本讲下钻到四个内置选项的**数学与实现**，学完后你应当能够：

1. 推导端口延伸的时延–相位旋转公式，解释 √f 损耗模型，并读懂 PortExtension 用开路/短路自动测量时延与损耗的最小二乘算法。
2. 写出阻抗再归一化的矩阵变换（S→Z→S′），并解释匹配网络如何用 ABCD 级联参数把一段集总元件网络「加上」或「减去」。
3. 描述 2x-Thru 智能去嵌入需要哪些测量输入，以及它在时间域「剥洋葱」求出两侧夹具误差盒的全过程。
4. 在完全没有硬件的条件下，用一份手工构造的 Touchstone 文件验证端口延伸公式的正确性。

## 2. 前置知识

本讲是手册中数学密度最高的一讲，先把四个工具性概念补齐。

**① 信号通过一段传输线会发生什么。** 一段无损、特性阻抗与系统一致、单向时延为 \(\tau\) 的传输线，对频率为 \(f\) 的信号只做一件事：乘上相位因子 \(e^{-j2\pi f\tau}\)（时域延迟 \(\tau\) 对应频域乘 \(e^{-j\omega\tau}\)）。若线有损耗，损耗随频率按 \(\sqrt{f}\) 增长（趋肤效应），用 dB 表示就是 \(\text{att}(f) = a_{DC} + a_1\sqrt{f/f_{ref}}\)。反射参数（如 S11）走一个来回，相位与损耗都要**加倍**。

**② 四种双端口矩阵。** 同一个双端口网络可以用多种参数描述，本讲全部用到：

| 参数 | 直觉 | 关键性质 |
|---|---|---|
| S 参数 | 归一化波之比，VNA 直接测它 | 级联不便 |
| Z 参数 | 端口电压/电流 | 阻抗再归一化的中转站 |
| ABCD 参数 | 信号流左侧到右侧的传输矩阵 | **级联 = 矩阵相乘** |
| T 参数 | 行波级联参数 | **级联 = 矩阵相乘**，适合夹具剥离 |

ABCD 级联的直觉：网络 A 后接网络 B，则整体 \(ABCD_{AB} = ABCD_A \cdot ABCD_B\)（矩阵乘法注意顺序）；想从测量中「减去」A，右乘 \(ABCD_A^{-1}\) 即可。T 参数同理。LibreVNA 把这些换算全部实现在 `Tools/parameters.cpp` 中（u11-l2 会再见到它）。

**③ 多重反射的几何级数。** 信号进入夹具，夹具输入端反射系数 \(\Gamma_f\)，DUT 反射系数 \(\Gamma_L\)，则从夹具输入端看进去的总反射是无穷级数 \(\Gamma_f + \Gamma_f\Gamma_L\Gamma_f + \cdots\)，闭合形式：

\[
\Gamma_{in} = \frac{\Gamma_f}{1-\Gamma_f\Gamma_L}
\]

本讲会在匹配网络源码里原样见到它。

**④ 夹具去嵌入的两种流派。** 测试夹具（一段走线、一个转接器）夹在仪器端口与 DUT 之间。修掉它的办法从简单到复杂排序：

- **端口延伸**：假设夹具就是一段均匀无损（或简单有损）传输线，手动或半自动给出时延。零测量成本，但完全不考虑夹具的失配反射。
- **匹配网络**：已知夹具是一个集总电路（如一个串联电感），直接按电路模型扣除。
- **2x-Thru**：拿一个「两段夹具直连」的标准件测一次，用算法把夹具的完整双端口行为（含反射与失配）解出来再扣除。这是工业界 IEEE P370 标准的做法。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [VNA/Deembedding/portextension.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp) | 端口延伸：相位/损耗修正 + 开短路自动测量 |
| [VNA/Deembedding/impedancerenormalization.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/impedancerenormalization.cpp) | 阻抗再归一化：S→Z→S′ 三行核心 |
| [VNA/Deembedding/matchingnetwork.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp) | 匹配网络：ABCD 级联 + 拖拽式电路编辑器（含 MatchingComponent） |
| [VNA/Deembedding/twothru.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp) | 2x-Thru：IEEE P370 风格的时间域剥离算法（700+ 行，本讲最难） |
| [VNA/Deembedding/deembedding.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp) | 选项容器与数据管线（u9-l4 已精读，本讲只引用调用点） |
| [Tools/parameters.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp) | S/Z/Y/ABCD/T 互换公式，本讲所有数学的落点 |
| [VNA/Deembedding/manualdeembeddingdialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/manualdeembeddingdialog.cpp) | 「对已导入迹线手动去嵌入」的入口（无硬件实践的通道） |
| [Traces/trace.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp) | `assembleDatapoints()` 把一组 Trace 重组为测量点 |
| [Traces/fftcomplex.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h) | 2x-Thru 依赖的 FFT 封装（u8-l6 已学） |

四个选项都注册在 `DeembeddingOption::create()` 工厂里（u9-l4），在 GUI 的 VNA 菜单 → De-embedding → Setup… 对话框中可任意添加、排序、叠加——它们只是 `transformDatapoint()` 的接力者。

## 4. 核心概念与源码讲解

### 4.1 端口延伸（PortExtension）

#### 4.1.1 概念说明

端口延伸是最便宜的去嵌入：把「夹具」简化为一段均匀传输线，只需两个标量参数——单向时延 `delay`（秒）和损耗模型（`DCloss` dB + `loss` dB @ `frequency` 参考频率）。它不建模夹具的阻抗失配，所以夹具反射很小（比如一段连续的 50Ω 走线）时它已经够用；夹具有明显失配时需要 4.3 的 2x-Thru。

PortExtension 还提供一个杀手锏：**自动测量**。在校准面接一个开路或短路标准件，扫一次频，它从反射系数的相位斜率解出时延、从幅度的 √f 特性回归出两个损耗系数——这正是传统商用 VNA「port extension auto」功能的实现方式。

#### 4.1.2 核心流程

对每个测量点 `d`（频率 `d.frequency`）：

```
φ  = -2π · delay · f                    ← 相位因子（负号 = 除掉一个延迟）
dB = DCloss + loss·sqrt(f/f_ref)        ← √f 损耗模型（f_ref≠0 时才加第二项）
att = 10^(-dB/20)                        ← dB 转线性幅度
correction = att · e^{jφ}                ← 一个复数
对每个 S 参数 S_ij：
    若 i == port（该端口是目的端口）：S_ij /= correction
    若 j == port（该端口是源端口）  ：S_ij /= correction
    （两个 if 独立判断：S_pp 两条都命中，除以 correction²）
```

注意最后一点：S 参数命名 `S<目的><源>`（与固件、驱动层一致），于是传输参数 S21（源=1）对端口 1 的延伸只除一次，而反射参数 S11 两条 if 都命中、除以 `correction²`——这正好对应「反射走一个来回、相位和损耗都加倍」的物理事实。代码用两个独立 `if` 天然实现了这一点，没有任何特判。

自动测量（开路/短路触发）的数据处理：

```
对整条扫描的每个点取 S<port><port>（非理想校准件先除以其理论反射系数）
→ 逐点求相位差并解卷绕（±π 折叠），平均得到每步相位增量 Δφ
→ delay = -Δφ / (2π·Δf) ，再除以 2（测的是来回，参数要单向）
→ 对 (x, y) = (sqrt(f/f_max), 20log10|Γ|) 做线性回归 y = α + βx
→ DCloss = -α/2, loss = -β/2（同样是来回 → 单向除以 2），f_ref = f_max
```

#### 4.1.3 源码精读

构造函数设默认值（velocityFactor 0.66 即同轴电缆典型值），并把五个参数全部注册为 SCPI 可控参数：

- [portextension.cpp:13-34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L13-L34) —— `DeembeddingOption("PORTEXTension")` 是它的 SCPI 短名；`addUnsignedIntParameter/addDoubleParameter` 把 PORT/DELAY/DCLOSS/LOSS/FREQuency 挂进命令树（SCPI 自动化时可直接 `DEEMBedding:1:DELAY 1e-10` 这样设置，见 u10 单元）。

核心修正只有 20 行：

- [portextension.cpp:41-61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L41-L61) —— `transformDatapoint()`：L43 算相位，L44-47 算 √f 损耗 dB，L49 转线性，L50 用 `polar(att, phase)` 合成一个复数，L51-60 按命名规则除到命中的 S 参数上。两个 `if` 不是 `else if`，反射参数因此被除两次。

```cpp
auto phase = -2 * M_PI * ext.delay * d.frequency;      // L43
auto db_attennuation = ext.DCloss;
if(ext.frequency != 0) {
    db_attennuation += ext.loss * sqrt(d.frequency / ext.frequency);  // √f 模型
}
auto att = pow(10.0, -db_attennuation / 20.0);
auto correction = polar<double>(att, phase);
```

编辑对话框把「时间 / 距离 / 速度因子」三个输入联动起来（物理关系 `距离 = 时延 × 速度因子 × 光速`，`c = 299792458`）：

- [portextension.cpp:63-119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L63-L119) —— L76-89 设置单位与初值；L105-116 三个 `SIUnitEdit` 互相推算，改任何一个另外两个跟着走；L123-132 的 Open/Short 按钮记下标准件类型后调 `startMeasurement()` 发出 `triggerMeasurement` 信号（由 u9-l4 讲过的 Deembedding 容器接管，弹出测量对话框、暂停常规扫描收集一整条扫描）。

自动测量回调是本模块的算法精华：

- [portextension.cpp:141-212](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L141-L212) —— L150 拼出反射参数名 `"S"+port+port`；L153-163 若非理想标准件，用 `CalStandard::OnePort::toS11()`（u9-l1 的校准件模型）除掉标准件自身的理论反射；L165-177 逐点相位差并做 ±π 解卷绕后求和；L186-190 求平均相位增量、换算成时延再除以 2（注释明言「measured delay is two-way but port extension expects one-way」）；L192-205 对 \((x,y)=(\sqrt{f/f_{max}}, \text{dB})\) 做最小二乘线性回归，斜率 β、截距 α，最后 `DCloss = -α/2, loss = -β/2`（L204-205，同样是来回除以 2），参考频率取扫描终点（L206）。

JSON 持久化兼容旧版数组格式：

- [portextension.cpp:224-252](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L224-L252) —— 新格式带 `port` 字段；旧格式整个 JSON 是数组、端口固定为 1。这是 Savable 体系向后兼容的惯例写法。

#### 4.1.4 代码实践

**实践目标**：验证端口延伸的相位公式——手算 100 ps 在 1 GHz 处的旋转角，与 GUI 实际结果对比。

1. 准备一条导入的 S21 迹线（无硬件路径）：用文本编辑器创建 `delay100ps.s2p`，内容为一条 100 ps 理想无损延迟线（S11=S22=0，S21=S12=cosθ−j·sinθ，θ=2πfτ）：

   ```
   ! ideal 100ps delay line for port extension practice
   # HZ S RI 50 R 50
   100000000 0 0 0.998027 -0.062791 0.998027 -0.062791 0 0
   200000000 0 0 0.992115 -0.125333 0.992115 -0.125333 0 0
   300000000 0 0 0.982287 -0.187381 0.982287 -0.187381 0 0
   400000000 0 0 0.968583 -0.248690 0.968583 -0.248690 0 0
   500000000 0 0 0.951057 -0.309017 0.951057 -0.309017 0 0
   600000000 0 0 0.929776 -0.368125 0.929776 -0.368125 0 0
   700000000 0 0 0.904827 -0.425779 0.904827 -0.425779 0 0
   800000000 0 0 0.876307 -0.481754 0.876307 -0.481754 0 0
   900000000 0 0 0.844328 -0.535827 0.844328 -0.535827 0 0
   1000000000 0 0 0.809017 -0.587785 0.809017 -0.587785 0 0
   ```

   这份文件是**示例代码**（按 u8-l4 学过的 Touchstone RI 格式手工生成），它本身就是「夹具（100 ps 线）+ 零长度 DUT」的仿真测量。
2. 启动 GUI，File → Import… 导入该文件，为四个 S 参数各建一条迹线，把 S21 显示为相位（Y 轴选 S21 的 Phase，u8-l1 的轴类型）。
3. 菜单 VNA → De-embedding → Setup… 添加 Port Extension，Port=1，Time 填 `100p`，DCloss/Loss 留 0。
4. 菜单 VNA → De-embedding → **De-embed traces…**（入口见 [vna.cpp:218-226](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L218-L226)；该对话框用 [manualdeembeddingdialog.cpp:15-33](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/manualdeembeddingdialog.cpp#L15-L33) 把选中迹线经 `Trace::assembleDatapoints()` 重组后走同一条 `Deembed()` 管线）。勾选 Include Port 2 以便同时选上 S21/S12/S22，全部选择后确认。
5. 观察去嵌入后的 S21 相位与幅度。

**需要观察的现象 / 预期结果**：

- 手算：\(\varphi = -2\pi f\tau = -2\pi \times 10^9 \times 100\times10^{-12} = -0.6283\ \text{rad} = -36°\)。文件里 1 GHz 处 S21 相位正是 −36°（S21 = 0.809 − j0.588）。GUI 除以 correction（幅值 1、相位 −36°），S21 应旋转 **+36°回到 0°**，幅度保持 0 dB——十个频点全部恢复为 \(1\angle 0°\)。
- S11 = 0 不变；若只选了 S11（端口选择器只显示受影响端口的参数，见 [sparamtraceselector.cpp:226-236](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/sparamtraceselector.cpp#L226-L236)），S11 会被除以 correction²，仍是 0。
- 再次执行 De-embed traces… 时会询问是否清除旧的去嵌入数据（[manualdeembeddingdialog.cpp:19-31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/manualdeembeddingdialog.cpp#L19-L31)）——去嵌入不可逆，重复执行会二次旋转，这正是这道防线存在的原因。

若手头有硬件：在校准面接开路件，点对话框里的 Open 按钮触发自动测量，对比自动解出的 delay 与你用尺子量的电缆长度 ÷ (0.66c)。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：端口 1 延伸 100 ps，1 GHz 处 S11 相位旋转多少度？
**答**：反射走来回，除以 correction²，即旋转 \(2\times36° = +72°\)。对应源码里两个独立 `if` 各除一次（[portextension.cpp:51-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L51-L60)）。

**练习 2**：为什么损耗模型用 \(\sqrt{f}\) 而不是 \(f\)？
**答**：导体趋肤效应使串联电阻正比 \(\sqrt{f}\)，介质损耗另有一项，窄带内近似总损耗 \(a_{DC} + a_1\sqrt{f/f_{ref}}\)；自动测量的回归变量也取 \(\sqrt{f/f_{max}}\)（L179），两者一一对应。

**练习 3**：自动测量为什么最后把 delay 除以 2？
**答**：反射系数测的是「去 + 回」的双程时延，而端口延伸参数 correction 是单向的（S11 会命中两次自动翻倍），所以要先除以 2（L189-190）。损耗回归的 −α/2、−β/2 同理。

### 4.2 阻抗再归一化与匹配网络

#### 4.2.1 概念说明

这两个选项解决「参考阻抗/已知电路」层面的问题，共用一个思想：**S 参数不便于直接做网络代数，先换到 Z 或 ABCD 域，做完运算再换回来**。

**阻抗再归一化（ImpedanceRenormalization）**：VNA 校准时假定系统阻抗 Z0（通常 50Ω），但 DUT 实际工作在别的阻抗体系（如 75Ω 的视频系统、差分 100Ω）。再归一化把「同一物理网络、在 50Ω 参考下测得」的 S 参数改写为「以新阻抗为参考」的 S 参数。它不改变 DUT，只改变**描述参考系**。数学上必须绕道 Z 参数（阻抗是物理量，与参考系无关），这正是换域的价值。

**匹配网络（MatchingNetwork）**：已知夹具/调谐电路是一个集总网络（串联/并联的 R、L、C，或一份 Touchstone 描述的黑盒双端口），把它**加上**（addNetwork=true，仿真嵌入效果）或**减去**（去嵌入）。集总网络的级联天然适合 ABCD 参数：每个元件一个小矩阵，整个网络 = 逐个相乘。

#### 4.2.2 核心流程

**阻抗再归一化**的三步换域（通用矩阵形式，N 端口同时换）：

\[
Z = \sqrt{z}\,(I+S)\,(I-S)^{-1}\,\sqrt{z},\qquad \sqrt{z}=\mathrm{diag}(\sqrt{Z_{0,1}},\dots,\sqrt{Z_{0,N}})
\]

\[
S' = \left(\sqrt{y}\,Z\,\sqrt{y}-I\right)\left(\sqrt{y}\,Z\,\sqrt{y}+I\right)^{-1},\qquad \sqrt{y}=\mathrm{diag}(1/\sqrt{Z'_{0,1}},\dots)
\]

单端口实数特例即熟悉的 \(Z = Z_0\frac{1+S}{1-S}\)、\(S' = \frac{Z-Z_0'}{Z+Z_0'}\)。矩阵形式之所以写成 \(\sqrt{z}\)（而不是直接乘 Z0），是因为它对**每个端口不同阻抗、甚至复数阻抗**都成立。

**匹配网络**的运算流程：

```
（按频率缓存）第一次遇到某频率时：
    forward = C1·C2·…·Cn          ← 从仪器端口看向 DUT 的元件顺序
    reverse = Cn·…·C2·C1          ← 从 DUT 侧回望的相反顺序
    若 addNetwork == false：两个矩阵都求逆
每个测量点：
    需要 S<port><port> 存在，否则整个选项放弃
    Γ_in = S22(forward) / (1 − S22(forward)·S_pp)     ← 几何级数闭合形式
    S_pp（网络所在端口的反射）：ABCD(forward)·ABCD(S_pp 视为负载) 取 S11
    S_ii（其他端口反射）：ABCD(S{ii,port})·reverse 后取回 S
    S_ij（两端都不是 port 的传输，≥3 端口情形）：+ S_pj·Γ_in·S_ip（寄生路径）
```

每个元件的 ABCD 矩阵（`MatchingComponent::parameters()`）：

| 元件 | ABCD 矩阵 \((A,B,C,D)\) |
|---|---|
| 串联 R | \((1,\ R,\ 0,\ 1)\) |
| 串联 L | \((1,\ j\omega L,\ 0,\ 1)\) |
| 串联 C | \((1,\ 1/j\omega C,\ 0,\ 1)\) |
| 并联 R | \((1,\ 0,\ 1/R,\ 1)\) |
| 并联 L | \((1,\ 0,\ 1/j\omega L,\ 1)\) |
| 并联 C | \((1,\ 0,\ j\omega C,\ 1)\) |
| Touchstone Through | 由 s2p 文件插值出 S 再转 ABCD |
| Touchstone Shunt | 由 s2p 文件插值出 Y，取 \(Y_{11}\) 作 C 项 |

#### 4.2.3 源码精读

**阻抗再归一化**的全部计算只有四行：

- [impedancerenormalization.cpp:38-45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/impedancerenormalization.cpp#L38-L45) —— `toSparam()` 把字符串键的测量 map 组装成 S 矩阵（u3-l1），`Zparam(S, p.Z0)` 换到旧参考阻抗下的 Z，`Sparam(Z, impedance)` 换到新参考阻抗，最后 `p.Z0 = impedance` 让后续选项知道参考系已变。注意它影响**所有端口**：[impedancerenormalization.cpp:29-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/impedancerenormalization.cpp#L29-L36) 的 `getAffectedPorts()` 返回当前活动驱动 VNA 端口的全集。

两条换域公式在数学库中的落点（注释里就写着公式）：

- [parameters.cpp:186-208](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L186-L208) —— `Zparam::Zparam(S, Z0n)`：L199-207 构造单位阵与 \(\sqrt{z}\) 对角阵，套用 \(Z=\sqrt{z}(I+S)(I-S)^{-1}\sqrt{z}\)。
- [parameters.cpp:27-56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L27-L56) —— `Sparam::Sparam(Z, Z0n)`：反向公式 \(S=(\sqrt{y}Z\sqrt{y}-I)(\sqrt{y}Z\sqrt{y}+I)^{-1}\)，\(\sqrt{y}\) 对角元是 \(1/\sqrt{Z_{0n}}\)。

**匹配网络**先看缓存与矩阵准备：

- [matchingnetwork.cpp:92-110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L92-L110) —— `matching` 是按频率缓存的 map：该频率首次出现时把 `forward`/`reverse` 两个 ABCD 乘出来（元件值只在这时读取一次），`addNetwork==false` 时立即求逆（ABCD 求逆是解析二阶逆，见 [parameters.h:85-94](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L85-L94)）。任何元件增删、改值、模式切换都会 `matching.clear()`（如 [matchingnetwork.cpp:337-349](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L337-L349)），下一批数据到来时重算——典型的惰性求值缓存。

多重反射闭合形式与三类修正：

- [matchingnetwork.cpp:115-123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L115-L123) —— 先检查 `S<port><port>` 存在（缺失则整个点放弃）；`matchingReflectionS` 取网络矩阵的 S22，`internalPortReflectionS = Γ_f/(1−Γ_f·S_pp)` 就是前置知识③的几何级数，给出「从 DUT 端看向网络内部」的等效反射。
- [matchingnetwork.cpp:133-149](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L133-L149) —— 反射参数分两支：网络自己端口的 S_pp（L137-139，把测得的 Γ 视为二端口负载 `\texttt{Sparam}(\Gamma,1,1,0)` 与 forward 级联后取 S11）；其他端口的 S_ii（L143-148，先用 `reduceTo({i,port})` 裁出相关双端口，再右乘 `reverse`——从 DUT 侧回望时元件顺序颠倒，所以缓存里专门存了反向乘积）。
- [matchingnetwork.cpp:150-166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L150-L166) —— 传输参数中两端都不是 `port` 的（仅 ≥3 端口测量会出现），补上经网络内部反射绕行的寄生路径 `toPort·Γ_in·fromPort`；涉及 `port` 的传输项已在反射分支的 `toSparam/fromSparam` 中一并处理。

元件的 ABCD 定义：

- [matchingnetwork.cpp:672-708](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L672-L708) —— 上表八种元件逐一返回 ABCD 矩阵；两种 Touchstone 元件（DefinedThrough/DefinedShunt）在频率超出文件范围时返回单位阵「透明通过」（L688-690、L697-699），文件数据用插值（`touchstone->interpolate(freq)`，u8-l4）。元件类型枚举与拖拽编辑器见 [matchingnetwork.h:15-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.h#L15-L60)，编辑器的拖拽实现（eventFilter + QDrag）在 [matchingnetwork.cpp:394-509](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L394-L509)，属于纯 UI，读懂 `parameters()` 即可跳过。

SCPI 面与 u9-l4 一致：选项级有 PORT/ADD/CLEAR/NUMber/NEW/DELete/TYPE（[matchingnetwork.cpp:34-79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L34-L79)），每个元件是编号子节点，增删后 `updateSCPINames()` 统一重编号（[matchingnetwork.cpp:511-524](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/matchingnetwork.cpp#L511-L524)）。

#### 4.2.4 代码实践

**实践目标**：用两个手算可验证的例子，分别验证再归一化公式与 ABCD 级联。

**A. 阻抗再归一化（约 10 分钟）**

1. 导入任意一份 50Ω 的 S 参数测量（可用 4.1.4 的文件，其 S11=0 恰好是理想匹配）。
2. De-embedding → Setup… 添加 Impedance Renormalization，impedance 填 `75`。
3. De-embed traces… 只选 S11，确认。

预期：S11 从 0 变为恒定 −0.2（约 −14 dB）。手算：\(Z = 50\frac{1+0}{1-0} = 50\Omega\)，\(S' = \frac{50-75}{50+75} = -0.2\)。幅度 0.2 → −13.98 dB，相位 180°。若 GUI 与此不符，优先检查导入文件的参考阻抗是否真的是 50Ω。

**B. 匹配网络嵌入（约 10 分钟）**

1. 同一份导入测量，添加 Matching Network，Port=1，选 Add network，从左侧元件栏拖一个 **Series L** 进图，值保持默认 1 nH。
2. De-embed traces… 选 S11，确认。
3. 手算 1 GHz 处的期望值：\(\omega L = 2\pi\times10^9\times10^{-9} = 6.283\Omega\)，串联元件在 50Ω 系统中的反射 \(S_{11} = \frac{j\omega L}{2Z_0+j\omega L} = \frac{j6.283}{100+j6.283}\)，幅度 \(\approx 0.0627\)（−24.1 dB），相位 \(\approx 86.4°\)。

需要观察：S11 迹线在 1 GHz 处应落在 (−24.1 dB, 86.4°) 附近，且随频率呈现 \(|S_{11}|=\frac{\omega L}{\sqrt{(2Z_0)^2+(\omega L)^2}}\) 的滚升曲线（1 GHz 处 0.0627，100 MHz 处约 0.0063 即 −44 dB）。若把 Add/Remove 切到 Remove 再执行，应近似还原为原始 S11（该文件 S11=0，除以网络逆矩阵在数值上仍得 0）。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：为什么再归一化必须经过 Z 参数，不能在 S 域直接换算？
**答**：S 参数依赖参考阻抗（归一化波定义），换参考系没有直接的 S 域公式；Z 参数（开路阻抗）是物理量，与参考系无关，所以路线是 S(Z0) → Z → S(Z0′)。对应 [impedancerenormalization.cpp:40-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/impedancerenormalization.cpp#L40-L43) 的两次构造函数调用。

**练习 2**：`matching` 缓存为什么以频率为 key、元件一变就要整体清空？
**答**：元件值是频率的函数（L、C 的 ABCD 含 ω），但元件**列表与连接关系**与频率无关，逐频率把两个方向的总 ABCD 乘好后缓存，同一频率的后续测量点零开销；任何结构/参数变化使缓存失效，`matching.clear()` 后惰性重建。

**练习 3**：`internalPortReflectionS` 的分母 \(1-\Gamma_f S_{pp}\) 什么时候影响大？
**答**：当夹具输入反射与负载反射都较大（高 Q 失配）时，多重反射项 \(\Gamma_f\Gamma_L\) 接近 1，级数收敛慢，闭合形式与单次反射差异显著——这正是端口延伸（完全忽略反射）失效、需要匹配网络或 2x-Thru 的场景。

### 4.3 2x-Thru 智能去嵌入（TwoThru）

#### 4.3.1 概念说明

端口延伸假设夹具无损均匀、匹配网络要求你**知道**夹具电路，2x-Thru 则只要一件东西：一个「两段夹具直接对接」的标准件（2x-thru）。测出它的双端口 S 参数，算法在时间域把它从中点「切开」，左半归夹具 1、右半归夹具 2，各自还原出完整的双端口误差盒（error box），此后每个测量点用 T 参数级联把两侧夹具剥掉：

\[
T_{\text{corr}} = T_{\text{fix1}}^{-1}\cdot T_{\text{meas}}\cdot T_{\text{fix2}}^{-1}
\]

因为误差盒是完整的双端口矩阵，夹具的损耗、色散、失配反射全部被建模——这是三选项中唯一「全双端口」的修正。算法源自 IEEE P370 标准的参考实现，源码注释明确指向其 MATLAB/Octave 脚本（[twothru.cpp:266](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L266)、[twothru.cpp:377](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L377)）。

TwoThru 有两档：

- **基础档**：只要 2x-thru 测量（对应 P370 `IEEEP3702xThru`），假设两段夹具对接处无反射突变。
- **增强档**：再加一份「夹具–DUT–夹具」测量（对应 P370 `IEEEP370Zc2xThru`），可处理夹具与 DUT 相接处阻抗不连续（characteristic impedance 不同）的情形。

#### 4.3.2 核心流程

**输入要求**（增强档）：

1. 2x-thru 测量：两段夹具直连，完整双端口 S 参数；
2. fix-DUT-fix 测量：夹具中间夹着 DUT（或 DUT 复制品），完整双端口 S 参数；
3. 两份测量**点数相同、频率栅格完全一致**（同一扫描设置下先后测两次即可），代码逐点校验；
4. 均匀频率步进（算法要做 FFT），非均匀栅格会被自动插值成均匀的。

**基础档算法**（把 2x-thru 从时间域中点切开）：

```
准备：剔除 DC 点 → 插值到均匀栅格 → 取出 S11,S12,S21,S22 向量
1. 频谱对称化 makeSymmetric：外推 DC 点（|DC|=2|S[0]|−|S[1]|，相位同理），
   再拼接负频率端的共轭 → 共轭对称频谱对应实数时域信号
2. IFFT + partial_sum：S11、S21 各自得到实数阶跃响应
3. 找 S21 阶跃响应到达终值 50% 的时刻 = 两段夹具的接缝位置
4. 把 S11 阶跃响应在接缝处截断（前半段属于夹具 1），
   差分回冲激响应再 FFT 回频域 → 夹具 1 的 S11（p111x）
5. 由级联信号流关系反解：
       p221x = (S11_2x − p111x) / S21_2x          ← 夹具 2 的 S11（对侧视角）
       p211x = ±sqrt(S21_2x·(1 − p221x²))         ← 夹具 1 的 S21
   开方符号逐点追踪：相邻点相位跳变超过 π/2 就翻转符号（防 180° 跳变）
6. 对 S22/S12 重复 1-5 得夹具 2（共享已算出的对侧量保证整体一致）
7. 两个误差盒 → T 参数 → 求逆 → 存入 points
```

**增强档算法**（多了「剥洋葱」）：先从 2x-thru 的 S21 解卷绕相位得到传播常数 \(\gamma=\alpha+j\beta\)；用迭代法外推出缺失的 DC 点（准则：时域阶跃响应在负时刻必须为零——因果性约束）；再把 fix-DUT-fix 的 S11 变换到阻抗域阶跃响应，从中**逐段剥落**长度 \(l = 1/(2x)\)（x 为 2x-thru 冲激响应峰值位置）的有损传输线，剥落的线累乘进误差盒 ABCD；最后 `hybrid` 步骤只保留剥落得到的反射项（e00/e11），传输项改用 2x-thru 的 S21 重算以保证能量一致。

**在线修正**（每个测量点，[twothru.cpp:28-64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L28-L64)）：测量点频率落在标定栅格内则取对应逆矩阵（不在格点上就线性插值两个逆 T 矩阵），栅格外取端点值；把 `S{port1,port2}` 子双端口转 T，左乘右乘两个逆矩阵，再转回 S 写回。

#### 4.3.3 源码精读

在线修正路径（测量时每个点都走）：

- [twothru.cpp:28-64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L28-L64) —— L31 `points.size()>0` 表示尚未标定（没测过 2x-thru 并点 Calculate）时该选项是透明的；L32 用 `reduceTo({port1,port2})` 从可能的多端口测量中裁出相关双端口（u3-l3 的 CompoundDriver 场景）；L35-58 三种取值：低于首点/高于末点取端点（钳位），格点精确命中直接用，否则 L54-56 对两个逆 T 矩阵做线性插值（`Tparam` 重载了 `+` 与 `*double`，见 [parameters.h:118-147](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.h#L118-L147)）；L60-62 三矩阵相乘后 `fromSparam(..., {port1,port2})` 写回原子视图。

编辑对话框与测量编排：

- [twothru.cpp:122-197](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L122-L197) —— L159-179 两个测量按钮只是置位 `measuring2xthru/measuringDUT` 后发 `triggerMeasurement` 信号（复用 u9-l4 的测量管线，一整条扫描回来经 `measurementCompleted` 存进对应 vector，L112-120）；L143-154 改端口即清空全部数据；L181-190 Calculate 按钮按有无 DUT 测量选择两个算法重载。L134-136 有句坦白的注释：Z0 的取值「似乎看不出差别」，于是输入框被隐藏——增强档里 Z0 只作 ABCD 换算的参考，数学上应可吸收进结果，作者选择向用户隐藏这个自由度。

- [twothru.cpp:445-460](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L445-L460) —— 增强档的两道输入防线：点数不等、任一频点相对偏差超过 \(10^{-9}\) 都弹窗拒绝计算。这就是 4.3.2 说的「同一扫描设置测两次」的代码化。

基础档的频谱对称化与阶跃响应：

- [twothru.cpp:292-311](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L292-L311) —— `makeSymmetric` 外推 DC 并拼接共轭；`makeRealAndScale` 取实部并除以 N——这不是多余操作：FFT 封装明确说明「逆变换不做缩放」（[fftcomplex.h:35-38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h#L35-L38)），1/N 缩放要在这里手动补上（u8-l6 讲过同一件事）。
- [twothru.cpp:317-336](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L317-L336) —— IFFT 得冲激响应、`partial_sum` 累加成阶跃响应、`Fft::shift` 把零时刻搬到中心（类似 MATLAB fftshift）；L333-336 以 S21 阶跃终值 50% 为门限定位接缝。
- [twothru.cpp:339-345](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L339-L345) —— 阶跃响应前半段掩膜保留、`adjacent_difference` 差分回冲激、FFT 回频域，得到夹具 1 的 S11。整个过程是 u8-l6「频→时→处理→回频」模式的复用。

符号追踪开方：

- [twothru.cpp:348-368](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L348-L368) —— L353 级联信号流反解 `p221x`，L354 开方求 `p211x`。L356-363 的注释很值得读：参考 Octave 脚本用 `if(arg(test)-arg(last_test) > 0)` 判翻转，作者发现那会导致 180° 相位翻转且不合逻辑，改成「相邻点相位差超过 π/2 才翻转符号」——正确的判据应该是跳变大（选错了开方分支），而不是跳变为正。这是仓库里「移植参考实现时修正其 bug」的一个范例。

增强档特有的三个构件：

- [twothru.cpp:485-502](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L485-L502) —— 从 S21 逐点解卷绕相位得 \(\beta\)，由 \(20\log_{10}|S_{21}|\) 得 \(\alpha\)（L498 的 `/−8.686` 即 \(20/(10\log 10)\) 换算成奈培每单位长度）。
- [twothru.cpp:521-569](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L521-L569) —— `DC2`：测量没有 0 Hz 点，而时域处理需要 DC。以 0.002 为种子迭代（割线法思路，L561-563），准则 `err = |h1[ts] − 0|`（L564）：调 DC 值直到阶跃响应在 −3 ns 时刻为零——因果系统在负时刻无响应，这是用物理约束当优化目标。
- [twothru.cpp:571-587](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L571-L579) 与 [twothru.cpp:618-687](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L618-L687) —— `makeTL` 用双曲函数 sinh/cosh 生成有损传输线 S 参数（教材上均匀线公式原样出现，L574-575）；`makeErrorbox` 是剥洋葱主体：L632-637 由 2x-thru 冲激响应峰值定位夹具电长度、定每次剥落的线长 \(l=1/(2x)\)，L645-681 循环剥落（L664 把 S 阶跃响应换算成阻抗 \(z=-Z_0\frac{s+1}{s-1}\)，L676-679 每轮 `abcd_TL.inverse() * abcd_dut` 从数据上剥掉、同轮 `abcd_errorbox * abcd_TL` 累积到误差盒上）；L686 调 `hybrid`（[twothru.cpp:581-616](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L581-L616)）：反射项用剥落结果、传输项按 4.3.2 第 5 步的 sqrt 公式从 2x-thru 重算。

两侧与收尾：

- [twothru.cpp:376-442](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L376-L442) —— 对 S22/S12 重复整套流程得夹具 2（变量沿用 P370 脚本命名，「从端口 2 看过去的 S22 现在叫 p112x」，L376-377 注释）；L428-429 两侧误差盒共享交叉项。最终 [twothru.cpp:434-442](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L434-L442)（增强档对应 [twothru.cpp:707-717](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L707-L717)）把每个频点的误差盒转 T、求逆、连同频率存进 `points`。

均匀栅格保障：

- [twothru.cpp:721-767](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L721-L767) —— `interpolateEvenFrequencySteps`：已是均匀栅格则原样通过（剔除 DC），否则以首点步进重建栅格并逐点插值（`VNAMeasurement::interpolateTo`）。对数扫描的测量在这里被悄悄转成线性栅格，代价是插值误差——追求 2x-Thru 精度时应直接用线性扫描测量。

顺带一提：TwoThru 的持久化只存算好的逆 T 矩阵逐频点展开的实/虚部（[twothru.cpp:199-262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L199-L262)），不存原始测量——与校准文件「存测量、加载重算」（u9-l2）的策略相反，因为重算代价高而逆矩阵用起来便宜。

#### 4.3.4 代码实践

**实践目标**：走读源码回答「2x-Thru 需要哪些测量输入」，并核对在线修正的取值逻辑。

1. **输入清单**：打开 [twothru.cpp:122-197](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L122-L197) 的 `edit()` 与 [twothru.cpp:97-109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L97-L109) 的 `updateGUI()`，用自己的话写出：必需输入是什么、可选输入是什么、什么条件下 Calculate 可点、Z0 为什么被隐藏。
2. **一致性约束**：阅读 [twothru.cpp:449-460](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L449-L460)，回答：两份测量点数差 1 会怎样？频率差 1 Hz（在 1 GHz 处）会怎样？（后者提示：容差是相对的 `frequency/1e9`，1 GHz 处允许 1 Hz。）
3. **离线小实验（无硬件、不依赖 GUI）**：4.1.4 的 `delay100ps.s2p` 本身就是一个合法的「2x-thru」——它是两段各 50 ps 夹具的直连。如果你想手算：理想延迟线 S11=S22=0、S21=S12=e^{−jωτ}，代入 4.3.2 第 5 步的两个公式，可得 p221x = (0−0)/S21 = 0、p211x = sqrt(S21·(1−0)) = e^{−jωτ/2}（取不跳变的符号）——即算法应当把延迟线平分为两个各 50 ps 的误差盒。有硬件时可实测验证：测 2x-thru → Calculate → 再测一次 2x-thru 作为「DUT」，开启 De-embed VNA samples 后 S21 相位应恒为 0°、S11 保持深负 dB（受限于重复性）。（待本地验证）

需要观察（第 3 步手算部分）：p211x 的模为 1、相位为 \(-\omega\tau/2 = -18°\) @1 GHz，两个误差盒级联相乘恢复 \(-36°\)，与在线修正公式 \(T_{fix1}^{-1}T_{meas}T_{fix2}^{-1}\) 互为逆运算、整体归一。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `makeRealAndScale` 里要除以 `in.size()`？
**答**：项目的 FFT 封装明确「逆变换不做缩放」（[fftcomplex.h:35-38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/fftcomplex.h#L35-L38)），正确的 1/N 归一化由调用方补上；同时取实部，因为对称化后的频谱理论上对应实信号，虚部是数值残渣。

**练习 2**：在线修正对栅格外与栅格内的频率分别怎么处理？为什么不外推？
**答**：栅格外直接钳位取端点的逆矩阵（[twothru.cpp:35-40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/twothru.cpp#L35-L40)），栅格内精确命中或线性插值两个逆 T 矩阵（L43-57）。标定数据只覆盖测量过的频段，外推误差无界，钳位是保守选择；插值发生在逆矩阵域而非 S 域，因为 T 参数随频率变化更平缓。

**练习 3**：基础档与增强档各自假设了什么、付出了什么代价？
**答**：基础档只测 2x-thru，隐含假设两段夹具在中点对接处无阻抗突变，测量成本低；增强档加测 fix-DUT-fix，通过 \(\gamma\) 提取与逐段剥落吸收夹具与 DUT 相接处特性阻抗不同（P370 称 Zc 校正）的情形，代价是多一次测量且两份栅格必须严格一致（L449-460）。

## 5. 综合实践

**任务：搭一条「仿真夹具测量」流水线，并用三种选项分别修它。**

无硬件闭环（约 40 分钟）：

1. **造夹具**：用 4.1.4 的方法再造一份 `fixture_dut.s2p`——「100 ps 延迟线（夹具） + 一个真实 DUT」的串联。取 DUT 为一个 20 pF 并联电容到地（按 u8-l4 的 Touchstone 格式逐点手算，或写十行 Python 生成：先算 DUT 的 S 参数，再与延迟线做 T 参数级联）。
2. **端口延伸修**：导入后对 S21 施加 100 ps 端口延伸，观察它只能修平相位斜率，S11 仍残留电容反射——印证「端口延伸不建模失配」。
3. **匹配网络修**：你知道夹具其实是一段线，但没有线的模型；改用「并联 20 pF 是 DUT」这一知识反过来验证：先给数据加一个 **Add network 的并联 20 pF**，看 S11 反射是否加倍；再用 Remove 还原。理解 add/remove 是同一数学的两个方向。
4. **阻抗再归一化**：把整条链改到 75Ω 参考下观察 Smith 图中心偏移。
5. **写结论**：每种选项在 \(|S_{11}|\)、S21 相位两个维度上各修掉了什么、修不掉什么，输出一张对照表。

有硬件时的进阶（待本地验证）：用两条等长电缆 + 直通转接做 2x-thru，实测校准后按 TwoThru 流程标定，再用电缆夹一个 20 dB 衰减器当 DUT，验证去嵌入后 S21 在 0 dB 附近、S11 深负。

## 6. 本讲小结

- **端口延伸**把夹具压缩为「时延 + √f 损耗」两个标量，修正就是除以 \(att\cdot e^{-j2\pi f\tau}\)；反射参数因两个独立 `if` 天然被除两次，对应双程物理。开路/短路自动测量用相位斜率解时延、√f 最小二乘解损耗，来回一律除以 2。
- **阻抗再归一化**是三行换域：\(S(Z_0)\xrightarrow{\sqrt{z}(I+S)(I-S)^{-1}\sqrt{z}}Z\xrightarrow{(\sqrt{y}Z\sqrt{y}\mp I)}S(Z_0')\)，Z 参数作物理量中转，公式实现在 `Tools/parameters.cpp` 并支持每端口不同阻抗。
- **匹配网络**用 ABCD 级联（元件矩阵逐个相乘）表示集总网络，add/remove 对应是否求逆；多重反射用几何级数闭合形式 \(\Gamma_f/(1-\Gamma_f\Gamma_L)\) 一次算清，逐频率缓存、结构一变即失效。
- **2x-Thru**只需一个直通标准件：时间域从阶跃响应 50% 处切开，信号流公式加带符号追踪的开方还原两个完整误差盒，在线以 \(T_{fix1}^{-1}T_{meas}T_{fix2}^{-1}\) 剥离；增强档再加 fix-DUT-fix 测量，靠 \(\gamma\) 提取与逐段剥落处理阻抗不连续。
- 四个选项都只是 `transformDatapoint()` 的接力者，可任意串联：例如先 2x-Thru 剥夹具、再阻抗再归一化换参考系——顺序即管线顺序（u9-l4）。
- 三个实践全部可在零硬件下完成：手工 Touchstone 文件给出已知标准答案，是验证这些数学代码最快的方法。

## 7. 下一步学习建议

本讲是单元 9（校准与去嵌入）的收官。建议：

1. **回头对照 u9-l2**：校准求解器的 12 项误差模型与 2x-Thru 的误差盒是同一思想（误差网络建模）在不同参考面的应用，比较两者的「已知标准件数量 vs 可解误差项数量」关系。
2. **进入单元 10（远程控制）**：把本讲的 SCPI 参数（如 `DEEMBedding:NEW PORTEXTension`、`:DELAY`）纳入 u10-l2 的 TCP 自动化实验，实现「脚本配置去嵌入 + 取数」的无人值守测量。
3. **源码延伸阅读**：`Tools/parameters.cpp` 的其余换算（ABCD↔S、T↔S）是 u11-l1 参数测试的对象，本讲引用过的每个构造函数在 `LibreVNA-Test/parametertests.cpp` 中都有数值验证用例，读测试可帮你确认对手算公式的理解。
4. 若要写自己的去嵌入选项：以 `portextension.cpp`（最短）为模板，按 u9-l4 的四处触点（Type 枚举、工厂、TypeToString、.pro）注册，再回到本讲挑选最贴近你数学的选项作参照。
