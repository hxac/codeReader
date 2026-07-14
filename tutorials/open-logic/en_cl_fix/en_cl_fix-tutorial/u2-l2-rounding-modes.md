# 舍入模式 FixRound

## 1. 本讲目标

承接 u2-l1 把「格式 `[S,I,F]`」讲透之后，本讲回答下一个自然的问题：**当 `F` 变小（要丢掉一些小数位）时，丢掉的那部分怎么办？** 这就是「舍入（rounding）」。读完本讲，你应当能够：

- 说清楚舍入**何时**发生，以及为什么补码定点里「直接截断 = 朝 \(-\infty\) 取整（floor）」。
- 区分七种舍入模式 `Trunc_s / NonSymPos_s / NonSymNeg_s / SymInf_s / SymZero_s / ConvEven_s / ConvOdd_s`，并说清它们**唯一**的差别在于「平局（tie）」如何处理。
- 看懂 README 的舍入示例表，能手算若干典型值在七种模式下的结果。
- 读懂 VHDL `cl_fix_round` 的核心实现：它用一个统一的「**先加偏移量、再截断**」机制实现全部七种模式，每种模式只是偏移量不同。
- 自己动手用 Python 跑一遍舍入，并与 VHDL 的 `case` 分支逐条对照。

本讲只讲「舍入」，**不**涉及饱和（那是 u2-l3）和算术运算（那是 U3、U4、U5）。

## 2. 前置知识

### 2.1 「舍入」到底在解决什么

定点数的小数位个数 `F` 决定了它的**分辨率**：相邻两个可表示值之间的步长是 \(2^{-F}\)。例如 `[0,4,4]` 的步长是 \(2^{-4}=0.0625\)，而 `[0,4,1]` 的步长是 \(2^{-1}=0.5\)。

当我们把一个格式从 `F=4` 改成 `F=1` 时，原来能精确表示的 `2.6875` 在新格式里**不存在**了——它落在 `2.5` 和 `3.0` 之间。我们必须决定：是把它当作 `2.5`，还是 `3.0`？这就是舍入要回答的问题。注意：**只有 `F` 减小（丢小数位）时才需要舍入**；`F` 增大只是低位补零，毫无歧义。

### 2.2 平局（tie）：所有舍入模式分歧的唯一来源

被丢弃的小数部分，如果**正好等于**结果最低位（LSB）权重的一半，就称为「平局」。例如把 `2.75`（二进制 `10.11`）舍入到 `F=1`：结果 LSB 权重是 \(0.5\)，它的一半是 \(0.25\)，而被丢弃的 `0.25` **正好等于**这个一半——这就是平局。

工程界对此没有唯一标准答案，于是衍生出多种舍入模式。**理解本讲的关键结论是：七种模式的差别，全部、且仅仅在于「平局往哪边靠」**；非平局情况下，绝大多数模式结果一致。README 用一句话点明了这一点：

[README.md:169-173](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L169-L173) —— 说明 `Trunc_s` 最省资源但误差最大（等价 `floor(x)`）；`NonSymPos_s` 是最常用的通用模式（等价 `floor(x+0.5)`），但因平局一律向上而有统计偏差；其余模式只在平局处理上与 `NonSymPos_s` 不同，需要无偏舍入时通常选 `ConvEven_s`/`ConvOdd_s`。

### 2.3 一个关键性质：补码截断 = 朝 \(-\infty\) 取整

这是理解 VHDL 实现的「钥匙」。对**补码**（有符号）或无符号整数，直接丢弃最低 `k` 位，等价于数学上的：

\[
\mathrm{trunc}(x) = \left\lfloor \frac{x}{2^{k}} \right\rfloor \cdot 2^{k}
\]

也就是**朝 \(-\infty\) 方向取整**（floor），而**不是**朝零取整。例如 `-0.5` 截断到整数会得到 `-1`（不是 `0`）。这一条贯穿后面 `cl_fix_round` 的全部推导。

## 3. 本讲源码地图

本讲涉及三个关键源文件，分别代表「文档」「Python 参考模型」「VHDL 金标准」三个视角（延续 u1-l2 的三语言镜像架构）：

