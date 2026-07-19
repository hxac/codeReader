# wide_fxp 任意精度类设计

## 1. 本讲目标

u6-l1 讲清了「narrow 用 float64、wide 用任意精度大整数」的两条路径，以及 `cl_fix_is_wide` 如何在两者之间派发。本讲把放大镜对准 wide 路径的载体——**`wide_fxp` 类本身**，拆解它的内部存储、整数位运算、运算符重载与 numpy 互操作。

学完本讲后，你应当能够：

1. 说清楚 `wide_fxp` 用 `dtype=object` 的 numpy 数组存「未归一化大整数」的约定，以及 `_data` / `_fmt` 两个私有字段如何分工。
2. 读懂 `wide_fxp.resize` 如何**完全用整数位运算**实现七种舍入（加偏移 + `>>`）、回绕（取模）与饱和（clip），并能与 u3-l2/u3-l3 的 narrow 浮点实现逐行对应。
3. 在 `__add__` / `__sub__` / `__mul__` 中追踪「对齐小数点 → 在整数上做算术」的统一套路，并理解 `AlignBinaryPoints` 为何是比较运算的对齐工具。
4. 理解 `__array_function__` 协议如何让 `np.where` / `np.array_equal` 作用于 `wide_fxp` 对象。
5. 掌握 `to_uint64_array` / `FromUint64Array` 的「按 64 位分块、LSB 在前」打包方式，以及它为何是与 MATLAB 等外部环境交换大位宽数据的桥梁。

本讲是 Unit 6 的第二篇，承接 u6-l1（narrow/wide 派发与构造方法），为理解整个 wide 路径画上闭环；它也回头印证 u3-l2（resize 舍入）、u3-l3（resize 饱和/回绕）、u4-l1（ForAdd/ForSub）在 wide 实现里的同款逻辑。

## 2. 前置知识

阅读本讲前，请先具备以下认知（前序讲义已建立）：

- **定点格式 `[S,I,F]`** 与位宽公式 \(W = S + I + F\)（见 u1-l2）。
- **七种 `FixRound` 舍入模式**统一遵循「先加偏移、再截断」，偏移积木 `HalfMinusDelta = 2^{(\text{DropFracBits}-1)} - 1`（见 u3-l2）。
- **`FixSaturate` 的饱和与回绕**：饱和 clip 到 `[MinValue, MaxValue]`，回绕取模丢弃高位（见 u1-l5、u3-l3）。
- **运算的中间格式增长规则** `FixFormat.ForAdd` / `ForSub` / `ForMult` / `ForNeg`（见 u4-l1、u4-l2、u4-l3）。
- **narrow/wide 双路径与派发**：`cl_fix_is_wide(fmt)` 判定 `width(fmt) > 53`；wide 内部存「未归一化大整数」，例如 `1.25` 在 `(0,2,4)` 下存为 `20`（见 u6-l1）。
- **numpy ndarray 向量化**：函数一次性处理整段数组而非逐元素循环（见 u2-l1）。

本讲要反复用到的一个关键直觉：**只要两个 `wide_fxp` 对象的小数位（`FracBits`）相同，它们 `_data` 里的整数就可以直接相加、相减、比较**——因为「小数点」已经被对齐到了同一个位置。wide_fxp 的运算符重载，核心就是围绕「对齐小数点」这一件事展开的。

## 3. 本讲源码地图

本讲主要涉及两个 Python 源文件，外加一个调用方：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `python/src/en_cl_fix_pkg/wide_fxp.py` | wide 路径的全部实现（533 行，单文件单类 + 两个非成员函数） | `__init__` / `_data` / `_fmt`、`resize`、`__add__` / `__sub__` / `__mul__`、`AlignBinaryPoints`、`__array_function__`、`to_uint64_array` / `FromUint64Array` |
| `python/src/en_cl_fix_pkg/en_cl_fix_types.py` | 共享类型定义 | `FixFormat.ForAdd` / `ForSub` / `ForMult` / `ForNeg`、`FixFormat.width` |
| `python/src/en_cl_fix_pkg/en_cl_fix_pkg.py` | 主体函数库（调用方） | `cl_fix_add` 如何把 `a + b` 委托给 `wide_fxp.__add__` |

## 4. 核心概念与源码讲解

### 4.1 内部存储与构造：`_data` / `_fmt` / `__init__`

#### 4.1.1 概念说明

`wide_fxp` 是一个极简的值对象：它只持有两样东西——

- **`_data`**：一个 `dtype=object` 的 numpy 数组，每个元素是一个**任意精度的 Python `int`**，存放「未归一化」的定点整数。
- **`_fmt`**：一个 `FixFormat` 对象，记录二进制小数点应该左移 `FracBits` 位才得到真实数值。

「未归一化」的含义在 u6-l1 已点明，这里再强调一次：定点数 `1.25` 在 `FixFormat(0,2,4)` 下，二进制是 `01.0100`，wide_fxp 不存 `1.25`，而存把它整体当成整数读出来的值 \(1.25 \times 2^{4} = 20\)。真实数值与内部数据的换算关系是：

\[
\text{real} = \frac{\_data}{2^{\_fmt.\text{FracBits}}}
\]

之所以「不归一化」，是因为 wide 路径的全部运算都在整数上做，而 Python 的 `int` 天然任意精度、不会溢出。把小数点位置单独记在 `_fmt` 里、运算时先用对齐技巧统一小数位，就能把定点运算化归为纯整数运算。

#### 4.1.2 核心流程

一个 `wide_fxp` 对象的生命周期：

