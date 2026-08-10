# WideFix：任意精度整数表示

## 1. 本讲目标

学完本讲后，你应该能够：

- 说明 WideFix 与 NarrowFix 的本质差异：WideFix 用 **Python 任意精度整数**（而非 float64）存放定点数据，因此没有位宽上限。
- 写出 WideFix 的内部表示公式 `integer_value = real × 2^F`，并能手工换算。
- 读懂并解释 `from_real` / `from_narrowfix` / `to_real` / `to_uint64_array` 等转换方法的实现。
- 理解在整数域里 `round` / `saturate` / `resize` 是如何用「加偏移 + 右移」「取模」纯整数运算实现的，且为何永远精确、不需要精度回退。
- 理解整数域算术（尤其是乘法为何无需对齐、加法为何需要对齐、移位为何只是「换个格式」）。
- 动手完成一次宽度超过 53 位的乘法，并用 `to_real` 验证（同时理解精度告警的含义）。

## 2. 前置知识

本讲假定你已经学完 **u6-l1（NarrowFix：float64 内部表示与 53 位上限）**，并掌握：

- **`[S, I, F]` 定点格式**：S 符号位、I 整数位、F 小数位，总位宽 `S+I+F`。
- **归一化（normalized）与非归一化（unnormalized）两种读法**：归一化值即真实数值 `real`；非归一化整数 `integer = real × 2^F`（把比特串当作普通整数看）。
- **NarrowFix**：内部存归一化的 `float64`，故 `to_real` 对它恒等；受 float64 尾数 53 位精度限制，设 `MAX_WIDTH = 53`，位宽超过即不可用。
- **`cl_fix_is_wide(fmt)`**：当 `fmt.width > 53` 时为真，库据此在 NarrowFix / WideFix 之间分发。
- **`round` / `saturate` / `resize` 的语义**：减少小数位用舍入，减少整数/符号位用饱和，`resize = 先 round 后 saturate`（顺序不可换）。

一句直觉：NarrowFix 把定点值「当小数（real）存」，所以快但受限于 53 位；WideFix 把定点值「当大整数存」，慢但无上限、永远精确。本讲就讲后者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/wide_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py) | **本讲主角**：`WideFix` 类的全部实现（构造、转换、round/saturate/resize、算术、运算符）。 |
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | `NarrowFix`，作为对照：float64 表示、`MAX_WIDTH=53`、`saturate` 中为保精度的「退回整数域」回退。 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | 主接口，`cl_fix_is_wide` 与各 `cl_fix_*` 函数的 narrow/wide 分发逻辑。 |
| [bittrue/tests/python/cl_fix_round_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py) | 穷举对拍测试：把 NarrowFix 数据经 `WideFix.from_narrowfix` 转过去再 round，与本地参考实现逐位比对，是验证「narrow/wide 两路 bit-true」的关键证据。 |

## 4. 核心概念与源码讲解

### 4.1 WideFix 的整数内部表示与构造

#### 4.1.1 概念说明

`WideFix` 与 `NarrowFix` 解决同一个问题——存放一个定点数——但选择了完全不同的内部表示：

- **NarrowFix**：把定点值以**归一化的 `float64`** 存储（即直接存 `real`）。优点是快（numpy 向量化浮点运算），缺点是 float64 只有 53 位尾数精度，定点位宽超过 53 位就无法精确表示。
- **WideFix**：把定点值以**非归一化的 Python 任意精度整数**存储，即直接存原始比特串对应的整数。Python 的 `int` 类型没有位宽上限（仅受内存限制），因此 WideFix 可以表示任意位宽的定点数，且所有运算都是**精确的整数运算**，永不丢精度。代价是慢得多（逐元素 Python 大整数运算，无法用硬件浮点加速）。

核心公式（贯穿全讲）：

\[
\text{integer\_value} = \text{real} \times 2^{F}
\]

例如定点值 `1.25` 在格式 `FixFormat(0,2,4)`（即二进制 `01.0100`）下：

- NarrowFix 内部存 `1.25`（float）。
- WideFix 内部存 `1.25 × 2^4 = 20`（整数）。

源码文件头部的注释把这一点讲得非常清楚：

> The fixed-point number 1.25 in FixFormat(0,2,4) has binary representation "01.0100". In WideFix this is stored internally as integer value 1.25\*2\*\*4 = 20. In NarrowFix, it would be stored as float value 1.25.

见 [wide_fix.py:L20-L35](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L20-L35)。

下表对照两种表示，建议记牢：

| 维度 | NarrowFix | WideFix |
| --- | --- | --- |
| 内部 dtype | `float64` | `object`（持有 Python 大整数） |
| 存的是什么 | 归一化 `real` | 非归一化整数 `real × 2^F` |
| 位宽上限 | 53（`MAX_WIDTH`） | 无上限 |
| 速度 | 快 | 慢 |
| `to_real` | 恒等（本身就是 real） | `integer / 2^F`，整数过大时发精度告警 |
| `from_integer` | 需除 `2^F` | 恒等（本身就是整数） |
| 有符号回绕 | 可能超 53 位、需临时退回整数域 | 始终精确 |

