# 舍入模式 FixRound

## 1. 本讲目标

本讲聚焦 en_cl_fix 里**与数值无关、只定义“怎么舍”**的一组枚举：舍入模式 `FixRound`。

学完本讲你应当能够：

1. 说清楚定点运算**为什么**必须要舍入，以及“舍入”与上一讲的“格式 `[S,I,F]`”是什么关系。
2. 准确说出七种舍入模式 `Trunc_s / NonSymPos_s / NonSymNeg_s / SymInf_s / SymZero_s / ConvEven_s / ConvOdd_s` 各自的语义，并能按“截断 / 非对称 / 对称 / 收敛”四大类归类。
3. 看懂 README 与 VHDL 包头里的舍入真值表，对任意一个具体数值（例如 −0.5）预测它在 `(true,2,0)` 下被每种模式舍入后的结果。
4. 知道 `Round_s` 只是 `NonSymPos_s` 的别名，并理解“为什么官方推荐尽量用 `Trunc_s`，其次用 `NonSymPos_s`”这一资源建议背后的硬件直觉。
5. 认出这七种模式在 VHDL、Python、MATLAB 三套实现里**用完全相同的整数编码 0–6**，这是 u1-l1 所述“位真一致性”的一个具体体现。

> 本讲只讲舍入模式的**定义与语义**。这些模式“在代码里到底是怎么被实现出来的”（加偏移、再截断）是 `cl_fix_resize` 的事，留到 **u3-l2** 精读。本讲先建立直觉。

## 2. 前置知识

本讲假设你已经学过 **u1-l2（定点格式 `[S,I,F]`）**。需要回顾的关键点：

- 一个定点格式 `(Signed, IntBits, FracBits)` 唯一决定了二进制小数点的位置，也就决定了**哪些实数能被精确表示**。
- 只有当一个小数的分母是 2 的幂时，它才能被二进制定点数精确表示。例如 \(0.5 = 2^{-1}\)、\(0.25 = 2^{-2}\) 可以精确表示；而 \(0.1\)、\(0.2\) 不能。
- 把一个实数装进某个格式，本质上是把它映射到该格式**能表示的离散网格点**上。网格的间距就是 \(2^{-F}\)（F 是小数位数）。

由此自然引出本讲的核心问题：**当运算结果落在了两个网格点之间，该往哪边靠？** 这个“往哪边靠”的决定规则，就是**舍入模式（rounding mode）**。

举个直观例子：把实数 \(2.7\) 放进 `(true,2,0)`（整数、无小数位，可表示值为 \(\ldots,1,2,3,\ldots\)）。\(2.7\) 介于 2 和 3 之间，离 3 更近，所有“会舍入”的模式都把它变成 3；唯独 `Trunc_s` 直接砍掉小数部分，得到 2。差别就在“砍”还是“靠”。

> 术语提示：
> - **舍入（rounding）**：决定往最近网格点靠的规则，本讲主题。
> - **饱和（saturation）**：当结果超出格式能表示的范围时，是“夹紧到最大/最小”还是“回绕”，这是下一讲 **u1-l5** 的主题。
> - 两者是正交的：一个负责“小数位怎么处理”，一个负责“溢出怎么处理”。本讲只谈前者。

## 3. 本讲源码地图

本讲涉及的源码文件很少，且都是**定义/声明**，没有复杂逻辑：

| 文件 | 在本讲中的作用 |
|------|----------------|
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | VHDL 端：`FixRound_t` 枚举定义、`Round_s` 别名、包头里的舍入真值表注释 |
| [python/src/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py) | Python 端：`FixRound` 枚举（`Enum`），整数编码 0–6 |
| [matlab/src/cl_fix_constants.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m) | MATLAB 端：`Round.*` 结构体常量，必须在使用前先执行 |
| [README.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md) | 项目级舍入说明与真值表（与 VHDL 包头注释逐字对应） |

辅助引用（用于本讲的代码实践，实现细节在后续讲义）：

| 文件 | 作用 |
|------|------|
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | `cl_fix_resize` / `cl_fix_from_real` / `cl_fix_to_real`，用于在本讲用 Python 直观验证七种模式 |

