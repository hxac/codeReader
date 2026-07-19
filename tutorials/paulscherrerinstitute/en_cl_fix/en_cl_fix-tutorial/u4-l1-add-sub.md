# 加减法与 addsub/saddsub

## 1. 本讲目标

本讲讲解 `en_cl_fix` 中最基础也最常用的两类运算：定点加法与定点减法。学完后你应该能够：

- 用 `FixFormat.ForAdd` / `ForSub` 手算出两个格式相加/相减后的「全精度中间格式」。
- 看懂 `cl_fix_add` / `cl_fix_sub` 的统一三步实现：先把两个操作数无损扩展到中间格式，再做精确加减，最后 `cl_fix_resize` 到目标格式。
- 理解 `cl_fix_addsub` 如何用一个 `add` 选择信号在加法与减法之间切换。
- 理解资源优化变体 `cl_fix_saddsub` 为什么会引入最多 1 LSB 的误差，以及它省下了什么硬件。

本讲是把 [u3-l3（resize 的饱和与回绕）](u3-l3-resize-saturation.md) 里学到的「中间全精度 `TempFmt` → 精确运算 → resize」统一架构，第一次落到具体运算上。掌握了它，下一讲的乘法、移位、均值都会非常自然。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前面几讲）：

- **定点格式 `[S, I, F]`**（u1-l2）：`S` 是否有符号位、`I` 整数位、`F` 小数位，总位宽 \(W = S + I + F\)，数值 \(V = N \cdot 2^{-F}\)。
- **舍入模式 `FixRound`**（u1-l4）与 **饱和模式 `FixSaturate`**（u1-l5）：决定 `resize` 时丢小数位怎么舍、丢整数位怎么处理溢出。
- **`cl_fix_resize` 是全库的心脏**（u3-l2、u3-l3）：它先把数放入一个足够宽的中间格式 `TempFmt_c`，再统一完成舍入与饱和。

