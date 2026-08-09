# 浮点/整数与定点转换：from_real、to_real、from_integer、to_integer

## 1. 本讲目标

定点数在硬件里是一串比特，在软件里却可以用「真实数值」或「原始整数」两种方式表达。本讲解决一个看似基础、实则贯穿整个库的问题：**如何在浮点 `real`、原始整数、定点比特模式这三者之间互相转换**。

学完本讲你应该能够：

1. 说清「归一化（normalized）」与「非归一化（unnormalized）」两种定点读法，以及它们之间的关系 \(\text{值} = \text{整数} \times 2^{-F}\)。
2. 读懂 Python `NarrowFix.from_real` / `from_integer` / `to_integer` 的实现：半进位量化、饱和钳位、范围检查。
3. 理解 VHDL `cl_fix_from_real` 为什么必须用 **30 位分块（ChunkSize）** 配合本地 `real_mod` 来处理大位宽，并能手工模拟一次分块过程。
4. 理解 VHDL `cl_fix_to_real` 的「符号位校正 + 逆向分块累加」算法。
5. 能解释同一个 40 位格式 `[0,0,40]` 在 Python（走 float）与 VHDL（走分块）中为何得到 bit-true 一致的结果。

## 2. 前置知识

本讲承接 u2-l2（Python 函数地图与 `r_fmt` 缺省约定）和 u4-l2（饱和四种模式）。你需要已经掌握：

- **格式三字段 `[S,I,F]`**：S 符号位、I 整数位、F 小数位，总位宽 `S+I+F`（见 u1-l2）。
- **定点值的位权公式**：第 k 位的权重为 \(w_k = 2^{k-F}\)，有符号数的符号位权重为 \(-2^{I}\)。
- **饱和模式** `None_s / Warn_s / Sat_s / SatWarn_s`：是否钳位、是否告警的组合（见 u4-l2）。
- **NarrowFix / WideFix 双表示**：位宽 ≤ 53 走 NarrowFix（float64 内部），否则走 WideFix（任意精度整数），由 `cl_fix_is_wide` 分发（见 u2-l2）。

几个本讲要用到的术语：

- **归一化（normalized）**：定点值用「真实物理数值」表示，例如 `2.5`。`from_real` / `to_real` 处理的就是归一化数据。
- **非归一化（unnormalized）**：定点值用「把整串比特当一个普通整数看」的原始整数值表示，例如 `2.5` 在 `[0,2,1]` 下存为整数 `5`。`from_integer` / `to_integer` 处理的就是非归一化数据。
- **VHDL `real` 类型**：本质是 IEEE 754 双精度浮点（与 Python `float` 同构），但 VHDL 的 `integer` 类型只有 **32 位有符号**（范围约 \(\pm 2^{31}\)）。这个「`integer` 只有 32 位」的限制，正是本讲 VHDL 部分要绕过的核心障碍。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | NarrowFix（≤53 位）类：`from_real`、`from_integer`、`to_integer`、`MAX_WIDTH` 的 float64 实现 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | Python 主接口：`cl_fix_from_real` / `cl_fix_from_integer` / `cl_fix_to_integer` / `cl_fix_to_real`，在 narrow/wide 间分发 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | VHDL 镜像：`cl_fix_from_real`（含 `real_mod` 与 30 位分块）、`cl_fix_to_real`、`cl_fix_from_integer` / `cl_fix_to_integer` |
| [bittrue/tests/python/en_cl_fix_pkg_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py) | 四个转换函数的断言式单元测试，是理解正确行为的最佳依据 |

## 4. 核心概念与源码讲解

### 4.1 归一化与非归一化：两种读法（from_integer / to_integer）

#### 4.1.1 概念说明

同一串定点比特，有两种等价的「读法」：

- **归一化读法**：直接读成真实物理值。比特 `010.1`（格式 `[0,2,1]`）读成 `2.5`。
- **非归一化读法**：忽略小数点的位置，把整串比特当一个无符号/有符号整数看。`010.1` 读成二进制 `0101` = 整数 `5`。

二者之间的桥梁就是小数位数 `F`：

\[
\boxed{\;\text{归一化值} \;=\; \text{非归一化整数} \;\times\; 2^{-F}\;}
\]

所以 `2.5 = 5 \times 2^{-1}`。`from_real` / `to_real` 走归一化通道，`from_integer` / `to_integer` 走非归一化通道。两套接口互相**正交**，互为逆运算：`to_integer(from_integer(x)) = x`、`to_real(from_real(x)) = x`（在不溢出、不损失的前提下）。

#### 4.1.2 核心流程