## 4. 核心概念与源码讲解

### 4.1 为什么定点运算需要舍入

#### 4.1.1 概念说明

定点数的精度是**有限且离散**的：一个格式 `(S,I,F)` 只能表示间距为 \(2^{-F}\) 的网格点。但凡发生下面任何一种情况，结果就可能不落在网格点上，必须做一次“靠到网格点”的操作（也就是舍入）：

1. **小数位变少**：从一个 `F` 较大的格式转到 `F` 较小的格式（典型如把 `(true,2,8)` 转到 `(true,2,0)`），多出来的小数位无处安放。
2. **实数装入**：把一个物理量（浮点实数）写进定点格式，例如 `cl_fix_from_real(2.7, (true,2,0))`。
3. **运算后再量化**：乘法会让 `F` 翻倍（见 u4-l2 的 `ForMult`），最终结果通常要再缩回目标精度。

舍入模式回答的就是这一次“靠网格”的**tie-breaking（平局裁决）规则**：当输入恰好在两个网格点的正中间（即小数部分正好是 \(0.5\) 个 LSB）时，往左还是往右？

> 一句话区分：**格式 `[S,I,F]` 决定“有哪些网格点”，舍入模式 `FixRound` 决定“怎么靠过去”。** 上一讲定义了网格，本讲定义靠法。

#### 4.1.2 核心流程

从概念上，任何一次舍入都可以拆成三步（注意：这只是**语义模型**，不是硬件实现；真正的位级实现见 u3-l2）：

```text
输入值 x，目标格式的网格间距 Δ = 2^(-F_dst)

1. 找到包围 x 的两个最近网格点：
      V_lo = floor(x / Δ) * Δ      （偏小的那个）
      V_hi = V_lo + Δ               （偏大的那个）

2. 判断 x 的位置：
      若 x < V_lo + Δ/2  → 离 V_lo 近
      若 x > V_lo + Δ/2  → 离 V_hi 近
      若 x == V_lo + Δ/2 → 正好在中点（.5 平局）

3. 选择：
      Trunc_s（唯一例外）：永远取 V_lo，不看距离。
      其它六种模式：
         - 非平局情况：一律取更近的那个（行为一致）。
         - 平局（.5）情况：各按自己的 tie-breaking 规则裁决。
```

关键结论：

- **非平局时，除 `Trunc_s` 外的六种模式行为完全一致**——都取最近邻。它们的差别**只出现在正好落在 .5 中点的那些值上**。
- `Trunc_s` 是唯一的“不四舍五入”模式：它永远向下（向 \(-\infty\)）取整，等价于直接丢弃低位 bit。这也是它最省硬件的原因。

#### 4.1.3 源码精读

七种模式在 VHDL 里被显式列为一个枚举类型 `FixRound_t`。紧挨着枚举的，是包头里一段**与 README 完全对应**的真值表注释：

[vhdl/src/en_cl_fix_pkg.vhd:163-175](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L163-L175) — 这是 VHDL 包头里对舍入模式的 Doxygen 文档注释，列出七种模式在 `(true,2,0)` 下对六个示例值的舍入结果，并以 `--! \note` 给出资源建议。这段注释就是 README 舍入表的源头。

注意第 175 行那句资源提示，它将贯穿整讲：

```vhdl
--! \note	Use Trunc_S or NonSymPos_s for wherever possible for lowest resource usage
```

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认“七种模式”这一说法不是凭空说的，而是源码里实实在在的七个枚举值。

**步骤**：

1. 打开 [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd)，定位第 176 行起的 `type FixRound_t is (...)`。
2. 数一下括号里有几个值，核对是否正好是：`Trunc_s, NonSymPos_s, NonSymNeg_s, SymInf_s, SymZero_s, ConvEven_s, ConvOdd_s`。
3. 再打开 [README.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md) 的 Rounding 小节（第 70 行起），核对 README 表格里也是这七行。

**预期结果**：VHDL 枚举、VHDL 包头注释表、README 表格，三处的七种模式**名称与顺序完全一致**。这正是“三套实现共享同一套语义”的第一个证据。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 \(0.1\) 装进 `(true,2,0)` 一定会触发舍入，而把 \(0.5\) 装进 `(true,2,1)` 却可以不触发？

