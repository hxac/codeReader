# 四种饱和模式：回绕与饱和

## 1. 本讲目标

本讲只解决一件事：当定点数的**整数位 `I` 或符号位 `S` 变少**时，放不下的高位怎么办。

学完后你应当能够：

1. 说出 `None_s / Warn_s / Sat_s / SatWarn_s` 四种饱和模式在「是否钳位」「是否告警」上的差别。
2. 用 `min_value / max_value` 写出一个格式的表示范围，并据此判断某个值是否溢出。
3. 读懂 Python `NarrowFix.saturate` 的**回绕（wrap）公式**，并解释为何有符号回绕会触发 narrow→wide 精度回退。
4. 读懂 VHDL `cl_fix_saturate` 如何用「先 `convert`（回绕）再 `cl_fix_compare`（钳位）」两步实现四种模式，并与 Python 严格对应。
5. 理解 `cl_fix_in_range` 的判定逻辑及其在穷举对拍测试中的角色。

---

## 2. 前置知识

本讲承接 [u4-l1 七种舍入模式](u4-l1-rounding.md)。请先记住两件事：

- **舍入（round）处理的是小数位 `F` 变少**——丢的是低位（LSB）。
- **饱和（saturate）处理的是整数位 `I` 和符号位 `S` 变少**——丢的是高位（MSB）。

二者互不重叠，所以 `cl_fix_saturate` 有一条硬性前提：**小数位不能变**（`r_fmt.F == a_fmt.F`）。要同时改小数位和整数位，请用 `cl_fix_resize`，它先 `round` 后 `saturate`（顺序见 [u4-l3](u4-l3-resize.md)）。

还需要回忆两个概念：

- **格式 `[S, I, F]`**：`S` 为符号位个数（0 无符号、1 补码有符号），`I` 为整数位，`F` 为小数位，总位宽 `S+I+F`。
- **NarrowFix / WideFix**：位宽 ≤ 53 位用 `NarrowFix`（float64 内部表示，快），否则用 `WideFix`（任意精度整数）。本讲的 narrow→wide 回退正是发生在这条边界附近。

> 关键直觉：硬件里「丢高位」是免费的——直接截断信号高位就发生了。所以**回绕（wrap）是硬件的自然行为，饱和（saturate）反而是需要额外比较器去刻意实现的行为**。理解了这一点，四种模式的优先级就清楚了。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | `FixSaturate` 枚举定义，给出四种模式的语义注释。 |
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | `NarrowFix.max_value/min_value/saturate`，本讲 Python 侧主角。 |
| [bittrue/models/python/en_cl_fix_pkg/wide_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py) | `WideFix.saturate`，整数域回绕，用于对照与精度回退。 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | `cl_fix_saturate / cl_fix_in_range / cl_fix_max_value` 等门面函数与 narrow/wide 分发。 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | VHDL 侧 `convert`、`resize_sensible`、`cl_fix_saturate`、`cl_fix_compare`、`cl_fix_in_range`。 |
| [bittrue/tests/python/cl_fix_saturate_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py) | 穷举对拍测试，含本地参考实现 `sat_check`。 |

---

## 4. 核心概念与源码讲解

### 4.1 四种饱和模式的语义

#### 4.1.1 概念说明

当目标格式的整数位 / 符号位少于源格式时，源数据可能超出目标范围。库提供两个**正交的开关**来描述如何处理这种超出：

- **是否饱和（Saturate?）**：`是` 则把超界值钳位（clamp）到端点；`否` 则丢高位，让值「回绕」（wrap）。
- **是否告警（Warn?）**：`是` 则在检测到超界时发警告（Python 用 `warnings.warn`，VHDL 用 `assert ... severity Warning`）；`否` 则静默。

两个开关组合出四种模式，定义在 `FixSaturate` 枚举里：

```python
None_s   = 0   # No saturation, no warning.   （回绕，静默）
Warn_s   = 1   # No saturation, only warning. （回绕，但告警）
Sat_s    = 2   # Only saturation, no warning. （钳位，静默）
SatWarn_s= 3   # Saturation and warning.      （钳位，并告警）
```

这正是 [README 中的饱和模式表 README.md:183-188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L183-L188) 的来源：

