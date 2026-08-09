# 全精度结果格式：for_add / for_sub / for_addsub

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 en_cl_fix 在做加减法之前要先算一个「全精度中间格式」`mid_fmt`，以及它「保守」在哪。
- 读懂 `FixFormat.for_add` 中关于 `rmax` / `rmin` 两个极值的位增长（bit-growth）推导，并能解释为什么加法最多只增长 1 个整数位。
- 读懂 `FixFormat.for_sub` 中三类特殊情形（无符号减无符号变有符号、幂 2 边界省 1 位、1 位有符号减数保持无符号），并理解符号位 `S` 是如何被推导出来的。
- 理解 `for_addsub` 为什么等于 `union(for_add, for_sub)`，以及「同时支持加减」要付出多宽的代价。
- 对照 Python 与 VHDL 两份实现，确认它们逐行等价（bit-true）。

## 2. 前置知识

本讲假定你已经掌握：

- **定点格式 `[S, I, F]`**：`S` 为符号位个数（0 无符号、1 补码有符号），`I` 为整数位，`F` 为小数位，总位宽 `S+I+F`。（见讲义 u1-l2）
- **位权**：定点数的最大值、最小值。对一个格式 `fmt`：
  - 最大值（无符号时）\( \text{amax} = 2^{I} - 2^{-F} \)；
  - 最小值（有符号时）\( \text{amin} = -2^{I} \)，无符号时为 \( 0 \)。
  - 直觉口诀：「把所有比特当普通整数看，再乘比例因子 \( 2^{-F} \)」。
- **Python 三大核心类型** `FixFormat` / `FixRound` / `FixSaturate`，以及 `FixFormat.width` 属性。（见讲义 u2-l1）
- **VHDL 包的镜像结构**：`FixFormat_t` record、`NullFixFormat_c` 哨兵常数、函数签名分类。（见讲义 u2-l3）

一个关键复习点：`FixFormat` 构造时强制两条断言——`S` 只能取 0 或 1、`I+F >= 0`（见 [en_cl_fix_types.py:61-70](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L61-L70)）。本讲推导出的所有结果格式都天然满足这两条。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | Python 侧 `FixFormat` 类，其中 `for_add` / `for_sub` / `for_addsub` / `union` 是本讲主角 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | VHDL 侧镜像，`cl_fix_add_fmt` / `cl_fix_sub_fmt` / `cl_fix_addsub_fmt` 与 Python 逐行对应 |
| [bittrue/tests/python/format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) | 穷举对拍测试：枚举大量格式组合，验证 `for_*` 给出的格式「够用且不浪费」 |

## 4. 核心概念与源码讲解

### 4.1 全精度中间格式 mid_fmt 与保守推导

#### 4.1.1 概念说明

做硬件加法时，初学者常犯的错误是：把两个 8 位有符号数直接相加，结果还塞回 8 位——一旦溢出，数值就错了。en_cl_fix 的设计哲学是：**先算出一个「保证不溢出」的结果格式 `mid_fmt`，把运算放到这个全精度空间里完成，最后再由用户决定要不要（以及如何）截断/饱和到目标格式 `r_fmt`。**

这里的 `mid_fmt` 就是「全精度中间格式」（full-precision / maximal intermediate format）。它满足两个性质：

1. **够用（sufficient）**：无论两个输入取什么合法值，`a op b` 的真实结果都能被 `mid_fmt` 精确表示，绝不溢出。
2. **不浪费（necessary / optimal）**：在「够用」的前提下，它的整数位 `I` 已经不能再小一格——再小一位就会溢出。

性质 2 是 en_cl_fix 区别于「无脑多加几位」的粗暴做法的关键。本讲的 `for_add` / `for_sub` 就是把性质 1 和性质 2 同时算出来的「最优」结果格式。

另一个贯穿全讲的词是**保守（conservative）**。三个 `for_*` 函数的文档串都写明：

> This is a conservative calculation (it assumes that a and b may take any values). If the values of a and/or b are constrained, then a narrower format may be feasible.

也就是说，推导只看「格式」（取值范围），不看「当前实际数值」。如果你已知某个输入恒为正、或范围受限，理论可以更窄——但库不会替你假设，它永远按最坏情况给。

#### 4.1.2 核心流程

一个算术函数（如 `cl_fix_add`）的整体三段式：