一个关键直觉：**两个定点数相加，结果的格式会比两个输入都「大」**。比如两个 `(true,3,5)` 相加，最坏情况 \(+7.96875 + 7.96875 = 15.9375\)，已经超出 `(true,3,5)` 的范围 \([-8, 7.96875]\)，必须多一个整数位。本讲的核心就是回答：**「大」多少？怎么算？** 这正是 `ForAdd` / `ForSub` 要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vhdl/src/en_cl_fix_pkg.vhd` | VHDL 实现：`cl_fix_add` / `cl_fix_sub` / `cl_fix_addsub` / `cl_fix_saddsub` 及内部 `cl_fix_addsub_internal`，含 `TempFmt_c` 构造与综合注释 |
| `python/src/en_cl_fix_pkg/en_cl_fix_types.py` | Python 的 `FixFormat.ForAdd` / `ForSub` 静态方法（格式增长规则的唯一公式来源） |
| `python/src/en_cl_fix_pkg/en_cl_fix_pkg.py` | Python 的 `cl_fix_add` / `cl_fix_sub` / `cl_fix_addsub` / `cl_fix_saddsub`，含 narrow/wide 自动派发 |
| `matlab/src/cl_fix_add.m` | MATLAB 加法（极简：算 `temp_fmt` 后 `a+b` 再 resize） |
| `matlab/src/cl_fix_sub.m` | MATLAB 减法（结构与加法完全一致） |

> 贯穿全讲的一条线索：**三种语言的 add/sub 都收敛到同一个三步模式**——算中间格式 → 无损对齐 → 精确运算 → resize。差别只在「中间格式怎么算」和「语言外壳」。

## 4. 核心概念与源码讲解

### 4.1 加减法的格式增长规则 ForAdd / ForSub

#### 4.1.1 概念说明

做 \(a + b\) 或 \(a - b\) 之前，必须先回答一个问题：**精确结果（没有任何舍入、没有任何截断）至少需要多大的格式才能装得下？** 这个「装得下精确结果」的格式就叫**中间格式（intermediate format）**。

`en_cl_fix` 用两个静态方法来算它：

- `FixFormat.ForAdd(aFmt, bFmt)`：算 \(a + b\) 的精确格式。
- `FixFormat.ForSub(aFmt, bFmt)`：算 \(a - b\) 的精确格式。

为什么加法和减法要分开？因为它们的极端值方向不同：

- 加法 \(a + b\)：极端值在**正方向**（两个大正数相加），需要多 1 个整数位防溢出。
- 减法 \(a - b\)：极端值在**负方向**（小减大），结果可能为负，**必须强制有符号**；而且当 `b` 本身是有符号数时，\(-b\) 的正方向极值会变大，需要给 `b` 多留一位。

#### 4.1.2 核心流程

设 \(a = (S_a, I_a, F_a)\)、\(b = (S_b, I_b, F_b)\)，记 \([P]\) 为「条件为真取 1 否则取 0」。

**ForAdd（加法）公式：**

\[
\text{ForAdd} = \big(\; S_a \lor S_b,\;\; \max(I_a, I_b) + 1,\;\; \max(F_a, F_b) \;\big)
\]

- 符号位：只要有一个输入有符号，结果就可能有符号（无符号 + 无符号 = 无符号）。
- 整数位：取两者较大值后**再加 1**，吸收两个大正数相加产生的进位。
- 小数位：取两者较大值即可——相加不会增加小数精度。

**ForSub（Python 版）公式：**

\[
\text{ForSub} = \big(\; \text{true},\;\; \max\big(I_a,\; I_b + [S_b]\big),\;\; \max(F_a, F_b) \;\big)
\]

- 符号位：**恒为 true**，因为即使两个无符号数相减也可能得到负数。
- 整数位：取 \(I_a\) 与 \(I_b + [S_b]\) 的较大值。注意这里没有加法的「+1」；当 `b` 无符号时 \([S_b]=0\)，因为无符号 `b` 的范围是 \([0, 2^{I_b})\)，\(-b\) 的范围是 \((-2^{I_b}, 0]\)，其绝对值严格小于 \(2^{I_b}\)，用有符号 \(I_b\) 位（最小值恰为 \(-2^{I_b}\)）就能装下，无需额外位。
- 小数位：同样取较大值。

> **跨语言差异（重要）**：MATLAB 的 `cl_fix_sub.m` 直接复用加法公式（`max(IntBits)+1`，比 Python 版多 1 位，偏保守）；VHDL 的 `cl_fix_sub` 不调用 `ForSub`，而是在函数体内联构造 `SubFmt_c`（见 4.2.3）。Python 的 `ForSub` 是三者中最「紧」的。

#### 4.1.3 源码精读

Python 的两个公式定义在类型文件里，只有一行：

[python/src/en_cl_fix_pkg/en_cl_fix_types.py:18-26](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L18-L26) —— `ForAdd` 返回 `(a或b有符号, max整数位+1, max小数位)`；`ForSub` 返回 `(恒true, max(a整数位, b整数位+b有符号), max小数位)`。

MATLAB 的加法与减法用同一个 `temp_fmt` 公式（两者逐字符相同，只是运算符 `+` 换成 `-`）：

[matlab/src/cl_fix_add.m:40-41](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_add.m#L40-L41) —— `temp_fmt = cl_fix_format(a_fmt.Signed || b_fmt.Signed, max(a_fmt.IntBits,b_fmt.IntBits)+1, max(a_fmt.FracBits,b_fmt.FracBits))`，随后 `result = cl_fix_resize(a+b, temp_fmt, result_fmt, round, saturate)`。

[matlab/src/cl_fix_sub.m:41-42](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_sub.m#L41-L42) —— 与加法同一 `temp_fmt`，运算改为 `a-b`。可见 MATLAB 把减法也当成「多 1 位整数位」来处理，比 Python 的 `ForSub` 宽 1 位。

#### 4.1.4 代码实践

**目标**：手算 + 代码验证 `ForAdd` / `ForSub`。

选定 `aFmt = (true, 3, 5)`、`bFmt = (false, -2, 8)`（这正是 VHDL 包头注释里的官方示例格式）。

手算：

| 项 | ForAdd | ForSub（Python） |
|----|--------|------------------|
| Signed | `T or F = True` | 恒 `True` |
| IntBits | `max(3,-2)+1 = 4` | `max(3, -2+0) = 3` |
| FracBits | `max(5,8) = 8` | `max(5,8) = 8` |
| **结果** | **(True, 4, 8)**，宽 \(1+4+8=13\) | **(True, 3, 8)**，宽 \(1+3+8=12\) |

操作步骤（在 `python/unittest` 目录下，或任何能 `import` 到包的位置）：

```python
# 示例代码
import sys
sys.path.append("../src")            # 指向 python/src
from en_cl_fix_pkg import *