| Saturation Mode | Saturate? | Warn? |
|-----------------|-----------|-------|
| `None_s`        | No        | No    |
| `Warn_s`        | No        | Yes   |
| `Sat_s`         | Yes       | No    |
| `SatWarn_s`     | Yes       | Yes   |

> 记忆口诀：名字里带 `Sat` 就钳位，带 `Warn` 就告警。`None_s` 什么都不做（纯回绕），`SatWarn_s` 什么都做。

#### 4.1.2 核心流程

四种模式可以归并成两条计算路径，由「是否饱和」这个开关二选一：

```
            ┌─ Saturate? 否 ─→ 回绕路径（wrap）：丢高位，取模
saturate ───┤
            └─ Saturate? 是 ─→ 钳位路径（clamp）：超上界→max，超下界→min

告警是独立旁路：只要带 Warn，就在两条路径之外，先检查是否超界并报警。
```

所以代码里通常先判告警、再二选一算结果。这也意味着 **`Sat_s` 和 `SatWarn_s` 算出来的数值完全相同**，差别只在有没有打印告警；`None_s` 和 `Warn_s` 同理。

#### 4.1.3 源码精读

枚举定义见 [en_cl_fix_types.py:43-50](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L43-L50)，每行的行内注释就是上表「Saturate? / Warn?」的直接来源。

VHDL 侧没有独立的「模式表」，但四种模式的判定散落在 `cl_fix_saturate` 体里，可以对照阅读 [hdl/en_cl_fix_pkg.vhd:990-1006](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L990-L1006)：

- 第 991 行 `if saturate = Warn_s or saturate = SatWarn_s` —— 带 Warn 的两种模式才告警。
- 第 1000 行 `if saturate = Sat_s or saturate = SatWarn_s` —— 带 Sat 的两种模式才钳位；否则（`None_s/Warn_s`）走 `convert` 的回绕结果。

> ⚠️ 跨语言默认值差异：VHDL 的 `cl_fix_saturate` / `cl_fix_resize` 默认 `saturate := Warn_s`（见 [vhd:146](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L142-L147)、[vhd:154](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L149-L155)）；而 Python 的 `cl_fix_resize` 默认 `FixSaturate.None_s`（见 [en_cl_fix.py:242](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L240-L242)）。同名函数默认行为不同，迁移代码时要显式指定 `sat`，别依赖默认值。

#### 4.1.4 代码实践

1. **目标**：直观感受「同名模式数值相同、只差告警」。
2. **步骤**：在 Python 中分别用 `Sat_s` 和 `SatWarn_s` 对同一个溢出值做饱和，比较返回值与告警。
3. **观察**：两者数值相等；只有 `SatWarn_s` 触发 `UserWarning`。
4. **预期结果**：`Sat_s` 静默钳位，`SatWarn_s` 钳位并打印 `NarrowFix.saturate : Saturation warning!`。（待本地运行确认）

```python
import warnings
from en_cl_fix_pkg import *
# 详见 4.5 与第 5 节的完整可运行示例；此处仅示意两种模式数值一致。
```

#### 4.1.5 小练习与答案

- **Q1**：你想要「硬件最省资源、但仿真时能看到溢出提示」的调试行为，该选哪种模式？
  - **A1**：`Warn_s`。它不插比较器（仍回绕），只在仿真里告警，正是「省硬件 + 可观测」的折中。
- **Q2**：为什么 `Sat_s` 和 `SatWarn_s` 算出的数值必然相同？
  - **A2**：两者都走钳位分支（`Saturate? = Yes`），告警只是该分支之外的一条独立旁路，不改变数值。

---

### 4.2 表示范围边界：min_value / max_value

#### 4.2.1 概念说明

要判断「是否超界」并实现钳位，首先得知道一个格式能表示的**最大值与最小值**。这是饱和与 `in_range` 的共同基石。

对格式 `[S, I, F]`（值 = 整数 × \(2^{-F}\)）：

- **无符号（S=0）**：所有位都用来表示非负数，范围是 \([0,\; 2^{I} - 2^{-F}]\)。
- **有符号（S=1，补码）**：最高位权重为 \(-2^{I}\)，范围是 \([-2^{I},\; 2^{I} - 2^{-F}]\)。注意补码**不对称**：负端能取到 \(-2^{I}\)，正端只能到 \(2^{I}-2^{-F}\)。

#### 4.2.2 核心流程

