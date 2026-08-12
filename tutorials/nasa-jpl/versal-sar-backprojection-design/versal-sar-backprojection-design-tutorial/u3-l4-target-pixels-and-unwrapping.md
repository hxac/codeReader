# 目标像素生成与方位角解卷绕

## 1. 本讲目标

本讲紧接 u3-l3「从 CSV 读取雷达数据」。在上一讲里，我们把 slowtime（天线几何）和 RC（距离压缩回波）两类文本数据解析进了 device buffer。但反投影要算的是「每个目标像素的回波」，于是主机还需要先**生成一张目标像素网格**：告诉 AIE「这一帧要在地面上哪些 (X, Y, Z) 点上成像」。

学完本讲你应该能够：

1. 说清为什么 `atan2` 给出的天线方位角需要「解卷绕（unwrap）」，以及 `unwrap()` 是怎么做的。
2. 掌握由方位角序列推导方位分辨率 `az_res`、半场景宽度 `half_az_width`、以及 `dr`/`dx` 分辨率的公式来源。
3. 明白 `genTargetPixels()` 如何按 `(pulse_idx, rng_idx)` 二重循环把目标像素排布成 PULSES 行 × RC_SAMPLES 列的网格，并写进 `m_xyz_px_array`。

本讲只涉及主机侧（ARM Cortex-A72）的纯 C++ 数值计算，不调用任何 XRT/AIE 接口；生成的像素数组将在 u3-l5 被 GMIO 送进 AIE。

## 2. 前置知识

### 2.1 atan2 与角度的分支切割

`std::atan2(y, x)` 返回向量 (x, y) 相对 X 轴的方位角，取值范围为 \((-\pi, \pi]\)。它在圆周上是「连续」的，但取值被强制压回主值区间：当一个真实的物理方位角越过 \(\pm\pi\)（即越过负 X 轴）时，`atan2` 的返回值会从 \(+\pi\) 突然跳到 \(-\pi\)，产生一个 \(2\pi\) 的人工跳变。这个跳变不是物理的，只是数学表达的「卷绕（wrap）」。

### 2.2 聚束模式 SAR 的方位分辨率

在聚束模式（spotlight）SAR 中，天线在采集过程中持续照射同一块场景，等效合成孔径的张角叫**总方位孔径** \(\Delta\varphi\)（本讲记作 `total_az`）。方位（cross-range）分辨率近似为：

\[
\rho_a = \frac{\lambda}{2\,\Delta\varphi} = \frac{C}{2\,\Delta\varphi\, f}
\]

其中 \(\lambda\) 是波长、\(C\) 是光速、\(f\) 是载频（本仓库用 `MIN_FREQ`）。孔径越大，方位分辨率越高（数值越小）。这是后面 `az_res`、`dx` 的公式来源。本节只需记住：「方位分辨率由总方位孔径 `total_az` 决定」，而 `total_az` 是否正确，完全取决于角度序列有没有先解卷绕。

### 2.3 需要用到的 common.h 常量

本讲会反复用到 `design/common.h` 里的这些量（u1-l4 已详细讲过）：

- `PULSES = 602`、`RC_SAMPLES = 512`：图像行数与列数。
- `BC_ELEMENTS = 4`：slowtime 每行的列数，依次是天线 X、Y、Z、参考距离 `ref_range`。
- `C`、`MIN_FREQ`、`RANGE_FREQ_STEP`：雷达物理常数。
- `RANGE_WIDTH = C/(2·RANGE_FREQ_STEP)`、`RANGE_RES = RANGE_WIDTH/RC_SAMPLES`：距离向场景宽度与距离分辨率。
- `HALF_RANGE_SAMPLES = RC_SAMPLES/2 = 256`：用于把距离坐标居中。
- `PI`、`TWO_PI`：圆周率常量。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [design/host/sar_backproject.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp) | 本讲主战场：`unwrap()`（L112–L133）与 `genTargetPixels()`（L217–L270）都在这里。 |
| [design/host/sar_backproject.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h) | `SARBackproject` 类声明；成员 `m_xyz_px_array`、`m_broadcast_data_array` 等的定义。 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 上述雷达常数与规模宏。 |

