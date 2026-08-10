# 七种舍入模式的实现原理

## 1. 本讲目标

当我们要把一个定点数的小数位 `F` 变少（降低 LSB 精度）时，被丢掉的低位不能直接扔掉——否则会引入系统性的截断误差。en_cl_fix 提供了 7 种「舍入模式」（FixRound）来处理这件事。

本讲读完，你应当能够：

- 说清楚 en_cl_fix 实现 7 种舍入的**统一框架**：先给数据加一个精心选择的「偏移量」，再做一次普通截断（floor）。
- 列出每种舍入模式对应的**偏移量公式**，并解释为什么「平局（tie，恰好 0.5）」是各种模式唯一的差别。
- 读懂 Python `NarrowFix.round` 与 VHDL `cl_fix_round` 两个镜像实现，并理解 VHDL 用到的 `get_half` / `get_unit_bit` 两个辅助函数。
- 解释 `cl_fix_round_fmt` 为什么在「非截断」模式下会给整数位 `+1`，以及 `fmt_check` 断言在保护什么。

本讲是单元 4（舍入、饱和、resize）的第一讲，只聚焦「舍入」一件事；饱和（u4-l2）和 resize（u4-l3）留待后续。

## 2. 前置知识

本讲假设你已经掌握（来自 u1-l2、u2-l1、u2-l2、u3-l2）：

- **`[S, I, F]` 格式**：`S` 符号位、`I` 整数位、`F` 小数位，总位宽 `S+I+F`。定点值 = 把比特当整数看再乘 `2^(-F)`。
- **三大类型**：`FixFormat`（格式）、`FixRound`（7 种舍入标签）、`FixSaturate`（4 种饱和标签），三者正交。
- **mid_fmt 三段式**：所有运算先算「全精度中间格式」mid_fmt、在其中无损计算、最后 resize 到目标格式。本讲的舍入就是 resize 里「减少小数位」的那一步。
- **for_round**（u3-l2）：舍入会减少小数位，结果格式的整数位可能需要 +1，由 `FixFormat.for_round` / `cl_fix_round_fmt` 保守推导。

几个本讲用到的小术语：

- **LSB（Least Significant Bit）**：最低有效位，即权重最小的那一位。
- **平局（tie）**：被丢掉的部分恰好等于「结果 LSB 的一半」。例如把 `0.5` 舍入到整数，`0.5` 正好在 `0` 和 `1` 中间，这就是一个平局。7 种模式的差别**只在于平局往哪边倒**。
- **floor / ceil**：向下取整 / 向上取整。`floor(2.7)=2`，`floor(-0.5)=-1`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | 定义三大类型 | `FixRound` 枚举的 7 个成员；`FixFormat.for_round` 的整数位 +1 规则 |
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | ≤53 位快速表示 | `NarrowFix.round` 的偏移分支与截断 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | 可综合 VHDL 包 | `cl_fix_round`、`get_half`、`get_unit_bit`、`cl_fix_round_fmt` |
| [bittrue/tests/python/cl_fix_round_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py) | 穷举对拍测试 | `round_check` 参考实现与测试主循环 |

核心心法只有一句话：**所有舍入 = 加偏移量 + 截断**。Python 用浮点数算偏移，VHDL 用位运算算偏移，但偏移的「形状」完全一致。

---

## 4. 核心概念与源码讲解

### 4.1 加偏移再截断：七种舍入的统一框架（NarrowFix.round 偏移分支）

#### 4.1.1 概念说明

假设输入格式有 `aF` 个小数位，结果格式有 `rF` 个小数位，且 `rF < aF`（要丢掉 `aF − rF` 位）。「舍入」本质上要回答一个问题：**被丢掉的低位，是直接扔掉（截断），还是要让保留的最低位「进一」？**

en_cl_fix 用一个极其统一的办法回答它：

> **先给原始数据加上一个「偏移量」（offset），再对齐到结果小数位做一次向下取整（floor）。**

为什么这样能实现舍入？因为「四舍五入」=`floor(x + 0.5)`。只要我们把「结果 LSB 的一半」加进去，原本够一半的低位就会把保留位顶进一，不够一半的则不影响。这个「一半」记作：

