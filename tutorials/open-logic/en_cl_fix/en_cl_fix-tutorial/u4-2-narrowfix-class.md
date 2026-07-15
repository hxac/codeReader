# NarrowFix：基于双精度浮点的 ≤53 位实现

## 1. 本讲目标

学完本讲后，你应该能够：

- 解释为什么 `NarrowFix` 以 **53 位**作为 narrow 与 wide 的分界线，并能从 IEEE 754 双精度浮点的结构推导出来。
- 看懂 `NarrowFix.round` 如何用「归一化浮点 + 加偏移 + 截断」实现全部七种舍入模式，并理解它与 VHDL `cl_fix_round` 在数学上完全等价。
- 理解 `NarrowFix.saturate` 在 wrap（回绕）模式下如何做模运算，以及它为何在某些情况下会**临时降级到任意精度整数运算**来避免浮点精度损失。
- 掌握 `NarrowFix` 的运算符重载（`__add__`、`__mul__` 等）与「全精度中间格式 → resize」三段式算术模板。

本讲承接 [u4-l1](u4-l1-python-main-interface.md)：你已经知道主接口 `cl_fix_*` 用 `cl_fix_is_wide` 以 53 位为界，把内部计算分发到 `NarrowFix`（快、限宽）或 `WideFix`（慢、任意精度）。本讲深入 `NarrowFix` 的内部实现。

## 2. 前置知识

- **IEEE 754 双精度浮点（float64）**：由 1 位符号、11 位指数、52 位显式小数位，外加 1 位「隐含的整数 1」组成，合计 **53 位有效位（significand）**。一个推论是：区间 \([-2^{53},\,2^{53}]\) 内的整数都能被 float64 精确表示，超出这个范围就会出现「相邻浮点之间有缝隙」。
- **归一化（normalized）存储**：`NarrowFix` 把定点数的**真实数值**直接存进 float64。例如定点数 `1.25`（格式 `[0,2,4]`）在 `NarrowFix` 里就是浮点 `1.25`；而 `WideFix` 则存它的「未归一化整数」`1.25 \times 2^{4} = 20`。这是两类实现最本质的差别。
- **舍入即「加偏移再截断」**：所有舍入模式都可以写成 \(\mathrm{round}(x)=\mathrm{trunc}(x+\mathrm{offset})\)，不同模式只是 `offset` 不同。这一思想在 [u2-l2](u2-l2-rounding-modes.md) 已经建立，本讲看它如何落地到浮点。
- **resize = round ⟶ saturate**：来自 [u4-l1](u4-l1-python-main-interface.md) 的核心范式，顺序不可交换。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | 本讲主角：`NarrowFix` 类的全部实现 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | 主接口，用 `cl_fix_is_wide` 决定是否调用 `NarrowFix` |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | `FixFormat`（含 `for_round`、`for_add` 等格式预测）与枚举定义 |
| [bittrue/models/python/en_cl_fix_pkg/wide_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py) | 对照组：任意精度整数实现，也是 wrap 降级时的计算方式 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | VHDL 金标准，用于在实践里对照验证 `round` 的等价性 |

## 4. 核心概念与源码讲解

### 4.1 NarrowFix 的存储模型与 MAX_WIDTH=53 的由来

#### 4.1.1 概念说明

`NarrowFix` 是 en_cl_fix 的「快速路径」：它假设定点位宽不超过 53 位，于是可以直接借用 CPU 原生的 float64 运算（加减乘都是单条指令级别），比用 Python 任意精度整数的 `WideFix` 快得多。代价是它**不能**处理超过 53 位的格式——构造时会直接断言失败。

关键设计决定：**数据以归一化形式存储**。即 `_data` 里放的就是定点数的真实浮点值，小数点位置由 `_fmt.F` 隐含记录，而不像 `WideFix` 那样把数据左移成整数。这让加减乘可以直接对浮点做，无需先「对齐小数点」。

#### 4.1.2 核心流程

构造一个 `NarrowFix` 对象时：