> 说明：下面给出的「默认配置下的量级估算」（如方位分辨率约 0.18 m）是基于 `common.h` 默认宏与约 5° 孔径的纸面推算，用于帮助理解量级；真实数值取决于 GOTCHA 数据，**待本地验证**。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：先讲 `unwrap()` 怎么得到连续方位角；再讲由方位角推导分辨率与场景宽度的公式；最后讲目标像素网格如何生成。

### 4.1 unwrap() 角度解卷绕

#### 4.1.1 概念说明

`genTargetPixels()` 需要先知道天线在采集这 602 个脉冲时，方位角一共扫过了多大范围（`total_az`）。方位角由 `atan2(Y_ant, X_ant)` 得到。问题在于：若这段扫掠恰好越过 \(\pm\pi\) 的分支切割，原始 `atan2` 序列会出现 \(2\pi\) 的虚假跳变，导致：

- `total_az = max - min` 被算成接近 \(2\pi\)（约 360°）而不是真实的小角度；
- 相邻角的平均步进 `delta_az` 也被跳变污染。

二者一旦错了，后续所有分辨率与场景宽度全错。**解卷绕（unwrap）** 就是把这些人工 \(2\pi\) 跳变「补回来」，还原成物理上连续的角度序列。

#### 4.1.2 核心流程

`unwrap()` 的思想是经典的「相位解卷绕」：对相邻两个角度，先把它们的差值折叠回主值区间 \([-\pi, \pi)\)，再把折叠后的差值累加到前一个**已解卷绕**的角度上。

设原始角度序列为 \(a_i\)，解卷绕后为 \(u_i\)，则：

\[
\Delta_i = a_i - a_{i-1}^{\text{orig}}
\]

\[
\Delta'_i = \big((\Delta_i + \pi) \bmod 2\pi\big) - \pi \quad \in [-\pi, \pi)
\]

\[
u_i = u_{i-1} + \Delta'_i, \qquad u_0 = a_0
\]

关键细节：计算差值 \(\Delta_i\) 要用**原始**的前一角 \(a_{i-1}^{\text{orig}}\)（代码里的 `prev_orig`），而累加要用**已解卷绕**的 \(u_{i-1}\)（代码里的 `angles[i-1]`，因为数组是原地改写的）。两者绝不能混用，否则跳变会被引入差值里。

折叠那一步用 `fmod` 实现，但 `fmod` 的结果符号跟随被除数，可能为负，所以代码额外加了一段归一化（见源码 L121–L124），把 `dp` 规范到 \([-\pi, \pi)\)；并对 `dp == -π` 且 `diff > 0` 的边界做了方向修正（L126–L127），保证累加方向正确。

#### 4.1.3 源码精读

完整的 `unwrap()`：

[design/host/sar_backproject.cpp:112-133](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L112-L133) —— 整个解卷绕函数，对长度为 `PULSES` 的角度数组**原地**改写。

几个关键点：

- [design/host/sar_backproject.cpp:114](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L114)：`prev_orig = angles[0]` 保存「原始」前一角度，专供差值计算使用。
- [design/host/sar_backproject.cpp:119](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L119)：`diff = current_orig - prev_orig`，两个都是**原始**值。
- [design/host/sar_backproject.cpp:121-124](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L121-L124)：把 `diff` 折叠到 \([-\pi, \pi)\)（先映射到 \([0, 2\pi)\) 再减 \(\pi\)）。
- [design/host/sar_backproject.cpp:129](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L129)：`angles[i] = angles[i-1] + dp`，这里 `angles[i-1]` 已经是**解卷绕后**的值，完成累加。
- [design/host/sar_backproject.cpp:131](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L131)：`prev_orig = current_orig` 更新为原始值，为下一轮差值做准备。

`PI`、`TWO_PI` 来自 [design/common.h:48-49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L48-L49)。

#### 4.1.4 代码实践

