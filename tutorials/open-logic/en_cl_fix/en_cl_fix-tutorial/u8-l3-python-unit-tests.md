# Python 单元测试与格式最优性验证

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `bittrue/tests/python/` 下四类测试脚本各自的**验证哲学**，并区分「差分测试」「性质测试」「逐点单元测试」三种思路。
- 读懂 `cl_fix_round_test.py` 与 `cl_fix_saturate_test.py` 如何用一个**完全独立于库本身**的 numpy 参考实现，对**全部穷举取值**做逐元素比对。
- 理解 `format_tests.py` 如何在不依赖任何「标准答案」的前提下，用「**充分且必要**」两条数学断言证明 `FixFormat.for_add / for_sub / for_addsub / for_mult / for_neg / for_abs / for_shift` 给出的格式是最优的。
- 看懂 `en_cl_fix_pkg_test.py` 基于 `unittest` 的逐点覆盖范围，尤其是 narrow / wide 一致性验证。
- 能够仿照已有测试，为 `for_shift` 设计一段最小化的最优性验证思路。

## 2. 前置知识

本讲是 U8（专家层）的收尾，假设你已掌握以下内容（对应前置讲义）：

- **Python 主接口与三段式范式**（u4-l1）：`cl_fix_*` 算术函数统一走「预测全精度中间格式 `mid_fmt` → 无损运算 → `cl_fix_resize` 收敛」，`cl_fix_resize = cl_fix_round ⟶ cl_fix_saturate`，且 `cl_fix_is_wide` 以 53 位为界把内部计算分发到 `NarrowFix` 或 `WideFix`。
- **NarrowFix / WideFix**（u4-l2、u4-l3）：narrow 用归一化的 float64 存储（快、限宽 53 位），wide 用未归一化的任意精度整数 `d = v·2^F` 存储（慢、无精度上限）；二者语义等价、可互转。
- **结果格式预测**（u3 系列）：`for_add / for_mult / ...` 是综合期可算的纯函数，遵循「保守最坏情况」原则，给出能装下任意输入结果的最小格式。

此外需要一点 numpy 常识：`np.floor`、`np.ceil`、`np.where`、`np.around`、`np.arange` 都是对整个数组逐元素操作的向量化函数。本讲大量使用「向量化 + 穷举」的写法，本质是把 `for` 循环交给 C 层去跑。

### 一个贯穿全讲的核心直觉：穷举（exhaustive）

定点格式的取值集合是**有限且可枚举**的：一个位宽为 \(W\) 的格式恰好有 \(2^W\) 个合法取值。因此 en_cl_fix 的数值正确性测试不走「随机采几千个点」的路子，而是**把每个被测格式在测试范围内全部取值都跑一遍**。只要测试范围覆盖到位，这就是比随机测试强得多的保证——它等于在说「对所有可能的输入，结论都成立」，而不是「对我随机采到的输入，结论成立」。