```
构造（从外部数据进来）
  ├── FromFloat(real, rFmt, sat)     # 用户入口：带 half-up 量化 + 饱和
  ├── FromNarrowFxp(float_arr, fmt) # 内部桥：仅 floor，无边界检查
  ├── FromFxp(x, fmt)               # 派发器：已是 wide 就原样返回
  ├── FromUint64Array(u64, fmt)     # 外部桥：从 MATLAB 等 uint64 包恢复
  └── __init__(int_arr, fmt)        # 最底层：直接装填整数（视为私有）
        ↓
  持有 _data (object int 数组) + _fmt (FixFormat)
        ↓
运算 / 查询：resize、+、-、*、比较、to_narrow_fxp、to_uint64_array ...
```

注意 `__init__` 被刻意「私有化」（注释明确写了 *Considered private*），原因是它要求调用者传入**未归一化**的整数，而普通用户很容易误传已归一化的「真实数值」。例如定点值 `3.0` 在 `(0,2,4)` 下的内部数据是 \(3.0 \times 2^{4} = 48\)，**不是** `3`。因此官方建议用户走 `en_cl_fix_pkg` 的公开函数，让库自动选择 narrow / wide 表示。

#### 4.1.3 源码精读

文件头的设计注释是理解整个类的总纲，明确给出了「narrow 存浮点、wide 存大整数」的对照与 `1.25 → 20` 的存储约定：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L5-L20](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L5-L20) —— wide_fxp 设计说明：内部数据用任意精度整数、不按小数位归一化、比 narrow 慢但支持 >53 位。

构造函数 `__init__` 只做两件事：断言数据类型正确、把数据和格式存起来。`assert data.dtype == object` 这一行就是 wide 路径的「身份证」——只有 `dtype=object` 的数组才装得下任意精度整数：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L359-L363](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L359-L363) —— 私有构造函数，断言 `data.dtype == object` 与 `fmt` 类型，赋值 `_data` / `_fmt`；注释强调不要把已归一化的定点数当内部数据传入。

`_data` 与 `_fmt` 通过只读 `property` 暴露给外部（`x.data`、`x.fmt`），因此它们的「赋值」只能在类内部发生（运算符里会写 `a._data = ...`）：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L148-L156](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L148-L156) —— `data` 与 `fmt` 两个 `@property`，只读返回 `_data` / `_fmt`。

把内部整数还原成「人类可读」浮点数的方法是 `to_narrow_fxp`，它正是上面换算公式的直接落地——**除以 \(2^{\text{FracBits}}\) 后强转 float**。注释提醒：这一步不做任何范围/精度检查，超过 53 位会丢精度：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L169-L171](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L169-L171) —— `to_narrow_fxp`：`_data / 2**FracBits` 转回 float64，可能丢精度。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 `wide_fxp`，验证「未归一化大整数」存储约定。

**操作步骤**（在 `python/unittest` 目录下，让 `sys.path` 能找到 `src`）：

```python
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *
import numpy as np

# rFmt 位宽 = 0+40+20 = 60 > 53，会走 wide 路径
rFmt = FixFormat(False, 40, 20)
x = cl_fix_from_real([1.25], rFmt)        # 公开入口，自动派发到 wide_fxp.FromFloat
print(type(x))                            # <class 'en_cl_fix_pkg.wide_fxp.wide_fxp'>
print(x.fmt)                              # (False, 40, 20)
print(x.data)                             # 预期: [1310720]  (= 1.25 * 2**20)
print(x.to_narrow_fxp())                  # 预期: [1.25]
```

**需要观察的现象**：`x.data` 应当是 `1310720`（即 \(1.25 \times 2^{20}\)），而不是 `1.25`，也不是 `20`（`20` 是 `(0,2,4)` 那个示例的值，FracBits 不同）。`to_narrow_fxp()` 把它除以 \(2^{20}\) 还原为 `1.25`。

**预期结果**：`data = 1310720`、`to_narrow_fxp() = 1.25`。手算 \(1.25 \times 1048576 = 1310720\) 可直接验证。

**待本地验证**：上述具体打印值需在本机运行确认。

#### 4.1.5 小练习与答案

**练习 1**：定点值 `3.0` 在 `FixFormat(0,2,4)` 下的 wide_fxp 内部数据是多少？在 `FixFormat(0,40,20)` 下又是多少？

**答案**：`(0,2,4)` 下为 \(3.0 \times 2^{4} = 48\)（正是 `__init__` 注释里举的反例）；`(0,40,20)` 下为 \(3.0 \times 2^{20} = 3145728\)。可见同一个真实数值，FracBits 越大，内部整数越大。

**练习 2**：为什么 `__init__` 被视为私有、推荐用户改用 `cl_fix_from_real`？

**答案**：因为 `__init__` 直接装填「未归一化整数」，普通用户极易误传「已归一化的真实数值」（如把 `3.0` 误当 `3.0` 而非 `48` 传入）。`cl_fix_from_real` 接收真实数值并自动完成量化与 narrow/wide 选择，避免了这个陷阱。

---

### 4.2 `resize` 的整数位运算实现

#### 4.2.1 概念说明

`wide_fxp.resize` 是 wide 路径的「心脏」，所有运算最终都汇聚到它（与 narrow 路径的 `cl_fix_resize` 完全对应）。它把当前对象从 `self._fmt` 转换到目标格式 `rFmt`，期间可能要做三件事：

1. **舍入**：当目标小数位 `fr` 小于当前小数位 `f` 时，要丢掉 `f - fr` 个小数位。
2. **扩位**：当 `fr > f` 时，只需把整数左移补零，无损。
3. **饱和/回绕**：当目标整数位装不下结果时，按 `FixSaturate` 模式 clip 或取模。

关键在于：narrow 路径用 `np.floor` 和 float 运算实现这些步骤，而 **wide 路径完全用整数位运算实现**——舍入用「加偏移 + `>>` 算术右移」、回绕用「取模 `%`」、饱和用 `np.where` clip。这两套实现是位真等价的（见 u3-l2、u3-l3）。