**实践目标**：用 Python/NumPy 复刻 `unwrap()` 的逻辑，亲眼看到「不解卷绕」会让 `total_az` 错成什么样。

**操作步骤**：把下面这段「示例代码」保存为 `unwrap_demo.py` 并运行（仅依赖 numpy）。

```python
# 示例代码：复刻 design/host/sar_backproject.cpp 的 unwrap()
import numpy as np

PI = 3.1415926535898
TWO_PI = 6.2831853071796
PULSES = 602

# 1) 模拟天线在一段约 5 度的圆弧上运动，刻意让方位角越过 +-pi 分支切割
#    起点 178 度 -> 终点 183 度，raw atan2 会从 +pi 跳到 -pi
deg = np.linspace(178.0, 183.0, PULSES)   # 与 GOTCHA ~5 度孔径同量级
rad = np.deg2rad(deg)
R = 1000.0
x_ant, y_ant = R*np.cos(rad), R*np.sin(rad)

# 2) raw atan2，每点都在 (-pi, pi]
angles = np.arctan2(y_ant, x_ant)

# 3) 逐字复刻 C++ 的 unwrap()
def unwrap(a):
    a = a.astype(np.float64).copy()
    prev_orig = a[0]
    for i in range(1, len(a)):
        current_orig = a[i]
        diff = current_orig - prev_orig
        dp = np.mod(diff + PI, TWO_PI)      # numpy.mod 总是非负，等价于 fmod+负值修正
        dp = dp - PI
        if dp == -PI and diff > 0:
            dp = PI
        a[i] = a[i-1] + dp
        prev_orig = current_orig
    return a

unwrapped = unwrap(angles)

def stats(a):
    delta_az = abs(np.mean(np.diff(a)))
    total_az = np.max(a) - np.min(a)
    return delta_az, total_az

d_raw, t_raw   = stats(angles)
d_un,  t_un    = stats(unwrapped)
print(f"raw      : delta_az={np.rad2deg(d_raw):.4f} deg, total_az={np.rad2deg(t_raw):.4f} deg")
print(f"unwrapped: delta_az={np.rad2deg(d_un):.4f} deg, total_az={np.rad2deg(t_un):.4f} deg")
print(f"np.unwrap对照 total_az={np.rad2deg(np.ptp(np.unwrap(angles))):.4f} deg")
```

**需要观察的现象**：

- `raw` 一行的 `total_az` 会是约 **357 度**（被分支切割污染），而不是真实孔径；
- `unwrapped` 一行的 `total_az` 约为 **5 度**，与 `np.unwrap` 的对照值一致。

**预期结果**：解卷绕前 `total_az ≈ 357°`，解卷绕后 `total_az ≈ 5°`。若你把起点改成远离 \(\pm\pi\) 的位置（例如 10°→15°），则 raw 与 unwrapped 会一致——这说明 `unwrap()` 对「恰好不跨越切割」的序列是无害的安全网，对「跨越切割」的序列则是必需的修正。

**若无法运行**：标注「待本地验证」，但可以根据上面公式手算验证：178° 与 183° 的 raw atan2 分别约为 +178° 与 −177°，max−min ≈ 355°–357°。

#### 4.1.5 小练习与答案

**练习 1**：若某段方位角序列单调递增且全程不跨越 \(\pm\pi\)，`unwrap()` 会改变它的数值吗？那它还有必要调用吗？

> **参考答案**：不会改变数值——每个 `diff` 本来就在 \([-\pi,\pi)\)，`dp=diff`，累加后与原值一致。但 `unwrap()` 仍是必要的安全网：真实数据的扫描起点是不可控的，无法保证一定不跨越分支切割，去掉它就埋下了「偶发性错误结果」的雷。

**练习 2**：代码里 `prev_orig` 与 `angles[i-1]` 各保存什么？为什么不能用同一个变量？

> **参考答案**：`prev_orig` 保存**原始**的前一角度，专门用于算 `diff`（差值必须基于原始值才能正确识别 \(2\pi\) 跳变）；`angles[i-1]` 在循环中已被改写成**解卷绕后**的值，用于累加。若混用，等于把已经补过 \(2\pi\) 的值再放进差值里，跳变会被错误地二次计入。