**参考答案**：`(true,2,0)` 没有小数位，网格间距 \(\Delta=1\)，\(0.1\) 不是网格点，必须舍入到 0；而 `(true,2,1)` 的小数位 \(F=1\)，网格间距 \(\Delta=0.5\)，\(0.5\) 正好是网格点，无需舍入即可精确表示。根本原因是 \(0.5=2^{-1}\) 可被二进制精确表示，而 \(0.1\) 不能。

**练习 2**：如果一次转换中输入值**不落在** .5 中点上，那么 `NonSymPos_s`、`SymInf_s`、`ConvEven_s` 三者的结果会不会不同？

**参考答案**：不会。非平局时这三种模式都取“最近邻”，结果一致。它们（以及除 `Trunc_s` 外的所有模式）的差别**只出现在正好 .5 的平局值上**。

---

### 4.2 七种舍入模式的语义与 README 真值表

这是本讲的核心模块。我们把七种模式按**四类**来讲，每一类抓住一条 tie-breaking 主线，再用 README 的真值表验证。

#### 4.2.1 概念说明

下表是 README 给出的、把六个示例值舍入到 `(true,2,0)` 的完整结果（来源：[README.md:82-117](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L82-L117)，与 VHDL 包头注释逐字一致）：

| 模式 | 类别 | 2.2 | 2.7 | −1.5 | −0.5 | 0.5 | 1.5 | 平局(.5)裁决规则 |
|------|------|-----|-----|------|------|-----|-----|------------------|
| `Trunc_s`     | 截断     | 2 | 2 | −2 | −1 | 0 | 1 | 不裁决，永远向 \(-\infty\)（floor） |
| `NonSymPos_s` | 非对称   | 2 | 3 | −1 | 0  | 1 | 2 | .5 朝 \(+\infty\)（round half up） |
| `NonSymNeg_s` | 非对称   | 2 | 3 | −2 | −1 | 0 | 1 | .5 朝 \(-\infty\)（round half down） |
| `SymInf_s`    | 对称     | 2 | 3 | −2 | −1 | 1 | 2 | .5 远离 0（away from zero） |
| `SymZero_s`   | 对称     | 2 | 3 | −1 | 0  | 0 | 1 | .5 朝向 0（toward zero） |
| `ConvEven_s`  | 收敛     | 2 | 3 | −2 | 0  | 0 | 2 | .5 朝最近**偶数** |
| `ConvOdd_s`   | 收敛     | 2 | 3 | −1 | −1 | 1 | 1 | .5 朝最近**奇数** |

读法提示：

- **非平局的两列 `2.2` 和 `2.7`**：除 `Trunc_s` 外，所有模式都给同样的答案（2.2→2，2.7→3），印证了“非平局行为一致”。
- **平局的四列 `−1.5 / −0.5 / 0.5 / 1.5`**：这才是七种模式真正分化的地方，也是本模块要重点理解的。

#### 4.2.2 核心流程：四类模式怎么裁决平局

把七种模式按 tie-breaking 主线分成四类，就不再需要死记：

**第 1 类 · 截断 `Trunc_s`（1 种）**
- 不做任何“四舍五入”，直接丢弃超出目标精度的小数位，等价于向 \(-\infty\) 取整（floor）。
- 硬件成本最低（什么都不加），但会引入向负方向的系统偏差。

**第 2 类 · 非对称 `NonSymPos_s / NonSymNeg_s`（2 种）**
- 平局时**永远朝同一方向**，与正负号无关。
- `NonSymPos_s`：平局朝 \(+\infty\)。即 \(+0.5\to +1\)，\(-0.5\to 0\)（负数也“向上”=朝 0）。
- `NonSymNeg_s`：平局朝 \(-\infty\)。即 \(+0.5\to 0\)，\(-0.5\to -1\)。
- “非对称”的含义：正数和负数在平局处的处理**不是镜像关系**，因而对零均值信号会引入一个 \(\tfrac{1}{2}\) LSB 的直流（DC）偏差。`NonSymPos_s` 的硬件实现极其简单（给被截掉的位段加一个 1 再截断），所以它是**最常用的舍入模式**，并被别名为 `Round_s`。

