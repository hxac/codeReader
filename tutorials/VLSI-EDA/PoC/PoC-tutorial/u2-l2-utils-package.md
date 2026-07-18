# utils 包：通用类型与辅助函数

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `src/common/utils.vhdl` 在 PoC 全库中的定位——它是被最先编译的公共包，几乎所有核都直接或间接依赖它。
- 读懂并使用 PoC 自定义的数组类型（`T_INTVEC` 等）与整数子类型（`T_INT_8` 等）。
- 理解 PoC 为「配置选项」定义的一批枚举类型（`T_POLARITY`、`T_BIT_ORDER`、`T_CLOCK_EDGE` 等），以及为它们重载的运算符。
- 掌握 `ite`、`imin`、`imax`、`log2ceil` 等高频辅助函数，并能解释 `SIMULATION` 这个「延迟常量（deferred constant）」是如何区分仿真与综合的。

本讲承接 [u2-l1 公共包总览与 Common 上下文](u2-l1-common-packages-overview.md)：那一讲给出了公共包清单与编译顺序，本讲深入其中第一个、也是最基础的一个——`utils`。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **VHDL 的 package 机制**：`package ... is` 声明对外可见的类型/常量/函数，`package body ... is` 放具体实现。本讲会反复在这两层之间跳转。
- **无约束数组（unconstrained array）**：写法是 `type T is array(natural range <>) of 元素类型;`，`<>` 表示「下标范围待定」，由使用者在使用时确定。这是 VHDL 里 `std_logic_vector` 的底层机制。
- **子类型（subtype）**：`subtype T is 基类型 range 约束;` 并不创建新类型，只是给已有类型加一个范围约束，相互之间可以直接赋值。
- **延迟常量（deferred constant）**：在包声明里写 `constant X : 类型;`（不给出值），在包体里再 `constant X : 类型 := 表达式;` 赋值。这样常量的值可以依赖包体里的函数计算，而使用者只需 `use` 包声明即可。
- **PoC 的编译模型**：所有 VHDL 都编译进名为 `PoC` 的库，公共包必须先于使用它的核编译（见 u2-l1）。

如果你对「为什么需要 `log2ceil`」这种问题感到陌生，不用急——本讲会从一个真实例子（计算 FIFO 地址位宽）讲起。

## 3. 本讲源码地图

本讲只围绕一个文件展开，但它内容很多，建议先建立整体印象：

| 文件 | 作用 |
| --- | --- |
| [src/common/utils.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl) | PoC 的通用类型与辅助函数集合，是全库的地基。第 40–312 行是包声明，第 315–1335 行是包体。 |
| [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files) | pyIPCMI 的编译清单。第 11 行把 `utils.vhdl` 列为「核心包」的第一条，说明它最早被编译进 `PoC` 库。 |
| [src/fifo/fifo_cc_got.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl) | 一个真实的 FIFO 核。第 129 行用 `log2ceil(MIN_DEPTH)` 计算地址位宽，是本讲「为什么要这些函数」的活样本。 |

> 提示：本讲没有单独的 `utils_tb.vhdl`（仓库里 `tb/common/` 下只有一个 `utils.py`，属于 pyIPCMI 的脚本，不是 VHDL 测试台）。因此本讲的「代码实践」以**自己动手写一个最小测试台**为主，并明确标注为「示例代码」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 数组与子类型**——PoC 自定义的向量类型与整数子范围。
- **4.2 枚举类型**——给配置选项赋予有名字的取值，并重载运算符。
- **4.3 辅助函数**——`log2ceil` / `ite` / `imin` / `imax` 等高频函数，以及 `SIMULATION` 延迟常量。

### 4.1 数组与子类型

#### 4.1.1 概念说明

标准 VHDL 只内置了少数位数组（`bit_vector`、`std_logic_vector`、`unsigned`、`signed`），却没有「整数数组」「布尔数组」这类常用容器。于是在写测试台或推导常量时，我们常常想要一个 `integer` 的数组却没有现成类型可用。