```text
1. mid_fmt = for_add(a_fmt, b_fmt)     # 全精度结果格式（本讲主题）
2. 把 a、b 对齐 convert 到 mid_fmt，做真正的 + / -，得到 mid_v
3. cl_fix_resize(mid_v, mid_fmt, r_fmt) # 再 round + saturate 到用户想要的目标格式
```

第 2、3 步留待单元 5 详讲。本讲只聚焦第 1 步——如何算 `mid_fmt`。

「够用 + 不浪费」如何被验证？看测试 [format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py)。它对每个格式组合做三件事：

```text
rmax = amax + bmax          # 真实结果的最大值
rmin = amin + bmin          # 真实结果的最小值
# 够用：真实极值必须落在 r_fmt 的表示范围内
assert rmax <= cl_fix_max_value(r_fmt)
assert rmin >= cl_fix_min_value(r_fmt)
# 不浪费：把 r_fmt 的 I 减 1 后，就装不下了
smaller = FixFormat(r_fmt.S, r_fmt.I - 1, r_fmt.F)
assert rmax > cl_fix_max_value(smaller) or rmin < cl_fix_min_value(smaller)
```

这段断言逻辑是理解本讲所有推导的「标尺」：任何 `for_*` 的输出，都必须同时通过这两关。

#### 4.1.3 源码精读

`mid_fmt` 在 VHDL 加法函数里被实际使用——`cl_fix_add` 先算 `mid_fmt_c := cl_fix_add_fmt(...)`，再 `convert` 对齐、相加、`resize`（见 [en_cl_fix_pkg.vhd:1149-1172](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1149-L1172)）。当用户不指定 `result_fmt`（即传入哨兵 `NullFixFormat_c`）时，结果就直接用 `mid_fmt`，这正是 u2-l2 提到的「`r_fmt` 缺省即全精度 mid_fmt」约定。

三个 `for_*` 静态方法的文档串都强调「conservative calculation」（见 [en_cl_fix_types.py:73-81](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L73-L81)）。

#### 4.1.4 代码实践

**目标**：亲手用「极值 + 标尺」验证一次，建立对「够用且不浪费」的直觉。

**步骤**：

1. 取 `a_fmt = b_fmt = FixFormat(1, 3, 0)`（4 位有符号，范围 \(-8 \dots 7\)）。
2. 手算：`amax = 7`，`amin = -8`，所以 `a + b` 的 `rmax = 14`、`rmin = -16`。
3. 调用 `FixFormat.for_add(a_fmt, b_fmt)`，应得到 `(1, 4, 0)`（5 位有符号，范围 \(-16 \dots 15\)）。
4. 用标尺检验：\(14 \le 15\) 且 \(-16 \ge -16\)，够用 ✓；把 `I` 减 1 成 `(1,3,0)`（范围 \(-8 \dots 7\)），\(14 > 7\) 装不下，不浪费 ✓。

**预期结果**：`for_add` 返回 `FixFormat(1, 4, 0)`，并通过上述两关。若结果不符，说明你对「够用/不浪费」的理解有偏差。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「先 round/saturate 再相加」是错的，而必须先在 `mid_fmt` 里全精度相加？

**答案**：因为 round/saturate 会改变数值甚至引入溢出回绕。若先把每个操作数各自截断到窄格式，再相加，误差（或溢出）会被放大且无法挽回；而在 `mid_fmt` 里相加保证中间过程零损失，最终的截断只发生一次、且由用户显式控制。

**练习 2**：「保守」推导会不会给出过宽的格式？

**答案**：不会过宽到浪费整数位（性质「不浪费」保证 `I` 最优），但会假设输入可取满整个范围。若你已知输入范围更小，实际可用更窄格式——这需要你在应用层自行收紧，库按最坏情况给。

---

### 4.2 FixFormat.for_add：rmax / rmin 双极值的位增长

#### 4.2.1 概念说明

加法结果格式要同时照顾「最大可能值 `rmax`」和「最小可能值 `rmin`」两端：

- `rmax = amax + bmax` 决定**正方向**需要多少整数位；
- `rmin = amin + bmin` 决定**负方向**（是否需要符号位、需要多少负向整数位）。

`for_add` 的核心结论非常简洁：**整数位的增长量 `growth` 取 `rmax` 增长和 `rmin` 增长的较大者，而每个都最多是 1**。所以加法最多让整数位 `I` 增长 1 位。直觉上：两个范围 \([-2^n, 2^n)\) 的数相加，结果落在 \([-2^{n+1}, 2^{n+1})\)，恰好多 1 位。

小数位则没有悬念：`F = max(a.F, b.F)`——先对齐到较多小数位的那一方，加法才不会丢精度。

