# common 基础包：types、attribute、common

## 1. 本讲目标

本讲深入 `common` 模块里三个最基础的 VHDL 包（package）。读完本讲，你应当能够：

- 用 `types_pkg` 提供的数组类型（`slv_vec_t`、`natural_vec_t` 等）声明「一组信号」「一组数值」，并调用 `sum`、`to_sl`、`to_int`、`count_ones` 等函数简化代码。
- 看懂 `attribute_pkg` 里 Xilinx Vivado 综合属性（`dont_touch`、`async_reg`、`ram_style` 等）的含义，并会用 `ram_style_t` 枚举与 `to_attribute` 函数给存储器打属性。
- 理解 `common_pkg` 的 `in_simulation` 如何区分仿真与综合、`if_then_else` 如何当三目运算符用。

这三个包本身不描述任何具体电路行为，而是「写电路时的辅助工具」。它们被全项目几乎所有模块 `use`，是阅读后续源码的识字课。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，VHDL 的 package 是什么。** VHDL 把「可以复用的常量、类型、函数」放进一个 package。package 分两部分：声明（`package xxx is ... end package;`）对外可见，函数实现藏在包体（`package body xxx is ... end package body;`）里。使用时写 `library common; use common.types_pkg.all;`（回顾 u1-l2：库名等于模块名，所以 `common` 模块的库就叫 `common`）。

**第二，`std_logic` 有九种值，`boolean` 只有两个。** `std_logic`（其实是项目里更常用的未解析版 `std_ulogic`）可以是 `'0'`、`'1'`，也可以是 `'X'`、`'U'`、`'Z'`、`'-'` 等非二值状态；而 `boolean` 只有 `true`/`false`。在测试台里，你经常要把这两种类型混在一起做逻辑判断，VHDL 默认不允许 `boolean and std_ulogic`。`types_pkg` 的一大职责就是补上这类「跨类型胶水」。

**第三，什么是综合属性（attribute）。** 属性是一段附加在信号/实例上的「字符串提示」，综合工具（如 Vivado）读到它后会改变实现策略。例如告诉工具「这个寄存器是跨时钟域同步链的一环，不要优化、不要搬动」。属性不是 VHDL 的行为描述，而是面向特定后端工具的「旁注」。本项目只针对 Xilinx Vivado，相关文档是 UG901 与 UG912（见包头注释）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [modules/common/src/types_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd) | 数组类型（向量的向量、整数的向量等）与一批类型转换/位运算函数 |
| [modules/common/src/attribute_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/attribute_pkg.vhd) | Xilinx Vivado 综合属性声明，外加 `ram_style_t` 枚举与 `to_attribute` 转换 |
| [modules/common/src/common_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/common_pkg.vhd) | 放不进别处的杂项：`in_simulation`（仿真判定）、`if_then_else`（三目） |

阅读时还会顺带引用几个「使用方」文件作为真实用例：`resync_pulse.vhd`、`fifo.vhd`、`resync_cycles.vhd`、`tb_handshake_mux.vhd`。

---

## 4. 核心概念与源码讲解

### 4.1 types_pkg：数组类型与类型转换函数

#### 4.1.1 概念说明

写 VHDL 时有两类反复出现的「啰嗦」：

1. **「一组同类型的信号」无法用一个变量装下。** 比如多路复用器（u2-l1 讲过的 `handshake_mux`）有 N 路输入，每路都有一条 `data` 总线。标准库里没有「`std_ulogic_vector` 的数组」这种类型，你需要自己声明一个二维结构。
2. **`std_ulogic` 与 `boolean` 之间不能直接运算。** 测试台里 `wait until ready and valid and rising_edge(clk);` 中，`ready`/`valid` 是 `std_ulogic`，`rising_edge(...)` 返回 `boolean`，三者直接 `and` 会编译报错。

`types_pkg` 就是来解决这两类问题的：它提供了一组「数组的数组」类型，以及一批把两种类型粘起来的转换函数与重载运算符。

#### 4.1.2 核心用法（API 总览）

