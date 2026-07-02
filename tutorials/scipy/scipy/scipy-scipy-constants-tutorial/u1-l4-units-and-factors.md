# 单位与线性换算因子

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `scipy.constants` 中"换算因子"的设计思想：**每一个常量都是把某个单位换算到 SI 基本单位的乘数**。
- 区分两类常量来源：纯 Python 字面量定义的常量，与通过 `_cd(...)`（即 `value()`）从 CODATA 物理常数数据库派生出来的常量。
- 会用这些常量做日常单位换算：长度、速度、能量、压力、力等。
- 理解温度类常量 `zero_Celsius` 与 `degree_Fahrenheit` 的特殊含义——它们**只用于温度差值的换算，不能用于温度本身的换算**。

本讲承接 [u1-l3 数学常数与 SI / 二进制前缀](u1-l3-math-and-prefixes.md)。上一讲我们认识了 `pi`、`golden`、SI 前缀（`kilo`、`milli`…）和二进制前缀（`kibi`、`mebi`…）。本讲继续往下读 `_constants.py`，进入"按物理量分类的单位换算因子"这一大块。

---

## 2. 前置知识

### 2.1 什么是"线性换算因子"

把一个物理量从一个单位换算到另一个单位，最简单的情况是**线性换算**：

\[
x_{\text{目标}} = x_{\text{原}} \times f
\]

其中 \(f\) 就是换算因子。比如 1 英寸 = 0.0254 米，那么 `inch = 0.0254` 这个常量就是"英寸 → 米"的换算因子。于是 `10 * inch` 就得到 10 英寸等于多少米。

`scipy.constants` 把**所有**换算因子都统一指向 SI 基本单位：

| 物理量 | SI 基本单位 | 常量的含义 |
|--------|------------|-----------|
| 长度 | 米 (m) | `inch = 0.0254` 表示 1 inch = 0.0254 m |
| 质量 | 千克 (kg) | `gram = 1e-3` 表示 1 g = 0.001 kg |
| 时间 | 秒 (s) | `minute = 60.0` 表示 1 min = 60 s |
| 能量 | 焦耳 (J) | `calorie_th = 4.184` 表示 1 cal = 4.184 J |

> 注意：质量这一类比较特殊。`gram`（克）被定义成 `1e-3`（千克），而不是 `1`，这是因为 SI 基本单位是**千克**而不是克。这是物理学约定，记住即可。

因为所有因子都换算到 SI，所以你可以直接做算术：`10 * mile / minute` 自动得到"米 / 秒"。这正是模块 docstring 开篇那行示例的含义。

### 2.2 什么是 CODATA

CODATA（科学技术数据委员会）会定期发布**基本物理常数的推荐值**，比如光速、普朗克常数、标准重力加速度等。`scipy.constants` 用的是 CODATA 2022 推荐值。

本讲里你会发现，少数换算因子（如标准大气压 `atm`、光年 `light_year`、磅力 `psi`）不是写死的字面量，而是从这些 CODATA 物理常数派生出来的。这些 CODATA 常数本身的数据库结构留到进阶篇 [u2-l1](u2-l1-physical-constants-api.md) 讲，本讲你只需要知道：**`_cd('某个常量名')` 会从 CODATA 数据库里取出该常量的数值**。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但它信息量很大，按物理量类别分成了十几段：

| 文件 | 作用 |
|------|------|
| [_constants.py:L125-L144](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L125-L144) | 质量单位（gram、pound、ton…）及原子质量（来自 CODATA） |
| [_constants.py:L146-L149](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L146-L149) | 角度单位（degree、arcmin、arcsec） |
| [_constants.py:L151-L157](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L151-L157) | 时间单位（minute、hour、day、year…） |
| [_constants.py:L159-L174](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L159-L174) | 长度单位（inch、foot、mile、light_year…） |
| [_constants.py:L176-L180](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L176-L180) | 压力单位（atm、bar、torr、psi） |
| [_constants.py:L182-L184](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L182-L184) | 面积单位（hectare、acre） |
| [_constants.py:L186-L194](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L186-L194) | 体积单位（liter、gallon、barrel…） |
| [_constants.py:L196-L201](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L196-L201) | 速度单位（kmh、mph、mach、knot） |
| [_constants.py:L203-L205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L203-L205) | 温度差值常量（zero_Celsius、degree_Fahrenheit） |
| [_constants.py:L207-L214](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L207-L214) | 能量单位（eV、calorie、erg、Btu…） |
| [_constants.py:L217-L218](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L217-L218) | 功率单位（horsepower） |
| [_constants.py:L220-L223](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L220-L223) | 力单位（dyne、lbf、kgf） |

还有一个关键的一行导入，是理解"派生常量"的钥匙：