> 小贴士：Python 的 `>>` 对负整数是**算术右移**（向 −∞ 取整），所以 Trunc_s「恒向 −∞ 截断」用 `val >>= shift` 一行即可天然实现，无需特殊处理符号。

#### 4.2.2 核心流程

`resize` 的执行流程（伪代码，对应源码 L212–L289）：

```
val = floor(self._data)          # 强制整数 object 类型
f  = self._fmt.FracBits
fr = rFmt.FracBits

# ① 小数位处理
if fr < f:                        # 需要舍入，丢弃 f-fr 位
    shift = f - fr
    half  = 2**(shift - 1)        # 半格 h
    hDelta = half - 1             # HalfMinusDelta
    按 rnd 加不同偏移（见下表）
    val >>= shift                 # 截断 = 算术右移
elif fr > f:                      # 无损扩位
    val = val * 2**(fr - f)
else:                             # 小数位不变
    pass

# ② 饱和/回绕（整数位处理）
if sat in {None_s, Warn_s}:       # 回绕
    satSpan = 2**(rFmt.IntBits + fr)
    if rFmt.Signed:
        val = ((val + satSpan) % (2*satSpan)) - satSpan
    else:
        val = val % satSpan
else:                             # 饱和（Sat_s / SatWarn_s）
    val = clip(val, MinValue(rFmt).data, MaxValue(rFmt).data)

return wide_fxp(val, rFmt)
```

七种舍入模式对应的「偏移表达式」与 u3-l2 完全一致，差别只是 narrow 用 `np.floor` 截断、wide 用 `>>=` 截断：

| 模式 | wide 加的偏移 | 直觉 |
|------|--------------|------|
| `Trunc_s` | `0` | 不加，直接右移（向 −∞） |
| `NonSymPos_s` | `+2**(shift-1)` | +半格，平局朝 +∞ |
| `NonSymNeg_s` | `+2**(shift-1) - 1` | +半格−δ，平局朝 −∞ |
| `SymInf_s` | `+2**(shift-1) - (val<0)` | 正数 +半格、负数 +半格−δ，平局远离零 |
| `SymZero_s` | `+2**(shift-1) - (val>=0)` | 正数 +半格−δ、负数 +半格，平局朝零 |
| `ConvEven_s` | `+2**(shift-1) - ((val>>shift)+1)%2` | 平局凑偶 |
| `ConvOdd_s` | `+2**(shift-1) - (val>>shift)%2` | 平局凑奇 |

回绕公式里的 `satSpan` 是「结果格式能表示的正值上限」：无符号下范围是 \([0,\ \text{satSpan}-1]\)，直接 `val % satSpan`；有符号下范围是 \([-\text{satSpan},\ \text{satSpan}-1]\)，先把值平移到 \([0,\ 2\cdot\text{satSpan})\) 取模再平移回来——这就是 `((val + satSpan) % (2*satSpan)) - satSpan` 的来历，与 u3-l3 narrow 路径的「平移-取模-平移」同构。

#### 4.2.3 源码精读

`resize` 函数签名与 narrow 的 `cl_fix_resize` 一致，默认 `Trunc_s` + `None_s`（回绕）：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L212-L223](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L212-L223) —— `resize` 开头：`np.floor(self._data)` 强制整数类型，取 `f` / `fr` 简写。

舍入分支是整个函数最密集的部分。看几个代表性模式：`Trunc_s` 什么都不加（`pass`），靠后面的 `>>=` 完成向 −∞ 截断；`NonSymPos_s` 加半格 `2**(f-fr-1)`；收敛类要先算出截断结果的奇偶性再决定加半格还是半格−δ：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L225-L263](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L225-L263) —— 舍入分支：七种 `FixRound` 各加不同偏移，最后统一 `val >>= shift` 截断。注意 `ConvEven_s` / `ConvOdd_s` 用 `val >> (f-fr)` 预判截断结果奇偶。

回绕与饱和分支清晰对应 `FixSaturate` 的两组开关。回绕用取模，饱和用 `np.where` 双向 clip 到 `MaxValue` / `MinValue` 的 `.data`：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L271-L289](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L271-L289) —— 饱和告警 + 回绕（`None_s`/`Warn_s` 取模）+ 饱和（`Sat_s`/`SatWarn_s` clip），最后 `return wide_fxp(val, rFmt)`。

`MaxValue` / `MinValue` 用纯整数算出边界，注意有符号最小值是 \(-2^{I+F}\)（补码「负方向多走一个 LSB」的不对称性，见 u1-l3）：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L116-L129](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L116-L129) —— `MaxValue` 返回 \(2^{I+F}-1\)；`MinValue` 有符号返回 \(-2^{I+F}\)、无符号返回 `0`，均经 `_FromIntScalar` 包成 wide_fxp。

#### 4.2.4 代码实践

**实践目标**：用一个会触发舍入 + 回绕的 resize，观察整数位运算的中间值。

**操作步骤**：

```python
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *
import numpy as np

# 60 位 wide 格式，装入 1.6875 (= 1.5 + 0.125 + 0.0625，FracBits=4 下精确)
src = FixFormat(False, 40, 20)
x = cl_fix_from_real([1.6875 * 2**16], src)   # 一个大数，data = floor(val * 2**20)
print(x.data)

# resize 到小数位=0（丢 20 位小数），先 Trunc 再 NonSymPos，对比差 1
rT = x.resize(FixFormat(False, 40, 0), rnd=FixRound.Trunc_s)
rP = x.resize(FixFormat(False, 40, 0), rnd=FixRound.NonSymPos_s)
print("Trunc data:", rT.data, "NonSymPos data:", rP.data)

# 构造一个会溢出的 resize，观察回绕（None_s）vs 饱和（Sat_s）
big = cl_fix_from_real([2**40], FixFormat(False, 41, 0))   # 接近 41 位无符号上限
wrapped = big.resize(FixFormat(False, 2, 0), sat=FixSaturate.None_s)
saturated = big.resize(FixFormat(False, 2, 0), sat=FixSaturate.Sat_s)
print("wrap:", wrapped.data, "sat:", saturated.data)       # sat 预期 clip 到 2**2-1 = 3
```