**第 3 类 · 对称 `SymInf_s / SymZero_s`（2 种）**
- 平局时按**符号对称**处理：正负数在平局处的舍入幅度相等、方向关于 0 对称，因而**不引入直流偏差**。
- `SymInf_s`（round half away from zero）：\(+0.5\to+1\)，\(-0.5\to-1\)（都远离 0）。
- `SymZero_s`（round half toward zero）：\(+0.5\to0\)，\(-0.5\to0\)（都朝向 0）。

**第 4 类 · 收敛 `ConvEven_s / ConvOdd_s`（2 种）**
- 平局时朝**最近的一个偶数/奇数**靠，这是统计学里著名的“银行家舍入（banker's rounding）”。
- `ConvEven_s`：\(+0.5\to0\)（0 是偶）、\(1.5\to2\)（2 是偶）、\(-0.5\to0\)、\(-1.5\to-2\)。
- `ConvOdd_s`：\(+0.5\to1\)（1 是奇）、\(1.5\to1\)、\(-0.5\to-1\)、\(-1.5\to-1\)。
- 收敛舍入在大量等概率平局出现时，能让约一半朝上、一半朝下，统计误差最小；代价是硬件最复杂（要看次低位的奇偶性）。

#### 4.2.3 源码精读：什么是“1 LSB 的对称性差异”

回到本讲实践任务要标注的“**哪些模式会引入 1 LSB 的对称性差异**”。

在网格上，两个相邻可表示整数相差正好 **1 LSB**。所以任意两种 tie-breaking 规则，在某个平局值上的结果**最多相差 1 LSB**。具体地：

- 对比 `NonSymPos_s` 与 `SymInf_s`：在 \(+0.5\) 处都给 \(+1\)（相同）；但在 \(-0.5\) 处，前者给 \(0\)、后者给 \(-1\)——**正好差 1 LSB**。
- 对比 `SymInf_s` 与 `SymZero_s`：在 \(+0.5\) 处，\(1\) 与 \(0\) 差 1 LSB；在 \(-0.5\) 处，\(-1\) 与 \(0\) 差 1 LSB。
- 对比 `ConvEven_s` 与 `ConvOdd_s`：在**每一个**平局点，一个朝偶、一个朝奇，而偶奇相邻——**处处差 1 LSB**。

由此可总结一句判别准则：

> **`Trunc_s` 是唯一不参与“1 LSB 之争”的模式**（它从不裁决）。其余六种模式在平局处两两之间最多差 1 LSB；“对称类（Sym/Conv）”保证正负镜像、无直流偏差，“非对称类（NonSym）”则不保证。

这些差异的源头定义就在 VHDL 枚举里：

[vhdl/src/en_cl_fix_pkg.vhd:176-185](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L176-L185) — `type FixRound_t is (Trunc_s, NonSymPos_s, NonSymNeg_s, SymInf_s, SymZero_s, ConvEven_s, ConvOdd_s)`，每个值后面跟一句简短注释说明该模式的舍入方向。本讲的所有语义都来自这七行。

#### 4.2.4 代码实践（可运行 · Python 验证真值表）

**目标**：用 Python 实际跑一遍，亲眼看到七种模式对六个示例值产生 README 表格里的结果，并定位平局处的 1 LSB 差异。

**思路**：把每个示例值先装入一个高精度源格式 `(true,2,8)`（小数位足够多，能分辨 \(0.5\) 边界），再用 `cl_fix_resize` 缩到 `(true,2,0)`，每次换一种 `FixRound` 模式，最后用 `cl_fix_to_real` 读回实数。

> 说明：`cl_fix_resize` 的内部实现（加偏移再截断）属于 u3-l2。本实践只是把它当作“黑盒舍入器”来观察七种模式的**输出差异**。

**操作步骤**：在仓库根目录新建一个临时脚本（不要提交，用完即删），内容如下：