1. 校验格式宽度不超过 53 位（否则断言失败，提示改用 `WideFix`）。
2. 校验数据类型是 `float64`。
3. 按需拷贝数据，并总是浅拷贝一份格式对象。

#### 4.1.3 源码精读

`MAX_WIDTH` 的推导写在一段很清晰的注释里，是本讲最重要的「为什么是 53」：

[narrow_fix.py:40-52](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L40-L52) —— IEEE 754 双精度有 53 位有效位（52 显式 + 1 隐含），区间 \([-2^{53},\,2^{53}]\) 内整数可精确表示，理论上够放 54 位有符号 / 53 位无符号数；但为了在有符号数做 wrap 时更简单，**额外预留 1 个整数位**，于是对有符号和无符号统一取 53 位上限。

构造函数里的两条断言把上述约束落实成代码：

[narrow_fix.py:54-65](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L54-L65) —— 断言 `fmt.width <= NarrowFix.MAX_WIDTH` 与数据必须是 `float64`；`_fmt` 用 `shallow_copy` 复制以避免外部修改波及内部。

这个 53 上限正是主接口分发的依据：

[en_cl_fix.py:79-84](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84) —— `cl_fix_is_wide(fmt)` 就是 `cl_fix_width(fmt) > NarrowFix.MAX_WIDTH`，即 `width > 53`。整个 `cl_fix_*` 体系都靠它决定走 narrow 还是 wide。

#### 4.1.4 代码实践

**实践目标**：用 `FixFormat.width` 验证 narrow/wide 的边界，并亲手触发 `NarrowFix` 的宽度断言。

**操作步骤**（在仓库根目录，已 `pip install -r requirements.txt`）：

```python
import sys; sys.path.insert(0, "bittrue/models/python")
import numpy as np
from en_cl_fix_pkg import NarrowFix, FixFormat, cl_fix_is_wide

f_narrow = FixFormat(1, 30, 22)   # width = 1+30+22 = 53
f_wide   = FixFormat(1, 31, 22)   # width = 1+31+22 = 54
print(f_narrow.width, cl_fix_is_wide(f_narrow))   # 预期 53, False
print(f_wide.width,   cl_fix_is_wide(f_wide))     # 预期 54, True

NarrowFix(np.array([1.0]), f_wide)                # 预期触发 AssertionError
```

**需要观察的现象**：前两行打印 `53 False` 与 `54 True`；最后一行抛出 `AssertionError: NarrowFix: Requested format is too wide. Use WideFix.`。

**预期结果**：53 位格式可正常构造 `NarrowFix`，54 位格式在构造时即被拒绝——这就是「快速路径」的安全护栏。若本地环境未装 numpy，打印步骤标为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么理论上 float64 能精确表示 54 位有符号整数，`NarrowFix` 却把上限设成 53？

**答案**：注释里说明了取舍——为了让有符号数在 wrap（回绕）时的中间计算（`data + 2**r.I`）不必担心溢出，额外预留 1 个整数位，从而对有符号和无符号统一用同一个 53 位上限，简化实现、避免分支。详见 4.1.3 的源码链接。

---

### 4.2 round：浮点偏移实现七种舍入

#### 4.2.1 概念说明

`NarrowFix.round` 的任务是在**减少小数位**（`r_fmt.F < fmt.F`）时，按指定舍入模式丢掉低位。由于数据已经是归一化浮点，实现非常直接：先把每个模式对应的实数偏移加到数据上，再用 `floor` 截断到目标小数位。这正是 [u2-l2](u2-l2-rounding-modes.md) 讲的「平局（tie）处理决定模式差异」的浮点落地。

#### 4.2.2 核心流程

记输入小数位为 \(F_a\)、结果小数位为 \(F_r\)，结果最低位（LSB）权重为 \(2^{-F_r}\)，输入 LSB 权重为 \(2^{-F_a}\)。