PoC 在 `utils` 包里补齐了这些缺口，定义了一批「基本类型的向量」。此外，VHDL 的 `integer` 默认是 32 位，但在仿真里对变量使用更窄的子范围（如 8 位）有时能加速仿真、也便于阅读；PoC 因此定义了几个整数子类型。

需要强调：这些数组类型在**综合后的核里很少直接成为端口**（端口仍以 `std_logic_vector` 为主），它们主要服务于**常量推导、测试台与配置计算**——这正是 `utils` 作为「地基包」的典型用法。

#### 4.1.2 核心流程

- 定义向量类型用无约束数组：`type T_XVEC is array(natural range <>) of 元素类型;`。下标类型选 `natural`，范围由使用者在实例化时给定。
- 定义整数子类型用 `subtype`：给 `integer` 套一个 `range` 约束，既保留 `integer` 的所有运算，又限制了取值区间。

#### 4.1.3 源码精读

五个向量类型集中在一起声明，元素分别是布尔、整数、自然数、正整数、实数：

[src/common/utils.vhdl:50-55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L50-L55) —— 定义 `T_BOOLVEC`/`T_INTVEC`/`T_NATVEC`/`T_POSVEC`/`T_REALVEC` 五个无约束数组。注意它们都用 `natural range <>` 作下标，因此长度可变。

整数子类型紧随其后，注释里点明了用途——「有时能加速仿真」：

[src/common/utils.vhdl:57-61](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L57-L61) —— `T_INT_8`(-128..127)、`T_INT_16`(-32768..32767)、`T_UINT_8`(0..255)、`T_UINT_16`(0..65535) 四个子类型。它们是 `integer` 的子类型，不是新类型，与 `integer` 之间可直接互相赋值。

#### 4.1.4 代码实践

> **实践目标**：亲手声明一个 `T_INTVEC` 常量，并用聚合函数 `imin`/`imax`/`isum`（4.3 会讲）读取它，体会「数组类型 + 向量函数」的组合用法。

下面的测试台是**示例代码**（仓库里没有对应的 `utils_tb.vhdl`）：

```vhdl
-- 示例代码：演示 T_INTVEC 与向量聚合函数的最小测试台
library IEEE;
use     IEEE.std_logic_1164.all;
use     work.utils.all;        -- 引入 PoC.utils（假设已编译进 work 库）

entity utils_vec_tb is end entity;
architecture tb of utils_vec_tb is
  constant SAMPLE : T_INTVEC := (7, 2, 30, -5, 12);   -- 用 T_INTVEC 装一组整数
begin
  process
  begin
    report "min   = " & integer'image(imin(SAMPLE));   -- 期望 -5
    report "max   = " & integer'image(imax(SAMPLE));   -- 期望 30
    report "sum   = " & integer'image(isum(SAMPLE));   -- 期望 46
    wait;
  end process;
end architecture;
```

**需要观察的现象**：仿真器打印三条 `report`，数值分别为 -5、30、46。
**预期结果**：`imin`/`imax`/`isum` 对向量分别取最小、最大、求和（精确实现见 4.3.3）。`report` 的确切文本格式随仿真器而异，**待本地验证**具体输出字符串，但数值是确定的。

#### 4.1.5 小练习与答案

**练习 1**：`subtype T_UINT_8 is integer range 0 to 255;` 定义之后，把一个 `T_UINT_8` 变量赋给一个 `integer` 变量需要做类型转换吗？

> **答案**：不需要。`subtype` 不创建新类型，`T_UINT_8` 本质上就是带范围约束的 `integer`，二者属于同一类型，可直接赋值（赋值时仍会做范围检查）。

**练习 2**：为什么这些向量类型在核的端口（`port`）里很少见，却常出现在测试台和常量推导里？

> **答案**：端口要面向综合与互连，统一用 `std_logic_vector` 这类可综合位数组更稳妥；而向量类型主要用于「在仿真/配置阶段处理一组数」，例如遍历一组 generic、计算一组常量，这些场景不进入最终硬件。

### 4.2 枚举类型

#### 4.2.1 概念说明