| 文件 | 作用 | 本讲用到的内容 |
|---|---|---|
| [README.md](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md) | 项目文档 | `Rounding Modes` 章节的七模式示例表与说明 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | Python 类型定义 | `FixRound` 枚举、`FixFormat.for_round` |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | VHDL 主包 | `FixRound_t` 枚举、`cl_fix_round`、`get_half`、`get_unit_bit`、`convert` |

> 提示：本讲会反复印证一个镜像事实——Python 的 `FixRound` 与 VHDL 的 `FixRound_t` 描述的是**同一组七种模式**，且 Python 参考实现与 VHDL 实现给出的结果在位级别一致（这也是 cosim 验证的前提，详见 U7）。

---

## 4. 核心概念与源码讲解

### 4.1 舍入的时机与统一机制：先加偏移、再截断

#### 4.1.1 概念说明

舍入只在 `result_fmt.F < a_fmt.F`（结果小数位比输入少）时发生。源码用一个 `if` 守住这一点，当 `F` 没有减小时，连偏移量都不加。

七种模式看起来五花八门，但 en_cl_fix 的 VHDL 实现用一个极其优雅的统一思路把它们全部搞定：**「朝 \(-\infty\) 取整（即截断）」是基础操作，只要在截断之前给数据加上一个恰当的「偏移量」，就能得到任何想要的舍入模式。** 换句话说：

\[ \text{round}(x) = \mathrm{trunc}\bigl(x + \text{offset}(x)\bigr) \]

每种模式只是 `offset` 的计算方式不同。这个思路的好处是：硬件上只需要一个加法器 + 一个截断，七种模式共用同一套数据通路，只是加法器的第二个操作数随模式变化。

#### 4.1.2 核心流程

`cl_fix_round` 的执行流程可以概括为四步：

1. **构造中间格式 `mid_fmt`**：在结果格式基础上，强制至少保留 `result_fmt.F+1` 位小数，用来容纳「平局位」（half）以及加偏移后可能产生的进位。`mid_fmt.F = max(result_fmt.F+1, a_fmt.F)`，所以把输入放进 `mid_fmt` 是**无损**的。
2. **对齐**：用 `convert` 把输入从 `a_fmt` 无损搬进 `mid_fmt`（小数点对齐、高位符号扩展）。
3. **加偏移量**：根据舍入模式，给中间值加上对应的 `offset`。
4. **截断**：丢弃 `mid_fmt` 最低的 `out_offset_c = mid_fmt.F - result_fmt.F` 位，得到结果。

其中 `half_c` 是一个关键常量：它是一个**只在「平局位」上为 1、其余为 0** 的数，数值上正好等于结果 LSB 权重的一半（\(2^{-(\text{result\_fmt.F}+1)}\)）。后面会看到，七种模式的偏移量几乎都围绕 `half_c` 加减。

#### 4.1.3 源码精读

中间格式与偏移常量的定义：

[hdl/en_cl_fix_pkg.vhd:920-936](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L920-L936) —— 定义 `mid_fmt_c`（强制至少 `result_fmt.F+1` 位小数）、`out_offset_c`（要截断掉的低位数）、`half_c`（平局位常量，由 `get_half` 生成）。

`get_half` 如何在「平局位」上置 1：

[hdl/en_cl_fix_pkg.vhd:290-297](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L290-L297) —— `tie_c := aFmt.F - rFmt.F - 1` 算出平局位的位置，再把该位置 1。注释明确：这正是「结果 LSB 权重的一半」。

无损搬入中间格式的 `convert`：

[hdl/en_cl_fix_pkg.vhd:329-351](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L329-L351) —— 把输入按小数点对齐写入结果缓冲区，高位做符号扩展（有符号）或零扩展（无符号），低位补零。注释强调它**不做**舍入、**不做**饱和，只做对齐。

#### 4.1.4 代码实践

**实践目标**：亲手验证「截断 = 朝 \(-\infty\) 取整（floor）」，确认它**不是**朝零取整——这是 2.3 节那条关键性质。

**操作步骤**（在仓库根目录执行）：

