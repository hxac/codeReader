# 饱和模式 FixSaturate

## 1. 本讲目标

定点格式 `[S,I,F]`（见 u1-l2）只能表示一组间距为 \(2^{-F}\) 的离散网格点，因而有一个有限的数值范围。当一个运算的**真实结果**落在这组网格所能表示的范围**之外**时，库必须决定：是把这个超范围的值「夹紧」到最近的边界（最大值或最小值），还是任由它的高位「溢出回绕」？以及，要不要顺便提醒调用者「刚才溢出了」？

这两个决定正是由本讲的主题——**饱和模式 `FixSaturate`**——来表达的。

学完本讲后，读者应当能够：

- 说清 `None_s` / `Warn_s` / `Sat_s` / `SatWarn_s` 四种模式各自的「是否饱和」「是否告警」组合。
- 区分**饱和（clip，夹紧到边界）**与**回绕（wrap，取模溢出）**在位级上的本质不同，并能用取模公式推算回绕后的值。
- 在 VHDL、Python、MATLAB 三种语言中分别定位饱和模式的定义，并理解三套实现共享同一套整数编码（位真一致性的前提）。
- 读懂 `cl_fix_from_real` 与 `cl_fix_resize` 中的饱和/告警分支，理解 Python 用 `warnings.warn`、VHDL 用 `assert ... severity warning` 实现告警。
- 理解 `SatWarn_s` 作为 `cl_fix_from_real` 默认值的含义，并留意 `cl_fix_resize` 在不同语言里默认饱和模式不同这一跨语言差异。

## 2. 前置知识

本讲假定读者已经掌握：

- 定点格式三元组 `[S,I,F]` 与总位宽 \(W = S+I+F\)（u1-l2）。
- 一个格式能表示的实数范围：有符号为 \([-2^{I},\ 2^{I}-2^{-F}]\)，无符号为 \([0,\ 2^{I}-2^{-F}]\)（u1-l2、u1-l3）。
- `cl_fix_max_value` / `cl_fix_min_value` 给出的范围边界（u1-l3）。
- 舍入模式 `FixRound`（u1-l4）的概念——舍入处理的是「小数位不够」的量化问题，而饱和处理的是「整数位不够」的溢出问题，两者是**正交**的两个维度。

两个本讲要用到的小概念：

- **溢出（overflow）**：结果的真实值超出格式可表示范围。注意「上溢」（超过最大值）与「下溢」（低于最小值）都算溢出。
- **二进制补码取模**：把一个整数限制在 \(W\) 位内的标准做法。无符号直接对 \(2^{W}\) 取模；有符号则先把范围平移到 \([0,\,2^{W})\) 取模再移回 \([-2^{W-1},\,2^{W-1})\)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | VHDL 单文件包：`FixSaturate_t` 枚举定义、`cl_fix_from_real` / `cl_fix_resize` / `cl_fix_from_int` 的饱和与告警实现。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py) | Python `FixSaturate(Enum)` 枚举定义，整数编码与 VHDL 一致。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | Python `cl_fix_from_real` 与 `cl_fix_resize` 的饱和/回绕/告警分支。 |
| [matlab/src/cl_fix_constants.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m) | MATLAB 端必须先执行的本子，建立 `Sat.*` 与 `Round.*` 常量结构。 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要饱和：溢出现象与四种 FixSaturate 模式

#### 4.1.1 概念说明

考虑无符号格式 `(false,2,2)`：位宽 \(W=4\)，可表示范围是 \([0,\ 3.75]\)（即整数 \(0\ldots15\)，步长 \(0.25\)）。如果我们想把实数 `4.2` 装进这个格式，就会发生溢出——`4.2` 超过最大值 `3.75`。

库此时面临两个独立的「是 / 否」选择：

1. **要不要把结果夹紧到边界？**（是 → 饱和 clip；否 → 回绕 wrap）
2. **要不要发出告警？**（是 → warning；否 → 静默）

两个「是/否」自由组合，得到四种模式，这正是 `FixSaturate` 的全部取值。需要特别强调：**饱和与告警是两个相互独立的开关**，`Sat_s` 表示「夹紧但不告诉你」，`Warn_s` 表示「不夹紧但告诉你」，二者不是非此即彼。

