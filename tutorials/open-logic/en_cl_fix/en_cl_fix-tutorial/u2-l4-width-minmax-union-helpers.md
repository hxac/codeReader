# 位宽、极值与格式工具函数

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `cl_fix_width` 计算任意 `[S,I,F]` 格式占用的总位宽，并说出它和 Python `FixFormat.width` 的关系。
- 用 `cl_fix_max_value` / `cl_fix_min_value` 求出一个格式能表示的最大值与最小值，并解释 Python 与 VHDL 在「返回值类型」上的差异。
- 用 `union`（`cl_fix_union_fmt`）求多个格式的「最小公共超集」，理解它为何是 `for_abs` / `for_addsub` 等格式预测函数的公共积木。
- 理解 `NullFixFormat_c`（VHDL）与 `None`（Python）这对「哨兵值」的作用，以及它们如何表示「结果格式未指定 → 使用全精度中间格式」。

本讲是 U2 的收尾，承接 u2-l1 的 `[S,I,F]` 三元组，把「格式」从静态描述升级为可计算的对象，为 U3 的结果格式预测打下工具基础。

## 2. 前置知识

在进入本讲前，你需要先掌握（来自 u2-l1）：

- **定点格式 `[S,I,F]`**：`S` 为符号位数（只能 0 或 1），`I` 为整数位数，`F` 为小数位数，`I`、`F` 均可为负。
- **位权重模型**：最低有效位（LSB）恒为 \(2^{-F}\)，符号位（补码）权重为 \(-2^{I}\)，其余位向左逐位翻倍。
- **三语言镜像架构**（u1-l2）：VHDL 是金标准语义，Python 同名同参数镜像，二者一一对应。
- **narrow / wide 内部表示**（u4-l1 的前置概念）：库以 53 位为界，把宽度 ≤53 的格式用双精度浮点（NarrowFix）表示，把 >53 的格式用任意精度整数（WideFix）表示。

本讲用到两个简单的数学事实，先放在这里：

- 一个格式的总位宽 \(W = S + I + F\)。
- 一个格式能表示的最大正数为「除符号位外全 1」，对应数值 \(2^{I} - 2^{-F}\)；最小值为符号位 1、其余全 0（有符号时为 \(-2^{I}\)，无符号时为 \(0\)）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py` | Python 主接口，定义 `cl_fix_width` / `cl_fix_max_value` / `cl_fix_min_value` / `cl_fix_union_fmt` 等公共函数。 |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py` | 定义 `FixFormat` 类，包括 `width` 属性与 `union` 静态方法。 |
| `bittrue/models/python/en_cl_fix_pkg/narrow_fix.py` | NarrowFix 的 `max_value` / `min_value` 实现（≤53 位，返回浮点）。 |
| `bittrue/models/python/en_cl_fix_pkg/wide_fix.py` | WideFix 的 `max_value` / `min_value` 实现（>53 位，返回任意精度整数）。 |
| `hdl/en_cl_fix_pkg.vhd` | VHDL 包，定义 `NullFixFormat_c`、`cl_fix_width`、`cl_fix_max_value` / `cl_fix_min_value` 与私有 `union`。 |
| `hdl/en_cl_fix_private_pkg.vhd` | 提供 `maximum` / `minimum` 等自实现工具函数。 |
| `README.md` | 给出格式定义与多组 `[S,I,F]` 示例范围表，是验证手算结果的黄金参考。 |

## 4. 核心概念与源码讲解

### 4.1 位宽计算 cl_fix_width

#### 4.1.1 概念说明

一个 `[S,I,F]` 格式到底要占多少位？答案简单到只有一行：把三部分位都加起来。

\[
W = S + I + F
\]

这个 `W` 就是「存储一个该格式数据所需的二进制位数」。它在库中无处不在：声明 `std_logic_vector` 的位宽、分配寄存器、计算 narrow/wide 分界（53 位）等，都要先求出 `W`。因此库把它封装成一个公共函数 `cl_fix_width`，而不是让每个调用点手写 `S+I+F`。