#### 4.1.2 核心流程

构造一个 `WideFix` 只需两步：

1. 传入**内部整数数据**（标量 `int` 或 numpy `object` 数组）和**格式** `fmt`。
2. 断言数据确实是任意精度整数（`dtype == object` 且元素是 `int`），然后拷贝数据、浅拷贝格式。

注意：构造函数接收的是**已经量化好的内部整数**，而不是 `real`。要把 `real` 变成 WideFix，请用 `WideFix.from_real`（见 4.2）。

#### 4.1.3 源码精读

构造函数，见 [wide_fix.py:L47-L62](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L47-L62)：

```python
def __init__(self, data, fmt : FixFormat, copy=True):
    if isinstance(data, int):
        data = np.array(data, dtype=object)
    assert data.dtype == object, "WideFix: requires arbitrary-precision int (dtype == object)."
    assert isinstance(data.flat[0], int), "WideFix: requires arbitrary-precision int (dtype == object)."
    if copy:
        self._data = data.copy()
    else:
        self._data = data
    self._fmt = shallow_copy(fmt)
```

要点：

- 标量 `int` 会被包成 `dtype=object` 的 0 维数组；这是 numpy 存放 Python 大整数的标准方式（普通 `int64` 数组放不下超过 64 位的数）。
- 两条 `assert` 强制「必须是任意精度整数」，杜绝把 float 或普通整型数组混进来。
- 与 NarrowFix 不同，**这里没有位宽断言**——任何 `fmt` 都接受，这正是「无上限」的体现。对照 NarrowFix 构造里的 `assert fmt.width <= NarrowFix.MAX_WIDTH`，见 [narrow_fix.py:L54-L59](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L54-L59)。

两个表示范围的辅助静态方法，注意它们返回的是**整数域**的端点（与 NarrowFix 的 real 域端点含义不同但数值等价），见 [wide_fix.py:L129-L146](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L129-L146)：

```python
@staticmethod
def max_value(fmt : FixFormat):
    val = 2**(fmt.I+fmt.F)-1          # 整数域最大值
    return WideFix(val, fmt, copy=False)

@staticmethod
def min_value(fmt : FixFormat):
    if fmt.S == 1:
        val = -2**(fmt.I+fmt.F)        # 有符号整数域最小值
    else:
        val = 0
    return WideFix(val, fmt, copy=False)
```

对照 NarrowFix 的 `max_value` 返回 `2^I - 2^(-F)`（real 域，见 [narrow_fix.py:L110-L123](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L110-L123)）。两者描述的是同一个最大值，只是一个用整数 `2^(I+F)-1`、一个用 real `2^I - 2^(-F)`，满足 `2^(I+F)-1 = (2^I - 2^(-F)) × 2^F`。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 WideFix 的内部整数表示 `integer = real × 2^F`。
2. **操作步骤**：

   ```python
   # 示例代码（需先 pip install -r requirements.txt，并在仓库根目录运行）
   import sys
   sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *

   fmt = FixFormat(0, 2, 4)                 # 二进制 01.0100 共 6 位
   x = WideFix.from_real(1.25, fmt)         # 用 4.2 的 from_real 构造
   print("内部整数:", int(x._data))          # 直接看原始整数
   print("换算 real:", int(x._data) / 2**fmt.F)
   ```

3. **需要观察的现象**：`x._data` 不是 `1.25`，而是一个整数。
4. **预期结果**（依据源码推算，待本地验证）：内部整数应为 `1.25 × 2^4 = 20`；换算回 real 为 `20 / 16 = 1.25`。
5. 若环境未就绪，可改为「源码阅读型实践」：精读 [wide_fix.py:L29-L31](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L29-L31) 的注释，确认「1.25 in (0,2,4) → 整数 20」。

#### 4.1.5 小练习与答案

**练习 1**：定点值 `-1.5` 在格式 `FixFormat(1,2,3)` 下，WideFix 的内部整数是多少？
**答案**：`integer = real × 2^F = -1.5 × 2^3 = -12`。

**练习 2**：为什么 WideFix 不需要 `MAX_WIDTH` 这样的常量？
**答案**：因为它用 Python 任意精度 `int` 存数据，Python `int` 没有固定位宽，仅受内存限制；只有用固定宽度容器（如 float64 的 53 位尾数）才需要设上限。

**练习 3**：`WideFix.max_value(FixFormat(0,2,4))._data` 与 `NarrowFix.max_value(FixFormat(0,2,4))._data` 各是多少？它们为何描述同一个值？
**答案**：前者为整数域 `2^(2+4)-1 = 63`，后者为 real 域 `2^2 - 2^(-4) = 3.9375`；`63 = 3.9375 × 2^4`，故等价。

---

### 4.2 数据转换：from_real / from_narrowfix / to_uint64_array / to_real

#### 4.2.1 概念说明