#### 4.1.2 核心流程

四种模式可以排成一张 \(2\times2\) 的真值表：

| 模式 | 是否饱和（clip） | 是否告警（warn） | 直观含义 |
| --- | :---: | :---: | --- |
| `None_s` | 否（回绕） | 否 | 完全不管，溢出就让它溢出 |
| `Warn_s` | 否（回绕） | 是 | 不夹紧，但提示你溢出了 |
| `Sat_s` | 是（夹紧） | 否 | 静默夹紧到边界 |
| `SatWarn_s` | 是（夹紧） | 是 | 夹紧到边界并提示你 |

记忆窍门：名字里带 `Sat` 的会**饱和**，名字里带 `Warn` 的会**告警**；`None_s` 两个都没有，`SatWarn_s` 两个都有。

#### 4.1.3 源码精读

VHDL 用一个枚举类型 `FixSaturate_t` 定义这四个值，并用注释表格写明了各自的含义：

[en_cl_fix_pkg.vhd:189-203](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L189-L203) — VHDL 的 `FixSaturate_t` 枚举及其文档表，四个值 `None_s / Warn_s / Sat_s / SatWarn_s` 的顺序与含义。

Python 用标准库 `enum.Enum` 定义完全同名的枚举，**整数值 0–3 与 VHDL 枚举位置序号一一对应**：

[en_cl_fix_types.py:67-71](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L67-L71) — Python `FixSaturate` 枚举，`None_s=0, Warn_s=1, Sat_s=2, SatWarn_s=3`。

MATLAB 没有枚举语法，而是用一个名为 `Sat` 的结构体承载四个常量，数值同样为 0–3：

[cl_fix_constants.m:9-13](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L9-L13) — MATLAB 的 `Sat.None_s … Sat.SatWarn_s` 常量。

MATLAB 端有一个**必须遵守的约定**：在调用任何 `cl_fix_*` 函数之前，必须先执行 `cl_fix_constants`，否则 `Sat.*` / `Round.*` 这些结构体根本不存在，见文件开头的说明：

