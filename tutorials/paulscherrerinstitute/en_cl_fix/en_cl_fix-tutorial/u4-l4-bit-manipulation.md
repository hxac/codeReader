# 位操作与字段提取组合

## 1. 本讲目标

本讲聚焦 en_cl_fix 库中的**位级访问函数族**——按位读写单个 bit，以及按字段（符号位 / 整数位 / 小数位）拆解与重组一个定点数。学完后你应当能够：

- 说清 `cl_fix_get_msb` / `cl_fix_get_lsb` / `cl_fix_set_msb` / `cl_fix_set_lsb` 的索引约定（MSB 从 0 起 vs LSB 从 0 起），并能手算任一位的值。
- 理解 `cl_fix_sign` / `cl_fix_int` / `cl_fix_frac` 如何按 `[S,I,F]` 三元组切出三个字段，以及负整数位、负小数位时的边界处理。
- 掌握 `cl_fix_combine` 的拼接公式与无符号格式下「符号位必须为 0」的约束。
- 体会到本讲最重要的跨语言差异：VHDL 在**位串域**做切片/拼接，Python / MATLAB 在**实数域**做算术，由此带来返回值含义的不同（尤其 `cl_fix_int` / `cl_fix_frac` 的取值约定），并能正确完成一次「提取 → 重组」的往返（round-trip）。

> 本讲不引入新的舍入或饱和算法，所有函数都是**对已有位串的无损搬运**（VHDL）或**等价算术**（Python/MATLAB）。舍入与饱和请回顾 u1-l4、u1-l5、u3-l2、u3-l3。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（均在前序讲义中建立）：

- **定点格式三元组 `[S,I,F]`**：`S` 决定是否有符号位（补码），`I` 为整数位数，`F` 为小数位数，总位宽 `W = S + I + F`（详见 u1-l2）。
- **位串域 vs 实数域**：VHDL 把定点数存成 `std_logic_vector` 位串，必须显式按位切片；Python / MATLAB 把它存成 `double`，本就在实数域里（详见 u3-l1）。
- **补码的不对称性**：有符号格式最负值 \(-2^I\) 比最正值 \(2^I - 2^{-F}\) 多走一个 LSB，最高位（符号位）为 1 当且仅当数值为负（详见 u1-l3）。
- **位宽查询 `cl_fix_width`**：本讲的索引范围校验（`0 … W-1`）依赖它（详见 u1-l3）。
- **Python 的 narrow / wide 双路径**：位宽 > 53 时函数会经 `cl_fix_is_wide` 派发到任意精度整数实现 `wide_fxp`（详见 u6-l1，本讲只点到为止）。

一个贯穿全讲的心智模型：

```
[S][ I 个整数位 ][ F 个小数位 ]
 ^                ^
 MSB(W-1)         LSB(0)      <-- LSB 索引从 0 起，向左递增
 ^                ^
 MSB 索引从 0 起，向右递增 <-- MSB 索引 0 永远指向最高位（即符号位或最高整数位）
```

同一位有两个「名字」：从 MSB 数下来是 `msb_index`，从 LSB 数上来是 `lsb_index`，且

\[ \text{msb\_index} + \text{lsb\_index} = W - 1 \]

这正是 `cl_fix_get_lsb` 与 `cl_fix_set_lsb` 实现的桥梁。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注 |
|------|------|---------|
| `vhdl/src/en_cl_fix_pkg.vhd` | VHDL 包，位串域实现 | 8 个函数的声明与函数体 |
| `python/src/en_cl_fix_pkg/en_cl_fix_pkg.py` | Python 主体库，实数域实现 | narrow 路径的 8 个函数 + `cl_fix_is_wide` |
| `python/src/en_cl_fix_pkg/wide_fxp.py` | Python >53 位任意精度实现 | `get_msb` / `set_msb` / `floor` / `frac_part` 整数实现 |
| `matlab/src/cl_fix_get_msb.m` 等 | MATLAB 一函数一文件 | 各函数的算术实现与 `help` 注释 |
| `vhdl/tb/en_cl_fix_pkg_tb.vhd` | VHDL 测试台 | 8 个函数的断言式用例 |
| `python/unittest/en_cl_fix_pkg_test.py` | Python 单元测试 | 对应 `TestCase` |

## 4. 核心概念与源码讲解