```python
# 示例代码：验证 README 舍入真值表
import sys
sys.path.insert(0, "python/src")
from en_cl_fix_pkg import (FixFormat, FixRound,
                           cl_fix_from_real, cl_fix_resize, cl_fix_to_real)

src = FixFormat(True, 2, 8)   # 高精度源格式：能分辨 0.5 边界
dst = FixFormat(True, 2, 0)   # 目标格式：整数，无小数位

values = [2.2, 2.7, -1.5, -0.5, 0.5, 1.5]
modes  = [FixRound.Trunc_s,  FixRound.NonSymPos_s, FixRound.NonSymNeg_s,
          FixRound.SymInf_s, FixRound.SymZero_s,
          FixRound.ConvEven_s, FixRound.ConvOdd_s]

for v in values:
    a = cl_fix_from_real(v, src)                                  # 装入高精度格式
    out = [cl_fix_to_real(cl_fix_resize(a, src, dst, m), dst)
           for m in modes]
    print(f"{v:>5} -> ", out)
```

`cl_fix_resize` 的签名确认它接受一个 `rnd : FixRound` 参数（默认 `Trunc_s`）：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:190-193](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L190-L193) — `def cl_fix_resize(a, aFmt, rFmt, rnd=FixRound.Trunc_s, sat=FixSaturate.None_s)`，可见舍入模式 `rnd` 与饱和 `sat` 是两个独立的正交参数，本讲只关心 `rnd`。

**需要观察的现象**：

1. 第一列 `2.2` 和第二列 `2.7`：除 `Trunc_s` 外，六种模式结果完全相同（印证“非平局一致”）。
2. 后四列（平局值）：七种模式各不相同，且任意两行在平局处最多差 1。

**预期结果**（与 [README.md:82-117](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L82-L117) 的表格逐项一致）：

```text
  2.2 ->  [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
  2.7 ->  [2.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0]
 -1.5 ->  [-2.0, -1.0, -2.0, -2.0, -1.0, -2.0, -1.0]
 -0.5 ->  [-1.0, 0.0, -1.0, -1.0, 0.0, 0.0, -1.0]
  0.5 ->  [0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0]
  1.5 ->  [1.0, 2.0, 1.0, 2.0, 1.0, 2.0, 1.0]
```

（每行依次对应 `Trunc / NonSymPos / NonSymNeg / SymInf / SymZero / ConvEven / ConvOdd`。）

> 关于 `2.2 / 2.7` 的微小精度说明：这两个值无法被二进制精确表示，`(true,2,8)` 下会有极小误差（如 \(2.2 \approx 2.1992\)），但该误差远小于 \(0.5\)，不影响舍入结论，故仍与 README 一致。若要完全避免表示误差，可只用四个 `.5` 平局值来观察模式差异。

若你的环境未装 `numpy`，请先 `pip install numpy`（Python 实现依赖它，见 README Dependencies）。若运行结果与上表不符，请核对 `sys.path` 是否指向正确的 `python/src`。**实际数值以你本地运行为准（待本地验证）。**

#### 4.2.5 小练习与答案

**练习 1**：仅看 \(-0.5\) 这一列 `[−1, 0, −1, −1, 0, 0, −1]`，找出哪两种模式在这里“与众不同”，并解释为什么。

**参考答案**：`NonSymPos_s` 和 `SymZero_s` 给 0，其余给 −1。
- `NonSymPos_s`（平局朝 \(+\infty\)）：\(-0.5\) 向上靠到 0。
- `SymZero_s`（平局朝 0）：\(-0.5\) 朝 0 靠到 0。
- 其余要么朝 \(-\infty\)（`Trunc/NonSymNeg`）、要么远离 0（`SymInf` 给 −1）、要么朝奇（`ConvOdd` 给 −1）；`ConvEven` 给 0 其实也“朝 0”，但它的理由是 0 为偶数。区分点在于**动机**：`SymZero` 靠 0 是因为方向规则，`ConvEven` 靠 0 是因为偶数规则——换一个值（如 \(1.5\)）两者就会分道扬镳（`SymZero`→1，`ConvEven`→2）。