#### 4.2.2 核心流程

`for_add(a_fmt, b_fmt)` 的算法（对应 [en_cl_fix_types.py:82-121](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L82-L121)）：

```text
assert a_fmt.width > 0 and b_fmt.width > 0

# —— rmax 方向：会不会比 max(a.I, b.I) 多需要 1 位？——
# 源码用一长串代数化简，最终收敛到一句极简判据：
rmax_growth = 1 if min(a.I, b.I) + min(a.F, b.F) > 0 else 0

# —— rmin 方向：只有「两个都有符号」时，负向才会额外撑出 1 位 ——
rmin_growth = 1 if (a.S == 1 and b.S == 1) else 0

return FixFormat(
    max(a.S, b.S),                                   # 符号位：任一有符号则有符号
    max(a.I, b.I) + max(rmin_growth, rmax_growth),   # 整数位：基底 + 增长
    max(a.F, b.F)                                    # 小数位：取较大
)
```

**关于 `rmax_growth` 判据的直觉**：`amax = 2^{a.I} - 2^{-a.F}`，把两个这样的最大值相加，结果是否顶到 `2^{max(a.I,b.I)+1}`？源码注释把这条布尔条件一步步代数化简，最终等价于 `min(a.I, b.I) + min(a.F, b.F) > 0`（见 [en_cl_fix_types.py:83-111](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L83-L111)）。你不必背这条式子，只需记住：**两个「实打实有数值范围」的格式相加通常都会增长 1 位；只有当某一侧几乎没有整数位、另一侧几乎没有小数位时，才可能不增长。**

**关于 `rmin_growth`**：负向极值 `amin` 在无符号时是 0，有符号时是 \(-2^{I}\)。只有当 `a`、`b` 都有符号，`amin + amin = -2^{a.I} - 2^{b.I}` 才会突破 \(-2^{max(a.I,b.I)}\) 而多需 1 位；只要有一个无符号，其最小值是 0，负向就不会增长。

#### 4.2.3 源码精读

`for_add` 的 `rmax_growth` 那行注释（[en_cl_fix_types.py:112](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L112)）把一整页代数推导浓缩成了一句：

```python
rmax_growth = 1 if min(a_fmt.I, b_fmt.I) + min(a_fmt.F, b_fmt.F) > 0 else 0
```

紧接其后的 `rmin_growth`（[en_cl_fix_types.py:119](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L119)）：

```python
rmin_growth = 1 if a_fmt.S == 1 and b_fmt.S == 1 else 0
```

最后组装结果（[en_cl_fix_types.py:121](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L121)）：整数位增长取 `max(rmin_growth, rmax_growth)` 而非相加——因为正、负两个方向「共享」同一组整数位，谁需要更多就听谁的。

#### 4.2.4 代码实践

**目标**：用三组典型组合，验证 `for_add` 的整数位增长与手算一致。

**操作步骤**（Python，示例代码）：

```python
# 示例代码：需先 sys.path.append 指向 bittrue/models/python
from en_cl_fix_pkg import FixFormat

cases = [
    (FixFormat(0,4,0), FixFormat(0,4,0)),   # 无符号 + 无符号
    (FixFormat(1,3,0), FixFormat(1,3,0)),   # 有符号 + 有符号
    (FixFormat(0,2,0), FixFormat(0,0,1)),   # 整数位多 + 小数位多（不增长）
]
for a, b in cases:
    print(a, "+", b, "->", FixFormat.for_add(a, b))
```

**需要观察的现象与预期结果**（下表均按本讲 4.1.5 的「极值标尺」手算验证过）：

| a_fmt | b_fmt | rmax_growth | rmin_growth | 结果 | 真实结果范围 |
| --- | --- | --- | --- | --- | --- |
| `(0,4,0)` | `(0,4,0)` | 1（min(4,4)+min(0,0)=4>0） | 0 | `(0,5,0)` | \(0 \dots 30\)，落进 \(0 \dots 31\) ✓ |
| `(1,3,0)` | `(1,3,0)` | 1 | 1（均有符号） | `(1,4,0)` | \(-16 \dots 14\)，落进 \(-16 \dots 15\) ✓ |
| `(0,2,0)` | `(0,0,1)` | 0（min(2,0)+min(0,1)=0） | 0 | `(0,2,1)` | \(0 \dots 3.5\)，落进 \(0 \dots 3.5\) ✓ |