WideFix 不直接暴露「内部整数」给用户，而是提供一组转换方法在 `real`、`NarrowFix`、以及「分块数组」之间进出：

- **`from_real(a, r_fmt, saturate)`**：把浮点 `real` 量化成 WideFix（固定半进位 + 饱和）。
- **`from_narrowfix(a)`**：把一个 NarrowFix **无损**地升级成 WideFix——这是 narrow↔wide 互转的桥梁。
- **`to_real(warn=True)`**：把内部整数除以 `2^F` 还原成 real 近似值；整数过大时会**发精度告警**。
- **`from_uint64_array` / `to_uint64_array`**：把任意宽的整数拆成/合并自若干个 64 位无符号块——这是与 MATLAB 交换宽定点数据的唯一通道（MATLAB 没有原生大整数）。

#### 4.2.2 核心流程

- `from_real` 流程：`real` → 乘 `2^F` 加 `0.5`（半进位）→ 取整得整数 → 饱和到格式范围 → 包成 WideFix。注意它**只支持饱和（Sat/SatWarn），不支持回绕**；要回绕请改用 `resize()`。
- `to_real` 流程：整数 → 除 `2^F` 得 float64 近似；若整数超出 `±2^52`（有符号）或 `2^53`（无符号），发精度告警。
- `from_narrowfix` 流程：取 NarrowFix 的归一化 float `_data`，乘 `2^F` 取整即得整数——无损。
- `to_uint64_array` 流程：先把负数按位宽重解释成无符号，再每 64 位切成一块。

#### 4.2.3 源码精读

`from_real`，见 [wide_fix.py:L64-L101](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L64-L101)，关键三段：

```python
# 1) 量化：恒定半进位（half-up）
x = a*(2.0**r_fmt.F) + 0.5
if hasattr(x, 'astype'):
    x = x.astype('object'); x = np.floor(x)   # 数组：转 object 再取整
else:
    x = int(x)                                # 标量：int() 截断

# 2) 饱和：只支持 Sat / SatWarn
if (saturate == FixSaturate.Sat_s) or (saturate == FixSaturate.SatWarn_s):
    x = np.where(x > WideFix.max_value(r_fmt)._data,  WideFix.max_value(r_fmt)._data, x)
    x = np.where(x < WideFix.min_value(r_fmt)._data,  WideFix.min_value(r_fmt)._data, x)
else:
    raise NotImplementedError(f"WideFix: Unsupported saturation mode {str(saturate)}")
```

两点解读：

- 量化公式 `a·2^F + 0.5` 然后 `floor`（数组）或 `int()`（标量）实现半进位。这与 NarrowFix.from_real 思路一致，见 [narrow_fix.py:L84-L86](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L84-L86)，区别在于 NarrowFix 算完又除回 `2^F`（存 real），而 WideFix 保留整数（存 integer）。
- 饱和比较用的是**整数域端点** `max_value/min_value._data`（即 `2^(I+F)-1` 等），因为这里 `x` 已经是整数。

`from_narrowfix`，见 [wide_fix.py:L103-L114](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L103-L114)：

```python
@staticmethod
def from_narrowfix(a : "NarrowFix"):
    int_data = np.floor((a._data*2.0**a._fmt.F).astype(object))
    ...
    return WideFix(int_data, a._fmt, copy=False)
```

 NarrowFix 的 `_data` 是 real，乘 `2^F` 取整即得整数——这就是「无损升级」。`cl_fix_*` 主接口里 narrow→wide 的所有分发都用它，见 [en_cl_fix.py:L200-L202](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L200-L202)。

`to_real`，见 [wide_fix.py:L179-L188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L179-L188)：

```python
def to_real(self, warn=True):
    if warn:
        if (self.fmt.S == 1 and (np.any(self.data < -2**52) or np.any(self.data >= 2**52))) \
            or (self.fmt.S == 0 and np.any(self.data >= 2**53)):
            warnings.warn("WideFix.to_real: Possible loss of precision ...", Warning)
    return np.array(self._data/2.0**self._fmt.F, dtype=np.float64)
```

这是本讲最重要的「坑」：**`to_real` 必然把任意精度整数塞进 float64**，而 float64 只能精确表示到 `2^53`。一旦原始整数超过该量级，结果 float 可能不再精确，于是库**主动告警**。注意告警是**保守的**——它只看整数大小，不看该值是否恰好可被 float64 精确表示（见综合实践的讨论）。

`from_uint64_array` / `to_uint64_array`，见 [wide_fix.py:L116-L127](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L116-L127) 与 [wide_fix.py:L190-L210](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L190-L210)。`to_uint64_array` 先把负数加上 `2^width` 重解释为无符号，再按 64 位切块：

```python
n_ints = (fmt.width + 63) // 64                 # ceil(width / 64)
val = np.where(val < 0, val + 2**fmt.width, val) # 负数 -> 无符号重解释
for i in range(n_ints):
    u64_array[i,:] = val % 2**64; val >>= 64     # 每 64 位取一块
```