aFmt = FixFormat(True, 3, 5)
bFmt = FixFormat(False, -2, 8)
print("ForAdd =", FixFormat.ForAdd(aFmt, bFmt))   # 期望 (True, 4, 8)
print("ForSub =", FixFormat.ForSub(aFmt, bFmt))   # 期望 (True, 3, 8)
```

**预期结果**：`ForAdd = (True, 4, 8)`、`ForSub = (True, 3, 8)`，与上表一致。注意 `ForSub` 比 `ForAdd` 少 1 个整数位——这正是 Python 版减法公式比 MATLAB 版「紧」的体现。

#### 4.1.5 小练习与答案

**练习 1**：`aFmt = (false, 4, 0)`、`bFmt = (false, 4, 0)`，求 `ForAdd` 与 `ForSub`。

**答案**：`ForAdd = (False, 5, 0)`（两个无符号 4 位数相加最大 \(15+15=30\)，需 5 位）；`ForSub = (True, 4, 0)`（减法强制有符号；`b` 无符号故 `I_b+[S_b]=4`，`max(4,4)=4`，结果范围 \([-15, 15]\) 用有符号 4 位整数可表示）。

**练习 2**：把上题的 `bFmt` 改成 `(true, 4, 0)`，`ForSub` 的整数位变成多少？为什么？

**答案**：变成 5。因为 `b` 有符号，\([S_b]=1\)，`I_b + 1 = 5`，`max(4, 5) = 5`。原因是 `b` 的最负值 \(-16\) 取反后变成 \(+16\)，而有符号 4 位整数最大只能表示 \(+15\)，装不下 \(+16\)，必须多一位。

---

### 4.2 cl_fix_add 与 cl_fix_sub 的统一 TempFmt 模式

#### 4.2.1 概念说明

知道了中间格式，`cl_fix_add` / `cl_fix_sub` 的实现就非常固定——它们都遵循 u3-l3 提出的**统一架构**：

> 任何运算 = 「把操作数无损搬进一个足够大的中间格式」+「在那个格式里做精确运算」+「最后 `resize` 到目标格式」。

这套模式的好处是：**精度损失只发生在最后一步 `resize`**，而那一步的舍入/饱和行为我们已经在前几讲彻底搞清楚了。换句话说，加法/减法本身不引入任何新算法，它们只是 `cl_fix_resize` 的「上层封装」。

#### 4.2.2 核心流程

`cl_fix_add(a, aFmt, b, bFmt, rFmt, rnd, sat)` 的执行流程（三语言一致）：

```text
1. midFmt = ForAdd(aFmt, bFmt)              # 算出能装下精确和的中间格式
2. a' = resize(a, aFmt -> midFmt, Trunc_s, None_s)   # 无损扩展：只补符号位/0
   b' = resize(b, bFmt -> midFmt, Trunc_s, None_s)   # 无损扩展
