# psi_common_logic_pkg 逻辑向量工具

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `psi_common_logic_pkg` 里每个函数**做什么、为什么需要它**，而不仅仅是背签名。
- 手算 `binary_to_gray` / `gray_to_binary` 的一次编解码，并理解为什么异步 FIFO 必须用格雷码指针跨时钟域。
- 看懂 `ppc_or`（并行前缀或）的输入输出关系，并解释它如何被 `arb_priority` 用两行代码实现“固定优先级仲裁”。
- 知道 `reduce_or` / `reduce_and` / `to_01X` / `invert_bit_order` 这些“小工具”在什么场景下能省掉一长串手写逻辑。
- 能够运行本包自带的测试平台，并扩展它来验证格雷码的可逆性。

## 2. 前置知识

本讲只依赖一个非常自然的直觉，下面用大白话讲清楚。

**std_logic_vector 的“九值”系统。**
VHDL 的 `std_logic` 不只有 `'0'` 和 `'1'`，而是有 9 个值：强 `'0'/'1'`、弱 `'L'/'H'`、高阻 `'Z'`、未知 `'X'/'W'`、无关 `'-'`、未初始化 `'U'`。仿真里经常出现 `'U'` 或 `'X'`，综合后真实电路只有 `'0'/'1'/'Z'`。本讲的 `to_01X` 就是为了把这套九值系统“归一化”成 `{'0','1','X'}`，方便判断。

**什么是“归约（reduction）”？**
把一个向量的所有比特用一个运算压缩成 1 个比特。比如 `reduce_or("0010") = '1'`（只要有一个 1 就是 1），`reduce_and("1110") = '0'`（必须全是 1 才是 1）。VHDL-2008 虽然引入了内置归约算子，但并非所有综合工具都支持，本库选择用显式循环实现，保证可移植。

**什么是格雷码（Gray Code）？**
任意两个相邻整数，在格雷码表示下**只有 1 个比特不同**。例如 0→1→2→3 的二进制是 `00→01→10→11`（01 到 10 时两个比特都翻），而格雷码是 `00→01→11→10`（每次只翻一个比特）。这个性质在“跨时钟域采样多比特值”时极其重要——后面 4.2 会详细讲。

**什么是“并行前缀（Parallel Prefix）”？**
对一个向量做“从低到高的累积运算”。普通做法是从低位到高位逐位串行累积，逻辑深度是 \(O(n)\)；并行前缀网络（类似加法器里的 Kogge-Stone 结构）把它降到 \(O(\log n)\) 级，对宽向量（比如几十路仲裁请求）的时序非常关键。本讲的 `ppc_or` 就是一个并行前缀“或”。

最后，本讲的 `ppc_or` 内部会调用 `log2ceil`，这个函数来自上一讲的 [psi_common_math_pkg](hdl/psi_common_math_pkg.vhd)（见讲义 u2-l1），用来推导需要多少级前缀网络。这是本包对 math_pkg 的唯一依赖。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_common_logic_pkg.vhd](hdl/psi_common_logic_pkg.vhd) | **本讲主角**。声明并实现 13 个可综合的逻辑向量工具函数。 |
| [hdl/psi_common_math_pkg.vhd](hdl/psi_common_math_pkg.vhd) | 提供 `log2ceil`，被 `ppc_or` 调用以推导前缀级数（依赖讲义 u2-l1）。 |
| [hdl/psi_common_async_fifo.vhd](hdl/psi_common_async_fifo.vhd) | 异步 FIFO，是 `binary_to_gray`/`gray_to_binary` 最典型的真实使用者。 |
| [hdl/psi_common_arb_priority.vhd](hdl/psi_common_arb_priority.vhd) | 固定优先级仲裁器，是 `ppc_or` 最典型的真实使用者。 |
| [testbench/psi_common_logic_pkg_tb/psi_common_logic_pkg_tb.vhd](testbench/psi_common_logic_pkg_tb/psi_common_logic_pkg_tb.vhd) | 本包自带的测试平台，给出每个函数的期望值，是本讲实践的依据。 |

