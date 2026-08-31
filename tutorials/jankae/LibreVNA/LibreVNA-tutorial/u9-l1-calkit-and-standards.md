# 校准件与校准套件模型

## 1. 本讲目标

校准（单元九的总主题）不是「点一下按钮让仪器变准」的魔法，而是一次**已知物理标准的比对**：你拿一个「理论上它该是什么」的标准件去测，仪器测到的偏差就是系统误差。因此，**校准的质量上限，先取决于你对标准件的数学描述有多准确**。

本讲解决「理论值从哪里来」这个问题。读完本讲你应该能够：

1. 说出理想 Open / Short / Load / Through 的 S 参数定义，以及每个真实标准件相对理想模型的**偏离参数**（偏移传输线的 Z0/delay/loss，Open 的边缘电容 C0~C3，Short 的串联电感 L0~L3，Load 的 R/L/C 网络）各自描述什么物理缺陷。
2. 手算任意频率下任一标准件的理论反射系数（`toS11()` / `toSparam()` 的公式链）。
3. 解释 `Calkit` 如何用 JSON 组织一整套标准件、如何兼容三代历史文件格式、如何暴露为 SCPI 命令树。
4. 看懂 GUI 的校准件编辑对话框（`CalkitDialog` 与各标准件的 `edit()`），并能为自制校准件（一个电阻、一段同轴线）录入近似模型。

本讲只讲「标准件模型」这一层；「拿这些理论值去解误差项」属于下一讲（u9-l2 校准求解器）。

## 2. 前置知识

### 2.1 反射系数与 S 参数（复习）

向阻抗为 \(Z\) 的负载入射一列波，反射系数是：

\[
\Gamma = \frac{Z - Z_\mathrm{ref}}{Z + Z_\mathrm{ref}}
\]

LibreVNA 的系统参考阻抗固定为 \(Z_\mathrm{ref} = 50\,\Omega\)（这一点后面在源码里会反复看到硬编码的 `50.0`）。理想开路 \(Z \to \infty\) 给出 \(\Gamma = +1\)，理想短路 \(Z = 0\) 给出 \(\Gamma = -1\)，理想 50 Ω 负载给出 \(\Gamma = 0\)。

对双端口标准件（Through/Line），S 参数是 2×2 矩阵；理想直通是 \(S_{11}=S_{22}=0,\ S_{21}=S_{12}=1\)。

### 2.2 「偏移传输线 + 非理想终端」模型

真实校准件不是贴在测量参考面上的纯开路/短路。以一个短路件为例：从 VNA 端口看进去，先是一小段传输线（SMA 连接器内部），末端才是短路面。这个模型叫 **offset 模型**，是 Keysight 应用笔记 *Specifying Calibration Standards and Kits for the Keysight 8722D Vector Network Analyzer*（代码注释里原样引用）定义的行业标准：

```
VNA 端口 ──[ 偏移传输线：Z0, delay, loss ]──[ 终端：理想缺陷模型 ]
                                            ├─ Open：边缘电容 C(f)
                                            ├─ Short：串联电感 L(f)
                                            ├─ Load：R + 串联 L + 并联 C
                                            └─ Through：偏移线本身就是标准
```

三个偏移参数的物理含义：

| 参数 | 单位（GUI 中） | 含义 |
|---|---|---|
| `Z0` | Ω | 偏移传输线的特征阻抗，**不是**系统参考阻抗 |
| `delay` | ps | 波从端口走到终端的单程时延 |
| `loss` | GΩ/s | 单位长度损耗，按 \(\sqrt{f}\) 规律集肤损耗建模 |

### 2.3 多项式寄生模型

Open 的边缘电容、Short 的引线电感随频率缓变，业界惯例用三次多项式表示（频率以 Hz 代入，源码中用 10 的幂缩放）：

\[
C_\mathrm{fringing} = C_0 \cdot 10^{-15} + C_1 \cdot 10^{-27} f + C_2 \cdot 10^{-36} f^2 + C_3 \cdot 10^{-45} f^3
\]

\[
L_\mathrm{series} = L_0 \cdot 10^{-12} + L_1 \cdot 10^{-24} f + L_2 \cdot 10^{-33} f^2 + L_3 \cdot 10^{-42} f^3
\]

直觉：\(C_0\) 是低频边缘电容（fF 量级），\(L_0\) 是低频引线电感（pH 量级），高阶项描述它随频率的漂移。自制校准件时，只填 0 阶项通常就够用。

### 2.4 与前面讲义的衔接

