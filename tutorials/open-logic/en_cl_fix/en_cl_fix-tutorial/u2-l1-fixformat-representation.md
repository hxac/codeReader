# FixFormat [S,I,F] 定点表示

## 1. 本讲目标

本讲是理解整个 en_cl_fix 库的「地基」。读完本讲，你应当能够：

- 说清楚定点格式 `[S, I, F]` 中三个分量 `S`、`I`、`F` 各自代表什么、对应的位权重是多少。
- 对任意一个 `[S, I, F]`，手算出它的**总位宽**与**可表示的数值范围**（最大值、最小值）。
- 理解「负 I」「负 F」这类不直观的特殊格式到底意味着什么。
- 在 Python（`FixFormat` 类）与 VHDL（`FixFormat_t` record）两套实现里找到同一个格式定义，并理解它们如何一一镜像。
- 自己动手用 Python 创建 `FixFormat` 对象，验证 `.width` 属性。

本讲只讲「格式本身」，**不**涉及舍入、饱和、运算——那些会在后续讲义（u2-l2、u2-l3、u2-l4）展开。把格式彻底吃透，后面的内容会顺理成章。

## 2. 前置知识

在进入源码前，先用最朴素的方式建立两点直觉。

### 2.1 从十进制小数到二进制小数

十进制数 `12.34` 的每一位权重是 10 的幂：

| 位 | 1 | 2 | . | 3 | 4 |
|---|---|---|---|---|---|
| 权重 | \(10^1\) | \(10^0\) | 小数点 | \(10^{-1}\) | \(10^{-2}\) |

定点数（fixed-point）就是把同样的规则搬到二进制：每一位权重是 **2 的幂**，小数点的位置是**固定**的（所以叫「定点」）。例如二进制 `101.01`：

| 位 | 1 | 0 | 1 | . | 0 | 1 |
|---|---|---|---|---|---|---|
| 权重 | \(2^2\) | \(2^1\) | \(2^0\) | 小数点 | \(2^{-1}\) | \(2^{-2}\) |

其值为 \(4 + 0 + 1 + 0 + 0.25 = 5.25\)。

### 2.2 定点 vs 浮点：为什么 FPGA 爱用定点

- **浮点数**（如 IEEE754）：小数点位置会「浮动」，用一个独立的指数字段记录。表达范围大，但硬件实现复杂、耗资源。
- **定点数**：小数点位置在编译期就固定好，**没有指数字段**，硬件就是一个普通的整数运算单元加一个「心照不宣」的小数点位置。

对 FPGA/ASIC 来说，定点运算几乎等同于整数运算，面积小、速度快、功耗低，因此是数字信号处理（滤波器、FFT、控制环路）的主流选择。en_cl_fix 这类库存在的意义，就是帮你**正确地管理小数点的位置与位宽**，并保证 Python 模型和 VHDL 硬件算出**位级别完全一致**的结果。

### 2.3 二进制补码（two's complement）回顾

有符号定点用**补码**表示负数：最高位（符号位）的权重是**负的**。这是后面 `S=1` 时「符号位权重为 \(-2^I\)」的来源。本讲会在 4.3 节用公式严格说明。

## 3. 本讲源码地图

本讲涉及三个关键源文件，分别代表「文档」「Python 参考模型」「VHDL 金标准」三个视角：

| 文件 | 作用 | 本讲用到的内容 |
|---|---|---|
| [README.md](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md) | 项目文档 | `Fixed-Point Number Format` 章节，含位权重图与格式示例表 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | Python 类型定义 | `FixFormat` 类、构造断言、`width` 属性 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | VHDL 主包 | `FixFormat_t` record、`NullFixFormat_c`、`cl_fix_width`、`max_real`/`min_real` |

> 提示：承接 u1-l2 的「三语言镜像架构」——VHDL 是语义金标准，Python 同名同参数镜像。本讲你会清楚地看到，`FixFormat`（Python）和 `FixFormat_t`（VHDL）描述的是**同一件事**。

---

## 4. 核心概念与源码讲解

### 4.1 [S, I, F] 记法：三个数字定义一种格式

#### 4.1.1 概念说明

en_cl_fix 用一个三元组 `[S, I, F]` 来描述一种定点格式：

