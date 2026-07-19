# psi_common_math_pkg 数学工具函数

## 1. 本讲目标

`psi_common_math_pkg` 是整个 psi_common 库里被引用最多的 package 之一。它本身不描述任何硬件，而是提供一批**编译期（elaboration time）就能算出结果**的函数，让其它组件可以用 generic 参数自动推导出端口位宽、地址范围、计数上限等常量。

学完本讲你应该能够：

1. 用 `log2` / `log2ceil` 根据深度或数量推导所需的二进制位宽，并说清两者的差别。
2. 理解 `choose` 的多态重载，知道它为什么能在 entity 端口声明里充当"三元运算符"。
3. 用 `to_uslv` / `to_sslv` / `from_uslv` / `from_sslv` 在整数与 `std_logic_vector` 之间互转，并用 `from_str` 把字符串解析成实数。
4. 知道 `ratio` / `is_int_ratio` / `nearest_pow2` 在时钟分频与 AXI 寄存器计数场景里的用途与局限。

本讲只读一个源码文件，但它会被后续的 RAM、FIFO、CDC、strobe、AXI 等几乎所有讲义反复引用。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是 VHDL 的 function？** VHDL 的 `function` 是一段在仿真或细化（elaboration）阶段被求值的代码。当它的所有入参都是常量（比如 generic），综合工具会在编译期把函数调用替换成一个常数，**不产生任何硬件电路**。math_pkg 里几乎所有函数都是这种"编译期工具函数"——它们用来算常量，而不是综合成逻辑门。

**第二，为什么需要位宽推导？** 假设你写了一个深度可配置的 RAM，generic 是 `depth_g`。地址端口的位宽取决于 `depth_g`：深度 1024 需要 10 位地址，深度 512 需要 9 位。你不可能手写一个固定宽度，于是需要"给定深度，算出位宽"的函数——这正是 `log2ceil`。

**第三，`std_logic_vector` 与整数不能直接赋值。** VHDL 是强类型语言，`std_logic_vector` 在 `numeric_std` 里只是比特串，本身不带"有符号/无符号"语义。要把它当数字用，必须先转成 `unsigned` / `signed`。`to_uslv` / `from_uslv` 就是为了省掉那行又长又啰嗦的 `std_logic_vector(to_unsigned(...))`。

> 承接 u1-l4：本讲涉及的命名规范（snake_case、`_i/_o` 后缀）与 package/entity 约定已经在上一讲建立。本讲出现的是 package，遵循"一个文件一个 package"的库约定。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它依赖另一个 package：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_common_math_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd) | 本讲主角。声明并实现全部数学工具函数。 |
| hdl/psi_common_array_pkg.vhd | math_pkg 在头部 `use work.psi_common_array_pkg.all`，从中引入 `t_ainteger` / `t_areal` / `t_abool` 数组类型（`count`、`max_a`、`from_str(t_areal)` 要用到）。这是 u2-l3 的内容，本讲只把它当成"已经定义好的数组类型"。 |

另外，本讲会引用几个**真实使用方**来证明这些函数确实在库里被用到：

- `hdl/psi_common_sdp_ram.vhd` —— 用 `log2ceil` 推导地址位宽。
- `hdl/psi_common_sync_fifo.vhd` —— 用 `log2ceil(depth_g + 1)` 推导电平输出位宽。
- `hdl/psi_common_ping_pong.vhd`、`hdl/psi_common_trigger_digital.vhd`、`hdl/psi_common_debouncer.vhd` —— 用 `choose` 在端口声明里做条件选择。
- `hdl/psi_common_pulse_generator_ctrl_static.vhd`、`hdl/psi_common_delay_cfg.vhd` —— 用 `to_uslv` / `from_uslv`。
- `hdl/psi_common_strobe_generator.vhd` —— 计算 clock/strobe 频率比（与本讲的 `ratio` 对照）。

## 4. 核心概念与源码讲解

按规格，本讲拆成四个最小模块：

- 4.1 对数与位宽函数：`log2` / `log2ceil` / `isLog2`
- 4.2 取极值与条件选择：`max` / `min` / `choose` / `max_a` / `min_a` / `count`
- 4.3 数值与字符串转换：`to_uslv` / `to_sslv` / `from_uslv` / `from_sslv` / `from_str`
- 4.4 比例与幂次：`ratio` / `is_int_ratio` / `nearest_pow2`

---

### 4.1 对数与位宽函数

#### 4.1.1 概念说明

给定一个数量 `n`（例如 RAM 深度、寄存器个数、请求者数量），我们经常要回答一个问题：**用几位二进制才能无歧义地编号 0 到 n-1？**