需要特别注意：由于 `I`、`F` 可为负（见 u2-l1 的负 I、负 F 格式），`W` 理论上可能很小甚至为零，但库的构造函数会强制 \(I+F \ge 0\)，从而保证有符号格式的 \(W \ge S \ge 1\)、无符号格式的 \(W \ge 0\)。

#### 4.1.2 核心流程

```
输入 fmt = (S, I, F)
    │
    ▼
返回 S + I + F
```

Python 与 VHDL 的实现逐字一致：

- Python：`FixFormat.width` 属性返回 `self.S + self.I + self.F`；`cl_fix_width(fmt)` 只是它的薄封装。
- VHDL：`cl_fix_width(fmt)` 返回 `fmt.S + fmt.I + fmt.F`。

#### 4.1.3 源码精读

Python 主接口的封装只有一行，直接转调 `FixFormat.width` 属性：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:72-76](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L72-L76) —— Python `cl_fix_width`：返回格式的总位宽。

真正的计算在 `FixFormat` 类的 `width` 属性里：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:377-382](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L377-L382) —— `width` 属性：`S + I + F`。

VHDL 包头先声明这个函数，包体给出实现，两处也是逐字相同的 `S+I+F`：

[hdl/en_cl_fix_pkg.vhd:365-368](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L365-L368) —— VHDL `cl_fix_width` 包体实现。

> 小贴士：VHDL 里 `cl_fix_width` 几乎出现在每一个需要分配位宽的地方（例如 `std_logic_vector(cl_fix_width(fmt)-1 downto 0)`），所以它是最被频繁调用的「格式工具」之一。

#### 4.1.4 代码实践

**实践目标**：验证手算位宽与库函数一致。

**操作步骤**（源码阅读型 + 可选运行）：

1. 对照 README 的示例表，手算几个格式的位宽。
2. （可选）用 Python 跑一行验证。

```python
# 示例代码
from en_cl_fix_pkg import FixFormat, cl_fix_width

for fmt in [FixFormat(1,2,1), FixFormat(0,4,0), FixFormat(1,4,-2), FixFormat(1,-2,4)]:
    print(fmt, "-> width =", cl_fix_width(fmt))
```

**需要观察的现象**：

- `[1,2,1]` → 4；`[0,4,0]` → 4；`[1,4,-2]` → 3；`[1,-2,4]` → 3。

**预期结果**：与 README 示例表的「Bit Pattern」位数一致（`sii.f`、`iiii.`、`sii--.`、`.-sff`）。

**运行方式**（待本地验证）：在仓库根目录执行 `PYTHONPATH=bittrue/models/python python -c "..."`，或把脚本放进 `bittrue/models/python` 目录运行。

#### 4.1.5 小练习与答案

**练习 1**：格式 `[0,4,2]` 的位宽是多少？它最多能容纳多少个不同的值？

**参考答案**：\(W = 0+4+2 = 6\)，可表示 \(2^{6} = 64\) 个不同值（范围 \(0 \ldots 15.75\)）。

**练习 2**：为什么 `cl_fix_width` 返回类型在 VHDL 里是 `natural` 而不是 `integer`？

**参考答案**：因为合法格式的位宽恒为非负（构造时已保证 \(I+F \ge 0\)），用 `natural` 在类型层面表达「不会为负」这一不变量，调用方可以放心用它做 `downto 0` 的位宽声明。

---

### 4.2 极值计算 cl_fix_max_value / cl_fix_min_value

#### 4.2.1 概念说明

知道了一个格式的位宽还不够，我们经常还需要回答另一个问题：**这个格式能表示的数值范围是多少？** 例如：

- 生成测试激励时要遍历「全部可表示值」（见 u7-l1 的 cosim `get_data`）。
- 判断某个数是否落在范围内（`cl_fix_in_range`）。
- 实现饱和时需要上下界来钳位（见 u2-l3）。

库用两个函数回答这个问题：

- `cl_fix_max_value(fmt)`：最大可表示值。
- `cl_fix_min_value(fmt)`：最小可表示值。