硬件设计里充满「二选一/多选一」的配置：复位是高有效还是低有效？时钟取上升沿还是下降沿？字节序是大端还是小端？如果用 `boolean` 或 `integer` 来表达，代码里就会出现大量 `if POLARITY = 1` 这种「魔法值」，可读性很差。

PoC 的做法是为每一类选项定义一个**有名字取值的枚举类型**，例如 `T_POLARITY is (HIGH_ACTIVE, LOW_ACTIVE)`。这样配置写出来是 `LOW_ACTIVE`，一目了然。更进一步，PoC 还为部分枚举**重载了运算符**：例如给 `T_POLARITY` 重载了 `"xor"`，于是可以直接写 `MYPOL xor sig`，让信号按指定极性自动取反——这是枚举类型「不只是常量，还能参与运算」的关键设计。

#### 4.2.2 核心流程

- 声明枚举：`type T is (值1, 值2, ...);`，VHDL 会自动给每个值一个从 0 开始的位置编号（`'pos`）。
- 翻转两值枚举：用属性 `'val(('pos(x) + 1) mod 2)`——把当前位置 +1 再对 2 取模，正好在两个值之间来回切。
- 把枚举变成运算：重载 `"xor"` 等运算符，让枚举值参与表达式。

#### 4.2.3 源码精读

枚举类型集中在包声明里，每个都对应一类配置选项：

[src/common/utils.vhdl:63-108](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L63-L108) —— 一批枚举与 BCD 类型。要点：
- `T_IPSTYLE`（行 65）：IP 类型，`IPSTYLE_HARD`/`IPSTYLE_SOFT` 区分硬核/软核。
- `T_BIT_ORDER`（行 68）：位序 `LSB_FIRST`/`MSB_FIRST`。
- `T_BYTE_ORDER`（行 72）：字节序 `LITTLE_ENDIAN`/`BIG_ENDIAN`。
- `T_POLARITY`（行 76）：有效电平 `HIGH_ACTIVE`/`LOW_ACTIVE`，其后紧跟一长串重载的 `"xor"`/`"xnor"` 声明。
- `T_CLOCK_EDGE`（行 96）：有效时钟沿 `RISING_EDGE`/`FALLING_EDGE`。
- `T_ROUNDING_STYLE`（行 100）：舍入方式，供 `scale` 函数使用。
- `T_BCD`（行 105）：4 位 `std_logic` 数组，用于 BCD（二进码十进制）运算；`C_BCD_MINUS`/`C_BCD_OFF`（行 107–108）是两个特殊码字。

下面是「两值枚举翻转」的实现，`"not"` 把 `HIGH_ACTIVE` 变 `LOW_ACTIVE`、反之亦然：

[src/common/utils.vhdl:351-354](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L351-L354) —— 用 `'val(('pos(left)+1) mod 2)` 在两个极性之间翻转。`T_BIT_ORDER`、`T_BYTE_ORDER`、`T_CLOCK_EDGE` 的 `"not"` 用的是同一套写法（行 339–342、345–348、469–472）。

重载后的 `"xor"` 让「按极性取反」变成一行表达式：

[src/common/utils.vhdl:374-381](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L374-L381) —— `T_POLARITY xor std_logic`：高有效时原样返回，低有效时返回 `not right`。于是写 `LOW_ACTIVE xor reset_in` 就自动得到取反后的复位。

#### 4.2.4 代码实践

> **实践目标**：用 `T_POLARITY` 和重载的 `"xor"`，写出一个「按可配置极性输出复位」的表达式，体会枚举参与运算的便利。

```vhdl
-- 示例代码：极性可配置的复位输出
library IEEE;
use     IEEE.std_logic_1164.all;
use     work.utils.all;

entity polarized_reset_demo is
  generic (RST_POL : T_POLARITY := HIGH_ACTIVE);
  port    (rst_in  : in  std_logic;
           rst_out : out std_logic);
end entity;

architecture rtl of polarized_reset_demo is
begin
  -- 用重载的 xor：HIGH_ACTIVE 时 rst_out = rst_in；LOW_ACTIVE 时自动取反
  rst_out <= RST_POL xor rst_in;
end architecture;
```