`from_integer`（整数 → 定点）与 `to_integer`（定点 → 整数）的非归一化流程：

```
from_integer(a_int, fmt):           to_integer(value, fmt):
  value = a_int / 2**fmt.F            a_int = round(value * 2**fmt.F)
  检查 value 是否在 fmt 范围内          返回 a_int（narrow 路径为 int64）
  （越界则抛 ValueError）
```

注意方向：`from_integer` 是「除以 \(2^F\)」（把整数缩放成真实值），`to_integer` 是「乘 \(2^F\) 再取整」（把真实值还原成整数）。

#### 4.1.3 源码精读

**Python NarrowFix 的整数转换**。[NarrowFix.from_integer](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L98-L108) 把非归一化整数除以 \(2^F\) 得到归一化 float，并做范围检查：

```python
# narrow_fix.py:L105-L107  除以 2^F 归一化，然后检查是否落在格式范围内
value = np.array(a/2**a_fmt.F, dtype=np.float64)
if not np.all(NarrowFix(value, a_fmt).in_range(a_fmt)):
    raise ValueError("NarrowFix.from_integer: Value not in number format range")
```

`to_integer` 是逆操作，[NarrowFix.to_integer](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L139-L145) 用 `np.round` 乘回去：

```python
# narrow_fix.py:L145  归一化值 × 2^F 再四舍五入 → 原始整数
return np.array(np.round(self._data*2.0**self._fmt.F), dtype=np.int64)
```

**Python 主接口的分发**。在 [en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L145-L170) 中，`cl_fix_from_integer` / `cl_fix_to_integer` 根据宽度分发——**对 wide 格式它们是恒等（no-op）**：

```python
# en_cl_fix.py:L153-L156  wide 内部本就存非归一化整数，from_integer 直接原样返回
if cl_fix_is_wide(r_fmt):
    return a
else:
    return NarrowFix.from_integer(a, r_fmt)._data
```

这是一个优雅的设计后果：WideFix 内部表示**就是**非归一化整数（见 [wide_fix.py 文件头注释](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L28-L31)「1.25 in FixFormat(0,2,4) stored as integer 1.25*2**4 = 20」），所以 `from_integer` / `to_integer` 对 wide 数据不需要任何换算。而 NarrowFix 内部存的是**归一化 float**，才需要乘除 \(2^F\)。

**VHDL 的整数转换**。[cl_fix_from_integer](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L878-L885) 直接用 `to_signed` / `to_unsigned` 把 `integer` 截断成 `width` 位的 `std_logic_vector`，**不做范围检查**（与 Python 的 `ValueError` 不同）：

```vhdl
-- en_cl_fix_pkg.vhd:L878-L885  整数直接当比特模式，按符号位有无选 to_signed/to_unsigned
function cl_fix_from_integer(a : integer; aFmt : FixFormat_t) return std_logic_vector is
begin
    if aFmt.S = 1 then
        return std_logic_vector(to_signed(a, cl_fix_width(aFmt)));
    else
        return std_logic_vector(to_unsigned(a, cl_fix_width(aFmt)));
    end if;
end function;
```

[cl_fix_to_integer](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L887-L910) 把比特重新解释为 `integer`，并显式处理两个边界特例（0 位、1 位有符号）以避免 Modelsim 告警：

```vhdl
-- en_cl_fix_pkg.vhd:L893-L902  特例：0 位返回 0；1 位有符号 '1' 在定点里是 -2**I，整数里是 -1
if cl_fix_width(aFmt) = 0 then
    return 0;
elsif aFmt.S = 1 and cl_fix_width(aFmt) = 1 then
    if a_c(0) = '1' then
        return -1;   -- 注意：-1 是整数表示，对应定点值 -2**aFmt.I
    else
        return 0;
    end if;
end if;
```

> **跨语言细节**：1 位有符号格式 `[1,0,0]` 只能表示 `{0, -1}`（补码）。它的「非归一化整数」是 1 比特，`'1'` 解释为有符号整数就是 `-1`，但定点物理值是 \(-2^{I}=-1\)。代码注释特意点出这个 \(-1\) 在两种读法下的含义差别。

#### 4.1.4 代码实践

**实践目标**：用真实测试断言验证「整数 ↔ 定点」的换算关系。

**操作步骤**（在仓库根目录，需先 `pip install -r requirements.txt`）：