由 u2-l1 的位权重模型可直接推出公式：

\[
v_{\max} = 2^{I} - 2^{-F}
\]

\[
v_{\min} = \begin{cases} -2^{I} & S = 1 \\ 0 & S = 0 \end{cases}
\]

直觉上：最大值是「符号位为 0、其余全 1」；最小值在有符号时是「符号位为 1、其余全 0」（补码最负），无符号时就是 0。

#### 4.2.2 核心流程

```
输入 fmt = (S, I, F)
    │
    ├── max_value: 返回 2**I - 2**(-F)
    │
    └── min_value: 若 S==1 返回 -2**I，否则返回 0
```

这里有一个**关键的跨语言、跨表示差异**，务必记住——同一个「最大值」概念，在三处有三种不同的返回形态：

| 调用 / 语言 | 返回形态 | `[1,2,1]` 的 max_value |
| --- | --- | --- |
| Python，narrow 格式（≤53 位） | 归一化**浮点**（真实数值） | `3.5` |
| Python，wide 格式（>53 位） | **未归一化整数**（真实值 × \(2^{F}\)） | \(7\)（即 \(3.5 \times 2\)） |
| VHDL | **比特向量** `std_logic_vector`（位模式） | `"0111"` |

之所以 narrow 返回浮点、wide 返回整数，是因为库的对外数据约定（u4-l1）：narrow 数据用 `float64` 传递（已是真实值），wide 数据用任意精度整数数组传递（需除以 \(2^{F}\) 才还原成真实值）。`cl_fix_max_value` 返回的是内部 `_data`，因此自然带上了这种表示差异。

而 VHDL 没有「浮点定点」一说，硬件里就是比特，所以它返回的是「最大值对应的位模式」：有符号时最高位（符号位）为 0、其余全 1；无符号时全部为 1。

#### 4.2.3 源码精读

Python 主接口按 narrow/wide 分发，返回各自类的 `_data`：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:87-104](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L87-L104) —— `cl_fix_max_value` / `cl_fix_min_value`：按 `cl_fix_is_wide` 分发到 NarrowFix 或 WideFix。

分发依据是 `cl_fix_is_wide`，它正是用 `cl_fix_width` 与 53 比较：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:79-84](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84) —— `cl_fix_is_wide`：宽度超过 53 即判为 wide。这就是 4.1 的 `cl_fix_width` 的又一处真实用例。

NarrowFix 的极值用浮点直接套公式：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:110-123](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L110-L123) —— NarrowFix `max_value` = `2.0**I - 2.0**(-F)`，`min_value` = 有符号 `-2.0**I`、无符号 `0.0`。返回的是归一化浮点。

WideFix 的极值则在「未归一化整数」域里算（整数 = 真实值 × \(2^{F}\)）：

[bittrue/models/python/en_cl_fix_pkg/wide_fix.py:129-146](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L129-L146) —— WideFix `max_value` = `2**(I+F)-1`，`min_value` = 有符号 `-2**(I+F)`、无符号 `0`。注意它们与 NarrowFix 在数学上等价（除以 \(2^{F}\) 后即得真实值），只是存储在整数域。

VHDL 的实现走的是「位模式」路线，非常直观——最大值就是「全 1，但有符号时符号位清 0」：

[hdl/en_cl_fix_pkg.vhd:370-390](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L370-L390) —— VHDL `cl_fix_max_value` / `cl_fix_min_value`：先建一个 `cl_fix_width(fmt)` 位的向量，`max` 置全 1（有符号再把最高位改 0），`min` 在有符号时置「最高位 1、其余 0」、无符号时全 0。这里又一次用到了 4.1 的 `cl_fix_width` 来定向量长度。

> 对照 README：`[1,2,1]` 的范围写作 `-4 ... +3.5`，与上面三套公式都吻合（浮点 3.5、整数 7、位模式 `0111`）。见 [README.md:109-116](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L109-L116)。

#### 4.2.4 代码实践

**实践目标**：用 Python 求 `[1,2,1]` 的范围，并用 `cl_fix_to_real` 验证。