**需要观察的现象**：把 `RST_POL` 分别设为 `HIGH_ACTIVE` 与 `LOW_ACTIVE`，给定 `rst_in='1'`，观察 `rst_out`。
**预期结果**：`HIGH_ACTIVE` 时 `rst_out='1'`；`LOW_ACTIVE` 时 `rst_out='0'`。若不便综合上板，可在测试台里驱动 `rst_in` 用 `report` 打印 `rst_out` 验证，**待本地验证**仿真器输出。

#### 4.2.5 小练习与答案

**练习 1**：`"not"` 用 `'val(('pos(left)+1) mod 2)` 实现。如果某个枚举有 3 个值，这套写法还正确吗？

> **答案**：不正确。`mod 2` 只适用于恰好两个值的枚举；3 个值时 `mod 2` 会在前两个值之间打转，到不了第三个。三值枚举的「翻转」需另行定义语义。

**练习 2**：为什么 PoC 选择定义 `T_POLARITY` 枚举并重载 `xor`，而不是约定「`0` 表示高有效、`1` 表示低有效」？

> **答案**：枚举字面量 `HIGH_ACTIVE`/`LOW_ACTIVE` 自解释，避免魔法数字；重载 `xor` 后还能直接写进表达式，既可读又不容易写错极性。

### 4.3 辅助函数

#### 4.3.1 概念说明

`utils` 包里数量最多、使用最频繁的就是各类辅助函数。按用途分三组：

- **数学类**：`div_ceil`（向上取整除法）、`is_pow2`/`ceil_pow2`/`floor_pow2`（2 的幂判断与舍入）、`log2ceil`/`log2ceilnz`/`log10ceil`/`log10ceilnz`（对数，向上取整）。其中 `log2ceil` 是全库出现频率最高的一个——「给定一个深度，求需要多少位地址」几乎处处都要它。
- **条件类**：`ite`（if-then-else，三元函数，被大量重载）、`inc_if`/`dec_if`（条件自增/自减）。VHDL 没有 C 的 `?:` 三元运算符，`when/else` 又只能用在并发赋值里，`ite` 填补了「在表达式和常量计算里嵌入条件」的空白。
- **聚合与转换类**：`imin`/`imax`（整数最小最大，可作用于两数或向量）、`isum`（求和）、`to_int`/`to_sl`/`to_slv`（类型转换）。其中 `rmin`/`rmax` 直接 `alias` 到 `IEEE.math_real` 的 `realmin`/`realmax`，避免重复造轮子。

另外还有一个特殊常量 `SIMULATION`：它是**延迟常量**，在仿真里为 `true`、综合里为 `false`，用来把「只在仿真阶段有意义」的检查（如越界保护、告警）包起来，避免它们污染综合出的硬件。

#### 4.3.2 核心流程

**`log2ceil` 的算法**（理解它就能理解 `log10ceil`）。目标是计算：

\[\text{log2ceil}(n) = \lceil \log_2 n \rceil,\quad n \ge 1\]

它不用浮点 `log2`，而是从 `tmp=1, log=0` 开始，反复 `tmp *= 2; log += 1`，直到 `tmp` 首次「够大」（`arg <= tmp`）。此时的 `log` 就是答案。以 `arg=30` 为例：

```
初始: tmp=1,  log=0
30 > 1  → tmp=2,  log=1
30 > 2  → tmp=4,  log=2
30 > 4  → tmp=8,  log=3
30 > 8  → tmp=16, log=4
30 > 16 → tmp=32, log=5
30 > 32 ? 否，停止 → 返回 5
```

于是 \(\lceil \log_2 30 \rceil = \lceil 4.907 \rceil = 5\)，与手算一致。`arg=1` 是特例，直接返回 0（因为 \(\log_2 1 = 0\)）。

**`ite` 的语义**：`ite(cond, value1, value2)` 就是「`cond` 为真返回 `value1`，否则返回 `value2`」，等价于一个三元运算符，对 `boolean`/`integer`/`std_logic_vector` 等十种类型都做了重载。

**`imin`/`imax` 的向量版**：从类型上界（`integer'high`）或下界（`integer'low`）起步，遍历向量逐个比较。

