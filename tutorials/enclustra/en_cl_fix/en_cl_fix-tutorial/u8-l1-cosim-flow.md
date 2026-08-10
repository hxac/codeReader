# cosim 验证流程总览：Python 生成黄金参考

## 1. 本讲目标

本讲是专家层「验证」单元的第一课。学完后你应当能够：

- 说清楚 en_cl_fix 为什么用「Python 算黄金参考、VHDL 仿真对拍」这种 co-simulation（协同仿真，下文简称 cosim）方式来验证三语言一致性。
- 读懂 `cosim_utils.py` 里 `get_data` / `repeat_each_value` / `repeat_whole_array` / `ProgressReporter` 这组工具，理解它们如何用「计数器穷举 + 笛卡尔积」生成测试激励。
- 逐段读懂 `cl_fix_add/cosim.py` 的 `run()`：它如何嵌套循环穷举所有格式与模式组合，用 Python 模型算出期望输出并落盘。
- 准确说出 `data/` 目录下四类文件（`test{N}_output.txt`、`a_fmt/b_fmt/r_fmt.txt`、`rnd.txt`、`sat.txt`）各自的含义与消费方，并理解「只存输出、不存输入」这个关键设计。

本讲**只讲 Python 这一侧**（生成黄金参考）。VHDL testbench 如何读文件、重生成输入并逐位比对，留给下一讲 u8-l3；VUnit 如何调度整个仿真，留给 u8-l2。

## 2. 前置知识

本讲假设你已经掌握：

- **定点格式 `[S, I, F]`**：S 符号位、I 整数位、F 小数位，总位宽 `S+I+F`（见 u1-l2）。
- **Python 主接口 `cl_fix_*`**：算术函数遵循「算 mid_fmt → 无损运算 → resize 到 r_fmt」三段式，`r_fmt` 缺省即全精度（见 u2-l2）。
- **算术链路 `convert → compute → resize`**：尤其是 `cl_fix_add` 的调用形式（见 u5-l2）。
- **NarrowFix / WideFix 双表示**：`cl_fix_is_wide(fmt)` 即 `width > 53`，决定走哪条路径（见 u6-l1～u6-l3）。

几个本讲会用到的补充事实（已在前面讲义确认）：

- `FixRound` 是 0～6 的枚举（`Trunc_s=0` … `ConvOdd_s=6`），`FixSaturate` 是 0～3 的枚举（`None_s=0` … `SatWarn_s=3`）。每个枚举成员的 `.value` 就是它声明时的整数。
- `cl_fix_to_integer(a, fmt)` 返回**非归一化原始整数**（即把定点比特当普通整数读出来）；`cl_fix_from_integer` 是其逆运算。
- `cl_fix_from_real` 固定做半进位量化 + 强制饱和，**不接受 `None_s`/`Warn_s`（回绕）**，也没有 `rnd` 参数。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [bittrue/cosim/cosim_utils.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cosim_utils.py) | 所有 cosim 脚本共用的工具函数库：清空目录、穷举取值、做笛卡尔积、打印进度。 |
| [bittrue/cosim/cl_fix_add/cosim.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py) | 二元算术 `cl_fix_add` 的 cosim 脚本：穷举 a_fmt/b_fmt/r_fmt/rnd/sat，算黄金参考并写盘。本讲的主精读对象。 |
| [bittrue/cosim/cl_fix_from_real/cosim.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_from_real/cosim.py) | 一元转换 `cl_fix_from_real` 的 cosim 脚本，结构与 add 类似但只有单输入、无 rnd、sat 受限。用作对照。 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | Python 参考模型本体。cosim 调用其中的 `cl_fix_add`、`cl_fix_from_real`、`cl_fix_min_value/max_value`、`cl_fix_write_formats` 等。 |
| [tb/cl_fix_add_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd) | add 的 VHDL testbench，是 cosim 数据的**消费方**。本讲只引用它来佐证文件约定，详细讲解在 u8-l3。 |

仓库里 `bittrue/cosim/` 下每个运算（`cl_fix_sub`、`cl_fix_mult`、`cl_fix_round`、`cl_fix_resize`、`cl_fix_shift`、`cl_fix_abs`、`cl_fix_neg`、`cl_fix_compare`、`cl_fix_addsub`）都有一个结构几乎相同的 `cosim.py`，本讲以 `cl_fix_add` 为代表讲透，其余可举一反三。

## 4. 核心概念与源码讲解

### 4.1 cosim 验证总览：Python 黄金参考与穷举对拍

#### 4.1.1 概念说明

en_clustra 这套库的灵魂是 **bit-true（位级精确）**：VHDL、Python、MATLAB 三种语言对同一个运算必须产出完全相同的比特。要证明这一点，最直接的办法就是拿 Python 当「标准答案」去逐位比对 VHDL 的输出——这套做法叫 **co-simulation（协同仿真）**，README 还专门推荐了一期 webinar《Fixed-Point Python Co-simulation》。

这里有几个关键概念：