**练习 2**：把 \(+0.5\) 和 \(-0.5\) 的舍入结果配对看，哪几种模式是“正负对称”的？

**参考答案**：对称意味着 \(|round(+0.5)| = |round(-0.5)|\)。
- `Trunc_s`：0 与 −1，不对称。
- `NonSymPos_s`：1 与 0，不对称。
- `NonSymNeg_s`：0 与 −1，不对称。
- `SymInf_s`：1 与 −1，**对称**（都远离 0）。
- `SymZero_s`：0 与 0，**对称**（都朝 0）。
- `ConvEven_s`：0 与 0，**对称**（0 是偶）。
- `ConvOdd_s`：1 与 −1，**对称**（1 是奇）。
故对称类 = `SymInf / SymZero / ConvEven / ConvOdd`，正是前文分类里的“对称”与“收敛”四者；非对称类 = `Trunc / NonSymPos / NonSymNeg`。

---

### 4.3 三种语言中的 FixRound 定义、Round_s 别名与资源建议

#### 4.3.1 概念说明

u1-l1 强调三套实现“位真一致”。对舍入模式而言，这种一致性做得非常彻底：**三种语言不仅语义相同，连底层整数编码都完全一样（0–6，顺序一致）**。这意味着同一段定点算法在 VHDL、Python、MATLAB 里选用同一个数值的舍入模式，行为可逐位比对。

此外，VHDL 还专门为“最常用的舍入模式”提供了一个**别名 `Round_s`**，让你写代码时不必每次都拼 `NonSymPos_s`。

#### 4.3.2 核心流程：三语言如何“声明”一个舍入模式

三种语言的语法外壳不同，但本质都是给七个常量各分配一个 0–6 的整数：

```text
VHDL:    type FixRound_t is (Trunc_s, NonSymPos_s, ...);  -- 枚举类型，强类型
         constant Round_s : FixRound_t := NonSymPos_s;     -- 别名

Python:  class FixRound(Enum):                             -- 枚举类
             Trunc_s = 0; NonSymPos_s = 1; ...

MATLAB:  Round.Trunc_s = 0; Round.NonSymPos_s = 1; ...     -- 结构体字段
         （必须先运行 cl_fix_constants 才会创建）
```

调用时：

- VHDL：`cl_fix_resize(a, aFmt, rFmt, NonSymPos_s)` 或 `cl_fix_resize(a, aFmt, rFmt, Round_s)`。
- Python：`cl_fix_resize(a, aFmt, rFmt, FixRound.NonSymPos_s)`。
- MATLAB：先 `cl_fix_constants;` 再 `cl_fix_resize(a, aFmt, rFmt, Round.NonSymPos_s)`。

#### 4.3.3 源码精读

**VHDL —— 枚举类型与 `Round_s` 别名**

[vhdl/src/en_cl_fix_pkg.vhd:176-187](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L176-L187) — 定义 `FixRound_t` 枚举，并紧跟一句：

```vhdl
--! \brief Alias for most common rounding mode
constant Round_s	: FixRound_t	:= NonSymPos_s;
```

即 `Round_s` 在 VHDL 里就是一个指向 `NonSymPos_s` 的常量。之后任何函数的 `round` 参数都可以写 `Round_s`，效果与 `NonSymPos_s` 完全相同。

**Python —— `FixRound` 枚举**

[python/src/en_cl_fix_pkg/en_cl_fix_types.py:58-65](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L58-L65) — 用标准库 `Enum` 定义，整数值 0–6 与 VHDL 枚举的 position 一一对应：

```python
class FixRound(Enum):
    Trunc_s = 0
    NonSymPos_s = 1
    NonSymNeg_s = 2
    SymInf_s = 3
    SymZero_s = 4
    ConvEven_s = 5
    ConvOdd_s = 6
```

注意：Python **没有**提供 `Round_s` 别名（VHDL 独有），所以在 Python 里你得写完整的 `FixRound.NonSymPos_s`。这是一个细微的跨语言差异。

**MATLAB —— `Round.*` 结构体常量**

[matlab/src/cl_fix_constants.m:15-22](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L15-L22) — MATLAB 没有枚举类型，故用一个结构体 `Round` 的字段来当常量，值同样是 0–6：