### 4.1 单位读写：cl_fix_get_msb / get_lsb / set_msb / set_lsb

#### 4.1.1 概念说明

`cl_fix_get_msb` 与 `cl_fix_get_lsb` 读取一个定点数中**指定的某一位**，返回 0 或 1。`cl_fix_set_msb` 与 `cl_fix_set_lsb` 把指定位写成给定值，返回改写后的**新**定点数（函数式风格，不就地修改原值）。两套函数的唯一区别是**索引方向**：

| 函数 | `index` 从哪一端数起 | `index = 0` 指向 |
|------|----------------------|------------------|
| `cl_fix_get_msb` / `cl_fix_set_msb` | 从 MSB（最高位）往下 | 最高位（有符号时即符号位） |
| `cl_fix_get_lsb` / `cl_fix_set_lsb` | 从 LSB（最低位）往上 | 最低位（权重 \(2^{-F}\)） |

合法索引范围是 `0 … cl_fix_width(a_fmt)-1`；越界在 MATLAB 抛 `error`、在 VHDL 由 `natural` 子类型与运行时约束兜底、在 Python 由后续算术产生异常或静默越界（见 4.1.4 实践）。

#### 4.1.2 核心流程

**读取一位（VHDL）**——纯位串切片，一步到位：

```
get_msb(a, index) = a( W-1 - index )     -- 从 MSB 端数 index 位
get_lsb(a, index) = a( index )           -- 从 LSB 端数 index 位
```

**读取一位（Python / MATLAB）**——实数域「缩放 + 取半」技巧：把目标位移到紧贴小数点左侧（权重 \(0.5\) 的边界），再看小数部分是否 \(\ge 0.5\)：

```
对于无符号，目标位 MSB-index = idx 的权重为 2^(IntBits-1-idx)
  bit = ( a * 2^(idx - IntBits)     mod 1 ) >= 0.5
对于有符号且 idx > 0：
  bit = ( a * 2^(idx - IntBits - 1) mod 1 ) >= 0.5
对于有符号且 idx = 0（符号位）：
  bit = (a < 0) ? 1 : 0            -- 补码符号位即「是否为负」
```

**改写一位**——Python / MATLAB 不直接动位，而是先读出当前位 `current`，若与目标 `value` 不同，就加上/减去该位的权重：

```
delta = (value - 0.5) - (current - 0.5)   -- 想置1且当前0 => +1；想置0且当前1 => -1；不变 => 0
result = a + delta * weight
其中 weight:
  有符号 idx=0（符号位）   : -2^IntBits        （补码最高位为负权）
  有符号 idx>0            :  2^(IntBits-idx)
  无符号                  :  2^(IntBits-idx-1)
```

#### 4.1.3 源码精读

**VHDL 声明**先给出索引约定（注释写明 "index = 0 retrieves the MSB"）：

- [vhdl/src/en_cl_fix_pkg.vhd:323-380](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L323-L380) ——「Bit Manipulation」段的四个函数声明，注释明确 MSB/LSB 两套索引语义。

**VHDL 函数体**极其简短，全是位串切片/赋值：

- [vhdl/src/en_cl_fix_pkg.vhd:1537-1553](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1537-L1553) —— `cl_fix_get_msb` 用 `a(a'high-index)`、`cl_fix_get_lsb` 用 `a(index)`，两行对照即可看出索引方向之差。
- [vhdl/src/en_cl_fix_pkg.vhd:1557-1581](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1557-L1581) —— `cl_fix_set_msb` / `cl_fix_set_lsb` 先把入参拷到局部 `a_v`，再用 `a_v(a_v'high-index) := value` 改写后返回。

**Python narrow 路径**用缩放取半法读位、用「读 + 加减权重」改位：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:114-125](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L114-L125) —— `cl_fix_get_msb`：有符号 `index==0` 走 `(a<0)` 取符号位，其余走 `(a*2**(index-IntBits-1)) % 1 >= 0.5`。
- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:127-128](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L127-L128) —— `cl_fix_get_lsb` 直接复用 `get_msb`，把 LSB 索引换算成 MSB 索引：`get_msb(a, aFmt, cl_fix_width(aFmt)-1-index)`。
- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:130-147](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L130-L147) —— `cl_fix_set_msb` 的三分支权重公式；`cl_fix_set_lsb` 同样委派给 `set_msb`。