**操作步骤**：

```python
# 示例代码
from en_cl_fix_pkg import FixFormat, cl_fix_max_value, cl_fix_min_value, cl_fix_to_real

fmt = FixFormat(1, 2, 1)                       # [1,2,1]，宽度 4 ≤ 53 => narrow
vmax = cl_fix_max_value(fmt)                   # 3.5（narrow 返回归一化浮点）
vmin = cl_fix_min_value(fmt)                   # -4.0

print("range =", vmin, "...", vmax)
# narrow 的 to_real 是恒等映射，用来验证 max_value 确实落在该格式内
print("to_real(max) =", cl_fix_to_real(vmax, fmt))
```

**需要观察的现象**：

- `vmax` 是浮点 `3.5`（而不是整数 7），因为 `[1,2,1]` 是 narrow 格式。
- `vmin` 是 `-4.0`。
- `cl_fix_to_real(vmax, fmt)` 仍为 `3.5`（narrow 下 `to_real` 原样返回，见主接口实现）。

**预期结果**：输出 `range = -4.0 ... 3.5` 与 `to_real(max) = 3.5`，与 README 表格 `-4 ... +3.5` 一致。

> 进阶观察（待本地验证）：把格式换成 65 位的 `FixFormat(1, 60, 4)`（宽度 65 > 53，wide），`cl_fix_max_value` 会返回一个**任意精度整数**（约 \(2^{64}-1\)），需要再除以 \(2^{F}=16\) 才得到真实最大值。这正是 wide 表示的「整数域」特征。

#### 4.2.5 小练习与答案

**练习 1**：求 `[0,4,2]`（无符号）的 \(v_{\max}\) 与 \(v_{\min}\)。

**参考答案**：\(v_{\max} = 2^{4} - 2^{-2} = 16 - 0.25 = 15.75\)；\(v_{\min} = 0\)（无符号）。与 README 表格一致。

**练习 2**：为什么 VHDL 的 `cl_fix_max_value` 返回 `std_logic_vector` 而不是 `real`？

**参考答案**：VHDL 描述的是硬件，定点数本身就是比特向量；返回位模式可以直接用于比较、赋值和综合。若需要人类可读的实数，可再套一层 `cl_fix_to_real(cl_fix_max_value(fmt), fmt)`。

**练习 3**：WideFix 的 `max_value` 返回 `2**(I+F)-1`，它和 NarrowFix 的 `2**I - 2**(-F)` 等价吗？

**参考答案**：等价。WideFix 存储的是未归一化整数 \(= \text{真实值} \times 2^{F}\)，所以 \((2^{I}-2^{-F}) \times 2^{F} = 2^{I+F} - 1\)。两者只是同一数值在不同表示域里的写法。

---

### 4.3 格式合并 union：最小公共超集

#### 4.3.1 概念说明

很多场景下，我们需要一个「能同时容纳多个不同格式」的格式。例如：

- `a + b` 与 `a - b` 的结果可能落在不同范围，`for_addsub` 要取两者之并（见 u3-l1）。
- `abs(a)` 的结果既可能是 `a` 本身，也可能是 `-a`，`for_abs` 要取 `a` 与 `-a` 之并（见 u3-l2）。
- 把两路不同格式的数据送进同一个寄存器/总线时，需要一个公共容器。

「最小公共超集」就是 **union**：对每一个分量 `S`、`I`、`F` 分别取最大值。

\[
\text{union}(a, b) = (\max(a_S, b_S),\; \max(a_I, b_I),\; \max(a_F, b_F))
\]

为什么取 max 就是「最小超集」？因为：

- 符号位取 max：只要有一个是有符号，结果就必须有符号才能表示负数。
- 整数位取 max：要覆盖两者中更大的整数范围。
- 小数位取 max：要覆盖两者中更细的小数精度。

这样得到的格式既能无损表示 `a` 的所有值，也能无损表示 `b` 的所有值，而且每一位都「必要」（去掉任何一位都会让某一侧越界或丢精度），因此是「最小」的。