\[
\text{half} = 2^{-(rF+1)}
\]

它是结果 LSB（权重 `2^{-rF}`）正好的一半，也就是**平局点**。`floor(x + half)` 就是经典的「四舍五入到正无穷」（half-up）。

那 7 种模式怎么区分？关键观察：**除了 Trunc_s（纯截断），其余 6 种模式在「非平局」时行为完全一样**——超过一半就进一、不到一半就舍掉。它们的差别**只在平局（恰好等于 half）时往哪边倒**。于是统一公式变成：

\[
\text{rounded} = \big\lfloor (x + \text{offset}) \cdot 2^{rF} \big\rfloor \cdot 2^{-rF}
\]

每种模式只是 `offset` 不同。其中除了 half，还会用到「输入 LSB」：

\[
\text{unit} = 2^{-aF}
\]

它是输入数据能表达的最小步长。**「减去一个 unit」这个技巧用来把一个平局点从「刚好够进一」推到「差一点点不够进一」，从而让它向下倒**——避免了写 `if/else` 分支。能这么干，是因为定点输入已经被量化到 `unit` 分辨率，减掉恰好一个 `unit` 是精确的（在 53 位精度内）。

各模式的偏移量汇总（这是本讲最重要的一张表）：

| 模式 | 偏移量 offset | 平局倒向 | 整数等价（README） |
|---|---|---|---|
| `Trunc_s` | `0` | （不平局，直接丢） | `floor(x)` |
| `NonSymPos_s` | `+half` | 向上（+∞） | `floor(x + 0.5)` |
| `NonSymNeg_s` | `+half − unit` | 向下（−∞） | 向下取整，平局向下 |
| `SymInf_s` | `+half − unit·(x<0)` | 远离 0 | 正数 half-up，负数 half-down |
| `SymZero_s` | `+half − unit·(x≥0)` | 靠近 0 | 正数 half-down，负数 half-up |
| `ConvEven_s` | `+half − unit·((⌊x·2^{rF}⌋+1) mod 2)` | 凑偶数 | 银行家舍入到偶 |
| `ConvOdd_s` | `+half − unit·(⌊x·2^{rF}⌋ mod 2)` | 凑奇数 | 银行家舍入到奇 |

读法提示：

- `NonSymPos_s`（非对称、正向）就是最常用的「四舍五入」，平局一律向上。
- `SymInf_s` / `SymZero_s` 用 `x<0` 这个条件在正负两侧分别选 half-up / half-down，实现「对称」。
- `ConvEven_s` / `ConvOdd_s` 用 `⌊x·2^{rF}⌋ mod 2` 判断「当前结果最低位是奇还是偶」，让平局倒向偶/奇，从而统计无偏。

#### 4.1.2 核心流程

`NarrowFix.round` 的执行流程（伪代码）：

```
输入：data（归一化浮点），fmt（输入格式，含 aF），r_fmt（结果格式，含 rF），rnd
1. 断言：r_fmt 必须等于 FixFormat.for_round(fmt, rF, rnd)   ← fmt_check
2. 若 rF >= aF：不需要舍入，跳过偏移
3. 否则按 rnd 选 offset，加到 data 上：
     half = 2^(-rF-1)
     unit = 2^(-aF)
     offset 见上表
4. 截断：data = floor(data * 2^rF) * 2^(-rF)
5. 返回 NarrowFix(data, r_fmt)
```

注意第 1 步的断言：它强制调用者传入一个「够大」的结果格式（由 `for_round` 推导）。如果你随手给一个太小的 `r_fmt`，断言会立刻报错——这就是 `fmt_check` 的作用，详见 4.1.3 末尾。

#### 4.1.3 源码精读

先看整体 `round` 方法，注意第 161 行的断言、167–183 行的偏移分支、186 行的截断：

[narrow_fix.py:157-188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L157-L188) —— `NarrowFix.round` 整体：先断言结果格式合法，再按模式加偏移，最后截断。

逐段看。**第 1 步：fmt_check 断言**（第 161 行）：

```python
assert r_fmt == FixFormat.for_round(self._fmt, r_fmt.F, rnd), \
       "NarrowFix.round: Invalid result format. Use FixFormat.for_round()."
```

