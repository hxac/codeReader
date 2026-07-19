# psi_common_array_pkg 数组类型

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说出 `psi_common_array_pkg` 里预定义了哪些数组类型，以及它们的命名规律（`t_a` 前缀）。
- 区分「元素宽度固定的 `t_aslvN`」与「元素宽度也无约束的 `t_aslv`」，并知道各自何时使用。
- 理解为什么在 VHDL-2008 完全可综合被各家工具广泛支持之前，这样一组「固定宽度的 slv 数组」是必要的。
- 看懂库中真实使用这些类型的代码（如 AXI 从机的寄存器数组端口）。
- 把本包与 `psi_common_math_pkg` 配合起来使用，例如用 `count` 统计数组元素。

本讲只读类型定义，不描述具体硬件行为——这些类型本身是「编译期」的建模工具，本身不产生逻辑门。

## 2. 前置知识

在开始前，读者应已经了解（来自前面几讲）：

- **VHDL 的类型系统基础**：`type` 声明、`array`、`std_logic_vector`、`integer`、`boolean`、`real`。
- **generic 化**：psi_common 全库追求「一切都用 generic 参数化」，因此端口宽度、数组长度经常需要随 generic 自动确定（见 u1-l1、u2-l1）。
- **package 的作用**：`psi_common_math_pkg` 已经在 u2-l1 讲过，本讲会承接它。

本讲会用到两个对初学者可能陌生的概念，先做通俗解释：

- **约束（constrained）与无约束（unconstrained）**：VHDL 里 `std_logic_vector(7 downto 0)` 是「已经约束好宽度」的子类型；而 `std_logic_vector` 单独写、不给范围时，是「无约束」的，等声明信号时再用 `(7 downto 0)` 把它定下来。数组类型同理：数组的「长度」可以无约束（声明时再定），数组的「元素」也可以无约束（声明时再定）。
- **数组的数组**：我们想表达「一组 slv 字」（比如 8 个 32 位寄存器）。这在 VHDL 里就是一个「元素类型是 slv 的数组」。本讲的核心就是 psi_common 为此预先准备了哪些类型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_common_array_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_array_pkg.vhd) | 本讲主角。声明了 `t_aslvN` 固定宽度 slv 数组家族、无约束 `t_aslv`，以及 `t_ainteger`/`t_areal`/`t_abool` 三个标量数组类型。 |
| [hdl/psi_common_math_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd) | 数学工具包，**依赖** array_pkg（`use work.psi_common_array_pkg.all`），其 `count`/`max_a`/`min_a`/`choose(t_areal)` 等函数就是作用在这些数组类型上的。 |

另外，本讲会引用几个「真实使用方」作为例子：AXI 从机的寄存器数组端口（`psi_common_axi_slave_ipif.vhd`、`psi_common_axi_slave_ipif64.vhd`、`psi_common_axilite_slave_ipif.vhd`）与脉冲发生器的步进常量（`psi_common_pulse_generator_ctrl_static.vhd`）。

## 4. 核心概念与源码讲解

### 4.1 t_aslvN 固定宽度数组

#### 4.1.1 概念说明

`psi_common_array_pkg` 提供了一大批名为 `t_aslvN` 的类型，其中 `N` 是固定的比特宽度。例如 `t_aslv16` 表示「元素是 16 位 `std_logic_vector(15 downto 0)` 的数组」。

命名规律：

- `t_` —— 这是一个 `type`。
- `a` —— array，数组。
- `slv` —— 元素是 `std_logic_vector`。
- `N` —— 每个元素的固定位宽。

所以 `t_aslv16` 读作「16 位 slv 的数组」，`t_aslv32` 读作「32 位 slv 的数组」。

为什么需要这样一族固定宽度的类型？这是本讲的核心理解之一：**在 VHDL-2008 之前，你不能声明一个「元素宽度也无约束」的 slv 数组并期望它在所有综合工具里都能干净地综合。** 详见 4.3 节。为绕开这一限制，psi_common 把元素宽度「钉死」在一个个具体类型里（`t_aslv2`…`t_aslv64`），只留下「数组长度」是无约束的——而「长度无约束的数组」在 VHDL-93/2002 里就是完全合法、可综合的。

#### 4.1.2 核心流程