- 承接 u8-l4：标准件可以附带 Touchstone 测量文件代替系数模型，解析器就是上一单元读过的 `touchstone.cpp`。
- 承接 u2-l3：`Calkit` 的 JSON 序列化走的是 Savable/`toJSON`/`fromJSON` 那套契约。
- 承接 u10-l1（向后衔接）：`Calkit` 同时是一棵 `SCPINode` 命令子树。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [Calibration/calstandard.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.h) | 标准件类层次声明：`Virtual` → `OnePort`/`TwoPort` → 六种具体标准 |
| [Calibration/calstandard.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp) | 本讲的主角：所有「频率 → 理论 S 参数」公式与各标准件编辑对话框 |
| [Calibration/calkit.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.h) | 套件容器声明：标准件列表 + 元信息 + Savable/SCPINode 双继承 |
| [Calibration/calkit.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp) | 套件的 JSON 序列化、`.calkit` 文件读写、三代格式兼容、SCPI 命令注册 |
| [Calibration/calkitdialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkitdialog.cpp) | 套件级编辑对话框：增删改移标准件、打开/保存文件 |
| [Util/util.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp) | `Util::addTransmissionLine`：所有单端口标准共用的偏移传输线公式 |
| [Calibration/calibrationmeasurement.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp) | 模型的消费者：`getActual()` 调 `toS11()`，可用频率范围守卫 |
| [VNA/vna.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp) | GUI 入口：「Edit Calibration Kit」菜单项 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **校准件模型** —— `CalStandard` 类层次与「系数 or 测量文件」双模式、每种标准件的 S 参数公式。
2. **校准件套件管理** —— `Calkit` 容器：JSON、`.calkit` 文件、三代格式迁移、SCPI 树。
3. **编辑对话框** —— `CalkitDialog` 与六种标准件各自的 `edit()`。

### 4.1 校准件模型

#### 4.1.1 概念说明

一个「校准件」在代码里是一个对象，它只须回答一个问题：**「在频率 f，我的真实 S 参数是什么？」** 这就是 `CalStandard::Virtual` 定下的契约。求解器（下一讲）会拿这个理论值与实际读数配对解方程。

围绕这个问题，类层次分三层：

- `Virtual`：公共底座——名字、随机 ID、频率上下限、`edit()`、`toJSON()`、SCPI 节点。
- `OnePort` / `TwoPort`：按端口数分流。单端口件要实现 `toS11(freq)`；双端口件要实现 `toSparam(freq)`。
- 六种具体标准：SOLT 用的 `Open`、`Short`、`Load`、`Through`，TRL 用的 `Reflect`、`Line`。

另一个关键设计是**双模式**：每个标准件要么用「物理系数」描述（0 项默认，建模公式计算），要么挂一份 Touchstone 测量文件（厂家给的出厂数据）。两者互斥，`toS11()` 里 Touchstone 优先——这是理解编辑对话框那对单选按钮的前提。

#### 4.1.2 核心流程

以系数模式的 `Short::toS11(freq)` 为例，计算链是三步：

```
输入 freq
  │
  ├─ ① Lseries = L0·1e-12 + L1·1e-24·f + L2·1e-33·f² + L3·1e-42·f³   （串联电感）
  │
  ├─ ② Γ_T = (jωL − 50) / (jωL + 50)                                  （终端反射系数）
  │
  └─ ③ Γ_in = addTransmissionLine(Γ_T, Z0, delay·1e-12, loss·1e9, f)   （级联偏移线）
  │
输出 Γ_in 即理论 S11
```

第 ③ 步的数学（`addTransmissionLine` 实现的公式，源自 ASU LOCO 实验室报告）：把偏移线当作一段有损传输线，其特征阻抗与传播常数为

\[
Z_c = Z_0 + \frac{G}{2\omega}\sqrt{\tfrac{f}{10^9}} - j\,\frac{G}{2\omega}\sqrt{\tfrac{f}{10^9}}, \qquad
\gamma_l = \frac{G\,\tau}{2 Z_0}\sqrt{\tfrac{f}{10^9}} + j\!\left(\omega\tau + \frac{G\,\tau}{2 Z_0}\sqrt{\tfrac{f}{10^9}}\right)
\]

（\(G\) 是 `loss`，\(\tau\) 是 `delay`。）从端口看进去的输入反射系数是多次往返波的叠加：

\[
\Gamma_1 = \frac{Z_c - Z_r}{Z_c + Z_r}, \qquad
\Gamma_{in} = \frac{\Gamma_1\left(1 - e^{-2\gamma_l} - \Gamma_1\Gamma_T\right) + e^{-2\gamma_l}\,\Gamma_T}{1 - \Gamma_1\left(e^{-2\gamma_l}\Gamma_1 + \Gamma_T\left(1 - e^{-2\gamma_l}\right)\right)}
\]

当 \(Z_c = Z_r = 50\,\Omega\) 且无损时，\(\Gamma_1 = 0\)、\(e^{-2\gamma_l} = e^{-j2\omega\tau}\)，公式退化成熟悉的 \(\Gamma_{in} = \Gamma_T \cdot e^{-j2\omega\tau}\)——纯相位旋转，幅度不变。

#### 4.1.3 源码精读

**① 类层次与双端口分流**