```python
# 文件名：exp_trunc_floor.py（示例代码，非项目原有文件）
import sys
sys.path.append("bittrue/models/python")   # 与项目测试一致的导入方式
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 2, 1)          # [1,2,1]，步长 0.5，范围 -4.0 .. 3.5
r_fmt = FixFormat(1, 2, 0)          # 舍入到整数（注意：Trunc 模式不需要 I 增长）

a = cl_fix_from_real([-1.5, -0.5, 0.5, 1.5], a_fmt)
r = cl_fix_round(a, a_fmt, r_fmt, FixRound.Trunc_s)
print(cl_fix_to_real(r, r_fmt))     # 截断结果
```

**需要观察的现象**：四个值 `-1.5, -0.5, 0.5, 1.5` 截断到整数后分别变成 `-2, -1, 0, 1`。注意 `-0.5 → -1`（朝 \(-\infty\)），而**不是** `0`（朝零）。

**预期结果**：打印出 `[-2.0, -1.0, 0.0, 1.0]`。这正是 README 舍入表里 `Trunc_s` 那一行的负数行为（[README.md:133-136](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L133-L136)）。

> 说明：`cl_fix_round` 的 Python 签名里 `r_fmt` 写作 `int`（[en_cl_fix.py:190](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190)），但函数体内实际把它当 `FixFormat` 用（访问 `.F`）。项目测试 [cl_fix_round_test.py:122](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L122) 传的正是 `FixFormat` 对象，本实践沿用这一被验证过的调用方式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cl_fix_round` 要强制 `mid_fmt` 至少有 `result_fmt.F+1` 位小数，而不是直接在 `result_fmt` 上加偏移量？

> **答案**：因为偏移量 `half_c` 本身就占用 `result_fmt.F+1` 那一位（平局位），加偏移后还可能产生向 `result_fmt.F` 位的进位。如果直接在 `result_fmt` 上做加法，平局位和进位都没地方放，会丢失信息。多留一位小数正是为了完整地完成「加偏移 → 进位 → 截断」这个过程。

**练习 2**：把 `-2.625` 截断到整数（`Trunc_s`），结果是 `-2` 还是 `-3`？

> **答案**：是 `-3`。截断 = floor = 朝 \(-\infty\) 取整，\(\lfloor -2.625 \rfloor = -3\)，而不是朝零的 `-2`。

---

### 4.2 七种舍入模式 FixRound：枚举、语义与示例表

#### 4.2.1 概念说明

en_cl_fix 把舍入模式定义成一个枚举（Python `FixRound`、VHDL `FixRound_t`），共七种。可以把它们分成三组来记：

- **截断**：`Trunc_s`——什么都不加，直接丢低位，等价 `floor(x)`。最省资源，误差最大。
- **有方向（非收敛）**：`NonSymPos_s`、`NonSymNeg_s`、`SymInf_s`、`SymZero_s`——用固定规则处理平局，简单快速，但带统计偏差。
- **收敛（convergent）**：`ConvEven_s`、`ConvOdd_s`——平局时让结果凑成偶数/奇数，统计上无偏，适合高精度信号处理。

「非对称（NonSym）」指对正负数采用**相同**的舍入方向，因而正负两半的范围不对齐；「对称（Sym）」则刻意让正负两半对称。`NonSymPos_s` 是工程里**最常用**的通用模式：它等价 `floor(x + 0.5)`，即「四舍五入、平局向上」，硬件实现最省、误差最小，代价是平局总偏向 `+\infty` 而带来轻微正偏差。

#### 4.2.2 核心流程

下表把 README 的官方示例表整理出来（六个值舍入到 `[1,2,0]`，即保留到整数；其中 `2.2/2.7` 是非平局，`±0.5/±1.5` 是平局）：

| 模式 | 平局规则 | 2.2 | 2.7 | -1.5 | -0.5 | 0.5 | 1.5 |
|---|---|---|---|---|---|---|---|
| `Trunc_s` | 不舍入，直接截断（floor） | 2 | 2 | -2 | -1 | 0 | 1 |
| `NonSymPos_s` | 平局一律朝 `+\infty` | 2 | 3 | -1 | 0 | 1 | 2 |
| `NonSymNeg_s` | 平局一律朝 `-\infty` | 2 | 3 | -2 | -1 | 0 | 1 |
| `SymInf_s` | 平局朝两侧「外推」（远离 0） | 2 | 3 | -2 | -1 | 1 | 2 |
| `SymZero_s` | 平局朝「内收」（趋向 0） | 2 | 3 | -1 | 0 | 0 | 1 |
| `ConvEven_s` | 平局凑成偶数 | 2 | 3 | -2 | 0 | 0 | 2 |
| `ConvOdd_s` | 平局凑成奇数 | 2 | 3 | -1 | -1 | 1 | 1 |

读这张表的三个要点：

1. **非平局的两列（2.2、2.7）所有模式结果完全相同**（2 和 3）。这印证了「差别只在平局」。
2. **`NonSymPos_s` 的四个平局结果（-1,0,1,2）都比重它一档的整数「更大」**：`-1.5→-1`、`-0.5→0`、`0.5→1`、`1.5→2`，全部朝 `+\infty`，这就是它的正偏差来源。
3. **`ConvEven_s` 让平局结果都是偶数**（-2,0,0,2），**`ConvOdd_s` 让平局结果都是奇数**（-1,-1,1,1）。大量数据下偶/奇各半，偏差相互抵消，因此收敛舍入是「无偏」的。

#### 4.2.3 源码精读

Python 端的 `FixRound` 枚举，七种模式一字排开，注释写明了每种模式的别名与含义：

[en_cl_fix_types.py:30-40](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L30-L40) —— `Trunc_s`/`NonSymPos_s`/`NonSymNeg_s`/`SymInf_s`/`SymZero_s`/`ConvEven_s`/`ConvOdd_s`，每个都带 `# ...` 注释（如 `NonSymPos_s # Non-symmetric positive (half-up)`）。