`from_uint64_array` 是逆过程：用权重 `2^(64·k)` 加权求和还原大整数，再按符号位把 `>= 2^(I+F)` 的值减去 `2^(I+F+1)` 重解释回负数。这套分块机制是 u9-l1（MATLAB 桥接）的基础。

#### 4.2.4 代码实践

1. **实践目标**：体会 narrow→wide 的无损升级，以及 `to_real` 的精度告警。
2. **操作步骤**：

   ```python
   # 示例代码
   import sys, warnings
   sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *
   import numpy as np

   # (a) 无损升级：NarrowFix(1.25,(0,2,4)) -> WideFix
   nf = NarrowFix.from_real(1.25, FixFormat(0,2,4))
   wf = WideFix.from_narrowfix(nf)
   print("narrow _data:", float(nf._data), " wide _data:", int(wf._data))  # 1.25 / 20

   # (b) 制造一个超大整数，触发 to_real 告警
   big = WideFix.from_real(3.0, FixFormat(0,30,30))  # width=60>53，内部整数=3*2^30
   big2 = big * big                                  # 整数=9*2^60，远超 2^53
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       print("to_real:", float(big2.to_real()))      # 9.0
       print("warned:", any("precision" in str(x.message) for x in w))
   ```

3. **需要观察的现象**：(a) 中 narrow 存 `1.25`、wide 存 `20`；(b) 中 `to_real` 仍返回 `9.0`，但同时打印出 `warned: True`。
4. **预期结果**（依据源码推算，待本地验证）：(a) `1.25` 与 `20`；(b) `9.0` 与 `True`。注意：`9.0` 恰好精确（`9·2^60 = 1.001₂ × 2^63` 只需 4 位尾数），但告警仍会触发——证明告警是保守的。
5. 若无法运行，改为精读 [wide_fix.py:L184-L188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L184-L188)，解释告警阈值 `-2^52 / 2^52 / 2^53` 的由来。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from_real` 对 `None_s` / `Warn_s` 会抛 `NotImplementedError`？
**答案**：`from_real` 是「量化入口」，库只实现了半进位 + 饱和的量化；回绕（wrap）未被实现。需要回绕语义时，应先用 `from_real`（带饱和）生成数据，再调用 `resize(..., sat=None_s)` 显式回绕。

**练习 2**：格式 `FixFormat(0,0,100)` 的数据，`to_uint64_array` 会输出几个 uint64？
**答案**：`n_ints = ceil(width/64) = ceil(100/64) = 2` 个。

**练习 3**：`from_narrowfix` 为何说是「无损」的？
**答案**：NarrowFix 的 `_data` 是 float64 归一化值，且 NarrowFix 保证位宽 ≤53，故 `real × 2^F` 落在 float64 精确整数范围内（≤ `2^53`），取整不丢精度，升级后的整数与原比特完全一致。

---

### 4.3 整数域的 round / saturate / resize

#### 4.3.1 概念说明

u4-l1/u4-l2/u4-l3 已讲清 round/saturate/resize 的**语义**。本节只讲一件事：同样的语义，在 WideFix 的**整数域**里是如何用纯整数运算实现的，以及它为何比 NarrowFix 更「干净」。

核心心法（来自 u4-l1）：**所有舍入 = 加一个偏移量 + 向下取整（floor）**。在整数域里，「向下取整减少低位」就是**右移** `val >>= (f - fr)`；而 NarrowFix 在 float 域里不得不用 `floor(data × 2^rF) / 2^rF`（放缩—取整—还原）。二者是同一套偏移公式的不同单位表达，因而 bit-true。

饱和（saturate）在整数域里就是**取模（回绕）**或**钳位**：回绕用 `((val + span) % (2·span)) - span`，钳位用 `np.where` 限到端点。全是大整数运算，**永远精确**。

#### 4.3.2 核心流程

- `round(r_fmt, rnd)`：先 `assert r_fmt == for_round(...)`（fmt_check）；按舍入模式加整数偏移；右移 `f - fr` 位截断。
- `saturate(r_fmt, sat)`：回绕则取模，钳位则 `np.where` 到 `max/min_value`；回绕前发可选告警。
- `resize(r_fmt, rnd, sat)`：固定 `round(rounded_fmt) → saturate(r_fmt)`，与 NarrowFix.resize 完全同序。

#### 4.3.3 源码精读

`round`，见 [wide_fix.py:L212-L273](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L212-L273)。截取关键分支：

```python
if fr < f:                                   # 小数位减少才舍入
    if rnd is FixRound.Trunc_s:
        pass                                 # 截断：什么都不加
    elif rnd is FixRound.NonSymPos_s:
        val = val + 2**(f - fr - 1)          # + 半值
    elif rnd is FixRound.NonSymNeg_s:
        val = val + (2**(f - fr - 1) - 1)    # + 半值 - 1 个 LSB
    ...
    shift = f - fr
    val >>= shift                            # 截断 = 右移
elif fr > f:
    val = val * 2**(fr - f)                  # 小数位增加 = 左移