```
max_value(fmt) = 2^I − 2^(−F)          （无论有无符号，正端相同）
min_value(fmt) = S==1 ? −2^I : 0       （负端取决于符号位）
```

- `max`：全 1 的比特向量；若有符号位，把符号位置 0。
- `min`：有符号时全 0 但符号位置 1（即最负值）；无符号时全 0。

#### 4.2.3 源码精读

Python 侧（返回 `NarrowFix`，内部 float）：

- [narrow_fix.py:110-115 `max_value`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L110-L115)：`2.0**fmt.I - 2.0**(-fmt.F)`。
- [narrow_fix.py:117-123 `min_value`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L117-L123)：`-2.0**fmt.I if fmt.S == 1 else 0.0`，正好对应上面的条件表达式。

门面函数 [en_cl_fix.py:87-104](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L87-L104) 按宽度在 Narrow/Wide 之间分发，返回标量数值。

VHDL 侧直接造比特向量，更接近硬件实现：

- [vhd:370-378 `cl_fix_max_value`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L370-L378)：先全置 `'1'`，若 `S=1` 再把最高位（符号位）改 `'0'`。
- [vhd:380-390 `cl_fix_min_value`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L380-L390)：`S=1` 时全 `'0'` 再把最左符号位置 `'1'`（最负值）；`S=0` 时全 `'0'`。

> 这就是为什么 `FixFormat` 构造时强制 `I+F >= 0`（见 [u2-l1](u2-l1-core-types.md)）：否则 `cl_fix_max_value` 这类函数会出现「无意义的最负值」等尴尬边界，库选择直接禁止。

#### 4.2.4 代码实践

1. **目标**：手算并验证一个格式的范围。
2. **步骤**：手算 `[1, 3, 4]` 的 min/max，再用 Python 核对。
3. **预期**：max = \(2^3 - 2^{-4} = 7.9375\)，min = \(-2^3 = -8\)。
4. **验证**：`cl_fix_max_value(FixFormat(1,3,4))` → `7.9375`；`cl_fix_min_value(...)` → `-8.0`。

#### 4.2.5 小练习与答案

- **Q1**：为什么有符号格式的正负端不对称？
  - **A1**：补码把 0 划归「非负」一侧，于是正端比负端少一个 LSB 的表示余地：负端能到 \(-2^I\)，正端只到 \(2^I - 2^{-F}\)。
- **Q2**：`max_value` 的公式对无符号和有符号是同一个，为什么？
  - **A2**：有符号时最高位是符号位、正端最大值要求它为 0，剩下位全 1，结果仍等于 \(2^I - 2^{-F}\)；符号位只是「不可用的一位」，不改变正端公式。

---

### 4.3 NarrowFix.saturate：回绕公式与 narrow→wide 精度回退

#### 4.3.1 概念说明

这是本讲 Python 侧的主角。`NarrowFix.saturate` 用 **float64 内部表示**直接算回绕或钳位。

回绕的数学本质是**取模**：把超出 \((S+I)\) 位范围的值，按周期折叠回来。对目标格式 `r_fmt = (S, I, F)`：

- 无符号（\(S=0\)）：值落在 \([0, 2^I)\)，取模 \(2^I\):

\[
x \mapsto x \bmod 2^{I}
\]

- 有符号（\(S=1\)）：值落在 \([-2^I, 2^I)\)，取模 \(2^{I+1}\) 后平移居中:

\[
x \mapsto \bigl((x + 2^{I}) \bmod 2^{I+1}\bigr) - 2^{I}
\]

钳位则简单：小于 min 就取 min，大于 max 就取 max。

#### 4.3.2 核心流程

`NarrowFix.saturate(r_fmt, sat)` 的执行顺序（[narrow_fix.py:190-244](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L190-L244)）：

1. **断言** `r_fmt.F == fmt.F`（小数位不能变）。
2. **告警旁路**：若 `sat ∈ {Warn_s, SatWarn_s}`，检查是否有值超出 `[min, max]`，超出则 `warnings.warn`。
3. **二选一计算**：
   - `sat ∈ {None_s, Warn_s}` → **回绕**：先判断有符号回绕是否会精度不足；会则切到整数域（wide）算，不会则在 float64 算取模。
   - `sat ∈ {Sat_s, SatWarn_s}` → **钳位**：`np.where` 把超界值替换为端点。

