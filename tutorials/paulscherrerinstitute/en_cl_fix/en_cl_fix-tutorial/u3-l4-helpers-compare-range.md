# 取整辅助、比较与范围检查

## 1. 本讲目标

本讲在 u3-l2（resize 的舍入）与 u3-l3（resize 的饱和/回绕）之后，讲解建立在 `cl_fix_resize` 之上的三类**高层辅助函数**。它们本身不引入新的位级算法，而是把 `cl_fix_resize` 当作"底层原语"组合出常用的工程语义。学完后你应当能够：

- 说清 `cl_fix_fix`/`cl_fix_floor`/`cl_fix_ceil`/`cl_fix_round` 四个取整函数如何用"小数位置零 + 一个固定舍入模式"的 `cl_fix_resize` 实现，并记住每个函数对应哪一种 `FixRound`。
- 解释 `cl_fix_in_range` 为什么要先把数值 `resize` 到一个"多一个整数位"的中间格式，再去比对结果格式的上下界。
- 理解 `cl_fix_compare` 如何用一个统一的 `FullFmt` 把两个**格式不同**的定点数对齐，再通过"翻转符号位"把有符号比较转化为无符号比较。
- 掌握这些辅助函数在 VHDL / Python / MATLAB 三种语言中的**覆盖差异**（这是本讲最重要的实战结论之一）。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前序讲义）：

- **定点格式 `[S,I,F]`** 与位宽 \( W = S + I + F \)（u1-l2）。
- **七种舍入模式 `FixRound`**：`Trunc_s`、`NonSymPos_s`、`NonSymNeg_s`、`SymInf_s`、`SymZero_s`、`ConvEven_s`、`ConvOdd_s`（u1-l4）。其中 `Round_s` 是 `NonSymPos_s` 的别名。
- **四种饱和模式 `FixSaturate`**：`None_s`（回绕）、`Warn_s`、`Sat_s`（夹紧）、`SatWarn_s`（u1-l5）。
- **`cl_fix_resize` 的两阶段机制**：先放入足够宽的中间格式 `TempFmt` 做无损运算/舍入，再按饱和模式夹紧或回绕（u3-l2、u3-l3）。
- **边界值函数**：`cl_fix_max_value` / `cl_fix_min_value` 在 Python / MATLAB 中返回**实数**，在 VHDL 中返回**位串**（u1-l3）。

一个贯穿本讲的关键直觉是：

