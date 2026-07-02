# 精确值与派生常数的计算

> 本讲承接 [u2-l2 CODATA 文本数据的固定列宽解析](u2-l2-codata-text-parsing.md)。
> 上一讲我们看清了 `_codata.py` 如何把定宽文本切成「名称 / 数值 / 不确定度 / 单位」四列，
> 并按 `(exact)` 与 `...` 两个标记把每行归入「未截断精确 / 截断精确 / 普通测量值」三类。
> 本讲要回答的核心问题是：**那些标记为精确、却因为文本里写着 `8.617 333 262...` 而丢失了尾数的常数，怎么把全精度补回来？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清「为什么 CODATA 文本里精确常数会出现 `...` 截断」，以及这种截断为何会伤害计算精度。
2. 读懂 `exact2018` 函数：它如何从五个 SI 基本定义常数 \(c, h, e, k, N_A\) 推导出几十个派生常数的全精度值。
3. 读懂 `replace_exact`：它用「相对误差 ≤ 1e-9」和「键集合相等」两道断言，保证回填值既正确又完整。
4. 理解 `test_gh14467` 这个回归测试修复的历史 bug——本应精确的常数被截断到只有约 10 位有效数字。
5. 了解 `exact2002 / exact2006 / exact2010 / exact2014 / exact2022` 这些历史/别名版本函数是如何演进的。

## 2. 前置知识

在进入源码之前，先建立两个直觉。

### 2.1 SI 2019 重新定义：「定义常数」与「测量常数」

2019 年 5 月 20 日起，国际单位制（SI）用 **七个固定数值的常数** 来定义千克、安培、开尔文、摩尔等单位。其中与常数表关系最密切的五个是：

| 符号 | 名称 | 定义值 |
|------|------|--------|
| \(c\) | 真空光速 `speed of light in vacuum` | 恰好 299 792 458 m/s |
| \(h\) | 普朗克常数 `Planck constant` | 恰好 6.626 070 15 × 10⁻³⁴ J·Hz⁻¹ |
| \(e\) | 元电荷 `elementary charge` | 恰好 1.602 176 634 × 10⁻¹⁹ C |
| \(k\) | 玻尔兹曼常数 `Boltzmann constant` | 恰好 1.380 649 × 10⁻²³ J/K |
| \(N_A\) | 阿伏伽德罗常数 `Avogadro constant` | 恰好 6.022 140 76 × 10²³ mol⁻¹ |

它们不是「测量出来的」，而是**人为规定**的——不确定度为 0。我们称之为**定义常数（defining constants）**。

而像「玻尔兹曼常数用电子伏特/开尔文表示」「冯·克利青常数」「斯特藩-玻尔兹曼常数」等，虽然它们的数值在理论上完全由上面的定义常数算出，因而不确定度也是 0，但它们是**派生的**。

### 2.2 矛盾：精确值却被写成 `...`

理论上，`Boltzmann constant in eV/K` = \(k/e\)。既然 \(k\) 和 \(e\) 都精确定义了，这个比值也应当精确。可是在 NIST 给出的 CODATA 文本里，它被写成：

```
Boltzmann constant in eV/K     8.617 333 262... e-5     (exact)     eV K^-1
```

数值列末尾的 `...` 意思是「后面还有小数，但这里写不下了，只给到约 10 位有效数字」。如果直接 `float("8.617333262e-5")` 存下来，就只有约 10 位精度，丢掉了双精度浮点本应能承载的 15～16 位。

**这就是本讲要解决的矛盾**：用一个被截断的字符串去表示一个本应精确的数，会损失精度。解决办法是——别信那个被截断的字符串，**用定义常数把它重新算出来**。

> 上一讲已经介绍了「列切片」如何把 `8.617 333 262...` 解析为 `8.617333262e-5`、把 `(exact)` 解析为不确定度 0。本讲聚焦于解析之后的那一步「精修」：把被截断的精确值替换成全精度推导值。

## 3. 本讲源码地图