> 注意：union 接受两种入参——两个 `FixFormat`，或一个 `FixFormat` 的集合（list/tuple）。

#### 4.3.2 核心流程

```
输入：若干个 fmt
    │
    ▼
r = 第 1 个 fmt 的浅拷贝
    │
    对每个后续 fmt_i：
    │   r.S = max(r.S, fmt_i.S)
    │   r.I = max(r.I, fmt_i.I)
    │   r.F = max(r.F, fmt_i.F)
    ▼
返回 r
```

VHDL 版本没有「集合」入参，只合并两个格式，用自实现的 `maximum` 函数；若要合并多个，调用方需自行折叠（如 `for_addsub` 里先分别算 add/sub 再 union）。

#### 4.3.3 源码精读

Python 的 `union` 是 `FixFormat` 的静态方法，兼容「两个参数」与「一个集合」两种调用方式，并用浅拷贝避免修改入参：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:345-362](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L345-L362) —— `FixFormat.union`：对 `S`、`I`、`F` 分别取 `max`，循环折叠所有输入格式。

主接口里给它起了一个与 VHDL 命名风格一致的别名 `cl_fix_union_fmt`，与其他 `cl_fix_*_fmt` 别名（`cl_fix_add_fmt = FixFormat.for_add` 等）排在一起：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:61-70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L61-L70) —— 一组格式函数别名，`cl_fix_union_fmt = FixFormat.union` 在最后一行。

VHDL 把 `union` 实现为包体内的**私有**函数（不在包头公共 API 里），直接用 `maximum` 构造 record：

[hdl/en_cl_fix_pkg.vhd:353-360](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L353-L360) —— VHDL 私有 `union`：用 `maximum(aFmt.S, bFmt.S)` 等三处取最大值。

这里的 `maximum` 不是 VHDL 内建函数，而是私有包里**手工实现**的（同理还有 `minimum`）：

[hdl/en_cl_fix_private_pkg.vhd:96-112](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L96-L112) —— 自实现的 `maximum` / `minimum`：用 `if` 比较返回较大/较小者。手工实现的原因与综合友好性有关（详见 u8-l2）：VHDL-93 没有可综合的 `max`/`min` 内建运算符，库选择自己写以保证跨工具链行为一致。

#### 4.3.4 代码实践

**实践目标**：用 Python 合并两个不同格式，验证结果是「各分量取 max」。

**操作步骤**：

```python
# 示例代码
from en_cl_fix_pkg import FixFormat, cl_fix_union_fmt

a = FixFormat(1, 4, 2)     # 有符号，整数位多
b = FixFormat(0, 2, 8)     # 无符号，小数位多
u = cl_fix_union_fmt(a, b)
print("union =", u)        # 期望 (1, 4, 8)

# 也可以一次合并多个（传一个集合）
u3 = cl_fix_union_fmt([FixFormat(1,4,2), FixFormat(0,2,8), FixFormat(1,1,1)])
print("union of 3 =", u3)  # 期望 (1, 4, 8)
```

**需要观察的现象**：

- 两两合并：`S=max(1,0)=1`、`I=max(4,2)=4`、`F=max(2,8)=8` → `(1,4,8)`。
- 三个合并：再并入 `(1,1,1)` 不改变结果（每一维都不更大）→ 仍是 `(1,4,8)`。

**预期结果**：`union = FixFormat(1, 4, 8)`；`union of 3 = FixFormat(1, 4, 8)`。

> 验证「最小性」：`union(a,b)=(1,4,8)` 的位宽是 13。你可以试着把任一维减 1（如 `(1,4,7)`），就会发现它无法再无损表示 `b` 的小数精度——这就反证了 union 给出的格式是必要的。

#### 4.3.5 小练习与答案

**练习 1**：`union([1,8,0], [0,0,8])` 等于什么？它的位宽是多少？

**参考答案**：`S=max(1,0)=1`、`I=max(8,0)=8`、`F=max(0,8)=8` → `(1,8,8)`，位宽 \(1+8+8=17\)。