```matlab
Round.Trunc_s    = 0;
Round.NonSymPos_s = 1;
...
Round.ConvOdd_s  = 6;
```

文件开头的注释（[cl_fix_constants.m:6-7](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L6-L7)）特别提醒：**这个脚本必须在使用任何 `cl_fix_...` 函数之前先执行一次**，否则 `Round` / `Sat` 结构体不存在，调用会报错。这是 MATLAB 端独有的“初始化约定”（同文件第 9–13 行还定义了 `Sat.*` 饱和常量，下一讲 u1-l5 详述）。

**资源使用建议（贯穿三处的同一句话）**

无论是 [README.md:121](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L121) 还是 [vhdl/src/en_cl_fix_pkg.vhd:175](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L175)，都给出同一条建议：

> Use `Trunc_s` wherever possible for lowest resource usage. If rounding is required, prefer `NonSymPos_s`.

直觉解释（呼应 4.2.2 的硬件实现）：

- `Trunc_s`：什么都不加，直接砍低位 → 几乎零成本，但精度最差、有负向偏差。
- `NonSymPos_s`：只需在被截掉的位段最高位加一个 1（半加），再截断 → 一个加法器/进位链，成本很低，是“要舍入”时的首选。
- 对称类（`SymInf/SymZero`）与收敛类（`ConvEven/ConvOdd`）：需要根据符号或次低位的奇偶性修正偏移 → 额外逻辑，成本最高，只在确实需要消除直流偏差或统计无偏时才用。

#### 4.3.4 代码实践（源码阅读型）

**目标**：亲手验证“三语言整数编码一致”以及“`Round_s` 是别名”。

**步骤**：

1. 在 Python 里打印每个模式的整数值：

   ```python
   # 示例代码
   from en_cl_fix_pkg.en_cl_fix_types import FixRound
   for m in FixRound:
       print(m.name, "=", m.value)
   ```

2. 对照 [matlab/src/cl_fix_constants.m:16-22](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L16-L22) 里 `Round.Xxx_s = N` 的赋值，核对同名常量的 `N` 是否与 Python 的 `m.value` 完全相同。
3. 在 [vhdl/src/en_cl_fix_pkg.vhd:187](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L187) 确认 `Round_s := NonSymPos_s`，再在包头里任意搜一个函数原型（如第 554 行附近的 `cl_fix_resize` 声明），看它的默认值是不是 `Trunc_s`。

**预期结果**：Python 与 MATLAB 的同名常量值逐对相等（0–6）；VHDL 的 `Round_s` 确为 `NonSymPos_s` 的别名，而绝大多数运算函数的 `round` 参数默认值是 `Trunc_s`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MATLAB 必须先执行 `cl_fix_constants`，而 VHDL 和 Python 不需要？

**参考答案**：VHDL 的枚举类型 `FixRound_t` 和 Python 的 `Enum` 类都是在“类型定义”阶段就把名字与值固定下来，随包/模块加载即可见。MATLAB 没有枚举类型，它是用脚本运行时给结构体 `Round` 赋字段的方式来“模拟常量”，这些字段只有在脚本真正执行后才存在于工作区，所以必须先运行一次 `cl_fix_constants`。

**练习 2**：如果某段算法对直流偏差敏感（例如零均值信号的长期累积），在七种模式里应优先排除哪几种？

**参考答案**：应排除三个**非对称**模式 `Trunc_s / NonSymPos_s / NonSymNeg_s`——它们在平局处对正负数处理不一致，会引入系统性的 \(\tfrac{1}{2}\) LSB 直流偏差。应从对称类 `SymInf_s / SymZero_s` 或收敛类 `ConvEven_s / ConvOdd_s` 中选择；其中收敛类在统计意义下无偏性最好，但硬件成本最高。

---

## 5. 综合实践

把本讲知识串起来完成下面这个“手算 + 机验”任务（对应本讲指定的实践任务）。

**任务**：对数值 `2.2 / 2.7 / -1.5 / -0.5 / 0.5 / 1.5`，分别写出七种舍入模式在 `(true,2,0)` 下的舍入结果，与 [README.md:82-117](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L82-L117) 的表格逐项对照，并标注哪些模式会引入 1 LSB 的对称性差异。

