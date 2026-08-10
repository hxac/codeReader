# Python 单元测试体系与边界用例

## 1. 本讲目标

本讲是专家层「测试体系」的开篇。前面几讲我们分别读了「算法实现」（NarrowFix / WideFix / en_cl_fix.py）和「结果格式推导」（for_add / for_mult / for_round），但只有测试能把「我相信它是对的」变成「它在每一种组合下都被证明是对的」。本讲集中精读 `bittrue/tests/python/` 下的测试代码，回答三个问题：

1. 这些测试是**怎么组织**的？为什么有的用 `unittest` 逐条手写断言，有的却用三重 for 循环穷举？
2. 它们如何**覆盖边界**——最负值、舍入诱发溢出、回绕 vs 饱和、负整数位格式？
3. `format_tests.py` 凭什么能断言「推导出来的结果格式既够用又不浪费」？

学完后你应当能够：读懂两类测试各自的适用场景；为一个新的格式组合补一个 `unittest` 用例；理解「三实现逐位对拍」和「够用 + 不浪费双断言」两种验证思想。

## 2. 前置知识

本讲假定你已掌握（来自前置讲义）：

- **[S,I,F] 格式与位权**（u1-l2）：总位宽 \(S+I+F\)，值 = 整数比特 \(\times 2^{-F}\)。
- **resize = round + saturate**（u4-l3）：`cl_fix_resize` 先按 `for_round` 推中间格式做舍入，再饱和；顺序不可换。
- **cl_fix_* 主接口与分发**（u2-l2 / u6-l3）：所有函数走「算 mid_fmt → 无损运算 → resize」骨架，`cl_fix_is_wide`（位宽 > 53）决定走 NarrowFix 还是 WideFix。

两类 Python 测试风格在 u1-l4 已点过名：**人工断言式**（`en_cl_fix_pkg_test.py`，挑选用例逐条 `assertEqual`）与**穷举对拍式**（`cl_fix_round_test.py`，枚举全部取值三实现比对）。本讲把它们拆开深读，并补上 u1-l4 没细讲的 `format_tests.py` 与 `matlab_interface_test.py`。

此外需要一点 Python `unittest` 常识：测试类继承 `unittest.TestCase`，每个 `test_` 开头的方法是一个用例，`assertEqual(期望, 实际)` 不等即失败，`assertWarns(Warning)` / `assertRaises(ValueError)` 用来断言「应当报警/抛错」，`unittest.main()` 发现并运行全部用例。

## 3. 本讲源码地图

本讲涉及的文件都在 `bittrue/tests/python/` 下，外加两处被测实现：

| 文件 | 作用 | 测试风格 |
|------|------|----------|
| `en_cl_fix_pkg_test.py` | 覆盖几乎所有 `cl_fix_*` 公共函数的手写用例 | 人工断言式（unittest） |
| `cl_fix_round_test.py` | 穷举全部格式 × 全部 7 种舍入模式，三实现逐位对拍 | 穷举对拍式 |
| `cl_fix_saturate_test.py` | 穷举全部格式 × 全部 4 种饱和模式，三实现逐位对拍 | 穷举对拍式 |
| `format_tests.py` | 验证 `for_add/sub/addsub/mult/neg/abs/shift` 推导的格式「够用且不浪费」 | 最优性双断言 |
| `matlab_interface_test.py` | 验证 wide 定点经 uint64 分块打包/解包后无损还原 | 随机往返测试 |
| `…/en_cl_fix_pkg/en_cl_fix.py`（被测） | 主接口、narrow/wide 分发 | — |
| `…/en_cl_fix_pkg/narrow_fix.py`（被测） | `from_real` 量化与饱和 | — |

## 4. 核心概念与源码讲解

### 4.1 人工断言式测试：en_cl_fix_pkg_test.py 的类组织与断言风格

#### 4.1.1 概念说明

`en_cl_fix_pkg_test.py` 是一份「阅读友好」的测试：它**不**追求穷举，而是为每个公共函数开一个测试类，类里放若干**精心挑选**的用例，每个用例用一行 `assertEqual(期望值, 函数调用)` 表达。这种写法的价值在于：它同时是**回归测试**和**可执行的函数说明书**——读测试就能知道函数在各种典型/边界输入下应当返回什么。

它的组织原则是「**一个被测函数 → 一个测试类**」，类名直接是被测函数名加 `_Test`：