```

对比 NarrowFix.round 的同一段，见 [narrow_fix.py:L167-L186](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L167-L186)：

```python
elif rnd is FixRound.NonSymPos_s:
    data = data + 2.0 ** (-r_fmt.F - 1)       # float 域的半值
elif rnd is FixRound.NonSymNeg_s:
    data = data + 2.0 ** (-r_fmt.F - 1) - 2.0 ** -fmt.F  # 半值 - 1 个 float LSB
...
data = np.floor(data * 2.0 ** r_fmt.F).astype(np.float64) * 2.0 ** -r_fmt.F  # 放缩取整还原
```

二者的对应关系：WideFix 的整数偏移 `2^(f-fr-1)` 恰好等于 NarrowFix 的 float 偏移 `2^(-rF-1)` 乘以 `2^f`（即 `2^(f-fr-1) = 2^(-rF-1)·2^f`，因为 `f - fr - 1 = -rF - 1 + f`）。`1`（WideFix 的「一个 LSB」）对应 `2^(-fmt.F)`（NarrowFix 的「一个 LSB」）。这正是两路 bit-true 的数学根源，也被 `cl_fix_round_test.py` 穷举对拍证实，见 [cl_fix_round_test.py:L124-L132](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/cl_fix_round_test.py#L124-L132)：

```python
# 用 WideFix（仍是 narrow 数据）重算一遍，与本地参考逐位比对
r_wide = WideFix.from_narrowfix(NarrowFix(a, a_fmt)).round(r_fmt, rnd).to_real()
expected = round_check(a, a_fmt, r_fmt, rnd)
assert np.array_equal(r, expected), "Numerical error detected."
assert np.array_equal(r_wide, expected), "Numerical error detected (WideFix)."
```

`saturate`，见 [wide_fix.py:L275-L300](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L275-L300)：

```python
if sat == FixSaturate.None_s or sat == FixSaturate.Warn_s:
    satSpan = 2**(r_fmt.I + r_fmt.F)
    if r_fmt.S == 1:
        val = ((val + satSpan) % (2*satSpan)) - satSpan   # 有符号回绕
    else:
        val = val % satSpan                               # 无符号回绕
else:
    val = np.where(val > WideFix.max_value(r_fmt).data, WideFix.max_value(r_fmt).data, val)
    val = np.where(val < WideFix.min_value(r_fmt).data, WideFix.min_value(r_fmt).data, val)
```

关键对比：NarrowFix.saturate 在做**有符号回绕**时，因为要先加 `2^I`，中间量可能顶破 53 位精度，所以有一段「判断 `add_fmt.width > MAX_WIDTH` 就退回 object 整数域计算」的回退逻辑，见 [narrow_fix.py:L210-L232](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L210-L232)。而 **WideFix 本身就在整数域，根本不需要这段回退**——它用的取模公式与 NarrowFix 回退时用的公式一模一样（对照 [narrow_fix.py:L226-L230](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L226-L230)）。换句话说：NarrowFix 的「精度不足回退」其实就是临时借用 WideFix 的整数算法。

`resize`，见 [wide_fix.py:L302-L315](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L302-L315)，与 [narrow_fix.py:L246-L255](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L246-L255) 逐行同构：

```python
def resize(self, r_fmt, rnd=FixRound.Trunc_s, sat=FixSaturate.None_s):
    rounded_fmt = FixFormat.for_round(self._fmt, r_fmt.F, rnd)
    rounded = self.round(rounded_fmt, rnd)     # 先 round（改 F）
    result = rounded.saturate(r_fmt, sat)      # 后 saturate（F 不变）
    return result