- `S` = **符号位**个数，只能是 0（无符号）或 1（有符号）。
- `I` = **整数位**个数（可以为负，见 4.6 节）。
- `F` = **小数位**个数（可以为负，见 4.6 节）。

这三个数字**唯一确定**了：数据有多少个二进制位、小数点在哪里、有没有符号位。整个库的所有运算结果格式，最终都是通过组合若干个 `[S, I, F]` 推导出来的。

注意一个关键设计选择：`I` 和 `F` 都是**普通整数，允许取负值**。这正是 en_cl_fix 比「位宽即格式」的朴素做法更强大之处——它能把「小数点位置」和「存储宽度」解耦。

#### 4.1.2 核心流程

给定 `[S, I, F]`，确定一个定点数语义的流程是：

1. **总位宽**：\(W = S + I + F\)（见 4.4 节）。
2. **最低位（LSB）权重**：永远是 \(2^{-F}\)。这一条是整个模型的「锚点」。
3. **从 LSB 往上数**，每一位权重依次是 \(2^{-F}, 2^{-F+1}, \dots, 2^{-F+W-1}\)。
4. **最高位（MSB）权重**：
   - 无符号（\(S=0\)）：\(2^{-F+W-1} = 2^{I-1}\)。
   - 有符号（\(S=1\)）：取负，即 \(-2^{-F+W-1} = -2^{I}\)（补码符号位）。
5. **范围**：由所有位的权重求和得到（见 4.3 节）。

这套「以 LSB 为锚、权重逐位翻倍」的模型对**任意** \(S, I, F\)（含负值）都成立，后面所有手算都基于它。

#### 4.1.3 源码精读

文档对格式的官方定义在 README：

[README.md:77-91](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L77-L91) —— 定义 `[S, I, F]` 三个分量的含义，并明确「总位宽就是 S+I+F」。

[README.md:93-97](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L93-L97) —— 关键一句：**有符号数的（补码）符号位权重为 \(-2^I\)**。这正是上面流程第 4 步的依据，旁边配了一张 `BitWeights.svg` 位权重示意图。

Python 端，`FixFormat` 类的文档字符串把同样的定义写进了代码：

[en_cl_fix_types.py:53-59](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L53-L59) —— `FixFormat` 类说明：`S=符号位数(0或1)`、`I=整数位数`、`F=小数位数`。

#### 4.1.4 代码实践

**实践目标**：用 Python 创建一个 `FixFormat`，直观确认它就是「三个数字」。

**操作步骤**（在仓库根目录执行）：

```python
# 文件名：exp_format.py（示例代码，非项目原有文件）
import sys
sys.path.append("bittrue/models/python")   # 与项目测试一致的导入方式
from en_cl_fix_pkg import FixFormat

fmt = FixFormat(1, 2, 1)          # [1,2,1]：1符号位 + 2整数位 + 1小数位
print(fmt)                         # 打印 -> (1, 2, 1)
print(repr(fmt))                   # 打印 -> FixFormat(1, 2, 1)
```

**需要观察的现象**：`print(fmt)` 输出 `(1, 2, 1)`，`repr` 输出 `FixFormat(1, 2, 1)`。这说明 `FixFormat` 对象的核心状态就是这三个数字，分别对应 `__str__` 与 `__repr__` 两个方法（见 4.5 节源码）。

**预期结果**：成功打印两行，无报错。

#### 4.1.5 小练习与答案

**练习 1**：`[0, 4, 0]` 一共有多少位？它有没有符号位？

> **答案**：\(W = 0+4+0 = 4\) 位；`S=0`，所以**无符号**。它能表示 0 到 15 的整数。

**练习 2**：如果一个格式有 1 个符号位、3 个整数位、2 个小数位，写成 `[S, I, F]` 是什么？总位宽多少？

> **答案**：`[1, 3, 2]`，总位宽 \(1+3+2=6\) 位。

---

### 4.2 位权重详解：把二进制串翻译成数值

#### 4.2.1 概念说明

「格式」回答了「位宽和小数点在哪」，而「位权重」回答了「拿到一串 0/1，它代表哪个数」。每一位的权重取决于它相对于小数点的位置——这和十进制完全同理，只是底数从 10 换成 2。

#### 4.2.2 核心流程