第一行是经典结论「两个 N 位无符号相加 → N+1 位」；第二行是「两个 N 位有符号相加 → N+1 位」；第三行展示了「不增长」的情形——一侧只有整数位、另一侧只有小数位时，正方向撑不到下一档，整数位维持 `max(a.I,b.I)=2`。

> 说明：上表为基于源码逻辑的手算结果，运行上述脚本应得到完全一致的输出。若你的环境未装 `numpy`，可只对照表格，逻辑已在 [format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) 的穷举对拍中被覆盖。

#### 4.2.5 小练习与答案

**练习 1**：`for_add(FixFormat(0,4,0), FixFormat(0,4,0))` 为什么结果是 `(0,5,0)` 而不是 `(0,4,0)`？

**答案**：`rmax_growth=1`。两个 4 位无符号最大值都是 15，相加得 30，4 位无符号最多到 15 装不下，必须扩到 5 位（最多 31）。

**练习 2**：举一个 `for_add` 整数位「不增长」的例子，并解释为什么。

**答案**：如 `(0,2,0) + (0,0,1)`。因为 `min(a.I,b.I)+min(a.F,b.F) = min(2,0)+min(0,1) = 0`，`rmax_growth=0`；两者都无符号，`rmin_growth=0`；故整数位维持 `max(2,0)=2`，结果 `(0,2,1)`。直觉：一侧只贡献整数方向的「大」，另一侧只贡献小数方向的「细」，正方向叠加后仍够装。

**练习 3**：两个有符号数相加，为什么 `rmin_growth` 可能为 1 而 `rmax_growth` 可能同时为 1，结果却只增长 1 位而不是 2 位？

**答案**：因为正、负极值共享同一组整数位，`I` 的增量取 `max(rmax_growth, rmin_growth)`。例如 `(1,3,0)+(1,3,0)` 两个增长都是 1，但结果只 +1 位得 `(1,4,0)`，它要同时覆盖正向 14 和负向 \(-16\)。

---

### 4.3 FixFormat.for_sub：符号变化与三类特殊情形

#### 4.3.1 概念说明

减法 `a - b` 比加法复杂得多，因为减法会**改变符号性**：两个无符号数相减，结果可能为负，于是结果格式必须变成有符号。`for_sub` 因此有大量分支。

极值定义变了：

- `rmax = amax - bmin`（最大减最小，正向最远）；
- `rmin = amin - bmax`（最小减最大，负向最远）。

关键难点是：`rmax` 和 `rmin` 两个方向，哪个需要更多整数位、是否需要符号位，要分别推导再取最宽。源码用 `rmaxI`（正向所需整数位）和 `rminI`（负向所需整数位）两个变量，最终 `I = max(rmaxI, rminI)`，符号位 `S` 由负向是否真的为负决定。

`for_sub` 还内嵌了**三类特殊情形**，用于在边界处省下 1 个整数位（保住「不浪费」性质）。

#### 4.3.2 核心流程

`for_sub` 算法（对应 [en_cl_fix_types.py:124-183](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L124-L183)），分两大块：

```text
# —— 正向 rmax = amax - bmin ——
if b.S == 0:                # b 无符号 ⇒ bmin = 0 ⇒ rmax = amax，不增长
    rmaxI = a.I
else:                       # b 有符号 ⇒ bmin = -2^b.I ⇒ 减负等于加正
    growth = b.S if min(a.I, b.I) >= -a.F else 0
    rmaxI = max(a.I, b.I) + growth

# —— 负向 rmin = amin - bmax ——
if a.S == 0:
    if b.width == 1 and b.S == 1:        # 特殊情形①：a 无符号、b 是 1 位有符号
        S = 0; I = rmaxI                  # 结果可保持无符号
    elif b.I == -b.F + 1:                 # 特殊情形②：rmin 恰为 2 的幂，省 1 位
        S = 1; I = max(rmaxI, -b.F)
    else:                                 # 一般情形：a 无符号，结果必有符号
        S = 1; I = max(rmaxI, b.I)
else:                                     # a 有符号
    S = 1
    growth = a.S if min(a.I, b.I) > -b.F else 0
    rminI = max(a.I, b.I) + growth
    I = max(rmaxI, rminI)

return FixFormat(S, I, max(a.F, b.F))
```

三类特殊情形的直觉：