**MATLAB** 与 Python 同思路，但显式做了越界 `error` 校验：[matlab/src/cl_fix_get_msb.m:17-34](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_get_msb.m#L17-L34)、[matlab/src/cl_fix_set_msb.m:19-39](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_set_msb.m#L19-L39)。

**Python wide 路径**（位宽 > 53）在整数上做移位取位：[python/src/en_cl_fix_pkg/wide_fxp.py:306-312](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L306-L312) 中 `get_msb` 用 `(self._data >> shift) % 2`，符号位同样走 `self._data < 0`；[wide_fxp.py:293-302](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L293-L302) 中 `set_msb` 用 `np.where(get_msb != value, data - weight*(-1)**value, data)`。

#### 4.1.4 代码实践

**实践目标**：用 `(true,3,3)` 中的 `2.25` 验证 MSB/LSB 两套索引，并与 VHDL testbench 的断言对照。

**操作步骤**（在 `python/unittest` 目录运行）：

```python
# 示例代码：保存为 tmp_bits.py 后 python3 tmp_bits.py
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *

fmt = FixFormat(True, 3, 3)      # W = 1+3+3 = 7 位
a = 2.25                         # 二进制 0_010_010

print("W =", cl_fix_width(fmt))
for idx in range(cl_fix_width(fmt)):
    print(f"MSB-index {idx}: {cl_fix_get_msb(a, fmt, idx)}  "
          f"LSB-index {idx}: {cl_fix_get_lsb(a, fmt, idx)}")

# 翻转 MSB-index=2（一个当前为 1 的位）
print("set_msb(idx=2, 0) =", cl_fix_set_msb(a, fmt, 2, 0))   # 2.25 - 2 = 0.25
print("set_msb(idx=1, 1) =", cl_fix_set_msb(a, fmt, 1, 1))   # 2.25 + 4 = 6.25
```

**需要观察的现象**：

1. `2.25` 在 7 位补码下为 `0 0 1 0 0 1 0`（MSB→LSB）。因此 `get_msb(idx=2)=1`、`get_msb(idx=1)=0`、`get_lsb(idx=1)=1`、`get_lsb(idx=2)=0`，与 testbench [en_cl_fix_pkg_tb.vhd:762-770](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L762-L770) 的 `CheckStdl` 断言一致。
2. `set_msb(idx=1, 1)` 把原本的 0 改成 1，加上该位权重 \(2^{3-1}=4\)，结果 `6.25`，与 [en_cl_fix_pkg_tb.vhd:776-777](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L776-L777) 一致。
3. `get_lsb(idx)` 与 `get_msb(W-1-idx)` 应当逐位相等——这是 Python 把 `get_lsb` 实现成 `get_msb` 委派（[en_cl_fix_pkg.py:127-128](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L127-L128)）的直接体现。

**预期结果**：`set_msb(idx=2,0)=0.25`、`set_msb(idx=1,1)=6.25`。（作者注：因运行环境未安装 numpy，以上数值为依据源码逻辑手算并经 VHDL testbench 断言交叉验证的结果，**待本地运行确认**。）

#### 4.1.5 小练习与答案

**练习 1**：对 `(false,4,0)` 的无符号数 `5`（即 `0101`），`get_msb(idx=0)`、`get_msb(idx=3)` 各是多少？`get_lsb(idx=0)` 又是多少？

**答案**：无符号最高位（idx=0）= `0`；最低位侧 idx=3 对应 MSB-index `4-1-3=0`，即最高位 = `0`；`get_lsb(idx=0)` = 最低位 = `1`。

**练习 2**：为什么有符号格式下 `get_msb(idx=0)` 不走 `% 1 >= 0.5` 公式，而单独用 `a < 0`？

**答案**：idx=0 是补码符号位，权重为负（\(-2^I\)），不能用「缩放到 0.5 边界再取半」的正权模型描述；而补码下「符号位为 1 ⇔ 数值为负」是等价定义，故直接判 `a < 0` 最简洁正确（见 [en_cl_fix_pkg.py:119-121](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L119-L121)）。

---

### 4.2 字段提取：cl_fix_sign / cl_fix_int / cl_fix_frac

#### 4.2.1 概念说明

这三个函数按 `[S,I,F]` 把一个定点数切成三段：符号位、整数位、小数位。它们与 4.1 的「单位读写」区别在于**按字段整体切**而非逐位。

本讲最关键的跨语言差异就发生在这里——**三套实现返回值的「域」不同**：

| 函数 | VHDL 返回（位串域） | Python 返回（实数域） | MATLAB 返回（实数域） |
|------|---------------------|----------------------|----------------------|
| `cl_fix_sign` | `std_logic`（0/1 位） | `int`（0/1） | `logical`（a<0） |
| `cl_fix_int` | `std_logic_vector`，整数位**原始位串** | `float`，`np.floor(a)` **带符号实数** | `mod(floor(a), 2^I)` **无符号整数** |
| `cl_fix_frac` | `std_logic_vector`，小数位**原始位串** | `float` ∈ [0,1)，小数**实数值** | `(a-floor(a))·2^F`，小数位**整数计数** |

把同一个数 \(-1.25\) 放进 `(true,2,2)`（\(W=5\)，补码位串 `11011`）对照：

| 提取 | VHDL（位串） | Python（实数） | MATLAB（实数） |
|------|-------------|---------------|----------------|
| sign | `'1'` | `1` | `1` (true) |
| int | `"10"`（原始整数位，按无符号读 = 2） | `-2.0`（floor，带符号） | `mod(-2,4) = 2` |
| frac | `"11"`（原始小数位，按无符号读 = 3） | `0.75`（实数值） | `0.75·4 = 3` |

规律很清楚：**把 VHDL 的位串按无符号整数解读，与 MATLAB 的返回值一致**（int=2、frac=3）；**Python 则走带符号实数**（int=-2、frac=0.75）。理解这一点，是 4.3 能否正确重组的前提。

#### 4.2.2 核心流程

**`cl_fix_sign`**：有符号返回最高位（即「是否为负」），无符号恒为 0。

**`cl_fix_int`（VHDL）**：在位串上切出整数字段 `[IntBits+FracBits-1 : FracBits]`，长度 `max(1, IntBits)`——`max(1,…)` 保证 `IntBits ≤ 0` 时也至少返回 1 位（全零）。`FracBits < 0` 时切片下界伸到负索引（虚拟小数位位置），但语义仍是「取出整数位那一段」。

**`cl_fix_int`（Python）**：直接 `np.floor(a)` 取整数部分（带符号）。若 `FracBits < 0`，结果格式 `(Signed, IntBits, 0)` 可能落到大位宽，经 `cl_fix_is_wide` 判定后转 `wide_fxp`（见 [en_cl_fix_pkg.py:64-80](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L64-L80)）。

**`cl_fix_int`（MATLAB）**：`mod(floor(a), 2^IntBits)`——先 floor 再对 \(2^I\) 取模，得到与 VHDL 位串一致的无符号整数。

**`cl_fix_frac`（VHDL）**：切出小数字段 `[FracBits-1 : 0]`，长度 `max(1, FracBits)`；`IntBits < 0` 时切片上界相应下移。

**`cl_fix_frac`（Python）**：返回小数实数值。对有符号负数先加偏移 \(2^{IntBits}\) 把它折回 \([0, 2^{IntBits})\)（丢掉符号位的影响），再 `a % 2^min(IntBits,0)`。当 `IntBits ≥ 0` 时 `min(IntBits,0)=0`，即 `a % 1`，得到 \([0,1)\) 的小数部分；`IntBits < 0` 时小数部分实际跨越更多位（含「隐式小数位」），故取模上界随 `IntBits` 变化。

**`cl_fix_frac`（MATLAB）**：`(a-floor(a)) * 2^FracBits`，返回小数位的整数计数（与 VHDL 位串一致）。

#### 4.2.3 源码精读

**VHDL 三件套**——注意 `max(1, …)` 与负位切片：

- [vhdl/src/en_cl_fix_pkg.vhd:1435-1446](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1435-L1446) —— `cl_fix_sign`：有符号返回 `a_v(a_v'high)`（最高位），无符号返回 `'0'`。
- [vhdl/src/en_cl_fix_pkg.vhd:1450-1468](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1450-L1468) —— `cl_fix_int`：`result_v` 长度 `max(1, IntBits)`，初值全零；按 `IntBits>0` / `FracBits>=0` 两个条件选切片范围，把整数字段拷进去。
- [vhdl/src/en_cl_fix_pkg.vhd:1472-1490](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1472-L1490) —— `cl_fix_frac`：对称结构，`result_v` 长度 `max(1, FracBits)`。

**Python 三件套**——实数域算术，并带 wide 派发：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:58-62](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L58-L62) —— `cl_fix_sign`：无符号返回 `0`，否则 `np.where(a<0, 1, 0)`。
- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:64-80](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L64-L80) —— `cl_fix_int`：wide 走 `a.floor()`，narrow 走 `np.floor(a)`。
- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:82-103](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L82-L103) —— `cl_fix_frac`：关键在最后一句 `return a % 2**min(aFmt.IntBits, 0)` 与负数偏移 `a + 2**IntBits`。