\[
\mathrm{round}(x) \;=\; \mathrm{trunc}_{F_r}\!\bigl(x + \mathrm{offset}(x)\bigr),\qquad
\mathrm{trunc}_{F_r}(y)=\frac{\lfloor y\cdot 2^{F_r}\rfloor}{2^{F_r}}
\]

各模式的 `offset`（仅当 \(F_r<F_a\) 时才加）：

| 模式 | 偏移（实数值） | 平局行为 |
|---|---|---|
| `Trunc_s` | \(0\) | 直接截断（朝 \(-\infty\)） |
| `NonSymPos_s` | \(2^{-F_r-1}\) | 平局朝 \(+\infty\)（最常用） |
| `NonSymNeg_s` | \(2^{-F_r-1}-2^{-F_a}\) | 平局朝 \(-\infty\) |
| `SymInf_s` | \(2^{-F_r-1}-2^{-F_a}\!\cdot\![x<0]\) | 平局远离 0 |
| `SymZero_s` | \(2^{-F_r-1}-2^{-F_a}\!\cdot\![x\ge 0]\) | 平局朝 0 |
| `ConvEven_s` | \(2^{-F_r-1}-2^{-F_a}\!\cdot\![(\lfloor x\,2^{F_r}\rfloor+1)\bmod 2]\) | 平局凑偶 |
| `ConvOdd_s` | \(2^{-F_r-1}-2^{-F_a}\!\cdot\![\lfloor x\,2^{F_r}\rfloor\bmod 2]\) | 平局凑奇 |

其中 \([\,\cdot\,]\) 是指示函数（条件成立为 1，否则为 0）。最后统一用 `np.floor(data * 2**F_r) * 2**-F_r` 截断。

#### 4.2.3 源码精读

[narrow_fix.py:157-188](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L157-L188) —— `round` 全体：开头断言「结果格式必须等于 `FixFormat.for_round(...)`」（格式契约，与 VHDL 一致）；中段按 `rnd` 分支加偏移；末尾 `np.floor(data * 2.0 ** r_fmt.F) * 2.0 ** -r_fmt.F` 截断。

值得逐行对照的是偏移分支 [narrow_fix.py:167-186](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L167-L186)：
- `NonSymPos_s`：`data + 2.0 ** (-r_fmt.F - 1)` —— 加半个结果 LSB。
- `NonSymNeg_s`：`data + 2.0 ** (-r_fmt.F - 1) - 2.0 ** -fmt.F` —— 半个结果 LSB 再扣掉一个**输入** LSB。
- `SymInf_s` / `SymZero_s`：在半个结果 LSB 基础上，用 `(data < 0)` / `(data >= 0)` 的 0/1 做微调。
- `ConvEven_s` / `ConvOdd_s`：用 `np.floor(data * 2 ** r_fmt.F)` 取「当前会落在哪个结果整数」，再 `% 2` 判断奇偶决定微调量。

**与 VHDL 的等价性**。VHDL `cl_fix_round` 也是「加偏移再截断」，但它先构造一个中间格式 `mid_fmt`：

[hdl/en_cl_fix_pkg.vhd:925-929](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L925-L929) —— `mid_fmt_c.F = maximum(result_fmt.F+1, a_fmt.F)`。

关键观察：偏移分支只在 `result_fmt.F < a_fmt.F` 时进入（[hdl/en_cl_fix_pkg.vhd:948](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L948)），此时 \(F_r+1 \le F_a\)，于是 \(\mathrm{mid\_fmt}.F = F_a\)。也就是说**在加偏移的分支里，mid 的 1 个单位恰好等于输入 LSB** \(2^{-F_a}\)。VHDL 里的 `half_c-1`、`half_c - sign_c` 等表达式中的「1」就对应 \(2^{-F_a}\)，与 `NarrowFix` 里减去的 `2.0 ** -fmt.F` 数值完全相同。VHDL 的 case 分支见 [hdl/en_cl_fix_pkg.vhd:953-969](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L953-L969)，截断见 [hdl/en_cl_fix_pkg.vhd:973](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L973)。