把 4.1.2 的模型画成一张位权重表（以 `[1, 2, 1]` 为例，位宽 4）：

| 位（从高到低） | 符号位 | 整数位 | 整数位 | 小数位 |
|---|---|---|---|---|
| 二进制 | 1 | 0 | 1 | 1 |
| 权重 | \(-2^2=-4\) | \(2^1=2\) | \(2^0=1\) | \(2^{-1}=0.5\) |
| 贡献 | \(-4\) | \(0\) | \(1\) | \(0.5\) |

求和：\(-4 + 0 + 1 + 0.5 = -2.5\)。这正是 README 示例表里 `[1,2,1]` 取 `101.1` 表示 \(-2.5\) 的来源。

验证锚点规则：LSB（最右）权重 = \(2^{-F} = 2^{-1} = 0.5\) ✓；MSB（符号位）权重 = \(-2^I = -2^2 = -4\) ✓。

#### 4.2.3 源码精读

README 给出的位权重示意图：

[README.md:93-97](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L93-L97) —— 文字说明符号位权重为 \(-2^I\)，并引用 `BitWeights.svg`。

README 用一个完整例子演示翻译过程（`1111110011100011` 解释为 `[0,11,5]` 得到 2023.09375）：

[README.md:99-105](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L99-L105) —— 把每一位的贡献列出并求和。注意这里是 `S=0`（无符号），所以最高位权重是正的 \(2^{10}=1024\)。

README 还汇总了一张「格式—范围—位模式—示例」对照表，建议对照阅读：

[README.md:109-116](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L109-L116) —— 含 `[1,4,-2]`、`[1,-2,4]` 等特殊格式（4.6 节细讲）。

#### 4.2.4 代码实践

**实践目标**：亲手把一串二进制按 `[1,2,1]` 翻译成数值，体会位权重。

**操作步骤**：

1. 取二进制串 `1011`，按 `[1,2,1]` 拆成「符号位=1，整数位=01，小数位=1」。
2. 仿照 4.2.2 的表格，逐位写出权重与贡献并求和。
3. 再取 `0111`，重复一次。

**需要观察的现象**：`1011` → \(-2.5\)；`0111` → \(2+1+0.5 = 3.5\)（无符号位为 1，所以全为正贡献）。

**预期结果**：`1011` = -2.5，`0111` = 3.5（与 README 表中 `[1,2,1]` 的范围 \(-4 \dots +3.5\) 一致；3.5 正是该格式的最大值）。

#### 4.2.5 小练习与答案

**练习 1**：`[0,4,2]` 下，二进制 `0101.01` 等于多少？

> **答案**：无符号。位权重 \(2^3,2^2,2^1,2^0,2^{-1},2^{-2}\)。`010101` → \(0+4+0+1+0+0.25 = 5.25\)。与 README 表一致。

**练习 2**：为什么 `[1,2,1]` 的最大值是 3.5 而不是 4？

> **答案**：要达到最大正值，符号位必须为 0（否则贡献 \(-4\)）。剩下 `011.1` → \(2+1+0.5=3.5\)。补码符号位「吃掉」了最顶上那一档正值。

---

### 4.3 表示范围：最大值与最小值

#### 4.3.1 概念说明

每种格式能表示的数值有一个确定的**闭区间** \([最小值, 最大值]\)。这个范围完全由 `[S, I, F]` 决定，与具体存的值无关。后续讲义讲「饱和」（u2-l3）时，判断「是否越界」就是拿当前值和这个范围比。

#### 4.3.2 核心流程

由 4.1.2 的权重模型，对位宽 \(W=S+I+F\) 的格式求所有位的最大/最小组合：

- **最大值**：让所有「正权重位」取 1、「负权重位」（仅符号位）取 0。对位权重从 LSB 到 MSB 求和（MSB 视符号决定正负），化简后得到：

\[
\mathrm{max} = 2^{I} - 2^{-F}
\]

无论 `S=0` 还是 `S=1`，这个公式都成立（有符号时符号位取 0，贡献为 0）。

- **最小值**：
  - 有符号（\(S=1\)）：只有符号位为 1、其余为 0，贡献即符号位权重：

\[
\mathrm{min}_{S=1} = -2^{I}
\]

  - 无符号（\(S=0\)）：没有负权重位，最小就是全 0：