**MATLAB**：[cl_fix_sign.m:17-19](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_sign.m#L17-L19)、[cl_fix_int.m:16-18](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_int.m#L16-L18)、[cl_fix_frac.m:20-22](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_frac.m#L20-L22)。

**测试用例**供你对照：VHDL [en_cl_fix_pkg_tb.vhd:741-755](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L741-L755)，Python [en_cl_fix_pkg_test.py:670-695](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L670-L695)。注意 Python 测试 `test_SignedNeg` 断言 `cl_fix_int(-1.25, (True,2,2)) == -2`，正是「带符号 floor」的体现。

#### 4.2.4 代码实践

**实践目标**：亲手验证上表三语言的取值差异，重点体会 Python `cl_fix_int`/`cl_fix_frac` 的实数域语义。

**操作步骤**：

```python
# 示例代码
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *

fmt = FixFormat(True, 2, 2)
for v in [3.25, -1.25]:
    print(f"v={v}: sign={cl_fix_sign(v, fmt)}, "
          f"int={cl_fix_int(v, fmt)}, frac={cl_fix_frac(v, fmt)}")
```

**需要观察的现象**：

- `v=3.25`：`sign=0, int=3.0, frac=0.25`。
- `v=-1.25`：`sign=1, int=-2.0, frac=0.75`。注意 `int` 是**带符号**的 `-2`（而非 VHDL 位串 `"10"` 解读出的 `2`），`frac` 是**实数值** `0.75`（而非 MATLAB 的小数位计数 `3`）。

**预期结果**：如上。这一差异是 4.3 重组能否成功的关键，**待本地运行确认**。

#### 4.2.5 小练习与答案

**练习 1**：对 `(true,-2,4)`（`IntBits=-2`）这种「负整数位」格式，Python `cl_fix_int` 返回的 `floor` 值与「整数位」是什么关系？

**答案**：负整数位意味着数值范围被压到 0 附近（如 `(true,-2,4)` 范围 \(-0.25 … +0.1875\)）。`np.floor(a)` 仍按实数取下整，对范围内任何值都得到 0 或 -1，**并不**对应「整数位字段」（该格式根本没有真正的整数位字段）。VHDL 此时 `IntBits≤0`，`cl_fix_int` 返回长度 `max(1,-2)=1` 的全零位串——两者语义都指向「没有有效整数位」，但表达方式迥异。

**练习 2**：为什么 Python `cl_fix_frac` 对有符号负数要先 `a = a + 2**IntBits`？

**答案**：补码下负数的小数位与「把它折回正区间后的数」的小数位相同。例如 `-1.25` 在 `(true,2,2)` 折回 `−1.25+4=2.75`，而 `2.75` 的小数部分正是 `0.75`，与 `-1.25` 的原始小数位 `"11"`（=0.75）一致。这一步等价于「丢掉符号位的负权影响，只看低位」，见 [en_cl_fix_pkg.py:97-100](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L97-L100)。

---

### 4.3 字段重组：cl_fix_combine

#### 4.3.1 概念说明

`cl_fix_combine(sign, int, frac, result_fmt)` 是 4.2 三个函数的逆操作：把符号位、整数位、小数位重新拼成一个 `result_fmt` 格式的定点数。它的核心公式（narrow 路径）是

\[
\text{value} = -\text{sign}\cdot 2^{I} \;+\; \text{int} \;+\; \text{frac}\cdot 2^{-F}
\]

其中 `frac` 是**小数位的整数计数**（即小数位字段按无符号读出的整数），而非小数实数值。这一点由函数自身文档坐实：

> Python docstring：`combine(0, 5, 1, FixFormat(True, 4, 2)) <==> 5.25`
> 验证：\(-0\cdot 2^4 + 5 + 1\cdot 2^{-2} = 5 + 0.25 = 5.25\) ✓（`frac=1` 即一个 LSB = 0.25）

> MATLAB 文档：`cl_fix_combine(0, 3, 15, cl_fix_format(true,5,4)) --> 3.9375`
> 验证：\(3 + 15\cdot 2^{-4} = 3 + 0.9375 = 3.9375\) ✓

**两个重要约束与陷阱**：

1. **无符号格式不允许 sign=1**：VHDL 用 `assert sign='0' ... severity failure`（[en_cl_fix_pkg.vhd:1518-1520](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1518-L1520)），MATLAB 用 `error(...)`（[cl_fix_combine.m:25-27](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_combine.m#L25-L27)），而 **Python 不做此检查**（[en_cl_fix_pkg.py:105-112](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L105-L112) 无 assert）——若误传，会得到一个对无符号格式而言非法的负值。这是真实的跨语言差异，跨语言调用需自行保证。
2. **Python 的「提取 → 重组」不能直接往返**：因为 4.2 中 Python `cl_fix_frac` 返回的是小数**实数值**（如 0.75），而 `cl_fix_combine` 的 `frac` 形参期望的是**整数计数**（如 3）。直接把 `cl_fix_frac` 的输出喂给 `cl_fix_combine` 会多除一次 \(2^F\)，结果错误。MATLAB 不存在此问题，因为它的 `cl_fix_frac` 本就返回整数计数。

#### 4.3.2 核心流程

**VHDL**——按字段长度做位串拼接，分「有符号 / 无符号」与「IntBits/FracBits 正负」四种切片组合，最后把 `sign & int_slice & frac_slice` 拼成 `result_v`。

**Python narrow**——直接套公式：`-sign*2**IntBits + int + frac*2**(-FracBits)`。

**Python wide**（位宽 > 53）——内部以「未归一化大整数」存储（`data = value * 2^FracBits`），所以符号位的权重放大成 \(2^{I+F}\)：

\[
\text{data} = -\text{sign}\cdot 2^{I+F} \;+\; \text{int}\cdot 2^{F} \;+\; \text{frac}
\]

见 [en_cl_fix_pkg.py:108-110](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L108-L110)。

**MATLAB**——与 Python narrow 同公式：`-sign*2^IntBits + int + frac*2^-FracBits`。

**正确往返的换算**（Python）：若想用 4.2 提取出的实数值重组回原数，需先把它们转成 `combine` 期望的「整数计数 / 无符号」约定：

```
s = cl_fix_sign(a, fmt)                              # 0/1
i = int(cl_fix_int(a, fmt)) % 2**fmt.IntBits         # 带符号 floor -> 无符号整数位计数
f = round(cl_fix_frac(a, fmt) * 2**fmt.FracBits)     # 小数实数值 -> 小数位整数计数
reconstructed = cl_fix_combine(s, i, f, fmt)         # == a（量化意义上）
```

#### 4.3.3 源码精读

- [vhdl/src/en_cl_fix_pkg.vhd:1494-1533](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1494-L1533) —— VHDL `cl_fix_combine` 全貌：无符号分支先 `assert sign='0' severity failure`；随后按 `Signed × (IntBits>0?) × (FracBits>0?)` 八种情况做 `sign & int_v(...) & frac_v(...)` 拼接，`IntBits≤0` 或 `FracBits≤0` 时相应省略该段切片。
- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:105-112](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L105-L112) —— Python `cl_fix_combine`：wide 与 narrow 两个公式分支，注意 wide 分支符号权重是 `2**(IntBits+FracBits)`。无符号 `sign=1` 不报警。
- [matlab/src/cl_fix_combine.m:23-29](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_combine.m#L23-L29) —— MATLAB 一行算术公式加无符号 `error` 校验。

**测试用例**：VHDL [en_cl_fix_pkg_tb.vhd:757-760](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L757-L760) 断言 `combine('1',"00","11",(True,2,2)) == from_real(-3.25,(True,2,2))`（验证 \(-4 + 0 + 0.75 = -3.25\)）；Python [en_cl_fix_pkg_test.py:697-700](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L697-L700) 断言 `combine(1, 0, 3, (True,2,2)) == -3.25`。

#### 4.3.4 代码实践

**实践目标**：用文档里的 `(true,4,2)` 示例验证 `combine` 的整数计数约定，并演示「直接喂 `cl_fix_frac` 实数值」的错误用法。

**操作步骤**：

```python
# 示例代码
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *

fmt = FixFormat(True, 4, 2)

# 1) 正确用法：frac 传整数计数（文档示例）
print("combine(0, 5, 1, fmt) =", cl_fix_combine(0, 5, 1, fmt))   # 期望 5.25

# 2) 错误用法：把 cl_fix_frac 的实数值直接当 frac 传
f_real = cl_fix_frac(5.25, fmt)      # 0.25
print("frac(5.25) =", f_real)
print("combine(0, 5, 0.25, fmt) =", cl_fix_combine(0, 5, f_real, fmt))  # 5.0625（错！）

# 3) 正确换算后再 combine
f_count = round(f_real * 2**fmt.FracBits)   # 0.25*4 = 1
print("combine(0, 5, 1, fmt) =", cl_fix_combine(0, 5, f_count, fmt))    # 5.25（对）
```

**需要观察的现象**：步骤 1 与步骤 3 都得 `5.25`；步骤 2 得 `5.0625`，恰好比正确值少 \(0.25 \cdot (1 - 2^{-2})\)，正是「多除一次 \(2^F\)」的可视化证据。

**预期结果**：如上。**待本地运行确认**（数值依据 docstring 与单元测试交叉推导）。

#### 4.3.5 小练习与答案

**练习 1**：用 `cl_fix_combine` 构造 `(true,2,2)` 下的 `-3.25`，应传入哪四个参数？并用公式验证。

**答案**：`combine(1, 0, 3, (True,2,2))`。验证：\(-1\cdot 2^2 + 0 + 3\cdot 2^{-2} = -4 + 0.75 = -3.25\) ✓。`-3.25` 的 5 位补码为 `10011`：sign=1、int 字段 `"00"`=0、frac 字段 `"11"`=3。

**练习 2**：若误对无符号格式 `(false,3,2)` 调用 Python `combine(1, 2, 1, ...)`，会发生什么？VHDL / MATLAB 呢？

**答案**：Python 不检查，返回 \(-1\cdot 8 + 2 + 0.25 = -5.75\)——一个对无符号格式非法的负值（见 [en_cl_fix_pkg.py:111-112](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L111-L112)）。VHDL 会触发 `assert ... severity failure` 中止仿真；MATLAB 会抛 `error`。跨语言协作时应统一由调用方保证 sign=0。

**练习 3**：为什么 wide 路径里符号位权重是 \(2^{I+F}\) 而 narrow 是 \(2^I\)？

**答案**：`wide_fxp` 内部把定点数存成「放大 \(2^F\) 倍的大整数」`data`（详见 u6-l1/u6-l2）。符号位对**实数值**贡献 \(-2^I\)，对应到 `data` 上就是 \(-2^I \cdot 2^F = -2^{I+F}\)，所以公式里写 `2**(IntBits+FracBits)`，见 [en_cl_fix_pkg.py:108-110](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L108-L110)。

## 5. 综合实践

把本讲三个最小模块串起来，完成规格要求的「提取 → 重组 → 翻转符号位」全流程。

**任务**：取 `(true,3,4)` 格式的值 `2.6875`，依次用 `cl_fix_sign` / `cl_fix_int` / `cl_fix_frac` 提取三段；用正确的整数计数约定经 `cl_fix_combine` 重组，验证等于原值；最后用 `cl_fix_set_msb` 翻转最高符号位，观察数值如何变化。

**操作步骤**：

```python
# 示例代码：bit_roundtrip.py
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *

fmt = FixFormat(True, 3, 4)          # W = 1+3+4 = 8 位
a   = 2.6875                          # 补码 0_010_1011

# ---- 步骤 1：字段提取 ----
s = cl_fix_sign(a, fmt)               # 0
i_real = cl_fix_int(a, fmt)           # 2.0 （带符号 floor）
f_real = cl_fix_frac(a, fmt)          # 0.6875 （小数实数值）
print(f"sign={s}  int={i_real}  frac={f_real}")

# ---- 步骤 2：换算到 combine 期望的「整数计数 / 无符号」约定 ----
i_cnt = int(i_real) % 2**fmt.IntBits        # 2
f_cnt = round(f_real * 2**fmt.FracBits)     # 0.6875*16 = 11
print(f"int_count={i_cnt}  frac_count={f_cnt}")

# ---- 步骤 3：重组并验证 ----
reconstructed = cl_fix_combine(s, i_cnt, f_cnt, fmt)
print(f"reconstructed={reconstructed}  (original={a})  equal={reconstructed==a}")

# ---- 步骤 4：翻转最高符号位 ----
flipped = cl_fix_set_msb(a, fmt, 0, 1)      # sign 0->1
print(f"flip sign bit: {a} -> {flipped}")
```

**需要观察的现象与预期结果**：

1. 步骤 1：`sign=0, int=2.0, frac=0.6875`。注意 `2.6875` 的 8 位补码为 `00101011`：整数位字段 `"010"`=2，小数位字段 `"1011"`=11，`11/16 = 0.6875`，与 Python 返回的小数实数值吻合。
2. 步骤 3：`reconstructed == a` 为 `True`，往返成功（前提是用了步骤 2 的换算；若直接 `combine(s, i_real, f_real, fmt)` 会得 `2.043`，复现 4.3.4 的陷阱）。
3. 步骤 4：翻转符号位后 `2.6875 -> -5.3125`。因为符号位权重为 \(-2^3 = -8\)，\(2.6875 - 8 = -5.3125\)，数值由正变负，正合补码语义。

> 作者注：受运行环境所限（无 numpy），上述数值是依据源码逻辑与 VHDL testbench 断言手算交叉验证的结果，**请本地运行 `python3 bit_roundtrip.py` 确认**。手算依据：`cl_fix_set_msb` 的符号位权重公式见 [en_cl_fix_pkg.py:138-140](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L138-L140)。

## 6. 本讲小结

- **索引双约定**：`*_msb` 从最高位往下数（`index=0` 是符号位/最高位），`*_lsb` 从最低位往上数（`index=0` 是最低位），二者满足 `msb_index + lsb_index = W - 1`；Python 的 `get_lsb`/`set_lsb` 直接委派给 `get_msb`/`set_msb`。
- **位串域 vs 实数域**：VHDL 用位串切片/拼接读写位与字段，一步到位；Python/MATLAB 用「缩放到 0.5 边界再取半」读位、用「读出当前位 ± 权重」改位。
- **字段返回值的跨语言差异是本讲核心**：`cl_fix_int` 在 VHDL 返回原始整数位串、MATLAB 返回无符号取模整数、Python 返回带符号 floor 实数；`cl_fix_frac` 在 VHDL 返回原始小数位串、MATLAB 返回小数位整数计数、Python 返回小数实数值。
- **`cl_fix_combine` 三语言同公式** \(-\text{sign}\cdot 2^I + \text{int} + \text{frac}\cdot 2^{-F}\)，`frac` 是整数计数；wide 路径符号权重放大为 \(2^{I+F}\)。
- **无符号 sign=1 约束**：VHDL `severity failure`、MATLAB `error`、Python 不检查——跨语言调用需自理。
- **Python 往返陷阱**：`cl_fix_frac` 返回实数值，不能直接喂给 `cl_fix_combine`，需先 `* 2^FracBits` 转成整数计数；MATLAB 无此问题。

## 7. 下一步学习建议

- **Unit 5（文件 IO）**：`cl_fix_get_bits_as_int` / `from_bits_as_int`（u3-l1 已引入）把整个位串打包成整数用于跨语言数据交换，本讲的「按字段切位」思维是理解其打包权重的基础。
- **Unit 6（wide_fxp）**：本讲多次出现 `cl_fix_is_wide` 派发与 `wide_fxp` 的整数实现（`get_msb` 用 `>>` 与 `% 2`、`frac_part` 用大整数取模）。想彻底搞清 >53 位路径，请进入 u6-l1、u6-l2。
- **Unit 7（架构深度）**：`cl_fix_compare`（u3-l4、u7-l2）翻转符号位把补码映射为偏移二进制再做无符号比较——与本讲 `cl_fix_sign`「符号位即正负」的认知一脉相承，可对照阅读。
- **源码延伸**：通读 [en_cl_fix_pkg.vhd 的 Bit Manipulation 段](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L323-L380) 与对应 Python 实现，体会「同一语义、三种域、三套写法」的位真一致性是如何落到具体代码上的。