```

顺序不可换的原因同 u4-l3：saturate 要求 F 不变；且舍入进位可能把合法值顶出范围，必须先 round 后 saturate 才能捕获「进位诱发溢出」。

#### 4.3.4 代码实践

1. **实践目标**：复现 `cl_fix_round_test.py` 的对拍思路，亲眼确认 WideFix 与 NarrowFix 的 round 结果一致。
2. **操作步骤**：

   ```python
   # 示例代码
   import sys
   sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *
   import numpy as np

   a_fmt = FixFormat(1, 3, 2)
   a = np.array([-2.00, -0.25, 0.25, 1.75])         # 4 个 real 值
   r_fmt = FixFormat.for_round(a_fmt, 0, FixRound.NonSymPos_s)  # 减到 0 位小数

   rn = NarrowFix(a, a_fmt, copy=False).round(r_fmt, FixRound.NonSymPos_s)._data
   rw = WideFix.from_narrowfix(NarrowFix(a, a_fmt)).round(r_fmt, FixRound.NonSymPos_s).to_real()
   print("narrow:", rn)
   print("wide  :", rw)
   print("equal :", np.array_equal(rn, rw))
   ```

3. **需要观察的现象**：两路结果逐元素相等。
4. **预期结果**（依据源码推算，待本地验证）：两路均给出 `[-2., 0., 0., 2.]`（半进位：`-0.25→0`、`0.25→0`、`1.75→2`），`equal` 为 `True`。这正是「同一套偏移公式、不同单位」的直接证据。
5. 若无法运行，改为精读 [cl_fix_round_test.py:L121-L132](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L121-L132) 的对拍断言，并解释为何它能在「所有格式 × 所有舍入模式」上保证 bit-true。

#### 4.3.5 小练习与答案

**练习 1**：WideFix.round 用 `val >>= shift` 截断，NarrowFix.round 用 `floor(data·2^rF)/2^rF`，为何形式不同而结果一致？
**答案**：整数域里丢掉低 `shift=f-fr` 位等价于除以 `2^shift` 再取整，即右移；float 域里数据是归一化 real，必须先乘 `2^rF` 把目标 LSB 变成整数位、取整、再除回 `2^rF`。两者数学等价，只是单位不同。

**练习 2**：WideFix.saturate 为何不需要 NarrowFix.saturate 里的「退回整数域」回退分支？
**答案**：WideFix 本身就是任意精度整数，任何中间量（如回绕时的 `val + 2^I`）都不会溢出，天然精确，没有 53 位天花板，故无需回退。

**练习 3**：`resize` 能否先 `saturate` 再 `round`？为什么？
**答案**：不能。`saturate` 硬性要求 `r_fmt.F == a_fmt.F`（小数位不变），而 `round` 的目的正是改变小数位；此外舍入的进位可能把原本合法的值顶出范围，只有「先 round 后 saturate」才能正确捕获这种进位诱发溢出（见 u4-l3）。

---

### 4.4 整数域算术与运算符重载

#### 4.4.1 概念说明

WideFix 的算术（add/sub/mult/neg/abs/shift）沿用全讲共享的**三段式骨架**（见 u5-l2）：先由 `for_*` 推导全精度中间格式 `mid_fmt` → 在其中无损运算 → `resize` 到目标 `r_fmt`。差别全在「运算」那一步——因为数据是整数，运算规则与 NarrowFix 的浮点运算不同，有三个特别值得注意的点：

1. **乘法无需对齐**：结果小数位 = `a.F + b.F`，所以 `a._data * b._data` 直接得到正确刻度下的整数。
2. **加法/减法需要对齐**：结果小数位 = `max(a.F, b.F)`，两个操作数的小数位可能不同，必须先把小数位少的那一方「左移补齐」才能做整数加减。
3. **移位只是换格式**：在整数域，移动小数点 = 重新解释 `I/F` 的划分，**存储的整数本身不变**，因此移位零开销、零损失。

#### 4.4.2 核心流程

- `mult`：算 `mid_fmt = for_mult`；`new_data = a._data * b._data`（大整数乘法，精确）；`resize`。
- `add`/`sub`：算 `mid_fmt = for_add/sub`；分别把 a、b 舍入（Trunc）到 `mid_fmt.F` 对齐小数点；整数加/减；`resize`。
- `neg`/`abs`：算 `mid_fmt`；`-self._data` / `where(self._data<0, neg, pos)`；`resize`。
- `shift`：算 `mid_fmt = for_shift`；**直接把原数据放进新格式** `WideFix(self._data, mid_fmt)`（数据不变）；`resize`。
- 运算符：`+ - * << ` 映射到 `add/sub/mult/shift`，比较运算先经 `align_binary_points` 对齐再比 `_data`。

#### 4.4.3 源码精读

`mult`，见 [wide_fix.py:L395-L404](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L395-L404)：

```python
def mult(self, b, r_fmt=None, rnd=FixRound.Trunc_s, sat=FixSaturate.None_s):
    mid_fmt = FixFormat.for_mult(self._fmt, b._fmt)
    if r_fmt is None: r_fmt = mid_fmt
    return WideFix(self._data * b._data, mid_fmt, copy=False).resize(r_fmt, rnd, sat)
```

注意它**没有对齐步骤**：两个整数直接相乘，结果的物理含义自动落在 `F = a.F + b.F` 的刻度上（因为 `(x·2^aF)·(y·2^bF) = xy·2^(aF+bF)`）。这也是它能突破 53 位的关键——两个 30 位整数相乘得到 60 位整数，Python 大整数精确承载，float64 早溢出了。

`add`，见 [wide_fix.py:L339-L357](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L339-L357)，重点是对齐：

```python
mid_fmt = FixFormat.for_add(a._fmt, b._fmt)
# 把两方都 round(Trunc) 到 mid_fmt.F，等价于把小数位少的一方左移补齐
a_round = a.round(FixFormat.for_round(a._fmt, mid_fmt.F, FixRound.Trunc_s))
b_round = b.round(FixFormat.for_round(b._fmt, mid_fmt.F, FixRound.Trunc_s))
return WideFix(a_round._data + b_round._data, mid_fmt).resize(r_fmt, rnd, sat)
```

对照 NarrowFix.add（[narrow_fix.py:L279-L288](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L279-L288)）直接 `self._data + b._data` 而无需对齐——因为 float 存的是归一化 real，不同 F 的值在 float 域天然可比；整数域则必须先对齐刻度。

`shift`，见 [wide_fix.py:L406-L433](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L406-L433)，核心是「换格式不换数据」：

```python
mid_fmt = FixFormat.for_shift(self._fmt, np.min(shift), np.max(shift))
if np.ndim(shift) == 0:
    mid = WideFix(self._data, mid_fmt)     # 同一份数据，新格式 => 等价于移位