- **情形①（`b` 是 1 位有符号）**：1 位有符号 `b` 只能取 \(0\) 或 \(-1\)。`a` 无符号时 `amin - bmax = 0 - 0 = 0 ≥ 0`，结果恒非负，故 `S=0`、保持无符号。否则按一般规则会被判成有符号而浪费符号位。
- **情形②（`b.I == -b.F + 1`，即 `b` 的 `I+F == 1`）**：此时 `rmin = amin - bmax = 0 - (2^{b.I} - 2^{-b.F})`，而 `2^{b.I} - 2^{-b.F} = 2^{-b.F}`（因为 `b.I = -b.F+1`），于是 `rmin = -2^{-b.F}` 恰好是 2 的幂。一个 \(-2^k\) 的负数只需要 `I = k`（而非 `k+1`），所以省下 1 位。
- **一般情形**：无符号 `a` 减任意 `b`，结果必能取负，故 `S=1`，整数位取 `max(rmaxI, b.I)`。

> 注：源码中还有一段针对 `a.S=1`（有符号 `a`）的 `rmin` 增长推导，逻辑与 `for_add` 的 `rmin` 类似——只有当 `b` 真正能取到足够大的正值时，负向才多撑 1 位。

#### 4.3.3 源码精读

`for_sub` 开头对 `rmax` 的两路分支（[en_cl_fix_types.py:146-150](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L146-L150)）：

```python
if b_fmt.S == 0:
    rmaxI = a_fmt.I
else:
    rmax_growth = b_fmt.S if min(a_fmt.I, b_fmt.I) >= -a_fmt.F else 0
    rmaxI = max(a_fmt.I, b_fmt.I) + rmax_growth
```

`rmin` 部分的三类分支（[en_cl_fix_types.py:163-181](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L163-L181)）——注意三个 `if` 如何精确区分「1 位有符号减数」「幂 2 边界」「一般无符号」：

```python
if a_fmt.S == 0:
    if b_fmt.width == 1 and b_fmt.S == 1:        # 情形①
        S = 0
        I = rmaxI
    elif b_fmt.I == -b_fmt.F+1:                  # 情形②
        S = 1
        I = max(rmaxI, -b_fmt.F)
    else:                                        # 一般情形
        S = 1
        I = max(rmaxI, b_fmt.I)
else:
    # 有符号 a：负向也可能增长
    ...
```

这些分支不是「炫技」——删掉任何一个，[format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) 的「不浪费」断言就会在某个边界组合上失败。

#### 4.3.4 代码实践

**目标**：用四组组合覆盖 `for_sub` 的主要分支（含三类特殊情形），验证符号位与整数位。

**操作步骤**（示例代码）：

```python
from en_cl_fix_pkg import FixFormat

subs = [
    (FixFormat(1,3,0), FixFormat(1,3,0)),  # 有符号 - 有符号
    (FixFormat(0,3,0), FixFormat(0,3,0)),  # 无符号 - 无符号 → 变有符号
    (FixFormat(0,0,1), FixFormat(0,1,0)),  # 幂 2 特殊情形②
    (FixFormat(0,3,0), FixFormat(1,0,0)),  # 1 位有符号减数，特殊情形①
]
for a, b in subs:
    print(a, "-", b, "->", FixFormat.for_sub(a, b))
```

**预期结果**（均经极值标尺手算验证）：

| a_fmt | b_fmt | 结果 | 真实 `rmin..rmax` | 命中分支 |
| --- | --- | --- | --- | --- |
| `(1,3,0)` | `(1,3,0)` | `(1,4,0)` | \(-15 \dots 15\) | 有符号 a，双向各 +1 |
| `(0,3,0)` | `(0,3,0)` | `(1,3,0)` | \(-7 \dots 7\) | 无符号减无符号 → `S=1` |
| `(0,0,1)` | `(0,1,0)` | `(1,0,1)` | \(-1 \dots 0.5\) | 情形②：`b.I=-b.F+1`，省 1 位 |
| `(0,3,0)` | `(1,0,0)` | `(0,4,0)` | \(0 \dots 8\) | 情形①：1 位有符号减数，保 `S=0` |

逐行解读：

- 第 1 行：`amax-bmin = 7-(-8)=15`，`amin-bmax = -8-7=-15`，需覆盖 \(-15 \dots 15\)，`(1,4,0)`（\(-16 \dots 15\)）正好。
- 第 2 行：`0-7 = -7` 能取负，故 `S` 从 0 变 1；正向 `7-0=7` 用 3 位整数位够，得 `(1,3,0)`。
- 第 3 行：`b=(0,1,0)` 满足 `b.I=-b.F+1`（\(1 = -0+1\)），`rmin = 0-1 = -1 = -2^0`，只需 `I=0`，得 `(1,0,1)`；若走一般分支会给 `(1,1,1)`，浪费 1 位。
- 第 4 行：`b=(1,0,0)` 是唯一的 1 位有符号格式（只能取 0 或 \(-1\)），`rmin = 0-0 = 0` 非负，结果保持无符号 `(0,4,0)`；正向 `7-(-1)=8` 需 4 位整数位。