> **本讲所有函数都不是"新算法"，而是 `cl_fix_resize` 的语义包装。** 理解了 resize，这些函数就只是"选哪种舍入、选哪种饱和、结果格式怎么摆"的配置问题。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vhdl/src/en_cl_fix_pkg.vhd` | VHDL 包，包含本讲全部六个函数的声明与实现（取整四件套、`in_range`、`compare`） |
| `python/src/en_cl_fix_pkg/en_cl_fix_pkg.py` | Python 主体库，**仅**实现 `cl_fix_in_range`（无取整四件套、无 `compare`） |
| `matlab/src/cl_fix_fix.m` | MATLAB 的 `cl_fix_fix`，一个 `.m` 文件对应一个函数 |
| `matlab/src/cl_fix_in_range.m` | MATLAB 的 `cl_fix_in_range`，直接在实数域判断范围 |
| `vhdl/tb/en_cl_fix_pkg_tb.vhd` | VHDL testbench，含 `cl_fix_in_range` 与 `cl_fix_compare` 的断言式用例 |
| `python/unittest/en_cl_fix_pkg_test.py` | Python 单元测试，含 `cl_fix_in_range` 的用例（与 VHDL 逐条对应） |

## 4. 核心概念与源码讲解

### 4.1 取整辅助函数 cl_fix_fix / floor / ceil / round

#### 4.1.1 概念说明

把一个带小数位的定点数"取整为整数"是数字信号处理中最常见的操作之一。不同数学传统对"取整"的定义不同，因此 en_cl_fix 提供了四个语义明确的函数：

| 函数 | 数学语义 | 朝哪个方向取整 | 隐式使用的 `FixRound` |
|------|----------|----------------|------------------------|
| `cl_fix_fix` | 朝零取整（truncate towards zero） | 正数朝下、负数朝上 | `SymZero_s` |
| `cl_fix_floor` | 朝 \(-\infty\) 取整 | 恒朝下 | `NonSymNeg_s` |
| `cl_fix_ceil` | 朝 \(+\infty\) 取整 | 恒朝上 | `NonSymPos_s` |
| `cl_fix_round` | 半值远离零（四舍五入） | 就近、平局远离零 | `SymInf_s` |

例如对 \(+2.7\) 与 \(-2.7\)：

- `fix`：\(2,\ -2\)（都朝零）
- `floor`：\(2,\ -3\)（都朝下）
- `ceil`：\(3,\ -2\)（都朝上）
- `round`：\(3,\ -3\)（远离零）

这四个函数的**结果格式**都一样：保持输入的 `Signed` 与 `IntBits`，把 `FracBits` 强制设为 0。也就是说，小数位被丢掉，整数部分（含可能的舍入进位）保留。

#### 4.1.2 核心流程

四个函数的实现流程完全一致，只是 `FixRound` 参数不同：

```
1. 构造 ResultFmt = (a_fmt.Signed, a_fmt.IntBits, FracBits = 0)
2. 调用 cl_fix_resize(a, a_fmt, ResultFmt, <各自固定的 Round>, None_s)
3. 返回结果
```

要点：

- **`FracBits = 0`**：丢掉所有小数位，所以 `DropFracBits = a_fmt.FracBits`（见 u3-l2）。
- **固定的舍入模式**：每个函数硬编码一种 `FixRound`，调用者无法改。
- **饱和用 `None_s`**：取整本身只在原始整数位范围内活动（最多因舍入进 1 位），结果格式的 `IntBits` 与输入相同，因此设计上不期望发生整数位溢出，用回绕即可。注意：这里 `None_s` 是"不饱和"，若数值恰好在边界发生舍入进位，行为依赖 resize 内部的 `CarryBit` 机制（见 u3-l3）预留位，正常不会丢精度。

#### 4.1.3 源码精读

VHDL 中四个函数体几乎逐字相同，我们看 `cl_fix_fix`：

[vhdl/src/en_cl_fix_pkg.vhd:2130-2141](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2130-L2141) —— 构造 `ResultFmt_c`（`FracBits => 0`），用 `SymZero_s` 舍入、`None_s` 饱和调用 `cl_fix_resize`。

其余三个函数只是把 `SymZero_s` 换成对应的舍入模式：

- [vhdl/src/en_cl_fix_pkg.vhd:2145-2156](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2145-L2156) —— `cl_fix_floor` 用 `NonSymNeg_s`。
- [vhdl/src/en_cl_fix_pkg.vhd:2160-2171](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2160-L2171) —— `cl_fix_ceil` 用 `NonSymPos_s`。
- [vhdl/src/en_cl_fix_pkg.vhd:2175-2186](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2175-L2186) —— `cl_fix_round` 用 `SymInf_s`。

> **跨语言覆盖差异（重要）**：这四个取整函数**只有 VHDL 完整提供**。Python 端完全没有 `cl_fix_fix/floor/ceil/round`（在 [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) 的函数清单中查不到）。MATLAB 端只提供了 `cl_fix_fix`：

[matlab/src/cl_fix_fix.m:19-26](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_fix.m#L19-L26) —— MATLAB 版 `cl_fix_fix`：把 `a_fmt.FracBits` 置 0，再调 `cl_fix_resize` 时显式传 `Round.SymZero_s` 与 `Sat.None_s`，逻辑与 VHDL 一致。

注意该文件头部第 9 行有一句注释 *"NOTE: Don't use this command yet since it is not yet implemented in VHDL."*——这是**历史遗留的过时注释**：`cl_fix_fix` 如今在 VHDL 中已实现（见上面的 2130–2141 行）。读源码时要留意这类注释与代码现状不符的情况。

如果你在 Python 或 MATLAB 中需要 `floor/ceil/round` 语义，可以直接调 `cl_fix_resize` 并手动传入对应的 `FixRound`（`NonSymNeg_s` / `NonSymPos_s` / `SymInf_s`）、结果格式 `FracBits=0`——这正是这几个函数在 VHDL 里替你做的事。

#### 4.1.4 代码实践

**实践目标**：验证四个取整函数的舍入方向差异。

由于取整四件套只在 VHDL 提供，本实践为**源码阅读 + 手算验证型**：

1. 阅读 [vhdl/src/en_cl_fix_pkg.vhd:2130-2186](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2130-L2186)，确认四个函数分别使用 `SymZero_s` / `NonSymNeg_s` / `NonSymPos_s` / `SymInf_s`。
2. 取输入格式 `(true, 3, 2)`（有符号，整数位 3、小数位 2，步长 0.25），输入值 `a = -2.75`（即位串表示的 \(-2.75\)）。
3. 手算四种取整（结果格式 `(true, 3, 0)`）：
   - `fix`（朝零）→ \(-2\)
   - `floor`（朝下）→ \(-3\)
   - `ceil`（朝上）→ \(-2\)
   - `round`（远离零）→ \(-3\)
4. **预期结果**：`fix` 与 `ceil` 都得 \(-2\)；`floor` 与 `round` 都得 \(-3\)。可见"朝零类"（fix/ceil 对负数）与"朝下类"（floor/round 对负数）在负数处分组一致。
5. 若本地有 Modelsim，可在 testbench 中参照 `CheckInt` 风格新增断言验证；否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cl_fix_round` 用 `SymInf_s` 而不是更常用的 `NonSymPos_s`（`Round_s`）？