3. temp = a' + b'                            # 精确加法（已对齐到同一格式同一位宽）
4. result = resize(temp, midFmt -> rFmt, rnd, sat)    # 唯一丢精度的地方
```

关键细节：

- 第 2 步用 `Trunc_s` + `None_s`：扩展操作数时**既不舍入也不饱和**，只是把数搬到更宽的格式（补的是符号扩展位或前导零），保证这一步零误差。
- 第 3 步要求两个操作数**位宽相同**（都已 resize 到 `midFmt`），这样 VHDL 的 `signed + signed` / `unsigned + unsigned` 才能正确对齐。
- `cl_fix_sub` 与之完全对称，只是把 `ForAdd` 换成 `ForSub`、把 `+` 换成 `-`。

#### 4.2.3 源码精读

**Python `cl_fix_add`**：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:327-339](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L327-L339) —— 第 331 行 `midFmt = FixFormat.ForAdd(aFmt, bFmt)`；第 332 行用 `cl_fix_is_wide(midFmt)` 判断是否需要走大位宽路径（>53 位见 u6-l1）；第 336-337 行把 `a`、`b` 分别 resize 到 `midFmt`；第 339 行 `cl_fix_resize(a + b, midFmt, rFmt, rnd, sat)`。

**Python `cl_fix_sub`** 结构完全镜像：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:341-353](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L341-L353) —— 唯一差别是 `midFmt = FixFormat.ForSub(...)`（第 345 行）与 `a - b`（第 353 行）。

**VHDL `cl_fix_add`** 把同一模式展开得更显式，并加入综合导向的细节：

[vhdl/src/en_cl_fix_pkg.vhd:2348-2380](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2348-L2380) ——
- 第 2356-2362 行 `CarryBit_c`：判断是否需要为加法预留进位位。条件是「结果整数位比输入大」或「需要饱和」（`Sat_s`/`SatWarn_s`，以及被 `-- synthesis translate_off` 包裹的 `Warn_s`——后者只在仿真生效，避免综合出多余硬件，详见 u1-l5 与 u7-l1）。
- 第 2363-2368 行 `TempFmt_c`：与 Python `ForAdd` 同构，整数位 = `max(a,b) + CarryBit`。
- 第 2375-2376 行把 `a`、`b` resize 到 `TempFmt_c`（`Trunc_s, None_s`，无损）。
- 第 2377 行调用内部函数做真正的加法。
- 第 2378 行 resize 到 `result_fmt`。

**VHDL `cl_fix_sub`** 多了一个「饱和时切换为有符号」的技巧：

[vhdl/src/en_cl_fix_pkg.vhd:2384-2423](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2384-L2423) —— 第 2392-2397 行算 `Saturate_c` 与 `Grow_c`；第 2399-2404 行构造 `SubFmt_c`（整数位 = `max(a,b) + (Grow_c or Saturate_c)`）；第 2406-2411 行另造一个 `ReszFmt_c`——**当需要饱和时把符号位强制置真**（`Signed => SubFmt_c.Signed or Saturate_c`），因为饱和到负边界需要按有符号补码解释，否则 `cl_fix_resize` 的饱和分支无法正确夹紧（u3-l3）。

> 注意：VHDL 这里的 `SubFmt_c` 并不等于 Python 的 `ForSub`，它不在构造期强制有符号、也不加 `b.Signed` 那一位。两种语言对「极端溢出」的中间格式取法不同，但对常规（不溢出）输入结果一致；若做跨语言位真对比，建议结果格式留足整数位并用显式 `Sat_s`/`SatWarn_s`（见 u2-l3 的同类提醒）。

#### 4.2.4 代码实践

**目标**：用 VHDL 包头官方示例的格式与数值，在 Python 里跑一遍加法，观察「精度只在 resize 时丢失」。

操作步骤：

```python
# 示例代码
import sys
sys.path.append("../src")
from en_cl_fix_pkg import *

aFmt = FixFormat(True, 3, 5)
bFmt = FixFormat(False, -2, 8)
rFmt = FixFormat(True, 3, 3)

# 先把输入量化到位真网格（half-up），与 VHDL 示例 cl_fix_from_real 对齐
a = cl_fix_from_real(-3.134, aFmt, FixSaturate.SatWarn_s)
b = cl_fix_from_real( 0.1,   bFmt, FixSaturate.SatWarn_s)
print("a =", a, " b =", b)