**需要观察的现象**：`Trunc` 与 `NonSymPos` 的 `data` 应相差 0 或 1（取决于被丢弃的小数部分是否越过半格）；饱和分支的 `data` 应被 clip 到 `MaxValue((False,2,0)).data = 3`，而回绕分支则取模得到一个小值。

**预期结果**：`sat` 分支 `data = 3`（即 `(False,2,0)` 的最大值 \(2^2-1\)）；`wrap` 分支为 `2**40 % 2**2 = 0`。具体舍入差值需根据输入数值手算确认。

**待本地验证**：舍入分支的精确 `data` 值取决于你装入的具体数值，需运行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Trunc_s` 分支里只有一个 `pass`，没有任何加偏移的语句？

**答案**：因为截断就是「向 −∞ 取整」，wide 实现紧接着用 `val >>= shift` 做算术右移，Python 对负整数的 `>>` 本身就向 −∞ 取整，所以无需任何偏移即可天然实现 Trunc_s。这也是 Trunc_s「零成本」在整数域的体现。

**练习 2**：有符号回绕公式 `((val + satSpan) % (2*satSpan)) - satSpan` 中，`satSpan` 和 `2*satSpan` 分别代表什么？

**答案**：`satSpan = 2**(IntBits+FracBits)` 是结果格式「不计符号位」的幅值上限，对应有符号范围 \([-\text{satSpan},\ \text{satSpan}-1]\)。`2*satSpan = 2**(1+IntBits+FracBits)` 是有符号格式能表示的**值个数**（含符号位）。先把 `val` 平移到 \([0,\ 2\cdot\text{satSpan})\) 这个无符号区间取模，再平移回有符号区间，就把任意大整数回绕进了合法范围。

---

### 4.3 运算符重载与小数点对齐：`__add__` / `__sub__` / `__mul__` 与 `AlignBinaryPoints`

#### 4.3.1 概念说明

wide_fxp 重载了 `+`、`-`（二元与一元）、`*` 以及六个比较运算符，使两个 `wide_fxp` 对象可以像普通数一样做 `a + b`、`a < b`。这里的核心难题是：**两个操作数的小数位往往不同，它们的 `_data` 整数不能直接相加**。

举个直观例子：`a = 1.25` 存成 `(0,2,4)` 的 `20`，`b = 0.75` 存成 `(0,2,2)` 的 `3`。直接 `20 + 3 = 23` 是错的（应该是 `2.0`）。必须先把它们对齐到同一个小数位：把 `b` 从 `(0,2,2)` 扩到 `(0,2,4)`，`_data` 变成 `3 * 2**(4-2) = 12`，然后 `20 + 12 = 32`，对应 \(32 / 2^4 = 2.0\)，正确。

所以运算符重载的关键套路是：**先把两个操作数 resize 到一个共同格式（小数位对齐 + 预留进位/符号位），再在 `_data` 整数上直接做算术**。这正是 u4-l1 「中间全精度格式 → 精确运算 → resize」架构在 wide 实现里的落地。

#### 4.3.2 核心流程

各运算符的统一套路：

```
__add__(a, b):
    rFmt = FixFormat.ForAdd(a.fmt, b.fmt)   # 共同格式：max(FracBits) + 1 进位位
    a = a.copy().resize(rFmt)               # 对齐小数点（无损，Trunc_s+None_s）
    b = b.copy().resize(rFmt)
    a._data = a.data + b.data               # 整数直接相加
    return a                                 # 结果 fmt = ForAdd