它要求传入的 `r_fmt` 恰好等于「按这个舍入模式保守推导出的结果格式」。如果你给的结果格式整数位不够（漏了 +1），舍入时可能溢出到根本不存在的位，导致静默错误。这个断言就是把这类错误提前变成显式失败。

**第 2 步：偏移分支**（[narrow_fix.py:167-183](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L167-L183)），只看 NonSymPos 和 NonSymNeg 两支对比「− unit」技巧：

```python
if r_fmt.F < fmt.F:
    if rnd is FixRound.Trunc_s:
        None
    elif rnd is FixRound.NonSymPos_s:
        data = data + 2.0 ** (-r_fmt.F - 1)                       # +half
    elif rnd is FixRound.NonSymNeg_s:
        data = data + 2.0 ** (-r_fmt.F - 1) - 2.0 ** -fmt.F       # +half - unit
    elif rnd is FixRound.SymInf_s:
        data = data + 2.0 ** (-r_fmt.F - 1) - 2.0 ** -fmt.F * (data < 0).astype(int)
    ...
```

- `NonSymPos_s`：只加 `half`，平局向上。
- `NonSymNeg_s`：加 `half` 再减 `unit`。对一个平局值 `n·2^{-rF} + half`，加上 `half − unit` 后变成 `n·2^{-rF} + 2·half − unit = (n+1)·2^{-rF} − unit`，它严格小于 `(n+1)·2^{-rF}`，于是 `floor` 后落回 `n·2^{-rF}`——平局向下。一个减法代替了一个条件判断。
- `SymInf_s`：`data < 0` 为真（负数）时减 `unit`。正数 half-up、负数 half-down，正好都「远离 0」。
- 收敛舍入两支见 [narrow_fix.py:178-181](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L178-L181)，用 `⌊data·2^{rF}⌋ mod 2` 判断当前结果最低位的奇偶来决定是否减 `unit`。

**第 3 步：截断**（[narrow_fix.py:186](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L186)）：

```python
data = np.floor(data * 2.0 ** r_fmt.F).astype(np.float64) * 2.0 ** -r_fmt.F
```

把数据乘 `2^{rF}`、向下取整、再乘回 `2^{-rF}`——这就是「丢掉多余小数位」的数学写法。所有模式共用这一行，差别全在前面加的偏移量上。

> 旁注：为什么这套浮点办法在 NarrowFix 里安全？因为 `MAX_WIDTH = 53`（[narrow_fix.py:52](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L52)），IEEE 754 双精度能精确表示 ≤53 位的整数，所以「加 half / 减 unit」这些小数运算不会丢精度。宽度超过 53 位时会自动走 WideFix（任意精度整数）路径，见 u6。

#### 4.1.4 代码实践

**实践目标**：复现 README 的舍入示例表，亲手验证 7 种模式只差在平局。

README 的例子是把 `2.2, 2.7, −1.5, −0.5, 0.5, 1.5` 舍入到格式 `[1,2,0]`（即 `F=0`）。其中后 4 个值都是**精确平局**（恰好 .5），用来观察各模式的差别；前 2 个是非平局，所有模式结果相同。

**操作步骤**（在仓库根目录，确保已 `pip install -r requirements.txt`）：

```python
# 文件名自定，例如 repo_root/round_demo.py
import numpy as np
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 4, 4)                       # 输入格式，足够表示 2.7、-1.5 等
rF    = 0                                        # 目标小数位：舍入到整数
vals  = np.array([2.2, 2.7, -1.5, -0.5, 0.5, 1.5])

a = cl_fix_from_real(vals, a_fmt)                # 归一化输入（half-up 量化）

for rnd in FixRound:
    r_fmt = FixFormat.for_round(a_fmt, rF, rnd)  # 必须用 for_round 推结果格式
    r     = cl_fix_round(a, a_fmt, r_fmt, rnd)
    out   = np.round(cl_fix_to_real(r, r_fmt), 3).tolist()
    print(f"{rnd.value} {str(rnd):12s} -> {out}")
```

**预期结果**（应与 README 表逐格一致）：