\[
\mathrm{min}_{S=0} = 0
\]

汇总：

| | 公式 |
|---|---|
| 最大值（任意 S） | \(2^{I} - 2^{-F}\) |
| 最小值（\(S=1\)） | \(-2^{I}\) |
| 最小值（\(S=0\)） | \(0\) |

#### 4.3.3 源码精读

VHDL 包用两个**内部**辅助函数 `max_real` / `min_real` 直接实现了上面的公式：

[hdl/en_cl_fix_pkg.vhd:276-279](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L276-L279) —— `max_real` 返回 `2.0**fmt.I - 2.0**(-fmt.F)`，正是 \(2^I - 2^{-F}\)。

[hdl/en_cl_fix_pkg.vhd:281-288](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L281-L288) —— `min_real`：`S=1` 时返回 `-2.0**fmt.I`，否则返回 `0.0`，与上面最小值两条分支一致。

库还提供**位级别**的最大/最小值（返回 `std_logic_vector`，而非 `real`），用于硬件比较：

[hdl/en_cl_fix_pkg.vhd:370-378](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L370-L378) —— `cl_fix_max_value`：全 1，若有符号位则最高位清 0（对应「符号位取 0、其余取 1」的最大值位模式）。

[hdl/en_cl_fix_pkg.vhd:380-390](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L380-L390) —— `cl_fix_min_value`：有符号时全 0 但最高位置 1（仅符号位为 1 的最小值位模式）；无符号时全 0。

> 对照：这两个位模式与 4.3.2 的推导完全吻合——`cl_fix_max_value` = 「正权重位全 1、符号位 0」，`cl_fix_min_value`（有符号）= 「仅符号位 1」。

#### 4.3.4 代码实践

**实践目标**：手算 `[1,2,1]` 的范围，并对照源码公式验证。

**操作步骤**：

1. 用 4.3.2 的公式：\(I=2, F=1\)。
   - 最大值 \(= 2^2 - 2^{-1} = 4 - 0.5 = 3.5\)。
   - 最小值（\(S=1\)）\(= -2^2 = -4\)。
2. 对照 README 示例表 `[1,2,1]` 一行，范围写作 `-4 ... +3.5`。

**需要观察的现象**：手算结果与 README 表格、与 `max_real`/`min_real` 公式三者一致。

**预期结果**：`[1,2,1]` 范围 = \([-4,\ +3.5]\)。

#### 4.3.5 小练习与答案

**练习 1**：`[0,4,2]` 的最大值是多少？用公式和「全 1」两种方法各算一遍。

> **答案**：公式 \(2^4 - 2^{-2} = 16 - 0.25 = 15.75\)。位模式：6 位全 1 = 二进制 `111111`，按 `[0,4,2]` 权重 \(8+4+2+1+0.5+0.25=15.75\)。两者一致。

**练习 2**：为什么无符号格式的最小值恒为 0，而与 `I`、`F` 无关？

> **答案**：无符号（\(S=0\)）没有负权重位，所有位取 0 时贡献之和为 0，不可能更小。

---

### 4.4 总位宽 width：S+I+F

#### 4.4.1 概念说明

「总位宽」就是存储这种格式的数据需要的二进制位数。en_cl_fix 的定义极其简单：**总位宽 = S + I + F**。这个值在硬件里决定 `std_logic_vector` 的长度，在 Python 里决定 `width` 属性。

注意：因为 `I`、`F` 可以为负，所以「位宽」可能小于直觉——这正是负 I/F 的作用（见 4.6 节）。

#### 4.4.2 核心流程

\[
W = S + I + F
\]

例如：

| 格式 | 计算 | 位宽 |
|---|---|---|
| `[1,2,1]` | \(1+2+1\) | 4 |
| `[0,4,0]` | \(0+4+0\) | 4 |
| `[0,4,2]` | \(0+4+2\) | 6 |
| `[1,4,-2]` | \(1+4-2\) | **3**（注意！） |
| `[1,-2,4]` | \(1-2+4\) | **3**（注意！） |

后两个例子说明：**位宽可以小于 I 甚至小于符号位数**，因为负 I/F 把一些「位位置」从存储里删掉了。

#### 4.4.3 源码精读