__sub__(a, b): 同上，rFmt = ForSub，a._data = a.data - b.data
__mul__(a, b): rFmt = ForMult，直接 a.data * b.data（小数位相加，无需对齐）
__neg__(a):    rFmt = ForNeg，返回 wide_fxp(-a.data, ForNeg(a.fmt))
比较运算符:    AlignBinaryPoints 对齐后，直接比 a.data 与 b.data
```

三个运算符用了三种不同的「共同格式」，对应 u4-l1/u4-l2/u4-l3 的增长规则：

- `ForAdd = (Sa∨Sb, max(Ia,Ib)+1, max(Fa,Fb))`：那个 `+1` 整数位吸收两个大正数相加的进位。
- `ForSub = (True, max(Ia, Ib+sign(b)), max(Fa,Fb))`：强制有符号（减法可能产生负数）。
- `ForMult = (True, Ia+Ib+1, Fa+Fb)`：小数位直接相加，所以**乘法根本不需要对齐小数点**——两个整数直接相乘，结果的 `_data` 自然就是 `a.data * b.data`，`FracBits` 也自然是 `Fa+Fb`。

`AlignBinaryPoints` 是一个独立的对齐工具，它**不预留进位位**，只把所有对象的小数位扩到 `max(FracBits)`、各自的 `Signed`/`IntBits` 保持不变。它主要被比较运算符使用（比较只需对齐，不需进位）：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L134-L144](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L134-L144) —— `AlignBinaryPoints`：取 `Fmax = max(FracBits)`，把每个对象 resize 到 `(各自 Signed, 各自 IntBits, Fmax)`，返回对齐后的列表。注释提醒用 `[a.copy(), b.copy()]` 调用以免修改原件。

#### 4.3.3 源码精读

`__add__` 是最典型的运算符实现，三步清晰可读——算 `ForAdd`、两边各自 `resize` 对齐、`_data` 相加：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L399-L414](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L399-L414) —— `__add__`：`copy()` 后 `resize(ForAdd)` 对齐小数点，再 `a._data = a.data + b.data`。

`__sub__` 与 `__add__` 结构对称，只是用 `ForSub` 且最后做减法：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L418-L433](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L418-L433) —— `__sub__`：与 `__add__` 同构，`rFmt = ForSub`，`a._data = a.data - b.data`。

`__mul__` 最简洁——因为乘法的小数位是相加而非对齐，所以无需 resize，直接整数相乘，结果格式取 `ForMult`：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L443-L445](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L443-L445) —— `__mul__`：`result_fmt = ForMult`，`wide_fxp(self._data * other.data, result_fmt)`，无需对齐。

`ForAdd` / `ForSub` / `ForMult` / `ForNeg` 的定义全部在 `en_cl_fix_types.py`，wide_fxp 直接复用：

- [python/src/en_cl_fix_pkg/en_cl_fix_types.py:L19-L36](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L19-L36) —— 四个静态增长规则 `ForAdd` / `ForSub` / `ForMult` / `ForNeg`。

比较运算符统一走 `_extract_comparison_data`：它先特判「与整数 0 比较」，否则调用 `AlignBinaryPoints` 对齐后返回两个 `_data` 数组，后续 `__eq__` / `__lt__` / ... 只需在返回的整数上做普通 numpy 比较：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L449-L463](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L449-L463) —— `_extract_comparison_data`（特判 int 0 + `AlignBinaryPoints` 对齐）与 `__eq__`（直接 `a == b` 比较 `_data`）。

最后看公开入口 `cl_fix_add` 如何把工作下放给 `__add__`：它在派发到 wide 后，直接写 `a + b`（触发 `__add__`），再对结果做一次 `cl_fix_resize` 到用户目标格式：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L327-L339](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L327-L339) —— `cl_fix_add`：`midFmt = ForAdd`，wide 派发后 `return cl_fix_resize(a + b, midFmt, rFmt, rnd, sat)`，其中 `a + b` 调用 `wide_fxp.__add__`。

#### 4.3.4 代码实践

**实践目标**：单步推理 `+` 运算符如何对齐小数点并在整数上完成加法。

**操作步骤**：

```python
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *

# 两个 >53 位、小数位不同的 wide 格式
aFmt = FixFormat(False, 40, 20)   # width 60
bFmt = FixFormat(False, 32, 30)   # width 62
a = cl_fix_from_real([1.25], aFmt)   # data 预期: 1.25 * 2**20 = 1310720
b = cl_fix_from_real([0.75], bFmt)   # data 预期: 0.75 * 2**30 = 805306368

# 单步推理 __add__：
rFmt = FixFormat.ForAdd(aFmt, bFmt)  # 预期 (False, 41, 30)
print("ForAdd:", rFmt)               # 小数位取 max(20,30)=30
# a 对齐: frac 20->30, _data * 2**10 = 1310720 * 1024 = 1342177280
# b 对齐: frac 30->30, _data 不变 = 805306368
c = a + b
print("sum data:", c.data)           # 预期 1342177280 + 805306368 = 2147483648
print("sum fmt:", c.fmt)             # 预期 (False, 41, 30)
print("sum real:", c.to_narrow_fxp())# 预期 2.0  (因为 1.25 + 0.75 = 2.0)
```

**需要观察的现象**：`c.fmt` 应为 `(False, 41, 30)`；`c.data` 应为 `2147483648`；`c.to_narrow_fxp()` 应为 `2.0`。这验证了「对齐小数点 → 整数相加 → 除以 \(2^{30}\)」全链路正确。

**预期结果**：`ForAdd = (False, 41, 30)`、`sum data = 2147483648`、`sum real = 2.0`。手算：\(1310720 \times 1024 + 805306368 = 2147483648\)，\(2147483648 / 2^{30} = 2.0\)。

**待本地验证**：上述打印值需运行确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__mul__` 不需要像 `__add__` 那样先 `resize` 对齐小数点？

**答案**：加法要求两个操作数的小数点对齐才能逐位相加，而乘法的小数位是**相加**关系（\(F_a + F_b\)）。两个未归一化整数直接相乘，结果整数的小数位自然就是 \(F_a + F_b\)，对应 `ForMult` 的 `FracBits`。所以乘法无需对齐，直接 `_data * _data` 即可。

**练习 2**：`AlignBinaryPoints` 与 `__add__` 里的对齐有何区别？为什么比较用前者、加法用后者？

**答案**：`AlignBinaryPoints` 只把小数位扩到 `max(FracBits)`，**不预留进位位、不改 IntBits**；`__add__` 用 `ForAdd`，会多留一个整数位吸收加法进位。比较运算只关心数值大小关系、不会产生进位，所以用更「紧」的 `AlignBinaryPoints` 即可；加法必须留进位位，否则两个接近上限的正数相加会溢出中间格式。

---

### 4.4 numpy 互操作：`__array_function__` 与 `np.where` / `np.array_equal`

#### 4.4.1 概念说明

wide_fxp 的 `_data` 是 numpy 数组，所以它天然支持向量化。但有一类操作不能靠重载普通运算符实现——**以 `np.where(cond, x, y)`、`np.array_equal(x, y)` 为代表的 numpy 顶层函数**。当 numpy 发现参数里有 `wide_fxp` 对象时，需要一种机制把控制权交还给 `wide_fxp` 自己的实现。

