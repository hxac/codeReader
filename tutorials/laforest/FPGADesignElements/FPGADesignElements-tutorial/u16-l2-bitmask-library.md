# Bitmask 位操作库

## 1. 本讲目标

上一讲（u16-l1）我们用 popcount、ntz、nlz、Hamming_Distance、Bit_Voting 把「一个字里有多少个 1、第一个 1 在哪、两个字差几位」算了出来。这些都是**数值型**位操作——输出是一个数。

本讲换成**掩码型**位操作——输出仍然是同样宽度的一个字（bitmask），只是某些位被置 1、某些位被清 0。这类操作几乎全部来自经典著作《Hacker's Delight》第 2-1 节「Manipulating Rightmost Bits」，是用加减法和按位运算在「不动循环」的前提下完成位级变换的小技巧。

学完本讲你应该能够：

1. 说清「隔离右起第一个 1 位」(`x & -x`) 与「关掉右起第一个 1 位」(`x & (x-1)`) 的原理，并用二进制补码推导它们。
2. 用一个加/减法生成「低 N 位全 1」的温度计码，并理解它为何是「等 popcount 掩码集合」的字典序第一名。
3. 读懂 `Bitmask_Next_with_Constant_Popcount_ntz` 这个全书最复杂的掩码模块，能手工推演一次「下一个等 popcount 排列」的全部中间值，并解释为何它能优雅地处理末尾绕回。

## 2. 前置知识

本讲假定你已经掌握以下概念（前序讲义已建立）：

- **二进制补码（two's complement）与负数表示**：在无符号视角下，对一个字求相反数满足 \(-x = \sim x + 1\)。这是本讲最关键的一块拼图——它让 `x & -x` 这种写法在 Verilog 里直接可用。
- **按位运算与移位**：`&`（与）、`|`（或）、`^`（异或）、`~`（取反）、`<<`（逻辑左移）、`>>`（逻辑右移）。
- **popcount（汉明重量）与 ntz/nlz**（u16-l1）：一个字里 1 的个数；尾零 / 首零个数。本讲会把 ntz 分解成的「隔离右起第一个 1 位 + 取对数」两步里，第一步单独抽出来讲。
- **Verilog 组合逻辑写法**（u2-l1、u3-l1）：`default_nettype none`、`always @(*)` 用阻塞赋值、`output reg` 用 `initial` 初始化。
- **构建块库思想**（u4-l1、u5 系列、u8-1）：本书不写「随机逻辑」，而是把每个有名小运算封装成模块，复杂模块靠实例化拼出。本讲的 `Bitmask_Next_with_Constant_Popcount_ntz` 就是一个把 5 个子模块连起来的范例。

> 一个贯穿全讲的直觉：**加减法本身就在做位级搬运**。给一个数加 1，会让末尾的一串连续 1 全部翻成 0、再让前一位 0 翻成 1（二进制进位涟漪）。本讲几乎所有技巧，本质上都是在「借用」加法器的这种涟漪行为来实现位的隔离与重组。

## 3. 本讲源码地图

本讲涉及的关键文件（均在仓库根目录）：

| 文件 | 作用 | 行数 |
|------|------|------|
| `Bitmask_Isolate_Rightmost_1_Bit.v` | 一行 `word_in & (-word_in)`，孤立右起第一个 1 位 | 33 |
| `Turn_Off_Rightmost_1_Bit.v` | 一行 `word_in & (word_in - 1)`，关掉右起第一个 1 位 | 34 |
| `Bitmask_Thermometer_from_Count.v` | `(1 << N) - 1`，生成低 N 位全 1 的温度计码 | 38 |
| `Bitmask_Next_with_Constant_Popcount_ntz.v` | 给定掩码，给出字典序下一个「等 popcount」掩码（本讲主角） | 196 |
| `Logarithm_of_Powers_of_Two.v` | 单热掩码 → 其位号（log₂），主角模块的子块之一 | 141 |
| `Bit_Shifter.v` | 三倍宽桶形移位器，主角模块的子块之一 | 109 |
| `Adder_Subtractor_Binary.v` | 加减法器（u8-l1 已讲），主角模块的子块之一 | — |

注意：前三个模块每个都只有**一行**核心逻辑。这不是偷懒，而是本书的刻意设计——把哪怕一行有名字、有意图的运算也封装成模块，让模块名本身成为注释（回顾 u5-l1「模块即文档、模块即设计意图」）。