> **答案**：`SymInf_s` 是"对称到无穷"=半值远离零，正好匹配数学上"四舍五入"的日常语义（\(-2.5 \to -3\)）。而 `NonSymPos_s` 是"非对称朝正"，平局恒朝正方向（\(-2.5 \to -2\)），会引入直流偏差，不适合作为通用 `round` 的默认语义。

**练习 2**：四个函数的 `ResultFmt` 为什么把 `FracBits` 设成 0，而不是 1 或别的值？

> **答案**：取整的目标就是"没有小数位"，`FracBits=0` 表示结果只含整数（和符号）位。任何非 0 的小数位都会保留 fractional 信息，违背"取整"本意。

---

### 4.2 范围检查 cl_fix_in_range

#### 4.2.1 概念说明

`cl_fix_in_range(a, aFmt, resultFmt, round)` 回答一个问题：

> **把 `a`（格式 `aFmt`）按 `round` 模式量化、再放进 `resultFmt`，会不会发生饱和（越界）？**

返回 `boolean`（VHDL）或布尔数组（Python，向量化）。它不返回量化后的值，只返回"能不能不饱和地表示"。

典型用途：在调用一个会饱和的运算**之前**先检查，或在 testbench 中断言某次转换本不应饱和。

#### 4.2.2 核心流程

关键设计：**不能直接把 `a` resize 到 `resultFmt` 再看是否被 clip**——因为一旦 resize 时用了 `Sat_s`，越界值会被夹紧，你就无法再区分"原本就在范围内"和"被夹紧进来"。

正确做法是先 resize 到一个**足够宽、绝不会饱和**的中间格式 `rndFmt`，保住真实量化值，再拿这个真实值去比 `resultFmt` 的上下界：

```
rndFmt   = (aFmt.Signed, aFmt.IntBits + 1, resultFmt.FracBits)
valRnd   = cl_fix_resize(a, aFmt, rndFmt, round, Sat_s)   # 多 1 整数位,保住舍入进位
inRange  = (min_value(resultFmt) <= valRnd) and (valRnd <= max_value(resultFmt))
```

为什么 `IntBits + 1`？

- 量化（舍入）最坏会在整数部分产生 **1 位进位**（例如 \(15.5\) 按 `NonSymPos_s` 取整到 0 小数位得 \(16\)，需要比原 \(15\) 多 1 个整数位）。
- `rndFmt` 用 `aFmt.IntBits + 1`，正好容纳这个进位，保证 `valRnd` 是**真实量化结果**而非被夹紧的值。
- 然后 `resultFmt` 的范围由它自己的 `IntBits` 决定；`valRnd` 与 `resultFmt` 上下界的比较才是真正的"可表示性"判断。

注意 `rndFmt.FracBits = resultFmt.FracBits`：用结果的小数粒度去量化，但保留输入的整数范围（加进位位）。

#### 4.2.3 源码精读

**VHDL** 实现，注意第 2195 行的注释明确写 *"This matches the python implementation"*：