---

### 4.2 az_res / half_az_width / 分辨率公式

#### 4.2.1 概念说明

有了连续的方位角序列，就能算两个核心量：

- **平均角步进** `delta_az`：相邻脉冲方位角的平均间隔，反映平台转动的「角速度」。
- **总方位孔径** `total_az`：整段采集期内天线扫过的总角度，决定方位分辨率。

由此派生出：

- **方位分辨率** `az_res = C/(2·total_az·MIN_FREQ)`：即前置知识里的聚束 SAR 方位分辨率公式 \(\rho_a = C/(2\Delta\varphi f)\)。它既是分辨率，也被直接用作目标网格里 Y 方向的像素间距。
- **半场景宽度** `half_az_width`：用来把 Y 坐标关于 0 对称的半宽度。

#### 4.2.2 核心流程

`genTargetPixels()` 上半段（L218–L242）的推导链：

1. 对每个脉冲算原始方位角 \(a_i = \text{atan2}(Y_i, X_i)\)。
2. 调 `unwrap(a)` 得到连续序列。
3. `mean_diff = mean(a[i] - a[i-1])`，`delta_az = |mean_diff|`。
4. `total_az = max(a) - min(a)`。
5. `az_res = C/(2·total_az·MIN_FREQ)`。
6. `az_width = C/(2·delta_az·MIN_FREQ)`，`half_az_width = az_width/2`。

注意两个容易混淆的点：

- `az_res` 用 `total_az`（总孔径）做分母 → 得到分辨率（小）；
- `az_width` 用 `delta_az`（单步角）做分母 → 得到一个大得多的「宽度」量，因为 \(\text{delta\_az} \approx \text{total\_az}/(PULSES-1)\)，所以 \(\text{az\_width} \approx (PULSES-1)\cdot\text{az\_res}\)，于是 `half_az_width` 恰好把网格 Y 轴居中（详见 4.3）。

随后代码还计算并打印了场景最大宽度与分辨率（L245–L254）：

\[
\text{max\_wr} = \frac{C}{2\cdot\text{RANGE\_FREQ\_STEP}} = \text{RANGE\_WIDTH} \quad (\text{距离向场景宽度})
\]

\[
\text{max\_wx} = \frac{C}{2\cdot\text{delta\_az}\cdot\text{MIN\_FREQ}} = \text{az\_width} \quad (\text{方位向场景宽度})
\]

\[
\text{dx} = \frac{C}{2\cdot\text{total\_az}\cdot\text{MIN\_FREQ}} = \text{az\_res} \quad (\text{方位分辨率})
\]

距离分辨率 `dr` 稍特殊：

\[
\text{dr} = \frac{C}{2\cdot\text{RANGE\_FREQ\_STEP}\cdot 424}
\]

这里的 `424` 来自 GOTCHA 数据集「512 个 RC 样本中只有 424 个是有效带宽样本」（见测试数据文件名 `gotcha_phdata_512-out-of-424-rc-samples_*`）。因此 `dr` 是「按有效样本数计算的真实距离分辨率」，仅用于打印展示。

> ⚠️ 一个值得注意的细节：**实际网格 X 间距用的是 `RANGE_RES`（除以 512），而不是展示用的 `dr`（除以 424）**。也就是说距离向网格是过采样的——网格间距（`RANGE_RES ≈ 0.20 m`）比真实分辨率（`dr ≈ 0.24 m`）更细；而方位向网格间距 `az_res` 与分辨率 `dx` 是 1:1 一致的。`dr` 这个量并不参与像素生成，只出现在 `printf` 里。

**默认配置下的量级估算（待本地验证）**：取 `total_az ≈ 5° ≈ 0.0873 rad`、`delta_az ≈ total_az/(PULSES-1)`、`C=2.9979e8`、`MIN_FREQ=9.288e9`、`RANGE_FREQ_STEP=1.4713e6`：