| 文件 | 作用 |
|------|------|
| [_constants.py:L13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L13) | `from ._codata import value as _cd`——把 CODATA 查值函数 `value` 重命名为 `_cd`，用于派生部分换算因子 |

---

## 4. 核心概念与源码讲解

### 4.1 线性换算因子的设计思想：一切归于 SI 单位

#### 4.1.1 概念说明

打开 `_constants.py` 顶部，你会看到模块 docstring 用一句话点明了整个模块的用法：

```python
"""
Collection of physical constants and conversion factors.

Most constants are in SI units, so you can do
print '10 mile per minute is', 10*mile/minute, 'm/s or', 10*mile/(minute*knot), 'knots'

The list is not meant to be comprehensive, but just convenient for everyday use.
"""
```

这段话揭示了两个关键设计：

1. **大多数常量都换算到 SI 单位**（米、千克、秒、焦耳、瓦特、牛顿、帕斯卡……），所以常量本身就是一个"无量纲的乘数"。
2. **直接用算术表达式做换算**：`10*mile/minute` 就得到米/秒，不需要调用任何函数。

这套设计的本质是：**把"单位"编码成一个浮点数，让 Python 的乘除法自动完成换算**。这比提供一个 `convert(10, 'mile', 'm')` 之类的函数更轻量，也更符合物理公式书写的直觉。

它还有一个隐藏的好处——**链式换算**。比如 `mile` 没有直接写死成 `1609.344`，而是层层组合：`mile = 1760 * yard`，`yard = 3 * foot`，`foot = 12 * inch`，`inch = 0.0254`。这样改动基准定义时只需改一处（`inch`），所有上级单位自动更新，单一数据源（single source of truth）。

#### 4.1.2 核心流程

用换算因子做单位换算的流程：

1. 确定要换算的量和它当前的单位。
2. 找到该单位对应的常量（它就是"原单位 → SI"的乘数）。
3. 把数值乘以这个常量，得到 SI 值。
4. 如果还要换到第三个单位，再除以目标单位的常量。

伪代码：

```
value_in_SI = value * unit_factor          # 原单位 → SI
value_in_other = value_in_SI / other_factor  # SI → 目标单位
# 化简：value_in_other = value * unit_factor / other_factor
```

例如把"10 英里每分钟"换算成"节（knot）"：

\[
v_{\text{knot}} = \frac{10 \times \text{mile}}{\text{minute} \times \text{knot}}
\]

这里 `mile / minute` 得到 m/s，再除以 `knot`（1 节 = 多少 m/s）就得到节数。

#### 4.1.3 源码精读

`_constants.py` 顶部的导入行是理解派生常量的钥匙：

[_constants.py:L10-L13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L10-L13)

```python
import math as _math
from typing import TYPE_CHECKING, Any

from ._codata import value as _cd
```

这一行做了三件事：

- 从同包子模块 `_codata` 导入函数 `value`。
- 把它**重命名为 `_cd`**，这样后续写 `_cd('speed of light in vacuum')` 比写 `value(...)` 更短，也避免与本文件后续可能定义的 `value` 混淆。
- 下划线前缀表示这是模块内部用的别名，不会被 `import *` 导出。

`value()` 函数本身定义在 [_codata.py:L2130](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2130)，作用是"按名字从 CODATA 物理常数字典里取出数值"。本讲你只需把它当作一个"查表函数"，详细机制留到 [u2-l1](u2-l1-physical-constants-api.md)。

紧随其后，文件用 `_cd` 定义了一批**物理常数**（光速、普朗克常数等），它们是后续换算因子的"原料"：

[_constants.py:L109-L123](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L109-L123)

```python
c = speed_of_light = _cd('speed of light in vacuum')
# ...（省略若干物理常数）
g = _cd('standard acceleration of gravity')
```

注意其中 `g = _cd('standard acceleration of gravity')`——**标准重力加速度**。这个 `g` 会在后面的压力（`psi`）、力（`lbf`、`kgf`）、功率（`hp`）等换算因子里反复用到。这些物理常数的深入讲解属于 [u2-l1](u2-l1-physical-constants-api.md)，本讲只把它们当作输入。

#### 4.1.4 代码实践

**实践目标**：亲手验证"所有换算因子都换算到 SI"这个设计，并体会链式定义。

**操作步骤**：

```python
from scipy.constants import inch, foot, yard, mile

# 1. 直接读出每级单位对应的米数
print("1 inch =", inch, "m")     # 0.0254
print("1 foot =", foot, "m")     # 12 * 0.0254 = 0.3048
print("1 yard =", yard, "m")     # 3 * foot = 0.9144
print("1 mile =", mile, "m")     # 1760 * yard = 1609.344

# 2. 验证链式定义：mile 应该等于 1760 * 3 * 12 * 0.0254
print("mile reconstructed =", 1760 * 3 * 12 * 0.0254)
```