使用 `t_aslvN` 的典型流程：

1. 在实体/包/结构体里 `use work.psi_common_array_pkg.all;`。
2. 选择一个宽度匹配的类型，例如要存「若干个 32 位寄存器」就选 `t_aslv32`。
3. 在声明信号/端口/常量时，用 `(0 to N-1)` 或 `(N-1 downto 0)` 把**长度**约束下来：

```vhdl
signal reg_array : t_aslv32(0 to 7);   -- 8 个 32 位寄存器
```

4. 像访问普通二维对象一样读写：`reg_array(3)` 取出第 4 个字（一个 32 位 slv），`reg_array(3)(7 downto 0)` 取出该字的低字节。

> 注意：被钉死的是「每个元素几位」，而「数组有几个元素」由你在声明时决定。这与 generic 化完美契合——例如 AXI 从机用 `t_aslv32(0 to num_reg_g - 1)`，让寄存器数量随 generic `num_reg_g` 自动伸缩（见 4.1.3 真实用法）。

#### 4.1.3 源码精读

固定宽度 slv 数组家族集中定义在包头部，从 2 位一直到 30 位逐位声明，之后还有若干「大宽度」类型：

[hdl/psi_common_array_pkg.vhd:L13-L46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_array_pkg.vhd#L13-L46) —— 逐行声明 `t_aslv2`…`t_aslv30`，再补 `t_aslv32`、`t_aslv36`、`t_aslv48`、`t_aslv64`、`t_aslv512`。每一行的形式完全一致：

```vhdl
type t_aslv16 is array (natural range <>) of std_logic_vector(15 downto 0);
```

这一行（[L27](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_array_pkg.vhd#L27)）的含义拆解：

- `array (natural range <>)`：数组长度无约束，索引类型是 `natural`，声明信号时再给范围。
- `of std_logic_vector(15 downto 0)`：每个元素是一个 **固定 16 位** 的 slv。

> 重要细节：预定义宽度**并不是**「2 到 64 每一位都有」。仔细看 [L13-L46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_array_pkg.vhd#L13-L46) 可以发现，只有 2–30 逐位、再加上 32、36、48、64、512。**没有 `t_aslv31`、`t_aslv33`** 等。若你需要一个不在表里的宽度（例如 31 位），可以选最近的大一号类型（`t_aslv32`）并只用到其中 31 位，或在自己的项目里仿照这一行声明一个私有类型。

**真实使用方（最重要的例证）**：AXI4 从机 IP 接口把「若干个寄存器」直接建模成 `t_aslv32` 端口，长度随 `num_reg_g` 变化：

[hdl/psi_common_axi_slave_ipif.vhd:L80-L82](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L80-L82) —— 读数据 `i_reg_rdata : in t_aslv32(0 to num_reg_g - 1)`、写数据 `o_reg_wdata : out t_aslv32(0 to num_reg_g - 1)`。寄存器复位值也用同一类型作 generic 默认值（`rst_val_g : t_aslv32`）。

64 位变体同理使用 `t_aslv64`：

[hdl/psi_common_axi_slave_ipif64.vhd:L86-L88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif64.vhd#L86-L88) —— 64 位寄存器数组的读/写端口。AXI-Lite 从机也用 `t_aslv32` 承载寄存器端口（[hdl/psi_common_axilite_slave_ipif.vhd:L58-L60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axilite_slave_ipif.vhd#L58-L60)）。

这说明 `t_aslvN` 在库里并不是「定义了没人用」的摆设，而是 AXI 寄存器接口的标准数据载体。

#### 4.1.4 代码实践（源码阅读型）

目标：体会「固定宽度、长度可变」如何在真实端口上落地。

1. 打开 [hdl/psi_common_axi_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd)，找到 [L80-L82](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L80-L82) 的 `i_reg_rdata` / `o_reg_wdata`。
2. 在同文件里搜索 generic `num_reg_g` 的声明。
3. 回答：如果把 `num_reg_g` 设为 16，`o_reg_wdata` 这个端口实际会变成多少位宽的总线？（提示：16 个 32 位字。）
4. 预期结论：端口宽度随 generic 自动伸缩，这正是「元素宽度钉死、数组长度无约束」带来的好处。

> 待本地验证：若有 Modelsim/GHDL，可按 u1-l3 的方式编译该实体并打印 `num_reg_g=16` 时的端口信息。

#### 4.1.5 小练习与答案

**练习 1**：库里有 `t_aslv16` 但没有 `t_aslv31`。如果你确实需要一个「31 位 slv 的数组」，最省事的做法是什么？

**参考答案**：直接用 `t_aslv32`，每个元素只用低 31 位、最高位留 0；或在自己工程里加一行 `type t_my_aslv31 is array (natural range <>) of std_logic_vector(30 downto 0);`。不建议去改 array_pkg。

**练习 2**：声明一个包含 4 个 16 位常数的常量数组 `data_c`，初值依次为 `X"000F"`、`X"00FF"`、`X"0F00"`、`X"FFFF"`。

**参考答案**：

```vhdl
constant data_c : t_aslv16(0 to 3) := (X"000F", X"00FF", X"0F00", X"FFFF");
```

（此常量会在第 5 节综合实践中继续使用。）

---

### 4.2 t_ainteger / t_areal / t_abool 标量数组

#### 4.2.1 概念说明

除了 slv 数组，包里还声明了三种「标量数组」：

- `t_ainteger` —— integer 的数组。
- `t_areal` —— real 的数组。
- `t_abool` —— boolean 的数组。

命名规律同前：`t_a` + 元素类型。这些类型用来存放「一组配置值」，比如多级斜坡的步进表、一组增益系数、一组使能开关。因为元素本身是标量（宽度天然确定），所以它们的可综合性一直没问题，更多是为了**书写方便**和**与 math_pkg 的函数对齐**。

#### 4.2.2 核心流程

典型用法：

```vhdl
-- 一组整数配置（如步进值）
constant steps_c : t_ainteger(0 to 3) := (10, 20, 30, 40);
-- 一组实数（如增益）
constant gains_c : t_areal(0 to 2) := (1.0, 0.5, 0.25);
-- 一组开关
constant en_c : t_abool(0 to 1) := (true, false);
```

声明后即可按下标读写，也能整体传给 math_pkg 里以数组为参数的函数（见 4.4 节）。

#### 4.2.3 源码精读

三种标量数组紧凑地声明在包尾部：

[hdl/psi_common_array_pkg.vhd:L49-L51](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_array_pkg.vhd#L49-L51) —— `t_ainteger`、`t_areal`、`t_abool` 三行，形式都是 `type t_aXXX is array (natural range <>) of XXX;`。

**真实使用方**：脉冲发生器用 `t_ainteger` 存放「前半段/后半段」的步进数：

[hdl/psi_common_pulse_generator_ctrl_static.vhd:L57](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L57) —— `constant step_array_c : t_ainteger(1 downto 0) := (nb_step_flh_g, nb_step_fll_g);` 注意这里索引用了下降范围 `(1 downto 0)`，把两个 generic 打包成一个便于下标访问的常量数组。

而 `t_areal` 主要被 math_pkg 内部使用，例如把逗号分隔的字符串解析成实数数组（见 4.4 节）。

#### 4.2.4 代码实践（源码阅读型）

目标：确认标量数组在库里被「当作配置表」用。

1. 打开 [hdl/psi_common_pulse_generator_ctrl_static.vhd:L57](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pulse_generator_ctrl_static.vhd#L57)。
2. 找到 `step_array_c` 在后续代码里被读取的位置（搜索 `step_array_c(`）。
3. 观察：作者用 `step_array_c(0)` / `step_array_c(1)` 在两段逻辑之间复用同一组步进参数，避免重复书写 generic。
4. 预期结论：`t_ainteger` 在这里就是一张「两元素的配置表」。

#### 4.2.5 小练习与答案

**练习**：为什么 `t_ainteger`/`t_areal`/`t_abool` 不像 `t_aslv` 那样需要预先声明「一堆不同宽度的变体」？

**参考答案**：因为 `integer`/`real`/`boolean` 是标量，元素本身没有「位宽」需要约束；只有 `std_logic_vector` 才有「元素宽度」这个维度，所以才需要 `t_aslvN` 家族把宽度钉死。

---

### 4.3 无约束 t_aslv

#### 4.3.1 概念说明

除了固定宽度的 `t_aslvN`，包里还声明了一个「最自由」的类型 `t_aslv`：

```vhdl
type t_aslv is array (natural range <>) of std_logic_vector;
```

它不仅「数组长度无约束」，连**每个元素的宽度也无约束**（元素是无约束的 `std_logic_vector`）。这是 VHDL-2008 才正式允许的写法（「无约束数组元素」 unconstrained array elements）。

#### 4.3.2 核心流程（与固定宽度对比）

| 维度 | `t_aslv16` 等 `t_aslvN` | `t_aslv` |
| --- | --- | --- |
| 元素宽度 | 在类型里钉死（如 16 位） | 无约束，每个元素可不同宽 |
| 数组长度 | 声明时约束 | 声明时约束 |
| VHDL 标准 | VHDL-93/2002/2008 均可 | 实质需要 VHDL-2008 |
| 可综合性/可移植性 | 好，全库主用 | 较弱，老工具可能不支持 |
| 典型场景 | 寄存器数组、AXI 数据通路 | 需要存放异构宽度 slv 的特殊场合 |

结论：**库实际使用的是 `t_aslvN` 家族**（4.1.3 的 AXI 例子就是证明）。`t_aslv` 是为「最大灵活性」预留的，但因为它对工具的 VHDL-2008 支持有要求，库的公开端口几乎不用它——这也正好回答了本讲标题里的问题：

> 「为什么在 VHDL-2008 全面可综合之前，`t_aslvN` 这样一族固定宽度类型仍然必要？」
>
> 因为「元素宽度也无约束的 slv 数组」（即 `t_aslv` 的写法）在 VHDL-2008 之前并不合法、也不被综合工具普遍支持；要让「一组 slv 字」在老标准下就能干净地综合且跨厂商可移植，最稳妥的办法就是把宽度钉死在类型里，只留长度可变——这正是 `t_aslvN` 家族存在的原因。

#### 4.3.3 源码精读

[hdl/psi_common_array_pkg.vhd:L48](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_array_pkg.vhd#L48) —— 唯一一行：

```vhdl
type t_aslv is array (natural range <>) of std_logic_vector;
```

注意末尾 `of std_logic_vector` 后面**没有** `(N downto 0)`，这正是「元素无约束」的标志。与之对照，[L13-L46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_array_pkg.vhd#L13-L46) 的每个 `t_aslvN` 末尾都带 `(N-1 downto 0)`。

> 你可以用 `grep "t_aslv\b" hdl/` 验证：库内实体端口大量使用 `t_aslv32`/`t_aslv64`，但几乎不直接使用无约束的 `t_aslv`。

#### 4.3.4 代码实践（源码阅读型）

目标：用搜索结果印证「库主用固定宽度、几乎不用无约束 t_aslv」。

1. 在仓库根目录执行（只读操作）：

   ```bash
   grep -rn "t_aslv32\|t_aslv64" hdl/ | head
   grep -rn ": t_aslv[^0-9]" hdl/ | head
   ```

2. 观察前者有大量命中（AXI 端口），后者几乎没有命中。
3. 预期结论：固定宽度家族是工程主力，无约束 `t_aslv` 是「备用」。

> 待本地验证：不同工具链对无约束 `t_aslv` 的支持差异，需在你自己的综合器上确认。

#### 4.3.5 小练习与答案

**练习**：判断对错——「`t_aslv` 比 `t_aslv16` 更通用，所以新代码应当一律优先用 `t_aslv`。」

**参考答案**：错。`t_aslv` 依赖 VHDL-2008 的「无约束数组元素」，可综合性/可移植性弱于 `t_aslvN`。库的公开端口几乎都用 `t_aslvN`。除非确实需要存「不同宽度的 slv」，否则应优先用与位宽匹配的 `t_aslvN`。

---

### 4.4 与 math_pkg 的协作

#### 4.4.1 概念说明

`psi_common_math_pkg` 与本包不是平级的两块——**math_pkg 依赖 array_pkg**。math_pkg 顶部直接 `use work.psi_common_array_pkg.all;`，它的一批函数（`count`、`max_a`、`min_a`、`choose(t_areal,…)`、`from_str→t_areal`）正是以本包的数组类型作为形参类型。换句话说，两个包合在一起才构成「带数组运算的工具集」。

#### 4.4.2 核心流程

调用 math_pkg 里作用在数组上的函数时，实参的类型就来自 array_pkg：

- `count(a : t_ainteger; v : integer)` —— 数整数组里等于 `v` 的元素个数。
- `count(a : t_abool; v : boolean)` —— 数布尔数组里等于 `v` 的元素个数。
- `count(a : std_logic_vector; v : std_logic)` —— 数**单个 slv** 里等于 `v` 的比特个数。
- `max_a` / `min_a` —— 求整数/实数数组的最大/最小值。

> 关键事实（务必记住）：**`count` 没有 `t_aslvN`（slv 数组）的重载**。它只能数「整数数组」「布尔数组」或「单个 slv 的各位」。所以「对一组 slv 统计某比特出现次数」不能直接传一个 `t_aslv16` 给 `count`，而要把这组字**拼接成一个 slv**，再调用 `count(slv, sl)` 这个重载。这一点在第 5 节综合实践中会实际用到。

#### 4.4.3 源码精读

math_pkg 依赖 array_pkg 的证据在它的第 12 行：

[hdl/psi_common_math_pkg.vhd:L12](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L12) —— `use work.psi_common_array_pkg.all;`。

`count` 的三个重载声明（注意没有任何一个是 `t_aslvN`）：

[hdl/psi_common_math_pkg.vhd:L72-L80](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L72-L80) —— 依次是 `count(t_ainteger, integer)`、`count(t_abool, boolean)`、`count(std_logic_vector, std_logic)`。

`count` 作用于单个 slv 的实现：

[hdl/psi_common_math_pkg.vhd:L358-L369](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L358-L369) —— 用 `for idx in a'low to a'high` 遍历 slv 的每一位，遇 `a(idx) = v` 就 `cnt_v := cnt_v + 1`，最后返回计数。这是后续「拼接后统计比特」会用到的真实实现。

`count` 作用于 `t_ainteger` 的实现结构完全相同：

[hdl/psi_common_math_pkg.vhd:L332-L343](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L332-L343) —— 遍历数组、比较、累加。

#### 4.4.4 代码实践（阅读 + 推理型）

目标：用真实重载验证「count 不能直接吃 slv 数组」。

1. 阅读 [hdl/psi_common_math_pkg.vhd:L72-L80](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L72-L80)，列出 `count` 的全部形参类型。
2. 确认没有 `count(a : t_aslv16; …)` 这一行。
3. 推论：若直接写 `count(data_c, '1')`（`data_c` 是 4.1.5 里的 `t_aslv16`），VHDL 分析器会因为找不到匹配的重载而报错。
4. 预期结论：必须改用「拼接成单个 slv 再 `count(slv, sl)`」的写法（见第 5 节）。

#### 4.4.5 小练习与答案

**练习 1**：给定 `constant v : t_ainteger(0 to 5) := (10, 20, 10, 30, 10, 5);`，`count(v, 10)` 的返回值是多少？

**参考答案**：3（值为 10 的元素有 3 个）。对应实现见 [L332-L343](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L332-L343)。

**练习 2**：为什么 `count` 没有 `t_aslvN` 重载也能接受？

**参考答案**：slv 数组的「某比特」是二维概念（哪个字、哪一位），单一重载语义不明确；而把需要的字拼接成一条 slv 后，用现成的 `count(slv, sl)` 一维遍历即可达成「统计某比特出现次数」的目的，无需额外重载。

---

## 5. 综合实践

本任务把 4.1、4.4 串起来，完成规格里指定的实践：**声明一个 `t_aslv16` 数组存放 4 个 16 位常数，并用 math_pkg 的 `count` 统计某比特出现次数。**

由于 `count` 没有 slv 数组的重载（见 4.4），这里采用合法且诚实的写法：先把 4 个字拼接成一条 64 位 slv，再调用 `count(slv, sl)`。

**实践目标**：掌握 `t_aslv16` 的声明与初始化，并理解如何借助 `count(slv, sl)` 对一组 slv 做比特统计。

**操作步骤**（可写在一个最小的 testbench 或包体里，示例代码）：

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use work.psi_common_array_pkg.all;
use work.psi_common_math_pkg.all;

entity array_pkg_demo is
end entity;

architecture sim of array_pkg_demo is
  -- 1) 用 t_aslv16 声明并初始化 4 个 16 位常数
  constant data_c : t_aslv16(0 to 3) := (X"000F", X"00FF", X"0F00", X"FFFF");

  -- 2) count 没有 t_aslv16 重载，把 4 个字拼接成一条 64 位 slv
  constant flat_c : std_logic_vector(63 downto 0) :=
       data_c(0) & data_c(1) & data_c(2) & data_c(3);

  -- 3) 用 math_pkg 现成的 count(slv, sl) 统计 '1' 的个数
  constant ones_c : integer := count(flat_c, '1');
begin
  -- 仅用于报告结果
  assert false
    report "number of '1' bits = " & integer'image(ones_c)
    severity note;
end architecture;
```

> 说明：上面的 `flat_c` / `ones_c` 是**示例代码**（不在 psi_common 原始源码中），仅用于演示如何把本包类型与 math_pkg 函数接起来。

**需要观察的现象**：

- 仿真开始即打印一行 `number of '1' bits = …`（`assert ... severity note` 触发的报告）。
- 编译阶段不应出现「`count` 找不到匹配重载」的错误——因为我们把 `t_aslv16` 拼成了 `std_logic_vector`。

**预期结果**：逐字数 1 的个数：

- `X"000F"` = `0000_0000_0000_1111` → 4 个 1
- `X"00FF"` = `0000_0000_1111_1111` → 8 个 1
- `X"0F00"` = `0000_1111_0000_0000` → 4 个 1
- `X"FFFF"` = `1111_1111_1111_1111` → 16 个 1

总计：

\[
\text{ones} = 4 + 8 + 4 + 16 = 32
\]

即打印应为 `number of '1' bits = 32`。

**若想统计 '0'**：把 `count(flat_c, '1')` 换成 `count(flat_c, '0')`，预期结果为 \( 64 - 32 = 32 \)（这组数据恰好 0、1 各半）。

**扩展（直接可用的数组重载）**：再声明一个整数数组，验证 `count` 对 `t_ainteger` 的原生支持：

```vhdl
constant v_c : t_ainteger(0 to 5) := (10, 20, 10, 30, 10, 5);
constant n10_c : integer := count(v_c, 10);   -- 预期 3
```

> 待本地验证：上述打印值需要在 Modelsim/GHDL 中实际运行确认（按 u1-l3 的仿真流程）。算术结果（32 与 3）是按位/按元素手算得到，供你核对仿真输出。

## 6. 本讲小结

- `psi_common_array_pkg` 用 `t_a` 前缀命名数组类型：`t_aslvN`（N 位 slv 数组）、`t_ainteger`/`t_areal`/`t_abool`（标量数组）。
- `t_aslvN` 家族把**元素宽度钉死、数组长度留作无约束**，这是让「一组 slv 字」在 VHDL-93/2002 下也能干净综合、跨厂商可移植的关键——也回答了「为何 VHDL-2008 全面可综合前仍需要它们」。
- 预定义宽度不是连续的：2–30 逐位，再加 32/36/48/64/512；没有 `t_aslv31` 等。
- 无约束 `t_aslv`（元素也无约束）依赖 VHDL-2008，库内公开端口几乎不用，主力是 `t_aslvN`。
- 真实用法：AXI 从机寄存器端口用 `t_aslv32`/`t_aslv64`，脉冲发生器用 `t_ainteger` 存步进表。
- math_pkg **依赖** array_pkg；`count` 只有 `t_ainteger`/`t_abool`/`std_logic_vector` 三个重载，**没有** slv 数组重载——对一组 slv 统计某比特需先拼接成单条 slv 再用 `count(slv, sl)`。

## 7. 下一步学习建议

- 下一讲 **u2-l4（psi_common_axi_pkg AXI 记录类型）** 会讲解如何用 `record` 把整组 AXI 信号收敛为两个方向的记录，与本章的「数组承载多个寄存器」思路互补，建议紧接着学。
- 想立刻看到 `t_aslv32` 在真实接口里的作用，可直接阅读 [hdl/psi_common_axi_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd)（对应 u9-l5）。
- 想深入理解 math_pkg 的其它数组函数（`max_a`/`min_a`/`from_str→t_areal`），可回看 u2-l1 中关于这些函数的说明，并对照本讲对 `count` 的分析。