[vhdl/src/en_cl_fix_pkg.vhd:2190-2208](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2190-L2208) —— `rndFmt_c` 取 `IntBits => a_fmt.IntBits + 1`、`FracBits => result_fmt.FracBits`；用 `round` 与 `Sat_s` resize 得到 `Rounded_c`；最后**复用 `cl_fix_compare`** 做 `a>=b`（对 `min_value`）与 `a<=b`（对 `max_value`）两次比较，取逻辑与。

> 这里 VHDL 复用了本讲 4.3 的 `cl_fix_compare`，因为 `Rounded_c`（格式 `rndFmt_c`）与 `min_value/max_value`（格式 `result_fmt`）格式不同，必须对齐后才能比——这正是 `cl_fix_compare` 的用途。

**Python** 实现等价，但更直接：因为 Python 的 `cl_fix_min_value/max_value` 返回实数（见 u1-l3），可以直接用 `<` / `>` 比实数，**无需** `cl_fix_compare`：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:277-284](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L277-L284) —— `rndFmt = FixFormat(aFmt.Signed, aFmt.IntBits+1, rFmt.FracBits)`；`cl_fix_resize(..., Sat_s)` 得 `valRnd`；用 `np.where` 向量化地判断 `valRnd` 是否落在 `[min_value, max_value]` 内。

**MATLAB** 实现思路相同，但把"舍入加偏移"和"截断小数"显式展开（因为 MATLAB 的 `cl_fix_resize` 接受的是浮点 `a`）：