```
0 Trunc_s      -> [2.0, 2.0, -2.0, -1.0, 0.0, 1.0]
1 NonSymPos_s  -> [2.0, 3.0, -1.0,  0.0, 1.0, 2.0]
2 NonSymNeg_s  -> [2.0, 3.0, -2.0, -1.0, 0.0, 1.0]
3 SymInf_s     -> [2.0, 3.0, -2.0, -1.0, 1.0, 2.0]
4 SymZero_s    -> [2.0, 3.0, -1.0,  0.0, 0.0, 1.0]
5 ConvEven_s   -> [2.0, 3.0, -2.0,  0.0, 0.0, 2.0]
6 ConvOdd_s    -> [2.0, 3.0, -1.0, -1.0, 1.0, 1.0]
```

**需要观察的现象**：

- 第 1、2 列（2.2、2.7，非平局）在所有模式下都是 `2, 3`，印证「非平局时 6 种模式行为一致」。
- 第 3–6 列（平局）每行不同：例如 `0.5` 在 NonSymPos 进位到 1、在 NonSymNeg 舍到 0；`1.5` 在 ConvEven 凑成偶数 2、在 ConvOdd 凑成奇数 1。

> 若你的环境暂未装好依赖，本实践也可作为「源码阅读型实践」：直接对照 [README.md 的舍入表](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L123-L167) 与 4.1.1 的偏移量表，手算每一格，结论一致。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：用「加偏移再截断」框架，手算 `NonSymPos_s` 把 `−0.5` 舍入到 `F=0` 的结果。

答案：`offset = half = 0.5`，`floor(−0.5 + 0.5) = floor(0) = 0`。所以 `−0.5 → 0`，与 README 一致。

**练习 2**：为什么 `NonSymNeg_s` 的偏移是 `half − unit` 而不是直接 `−half`？

答案：因为输入是量化到 `unit` 分辨率的定点数，减一个 `unit` 是精确的；而 `half − unit` 让平局点落在「下一个结果 LSB 之下一点点」，`floor` 后自然向下。直接用 `−half` 会把所有非平局值也整体下推，破坏「非平局时与 half-up 一致」的性质。

---

### 4.2 VHDL cl_fix_round：位级实现与 get_half / get_unit_bit

#### 4.2.1 概念说明

Python 在浮点域里做「加偏移再截断」，VHDL 要在**比特域**里做同样的事，而且要能综合成真实电路。思路完全平行：

- 「half」→ 一个只在「平局位」为 1、其余为 0 的比特向量（由 `get_half` 生成）。
- 「unit」→ 无符号数里的「1」（即 mid 向量的最低位），加/减它就是进/退一个 LSB。
- 「`x<0`」→ 符号位 `sign_c`（由 `cl_fix_sign` 取出）。
- 「`⌊x·2^{rF}⌋ mod 2`」→ 结果最低位的实际比特值（由 `get_unit_bit` 取出）。

VHDL 实现先建立一个中间格式 `mid_fmt`，它至少保留 `result_fmt.F + 1` 个小数位（多留一位用来放「平局位」），把输入对齐进去，再加偏移、截断。

#### 4.2.2 核心流程

```
function cl_fix_round(a, a_fmt, result_fmt, round, fmt_check):
  1. mid_fmt = (result_fmt.S, result_fmt.I, max(result_fmt.F+1, a_fmt.F))
  2. fmt_check 断言：result_fmt == cl_fix_round_fmt(a_fmt, result_fmt.F, round)
  3. mid_v = convert(a, a_fmt, mid_fmt)        ← 对齐小数点 + 符号扩展
  4. half_c = get_half(mid_fmt, result_fmt)    ← 平局位的单 bit 向量
  5. 若 result_fmt.F < a_fmt.F:
       unit_v = get_unit_bit(mid_v, mid_fmt, result_fmt)  ← 当前结果最低位
       case round:
         Trunc_s    : 不加
         NonSymPos_s: mid_v + half_c
         NonSymNeg_s: mid_v + (half_c - 1)
         SymInf_s   : mid_v + half_c - sign_c
         SymZero_s  : mid_v + half_c - (not sign_c)
         ConvEven_s : mid_v + half_c - (not unit_v)
         ConvOdd_s  : mid_v + half_c - unit_v
  6. 截断：取出 mid_v 的高 result_fmt.width 位（丢掉低位 out_offset_c 个）
  7. 返回 result_v
```