numpy 提供的这套机制叫 **`__array_function__` 协议**（[NumPy 文档：basics.dispatch](https://numpy.org/doc/stable/user/basics.dispatch.html)）。它的运作方式是：numpy 维护一张「函数 → 实现」的分发表，自定义类通过重写 `__array_function__` 来查表并调用自己注册的实现。wide_fxp 正是这么做的。

#### 4.4.2 核心流程

注册与派发的协作流程：

```
# ① 注册阶段（模块加载时）
@implements(np.where)           # 装饰器把 where 存进 _HANDLED_NUMPY_FUNCTIONS[np.where]
def where(*args, **kwargs): ...

@implements(np.array_equal)
def array_equal(*args, **kwargs): ...

# ② 调用阶段
np.where(cond, x, y)            # x, y 是 wide_fxp
  → numpy 探测到 wide_fxp 参数
  → 调用 x.__array_function__(np.where, types, args, kwargs)
  → 查表 _HANDLED_NUMPY_FUNCTIONS[np.where]
  → 执行注册的 where(): 对 _data 做 np.where，包回 wide_fxp
```

`where` 的实现很巧妙：`cond` 是普通 bool ndarray，`x` / `y` 是 wide_fxp。它直接对 `x.data` 与 `y.data`（两个整数数组）调用底层 `np.where` 选位，再包成 wide_fxp。前提是 `x.fmt == y.fmt`（同格式才能逐元素选位）。`array_equal` 同理，对齐后用 `np.all(x == y)` 比较。

#### 4.4.3 源码精读

`__array_function__` 是协议入口：若请求的 numpy 函数没注册就返回 `NotImplemented`（交回 numpy 默认处理），否则查表执行。注意源码里有一处诚实的 TODO——注释说那个 `any` 「应该是 `all`？但改成 all 就注册不上了」，这是当前实现的一个已知小瑕疵：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L373-L381](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L373-L381) —— `__array_function__`：查 `_HANDLED_NUMPY_FUNCTIONS`，未命中返回 `NotImplemented`，命中则派发；注释标注了 `any`/`all` 的 TODO。

注册机制靠模块级的 `_HANDLED_NUMPY_FUNCTIONS` 字典 + `implements` 装饰器：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L499-L506](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L499-L506) —— `_HANDLED_NUMPY_FUNCTIONS` 字典与 `implements` 装饰器，把 numpy 函数映射到 wide_fxp 实现。

`np.where` 的实现——断言两选择支同格式，对 `_data` 选位后包回：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L510-L520](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L510-L520) —— `where`：`np.where(condition, x.data, y.data)` 后用 `x.fmt` 包成 wide_fxp。

`np.array_equal` 的实现——形状不匹配直接 assert 报错（而非悄悄返回 False），再用 `np.all(x == y)`（其中 `==` 触发 `__eq__` → AlignBinaryPoints 对齐比较）：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L524-L533](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L524-L533) —— `array_equal`：先 assert 形状一致，再 `np.all(x == y)`，`==` 复用 `__eq__` 的对齐比较逻辑。

#### 4.4.4 代码实践

**实践目标**：触发 `__array_function__`，观察 `np.where` 与 `np.array_equal` 在 wide_fxp 上的行为。

**操作步骤**：

```python
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *
import numpy as np

fmt = FixFormat(False, 40, 20)               # width 60, wide
x = cl_fix_from_real([1.0, 2.0, 3.0, 4.0], fmt)
y = cl_fix_from_real([9.0, 9.0, 9.0, 9.0], fmt)
cond = np.array([True, False, True, False])

z = np.where(cond, x, y)                      # 触发 __array_function__ → where
print(type(z), z.to_narrow_fxp())             # 预期 [1.0, 9.0, 3.0, 9.0]

print(np.array_equal(x, x))                   # 预期 True
print(np.array_equal(x, y))                   # 预期 False
```

**需要观察的现象**：`z` 仍是 `wide_fxp`，其 `to_narrow_fxp()` 为 `[1.0, 9.0, 3.0, 9.0]`，说明 `np.where` 按 `cond` 在两个 wide_fxp 之间逐元素选位。`np.array_equal(x, x)` 为 `True`、`np.array_equal(x, y)` 为 `False`。

**预期结果**：`z = [1.0, 9.0, 3.0, 9.0]`；两个 `array_equal` 分别为 `True` / `False`。

**待本地验证**：需运行确认。

#### 4.4.5 小练习与答案

**练习 1**：如果对一个未注册的 numpy 函数（比如 `np.sum`）传入 wide_fxp 对象，会发生什么？

**答案**：`__array_function__` 会在 `_HANDLED_NUMPY_FUNCTIONS` 里查不到 `np.sum`，于是返回 `NotImplemented`。numpy 收到 `NotImplemented` 后会回退到默认行为，通常会抛出 `TypeError`（因为它不知道如何对自定义类型求和）。目前 wide_fxp 只注册了 `np.where` 与 `np.array_equal` 两个。

**练习 2**：`array_equal` 实现里为什么用 `np.all(x == y)` 而不是直接 `x.data == y.data`？

**答案**：因为两个 wide_fxp 可能有不同的 `FracBits`，直接比 `_data` 整数会在小数位未对齐时出错。`x == y` 触发 `__eq__` → `_extract_comparison_data` → `AlignBinaryPoints`，先把两边对齐到同一个小数位再比较整数，从而正确处理不同格式的相等性判断。

---

### 4.5 uint64 数组打包：`to_uint64_array` / `FromUint64Array`

#### 4.5.1 概念说明

wide_fxp 内部用 Python 任意精度整数，但外部世界（MATLAB、HDL 仿真器、C 代码）通常只认固定宽度的 `uint64`。当要把一个 >64 位的 wide 定点数传给 MATLAB 时，需要把它**拆成若干个 64 位块**；反过来从 MATLAB 拿到一堆 `uint64` 时，又要**按权重拼回**一个大整数。

`to_uint64_array` 与 `FromUint64Array` 就是这对打包/解包函数。它们的约定是：**LSB 在前**——第 0 行放最低 64 位，第 1 行放次低 64 位，依此类推，第 `i` 行的权重是 \(2^{64i}\)。这与 MATLAB 的小端存储习惯一致。

#### 4.5.2 核心流程

打包 `to_uint64_array`：