# 加法：NonSymPos_s 舍入 + Sat_s 饱和（与 VHDL 示例一致）
r = cl_fix_add(a, aFmt, b, bFmt, rFmt, FixRound.NonSymPos_s, FixSaturate.Sat_s)
print("add result =", r)
```

**需要观察的现象**：

1. `a` 被 `(true,3,5)` 量化后约为 \(-3.125\)（网格间距 \(2^{-5}=0.03125\)）；`b` 被 `(false,-2,8)` 量化后约为 \(0.09375\)。
2. 精确和约为 \(-3.03125\)，落在 `(true,3,3)`（网格 \(2^{-3}=0.125\)，范围 \([-8, 7.875]\)）内，不触发饱和。
3. 最终结果经 `NonSymPos_s` 舍入到 \(2^{-3}\) 网格。

**预期结果**：`add result` 约为 \(-3.0\)（精确数值「待本地验证」，取决于 half-up 量化与舍入的舍入方向）。把 `rFmt` 改成 `(true,4,8)`（等于 `ForAdd`）再跑一次，结果应等于 `a + b` 本身，验证「中间格式够宽时加法零误差」。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 2 步 resize 操作数时必须用 `Trunc_s, None_s`，而不能用 `NonSymPos_s, Sat_s`？

**答案**：因为这一步的目的是「无损对齐」，只允许向更宽的格式扩展（补符号位或前导零）。`Trunc_s` 在扩展（不丢位）时是恒等操作，`None_s` 保证不夹紧。若用舍入/饱和，就会在加法之前先篡改操作数的值，破坏「精确运算」的前提。

**练习 2**：两个 `(true,3,5)` 相加，结果格式也指定为 `(true,3,5)`，会发生什么？

**答案**：`ForAdd = (true, 4, 5)`，精确和可能达到 \(+15.9375\)，超出 `(true,3,5)` 的上界 \(+7.96875\)。最终 resize 时若 `sat=None_s` 则回绕（符号可能反转），若 `sat=Sat_s` 则夹紧到 \(+7.96875\)。

---

### 4.3 addsub_internal 与加减切换 cl_fix_addsub

#### 4.3.1 概念说明

很多时候硬件需要同一个加法器既能做加法又能做减法（典型如滤波器系数正负切换、I/Q 路加减）。`en_cl_fix` 提供两层支持：

- **`cl_fix_addsub_internal`**（VHDL 内部函数）：真正执行「按 `add` 信号选择 `+` 或 `-`，并自动选用 `signed`/`unsigned` 类型」的核心。它只在包内部使用。
- **`cl_fix_addsub`**（对外接口）：完整的「带舍入/饱和的加减法器」。

Python 没有独立的 `_internal`，而是在 `cl_fix_addsub` 里直接同时算出加法和减法两个结果，再用 `np.where` 选择。

#### 4.3.2 核心流程

**VHDL `cl_fix_addsub_internal(a, aFmt, b, bFmt, add)`**：

```text
IsSigned = aFmt.Signed or bFmt.Signed
if add = '1':
    return IsSigned ? signed(a)+signed(b) : unsigned(a)+unsigned(b)
else:
    return IsSigned ? signed(a)-signed(b) : unsigned(a)-unsigned(b)
```

注意调用方（`cl_fix_add`/`cl_fix_sub`/`cl_fix_saddsub`）在调用前已把 `a`、`b` 都 resize 到**同一 `TempWidth_c`**，所以传入时两者位宽一致；这样 `signed(a) ± signed(b)` 的结果位宽也就确定等于 `TempWidth_c`，语义清晰、利于综合。

**VHDL `cl_fix_addsub`**（对外）：只是个分发器——`add='1'` 调 `cl_fix_add`，`add='0'` 调 `cl_fix_sub`。

**Python `cl_fix_addsub`**：算 `radd = cl_fix_add(...)`、`rsub = cl_fix_sub(...)`，返回 `np.where(add, radd, rsub)`。这里 `add` 可以是标量布尔，也可以是布尔数组（对向量化数据逐元素选择加或减）。

#### 4.3.3 源码精读

VHDL 内部函数：

[vhdl/src/en_cl_fix_pkg.vhd:2320-2344](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2320-L2344) —— 第 2325 行 `IsSigned_c := a_fmt.Signed or b_fmt.Signed`；第 2330-2342 行按 `add` 与 `IsSigned_c` 四种组合选用 `signed`/`unsigned` 的 `+`/`-`。注释（第 2328-2329 行）特别说明：综合工具对 `signed`/`unsigned` 类型敏感，必须用对类型，否则可能综合出错。

VHDL 对外分发器：

[vhdl/src/en_cl_fix_pkg.vhd:2427-2444](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2427-L2444) —— 第 2438-2442 行 `if to01(add)='1' then cl_fix_add(...) else cl_fix_sub(...)`。

Python 版：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:355-362](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L355-L362) —— 第 360-361 行分别调用 `cl_fix_add` 与 `cl_fix_sub`，第 362 行 `np.where(add, radd, rsub)` 完成逐元素选择。

#### 4.3.4 代码实践

**目标**：用一个布尔数组驱动 `cl_fix_addsub`，对同一组输入同时做「前一半加、后一半减」。

操作步骤：

```python
# 示例代码
import sys, numpy as np
sys.path.append("../src")
from en_cl_fix_pkg import *

aFmt = FixFormat(True, 3, 5)
bFmt = FixFormat(True, 3, 5)
rFmt = FixFormat(True, 4, 5)

a = cl_fix_from_real(np.array([1.0, 2.0, 3.0, 4.0]), aFmt)
b = cl_fix_from_real(np.array([0.5, 0.5, 0.5, 0.5]), bFmt)
add = np.array([True, True, False, False])