- `cl_fix_width_Test`、`cl_fix_from_real_Test`、`cl_fix_from_integer_Test`、`cl_fix_to_integer_Test`
- `cl_fix_resize_Test`（本文件最大的一类，覆盖全部 7 种舍入 + 回绕/饱和）
- `cl_fix_add_Test` / `cl_fix_sub_Test` / `cl_fix_mult_Test` / `cl_fix_abs_test` / `cl_fix_neg_Test`
- `cl_fix_shift_left_Test` / `cl_fix_shift_right_Test`
- `cl_fix_max_value_Test` / `cl_fix_min_value_Test` / `cl_fix_in_range_Test` / `cl_fix_addsub_Test`
- `cl_fix_Indexing_Test`（narrow/wide 数组的索引行为，见 4.4）

文件末尾用标准的 `unittest.main()` 驱动，由 `unittest` 自动发现所有 `test_` 方法：

[bittrue/tests/python/en_cl_fix_pkg_test.py:758-759](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L758-L759) —— 标准的 `if __name__ == "__main__": unittest.main()`，把发现和驱动交给框架。

#### 4.1.2 核心流程

一个手工用例的执行流程：

```
构造一个（或几个）有代表性的输入与格式
  → 调用被测 cl_fix_* 函数
  → 用 assertEqual 比较「手算的期望值」与「实际返回」
  → 若是越界/告警场景，改用 assertWarns / assertRaises 包裹
```

挑选用例时的几条经验法则（贯穿整个文件）：

1. **正负两侧各取一例**：舍入、饱和都对称地各测正负。
2. **边界值优先**：最负值、最大值、恰好半进位的 0.5。
3. **「应当出错」也要测**：越界默认应告警，不饱和应抛错。

#### 4.1.3 源码精读

**例 1：最朴素的宽度测试。** `cl_fix_width_Test` 验证 `S+I+F` 这个总位宽公式，顺带覆盖负整数位、负小数位（它们改变粒度但不改变位宽算法）：

[bittrue/tests/python/en_cl_fix_pkg_test.py:51-55](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L51-L55) —— `test_NegativeInt` / `test_NegativeFract`：`FixFormat(True,-2,3)` 与 `FixFormat(True,3,-2)` 宽度都是 2（\(1+(-2)+3 = 1+3+(-2) = 2\)）。

**例 2：告警场景——用 `assertWarns` 断言「应当报警」。** `cl_fix_from_real` 默认饱和模式是 `SatWarn_s`（半进位 + 饱和 + 越界告警）。越界时不抛异常、而是发 `Warning`，因此用 `assertWarns(Warning)` 包裹：

[bittrue/tests/python/en_cl_fix_pkg_test.py:64-70](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L64-L70) —— 三种越界（上溢、有符号下溢、无符号负值）都期望告警；对照 [第 72-75 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L72-L75) 显式传 `Sat_s` 后则**返回钳位值、不告警**。同一输入、不同 `saturate` 参数 → 不同契约，这正是「饱和模式」要覆盖的核心。

> 这个告警的源头在被测实现里：`NarrowFix.from_real` 在 `SatWarn_s`/`Warn_s` 时比较**输入**与格式上下界，越界即 `warnings.warn`；随后无论是否告警都做半进位量化 + 钳位。见 [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:76-94](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L76-L94)。

**例 3：边界用例——舍入诱发的溢出（round 把合法值顶出范围）。** 这是 u4-l3 强调过的关键场景：`7.5` 在 `[1,3,0]`（有符号 4 位整数）里是合法的，但用 `NonSymPos_s`（四舍五入）round 到 `[1,3,0]` 时 `7.5 → 8.0`，而 8.0 超过最大值 7.0。此时回绕与饱和给出截然不同的结果：

[bittrue/tests/python/en_cl_fix_pkg_test.py:169-179](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L169-L179) —— `None_s`（回绕）把 8.0 折成 -8.0；`Sat_s`（饱和）钳到 7.0。这组用例直接证明 resize **必须先 round 后 saturate**：若反过来先饱和，7.5 不超界会被放过，round 后的 8.0 就成了漏网的溢出。

**例 4：边界用例——最负值取反/取绝对值。** 补码的不对称性体现在「最负值没有对应的正值」。`[1,2,2]` 的最负值是 \(-2^2 = -4.0\)，对其取绝对值在原格式里放不下（最大正只有 3.75）：

[bittrue/tests/python/en_cl_fix_pkg_test.py:490-491](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L490-L491) —— `cl_fix_abs(-4.0, [1,2,2], [1,2,2], Trunc_s, Sat_s)` 期望 3.75（饱和到最大正）；同类思想见 `cl_fix_sub_Test` 的 `test_InvertMostNegative_Signed_NoSat`/`Sat`（[第 388-400 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L388-L400)）：`0 - (-16)` 在 `[1,4,0]` 里回绕得 -16、饱和得 15。

#### 4.1.4 代码实践