```
fmtWidth = fmt.width()                      # 总位宽
nInts = ceil(fmtWidth / 64)                 # 需要几个 uint64
val = _data
val = where(val < 0, val + 2**fmtWidth, val) # 负数 reinterpret 成无符号（补码）
for i in 0..nInts-1:
    uint64Array[i,:] = val % 2**64          # 取最低 64 位
    val >>= 64                              # 丢掉已取走的 64 位
# 结果形状 (nInts,) + val.shape，即每列对应一个元素
```

解包 `FromUint64Array`：

```
weights = 2**(64 * arange(nInts))           # [1, 2**64, 2**128, ...]
val = matmul(weights, data)                 # 加权求和拼回大整数（无符号）
# 处理符号：若 val 落在「符号位为 1」的区间，减去 2**width 还原成负数
val = where(val >= 2**(IntBits+FracBits), val - 2**(IntBits+FracBits+1), val)
return wide_fxp(val, fmt)
```

关键点：

- **符号 reinterpret**：打包时若 `_data` 为负，先加 `2**fmtWidth` 转成等价的无符号补码；解包时若拼回的无符号值 ≥ \(2^{I+F}\)（即符号位权重，因符号位在最高位、权重为 \(2^{width-1} = 2^{I+F}\)），说明原本是负数，减去 `2**width = 2**(I+F+1)` 还原。
- **块数 `nInts`**：由 `ceil(width / 64)` 决定。例如 71 位需要 2 个 uint64，130 位需要 3 个。

#### 4.5.3 源码精读

`to_uint64_array` 的注释点明了「按列打包」的形状约定（`result[:,k]` 对应 `data[k]`）：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L176-L193](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L176-L193) —— `to_uint64_array`：`nInts = (width+63)//64`，负数加 `2**width` reinterpret，循环 `% 2**64` 取块、`>>= 64` 右移。

`FromUint64Array` 用 `matmul(weights, data)` 做加权求和，再用 `np.where` 还原符号：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L104-L112](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L104-L112) —— `FromUint64Array`：`weights = 2**(64*arange(n))`，`matmul` 拼回无符号大整数，`where` 还原符号位。

#### 4.5.4 代码实践

**实践目标**：把一个 wide_fxp 打包成 uint64 数组，读懂每行的权重。

**操作步骤**：接续 4.3.4 的加法结果 `c`（`fmt = (False, 41, 30)`，`data = 2147483648`）：

```python
u = c.to_uint64_array()
print(u.shape, u.dtype)        # 预期 (2, 1) uint64；width=71 → nInts=ceil(71/64)=2
print(u)                       # 预期 [[2147483648], [0]]
# 第 0 行权重 2**0  = 1（最低 64 位）：2147483648
# 第 1 行权重 2**64（次低 64 位）：0

# 验证往返：解包回 wide_fxp
c2 = wide_fxp.FromUint64Array(u, c.fmt)
print(np.array_equal(c, c2))   # 预期 True
print(c2.to_narrow_fxp())      # 预期 2.0
```

**需要观察的现象**：`u` 形状为 `(2, 1)`——`2` 是 `nInts`（71 位需要 2 个 uint64 块），`1` 是元素个数。`u[0] = 2147483648`（因为 `data` 本身 < \(2^{64}\)，全部落在第 0 块），`u[1] = 0`（第 1 块没有内容）。`FromUint64Array` 能无损还原。

**预期结果**：`u = [[2147483648], [0]]`、往返 `array_equal` 为 `True`、还原值 `2.0`。手算：\(2147483648 < 2^{64}\)，故 `2147483648 % 2^{64} = 2147483648`、`2147483648 >> 64 = 0`。

**待本地验证**：需运行确认。

#### 4.5.5 小练习与答案

**练习 1**：一个 `FixFormat(True, 100, 100)` 的 wide_fxp，`to_uint64_array` 会得到几行的 uint64 数组？

**答案**：位宽 \(W = 1 + 100 + 100 = 201\)，`nInts = ceil(201/64) = 4`，所以是 4 行。每行权重依次为 \(2^0, 2^{64}, 2^{128}, 2^{192}\)。

**练习 2**：为什么打包时要先做 `val = where(val < 0, val + 2**fmtWidth, val)`？

**答案**：因为 `uint64` 只能装非负数。负的 `_data` 是 Python 的任意精度负整数，直接 `% 2**64` 虽然在 Python 里能算出非负结果，但语义上需要先把整个数 reinterpret 成「宽度为 `fmtWidth` 的无符号补码」（即加 `2**fmtWidth`），才能保证拆出来的 64 位块与硬件二进制补码表示逐位一致。这与解包端的「减 `2**width` 还原符号」是互逆操作。

---

## 5. 综合实践

把本讲的存储约定、`resize` 整数实现、运算符对齐、uint64 打包串成一条完整链路。

**任务**：构造两个**不同格式且均 >53 位**的 wide_fxp 对象，完成一次加法并打包导出，全程单步推理。

**完整步骤**（在 `python/unittest` 目录运行）：

