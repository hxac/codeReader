# math 模块：常用数学函数

## 1. 本讲目标

本讲进入 `math` 模块。它提供两类东西：一个纯函数工具包 `math_pkg`，以及三个真正综合成电路的运算实体。读完本讲，你应当能够：

- 用 `math_pkg` 的极值/钳位/对数/位宽计算/格雷码等函数，在精化期（elaboration）算出 generic 派生量，避免手算出错。
- 看懂 `saturate_signed` 如何只看「符号位 + 保护位」就用极小逻辑把有符号数饱和（钳位）到目标位宽。
- 理解 `truncate_round_signed` 如何在去掉小数位时做四舍五入（含「收敛舍入」与溢出饱和）。
- 掌握 `unsigned_divider` 的位串行长除法结构，以及它为何是一个「单事务在途、吞吐随位宽线性下降」的多周期模块。

本讲承接 u2-l2：会反复用到 `types_pkg` 的 `to_sl`/`to_int`/`binary_integer_t`，并继续看到「用 generic 裁剪功能、把资源纳入回归」的项目风格（u1-l1、u1-l4）。

## 2. 前置知识

进入源码前，先建立四个直觉。

**第一，定点数与「保护位」。** 硬件里常用定点数表示带小数的值：约定某些低位是「小数位」。做完乘法或加法后，结果往往会变宽。例如两个 16 位定点数相乘得到 32 位，其中多出的高位叫**保护位（guard bits）**——它们是符号位的延伸，用来暂存可能的溢出。等到下一级再把这些多余位「收窄」回目标位宽，就需要**饱和（saturate）**或**截断（truncate）**。本讲的 `saturate_signed` 与 `truncate_round_signed` 就是干这两件事的。

**第二，二进制补码与符号扩展。** 一个有符号数（two's complement）在范围内时，它的高位必然全部等于符号位（正数高位全 0，负数高位全 1），这叫**符号扩展**。反过来：如果一个数的若干高位「不全等于符号位」，就说明它已经超出更窄的表示范围。`saturate_signed` 正是利用这条性质，只看最高几位就能判断是否需要钳位。

**第三，舍入的两种「半数」规则。** 把小数位去掉时要四舍五入，难点在「正好 0.5」时往哪边凑：

- **四舍五入到正无穷（round half up）**：0.5 一律向上（往大的方向）凑。简单，但长期累加会引入正偏差。
- **收敛舍入（round half to even，银行家舍入）**：0.5 时凑到最近的**偶数**。这是 IEEE 754 浮点的默认规则，统计上无偏差。代价是多一点逻辑、关键路径更长。

**第四，长除法与「位串行」电路。** 小学竖式除法是「把除数对齐到被除数最高位，逐位试商」。把这套搬进硬件、用二进制做，每位只试 0 或 1，就是**位串行除法器**：每拍处理一位，延迟随被除数位宽线性增长。它面积小、吞吐低，适合不频繁的除法（如配置计算）；若要每拍一个除法，得用流水线除法器，本项目不提供。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [modules/math/src/math_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd) | 纯函数包：极值、钳位、对数、位宽计算、符号判断、格雷码、整除取整、数论等 |
| [modules/math/src/saturate_signed.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/saturate_signed.vhd) | 有符号数饱和：去掉高位保护位，超范围则钳到最大/最小值 |
| [modules/math/src/truncate_round_signed.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd) | 有符号数截断 + 四舍五入：去掉低位小数位，可选收敛舍入与溢出饱和 |
| [modules/math/src/unsigned_divider.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd) | 无符号位串行除法器：多周期，输出商与余数 |

阅读时还会顺带引用三个真实用例：测试台 `tb_saturate_signed.vhd`、`tb_unsigned_divider.vhd`，以及登记测试与 netlist 构建的 `module_math.py`。

---

## 4. 核心概念与源码讲解

### 4.1 math_pkg：精化期可用的数学工具箱

#### 4.1.1 概念说明

写 RTL 时有一类「计算」其实跟电路运行无关，而是在**精化期**（工具把 generic 展开成具体电路的那一刻）就要算出确定值。比如：

- 「表示 1000 个状态需要几位？」→ `num_bits_needed(1000)` = 10。
- 「这个 FIFO 深度是不是 2 的幂？」→ `is_power_of_two(depth)`。
- 「32 位有符号数的最大/最小值是多少？」→ `get_max_signed_integer(32)`。

这些值在综合前就能确定，写死成常数又容易算错或失去通用性。`math_pkg` 把它们做成纯函数，让你在常量声明里直接调用，由工具替你算。它本身不综合出任何电路（函数在精化期被求值后即「折叠」进常量），是写参数化 RTL 的得力助手。

#### 4.1.2 核心用法（函数清单）

[math_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd) 提供的函数可按下表分类记忆：