VHDL 的偏移量表与 4.1.1 的 Python 表一一对应，只是把 `unit`（float）换成 `1`（一个 LSB），把布尔条件换成单比特信号：

| 模式 | VHDL 偏移 | 对应 Python |
|---|---|---|
| `Trunc_s` | `0` | `0` |
| `NonSymPos_s` | `+ half_c` | `+ half` |
| `NonSymNeg_s` | `+ (half_c − 1)` | `+ half − unit` |
| `SymInf_s` | `+ half_c − sign_c` | `+ half − unit·(x<0)` |
| `SymZero_s` | `+ half_c − (not sign_c)` | `+ half − unit·(x≥0)` |
| `ConvEven_s` | `+ half_c − (not unit_v)` | `+ half − unit·((⌊…⌋+1) mod 2)` |
| `ConvOdd_s` | `+ half_c − unit_v` | `+ half − unit·(⌊…⌋ mod 2)` |

#### 4.2.3 源码精读

**辅助函数 `get_half`**（[en_cl_fix_pkg.vhd:290-297](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L290-L297)）：构造「平局位」向量。

```vhdl
function get_half(aFmt, rFmt : FixFormat_t) return unsigned is
    constant tie_c : natural := aFmt.F - rFmt.F - 1;
    variable v     : unsigned(cl_fix_width(aFmt)-1 downto 0) := (others => '0');
begin
    v(tie_c) := '1';   -- 只在平局位置 1
    return v;
end function;
```

`tie_c` 就是「结果 LSB 往下一个位置」——权重正好是结果 LSB 的一半，即数学上的 `half`。把这一位置 1、其余置 0，加到 `mid_v` 上就等于「加 half」。

**辅助函数 `get_unit_bit`**（[en_cl_fix_pkg.vhd:299-311](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L299-L311)）：取出「结果最低位」的比特值，供收敛舍入判断奇偶。

```vhdl
function get_unit_bit(a, aFmt, rFmt : ...) return std_logic is
    constant unit_c : natural := aFmt.F - rFmt.F;
begin
    if unit_c >= cl_fix_width(aFmt) then
        return cl_fix_sign(a, aFmt);   -- 该位超出存储范围，由符号扩展隐含
    else
        return a(unit_c);              -- 正常取结果最低位
    end if;
end function;
```

它返回结果格式的最低位是 0 还是 1：是 0 即「当前为偶」，是 1 即「当前为奇」。`ConvEven_s` 据此决定要不要让平局进位凑偶。

**主函数 `cl_fix_round`**（[en_cl_fix_pkg.vhd:912-978](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L912-L978)）。先看 `mid_fmt` 与 `fmt_check`：

```vhdl
constant mid_fmt_c : FixFormat_t := (
    result_fmt.S, result_fmt.I, maximum(result_fmt.F+1, a_fmt.F));   -- L927-L931
...
if fmt_check then
    assert result_fmt = cl_fix_round_fmt(a_fmt, result_fmt.F, round)  -- L941-L944
        report "cl_fix_round: Invalid result format. Use cl_fix_round_fmt()." severity Failure;
end if;
```

`mid_fmt_c` 强制至少保留 `result_fmt.F+1` 位小数，确保平局位有地方放（注释 L922-L926 说明这一点）。`fmt_check` 是个布尔参数（默认 `true`），与 Python 的断言同义：结果格式必须等于 `cl_fix_round_fmt` 的保守推导。注释 L940 写明「允许设计者谨慎地忽略最坏情况结果格式」——即你可以传 `fmt_check => false` 绕过检查，但风险自负。

再看偏移分支（[en_cl_fix_pkg.vhd:955-971](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L955-L971)），挑两支对比：

```vhdl
when NonSymPos_s => mid_v := mid_v + half_c;             -- L958-L959
when NonSymNeg_s => mid_v := mid_v + (half_c-1);         -- L960-L961  ← "half - unit"
when SymInf_s    => mid_v := mid_v + half_c - ("" & sign_c);       -- L962-L963
when ConvEven_s  => mid_v := mid_v + half_c - ("" & not unit_v);   -- L966-L967
```