**练习 2**：`for_abs(a)` 内部为什么需要 `union`？（提示：结合 u2-l1 与 u3-l2）

**参考答案**：`abs(a)` 的结果要么是 `a`（当 `a≥0`），要么是 `-a`（当 `a<0`）。`-a` 的格式由 `for_neg` 给出，可能与 `a` 不同（尤其 1 位无符号取反会变成有符号）。`for_abs` 用 `union(a_fmt, neg_fmt)` 得到一个能同时无损表示这两种情况的格式。

**练习 3**：VHDL 的 `union` 是私有函数，调用方（如 `for_addsub`）如何合并两个以上格式？

**参考答案**：嵌套调用折叠，例如 `union(union(x, y), z)`。Python 版则可直接传一个集合一次性合并，这是两边的一个便利性差异。

---

### 4.4 哨兵格式 NullFixFormat_c：「未指定格式」的约定

#### 4.4.1 概念说明

库里的算术函数（`cl_fix_add`、`cl_fix_mult` 等）都有一个可选参数 `result_fmt`（结果格式）。它的语义是：

- **若指定**了一个具体格式：结果会被舍入+饱和到该格式（见 u2-l2 / u2-l3）。
- **若未指定**：返回**全精度无损**结果，即直接用 `mid_fmt`（运算自然产生的最大格式）。

那么「未指定」用什么值表示？这就需要一个**哨兵值（sentinel）**——一个不可能成为合法格式的特殊值，用来代表「用户没填」。

- **VHDL**：用常量 `NullFixFormat_c := (0, 0, -1)`。它的位宽是 \(0+0+(-1) = -1\)，是个**非法格式**（位宽为负），因此绝不会和任何真实格式冲突。
- **Python**：用内置的 `None`。

为什么 VHDL 不学 Python 用 `None`？因为 VHDL 的 `FixFormat_t` 是一个 record 类型，参数必须有该类型的默认值，没有「空值」概念，只能挑一个非法 record 当哨兵。而 Python 的 `FixFormat(0,0,-1)` 根本**构造不出来**——构造函数会断言失败：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:61-70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L61-L70) —— Python 构造函数断言 `I+F >= 0`，因此 `(0,0,-1)`（\(I+F=-1\)）会被拒绝。这正是两语言「刻意差异」：VHDL 用非法 record 当哨兵，Python 改用 `None`，各自避开本语言无法表达空值的尴尬。

#### 4.4.2 核心流程

```
算术函数被调用，result_fmt 可能是「哨兵」或「真实格式」
    │
    ▼
判断 result_fmt 是否等于哨兵？
    │
    ├── 是（未指定）→ r_fmt = mid_fmt（全精度无损）
    │
    └── 否（已指定）→ r_fmt = result_fmt（会触发舍入/饱和）
    │
    ▼
用 r_fmt 做 resize 收敛结果
```

- VHDL 判断：`choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt)`。
- Python 判断：`if r_fmt is None: r_fmt = mid_fmt`。

#### 4.4.3 源码精读

VHDL 在包头定义这个哨兵常量，紧跟 `FixFormat_t` 类型之后：

[hdl/en_cl_fix_pkg.vhd:45](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L45) —— `NullFixFormat_c : FixFormat_t := (0, 0, -1)`。

它被用作所有算术/转换函数 `result_fmt` 参数的**默认值**，例如：

[hdl/en_cl_fix_pkg.vhd:192-193](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L192-L193) —— 某算术函数声明中 `result_fmt : FixFormat_t := NullFixFormat_c`，作为「未指定」的默认值。

函数体里用 `choose` 把哨兵翻译回全精度 `mid_fmt_c`（`choose` 是私有包里的三元选择函数）：

[hdl/en_cl_fix_pkg.vhd:1114-1120](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1114-L1120) —— `r_fmt_c := choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt)`：若用户没指定结果格式，就回退到全精度中间格式；随后用 `cl_fix_width(mid_fmt_c)` 分配中间向量。