**`SIMULATION` 的判定**：靠综合 pragmas 配合 `Is_X`。`Is_X('X')` 在仿真里为真、综合时本应被优化为假，但某些 Xilinx 工具会误判，于是用 `--synthesis translate_off/translate_on` 把这段代码在综合时整段剔除，确保综合结果恒为假。

#### 4.3.3 源码精读

先看 `SIMULATION`：包声明里是延迟常量（只声明类型，不给值）：

[src/common/utils.vhdl:42-45](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L42-L45) —— `SIMULATION : boolean` 是延迟常量声明，注释写明「区分仿真与综合」。

它的值在包体里由 `is_simulation` 计算得出：

[src/common/utils.vhdl:319-334](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L319-L334) —— `is_simulation` 用 `--synthesis translate_off`（行 327）把 `if Is_X('X')...` 包起来，综合时整段被剔除、返回 `false`；仿真时 `Is_X('X')` 为真、返回 `true`。第 334 行把结果赋给延迟常量 `SIMULATION`。

`SIMULATION` 的典型用法是「只在仿真时做保护性检查」。例如 `to_index` 只在仿真里用 `imin` 把下标夹到合法范围：

[src/common/utils.vhdl:970-980](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L970-L980) —— `if SIMULATION and max > 0 then res := imin(res, max);`。综合时这段被常量折叠掉，不产生额外硬件；仿真时则能防止越界。

接下来是本讲的主角 `log2ceil`：

[src/common/utils.vhdl:513-525](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L513-L525) —— `log2ceil` 的实现：`arg=1` 提前返回 0；否则用 `while arg > tmp` 的倍增循环求 \(\lceil\log_2 n\rceil\)。

与它配套的还有「保证至少为 1」的 `log2ceilnz`、以及十进制的 `log10ceil`/`log10ceilnz`：

[src/common/utils.vhdl:527-552](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L527-L552) —— `log2ceilnz = imax(1, log2ceil(arg))`（行 530），避免地址位宽被算成 0；`log10ceil` 把倍增基数换成 10（行 541）。

`ite` 以最简单的 `boolean` 重载为例（其余九个重载结构完全一样）：

[src/common/utils.vhdl:556-563](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L556-L563) —— `ite` 的本质就是一个 `if cond then value1 else value2`，靠重载覆盖多种返回类型。

`imin`/`imax` 的标量版只有两三行：

[src/common/utils.vhdl:670-674](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L670-L674) —— `imin(arg1, arg2)`：谁小返回谁。

向量版则需要遍历：

[src/common/utils.vhdl:682-692](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L682-L692) —— `imin(vec : T_INTVEC)`：初值取 `integer'high`，逐个比较取更小者。

最后，看一个**真实的全库调用**——这正是 `log2ceil` 存在的意义。在 `fifo_cc_got` 里，给定 FIFO 深度 `MIN_DEPTH`，一行就能算出地址位宽：

[src/fifo/fifo_cc_got.vhdl:127-136](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L127-L136) —— `constant A_BITS : natural := log2ceil(MIN_DEPTH);`（行 129），随后 `A_BITS` 被用作读/写指针的位宽（行 135–136）。当 `MIN_DEPTH=30` 时，`A_BITS=5`，指针是 5 位（可表示 0..31，足以覆盖 30 个表项）。

#### 4.3.4 代码实践

> **实践目标**：写一个最小测试台，验证 `log2ceil` 把「深度 30」映射为「地址位宽 5」，并对照真实核 `fifo_cc_got` 的用法。

下面的测试台是**示例代码**（仓库无对应 `utils_tb.vhdl`）：