`half_c - 1` 就是「half 减一个 LSB」，与 Python 的 `half − unit` 完全同构。`("" & sign_c)` 把单比特拼成 1 位无符号数参与减法。最后是截断（[en_cl_fix_pkg.vhd:975](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L975)）：

```vhdl
result_v := std_logic_vector(mid_v(cl_fix_width(result_fmt)+out_offset_c-1 downto out_offset_c));
```

`out_offset_c = mid_fmt_c.F − result_fmt.F` 是要丢掉的低位个数，从 `out_offset_c` 起向上取 `result_fmt.width` 位，即「丢掉低位的截断」。

> **fmt_check 的作用（学习目标之三）**：Python 用 `assert`、VHDL 用 `assert ... severity Failure` + 可选 `fmt_check` 开关，二者都在保护同一件事——**防止调用者给一个「装不下舍入结果」的过小结果格式**。舍入可能让数值向上进位（见 4.3），若结果格式没预留整数位，进位会溢出到不存在的位，产生静默错误。`fmt_check` 把这种错误变成即时失败；VHDL 额外允许「明知数据不会触顶」的设计者关掉检查以节省面积。

#### 4.2.4 代码实践

**实践目标**：用测试脚本验证「VHDL 镜像 == Python NarrowFix == 独立 numpy 参考」三者逐位相等。

**操作步骤**（在仓库根目录）：

```bash
python bittrue/tests/python/cl_fix_round_test.py
```

**需要观察的现象**：脚本末尾打印 `Completed N tests.`（N 通常为几千），且中途没有任何 `AssertionError`。若出现 `Numerical error detected.` 则说明三路不一致——但在健康代码里不会发生。

**如何读懂这个测试**：核心是参考函数 `round_check`（[cl_fix_round_test.py:49-78](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L49-L78)），它用 numpy 独立实现了 7 种模式（例如 `ConvEven_s` 直接用 `np.around`，[L71-L73](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L71-L73)）。主循环（[L101-L134](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L101-L134)）枚举几乎所有 `a_fmt` × `rF` × `rnd` 组合，用 `get_data`（[L42-L47](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L42-L47)）生成该格式的**全部可能取值**，然后三路对拍：

```python
r      = cl_fix_round(a, a_fmt, r_fmt, rnd)                         # 待测：NarrowFix 路径
r_wide = WideFix.from_narrowfix(NarrowFix(a, a_fmt)).round(...).to_real()  # WideFix 路径
expected = round_check(a, a_fmt, r_fmt, rnd)                       # numpy 独立参考
assert np.array_equal(r, expected)
assert np.array_equal(r_wide, expected)
```

这种「枚举全部取值 + 三实现两两对拍」就是 en_cl_fix 保证 bit-true 的核心手段。

> 运行结果待本地验证（取决于是否装好 numpy/vunit-hdl）。即便不运行，精读 `round_check` 也能让你独立确认 4.1.1 的偏移量表。

#### 4.2.5 小练习与答案

**练习 1**：VHDL 里 `mid_fmt_c` 为什么要 `maximum(result_fmt.F+1, a_fmt.F)` 而不是直接用 `a_fmt.F`？

答案：为了保证「平局位」始终有物理位可放。`get_half` 要在 `tie_c = mid_fmt.F − result_fmt.F − 1` 处置 1，需要 `mid_fmt.F >= result_fmt.F + 1`。当 `a_fmt.F` 本身就够大时取 `a_fmt.F`，否则强制垫到 `result_fmt.F + 1`。注释（L922-L926）说明综合工具通常会优化掉多余的位。

**练习 2**：`SymInf_s` 在 VHDL 里用 `half_c − sign_c`，而 Python 用 `half − unit·(x<0)`。为什么 VHDL 的 `sign_c` 对应 Python 的 `(x<0)`？

答案：`cl_fix_sign` 在负数（符号位为 1）时返回 `'1'`，非负返回 `'0'`，正好等价于 Python 的布尔 `(x<0)`。把它拼成 1 位无符号数参与减法，负数时减 1（half-down，远离 0），正数时减 0（half-up，远离 0）。

---

### 4.3 cl_fix_round_fmt：为何部分模式整数位 +1