VHDL 端镜像的 `FixRound_t` 枚举，字面量与注释与 Python 逐字对应：

[hdl/en_cl_fix_pkg.vhd:49-58](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L49-L58) —— `FixRound_t` 类型定义，七个枚举值与 Python 完全一致（注意字面量都用 `_s` 后缀，这是项目的命名约定）。

README 的官方舍入章节与示例表：

[README.md:118-122](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L118-L122) —— 说明舍入只在 `F` 减小时相关，与十进制「四舍五入」同理，只是底数为 2。

[README.md:123-167](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L123-L167) —— 七模式示例表（即上面 4.2.2 整理的表格的原始 HTML 版本）。

#### 4.2.4 代码实践

**实践目标**：用 Python 一次性跑出六个典型值在七种模式下的舍入结果，**逐行**与 4.2.2 的表格（即 README 表）对照，亲手确认「差别只在平局」。

**操作步骤**（在仓库根目录执行）：

```python
# 文件名：exp_rounding_all_modes.py（示例代码，非项目原有文件）
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 2, 8)            # F=8：足够精确表示 ±0.5/±1.5，且 2.2/2.7 误差极小
inputs = [2.2, 2.7, -1.5, -0.5, 0.5, 1.5]
a = cl_fix_from_real(inputs, a_fmt)

for rnd in FixRound:                  # 遍历全部七种模式
    r_fmt = FixFormat.for_round(a_fmt, 0, rnd)   # 自动给出合法结果格式（I 会随模式变化，见 u3-l3）
    r = cl_fix_round(a, a_fmt, r_fmt, rnd)
    print(f"{rnd.name:12s}", cl_fix_to_real(r, r_fmt))
```

**需要观察的现象**：打印出七行，每行是六个数的列表。把它们与 4.2.2 表格逐行比对，应当**完全一致**。

**预期结果**（待本地验证；数值应与 README 表吻合）：