Python `FixFormat` 把 `width` 实现成一个只读属性：

[en_cl_fix_types.py:377-382](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L377-L382) —— `@property width` 直接返回 `self.S + self.I + self.F`。注释明确写了「返回 S+I+F」。

VHDL 端是镜像的公共函数：

[hdl/en_cl_fix_pkg.vhd:365-368](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L365-L368) —— `cl_fix_width` 返回 `fmt.S + fmt.I + fmt.F`，与 Python `width` 公式逐字一致。

其声明（供其他函数调用）在包头：

[hdl/en_cl_fix_pkg.vhd:78](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L78) —— `function cl_fix_width(fmt : FixFormat_t) return natural`。

#### 4.4.4 代码实践

**实践目标**：用 Python 验证 `.width` 属性，尤其是验证「负 I/F 导致位宽变小」这一反直觉现象。

**操作步骤**（仓库根目录）：

```python
# 文件名：exp_width.py（示例代码，非项目原有文件）
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixFormat

for (s, i, f) in [(1,2,1), (0,4,0), (0,4,2), (1,4,-2), (1,-2,4)]:
    fmt = FixFormat(s, i, f)
    print(f"[{s},{i},{f}]  width = {fmt.width}")
```

**需要观察的现象**：`[1,4,-2]` 与 `[1,-2,4]` 的 `width` 都输出 `3`，而不是 7 或 3-之外的某个大数。

**预期结果**（待本地验证）：

```
[1,2,1]  width = 4
[0,4,0]  width = 4
[0,4,2]  width = 6
[1,4,-2] width = 3
[1,-2,4] width = 3
```

#### 4.4.5 小练习与答案

**练习 1**：`[1,8,8]` 这种常见的 17 位有符号 Q8 格式，`width` 是多少？

> **答案**：\(1+8+8 = 17\)。

**练习 2**：如果想让一个有符号格式只存「符号位 + 4 个小数位」，不存任何整数位，写成 `[S,I,F]` 并算位宽。

> **答案**：`[1, 0, 4]`，位宽 \(1+0+4 = 5\)。符号位权重 \(-2^0 = -1\)，范围 \([-1,\ 0.9375]\)。

---

### 4.5 FixFormat 类：构造、断言与默认值

#### 4.5.1 概念说明

`FixFormat` 是 Python 参考模型里描述格式的「值对象」。它本身**不做运算**，只保存 `S, I, F` 三个整数，并提供相等比较、字符串化和位宽查询。所有 `cl_fix_*_fmt` 格式预测函数（后续讲义）都返回一个 `FixFormat`。

构造时，类会用**断言（assert）**拒绝几种「没有实际意义、又会制造边角案例」的非法组合。理解这些断言，就理解了格式定义的合法边界。

#### 4.5.2 核心流程

`FixFormat(S, I, F)` 的构造逻辑：

1. 检查 `S ∈ {0, 1}`，否则报错。
2. 检查 `I + F >= 0`，否则报错（保证位宽 \(\ge S \ge 0\)，且避免 `cl_fix_max_value` 等函数的边角案例）。
3. 把三个值转成 `int` 存起来。

合法与非法对照：

| 格式 | 合法？ | 说明 |
|---|---|---|
| `(0,0,0)` | ✅ | 无符号空格式（width=0） |
| `(1,0,0)` | ✅ | 只有符号位（width=1） |
| `(0,-5,5)` | ✅ | 负 I 正 F，width=0 |
| `(1,-5,5)` | ✅ | 有符号、负 I 正 F，width=1 |
| `(2,0,0)` | ❌ | S 只能 0 或 1 |
| `(0,-1,0)` | ❌ | `I+F = -1 < 0` |
| `(1,-1,0)` | ❌ | 有符号空格式，`I+F = -1 < 0`，被禁 |

#### 4.5.3 源码精读

Python 构造与断言（本讲最核心的一段源码）：

[en_cl_fix_types.py:61-70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L61-L70) —— `__init__`：
- 第 62 行断言 `S == 0 or S == 1`。
- 第 63-66 行注释解释了为什么允许 `(0,0,0)`、`(1,0,0)`、`(0,-5,5)`、`(1,-5,5)`，却禁止 `(1,-1,0)`、`(0,-1,0)` 这类「有符号空格式 / 负位宽」——它们会制造边角案例且无实用价值。
- 第 67 行断言 `I+F >= 0`。
- 第 68-70 行把 `S/I/F` 转 `int` 存储。