---

## 4. 核心概念与源码讲解

### 4.1 位隔离 / 翻转：`x & -x` 与 `x & (x-1)`

#### 4.1.1 概念说明

很多算法需要在「一个字里定位最右边的那个 1」。比如：

- u16-l1 的 **ntz**：先孤立右起第一个 1，再取它的对数（位号），就是尾零个数。
- u11-l1 的 **优先仲裁器 / 优先编码器**：用 `x & -x` 找到最高优先级的请求位。
- 经典的 **Brian Kernighan popcount**：反复执行 `x = x & (x-1)`，每执行一次关掉一个 1，执行几次就有几个 1。

《Hacker's Delight》给出了两个对偶的基础恒等式：

- **隔离右起第一个 1 位**：\( s = x \,\&\, (-x) \)。
- **关掉右起第一个 1 位**：\( t = x \,\&\, (x - 1) \)。

它们之所以成立，全靠二进制加减法的「涟漪」性质，下面拆开看。

#### 4.1.2 核心流程

**先理解 `x & -x`。** 在补码下 \(-x = \sim x + 1\)。设 \(x\) 最右边的那个 1 在第 \(k\) 位，那么：

1. 第 \(0\) 到 \(k-1\) 位全是 0。对它们取反后全变 1，再加 1 会一路进位到第 \(k\) 位。
2. 进位到达第 \(k\) 位时，恰好把「取反后的 0」恢复成原来的 1（因为 \(\sim 0 + \text{进位} = 1\)），同时第 \(0\) 到 \(k-1\) 位被清回 0。
3. 第 \(k\) 位以上的部分，补码等于原码取反。

于是 \(x\) 与 \(-x\) 只有第 \(k\) 位同为 1，其余位必有一个是 0。`&` 之后只剩第 \(k\) 位——也就是一个独热（one-hot）掩码。

例如 `x = 01011000`：

```
 x  = 01011000
-x  = 10101000   (= ~01011000 + 1 = 10100111 + 1)
s  = x & -x = 00001000   ← 只剩右起第一个 1
```

**再看 `x & (x-1)`。** 给 \(x\) 减 1，会让第 \(k\) 位那一个 1 被借位借走、变成 0，而第 \(0\) 到 \(k-1\) 位全部被借成 1；第 \(k\) 位以上不变。于是 \(x\) 与 \(x-1\) 的 `&` 会把第 \(k\) 位清掉、其余位保持原样。

例如 `x = 01011000`：

```
 x    = 01011000
 x-1  = 01010111
 t = x & (x-1) = 01010000   ← 右起第一个 1 被关掉
```

两者可用一张表对照：

| 输入 | 隔离右起 1：`x & -x` | 关掉右起 1：`x & (x-1)` |
|------|----------------------|--------------------------|
| `01011000` | `00001000` | `01010000` |
| `00100011` | `00000001` | `00100010` |
| `00000000` | `00000000`（无 1 可隔离） | `00000000`（无 1 可关） |

两个巧妙的副产物：

- `x & -x` 当输入是 2 的幂时返回自身，是「2 的幂检测」与「优先级选取」的基础。
- `(x & (x-1)) == 0` 当且仅当 \(x\) 是 2 的幂或 0——这是 `Turn_Off_Rightmost_1_Bit` 注释里点出的用法。

#### 4.1.3 源码精读

两个模块结构几乎一样：参数化字宽、`output reg`、`initial` 初始化、一行组合逻辑。

`Bitmask_Isolate_Rightmost_1_Bit` 的核心就是一行：