- `az_res = dx ≈ 0.185 m`
- `az_width = max_wx ≈ 111 m`，`half_az_width ≈ 55.6 m`
- `RANGE_WIDTH = max_wr ≈ 101.9 m`，`RANGE_RES ≈ 0.199 m`
- `dr ≈ 0.240 m`

#### 4.2.3 源码精读

方位角与解卷绕：

- [design/host/sar_backproject.cpp:225-229](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L225-L229) —— 对每个脉冲算 `atan2(m_broadcast_data_array[BC_ELEMENTS*i + 1], m_broadcast_data_array[BC_ELEMENTS*i])`，即 `atan2(Y_ant, X_ant)`，再调 `this->unwrap(az_ant)`。
  - 索引 `BC_ELEMENTS*i + 0` 是天线 X、`+1` 是天线 Y（对应 [design/common.h:40-45](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L40-L45) 的 BC_ELEMENTS 注释）。

步进、孔径与分辨率：

- [design/host/sar_backproject.cpp:230-235](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L230-L235) —— `sum_diff` 累加相邻角差，除以 `PULSES-1` 得 `mean_diff`，取绝对值得 `delta_az`。
- [design/host/sar_backproject.cpp:236-241](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L236-L241) —— `min/max` 求 `total_az`；随后算 `az_res` 与 `half_az_width`。

打印用的场景宽度与分辨率：

- [design/host/sar_backproject.cpp:245-254](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L245-L254) —— 计算 `max_wr/max_wx/dr/dx` 并 `printf`。注意 `delta_az`、`total_az` 在这里被复用。

`C`、`MIN_FREQ`、`RANGE_FREQ_STEP` 见 [design/common.h:53-55](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L53-L55)；`RANGE_WIDTH`、`RANGE_RES` 见 [design/common.h:56-57](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L56-L57)。

> 边界保护：以上推导都在 `if (PULSES != 1)` 内（[L223](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L223)），避免 `PULSES-1 == 0` 时除零。若 `PULSES == 1`，`az_res`/`half_az_width` 保持 0，但 `delta_az`、`total_az` 在 L246/L250 仍会被使用——这是单脉冲下的退化情形，默认配置不会触发。

#### 4.2.4 代码实践

**实践目标**：亲手算出默认配置下的分辨率，并对比「忘记解卷绕」的后果。

**操作步骤**：

1. 在 4.1.4 的 Python 脚本末尾追加下面这段「示例代码」：

```python
# 示例代码：用解卷绕前后的 total_az 分别算 az_res
C = 299792458.0
MIN_FREQ = 9288080400.0

def az_res(total_az):
    return C / (2.0 * total_az * MIN_FREQ)

print(f"az_res(正确的 total_az={np.rad2deg(t_un):.2f}deg): {az_res(t_un):.4f} m")
print(f"az_res(错误的 total_az={np.rad2deg(t_raw):.2f}deg): {az_res(t_raw):.4f} m")
```

2. 运行并对比两个 `az_res`。

**需要观察的现象**：用错误 `total_az`（约 357°）算出的 `az_res` 会是约 **0.0026 m**（荒谬地小）；用正确 `total_az`（约 5°）算出的约 **0.185 m**（合理）。

**预期结果**：一个荒谬的极小值 vs 一个合理的米级分辨率，直观说明 `unwrap()` 对分辨率的决定性影响。

**若无法运行**：标注「待本地验证」，可用上面的手算公式验证量级。

#### 4.2.5 小练习与答案

**练习 1**：用默认配置（`total_az ≈ 5°`、`MIN_FREQ = 9.288 GHz`）估算 `az_res`。

> **参考答案**：\(\text{az\_res} = 2.9979\times10^8 / (2 \times 0.0873 \times 9.288\times10^9) \approx 0.185\ \text{m}\)。

**练习 2**：若忘记解卷绕导致 `total_az` 被算成约 357°，`az_res` 会变成多少？这会导致什么后果？

> **参考答案**：约 0.0026 m。后果是网格 Y 方向间距被设得极小、`half_az_width` 也跟着错乱，生成的目标像素会挤在一个错误到离谱的微小区域里，最终图像完全失真——而且程序不会报错。