| 分类 | 代表函数 | 用途 |
| --- | --- | --- |
| 极值 | `get_min/max_signed(_integer)`、`get_min/max_unsigned(_integer)` | 给定位宽的合法表示范围（本讲 `saturate` 测试台用到） |
| 钳位 | `clamp(value, min, max)`（integer 与 u_signed 两版） | 把值限制在闭区间 |
| 对数 / 2 的幂 | `ceil_log2`、`log2`、`is_power_of_two`、`round_up_to_power_of_two` | 地址位宽、深度向上取整到 2 的幂 |
| 位宽计算 | `num_bits_needed`（无符号）、`num_bits_needed_signed`（标量/向量/矩阵） | 表示一个数至少需要几位 |
| 符号判断 | `lt_0`、`geq_0` | 判断有符号数正负，综合友好 |
| 整除取整 | `div_round_negative` | 向负无穷取整的整数除法（Python 风格） |
| 格雷码 | `to_gray`、`from_gray`、`hamming_distance` | 跨时钟域指针编码（承接 u3） |
| 向量 | `abs_vector`、`vector_sum` | 整数向量求绝对值、求和 |
| 数论 | `greatest_common_divisor`、`is_mutual_prime` | 最大公约数、互质判定 |

#### 4.1.3 源码精读

**极值函数：给定位宽的范围。** 这是本讲后续 `saturate` 测试台的「标准答案」来源。有符号 N 位的范围是 \([-2^{N-1},\ 2^{N-1}-1]\)：

[math_pkg.vhd:120-132](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L120-L132) —— `get_min_signed` 把最高位置 1（最小负数 `100...0`），`get_max_signed` 全 1 再把最高位清 0（最大正数 `011...1`）。

```vhdl
function get_min_signed(num_bits : positive) return u_signed is
  variable result : u_signed(num_bits - 1 downto 0) := (others => '0');
begin
  result(result'high) := '1';
  return result;
end function;
```

**clamp：闭区间钳位。** `clamp` 有两个重载，分别吃 `integer` 和 `u_signed`。u_signed 版本（[math_pkg.vhd:184-198](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L184-L198)）先断言 min/max 不比 value 宽，再分三段返回：小于 min 则放大 min、大于 max 则放大 max、否则原样返回。它就是 `saturate_signed` 期望行为的「软件定义」，测试台直接拿它当对拍基准（见 4.2.3）。

**lt_0 / geq_0：避开 Vivado 的「重逻辑」陷阱。** 这是一对很有教学意义的函数。直观写法是 `if value < 0 then ...`，但注释指出 Vivado 综合它会生成 20–30 个 LUT。于是改看符号位：

[math_pkg.vhd:241-253](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L241-L253)

```vhdl
function lt_0(value : u_signed) return boolean is
begin
  -- The Vivado synthesis engine has been shown to produce a lot of logic (20-30 LUTs) when
  -- doing simply "if value < 0 then ...", hence this bit operation is used instead.
  return value(value'left) = '1';
end function;
```

有符号数负数的符号位（最左位）为 `'1'`，于是判负退化成读一位——几乎零成本。这个 `lt_0` 会在 4.4 的 `unsigned_divider` 里被用来判断减法结果是否为负（即「不够减」）。这是一处「写法不同、综合结果天差地别」的真实案例，也是本项目把 `lt_0` 放进 `math_pkg` 复用的原因。

**对数与位宽：地址译码的常客。** `ceil_log2(value)` 返回 \(\lceil \log_2(\text{value}) \rceil\)，正好是「表示 value 个不同地址所需的最少位数」。例如 `ceil_log2(1000) = 10`。[math_pkg.vhd:202-206](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L202-L206) 借 `ieee.math_real` 的实数 `log2` 再向上取整实现。注意它和 `log2`（[math_pkg.vhd:213-219](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L213-L219)）不同：后者要求入参是 2 的幂，否则断言报错，适合「我知道这是 2 的幂，只想取对数」的场景。

**整除取整：补上 VHDL 的语义缺口。** VHDL 的 `integer` 除法向零取整（`-7/2 = -3`），而 Python/C 及硬件移位向负无穷取整（`-7//2 = -4`）。`div_round_negative`（[math_pkg.vhd:320-327](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L320-L327)）统一成后者，让 RTL 里的整数除法与上位机软件行为一致，避免协议层算地址时两边对不齐。

#### 4.1.4 代码实践

**实践目标：** 用 `math_pkg` 的纯函数在精化期算出几个常用量，并在仿真里打印验证。

**操作步骤：**

1. 仿照 `tb_math_pkg.vhd` 的写法，在一个测试台 process 里加下面这段「示例代码」（非项目原有代码）：

```vhdl
-- 示例代码：体验 math_pkg 的纯函数
library math;
use math.math_pkg.all;
-- ...
process is
begin
  report "ceil_log2(1000) = " & integer'image(ceil_log2(1000));          -- 期望 10
  report "num_bits_needed(1000) = " & integer'image(num_bits_needed(1000));-- 期望 10
  report "round_up_to_power_of_two(1000) = " & integer'image(round_up_to_power_of_two(1000));-- 期望 1024
  report "is_power_of_two(1024) = " & boolean'image(is_power_of_two(1024));-- 期望 true
  report "get_max_signed_integer(8) = " & integer'image(get_max_signed_integer(8));-- 期望 127
  std.env.stop;
end process;
```

2. 把它挂到某个 `module_*.py` 的 `setup_vunit`（见 u1-l4），用 `tools/simulate.py` 运行；或在 GHDL/ModelSim 里单独编译 `math`、`common` 库后仿真。

**需要观察的现象：** 仿真控制台输出五行 `report`。

**预期结果：** `10`、`10`、`1024`、`true`、`127`。