字符串化方法（4.1.4 实践里用到）：

[en_cl_fix_types.py:365-370](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L365-L370) —— `__repr__` 输出 `FixFormat(S, I, F)`，`__str__` 输出 `(S, I, F)`。

相等比较：

[en_cl_fix_types.py:373-374](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L373-L374) —— `__eq__` 逐字段比较 `S/I/F`，因此两个 `FixFormat` 只要三元素相同即视为相等（后续测试大量依赖这一点）。

#### 4.5.4 代码实践

**实践目标**：触发断言，亲眼看哪些格式被拒绝。

**操作步骤**：

```python
# 文件名：exp_assert.py（示例代码，非项目原有文件）
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixFormat

# 合法
print(FixFormat(0, 0, 0))     # (0, 0, 0)
print(FixFormat(1, -5, 5))    # (1, -5, 5)

# 非法：去掉下面两行注释，逐个运行观察 AssertionError
# FixFormat(2, 0, 0)          # S 必须 0 或 1
# FixFormat(0, -1, 0)         # I+F 必须 >= 0
```

**需要观察的现象**：前两行正常打印；被注释的两行一旦运行，会抛出 `AssertionError`，信息分别是 "S must be 0 or 1" 和 "I+F must be at least 0"。

**预期结果**（待本地验证）：合法格式正常输出；非法格式抛出对应断言错误。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `(1, -1, 0)` 被禁止，而 `(1, -5, 5)` 被允许？两者不都是「有符号 + I 为负」吗？

> **答案**：区别在 `I+F`。`(1,-5,5)` 的 `I+F=0 \ge 0`，合法（width=1，就是一个纯符号位）；`(1,-1,0)` 的 `I+F=-1 < 0`，违反 `I+F>=0` 断言。源码注释说明有符号空格式会制造边角案例，故直接禁止。

**练习 2**：`FixFormat(1, 2, 1) == FixFormat(1, 2, 1)` 的结果是 True 还是 False？为什么？

> **答案**：True。`__eq__` 逐字段比较 S/I/F，三者都相等即返回 True。这让格式对象可以作为字典 key 或用于测试断言对比。

---

### 4.6 VHDL FixFormat_t 与 NullFixFormat_c：金标准镜像

#### 4.6.1 概念说明

VHDL 是 en_cl_fix 的语义金标准（承接 u1-l2）。在 VHDL 里，格式不是一个类，而是一个 **record（记录）类型** `FixFormat_t`，含三个字段 `S, I, F`。它和 Python `FixFormat` 在语义上**完全镜像**，只是语言表达不同。

此外，VHDL 包定义了一个特殊常量 `NullFixFormat_c := (0, 0, -1)`，它在整个库里扮演「默认/占位格式」的角色——很多函数的 `result_fmt` 参数默认取它，表示「不指定结果格式，按全精度返回」。这一点会在 u4-l1（Python 主接口）深入展开，这里先认识它。

#### 4.6.2 核心流程

VHDL record 定义要点：

- `S : natural range 0 to 1` —— 用**子类型约束**把 `S` 限制为 0 或 1（等价于 Python 的断言，但在 VHDL 里由语言/工具在编译期或运行期保证）。
- `I : integer`、`F : integer` —— 都是普通整数，可取负值（与 Python 一致）。

`NullFixFormat_c` 的特殊性：

- 取值 `(0, 0, -1)`：`S=0, I=0, F=-1`，注意 `I+F = -1 < 0`。
- 这个值若用 Python `FixFormat(0,0,-1)` 构造会触发 `I+F>=0` 断言！这是 VHDL 与 Python 之间一个**刻意的差异**：VHDL 把 `NullFixFormat_c` 当作「哨兵值」直接绕过合法性检查，专门用来表示「未指定格式」。

> 这是一个值得记住的细节：**镜像不是字面照抄**。Python 用「`None`/断言拒绝」表达未指定，VHDL 用一个越界的哨兵常量表达同一意图。

#### 4.6.3 源码精读