```bash
cd bittrue/tests/python
python -c "
from en_cl_fix_pkg import *
# from_integer: 整数 3 在 [0,3,1] 下 = 3 * 2^-1 = 1.5
print('from_integer(3, [0,3,1]) =', cl_fix_from_integer(3, FixFormat(0,3,1)))
# to_integer: 归一化 1.5 在 [0,3,1] 下 = round(1.5 * 2^1) = 3
print('to_integer(1.5, [0,3,1]) =', cl_fix_to_integer(1.5, FixFormat(0,3,1)))
# 越界: 17 > [0,4,0] 的最大值 15，应抛 ValueError
try:
    cl_fix_from_integer(17, FixFormat(0,4,0))
except ValueError as e:
    print('越界捕获:', e)
"
```

**预期结果**：

```
from_integer(3, [0,3,1]) = 1.5
to_integer(1.5, [0,3,1]) = 3
越界捕获: NarrowFix.from_integer: Value not in number format range
```

这与测试文件中的断言完全一致：`assertEqual(1.5, cl_fix_from_integer(3, FixFormat(False,3,1)))`（见 [en_cl_fix_pkg_test.py:L85](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L85)）和 `assertEqual(3, cl_fix_to_integer(1.5, ...))`（[L101](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L101)）。**需要观察的现象**：`from_integer` 与 `to_integer` 互为逆运算；越界只发生在输入整数超出格式范围时。

#### 4.1.5 小练习与答案

**练习 1**：`cl_fix_from_integer(3, FixFormat(True, 2, 1))` 的结果是多少？为什么和 `[0,3,1]` 一样都是 `1.5`？

> **答案**：`1.5`。因为归一化只取决于 `F`（除以 \(2^F\)），与 `S`、`I` 无关。两种格式 `F` 都是 1，所以 `3 × 2^{-1} = 1.5` 相同。`S`/`I` 只决定**范围**与位宽，不决定换算系数。（对照 [L88](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L88)。）

**练习 2**：为什么 `cl_fix_from_integer` 对 wide 格式直接 `return a`，而对 narrow 格式要调用 `NarrowFix.from_integer`？

> **答案**：WideFix 内部本来就以「非归一化整数」存储数据，`from_integer` 的输入恰是这种表示，故无需换算；NarrowFix 内部以「归一化 float」存储，必须除以 \(2^F\) 转换（见 [en_cl_fix.py:L153-L156](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L153-L156)）。

### 4.2 Python from_real：半进位量化 + 饱和

#### 4.2.1 概念说明

`from_real` 把一个浮点数（真实物理值，可能带任意精度）装进一个有限位宽的定点格式。这必然做两件事：

1. **量化（quantization）**：把无限精度的浮点对齐到格式的最小步长 \(2^{-F}\)。`from_real` 固定使用**半进位（half-up）**舍入——即四舍五入、平局向上——因为它不想在浮点域重新实现全部 7 种舍入模式（u4-l1）。若需其他舍入，应改用 `resize()`。
2. **饱和（saturation）**：若（量化前或量化后的）值超出格式范围，按 `saturate` 模式处理。`from_real` **强制要求饱和**——回绕（wrap）在浮点域没有实现，传 `None_s` / `Warn_s` 会抛 `NotImplementedError`。

#### 4.2.2 核心流程

```
from_real(a, r_fmt, saturate=SatWarn_s):
  1. 若需告警(SatWarn/Warn): 比较 a 的 max/min 与格式端点，超界发 warning
  2. 半进位量化:  x = floor(a * 2^F + 0.5) / 2^F
  3. 饱和:
       - Sat/SatWarn: 用 np.where 把 x 钳到 [min_value, max_value]
       - None/Warn:    raise NotImplementedError（回绕未实现）
  4. 返回 NarrowFix(x, r_fmt)
```

格式范围由 `max_value = 2^{I} - 2^{-F}`、`min_value = S ? -2^{I} : 0` 给出（与 u4-l2 一致）。

#### 4.2.3 源码精读

[NarrowFix.from_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L67-L96) 三段式实现：

```python
# narrow_fix.py:L75-L82  告警：仅在 SatWarn/Warn 模式下检查并 warn
if (saturate == FixSaturate.SatWarn_s) or (saturate == FixSaturate.Warn_s):
    amax = np.max(a); amin = np.min(a)
    if amax > NarrowFix.max_value(r_fmt)._data:
        warnings.warn(f"NarrowFix: Number {amax} exceeds maximum for format {r_fmt}", Warning)
    ...

# narrow_fix.py:L86  半进位量化核心一行：×2^F、+0.5、floor、÷2^F
x = np.floor(a*(2.0**r_fmt.F)+0.5)/2.0**r_fmt.F

# narrow_fix.py:L89-L94  饱和：Sat/SatWarn 钳位；其余(回绕)未实现
if (saturate == FixSaturate.Sat_s) or (saturate == FixSaturate.SatWarn_s):
    x = np.where(x > NarrowFix.max_value(r_fmt)._data, NarrowFix.max_value(r_fmt)._data, x)
    x = np.where(x < NarrowFix.min_value(r_fmt)._data, NarrowFix.min_value(r_fmt)._data, x)
else:
    raise NotImplementedError(f"NarrowFix: Unsupported saturation mode {str(saturate)}")
```