Python 主接口走完全对称的逻辑，只是哨兵换成 `None`：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:313-326](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L313-L326) —— `cl_fix_add` 中 `mid_fmt = cl_fix_add_fmt(a_fmt, b_fmt)`，随后 `if r_fmt is None: r_fmt = mid_fmt`。其余算术函数（`cl_fix_mult`、`cl_fix_neg`、`cl_fix_abs`、`cl_fix_shift`）都是同一套模板。

> 把三者串起来看：`NullFixFormat_c` / `None` 决定了 `r_fmt`，`r_fmt` 决定要不要 resize，而 resize 的边界又来自 4.2 的 `cl_fix_max_value` / `cl_fix_min_value`。本讲的四个工具函数正是在这里汇合成一条完整的链路。

#### 4.4.4 代码实践

**实践目标**：观察「指定 vs 不指定 `r_fmt`」对结果精度的影响，体会哨兵值的作用。

**操作步骤**：

```python
# 示例代码
from en_cl_fix_pkg import (
    FixFormat, cl_fix_add, cl_fix_add_fmt,
    cl_fix_max_value, cl_fix_min_value,
)

a_fmt, b_fmt = FixFormat(1,7,8), FixFormat(0,7,8)

# (1) 不指定 r_fmt（等价于 VHDL 传 NullFixFormat_c）=> 全精度无损
full = cl_fix_add(3.0, 4.0, a_fmt, b_fmt)
print("mid_fmt =", cl_fix_add_fmt(a_fmt, b_fmt))   # 全精度中间格式
print("full-precision result fmt used")

# (2) 指定一个更窄的 r_fmt => 触发舍入/饱和
narrow_fmt = FixFormat(1, 4, 4)
print("narrow range =", cl_fix_min_value(narrow_fmt), "...", cl_fix_max_value(narrow_fmt))
narrow = cl_fix_add(3.0, 4.0, a_fmt, b_fmt, r_fmt=narrow_fmt)
print("resized result fmt =", narrow_fmt)
```

**需要观察的现象**：

- 第 (1) 步：`cl_fix_add_fmt(a_fmt, b_fmt)` 给出一个宽度足够容纳最坏情况加法的中间格式（关于它如何预测，见 u3-l1）。
- 第 (2) 步：把结果限制到 `[1,4,4]`（范围 \(-16 \ldots 15.9375\)），`3.0+4.0=7.0` 仍在范围内，所以值不变；但若把输入换成会越界的值（如 `10.0 + 8.0`），就会看到饱和/回绕效果。

**预期结果**：能清楚看到「不指定 `r_fmt` → 全精度」与「指定 `r_fmt` → 收敛到目标格式」两种行为的分界，这正是哨兵值 `None` / `NullFixFormat_c` 在背后控制的开关。

#### 4.4.5 小练习与答案

**练习 1**：`NullFixFormat_c` 的位宽是多少？为什么用它当哨兵是安全的？

**参考答案**：位宽 \(0+0+(-1) = -1\)。因为任何合法格式的位宽都非负，所以一个位宽为 \(-1\) 的格式绝不可能是用户真正想要的结果格式，用它当「未指定」标记不会产生歧义。

**练习 2**：如果想在 VHDL 里调用 `cl_fix_add` 并希望「全精度无损结果」，`result_fmt` 该怎么传？

**参考答案**：不传（使用默认值 `NullFixFormat_c`），或显式传入 `cl_fix_add_fmt(a_fmt, b_fmt)` 算出的全精度格式。两者等价。

**练习 3**：Python 为什么不能用 `FixFormat(0,0,-1)` 当哨兵，而要用 `None`？

**参考答案**：Python 的 `FixFormat.__init__` 断言 `I+F >= 0`，`(0,0,-1)` 违反该断言无法构造。VHDL 的 record 没有这种构造期检查，且没有「空值」类型，所以反过来只能用非法 record `(0,0,-1)` 当哨兵。这是同一设计意图在两种语言里的不同落地。

## 5. 综合实践

把本讲四个工具串起来，完成一个小任务：**为一个简单加法器推导并核验它的可表示范围**。