结论：**两套实现的偏移数值逐位一致，因此对任意合法输入给出完全相同的结果**。`NarrowFix` 只是把 VHDL「显式 mid_fmt + 整数偏移」简化成了「归一化浮点 + 实数偏移」，省掉了显式的二进制点位对齐。

#### 4.2.4 代码实践

**实践目标**：用一批含平局的值，验证 `NarrowFix.round` 的七种模式表现，并确认它与「加偏移再截断」的公式一致。

**操作步骤**：

```python
import sys; sys.path.insert(0, "bittrue/models/python")
import numpy as np
from en_cl_fix_pkg import NarrowFix, FixFormat, FixRound

a = NarrowFix(np.array([-1.5, -0.5, 0.5, 1.5]), FixFormat(0, 4, 4))   # 全是平局值
for rnd in [FixRound.NonSymPos_s, FixRound.NonSymNeg_s,
            FixRound.SymInf_s, FixRound.SymZero_s,
            FixRound.ConvEven_s, FixRound.ConvOdd_s]:
    rf = FixFormat.for_round(FixFormat(0, 4, 4), 1, rnd)   # F: 4 -> 1
    print(str(rnd), a.round(rf, rnd)._data)
```

**需要观察的现象**：同一组输入 `[-1.5, -0.5, 0.5, 1.5]` 在不同模式下，「平局」元素（`.5` 结尾）会朝不同方向落，例如 `NonSymPos_s` 全部朝 \(+\infty\)（`-1.5→-1, -0.5→0, 0.5→1, 1.5→2`），`NonSymNeg_s` 全部朝 \(-\infty\)，`ConvEven_s` 凑到最近的偶数（`0.5→0, 1.5→2`）。

**预期结果**：打印结果与上表「平局行为」列逐一吻合。具体数值待本地验证，但方向性可由公式直接推出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `NonSymNeg_s` 的偏移是「半个结果 LSB 减去一个**输入** LSB」，而不是「减去半个输入 LSB」？

**答案**：要在平局（被丢弃部分恰等于半个结果 LSB）时把结果往下压一档，只需在「半」的基础上少加一个最小的可分辨量。输入 LSB \(2^{-F_a}\) 正是当前数据能分辨的最小单位（且 \(F_a>F_r\)，所以它比半个结果 LSB 更小），减掉它就能让平局向下取整，而非精确落在两数正中间导致行为不确定。

**练习 2**：`ConvEven_s` 里 `(... + 1) % 2` 起什么作用？

**答案**：`\lfloor x\,2^{F_r}\rfloor` 是「若不处理平局，x 会落入的结果整数」；`(n+1)%2` 判断该整数的**奇偶性取反**——当它是奇数时 `(n+1)%2==0`（不扣，等于半向上凑到偶），当它是偶数时为 1（扣一个输入 LSB，凑到更低的偶）。这正是「平局凑偶」的判定。

---

### 4.3 saturate：wrap 模运算与 wide 降级判断

#### 4.3.1 概念说明

`saturate` 处理整数位/符号位被压缩时的越界。它有两条截然不同的路径：

- **钳位（Sat/SatWarn）**：把超出范围的值钉在 `max`/`min`，浮点 `np.where` 直接比较即可，没有精度问题。
- **回绕（None/Warn，即 wrap）**：丢弃高位，相当于**模运算**。问题在于：有符号数的回绕公式需要先算 `data + 2**r.I`，这个中间值可能比原数据多 1 位；如果原格式已经接近 53 位上限，中间值就无法被 float64 精确表示。此时 `NarrowFix` 会**临时切换到任意精度整数运算**（与 `WideFix` 同款思路）来保证精度，算完再转回浮点。这就是本讲的另一个核心：**narrow 内部也可能临时「降级」到 wide 计算**。

#### 4.3.2 核心流程

前置约束：`assert r_fmt.F == fmt.F`（饱和不改小数位，这与 resize「先 round 后 saturate」的顺序一致）。

回绕（wrap）的数学定义（\(M\) 为目标格式可表示值的个数）：