主接口 [cl_fix_from_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L130-L142) 只是分发到 NarrowFix 或 WideFix，并把结果取 `._data` 返回原始归一化数组：

```python
# en_cl_fix.py:L139-L142  按宽度分发：>53 位走 WideFix，否则 NarrowFix
if cl_fix_is_wide(r_fmt):
    return WideFix.from_real(a, r_fmt, saturate)._data
else:
    return NarrowFix.from_real(a, r_fmt, saturate)._data
```

**关键测试用例**（[en_cl_fix_pkg_test.py:L60-L79](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L60-L79)）把行为钉死，是理解语义的最佳捷径：

| 输入 | 格式 | 结果 | 说明 |
|------|------|------|------|
| `1.2` | `[0,2,2]` | `1.25` | 半进位：\(1.2\times4=4.8\)，\(\lfloor4.8+0.5\rfloor=5\)，\(5/4=1.25\) |
| `-0.52` | `[1,2,2]` | `-0.5` | 半进位向 0 靠拢 |
| `4.2` | `[0,2,2]` | 抛 Warning | 超过 max=3.75 |
| `4.2` | `[0,2,2]`，`Sat_s` | `3.75` | 钳到上界 |
| `3.9` | `[0,2,2]` | 抛 Warning | 3.9 超过 max=3.75 |

#### 4.2.4 代码实践

**实践目标**：亲见半进位量化的「+0.5」如何改变结果。

**操作步骤**：

```bash
cd bittrue/tests/python
python -c "
from en_cl_fix_pkg import *
import warnings
warnings.simplefilter('always')
fmt = FixFormat(0,2,2)   # 步长 0.25, 范围 [0, 3.75]
print('1.2  ->', cl_fix_from_real(1.2, fmt))   # 半进位到 1.25
print('3.9  ->', cl_fix_from_real(3.9, fmt, FixSaturate.Sat_s))  # 钳到 3.75
# 默认 SatWarn_s 时超界会 warn
print('4.2  ->', cl_fix_from_real(4.2, fmt, FixSaturate.Sat_s))  # 钳到 3.75
"
```

**预期结果**：

```
1.2  -> 1.25
3.9  -> 3.75
4.2  -> 3.75
```

**需要观察的现象**：`1.2` 不是格式 `[0,2,2]` 的可表示点（步长 0.25），半进位把它推到最近的 `1.25`；`3.9`、`4.2` 都超过上界 `3.75`，`Sat_s` 模式下被钳到 `3.75`。**待本地验证**：若用默认 `SatWarn_s`（不传第三参），`3.9`/`4.2` 会额外打印一条 `NarrowFix: Number ... exceeds maximum` 告警。

#### 4.2.5 小练习与答案

**练习 1**：`cl_fix_from_real(1.2, FixFormat(0,2,2))` 为什么等于 `1.25` 而不是 `1.0` 或 `1.5`？

> **答案**：半进位量化：\(1.2 \times 2^2 = 4.8\)，加 0.5 得 5.3，`floor` 得 5，再除 \(2^2\) 得 \(5/4 = 1.25\)。这是「最近的」可表示点（1.25 比 1.0 更近 1.2）。

**练习 2**：能否用 `cl_fix_from_real(3.0, FixFormat(0,2,2), FixSaturate.None_s)` 实现「不饱和、直接回绕」？

> **答案**：不能。`from_real` 对 `None_s` / `Warn_s` 会抛 `NotImplementedError`（[narrow_fix.py:L94](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L94)）。回绕需先 `from_real` 饱和，再显式 `cl_fix_saturate(..., None_s)`（见 u4-l2）。注：本例 3.0 在范围内不会触发任何路径问题，但语义上 `from_real` 不支持回绕模式。

### 4.3 VHDL cl_fix_from_real：30 位分块与 real_mod

#### 4.3.1 概念说明

VHDL 版 `cl_fix_from_real` 要解决一个 Python 不存在的问题：**VHDL 的 `integer` 只有 32 位**，而定点格式可以远超 32 位（例如 `[0,0,40]` 是 40 位）。Python 的 NarrowFix 用 float64 直接算 \(\lfloor a\cdot2^F+0.5\rfloor\)（float64 能精确表示 ≤53 位整数，见 [narrow_fix.py:L40-L52](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L40-L52) 的 MAX_WIDTH 注释），但 VHDL 没法把一个 40 位整数塞进 32 位 `integer`。