#### 4.3.1 概念说明

舍入只减少小数位，为什么可能要给**整数位** +1？因为「非截断」的舍入会把数值**向上进位**。考虑最坏情况：输入恰好是格式能表示的最大值附近，且舍入让它进一，就可能顶到原本装不下的整数位。

具体例子：无符号格式 `[0,2,2]`（2 整数位 + 2 小数位），最大值是 `11.11₂ = 3.75`。其中 `3.5 = 11.10₂` 是一个可表示值，也是舍入到 `F=0` 时的**平局点**。用 `NonSymPos_s`（half-up）舍入：`3.5 → 4 = 100₂`，需要 3 个整数位——比原来的 2 多了 1。所以 `for_round` 对非截断模式返回 `[0,3,0]`，而不是 `[0,2,0]`。

而 `Trunc_s` 只会向下取整，数值只会变小，绝不可能顶破上限，所以整数位不变。这就是「+1」规则的由来：

\[
I_{\text{result}} = \begin{cases} aFmt.I & \text{若 } rF \geq aFmt.F \text{（不减位）} \\ aFmt.I & \text{若 } rnd = \text{Trunc\_s} \\ aFmt.I + 1 & \text{其余所有舍入模式} \end{cases}
\]

#### 4.3.2 核心流程

`cl_fix_round_fmt` / `FixFormat.for_round` 的逻辑：

```
输入：a_fmt，rF（目标小数位），rnd
1. 若 rF >= a_fmt.F：不减位，I = a_fmt.I
2. 否则若 rnd == Trunc_s：截断不溢出，I = a_fmt.I
3. 否则：可能向上进位，I = a_fmt.I + 1
4. 兜底：若 S + I + rF < 1（格式会塌成 ≤0 位），把 I 顶到至少让位宽 = 1
5. 返回 (a_fmt.S, I, rF)
```

第 4 步是处理极端小格式（带负 I/F 的隐含位）的兜底，保证结果至少 1 位宽。

#### 4.3.3 源码精读

VHDL 版（[en_cl_fix_pkg.vhd:608-628](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L608-L628)）：

```vhdl
function cl_fix_round_fmt(a_fmt, r_frac_bits, rnd) return FixFormat_t is
    variable I_v : integer;
begin
    if r_frac_bits >= a_fmt.F then
        I_v := a_fmt.I;                  -- 不减位
    elsif rnd = Trunc_s then
        I_v := a_fmt.I;                  -- 截断不溢出
    else
        I_v := a_fmt.I + 1;              -- 其余模式可能 +1
    end if;
    if a_fmt.S + I_v + r_frac_bits < 1 then   -- 兜底：至少 1 位宽
        I_v := -a_fmt.S - r_frac_bits + 1;
    end if;
    return (a_fmt.S, I_v, r_frac_bits);
end;
```

Python 版（[en_cl_fix_types.py:318-342](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L318-L342)）逐行同义，例如 [L331-L336](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L331-L336) 就是「Trunc 不变、其余 +1」。

> 这个 +1 是「保守推导」的一环（见 u3-l2 心法）：`for_round` 只看格式、不看运行时数值，所以只要「存在某个合法输入会进位顶破上限」，它就一律 +1。即便你的实际数据永远到不了 `3.5`，格式依然预留这一位。如果想省掉它，可以在 VHDL 里用 `fmt_check => false` 显式放弃保护——但必须自己保证数据不触顶。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「非截断模式 +1 整数位」，并触发一次 fmt_check 失败。

**操作步骤**：

```python
from en_cl_fix_pkg import *

a_fmt = FixFormat(0, 2, 2)        # 无符号，max = 3.75

for rnd in FixRound:
    print(str(rnd), FixFormat.for_round(a_fmt, 0, rnd))
```

**预期结果**：

```
Trunc_s     (0, 2, 0)     # 截断：整数位不变
NonSymPos_s (0, 3, 0)     # 其余 6 种：整数位 +1
NonSymNeg_s (0, 3, 0)
SymInf_s    (0, 3, 0)
SymZero_s   (0, 3, 0)
ConvEven_s  (0, 3, 0)
ConvOdd_s   (0, 3, 0)
```