- **黄金参考（golden reference）**：用 Python 参考模型算出来的、被认为绝对正确的期望输出。它会被写进文件，供 VHDL testbench 比对。
- **穷举（exhaustive）**：对于小位宽格式（测试范围里 `S+I+F` 一般 ≤ 5），把该格式**所有可能取值**都跑一遍。这比随机生成激励强得多——随机永远无法保证覆盖到「最负值」「舍入进位顶破上限」这类边界。
- **文件交换**：Python 和 VHDL 不在同一个进程里跑，靠 `data/` 目录下的文本文件传数据。Python 写、VHDL 读。
- **只存输出、不存输入**：这是整套设计最聪明的一点（下一节细讲）。

#### 4.1.2 核心流程

一次 cosim 验证分三段，跨两个进程：

```
┌─────────────────────────────────────────────────────────────┐
│  进程 A：Python cosim 脚本（本讲主讲）                        │
│                                                             │
│  for 每种 (a_fmt, b_fmt, r_fmt, rnd, sat) 组合：              │
│      1. get_data(a_fmt) → 穷举 a 的全部取值（计数器）          │
│      2. get_data(b_fmt) → 穷举 b 的全部取值                   │
│      3. repeat_* → 生成 a×b 的笛卡尔积（全部配对）             │
│      4. r = cl_fix_add(a_all, b_all, ...) ← 黄金参考          │
│      5. 把 r 写成 test{N}_output.txt                         │
│  把所有 fmt/rnd/sat 汇总写成索引文件                          │
└──────────────────────────────┬──────────────────────────────┘
                               │ 写入 data/*.txt
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  data/ 目录（文件交换桥梁）                                   │
│  test0_output.txt  test1_output.txt  ...                     │
│  a_fmt.txt  b_fmt.txt  r_fmt.txt  rnd.txt  sat.txt           │
└──────────────────────────────┬──────────────────────────────┘
                               │ 读取
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  进程 B：VHDL testbench（u8-l3 主讲）                         │
│                                                             │
│  读 a_fmt/b_fmt/r_fmt/rnd/sat → 知道每个测试用例的参数         │
│  for 每个测试用例 i：                                         │
│      读 test{i}_output.txt → 期望值 Expected                  │
│      用计数器「重新生成」输入（不读输入文件！）                  │
│      Result := cl_fix_add(...)(VHDL 版)                      │
│      逐位比对 Result == Expected ?                            │
└─────────────────────────────────────────────────────────────┘
```

**为什么「只存输出、不存输入」？** 因为输入是按固定规则（计数器从最小值到最大值）生成的，只要把**格式**告诉 testbench，testbench 就能用同一条规则把输入重新生成出来。这样 `data/` 里就不必存动辄上万行的输入向量，只存「期望输出 + 参数描述」，文件数和体积都小得多。两边用同一套计数规则，是这套设计成立的前提——4.2 节会精确给出这条规则。

#### 4.1.3 源码精读

整套流程的「契约」在 testbench 一侧体现得最清楚。看 [tb/cl_fix_add_tb.vhd:52-61](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L52-L61)：testbench 先用 `tb_path` 定位到对应的 `data/` 目录，再读入格式与模式。

```vhdl
constant DataPath_c : string := tb_path(runner_cfg) & "../bittrue/cosim/cl_fix_add/data/";
constant AFmt_c     : FixFormatArray_t := cl_fix_read_format_file(DataPath_c & "a_fmt.txt");
constant BFmt_c     : FixFormatArray_t := cl_fix_read_format_file(DataPath_c & "b_fmt.txt");
constant RFmt_c     : FixFormatArray_t := cl_fix_read_format_file(DataPath_c & "r_fmt.txt");
constant Rnd_c      : integer_vector := read_file(DataPath_c & "rnd.txt");
constant Sat_c      : integer_vector := read_file(DataPath_c & "sat.txt");
constant TestCount_c: positive := AFmt_c'length;   -- 测试个数 = 格式列表长度
```

这五行揭示了文件约定的全部：三个格式文件、两个模式文件，且**测试用例总数由格式列表的长度决定**。本讲要回答的核心问题就是——Python 那一侧，这些文件是怎么被「恰好」写成这个样子的。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在脑海里把上面那张三段式流程图和真实文件对应起来。

**步骤**：

1. 打开 [bittrue/cosim/cl_fix_add/cosim.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py)，找到所有 `np.savetxt(...)` 和 `cl_fix_write_formats(...)` 调用，数一数 Python 一共写了几类文件。
2. 打开 [tb/cl_fix_add_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd)，找到所有 `cl_fix_read_format_file(...)` 和 `read_file(...)` 调用，数一数 VHDL 一共读了几类文件。
3. 对照确认：写方和读方一一对应。

**预期现象**：Python 写 `test{N}_output.txt`、`a_fmt.txt`、`b_fmt.txt`、`r_fmt.txt`、`rnd.txt`、`sat.txt` 共 6 类；VHDL 读其中 5 类索引文件 + 按需读每个 `test{N}_output.txt`。注意 Python 从不写输入文件——这正是「只存输出」契约的体现。

#### 4.1.5 小练习与答案

**练习 1**：既然输入不存盘，万一 Python 生成输入的规则和 VHDL 重生成输入的规则不一致，会发生什么？

**答**：比对会在「正确」的用例上误报错误——因为两边把同一个 `test{N}_output.txt` 的第 i 行配给了不同的 (a,b) 输入。这正是为什么 4.2 节要精确定义计数器规则，且 4.3 节会强调 testbench 的嵌套循环顺序（b 外 a 内）必须和 Python 的 `repeat_*` 顺序对齐。