这个穷举能力由一个小函数 `get_data` 提供，它是本讲的主角之一。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 行数 | 作用 |
|------|------|------|
| [cl_fix_round_test.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py) | 136 | 舍入的**差分测试**：库的 `cl_fix_round` vs 独立 numpy 参考实现，逐模式、穷举取值比对 |
| [cl_fix_saturate_test.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py) | 133 | 饱和的差分测试：库的 `cl_fix_saturate` vs 独立 numpy 参考实现，含回绕（wrap）与钳位（clamp） |
| [format_tests.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py) | 315 | 格式预测的**性质测试**：用「充分 + 必要」断言证明 `FixFormat.for_*` 给出的格式最优 |
| [en_cl_fix_pkg_test.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py) | 763 | 基于 `unittest` 的**逐点单元测试**：覆盖 width / from_real / resize / add / mult / shift / in_range / 索引等 |
| [cosim_utils.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py) | 83 | 协同仿真公共脚手架，提供**权威版** `get_data`（被全部 13 个 cosim 脚本复用） |
| [en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | — | 被测对象本体：`FixRound` / `FixSaturate` 枚举与 `FixFormat.for_*` 静态方法 |

> 提示：前三个测试脚本都是**顶层脚本**（无 `unittest`、无函数入口），直接 `python xxx.py` 运行，靠 `assert` 爆错来报失败；只有 `en_cl_fix_pkg_test.py` 用了标准 `unittest` 框架。

---

## 4. 核心概念与源码讲解

本讲按「**穷举工具 → 差分测试（数值正确性）→ 性质测试（格式最优性）→ 逐点单元测试**」的顺序展开四个最小模块。

### 4.1 穷举验证的基石：get_data 与三种验证哲学

#### 4.1.1 概念说明

定点库的「正确性」其实分两个层面，需要用**不同**的测试方法去攻：

1. **数值正确性**——给定输入，`cl_fix_round` / `cl_fix_saturate` 算出来的数对不对？
   适合用**差分测试（differential testing）**：另写一个**与库毫无代码关联**的参考实现，把两者的输出逐元素比对。两份独立代码如果对所有输入都一致，就强烈说明它们都对了（同时错且错得一模一样的概率极低）。这是「N 版本编程」思想。

2. **格式最优性**——`for_add` 给出的结果格式 `[S,I,F]` 是不是「正好够用、一点不浪费」？
   这没有「标准答案」可比（格式的最优性是个数学性质），所以用**性质测试（property-based testing）**：不比对具体值，而是断言两条数学性质对所有枚举格式都成立——「**充分**（装得下最坏结果）」和「**必要**（少一位就装不下）」。

3. **边界行为**——回绕、饱和、平局舍入这些容易出错的角落。
   适合用**逐点单元测试（example-based unit test）**：人工挑出几个精心设计的输入，写死期望输出，专门打边界。

en_cl_fix 把这三件事分别交给三个脚本，正好对应本讲的三个核心模块。

而无论哪种方法，都依赖一个把「某格式的全部取值」生成出来的工具——`get_data`。

#### 4.1.2 核心流程

`get_data(fmt)` 的思路极其朴素：定点格式在硬件里就是一个**整数计数器**。把格式能表示的最小值、最大值各自转回「整数视图」（即硬件位串的整数值），然后用 `np.arange` 从最小到最大（含端点）走一遍计数器，再转回定点实数值即可。

位宽为 \(W\) 的格式有 \(2^W\) 个取值，因此 `np.arange(int_min, 1+int_max)` 恰好覆盖全部（`arange` 右端开区间，所以要 `1+int_max` 才含 `int_max`）。

#### 4.1.3 源码精读

权威版 `get_data` 位于协同仿真的公共脚手架 `cosim_utils.py`，被全部 13 个 cosim 脚本复用：

```python
def get_data(fmt : FixFormat):
    # Generate every possible value in format (counter)
    int_min = cl_fix_to_integer(cl_fix_min_value(fmt), fmt)
    int_max = cl_fix_to_integer(cl_fix_max_value(fmt), fmt)
    int_data = np.arange(int_min, 1+int_max)
    return cl_fix_from_integer(int_data, fmt)
```

- [cosim_utils.py:40-45](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py#L40-L45)：先用 `cl_fix_min_value` / `cl_fix_max_value` 拿到该格式可表示的实数极值，再用 `cl_fix_to_integer` 转成「未归一化整数」（即硬件位串的整数值，记作 \(d = v \cdot 2^{F}\)），最后 `cl_fix_from_integer` 把整数计数器还原成定点实数数组。`np.arange(int_min, 1+int_max)` 生成闭区间 \([int\_min, int\_max]\) 的全部整数。

两个数值测试脚本各自**复制了一份相同的 `get_data`**（脚本自包含、不 import cosim_utils）：

- [cl_fix_round_test.py:42-47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L42-L47)：舍入测试用的版本，逐字相同。
- [cl_fix_saturate_test.py:42-47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py#L42-L47)：饱和测试用的版本，同样逐字相同。

> 设计观察：`get_data` 的返回值是「归一化的实数数组」（narrow 路径，float64）。即使格式位宽超过 53 位，这里也不会触发 wide——因为测试用的输入格式都被限制在很窄的范围（见下文 `aI_values = np.arange(-4, 1+4)` 等），位宽远小于 53。

#### 4.1.4 代码实践

1. **实践目标**：亲手感受「穷举」的威力，看清一个格式到底有多少取值。
2. **操作步骤**：
   - 在仓库根目录确保已装好依赖：`python -m pip install -r requirements.txt`（需要 numpy）。
   - 进入测试目录运行舍入测试脚本：`cd bittrue/tests/python && python cl_fix_round_test.py`。
   - 也可以单独玩 `get_data`，把脚本里的辅助函数抄出来跑：
     ```python
     import sys; sys.path.append("bittrue/models/python")
     from en_cl_fix_pkg import *
     import numpy as np
     fmt = FixFormat(1, 1, 1)   # 位宽 3，应有 2**3 = 8 个取值
     int_min = cl_fix_to_integer(cl_fix_min_value(fmt), fmt)
     int_max = cl_fix_to_integer(cl_fix_max_value(fmt), fmt)
     data = cl_fix_from_integer(np.arange(int_min, 1+int_max), fmt)
     print(data)                # 看到全部 8 个取值
     ```
3. **需要观察的现象**：`python cl_fix_round_test.py` 末尾会打印一行 `Completed N tests.`，N 是一个很大的数（成千上万），表示穷举比对的次数。
4. **预期结果**：脚本静默跑完并打印 `Completed ... tests.`，没有任何 `AssertionError`——说明库的舍入与独立 numpy 参考在所有穷举点上完全一致。
5. **待本地验证**：具体 N 的数值取决于你机器上跑出的格式组合数，以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：格式 `[1,1,1]`（符号 1、整数 1、小数 1）共有多少个取值？最小值和最大值各是多少？

**答案**：位宽 \(W = S+I+F = 3\)，故有 \(2^3 = 8\) 个取值。最小值（有符号）为 \(-2^I = -2\)，最大值为 \(2^I - 2^{-F} = 2 - 0.5 = 1.5\)。8 个取值是 \(\{-2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5\}\)。

**练习 2**：为什么 `np.arange(int_min, 1+int_max)` 的第二个参数要写成 `1+int_max` 而不是 `int_max`？

**答案**：`np.arange` 的区间是**左闭右开**，写 `int_max` 会漏掉最大值那一点。加 1 才能把计数器的最后一个取值（`int_max`）包含进来，做到真正的全穷举。

---

### 4.2 round_check：用 numpy 参考实现逐模式比对

#### 4.2.1 概念说明

`cl_fix_round_test.py` 做的是舍入的**差分测试**。它的妙处在于：参考实现 `round_check` 用的是**最朴素的浮点数学**（`np.floor` / `np.ceil` / `np.around`），完全不走 en_cl_fix 库那套「构造 mid_fmt + 加偏移 + 截断」的位运算机制（见 u2-l2、u5-l2）。两套实现从完全不同的角度得到同一个答案，互为独立证人。

回顾 u2-l2 的核心结论：七种舍入模式的差别**只在于「平局」（被丢弃部分恰等于半个结果 LSB）如何处理**。`round_check` 把这条结论用浮点数学精确地表达了出来。

#### 4.2.2 核心流程

所有舍入模式都可以统一写成「先缩放、再取整、再缩回」的形式。设结果格式小数位为 \(F\)（即 `r_fmt.F`），结果 LSB 权重为 \(2^{-F}\)，半个 LSB 为 \(2^{-(F+1)}\)。设 \(q = 2^{F}\)，则「缩放后取整再缩回」的通式为：

\[
\text{round}(x) = \frac{\text{取整函数}\!\left(x \cdot q\right)}{q}
\]

不同模式只是换不同的「取整函数」或在缩放前加一个偏移：

- **Trunc_s**（截断）：\(\lfloor x \cdot q \rfloor / q\)，即朝 \(-\infty\) 取整（补码截断的数学本质）。
- **NonSymPos_s**（半向上，最常用）：先加半个 LSB 再截断，
  \[
  \frac{\lfloor (x + 2^{-(F+1)}) \cdot q \rfloor}{q}
  \]
- **NonSymNeg_s**（半向下）：先减半个 LSB 再向上取整，\(\lceil (x - 2^{-(F+1)}) \cdot q \rceil / q\)。
- **SymInf_s**（朝 ±∞ 对称）：正数用 NonSymPos、负数用 NonSymNeg——平局总是「远离零」。
- **SymZero_s**（朝 0 对称）：正数用 NonSymNeg、负数用 NonSymPos——平局总是「朝向零」。
- **ConvEven_s**（收敛到偶）：直接用 `np.around`（numpy 的 `around` 实现的就是「四舍六入五凑偶」banker's rounding）。
- **ConvOdd_s**（收敛到奇）：把输入整体偏移 +1 个 LSB，做一次凑偶，再减回 1 个 LSB——巧妙的「凑偶 → 凑奇」转换。

这正好与库内「round(x) = trunc(x + offset)，七模式只差 ±1 微调」的位运算实现（u5-l2）**数学等价但写法无关**。

#### 4.2.3 源码精读

`round_check` 用两个嵌套小函数封装 NonSymPos / NonSymNeg，再按模式分发：

```python
def NonSymPos(a, a_fmt, r_fmt):
    a = a + 2.0**-(r_fmt.F+1)
    return np.floor(a * 2.0**r_fmt.F) / 2.0**r_fmt.F

def NonSymNeg(a, a_fmt, r_fmt):
    a = a - 2.0**-(r_fmt.F+1)
    return np.ceil(a * 2.0**r_fmt.F) / 2.0**r_fmt.F
```

- [cl_fix_round_test.py:53-59](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L53-L59)：两个基础积木。`2.0**-(r_fmt.F+1)` 正是半个结果 LSB。注意它们对整个数组 `a` 向量化操作，没有 Python 层循环。

模式分发主体：

```python
if rnd is FixRound.Trunc_s:
    return np.floor(a * 2.0**r_fmt.F) / 2.0**r_fmt.F
elif rnd is FixRound.NonSymPos_s:
    return NonSymPos(a, a_fmt, r_fmt)
...
elif rnd is FixRound.SymInf_s:
    return np.where(a >= 0, NonSymPos(...), NonSymNeg(...))
elif rnd is FixRound.SymZero_s:
    return np.where(a >= 0, NonSymNeg(...), NonSymPos(...))
elif rnd is FixRound.ConvEven_s:
    return np.around(a * 2.0**r_fmt.F) / 2.0**r_fmt.F
elif rnd is FixRound.ConvOdd_s:
    return (np.around(a * 2.0**r_fmt.F + 1) - 1) / 2.0**r_fmt.F
```

- [cl_fix_round_test.py:61-76](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L61-L76)：七种模式逐一分发。`np.where(cond, x, y)` 逐元素三选一，实现 SymInf / SymZero 的「按符号选不同半舍」。ConvOdd 的 `+1 ... -1` 技巧把 numpy 自带的凑偶 `around` 改造成了凑奇。

驱动循环把「输入格式 × 目标小数位 × 七种舍入模式」全部组合穷举，并对**三套实现**同时比对：

```python
r      = cl_fix_round(a, a_fmt, r_fmt, rnd)               # 库的 narrow 实现
r_wide = WideFix.from_narrowfix(NarrowFix(a, a_fmt)).round(r_fmt, rnd).to_real()  # WideFix 实现
expected = round_check(a, a_fmt, r_fmt, rnd)              # 独立 numpy 参考
assert np.array_equal(r, expected),        "Numerical error detected."
assert np.array_equal(r_wide, expected),   "Numerical error detected (WideFix)."
```

- [cl_fix_round_test.py:122-132](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L122-L132)：三元对照——`cl_fix_round`（narrow 路径）、`WideFix.round`（wide 路径，输入是 narrow 数据临时升格，见 u4-l3 的「提升规则」）、`round_check`（独立参考）三者必须**逐元素全等**。这同时验证了 narrow 与 wide 两条实现路径彼此一致（承接 u4-l2/u4-l3 的 narrow/wide 对偶）。

枚举范围与跳过逻辑：

- [cl_fix_round_test.py:85-90](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L85-L90)：输入格式的 S/I/F 与目标小数位 rF 都在 `[-4, +4]` 间枚举。
- [cl_fix_round_test.py:101-119](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L101-L119)：跳过非法格式（`aS+aI+aF <= 0`），并用 `FixFormat.for_round` 推导目标格式（`try/except AssertionError` 跳过 `for_round` 拒绝的边角组合）。

#### 4.2.4 代码实践

1. **实践目标**：验证「平局」是七种模式唯一分歧点，并亲手把库实现与浮点参考对上。
2. **操作步骤**：
   - 构造一个目标格式 `[0,2,0]`（无符号、2 整数位、0 小数位，可表示 0..3），输入取若干「平局」值：0.5、1.5、2.5。
   - 用 `round_check` 的公式手算 NonSymPos / NonSymNeg / SymInf / SymZero / ConvEven 在这些点上的结果，再调用库 `cl_fix_round` 核对。
     ```python
     from en_cl_fix_pkg import *
     a_fmt = FixFormat(0, 2, 2); r_fmt = FixFormat(0, 2, 0)
     a = cl_fix_from_real([0.5, 1.5, 2.5], a_fmt)
     for rnd in [FixRound.NonSymPos_s, FixRound.NonSymNeg_s,
                 FixRound.SymInf_s, FixRound.SymZero_s, FixRound.ConvEven_s]:
         print(rnd, cl_fix_round(a, a_fmt, r_fmt, rnd))
     ```
3. **需要观察的现象**：在 0.5、1.5 这些「正好半个 LSB」的平局点上，不同模式结果**开始分化**（NonSymPos 都向上、NonSymNeg 都向下、SymInf 远离零、SymZero 朝向零、ConvEven 凑到最近的偶数）；而在非平局点（如 0.2、0.7）所有模式结果一致。
4. **预期结果**：库输出与 `round_check` 手算完全一致。
5. **待本地验证**：以上为源码阅读 + 公式推导所得，具体数值以本地运行为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `round_check` 里 ConvOdd_s 写成 `(np.around(x·q + 1) − 1)/q`，而不是另写一个「凑奇」取整函数？

**答案**：因为「凑奇」等价于「先把数轴整体平移 1 个 LSB、做凑偶、再平移回去」。平移后原来落在偶数上的点变成落在奇数上，凑偶就变成凑原来的奇数。这样直接复用 numpy 已有的 `around`（凑偶），无需自己实现凑奇逻辑，简洁且不易错。

**练习 2**：本测试同时比对了 `r`、`r_wide`、`expected` 三者。如果某天有人改坏了 `WideFix.round` 但 `cl_fix_round`（narrow）没动，哪个断言会先爆？

**答案**：`assert np.array_equal(r_wide, expected)` 会爆（带后缀 `(WideFix)`）。这正是设计三层对照的意义——把 narrow 与 wide 两条独立路径都钉在同一个黄金参考上，任一条偏移都会被单独定位。

---

### 4.3 cl_fix_saturate_test：wrap 与 clamp 的独立参考实现

#### 4.3.1 概念说明

饱和（见 u2-l3）处理的是「整数位 / 符号位被压缩」时的越界。四种饱和模式由「是否钳位」「是否告警」两个开关组合而成。`cl_fix_saturate_test.py` 同样用差分测试，参考实现 `sat_check` 用纯浮点数学独立复现两种行为：

- **不钳位**（None_s / Warn_s）：越界值**回绕（wrap）**——等价于模运算，`+100` 可能变成 `+4`。
- **钳位**（Sat_s / SatWarn_s）：越界值被**钉在** `v_max` / `v_min`，保持单调。

#### 4.3.2 核心流程

回绕的数学本质是模运算：把值反复加减一个「周期」\(T = 2^{S+I}\)（即整个可表示区间的宽度，等于符号位+整数位覆盖的范围），直到落进 \([v_{\min}, v_{\max}]\)。钳位则是简单的 `clip`：超过上限取上限、低于下限取下限。

注意一个前提约束：饱和要求**小数位不变**（`r_fmt.F == a_fmt.F`），因为饱和是 `cl_fix_resize` 的末步、必须在舍入对齐小数位之后进行（u2-l3）。

#### 4.3.3 源码精读

`sat_check` 的回绕分支用 `while` 循环反复加减周期：

```python
if sat is FixSaturate.None_s or sat is FixSaturate.Warn_s:
    min_r = cl_fix_min_value(r_fmt)
    max_r = cl_fix_max_value(r_fmt)
    offset = 2.0 ** (r_fmt.S + r_fmt.I)
    for i in range(len(a)):
        while a[i] < min_r:
            a[i] += offset
        while a[i] > max_r:
            a[i] -= offset
    return a
```

- [cl_fix_saturate_test.py:55-65](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py#L55-L65)：回绕分支。`offset = 2.0 ** (r_fmt.S + r_fmt.I)` 就是区间周期 \(T\)。这里**有 Python 层循环**（逐元素 `while`），因为模运算的「加减次数」随元素而变、不便向量化——这是参考实现「宁可慢也要显然正确」的典型取舍。

钳位分支则用 `np.where` 一次性裁剪：

```python
elif sat is FixSaturate.Sat_s or sat is FixSaturate.SatWarn_s:
    a = np.where(a > cl_fix_max_value(r_fmt), cl_fix_max_value(r_fmt), a)
    a = np.where(a < cl_fix_min_value(r_fmt), cl_fix_min_value(r_fmt), a)
    return a
```

- [cl_fix_saturate_test.py:66-70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py#L66-L70)：钳位分支，两次 `np.where` 分别裁上下限，向量化、无循环。

函数顶部的契约断言：

- [cl_fix_saturate_test.py:53](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py#L53)：`assert r_fmt.F == a_fmt.F`，强制饱和前后小数位不变。

驱动循环同样穷举「输入格式 × 目标 S/I × 四种饱和模式」，并三路比对库实现、WideFix 实现、独立参考：

- [cl_fix_saturate_test.py:118-132](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py#L118-L132)：与舍入测试同构——`cl_fix_saturate`、`WideFix.saturate`、`sat_check` 三者必须逐元素全等。

> 阅读观察（不影响测试，但值得一品）：[cl_fix_saturate_test.py:72](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py#L72) 的兜底 `raise ValueError(f"Unrecognized rounding mode: {rnd}")` 里引用了一个**未定义的变量 `rnd`**（本函数参数是 `sat`）。这是从舍入测试复制过来时漏改的痕迹。它之所以从未暴露，是因为驱动循环只遍历合法的 `FixSaturate` 成员，这条 `else` 永远走不到——是一段「死代码」。这也提醒我们：差分测试之所以可靠，正因为它把所有合法路径都跑了一遍，使这种潜伏问题无处藏身（一旦有人传入非法值，这里会抛 `NameError` 而非预期的 `ValueError`）。

#### 4.3.4 代码实践

1. **实践目标**：看清回绕与钳位在越界值上的不同表现，并理解为何二者不可混淆。
2. **操作步骤**：
   - 运行 `cd bittrue/tests/python && python cl_fix_saturate_test.py`，观察末尾 `Completed N tests.`。
   - 构造一个「把大格式压进小格式」的场景：输入 `[1,8,0]`（可表示 -256..255），目标 `[1,4,0]`（可表示 -16..15），挑几个越界值（如 17、100、-20）：
     ```python
     from en_cl_fix_pkg import *
     a_fmt = FixFormat(1, 8, 0); r_fmt = FixFormat(1, 4, 0)
     a = cl_fix_from_integer([17, 100, -20], a_fmt)
     print("wrap :", cl_fix_saturate(a, a_fmt, r_fmt, FixSaturate.None_s))
     print("clamp:", cl_fix_saturate(a, a_fmt, r_fmt, FixSaturate.Sat_s))
     ```
3. **需要观察的现象**：`None_s`（回绕）下，17→1、100→4、-20→-4（模 \(2^{1+4}=32\) 的结果）；`Sat_s`（钳位）下，越界值被钉在 15 或 -16。
4. **预期结果**：库输出与 `sat_check` 手算（周期 32 反复加减）一致。
5. **待本地验证**：具体回绕数值以本地运行为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sat_check` 的回绕分支用 `while` 循环逐元素加减，而不是像钳位分支那样用 `np.where` 一次性算？

**答案**：回绕是模运算，每个元素需要加减的「周期次数」不同（越界越多越多次），无法用一个固定的向量化表达式表达。钳位则是「超过阈值就替换为阈值」，阈值固定，天然适合 `np.where` 一次搞定。参考实现优先「显然正确」，宁可慢。

**练习 2**：`offset = 2.0 ** (r_fmt.S + r_fmt.I)` 为什么是 `S+I` 而不是总位宽 `S+I+F`？

**答案**：回绕发生在「整数位/符号位被压缩」时，小数位不变（函数顶部已断言 `r_fmt.F == a_fmt.F`）。区间的周期等于「整数部分（含符号位）覆盖的范围」\(2^{S+I}\)，与无关的小数位无关。补码回绕的本质就是按整数部分的模折叠。

---

### 4.4 format_tests：格式预测的「充分且必要」最优性验证

#### 4.4.1 概念说明

前两个模块验证「数值对不对」，本模块验证「**格式预测得好不好**」——这是性质测试，没有标准答案可比。`format_tests.py` 的目标用一句话说清（来自其文件头注释）：确保所有 `FixFormat.For*` 函数给出的格式都是**最优的（sufficient and necessary）**。

- **充分（sufficient）**：预测格式 `r_fmt` 能装下**最坏情况**的结果——既不溢出上限，也不溢出下限。少这一条，格式就「不够宽」，综合后会丢数据。
- **必要（necessary）**：把 `r_fmt` 的整数位**减 1** 后就**装不下**了——上限或下限至少有一侧越界。少这一条，格式就「过宽」，浪费硬件资源。

两条同时成立，才证明 `r_fmt` 恰好「够用且不浪费」。此外还有一个**闭式断言**：小数位 F 必须等于理论最优值（如乘法是 `a.F + b.F`），这是「显然最优」的部分，直接断言相等即可。

#### 4.4.2 核心流程

`format_tests.py` 把每个二元运算（add/sub/addsub/mult）和每个一元运算（neg/abs/shift）都套进同一个三段式模板：

1. **算最坏结果极值** `rmax` / `rmin`：用 `cl_fix_min_value` / `cl_fix_max_value` 拿到两输入各自极值，再按运算的极端组合算出结果的上下界。关键是挑对「极端组合」——乘法在双方都有符号时，最大值是 `amin * bmin`（两个最负值相乘得最大正值），而不是 `amax * bmax`。
2. **充分性断言**：`rmax <= max_value(r_fmt)` 且 `rmin >= min_value(r_fmt)`。
3. **必要性断言**：构造 `smaller_fmt = FixFormat(r_fmt.S, r_fmt.I - 1, r_fmt.F)`，断言 `rmax > max_value(smaller_fmt)` 或 `rmin < min_value(smaller_fmt)`（少一位就至少一侧越界）。
4. **小数位断言**：`r_fmt.F == 理论最优F`。

格式参数被**大范围穷举**：`a/b` 的 I、F 都在 `[-6, +6]` 间枚举（比数值测试的 `[-4,+4]` 更宽，因为格式预测只算极值、不生成数据，便宜得多）。

数学上，充分与必要可以写成（设 `smaller_fmt` 为整数位减 1 后的格式）：

\[
\underbrace{v_{\max} \le \mathrm{maxValue}(r\_fmt) \;\wedge\; v_{\min} \ge \mathrm{minValue}(r\_fmt)}_{\text{充分}}
\qquad
\underbrace{v_{\max} > \mathrm{maxValue}(r\_fmt') \;\vee\; v_{\min} < \mathrm{minValue}(r\_fmt')}_{\text{必要}}
\]

其中 \(r\_fmt' = (S,\, I-1,\, F)\)。

#### 4.4.3 源码精读

配置区把格式参数的枚举范围集中声明：

- [format_tests.py:44-55](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L44-L55)：`a/b` 的 S∈{0,1}、I∈[-6,6]、F∈[-6,6]；shift 的 `min_shift`∈[-4,4]、`shift_range`∈[0,4]。注意这里穷举的是**格式参数**，不是数据取值——所以即使范围更大，代价也远低于数值测试。

以加法块为例，看完整的「极值 → 充分 → 必要 → F」四步：

```python
# Calculate the extreme results
rmax = amax + bmax
rmin = amin + bmin
# Sanity checks
assert rmax == np.amax([amin + bmin, amin + bmax, amax + bmin, amax + bmax])
assert rmin == np.amin([amin + bmin, amin + bmax, amax + bmin, amax + bmax])

# Format to test
r_fmt = FixFormat.for_add(a_fmt, b_fmt)

# Check int bits are sufficient
assert rmax <= cl_fix_max_value(r_fmt), "add: Max value exceeded" + ...
assert rmin >= cl_fix_min_value(r_fmt), "add: Min value exceeded" + ...

# Check int bits are necessary
smaller_fmt = FixFormat(r_fmt.S, r_fmt.I - 1, r_fmt.F)
assert rmax > cl_fix_max_value(smaller_fmt) or rmin < cl_fix_min_value(smaller_fmt), "add: Format is excessively wide." + ...

# The optimal number of frac bits is trivial: max(a_fmt.F, b_fmt.F)
assert r_fmt.F == max(a_fmt.F, b_fmt.F), "add: Unexpected number of frac bits"
```

- [format_tests.py:101-123](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L101-L123)：加法完整模板。
  - L102-106：先算 `rmax/rmin`，再用 `np.amax/amin` 对**四个角**做「健全性检查（sanity check）」——确保手写的极端组合确实就是真正的极值（防止人脑挑错角）。
  - L109：调用被测的 `FixFormat.for_add`。
  - L112-115：**充分性**——结果极值必须落在 `r_fmt` 的可表示范围内，否则报 "Max/Min value exceeded"。
  - L118-120：**必要性**——把整数位减 1 得 `smaller_fmt`，要求「至少一侧越界」，否则报 "Format is excessively wide"。
  - L123：**小数位**闭式断言，加法最优 F = `max(a.F, b.F)`。

乘法块的「极端组合」最值得品味——最大值的选取要分符号情况：

```python
if a_fmt.S == 1 and b_fmt.S == 1:
    rmax = amin * bmin  # -max*-max = +max
else:
    rmax = amax * bmax
```

- [format_tests.py:186-203](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L186-L203)：乘法的极值计算。当**双方都有符号**时，最大正值来自两个最负值相乘（`amin * bmin`，因为 \(-M \times -M = +M^2\)），这正是 u3-l2 强调的「极端值恰为 2 的幂」导致位宽特殊增长的根因。`rmin` 则按四种符号组合分别挑出最负的乘积。两条 `np.amax/amin` 的 sanity check 同样守护「别挑错角」。

- [format_tests.py:206-223](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L206-L223)：乘法的充分/必要/F 断言。注意 L215 的守卫 `if r_fmt.I + r_fmt.F > 0`——当结果格式只剩符号位（`I+F == 0`）时不能再减整数位（会变成负宽度非法格式），故跳过必要性检查。乘法的最优 F 是 `a.F + b.F`（L223），注释用「各取 ±1 LSB 相乘得 ±2^{-(aF+bF)}」给出了简洁证明。

`for_shift` 也已经被这个文件覆盖了（这很重要，别误以为它没测）：

- [format_tests.py:285-315](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L285-L315)：shift 块。双层循环枚举 `min_shift` 与 `shift_range`（得 `max_shift = min_shift + shift_range`），算移位后的极值 `rmax/rmin`，对 `FixFormat.for_shift(a_fmt, min_shift, max_shift)` 做同样的充分/必要/F 三连断言，最优 F = `a.F - min_shift`（L315）。这与 [en_cl_fix_types.py:303-315](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L303-L315) 中 `for_shift` 返回 `FixFormat(a_fmt.S, a_fmt.I + maxShift, a_fmt.F - minShift)` 的定义逐字对应。

一元运算（neg/abs）的模板更短，但同样是「极值 → 充分 → 必要 → F」四步：

- [format_tests.py:225-280](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L225-L280)：neg（`rmax=-amin, rmin=-amax`）与 abs（`rmax=max(amax,-amin)`）块，均验证 `for_neg` / `for_abs` 的最优性。

#### 4.4.4 代码实践

1. **实践目标**：吃透「充分 + 必要」二连断言的结构，并亲手为 `for_shift` 写一段独立的最小化验证思路（注意：`format_tests.py` 其实已内置 shift 验证块，本练习是让你**独立重写一遍**以加深理解，再与已有实现对照）。
2. **操作步骤**：
   - 通读 [format_tests.py:101-123](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L101-L123) 的 add 块，确认它就是「极值 → 充分 → 必要 → F」四步。
   - 仿照 add 块，为 `for_shift` 写一段**独立的最小验证脚本**（示例代码，非项目原有）：
     ```python
     # 示例代码：为 for_shift 写一段最小化的「充分且必要」验证
     from en_cl_fix_pkg import *
     for aS in [0,1]:
       for aI in range(-4, 5):
         for aF in range(-4, 5):
           if aS+aI+aF < 1: continue
           a_fmt = FixFormat(aS, aI, aF)
           amin, amax = cl_fix_min_value(a_fmt), cl_fix_max_value(a_fmt)
           for min_shift in range(-4, 5):
             for max_shift in range(min_shift, min_shift+5):
               # 1) 算移位后的最坏极值
               rmax = amax * 2.0**max_shift
               rmin = amin * (2.0**max_shift if amin < 0 else 2.0**min_shift)
               # 2) 被测格式
               r_fmt = FixFormat.for_shift(a_fmt, min_shift, max_shift)
               # 3) 充分：装得下
               assert rmax <= cl_fix_max_value(r_fmt)
               assert rmin >= cl_fix_min_value(r_fmt)
               # 4) 必要：整数位减1就装不下
               if r_fmt.I + r_fmt.F > 0:
                   smaller = FixFormat(r_fmt.S, r_fmt.I-1, r_fmt.F)
                   assert rmax > cl_fix_max_value(smaller) or rmin < cl_fix_min_value(smaller)
               # 5) 小数位闭式最优：a.F - min_shift
               assert r_fmt.F == a_fmt.F - min_shift
     print("for_shift 最优性验证通过")
     ```
   - 把你写的这段与仓库已有的 [format_tests.py:285-315](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L285-L315) 逐行对照，确认思路一致。
3. **需要观察的现象**：你的独立脚本应静默跑完并打印通过信息；与已有 shift 块对比时，注意已有的实现还多了一条 `rmin` 的分支（`amin < 0` 时用 `max_shift`、否则用 `min_shift`）和更详细的失败信息。
4. **预期结果**：两份实现断言逻辑等价，均不爆 `AssertionError`。
5. **待本地验证**：穷举范围与极值分支以本地实际代码与运行为准。

#### 4.4.5 小练习与答案

**练习 1**：`format_tests.py` 为什么不需要像 `round_check` 那样写一个「独立参考实现」来比对其中的数值？

**答案**：因为它测的不是「某个数算得对不对」，而是「格式这个**性质**成不成立」。性质的真伪可以直接用 `cl_fix_max_value/min_value` 这些库内基础工具去检验（极值是否落在范围内），不需要第二个实现。它依赖的是「充分 + 必要」这两条数学定义本身，而不是另一份代码。

**练习 2**：必要性断言里为什么是「`rmax > max(smaller)` **或** `rmin < min(smaller)`」而不是「**且**」？

**答案**：必要性的含义是「少一位就**至少有一侧**装不下」。只要上限或下限任一侧越界，就说明这一位是必需的、不可省。「且」会要求两侧同时越界，那条件太强——很多格式只在单侧用满了整数位（例如无符号格式只有上限、没有负下限），「且」会错误地判定它们「过宽」。

**练习 3**：为什么 mult/neg/shift 的必要性断言前都有 `if r_fmt.I + r_fmt.F > 0` 守卫，而 add/sub/addsub/abs 没有？

**答案**：add/sub/addsub/abs 的结果格式总是至少有 1 个整数位（它们的极值通常跨过 0 或达到 ±某幂），减 1 不会变成非法格式；而 mult/neg/shift 在某些边角格式下结果可能只剩符号位（`I+F == 0`），此时再减整数位会得到负宽度、`FixFormat` 构造直接抛 `AssertionError`。守卫就是为了在这些「已经是最小」的情况下跳过必要性检查。

---

### 4.5 en_cl_fix_pkg_test：基于 unittest 的逐点覆盖与 narrow/wide 一致性

#### 4.5.1 概念说明

第四个脚本 `en_cl_fix_pkg_test.py` 是传统的 `unittest` 单元测试集（763 行，最大的一个）。它和前三个脚本的哲学不同：不穷举、不证性质，而是**人工挑选边界输入、写死期望输出**，专打容易出错的角落——回绕、饱和、平局舍入、最负值、narrow/wide 索引等。它是「白盒、针对性」的补充，覆盖前三个脚本没专门照顾到的具体 API。

#### 4.5.2 核心流程

每个被测 API 对应一个 `unittest.TestCase` 子类，类里若干 `test_xxx` 方法，每个方法用 `self.assertEqual` / `self.assertWarns` / `self.assertRaises` 断言一个具体输入→具体期望。脚本末尾用 `unittest.main()` 驱动，由标准框架收集并报告通过/失败。

#### 4.5.3 源码精读

覆盖面一览（按类组织）：

- [en_cl_fix_pkg_test.py:34-55](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L34-L55)：`cl_fix_width_Test`——位宽公式 `S+I+F`，含负 I、负 F 的边角。
- [en_cl_fix_pkg_test.py:58-79](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L58-L79)：`cl_fix_from_real_Test`——量化舍入、越界告警（`assertWarns`）、`Sat_s` 钳位、因舍入触发的边界告警。
- [en_cl_fix_pkg_test.py:110-253](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L110-L253)：`cl_fix_resize_Test`——本文件最长的类，逐模式覆盖 resize 的增减小数位、增减整数位、增减符号位、以及七种舍入模式在固定小数点（如 ±0.5、±1.5、±1.75）上的具体结果。这些「平局点」正是差分测试不专门标注、而逐点测试能精确钉死的角落。
- [en_cl_fix_pkg_test.py:417-481](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L417-L481)：`cl_fix_mult_Test`——覆盖有符号×有符号、无符号×无符号、混合符号、以及「无符号×无符号结果仍无符号」等特殊情形（承接 u3-l2 的乘法符号位特例）。
- [en_cl_fix_pkg_test.py:697-753](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L697-L753)：`cl_fix_Indexing_Test`——分别用 narrow 格式 `[1,5,5]`（`assertFalse(cl_fix_is_wide)`）和 wide 格式 `[1,50,50]`（`assertTrue(cl_fix_is_wide)`）验证位索引的 get/set，确保 narrow 与 wide 两条内部路径在索引语义上一致（承接 u4-l2/u4-l3）。

驱动入口：

- [en_cl_fix_pkg_test.py:758-759](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L758-L759)：`if __name__ == "__main__": unittest.main()`，标准 unittest 启动方式。可直接 `python en_cl_fix_pkg_test.py` 运行，也可用 `python -m unittest` 发现。

#### 4.5.4 代码实践

1. **实践目标**：用 unittest 框架跑一遍，看清它与穷举脚本的输出风格差异。
2. **操作步骤**：
   - 运行：`cd bittrue/tests/python && python en_cl_fix_pkg_test.py`（或 `python -m unittest en_cl_fix_pkg_test -v`）。
   - 阅读一个具体用例，例如 [en_cl_fix_pkg_test.py:181-193](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L181-L193) 的 `test_NonSymNeg_*` 系列，确认它把 `-0.5 / -1.5 / +0.5 / +1.5 / +1.75` 这些平局点的期望输出逐一写死。
3. **需要观察的现象**：unittest 会打印 `Ran N tests in x.xs` 与 `OK`（或失败详情），风格与穷举脚本的 `Completed N tests.` 不同。
4. **预期结果**：全部用例通过、打印 `OK`。
5. **待本地验证**：用例数 N 以本地实际为准。

#### 4.5.5 小练习与答案

**练习 1**：`en_cl_fix_pkg_test` 里大量使用 `self.assertWarns(Warning)`（如 `cl_fix_from_real(4.2, [0,2,2])`）。这对应 `FixSaturate` 的哪种模式？

**答案**：对应 `Warn_s`（回绕 + 告警）或 `SatWarn_s`（钳位 + 告警）——即「告警」开关闭合的模式。`cl_fix_from_real` 的默认饱和模式正是 `SatWarn_s`（u5-l1 的破例之一），所以越界时既会钳位又会告警，`assertWarns` 能捕获到这条告警。

**练习 2**：`cl_fix_Indexing_Test` 为什么要同时测一个 narrow 格式和一个 wide 格式？

**答案**：因为 narrow（float64）和 wide（object 整数数组）是两条**完全不同**的内部存储路径（u4-l2/u4-l3）。索引的 get/set 在两条路径上各自实现，必须分别验证语义一致，否则可能出现「窄格式索引对、宽格式索引错」的隐蔽 bug。`cl_fix_is_wide` 的断言先确认格式确实落在了预期的路径上，再做索引验证。

---

## 5. 综合实践

把本讲四类测试串起来，做一次「**给库加一个新验证**」的完整小任务：

**背景**：假设你怀疑 `cl_fix_resize`（先 round 后 saturate）在「舍入进位恰好顶到饱和上限」的边界上可能有差一错误。

**任务**：

1. **用差分思路**：参考 `round_check` + `sat_check` 的写法，独立写一个 `resize_check(a, a_fmt, r_fmt, rnd, sat)`——先调 `round_check` 舍入到 `for_round` 推出的中间格式，再调 `sat_check` 收敛到 `r_fmt`。注意顺序必须是「先 round 后 saturate」，且饱和阶段小数位不变。
2. **用穷举思路**：用 `get_data` 生成若干 `(a_fmt, r_fmt)` 组合的全部取值，逐模式、逐饱和模式比对 `cl_fix_resize` 与你的 `resize_check`，外加 `WideFix.from_narrowfix(...).resize(...).to_real()` 三路一致。
3. **用逐点思路**：仿照 `en_cl_fix_pkg_test` 的 `test_OverflowDueRounding_*`（[L169-178](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L169-L178)），手挑几个「舍入后恰好等于上限」的输入，写死期望输出。

**验收标准**：三种方法互不依赖、互为佐证，若三者全过，则该边界行为得到三层保证；若某一种爆错，则精确定位是库的问题还是参考实现的问题。这是 en_cl_fix 测试体系的核心精神——**用多个独立视角把同一个结论钉死**。

## 6. 本讲小结

- en_cl_fix 的 Python 测试分四类，体现**三种验证哲学**：穷举差分（数值正确性）、性质断言（格式最优性）、逐点 unittest（边界行为）。
- **穷举**是数值测试的基石：`get_data` 用整数计数器生成某格式的全部 \(2^W\) 个取值，权威版在 [cosim_utils.py:40-45](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py#L40-L45)，被数值测试脚本各自复制一份。
- `round_check` / `sat_check` 用**完全独立**的 numpy 浮点数学复现七种舍入与四种饱和，与库的位运算实现互为证人，且同时钉死 narrow 与 wide 两条路径。
- `format_tests.py` 不比对数值，而是用「**充分**（装得下最坏结果）+ **必要**（少一位就装不下）+ **小数位闭式最优**」三条断言，证明 `for_add/sub/addsub/mult/neg/abs/shift` 给出的格式最优，并已覆盖 `for_shift`。
- `en_cl_fix_pkg_test.py` 用 `unittest` 逐点覆盖 width / from_real / resize / add / mult / shift / in_range / 索引，并专门验证 narrow 与 wide 索引语义一致。
- 这套测试是整个「VHDL 金标准 ↔ Python 参考模型」验证闭环的**Python 侧根基**：Python 参考模型本身必须先被证明正确，它才能作为 u7 协同仿真里生成 VHDL 黄金数据的可信源头。

## 7. 下一步学习建议

- **回看协同仿真闭环**：本讲验证了「Python 参考模型正确」，接下来可重读 u7-l1，看 `cosim.py` 如何调用这些已被验证的 `cl_fix_*` 函数生成 VHDL 比对用的黄金数据，理解「Python 正确 → VHDL 可信」的传递链。
- **对照 VHDL 实现**：本讲的 `round_check` 偏移技巧可与 u5-l2 的 `cl_fix_round`（mid_fmt + half_c + 截断）逐行对照，体会「同一数学、两种实现」。
- **扩展测试范围**：尝试把 `format_tests.py` 的格式枚举范围从 `[-6,6]` 扩到更大，或为 `for_round`（[en_cl_fix_types.py:319-342](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L319-L342)）补一段「充分且必要」验证（注意它还多一条「舍入可能 +1 整数位」的进位性质需要单独覆盖）。
- **接入 CI**：这些脚本是顶层 `python xxx.py` 即可跑的轻量测试，适合挂入 CI 作为 PR 门禁，与 `sim/run.py` 驱动的 VHDL 仿真形成「Python 快测 + HDL 全测」两层防线。