VHDL record 与哨兵常量定义：

[hdl/en_cl_fix_pkg.vhd:39-45](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L39-L45) ——
- 第 39-43 行：`type FixFormat_t is record ... end record`，三个字段 `S(范围0到1)/I/F`，注释标注「Sign bit / Integer bits / Fractional bits」，与 Python `FixFormat` 文档一一对应。
- 第 45 行：`constant NullFixFormat_c : FixFormat_t := (0, 0, -1)`。

紧跟其后的数组类型（后续讲义从文件批量读格式时会用）：

[hdl/en_cl_fix_pkg.vhd:47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L47) —— `type FixFormatArray_t is array(natural range <>) of FixFormat_t`。

同一个 record 也在 VHDL 端配套了宽度计算函数（与 Python `width` 镜像）：

[hdl/en_cl_fix_pkg.vhd:365-368](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L365-L368) —— `cl_fix_width` 返回 `fmt.S + fmt.I + fmt.F`。

> 三视角对照：README 用自然语言+图说 `[S,I,F]`；Python 用 `FixFormat` 类 + 断言；VHDL 用 `FixFormat_t` record + `natural range 0 to 1` 约束。三者描述的是同一个数学对象。

#### 4.6.4 代码实践

**实践目标**：源码阅读型实践——确认 `NullFixFormat_c` 是一个会被 Python 拒绝的「哨兵值」，从而体会两种语言在「未指定格式」表达上的差异。

**操作步骤**：

1. 打开 [hdl/en_cl_fix_pkg.vhd:45](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L45)，记下 `NullFixFormat_c = (0, 0, -1)`。
2. 在 Python 里尝试构造同样的值：

```python
# 文件名：exp_null.py（示例代码，非项目原有文件）
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixFormat

try:
    fmt = FixFormat(0, 0, -1)     # 与 NullFixFormat_c 同值
    print("构造成功", fmt)
except AssertionError as e:
    print("被 Python 拒绝:", e)
```

3. 在 VHDL 包里搜索 `NullFixFormat_c` 的使用处（例如 `result_fmt : FixFormat_t := NullFixFormat_c` 的默认值），观察它在哪些函数里当默认值。

**需要观察的现象**：Python 抛出 `AssertionError: I+F must be at least 0`；而 VHDL 端这个值作为常量合法存在，并被多个数学函数（如 `cl_fix_abs`、`cl_fix_neg`、`cl_fix_add` 等，见包头中带 `:= NullFixFormat_c` 默认值的参数）当作「未指定 result_fmt」的哨兵。

**预期结果**（待本地验证）：Python 拒绝 `(0,0,-1)`；VHDL 把它作为合法常量使用。

#### 4.6.5 小练习与答案

**练习 1**：VHDL 用什么机制保证 `S` 只能取 0 或 1？它和 Python 的做法有什么不同？

> **答案**：VHDL 用子类型约束 `S : natural range 0 to 1`，在类型层面限定；Python 用运行期 `assert S == 0 or S == 1`。前者偏编译期/类型检查，后者偏运行期断言。

**练习 2**：`NullFixFormat_c = (0, 0, -1)` 的位宽（按 `cl_fix_width` 公式）是多少？为什么它不适合作为真实数据格式？

> **答案**：\(0+0+(-1) = -1\)，位宽为负，显然不能存任何数据。它只作「未指定格式」的哨兵，不代表真实格式。

---

## 5. 综合实践

把本讲的「位宽、位权重、范围、合法约束、三语言镜像」串起来，完成下面这张「格式速查表」手工推导，并用 Python 验证位宽部分。

**任务**：对以下 4 种格式，分别完成 (a) 总位宽、(b) 最大值、(c) 最小值、(d) 用 4.1.2 的锚点模型写出每一位的权重。

1. `[1, 4, -2]`
2. `[0, 4, 2]`
3. `[1, -2, 4]`
4. `[1, 2, 2]`

**参考推导**（先自己算，再对照）：