**需要观察的现象**：`foot`、`yard`、`mile` 的值恰好等于由 `inch = 0.0254` 逐级乘出来的结果。

**预期结果**：

- `inch = 0.0254`
- `foot = 0.3048`
- `yard = 0.9144`
- `mile = 1609.344`
- 重构值 `1760 * 3 * 12 * 0.0254 = 1609.344`，与 `mile` 完全相等。

这说明改动 `inch` 一处，整条链都会同步——这正是"链式定义 = 单一数据源"的体现。以上数值由源码表达式直接推得，建议在本地运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `scipy.constants` 选择把所有因子都换算到 SI，而不是提供形如 `convert(value, from_unit, to_unit)` 的函数？

**参考答案**：因为这样可以把"单位"编码成一个普通浮点数，让 Python 的 `*` 和 `/` 自动完成换算，书写更接近物理公式直觉（如 `F = m * g`），也省去了维护一张单位对换表的开销；同时通过链式定义（`mile = 1760*yard`）保证单一数据源。

**练习 2**：`gram` 被定义为 `1e-3` 而不是 `1`，为什么？

**参考答案**：因为 SI 基本单位是**千克（kg）**而非克，所有质量换算因子都指向 kg，所以 1 g = 0.001 kg。

---

### 4.2 各物理量类别的换算因子

#### 4.2.1 概念说明

在"一切归于 SI"的总思想下，`_constants.py` 把换算因子**按物理量类别分组**，每组用注释标明 SI 单位。这种分组让代码可读性很强：你想找长度单位就去 `# length in meter` 那一段，找能量单位就去 `# energy in joule` 那一段。

绝大多数常量都是**纯 Python 字面量或字面量的算术组合**，不依赖 CODATA。本小节集中看这些"自给自足"的类别。

#### 4.2.2 核心流程

每个类别的组织方式都一样：

1. 一行注释写明 SI 单位，如 `# time in second`。
2. 若干赋值语句，每个常量 = 换算到 SI 的乘数。
3. 常量之间可以互相引用（链式定义），也可以引用前面已定义的常量（如 `mile` 引用 `yard`）。

#### 4.2.3 源码精读

**质量（kg）**：

[_constants.py:L125-L139](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L125-L139)

```python
gram = 1e-3
metric_ton = 1e3
grain = 64.79891e-6
lb = pound = 7000 * grain  # avoirdupois
oz = ounce = pound / 16
stone = 14 * pound
long_ton = 2240 * pound
short_ton = 2000 * pound
carat = 200e-6
```

注意 `pound` 是从 `grain`（格令）派生的：1 磅 = 7000 格令。`grain` 用的是精确字面量 `64.79891e-6` kg，于是 `pound = 7000 * 64.79891e-6 = 0.45359237` kg（国际协议磅的精确定义）。

**时间（s）**：

[_constants.py:L151-L157](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L151-L157)

```python
minute = 60.0
hour = 60 * minute
day = 24 * hour
week = 7 * day
year = 365 * day
Julian_year = 365.25 * day
```

注意区分 `year`（365 天，民用）与 `Julian_year`（365.25 天，儒略年，天文用）。后者会用在光年 `light_year` 的定义里。

**长度（m）**：

[_constants.py:L159-L174](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L159-L174)

```python
inch = 0.0254
foot = 12 * inch
yard = 3 * foot
mile = 1760 * yard
mil = inch / 1000
nautical_mile = 1852.0
fermi = 1e-15
angstrom = 1e-10
micron = 1e-6
au = astronomical_unit = 149597870700.0
parsec = au / arcsec
```