> 说明：上表为基于源码逻辑的手算结果。运行脚本应得到一致输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `for_sub(FixFormat(0,3,0), FixFormat(0,3,0))` 的结果是**有符号**的 `(1,3,0)`？

**答案**：因为 `amin - bmax = 0 - 7 = -7 < 0`，结果能取负值，必须有符号位。这正是「无符号减无符号可能变有符号」的典型。

**练习 2**：在情形②中，为什么 `I` 用 `-b.F` 而不是 `b.I`？

**答案**：此时 `rmin = -2^{-b.F}`，是一个 2 的幂。表示 \(-2^k\) 只需整数位 `I = k`（符号位权重 \(-2^I\) 直接等于该值），这里 \(k = -b.F\)。而一般分支用 `b.I` 会多给一位。注意此条件下 `b.I = -b.F + 1`，所以 `-b.F = b.I - 1`，确实省了 1 位。

**练习 3**：把 `for_sub` 的情形①去掉会怎样？

**答案**：当 `a` 无符号、`b=(1,0,0)` 时，结果实际恒非负（`rmin=0`），但一般分支会强行设 `S=1`，使 [format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) 的「不浪费」断言在符号位维度上失败（给出多余符号位）。情形①正是为修正这一边界而设。

---

### 4.4 for_addsub 与 union，以及 VHDL 镜像 body

#### 4.4.1 概念说明

很多硬件场景下，同一套数据通路要**同时支持加和减**（例如用一个 `add` 控制位选择 `a+b` 或 `a-b`）。此时结果格式必须同时装得下加法结果和减法结果——取两者各自最优格式的「并集」即可。这就是 `for_addsub`。

`union(a, b)` 的定义很简单：对 `S`、`I`、`F` 三个字段分别取 `max`，得到「能同时精确表示 a 和 b 两个格式所有取值」的最小格式。`for_addsub` = `union(for_add, for_sub)`。

本模块同时对照 VHDL 实现 `cl_fix_add_fmt` / `cl_fix_sub_fmt` / `cl_fix_addsub_fmt`，确认三语言 bit-true。

#### 4.4.2 核心流程

```text
for_addsub(a_fmt, b_fmt):
    add_fmt = for_add(a_fmt, b_fmt)
    sub_fmt = for_sub(a_fmt, b_fmt)
    return union(add_fmt, sub_fmt)     # S/I/F 三字段各取 max

union(a_fmt, b_fmt):
    return FixFormat(max(a.S, b.S), max(a.I, b.I), max(a.F, b.F))
```

注意：`for_addsub` 是「两个最优格式的并集」，而不是「先把加和减的真实极值合起来再求最优」。两者在数学上等价（因为 `for_add`/`for_sub` 已各自最优），但实现上复用 `union` 更简洁。验证它的「够用且不浪费」时，[format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) 用的极值是 `rmax = max(amax+bmax, amax-bmin)`、`rmin = min(amin+bmin, amin-bmax)`，即把加、减两套极值合起来取最远端。

#### 4.4.3 源码精读

Python `for_addsub` 与 `union`（[en_cl_fix_types.py:186-198](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L186-L198) 与 [en_cl_fix_types.py:345-362](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L345-L362)）：

```python
@staticmethod
def for_addsub(a_fmt, b_fmt):
    add_fmt = FixFormat.for_add(a_fmt, b_fmt)
    sub_fmt = FixFormat.for_sub(a_fmt, b_fmt)
    return FixFormat.union(add_fmt, sub_fmt)
```

VHDL 侧 `cl_fix_add_fmt` body（[en_cl_fix_pkg.vhd:392-436](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L392-L436)）——注意它把 Python 里的 `rmax_growth` / `rmin_growth` 写成常数，并用自定义 `choose(cond, a, b)` 三目函数替代 Python 的条件表达式：