\[
\text{有符号：}\quad \mathrm{wrap}(x)=\bigl((x+2^{I_r}) \bmod 2^{I_r+1}\bigr)-2^{I_r}
\]

\[
\text{无符号：}\quad \mathrm{wrap}(x)=x \bmod 2^{I_r}
\]

有符号公式先把值平移到 \([0,\,2^{I_r+1})\) 再取模，最后平移回 \([-2^{I_r},\,2^{I_r})\)。难点是中间项 \(x+2^{I_r}\) 的精度。

降级判定（仅对有符号 `r_fmt`）：
1. 构造一个表示常量 \(2^{I_r}\) 的格式 `offset_fmt`（`I>=0` 时用 `(0, I+1, 0)`；`I<0` 时用 `(0, I+1, -I)` 保证至少 1 位宽）。
2. 用 `FixFormat.for_add(fmt, offset_fmt)` 算出「`data + 2**r.I`」所需的最小格式 `add_fmt`。
3. 若 `add_fmt.width > 53` → 走 wide 整数路径；否则走 float64 路径。

#### 4.3.3 源码精读

整个 `saturate` 见 [narrow_fix.py:190-244](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L190-L244)。

小数位不变约束与告警：

[narrow_fix.py:197-204](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L197-L204) —— `assert r_fmt.F == fmt.F`；`Warn`/`SatWarn` 下若有越界则发警告。

**降级判定**是本模块最精巧的部分：

[narrow_fix.py:207-221](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L207-L221) —— 对有符号 `r_fmt`，构造 `offset_fmt` 并用 `for_add` 预测 `data + 2**r.I` 的格式宽度；超过 53 即 `convert_to_wide = True`。无符号 wrap 只做取模、不涉及加法扩位，因此恒为 `False`。

**wide 整数路径**（用 numpy `object` dtype 承载任意精度整数，与 `WideFix` 内部表示一致）：

[narrow_fix.py:223-232](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L223-L232) —— 先 `np.floor(data.astype(object) * 2**r.F)` 把归一化浮点还原成未归一化整数；`span = 2**(r.I+r.F)`；有符号 `((data+span) % (2*span)) - span`，无符号 `data % span`；最后 `/ 2**r.F` 转回浮点。注释明确说明「在 WideFix（整数）里做中间计算以避免精度损失」。

**float64 路径**（格式够窄、不会丢精度时）：

[narrow_fix.py:233-238](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L233-L238) —— 同一套模运算公式，但全程在 float64 里算，更快。

**钳位路径**：

[narrow_fix.py:239-242](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L239-L242) —— `Sat`/`SatWarn` 用两次 `np.where` 把越界值钉到 `fmt_max`/`fmt_min`。

注意：这里的 wide 路径**并未调用 `WideFix` 类**，而是就地用 `object` dtype 数组做整数模运算——思想与 `WideFix.saturate` 完全一致（可对照 [wide_fix.py:288-294](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L288-L294) 的同款公式），但为了避免对象构造开销而内联实现。

#### 4.3.4 代码实践

**实践目标**：构造一个宽度恰为 53 的有符号格式，证明对它做 wrap 饱和会触发 wide 降级，并验证降级后的结果与真正的 `WideFix` 一致。

**操作步骤**：

```python
import sys; sys.path.insert(0, "bittrue/models/python")
import numpy as np
from en_cl_fix_pkg import NarrowFix, WideFix, FixFormat, FixSaturate

a_fmt = FixFormat(1, 30, 22)   # width = 53, 恰好 narrow 上限
r_fmt = FixFormat(1, 29, 22)   # 压缩 1 个整数位
# 手动复现降级判定
offset_fmt = FixFormat(0, r_fmt.I + 1, 0)
add_fmt = FixFormat.for_add(a_fmt, offset_fmt)
print("add_fmt =", add_fmt, " width =", add_fmt.width,
      " convert_to_wide =", add_fmt.width > NarrowFix.MAX_WIDTH)

# 用一个超出 r_fmt 范围的值，触发 wrap
a = NarrowFix(np.array([2.0**29 - 2.0**-22, -(2.0**29)]), a_fmt)
rn = a.saturate(r_fmt, FixSaturate.None_s)             # NarrowFix 路径
w  = WideFix.from_narrowfix(a).saturate(r_fmt, FixSaturate.None_s)  # WideFix 金标准
print("narrow wrap =", rn._data)
print("wide   wrap =", w.to_real(warn=False))
print("match =", np.allclose(rn._data, w.to_real(warn=False)))
```