**练习 2**：为什么穷举只对「小位宽」可行？如果格式是 `[0, 16, 16]`（32 位）会怎样？

**答**：穷举的取值数 \(=2^{\text{width}}\)。32 位格式有 \(2^{32}\approx 4.3\times 10^9\) 个值，单算 a 一个输入就放不进内存，更别说笛卡尔积。所以 cosim 脚本里测试范围都压得很小（`I`、`F` 在 −2..2 或 −4..4），保证穷举量可控；大位宽的正确性靠 NarrowFix/WideFix 双路径自检（见 4.3.3）和算法本身的位级等价性来保证。

---

### 4.2 cosim_utils 工具函数库

#### 4.2.1 概念说明

`cosim_utils.py` 是所有 cosim 脚本的「公共脚手架」。每个运算的 cosim 脚本结构都一样，只有「算什么」不同，所以把重复的工具抽到一起：

- `clear_directory(path)`：每次运行前清空 `data/` 目录，避免残留旧文件干扰。
- `get_data(fmt)`：核心。返回某个格式下**全部可能取值**，按计数器从小到大排列。
- `repeat_each_value(x, n)` / `repeat_whole_array(x, n)`：把两个输入向量展开成笛卡尔积（全部两两配对）。
- `ProgressReporter`：因为穷举组合量巨大，打印进度百分比，让长时间运行的脚本不至于「看起来卡死」。

#### 4.2.2 核心流程

**`get_data` 的计数器规则**（全库最重要的约定）：

1. 用 `cl_fix_min_value(fmt)` / `cl_fix_max_value(fmt)` 求出该格式的最小/最大**归一化实数值**。
2. 用 `cl_fix_to_integer` 把它们转成**非归一化整数**下标 `int_min`、`int_max`。
3. `np.arange(int_min, 1+int_max)` 生成连续整数（注意闭区间，所以要 `1+`）。
4. `cl_fix_from_integer(...)` 把整数还原成定点实值数组。

效果：得到一个长度为 \(2^{\text{width}}\) 的数组，按整数值从小到大（也即按定点 real 值从小到大）排列。**全库任何地方提到「该格式的全部取值」，指的都是这个序列。**

**笛卡尔积规则**：设 a 有 La 个值、b 有 Lb 个值，要生成 La×Lb 个配对：

- `a_all = repeat_whole_array(a, Lb)`：整个 a 重复 Lb 次 → `[a0..aLa, a0..aLa, …]`（a 在快位置变化）。
- `b_all = repeat_each_value(b, La)`：每个 b 重复 La 次 → `[b0,b0,…(La 个), b1,b1,…]`（b 在慢位置变化）。

合起来第 `i = k*La + j` 个元素是 `(a[j], b[k])`——**外层循环 b、内层循环 a**。这条顺序在 4.3.3 会和 testbench 对上。

#### 4.2.3 源码精读