**练习 3**：`half_az_width` 为什么用 `delta_az`（单步角）而不是 `total_az`（总孔径）来推？

> **参考答案**：因为 `half_az_width = az_width/2 = C/(4·delta_az·MIN_FREQ)`，而 `delta_az ≈ total_az/(PULSES-1)`，于是 `half_az_width ≈ (PULSES-1)·az_res/2`。这样 Y 坐标 `az_res·pulse_idx - half_az_width` 在 `pulse_idx=0` 和 `pulse_idx=PULSES-1` 时近似为 \(\mp\text{half\_az\_width}\)，让场景关于 0 对称。若改用 `total_az` 推，宽度会错配 Y 的实际跨度。

---

### 4.3 xyz_px_array 网格生成

#### 4.3.1 概念说明

算完分辨率与场景宽度，主机要在地面平面（Z=0）上铺一张二维目标像素网格，供 AIE 逐像素做反投影。网格大小就是输出图像尺寸：`PULSES` 行（方位/Y）× `RC_SAMPLES` 列（距离/X）。每个像素存 3 个 float：(X, Y, Z)。整张网格被写入构造函数已经映射好的 `m_xyz_px_array`（类型 `float*`，容量 `PULSES*RC_SAMPLES*3`，见 [design/host/sar_backproject.cpp:36-37](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L36-L37)）。

#### 4.3.2 核心流程

二重循环（外层 `pulse_idx`，内层 `rng_idx`）按行优先生成：

\[
X_{j} = (j - \text{HALF\_RANGE\_SAMPLES})\cdot\text{RANGE\_RES}, \quad j \in [0, \text{RC\_SAMPLES})
\]

\[
Y_{i} = \text{az\_res}\cdot i - \text{half\_az\_width}, \quad i \in [0, \text{PULSES})
\]

\[
Z = 0
\]

要点：

- **X（距离向）**：以 `rng_idx - 256` 居中，乘距离分辨率 `RANGE_RES`。所以 X 从 \(-256\cdot\rho_r\) 走到 \(+255\cdot\rho_r\)，关于 0 对称。
- **Y（方位向）**：`az_res·pulse_idx - half_az_width`，用 4.2 推得的 `half_az_width` 居中，使首末像素近似为 \(\mp\text{half\_az\_width}\)。
- **Z 恒为 0**：本设计在地面平面成像（二维），所有目标同高。
- **内存排布**：外层 `pulse_idx`（行）是慢索引，内层 `rng_idx`（列）是快索引，连续写 3 个 float。这正是行优先（row-major）排布，对应输出图像「PULSES 行 × RC_SAMPLES 列」。这也决定了 u3-l5 里 `bp()` 如何按连续像素块把网格切片分给各个 AIE switch：`px_per_demux_kern = PULSES*RC_SAMPLES/AIE_SWITCHES`，每个 switch 取走连续的一段。

伪代码：

```
idx = 0
for pulse_idx in 0..PULSES-1:        # 行(方位 Y)
    for rng_idx in 0..RC_SAMPLES-1:  # 列(距离 X)
        m_xyz_px_array[idx++] = (rng_idx - 256) * RANGE_RES   # X
        m_xyz_px_array[idx++] = az_res*pulse_idx - half_az_width  # Y
        m_xyz_px_array[idx++] = 0.0                             # Z
```

#### 4.3.3 源码精读

网格生成二重循环：

- [design/host/sar_backproject.cpp:256-269](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L256-L269) —— 双层循环填 `m_xyz_px_array`。
  - [L261](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L261)：X = `(rng_idx-HALF_RANGE_SAMPLES)*RANGE_RES`。
  - [L264](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L264)：Y = `az_res*pulse_idx - half_az_width`。
  - [L267](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L267)：Z = `0.0`。

宏来源：`HALF_RANGE_SAMPLES`、`RANGE_RES` 见 [design/common.h:57-59](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L57-L59)。

缓冲与映射（在构造函数里早已备好，本函数只负责填数据）：