解法是**分块（chunking）**：把量化后的实数按每 **30 位** 一段，从低位到高位逐段用 `real_mod` 取模剥离，每段单独转成 `std_logic_vector` 再拼接。为什么是 30 而不是 31 或 32？因为 30 位的无符号值最大约 \(2^{30} \approx 1.07\times10^9\)，安全落在 32 位有符号 `integer` 的范围 \(\pm2^{31}\approx\pm2.15\times10^9\) 内，保证 `integer(real_mod(...))` 永不溢出。这是一个精心选定的「安全分块大小」。

此外，函数体内还定义了一个本地 `real_mod`，替代标准库的 `ieee.math_real."mod"`——因为注释明确指出多个工具链（Vivado、Efinity、Gowin EDA）对该运算符有 bug（[en_cl_fix_pkg.vhd:L801-L802](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L801-L802)）。

#### 4.3.2 核心流程

```
cl_fix_from_real(a, result_fmt, saturate=SatWarn_s):
  1. 断言 saturate 必须是 SatWarn_s 或 Sat_s（回绕未实现，与 Python 一致）
  2. 饱和: a 钳到 [min_real, max_real]，超界时按模式 warn
  3. 半进位量化: ASat = floor(ASat * 2^F + 0.5)
  4. 分块循环 i = 0 .. ChunkCount-1:
       Chunk_i  = integer( real_mod(ASat, 2^30) )   -- 取最低 30 位
       Result[i*30 .. (i+1)*30-1] = to_unsigned(Chunk_i, 30)
       ASat = floor(ASat / 2^30)                     -- 右移 30 位，处理下一段
  5. 返回 Result 的低 width 位
```

其中 `ChunkCount = ceil(width / 30)`，`real_mod(a,b) = a - b*floor(a/b)`。

#### 4.3.3 源码精读

[cl_fix_from_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L800-L841) 完整实现：

```vhdl
-- en_cl_fix_pkg.vhd:L803-L806  本地取模，规避多家工具链 ieee.math_real."mod" 的 bug
function real_mod(a, b : real) return real is
begin
    return a - b * floor(a/b);
end function;

-- en_cl_fix_pkg.vhd:L808-L809  分块大小 30；块数 = ceil(width/30)
constant ChunkSize_c    : positive := 30;
constant ChunkCount_c   : positive := (cl_fix_width(result_fmt) + ChunkSize_c - 1)/ChunkSize_c;
```

强制饱和与饱和钳位：

```vhdl
-- en_cl_fix_pkg.vhd:L814-L828  必须饱和；用 max_real/min_real（见 L276-L288）钳位并告警
assert saturate = SatWarn_s or saturate = Sat_s
    report "cl_fix_for_real: Saturation mode must be SatWarn_s or Sat_s" severity Failure;
if a > max_real(result_fmt) then
    assert saturate = Sat_s report "cl_fix_from_real : Saturation warning!" severity Warning;
    ASat_v := max_real(result_fmt);
elsif a < min_real(result_fmt) then ... ASat_v := min_real(result_fmt);
else ASat_v := a; end if;
```

半进位量化与分块循环：

```vhdl
-- en_cl_fix_pkg.vhd:L831  量化到整数（实数域，可能 > 32 位）
ASat_v := floor(ASat_v * 2.0**result_fmt.F + 0.5);

-- en_cl_fix_pkg.vhd:L834-L838  逐块取模剥离低位，每块 30 位
for i in 0 to ChunkCount_c-1 loop
    Chunk_v := std_logic_vector(to_unsigned(integer(real_mod(ASat_v, 2.0**ChunkSize_c)), ChunkSize_c));
    Result_v((i+1)*ChunkSize_c-1 downto i*ChunkSize_c) := Chunk_v;
    ASat_v := floor(ASat_v/2.0**ChunkSize_c);   -- 右移 30 位
end loop;

return Result_v(cl_fix_width(result_fmt)-1 downto 0);  -- L840 截到实际宽度
```

> **Python vs VHDL 的对称性**：两者都做「饱和 → 半进位量化」，差别只在量化后如何落地成比特——Python 用 float64 直接表示（≤53 位天然精确），VHDL 因 `integer` 仅 32 位而不得不分块。**两路结果 bit-true 一致**，这是经穷举对拍保证的（u8 将详述 cosim 流程）。

#### 4.3.4 代码实践

**实践目标**：手工模拟一次 VHDL 分块，验证它与 Python 结果一致。