```python
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *
import numpy as np

# 1) 构造两个 >53 位、小数位不同的 wide 格式
aFmt = FixFormat(False, 40, 20)   # width 60
bFmt = FixFormat(False, 32, 30)   # width 62

# 2) 装入真实数值（验证存储约定：data = real * 2**FracBits）
a = cl_fix_from_real([1.25], aFmt)
b = cl_fix_from_real([0.75], bFmt)
print("a.data =", a.data, "（预期 1.25*2**20 = 1310720）")
print("b.data =", b.data, "（预期 0.75*2**30 = 805306368）")

# 3) 手算 ForAdd，再用 + 运算符相加（验证 AlignBinaryPoints/ForAdd 对齐 + 整数加法）
rFmt = FixFormat.ForAdd(aFmt, bFmt)
print("ForAdd =", rFmt, "（预期 (False, 41, 30)，小数位取 max=30）")
c = a + b
print("c.fmt  =", c.fmt)
print("c.data =", c.data, "（预期 1310720*1024 + 805306368 = 2147483648）")
print("c.real =", c.to_narrow_fxp(), "（预期 2.0）")

# 4) 对结果做一次 resize，体验整数舍入（丢 30 位小数到 0）
cInt = c.resize(FixFormat(False, 41, 0), rnd=FixRound.NonSymPos_s)
print("cInt.data =", cInt.data, "cInt.real =", cInt.to_narrow_fxp())

# 5) 用比较运算符触发 AlignBinaryPoints（验证比较侧的对齐）
d = cl_fix_from_real([2.0], rFmt)
print("c == d ?", np.array_equal(c, d), "（预期 True，都表示 2.0）")

# 6) 打包成 uint64 数组导出，再解包往返（验证打包/解包互逆）
u = c.to_uint64_array()
print("uint64 shape:", u.shape, "（预期 (2,1)，width 71 → 2 块）")
print("uint64:\n", u, "（预期 [[2147483648],[0]]，第0行权重2**0，第1行权重2**64）")
cBack = wide_fxp.FromUint64Array(u, c.fmt)
print("roundtrip equal?", np.array_equal(c, cBack), "（预期 True）")
```

**需要观察与解释的要点**：

1. **存储约定**：`a.data` 与 `b.data` 分别是真实值乘以 \(2^{FracBits}\) 的大整数，不是 `1.25` / `0.75`。
2. **对齐**：`+` 运算符内部把 `a` 从 `(False,40,20)` resize 到 `(False,41,30)`，`_data` 乘以 \(2^{10}\)（小数位 20→30）；`b` 已是 30 位小数，不变。对齐后两边小数位都是 30，整数方可直接相加。
3. **整数加法**：`c.data` 是两个对齐后整数的和，除以 \(2^{30}\) 还原为 `2.0`。
4. **resize 舍入**：第 4 步把 30 位小数舍到 0 位，用 `NonSymPos_s`（加半格）再算术右移 30 位。
5. **比较对齐**：第 5 步 `np.array_equal` 触发 `__eq__` → `AlignBinaryPoints`，把 `c` 与 `d` 对齐到同小数位后比 `_data`。
6. **uint64 权重**：第 6 步结果 2 行，第 0 行权重 \(2^0\) 装 LSB，第 1 行权重 \(2^{64}\) 装高 64 位；因 `data < 2^64`，高位块为 0。

**预期结果**：各步预期值已在注释中标注（`a.data=1310720`、`b.data=805306368`、`ForAdd=(False,41,30)`、`c.data=2147483648`、`c.real=2.0`、uint64 `[[2147483648],[0]]`、往返 `True`）。**待本地验证**：全部数值需在本机运行确认。

## 6. 本讲小结

- `wide_fxp` 是极简值对象，只持 `_data`（`dtype=object` 的任意精度整数数组）与 `_fmt`（`FixFormat`），存「未归一化大整数」，真实值 \(= \_data / 2^{\text{FracBits}}\)；`__init__` 被刻意私有化以防误用。
- `resize` 完全用整数位运算实现：七种舍入 =「加不同偏移 + 算术右移 `>>`」、回绕 =「取模 `%`（有符号带平移）」、饱和 = `np.where` clip，与 narrow 浮点实现位真等价。
- 运算符 `__add__`/`__sub__` 的套路是「`ForAdd`/`ForSub` 对齐小数点 → `_data` 直接加减」；`__mul__` 因小数位相加而无需对齐，直接 `_data * _data`；比较运算符走 `AlignBinaryPoints`（只对齐、不留进位位）。
- `__array_function__` 协议 + `_HANDLED_NUMPY_FUNCTIONS` 字典 + `@implements` 装饰器，使 `np.where` / `np.array_equal` 能作用于 wide_fxp；目前只注册了这两个函数。
- `to_uint64_array` / `FromUint64Array` 按「LSB 在前、每块权重 \(2^{64i}\)」打包/解包，`nInts = ceil(width/64)`，并通过加/减 `2**width` 处理符号 reinterpret，是与 MATLAB 等外部环境交换大位宽数据的桥梁。
- 整个 wide 路径再次印证了贯穿全库的统一架构：所有运算先在足够大的中间格式上做精确整数运算，最后由 `resize` 统一完成舍入与饱和。

## 7. 下一步学习建议

- **横向对照 narrow 实现**：回到 `en_cl_fix_pkg.py` 的 `cl_fix_resize`（u3-l2/u3-l3），把它与 `wide_fxp.resize` 逐行对照，体会「同一种语义、浮点 vs 整数两种实现」的位真等价。
- **读 wide 路径的派发全貌**：在 `en_cl_fix_pkg.py` 中搜索所有 `cl_fix_is_wide` / `wide_fxp.FromFxp` 出现处，画出 `cl_fix_add` / `cl_fix_sub` / `cl_fix_mult` / `cl_fix_neg` 的「判定宽 → 转 wide → 用运算符 → 按需 `to_narrow_fxp` 切回」数据流图。
- **向 VHDL 架构收口**：进入 Unit 7（u7-l1 VHDL TempFmt 全精度中间格式与综合考量），把本讲「中间全精度格式 → 精确运算 → resize」的整数实现，与 VHDL 的 `TempFmt_c` 架构对照，理解三套实现如何收敛到同一种设计哲学。
- **扩展 numpy 互操作**（进阶练习）：参照 `where` / `array_equal` 的写法，尝试为 `np.zeros_like` 或 `np.broadcast_to` 注册一个 `@implements` 实现，加深对 `__array_function__` 协议的理解（注意这是练习题，不要修改仓库源码，可在本地副本上实验）。