r = cl_fix_addsub(a, aFmt, b, bFmt, add, rFmt)
print(r)   # 期望 [1.5, 2.5, 2.5, 3.5]
```

**预期结果**：`[1.5, 2.5, 2.5, 3.5]`——前两个元素是 \(a+b\)，后两个是 \(a-b\)。这验证了 `add` 数组如何逐元素切换运算。

#### 4.3.5 小练习与答案

**练习 1**：VHDL `cl_fix_addsub_internal` 为什么要根据 `IsSigned_c` 选择 `signed` 还是 `unsigned` 类型再做加减？

**答案**：因为二进制补码的加减法在「按位」层面相同，但综合工具需要知道结果的解释方式（最高位是符号位还是数据位）才能正确处理进位/借位与符号扩展。用错类型可能导致综合后的电路语义错误（注释 2328-2329 行专门强调）。

**练习 2**：Python 的 `cl_fix_addsub` 同时计算了 `radd` 和 `rsub`，相比 VHDL 的「按 `add` 二选一调用」是否浪费？

**答案**：在 Python 参考模型里无所谓浪费——它是软件仿真，追求的是与硬件位真一致的结果而非资源最优。真正关心资源的是 VHDL 综合结果，所以资源优化放在了下一节的 `cl_fix_saddsub`。

---

### 4.4 资源优化变体 cl_fix_saddsub

#### 4.4.1 概念说明

`cl_fix_addsub` 虽然精确，但在硬件里「能加能减」通常意味着需要一个完整的加减法器（或加法器 + 取补逻辑）。`cl_fix_saddsub`（前缀 `s` = simple / saving）是一个**用精度换资源**的变体：它永远只做加法，通过「把减数按位取反再相加」来实现减法，从而省下取补所需的进位链，换来更好的时序和更小的面积。

代价是：**当选减法时，结果最多有 1 LSB 的误差**（VHDL 注释第 881 行明确写明）。

#### 4.4.2 核心流程

原理是二进制补码恒等式。对任意整数 \(x\)（按 \(N\) 位补码解释）：

\[
-b \;=\; (\sim b) + 1
\]

其中 \(\sim b\) 是按位取反。于是精确减法：

\[
a - b \;=\; a + (\sim b) + 1
\]

`saddsub` 的做法是**省掉那个 `+1`**，直接算：

\[
a + (\sim b) \;=\; a - b - 1
\]

所以当选减法（`add='0'`）时，结果比精确值少了 1 个 LSB（在中间格式 `temp_fmt` 的分辨率上）。省掉 `+1` 在硬件里就是省掉了加法器的「最低位进位输入」或一个额外的递增器，这对时序（关键路径）和面积都有利。

`cl_fix_saddsub` 的流程：

```text
1. midFmt = ForAdd(aFmt, bFmt)            # 注意：saddsub 用 ForAdd，不用 ForSub
2. a' = resize(a -> midFmt); b' = resize(b -> midFmt)   # 无损对齐
3. if add = '0':  b' = NOT b'             # 减法时把 b 按位取反（硬件只是反相器，几乎免费）
4. temp = a' + b'                          # 永远做加法
5. result = resize(temp, midFmt -> rFmt, rnd, sat)
```

当选加法（`add='1'`）时不取反，`temp = a + b`，与精确 `cl_fix_add` 完全一致——**误差只在减法时出现**。

#### 4.4.3 源码精读

VHDL `cl_fix_saddsub`：

[vhdl/src/en_cl_fix_pkg.vhd:2448-2483](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2448-L2483) —— 与 `cl_fix_add` 几乎同构（同样的 `CarryBit_c`、`TempFmt_c`、resize 两操作数）。关键差别在第 2477-2479 行：

```vhdl
if to01 (add) = '0' then
    b_v := not b_v;