[matlab/src/cl_fix_in_range.m:36-57](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_in_range.m#L36-L57) —— 按七种 `round` 模式分别给 `a` 加偏移（与 u3-l2 讲的偏移积木一致），再用 `floor` 截断到 `result_fmt.FracBits` 粒度。

[matlab/src/cl_fix_in_range.m:60-67](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_in_range.m#L60-L67) —— 范围判定：有符号看 \(a \in [-2^I,\ 2^I)\)，无符号看 \(a \in [0,\ 2^I)\)。注意上界用 `>=` 排除（最大可表示值是 \(2^I - 2^{-F}\)，小于 \(2^I\)）。

> **跨语言一致性**：`cl_fix_in_range` 是本讲**唯一三个语言都实现**的函数，且 VHDL 源码注释明确声明与 Python 一致。三套实现的 `rndFmt = (aFmt.Signed, aFmt.IntBits+1, resultFmt.FracBits)` 公式逐字符对应——这是位真一致性的直接体现。

#### 4.2.4 代码实践

**实践目标**：用 `cl_fix_in_range` 检查 `(true,4,4)` 的若干值能否不饱和地表示到 `(true,3,2)`，并理解 `IntBits+1` 的作用。

在 `python/unittest` 目录运行（Python 端可用、可立即验证）：

1. 进入 `python/unittest` 目录，启动 `python3`。
2. 执行：
   ```python
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *
   import numpy as np
   aFmt = FixFormat(True,4,4)      # 范围 [-16, 16-2^-4]
   rFmt = FixFormat(True,3,2)      # 范围 [-8, 8-2^-2] = [-8, 7.75]
   # 待检查的实数值
   vals = np.array([3.0, 7.75, 8.0, -8.0, -8.25, 15.9])
   print(cl_fix_in_range(vals, aFmt, rFmt, FixRound.Trunc_s))
   ```
3. **需要观察的现象**：`3.0`、`7.75`、`-8.0` 为 `True`（在 `(true,3,2)` 范围内）；`8.0`、`-8.25`、`15.9` 为 `False`（越界）。
4. **预期结果**：输出 `[True True False True False False]`。其中 `7.75` 恰为 `(true,3,2)` 的最大值，应判 `True`；`8.0` 刚好越界，判 `False`。
5. **进阶**：把 `rFmt.FracBits` 改大（如 `(true,3,4)`）会让更多值因小数位充足而 `True`，但整数位仍是 3，所以 `8.0` 依旧 `False`——体会"整数位决定范围、小数位决定精度"。
6. **验证 `IntBits+1` 的必要性**：构造 `aFmt=(false,4,2)`、`a=15.5`、`resultFmt=(true,4,0)`、`round=NonSymPos_s`。手算：\(15.5\) 舍入到 0 小数位得 \(16\)，需要 5 个整数位才能放下这个进位；`rndFmt=(false,5,0)` 恰好放下 16，随后与 `(true,4,0)` 的上界 15 比较 → 越界 → `False`。这正是 testbench 里 "rounding OOR" 用例的场景，见 [vhdl/tb/en_cl_fix_pkg_tb.vhd:630-633](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L630-L633) 与 [python/unittest/en_cl_fix_pkg_test.py:661-662](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L661-L662)。

> 若运行环境无 Python/numpy，步骤 2–5 标注「待本地验证」；步骤 6 的手算推理可不依赖运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rndFmt` 用 `aFmt.IntBits + 1` 而不是 `resultFmt.IntBits`？

> **答案**：若用 `resultFmt.IntBits`（通常更小），resize 时一旦数值越界就会被 `Sat_s` 夹紧，丢失"原始值到底是多少"的信息，从而无法判断是否真的越界。用 `aFmt.IntBits + 1` 保证中间格式足够宽、不夹紧，保住真实量化值，再与 `resultFmt` 的边界比较，才能得到正确结论。`+1` 用于吸收舍入最坏情况的 1 位进位。

**练习 2**：`cl_fix_in_range` 内部那次 resize 为什么用 `Sat_s` 而不是 `None_s`？

> **答案**：理论上 `rndFmt` 足够宽（多了 1 个整数位）时不会真正饱和；用 `Sat_s` 是一道"防御性夹紧"。即便输入异常导致越界，`Sat_s` 也会把 `valRnd` 夹到 `rndFmt` 的边界，随后的范围比较仍会判定为越界（`False`），不会产生误导性的回绕值。这是一种保守、安全的选择。

---

### 4.3 比较 cl_fix_compare

#### 4.3.1 概念说明

`cl_fix_compare(comparison, a, aFmt, b, bFmt)` 比较两个**格式可以不同**的定点数，支持六种运算：

| `comparison` 字符串 | 语义 |
|---------------------|------|
| `"a=b"` / `"a!=b"` | 相等 / 不等 |
| `"a<b"` / `"a>b"` | 小于 / 大于 |
| `"a<=b"` / `"a>=b"` | 小于等于 / 大于等于 |

难点在于：两个数的 `Signed`、`IntBits`、`FracBits` 可能都不同，二进制位串无法直接比较。例如 `(false,4,2)` 的 `1.5`（位串 `01111000`... 实际宽度 6）和 `(false,2,1)` 的 `1.5`（位串 `011`）位串完全不同但数值相等。

#### 4.3.2 核心流程

`cl_fix_compare` 用两步解决"格式不同"的问题：

```
1. 构造统一对齐格式 FullFmt:
     Signed   = aFmt.Signed OR bFmt.Signed       # 只要有一个有符号,结果就有符号
     IntBits  = max(aFmt.IntBits, bFmt.IntBits) # 取较大整数范围
     FracBits = max(aFmt.FracBits, bFmt.FracBits)# 取较小步长(较多小数位)
2. 把 a、b 分别 resize 到 FullFmt(无损扩展,不丢精度)
3. 若 FullFmt 有符号: 翻转 a、b 的最高位(符号位)
     —— 这把二进制补码映射成"偏移二进制(unsigned+offset)",
        使无符号的大小比较等价于有符号的大小比较
4. 按 comparison 用 unsigned 比较 FullFmt 位串
```

**为什么翻转符号位等价于有符号比较？** 二进制补码下，负数的最高位是 1，正数是 0，导致负数的"无符号值"反而比正数大，直接 `unsigned` 比较会把所有负数判成"很大"。把最高位取反后：

\[
\text{offset}(x) = x + 2^{W-1} \pmod{2^W}
\]

即给所有数加上 \(2^{W-1}\) 的偏移，负数被搬到小值区、正数搬到大值区，**单调性保持**，于是无符号比较的结果与有符号比较完全一致。这是一个经典技巧（offset-binary / bias 比较），硬件上只需一个 NOT 门。

#### 4.3.3 源码精读

`cl_fix_compare` 是本讲中**仅 VHDL 提供**的函数（Python、MATLAB 均无；它们直接用浮点 `<` / `>` 比实数即可，不需要这个位级技巧）。

[vhdl/src/en_cl_fix_pkg.vhd:2619-2648](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2619-L2648) —— 完整实现：

- 第 2624 行：`FullFmt_c = (aFmt.Signed or bFmt.Signed, max(IntBits), max(FracBits))`。
- 第 2630–2631 行：把 `a`、`b` resize 到 `FullFmt_c`（用 `cl_fix_resize` 的默认 `Trunc_s` / `None_s`；由于 `FullFmt` 是两个格式的"并集"，resize 是无损扩展，不会真的舍入或回绕）。
- 第 2633–2636 行：若 `FullFmt_c.Signed`，翻转两者的最高位 `AFull_v(AFull_v'high) := not ...`。
- 第 2638–2646 行：按 `comparison` 字符串分派到六种 `unsigned` 比较；非法字符串触发 `###ERROR###`（参见 u2-l2 讲的 testbench 失败检测机制）。

`cl_fix_compare` 在本讲 4.2 的 `cl_fix_in_range` 中被复用（VHDL 端）来比对 `Rounded_c` 与 `result_fmt` 的上下界——正因为两边格式不同，才需要 `FullFmt` 对齐。

testbench 中的用例覆盖了"不同格式、有符号/无符号混搭"的场景：

[vhdl/tb/en_cl_fix_pkg_tb.vhd:643-664](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L643-L664) —— 例如 `(false,4,2)` 的 `1.25` 与 `(false,2,1)` 的 `1.5` 比 `a<b` 应为 `True`；以及有符号 vs 无符号的混搭用例。

#### 4.3.4 代码实践

**实践目标**：对两个**格式不同**的数做 `a<b` 比较，体会 `FullFmt` 对齐的必要性。

由于 `cl_fix_compare` 仅 VHDL 提供，本实践为**源码阅读 + 手算型**：

1. 取 `a = 1.25`（格式 `(false,4,2)`，无符号）、`b = 1.5`（格式 `(false,2,1)`，无符号）。
2. 手算 `FullFmt = (false or false, max(4,2), max(2,1)) = (false, 4, 2)`。
3. 把 `b` 从 `(false,2,1)` resize 到 `(false,4,2)`：左移补 1 个小数位，\(1.5 \to 1.5\)（无损），`a` 已在该格式。
4. `FullFmt` 无符号 → 不翻转符号位。比较 \(1.25 < 1.5\) → `True`。
5. **预期结果**：与 testbench 用例 [vhdl/tb/en_cl_fix_pkg_tb.vhd:645-649](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L645-L649) 一致，返回 `True`。
6. **进阶（理解符号位翻转）**：把 `a` 改成 `(true,4,2)` 的 `-1.0`、`b` 改成 `(false,2,1)` 的 `1.5`。则 `FullFmt = (true, 4, 2)` 有符号，需翻转最高位。手算补码：\(-1.0\) 在 `(true,4,2)` 的位串是 `11111100`（最高位 1），翻转后最高位变 0 → 变成"很小的无符号数"；`1.5` 最高位 0 → 翻转后变 1 → "较大的无符号数"。于是 `unsigned(-1.0翻转) < unsigned(1.5翻转)` → `True`，即 \(-1.0 < 1.5\)，正确。
7. 若本地有 Modelsim，可在 testbench 中新增 `CheckBoolean` 断言验证步骤 6；否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`FullFmt` 的三个字段为什么取 `or` / `max` / `max`？

> **答案**：`Signed` 取 `or` 是因为只要有一个数有符号，对齐格式就必须能表示负数；`IntBits` 取 `max` 保证整数范围覆盖两者；`FracBits` 取 `max` 保证步长（精度）达到两者中更细的那个。这样把任意一个数 resize 到 `FullFmt` 都是**无损扩展**，不丢信息。

**练习 2**：如果 `FullFmt` 是无符号的（两边都无符号），代码还会翻转最高位吗？

> **答案**：不会。第 2633 行的 `if FullFmt_c.Signed then` 只在有符号时翻转。无符号数的最高位就是数值位，直接 `unsigned` 比较即可，无需偏移。

**练习 3**：为什么 Python / MATLAB 不需要 `cl_fix_compare`？

> **答案**：Python 与 MATLAB 把定点数存为 `double` 实数，本就在实数域，直接用 `<`、`>`、`==` 比实数即可，格式差异已被"实数"这个统一表示吸收。只有 VHDL 把数存为 `std_logic_vector` 位串，才需要 `FullFmt` 对齐 + 符号位翻转这套位级技巧。这也是 `cl_fix_compare` 是 VHDL 独有的根本原因。

---

## 5. 综合实践

设计一个把本讲三个最小模块串起来的小任务：**"写一个不饱和的安全类型转换检查器"**。

场景：你有一组算法结果存在 `(true,4,4)` 格式里（范围约 \([-16, 16)\)），要送到一个下游模块，该模块只接受 `(true,3,2)`（范围 \([-8, 7.75]\)）。你希望在送出前挑出"能不饱和表示"的样本，并对其中两个样本比较大小做排序判断。

任务步骤：

1. **取整理解**：先在源码中确认 `cl_fix_round` 等价于 `cl_fix_resize(..., FracBits=0, SymInf_s, None_s)`（见 [vhdl/src/en_cl_fix_pkg.vhd:2175-2186](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2175-L2186)）。说明如果下游模块其实想要的是"整数结果"，你会用哪个取整函数、对应哪种 `FixRound`。
2. **范围筛选**（Python 可运行）：用 `cl_fix_in_range` 对 `(true,4,4)` 的一批值（如 `[-9, -8, -4, 0, 3.75, 7.75, 8, 12]`）筛选能不饱和表示到 `(true,3,2)` 的子集，记录哪些被排除、为什么。
3. **比较**（源码阅读）：任取两个格式不同的合格样本（如 `(true,4,4)` 的 `3.75` 与另一个 `(true,3,2)` 的值），手算 `cl_fix_compare` 的 `FullFmt` 对齐与符号位翻转过程，预测 `a<b` 结果。
4. **反思跨语言**：写出上述三步在 Python、VHDL、MATLAB 中分别能用哪些函数完成，指出哪些步骤在某语言中**没有直接对应的函数**（如 `cl_fix_compare` 在 Python/MATLAB 缺失），你会如何替代（答案：直接用浮点比较）。

**交付物**：一张表，列出每个样本"是否在范围内"，以及至少一对样本的 `FullFmt` 推导与比较结果。

## 6. 本讲小结

- `cl_fix_fix/floor/ceil/round` 四个取整函数是 `cl_fix_resize` 的薄包装：结果格式 `FracBits=0`，分别固定使用 `SymZero_s` / `NonSymNeg_s` / `NonSymPos_s` / `SymInf_s`，饱和用 `None_s`。
- `cl_fix_in_range` 通过"先 resize 到多 1 个整数位的中间格式 `rndFmt`（用 `Sat_s` 保住真实量化值）再比 `resultFmt` 上下界"来判断可表示性；`IntBits+1` 用于吸收舍入进位。
- `cl_fix_compare` 用统一 `FullFmt = (a.Signed OR b.Signed, max IntBits, max FracBits)` 把两个不同格式的数无损对齐，再翻转符号位（补码→偏移二进制）把有符号比较转为无符号比较。
- **关键跨语言差异**：只有 `cl_fix_in_range` 三语言齐全；`cl_fix_fix` 仅 VHDL+MATLAB（且 MATLAB 头注释过时）；`cl_fix_floor/ceil/round` 与 `cl_fix_compare` 仅 VHDL 提供。Python/MATLAB 靠浮点直接比较与手动 `cl_fix_resize` 替代缺失函数。
- VHDL 的 `cl_fix_in_range` 复用了 `cl_fix_compare` 来比对格式不同的 `Rounded_c` 与边界值，体现了"辅助函数互相组合"的设计。
- 这些函数再次印证了 u3-l3 的统一架构：**所有运算最终都汇聚到 `cl_fix_resize`**。

## 7. 下一步学习建议

- 本讲结束后，Unit 3（核心转换与 resize 管线）已完整。建议进入 **Unit 4（定点运算与位操作）**，学习 `cl_fix_add/sub/mult/shift` 等如何用 `ForAdd/ForSub/ForMult` 构造中间格式并最终落到 `cl_fix_resize`——你会再次看到本讲的"中间格式"思想。
- 想深入 `cl_fix_compare` 的符号位翻转技巧与 `cl_fix_mean_angle` 的角度模运算，可提前跳读 **u7-l2（compare 与 mean_angle）**，那是专家层对这两个函数的完整剖析。
- 若对"辅助函数复用底层原语"的模式感兴趣，可对比阅读 [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) 中 `cl_fix_abs/neg/mean` 等函数，它们同样以 `cl_fix_resize` 收尾。