**操作步骤**：

1. **手算填表**：先用 4.2.2 的“四类 tie-breaking”规则，不查表，独立推断这 \(6 \times 7 = 42\) 个结果。提示：先把 `2.2 / 2.7` 两列填成“除 Trunc 外全相同”，再把四个 `.5` 平局值按类别填出。
2. **对照 README**：把你填的表与 README 真值表逐格比对，统计有几个格子不一致（理想为 0）。
3. **机验**：运行 4.2.4 的 Python 脚本，把输出再和你的手算表比对一次，确认三方（手算 / README / Python）完全一致。
4. **标注 1 LSB 差异**：在平局四列里，把“任意两模式结果不同”的格子圈出来，确认它们相差都恰好是 1；并按 4.2.5 练习 2 的方法，标出哪几种模式“正负对称”。

**需要观察的现象**：

- `2.2 / 2.7` 两列里，`Trunc_s` 与其它六种仅在“非平局但需要进位”时不同（`2.7`：Trunc=2，其余=3）。
- 四个 `.5` 列才是模式分化的主战场，且任意两行最多差 1 LSB。
- 对称类（`SymInf/SymZero/ConvEven/ConvOdd`）的 \(+0.5\) 与 \(-0.5\) 结果互为镜像；非对称类不是。

**预期结果**：得到一张与 README 完全一致的 7×6 表，并能指出“1 LSB 对称性差异仅出现在除 `Trunc_s` 外的六种模式之间、且仅在平局值上”。

> 如果手算与机验出现不一致，优先怀疑自己对 `ConvEven_s / ConvOdd_s` 的奇偶判断（注意 0 是偶数、负数的奇偶看绝对值），并用 4.2.4 脚本逐值核对。

## 6. 本讲小结

- 舍入模式 `FixRound` 回答的是“当运算结果落在两个可表示网格点之间时往哪边靠”，它与格式 `[S,I,F]`（定义网格）和饱和 `FixSaturate`（处理溢出，下一讲）是**三个正交**的概念。
- 七种模式可分为四类：**截断** `Trunc_s`、**非对称** `NonSymPos_s / NonSymNeg_s`、**对称** `SymInf_s / SymZero_s`、**收敛** `ConvEven_s / ConvOdd_s`。非平局时除 `Trunc_s` 外行为一致，差别只出现在 .5 平局处。
- 平局处任意两种模式最多相差 **1 LSB**；对称类与收敛类对正负数镜像处理、不引入直流偏差，非对称类则相反。
- `Round_s` 只是 `NonSymPos_s` 的别名（VHDL 独有），是工程上最常用的舍入模式。
- 资源建议：**能用 `Trunc_s` 就用，需要舍入时优先 `NonSymPos_s`**——因为 `Trunc_s` 零成本、`NonSymPos_s` 只需半加，其余模式需要符号/奇偶修正逻辑。
- 三种语言对七个模式使用了**完全相同的整数编码 0–6**，是 en_cl_fix 位真一致性的一个直接证据；MATLAB 端须先运行 `cl_fix_constants` 建立 `Round.*` 常量。

## 7. 下一步学习建议

- 本讲只讲了舍入模式的**定义与语义**，还没讲它们“在位级上是怎么被实现出来的”。接下来请学 **u1-l5（饱和模式 FixSaturate）**，把另一个正交维度（溢出处理）补齐。
- 补齐饱和后，**u3-l2（cl_fix_resize 的舍入机制）** 会回到本讲这些模式，剖析 `DropFracBits / NeedRound / HalfMinusDelta` 等常量是如何把七种模式统一成“加一个偏移再截断”的——届时你会彻底理解为什么 `NonSymPos_s` 最省硬件。
- 在进入 u3-l2 之前，建议先用本讲 4.2.4 的脚本多跑几组值（例如改 `(true,2,0)` 为 `(true,3,-1)` 看负小数位的效果），建立对“网格间距 \(\Delta = 2^{-F}\)”的直觉。