end if;
```

减法时把 `b_v` 按位取反，然后第 2480 行**固定调用** `cl_fix_addsub_internal(..., '1')`（永远加法）。这就是「省掉 `+1`」的实现。

Python `cl_fix_saddsub`：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:364-376](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L364-L376) —— 第 373 行 `temp_fmt = FixFormat.ForAdd(aFmt, bFmt)`；第 374 行 `notAdd = 1` 当减法（`add` 为假）；第 375 行：

```python
temp = a + (-1.0) ** notAdd * b - notAdd * 2.0 ** -temp_fmt.FracBits
```

当 `notAdd=1`（减法）时，化简为 `a - b - 2^(-F)`，其中 \(2^{-F}\) 正是 `temp_fmt` 的 1 个 LSB——与 VHDL 的 `a + ~b = a - b - 1`（LSB 单位）逐位对应。注意第 369-371 行：当输入是 `wide_fxp`（>53 位）时直接 `raise NotImplementedError()`，即大位宽暂不支持 s 变体。

> 为什么文档说「最多 1 LSB」而不是「正好 1 LSB」？因为第 5 步的最终 `resize` 还会按 `rnd` 舍入：若 `rFmt` 的小数位比 `temp_fmt` 少，那 1 个 LSB 的偏差可能在舍入中被吸收或放大，但文档保证最终结果与精确 `cl_fix_sub` 之差不超过 `rFmt` 的 1 个 LSB。

#### 4.4.4 代码实践

**目标**：对比 `cl_fix_sub`（精确）与 `cl_fix_saddsub`（`add=False`，减法），亲眼看到 1 LSB 误差。

为了让误差「肉眼可见」，把结果格式设成 `ForAdd(aFmt, bFmt)` 本身（这样最终 resize 不再额外丢精度，误差就锁定在 `temp_fmt` 的 1 LSB = \(2^{-F}\)）：

```python
# 示例代码
import sys
sys.path.append("../src")
from en_cl_fix_pkg import *

aFmt = FixFormat(True, 3, 5)
bFmt = FixFormat(False, -2, 8)
midFmt = FixFormat.ForAdd(aFmt, bFmt)      # (True, 4, 8)，1 LSB = 2^-8 = 0.00390625

a = cl_fix_from_real(-3.134, aFmt, FixSaturate.SatWarn_s)
b = cl_fix_from_real( 0.1,   bFmt, FixSaturate.SatWarn_s)

# 精确减法
r_exact = cl_fix_sub(a, aFmt, b, bFmt, midFmt, FixRound.Trunc_s, FixSaturate.None_s)
# s 变体减法（add=False）
r_s     = cl_fix_saddsub(a, aFmt, b, bFmt, False, midFmt, FixRound.Trunc_s, FixSaturate.None_s)

print("exact   =", r_exact)
print("saddsub =", r_s)
print("diff    =", r_exact - r_s)
print("1 LSB   =", 2.0**-midFmt.FracBits)
```

**需要观察的现象**：`diff` 应等于 \(2^{-8} = 0.00390625`，即 `saddsub` 比 `cl_fix_sub` 小了正好 1 个 `temp_fmt` 的 LSB（`a - b - 1`）。把 `add` 改成 `True`（加法）再跑一次，`diff` 应为 0——验证「误差只在减法时出现」。

**预期结果**：`diff = 0.00390625`（精确数值「待本地验证」输入量化结果，但差值应稳定等于 \(2^{-8}\)）。

#### 4.4.5 小练习与答案

**练习 1**：`cl_fix_saddsub` 选用 `ForAdd` 而不是 `ForSub` 作为中间格式，为什么没关系？

**答案**：因为 s 变体减法实际算的是 \(a + \sim b\)，本质是一次加法运算，所以用加法的中间格式 `ForAdd`（比 `ForSub` 多 1 个整数位）是安全甚至略保守的选择；多出的位只是留白，不影响正确性。

**练习 2**：如果把 `cl_fix_saddsub` 的注释「最多 1 LSB 误差」改成「正好 1 LSB 误差」，对吗？

**答案**：不对。「正好 1 LSB」只在最终结果格式等于 `temp_fmt` 且用 `Trunc_s` 时成立（如上面实践）。一旦最终 `resize` 改变小数位或用别的舍入模式，那 1 个 LSB 的偏差可能被舍入吸收，所以严谨的说法是「最多 1 LSB」（相对最终结果格式）。

---

## 5. 综合实践

把本讲四块内容串起来：**手算中间格式 → 跑精确 add/sub → 验证 addsub 选择 → 量化 s 变体的误差代价**。

任务：实现一个「可切换加/减的定点运算单元」，对同一组输入比较三种实现的输出。