[Calibration/calstandard.h:64-82](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.h#L64-L82) 定义 `OnePort`：纯虚的 `toS11(double freq)` 是单端口标准的唯一数学契约；`setMeasurement()` 用来挂 Touchstone，同时把该标准的可用频率范围收紧到文件覆盖的区间。

[Calibration/calstandard.h:158-176](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.h#L158-L176) 定义 `TwoPort`，契约换成 `toSparam()`。注意 `OnePort` 和 `TwoPort` 是**平行**的两条分支，不是继承关系——`Virtual` 直接派生两者，端口数决定走哪条路。

**② Virtual 底座：随机 ID 与默认无界的频率范围**

[Calibration/calstandard.cpp:14-21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L14-L21) 构造函数里，`minFreq`/`maxFreq` 初始为「负到正无穷」（系数模式全频段有效），`id` 由 [Util/util.cpp:94-102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp#L94-L102) 的 `mt19937_64` 随机数发生器生成。这个 ID 是套件层查重的关键（见 4.2.3）。

**③ Short：三步公式的原型**

[Calibration/calstandard.cpp:316-328](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L316-L328) 是短路件模型。第一行分支：有 Touchstone 就直接插值返回（测量模式优先）；否则按 4.1.2 的三步走：

- 第 322 行：多项式算 `Lseries`；
- 第 324-325 行：\( \Gamma_T = (j\omega L - 50)/(j\omega L + 50) \)——注意参考阻抗硬编码 `50.0`，而 `Z0` 是偏移线的；
- 第 326 行：套上偏移传输线（单位换算藏在实参里：`delay*1e-12` 把 ps 变 s，`loss*1e9` 把 GΩ/s 变 Ω/s）。

**④ Open：除零特判**

[Calibration/calstandard.cpp:182-200](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L182-L200) 是开路件。第 188 行算边缘电容 \(C_\mathrm{fringing}\)（容抗是 \(-j/(\omega C)\)）。理想开路 \(C=0\) 时容抗为无穷大，\(\Gamma_T = (\infty - 50)/(\infty + 50)\) 数学上取极限是 +1，但浮点会算出 `inf/inf = NaN`，所以第 191-194 行专门特判 \(C=0\) 直接返回 `1.0`。这个特判就是「理想开路件在套件里全零参数也能用」的原因。

**⑤ Load：Cfirst 的接线顺序**

[Calibration/calstandard.cpp:446-469](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L446-L469) 是负载件，终端是一个 **R + 串联 L + 并联 C** 三元件网络。这里的微妙点是元件级联顺序：`Cfirst` 为真表示「从 VNA 端口看，并联电容在最前面」——但由于代码是从负载**另一端**开始搭建阻抗的（第 452-456 行的注释明说了这一点），先加的反而是串联电感；`Cfirst` 为假时顺序对调（第 462-465 行）。并联电容用并联阻抗公式 \(\;Z_L \parallel Z_C = \frac{Z_L Z_C}{Z_L + Z_C}\;\) 合并（第 460 行）。

**⑥ Through：传输线 S 参数公式**

[Calibration/calstandard.cpp:665-692](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L665-L692) 直连件不需要终端模型，偏移线本身就是标准。第 679-681 行算 \(Z_c\)、\(\gamma_l\) 和传播因子 \(p = e^{-\gamma_l}\)（与 4.1.2 同源），第 688-689 行按 Keysight 应用笔记的式 (6)(7) 组装 S 参数：

\[
S_{xx} = \frac{\Gamma(1-p^2)}{1-p^2\Gamma^2}, \qquad S_{xy} = \frac{p(1-\Gamma^2)}{1-p^2\Gamma^2}
\]

且 \(S_{11}=S_{22},\ S_{21}=S_{12}\)（对称互易）。

**⑦ 公共偏移线公式**

[Util/util.cpp:183-202](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.cpp#L183-L202) 的 `addTransmissionLine` 被 Open/Short/Load 三者共用（Through 是把同一组公式内联展开成 S 参数形式）。端口扩展去嵌入（u9-l5）也复用这里。

**⑧ TRL 的两个「不求值」标准**

TRL 校准算法的特殊性在于它**不需要**知道反射标准和线的绝对 S 参数，只需知道符号（短路还是开路）和大致电长度。所以 [Calibration/calstandard.cpp:787-791](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L787-L791) 的 `Reflect::toS11()` 和 [Calibration/calstandard.cpp:844-853](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L844-L853) 的 `Line::toSparam()` 都返回 NaN——「理论值未定义」。而 `Line` 的可用频率范围由电长度约束：[Calibration/calstandard.cpp:908-913](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L908-L913) 的 `setDelay` 把范围设为线的相位在 20°~160° 之间的频段（TRL 对线电长度的经典约束）：

\[
f_\min = \frac{20^\circ}{360^\circ \cdot \tau}, \qquad f_\max = \frac{160^\circ}{360^\circ \cdot \tau}
\]

**⑨ 谁在消费这些公式**

[Calibration/calibrationmeasurement.cpp:366-369](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L366-L369)：校准测量类把「理论值」直接定义为所挂标准件的 `toS11()`。而 [Calibration/calibrationmeasurement.cpp:396-399](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibrationmeasurement.cpp#L396-L399) 则在测量频率超出标准件频率范围时把表格单元格标黄——这就是 `minFreq`/`maxFreq` 的用途。

#### 4.1.4 代码实践

**实践目标**：手算「理想 Short（零偏移）」在 1 GHz 的理论反射系数，验证与 GUI/模型行为一致。

**操作步骤**：

1. 启动 GUI（无硬件即可，参考 u1-l3 的构建步骤），进入 VNA 模式的 **Calibration → Edit Calibration Kit** 菜单（入口在 [VNA/vna.cpp:120-127](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L120-L127)）。
2. 点 **Add → Short**，双击新条目打开编辑对话框，录入：Z0 = 50 Ω、delay = 0、loss = 0、L0=L1=L2=L3 = 0（「零偏移」的含义）。
3. 手算。代入 [Calibration/calstandard.cpp:316-328](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L316-L328) 的公式链：
   - ① \(L_\mathrm{series} = 0\)，终端阻抗 \(j\omega L = 0\)；
   - ② \(\Gamma_T = (0-50)/(0+50) = -1\)；
   - ③ 偏移线零时延零损耗：\(Z_c = 50\)，\(\Gamma_1 = 0\)，\(e^{-2\gamma_l} = 1\)，代入 `addTransmissionLine` 分子分母得 \(\Gamma_{in} = -1/1 = -1\)。
4. 交叉验证模型自洽性：GUI 里其实预置了同样的理想套件——对照 [Calibration/calkit.cpp:540-550](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L540-L550) 的 `setIdealDefault()`，其中 `Short("Ideal Short Standard", 50.0, 0, 0, 0, 0, 0, 0)` 与你手录的参数完全一致。
5. 点对话框 **Save** 存成 `mykit.calkit`，用文本编辑器打开检查 JSON（详见 4.2.4）。

**需要观察的现象**：

- `mykit.calkit` 中该标准件的 `type` 为 `"Short"`，`params` 里所有 L 系数为 0（待本地验证，需要能运行 GUI 的环境）。
- **需要说明**：校准件编辑器本身没有「绘制理论 S11」的窗口——这个值只在求解器里被 `getActual()` 消费。所以「验证」有两条诚实路径：
  - **有硬件**（待本地验证）：用这套理想件完成 SOLT 校准后，再测这个短路件本身，Smith 图上校准后的 S11 应落在 \(-1\)（0 dB、180°）附近；
  - **无硬件**（本讲采用）：JSON 参数核对 + 手算，两者都指向 \(S_{11} = -1\)。

**预期结果**：\(S_{11}(1\,\mathrm{GHz}) = -1 + j0\)，即 \(|S_{11}| = 1\)（0 dB）、相位 180°。且由于所有参数为零，该值**与频率无关**——1 MHz 和 6 GHz 算出来一样。

#### 4.1.5 小练习与答案

**练习 1**：给理想短路件加上 \(L_0 = 100\,\mathrm{pH}\) 的引线电感，1 GHz 处 \(S_{11}\) 的幅度和相位变成多少？

**答案**：\(\omega L = 2\pi \times 10^9 \times 100\times10^{-12} = 0.6283\,\Omega\)。纯电抗终端无损，幅度仍为 1；相位 \(\approx 180^\circ - 2\arctan(0.6283/50) = 180^\circ - 2\times0.72^\circ \approx 178.56^\circ\)。引线电感让相位「提前」离开 180°，且随频率线性加剧——这正是 6 GHz 处短路件相位误差可达几十度的原因。

**练习 2**：理想开路件 \(C_0 = 50\,\mathrm{fF}\)，1 GHz 处相位偏离 0° 多少？

**答案**：容抗 \(-j/(\omega C) = -j\,3183\,\Omega\)。\(\Gamma = (-j3183-50)/(-j3183+50)\)，幅度 1，相位 \(\approx -1.8^\circ\)（分子幅角 −90.9° 减分母幅角 −89.1°）。对比练习 1 可见：50 fF 的电容和 100 pH 的电感在 1 GHz 造成的相位偏差同量级（这正是校准件「寄生参数必须建模」的定量感受）。

**练习 3**：`Open::toS11` 里为什么要对 `Cfringing == 0` 做特判？去掉会怎样？

**答案**：\(C=0\) 时 \(1/(\omega C)\) 是除以零，得到 `inf`；随后 `(inf-50)/(inf+50)` 是 `inf/inf`，C++ 浮点规定为 NaN，理想开路件会算出 NaN 而不是 +1。特判直接返回 `1.0`，等价于数学上的极限值。Short 不需要特判，因为 \(j\omega \cdot 0 = 0\) 是良定义的。

---

### 4.2 校准件套件管理

#### 4.2.1 概念说明

`Calkit` 是「一套」标准件：除了标准件列表，还有厂家、序列号、描述三条元信息。它解决三件事：

1. **容器**：增删标准件、防 ID 冲突；
2. **持久化**：读写 `.calkit` 文件（JSON），还要兼容两代历史格式；
3. **远程可编辑**：整套件挂成 SCPI 命令树，脚本可以增删标准件、写系数。

它的身份也是双重的：`class Calkit : public Savable, public SCPINode`（[Calibration/calkit.h:16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.h#L16)）——既能进出 JSON，又天然是命令树节点。这与 u2-l2 里 `Mode` 的三重继承是同一套设计语言。

注意持有关系：`Calkit` 不独立存在，它被 `Calibration` 持有（[Calibration/calibration.h:173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L173)），GUI 通过 `cal.getKit()` 拿到它。一个校准工作区自带一套件。

#### 4.2.2 核心流程

`.calkit` 文件读取的分诊流程（`fromFile`）：

```
打开文件 → 尝试解析为 JSON
  │
  ├─ JSON 含 "standards" 键 ──→ 新格式：fromJSON() 逐件 create+fromJSON
  │
  ├─ JSON 含 "SOLT" 键 ──────→ 旧 JSON 格式：解析平铺的 SOLT/TRL 系数表，
  │                             手工 new 出 7 个标准件，弹弃用警告
  │
  └─ 不是 JSON ─────────────→ 更老的逐行文本格式：readLine 逐个读系数，
                                同样转换成标准件对象
```

新格式 JSON 的形状（`toJSON` 产出）：

```json
{
  "Manufacturer": "...", "Serialnumber": "...", "Description": "...",
  "standards": [
    { "type": "Short", "params": { "name": "...", "id": 123456789,
        "Z0": 50.0, "delay": 0.0, "loss": 0.0,
        "L0": 0.0, "L1": 0.0, "L2": 0.0, "L3": 0.0 } },
    { "type": "Open", "params": { ..., "touchstone": { ... } } }
  ],
  "version": "1.5.0"
}
```

SCPI 命令面（构造函数注册）：`KIT:MANufacturer/SERial/DESCription`（读写）、`KIT:FILEname?`、`KIT:SAVE`（带文件名）、`KIT:LOAD`（带文件名触发 `fromFile`），以及子节点 `KIT:STAndard` 下的 `CLEar/NUMber?/NEW/DELete/TYPE?`——整套件可以被脚本完整重建。

#### 4.2.3 源码精读

**① 元信息的声明式描述表**

[Calibration/calkit.h:60-64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.h#L60-L64) 用 `SettingDescription` 表把三个字符串成员映射到 JSON 键（u2-l3 讲过的同一机制），`toJSON`/`fromJSON` 因此各只需一行 `createJSON/parseJSON`。

**② 增删标准件与 ID 查重**

[Calibration/calkit.cpp:477-494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L477-L494) 的 `addStandard` 先遍历已有标准件比对 64 位随机 ID，撞了就弹错误框并拒绝添加——因为 ID 是校准测量关联标准件的钥匙（重复会让 `fromJSON` 恢复的测量-标准件配对错乱）。[Calibration/calkit.cpp:496-501](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L496-L501) 的 `removeStandard` 则直接 `delete` 对象——套件拥有标准件的生存期。

**③ SCPI 名字按位置重排**

[Calibration/calkit.cpp:457-470](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L457-L470) 的 `updateSCPINames` 先把所有标准件摘下再按顺序挂回，并把 SCPI 节点名改成序号 `1,2,3...`。后果：SCPI 寻址是**按位置**的，删掉第 2 件后原第 3 件自动变成 `KIT:STAndard2`。先摘再挂的写法是为了避免改名过程中的临时重名。

**④ 新格式的序列化**

[Calibration/calkit.cpp:503-516](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L503-L516) 的 `toJSON` 把每个标准件序列化成 `{type, params}` 二元组——`type` 字符串决定 `fromJSON` 时工厂 `create()` new 什么类。这是「类型打头」的多态序列化惯用法。[Calibration/calkit.cpp:518-538](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L518-L538) 的 `fromJSON` 对缺字段、未知类型都静默跳过（容错优先）。`version` 字段写入的是应用程序版本号，供将来迁移参考。

**⑤ 三代格式兼容**

[Calibration/calkit.cpp:149-171](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L149-L171) 是新格式分支；[Calibration/calkit.cpp:216-297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L216-L297) 解析旧 JSON（当时男女头标准件是平铺的 30 多个键，靠一张 76 行的弃用描述表读入）；[Calibration/calkit.cpp:299-350](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L299-L350) 是逐行文本格式。两条老路径最终都汇合到 [Calibration/calkit.cpp:366-423](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L366-L423)：把老系数 `new` 成现代的 `CalStandard::Open/Short/Load/Through` 对象，相对路径的测量文件会被补成绝对路径，最后 [Calibration/calkit.cpp:425-427](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L425-L427) 弹出「请另存为新格式」的弃用提示。

**⑥ 理想默认套件**

[Calibration/calkit.cpp:540-550](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L540-L550) 的 `setIdealDefault()` 生成四件套：Open/Short 全零（C、L 系数为 0）、Load 是 `50 Ω + 0 寄生 + 0 偏移`、Through 是 `50 Ω + 0 时延 + 0 损耗`。这是新用户的起点，也是 u9 校准的「零知识先验」。

#### 4.2.4 代码实践

**实践目标**：不依赖 GUI 运行，从源码推导出一个 `.calkit` 文件应该长什么样，并（在有 GUI 的机器上）做一次写-读闭环验证。

**操作步骤**：

1. **源码阅读**：从 [Calibration/calkit.cpp:128-141](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L128-L141) 的 `toFile` 确认：扩展名不是 `.calkit` 会自动补上；文件内容是 `setw(4)` 缩进的 `toJSON()` 结果（4 空格缩进的人可读 JSON）。
2. **手工构造**：按 4.2.2 的 JSON 形状，手写一个含一个理想 Short 的 `mykit.calkit`（`id` 任取一个大整数，如 `7`）。
3. **加载验证**（待本地验证，需要 GUI）：Calibration → Edit Calibration Kit → Open，选择手写文件；标准件列表应出现一条 `Short, Ideal Short`。
4. **逆向验证**：在 GUI 里用 Add → Short 新建、Save 保存，再用文本编辑器打开，与你的手写文件逐字段对比——重点看 `type`、`params` 的键名与 `standards` 数组结构是否如 4.2.2 所述。

**需要观察的现象**：GUI 保存的文件里每个标准件都有唯一且巨大的 `id`（64 位随机数），而手写文件里的 `7` 也能正常加载——说明 `fromJSON` 对 id 不做格式校验，只要求套件内不重复。

**预期结果**：手写 JSON 与 GUI 保存的 JSON 除 `id`/`version`/元信息外结构一致；两种文件都能被加载且标准件参数（双击可见）与预期相符。GUI 侧的实际运行效果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`fromFile` 靠什么区分三代文件格式？

**答案**：两级判据（[Calibration/calkit.cpp:169-173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L169-L173) 与 [Calibration/calkit.cpp:216-218](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L216-L218)）：先尝试 JSON 解析，成功且含 `"standards"` 键是新格式；成功但只有 `"SOLT"` 键是旧 JSON；JSON 解析失败则回退到逐行文本格式。

**练习 2**：为什么 `addStandard` 要检查 ID 冲突？随机 64 位 ID 撞上的概率极小，这行代码是多余的吗？

**答案**：随机碰撞确实可忽略，但文件来源不止随机：手写/脚本生成的 `.calkit` 可以指定任意 `id`（`fromJSON` 原样读入），复制粘贴标准件条目就会产生真冲突。ID 冲突会让「校准测量 → 标准件」的按 ID 关联配对到错误的标准件上，静默出错最难排查，所以在入口处显式拒绝。

**练习 3**：TRL 老格式默认值里 `TRL.Line.delay = 74 ps`、频率范围 751 MHz~6 GHz（[Calibration/calkit.cpp:293-295](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L293-L295)）。用 4.1.3 ⑧ 的公式验证这三个数是自洽的。

**答案**：\(f_\min = \frac{20}{360 \times 74\,\mathrm{ps}} = \frac{0.0556}{74\times10^{-12}} \approx 751\,\mathrm{MHz}\)，\(f_\max = \frac{160}{360 \times 74\,\mathrm{ps}} = \frac{0.444}{74\times10^{-12}} \approx 6.0\,\mathrm{GHz}\)。老格式的硬编码默认值正是新代码 `Line::setDelay` 公式的手算结果——代码演进时把经验值变成了公式。

---

### 4.3 编辑对话框

#### 4.3.1 概念说明

编辑界面分两层：

- **套件层** `CalkitDialog`：一个列表 + 增/删/上移/下移按钮 + 元信息输入框 + Open/Save。它不编辑标准件本身，双击某条目时**委托**给该标准件自己的 `edit()`。
- **标准件层**：每种标准件各自实现 `edit()`（`Virtual` 的纯虚函数），弹出对应参数表单。这是**按类型分发 UI** 的典型做法——工厂 `create()` 按类型 new 对象，虚函数 `edit()` 按类型弹窗，新增一种标准件不需要改动套件对话框一行代码。

每个单端口标准件的表单里都有一对互斥单选按钮：**系数（coefficients）** 或 **测量文件（measurement）**。它对应 4.1 里说的双模式，切换到「系数」会调用 `clearMeasurement()` 清空 Touchstone——保证 `toS11()` 的分支不会歧义。

#### 4.3.2 核心流程

「新增一个标准件」的完整事件流：

```
用户点 Add ▾（QMenu，按 availableTypes() 动态生成六项）
  → Virtual::create(type)          # 工厂按类型 new
  → kit.addStandard(s)             # ID 查重 + 挂 SCPI 节点
  → updateStandardList()           # 刷新列表（getDescription = "类型, 名字"）
  → standards.back()->edit(回调)    # 立刻弹出该件的参数表单
       用户点 OK
  → accepted 信号：表单值写回成员变量 → 回调刷新列表
```

「修改一个标准件」：双击列表项 → `edit()` → OK 写回。注意写回用的是对话框 `accepted` 信号的回调（例如 [Calibration/calstandard.cpp:383-395](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L383-L395)），点 Cancel 不写——参数与预览天然分离。

#### 4.3.3 源码精读

**① 套件对话框的组装**

[Calibration/calkitdialog.cpp:57-72](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkitdialog.cpp#L57-L72)：Add 按钮的菜单不是手写六项，而是遍历 `availableTypes()` 动态生成——**将来新增标准件类型，这里零改动**。[Calibration/calkitdialog.cpp:76-81](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkitdialog.cpp#L76-L81)：双击委托编辑并绑定刷新回调。[Calibration/calkitdialog.cpp:35-53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkitdialog.cpp#L35-L53)：上移/下移直接 `swap` 向量元素后重挂 SCPI 名（呼应 4.2.3 ③ 的按位置命名）。

**② 标准件表单：以 Short 为例**

[Calibration/calstandard.cpp:330-398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L330-L398) 的 `Short::edit()`：先把成员值灌进表单（含 SI 单位控件），[第 357-362 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L357-L362) 监听「系数」单选按钮——一旦选中立即 `clearMeasurement()`（切回系数模式就放弃测量文件），[第 371-376 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L371-L376) 按当前模式决定哪个单选钮预选中。[第 383-395 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L383-L395) OK 时写回。Open/Load/Through 的 `edit()` 是同构的复制粘贴变体。

**③ Load 表单的接线顺序选择**

[Calibration/calstandard.cpp:495-499](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L495-L499)：Load 独有 `C_first`/`L_first` 两个单选钮，直接写 `Cfirst` 布尔——对应 4.1.3 ⑤ 的级联顺序语义。

**④ Line 表单的联动推导**

[Calibration/calstandard.cpp:878-881](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L878-L881)：TRL Line 表单里输入 delay 时，minFreq/maxFreq 输入框按 20°/160° 公式自动刷新——把 4.2.5 练习 3 的公式变成了 UI 联动。

**⑤ 套件对话框与校准的联动入口**

[Calibration/calkit.cpp:437-448](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L437-L448) 的 `Calkit::edit(updateCal)` 接受一个回调：VNA 模式传入的回调（[VNA/vna.cpp:122-126](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L122-L126)）会在套件参数变化时**立即重算校准**——如果你已经做过校准，改一个系数，所有误差项马上按新模型刷新。另注意第 445 行的 `AppWindow::showGUI()` 守卫：`--no-gui` 模式下不弹窗（呼应 u2-l1）。

#### 4.3.4 代码实践

**实践目标**：通过改一个参数观察「套件编辑 → 校准重算」的联动，以及系数/测量文件互斥行为。

**操作步骤**：

1. **纯阅读路径（无 GUI 也能做）**：在 [Calibration/calkitdialog.cpp:83-91](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkitdialog.cpp#L83-L91) 找到 Apply/OK 按钮的处理：两者都先 `parseEntries()`（把元信息写回套件）再 `emit settingsChanged()`。沿信号追到 [Calibration/calkit.cpp:441-443](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L441-L443)：`settingsChanged` 触发 VNA 传入的 `updateCal` 回调，即 `cal.compute()`。把这条链画成时序图。
2. **参数实验**（待本地验证，需要 GUI + 已完成一次 SOLT 校准）：打开 Edit Calibration Kit，把 Short 件的 delay 从 0 改成 50 ps，点 Apply。观察已校准 Trace 的 S11 相位是否立即整体旋转（50 ps 在 1 GHz 对应 \(360^\circ \times 10^9 \times 50\times10^{-12} = 18^\circ\)，二次通过偏移线故短路参考面移动约 \(2\times18^\circ = 36^\circ\)）。
3. **互斥实验**（待本地验证）：给 Open 件挂一份 Touchstone（Measurement 单选），再切回 Coefficients 单选，重新打开对话框确认测量信息标签回到 "No measurements stored yet"。

**需要观察的现象**：步骤 2 中不重测任何校准测量、只改系数，校准后曲线立刻变化——证明误差项求解与标准件模型是解耦的两步（本讲管模型，下一讲管求解）。步骤 3 中切换模式即清空测量——对应 4.3.1 的互斥设计。

**预期结果**：时序图链路为 `OK/Apply → parseEntries → settingsChanged → updateCal() → cal.compute()`。GUI 侧两步实验的具体数值待本地验证；delay 影响的相位量级可按上式预算。

#### 4.3.5 小练习与答案

**练习 1**：为什么「系数/测量文件」必须互斥？如果允许同时存在，`toS11()` 会出现什么问题？

**答案**：`toS11()` 的分支是 `if(touchstone) {...} else {...}`，Touchstone 优先。若允许两者同时存在，用户在系数页改参数会毫无效果（被测量文件覆盖），界面显示与计算结果背离。所以 UI 在切回系数页时主动 `clearMeasurement()`，让状态与分支一致。

**练习 2**：各标准件的 `setupSCPI()` 给每个系数参数都挂了回调 `[=](){clearMeasurement();}`（如 [Calibration/calstandard.cpp:427-433](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L427-L433)），这个回调什么时候触发、为什么需要它？

**答案**：通过 SCPI（如 `KIT:STAndard1:L0 1e-10`）远程写系数时触发。它与 GUI 的互斥单选按钮殊途同归：一旦改了系数，旧测量文件就不再代表该标准件，必须清空，否则 `toS11()` 仍走测量分支、刚写入的系数被无视。GUI 与 SCPI 两个入口都要维护同一条不变量。

**练习 3**（进阶，代码审读）：阅读旧格式迁移代码 [Calibration/calkit.cpp:366-423](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calkit.cpp#L366-L423)，找出至少两处「疑似复制粘贴遗留」的可疑写法。

**答案**：至少有三处：(a) 第 378 行 female Open 的测量被 `setMeasurement` 到 `open_m` 而不是 `open_f`；(b) 第 395 行 female Short 同样设到了 `short_m`；(c) 第 408 行构造 female Load 时误用 `SOLT.load_m.Z0` 与 `SOLT.load_m.Cparallel`（应为 `load_f`）。这段代码只在加载「男女分头」的老式套件时才执行，平时不触发——读历史迁移代码时保持怀疑是源码阅读的基本功。

## 5. 综合实践

**任务：为一只自制 50 Ω 负载建模、录入、存档并手算验证。**

场景：你手焊了一个 SMA 接头 + 49.9 Ω 0402 贴片电阻的负载。没有厂家数据，要给它建一个够用的近似模型。步骤：

1. **建模**（把物理直觉翻译成参数）：
   - 电阻本体：`resistance = 49.9`；
   - 焊盘与引线的串联电感：`Lseries = 0.1 nH`（0402 封装的典型量级）；
   - 焊盘对地寄生电容：`Cparallel = 0`（先忽略，留作敏感性分析）；
   - SMA 连接器的偏移段：`Z0 = 50`、`delay = 10 ps`、`loss = 0`；
   - 接线顺序：焊盘电容靠近端口 → `Cfirst = true`。
2. **录入**（待本地验证，需要 GUI）：Edit Calibration Kit → Add → Load，双击录入上述参数，Save 为 `homemade.calkit`。
3. **手算 1 GHz 的理论 S11**（对照 [Calibration/calstandard.cpp:446-469](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L446-L469) 逐步验算）：
   - 终端阻抗：\(Z_L = 49.9 + j\,2\pi \times 10^9 \times 10^{-10} = 49.9 + j0.628\,\Omega\)（`Cfirst=true` 故先加串联电感，`Cparallel=0` 跳过并联）；
   - 反射系数：\(\Gamma_T = \frac{-0.1 + j0.628}{99.9 + j0.628} \approx 3.9\times10^{-5} + j6.28\times10^{-3}\)，幅度 \(\approx 6.28\times10^{-3}\)，即 **−44 dB**；
   - 偏移线：10 ps 在 1 GHz 只旋转 \(3.6^\circ\) 相位，幅度不变；
   - 结论：\(S_{11} \approx -44\,\mathrm{dB} \angle \sim 90^\circ\)。
4. **敏感性分析**（纯手算）：把 `Lseries` 翻倍到 0.2 nH，\(\omega L = 1.26\,\Omega\)，\(|\Gamma|\) 升到约 \(1.26\times10^{-2}\) 即 −38 dB——问：这说明回波损耗对引线电感敏感还是对电阻阻值敏感？再单独看电阻项：阻值偏离 50 Ω 达 0.2 Ω（即 resistance = 49.8）时 \(|\Gamma| \approx 0.2/100 = 2\times10^{-3}\)，约 −54 dB，仍被电感项（\(6.3\times10^{-3}\)，−44 dB）淹没。**结论**：1 GHz 附近，自制负载的回波损耗瓶颈是寄生电感而非电阻精度；且电感项随频率线性增长，频率越高越是如此。
5. **反思**：同一只负载，若当厂家的「标准 Load」用于 SOLT，−44 dB 的模型误差会直接进入误差项；这就是为什么 SOLT 的 Load 只用于「方向性/源匹配」项，而反射标准用 Open/Short（它们 \(|\Gamma|=1\)，对寄生参数远不敏感）。

无 GUI 时步骤 2 的替代：手写 `homemade.calkit` JSON（对照 4.2.2 的结构），其余步骤全部可以纸面完成。

## 6. 本讲小结

- 校准件模型是「**偏移传输线（Z0/delay/loss）+ 非理想终端**」：Open 终端是边缘电容 \(C_0..C_3\)（fF 起）、Short 是串联电感 \(L_0..L_3\)（pH 起）、Load 是 R+串联L+并联C 网络、Through 的偏移线本身就是标准。参考阻抗在整个模型里硬编码 50 Ω，`Z0` 只是偏移线的特征阻抗。
- 类层次按端口数分流：`Virtual → OnePort(toS11) / TwoPort(toSparam)`，六种具体标准；TRL 的 `Reflect`/`Line` 故意返回 NaN——TRL 算法不需要绝对理论值，`Line` 的可用频段由 20°~160° 电长度约束。
- 每个标准件有**系数**与**Touchstone 测量文件**两种互斥描述，测量优先；GUI 单选钮切换与 SCPI 写系数回调都会 `clearMeasurement()` 维护这条不变量。
- `Calkit` 是容器 + JSON 序列化器 + SCPI 子树三合一：`{type, params}` 打头的多态序列化、64 位随机 ID 查重、按位置重排的 SCPI 命名，以及三代历史文件格式的自动迁移。
- 编辑对话框两层分工：套件层（列表增删移、文件读写）通过工厂 + 虚函数 `edit()` 委托给各标准件表单，新增类型对套件对话框零侵入；改系数会经 `settingsChanged` 立即触发已有校准重算。

## 7. 下一步学习建议

本讲解决了「理论值 \(S_\mathrm{actual}\) 从哪来」。下一讲 **u9-l2 校准求解器** 将解决另一半：`CalibrationMeasurement` 的 `getMeasured()`（实际读数，含插值与频率范围守卫）与 `getActual()`（本讲的理论值）如何配对，进入 [Calibration/calibration.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp) 求解 12 项误差模型。建议先自己读一遍 [Calibration/calibration.cpp:776-835](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L776-L835)——那里出现的每一个 `getActual()` 调用，喂进去的都是本讲逐行读过的公式。若对单位换算还不熟，可回看 u5-l3 的设备级校准（那是另一层「让设备说真话」的校准，与本讲的 SOLT 职责正交）。