**需要观察的现象**：`add_fmt` 应为 `FixFormat(1, 31, 22)`，宽度 `54 > 53`，因此 `convert_to_wide = True`——证明降级路径被触发。`NarrowFix.saturate` 与 `WideFix.saturate` 的输出应当完全一致（`match = True`），说明降级计算保持了精度。

**预期结果**：`add_fmt.width = 54`、降级为真、两条路径结果一致。具体回绕数值待本地验证（取决于输入是否真越界）；可自行把输入换成明显越界值（如 `2**30`）观察回绕效果。另外可对照实验：把 `r_fmt` 设为无符号（如 `FixFormat(0, 29, 22)`），此时降级判定恒为 `False`，全程走 float64 路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么无符号 wrap 永远不需要降级到 wide？

**答案**：无符号 wrap 是 `data % 2**r.I`，只做取模、不做加法，不会产生比原数据更宽的中间值；而原数据本身已是 ≤53 位的 narrow 格式，float64 足以精确表示，故无需降级。只有有符号 wrap 的 `data + 2**r.I` 才可能扩位到 54 位。

**练习 2**：`r_fmt.I < 0` 时，`offset_fmt` 为什么要写成 `FixFormat(0, r.I+1, -r.I)` 而不是 `FixFormat(0, r.I+1, 0)`？

**答案**：当 `r.I < 0` 时 `(0, r.I+1, 0)` 的宽度 `r.I+1 ≤ 0`，是非法/无意义的格式。引入 `F = -r.I` 个小数位后宽度变为 `(r.I+1) + (-r.I) = 1`，正好用 1 位表示常量 \(2^{I_r}\)（其权重为 \(2^{-(-r.I)}\cdot\text{?}\)，落在该 1 位上），保证格式合法且能精确承载这个偏移常量。注释「we increase frac bits to guarantee at least 1 bit in the format」即此意。

---

### 4.4 运算符重载与算术三段式

#### 4.4.1 概念说明

`NarrowFix` 重载了 Python 的算术与比较运算符，使两个 `NarrowFix` 对象可以直接写 `a + b`、`a * b`、`-a`、`a << n`，语义与主接口 `cl_fix_add`/`cl_fix_mult` 完全一致。所有算术方法都遵循 [u4-l1](u4-l1-python-main-interface.md) 讲过的三段式：**预测全精度中间格式 `mid_fmt` → 在 `mid_fmt` 下做无损运算 → `resize` 收敛到目标格式**。由于数据是归一化浮点，加减乘可以直接对 `_data` 做，无需像 `WideFix` 那样先对齐小数点——这正是 narrow 更快的根本原因。

#### 4.4.2 核心流程

以 `add` 为例：

1. `mid_fmt = FixFormat.for_add(self._fmt, b._fmt)` 算出全精度结果格式。
2. 若调用者未指定 `r_fmt`，则 `r_fmt = mid_fmt`（返回无损全精度结果）。
3. `NarrowFix(self._data + b._data, mid_fmt)` 直接做浮点加法（两数小数位相同才能直接相加——而 `for_add` 保证 `mid_fmt.F = max(a.F, b.F)`，构造前数据已在各自格式下归一化，加法在浮点域天然对齐）。
4. `.resize(r_fmt, rnd, sat)` 收敛。

`mult`、`sub`、`neg`、`abs`、`shift` 同构，只是 `mid_fmt` 分别来自 `for_mult`/`for_sub`/`for_neg`/`for_abs`/`for_shift`。