```
Trunc_s      [2.0, 2.0, -2.0, -1.0, 0.0, 1.0]
NonSymPos_s  [2.0, 3.0, -1.0, 0.0, 1.0, 2.0]
NonSymNeg_s  [2.0, 3.0, -2.0, -1.0, 0.0, 1.0]
SymInf_s     [2.0, 3.0, -2.0, -1.0, 1.0, 2.0]
SymZero_s    [2.0, 3.0, -1.0, 0.0, 0.0, 1.0]
ConvEven_s   [2.0, 3.0, -2.0, 0.0, 0.0, 2.0]
ConvOdd_s    [2.0, 3.0, -1.0, -1.0, 1.0, 1.0]
```

> 说明：这里必须用 `FixFormat.for_round(a_fmt, 0, rnd)` 生成 `r_fmt`，因为 `cl_fix_round` 内部有断言 `r_fmt == cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)`（[en_cl_fix.py:194](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L194)）。非截断模式会让整数位 `I` 加 1（保守预留进位空间），这正是 u3-l3「`round_fmt` 与整数位增长」的内容。

#### 4.2.5 小练习与答案

**练习 1**：用一句话说明 `SymInf_s` 与 `SymZero_s` 处理平局的区别。

> **答案**：`SymInf_s` 把平局「外推」——朝远离零的方向（正数向上、负数向下，即朝 \(\pm\infty\)）；`SymZero_s` 把平局「内收」——朝趋向零的方向（正数向下、负数向上）。

**练习 2**：为什么说 `ConvEven_s` 是「无偏」的，而 `NonSymPos_s` 有偏差？

> **答案**：`NonSymPos_s` 把所有平局都朝 `+\infty` 方向舍，大量样本下会引入系统性正偏差。`ConvEven_s` 在平局时让结果凑偶数，连续多个平局会向上向下交替出现，正负偏差在统计上相互抵消，长期均值为零，故无偏。

**练习 3**：README 表中，哪几列（值）能用来区分 `NonSymPos_s` 和 `SymInf_s`？

> **答案**：只有正数平局区分不了（两者都向上）。要看负数平局列 `-1.5`、`-0.5`：`NonSymPos_s` 给 `-1, 0`（朝 `+\infty`），`SymInf_s` 给 `-2, -1`（朝 `-\infty`，即远离零）。

---

### 4.3 VHDL `cl_fix_round`：每种模式的偏移加法实现

#### 4.3.1 概念说明

4.1 讲了「先加偏移、再截断」的统一框架。本节就来看这七种模式的偏移量**分别**是什么、为什么是那个值。下表把 `cl_fix_round` 里 `case` 分支的偏移量与对应语义列出（`half_c` = 平位值，`sign_c` = 输入符号位，`unit_v` = 结果未来的最低位）：

| 模式 | 加到 `mid_v` 的偏移量 | 直觉解释 |
|---|---|---|
| `Trunc_s` | `0`（`null`） | 纯截断 = floor |
| `NonSymPos_s` | `+ half_c` | `floor(x + 0.5)`，平局一律向上 |
| `NonSymNeg_s` | `+ (half_c - 1)` | 比 `0.5` 少一点点，平局一律向下 |
| `SymInf_s` | `+ half_c - sign_c` | 正数（sign=0）向上、负数（sign=1）向下 → 远离零 |
| `SymZero_s` | `+ half_c - (not sign_c)` | 正数向下、负数向上 → 趋向零 |
| `ConvEven_s` | `+ half_c - (not unit_v)` | 让结果最低位凑成 0（偶） |
| `ConvOdd_s` | `+ half_c - unit_v` | 让结果最低位凑成 1（奇） |

注意三个规律：(1) 除了 `Trunc_s`，所有模式都以 `+ half_c` 起手，再减去一个 0/1 的小修正；(2) `sign_c`、`unit_v` 都会被包成 1 位的 `unsigned`（`"" & x`），所以 `half_c ± (1 位值)` 是一次普通的加减；(3) `not sign_c`、`not unit_v` 是对单比特取反，等价于「条件取 0 或 1」。

#### 4.3.2 核心流程

理解每个偏移量为何能产生对应语义，关键是盯住「平局时」这一个场景（非平局时加 `half_c` 与加 `half_c±1` 的差别被截断抹平，结果一致）：