答案是向上取整的以 2 为底的对数：

\[
\text{位数} = \lceil \log_2 n \rceil
\]

直观例子：

| n（要编号的数量） | 所需位数 \(\lceil\log_2 n\rceil\) | 说明 |
| --- | --- | --- |
| 1 | 0 | 只有 1 个值，理论上 0 位即可（实际工程常至少留 1 位） |
| 2 | 1 | 0、1 |
| 4 | 2 | 00、01、10、11（恰好是 2 的幂） |
| 5 | 3 | 4 不够装 5 个，进位到 3 位 |
| 8 | 3 | 恰好是 2 的幂 |
| 1024 | 10 | \(2^{10}=1024\) |

math_pkg 给出两个对数函数：`log2` 返回**向下取整**（floor），`log2ceil` 返回**向上取整**（ceil）。推导位宽几乎总是用 `log2ceil`。

#### 4.1.2 核心流程

`log2(arg)` 用"不断除以 2 数次数"的办法求 floor：

```
v = arg, r = 0
while v > 1:
    v = v / 2
    r = r + 1
return r
```

`log2ceil(arg)` 则复用 `log2`，用一个经典恒等式把"向上取整"转成"向下取整"：

\[
\lceil \log_2 n \rceil = \lfloor \log_2(2n - 1) \rfloor, \quad n \geq 1
\]

为什么 `2n-1` 里的那个 `-1` 很关键？因为当 `n` 恰好是 2 的幂时（比如 4），我们不希望多算一位。\(2\times4-1=7\)，\(\lfloor\log_2 7\rfloor=2\)，正好还是 2 位；而 \(2\times5-1=9\)，\(\lfloor\log_2 9\rfloor=3\)，正确地进位到 3 位。

`isLog2(arg)` 判断 `arg` 是不是 2 的幂：当且仅当 floor 与 ceil 相等。

```
return log2(arg) = log2ceil(arg)
```

#### 4.1.3 源码精读

**`log2`（向下取整）** —— 整数除法循环：

```vhdl
function log2(arg : in natural) return natural is
  variable v : natural := arg;
  variable r : natural := 0;
begin
  while v > 1 loop
    v := v / 2;
    r := r + 1;
  end loop;
  return r;
end function;
```

> 见 [psi_common_math_pkg.vhd:L152-L161](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L152-L161)。注意它对 `arg=0` 或 `arg=1` 都返回 0。

**`log2ceil`（向上取整，natural 入参）** —— 用 `log2(arg*2-1)` 实现 ceil，并对 0 做特判：

```vhdl
function log2ceil(arg : in natural) return natural is
begin
  if arg = 0 then
    return 0;
  end if;
  return log2(arg * 2 - 1);
end function;
```

> 见 [psi_common_math_pkg.vhd:L164-L170](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L164-L170)。

**`log2ceil`（real 入参重载）** —— 当比值不是整数时（例如频率比 100.0/30.0），用 real 重载更直观，它直接对 real 不断除 2：

```vhdl
function log2ceil(arg : in real) return natural is
  variable v : real    := arg;
  variable r : natural := 0;
begin
  while v > 1.0 loop
    v := v / 2.0;
    r := r + 1;
  end loop;
  return r;
end function;
```