**场景**：`cl_fix_from_real(0.5, FixFormat(0,0,40), SatWarn_s)`。

**操作步骤**：先算 Python 侧，再手算 VHDL 分块。

1. **Python 侧**（`[0,0,40]`，width=40 ≤ 53，走 narrow）：

```bash
cd bittrue/tests/python
python -c "
from en_cl_fix_pkg import *
fmt = FixFormat(0,0,40)              # 40 位纯小数, 范围 [0, 1-2^-40]
r = cl_fix_from_real(0.5, fmt)
print('归一化值 =', r)
print('原始整数 =', cl_fix_to_integer(r, fmt))   # 0.5 * 2^40
print('二进制  =', bin(int(cl_fix_to_integer(r, fmt))))
"
```

预期：归一化值 `0.5`，原始整数 `549755813888`，二进制 `0b10000000000000000000000000000000000000000`（即 \(2^{39}\)，bit 39 为 1）。

2. **手算 VHDL 分块**（width=40，`ChunkCount = ceil(40/30) = 2`）：

   - 量化：\(ASat = \lfloor 0.5 \times 2^{40} + 0.5 \rfloor = 549755813888\)。
   - `i=0`（低 30 位）：\(549755813888 \bmod 2^{30} = 549755813888 \bmod 1073741824 = 0\) → `Result[29:0]=0`；\(ASat = \lfloor 549755813888 / 2^{30}\rfloor = 512\)。
   - `i=1`（高 10 位）：\(512 \bmod 2^{30} = 512\) → `Result[39:30] = 512 = '1000000000'`（bit 39 为 1）；\(ASat = 0\)。
   - 拼接：bit 39=1，其余 0，即 \(2^{39} = 549755813888\)。

**预期结果**：VHDL 分块拼出的整数 \(2^{39}\) 与 Python `cl_fix_to_integer` 完全相等。两者都表示定点值 `0.5`，证明「float 直接算（Python）」与「30 位分块（VHDL）」殊途同归。**待本地验证**：如果你装了 GHDL/NVC，可参考 u8 的 cosim 流程用 testbench 实跑对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ChunkSize_c` 选 30 而不是 32？

> **答案**：每块要经 `integer(real_mod(...))` 转成 VHDL `integer`（32 位有符号，上界 \(2^{31}-1\)）。30 位无符号值（≤\(2^{30}-1\)）安全落在该范围内；若选 31 或 32，\(2^{31}\) 及以上的值会超出 `integer` 上界导致溢出。30 是兼顾「块尽量大、转换绝不溢出」的保守选择。

**练习 2**：`real_mod(a,b)` 与 VHDL 的 `a mod b` 有何不同？为什么要自己写一个？

> **答案**：`real_mod(a,b) = a - b*floor(a/b)` 用 `floor` 保证对正 `b` 返回非负余数，等价于数学取模。自己写是因为注释（[L801-L802](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L801-L802)）指出 Vivado/Efinity/Gowin EDA 等工具链对 `ieee.math_real."mod"` 实现有 bug，本地版本可移植、可综合。

### 4.4 VHDL cl_fix_to_real：符号位校正与逆向分块累加

#### 4.4.1 概念说明

`cl_fix_to_real` 是 `from_real` 的逆运算：把一串 `std_logic_vector` 比特还原成 `real`。它同样要分块（因为 `integer` 装不下宽格式），但方向相反——**从高位块向低位块**逐块累加，等价于「从最高有效位开始，每位 `result = result*2 + bit`」但以 30 位块为单位。

有符号数还多一步：补码的符号位权重是 \(-2^{I}\)（负的），但其余位都是正权重。算法先把符号位**临时清零、记下它的负权重**（`Correction_v`），对剩下的「全正权重」比特做无符号累加，最后再把负的符号位贡献加回去。

#### 4.4.2 核心流程

```
cl_fix_to_real(a, a_fmt):
  1. 若有符号且最高位='1':
       清零符号位；Correction = -2^(width-1-F)   -- 记住它的负权重
  2. resize 到整数块数 (ChunkSize*ChunkCount 位)
  3. 逆向循环 i = ChunkCount-1 downto 0:
       result = result * 2^30                     -- 给上一次累加的块左移 30 位
       result = result + chunk_i * 2^(-F)         -- 加本块（归一化）
  4. result = result + Correction                 -- 加回符号位贡献
  5. 返回 result
```

#### 4.4.3 源码精读

[cl_fix_to_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L843-L876)：

```vhdl
-- en_cl_fix_pkg.vhd:L857-L860  符号位处理：清零并记下负权重
if a_fmt.S = 1 and a_v(ABits_c-1) = '1' then
    a_v(ABits_c-1) := '0';                                  -- 清零符号位
    Correction_v := -2.0**(ABits_c-1 - a_fmt.F);            -- 记住它的权重