```vhdl
-- 示例代码：验证 log2ceil 的深度→位宽映射
library IEEE;
use     IEEE.std_logic_1164.all;
use     work.utils.all;

entity utils_log2ceil_tb is end entity;
architecture tb of utils_log2ceil_tb is
  constant FIFO_DEPTH : positive := 30;
  -- 仿照 fifo_cc_got.vhdl:129 的写法
  constant A_BITS     : natural  := log2ceil(FIFO_DEPTH);
begin
  process
  begin
    report "FIFO_DEPTH = " & integer'image(FIFO_DEPTH);
    report "A_BITS     = " & integer'image(A_BITS);   -- 期望 5
    -- 顺便验证几个边界：
    report "log2ceil(1)  = " & integer'image(log2ceil(1));   -- 期望 0
    report "log2ceil(16) = " & integer'image(log2ceil(16));  -- 期望 4
    report "log2ceil(32) = " & integer'image(log2ceil(32));  -- 期望 5
    report "log2ceilnz(1)= " & integer'image(log2ceilnz(1)); -- 期望 1
    wait;
  end process;
end architecture;
```

**操作步骤**：

1. 确认 `utils.vhdl` 已经编译进 `work`（或 `PoC`）库——可参考 `common.files` 第 11 行的编译顺序。
2. 把上面这段测试台存为 `utils_log2ceil_tb.vhdl`，编译并仿真。
3. 修改 `FIFO_DEPTH` 为 16、32、64，重新观察 `A_BITS`。

**需要观察的现象**：`report` 打印的 `A_BITS` 随 `FIFO_DEPTH` 变化。
**预期结果**：

| FIFO_DEPTH | log2ceil（A_BITS） | 说明 |
| --- | --- | --- |
| 1 | 0 | 特例，\(\log_2 1=0\) |
| 16 | 4 | 恰好 2 的幂，\(2^4=16\) |
| 30 | 5 | \(\lceil\log_2 30\rceil=5\)，\(2^4=16<30\le 32=2^5\) |
| 32 | 5 | 恰好 \(2^5\) |
| 64 | 6 | 恰好 \(2^6\) |

`report` 文本格式随仿真器而异，**待本地验证**确切字符串；但上述数值是确定的，可作为断言依据。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fifo_cc_got` 用 `log2ceil(MIN_DEPTH)` 而不是 `log2ceilnz`？当 `MIN_DEPTH=1` 时会出现什么？

> **答案**：`log2ceil(1)=0`，意味着 `A_BITS=0`、指针是 0 位（退化为常量），对应「深度为 1 的 FIFO」只需一个表项、不需要地址指针。如果改用 `log2ceilnz`，会强制得到 1，反而多出无谓的 1 位指针。所以选哪个取决于语义：地址位宽允许为 0 时用 `log2ceil`，需要「至少 1 位」时才用 `log2ceilnz`。

**练习 2**：`SIMULATION` 为什么用「延迟常量 + 包体内函数计算」，而不是直接写 `constant SIMULATION : boolean := true;` 让用户自己改？

> **答案**：延迟常量让仿真和综合共用**同一份源码**——`is_simulation` 配合 `--synthesis translate_off` 自动在两种环境下得到不同值，无需用户手改，也避免「忘记改回 false 导致综合出错」。这正是 u2-l1 提到的「跨版本/跨环境可移植」思想在单个常量上的体现。

**练习 3**：`rmin`/`rmax` 为什么用 `alias` 指向 `IEEE.math_real`，而不是自己写实现？

> **答案**：标准库已经提供了正确、经过验证的 `realmin`/`realmax`，用 `alias` 改个名字复用，既减少重复代码，也降低出错概率——这是 `utils` 作为「薄封装层」的典型风格。

## 5. 综合实践

把本讲的三个模块串起来，完成一个小任务：**模拟「根据 FIFO 深度推导地址位宽与寄存器宽度，并按复位极性输出初始化值」的配置计算**。

要求写一个测试台（**示例代码**），综合使用：

- `T_INTVEC` 与 `imin`/`imax`/`isum`（4.1、4.3）：处理一组候选深度。
- `log2ceil`（4.3）：把每个深度映射成地址位宽。
- `ite` 与 `T_POLARITY`（4.2、4.3）：按极性选择复位初值。

```vhdl
-- 示例代码：综合实践
library IEEE;
use     IEEE.std_logic_1164.all;
use     work.utils.all;