**实践目标**：把 `en_cl_fix_pkg_test.py` 跑起来，挑一个用例手算验证。

**操作步骤**：

1. 安装依赖：`pip install -r requirements.txt`（numpy + vunit-hdl）。
2. 进入测试目录（注意：本文件的 `sys.path.append` 用的是相对 CWD 的写法，必须在测试目录内运行）：

   ```bash
   cd bittrue/tests/python
   python en_cl_fix_pkg_test.py
   ```

3. 挑 [第 169 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L169) 的 `test_OverflowDueRounding_Signed_Wrap` 手算：`7.5` 在 `[1,3,1]` 中，round 到 `[1,3,0]` 用 `NonSymPos_s` → `8.0`，再以 `None_s` 回绕进 4 位有符号（范围 \([-8,7]\)）→ `-8.0`。

**需要观察的现象**：`unittest` 输出 `OK` 与用例计数（约 130+ 个 test）；若环境未就绪会报 `ModuleNotFoundError: en_cl_fix_pkg`，说明 `sys.path` 没指到 `models/python`。

**预期结果**：全部用例通过。**待本地验证**：具体用例计数以本机运行输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`cl_fix_from_real_Test` 里 `test_OutOfRangeError`（[第 64-70 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L64-L70)）为什么用 `assertWarns` 而不是 `assertRaises`？

**参考答案**：因为 `cl_fix_from_real` 默认 `SatWarn_s`，越界时**不抛异常**，而是发 `Warning` 并把值钳到边界。若它抛异常，就得用 `assertRaises`。

**练习 2**：`cl_fix_from_integer_Test` 的 `test_Wrap_Unsigned`（[第 93-95 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L93-L95)）期望 `ValueError`。结合被测实现，这个异常是从哪里抛出的？

**参考答案**：`NarrowFix.from_integer` 在量化后调用 `in_range` 检查，超范围则 `raise ValueError("...Value not in number format range")`。`from_integer` 不像 `from_real` 那样做饱和，所以越界直接报错（见 [narrow_fix.py:99-108](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L99-L108)）。

---

### 4.2 穷举对拍测试：round / saturate 的三实现逐位比对

#### 4.2.1 概念说明

人工断言式测试覆盖面有限——人不可能为几千种「格式 × 模式」组合各写一行。`cl_fix_round_test.py` 和 `cl_fix_saturate_test.py` 用另一种思想：**穷举某格式全部可能取值，把待测函数与两个独立实现逐位比对，三者完全相等才算通过**。

这里的关键词是「**独立**」。三个实现是：

1. **待测函数本身**（`cl_fix_round` / `cl_fix_saturate`，走 NarrowFix 主路径）；
2. **WideFix 路径**（把 narrow 数据经 `from_narrowfix` 搬到整数域算完再 `to_real`，算法实现完全独立于 float 域）；
3. **本地 numpy 参考**（`round_check` / `sat_check`，用 `np.floor` / `np.ceil` / `np.where` 等通用原语另写一份）。

三者用不同的数据类型（float64 / Python int）、不同的算法（加偏移 floor / 取模回绕 / 比较钳位）得到同一答案，才能互证无误。这是 en_cl_fix 证明「bit-true」的骨架——只不过 cosim（u8）比的是 Python vs VHDL，这里比的是 Python 内部三条独立路径。

#### 4.2.2 核心流程

以 `cl_fix_round_test.py` 为例，其循环结构是「**先生成全部输入数据，再枚举结果格式与模式**」：

```
for aS in {0,1}:                       # 符号位
  for aI in -4..4:                     # 整数位
    for aF in -4..4:                   # 小数位
      若 aS+aI+aF <= 0: 跳过（非法/空格式）
      a_fmt = FixFormat(aS,aI,aF)
      a = get_data(a_fmt)              # 穷举该格式全部取值（见下）
      for rF in -4..4:                 # 结果小数位
        for rnd in 全部 7 种 FixRound:
          r_fmt = FixFormat.for_round(a_fmt, rF, rnd)   # 用保守推导求合法 r_fmt
          r      = cl_fix_round(a, a_fmt, r_fmt, rnd)   # ① 待测
          r_wide = WideFix(...).round(r_fmt, rnd)       # ② WideFix 独立路径
          expected = round_check(a, a_fmt, r_fmt, rnd)  # ③ numpy 参考
          assert r == expected 且 r_wide == expected     # 三者逐位相等
```

`get_data` 是穷举的核心工具：它把某格式的**全部**取值用一个计数器生成——`to_integer(min_value) .. to_integer(max_value)` 的整数区间，再 `from_integer` 还原成定点：