1. **`NonSymPos_s = +half_c`**：平局时 `x` 的小数部分正好是 `0.5`，加 `0.5` 后进位到整数，截断后向上取整 → 平局向上。这正是 `floor(x+0.5)`。
2. **`NonSymNeg_s = +(half_c-1)`**：平局时只加了 `0.5 - ε`（比半位少 1 个最低位），不足以进位，截断后向下 → 平局向下。
3. **`SymInf_s = +half_c - sign_c`**：正数（`sign_c=0`）退化为 `NonSymPos`（向上、远离零）；负数（`sign_c=1`）退化为 `NonSymNeg`（向下、远离零）。正负两侧都远离零 → 对称外推。
4. **`SymZero_s = +half_c - (not sign_c)`**：正数（`not 0 = 1`）退化为 `NonSymNeg`（向下、趋零）；负数（`not 1 = 0`）退化为 `NonSymPos`（向上、趋零）。两侧都趋向零 → 对称内收。
5. **`ConvEven_s = +half_c - (not unit_v)`**：若结果最低位 `unit_v=1`（当前为奇），偏移为 `+half_c`，平局向上进位后最低位翻成 `0` → 凑偶；若 `unit_v=0`（当前为偶），偏移为 `+half_c-1`，平局不进位，保持偶 → 凑偶。两种情况都得到偶数。
6. **`ConvOdd_s = +half_c - unit_v`**：与 `ConvEven` 相反，`unit_v=1` 时不进位保持奇，`unit_v=0` 时进位翻成奇 → 总是凑奇。

#### 4.3.3 源码精读

`cl_fix_round` 的主体（含 `case` 分支与截断）：

[hdl/en_cl_fix_pkg.vhd:944-975](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L944-L975) —— 先用 `convert` 把输入搬进 `mid_v`，再进入 `case round is`，每个模式加对应偏移量；最后 `result_v := mid_v(width(result_fmt)+out_offset_c-1 downto out_offset_c)` 截断低位得到结果。注意整个 `case` 包在 `if result_fmt.F < a_fmt.F then` 之内——`F` 不减小则根本不加偏移。

完整的 `case` 偏移分支（即 4.3.1 表格的代码出处）：

[hdl/en_cl_fix_pkg.vhd:953-969](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L953-L969) —— 七个 `when` 分支逐条对应七种模式：`Trunc_s ⇒ null`、`NonSymPos_s ⇒ mid_v + half_c`、`NonSymNeg_s ⇒ mid_v + (half_c-1)`、`SymInf_s ⇒ mid_v + half_c - ("" & sign_c)`、`SymZero_s ⇒ mid_v + half_c - ("" & not sign_c)`、`ConvEven_s ⇒ mid_v + half_c - ("" & not unit_v)`、`ConvOdd_s ⇒ mid_v + half_c - ("" & unit_v)`。`"" & x` 是把单比特 `std_logic` 拼成 1 位 `unsigned` 以便参与加减。

收敛舍入要用到的「结果最低位」`unit_v`：

[hdl/en_cl_fix_pkg.vhd:951](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L951) 调用 `get_unit_bit` 取出结果未来的最低位。其实现见 [hdl/en_cl_fix_pkg.vhd:299-311](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L299-L311)——`unit_c := aFmt.F - rFmt.F` 算出该位位置，正常情况取 `a_c(unit_c)`；若该位落在数据范围之外（`unit_c >= 宽度`），则用符号位做隐式扩展。

`sign_c` 的来源：

[hdl/en_cl_fix_pkg.vhd:933](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L933) 取 `cl_fix_sign(a_c, a_fmt)` 作为 `sign_c`。`cl_fix_sign` 的实现见 [hdl/en_cl_fix_pkg.vhd:1315-1323](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1315-L1323)：无符号或零宽返回 `'0'`，否则取最高位。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践。挑一个**平局**值，手工追踪它在 VHDL `cl_fix_round`（`NonSymPos_s`）下的完整计算过程，验证「加偏移 → 进位 → 截断」真的能得到正确结果。

**操作步骤**：

追踪输入 `a = 2.75`、`a_fmt = [0,4,4]`、模式 `NonSymPos_s`、舍入到 `F=1`。