entity utils_capstone_tb is end entity;
architecture tb of utils_capstone_tb is
  constant DEPTHS    : T_POSVEC := (8, 30, 64, 100);  -- 一组候选 FIFO 深度
  constant RST_POL   : T_POLARITY := LOW_ACTIVE;      -- 复位低有效
  constant RST_VALUE : std_logic := '1';              -- 想要的「有效」电平
begin
  process
    variable depth, bits : integer;
  begin
    -- 1) 聚合：这一组深度的最小/最大/求和
    report "min depth = " & integer'image(imin(DEPTHS));   -- 期望 8
    report "max depth = " & integer'image(imax(DEPTHS));   -- 期望 100

    -- 2) 逐个把深度映射为地址位宽
    for i in DEPTHS'range loop
      depth := DEPTHS(i);
      bits  := log2ceil(depth);                            -- 例如 30 -> 5
      report "depth " & integer'image(depth)
           & " -> A_BITS " & integer'image(bits);
    end loop;

    -- 3) 按极性算出真正的复位电平（LOW_ACTIVE 时把 RST_VALUE 取反）
    report "rst_out = " & std_logic'image(RST_POL xor RST_VALUE);
    wait;
  end process;
end architecture;
```

**自检要点**：

1. `imin`/`imax` 对 `T_POSVEC` 的返回应当是 8 和 100。
2. 深度 30 应映射为位宽 5、深度 100 应映射为位宽 7（\(\lceil\log_2 100\rceil=7\)，因为 \(2^6=64<100\le128=2^7\)）。
3. `RST_POL xor RST_VALUE`：`LOW_ACTIVE xor '1'` 应得 `'0'`。

把这些断言改写成 `assert ... report ... severity failure;`，你就有了一个真正能回归测试 `utils` 关键函数的最小测试台。具体输出文本**待本地验证**，但断言条件是确定的。

## 6. 本讲小结

- `utils.vhdl` 是 PoC 全库最先编译的公共包（`common.files` 第 11 行），提供类型与辅助函数两大地基。
- **数组与子类型**：`T_BOOLVEC`/`T_INTVEC`/`T_NATVEC`/`T_POSVEC`/`T_REALVEC` 补齐了标准库缺失的「基本类型向量」；`T_INT_8` 等子类型给 `integer` 加范围约束，主要用于加速仿真与常量推导。
- **枚举类型**：`T_POLARITY`/`T_BIT_ORDER`/`T_BYTE_ORDER`/`T_CLOCK_EDGE` 等把配置选项变成自解释的名字，并为 `T_POLARITY` 重载了 `"xor"`，让极性可参与运算。
- **数学类函数**：`log2ceil` 用倍增循环求 \(\lceil\log_2 n\rceil\)，是「深度→位宽」映射的核心；`log2ceilnz`/`log10ceil`/`div_ceil`/`ceil_pow2` 等配套使用。
- **条件与聚合函数**：`ite` 是 VHDL 里事实上的三元运算符（十种重载）；`imin`/`imax`/`isum` 既作用于两数也作用于向量。
- **`SIMULATION` 延迟常量**：靠 `--synthesis translate_off` + `Is_X` 自动区分仿真/综合，用于把保护性检查隔离在仿真侧，不污染硬件。

## 7. 下一步学习建议

- 下一讲 [u2-l3 配置机制：my_config 与 config 包](u2-l3-config-mechanism.md) 会展示 `utils` 的实战价值——`config.vhdl` 解析板卡/器件时大量使用 `utils` 里的类型与函数，建议带着「这些辅助函数被谁用」的视角去读。
- 想看 `log2ceil` 的更多真实调用点，可在 `src/` 下搜索它的名字，几乎所有存储器、FIFO、缓存的地址位宽都靠它推导。
- 若你对 `T_BCD` 与 `to_BCD_Vector` 感兴趣，可顺带阅读 `src/io/io_7SegmentMux_BCD.vhdl`（七段显示）相关代码，理解 BCD 在显示外设里的用途。
- 想系统了解「枚举 + 运算符重载」的设计手法，可在后续阅读 `components` 包（[u2-l5](u2-l5-components-primitives.md)）时对照体会。