[cl_fix_constants.m:6](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_constants.m#L6) — 注释明确指出本脚本必须先于任何 `cl_fix_...` 函数执行。

> 三语言共享 0–3 的整数编码并非巧合：它正是 u1-l1 所述「位真一致性」在饱和模式上的体现——同一个整数编码在 VHDL/Python/MATLAB 里指向同一种饱和行为，从而保证三套实现对同一输入产生一致的溢出处理结果。

#### 4.1.4 代码实践

1. **实践目标**：确认三种语言里四种模式的整数编码完全一致。
2. **操作步骤**：在 Python 中 `from en_cl_fix_pkg.en_cl_fix_types import FixSaturate`，打印 `{m.name: m.value for m in FixSaturate}`；同时打开 VHDL 源码 197–203 行与 MATLAB `cl_fix_constants.m` 9–13 行对照。
3. **需要观察的现象**：Python 的 `None_s=0, Warn_s=1, Sat_s=2, SatWarn_s=3`，与 VHDL 枚举位置序号、MATLAB 结构体赋值三者完全相同。
4. **预期结果**：三语言编码表逐项相等。
5. 若仅阅读源码未运行环境，本步可标注「待本地验证」后直接据源码断言。

#### 4.1.5 小练习与答案

**练习 1**：想要「结果溢出时夹紧到最大值，但不要打印任何告警」，应选哪个模式？
**答案**：`Sat_s`（饱和、不告警）。

**练习 2**：`Warn_s` 和 `SatWarn_s` 都会告警，它们对**结果数值**的影响有何不同？
**答案**：`Warn_s` 不夹紧，结果按回绕（取模）得到一个可能符号反转的「错误」值；`SatWarn_s` 把结果夹紧到合法边界（最大/最小值），数值仍是合法的极值。

---

### 4.2 饱和（clip）与回绕（wrap）：位级行为对比

#### 4.2.1 概念说明

「饱和」和「回绕」是两种截然不同的溢出处理策略：

- **饱和（clip / saturate）**：把超范围的值**夹紧**到最近的合法边界。超过最大值就取最大值，低于最小值就取最小值。结果永远是格式范围内的合法值，代价是丢失了「超出多少」的信息。
- **回绕（wrap）**：不做夹紧，直接对结果**取模**，让多余的高位自然丢弃。这正是二进制补码运算的天然行为——在硬件加法器里，溢出位本来就会被丢掉。回绕的结果仍在范围内，但数值可能发生剧烈跳变（例如大正值变成负值）。

二者的关系：回绕是**硬件加法器的默认行为**（几乎零成本），饱和则需要额外的比较与选择逻辑（成本更高，但结果更「安全」）。

#### 4.2.2 核心流程

设目标格式总位宽为 \(W\)，结果的真实整数值为 \(N\)（即真实值 \(= N\cdot 2^{-F}\)）。

**回绕（wrap）** 的整数公式（与源码一致）：

- 无符号：\(N_{\text{wrap}} = N \bmod 2^{W}\)
- 有符号（补码）：先把范围平移到 \([0,\,2^{W})\)，取模，再移回 \([-2^{W-1},\,2^{W-1})\)：

\[
N_{\text{wrap}} = \bigl((N + 2^{W-1}) \bmod 2^{W}\bigr) - 2^{W-1}
\]

**饱和（clip）** 的公式：

\[
N_{\text{clip}} = \min\bigl(\max(N,\ N_{\min}),\ N_{\max}\bigr)
\]

其中 \(N_{\min}\)、\(N_{\max}\) 是格式可表示的最小/最大整数（对应 `cl_fix_min_value` / `cl_fix_max_value` 乘以 \(2^{F}\)）。

以把有符号 `(true,4,0)` 的值 `7` 放进 `(true,2,0)`（\(W=2\)，范围 \([-4,\,3]\)）为例：

| 策略 | 计算 | 结果 |
| --- | --- | --- |
| 回绕 | \(((7+2)\bmod 4)-2 = (9\bmod 4)-2 = 1-2\) | `-1` |
| 饱和 | \(\min(\max(7,-4),3)\) | `3` |

回绕把 `7`（4 位二进制 `0111`）截到低 2 位 `11`，在有符号 2 位解释下正是 `-1`——这就是「高位丢弃、符号反转」的典型现象。

#### 4.2.3 源码精读

Python `cl_fix_resize` 的回绕分支用 `np.where` 与取模实现，符号位通过加减偏移完成平移：

[en_cl_fix_pkg.py:242-269](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L242-L269) — `None_s` / `Warn_s` 时的回绕分支：有符号 `((rounded + 2^IntBits) % 2^(IntBits+1)) - 2^IntBits`，无符号 `rounded % 2^IntBits`，与上面的取模公式逐项对应。

而饱和分支则是简单的上下界夹紧：

[en_cl_fix_pkg.py:271-273](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L271-L273) — `Sat_s` / `SatWarn_s` 时的饱和分支：`np.where(rounded > fmtMax, fmtMax, rounded)` 与对称的下界夹紧。

> 注意：回绕分支里有一个 `convertToWide` 判定（245–263 行）。当有符号回绕的中间加法可能超出双精度 53 位精度时，Python 会临时切到大整数（`object` dtype）路径计算取模，再转回浮点。这是 u6-l1 要详讲的 narrow/wide 派发问题，本讲只需知道「回绕在某些宽格式下会借用大整数运算」即可。

#### 4.2.4 代码实践

1. **实践目标**：用 Python 直观对比同一溢出场景下回绕与饱和的结果差异。
2. **操作步骤**：在已安装 numpy 的环境里运行：

   ```python
   # 示例代码
   import warnings
   from en_cl_fix_pkg.en_cl_fix_types import FixFormat, FixRound, FixSaturate
   from en_cl_fix_pkg.en_cl_fix_pkg import cl_fix_resize, cl_fix_to_real

   aFmt = FixFormat(True, 4, 0)   # 值 7 在范围内
   rFmt = FixFormat(True, 2, 0)   # 范围 [-4, 3]
   for sat in (FixSaturate.None_s, FixSaturate.Sat_s):
       r = cl_fix_resize(7, aFmt, rFmt, FixRound.Trunc_s, sat)
       print(sat, cl_fix_to_real(r, rFmt))
   ```

3. **需要观察的现象**：`None_s` 输出 `-1`（回绕），`Sat_s` 输出 `3`（饱和夹紧到最大值）。
4. **预期结果**：回绕与 4.2.2 节手算一致；饱和给出范围内的极值 `3`。
5. 若本地未配置运行环境，可标注「待本地验证」，但结论可由 242–273 行源码直接推断。

#### 4.2.5 小练习与答案

**练习 1**：为什么硬件设计者通常更倾向回绕而非饱和？
**答案**：回绕是二进制加法器的天然行为，不需要额外比较/选择逻辑，几乎零成本、零延迟；饱和需要额外的范围检测与多路选择，面积和时序代价更高。

**练习 2**：把无符号 `(false,3,0)` 的值 `9` 回绕到 `(false,2,0)`（\(W=2\)，范围 \([0,3]\)），结果是多少？
**答案**：\(9 \bmod 4 = 1\)，结果是 `1`。

---

### 4.3 cl_fix_from_real 的饱和与告警逻辑

#### 4.3.1 概念说明

`cl_fix_from_real` 是把一个（双精度）实数装入定点格式的入口函数。它做三件事，顺序很重要：

1. **量化**：用 half-up 舍入把实数对齐到 \(2^{-F}\) 网格（注意：这里的舍入固定为 half-up，**不受** `FixRound` 参数控制，与 u1-l4 的七种模式无关）。
2. **告警判定**：若调用者选择了带 `Warn` 的模式，且量化后的值超出格式范围，就发出一条警告。
3. **饱和判定**：若调用者选择了带 `Sat` 的模式，就把值夹紧到 `[min_value, max_value]`；否则**不做夹紧**——此时返回值可能是一个超出格式范围的数。

这里有一个对初学者非常反直觉、却很重要的点：当模式是 `None_s` 或 `Warn_s` 时，`cl_fix_from_real` **不会**把超范围值夹紧，它会返回一个「装不进该格式」的值（在 Python 里这只是个普通的超出范围的浮点数）。这正是「是否饱和」开关的实际效果。

#### 4.3.2 核心流程

以无符号 `(false,2,2)`（范围 \([0,\ 3.75]\)）输入实数 `4.2` 为例：

```
4.2
 │  量化(half-up): floor(4.2*4 + 0.5)/4 = floor(17.3)/4 = 17/4
 ▼
4.25            ← 17 这个整数已超出 4 位无符号上限 15
 │
 ├── None_s   → 4.25      （不夹紧、不告警）
 ├── Warn_s   → 4.25      （不夹紧，但发 Warning）
 ├── Sat_s    → 3.75      （夹紧到 max，不告警）
 └── SatWarn_s→ 3.75      （夹紧到 max，并发 Warning）
```

四种模式的输出差异一目了然：`None_s`/`Warn_s` 保留 `4.25`（一个越界值），`Sat_s`/`SatWarn_s` 夹紧到 `3.75`；告警只在带 `Warn` 的两种模式里出现。

#### 4.3.3 源码精读

Python `cl_fix_from_real` 的默认饱和模式正是 `SatWarn_s`（既夹紧又告警，最「安全」的默认）：

[en_cl_fix_pkg.py:149-171](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L149-L171) — `cl_fix_from_real` 全文：默认 `saturate=SatWarn_s`；155–161 行是告警分支，164 行是固定的 half-up 量化，167–169 行是饱和夹紧。

关键的三段：

- **告警分支**（带 `Warn` 才触发）—— [en_cl_fix_pkg.py:155-161](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L155-L161)：用 `np.max`/`np.min` 找出数组上下界，分别与 `cl_fix_max_value` / `cl_fix_min_value` 比较，越界则 `warnings.warn`。
- **量化**（恒定 half-up）—— [en_cl_fix_pkg.py:164](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L164)：`np.floor(a*(2.0**rFmt.FracBits)+0.5)/2.0**rFmt.FracBits`。
- **饱和分支**（带 `Sat` 才触发）—— [en_cl_fix_pkg.py:167-169](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L167-L169)：用 `np.where` 把越界值夹紧到 `max_value` / `min_value`。

注意 Python 用的是标准库 `warnings.warn(..., Warning)` 来发告警——这是 Python 惯用的「非致命提示」机制，可以被 `warnings.filterwarnings` 过滤或捕获，不会抛异常中断程序。

**一个值得留意的跨语言差异**：VHDL 的 `cl_fix_from_real` 虽然同样声明了 `saturate : FixSaturate_t := SatWarn_s` 参数，但它的函数体**无条件**先把输入夹紧到 `[cl_fix_min_real, cl_fix_min_real]` 范围，再量化，全程既不引用 `saturate` 参数、也不发任何 `assert` 告警：

[en_cl_fix_pkg.vhd:1660-1668](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1660-L1668) — VHDL `cl_fix_from_real` 的 `-- Limit` 段，无条件把 `a` 夹紧到 `[cl_fix_min_real, cl_fix_max_real]`。

这意味着在 VHDL 里，`cl_fix_from_real` 实际上**总是饱和**（等价于 `Sat_s`），`saturate` 参数对该函数没有行为效果（既不能关闭夹紧，也不会触发告警）。这跟 Python 实现（严格按四种模式分支）并不完全一致。作为对比，VHDL 的 `cl_fix_from_int` 是**严格按模式分支**的，带 `Warn` 时会 `assert ... severity warning`，带 `Sat` 时才夹紧：

[en_cl_fix_pkg.vhd:1595-1610](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1595-L1610) — `cl_fix_from_int` 的告警 `assert` 与饱和夹紧分支，展示了 VHDL 中「严格按模式分支」的标准写法。

> 这是本讲一个需要「待本地验证」的细节：在严格的位真协同仿真中，若依赖 `cl_fix_from_real` 在 `None_s` 下返回越界值的行为，VHDL 与 Python 会给出不同结果。读者在跨语言比对时应特别留意 `from_real` 这一函数。

#### 4.3.4 代码实践（本讲主实践任务）

1. **实践目标**：亲手观察四种饱和模式在 `cl_fix_from_real` 上的输出与告警差异，验证 4.3.2 节的流程图。
2. **操作步骤**：在已安装 numpy 的环境里运行（确保 `python/src` 在 `PYTHONPATH` 中）：

   ```python
   # 示例代码
   import warnings
   from en_cl_fix_pkg.en_cl_fix_types import FixFormat, FixSaturate
   from en_cl_fix_pkg.en_cl_fix_pkg import cl_fix_from_real

   rFmt = FixFormat(False, 2, 2)   # 无符号，范围 [0, 3.75]
   for sat in FixSaturate:
       with warnings.catch_warnings(record=True) as w:
           warnings.simplefilter("always")
           val = cl_fix_from_real(4.2, rFmt, sat)
           warned = any("exceeds" in str(x.message) for x in w)
       print(f"{sat.name:10s} -> {val:.4f}   warned={warned}")
   ```

3. **需要观察的现象**：`None_s`/`Warn_s` 输出 `4.2500`（越界值），`Sat_s`/`SatWarn_s` 输出 `3.7500`（夹紧到最大值）；告警仅在 `Warn_s` 与 `SatWarn_s` 出现。
4. **预期结果**：四行输出依次为 `4.25 / 4.25 / 3.75 / 3.75`，`warned` 标志依次为 `False / True / False / True`。
5. 若本地未配置运行环境，可标注「待本地验证」，但结论可由 155–169 行源码直接推出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SatWarn_s` 被选作 `cl_fix_from_real` 的默认饱和模式？
**答案**：因为它同时「夹紧到合法范围」与「告警」，是最安全的默认——既保证返回值总能装进目标格式，又提醒调用者发生了信息丢失。

**练习 2**：用 `None_s` 调用 `cl_fix_from_real(4.2, FixFormat(False,2,2), FixSaturate.None_s)` 返回 `4.25`，但 `4.25` 并不在该格式范围内。这个返回值「错」吗？
**答案**：不算「实现错误」，而是该模式的**约定行为**：`None_s` 明确表示「不饱和」，因此函数如实返回量化后的越界值，由调用者自行决定如何处理。在 Python 里它只是一个普通浮点数；若随后真正写入 4 位硬件寄存器，高位会被丢弃（等价于回绕）。

---

### 4.4 cl_fix_resize 的饱和/回绕与告警机制

#### 4.4.1 概念说明

`cl_fix_resize` 是整个库最核心的函数（u3-l2、u3-l3 会专门拆讲它的舍入与饱和实现）。它负责把一种格式的定点数「重塑」成另一种格式——可能要丢小数位（舍入）、也可能要丢整数位（饱和）。本讲只聚焦其中的**饱和维度**：`cl_fix_resize` 如何根据 `sat` 参数在回绕与饱和之间切换、如何发告警。

一个容易踩的坑：**`cl_fix_resize` 的默认饱和模式在不同语言里不一样**。Python 默认 `None_s`（回绕、不告警），而 VHDL 默认 `Warn_s`（回绕、但告警）。所以「不显式传 `sat` 参数」时，两种语言的行为并不完全相同——这是比 `cl_fix_from_real`（两语言都默认 `SatWarn_s`）更需要警惕的跨语言差异。

#### 4.4.2 核心流程

`cl_fix_resize` 处理饱和的统一流程：

```
1. 量化/舍入（见 u3-l2，受 FixRound 控制）
        ▼  得到 rounded（一个可能越界的中间值）
2. 告警判定：if sat ∈ {Warn_s, SatWarn_s} 且 rounded 越界 → warnings.warn / assert severity warning
        ▼
3. 分支：
   ├── if sat ∈ {None_s, Warn_s} → 回绕（取模，见 4.2）
   └── if sat ∈ {Sat_s, SatWarn_s} → 饱和（夹紧到 [min, max]，见 4.2）
```

注意步骤 2 的告警与步骤 3 的回绕/饱和是**独立**的：`Warn_s` 会告警但走回绕分支，`Sat_s` 不告警但走饱和分支。这与 4.1.1 节「两个独立开关」的论述一致。

#### 4.4.3 源码精读

先看默认值差异。Python `cl_fix_resize` 默认 `None_s`：

[en_cl_fix_pkg.py:190-192](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L190-L192) — Python `cl_fix_resize` 签名，`sat : FixSaturate = FixSaturate.None_s`。

VHDL `cl_fix_resize` 默认 `Warn_s`：

[en_cl_fix_pkg.vhd:2027-2032](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2027-L2032) — VHDL `cl_fix_resize` 签名，`saturate : FixSaturate_t := Warn_s`。

Python 的告警与饱和/回绕分支（已在 4.2.3 引用回绕与饱和分支，这里补上告警分支）：

[en_cl_fix_pkg.py:234-239](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L234-L239) — Python 告警分支：`sat ∈ {Warn_s, SatWarn_s}` 且 `rounded` 越界时 `warnings.warn("cl_fix_resize : Saturation warning!", Warning)`。

VHDL 的实现更体现硬件思维：它先在一个足够宽的 `TempFmt_c` 中间格式上做完舍入（预留了 `CarryBit_c` 进位位），然后用 `CutIntSignBits_c` 检测「超出结果位宽的高位是否既非全 0 也非全 1」来判断溢出，再按模式夹紧。检测到溢出时，带 `Warn` 的模式会触发 `assert ... severity warning`：

[en_cl_fix_pkg.vhd:2105-2123](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2105-L2123) — VHDL `cl_fix_resize` 的饱和段：`CutIntSignBits_c > 0 and saturate /= None_s` 时进入；有符号/无符号输出分别检测高位，`assert saturate = Sat_s report "..." severity warning` 发告警，`if saturate /= Warn_s` 才真正执行夹紧（把越界的高位用符号位填充）。

读懂这段的关键在 2109–2110 行的配合：

- `assert saturate = Sat_s report "cl_fix_resize : Saturation Warning!" severity warning` —— 当 `sat` 是 `Warn_s` 或 `SatWarn_s` 时（即「带 Warn」），`saturate = Sat_s` 为假，`assert` 条件为假，于是触发 `report` 并以 `severity warning` 告警；当 `sat` 是 `Sat_s` 时不告警。
- `if saturate /= Warn_s` —— 只有当 `sat` 不是「只告警不夹紧」的 `Warn_s` 时才执行夹紧。所以 `Warn_s` 只告警不夹紧（走回绕），`Sat_s`/`SatWarn_s` 才夹紧。

这与 Python 的四分支逻辑语义一致，只是 VHDL 用 `assert` + 条件夹紧的紧凑写法实现。此外，VHDL 用 `-- synthesis translate_off` / `translate_on` 包裹告警分支，使告警逻辑不参与综合（不生成额外硬件），这一综合考量将在 u7-l1 详讲。

#### 4.4.4 代码实践

1. **实践目标**：验证 `cl_fix_resize` 的告警与回绕/饱和分支互相独立，并体验默认模式的跨语言差异。
2. **操作步骤**：对 4.2.4 节的脚本稍作扩展，把四种 `sat` 模式都跑一遍，并捕获告警：

   ```python
   # 示例代码
   import warnings
   from en_cl_fix_pkg.en_cl_fix_types import FixFormat, FixRound, FixSaturate
   from en_cl_fix_pkg.en_cl_fix_pkg import cl_fix_resize, cl_fix_to_real

   aFmt, rFmt = FixFormat(True, 4, 0), FixFormat(True, 2, 0)
   for sat in FixSaturate:
       with warnings.catch_warnings(record=True) as w:
           warnings.simplefilter("always")
           r = cl_fix_resize(7, aFmt, rFmt, FixRound.Trunc_s, sat)
           warned = any("Saturation" in str(x.message) for x in w)
       print(f"{sat.name:10s} -> {cl_fix_to_real(r, rFmt):+.0f}   warned={warned}")
   ```

3. **需要观察的现象**：`None_s`/`Warn_s` 走回绕得 `-1`，`Sat_s`/`SatWarn_s` 走饱和得 `3`；告警只在 `Warn_s`/`SatWarn_s` 出现。
4. **预期结果**：四行输出 `-1 / -1 / +3 / +3`，`warned` 依次为 `False / True / False / True`。
5. 同时对照 [en_cl_fix_pkg.vhd:2031](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2031) 与 [en_cl_fix_pkg.py:192](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L192)，提醒自己：若不显式传 `sat`，VHDL 默认会告警而 Python 不会。本地未跑 VHDL 仿真时，VHDL 行为可标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：在 VHDL `cl_fix_resize` 的饱和段，为什么检测到溢出后用的是 `if saturate /= Warn_s then ...夹紧...`，而不是 `if saturate = Sat_s or saturate = SatWarn_s then ...`？
**答案**：两者等价——`Warn_s` 是四个模式里唯一「带 Warn 但不带 Sat」的，所以 `saturate /= Warn_s` 恰好选中所有「应夹紧」的模式（`Sat_s`、`SatWarn_s`），以及 `None_s`（但 `None_s` 在 2105 行的 `saturate /= None_s` 外层判定里已被排除，根本进不来）。作者用更简短的单条件写法表达同样的语义。

**练习 2**：调用 `cl_fix_resize` 时若**不**显式传 `sat`，Python 与 VHDL 的默认行为有何不同？这对位真协同仿真意味着什么？
**答案**：Python 默认 `None_s`（回绕、不告警），VHDL 默认 `Warn_s`（回绕、但告警）。两者**结果数值相同**（都回绕），但**告警不同**；更重要的是，它提醒我们：跨语言调用时应当显式传 `sat` 参数，避免依赖语言相关的默认值。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「饱和模式全景」小调研：

1. 在 Python 中构造 `rFmt = FixFormat(False, 2, 2)`（范围 \([0,\ 3.75]\)）。
2. 用 `cl_fix_from_real` 把 `[-0.5, 4.2]` 两个越界值分别按四种 `FixSaturate` 模式装入 `rFmt`，记录返回值与是否告警，整理成一张 `模式 × {返回值, 是否告警}` 的表。
3. 把任一 `Sat_s` 得到的结果（一个 `wide_fxp` 之外的普通浮点 `3.75`），再用 `cl_fix_resize` 重塑到更窄的 `FixFormat(False, 1, 1)`（范围 \([0,\ 1.5]\)），同样跑四种 `sat` 模式，观察第二次重塑时是否再次发生溢出与告警。
4. 整理结论：回答「`SatWarn_s` 在两级级联 reshape 中一共可能触发几次告警、最终值是多少」，并用 4.1.2 的真值表逐级核对。
5. 最后打开 [en_cl_fix_pkg.vhd:189-203](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L189-L203) 与 [en_cl_fix_types.py:67-71](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L67-L71)，确认你所用的四个模式在两种语言里指向同一组整数编码。

本任务同时用到 4.1（模式语义）、4.2（clip 与 wrap 的数值差异）、4.3（`from_real` 分支）和 4.4（`resize` 分支与默认值差异）四个模块的知识。若全程未跑代码，请将数值结果标注「待本地验证」，但模式→行为的映射应能据源码断言。

## 6. 本讲小结

- `FixSaturate` 是两个独立开关——「是否饱和（clip）」与「是否告警（warn）」——组合出的四种模式：`None_s` / `Warn_s` / `Sat_s` / `SatWarn_s`，名字里带 `Sat` 的会夹紧、带 `Warn` 的会告警。
- **饱和**把越界值夹紧到 `[min_value, max_value]`；**回绕**用取模 \(N \bmod 2^{W}\)（有符号需平移）丢弃高位，可能让大正值变成负值。回绕几乎零成本，饱和代价更高但更安全。
- 三种语言共享 0–3 的整数编码（VHDL 枚举、Python `Enum`、MATLAB `Sat.*` 结构体），这是位真一致性的前提；MATLAB 必须先运行 `cl_fix_constants` 建立 `Sat.*` 常量。
- `cl_fix_from_real` 默认 `SatWarn_s`：先 half-up 量化，再按模式告警/夹紧；`None_s`/`Warn_s` 会返回**越界值**而不夹紧。
- 一个需留意的跨语言差异：VHDL `cl_fix_from_real` **无条件**夹紧、不引用 `saturate` 参数也不告警，与严格按模式分支的 Python 实现不一致；而 `cl_fix_from_int`（VHDL）则严格按模式分支（`assert ... severity warning`）。
- 另一个跨语言差异：`cl_fix_resize` 的默认饱和模式 Python 为 `None_s`、VHDL 为 `Warn_s`，跨语言调用时应显式传 `sat`。告警在 Python 用 `warnings.warn`，在 VHDL 用 `assert ... severity warning`。

## 7. 下一步学习建议

本讲只讲了饱和模式「是什么、怎么选、在哪发告警」，还没有深入 `cl_fix_resize` 内部如何**实现**饱和（中间全精度格式 `TempFmt_c`、`CarryBit_c`、`CutIntSignBits_c` 的溢出检测）。建议接下来的学习路径：

- **u3-l1（数值与字符串转换函数）**：系统了解 `cl_fix_from_real` / `cl_fix_to_real` / `cl_fix_from_int` 等转换函数族，巩固本讲对 `from_real` 量化与饱和顺序的理解。
- **u3-l2（cl_fix_resize 的舍入机制）**：进入 resize 的舍入维度（`DropFracBits`、`HalfMinusDelta`），与本讲的饱和维度合起来就是完整的 resize。
- **u3-l3（cl_fix_resize 的饱和与回绕）**：专门拆解 `TempFmt_c`、`CarryBit_c`、`CutIntSignBits_c` 与有/无符号 clip 的位级实现，把本讲 4.4 的 VHDL 片段读透。
- 阅读源码时，可带着本讲的两个跨语言差异（`from_real` 的无条件夹紧、`resize` 的默认模式不同）去对照 testbench `vhdl/tb/en_cl_fix_pkg_tb.vhd` 与 Python `python/unittest/en_cl_fix_pkg_test.py` 中的饱和相关用例，亲手验证行为。