```vhdl
constant rmax_growth_c : natural := choose(minimum(a_fmt.I, b_fmt.I)
                                          + minimum(a_fmt.F, b_fmt.F) > 0, 1, 0);
constant rmin_growth_c : natural := choose(a_fmt.S = 1 and b_fmt.S = 1, 1, 0);
-- ...
return ( maximum(a_fmt.S, b_fmt.S),
         maximum(a_fmt.I, b_fmt.I) + maximum(rmin_growth_c, rmax_growth_c),
         maximum(a_fmt.F, b_fmt.F) );
```

`choose` 的定义见 [en_cl_fix_pkg.vhd:630-636](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L630-L636)（`condition` 为真返回 `a_fmt` 否则 `b_fmt`）。VHDL 的 `cl_fix_sub_fmt` body（[en_cl_fix_pkg.vhd:438-498](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L438-L498)）和 `cl_fix_addsub_fmt` body（[en_cl_fix_pkg.vhd:500-506](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L500-L506)）与 Python 逐分支对应——把两份代码左右对照阅读，是体会「三语言 bit-true」的最佳方式。

VHDL 与 Python 的一处**风格差异**值得留意：VHDL body 把所有注释放在 `is ... begin` 之间的说明区，推导逻辑放在 `begin` 后的返回语句里；Python 则把注释和方法体混在一起。但两者的**判据和分支完全相同**。

#### 4.4.4 代码实践

**目标**：理解 `for_addsub` 比 `for_add` / `for_sub` 多宽，并对照 Python 与 VHDL 一致。

**操作步骤**（示例代码）：

```python
from en_cl_fix_pkg import FixFormat

a = FixFormat(0,3,0)
b = FixFormat(0,3,0)
print("add   :", FixFormat.for_add(a, b))      # (0,4,0)
print("sub   :", FixFormat.for_sub(a, b))      # (1,3,0)
print("addsub:", FixFormat.for_addsub(a, b))   # union -> (1,4,0)
```

**需要观察的现象与预期结果**：

- `for_add` = `(0,4,0)`（正向 `7+7=14` 要 4 位无符号）。
- `for_sub` = `(1,3,0)`（负向 `0-7=-7` 要有符号）。
- `for_addsub` = `union((0,4,0),(1,3,0)) = (1,4,0)`：`S=max(0,1)=1`、`I=max(4,3)=4`、`F=0`。
- 合并后的真实极值：`rmax = max(14, 7) = 14`，`rmin = min(0, -7) = -7`，`(1,4,0)`（\(-16 \dots 15\)）覆盖且必要（`(1,3,0)` 最大 7 < 14 装不下）。

**对照 VHDL**：在 GHDL/NVC 等仿真器里（或直接阅读 body），对同样的 `(0,3,0)` 与 `(0,3,0)` 调用 `cl_fix_addsub_fmt`，应得到 `(1,4,0)`，与 Python 完全一致。这印证了三语言 bit-true。

> 说明：上述为基于源码逻辑的手算预期。若无仿真器，精读 [en_cl_fix_pkg.vhd:392-506](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L392-L506) 三段 body 即可确认其与 Python 分支一一对应。

#### 4.4.5 小练习与答案

**练习 1**：`for_addsub` 为什么不直接写成「先算加、减合并后的真实 `rmax`/`rmin`，再求最优格式」，而是 `union(for_add, for_sub)`？

**答案**：因为 `for_add` 和 `for_sub` 已经分别给出加、减的最优格式，对它们取 `union`（三字段取 max）在数学上等价于「合并真实极值后求最优」，但实现上能直接复用现成的两个函数和 `union`，更简洁、更不易出错。`union` 的三字段取 max 也保证结果同时覆盖两个格式。

**练习 2**：VHDL `cl_fix_add_fmt` 里为什么用 `choose(cond, 1, 0)` 而不是直接写 `if ... then 1 else 0`？

**答案**：因为该值要作为**常数**（`constant rmax_growth_c : natural`）在 `is...begin` 说明区求值，用于初始化；VHDL 中在说明区写 `if` 语句不方便，而 `choose` 是一个可在常数上下文求值的函数，正好替代 Python 的条件表达式，使两语言代码形神一致。

**练习 3**：`union` 对 `S` 取 `max`，意味着什么？

**答案**：只要两个格式里有一个是有符号（`S=1`），并集就是有符号。这在 `for_addsub` 里意味着：只要减法可能产生负值（`for_sub` 给出 `S=1`），最终 `addsub` 格式就必须有符号——即便加法那一侧是无符号的。

---

## 5. 综合实践