#### 4.3.3 源码精读

**告警**（[narrow_fix.py:199-204](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L199-L204)）：

```python
if sat == FixSaturate.Warn_s or sat == FixSaturate.SatWarn_s:
    if np.any(self > fmt_max) or np.any(self < fmt_min):
        warnings.warn("NarrowFix.saturate : Saturation warning!", Warning)
```

**回绕分支与 narrow→wide 回退判定**（[narrow_fix.py:207-221](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L207-L221)）：有符号回绕需要计算 `data + 2^I`，这相当于把数据又加了 1 个整数位。库用 `FixFormat.for_add(fmt, offset_fmt)` 算出这个加法的全精度中间格式 `add_fmt`，若其位宽超过 `MAX_WIDTH=53`，float64 无法精确表示，于是切到任意精度整数（Python `object` 数组）计算：

```python
if r_fmt.S == 1:
    ...
    add_fmt = FixFormat.for_add(fmt, offset_fmt)
    convert_to_wide = (add_fmt.width > NarrowFix.MAX_WIDTH)
else:
    convert_to_wide = False      # 无符号回绕不额外占位，无需回退
```

> 这里 `MAX_WIDTH=53` 的来历见 [narrow_fix.py:40-52](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L40-L52) 的长注释：IEEE 754 双精度有 52 位尾数 + 1 位隐含位，故 `[-2^53, 2^53]` 内整数可精确表示。为让有符号/无符号共用同一 53 位上限，有符号额外预留 1 个整数位，使「回绕」也能在 float 域安全完成——超出时才回退。

**wide 路径（整数域取模）**（[narrow_fix.py:223-232](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L223-L232)）：把数据乘 \(2^F\) 转成整数 `data`，`span = 2^(I+F)`，做与上式同构的取模，再除回 \(2^F\)：

```python
data = np.floor(data.astype(object) * 2**r_fmt.F)
span = 2**(r_fmt.I + r_fmt.F)
if r_fmt.S == 1:
    sat_data = ((data + span) % (2*span)) - span
else:
    sat_data = data % span
sat_data = (sat_data / 2**r_fmt.F).astype(float)
```

这与 [wide_fix.py:288-298 `WideFix.saturate`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L288-L298) 的整数回绕公式**逐字同构**——只是 NarrowFix 把它当作「精度不足时的临时救场」。

**float 路径**（[narrow_fix.py:233-238](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L233-L238)）：未触发回退时，直接在 float64 上套本节开头的两个取模公式。

**钳位分支**（[narrow_fix.py:240-242](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L239-L242)）：

```python
sat_data = np.where(self > fmt_max, fmt_max._data, data)
sat_data = np.where(self < fmt_min, fmt_min._data, sat_data)
```

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：跟踪一次 narrow→wide 回退的判定。
2. **步骤**：阅读 [narrow_fix.py:211-221](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L211-L221)，构造一个接近 53 位的有符号格式（如 `FixFormat(1, 26, 26)`，宽 53），把它的整数位再增大 1（`FixFormat(1, 27, 26)`，宽 54）。
3. **观察**：手算 `add_fmt.width` 是否越过 53，预测是否回退。
4. **预期**：53 位时 `add_fmt` 多 1 位变 54 > 53 → 回退；可加 `print` 在 [narrow_fix.py:223](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L223-L232) 前打印 `convert_to_wide` 确认。（待本地验证）

#### 4.3.5 小练习与答案

- **Q1**：为什么只有**有符号**回绕才会触发 narrow→wide 回退，无符号不会？
  - **A1**：有符号回绕要先算 `data + 2^I`，多占 1 个整数位，可能把总宽顶过 53；无符号回绕是纯取模 `data % 2^I`，不增加位宽，float64 始终够用。
- **Q2**：回退到整数域后，回绕结果与 float 域结果在数学上应满足什么关系？
  - **A2**：完全相等。两条路径是同一取模公式的两种算术实现，回退只为避免 float64 舍入误差，不改语义。

---

### 4.4 VHDL cl_fix_saturate：convert 回绕 + cl_fix_compare 钳位

#### 4.4.1 概念说明

VHDL 侧没有「取模运算」，它用一种更贴近硬件的写法实现同一逻辑：**回绕 = 直接截断高位**，**钳位 = 截断后比较、必要时替换为端点**。