这个包没有任何时序逻辑，只是一堆类型与纯函数。可按下表分类记忆：

| 分类 | 代表声明 | 用途 |
| --- | --- | --- |
| 向量的向量 | `slv_vec_t`、`unsigned_vec_t`、`signed_vec_t` | 「一组总线」，多路数据的天然容器 |
| 数值的向量 | `integer_vec_t`、`natural_vec_t`、`positive_vec_t`、`real_vec_t`、`time_vec_t` | 「一组数」，配合聚合/求和 |
| 向量的向量之向量 | `integer_matrix_t` | 二维整数表 |
| 布尔向量 | `boolean_vec_t` | 「一组开关」 |
| 求和/最值 | `sum`、`get_maximum` | 对数值向量归约 |
| 跨类型转换 | `to_sl`、`to_bool`、`to_int`、`to_real` | `boolean` ↔ `std_ulogic` ↔ `integer` ↔ `real` |
| 位/字节序 | `swap_bit_order`、`swap_byte_order` | 翻转位序、大小端字节交换 |
| 位计数 | `count_ones` | 统计 `'1'` 的个数（popcount） |
| 合法性检查 | `is_01` | 判断信号是否只有 `'0'`/`'1'`，无 `'X'`/`'U'` |
| 重载运算符 | `"and"` | 允许 `boolean` 与 `std_ulogic` 混用 `and` |

#### 4.1.3 源码精读

**数组类型：一行声明一种「容器」。** 这些类型都是无约束数组（`array (integer range <>) of ...`），下标范围留到实例化时再定：

[types_pkg.vhd:20-22](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L20-L22) 声明了三条「向量的向量」，元素分别是 `std_ulogic_vector` 和两种数值类型 `u_unsigned`/`u_signed`（无符号/有符号数值类型）。

```vhdl
type slv_vec_t is array (integer range <>) of std_ulogic_vector;
type unsigned_vec_t is array (integer range <>) of u_unsigned;
type signed_vec_t is array (integer range <>) of u_signed;
```

`slv_vec_t` 在 u2-l1 讲过的 `handshake_mux` 测试台里被用来一次性声明「每路一条数据总线」。注意它有两个维度：外层是路数，内层是位宽。声明时要两层约束：