这里几乎所有都是字面量组合，唯一例外是 `light_year`（用到 CODATA 的光速 `c`），它放到下一节 [4.3](#43-由-codata-值派生的换算因子) 讲。`parsec` 的定义很有意思：它等于"地球-太阳平均距离（au）张角为 1 角秒（arcsec）时对应的距离"，是一个几何定义，但仍然化简成了 `au / arcsec` 这个乘除表达式。

**面积（m²）与体积（m³）**：

[_constants.py:L182-L194](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L182-L194)

```python
hectare = 1e4
acre = 43560 * foot**2

litre = liter = 1e-3
gallon = gallon_US = 231 * inch**3
bbl = barrel = 42 * gallon_US  # for oil
gallon_imp = 4.54609e-3  # UK
```

注意面积单位用 `foot**2`、体积单位用 `inch**3`，这是因为它们由长度单位平方/立方便利地得到，自动保持量纲一致。`acre = 43560 * foot**2` 中的 `43560` 是"1 英亩 = 43560 平方英尺"这个传统定义。

**速度（m/s）**：

[_constants.py:L196-L201](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L196-L201)

```python
kmh = 1e3 / hour
mph = mile / hour
# approx value of mach at 15 degrees in 1 atm. Is this a common value?
mach = speed_of_sound = 340.5
knot = nautical_mile / hour
```

速度类是"长度 / 时间"的组合。`kmh` 表示"1 km/h 等于多少 m/s"。注释里那句疑问是开发者留下的思考——`mach` 取的是 15℃、1 atm 下声速的近似值 340.5，并非精确值。

**能量（J）、力（N）的字面量部分**：

[_constants.py:L207-L214](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L207-L214)

```python
calorie = calorie_th = 4.184
calorie_IT = 4.1868
erg = 1e-7
ton_TNT = 1e9 * calorie_th
```

[_constants.py:L220-L223](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L220-L223)

```python
dyn = dyne = 1e-5
```

注意区分两种卡路里：`calorie_th`（热化学卡路里，= 4.184 J）与 `calorie_IT`（国际蒸汽表卡路里，= 4.1868 J）。它们各自对应不同的 BTU（`Btu_th`、`Btu_IT`），见下一节。`erg = 1e-7`（CGS 制能量单位，1 erg = 10⁻⁷ J）。

#### 4.2.4 代码实践

**实践目标**：用字面量定义的换算因子完成几组日常换算，体会"乘除即可"的便利。

**操作步骤**：

```python
from scipy.constants import (
    kmh, mph, knot, mile, minute, hour,
    gram, pound, ounce, carat,
    calorie_th, erg, ton_TNT,
)

# 1. 速度：把 90 km/h 换算成 m/s 和 mph
print("90 km/h =", 90 * kmh, "m/s")          # 25.0
print("90 km/h =", 90 * kmh / mph, "mph")    # 约 55.92

# 2. 质量：把 2 磅换算成千克和克
print("2 lb =", 2 * pound, "kg")             # 0.90718474
print("2 lb =", 2 * pound / gram, "g")       # 907.18474

# 3. 能量：把 1 吨 TNT 当量换算成焦耳，再换算成卡路里
print("1 ton TNT =", ton_TNT, "J")           # 4.184e9
print("1 ton TNT =", ton_TNT / calorie_th, "cal")  # 1e9
```

**需要观察的现象**：

- `90 * kmh` 直接得到 m/s 值。
- 用"除以目标单位的因子"就能从 SI 换到任意单位（`/ mph` 得 mph，`/ gram` 得克）。
- `ton_TNT = 1e9 * calorie_th`，所以 `ton_TNT / calorie_th` 恰好是 1e9。

**预期结果**：

- `90 km/h = 25.0 m/s`，约 `55.92 mph`
- `2 lb = 0.90718474 kg = 907.18474 g`
- `1 ton TNT = 4.184e9 J = 1e9 cal`

以上数值由源码表达式推得，建议在本地运行确认。

#### 4.2.5 小练习与答案

**练习 1**：`kmh` 的定义是 `1e3 / hour`，请解释为什么这等于"1 km/h 换算成 m/s"。

**参考答案**：`1e3` 是 1 km = 1000 m，`hour` 是 1 h = 3600 s。`1e3 / hour = 1000 / 3600 = 0.277…`，即 1 km/h = 0.277… m/s。于是 `x * kmh` 就是把 x km/h 换算成 m/s。

**练习 2**：`calorie_th` 与 `calorie_IT` 有什么区别？它们分别对应哪种 BTU？

**参考答案**：`calorie_th = 4.184` J（热化学卡路里，用于热化学），`calorie_IT = 4.1868` J（国际蒸汽表卡路里，用于工程热力学）。它们分别对应 `Btu_th`（热化学 BTU）和 `Btu_IT`（国际蒸汽表 BTU）。

---

### 4.3 由 CODATA 值派生的换算因子

#### 4.3.1 概念说明

上一节看到的常量都是"自给自足"的字面量。但有一批换算因子**必须依赖 CODATA 物理常数**才能定义，因为它们的定义本身就包含某个测量值。最典型的有：

- **标准大气压** `atm`：定义为 101325 Pa，但代码里直接从 CODATA 取 `standard atmosphere` 的值。
- **光年** `light_year`：等于光速 × 儒略年长度，而光速 `c` 来自 CODATA。
- **磅每平方英寸** `psi`：1 psi = 1 磅的力作用在 1 平方英寸上 = `pound * g / inch**2`，其中标准重力加速度 `g` 来自 CODATA。
- **磅力** `lbf`、**千克力** `kgf`、**机械马力** `horsepower`：都用到 `g`。
- **原子质量** `m_e`、`m_p` 等：本身就是 CODATA 测量值。

这些常量之所以走 `_cd(...)` 而非写死字面量，是为了**让数值与 CODATA 推荐值保持一致并随版本更新**——当 SciPy 升级 CODATA 数据集时，这些派生因子会自动跟着变。

#### 4.3.2 核心流程

派生常量的构造方式有两种：

1. **直接取值**：常量就是某个 CODATA 物理常数的数值本身，如 `atm = _cd('standard atmosphere')`。
2. **组合派生**：用 CODATA 常数参与算术，如 `light_year = Julian_year * c`、`psi = pound * g / (inch * inch)`。

第二种方式体现了换算因子可以**混合字面量和 CODATA 值**——`pound`、`inch` 是字面量链，`g` 是 CODATA 值，它们组合在一起得到新的换算因子。

#### 4.3.3 源码精读

**直接取值的派生常量（压力类）**：

[_constants.py:L176-L180](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L176-L180)

```python
# pressure in pascal
atm = atmosphere = _cd('standard atmosphere')
bar = 1e5
torr = mmHg = atm / 760
psi = pound * g / (inch * inch)
```

- `atm` 直接取 CODATA 的 `standard atmosphere`（= 101325 Pa）。
- `bar = 1e5` 是纯字面量。
- `torr = mmHg = atm / 760`：1 标准大气压 = 760 托，所以用 `atm / 760` 得到 1 托的帕斯卡值。这里 `torr` 间接依赖了 CODATA（通过 `atm`）。
- `psi = pound * g / (inch * inch)`：1 psi 是 1 磅力分布在 1 平方英寸上的压强。磅力 = `pound * g`（质量 × 重力加速度），面积 = `inch * inch`，所以 `psi = pound * g / inch**2`，依赖 CODATA 的 `g`。

**组合派生（长度类）**：

[_constants.py:L173](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L173)

```python
light_year = Julian_year * c
```

光年 = 光在儒略年里走的距离 = 儒略年秒数 × 光速。`Julian_year` 是字面量（365.25 天），`c` 来自 CODATA（= 299792458 m/s）。

**组合派生（力与功率类）**：

[_constants.py:L217-L223](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L217-L223)

```python
# power in watt
hp = horsepower = 550 * foot * pound * g

# force in newton
lbf = pound_force = pound * g
kgf = kilogram_force = g  # * 1 kg
```

- `hp = 550 * foot * pound * g`：机械马力定义为 550 英尺·磅力/秒。`foot * pound * g` 得到英尺·磅力（功），隐含"每秒"对应瓦特。`g` 来自 CODATA。
- `lbf = pound * g`：1 磅力 = 1 磅质量 × 标准重力加速度。
- `kgf = g`：注释 `# * 1 kg` 提示"千克力 = 1 kg × g"，因为 1 kg 质量在标准重力下的力 = `1 * g`，所以 `kgf` 的数值就等于 `g`。

**CODATA 本身的物理常数（作为派生因子的"原料"）**：

[_constants.py:L141-L144](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L141-L144)

```python
m_e = electron_mass = _cd('electron mass')
m_p = proton_mass = _cd('proton mass')
m_n = neutron_mass = _cd('neutron mass')
m_u = u = atomic_mass = _cd('atomic mass constant')
```

这些粒子质量本身就是 CODATA 推荐值，放在"质量"这一类里。它们是测量值（不是字面量），会随 CODATA 版本更新。

> 小结：判断一个常量是否"依赖 CODATA"，看它定义里有没有出现 `_cd(...)` 或引用了 `_cd` 派生的常量（`c`、`g`、`atm` 等）。其余都是纯字面量。

#### 4.3.4 代码实践

**实践目标**：验证几个 CODATA 派生换算因子，并理解它们的构造。

**操作步骤**：

```python
from scipy.constants import (
    atm, torr, mmHg, psi, pound, g, inch,
    light_year, Julian_year, c,
    lbf, kgf, horsepower, hp, foot,
)

# 1. atm 是 CODATA 取值；torr 由 atm 派生
print("1 atm =", atm, "Pa")             # 101325.0
print("1 torr =", torr, "Pa")           # atm / 760 ≈ 133.322
print("torr == mmHg:", torr == mmHg)    # True（同一常量的两个名字）

# 2. psi 由 pound、g、inch 组合派生
print("1 psi =", psi, "Pa")
print("psi reconstructed =", pound * g / (inch * inch))  # 应与 psi 完全相等

# 3. light_year = Julian_year * c
print("1 light_year =", light_year, "m")
print("light_year reconstructed =", Julian_year * c)     # 应与 light_year 完全相等

# 4. kgf 的数值就等于 g
print("kgf =", kgf, "N")
print("g   =", g, "m/s^2")
print("kgf == g:", kgf == g)            # True
```

**需要观察的现象**：

- `psi` 与手工重构 `pound * g / (inch * inch)` **完全相等**（同一表达式）。
- `light_year` 与 `Julian_year * c` **完全相等**。
- `kgf` 与 `g` **完全相等**，印证"千克力 = 1 kg × g"。
- `torr == mmHg` 为 `True`，因为 `torr = mmHg = atm / 760` 是多重赋值。

**预期结果**：

- `1 atm = 101325.0 Pa`
- `1 torr ≈ 133.322 Pa`
- `1 psi ≈ 6894.76 Pa`
- `1 light_year ≈ 9.4607e15 m`
- `kgf = g ≈ 9.80665`（数值相等，但单位含义不同：一个是 N，一个是 m/s²）

这些值由源码表达式与 CODATA 2022 值推得，建议在本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `atm` 要用 `_cd('standard atmosphere')` 取值，而 `bar` 直接写 `1e5`？

**参考答案**：`bar` 是人为定义的精确值（1 bar = 10⁵ Pa，无歧义），所以用字面量。`atm`（标准大气压）传统上由 CODATA 维护其推荐值（= 101325 Pa），用 `_cd` 取值可以与 CODATA 数据集保持一致并随版本更新（虽然这里数值恰好是精确的 101325）。

**练习 2**：`kgf` 和 `g` 的数值相等，但它们是同一个物理量吗？

**参考答案**：不是。`g` 是标准重力加速度（单位 m/s²），`kgf` 是千克力（单位 N）。它们数值相等是因为 1 kg 质量在标准重力下受力 `1 × g` N，即 `kgf = 1 * g`。这是量纲不同但数值相同，使用时要注意。

---

### 4.4 温度的差值类常量 zero_Celsius 与 degree_Fahrenheit

#### 4.4.1 概念说明

温度是本子包里**唯一不能用简单乘法换算**的物理量，因为不同温标的零点不同（摄氏 0℃ = 273.15 K，华氏 32℉ = 0℃）。所以温度换算有专门的函数 `convert_temperature`（留到 [u3-l1](u3-l1-convert-temperature.md) 讲）。

但在 `_constants.py` 的温度段里，仍定义了两个温度相关常量：

[_constants.py:L203-L205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L203-L205)

```python
# temperature in kelvin
zero_Celsius = 273.15
degree_Fahrenheit = 1/1.8  # only for differences
```

关键是注释里的 `# only for differences`——这两个常量**只用于温度差值（temperature difference）的换算，不能用于温度本身的换算**。

什么叫"温度差值"？比如：

- "升高 10℃" 等价于 "升高 10 K"（温差，零点不重要）。
- "升高 18℉" 等价于 "升高 10 K"（因为华氏温标 1 度 = 5/9 K）。

但对"温度本身"就不同了：

- 10℃（温度本身）= 283.15 K，不是 10 K。
- 32℉（温度本身）= 0℃，不是 0 K。

#### 4.4.2 核心流程

温度差值换算的原理：

1. **摄氏温差 ↔ 开尔文温差**：刻度宽度相同，1℃ 的差 = 1 K 的差。所以温差直接相等，不需要常量。
2. **华氏温差 ↔ 开尔文/摄氏温差**：华氏 180 度对应摄氏 100 度，所以 1℉ 的差 = 5/9 K = 1/1.8 K。

数学上：

\[
\Delta T_{\text{K}} = \Delta T_{\text{℉}} \times \frac{5}{9} = \Delta T_{\text{℉}} \times \text{degree\_Fahrenheit}
\]

其中 `degree_Fahrenheit = 1/1.8 = 5/9`。

而 `zero_Celsius = 273.15` 只在"温度本身"的换算里用作偏移量（如 `T_K = T_℃ + zero_Celsius`），这正是 `convert_temperature` 里做的事，本讲暂不展开。

#### 4.4.3 源码精读

看这两个常量如何被使用。`convert_temperature` 函数在摄氏分支里用到了 `zero_Celsius`：

[_constants.py:L276-L281](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L276-L281)

```python
if old_scale.lower() in ['celsius', 'c']:
    tempo = _val + zero_Celsius          # 摄氏温度本身 → 开尔文：加偏移
elif old_scale.lower() in ['kelvin', 'k']:
    tempo = _val
elif old_scale.lower() in ['fahrenheit', 'f']:
    tempo = (_val - 32) * 5 / 9 + zero_Celsius  # 减零点偏移、乘刻度比、加开尔文偏移
```

可以看到，温度**本身**的换算既需要 `zero_Celsius`（零点偏移），又需要 `* 5/9`（刻度比，即 `degree_Fahrenheit` 的倒数关系）——这是非线性的仿射变换，无法用一个乘数表达。

而 `degree_Fahrenheit = 1/1.8` 这个常量，在 `_constants.py` 里其实没有直接用于温度本身换算（温度换算走的是 `convert_temperature` 的硬编码公式），它更多地服务于**能量单位 `Btu_th` 的定义**——见 [_constants.py:L212-L213](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L212-L213)：

```python
Btu_th = pound * degree_Fahrenheit * calorie_th / gram
Btu = Btu_IT = pound * degree_Fahrenheit * calorie_IT / gram
```

这里 `degree_Fahrenheit` 扮演的是"把华氏温标下的热容刻度换算到开尔文刻度"的角色，属于温差换算。BTU（英热单位）原始定义是"把 1 磅水升高 1℉ 所需热量"，用温差刻度比 `degree_Fahrenheit` 把华氏温差换算到开尔文温差后，再与卡路里/克组合，就得到了焦耳值。

> 重点记住：`zero_Celsius` 和 `degree_Fahrenheit` 都是**温差/偏移**用途，绝不要用 `T * degree_Fahrenheit` 去换算温度本身——那会得到错误结果。

#### 4.4.4 代码实践

**实践目标**：体会"温差"与"温度本身"的区别，并理解 `degree_Fahrenheit` 在能量单位定义中的角色。

**操作步骤**：

```python
from scipy.constants import (
    zero_Celsius, degree_Fahrenheit,
    Btu_th, pound, calorie_th, gram,
)

# 1. 温差换算：升高 18°F 对应升高多少 K
delta_F = 18.0
delta_K = delta_F * degree_Fahrenheit
print("升高", delta_F, "°F = 升高", delta_K, "K")   # 10.0 K

# 2. 反面教材：千万别用乘法换算温度本身！
T_F = 32.0  # 32°F 本应是 0°C = 273.15 K
print("错误做法 32 * degree_Fahrenheit =", T_F * degree_Fahrenheit, "（这不是开尔文温度！）")
print("正确做法应使用 convert_temperature（见 u3-l1）")

# 3. degree_Fahrenheit 在 Btu_th 定义中的角色
print("1 Btu_th =", Btu_th, "J")
print("Btu_th reconstructed =", pound * degree_Fahrenheit * calorie_th / gram)
print("两者相等：", Btu_th == pound * degree_Fahrenheit * calorie_th / gram)
```

**需要观察的现象**：

- `18 * degree_Fahrenheit = 10.0`（18℉ 温差 = 10 K 温差）。
- `32 * degree_Fahrenheit` 不是 273.15，证明温度本身不能乘 `degree_Fahrenheit`。
- `Btu_th` 与 `pound * degree_Fahrenheit * calorie_th / gram` **完全相等**（因为这就是它的定义）。

**预期结果**：

- `18°F 温差 = 10.0 K 温差`
- `1 Btu_th ≈ 1054.35 J`
- `Btu_th == pound * degree_Fahrenheit * calorie_th / gram` 为 `True`

以上数值由源码表达式推得，建议在本地运行确认。

#### 4.4.5 小练习与答案

**练习 1**：注释 `# only for differences` 的含义是什么？如果误用 `degree_Fahrenheit` 去换算"温度本身"会怎样？

**参考答案**：它表示 `degree_Fahrenheit` 只能用于**温度差值**的换算（1℉ 差 = 5/9 K 差）。温度本身的换算需要同时处理零点偏移和刻度比（仿射变换 `T_K = (T_F - 32) * 5/9 + 273.15`），不能只用乘法。若误用 `T_F * degree_Fahrenheit`，会忽略 32℉ 的零点偏移，得到错误结果。

**练习 2**：`degree_Fahrenheit = 1/1.8` 这个值从何而来？

**参考答案**：华氏温标把水的冰点到沸点分成 180 度（32℉→212℉），摄氏/开尔文分成 100 度（0℃→100℃）。所以 1℉ 的刻度宽度 = 100/180 K = 5/9 K = 1/1.8 K。

---

## 5. 综合实践

把本讲的换算因子串起来，完成一个"航海速度与能耗"的小计算。

**任务背景**：一艘船以 10 英里/分钟的速度航行（这是模块 docstring 里的示例速度），请你：

1. 把这个速度换算成 **m/s** 和**节（knot）**。
2. 假设该船每航行 1 海里消耗 1 磅燃料，每磅燃料的热值是 18000 BTU（热化学），请把每秒消耗的能量换算成**焦耳**和**瓦特（功率）**。
3. 验证 `Btu_th` 与其定义式 `pound * degree_Fahrenheit * calorie_th / gram` 数值一致。

**参考实现**：

```python
from scipy.constants import (
    mile, minute, knot, nautical_mile,
    Btu_th, pound, degree_Fahrenheit, calorie_th, gram,
)

# 1. 速度换算
v_ms = 10 * mile / minute              # m/s
v_knot = (10 * mile / minute) / knot   # 节
print("10 mile/min =", v_ms, "m/s")
print("10 mile/min =", v_knot, "knot")

# 2. 每秒能耗：每海里耗 1 磅燃料（18000 Btu_th/磅）
#    速度 v_ms m/s 对应每秒航行 v_ms / nautical_mile 海里
nautical_per_second = v_ms / nautical_mile      # 每秒航行多少海里
fuel_per_second = nautical_per_second * pound    # 每秒消耗多少磅燃料
energy_per_second = fuel_per_second * 18000 * Btu_th   # 每秒消耗多少焦耳
print("每秒能耗 =", energy_per_second, "W (= J/s)")

# 3. 验证 Btu_th 定义
print("Btu_th =", Btu_th, "J")
print("定义式 =", pound * degree_Fahrenheit * calorie_th / gram, "J")
print("一致：", Btu_th == pound * degree_Fahrenheit * calorie_th / gram)
```

**预期结果**：

- `10 mile/min ≈ 268.224 m/s`，约 `521.49 knot`。
- `Btu_th ≈ 1054.35 J`，与定义式完全相等。
- 每秒能耗约为：`268.224 / 1852 ≈ 0.1448` 海里/秒 → 约 `0.1448 磅/秒` → `0.1448 * 18000 * 1054.35 ≈ 2.747e6 W`。

第 1 步的 `268.224 m/s` 和 `Btu_th ≈ 1054.35 J` 由源码表达式精确推得；综合能耗结果建议在本地运行确认。

> 说明：第 3 步的"验证"本质上是把 `Btu_th` 拆回它的定义式 `pound * degree_Fahrenheit * calorie_th / gram`（见 [_constants.py:L212](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L212)）。两者必然完全相等，因为 `Btu_th` 就是由该表达式赋值的。这一步的意义不在于"独立检验"，而在于让你看清 `Btu_th` 是如何由"质量 × 温差刻度 × 单位质量热容"三类常量组合构造出来的——这正是 4.2、4.3、4.4 三个小节知识的汇合点。

---

## 6. 本讲小结

- `scipy.constants` 的换算因子都统一指向 **SI 基本单位**，常量本身就是一个乘数，可直接用 `*`、`/` 做换算（如 `10 * mile / minute`）。
- 换算因子**按物理量类别分组**（质量、长度、时间、压力、面积、体积、速度、能量、功率、力），每段用注释标明 SI 单位。
- 大量常量采用**链式定义**（`mile = 1760 * yard`，`yard = 3 * foot`…），改一处 `inch` 即可联动整条链，体现单一数据源思想。
- 少数换算因子**依赖 CODATA 物理常数**，通过 `from ._codata import value as _cd` 引入：`atm`、`psi`、`light_year`、`lbf`、`hp`、粒子质量等都用到 `_cd` 或它派生的 `c`、`g`。
- 温度常量 `zero_Celsius`（= 273.15）和 `degree_Fahrenheit`（= 1/1.8）**只用于温度差值/偏移**，不能用于温度本身的换算；温度本身换算走非线性函数 `convert_temperature`（见 u3-l1）。
- 判断一个常量是否依赖 CODATA：看它定义里是否出现 `_cd(...)` 或引用了 `c`、`g`、`atm` 等 CODATA 派生值。

---

## 7. 下一步学习建议

- **进入 u2-l1（physical_constants 数据库与查找 API）**：本讲多次用到 `_cd(...)` 即 `value()`，下一讲将深入 `_codata.py`，讲解 `physical_constants` 字典的三元组结构 `(value, unit, uncertainty)`，以及 `value` / `unit` / `precision` / `find` 四个查找函数。
- **阅读 `_codata.py` 的 `value` 函数**：位于 [_codata.py:L2130](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2130)，看看它如何从字典里取值并检查废弃常量。
- **对比 `convert_temperature`**：本讲强调了温度不能用乘法换算，可以提前扫一眼 [_constants.py:L229-L302](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_constants.py#L229-L302)，理解它如何用 `zero_Celsius` 做偏移、用 `* 5/9` 做刻度比，为 u3-l1 做铺垫。
- **动手扩展**：试着在 REPL 里用 `find('atmosphere')`（下一讲内容）查看与大气压相关的所有 CODATA 键，理解 `atm` 这个常量背后的数据来源。