[bittrue/tests/python/cl_fix_round_test.py:42-47](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L42-L47) —— `np.arange(int_min, 1+int_max)` 生成闭区间全部整数（注意右端 +1，因为 `arange` 是左闭右开）。

一个值得注意的细节：`r_fmt` 不是任意取的，而是用 `FixFormat.for_round(a_fmt, rF, rnd)` **保守推导**出来的合法结果格式（[第 116-119 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L116-L119)），并用 `try/except AssertionError` 跳过非法组合。这正好配合被测函数体内的 `assert r_fmt == cl_fix_round_fmt(...)`（见 [en_cl_fix.py:194](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L194)）——测试只喂合法格式，绝不会误触发那条断言。

#### 4.2.3 源码精读

**numpy 参考实现 `round_check`** 把 7 种模式拆成「加偏移 → floor/ceil」的组合，是 u4-l1「舍入 = 加偏移再截断」的直接编码：

[bittrue/tests/python/cl_fix_round_test.py:49-78](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L49-L78) —— `NonSymPos` 加 \(+2^{-(rF+1)}\) 再 floor（四舍五入，平局向上）；`NonSymNeg` 减同量再 ceil（平局向下）；`SymInf` 按符号选二者（远离 0）；`SymZero` 反向（靠近 0）；`ConvEven` 直接用 numpy 的 `np.around`（原生凑偶）；`ConvOdd` 在 `np.around` 基础上偏移 +1 再 -1 实现凑奇。

**三实现比对与计数**，在循环体末尾一次性断言两条相等：

[bittrue/tests/python/cl_fix_round_test.py:122-134](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L122-L134) —— `assert np.array_equal(r, expected)` 验 NarrowFix 路径，`assert np.array_equal(r_wide, expected)` 验 WideFix 路径，两条都过才 `test_count += 1`。注意第 125 行的 WideFix 自检：输入是 narrow 数据，先 `WideFix.from_narrowfix(NarrowFix(a, a_fmt))` 搬到整数域，再 `.round(...).to_real()`，刻意走与主路径不同的表示。

**saturate 测试的结构完全同构**，只是固定 `rF = aF`（饱和不允许小数位变化，见被测函数 [en_cl_fix.py:219](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L219) 的断言），并把参考实现换成处理「回绕 vs 钳位」的 `sat_check`：

[bittrue/tests/python/cl_fix_saturate_test.py:49-72](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L49-L72) —— `None_s`/`Warn_s` 走回绕（用 `offset = 2^(rS+rI)` 反复加减直到落入 \([min_r, max_r]\)，[第 55-65 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L55-L65)）；`Sat_s`/`SatWarn_s` 走钳位（`np.where` 钳到端点，[第 66-70 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L66-L70)）。回绕与饱和数值不同，所以能区分二者——带 `Sat` 的两条数值相等、带 `Warn` 的两条数值相等。

两个脚本结尾都只打印一行计数，没有任何 `assert` 失败信息就算通过：

[bittrue/tests/python/cl_fix_round_test.py:136](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L136) —— `print(f"Completed {test_count} tests.")`。看到这行就代表全部 `assert` 都没触发（否则 Python 会在第一个失败处抛 `AssertionError` 中止）。这与人工断言式测试「输出 OK」的反馈方式不同，要适应。

#### 4.2.4 代码实践

**实践目标**：运行 round 穷举测试，理解它的反馈方式。

**操作步骤**：

1. 这两个脚本的 `sys.path` 用 `dirname(__file__)` 解析（[cl_fix_round_test.py:31-33](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L31-L33)），所以**可从任意目录运行**：

   ```bash
   python bittrue/tests/python/cl_fix_round_test.py
   python bittrue/tests/python/cl_fix_saturate_test.py
   ```

2. 阅读输出末行 `Completed N tests.`，理解 N 是「合法的 `(a_fmt, r_fmt, 模式)` 组合数」。

**需要观察的现象**：终端只打印一行 `Completed ... tests.`，没有 `OK`、没有用例列表——这与 `unittest` 风格的输出截然不同。若任一 `assert` 失败，会看到 `AssertionError: Numerical error detected.` 并中止。

**预期结果**：两个脚本各打印一行 `Completed N tests.`（N 为数千量级）。**待本地验证**：精确 N 值以本机输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么穷举测试里要同时比对 NarrowFix 路径和 WideFix 路径？只比对 numpy 参考不够吗？

**参考答案**：numpy 参考只能证明「主路径 float64 实现与公式一致」，无法发现 float64 自身的精度问题（如 53 位上限附近的回绕误差）。WideFix 用任意精度整数实现，算法与 float 域完全独立；两者一致才能排除「float 域实现碰巧因精度误差也算对了」的假象。