1. **确定结果格式**：`cl_fix_round_fmt([0,4,4], 1, NonSymPos_s) = [0,5,1]`（非截断模式，`I` 加 1）。所以 `result_fmt = [0,5,1]`，位宽 6。（这一点由 `fmt_check` 强制，见 [hdl/en_cl_fix_pkg.vhd:939-942](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L939-L942)。）
2. **构造 `mid_fmt`**：`mid_fmt = [0, 5, max(1+1, 4)] = [0,5,4]`，位宽 9。
3. **搬入 `mid_v`**：`2.75` 在 `[0,5,4]` 下是 `00010.1100` = `000101100`（= 44，无符号零扩展整数位）。
4. **算 `half_c`**：平局位在第 `4-1-1 = 2` 位 → `000000100`（= 4，数值 \(0.25\)，正好是结果 LSB \(0.5\) 的一半）。
5. **加偏移**（`NonSymPos_s`）：`mid_v + half_c = 000101100 + 000000100 = 000110000`（= 48，即 `3.0`）。注意这一步**产生了进位**，把 `2.75` 的整数部分从 `2` 抬到了 `3`。
6. **截断**：`out_offset_c = mid_fmt.F - result_fmt.F = 4-1 = 3`，保留高 6 位 `000110000[8:3] = 000110`。作为 `[0,5,1]` 解读：`00011.0` = `3.0`。

**需要观察的现象**：第 5 步的进位是整个机制的精髓——平局值 `2.75` 加上 `0.25` 后刚好越过整数边界，截断后稳定落在 `3.0`，即「平局向上」。

**预期结果**：得到 `3.0`，与 4.2.2 表格中 `NonSymPos_s` 处理平局「向上」的语义一致。若改用 `NonSymNeg_s`（偏移 `half_c-1 = 3`），第 5 步变为 `000101100 + 000000011 = 000101111`，截断得 `000101` = `2.5`（平局向下）——可自行对照验证。