**清空目录**——[cosim_utils.py:33-38](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cosim_utils.py#L33-L38)：先 `rmtree`（目录不存在则吞掉 `FileNotFoundError`），再 `mkdir`，保证每次都是干净空目录。

**穷举取值**——[cosim_utils.py:40-45](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cosim_utils.py#L40-L45)，这就是上一节描述的计数器规则：

```python
def get_data(fmt : FixFormat):
    # Generate every possible value in format (counter)
    int_min = cl_fix_to_integer(cl_fix_min_value(fmt), fmt)
    int_max = cl_fix_to_integer(cl_fix_max_value(fmt), fmt)
    int_data = np.arange(int_min, 1+int_max)        # 闭区间
    return cl_fix_from_integer(int_data, fmt)
```

注意 `cl_fix_min_value` / `cl_fix_max_value` 内部会按 [en_cl_fix.py:87-104](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L87-L104) 自动在 narrow/wide 间分发，所以即便格式位宽超过 53，`get_data` 也不会出错。

**笛卡尔积**——[cosim_utils.py:47-51](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cosim_utils.py#L47-L51)，巧妙之处在于用同一个 `np.tile(x, (n,1))`、只换 flatten 的方向：

```python
def repeat_each_value(x, n):
    return np.tile(x, (n,1)).flatten(order='F')   # 列优先：每个值重复 n 次

def repeat_whole_array(x, n):
    return np.tile(x, (n,1)).flatten(order='C')   # 行优先：整个数组重复 n 次
```

`np.tile(x, (n,1))` 把一维 `x`（长度 L）铺成 `n×L` 矩阵；`'C'`（行优先）按行展开得到「整段重复」，`'F'`（列优先）按列展开得到「逐值重复」。两种展开方式配对就构成了笛卡尔积。

**进度报告**——[cosim_utils.py:58-83](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cosim_utils.py#L58-L83)：构造时传入若干参数列表，用 `np.prod` 算出总组合数；每次 `report()` 自增计数、换算成百分比，每跨过 10% 打印一次，结束时打印 `Done.`。它只挂在**最外层循环**上（见 4.3.3），用外层组合数估算进度。

#### 4.2.4 代码实践

**目标**：亲手验证 `get_data` 与两个 `repeat_*` 的行为，确认笛卡尔积顺序。

**步骤**（在仓库根目录执行，示例代码）：

```python
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *
sys.path.append("bittrue/cosim")
from cosim_utils import get_data, repeat_each_value, repeat_whole_array

# 1) 穷举一个 3 位无符号格式 [0,1,2] 的全部取值
fmt = FixFormat(0, 1, 2)
a = get_data(fmt)
print("a =", a)                      # 预期：[0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75]  共 8=2^3 个

# 2) 取另一个 2 位格式做笛卡尔积
b = get_data(FixFormat(0, 1, 1))     # [0, 0.5, 1, 1.5]  共 4 个
a_all = repeat_whole_array(a, len(b))
b_all = repeat_each_value(b, len(a))
for i in range(len(a_all)):
    print(a_all[i], b_all[i])
```

**需要观察的现象**：

- `a` 恰好 \(2^3=8\) 个值，从小到大、步长 \(2^{-2}=0.25\)。
- 打印出的配对应为 `(a0,b0),(a1,b0),(a2,b0),…,(a7,b0),(a0,b1),…`——即 **b 在外、a 在内**。

**预期结果**：共 `len(a)*len(b)=32` 行配对，无遗漏无重复，覆盖全部 (a,b) 组合。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`np.arange(int_min, 1+int_max)` 里那个 `1+` 去掉会怎样？

**答**：`np.arange` 是**左闭右开**，去掉 `1+` 就会漏掉最大值那一个取值，导致穷举少一个值，testbench 重生成的输入数量与 `test{N}_output.txt` 行数对不上，逐位比对整体错位。

**练习 2**：把 `repeat_each_value` 的 `order='F'` 改成 `order='C'`，它和 `repeat_whole_array` 还能配成正确的笛卡尔积吗？

**答**：不能。两个都改成 `'C'` 后，`a_all` 和 `b_all` 会变成同一种周期，配对会重复出现某些组合、又漏掉另一些，不再是完整笛卡尔积。两种展开方向**必须一快一慢**（一个 `'C'` 一个 `'F'`）才能错开成全配对。

---

### 4.3 cl_fix_add/cosim.run：穷举格式组合与黄金参考生成

#### 4.3.1 概念说明

`cl_fix_add/cosim.py` 是本讲的主精读对象，也是其余所有 cosim 脚本的模板。它做的事用一句话讲：**嵌套穷举所有 `(a_fmt, b_fmt, r_fmt, rnd, sat)` 组合，对每个组合用 Python 的 `cl_fix_add` 算出黄金参考，把结果与参数分别落盘。**

这里有几个设计要点：

- **测试点（test points）**：并不是穷举所有可能的 `I`、`F`（那会无穷多），而是在一个小范围里取一批代表性取值。`cl_fix_add` 里 `I`、`F` 都取 `np.arange(-2, 1+2)`（即 −2..2），`S` 取 `{0,1}`。负的 `I`/`F` 专门用来覆盖「小数点落在物理位之外」的边界格式（见 u1-l2）。
- **跳过无用格式**：`S+I+F < 1` 即位宽 < 1，是空格式，直接 `continue`。
- **附带自检**：在算黄金参考的循环里，**顺手**用 WideFix 路径再算一遍并 `assert` 相等。这不是 cosim 数据的一部分，而是「借 cosim 的穷举量」免费给 NarrowFix/WideFix 双路径做交叉验证（见 u6-l3）。

#### 4.3.2 核心流程

`run()` 的骨架（伪代码）：

```
clear_directory(data/)                         # 干净起点
test_count = 0; 列表 a_fmt/b_fmt/r_fmt/rnd/sat
progress = ProgressReporter((aS, aI, aF))      # 进度只挂最外层

for aS, aI, aF in 笛卡尔积(aS_values, aI_values, aF_values):
    progress.report()
    if aS+aI+aF < 1: continue                  # 跳过空格式
    a_fmt = FixFormat(aS,aI,aF)
    a = get_data(a_fmt)                        # 穷举 a（与 r/rnd/sat 无关，上提）

    for bS, bI, bF in ...:
        if bS+bI+bF < 1: continue
        b_fmt = FixFormat(bS,bI,bF)
        b = get_data(b_fmt)
        a_all, b_all = 笛卡尔积(a, b)           # repeat_*
        a_wide, b_wide = WideFix.from_narrowfix(...)   # 给自检用

        for rS, rI, rF in ...:
            if rS+rI+rF < 1: continue
            r_fmt = FixFormat(rS,rI,rF)
            for rnd in rnd_values:              # cl_fix_add 配置里只有 Trunc_s
                for sat in sat_values:          # 只有 None_s
                    r      = cl_fix_add(a_all,a_fmt, b_all,b_fmt, r_fmt, rnd, sat)   # 黄金参考
                    r_wide = a_wide.add(b_wide, r_fmt, rnd, sat)                      # 自检
                    assert array_equal(r_wide.to_real(), r)
                    savetxt(test{test_count}_output.txt, cl_fix_to_integer(r, r_fmt)) # 存输出
                    记录 a_fmt/b_fmt/r_fmt/rnd/sat 到列表
                    test_count += 1

print(test_count)
cl_fix_write_formats(test_a_fmt, ..., a_fmt.txt)   # 汇总写格式文件
cl_fix_write_formats(test_b_fmt, ..., b_fmt.txt)
cl_fix_write_formats(test_r_fmt, ..., r_fmt.txt)
savetxt(rnd.txt, test_rnd); savetxt(sat.txt, test_sat)
```

注意三个**上提（hoist）**优化：`get_data(a_fmt)` 只跟 a_fmt 有关，所以提到 b/r/rnd/sat 循环之外；同理 `get_data(b_fmt)` 提到 r/rnd/sat 之外。这避免了在海量内层组合里重复穷举同一组输入。

#### 4.3.3 源码精读

**导入与路径**——[cl_fix_add/cosim.py:23-33](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L23-L33)：先把 `models/python` 加入 `sys.path` 以 `import en_cl_fix_pkg`，再把上一级目录加入以 `import cosim_utils`。因为用 `dirname(__file__)` 解析路径，脚本可从任意目录运行。

**配置测试点**——[cl_fix_add/cosim.py:46-66](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L46-L66)：三种格式各一组 `S/I/F` 取值，外加 `rnd_values=[FixRound.Trunc_s]`、`sat_values=[FixSaturate.None_s]`。注意 add 的 cosim 只测最朴素的截断+回绕一对模式——round/sat 本身有专门的 `cl_fix_round`/`cl_fix_saturate` cosim 去穷举全部 7×4 种。

**最外层循环 + 进度 + 跳过 + 穷举 a**——[cl_fix_add/cosim.py:83-97](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L83-L97)：

```python
progress = ProgressReporter((aS_values, aI_values, aF_values))
for aS in aS_values:
    for aI in aI_values:
        for aF in aF_values:
            progress.report()
            if aS+aI+aF < 1: continue          # 跳过空格式
            a_fmt = FixFormat(aS, aI, aF)
            a = get_data(a_fmt)                 # 穷举 a，上提到 b 循环外
```

**笛卡尔积 + 构造 wide 自检输入**——[cl_fix_add/cosim.py:102-118](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L102-L118)：

```python
a_all = repeat_whole_array(a, len(b))
b_all = repeat_each_value(b, len(a))
a_wide = WideFix.from_narrowfix(NarrowFix(a_all, a_fmt))
b_wide = WideFix.from_narrowfix(NarrowFix(b_all, b_fmt))
```

这里 `repeat_whole_array(a, len(b))` + `repeat_each_value(b, len(a))` 正是 4.2 讲的「b 外 a 内」配对。注意它和 testbench [cl_fix_add_tb.vhd:85-86](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L85-L86) 的 `for b ... for a ...` 嵌套顺序**严格一致**——这是输出行与输入重新对齐的关键。

**内层：算黄金参考 + 自检 + 落盘**——[cl_fix_add/cosim.py:140-161](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L140-L161)：

```python
for sat in sat_values:
    r = cl_fix_add(a_all, a_fmt, b_all, b_fmt, r_fmt, rnd, sat)   # 黄金参考（默认走 NarrowFix）

    # 借 cosim 穷举量做 NarrowFix vs WideFix 自检，不是 cosim 数据
    r_wide = a_wide.add(b_wide, r_fmt, rnd, sat)
    assert np.array_equal(r_wide.to_real(), r)

    np.savetxt(join(DATA_DIR, f"test{test_count}_output.txt"),
               cl_fix_to_integer(r, r_fmt), fmt="%i", header=f"r[{r.size}]")
    test_a_fmt.append(a_fmt); test_b_fmt.append(b_fmt); test_r_fmt.append(r_fmt)
    test_rnd.append(rnd.value); test_sat.append(sat.value)
    test_count += 1
```

两个要点：(1) `cl_fix_add` 返回的 `r` 被立刻用 `cl_fix_to_integer` 转成**非归一化整数**再存盘——也就是说 `output.txt` 里是原始比特的整数值，不带比例因子，testbench 读回后用 `RFmt_c(i)` 重新解释。(2) 存的是 `rnd.value` / `sat.value`（枚举的整数 0/2/3…），testbench 用 `FixRound_t'val(...)` 按位置还原——之所以可行，是因为枚举声明顺序与赋值一一对应（见 [en_cl_fix_types.py:30-50](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L30-L50)）。

**汇总写索引文件**——[cl_fix_add/cosim.py:163-177](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L163-L177)：循环结束后，把累积的格式列表交给 `cl_fix_write_formats`（定义见 [en_cl_fix.py:458-472](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L458-L472)），它把每个格式写成 `str(fmt)` 即 `(S, I, F)` 一行，并在首行写逗号分隔的名字表头；`rnd`/`sat` 用 `np.savetxt` 写成整数列。

#### 4.3.4 对照：cl_fix_from_real/cosim.py 的差异

[cl_fix_from_real/cosim.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_from_real/cosim.py) 结构同构，但有三处值得对照的差异，正好印证前面讲义的约束：

1. **单输入**：只有 `a_fmt` 和 `r_fmt`，没有 `b_fmt`，也没有笛卡尔积。
2. **测试范围更大**：`I`/`F` 取 `np.arange(-4, 1+4)`（−4..4），因为单输入组合数远少于二元运算，可以放宽（见 [cl_fix_from_real/cosim.py:48-55](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_from_real/cosim.py#L48-L55)）。
3. **sat 受限、无 rnd**：[cl_fix_from_real/cosim.py:99-105](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_from_real/cosim.py#L99-L105) 只遍历 `(SatWarn_s, Sat_s)`，因为 `cl_fix_from_real` 强制饱和、不接受回绕；且它没有 `rnd` 参数（恒为半进位）。还有一个妙处：`get_data(a_fmt)` 返回的是 a_fmt 的全部**实数值**，这里直接当作 `cl_fix_from_real` 的浮点激励 `a` 传进去——输入既是「a_fmt 的全部取值」，又是「送给 from_real 的 real」。

#### 4.3.5 代码实践

**目标**：估算 `cl_fix_add/cosim.py` 默认配置下会生成多少个测试用例（即多少个 `test{N}_output.txt`）。

**步骤**：

1. 读 [cl_fix_add/cosim.py:46-66](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L46-L66) 的配置：`S∈{0,1}`、`I,F∈{-2,-1,0,1,2}`。
2. 手算「合法格式」数量（`S+I+F ≥ 1`）：S=0 时 I+F≥1 有 10 个；S=1 时 I+F≥0 有 15 个；合计 25 个合法格式。
3. 因为 `rnd` 只有 1 种、`sat` 只有 1 种，测试用例数 \(=25\times25\times25=15625\)。

**预期结果**：约 **15625** 个 `test{N}_output.txt` 文件（待本地验证）。这也解释了为什么脚本需要 `ProgressReporter`、为什么「只存输出不存输入」如此重要——若再存两份输入向量，文件体积会膨胀数倍。

> 提示：正因为默认配置生成的文件量巨大，**不建议直接运行未修改的脚本**。若想本地快速跑通，可临时把各 `*_values` 范围缩小（例如 `I,F` 都取 `{0,1}`），观察 `data/` 下生成的几类文件即可（这是你自己的本地实验，不必提交改动）。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `get_data(a_fmt)` 要放在 b_fmt 循环之外，而不是放在最内层？

**答**：`get_data` 只依赖 a_fmt，与 b/r/rnd/sat 无关。提到外层可以避免在最内层（r/rnd/sat 的海量组合）里重复穷举同一组 a 值，把复杂度从「组合数 × 穷举成本」降到「a_fmt 数 × 穷举成本」。

**练习 2**：循环里那段 `r_wide = a_wide.add(...); assert ...` 是 cosim 数据吗？去掉它会影响 testbench 对拍吗？

**答**：不是 cosim 数据，它是借 cosim 的穷举量顺便做的 NarrowFix↔WideFix 自检（脚本注释明确写了 "This is not actually part of the cosim data generation"）。去掉它不影响写盘的黄金参考，也不影响 testbench 对拍，但会丢失一个免费的双路径交叉验证机会。

**练习 3**：`cl_fix_add` 的 cosim 只测 `(Trunc_s, None_s)` 一对模式。如果要测全部 7×4 种 round/sat 组合，最自然的做法是什么？

**答**：不必改 `cl_fix_add/cosim.py`。round 与 sat 各自有独立的 `cl_fix_round/cosim.py` 和 `cl_fix_saturate/cosim.py` 去穷举全部模式。`cl_fix_add` 只负责验证「加法本身的位增长与对齐逻辑」是否在三语言间一致，舍入/饱和的正确性由专项 cosim 覆盖——这是合理的关注点分离。

---

### 4.4 输出文件约定：output / fmt / rnd / sat 四类文件

#### 4.4.1 概念说明

`data/` 目录是 Python 与 VHDL 之间的契约接口。理解这套约定，就理解了 cosim 的全部数据流。文件分四类：

| 文件 | 内容 | 写方（Python） | 读方（VHDL） |
|------|------|----------------|--------------|
| `test{N}_output.txt` | 第 N 个用例的黄金输出（非归一化整数列，表头 `r[size]`） | `np.savetxt` + `cl_fix_to_integer` | `cl_fix_read_file` |
| `a_fmt.txt` / `b_fmt.txt` / `r_fmt.txt` | 每行一个格式 `(S, I, F)`，首行是名字表头 | `cl_fix_write_formats` | `cl_fix_read_format_file` |
| `rnd.txt` | 舍入模式的整数码列 | `np.savetxt(test_rnd)` | `read_file` |
| `sat.txt` | 饱和模式的整数码列 | `np.savetxt(test_sat)` | `read_file` |

关键约定：**第 i 行的所有索引文件描述的是同一个测试用例 i**——`a_fmt.txt` 第 i 行、`b_fmt.txt` 第 i 行、`r_fmt.txt` 第 i 行、`rnd.txt` 第 i 行、`sat.txt` 第 i 行，加上 `test{i}_output.txt`，合起来完整描述「第 i 次运算」。测试用例总数 = 格式文件行数 = `AFmt_c'length`。

#### 4.4.2 核心流程

一次比对的数据往返：

```
Python 写                          VHDL 读
-----------                        -----------
a_fmt.txt  ─┐                      ┌─ AFmt_c(i)   （第 i 个用例的 a 格式）
b_fmt.txt  ─┤  同一行 i 描述同一    ├─ BFmt_c(i)
r_fmt.txt  ─┤  测试用例 i           ├─ RFmt_c(i)
rnd.txt    ─┤                      ├─ Rnd_c(i)    → FixRound_t'val 还原
sat.txt    ─┘                      └─ Sat_c(i)    → FixSaturate_t'val 还原
test{i}_output.txt ───────────────► Expected_c    （第 i 个用例的期望输出）
```

枚举码的往返尤其值得注意：Python 存 `rnd.value`（声明时的整数），VHDL 用 `'val()` 按位置还原。两者能对上，**仅因为 `FixRound`/`FixSaturate` 的声明顺序恰好与赋值一一对应**（0,1,2,…）。若哪天有人把枚举值改成非顺序赋值，这条隐式契约就会失效——这是一个值得警惕的脆弱点。

#### 4.4.3 源码精读

**写格式文件**——`cl_fix_write_formats`，[en_cl_fix.py:458-472](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L458-L472)：

```python
def cl_fix_write_formats(fmts, names, filename : str):
    with open(filename, "w") as fid:
        header = "# " + ",".join(names)      # 首行：# a_fmt0,a_fmt1,...
        fid.write(header + "\n")
        ...
        for fmt in fmts:
            fid.write(cl_fix_format_to_string(fmt) + "\n")   # 每行：(S, I, F)
```

`cl_fix_format_to_string` 调用的是 `FixFormat.__str__`，即 [en_cl_fix_types.py:369-370](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L369-L370) 的 `(S, I, F)` 形式。所以 `a_fmt.txt` 长这样：

```
# a_fmt0,a_fmt1,a_fmt2,...
(0, 0, 1)
(0, 0, 2)
(0, 1, -1)
...
```

**写输出文件**——已见 4.3.3，存的是 `cl_fix_to_integer(r, r_fmt)`，即**非归一化整数**，表头 `r[size]` 告诉 testbench 这一组有多少个值。

**VHDL 读回 + 比对**——[cl_fix_add_tb.vhd:73-101](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L73-L101) 的 `Check(i)` 过程，把约定落实为代码：

```vhdl
constant Expected_c : SlvArray_t := cl_fix_read_file(... & "test" & to_string(i) & "_output.txt", RFmt_c(i));
...
for b in Bmin to Bmax loop           -- 外层 b
    for a in Amin to Amax loop       -- 内层 a（与 Python repeat_* 顺序一致）
        Result_v := cl_fix_add(
            cl_fix_from_integer(a, AFmt_c(i)), AFmt_c(i),
            cl_fix_from_integer(b, BFmt_c(i)), BFmt_c(i),
            RFmt_c(i), FixRound_t'val(Rnd_c(i)), FixSaturate_t'val(Sat_c(i)));
        if Result_v /= Expected_c(Idx_v) then
            ... print 上下文 ...
            check_equal(Result_v, Expected_c(Idx_v), "Error at index " & to_string(Idx_v));
        end if;
        Idx_v := Idx_v + 1;
        wait until rising_edge(Clk);
    end loop;
end loop;
```

三个细节呼应前文：(1) 输入用 `for b / for a` 计数器重生成（`Amin..Amax` 正是 Python `get_data` 的整数范围），不读输入文件；(2) `FixRound_t'val(Rnd_c(i))` 把整数码还原成枚举；(3) 出错时打印完整的 `a + b [rnd, sat] --> r_fmt` 上下文，方便定位是哪个组合失败。

#### 4.4.4 代码实践

**目标**：把 4.4.1 的文件约定表逐条对应到源码行号。

**步骤**：

1. 在 [cl_fix_add/cosim.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py) 里找到写出 `a_fmt.txt` / `b_fmt.txt` / `r_fmt.txt` 的三条 `cl_fix_write_formats` 语句（[第 167-173 行附近](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L166-L173)）。
2. 在同一文件找到写 `rnd.txt` / `sat.txt` 的两条 `np.savetxt` 语句（[第 176-177 行](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L176-L177)）。
3. 在 [cl_fix_add_tb.vhd:55-61](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L55-L61) 找到对应的五条读取语句。

**预期结果**：每类文件都能找到「写一行 + 读一行」的精确对应，确认契约两端闭合。

#### 4.4.5 小练习与答案

**练习 1**：`test{N}_output.txt` 里存的是 real 值还是整数？为什么这样选？

**答**：存的是 `cl_fix_to_integer` 得到的**非归一化整数**（原始比特的整数值），不是 real。因为 VHDL 的 `cl_fix_read_file` 读回的是 `std_logic_vector`，按位比对必须用整数比特而非带比例因子的浮点；存整数让两端都能直接按比特解释，避免浮点文本往返的精度问题。

**练习 2**：若把 `FixRound` 枚举改成 `Trunc_s=0, NonSymPos_s=10, ...`（非连续赋值），cosim 会出什么问题？

**答**：Python 仍写 `rnd.value`（0,10,…），但 VHDL 的 `FixRound_t'val(N)` 是**按位置**还原（第 N 个成员），`'val(10)` 会越界报错或取到错误成员。这条「枚举码往返」依赖 Python `.value` 与 VHDL 位置严格一致这一隐式前提，非连续赋值会直接破坏它。

---

## 5. 综合实践

**任务**：精读（或缩小范围后运行）`cl_fix_add/cosim.py`，列出它在 `data/` 下生成的全部文件类别，并解释每个文件如何被 testbench 消费。

**操作步骤**：

1. **准备**：确认依赖已装（`pip install -r requirements.txt`，至少需要 numpy）。
2. **缩小范围**（可选，仅为快速跑通）：复制一份 `cl_fix_add/cosim.py` 到临时位置，把 `aI_values/aF_values/bI_values/bF_values/rI_values/rF_values` 都改成 `np.arange(0, 1+1)`（即 `{0,1}`），把 `aS_values/bS_values/rS_values` 保持 `[0,1]`。这是你自己的本地实验脚本，不要改仓库源码。
3. **运行**：`python <你的临时脚本>`，观察 `ProgressReporter` 的百分比输出与最后的 `Cosim generated N tests.`。
4. **列目录**：`ls bittrue/cosim/cl_fix_add/data/`，统计文件类别与数量。
5. **抽样查看**：打开 `a_fmt.txt` 看表头与 `(S, I, F)` 行；打开 `rnd.txt`/`sat.txt` 看整数码；打开任意一个 `test0_output.txt` 看输出整数列与 `r[size]` 表头。
6. **对照消费方**：打开 [tb/cl_fix_add_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd)，确认每个文件被哪一行读取、如何参与比对。

**需要观察的现象**：

- 进度按 10% 递增打印，结束时打印 `Done.` 与生成的测试数。
- `data/` 下出现：每个用例一个 `test{N}_output.txt`，外加 `a_fmt.txt`、`b_fmt.txt`、`r_fmt.txt`、`rnd.txt`、`sat.txt` 五个索引文件。
- 索引文件行数相等，等于 `test_count`；`a_fmt.txt` 首行是 `# a_fmt0,a_fmt1,...`。

**预期结果**（缩小范围后，待本地验证）：用 `{0,1}` 的 I/F、`{0,1}` 的 S，合法格式（`S+I+F≥1`）共 7 个，测试用例数 \(=7\times7\times7=343\)，故生成 343 个 `test{N}_output.txt` + 5 个索引文件。若不缩小范围直接跑，则约 15625 个输出文件（见 4.3.5）。

**反思题**：假如你新增了一个算子（比如 `cl_fix_mult` 已存在，你要加 `cl_fix_div`），需要复制哪些文件、改哪几处？——答：复制任意一个现有 `cosim.py` 为 `cl_fix_div/cosim.py`，改 `run()` 里的「算什么」（把 `cl_fix_add` 换成新函数）、调整输入个数与测试点范围、保持四类文件的写盘约定不变，再配套写一个读同样文件的 `cl_fix_div_tb.vhd` 并在 `sim/run.py` 注册（详见 u8-l2）。

## 6. 本讲小结

- **cosim = Python 算黄金参考 + VHDL 仿真逐位对拍**，靠 `data/` 目录的文本文件在两个进程间交换数据，是 en_clustra 证明三语言 bit-true 的核心手段。
- **穷举而非随机**：对小位宽格式用计数器遍历全部取值，能覆盖最负值、进位溢出等随机激励难触达的边界。
- **`cosim_utils.py` 是公共脚手架**：`get_data` 用 `min/max_value → 整数区间 → from_integer` 穷举取值；`repeat_each_value`（F 序）+ `repeat_whole_array`（C 序）配成笛卡尔积，顺序是「b 外 a 内」。
- **`cl_fix_add/cosim.py` 是模板**：嵌套穷举 `(a_fmt, b_fmt, r_fmt, rnd, sat)`，用 `cl_fix_add` 算黄金参考，顺手用 WideFix 做双路径自检，再把输出与参数落盘；`get_data` 等上提优化避免重复穷举。
- **四类文件约定**：`test{N}_output.txt`（黄金输出，非归一化整数）、`a/b/r_fmt.txt`（格式 `(S,I,F)`，每行一个）、`rnd.txt`/`sat.txt`（枚举整数码）；同一行 i 描述同一用例，测试总数由格式文件行数决定。
- **两个隐式契约需警惕**：(1) 输入不存盘，靠两边用同一计数规则重生成，故 testbench 的 `for b / for a` 顺序必须与 Python 的 `repeat_*` 顺序对齐；(2) 枚举码往返依赖 Python `.value` 与 VHDL `'val` 位置一致。

## 7. 下一步学习建议

本讲只讲了 cosim 的「Python 生成」这一半。要补全闭环，建议按序学习：

- **u8-l2 VUnit 仿真框架与 cosim_runner 单例调度**：看 `sim/run.py` 如何用 VUnit 把这些 testbench 编译运行起来，`cosim_runner` 如何用线程锁保证每个 cosim 脚本在一轮仿真里只跑一次（`pre_config` 回调触发），以及 `common.py` 如何配置 GHDL/NVC/Modelsim 等多仿真器。
- **u8-l3 VHDL testbench 模式与文件 I/O**：精读 `cl_fix_*_tb.vhd` 的「读格式 → 重生成输入 → 调 VHDL 函数 → 比对」全流程，以及 `en_cl_fix_fileio_pkg` 与 `en_tb` 库如何包装文件读写。
- 若想看其它算子的 cosim 变体，可对照阅读 `bittrue/cosim/cl_fix_round/cosim.py`（穷举 7 种舍入）和 `cl_fix_mult/cosim.py`（一元与二元混合的写法），它们与本讲的 `cl_fix_add` 结构同构。