## 4. 核心概念与源码讲解

本包的函数全部是**纯组合逻辑、可综合**的（见 [doc/old/ch2_packages/ch2_packages.md](doc/old/ch2_packages/ch2_packages.md) 第 2.2 节的官方说明）。也就是说，它们不是仿真专用的“花架子”，而是真的会被编译成逻辑门。我们按“向量生成与移位 → 格雷码 → 并行前缀或 → 归约与归一化”四块来拆。

### 4.1 向量生成与移位

#### 4.1.1 概念说明

这一组函数解决两类小烦恼：

1. **“我要一个宽度由 generic 决定的全 0/全 1 向量”。** VHDL 里你不能直接写一个字面量然后说“它的宽度等于某个变量”——`(others => '0')` 虽然行，但需要一个目标类型上下文。`zeros_vector` / `ones_vector` 把这事封装成函数，可以在任意表达式里直接用。
2. **“我要对 `std_logic_vector` 做逻辑移位并保持宽度、用指定值填充”。** IEEE `numeric_std` 的 `shift_left`/`shift_right` 是针对 `unsigned`/`signed` 的算术移位，语义不直观；本包的版本直接面向 `std_logic_vector`，并允许你选择用 `'0'` 还是 `'1'` 填充空出来的位。

`partially_ones_vector(size, ones_nb)` 则生成一个“低 `ones_nb` 位为 1、其余为 0”的掩码，常用于地址译码、字节使能掩码等。

#### 4.1.2 核心流程

- `zeros_vector(size)`：返回 `size` 位的全 0 向量。
- `ones_vector(size)`：返回 `size` 位的全 1 向量。
- `partially_ones_vector(size, ones_nb)`：返回 `size` 位向量，只有最低 `ones_nb` 位是 1。
- `shift_left(arg, bits, fill)`：把 `arg` 左移 `bits` 位，高位移出，低位用 `fill` 补；**若 `bits` 为负，则自动转成右移**。
- `shift_right(arg, bits, fill)`：对称的右移版本；同样支持负数 `bits` 自动转左移。

#### 4.1.3 源码精读

`zeros_vector` 与 `ones_vector` 都很直白，用 `(others => ...)` 构造：
[zeros_vector 与 ones_vector 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L77-L88)

`partially_ones_vector` 的写法看起来绕，是刻意为之的“防综合器踩坑”技巧：
[partially_ones_vector 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L91-L104)

关键在第 96 行注释：“We need this to avoid synthesis problems with Xilinx ISE”。作者故意把中间向量多扩 1 位（`v_plus_1` 宽到 `size+2`），再切片 `v_plus_1(size downto 1)` 取回 `size` 位。这样无论 `ones_nb` 是 0 还是 `size`，切片范围都合法、不会出现“空范围（null range）”，从而绕开老版本 Xilinx ISE 对空范围的处理 bug。手动验证 `partially_ones_vector(8, 3)`：

```
v_low  = "111111111"  (9 位全 1)
v_high = "000000000"  (9 位全 0)
v_plus_1 = v_high(5 downto 0) & v_low(3 downto 0)
         = "000000" & "1111" = "0000001111" (10 位)
v = v_plus_1(8 downto 1) = "00000111"   ✓ 低 3 位为 1
```

`shift_left` 的核心技巧在第 111 行 `argDt : std_logic_vector(arg'high downto arg'low) := arg;`：先把输入“方向归一化”成 `downto`，保证函数对 `to` 方向声明的向量也能正常工作；第 114 行的 `if bits < 0` 让左移接受负数并自动转右移：
[shift_left 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L107-L121)

`shift_right` 与之完全对称，参见 [shift_right 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L124-L138)。