> 若无法运行 VHDL 仿真，可改用上一节 4.2.4 的 Python 脚本，把 `inputs` 改成 `[2.75]`、`a_fmt = FixFormat(0,4,4)`，观察 `NonSymPos_s` 与 `NonSymNeg_s` 分别输出 `3.0` 与 `2.5`，与上面手工追踪一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SymInf_s` 的偏移量是 `+ half_c - sign_c`，而 `SymZero_s` 是 `+ half_c - (not sign_c)`？它们正好「相反」体现在哪里？

> **答案**：`SymInf`（远离零）对正数要向上、对负数要向下；`SymZero`（趋向零）对正数要向下、对负数要向上。两者对同一个符号 `sign_c` 取的修正项正好相反（一个减 `sign_c`、一个减 `not sign_c`），所以正负数的舍入方向也正好相反。

**练习 2**：`ConvEven_s` 想让结果凑偶。请说明当 `unit_v = 1`（结果最低位当前为 1，即「奇」）时，偏移 `+ half_c - (not unit_v) = + half_c` 如何把它变成偶。

> **答案**：`unit_v = 1` 时 `not unit_v = 0`，偏移就是 `+ half_c`。平局时加上半位会触发进位，最低位 `1` 加上进位变成 `0`（并向更高位进一位），于是结果最低位变为 `0`，即偶数。

**练习 3**：整个 `case` 语句被包在 `if result_fmt.F < a_fmt.F then ... end if;` 里。如果把 `F` 增大的情况（比如 `[0,4,1]` → `[0,4,4]`）也送进来，会发生什么？

> **答案**：不会进入 `case`，偏移量保持为 0，直接走到截断。由于 `F` 增大时 `convert` 只是在低位补零、不存在精度损失，所以「不舍入」正是正确行为。这也解释了为什么舍入只在 `F` 减小时才有意义。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个贯穿性任务（对应本讲规格里的核心实践）：

**任务**：取 README 示例表的六个值 `2.2, 2.7, -1.5, -0.5, 0.5, 1.5`，目标是舍入到 `[1,2,0]`（整数、有符号）。

1. **手工推算**：对**七种**模式各推算一遍这六个值的结果，填出一张 7×6 的表。提示——
   - 先判断每个值是不是平局（丢弃部分是否正好等于结果 LSB 的一半）；
   - 非平局值（`2.2, 2.7`）七种模式结果都相同；
   - 平局值按 4.2.2 的「平局规则」列决定方向。
2. **与 README 对照**：把你推算的表与 [README.md:123-167](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L123-L167) 逐格比对，应当完全一致。
3. **与 VHDL 对照**：对你推算的每一个结果，回到 `cl_fix_round` 的 `case` 分支 [hdl/en_cl_fix_pkg.vhd:953-969](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L953-L969)，说出这个模式用的是哪个偏移量、为什么得到这个值。重点解释：为什么 `NonSymPos_s` 把 `-1.5` 舍成 `-1`（朝 `+\infty`），而 `SymInf_s` 把它舍成 `-2`（朝 `-\infty`，远离零）。
4. **（可选）用 Python 验证**：直接运行 4.2.4 的脚本，确认程序输出与你的手算表一致。

**验收标准**：能不查表地说出「七种模式唯一区别是平局处理」，并能对 `-1.5` 这一个值说清楚 `NonSymPos_s`/`NonSymNeg_s`/`SymInf_s`/`SymZero_s`/`ConvEven_s`/`ConvOdd_s` 各自给出什么、为什么。

## 6. 本讲小结

- 舍入**只在 `F` 减小（丢小数位）时**才有意义；`F` 增大只是低位补零。
- 补码/无符号**直接截断 = 朝 \(-\infty\) 取整（floor）**，不是朝零取整（如 `-0.5 → -1`）。
- en_cl_fix 用统一的「**先加偏移量、再截断**」机制实现全部七种模式：\(\text{round}(x)=\text{trunc}(x+\text{offset})\)，每种模式只是 `offset` 不同。
- 七种模式（[en_cl_fix_types.py:30-40](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L30-L40) 与 [hdl/en_cl_fix_pkg.vhd:49-58](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L49-L58) 镜像）**唯一**的差别是平局如何处理；非平局情况结果一致。
- `NonSymPos_s`（`floor(x+0.5)`，等价 [README.md:171](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L171)）是最常用通用模式，但平局总朝 `+\infty` 而有正偏差；需要无偏时选收敛模式 `ConvEven_s`/`ConvOdd_s`。
- 各模式偏移量的代码出处是 `cl_fix_round` 的 `case` 分支（[hdl/en_cl_fix_pkg.vhd:953-969](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L953-L969)），它们都以 `+ half_c` 为基础、再用 `sign_c`（符号）或 `unit_v`（结果最低位）做 0/1 微调。

## 7. 下一步学习建议

本讲把「丢小数位时怎么办」讲清楚了。定点格式变化还有另一个方向，建议按顺序继续：

1. **u2-l3 饱和模式 FixSaturate**：当 `I`/`S` 减小（丢整数位或有符号转无符号）时怎么办？那是「越界」问题，与舍入正交。你会看到 `cl_fix_resize = cl_fix_round ⟶ cl_fix_saturate` 的组合关系。
2. **u2-l4 位宽、极值与格式工具函数**：系统学习 `cl_fix_width`、`cl_fix_max_value`/`min_value`、`union` 等，它们是判断「是否越界」与构造合法格式的工具。
3. **u3-l3 `round_fmt` 与 `cl_fix_in_range`**：本讲多次出现的「非截断模式让 `I` 加 1」就来自 `cl_fix_round_fmt`（[hdl/en_cl_fix_pkg.vhd:608-628](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L608-L628)），那里会系统讲解「舍入可能溢出到整数位」的格式预测。
4. 之后进入 U4/U5，你会看到 `cl_fix_round` 如何作为算术运算精度收敛的核心一环被反复调用。

> 推荐配合运行：项目自带的舍入测试 `python bittrue/tests/python/cl_fix_round_test.py`（README [README.md:190-198](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L190-L198) 有说明），它用 numpy 参考实现逐模式穷举所有取值做比对，是验证你理解的最好工具。