任务背景：你要把两路数据 `a`（格式 `[1,7,8]`）与 `b`（格式 `[0,7,8]`）相加，结果先在全精度中间格式里得到，再收敛到一个目标格式 `[1,8,4]`。

请按下列步骤完成（源码阅读 + 可选运行）：

1. **位宽**：用 `cl_fix_width` 计算 `a`、`b`、目标格式 `[1,8,4]` 各自的位宽。
2. **全精度中间格式**：调用 `cl_fix_add_fmt(FixFormat(1,7,8), FixFormat(0,7,8))` 得到 `mid_fmt`，再用 `cl_fix_width(mid_fmt)` 看它比输入宽了多少位（预期整数位 +1）。
3. **极值**：用 `cl_fix_max_value` / `cl_fix_min_value` 分别求出 `mid_fmt` 与目标格式 `[1,8,4]` 的范围，比较两者。
4. **哨兵观察**：用 `cl_fix_add` 做一次加法，先不传 `r_fmt`（全精度），再传 `r_fmt=FixFormat(1,8,4)`（收敛），对比两次结果；并解释这背后的开关就是 `None` / `NullFixFormat_c`。
5. **union 拓展**（选做）：若改成 `cl_fix_addsub`（同时支持加和减），其结果格式是 `union(for_add, for_sub)`。调用 `cl_fix_addsub_fmt` 与 `cl_fix_union_fmt` 验证这一关系。

**验收标准**：你能用一句话说清「位宽 → 极值 → 收敛」这条链路上，本讲的四个工具各自负责哪一环；并能解释 Python 与 VHDL 在「极值返回类型」和「哨兵值」上的两处刻意差异。

## 6. 本讲小结

- `cl_fix_width(fmt) = S + I + F`，是最基础的格式工具，Python（`FixFormat.width`）与 VHDL 逐字一致，且被全库频繁用于分配位宽和判定 narrow/wide。
- `cl_fix_max_value` / `cl_fix_min_value` 给出格式的可表示范围：\(v_{\max}=2^{I}-2^{-F}\)，\(v_{\min}=-2^{I}\)（有符号）或 \(0\)（无符号）。
- 同一个「极值」在三处有三种返回形态：Python narrow 返回归一化浮点、Python wide 返回未归一化整数、VHDL 返回 `std_logic_vector` 位模式——这是三语言镜像 + narrow/wide 表示的自然结果。
- `union`（`cl_fix_union_fmt`）按 `S`、`I`、`F` 各取最大值，得到能无损容纳所有输入的「最小公共超集」，是 `for_abs` / `for_addsub` 等格式预测函数的公共积木。
- `NullFixFormat_c = (0,0,-1)`（VHDL）与 `None`（Python）是「结果格式未指定 → 用全精度 mid_fmt」的哨兵值；前者因 record 无空值而用非法格式，后者因构造期断言拒绝 `(0,0,-1)` 而改用 `None`，属两语言刻意差异。

## 7. 下一步学习建议

本讲把「格式」变成了可计算的对象，并补齐了 `width` / 极值 / `union` / 哨兵值这组底层工具。接下来：

- **进入 U3（结果格式预测）**：U3 会大量调用本讲的工具——`for_add` / `for_sub` 用极值推导位增长，`for_addsub` / `for_abs` 用 `union` 取并集，`for_round` 结合 `cl_fix_width` 判断是否需要补整数位。建议先读 u3-l1。
- **回顾 narrow/wide（u4-l2 / u4-l3）**：本讲提到的「极值在 narrow 返回浮点、wide 返回整数」的差异，其根源就在 NarrowFix / WideFix 的内部表示，可对照这两个类的 `max_value` / `min_value` 源码加深理解。
- **延伸阅读**：在 `hdl/en_cl_fix_pkg.vhd` 中全局搜索 `cl_fix_width` 的调用点，体会它如何贯穿 round / saturate / compare 等几乎所有函数；再搜索 `NullFixFormat_c`，数一数有多少个函数把「全精度无损」作为默认行为。