[Bitmask_Isolate_Rightmost_1_Bit.v:28-30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Isolate_Rightmost_1_Bit.v#L28-L30) —— 用 `word_in & (-word_in)` 孤立右起第一个 1 位。Verilog 的一元负号 `-` 在无符号向量上即补码求负，综合器会把它映射成「取反 + 加 1」的加法器逻辑。

[Bitmask_Isolate_Rightmost_1_Bit.v:15-22](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Isolate_Rightmost_1_Bit.v#L15-L22) —— 参数化端口；`WORD_WIDTH` 默认为 0（回顾 u1-l2、u2-l2：必须实例化并显式设参才能用）。

`Turn_Off_Rightmost_1_Bit` 多定义了一个常量 `ONE`，因为本书不信任工具对整数字面量的位宽扩展（回顾 u2-l2），坚持手工拼出整个字宽的 1：

[Turn_Off_Rightmost_1_Bit.v:27](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Turn_Off_Rightmost_1_Bit.v#L27) —— `ONE = {{WORD_WIDTH-1{1'b0}},1'b1}`，一个定宽的整数 1。

[Turn_Off_Rightmost_1_Bit.v:29-31](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Turn_Off_Rightmost_1_Bit.v#L29-L31) —— 核心一行 `word_in & (word_in - ONE)`。

[Turn_Off_Rightmost_1_Bit.v:9-10](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Turn_Off_Rightmost_1_Bit.v#L9-L10) —— 注释点出：套用本式再做一次「结果是否为 0」的测试，即可判断一个无符号数是不是 2 的幂或 0。

#### 4.1.4 代码实践

**实践目标**：手工验证两个恒等式，并体会 `x & (x-1)` 的「关掉一个 1」效果。

**操作步骤**：

1. 取 `x = 0b01101100`（十进制 108）。在纸上写出 `x`、`-x`、`x & -x`、`x-1`、`x & (x-1)` 五个二进制值。
2. 把 `x & (x-1)` 的结果当作新的 `x`，再算一次 `x & (x-1)`，重复直到结果为 0，数一数执行了几次。
3. 改用 `x = 0b01000000`（2 的幂），算 `x & (x-1)`。

**需要观察的现象**：

- 第 1 步里 `x & -x` 应为 `0b00000100`，`x & (x-1)` 应为 `0b01101000`。
- 第 2 步执行次数应恰好等于 `x` 的 popcount（4）——这就是 Kernighan 计数法。
- 第 3 步结果应为 0：2 的幂只有 1 个 1，关掉一次就空了。

**预期结果**：第 2 步得到 4 次，与 popcount 一致；第 3 步得 0，印证 `(x & (x-1)) == 0` 是「2 的幂或 0」的判据。

> 若想跑仿真，可仿照 `tests/` 里的写法用 cocotb 建一个最小 testbench，把 `word_in` 接计数器扫描 0..255（8 位情形），断言 `popcount(x & (x-1)) == popcount(x) - 1`（x ≠ 0 时）。本讲以纸笔推演为主，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `x & -x` 在 `x = 0` 时返回 0？用补码解释。

**答案**：`x = 0` 时所有位都是 0，没有「右起第一个 1」，补码 `-0 = 0`，`0 & 0 = 0`。这正是模块注释里「producing 0 if none」的来源，也提醒下游：全零输入没有有效位，必须另行处理（u16-l1 的 ntz 模块因此设了 `logarithm_undefined` 标志）。

**练习 2**：用 `x & -x` 实现一个 LSB 优先的优先编码器还需要什么？

**答案**：还需要把独热掩码转成它的位号——也就是取 \(\log_2\)（u16-l1 的 ntz 里那一步，本讲的 `Logarithm_of_Powers_of_Two`）。`x & -x` 只负责「挑出那一位」，对数才负责「说出它是第几位」。

---

### 4.2 温度计码：从整数 N 生成「低 N 位全 1」

#### 4.2.1 概念说明

**温度计码（thermometer code）** 是这样一种编码：值为 \(N\) 时，低 \(N\) 位全 1、其余位全 0，像水银温度计一样「从底下连续填满」。

| N | 温度计码（8 位） |
|---|------------------|
| 0 | `00000000` |
| 1 | `00000001` |
| 3 | `00000111` |
| 5 | `00011111` |
| 8 | `11111111` |

它在硬件里用途很广：DAC 输出、one-hot 风格的优先级表示、DMA 传输长度掩码等。本讲更关心它的一个数学身份：**温度计码是「popcount = N 的所有掩码里，字典序最小的那一个**」——把 N 个 1 全部挤到最右边。理解这一点，是下一节「等 popcount 的下一排列」的钥匙。

#### 4.2.2 核心流程

生成温度计码有一个极其简洁的恒等式：

\[
\text{mask} = (1 \ll N) - 1
\]

直觉：`1 << N` 在第 \(N\) 位放一个孤零零的 1（例如 \(N=3\) 得 `001000`），减 1 时借位涟漪把这个 1 借掉、下面所有位全填成 1，于是得到低 \(N\) 位全 1。

当 \(N \ge \text{WORD\_WIDTH}\) 时，`1 << N` 会把那一个 1 移出字外（移入「第 WORD_WIDTH 位」之外），字内只剩 0，再减 1 得到全 1——这正是模块注释承诺的「N 超过字宽则掩码全 1」。

> 与 4.1 的呼应：`x & -x` 和 `(1<<N)-1` 都在「借用减法的借位涟漪」。前者让涟漪停在第一个 1 处，后者让涟漪填满整个低位段。

#### 4.2.3 源码精读

[Bitmask_Thermometer_from_Count.v:33-35](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Thermometer_from_Count.v#L33-L35) —— 核心一行 `word_out = (ONE << count_in) - ONE`，完全对应上面的恒等式。

[Bitmask_Thermometer_from_Count.v:27](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Thermometer_from_Count.v#L27) —— 同样手工定义 `ONE` 为定宽的 1，不依赖工具对字面量的位宽扩展。

[Bitmask_Thermometer_from_Count.v:9-14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Thermometer_from_Count.v#L9-L14) —— 注释点出温度计码的双重身份：它既是「低 N 位全 1」的掩码，也是「popcount = N 的掩码集合」的字典序第一名；最后一名是把位序反转后的版本（用 `Word_Reverser`，`WORD_WIDTH=1`），而要遍历集合里所有成员，就用下一节的 `Bitmask_Next_with_Constant_Popcount`。

这里出现了一个重要的工程取舍：`count_in` 是变量，所以 `<< count_in` 是一个**变量移位**，综合后会展开成桶形移位器（多路选择器树），比常量移位贵得多。这与下一节 `Bit_Shifter` 的代价是同一类问题。

#### 4.2.4 代码实践

**实践目标**：验证 `(1<<N)-1` 在边界条件下的行为。

**操作步骤**：

1. 设 `WORD_WIDTH = 8`，依次令 `count_in = 0,1,3,5,8,10`，手算 `(1 << count_in) - 1` 的 8 位结果。
2. 特别注意 `count_in = 8` 和 `10`（≥ 字宽）两种情况。

**需要观察的现象**：

- `N=0` → `00000000`；`N=3` → `00000111`；`N=5` → `00011111`。
- `N=8` 与 `N=10` 都得到 `11111111`：因为 `1 << 8` 已经把那一位移出了 8 位字，字内为 0，减 1 借位填满全 1。

**预期结果**：N ≥ 字宽时统一饱和为全 1，符合模块注释。这一点在用温度计码做「剩余容量掩码」时要当心——它不会报错，只会静默饱和。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：温度计码与「popcount = N 的掩码集合」有什么关系？

**答案**：温度计码（N 个 1 全挤在最低位）是该集合里**字典序最小**的成员；把位序反转（`Word_Reverser`）得到 N 个 1 全挤在最高位的版本，是字典序**最大**的成员。用 `Bitmask_Next_with_Constant_Popcount` 从温度计码出发不断取「下一个」，就能不重复地遍历整个集合。

**练习 2**：为什么作者坚持写 `ONE` 常量，而不直接写 `(1 << count_in) - 1` 里的字面量 1？

**答案**：回顾 u2-l2：本书不信任 Verilog 整数字面量在宽位下的扩展行为，且 Verilog-2001 禁止用 parameter/localparam 当位宽说明符。手工把 `1` 拼成完整的 `WORD_WIDTH` 位宽，能让位宽告警有意义、避免不同工具扩展不一致带来的 bug。

---

### 4.3 恒定 popcount 的下一排列：全书最复杂的掩码模块

#### 4.3.1 概念说明

很多算法需要在「固定 popcount」的约束下遍历所有掩码。例如：

- 选 K 个资源中的某几个组合（n-choose-k 枚举）；
- 在固定数量的请求位上做组合搜索；
- 生成测试向量覆盖所有「恰好 K 位为 1」的输入。

《Hacker's Delight》给出了一个精妙的恒等式，能把任意掩码 \(x\) 变成「字典序下一个、且 popcount 相同」的掩码 \(y\)。例如：

```
00100011  -->  00100101      (都是 3 个 1，后者更大)
01011000  -->  01100000
```

它还有一个讨人喜欢的性质：在字末尾会**优雅绕回**——最高成员（如 `11100000`）的「下一个」是最低成员（`00000111`）。这意味着你不必预先算出 n-choose-k 的总数，也不必单独检测最高成员：从任意掩码出发，不断取「下一个」，当结果再次等于起点时，就说明所有情况都遍历完了。

本仓库实现了两个版本：

- `Bitmask_Next_with_Constant_Popcount_pop`：用 popcount 计算移位量，移位对象是常量 1；
- `Bitmask_Next_with_Constant_Popcount_ntz`（本讲主角）：用 ntz（取对数）计算移位量，移位对象是变化的数据。

作者在注释里坦言不确定哪个版本在面积/速度上更划算。我们精读 ntz 版，因为它把前面学的 `Bitmask_Isolate_Rightmost_1_Bit`、`Logarithm_of_Powers_of_Two`、`Adder_Subtractor_Binary`、`Bit_Shifter` 全串了起来，是构建块库思想的集中展示。

#### 4.3.2 核心流程

原始公式（来自 Hacker's Delight），对 \(x \to y\)：

\[
\begin{aligned}
s &= x \,\&\, (-x)           &\text{// 孤立右起第一个 1（第 4.1 节）}\\
r &= s + x                   &\text{// 让右起的一串连续 1 涟漪进位上去}\\
c &= \text{carry}(s + x)     &\text{// 保存最高位的进位输出}\\
y &= r \,|\, \big[\,((x \oplus r) \gg (2 - 2c)) \,/\, s\,\big]
\end{aligned}
\]

关键观察：\(s\) 永远是 2 的幂（它是 `x & -x`），所以「除以 \(s\)」就是「逻辑右移 \(\log_2 s\)」位。把两次右移合并后：

\[
y = r \,\big|\, (x \oplus r) \gg \big[(2 - 2c) + \mathrm{ntz}(x)\big]
\]

作者最终选择保留「先按 \(2-2c\) 校正移位、再按 \(\log_2 s\) 数据相关移位」的两段式形式，原因写得很实在：合并形式需要一个「位宽为 \(\log_2(\text{WORD\_WIDTH})\)」的额外加法器，写干净 Verilog 反而更麻烦；而两段式里校正移位只有「移 2」或「移 0」两种取值，可以用一个二选一实现。

**直觉解读**：`r = s + x` 把右起那串连续 1「推」到下一个 0 的位置（消耗掉一段连续 1，在更高处生成一个新 1）。但这样会让 popcount 变少——少掉的正是那段被进位吞掉的连续 1 的个数减一。`x ^ r` 标出了所有「因涟漪而改变的位」，把这些改变位重新右移回最低位段，就补回了 popcount。这就是算法的灵魂：**先把一段连续 1 向上推一格，再把它们「降级」搬回最低位**。

让我们手工跑一遍 `x = 00100011 -> y = 00100101`：

```
x = 00100011
s = x & -x = 00000001
r = s + x  = 00100100       (右起的连续 1 段 "11" 涟漪进位)
c = carry  = 0
x^r        = 00000111       (涟漪改变了末 3 位)
校正(c=0):  (x^r) >> 2 = 00000001
log2(s)    = 0              (s 在第 0 位)
再移 0:     00000001
y = r | 上式 = 00100100 | 00000001 = 00100101  ✓
```

再看**末尾绕回**的情形 `x = 11100000`（8 位、popcount 3 的最高成员）：

```
x = 11100000
s = x & -x = 00100000
r = s + x  = 00000000       (进位溢出到第 8 位，字内归 0)
c = carry  = 1              ← 关键：检测到溢出
x^r        = 11100000
校正(c=1):  不移位，保持 11100000
log2(s)    = 5
右移 5:     00000111
y = r | 上式 = 00000000 | 00000111 = 00000111  ✓
```

结果 `00000111` 正是 popcount 3 的温度计码（字典序最低成员），绕回成功。这就是 `c`（进位位）的用途：它区分了「正常进位」与「溢出绕回」两种情形，让模块无需任何特判就能处理最高成员。

整个数据通路可以画成五级：

```
word_in ─┬─> Bitmask_Isolate_Rightmost_1_Bit ─> smallest (s)
         │                                          │
         ├──────────────(+ s)────────────> ripple (r), carry (c)  [Adder_Subtractor]
         │                                                │
         └─> XOR with ripple ─> changed_bits ─校正(>>2?)─> corrected
                                                              │
         smallest ─> Logarithm_of_Powers_of_Two ─> shift_amount(=log2 s)
                                                              │
                              corrected >> shift_amount ─> changed_bits_shifted  [Bit_Shifter]
                                                              │
                           ripple | changed_bits_shifted ─> word_out (y)
```

#### 4.3.3 源码精读

模块开头那段长注释把整个算法推导写得很清楚，强烈建议先读：

[Bitmask_Next_with_Constant_Popcount_ntz.v:17-47](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L17-L47) —— 从原始含除法公式，逐步化简为「除以 2 的幂 = 右移」、再到「两次右移合并 = 移 ntz(x)」，并说明为何选两段式实现。

下面逐段对照源码。

**第 1 步：算 `s`**——直接复用本讲 4.1 的模块，是构建块库复用的范例：

[Bitmask_Next_with_Constant_Popcount_ntz.v:74-82](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L74-L82) —— 实例化 `Bitmask_Isolate_Rightmost_1_Bit` 得到 `smallest`，即 \(s = x \,\&\, -x\)。

**第 2 步：算 `r = s + x` 与进位 `c`**——复用 u8-l1 的加减法器：

[Bitmask_Next_with_Constant_Popcount_ntz.v:96-112](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L96-L112) —— 实例化 `Adder_Subtractor_Binary`，`add_sub=0`（做加法），`A=word_in, B=smallest`，得到 `ripple` 与 `ripple_carry_out`。注释 [88-92 行](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L88-L92) 解释：`c` 用于处理连续 1 一直延伸到字最高位、进位溢出的情形。

**第 3 步：算 `x ^ r`（涟漪改变了哪些位）**：

[Bitmask_Next_with_Constant_Popcount_ntz.v:119-123](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L119-L123) —— `changed_bits = word_in ^ ripple`。

**第 4 步：校正移位（移 2 或移 0，由进位 `c` 选择）**：

[Bitmask_Next_with_Constant_Popcount_ntz.v:134-138](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L134-L138) —— `changed_bits_corrected = (c==1) ? changed_bits : (changed_bits >> 2)`。这里就是公式里的 `>> (2-2c)`：未溢出时移 2（丢掉最低的「被吞掉的那一位」与新生的那一位），溢出时移 0（绕回情形没有新生位要丢弃）。注释 [125-133 行](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L125-L133) 把这个取舍讲得很细。

**第 5 步：算移位量 `log2(s)`**——复用 u16-l1 见过的对数模块：

[Bitmask_Next_with_Constant_Popcount_ntz.v:146-157](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L146-L157) —— 实例化 `Logarithm_of_Powers_of_Two`，把独热的 `smallest` 转成它的位号 `final_shift_amount`。这正好是 u16-l1 里 ntz = 「隔离右起 1 + 取对数」的第二步。

`Logarithm_of_Powers_of_Two` 的内部实现也值得一看：它对每个输入位预算好「若该位为 1，对数是多少」，再 OR 归约：

[Logarithm_of_Powers_of_Two.v:110-117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Logarithm_of_Powers_of_Two.v#L110-L117) —— 用 `generate for` 为每个输入位算一个候选对数，按该位是否为 1 选择输出候选或 0。

**第 6 步：按 `log2(s)` 右移**——复用桶形移位器：

[Bitmask_Next_with_Constant_Popcount_ntz.v:165-183](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L165-L183) —— 实例化 `Bit_Shifter`，`shift_direction=1`（右移），把 `changed_bits_corrected` 右移 `final_shift_amount` 位，两侧补 0，得到 `changed_bits_shifted`。

`Bit_Shifter` 的实现思路是把输入拼成三倍宽、整体移位、再拆回三段，避免手工算切片：

[Bit_Shifter.v:104-107](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Shifter.v#L104-L107) —— `{word_in_left, word_in, word_in_right}` 拼成三倍宽，按方向左/右移，再拆回 `word_out_left/word_out/word_out_right`。本模块只取中段 `word_out`，左右段悬空。

**第 7 步：合并**：

[Bitmask_Next_with_Constant_Popcount_ntz.v:191-193](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bitmask_Next_with_Constant_Popcount_ntz.v#L191-L193) —— `word_out = ripple | changed_bits_shifted`，得到最终的等 popcount 下一掩码。

把整条链路串起来看，本模块自身**只有 3 个 `always @(*)` 块**（异或、校正选择、最终或），其余全是已测试子模块的实例化——这正是 u4-l1 / u5 系列反复强调的「消灭随机逻辑、用构建块拼装」哲学的活样本。

#### 4.3.4 代码实践

**实践目标**：手工生成「下一个等 popcount 排列」，并解释 `x & (x-1)` 技巧（本讲义指定实践任务）。

**操作步骤（推演 ntz 版模块）**：

1. 取起点 `x = 0b00100011`（popcount 3，8 位）。按 4.3.2 的七步，依次写出 `s`、`r`、`c`、`x^r`、校正后值、`log2(s)`、右移结果、`y`。
2. 把得到的 `y` 当作新的 `x`，重复取「下一个」，直到结果绕回到 `00100011`，记录沿途所有掩码。
3. 改用起点 `x = 0b00000111`（温度计码，即 4.2 的字典序最低成员）再做一遍，观察序列是否相同。

**需要观察的现象 / 预期结果**：

- 第 1 步应得 `y = 00100101`。
- 第 2 步完整序列（popcount 3，8 位，共 \(\binom{8}{3}=56\) 个）应从 `00100011` 出发，按字典序严格递增，绕过最高成员 `11100000` 后回到起点。例如开头几项：`00100011 → 00100101 → 00100110 → 00101001 → ...`。
- 第 3 步从温度计码出发得到的序列，是第 2 步序列的「轮转」——证明从任一成员出发都能遍历全集。

**配套解释 `x & (x-1)` 技巧（`Turn_Off_Rightmost_1_Bit`）**：

- 写出 `x = 01011000`，则 `x-1 = 01010111`，`x & (x-1) = 01010000`：右起的那个 1（第 3 位）被关掉，其余位不变。
- 原理：减 1 时，最低位的那个 1 被借位借走变 0，它右边的所有 0 被借成 1；它左边不变。所以 `x` 与 `x-1` 的按位与，恰好把那一个 1 清零、其余位保持。
- 经典用途：**Brian Kernighan popcount**——`while (x) { x = x & (x-1); count++; }`，循环次数正好等于 1 的个数，硬件里可用于极简场景下的位数统计；`(x & (x-1)) == 0` 则是「2 的幂或 0」的判据。

> 若要仿真验证，可仿照 `tests/Counter_Gray_Tb.py` 用 cocotb 实例化 `Bitmask_Next_with_Constant_Popcount_ntz`（设 `WORD_WIDTH=8`），从 `00000111` 起反复把输出回送输入，断言：每次输出 popcount 恒为 3、且严格大于输入（除绕回那拍），并在 56 拍后回到起点。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么模块需要保存进位 `c = carry(s+x)`？没有它会怎样？

**答案**：`c` 用于区分「正常涟漪」与「连续 1 延伸到字最高位、进位溢出」两种情形。在溢出情形（如 `11100000`）下，`r` 在字内归 0，没有「新生成的更高 1」需要丢弃，所以校正移位应为 0 而非 2。若丢掉 `c`、一律移 2，就会把绕回时的掩码多移两位，popcount 虽对但序列错乱、绕回点也会错。`c` 正是让模块「无需特判最高成员」的关键。

**练习 2**：ntz 版与 pop 版的最大区别是什么？各自的主要代价？

**答案**：ntz 版用 `Logarithm_of_Powers_of_Two`（小）算移位量，但要对**变化的数据**做一次全字宽变量移位（`Bit_Shifter`，大）；pop 版用 `Population_Count`（大）算移位量，但只需对**常量 1** 做变量移位（小）。一个是「小对数 + 大数据移位」，另一个是「大 popcount + 小常量移位」。作者注释里明说尚不确定哪种在面积/速度上更优——这是真实的工程取舍，不是定论。

**练习 3**：从 `00000111` 出发不断取「下一个」，为什么一定能在有限步后回到 `00000111`？

**答案**：因为「popcount = 3 的 8 位掩码」是个有限集合（\(\binom{8}{3}=56\) 个），而「下一个」运算在集合内、且（除绕回拍外）严格递增。有限的严格递增序列必然走到最大成员 `11100000`，再走一步经绕回回到最小成员 `00000111`。这让我们无需预先算 \(\binom{n}{k}\)，只要检测「输出 == 起点」就知道遍历完成。

---

## 5. 综合实践

**任务**：搭一个「popcount-3 组合枚举器」，把本讲三个模块串起来。

设 `WORD_WIDTH = 8`。请设计（纸面即可，能仿真更好）一个纯组合的「自举式枚举」结构：

1. 用一个 `Register`（u6-l1）保存「当前掩码」，初值为温度计码 `00000111`——用本讲的 `Bitmask_Thermometer_from_Count`（`count_in = 3`）生成这个初值。
2. 每个时钟沿，把 `Bitmask_Next_with_Constant_Popcount_ntz` 的输出回写进这个寄存器。
3. 用一个比较器检测「输出 == `00000111` 且不是第一拍」，作为「遍历完成」标志。

**要求**：

- 画出框图，标注 `Thermometer`、`Next_with_Constant_Popcount_ntz`、`Register`、比较器四个块及其连线。
- 解释为什么初值用温度计码最方便（提示：回顾 4.2.5 练习 1——它是集合的字典序最低成员，绕回点天然落在它身上）。
- 估算 8 位、popcount 3 时一共需要多少拍才能遍历完全（答：\(\binom{8}{3}=56\) 拍）。
- 进阶思考：如果想统计「当前是第几个掩码」，能否复用本讲的 `Turn_Off_Rightmost_1_Bit` 配合 u16-l1 的 popcount？给出思路。

**预期结果**：一个 56 拍走完一圈、回到 `00000111` 的组合枚举器；框图清晰展示「温度计码做种子 → ntz 版下一排列做迭代 → 寄存器做状态 → 比较器做终止」四段。这个练习同时用到了本讲的全部三个最小模块，并把它们锚定在 u6-l1（Register）和 u16-l1（popcount）之上。

## 6. 本讲小结

- **位隔离与翻转**是一切掩码技巧的基石：`x & -x` 孤立右起第一个 1（补码 `\sim x + 1` 让涟漪停在第一个 1），`x & (x-1)` 关掉右起第一个 1（减 1 借位把那个 1 借走）。两者都靠加减法的进位/借位涟漪实现，零循环。
- **温度计码** `(1<<N)-1` 用一次借位涟漪填满低 N 位；它还是「popcount = N 掩码集合」的字典序最低成员，N 超过字宽时饱和为全 1。
- **等 popcount 的下一排列**算法分两段：`r = s + x` 把右起连续 1 向上推一格，再把 `(x^r)` 校正后右移回最低位补回 popcount；进位位 `c` 让它在字末尾优雅绕回，无需特判最高成员、无需预知 n-choose-k。
- **构建块库哲学**在本讲的 ntz 版模块里体现得淋漓尽致：自身只有 3 个组合块，其余全靠实例化 `Bitmask_Isolate_Rightmost_1_Bit`、`Adder_Subtractor_Binary`、`Logarithm_of_Powers_of_Two`、`Bit_Shifter` 拼出。
- 全部掩码模块都遵循本书编码规范：`default_nettype none`、定宽 `ONE`/`WORD_ZERO` 常量、`output reg` 配 `initial` 初始化、组合块用阻塞赋值。
- ntz 版与 pop 版是真实的面积/速度取舍，作者并未给出定论——这是「用对数换 popcount、用大数据移位换小常量移位」的工程天平。

## 7. 下一步学习建议

1. **读完整个 Bitmask 家族**：仓库里还有 `Bitmask_0_Bit_at_Rightmost_1_Bit`、`Bitmask_1_Bit_at_Rightmost_0_Bit`、`Bitmask_Turn_Off_Trailing_1_Bits`、`Bitmask_Turn_On_Trailing_0_Bits`、`Bitmask_Thermometer_to_Rightmost_0_Bit/1_Bit`、`Turn_On_Trailing_0_Bits` 等。它们都是同一族恒等式（隔离/翻转右起位、处理尾部连续位）的变体，建议挨个读一遍，体会「一套加减法技巧覆盖一整片位操作」。
2. **对照 pop 版**：读 `Bitmask_Next_with_Constant_Popcount_pop.v`，亲手比较它与 ntz 版的子模块构成与变量移位对象，理解作者说的「不确定哪个更优」具体指什么。
3. **回到 u16-l1 串联**：把本讲的 `Bitmask_Isolate_Rightmost_1_Bit` + `Logarithm_of_Powers_of_Two` 重新看作 ntz 的两个组成部分，确认「掩码型」与「数值型」位操作如何互相转化。
4. **进入下一单元 u17（复合流水线引擎）**：本讲的位操作、u16-l1 的 popcount/ntz、加上 u10/u12 的 ready/valid 握手与流水线控制，将在 `Pipeline_Iterator`、`Pipeline_Handshake_Multiplier` 等复合引擎里被组装起来，是「用构建块搭大引擎」的下一站。