关键洞察是：在硬件里，把一个向量写入更窄的位宽，**天然就完成了回绕**——多余的高位被丢弃，正是补码/无符号的取模效果。库把这个「无舍入无饱和的对齐写入」封装成内部函数 `convert`，它本身就是 `None_s` 模式的回绕实现。

#### 4.4.2 核心流程

`cl_fix_saturate`（[vhd:980-1009](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L980-L1009)）：

1. **断言** `result_fmt.F = a_fmt.F`（小数位不能变）。
2. **告警旁路**：`Warn_s/SatWarn_s` 时用 `cl_fix_in_range` 检测，超界则 `assert ... severity Warning`。
3. **先回绕**：`result_v := convert(a, a_fmt, result_fmt)` —— 对齐小数点并截断高位，等价于 `None_s` 回绕。
4. **再钳位**：仅当 `Sat_s/SatWarn_s`，用 `cl_fix_compare` 把**原始值 `a`** 与端点比较，超界则覆盖 `result_v` 为 `min/max`。

> 注意第 4 步比较用的是**原始 `a`**，而不是已经截断的 `result_v`。因为截断后的小值无法再反推是否曾溢出，必须拿原始大值去比。

`cl_fix_compare`（[vhd:1276-1315](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1276-L1315)）的做法是：把两个可能不同格式的操作数都 `convert` 到 `union(aFmt, bFmt)` 公共格式，再按 `S` 决定用 `signed` 还是 `unsigned` 比较。这避免了「不同位宽/符号性直接比较」的歧义。

#### 4.4.3 源码精读

**`convert`（回绕本体）**（[vhd:329-351](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L329-L351)）：按小数点对齐把输入写入结果向量，多余的整数位用符号扩展（有符号）或 0 扩展（无符号）填充；**当结果整数位更少时，调用 `resize_sensible` 做纯截断**，这正是丢高位=回绕：

```vhdl
if aFmt.S = 0 then
    result_v(...) := std_logic_vector(resize(unsigned(a_c), ...));
else
    result_v(...) := std_logic_vector(resize_sensible(signed(a_c), ...));
end if;
```

其注释明确写道：*this implements cl_fix_saturate, with None_s saturation mode*。

**`resize_sensible`**（[vhd:313-326](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L313-L326)）：位宽变长用标准 `resize`（符号扩展），位宽变短**只做朴素截断**——它特意不用 `numeric_std.resize`（后者会保留符号位），因为定点饱和语境下要的就是「丢高位」。

**`cl_fix_saturate` 钳位段**（[vhd:1000-1006](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L999-L1006)）：

```vhdl
if saturate = Sat_s or saturate = SatWarn_s then
    if cl_fix_compare("<", a, a_fmt, cl_fix_min_value(result_fmt), result_fmt) then
        result_v := cl_fix_min_value(result_fmt);
    elsif cl_fix_compare(">", a, a_fmt, cl_fix_max_value(result_fmt), result_fmt) then
        result_v := cl_fix_max_value(result_fmt);
    end if;
end if;
```

Python 与 VHDL 的实现风格迥异（取模 vs 截断+比较），但经穷举对拍保证**逐位一致**（bit-true），这正是 en_clustra 「三语言 bit-true」承诺的体现。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解「回绕免费、钳位有成本」。
2. **步骤**：对比 [vhd:997](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L996-L997)（无条件 `convert`）与 [vhd:1000-1006](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L999-L1006)（仅 Sat 模式才比较）。
3. **观察**：`None_s/Warn_s` 不触发任何比较器，综合后只有连线；`Sat_s/SatWarn_s` 才生成比较逻辑。
4. **预期**：从综合资源角度，`None_s` 最省，`Sat*` 多一组比较器（待综合验证）。

#### 4.4.5 小练习与答案

- **Q1**：为什么 `cl_fix_saturate` 第 4 步比较的是 `a` 而不是 `result_v`？
  - **A1**：`result_v` 已被 `convert` 截断，溢出信息已丢失；必须用未截断的原始 `a` 与端点比，才能判断是否曾超界。
- **Q2**：`convert` 既用于回绕又用于 `cl_fix_compare` 的对齐，它在两种用途下分别截断/扩展了什么？
  - **A2**：回绕时把更宽的源截到目标宽度（丢 MSB）；比较对齐时把两操作数扩展到 `union` 公共格式（补 MSB），方向相反但用同一函数。