> 由于运行依赖本地工具链，具体命令与输出「待本地验证」。若暂无仿真器，可改为「源码阅读型实践」：阅读 [tb_math_pkg.vhd:136-175](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/test/tb_math_pkg.vhd#L136-L175) 中 `ceil_log2` 与 `num_bits_needed` 的断言，记录每个入参对应的期望值，与你的心算对照。

#### 4.1.5 小练习与答案

**练习 1：** `ceil_log2(8)` 和 `log2(8)` 都返回 3，它们有何区别？
**答案：** `ceil_log2` 对任意正整数都能用，返回向上取整的对数；`log2` 要求入参必须是 2 的幂，否则触发 [math_pkg.vhd:216](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L216) 的断言失败。前者用于「算需要几位」，后者用于「确认并取对数」，语义不同。

**练习 2：** 为什么 `lt_0` 不直接写 `return value < 0`？
**答案：** 因为 Vivado 综合会把它展开成 20–30 个 LUT 的比较器；而补码负数的符号位就是最左位，读一位即可判断（[math_pkg.vhd:245](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L245)）。这把一个「贵」的比较变成了「免费」的位选，是面积优化的典型技巧（呼应 u1-l1 的面积优先哲学）。

---

### 4.2 saturate_signed：用保护位实现一键饱和

#### 4.2.1 概念说明

`saturate_signed` 解决的问题是「把一个较宽的有符号数安全收窄到较窄位宽」。设输入 `input_width` 位、输出 `result_width` 位（`result_width ≤ input_width`），则输出范围为：

\[
\text{min} = -2^{\text{result\_width}-1},\qquad \text{max} = 2^{\text{result\_width}-1}-1
\]

朴素做法是用比较器：`if value < min then min; elif value > max then max; else value`。但这要两个宽比较器，面积不小。本实体的洞察是：高位保护位 + 符号位已经包含了「是否越界」的全部信息，无需完整比较。

设保护位数为 `num_guard_bits = input_width - result_width`。把输入写成：

\[
\text{input\_value} = \underbrace{S}_{\text{符号位}}\ \underbrace{G\ G\ \cdots\ G}_{\text{num\_guard\_bits}}\ \underbrace{N\ N\ \cdots\ N}_{\text{result\_width}}
\]

由符号扩展性质：**值在范围内，当且仅当所有保护位都等于符号位**（即最高 `num_guard_bits+1` 位全 0 或全 1）。否则越界，按符号位钳到 min（负）或 max（正）。这只看最高几位、做一次「或/与」归约，逻辑极小。

#### 4.2.2 核心流程

1. 取出最高 `num_guard_bits + 1` 位（保护位 + 符号位），记为 `guard_and_sign`。
2. 判定是否在范围：若 `guard_and_sign` 全相同（全 0 或全 1），在范围。
3. 在范围：结果 = 输入的低 `result_width` 位（直接截取）。
4. 越界：结果 = 符号位为 1 → `100...0`（min）；符号位为 0 → `011...1`（max）；并拉高 `result_is_saturated`。
5. 可选 `enable_output_register`：在结果通路插一拍寄存器改善时序（呼应 u2-l1 的流水线思想）。

伪代码：

```python
guard_and_sign = input_value[-(num_guard_bits+1):]   # 最高 num_guard_bits+1 位
if all_0_or_all_1(guard_and_sign):
    result = input_value[0:result_width]             # 在范围，直接取低位
else:
    sign = guard_and_sign[0]                         # 符号位
    result = min_value if sign == 1 else max_value   # 钳位
```

#### 4.2.3 源码精读

**实体接口：位宽与可选寄存器都由 generic 控制。** 注意 `result_width` 的约束 `range 1 to input_width`，保证输出不会比输入宽。

[saturate_signed.vhd:65-81](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/saturate_signed.vhd#L65-L81) 声明 generic 与端口；`enable_output_register` 默认 `false`，呼应「能省一拍就省」的面积取向。

```vhdl
generic (
  input_width : positive;
  result_width : positive range 1 to input_width;
  enable_output_register : boolean := false
);
```

**核心判定：一次或/与归约。** 这是整个实体的灵魂。

[saturate_signed.vhd:93-108](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/saturate_signed.vhd#L93-L108) —— 先切出 `guard_and_sign`（最高 `num_guard_bits+1` 位），再用 VHDL-2008 的归约运算符 `(or guard_and_sign)` 与 `(and guard_and_sign)` 判断它们是否「全相同」。

```vhdl
guard_and_sign := input_value(
  input_value'high downto input_value'length - guard_and_sign'length
);

if (or guard_and_sign) = (and guard_and_sign) then
  result <= input_value(input_value'high - num_guard_bits downto 0);
  is_saturated <= '0';
else
  result <= (others => not guard_and_sign(guard_and_sign'high));
  result(result_value'high) <= guard_and_sign(guard_and_sign'high);
  is_saturated <= '1';
end if;
```

判据 `(or X) = (and X)` 的真值表只有三行：全 0（`or=0, and=0`，相等，正数在范围）、全 1（`or=1, and=1`，相等，负数在范围）、混合（`or=1, and=0`，不等，越界）。

越界时的钳位写法非常巧妙，一行覆盖两种极值：先 `result <= (others => not sign)`，再把最高位单独设成 `sign`。

- 符号位为 1（负溢出）：`not '1' = '0'` → 全 0，再置最高位 1 → `100...0` = min。
- 符号位为 0（正溢出）：`not '0' = '1'` → 全 1，再置最高位 0 → `011...1` = max。

> 小知识：`(or guard_and_sign)` 这种「一元归约」写法是 VHDL-2008 新增的（2008 之前要写 `or_reduce` 函数）。这也是项目要求 VHDL-2008 编译的另一个实例（见 u1-l2）。

**可选输出寄存器。** [saturate_signed.vhd:112-126](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/saturate_signed.vhd#L112-L126) 用 `generate` 在「打一拍」与「纯组合」之间切换，与 u2-l1 讲过的时序收敛思路一致：组合路径太长就花一拍寄存器换裕量。

**测试台：用 clamp 当对拍基准。** [tb_saturate_signed.vhd:119-133](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/test/tb_saturate_signed.vhd#L119-L133) 用 4.1 讲过的 `get_min/max_signed` 算出目标范围，再用 `math_pkg.clamp` 把同一个随机输入钳位，与 DUT 输出逐拍比对：

```vhdl
check_equal(result_value, clamp(value_in, min_result_value, max_result_value));
check_equal(result_is_saturated, value_in < min_result_value or value_in > max_result_value);
```

这就是「软件定义期望、硬件照着实现、随机对拍」的典型验证范式。`module_math.py` 里用 `self.add_vunit_config(test=tb, count=4)` 随机化 generic 跑 4 轮（见 [module_math.py:32-34](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/module_math.py#L32-L34)）。

#### 4.2.4 代码实践

**实践目标：** 实例化 `saturate_signed`，给定上下限（由 `result_width` 决定）与一组递增输入，验证输出被正确钳位、`result_is_saturated` 在越界时拉高。

**操作步骤：**

1. 新建一个最小测试台 `tb_saturate_play.vhd`（示例代码，非项目原有），实例化一个 8 位输入、4 位输出的饱和器。4 位有符号范围是 \([-8, 7]\)。喂入递增序列 `-12, -8, -1, 0, 7, 11`：

```vhdl
-- 示例代码：最小饱和验证
library ieee;
use ieee.numeric_std.all;
library math;
use math.math_pkg.all;
-- ...
dut : entity work.saturate_signed
  generic map (input_width => 8, result_width => 4, enable_output_register => false)
  port map (clk => clk, input_valid => input_valid, input_value => input_value,
            result_valid => result_valid, result_value => result_value,
            result_is_saturated => result_is_saturated);

-- process 内：依次给 -12,-8,-1,0,7,11
```

2. 对每个输入记录 `result_value` 与 `result_is_saturated`。

**需要观察的现象：** 越界输入（-12、11）的输出被钳到极值，且 `result_is_saturated = '1'`；在范围输入原样（截到 4 位）输出，`result_is_saturated = '0'`。

**预期结果：**

| 输入 | 期望 result_value | 期望 result_is_saturated |
| --- | --- | --- |
| -12 | -8（min） | 1 |
| -8 | -8（恰为 min，在范围） | 0 |
| -1 | -1 | 0 |
| 0 | 0 | 0 |
| 7 | 7（max，在范围） | 0 |
| 11 | 7（max） | 1 |

> 由于运行依赖本地工具链，具体命令与波形「待本地验证」。若暂无仿真器，可改为「源码阅读型实践」：把 `enable_output_register` 设为 `true`，阅读 [saturate_signed.vhd:112-118](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/saturate_signed.vhd#L112-L118)，解释 `result_valid` 相对 `input_valid` 延迟几拍，以及资源/时序的取舍。

#### 4.2.5 小练习与答案

**练习 1：** 若 `result_width = input_width`（没有保护位），`saturate_signed` 的行为是什么？
**答案：** 此时 `num_guard_bits = 0`，`guard_and_sign` 退化为单独的符号位，`(or X) = (and X)` 恒成立，于是永不饱和、`result = input`。即「不收窄就无需饱和」，符合直觉。

**练习 2：** 为什么作者强调「上游算术要有足够保护位」？保护位太少会怎样？
**答案：** 饱和的前提是「溢出还停在保护位里、没有被回卷」。若上游加法/乘法保护位不足，值可能已经在二进制补码里**回卷（wrap around）**——一个本该很大的正数会变成负数。此时 `saturate_signed` 看到的是回卷后的错误符号，会朝错误方向钳位。注释（[saturate_signed.vhd:53-57](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/saturate_signed.vhd#L53-L57)）特别提醒了这一点。

---

### 4.3 truncate_round_signed：去小数位并四舍五入

#### 4.3.1 概念说明

`truncate_round_signed` 解决的是「去掉低位小数位、把定点数收窄到更宽的有效位」并在此过程中**四舍五入**。设去掉 `num_lsb_to_remove = input_width - result_width` 位小数，相当于把数值除以 \(2^{\text{num\_lsb\_to\_remove}}\) 再取整。

关键在「如何取整」。被去掉的小数部分是一个 `num_lsb_to_remove` 位的无符号值，其中最高位（权重 0.5）决定是否「过半」。本实体支持两种模式（由 generic `convergent_rounding` 选）：

- **非收敛（`false`）**：过半（含正好 0.5）就向上入，等价于「四舍五入到正无穷」，`value_to_add` 取 0.5 那一位。
- **收敛（`true`，默认）**：正好 0.5 时凑到偶数（IEEE 754 默认），其余情况按 0.5 位入。

此外，若整数部分已是最大正数、又要向上入，加 1 会溢出。`enable_saturation` 可在这种情形把结果钳到最大值并给出 `result_overflow`。

#### 4.3.2 核心流程

1. 若 `input_width = result_width`：直通，不做任何事。
2. 否则把输入拆成「整数部分（高 result_width 位）」与「小数部分（低 num_lsb_to_remove 位）」。
3. 取两个关键位：`point_five_index`（小数部分最高位，权重 0.5）与 `one_index`（整数部分最低位，权重 1，即奇偶位）。
4. 决定 `value_to_add`（0 或 1）：
   - 收敛且小数恰为 0.5：`value_to_add = one_index`（奇则入、偶则舍，凑偶）。
   - 否则：`value_to_add = point_five`（按 0.5 位入）。
5. `result = integer_part + value_to_add`；若整数已满且加 1，则 `result_overflow = 1`。
6. 可选 `enable_saturation`：溢出时钳到最大值。

#### 4.3.3 源码精读

**generic 矩阵：四种可选特性。**

[truncate_round_signed.vhd:58-66](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd#L58-L66) 用五个 generic 控制「舍入模式 + 三级可选寄存器/饱和」，典型的「按需付费」设计。

```vhdl
generic (
  input_width : positive;
  result_width : positive range 1 to input_width;
  convergent_rounding : boolean := true;
  enable_addition_register : boolean := false;
  enable_saturation : boolean := false;
  enable_saturation_register : boolean := false
);
```

**直通捷径。** [truncate_round_signed.vhd:84-88](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd#L84-L88) 当两个位宽相等时直接旁路，零开销——呼应 u1-l1「generic 为假即零资源」。

**拆位与两个关键索引。**

[truncate_round_signed.vhd:103-124](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd#L103-L124) 算出 `num_lsb_to_remove`，定义 `one_index = num_lsb_to_remove`（整数最低位）与 `point_five_index = num_lsb_to_remove - 1`（小数最高位），并切出整数/小数两部分。

```vhdl
constant num_lsb_to_remove : positive := input_width - result_width;
constant one_index : natural := num_lsb_to_remove;
constant point_five_index : natural := num_lsb_to_remove - 1;
...
input_value_integer <= input_value(input_value'high downto num_lsb_to_remove);
input_value_fractional <= input_value(input_value_fractional'range);
one_index_value <= to_int(input_value(one_index));
point_five_index_value <= to_int(input_value(point_five_index));
```

这里的 `to_int` 与 `binary_integer_t` 正是 u2-l2 讲过的 `types_pkg` 工具——把某一位变成 0/1 整数。`one_index_value` 实际就是整数部分的奇偶标志。

**舍入判定：一段极简的「凑偶」逻辑。**

[truncate_round_signed.vhd:128-153](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd#L128-L153) 是本实体的核心。注释说这种写法资源最低：

```vhdl
if convergent_rounding then
  if input_value_fractional = input_value_fractional_point_five then
    value_to_add := one_index_value;          -- 恰 0.5：凑偶（奇入偶舍）
  else
    value_to_add := point_five_index_value;   -- 非 0.5：按 0.5 位入
  end if;
else
  value_to_add := point_five_index_value;     -- 非收敛：一律按 0.5 位入
end if;

result_int <= input_value_integer + value_to_add;
overflow_int <= to_sl(input_value_integer_is_max and value_to_add = 1);
```

收敛舍入的精妙之处：当小数**恰为 0.5**（`input_value_fractional_point_five` 是只有最高位为 1 的模式），向「偶」靠拢 = 给整数加上它的奇偶位 `one_index_value`——奇数（LSB=1）加 1 变偶、偶数（LSB=0）加 0 保持偶。一句话实现「四舍六入五凑偶」。`value_to_add` 是 `binary_integer_t`（仅 0/1），所以加法是一位增量。

**溢出与饱和。** [truncate_round_signed.vhd:182-214](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd#L182-L214) 在 `enable_saturation` 时把溢出结果替换为 `result_value_max`（由 `math_pkg.get_max_signed` 算出，见 4.1）。

```vhdl
result_int <= result_value_max when addition_overflow else addition_result;
```

**资源回归：把舍入模式量化成 LUT 数。** [module_math.py:110-154](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/module_math.py#L110-L154) 的 `get_build_projects` 为收敛/非收敛两种舍入各建一个 netlist 工程，并用 `EqualTo` 断言具体的 LUT/FF/逻辑级数：

```python
add(convergent_rounding=False, lut=6,  ff=52, logic=6)
add(convergent_rounding=True,  lut=7,  ff=52, logic=8)
```

这把「收敛舍入多 1 个 LUT、关键路径多 2 级逻辑」这一文档结论（包头注释 [truncate_round_signed.vhd:24-26](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd#L24-L26)）变成了 CI 里的硬性回归检查——一旦某次改动让资源数变了，构建立即失败（承接 u1-l4 的 netlist 回归思路）。

#### 4.3.4 代码实践

**实践目标：** 比较「收敛」与「非收敛」两种舍入在「正好 0.5」时的差异。

**操作步骤：**

1. 实例化两个 `truncate_round_signed`（示例代码，非项目原有），`input_width=6`、`result_width=4`（去掉 2 位小数，即除以 4）。4 位有符号整数范围 \([-8, 7]\)。
2. 喂入小数部分恰为 0.5 的值，例如定点值 `+1.5`（整数 1、小数 0.5）与 `+2.5`（整数 2、小数 0.5）。用 6 位定点（2 整数位 + 1 符号位扩展 + …；实际取 `input_value` 使低 2 位为 `01`，即 0.25×2=0.5）。
3. 分别在 `convergent_rounding=true` 与 `false` 下记录输出。

**需要观察的现象：** 非收敛模式下，所有 0.5 都向上入；收敛模式下，0.5 凑到最近的偶数。

**预期结果：** 对「整数部分 = 1（奇）、小数 = 0.5」：非收敛 → 2，收敛 → 2（1 是奇，入到偶 2）。对「整数部分 = 2（偶）、小数 = 0.5」：非收敛 → 3，收敛 → 2（2 已偶，舍）。两种模式在「.5 处」结果不同。

> 由于运行依赖本地工具链，具体命令与输出「待本地验证」。源码阅读型替代实践：阅读 [module_math.py:151-152](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/module_math.py#L151-L152)，解释为何收敛模式比非收敛多 1 个 LUT、逻辑级数多 2（提示：多了一次「小数是否恰为 0.5」的相等比较）。

#### 4.3.5 小练习与答案

**练习 1：** 非收敛舍入为何会引入「正偏差」？收敛舍入如何消除它？
**答案：** 非收敛模式下每个 0.5 都向 +∞ 入，长期累加会使结果系统性偏大。收敛模式让 0.5 一半向上、一半向下（按奇偶交替），正负抵消，统计无偏，所以 IEEE 754 选它作默认。

**练习 2：** `enable_saturation=false` 时，整数已满且向上入会发生什么？
**答案：** 加 1 会二进制回卷，最大正数变成最小负数（符号翻转），同时 `result_overflow` 拉高告警（[truncate_round_signed.vhd:152](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/truncate_round_signed.vhd#L152)）。调用方若没开饱和，就得自己处理这个溢出标志。

**练习 3：** 为什么 `get_build_projects` 里要把资源数写死成 `EqualTo` 断言？
**答案：** 这样资源占用就成了回归基线：任何人改动 `truncate_round_signed` 若引起 LUT/FF/逻辑级数变化，netlist 构建会立刻失败，迫使作者确认这是预期改变并更新基线。这是把「文档里说的资源数字」变成「可执行检查」的工程化手段。

---

### 4.4 unsigned_divider：位串行长除法

#### 4.4.1 概念说明

`unsigned_divider` 是一个**无符号、位串行、多周期**的除法器，实现：

\[
\text{dividend} \div \text{divisor} = \text{quotient} \quad \text{余} \quad \text{remainder}
\]

算法是小学竖式长除法的二进制版：把除数对齐到被除数的最高位，每拍右移一位、试减一次——够减则商位为 1 并从余数里扣除，不够减则商位为 0。每拍出一位商，从高到低，共 `dividend_width` 拍。包头注释（[unsigned_divider.vhd:9-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L9-L16)）点明「延迟随 `dividend_width` 线性增长」。

注意它的握手**不是** u2-l1 那种「每拍可流式吞吐」的 ready/valid 流水线握手，而是「单事务在途」：启动时采一次被除数/除数，运算 `dividend_width` 拍，结束后输出一次商/余数。期间 `input_ready='0'`，不能再收新事务。因此吞吐约为「每 `dividend_width+2` 拍一次除法」，适合低频使用。

#### 4.4.2 核心流程（状态机）

实体用一个三态有限状态机驱动（[unsigned_divider.vhd:50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L50)）：

```text
        input_valid & input_ready                current_bit 数到 0
 ready ───────────────────────► busy ─────────────────────► done
   ▲                                 每拍：试减、出 1 位商、除数右移 1 位     │
   └──────────────────────────────────────────────────────────────┘
                     result_valid & result_ready（输出被取走）
```

1. **ready**：`input_ready='1'` 等待。收到 `input_valid` 后，装入被除数到 `remainder_int`、把除数左移对齐到最高位装入 `divisor_int`、`current_bit = dividend_width-1`，进入 busy。
2. **busy**（运行 `dividend_width` 拍）：计算 `sub_result = remainder_int - divisor_int`；用 `lt_0` 判断是否为负（不够减）。
   - 够减：商位 = 1，`remainder_int -= divisor_int`。
   - 不够减：商位 = 0，余数不变。
   - 商寄存器每拍左移、追加新商位（MSB 优先）。`divisor_int` 每拍右移一位。`current_bit` 递减；到 0 时进入 done 并拉高 `result_valid`。
3. **done**：等 `result_ready` 握手取走结果后，回到 ready。

#### 4.4.3 源码精读

**实体接口：输入输出都是 ready/valid，但属「单事务」语义。**

[unsigned_divider.vhd:28-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L28-L46) —— 商的位宽与被除数相同，余数位宽取「除数/被除数位宽的较小值」。

```vhdl
quotient : out u_unsigned(dividend_width - 1 downto 0);
remainder : out u_unsigned(minimum(divisor_width, dividend_width) - 1 downto 0)
```

**移位辅助函数：约定「shift_down=右移、shift_up=左移」。** [unsigned_divider.vhd:57-70](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L57-L70) 自定义了带「补位」的移位。`shift_down(bit, v) = bit & v(high downto low+1)` 把值右移一位、高位补 `bit`；`shift_up(bit, v) = v(high-1 downto low) & bit` 左移一位、低位补 `bit`。

**主进程：先移除数、再用旧值试减。**

[unsigned_divider.vhd:76-117](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L76-L117) 是核心。注意两条语句的求值时序：

```vhdl
wait until rising_edge(clk);
divisor_int <= shift_down(divisor_int);                                    -- 用旧值算，结果下拍生效
sub_result := u_signed('0' & remainder_int) - u_signed('0' & divisor_int); -- 变量，用旧值立即算
```

`sub_result` 是变量，用的是**本拍起始**的旧 `remainder_int`/`divisor_int`；而 `divisor_int <= shift_down(...)` 是信号赋值，下一拍才生效。这就实现了「先用当前对齐的除数试减、下拍再把除数右移」的节奏。

**ready 分支：装入并左对齐除数。**

[unsigned_divider.vhd:86-93](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L86-L93) —— `divisor & to_unsigned(0, dividend_width-1)` 把除数放到高位、低位补零，使其 LSB 对齐到被除数的最高有效位，这正是长除法的起始对齐位置。

```vhdl
remainder_int <= dividend;
divisor_int <= divisor & to_unsigned(0, dividend_width - 1);
current_bit <= dividend_width - 1;
state <= busy;
```

**busy 分支：用 math_pkg.lt_0 判负、出商位。**

[unsigned_divider.vhd:95-108](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L95-L108) —— 这里用到了 4.1 讲过的 `lt_0`（[math_pkg.vhd:241-246](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L241-L246)）来判断「不够减」：

```vhdl
if lt_0(sub_result) then
  quotient <= shift_up('0', quotient);                 -- 不够减，商位 0
else
  quotient <= shift_up('1', quotient);                 -- 够减，商位 1
  remainder_int <= remainder_int - divisor_int(remainder_int'range);  -- 恢复除法：扣除
end if;
```

这是「恢复式（restoring）」长除法：够减才扣、不够则保留原余数。商从高到低逐位左移追加。

**除以零的行为：** 当 `divisor=0`，`divisor_int` 恒为 0，`sub_result = remainder - 0 ≥ 0` 恒成立，于是每个商位都是 1，结果商为全 1（最大值），余数未定义。测试台 [tb_unsigned_divider.vhd:89-95](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/test/tb_unsigned_divider.vhd#L89-L95) 正好断言这一点。

**测试台：穷举小位宽对拍。** [tb_unsigned_divider.vhd:72-87](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/test/tb_unsigned_divider.vhd#L72-L87) 对小位宽（4/7/8）穷举所有被除数 × 除数组合，用 VHDL 内置的 `dividend_tb / divisor_tb` 与 `dividend_tb rem divisor_tb` 当标准答案：

```vhdl
check_equal(quotient,  dividend_tb / divisor_tb, ...);
check_equal(remainder, dividend_tb rem divisor_tb, ...);
```

而 `module_math.py` 的 `_setup_unsigned_divider_tests`（[module_math.py:100-108](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/module_math.py#L100-L108)）用 `dividend_width`/`divisor_width` 各取 4/7/8 的 9 种组合登记配置，把位宽参数化的正确性纳入回归。

#### 4.4.4 代码实践

**实践目标：** 实例化 `unsigned_divider`，完成一次多周期除法，核对商与余数；并体会「单事务在途」的握手节奏。

**操作步骤：**

1. 实例化一个 `dividend_width=8`、`divisor_width=8` 的除法器（示例代码，非项目原有）。计算 `200 / 7`（期望商 25、余数 5）。
2. 在测试台里：拉高 `input_valid`、给出 `dividend=200`、`divisor=7`，等 `input_ready & input_valid` 完成采样；再拉高 `result_ready`，等 `result_valid & result_ready` 取走结果。
3. 期间用一个计数器记录从采样到出结果经历的时钟周期数。

```vhdl
-- 示例代码：最小除法验证
dut : entity work.unsigned_divider
  generic map (dividend_width => 8, divisor_width => 8)
  port map (clk => clk, input_ready => input_ready, input_valid => input_valid,
            dividend => dividend, divisor => divisor,
            result_ready => result_ready, result_valid => result_valid,
            quotient => quotient, remainder => remainder);
```

**需要观察的现象：** 采样后 `input_ready` 立即拉低，运算约 8 拍后 `result_valid` 拉高；`quotient` 与 `remainder` 稳定出现。

**预期结果：** `quotient = 25`、`remainder = 5`；从采样到 `result_valid` 约经历 `dividend_width + 1` ≈ 9 个时钟周期。

> 由于运行依赖本地工具链，具体命令与波形「待本地验证」。若暂无仿真器，可改为「源码阅读型实践」：跟踪 [unsigned_divider.vhd:86-116](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/unsigned_divider.vhd#L86-L116) 的状态机，画出 `dividend=15, divisor=1, dividend_width=4` 时每一拍的 `remainder_int`、`divisor_int`、`quotient`、`current_bit`，验证最终 `quotient=15, remainder=0`。

#### 4.4.5 小练习与答案

**练习 1：** 为什么说这个除法器「吞吐低」？它的吞吐大约是多少？
**答案：** 它是「单事务在途」：一次除法占用 `dividend_width` 拍运算 + ready/done 各一拍握手，期间不能接收新输入。吞吐约为每 `dividend_width+2` 拍一次除法，与「每拍一个结果」的流水线除法器相比低很多。所以它适合低频配置类除法，不适合数据流式除法。

**练习 2：** 主进程里 `divisor_int <= shift_down(divisor_int)` 和 `sub_result := ... - ... divisor_int` 都用到 `divisor_int`，它们用的是同一个值吗？
**答案：** 不是。`sub_result` 是**变量**，用的是本拍起始的旧 `divisor_int`；`divisor_int <= shift_down(...)` 是**信号**赋值，下拍才生效。所以本拍用「当前对齐位置」试减，下一拍除数才右移到新位置——这正是长除法「逐位试商」的节奏。

**练习 3：** 除数是动态变量（运行时才知），为何这里不用查找表或 IP 核？
**答案：** 查找表法只适合除数固定或范围极小；Xilinx 除法 IP 核面积大且不可移植。位串行长除法用纯 RTL 实现、面积小、跨厂商可综合，符合本项目「可复用、可移植、面积优先」的取向（u1-l1）。代价就是吞吐低，由调用方按需权衡。

---

## 5. 综合实践

把本讲内容串起来：用 `math_pkg` 算常量，再用 `saturate_signed` 与 `unsigned_divider` 搭一个「定点收窄 + 概率统计」的小数据通路（示例代码，非项目原有）。

**场景：** 一个 12 位有符号传感器采样值要收窄到 8 位存储；同时需要把「样本总和」除以「样本个数」算平均（用整数除法）。

任务要求：

1. 用 `math_pkg.num_bits_needed` 确认 8 位结果范围，用 `get_min/max_signed_integer(8)` 算出饱和上下限（应为 \([-128, 127]\)），在测试台里 `report` 出来。
2. 实例化 `saturate_signed`（`input_width=12, result_width=8`），喂入几个超出 \([-128,127]\) 的 12 位值（如 +300、-300），确认输出被钳到 127/-128 且 `result_is_saturated='1'`。
3. 把若干次采样的绝对值累加（用 `math_pkg.abs_vector` 与 `vector_sum` 在软件侧算期望值），再用 `unsigned_divider` 把累加和除以样本数，核对商。
4. 用 `module_math.py` 的模式（[module_math.py:23-30](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/module_math.py#L23-L30)）为这个数据通路写一个最小测试台并在 `setup_vunit` 里登记。

```vhdl
-- 示例代码：综合实践骨架（省略时钟与握手进程）
library ieee;
use ieee.numeric_std.all;
library math;
use math.math_pkg.all;

architecture a of sample_path is
  constant result_min : integer := get_min_signed_integer(8);  -- -128
  constant result_max : integer := get_max_signed_integer(8);  --  127
begin
  sat : entity work.saturate_signed
    generic map (input_width => 12, result_width => 8)
    port map (clk => clk, input_valid => valid_in, input_value => sample_in,
              result_valid => valid_out, result_value => sample_out,
              result_is_saturated => saturated);

  div : entity work.unsigned_divider
    generic map (dividend_width => 16, divisor_width => 8)
    port map (clk => clk, input_valid => div_in_v, input_ready => div_in_r,
              dividend => accu, divisor => count,
              result_valid => div_out_v, result_ready => div_out_r,
              quotient => mean, remainder => open);
end architecture;
```

完成后，对照本讲逐一标注：哪些常量用了 `math_pkg`、哪段逻辑用了 `saturate_signed`、哪段用了 `unsigned_divider`，并解释为什么除法器放在「低频统计」路径而不是数据主路径。

> 综合与仿真结果「待本地验证」。

## 6. 本讲小结

- `math_pkg` 是精化期纯函数工具箱：极值（`get_min/max_signed`）、钳位（`clamp`）、对数（`ceil_log2`）、位宽（`num_bits_needed`）、符号判断（`lt_0`）、整除取整（`div_round_negative`）等，函数求值后折叠进常量，不占电路资源。
- `lt_0` 用「读符号位」代替 `value < 0`，避开 Vivado 的 20–30 LUT 比较，是面积优化的典范，并被 `unsigned_divider` 复用。
- `saturate_signed` 利用「保护位 + 符号位全相等 ⇔ 在范围」的性质，仅用一次或/与归约就完成有符号饱和；测试台用 `clamp` 对拍。
- `truncate_round_signed` 去小数位并四舍五入：收敛舍入用「整数奇偶位」一行实现凑偶，资源数被 netlist 构建回归钉死（收敛多 1 LUT、多 2 级逻辑）。
- `unsigned_divider` 是位串行恢复式长除法，三态状态机、`dividend_width` 拍出结果，单事务在途、吞吐低但面积小、可移植。
- 三个实体都体现项目一贯风格：generic 裁剪可选特性、`module_math.py` 用 `add_vunit_config`/`EqualTo` 把功能与资源纳入 CI 回归（承接 u1-l4）。

## 7. 下一步学习建议

- 接下来进入 **u3-l1（resync 基础）**：跨时钟域会用到大位宽指针的格雷码同步，正好复用本讲 `math_pkg` 的 `to_gray`/`from_gray`/`hamming_distance`（[math_pkg.vhd:331-356](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L331-L356)），是很好的承接。
- 想看 `saturate`/`truncate_round` 在真实信号链里的用法，可留意后续 **u7-l1（正弦发生器）**：定点 NCO 的相位/幅度路径会用到本讲的饱和与舍入来控制位宽与 SFDR。
- 想深入「资源回归」机制，可先读 [module_math.py:110-154](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/module_math.py#L110-L154)，详细讲解在 **u8-l3（资源占用回归）**。