本讲几乎全部围绕同一个文件 [`_codata.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py)。涉及的关键位置：

| 位置 | 作用 |
|------|------|
| [`_codata.py:1535-1628`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1535-L1628) | `exact2018`：用定义常数推导派生精确值，返回 `replace` 字典 |
| [`_codata.py:1989`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1989) | `exact2022 = exact2018`：2022 数据集复用同一套推导 |
| [`_codata.py:2047-2053`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2047-L2053) | `replace_exact`：执行回填，并用两道断言把关 |
| [`_codata.py:1995-2018`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1995-L2018) 与 [`_codata.py:2021-2044`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2021-L2044) | 两个 `parse_constants_*`：在解析末尾调用 `exact_func` + `replace_exact` |
| [`_codata.py:2056-2071`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2056-L2071) | 把六个版本的解析结果挂起来、合并、定当前数据集 |
| [`_codata.py:144-148`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L144-L148)、[`_codata.py:480-493`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L480-L493)、[`_codata.py:834`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L834)、[`_codata.py:1175`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1175) | `exact2002 / exact2006` 及 `exact2010 = exact2006`、`exact2014 = exact2010` 的演进 |
| [`tests/test_codata.py:53-59`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L53-L59)、[`tests/test_codata.py:71-78`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L71-L78) | `test_exact_values` 与 `test_gh14467` 回归测试 |

## 4. 核心概念与源码讲解

### 4.1 为什么要「精确值回填」——精确却遭截断的矛盾

#### 4.1.1 概念说明

CODATA 文本里，一个常数行可能同时带有两个标记：

- 数值列里的 `...`：表示数值被**截断**（truncated），真实值还有更多位没写出。
- 不确定度列里的 `(exact)`：表示该常数在物理上是**精确的**，不确定度为 0。

这两个标记本不该同时出现——既然精确，就该给出全部有效数字。但 NIST 的文本为了排版，对很多派生精确常数只保留了约 10 位，再加个 `...` 表示「省略」。这就产生了一类特殊的常数：**精确但被截断**。

如果直接把截断字符串 `8.617 333 262...` 解析成浮点数，你会得到 `8.617333262e-5`，只剩约 10 位有效数字，而 IEEE 754 双精度本可承载约 15～16 位。对绝大多数应用无所谓，但对「物理常数表」这种以精度为生命的工具，这是不可接受的精度损失——尤其当用户拿这些值做精密计算时，误差会层层放大。

#### 4.1.2 核心流程

解决思路可以归纳为一句话：**别截断，去推导。**

1. 解析文本时，把常数分成三类（详见上一讲）：
   - 未截断的精确常数（如 `c, h, e, k, N_A`，定义值已全部写出）→ 作为**原料**收入 `exact` 字典。
   - 截断的精确常数（如 `Boltzmann constant in eV/K`）→ 暂存为待回填集合 `need_replace`。
   - 普通测量值 → 原样保留。
2. 调用一个 `exact_func`（如 `exact2018`），把 `exact` 里的原料喂进去，算出所有派生精确常数的**全精度值**，返回 `replace` 字典。
3. 调用 `replace_exact`，用 `replace` 里的全精度值替换掉 `need_replace` 里那些被截断的值，并用断言校验。

伪代码：

```
exact = {}              # 原料：未截断的精确常数
need_replace = set()    # 待回填：截断的精确常数
for line in txt:
    解析 name / val / uncert / units
    if 数值被截断 and 标记为精确:
        need_replace.add(name)
    elif 标记为精确:        # 未截断
        exact[name] = val   # 当原料
    else:
        断言没有被截断       # 普通测量值不应带 ...

replace = exact_func(exact)        # 由原料推导出全精度派生值
replace_exact(constants, need_replace, replace)   # 回填 + 校验
```

#### 4.1.3 源码精读：解析末尾的两行接线

`parse_constants_2018toXXXX`（2018 及之后版本用）的循环体我们已经熟悉，关注它结尾的两行：

```python
# [_codata.py:2021-2044] parse_constants_2018toXXXX 末尾
        ...
        uncert = float(line[85:110].replace(' ', '').replace('(exact)', '0'))
        units = line[110:].rstrip()
        constants[name] = (val, units, uncert)
    replace = exact_func(exact)          # 步骤 2：由原料推导全精度值
    replace_exact(constants, need_replace, replace)   # 步骤 3：回填并断言
    return constants
```

这里 `exact_func` 是作为参数注入的——对不同 CODATA 版本传入不同的函数（`exact2002 / exact2006 / exact2018 …`）。这就是上一讲提到的「把精确值推导与解析解耦」的设计：解析器只管切列、归类，**怎么算派生值**交给具体版本的 `exact_func`。

#### 4.1.4 代码实践

> **实践目标**：亲眼看到「截断精确常数」在原始文本里的样子，并理解它如何同时满足两个条件。

**操作步骤**：

1. 打开 [`_codata.py` 的 txt2022 文本块](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1631)，找到这一行（[`_codata.py:1683`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1683)）：

   ```
   Boltzmann constant in eV/K     8.617 333 262... e-5     (exact)     eV K^-1
   ```

2. 对照 [`_codata.py:1682`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1682) 的原料 `Boltzmann constant` 和 [`_codata.py:1752`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1752) 的 `elementary charge`，注意它们数值列都没有 `...`，所以是「未截断的精确常数」，会成为推导原料。

**需要观察的现象**：`Boltzmann constant in eV/K` 的数值列同时含有 `...`（截断）和不确定度列含 `(exact)`（精确）——这正是 `need_replace` 集合要捕捉的对象。

**预期结果**：你能指出该行同时满足 `is_truncated=True` 且 `is_exact=True`，因此会被加入待回填集合。

#### 4.1.5 小练习与答案

**练习 1**：在 txt2022 里，`atomic unit of action`（[`_codata.py:1654`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1654)）会被归入哪一类？它是原料还是待回填？

> **参考答案**：该行数值为 `1.054 571 817... e-34`、不确定度为 `(exact)`，同时含 `...` 和 `(exact)`，所以是「截断的精确常数」，归入 `need_replace`（待回填），不是原料。

**练习 2**：为什么 `speed of light in vacuum`（[`_codata.py:1950`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1950)）会被当作「原料」而不是「待回填」？

> **参考答案**：它的数值 `299 792 458` 是整数、不含 `...`，但不确定度列是 `(exact)`，满足 `is_exact` 且 `not is_truncated`，因此进入 `exact` 字典作为原料，供 `exact2018` 读取。

---

### 4.2 exact2018：用五个定义常数推导派生精确值

#### 4.2.1 概念说明

`exact2018` 是整个机制的核心。它接收一个 `exact` 字典（原料：所有未截断的精确常数），返回一个 `replace` 字典（键 = 待回填常数名，值 = 全精度推导值）。它的关键设计是：

- 只真正依赖 **五个 SI 定义常数** \(c, h, e, k, N_A\)（外加两个「约定电学单位」`K_J90 / R_K90` 作输入），其它一切都由这五个常数算出来。
- 所有推导都用 Python 的浮点运算（双精度），因而能给出约 15～16 位有效数字——这正是被 `...` 截断所丢失的精度。

它解决的问题是：**用确定的代数关系，把被截断的精确常数还原到全精度。**

#### 4.2.2 核心流程

`exact2018` 的内部结构很清晰，分四步：

1. **取原料**：从 `exact` 字典取出五个定义常数及两个约定电学单位。
2. **定义中间量**：组合出 `R = N_A·k`（摩尔气体常数）、`hbar = h/(2π)`、`G_0 = 2e²/h`（电导量子）等。
3. **算派生值**：用这些量算出几十个常数的全精度值，组装成 `replace` 字典。
4. **返回** `replace`。

典型推导关系（数学表达）：

\[ R = N_A \cdot k \qquad \hbar = \frac{h}{2\pi} \qquad G_0 = \frac{2e^2}{h} \]

\[ \frac{k}{e}\;\text{（玻尔兹曼常数 in eV/K）}\qquad \frac{h}{e}\;\text{（普朗克常数 in eV/Hz）} \]

\[ \sigma = \frac{2\pi^5 k^4}{15 h^3 c^2}\;\text{（斯特藩-玻尔兹曼常数）} \]

其中斯特藩-玻尔兹曼常数是最复杂的派生之一，同时依赖 \(k, h, c\) 三个定义常数。

#### 4.2.3 源码精读

先看函数签名与原料提取（[`_codata.py:1535-1541`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1535-L1541)）——这五取就是「定义常数」的来源：

```python
def exact2018(exact):
    # SI base constants
    c = exact['speed of light in vacuum']
    h = exact['Planck constant']
    e = exact['elementary charge']
    k = exact['Boltzmann constant']
    N_A = exact['Avogadro constant']
```

> 这里的 `exact[...]` 拿到的就是上一节说的「未截断精确常数」原料——它们的定义值在文本里已完整写出（无 `...`），所以浮点精度无损。

再看中间量与约定电学单位（[`_codata.py:1544-1562`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1544-L1562)）：

```python
    # Other useful constants
    R = N_A * k
    hbar = h / (2*math.pi)
    G_0 = 2 * e**2 / h
    ...
    K_J90 = exact['conventional value of Josephson constant']
    K_J = 2 * e / h
    R_K90 = exact['conventional value of von Klitzing constant']
    R_K = h / e**2
    V_90 = K_J90 / K_J
    ohm_90 = R_K / R_K90
    A_90 = V_90 / ohm_90
```

> `math` 在文件顶部导入（[`_codata.py:55`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L55)）。

最后是 `replace` 字典——把每个「截断精确常数」的名字映射到它的全精度推导值。挑几条与本讲实践任务直接相关的看（[`_codata.py:1564-1628`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1564-L1628)）：

```python
    replace = {
        'atomic unit of action': hbar,
        'Boltzmann constant in eV/K': k / e,           # ← 本讲实践主角
        'Boltzmann constant in Hz/K': k / h,
        'Boltzmann constant in inverse meter per kelvin': k / (h * c),
        'conductance quantum': G_0,                     # = 2*e**2/h
        ...
        'Stefan-Boltzmann constant': 2 * math.pi**5 * k**4 / (15 * h**3 * c**2),
        ...
        'Wien wavelength displacement law constant': h * c / (x_W * k),
    }
    return replace
```

读这段要抓住一个模式：**左边是「被截断的精确常数名」，右边是「由定义常数表达的代数关系」**。例如：

- `'Boltzmann constant in eV/K': k / e` —— 把 J/K 换成 eV/K，就是除以元电荷 \(e\)。
- `'Stefan-Boltzmann constant'` —— 严格物理公式 \(\sigma = 2\pi^5 k^4/(15 h^3 c^2)\) 一字不差地落在代码里。

另有两类派生值得注意：

- **Wien 位移定律常数**（[`_codata.py:1551-1552`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1551-L1552)）涉及超越方程的根 `alpha_W`、`x_W`，代码注释里直接给出了它们的 Lambert W 函数来源，这是少数「不能纯代数推导、需引入数值常数」的派生。
- **约定电学单位（`*-90` 系列）**：1990 年代采用的实验约定值 `K_J90 / R_K90`，与 SI 重定义后的 `K_J / R_K` 之比，给出 volt-90、ohm-90 等换算因子。

#### 4.2.4 代码实践

> **实践目标**：亲手用 `exact2018` 的思路，验证 `'Boltzmann constant in eV/K'` 的全精度值等于 \(k/e\)，并与数据库里的值对照。

**操作步骤**（在仓库根目录运行 Python）：

```python
# 示例代码：复现 exact2018 对 'Boltzmann constant in eV/K' 的推导
from scipy.constants import value, precision, physical_constants
import scipy.constants._codata as _cd

# 1) 取两个定义常数原料（它们在文本里是未截断的精确值）
k = value('Boltzmann constant')        # 1.380649e-23 J/K
e = value('elementary charge')         # 1.602176634e-19 C

# 2) 按 exact2018 的公式手算
derived = k / e                         # 对应 replace['Boltzmann constant in eV/K']

# 3) 与数据库里（已被 replace_exact 回填过）的值比较
stored = physical_constants['Boltzmann constant in eV/K'][0]

print("derived k/e =", derived)
print("stored      =", stored)
print("相等？", derived == stored)
print("precision   =", precision('Boltzmann constant in eV/K'))   # 期望 0.0
```

**需要观察的现象**：

1. `derived == stored` 应为 `True`——因为数据库里的值正是由 `k/e` 全精度算出并回填的，二者逐位相同。
2. `precision('Boltzmann constant in eV/K')` 应为 `0.0`——它是精确常数，不确定度为 0，故 `precision = uncertainty/value = 0`。

**预期结果**：

```
derived k/e = 8.617333262145...e-05
stored      = 8.617333262145...e-05
相等？ True
precision   = 0.0
```

注意全精度值是 `8.617333262145...e-05`（约 16 位），而截断文本只给了 `8.617 333 262... e-5`（约 10 位）。**这多出来的 5～6 位有效数字，正是回填机制找回的精度。**

> 若本地环境未编译安装 SciPy，运行结果待本地验证；也可直接阅读上面源码确认逻辑。

#### 4.2.5 小练习与答案

**练习 1**：用 `exact2018` 的源码回答：`'conductance quantum'` 的推导公式是什么？它依赖哪几个定义常数？

> **参考答案**：`G_0 = 2 * e**2 / h`（[`_codata.py:1546`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1546)），只依赖 \(e\) 和 \(h\) 两个定义常数。

**练习 2**：`'molar gas constant'`（摩尔气体常数）在 SI 2019 后为什么也是精确的？写出它的推导式。

> **参考答案**：因为 \(R = N_A \cdot k\)（[`_codata.py:1544`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1544)），而 \(N_A\) 和 \(k\) 都是定义常数，故 \(R\) 也精确，replace 字典里 `'molar gas constant': R`。

**练习 3**：`replace` 字典里 `'Boltzmann constant in Hz/K'` 的值是 `k / h`。请解释这个比值的物理含义。

> **参考答案**：它表示「每开尔文对应多少赫兹频率」，由 \(E = h\nu\) 与 \(E = kT\) 联立得 \(\nu/T = k/h\)，故单位是 Hz/K。

---

### 4.3 replace_exact：两道断言如何守住正确性与完整性

#### 4.3.1 概念说明

`exact2018` 只是「给出了一份替换清单」。真正执行替换、并验证替换没出错的是 `replace_exact`。它的代码只有 7 行，却承担了两项关键校验：

- **正确性**：每个推导值，必须与文本里那个被截断的值「足够接近」（相对误差 ≤ 1e-9）。
- **完整性**：推导清单的键集合，必须与「待回填」集合**完全相等**——既不能漏算一个，也不能多算一个。

这两道断言是整套机制的「安全网」：如果有人写错了推导公式，或漏掉/多加了某个常数，模块在导入时就会直接抛 `AssertionError`，而不会静默地把错误数据塞进 `physical_constants`。

#### 4.3.2 核心流程

`replace_exact(d, to_replace, exact)` 三个参数：

- `d`：完整的常数字典（键→`(value, unit, uncertainty)`），即解析器维护的 `constants`。
- `to_replace`：待回填集合，即解析器里的 `need_replace`（截断的精确常数名）。
- `exact`：`exact_func` 返回的 `replace` 字典（推导出的全精度值）。注意这里的形参名 `exact` 与 `exact2018` 的入参 `exact` 是**不同的东西**，别混淆。

逻辑：

```
for name in to_replace:
    断言 name 出现在 replace 中                 # ① 必须被推导覆盖
    断言 |replace[name] / d[name].value - 1| <= 1e-9   # ② 相对误差足够小
    用 replace[name] 替换 d[name] 的数值（保留单位与不确定度）
断言 set(replace 的键) == set(to_replace)         # ③ 完整且无多余
```

第二道断言的数学形式：

\[ \left| \frac{v_{\text{推导}}}{v_{\text{截断}}} - 1 \right| \le 10^{-9} \]

阈值 \(10^{-9}\) 的含义：截断文本至少保留了约 10 位有效数字（相对 \(10^{-10}\) 量级误差），所以正确的推导值与它的相对差异应远小于 \(10^{-9}\)。一旦公式写错，差异通常是 \(10^{-2}\) 量级或更大，断言立刻失败。

第三道断言 `set(exact.keys()) == set(to_replace)` 是双向的完整性检查：

- 若 `replace` 缺了某个待回填键 → 第一道 `assert name in exact` 先拦下。
- 若 `replace` 多了某个不在 `to_replace` 里的键 → 第三道集合相等断言拦下（说明推导了一个本不该被替换的常数）。

#### 4.3.3 源码精读

[`_codata.py:2047-2053`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2047-L2053)：

```python
def replace_exact(d, to_replace, exact):
    for name in to_replace:
        assert name in exact, f'Missing exact value: {name}'                       # ① 覆盖
        assert abs(exact[name]/d[name][0] - 1) <= 1e-9, \                          # ② 相对误差
            f'Bad exact value: {name}: { exact[name]}, {d[name][0]}'
        d[name] = (exact[name],) + d[name][1:]                                     # 回填：只换数值，留单位与不确定度
    assert set(exact.keys()) == set(to_replace)                                    # ③ 完整且无多余
```

要点逐行解读：

- 第 2 行 `assert name in exact`：每个待回填常数，`exact2018` 都必须给出推导值；否则报 `Missing exact value`。
- 第 3 行相对误差断言：`exact[name]` 是推导值，`d[name][0]` 是文本里被截断的值。注意是**推导值除以截断值**，比值与 1 的距离即相对误差。
- 第 5 行回填：`(exact[name],) + d[name][1:]` 表示「用新数值，拼上原来的单位与不确定度」。所以回填后该常数仍保持 `(value, unit, 0.0)` 三元组结构，且不确定度仍是 0。
- 第 6 行集合相等：最关键的完整性约束——`replace` 字典既不能漏也不能多。

> 一个细节：回填只动数值，不动单位。因为被截断的只是「数值的尾数」，单位本身是完整的，无需替换。

#### 4.3.4 代码实践

> **实践目标**：用源码阅读的方式，验证「相对误差 ≤ 1e-9」对 `Boltzmann constant in eV/K` 确实成立，并理解断言失败会怎样。

**操作步骤**：

1. 手算截断值与推导值的相对误差（可在本地运行确认）：

   ```python
   # 示例代码：模拟 replace_exact 第二道断言的检查
   truncated = 8.617333262e-5          # 文本里 "8.617 333 262..." 解析后的值
   derived   = 1.380649e-23 / 1.602176634e-19   # = k/e 全精度
   rel_err = abs(derived / truncated - 1)
   print("相对误差 =", rel_err, " 通过？", rel_err <= 1e-9)
   ```

2. 阅读断言失败分支：把上面 `derived` 故意改错（例如乘以 1.01），观察 `rel_err` 会跳到约 1e-2，远大于 1e-9，此时若在真实 `replace_exact` 中会抛 `AssertionError: Bad exact value ...`。

**需要观察的现象**：

- 正确推导时，相对误差应在 1e-11 量级（因为截断保留了约 10 位有效数字），远小于阈值 1e-9。
- 公式一旦写错，相对误差通常落在 1e-2 量级，断言立即拦截。

**预期结果**：

```
相对误差 = 1.7e-11（量级） 通过？ True
```

> 精确到多少位待本地验证；核心结论是「正确推导的相对误差远小于 1e-9，错误推导远大于 1e-9」，这正是阈值设在 1e-9 的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么相对误差阈值取 `1e-9`，而不是更严的 `1e-15`？

> **参考答案**：截断文本只保留约 10 位有效数字，截断值本身的相对误差就在 1e-10～1e-11 量级。若阈值设成 1e-15，正确的推导值也会因「截断值的固有舍入」而误判为失败。1e-9 既远大于截断噪声（不会误杀正确值），又远小于典型公式错误（1e-2 量级，能可靠拦截），是合理的分界。

**练习 2**：第三道断言 `set(exact.keys()) == set(to_replace)` 中，如果 `exact2018` 的 `replace` 字典里多写了一个**未截断**的常数，会发生什么？

> **参考答案**：该键不在 `to_replace`（待回填集合）里，集合不等，断言失败。这防止了「用推导值覆盖一个本就完整、不该被替换的常数」，守住完整性。

**练习 3**：回填语句 `d[name] = (exact[name],) + d[name][1:]` 中，为什么用切片 `[1:]` 保留后两个元素，而不是直接 `d[name] = (exact[name], d[name][1], d[name][2])`？

> **参考答案**：两种写法等价，但 `[1:]` 更简洁、不依赖「三元组恰好三个元素」的硬编码下标，可读性更好。语义都是「换掉数值，保留单位与不确定度」。

---

### 4.4 解析流水线的接线与历史版本函数

#### 4.4.1 概念说明

前三节讲清了「单次回填」的机制。本节把它放回整体：六个 CODATA 版本（2002/2006/2010/2014/2018/2022）各自解析时，分别传入**对应版本**的 `exact_func`，再把六份结果合并成最终的 `physical_constants`。

历史版本函数（`exact2002 / exact2006 / …`）体现了 CODATA 的演进：早期版本里精确的常数很少，需要推导的也少；越靠近 SI 2019 重定义，精确常数越多，`exact_func` 的 `replace` 字典越长。

#### 4.4.2 核心流程

1. 每个版本文本 `txtYYYY` 配一个 `exactYYYY`，交给对应的 `parse_constants_*` 解析。
2. 解析器在内部调用 `exact_func(exact)` 与 `replace_exact(...)`，产出该版本完整的 `_physical_constants_YYYY`。
3. 用一系列 `physical_constants.update(_physical_constants_YYYY)` 依次合并六个版本，后覆盖前。
4. 把 CODATA 2022 的结果记为 `_current_constants`，作为「当前数据集」。

版本别名关系：

```
exact2010 = exact2006     # 2010 复用 2006 的推导
exact2014 = exact2010     # 2014 复用 2010（等价于 2006）
exact2022 = exact2018     # 2022 复用 2018 的推导（SI 重定义后稳定）
```

#### 4.4.3 源码精读

先看版本挂载与合并（[`_codata.py:2056-2071`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2056-L2071)）——每个版本传入自己的 `exact_func`：

```python
_physical_constants_2002 = parse_constants_2002to2014(txt2002, exact2002)
_physical_constants_2006 = parse_constants_2002to2014(txt2006, exact2006)
_physical_constants_2010 = parse_constants_2002to2014(txt2010, exact2010)
_physical_constants_2014 = parse_constants_2002to2014(txt2014, exact2014)
_physical_constants_2018 = parse_constants_2018toXXXX(txt2018, exact2018)
_physical_constants_2022 = parse_constants_2018toXXXX(txt2022, exact2022)

physical_constants = {}
physical_constants.update(_physical_constants_2002)
...
physical_constants.update(_physical_constants_2022)
_current_constants = _physical_constants_2022
```

注意：2002～2014 用 `parse_constants_2002to2014`（列宽 55/22/22），2018～2022 用 `parse_constants_2018toXXXX`（列宽 60/25/25）。列宽的变化在上一讲解释过，源于 SI 重定义后精确常数有效数字增多。**但两个解析器末尾都同样调用 `exact_func` + `replace_exact`**，回填机制是共用的。

再看版本别名（[`_codata.py:834`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L834)、[`_codata.py:1175`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1175)、[`_codata.py:1989`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1989)）：

```python
exact2010 = exact2006
exact2014 = exact2010
...
exact2022 = exact2018
```

> 这些是直接的函数对象赋值（不是调用），表示「该版本的推导逻辑与被引用版本完全相同」。`exact2010/2014` 都等价于 `exact2006`，`exact2022` 等价于 `exact2018`。

最后对比历史版本的「规模差异」。`exact2002` 几乎空空如也，只回填一个常数（[`_codata.py:144-148`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L144-L148)）：

```python
def exact2002(exact):
    replace = {
        'magn. constant': 4e-7 * math.pi,
    }
    return replace
```

`exact2006` 稍多几个，且依赖 `c`（[`_codata.py:480-493`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L480-L493)）：

```python
def exact2006(exact):
    mu0 = 4e-7 * math.pi
    c = exact['speed of light in vacuum']
    epsilon0 = 1 / (mu0 * c**2)
    replace = {
        'mag. constant': mu0,
        'electric constant': epsilon0,
        'atomic unit of permittivity': 4*math.pi*epsilon0,
        'characteristic impedance of vacuum': math.sqrt(mu0 / epsilon0),
        'hertz-inverse meter relationship': 1/c,
        'joule-kilogram relationship': 1/c**2,
        'kilogram-joule relationship': c**2,
    }
    return replace
```

> 2002/2006 时代，SI 还没重定义，\(c\) 已精确但 \(h, e, k, N_A\) 仍是测量值，所以能精确推导的常数极少——`exact2002` 只有 1 个，`exact2006` 只有 7 个。而到了 SI 2019 后的 `exact2018`，五个定义常数全部精确，`replace` 字典一下膨胀到 60+ 条。这就是版本演进的核心脉络：**精确常数随 SI 重定义而大幅增多**。

#### 4.4.4 代码实践

> **实践目标**：理解 `test_gh14467` 这个回归测试修复了什么，并用 `test_exact_values` 验证整套回填机制。

**操作步骤**：

1. 阅读 [`tests/test_codata.py:71-78`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L71-L78) 的 `test_gh14467`：

   ```python
   def test_gh14467():
       # gh-14467 noted that some physical constants in CODATA are rounded
       # to only ten significant figures even though they are supposed to be
       # exact. Check that (at least) the case mentioned in the issue is resolved.
       res = constants.physical_constants['Boltzmann constant in eV/K'][0]
       ref = (constants.physical_constants['Boltzmann constant'][0]
              / constants.physical_constants['elementary charge'][0])
       assert res == ref
   ```

2. 运行回归测试与精确值测试（待本地验证）：

   ```bash
   python -m pytest scipy/constants/tests/test_codata.py::test_gh14467 \
                     scipy/constants/tests/test_codata.py::test_exact_values -v
   ```

**需要观察的现象 / 预期结果**：

- `test_gh14467` 断言 `res == ref`：数据库里 `'Boltzmann constant in eV/K'` 的存储值，**逐位等于** `k/e` 的全精度比值。这正是 `replace_exact` 回填的功劳——若没有回填，`res` 会停在截断值 `8.617333262e-5`，与全精度 `ref` 不相等，断言失败。
- `test_exact_values`（[`tests/test_codata.py:53-59`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L53-L59)）的做法更彻底：它重新拿 2018 的原料跑一遍 `exact2018`，再逐一比对数据库里的值，并断言每个的 `precision == 0`。

**gh-14467 修复的问题，一句话总结**：本应精确（不确定度为 0）的派生常数，因为 CODATA 文本写成 `8.617 333 262...` 而被解析成只有约 10 位有效数字的值；`exact2018` + `replace_exact` 把它们用定义常数重新算到全精度并回填，既保住精度，又通过断言守住正确与完整。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `exact2022` 直接写成 `exact2022 = exact2018`，而不是复制一份 2018 的代码？

> **参考答案**：CODATA 2022 与 2018 共用同一套 SI 定义常数，精确常数的推导关系完全相同，故 2022 复用 2018 的实现。直接赋值函数对象，避免代码重复，单一数据源——将来若推导公式要改，只改 `exact2018` 一处即可，2018 与 2022 同步生效。

**练习 2**：`exact2006` 里为什么没有 `'Boltzmann constant in eV/K': k/e` 这种条目？

> **参考答案**：2006 年时 \(k\)（玻尔兹曼常数）还是测量值、未精确化，所以 \(k/e\) 也不是精确的，不会被 CODATA 标记为 `(exact)`，自然不进 `need_replace`，也就不必出现在 `replace` 里。彼时唯一能精确推导的，是仅依赖已精确的 \(c\)（和纯几何常数 \(\pi\)）的量，如 `mag. constant`、`electric constant`。

**练习 3**：如果未来 CODATA 2030 又新增了一个「精确但被截断」的常数，需要改哪些地方？

> **参考答案**：① 新增 `txt2030` 文本块；② 写 `exact2030`（或若与 2018 推导一致则 `exact2030 = exact2018`），在其中给新常数的推导公式；③ 新增 `_physical_constants_2030 = parse_constants_2018toXXXX(txt2030, exact2030)`；④ 把它 `update` 进 `physical_constants`，并把 `_current_constants` 指向它。解析器与 `replace_exact` 无需改动——这正是把「推导」与「解析」解耦带来的可维护性。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个端到端的小任务。

**任务**：模拟一次「微型 CODATA 回填」，亲手走完「原料 → 推导 → 断言 → 回填」全流程。

**步骤**：

1. **造一份迷你文本**（示例代码，仿照真实列宽与标记）：

   ```python
   # 示例代码：仿 2018 列宽（name[:60] / val[60:85] / uncert[85:110] / unit[110:]）
   mini_txt = (
       "speed of light in vacuum                                    299 792 458              (exact)                  m s^-1\n"
       "Planck constant                                             6.626 070 15 e-34       (exact)                  J Hz^-1\n"
       "elementary charge                                           1.602 176 634 e-19      (exact)                  C\n"
       "Boltzmann constant                                          1.380 649 e-23           (exact)                  J K^-1\n"
       "Boltzmann constant in eV/K                                  8.617 333 262... e-5     (exact)                  eV K^-1\n"
       "conductance quantum                                         7.748 091 729... e-5     (exact)                  S\n"
   )
   ```

2. **复刻解析 + 回填逻辑**（直接复用 scipy 内部函数，验证机制）：

   ```python
   # 示例代码：调用 scipy 真实实现，观察回填前后差异
   import scipy.constants._codata as _cd

   # 解析前先看「截断值」长什么样
   truncated_k_over_e = float("8.617 333 262".replace(' ', '')) * 1e-5   # 仅 10 位
   print("截断值   =", truncated_k_over_e)

   # 用真实解析器跑一遍（exact_func 复用 exact2018 的思路即可，此处直接用其 replace）
   parsed = _cd.parse_constants_2018toXXXX(mini_txt, _cd.exact2018)
   stored = parsed['Boltzmann constant in eV/K']
   print("回填后   =", stored)            # (value, unit, uncertainty)
   print("全精度   =", stored[0])          # 约 8.617333262145...e-05
   print("不确定度=", stored[2], "（精确 → 0.0）")

   # 复算 k/e 验证
   k = parsed['Boltzmann constant'][0]
   e = parsed['elementary charge'][0]
   print("k/e      =", k / e, " 相等？", k/e == stored[0])
   ```

3. **回答三个问题**（写在你的学习笔记里）：
   - 回填前后的 `Boltzmann constant in eV/K` 数值差几位有效数字？
   - 为什么 `conductance quantum` 也被自动回填了？（提示：它在 `exact2018` 的 `replace` 字典里，且文本中是截断精确。）
   - 如果你把 `mini_txt` 里 `Boltzmann constant in eV/K` 的截断值故意改成 `9.999 999 999... e-5`，`replace_exact` 的哪条断言会失败？

**预期结果**：

- 截断值约 `8.617333262e-05`（10 位），回填值约 `8.617333262145e-05`（16 位），多出约 6 位。
- `conductance quantum` 被回填，因为它同时满足「文本截断」且「标记精确」，且 `exact2018` 给出了 `2*e**2/h` 的推导值。
- 故意改错截断值后，第二道断言 `abs(exact[name]/d[name][0] - 1) <= 1e-9` 会失败，抛 `AssertionError: Bad exact value: Boltzmann constant in eV/K ...`。

> 本实践调用的是 scipy 真实的 `parse_constants_2018toXXXX` 与 `exact2018`，但 `mini_txt` 是为本讲构造的示例文本，与真实 CODATA 数据无关。运行结果待本地验证。

## 6. 本讲小结

- **矛盾**：CODATA 文本把一些「本应精确」的派生常数写成 `8.617 333 262... e-5`（带 `...`），直接解析只剩约 10 位有效数字，损失了双精度本该有的 15～16 位。
- **机制**：`exact2018` 从五个 SI 定义常数 \(c, h, e, k, N_A\) 出发，用代数关系（如 `k/e`、`2*e**2/h`、`2π⁵k⁴/(15h³c²)`）算出所有派生精确常数的全精度值，装进 `replace` 字典。
- **校验**：`replace_exact` 用两道断言把关——每个待回填常数的推导值与截断值「相对误差 ≤ 1e-9」（正确性），且 `replace` 键集合与待回填集合「完全相等」（完整性）。
- **解耦**：解析器在末尾通过注入的 `exact_func` 调用回填，使「切列」与「推导」分离，便于多版本共存。
- **演进**：`exact2002` 仅 1 条、`exact2006` 7 条，而 SI 2019 重定义后的 `exact2018` 膨胀到 60+ 条；`exact2010/2014 = exact2006`、`exact2022 = exact2018` 通过函数对象赋值复用实现。
- **回归**：`test_gh14467` 与 `test_exact_values` 锁定了「精确常数必须全精度、precision 必须为 0」这一行为，防止截断精度问题复发。

## 7. 下一步学习建议

本讲把「单个 CODATA 版本如何解析 + 回填」讲透了。接下来建议：

1. **学习 [u2-l4 多版本 CODATA 合并、别名与废弃常量](u2-l4-versions-aliases-obsolete.md)**：本讲只讲了六个版本「各自解析」，u2-l4 讲它们如何被 `update` 合并、旧版独有的常数如何被标记为 `obsolete` 并触发 `ConstantWarning`，以及 `magn.→mag.`、`momentum→mom.um`、`electric constant→vacuum electric permittivity` 等别名机制。
2. **深入阅读** [`_codata.py` 的 `exact2018`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1535-L1628) 全文，对照 CODATA 2022 原始文本，尝试自己推导一两个未在讲义中展开的常数（如 `Wien frequency displacement law constant`，注意它涉及 Lambert W 函数根 `alpha_W`）。
3. **回顾** [u2-l2 CODATA 文本数据的固定列宽解析](u2-l2-codata-text-parsing.md)：把「列切片分类」与「本讲的回填」在脑中连成完整流水线：`txt → 切列 → 分类(原料/待回填/测量值) → exact_func 推导 → replace_exact 回填+断言 → constants 字典`。