---

### 4.5 cl_fix_in_range 与穷举对拍测试

#### 4.5.1 概念说明

`cl_fix_in_range` 回答一个问题：**给定舍入模式，源数据量化后是否能落在目标格式范围内（即不会触发饱和）？** 它只判断、不修改数据。它被两处复用：一是 `Warn*` 模式的告警判定（VHDL 侧），二是测试中的覆盖检查。

#### 4.5.2 核心流程

`cl_fix_in_range(a, a_fmt, r_fmt, rnd)`（Python [en_cl_fix.py:114-124](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L114-L124)，VHDL [vhd:1026-1041](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1026-L1041)）：

1. 先按 `rnd` 算出舍入后的中间格式 `rounded_fmt = cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)`。
2. 对数据做舍入得到 `rounded`。
3. 返回 `rounded >= min_value(r_fmt)` 且 `rounded <= max_value(r_fmt)`。

> 为什么先舍入再比范围？因为舍入可能向上进位（见 [u4-l1](u4-l1-rounding.md) 的整数位 +1），一个原本不溢出的值，舍入后可能恰好顶破上限。所以「在范围内」必须按**实际会用的舍入**来判定。

#### 4.5.3 源码精读

`cl_fix_saturate_test.py` 是本讲的验证基石。它对每个合法的窄格式组合，**枚举源格式的所有可能取值**，把 `cl_fix_saturate` 的输出同时与三条独立实现比对：

1. 待测函数 `cl_fix_saturate`（[test:120](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L118-L120)）。
2. `WideFix` 路径（[test:123](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L122-L123)）。
3. 本地参考 `sat_check`（[test:49-72](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L49-L72)）。

`sat_check` 用最朴素的 `while` 循环实现回绕（不断加减 `offset = 2^(S+I)` 直到落入 `[min, max]`），与库的高效取模实现完全独立，二者相等才说明取模公式正确：

```python
offset = 2.0 ** (r_fmt.S + r_fmt.I)
for i in range(len(a)):
    while a[i] < min_r: a[i] += offset
    while a[i] > max_r: a[i] -= offset
```

三份实现两两 `np.array_equal` 全过，才会打印 `Completed N tests.`（[test:134](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L118-L134)）。这正是 [u1-l4 快速上手](u1-l4-quick-start-tests.md) 讲过的「穷举对拍」方法论在饱和场景的应用。

#### 4.5.4 代码实践（可运行）

1. **目标**：运行穷举测试，确认饱和实现零失败。
2. **步骤**：在仓库根目录运行 `python bittrue/tests/python/cl_fix_saturate_test.py`。
3. **观察**：脚本无 `AssertionError`，最终打印 `Completed <N> tests.`。
4. **预期结果**：`N` 为数千量级（取决于合法格式组合数 × 4 模式 × 取值数），无断言失败即代表 NarrowFix、WideFix、`sat_check` 三路一致。（待本地运行确认具体 N）

#### 4.5.5 小练习与答案

- **Q1**：`cl_fix_in_range` 为什么默认用 `Trunc_s` 舍入来判定？换舍入模式会改变结论吗？
  - **A1**：默认 `Trunc_s` 只向下取整、不进位，是最「宽松」的判定；换成会向上进位的模式（如 `NonSymPos_s`），原本刚好在范围顶端的值可能因进位被判为「超界」，结论会变。
- **Q2**：`sat_check` 用 `while` 循环加减 `offset`，与库的取模实现为何能对拍出相同结果？
  - **A2**：两者是同一「按周期 \(2^{S+I}\) 折叠」操作的两种表述：`while` 是逐步折叠，取模是一步到位，数学等价，故穷举下逐位相等。

---

## 5. 综合实践

把本讲内容串起来：构造一个**源在范围内、但目标必然溢出**的场景，分别用 `SatWarn_s`（钳位+告警）和 `None_s`（回绕）观察差异。

**设计**：源 `[1,4,0]`（有符号 5 位，范围 \([-16, 15]\)）存值 `10`；目标 `[1,2,0]`（有符号 3 位，范围 \([-4, 3]\)）。`10` 在源范围内合法，但超出目标范围，必然触发饱和或回绕。