end if;

-- en_cl_fix_pkg.vhd:L866-L870  从高块到低块累加：先左移 30 位，再加本块（带 2^-F 归一化）
for i in ChunkCount_c-1 downto 0 loop
    result_v := result_v * 2.0**ChunkSize_c;                -- 左移到下一块
    Chunk_v := apad_v((i+1)*ChunkSize_c-1 downto i*ChunkSize_c);
    result_v := result_v + real(to_integer(Chunk_v)) * 2.0**(-a_fmt.F);
end loop;

result_v := result_v + Correction_v;   -- L873 加回符号位贡献
```

> **方向对比**：`from_real` 的循环是 `0 to ChunkCount-1`（从低块开始，逐块取模剥离），`to_real` 是 `ChunkCount-1 downto 0`（从高块开始，逐块左移累加）。两者互为逆运算，块的编号语义对称。

**Python 侧的精妙简化**。在 [cl_fix_to_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L173-L184) 中，narrow 路径**直接返回输入 `a`**，不做任何计算：

```python
# en_cl_fix.py:L181-L184  narrow 内部本就是归一化 real，to_real 是恒等！
if cl_fix_is_wide(a_fmt):
    return WideFix(a, a_fmt, copy=False).to_real()
else:
    return a   # narrow 数据本身就是归一化 float，无需转换
```

这是 NarrowFix「内部存归一化 float」设计带来的红利：`to_real` 对 narrow 数据是零成本恒等。只有 wide 路径才需要调用 [WideFix.to_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L179-L188) 做 `self._data / 2^F`（因为 wide 内部存的是非归一化整数）。

#### 4.4.4 代码实践

**实践目标**：验证 `from_real` → `to_real` 的往返（round-trip）无损，并对比 Python narrow 的「直接返回」与 VHDL 的「分块累加」。

**操作步骤**：

```bash
cd bittrue/tests/python
python -c "
from en_cl_fix_pkg import *
# 1) from_real -> to_real 往返
fmt = FixFormat(0,0,40)
r = cl_fix_from_real(0.5, fmt)
print('往返 to_real =', cl_fix_to_real(r, fmt))   # 0.5，无损还原