[tb_handshake_mux.vhd:51-53](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/test/tb_handshake_mux.vhd#L51-L53) 把 `input_data` 声明为「`input_valid'range` 路、每路 `data_width` 位」的总线数组，内层位宽在第二对括号里给出。

```vhdl
signal input_data : slv_vec_t(input_valid'range)(data_width - 1 downto 0) :=
  (others => (others => '0'));
```

> 小知识：VHDL-2008 才允许在数组类型上再叠加一层无约束维度（即「数组元素仍是数组」并各自带范围）。这正是项目要求按 VHDL-2008 编译的原因之一（见 u1-l2）。

**数值向量与归约函数。** `natural_vec_t` 是「自然数数组」，配了 `sum` 与 `get_maximum`：

[types_pkg.vhd:27-29](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L27-L29) 声明类型与两个归约函数。

```vhdl
type natural_vec_t is array (integer range <>) of natural;
function sum(data : natural_vec_t) return natural;
function get_maximum(data : natural_vec_t) return natural;
```

[types_pkg.vhd:102-110](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L102-L110) 是 `sum` 的实现：遍历累加，注意结果变量类型是 `natural`（下界为 0），所以传负数会运行期报错。

```vhdl
function sum(data : natural_vec_t) return natural is
  variable result : natural := 0;
begin
  for data_idx in data'range loop
    result := result + data(data_idx);
  end loop;
  return result;
end function;
```

`positive_vec_t`（L31）也有同名 `sum`/`get_maximum`（L32-33，实现见 L122-140），靠参数类型重载区分——调用时编译器按实参类型自动选对版本。

**跨类型转换函数。** 这是把 `boolean` 与 `std_ulogic`「双向翻译」的小工具集：

[types_pkg.vhd:46-54](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L46-L54) 集中声明了 `to_sl`/`to_bool`/`to_int` 及其重载，还有子类型 `binary_integer_t`（只有 0/1 两个值的整数）。

```vhdl
function to_sl(value : boolean) return std_ulogic;
function to_bool(value : std_ulogic) return boolean;
subtype binary_integer_t is integer range 0 to 1;
function to_int(value : boolean) return binary_integer_t;
function to_int(value : std_ulogic) return binary_integer_t;
```

`to_int` 在真实源码里很有用：当你要把一个布尔开关算进位宽时，`to_int(enable_last)` 把 `true`/`false` 变成 `1`/`0`。`fifo` 模块就用它把可选的 `last` 位加进存储字宽：

[fifo.vhd:410](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L410) —— 当 `enable_last=true` 时存储字比数据多 1 位。

```vhdl
constant memory_word_width : positive := width + to_int(enable_last);
```

`to_bool`（[types_pkg.vhd:165-174](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L165-L174)）在遇到既不是 `'0'` 也不是 `'1'` 的值时会 `assert` 报错并打印该值——这是「不放过 `'X'`/`'U'`」的防御式写法。

**位计数 `count_ones`（popcount）。**

[types_pkg.vhd:62-63](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L62-L63) 声明了对 `std_ulogic_vector` 与 `u_unsigned` 两个版本；实现 [types_pkg.vhd:239-246](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L239-L246) 逐位调用上面的 `to_int` 求和。数学上就是对每一位求和：

\[
\mathrm{count\_ones}(x) = \sum_{i} x_i,\quad x_i \in \{0,1\}
\]

**重载 `and`：让 `boolean` 与 `std_ulogic` 混用。** 包头注释（[types_pkg.vhd:75-96](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L75-L96)）解释了动机：把 `wait until (ready and valid) = '1' and rising_edge(clk);` 简化成 `wait until ready and valid and rising_edge(clk);`。它提供了四种重载（[types_pkg.vhd:91-95](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L91-L95)），实现里约定「只有 `'1'` 等价于 `true`」：

[types_pkg.vhd:283-286](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L283-L286)

```vhdl
function "and" (left : boolean; right: std_ulogic) return boolean is
begin
  return left and (right = '1');
end function;
```

#### 4.1.4 代码实践

**实践目标：** 用 `natural_vec_t` 装一组数，调用 `sum` 求和；并体验 `to_sl`/`to_int` 的双向转换。

**操作步骤：**

1. 在任意测试台（或新建一个 `tb_types_play.vhd`）的 process 里写下面这段「示例代码」（非项目原有代码）：

```vhdl
-- 示例代码：体验 types_pkg 的数组与转换
library common;
use common.types_pkg.all;

-- ... 在 process 内：
variable values : natural_vec_t(0 to 4) := (1, 2, 3, 4, 5);
variable total  : natural;
begin
  total := sum(values);              -- 期望 15
  report "sum = " & integer'image(total);

  report "to_sl(true) = " & std_ulogic'image(to_sl(true));   -- 期望 '1'
  report "to_int('1') = " & integer'image(to_int('1'));     -- 期望 1
  report "count_ones = " & integer'image(count_ones("10110011")); -- 期望 5
  std.env.stop;
```

2. 想跑起来，把它作为一个 VUnit 测试台挂到某个 `module_*.py` 的 `setup_vunit` 里（参考 u1-l4 的模式），用 `tools/simulate.py` 运行；或在你熟悉的仿真器（GHDL/ModelSim 等）里单独编译 `common` 库后仿真。

**需要观察的现象：** 仿真控制台输出三行 `report`。

**预期结果：** `sum = 15`、`to_sl(true) = '1'`、`to_int('1') = 1`、`count_ones = 5`。

> 由于运行依赖本地工具链，具体命令与输出「待本地验证」。若暂无仿真器，可改为「源码阅读型实践」：打开 [types_pkg.vhd:122-140](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L122-L140)，对照 `positive_vec_t` 版 `sum`，解释为何它的结果变量初值能安全写成 `natural := 0` 而 `get_maximum` 初值写成 `positive := 1`。

#### 4.1.5 小练习与答案

**练习 1：** `slv_vec_t` 为什么需要「两层范围」`(outer)(inner)`？一层不行吗？
**答案：** 外层是「有几路」，内层是「每路几位」。`std_ulogic_vector` 本身已是数组，但它的长度在类型定义时未定；只有给两套范围，仿真器才知道这是一个「N 条、每条 M 位」的二维结构。一层只能表达「N 个无宽度的标量」。

**练习 2：** `to_bool('X')` 会发生什么？为什么这样设计？
**答案：** 它既非 `'0'` 也非 `'1'`，于是触发 [types_pkg.vhd:172](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L172) 的 `assert false`，打印 `Can not convert value: X` 并默认以 `error` 级别报错。这样设计是为了不让 `'X'`/`'U'` 被悄悄当成 `false`，把隐藏的竞争/未初始化问题尽早暴露。

**练习 3：** `count_ones` 为什么对 `u_unsigned` 单独写一个版本，而不是让用户自己转 `std_ulogic_vector`？
**答案：** 为了方便与类型安全。它内部（[types_pkg.vhd:248-252](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/types_pkg.vhd#L248-L252)）就是转成 `std_ulogic_vector` 再调同名函数，重载让调用方少写一次类型转换。

---

### 4.2 attribute_pkg：Xilinx 综合属性与 ram_style_t

#### 4.2.1 概念说明

写可综合 RTL 时，光描述「逻辑该怎样」还不够，有时你得告诉综合工具「这个信号请特别对待」。例如：

- 跨时钟域同步链上的两个寄存器，**绝不能被优化掉、不能被搬离同一个 slice**，否则会严重恶化亚稳态的 MTBF（平均无故障时间）。
- 一段被推断成 RAM 的存储器，你希望它落到 **块 RAM（BRAM）** 还是 **分布式 RAM（LUTRAM）**，差别巨大：BRAM 容量大但延迟可能多一拍，LUTRAM 快但吃 LUT 资源。

这些「特别对待」就是综合属性。VHDL 里属性写成 `attribute <名> of <对象> : <对象类别> is "<值>";`，值永远是字符串。`attribute_pkg` 把项目用到的 Xilinx 属性集中声明，避免每个文件各写一遍，并为最常用的 `ram_style` 提供了枚举与转换函数，让值可以「按枚举选、由函数翻译成字符串」。

#### 4.2.2 核心用法（属性清单）

[attribute_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/attribute_pkg.vhd) 声明的属性及其用途：

| 属性 | 取值 | 作用 |
| --- | --- | --- |
| `dont_touch` (L23) | `"true"`/`"false"` | 阻止优化/吸收，且前推到布局布线（比 `KEEP` 更强） |
| `mark_debug` (L30) | `"true"`/`"false"` | 保留网络供硬件调试（ILA 抓信号） |
| `async_reg` (L37) | `"true"`/`"false"` | 标记寄存器在同步链中、接收异步数据 |
| `ram_style` (L44) | `"block"`/`"distributed"`/`"registers"`/`"ultra"`/`"auto"` | 指定推断 RAM 的实现原语 |
| `use_dsp` (L61) | `"yes"`/`"no"`/`"logic"` | 是否把算术结构映射进 DSP48 |
| `iob` (L67) | `"true"`/`"false"` | 把寄存器放进 I/O 缓冲（IOB） |
| `pullup`/`pulldown` (L74/L81) | `"true"`/`"false"` 等 | 三态网络弱上拉/下拉 |
| `shreg_extract` (L87) | `"yes"`/`"no"` | 是否把移位寄存器推断成 SRL |

#### 4.2.3 源码精读

**`ram_style_t` 枚举：用强类型代替手写字符串。**

[attribute_pkg.vhd:44-52](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/attribute_pkg.vhd#L44-L52) 先声明 `ram_style` 字符串属性，再定义一个枚举 `ram_style_t`，最后声明转换函数 `to_attribute`：

```vhdl
attribute ram_style : string;
type ram_style_t is (
  ram_style_block,
  ram_style_distributed,
  ram_style_registers,
  ram_style_ultra,
  ram_style_auto
);
function to_attribute(ram_style_enum : ram_style_t) return string;
```

这样做的好处是：模块的 generic 可以写成 `ram_type : ram_style_t := ram_style_auto`，调用方只能从五个合法值里选，拼错或写非法字符串在编译期就被拦截，而不用等到综合日志里才报错。`fifo` 模块正是这么暴露 generic 的：

[fifo.vhd:91](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L91)

```vhdl
ram_type : ram_style_t := ram_style_auto
```

**`to_attribute`：枚举值 → Vivado 认的字符串。**

[attribute_pkg.vhd:94-117](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/attribute_pkg.vhd#L94-L117) 用 `case` 把每个枚举映射成 Vivado 文档（UG912）规定的字符串；落到 `when others` 分支会 `assert false severity failure`，理论上不会触发（枚举已穷举）。

```vhdl
case ram_style_enum is
  when ram_style_block => return "block";
  when ram_style_distributed => return "distributed";
  ...
  when others => assert false severity failure; return "error";
end case;
```

**真实用法：把属性打到信号上。** 综合属性要生效，必须按 VHDL 语法「声明 + 赋值」两步。`fifo` 在存储器信号 `mem` 上同时用了上面两个工具：generic 用枚举类型，属性值用 `to_attribute` 翻译：

[fifo.vhd:414-415](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L414-L415)

```vhdl
signal mem : mem_t(0 to memory_depth - 1) := (others => (others => '0'));
attribute ram_style of mem : signal is to_attribute(ram_type);
```

**另一个高频组合：`dont_touch` + `async_reg`。** 跨时钟域模块 `resync_pulse` 用这两个属性「锁死」同步链上的寄存器：

[resync_pulse.vhd:107-114](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_pulse.vhd#L107-L114) —— 注释点明：这两级寄存器喂给 `async_reg` 同步链，**必须由触发器驱动**，所以打 `dont_touch` 防止工具把它们吸收进组合逻辑；再打 `async_reg` 让工具把它们放在同一个 slice 以最大化 MTBF。

```vhdl
attribute dont_touch of level_in : signal is "true";
attribute dont_touch of level_out : signal is "true";
attribute async_reg of level_out_m1 : signal is "true";
attribute async_reg of level_out : signal is "true";
```

#### 4.2.4 代码实践

**实践目标：** 仿照 `fifo`，用 `ram_style_t` + `to_attribute` 给一段存储器指定「块 RAM」实现。

**操作步骤：**

1. 在一个可综合实体里写下面这段「示例代码」（仿照 [fifo.vhd:409-415](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L409-L415) 的结构）：

```vhdl
-- 示例代码：用枚举指定 BRAM
library common;
use common.attribute_pkg.all;

architecture a of my_mem is
  type mem_t is array (0 to 1023) of std_ulogic_vector(31 downto 0);
  signal mem : mem_t := (others => (others => '0'));
  attribute ram_style of mem : signal is to_attribute(ram_style_block);
begin
  -- 读写逻辑略
end architecture;
```

2. 若想观察综合效果，可用 `tools/synthesize.py`（见 u9-l2）把这个实体综合成 netlist，再查看资源报告中 BRAM 占用数。

**需要观察的现象：** 综合后资源报告里出现 `RAMB36`/`RAMB18`（块 RAM 原语）被使用，而 LUTRAM 近似为 0。

**预期结果：** 指定 `ram_style_block` → 命中 BRAM；若把枚举换成 `ram_style_distributed` 重新综合，同样的存储会落到分布式 RAM，BRAM 归零、LUT 上升。

> 实际综合资源数字「待本地验证」，取决于器件型号与工具版本。若暂无 Vivado，可改为「源码阅读型实践」：用 `grep` 在 `modules/` 下统计 `to_attribute(ram_style_distributed)` 出现的位置（提示：`resync/src/resync_twophase_lutram.vhd:78`、`resync/src/resync_rarely_valid_lutram.vhd:89`），解释为何这两处跨域查找表型同步要强制用分布式 RAM。

#### 4.2.5 小练习与答案

**练习 1：** 为什么用 `ram_style_t` 枚举 + `to_attribute`，而不是直接写 `attribute ram_style of mem : signal is "block";`？
**答案：** 字符串裸写无法在编译期查错，拼成 `"blokc"` 也要等综合才暴露。枚举把合法值固定成符号，generic 端只能传合法值，`case` 又保证翻译完整，从而把错误前移到编译期；同时也让 generic 在 IP 使用者看来是「有选项的下拉」，可读性更好。

**练习 2：** `dont_touch` 和 `async_reg` 各自阻止什么？为什么跨时钟域同步链两者都要？
**答案：** `dont_touch` 阻止综合把信号优化掉或吸收进别处逻辑（前推到布线阶段）；`async_reg` 告知工具该寄存器处在异步采样链上，应放在紧邻上一级的同一个 slice 里。同步链的可靠性来自「两级寄存器物理靠近、各自由触发器驱动」，所以既要防优化（`dont_touch`）又要约束布局（`async_reg`）。

**练习 3：** `to_attribute` 的 `when others => assert false severity failure` 何时会触发？
**答案：** 枚举已穷举五个值，正常情况下永不触发。这是防御式写法：万一将来有人往 `ram_style_t` 加了新枚举却忘了在 `case` 里补分支，运行到这里会立即失败，把遗漏暴露出来，而不是悄悄返回错误字符串。

---

### 4.3 common_pkg：in_simulation 与 if_then_else

#### 4.3.1 概念说明

`common_pkg` 很小，只放「无处安家又不够格单开一个包」的东西。它解决两个小问题：

- **「现在是不是在仿真？」** 同一份 RTL，在仿真时要插入断言/检查器，但在真实 FPGA 里这些检查既浪费资源又无意义。需要一个能在编译/精化期区分两端的开关。
- **「VHDL 没有三目运算符」。** C 里的 `cond ? a : b` 在 VHDL 里要写一整个 `if ... then ... else ... end if;`，对「给常量选个字符串」这种小事很啰嗦。

#### 4.3.2 核心用法

| 函数 | 作用 |
| --- | --- |
| `in_simulation` (L19) | 仿真返回 `true`，综合返回 `false`；用来 guard 只该在仿真存在的代码 |
| `if_then_else` (L26) | 三目运算符：`condition` 真则返回 `value_if_true`，否则 `value_if_false` |

#### 4.3.3 源码精读

**`in_simulation`：靠综合注释双向翻译。**

[common_pkg.vhd:34-41](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/common_pkg.vhd#L34-L41)

```vhdl
function in_simulation return boolean is
begin
  -- synthesis translate_off
  return true;
  -- synthesis translate_on

  return false;
end function;
```

关键在两行综合指令（`synthesis translate_off`/`translate_on`）：**仿真器无视它们**，于是函数直接 `return true`；**综合器遇到 `translate_off` 会跳过其间代码**，于是只剩最后那行 `return false`。同一份源码，两端各取所需。注意它没有参数，调用时写 `in_simulation`（不加括号也可，VHDL 允许无参函数调用省略括号列表）。

**真实用法 1：用 `generate` 守卫仿真专属断言。** `resync_cycles` 用来测量两个时钟域之间的周期差，其自检断言只在仿真有意义：

[resync_cycles.vhd:37](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L37) 引入函数；[resync_cycles.vhd:106](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L106) 用它做 `generate` 条件：

```vhdl
use common.common_pkg.in_simulation;
...
assertions_gen : if in_simulation generate
```

综合时 `in_simulation` 为 `false`，整个 `generate` 块连同里面的断言进程被删除，零资源占用（呼应 u1-l1 讲过的「generic/条件为假即零资源」哲学）。

**`if_then_else`：三目运算符。**

[common_pkg.vhd:43-52](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/common_pkg.vhd#L43-L52) 当前只对 `string` 重载（注释说明可按需扩展到其他类型）：

```vhdl
function if_then_else(
  condition : boolean; value_if_true : string; value_if_false : string
) return string is
begin
  if condition then
    return value_if_true;
  end if;
  return value_if_false;
end function;
```

**真实用法 2：在精化期给属性选字符串。** 回到 4.2 的 `resync_pulse`：反馈链是否启用决定了两级反馈寄存器是否真实存在，所以 `async_reg` 属性必须「只在启用时为 `"true"`」，否则会出现「属性标了但寄存器不存在」的告警。作者用 `if_then_else` 在一行内算出这个字符串：

[resync_pulse.vhd:116-121](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_pulse.vhd#L116-L121)

```vhdl
constant async_reg_feedback : string := if_then_else(enable_feedback, "true", "false");
attribute async_reg of level_out_feedback_m1 : signal is async_reg_feedback;
attribute async_reg of level_out_feedback : signal is async_reg_feedback;
```

这是三个包协同的好例子：`common_pkg.if_then_else` 算出一个字符串，喂给 `attribute_pkg.async_reg` 属性；而 `enable_feedback` 是个布尔 generic，必要时还能用 `types_pkg.to_sl` 转成电平。

#### 4.3.4 代码实践

**实践目标：** 用 `in_simulation` 给一段 RTL 加一个「只在仿真报错」的断言；用 `if_then_else` 在精化期选择一个常量字符串。

**操作步骤：**

1. 在一个实体的架构里写下面「示例代码」：

```vhdl
-- 示例代码：in_simulation + if_then_else
library common;
use common.common_pkg.all;

architecture a of demo is
  constant banner : string := if_then_else(sim_verbose, "ON", "OFF");
begin
  check_gen : if in_simulation generate
    assert_proc : process is
    begin
      wait until rising_edge(clk);
      -- 仅仿真期检查，例如某信号不应为 'X'
    end process;
  end generate;
end architecture;
```

2. 分别在仿真器里跑、在综合器里综合这份代码。

**需要观察的现象：** 仿真时 `assert_proc` 存在并运行；综合后网表里找不到该进程，资源占用不增加。

**预期结果：** 仿真期断言生效；综合期 `generate` 块被删除。`banner` 取值随 `sim_verbose` 在 `"ON"`/`"OFF"` 间切换。

> 具体资源数字「待本地验证」。源码阅读型替代实践：阅读 [resync_cycles.vhd:106-117](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L106-L117)，说明这个被 `in_simulation` 守卫的断言在检测什么故障（提示：输入过密导致输出丢失）。

#### 4.3.5 小练习与答案

**练习 1：** 为什么不直接用一个 `constant IN_SIM : boolean := true;`，而要搞 `synthesis translate_off/on`？
**答案：** 那样常量在两端都是 `true`，综合时 `generate` 块不会被删除，断言代码会被综合进网表，浪费资源甚至引入意外行为。`translate_off/on` 才能让同一个函数在两端返回不同值，实现「一份源码、两端各异」。

**练习 2：** `if_then_else` 为什么当前只重载了 `string`？
**答案：** 因为现有的真实需求（给属性选字符串）只需要 `string`。注释（[common_pkg.vhd:24-25](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/common_pkg.vhd#L24-L25)）明确说可按需为其他类型重载——这是「够用即可、不过度设计」的体现。

**练习 3：** `in_simulation` 函数体里 `return true;` 之后还有 `return false;`，为什么仿真器不会因为「不可达语句」报警？
**答案：** 对仿真器而言，第一个 `return true;` 确实先执行，后面的 `return false;` 不可达但语法合法（VHDL 函数需要保证有返回路径）；对综合器而言，`translate_off` 段被删，首条 `return` 消失，`return false;` 反而成了唯一可达语句。两段注释各服务一个后端。

---

## 5. 综合实践

把三个包串起来用。目标：写一个「带可选调试断言的小存储器」模块，体现本项目「用 generic/枚举裁剪功能、用属性控制实现、用仿真守卫隔离检查」的一贯风格。

任务要求（示例代码，非项目原有）：

1. 声明一个存储器 `signal mem : mem_t`，并用 `ram_style_t` + `to_attribute` 让它的实现方式由 generic `ram_type` 决定（默认 `ram_style_auto`）。
2. 用 `natural_vec_t` 保存「各 bank 的深度」，用 `sum` 算出总深度并 `report` 出来。
3. 用 `if_then_else` 根据 generic `enable_guard` 算出一个 `guard_attr : string`，作为某 `dont_touch` 信号的属性值（呼应 `resync_pulse` 的写法）。
4. 用 `if in_simulation generate` 包住一个断言进程，断言 `mem` 写入地址不会越界。

```vhdl
-- 示例代码：综合实践（仅示意结构，省略读写进程）
library ieee;
use ieee.std_logic_1164.all;
library common;
use common.types_pkg.all;
use common.attribute_pkg.all;
use common.common_pkg.all;

entity mini_mem is
  generic (
    bank_depths : natural_vec_t := (256, 512, 128);  -- 各 bank 深度
    ram_type    : ram_style_t := ram_style_auto;
    enable_guard: boolean := true
  );
end entity;

architecture a of mini_mem is
  constant total_depth : natural := sum(bank_depths);   -- types_pkg.sum
  type mem_t is array (0 to total_depth - 1) of std_ulogic_vector(31 downto 0);
  signal mem : mem_t := (others => (others => '0'));
  attribute ram_style of mem : signal is to_attribute(ram_type);  -- attribute_pkg

  signal guard_node : std_ulogic := '0';
  constant guard_attr : string := if_then_else(enable_guard, "true", "false");  -- common_pkg
  attribute dont_touch of guard_node : signal is guard_attr;       -- attribute_pkg
begin
  assert_gen : if in_simulation generate          -- common_pkg
    chk : process is
    begin
      wait until rising_edge(clk);
      assert wr_addr < total_depth report "write address out of range!" severity error;
    end process;
  end generate;
end architecture;
```

完成后，对照本讲三个包逐一标注：哪些行用了 `types_pkg`、哪些用了 `attribute_pkg`、哪些用了 `common_pkg`。这正是后续阅读 `fifo`、`resync`、`axi_*` 等模块时你会反复看到的「三件套」组合。

> 综合与仿真结果「待本地验证」。

## 6. 本讲小结

- `types_pkg` 提供数组的数组（`slv_vec_t` 等）与跨类型胶水函数（`to_sl`/`to_bool`/`to_int`/`count_ones`/重载 `and`），解决「一组信号」与「`std_ulogic`/`boolean` 混用」两类啰嗦。
- `slv_vec_t` 是二维结构，外层路数、内层位宽，是 `handshake_mux` 等多路模块的容器（承接 u2-l1）。
- `attribute_pkg` 集中声明 Xilinx Vivado 综合属性；其中 `ram_style_t` 枚举 + `to_attribute` 把「存哪种 RAM」从易错的字符串提升为编译期可查的强类型 generic。
- `dont_touch` 防止优化吸收、`async_reg` 约束同步链布局，两者共同保障跨时钟域同步链的 MTBF（承接 u1-l1 的质量优先）。
- `common_pkg.in_simulation` 用 `translate_off/on` 实现「一份源码、仿真与综合各取所需」；`if_then_else` 当三目运算符用，常在精化期为属性选字符串。
- 三个包常协同出现：`if_then_else` 算字符串 → 喂给属性；布尔开关可用 `to_sl` 转电平；仿真检查用 `in_simulation` 守卫。

## 7. 下一步学习建议

- 下一讲 **u2-l3（math 模块）** 会进入另一组基础包：饱和、四舍五入截断、无符号除法，依赖本讲的类型转换直觉。
- 想立刻看到这些包的「实战」，推荐先读 **u3-l1（resync 基础）**：`resync_pulse.vhd` 几乎用到了本讲全部内容（`dont_touch`/`async_reg`/`if_then_else`），是最好的复习材料。
- 想看 `ram_style_t` 在 generic 链上如何层层传递，可先扫一眼 [fifo.vhd:91](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L91)，详细讲解在 **u4-l1（同步 FIFO）**。