> 见 [psi_common_math_pkg.vhd:L173-L182](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L173-L182)。测试平台里就有真实用法：`pwm_tb` 用它算频率比所需的计数位宽
> [psi_common_pwm_tb.vhd:L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pwm_tb/psi_common_pwm_tb.vhd#L29)。

**`isLog2`** —— 用 floor 与 ceil 是否相等判定 2 的幂：

```vhdl
function isLog2(arg : in natural) return boolean is
begin
  if log2(arg) = log2ceil(arg) then  return true;
  else                              return false;
  end if;
end function;
```

> 见 [psi_common_math_pkg.vhd:L185-L192](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L185-L192)。

**真实使用方：sdp_ram 的地址端口**。最典型的用法是在 entity 端口声明里用 `log2ceil(depth_g)-1 downto 0` 自动确定地址位宽：

```vhdl
wr_addr_i : in std_logic_vector(log2ceil(depth_g) - 1 downto 0) := (others => '0');
rd_addr_i : in std_logic_vector(log2ceil(depth_g) - 1 downto 0) := (others => '0');
```

> 见 [psi_common_sdp_ram.vhd:L25-L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L25-L29)。`sync_fifo` 的电平输出更微妙，用的是 `log2ceil(depth_g + 1)`，因为电平可取 0…depth 共 depth+1 个值
> [psi_common_sync_fifo.vhd:L45-L49](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_fifo.vhd#L45-L49)。

#### 4.1.4 代码实践

**实践目标**：亲手用 `log2ceil` 推导深度 1024 的 RAM 地址位宽，验证 2 的幂与非 2 的幂的差异。

**操作步骤**：

1. 新建一个最小的 testbench 文件 `math_pkg_try_tb.vhd`（放在你自己的工程里，不要写进 psi_common 仓库）。
2. 在库声明里 `use work.psi_common_math_pkg.all;`。
3. 在 architecture 的声明区写如下常量并 `report` 出来：

```vhdl
-- 示例代码：仅用于观察编译期函数的求值结果，不对应任何真实硬件
constant RAM_DEPTH_C   : positive := 1024;
constant ADDR_BITS_C   : natural  := log2ceil(RAM_DEPTH_C);   -- 期望 10
constant NON_POW2_C    : natural  := log2ceil(1000);          -- 期望 10（1000 介于 512 与 1024 之间）
constant FLOOR_C       : natural  := log2(1024);              -- 期望 10
constant FLOOR_NON_C   : natural  := log2(1000);              -- 期望 9（floor）
constant ISPOW_C       : boolean  := isLog2(1024);            -- 期望 true
constant ISPOW_NON_C   : boolean  := isLog2(1000);            -- 期望 false
```

4. 在 stimulus 进程里加 `report` 把它们打印出来，跑一次仿真。

**需要观察的现象**：编译期常量在仿真开始的 0 时刻就能在控制台看到，无需时钟驱动。

**预期结果**：

| 表达式 | 结果 |
| --- | --- |
| `log2ceil(1024)` | 10 |
| `log2ceil(1000)` | 10 |
| `log2(1024)` | 10 |
| `log2(1000)` | 9 |
| `isLog2(1024)` | true |
| `isLog2(1000)` | false |

如果 `log2(1000)` 与 `log2ceil(1000)` 结果不同，就直观看到了 ceil 对"非 2 的幂"的进位效果。**待本地验证**：不同仿真器对 `report` 的输出格式略有差异，但常量值应当如上表。

#### 4.1.5 小练习与答案

**练习 1**：一个 FIFO 深度为 33，它需要几位读写指针？用 `log2ceil` 该怎么写？

> 答案：`log2ceil(33)`。\(2\times33-1=65\)，\(\lfloor\log_2 65\rfloor=6\)，所以是 6 位（\(2^5=32\) 不够，\(2^6=64\) 够）。

**练习 2**：为什么 `sync_fifo` 的电平输出用 `log2ceil(depth_g + 1)` 而不是 `log2ceil(depth_g)`？

> 答案：电平的取值范围是 0 到 depth，共 depth+1 个状态，比地址多一个状态，所以是 `+1` 后再取 ceil-log2。

---

### 4.2 取极值与条件选择

#### 4.2.1 概念说明

这一组函数解决两个高频小需求：

1. **取两个值的最大/最小** —— `max` / `min`。VHDL 标准库里没有内置的 `max(integer, integer)`，写起来很啰嗦，所以封装一下。
2. **根据布尔条件在两个值里选一个** —— `choose(s, t, f)`：`s` 为真返回 `t`，否则返回 `f`。它相当于 C/Python 里的三元表达式 `s ? t : f`。

`choose` 看起来平凡，但有个关键用途：**VHDL 的 entity 端口/generic 声明区里不能写 `if-then-else` 语句**，但可以写函数调用。于是 `choose` 成了"在端口声明里根据 generic 条件选择位宽"的惯用手法，整个库里到处都是。

数组版本 `max_a` / `min_a` / `count` 把这些操作扩展到 `array_pkg` 提供的数组类型上。

#### 4.2.2 核心流程

- `max(a,b)` / `min(a,b)`：一次比较，返回较大/较小者。各有 `integer` 与 `real` 两个重载。
- `choose(s,t,f)`：`if s then return t; else return f;`。本包共提供 **8 个重载**，覆盖 `std_logic`、`std_logic_vector`、`integer`、`string`、`real`、`unsigned`、`boolean`、`t_areal`。
- `max_a(a)` / `min_a(a)`：遍历数组，逐元素用 `max`/`min` 累积。
- `count(a,v)`：遍历数组，统计值等于 `v` 的元素个数；对 `std_logic_vector` 重载而言就是统计某个比特（如 `'1'`）出现的次数，相当于 popcount。

#### 4.2.3 源码精读

**`max` / `min`（integer 重载）** —— 单个比较：

```vhdl
function max(a : integer; b : integer) return integer is
begin
  if a > b then return a; else return b; end if;
end function;
```

> 见 [psi_common_math_pkg.vhd:L195-L203](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L195-L203)（`min` 紧随其后
> [L216-L224](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L216-L224)）。real 重载结构相同。

**`choose` 的 8 个重载** —— 逻辑完全一样，只是类型不同。以 `std_logic` 与 `unsigned` 为例：

```vhdl
function choose(s : boolean; t : std_logic; f : std_logic) return std_logic is
begin
  if s then return t; else return f; end if;
end function;

function choose(s : boolean; t : unsigned; f : unsigned) return unsigned is
begin
  if s then return t; else return f; end if;
end function;
```

> std_logic 版见 [psi_common_math_pkg.vhd:L237-L246](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L237-L246)；unsigned 版见
> [L297-L306](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L297-L306)。完整重载列表在头部声明区
> [L40-L70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L40-L70)。

**真实使用方 1：在端口声明里条件选择位宽**。`trigger_digital` 把 `choose` 和 `log2ceil` 嵌套，根据触发器个数决定配置端口的宽度：

```vhdl
trg_digital_source_cfg_i : in std_logic_vector(
    choose(trig_nb_g > 1, log2ceil(trig_nb_g) - 1, 0) downto 0);
```

> 见 [psi_common_trigger_digital.vhd:L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd#L32)。当 `trig_nb_g=1` 时不需选择位（宽度 0+1=1），多于 1 个才需要 `log2ceil` 位。

**真实使用方 2：boolean 重载做极性判断**。`debouncer` 用它把一段"输入/输出极性是否一致"的比较压成一行常量：

```vhdl
constant pol_eq_c : boolean := choose(in_pol_g = out_pol_g, true, false);
```

> 见 [psi_common_debouncer.vhd:L38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L38)。（此处仅为演示 boolean 重载；严格说这行等价于直接写 `in_pol_g = out_pol_g`。）

**`count`（std_logic_vector 重载）** —— 逐位比对：

```vhdl
function count(a : std_logic_vector; v : std_logic) return integer is
  variable cnt_v : integer := 0;
begin
  for idx in a'low to a'high loop
    if a(idx) = v then cnt_v := cnt_v + 1; end if;
  end loop;
  return cnt_v;
end function;
```

> 见 [psi_common_math_pkg.vhd:L359-L369](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L359-L369)。`max_a` / `min_a` 的遍历结构见
> [L524-L533](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L524-L33)
> / [L548-L557](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L548-L557)。

> ⚠️ **一个需要留意的局限**：`max_a` / `min_a` 的累积变量初值是 `0`（见 [L525](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L525) 与 [L549](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L549)）。若数组**全部为负**，`max_a` 仍会返回 0 而非真实的最大负数。本库组件的数组一般表示通道数/位宽/系数，默认非负，所以实践中不踩这个坑——但自己用时要知道这个前提。

#### 4.2.4 代码实践

**实践目标**：体会 `choose` 在端口声明里的"三元运算符"价值，并理解重载如何按上下文自动挑选。

**操作步骤**：

1. 阅读 `ping_pong.vhd` 第 38 行这一段端口声明：

```vhdl
dat_i : in std_logic_vector(choose(tdm_g, width_g - 1, ch_nb_g * width_g - 1) downto 0)
```

   > 见 [psi_common_ping_pong.vhd:L38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L38)。

2. 解释它的含义：当 `tdm_g=true`（TDM 模式，串行单通道）时数据端口宽度是 `width_g`；否则（并行模式）是 `ch_nb_g * width_g`。
3. 在你自己的某个练习 entity 里，模仿这种写法，用一个 generic `mode_g` 决定端口宽度，例如：

```vhdl
-- 示例代码：根据模式选择端口宽度
port (
    dat_i : in std_logic_vector(choose(mode_g, 7, 15) downto 0)
);
```

**需要观察的现象**：分别把 `mode_g` 设为 `true` / `false` 重新综合或 elaborate，观察 `dat_i` 的宽度在 8 与 16 之间切换。

**预期结果**：`mode_g=true` → 宽度 8；`mode_g=false` → 宽度 16。这相当于在端口声明区写了一个无法用 `if` 表达的条件分支。**待本地验证**：具体宽度报告请以你所用综合工具的端口列表为准。

#### 4.2.5 小练习与答案

**练习 1**：库里有几个 `choose` 重载？为什么需要这么多？

> 答案：8 个，覆盖 `std_logic` / `std_logic_vector` / `integer` / `string` / `real` / `unsigned` / `boolean` / `t_areal`。VHDL 是强类型语言，三元选择的两个候选必须类型完全一致，且函数返回类型在调用点必须能被编译器无歧义地推断，所以为每种常用类型各写一个。

**练习 2**：`count(x"FF", '1')` 返回多少？（`x"FF"` 是 8 位全 1）

> 答案：8。它逐位统计等于 `'1'` 的位数，即 8 位 popcount。

---

### 4.3 数值与字符串转换

#### 4.3.1 概念说明

这一组解决"整数 ↔ `std_logic_vector`"和"字符串 → real"两类转换。

**为什么需要 `to_uslv` / `to_sslv`？** 在 `numeric_std` 下，把整数变成比特串要写一长串：

```vhdl
std_logic_vector(to_unsigned(123, 8))
```

而且必须明确说清楚是无符号（`to_unsigned`）还是有符号（`to_signed`）。`to_uslv` 就是 `std_logic_vector(to_unsigned(...))` 的缩写，`to_sslv` 是有符号版。反向的 `from_uslv` / `from_sslv` 则把比特串还原成整数。

**为什么需要 `from_str`？** generic 经常只能写成字符串（比如把一串系数写成 `"1.0,2.5,-3.7"` 传进 TB 或代码生成器），而硬件代码里要用 real。`from_str` 就是手写的字符串→real 解析器，支持小数和科学计数法。它还有一个数组版重载，用逗号分隔。

#### 4.3.2 核心流程

- `to_uslv(input, len)` = `std_logic_vector(to_unsigned(input, len))`，无符号。
- `to_sslv(input, len)` = `std_logic_vector(to_signed(input, len))`，有符号。
- `from_uslv(input)` = `to_integer(unsigned(input))`，按无符号解读。
- `from_sslv(input)` = `to_integer(signed(input))`，按有符号解读（补码）。
- `from_str(input)`（real 版）：状态机式扫描字符串——跳过前导空白 → 读符号 → 读整数部分 → 遇到 `.` 读小数部分 → 遇到 `E/e` 读指数部分 → 组装成 `real`。
- `from_str(input)`（t_areal 版）：按逗号切分，对每一段调用 real 版 `from_str`，组装成 real 数组。元素个数由私有辅助函数 `count_array_str_elements` 数逗号得到。

#### 4.3.3 源码精读

**`to_uslv` / `to_sslv`** —— 一行包装：

```vhdl
function to_uslv(input : integer; len : integer) return std_logic_vector is
begin
  return std_logic_vector(to_unsigned(input, len));
end function;
```

> 见 [psi_common_math_pkg.vhd:L372-L376](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L372-L376)（`to_sslv` 见
> [L379-L383](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L379-L383)）。

**`from_uslv` / `from_sslv`** —— 反向一行包装：

```vhdl
function from_uslv(input : std_logic_vector) return integer is
begin
  return to_integer(unsigned(input));
end function;
```

> 见 [psi_common_math_pkg.vhd:L386-L389](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L386-L389)（`from_sslv` 见
> [L392-L395](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L392-L395)）。

**真实使用方**。`pulse_generator_ctrl_static` 用 `to_uslv` 把"全 1 的目标电平"直接写成常量：

```vhdl
constant tgt_lvl_c : std_logic_vector(width_g-1 downto 0) := to_uslv(2**width_g-1, width_g);
```

> 见 [psi_common_pulse_generator_ctrl_static.vhd:L59](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L59)。`delay_cfg` 则大量用 `from_uslv` 把寄存器里的延迟值读回整数做比较
> [psi_common_delay_cfg.vhd:L64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_delay_cfg.vhd#L64)。

**`from_str`（real 版）核心片段** —— 这里只看它如何处理整数、小数、指数三段（完整实现近 70 行）：

```vhdl
-- Parse Integer
while (Idx_v <= input'high) and (input(Idx_v) <= '9') and (input(Idx_v) >= '0') loop
  ValInt_v := ValInt_v * 10 + (character'pos(input(Idx_v)) - character'pos('0'));
  Idx_v    := Idx_v + 1;
end loop;
-- ...（遇到 '.' 解析小数部分，遇到 'E'/'e' 解析指数部分）...
ValAbs_v := (real(ValInt_v) + ValFrac_v / 10.0**real(FracDigits_v)) * 10.0**real(Exp_v);
```

> 见 [psi_common_math_pkg.vhd:L398-L468](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L398-L468)。它还跳过普通空格、不可间断空格（`character'val(160)`）和制表符，对从 GUI/配置文件粘贴的字符串很友好。
> t_areal 数组版见 [L471-L491](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L471-L491)，它依赖私有辅助函数 `count_array_str_elements` 数逗号
> [L134-L145](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L134-L145)。

#### 4.3.4 代码实践

**实践目标**：用 `to_uslv` 把整数转成 `std_logic_vector`，并验证 `to_uslv` / `from_uslv` 的可逆性。

**操作步骤**：

在 4.1.4 的练习 testbench 里追加：

```vhdl
-- 示例代码：整数与 slv 互转
constant VAL_C        : integer := 5;
constant SLV_C        : std_logic_vector(7 downto 0) := to_uslv(VAL_C, 8);  -- 期望 "00000101"
constant ROUNDTRIP_C  : integer := from_uslv(SLV_C);                          -- 期望 5
constant NEG_C        : std_logic_vector(7 downto 0) := to_sslv(-3, 8);       -- 期望 "11111101"
constant NEG_BACK_C   : integer := from_sslv(NEG_C);                           -- 期望 -3
```

**需要观察的现象**：`to_uslv(5,8)` 得到的比特串与手动展开的二进制一致；`to_sslv(-3,8)` 是 8 位补码。

**预期结果**：

| 表达式 | 结果 |
| --- | --- |
| `to_uslv(5, 8)` | `"00000101"` |
| `from_uslv("00000101")` | 5 |
| `to_sslv(-3, 8)` | `"11111101"`（8 位补码） |
| `from_sslv("11111101")` | -3 |

**待本地验证**：`report` 打印 `std_logic_vector` 时不同仿真器格式可能不同（有的带双引号、有的用 `std_logic_vector'("...")`），但比特值如上表。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `to_sslv(-3, 8)` 和 `to_uslv(253, 8)` 得到的比特串完全一样（都是 `"11111101"`）？

> 答案：8 位补码下 -3 的二进制就是 `11111101`；而 253 的无符号二进制也是 `11111101`。同样的比特串，按 `signed` 解读是 -3，按 `unsigned` 解读是 253——这正是 `to_uslv`/`to_sslv` 必须分开的原因。

**练习 2**：`from_str("1.5e3")` 等于多少？

> 答案：1500.0。整数部分 1，小数部分 5（2 位），指数 3，合起来 \(1.5 \times 10^3 = 1500.0\)。

---

### 4.4 比例与幂次

#### 4.4.1 概念说明

最后一组函数服务于"频率/计数"场景：

- **`ratio(ina, inb)`**：返回两个频率（或周期）的整数比，且**向上取整**。常用来算"为了从时钟 A 生成频率更低的选通，计数器要数到几"。
- **`is_int_ratio(a, b)`**：判断两个频率的比值是不是整数。后续讲义会看到，整数比时钟域之间的跨越（`sync_cc_n2xn` / `sync_cc_xn2n`）依赖这个前提。
- **`nearest_pow2(a)`**：返回大于等于 `a` 的最小 2 的幂。源码注释明确写它"用于 AXI 寄存器个数取整、不可综合"——也就是说它是给地址译码/Tcl 脚本算常量用的，不是数据通路里的硬件。

#### 4.4.2 核心流程

**`ratio`**：先把大小关系理顺（总是大除以小），再做 ceil。

\[
\text{ratio}(a,b) = \left\lceil \frac{\max(a,b)}{\min(a,b)} \right\rceil
\]

若 `a = b`，打一条 warning 并返回 1。

**`is_int_ratio(a,b)`**：同样先大除以小，然后判断商是否等于 `ceil(商)` 或 `floor(商)`（即商本身就是整数）。

**`nearest_pow2(a)`**：从 \(i=0\) 起扫描，找到第一个满足 \(2^i \geq a\) 的 \(i\)，返回 \(2^i\)。

```
for i in 0 .. 31:
    if 2**i >= a:  return 2**i
```

#### 4.4.3 源码精读

**`ratio`（integer 重载）** —— 大除以小再 ceil，相等时告警：

```vhdl
function ratio(ina : integer; inb : integer) return integer is
  variable res : integer;
begin
  if ina > inb then      res := integer(ceil(real(ina) / real(inb)));
  elsif ina = inb then    assert false report "both freq. are similar" severity warning;
                          res := 1;
  else                    res := integer(ceil(real(inb) / real(ina)));
  end if;
  return res;
end function;
```

> 见 [psi_common_math_pkg.vhd:L509-L521](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L509-L521)（real 重载见
> [L494-L506](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L494-L506)）。它用了 `ieee.math_real` 的 `ceil`。

> 📌 **诚实提醒**：库里并非所有组件都用这个 `ratio` 函数。例如 `strobe_generator` 就直接手写了等价表达式 `integer(ceil(freq_clock_g / freq_strobe_g))`（见 [psi_common_strobe_generator.vhd:L30](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L30)），而没有调用 `ratio`——因为它是单向（时钟一定快于选通），不需要 ratio 的"大除小"通用化。读到这种"内联实现"时不要困惑，本质是同一个公式。

**`is_int_ratio`** —— 判断商是否整数：

```vhdl
if a_v / b_v = ceil(a_v / b_v) or a_v / b_v = floor(a_v / b_v) then
  return true;
else
  return false;
end if;
```

> 见 [psi_common_math_pkg.vhd:L572-L590](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L572-L590)（integer 重载见
> [L593-L612](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L593-L612)）。它将在 u5-l3（同步整数比跨越）里成为前置判断。

**`nearest_pow2`** —— 扫描找首个足够大的 2 的幂：

```vhdl
function nearest_pow2(a: integer) return integer is
    variable power_of_two : unsigned(31 downto 0);
    variable num_bits     : integer;
  begin
    num_bits     := 32;
    power_of_two := (others => '0');
    for i in 0 to num_bits-1 loop
        if (2**i >= a) then
            power_of_two    := (others => '0');
            power_of_two(i) := '1';
            exit;
        end if;
    end loop;
    return to_integer(power_of_two);
end function;
```

> 见 [psi_common_math_pkg.vhd:L615-L630](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L615-L630)。注释 [L120](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L120) 写明它用于 AXI 寄存器计数且不可综合。

> ⚠️ **局限**：循环只到 `i=31`，所以 `a > 2^31` 时 `power_of_two` 保持全 0，函数返回 0。此外 `a <= 0` 时第一轮 `2**0=1 >= a` 即命中，返回 1。日常用不到这么大的值，但要知道边界。

#### 4.4.4 代码实践

**实践目标**：用 `ratio` 算出"100 MHz 时钟下生成 1 kHz 选通"的计数值，并理解它与 `strobe_generator` 的关系。

**操作步骤**：

1. 在练习 testbench 里写：

```vhdl
-- 示例代码：频率比计算
constant F_CLK_C   : real := 100.0e6;  -- 100 MHz
constant F_STR_C   : real := 1.0e3;    -- 1 kHz
constant RATIO_C   : integer := ratio(F_CLK_C, F_STR_C);  -- 期望 100_000
constant IS_INT_C  : boolean := is_int_ratio(F_CLK_C, F_STR_C);  -- 期望 true
constant NP2_C     : integer := nearest_pow2(5);   -- 期望 8
constant NP2_POW_C : integer := nearest_pow2(4);   -- 期望 4
```

2. 打印这些常量。
3. 对照 `strobe_generator.vhd` 第 30 行的 `ratio_c`，确认它和这里的 `RATIO_C` 是同一个数学含义。

**需要观察的现象**：`ratio` 返回的是计数器的"数到几就翻转"的上限值；`is_int_ratio` 为真说明这两个频率能精确分频（无小数残留）。

**预期结果**：

| 表达式 | 结果 |
| --- | --- |
| `ratio(100.0e6, 1.0e3)` | 100000 |
| `is_int_ratio(100.0e6, 1.0e3)` | true |
| `nearest_pow2(5)` | 8 |
| `nearest_pow2(4)` | 4 |

**待本地验证**：`ratio` 在 `a=b` 时会触发 `assert ... severity warning`，仿真器控制台会出现一条警告（这并非错误，是设计意图）。

#### 4.4.5 小练习与答案

**练习 1**：`ratio(100.0, 30.0)` 等于多少？为什么是向上取整？

> 答案：`ceil(100/30) = ceil(3.33) = 4`。向上取整是为了保证计数器计满一次的时间**不短于**一个选通周期——计数器只能数整数拍，宁可慢一点也不能让生成的选通频率偏高。

**练习 2**：`is_int_ratio(100.0, 30.0)` 返回什么？这对 `sync_cc_n2xn` 意味着什么？

> 答案：返回 false（100/30≈3.33 不是整数）。这意味着两个时钟不是整数倍关系，**不能**用同步整数比跨越组件，必须改用真正的异步 CDC（见 u5-l1/u5-l2）。

---

## 5. 综合实践

把四个模块串起来，设计一个**微型可配置 RAM 包装器**的"声明区"（只写常量与端口，不写架构体），它要同时用到本讲全部四类函数。

任务：给定 generic `depth_g`（RAM 深度）与 `use_full_range_g`（布尔），完成下面的声明：

```vhdl
-- 示例代码：综合实践，仅 entity 声明部分
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.psi_common_math_pkg.all;

entity tiny_ram_wrap is
  generic(
    depth_g           : positive := 1024;
    use_full_range_g  : boolean  := false
  );
  port(
    -- 4.1 用 log2ceil 推导地址位宽
    addr_i  : in  std_logic_vector(log2ceil(depth_g) - 1 downto 0);
    -- 4.2 用 choose 根据 use_full_range_g 选择数据位宽：全量 16 位 或 精简 8 位
    dat_i   : in  std_logic_vector(choose(use_full_range_g, 15, 7) downto 0);
    -- 4.3 用 to_uslv 把一个默认常量写进去（例如复位时数据线拉满）
    def_dat : out std_logic_vector(choose(use_full_range_g, 15, 7) downto 0) := to_uslv(0, 16);
    -- 4.4 用 isLog2 暴露"当前深度是否为 2 的幂"，便于上游知道能否做地址位宽压缩
    is_pow2 : out boolean := isLog2(depth_g)
  );
end entity;
```

完成后，回答：

1. `depth_g = 1024`、`use_full_range_g = true` 时，`addr_i` 是几位？`dat_i` 呢？`is_pow2` 是什么？
2. 把 `depth_g` 改成 1000，上述答案怎么变？
3. `def_dat` 这一行用 `to_uslv(0, 16)` 是否一定与 `dat_i` 等宽？如果不一致，应该怎么改？（提示：把长度也用 `choose` 表达。）

参考答案：

1. `addr_i` = 10 位，`dat_i` = 16 位，`is_pow2` = true。
2. `addr_i` 仍为 10 位（`log2ceil(1000)=10`），`dat_i` 不变 = 16 位（与深度无关），`is_pow2` = false。
3. 不一定等宽：当 `use_full_range_g=false` 时 `dat_i` 是 8 位而 `to_uslv(0,16)` 是 16 位，赋值会因宽度不匹配报错。应改为 `to_uslv(0, choose(use_full_range_g, 16, 8))`，让长度也随条件变化。这个练习把 `choose`、`log2ceil`、`to_uslv`、`isLog2` 四个函数在同一个 entity 声明里协作起来了。

## 6. 本讲小结

- `log2ceil(n)` 给出编号 `n` 个值所需的二进制位宽（向上取整），是 RAM 地址、指针、电平端口的标准推导工具；`log2` 是向下取整版，`isLog2` 判断 2 的幂。
- `max/min` 有 integer/real 重载；`choose(s,t,f)` 是 VHDL 里"端口声明区的三元运算符"，靠 8 个类型重载覆盖几乎所有常用类型。
- `to_uslv`/`to_sslv` 把整数变 `std_logic_vector`，`from_uslv`/`from_sslv` 反向；`from_str` 是支持小数与科学计数法的字符串→real 解析器，并有逗号分隔的数组重载。
- `ratio` 给出大/小频率比的向上取整，`is_int_ratio` 判断两频率是否整数倍（同步 CDC 的前提），`nearest_pow2` 返回≥a 的最小 2 的幂（用于 AXI 寄存器计数等常量推导，注释标注不可综合）。
- 这些函数几乎都是编译期求值，不产生硬件；使用它们能让组件端口与常量随 generic 自动伸缩——这是 psi_common 全库"全 generic 化"风格的具体支撑。
- 函数也有边界与局限：`max_a/min_a` 初值为 0（对全负数组不准）、`nearest_pow2` 对 `a>2^31` 返回 0、`ratio` 在两值相等时打 warning。自己扩展使用时要心里有数。

## 7. 下一步学习建议

1. **学 array_pkg（u2-l3）**：本讲的 `count`、`max_a`、`min_a`、`from_str(t_areal)` 都依赖 `t_ainteger`/`t_areal`/`t_abool`，去读 `psi_common_array_pkg.vhd` 弄清这些数组类型如何声明、约束与无约束有何区别。
2. **学 logic_pkg（u2-l2）**：它提供格雷码转换、归约等工具，与 `log2ceil` 一起在 u4-l2 异步 FIFO 里组合使用。
3. **进入存储组件（u3-l1）**：带着 `log2ceil` 去 [psi_common_sdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd) 实战，看地址位宽、字节使能、RAM 行为（RBW/WBR）如何围绕这些常量搭建——这是验证你是否真懂 math_pkg 的最好场景。
4. **回头看 strobe（u6-l1）**：对照 `ratio` 函数与 `strobe_generator` 第 30 行的内联 `ceil(...)`，体会"库函数 vs 内联公式"的取舍。