# 2) 有符号往返：-0.75 在 [1,0,2]
fmts = FixFormat(1,0,2)
rs = cl_fix_from_real(-0.75, fmts)
print('有符号往返 =', cl_fix_to_real(rs, fmts))   # -0.75
print('其原始整数 =', cl_fix_to_integer(rs, fmts)) # -3 (= -0.75*4)，补码
"
```

**预期结果**：

```
往返 to_real = 0.5
有符号往返 = -0.75
其原始整数 = -3
```

**需要观察的现象**：`to_real` 完美还原原值（round-trip 无损）；有符号 `-0.75` 的原始整数是 `-3`（\(-0.75\times4\)），在 2 位补码 `[1,0,2]` 中比特为 `01` 的补码取负……实际 `[1,0,2]` 表示 \(\{-1,-0.75,-0.5,-0.25,0,...\}\) 中 `-0.75` 对应整数 `-3`。**手算对照 VHDL `to_real`**：对上面的 `0.5`/`[0,0,40]`，符号位无需处理；高块 512、低块 0，累加 \(512\times2^{-40}\times2^{30} + 0 = 512\times2^{-30}\times2^{30}\cdots\) 化简即 \(2^{39}\times2^{-40}=0.5\)，与 Python 一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 VHDL `cl_fix_to_real` 要先把符号位清零、最后再加 `Correction_v`，而不是直接对整个补码做有符号累加？

> **答案**：因为分块累加用的是 `to_integer(Chunk_v)`（无符号解释），逐块当作正权重相加最简单。补码只有符号位是负权重 \(-2^{I}\)，其余位都是正的。把那一位单独抠出来记成负的 `Correction_v`，剩下的就全是纯正权重的无符号数，累加逻辑统一；最后补上负的符号位贡献即可。

**练习 2**：为什么 Python narrow 路径的 `cl_fix_to_real` 可以直接 `return a`？

> **答案**：NarrowFix 内部用归一化 float64 存数据（即「真实物理值」），`to_real` 期望的输出正是归一化 real，所以输入 `a` 已经是结果，转换是恒等（[en_cl_fix.py:L184](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L184)）。VHDL 内部存的是比特（非归一化），必须主动计算 real。

## 5. 综合实践

把本讲四个模块串起来：用 `cl_fix_from_real` 转换一个**超出 32 位但不超过 53 位**的大格式 `[0,0,40]`，对比 Python 与 VHDL 两条实现路径，并验证往返一致性。

**任务**：

1. 在 Python 中执行：

   ```python
   from en_cl_fix_pkg import *
   fmt = FixFormat(0,0,40)                       # 40 位 > 32 位 integer 上限
   r   = cl_fix_from_real(0.5, fmt)              # narrow 路径(40<=53)
   print('归一化 =', r)
   print('整数   =', cl_fix_to_integer(r, fmt))  # 应为 2^39
   print('往返   =', cl_fix_to_real(r, fmt))     # 应为 0.5
   ```

2. 手工推演 VHDL `cl_fix_from_real(0.5, [0,0,40])` 的分块过程（见 4.3.4），确认它拼出的整数等于上一步的 `cl_fix_to_integer` 输出。

3. 回答：`[0,0,40]` 走的是 narrow 还是 wide 路径？为什么？如果把格式换成 `[0,0,60]`（60 位），Python 会改走哪条路径，`WideFix.from_real` 的量化公式（[wide_fix.py:L84](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L84)）与 NarrowFix 有何异同？

**参考答案要点**：
- `[0,0,40]` width=40 ≤ 53 → narrow 路径，Python 用 float64 算 \(\lfloor0.5\times2^{40}+0.5\rfloor\)；VHDL 用 2 个 30 位块（512, 0）拼出 \(2^{39}\)。两者相等。
- `[0,0,60]` width=60 > 53 → wide 路径。`WideFix.from_real` 量化公式 `x = a*2^F + 0.5` 后转**任意精度整数**（`astype('object')`），而非 float；这样能精确表示 >53 位的整数，而 NarrowFix 的 float64 做不到。两者都做「半进位 + 饱和」，差别仅在内部数值类型（float vs 任意精度 int）。
- **待本地验证**：第 3 问若用 `[0,0,60]` 实跑 `cl_fix_from_real`，注意 `to_real` 会触发精度告警（[wide_fix.py:L185-L187](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L185-L187)，因为 60 位超过 float64 的 52 位尾数）。

## 6. 本讲小结

- **归一化 vs 非归一化**：定点值 = 原始整数 × \(2^{-F}\)。`from_real`/`to_real` 走归一化通道，`from_integer`/`to_integer` 走非归一化通道，二者正交互逆。
- **Python 的实现分表示**：NarrowFix 内部存归一化 float，故 `to_real` 对 narrow 是恒等 `return a`、`from_integer` 要除 \(2^F\)；WideFix 内部存非归一化整数，故 `from_integer`/`to_integer` 对 wide 是恒等。这种差异让分发逻辑极其简洁。
- **from_real = 半进位量化 + 强制饱和**：固定用 `floor(a·2^F+0.5)/2^F`，不支持回绕（`None_s`/`Warn_s` 抛 `NotImplementedError`）。
- **VHDL 的 30 位分块**：因为 `integer` 只有 32 位，VHDL 把量化后的实数按 30 位一段逐块取模（`real_mod`）剥离成比特；30 是保证 `integer(...)` 不溢出的保守块大小。
- **VHDL to_real 的符号位校正**：先清零符号位、记其负权重，对剩余正权重比特从高块到低块左移累加，最后加回校正项。
- **三语言 bit-true**：Python（float 或任意精度 int）与 VHDL（分块）路径不同但结果逐位一致，由穷举对拍保证。

## 7. 下一步学习建议

- **u5-l2（算术链路）**：本讲的 `from_real` / `to_real` 是所有算术函数（`add`/`mult`/`shift`）数据进出归一化域的入口；学完转换后，去读它们如何「convert → compute → resize」串成统一三段式。
- **u6（Narrow/Wide 双表示架构）**：本讲多次提到「narrow 存 float、wide 存整数」的内部分歧，u6 会系统讲解 `MAX_WIDTH=53` 的由来、`cl_fix_is_wide` 的分发与 `from_narrowfix` 互转。
- **u8-l1/u8-l3（cosim 与 testbench）**：想看「Python 写黄金参考 → VHDL 读文件对拍」如何**验证**本讲的 bit-true 一致性（尤其是 `cl_fix_from_real` 的 cosim 脚本 [bittrue/cosim/cl_fix_from_real/cosim.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_from_real/cosim.py) 穷举所有格式/模式组合），就继续进入验证单元。
- **延伸阅读**：精读 [en_cl_fix_pkg_test.py 的转换测试段](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L57-L107)，把每个 `assertEqual` 当成一条规格条目，能极大加深对边界行为（越界、半进位、回绕未实现）的直觉。