...
return mid.resize(r_fmt, rnd, sat)
```

对照 NarrowFix.shift（[narrow_fix.py:L328-L341](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L328-L341)）必须 `self._data * 2.0**shift`——因为 float 域里「移位」只能靠真正乘除一个比例因子；整数域里小数点是格式约定，挪动它不需要碰数据。变长移位（`shift` 是数组）时，WideFix 逐元素地为每个移位值构造临时格式再 resize 汇总（见 [wide_fix.py:L422-L431](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L422-L431)）。

运算符重载见 [wide_fix.py:L435-L453](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L435-L453)，比较运算统一走 `align_binary_points`（[wide_fix.py:L148-L163](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L148-L163)）把两方 resize 到相同 F 再比 `_data`，这使得不同格式的 WideFix 也能正确比较——这是它比 NarrowFix（只允许相同格式比较，见 [narrow_fix.py:L363-L365](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L363-L365)）更灵活的地方。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：用 WideFix 完成一次 **>53 位**的乘法，用 `to_real` 验证，并理解精度告警。
2. **操作步骤**：

   ```python
   # 示例代码
   import sys, warnings
   sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *
   import numpy as np

   # 1) 选一个宽格式（width=60 > 53），确认走 WideFix 路径
   fmt = FixFormat(0, 30, 30)
   print("width:", fmt.width, "is_wide:", cl_fix_is_wide(fmt))   # 60 / True

   # 2) 用 from_real 放入值 3.0（内部整数 = 3*2^30）
   a = WideFix.from_real(3.0, fmt)
   print("a._data:", int(a._data))                               # 3221225472

   # 3) 相乘（运算符 * -> WideFix.mult，纯整数，精确）
   r = a * a
   print("r.fmt:", repr(r.fmt))                                  # FixFormat(0, 60, 60)
   print("r._data:", int(r._data))                               # 9*2^60 的大整数

   # 4) 用 to_real 验证：会发精度告警，但结果仍是精确的 9.0
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       print("to_real:", float(r.to_real()))                     # 9.0
       print("warned:", any("precision" in str(x.message) for x in w))  # True
   print("手算核对 9*2^60:", 9*2**60)
   ```

3. **需要观察的现象**：`is_wide` 为 `True`；`a._data` 是大整数 `3221225472` 而非 `3.0`；乘积 `_data` 是一个 19 位十进制大整数；`to_real` 返回 `9.0` 但伴随精度告警。
4. **预期结果**（依据源码推算，待本地验证）：
   - `width 60 is_wide True`
   - `a._data 3221225472`（即 `3·2^30`）
   - `r.fmt FixFormat(0, 60, 60)`、`r._data 10376293541461622784`（即 `9·2^60`）
   - `to_real 9.0`、`warned True`、`手算核对 10376293541461622784`
   - 关键体会：`9·2^60 = 1.001₂ × 2^63` 只需 4 位尾数，恰好可被 float64 精确表示，所以 `to_real` 得到精确的 `9.0`；但告警仍触发，因为它只看「整数 ≥ 2^53」这个保守条件。若换成需要更多尾数位的值，`to_real` 就会真正丢精度。
5. 若无法运行，改为「源码阅读型实践」：跟踪 [en_cl_fix.py:L392-L421](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L392-L421) 的 `cl_fix_mult`，说明当 `a_fmt` 宽时它如何经 `WideFix.from_narrowfix`/直接 `WideFix(a, a_fmt)` 进入整数域、完成大整数乘法、再 `resize`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `WideFix.mult` 不需要对齐小数点，而 `WideFix.add` 需要？
**答案**：乘法结果的小数位是 `a.F + b.F`，`a._data · b._data` 自动落在该刻度上，无需对齐；加法结果的小数位是 `max(a.F, b.F)`，两个操作数的小数位往往不同，整数相加前必须先把小数位少的一方左移到 `max` 刻度，否则会把不同粒度的数错误相加。

**练习 2**：`WideFix.shift` 为何「数据不变，只换格式」就能实现移位？
**答案**：整数域里，定点小数点的位置是格式 `I/F` 的约定，并非数据的一部分。左移 `n` 位等价于把 `n` 个比特从「小数位」划归「整数位」（`I+n, F-n`），存储的原始整数不变。float 域做不到这点，因为 float 存的是已归一化的 real，必须真正乘 `2^n`。

**练习 3**：把 `a * a` 改成在 `FixFormat(0,30,30)` 上用 NarrowFix 直接算会发生什么？
**答案**：NarrowFix 构造时即被 `assert fmt.width <= MAX_WIDTH` 拒绝（width=60 > 53），抛出「Request format is too wide. Use WideFix.」。这正是 WideFix 存在的意义。

---

## 5. 综合实践

把本讲四块知识串起来：构造一个宽格式 → 量化 → 相乘 → 验证，并与 narrow 路径对照。

```python
# 示例代码
import sys, warnings
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *
import numpy as np