测试平台给出了移位的黄金样例（高位在左、低位在右）：
[shift_left/shift_right 期望值](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_logic_pkg_tb/psi_common_logic_pkg_tb.vhd#L43-L53)，例如 `shift_left("11101", 2, '0') = "10100"`（最高两位 `11` 移出，最低两位补 `0`），`shift_right("10111", 2, '1') = "11101"`（最低两位 `11` 移出，最高两位补 `1`）。

#### 4.1.4 代码实践

**实践目标**：用测试平台验证移位函数，并手算预测一个新输入。

**操作步骤**：
1. 打开 [psi_common_logic_pkg_tb.vhd](testbench/psi_common_logic_pkg_tb/psi_common_logic_pkg_tb.vhd) 第 43–53 行，看清已有的 6 组移位断言。
2. 在 `*** shift_left ***` 段后追加一行（**示例代码**，仅用于练习，不是项目原有内容）：

   ```vhdl
   StdlvCompareStdlv("01111", shift_left("11101", 3, '1'), "My own check");
   ```

3. 按讲义 u1-l3 描述的 PsiSim/Modelsim 流程编译并运行该 TB。

**需要观察的现象**：仿真日志里出现 `*** shift_left ***`，且你新加的那行不打印 `Wrong Result` 类报错。

**预期结果**：`shift_left("11101", 3, '1')` 把 `11101` 左移 3 位，高 3 位 `111` 移出，剩下 `01`，低 3 位补 `'1'`，得到 `"01111"`。

**待本地验证**：本讲无法替你跑仿真，请在本机确认日志中无 `###ERROR###`。

#### 4.1.5 小练习与答案

**练习 1**：`shift_left("10101", 1, '1')` 等于什么？参考答案在 TB 第 47 行。

> 答案：`"01011"`。左移 1 位，最高位 `1` 移出，低 1 位补 `'1'`。

**练习 2**：`shift_left(x, -2, '0')` 会发生什么？

> 答案：等价于 `shift_right(x, 2, '0')`。见源码第 114–115 行对负数 `bits` 的处理。

**练习 3**：`partially_ones_vector(8, 8)` 的结果是什么？

> 答案：`"11111111"`（8 位全 1）。第 101–102 行的切片在 `ones_nb = size` 时仍然合法，得到全 1。

---

### 4.2 格雷码转换

#### 4.2.1 概念说明

这是本包**最重要、被复用最多**的一组函数，是异步 FIFO 的命根子。

问题背景：当两个不同时钟域之间要传递一个多比特值（比如 FIFO 的读写指针），你**不能**直接把多根线接过去。因为每个寄存器到同步器的布线延迟不同，多位同时翻转时，同步器可能在某一次采样里捕获到“某些位是新值、某些位是旧值”的中间态，得到一个完全错误的数值。

格雷码的妙处在于：**相邻两个值之间只有 1 个比特不同**。因此同步器在任一时刻要么采到旧值、要么采到新值，永远不会采到“错位的中间态”。这就是为什么异步 FIFO 的指针要先转成格雷码再跨时钟域，到了对岸再转回二进制。

#### 4.2.2 核心流程

设二进制为 \(B\)，格雷码为 \(G\)，位宽 \(n\)（最高位下标 \(n-1\)）：

- 二进制 → 格雷码（前缀式，廉价）：

\[ G_i = \begin{cases} B_i \oplus B_{i+1}, & i < n-1 \\ B_{n-1}, & i = n-1 \end{cases} \]

- 格雷码 → 二进制（累积异或，较贵）：

\[ B_i = \bigoplus_{k=i}^{n-1} G_k \]

也就是从最高位往低位逐位“累积异或”：\(B_{n-1} = G_{n-1}\)，\(B_i = G_i \oplus B_{i+1}\)。正因为它是逐级异或链，逻辑深度比编码方向深，所以异步 FIFO 把这一步放在额外的一拍寄存器里。

#### 4.2.3 源码精读

`binary_to_gray` 用一行位移+异或完成：
[binary_to_gray 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L141-L147)

第 145 行 `binary xor ('0' & binary(binary'high downto binary'low + 1))`：把二进制右移 1 位（丢掉最低位、最高位补 `'0'`），再与原值异或。最高位因为补了 `'0'`，异或后保持原值；其余每位与它上一位异或——正好就是上面的公式。

`gray_to_binary` 是一个从高到低的循环：
[gray_to_binary 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L150-L159)

第 154 行先拷贝最高位，第 155–157 行的 `for` 循环从次高位往低位逐位做 \(B_i = G_i \oplus B_{i+1}\)。这条异或链就是“累积异或”。

测试平台用一张 3 位真值表覆盖了全部 8 种取值：
[格雷码真值表](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_logic_pkg_tb/psi_common_logic_pkg_tb.vhd#L32-L33)，即二进制 `000,001,010,011,100,101,110,111` 对应格雷码 `000,001,011,010,110,111,101,100`。注意每相邻两项确实只差 1 个比特。

最有说服力的是真实使用者 `psi_common_async_fifo`：
[异步 FIFO 的指针跨时钟域代码](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L201-L214)

- 第 203–204 行：写/读二进制指针在**本时钟域内**转成格雷码（注释说“Bin->Gray is simple, can be done without additional FF”，因为是一拍组合异或）。
- 第 207–210 行：两级寄存器同步（`GraySync` ← `Gray`），把格雷码指针安全地送到对岸时钟域。
- 第 213–214 行：在对岸把格雷码转回二进制（注释说“Gray->Bin involves some logic, needs additional FF”，因为异或链较深，单独占一拍）。

这套“二进制→格雷码→双 FF 同步→格雷码→二进制”正是异步 FIFO 指针 CDC 的教科书做法（Cummings 经典论文的套路），而它完全建立在 `binary_to_gray`/`gray_to_binary` 两个函数之上。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：用 `binary_to_gray` / `gray_to_binary` 实现地址的格雷编解码，并验证两者的可逆性（先编码再解码应得到原值）。

**操作步骤**：
1. 打开 [psi_common_logic_pkg_tb.vhd 第 55–65 行](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_logic_pkg_tb/psi_common_logic_pkg_tb.vhd#L55-L65)，看清现有的 3 位可逆性测试：它先用 `binary_to_gray` 把每个二进制编成格雷码（与黄金表比对），再用 `gray_to_binary` 把每个格雷码解回二进制（与原表比对）。
2. 模仿异步 FIFO 的真实用法，在 TB 的 `*** gray_to_binary ***` 段后追加一段 **4 位地址的往返测试**（**示例代码**，非项目原有）：

   ```vhdl
   -- 4-bit address round-trip: binary -> gray -> binary must be identity
   for a in 0 to 15 loop
     declare
       constant bin_c  : std_logic_vector(3 downto 0) := std_logic_vector(to_unsigned(a, 4));
       constant gray_c : std_logic_vector(3 downto 0) := binary_to_gray(bin_c);
     begin
       StdlvCompareStdlv(bin_c, gray_to_binary(gray_c),
                         "Round-trip failed for a=" & integer'image(a));
     end;
   end loop;
   ```

   > 说明：VHDL 的 `for` 循环里声明常量需要 `declare` 块；若你的仿真器对循环内声明支持不好，也可改用先在循环外算好数组的写法，与本文件第 32–33 行的 `t_aslv3` 数组一致，只是换成 `t_aslv4`。
3. 按 u1-l3 的 PsiSim 流程编译运行。

**需要观察的现象**：日志依次打印 `*** binary_to_gray ***` 和 `*** gray_to_binary ***`，且你新加的 16 次往返比较全部不触发 `Wrong Result` / `###ERROR###`。

**预期结果**：对任意 4 位地址 \(a \in [0,15]\)，`gray_to_binary(binary_to_gray(bin_c)) = bin_c` 恒成立——即可逆性成立。同时可以顺手观察到，相邻地址 `a` 与 `a+1` 的格雷码**恰好只差 1 个比特**。

**待本地验证**：请在本地仿真器确认无 `###ERROR###`。

#### 4.2.5 小练习与答案

**练习 1**：手算 `binary_to_gray("010")`。

> 答案：`"010" xor ('0' & "01") = "010" xor "001" = "011"`。与真值表第 3 项一致。

**练习 2**：为什么异步 FIFO 在“格雷码→二进制”这一步要额外占用一拍寄存器，而“二进制→格雷码”不用？

> 答案：编码方向只做一次移位+异或，逻辑很浅，可与其他逻辑合在一拍；解码方向是逐位累积异或链，逻辑深度随位宽线性增长，为保证时序单独打一拍。见 `async_fifo` 第 202、212 行的注释。

**练习 3**：如果直接把 4 位二进制指针（不经格雷码）跨时钟域同步，最坏情况下同步器可能采到什么？

> 答案：当多个比特同时翻转（例如 `0111` → `1000`，8 个比特全翻），由于各比特布线延迟不同，同步器可能采到任意中间值，如 `1111`、`0000`、`1011` 等，导致 FIFO 满空判断完全错误。格雷码保证每次只翻 1 位，从根本上消除这种风险。

---

### 4.3 并行前缀或 ppc_or

#### 4.3.1 概念说明

`ppc_or` = Parallel Prefix Computation of OR，即“并行前缀或”。它对输入向量的每一位，输出“该位及所有低于它的位的或”。源码注释给了一个最直观的样例：

```
输入    --> 输出
0100    --> 0111
0101    --> 0111
0011    --> 0011
0010    --> 0011
```

数学上：

\[ \text{out}_i = \bigvee_{k=0}^{i} \text{in}_k \]

也就是从低位到高位的“累积或”。它最有名的用途是**固定优先级编码**：只要再做一次“与上自己右移一位的反”，就能找出“最低的那个置位比特”。

#### 4.3.2 核心流程

朴素实现是从低到高逐位累积异或……不，是逐位累积或，逻辑深度 \(O(n)\)，位宽一大就时序紧张。`ppc_or` 用并行前缀网络把它压到 \(O(\log n)\) 级：

1. 用 `log2ceil(inp'length)` 算出需要的级数 `Stages_c`（调用了 math_pkg）。
2. 算出大于等于位宽的最小 2 的幂 `Pwr2Width_c`，把输入零扩展到 2 的幂宽，方便统一处理。
3. 每一级 `stage` 中，每个比特要么与“比自己低 \(2^{\text{stage}}\) 位”的那个比特相或，要么直通；逐级倍增跨度。
4. 取回原宽度返回。

#### 4.3.3 源码精读

[ppc_or 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L162-L184)

- 第 164 行 `Stages_c := log2ceil(inp'length)`：级数 = 位宽的 log2 上取整，例如位宽 5 → 级数 3。这是对本包对 math_pkg 的唯一调用点。
- 第 165–167 行：把内部工作宽度对齐到 2 的幂 `Pwr2Width_c`，并声明“每级一个全宽向量”的数组 `StageOut_t`。
- 第 170–171 行：第 0 级初始化为零扩展后的输入。
- 第 172–182 行：双重循环——外层逐级、内层逐比特；第 175–178 行用 `BinCnt_v(stage)` 这一位做选择，决定该比特是“或上 \(2^{\text{stage}}\) 位之前的结果”还是“直通”。
- 第 183 行：从 2 的幂宽切回原宽度返回。

最经典的真实用法在固定优先级仲裁器 `psi_common_arb_priority`：
[arb_priority 用 ppc_or 做优先级编码](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_priority.vhd#L41-L49)

```vhdl
OredRequest_v := ppc_or(req_i);                                          -- 第 45 行
Grant_I <= OredRequest_v and not ('0' & OredRequest_v(OredRequest_v'high downto 1));  -- 第 48 行
```

解读：
- `OredRequest_v(i) = '1'` 表示“下标 ≤ i 中存在请求”。
- `'0' & OredRequest_v(high downto 1)` 是把 `OredRequest_v` 向高位平移 1 位、最低位补 `'0'`；`not` 后取反。
- 两者相与，恰好只在“第一个出现请求的位置”为 `'1'`，即授予优先级最高（下标最小）的那个请求者。

这就是用两行代码、且时序为 \(O(\log n)\) 的固定优先级仲裁。

#### 4.3.4 代码实践

**实践目标**：手算 `ppc_or` 在一段请求向量上的输出，并验证 `arb_priority` 的授权结果。

**操作步骤**：
1. 设 `req_i = "0100"`（4 位，只有下标 2 的请求者拉高）。
2. 手算 `ppc_or("0100")`：下标 0、1 的累积或为 0，下标 2、3 的累积或为 1，得 `"0111"`。
3. 手算第 48 行的授权：`'0' & "011" = "0011"`，`not "0011" = "1100"`，`"0111" and "1100" = "0100"`，即授权下标 2。
4. 打开测试平台 [第 67–79 行的 ppc_or 测试](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_logic_pkg_tb/psi_common_logic_pkg_tb.vhd#L67-L79)，它遍历长度 1–6、每个可能取值，断言 `ppc_or` 等于 `2**(log2(value)+1)-1`（即“值 v 的最高置位比特以及它以下全置 1”）。
5. 运行 TB 观察该段是否通过。

**需要观察的现象**：`*** ppc_or ***` 段无 `###ERROR###`；对 `req_i = "0100"`，仲裁器输出 grant 仅在 bit 2 为 1。

**预期结果**：如上手算——`ppc_or("0100") = "0111"`，授权 `grant = "0100"`。

**待本地验证**：请在本机仿真确认。

#### 4.3.5 小练习与答案

**练习 1**：`ppc_or("0010")` 等于什么？

> 答案：`"0011"`（下标 1 置位，故下标 1、2、3 的累积或均为 1；下标 0 为 0）。

**练习 2**：为什么 `arb_priority` 不直接用一个 `for` 循环从低到高找第一个置位比特？

> 答案：串行 `for` 循环综合后是 \(O(n)\) 深度的选择链，请求者一多就成时序瓶颈；`ppc_or` 的并行前缀结构把深度压到 \(O(\log n)\)，对宽仲裁器时序更友好。

**练习 3**：`ppc_or` 内部为什么要把位宽对齐到 2 的幂？

> 答案：并行前缀网络按 \(2^{\text{stage}}\) 的跨度倍增合并，2 的幂宽能让每一级的索引 `(idx/(2**stage)+1)*2**stage` 始终落在合法范围内，避免对非 2 的幂位宽做特殊边界处理。见源码第 165–176 行。

---

### 4.4 归约、归一化与杂项函数

#### 4.4.1 概念说明

这一组是“用得不多但用到时很省事”的小工具：

- `reduce_or(vec)` / `reduce_and(vec)`：把整个向量或/与归约成 1 个 `std_logic`。常用于“是否有任何请求”“是否全部就绪”。
- `int_to_std_logic(int)`：把整数 `0/1` 转成 `'0'/'1'`，其它值转 `'X'`。当你用整数算了一个条件、却要驱动 `std_logic` 信号时很方便。
- `to_01X(inp)`：把九值 `std_logic` 归一化成 `{'0','1','X'}`——`'L'→'0'`、`'H'→'1'`、其余（`'Z'/'U'/'W'/'-'` 等）→`'X'`。有标量和向量两个重载。
- `invert_bit_order(inp)`：把向量比特顺序整个反过来，常用于某些串行协议或比特倒序寻址（如 FFT 地址翻转）。

#### 4.4.2 核心流程

- `reduce_or`：初值 `'0'`，从低到高逐位 `or`。
- `reduce_and`：初值 `'1'`，从低到高逐位 `and`。
- `int_to_std_logic`：`1→'1'`、`0→'0'`、否则 `'X'`。
- `to_01X`（标量）：`'0'/'L'→'0'`、`'1'/'H'→'1'`、其余 `'X'`；向量版逐位调用标量版。
- `invert_bit_order`：`tmp(high - i) := inp(i)`，逐位镜像翻转。

#### 4.4.3 源码精读

归约函数的循环写得很直白：
[reduce_or / reduce_and 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L198-L218)

注意它们用 `vec'low to vec'high` 遍历，因此对任意范围/方向的向量都成立。

`int_to_std_logic` 用 `if/elsif` 把整数映射到三值：
[int_to_std_logic 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L186-L196)

`to_01X` 用 `case` 把九值收敛为三值：
[to_01X 标量与向量重载](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L220-L238)

第 224–226 行把弱信号 `'L'/'H'` 也归一到强 `'0'/'1'`，这在仿真里特别有用——比如开漏/上拉总线返回的是 `'H'`，归一化后才能与 `'1'` 正确比较。

`invert_bit_order` 通过下标镜像实现比特翻转：
[invert_bit_order 实现](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L240-L248)

> 说明：测试平台 `psi_common_logic_pkg_tb` 目前**只覆盖了** `zeros/ones/shift/gray/ppc_or` 这几组函数（见 TB 第 35–79 行），并未对 `reduce_or`/`reduce_and`/`to_01X`/`invert_bit_order`/`int_to_std_logic`/`partially_ones_vector` 做断言。使用它们时建议自己补测——这也是下面综合实践的动机之一。

#### 4.4.4 代码实践

**实践目标**：为未被 TB 覆盖的 `reduce_or` / `to_01X` 补一个最小自检。

**操作步骤**：在 TB 末尾（第 79 行 `end loop;` 之后、`wait;` 之前）追加（**示例代码**，非项目原有）：

```vhdl
-- reduce_or / reduce_and
print("*** reduce_or / reduce_and ***");
StdlvCompareInt(1, bit'pos(reduce_or("0100")), "reduce_or wrong");   -- 有 1 即 1
StdlvCompareInt(0, bit'pos(reduce_and("1101")), "reduce_and wrong"); -- 非全 1 即 0

-- to_01X
print("*** to_01X ***");
StdlvCompareStdlv("01X", to_01X("H L Z" ... ), "to_01X wrong");
```

> 说明：第二段里把一个含 `'H'/'L'/'Z'` 的向量传给 `to_01X`，期望结果是 `"01X"`（`'H'→'1'`、`'L'→'0'`、`'Z'→'X'`）。由于向量字面量里直接写 `'H'/'L'/'Z'` 需要按字符拼接，请按你的仿真器语法构造该常量；核心是验证 `'H'→'1'`、`'L'→'0'`、`'Z'→'X'` 这三条映射。

**需要观察的现象**：新打印的三个小节均无 `###ERROR###`。

**预期结果**：`reduce_or("0100") = '1'`、`reduce_and("1101") = '0'`、`to_01X` 把 `'H'/'L'/'Z'` 分别映射为 `'1'/'0'/'X'`。

**待本地验证**：本讲未运行仿真，请在本地确认；尤其 `bit'pos(...)` 这类把 `std_logic` 转成整数用于 `StdlvCompareInt` 的写法，可能需要根据 psi_tb 版本调整。

#### 4.4.5 小练习与答案

**练习 1**：`reduce_and("1111")` 和 `reduce_or("0000")` 分别等于什么？

> 答案：`reduce_and("1111") = '1'`（全 1 才为 1）；`reduce_or("0000") = '0'`（全 0 才为 0）。见第 198–218 行。

**练习 2**：`to_01X('H')` 和 `to_01X('Z')` 分别等于什么？为什么把 `'H'` 归一成 `'1'` 有用？

> 答案：`to_01X('H') = '1'`、`to_01X('Z') = 'X'`。开漏总线空闲时经上拉读到的是弱高 `'H'`，若不归一化，`'H' = '1'` 在 std_logic 下并非恒真，比较会失败；归一成 `'1'` 后才能正确判“总线空闲”。

**练习 3**：`invert_bit_order("1100")`（4 位，downto）等于什么？

> 答案：`"0011"`。第 0 位与最高位互换、第 1 位与次高位互换，整体镜像。

---

## 5. 综合实践

**任务**：模拟一次“异步 FIFO 指针跨时钟域”的全过程，把本讲的格雷码与前缀或串起来。

**背景**：异步 FIFO 的写指针是二进制计数器，要安全地送到读时钟域去判断“空”。我们用本包的函数手工走一遍编码—同步—解码链路，并顺便用 `reduce_or` 判断“指针是否非零”。

**操作步骤**（**示例代码**，可放在一个新的小型 TB 或 `psi_common_logic_pkg_tb` 的扩展里）：

1. 声明一个 4 位写指针，从 `0000` 起每拍 `+1`，共走 16 个值。
2. 对每个值，依次执行：

   ```vhdl
   -- (a) 本域内：二进制 -> 格雷码
   gray_c       := binary_to_gray(bin_c);
   -- (b) 模拟双 FF 同步：在真实 FIFO 里这里跨到对岸时钟域，仿真里直接透传一拍即可
   gray_synced  := gray_c;
   -- (c) 对岸：格雷码 -> 二进制
   bin_recovered := gray_to_binary(gray_synced);
   -- (d) 用归约判断“指针是否非零”
   ptr_nonzero  := reduce_or(bin_recovered);
   ```

3. 对每一步加断言：
   - `bin_recovered = bin_c`（可逆性）。
   - 相邻两拍的 `gray_c` 汉明距离恰为 1（仅 1 位不同）。
   - 当 `bin_c = 0` 时 `ptr_nonzero = '0'`，否则 `'1'`。

**预期结果**：
- 全部 16 个值的往返都可逆。
- 相邻格雷码两两只差 1 位（这正是格雷码被选中的根本原因）。
- `reduce_or` 正确区分零指针与非零指针。

**反思题**：如果把第 (b) 步换成“直接同步二进制 `bin_c`”（不走格雷码），你能构造出一个使往返出错的 `bin_c` 跳变吗？（提示：`0111 → 1000` 这种全位翻转最危险。）

**待本地验证**：完整跑通需要 PsiSim/psi_tb 环境（见 u1-l3），请在本地确认无 `###ERROR###`。

## 6. 本讲小结

- `psi_common_logic_pkg` 是一组**纯组合、可综合**的逻辑向量工具，不描述具体硬件，而是让全库的位运算更简洁、更可移植。
- **向量与移位**：`zeros_vector/ones_vector/partially_ones_vector` 生成掩码；`shift_left/shift_right` 对 `std_logic_vector` 做带填充的逻辑移位，且支持负位移自动换向。
- **格雷码**：`binary_to_gray`（一拍廉价）与 `gray_to_binary`（累积异或、较深）是异步 FIFO 指针跨时钟域的基石，保证多位值在同步器里永远只被采到旧值或新值。
- **并行前缀或 `ppc_or`**：用 \(O(\log n)\) 深度的前缀网络实现“低位到高位的累积或”，被 `arb_priority` 用两行代码完成固定优先级仲裁。
- **归约与归一化**：`reduce_or/reduce_and` 压缩向量、`to_01X` 把九值收敛为 `{'0','1','X'}`（弱信号 `'L'/'H'` 也被归一），`invert_bit_order` 做比特镜像翻转。
- 本包对外的唯一依赖是 math_pkg 的 `log2ceil`（被 `ppc_or` 用于推导级数）；自带 TB 覆盖了 `zeros/ones/shift/gray/ppc_or`，其余函数建议自行补测。

## 7. 下一步学习建议

- **紧接着读 u2-l3（array_pkg）**：本讲的 TB 用到了 `t_aslv3`，那是 array_pkg 提供的固定宽度数组类型，配合本讲的向量函数可以构造查表式的常量测试。
- **在 CDC 单元（u5）回头看格雷码**：等你学到 `async_fifo`（u4-l2）和 `pulse_cc`（u5-l1）时，会发现本讲的 `binary_to_gray/gray_to_binary` 是它们跨时钟域机制的核心；届时建议重读 4.2，把“函数”和“系统级 CDC”对应起来。
- **在仲裁单元（u10-l1）回头看 ppc_or**：`arb_priority` / `arb_round_robin` 会大量用到本讲的 `ppc_or`，到时可结合 4.3 理解“为什么宽仲裁器必须用并行前缀而不是串行循环”。
- **动手补 TB**：本讲 4.4 指出 `reduce_or/reduce_and/to_01X/invert_bit_order` 尚未被官方 TB 覆盖，按讲义 u11-l1 的自校验 TB 约定为它们补一组断言，是一次很好的练手。