- [design/host/sar_backproject.cpp:36-37](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L36-L37) —— `m_xyz_px_buffer` 大小为 `PULSES*RC_SAMPLES*sizeof(float)*3`，`m_xyz_px_array` 是它的 `float*` 映射。网格生成的 X/Y/Z 顺序写入这里。

> 与 u3-l5 的衔接：`bp()` 里把这块 buffer 按每个 switch 切 `px_per_demux_kern` 个像素（即 `px_per_demux_kern*3` 个 float）经 GMIO 送进 AIE（[L300-L305](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L300-L305)）。因为本函数是按 pulse-major 连续写的，switch 拿到的就是一段连续的「整行/部分行」像素块。

#### 4.3.4 代码实践

**实践目标**：用 NumPy 复现这张目标像素网格，验证它的形状、居中性与规模。

**操作步骤**：运行下面这段「示例代码」（接在 4.1.4 的常量之后）：

```python
# 示例代码：复现 genTargetPixels() 的目标像素网格
PULSES = 602
RC_SAMPLES = 512
HALF_RANGE_SAMPLES = RC_SAMPLES // 2
RANGE_FREQ_STEP = 1471301.6
C = 299792458.0
RANGE_WIDTH = C / (2.0 * RANGE_FREQ_STEP)
RANGE_RES = RANGE_WIDTH / RC_SAMPLES
az_res = 0.185   # 用 4.2 估算的值
half_az_width = 55.6

# 复现行优先排布：外层 pulse, 内层 rng, 连续写 X,Y,Z
xyz = np.empty((PULSES, RC_SAMPLES, 3), dtype=np.float32)
for pulse_idx in range(PULSES):
    for rng_idx in range(RC_SAMPLES):
        xyz[pulse_idx, rng_idx, 0] = (rng_idx - HALF_RANGE_SAMPLES) * RANGE_RES
        xyz[pulse_idx, rng_idx, 1] = az_res * pulse_idx - half_az_width
        xyz[pulse_idx, rng_idx, 2] = 0.0

print("网格形状(脉冲, 距离, XYZ):", xyz.shape)
print("X 范围:", xyz[...,0].min(), "~", xyz[...,0].max())
print("Y 范围:", xyz[...,1].min(), "~", xyz[...,1].max())
print("是否关于 0 对称: X", np.isclose(xyz[...,0].min(), -xyz[...,0].max(), atol=RANGE_RES))
```

**需要观察的现象**：

- 形状为 `(602, 512, 3)`，总元素 `602*512*3 = 924672` 个 float；
- X 关于 0 近似对称（最小值约为 \(-256\cdot\rho_r\)，最大值约为 \(+255\cdot\rho_r\)）；
- Y 首行约 \(-55.6\)，末行约 \(+55.6\)，同样关于 0 近似对称。

**预期结果**：形状 `(602, 512, 3)`，X、Y 都关于 0 对称，Z 全为 0。

**若无法运行**：标注「待本地验证」；形状与对称性可由公式直接推出。

#### 4.3.5 小练习与答案

**练习 1**：这张网格一共有多少个目标像素？每个像素几个 float？总共多大（float）？

> **参考答案**：`PULSES·RC_SAMPLES = 602·512 = 308224` 个像素；每个像素 3 个 float（X/Y/Z）；共 `308224·3 = 924672` 个 float（约 3.5 MiB）。

**练习 2**：为什么 Z 恒为 0？

> **参考答案**：本设计在地面的二维平面（Z=0）上成像，假定所有目标处于同一高度，所以第三维固定为 0，反投影只在 X-Y 网格上进行。

**练习 3**：外层循环是 `pulse_idx` 还是 `rng_idx`？这决定了什么？

> **参考答案**：外层是 `pulse_idx`（行/方位 Y），内层是 `rng_idx`（列/距离 X）。这决定了内存是 **pulse-major（行优先）** 排布：`pulse_idx` 是慢索引、`rng_idx` 是快索引。对应输出图像是 PULSES 行 × RC_SAMPLES 列，也决定了 `bp()` 里按连续像素块切给各 AIE switch 的方式。