```python
# 示例代码
import sys, numpy as np
sys.path.append("../src")
from en_cl_fix_pkg import *

# 1. 选定格式（VHDL 官方示例）
aFmt = FixFormat(True, 3, 5)
bFmt = FixFormat(False, -2, 8)
rFmt = FixFormat(True, 3, 3)

# 2. 手算并打印中间格式
print("ForAdd =", FixFormat.ForAdd(aFmt, bFmt))
print("ForSub =", FixFormat.ForSub(aFmt, bFmt))

# 3. 准备输入（向量）
a = cl_fix_from_real(np.array([-3.134, -3.134]), aFmt)
b = cl_fix_from_real(np.array([ 0.1,   0.1  ]), bFmt)

# 4. 三种实现
r_add    = cl_fix_add(a, aFmt, b, bFmt, rFmt, FixRound.NonSymPos_s, FixSaturate.Sat_s)
r_addsub = cl_fix_addsub(a, aFmt, b, bFmt, np.array([True, False]), rFmt,
                         FixRound.NonSymPos_s, FixSaturate.Sat_s)
r_saddsub= cl_fix_saddsub(a, aFmt, b, bFmt, np.array([True, False]), rFmt,
                          FixRound.NonSymPos_s, FixSaturate.Sat_s)

print("add      :", r_add)
print("addsub   :", r_addsub)     # [加, 减]
print("saddsub  :", r_saddsub)    # [加, 减（可能有 1 LSB 误差）]
print("sub vs s :", r_addsub - r_saddsub)
```

**需要观察与解释**：

1. `ForAdd` 与 `ForSub` 是否与 4.1.4 手算一致。
2. `addsub` 的第一个元素（加）应等于 `r_add`；第二个元素（减）应等于 `cl_fix_sub` 的结果。
3. `addsub` 与 `saddsub` 的第一个元素（加法）应完全相等；第二个元素（减法）可能相差最多 1 LSB（`rFmt` 的 \(2^{-3}=0.125\)）。请解释：为什么加法分支完全一致，而减法分支可能有差？用 4.4 的「省掉 `+1`」原理解释。

**预期结果**：加法分支 `diff=0`；减法分支 `diff` 为 0 或 \(\pm 0.125\)（取决于舍入方向，具体数值「待本地验证」）。

## 6. 本讲小结

- **`ForAdd`**：\((S_a\lor S_b,\; \max(I_a,I_b)+1,\; \max(F_a,F_b))\)，那个 `+1` 吸收两个大正数相加的进位。
- **`ForSub`**（Python）：\((\text{true},\; \max(I_a, I_b+[S_b]),\; \max(F_a,F_b))\)，强制有符号；MATLAB 复用加法公式（多 1 位，偏保守）；VHDL 在函数体内联构造 `SubFmt_c`。
- **统一三步模式**：所有 add/sub 都是「算中间格式 → 用 `Trunc_s`+`None_s` 无损对齐两操作数 → 精确加减 → `resize` 到结果」，**精度只在最后一步丢失**。
- **`cl_fix_addsub`**：用 `add` 信号在加/减间切换（VHDL 是 if/else 分发，Python 是 `np.where` 选择，支持逐元素数组）。
- **`cl_fix_addsub_internal`**：VHDL 内部核心，按 `IsSigned` 选用 `signed`/`unsigned` 类型，注释强调综合工具对此敏感。
- **`cl_fix_saddsub`**：减法时把 `b` 按位取反再相加（省掉补码的 `+1`），换来时序/面积优化，代价是**减法结果最多 1 LSB 误差**；加法分支与精确版完全一致。

## 7. 下一步学习建议

- 下一篇 [u4-l2（乘法 cl_fix_mult）](u4-l2-multiply.md) 会用同样的「中间全精度格式 → 精确运算 → resize」模式讲解乘法，重点是 `FixFormat.ForMult`（整数位相加再 +1、小数位相加）这一更剧烈的格式增长规则。本讲对 `TempFmt` 模式的理解是直接前置。
- 想深入「为什么 `CarryBit_c`、`-- synthesis translate_off` 这些综合细节这么写」的读者，可跳到 [u7-l1（VHDL TempFmt 全精度中间格式与综合考量）](u7-l1-vhdl-tempfmt-synthesis.md) 看架构层的系统总结。
- 若对 saddsub 牺牲精度换资源的思路感兴趣，[u4-l3](u4-l3-shift-mean-abs-neg.md) 里的 `sneg`/`sabs` 是同一思想在取反/绝对值上的应用，可对照阅读。