```python
# 文件名建议：sat_demo.py，放在 bittrue/tests/python/ 旁，或自行 sys.path 指向 models/python
import sys, warnings
from os.path import join, dirname
sys.path.append(join(dirname(__file__), "../../bittrue/models/python"))
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 4, 0)   # 范围 [-16, 15]
r_fmt = FixFormat(1, 2, 0)   # 范围 [-4, 3]
a = cl_fix_from_integer(10, a_fmt)   # 10 在源范围内，合法

# ① SatWarn_s：钳位到 max=3，并触发告警
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    r_sat = cl_fix_saturate(a, a_fmt, r_fmt, FixSaturate.SatWarn_s)
    print("SatWarn_s ->", r_sat, "| 告警数:", len(w),
          "| 告警内容:", w[0].message if w else None)

# ② None_s：回绕。10 的 5 位补码 01010 截到 3 位得 010 = 2
r_wrap = cl_fix_saturate(a, a_fmt, r_fmt, FixSaturate.None_s)
print("None_s     ->", r_wrap)

# ③ 用 in_range 事先预测（注意先用 round 量化到目标小数位，此处 F 未变）
print("in_range?  ->", bool(cl_fix_in_range(a, a_fmt, r_fmt)))
```

**预期结果（按公式手算，待本地运行确认）**：

- `SatWarn_s -> 3.0`，告警数 1，内容为 `NarrowFix.saturate : Saturation warning!`。
- `None_s -> 2.0`（回绕：\(10 \bmod 8\) 的有符号解释 \(= 6 - 4 = 2\)）。
- `in_range? -> False`（10 超出 \([-4,3]\)）。

**思考延伸**：把目标改成 `[1,3,0]`（有符号 4 位，范围 \([-8, 7]\)），重算回绕值与是否告警，验证你对取模公式的理解。提示：`10` 仍超出 \([-8,7]\)，回绕结果应为 \(((10+8) \bmod 16) - 8 = -6\)。

> 如果运行时 `cl_fix_from_integer(10, FixFormat(1,4,0))` 报 `Value not in number format range`，说明你误把值写成了源范围之外（如 `20`）；务必让源值落在源格式范围内，溢出只应发生在「源→目标」这一步。

---

## 6. 本讲小结

- 四种饱和模式 = 「是否钳位」×「是否告警」两个正交开关：`None_s` 回绕静默、`Warn_s` 回绕告警、`Sat_s` 钳位静默、`SatWarn_s` 钳位告警；带 `Sat` 的两种数值相同，带 `Warn` 的两种才告警。
- 范围边界：`max = 2^I − 2^(−F)`，`min = S ? −2^I : 0`；有符号补码正负端不对称。
- `NarrowFix.saturate` 用取模实现回绕（有符号 \( ((x+2^I) \bmod 2^{I+1}) - 2^I \)，无符号 \(x \bmod 2^I\)）；有符号回绕多占 1 个整数位，可能越过 53 位上限而**回退到整数域（wide）**计算以保精度。
- VHDL `cl_fix_saturate` 用「先 `convert`（截断=回绕）再 `cl_fix_compare`（钳位）」实现同一语义；`None_s/Warn_s` 不生成比较器，最省硬件。
- `cl_fix_in_range` 按**指定舍入模式**先量化再判范围，是告警判定与测试覆盖的共用工具。
- 跨语言默认值不同：VHDL `cl_fix_resize` 默认 `Warn_s`，Python 默认 `None_s`，迁移代码要显式指定 `sat`。

---

## 7. 下一步学习建议

- 进入 [u4-3 resize = round + saturate](u4-l3-resize.md)，看 `cl_fix_resize` 如何把本讲的 `saturate` 与 [u4-1](u4-l1-rounding.md) 的 `round` 串成一条「先舍入后饱和」的统一链路，以及为何顺序不可对调。
- 若对 narrow/wide 边界与精度回退感兴趣，可提前跳读 [u6-1 NarrowFix](u6-l1-narrow-fix.md) 与 [u6-2 WideFix](u6-l2-wide-fix.md)。
- 想看饱和如何在可综合 RTL 里落地（带寄存器、valid 握手），请读 [u7-1 RTL 组件](u7-l1-rtl-components.md) 中的 `en_cl_fix_saturate.vhd` / `en_cl_fix_resize.vhd`。