---

## 5. 综合实践

把三个模块串起来：在本地用 Python 完整复现「方位角 → 解卷绕 → 分辨率 → 目标像素网格」的全流程，并回答一个总问题——**若删掉解卷绕这一步，最终的 `m_xyz_px_array` 会错成什么样？**

建议步骤：

1. 用 4.1.4 的脚本生成一段越过 \(\pm\pi\) 分支切割的方位角序列，得到 `raw` 与 `unwrapped` 两组角度。
2. 分别用这两组角度走 4.2 的公式算出 `delta_az / total_az / az_res / half_az_width`。
3. 分别用这两套参数走 4.3.4 的脚本生成两张目标像素网格。
4. 对比两张网格的 **Y 坐标范围**：正确网格 Y 应在约 \([-55, +55]\) 米；错误网格 Y 会被压到一个荒谬的极小范围（因为 `az_res` 被算成了约 0.0026 m）。
5. 写一段结论：解卷绕是如何通过 `total_az → az_res` 这条链，最终决定整张像素网格的物理尺度的。

进阶（选做）：阅读 [design/host/sar_backproject.cpp:300-305](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L300-L305)，说明本讲生成的 `m_xyz_px_array` 是如何按 `px_per_demux_kern` 被切成 `AIE_SWITCHES` 段送进 AIE 的，并解释「为什么 pulse-major 排布能让每个 switch 拿到连续的一段像素」。

## 6. 本讲小结

- `genTargetPixels()` 的第一步是把每个脉冲的天线位置用 `atan2(Y, X)` 转成方位角，再调 `unwrap()` 把 \(\pm\pi\) 处的人工 \(2\pi\) 跳变补回，得到物理上连续的角度序列。
- `unwrap()` 的关键细节：差值用「原始」前角 `prev_orig`，累加用「已解卷绕」的 `angles[i-1]`，二者不能混用；差值先折叠到 \([-\pi,\pi)\) 再累加。
- 由连续角度序列算出平均步进 `delta_az` 与总孔径 `total_az`，进而推出方位分辨率 `az_res = C/(2·total_az·MIN_FREQ)` 与半场景宽度 `half_az_width`；`total_az` 是否正确完全取决于解卷绕。
- 目标像素网格按外层 `pulse_idx`（方位行）、内层 `rng_idx`（距离列）行优先生成：`X=(rng_idx-256)·RANGE_RES`、`Y=az_res·pulse_idx-half_az_width`、`Z=0`，共 `PULSES·RC_SAMPLES` 个像素写入 `m_xyz_px_array`。
- 展示用的距离分辨率 `dr` 用 424（有效样本数）做分母，而实际 X 网格间距用 `RANGE_RES`（512 做分母），距离向是过采样的；方位向网格间距与分辨率 1:1。
- 本函数只做主机侧纯数值计算、不碰 XRT/AIE；其产物 `m_xyz_px_array` 将在 u3-l5 被 GMIO 切片送进 AIE 图。

## 7. 下一步学习建议

- **下一讲 u3-l5「用 XRT 编排 AIE 图与 PL 内核」**：本讲生成的 `m_xyz_px_array`（连同 slowtime、RC buffer）会在那里被 `bp()` 经 GMIO async 逐脉冲投递进 AIE，并由 RTP 控制末脉冲 dump、由 PL 包路由器写回 DDR。重点关注 `px_per_demux_kern` 如何把本讲的 pulse-major 网格切成 `AIE_SWITCHES` 段。
- **建议阅读的源码**：回头看 [design/host/sar_backproject.cpp:279-335](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L279-L335) 的 `bp()`，把「像素如何被消费」和本讲「像素如何被生产」对照起来。
- **延伸阅读**：聚束 SAR 方位分辨率公式 \(\rho_a = \lambda/(2\Delta\varphi)\) 的推导可参考任何 SAR 成像教材（如 Cumming & Wong《合成孔径雷达成像算法》）；理解后你会更清楚为什么 `total_az` 是整条链路的命脉。