**练习 2**：`cl_fix_saturate_test.py` 为什么在循环里写死 `rF = aF`（[第 110 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_saturate_test.py#L110)）？

**参考答案**：因为 `cl_fix_saturate` 的硬性前提是「小数位不变」（被测函数第 219 行有 `assert r_fmt.F == a_fmt.F`）。若 rF ≠ aF 会直接触发断言中止，所以要测的是「整数位/符号位变化」的饱和行为，小数位必须保持不变。

---

### 4.3 格式最优性验证：format_tests 的「够用 + 不浪费」双断言

#### 4.3.1 概念说明

`format_tests.py` 测的不是数值，而是**结果格式推导函数**（`for_add` / `for_sub` / `for_addsub` / `for_mult` / `for_neg` / `for_abs` / `for_shift`）是否返回「最优」格式。这里「最优」有严格定义，正是 u3 反复强调的 mid_fmt 两标准：

- **够用（sufficient）**：任何合法输入的运算结果都不会超出该格式的表示范围。
- **不浪费（necessary）**：再少一个整数位就放不下——格式没有被过度放宽。

`format_tests.py` 的开头注释点明了这一定义：

[bittrue/tests/python/format_tests.py:20-25](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L20-L25) —— 明确说要检查 `For*` 函数都给出「optimal (sufficient and necessary) formats」。

它的方法不是穷举输入值，而是**穷举输入格式**（\(aS, aI, aF, bS, bI, bF\) 各取 \([-6,6]\) 与 \(\{0,1\}\)），对每种格式组合算出结果的**真实极值** `rmax`/`rmin`，再检查推导出的 `r_fmt` 是否恰好容纳这组极值。

#### 4.3.2 核心流程

以 `cl_fix_add` 为例，每组 `(a_fmt, b_fmt)` 的验证流程：

```
amin, amax = cl_fix_min/max_value(a_fmt)     # a 格式的值域
bmin, bmax = cl_fix_min/max_value(b_fmt)     # b 格式的值域
rmax = amax + bmax;  rmin = amin + bmin      # 加法结果的真实极值（四角点中的最大/最小）
r_fmt = FixFormat.for_add(a_fmt, b_fmt)      # 待测：保守推导的结果格式

# ① 够用：真实极值必须落在 r_fmt 的 [min,max] 内
assert rmax <= cl_fix_max_value(r_fmt)
assert rmin >= cl_fix_min_value(r_fmt)

# ② 不浪费：把 r_fmt 的整数位减 1，就至少有一端装不下
smaller = FixFormat(r_fmt.S, r_fmt.I - 1, r_fmt.F)
assert rmax > cl_fix_max_value(smaller)  or  rmin < cl_fix_min_value(smaller)

# ③ 小数位必须等于 max(a.F, b.F)（加法不产生新的小数粒度）
assert r_fmt.F == max(a_fmt.F, b_fmt.F)
```

三条断言里，**② 是「不浪费」的关键**：它证明推导没有给多余的整数位。少了这一条，一个永远返回 `[1, 1000, 0]` 的错误 `for_add` 也能通过「够用」检查。

一个优雅的技巧是**用极值的「四角点」做 sanity check**。加法结果的最大值一定出现在四个端点组合 \((amin+bmin,\ amin+bmax,\ amax+bmin,\ amax+bmax)\) 之一，所以脚本先断言 `rmax == np.amax([...四点...])`，确保极值算对：

[bittrue/tests/python/format_tests.py:102-106](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L102-L106) —— 先 sanity check 极值取对，再做后续 sufficiency/necessity 判断。乘法因符号影响更大，`rmax`/`rmin` 的取法按符号组合分支（[第 186-203 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L186-L203)），但思路一致。

#### 4.3.3 源码精读

**双断言本体**（`cl_fix_add` 段）：

[bittrue/tests/python/format_tests.py:108-123](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L108-L123) —— 第 109 行调用待测 `for_add`；[第 112-115 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L112-L115) 是「够用」断言（rmax/rmin 落在 r_fmt 范围内）；[第 117-120 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L117-L120) 是「不浪费」断言（整数位减 1 后必有一端越界）；[第 123 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L123) 校验小数位最优。每条 `assert` 都拼接了详细的上下文（`a_fmt/b_fmt/r_fmt/rmax/rmin`），失败时能立刻定位是哪种格式组合出了问题。

**单操作数的 neg / abs / shift** 在 `b_fmt` 循环之外（[第 225-315 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L225-L315)），结构相同，只是极值公式不同——例如 `for_neg` 的 `rmax = -amin`、`rmin = -amax`（取反翻转极值），`for_shift` 还嵌套一层 `(min_shift, shift_range)` 循环，因为移位范围本身就是参数。

注意 `for_mult` 与 `for_shift` 的「不浪费」断言前有一道 `if r_fmt.I + r_fmt.F > 0` 守卫（[第 215 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L215)、[第 309 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L309)）：当结果退化成「几乎空」的格式时，整数位再减可能构造出非法格式，故跳过——这是测边界格式时的必要保护。

> 串讲：`format_tests.py` 验证的是 u3 全部「保守推导」函数的最优性。它和 4.2 的穷举对拍互补——后者证明「数值算得对」，前者证明「格式给得对」。

#### 4.3.4 代码实践

**实践目标**：运行 `format_tests.py`，并人为构造一个反例验证「不浪费」断言的有效性。

**操作步骤**：

1. 运行脚本（路径同样用 `dirname(__file__)`，可从任意目录）：

   ```bash
   python bittrue/tests/python/format_tests.py
   ```

2. 静默即通过——脚本没有任何成功输出，只有 `assert` 失败时才会打印上下文并中止。

3. **思考实验**（不必改源码）：若有人把 `for_add` 错写成「永远多给 1 个整数位」，4.3 的哪条断言会抓住它？答：第 117-120 行的「不浪费」断言——`smaller_fmt` 仍能容纳极值，`assert` 失败，提示 `Format is excessively wide`。

**需要观察的现象**：脚本无输出地结束（退出码 0）；可人为在 `for_add` 里加一位重跑，观察 `AssertionError: ... Format is excessively wide.` 的详细上下文。

**预期结果**：原样运行无任何输出、退出码 0。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `format_tests.py` 用「整数位减 1」而不是「减 1 位小数位」来测「不浪费」？

**参考答案**：因为 `for_*` 推导时小数位有固定法则（加法取 `max(a.F,b.F)`、乘法取 `a.F+b.F`），是最优且确定的；可能「浪费」的是整数位（保守推导按最坏情况给）。所以「不浪费」只需针对整数位检验，小数位另用一条独立断言（如第 123 行）校验等于公式值。

**练习 2**：`for_add` 的「够用」断言里，为什么 `rmax = amax + bmax`、`rmin = amin + bmin` 就够，不必考虑另外两个角点？

**参考答案**：加法单调——两数同时取最大则和最大、同时取最小则和最小，所以四角点中的极值就是 `amax+bmax` 与 `amin+bmin`。脚本第 105-106 行的 `np.amax/amin` sanity check 正是用来确认这一点（对减法、乘法因不单调，极值取法需调整）。

---

### 4.4 辅助测试矩阵：matlab_interface_test 与 narrow/wide 索引测试

#### 4.4.1 概念说明

剩下两个测试覆盖面分别落在「跨语言数据搬运」和「数组索引」上，是 u9-l2 学习目标里点名的窄/宽索引与 MATLAB 接口的归属地。

- **`matlab_interface_test.py`** 验证 wide 定点数据经 `to_uint64_array`（打包成 uint64 分块）→ `from_uint64_array`（解包还原）后**逐位无损**。这是 u9-l1 讲过的 MATLAB 桥接里 uint64 分块通道的往返（round-trip）正确性保障——任何分块/符号位重解释的错误都会在往返后暴露。
- **`cl_fix_Indexing_Test`**（在 `en_cl_fix_pkg_test.py` 内）验证 narrow（float64）与 wide（object int）两种 ndarray 都能像普通 numpy 数组一样被索引、切片、赋值——因为 narrow 是原生 float64 数组天然支持，而 wide 是 object dtype 数组，索引行为需要单独确认。

#### 4.4.2 核心流程

`matlab_interface_test.py` 是**随机往返测试**：随机生成宽格式（位宽 60/65/151，均 > 53 走 wide）与随机形状，生成数据 → 打包 → 解包 → 断言与原数据完全相等：

```
for trial in 0..9:
  for bit_width in {60, 65, 151}:        # 三档宽位宽
    随机选 S, I, F (满足 S+I+F = bit_width)
    for array_dims in 1..5:              # 1~5 维
      随机生成形状 shape
      in_data  = cl_fix_random(shape, fmt)
      packed   = to_uint64_array(in_data, fmt)    # → MATLAB 方向
      unpacked = from_uint64_array(packed, fmt)   # ← MATLAB 方向
      assert in_data == unpacked                  # 往返无损
```

`cl_fix_Indexing_Test` 则用 `RunGetTest` / `RunSetTest` 两个辅助方法，对 1D/2D 的标量索引、切片索引（get 与 set）逐一断言，再用 `test_Narrow_Indexing` 和 `test_Wide_Indexing` 分别在窄格式 `[1,5,5]`（宽 11）与宽格式 `[1,50,50]`（宽 101）上跑同一套检查。

#### 4.4.3 源码精读

**uint64 往返测试**核心三行：

[bittrue/tests/python/matlab_interface_test.py:65-74](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/matlab_interface_test.py#L65-L74) —— `cl_fix_random` 生成全范围随机数据（[第 65 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/matlab_interface_test.py#L65)），`to_uint64_array` 打包（[第 68 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/matlab_interface_test.py#L68)），`from_uint64_array` 解包（[第 71 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/matlab_interface_test.py#L71)），`assert np.array_equal(in_data, unpacked)` 断言往返无损（[第 74 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/matlab_interface_test.py#L74)）。位宽 151 需要 ⌈151/64⌉ = 3 个 uint64 分块，能覆盖多分块路径。这两个函数实际定义在 `matlab_interface.py`（见 [matlab_interface.py:6](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L6) 与 [第 40 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L40)），是对 `WideFix` 同名方法的薄包装。

**narrow/wide 索引测试**先断言格式确实落在期望的表示路径上，再跑通用索引检查：

[bittrue/tests/python/en_cl_fix_pkg_test.py:743-753](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L743-L753) —— `test_Narrow_Indexing` 用 `assertFalse(cl_fix_is_wide(...))` 确认走 narrow（float64），`test_Wide_Indexing` 用 `assertTrue(...)` 确认走 wide（object int）。两路用**同一份** `RunGetTest`/`RunSetTest`（[第 698-741 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L698-L741)），证明两种 dtype 的数组索引语义一致——这是「唯一表示约定」（u6-l3）在数组层面的体现。

#### 4.4.4 代码实践

**实践目标**：运行 `matlab_interface_test.py`，观察随机往返测试的输出风格。

**操作步骤**：

```bash
python bittrue/tests/python/matlab_interface_test.py
```

阅读它逐步打印的 trial / format / shape 信息（脚本有大量 `print`），末行应为 `Success: All tests passed.`

**需要观察的现象**：与穷举测试的「静默或一行」不同，本脚本打印详细进度；位宽 151 的格式会触发 3 段 uint64 分块。

**预期结果**：末行打印 `Success: All tests passed.`。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`matlab_interface_test.py` 为什么固定把位宽选成 60/65/151（[第 41 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/matlab_interface_test.py#L41)），而不是也测 8 位、16 位？

**参考答案**：因为该脚本专测 **uint64 分块通道**，只在 wide（> 53 位）路径生效。8/16 位走 narrow（float64），不经 `to/from_uint64_array`，与本测试目的无关。60 刚跨过 53（1 段 uint64），65 跨段边界（2 段），151 需 3 段——三档覆盖了 1/2/3 段的分块逻辑。

**练习 2**：`cl_fix_Indexing_Test` 为什么 narrow 和 wide 用同一套 `RunGetTest`/`RunSetTest`，而不各写一份？

**参考答案**：因为「唯一表示约定」要求两种 dtype 在外部行为（索引、切片、赋值）上完全一致；共用一套检查正是为了施加同一份契约。若 wide 的 object 数组在某类索引上行为异常，同一套断言会立刻抓住。

---

## 5. 综合实践

把本讲四类测试思想串起来，完成下面的任务（贯穿「读测试 → 算期望 → 补用例 → 验证」）：

**任务**：为 `cl_fix_from_real` 补一个**尚未显式覆盖**的格式组合——带**负整数位**的有符号格式 `[1,-2,4]`（即 `FixFormat(True, -2, 4)`），写一个 `unittest` 用例并运行通过。

> 现有 `cl_fix_from_real_Test`（[第 58-79 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L58-L79)）只测了 `[0,2,2]` 与 `[1,2,2]`，从未测过负整数位格式，这是一个真实的覆盖空白。

**步骤 1——先算格式性质**（手算，复习 u1-l2/u2-l1）：

- 位宽 \(= S+I+F = 1+(-2)+4 = 3\)（3 位有符号，走 narrow）。
- 最小值 \(= -2^I = -2^{-2} = -0.25\)；最大值 \(= 2^I - 2^{-F} = 0.25 - 0.0625 = 0.1875\)。
- 量化粒度 \(= 2^{-F} = 0.0625\)，共 \(2^3 = 8\) 个取值。

**步骤 2——按源码逻辑算期望值**。`cl_fix_from_real` 默认 `SatWarn_s`，量化公式为（见 [narrow_fix.py:86](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L86)）：

\[ \text{quantize}(a) = \frac{\lfloor a \cdot 2^{F} + 0.5 \rfloor}{2^{F}} \quad \text{再饱和到}\ [-0.25,\ 0.1875] \]

- 范围内 \(a=0.1\)：\(\lfloor 0.1 \times 16 + 0.5 \rfloor / 16 = \lfloor 2.1 \rfloor / 16 = 2/16 = 0.125\)（未越界，无告警）。
- 范围内 \(a=-0.1\)：\(\lfloor -1.6 + 0.5 \rfloor / 16 = \lfloor -1.1 \rfloor / 16 = -2/16 = -0.125\)（未越界，无告警）。
- 越界 \(a=1.0\)：量化得 \(16/16 = 1.0\)，超过 max 0.1875 → 饱和到 0.1875；又因输入 \(1.0 > 0.1875\) 触发告警（见 [narrow_fix.py:79-80](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L79-L80)）。

**步骤 3——补用例**。在 `en_cl_fix_pkg_test.py` 的 `cl_fix_from_real_Test` 类里加两个方法（示例代码，非项目原有）：

```python
# 示例代码：新增用例，覆盖带负整数位的格式 [1,-2,4]
def test_NegativeIntFmt_InRange(self):
    # [1,-2,4]: 3 位有符号, 范围 [-0.25, 0.1875], 粒度 0.0625
    self.assertEqual(0.125, cl_fix_from_real(0.1, FixFormat(True, -2, 4)))
    self.assertEqual(-0.125, cl_fix_from_real(-0.1, FixFormat(True, -2, 4)))

def test_NegativeIntFmt_OutOfRange_SatWarn(self):
    with self.assertWarns(Warning):
        self.assertEqual(0.1875, cl_fix_from_real(1.0, FixFormat(True, -2, 4)))
```

**步骤 4——运行验证**：

```bash
cd bittrue/tests/python
python en_cl_fix_pkg_test.py
```

**需要观察的现象与预期结果**：`unittest` 报告新增的两个用例通过、整体 `OK`；上方的期望值（0.125 / -0.125 / 0.1875）应与实际一致。若 `0.125` 类断言失败，说明你对半进位量化的手算有误——回头核对步骤 2 的 floor 运算。**待本地验证**：以本机实际运行结果为最终准绳。

> 这个综合实践同时用到了：读已有测试找覆盖空白（4.1）、按源码逻辑独立算期望值（4.2 的「独立参考」思想）、以及 `assertWarns` 断言越界告警（4.1.3 例 2）。

## 6. 本讲小结

- `bittrue/tests/python/` 下有**两种测试风格**：人工断言式（`en_cl_fix_pkg_test.py`，一个函数一个测试类、逐条 `assertEqual`）与穷举对拍式（`cl_fix_round_test.py` / `cl_fix_saturate_test.py`，三重循环枚举全部组合）。
- 穷举对拍用**三条独立实现**互证：待测主路径、WideFix 整数域路径、本地 numpy 参考；三者逐位相等才记一次通过，反馈是单行 `Completed N tests.`。
- `format_tests.py` 用**「够用 + 不浪费」双断言**验证结果格式推导的最优性——够用保证不溢出，不浪费（整数位减 1 即越界）保证无多余位宽。
- 边界用例集中在：舍入诱发溢出（必须先 round 后 saturate）、补码最负值取反/取绝对值、负整数位/负小数位格式。
- 跨语言与索引由 `matlab_interface_test.py`（uint64 分块往返无损）与 `cl_fix_Indexing_Test`（narrow/wide 同一套索引契约）覆盖。
- 两类脚本的运行目录依赖不同：`en_cl_fix_pkg_test.py` 用相对 `sys.path`、须在测试目录内运行；其余三个用 `dirname(__file__)`、可从任意目录运行。

## 7. 下一步学习建议

本讲把 Python 侧的测试体系讲完了。建议接下来：

1. **横向对照 cosim（u8-l1 ~ u8-l3）**：穷举对拍里「三条独立实现」的思想，在 cosim 里升级为「Python 黄金参考 vs VHDL 逐位对拍」，可体会从「语言内三路互证」到「跨语言 bit-true」的延伸。
2. **纵向深读被测实现**：若对 4.2 里 WideFix 整数域 round/saturate 的「独立算法」感兴趣，回到 `wide_fix.py` 的 `round` / `saturate` 方法，对照本讲的 `round_check` / `sat_check` 参考，看整数域如何用「加偏移再右移」「取模回绕」实现同一语义。
3. **动手扩展测试**：以综合实践为模板，继续为 `cl_fix_mult` 的混合符号组合或 `cl_fix_shift` 的极端移位补人工用例，并尝试把某个人工用例改写成穷举对拍形式，体会两种风格的取舍。