# (1) 选宽格式，确认 is_wide
fmt = FixFormat(0, 30, 30)
assert cl_fix_is_wide(fmt), "应为 wide"

# (2) 经 cl_fix_from_real 量化（内部走 WideFix.from_real）
a = cl_fix_from_real(3.0, fmt)
b = cl_fix_from_real(3.0, fmt)

# (3) 结果格式与乘法（内部走 cl_fix_mult 的 WideFix 分支）
r_fmt = cl_fix_mult_fmt(fmt, fmt)          # (0, 60, 60)
r = cl_fix_mult(a, fmt, b, fmt, r_fmt)

# (4) 验证：to_real 发告警但数值正确
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    val = cl_fix_to_real(r, r_fmt)
    print("结果:", float(val), "告警:", any("precision" in str(x.message) for x in w))

# (5) 对照：同样「3×3=9」用窄格式做， NarrowFix 路径无告警
nfmt = FixFormat(0, 2, 2)
na = cl_fix_from_real(3.0, nfmt)
nr = cl_fix_mult(na, nfmt, na, nfmt, cl_fix_mult_fmt(nfmt, nfmt))
print("窄路径结果:", float(cl_fix_to_real(nr, cl_fix_mult_fmt(nfmt, nfmt))))

# (6) 反例：直接给 NarrowFix 喂宽格式
try:
    NarrowFix(1, fmt)
except AssertionError as e:
    print("NarrowFix 拒绝宽格式:", e)
```

**实践要求**：

1. 运行上述脚本（或逐段精读），记录每一步输出。
2. 解释第 (4) 步为何「数值精确却仍告警」。
3. 解释第 (6) 步的断言来自哪一行源码（提示：[narrow_fix.py:L58](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L58)）。
4. **预期结果**（依据源码推算，待本地验证）：第 (4) 步 `结果 9.0 告警 True`；第 (5) 步 `窄路径结果 9.0`（无告警）；第 (6) 步打印类似 `NarrowFix: Requested format is too wide. Use WideFix.`。

通过这个任务，你会亲历：宽格式强制走 WideFix → 大整数乘法精确 → `to_real` 因整数超 `2^53` 而保守告警 → 同一运算在窄格式下走 NarrowFix 无告警。这正是 en_cl_fix「双表示」架构的价值与代价。

## 6. 本讲小结

- **WideFix 用 Python 任意精度整数**（numpy `object` 数组）存放定点数据，内部存的是非归一化整数 `integer = real × 2^F`，无位宽上限，永远精确，但比 NarrowFix 慢。
- **构造只收整数**：`WideFix(data, fmt)` 断言 `dtype==object` 且元素是 `int`；无位宽断言（对比 NarrowFix 的 53 位上限）。
- **转换四件套**：`from_real`（半进位 + 饱和，不支持回绕）、`from_narrowfix`（narrow→wide 无损桥）、`to_real`（除 `2^F` 还原，整数过大时**保守告警**）、`from/to_uint64_array`（64 位分块，MATLAB 互操作通道）。
- **整数域 round/saturate/resize** 是同一套语义的整数实现：round = 加偏移 + **右移**（而非 float 的放缩取整），saturate = **取模**回绕 / `np.where` 钳位，全精确、无需 NarrowFix 那种「退回整数域」的精度回退。
- **算术三段式**：mult 无需对齐（结果 F = a.F+b.F），add/sub 需先对齐小数点（结果 F = max），shift 只换格式不改数据；运算符 `+ - * <<` 与比较（经 `align_binary_points`）均已重载。
- **`to_real` 精度告警是保守的**：只要原始整数 ≥ `2^53`（无符号）或越过 `±2^52`（有符号）就告警，与该值是否真能被 float64 精确表示无关。

## 7. 下一步学习建议

- 下一讲 **u6-l3（cl_fix_is_wide 分发与 Narrow↔Wide 互转）** 会把本讲的 `from_narrowfix`、`to_real` 放回主接口 `en_cl_fix.py`，讲解 `cl_fix_is_wide` 如何根据格式宽度在两路之间自动分发、混合宽度操作数如何处理、以及 wide 结果如何回转 narrow。建议先复习本讲的 4.4.3 与 [en_cl_fix.py:L200-L212](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L200-L212) 的分发片段作为预热。
- 若对「与 MATLAB 交换宽定点数据」感兴趣，可提前跳读 [wide_fix.py:L116-L127](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L116-L127) 与 [wide_fix.py:L190-L210](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L190-L210)，那是 u9-l1 的基础。
- 想验证「两路 bit-true」的最直接材料是穷举对拍测试 [cl_fix_round_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py) 与 [cl_fix_saturate_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py)，建议运行一遍观察 `Completed N tests.`。