**进阶**：故意给一个过小的结果格式，观察 fmt_check 如何拦截：

```python
import numpy as np
a = cl_fix_from_real(np.array([3.5]), FixFormat(0,2,2))   # 3.5 是平局点
bad_fmt = FixFormat(0, 2, 0)   # 故意漏掉 +1（应为 [0,3,0]）
cl_fix_round(a, FixFormat(0,2,2), bad_fmt, FixRound.NonSymPos_s)  # 触发断言
```

**需要观察的现象**：第一条循环里，只有 `Trunc_s` 的整数位是 2，其余都是 3。第二条会抛出 `AssertionError: cl_fix_round: Invalid result format. Use cl_fix_round_fmt().`——这正是 fmt_check 在保护你。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Trunc_s` 不需要 +1 整数位？

答案：截断等价于 `floor`，只会让数值变小或不变，永远不会向上顶破格式上限，所以不可能需要额外的整数位。

**练习 2**：`for_round` 对 `[0,2,2]` 舍入到 `F=0` 的 `NonSymPos_s` 给出 `[0,3,0]`。请举出一个具体输入值，证明这个 +1 是「真的需要」的。

答案：输入 `3.5`（二进制 `11.10`，在 `[0,2,2]` 内可表示）。`NonSymPos_s` 把它 half-up 舍入到 `4 = 100₂`，确实需要 3 个整数位。若结果格式只有 `[0,2,0]`，`4` 就装不下。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个小小的「舍入探查器」：

1. **复现 README 平局表**：用 4.1.4 的脚本，确认 7 种模式只在平局处不同。
2. **验证整数位增长**：对 `a_fmt = [0,2,2]`、目标 `F=0`，打印 7 种模式的 `for_round` 结果与 `cl_fix_width`，确认非截断模式位宽比截断多 1。
3. **端到端追一次**：取 `a = 3.5`（`[0,2,2]`），用 `NonSymPos_s` 舍入到 `[0,3,0]`，打印 `cl_fix_to_real` 结果，确认是 `4.0`；再故意把结果格式改成 `[0,2,0]`，确认 `fmt_check` 报错。

把三步的输出整理成一张表（模式 / 平局倒向 / 结果格式 / 位宽 / 3.5 的舍入值），你就把「偏移框架」「位级实现」「格式 +1」三件事用一组数据同时印证了。运行结果待本地验证。

## 6. 本讲小结

- **统一框架**：7 种舍入 = 加偏移量 + 截断（floor）。非平局时 6 种非截断模式行为一致，差别只在平局（half）往哪边倒。
- **偏移量表**：所有偏移都由 `half = 2^-(rF+1)` 和 `unit = 2^-aF`（输入 LSB）组合而成；「减一个 unit」是把平局推向下方的无分支技巧。
- **Python vs VHDL 镜像**：`NarrowFix.round` 用浮点算偏移，`cl_fix_round` 用位运算（`get_half` 造平局位、`get_unit_bit` 取结果最低位）算偏移，两者偏移形状完全同构，经穷举对拍保证 bit-true。
- **整数位 +1**：`cl_fix_round_fmt` / `for_round` 对所有非截断模式给整数位 +1，因为舍入可能向上进位顶破上限；`Trunc_s` 只向下取整故不变。
- **fmt_check**：Python `assert` 与 VHDL `assert severity Failure`（可由 `fmt_check => false` 关闭）共同保护调用者不传入「装不下舍入结果」的过小格式，把潜在静默溢出变成即时失败。

## 7. 下一步学习建议

- **u4-l2 饱和**：本讲只解决「减少小数位」的舍入；当整数位/符号位变少时需要「饱和」（4 种 FixSaturate 模式）。饱和与舍入正交，下一讲会看到 NarrowFix 如何在 narrow 精度不足时回退到 WideFix(int) 做回绕。
- **u4-l3 resize**：把本讲的 round 与下讲的 saturate 串成 `resize = round → saturate`，并理解为什么顺序不能对调。
- **延伸阅读**：若想看舍入在真实可综合电路里如何流水线化，可预习 `hdl/en_cl_fix_round.vhd` 实体与 `cl_fix_recommended_pipelining`（u7-l1、u7-l2）。