#### 4.4.3 源码精读

算术方法集中在一处，以 `add` 与 `mult` 为代表：

[narrow_fix.py:279-288](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L279-L288) —— `add`：`for_add` 算 `mid_fmt`，`self._data + b._data` 直接相加，`resize` 收敛。

[narrow_fix.py:317-326](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L317-L326) —— `mult`：`for_mult` 算 `mid_fmt`，`self._data * b._data` 直接相乘，`resize` 收敛。注意 `mid_fmt.F = a.F + b.F`，乘积的小数位是两者之和，但归一化浮点乘法自动处理了这一点，无需手动移位。

运算符重载把它们接到 Python 语法糖上：

[narrow_fix.py:343-360](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L343-L360) —— `__add__`→`add`、`__sub__`→`sub`、`__neg__`→`neg`、`__mul__`→`mult`、`__lshift__`→`shift`。于是 `a + b`、`a * b`、`a << 4` 都能直接用。

比较运算符 [narrow_fix.py:362-390](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L362-L390) 直接比较 `_data` 浮点（并断言另一方也是 `NarrowFix`）。

> 顺带一个源码阅读发现（不影响学习主线，留作严谨性练习）：`__len__` 在 [narrow_fix.py:392-395](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L392-L395) 里写了一句 `assert isinstance(other, NarrowFix)`，但 `__len__` 的形参里并没有 `other`。若真被 `len(x)` 调用，会先抛 `NameError: name 'other' is not defined`。这看起来是从比较运算符复制过来后漏改的遗留缺陷——可作为「读源码时要带着质疑」的一个真实例子。

#### 4.4.4 代码实践

**实践目标**：用运算符语法完成一次「乘法 → 舍入 → 饱和」链路，体会三段式与 `r_fmt=None`（全精度）的区别。

**操作步骤**：

```python
import sys; sys.path.insert(0, "bittrue/models/python")
import numpy as np
from en_cl_fix_pkg import NarrowFix, FixFormat, FixRound, FixSaturate

a = NarrowFix(np.array([1.5, -0.75]), FixFormat(1, 4, 8))
b = NarrowFix(np.array([2.0,  0.5 ]), FixFormat(0, 4, 8))

mid = a * b                                   # r_fmt=None -> 全精度
print("mid fmt F =", mid.fmt.F)               # 预期 16 = 8+8

# 收敛到一个更窄的有符号格式，带舍入与饱和
rf = FixFormat(1, 5, 4)
out = (a * b).resize(rf, FixRound.NonSymPos_s, FixSaturate.SatWarn_s)
print(out._data)
```

**需要观察的现象**：`mid.fmt.F` 应为 `16`（两输入各 8 位小数相加），说明 `r_fmt=None` 时确实返回全精度中间结果；显式 `resize` 到 `[1,5,4]` 后，小数位被舍入、整数位被饱和收敛。

**预期结果**：`mid.fmt.F = 16`；最终 `out` 为收敛后的浮点数组。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `NarrowFix.add` 可以直接 `self._data + b._data`，而 `WideFix.add` 必须先「对齐小数点」？

**答案**：`NarrowFix` 存的是归一化浮点（真实数值），浮点加法天然按数值相加，小数点「对齐」由浮点硬件完成；只要两个操作数的格式都满足 `mid_fmt`（其 `F = max(a.F, b.F)`），加法即正确。`WideFix` 存的是未归一化整数，两个不同 `F` 的整数相加前必须先把它们移位到同一小数点位（即 `WideFix.add` 里的 `a_round`/`b_round` 对齐步骤），否则会错位。这也是 narrow 更快的原因之一。

**练习 2**：`a * b` 不指定 `r_fmt` 时，结果的小数位为什么是 `a.F + b.F`？