**任务**：写一个小脚本，对若干自选格式组合 `(a_fmt, b_fmt)`，独立计算「够用且不浪费」的最优加/减/加减结果格式，并与 `FixFormat.for_add` / `for_sub` / `for_addsub` 的输出逐一比对，从而**复现** [format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) 的核心断言思想。

**建议步骤**：

1. 选 4\~6 组覆盖不同分支的组合，例如：
   - `(1,3,0)` 与 `(1,3,0)`（均有符号）
   - `(0,4,0)` 与 `(0,4,0)`（均无符号）
   - `(0,2,3)` 与 `(1,1,3)`（混合符号、带小数位）
   - `(0,0,1)` 与 `(0,1,0)`（触发 `for_sub` 幂 2 特殊情形）
2. 对每组：
   - 用 `cl_fix_max_value` / `cl_fix_min_value`（Python 侧返回浮点极值）取 `amax/amin/bmax/bmin`。
   - 手算加法的 `rmax=amax+bmax`、`rmin=amin+bmin`，减法的 `rmax=amax-bmin`、`rmin=amin-bmax`。
   - 调用 `FixFormat.for_add` / `for_sub` / `for_addsub` 得到 `r_fmt`。
   - 检验三件事：① `rmax <= cl_fix_max_value(r_fmt)` 且 `rmin >= cl_fix_min_value(r_fmt)`（够用）；② 把 `r_fmt.I` 减 1 后，`rmax` 或 `rmin` 之一必越界（不浪费）；③ `r_fmt.F == max(a.F, b.F)`。
3. 把结果整理成一张表，与 4.2.4、4.3.4 的预期表互相印证。

**预期现象**：所有组合都通过①②③三条检验。若某组合在②上失败（即 `I` 还能再减 1 仍够用），说明你发现了一个库的「不够最优」——更可能是你手算 `rmax`/`rmin` 时漏掉了某个极端搭配（共有 `amin/amax × bmin/bmax` 四种两两搭配，取最远端）。

**交付物**：一张「组合 → for_add/for_sub/for_addsub 输出 → 够用?/不浪费?」的核对表，并标注每个 `for_sub` 结果命中了 4.3 中的哪条分支。

> 说明：若环境无 `numpy`/`vunit-hdl`，可改为纯源码阅读型实践——对照 [en_cl_fix_types.py:73-198](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L73-L198) 与 [en_cl_fix_pkg.vhd:392-506](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L392-L506)，逐分支手填上表并验证「够用且不浪费」。

## 6. 本讲小结

- en_cl_fix 的算术函数遵循三段式：先算全精度中间格式 `mid_fmt`，在 `mid_fmt` 中无损运算，最后 `resize` 到目标格式。`mid_fmt` 是「够用且不浪费」的最优结果格式。
- 「保守」指只看格式（取值范围）不看实际数值，按最坏情况推导；`for_*` 文档串明确说明若输入受限可更窄。
- `for_add` 的整数位增长来自两个极值方向：`rmax_growth` 由代数化简为 `min(a.I,b.I)+min(a.F,b.F)>0`，`rmin_growth` 仅当两数均有符号时为 1；增长量取两者 `max`，故加法最多 +1 位。
- `for_sub` 难在符号位会变（无符号减无符号可变有符号），并含三类省位特殊情形：1 位有符号减数（保无符号）、幂 2 边界（省 1 位）、一般无符号（必有符号）。
- `for_addsub = union(for_add, for_sub)`，对 `S/I/F` 三字段取 `max`，让单一通路同时支持加减。
- VHDL `cl_fix_add_fmt`/`cl_fix_sub_fmt`/`cl_fix_addsub_fmt` body 与 Python 逐分支对应，用 `choose` 替代条件表达式，是三语言 bit-true 的体现。

## 7. 下一步学习建议

- 下一讲 **u3-l2** 将把同样的「全精度 + 极值推导」方法推广到乘法、取反、绝对值、移位：`for_mult`（含 1 位有符号相乘变无符号的特例）、`for_neg` / `for_abs`、`for_shift`，以及更一般地使用 `union` 与 `for_round`。建议先复习本讲的 `union` 与「极值标尺」，因为 u3-l2 会反复用到。
- 想立刻看到 `mid_fmt` 如何被消费，可跳读 [en_cl_fix_pkg.vhd:1149-1197](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1149-L1197) 的 `cl_fix_add` / `cl_fix_sub` 函数体（单元 5 会系统讲解）。
- 想加深对「够用且不浪费」的信心，建议精读 [format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) 的穷举对拍逻辑——它正是本讲所有推导的最终裁判。