| 格式 | (a) 位宽 \(S+I+F\) | (b) 最大值 \(2^I-2^{-F}\) | (c) 最小值 | (d) 位权重（MSB→LSB） |
|---|---|---|---|---|
| `[1,4,-2]` | 3 | \(16-4=12\) | \(-16\) | \(-16, 8, 4\) |
| `[0,4,2]` | 6 | \(16-0.25=15.75\) | \(0\) | \(8,4,2,1,0.5,0.25\) |
| `[1,-2,4]` | 3 | \(0.25-0.0625=0.1875\) | \(-0.25\) | \(-0.25, 0.125, 0.0625\) |
| `[1,2,2]` | 5 | \(4-0.25=3.75\) | \(-4\) | \(-4,2,1,0.5,0.25\) |

**验证步骤**（位宽部分，仓库根目录）：

```python
# 文件名：exp_summary.py（示例代码，非项目原有文件）
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixFormat

for (s, i, f) in [(1,4,-2), (0,4,2), (1,-2,4), (1,2,2)]:
    print(f"[{s},{i},{f}] width = {FixFormat(s,i,f).width}")
```

**自查要点**：
- 位宽输出应为 `3, 6, 3, 5`（待本地验证）。
- 对照 README 示例表 [README.md:109-116](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L109-L116)，确认 `[1,4,-2]` 范围 `-16...12`、`[1,-2,4]` 范围 `-0.25...+0.1875` 与你的推导一致。
- 思考：为什么 `[1,4,-2]` 只有 3 位却能表示到 \(\pm 16\) 量级？（答：LSB 权重 \(2^{-F}=2^2=4\)，每位权重翻倍，量级大但分辨率粗。）

> 进阶（可选）：用 VHDL 的 `max_real`/`min_real` 公式 [hdl/en_cl_fix_pkg.vhd:276-288](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L276-L288) 在脑中代入上面每个格式的 `I, F`，确认与 (b)(c) 列吻合——这是打通 Python 与 VHDL 镜像关系的关键一步。

## 6. 本讲小结

- 定点格式 `[S, I, F]` 用三个数字描述：`S` 符号位（0 或 1）、`I` 整数位、`F` 小数位，`I/F` 均可为负。
- **总位宽** \(W = S + I + F\)；Python 的 `FixFormat.width`（[en_cl_fix_types.py:377-382](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L377-L382)）与 VHDL 的 `cl_fix_width`（[hdl/en_cl_fix_pkg.vhd:365-368](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L365-L368)）公式逐字一致。
- **位权重**以 LSB 为锚：LSB 权重恒为 \(2^{-F}\)，符号位权重为 \(-2^I\)（补码），其余逐位翻倍。
- **范围**：最大值 \(2^I - 2^{-F}\)（任意 S），最小值有符号为 \(-2^I\)、无符号为 0；对应 VHDL `max_real`/`min_real`（[hdl/en_cl_fix_pkg.vhd:276-288](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L276-L288)）。
- Python `FixFormat.__init__` 用断言约束 `S∈{0,1}` 且 `I+F>=0`（[en_cl_fix_types.py:61-70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L61-L70)），禁止无意义的边角格式。
- VHDL `FixFormat_t` record（[hdl/en_cl_fix_pkg.vhd:39-43](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L39-L43)）与 Python 镜像；哨兵常量 `NullFixFormat_c = (0,0,-1)` 表示「未指定格式」，是两语言间一个刻意的差异。

## 7. 下一步学习建议

本讲把「格式」讲透了，但还没讲「当格式变化时，数值如何处理」。建议按顺序继续：

1. **u2-l2 舍入模式 FixRound**：当 `F` 减小（丢小数位）时，如何舍入？七种模式在「平局」上的差异是什么。
2. **u2-l3 饱和模式 FixSaturate**：当 `I`/`S` 减小（丢整数位或转无符号）时，如何处理越界？本讲的「范围」是判断越界的直接依据。
3. **u2-l4 位宽、极值与格式工具函数**：系统学习 `cl_fix_width`、`cl_fix_max_value`、`cl_fix_min_value`、`union` 等工具，本讲已经预告了它们的实现。
4. 之后进入 U3「结果格式预测」——你会看到加法、乘法如何**推导**出新的 `[S,I,F]`，那是本讲格式概念的第一场真正应用。

> 推荐配合阅读：README 的 `Fixed-Point Number Format` 章节 [README.md:77-116](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L77-L116)，以及 Enclustra 官方定点数网络研讨会（README 中有链接），把直觉彻底夯实。