**答案**：`r_fmt=None` 时回退到 `mid_fmt = for_mult(a.fmt, b.fmt)`，而 `for_mult` 的小数位恒为 `a.F + b.F`（见 [en_cl_fix_types.py:269](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L269)）。两个 \(F_a\)、\(F_b\) 位小数相乘，乘积的小数位正是两者之和，故 `mid_fmt` 精确承载乘积且无损。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**从构造到 wrap 饱和**」的完整追踪：

1. **构造与边界**：构造 `a_fmt = FixFormat(1, 30, 22)`（宽 53）。说明它为何是 narrow 的极限，并解释为何宽 54 的同类格式会被 `NarrowFix` 拒绝。
2. **舍入等价性**：取该格式下的若干值，用 `NarrowFix.round` 配合 `for_round` 做一次 `NonSymPos_s` 舍入到 `F=20`；再对照 4.2.2 的偏移表手算其中一个值，确认与程序输出一致。进而口头论证它等价于 VHDL `cl_fix_round`（提示：在偏移分支 `mid_fmt.F == a_fmt.F`）。
3. **wrap 降级**：把数据 `resize`/`saturate` 到 `r_fmt = FixFormat(1, 29, 22)` 并用 `None_s`（回绕）。先用 `for_add(a_fmt, FixFormat(0, r_fmt.I+1, 0))` 算出 `add_fmt.width`，判断是否触发 wide 降级；再用 `WideFix` 做同输入的回绕，验证两者结果一致。
4. **运算符**：用 `a + a`、`a * a` 体会三段式，打印 `mid_fmt.F` 确认全精度结果的小数位增长规律（加法取 max、乘法取和）。

把这个流程整理成一张表：每一步用到的源码函数、输入输出格式、是否触发降级、是否与 VHDL/`WideFix` 等价。这张表就是本讲的知识地图。

> 说明：以上命令的逐字输出未在本环境实跑（运行 Python 需另授权），具体数值结果标注为「待本地验证」；但格式宽度、降级条件、等价性等结构性结论均可由源码与公式直接推出。

## 6. 本讲小结

- `NarrowFix` 把定点数以**归一化浮点**形式存进 float64，因而能用原生浮点运算高速完成 ≤53 位的全部算术与转换。
- **53 位上限**源自 IEEE 754 双精度的 53 位有效位；为简化有符号 wrap，额外预留 1 位，使有符号/无符号统一为 53。
- `round` 用「归一化浮点 + 实数偏移 + floor 截断」实现七种模式；由于在偏移分支 `mid_fmt.F == a_fmt.F`，其偏移数值与 VHDL `cl_fix_round` 逐位一致，二者结果完全等价。
- `saturate` 的 wrap 路径做模运算；有符号 wrap 的中间项 `data + 2**r.I` 可能扩位到 54 位，于是用 `for_add` 预测，超 53 即**临时降级到任意精度整数**（`object` dtype）计算，保证精度后再转回浮点。
- 算术方法统一遵循「`for_*` 算 `mid_fmt` → 浮点运算 → `resize` 收敛」三段式；运算符重载让 `a+b`/`a*b`/`a<<n` 直接可用。
- 读源码时发现 `__len__` 存在引用未定义变量 `other` 的疑似遗留缺陷，是「带着质疑读代码」的真实例子。

## 7. 下一步学习建议

- 阅读 [u4-l3 WideFix：任意精度整数实现](u4-l3-widefix-class.md)，对照「未归一化整数存储 + 手动对齐小数点 + 任意精度」的实现，理解 narrow/wide 在表示与运算上的本质差异，以及 `from_narrowfix`/`to_real` 的互转。
- 回到主接口 [en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py)，逐个追踪 `cl_fix_round`/`cl_fix_saturate`/`cl_fix_mult` 中 `a_wide or r_wide` 的分发逻辑，确认「任一相关格式 wide 即全程 wide」的提升规则。
- 进入 U5 的 VHDL 实现，重点对照 [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) 里 `cl_fix_round` 的 `mid_fmt` 与偏移 case，把本讲证明的「narrow round ≡ VHDL round」在硬件代码里再确认一遍。
